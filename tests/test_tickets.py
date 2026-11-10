"""Phase 3 tests: ledger claim rules, freeze, ticket lifecycle via the gateway,
command-handler smoke tests, and the Phase 2 flow (add -> claim -> fetch ->
complete)."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from ledger import (ClaimRefused, add_member, claim_ticket_enforced, create_pool,
                    create_ticket, get_ticket, init_ledger, is_frozen, list_members,
                    release_ticket, set_frozen, set_ticket_state)
from tickets import PoolService
from transport import FakeTransport
from identity import ensure_identity

OWNER = "owner-1"
SRC = {"user_id": OWNER}


def _memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


@pytest.fixture
def conn():
    c = _memdb()
    create_pool(c, "pool-1", "Team", "agent:maint", relay_url="off",
                owner_discord_user_id=OWNER)
    c.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
              " VALUES ('demo','pool-1','Demo',0)")
    c.commit()
    return c


def test_double_claim_refused(conn):
    create_ticket(conn, "t-1", "pool-1", "demo", "Add endpoint")
    claim_ticket_enforced(conn, "c-1", "t-1", "agent:alice", declared_paths=["src/a.py"])
    with pytest.raises(ClaimRefused, match="one-ticket-one-worker"):
        claim_ticket_enforced(conn, "c-2", "t-1", "agent:bob", declared_paths=["src/b.py"])


def test_overlap_refused_and_maintainer_override_recorded(conn):
    create_ticket(conn, "t-1", "pool-1", "demo", "First")
    create_ticket(conn, "t-2", "pool-1", "demo", "Second")
    claim_ticket_enforced(conn, "c-1", "t-1", "agent:alice", declared_paths=["src/a.py"])
    with pytest.raises(ClaimRefused, match="overlap"):
        claim_ticket_enforced(conn, "c-2", "t-2", "agent:bob", declared_paths=["src/a.py"])
    # Non-maintainer cannot override.
    with pytest.raises(ClaimRefused, match="maintainer"):
        claim_ticket_enforced(conn, "c-3", "t-2", "agent:bob",
                              declared_paths=["src/a.py"], maintainer_override=True)
    # Maintainer override succeeds and is recorded.
    claim = claim_ticket_enforced(conn, "c-4", "t-2", "agent:maint",
                                  declared_paths=["src/a.py"], maintainer_override=True)
    assert claim["state"] == "active"
    kinds = [r[0] for r in conn.execute(
        "SELECT kind FROM events WHERE pool_id='pool-1'")]
    assert "claim_override" in kinds


def test_overlap_allowed_on_different_repo(conn):
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
                 " VALUES ('other','pool-1','Other',0)")
    conn.commit()
    create_ticket(conn, "t-1", "pool-1", "demo", "First")
    create_ticket(conn, "t-2", "pool-1", "other", "Second")
    claim_ticket_enforced(conn, "c-1", "t-1", "agent:alice", declared_paths=["src/a.py"])
    claim_ticket_enforced(conn, "c-2", "t-2", "agent:bob", declared_paths=["src/a.py"])


def test_budget_fields_persisted(conn):
    create_ticket(conn, "t-1", "pool-1", "demo", "Budgeted")
    claim_ticket_enforced(conn, "c-1", "t-1", "agent:alice",
                          declared_paths=["src/a.py"],
                          budget={"max_turns": 9, "token_cap": 12345})
    row = conn.execute("SELECT budget_json FROM claims WHERE claim_id='c-1'").fetchone()
    budget = json.loads(row[0])
    assert budget == {"max_turns": 9, "max_tasks_per_day": 5, "token_cap": 12345,
                      "wall_clock_seconds": 1800}
    create_ticket(conn, "t-2", "pool-1", "demo", "Bad budget")
    with pytest.raises(ValueError, match="positive int"):
        claim_ticket_enforced(conn, "c-2", "t-2", "agent:bob", declared_paths=["x"],
                              budget={"max_turns": 0})


def test_claim_without_paths_is_whole_ticket(conn):
    """Empty declared_paths = whole-ticket claim (no paths pre-declared);
    recorded with the '*' sentinel and blocks later path claims on the
    same repo (project-registry feature)."""
    create_ticket(conn, "t-1", "pool-1", "demo", "Whole ticket")
    claim = claim_ticket_enforced(conn, "c-1", "t-1", "agent:alice", declared_paths=[])
    assert claim["state"] == "active" and claim["whole_ticket"] is True
    create_ticket(conn, "t-2", "pool-1", "demo", "Second")
    with pytest.raises(ClaimRefused, match="overlap"):
        claim_ticket_enforced(conn, "c-2", "t-2", "agent:bob",
                              declared_paths=["src/a.py"])


def test_release_frees_claim(conn):
    create_ticket(conn, "t-1", "pool-1", "demo", "Releasable")
    claim_ticket_enforced(conn, "c-1", "t-1", "agent:alice", declared_paths=["src/a.py"])
    with pytest.raises(ClaimRefused, match="only the claimant"):
        release_ticket(conn, "t-1", "agent:bob")
    release_ticket(conn, "t-1", "agent:alice")
    assert get_ticket(conn, "t-1")["state"] == "open"
    assert conn.execute("SELECT COUNT(*) FROM file_claims").fetchone()[0] == 0
    # Another worker can now claim the same paths.
    claim_ticket_enforced(conn, "c-2", "t-1", "agent:bob", declared_paths=["src/a.py"])


def test_freeze_blocks_claims(conn):
    create_ticket(conn, "t-1", "pool-1", "demo", "Frozen")
    set_frozen(conn, "pool-1", True)
    assert is_frozen(conn, "pool-1") is True
    with pytest.raises(ClaimRefused, match="frozen"):
        claim_ticket_enforced(conn, "c-1", "t-1", "agent:alice", declared_paths=["src/a.py"])
    set_frozen(conn, "pool-1", False)
    claim_ticket_enforced(conn, "c-1", "t-1", "agent:alice", declared_paths=["src/a.py"])


def test_state_transitions(conn):
    create_ticket(conn, "t-1", "pool-1", "demo", "Lifecycle")
    # Phase 1: only open->claimed->completed / claimed->open are legal.
    with pytest.raises(ValueError, match="not allowed"):
        set_ticket_state(conn, "t-1", "completed")
    with pytest.raises(ValueError, match="unknown ticket state"):
        set_ticket_state(conn, "t-1", "merged")
    set_ticket_state(conn, "t-1", "claimed")
    set_ticket_state(conn, "t-1", "open")
    set_ticket_state(conn, "t-1", "claimed")
    set_ticket_state(conn, "t-1", "completed")
    assert get_ticket(conn, "t-1")["state"] == "completed"
    with pytest.raises(ValueError, match="not allowed"):
        set_ticket_state(conn, "t-1", "open")


def test_member_roles_persist(conn):
    add_member(conn, "pool-1", "agent:alice", role="maintainer", box_pubkey_hex="ab" * 32)
    add_member(conn, "pool-1", "agent:bob", role="member")
    roles = {m["key_id"]: m["role"] for m in list_members(conn, "pool-1")}
    assert roles == {"agent:alice": "maintainer", "agent:bob": "member"}


# --- service + gateway integration -----------------------------------------

def _as_gateway(conn, ident):
    """Phase 2 (WO-021): the test identity IS the gateway identity, so the
    pool's maintainer/owner-gateway must be that identity for owner verbs."""
    conn.execute("UPDATE pools SET maintainer_key_id=?, owner_gateway_key_id=?",
                 (ident.key_id, ident.key_id))
    conn.commit()


def _service(conn, gateway=None, ident=None, transport=None):
    # gateway arg ignored (legacy)
    if ident is None:
        raise ValueError('ident required')
    return PoolService(conn, transport or FakeTransport(),
                       caller_key_id=ident.key_id,
                       caller_signing_key=ident.signing_key,
                       caller_box_sk=ident.box_sk,
                       maintainer_discord_user_id=OWNER)


def test_ticket_creation_thread_and_state_marker(conn, tmp_path):
    ident = ensure_identity(tmp_path / "a")
    svc = _service(conn, None, ident)
    _as_gateway(conn, ident)
    ticket = svc.add_ticket("pool-1", "demo", "Fix the thing", brief="Scope: src/a.py. Accept: tests green.", source=SRC)
    assert ticket["state"] == "open"
    assert ticket["title"] == "Fix the thing"
    assert "src/a.py" in ticket.get("brief", "")
    assert "state=open" in ticket.get("notice", "")
    assert len(json.dumps(ticket).encode()) <= 4096


def test_full_flow_claim_fetch_complete(tmp_path):
    """add -> claim -> maintainer add_repo (accepted v1) -> fetch_repo
    manifest -> complete -> task list drops it, repo list keeps it.
    Maintainer and contributor are distinct local identities."""
    conn = _memdb()
    gateway = None
    transport = FakeTransport()
    maint = ensure_identity(tmp_path / "m")
    contrib = ensure_identity(tmp_path / "a")
    create_pool(conn, "pool-1", "Team", maint.key_id, relay_url="off",
                owner_discord_user_id=OWNER)
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
                 " VALUES ('demo','pool-1','Demo',0)")
    for ident, role in ((maint, "maintainer"), (contrib, "member")):
        conn.execute(
            "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
            " VALUES (?,?,?,0)",
            (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
             ident.box_pk.encode(RawEncoder).hex()))
        add_member(conn, "pool-1", ident.key_id, role=role,
                   box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex())
    conn.commit()
    maint_svc = _service(conn, gateway, maint, transport)
    work_svc = _service(conn, gateway, contrib, transport)
    # The contributor drives its OWN Discord user (distinct from the
    # owner), authorized + bound to the contributor key at enrollment.
    from ledger import authorize_user, bind_membership
    authorize_user(conn, "pool-1", "contrib-1")
    bind_membership(conn, "pool-1", "contrib-1", contrib.key_id)
    contrib_src = {"user_id": "contrib-1"}

    ticket = maint_svc.add_ticket("pool-1", "demo", "Add hello", brief="Scope: hello.txt", source=SRC)
    claimed = work_svc.claim(ticket["ticket_id"], declared_paths=["hello.txt"], source=contrib_src)
    assert claimed["state"] == "active"
    assert "start" not in claimed  # Phase 2: claim no longer returns a 'start' key

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "T"], check=True)
    (src / "hello.txt").write_text("hello agent pool\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "v1"], check=True)

    added = maint_svc.add_repo("pool-1", "demo", str(src), source=SRC)
    assert added["ok"] and added["version"] == 1 and added["state"] == "accepted"

    fetched = work_svc.fetch_repo("demo", source=contrib_src)
    assert fetched["ok"] and fetched["repo_id"] == "demo"
    manifest = json.loads(fetched["manifest_json"])
    assert manifest["repo_id"] == "demo" and manifest["version"] == 1
    assert fetched["manifest_hash"] and "transfer_ticket" in fetched

    completed = work_svc.complete(ticket["ticket_id"], source=contrib_src)
    assert completed["state"] == "completed"
    assert get_ticket(conn, ticket["ticket_id"])["state"] == "completed"

    tasks = work_svc.fetch_task_list(source=contrib_src)
    assert tasks["ok"]
    assert ticket["ticket_id"] not in [t["ticket_id"] for t in tasks["tasks"]]

    repos = work_svc.fetch_repo_list(source=contrib_src)
    assert repos["ok"]
    demo = [r for r in repos["repos"] if r["repo_id"] == "demo"]
    assert demo and demo[0]["accepted_version"] == 1


def test_freeze_via_service_blocks_claim_and_ticket(conn, tmp_path):
    ident = ensure_identity(tmp_path / "b")
    svc = _service(conn, None, ident)
    _as_gateway(conn, ident)
    svc.freeze("pool-1", True, source=SRC)
    with pytest.raises(ClaimRefused, match="frozen"):
        svc.add_ticket("pool-1", "demo", "Nope", source=SRC)
    svc.freeze("pool-1", False, source=SRC)
    ticket = svc.add_ticket("pool-1", "demo", "Again", source=SRC)
    svc.freeze("pool-1", True, source=SRC)
    with pytest.raises(ClaimRefused, match="frozen"):
        svc.claim(ticket["ticket_id"], declared_paths=["src/a.py"], source=SRC)


# --- command-handler smoke tests (one per command) --------------------------

def _load_plugin():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin_p3", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCtx:
    def __init__(self):
        self.tools, self.commands, self.config = {}, {}, {"transport": "fake"}
        self.state = {}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = handler

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler

    def register_platform_handler(self, platform, factory):
        pass

    def register_hook(self, name, handler):
        pass

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_register_wires_all_commands():
    plugin = _load_plugin()
    ctx = _FakeCtx()
    plugin.register(ctx)
    assert set(ctx.commands) == {"pool", "fetch", "task", "claim", "release",
                                 "push", "audit", "add-repo", "task_add", "task_list"}
    out = json.loads(ctx.tools["pool_status"]({}))
    assert out["ok"] is True and out["version"] == "2.8.0" and "phase" not in out


def test_command_handlers_smoke(tmp_path):
    """Drive every command handler with a patched service factory (no real DB)."""
    plugin = _load_plugin()
    conn = _memdb()
    ident = ensure_identity(tmp_path / "smoke")
    svc = _service(conn, None, ident)
    original = plugin._service_for
    plugin._service_for = lambda ctx, source=None: svc
    try:
        ctx = _FakeCtx()
        ctx.config["maintainer_discord_user_id"] = "owner-1"
        plugin.register(ctx)
        svc._maintainer_discord_user_id = "owner-1"
        # Whitelist model: register requires the owner discord id, and
        # every owner-only verb below is driven by that same id.
        OWNER = {"user_id": "owner-1"}
        out = json.loads(ctx.commands["pool"]("register pool-1 --name Team"))
        assert out["ok"] and out["maintainer"] == ident.key_id
        out = json.loads(ctx.commands["pool"]("auto-approve on --pool pool-1", source=OWNER))
        assert out["ok"] is False  # deleted in Phase 1
        # Phase 1: /pool invite is removed; membership comes from the
        # env/config whitelist, so the subcommand is unknown and the only
        # member is the maintainer.
        out = json.loads(ctx.commands["pool"]("invite pool-1 agent:alice --role member --discord-user alice-1", source=OWNER))
        assert out["ok"] is False
        out = json.loads(ctx.commands["pool"]("members pool-1", source=OWNER))
        assert {m["key_id"] for m in out["members"]} == {ident.key_id}
        out = json.loads(ctx.commands["task"](
            'add --pool-id pool-1 --repo demo --title "Fix it" --brief "Scope: a.py"',
            source=OWNER))
        assert out["ok"] and out["state"] == "open"
        ticket_id = out["ticket_id"]
        assert ticket_id.startswith("t-")
        out = json.loads(ctx.commands["task"]("list --pool-id pool-1", source=OWNER))
        assert [t["ticket_id"] for t in out["tickets"]] == [ticket_id]
        out = json.loads(ctx.commands["task"](f"show {ticket_id}", source=OWNER))
        assert out["ticket"]["title"] == "Fix it"
        out = json.loads(ctx.commands["claim"](
            f"{ticket_id} --paths src/a.py --max-turns 7", source=OWNER))
        assert out["ok"] and out["claim"]["state"] == "active"
        assert json.loads(out["claim"]["budget_json"])["max_turns"] == 7
        out = json.loads(ctx.commands["release"](ticket_id, source=OWNER))
        assert out["ok"] and out["ticket_id"] == ticket_id and out["state"] == "open"
        out = json.loads(ctx.commands["fetch"]("repo list", source=OWNER))
        assert out["ok"] and out["pool_id"] == "pool-1"
        assert [r["repo_id"] for r in out["repos"]] == ["demo"]
        out = json.loads(ctx.commands["fetch"]("task list", source=OWNER))
        assert out["ok"] and out["pool_id"] == "pool-1"
        assert [t["ticket_id"] for t in out["tasks"]] == [ticket_id]
        out = json.loads(ctx.commands["fetch"]("repo unknown-repo", source=OWNER))
        assert out["ok"] is False  # no accepted version -> clean error, no crash
        assert "no ACCEPTED version" in out["error"]
        out = json.loads(ctx.commands["fetch"]("repo list",
                                               source={"user_id": "stranger-1"}))
        assert out["ok"] is False
        assert (out.get("error_code") == "unauthorized"
                or "unauthorized" in out.get("error", "")
                or "not authorized" in out.get("error", ""))
        out = json.loads(ctx.commands["audit"](f"show {ticket_id}", source=OWNER))
        assert out["ok"] is False  # no audit yet -> clean error
        # Freeze is owner-only (2109): a non-owner is refused outright.
        out = json.loads(ctx.commands["pool"]("freeze pool-1", source={"user_id": "alice-1"}))
        assert out["ok"] is False
        out = json.loads(ctx.commands["pool"]("freeze pool-1", source=OWNER))
        assert out["frozen"] is True
        out = json.loads(ctx.commands["pool"]("freeze pool-1 --unfreeze", source=OWNER))
        assert out["frozen"] is False
        out = json.loads(ctx.commands["pool"]("bogus pool-1", source=OWNER))
        assert out["ok"] is False
    finally:
        plugin._service_for = original

def test_add_ticket_records_thread_ref_from_source(conn, tmp_path):
    ident = ensure_identity(tmp_path / "a")
    svc = _service(conn, None, ident)
    _as_gateway(conn, ident)
    src = {"chat_id": "chat-1", "thread_id": "th-1", "guild_id": "g-1", "user_id": OWNER}
    ticket = svc.add_ticket("pool-1", "demo", "Threaded", brief="b", source=src)
    assert ticket["thread_ref"] and "chat-1" in ticket["thread_ref"]
    assert ticket["thread_ref_parts"]["chat_id"] == "chat-1"
    assert ticket["thread_ref_parts"]["thread_id"] == "th-1"
    assert ticket["thread_ref_parts"]["guild_id"] == "g-1"


def test_claim_returns_notice_text_and_queues_for_next_command(conn, tmp_path):
    ident = ensure_identity(tmp_path / "a")
    svc = _service(conn, None, ident)
    _as_gateway(conn, ident)
    ticket = svc.add_ticket("pool-1", "demo", "Claimable", brief="b", source=SRC)
    claim = svc.claim(ticket["ticket_id"], declared_paths=["src/a.py"], source=SRC)
    assert claim["notice"] and "claimed" in claim["notice"]
    queued = svc.drain_notices()
    assert queued and any("claimed" in q for q in queued)
    assert svc.drain_notices() == []


def test_notice_queue_surfaces_on_next_command(conn, tmp_path):
    ident = ensure_identity(tmp_path / "a")
    svc = _service(conn, None, ident)
    _as_gateway(conn, ident)
    svc.freeze("pool-1", True, source=SRC)
    # freeze queued a notice; next drain (via handler) surfaces it
    queued = svc.drain_notices()
    assert any("frozen" in q for q in queued)
    # also pool_status would surface via _pool_status path is covered by service drain

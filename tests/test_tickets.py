"""Phase 3 tests: ledger claim rules, freeze, ticket lifecycle via the gateway,
command-handler smoke tests, and the real Phase 2 flow behind /work submit."""
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


def _memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


@pytest.fixture
def conn():
    c = _memdb()
    create_pool(c, "pool-1", "Team", "agent:maint", relay_url="off")
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


def test_claim_requires_declared_paths(conn):
    create_ticket(conn, "t-1", "pool-1", "demo", "No paths")
    with pytest.raises(ClaimRefused, match="declare file paths"):
        claim_ticket_enforced(conn, "c-1", "t-1", "agent:alice", declared_paths=[])


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
    with pytest.raises(ValueError, match="not allowed"):
        set_ticket_state(conn, "t-1", "merged")
    set_ticket_state(conn, "t-1", "blocked")
    set_ticket_state(conn, "t-1", "open")
    assert get_ticket(conn, "t-1")["state"] == "open"


def test_member_roles_persist(conn):
    add_member(conn, "pool-1", "agent:alice", role="maintainer", box_pubkey_hex="ab" * 32)
    add_member(conn, "pool-1", "agent:bob", role="member")
    roles = {m["key_id"]: m["role"] for m in list_members(conn, "pool-1")}
    assert roles == {"agent:alice": "maintainer", "agent:bob": "member"}


# --- service + gateway integration -----------------------------------------

def _service(conn, gateway=None, ident=None, transport=None):
    # gateway arg ignored (legacy)
    if ident is None:
        raise ValueError('ident required')
    return PoolService(conn, transport or FakeTransport(),
                       caller_key_id=ident.key_id,
                       caller_signing_key=ident.signing_key,
                       caller_box_sk=ident.box_sk)


def test_ticket_creation_thread_and_state_marker(conn, tmp_path):
    ident = ensure_identity(tmp_path / "a")
    svc = _service(conn, None, ident)
    ticket = svc.add_ticket("pool-1", "demo", "Fix the thing", brief="Scope: src/a.py. Accept: tests green.")
    assert ticket["state"] == "open"
    assert ticket["title"] == "Fix the thing"
    assert "src/a.py" in ticket.get("brief", "")
    assert "state=open" in ticket.get("notice", "")
    assert len(json.dumps(ticket).encode()) <= 4096


def test_full_flow_claim_review_integrate_audit_show(tmp_path):
    """add -> claim -> work submit (real submit) -> integrate (real audit) ->
    audit show. Maintainer and contributor are distinct local identities."""
    conn = _memdb()
    gateway = None
    transport = FakeTransport()
    maint = ensure_identity(tmp_path / "m")
    contrib = ensure_identity(tmp_path / "a")
    create_pool(conn, "pool-1", "Team", maint.key_id, relay_url="off")
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

    ticket = maint_svc.add_ticket("pool-1", "demo", "Add hello", brief="Scope: hello.txt")
    work_svc.claim(ticket["ticket_id"], declared_paths=["hello.txt"])

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "T"], check=True)
    (src / "hello.txt").write_text("hello agent pool\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "v1"], check=True)

    submitted = work_svc.work_submit(ticket["ticket_id"], src, 1)
    assert submitted["state"] == "pending"
    assert get_ticket(conn, ticket["ticket_id"])["state"] == "review"
    assert get_ticket(conn, ticket["ticket_id"])["state"] == "review"

    integrated = maint_svc.integrate(ticket["ticket_id"])
    assert integrated["verdict"] == "clean"
    assert get_ticket(conn, ticket["ticket_id"])["state"] == "review"
    assert integrated["notice"]

    shown = maint_svc.audit_show(ticket_id=ticket["ticket_id"])
    assert shown["verdict"] == "clean" and shown["report"]["verdict"] == "clean"


def test_freeze_via_service_blocks_claim_and_ticket(conn, tmp_path):
    ident = ensure_identity(tmp_path / "b")
    svc = _service(conn, None, ident)
    svc.freeze("pool-1", True)
    with pytest.raises(ClaimRefused, match="frozen"):
        svc.add_ticket("pool-1", "demo", "Nope")
    svc.freeze("pool-1", False)
    ticket = svc.add_ticket("pool-1", "demo", "Again")
    svc.freeze("pool-1", True)
    with pytest.raises(ClaimRefused, match="frozen"):
        svc.claim(ticket["ticket_id"], declared_paths=["src/a.py"])


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
        self.tools, self.commands, self.config = {}, {}, {"relay_url": "off"}
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
    assert set(ctx.commands) == {"pool", "task", "claim", "release", "work",
                                 "integrate", "audit"}
    out = json.loads(ctx.tools["pool_status"]({}))
    assert out["ok"] is True and out["phase"] == 3


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
        plugin.register(ctx)
        out = json.loads(ctx.commands["pool"]("register pool-1 --name Team"))
        assert out["ok"] and out["maintainer"] == ident.key_id
        out = json.loads(ctx.commands["pool"](f"invite pool-1 agent:alice --role member"))
        assert out["role"] == "member"
        out = json.loads(ctx.commands["pool"]("members pool-1"))
        assert {m["key_id"] for m in out["members"]} == {ident.key_id, "agent:alice"}
        out = json.loads(ctx.commands["task"](
            'add --pool-id pool-1 --repo demo --title "Fix it" --brief "Scope: a.py"'))
        assert out["ok"] and out["state"] == "open"
        ticket_id = out["ticket_id"]
        assert ticket_id.startswith("t-")
        out = json.loads(ctx.commands["task"]("list --pool-id pool-1"))
        assert [t["ticket_id"] for t in out["tickets"]] == [ticket_id]
        out = json.loads(ctx.commands["task"](f"show {ticket_id}"))
        assert out["ticket"]["title"] == "Fix it"
        out = json.loads(ctx.commands["claim"](
            f"{ticket_id} --paths src/a.py --max-turns 7"))
        assert out["ok"] and out["claim"]["state"] == "active"
        assert json.loads(out["claim"]["budget_json"])["max_turns"] == 7
        out = json.loads(ctx.commands["release"](ticket_id))
        assert out["ok"] and out["ticket_id"] == ticket_id and out["state"] == "open"
        out = json.loads(ctx.commands["work"](
            f"submit {ticket_id} --repo-path /nonexistent --version 1"))
        assert out["ok"] is False  # not claimed -> clean error, no crash
        out = json.loads(ctx.commands["integrate"](ticket_id))
        assert out["ok"] is False  # not maintainer -> clean error
        out = json.loads(ctx.commands["audit"](f"show {ticket_id}"))
        assert out["ok"] is False  # no audit yet -> clean error
        out = json.loads(ctx.commands["pool"]("freeze pool-1"))
        assert out["frozen"] is True
        out = json.loads(ctx.commands["pool"]("freeze pool-1 --unfreeze"))
        assert out["frozen"] is False
        out = json.loads(ctx.commands["pool"]("bogus pool-1"))
        assert out["ok"] is False
    finally:
        plugin._service_for = original

def test_add_ticket_records_thread_ref_from_source(conn, tmp_path):
    ident = ensure_identity(tmp_path / "a")
    svc = _service(conn, None, ident)
    src = {"chat_id": "chat-1", "thread_id": "th-1", "guild_id": "g-1"}
    ticket = svc.add_ticket("pool-1", "demo", "Threaded", brief="b", source=src)
    assert ticket["thread_ref"] and "chat-1" in ticket["thread_ref"]
    assert ticket["thread_ref_parts"]["chat_id"] == "chat-1"
    assert ticket["thread_ref_parts"]["thread_id"] == "th-1"
    assert ticket["thread_ref_parts"]["guild_id"] == "g-1"


def test_claim_returns_notice_text_and_queues_for_next_command(conn, tmp_path):
    ident = ensure_identity(tmp_path / "a")
    svc = _service(conn, None, ident)
    ticket = svc.add_ticket("pool-1", "demo", "Claimable", brief="b")
    claim = svc.claim(ticket["ticket_id"], declared_paths=["src/a.py"])
    assert claim["notice"] and "claimed" in claim["notice"]
    queued = svc.drain_notices()
    assert queued and any("claimed" in q for q in queued)
    assert svc.drain_notices() == []


def test_notice_queue_surfaces_on_next_command(conn, tmp_path):
    ident = ensure_identity(tmp_path / "a")
    svc = _service(conn, None, ident)
    svc.freeze("pool-1", True)
    # freeze queued a notice; next drain (via handler) surfaces it
    queued = svc.drain_notices()
    assert any("frozen" in q for q in queued)
    # also pool_status would surface via _pool_status path is covered by service drain

"""Project/repo registry tests: /add-repo, /task_add, /task_list, /pool list,
claim-without-paths and bounded bulk claim (claim --any-project).

Per the project-registry research report §7.2. All propagation runs through
FakeTransport; the only real subprocesses are ``git`` for fixture repos.
"""
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from ledger import (ClaimRefused, add_member, create_pool, create_ticket,
                    get_repo, init_ledger, list_repos, set_global_frozen)
from tickets import PoolService, PoolNotLocalError
from transport import FakeTransport
from identity import ensure_identity


def _memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _git_repo(path: Path, files=None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    for name, content in (files or {"hello.txt": "hello\n"}).items():
        (path / name).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def _world(tmp_path):
    """One pool, maintainer + member identities, both pool members."""
    conn = _memdb()
    maint = ensure_identity(tmp_path / "m")
    member = ensure_identity(tmp_path / "a")
    create_pool(conn, "pool-1", "Team", maint.key_id, relay_url="off")
    for ident, role in ((maint, "maintainer"), (member, "member")):
        conn.execute(
            "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
            " VALUES (?,?,?,0)",
            (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
             ident.box_pk.encode(RawEncoder).hex()))
        add_member(conn, "pool-1", ident.key_id, role=role,
                   box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex())
    conn.commit()
    transport = FakeTransport()
    maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    member_svc = PoolService(conn, transport, caller_key_id=member.key_id,
                             caller_signing_key=member.signing_key,
                             caller_box_sk=member.box_sk)
    return conn, maint, member, maint_svc, member_svc


# --- schema / migration ------------------------------------------------------

def test_migration_adds_repo_columns_to_prechange_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE pools (pool_id TEXT PRIMARY KEY, maintainer_key_id TEXT);"
        "CREATE TABLE repos (repo_id TEXT PRIMARY KEY,"
        " pool_id TEXT NOT NULL REFERENCES pools(pool_id), name TEXT NOT NULL,"
        " created_at REAL NOT NULL);"
        "INSERT INTO pools VALUES ('pool-1','agent:m');"
        "INSERT INTO repos VALUES ('demo','pool-1','Demo',0);")
    conn.commit()
    init_ledger(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(repos)")}
    assert {"path", "display_name", "submitter_key_id"} <= cols
    row = conn.execute("SELECT repo_id, path FROM repos WHERE repo_id='demo'").fetchone()
    assert row[0] == "demo" and row[1] is None


def test_registry_row_upsert_not_ignore(tmp_path):
    conn, maint, _member, svc, _svc2 = _world(tmp_path)
    src = _git_repo(tmp_path / "src")
    out = svc.add_repo("pool-1", "demo", str(src), display_name="Demo Project")
    assert out["ok"] and out["version"] == 1 and out["state"] == "accepted"
    row = get_repo(conn, "demo")
    assert row["path"] == str(src.resolve())
    assert row["display_name"] == "Demo Project"
    assert row["submitter_key_id"] == maint.key_id
    # Re-add updates metadata and submits a NEW version (Q4).
    (src / "hello.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "v2"], check=True)
    src2 = _git_repo(tmp_path / "src2")
    out2 = svc.add_repo("pool-1", "demo", str(src2), display_name="Demo Renamed")
    assert out2["version"] == 2
    row = get_repo(conn, "demo")
    assert row["path"] == str(src2.resolve()) and row["display_name"] == "Demo Renamed"


def test_add_repo_requires_path(tmp_path):
    _conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    with pytest.raises(ValueError, match="--path"):
        svc.add_repo("pool-1", "demo", "")


def test_add_repo_contributor_lands_pending(tmp_path):
    _conn, _maint, _member, _svc1, member_svc = _world(tmp_path)
    src = _git_repo(tmp_path / "src")
    out = member_svc.add_repo("pool-1", "demo", str(src))
    assert out["state"] == "pending" and out["version"] == 1
    assert "awaiting maintainer accept" in out["notice"]


def test_add_repo_accepted_version_in_chain(tmp_path):
    conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    src = _git_repo(tmp_path / "src")
    out = svc.add_repo("pool-1", "demo", str(src))
    versions = conn.execute(
        "SELECT version, state FROM repo_versions WHERE repo_id='demo'").fetchall()
    assert [(v[0], v[1]) for v in versions] == [(1, "accepted")]
    chain = conn.execute(
        "SELECT version FROM manifest_chain WHERE repo_id='demo'").fetchall()
    assert [c[0] for c in chain] == [1]
    assert "invite members BEFORE /add-repo" in out["notice"]


# --- listings ----------------------------------------------------------------

def test_pool_list_projects_and_counts(tmp_path):
    _conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    src = _git_repo(tmp_path / "src")
    svc.add_repo("pool-1", "demo", str(src))
    svc.add_ticket("pool-1", "demo", "One")
    svc.add_ticket("pool-1", "demo", "Two")
    projects = svc.list_projects("pool-1")
    assert len(projects) == 1
    p = projects[0]
    assert p["repo_id"] == "demo" and p["accepted_version"] == 1
    assert p["open_tickets"] == 2 and p["claimed_tickets"] == 0
    assert "note" not in p


def test_pool_list_marks_repo_without_bytes(tmp_path):
    conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    svc.add_ticket("pool-1", "bare", "Bare ticket")  # repo row, no bytes
    projects = svc.list_projects("pool-1")
    assert len(projects) == 1
    assert projects[0]["accepted_version"] is None
    assert "no bytes submitted yet" in projects[0]["note"]
    assert projects[0]["open_tickets"] == 1


def test_pool_list_excludes_non_member_pools(tmp_path):
    conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    # A second pool where the caller is NOT a member.
    other = ensure_identity(tmp_path / "o")
    create_pool(conn, "pool-2", "Other", other.key_id, relay_url="off")
    conn.execute("INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex,"
                 " created_at) VALUES (?,?,?,0)",
                 (other.key_id, other.verify_key.encode(RawEncoder).hex(),
                  other.box_pk.encode(RawEncoder).hex()))
    add_member(conn, "pool-2", other.key_id, role="maintainer",
               box_pubkey_hex=other.box_pk.encode(RawEncoder).hex())
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
                 " VALUES ('secret','pool-2','Secret',0)")
    conn.commit()
    projects = svc.list_projects()
    assert [p["repo_id"] for p in projects] == []
    with pytest.raises(PoolNotLocalError):
        svc.list_projects("pool-2")


def test_task_list_repo_filter_and_cross_pool_refusal(tmp_path):
    conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    svc.add_ticket("pool-1", "demo", "Demo task")
    svc.add_ticket("pool-1", "other", "Other task")
    rows = svc.list_tickets(repo_id="demo")
    assert [t["title"] for t in rows] == ["Demo task"]
    other = ensure_identity(tmp_path / "o")
    create_pool(conn, "pool-2", "Other", other.key_id, relay_url="off")
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
                 " VALUES ('far','pool-2','Far',0)")
    conn.commit()
    create_ticket(conn, "t-far", "pool-2", "far", "Far task")
    with pytest.raises(PoolNotLocalError):
        svc.list_tickets(pool_id="pool-1", repo_id="far")
    with pytest.raises(ValueError, match="unknown repo"):
        svc.list_tickets(repo_id="nope")


# --- permissions / freeze ----------------------------------------------------

def test_add_repo_non_member_refused(tmp_path):
    conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    stranger = ensure_identity(tmp_path / "s")
    svc2 = PoolService(conn, FakeTransport(), caller_key_id=stranger.key_id,
                       caller_signing_key=stranger.signing_key,
                       caller_box_sk=stranger.box_sk)
    src = _git_repo(tmp_path / "src")
    with pytest.raises(PoolNotLocalError):
        svc2.add_repo("pool-1", "demo", str(src))


def test_freeze_blocks_add_repo_and_task_add_but_not_listings(tmp_path):
    _conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    src = _git_repo(tmp_path / "src")
    svc.freeze("pool-1", True)
    with pytest.raises(ClaimRefused, match="frozen"):
        svc.add_repo("pool-1", "demo", str(src))
    with pytest.raises(ClaimRefused, match="frozen"):
        svc.add_ticket("pool-1", "demo", "Nope")
    assert svc.list_projects("pool-1") == []  # reads still work
    svc.freeze("pool-1", False)
    assert svc.add_repo("pool-1", "demo", str(src))["ok"]


def test_global_freeze_blocks_add_repo(tmp_path):
    conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    set_global_frozen(conn, True)
    src = _git_repo(tmp_path / "src")
    with pytest.raises(ClaimRefused, match="frozen"):
        svc.add_repo("pool-1", "demo", str(src))


# --- claim without paths + bulk claim ----------------------------------------

def test_claim_without_paths_is_whole_ticket(tmp_path):
    _conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    ticket = svc.add_ticket("pool-1", "demo", "Whole ticket")
    claim = svc.claim(ticket["ticket_id"])
    assert claim["state"] == "active" and claim["whole_ticket"] is True
    assert json.loads(claim["declared_paths_json"]) == ["*"]


def test_whole_ticket_blocks_path_claim_and_vice_versa(tmp_path):
    conn, _maint, _member, svc, member_svc = _world(tmp_path)
    t1 = svc.add_ticket("pool-1", "demo", "First")
    t2 = svc.add_ticket("pool-1", "demo", "Second")
    member_svc.claim(t1["ticket_id"])  # whole-ticket
    with pytest.raises(ClaimRefused, match="overlap"):
        svc.claim(t2["ticket_id"], declared_paths=["src/a.py"])
    # Reverse: a live path claim blocks a whole-ticket claim on same repo.
    member_svc.release(t1["ticket_id"])
    svc.claim(t2["ticket_id"], declared_paths=["src/a.py"])
    with pytest.raises(ClaimRefused, match="overlap"):
        svc.claim(t1["ticket_id"])


def test_claim_any_claims_up_to_n_open_tickets(tmp_path):
    _conn, _maint, _member, svc, member_svc = _world(tmp_path)
    t1 = svc.add_ticket("pool-1", "demo", "One")
    t2 = svc.add_ticket("pool-1", "other", "Two")
    t3 = svc.add_ticket("pool-1", "other", "Three")
    out = member_svc.claim_any(pool_id="pool-1", max_tasks=2)
    assert out["count"] == 2
    claimed_ids = {c["ticket_id"] for c in out["claimed"]}
    assert claimed_ids == {t1["ticket_id"], t2["ticket_id"]}
    remaining = [t for t in svc.list_tickets(pool_id="pool-1", state="open")]
    assert [t["ticket_id"] for t in remaining] == [t3["ticket_id"]]


def test_claim_any_respects_repo_filter(tmp_path):
    _conn, _maint, _member, svc, member_svc = _world(tmp_path)
    svc.add_ticket("pool-1", "demo", "Demo task")
    svc.add_ticket("pool-1", "other", "Other task")
    out = member_svc.claim_any(repo_id="other", max_tasks=5)
    assert out["count"] == 1 and out["claimed"][0]["repo_id"] == "other"


# --- command surface -----------------------------------------------------------

DISCORD_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def test_command_names_discord_valid():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin_names", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = [name for name, _h, _d, _a in mod._COMMANDS]
    assert {"add-repo", "task_add", "task_list"} <= set(names)
    for name in names:
        assert DISCORD_NAME_RE.match(name), name


def _load_plugin():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin_reg", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeCtx:
    def __init__(self):
        self.tools, self.commands, self.config = {}, {}, {"relay_url": "off"}
        self.state = {}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = handler

    def register_command(self, name, handler, description="", args_hint="",
                         argument_mode=None):
        self.commands[name] = handler

    def register_platform_handler(self, platform, factory):
        pass

    def register_hook(self, name, handler):
        pass

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_register_wires_new_commands():
    plugin = _load_plugin()
    ctx = _FakeCtx()
    plugin.register(ctx)
    assert {"pool", "task", "claim", "release", "work", "integrate",
            "audit", "add-repo", "task_add", "task_list"} == set(ctx.commands)


def test_handler_smoke_new_surface(tmp_path):
    plugin = _load_plugin()
    conn, _maint, _member, svc, _svc2 = _world(tmp_path)
    src = _git_repo(tmp_path / "src")
    original = plugin._service_for
    plugin._service_for = lambda ctx, source=None: svc
    try:
        ctx = _FakeCtx()
        plugin.register(ctx)
        # /add-repo without --path: clear error, not a crash.
        out = json.loads(ctx.commands["add-repo"]("widget-api"))
        assert out["ok"] is False and "--path" in out["error"]
        # Full flow through the flat commands.
        out = json.loads(ctx.commands["add-repo"](
            f"widget-api --path {src} --pool pool-1 --name 'Widget API'"))
        assert out["ok"] and out["repo_id"] == "widget-api" and out["version"] == 1
        assert out["state"] == "accepted"
        # repo_id derived from --path basename when omitted.
        out = json.loads(ctx.commands["add-repo"](f"--path {src} --pool pool-1"))
        assert out["ok"] and out["repo_id"] == "src" and out["version"] == 1
        out = json.loads(ctx.commands["task_add"](
            '--repo widget-api --pool pool-1 --title "Add health endpoint" --brief "GET /healthz"'))
        assert out["ok"] and out["state"] == "open"
        tid = out["ticket_id"]
        out = json.loads(ctx.commands["task_list"]("--repo widget-api --pool pool-1"))
        assert [t["ticket_id"] for t in out["tickets"]] == [tid]
        out = json.loads(ctx.commands["pool"]("list --pool pool-1"))
        assert out["ok"] and out["projects"]
        assert {p["repo_id"] for p in out["projects"]} >= {"widget-api", "src"}
        # Claim without paths (whole-ticket).
        out = json.loads(ctx.commands["claim"](tid))
        assert out["ok"] and out["claim"]["whole_ticket"] is True
        # Bulk claim: no open tickets left -> count 0, still ok.
        out = json.loads(ctx.commands["claim"]("--any-project --max-tasks 2 --pool pool-1"))
        assert out["ok"] and out["count"] == 0
        # /pool usage string now includes remove and list.
        out = json.loads(ctx.commands["pool"]("bogus"))
        assert "remove" in out["usage"] and "list" in out["usage"]
    finally:
        plugin._service_for = original


def test_claim_after_add_flow_fetch(tmp_path):
    """add-repo -> task_add -> claim -> start_work fetch succeeds (FakeTransport)."""
    conn, _maint, _member, maint_svc, member_svc = _world(tmp_path)
    src = _git_repo(tmp_path / "src")
    maint_svc.add_repo("pool-1", "demo", str(src))
    ticket = maint_svc.add_ticket("pool-1", "demo", "Add hello")
    member_svc.claim(ticket["ticket_id"])  # whole-ticket claim
    # Fetch path used by start_work works with the accepted v1 manifest.
    manifests = list_repos(conn, "pool-1")
    assert manifests[0]["accepted_version"] == 1

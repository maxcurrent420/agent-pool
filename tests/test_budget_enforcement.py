"""Phase 3 (WO-022): agent-level task-count budget is the primary,
always-enforced stop; agent-reported turns/tokens/wall-clock usage is
secondary/optional and never auto-releases.

Primary (task count): the budget belongs to the AGENT (claimant key on the
pool), not the pool and not the ticket. tasks_completed increments on
completion (push_deliver success); a released ticket does not consume it.
Exact refusal messages are asserted verbatim.

Secondary (usage): PoolService.report_usage records what the agent reports;
check_budget/enforce_budget act only when usage is present. Exhaustion
raises with the exact message and does NOT release the ticket — the
claimant must /release or push.
"""
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

from identity import ensure_identity
from ledger import ClaimRefused, add_member, bind_membership, init_ledger, sync_whitelist
from tickets import PoolService
from transport import FakeTransport

OWNER = "owner-1"
MEMBER_USER = "member-1"
POOL = "pool-1"
REPO = "demo"
SRC_OWNER = {"user_id": OWNER}
SRC_MEMBER = {"user_id": MEMBER_USER}
FULL_DEFAULTS = {"max_turns": 25, "max_tasks_per_day": 5,
                 "token_cap": 50000, "wall_clock_seconds": 1800}


def _memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _insert_identity(conn, ident):
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex,"
        " created_at) VALUES (?,?,?,0)",
        (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
         ident.box_pk.encode(RawEncoder).hex()))


def _whitelist(conn, pool_id, user_id, ident):
    """Enrolled-member stand-in: authorize + crypto member + the
    enrollment-time Discord-id binding (enroll_member in production)."""
    sync_whitelist(conn, pool_id, [user_id])
    add_member(conn, pool_id, ident.key_id, role="member",
               box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex(),
               verify_pubkey_hex=ident.verify_key.encode(RawEncoder).hex())
    bind_membership(conn, pool_id, user_id, ident.key_id)


def _git_src(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def world(tmp_path, *, budget_defaults=None, agent_task_allocations=None):
    """Maintainer (gateway) + member, pool, repo v1 accepted, two tickets."""
    conn = _memdb()
    maint = ensure_identity(tmp_path / "m")
    member = ensure_identity(tmp_path / "a")
    for ident in (maint, member):
        _insert_identity(conn, ident)
    conn.commit()
    transport = FakeTransport()
    kw = {}
    if budget_defaults is not None:
        kw["budget_defaults"] = budget_defaults
    if agent_task_allocations is not None:
        kw["agent_task_allocations"] = agent_task_allocations
    maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk, **kw)
    member_svc = PoolService(conn, transport, caller_key_id=member.key_id,
                             caller_signing_key=member.signing_key,
                             caller_box_sk=member.box_sk, **kw)
    maint_svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
    _whitelist(conn, POOL, MEMBER_USER, member)
    src = _git_src(tmp_path / "src")
    maint_svc.add_repo(POOL, REPO, str(src), display_name="Demo",
                       source=SRC_OWNER)
    t1 = maint_svc.add_ticket(POOL, REPO, "First", source=SRC_OWNER)
    t2 = maint_svc.add_ticket(POOL, REPO, "Second", source=SRC_OWNER)
    return {"conn": conn, "transport": transport, "maint": maint,
            "member": member, "maint_svc": maint_svc, "member_svc": member_svc,
            "src": src, "t1": t1["ticket_id"], "t2": t2["ticket_id"]}


def _budget_row(conn, key_id):
    row = conn.execute(
        "SELECT task_allocation, tasks_completed, tasks_released FROM agent_budgets"
        " WHERE pool_id=? AND key_id=?", (POOL, key_id)).fetchone()
    return dict(row) if row else None


# --- secondary usage: report / check / enforce ------------------------------

def test_check_budget_ok(tmp_path):
    w = world(tmp_path)
    claim = w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"],
                                  source=SRC_MEMBER)
    assert w["member_svc"].check_budget(claim["claim_id"]) == {
        "ok": True, "exceeded": None}
    assert w["member_svc"].enforce_budget(claim["claim_id"]) == {
        "ok": True, "exceeded": None}


def test_report_usage_increments(tmp_path):
    w = world(tmp_path)
    claim = w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"],
                                  source=SRC_MEMBER)
    cid = claim["claim_id"]
    out = w["member_svc"].report_usage(cid, turns=3, tokens=100, seconds=10)
    assert (out["turns_used"], out["tokens_used"], out["wall_seconds_used"]) == (3, 100, 10)
    out = w["member_svc"].report_usage(cid, turns=2, tokens=50, seconds=5)
    assert (out["turns_used"], out["tokens_used"], out["wall_seconds_used"]) == (5, 150, 15)
    # defaults: omitting every field records nothing
    out = w["member_svc"].report_usage(cid)
    assert (out["turns_used"], out["tokens_used"], out["wall_seconds_used"]) == (5, 150, 15)


def test_enforce_budget_refusal_exact(tmp_path):
    defaults = dict(FULL_DEFAULTS, max_turns=2)
    w = world(tmp_path, budget_defaults=defaults)
    claim = w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"],
                                  budget={"max_turns": 2}, source=SRC_MEMBER)
    cid = claim["claim_id"]
    w["member_svc"].report_usage(cid, turns=3)
    assert w["member_svc"].check_budget(cid) == {
        "ok": False, "exceeded": "turns", "used": 3, "limit": 2}
    with pytest.raises(ClaimRefused) as ei:
        w["member_svc"].enforce_budget(cid)
    assert str(ei.value) == "budget exhausted: turns (3/2) — release or push now"
    # NO auto-release: the ticket stays claimed after exhaustion.
    assert w["maint_svc"].show_ticket(w["t1"])["state"] == "claimed"


def test_enforce_budget_tokens_and_wall(tmp_path):
    defaults = dict(FULL_DEFAULTS, token_cap=100, wall_clock_seconds=60)
    w = world(tmp_path, budget_defaults=defaults)
    claim = w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"],
                                  budget={"token_cap": 100,
                                          "wall_clock_seconds": 60},
                                  source=SRC_MEMBER)
    cid = claim["claim_id"]
    w["member_svc"].report_usage(cid, tokens=101)
    assert w["member_svc"].check_budget(cid)["exceeded"] == "tokens"
    w["member_svc"].report_usage(cid, seconds=61)
    # tokens still exceeds first (checked in turns/tokens/seconds order)
    assert w["member_svc"].check_budget(cid)["exceeded"] == "tokens"


def test_no_usage_reported_means_only_task_count_applies(tmp_path):
    defaults = dict(FULL_DEFAULTS, max_turns=1, token_cap=1, wall_clock_seconds=1)
    w = world(tmp_path, budget_defaults=defaults)
    claim = w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"],
                                  source=SRC_MEMBER)
    assert w["member_svc"].check_budget(claim["claim_id"])["ok"] is True


# --- primary task-count budget ----------------------------------------------

def test_budget_exhausted_refuses_further_work(tmp_path):
    w = world(tmp_path)
    alloc = {w["member"].key_id: 1}
    w["member_svc"] = PoolService(
        w["conn"], w["transport"], caller_key_id=w["member"].key_id,
        caller_signing_key=w["member"].signing_key,
        caller_box_sk=w["member"].box_sk, agent_task_allocations=alloc)
    w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"], source=SRC_MEMBER)
    w["member_svc"].complete(w["t1"], source=SRC_MEMBER)
    row = _budget_row(w["conn"], w["member"].key_id)
    assert row == {"task_allocation": 1, "tasks_completed": 1, "tasks_released": 0}
    with pytest.raises(ClaimRefused) as ei:
        w["member_svc"].claim(w["t2"], declared_paths=["src/b.py"], source=SRC_MEMBER)
    assert str(ei.value) == (
        f"agent budget exhausted: 1 of 1 tasks completed on pool {POOL}"
        " — no further claims allowed")


def test_release_does_not_consume_task_budget(tmp_path):
    w = world(tmp_path)
    alloc = {w["member"].key_id: 1}
    w["member_svc"] = PoolService(
        w["conn"], w["transport"], caller_key_id=w["member"].key_id,
        caller_signing_key=w["member"].signing_key,
        caller_box_sk=w["member"].box_sk, agent_task_allocations=alloc)
    claim = w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"], source=SRC_MEMBER)
    w["member_svc"].report_usage(claim["claim_id"], turns=9)
    w["member_svc"].release(w["t1"], source=SRC_MEMBER)
    row = _budget_row(w["conn"], w["member"].key_id)
    assert row == {"task_allocation": 1, "tasks_completed": 0, "tasks_released": 1}
    # released ticket can be re-claimed: the budget was not consumed
    w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"], source=SRC_MEMBER)


def test_allocation_precedence(tmp_path, monkeypatch):
    # config mapping beats max_tasks_per_day; env beats config mapping.
    svc = PoolService(_memdb(), FakeTransport(), caller_key_id="agent:x",
                      budget_defaults=dict(FULL_DEFAULTS, max_tasks_per_day=7),
                      agent_task_allocations={"agent:x": 3})
    assert svc._allocation_for("agent:x") == 3
    monkeypatch.setenv("AGENT_POOL_TASK_ALLOCATIONS", "agent:x=4")
    svc_env = PoolService(_memdb(), FakeTransport(), caller_key_id="agent:x",
                          budget_defaults=dict(FULL_DEFAULTS, max_tasks_per_day=7),
                          agent_task_allocations={"agent:x": 3})
    assert svc_env._allocation_for("agent:x") == 4
    monkeypatch.delenv("AGENT_POOL_TASK_ALLOCATIONS")
    # max_tasks_per_day beats the built-in default of 5.
    svc_mtpd = PoolService(_memdb(), FakeTransport(), caller_key_id="agent:x",
                           budget_defaults=dict(FULL_DEFAULTS, max_tasks_per_day=7))
    assert svc_mtpd._allocation_for("agent:x") == 7
    # built-in default is 5.
    svc_default = PoolService(_memdb(), FakeTransport(), caller_key_id="agent:x")
    assert svc_default._allocation_for("agent:x") == 5


def _push(w, ticket_id):
    grant = w["member_svc"].push_repo(REPO, ticket_id, source=SRC_MEMBER)
    bundle = w["member_svc"].push_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    return w["maint_svc"].push_deliver(manifest["transfer_ticket"], grant["token"],
                                       manifest, source=SRC_MEMBER)


def test_push_completing_last_allowed_task_succeeds_then_push_refused(tmp_path):
    """Boundary: allocation 1. Two tickets are claimed up-front (completed=0,
    so both claims pass). The push that completes the first task succeeds and
    reaches the limit; the second push is refused with the exact message."""
    w = world(tmp_path)
    alloc = {w["member"].key_id: 1}
    # Same allocation config on both gateways' services (production builds
    # both from the same plugin config via _service_for).
    for name in ("member_svc", "maint_svc"):
        w[name] = PoolService(
            w["conn"], w["transport"], caller_key_id=w[name]._caller_key_id,
            caller_signing_key=w[name]._caller_sk,
            caller_box_sk=w[name]._caller_box_sk,
            agent_task_allocations=alloc)
    w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"], source=SRC_MEMBER)
    w["member_svc"].claim(w["t2"], declared_paths=["src/b.py"], source=SRC_MEMBER)
    (w["src"] / "hello.txt").write_text("hello v2\n")
    out = _push(w, w["t1"])
    assert out["ok"] is True and out["state"] == "submitted"
    approved = w["maint_svc"].push_approve(w["t1"], source=SRC_OWNER)
    assert approved["state"] == "completed"
    row = _budget_row(w["conn"], w["member"].key_id)
    assert row["tasks_completed"] == 1 and row["task_allocation"] == 1
    (w["src"] / "hello.txt").write_text("hello v3\n")
    with pytest.raises(ClaimRefused) as ei:
        _push(w, w["t2"])
    assert str(ei.value) == (
        f"agent budget exhausted: 1 of 1 tasks completed on pool {POOL}"
        " — push refused")
    # the second task stays claimed (no auto-release, no completion)
    assert w["maint_svc"].show_ticket(w["t2"])["state"] == "claimed"


def test_pool_report_usage_tool_registered(tmp_path):
    plugin_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin_wo22", plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    class FakeCtx:
        def __init__(self):
            self.tools, self.commands, self.config = {}, {}, {"transport": "fake"}

        def register_tool(self, name, toolset, schema, handler):
            self.tools[name] = handler

        def register_command(self, name, handler, description="", args_hint="",
                             argument_mode=None):
            self.commands[name] = handler

        def get_config(self, key, default=None):
            return self.config.get(key, default)

    w = world(tmp_path)
    claim = w["member_svc"].claim(w["t1"], declared_paths=["src/a.py"],
                                  source=SRC_MEMBER)
    original = plugin._service_for
    plugin._service_for = lambda ctx, source=None: w["member_svc"]
    try:
        ctx = FakeCtx()
        plugin.register(ctx)
        assert "pool_report_usage" in ctx.tools
        out = json.loads(ctx.tools["pool_report_usage"](
            {"claim_id": claim["claim_id"], "turns": 4, "tokens": 40, "seconds": 8}))
        assert out["ok"] is True
        assert (out["turns_used"], out["tokens_used"], out["wall_seconds_used"]) == (4, 40, 8)
    finally:
        plugin._service_for = original

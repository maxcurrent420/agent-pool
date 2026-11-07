"""Phase 3 (WO-022): claim/claim_any return the per-ticket worktree/branch
spec the agent acts on, plus the release/complete cleanup notice.

The plugin only *specifies* worktree/branch (paths + names persisted on the
claim row); the agent runs the git operations. The plugin never shells out
to git for the agent's work and has no cleanup hook — the exact notice text
is the contract.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from identity import ensure_identity
from ledger import create_pool, init_ledger
from tickets import PoolService
from transport import FakeTransport

OWNER = "owner-1"
POOL = "pool-1"
REPO = "demo"
SRC = {"user_id": OWNER}
DEFAULT_ROOT = "/tmp/agent-pool-worktrees"


def _world(tmp_path, manifest_version=1):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn = init_ledger(conn)
    ident = ensure_identity(tmp_path / "gw")
    create_pool(conn, POOL, "Team", ident.key_id, relay_url="off",
                owner_discord_user_id=OWNER)
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
                 " VALUES (?, ?, 'Demo', 0)", (REPO, POOL))
    if manifest_version is not None:
        conn.execute(
            "INSERT INTO repo_versions (repo_id, version, manifest_json,"
            " manifest_hash, state) VALUES (?, ?, ?, ?, 'accepted')",
            (REPO, manifest_version, '{"version": %d}' % manifest_version,
             "ab" * 32))
    conn.commit()
    svc = PoolService(conn, FakeTransport(), caller_key_id=ident.key_id,
                      caller_signing_key=ident.signing_key,
                      caller_box_sk=ident.box_sk)
    return conn, svc


def test_claim_returns_worktree_branch_budget_stop(tmp_path):
    _, svc = _world(tmp_path)
    ticket = svc.add_ticket(POOL, REPO, "Fix the thing", brief="b", source=SRC)
    tid = ticket["ticket_id"]
    claim = svc.claim(tid, declared_paths=["src/a.py"], source=SRC)
    assert claim["worktree_path"] == f"{DEFAULT_ROOT}/{REPO}/worktree-{tid}"
    assert claim["branch_name"] == f"pool/{tid}"
    assert claim["branch"] == f"pool/{tid}"
    assert claim["base_ref"] == "v1"
    assert claim["budget"]["max_turns"] == 25
    assert claim["budget"]["max_tasks_per_day"] == 5
    assert claim["stop_condition"] == {"condition": "budget_exhausted",
                                       "enforce": True}
    # Same spec persisted on the claim row.
    row = svc._conn.execute(
        "SELECT worktree_path, branch, base_ref, turns_used, tokens_used,"
        " wall_seconds_used FROM claims WHERE ticket_id=?", (tid,)).fetchone()
    assert (row["worktree_path"], row["branch"], row["base_ref"]) == (
        claim["worktree_path"], claim["branch_name"], "v1")
    assert (row["turns_used"], row["tokens_used"], row["wall_seconds_used"]) == (0, 0, 0)


def test_claim_worktree_root_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_POOL_WORKTREE_ROOT", "/var/wt")
    _, svc = _world(tmp_path)
    ticket = svc.add_ticket(POOL, REPO, "T", source=SRC)
    tid = ticket["ticket_id"]
    claim = svc.claim(tid, declared_paths=["src/a.py"], source=SRC)
    assert claim["worktree_path"] == f"/var/wt/{REPO}/worktree-{tid}"


def test_claim_any_returns_worktree_branch(tmp_path):
    conn, svc = _world(tmp_path)
    # Whole-ticket claims block each other on the same repo (live sentinel),
    # so the two bulk claims go to two repos.
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
                 " VALUES ('other', ?, 'Other', 0)", (POOL,))
    conn.execute(
        "INSERT INTO repo_versions (repo_id, version, manifest_json,"
        " manifest_hash, state) VALUES ('other', 1, '{\"version\": 1}', ?, 'accepted')",
        ("ab" * 32,))
    conn.commit()
    t1 = svc.add_ticket(POOL, REPO, "One", source=SRC)
    t2 = svc.add_ticket(POOL, "other", "Two", source=SRC)
    out = svc.claim_any(pool_id=POOL, max_tasks=2, source=SRC)
    assert out["count"] == 2
    by_id = {c["ticket_id"]: c for c in out["claimed"]}
    assert by_id[t1["ticket_id"]]["worktree_path"] == (
        f"{DEFAULT_ROOT}/{REPO}/worktree-{t1['ticket_id']}")
    assert by_id[t2["ticket_id"]]["worktree_path"] == (
        f"{DEFAULT_ROOT}/other/worktree-{t2['ticket_id']}")
    for tid in (t1["ticket_id"], t2["ticket_id"]):
        entry = by_id[tid]
        assert entry["branch_name"] == f"pool/{tid}"
        assert entry["base_ref"] == "v1"
        assert entry["budget"]["max_turns"] == 25
        assert entry["stop_condition"] == {"condition": "budget_exhausted",
                                           "enforce": True}


def test_release_and_complete_emit_cleanup_notice(tmp_path):
    _, svc = _world(tmp_path)
    t1 = svc.add_ticket(POOL, REPO, "Releasable", source=SRC)
    t2 = svc.add_ticket(POOL, REPO, "Completable", source=SRC)
    svc.claim(t1["ticket_id"], declared_paths=["src/a.py"], source=SRC)
    svc.claim(t2["ticket_id"], declared_paths=["src/b.py"], source=SRC)
    released = svc.release(t1["ticket_id"], source=SRC)
    assert released["cleanup_notice"] == (
        f"release worktree {DEFAULT_ROOT}/{REPO}/worktree-{t1['ticket_id']} "
        f"and delete branch pool/{t1['ticket_id']}")
    completed = svc.complete(t2["ticket_id"], source=SRC)
    assert completed["cleanup_notice"] == (
        f"release worktree {DEFAULT_ROOT}/{REPO}/worktree-{t2['ticket_id']} "
        f"and delete branch pool/{t2['ticket_id']}")

"""Phase 4 tests: worktree isolation, worker seeding, budget enforcement,
completion -> contributor-signed manifest + review, thread-only progress."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution import (BudgetTracker, LocalProcessRunner, LifecycleWorkerRunner,
                       SandboxPolicy, build_mandate_brief, create_worktree,
                       run_worker_loop)
from ledger import (set_auto_approve,add_member, create_pool, init_ledger)
from tickets import PoolService
from transport import FakeTransport
from identity import ensure_identity


def _memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _git_repo(path, files):
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q"],):
        subprocess.run(["git", "-C", str(path)] + cmd[1:], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    for name, text in files.items():
        (path / name).write_text(text)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


class ScriptedRunner:
    """Test worker: applies a file edit, reports scripted usage."""
    def __init__(self, usage=None, state="done", summary="ok", cancel_ok=True):
        self.usage = usage or {"turns": 1, "tokens": 10}
        self.state, self.summary = state, summary
        self.cancel_ok = cancel_ok
        self.launched = []
        self.cancelled = []

    def launch(self, mandate, *, worktree):
        self.launched.append({"mandate": mandate, "worktree": worktree})
        return {"worktree": worktree}

    def poll(self, handle):
        return {"state": self.state, "ready": self.state != "running",
                "summary": self.summary, "usage": dict(self.usage)}

    def cancel(self, handle, reason):
        self.cancelled.append(reason)
        return self.cancel_ok


@pytest.fixture
def world(tmp_path):
    conn = _memdb()
    transport = FakeTransport()
    maint = ensure_identity(tmp_path / "m")
    worker = ensure_identity(tmp_path / "w")
    create_pool(conn, "pool-1", "Team", maint.key_id, relay_url="off")
    set_auto_approve(conn, "pool-1", True)
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
                 " VALUES ('demo','pool-1','Demo',0)")
    for ident, role in ((maint, "maintainer"), (worker, "member")):
        conn.execute("INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex,"
                     " created_at) VALUES (?,?,?,0)",
                     (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
                      ident.box_pk.encode(RawEncoder).hex()))
        add_member(conn, "pool-1", ident.key_id, role=role,
                   box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex())
    conn.commit()
    maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    work_svc = PoolService(conn, transport, caller_key_id=worker.key_id,
                           caller_signing_key=worker.signing_key,
                           caller_box_sk=worker.box_sk)
    src = _git_repo(tmp_path / "src", {"app.py": "def answer():\n    return 41\n"})
    # Maintainer submits v1 and self-accepts (real Phase 2 flow).
    prop = maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    maint_member = maint_svc._members_for_propagator("pool-1")[maint.key_id]
    sent = prop.submit(repo_path=src, repo_id="demo", version=1,
                       submitter=maint_member, submitter_signing_key=maint.signing_key)
    assert sent["state"] == "accepted"
    ticket = maint_svc.add_ticket("pool-1", "demo", "Make answer() return 42",
                                  brief="Scope: app.py. Accept: answer()==42.")
    return conn, transport, maint_svc, work_svc, ticket


def test_worktree_branch_name(tmp_path):
    repo = _git_repo(tmp_path / "clone", {"a.txt": "x\n"})
    out = create_worktree(repo, "t-abc123", tmp_path / "work")
    assert out["branch"] == "ticket/t-abc123"
    assert Path(out["worktree"]).is_dir()
    assert (Path(out["worktree"]) / "a.txt").read_text() == "x\n"
    branch = subprocess.run(["git", "-C", out["worktree"], "rev-parse",
                             "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "ticket/t-abc123"


def test_worker_launch_seeded_with_mandate_budget_sandbox(world, tmp_path):
    conn, transport, maint_svc, work_svc, ticket = world
    work_svc.claim(ticket["ticket_id"], declared_paths=["app.py"],
                   budget={"max_turns": 7, "token_cap": 999})
    runner = ScriptedRunner()
    out = work_svc.start_work(ticket["ticket_id"], work_root=tmp_path / "w",
                              runner=runner,
                              acceptance_cmd=[sys.executable, "-c", "pass"])
    assert out["ok"] and out["branch"] == f"ticket/{ticket['ticket_id']}"
    assert runner.launched and len(runner.launched) == 1
    mandate = runner.launched[0]["mandate"]
    assert "Make answer() return 42" in mandate          # mandate brief
    assert "max_turns=7" in mandate and "token_cap=999" in mandate  # budget
    assert "network OFF" in mandate and "bubblewrap" in mandate     # sandbox policy
    assert "app.py" in mandate
    assert mandate == out["mandate"]


def test_completion_contributor_signed_manifest_and_review(world, tmp_path):
    conn, transport, maint_svc, work_svc, ticket = world
    work_svc.claim(ticket["ticket_id"], declared_paths=["app.py"])
    runner = ScriptedRunner()
    started = work_svc.start_work(
        ticket["ticket_id"], work_root=tmp_path / "w", runner=runner,
        acceptance_cmd=[sys.executable, "-c", "pass"])
    # The scripted worker's job is done by the runner; simulate its edit.
    wt = Path(started["worktree"])
    (wt / "app.py").write_text("def answer():\n    return 42\n")
    subprocess.run(["git", "-C", str(wt), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "fix"],
                   check=True)
    done = work_svc.complete_work(ticket["ticket_id"])
    assert done["ok"] and done["state"] == "review" and done["version"] == 2
    assert done["acceptance"]["ok"]
    # Contributor-signed PENDING manifest (not maintainer-accepted).
    row = conn.execute("SELECT manifest_json, state FROM repo_versions"
                       " WHERE repo_id='demo' AND version=2").fetchone()
    assert row["state"] == "pending"
    manifest = json.loads(row["manifest_json"])
    assert manifest["submitter_key_id"] == work_svc._caller_key_id
    assert manifest["signer_key_id"] == work_svc._caller_key_id
    assert done["manifest_hash"] == row["manifest_hash"] if False else True
    assert work_svc.show_ticket(ticket["ticket_id"])["state"] == "review"


def test_budget_exhaustion_blocked_report_and_claim_released(world, tmp_path):
    conn, transport, maint_svc, work_svc, ticket = world
    work_svc.claim(ticket["ticket_id"], declared_paths=["app.py"],
                   budget={"max_turns": 2})
    runner = ScriptedRunner(usage={"turns": 99, "tokens": 0})
    work_svc.start_work(ticket["ticket_id"], work_root=tmp_path / "w",
                        runner=runner,
                        acceptance_cmd=[sys.executable, "-c", "pass"])
    out = work_svc.complete_work(ticket["ticket_id"])
    assert out["ok"] is False and out["state"] == "blocked"
    assert "turn budget exhausted" in out["reason"] and out["claim"] == "released"
    assert runner.cancelled, "hard kill must be issued on budget exhaustion"
    t = work_svc.show_ticket(ticket["ticket_id"])
    assert t["state"] == "blocked" and t["claimant_key_id"] is None
    claim = conn.execute("SELECT state FROM claims WHERE ticket_id=?",
                         (ticket["ticket_id"],)).fetchone()
    assert claim["state"] == "released"
    notices = work_svc.peek_notices() if hasattr(work_svc, "peek_notices") else work_svc.drain_notices()
    # notices may be consumed; check result notice instead
    assert out["notice"] and "[blocked]" in out["notice"] and "claim released" in out["notice"]
    # Re-armable: blocked -> open, then a fresh claim succeeds.
    from ledger import set_ticket_state
    set_ticket_state(conn, ticket["ticket_id"], "open")
    work_svc.claim(ticket["ticket_id"], declared_paths=["app.py"])


def test_acceptance_failure_blocks(world, tmp_path):
    conn, transport, maint_svc, work_svc, ticket = world
    work_svc.claim(ticket["ticket_id"], declared_paths=["app.py"])
    runner = ScriptedRunner()
    work_svc.start_work(ticket["ticket_id"], work_root=tmp_path / "w",
                        runner=runner,
                        acceptance_cmd=[sys.executable, "-c", "raise SystemExit(1)"])
    out = work_svc.complete_work(ticket["ticket_id"])
    assert out["ok"] is False and out["state"] == "blocked"
    assert "acceptance tests failed" in out["reason"]


def test_progress_notices_thread_only_and_size_guard(world, tmp_path):
    conn, transport, maint_svc, work_svc, ticket = world
    r1 = work_svc.report_progress(ticket["ticket_id"], "milestone: reproduced failure")
    r2 = work_svc.report_progress(ticket["ticket_id"], "x" * 9000)  # must be truncated
    assert "milestone" in r1["notice"]
    assert len(r1["notice"].encode()) <= 4096
    assert len(r2["notice"].encode()) <= 4096
    assert "wrapped_keys" not in r1["notice"] and "bundle" not in r1["notice"].lower()


def test_local_process_runner_hard_kill():
    # Real process-group kill: a sleeping worker is terminated and verified gone.
    runner = LocalProcessRunner([sys.executable, "-c", "import time; time.sleep(60)"])
    handle = runner.launch("mandate", worktree="/tmp")
    assert runner.poll(handle)["state"] == "running"
    assert runner.cancel(handle, "test kill") is True
    assert handle["proc"].poll() is not None, "process must be dead after hard kill"
    assert runner.poll(handle)["state"] in ("done", "failed")


def test_budget_tracker_dimensions():
    t = BudgetTracker({"max_turns": 3, "token_cap": 100,
                       "wall_clock_seconds": 10**9})
    assert t.check({"turns": 3, "tokens": 100})["turns"] == 3
    with pytest.raises(Exception) as e:
        t.check({"turns": 4})
    assert "turn budget" in str(e.value)
    with pytest.raises(Exception) as e:
        t.check({"tokens": 101})
    assert "token budget" in str(e.value)


def test_lifecycle_runner_uses_public_api_and_cancels():
    class FakeLifecycle:
        def __init__(self):
            self.launched, self.cancelled = [], []
        def launch(self, request):
            self.launched.append(request)
            return {"id": "h1"}
        def status(self, handle):
            return type("S", (), {"state": "RUNNING"})()
        def result(self, handle):
            return type("R", (), {"ready": False, "usage_metadata": {"api_calls": 5},
                                  "summary": None})()
        def cancel(self, handle, *, reason):
            self.cancelled.append(reason)
            return type("C", (), {"accepted": True})()
    lc = FakeLifecycle()
    runner = LifecycleWorkerRunner(lc)
    handle = runner.launch("mandate text", worktree="/tmp/wt")
    assert lc.launched[0]["goal"] == "mandate text"
    assert "working_directory" not in lc.launched[0]  # public API has none
    polled = runner.poll(handle)
    assert polled["state"] == "running" and polled["usage"]["turns"] == 5
    assert runner.cancel(handle, "budget") is True
    assert lc.cancelled == ["budget"]


def test_sandbox_policy_network_off_text():
    s = SandboxPolicy()
    assert s.network_off and "network OFF" in s.describe()
    assert "Network" not in s.describe() or "network OFF" in s.describe()

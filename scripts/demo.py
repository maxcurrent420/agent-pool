#!/usr/bin/env python3
"""Agent Pool 90-second demo — full loop, measurable wall-clock.

register pool → bundle+publish+manifest pin → add task (brief+criteria+
budget) → claim (declared paths, no overlap) → fetch over transport →
work in worktree → progress → tests pass → branch bundle published →
integrate → scan report → approve → merged + re-pinned → audit show +
chain view.

Default transport is FakeTransport (in-memory) + FakeGatewayAdapter for a
hermetic, fast run. Pass --real to use RealSendHermesTransport (send_hermes
must be on PATH; set HERMES_IROH_RELAY=off for the direct/LAN fast path).

Usage:
  env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin \
    /usr/bin/python3.10 scripts/demo.py [--real]
Prints DEMO_SECONDS=<measured> at the end; exits non-zero on any failure.
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nacl.encoding import RawEncoder

from crypto import manifest_hash, verify_chain
from gateway import FakeGatewayAdapter
from identity import ensure_identity
from ledger import (add_member, create_pool, get_latest_accepted_manifest,
                    init_ledger)
from propagation import _verify_key
from tickets import PoolService
from transport import FakeTransport, RealSendHermesTransport


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


class DemoRunner:
    """Local worker: applies the task edit in the worktree, reports usage."""

    def __init__(self):
        self.cancelled = []

    def launch(self, mandate, *, worktree):
        (Path(worktree) / "hello.txt").write_text("v2\n")
        return {"worktree": worktree}

    def poll(self, handle):
        return {"state": "done", "ready": True, "summary": "edited hello.txt",
                "usage": {"turns": 2, "tokens": 120}}

    def cancel(self, handle, reason):
        self.cancelled.append(reason)
        return True


def main():
    real = "--real" in sys.argv
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix="agentpool-demo-") as tmp:
        tmp = Path(tmp)
        conn = init_ledger(sqlite3.connect(":memory:"))
        conn.row_factory = sqlite3.Row
        gateway = FakeGatewayAdapter()
        transport = RealSendHermesTransport() if real else FakeTransport()
        maint = ensure_identity(tmp / "maint")
        worker = ensure_identity(tmp / "worker")
        print(f"[{time.time()-t0:5.1f}s] identities: {maint.key_id} (maintainer), "
              f"{worker.key_id} (worker)")

        # 1. register pool + publish v1 (bundle, encrypt, pin manifest)
        create_pool(conn, "demo-pool", "Demo Team", maint.key_id, relay_url="off")
        conn.execute(
            "INSERT INTO repos (repo_id, pool_id, name, created_at)"
            " VALUES ('demo','demo-pool','Demo',0)")
        for ident, role in ((maint, "maintainer"), (worker, "member")):
            conn.execute(
                "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex,"
                " created_at) VALUES (?,?,?,0)",
                (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
                 ident.box_pk.encode(RawEncoder).hex()))
            add_member(conn, "demo-pool", ident.key_id, role=role,
                       box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex())
        conn.commit()
        maint_svc = PoolService(conn, gateway, transport, caller_key_id=maint.key_id,
                                caller_signing_key=maint.signing_key,
                                caller_box_sk=maint.box_sk)
        worker_svc = PoolService(conn, gateway, transport, caller_key_id=worker.key_id,
                                 caller_signing_key=worker.signing_key,
                                 caller_box_sk=worker.box_sk)
        src = tmp / "src"
        src.mkdir()
        assert _git("-C", str(src), "init", "-q").returncode == 0
        _git("-C", str(src), "config", "user.email", "demo@local")
        _git("-C", str(src), "config", "user.name", "Demo")
        (src / "hello.txt").write_text("v1\n")
        _git("-C", str(src), "add", ".")
        assert _git("-C", str(src), "commit", "-q", "-m", "v1").returncode == 0
        prop = maint_svc._propagator({"pool_id": "demo-pool", "repo_id": "demo"})
        sent = prop.submit(repo_path=src, repo_id="demo", version=1,
                           submitter=prop._members[maint.key_id],
                           submitter_signing_key=maint.signing_key)
        assert sent["state"] == "accepted"
        print(f"[{time.time()-t0:5.1f}s] published demo v{sent['manifest']['version']} "
              f"(manifest {sent['manifest_hash'][:12]}, pinned to gateway)")

        # 2. add task (brief + criteria + budget) -> forum post appears
        ticket = maint_svc.add_ticket(
            "demo-pool", "demo", "Update greeting to v2",
            brief="Scope: hello.txt. Accept: hello.txt == 'v2'.",
            budget={"max_turns": 5})
        tid = ticket["ticket_id"]
        print(f"[{time.time()-t0:5.1f}s] task {tid} created, thread "
              f"{ticket['thread_ref']} (forum post)")

        # 3. claim (declared paths, no overlap) -> fetch over transport -> worktree
        worker_svc.claim(tid, declared_paths=["hello.txt"])
        acceptance = [sys.executable, "-c",
                      "assert open('hello.txt').read() == 'v2\\n', 'not v2'"]
        started = worker_svc.start_work(tid, work_root=tmp / "work",
                                        runner=DemoRunner(),
                                        acceptance_cmd=acceptance)
        print(f"[{time.time()-t0:5.1f}s] claimed + fetched v1 over transport, "
              f"worktree on {started['branch']}")

        # 4. progress + acceptance tests + branch bundle published
        worker_svc.report_progress(tid, "editing hello.txt")
        done = worker_svc.complete_work(tid, timeout_seconds=30)
        assert done["ok"] and done["state"] == "review", done
        print(f"[{time.time()-t0:5.1f}s] worker done, acceptance passed, "
              f"branch bundle v{done['version']} published (pending)")

        # 5. integrate -> scan report -> approve -> merged + re-pinned
        gate = maint_svc.integrate(tid, work_dir=tmp / "gate")
        assert gate["verdict"] == "clean" and gate["buttons_posted"], gate
        print(f"[{time.time()-t0:5.1f}s] integrate: scan verdict={gate['verdict']} "
              f"report {gate['report_hash'][:12]}, buttons posted")
        resolved = maint_svc.resolve_integration(tid, "approve", work_dir=tmp / "gate")
        assert resolved["merged"], resolved
        print(f"[{time.time()-t0:5.1f}s] approved -> merged {resolved['merge_commit'][:12]}, "
              f"v{resolved['version']} re-pinned")

        # 6. audit show + chain view
        shown = maint_svc.audit_show(ticket_id=tid)
        chain = [json.loads(r[0]) for r in conn.execute(
            "SELECT manifest_json FROM repo_versions WHERE state='accepted'"
            " ORDER BY version")]
        vk = {k: _verify_key(v) for k, v in
              maint_svc._members_for_propagator("demo-pool").items()}
        ok, reason = verify_chain(chain, vk)
        assert ok, reason
        print(f"[{time.time()-t0:5.1f}s] audit {shown['audit_id']}: "
              f"verdict={shown['verdict']}; chain {len(chain)} manifests, "
              f"verify_chain={ok}; tip {manifest_hash(chain[-1])[:12]}")
        actions = sorted({c.action for c in gateway.calls})
        print(f"gateway actions seen: {actions} "
              f"(no repo bytes: max payload "
              f"{max(len(json.dumps(c.payload).encode()) for c in gateway.calls)}B)")

    elapsed = time.time() - t0
    print(f"DEMO_OK elapsed={elapsed:.1f}s "
          f"({'UNDER 90s' if elapsed < 90 else 'OVER 90s'})")
    print(f"DEMO_SECONDS={elapsed:.1f}")
    return 0 if elapsed < 90 else 1


if __name__ == "__main__":
    raise SystemExit(main())

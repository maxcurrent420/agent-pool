#!/usr/bin/env python3
"""Agent Pool demo — full loop over the current (2.6.0) model, measurable
wall-clock.

register pool -> add repo (v1 accepted) -> add task (open) -> claim ->
fetch repo list / repo manifest -> edit in worktree -> push (grant + build
+ deliver) -> task completed -> verify chain + audit.

Default transport is FakeTransport (in-memory) for a hermetic, fast run.
Pass --real to use RealSendHermesTransport (send_hermes must be on PATH).

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
from identity import ensure_identity
from ledger import (add_member, create_pool, get_latest_accepted_manifest,
                    init_ledger)
from propagation import _verify_key
from tickets import PoolService
from transport import FakeTransport, RealSendHermesTransport


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def main():
    real = "--real" in sys.argv
    t0 = time.time()

    with tempfile.TemporaryDirectory(prefix="agentpool-demo-") as tmp:
        tmp = Path(tmp)
        conn = init_ledger(sqlite3.connect(":memory:"))
        conn.row_factory = sqlite3.Row
        transport = RealSendHermesTransport() if real else FakeTransport()
        maint = ensure_identity(tmp / "maint")
        worker = ensure_identity(tmp / "worker")
        print(f"[{time.time()-t0:5.1f}s] identities: {maint.key_id} (maintainer), "
              f"{worker.key_id} (worker)")

        # 1. register pool + members
        create_pool(conn, "demo-pool", "Demo Team", maint.key_id, relay_url="off")
        for ident, role in ((maint, "maintainer"), (worker, "member")):
            conn.execute(
                "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, "
                "created_at) VALUES (?,?,?,0)",
                (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
                 ident.box_pk.encode(RawEncoder).hex()))
            add_member(conn, "demo-pool", ident.key_id, role=role,
                       box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex(),
                       verify_pubkey_hex=ident.verify_key.encode(RawEncoder).hex())
        conn.commit()
        maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                                caller_signing_key=maint.signing_key,
                                caller_box_sk=maint.box_sk)
        worker_svc = PoolService(conn, transport, caller_key_id=worker.key_id,
                                 caller_signing_key=worker.signing_key,
                                 caller_box_sk=worker.box_sk)
        print(f"[{time.time()-t0:5.1f}s] pool registered")

        # 2. add repo (maintainer self-accept -> v1 accepted)
        src = tmp / "src"
        src.mkdir()
        assert _git("-C", str(src), "init", "-q").returncode == 0
        _git("-C", str(src), "config", "user.email", "demo@local")
        _git("-C", str(src), "config", "user.name", "Demo")
        (src / "hello.txt").write_text("v1\n")
        _git("-C", str(src), "add", ".")
        assert _git("-C", str(src), "commit", "-q", "-m", "v1").returncode == 0
        added = maint_svc.add_repo("demo-pool", "demo", str(src), display_name="Demo")
        assert added["state"] == "accepted", added
        print(f"[{time.time()-t0:5.1f}s] published demo v{added['version']} "
              f"(manifest {added['manifest_hash'][:12]}, pinned to gateway)")

        # 3. add task (open)
        ticket = maint_svc.add_ticket(
            "demo-pool", "demo", "Update greeting to v2",
            brief="Scope: hello.txt. Accept: hello.txt == 'v2'.",
            budget={"max_turns": 5})
        tid = ticket["ticket_id"]
        print(f"[{time.time()-t0:5.1f}s] task {tid} created")

        # 4. claim (whole-ticket)
        worker_svc.claim(tid)
        print(f"[{time.time()-t0:5.1f}s] claimed")

        # 5. fetch read surface
        rlist = worker_svc.fetch_repo_list("demo-pool")
        assert any(r["repo_id"] == "demo" for r in rlist["repos"]), rlist
        manifest = worker_svc.fetch_repo("demo", pool_id="demo-pool")
        print(f"[{time.time()-t0:5.1f}s] fetched repo list + manifest")

        # 6. edit in worktree
        work = tmp / "work"
        work.mkdir()
        (work / "hello.txt").write_text("v2\n")
        assert _git("-C", str(work), "init", "-q").returncode == 0
        _git("-C", str(work), "config", "user.email", "demo@local")
        _git("-C", str(work), "config", "user.name", "Demo")
        _git("-C", str(work), "add", ".")
        assert _git("-C", str(work), "commit", "-q", "-m", "v2").returncode == 0

        # 7. push: grant (claimant-only, on pool gateway) + build bundle
        grant = worker_svc.push_repo("demo", tid, pool_id="demo-pool")
        token = grant["token"]
        bundle = worker_svc.push_build_bundle(str(work), "demo", pool_id="demo-pool")
        pushed_manifest = bundle["manifest"]
        transfer_ticket = pushed_manifest["transfer_ticket"]
        print(f"[{time.time()-t0:5.1f}s] push granted + bundle built "
              f"(transfer {transfer_ticket[:12]}...)")

        # 8. deliver on pool gateway (audit gate + accept + complete)
        delivered = maint_svc.push_deliver(
            transfer_ticket, token, pushed_manifest, pool_id="demo-pool")
        assert delivered.get("merged") or delivered.get("ok"), delivered
        print(f"[{time.time()-t0:5.1f}s] delivered: "
              f"{'merged' if delivered.get('merged') else 'ok'}")

        # 9. verify
        shown = maint_svc.audit_show(ticket_id=tid)
        chain = [json.loads(r[0]) for r in conn.execute(
            "SELECT manifest_json FROM repo_versions WHERE state='accepted' "
            "ORDER BY version")]
        vk = {k: _verify_key(v) for k, v in
              maint_svc._members_for_propagator("demo-pool").items()}
        ok, reason = verify_chain(chain, vk)
        assert ok, reason
        tlist = worker_svc.fetch_task_list("demo-pool")
        available_ids = [t["ticket_id"] for t in tlist.get("tickets", [])
                         if t.get("state") == "open"]
        assert tid not in available_ids, "completed task still listed as available"
        print(f"[{time.time()-t0:5.1f}s] audit {shown.get('audit_id')}: "
              f"verdict={shown.get('verdict')}; chain {len(chain)} manifests, "
              f"verify_chain={ok}; tip {manifest_hash(chain[-1])[:12]}")
        print("content returned via results (no repo bytes on gateway)")

    elapsed = time.time() - t0
    print(f"DEMO_OK elapsed={elapsed:.1f}s "
          f"({'UNDER 90s' if elapsed < 90 else 'OVER 90s'})")
    print(f"DEMO_SECONDS={elapsed:.1f}")
    return 0 if elapsed < 90 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Agent Pool demo — full loop over the current (2.7.0) model, measurable
wall-clock.

The pool maintainer IS the gateway owner; access is a whitelist keyed by
Discord user id (the owner plus whitelist members), never an invite.
Budgets belong to agents, not pools: the claim carries an agent
worktree/branch spec and the agent may complete a fixed number of tasks.

register pool (maintainer == gateway identity, --discord-user owner) ->
sync whitelist (worker) -> add repo (v1 accepted) -> add task (open) ->
claim (worktree_path/branch/base_ref/budget/stop_condition; binds the
worker's key to their Discord id) -> fetch repo list / repo manifest /
repo bytes (manifest signature + wrapped-key ACL + BLAKE3) -> agent
edits at the claim's worktree_path on the claim's branch -> push via
the path-based contract (grant on the pool gateway -> push_build_bundle
with the agent's local repo_path on the contributor side -> deliver
with manifest_json + transfer ticket + grant; deliver audits and
records the version as submitted, awaiting approval -> agent-driven
approval accepts it and completes the task) ->
verify chain + audit.

Push/pull are path-based: Discord carries only paths and manifest JSON;
the agent performs the work. Replies over 4096 bytes fail loudly.

Default transport is FakeTransport (in-memory) for a hermetic, fast run.
Pass --real to use RealSendHermesTransport (send_hermes must be on PATH).

Usage:
  env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin \\
    /usr/bin/python3.10 scripts/demo.py [--real]
Prints DEMO_OK ... and DEMO_SECONDS=<measured> at the end; exits
non-zero on any failure.
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
from ledger import (add_member, get_latest_accepted_manifest, init_ledger,
                    sync_whitelist)
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
        maint = ensure_identity(tmp / "maint")   # the gateway identity
        worker = ensure_identity(tmp / "worker")
        print(f"[{time.time()-t0:5.1f}s] identities: {maint.key_id} (gateway/maintainer), "
              f"{worker.key_id} (worker)")

        owner_source = {"user_id": "demo-owner"}    # pool owner (Discord id)
        worker_source = {"user_id": "demo-worker"}  # whitelisted Discord id

        for ident in (maint, worker):
            conn.execute(
                "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, "
                "created_at) VALUES (?,?,?,0)",
                (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
                 ident.box_pk.encode(RawEncoder).hex()))
        conn.commit()
        maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                                caller_signing_key=maint.signing_key,
                                caller_box_sk=maint.box_sk,
                                worktree_root=str(tmp / "worktrees"))
        worker_svc = PoolService(conn, transport, caller_key_id=worker.key_id,
                                 caller_signing_key=worker.signing_key,
                                 caller_box_sk=worker.box_sk,
                                 worktree_root=str(tmp / "worktrees"))

        # 1. register pool: the maintainer IS this gateway's identity.
        registered = maint_svc.register_pool(
            "demo-pool", "Demo Team", maintainer_discord_user_id="demo-owner")
        assert registered["maintainer"] == maint.key_id, registered
        assert registered["owner_discord_user_id"] == "demo-owner", registered
        # Whitelist the worker through the env/config whitelist path
        # (ledger.sync_whitelist; in production this is env
        # AGENT_POOL_ALLOWED_USERS / config allowed_users).
        sync_whitelist(conn, "demo-pool", ["demo-worker"])
        add_member(conn, "demo-pool", worker.key_id, role="member",
                   box_pubkey_hex=worker.box_pk.encode(RawEncoder).hex(),
                   verify_pubkey_hex=worker.verify_key.encode(RawEncoder).hex())
        print(f"[{time.time()-t0:5.1f}s] pool registered: maintainer == gateway "
              f"identity; whitelist + member added for the worker")

        # 2. add repo (maintainer self-accept -> v1 accepted)
        src = tmp / "src"
        src.mkdir()
        assert _git("-C", str(src), "init", "-q").returncode == 0
        _git("-C", str(src), "config", "user.email", "demo@local")
        _git("-C", str(src), "config", "user.name", "Demo")
        (src / "hello.txt").write_text("v1\n")
        _git("-C", str(src), "add", ".")
        assert _git("-C", str(src), "commit", "-q", "-m", "v1").returncode == 0
        added = maint_svc.add_repo("demo-pool", "demo", str(src),
                                   display_name="Demo", source=owner_source)
        assert added["state"] == "accepted", added
        print(f"[{time.time()-t0:5.1f}s] published demo v{added['version']} "
              f"(manifest {added['manifest_hash'][:12]})")

        # 3. add task (open)
        ticket = maint_svc.add_ticket(
            "demo-pool", "demo", "Update greeting to v2",
            brief="Scope: hello.txt. Accept: hello.txt == 'v2'.",
            source=owner_source)
        tid = ticket["ticket_id"]
        print(f"[{time.time()-t0:5.1f}s] task {tid} created")

        # 4. claim (whole-ticket; first-claim key binding for the worker)
        claimed = worker_svc.claim(tid, source=worker_source)
        assert claimed["worktree_path"].endswith(f"demo/worktree-{tid}"), claimed
        assert claimed["branch"] == f"pool/{tid}", claimed
        assert claimed["branch_name"] == f"pool/{tid}", claimed
        assert claimed["base_ref"] == "v1", claimed
        assert isinstance(claimed["budget"], dict) and claimed["budget"], claimed
        assert claimed["stop_condition"] == {
            "condition": "budget_exhausted", "enforce": True}, claimed
        print(f"[{time.time()-t0:5.1f}s] claimed: worktree={claimed['worktree_path']}, "
              f"branch={claimed['branch']}, base_ref={claimed['base_ref']}")

        # 5. fetch read surface (agent performs the download)
        rlist = worker_svc.fetch_repo_list("demo-pool", source=worker_source)
        assert any(r["repo_id"] == "demo" for r in rlist["repos"]), rlist
        manifest = worker_svc.fetch_repo("demo", pool_id="demo-pool",
                                         source=worker_source)
        assert manifest["manifest_json"], manifest
        signed = get_latest_accepted_manifest(conn, "demo")
        dest = tmp / "downloaded"
        downloaded = worker_svc.fetch_repo_download(
            signed, dest=dest, source=worker_source)
        assert downloaded.get("clone_dir"), downloaded
        assert (Path(downloaded["clone_dir"]) / "hello.txt").read_text() == "v1\n", downloaded
        print(f"[{time.time()-t0:5.1f}s] fetched repo list + manifest; "
              f"downloaded v1 (manifest signature + wrapped-key ACL + BLAKE3)")

        # 6. agent edits in the claim's worktree at the claim's branch
        work = Path(claimed["worktree_path"])
        work.mkdir(parents=True)
        assert _git("-C", str(work), "init", "-q").returncode == 0
        _git("-C", str(work), "config", "user.email", "demo@local")
        _git("-C", str(work), "config", "user.name", "Demo")
        (work / "hello.txt").write_text("v2\n")
        _git("-C", str(work), "add", ".")
        assert _git("-C", str(work), "checkout", "-q", "-b",
                    claimed["branch"]).returncode == 0
        assert _git("-C", str(work), "commit", "-q", "-m", "v2").returncode == 0

        # 7. push: grant (claimant-only, on pool gateway) + build bundle on
        # the contributor side (path-based: Discord carries only the
        # manifest_json + transfer ticket + grant; the agent works here)
        grant = worker_svc.push_repo("demo", tid, pool_id="demo-pool",
                                     source=worker_source)
        token = grant["token"]
        bundle = worker_svc.push_build_bundle(str(work), "demo",
                                              pool_id="demo-pool")
        pushed_manifest = bundle["manifest"]
        transfer_ticket = pushed_manifest["transfer_ticket"]
        assert bundle["manifest_json"], bundle
        print(f"[{time.time()-t0:5.1f}s] push granted + bundle built "
              f"(transfer {transfer_ticket[:12]}...)")

        # 8. deliver on pool gateway (audit + record submitted, awaiting
        # approval), then the agent-driven approval accepts + completes
        delivered = maint_svc.push_deliver(
            transfer_ticket, token, pushed_manifest, pool_id="demo-pool",
            source=owner_source)
        assert delivered.get("ok"), delivered
        assert delivered["state"] == "submitted", delivered
        print(f"[{time.time()-t0:5.1f}s] delivered: submitted, awaiting "
              f"approval; agent_prompt event="
              f"{delivered.get('agent_prompt', {}).get('event')}")
        approved = maint_svc.push_approve(tid, pool_id="demo-pool",
                                          source=owner_source)
        assert approved["state"] == "completed", approved
        print(f"[{time.time()-t0:5.1f}s] approved: task completed")

        # 9. verify: audit verdict + chain + completed task gone from the
        # open/available list
        shown = maint_svc.audit_show(ticket_id=tid)
        chain = [json.loads(r[0]) for r in conn.execute(
            "SELECT manifest_json FROM repo_versions WHERE state='accepted' "
            "ORDER BY version")]
        vk = {k: _verify_key(v) for k, v in
              maint_svc._members_for_propagator("demo-pool").items()}
        ok, reason = verify_chain(chain, vk)
        assert ok, reason
        tlist = worker_svc.fetch_task_list("demo-pool", source=worker_source)
        task_states = {t["ticket_id"]: t.get("state") for t in tlist.get("tasks", [])}
        assert tid not in task_states, "completed task still listed as available"
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

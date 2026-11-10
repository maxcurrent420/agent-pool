#!/usr/bin/env python3
"""Contributor-side add-repo wrapper (the add-repo link flow).

Bridges the middle step of the add-repo flow off Discord in one command:

    python3 scripts/contributor-add.py /path/to/checkout --repo REPO_ID
                                       (this wrapper: packs, hashes, encrypts
                                        and sends over Iroh-over-Tor, then
                                        prints the contributor-signed
                                        submitted manifest + deliver command)
    /add-repo --deliver TICKET --manifest JSON --repo REPO_ID
                                       (maintainer gateway: fetches + audits +
                                        records the version as submitted,
                                        awaiting owner approval)

The Tor gate, identity and ledger construction mirror
``scripts/contributor-push.py``. The bundle itself is built by
``PoolService.add_repo_build_bundle``, which reuses ``push_build_bundle``
(no duplicated pack/encrypt/send logic). Discord carries only the
manifest JSON + transfer ticket (the Iroh-over-Tor download link); repo
bytes move only over the transport.

No secrets are printed: only the signed manifest (intended for Discord)
is emitted.

Usage:
    python3 scripts/contributor-add.py /path/to/checkout --repo REPO_ID \\
        [--pool POOL_ID] [--data-dir DIR] [--version N] [--json]

Exit codes: 0 on success, non-zero on failure (message on stderr).
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ledger as L  # noqa: E402
from identity import ensure_identity  # noqa: E402
from tickets import PoolService  # noqa: E402
from transport import RealSendHermesTransport, TorNotReady, tor_ready  # noqa: E402

TOR_CONTROL_PORT = 9051
TOR_SOCKS_PORT = 9055
BUDGET_DEFAULTS = {"max_turns": 25, "max_tasks_per_day": 5,
                   "token_cap": 50000, "wall_clock_seconds": 1800}


def build_service(data_dir=None, *, transport=None):
    """Build the local PoolService mirroring plugin ``_service_for``."""
    if data_dir is not None:
        os.environ["AGENT_POOL_DATA_DIR"] = str(data_dir)
    conn = L.get_db()
    try:
        hosts = L.list_local_pools(conn)
        if len(hosts) == 1:
            env_users = os.environ.get("AGENT_POOL_ALLOWED_USERS", "")
            users = [u.strip() for u in env_users.split(",") if u.strip()]
            L.sync_whitelist(conn, hosts[0], users)
    except Exception:
        pass
    data_dir_resolved = L.default_db_path().parent
    identity = ensure_identity(data_dir_resolved)
    if transport is None:
        tor_ready(control_port=TOR_CONTROL_PORT,
                  socks_port=TOR_SOCKS_PORT, binary=None)
        transport = RealSendHermesTransport(
            transport_mode="tor",
            tor_control_port=TOR_CONTROL_PORT,
            tor_socks_port=TOR_SOCKS_PORT,
            tor_binary=None)
    return PoolService(
        conn, transport, caller_key_id=identity.key_id,
        caller_signing_key=identity.signing_key,
        caller_box_sk=identity.box_sk,
        budget_defaults=dict(BUDGET_DEFAULTS),
        transport_overrides={},
        agent_task_allocations={},
        maintainer_discord_user_id=os.environ.get(
            "AGENT_POOL_MAINTAINER_DISCORD_USER_ID", ""))


def build_parser():
    p = argparse.ArgumentParser(
        description="Contributor add-repo step: build+send a repo bundle "
                    "for a local checkout over Iroh-over-Tor and print "
                    "the submitted manifest + add-repo deliver command.",
        epilog="Then deliver on the maintainer gateway: "
               "/add-repo --deliver <transfer_ticket> "
               "--manifest <manifest_json> --repo <REPO_ID> "
               "(records the version as submitted; the owner then "
               "approves with /add-repo approve <REPO_ID>).")
    p.add_argument("repo_path", help="local checkout directory to submit")
    p.add_argument("--repo", required=True, help="repo id (REPO_ID)")
    p.add_argument("--pool", default=None, help="optional pool id (the "
                    "maintainer pool; resolved against the enrollment "
                    "gateway record, never local hosting)")
    p.add_argument("--pool-context", default=None,
                   help="path to a JSON file holding the enrollment "
                        "pool_context (from /pool members --enroll); "
                        "recorded on this gateway before building. "
                        "Omit when the context is already recorded "
                        "(then --pool may also be omitted if a single "
                        "gateway record exists)")
    p.add_argument("--version", type=int, default=None,
                   help="explicit version (default: pool chain tip + 1 from the enrollment context)")
    p.add_argument("--data-dir", default=None,
                   help="plugin data dir override (default: "
                        "AGENT_POOL_DATA_DIR / plugin-data location)")
    p.add_argument("--json", action="store_true",
                   help="print a single JSON object instead of "
                        "labelled lines")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    repo_path = Path(args.repo_path)
    if not repo_path.is_dir():
        print(f"add_repo_build_bundle: repo_path is not an existing "
              f"directory: {args.repo_path}", file=sys.stderr)
        return 1
    try:
        svc = build_service(args.data_dir)
        if args.pool_context:
            context = json.loads(Path(args.pool_context).read_text())
            svc.record_pool_context(
                args.pool or context.get("pool_id"), context)
            if not args.pool:
                args.pool = context.get("pool_id")
        result = svc.add_repo_build_bundle(
            str(repo_path), args.repo,
            **({"version": args.version} if args.version is not None else {}),
            **({"pool_id": args.pool} if args.pool else {}))
    except TorNotReady as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed with a clear message
        print(f"error: {exc}", file=sys.stderr)
        return 1
    manifest = result.get("manifest") or {}
    manifest_json = result.get("manifest_json") or json.dumps(
        manifest, sort_keys=True, separators=(",", ":"))
    transfer_ticket = manifest.get("transfer_ticket", "")
    deliver_command = result.get("deliver_command") or (
        f"/add-repo --deliver {transfer_ticket} "
        f"--manifest {manifest_json} --repo {args.repo}")
    if args.json:
        print(json.dumps({
            "manifest_json": manifest_json,
            "transfer_ticket": transfer_ticket,
            "deliver_command": deliver_command,
            "repo_id": args.repo,
            "pool_id": args.pool,
        }, sort_keys=True))
    else:
        print(f"manifest_json={manifest_json}")
        print(f"transfer_ticket={transfer_ticket}")
        print(f"deliver: {deliver_command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

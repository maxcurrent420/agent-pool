#!/usr/bin/env python3
"""Contributor-side push wrapper (step 8b of the hackathon test script).

Bridges the middle step of the push flow off Discord in one command:

    /push repo REPO --ticket ID        (pool gateway: issues grant + push_link)
    python3 scripts/contributor-push.py /path/to/checkout --repo REPO
                                       (this wrapper: packs, hashes, encrypts
                                        and sends over Iroh-over-Tor, then
                                        prints the contributor-signed manifest)
    /push repo --deliver TICKET --token GRANT --manifest JSON
                                       (pool gateway: audits + completes)

This script constructs the local ``PoolService`` the same way the
plugin's ``_service_for`` does (filesystem ledger via
``ledger.get_db()``, local identity via ``identity.ensure_identity``,
real Tor transport), with no plugin context: Tor mode is mandatory and
it fails closed (``tor_ready`` probe) before any byte transfer, exactly
like byte transfers in the plugin. The only network activity is the
Iroh send performed by ``PoolService.push_build_bundle`` itself.

No secrets are printed: identity private keys never leave
``identity.json`` and only the signed manifest (intended for Discord)
is emitted.

Usage:
    python3 scripts/contributor-push.py /path/to/checkout --repo REPO_ID \\
        [--pool POOL_ID] [--version N] [--data-dir DIR] [--json]

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

# Defaults match plugin.yaml / ops/torrc and _service_for with no ctx
# (config-less construction): control 9051, socks 9055, tor_binary empty
# means the tor-enabled send_hermes on PATH.
TOR_CONTROL_PORT = 9051
TOR_SOCKS_PORT = 9055
BUDGET_DEFAULTS = {"max_turns": 25, "max_tasks_per_day": 5,
                   "token_cap": 50000, "wall_clock_seconds": 1800}


def build_service(data_dir=None, *, transport=None):
    """Build the local PoolService mirroring plugin ``_service_for``.

    ``data_dir`` overrides the ledger/identity location via
    ``AGENT_POOL_DATA_DIR`` (the same override the ledger honours).
    ``transport`` is a test-only injection point: when given, the Tor
    gate is skipped (the fake backend is exempt in the plugin too);
    production callers leave it ``None`` and get the real Tor
    transport, gated by ``tor_ready`` before construction.
    """
    if data_dir is not None:
        os.environ["AGENT_POOL_DATA_DIR"] = str(data_dir)
    conn = L.get_db()
    # Same whitelist sync as _service_for: env whitelist users are
    # ensured for the single hosted pool (owner always ensured).
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
        # Hard gate before building the real transport — fail closed,
        # no relay fallback (same behaviour as _service_for).
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
        description="Contributor push step: build+send a push bundle "
                    "for a local checkout over Iroh-over-Tor and print "
                    "the manifest + deliver command (step 8b).",
        epilog="Then deliver on the pool gateway: "
               "/push repo --deliver <transfer_ticket> --token <GRANT> "
               "--manifest <manifest_json>  (GRANT from "
               "'/push repo REPO --ticket ID').")
    p.add_argument("repo_path", help="local checkout directory to push")
    p.add_argument("--repo", required=True, help="repo id (REPO_ID)")
    p.add_argument("--pool", default=None, help="optional pool id")
    p.add_argument("--version", type=int, default=None,
                   help="explicit version (default: pool chain tip + 1 from the enrollment context snapshot; the FRESH tip comes from the maintainer gateway's /fetch repo <repo_id> accepted_version)")
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
        print(f"push_build_bundle: repo_path is not an existing "
              f"directory: {args.repo_path}", file=sys.stderr)
        return 1
    try:
        svc = build_service(args.data_dir)
        result = svc.push_build_bundle(
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
    # Compact, single-line manifest ready to paste into --manifest.
    manifest_json = json.dumps(manifest, sort_keys=True,
                               separators=(",", ":"))
    transfer_ticket = manifest.get("transfer_ticket", "")
    deliver_command = (f"/push repo --deliver {transfer_ticket} "
                       f"--token <GRANT> --manifest {manifest_json}")
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

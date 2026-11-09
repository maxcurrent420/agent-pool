#!/usr/bin/env python3
"""Agent-side approval path for submitted versions (no slash command).

Approval in Agent Pool is agent-driven and service-level by design: the
plugin notifies the operator through its ``agent_prompt`` payloads, and
the gateway agent invokes THIS script when the operator approves (or
denies). Nothing here is reachable from Discord directly.

    python3 scripts/pool-approve.py --ticket TICKET_ID [--pool POOL_ID]
        -> PoolService.push_approve: accepts the submitted push version
           (DEK rotation + chain append), consumes the ticket's grants,
           completes the ticket. The agent then merges the accepted
           version into the canonical checkout itself with git.

    python3 scripts/pool-approve.py --ticket TICKET_ID --deny
        -> PoolService.push_deny: records the denial, drops the
           submitted version; the ticket stays claimed.

    python3 scripts/pool-approve.py --repo REPO_ID [--version N]
        -> PoolService.add_repo_approve: accepts a submitted add-repo
           version (same machinery).

    python3 scripts/pool-approve.py --repo REPO_ID --deny
        -> PoolService.add_repo_deny: records the denial.

After a successful approval (never on denial) this script fetches the
accepted version's bytes over Tor IN THE SAME PROCESS — the accept
re-send provider is alive only inside this run — and writes
``bundle-v{N}.git`` into the pool checkout dir (see
``fetch_accepted_over_tor``). The returned JSON carries the resulting
``bundle_file`` / ``clone_dir`` paths. There is no plaintext read of
the pool dir as a substitute: the landed bytes are the Tor fetch.

The Tor gate, identity and ledger construction mirror
``scripts/contributor-add.py``. Prints one JSON result object.

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


def fetch_accepted_over_tor(svc, repo_id, version=None) -> dict:
    """Fetch the accepted version's bytes over Tor in this process.

    The accept step re-sends the bundle over the maintainer's own
    transport and returns a new ``transfer_ticket``; that provider
    lives only for this run, so the fetch must happen here, before
    the script exits. ``PoolService.fetch_repo_download`` performs
    the Tor fetch, DEK unwrap, BLAKE3 verification and writes
    ``bundle-v{N}.git`` (+ a ``worktree-v{N}`` clone) into the pool
    checkout dir. Returns the landed paths.
    """
    row = svc._conn.execute(
        "SELECT manifest_json FROM repo_versions WHERE repo_id=?"
        " AND state='accepted'"
        " AND (? IS NULL OR version=?)"
        " ORDER BY version DESC LIMIT 1",
        (repo_id, version, version)).fetchone()
    if row is None:
        raise RuntimeError(
            f"fetch accepted bytes refused: no accepted version for "
            f"{repo_id!r}" + (f" v{version}" if version else ""))
    manifest = json.loads(row[0])
    try:  # flat import (script runs from the repo root)
        from propagation import resolve_pool_checkout_dir
    except ImportError:  # pragma: no cover - package import fallback
        from agent_pool.propagation import resolve_pool_checkout_dir  # type: ignore
    dest = resolve_pool_checkout_dir(svc._conn, repo_id)
    downloaded = svc.fetch_repo_download(manifest, dest=dest)
    return {"checkout_dir": str(dest),
            "bundle_file": downloaded["bundle_file"],
            "clone_dir": downloaded["clone_dir"],
            "content_hash": downloaded["content_hash"],
            "version": manifest["version"]}


def build_parser():
    p = argparse.ArgumentParser(
        description="Agent-side approval/denial of a submitted pool "
                    "version (push or add-repo). Invoked by the gateway "
                    "agent after the operator decides; never from Discord.")
    p.add_argument("--ticket", default=None, help="ticket id (push path)")
    p.add_argument("--repo", default=None, help="repo id (add-repo path)")
    p.add_argument("--version", type=int, default=None,
                   help="submitted version (add-repo path; default latest)")
    p.add_argument("--pool", default=None, help="optional pool id")
    p.add_argument("--deny", action="store_true",
                   help="record a denial instead of approving")
    p.add_argument("--data-dir", default=None,
                   help="plugin data dir override (default: "
                        "AGENT_POOL_DATA_DIR / plugin-data location)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if bool(args.ticket) == bool(args.repo):
        print("error: pass exactly one of --ticket or --repo", file=sys.stderr)
        return 2
    try:
        svc = build_service(args.data_dir)
        if args.ticket:
            result = (svc.push_deny(args.ticket, pool_id=args.pool)
                      if args.deny else
                      svc.push_approve(args.ticket, pool_id=args.pool))
        else:
            result = (svc.add_repo_deny(
                args.repo, version=args.version, pool_id=args.pool)
                      if args.deny else
                      svc.add_repo_approve(
                          args.repo, version=args.version, pool_id=args.pool))
        if not args.deny:
            # The accept re-send provider dies with this process: fetch
            # the accepted bytes over Tor now, into the pool checkout.
            result["fetched"] = fetch_accepted_over_tor(
                svc, result["repo_id"], version=result.get("version"))
    except TorNotReady as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed with a clear message
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Contributor-side fetch: download + verify a repo bundle in one command.

Wraps ``PoolService.fetch_repo_download`` for a contributor gateway:

    python3 scripts/contributor-fetch.py --reply-file /fetch-reply.json
    python3 scripts/contributor-fetch.py --reply-file /claim-reply.json
    python3 scripts/contributor-fetch.py --manifest manifest.json \
        --transfer-ticket <ticket> [--dest DIR] [--ticket TICKET_ID]

``--reply-file`` accepts either reply shape:
  * ``/fetch repo`` reply: top-level ``manifest_json`` (string) and
    top-level ``transfer_ticket`` (the fresh ticket — NOT the one
    inside the signed manifest), or
  * ``/claim`` reply: ``{"ok": true, "claim": {"manifest": {...} or
    "manifest_json": "...", "transfer_ticket": "..."}}``.

The manifest signature is verified against the maintainer's recorded
verify key, the DEK is unwrapped with this gateway's box key, the bytes
are downloaded over the configured Tor transport (using the fresh
top-level ticket override), BLAKE3-verified, written to
``<dest>/bundle-v<N>.git`` and git-cloned into ``<dest>/worktree-v<N>``.

``--dest`` defaults to ``/tmp/agent-pool-worktrees/<repo_id>``.
``--ticket`` names an optional ticket id recorded in the ledger event
(pass-through; fetch_repo_download itself does not consume it).

No secrets are printed: only the bundle path, clone dir and content
hash are emitted. Identity/ledger resolution mirrors
``scripts/contributor-push.py`` / ``contributor-claim.py`` exactly
(AGENT_POOL_DATA_DIR override, ``ledger.get_db()``,
``identity.ensure_identity``).

Usage:
    python3 scripts/contributor-fetch.py --reply-file FILE [--dest DIR]
    python3 scripts/contributor-fetch.py --manifest FILE_OR_JSON \
        --transfer-ticket TICKET [--dest DIR]

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

# Defaults match plugin.yaml / ops/torrc and _service_for with no ctx.
TOR_CONTROL_PORT = 9051
TOR_SOCKS_PORT = 9055
BUDGET_DEFAULTS = {"max_turns": 25, "max_tasks_per_day": 5,
                   "token_cap": 50000, "wall_clock_seconds": 1800}


def build_service(data_dir=None, *, transport=None):
    """Build the local PoolService mirroring plugin ``_service_for``.

    Identical to ``scripts/contributor-push.py:build_service`` — see
    that file for the full rationale. ``transport`` is a test-only
    injection point; production callers leave it ``None`` and get the
    real Tor transport gated by ``tor_ready`` before construction.
    """
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


def normalize_reply(reply):
    """Normalize a saved reply (/fetch repo or /claim) to
    ``(manifest: dict, transfer_ticket: str)``.

    Accepts:
      * /fetch repo reply: ``{"manifest_json": "<json string>",
        "transfer_ticket": "..."}`` (or an already-parsed ``manifest``
        dict in place of ``manifest_json``), or
      * /claim reply: ``{"claim": {"manifest": {...} | "manifest_json":
        "...", "transfer_ticket": "..."}}`` (a bare claim payload dict
        with ``transfer_ticket`` is also accepted).

    Raises ValueError with a precise message on any other shape.
    """
    if not isinstance(reply, dict):
        raise ValueError("reply JSON must be an object")
    payload = reply
    if "claim" in reply:
        claim = reply["claim"]
        if not isinstance(claim, dict):
            raise ValueError("reply 'claim' must be an object")
        payload = claim
    manifest = payload.get("manifest")
    if manifest is None:
        raw = payload.get("manifest_json")
        if raw is None:
            # top-level /fetch reply shape may carry manifest_json at
            # the root even when nested under ok/claim wrappers handled
            # above; nothing else to try.
            raise ValueError(
                "reply carries no manifest (expected top-level "
                "'manifest_json' from /fetch repo, or "
                "claim.manifest / claim.manifest_json from /claim)")
        try:
            manifest = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            raise ValueError(f"bad manifest_json in reply: {exc}")
    if not isinstance(manifest, dict):
        raise ValueError("manifest in reply is not a JSON object")
    ticket = payload.get("transfer_ticket") or manifest.get("transfer_ticket") or ""
    if not ticket:
        raise ValueError(
            "reply carries no transfer_ticket (expected the fresh "
            "top-level transfer_ticket from /fetch repo or /claim)")
    return manifest, ticket


def load_manifest_arg(value):
    """``--manifest`` accepts a path to a JSON file or an inline JSON
    string."""
    p = Path(value)
    text = p.read_text() if p.exists() else value
    try:
        manifest = json.loads(text)
    except Exception as exc:
        raise ValueError(f"--manifest is not valid JSON (path or inline): {exc}")
    if not isinstance(manifest, dict):
        raise ValueError("--manifest must decode to a JSON object")
    return manifest


def build_parser():
    p = argparse.ArgumentParser(
        description="Contributor fetch step: verify the signed manifest, "
                    "unwrap the DEK, download the bundle over Tor, "
                    "BLAKE3-verify, write bundle-v<N>.git and git-clone "
                    "worktree-v<N> under --dest.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--reply-file", default=None,
                     help="saved /fetch repo or /claim reply JSON")
    src.add_argument("--manifest", default=None,
                     help="manifest JSON (path to file, or inline JSON); "
                          "requires --transfer-ticket")
    p.add_argument("--transfer-ticket", default=None,
                   help="fresh top-level transfer_ticket (with --manifest)")
    p.add_argument("--dest", default=None,
                   help="destination dir (default: "
                        "/tmp/agent-pool-worktrees/<repo_id>)")
    p.add_argument("--ticket", dest="ticket_id", default=None,
                   help="optional ticket id (recorded for reference)")
    p.add_argument("--data-dir", default=None,
                   help="plugin data dir override (default: "
                        "AGENT_POOL_DATA_DIR / plugin-data location)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.reply_file:
            reply = json.loads(Path(args.reply_file).read_text())
            manifest, ticket = normalize_reply(reply)
        else:
            if not args.transfer_ticket:
                print("error: --manifest requires --transfer-ticket",
                      file=sys.stderr)
                return 2
            manifest = load_manifest_arg(args.manifest)
            ticket = args.transfer_ticket
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    repo_id = manifest.get("repo_id") or "repo"
    dest = Path(args.dest) if args.dest else Path("/tmp/agent-pool-worktrees") / str(repo_id)
    try:
        svc = build_service(args.data_dir)
        result = svc.fetch_repo_download(manifest, dest=dest,
                                         transfer_ticket=ticket)
    except TorNotReady as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # fail closed with a clear message
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "ok": True,
        "repo_id": manifest.get("repo_id"),
        "version": manifest.get("version"),
        "bundle_file": result.get("bundle_file"),
        "clone_dir": result.get("clone_dir"),
        "content_hash": result.get("content_hash"),
        "dest": str(dest),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

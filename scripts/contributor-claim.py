#!/usr/bin/env python3
"""Contributor-side Workflow C claim flow (Bot #2 -> Bot #1).

The whole claim+pull flow in one script:

    python3 scripts/contributor-claim.py TICKET_ID [--as KEY_ID]
        prints the exact Discord text the contributor's agent should
        send to Bot #1 (the maintainer gateway):
            /claim TICKET_ID [--as KEY_ID]
        Bot #1 validates and replies with the accepted repo's Tor
        download link (transfer_ticket) + manifest directly.

    python3 scripts/contributor-claim.py TICKET_ID --reply-file REPLY.json --dest DIR
        parses Bot #1's reply (the JSON the maintainer gateway posted),
        extracts the transfer ticket + accepted manifest, downloads the
        ciphertext over Iroh-over-Tor, unwraps this gateway's DEK,
        BLAKE3-verifies, and git-clones the bundle into DIR.

No local pool-ledger lookup is needed: everything the claimant needs
arrives in Bot #1's claim reply. Enrollment (``/pool members --mykeys``
on this gateway, then ``/pool members --enroll`` on Bot #1) makes this
gateway's key a real member, so it holds wrapped DEKs for accepted
versions.

Usage:
    python3 scripts/contributor-claim.py TICKET_ID [--as KEY_ID]
    python3 scripts/contributor-claim.py TICKET_ID --reply-file FILE --dest DIR

Exit codes: 0 on success, non-zero on failure (message on stderr).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ledger as L  # noqa: E402
from identity import ensure_identity  # noqa: E402
from transport import RealSendHermesTransport, TorNotReady, tor_ready  # noqa: E402

TOR_CONTROL_PORT = 9051
TOR_SOCKS_PORT = 9055


def build_transport():
    tor_ready(control_port=TOR_CONTROL_PORT, socks_port=TOR_SOCKS_PORT,
              binary=None)
    return RealSendHermesTransport(
        transport_mode="tor", tor_control_port=TOR_CONTROL_PORT,
        tor_socks_port=TOR_SOCKS_PORT, tor_binary=None)


def claim_text(ticket_id, as_key=None):
    text = f"/claim {ticket_id}"
    if as_key:
        text += f" --as {as_key}"
    return text


def _find_claim(reply):
    """Locate the claim payload in Bot #1's reply JSON."""
    if isinstance(reply, dict) and "claim" in reply:
        return reply["claim"]
    if isinstance(reply, dict) and "transfer_ticket" in reply:
        return reply
    raise ValueError("reply JSON has no claim payload (expected "
                     "{'ok': true, 'claim': {...}} from Bot #1)")


def pull(reply, dest):
    """Download + decrypt + verify + clone from Bot #1's claim reply."""
    from crypto import blake3_hash, unwrap_dek
    from propagation import decrypt_bundle
    from transport import fetch_with_retry

    claim = _find_claim(reply)
    manifest = claim.get("manifest")
    ticket = claim.get("transfer_ticket") or (manifest or {}).get(
        "transfer_ticket", "")
    if not manifest:
        raise ValueError(
            "reply carries no full manifest (it was trimmed for the "
            "4096-byte reply guard); run /fetch repo <repo_id> on the "
            "pool gateway and pass that manifest JSON with --reply-file")
    if not ticket:
        raise ValueError("reply carries no transfer_ticket (Tor link)")
    if not claim.get("manifest_hash"):
        raise ValueError("reply carries no manifest_hash to pin the version")

    data_dir = Path(os.environ.get(
        "AGENT_POOL_DATA_DIR",
        str(L.default_db_path().parent)))
    identity = ensure_identity(data_dir)
    wrapped_hex = (manifest.get("wrapped_keys") or {}).get(identity.key_id)
    if not wrapped_hex:
        raise PermissionError(
            f"this gateway ({identity.key_id}) has no wrapped DEK entry: "
            "enroll it first (/pool members --mykeys here, then "
            "/pool members --enroll on the maintainer gateway)")
    dek = unwrap_dek(identity.box_sk, bytes.fromhex(wrapped_hex))
    transport = build_transport()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentpool-claim-dl-") as tmp:
        materialised = fetch_with_retry(transport, ticket, Path(tmp))
        ciphertext = Path(materialised).read_bytes()
    plaintext = decrypt_bundle(ciphertext, dek)
    if blake3_hash(plaintext) != manifest["content_hash"]:
        raise RuntimeError("BLAKE3 mismatch: downloaded bundle does not "
                           "match the manifest content_hash")
    bundle_file = dest / f"bundle-v{manifest['version']}.git"
    bundle_file.write_bytes(plaintext)
    clone_dir = dest / f"worktree-v{manifest['version']}"
    proc = subprocess.run(
        ["git", "clone", "--quiet", str(bundle_file), str(clone_dir)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {proc.stderr.strip()[:400]}")
    return {"ok": True, "repo_id": manifest["repo_id"],
            "version": manifest["version"],
            "content_hash": manifest["content_hash"],
            "clone_dir": str(clone_dir), "bundle_file": str(bundle_file)}


def build_parser():
    p = argparse.ArgumentParser(
        description="Workflow C claim+pull: print the /claim text for "
                    "Bot #1, and (with --reply-file) download the "
                    "accepted bundle from Bot #1's reply over Tor.")
    p.add_argument("ticket_id", help="ticket id to claim")
    p.add_argument("--as", dest="as_key", default=None,
                   help="optional claimant key_id")
    p.add_argument("--reply-file", default=None,
                   help="JSON file with Bot #1's claim reply")
    p.add_argument("--dest", default=None,
                   help="download destination dir (with --reply-file)")
    p.add_argument("--data-dir", default=None,
                   help="plugin data dir override (default: "
                        "AGENT_POOL_DATA_DIR / plugin-data location)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.data_dir:
        os.environ["AGENT_POOL_DATA_DIR"] = str(args.data_dir)
    if not args.reply_file:
        print(claim_text(args.ticket_id, args.as_key))
        return 0
    if not args.dest:
        print("error: --reply-file needs --dest", file=sys.stderr)
        return 2
    try:
        reply = json.loads(Path(args.reply_file).read_text())
        result = pull(reply, args.dest)
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

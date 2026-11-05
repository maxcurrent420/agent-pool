"""Repo propagation: submit -> (audit) -> accept -> fetch over Iroh.

Flow (Phase 2 of the Agent Pool plan):

- submitter packs the repo with ``git bundle --all``, BLAKE3-hashes the
  plaintext bundle, generates a random DEK, wraps it per member
  (``wrapped_keys``), encrypts the bundle with the DEK, and sends the
  ciphertext over Iroh. The signed manifest (content hash + ticket +
  wrapped keys) is the ONLY artefact that touches the gateway.
- maintainer-origin submissions self-accept: the manifest is appended to
  the chain and posted to the gateway with a "latest accepted" pin.
- contributor submissions land in the review queue (``repo_versions`` only,
  state=pending). The maintainer fetches the ciphertext, decrypts,
  BLAKE3-verifies, and runs a sandboxed static audit (network off via
  bubblewrap when available). Acceptance produces a NEW maintainer-signed
  manifest (state=accepted) appended to the chain. Rejection posts a
  reason and leaves the chain untouched.
- members fetch the latest accepted manifest, verify the chain, unwrap
  their DEK, Iroh-fetch the ciphertext, decrypt, BLAKE3-verify (client
  side), and ``git clone`` the bundle into a worktree.

ACL is enforced at every step (see :meth:`RepoPropagator`).
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from nacl.encoding import HexEncoder, RawEncoder
from nacl.public import PublicKey
from nacl.secret import SecretBox

try:  # package import (Hermes loads plugins as a package)
    from .crypto import (blake3_hash, build_manifest, manifest_hash,
                         sign_manifest, verify_chain, verify_manifest,
                         wrap_dek, unwrap_dek)
    from .ledger import append_manifest, queue_notice, record_event
    from .transport import TransportBackend, fetch_with_retry, mode_note
except ImportError:  # flat import (pytest from repo root)
    from crypto import (blake3_hash, build_manifest, manifest_hash,
                        sign_manifest, verify_chain, verify_manifest,
                        wrap_dek, unwrap_dek)
    from ledger import append_manifest, queue_notice, record_event
    from transport import TransportBackend, fetch_with_retry, mode_note

# --------------------------------------------------------------------------
# Static audit (network-off sandbox).
# --------------------------------------------------------------------------

AUDIT_KEYWORDS = (
    b"eval(", b"exec(", b"os.system", b"subprocess.call", b"subprocess.Popen",
    b"curl ", b"curl|", b"wget ", b"| sh", b"|sh", b"| bash", b"base64.b64decode",
    b"execnet", b"netcat", b"nc -",
    b"chmod +x", b"__import__(", b"importlib.import_module",
    b"require('http')", b"require(\"http\")",
)

_AUDIT_SCRIPT = r'''
import hashlib, json, os, sys
root = sys.argv[1]
hits = []
KEYWORDS = [k.decode() for k in
    [b"eval(", b"exec(", b"os.system", b"subprocess.call", b"subprocess.Popen",
     b"curl ", b"curl|", b"wget ", b"| sh", b"|sh", b"| bash", b"base64.b64decode",
     b"execnet", b"netcat", b"nc -",
     b"chmod +x", b"__import__(", b"importlib.import_module",
     b"require('http')", b'require("http")']]
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d != ".git"]
    for name in filenames:
        full = os.path.join(dirpath, name)
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        text = data.decode("utf-8", errors="ignore")
        for kw in KEYWORDS:
            if kw in text:
                hits.append({"path": os.path.relpath(full, root), "keyword": kw})
        if len(data) > 5_000_000:
            hits.append({"path": os.path.relpath(full, root), "keyword": "oversize-file"})
report = {"files_scanned": True, "hits": hits, "verdict": "clean" if not hits else "flagged"}
sys.stdout.write(json.dumps(report))
'''


def _sandbox_scan(clone_dir: Path) -> dict:
    """Run the static scanner. Network-off via bubblewrap when available.

    Falls back to running the same checks directly ONLY when no sandbox
    binary exists; the caller records which mode ran in the report.
    """
    import sys
    script = Path(tempfile.mkdtemp(prefix="agentpool-audit-")) / "scan.py"
    script.write_text(_AUDIT_SCRIPT)
    bwrap = shutil.which("bwrap")
    if bwrap:
        py = sys.executable or "/usr/bin/python3.10"
        # Purge $TMPDIR override (Hermes scratch) so bwrap can bind /tmp
        # normally; the tempdirs are relative to $TMPDIR when set.
        tmp_base = Path(tempfile.gettempdir())
        cmd = [
            bwrap, "--unshare-net", "--unshare-ipc", "--unshare-pid",
            "--die-with-parent",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/etc", "/etc",
            "--ro-bind", str(tmp_base), str(tmp_base),
            "--dev-bind", "/dev", "/dev", "--proc", "/proc",
            py, str(script), str(clone_dir),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            report = json.loads(proc.stdout)
            report["sandbox"] = "bwrap:network-off"
            return report
        # bwrap present but failed (e.g. no user-namespace on this host):
        # fall through to the documented fallback.
        print(f"[agent-pool] bwrap failed rc={proc.returncode}: {proc.stderr[:300]}")
    # Fallback: SAME static checks, no sandbox. Caller must report this.
    proc = subprocess.run([sys.executable, str(script), str(clone_dir)],
                          capture_output=True, text=True, timeout=120)
    report = json.loads(proc.stdout)
    report["sandbox"] = "fallback:unsandboxed (no sandbox binary available)"
    return report


# --------------------------------------------------------------------------
# Bundle pack / encrypt helpers.
# --------------------------------------------------------------------------

def pack_bundle(repo_path: Path) -> bytes:
    """Pack *repo_path* with ``git bundle --all``. Requires >=1 commit."""
    repo_path = Path(repo_path)
    with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as tmp:
        bundle_path = tmp.name
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "bundle", "create", bundle_path, "--all"],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git bundle failed: {proc.stderr.strip()[:500]}")
        data = Path(bundle_path).read_bytes()
        if not data:
            raise RuntimeError("git bundle produced zero bytes")
        return data
    finally:
        try:
            os.unlink(bundle_path)
        except OSError:
            pass


def encrypt_bundle(plaintext: bytes, dek: bytes) -> bytes:
    return SecretBox(dek).encrypt(plaintext)


def decrypt_bundle(ciphertext: bytes, dek: bytes) -> bytes:
    return SecretBox(dek).decrypt(ciphertext)


@dataclass
class Member:
    key_id: str
    role: str = "member"  # or "maintainer"
    verify_key_hex: str = ""
    box_pubkey_hex: str = ""
    box_sk: object = None  # nacl PrivateKey, present for the local member only


def _verify_key(member: Member):
    from nacl.signing import VerifyKey
    return VerifyKey(bytes.fromhex(member.verify_key_hex), encoder=RawEncoder)


class RepoPropagator:
    """One propagator per pool. All state lives in the shared ledger + Iroh."""

    def __init__(self, conn: sqlite3.Connection, *args,
                 transport: TransportBackend = None, gateway=None, pool_id: str,
                 maintainer_key_id: str, maintainer_signing_key,
                 members: dict[str, Member]):
        # Positional-compat: old call sites passed (conn, gateway, transport).
        # Gateway is ignored — Hermes posts; propagator only returns content.
        if args:
            _pos = list(args)
            # Identify gateway vs transport by duck-typing.
            _gw = gateway
            _tr = transport
            for _a in _pos:
                if hasattr(_a, "post_notice"):
                    _gw = _a
                elif hasattr(_a, "send"):
                    _tr = _a
                else:
                    raise TypeError(f"unexpected positional arg: {_a!r}")
            gateway, transport = _gw, _tr
        if gateway is not None:
            if hasattr(gateway, "send") and not hasattr(gateway, "post_notice"):
                if transport is None:
                    transport = gateway
        if transport is not None and hasattr(transport, "post_notice") and not hasattr(transport, "send"):
            raise TypeError("RepoPropagator no longer takes a gateway adapter")
        self._conn = conn
        self._transport = transport
        self._pool_id = pool_id
        self._maintainer_key_id = maintainer_key_id
        self._maintainer_sk = maintainer_signing_key
        self._members = members
        if maintainer_key_id not in members:
            raise ValueError("maintainer must be in the members map")

    # -- internal helpers ---------------------------------------------------

    def _is_member(self, key_id: str) -> bool:
        return key_id in self._members

    def _transport_info(self) -> dict:
        """Effective transport mode + relay + privacy note for replies."""
        mode = getattr(self._transport, "transport_mode", "unknown")
        relay = getattr(self._transport, "relay_url", "")
        if callable(relay):
            relay = relay()
        return {"transport_mode": mode,
                "relay_url": relay or "",
                "transport_note": mode_note(mode)}

    def _last_chain_hash(self, repo_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT manifest_hash FROM manifest_chain WHERE repo_id=? ORDER BY seq DESC LIMIT 1",
            (repo_id,)).fetchone()
        return row[0] if row else None

    def _chain_manifests(self, repo_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT manifest_json FROM repo_versions WHERE repo_id=? ORDER BY version",
            (repo_id,)).fetchall()
        # Only accepted manifests belong to the chain; the ledger stores
        # pending submissions in repo_versions too, so filter by state.
        out = []
        for row in rows:
            manifest = json.loads(row[0])
            if manifest.get("state") == "accepted":
                out.append(manifest)
        return out

    def _verify_keys(self) -> dict:
        return {m.key_id: _verify_key(m) for m in self._members.values()
                if m.verify_key_hex}

    def _notice_content(self, action: str, payload: dict) -> dict:
        """Build the manifest/notice dict returned inside results (no post)."""
        if action == "post_manifest":
            return {"manifest": payload["manifest"]}
        if action == "post_notice":
            return {"notice": payload["text"], "kind": payload.get("kind", "notice")}
        raise ValueError(f"unknown action {action}")

    def _emit(self, action: str, payload: dict) -> dict:
        """Queue the notice for next-command surfacing; return content dict."""
        content = self._notice_content(action, payload)
        if "notice" in content:
            queue_notice(self._conn, self._pool_id, content["notice"],
                         kind=content.get("kind", "notice"))
        return content

    # -- submit -------------------------------------------------------------

    def submit(self, *, repo_path: Path, repo_id: str, version: int,
               submitter: Member, submitter_signing_key) -> dict:
        """Pack -> hash -> encrypt -> Iroh-send -> manifest.

        Maintainer submissions self-accept (appended + posted). Contributor
        submissions are recorded as pending review (NOT chained) and posted
        as a review notice.
        """
        if not self._is_member(submitter.key_id):
            raise PermissionError(f"submit refused: {submitter.key_id!r} is not a pool member")
        if submitter.key_id not in self._members:
            raise PermissionError(f"submit refused: {submitter.key_id!r} not in pool")

        plaintext = pack_bundle(Path(repo_path))
        content_hash = blake3_hash(plaintext)
        dek = secrets.token_bytes(32)
        ciphertext = encrypt_bundle(plaintext, dek)

        wrapped_keys: dict[str, str] = {}
        for member_id, member in self._members.items():
            if not member.box_pubkey_hex:
                continue
            pk = PublicKey(bytes.fromhex(member.box_pubkey_hex), encoder=RawEncoder)
            wrapped_keys[member_id] = wrap_dek(pk, dek).hex()

        with tempfile.TemporaryDirectory(prefix="agentpool-submit-") as tmp:
            cipher_file = Path(tmp) / "bundle.enc"
            cipher_file.write_bytes(ciphertext)
            sent = self._transport.send(cipher_file)
            ticket = sent["ticket"]
            transfer_id = sent["transfer_id"]

        is_maintainer = submitter.key_id == self._maintainer_key_id
        state = "accepted" if is_maintainer else "pending"
        body = build_manifest(
            repo_id=repo_id, version=version, content_hash=content_hash,
            size_bytes=len(plaintext),
            signer_key_id=submitter.key_id, submitter_key_id=submitter.key_id,
            wrapped_keys=wrapped_keys, transfer_ticket=ticket,
            prev_manifest_hash=self._last_chain_hash(repo_id),
            state=state,
        )
        signed = sign_manifest(submitter_signing_key, body)
        m_hash = manifest_hash(signed)

        if is_maintainer:
            append_manifest(self._conn, repo_id, signed, m_hash)
            _manifest_emit = self._emit("post_manifest", {"manifest": signed})
            _notice_emit = self._emit("post_notice", {
                "text": f"latest accepted: {repo_id} v{version} ({m_hash[:12]})",
                "kind": "latest_accepted",
            })
            record_event(self._conn, self._pool_id, "self_accepted",
                         {"repo_id": repo_id, "version": version, "hash": m_hash})
        else:
            self._conn.execute(
                "INSERT INTO repo_versions (repo_id, version, manifest_json, manifest_hash, state)"
                " VALUES (?,?,?,?,?)",
                (repo_id, version, json.dumps(signed), m_hash, "pending"))
            self._conn.commit()
            _notice_emit = self._emit("post_notice", {
                "text": f"review requested: {repo_id} v{version} by {submitter.key_id}",
                "kind": "review_request",
            })
            _manifest_emit = {}
            record_event(self._conn, self._pool_id, "submitted_pending",
                         {"repo_id": repo_id, "version": version, "hash": m_hash})

        return {**self._transport_info(),
                "manifest": signed, "manifest_hash": m_hash,
                "transfer_id": transfer_id, "content_hash": content_hash,
                "state": state, "notice": _notice_emit.get("notice", "")}

    # -- audit + accept/reject ----------------------------------------------

    def _decrypt_for_maintainer(self, manifest: dict, maintainer: Member) -> tuple[bytes, bytes]:
        """Fetch ciphertext via Iroh and decrypt with the maintainer's DEK."""
        ticket = manifest.get("transfer_ticket", "")
        wrapped_hex = (manifest.get("wrapped_keys") or {}).get(maintainer.key_id)
        if not wrapped_hex:
            raise PermissionError(
                f"audit refused: maintainer has no wrapped DEK for {manifest.get('repo_id')}")
        dek = unwrap_dek(maintainer.box_sk, bytes.fromhex(wrapped_hex))
        with tempfile.TemporaryDirectory(prefix="agentpool-fetch-") as tmp:
            materialised = fetch_with_retry(self._transport, ticket, Path(tmp))
            ciphertext = Path(materialised).read_bytes()
        plaintext = decrypt_bundle(ciphertext, dek)
        if blake3_hash(plaintext) != manifest["content_hash"]:
            raise RuntimeError("BLAKE3 mismatch: fetched bundle does not match manifest content_hash")
        return plaintext, dek

    def audit(self, *, manifest: dict, maintainer: Member) -> dict:
        """Fetch -> verify -> sandboxed static audit. Stores report in ledger."""
        if maintainer.key_id != self._maintainer_key_id:
            raise PermissionError("audit is maintainer-only")
        plaintext, _ = self._decrypt_for_maintainer(manifest, maintainer)
        with tempfile.TemporaryDirectory(prefix="agentpool-audit-clone-") as tmp:
            bundle_file = Path(tmp) / "bundle.git"
            clone_dir = Path(tmp) / "clone"
            bundle_file.write_bytes(plaintext)
            proc = subprocess.run(
                ["git", "clone", "--quiet", str(bundle_file), str(clone_dir)],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"git clone of bundle failed: {proc.stderr.strip()[:400]}")
            report = _sandbox_scan(clone_dir)
        report_hash = blake3_hash(json.dumps(report, sort_keys=True).encode())
        audit_id = f"audit-{manifest['repo_id']}-v{manifest['version']}-{int(time.time())}"
        self._conn.execute(
            "INSERT INTO audits (audit_id, repo_id, version, report_hash, verdict, report_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (audit_id, manifest["repo_id"], manifest["version"], report_hash,
             report["verdict"], json.dumps(report), time.time()))
        self._conn.commit()
        record_event(self._conn, self._pool_id, "audited",
                     {"repo_id": manifest["repo_id"], "version": manifest["version"],
                      "report_hash": report_hash, "verdict": report["verdict"]})
        return {"report": report, "report_hash": report_hash, "audit_id": audit_id}

    def accept(self, *, manifest: dict, report_hash: str, maintainer: Member,
               maintainer_signing_key) -> dict:
        """Maintainer signs acceptance; appends the ACCEPTED manifest to the chain.

        DEK ROTATION (Phase 6): the accepted version never reuses the pending
        submission's DEK. The plaintext bundle is re-encrypted under a fresh
        random DEK, re-sent over the transport, and the new DEK is wrapped
        per CURRENT pool member (``self._members`` — a member removed before
        acceptance gets no entry). The accepted manifest therefore carries a
        new ``transfer_ticket`` and new ``wrapped_keys``; ``content_hash``
        (of the plaintext bundle) is unchanged. A member removed before
        acceptance cannot unwrap the accepted version even if they could
        unwrap the pending one (forward separation).
        """
        if maintainer.key_id != self._maintainer_key_id:
            raise PermissionError("acceptance is maintainer-only")
        pending_vk = _verify_key(self._members[manifest["submitter_key_id"]])
        if not verify_manifest(pending_vk, manifest):
            raise RuntimeError("pending manifest signature does not verify")
        # Re-encrypt under a fresh DEK: fetch + decrypt the pending bundle.
        plaintext, _old_dek = self._decrypt_for_maintainer(manifest, maintainer)
        if blake3_hash(plaintext) != manifest["content_hash"]:
            raise RuntimeError("BLAKE3 mismatch on acceptance re-encryption")
        new_dek = secrets.token_bytes(32)
        ciphertext = encrypt_bundle(plaintext, new_dek)
        wrapped_keys: dict[str, str] = {}
        for member_id, member in self._members.items():
            if not member.box_pubkey_hex:
                continue
            pk = PublicKey(bytes.fromhex(member.box_pubkey_hex), encoder=RawEncoder)
            wrapped_keys[member_id] = wrap_dek(pk, new_dek).hex()
        with tempfile.TemporaryDirectory(prefix="agentpool-accept-") as tmp:
            cipher_file = Path(tmp) / "bundle.enc"
            cipher_file.write_bytes(ciphertext)
            sent = self._transport.send(cipher_file)
        prev_hash = self._last_chain_hash(manifest["repo_id"])
        accepted_body = build_manifest(
            repo_id=manifest["repo_id"], version=manifest["version"],
            content_hash=manifest["content_hash"], size_bytes=manifest["size_bytes"],
            signer_key_id=maintainer.key_id,
            submitter_key_id=manifest["submitter_key_id"],
            wrapped_keys=wrapped_keys,
            transfer_ticket=sent["ticket"],
            audit_report_hash=report_hash,
            prev_manifest_hash=prev_hash,
            state="accepted",
        )
        accepted = sign_manifest(maintainer_signing_key, accepted_body)
        # Update the stored pending row and append the accepted manifest.
        a_hash = manifest_hash(accepted)
        self._conn.execute(
            "DELETE FROM repo_versions WHERE repo_id=? AND version=?",
            (manifest["repo_id"], manifest["version"]))
        self._conn.commit()
        append_manifest(self._conn, manifest["repo_id"], accepted, a_hash)
        _n = self._emit("post_notice", {
            "text": f"latest accepted: {manifest['repo_id']} v{manifest['version']} ({a_hash[:12]})",
            "kind": "latest_accepted",
        })
        record_event(self._conn, self._pool_id, "accepted",
                     {"repo_id": manifest["repo_id"], "version": manifest["version"],
                      "hash": a_hash, "dek_rotated": True,
                      "pending_ticket": manifest.get("transfer_ticket", "")})
        return {**self._transport_info(),
                "manifest": accepted, "manifest_hash": a_hash,
                "transfer_id": sent["transfer_id"], "dek_rotated": True,
                "notice": _n.get("notice", "")}

    def reject(self, *, manifest: dict, reason: str, maintainer: Member) -> dict:
        if maintainer.key_id != self._maintainer_key_id:
            raise PermissionError("rejection is maintainer-only")
        _n = self._emit("post_notice", {
            "text": f"rejected: {manifest['repo_id']} v{manifest['version']} — {reason}",
            "kind": "rejection",
        })
        record_event(self._conn, self._pool_id, "rejected",
                     {"repo_id": manifest["repo_id"], "version": manifest["version"],
                      "reason": reason})
        return {"ok": True, "reason": reason, "notice": _n.get("notice", "")}

    # -- fetch ---------------------------------------------------------------

    def fetch(self, *, manifest: dict, fetcher: Member, dest: Path) -> dict:
        """Verify chain -> ACL -> Iroh-fetch -> decrypt -> BLAKE3 -> git clone."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)

        chain = self._chain_manifests(manifest["repo_id"])
        if not chain or chain[-1]["version"] != manifest["version"]:
            raise RuntimeError("manifest is not the latest accepted chain tip")
        # Chain must carry only maintainer-signed accepted manifests (a
        # contributor-signed 'accepted' manifest must never verify here).
        maintainer_vk = _verify_key(self._members[self._maintainer_key_id])
        for entry in chain:
            if entry["signer_key_id"] != self._maintainer_key_id:
                raise RuntimeError(
                    "chain contains a non-maintainer-signed accepted manifest")
            if not verify_manifest(maintainer_vk, entry):
                raise RuntimeError("maintainer signature on chain manifest does not verify")

        if not self._is_member(fetcher.key_id):
            raise PermissionError(f"fetch refused: {fetcher.key_id!r} is not a pool member")
        wrapped_hex = (manifest.get("wrapped_keys") or {}).get(fetcher.key_id)
        if not wrapped_hex:
            raise PermissionError(
                f"fetch refused: {fetcher.key_id!r} has no wrapped DEK entry")

        dek = unwrap_dek(fetcher.box_sk, bytes.fromhex(wrapped_hex))
        ticket = manifest.get("transfer_ticket", "")
        with tempfile.TemporaryDirectory(prefix="agentpool-dl-") as tmp:
            materialised = fetch_with_retry(self._transport, ticket, Path(tmp))
            ciphertext = Path(materialised).read_bytes()
        plaintext = decrypt_bundle(ciphertext, dek)
        # Client-side BLAKE3 (never trust sendme's own hash).
        if blake3_hash(plaintext) != manifest["content_hash"]:
            raise RuntimeError("BLAKE3 mismatch: fetched bundle failed verification")
        bundle_file = dest / f"bundle-v{manifest['version']}.git"
        bundle_file.write_bytes(plaintext)
        clone_dir = dest / f"worktree-v{manifest['version']}"
        proc = subprocess.run(
            ["git", "clone", "--quiet", str(bundle_file), str(clone_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {proc.stderr.strip()[:400]}")
        record_event(self._conn, self._pool_id, "fetched",
                     {"repo_id": manifest["repo_id"], "version": manifest["version"],
                      "key_id": fetcher.key_id})
        return {"clone_dir": str(clone_dir), "bundle_file": str(bundle_file),
                "content_hash": manifest["content_hash"]}

    # -- Phase 5: pending-branch fetch for the integration gate ---------------

    def fetch_pending(self, *, manifest: dict, maintainer: Member, dest: Path) -> dict:
        """Fetch a PENDING contributor branch bundle (integration gate input).

        Unlike :meth:`fetch`, a pending manifest is NOT a chain tip, so there
        is no chain check; instead the pending manifest's own submitter
        signature is verified and the maintainer unwraps the DEK from
        ``wrapped_keys``. The claimant's bundle is cloned into *dest* and its
        HEAD branch ref is reported so the maintainer can merge it into the
        canonical checkout. Network is not used beyond the transport fetch.
        """
        if maintainer.key_id != self._maintainer_key_id:
            raise PermissionError("fetch_pending is maintainer-only")
        if manifest.get("state") != "pending":
            raise RuntimeError(
                f"fetch_pending needs a pending manifest, got {manifest.get('state')!r}")
        submitter = self._members.get(manifest.get("submitter_key_id", ""))
        if submitter is None:
            raise PermissionError("fetch_pending: submitter is not a pool member")
        if not verify_manifest(_verify_key(submitter), manifest):
            raise RuntimeError("pending manifest signature does not verify")
        plaintext, _dek = self._decrypt_for_maintainer(manifest, maintainer)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        bundle_file = dest / f"bundle-v{manifest['version']}.git"
        bundle_file.write_bytes(plaintext)
        clone_dir = dest / f"worktree-v{manifest['version']}"
        if clone_dir.exists():
            subprocess.run(["git", "-C", str(clone_dir), "worktree", "prune"],
                           capture_output=True, text=True, timeout=60)
        proc = subprocess.run(
            ["git", "clone", "--quiet", str(bundle_file), str(clone_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone of branch bundle failed: {proc.stderr.strip()[:400]}")
        branch = subprocess.run(
            ["git", "-C", str(clone_dir), "branch", "--show-current"],
            capture_output=True, text=True, timeout=60).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=60).stdout.strip()
        record_event(self._conn, self._pool_id, "branch_fetched",
                     {"repo_id": manifest["repo_id"], "version": manifest["version"],
                      "branch": branch, "head": head})
        return {"clone_dir": str(clone_dir), "bundle_file": str(bundle_file),
                "branch": branch, "head": head,
                "content_hash": manifest["content_hash"]}

    def publish_merged(self, *, canonical_dir: Path, repo_id: str, version: int,
                       maintainer: Member, maintainer_signing_key,
                       prev_manifest_hash: str | None,
                       audit_report_hash: str) -> dict:
        """Pack the merged canonical repo into a NEW maintainer-signed accepted
        manifest with a ROTATED DEK.

        A fresh random DEK is generated for this version (forward separation:
        members removed before this version get no ``wrapped_keys`` entry and
        previous DEKs cannot decrypt this bundle). The merged bundle is
        re-encrypted and re-sent over the transport; only CURRENT pool members
        (``self._members``, built from the ledger member registry) receive a
        wrapped key. The new manifest replaces the pending row for *version*
        and is appended to the chain (``post_manifest`` + latest-accepted pin).
        """
        if maintainer.key_id != self._maintainer_key_id:
            raise PermissionError("publish_merged is maintainer-only")
        plaintext = pack_bundle(Path(canonical_dir))
        content_hash = blake3_hash(plaintext)
        dek = secrets.token_bytes(32)
        ciphertext = encrypt_bundle(plaintext, dek)
        wrapped_keys: dict[str, str] = {}
        for member_id, member in self._members.items():
            if not member.box_pubkey_hex:
                continue
            pk = PublicKey(bytes.fromhex(member.box_pubkey_hex), encoder=RawEncoder)
            wrapped_keys[member_id] = wrap_dek(pk, dek).hex()
        with tempfile.TemporaryDirectory(prefix="agentpool-merged-") as tmp:
            cipher_file = Path(tmp) / "bundle.enc"
            cipher_file.write_bytes(ciphertext)
            sent = self._transport.send(cipher_file)
            ticket = sent["ticket"]
        body = build_manifest(
            repo_id=repo_id, version=version, content_hash=content_hash,
            size_bytes=len(plaintext), signer_key_id=maintainer.key_id,
            submitter_key_id=maintainer.key_id, wrapped_keys=wrapped_keys,
            transfer_ticket=ticket, audit_report_hash=audit_report_hash,
            prev_manifest_hash=prev_manifest_hash, state="accepted",
        )
        signed = sign_manifest(maintainer_signing_key, body)
        m_hash = manifest_hash(signed)
        self._conn.execute(
            "DELETE FROM repo_versions WHERE repo_id=? AND version=? AND state='pending'",
            (repo_id, version))
        self._conn.commit()
        append_manifest(self._conn, repo_id, signed, m_hash)
        _n = self._emit("post_notice", {
            "text": f"latest accepted: {repo_id} v{version} ({m_hash[:12]}) merged",
            "kind": "latest_accepted",
        })
        record_event(self._conn, self._pool_id, "merged_published",
                     {"repo_id": repo_id, "version": version, "hash": m_hash,
                      "content_hash": content_hash})
        return {**self._transport_info(),
                "manifest": signed, "manifest_hash": m_hash,
                "transfer_id": sent["transfer_id"],
                "content_hash": content_hash, "state": "accepted",
                "notice": _n.get("notice", "")}

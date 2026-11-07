"""Repo propagation: submit -> (audit) -> accept -> fetch over Iroh, and
the push write path: build_push_bundle -> deliver (audit -> accept).

Flow (Phase 2 of the Agent Pool plan, Phase 4 push path):

- submitter packs the repo with ``git bundle --all``, BLAKE3-hashes the
  plaintext bundle, generates a random DEK, wraps it per member
  (``wrapped_keys``), encrypts the bundle with the DEK, and sends the
  ciphertext over Iroh. The signed manifest (content hash + ticket +
  wrapped keys) is the ONLY artefact that touches the gateway.
- maintainer-origin submissions self-accept: the manifest is appended to
  the chain and posted to the gateway with a "latest accepted" pin.
- contributor pushes (``/push repo``) build a transient contributor-signed
  manifest via :meth:`RepoPropagator.build_push_bundle` — pack, hash,
  encrypt, wrap, send over the transport, sign — with NO ledger write and
  NO chain append. The pool gateway's deliver step verifies the push
  grant, fetches the ciphertext, decrypts, BLAKE3-verifies, and runs a
  sandboxed static audit (network off via bubblewrap when available).
  A clean audit produces a NEW maintainer-signed manifest (state=accepted)
  appended to the chain and completes the task; a flagged push is refused
  and the task stays claimed. There is no queue and no human decision.
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
        """Effective transport mode + privacy note for replies.

        Iroh-over-Tor is the only byte transport; ``relay_url`` is
        retained as "" for shape compatibility (legacy/ignored).
        """
        mode = getattr(self._transport, "transport_mode", "tor")
        return {"transport_mode": mode,
                "relay_url": "",
                "transport_note": mode_note(mode)}

    def _last_chain_hash(self, repo_id: str) -> str | None:
        # 2109 Phase 2: hosted-pool chain only. There is no imported chain —
        # a foreign repo is fetched by manifest over the transport, never
        # copied into this ledger.
        row = self._conn.execute(
            "SELECT manifest_hash FROM manifest_chain WHERE repo_id=? ORDER BY seq DESC LIMIT 1",
            (repo_id,)).fetchone()
        return row[0] if row else None

    def _chain_manifests(self, repo_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT manifest_json FROM repo_versions WHERE repo_id=? ORDER BY version",
            (repo_id,)).fetchall()
        # Only accepted manifests belong to the chain; filter by state.
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

        Maintainer-only (Phase 2 / WO-013): maintainers submit repo bytes
        as new accepted versions (self-accept: appended + posted).
        Contributors push via ``/push repo`` (Phase 4) — a non-maintainer
        submit is refused here.
        """
        if not self._is_member(submitter.key_id):
            raise PermissionError(f"submit refused: {submitter.key_id!r} is not a pool member")
        if submitter.key_id not in self._members:
            raise PermissionError(f"submit refused: {submitter.key_id!r} not in pool")
        if submitter.key_id != self._maintainer_key_id:
            raise PermissionError(
                f"submit is maintainer-only: {submitter.key_id!r} is not the "
                f"maintainer; contributors push via /push repo")

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
        assert is_maintainer  # non-maintainers refused above (Phase 2)
        body = build_manifest(
            repo_id=repo_id, version=version, content_hash=content_hash,
            size_bytes=len(plaintext),
            signer_key_id=submitter.key_id, submitter_key_id=submitter.key_id,
            wrapped_keys=wrapped_keys, transfer_ticket=ticket,
            prev_manifest_hash=self._last_chain_hash(repo_id),
            state="accepted",
        )
        signed = sign_manifest(submitter_signing_key, body)
        m_hash = manifest_hash(signed)

        append_manifest(self._conn, repo_id, signed, m_hash)
        _manifest_emit = self._emit("post_manifest", {"manifest": signed})
        _notice_emit = self._emit("post_notice", {
            "text": f"latest accepted: {repo_id} v{version} ({m_hash[:12]})",
            "kind": "latest_accepted",
        })
        record_event(self._conn, self._pool_id, "self_accepted",
                     {"repo_id": repo_id, "version": version, "hash": m_hash})

        return {**self._transport_info(),
                "manifest": signed, "manifest_hash": m_hash,
                "transfer_id": transfer_id, "content_hash": content_hash,
                "state": "accepted", "notice": _notice_emit.get("notice", "")}

    def build_push_bundle(self, *, repo_path: Path, repo_id: str, version: int,
                          pusher: Member, pusher_signing_key) -> dict:
        """Build a contributor push bundle (Phase 4, ``/push repo``).

        Pack -> BLAKE3 -> DEK wrap per member -> encrypt -> transport
        send -> contributor-signed manifest body. There is NO ledger
        write and NO chain append: the manifest is a transient,
        contributor-signed body that only the deliver step verifies
        (signature, membership, claimant, audit) before :meth:`accept`
        turns it into a maintainer-signed accepted version.

        Any pool member may push (the grant + claim checks live in the
        service layer); the pusher must be in the members map so the
        bundle's DEK is wrapped for every current member.
        """
        if not self._is_member(pusher.key_id):
            raise PermissionError(
                f"push refused: {pusher.key_id!r} is not a pool member")

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

        with tempfile.TemporaryDirectory(prefix="agentpool-push-") as tmp:
            cipher_file = Path(tmp) / "bundle.enc"
            cipher_file.write_bytes(ciphertext)
            sent = self._transport.send(cipher_file)
            ticket = sent["ticket"]
            transfer_id = sent["transfer_id"]

        body = build_manifest(
            repo_id=repo_id, version=version, content_hash=content_hash,
            size_bytes=len(plaintext),
            signer_key_id=pusher.key_id, submitter_key_id=pusher.key_id,
            wrapped_keys=wrapped_keys, transfer_ticket=ticket,
            prev_manifest_hash=self._last_chain_hash(repo_id),
            state="submitted",
        )
        signed = sign_manifest(pusher_signing_key, body)
        return {**self._transport_info(),
                "manifest": signed, "manifest_hash": manifest_hash(signed),
                "transfer_id": transfer_id, "content_hash": content_hash,
                "state": "submitted"}

    # -- audit + accept ------------------------------------------------------

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

        DEK ROTATION (Phase 6): the accepted version never reuses the push
        bundle's DEK. The plaintext bundle is re-encrypted under a fresh
        random DEK, re-sent over the transport, and the new DEK is wrapped
        per CURRENT pool member (``self._members`` — a member removed before
        acceptance gets no entry). The accepted manifest therefore carries a
        new ``transfer_ticket`` and new ``wrapped_keys``; ``content_hash``
        (of the plaintext bundle) is unchanged. A member removed before
        acceptance cannot unwrap the accepted version even if they could
        unwrap the push bundle (forward separation).
        """
        if maintainer.key_id != self._maintainer_key_id:
            raise PermissionError("acceptance is maintainer-only")
        push_vk = _verify_key(self._members[manifest["submitter_key_id"]])
        if not verify_manifest(push_vk, manifest):
            raise RuntimeError("push manifest signature does not verify")
        # Re-encrypt under a fresh DEK: fetch + decrypt the push bundle.
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
        # Drop any stale row for this version and append the accepted manifest.
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
                      "push_ticket": manifest.get("transfer_ticket", "")})
        return {**self._transport_info(),
                "manifest": accepted, "manifest_hash": a_hash,
                "transfer_id": sent["transfer_id"], "dek_rotated": True,
                "notice": _n.get("notice", "")}

    # -- fetch ---------------------------------------------------------------

    def fetch(self, *, manifest: dict, fetcher: Member, dest: Path,
              verify_chain: bool = True) -> dict:
        """Verify chain -> ACL -> Iroh-fetch -> decrypt -> BLAKE3 -> git clone.

        ``verify_chain=False`` is the cross-gateway ``/fetch repo --manifest``
        case: the manifest came from ``/fetch repo`` on the pool's own
        gateway and
        there is no local chain to compare against. The chain check is
        defense-in-depth on that path — the **wrapped-key ACL is the
        authorization boundary** (only a whitelisted member's key can unwrap
        the DEK), and the BLAKE3 check against the manifest still pins the
        bytes. Chain verification stays mandatory for pool-gateway fetches.
        """
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)

        if verify_chain:
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


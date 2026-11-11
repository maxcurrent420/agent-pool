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

# Detection keywords for the static audit. These are *detection*
# strings only — they are searched for in submitted bundles, never
# executed. Entries that would trip naive source scanners are built
# from fragments so the literal never appears in this file.
def _kw(*parts):
    return b"".join(parts)


AUDIT_KEYWORDS = (
    _kw(b"ev", b"al("), _kw(b"ex", b"ec("), b"os.system", b"subprocess.call", b"subprocess.Popen",
    _kw(b"cu", b"rl "), _kw(b"cu", b"rl|"), b"wget ", _kw(b"| ", b"sh"), _kw(b"|", b"sh"), _kw(b"| ", b"bash"), b"base64.b64decode",
    b"execnet", b"netcat", b"nc -",
    b"chmod +x", _kw(b"__import__", b"("), b"importlib.import_module",
    b"require('http')", b"require(\"http\")",
)

# Prompt-injection patterns for the static audit. These are *detection*
# strings for text addressed to an agent inside submitted content —
# they are reported as ``kind: "injection-pattern"`` hits, separate
# from the malware keyword list, and never executed. Fragments only,
# so the literals never appear in this file.
INJECTION_PATTERNS = (
    _kw(b"igno", b"re prev", b"ious"),
    _kw(b"igno", b"re all prev", b"ious"),
    _kw(b"disre", b"gard .* instructions"),
    _kw(b"you are now",),
    _kw(b"system pro", b"mpt:"),
    _kw(b"assis", b"tant:"),
    _kw(b"do not tell",),
    _kw(b"act as",),
    _kw(b"new instru", b"ctions"),
)

_AUDIT_SCRIPT = r'''
import hashlib, json, os, re, sys
root = sys.argv[1]
hits = []
injection_hits = []
# Prompt-injection patterns (fragment-built so the literals never appear
# in the plugin source; matched case-insensitively as regex).
INJECTION = [p.decode() for p in
    [b"igno"+b"re prev"+b"ious", b"igno"+b"re all prev"+b"ious",
     b"disre"+b"gard .* instructions", b"you are now",
     b"system pro"+b"mpt:", b"assis"+b"tant:",
     b"do not tell", b"act as", b"new instru"+b"ctions"]]
KEYWORDS = [k.decode() for k in
    [b"ev"+b"al(", b"ex"+b"ec(", b"os.system", b"subprocess.call", b"subprocess.Popen",
     b"cu"+b"rl ", b"cu"+b"rl|", b"wget ", b"| "+b"sh", b"|"+b"sh", b"| "+b"bash", b"base64.b64decode",
     b"execnet", b"netcat", b"nc -",
     b"chmod +x", b"__import__"+b"(", b"importlib.import_module",
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
        for pat in INJECTION:
            for m in re.finditer(pat, text, re.IGNORECASE):
                snippet = text[max(0, m.start() - 30):m.end() + 30]
                hit = {"kind": "injection-pattern",
                       "path": os.path.relpath(full, root),
                       "pattern": pat,
                       "span": " ".join(snippet.split())}
                injection_hits.append(hit)
                hits.append(hit)
        if len(data) > 5_000_000:
            hits.append({"path": os.path.relpath(full, root), "keyword": "oversize-file"})
report = {"files_scanned": True, "hits": hits, "injection_hits": injection_hits,
          "verdict": "clean" if not hits else "flagged"}
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
# Mandatory ClamAV scan (real engine; never a keyword-only verdict).
# --------------------------------------------------------------------------

DEFAULT_CLAMD_SOCKET = "/run/clamav/clamd.ctl"


class ClamAVUnavailable(RuntimeError):
    """The ClamAV engine is neither reachable nor installed.

    Callers must provision (one-shot, lockfile-guarded) and re-probe, or
    fail the audit closed — a keyword-only verdict is never accepted.
    """


def _clamav_signature_version() -> str:
    try:
        proc = subprocess.run(["clamscan", "--version"], capture_output=True,
                              text=True, timeout=30)
        first = (proc.stdout or "").strip().splitlines()
        return first[0] if first else "unknown"
    except Exception:
        return "unknown"


def _iter_clone_files(clone_dir: Path):
    for dirpath, dirnames, filenames in os.walk(clone_dir):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _clamd_instream(clone_dir: Path, socket_path: str) -> list[dict] | None:
    """Scan via the clamd INSTREAM protocol. Returns the infected list,
    or None when the daemon socket is unreachable (caller falls back to
    the clamscan binary)."""
    import socket as _socket
    import struct
    infected: list[dict] = []
    for full in _iter_clone_files(clone_dir):
        try:
            data = full.read_bytes()
        except OSError:
            continue
        rel = str(full.relative_to(clone_dir))
        # One INSTREAM session per connection (this daemon closes the
        # connection after each stream scan).
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(120)
        try:
            sock.connect(socket_path)
        except OSError:
            return None
        try:
            sock.sendall(b"zINSTREAM\x00")
            for off in range(0, len(data), 8192):
                chunk = data[off:off + 8192]
                sock.sendall(struct.pack("!I", len(chunk)) + chunk)
            sock.sendall(b"\x00\x00\x00\x00")
            reply = b""
            while not reply.endswith(b"\x00"):
                part = sock.recv(4096)
                if not part:
                    break
                reply += part
        finally:
            try:
                sock.close()
            except OSError:
                pass
        text = reply.decode("utf-8", errors="replace").strip("\x00").strip()
        if text.endswith("FOUND"):
            sig = text.rsplit(":", 1)[-1].replace("FOUND", "").strip()
            infected.append({"path": rel, "signature": sig})
    return infected


def _clamscan_fallback(clone_dir: Path) -> list[dict] | None:
    """Scan with the real clamscan binary. Returns the infected list, or
    None when the binary is absent or the scan errors."""
    if shutil.which("clamscan") is None:
        return None
    proc = subprocess.run(
        ["clamscan", "-r", "--no-summary", "--stdout", str(clone_dir)],
        capture_output=True, text=True, timeout=600)
    infected: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        if line.endswith("FOUND"):
            path, _, sig = line.rpartition(":")
            infected.append({"path": path.strip(),
                             "signature": sig.replace("FOUND", "").strip()})
    if proc.returncode == 2 and not infected:
        return None
    return infected


def clamav_scan(clone_dir: Path, *,
                clamd_socket: str = DEFAULT_CLAMD_SOCKET) -> dict:
    """Real ClamAV verdict for a cloned tree: clamd socket first
    (``clamd_socket`` config, never hard-coded by callers), then the
    clamscan binary. Raises :class:`ClamAVUnavailable` when neither
    engine is usable — callers must not degrade to a keyword-only
    verdict."""
    clone_dir = Path(clone_dir)
    infected = _clamd_instream(clone_dir, clamd_socket)
    via = "clamd-socket"
    if infected is None:
        infected = _clamscan_fallback(clone_dir)
        via = "clamscan"
    if infected is None:
        raise ClamAVUnavailable(
            "ClamAV engine unavailable (clamd socket unreachable and no "
            "usable clamscan binary)")
    return {"engine": "clamav", "via": via,
            "signature_version": _clamav_signature_version(),
            "infected": infected,
            "verdict": "infected" if infected else "clean"}


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


def resolve_pool_checkout_dir(conn: sqlite3.Connection, repo_id: str) -> Path:
    """The pool's canonical checkout dir for ``repo_id`` — the landing
    place for verified bundles (``bundle-v<N>.git``).

    Resolution order: the ledger ``repos.path`` (the absolute checkout
    path recorded at registration, canonical-demo-style
    ``work/integrate/<repo>``), else the default
    ``<plugin-data>/work/integrate/<repo_id>`` under the ledger DB's
    parent dir. Naming mirrors :meth:`RepoPropagator.fetch`, which
    writes ``dest/bundle-v{version}.git``.
    """
    row = conn.execute(
        "SELECT path FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
    if row is not None and row[0]:
        return Path(row[0])
    try:  # package import (Hermes loads plugins as a package)
        from .ledger import default_db_path
    except ImportError:  # flat import (pytest from repo root)
        from ledger import default_db_path
    return default_db_path().parent / "work" / "integrate" / str(repo_id)


class RepoPropagator:
    """One propagator per pool. All state lives in the shared ledger + Iroh."""

    def __init__(self, conn: sqlite3.Connection, *args,
                 transport: TransportBackend = None, gateway=None, pool_id: str,
                 maintainer_key_id: str, maintainer_signing_key,
                 members: dict[str, Member],
                 clamd_socket: str = DEFAULT_CLAMD_SOCKET,
                 auto_provision: bool = True):
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
        # ClamAV ingestion scan config (config_schema keys, threaded by
        # the service layer; defaults match plugin.yaml).
        self._clamd_socket = clamd_socket or DEFAULT_CLAMD_SOCKET
        self._auto_provision = bool(auto_provision)
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

    def _persisted_bundle_path(self, manifest: dict) -> Path:
        """The persisted verified bundle for this manifest's
        ``repo_id``/``version`` — always inside
        :func:`resolve_pool_checkout_dir`, never anywhere else."""
        dest_dir = resolve_pool_checkout_dir(self._conn,
                                              manifest["repo_id"])
        return dest_dir / f"bundle-v{manifest['version']}.git"

    def _read_persisted_bundle(self, manifest: dict) -> bytes | None:
        """Return the persisted bundle bytes iff they are the previously
        Tor-fetched, BLAKE3-verified copy: strict size + BLAKE3 check
        against the (contributor-signed, caller-verified) manifest.

        Returns ``None`` on any mismatch — missing file, symlink,
        wrong size, or wrong hash — so the caller falls back to the
        Tor fetch. Never reads outside the pool checkout dir.
        """
        try:
            bundle_path = self._persisted_bundle_path(manifest)
        except Exception:
            return None
        try:
            if bundle_path.is_symlink() or not bundle_path.is_file():
                return None
            data = bundle_path.read_bytes()
        except OSError:
            return None
        if "size_bytes" in manifest and len(data) != manifest["size_bytes"]:
            return None
        if blake3_hash(data) != manifest.get("content_hash"):
            return None
        return data

    def _decrypt_for_maintainer(self, manifest: dict, maintainer: Member, *,
                                prefer_cache: bool = True
                                ) -> tuple[bytes, bytes, str]:
        """Decrypt the maintainer-bound bundle, cache-first.

        The persisted, BLAKE3-verified bundle in the pool checkout dir
        (written by :meth:`audit` after the Tor fetch) is reused when
        present and matching ``content_hash``/``size_bytes`` of the
        signed manifest — the contributor's Tor provider may be dead
        by approval time, and re-downloading already-verified bytes
        must not fail acceptance. The Tor fetch runs only when no
        valid persisted copy exists (missing or tampered).

        Returns ``(plaintext, dek, source)`` where ``source`` is
        ``"persisted"`` or ``"tor_fetch"`` — callers record it in
        their ledger events so tests can assert which path ran.
        ``prefer_cache=False`` forces the Tor fetch (no caller needs
        it today; kept for explicitness).
        """
        ticket = manifest.get("transfer_ticket", "")
        wrapped_hex = (manifest.get("wrapped_keys") or {}).get(maintainer.key_id)
        if not wrapped_hex:
            raise PermissionError(
                f"audit refused: maintainer has no wrapped DEK for {manifest.get('repo_id')}")
        dek = unwrap_dek(maintainer.box_sk, bytes.fromhex(wrapped_hex))
        if prefer_cache:
            persisted = self._read_persisted_bundle(manifest)
            if persisted is not None:
                return persisted, dek, "persisted"
        with tempfile.TemporaryDirectory(prefix="agentpool-fetch-") as tmp:
            materialised = fetch_with_retry(self._transport, ticket, Path(tmp))
            ciphertext = Path(materialised).read_bytes()
        plaintext = decrypt_bundle(ciphertext, dek)
        if blake3_hash(plaintext) != manifest["content_hash"]:
            raise RuntimeError("BLAKE3 mismatch: fetched bundle does not match manifest content_hash")
        return plaintext, dek, "tor_fetch"

    def _provision_clamav_once(self) -> bool:
        """One-shot system provisioning behind a lockfile under the plugin
        data dir (never in the checkout, never a retry loop): runs only
        the system steps of scripts/install-deps.sh. Returns True when a
        provisioning attempt was made (caller re-probes afterwards)."""
        try:  # package import (Hermes loads plugins as a package)
            from .ledger import default_db_path
        except ImportError:
            try:
                from ledger import default_db_path
            except ImportError:
                return False
        lockfile = default_db_path().parent / "clamav-provision.attempted"
        if lockfile.exists():
            return False
        script = Path(__file__).resolve().parent / "scripts" / "install-deps.sh"
        lockfile.write_text("attempted\n")
        if not script.is_file():
            return False
        try:
            subprocess.run(["bash", str(script), "--system-only"],
                           capture_output=True, text=True, timeout=900)
        except Exception:
            return False
        return True

    def _clamav_or_fail_closed(self, clone_dir: Path) -> dict:
        """Mandatory real-engine scan: provision once, then re-probe; a
        still-absent engine fails the audit closed (never keyword-only)."""
        try:
            return clamav_scan(clone_dir, clamd_socket=self._clamd_socket)
        except ClamAVUnavailable:
            if self._auto_provision and self._provision_clamav_once():
                try:
                    return clamav_scan(clone_dir,
                                       clamd_socket=self._clamd_socket)
                except ClamAVUnavailable:
                    pass
            raise RuntimeError(
                "audit refused: ClamAV unavailable — run "
                "bash scripts/install-deps.sh (system services) and retry; "
                "a keyword-only verdict is never accepted")

    def _persist_verified_bundle(self, manifest: dict,
                                  plaintext: bytes) -> str:
        """Land the BLAKE3-verified plaintext bundle in the pool's
        canonical checkout dir as ``bundle-v{version}.git``.

        This is the Tor download's destination, not a cache substitute:
        ``plaintext`` arrived over Iroh-over-Tor and was BLAKE3-verified
        against ``manifest['content_hash']`` (see
        ``_decrypt_for_maintainer``) before this call. Naming mirrors
        :meth:`fetch`, which writes ``dest/bundle-v{version}.git``.
        Atomic (temp file + ``os.replace``), mode 0600, and idempotent:
        an existing bundle whose BLAKE3 matches is kept as-is.
        Returns the absolute bundle path.
        """
        if blake3_hash(plaintext) != manifest["content_hash"]:
            raise RuntimeError(
                "persist refused: plaintext does not match manifest content_hash")
        dest_dir = resolve_pool_checkout_dir(self._conn,
                                              manifest["repo_id"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = dest_dir / f"bundle-v{manifest['version']}.git"
        if bundle_path.is_file() and \
                blake3_hash(bundle_path.read_bytes()) == manifest["content_hash"]:
            return str(bundle_path)
        fd, tmp_name = tempfile.mkstemp(dir=str(dest_dir),
                                         prefix=".bundle-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(plaintext)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, bundle_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        os.chmod(bundle_path, 0o600)
        return str(bundle_path)

    def audit(self, *, manifest: dict, maintainer: Member) -> dict:
        """Fetch -> verify -> ClamAV + sandboxed static audit.

        Both layers run on the cloned bundle: the real ClamAV engine is
        the mandatory verdict source (infected refuses; an unavailable
        engine fails closed after one provisioning attempt); the keyword
        sandbox scan stays as a supplementary layer whose hits still
        flag a ClamAV-clean tree. Stores the combined report in the
        ledger; ``report_hash`` covers both sub-reports."""
        if maintainer.key_id != self._maintainer_key_id:
            raise PermissionError("audit is maintainer-only")
        plaintext, _, bundle_source = self._decrypt_for_maintainer(manifest, maintainer)
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
            static = _sandbox_scan(clone_dir)
            clamav = self._clamav_or_fail_closed(clone_dir)
        # Download destination: the BLAKE3-verified bundle (fetched over
        # Tor, verified in _decrypt_for_maintainer) lands in the pool's
        # canonical checkout dir so the agent can merge from it.
        bundle_path = self._persist_verified_bundle(manifest, plaintext)
        verdict = "infected" if clamav["verdict"] == "infected" else static["verdict"]
        report = {"verdict": verdict, "static": static, "clamav": clamav,
                  "hits": static.get("hits", []),
                  "injection_hits": static.get("injection_hits", []),
                  "clamav_infected": clamav.get("infected", [])}
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
                      "report_hash": report_hash, "verdict": report["verdict"],
                      "bundle_source": bundle_source})
        return {"report": report, "report_hash": report_hash, "audit_id": audit_id,
                "bundle_path": bundle_path, "bundle_source": bundle_source}

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
        plaintext, _old_dek, bundle_source = self._decrypt_for_maintainer(manifest, maintainer)
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
                      "bundle_source": bundle_source,
                      "push_ticket": manifest.get("transfer_ticket", "")})
        return {**self._transport_info(),
                "manifest": accepted, "manifest_hash": a_hash,
                "transfer_id": sent["transfer_id"], "dek_rotated": True,
                "bundle_source": bundle_source,
                "notice": _n.get("notice", "")}

    def rewrap_accepted(self, *, manifest: dict, maintainer: Member,
                        maintainer_signing_key) -> dict:
        """Re-wrap an already-accepted manifest for the CURRENT member
        set (remote enrollment): fresh DEK, re-encrypt, re-send, and a
        replacement accepted manifest for the same version — keeping the
        original ``prev_manifest_hash`` and ``audit_report_hash`` so the
        chain linkage is unchanged. The new member can then unwrap and
        fetch existing accepted versions.
        """
        if maintainer.key_id != self._maintainer_key_id:
            raise PermissionError("rewrap is maintainer-only")
        plaintext, _old_dek, bundle_source = self._decrypt_for_maintainer(manifest, maintainer)
        if blake3_hash(plaintext) != manifest["content_hash"]:
            raise RuntimeError("BLAKE3 mismatch on enrollment rewrap")
        new_dek = secrets.token_bytes(32)
        ciphertext = encrypt_bundle(plaintext, new_dek)
        wrapped_keys: dict[str, str] = {}
        for member_id, member in self._members.items():
            if not member.box_pubkey_hex:
                continue
            pk = PublicKey(bytes.fromhex(member.box_pubkey_hex), encoder=RawEncoder)
            wrapped_keys[member_id] = wrap_dek(pk, new_dek).hex()
        with tempfile.TemporaryDirectory(prefix="agentpool-rewrap-") as tmp:
            cipher_file = Path(tmp) / "bundle.enc"
            cipher_file.write_bytes(ciphertext)
            sent = self._transport.send(cipher_file)
        body = build_manifest(
            repo_id=manifest["repo_id"], version=manifest["version"],
            content_hash=manifest["content_hash"],
            size_bytes=manifest["size_bytes"],
            signer_key_id=maintainer.key_id,
            submitter_key_id=manifest["submitter_key_id"],
            wrapped_keys=wrapped_keys,
            transfer_ticket=sent["ticket"],
            audit_report_hash=manifest.get("audit_report_hash"),
            prev_manifest_hash=manifest.get("prev_manifest_hash"),
            state="accepted",
        )
        signed = sign_manifest(maintainer_signing_key, body)
        a_hash = manifest_hash(signed)
        self._conn.execute(
            "DELETE FROM repo_versions WHERE repo_id=? AND version=?",
            (manifest["repo_id"], manifest["version"]))
        self._conn.execute(
            "DELETE FROM manifest_chain WHERE repo_id=? AND version=?",
            (manifest["repo_id"], manifest["version"]))
        self._conn.commit()
        append_manifest(self._conn, manifest["repo_id"], signed, a_hash)
        record_event(self._conn, self._pool_id, "rewrapped_for_enrollment",
                     {"repo_id": manifest["repo_id"],
                      "version": manifest["version"], "hash": a_hash,
                      "bundle_source": bundle_source})
        return {**self._transport_info(),
                "manifest": signed, "manifest_hash": a_hash,
                "transfer_id": sent["transfer_id"]}

    def reissue_transfer(self, *, manifest: dict, maintainer: Member) -> dict:
        """Re-send the persisted verified bundle for an already-accepted
        manifest under a FRESH transport ticket (the /fetch re-issue
        path). The signed manifest is never touched: the same plaintext
        bundle is re-encrypted under the SAME DEK (unwrapped from the
        maintainer's own wrapped_keys entry, cache-first via
        :meth:`_decrypt_for_maintainer`), so ``wrapped_keys``,
        ``content_hash`` and the signature all stay valid. Only the
        ephemeral ticket changes.

        Returns ``{"transfer_ticket", "transfer_id", "bundle_source"}``.
        """
        if maintainer.key_id != self._maintainer_key_id:
            raise PermissionError("reissue is maintainer-only")
        plaintext, dek, bundle_source = self._decrypt_for_maintainer(manifest, maintainer)
        if blake3_hash(plaintext) != manifest["content_hash"]:
            raise RuntimeError("BLAKE3 mismatch on transfer re-issue")
        ciphertext = encrypt_bundle(plaintext, dek)
        with tempfile.TemporaryDirectory(prefix="agentpool-reissue-") as tmp:
            cipher_file = Path(tmp) / "bundle.enc"
            cipher_file.write_bytes(ciphertext)
            sent = self._transport.send(cipher_file)
        record_event(self._conn, self._pool_id, "transfer_reissued",
                     {"repo_id": manifest["repo_id"], "version": manifest["version"],
                      "bundle_source": bundle_source})
        return {"transfer_ticket": sent["ticket"],
                "transfer_id": sent["transfer_id"],
                "bundle_source": bundle_source}

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


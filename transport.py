"""Iroh transport for Agent Pool bundle propagation.

Two backends:

- ``FakeTransport`` — in-memory copy used by tests; mirrors the timing
  contract of the real backend (sender must stay alive until fetch).
- ``RealSendHermesTransport`` — drives the ``send_hermes`` CLI (sendme fork
  used by hermes-iroh-interconnect) exactly like its
  ``iroh_send_file``/``iroh_fetch_file`` tools: ``send_hermes send`` runs as
  a provider behind ``script -qefc`` so it survives the tool call, the
  ticket is scraped from its log, and ``send_hermes receive`` extracts into
  an explicit destination directory. The sender provider stays alive until
  ``stop()`` is called (the real flow needs this: unlinking early kills
  the transfer).
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

_TICKET_RE = re.compile(r"(?:sendme receive )([a-z0-9]{20,})|\b(blob[a-z0-9]{40,})\b",
                        re.IGNORECASE)
_HASH_RE = re.compile(r"hash ([0-9a-f]{16,})")
_EXPORT_RE = re.compile(r"exporting to (.+)", re.IGNORECASE)


class TransportUnavailable(Exception):
    """Structured failure after bounded fetch retries (sender offline, etc.).

    Carries ``attempts`` (how many tries were made) and ``last_error`` so
    callers surface a machine-readable failure instead of a raw traceback.
    """

    def __init__(self, ticket: str, attempts: int, last_error: Exception):
        self.ticket = ticket
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"transport unavailable for ticket {ticket!r} after {attempts} "
            f"attempt(s); last error: {last_error}")


def fetch_with_retry(transport, ticket: str, dest: Path, *,
                     max_attempts: int = 3, base_delay: float = 0.5,
                     max_delay: float = 8.0, sleep=time.sleep) -> Path:
    """Fetch *ticket* with bounded exponential backoff.

    The real sender (``send_hermes`` provider) must stay alive during a
    transfer; if it drops mid-fetch the first attempt fails and a retry
    after a short backoff is the correct behaviour (the sender may have
    been briefly unreachable). After ``max_attempts`` failures a
    :class:`TransportUnavailable` is raised — never an unbounded loop.
    Delays double from ``base_delay`` and are capped at ``max_delay``.
    ``sleep`` is injectable so tests run with zero real delay.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    last_error: Exception | None = None
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            return transport.fetch(ticket, dest)
        except Exception as exc:  # sender offline / transient network error
            last_error = exc
            if attempt < max_attempts:
                sleep(min(delay, max_delay))
                delay *= 2
    raise TransportUnavailable(ticket, max_attempts, last_error)


def _relay_flag() -> list[str]:
    relay = os.environ.get("HERMES_IROH_RELAY", "").strip()
    if relay in ("", "default", "n0"):
        return []
    return ["--relay", "disabled" if relay == "off" else relay]


class TransportBackend(ABC):
    """Ticket-based payload transfer. ``send`` returns a bearer ticket."""

    @abstractmethod
    def send(self, path: Path) -> dict:
        """Share *path*; return {"ticket": str, "transfer_id": str, "hash": str|None}."""

    @abstractmethod
    def fetch(self, ticket: str, dest: Path) -> Path:
        """Fetch *ticket* into existing directory *dest*; return the materialised path."""

    @abstractmethod
    def stop(self, transfer_id: str) -> None:
        """Terminate the sender provider for *transfer_id* (best-effort)."""


class FakeTransport(TransportBackend):
    """In-memory transport for tests. Provider lifetime is tracked so tests
    can assert the sender stayed alive until the fetch completed."""

    def __init__(self):
        self._store: dict[str, bytes] = {}
        self._providers: dict[str, dict] = {}
        self._seq = 0

    def send(self, path: Path) -> dict:
        data = Path(path).read_bytes()
        self._seq += 1
        ticket = f"fake-ticket-{self._seq:04d}-{secrets.token_hex(4)}"
        transfer_id = f"fake-transfer-{self._seq:04d}"
        self._store[ticket] = data
        self._providers[transfer_id] = {"ticket": ticket, "alive": True}
        return {"ticket": ticket, "transfer_id": transfer_id, "hash": None}

    def fetch(self, ticket: str, dest: Path) -> Path:
        if ticket not in self._store:
            raise ValueError(f"unknown ticket: {ticket!r}")
        dest = Path(dest)
        out = dest / f"payload-{len(self._store[ticket])}.bin"
        out.write_bytes(self._store[ticket])
        return out

    def stop(self, transfer_id: str) -> None:
        provider = self._providers.get(transfer_id)
        if provider is not None:
            provider["alive"] = False

    def provider_alive(self, transfer_id: str) -> bool:
        p = self._providers.get(transfer_id)
        return bool(p and p["alive"])


class RealSendHermesTransport(TransportBackend):
    """Real ``send_hermes`` CLI backend (same mechanics as the iroh plugin's
    ``iroh_send_file``/``iroh_fetch_file``)."""

    def __init__(self, state_dir: str | Path | None = None):
        env_dir = os.environ.get("HERMES_IROH_STATE_DIR")
        base = Path(state_dir or env_dir or Path.home() / ".hermes" / "iroh-interconnect")
        self._state_dir = base
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._providers: dict[str, dict] = {}

    @staticmethod
    def binary() -> str | None:
        return shutil.which("send_hermes")

    def send(self, path: Path) -> dict:
        binary = self.binary()
        if binary is None:
            raise RuntimeError("send_hermes binary not found on PATH")
        path = Path(path)
        if not path.is_file():
            raise ValueError(f"path is not a regular file: {path}")
        if shutil.which("script") is None:
            raise RuntimeError("sendme requires the 'script' utility to keep its provider TTY alive")
        cmd = [binary, "send", "--no-progress", str(path)] + _relay_flag()
        if os.environ.get("HERMES_IROH_RELAY", "").strip().lower() in {"off", "disabled", "none"}:
            cmd += ["--ticket-type", "addresses"]
        nonce = secrets.token_hex(8)
        log_path = self._state_dir / f"sendme-agentpool-{nonce}.log"
        log_path.touch(mode=0o600)
        import shlex
        script_cmd = " ".join(shlex.quote(part) for part in cmd)
        proc = subprocess.Popen(
            ["script", "-qefc", script_cmd, str(log_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        ticket = None
        content_hash = None
        timeout = max(20, int(os.environ.get("HERMES_IROH_SEND_TIMEOUT", "90")))
        deadline = time.time() + timeout
        while time.time() < deadline:
            output = log_path.read_text(encoding="utf-8", errors="replace")
            for line in output.splitlines():
                h = _HASH_RE.search(line)
                if h and content_hash is None:
                    content_hash = h.group(1)
                t = _TICKET_RE.search(line)
                if t:
                    ticket = t.group(1) or t.group(2)
                    break
            if ticket:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if ticket is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            raise RuntimeError(
                f"send_hermes produced no ticket within {timeout}s (exit {proc.poll()})")
        transfer_id = f"real-transfer-{int(time.time())}-{proc.pid}"
        self._providers[transfer_id] = {"pid": proc.pid, "log": str(log_path), "ticket": ticket}
        return {"ticket": ticket, "transfer_id": transfer_id, "hash": content_hash}

    def fetch(self, ticket: str, dest: Path) -> Path:
        binary = self.binary()
        if binary is None:
            raise RuntimeError("send_hermes binary not found on PATH")
        dest = Path(dest)
        if not dest.is_dir():
            raise ValueError(f"destination is not an existing directory: {dest}")
        before = set(dest.iterdir())
        cmd = [binary, "receive", ticket, "--no-progress"] + _relay_flag()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, cwd=str(dest),
        )
        timeout = int(os.environ.get("HERMES_IROH_FETCH_TIMEOUT", "600"))
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.communicate(timeout=5)
            except Exception:
                proc.kill()
            raise TimeoutError("send_hermes receive timed out")
        except OSError as exc:
            raise RuntimeError(f"failed to run send_hermes receive: {exc}") from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"send_hermes receive failed (exit {proc.returncode}): "
                f"{(stderr or stdout or '')[:500]}")
        output = (stdout or "") + "\n" + (stderr or "")
        matches = _EXPORT_RE.findall(output)
        result = None
        if matches:
            candidate = (dest / matches[-1].strip()).resolve()
            if candidate.exists() and dest.resolve() in candidate.parents:
                result = candidate
        if result is None:
            fresh = [e for e in dest.iterdir() if e not in before and not e.name.startswith(".sendme")]
            if len(fresh) != 1:
                raise RuntimeError("receive exited 0 but the resulting path could not be verified")
            result = fresh[0]
        return result

    def stop(self, transfer_id: str) -> None:
        provider = self._providers.get(transfer_id)
        if provider is None:
            return
        try:
            os.killpg(provider["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        self._providers.pop(transfer_id, None)

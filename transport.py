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

Iroh-over-Tor is the ONLY byte transport (``transport_mode`` config,
default ``tor``; the sole valid value). The ``send_hermes`` sidecar
(option (A), rebuilt with iroh's Tor custom transport,
``iroh-tor-transport``) routes the Iroh connection through Tor hidden
services. The send/receive argv always carries ``--transport tor`` and
tor ports, and emits NO relay flags — there is no relay in the
connection path, and neither peer learns the other's IP. Both endpoints
must run a tor-enabled sidecar (every gateway in a pool); a mixed pool
fails closed. ``tor_ready()`` probes the Tor control port
(``127.0.0.1:<tor_control_port>``, cookie auth off) and the sidecar
binary before every transfer and at service construction.

Historical note: a ``relay`` mode existed in earlier releases and was
removed entirely (see docs/fallback-transport.md, fork-only escape
hatch). It is not a valid mode value, not a config option, and not a
fallback — selecting it raises ``TransportConfigError``. There is also
no direct mode and no way to opt into one.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

_TICKET_RE = re.compile(r"(?:sendme receive )([a-z0-9]{20,})|\b(blob[a-z0-9]{40,})\b",
                        re.IGNORECASE)
_HASH_RE = re.compile(r"hash ([0-9a-f]{16,})")
_EXPORT_RE = re.compile(r"exporting to (.+)", re.IGNORECASE)


class TransportConfigError(ValueError):
    """Invalid transport configuration (unknown mode).

    Structured: carries ``detail`` for the handler to surface as-is.
    """

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


_TOR_UNIT = "agent-pool-tor"
_TOR_SENDHERMES_UNIT = "agent-pool-sendhermes"
_TOR_CONTROL_PORT = 9051
_TOR_SOCKS_PORT = 9055


class TorNotReady(RuntimeError):
    """``tor`` mode selected but the Tor anonymity layer is not ready.

    Fail closed: there is no fallback transport. Carries
    ``detail`` naming the unit/binary to fix (e.g. ``systemctl start
    agent-pool-tor``).
    """

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def tor_ready(*, control_port: int = _TOR_CONTROL_PORT,
              socks_port: int = _TOR_SOCKS_PORT,
              binary: str | None = None,
              timeout: float = 2.0) -> dict:
    """Probe the Tor anonymity layer; raise TorNotReady with an actionable
    fix if it is not usable. There is no fallback transport.

    Checks, in order:
    1. The sidecar binary resolves (``tor_binary`` config or ``send_hermes``
       on PATH) and supports ``--transport tor`` (``send --help``).
    2. The Tor control port answers on 127.0.0.1 with cookie auth off
       (``AUTHENTICATE`` -> ``250 OK``).

    The hidden service itself is created per transfer by the sidecar, so
    there is no long-lived onion identity to check here (Q5 per-transfer
    default). Returns a readiness dict on success.
    """
    resolved = (str(binary).strip() if binary else "") or shutil.which("send_hermes")
    if not resolved or not Path(resolved).is_file():
        raise TorNotReady(
            "tor transport unavailable: no tor-enabled send_hermes sidecar "
            f"found (tor_binary={binary!r}, PATH lookup failed). Build it "
            "with scripts/install-tor.sh (option (A): send_hermes rebuilt "
            "with iroh-tor-transport) and set tor_binary if it is not on "
            "PATH. Start the units: systemctl start "
            f"{_TOR_UNIT}. tor mode fails closed — Iroh-over-Tor is "
            "the only byte transport; there is no relay fallback "
            "(fork the source to change this).")
    try:
        help_out = subprocess.run(
            [resolved, "send", "--help"], capture_output=True, text=True,
            timeout=max(5.0, float(timeout)))
        help_text = (help_out.stdout or "") + "\n" + (help_out.stderr or "")
    except Exception as exc:
        raise TorNotReady(
            f"tor transport unavailable: sidecar {resolved!r} did not "
            f"answer 'send --help' ({exc}). Rebuild it with "
            "scripts/install-tor.sh and check systemctl status "
            f"{_TOR_SENDHERMES_UNIT}.")
    if "--transport" not in help_text:
        raise TorNotReady(
            f"tor transport unavailable: sidecar {resolved!r} does not "
            "support '--transport tor' (this send_hermes was built "
            "without iroh-tor-transport). Rebuild it with "
            "scripts/install-tor.sh (option (A)) or point tor_binary at "
            "a tor-enabled sidecar (option (B): agent-pool-iroh). tor "
            "mode fails closed — there is no fallback transport.")
    try:
        with socket.create_connection(("127.0.0.1", int(control_port)),
                                      timeout=float(timeout)) as sock:
            sock.sendall(b"AUTHENTICATE\r\n")
            reply = sock.recv(1024).decode("utf-8", errors="replace")
    except OSError as exc:
        raise TorNotReady(
            f"tor transport unavailable: Tor control port "
            f"127.0.0.1:{control_port} is not reachable ({exc}). Start "
            f"the Tor daemon: systemctl start {_TOR_UNIT} (torrc: "
            "ops/torrc, ControlPort 127.0.0.1:9051, CookieAuthentication "
            "0). tor mode fails closed — there is no fallback transport.")
    if "250" not in reply:
        raise TorNotReady(
            f"tor transport unavailable: Tor control port "
            f"127.0.0.1:{control_port} refused AUTHENTICATE "
            f"({reply.strip()[:120]!r}); torrc must set "
            "'CookieAuthentication 0' and bind ControlPort to 127.0.0.1 "
            f"only (ops/torrc). Restart: systemctl restart {_TOR_UNIT}.")
    return {"ok": True, "control_port": int(control_port),
            "socks_port": int(socks_port), "binary": resolved,
            "hidden_service": "per-transfer (created by the sidecar)"}


MODE_TOR = "tor"
# The ONLY valid mode. Relay and direct are deliberately absent: there
# is no mode value, fallback, or argv branch that can produce a relay
# or direct (hole-punched) transfer. Forks that want another transport
# must edit this source (see docs/fallback-transport.md).
VALID_MODES = (MODE_TOR,)


def normalize_mode(value, default: str = MODE_TOR) -> str:
    """Normalize/validate a transport_mode config value.

    Empty means the tor default. Any value other than ``tor`` —
    including the historically removed ``relay`` — raises.
    """
    mode = str(value or "").strip().lower() or default
    if mode not in VALID_MODES:
        raise TransportConfigError(
            f"invalid transport_mode {value!r}: expected one of "
            f"{' | '.join(VALID_MODES)}; got {mode!r} "
            "(Iroh-over-Tor is the only byte transport; "
            "fork the source to change this)")
    return mode


def mode_note(mode: str) -> str:
    """User-visible privacy note (echoed in command replies)."""
    if mode == MODE_TOR:
        return ("tor: onion-routed via Iroh Tor custom transport "
                "(sidecar + tor daemon); peer learns no IP, no relay "
                "in the connection path; both endpoints must enable Tor")
    raise TransportConfigError(
        f"invalid transport_mode {mode!r}: expected one of "
        f"{' | '.join(VALID_MODES)}")


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
    can assert the sender stayed alive until the fetch completed.

    In-memory passthrough: accepts ``tor`` so tests can drive the
    tor code path (the Fake never probes a real tor daemon or
    sidecar — tor_ready() is only called by the real backend, and
    the fake backend is test-only / exempt from the tor gate).
    """

    def __init__(self, transport_mode: str = MODE_TOR):
        self.transport_mode = normalize_mode(transport_mode)
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
    ``iroh_send_file``/``iroh_fetch_file``).

    ``transport_mode`` is ``tor`` — the only valid value (``--transport
    tor`` argv; the tor-enabled sidecar routes the Iroh connection
    through Tor hidden services, no relay in the connection path,
    neither peer learns the other's IP; probed by :func:`tor_ready`
    before every transfer, fail closed, there is no fallback).
    """

    def __init__(self, state_dir: str | Path | None = None, *,
                 transport_mode: str = MODE_TOR,
                 tor_control_port: int = _TOR_CONTROL_PORT,
                 tor_socks_port: int = _TOR_SOCKS_PORT,
                 tor_binary: str | None = None):
        self.transport_mode = normalize_mode(transport_mode)
        self.tor_control_port = int(tor_control_port)
        self.tor_socks_port = int(tor_socks_port)
        self.tor_binary = (str(tor_binary).strip() if tor_binary else "") or None
        env_dir = os.environ.get("HERMES_IROH_STATE_DIR")
        base = Path(state_dir or env_dir or Path.home() / ".hermes" / "iroh-interconnect")
        self._state_dir = base
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._providers: dict[str, dict] = {}

    def _send_cmd(self, binary: str, path: Path) -> list[str]:
        """Build the send argv (pure — unit-testable without spawning).

        Always ``--transport tor`` with tor ports, no relay flags at
        all — the sidecar's Tor custom transport is the connection
        path.
        """
        return [binary, "send", "--no-progress", "--transport", "tor",
                "--tor-socks-port", str(self.tor_socks_port),
                "--tor-control-port", str(self.tor_control_port),
                str(path)]

    def _fetch_cmd(self, binary: str, ticket: str) -> list[str]:
        """Build the receive argv (pure — unit-testable without spawning)."""
        return [binary, "receive", ticket, "--no-progress",
                "--transport", "tor",
                "--tor-socks-port", str(self.tor_socks_port),
                "--tor-control-port", str(self.tor_control_port)]

    @staticmethod
    def binary() -> str | None:
        return shutil.which("send_hermes")

    def _resolve_binary(self) -> str | None:
        """``tor_binary`` config (option (B) sidecar) wins over PATH."""
        if self.tor_binary:
            return self.tor_binary
        return self.binary()

    def _require_tor_ready(self) -> None:
        """Fail closed before any tor transfer spawns (raises
        TorNotReady naming the unit to start; no fallback)."""
        tor_ready(control_port=self.tor_control_port,
                  socks_port=self.tor_socks_port,
                  binary=self._resolve_binary())

    def send(self, path: Path) -> dict:
        self._require_tor_ready()
        binary = self._resolve_binary()
        if binary is None:
            raise RuntimeError("send_hermes binary not found on PATH")
        path = Path(path)
        if not path.is_file():
            raise ValueError(f"path is not a regular file: {path}")
        if shutil.which("script") is None:
            raise RuntimeError("sendme requires the 'script' utility to keep its provider TTY alive")
        cmd = self._send_cmd(binary, path)
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
        self._require_tor_ready()
        binary = self._resolve_binary()
        if binary is None:
            raise RuntimeError("send_hermes binary not found on PATH")
        dest = Path(dest)
        if not dest.is_dir():
            raise ValueError(f"destination is not an existing directory: {dest}")
        before = set(dest.iterdir())
        cmd = self._fetch_cmd(binary, ticket)
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

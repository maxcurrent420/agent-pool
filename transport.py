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

Privacy modes (``transport_mode`` config, default ``relay``):

Transfers are ALWAYS relay-only, on both ends. There is no direct mode
and no way to opt into one: the send argv always uses
``--ticket-type relay`` and never emits ``--relay disabled``; the
installed ``send_hermes`` endpoint is built with
``clear_ip_transports()`` + ``addr_filter(AddrFilter::relay_only())`` so
neither endpoint publishes or accepts direct IP addresses. The peer
learns no IP. The relay still sees both IPs + metadata (never plaintext
— QUIC/TLS + NaCl SecretBox); an operator can point both ends at a
self-hosted/trusted relay via ``relay_url`` (global or per-pool) for a
stronger anonymity layer.

- ``relay`` — the only working mode: relay-only ticket, peer learns no IP.
- ``tor`` — accepted as a config value but fails closed: no Tor daemon on
  this host and Iroh's ``proxy_url`` carries HTTP(S) only, so it cannot
  carry QUIC/UDP. Never a half-working path.
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


class TransportConfigError(ValueError):
    """Invalid transport configuration (unknown mode, relay+disabled combo).

    Structured: carries ``detail`` for the handler to surface as-is.
    """

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class TorTransportUnavailable(RuntimeError):
    """``tor`` mode selected but Tor transport is not available — fail closed.

    Tor is not installed on this host and Iroh's ``proxy_url`` carries
    HTTP(S) only (never QUIC/UDP), so a Tor transfer cannot be honoured
    honestly. The error tells the operator to use ``relay`` instead.
    """

    MESSAGE = (
        "tor transport unavailable: no Tor daemon is installed on this host "
        "and Iroh's proxy_url carries HTTP(S) only, so it cannot carry "
        "QUIC/UDP Iroh transfers. Configure transport_mode=relay (the "
        "supported privacy mode: relay-only ticket, peer learns no IP) "
        "instead — tor mode fails closed rather than giving false confidence."
    )

    def __init__(self):
        self.detail = self.MESSAGE
        super().__init__(self.MESSAGE)


MODE_RELAY = "relay"
MODE_TOR = "tor"
# Direct is deliberately absent: there is no mode value, fallback, or
# argv branch that can produce a direct (hole-punched) transfer.
VALID_MODES = (MODE_RELAY, MODE_TOR)

_OFF_VALUES = {"off", "disabled", "none"}
_DEFAULT_RELAY_VALUES = {"", "default", "n0"}


def normalize_mode(value, default: str = MODE_RELAY) -> str:
    """Normalize/validate a transport_mode config value."""
    mode = str(value or "").strip().lower() or default
    if mode not in VALID_MODES:
        raise TransportConfigError(
            f"invalid transport_mode {value!r}: expected one of "
            f"{' | '.join(VALID_MODES)}; got {mode!r}")
    return mode


def mode_note(mode: str) -> str:
    """User-visible privacy note for a mode (echoed in command replies)."""
    if mode == MODE_RELAY:
        return ("relay-only: peer IP not disclosed on either end; "
                "relay sees connection metadata (not plaintext)")
    if mode == MODE_TOR:
        return ("tor: unavailable on this host (fails closed); "
                "use relay for peer-IP privacy")
    return f"transport_mode={mode}"


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


def _relay_flag(relay_url: str | None = None) -> list[str]:
    """Relay argv for the send/receive commands.

    Relay-only, always: a custom relay URL (explicit config beats the
    ``HERMES_IROH_RELAY`` env var) yields ``--relay <url>``; empty /
    default / n0 — and env/config ``off``/``none``/``disabled`` — yield
    no flag (the CLI default relay applies). ``--relay disabled`` is
    NEVER emitted: that would remove the anonymity layer. An explicit
    config relay of ``off`` is rejected in the constructor; the env var
    is a no-op in every mode.
    """
    relay = (str(relay_url).strip() if relay_url is not None
             else os.environ.get("HERMES_IROH_RELAY", "").strip())
    if relay.lower() in _DEFAULT_RELAY_VALUES | _OFF_VALUES:
        return []
    return ["--relay", relay]


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

    def __init__(self, transport_mode: str = MODE_RELAY):
        self.transport_mode = normalize_mode(transport_mode)
        self.relay_url = ""
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

    ``transport_mode`` is ``relay`` (the only working mode: relay-only
    ticket on the sender side — the peer learns no IP — and the
    installed ``send_hermes`` endpoint is relay-only on the receiver
    side too) or ``tor`` (fails closed). ``relay_url`` (explicit, from
    plugin/per-pool config) wins over the ``HERMES_IROH_RELAY`` env var;
    unset → env applies. An explicit relay of ``off``/``disabled``/
    ``none`` is a contradictory config and is rejected in every mode; an
    env ``off`` is a no-op (privacy cannot be silently stripped — see
    ``_pool_status`` warning).
    """

    def __init__(self, state_dir: str | Path | None = None, *,
                 transport_mode: str = MODE_RELAY,
                 relay_url: str | None = None):
        self.transport_mode = normalize_mode(transport_mode)
        self._explicit_relay = (None if relay_url is None
                                else str(relay_url).strip())
        if (self._explicit_relay is not None
                and self._explicit_relay.lower() in _OFF_VALUES):
            raise TransportConfigError(
                "relay-only transport cannot be combined with "
                f"relay_url={self._explicit_relay!r}: a disabled relay "
                "means no anonymity layer and no transport at all. "
                "Unset relay_url or set it to a relay URL "
                "(global or per-pool) to use a self-hosted/trusted relay.")
        env_dir = os.environ.get("HERMES_IROH_STATE_DIR")
        base = Path(state_dir or env_dir or Path.home() / ".hermes" / "iroh-interconnect")
        self._state_dir = base
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._providers: dict[str, dict] = {}

    @property
    def relay_url(self) -> str:
        """Effective relay value: explicit config > env > '' (CLI default)."""
        if self._explicit_relay is not None:
            return self._explicit_relay
        return os.environ.get("HERMES_IROH_RELAY", "").strip()

    def _send_cmd(self, binary: str, path: Path) -> list[str]:
        """Build the send argv (pure — unit-testable without spawning)."""
        if self.transport_mode == MODE_TOR:
            raise TorTransportUnavailable()
        cmd = [binary, "send", "--no-progress", str(path)]
        cmd += _relay_flag(self.relay_url)
        # Always a relay-only ticket: the peer learns no IP. There is no
        # addresses-ticket branch — direct is not constructible.
        cmd += ["--ticket-type", "relay"]
        return cmd

    def _fetch_cmd(self, binary: str, ticket: str) -> list[str]:
        """Build the receive argv (pure — unit-testable without spawning)."""
        if self.transport_mode == MODE_TOR:
            raise TorTransportUnavailable()
        cmd = [binary, "receive", ticket, "--no-progress"]
        cmd += _relay_flag(self.relay_url)
        return cmd

    @staticmethod
    def binary() -> str | None:
        return shutil.which("send_hermes")

    def send(self, path: Path) -> dict:
        if self.transport_mode == MODE_TOR:
            raise TorTransportUnavailable()
        binary = self.binary()
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
        if self.transport_mode == MODE_TOR:
            raise TorTransportUnavailable()
        binary = self.binary()
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

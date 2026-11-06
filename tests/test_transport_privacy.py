"""Transport privacy tests: transport_mode (relay/tor).

Covers the argv evidence for relay mode (relay-only on both ends) and
tor mode (``--transport tor``, no relay flags at all), proof that direct
cannot be selected (rejected at construction; the pure builders can never
emit an addresses ticket or "--relay disabled" under any env/config
combination), the tor fail-closed probe (``tor_ready`` — no silent relay
fallback), config plumbing (plugin config + per-pool override +
tor_control_port/tor_socks_port/tor_binary), pool_status surfacing, the
mode echo in submit results, and a full submit->accept->fetch round trip
in relay mode (crypto/ACL path unchanged — FakeTransport carrier).
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from ledger import add_member, create_pool, init_ledger
from propagation import Member, RepoPropagator
from transport import (
    FakeTransport, RealSendHermesTransport, TorNotReady,
    TransportConfigError, mode_note, normalize_mode, tor_ready,
)
from identity import ensure_identity


# --- argv evidence (pure builders, no subprocess) ---------------------------

def test_relay_mode_uses_relay_ticket(monkeypatch):
    monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    t = RealSendHermesTransport(transport_mode="relay")
    cmd = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    assert cmd[:3] == ["/bin/true", "send", "--no-progress"]
    assert "--ticket-type" in cmd and cmd[cmd.index("--ticket-type") + 1] == "relay"
    assert "addresses" not in cmd
    assert "--relay" not in cmd or cmd[cmd.index("--relay") + 1] != "disabled"


def test_relay_mode_custom_relay_url(monkeypatch):
    monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    t = RealSendHermesTransport(transport_mode="relay",
                                relay_url="https://relay.example.lan")
    cmd = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    assert cmd[cmd.index("--relay") + 1] == "https://relay.example.lan"
    assert cmd[cmd.index("--ticket-type") + 1] == "relay"


def test_relay_mode_never_direct_even_with_env_off(monkeypatch):
    monkeypatch.setenv("HERMES_IROH_RELAY", "off")
    t = RealSendHermesTransport(transport_mode="relay")
    cmd = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    assert cmd[cmd.index("--ticket-type") + 1] == "relay"
    # Env "off" must not become "--relay disabled" NOR "--relay off".
    assert "disabled" not in cmd and "addresses" not in cmd and "off" not in cmd
    fcmd = t._fetch_cmd("/bin/true", "blobticket")
    assert "disabled" not in fcmd


def test_relay_mode_rejects_explicit_relay_off():
    for off in ("off", "none", "disabled"):
        with pytest.raises(TransportConfigError):
            RealSendHermesTransport(transport_mode="relay", relay_url=off)
    # Rejected in every mode, including tor.
    with pytest.raises(TransportConfigError):
        RealSendHermesTransport(transport_mode="tor", relay_url="off")


@pytest.mark.parametrize("mode", ["direct", "wireless", "addresses", "none",
                                  "off", "lan", "holepunch", ""])
def test_direct_mode_cannot_be_selected(mode):
    # Direct is not a config value, not a fallback, not an alias: any
    # mode other than relay/tor — including "direct" itself — is
    # rejected at construction, and "" means the relay default.
    if mode == "":
        assert normalize_mode("") == "relay"
        return
    with pytest.raises(TransportConfigError):
        normalize_mode(mode)
    with pytest.raises(TransportConfigError):
        RealSendHermesTransport(transport_mode=mode)
    with pytest.raises(TransportConfigError):
        FakeTransport(transport_mode=mode)


@pytest.mark.parametrize("env", [None, "", "off", "none", "disabled",
                                 "default", "n0",
                                 "https://relay.example.lan"])
@pytest.mark.parametrize("relay_url", [None, "", "https://relay.example.lan"])
def test_argv_never_contains_direct_branches(monkeypatch, env, relay_url):
    # Sweep every env/config combination: the pure builders can never
    # emit "--relay disabled" or an addresses ticket.
    if env is None:
        monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    else:
        monkeypatch.setenv("HERMES_IROH_RELAY", env)
    t = RealSendHermesTransport(transport_mode="relay", relay_url=relay_url)
    for cmd in (t._send_cmd("/bin/true", Path("/tmp/x.enc")),
                t._fetch_cmd("/bin/true", "blobticket")):
        assert "addresses" not in cmd
        assert "disabled" not in cmd
        if "--relay" in cmd:
            assert cmd[cmd.index("--relay") + 1] not in {
                "disabled", "off", "none", "addresses"}
    send = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    assert send[send.index("--ticket-type") + 1] == "relay"


def _fake_sidecar(tmp_path, *, tor_capable: bool):
    """A fake sidecar binary: answers 'send --help' (with --transport
    only when tor_capable)."""
    script = tmp_path / "fake-send_hermes"
    help_line = "--transport <TRANSPORT>" if tor_capable else "--relay <RELAY>"
    script.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"send --help\" ]; then\n"
        f"  echo 'Usage: fake send [OPTIONS] <PATH>'\n"
        f"  echo '  {help_line}'\n"
        "  exit 0\n"
        "fi\n"
        "echo 'fake sidecar: no tor transfer performed' >&2\n"
        "exit 1\n")
    script.chmod(0o755)
    return str(script)


def test_tor_mode_send_argv(monkeypatch):
    monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    t = RealSendHermesTransport(transport_mode="tor")
    cmd = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    assert cmd[:3] == ["/bin/true", "send", "--no-progress"]
    assert cmd[cmd.index("--transport") + 1] == "tor"
    # Tor mode: NO relay flags at all — the onion path is the transport.
    assert "--relay" not in cmd and "--ticket-type" not in cmd
    assert "addresses" not in cmd and "disabled" not in cmd


def test_tor_mode_fetch_argv(monkeypatch):
    monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    t = RealSendHermesTransport(transport_mode="tor")
    cmd = t._fetch_cmd("/bin/true", "blobticket")
    assert cmd[:3] == ["/bin/true", "receive", "blobticket"]
    assert cmd[cmd.index("--transport") + 1] == "tor"
    assert "--relay" not in cmd
    assert "addresses" not in cmd and "disabled" not in cmd


@pytest.mark.parametrize("env", [None, "", "off",
                                 "https://relay.example.lan"])
@pytest.mark.parametrize("relay_url", [None, "",
                                       "https://relay.example.lan"])
def test_tor_mode_never_emits_relay_flags(monkeypatch, env, relay_url):
    # Even with a relay configured, tor argv never silently drops to
    # relay: no --relay, no --ticket-type, ever.
    if env is None:
        monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    else:
        monkeypatch.setenv("HERMES_IROH_RELAY", env)
    t = RealSendHermesTransport(transport_mode="tor", relay_url=relay_url)
    for cmd in (t._send_cmd("/bin/true", Path("/tmp/x.enc")),
                t._fetch_cmd("/bin/true", "blobticket")):
        assert cmd[cmd.index("--transport") + 1] == "tor"
        assert "--relay" not in cmd and "--ticket-type" not in cmd
        assert "addresses" not in cmd and "disabled" not in cmd


def test_tor_mode_fails_closed_when_probe_fails(monkeypatch, tmp_path):
    # tor_ready() failure (daemon/sidecar not up) must fail closed with
    # the unit name — and the argv must never silently drop the tor flag
    # and use relay instead.
    import transport as transport_mod

    def _boom(**kwargs):
        raise TorNotReady(
            "tor transport unavailable: Tor control port 127.0.0.1:9051 "
            "is not reachable. Start the Tor daemon: systemctl start "
            "agent-pool-tor. tor mode fails closed — it never falls "
            "back to relay.")

    monkeypatch.setattr(transport_mod, "tor_ready", _boom)
    t = RealSendHermesTransport(transport_mode="tor",
                                relay_url="https://relay.example.lan")
    # The pure tor argv still carries the flag (no silent downgrade)...
    send = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    assert send[send.index("--transport") + 1] == "tor"
    # ...but no transfer may spawn while the probe fails.
    with pytest.raises(TorNotReady, match="agent-pool-tor"):
        t.send(Path("/tmp/x.enc"))
    with pytest.raises(TorNotReady, match="agent-pool-tor"):
        t.fetch("blobticket", tmp_path)


def test_tor_ready_missing_binary():
    with pytest.raises(TorNotReady, match="install-tor.sh"):
        tor_ready(binary="/nonexistent/send_hermes")


def test_tor_ready_binary_without_tor_flag(tmp_path):
    binary = _fake_sidecar(tmp_path, tor_capable=False)
    with pytest.raises(TorNotReady, match="install-tor.sh"):
        tor_ready(binary=binary)


def test_tor_ready_control_port_down(tmp_path):
    # Tor-capable binary but no daemon listening: fail closed naming
    # the unit to start.
    binary = _fake_sidecar(tmp_path, tor_capable=True)
    with pytest.raises(TorNotReady, match="agent-pool-tor"):
        tor_ready(binary=binary, control_port=59999, timeout=0.5)


def test_tor_ready_ok(tmp_path):
    # Tor-capable binary + a fake control port answering 250 OK.
    import socket
    import threading

    binary = _fake_sidecar(tmp_path, tor_capable=True)
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _serve():
        conn, _ = server.accept()
        conn.recv(1024)
        conn.sendall(b"250 OK\r\n")
        conn.close()
        server.close()

    threading.Thread(target=_serve, daemon=True).start()
    ready = tor_ready(binary=binary, control_port=port, socks_port=9055)
    assert ready["ok"] is True and ready["control_port"] == port
    assert ready["binary"] == binary


def test_fake_transport_tor_passthrough(tmp_path):
    # FakeTransport accepts tor mode so tests can drive the tor code
    # path in-memory (no daemon/sidecar needed).
    t = FakeTransport(transport_mode="tor")
    src = tmp_path / "payload.bin"
    src.write_bytes(b"tor-bytes")
    sent = t.send(src)
    out = t.fetch(sent["ticket"], tmp_path)
    assert out.read_bytes() == b"tor-bytes"
    assert t.transport_mode == "tor"


def test_mode_note_tor():
    note = mode_note("tor")
    assert "onion-routed" in note and "no relay" in note
    assert "unavailable" not in note
    relay_note = mode_note("relay")
    assert "relay-only" in relay_note


def test_pool_status_tor_mode_surfaces_probe(tmp_path, monkeypatch):
    # tor mode in pool_status: tor block present, readiness probed,
    # failure is fail-closed copy naming the unit (no relay fallback).
    monkeypatch.setattr("transport.tor_ready",
                        lambda **kw: (_ for _ in ()).throw(
                            TorNotReady("tor transport unavailable: "
                                        "systemctl start agent-pool-tor")))
    plugin = _load_plugin()
    status = json.loads(plugin._pool_status(
        ctx=_Ctx({"transport_mode": "tor", "transport": "real"})))
    assert status["transport_mode"] == "tor"
    assert status["tor"]["control_port"] == 9051
    assert status["tor"]["socks_port"] == 9055
    assert status["tor"]["ready"]["ok"] is False
    assert "agent-pool-tor" in status["tor"]["ready"]["error"]
    assert "fails closed" in status["transport_warning"] or "fail closed" in status["transport_warning"]
    assert "onion-routed" in status["transport_privacy"]


def test_invalid_mode_rejected():
    with pytest.raises(TransportConfigError, match="transport_mode"):
        normalize_mode("wireless")
    with pytest.raises(TransportConfigError):
        RealSendHermesTransport(transport_mode="wireless")


# --- config plumbing ---------------------------------------------------------

class _Ctx:
    def __init__(self, cfg):
        self._cfg = cfg

    def get_config(self, key, default=None):
        return self._cfg.get(key, default)


def _load_plugin():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin_priv", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_config_default_is_relay():
    plugin = _load_plugin()
    ctx = _Ctx({"transport": "real"})
    mode, relay, per_pool = plugin._transport_config(ctx)
    assert mode == "relay"
    assert relay == "" and per_pool == {}
    status = json.loads(plugin._pool_status(ctx=ctx))
    assert status["transport_mode"] == "relay"
    assert "transport_warning" not in status


def test_pool_status_warns_env_off_in_relay(monkeypatch):
    monkeypatch.setenv("HERMES_IROH_RELAY", "off")
    plugin = _load_plugin()
    status = json.loads(plugin._pool_status(ctx=_Ctx({"transport_mode": "relay"})))
    assert status["transport_mode"] == "relay"
    assert "HERMES_IROH_RELAY=off" in status["transport_warning"]


def test_per_pool_override_precedence():
    plugin = _load_plugin()
    ctx = _Ctx({"transport_mode": "relay", "relay_url": "",
                "pools": {"pool-custom-relay": {
                                      "transport_mode": "relay",
                                      "relay_url": "https://relay.example.lan"},
                          "pool-same": {"transport_mode": "relay"}}})
    mode, relay, per_pool = plugin._transport_config(ctx)
    assert mode == "relay"
    assert per_pool == {"pool-custom-relay": {
        "transport_mode": "relay",
        "relay_url": "https://relay.example.lan"}}
    # pool-same matches top-level -> no override entry needed.


# --- submit echo + round trip in relay mode ---------------------------------

def _world(tmp_path, transport):
    import subprocess

    def _git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True)

    conn = init_ledger(sqlite3.connect(":memory:"))
    create_pool(conn, "pool-1", "Team", "agent:m-maint")
    identities = {}
    for name in ("m-maint", "b-member"):
        identities[name] = ensure_identity(tmp_path / name)
    members = {}
    for name, ident in identities.items():
        members[ident.key_id] = Member(
            key_id=ident.key_id,
            role="maintainer" if name == "m-maint" else "member",
            verify_key_hex=ident.verify_key.encode(RawEncoder).hex(),
            box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex(),
            box_sk=ident.box_sk)
        add_member(conn, "pool-1", ident.key_id,
                   role="maintainer" if name == "m-maint" else "member",
                   box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex())
    src = tmp_path / "src"
    src.mkdir()
    assert _git("-C", str(src), "init", "-q").returncode == 0
    _git("-C", str(src), "config", "user.email", "t@t.t")
    _git("-C", str(src), "config", "user.name", "T")
    (src / "hello.txt").write_text("hello agent pool\n")
    _git("-C", str(src), "add", ".")
    assert _git("-C", str(src), "commit", "-q", "-m", "v1").returncode == 0
    return {
        "conn": conn, "src": src, "identities": identities,
        "members": members, "transport": transport,
        "prop": RepoPropagator(
            conn, transport, pool_id="pool-1",
            maintainer_key_id=identities["m-maint"].key_id,
            maintainer_signing_key=identities["m-maint"].signing_key,
            members=members),
        "maint": identities["m-maint"], "member": identities["b-member"],
    }


def test_submit_result_carries_mode_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    w = _world(tmp_path, FakeTransport(transport_mode="relay"))
    res = w["prop"].submit(
        repo_path=w["src"], repo_id="demo", version=1,
        submitter=w["members"][w["maint"].key_id],
        submitter_signing_key=w["maint"].signing_key)
    assert res["transport_mode"] == "relay"
    assert "peer IP not disclosed" in res["transport_note"]
    assert res["state"] == "accepted"

    fetched = w["prop"].fetch(
        manifest=res["manifest"],
        fetcher=w["members"][w["member"].key_id],
        dest=tmp_path / "fetch")
    clone_dir = Path(fetched["clone_dir"])
    assert (clone_dir / "hello.txt").read_text() == "hello agent pool\n"


def test_submit_result_direct_note_rejected(tmp_path):
    # There is no direct submit path: constructing the carrier with a
    # direct mode fails before any bytes move.
    with pytest.raises(TransportConfigError):
        FakeTransport(transport_mode="direct")

"""Transport privacy tests: Iroh-over-Tor is the only byte transport.

Covers argv evidence (always ``--transport tor``, no relay flags ever),
proof that relay/direct cannot be selected (rejected at construction),
the tor fail-closed probe (``tor_ready`` — no fallback), config plumbing
(plugin default tor, legacy relay ignored with migration warning,
per-pool overrides ignored, tor ports/binary), pool_status surfacing,
the mode echo in submit results, and a full submit->accept->fetch
round trip (crypto/ACL path unchanged — FakeTransport carrier).
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
    TransportConfigError, VALID_MODES, mode_note, normalize_mode, tor_ready,
)
from identity import ensure_identity


# --- argv evidence (pure builders, no subprocess) ---------------------------

def test_only_tor_is_a_valid_mode():
    assert VALID_MODES == ("tor",)
    assert normalize_mode("") == "tor"
    assert normalize_mode("tor") == "tor"
    assert normalize_mode(" TOR ") == "tor"


@pytest.mark.parametrize("mode", ["relay", "direct", "wireless", "addresses",
                                  "none", "off", "lan", "holepunch"])
def test_relay_and_direct_cannot_be_selected(mode):
    # Relay/direct are not config values or fallbacks: rejected at
    # construction by normalize_mode and both transports.
    with pytest.raises(TransportConfigError):
        normalize_mode(mode)
    with pytest.raises(TransportConfigError):
        RealSendHermesTransport(transport_mode=mode)
    with pytest.raises(TransportConfigError):
        FakeTransport(transport_mode=mode)


def test_relay_url_kwarg_is_gone():
    # No relay option remains: passing relay_url is a TypeError.
    with pytest.raises(TypeError):
        RealSendHermesTransport(transport_mode="tor", relay_url="https://relay.example.lan")
    with pytest.raises(TypeError):
        FakeTransport(transport_mode="tor", relay_url="https://relay.example.lan")
    assert not hasattr(FakeTransport(), "relay_url")
    assert not hasattr(RealSendHermesTransport(), "relay_url")


def test_tor_mode_send_argv(monkeypatch):
    monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    t = RealSendHermesTransport(transport_mode="tor")
    cmd = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    assert cmd[:3] == ["/bin/true", "send", "--no-progress"]
    assert cmd[cmd.index("--transport") + 1] == "tor"
    assert "--relay" not in cmd and "--ticket-type" not in cmd
    assert "addresses" not in cmd and "disabled" not in cmd


def test_tor_mode_fetch_argv(monkeypatch):
    monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    t = RealSendHermesTransport(transport_mode="tor",
                                tor_socks_port=9055, tor_control_port=9051)
    cmd = t._fetch_cmd("/bin/true", "blobticket")
    assert cmd[:3] == ["/bin/true", "receive", "blobticket"]
    assert cmd[cmd.index("--transport") + 1] == "tor"
    assert cmd[cmd.index("--tor-socks-port") + 1] == "9055"
    assert cmd[cmd.index("--tor-control-port") + 1] == "9051"
    assert "--relay" not in cmd
    assert "addresses" not in cmd and "disabled" not in cmd


@pytest.mark.parametrize("env", [None, "", "off",
                                 "https://relay.example.lan"])
def test_tor_mode_never_emits_relay_flags(monkeypatch, env):
    # Even with HERMES_IROH_RELAY set, tor argv never emits relay flags.
    if env is None:
        monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    else:
        monkeypatch.setenv("HERMES_IROH_RELAY", env)
    t = RealSendHermesTransport(transport_mode="tor")
    for cmd in (t._send_cmd("/bin/true", Path("/tmp/x.enc")),
                t._fetch_cmd("/bin/true", "blobticket")):
        assert cmd[cmd.index("--transport") + 1] == "tor"
        assert "--relay" not in cmd and "--ticket-type" not in cmd
        assert "addresses" not in cmd and "disabled" not in cmd


def _fake_sidecar(tmp_path, *, tor_capable: bool):
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


def test_tor_mode_fails_closed_when_probe_fails(monkeypatch, tmp_path):
    import transport as transport_mod

    def _boom(**kwargs):
        raise TorNotReady(
            "tor transport unavailable: Tor control port 127.0.0.1:9051 "
            "is not reachable. Start the Tor daemon: systemctl start "
            "agent-pool-tor.")

    monkeypatch.setattr(transport_mod, "tor_ready", _boom)
    t = RealSendHermesTransport(transport_mode="tor")
    send = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    assert send[send.index("--transport") + 1] == "tor"
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
    binary = _fake_sidecar(tmp_path, tor_capable=True)
    with pytest.raises(TorNotReady, match="agent-pool-tor"):
        tor_ready(binary=binary, control_port=59999, timeout=0.5)


def test_tor_ready_ok(tmp_path):
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
    with pytest.raises(TransportConfigError):
        mode_note("relay")


def test_pool_status_tor_mode_surfaces_probe(monkeypatch):
    monkeypatch.setattr("transport.tor_ready",
                        lambda **kw: (_ for _ in ()).throw(
                            TorNotReady("tor transport unavailable: "
                                        "systemctl start agent-pool-tor")))
    plugin = _load_plugin()
    status = json.loads(plugin._pool_status(
        ctx=_Ctx({"transport": "fake"})))
    assert status["transport_mode"] == "tor"
    assert status["tor"]["control_port"] == 9051
    assert status["tor"]["socks_port"] == 9055
    assert status["tor"]["ready"]["ok"] is False
    assert "agent-pool-tor" in status["tor"]["ready"]["error"]
    assert "fail" in status["transport_warning"]
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


def test_config_default_is_tor():
    plugin = _load_plugin()
    ctx = _Ctx({"transport": "fake"})
    mode, relay, per_pool = plugin._transport_config(ctx)
    assert mode == "tor"
    assert relay == "" and per_pool == {}
    status = json.loads(plugin._pool_status(ctx=ctx))
    assert status["transport_mode"] == "tor"
    assert status["relay_url"] == ""


def test_legacy_relay_config_ignored_with_migration_warning():
    plugin = _load_plugin()
    ctx = _Ctx({"transport": "fake", "transport_mode": "relay",
                "pools": {"pool-old": {"transport_mode": "relay"}}})
    mode, relay, per_pool = plugin._transport_config(ctx)
    assert mode == "tor" and relay == "" and per_pool == {}
    status = json.loads(plugin._pool_status(ctx=ctx))
    assert status["transport_mode"] == "tor"
    assert "migration_warning" in status
    assert "<global>" in status["migration_warning"]
    assert "pool-old" in status["migration_warning"]


def test_per_pool_overrides_ignored():
    plugin = _load_plugin()
    ctx = _Ctx({"transport_mode": "relay", "relay_url": "",
                "pools": {"pool-custom": {
                                      "transport_mode": "relay",
                                      "relay_url": "https://relay.example.lan"},
                          "pool-same": {"transport_mode": "relay"}}})
    mode, relay, per_pool = plugin._transport_config(ctx)
    assert mode == "tor"
    assert per_pool == {}
    assert relay == ""


def test_invalid_plugin_mode_rejected():
    plugin = _load_plugin()
    ctx = _Ctx({"transport_mode": "direct"})
    with pytest.raises(TransportConfigError):
        plugin._transport_config(ctx)


# --- submit echo + round trip ------------------------------------------------

def _world(tmp_path, transport):
    import subprocess

    def _git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True)

    conn = init_ledger(sqlite3.connect(":memory:"))
    create_pool(conn, "pool-1", "Team", "agent:m-maint")
    # prop.submit appends repo_versions (FK repo_id -> repos), so the
    # parent repo row must exist first — mirroring the production
    # PoolService.add_repo (tickets.py) which upserts before submit,
    # and test_propagation's world which inserts the row directly.
    conn.execute(
        "INSERT INTO repos (repo_id, pool_id, name, created_at)"
        " VALUES ('demo','pool-1','Demo',0)")
    conn.commit()
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
    w = _world(tmp_path, FakeTransport(transport_mode="tor"))
    res = w["prop"].submit(
        repo_path=w["src"], repo_id="demo", version=1,
        submitter=w["members"][w["maint"].key_id],
        submitter_signing_key=w["maint"].signing_key)
    assert res["transport_mode"] == "tor"
    assert "onion-routed" in res["transport_note"]
    assert res["state"] == "accepted"

    fetched = w["prop"].fetch(
        manifest=res["manifest"],
        fetcher=w["members"][w["member"].key_id],
        dest=tmp_path / "fetch")
    clone_dir = Path(fetched["clone_dir"])
    assert (clone_dir / "hello.txt").read_text() == "hello agent pool\n"


def test_submit_result_direct_note_rejected(tmp_path):
    with pytest.raises(TransportConfigError):
        FakeTransport(transport_mode="direct")

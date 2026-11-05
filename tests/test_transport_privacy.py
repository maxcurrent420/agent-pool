"""Transport privacy tests: transport_mode (relay/direct/tor).

Covers the argv evidence for each mode, the tor fail-closed error, config
plumbing (plugin config + per-pool override), pool_status surfacing, the
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
    FakeTransport, RealSendHermesTransport, TorTransportUnavailable,
    TransportConfigError, normalize_mode,
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
    with pytest.raises(TransportConfigError, match="transport_mode=relay"):
        RealSendHermesTransport(transport_mode="relay", relay_url="off")


def test_direct_mode_preserves_default_ticket(monkeypatch):
    monkeypatch.delenv("HERMES_IROH_RELAY", raising=False)
    t = RealSendHermesTransport(transport_mode="direct")
    cmd = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    # No --ticket-type: inherits CLI default RelayAndAddresses (today's behaviour).
    assert "--ticket-type" not in cmd
    assert "--relay" not in cmd


def test_direct_mode_env_off_is_addresses(monkeypatch):
    monkeypatch.setenv("HERMES_IROH_RELAY", "off")
    t = RealSendHermesTransport(transport_mode="direct")
    cmd = t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    assert cmd[cmd.index("--relay") + 1] == "disabled"
    assert cmd[cmd.index("--ticket-type") + 1] == "addresses"


def test_tor_mode_fails_closed():
    t = RealSendHermesTransport(transport_mode="tor")
    with pytest.raises(TorTransportUnavailable, match="tor transport unavailable"):
        t._send_cmd("/bin/true", Path("/tmp/x.enc"))
    with pytest.raises(TorTransportUnavailable, match="transport_mode=relay"):
        t._fetch_cmd("/bin/true", "blobticket")
    # The error must name the prerequisite and the supported fallback.
    try:
        t.send(Path("/tmp/x.enc"))
        raise AssertionError("send must fail closed")
    except TorTransportUnavailable as exc:
        assert "proxy_url" in str(exc) and "relay" in str(exc)


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
                "pools": {"pool-lan": {"transport_mode": "direct",
                                       "relay_url": "off"},
                          "pool-same": {"transport_mode": "relay"}}})
    mode, relay, per_pool = plugin._transport_config(ctx)
    assert mode == "relay"
    assert per_pool == {"pool-lan": {"transport_mode": "direct",
                                     "relay_url": "off"}}
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


def test_submit_result_direct_note(tmp_path):
    w = _world(tmp_path, FakeTransport(transport_mode="direct"))
    res = w["prop"].submit(
        repo_path=w["src"], repo_id="demo", version=1,
        submitter=w["members"][w["maint"].key_id],
        submitter_signing_key=w["maint"].signing_key)
    assert res["transport_mode"] == "direct"
    assert "peer IP exposed" in res["transport_note"]

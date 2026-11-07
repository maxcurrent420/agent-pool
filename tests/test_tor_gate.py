"""Tor hard gate: service construction, plugin registration, pool
registration fail closed when Tor is not ready (real backend only;
fake backend is test-only and exempt). No live Discord / no real Tor
daemon required — tor_ready is monkeypatched.
"""
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ledger as L
from identity import ensure_identity
from tickets import PoolService
from transport import FakeTransport, RealSendHermesTransport, TorNotReady


def _load_plugin():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin_gate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    def __init__(self, cfg):
        self._cfg = cfg
        self.tools, self.commands = {}, {}

    def get_config(self, key, default=None):
        return self._cfg.get(key, default)

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = handler

    def register_command(self, name, handler, description="", args_hint="",
                         argument_mode=None):
        self.commands[name] = handler


def _boom(**kwargs):
    raise TorNotReady(
        "tor transport unavailable: Tor control port 127.0.0.1:9051 "
        "is not reachable. Start the Tor daemon: systemctl start "
        "agent-pool-tor.")


def test_service_for_real_backend_gates_on_tor(monkeypatch, tmp_path):
    # _service_for with transport=real must probe tor_ready before
    # building the real transport and raise TorNotReady naming the unit.
    import transport as transport_mod
    monkeypatch.setattr(transport_mod, "tor_ready", _boom)
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()
    # plugin imports transport as `transport` (flat) — patch both names
    monkeypatch.setattr("transport.tor_ready", _boom, raising=False)
    with pytest.raises(TorNotReady, match="agent-pool-tor"):
        plugin._service_for(_Ctx({"transport": "real"}))


def test_service_for_fake_backend_exempt(monkeypatch, tmp_path):
    import transport as transport_mod
    monkeypatch.setattr(transport_mod, "tor_ready", _boom)
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()
    svc = plugin._service_for(_Ctx({"transport": "fake"}))
    assert svc is not None
    assert isinstance(svc._transport, FakeTransport)


def test_plugin_register_real_backend_raises_on_tor_down(monkeypatch):
    import transport as transport_mod
    monkeypatch.setattr(transport_mod, "tor_ready", _boom)
    monkeypatch.setattr("transport.tor_ready", _boom, raising=False)
    plugin = _load_plugin()
    with pytest.raises(TorNotReady, match="agent-pool-tor"):
        plugin.register(_Ctx({"transport": "real"}))


def test_plugin_register_fake_backend_ok(monkeypatch):
    plugin = _load_plugin()
    ctx = _Ctx({"transport": "fake"})
    plugin.register(ctx)
    assert "pool_status" in ctx.tools and "pool" in ctx.commands


def test_pool_status_names_unit_when_tor_down(monkeypatch):
    monkeypatch.setattr("transport.tor_ready", _boom, raising=False)
    plugin = _load_plugin()
    status = json.loads(plugin._pool_status(ctx=_Ctx({"transport": "fake"})))
    assert status["ok"] is True
    assert status["transport_mode"] == "tor"
    assert status["tor"]["ready"]["ok"] is False
    assert "agent-pool-tor" in status["tor"]["ready"]["error"]
    assert "agent-pool-tor" in status["transport_warning"]
    assert "no relay" in status["transport_privacy"].lower() or "no relay" in status["transport_privacy"]


def _svc_with_real_transport(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn = L.init_ledger(conn)
    ident = ensure_identity(Path(tmp_path) / "m")
    transport = RealSendHermesTransport(transport_mode="tor")
    svc = PoolService(conn, transport, caller_key_id=ident.key_id,
                      caller_signing_key=ident.signing_key,
                      caller_box_sk=ident.box_sk,
                      maintainer_discord_user_id="owner-1")
    return conn, svc


def test_register_pool_refuses_without_tor(tmp_path, monkeypatch):
    import transport as transport_mod
    monkeypatch.setattr(transport_mod, "tor_ready", _boom)
    _conn, svc = _svc_with_real_transport(tmp_path)
    with pytest.raises(TorNotReady, match="agent-pool-tor"):
        svc.register_pool("pool-1", "Team")
    # Nothing was registered.
    assert L.list_local_pools(_conn) == []


def test_register_pool_fake_transport_exempt(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn = L.init_ledger(conn)
    ident = ensure_identity(Path(tmp_path) / "m")
    svc = PoolService(conn, FakeTransport(), caller_key_id=ident.key_id,
                      caller_signing_key=ident.signing_key,
                      caller_box_sk=ident.box_sk,
                      maintainer_discord_user_id="owner-1")
    out = svc.register_pool("pool-1", "Team")
    assert out["ok"] is True
    assert L.list_local_pools(conn) == ["pool-1"]


def test_transport_overrides_always_empty():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn = L.init_ledger(conn)
    svc = PoolService(conn, FakeTransport(), caller_key_id="agent:x",
                      transport_overrides={"pool-1": FakeTransport()})
    assert svc._transport_overrides == {}

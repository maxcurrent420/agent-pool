import importlib.util
import json
import os
import stat
import sys

import pytest


def _load_plugin():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self):
        self.tools, self.commands, self.config = {}, {}, {"transport": "fake"}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = handler

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler

    def register_platform_handler(self, platform, factory):
        pass

    def register_hook(self, name, handler):
        pass

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_register_wires_tool_and_command():
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    assert "pool_status" in ctx.tools
    assert "pool" in ctx.commands
    out = json.loads(ctx.tools["pool_status"]({}))
    assert out["ok"] is True and out["transport_mode"] == "tor"
    assert out["relay_url"] == ""
    assert set(out["budgets"]) == {"max_turns", "max_tasks_per_day",
                                   "token_cap", "wall_clock_seconds"}


def test_pool_status_reports_version_not_phase():
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    out = json.loads(ctx.tools["pool_status"]({}))
    assert out["version"] == "2.7.0"
    assert "phase" not in out


def test_identity_generated_once_and_private(tmp_path):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from identity import ensure_identity, load_identity

    first = ensure_identity(tmp_path)
    second = ensure_identity(tmp_path)
    assert first.key_id == second.key_id
    assert first.key_id.startswith("agent:")
    loaded = load_identity(tmp_path)
    assert loaded.verify_key.encode().hex() == first.verify_key.encode().hex()
    mode = stat.S_IMODE(os.stat(tmp_path / "identity.json").st_mode)
    assert mode == 0o600


# -- Phase 0: maintainer id from config/env, no --discord-user flag --------

def _svc(tmp_path, **kw):
    import sqlite3
    from pathlib import Path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from identity import ensure_identity
    import ledger as L
    from tickets import PoolService
    from transport import FakeTransport
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn = L.init_ledger(conn)
    ident = ensure_identity(Path(tmp_path) / "m")
    svc = PoolService(conn, FakeTransport(), caller_key_id=ident.key_id,
                      caller_signing_key=ident.signing_key,
                      caller_box_sk=ident.box_sk, **kw)
    return conn, svc


def test_register_requires_maintainer_config(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", raising=False)
    _conn, svc = _svc(tmp_path)
    with pytest.raises(ValueError) as exc:
        svc.register_pool("pool-1", "Team")
    assert str(exc.value) == (
        "register needs maintainer_discord_user_id: set it in config or "
        "AGENT_POOL_MAINTAINER_DISCORD_USER_ID")


def test_register_uses_config_maintainer(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", raising=False)
    conn, svc = _svc(tmp_path, maintainer_discord_user_id="owner-1")
    out = svc.register_pool("pool-1", "Team")
    assert out["ok"] is True and out["owner_discord_user_id"] == "owner-1"
    row = conn.execute(
        "SELECT owner_discord_user_id FROM pools WHERE pool_id='pool-1'").fetchone()
    assert row[0] == "owner-1"


def test_register_uses_env_maintainer(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", "env-owner")
    _conn, svc = _svc(tmp_path, maintainer_discord_user_id="config-owner")
    # env beats config at service construction
    assert svc._maintainer_discord_user_id == "env-owner"
    out = svc.register_pool("pool-1", "Team")
    assert out["owner_discord_user_id"] == "env-owner"


def test_maintainer_helper_env_beats_config(monkeypatch):
    plugin = _load_plugin()
    ctx = FakeCtx()
    ctx.config["maintainer_discord_user_id"] = "cfg-1"
    monkeypatch.delenv("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", raising=False)
    assert plugin._maintainer_discord_user_id(ctx) == "cfg-1"
    monkeypatch.setenv("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", "env-1")
    assert plugin._maintainer_discord_user_id(ctx) == "env-1"

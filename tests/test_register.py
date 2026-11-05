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
        self.tools, self.commands, self.config = {}, {}, {"relay_url": "off"}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = handler

    def register_command(self, name, handler, description=""):
        self.commands[name] = handler

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_register_wires_tool_and_command():
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    assert "pool_status" in ctx.tools
    assert "pool" in ctx.commands
    out = json.loads(ctx.tools["pool_status"]({}))
    assert out["ok"] is True and out["relay_url"] == "off"
    assert set(out["budgets"]) == {"max_turns", "max_tasks_per_day",
                                   "token_cap", "wall_clock_seconds"}


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

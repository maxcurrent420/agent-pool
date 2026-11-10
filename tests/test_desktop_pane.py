"""Desktop pane backend tests (Task 4): the read-only dashboard
router and the pool_pane_report sidecar tool.

Task 10 extends this file.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_plugin():
    path = str(ROOT / "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin_pane", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_api():
    path = str(ROOT / "dashboard" / "plugin_api.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin_api", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self, config=None):
        self.tools, self.commands, self.hooks = {}, {}, {}
        self.config = {"transport": "fake"}
        if config:
            self.config.update(config)

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = handler

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_pane_report_registered_and_in_provides_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    import yaml
    data = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert "pool_pane_report" in ctx.tools
    assert set(ctx.tools) == set(data["provides_tools"])
    assert len(data["provides_tools"]) == 3


def test_pane_report_writes_sidecar_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    out = json.loads(ctx.tools["pool_pane_report"](
        {"op_id": "op-1", "kind": "claim", "status": "done", "detail": "ok"}))
    assert out["ok"] is True
    sidecar = tmp_path / "data" / "pane-state.json"
    state = json.loads(sidecar.read_text())
    assert state["ops"][0]["op_id"] == "op-1"
    assert state["ops"][0]["status"] == "done"
    assert "updated_at" in state and "pool_status" in state
    # Tool never mutates ledger *contents*; the snapshot read path is the
    # same one pool_status uses (service construction may create the
    # sqlite file, as it does for pool_status).


def test_pane_report_upserts_and_never_raises_on_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    ctx.tools["pool_pane_report"]({"op_id": "op-1", "kind": "claim", "status": "submitted"})
    ctx.tools["pool_pane_report"]({"op_id": "op-1", "kind": "claim", "status": "done"})
    out = json.loads(ctx.tools["pool_pane_report"](
        {"op_id": "", "kind": "", "status": ""}))
    assert out["ok"] is False
    state = json.loads((tmp_path / "data" / "pane-state.json").read_text())
    assert len(state["ops"]) == 1 and state["ops"][0]["status"] == "done"


def test_status_route_returns_dict_on_empty_state(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    api = _load_api()
    result = api.status()
    assert isinstance(result, dict)
    assert result.get("ok") is True
    assert result["plugin"] == "agent-pool"


def test_pane_state_route_returns_dict_on_missing_sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    api = _load_api()
    result = api.pane_state()
    assert isinstance(result, dict)
    assert result["ops"] == [] and result["pool_status"] == {}
    assert "updated_at" in result


def test_no_mutating_routes():
    api = _load_api()
    methods = set()
    for route in api.router.routes:
        methods |= set(getattr(route, "methods", set()))
    assert methods <= {"GET"}
    assert "POST" not in methods and "PUT" not in methods and "DELETE" not in methods

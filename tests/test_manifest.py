"""Manifest tests: strict YAML, version shape, and the registration
surface (registered == declared) — the plugin's single most valuable
test. (Crypto/manifest-chain tests live in test_crypto_manifest.py.)
"""
import importlib.util
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / "plugin.yaml"


class _StrictLoader(yaml.SafeLoader):
    """YAML loader that raises on duplicate keys (PyYAML keeps the last)."""


def _construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _load_manifest_strict():
    return yaml.load(MANIFEST.read_text(encoding="utf-8"), Loader=_StrictLoader)


def _load_plugin():
    path = str(ROOT / "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin_manifest", path)
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


def test_manifest_strict_yaml_no_duplicate_keys():
    data = _load_manifest_strict()
    assert data["name"] == "agent-pool"


def test_manifest_version_is_string():
    data = _load_manifest_strict()
    assert isinstance(data["version"], str)
    assert data["version"] == "2.8.0"


def test_manifest_declares_requires_env():
    data = _load_manifest_strict()
    env = data.get("requires_env") or []
    assert env, "plugin.yaml must declare requires_env"
    for entry in env:
        name = entry if isinstance(entry, str) else entry.get("name", "")
        assert name and name.upper() == name, f"requires_env entry {entry!r} not UPPER_SNAKE"


def test_registered_tools_match_provides_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    data = _load_manifest_strict()
    assert set(ctx.tools) == set(data["provides_tools"])
    assert set(ctx.hooks) == set(data["provides_hooks"])


def test_registered_commands_match_provides_commands(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    data = _load_manifest_strict()
    assert set(ctx.commands) == set(data["provides_commands"])
    assert len(ctx.commands) == 10


def test_register_loads_with_tor_down(monkeypatch):
    """H1: register() is pure wiring — real backend + tor down still
    wires every tool/command (the Tor gate is lazy, at first use)."""
    import transport as transport_mod

    def _boom(**kwargs):
        raise transport_mod.TorNotReady("tor transport unavailable: agent-pool-tor")

    monkeypatch.setattr(transport_mod, "tor_ready", _boom)
    plugin = _load_plugin()
    ctx = FakeCtx({"transport": "real"})
    plugin.register(ctx)
    assert set(ctx.tools) == {"pool_status", "pool_report_usage", "pool_pane_report"}
    assert len(ctx.commands) == 10


def test_tool_error_shape_has_code_and_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    out = json.loads(ctx.tools["pool_report_usage"]({}))
    assert out["ok"] is False
    assert out["code"] == "missing_argument"
    assert out["fix"]


def test_guard_fails_closed_when_service_raises(monkeypatch, tmp_path):
    """H4: a raising _service_for must refuse, never run the handler."""
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()

    def _boom(ctx, source=None):
        raise RuntimeError("synthetic service failure")

    monkeypatch.setattr(plugin, "_service_for", _boom)
    ctx = FakeCtx()
    plugin.register(ctx)
    out = ctx.commands["pool"]("list", source={"user_id": "u1"})
    assert "guard_unavailable" in out
    assert "fix" in out


def test_pool_status_version_matches_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    out = json.loads(ctx.tools["pool_status"]({}))
    data = _load_manifest_strict()
    assert out["version"] == data["version"]

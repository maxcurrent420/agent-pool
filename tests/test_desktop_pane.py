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


# ---------------------------------------------------------------- Task 10
import re
import shutil
import subprocess

DESKTOP = ROOT / "desktop"
DASHBOARD = ROOT / "dashboard"

SDK_ALLOWLIST = {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
DOCUMENTED_COMMANDS = {
    "pool", "fetch", "task", "add-repo", "task_add", "task_list",
    "claim", "release", "push", "audit",
}
DOCUMENTED_SCRIPTS = {
    "scripts/contributor-add.py",
    "scripts/contributor-claim.py",
    "scripts/contributor-push.py",
    "scripts/pool-approve.py",
}


def test_artifacts_exist_in_repo():
    for rel in ["desktop/plugin.js",
                "desktop/ops-manifest.json",
                "dashboard/plugin_api.py", "dashboard/manifest.json"]:
        assert (ROOT / rel).is_file(), rel
    for rel in ["desktop/prompts.js", "desktop/track.js"]:
        assert not (ROOT / rel).exists(), f"{rel} must be inlined into plugin.js"


def test_installed_tree_artifacts_optional():
    installed = Path.home() / ".hermes" / "plugins" / "agent-pool"
    if not installed.is_dir():
        pytest.skip("installed tree not present")
    for rel in ["desktop/plugin.js",
                "desktop/ops-manifest.json",
                "dashboard/plugin_api.py", "dashboard/manifest.json"]:
        assert (installed / rel).is_file(), rel


def _import_specifiers(path):
    src = path.read_text()
    specs = re.findall(r"""import\s+(?:[^'"]*\s+from\s+)?['"]([^'"]+)['"]""", src)
    specs += re.findall(r"""import\(\s*['"]([^'"]+)['"]\s*\)""", src)
    return specs


def test_plugin_js_import_allowlist():
    specs = _import_specifiers(DESKTOP / "plugin.js")
    assert specs, "expected imports in plugin.js"
    for spec in specs:
        assert spec in SDK_ALLOWLIST, \
            f"disallowed import specifier: {spec}"


def test_plugin_js_no_relative_imports():
    src = (DESKTOP / "plugin.js").read_text()
    assert not re.search(r"""(?:from|import)\s+['"]\.""", src), \
        "plugin.js must contain zero relative imports"


def _scan_sources():
    files = list(DESKTOP.glob("*.js")) + list(DASHBOARD.glob("*.py")) + list(DASHBOARD.glob("*.json"))
    for f in files:
        yield f, f.read_text()


def test_forbidden_patterns_zero_matches():
    patterns = ["eval(", "new Function", ".prototype", "document.", "window.__",
                "__HERMES_PLUGIN_SDK__", "data.db", "PoolService", "register_command"]
    for f, src in _scan_sources():
        for pat in patterns:
            assert pat not in src, f"{f}: forbidden pattern {pat!r}"


def test_plugin_api_no_mutating_routes_no_ledger_strings():
    src = (DASHBOARD / "plugin_api.py").read_text()
    for pat in ["@router.post", "@router.put", "@router.delete", "@router.patch",
                "data.db", "/plugin-data/", ".hermes/plugin-data", "agent-pool/data"]:
        assert pat not in src, pat


def test_desktop_no_raw_fetch():
    for f in DESKTOP.glob("*.js"):
        src = f.read_text()
        for m in re.finditer(r"(?<![\w.])fetch\(", src):
            line = src[:m.start()].count("\n") + 1
            ctx = src[max(0, m.start() - 80):m.start()]
            assert "ctx.rest" in ctx or "host." in ctx, f"{f}:{line}: raw fetch("


def test_fake_ctx_tools_and_commands_parity():
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    import yaml
    data = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert set(ctx.tools) == set(data["provides_tools"]) == {
        "pool_status", "pool_report_usage", "pool_pane_report"}
    assert set(ctx.commands) == set(data["provides_commands"]) == DOCUMENTED_COMMANDS
    assert len(ctx.commands) == 10


def test_pane_state_route_roundtrip_empty_and_populated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    api = _load_api()
    # Empty: no sidecar written yet.
    empty = api.pane_state()
    assert empty["ops"] == [] and empty["pool_status"] == {}
    # Populated: write via the real tool, read back through the route shape.
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    ctx.tools["pool_pane_report"](
        {"op_id": "op-rt", "kind": "push", "status": "done", "detail": "ok"})
    populated = api.pane_state()
    assert [o["op_id"] for o in populated["ops"]] == ["op-rt"]
    assert populated["ops"][0]["kind"] == "push"
    assert "updated_at" in populated and "pool_status" in populated


def _load_manifest():
    return json.loads((DESKTOP / "ops-manifest.json").read_text())


def test_ops_manifest_shape_and_kinds():
    manifest = _load_manifest()
    ops = manifest["operations"]
    kinds = [op["kind"] for op in ops]
    assert kinds == ["add-repo-owner", "add-repo-link", "fetch-tasks", "claim",
                     "pull", "push", "approve", "merge"]
    for op in ops:
        assert set(op) == {"kind", "commands", "script", "fields"}
        assert isinstance(op["commands"], list) and isinstance(op["fields"], list)


def test_ops_manifest_within_documented_surface():
    for op in _load_manifest()["operations"]:
        for cmd in op["commands"]:
            m = re.match(r"^/([a-z-]+)", cmd)
            assert m, cmd
            assert m.group(1) in DOCUMENTED_COMMANDS, cmd
        if op["script"] is not None:
            assert op["script"] in DOCUMENTED_SCRIPTS, op["script"]
            assert (ROOT / op["script"]).is_file(), op["script"]


def test_ops_manifest_to_prompts_parity():
    src = (DESKTOP / "plugin.js").read_text()
    # Scan only the inlined prompts.js section — same corpus as before the merge.
    prompts = src[src.index("// --- inlined from prompts.js"):src.index("// Contribution ids", src.index("// --- inlined from prompts.js"))]
    for op in _load_manifest()["operations"]:
        for cmd in op["commands"]:
            assert cmd in prompts, f"{op['kind']}: {cmd!r} not in plugin.js"
        if op["script"] is not None:
            assert op["script"] in prompts, f"{op['kind']}: {op['script']} not in plugin.js"


def test_prompts_to_manifest_parity():
    src = (DESKTOP / "plugin.js").read_text()
    # Scan only the inlined prompts.js section — same corpus as before the merge.
    prompts = src[src.index("// --- inlined from prompts.js"):src.index("// Contribution ids", src.index("// --- inlined from prompts.js"))]
    manifest = _load_manifest()["operations"]
    manifest_cmds = [c for op in manifest for c in op["commands"]]
    manifest_scripts = [op["script"] for op in manifest if op["script"]]
    manifest_verbs = {re.match(r"^/([a-z-]+)", c).group(1) for c in manifest_cmds}
    for verb in set(re.findall(r"/(add-repo|fetch|task|task_add|task_list|claim|release|push|audit|pool)(?![\w-])", prompts)):
        assert verb in DOCUMENTED_COMMANDS
        assert verb in manifest_verbs, f"plugin.js invokes /{verb} absent from manifest"
    for script in set(re.findall(r"scripts/[a-z-]+\.py", prompts)):
        assert script in DOCUMENTED_SCRIPTS
        assert script in manifest_scripts, f"plugin.js invokes {script} absent from manifest"


def test_node_check_js_files():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for name in ("plugin.js",):
        result = subprocess.run([node, "--check", str(DESKTOP / name)],
                                capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"

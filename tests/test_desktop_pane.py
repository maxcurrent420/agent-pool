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


def test_desktop_plugin_registers_page_and_nav_only_no_dock():
    src = (DESKTOP / "plugin.js").read_text()
    # Sidebar nav row at /agent-pool and a ROUTES_AREA route at /agent-pool.
    assert "SIDEBAR_NAV_AREA" in src
    assert "ROUTES_AREA" in src
    assert "/agent-pool" in src
    # The right-dock pane contribution is gone entirely.
    assert "PANES_AREA" not in src
    assert "placement" not in src
    assert "420px" not in src
    assert "initializing" not in src.lower()


def test_desktop_plugin_str_guard_defined_and_used():
    src = (DESKTOP / "plugin.js").read_text()
    # str() guard helper is defined.
    assert re.search(r"function str\(v\)\s*{", src), "str() guard helper missing"
    # str() call sites exist for the previously crashing render values.
    for site in ["str(sessionId)", "str(gatewayState)", "str(op.kind)",
                 "str(contributionId)", "str(entry && entry.id)"]:
        assert site in src, f"str() guard missing at: {site}"
    # useValue(host.state.*) results never reach JSX raw — each is stringified.
    for m in re.finditer(r"const\s+(\w+)\s*=\s*useValue\(host\.state\.\w+\)", src):
        assert f"str({m.group(1)})" in src, \
            f"useValue result {m.group(1)} never stringified"
    # Error boundary present so a render error cannot take down the chat UI.
    assert "getDerivedStateFromError" in src
    assert "PoolErrorBoundary" in src


# --- Task 1: snapshot schema + discovery prompts -----------------------------
# Note on manifest parity: discoverReposPrompt() deliberately names NO slash
# command and no plugin script — it asks the agent to enumerate host git repos
# read-only. Discovery is therefore OUTSIDE the slash-command manifest parity
# set enforced by test_ops_manifest_to_prompts_parity /
# test_prompts_to_manifest_parity, by design.

def test_snapshot_read_malformed_returns_empty_list():
    src = (DESKTOP / "plugin.js").read_text()
    m = re.search(r"export function readSnapshot\(ctx, key\) \{.*?\n\}", src, re.S)
    assert m, "readSnapshot helper missing"
    body = m.group(0)
    assert "return []" in body
    assert "catch" in body


def test_snapshot_keys_and_shape_documented():
    src = (DESKTOP / "plugin.js").read_text()
    for key in ["snapshot.repos", "snapshot.tasks", "snapshot.claims",
                "snapshot.discovered", "snapshot.poolStatus", "snapshot.gateways"]:
        assert key in src, f"snapshot key {key} missing from plugin.js"
    assert "SNAPSHOT_KEYS" in src


def test_discover_repos_prompt_agent_scoped():
    src = (DESKTOP / "plugin.js").read_text()
    m = re.search(r"export function discoverReposPrompt\(\) \{\s*return '([^']+)'", src)
    assert m, "discoverReposPrompt missing"
    prompt = m.group(1)
    assert "~/Documents" in prompt and "~/projects" in prompt
    # Instructs the agent; executes nothing itself; names no slash command.
    assert not re.search(r"/(add-repo|fetch|claim|push|audit|pool)(?![\w-])", prompt)
    assert "scripts/" not in prompt
    # The pane itself never touches the filesystem.
    assert "fs." not in src.lower() or "fsync" not in src  # sanity: no fs module use
    assert "require('fs" not in src and 'from \'fs\'' not in src


def test_full_refresh_prompt_covers_four_sources():
    src = (DESKTOP / "plugin.js").read_text()
    m = re.search(r"export const FULL_REFRESH_PROMPT = '([^']+)'", src)
    assert m, "FULL_REFRESH_PROMPT missing"
    prompt = m.group(1)
    assert "pool_status" in prompt
    assert "/fetch repo list" in prompt
    assert "/fetch task list" in prompt
    assert "pool_pane_report" in prompt


def test_discover_op_kind_tracked():
    src = (DESKTOP / "plugin.js").read_text()
    assert "'discover'" in src
    assert re.search(r"OP_KINDS = \[[^\]]*'discover'", src)


def test_discover_repos_button_present():
    src = (DESKTOP / "plugin.js").read_text()
    assert "Discover repos" in src
    assert src.count("Discover repos") >= 1


# --- Task 2: dropdown-fed selectors ------------------------------------------
# Discovery prompt remains outside the manifest parity set (see Task 1 note).

def _load_options_for(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    parts = []
    for pattern in [
        r"function str\(v\) \{.*?\n\}\n",
        r"function asArray\(v\) \{[^\n]*\n",
        r"export function labelFor\(entry\) \{.*?\n\}\n",
        r"export const SNAPSHOT_KIND_EMPTY = \{.*?\n\}\n",
        r"const SNAPSHOT_KIND_SELECT = \{.*?\n\}\n",
        r"export const MANUAL_OPTION = '[^']+'\n",
        r"function entryId\(entry, fallback\) \{.*?\n\}\n",
        r"export function itemsFor\(snapshot, kind\) \{.*?\n\}\n",
        r"export function buildOptions\(items, emptyHint, selectLabel, allowManual\) \{.*?\n\}\n",
        r"export function optionsFor\(snapshot, kind, allowManual = true\) \{.*?\n\}\n",
    ]:
        m = re.search(pattern, src, re.S)
        assert m, f"missing piece: {pattern}"
        parts.append(m.group(0))
    harness = (
        "".join(parts)
        + "\nconst snapshot = JSON.parse(process.argv[1]);\n"
        + "const kind = process.argv[2];\n"
        + "console.log(JSON.stringify(optionsFor(snapshot, kind)));\n"
    )
    return node, harness


def _run_options_for(src, snapshot, kind):
    node, harness = _load_options_for(src)
    out = subprocess.run([node, "-e", harness, json.dumps(snapshot), kind],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_options_for_tasks_contains_label_shape():
    src = (DESKTOP / "plugin.js").read_text()
    options = _run_options_for(src,
        {"tasks": [{"ticket_id": "ticket 42", "title": "fix parser", "repo": "my-repo"}]},
        "tasks")
    labels = [o["label"] for o in options]
    assert any("ticket 42: fix parser" in label for label in labels), labels


def test_options_for_empty_snapshot_exact_placeholder_and_manual():
    src = (DESKTOP / "plugin.js").read_text()
    options = _run_options_for(src, {}, "tasks")
    labels = [o["label"] for o in options]
    assert labels == ["No tasks fetched yet — click Fetch tasks", "(enter manually…)"], labels
    assert options[0]["value"] == ""


def test_label_for_falls_back_to_id():
    src = (DESKTOP / "plugin.js").read_text()
    options = _run_options_for(src, {"tasks": [{"ticket_id": "ticket 7"}]}, "tasks")
    labels = [o["label"] for o in options]
    assert "ticket 7" in labels


def test_snapshot_select_and_helpers_exist():
    src = (DESKTOP / "plugin.js").read_text()
    assert "function SnapshotSelect(" in src
    assert "export function optionsFor(" in src
    assert "export function labelFor(" in src
    for hint in ["No pools loaded yet — click Refresh pools",
                 "No repos in the pool yet — click Refresh pools or Add repo",
                 "No tasks fetched yet — click Fetch tasks",
                 "No claims yet — claim a ticket first",
                 "Nothing awaiting approval right now",
                 "Request the grant first (Step 1)",
                 "Version resolves automatically"]:
        assert hint in src, hint


# --- Task 3: repo add flow without typing a path ------------------------------

def _load_add_repo_helpers(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    parts = []
    for pattern in [
        r"function str\(v\) \{.*?\n\}\n",
        r"function asArray\(v\) \{[^\n]*\n",
        r"function isAbsoluteLocalPath\(value\) \{.*?\n\}\n",
        r"export function repoNameFromPath\(path\) \{.*?\n\}\n",
        r"function normalizeDiscoveredEntry\(raw\) \{.*?\n\}\n",
        r"export function filterDiscovered\(items\) \{.*?\n\}\n",
        r"export function resolveAddRepoSelection\(\{ discoveredId, manualPath, discovered \}\) \{.*?\n\}\n",
        r"function replySuffix\(\) \{.*?\n\}\n",
        r"export function addRepoOwnerPrompt\(\{ gateway, repoId, path, version \}\) \{.*?\n\}\n",
        r"export function addRepoLinkPrompt\(\{ gateway, repoId, path, version, maintainerGateway \}\) \{.*?\n\}\n",
    ]:
        m = re.search(pattern, src, re.S)
        assert m, f"missing piece: {pattern}"
        parts.append(m.group(0))
    return node, "".join(parts)


def _run_node(src, body):
    node, harness = _load_add_repo_helpers(src)
    out = subprocess.run([node, "-e", harness + body],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_add_repo_owner_prompt_golden():
    src = (DESKTOP / "plugin.js").read_text()
    out = _run_node(src, "console.log(JSON.stringify(addRepoOwnerPrompt({ gateway: 'default', repoId: 'my-repo', path: '/home/u/my-repo' })));")
    assert json.loads(out) == (
        "On the default gateway, register this repo in the pool and ingest it as a new version: "
        "run /add-repo my-repo --path /home/u/my-repo. If the user did not supply a version and "
        "the pool already has accepted versions, first ask the maintainer gateway /fetch repo my-repo "
        "for accepted_version and use the next one. Return the plugin's reply verbatim."
    )
    out_v = _run_node(src, "console.log(JSON.stringify(addRepoOwnerPrompt({ gateway: 'default', repoId: 'my-repo', path: '/home/u/my-repo', version: 3 })));")
    assert "/add-repo my-repo --path /home/u/my-repo --version 3." in json.loads(out_v)


def test_add_repo_link_prompt_golden():
    src = (DESKTOP / "plugin.js").read_text()
    out = _run_node(src, "console.log(JSON.stringify(addRepoLinkPrompt({ gateway: 'pool-contrib', repoId: 'my-repo', path: '/home/u/my-repo', maintainerGateway: 'default' })));")
    assert json.loads(out) == (
        "On the pool-contrib gateway, link this repo into a designated pool (two steps, one tracked operation): "
        "(1) run scripts/contributor-add.py /home/u/my-repo --repo my-repo and capture the printed manifest_json "
        "and transfer_ticket; (2) send /add-repo --deliver <transfer_ticket> --manifest '<manifest_json>' --repo my-repo "
        "to default's bot. Do not use remote URLs — local paths only. Return the plugin's reply verbatim."
    )


def test_add_repo_flow_state_requires_path_and_composes_verbatim():
    src = (DESKTOP / "plugin.js").read_text()
    discovered = [{"id": "/home/u/my-repo", "path": "/home/u/my-repo", "name": "my-repo"}]
    body = (
        "const d = " + json.dumps(discovered) + ";\n"
        "console.log(JSON.stringify(["
        " resolveAddRepoSelection({ discoveredId: '', discovered: d }),"
        " resolveAddRepoSelection({ discoveredId: '/home/u/my-repo', discovered: d }),"
        " resolveAddRepoSelection({ discoveredId: '', manualPath: 'relative/x', discovered: d }),"
        " resolveAddRepoSelection({ discoveredId: '', manualPath: 'https://evil.example/repo', discovered: d }),"
        " resolveAddRepoSelection({ discoveredId: '', manualPath: '/home/u/manual-repo', discovered: d })"
        "]));"
    )
    out = json.loads(_run_node(src, body))
    assert out[0] is None and out[2] is None and out[3] is None
    assert out[1] == {"path": "/home/u/my-repo", "repoId": "my-repo"}
    assert out[4] == {"path": "/home/u/manual-repo", "repoId": "manual-repo"}
    prompt = json.loads(_run_node(src, "console.log(JSON.stringify(addRepoOwnerPrompt({ gateway: 'default', repoId: out_repoId, path: '/home/u/my-repo' })));".replace("out_repoId", "'my-repo'")))
    assert "/add-repo my-repo --path /home/u/my-repo" in prompt


def test_filter_discovered_filters_non_local_paths():
    src = (DESKTOP / "plugin.js").read_text()
    items = [
        "https://github.com/u/repo — remote",
        "ssh://git@host/repo",
        "git@github.com:u/repo.git",
        "relative/path/repo",
        "/home/u/good-repo — good-repo",
        {"path": "/home/u/obj-repo"},
        {"path": "scheme://nope"},
        "/home/u/plain",
    ]
    body = "console.log(JSON.stringify(filterDiscovered(" + json.dumps(items) + ")));"
    out = json.loads(_run_node(src, body))
    paths = [e["path"] for e in out]
    assert paths == ["/home/u/good-repo", "/home/u/obj-repo", "/home/u/plain"], paths


def test_add_repo_form_rework_markers():
    src = (DESKTOP / "plugin.js").read_text()
    assert "filterDiscovered" in src
    assert "version auto-resolves (agent asks /fetch repo for accepted_version)" in src
    assert "entries skipped (non-local paths)" in src
    assert "'Add repo'" in src and "'Contribute repo'" in src
    assert "substeps: ['build bundle', 'deliver']" in src
    assert "args.substepsDone" in src or "substepsDone" in src
    # Manual path only behind the disclosure; the standalone absolute-path field is gone.
    assert "field('local path (absolute)'" not in src
    assert "field('repo_id'" not in src


# --- Task 4: one primary button per flow + confirm gates ----------------------

def _load_confirm_helpers(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    parts = []
    for pattern in [
        r"function str\(v\) \{.*?\n\}\n",
        r"export const PRIMARY_BUTTONS = \{.*?\n\}\n",
        r"export function buildConfirmCard\(target, gateway, commandText\) \{.*?\n\}\n",
        r"export function approveCommandText\(\{ repoId, version, ticketId, deny \} = \{\}\) \{.*?\n\}\n",
        r"export function mergeCommandText\(\{ repoId, ticketId, checkoutPath \} = \{\}\) \{.*?\n\}\n",
        r"export function finalizeOp\(draft, confirmState\) \{.*?\n\}\n",
    ]:
        m = re.search(pattern, src, re.S)
        assert m, f"missing piece: {pattern}"
        parts.append(m.group(0))
    return node, "".join(parts)


def _run_confirm_node(src, body):
    node, harness = _load_confirm_helpers(src)
    out = subprocess.run([node, "-e", harness + body],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_build_confirm_card_text_and_command():
    src = (DESKTOP / "plugin.js").read_text()
    cmd = json.loads(_run_confirm_node(src, "console.log(JSON.stringify(approveCommandText({ repoId: 'my-repo', version: 3 })));"))
    assert cmd == "scripts/pool-approve.py --repo my-repo --version 3"
    card = json.loads(_run_confirm_node(src, "console.log(JSON.stringify(buildConfirmCard('Approve `my-repo v3`', 'default', approveCommandText({ repoId: 'my-repo', version: 3 }))));"))
    assert "my-repo" in card["text"] and "v3" in card["text"]
    assert "scripts/pool-approve.py --repo my-repo --version 3" in card["text"]
    assert card["commandText"] == "scripts/pool-approve.py --repo my-repo --version 3"
    assert card["banner"] == "OWNER ONLY"
    deny_cmd = json.loads(_run_confirm_node(src, "console.log(JSON.stringify(approveCommandText({ ticketId: '42', deny: true })));"))
    assert deny_cmd == "scripts/pool-approve.py --ticket 42 --deny"


def test_finalize_op_requires_confirmed():
    src = (DESKTOP / "plugin.js").read_text()
    body = (
        "const draft = { kind: 'approve', gateway: 'default', args: { repoId: 'my-repo', version: 3 },"
        " text: \"On the default gateway, OWNER ONLY: run scripts/pool-approve.py --repo my-repo --version 3. Return the plugin's reply verbatim.\" };\n"
        "console.log(JSON.stringify(["
        " finalizeOp(draft, { confirmed: false }),"
        " finalizeOp(draft, {}),"
        " finalizeOp(draft, null),"
        " finalizeOp(draft, { confirmed: true })"
        "]));"
    )
    out = json.loads(_run_confirm_node(src, body))
    assert out[0] is None and out[1] is None and out[2] is None
    assert out[3]["kind"] == "approve"
    assert "scripts/pool-approve.py --repo my-repo --version 3" in out[3]["text"]


def test_seven_primary_flows_have_exactly_one_button_label():
    src = (DESKTOP / "plugin.js").read_text()
    m = re.search(r"export const PRIMARY_BUTTONS = \{.*?\n\}", src, re.S)
    assert m, "PRIMARY_BUTTONS table missing"
    table = m.group(0)
    for label in ["Add repo", "Fetch tasks", "Claim", "Pull repo",
                  "Submit work (push)", "Approve", "Merge"]:
        assert f"'{label}'" in table, label
    for label in ["'Add repo'", "'Fetch tasks'", "'Claim'", "'Pull repo'",
                  "'Submit work (push)'", "'Approve'", "'Merge'", "'Deny'"]:
        assert label in table
    for key in ["addRepo", "fetchTasks", "claim", "pull", "push", "approve", "deny", "merge"]:
        assert f"PRIMARY_BUTTONS.{key}" in src, key


def test_confirm_gate_wired_in_approve_and_merge():
    src = (DESKTOP / "plugin.js").read_text()
    assert "function ConfirmCard(" in src
    assert "buildConfirmCard(" in src
    assert "finalizeOp(" in src
    assert src.count("finalizeOp(") >= 4
    assert "confirmed: true" in src
    assert "card.banner" in src


def test_push_card_autofill_wiring_and_step3_gate():
    src = (DESKTOP / "plugin.js").read_text()
    assert "effectiveGrantToken" in src and "effectiveTransferTicket" in src and "effectiveManifest" in src
    assert "onPickClaim" in src
    assert "auto-filled from the grant op reply" in src
    assert "auto-filled from the package op reply" in src
    assert "grantDone && packageDone" in src  # step-3 gating intact
    assert "PRIMARY_BUTTONS.push" in src

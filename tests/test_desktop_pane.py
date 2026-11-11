"""Desktop pane backend tests (Task 4): the read-only dashboard
router and the pool_pane_report sidecar tool.

Task 10 extends this file.
"""
import importlib.util
import json
import shutil
import subprocess
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
    monkeypatch.setenv("AGENT_POOL_PANE_LEDGER", str(tmp_path / "missing" / "data.db"))
    api = _load_api()
    result = api.pane_state()
    assert isinstance(result, dict)
    assert result["ops"] == []
    assert isinstance(result["pool_status"], dict)
    assert "updated_at" in result and "at" in result
    # Missing ledger must surface loudly, never as a silent empty list.
    assert result.get("lists_error")


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
    monkeypatch.setenv("AGENT_POOL_PANE_LEDGER", str(tmp_path / "missing" / "data.db"))
    api = _load_api()
    # Empty: no sidecar written yet.
    empty = api.pane_state()
    assert empty["ops"] == []
    assert isinstance(empty["pool_status"], dict)
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
    assert kinds == ["add-repo-owner", "add-repo-link", "add-task", "fetch-tasks",
                     "claim", "pull", "push", "approve", "audit-show", "merge"]
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
    manifest_verbs = {re.match(r"^/([a-z_-]+)", c).group(1) for c in manifest_cmds}
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


def test_registers_page_and_nav_only_no_dock():
    src = (DESKTOP / "plugin.js").read_text()
    # The /agent-pool page route AND the left sidebar nav row that links to it.
    assert "ROUTES_AREA" in src
    assert "SIDEBAR_NAV_AREA" in src
    assert "/agent-pool" in src
    assert "id: 'nav'" in src
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
    for site in ["str(gatewayState)", "str(gateway)", "str(op.reply)",
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
                "snapshot.poolStatus", "snapshot.gateways"]:
        assert key in src, f"snapshot key {key} missing from plugin.js"
    assert "SNAPSHOT_KEYS" in src


def test_discover_op_and_prompt_are_gone():
    src = (DESKTOP / "plugin.js").read_text()
    assert "discoverReposPrompt" not in src
    assert "FULL_REFRESH_PROMPT" not in src
    assert "BATCHED_STATUS_PROMPT" not in src
    assert not re.search(r"OP_KINDS = \[[^\]]*'discover'", src)
    assert "'Discover repos'" not in src
    # Display refreshes are local reads; snapshot-selects carry no refresh actions.
    assert "refreshAction" not in src
    assert "refreshSnapshot(ctx, sessionId, 'full'" not in src
    assert "refreshSnapshot(ctx, sessionId, 'repos'" not in src
    assert "refreshSnapshot(ctx, sessionId, 'tasks'" not in src
    assert "refreshSnapshot(ctx, sessionId, 'discovered'" not in src


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
        r"export function addRepoOwnerPrompt\(\{ gateway, repoId, path, version \}\) \{.*?\n\}\n",
        r"export function addRepoLinkPrompt\(\{ gateway, repoId, path \}\) \{.*?\n\}\n",
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
        "Add the local checkout /home/u/my-repo to the agent pool as repo my-repo: "
        "on the default gateway run /add-repo my-repo --path /home/u/my-repo. "
        "If the pool already has accepted versions, first run /fetch repo my-repo "
        "to get accepted_version and pass --version <next>. Report the plugin's reply verbatim."
    )
    out_v = _run_node(src, "console.log(JSON.stringify(addRepoOwnerPrompt({ gateway: 'default', repoId: 'my-repo', path: '/home/u/my-repo', version: 3 })));")
    assert "/add-repo my-repo --path /home/u/my-repo --version 3." in json.loads(out_v)


def test_add_repo_link_prompt_golden():
    src = (DESKTOP / "plugin.js").read_text()
    out = _run_node(src, "console.log(JSON.stringify(addRepoLinkPrompt({ gateway: 'pool-contrib', repoId: 'my-repo', path: '/home/u/my-repo' })));")
    assert json.loads(out) == (
        "Contribute my local checkout /home/u/my-repo to the maintainer's pool as repo my-repo: "
        "(1) run scripts/contributor-add.py /home/u/my-repo --repo my-repo to mint a signed manifest "
        "and Tor transfer ticket; (2) post into the shared Discord channel, mention-first, manifest single-quoted: "
        "<@<maintainer_bot_id>> /add-repo --deliver <transfer_ticket> --manifest '<manifest_json>' --repo my-repo. "
        "Local paths only — never a remote URL. Report the replies verbatim."
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


def test_add_repo_prompt_companion_markers():
    src = (DESKTOP / "plugin.js").read_text()
    assert "filterDiscovered" in src
    # No forms: the add-repo flow is a copy-to-clipboard companion now.
    assert "'Add repo'" in src and "'Contribute repo'" in src
    assert "RepoAddCompanion" in src
    assert "Ask your agent to add a repo to the pool" in src
    assert "AddRepoForm" not in src


# --- Task 5: tracker as the operational dashboard ------------------------------

def _run_task5(src, body):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    patterns = [
        r"function str\(v\) \{.*?\n\}\n",
        r"function asArray\(v\) \{[^\n]*\n",
        r"export function extractAwaitingApproval\(ps\) \{.*?\n\}\n",
        r"export function extractBudgets\(ps\) \{.*?\n\}\n",
        r"export function parsePaneState\(raw\) \{.*?\n\}\n",
        r"export function summarizeOp\(op, snapshotLookup\) \{.*?\n\}\n",
    ]
    parts = []
    for pattern in patterns:
        m = re.search(pattern, src, re.S)
        assert m, f"missing piece: {pattern}"
        parts.append(m.group(0))
    harness = "".join(parts) + "\n" + body
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()


def test_summarize_op_push_with_repo_and_ticket_labels():
    src = (DESKTOP / "plugin.js").read_text()
    body = (
        "const op = { kind: 'push', gateway: 'pool-contrib', at: 0, args: { repoId: 'my-repo', version: 3, ticketId: '42' } };\n"
        "const lookup = { repos: [{ id: 'my-repo', title: 'my-repo', version: 3 }], tasks: [{ ticket_id: '42', title: 'fix parser' }] };\n"
        "console.log(JSON.stringify(summarizeOp(op, lookup)));\n"
        "const empty = { repos: [], tasks: [] };\n"
        "console.log(JSON.stringify(summarizeOp(op, empty)));\n"
    )
    out = _run_task5(src, body)
    labeled, fallback = (json.loads(l) for l in out)
    assert labeled.startswith('push · my-repo v3 · ticket 42: fix parser · gateway pool-contrib')
    assert 'my-repo v3' in fallback and 'ticket 42' in fallback
    assert 'fix parser' not in fallback


def test_awaiting_tab_sourced_only_from_pool_status():
    src = (DESKTOP / "plugin.js").read_text()
    # An op-log entry alone never creates an awaiting-approval row: with no
    # pool_status in the sidecar, parsePaneState yields an empty list even if
    # the payload carries op-like entries.
    body = (
        "const parsed = parsePaneState({ ops: [{ id: 'op-1', kind: 'approve', status: 'done (awaiting approval)' }], detail: 'x' });\n"
        "console.log(JSON.stringify(parsed.awaitingApproval));\n"
    )
    out = _run_task5(src, body)
    assert json.loads(out[0]) == []
    # Source-level: the awaiting tab reads poolStatus.awaitingApproval only.
    m = re.search(r"\} else if \(tab === 'awaiting'\) \{.*?\n  \} else if", src, re.S)
    assert m, "awaiting tab branch missing"
    assert "poolStatus.awaitingApproval" in m.group(0) or "awaiting" in m.group(0)
    assert "list.filter" not in m.group(0)
    # Awaiting entries get copy-prompt companions (no form pre-fill).
    assert "Copy approve prompt" in src
    assert "Copy deny prompt" in src


def test_refused_rows_have_no_retry_button():
    src = (DESKTOP / "plugin.js").read_text()
    m = re.search(r"function opRow\(.*?\n\}\n", src, re.S)
    assert m
    body = m.group(0)
    # Refused rows render no action buttons beyond "Copy prompt again".
    assert "Retry deliver" not in body and "retry === 'deliver'" not in body and "retry === 'resubmit'" not in body
    assert "retryLegality" not in body
    assert "read the refusal; fix the cause, then copy the prompt again." in body
    assert "Copy prompt again" in body


def test_audit_dropdown_all_tickets_default_and_run_button():
    src = (DESKTOP / "plugin.js").read_text()
    assert "selectLabel: '(all tickets)'" in src
    assert "Copy audit prompt" in src
    assert "copyAndLog('audit-show'" in src


# --- Task 6: empty / loading / error states ----------------------------------

def _run_first_run(src, storage_obj):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    m = re.search(r"function str\(v\) \{.*?\n\}\n", src, re.S)
    assert m
    m2 = re.search(r"export function firstRun\(source\) \{.*?\n\}\n", src, re.S)
    assert m2, "firstRun export missing"
    harness = (
        m.group(0) + m2.group(0)
        + "\nconst storage = JSON.parse(process.argv[1]);\n"
        + "const ctx = { storage: { get: (k, d) => (k in storage ? storage[k] : d) } };\n"
        + "console.log(JSON.stringify(firstRun(ctx)));\n"
    )
    out = subprocess.run([node, "-e", harness, json.dumps(storage_obj)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_first_run_true_on_empty_storage():
    src = (DESKTOP / "plugin.js").read_text()
    assert _run_first_run(src, {}) is True


def test_first_run_false_once_any_snapshot_or_ops_key_exists():
    src = (DESKTOP / "plugin.js").read_text()
    assert _run_first_run(src, {"snapshot.poolStatus": {"at": 123, "poolStatus": {"awaiting_approval": []}}}) is False
    assert _run_first_run(src, {"snapshot.repos": {"at": 123, "items": [{"id": "my-repo"}]}}) is False
    assert _run_first_run(src, {"ops": [{"id": "op-1", "kind": "claim"}]}) is False
    # Empty collections do not count as "a snapshot has landed".
    assert _run_first_run(src, {"snapshot.repos": {"at": 123, "items": []}, "ops": []}) is True


def _run_stale_strip(src, last_at):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    m = re.search(r"function str\(v\) \{.*?\n\}\n", src, re.S)
    assert m
    m2 = re.search(r"export function staleStripText\(lastAt\) \{.*?\n\}\n", src, re.S)
    assert m2, "staleStripText export missing"
    harness = (
        m.group(0) + m2.group(0)
        + "\nconsole.log(JSON.stringify(staleStripText(Number(process.argv[1]))));\n"
    )
    out = subprocess.run([node, "-e", harness, str(last_at)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_stale_strip_contains_marker_and_timestamp():
    src = (DESKTOP / "plugin.js").read_text()
    text = _run_stale_strip(src, 1700000000000)
    assert "sidecar unreachable" in text
    assert "1970-01-01T00:00:00.000Z" not in text  # sanity: real ts rendered
    assert "last update:" in text
    assert "2023-11-14T22:13:20.000Z" in text


def test_stale_strip_absent_when_snapshot_exists():
    src = (DESKTOP / "plugin.js").read_text()
    # The renderer gates the strip on `!hasSnapshot`; verify the wiring exists.
    assert "const showStaleStrip = Boolean(sidecarDownAt) && !hasSnapshot" not in src
    assert "sidecarDownAt && !hasSnapshot" in src  # ListsBanner gates the stale strip on having no snapshot
    assert "staleStripText(lastUpdate ?? sidecarDownAt)" in src
    # ...and the onboarding/first-run wiring is present.
    assert "export function firstRun(" in src
    assert "Welcome to your Pool Console" in src
    assert "Refresh state" in src
    assert "Loading…" in src
    assert "Refreshing local state…" in src
    # Mount refresh re-reads the local computed pane-state (no agent round-trip).
    assert "useEffect(() => { refreshFromSidecar() }, [])" in src
    # Tor chip is rendered from computed pool_status.tor, never a storage key.
    assert "status.tor" not in src
    assert "poolPaneStore.tor" not in src


# --- Task 7: UX-guarantee battery completion -----------------------------------

def test_forbidden_patterns_extend_file_apis():
    # In-frame file APIs are banned outright; plugin.js also contains no
    # word-boundary 'drop' (drag-drop of host paths must never exist).
    src = (DESKTOP / "plugin.js").read_text()
    for pat in ["FileReader", "DataTransfer", "createObjectURL"]:
        assert pat not in src, pat
    assert not re.search(r"(?<![\w])drop(?![\w])", src), "word-boundary 'drop' found"
    # Same scan across the shipped desktop sources.
    for f in DESKTOP.glob("*.js"):
        s = f.read_text()
        for pat in ["FileReader", "DataTransfer", "createObjectURL"]:
            assert pat not in s, f"{f}: {pat}"
        assert not re.search(r"(?<![\w])drop(?![\w])", s), f"{f}: word-boundary 'drop'"


def test_dropdown_options_empty_exact_list_and_label_shape():
    # optionsFor on a fake snapshot yields labeled options (id — title shape);
    # on empty yields exactly placeholder + manual fallback with the empty-hint.
    src = (DESKTOP / "plugin.js").read_text()
    options = _run_options_for(src,
        {"tasks": [{"ticket_id": "42", "title": "fix parser", "repo": "my-repo"}]},
        "tasks")
    labels = [o["label"] for o in options]
    assert any(l.startswith("42") or "ticket 42" in l for l in labels), labels
    task_opts = [o for o in options if o["value"] == "42"]
    assert task_opts and "fix parser" in task_opts[0]["label"], options
    empty = _run_options_for(src, {}, "repos")
    assert [o["label"] for o in empty] == [
        "No repos in the pool yet — click Refresh pools or Add repo",
        "(enter manually…)",
    ], empty
    assert empty[0]["value"] == ""


def _prompt_builder_inventory(src):
    fns = re.findall(r"export function (\w*[Pp]rompt)\(", src)
    consts = re.findall(r"export const (\w*[Pp]ROMPT|\w*PROMPT) = ", src)
    return sorted(set(fns) | set(consts))


def test_prompt_golden_strings_all_builders():
    src = (DESKTOP / "plugin.js").read_text()
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    golden = {
        "auditShowPrompt": "Read-only: on the default gateway run /audit show 42 and report the result verbatim. No approval implied.",
        "addRepoOwnerPrompt": "Add the local checkout /home/u/my-repo to the agent pool as repo my-repo: on the default gateway run /add-repo my-repo --path /home/u/my-repo --version 3. If the pool already has accepted versions, first run /fetch repo my-repo to get accepted_version and pass --version <next>. Report the plugin's reply verbatim.",
        "addRepoLinkPrompt": "Contribute my local checkout /home/u/my-repo to the maintainer's pool as repo my-repo: (1) run scripts/contributor-add.py /home/u/my-repo --repo my-repo to mint a signed manifest and Tor transfer ticket; (2) post into the shared Discord channel, mention-first, manifest single-quoted: <@<maintainer_bot_id>> /add-repo --deliver <transfer_ticket> --manifest '<manifest_json>' --repo my-repo. Local paths only — never a remote URL. Report the replies verbatim.",
        "fetchRepoListPrompt": "On the default gateway run /fetch repo list. Report the table verbatim.",
        "fetchTaskListPrompt": "On the default gateway run /fetch repo list, then /fetch task list --repo my-repo. Report both tables verbatim so I can paste them or so the pane's Refresh can pick up the sidecar.",
        "taskAddPrompt": "Open a pool ticket on repo my-repo: run /task_add --repo my-repo --title \"Fix the thing\" --brief \"Do X\" --pool demo (use the flat alias task_add — the spaced `task add` drops --pool). Report the new ticket_id verbatim.",
        "prunePrompt": "Prune stale agent-pool worktrees under ~/.hermes/plugin-data/agent-pool/work/integrate/. Step 1: enumerate every directory and gate-t-*.json file there. Step 2: classify — KEEP: every bundle-v*.git whose version is accepted in the ledger, every worktree tied to an open/claimed ticket, and anything you cannot positively classify as stale. STALE (the only things you may delete): branch-t-* directories and gate-t-*.json files with no matching live ticket. Step 3: print the full keep/delete list with counts before deleting anything. Step 4: delete ONLY the stale candidates. Touch nothing else. Report the before/after listing and the exact commands you ran.",
        "claimPrompt": "Claim pool ticket 42: on the default gateway run /claim 42. The reply carries worktree_path, branch (pool/42), base_ref and budget. Then run scripts/contributor-claim.py 42 --reply-file <saved reply.json> --dest <work dir> to fetch the accepted bytes and clone the worktree. Report the worktree path and branch verbatim.",
        "pullPrompt": "On the default gateway, fetch the accepted bytes for ticket 42 into a worktree: run scripts/contributor-claim.py 42 --reply-file reply.json --dest /tmp/dest. Report the worktree path and branch verbatim.",
        "pushPrompt": "Push the completed work for ticket 42 on repo my-repo, pool pool-1: (1) request a grant: /push repo my-repo --ticket 42; (2) package the worktree /wt: scripts/contributor-push.py /wt --repo my-repo --pool pool-1 --version 3 — take next_version from the grant reply or re-ask /fetch repo my-repo for accepted_version; never trust enroll-time chain_tips; (3) deliver: /push repo --deliver <transfer_ticket> --token <grant_token> --manifest '<manifest_json>'. A redelivery is legal — the grant is not consumed by deliver. Report each step's reply verbatim.",
        "approvePrompt": "OWNER ONLY: accept the submitted version for repo my-repo (version 3): run scripts/pool-approve.py --repo my-repo --version 3. If the plugin refuses because this profile is not the pool owner, report the refusal verbatim and do not retry. Report the plugin's reply verbatim.",
        "mergePrompt": "OWNER ONLY: merge the approved bundle for pushed claim 42 into the canonical checkout at /co — the plugin never merges, so do it as your own git work: git -C /co fetch <bundle> 'refs/heads/master:refs/accepted/my-repo-v3' then git -C /co merge --no-ff --allow-unrelated-histories refs/accepted/my-repo-v3. (Unrelated histories are normal for a fresh pool root; identical content is a correct no-op.) If the audit is not clean or no accepted bundle exists, report verbatim and stop.",
    }
    inventory = _prompt_builder_inventory(src)
    missing = [name for name in inventory if name not in golden]
    assert not missing, f"prompt builders without golden coverage: {missing}"
    calls = {
        "auditShowPrompt": "auditShowPrompt('default', '42')",
        "addRepoOwnerPrompt": "addRepoOwnerPrompt({ gateway: 'default', repoId: 'my-repo', path: '/home/u/my-repo', version: 3 })",
        "addRepoLinkPrompt": "addRepoLinkPrompt({ gateway: 'default', repoId: 'my-repo', path: '/home/u/my-repo' })",
        "fetchRepoListPrompt": "fetchRepoListPrompt({ gateway: 'default' })",
        "fetchTaskListPrompt": "fetchTaskListPrompt({ gateway: 'default', repoId: 'my-repo' })",
        "taskAddPrompt": "taskAddPrompt({ gateway: 'default', repoId: 'my-repo', title: 'Fix the thing', brief: 'Do X', poolId: 'demo' })",
        "prunePrompt": "prunePrompt({ gateway: 'default' })",
        "claimPrompt": "claimPrompt({ gateway: 'default', ticketId: '42' })",
        "pullPrompt": "pullPrompt({ gateway: 'default', repoId: 'my-repo', ticketId: '42', replyFile: 'reply.json', dest: '/tmp/dest' })",
        "pushPrompt": "pushPrompt({ gateway: 'default', repoId: 'my-repo', ticketId: '42', worktreePath: '/wt', poolId: 'pool-1', version: 3 })",
        "approvePrompt": "approvePrompt({ gateway: 'default', repoId: 'my-repo', version: 3 })",
        "mergePrompt": "mergePrompt({ gateway: 'default', repoId: 'my-repo', ticketId: '42', checkoutPath: '/co', version: 3 })",
    }
    parts = [re.search(r"function str\(v\) \{.*?\n\}\n", src, re.S).group(0)]
    for name in inventory:
        parts.append(re.search(r"export function " + name + r"\(.*?\n\}\n", src, re.S).group(0))
    body = "\n".join(parts).replace("export ", "")
    for name in inventory:
        script = body + "\nconsole.log(JSON.stringify(" + calls[name] + "));\n"
        outs = []
        for _ in range(2):
            out = subprocess.run([node, "-e", script], capture_output=True, text=True)
            assert out.returncode == 0, out.stderr
            outs.append(json.loads(out.stdout))
        assert outs[0] == outs[1] == golden[name], f"{name} drifted from golden"


# --- Pane computed-read wiring (2026-11-10) -----------------------------------

def _load_parse_harness(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    parts = []
    for pattern in [
        r"function str\(v\) \{.*?\n\}\n",
        r"function asArray\(v\) \{[^\n]*\n",
        r"export function extractAwaitingApproval\(ps\) \{.*?\n\}\n",
        r"export function extractBudgets\(ps\) \{.*?\n\}\n",
        r"export function parsePaneState\(raw\) \{.*?\n\}\n",
    ]:
        m = re.search(pattern, src, re.S)
        assert m, f"missing piece: {pattern}"
        parts.append(m.group(0))
    return node, "".join(parts).replace("export ", "")


def test_parse_pane_state_at_rules():
    src = (DESKTOP / "plugin.js").read_text()
    node, harness = _load_parse_harness(src)
    body = (
        "const out = ["
        " parsePaneState({ updated_at: '2026-11-10T08:52:00.000Z', pool_status: {} }).at,"
        " parsePaneState({ at: 1700000000000 }).at,"
        " parsePaneState({}).at,"
        " parsePaneState(null).at"
        "];\nconsole.log(JSON.stringify(out));\n"
    )
    out = subprocess.run([node, "-e", harness + body], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    at_iso, at_num, at_empty, at_null = json.loads(out.stdout)
    assert at_iso == 1794300720000
    assert at_num == 1700000000000
    assert at_empty is None and at_null is None
    # The Date.now() fallback is gone from parsePaneState entirely.
    m = re.search(r"export function parsePaneState\(raw\) \{.*?\n\}\n", src, re.S)
    assert m and "Date.now()" not in m.group(0)


def test_parse_pane_state_surfaces_lists_error_and_worktrees_and_tor():
    src = (DESKTOP / "plugin.js").read_text()
    node, harness = _load_parse_harness(src)
    body = (
        "const p = parsePaneState({ updated_at: '2026-11-10T08:52:00Z',"
        " lists_error: 'boom', lists_warning: 'warn',"
        " worktrees: [{ repo_id: 'demo-repo', bundles: ['bundle-v1.git'], worktrees: [] }],"
        " pool_status: { tor: { ready: { ok: true }, socks_port: 9055 }, awaiting_approval: [] } });\n"
        "console.log(JSON.stringify({ e: p.listsError, w: p.listsWarning,"
        " wt: p.worktrees, tor: p.tor, ps: !!p.poolStatus }));\n"
    )
    out = subprocess.run([node, "-e", harness + body], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    parsed = json.loads(out.stdout)
    assert parsed["e"] == "boom" and parsed["w"] == "warn"
    assert parsed["wt"][0]["repo_id"] == "demo-repo"
    assert parsed["tor"]["socks_port"] == 9055
    assert parsed["ps"] is True


# --- Pane usability pass: add-task, affordances, guidance (2026-11-10) --------

def _load_task_add_helpers(src):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    parts = []
    for pattern in [
        r"function str\(v\) \{.*?\n\}\n",
        r"export function taskAddPrompt\(\{ gateway, repoId, title, brief, poolId \}\) \{.*?\n\}\n",
    ]:
        m = re.search(pattern, src, re.S)
        assert m, f"missing piece: {pattern}"
        parts.append(m.group(0))
    return node, "".join(parts)


def test_task_add_prompt_registry():
    src = (DESKTOP / "plugin.js").read_text()
    assert "PROMPT_ACTIONS" in src
    assert "'add-task':" in src
    m = re.search(r"export const OP_KINDS = \[(.*?)\]", src, re.S)
    assert m, "OP_KINDS missing"
    assert "'add-task'" in m.group(1)
    # The pane composes /task_add (the flat alias), never "/task add" — the
    # add-branch of _handle_task reads pool_id and would drop --pool.
    assert "/task_add" in src


def test_task_add_prompt_golden():
    src = (DESKTOP / "plugin.js").read_text()
    node, harness = _load_task_add_helpers(src)
    body = (
        "console.log(JSON.stringify(["
        " taskAddPrompt({ gateway: 'default', repoId: 'demo-repo', title: 'Fix the thing', brief: 'Do X', poolId: 'demo' }),"
        " taskAddPrompt({ gateway: 'default', repoId: 'demo-repo', title: 'Fix the thing' })"
        "]));\n"
    )
    out = subprocess.run([node, "-e", harness + body], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    full, minimal = json.loads(out.stdout)
    assert full == (
        "Open a pool ticket on repo demo-repo: run /task_add --repo demo-repo "
        "--title \"Fix the thing\" --brief \"Do X\" --pool demo "
        "(use the flat alias task_add — the spaced `task add` drops --pool). "
        "Report the new ticket_id verbatim."
    )
    # Optional flags are omitted entirely when empty.
    assert "--brief" not in minimal and "--pool demo" not in minimal
    assert minimal == (
        "Open a pool ticket on repo demo-repo: run /task_add --repo demo-repo "
        "--title \"Fix the thing\" "
        "(use the flat alias task_add — the spaced `task add` drops --pool). "
        "Report the new ticket_id verbatim."
    )


def test_task_add_prompt_shlex_safe_quoting():
    src = (DESKTOP / "plugin.js").read_text()
    node, harness = _load_task_add_helpers(src)
    title = 'Fix "the" thing \\ now'  # quote + backslash: the shlex trap
    body = (
        "const p = taskAddPrompt({ gateway: 'default', repoId: 'demo-repo',"
        " title: " + json.dumps(title) + ", brief: '' });\n"
        "console.log(JSON.stringify(p));\n"
    )
    out = subprocess.run([node, "-e", harness + body], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    prompt = json.loads(out.stdout)
    # JSON.stringify quoting mirrors json.dumps: inner " escaped as \",
    # backslash as \\ — double-quoted and shlex parses it back exactly.
    escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    assert f'--title "{escaped}"' in prompt


def test_usability_affordance_and_guidance_markers():
    src = (DESKTOP / "plugin.js").read_text()
    # Shared button classes exist and use only --ui-* vars.
    for token in ["const btnPrimary", "const btnSecondary", "const btnGhost"]:
        assert token in src, token
    for cls in [
        "hover:bg-(--ui-bg-quaternary)",
        "focus-visible:outline-(--ui-accent)",
        "bg-(--ui-bg-tertiary)",
    ]:
        assert cls in src, cls
    # submitButton takes a variant.
    assert "variant = 'secondary'" in src
    assert "variant === 'primary' ? btnPrimary : btnSecondary" in src
    # Prompt companions replace every composer-sending control.
    assert "OperationLauncher" not in src
    assert "PROMPT_ACTIONS" in src
    assert "Copy prompt again" in src
    assert "Ask your agent to open a ticket" in src
    assert "Activity & replies" in src
    assert "Copy approve prompt" in src and "Copy deny prompt" in src
    assert "Copy audit prompt" in src
    assert "Ask your agent to add a repo to the pool" in src
    assert "Ask your agent to fetch the catalog" in src
    assert "Prune stale worktrees" in src
    # Onboarding explains the copy → paste → reply loop.
    assert "Paste it to your agent in a Desktop chat" in src
    assert "live local reads — Refresh updates them without any agent round-trip." in src
    # No hex literals in the button classes.
    m = re.search(r"const btnPrimary = '([^']*)'", src)
    assert m and "#" not in m.group(1)


# --- Add-task crash fix: notification details must be strings (React #31) -----

def _write_stub_env(tmp_path):
    """Write the stub SDK/react modules + a copy of the real desktop/plugin.js
    into tmp_path/stub. Returns the stub directory."""
    stub = tmp_path / "stub"
    (stub / "node_modules" / "react").mkdir(parents=True)
    (stub / "node_modules" / "@hermes" / "plugin-sdk").mkdir(parents=True)
    (stub / "package.json").write_text('{"type":"module"}\n')
    (stub / "node_modules" / "react" / "package.json").write_text(
        '{"name":"react","type":"module","main":"index.js","exports":{".":"./index.js","./jsx-runtime":"./jsx-runtime.js"}}\n')
    (stub / "node_modules" / "react" / "index.js").write_text(
        "export const useState = () => ['', () => {}]\n"
        "export const useEffect = () => {}\n"
        "export const Component = class {}\n")
    (stub / "node_modules" / "react" / "jsx-runtime.js").write_text(
        "export const jsx = (type, props) => ({ $$el: true, type, props })\n"
        "export const jsxs = jsx\n"
        "export const Fragment = 'Fragment'\n")
    (stub / "node_modules" / "@hermes" / "package.json").write_text('{"name":"@hermes"}\n')
    (stub / "node_modules" / "@hermes" / "plugin-sdk" / "package.json").write_text(
        '{"name":"@hermes/plugin-sdk","type":"module","main":"index.js"}\n')
    (stub / "node_modules" / "@hermes" / "plugin-sdk" / "index.js").write_text(
        "globalThis.__hostCalls ||= []\n"
        "globalThis.__composerOk = true\n"
        "globalThis.__composerHandler = null\n"
        "const capturing = (name) => (...a) => { globalThis.__hostCalls.push([name, a]) }\n"
        "export const host = {\n"
        "  notify: capturing('notify'),\n"
        "  composer: { submit: (...a) => { capturing('composer.submit')(...a); if (typeof globalThis.__composerHandler === 'function') return globalThis.__composerHandler(...a); return globalThis.__composerOk !== false } },\n"
        "  os: { writeClipboard: async () => globalThis.__clipboardOk !== false }\n"
        "}\n"
        "export const ListRow = 'ListRow'\n"
        "export const SegmentedControl = 'SegmentedControl'\n"
        "export const ToggleRow = 'ToggleRow'\n"
        "export const ROUTES_AREA = 'ROUTES_AREA'\n"
        "export const SIDEBAR_NAV_AREA = 'SIDEBAR_NAV_AREA'\n"
        "export const useValue = () => null\n")
    shutil.copyfile(DESKTOP / "plugin.js", stub / "plugin.js")
    return stub


def _run_node_script(tmp_path, script_text, name="run_script.mjs"):
    """Run a node ESM script against the real plugin.js under stub SDK/react."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    stub = _write_stub_env(tmp_path)
    script = stub / name
    script.write_text(script_text)
    out = subprocess.run([node, name], cwd=stub, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def _run_node_module_checks(tmp_path):
    """Import the real desktop/plugin.js under stub SDK/react modules and run
    runtime + static checks. Returns the check results as [(ok, msg), ...]."""
    stub = _write_stub_env(tmp_path)
    script = stub / "run_checks.mjs"
    script.write_text(_CHECK_SCRIPT)
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "run_checks.mjs"], cwd=stub, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return [line for line in out.stdout.splitlines() if line.startswith(("PASS", "FAIL"))]


_CHECK_SCRIPT = r"""
import * as P from './plugin.js'

const hostCalls = []
globalThis.__hostCalls = hostCalls

const storage = new Map()
const ctx = {
  storage: {
    get: (k, d) => (storage.has(k) ? storage.get(k) : d),
    set: (k, v) => storage.set(k, v)
  },
  os: { writeClipboard: async () => true }
}
const results = []
const assert = (cond, msg) => results.push([!!cond, msg])

// copyPrompt: builds the companion, copies it, and for approve/push/prune
// appends a status:'copied' record — never 'submitted'/'awaiting agent'.
const rec = await P.copyPrompt(ctx, 'approve', { gateway: 'default', repoId: 'demo-repo', version: 3 })
const approveNotify = hostCalls.find(([k, a]) => k === 'notify' && a[0] && String(a[0].message || '').includes('Prompt copied'))
assert(approveNotify, 'approve copy emitted a notify call')
const approveDetail = approveNotify[1][0].detail
assert(approveDetail === undefined || typeof approveDetail === 'string', `copy-toast detail is string|undefined (got ${JSON.stringify(approveDetail)})`)
assert(rec && rec.status === 'copied', `approve copy stores a status:copied record (got ${rec && rec.status})`)
const stored = ctx.storage.get('ops', [])
assert(stored.length === 1 && stored[0].status === 'copied', 'copied record landed in ctx.storage ops')
assert(!stored.some(o => o.status === 'submitted' || o.status === 'awaiting agent'), 'no submitted/awaiting agent record from the pane')
assert(!('submitOp' in P), 'submitOp is absent from the module')
const claimRec = await P.copyPrompt(ctx, 'claim', { gateway: 'default', ticketId: '42' })
assert(claimRec && claimRec.status === 'copied', `claim copy stores a status:copied record (got ${claimRec && claimRec.status})`)

// summarizeOp: add-task rows render title + pool, never raw JSON.
const op = { id: 'op-x', kind: 'add-task', gateway: 'default', at: 0, args: { repoId: 'demo-repo', title: 'Extend INJECTION_PATTERNS', brief: null, poolId: 'demo-pool' } }
const s = P.summarizeOp(op, { repos: [{ id: 'demo-repo', title: 'demo-repo' }], tasks: [] })
assert(s.includes('Extend INJECTION_PATTERNS'), `summarizeOp shows title (${s})`)
assert(s.includes('pool demo-pool'), `summarizeOp shows pool (${s})`)
assert(!s.includes('{'), `summarizeOp output has no raw JSON (${s})`)

// Static: no notifyOp call site may pass an object-literal detail anymore.
import { readFileSync } from 'node:fs'
const src = readFileSync('./plugin.js', 'utf8')
for (const m of src.matchAll(/notifyOp\(ctx,[^\n]*/g)) {
  assert(!/(?<!\$)\{\s*id/.test(m[0]), `notifyOp call site passes no object detail: ${m[0].trim()}`)
}
assert(/\bdetailText\b/.test(src) && /typeof detail === 'string' \? detail/.test(src), 'notifyOp stringifies non-string details defensively')

// Static child sweep: flag bare non-str() object derefs passed as children.
let flagged = []
for (const line of src.split('\n')) {
  const m = line.match(/children:\s*(\S.*)$/)
  if (!m) continue
  const expr = m[1].trim()
  if (/^['"`]/.test(expr)) continue
  if (/^(jsx|jsxs|Component)/.test(expr)) continue
  if (/^str\(/.test(expr)) continue
  if (/^\[/.test(expr)) continue
  if (/^\d+$/.test(expr)) continue
  if (/^[A-Za-z_$][\w$]*(\.[\w$]+)*$/.test(expr)) {
    if (!/^(text|label|hint|title|message|detail|caption|kind|name|placeholder|className|ariaLabel)/.test(expr) && !/\.label$/.test(expr)) {
      flagged.push(line.trim())
    }
  }
}
assert(flagged.length === 0, `no bare non-str children sites (flagged: ${JSON.stringify(flagged)})`)

let failed = 0
for (const [ok, msg] of results) { console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); if (!ok) failed++ }
console.log(failed === 0 ? 'ALL CHECKS PASSED' : `${failed} CHECK(S) FAILED`)
process.exit(failed ? 1 : 0)
"""


def test_copy_prompt_notification_details_are_strings(tmp_path):
    lines = _run_node_module_checks(tmp_path)
    assert any("copy-toast detail is string|undefined" in l and l.startswith("PASS") for l in lines), lines
    assert any("copied record landed in ctx.storage ops" in l and l.startswith("PASS") for l in lines), lines


def test_notifyop_hardened_against_object_details(tmp_path):
    lines = _run_node_module_checks(tmp_path)
    assert any("notifyOp stringifies non-string details defensively" in l and l.startswith("PASS") for l in lines), lines
    assert not any("notifyOp call site passes no object detail: notifyOp(ctx," in l and l.startswith("FAIL") for l in lines), lines


def test_summarize_op_add_task_renders_title_and_pool(tmp_path):
    lines = _run_node_module_checks(tmp_path)
    assert any("summarizeOp shows title" in l and l.startswith("PASS") for l in lines), lines
    assert any("summarizeOp shows pool" in l and l.startswith("PASS") for l in lines), lines
    assert any("summarizeOp output has no raw JSON" in l and l.startswith("PASS") for l in lines), lines


def test_pane_child_sweep_no_bare_object_children(tmp_path):
    lines = _run_node_module_checks(tmp_path)
    assert any("no bare non-str children sites" in l and l.startswith("PASS") for l in lines), lines


# --- Composer submit chain: 'new' → focused session → active (null) ----------

_CHAIN_SCRIPT = r"""
import * as P from './plugin.js'

const out = []
const assert = (cond, msg) => out.push([!!cond, msg])

// 1. The chain order is the operator's preferred order: fresh draft first,
//    then the focused session, then the active composer (null) as last resort.
assert(JSON.stringify(P.composerSubmitChain('sess-1')) === JSON.stringify(['new', 'sess-1', null]), `chain order (got ${JSON.stringify(P.composerSubmitChain('sess-1'))})`)
assert(JSON.stringify(P.composerSubmitChain(null)) === JSON.stringify(['new', null]), `chain with no focused session (got ${JSON.stringify(P.composerSubmitChain(null))})`)
assert(JSON.stringify(P.composerSubmitChain('')) === JSON.stringify(['new', null]), 'empty session id is not submitted as an address')

// 2. submitComposer stops at the FIRST address that claims the text.
const tries = []
const claimedAt = async addr => { tries.push(addr); return addr === 'sess-1' }
const ok = await P.submitComposer('sess-1', '/task_add --repo r --title t', claimedAt)
assert(ok === true, 'submitComposer returns true when an address claims the text')
assert(JSON.stringify(tries) === JSON.stringify(['new', 'sess-1']), `submitComposer stops at the first claimant (tried ${JSON.stringify(tries)})`)

// 3. A stale focused session id fails closed: the chain still lands on the
//    active composer instead of dropping the op.
const tries2 = []
const ok2 = await P.submitComposer('stale-sess', 'text', async addr => { tries2.push(addr); return addr === null })
assert(ok2 === true, 'submitComposer falls through to the active composer')
assert(JSON.stringify(tries2) === JSON.stringify(['new', 'stale-sess', null]), `stale session falls through new → session → null (tried ${JSON.stringify(tries2)})`)

// 4. All three addresses failing closed → false (the caller reports failure).
const tries3 = []
const ok3 = await P.submitComposer('stale-sess', 'text', async addr => { tries3.push(addr); return false })
assert(ok3 === false, 'submitComposer returns false when every address fails closed')
assert(tries3.length === 3, `every address was attempted (tried ${JSON.stringify(tries3)})`)

// 5. A throwing submit is treated as a fail-closed answer, not a crash.
const ok4 = await P.submitComposer('sess-1', 'text', async addr => { if (addr === 'new') throw new Error('boom'); return addr === null })
assert(ok4 === true, 'a throwing submit fails closed and the chain continues')

// 6. Production path uses host.composer.submit with the same address order.
globalThis.__hostCalls = []
globalThis.__composerHandler = (addr) => addr === null
const ok5 = await P.submitComposer('sess-1', 'text')
globalThis.__composerHandler = null
const submits = globalThis.__hostCalls.filter(([k]) => k === 'composer.submit').map(([, a]) => a[0])
assert(ok5 === true, 'production submitComposer reached the active composer')
assert(JSON.stringify(submits) === JSON.stringify(['new', 'sess-1', null]), `production chain used host.composer.submit in order (got ${JSON.stringify(submits)})`)

// 6b. THE OPERATOR-REPORTED SCENARIO: no chat focused (sessionId null) and the
//     primary composer showing the new draft — the op must reach the agent
//     through the 'new' address instead of being blocked or erroring.
const storage2 = new Map()
const ctx2 = { storage: { get: (k, d) => (storage2.has(k) ? storage2.get(k) : d), set: (k, v) => storage2.set(k, v) } }
globalThis.__hostCalls = []
globalThis.__composerHandler = (addr) => addr === 'new'
const rec2 = await P.submitOp(ctx2, null, { kind: 'add-task', text: '/task_add --repo r --title t' })
globalThis.__composerHandler = null
const submits2 = globalThis.__hostCalls.filter(([k]) => k === 'composer.submit').map(([, a]) => a[0])
assert(rec2.status === 'awaiting agent', `a null session id still submits via the new draft (got ${rec2.status})`)
assert(JSON.stringify(submits2) === JSON.stringify(['new']), `the new address is the only one tried (got ${JSON.stringify(submits2)})`)
const notified2 = globalThis.__hostCalls.find(([k, a]) => k === 'notify' && a[0])
assert(!!notified2 && String(notified2[1][0].message).includes('submitted'), 'the successful op notified a submission')
const noFailureToast = globalThis.__hostCalls.some(([k, a]) => k === 'notify' && a[0] && String(a[0].message || '').startsWith('composer unreachable'))
assert(!noFailureToast, 'no composer-unreachable error when the new draft accepted the op')

// 6c. Gating: a null focused session id must NOT block the form; only an
//     UNKNOWN chat state (undefined) blocks it.
assert(P.composerSubmitBlocked(null) === false, 'a null session id does not block a submit')
assert(P.composerSubmitBlocked('sess-1') === false, 'a focused session id does not block a submit')
assert(P.composerSubmitBlocked(undefined) === true, 'an unknown chat state blocks a submit')

// 7. Failure copy: status 'failed', error 'composer unreachable', actionable message.
const storage = new Map()
const ctx = { storage: { get: (k, d) => (storage.has(k) ? storage.get(k) : d), set: (k, v) => storage.set(k, v) } }
globalThis.__hostCalls = []
globalThis.__composerOk = false
const rec = await P.submitOp(ctx, 'stale-sess', { kind: 'add-task', text: '/task_add --repo r --title t' })
globalThis.__composerOk = true
assert(rec.status === 'failed', `failed op keeps status failed (got ${rec.status})`)
assert(rec.error === 'composer unreachable', `failed op keeps the composer unreachable error (got ${rec.error})`)
const notify = globalThis.__hostCalls.find(([k, a]) => k === 'notify' && a[0])
assert(!!notify, 'failure emitted a notify call')
assert(String(notify[1][0].message).startsWith('composer unreachable'), `notify message starts with composer unreachable (got ${notify[1][0].message})`)
assert(String(notify[1][0].message).includes('open a chat (or click New chat) and retry'), `notify message is actionable (got ${notify[1][0].message})`)
assert(notify[1][0].detail === undefined || typeof notify[1][0].detail === 'string', `notify detail is a string (got ${JSON.stringify(notify[1][0].detail)})`)

// 8. No call site bypasses the helper.
const src = (await import('node:fs')).readFileSync('./plugin.js', 'utf8')
const direct = src.match(/host\.composer\.submit\(/g) || []
assert(direct.length === 1, `host.composer.submit is called only inside submitComposer (found ${direct.length})`)

let failed = 0
for (const [ok, msg] of out) { console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); if (!ok) failed++ }
console.log(failed === 0 ? 'ALL CHAIN CHECKS PASSED' : `${failed} CHAIN CHECK(S) FAILED`)
process.exit(failed ? 1 : 0)
"""


def test_no_composer_submission_sites_remain():
    src = (DESKTOP / "plugin.js").read_text()
    for token in ["submitComposer", "host.composer", "composerSubmit", "COMPOSER_UNREACHABLE", "noSession", "submitOp", "recordOp", "UNKNOWN_SESSION_ATOM", "pushDeliverPrompt", "pushGrantPrompt", "pushPackagePrompt", "retryLegality", "OperationLauncher", "ConfirmCard", "finalizeOp", "PRIMARY_BUTTONS"]:
        assert token not in src, f"composer/operation machinery still present: {token}"

def test_copied_status_record_without_submit():
    src = (DESKTOP / "plugin.js").read_text()
    assert "'copied'" in src
    m = re.search(r"const OP_STATUS_PILL = \{.*?\n\}", src, re.S)
    assert m and "copied: 'copied'" in m.group(0)
    # 'copied' records are written only by copyPrompt — never a composer path.
    assert "status: 'copied'" in src
    assert "status: 'submitted'" not in src and "status: 'awaiting agent'" not in src

def test_prompt_actions_manifest_covers_every_removed_op():
    src = (DESKTOP / "plugin.js").read_text()
    m = re.search(r"export const PROMPT_ACTIONS = \{.*?\n\}", src, re.S)
    assert m, "PROMPT_ACTIONS manifest missing"
    for actionId in ["add-repo-owner", "add-repo-link", "fetch-tasks", "claim", "claim-auto",
                     "push", "approve", "deny", "merge", "prune", "audit-show", "add-task"]:
        assert f"'{actionId}':" in m.group(0), actionId
    # Every manifest entry carries label + builder + where, and labels are unique.
    for key in re.findall(r"'([a-z-]+)': \{", m.group(0)):
        entry = re.search(r"'" + key + r"': \{.*?\}", m.group(0), re.S).group(0)
        assert "label:" in entry and "builder:" in entry and "where:" in entry
    labels = re.findall(r"label: '([^']+)'", m.group(0))
    assert len(labels) == len(set(labels))

def test_prompt_companions_carry_literal_commands_and_no_remote_urls():
    src = (DESKTOP / "plugin.js").read_text()
    prompts = src[src.index("// --- inlined from prompts.js"):src.index("// Contribution ids")]
    for needle in ["/add-repo", "/fetch task list", "/claim", "/push repo", "/task_add", "/audit show",
                   "scripts/pool-approve.py", "scripts/contributor-push.py", "scripts/contributor-add.py",
                   "scripts/contributor-claim.py", "Report the plugin's reply verbatim"]:
        assert needle in prompts, needle
    assert not re.search(r"https?://", prompts), "prompt companions must not carry remote URLs"


# --- Prompt-companion field set (restored input widgets, composition-only) ---

def test_add_repo_path_and_pool_compose_add_repo_prompt():
    src = (DESKTOP / "plugin.js").read_text()
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    for marker in ["Add a repo to the pool", "Copy /add-repo prompt", "repo id (optional", "'demo-pool'"]:
        assert marker in src, marker
    parts = []
    for pattern in [
        r"function str\(v\) \{.*?\n\}\n",
        r"export function addRepoOwnerPrompt\(\{ gateway, repoId, path, version \}\) \{.*?\n\}\n",
    ]:
        m = re.search(pattern, src, re.S)
        assert m, pattern
        parts.append(m.group(0))
    harness = "\n".join(parts).replace("export ", "")
    body = "console.log(JSON.stringify(addRepoOwnerPrompt({ gateway: 'default', repoId: 'my-repo', path: '/home/u/my-repo' })));\n"
    out = subprocess.run([node, "-e", harness + body], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    prompt = json.loads(out.stdout)
    assert "/add-repo my-repo --path /home/u/my-repo" in prompt
    assert "/fetch repo my-repo" in prompt


def test_add_task_repo_title_compose_task_add_prompt():
    src = (DESKTOP / "plugin.js").read_text()
    node, harness = _load_task_add_helpers(src)
    for marker in ["Open a task (ticket)", "Copy /task_add prompt"]:
        assert marker in src, marker
    body = "console.log(JSON.stringify(taskAddPrompt({ gateway: 'default', repoId: 'demo-repo', title: 'Fix the thing', brief: '', poolId: 'demo-pool' })));\n"
    out = subprocess.run([node, "-e", harness + body], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    prompt = json.loads(out.stdout)
    assert "/task_add --repo demo-repo" in prompt
    assert '--title "Fix the thing"' in prompt
    assert "--pool demo-pool" in prompt


def test_claim_open_ticket_field_and_auto_button_markers():
    src = (DESKTOP / "plugin.js").read_text()
    for marker in ["Claim a task", "open ticket", "state === 'open'", "Copy /claim prompt", "Copy auto-assign prompt"]:
        assert marker in src, marker


def test_prompt_companion_section_field_inventory_markers():
    src = (DESKTOP / "plugin.js").read_text()
    for marker in [
        "Push work (grant → package → deliver)",
        "Fetch the catalog",
        "Pull accepted bytes",
        "Approve or deny a version (maintainer-only)",
        "Merge an approved bundle (maintainer-only)",
        "Prune stale worktrees",
        "PromptCompanionSection",
        "Paste this in your Hermes chat",
        "Ask your agent — compose a prompt",
        "grantToken",
    ]:
        assert marker in src, marker
    assert "host.composer" not in src
    assert src.count("PASTE_HINT") >= 10

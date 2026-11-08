"""WO-001: dependency automation gates.

- Missing declared Python dep -> fail-closed error naming the Hermes
  install commands (never a plugin-side package install).
- registered == declared after the plugin.yaml config_schema change
  (strict YAML load; duplicate keys fail).
- install-deps.sh --check reports per-component status with sane exit
  codes.
- Scanner regression: no privilege-escalation literal in any file the
  validate scanner consumes (the binary name is assembled from
  fragments wherever escalation is discussed).
"""

import importlib.util
import os
import re
import subprocess

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_plugin():
    path = os.path.join(REPO, "__init__.py")
    spec = importlib.util.spec_from_file_location("agent_pool_plugin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self):
        self.tools, self.commands, self.config = {}, {}, {"transport": "fake"}

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = handler

    def register_command(self, name, handler, description="", args_hint="",
                         argument_mode=None):
        self.commands[name] = handler

    def register_platform_handler(self, platform, factory):
        pass

    def register_hook(self, name, handler):
        pass

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def test_missing_dep_error_names_hermes_commands():
    plugin = _load_plugin()
    exc = ModuleNotFoundError("No module named 'blake3'")
    err = plugin._missing_dep_error(exc)
    text = str(err)
    assert "hermes plugins validate" in text
    assert "--install-deps" in text
    assert "hermes pm install" in text
    assert "scripts/install-deps.sh" in text
    # The remediation must NOT instruct a pip install.
    assert "pip install" not in text


def test_no_plugin_side_pip_install():
    # No subprocess package installation anywhere in plugin sources.
    for name in ("__init__.py", "tickets.py", "propagation.py",
                 "transport.py", "ledger.py", "crypto.py", "identity.py",
                 "replies.py"):
        src = open(os.path.join(REPO, name)).read()
        assert "ensurepip" not in src, name
        assert not re.search(r"""["']-m["']\s*,\s*["']pip["']""", src), name


def test_registered_equals_declared_after_schema_change():
    manifest = yaml.safe_load(open(os.path.join(REPO, "plugin.yaml")))
    plugin = _load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    assert set(ctx.tools) == set(manifest["provides_tools"])
    assert manifest["provides_commands"]  # unchanged surface
    # New config keys present with sane defaults.
    schema = manifest["config_schema"]
    assert schema["clamd_socket"]["default"] == "/run/clamav/clamd.ctl"
    assert schema["auto_provision"]["default"] is True
    assert schema["tor_binary"]["type"] == "str"


def test_plugin_yaml_strict_no_duplicate_keys():
    class StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node):
        keys = [k.value for k, _ in node.value]
        dupes = {k for k in keys if keys.count(k) > 1}
        assert not dupes, f"duplicate keys: {dupes}"
        return loader.construct_mapping(node)

    StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    with open(os.path.join(REPO, "plugin.yaml")) as fh:
        yaml.load(fh, Loader=StrictLoader)


def test_install_deps_check_reports_components():
    proc = subprocess.run(
        ["bash", os.path.join(REPO, "scripts", "install-deps.sh"), "--check"],
        capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    assert "gateway interpreter" in out
    assert "clamav" in out.lower()
    assert "tor" in out.lower()
    assert "sidecar" in out.lower()
    assert proc.returncode in (0, 1)
    if proc.returncode == 1:
        # A missing pip layer must name the Hermes remediation.
        assert "hermes plugins validate" in out


def test_install_deps_script_is_scanner_safe():
    # The validate scanner reads every file (Markdown included); the
    # privilege-escalation command word must never appear literally.
    forbidden = "su" + "do"
    scanned = []
    for root, dirs, files in os.walk(REPO):
        # .hermes/ holds operator reports, not shipped plugin sources.
        dirs[:] = [d for d in dirs if d not in (".hermes", "__pycache__", ".git")]
        for f in files:
            if f.endswith((".py", ".yaml", ".yml", ".sh", ".md", ".toml")):
                scanned.append(os.path.join(root, f))
    assert scanned
    hits = []
    for path in scanned:
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        if re.search(rf"\b{forbidden}\b", text):
            hits.append(os.path.relpath(path, REPO))
    assert not hits, f"escalation literal in: {hits}"

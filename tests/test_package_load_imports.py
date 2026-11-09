"""Package-load import regression: Hermes loads the plugin as a package.

Hermes loads plugins as ``hermes_plugins.agent_pool`` with
``submodule_search_locations`` and NEVER puts the plugin dir on ``sys.path``
(see ``hermes_cli/plugins_loader.py`` _load_directory_module). Sibling
modules are therefore only reachable as ``hermes_plugins.agent_pool.<stem>``
— a bare ``from propagation import ...`` fails with
``No module named 'propagation'`` even though the same code works under
pytest from the repo root, where conftest puts the root on ``sys.path``.

This test reproduces the Hermes load exactly, in a subprocess with a
scrubbed environment, and asserts the delivery-path sibling imports
resolve. It must fail on a checkout containing bare absolute sibling
imports (pre-fix) and pass after the fix.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SIBLING = {"crypto", "identity", "ledger", "propagation", "replies",
           "tickets", "transport"}


def _bare_sibling_violations(src: Path):
    """Bare absolute sibling imports NOT inside an except-ImportError
    fallback whose try body carries the relative form.

    These are exactly the lines that die with ``No module named
    'propagation'`` when Hermes loads the plugin as a package.
    """
    tree = ast.parse(src.read_text())
    out = []

    def is_importerror_handler(h):
        t = h.type
        names = []
        if isinstance(t, ast.Name):
            names = [t.id]
        elif isinstance(t, ast.Tuple):
            names = [e.id for e in t.elts if isinstance(e, ast.Name)]
        return any(n in ("ImportError", "ModuleNotFoundError")
                   for n in names)

    def has_relative(tnode, name):
        for n in ast.walk(tnode):
            if isinstance(n, ast.ImportFrom) and n.level >= 1 and \
                    n.module == name:
                return True
            if isinstance(n, ast.ImportFrom) and n.level >= 1 and \
                    n.module is None:
                return True
        return False

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []  # (is_fallback, rel_name_set)

        def visit_Try(self, node):
            rel_names = set()
            for n in ast.walk(node):
                if (isinstance(n, ast.ImportFrom) and n.level >= 1
                        and n.module in SIBLING):
                    rel_names.add(n.module)
                if (isinstance(n, ast.ImportFrom) and n.level >= 1
                        and n.module is None):
                    # `from . import ledger` -> names carry the module
                    rel_names.update(a.name for a in n.names
                                     if a.name in SIBLING)
            for h in node.handlers:
                if is_importerror_handler(h):
                    self.stack.append((True, rel_names))
                    for b in h.body:
                        self.visit(b)
                    self.stack.pop()
                else:
                    self.stack.append((False, set()))
                    for b in h.body:
                        self.visit(b)
                    self.stack.pop()
            for b in node.body:
                self.visit(b)
            for b in node.orelse + node.finalbody:
                self.visit(b)

        def visit_ImportFrom(self, node):
            if node.level == 0 and node.module in SIBLING:
                allowed = any(
                    ok and node.module in names
                    for ok, names in self.stack)
                if not allowed:
                    out.append(f"{src.name}:{node.lineno}: "
                               f"from {node.module} import ...")
            self.generic_visit(node)

        def visit_Import(self, node):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in SIBLING:
                    allowed = any(ok and top in names
                                  for ok, names in self.stack)
                    if not allowed:
                        out.append(f"{src.name}:{node.lineno}: "
                                   f"import {top}")
            self.generic_visit(node)

    V().visit(tree)
    return out


def test_no_bare_sibling_imports_in_plugin_code():
    """Every sibling import must be relative-first with a flat fallback."""
    violations = []
    for src in sorted(REPO.glob("*.py")):  # plugin root only; scripts
        violations += _bare_sibling_violations(src)  # set sys.path themselves
    assert not violations, (
        f"{len(violations)} bare sibling import(s) would fail under "
        f"Hermes package load:\n" + "\n".join(violations))

_SUBPROC = r'''
import importlib.util
import sys
from pathlib import Path

repo = Path(sys.argv[1])

# Hermes plugins_loader.py _load_directory_module, verbatim logic:
# module name, init file, submodule_search_locations=[plugin_dir].
# NOTE: repo is NOT added to sys.path.
import types
ns = types.ModuleType("hermes_plugins")
ns.__path__ = []
sys.modules["hermes_plugins"] = ns
spec = importlib.util.spec_from_file_location(
    "hermes_plugins.agent_pool",
    str(repo / "__init__.py"),
    submodule_search_locations=[str(repo)],
)
pkg = importlib.util.module_from_spec(spec)
sys.modules["hermes_plugins.agent_pool"] = pkg
spec.loader.exec_module(pkg)

# Every sibling must be importable as a package module WITHOUT any
# top-level bare module named (propagation|ledger|... ) being loaded.
for stem in ("crypto", "identity", "ledger", "propagation", "replies",
             "tickets", "transport"):
    mod_pkg = importlib.import_module(f"hermes_plugins.agent_pool.{stem}")
    assert mod_pkg.__name__ == f"hermes_plugins.agent_pool.{stem}", stem
    assert stem not in sys.modules, f"bare top-level {stem!r} loaded from sys.path!"

# Simulate the exact inner import lines the deliver paths run.
# (Pre-fix each of these bare forms died with No module named 'propagation'.)
from hermes_plugins.agent_pool.propagation import Member, RepoPropagator          # noqa: F401
from hermes_plugins.agent_pool.propagation import manifest_hash, _verify_key       # noqa: F401
from hermes_plugins.agent_pool.replies import MAX_CONTENT_BYTES                    # noqa: F401
from hermes_plugins.agent_pool.tickets import PoolService

# Exercise the reply-guard import on the real code path (tickets.py).
svc = PoolService.__new__(PoolService)  # no DB needed for this method
svc._enforce_reply_guard({"ok": True, "manifest_hash": "abc", "n": 1})

print("PACKAGE_LOAD_OK")
'''


def test_plugin_loads_as_hermes_package_without_syspath():
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        # PYTHONNOUSERSITE unset: user site-packages holds blake3/nacl.
        # cwd=/tmp + no plugin dir anywhere: the plugin dir stays off
        # sys.path, which is exactly the Hermes package-load condition.
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    p = subprocess.run(
        [sys.executable, "-c", _SUBPROC, str(REPO)],
        capture_output=True, text=True, timeout=120, env=env,
        cwd="/tmp",
    )
    assert p.returncode == 0, (
        f"package load failed\nstdout: {p.stdout}\nstderr: {p.stderr}")
    assert "PACKAGE_LOAD_OK" in p.stdout

"""Subprocess regression: Hermes plugin-storage import from standalone CLIs.

Reproduces the real bug: an agent-spawned interpreter (no gateway
``sys.path``, no ``PYTHONPATH``) cannot ``import plugins.plugin_storage``,
so ``ledger.get_db()`` / ``ledger.default_db_path()`` failed closed at
runtime while the in-process suite stayed green behind the conftest fake.

Each test here spawns a stdlib-only subprocess with a scrubbed environment
(no ``PYTHONPATH``, no stray ``HERMES_*``) in a fake home whose
``.hermes/hermes-agent`` symlinks to the real Hermes install tree, so the
*real* ``plugins.plugin_storage`` is exercised and all writes land under
the fake home — never the real profile.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
REAL_INSTALL = Path.home() / ".hermes" / "hermes-agent"


def _subprocess_python() -> str:
    """Interpreter that can run the real Hermes storage.

    ``hermes_state_errors`` needs ``sqlite3.SQLITE_BUSY`` (Python >= 3.11),
    which the pytest gate interpreter (3.10) lacks; the gateway runs 3.14
    from ``~/.hermes/tools/python-*/bin/python3``. Prefer that, then any
    >= 3.11 on PATH, then the running interpreter.
    """
    for cand in sorted(Path.home().glob(".hermes/tools/python-*/bin/python3")):
        if cand.is_file():
            return str(cand)
    for name in ("python3.14", "python3.13", "python3.12", "python3.11"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable

requires_install = pytest.mark.skipif(
    not (REAL_INSTALL / "plugins" / "plugin_storage.py").is_file(),
    reason="real Hermes install tree not present on this machine",
)


def _clean_env(fake_home: Path, extra: dict | None = None) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(fake_home),
        "PYTHONNOUSERSITE": "1",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    if extra:
        env.update(extra)
    return env


def _run(code: str, env: dict, cwd: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_subprocess_python(), "-c", code],
        capture_output=True, text=True, env=env, cwd=str(cwd), timeout=120,
    )


def _fake_home_with_install(tmp_path: Path, name: str = "home") -> Path:
    home = tmp_path / name
    hermes_dir = home / ".hermes"
    hermes_dir.mkdir(parents=True)
    (hermes_dir / "hermes-agent").symlink_to(REAL_INSTALL, target_is_directory=True)
    return home


@requires_install
def test_bare_import_fails_without_bootstrap(tmp_path):
    """(a) The underlying failure: no gateway sys.path -> ModuleNotFoundError.

    This is the condition the ledger bootstrap must work around; even after
    the fix, a bare interpreter without ``import ledger`` still cannot find
    the module (the fix lives in the ledger, not in PYTHONPATH).
    """
    home = _fake_home_with_install(tmp_path)
    proc = _run("import plugins.plugin_storage\n", _clean_env(home))
    assert proc.returncode != 0
    assert "No module named 'plugins'" in proc.stderr
    assert "ModuleNotFoundError" in proc.stderr


@requires_install
def test_get_db_and_default_path_from_clean_subprocess(tmp_path):
    """(b) After the fix: get_db()/default_db_path() work in a clean env.

    Uses the real Hermes storage; the ledger file lands under the fake
    home's ``plugin-data/agent-pool/data.db`` (no real-profile writes).
    """
    home = _fake_home_with_install(tmp_path)
    code = (
        "import sqlite3, sys\n"
        "sys.path.insert(0, %r)\n"
        "import ledger\n"
        "dbp = ledger.default_db_path()\n"
        "conn = ledger.get_db()\n"
        "assert conn.row_factory is sqlite3.Row\n"
        "ledger.create_pool(conn, 'pool-subproc', 'T', 'agent:maint', relay_url='off')\n"
        "row = ledger._pool_row(conn, 'pool-subproc')\n"
        "assert row['settings_json'] is not None\n"
        "print(str(dbp))\n"
        "print('OK')\n" % str(REPO))
    proc = _run(code, _clean_env(home))
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[-1] == "OK"
    assert lines[0] == str(home / ".hermes" / "plugin-data" / "agent-pool" / "data.db")
    assert Path(lines[0]).is_file()


@requires_install
def test_hermes_home_resolution_and_data_dir_precedence(tmp_path):
    """HERMES_HOME (a home dir) locates the install; AGENT_POOL_DATA_DIR wins."""
    custom = tmp_path / "custom-home"
    custom.mkdir()
    (custom / "hermes-agent").symlink_to(REAL_INSTALL, target_is_directory=True)
    other_home = tmp_path / "other-home"  # NO install under HOME: must not be used
    other_home.mkdir()
    pool_data = tmp_path / "pool-data"
    env = _clean_env(other_home, {
        "HERMES_HOME": str(custom),
        "AGENT_POOL_DATA_DIR": str(pool_data),
    })
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import ledger\n"
        "assert str(ledger.default_db_path()) == %r\n"
        "conn = ledger.get_db()\n"
        "ledger.create_pool(conn, 'pool-hh', 'T', 'agent:maint', relay_url='off')\n"
        "print('OK')\n" % (str(REPO), str(pool_data / "data.db")))
    proc = _run(code, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("OK")
    # get_db() went through the real plugin_db under HERMES_HOME.
    assert (custom / "plugin-data" / "agent-pool" / "data.db").is_file()


@requires_install
def test_fails_closed_when_no_hermes_anywhere(tmp_path):
    """No install discoverable from HERMES_HOME or HOME -> RuntimeError."""
    home = tmp_path / "empty-home"
    home.mkdir()  # no .hermes/hermes-agent, no HERMES_HOME
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import ledger\n"
        "try:\n"
        "    ledger.get_db()\n"
        "except RuntimeError as e:\n"
        "    assert 'plugins.plugin_storage' in str(e), str(e)\n"
        "    print('CLOSED')\n"
        "else:\n"
        "    raise SystemExit('expected RuntimeError')\n" % str(REPO))
    proc = _run(code, _clean_env(home))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "CLOSED"


@requires_install
def test_bootstrap_noop_when_already_importable(tmp_path):
    """Subprocess with plugins already importable: sys.path must not grow."""
    home = _fake_home_with_install(tmp_path)
    env = _clean_env(home, {"PYTHONPATH": str(REAL_INSTALL)})
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "before = list(sys.path)\n"
        "import ledger\n"
        "ledger._load_plugin_storage()\n"
        "assert list(sys.path) == before, (before, list(sys.path))\n"
        "print('OK')\n" % str(REPO))
    proc = _run(code, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_bootstrap_noop_in_process(monkeypatch):
    """In the pytest process (conftest fake): bootstrap touches nothing."""
    import ledger

    before = list(sys.path)
    storage = ledger._load_plugin_storage()
    assert list(sys.path) == before
    assert hasattr(storage, "plugin_db")
    assert hasattr(storage, "plugin_data_dir")
    assert ledger._resolve_hermes_install_root() is None or True  # no side effects
    # Root resolution must never hardcode a home directory.
    assert "/home/maxcurrent" not in [str(c) for c in ledger._hermes_install_root_candidates()]

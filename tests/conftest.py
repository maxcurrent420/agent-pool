"""Test harness: fake Hermes ``plugins.plugin_storage``.

The production ledger path is Hermes plugin storage only
(``ledger.get_db()`` -> ``plugins.plugin_storage.plugin_db``), so the
suite must exercise that path, not a local-file fallback. This fake
reproduces the real Hermes behaviour faithfully: ``plugin_db`` returns
a connection with ``row_factory=None`` (plain tuple rows) — exactly
what Hermes' ``plugin_db`` does (it passes ``row_factory=None`` to
``open_db``). If ``ledger.get_db()`` ever stops setting
``sqlite3.Row`` itself, string-keyed row indexing breaks and the
regression test in ``test_storage_row_factory.py`` fails red.
"""
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_plugin_data_dir(name):
    base = Path(os.environ.get("AGENT_POOL_DATA_DIR") or Path.cwd() / ".agent-pool-test-data")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _fake_plugin_db(name, filename="data.db"):
    # Mirror Hermes plugins.plugin_storage.plugin_db: file-backed sqlite
    # connection with row_factory=None (tuple rows) unless the caller sets
    # it. The caller (ledger.get_db) must set sqlite3.Row itself.
    path = _fake_plugin_data_dir(name) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = None
    return conn


_plugins_pkg = types.ModuleType("plugins")
_plugins_pkg.__path__ = []  # mark as package
_storage = types.ModuleType("plugins.plugin_storage")
_storage.plugin_data_dir = _fake_plugin_data_dir
_storage.plugin_db = _fake_plugin_db
_plugins_pkg.plugin_storage = _storage
sys.modules.setdefault("plugins", _plugins_pkg)
sys.modules.setdefault("plugins.plugin_storage", _storage)


@pytest.fixture(autouse=True)
def _isolated_agent_pool_data_dir(tmp_path, monkeypatch):
    """Per-test isolated storage dir (per-profile ledger isolation)."""
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "agent-pool-data"))
    # Registration hard-fails without a maintainer bot Discord id
    # (config resolution, Task 2). The suite runs with one configured,
    # exactly like a real deployment; tests that exercise the missing-id
    # hard-fail delenv it explicitly.
    monkeypatch.setenv("AGENT_POOL_MAINTAINER_BOT_DISCORD_USER_ID", "maint-bot-1")

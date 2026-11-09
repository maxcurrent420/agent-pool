"""Regression: live row_factory bug (tuple rows from Hermes plugin_db).

Hermes ``plugin_db`` returns ``row_factory=None`` connections; ledger
code indexes rows with string keys (``_pool_row`` -> ``row["settings_json"]``).
``get_db()`` must set ``sqlite3.Row`` on the storage connection itself,
and must fail closed (never a local file) if Hermes storage is absent.
"""
import sqlite3
import sys
import types

import pytest

import ledger as L


def test_fake_storage_returns_tuple_rows_by_default():
    # Fidelity guard: the fake must reproduce real Hermes behaviour, or
    # this regression test would pass vacuously.
    from plugins.plugin_storage import plugin_db

    raw = plugin_db("agent-pool")
    assert raw.row_factory is None
    raw.execute("CREATE TABLE IF NOT EXISTS _probe (id TEXT)")
    raw.execute("INSERT INTO _probe (id) VALUES ('x')")
    raw.commit()
    row = raw.execute("SELECT id FROM _probe").fetchone()
    assert isinstance(row, tuple)
    with pytest.raises(TypeError):
        row["id"]
    raw.close()


def test_get_db_row_factory_is_sqlite_row():
    conn = L.get_db()
    assert conn.row_factory is sqlite3.Row


def test_storage_path_string_keyed_rows_work():
    conn = L.get_db()
    L.create_pool(conn, "pool-1", "Team", "agent:maint", relay_url="off")
    row = L._pool_row(conn, "pool-1")
    assert row["settings_json"] is not None  # string-keyed access works
    assert L.is_frozen(conn, "pool-1") is False
    L.set_frozen(conn, "pool-1", True)
    assert L.is_frozen(conn, "pool-1") is True


def test_get_db_fails_closed_without_hermes_storage(monkeypatch):
    broken = types.ModuleType("plugins.plugin_storage")

    def _boom(name, filename="data.db"):
        raise ModuleNotFoundError("No module named 'plugins' (simulated)")

    broken.plugin_db = _boom
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", broken)
    with pytest.raises(RuntimeError, match="Hermes plugin-storage"):
        L.get_db()

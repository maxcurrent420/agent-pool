"""Regression: the plugin's own connections must enforce declared FKs.

SQLite defaults ``PRAGMA foreign_keys`` OFF per connection, so the
schema's ``REFERENCES`` clauses (e.g. ``audits.repo_id REFERENCES
repos(repo_id)``) were inert on any connection that never opted in —
the pytest fake in ``conftest.py`` and any other plugin-owned path
that skipped the pragma. A live gateway surfaced a latent ordering
bug only because Hermes' ``open_db`` happened to enable FKs; the
plugin must own enforcement itself.

These tests assert, through the plugin's own paths, that:
  * ``ledger.get_db()`` connections report ``PRAGMA foreign_keys == 1``,
  * ``ledger.init_ledger()`` turns it on for a raw connection too
    (covers ``scripts/demo.py`` and the in-memory test helpers),
  * a child insert with a missing parent raises ``IntegrityError``
    via the plugin's insert path (``create_ticket`` -> ``tickets``).
"""
import sqlite3

import pytest

import ledger as L


def test_get_db_connection_has_foreign_keys_enabled():
    conn = L.get_db()
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_init_ledger_enables_foreign_keys_on_raw_connection():
    conn = sqlite3.connect(":memory:")
    # SQLite default: OFF — proves the plugin does the enabling.
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    L.init_ledger(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_child_insert_with_missing_parent_is_rejected():
    conn = L.get_db()
    # tickets.pool_id -> pools(pool_id) and tickets.repo_id ->
    # repos(repo_id): neither parent exists, so the plugin's own
    # insert path must raise instead of silently writing an orphan.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        L.create_ticket(conn, "t-orphan", "pool-missing", "repo-missing",
                        "Orphan ticket")
    # Nothing was written.
    assert L.get_ticket(conn, "t-orphan") is None


def test_audit_insert_with_missing_repo_is_rejected():
    conn = L.get_db()
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO audits (audit_id, repo_id, version, report_hash,"
            " created_at) VALUES (?,?,?,?,?)",
            ("audit-orphan", "repo-missing", 1, "deadbeef", 0))
        conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM audits").fetchone()[0] == 0

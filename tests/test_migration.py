"""Phase 6: 2.6.0 ticket-state migration (WO-017).

Old states map onto the three-state machine: pending->open,
review->claimed, blocked->open, merged->completed. The migration is
idempotent (guarded by meta.state_migration) and writes one
state_migrated_2_6_0 event per migrated row. No tables/rows dropped.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ledger import create_pool, get_ticket, init_ledger

OLD_TO_NEW = {
    "t-pend": ("pending", "open"),
    "t-rev": ("review", "claimed"),
    "t-block": ("blocked", "open"),
    "t-merged": ("merged", "completed"),
    "t-open": ("open", "open"),
    "t-claimed": ("claimed", "claimed"),
}


def _legacy_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_ledger(c)
    c.execute("DELETE FROM meta WHERE key='state_migration'")
    create_pool(c, "pool-1", "Team", "agent:maint")
    c.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
              " VALUES ('r','pool-1','R',0)")
    for tid, (old, _new) in OLD_TO_NEW.items():
        c.execute(
            "INSERT INTO tickets (ticket_id, pool_id, repo_id, title, state,"
            " created_at, updated_at) VALUES (?,?,?,?,?,0,0)",
            (tid, "pool-1", "r", tid, old))
    c.commit()
    return c


def test_state_migration_maps_old_states():
    c = _legacy_db()
    init_ledger(c)  # runs the 2.6.0 migration
    for tid, (_old, want) in OLD_TO_NEW.items():
        assert get_ticket(c, tid)["state"] == want, tid


def test_state_migration_events_and_guard():
    c = _legacy_db()
    init_ledger(c)
    kinds = [r[0] for r in c.execute(
        "SELECT kind FROM events WHERE kind='state_migrated_2_6_0'")]
    assert len(kinds) == 4  # one per migrated row (old states only)
    guard = c.execute(
        "SELECT value FROM meta WHERE key='state_migration'").fetchone()[0]
    assert guard == "2.6.0"


def test_state_migration_idempotent():
    c = _legacy_db()
    init_ledger(c)
    before = [tuple(r) for r in c.execute(
        "SELECT ticket_id, state FROM tickets ORDER BY ticket_id")]
    events_before = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    init_ledger(c)
    init_ledger(c)
    after = [tuple(r) for r in c.execute(
        "SELECT ticket_id, state FROM tickets ORDER BY ticket_id")]
    assert after == before
    assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before


def test_migration_is_not_destructive():
    c = _legacy_db()
    init_ledger(c)
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # WO-019: pool_chats is no longer created; remote_repos is kept for
    # back-compat. No destructive migration either way.
    assert "remote_repos" in tables
    assert "pool_chats" not in tables
    assert c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == len(OLD_TO_NEW)

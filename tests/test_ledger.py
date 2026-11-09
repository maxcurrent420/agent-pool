import sqlite3

import pytest

from crypto import build_manifest, manifest_hash, sign_manifest, generate_signing_key
from ledger import (
    TABLES, add_member, append_manifest, claim_ticket, create_pool,
    create_ticket, init_ledger, record_event,
)


@pytest.fixture
def conn():
    c = init_ledger(sqlite3.connect(":memory:"))
    create_pool(c, "pool-1", "StaticEcho", "agent:maintainer", relay_url="off")
    c.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
              " VALUES ('static-echo','pool-1','StaticEcho',0)")
    c.commit()
    return c


def test_schema_tables_exist(conn):
    found = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in TABLES:
        assert table in found, f"missing table {table}"


def test_members_and_manifest_chain(conn):
    add_member(conn, "pool-1", "agent:alice", role="member", box_pubkey_hex="ab" * 32)
    sk = generate_signing_key()
    body = build_manifest(repo_id="static-echo", version=1,
                          content_hash="0" * 64, size_bytes=10,
                          signer_key_id="agent:maintainer",
                          submitter_key_id="agent:maintainer", state="submitted")
    signed = sign_manifest(sk, body)
    append_manifest(conn, "static-echo", signed, manifest_hash(signed))
    rows = conn.execute("SELECT version, state FROM repo_versions").fetchall()
    assert rows == [(1, "submitted")]
    chain = conn.execute(
        "SELECT signer_key_id, prev_manifest_hash FROM manifest_chain").fetchall()
    assert chain[0][0] == "agent:maintainer" and chain[0][1] is None
    members = conn.execute("SELECT key_id, role FROM members").fetchall()
    assert members == [("agent:alice", "member")]


def test_one_ticket_one_worker(conn):
    create_ticket(conn, "t-1", "pool-1", "static-echo", "Add endpoint",
                  brief="mandate", budget={"max_turns": 10})
    claim_ticket(conn, "c-1", "t-1", "agent:alice", declared_paths=["src/a.py"])
    with pytest.raises(sqlite3.IntegrityError):
        claim_ticket(conn, "c-2", "t-1", "agent:bob", declared_paths=["src/b.py"])
    ticket = conn.execute("SELECT state, claimant_key_id FROM tickets WHERE ticket_id='t-1'").fetchone()
    assert tuple(ticket) == ("claimed", "agent:alice")
    files = conn.execute("SELECT path FROM file_claims").fetchall()
    assert files == [("src/a.py",)]


def test_events_recorded(conn):
    record_event(conn, "pool-1", "pool_created", {"pool_id": "pool-1"})
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events")]
    assert kinds == ["pool_created"]


def test_one_pool_per_gateway(conn):
    with pytest.raises(ValueError, match="one pool per gateway"):
        create_pool(conn, "pool-2", "Second", "agent:maintainer")


def test_ticket_states_and_illegal_transitions(conn):
    conn.row_factory = sqlite3.Row
    from ledger import TICKET_STATES, _TRANSITIONS, set_ticket_state
    assert TICKET_STATES == ("open", "claimed", "completed")
    assert _TRANSITIONS == {"open": {"claimed"},
                             "claimed": {"open", "completed"},
                             "completed": set()}
    create_ticket(conn, "t-1", "pool-1", "static-echo", "Lifecycle")
    with pytest.raises(ValueError, match="unknown ticket state"):
        create_ticket(conn, "t-2", "pool-1", "static-echo", "Bad", state="pending")
    with pytest.raises(ValueError, match="not allowed"):
        set_ticket_state(conn, "t-1", "completed")


def test_pool_gateways_record(conn):
    from ledger import list_gateways, record_gateway
    assert list_gateways(conn) == []
    record_gateway(conn, "pool-x", gateway_endpoint="gw:1", transport_addr="t:1", role="member")
    rows = list_gateways(conn)
    assert rows[0]["pool_id"] == "pool-x" and rows[0]["gateway_endpoint"] == "gw:1"
    record_gateway(conn, "pool-x", role="contributor")
    assert list_gateways(conn)[0]["role"] == "contributor"


def test_state_migration_idempotent():
    from ledger import get_ticket
    c = init_ledger(sqlite3.connect(":memory:"))
    c.row_factory = sqlite3.Row
    # Simulate a pre-2.6.0 DB: clear the guard and insert old-state rows
    # directly (create_ticket now refuses old states).
    c.execute("DELETE FROM meta WHERE key='state_migration'")
    create_pool(c, "pool-1", "Team", "agent:maint")
    c.execute("INSERT INTO repos (repo_id, pool_id, name, created_at) VALUES ('r','pool-1','R',0)")
    for tid, old in (("t-pend", "pending"), ("t-rev", "review"),
                     ("t-block", "blocked"), ("t-merged", "merged"),
                     ("t-open", "open"), ("t-claimed", "claimed")):
        c.execute("INSERT INTO tickets (ticket_id, pool_id, repo_id, title, state, created_at, updated_at)"
                  " VALUES (?,?,?,?,?,0,0)", (tid, "pool-1", "r", tid, old))
    c.commit()
    init_ledger(c)  # runs the 2.6.0 migration
    expect = {"t-pend": "open", "t-rev": "claimed", "t-block": "open",
              "t-merged": "completed", "t-open": "open", "t-claimed": "claimed"}
    for tid, want in expect.items():
        assert get_ticket(c, tid)["state"] == want, tid
    kinds = [r[0] for r in c.execute("SELECT kind FROM events WHERE kind='state_migrated_2_6_0'")]
    assert len(kinds) == 4  # one record_event per migrated row
    guard = c.execute("SELECT value FROM meta WHERE key='state_migration'").fetchone()[0]
    assert guard == "2.6.0"
    # Run twice more: no further rows, no further events (idempotent).
    init_ledger(c); init_ledger(c)
    for tid, want in expect.items():
        assert get_ticket(c, tid)["state"] == want, tid
    kinds2 = [r[0] for r in c.execute("SELECT kind FROM events WHERE kind='state_migrated_2_6_0'")]
    assert kinds2 == kinds
    # Old tables stay on disk (no destructive migration). pool_chats is no
    # longer created (WO-019); remote_repos is kept for back-compat.
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "remote_repos" in tables
    assert "pool_chats" not in tables


class TestBindMembership:
    """Task 1: binding is explicit and immutable — a conflicting re-bind
    hard-fails; same-key re-bind is an idempotent no-op."""

    def test_fresh_bind_inserts(self, conn):
        from ledger import bind_membership, lookup_membership_key
        bind_membership(conn, "pool-1", "user-1", "agent:alice")
        assert lookup_membership_key(conn, "user-1", pool_id="pool-1") == "agent:alice"

    def test_same_key_rebind_is_noop(self, conn):
        from ledger import bind_membership, lookup_membership_key
        bind_membership(conn, "pool-1", "user-1", "agent:alice")
        bind_membership(conn, "pool-1", "user-1", "agent:alice")  # no raise
        assert lookup_membership_key(conn, "user-1", pool_id="pool-1") == "agent:alice"

    def test_conflicting_rebind_raises_and_first_binding_unchanged(self, conn):
        from ledger import bind_membership, lookup_membership_key
        bind_membership(conn, "pool-1", "user-1", "agent:alice")
        with pytest.raises(ValueError, match="membership conflict"):
            bind_membership(conn, "pool-1", "user-1", "agent:bob")
        assert lookup_membership_key(conn, "user-1", pool_id="pool-1") == "agent:alice"

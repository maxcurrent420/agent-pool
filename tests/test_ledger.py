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

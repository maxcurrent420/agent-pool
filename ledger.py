"""SQLite ledger for Agent Pool, stored via Hermes ``plugin_db()``.

Schema (Phase 1): pools, members, repos, repo_versions, manifest_chain,
tickets, claims, file_claims, audits, events (+ local identities metadata).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
    pool_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    maintainer_key_id TEXT NOT NULL,
    relay_url TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    pool_id TEXT NOT NULL REFERENCES pools(pool_id),
    key_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    box_pubkey_hex TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (pool_id, key_id)
);
CREATE TABLE IF NOT EXISTS identities (
    key_id TEXT PRIMARY KEY,
    verify_key_hex TEXT NOT NULL,
    box_pubkey_hex TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
    repo_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES pools(pool_id),
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repo_versions (
    repo_id TEXT NOT NULL REFERENCES repos(repo_id),
    version INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted',
    PRIMARY KEY (repo_id, version)
);
CREATE TABLE IF NOT EXISTS manifest_chain (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL REFERENCES repos(repo_id),
    version INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL,
    prev_manifest_hash TEXT,
    signer_key_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    UNIQUE (repo_id, version)
);
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES pools(pool_id),
    repo_id TEXT NOT NULL REFERENCES repos(repo_id),
    title TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    claimant_key_id TEXT,
    budget_json TEXT NOT NULL DEFAULT '{}',
    thread_ref TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    key_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    declared_paths_json TEXT NOT NULL DEFAULT '[]',
    budget_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_claim_per_ticket
    ON claims (ticket_id) WHERE state = 'active';
CREATE TABLE IF NOT EXISTS file_claims (
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    path TEXT NOT NULL,
    key_id TEXT NOT NULL,
    PRIMARY KEY (ticket_id, path)
);
CREATE TABLE IF NOT EXISTS audits (
    audit_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL REFERENCES repos(repo_id),
    version INTEGER NOT NULL,
    report_hash TEXT NOT NULL,
    verdict TEXT NOT NULL DEFAULT 'pending',
    report_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
"""

TABLES = ("pools", "members", "identities", "repos", "repo_versions",
          "manifest_chain", "tickets", "claims", "file_claims", "audits",
          "events")


def init_ledger(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def default_db_path() -> Path:
    override = os.environ.get("AGENT_POOL_DATA_DIR")
    base = Path(override) if override else Path.home() / ".hermes" / "plugin-data" / "agent-pool"
    base.mkdir(parents=True, exist_ok=True)
    return base / "data.db"


def get_db() -> sqlite3.Connection:
    """Open the ledger via Hermes plugin_db(); fall back to a local SQLite file
    when running outside Hermes (tests / standalone)."""
    try:
        from plugins.plugin_storage import plugin_db
        conn = plugin_db("agent-pool")
    except Exception:
        conn = sqlite3.connect(str(default_db_path()), check_same_thread=False)
        conn.row_factory = sqlite3.Row
    return init_ledger(conn)


def _now() -> float:
    return time.time()


def record_event(conn, pool_id, kind, payload=None) -> None:
    conn.execute(
        "INSERT INTO events (pool_id, kind, payload_json, created_at) VALUES (?,?,?,?)",
        (pool_id, kind, json.dumps(payload or {}), _now()))
    conn.commit()


def create_pool(conn, pool_id, name, maintainer_key_id, relay_url="", settings=None) -> None:
    conn.execute(
        "INSERT INTO pools (pool_id, name, maintainer_key_id, relay_url, settings_json, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (pool_id, name, maintainer_key_id, relay_url,
         json.dumps(settings or {}), _now()))
    conn.commit()


def add_member(conn, pool_id, key_id, role="member", box_pubkey_hex="") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO members (pool_id, key_id, role, box_pubkey_hex)"
        " VALUES (?,?,?,?)", (pool_id, key_id, role, box_pubkey_hex))
    conn.commit()


def append_manifest(conn, repo_id, signed_manifest, manifest_hash_value) -> None:
    """Append a signed manifest to repo_versions + manifest_chain atomically."""
    conn.execute(
        "INSERT INTO repo_versions (repo_id, version, manifest_json, manifest_hash, state)"
        " VALUES (?,?,?,?,?)",
        (repo_id, signed_manifest["version"], json.dumps(signed_manifest),
         manifest_hash_value, signed_manifest.get("state", "submitted")))
    conn.execute(
        "INSERT INTO manifest_chain (repo_id, version, manifest_hash, prev_manifest_hash,"
        " signer_key_id, signature) VALUES (?,?,?,?,?,?)",
        (repo_id, signed_manifest["version"], manifest_hash_value,
         signed_manifest.get("prev_manifest_hash"),
         signed_manifest["signer_key_id"], signed_manifest.get("signature", "")))
    conn.commit()


def create_ticket(conn, ticket_id, pool_id, repo_id, title, brief="",
                  budget=None, thread_ref=None) -> None:
    conn.execute(
        "INSERT INTO tickets (ticket_id, pool_id, repo_id, title, brief, state,"
        " budget_json, thread_ref, created_at, updated_at) VALUES (?,?,?,?,?,'open',?,?,?,?)",
        (ticket_id, pool_id, repo_id, title, brief, json.dumps(budget or {}),
         thread_ref, _now(), _now()))
    conn.commit()


def claim_ticket(conn, claim_id, ticket_id, key_id, declared_paths=None, budget=None) -> None:
    """One-ticket-one-worker: the partial unique index refuses a second active claim."""
    conn.execute(
        "INSERT INTO claims (claim_id, ticket_id, key_id, state, declared_paths_json,"
        " budget_json, created_at) VALUES (?,?,?,'active',?,?,?)",
        (claim_id, ticket_id, key_id, json.dumps(list(declared_paths or [])),
         json.dumps(budget or {}), _now()))
    conn.execute(
        "UPDATE tickets SET state='claimed', claimant_key_id=?, updated_at=? WHERE ticket_id=?",
        (key_id, _now(), ticket_id))
    for path in declared_paths or []:
        conn.execute(
            "INSERT INTO file_claims (ticket_id, path, key_id) VALUES (?,?,?)",
            (ticket_id, path, key_id))
    conn.commit()

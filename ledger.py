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
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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

# --- Phase 3: ticket coordination (freeze, overlap, budgets, lifecycle) -----

TICKET_STATES = ("open", "claimed", "review", "merged", "blocked")
# Allowed transitions enforced by set_ticket_state().
_TRANSITIONS = {
    "open": {"claimed", "blocked"},
    "claimed": {"review", "open", "blocked"},
    "review": {"merged", "blocked", "review"},
    "blocked": {"open", "review"},
    "merged": set(),
}
BUDGET_FIELDS = ("max_turns", "max_tasks_per_day", "token_cap", "wall_clock_seconds")
BUDGET_DEFAULTS = {"max_turns": 25, "max_tasks_per_day": 5, "token_cap": 50000,
                   "wall_clock_seconds": 1800}


class ClaimRefused(Exception):
    """A claim violated a ledger rule (double-claim, overlap, frozen, no paths)."""


def is_frozen(conn, pool_id) -> bool:
    row = _pool_row(conn, pool_id)
    settings = json.loads(row["settings_json"] or "{}")
    return bool(settings.get("frozen"))


def _pool_row(conn, pool_id):
    row = conn.execute("SELECT * FROM pools WHERE pool_id=?", (pool_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown pool: {pool_id!r}")
    return row


def set_frozen(conn, pool_id, frozen: bool) -> bool:
    row = _pool_row(conn, pool_id)
    settings = json.loads(row["settings_json"] or "{}")
    settings["frozen"] = bool(frozen)
    conn.execute("UPDATE pools SET settings_json=? WHERE pool_id=?",
                 (json.dumps(settings), pool_id))
    conn.commit()
    record_event(conn, pool_id, "pool_frozen" if frozen else "pool_unfrozen",
                 {"frozen": bool(frozen)})
    return bool(frozen)


# --- Global kill-switch (Phase 6) -------------------------------------------
# Stored in the meta table so it spans every pool in the process (and survives
# restarts — it is ledger state, not process state). While set, every
# pool-mutating and claim/work/integrate action is refused; only the freeze
# command itself (to unfreeze) is allowed through. Per-pool freeze remains
# available for a single pool.

_GLOBAL_FROZEN_KEY = "global_frozen"


def is_global_frozen(conn) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (_GLOBAL_FROZEN_KEY,)).fetchone()
    except Exception:  # pre-Phase-6 ledger without the meta table
        return False
    return bool(row and row[0] == "1")


def set_global_frozen(conn, frozen: bool) -> bool:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                 (_GLOBAL_FROZEN_KEY, "1" if frozen else "0"))
    conn.commit()
    record_event(conn, None, "global_frozen" if frozen else "global_unfrozen",
                 {"frozen": bool(frozen)})
    return bool(frozen)


def is_frozen_any(conn, pool_id) -> bool:
    """True if the pool is frozen per-pool OR the global kill-switch is set."""
    return is_global_frozen(conn) or is_frozen(conn, pool_id)


def check_not_frozen(conn, pool_id) -> None:
    """Raise ClaimRefused if any freeze (global or per-pool) blocks *pool_id*."""
    if is_global_frozen(conn):
        raise ClaimRefused("global kill-switch is set: all pools frozen "
                           "(unfreeze with /pool freeze --unfreeze)")
    if is_frozen(conn, pool_id):
        raise ClaimRefused(f"pool {pool_id!r} is frozen")


def normalize_budget(budget=None, defaults=None) -> dict:
    """Fill every budget field from defaults; reject non-positive values."""
    base = dict(BUDGET_DEFAULTS if defaults is None else defaults)
    out = {}
    for field in BUDGET_FIELDS:
        value = (budget or {}).get(field, base.get(field))
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"budget field {field!r} must be a positive int, got {value!r}")
        out[field] = value
    return out


def get_ticket(conn, ticket_id):
    row = conn.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def get_ticket_or_raise(conn, ticket_id) -> dict:
    ticket = get_ticket(conn, ticket_id)
    if ticket is None:
        raise ValueError(f"unknown ticket: {ticket_id!r}")
    return ticket


def list_tickets(conn, pool_id=None, state=None) -> list:
    query, params = "SELECT * FROM tickets WHERE 1=1", []
    if pool_id:
        query += " AND pool_id=?"; params.append(pool_id)
    if state:
        if state not in TICKET_STATES:
            raise ValueError(f"unknown ticket state: {state!r}")
        query += " AND state=?"; params.append(state)
    query += " ORDER BY created_at, ticket_id"
    return [dict(r) for r in conn.execute(query, params)]


def list_members(conn, pool_id) -> list:
    _pool_row(conn, pool_id)
    rows = conn.execute(
        "SELECT key_id, role, box_pubkey_hex FROM members WHERE pool_id=? ORDER BY key_id",
        (pool_id,)).fetchall()
    return [dict(r) for r in rows]


def ensure_repo(conn, repo_id, pool_id, name=None) -> None:
    _pool_row(conn, pool_id)
    conn.execute(
        "INSERT OR IGNORE INTO repos (repo_id, pool_id, name, created_at) VALUES (?,?,?,?)",
        (repo_id, pool_id, name or repo_id, _now()))
    conn.commit()


def set_ticket_state(conn, ticket_id, state) -> dict:
    """Move a ticket through the lifecycle; invalid jumps raise ValueError."""
    if state not in TICKET_STATES:
        raise ValueError(f"unknown ticket state: {state!r}")
    ticket = get_ticket_or_raise(conn, ticket_id)
    if state not in _TRANSITIONS.get(ticket["state"], set()):
        raise ValueError(
            f"ticket {ticket_id!r}: transition {ticket['state']!r} -> {state!r} not allowed")
    conn.execute("UPDATE tickets SET state=?, updated_at=? WHERE ticket_id=?",
                 (state, _now(), ticket_id))
    conn.commit()
    record_event(conn, ticket["pool_id"], "ticket_state",
                 {"ticket_id": ticket_id, "from": ticket["state"], "to": state})
    return get_ticket_or_raise(conn, ticket_id)


def _overlaps(conn, repo_id, ticket_id, paths) -> list:
    """Declared paths of OTHER tickets on the same repo that are still live."""
    rows = conn.execute(
        "SELECT fc.path, fc.ticket_id FROM file_claims fc"
        " JOIN tickets t ON t.ticket_id = fc.ticket_id"
        " WHERE t.repo_id=? AND fc.ticket_id<>? AND t.state IN ('claimed','review','blocked')",
        (repo_id, ticket_id)).fetchall()
    claimed = {r[0] for r in rows}
    return sorted(p for p in paths if p in claimed)


def claim_ticket_enforced(conn, claim_id, ticket_id, key_id, declared_paths=None,
                          budget=None, maintainer_override=False) -> dict:
    """Ledger-enforced claim: frozen check, paths required, overlap refused,
    one-ticket-one-worker, budget persisted. Returns the claim row."""
    paths = [str(p).strip() for p in (declared_paths or []) if str(p).strip()]
    if not paths:
        raise ClaimRefused("claim refused: claimant must declare file paths")
    ticket = get_ticket_or_raise(conn, ticket_id)
    if ticket["state"] != "open":
        raise ClaimRefused(
            f"claim refused: ticket {ticket_id!r} is {ticket['state']!r}, not open "
            "(one-ticket-one-worker)")
    if is_global_frozen(conn):
        raise ClaimRefused("claim refused: global kill-switch is set")
    if is_frozen(conn, ticket["pool_id"]):
        raise ClaimRefused(f"claim refused: pool {ticket['pool_id']!r} is frozen")
    overlaps = _overlaps(conn, ticket["repo_id"], ticket_id, paths)
    if overlaps and not maintainer_override:
        raise ClaimRefused(
            f"claim refused: paths overlap other tickets on repo "
            f"{ticket['repo_id']!r}: {', '.join(overlaps)}")
    if overlaps and maintainer_override:
        pool = _pool_row(conn, ticket["pool_id"])
        if pool["maintainer_key_id"] != key_id:
            raise ClaimRefused(
                "claim refused: only the pool maintainer may override a path overlap")
    full_budget = normalize_budget(budget)
    try:
        # claim_ticket() inserts the claim + file_claims and flips the ticket.
        claim_ticket(conn, claim_id, ticket_id, key_id,
                     declared_paths=paths, budget=full_budget)
    except sqlite3.IntegrityError as exc:  # double-claim race / dup paths
        raise ClaimRefused(f"claim refused: {exc}") from exc
    if overlaps:
        record_event(conn, ticket["pool_id"], "claim_override",
                     {"ticket_id": ticket_id, "key_id": key_id, "paths": overlaps})
    else:
        record_event(conn, ticket["pool_id"], "claimed",
                     {"ticket_id": ticket_id, "key_id": key_id, "paths": paths})
    row = conn.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    return dict(row)


def release_ticket(conn, ticket_id, key_id) -> dict:
    """Return a claimed ticket to open; frees the claim and file claims."""
    ticket = get_ticket_or_raise(conn, ticket_id)
    if ticket["state"] != "claimed":
        raise ClaimRefused(f"release refused: ticket {ticket_id!r} is {ticket['state']!r}, not claimed")
    if ticket["claimant_key_id"] != key_id:
        raise ClaimRefused("release refused: only the claimant may release the ticket")
    if is_global_frozen(conn):
        raise ClaimRefused("release refused: global kill-switch is set")
    if is_frozen(conn, ticket["pool_id"]):
        raise ClaimRefused(f"release refused: pool {ticket['pool_id']!r} is frozen")
    conn.execute("UPDATE claims SET state='released' WHERE ticket_id=? AND state='active'",
                 (ticket_id,))
    conn.execute("DELETE FROM file_claims WHERE ticket_id=?", (ticket_id,))
    conn.execute(
        "UPDATE tickets SET state='open', claimant_key_id=NULL, updated_at=? WHERE ticket_id=?",
        (_now(), ticket_id))
    conn.commit()
    record_event(conn, ticket["pool_id"], "released",
                 {"ticket_id": ticket_id, "key_id": key_id})
    return get_ticket_or_raise(conn, ticket_id)


def get_active_claim(conn, ticket_id) -> dict | None:
    row = conn.execute(
        "SELECT * FROM claims WHERE ticket_id=? AND state='active'", (ticket_id,)).fetchone()
    return dict(row) if row else None


def get_latest_accepted_manifest(conn, repo_id) -> dict | None:
    """Latest maintainer-accepted manifest for *repo_id* (chain tip), or None."""
    row = conn.execute(
        "SELECT manifest_json FROM repo_versions WHERE repo_id=? AND state='accepted'"
        " ORDER BY version DESC LIMIT 1", (repo_id,)).fetchone()
    return json.loads(row[0]) if row else None


def release_claim_for_ticket(conn, ticket_id) -> None:
    """Mark the active claim released and clear the claimant (budget-kill path).

    Unlike release_ticket() this does NOT flip the ticket state: the caller
    moves the ticket to ``blocked`` with a structured report first.
    """
    ticket = get_ticket_or_raise(conn, ticket_id)
    conn.execute("UPDATE claims SET state='released' WHERE ticket_id=? AND state='active'",
                 (ticket_id,))
    conn.execute("DELETE FROM file_claims WHERE ticket_id=?", (ticket_id,))
    conn.execute("UPDATE tickets SET claimant_key_id=NULL, updated_at=? WHERE ticket_id=?",
                 (_now(), ticket_id))
    conn.commit()
    record_event(conn, ticket["pool_id"], "claim_released_budget",
                 {"ticket_id": ticket_id})


def next_repo_version(conn, repo_id) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM repo_versions WHERE repo_id=?",
                       (repo_id,)).fetchone()
    return int(row[0]) + 1


def get_audit(conn, repo_id, version=None) -> dict | None:
    if version is None:
        row = conn.execute(
            "SELECT * FROM audits WHERE repo_id=? ORDER BY created_at DESC LIMIT 1",
            (repo_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM audits WHERE repo_id=? AND version=? ORDER BY created_at DESC LIMIT 1",
            (repo_id, version)).fetchone()
    return dict(row) if row else None


def get_pending_manifest(conn, repo_id, version=None) -> dict | None:
    query = "SELECT manifest_json FROM repo_versions WHERE repo_id=? AND state='pending'"
    params = [repo_id]
    if version is not None:
        query += " AND version=?"; params.append(version)
    query += " ORDER BY version DESC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    return json.loads(row[0]) if row else None


# --- Phase 5: member revocation ---------------------------------------------
# Revocation semantics: the member is removed from the registry and future
# manifests omit their wrapped key (new DEK per version). Already-fetched
# versions are inherently unfetchable retroactively — DEK rotation gives
# forward separation only; this is stated in every revocation notice.

REVOCATION_LIMITATION = (
    "already-fetched versions cannot be revoked retroactively "
    "(forward separation only: future manifests omit the removed member's wrapped key)"
)


def remove_member(conn, pool_id, key_id) -> dict:
    """Remove a member from the pool registry (maintainer guard, logged)."""
    pool = _pool_row(conn, pool_id)
    if pool["maintainer_key_id"] == key_id:
        raise ValueError("cannot remove the pool maintainer; transfer maintainer role first")
    row = conn.execute(
        "SELECT role FROM members WHERE pool_id=? AND key_id=?",
        (pool_id, key_id)).fetchone()
    if row is None:
        raise ValueError(f"not a pool member: {key_id!r} in pool {pool_id!r}")
    conn.execute("DELETE FROM members WHERE pool_id=? AND key_id=?",
                 (pool_id, key_id))
    conn.commit()
    record_event(conn, pool_id, "member_removed",
                 {"key_id": key_id, "role": row[0],
                  "limitation": REVOCATION_LIMITATION})
    return {"ok": True, "pool_id": pool_id, "key_id": key_id, "role": row[0],
            "limitation": REVOCATION_LIMITATION}

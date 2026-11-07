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
    owner_discord_user_id TEXT,
    relay_url TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    pool_id TEXT NOT NULL REFERENCES pools(pool_id),
    key_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    box_pubkey_hex TEXT NOT NULL DEFAULT '',
    verify_pubkey_hex TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (pool_id, key_id)
);
CREATE TABLE IF NOT EXISTS identities (
    key_id TEXT PRIMARY KEY,
    verify_key_hex TEXT NOT NULL,
    box_pubkey_hex TEXT NOT NULL,
    discord_user_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
    repo_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL REFERENCES pools(pool_id),
    name TEXT NOT NULL,
    path TEXT,
    display_name TEXT,
    submitter_key_id TEXT,
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
-- pool_chats surface removed (WO-019): the table is no longer created or
-- read. Old DBs keep it on disk unread; no DROP migration.
-- NOTE (2109 Phase 2): the peers / imported_* / trust-anchor tables are
-- GONE — no import, no peer trust, no anchors. Reads are fetch commands
-- on the pool's own gateway (/fetch repo + /fetch repo --manifest). Existing DBs keep
-- the old tables on disk and they are simply never read again (no
-- destructive migration); new DBs never create them.
--
-- remote_repos is DEAD (WO-019): /add-repo --link is rejected, so nothing
-- writes this table and nothing reads it. Kept via CREATE TABLE IF NOT
-- EXISTS for back-compat only (old DBs keep it on disk unread; no
-- destructive migration).
CREATE TABLE IF NOT EXISTS remote_repos (
    pool_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    remote_repo_id TEXT NOT NULL,
    link TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (pool_id, repo_id)
);
CREATE TABLE IF NOT EXISTS pool_authorized (
    pool_id TEXT NOT NULL REFERENCES pools(pool_id),
    discord_user_id TEXT NOT NULL,
    PRIMARY KEY (pool_id, discord_user_id)
);
CREATE TABLE IF NOT EXISTS pool_memberships (
    pool_id TEXT NOT NULL REFERENCES pools(pool_id),
    discord_user_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    PRIMARY KEY (pool_id, discord_user_id)
);
CREATE TABLE IF NOT EXISTS pool_gateways (
    pool_id TEXT PRIMARY KEY,
    gateway_endpoint TEXT,
    transport_addr TEXT,
    role TEXT,
    added_at REAL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS push_grants (
    token TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    issued_at REAL NOT NULL,
    consumed_at REAL
);
"""

TABLES = ("pools", "members", "identities", "repos", "repo_versions",
          "manifest_chain", "tickets", "claims", "file_claims", "audits",
          "events", "remote_repos", "pool_authorized", "pool_memberships", "pool_gateways",
          "push_grants")


def init_ledger(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    conn.commit()
    # Guarded additive migrations for existing DBs.
    for table, col, ddl in (
        ("identities", "discord_user_id",
         "ALTER TABLE identities ADD COLUMN discord_user_id TEXT"),
        ("repos", "path",
         "ALTER TABLE repos ADD COLUMN path TEXT"),
        ("repos", "display_name",
         "ALTER TABLE repos ADD COLUMN display_name TEXT"),
        ("repos", "submitter_key_id",
         "ALTER TABLE repos ADD COLUMN submitter_key_id TEXT"),
        ("members", "verify_pubkey_hex",
         "ALTER TABLE members ADD COLUMN verify_pubkey_hex TEXT NOT NULL DEFAULT ''"),
    ):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(ddl)
            conn.commit()
    try:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version','4')")
        conn.commit()
    except Exception:
        pass
    # Whitelist migration (2109 Phase 1): pools.owner_discord_user_id +
    # pool_authorized. Backfill pool_authorized from identities bindings.
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pools)")}
        if "owner_discord_user_id" not in cols:
            conn.execute("ALTER TABLE pools ADD COLUMN owner_discord_user_id TEXT")
            conn.commit()
        conn.execute(
            "INSERT OR IGNORE INTO pool_authorized (pool_id, discord_user_id) "
            "SELECT pool_id, owner_discord_user_id FROM pools "
            "WHERE owner_discord_user_id IS NOT NULL AND owner_discord_user_id != ''")
        conn.commit()
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version','5')")
        conn.commit()
    except Exception:
        pass
    # --- Phase 1 (2.6.0) ticket-state migration -------------------------
    # MIGRATION_2_6_0_START
    # Old ticket states map onto the three-state machine. Idempotent and
    # guarded by meta.state_migration; one record_event per migrated row.
    # No tables/rows are dropped: old tables stay on disk unread.
    try:
        _row = conn.execute("SELECT value FROM meta WHERE key='state_migration'").fetchone()
        if _row is None or _row[0] != "2.6.0":
            _map = (("pending", "open"), ("review", "claimed"),
                    ("blocked", "open"), ("merged", "completed"))
            for _old, _new in _map:
                for _tid, _pid in conn.execute(
                        "SELECT ticket_id, pool_id FROM tickets WHERE state=?",
                        (_old,)).fetchall():
                    conn.execute("UPDATE tickets SET state=?, updated_at=? WHERE ticket_id=?",
                                 (_new, _now(), _tid))
                    record_event(conn, _pid, "state_migrated_2_6_0",
                                 {"ticket_id": _tid, "from": _old, "to": _new})
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('state_migration','2.6.0')")
            conn.commit()
    except Exception:
        pass
    # MIGRATION_2_6_0_END
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


def create_pool(conn, pool_id, name, maintainer_key_id, relay_url="", settings=None, owner_discord_user_id=None) -> None:
    _existing = conn.execute("SELECT pool_id FROM pools LIMIT 1").fetchone()
    if _existing is not None:
        raise ValueError(
            f"this gateway already hosts pool {_existing[0]} — one pool per gateway")
    conn.execute(
        "INSERT INTO pools (pool_id, name, maintainer_key_id, owner_discord_user_id, relay_url, settings_json, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (pool_id, name, maintainer_key_id, str(owner_discord_user_id) if owner_discord_user_id else None, relay_url,
         json.dumps(settings or {}), _now()))
    if owner_discord_user_id:
        conn.execute("INSERT OR IGNORE INTO pool_authorized (pool_id, discord_user_id) VALUES (?,?)",
                     (pool_id, str(owner_discord_user_id)))
    conn.commit()


def add_member(conn, pool_id, key_id, role="member", box_pubkey_hex="", verify_pubkey_hex="") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO members (pool_id, key_id, role, box_pubkey_hex, verify_pubkey_hex)"
        " VALUES (?,?,?,?,?)", (pool_id, key_id, role, box_pubkey_hex, verify_pubkey_hex or ""))
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
                  budget=None, thread_ref=None, state="open") -> None:
    if state not in TICKET_STATES:
        raise ValueError(f"unknown ticket state: {state!r}")
    conn.execute(
        "INSERT INTO tickets (ticket_id, pool_id, repo_id, title, brief, state,"
        " budget_json, thread_ref, created_at, updated_at) VALUES (?,?,?,?,?,?,?, ?,?,?)",
        (ticket_id, pool_id, repo_id, title, brief, state, json.dumps(budget or {}),
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

TICKET_STATES = ("open", "claimed", "completed")
# Allowed transitions enforced by set_ticket_state().
_TRANSITIONS = {
    "open": {"claimed"},
    "claimed": {"open", "completed"},
    "completed": set(),
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

FROZEN_MESSAGE = "pool frozen — contact the maintainer"


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
        raise ClaimRefused(FROZEN_MESSAGE)
    try:
        frozen = is_frozen(conn, pool_id)
    except Exception:
        frozen = False
    if frozen:
        raise ClaimRefused(FROZEN_MESSAGE)


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


def list_tickets(conn, pool_id=None, state=None, repo_id=None) -> list:
    query, params = "SELECT * FROM tickets WHERE 1=1", []
    if pool_id:
        query += " AND pool_id=?"; params.append(pool_id)
    if repo_id:
        query += " AND repo_id=?"; params.append(repo_id)
    if state:
        if state not in TICKET_STATES:
            raise ValueError(f"unknown ticket state: {state!r}")
        query += " AND state=?"; params.append(state)
    query += " ORDER BY created_at, ticket_id"
    return [dict(r) for r in conn.execute(query, params)]


def list_members(conn, pool_id) -> list:
    _pool_row(conn, pool_id)
    rows = conn.execute(
        "SELECT key_id, role, box_pubkey_hex, verify_pubkey_hex FROM members WHERE pool_id=? ORDER BY key_id",
        (pool_id,)).fetchall()
    return [dict(r) for r in rows]


def ensure_repo(conn, repo_id, pool_id, name=None) -> None:
    _pool_row(conn, pool_id)
    conn.execute(
        "INSERT OR IGNORE INTO repos (repo_id, pool_id, name, created_at) VALUES (?,?,?,?)",
        (repo_id, pool_id, name or repo_id, _now()))
    conn.commit()


# Sentinel recorded in file_claims for a whole-ticket claim (no paths
# pre-declared). It overlaps every path on the same repo, in both
# directions: a whole-ticket claim is refused while other tickets hold
# live file claims on the repo, and a path claim is refused while a
# whole-ticket claim is live on the repo.
WHOLE_TICKET_PATH = "*"


def create_repo(conn, repo_id, pool_id, name=None, path=None,
                display_name=None, submitter_key_id=None) -> None:
    """Insert a project/registry row (fails if the repo already exists)."""
    _pool_row(conn, pool_id)
    conn.execute(
        "INSERT INTO repos (repo_id, pool_id, name, path, display_name,"
        " submitter_key_id, created_at) VALUES (?,?,?,?,?,?,?)",
        (repo_id, pool_id, name or repo_id, path,
         display_name or name or repo_id, submitter_key_id, _now()))
    conn.commit()


def upsert_repo(conn, repo_id, pool_id, name=None, path=None,
                display_name=None, submitter_key_id=None) -> None:
    """Register a project, or update its metadata if it already exists.

    Unlike ensure_repo (INSERT OR IGNORE), a re-run of /add-repo updates
    path/display_name/submitter on conflict. The pool binding never
    changes: a repo that already belongs to a different pool is refused.
    Non-None fields overwrite; the name column is only set when given.
    """
    _pool_row(conn, pool_id)
    row = conn.execute("SELECT pool_id FROM repos WHERE repo_id=?",
                       (repo_id,)).fetchone()
    if row is not None and row[0] != pool_id:
        raise ValueError(
            f"repo {repo_id!r} already belongs to pool {row[0]!r}, "
            f"not pool {pool_id!r}")
    if row is None:
        create_repo(conn, repo_id, pool_id, name=name, path=path,
                    display_name=display_name, submitter_key_id=submitter_key_id)
        return
    sets, params = [], []
    if path is not None:
        sets.append("path=?"); params.append(path)
    if display_name is not None:
        sets.append("display_name=?"); params.append(display_name)
    if name is not None:
        sets.append("name=?"); params.append(name)
    if submitter_key_id is not None:
        sets.append("submitter_key_id=?"); params.append(submitter_key_id)
    if sets:
        params.append(repo_id)
        conn.execute(f"UPDATE repos SET {', '.join(sets)} WHERE repo_id=?", params)
        conn.commit()


def get_repo(conn, repo_id) -> dict | None:
    row = conn.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
    return dict(row) if row else None


def list_repos(conn, pool_id=None) -> list:
    """Projects with accepted-version pointer and per-state ticket counts.

    ``accepted_version`` is None for repos that have no submitted/accepted
    bytes yet (e.g. registered via a bare ticket before /add-repo ever
    ran). ``display_name`` falls back to ``name`` for pre-migration rows.
    """
    query = (
        "SELECT r.repo_id, r.pool_id, r.name,"
        " COALESCE(r.display_name, r.name) AS display_name, r.path,"
        " r.submitter_key_id, r.created_at,"
        " (SELECT MAX(rv.version) FROM repo_versions rv"
        "  WHERE rv.repo_id = r.repo_id AND rv.state = 'accepted')"
        "   AS accepted_version,"
        " (SELECT COUNT(*) FROM tickets t WHERE t.repo_id = r.repo_id"
        "  AND t.state = 'open') AS open_tickets,"
        " (SELECT COUNT(*) FROM tickets t WHERE t.repo_id = r.repo_id"
        "  AND t.state = 'claimed') AS claimed_tickets,"
        " (SELECT COUNT(*) FROM tickets t WHERE t.repo_id = r.repo_id)"
        "   AS total_tickets"
        " FROM repos r")
    params: list = []
    if pool_id:
        query += " WHERE r.pool_id=?"
        params.append(pool_id)
    query += " ORDER BY r.pool_id, r.repo_id"
    return [dict(r) for r in conn.execute(query, params)]


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
    """Declared paths of OTHER tickets on the same repo that are still live.

    A live whole-ticket claim (sentinel ``WHOLE_TICKET_PATH``) overlaps
    every path, so a path claim is refused while one is active.
    """
    rows = conn.execute(
        "SELECT fc.path, fc.ticket_id FROM file_claims fc"
        " JOIN tickets t ON t.ticket_id = fc.ticket_id"
        " WHERE t.repo_id=? AND fc.ticket_id<>? AND t.state IN ('claimed')",
        (repo_id, ticket_id)).fetchall()
    claimed = {r[0] for r in rows}
    if WHOLE_TICKET_PATH in claimed:
        return sorted(set(paths) | {WHOLE_TICKET_PATH})
    return sorted(p for p in paths if p in claimed)


def _live_file_claims(conn, repo_id, ticket_id) -> list:
    """Live declared paths of OTHER tickets on the same repo (sorted)."""
    rows = conn.execute(
        "SELECT fc.path FROM file_claims fc"
        " JOIN tickets t ON t.ticket_id = fc.ticket_id"
        " WHERE t.repo_id=? AND fc.ticket_id<>? AND t.state IN ('claimed')",
        (repo_id, ticket_id)).fetchall()
    return sorted({r[0] for r in rows})


def claim_ticket_enforced(conn, claim_id, ticket_id, key_id, declared_paths=None,
                          budget=None, maintainer_override=False) -> dict:
    """Ledger-enforced claim: frozen check, overlap refused,
    one-ticket-one-worker, budget persisted. Returns the claim row.

    An empty ``declared_paths`` is a whole-ticket claim (no paths
    pre-declared): recorded with the ``WHOLE_TICKET_PATH`` sentinel,
    refused while other tickets hold live file claims on the same repo,
    and blocks later path claims on that repo until released.
    """
    paths = [str(p).strip() for p in (declared_paths or []) if str(p).strip()]
    whole_ticket = not paths
    ticket = get_ticket_or_raise(conn, ticket_id)
    if ticket["state"] != "open":
        raise ClaimRefused(
            f"claim refused: ticket {ticket_id!r} is {ticket['state']!r}, not open "
            "(one-ticket-one-worker)")
    if is_global_frozen(conn):
        raise ClaimRefused(FROZEN_MESSAGE)
    if is_frozen(conn, ticket["pool_id"]):
        raise ClaimRefused(FROZEN_MESSAGE)
    if whole_ticket:
        live = _live_file_claims(conn, ticket["repo_id"], ticket_id)
        if live and not maintainer_override:
            raise ClaimRefused(
                "claim refused: whole-ticket claim overlaps live file claims "
                f"on repo {ticket['repo_id']!r}: {', '.join(live)}")
        claim_paths = [WHOLE_TICKET_PATH]
        overlaps = [WHOLE_TICKET_PATH] if live else []
    else:
        claim_paths = paths
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
                     declared_paths=claim_paths, budget=full_budget)
    except sqlite3.IntegrityError as exc:  # double-claim race / dup paths
        raise ClaimRefused(f"claim refused: {exc}") from exc
    if overlaps:
        record_event(conn, ticket["pool_id"], "claim_override",
                     {"ticket_id": ticket_id, "key_id": key_id, "paths": overlaps})
    else:
        record_event(conn, ticket["pool_id"], "claimed",
                     {"ticket_id": ticket_id, "key_id": key_id, "paths": claim_paths})
    row = conn.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
    claim = dict(row)
    claim["whole_ticket"] = whole_ticket
    return claim


def release_ticket(conn, ticket_id, key_id) -> dict:
    """Return a claimed ticket to open; frees the claim and file claims."""
    ticket = get_ticket_or_raise(conn, ticket_id)
    if ticket["state"] != "claimed":
        raise ClaimRefused(f"release refused: ticket {ticket_id!r} is {ticket['state']!r}, not claimed")
    if ticket["claimant_key_id"] != key_id:
        raise ClaimRefused("release refused: only the claimant may release the ticket")
    if is_global_frozen(conn):
        raise ClaimRefused(FROZEN_MESSAGE)
    if is_frozen(conn, ticket["pool_id"]):
        raise ClaimRefused(FROZEN_MESSAGE)
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


def complete_ticket(conn, ticket_id, key_id=None) -> dict:
    """End a claimed ticket as ``completed`` (terminal).

    The claim row ends with state ``completed``; file claims are freed.
    Only the claimant may complete (when a key_id is given).
    """
    ticket = get_ticket_or_raise(conn, ticket_id)
    if ticket["state"] != "claimed":
        raise ClaimRefused(f"complete refused: ticket {ticket_id!r} is {ticket['state']!r}, not claimed")
    if key_id is not None and ticket["claimant_key_id"] != key_id:
        raise ClaimRefused("complete refused: only the claimant may complete the ticket")
    if is_global_frozen(conn):
        raise ClaimRefused(FROZEN_MESSAGE)
    if is_frozen(conn, ticket["pool_id"]):
        raise ClaimRefused(FROZEN_MESSAGE)
    conn.execute("UPDATE claims SET state='completed' WHERE ticket_id=? AND state='active'", (ticket_id,))
    conn.execute("DELETE FROM file_claims WHERE ticket_id=?", (ticket_id,))
    conn.execute("UPDATE tickets SET state='completed', updated_at=? WHERE ticket_id=?", (_now(), ticket_id))
    conn.commit()
    record_event(conn, ticket["pool_id"], "completed", {"ticket_id": ticket_id, "key_id": ticket["claimant_key_id"]})
    return get_ticket_or_raise(conn, ticket_id)


def record_gateway(conn, pool_id, gateway_endpoint="", transport_addr="", role="") -> dict:
    """Record a pool/gateway this agent interacts with (differentiation, not management)."""
    now = _now()
    conn.execute(
        "INSERT INTO pool_gateways (pool_id, gateway_endpoint, transport_addr, role, added_at, last_seen)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(pool_id) DO UPDATE SET gateway_endpoint=excluded.gateway_endpoint,"
        " transport_addr=excluded.transport_addr, role=excluded.role, last_seen=excluded.last_seen",
        (str(pool_id), gateway_endpoint or "", transport_addr or "", role or "", now, now))
    conn.commit()
    return {"pool_id": str(pool_id), "gateway_endpoint": gateway_endpoint or "",
            "transport_addr": transport_addr or "", "role": role or ""}


def list_gateways(conn) -> list:
    cur = conn.execute(
        "SELECT pool_id, gateway_endpoint, transport_addr, role, added_at, last_seen"
        " FROM pool_gateways ORDER BY pool_id")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_active_claim(conn, ticket_id) -> dict | None:
    row = conn.execute(
        "SELECT * FROM claims WHERE ticket_id=? AND state='active'", (ticket_id,)).fetchone()
    return dict(row) if row else None


# --- Phase 4: push grants (one-time, claimant-only capabilities) ------------
# A grant is issued by /push repo on the pool gateway to the claimant of a
# claimed ticket. It is consumed exactly once, on a successful deliver;
# validity is computed from the ticket state (valid while the ticket is
# still claimed by the grant's key), so a released/completed ticket makes
# every outstanding grant for it refuse at deliver time.

def create_push_grant(conn, token, pool_id, ticket_id, key_id) -> dict:
    conn.execute(
        "INSERT INTO push_grants (token, pool_id, ticket_id, key_id, issued_at, consumed_at)"
        " VALUES (?,?,?,?,?,NULL)",
        (str(token), pool_id, ticket_id, key_id, _now()))
    conn.commit()
    return get_push_grant(conn, token)


def get_push_grant(conn, token) -> dict | None:
    if not token:
        return None
    row = conn.execute("SELECT * FROM push_grants WHERE token=?", (str(token),)).fetchone()
    return dict(row) if row else None


def consume_push_grant(conn, token) -> dict | None:
    """Consume the grant exactly once. Returns the grant row, or None when
    the token is unknown or already consumed (replay)."""
    cur = conn.execute(
        "UPDATE push_grants SET consumed_at=? WHERE token=? AND consumed_at IS NULL",
        (_now(), str(token)))
    conn.commit()
    if cur.rowcount != 1:
        return None
    return get_push_grant(conn, token)


def get_latest_accepted_manifest(conn, repo_id) -> dict | None:
    """Latest maintainer-accepted manifest for *repo_id* (chain tip), or None."""
    row = conn.execute(
        "SELECT manifest_json FROM repo_versions WHERE repo_id=? AND state='accepted'"
        " ORDER BY version DESC LIMIT 1", (repo_id,)).fetchone()
    return json.loads(row[0]) if row else None


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


# --- Gateway-native: discord identity, queued notices ------------------------

def authorize_user(conn, pool_id, discord_user_id) -> None:
    if discord_user_id is None:
        raise ValueError("authorize needs a discord_user_id")
    conn.execute("INSERT OR IGNORE INTO pool_authorized (pool_id, discord_user_id) VALUES (?,?)",
                 (pool_id, str(discord_user_id)))
    conn.commit()


def sync_whitelist(conn, pool_id, user_ids) -> None:
    """Sync the env/config whitelist into ``pool_authorized`` (Phase 1).

    The pool owner (``pools.owner_discord_user_id``) is always ensured.
    Missing whitelist users are inserted; existing authorized rows are
    NEVER deleted (removal is ``deauthorize_user``, a separate act).
    Empty ``user_ids`` therefore authorizes nobody beyond the owner.
    """
    ids = [str(u).strip() for u in (user_ids or []) if str(u).strip()]
    try:
        pool = conn.execute(
            "SELECT owner_discord_user_id FROM pools WHERE pool_id=?", (pool_id,)).fetchone()
    except Exception:
        pool = None
    if pool is not None and pool[0]:
        ids.append(str(pool[0]))
    for uid in dict.fromkeys(ids):  # dedupe, keep order
        conn.execute("INSERT OR IGNORE INTO pool_authorized (pool_id, discord_user_id) VALUES (?,?)",
                     (pool_id, uid))
    conn.commit()


def bind_membership(conn, pool_id, discord_user_id, key_id) -> None:
    """First-claim key binding: record the key a whitelisted user presented.

    ``INSERT OR IGNORE`` — the first binding wins; later binds are no-ops
    (a different presented key is refused in ``PoolService.claim``).
    """
    conn.execute(
        "INSERT OR IGNORE INTO pool_memberships (pool_id, discord_user_id, key_id)"
        " VALUES (?,?,?)", (pool_id, str(discord_user_id), str(key_id)))
    conn.commit()


def lookup_membership_key(conn, discord_user_id, pool_id=None):
    """Bound key_id for a Discord user from ``pool_memberships``, or None.

    One pool per gateway, so ``pool_id`` is optional; when given it narrows
    the lookup to that pool.
    """
    if discord_user_id is None:
        return None
    try:
        if pool_id is not None:
            row = conn.execute(
                "SELECT key_id FROM pool_memberships WHERE pool_id=? AND discord_user_id=?",
                (pool_id, str(discord_user_id))).fetchone()
        else:
            row = conn.execute(
                "SELECT key_id FROM pool_memberships WHERE discord_user_id=?",
                (str(discord_user_id),)).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def deauthorize_user(conn, pool_id, discord_user_id) -> None:
    conn.execute("DELETE FROM pool_authorized WHERE pool_id=? AND discord_user_id=?",
                 (pool_id, str(discord_user_id)))
    conn.commit()


def is_authorized(conn, pool_id, discord_user_id) -> bool:
    if discord_user_id is None:
        return False
    try:
        pool = conn.execute("SELECT owner_discord_user_id FROM pools WHERE pool_id=?", (pool_id,)).fetchone()
        if pool is not None and pool[0] is not None and str(pool[0]) == str(discord_user_id):
            return True
        row = conn.execute("SELECT 1 FROM pool_authorized WHERE pool_id=? AND discord_user_id=?",
                           (pool_id, str(discord_user_id))).fetchone()
        return row is not None
    except Exception:
        return False


def is_owner(conn, pool_id, discord_user_id) -> bool:
    if discord_user_id is None:
        return False
    try:
        pool = conn.execute("SELECT owner_discord_user_id FROM pools WHERE pool_id=?", (pool_id,)).fetchone()
        if pool is None or pool[0] is None:
            return False
        return str(pool[0]) == str(discord_user_id)
    except Exception:
        return False


def list_local_pools(conn):
    rows = conn.execute("SELECT pool_id FROM pools ORDER BY pool_id").fetchall()
    return [r[0] for r in rows]


def queue_notice(conn, pool_id, text, kind="notice") -> None:
    record_event(conn, pool_id, "queued_notice",
                 {"text": text[:3500], "kind": kind})


def drain_notices(conn, pool_id=None):
    """Unread notices since last drain (meta cursor). Advances cursor."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           ("notice_cursor",)).fetchone()
        cursor = int(row[0]) if row else 0
    except Exception:
        cursor = 0
    q = ("SELECT id, pool_id, payload_json FROM events"
         " WHERE kind='queued_notice' AND id>?")
    p: list = [cursor]
    if pool_id is not None:
        q += " AND (pool_id=? OR pool_id IS NULL)"
        p.append(pool_id)
    q += " ORDER BY id"
    rows = conn.execute(q, p).fetchall()
    texts = []
    last = cursor
    for rid, _pid, pj in rows:
        try:
            d = json.loads(pj or "{}")
        except Exception:
            d = {}
        t = d.get("text") or ""
        if t:
            texts.append(t)
        last = max(last, int(rid))
    if last != cursor or rows:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                     ("notice_cursor", str(last)))
        conn.commit()
    return texts


def peek_notices(conn, pool_id=None):
    """Peek unread notices without advancing the cursor (tests/debug)."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           ("notice_cursor",)).fetchone()
        cursor = int(row[0]) if row else 0
    except Exception:
        cursor = 0
    q = ("SELECT payload_json FROM events"
         " WHERE kind='queued_notice' AND id>? ORDER BY id")
    rows = conn.execute(q, (cursor,)).fetchall()
    out = []
    for (pj,) in rows:
        try:
            d = json.loads(pj or "{}")
        except Exception:
            d = {}
        if d.get("text"):
            out.append(d["text"])
    return out


# --- Operator link note (OD5): DEAD (WO-019) --------------------------------
# remote_repos is unread: /add-repo --link is rejected, so nothing writes
# or reads this table. It stays on disk unread; no ledger API for it remains.



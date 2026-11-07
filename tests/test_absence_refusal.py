"""Absence/empty-whitelist refusal (Phase 1 slice).

Phase 0's legacy-skip removal (``_require_owner``/``_require_authorized``
skipping when no owner/whitelist rows exist) is NOT in this dispatch, so
the legacy-skip tests (``test_require_owner_refuses_without_rows`` /
``test_require_authorized_refuses_without_rows``) are intentionally NOT
written here — they belong to Phase 0 and would fail while the skip is
still present. What Phase 1 can and does guarantee: with a registered
owner and an EMPTY whitelist, nobody beyond the owner is authorized.
"""
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent.parent))

from ledger import init_ledger
from tickets import PoolService
from transport import FakeTransport
from identity import ensure_identity

OWNER = "owner-1"
STRANGER = "stranger-1"


def _owner_world(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_ledger(conn)
    maint = ensure_identity(tmp_path / "m")
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
        " VALUES (?,?,?,0)",
        (maint.key_id, maint.verify_key.encode(RawEncoder).hex(),
         maint.box_pk.encode(RawEncoder).hex()))
    svc = PoolService(conn, FakeTransport(), caller_key_id=maint.key_id,
                      caller_signing_key=maint.signing_key, caller_box_sk=maint.box_sk)
    svc.register_pool("pool-1", "Team", discord_user_id=OWNER)
    return conn, svc


def test_empty_whitelist_refuses_non_owner(tmp_path):
    conn, svc = _owner_world(tmp_path)
    ticket = svc.add_ticket("pool-1", "demo", "Work", source={"user_id": OWNER})
    stranger = PoolService(conn, FakeTransport(), caller_key_id="agent:stranger",
                           caller_signing_key=SigningKey.generate())
    with pytest.raises(PermissionError):
        stranger.claim(ticket["ticket_id"], source={"user_id": STRANGER})
    with pytest.raises(PermissionError):
        stranger.list_tickets(pool_id="pool-1", source={"user_id": STRANGER})
    with pytest.raises(PermissionError):
        stranger.members("pool-1", source={"user_id": STRANGER})
    with pytest.raises(PermissionError):
        stranger.list_projects("pool-1", source={"user_id": STRANGER})


def test_empty_whitelist_owner_still_passes(tmp_path):
    _conn, svc = _owner_world(tmp_path)
    ticket = svc.add_ticket("pool-1", "demo", "Work", source={"user_id": OWNER})
    assert ticket["state"] == "open"
    assert svc.list_tickets(pool_id="pool-1", source={"user_id": OWNER})
    assert svc.members("pool-1", source={"user_id": OWNER})

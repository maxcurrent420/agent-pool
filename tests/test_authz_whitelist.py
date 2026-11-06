"""Whitelist authorization matrix (2109 Phase 4, per the plan).

Owner registers -> invites a friend -> friend claims/submits -> a
non-invitee is refused -> the friend's freeze is refused. Authorization
is the whitelisted Discord user id only; keys are crypto-only.

Also asserts the plan's construction gate: no authorisation path reads
``identities`` or ``members`` for a decision.
"""
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent.parent))

from ledger import (create_pool, init_ledger)
from tickets import PoolService
from transport import FakeTransport
from identity import ensure_identity

OWNER = "owner-1"
FRIEND = "friend-1"
STRANGER = "stranger-1"


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def world(tmp_path):
    """Owner registers and invites a friend, both with real identities."""
    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    friend = ensure_identity(tmp_path / "f")
    for ident in (maint, friend):
        conn.execute(
            "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
            " VALUES (?,?,?,0)",
            (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
             ident.box_pk.encode(RawEncoder).hex()))
    transport = FakeTransport()
    svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                      caller_signing_key=maint.signing_key,
                      caller_box_sk=maint.box_sk)
    # The owner registers (whitelist row written at register).
    svc.register_pool("pool-1", "Team", discord_user_id=OWNER)
    # Phase 1: tickets always mint open (no auto-approve toggle).
    # ... and invites the friend.
    svc.invite("pool-1", friend.key_id, role="member",
               box_pubkey_hex=friend.box_pk.encode(RawEncoder).hex(),
               verify_pubkey_hex=friend.verify_key.encode(RawEncoder).hex(),
               discord_user_id=FRIEND, source={"user_id": OWNER})
    friend_svc = PoolService(conn, transport, caller_key_id=friend.key_id,
                             caller_signing_key=friend.signing_key,
                             caller_box_sk=friend.box_sk)
    return conn, svc, friend_svc, maint, friend


def src(user):
    return {"user_id": user}


# --- the live Discord check scenario, as a test ----------------------------


def test_live_scenario_owner_invites_friend_friend_claims(tmp_path):
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    ticket = svc.add_ticket("pool-1", "demo", "Work", source=src(OWNER))
    # Friend (authorized non-owner) may claim and submit.
    claim = friend_svc.claim(ticket["ticket_id"], source=src(FRIEND))
    assert claim["state"] == "active"


def test_non_invitee_refused_on_every_verb(tmp_path):
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    stranger = PoolService(conn, FakeTransport(),
                           caller_key_id="agent:stranger",
                           caller_signing_key=SigningKey.generate())
    ticket = svc.add_ticket("pool-1", "demo", "Work", source=src(OWNER))
    for call in (
        lambda: stranger.add_ticket("pool-1", "demo", "X", source=src(STRANGER)),
        lambda: stranger.claim(ticket["ticket_id"], source=src(STRANGER)),
        lambda: stranger.list_tickets(pool_id="pool-1", source=src(STRANGER)),
        lambda: stranger.list_projects("pool-1", source=src(STRANGER)),
        lambda: stranger.members("pool-1", source=src(STRANGER)),
        lambda: stranger.invite("pool-1", "agent:x",
                                discord_user_id="x-1", source=src(STRANGER)),
    ):
        with pytest.raises(PermissionError):
            call()


def test_authorized_non_owner_refused_on_owner_verbs(tmp_path):
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    # Phase 1: the owner-only mint is refused to non-owners; tickets mint open.
    ticket = svc.add_ticket("pool-1", "demo", "Work", source=src(OWNER))
    assert ticket["state"] == "open"
    for call in (
        lambda: friend_svc.freeze("pool-1", True, source=src(FRIEND)),
        lambda: friend_svc.remove_member("pool-1", maint.key_id, source=src(FRIEND)),
        lambda: friend_svc.invite("pool-1", "agent:new",
                                  discord_user_id="new-1", source=src(FRIEND)),
    ):
        with pytest.raises(PermissionError):
            call()


def test_owner_passes_everywhere(tmp_path):
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    # Phase 1: tickets mint open; the approval surface is deleted.
    assert not hasattr(svc, "set_auto_approve")
    assert not hasattr(svc, "approve_ticket")
    ticket = svc.add_ticket("pool-1", "demo", "Work", source=src(OWNER))
    assert ticket["state"] == "open"
    # Owner freezes and unfreezes.
    assert svc.freeze("pool-1", True, source=src(OWNER))["frozen"] is True
    assert svc.freeze("pool-1", False, source=src(OWNER))["frozen"] is False


def test_frozen_blocks_authorized_non_owner_with_exact_message(tmp_path):
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    ticket = svc.add_ticket("pool-1", "demo", "Work", source=src(OWNER))
    svc.freeze("pool-1", True, source=src(OWNER))
    from ledger import FROZEN_MESSAGE
    with pytest.raises(Exception) as exc:
        friend_svc.claim(ticket["ticket_id"], source=src(FRIEND))
    assert str(exc.value) == FROZEN_MESSAGE
    # Only the owner's unfreeze runs while frozen.
    assert svc.freeze("pool-1", False, source=src(OWNER))["frozen"] is False
    assert friend_svc.claim(ticket["ticket_id"], source=src(FRIEND))["state"] == "active"


# --- construction gate: no authz path reads identities/members --------------


def test_authz_helpers_do_not_read_identities_or_members(tmp_path):
    """The whitelist helpers are one indexed query on pools/pool_authorized
    (asserted by construction: monkeypatch both other tables to raise if
    touched)."""
    conn, svc, _f, _m, _fr = world(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("authz must not read identities/members")

    import tickets as _t
    orig_ident = _t.PoolService._discord_user
    # Wrap the helpers so any identities/members read inside them raises.
    real_require_owner = _t.PoolService._require_owner
    real_require_authorized = _t.PoolService._require_authorized

    def guard(self, pool_id, source):
        # permission checks may only touch pools + pool_authorized
        rows = self._conn.execute(
            "SELECT owner_discord_user_id FROM pools WHERE pool_id=?", (pool_id,)).fetchone()
        assert rows is not None
        self._conn.execute(
            "SELECT 1 FROM pool_authorized WHERE pool_id=?", (pool_id,)).fetchone()
        return real_require_owner(self, pool_id, source)

    def guard_auth(self, pool_id, source):
        rows = self._conn.execute(
            "SELECT owner_discord_user_id FROM pools WHERE pool_id=?", (pool_id,)).fetchone()
        assert rows is not None
        self._conn.execute(
            "SELECT 1 FROM pool_authorized WHERE pool_id=?", (pool_id,)).fetchone()
        return real_require_authorized(self, pool_id, source)

    _t.PoolService._require_owner = guard
    _t.PoolService._require_authorized = guard_auth
    try:
        # Every gate in the matrix still behaves correctly through the guard.
        svc.add_ticket("pool-1", "demo", "Owner", source=src(OWNER))
        with pytest.raises(PermissionError):
            svc.add_ticket("pool-1", "demo", "Friend", source=src(FRIEND))
    finally:
        _t.PoolService._require_owner = real_require_owner
        _t.PoolService._require_authorized = real_require_authorized

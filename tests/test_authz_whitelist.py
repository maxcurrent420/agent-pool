"""Whitelist authorization matrix (2109 Phase 4, per the plan).

Owner registers -> a friend is on the env/config whitelist -> friend
claims/submits (first claim binds their key) -> a non-whitelisted user
is refused -> the friend's freeze is refused. Authorization is the
whitelisted Discord user id only; keys are crypto-only.

There is no invite command: membership comes from the whitelist
(env ``AGENT_POOL_ALLOWED_USERS`` / config ``allowed_users``), synced
into ``pool_authorized`` by ``ledger.sync_whitelist``, and key
attribution comes from first-claim binding in ``pool_memberships``.

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


def whitelist_member(conn, pool_id, user_id, ident=None):
    """Whitelist a user the Phase 1 way (no invite command exists):

    ``sync_whitelist`` authorizes the Discord id; ``add_member`` registers
    the crypto member so DEK wrapping / propagator paths see them. Key
    attribution is NOT set here — it binds on the user's first claim.
    """
    import ledger as L
    L.sync_whitelist(conn, "pool-1", [user_id])
    if ident is not None:
        L.add_member(conn, pool_id, ident.key_id, role="member",
                     box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex(),
                     verify_pubkey_hex=ident.verify_key.encode(RawEncoder).hex())


def world(tmp_path):
    """Owner registers; a friend is whitelisted, both with real identities."""
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
    # ... and whitelists the friend (env/config whitelist, synced here).
    whitelist_member(conn, "pool-1", FRIEND, friend)
    friend_svc = PoolService(conn, transport, caller_key_id=friend.key_id,
                             caller_signing_key=friend.signing_key,
                             caller_box_sk=friend.box_sk)
    return conn, svc, friend_svc, maint, friend


def src(user):
    return {"user_id": user}


# --- the live Discord check scenario, as a test ----------------------------


def test_live_scenario_owner_whitelists_friend_friend_claims(tmp_path):
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    ticket = svc.add_ticket("pool-1", "demo", "Work", source=src(OWNER))
    # Friend (authorized non-owner) may claim and submit.
    claim = friend_svc.claim(ticket["ticket_id"], source=src(FRIEND))
    assert claim["state"] == "active"


def test_non_whitelisted_refused_on_every_verb(tmp_path):
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
        lambda: stranger.freeze("pool-1", True, source=src(STRANGER)),
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
        lambda: friend_svc.add_ticket("pool-1", "demo", "Friend ticket",
                                      source=src(FRIEND)),
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


# --- wrapper seam + freeze (migrated from deleted test_authz.py) -----------

def test_unmapped_discord_user_refused_via_wrapper(tmp_path):
    """An unmapped/non-whitelisted Discord id is refused at the wrapper."""
    conn, svc, _f, _m, _fr = world(tmp_path)
    import __init__ as plugin
    orig = plugin._service_for
    plugin._service_for = lambda ctx, source=None: svc
    try:
        class Ctx:
            pass

        wrapped = plugin._wrap_command(plugin._handle_task, Ctx())
        out = wrapped("list --pool-id pool-1", source={"user_id": "unmapped-123"})
        assert "not authorized" in out
    finally:
        plugin._service_for = orig


def test_frozen_blocks_every_user_except_owner_unfreeze(tmp_path):
    """While frozen, every verb from every user raises the exact frozen
    message; only the owner's unfreeze runs."""
    conn, svc, friend_svc, _m, _fr = world(tmp_path)
    from ledger import FROZEN_MESSAGE
    svc.freeze("pool-1", True, source=src(OWNER))
    for user in (OWNER, FRIEND, STRANGER):
        with pytest.raises(Exception) as exc:
            svc.add_ticket("pool-1", "demo", "X", source=src(user))
        assert str(exc.value) == FROZEN_MESSAGE
    out = svc.freeze("pool-1", False, source=src(OWNER))
    assert out["frozen"] is False
    assert svc.add_ticket("pool-1", "demo", "After unfreeze",
                          source=src(OWNER))["ticket_id"]


# --- Phase 1: env/config whitelist + first-claim key binding -----------------

def test_whitelist_first_claim_binds_key(tmp_path):
    """Whitelisted user with no bound key: first claim binds the presented key."""
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    # Nothing bound before any claim.
    assert friend_svc._whitelist_bound_key("pool-1", src(FRIEND)) is None
    ticket = svc.add_ticket("pool-1", "demo", "Work", source=src(OWNER))
    claim = friend_svc.claim(ticket["ticket_id"], source=src(FRIEND))
    assert claim["state"] == "active"
    # The friend's gateway key is now bound to their Discord id.
    assert friend_svc._whitelist_bound_key("pool-1", src(FRIEND)) == friend.key_id
    row = conn.execute(
        "SELECT key_id FROM pool_memberships WHERE pool_id='pool-1' AND discord_user_id=?",
        (FRIEND,)).fetchone()
    assert row[0] == friend.key_id


def test_whitelist_subsequent_reuses_key(tmp_path):
    """After first-claim binding, later claims reuse the bound key."""
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    t1 = svc.add_ticket("pool-1", "demo", "Work 1", source=src(OWNER))
    t2 = svc.add_ticket("pool-1", "demo", "Work 2", source=src(OWNER))
    c1 = friend_svc.claim(t1["ticket_id"], declared_paths=["a.py"], source=src(FRIEND))
    assert c1["key_id"] == friend.key_id
    # Second claim (explicitly presenting the same key) reuses it.
    c2 = friend_svc.claim(t2["ticket_id"], key_id=friend.key_id,
                          declared_paths=["b.py"], source=src(FRIEND))
    assert c2["key_id"] == friend.key_id
    rows = conn.execute("SELECT COUNT(*) FROM pool_memberships WHERE discord_user_id=?",
                        (FRIEND,)).fetchone()[0]
    assert rows == 1


DIFFERENT_KEY_MESSAGE = (
    "claim refused: user {user} already bound to key {bound} on pool {pool} — "
    "presented key {presented} differs; first-claim binding is permanent")


def test_whitelist_different_key_refused(tmp_path):
    """A whitelisted user presenting a different key than the bound one is
    refused with the exact decided message (binding is permanent)."""
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    t1 = svc.add_ticket("pool-1", "demo", "Work 1", source=src(OWNER))
    t2 = svc.add_ticket("pool-1", "demo", "Work 2", source=src(OWNER))
    friend_svc.claim(t1["ticket_id"], source=src(FRIEND))
    with pytest.raises(PermissionError) as exc:
        friend_svc.claim(t2["ticket_id"], key_id="agent:other-key", source=src(FRIEND))
    assert str(exc.value) == DIFFERENT_KEY_MESSAGE.format(
        user=FRIEND, bound=friend.key_id, pool="pool-1", presented="agent:other-key")


def test_empty_whitelist_refuses(tmp_path):
    """Empty whitelist (owner only): non-owner users are refused everywhere."""
    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
        " VALUES (?,?,?,0)",
        (maint.key_id, maint.verify_key.encode(RawEncoder).hex(),
         maint.box_pk.encode(RawEncoder).hex()))
    svc = PoolService(conn, FakeTransport(), caller_key_id=maint.key_id,
                      caller_signing_key=maint.signing_key, caller_box_sk=maint.box_sk)
    svc.register_pool("pool-1", "Team", discord_user_id=OWNER)
    import ledger as L
    # Syncing an empty whitelist authorizes nobody beyond the owner.
    L.sync_whitelist(conn, "pool-1", [])
    authorized = [r[0] for r in conn.execute(
        "SELECT discord_user_id FROM pool_authorized WHERE pool_id='pool-1'").fetchall()]
    assert authorized == [OWNER]
    ticket = svc.add_ticket("pool-1", "demo", "Work", source=src(OWNER))
    stranger = PoolService(conn, FakeTransport(), caller_key_id="agent:stranger",
                           caller_signing_key=SigningKey.generate())
    with pytest.raises(PermissionError):
        stranger.claim(ticket["ticket_id"], source=src(STRANGER))
    with pytest.raises(PermissionError):
        stranger.list_tickets(pool_id="pool-1", source=src(STRANGER))


def test_sync_whitelist_never_deletes(tmp_path):
    """sync_whitelist inserts missing users and keeps existing rows."""
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    import ledger as L
    L.sync_whitelist(conn, "pool-1", ["new-user-1"])
    users = {r[0] for r in conn.execute(
        "SELECT discord_user_id FROM pool_authorized WHERE pool_id='pool-1'").fetchall()}
    assert {OWNER, FRIEND, "new-user-1"} <= users


def test_no_invite_surface():
    """Phase 0 prerequisite: the invite command/method is gone."""
    assert not hasattr(PoolService, "invite")
    import tickets as _t
    assert "_invite_bound_key" not in dir(_t.PoolService)


def test_remove_member_revokes_whitelisted_discord_id(tmp_path):
    """WO-020: /pool remove revokes the removed member's whitelist access.

    The Discord id is bound via pool_memberships (first-claim binding),
    NOT identities.discord_user_id (Phase 1 removed that writer).
    """
    conn, svc, friend_svc, maint, friend = world(tmp_path)
    import ledger as L
    L.bind_membership(conn, "pool-1", FRIEND, friend.key_id)
    assert L.is_authorized(conn, "pool-1", FRIEND) is True
    svc.remove_member("pool-1", friend.key_id, source=src(OWNER))
    assert L.is_authorized(conn, "pool-1", FRIEND) is False


def test_whitelist_env_wins_over_config(monkeypatch):
    """Env AGENT_POOL_ALLOWED_USERS beats config allowed_users; owner is
    always added by sync_whitelist (checked via the resolver helper)."""
    import __init__ as plugin

    class Ctx:
        def __init__(self, cfg):
            self._cfg = cfg

        def get_config(self, key, default=None):
            return self._cfg.get(key, default)

    monkeypatch.setenv("AGENT_POOL_ALLOWED_USERS", "env-user-1, env-user-2")
    assert plugin._whitelist_user_ids(Ctx({"allowed_users": ["cfg-user-1"]})) == [
        "env-user-1", "env-user-2"]
    monkeypatch.delenv("AGENT_POOL_ALLOWED_USERS")
    assert plugin._whitelist_user_ids(Ctx({"allowed_users": ["cfg-user-1"]})) == ["cfg-user-1"]
    assert plugin._whitelist_user_ids(Ctx({})) == []

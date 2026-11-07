import sqlite3, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import (init_ledger, create_pool, add_member, bind_membership,
                    list_members, authorize_user, is_owner,
                    is_authorized)
from tickets import PoolService
from nacl.signing import SigningKey, VerifyKey
from nacl.encoding import RawEncoder
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_F1_non_maintainer_mint_refused_through_wrapper():
    """F1 (re-gated, 2109 whitelist model): the gateway's own agent key
    must never satisfy a maintainer check. Authorization is the
    whitelisted Discord user id only — an authorized non-owner is refused
    the owner-only mint even though the gateway identity IS the pool
    maintainer. Keys never decide access."""
    c=memdb()
    create_pool(c,"pool-1","Team","agent:maint", owner_discord_user_id="maint-123")
    add_member(c,"pool-1","agent:maint",role="maintainer")
    add_member(c,"pool-1","agent:friend",role="member")
    bind_membership(c,"pool-1","friend-123","agent:friend")
    authorize_user(c,"pool-1","friend-123")
    from __init__ import _wrap_command
    import __init__ as plugin
    # The service's caller IS the gateway identity (the maintainer key) —
    # under the old model that passed every service-level check.
    svc_real = PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    orig = plugin._service_for
    plugin._service_for = lambda ctx, source=None: svc_real
    try:
        from types import SimpleNamespace
        ctx = SimpleNamespace()
        wrapped = plugin._wrap_command(plugin._handle_task, ctx)
        # Authorized non-owner: refused on the owner-only mint.
        out = wrapped('add --pool-id pool-1 --repo demo --title "X" --brief "b"', source={"user_id": "friend-123"})
        assert "not authorized" in out
        # Owner passes everywhere.
        out2 = wrapped('add --pool-id pool-1 --repo demo --title "X" --brief "b"', source={"user_id": "maint-123"})
        assert "not authorized" not in out2
    finally:
        plugin._service_for = orig

def test_F2_maintainer_self_wrap_at_register(tmp_path):
    c=memdb()
    maint_sk=SigningKey.generate()
    # identities row for maintainer with box pubkey
    c.execute("INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at) VALUES (?,?,?,?)",
              ("agent:maint", maint_sk.verify_key.encode().hex(), "abcd"*8, 0))
    c.commit()
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=maint_sk)
    # Register requires --discord-user: the whitelist IS the auth (2109).
    svc.register_pool("pool-1","Team", discord_user_id="maint-123")
    rows=list_members(c,"pool-1")
    m=[r for r in rows if r["key_id"]=="agent:maint"][0]
    assert m["box_pubkey_hex"]=="abcd"*8
    # The owner is whitelisted from the register call.
    assert is_owner(c,"pool-1","maint-123") is True
    assert is_authorized(c,"pool-1","maint-123") is True
    # propagator carries maintainer box pubkey
    from transport import FakeTransport
    svc2=PoolService(c, FakeTransport(), caller_key_id="agent:maint", caller_signing_key=maint_sk)
    prop=svc2._propagator("pool-1")
    assert prop._members["agent:maint"].box_pubkey_hex=="abcd"*8

def test_F3_remote_write_refused_and_fetch_is_the_read_path():
    """F3 (re-gated, 2109): there is no import. A write aimed at a pool
    this gateway does not host is refused naming the read path — reads
    are /pool fetch on the pool's own gateway."""
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    from tickets import PoolNotLocalError
    svc=PoolService(c, caller_key_id="agent:alice", caller_signing_key=SigningKey.generate())
    with pytest.raises(PoolNotLocalError):
        svc._resolve_pool("pool-remote")
    # /fetch repo answers only for hosted pools, naming its hosts.
    with pytest.raises(PoolNotLocalError) as exc:
        svc.fetch_repo("r1", pool_id="pool-remote")
    assert "not hosted by this gateway" in str(exc.value)
    assert "pool-1" in str(exc.value)

def test_F4_dead_peer_code_removed():
    assert not hasattr(PoolService, "register_remote_peer")
    assert not hasattr(PoolService, "confirm_peer")
    import ledger as _L
    # Phase 1: the proposal/approval surface is deleted.
    for gone in ("propose", "accept_proposal", "approve_ticket", "resolve_pending",
                 "list_pending", "set_auto_approve", "_packet_hash"):
        assert not hasattr(PoolService, gone), gone
    for gone in ("get_pending_manifest", "get_auto_approve", "set_auto_approve",
                 "get_remote_repo", "upsert_remote_repo"):
        assert not hasattr(_L, gone), gone
    # 2109 Phase 2: the whole remote-view surface is gone from the ledger.
    for gone in ("upsert_peer", "get_peer", "list_peers", "set_peer_trust",
                 "get_trust_anchor", "list_trust_anchors", "pin_trust_anchor",
                 "revoke_trust_anchor", "import_version", "import_ticket",
                 "list_imported", "list_imported_tickets",
                 "get_latest_imported_manifest", "list_imported_chain"):
        assert not hasattr(_L, gone), f"{gone} must be deleted"

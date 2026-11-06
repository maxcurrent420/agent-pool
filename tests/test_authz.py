"""Whitelist authorization + absolute freeze (2109 Phase 1).

Authorization is the whitelisted Discord user id ONLY (owner or
pool_authorized). Keys never decide access — the gateway's own agent key
must never satisfy a maintainer check. Full verb-by-verb matrix lives in
test_authz_whitelist.py; this file pins the wrapper seam, the fail-closed
semantics, and the exact frozen message.
"""
import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, set_frozen, set_global_frozen, FROZEN_MESSAGE, record_event
from tickets import PoolService
from nacl.signing import SigningKey
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def whitelisted(conn, pool_id="pool-1", owner="owner-1", extra=()):
    """A whitelisted pool: owner + (optional) authorized non-owners."""
    create_pool(conn, pool_id, "Team", "agent:maint",
                owner_discord_user_id=owner)
    for user in extra:
        conn.execute("INSERT OR REPLACE INTO pool_authorized (pool_id, discord_user_id) VALUES (?,?)",
                     (pool_id, user))
    conn.commit()
    return conn

def test_no_key_based_authz_surface_remains():
    """The old key-indirection authz helpers are deleted, not deprecated."""
    assert not hasattr(PoolService, "_can_access")
    assert not hasattr(PoolService, "_is_remote")
    assert not hasattr(PoolService, "import_canonical")
    assert not hasattr(PoolService, "pool_trust")

def test_authz_helpers_never_read_identities_or_members():
    """Grep gate (2109 P1 acceptance): the authorization helpers are one
    indexed query on pool_authorized / owner_discord_user_id — no
    identities/members join, no key lookups. A key appearing anywhere in
    an authorization decision is a bug, so the helper bodies must not
    reference those tables or the old key-indirection helpers."""
    import inspect
    for name in ("_require_owner", "_require_authorized"):
        src = inspect.getsource(getattr(PoolService, name))
        for forbidden in ("identities", "members", "lookup_key_by_discord_user",
                          "maintainer_key_id", "_is_bound_maintainer"):
            assert forbidden not in src, f"{name} references {forbidden}"  # noqa: E211

def test_unmapped_discord_user_refused_via_wrap():
    c=whitelisted(memdb())
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    from __init__ import _wrap_command, _handle_task
    import __init__ as plugin
    orig = plugin._service_for
    plugin._service_for = lambda ctx, source=None: svc
    try:
        class Ctx: pass
        wrapped=_wrap_command(_handle_task, Ctx())
        # An unmapped/uninvited Discord id is refused on every verb.
        out=wrapped("list --pool-id pool-1", source={"user_id": "unmapped-123"})
        assert "not authorized" in out
    finally:
        plugin._service_for = orig

def test_owner_passes_and_non_owner_refused_on_owner_verb():
    c=whitelisted(memdb(), extra=("friend-1",))
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    # Owner mints (auto-approve off by default -> the ticket starts pending).
    t=svc.add_ticket("pool-1","demo","Owner mint", source={"user_id":"owner-1"})
    assert t["ticket_id"] and t["state"]=="pending"
    # Authorized non-owner is refused the owner-only mint (F1: the gateway
    # agent key is the pool maintainer and still does not grant access).
    with pytest.raises(PermissionError):
        svc.add_ticket("pool-1","demo","Friend mint", source={"user_id":"friend-1"})
    # Same user on an authorized-level verb passes (owner approves first).
    svc.approve_ticket(t["ticket_id"], "approve", source={"user_id":"owner-1"})
    svc2=PoolService(c, caller_key_id="agent:friend", caller_signing_key=SigningKey.generate())
    claim=svc2.claim(t["ticket_id"], source={"user_id":"friend-1"})
    assert claim["state"]=="active"

def test_freeze_exact_message_and_event():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    set_frozen(c,"pool-1",True)
    with pytest.raises(Exception, match="pool frozen"):
        svc.add_ticket("pool-1","demo","X")
    # freeze_blocked event written via wrapped path check (service raises exact message)
    try:
        svc.add_ticket("pool-1","demo","X")
    except Exception as e:
        assert str(e)==FROZEN_MESSAGE
    # global freeze blocks claim too
    set_global_frozen(c,True)
    assert FROZEN_MESSAGE=="pool frozen — contact the maintainer"

def test_frozen_blocks_every_user_except_owner_unfreeze():
    """While frozen, every other verb from every user returns exactly the
    frozen message; only the owner's unfreeze runs."""
    c=whitelisted(memdb(), extra=("friend-1",))
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    svc.freeze("pool-1", True, source={"user_id":"owner-1"})
    for user in ("owner-1", "friend-1", "stranger-1"):
        with pytest.raises(Exception) as exc:
            svc.add_ticket("pool-1","demo","X", source={"user_id":user})
        assert str(exc.value)==FROZEN_MESSAGE
    # The owner's unfreeze is the only path that runs while frozen.
    out=svc.freeze("pool-1", False, source={"user_id":"owner-1"})
    assert out["frozen"] is False
    assert svc.add_ticket("pool-1","demo","After unfreeze", source={"user_id":"owner-1"})["ticket_id"]

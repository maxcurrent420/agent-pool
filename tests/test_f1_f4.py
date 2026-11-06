import sqlite3, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, add_member, bind_discord_user, set_auto_approve, list_members
from tickets import PoolService
from nacl.signing import SigningKey, VerifyKey
from nacl.encoding import RawEncoder
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_F1_non_maintainer_mint_refused_through_wrapper():
    # Gateway agent key must not satisfy maintainer checks; Discord binding decides.
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    add_member(c,"pool-1","agent:maint",role="maintainer")
    add_member(c,"pool-1","agent:friend",role="member")
    bind_discord_user(c,"agent:friend","friend-123")
    # Gateway's own service (caller = gateway identity) would pass service-level checks,
    # but the wrapper must compare Discord binding to pools.maintainer_key_id.
    from __init__ import _wrap_command, _handle_task
    import __init__ as plugin
    # Patch _service_for to return a service whose caller IS the maintainer (gateway identity).
    svc_real = PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    orig = plugin._service_for
    plugin._service_for = lambda ctx, source=None: svc_real
    try:
        from types import SimpleNamespace
        ctx = SimpleNamespace()
        wrapped = plugin._wrap_command(plugin._handle_task, ctx)
        out = wrapped('add --pool-id pool-1 --repo demo --title "X" --brief "b"', source={"user_id": "friend-123"})
        assert "not authorized" in out
        # Maintainer binding passes (bound maintainer user)
        bind_discord_user(c,"agent:maint","maint-123")
        out2 = wrapped('add --pool-id pool-1 --repo demo --title "X" --brief "b"', source={"user_id": "maint-123"})
        assert "not authorized" not in out2
    finally:
        plugin._service_for = orig

def test_F2_maintainer_self_wrap_at_register(tmp_path):
    c=memdb()
    maint_sk=SigningKey.generate()
    # identities row for maintainer with box pubkey
    from ledger import init_ledger as _il
    c.execute("INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at) VALUES (?,?,?,?)",
              ("agent:maint", maint_sk.verify_key.encode().hex(), "abcd"*8, 0))
    c.commit()
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=maint_sk)
    svc.register_pool("pool-1","Team")
    rows=list_members(c,"pool-1")
    m=[r for r in rows if r["key_id"]=="agent:maint"][0]
    assert m["box_pubkey_hex"]=="abcd"*8
    # propagator carries maintainer box pubkey
    from transport import FakeTransport
    svc2=PoolService(c, FakeTransport(), caller_key_id="agent:maint", caller_signing_key=maint_sk)
    prop=svc2._propagator("pool-1")
    assert prop._members["agent:maint"].box_pubkey_hex=="abcd"*8

def test_F3_import_anchor_stable_and_maintainer_required():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    from crypto import build_manifest, sign_manifest
    maint_sk=SigningKey.generate(); maint_kid="agent:remote-maint"
    body=build_manifest(repo_id="r1",version=1,content_hash="ab"*32,size_bytes=1,signer_key_id=maint_kid,submitter_key_id=maint_kid,state="accepted")
    m=sign_manifest(maint_sk,body)
    svc=PoolService(c, caller_key_id="agent:alice", caller_signing_key=SigningKey.generate())
    # key without maintainer refused
    with pytest.raises(ValueError, match="--maintainer"):
        svc.import_canonical("pool-remote",[m],{maint_kid: maint_sk.verify_key}, maintainer_key_id=None)
    # with maintainer, anchor key is stable "anchor"
    out=svc.import_canonical("pool-remote",[m],{maint_kid: maint_sk.verify_key}, maintainer_key_id=maint_kid)
    assert out["trust_state"]=="pinned"
    rows=c.execute("SELECT agent_key_id FROM peers WHERE pool_id='pool-remote'").fetchall()
    assert [r[0] for r in rows]==["anchor"]

def test_F4_dead_peer_code_removed():
    assert not hasattr(PoolService, "register_remote_peer")
    assert not hasattr(PoolService, "confirm_peer")

import sqlite3, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, pin_trust_anchor, get_trust_anchor, list_trust_anchors
from tickets import PoolService
from crypto import build_manifest, sign_manifest, manifest_hash
from nacl.signing import SigningKey
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def _manifest(sk, kid, repo="r1", version=1):
    body=build_manifest(repo_id=repo,version=version,content_hash="ab"*32,size_bytes=1,signer_key_id=kid,submitter_key_id=kid,state="accepted")
    return sign_manifest(sk,body)

def test_import_needs_verify_key():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    sk=SigningKey.generate()
    m=_manifest(sk,"agent:maint")
    svc=PoolService(c, caller_key_id="agent:alice", caller_signing_key=SigningKey.generate())
    with pytest.raises(ValueError, match="verify-key"):
        svc.import_canonical("pool-remote",[m],{})
    # F3: key without --maintainer refused (no TOFU via self-declared signer)
    with pytest.raises(ValueError, match="--maintainer"):
        svc.import_canonical("pool-remote",[m],{"agent:maint": sk.verify_key}, maintainer_key_id=None)
    # wrong key refused
    wrong=SigningKey.generate()
    with pytest.raises(ValueError, match="import rejected"):
        svc.import_canonical("pool-remote",[m],{"agent:maint": wrong.verify_key}, maintainer_key_id="agent:maint")

def test_two_pools_same_repo_isolated():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    sk1=SigningKey.generate(); sk2=SigningKey.generate()
    m1=_manifest(sk1,"agent:m1",repo="r1"); m2=_manifest(sk2,"agent:m2",repo="r1")
    svc=PoolService(c, caller_key_id="agent:alice", caller_signing_key=SigningKey.generate())
    svc.import_canonical("pool-a",[m1],{"agent:m1": sk1.verify_key}, maintainer_key_id="agent:m1")
    svc.import_canonical("pool-b",[m2],{"agent:m2": sk2.verify_key}, maintainer_key_id="agent:m2")
    rows=c.execute("SELECT pool_id, repo_id FROM imported_versions ORDER BY pool_id").fetchall()
    assert [(r[0],r[1]) for r in rows]==[("pool-a","r1"),("pool-b","r1")]
    assert get_trust_anchor(c,"pool-a")["trust_state"]=="pinned"

def test_trust_pin_list_revoke_and_own_pool_refused():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    svc=PoolService(c, caller_key_id="agent:maint")
    out=svc.pool_trust("pool-x","agent:m","abcd")
    assert out["trust_state"]=="pinned"
    lst=svc.pool_trust_list()
    assert len(lst["anchors"])==1
    with pytest.raises(ValueError):
        svc.pool_trust("pool-1","agent:m","abcd")
    rev=svc.pool_trust_revoke("pool-x")
    assert rev["trust_state"]=="revoked"
    assert get_trust_anchor(c,"pool-x") is None

def test_remote_chain_verifies_against_pinned():
    from propagation import RepoPropagator
    from transport import FakeTransport
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    maint_sk=SigningKey.generate(); maint_kid="agent:maint"
    # Need pool-remote pinned + imported manifest with wrapped keys for fetch path
    m=_manifest(maint_sk,maint_kid,repo="r1")
    svc=PoolService(c, FakeTransport(), caller_key_id="agent:alice", caller_signing_key=SigningKey.generate())
    svc.import_canonical("pool-remote",[m],{maint_kid: maint_sk.verify_key}, maintainer_key_id=maint_kid)
    # Propagator built via service carries pinned verify key
    prop=svc._propagator("pool-remote")
    assert maint_kid in prop._members
    assert prop._members[maint_kid].verify_key_hex

import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool
from tickets import PoolService, PoolNotLocalError
from crypto import build_manifest, sign_manifest, manifest_hash
from nacl.signing import SigningKey
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_can_access_remote_but_not_local_owned():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    sk=SigningKey.generate()
    svc=PoolService(c, caller_key_id="agent:alice", caller_signing_key=sk)
    svc.register_remote_peer("pool-remote","agent:maint",agent_key_id="agent:alice",trust_state="verified")
    assert svc._can_access("pool-remote") is True
    with pytest.raises(PoolNotLocalError):
        svc._assert_local_owned("pool-remote")
    # import canonical makes list work
    body=build_manifest(repo_id="r1",version=1,content_hash="ab"*32,size_bytes=1,signer_key_id="agent:maint",submitter_key_id="agent:maint",state="accepted")
    m=sign_manifest(sk,body)  # signer kid mismatch intentional? use maint key from sk
    # Use sk as maint signer
    svc2=PoolService(c, caller_key_id="agent:alice", caller_signing_key=SigningKey.generate())
    # Rebuild with correct signer mapping: verify_keys keyed by signer_key_id
    svc.register_remote_peer("pool-remote","agent:maint",agent_key_id="agent:alice",trust_state="verified")
    assert svc._can_access("pool-remote") is True
    # list_tickets on remote returns imported (empty) without raising
    assert svc.list_tickets(pool_id="pool-remote")==[]
    assert svc.list_projects(pool_id="pool-remote")==[]

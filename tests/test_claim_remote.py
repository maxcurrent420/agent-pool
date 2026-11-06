import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, ClaimRefused
from tickets import PoolService
from crypto import build_manifest, sign_manifest
from nacl.signing import SigningKey
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_claim_imported_ticket_and_double_claim_refused():
    c=memdb()
    maint_sk=SigningKey.generate(); maint_kid="agent:maint"
    body=build_manifest(repo_id="r1",version=1,content_hash="ab"*32,size_bytes=1,signer_key_id=maint_kid,submitter_key_id=maint_kid,state="accepted")
    m=sign_manifest(maint_sk,body)
    svc=PoolService(c, caller_key_id="agent:alice", caller_signing_key=SigningKey.generate())
    svc.import_canonical("pool-remote",[m],{maint_kid:maint_sk.verify_key},tickets=[{"ticket_id":"t-9","pool_id":"pool-remote","repo_id":"r1","title":"hi","state":"open"}], maintainer_key_id=maint_kid)
    claim=svc.claim("t-9",key_id="agent:alice")
    assert claim["state"]=="active" and claim.get("remote") is True
    svc2=PoolService(c, caller_key_id="agent:bob", caller_signing_key=SigningKey.generate())
    with pytest.raises(ClaimRefused):
        svc2.claim("t-9",key_id="agent:bob")
    released=svc.release("t-9",key_id="agent:alice")
    assert released["state"]=="open"
    # after release bob can claim
    assert svc2.claim("t-9",key_id="agent:bob")["state"]=="active"

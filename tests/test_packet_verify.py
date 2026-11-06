import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, add_member
from tickets import PoolService
from crypto import build_proposal
from nacl.signing import SigningKey
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def _setup():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    proposer_sk=SigningKey.generate()
    add_member(c,"pool-1","agent:alice",role="member",verify_pubkey_hex=proposer_sk.verify_key.encode().hex())
    # maintainer member row too
    add_member(c,"pool-1","agent:maint",role="maintainer",verify_pubkey_hex="")
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    return c, svc, proposer_sk

def test_byte_flip_refused():
    c, svc, sk = _setup()
    pkt=build_proposal("ticket_proposal",{"pool_id":"pool-1","repo_id":"r1","title":"Good"},"agent:alice",sk)
    bad=dict(pkt); bad["payload"]=dict(pkt["payload"]); bad["payload"]["title"]="evil"
    with pytest.raises(ValueError, match="signature"):
        svc.accept_proposal(bad)
    # nothing minted
    assert c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]==0

def test_unknown_proposer_refused():
    c, svc, sk = _setup()
    other=SigningKey.generate()
    pkt=build_proposal("ticket_proposal",{"pool_id":"pool-1","repo_id":"r1","title":"Good"},"agent:bob",other)
    with pytest.raises(ValueError):
        svc.accept_proposal(pkt)

def test_duplicate_refused():
    c, svc, sk = _setup()
    pkt=build_proposal("ticket_proposal",{"pool_id":"pool-1","repo_id":"r1","title":"Good"},"agent:alice",sk)
    t=svc.accept_proposal(pkt)
    assert t["state"]=="pending"
    with pytest.raises(ValueError, match="duplicate"):
        svc.accept_proposal(pkt)

def test_revoked_proposer_refused():
    c, svc, sk = _setup()
    c.execute("DELETE FROM members WHERE pool_id='pool-1' AND key_id='agent:alice'"); c.commit()
    pkt=build_proposal("ticket_proposal",{"pool_id":"pool-1","repo_id":"r1","title":"Good"},"agent:alice",sk)
    with pytest.raises(ValueError):
        svc.accept_proposal(pkt)

def test_valid_mints_pending():
    c, svc, sk = _setup()
    pkt=build_proposal("ticket_proposal",{"pool_id":"pool-1","repo_id":"r1","title":"Good"},"agent:alice",sk)
    t=svc.accept_proposal(pkt)
    assert t["title"]=="Good" and t["state"]=="pending"

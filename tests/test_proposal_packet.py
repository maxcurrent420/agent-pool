import sqlite3, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, get_ticket
from tickets import PoolService
from crypto import build_proposal, verify_proposal, build_manifest, sign_manifest
from nacl.signing import SigningKey
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_proposal_packet_roundtrip_and_tamper():
    proposer_sk=SigningKey.generate(); proposer_kid="agent:alice"
    pkt=build_proposal("ticket_proposal",{"pool_id":"p1","repo_id":"r1","title":"x"},proposer_kid,proposer_sk)
    assert pkt["kind"]=="ticket_proposal"
    assert verify_proposal(proposer_sk.verify_key, pkt) is True
    tampered=dict(pkt); tampered["payload"]={"pool_id":"p1","repo_id":"r1","title":"evil"}
    assert verify_proposal(proposer_sk.verify_key, tampered) is False

def test_remote_add_ticket_returns_packet_and_maintainer_mints():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    proposer_sk=SigningKey.generate()
    svc=PoolService(c, caller_key_id="agent:alice", caller_signing_key=proposer_sk)
    svc.pool_trust("pool-remote","agent:maint", proposer_sk.verify_key.encode().hex())
    out=svc.add_ticket("pool-remote","r1","Remote task")
    assert out["kind"]=="ticket_proposal" and out["proposal"]["payload"]["title"]=="Remote task"
    assert verify_proposal(proposer_sk.verify_key, out["proposal"]) is True
    # maintainer accepts
    maint_sk=SigningKey.generate()
    maint_svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=maint_sk)
    # payload pool is remote in this DB, so accept would loop; simulate maintainer DB pool as local:
    # Instead directly test accept_proposal rejects bad sig
    bad=dict(out["proposal"]); bad["payload"]={"pool_id":"pool-1","repo_id":"r1","title":"evil"}
    with pytest.raises(ValueError):
        maint_svc.accept_proposal(bad, proposer_verify_key=proposer_sk.verify_key)
    # valid packet minted on maintainer side (pool-1 local)
    pkt=build_proposal("ticket_proposal",{"pool_id":"pool-1","repo_id":"r1","title":"Minted"},"agent:alice",proposer_sk)
    ticket=maint_svc.accept_proposal(pkt, proposer_verify_key=proposer_sk.verify_key)
    assert ticket["ticket_id"] and get_ticket(c,ticket["ticket_id"])["title"]=="Minted"

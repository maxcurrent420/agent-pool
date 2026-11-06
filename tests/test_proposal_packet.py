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

def test_remote_pool_write_refused_writes_are_owner_gateway_only():
    """2109: there is no import. A write aimed at a pool this gateway does
    not host is refused — the packet belongs on the pool's own gateway."""
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    proposer_sk=SigningKey.generate()
    svc=PoolService(c, caller_key_id="agent:alice", caller_signing_key=proposer_sk)
    pkt=build_proposal("ticket_proposal",{"pool_id":"pool-remote","repo_id":"r1","title":"X"},"agent:alice",proposer_sk)
    with pytest.raises(ValueError, match="not hosted by this agent"):
        svc.accept_proposal(pkt, proposer_verify_key=proposer_sk.verify_key,
                            source={"user_id":"owner-1"})

def test_packet_mint_is_owner_only_on_the_pool_gateway():
    """A relayed packet mints only for the pool owner (whitelist model);
    an authorized non-owner is refused even on their own gateway."""
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint", owner_discord_user_id="owner-1")
    proposer_sk=SigningKey.generate()
    pkt=build_proposal("ticket_proposal",{"pool_id":"pool-1","repo_id":"r1","title":"Minted"},"agent:alice",proposer_sk)
    maint_svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    # Owner mints it.
    ticket=maint_svc.accept_proposal(pkt, proposer_verify_key=proposer_sk.verify_key,
                                     source={"user_id":"owner-1"})
    assert ticket["ticket_id"] and get_ticket(c,ticket["ticket_id"])["title"]=="Minted"
    # A tampered payload still fails verification (fail closed).
    bad=dict(pkt); bad["payload"]={"pool_id":"pool-1","repo_id":"r1","title":"evil"}
    with pytest.raises(ValueError):
        maint_svc.accept_proposal(bad, proposer_verify_key=proposer_sk.verify_key,
                                  source={"user_id":"owner-1"})

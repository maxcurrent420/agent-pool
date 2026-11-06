import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, add_member, set_auto_approve, get_ticket
from tickets import PoolService
from nacl.signing import SigningKey
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_pending_hidden_from_non_maintainer():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    add_member(c,"pool-1","agent:maint",role="maintainer")
    add_member(c,"pool-1","agent:alice",role="member")
    maint=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    t=maint.add_ticket("pool-1","demo","Pending one")
    assert t["state"]=="pending"
    member=PoolService(c, caller_key_id="agent:alice")
    assert member.list_tickets(pool_id="pool-1")==[]
    assert maint.list_tickets(pool_id="pool-1")[0]["ticket_id"]==t["ticket_id"]
    # Explicit state filter shows it to member (explicit request)
    assert member.list_tickets(pool_id="pool-1", state="pending")[0]["ticket_id"]==t["ticket_id"]

def test_approve_deny_modify():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    add_member(c,"pool-1","agent:maint",role="maintainer")
    add_member(c,"pool-1","agent:alice",role="member")
    maint=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    member=PoolService(c, caller_key_id="agent:alice")
    t1=maint.add_ticket("pool-1","demo","A")
    with pytest.raises(PermissionError):
        member.approve_ticket(t1["ticket_id"],"approve")
    out=maint.approve_ticket(t1["ticket_id"],"approve")
    assert out["ticket_state"]=="open"
    t2=maint.add_ticket("pool-1","demo","B")
    out=maint.approve_ticket(t2["ticket_id"],"deny", reason="no")
    assert out["ticket_state"]=="blocked"
    t3=maint.add_ticket("pool-1","demo","C")
    out=maint.approve_ticket(t3["ticket_id"],"modify", edit_files={"title":"C2"})
    assert out["ticket_state"]=="open" and get_ticket(c,t3["ticket_id"])["state"]=="open"

import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, set_auto_approve, get_ticket
from tickets import PoolService
from nacl.signing import SigningKey

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_task_add_auto_approve_and_pending():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    # Default is pending (auto-approve off).
    t=svc.add_ticket("pool-1","demo","First")
    assert t["state"]=="pending" and t["ticket_id"]
    # Maintainer sees pending; approve opens it.
    assert any(x["ticket_id"]==t["ticket_id"] for x in svc.list_tickets(pool_id="pool-1"))
    out=svc.approve_ticket(t["ticket_id"],"approve")
    assert out["ticket_state"]=="open" and get_ticket(c,t["ticket_id"])["state"]=="open"
    set_auto_approve(c,"pool-1",True)
    t2=svc.add_ticket("pool-1","demo","Second")
    assert t2["state"]=="open"
    assert get_ticket(c,t2["ticket_id"])["state"]=="open"
    set_auto_approve(c,"pool-1",False)
    t3=svc.add_ticket("pool-1","demo","Third")
    assert t3["state"]=="pending"

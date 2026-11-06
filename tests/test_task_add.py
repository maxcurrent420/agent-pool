import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, get_ticket
from tickets import PoolService
from nacl.signing import SigningKey

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_task_add_always_mints_open():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint", owner_discord_user_id="owner-1")
    from ledger import authorize_user
    authorize_user(c,"pool-1","owner-1")
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    # Phase 1: add_ticket always mints open — no approval queue, no toggle.
    t=svc.add_ticket("pool-1","demo","First", source={"user_id": "owner-1"})
    assert t["state"]=="open" and t["ticket_id"]
    assert any(x["ticket_id"]==t["ticket_id"] for x in svc.list_tickets(pool_id="pool-1", source={"user_id": "owner-1"}))
    assert get_ticket(c,t["ticket_id"])["state"]=="open"
    t2=svc.add_ticket("pool-1","demo","Second", source={"user_id": "owner-1"})
    assert t2["state"]=="open"
    assert get_ticket(c,t2["ticket_id"])["state"]=="open"
    # The deleted approval surface is gone.
    assert not hasattr(svc, "approve_ticket")
    assert not hasattr(svc, "set_auto_approve")
    assert not hasattr(svc, "list_pending")

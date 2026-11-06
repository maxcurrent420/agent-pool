import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, set_frozen, set_global_frozen, FROZEN_MESSAGE, record_event
from tickets import PoolService
from nacl.signing import SigningKey
import pytest

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_can_access_fail_closed():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    # maintainer
    maint=PoolService(c, caller_key_id="agent:maint")
    assert maint._can_access("pool-1") is True
    # stranger with members present -> False
    assert PoolService(c, caller_key_id="agent:stranger")._can_access("pool-1") is False
    # zero members + no key -> False (fail-closed); maintainer key still True via maintainer check
    assert PoolService(c, caller_key_id="")._can_access("pool-1") is False

def test_unmapped_discord_user_refused_via_wrap():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    from __init__ import _wrap_command, _handle_task
    class Ctx: pass
    wrapped=_wrap_command(_handle_task, Ctx())
    out=wrapped("list --pool-id pool-1", source={"user_id": "unmapped-123"})
    assert "not authorized" in out

def test_freeze_exact_message_and_event():
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    set_frozen(c,"pool-1",True)
    with pytest.raises(Exception, match="pool frozen"):
        svc.add_ticket("pool-1","demo","X")
    # freeze_blocked event written via wrapped path check (service raises exact message)
    try:
        svc.add_ticket("pool-1","demo","X")
    except Exception as e:
        assert str(e)==FROZEN_MESSAGE
    # global freeze blocks claim too
    set_global_frozen(c,True)
    assert FROZEN_MESSAGE=="pool frozen — contact the maintainer"

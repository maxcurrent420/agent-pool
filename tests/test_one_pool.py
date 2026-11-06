"""Phase 6: one pool per gateway (WO-017).

Second register refused; foreign ``--pool`` refused naming the hosted
pool; ``pool_gateways`` recorded on interaction (fetch_repo_list for a
foreign pool records role=interact, then refuses); list_projects never
aggregates across hosted pools (flagged follow-up 2).
"""
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent.parent))

import ledger as L
from ledger import create_pool, init_ledger
from tickets import PoolNotLocalError, PoolService


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def world():
    c = memdb()
    create_pool(c, "pool-1", "Team", "agent:maint", owner_discord_user_id="owner-1")
    c.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
              " VALUES ('demo','pool-1','Demo',0)")
    c.commit()
    svc = PoolService(c, caller_key_id="agent:maint",
                      caller_signing_key=SigningKey.generate())
    return c, svc


def test_second_register_refused():
    _c, svc = world()
    with pytest.raises(ValueError, match="one pool per gateway"):
        svc.register_pool("pool-2", "Second", discord_user_id="owner-2")


def test_create_pool_second_refused_at_ledger():
    c, _svc = world()
    with pytest.raises(ValueError, match="one pool per gateway"):
        create_pool(c, "pool-9", "Ninth", "agent:maint")


def test_foreign_pool_refused_naming_hosted_pool():
    _c, svc = world()
    with pytest.raises(PoolNotLocalError) as exc:
        svc._resolve_pool("pool-foreign")
    assert "pool-foreign is not hosted by this gateway" in str(exc.value)
    assert "pool-1" in str(exc.value)
    assert svc._resolve_pool(None) == "pool-1"
    assert svc._resolve_pool("pool-1") == "pool-1"


def test_pool_gateways_recorded_on_interaction():
    c, svc = world()
    with pytest.raises(PoolNotLocalError):
        svc.fetch_repo_list("pool-foreign", source={"user_id": "owner-1"})
    rows = L.list_gateways(c)
    assert len(rows) == 1
    assert rows[0]["pool_id"] == "pool-foreign"
    assert rows[0]["role"] == "interact"


def test_list_projects_no_pool_uses_single_hosted_pool():
    _c, svc = world()
    projects = svc.list_projects(source={"user_id": "owner-1"})
    assert [p["repo_id"] for p in projects] == ["demo"]


def test_list_projects_refuses_multi_host_db():
    """A pre-existing multi-pool DB is refused, never aggregated."""
    c, svc = world()
    # Bypass the create_pool guard to simulate a legacy multi-pool DB.
    c.execute("INSERT INTO pools (pool_id, name, maintainer_key_id,"
              " owner_discord_user_id, created_at)"
              " VALUES ('pool-2','Legacy','agent:maint','owner-1',0)")
    c.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
              " VALUES ('legacy','pool-2','Legacy',0)")
    c.commit()
    with pytest.raises(PoolNotLocalError):
        svc.list_projects(source={"user_id": "owner-1"})
    # An explicit pool id still resolves within the multi-host DB only
    # if it names one pool — _resolve_pool refuses multiples too.
    with pytest.raises(PoolNotLocalError):
        svc.list_projects("pool-1", source={"user_id": "owner-1"})


def test_list_projects_refuses_when_no_pool_hosted():
    c = memdb()
    svc = PoolService(c, caller_key_id="agent:maint",
                      caller_signing_key=SigningKey.generate())
    with pytest.raises(PoolNotLocalError):
        svc.list_projects(source={"user_id": "owner-1"})

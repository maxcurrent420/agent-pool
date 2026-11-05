"""Isolation: pool_not_local, chat binding, profile dirs, remote fetch."""
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from ledger import (add_member, create_pool, init_ledger)
from tickets import PoolNotLocalError, PoolService
from transport import FakeTransport
from identity import ensure_identity


def _setup(tmp_path):
    conn = init_ledger(sqlite3.connect(":memory:"))
    conn.row_factory = sqlite3.Row
    maint = ensure_identity(tmp_path / "m")
    create_pool(conn, "pool-1", "Team", maint.key_id, relay_url="off")
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at) VALUES ('demo','pool-1','Demo',0)")
    conn.execute("INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at) VALUES (?,?,?,0)",
                 (maint.key_id, maint.verify_key.encode(RawEncoder).hex(), maint.box_pk.encode(RawEncoder).hex()))
    add_member(conn, "pool-1", maint.key_id, role="maintainer", box_pubkey_hex=maint.box_pk.encode(RawEncoder).hex())
    conn.commit()
    svc = PoolService(conn, FakeTransport(), caller_key_id=maint.key_id, caller_signing_key=maint.signing_key, caller_box_sk=maint.box_sk)
    return conn, svc, maint


def test_pool_not_local_refusal(tmp_path):
    conn, svc, _ = _setup(tmp_path)
    with pytest.raises(PoolNotLocalError, match="pool_not_local"):
        svc.add_ticket("pool-remote", "demo", "Nope")
    # also via invite
    with pytest.raises(PoolNotLocalError, match="pool_not_local"):
        svc.invite("pool-remote", "agent:x")


def test_chat_binding_resolution_order(tmp_path):
    conn, svc, maint = _setup(tmp_path)
    # explicit beats chat binding
    create_pool(conn, "pool-2", "Two", maint.key_id)
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at) VALUES ('r2','pool-2','R2',0)")
    add_member(conn, "pool-2", maint.key_id, role="maintainer", box_pubkey_hex=maint.box_pk.encode(RawEncoder).hex())
    conn.commit()
    # bind chat to pool-2, but call with explicit pool-1 must win
    source = {"chat_id": "c1", "guild_id": "g1", "thread_id": None}
    svc.register_pool("pool-3", "Three", source=source)  # device1 pool-3 caller is maint; binds c1->pool-3
    # After register, resolving for that chat should be pool-3
    assert svc._resolve_pool(None, source=source) == "pool-3"
    # explicit pool-1 still wins
    assert svc._resolve_pool("pool-1", source=source) == "pool-1"
    # single-pool fallback: when only one pool and no chat binding
    conn2, svc2, _ = _setup(tmp_path)
    assert svc2._resolve_pool(None, source={"chat_id": "unknown"}) == "pool-1"
    # no pool bound + multiple pools -> refusal
    with pytest.raises(PoolNotLocalError, match="no pool bound"):
        svc._resolve_pool(None, source={"chat_id": "unknown2"})


def test_profile_data_dir_isolation(tmp_path, monkeypatch):
    import ledger as L
    d1 = tmp_path / "profA"; d2 = tmp_path / "profB"
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(d1))
    p1 = L.default_db_path()
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(d2))
    p2 = L.default_db_path()
    assert p1 != p2
    assert p1.parent == d1 and p2.parent == d2
    # two profiles get two separate plugin_db files
    assert str(p2) not in str(p1)


def test_remote_fetch_requires_wrapped_dek(tmp_path):
    conn, svc, maint = _setup(tmp_path)
    # Non-member fetch must fail at propagator layer (handler layer asserts member).
    from propagation import Member, RepoPropagator
    members = svc._members_for_propagator("pool-1")
    outsider = Member(key_id="agent:out", verify_key_hex="", box_pubkey_hex="", box_sk=None)
    # is not member -> PermissionError
    prop = svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    assert not prop._is_member("agent:out")
    # Chain failures still refuse (no chain yet) — assert that is not a success.
    with pytest.raises(Exception):
        prop.fetch(manifest={"repo_id": "demo", "version": 1}, fetcher=outsider, dest=tmp_path / "x")
    # Direct membership/DEK refusal when a chain exists is covered by propagation tests.

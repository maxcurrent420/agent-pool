"""Phase 1: three-state ticket lifecycle + one pool per gateway.

create -> claim -> release -> claim -> complete; a claimed task is not in
the available (open) set; a completed task never is; illegal transitions
raise; a second register on the same gateway is refused; foreign --pool
is refused naming the hosted pool. Plus the source-shape gate: the
ticket-state surface carries no old states and no proposal references.
(repo_versions/audits 'pending' literals in the propagation/integrate
later-phase paths are out of scope here — see WO-012.)
"""
import inspect
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent.parent))

import ledger as L
from ledger import (ClaimRefused, complete_ticket, create_pool, create_ticket,
                    get_ticket, init_ledger, release_ticket, set_ticket_state)
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


def test_create_claim_release_claim_complete():
    c, svc = world()
    t = svc.add_ticket("pool-1", "demo", "Work", source={"user_id": "owner-1"})
    assert t["state"] == "open"
    tid = t["ticket_id"]
    svc.claim(tid, key_id="agent:alice", declared_paths=["a.py"], source={"user_id": "owner-1"})
    assert get_ticket(c, tid)["state"] == "claimed"
    # Claimed is not available.
    assert svc.list_tickets(pool_id="pool-1", state="open",
                            source={"user_id": "owner-1"}) == []
    svc.release(tid, key_id="agent:alice", source={"user_id": "owner-1"})
    assert get_ticket(c, tid)["state"] == "open"
    # Phase 1 first-claim binding is permanent per Discord user: owner-1 is
    # bound to agent:alice, so the re-claim/complete reuse that key.
    svc.claim(tid, key_id="agent:alice", declared_paths=["a.py"], source={"user_id": "owner-1"})
    done = svc.complete(tid, key_id="agent:alice", source={"user_id": "owner-1"})
    assert done["state"] == "completed"
    assert get_ticket(c, tid)["state"] == "completed"
    # Claim row ends completed (the latest claim; the first was released).
    row = c.execute("SELECT state FROM claims WHERE ticket_id=? AND state='completed'", (tid,)).fetchone()
    assert row is not None
    # Completed is terminal and never available.
    assert svc.list_tickets(pool_id="pool-1", state="open",
                            source={"user_id": "owner-1"}) == []
    with pytest.raises(ClaimRefused):
        svc.claim(tid, key_id="agent:alice", declared_paths=["a.py"], source={"user_id": "owner-1"})


def test_illegal_transitions_raise():
    c, svc = world()
    create_ticket(c, "t-1", "pool-1", "demo", "Lifecycle")
    with pytest.raises(ValueError, match="not allowed"):
        set_ticket_state(c, "t-1", "completed")  # open -> completed
    set_ticket_state(c, "t-1", "claimed")
    with pytest.raises(ValueError, match="not allowed"):
        set_ticket_state(c, "t-1", "claimed")  # claimed -> claimed
    set_ticket_state(c, "t-1", "open")  # claimed -> open is legal
    assert get_ticket(c, "t-1")["state"] == "open"


def test_ledger_release_and_complete_guards():
    c, _svc = world()
    create_ticket(c, "t-1", "pool-1", "demo", "Guards")
    from ledger import claim_ticket_enforced
    claim_ticket_enforced(c, "c-1", "t-1", "agent:alice", declared_paths=["x"])
    with pytest.raises(ClaimRefused, match="only the claimant"):
        complete_ticket(c, "t-1", "agent:bob")
    with pytest.raises(ClaimRefused, match="only the claimant"):
        release_ticket(c, "t-1", "agent:bob")
    out = complete_ticket(c, "t-1", "agent:alice")
    assert out["state"] == "completed"
    with pytest.raises(ClaimRefused):
        release_ticket(c, "t-1", "agent:alice")


def test_second_register_refused_and_foreign_pool_refused(tmp_path):
    c, svc = world()
    with pytest.raises(ValueError, match="one pool per gateway"):
        svc.register_pool("pool-2", "Second", maintainer_discord_user_id="owner-2")
    with pytest.raises(PoolNotLocalError) as exc:
        svc._resolve_pool("pool-foreign")
    assert "pool-foreign is not hosted by this gateway" in str(exc.value)
    assert "pool-1" in str(exc.value)  # names the hosted pool
    # No --pool: the single hosted pool.
    assert svc._resolve_pool(None) == "pool-1"


def test_record_and_list_gateways():
    c, svc = world()
    assert svc.list_gateways() == []
    svc.record_gateway("pool-x", gateway_endpoint="gw:9", transport_addr="t:9", role="member")
    rows = svc.list_gateways()
    assert rows[0]["pool_id"] == "pool-x" and rows[0]["role"] == "member"


def test_source_shape_no_old_states_or_proposal_refs():
    old = ("pending", "review", "merged", "blocked")
    ticket_surface = [
        L.create_ticket, L.set_ticket_state, L.claim_ticket,
        L.claim_ticket_enforced, L.release_ticket, L.complete_ticket,
        L.create_pool, PoolService.add_ticket, PoolService._resolve_pool,
        PoolService.list_tickets, PoolService.claim, PoolService.release,
        PoolService.complete,
        PoolService.register_pool,
    ]
    for fn in ticket_surface:
        src = inspect.getsource(fn)
        for token in old:
            assert f"'{token}'" not in src and f'"{token}"' not in src, (
                f"{fn.__qualname__} still references old state {token!r}")
    # No proposal machinery anywhere in the service/ledger/crypto modules.
    import crypto as C
    import tickets as T
    for mod in (L, T, C):
        src = inspect.getsource(mod)
        for gone in ("build_proposal", "verify_proposal", "accept_proposal",
                     "proposal_packet_string", "parse_proposal"):
            assert gone not in src, f"{mod.__name__} still references {gone}"
    assert L.TICKET_STATES == ("open", "claimed", "completed")
    for gone in ("_budget_exhausted", "pool_fetch", "work_fetch", "work_submit",
               "start_work", "complete_work", "cancel_work", "report_progress",
               "integrate", "resolve_integration",
               "propose", "approve_ticket", "resolve_pending", "list_pending",
                 "_filter_pending", "set_auto_approve", "_packet_hash",
                 "_member_verify_keys"):
        assert not hasattr(PoolService, gone), gone
    for gone in ("get_pending_manifest", "get_auto_approve", "set_auto_approve",
                 "get_remote_repo", "upsert_remote_repo"):
        assert not hasattr(L, gone), gone

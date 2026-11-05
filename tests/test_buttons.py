"""Buttons: custom_id parse, dispatch, maintainer gate, legacy hook."""
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from bridge import dispatch_button_event, make_platform_event_handler, parse_button_event
from buttons import parse_custom_id
from ledger import add_member, create_pool, init_ledger
from tickets import PoolService
from transport import FakeTransport
from identity import ensure_identity


def test_custom_id_format_and_parse():
    assert parse_custom_id("agent-pool:approve:t-abc123") == ("approve", "t-abc123")
    assert parse_button_event({"custom_id": "agent-pool:discard:t-x1"}) == {"decision": "discard", "ticket_id": "t-x1"}
    assert parse_button_event({"custom_id": "agent-pool:edit:t-zz"}) == {"decision": "edit", "ticket_id": "t-zz"}


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


def test_button_refuses_unmapped_user(tmp_path):
    conn, svc, maint = _setup(tmp_path)
    ticket = svc.add_ticket("pool-1", "demo", "T", brief="b")
    # seed integration ctx so maintainer check is reached — but unmapped must fail first
    with pytest.raises(PermissionError):
        dispatch_button_event(svc, "discord", "button",
                              {"custom_id": f"agent-pool:approve:{ticket['ticket_id']}", "user_id": "unknown"},
                              identity_map={}, work_dir=tmp_path)
    # no dispatch happened (ticket still open)
    assert svc.show_ticket(ticket["ticket_id"])["state"] == "open"


def test_button_refuses_non_maintainer(tmp_path):
    conn, svc, maint = _setup(tmp_path)
    other = ensure_identity(tmp_path / "o")
    conn.execute("INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at) VALUES (?,?,?,0)",
                 (other.key_id, other.verify_key.encode(RawEncoder).hex(), other.box_pk.encode(RawEncoder).hex()))
    add_member(conn, "pool-1", other.key_id, role="member", box_pubkey_hex=other.box_pk.encode(RawEncoder).hex())
    conn.commit()
    ticket = svc.add_ticket("pool-1", "demo", "T", brief="b")
    with pytest.raises(PermissionError):
        dispatch_button_event(svc, "discord", "button",
                              {"custom_id": f"agent-pool:approve:{ticket['ticket_id']}", "user_id": "u-other"},
                              identity_map={"u-other": other.key_id}, work_dir=tmp_path)


def test_button_dispatches_maintainer_decision(tmp_path):
    # Use mocked resolve_integration to avoid full gate.
    conn, svc, maint = _setup(tmp_path)
    ticket = svc.add_ticket("pool-1", "demo", "Gate", brief="b")
    # need review state + gate ctx: stub integrate by directly setting state + ctx file
    # Instead test dispatch path via monkeypatching resolve.
    called = {}

    def fake_resolve(tid, decision, *, key_id=None, **kw):
        called["decision"] = decision; called["tid"] = tid
        return {"ok": True, "decision": decision, "ticket_id": tid, "ticket_state": "blocked", "merged": False}
    svc.resolve_integration = fake_resolve
    # discard requires review; our fake bypasses state, so transition to review first via ledger
    # Fake resolve bypasses state transitions; no ledger change needed.
    import ledger as _L
    _L.bind_discord_user(conn, maint.key_id, "u-maint")
    result = dispatch_button_event(svc, "discord", "button",
                                   {"custom_id": f"agent-pool:discard:{ticket['ticket_id']}", "user_id": "u-maint"},
                                   identity_map={}, work_dir=tmp_path)
    assert result["dispatched"] is True and called["decision"] == "discard"


def test_bridge_hook_ignores_non_button_events(tmp_path):
    conn, svc, maint = _setup(tmp_path)
    handler = make_platform_event_handler(lambda: svc)
    assert handler("discord", "message_edited", {"chat_id": "1", "text": "edited"}) is None
    assert handler("discord", "reaction", {"emojis": []}) is None
    assert parse_button_event({"chat_id": "1"}) is None

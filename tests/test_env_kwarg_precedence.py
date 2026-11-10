"""Explicit maintainer ids threaded into PoolService must win over env.

Verified defect: the constructor let AGENT_POOL_MAINTAINER_* env vars
override explicit kwargs, so in an operator shell exporting the real
ids, fixture ids stopped being maintainer ids and 16 tests hard-failed
(claim refused: user owner-1 has no bound key). The plugin layer already
resolves env-beats-config; the constructor must not override an id the
caller threaded explicitly.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from identity import ensure_identity
from ledger import init_ledger
from tickets import PoolService
from transport import FakeTransport


def _svc(tmp_path, **kw):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn = init_ledger(conn)
    ident = ensure_identity(tmp_path / "k")
    sender = PoolService(conn, FakeTransport(),
                        caller_key_id=ident.key_id,
                        caller_signing_key=ident.signing_key,
                        caller_box_sk=ident.box_sk, **kw)
    return conn, sender


def test_explicit_maintainer_id_beats_ambient_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", "someone-else")
    _conn, svc = _svc(tmp_path, maintainer_discord_user_id="owner-1")
    assert "owner-1" in svc._maintainer_ids()
    assert "someone-else" not in svc._maintainer_ids()


def test_explicit_bot_id_beats_ambient_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_POOL_MAINTAINER_BOT_DISCORD_USER_ID", "someone-else")
    _conn, svc = _svc(tmp_path, maintainer_bot_discord_user_id="bot-1")
    assert "bot-1" in svc._maintainer_ids()
    assert "someone-else" not in svc._maintainer_ids()


def test_env_fallback_when_no_explicit_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_POOL_MAINTAINER_DISCORD_USER_ID", "env-owner")
    _conn, svc = _svc(tmp_path)
    assert svc._maintainer_discord_user_id == "env-owner"


def test_suite_is_scrubbed_of_ambient_env():
    # The session guard guarantees no ambient maintainer id leaks in.
    for key in ("AGENT_POOL_MAINTAINER_DISCORD_USER_ID",
                "AGENT_POOL_MAINTAINER_BOT_DISCORD_USER_ID"):
        monkey = os.environ.get(key, "")
        if monkey and monkey not in ("maint-bot-1",):
            pytest.fail(f"ambient {key} leaked into the test session")

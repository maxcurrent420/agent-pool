"""add_repo semantics: --path is required and the --link pointer form is
deleted (Phase 2 / WO-013) — passing repo_link is a TypeError, before any
write. All propagation runs through FakeTransport; the only real
subprocesses are ``git`` for fixture repos.
"""
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool
from tickets import PoolService


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _svc(c):
    create_pool(c, "pool-1", "Team", "agent:maint")
    return PoolService(c, caller_key_id="agent:maint",
                       caller_signing_key=SigningKey.generate())


def test_add_repo_requires_path():
    svc = _svc(memdb())
    with pytest.raises(ValueError, match="--path"):
        svc.add_repo("pool-1", "demo", "")


def test_add_repo_link_kwarg_rejected(tmp_path):
    """The --link branch is deleted: repo_link is an unexpected keyword."""
    svc = _svc(memdb())
    with pytest.raises(TypeError):
        svc.add_repo("pool-1", "demo", str(tmp_path),
                     repo_link="https://example/demo.git")


def test_add_repo_link_attempt_writes_nothing():
    """TypeError is raised before any write: no remote_repos / repo_versions
    rows for an add_repo that carries the deleted link kwarg."""
    c = memdb()
    svc = _svc(c)
    with pytest.raises(TypeError):
        svc.add_repo("pool-1", "demo", "", repo_link="https://example/demo.git")
    assert c.execute("SELECT COUNT(*) FROM remote_repos").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM repo_versions").fetchone()[0] == 0

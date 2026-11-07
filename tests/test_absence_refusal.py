"""Phase 0 (WO-019): absence of a record = refusal on every verb.

The legacy-skip branches in ``_require_owner`` / ``_require_authorized``
(previously: a pool with no owner row and no ``pool_authorized`` rows
silently passed) are deleted. These tests pin the refusal semantics,
the ``--link`` rejection, and the transport-only per-pool config.
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from ledger import init_ledger, create_pool  # noqa: E402
from tickets import PoolService  # noqa: E402
from transport import FakeTransport  # noqa: E402

POOL = "bare-pool"


def _bare_world():
    """A pool row with NO owner_discord_user_id and NO pool_authorized rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_ledger(conn)
    create_pool(conn, POOL, "Bare", maintainer_key_id="agent:maintainer")
    svc = PoolService(conn, FakeTransport(), caller_key_id="agent:maintainer")
    return conn, svc


def test_require_owner_refuses_without_rows():
    _conn, svc = _bare_world()
    with pytest.raises(
        PermissionError,
        match="not authorized for pool bare-pool — ask the maintainer",
    ):
        svc._require_owner(POOL, source={"user_id": "anyone"})


def test_require_authorized_refuses_without_rows():
    _conn, svc = _bare_world()
    with pytest.raises(
        PermissionError,
        match="not authorized for pool bare-pool — ask the maintainer",
    ):
        svc._require_authorized(POOL, source={"user_id": "anyone"})


def test_link_flag_is_rejected():
    spec = importlib.util.spec_from_file_location(
        "agent_pool_plugin", Path(__file__).parent.parent / "__init__.py"
    )
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    out = plugin._handle_add_repo(None, "demo --link https://example.invalid/repo.git")
    assert "add-repo: --link is not supported; use /add-repo --path <checkout>" in out


def test_pools_config_is_transport_only():
    doc = yaml.safe_load((Path(__file__).parent.parent / "plugin.yaml").read_text())
    pools = doc["config_schema"]["pools"]
    assert pools["description"] == (
        "Deprecated and ignored: per-pool transport settings have no effect — "
        "Iroh-over-Tor is the only transport for every pool. Legacy "
        "transport_mode=relay entries are ignored with a migration warning; "
        "remove them."
    )
    assert "budget" not in pools["description"].lower()

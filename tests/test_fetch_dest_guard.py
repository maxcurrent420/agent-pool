import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from propagation import _resolve_fetch_dest  # noqa: E402


def test_default_fetch_dest_is_plugin_data(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "pd"))
    assert _resolve_fetch_dest(None) == (tmp_path / "pd" / "fetch").resolve()


def test_fetch_dest_outside_roots_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "pd"))
    with pytest.raises(ValueError, match="fetch --dest"):
        _resolve_fetch_dest("/tmp/fetch")
    with pytest.raises(ValueError, match="fetch --dest"):
        _resolve_fetch_dest(str(tmp_path / "pd" / ".." / "elsewhere"))


def test_fetch_dest_under_worktree_root_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "pd"))
    monkeypatch.setenv("AGENT_POOL_WORKTREE_ROOT", str(tmp_path / "wt"))
    out = _resolve_fetch_dest(str(tmp_path / "wt" / "repo" / "dl"))
    assert out == (tmp_path / "wt" / "repo" / "dl").resolve()

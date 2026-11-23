import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from propagation import ClamAVUnavailable, RepoPropagator  # noqa: E402


def test_no_auto_provision_kwarg_exists():
    assert "auto_provision" not in RepoPropagator.__init__.__code__.co_varnames
    assert not hasattr(RepoPropagator, "_provision_clamav_once")


def test_audit_clamav_absent_provisions_nothing(monkeypatch, tmp_path):
    import propagation as P
    calls = []
    monkeypatch.setattr(P.subprocess, "run",
                        lambda *a, **k: calls.append((a, k)))

    def _unavailable(*a, **k):
        raise P.ClamAVUnavailable("no engine")

    monkeypatch.setattr(P, "clamav_scan", _unavailable)
    prop = RepoPropagator.__new__(RepoPropagator)
    prop._clamd_socket = "/nonexistent/clamd.ctl"
    with pytest.raises(RuntimeError) as exc:
        prop._clamav_or_fail_closed(tmp_path)
    assert "audit refused: ClamAV unavailable" in str(exc.value)
    assert "bash scripts/install-deps.sh" in str(exc.value)
    assert calls == []

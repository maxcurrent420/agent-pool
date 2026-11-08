"""WO-003: mandatory real ClamAV in the ingestion path.

Real engine only: the host's clamscan/clamd do the scanning. The EICAR
fixture is the standard antivirus test signature, assembled from
fragments so the literal never appears in this file.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from propagation import ClamAVUnavailable, _sandbox_scan, clamav_scan  # noqa: E402

# Standard antivirus test-file signature, fragment-assembled.
EICAR = (b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-"
         b"ANTIVIRUS-TEST-FILE!$H+H*")

pytestmark = pytest.mark.skipif(
    shutil.which("clamscan") is None,
    reason="clamscan not installed on this host")


def _tree(tmp_path, files):
    root = tmp_path / "tree"
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def test_clamav_scan_clean_tree(tmp_path):
    tree = _tree(tmp_path, {"a.txt": b"hello world\n", "sub/b.py": b"x = 1\n"})
    # Force the clamscan fallback path (no daemon socket on this host).
    out = clamav_scan(tree, clamd_socket="/nonexistent/clamd.ctl")
    assert out["engine"] == "clamav"
    assert out["via"] == "clamscan"
    assert out["verdict"] == "clean"
    assert out["infected"] == []
    assert out["signature_version"]


def test_clamav_scan_eicar_infected(tmp_path):
    tree = _tree(tmp_path, {"evil.bin": EICAR, "ok.txt": b"fine\n"})
    out = clamav_scan(tree, clamd_socket="/nonexistent/clamd.ctl")
    assert out["verdict"] == "infected"
    assert any(i["path"].endswith("evil.bin") for i in out["infected"])


def test_clamav_scan_engine_absent_fails_closed(tmp_path, monkeypatch):
    tree = _tree(tmp_path, {"a.txt": b"hello\n"})
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(ClamAVUnavailable):
        clamav_scan(tree, clamd_socket="/nonexistent/clamd.ctl")


def test_audit_layers_compose_flagged_over_clean(tmp_path):
    """Layer composition: a keyword hit flags a ClamAV-clean tree, and a
    ClamAV infection refuses even a keyword-clean tree (the aggregate
    rule mirrors RepoPropagator.audit)."""
    tree = _tree(tmp_path, {"kw.py": b'import os\nos.system("ls")\n'})
    static = _sandbox_scan(tree)
    clamav = clamav_scan(tree, clamd_socket="/nonexistent/clamd.ctl")
    assert static["verdict"] == "flagged"  # keyword layer
    assert clamav["verdict"] == "clean"    # real engine: no signature hit
    # Aggregate rule mirrors RepoPropagator.audit.
    verdict = ("infected" if clamav["verdict"] == "infected"
               else static["verdict"])
    assert verdict == "flagged"

    evil = _tree(tmp_path / "e", {"evil.bin": EICAR})
    clamav_evil = clamav_scan(evil, clamd_socket="/nonexistent/clamd.ctl")
    assert clamav_evil["verdict"] == "infected"


def test_audit_clamav_absent_fails_closed_exact_text(tmp_path, monkeypatch):
    """Engine absent + remediation disabled -> the audit fails closed with
    the exact operator-facing text (never a keyword-only verdict)."""
    from propagation import RepoPropagator
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    prop = RepoPropagator.__new__(RepoPropagator)
    prop._clamd_socket = "/nonexistent/clamd.ctl"
    prop._auto_provision = False
    tree = _tree(tmp_path, {"a.txt": b"hello\n"})
    with pytest.raises(RuntimeError) as exc:
        prop._clamav_or_fail_closed(tree)
    assert str(exc.value).startswith(
        "audit refused: ClamAV unavailable — run bash scripts/install-deps.sh")

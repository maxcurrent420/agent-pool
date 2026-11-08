"""WO-002: tor sidecar resolution order (no new transport, no mocks).

Resolution order: tor_binary config > send_hermes-tor build on PATH >
stock send_hermes (which tor_ready still probes for the tor flag and
fails closed on when absent).
"""

import os
import stat

import pytest

import transport as T


def _stub(tmp_path, name, help_text):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"send\" ] && [ \"$2\" = \"--help\" ]; then\n"
        f"  printf '%s\\n' {help_text!r}\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(bindir)


TOR_HELP = "--transport tor"
STOCK_HELP = "usage: send_hermes send"


def _path_with(monkeypatch, *dirs):
    monkeypatch.setenv("PATH", os.pathsep.join(dirs) + os.pathsep + "/usr/bin:/bin")


def test_config_wins_over_path(monkeypatch, tmp_path):
    tor_dir = _stub(tmp_path, "send_hermes-tor", TOR_HELP)
    _stub(tmp_path, "send_hermes", STOCK_HELP)
    _path_with(monkeypatch, tor_dir)
    assert T.resolve_tor_binary("/opt/agent-pool-iroh") == "/opt/agent-pool-iroh"


def test_tor_build_wins_over_stock(monkeypatch, tmp_path):
    tor_dir = _stub(tmp_path, "send_hermes-tor", TOR_HELP)
    _stub(tmp_path, "send_hermes", STOCK_HELP)
    _path_with(monkeypatch, tor_dir)
    resolved = T.resolve_tor_binary()
    assert resolved.endswith("send_hermes-tor")


def test_stock_used_only_when_no_tor_build(monkeypatch, tmp_path):
    stock_dir = _stub(tmp_path, "send_hermes", STOCK_HELP)
    _path_with(monkeypatch, stock_dir)
    assert T.resolve_tor_binary().endswith("send_hermes")
    # ...but tor_ready still fails closed: the stock binary does not
    # advertise the tor transport flag.
    with pytest.raises(T.TorNotReady) as exc:
        T.tor_ready(control_port=9051, socks_port=9055, timeout=1.0)
    assert "--transport" in str(exc.value)


def test_tor_build_passes_probe(monkeypatch, tmp_path):
    # Tor build present: tor_ready gets past the binary probe. On this
    # host the daemon answers on 9051, so readiness is fully OK; in an
    # environment without the daemon it must fail at the control-port
    # stage (never at the binary stage).
    tor_dir = _stub(tmp_path, "send_hermes-tor", TOR_HELP)
    _stub(tmp_path, "send_hermes", STOCK_HELP)
    _path_with(monkeypatch, tor_dir)
    try:
        ready = T.tor_ready(control_port=9051, socks_port=9055, timeout=2.0)
    except T.TorNotReady as exc:
        assert "control port" in str(exc.value)
        assert "--transport" not in str(exc.value)
    else:
        assert ready["ok"] is True
        assert ready["binary"].endswith("send_hermes-tor")


def test_no_binary_fails_closed(monkeypatch, tmp_path):
    empty = tmp_path / "bin"
    empty.mkdir()
    _path_with(monkeypatch, str(empty))
    with pytest.raises(T.TorNotReady):
        T.tor_ready(timeout=1.0)

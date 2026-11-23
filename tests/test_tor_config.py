from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_torrc_template_is_separate_and_cookie_auth():
    tpl = (REPO / "ops" / "agent-pool.torrc").read_text()
    assert "@DATA_DIR@" in tpl
    assert "CookieAuthentication 1" in tpl
    assert "CookieAuthFile @DATA_DIR@/control_auth_cookie" in tpl
    assert "/home/maxcurrent" not in tpl
    assert not (REPO / "ops" / "torrc").exists()


def test_units_use_dedicated_config_and_no_host_path():
    tor = (REPO / "ops/systemd/agent-pool-tor.service").read_text()
    assert "-f /etc/tor/agent-pool.torrc" in tor
    assert "User=" in tor
    sh = (REPO / "scripts/lib/tor-config.sh").read_text()
    assert "/etc/tor/agent-pool.torrc" in sh
    assert "/etc/tor/torrc" not in sh   # legacy config is never written
    for p in (REPO / "scripts/install-tor.sh", REPO / "scripts/install-deps.sh"):
        assert "/etc/tor/torrc" not in p.read_text().replace("tor-config.sh", "")

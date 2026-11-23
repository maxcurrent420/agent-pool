from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_no_operator_host_paths_in_shipped_files():
    bad = "/home/maxcurrent"
    paths = [REPO / n for n in (
        "tickets.py", "transport.py", "__init__.py", "propagation.py",
        "README.md", "ops/systemd/agent-pool-sendhermes.service",
        "ops/tor-build.md", "scripts/live_discord_check.py",
        "scripts/live-discord-check.md", "scripts/demo.py",
        "scripts/demo.sh", "scripts/measure_300mb.py",
        "skills/agent-pool/SKILL.md")]
    hits = [str(p.relative_to(REPO)) for p in paths
            if p.exists() and bad in p.read_text()]
    assert hits == []

#!/usr/bin/env python3
"""Live Discord test preflight — PRINTS ONLY, never acts.

Checks read-only state (filesystem, PATH, `hermes plugins list` output)
and prints the operator commands from scripts/live-discord-check.md.
Does not install, enable, disable, or restart anything.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN = Path("/home/maxcurrent/Documents/AntiGravityProjects/agent-pool")
PLUGINS_DIR = Path.home() / ".hermes" / "plugins"
LINK = PLUGINS_DIR / "agent-pool"


def check(label, ok, detail=""):
    print(f"[{'OK ' if ok else 'MISS'}] {label}" + (f" — {detail}" if detail else ""))


def main():
    print("=== agent-pool live Discord preflight (read-only) ===\n")

    # Plugin source + manifest
    check("plugin source present", PLUGIN.is_dir(), str(PLUGIN))
    manifest = PLUGIN / "plugin.yaml"
    check("plugin.yaml present", manifest.is_file())
    if manifest.is_file():
        text = manifest.read_text(errors="replace")
        check("gateway_channel_id absent (migrated)", "gateway_channel_id" not in text)
        for line in text.splitlines():
            if line.startswith("version:"):
                print(f"       manifest {line}")

    # Install target state
    if LINK.is_symlink():
        check("~/.hermes/plugins/agent-pool symlink", True, f"-> {os.readlink(LINK)}")
    elif LINK.exists():
        check("~/.hermes/plugins/agent-pool symlink", False,
              "exists but is not a symlink; remove it before linking")
    else:
        check("~/.hermes/plugins/agent-pool symlink", False,
              "not installed yet (step 1 of live-discord-check.md)")

    # Tools
    check("send_hermes on PATH", shutil.which("send_hermes") is not None,
          shutil.which("send_hermes") or "missing")
    check("hermes on PATH", shutil.which("hermes") is not None,
          shutil.which("hermes") or "missing")
    check("bubblewrap on PATH", shutil.which("bwrap") is not None,
          shutil.which("bwrap") or "missing (scanner will run unsandboxed)")

    # Python deps in the system interpreter (the Hermes runtime venv
    # lacks blake3/PyNaCl/pytest by design — see README "Tests").
    for mod in ("nacl", "blake3"):
        try:
            __import__(mod)
            check(f"python dep importable: {mod}", True)
        except ImportError:
            check(f"python dep importable: {mod}", False)

    # Read-only plugin list
    hermes = shutil.which("hermes")
    if hermes:
        print("\n--- hermes plugins list (read-only) ---")
        try:
            out = subprocess.run([hermes, "plugins", "list"],
                                 capture_output=True, text=True, timeout=30)
            print(out.stdout.strip() or "(empty)")
            if out.stderr.strip():
                print("stderr:", out.stderr.strip()[:300])
        except Exception as exc:
            print(f"could not run `hermes plugins list`: {exc}")

    print("\n=== next commands (from scripts/live-discord-check.md) ===")
    steps = [
        "1. ln -s /home/maxcurrent/Documents/AntiGravityProjects/agent-pool ~/.hermes/plugins/agent-pool",
        "2. hermes plugins enable agent-pool",
        "3. Restart the gateway (slash sync adapter.py:1556-1558 + handler wiring adapter.py:1573)",
        "4. Config: relay_url, budgets, transport=real (no gateway_channel_id)",
        "5. Discord: bot allowlist contains tester; 7 commands in picker",
        "6. /pool register demo-pool --name Demo --discord-user <id>",
        "7. /pool invite demo-pool <key_id> --box-pubkey <hex> --discord-user <id>",
        "8. /task add --pool-id demo-pool --repo demo --title \"Live test\"",
        "9. /claim <ticket> --paths <file>",
        "10. /work submit <ticket> --repo-path <repo> --version 1",
        "11. /integrate <ticket>  ->  click Approve",
        "12. /audit show <ticket>",
        "13. Verify: gateway payloads <=4KB; fetched BLAKE3 == manifest content_hash",
        "14. Roll back: hermes plugins disable agent-pool; restart gateway",
    ]
    for s in steps:
        print("   " + s)
    print("\nNothing was installed, enabled, or restarted by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

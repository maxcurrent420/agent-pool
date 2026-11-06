# Live Discord test — operator checklist (PREPARE, DO NOT EXECUTE)

This document contains the exact operator steps for the live gateway test
on this host. **Nothing here has been run.** Do not install, enable, or
restart anything until the maintainer says go. The helper
`scripts/live_discord_check.py` only prints state + these commands; it
never acts.

Plugin source: `/home/maxcurrent/Documents/AntiGravityProjects/agent-pool`
Hermes checkout (read-only reference): `/home/maxcurrent/.hermes/hermes-agent`

## Preflight (read-only — safe to run anytime)

0. Print state and the commands below without acting:
   `python3 scripts/live_discord_check.py`
   (It checks: plugin dir present, `~/.hermes/plugins/` layout,
   `send_hermes` on PATH, `hermes` on PATH, python deps in the system
   interpreter, and `hermes plugins list` read-only output.)

## Install into the live gateway profile

1. Link the plugin into the Hermes plugins dir. The plugin uses relative
   imports (flat layout at repo root), so a **symlink** is the right
   approach:
   ```bash
   ln -s /home/maxcurrent/Documents/AntiGravityProjects/agent-pool \
         ~/.hermes/plugins/agent-pool
   ```
2. Enable it (installs declared Python deps pynacl + blake3 at enable
   time; expect a WARN that blake3 is missing from the Hermes runtime
   venv — tests and the plugin run under the system interpreter's deps):
   ```bash
   hermes plugins enable agent-pool
   ```
3. **Restart the gateway.** REQUIRED — slash-command tree sync runs at
   Discord `connect()`:
   - `H/plugins/platforms/discord/adapter.py:1556-1558`
     (`_register_slash_commands()` at connect).
   New commands do not appear in the Discord picker until this restart.
   There is no hot-reload.

## Tor (optional, for `transport_mode=tor`)

4. Install Tor + sidecar on EVERY gateway in the pool (both endpoints
   must enable Tor; mixed pools fail closed):
   ```bash
   apt install tor
   scripts/install-tor.sh   # writes ops/torrc, builds sidecar, enables agent-pool-tor.service
   systemctl start agent-pool-tor
   ```
   `transport_mode=tor` is opt-in; `relay` mode needs none of this and
   stays the out-of-the-box mode.

## Config

5. Set in the profile's plugin config (no `gateway_channel_id` exists —
   gateway routing is Hermes's):
   - `transport`: `real` (send_hermes/Iroh; `fake` is tests/CLI only)
   - `transport_mode`: `relay` (default) or `tor`
   - `relay_url`: `""` (default n0 relay) or a self-hosted relay URL
   - budgets: `max_turns` 25, `max_tasks_per_day` 5, `token_cap` 50000,
     `wall_clock_seconds` 1800 (or per-pool overrides under `pools`)
   - confirm `send_hermes` is on PATH and `bubblewrap` is installed
     (without bwrap the scanner runs unsandboxed and says so).

## Discord-side setup

6. Pick a guild channel for the run. The bot's `DISCORD_ALLOWED_USERS`
   must contain the tester's Discord user id — that allowlist
   (`adapter.py:4159-4167`) is the only ingress gate; there is no
   plugin-side Discord client.
7. After the restart, verify all **ten** commands in the picker:
   `pool`, `fetch`, `task`, `add-repo`, `task_add`, `task_list`, `claim`,
   `release`, `push`, `audit` (each with one optional `args` string
   option from its `args_hint`).

## The command sequence (in Discord)

8. Register a pool and bind yourself as owner — the `--discord-user`
   you pass here becomes the whitelist owner and is the authorization for
   every owner-only verb:
   ```text
   /pool register demo-pool --name Demo --discord-user <your Discord user id>
   ```
9. Invite a second member (optional; single-agent run is fine):
   ```text
   /pool invite demo-pool <member key_id> --box-pubkey <hex> --discord-user <their Discord user id>
   ```
10. Add a repo (owner-only):
    ```text
    /add-repo demo --path /srv/repos/demo --pool demo-pool --name "Demo"
    ```
11. Create a ticket (owner-only):
    ```text
    /task add --pool-id demo-pool --repo demo --title "Live test" --brief "first live run"
    ```
    (Note the returned `ticket_id`.)
12. A non-invitee must be refused on every verb — try with a third
    account before continuing (expect a permission refusal, never a
    dispatch).
13. Claim it (any authorized user may):
    ```text
    /claim <ticket_id>
    ```
14. Fetch the repo list (read surface):
    ```text
    /fetch repo list --pool demo-pool
    ```
15. Fetch the repo manifest:
    ```text
    /fetch repo demo --pool demo-pool
    ```
16. Push (grant on the pool's gateway; claimant-only):
    ```text
    /push repo demo --ticket <ticket_id>
    ```
    (Returns a one-time grant token + gateway addressing.)
17. Build + send the bundle on the contributor's own gateway
    (`PoolService.push_build_bundle` — service-level, no slash command),
    then deliver on the pool's gateway:
    ```text
    /push repo --deliver <transfer_ticket> --token <grant> --manifest '<contributor-signed manifest json>'
    ```
    Expect the sandbox-scan report; on a clean scan the task completes.
18. Verify the audit:
    ```text
    /audit show <ticket_id>
    ```
19. Confirm the task is now completed (not available):
    ```text
    /fetch task list --pool demo-pool
    ```
    (The claimed/completed ticket must not appear as available.)

## Verify repo bytes never hit Discord

20. **Gateway payload size check.** The plugin's content guard caps
    coordination payloads at 4 KB (`P/replies.py:check_content_size`),
    and measured gateway payloads were ≤ ~1.6 KB with
    `gateway_calls_over_4KB=0`
    (`.hermes/reports/phase2-propagation.md:20`). Check the gateway log
    for this run: every Discord send from the plugin's replies must be a
    short manifest/notice — grep the gateway's Discord adapter send log
    for the ticket id and confirm no send body contains repo content.
21. **Iroh-side evidence.** The push reply (and the ledger
    `manifest_chain` row) carries the `transfer_ticket` / `content_hash`
    (BLAKE3). Fetch the accepted version on the consumer side and confirm
    the fetched bundle's BLAKE3 equals the manifest `content_hash` — that
    match is the proof the bytes travelled the Iroh path.

## Roll back

22. Disable and restart:
    ```bash
    hermes plugins disable agent-pool
    # restart the gateway
    rm ~/.hermes/plugins/agent-pool   # remove the symlink (optional)
    ```
    The pool ledger and keys stay under the profile's plugin-data dir;
    nothing else on the host changes.

## What this does NOT cover

- A real Discord button click (there are no buttons in 2.6.0 — the audit
  gate is automatic on push-deliver).
- Multi-machine pools: single-host run first; cross-machine uses the same
  flow with `relay_url` left at the default.
- `transport_mode=tor`: requires Tor installed and running on both
  endpoints; verify `tor_ready()` passes before attempting a tor-mode
  transfer.

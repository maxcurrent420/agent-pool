# Live Discord test — operator checklist (PREPARE, DO NOT EXECUTE)

This document contains the exact operator steps for the live gateway
test on this host. **Nothing here has been run.** Do not install,
enable, or restart anything until the maintainer says go. The helper
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

1. Link the plugin into the Hermes plugins dir. The plugin uses
   relative imports (flat layout at repo root), so a **symlink** is
   the right approach:
   ```bash
   ln -s /home/maxcurrent/Documents/AntiGravityProjects/agent-pool \
         ~/.hermes/plugins/agent-pool
   ```
2. Enable it (installs declared Python deps pynacl + blake3 at enable
   time; expect a WARN that blake3 is missing from the Hermes runtime
   venv — tests and the plugin run under the system interpreter's
   deps):
   ```bash
   hermes plugins enable agent-pool
   ```
3. **Restart the gateway.** REQUIRED — slash-command tree sync runs at
   Discord connect. New commands do not appear in the Discord picker
   until this restart. There is no hot-reload.

## Tor (optional, for `transport_mode=tor`)

4. Install Tor + sidecar on EVERY gateway in the pool (both endpoints
   must enable Tor; mixed pools fail closed):
   ```bash
   apt install tor
   scripts/install-tor.sh   # writes ops/torrc, builds sidecar, enables the tor service
   systemctl start agent-pool-tor
   ```
   `transport_mode=tor` is opt-in; `relay` mode needs none of this and
   stays the out-of-the-box mode.

## Config

5. Set in the profile's plugin config (no `gateway_channel_id` exists —
   gateway routing is Hermes's):
   - `transport`: `real` (send_hermes/Iroh; `fake` is tests/CLI only)
   - `transport_mode`: `relay` (default) or `tor`
   - `relay_url`: `""` (default relay) or a self-hosted relay URL
   - `allowed_users`: the Discord user ids allowed on the hosted pool
     (a list), or env `AGENT_POOL_ALLOWED_USERS` (comma-separated) —
     env wins over config; the pool owner is always whitelisted.
   - `max_tasks_per_day`: `5` (default per-agent completion
     allocation); `agent_task_allocations` / env
     `AGENT_POOL_TASK_ALLOCATIONS` override it per agent.
   - Secondary per-claim limits (checked only from agent-reported
     usage): `max_turns` 25, `token_cap` 50000, `wall_clock_seconds`
     1800. There are no budget keys under `pools` — `pools` holds
     `transport_mode`/`relay_url` only.
   - confirm `send_hermes` is on PATH and `bubblewrap` is installed
     (without bwrap the scanner runs unsandboxed and says so).

## Discord-side setup

6. Pick a guild channel for the run. The bot's `DISCORD_ALLOWED_USERS`
   must contain the tester's Discord user id — that allowlist is the
   only ingress gate; there is no plugin-side Discord client.
7. After the restart, verify all **ten** commands in the picker:
   `pool`, `fetch`, `task`, `add-repo`, `task_add`, `task_list`,
   `claim`, `release`, `push`, `audit` (each with one optional `args`
   string option from its args hint). Also verify the two tools
   `pool_status` and `pool_report_usage` are registered (toolset
   `agent_pool`).

## The command sequence (in Discord)

8. Register a pool and bind yourself as owner — the `--discord-user`
   you pass here becomes the whitelist owner and is the authorization
   for every owner-only verb. The maintainer is the gateway owner:
   the gateway identity on this gateway becomes the only maintainer.
   ```text
   /pool register demo-pool --name Demo --discord-user <your Discord user id>
   ```
9. Whitelist a second member (optional; single-agent run is fine) via
   env `AGENT_POOL_ALLOWED_USERS` or config `allowed_users` on the
   pool gateway, then restart/refresh so `sync_whitelist` inserts them
   into `pool_authorized`. There is no onboarding command. The second
   member's key binds on their first claim.
10. Add a repo (owner-only). `--path` is required; `--link` is
    rejected with `add-repo: --link is not supported; use /add-repo
    --path <checkout>`:
    ```text
    /add-repo demo --path /srv/repos/demo --pool demo-pool --name "Demo"
    ```
    Re-running `/add-repo` mints a new version. Members whitelisted
    after a version have no wrapped key for it and wait for the next
    version.
11. Create a ticket (owner-only):
    ```text
    /task add --pool-id demo-pool --repo demo --title "Live test" --brief "first live run"
    ```
    (Note the returned `ticket_id`.)
12. A non-whitelisted user must be refused on every verb — try with a
    third account before continuing (expect
    `not authorized for pool demo-pool — ask the maintainer`, never a
    dispatch).
13. Claim it (any whitelisted user may). Note the returned
    `worktree_path` (`<dest_root>/<repo_id>/worktree-<ticket_id>`),
    `branch` (`pool/<ticket_id>`), `base_ref`, `budget`, and
    `stop_condition`. The agent does the git work at that path/branch
    — the plugin does not run git:
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
17. Build + send the bundle on the contributor's own gateway — push
    is never completed from Discord. The agent calls
    `PoolService.push_build_bundle(repo_path, repo_id)` with its local
    checkout path, reads `manifest_json` from the result, and passes
    that + the transfer ticket + the grant token through Discord on
    the pool's gateway:
    ```text
    /push repo --deliver <transfer_ticket> --token <grant> --manifest '<contributor-signed manifest json>'
    ```
    Expect the sandbox-scan report; on a clean scan the task
    completes. Only the maintainer gateway may accept: a deliver
    attempted on any other gateway is refused with
    `push deliver refused: only the pool maintainer gateway may accept`.
18. Verify the audit:
    ```text
    /audit show <ticket_id>
    ```
19. Confirm the task is now completed (not available):
    ```text
    /fetch task list --pool demo-pool
    ```
    (The completed ticket must not appear as available.)
20. (Pull side, on the consumer gateway) Download with the manifest
    and a destination path via `/fetch repo --manifest <json>`; the
    agent runs the download and gets a `worktree-v<N>` checkout. A bad
    manifest signature is refused with
    `fetch refused: manifest signature does not verify`.

## Verify repo bytes never hit Discord

21. **Gateway payload size check.** The plugin's content guard caps
    coordination payloads at 4096 bytes, and oversize replies fail
    loudly with `reply exceeds 4096-byte gateway limit` rather than
    being truncated. Check the gateway log for this run: every Discord
    send from the plugin's replies must be a short manifest/notice —
    grep the gateway's Discord adapter send log for the ticket id and
    confirm no send body contains repo content.
22. **Iroh-side evidence.** The push reply (and the ledger
    `manifest_chain` row) carries the `transfer_ticket` /
    `content_hash` (BLAKE3). Fetch the accepted version on the consumer
    side and confirm the fetched bundle's BLAKE3 equals the manifest
    `content_hash` — that match is the proof the bytes travelled the
    Iroh path.

## Roll back

23. Disable and restart:
    ```bash
    hermes plugins disable agent-pool
    # restart the gateway
    rm ~/.hermes/plugins/agent-pool   # remove the symlink (optional)
    ```
    The pool ledger and keys stay under the profile's plugin-data
    dir; nothing else on the host changes.

## What this does NOT cover

- A real Discord button click (there are no buttons in 2.7.0 — the
  audit gate is automatic on push-deliver).
- Multi-machine pools: single-host run first; cross-machine uses the
  same flow with `relay_url` left at the default.
- `transport_mode=tor`: requires Tor installed and running on both
  endpoints; verify `tor_ready()` passes before attempting a tor-mode
  transfer.
- Freeze behaviour: while frozen, only the `pool_status` tool answers;
  every slash verb (including reads) is blocked with
  `pool frozen — contact the maintainer`.

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
   approach — the same layout Hermes uses for directory plugins:
   ```bash
   ln -s /home/maxcurrent/Documents/AntiGravityProjects/agent-pool \
         ~/.hermes/plugins/agent-pool
   ```
   (Note: `~/.hermes/plugins/hermes-iroh-interconnect` is a real
   directory on this host, not a symlink — the symlink is for
   agent-pool itself, whose package-relative imports resolve through the
   link.)
2. Enable it (this installs the declared Python deps pynacl + blake3 at
   enable time; expect a WARN that blake3 is missing from the Hermes
   runtime venv — tests and the plugin run under the system
   interpreter's deps, see README "Tests"):
   ```bash
   hermes plugins enable agent-pool
   ```
3. **Restart the gateway.** REQUIRED — both wirings run at Discord
   `connect()`:
   - slash-command tree sync: `H/plugins/platforms/discord/adapter.py:1556-1558`
     (`_register_slash_commands()` at connect),
   - plugin platform-handler wiring:
     `H/plugins/platforms/discord/adapter.py:1573`
     (`_wire_plugin_handlers`, `H/gateway/platforms/base.py:2211-2241`).
   New commands do not appear in the Discord picker and button clicks do
   not route until this restart. There is no hot-reload for either.

## Config

4. Set in the profile's plugin config (no `gateway_channel_id` exists
   anymore — gateway routing is Hermes's):
   - `relay_url`: `""` (default n0 relay) or `"off"` (direct/LAN)
   - `transport`: `real` (send_hermes/Iroh; `fake` is tests/CLI only)
   - budgets: `max_turns` 25, `max_tasks_per_day` 5, `token_cap` 50000,
     `wall_clock_seconds` 1800 (or per-pool overrides under `pools`)
   - confirm `send_hermes` is on PATH (`~/.cargo/bin/send_hermes` on
     this host) and `bubblewrap` is installed (without it the scanner
     runs unsandboxed and says so — see threat model).

## Discord-side setup

5. Pick a guild channel for the run. The bot's `DISCORD_ALLOWED_USERS`
   must contain the tester's Discord user id — that allowlist
   (`adapter.py:4159-4167`) is the only ingress gate; there is no
   plugin-side Discord client.
6. After the restart, verify all **ten** commands in the picker:
   `pool`, `task`, `add-repo`, `task_add`, `task_list`, `claim`, `release`,
   `work`, `integrate`, `audit` (each with one optional `args` string option
   from its `args_hint`).

## The command sequence (in Discord)

7. Register a pool and bind yourself as owner — **the `--discord-user`
   you pass here becomes the whitelist owner** and is the authorization
   for every owner-only verb; get it right (a mis-bound id is the residual
   auth risk). Register without `--discord-user` is refused:
   ```
   /pool register demo-pool --name Demo --discord-user <your Discord user id>
   ```
8. Invite a second member (or skip — single-agent run is fine). The
   invitee's Discord id becomes an **authorized** user (may claim/submit/
   list/fetch, but NOT mint, approve, invite, remove, freeze or
   integrate). The invite also records their agent key — attribution only:
   ```
   /pool invite demo-pool <member key_id> --box-pubkey <hex> --discord-user <their Discord user id>
   ```
9. Create a ticket (owner-only; an authorized non-owner is refused):
   ```
   /task add --pool-id demo-pool --repo demo --title "Live test" --brief "first live run"
   ```
   (Note the returned `ticket_id` and `thread_ref` — the invoking chat
   was recorded as the ticket's thread.)
10. A non-invitee (an id with no whitelist row) must be refused on every
    verb — try it with a third account before continuing (expect a
    permission refusal, never a dispatch).
11. Claim it (any authorized user may):
    ```
    /claim <ticket_id> --paths hello.txt
    ```
12. Submit work (bytes go over Iroh from this machine):
    ```
    /work submit <ticket_id> --repo-path <path to a git repo with >=1 commit> --version 1
    ```
    Expect a pending-manifest summary (manifest hash, BLAKE3
    `content_hash`, transfer id) — **not** repo bytes in the reply.
13. The invited friend must NOT be able to freeze the pool — have them
    try `/pool freeze demo-pool` and confirm the owner-only refusal
    (`pool frozen — contact the maintainer` never appears here; the
    refusal is an unauthorized-verb one).
14. Run the integration gate, then click **Approve** on the buttons in
    the reply (or run `/integrate approve <ticket_id>` — same code path,
    same owner check):
    ```
    /integrate <ticket_id>
    ```
15. Verify the audit:
    ```
    /audit show <ticket_id>
    ```
    Expect the sandbox-scan report and, after approve, the merged state.
16. Read the project back through fetch (the read path — no import):
    ```
    /pool fetch demo --pool demo-pool          (returns the latest accepted manifest)
    /work fetch --manifest '<manifest json>' --dest <workdir>
    ```
    Confirm the clone decrypts with the fetching agent's own box key and
    matches the manifest's BLAKE3 `content_hash`.

## Verify repo bytes never hit Discord

14. **Gateway payload size check.** The plugin's content guard caps
    coordination payloads at 4 KB (`P/replies.py:check_content_size`), and
    measured gateway payloads were ≤ ~1.6 KB with
    `gateway_calls_over_4KB=0` (`.hermes/reports/phase2-propagation.md:20`).
    Check the gateway log for this run: every Discord send from the
    plugin's replies must be a short manifest/notice (a few hundred
    bytes to ~1.6 KB) — grep the gateway's Discord adapter send log for
    the ticket id and confirm no send body contains repo content or
    anything near attachment size.
15. **Iroh-side evidence.** The `/work submit` reply (and the ledger
    `manifest_chain` row) carries the `transfer_ticket` / `content_hash`
    (BLAKE3). Fetch the accepted version on the consumer side and
    confirm the fetched bundle's BLAKE3 equals the manifest
    `content_hash` — that match is the proof the bytes travelled the
    Iroh path (`propagation.py:471` `fetch` verifies it). On this host
    the direct/LAN path measured ~97 MB/s and the n0 relay path ~2 MB/s
    (phase 6 report); either is fine, the BLAKE3 match is what matters.
    Also confirm the sender's `send_hermes` provider was online during
    the transfer (Phase 0 requirement).

## Roll back

16. Disable and restart:
    ```bash
    hermes plugins disable agent-pool
    # restart the gateway (same step 3)
    rm ~/.hermes/plugins/agent-pool   # remove the symlink (optional)
    ```
    The pool ledger and keys stay under the profile's plugin-data dir;
    nothing else on the host changes. The pre-migration tree remains
    available as the fallback (commit `ff0e63b`).

## What this does NOT cover

- A real Discord button click on a real guild (permissions, component
  rendering, persistent views across a gateway restart, ephemeral
  refusals) — the plugin-repo harness (`tests/test_discord_e2e.py`)
  covers the listener/dispatch/maintainer-gate logic with a simulated
  interaction; only this live run proves the real-client rendering and
  the 3 s interaction timing.
- Multi-machine pools: single-host run first; cross-machine uses the
  same flow with `relay_url` left at the default.

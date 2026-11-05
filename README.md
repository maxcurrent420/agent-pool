# Agent Pool — Hermes plugin

Friends on Discord form an agent team. Anyone submits a repo to a pool, the
maintainer (or their agent) audits and approves it, approved members' agents
claim and work tasks on it, and finished work is audited and merged back.
**Repo bytes never touch Discord** — they travel over Iroh (QUIC, BLAKE3-verified
bearer tickets via `send_hermes`); Discord/gateway carries only small signed
manifests and notices (< 4 KB).

## How it fits together

```
Discord / any gateway (coordination ONLY — signed manifests, commands, notices)
    │
    ▼
agent-pool plugin (Python, this repo)
  • pool ledger (SQLite via plugin_db)      bytes (Iroh QUIC, send_hermes)
  • manifest chain + crypto                 ◄─────────────────────► peers' plugins
  • ACL + approval workflow                           iroh_send_file / iroh_fetch_file
  • ticket store + budgets
  • worktrees + sandbox dispatch
    │ uses
    ▼
hermes-iroh-interconnect transport · bubblewrap (audit/worker sandbox)
```

- **Manifest chain**: append-only signed manifests, `prev_manifest_hash`
  linkage; the mutable "latest accepted" pointer is the maintainer's signature.
  Only maintainer-signed *accepted* versions are fetchable.
- **ACL**: per-pool member registry; each version gets a fresh random DEK,
  wrapped per current member's box pubkey (sealed box). Revocation = stop
  wrapping + DEK rotation per version (forward separation only — see
  [docs/threat-model.md](docs/threat-model.md)).
- **Approval gate**: the maintainer's agent sandbox-scans (bubblewrap,
  network off) every submission before acceptance and every diff before
  merge. Flagged scans never reach human review.
- **Tickets**: own ledger (Discord forum post = view). One-ticket-one-worker,
  declared file-path claims with overlap refusal, per-claim budgets
  (turns / tasks-per-day / token cap / wall clock) with hard kill.
- **Gateway abstraction**: the plugin talks only to `GatewayAdapter`
  (`post_manifest`, `post_notice`, `open_ticket_thread`, `set_ticket_state`,
  `post_buttons`). It never imports `discord` and never opens its own
  client — the Hermes gateway host supplies the transport. Telegram/Slack
  are later adapters over the same interface.

## Install

```bash
hermes plugins install <path-or-url-to-this-repo>   # or clone into ~/.hermes/plugins/
```

Requirements: Python deps `pynacl`, `blake3` (declared in `plugin.yaml`);
`git`; `bubblewrap` (audit/worker sandbox — without it the scanner says so
and runs unsandboxed, see threat model); `send_hermes` on PATH for the real
transport (`hermes-iroh-interconnect` builds it from source with cargo).

## Commands

| Command | What it does |
|---|---|
| `/pool register <pool_id> --name <name>` | Create a pool; caller is maintainer |
| `/pool invite <pool_id> <key_id> --role member\|maintainer --box-pubkey <hex>` | Add a member |
| `/pool members <pool_id>` | List members |
| `/pool remove <pool_id> <key_id>` | Revoke a member (forward separation) |
| `/pool freeze [--global] [--unfreeze]` | Per-pool freeze; **no pool id / `--global` = global kill-switch** across every pool |
| `/task add --pool-id … --repo … --title … --brief … [--max-turns …]` | Create a ticket (forum post) |
| `/task list|show` | List/show tickets |
| `/claim <ticket> --paths a.py,b.py [--max-turns …] [--override]` | Claim (declares paths; overlap refused) |
| `/release <ticket>` | Release a claim back to open |
| `/work start <ticket> --acceptance "cmd…" [--worker-command "cmd…"]` | Fetch accepted version over Iroh, worktree, spawn worker |
| `/work progress <ticket> <text>` | Milestone progress to the ticket thread only |
| `/work complete <ticket>` | Wait, enforce budget, run acceptance, submit branch bundle → `review` |
| `/work cancel <ticket>` | Hard-kill the worker (budget-kill path) |
| `/work submit <ticket> --repo-path PATH --version N` | Submit finished work (contributor-signed, pending) |
| `/integrate <ticket>` | Maintainer gate: fetch branch, sandbox diff scan, audit to thread, post approve/edit/discard buttons |
| `/integrate approve|edit|discard <ticket> [--edit-file PATH --edit-content TEXT] [--reason …]` | **The actionable decision** (see button note below) |
| `/audit show <ticket>` | Show the audit report for a ticket |
| `pool_status` (tool) | Relay URL, budgets, ledger/identity readiness |

### The approve/edit/discard buttons — read this

`post_buttons` posts real buttons in Discord (approve/edit/discard) with
values `approve:<ticket_id>` etc. In Hermes v0.22.7, the
`gateway_platform_event` hook normalizes only reactions, message edits/
deletes, and thread lifecycle events — **Discord button interactions are
not delivered to plugins** (the raw-events capability is tracked upstream
as #64228). So in this version the buttons are a visual prompt; the
actionable control is the slash command above, which runs the exact same
`PoolService.resolve_integration` with the same maintainer check. The
plugin registers a `gateway_platform_event` hook (`bridge.py`) that parses
button-shaped payloads defensively, resolves the clicker through a
host-supplied `identity_map`, verifies maintainer identity, and dispatches
— it is behaviour-neutral today and lights up when the host normalizes
interactions. Details: `bridge.py` module docstring.

### Freeze semantics

`/pool freeze` with no pool id (or `--global`) sets the **global
kill-switch**: every pool in the process is frozen (persisted in the
ledger `meta` table, so it survives restarts), and all pool-mutating and
claim/work/integrate actions raise. `/pool freeze <pool_id>` freezes only
that pool. `/pool freeze --unfreeze` (optionally with a pool id) clears
it. Read-only queries (`/task list|show`, `/pool members`, `/audit show`)
still work while frozen, and `/work cancel` still works (a kill path
must not be blockable by the switch).

## Config

| Key | Default | Meaning |
|---|---|---|
| `relay_url` | `""` | Iroh relay URL; `off` = direct/LAN (fast path on one LAN) |
| `max_turns` | 25 | Per-claim budget: max agent turns |
| `max_tasks_per_day` | 5 | Per-claim budget: max tasks/day |
| `token_cap` | 50000 | Per-claim budget: token cap |
| `wall_clock_seconds` | 1800 | Per-claim budget: wall clock |
| `pools` | `{}` | Per-pool settings (budgets, relay overrides) |
| `gateway_channel_id` | `""` | Discord channel for the gateway adapter (empty = in-memory offline adapter) |
| `transport` | `real` | `real` (send_hermes/Iroh) or `fake` (in-memory; tests/CLI) |
| `work_root` | `~/.hermes/plugin-data/agent-pool/work` | Worktrees + integration checkouts |

## Transport notes

- The sender's `send_hermes` provider must stay running until the fetch
  completes (a ticket is a bearer capability). Publish/fetch are
  plugin-driven windows; fetches retry with bounded exponential backoff
  (`transport.fetch_with_retry`, 3 attempts default) and then fail with a
  structured `TransportUnavailable`.
- Throughput on this host is ~2 MB/s (network ceiling, accepted).
  `HERMES_IROH_RELAY=off` is the direct/LAN fast path.
- Fallbacks (Radicle seed, git-over-I2P): [docs/fallback-transport.md](docs/fallback-transport.md).

## Tests

```bash
env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin \
  /usr/bin/python3.10 -m pytest tests -q
```

## Demo

`scripts/demo.sh` runs the full loop end-to-end (fake transport + fake
gateway for a fast, hermetic run; `--real` uses `send_hermes`) and prints
measured wall-clock time. `scripts/measure_300mb.py` does the real 300 MB
transfer measurement.

## Security

Read [docs/threat-model.md](docs/threat-model.md) before running this with
real repos: the relay cannot read content; malware that defeats both the
scan and the owner's review is out of scope; revocation is
forward-separation only.

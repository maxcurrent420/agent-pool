# Agent Pool

**Version 2.6.0** · MIT · community plugin for Hermes Agent (≥ 0.22.0).

A pool is a small team of agents sharing repos. Repos propagate as
BLAKE3-verified, end-to-end encrypted bundles over Iroh (with an optional
Tor anonymity layer); coordination (tasks, claims, freezes) travels only
through the Hermes gateway via Discord. The plugin never imports `discord`
and never posts to Discord itself — it returns content for Hermes to post.

## Command surface (10 commands + 1 tool)

Registered in `__init__.py:579-600` (`_COMMANDS`); `pool_status` is a
tool, not a slash command (`__init__.py:749-751`).

| Command | Args (verbatim) | Notes |
|---|---|---|
| `pool` | `register\|invite\|members\|remove\|freeze\|list [options]` | bare `/pool` → status JSON |
| `fetch` | `repo list \| task list [--ticket <id>] \| repo <repo_id> \| repo --manifest <json> [options]` | read surface on the pool gateway |
| `task` | `add\|list\|show [options]` | tickets mint `open` always |
| `add-repo` | `<repo_id> --path <abs/path> [--pool <pool_id>] [--name <display name>]` | register + submit as maintainer |
| `task_add` | `--repo <repo_id> --title "..." [--brief "..."] [--pool <pool_id>]` | flat alias of `/task add` |
| `task_list` | `[--repo <repo_id>] [--pool <pool_id>] [--state open]` | flat alias of `/task list` |
| `claim` | `[ticket] [--paths <paths>] [--any-project --max-tasks N] [options]` | whole-ticket (`--paths` omitted) or bulk |
| `release` | `[ticket]` | claimant-only |
| `push` | `repo <repo_id> --ticket <id> \| repo --deliver <transfer_ticket> --token <grant> --manifest <json>` | grant, then deliver (audit gate) |
| `audit` | `show [ticket]` | show the audit report |

Removed surface (no longer exists): `/pool fetch`, `/work *`, `/integrate`,
`/task approve\|deny\|modify`, `/pool auto-approve`, `--link`, proposal
packets. A bare `/pool import\|trust\|peer\|confirm` replies with
`import/trust/peers removed: use /fetch repo list / /fetch task list /
/fetch repo (reads are fetch commands on the pool gateway)`
(`__init__.py:328-329`).

## Task lifecycle

`open → claimed → open (release) | completed (accepted push)`. `completed`
is terminal. `TICKET_STATES = ("open","claimed","completed")`
(`ledger.py:333`); `_TRANSITIONS` enforced in `set_ticket_state`
(`ledger.py:335-339,564-577`). `add_ticket` always mints `open`
(`tickets.py:503-509`). There is no `pending`/`review`/`merged`/`blocked`
state.

Budgets are **recorded per claim, not metered**: `normalize_budget`
validates positive ints (`ledger.py:421-430`); there is no runtime
metering/kill.
`BUDGET_DEFAULTS = {"max_turns":25, "max_tasks_per_day":5, "token_cap":50000,
"wall_clock_seconds":1800}` (`ledger.py:341-342`).

## One pool per gateway

A gateway hosts and manages exactly one pool. `create_pool` refuses a
second: `this gateway already hosts pool <id> — one pool per gateway`
(`ledger.py:264-268`). Management verbs never need `--pool`; an explicit
`--pool` naming anything but THE hosted pool is refused
(`PoolNotLocalError`, `tickets.py:112-143`). A pre-existing multi-pool DB
is refused for management verbs until the operator removes the extras
(`tickets.py:128-132,140-143`); rows are never auto-deleted.

Agents *interact* with multiple pools (fetch lists, claim, release, push)
by commands the platform routes to those pools' own gateways, recording
each in the `pool_gateways` table (`ledger.py:144-156`; schema:
`pool_id TEXT PRIMARY KEY, gateway_endpoint, transport_addr, role,
added_at, last_seen`).

## Passive plumbing

The plugin answers fetch/claim/release/push commands on its own gateway;
the platform routes commands across gateways; agents pull over the
configured transport and push over it. No timers, no observation, no
execution, no Discord posting. `register()` wires only the `pool_status`
tool and the `_COMMANDS` handlers (`__init__.py:749-758`).

## Transport privacy

Substrate: Iroh (endpoint, relay, transfer tickets, end-to-end
encryption) — unchanged. Anonymity layer: iroh's Tor custom transport
via our sidecar, opt-in per pool.

- `transport_mode: relay` (default) — transfers are relay-only on BOTH
  sender and receiver; the peer learns no IP; the relay sees both peers'
  IPs + metadata, never plaintext. Explicit fallback.
- `transport_mode: tor` — Iroh Tor custom transport over hidden services;
  neither peer learns the other's IP; no relay in the connection path.
  Both endpoints must enable Tor; `tor_ready()` fails closed (names
  `systemctl start agent-pool-tor`) and never silently downgrades to relay.
- `direct` mode does not exist and cannot be constructed
  (`transport.py:161-163`).

Residuals (stated honestly):
1. Timing/volume correlation (Tor is not a mix network).
2. That both sides use Tor (entry guards know the client uses Tor).
3. Rendezvous-level facts (intro points learn *some* client connected
   at time T, not the IP).
4. Application-layer identity (the pool operator knows which invited agent
   downloaded/pushed — IP anonymity is not identity anonymity).
5. The pool's pseudonym (stable hidden-service identity links activity
   over time).
6. Discord visibility (endpoint/ticket metadata travels in command
   replies; bytes never do).
7. `relay` fallback mode still routes through an Iroh relay that sees
   both endpoints' IPs + metadata (never plaintext).

ControlPort is cookie-auth-disabled and bound to `127.0.0.1` only
(`ops/torrc`).

## Isolation + security

- Whitelisted-Discord-id authorization: owner + invited (`pool_authorized`
  table, `ledger.py:138-142`). Keys are crypto-only, never decide access.
- Owner-only freeze (global + per-pool), `freeze_global` iterates all
  hosted pools (`tickets.py:476-487`).
- Hosted-pool-only resolution (`_resolve_pool`).
- Per-pool ledger (single SQLite DB per profile, but no cross-ledger
  writes).
- Per-version DEK wrap per member; BLAKE3 content hash; manifest
  signature; sandbox audit (bwrap network-off with unsandboxed fallback).
- Audit-on-push: `push_deliver` fetches, decrypts with maintainer's wrapped
  DEK, BLAKE3-verifies, sandbox-audits; clean ⇒ `accept` (DEK rotation +
  chain append) + consume grant + complete task; flagged ⇒ task stays
  claimed, `push_flagged` event, grant NOT consumed
  (`tickets.py:912-989`). This synchronous audit is the only gate — no
  queue, no human approval.
- DEK ACL: only invited members get wrapped DEKs; removed members can no
  longer unwrap new versions.
- No pending state, no proposal packets, no approval queue.

## Configuration (`plugin.yaml`)

| Key | Default | Notes |
|---|---|---|
| `transport_mode` | `relay` | `relay` or `tor` (see Transport privacy) |
| `relay_url` | `""` | Iroh relay URL; empty = default n0 relay; `off` rejected |
| `tor_control_port` | `9051` | 127.0.0.1 only |
| `tor_socks_port` | `9055` | operator tooling |
| `tor_binary` | `""` | option (B) sidecar path; empty = tor-enabled send_hermes |
| `max_turns` | `25` | per-claim budget (recorded, not metered) |
| `max_tasks_per_day` | `5` | per-claim budget (recorded, not metered) |
| `token_cap` | `50000` | per-claim budget (recorded, not metered) |
| `wall_clock_seconds` | `1800` | per-claim budget (recorded, not metered) |
| `pools` | `{}` | per-pool overrides (budgets, relay) |
| `transport` | `real` | `real` (send_hermes/Iroh) or `fake` (in-memory, tests/CLI) |

The code default for `transport` is `real`, matching `plugin.yaml`: an
unset key runs the real transport. Set `transport: fake` only for
tests/CLI.

Env vars: `HERMES_IROH_RELAY`, `HERMES_IROH_STATE_DIR`,
`HERMES_IROH_SEND_TIMEOUT` (90s), `HERMES_IROH_FETCH_TIMEOUT` (600s),
`AGENT_POOL_DATA_DIR`.

## Install

1. `pip` deps: `pynacl>=1.5,<2`, `blake3>=0.4,<2`.
2. `send_hermes` binary on PATH (hermes-iroh-interconnect sendme fork).
3. Tor (optional, for `transport_mode=tor`):
   `apt install tor` + `scripts/install-tor.sh` (writes `ops/torrc`, builds
   the tor-enabled sidecar, enables `agent-pool-tor.service`). Both
   endpoints in a pool must enable Tor; fail closed until then.
4. Symlink the plugin into `~/.hermes/plugins/agent-pool`.

## Tests / Demo

- `env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin \
  /usr/bin/python3.10 -m pytest tests -q` — 202 tests passed at 2.5.0
  baseline; Phase 6 re-gates the suite for the corrected model.
- `scripts/demo.py` — end-to-end demo of the current model: add repo → add
  task → claim → push (grant + build + deliver) → task completed → verify
  chain. `--real` uses `RealSendHermesTransport` (send_hermes on PATH).
- `scripts/live-discord-check.md` — operator runbook for a live Discord
  guild.

## Breaking surface (2.5.0 → 2.6.0)

- `pending`/approval queue removed; `/integrate` removed (push-deliver is
  the gate); `/work` namespace removed; `/pool fetch` + `/work fetch`
  removed (replaced by `/fetch repo list` / `/fetch task list` /
  `/fetch repo`); multi-pool hosting removed (one pool per gateway);
  `/add-repo --link` removed (push grants); `transport_mode` gains a real
  `tor`.
- Ticket-state migration (in `init_ledger`, idempotent, meta-guarded):
  `pending→open`, `review→claimed`, `blocked→open`, `merged→completed`.
  Existing DBs keep their data; no destructive migration.
- Multi-pool DBs: refused for management verbs until the operator removes
  the extras (rows never auto-deleted).
- Accepted versions pre-2.6.0 remain fetchable (Iroh tickets unchanged).
- Tor must be installed and running on EVERY gateway in a pool before
  `transport_mode=tor` transfers work; `relay` mode needs none of this and
  stays the out-of-the-box mode.

## See also

- `docs/architecture.md` — command-surface chain, Tor-routed Iroh flow,
  one-pool model + `pool_gateways`.
- `docs/threat-model.md` — anonymity/residual-risk, guarantees.
- `docs/fallback-transport.md` — relay fallback, Radicle seed, Tor ceilings.
- `scripts/live-discord-check.md` — operator runbook.
- `.hermes/plans/2026-11-06_0156-agent-pool-task-repo-model-corrections.md`
  — the plan this phase implements.

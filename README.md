# Agent Pool

**Version 2.7.0** · MIT · community plugin for Hermes Agent (≥ 0.22.0).

A pool is a small team of agents sharing repos. Repos propagate as
BLAKE3-verified, end-to-end encrypted bundles over Iroh over Tor
(Tor is mandatory — Iroh-over-Tor is the only byte transport); coordination (tasks, claims, freezes) travels only
through the Hermes gateway via Discord. The plugin never imports `discord`
and never posts to Discord itself — it returns content for Hermes to post.

The plugin is a passive add-on. It tells the agent how to push and pull
and exposes controls to maintain the pool; the agent runs the work. On a
claim the plugin hands the agent a per-ticket worktree path, branch,
base ref, budget, and stop condition (see Agent worktree / branch / budget
below). The agent performs the git operations at that path and branch;
the plugin does not run git for the agent.

## Command surface (10 commands + 2 tools)

Registered in `__init__.py` (`_COMMANDS`); `pool_status` and
`pool_report_usage` are tools, not slash commands.

| Command | Args (verbatim) | Notes |
|---|---|---|
| `pool` | `register\|members\|remove\|freeze\|list [options]` | bare `/pool` → status JSON |
| `fetch` | `repo list \| task list [--ticket <id>] \| repo <repo_id> \| repo --manifest <json> [options]` | read surface on the pool gateway |
| `task` | `add\|list\|show [options]` | tickets mint `open` always |
| `add-repo` | `<repo_id> --path <abs/path> [--pool <pool_id>] [--name <display name>]` | register + submit as maintainer |
| `task_add` | `--repo <repo_id> --title "..." [--brief "..."] [--pool <pool_id>]` | flat alias of `/task add` |
| `task_list` | `[--repo <repo_id>] [--pool <pool_id>] [--state open]` | flat alias of `/task list` |
| `claim` | `[ticket] [--paths <paths>] [--any-project --max-tasks N] [options]` | whole-ticket (`--paths` omitted) or bulk |
| `release` | `[ticket]` | claimant-only |
| `push` | `repo <repo_id> --ticket <id> \| repo --deliver <transfer_ticket> --token <grant> --manifest <json>` | grant, then deliver (audit gate) |
| `audit` | `show [ticket]` | show the audit report |

Agent-facing tools (toolset `agent_pool`):

- `pool_status` — reports plugin status, including `"version": "2.7.0"`
  (it tracks the `plugin.yaml` version), transport mode, budgets,
  ledger/identity readiness, and queued notices.
- `pool_report_usage` — records agent-reported usage on a claim
  (`claim_id`, plus optional `turns`, `tokens`, `seconds`). The plugin
  records what the agent reports; it does not meter turns itself.

There is no `/complete` command — completion happens only via a clean
push-deliver.

## Membership and authorization — whitelist, not a command

There is no separate onboarding command. Membership is a
whitelist of Discord user ids:

- Source: env `AGENT_POOL_ALLOWED_USERS` (comma-separated Discord user
  ids) or config `allowed_users` (a list). Env wins over config when
  both are set.
- The pool owner (from `maintainer_discord_user_id` in config/env) is always
  whitelisted.
- `ledger.sync_whitelist` syncs the whitelist into `pool_authorized`:
  it inserts missing users and never deletes existing rows. Removal is
  a separate act (`/pool remove`), not a sync side effect.

Key attribution is first-claim binding in `pool_memberships`: on a
user's first claim, the key they present is bound to that Discord user
on that pool. Presenting a different key under a bound user is refused
with:

```text
claim refused: user {user} already bound to key {bound} on pool {pool} — presented key {presented} differs; first-claim binding is permanent
```

`/pool remove` revokes the removed member's whitelisted Discord id
(resolved via `pool_memberships`). Revocation is forward separation
only for already-fetched bytes: future versions omit the removed
member's wrapped DEK, but bytes already fetched and decrypted cannot
be recalled.

Absence of a record is refusal on every verb — there is no legacy skip.
The exact refusal is:

```text
not authorized for pool {pool_id} — ask the maintainer
```

Keys are crypto-only (sign manifests, wrap DEKs); a key never decides
who may act.

## Maintainer == gateway owner

The user who runs the gateway owns the pool on that gateway. No other
user can maintain that pool; there is no second owner and no second
pool on one gateway.

- Registration refuses a maintainer that is not the gateway identity:
  `register refused: maintainer must be the gateway owner`
- Owner verbs require both the Discord owner match and the same
  gateway (`pools.owner_gateway_key_id`).
- Gateway identity rotation is refused, never auto-updated:
  `gateway identity changed: caller is not the pool's owner gateway — re-register the pool to continue`
- `push_deliver` requires the owner gateway:
  `push deliver refused: only the pool maintainer gateway may accept`
- Cross-gateway interaction (fetch/claim/release/complete against a
  remote pool) is unaffected by the gateway-owner check.

## One pool per gateway

A gateway hosts and manages exactly one pool. `create_pool` refuses a
second pool. Management verbs never need `--pool`; an explicit
`--pool` naming anything but the hosted pool is refused. A
pre-existing multi-pool DB is refused for management verbs until the
operator removes the extras; rows are never auto-deleted.

Agents *interact* with multiple pools (fetch lists, claim, release,
push) by telling the agent which Discord bot/gateway to address; that
gateway's plugin answers from its own ledger. `pool_gateways` is a
local notebook of foreign pools refused, not a routing table. Each
interaction is recorded in the `pool_gateways` table.

## Task lifecycle

`open → claimed → open (release) | completed (accepted push)`.
`completed` is terminal. Tickets mint `open` always. There is no
`pending`/`review`/`merged`/`blocked` state.

## Agent worktree / branch / budget

On claim, the plugin hands the agent:

- `worktree_path`: `<dest_root>/<repo_id>/worktree-<ticket_id>`, where
  dest_root defaults to `/tmp/agent-pool-worktrees` and is overridable
  via env `AGENT_POOL_WORKTREE_ROOT`.
- `branch`: `pool/<ticket_id>`
- `base_ref`: `v{accepted_version}`
- `budget`: the normalized budget dict.
- `stop_condition`: `{"condition": "budget_exhausted", "enforce": true}`

The agent performs the git operations (`git worktree add`, commit,
push) at that path and branch. On release/complete the plugin emits
the cleanup notice `release worktree {worktree_path} and delete
branch {branch}`; the agent cleans up.

## Budgets belong to agents, not pools

An agent is allocated a maximum number of tasks it may complete on the
pool. There are no pool-level budget keys; the `pools` config holds
`pools` transport settings are ignored (see Transport privacy).

Allocation precedence: env `AGENT_POOL_TASK_ALLOCATIONS`
(comma-separated `key_id=N`) > config `agent_task_allocations`
(mapping key_id to int) > config `max_tasks_per_day` > 5.
`max_tasks_per_day` is kept as the default per-agent allocation.

When an agent's completed-task count reaches its allocation, the
plugin refuses further work:

- claim: `agent budget exhausted: {completed} of {allocation} tasks completed on pool {pool_id} — no further claims allowed`
- push: `agent budget exhausted: {completed} of {allocation} tasks completed on pool {pool_id} — push refused`

Turns/tokens/wall-clock are secondary and optional. The agent may
report usage through the `pool_report_usage` tool; the plugin records
and checks only what is reported, and refuses with
`budget exhausted: <field> (<used>/<limit>) — release or push now`.
The plugin does not meter turns itself. There is no auto-release on
exhaustion — the claimant must `/release` or push.

## Push/pull — Discord carries paths

Push and pull are never completed from Discord. Discord carries
paths; the agent on the gateway does the pull/push.

- The agent calls `PoolService.push_build_bundle(repo_path, repo_id)`
  with its local checkout path. The plugin validates the path
  (`push_build_bundle: repo_path is not an existing directory: <path>`),
  returns `manifest_json`, and the agent passes that plus the transfer
  ticket and the grant token through Discord to
  `/push repo --deliver`.
- On the pull side, the agent runs the download with the manifest and
  a destination path and gets a `worktree-v<N>` checkout. The
  cross-gateway `/fetch repo --manifest` download verifies the
  manifest signature, the wrapped-key ACL, and the BLAKE3 content
  hash, and refuses with `fetch refused: manifest signature does not verify`.
- Full chain verification happens on the hosted-pool `fetch()`. There
  is no local-pool branch in the download path.
- Worktrees use Iroh for push/pull: claim hands the agent a worktree
  path, branch, and base ref but no bytes (claim does not deliver
  bytes); the pull chain is the separate fetch-manifest then download
  steps, and push uses the same Iroh transport as fetch.
- On a clean push accept, `push_deliver` returns an ephemeral
  (gateway-only) `agent_prompt` payload carrying the accepted
  manifest's `transfer_ticket` (the Iroh download link), `manifest_json`,
  version, and a prompt asking the agent to notify the user that tasks
  are complete and to merge after approval. The plugin never posts to
  Discord and never merges; merge requires explicit approval recorded
  by `merge_approved` (gateway-owner only). In the mixed case where
  the contributor is not the maintainer, the maintainer must re-run
  `/fetch repo` to obtain the accepted manifest.

## Freeze scope

While frozen, the `pool_status` tool still answers; every other slash
verb is blocked with `pool frozen — contact the maintainer`. This
includes read verbs — read-only queries do not survive a freeze.

Two honest caveats: the wrapper guard is best-effort (an exception
inside the guard falls through to the handler), and verbs run before
any pool exists (e.g. register) or naming a foreign pool skip the
freeze check; there is nothing to resolve.

## Passive plumbing

The plugin answers fetch/claim/release/push commands on its own
gateway. The user tells the agent which Discord bot/gateway to address;
that gateway's plugin answers from its own ledger. `pool_gateways` is a
local notebook of foreign pools refused, not a routing table. Agents
pull and push over the configured transport. No timers, no
observation, no execution, no Discord posting.

## Transport privacy

Substrate: Iroh (endpoint, transfer tickets, end-to-end
encryption) over Tor — Iroh-over-Tor is the only byte transport. There
is no relay fallback and no relay option; a fork must edit source to
change the transport (see `docs/fallback-transport.md`).

- `transport_mode: tor` (default, only valid value) — Iroh Tor custom
  transport over hidden services; neither peer learns the other's IP;
  no relay in the connection path. Both endpoints must enable Tor;
  `tor_ready()` is a hard gate at service construction, plugin
  registration, and pool registration, and fails closed with no
  fallback.
- `relay` and `direct` modes do not exist and cannot be constructed;
  selecting them is rejected. A legacy top-level `transport_mode=relay`
  in plugin config is ignored (treated as tor) with a migration warning
  in `pool_status`; per-pool transport settings are ignored.

Residuals (stated honestly):

1. Timing/volume correlation (Tor is not a mix network).
2. That both sides use Tor (entry guards know the client uses Tor).
3. Rendezvous-level facts (intro points learn *some* client connected
   at time T, not the IP).
4. Application-layer identity (the pool operator knows which
   whitelisted agent downloaded/pushed — IP anonymity is not identity
   anonymity).
5. The pool's pseudonym (stable hidden-service identity links activity
   over time).
6. Discord visibility (endpoint/ticket metadata travels in command
   replies; bytes never do).
7. (Removed) The former relay fallback mode no longer exists; there
   is no relay in the connection path to see endpoint IPs.

ControlPort is cookie-auth-disabled and bound to `127.0.0.1` only
(`ops/torrc`).

## Isolation + security

- Whitelisted-Discord-id authorization: owner plus `pool_authorized`
  rows. Keys are crypto-only, never decide access.
- Owner-only freeze (global + per-pool).
- Hosted-pool-only resolution.
- Per-pool ledger (single SQLite DB per profile via `get_db` in
  `ledger.py`, which uses Hermes `plugin_db("agent-pool")` and falls
  back to a local SQLite file outside Hermes; no cross-ledger writes).
- Per-version DEK wrap per current member; BLAKE3 content hash;
  manifest signature checked on the download path and full chain
  verification on the hosted-pool fetch; sandbox audit (bubblewrap
  network-off with unsandboxed fallback, reported as such).
- Audit-on-push: `push_deliver` fetches, decrypts with the maintainer
  gateway's wrapped DEK, BLAKE3-verifies, sandbox-audits; clean ⇒
  `accept` (DEK rotation + chain append, signed by the maintainer
  gateway) + consume grant + complete task; flagged ⇒ task stays
  claimed, `push_flagged` event, grant NOT consumed. This synchronous
  audit is the only gate — no queue, no human approval.
- DEK ACL: only current members get wrapped DEKs; removed members can
  no longer unwrap new versions.
- Replies over 4096 bytes fail loudly
  (`reply exceeds 4096-byte gateway limit`); they are not silently
  truncated.
- `--link` is rejected: `add-repo: --link is not supported; use
  /add-repo --path <checkout>`. Re-running `/add-repo` mints a new
  version; members whitelisted after a version have no wrapped key for
  it and wait for the next version.
- No pending state, no proposal packets, no approval queue. The old
  chat-binding surface is gone; any old table left on disk is unread.

## Configuration (`plugin.yaml`)

| Key | Default | Notes |
|---|---|---|
| `transport_mode` | `tor` | `tor` only (see Transport privacy) |
| `relay_url` | `""` | Deprecated and ignored (no relay transport) |
| `tor_control_port` | `9051` | 127.0.0.1 only |
| `tor_socks_port` | `9055` | operator tooling |
| `tor_binary` | `""` | optional sidecar path; empty = tor-enabled send_hermes |
| `allowed_users` | `[]` | Discord user ids allowed on the hosted pool; env wins; owner always added |
| `agent_task_allocations` | `{}` | per-agent completion allocation (key_id to int); env wins |
| `max_tasks_per_day` | `5` | default per-agent allocation |
| `max_turns` | `25` | secondary per-claim limit, checked only from agent-reported usage |
| `token_cap` | `50000` | secondary per-claim limit, checked only from agent-reported usage |
| `wall_clock_seconds` | `1800` | secondary per-claim limit, checked only from agent-reported usage |
| `pools` | `{}` | Deprecated and ignored (tor-only for every pool) |
| `transport` | `real` | `real` (send_hermes/Iroh) or `fake` (in-memory, tests/CLI) |

The code default for `transport` is `real`, matching `plugin.yaml`: an
unset key runs the real transport. Set `transport: fake` only for
tests/CLI.

Env vars: `HERMES_IROH_STATE_DIR`,
`HERMES_IROH_SEND_TIMEOUT`, `HERMES_IROH_FETCH_TIMEOUT`,
`AGENT_POOL_DATA_DIR`, `AGENT_POOL_ALLOWED_USERS`,
`AGENT_POOL_TASK_ALLOCATIONS`, `AGENT_POOL_WORKTREE_ROOT`.

## Install

1. `pip` deps: `pynacl>=1.5,<2`, `blake3>=0.4,<2`.
2. `send_hermes` binary on PATH (hermes-iroh-interconnect sendme fork).
3. Tor (mandatory — Iroh-over-Tor is the only byte transport):
   `apt install tor` + `scripts/install-tor.sh` (writes `ops/torrc`,
   builds the tor-enabled sidecar, enables the tor service). Both
   endpoints in a pool must enable Tor; the plugin fails closed at
   service construction (lazy, first use) and pool registration
   until then — plugin registration itself is pure wiring and loads
   even with Tor down. No relay fallback exists.
4. Symlink the plugin into `~/.hermes/plugins/agent-pool`.

## Tests / Demo

- `env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin /usr/bin/python3.10 -m pytest tests -q`
- `scripts/demo.py` — end-to-end demo of the current model: add repo →
  add task → claim → push (grant + build + deliver) → task completed →
  verify chain. `--real` uses the real transport (send_hermes on PATH).
- `scripts/live-discord-check.md` — operator runbook for a live Discord
  guild.

## See also

- `docs/architecture.md` — command-surface chain, Tor-routed Iroh
  flow, one-pool model + `pool_gateways`.
- `docs/threat-model.md` — anonymity/residual-risk, guarantees.
- `docs/fallback-transport.md` — why there is no relay fallback
  (fork-only escape hatch), Radicle seed, Tor ceilings.
- `scripts/live-discord-check.md` — operator runbook.

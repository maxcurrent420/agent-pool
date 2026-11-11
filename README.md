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

## Why this exists

Your Hermes agent can already work on a GitHub repo, and your friends'
agents can work on the same one. But many people do not want to **depend
on** a third party like GitHub. The reasons range from platform risk
(accounts, repos, or whole platforms disappearing on someone else's
decision) to who owns the platform — Microsoft owns GitHub, and your code
sitting there is a dataset someone else may train an AI on.

This plugin removes that dependency. Coordination travels through a
messaging gateway you control (Discord via Hermes); code travels as
encrypted bundles over Iroh over Tor. No forge, no cloud, and no peer
learns the other's IP address. A pool of agents can review, claim, push,
approve, and merge repos end to end without any repo ever being uploaded
to a third party.

That does not lock you out of GitHub. You can still message your Hermes
gateway and say "push this repo to GitHub" whenever you want a public
mirror. The point is that others can contribute **off** GitHub — to closed
or partly closed source projects — without a "Microsoft in the Middle"
between you and your collaborators, and without exposing your network
location to anyone.

## Pooled capacity — and what's planned

If you run on free models, your inference capacity is your scarcest
resource: rate limits, quotas, and time. A pool lets friends **pool that
free capacity** — you can outsource work to your friends' agents, work on
their projects in return, and so whenever one of you is on a break
(everyone sleeps eventually), everyone else keeps going longer on the same
budget. The pool is a way to turn "my agent is idle until Monday" into
"my agent's idle hours are still productive for the team".

Two planned pieces build on this and are **not implemented yet**:

1. **Better metering of the capacity you dedicate to the pool** — today
   the plugin meters tasks and agent-reported usage; the goal is to meter
   exactly how much of your inference budget you contribute and consume
   against it.
2. **Charging for it via shielded ZEC** — so contributing capacity can be
   paid for privately, paving the way for users to earn from an agent job
   board and get far more done without spending their time managing
   agents.

Neither is a promise about the catalog listing; both are roadmap items
tracked in this repository.

## Command surface (10 commands + 2 tools)

Registered in `__init__.py` (`_COMMANDS`); `pool_status` and
`pool_report_usage` are tools, not slash commands.

| Command | Args (verbatim) | Notes |
|---|---|---|
| `pool` | `register\|members\|remove\|freeze\|list [options]` | bare `/pool` → status JSON |
| `fetch` | `repo list \| task list [--ticket <id>] \| repo <repo_id> \| repo --manifest <json> [options]` | read surface on the pool gateway |
| `task` | `add\|list\|show [options]` | tickets mint `open` always |
| `add-repo` | `<repo_id> --path <abs/path> [options] \| --deliver <transfer_ticket> --manifest <json> [--repo <repo_id>] \| approve <repo_id> [--version N] \| submissions [--repo <repo_id>]` | owner fast path, or link flow + approval queue (see Add-repo below) |
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
- `add_repo_deliver` requires the owner gateway:
  `add-repo deliver refused: only the pool maintainer gateway may accept`
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
- On a clean push deliver, `push_deliver` downloads the bytes during the
  audit but does NOT accept or complete anything: it records the version
  as `submitted` (awaiting owner approval) and returns an ephemeral
  (gateway-only) `agent_prompt` payload instructing the agent to notify
  the operator that the change is awaiting approval (quoting the audit
  verdict — including the ClamAV verdict — and the audit id), ask for
  the decision, and act on it. On approval the agent invokes the
  agent-side accept path (`scripts/pool-approve.py --ticket <id>`,
  service `push_approve`): the version is accepted (DEK rotation + chain
  append), the grant is consumed, and the ticket completes. On denial
  the agent runs `scripts/pool-approve.py --ticket <id> --deny` (service
  `push_deny`) to record the denial; the ticket stays claimed. There is
  no approval slash command by design — approval is agent-driven and
  service-level. The full manifest is NOT part of the deliver reply
  (replies must stay under the 4096-byte gateway limit). The plugin
  never posts to Discord and never merges; merging into the canonical
  checkout is the agent's own git work after approval (the approval
  itself is recorded by `merge_approved`, gateway-owner only).
- What awaits approval is always discoverable: `pool_status` carries an
  `awaiting_approval` list (submitted versions from both ingestion
  paths, each with its audit verdict and, for pushes, the claimed
  ticket), and `/add-repo submissions` lists the add-repo queue — so
  the agent can tell the operator what needs a decision without any
  approval command.

## Add-repo — link flow + owner approval

`/add-repo` has two ways to get repo bytes onto the pool, and both
move bytes only over Iroh-over-Tor (Discord carries the manifest JSON
and the transfer ticket, never repo bytes):

- **Owner fast path** — `/add-repo <repo_id> --path <abs/path>` is the
  maintainer's own single-command workflow: it registers the project
  and self-accepts the next version (owner-only).
- **Link flow** — a contributor submits a repo over the link:
  1. On the contributor's gateway, the agent runs
     `scripts/contributor-add.py /path/to/checkout --repo <repo_id>`,
     which calls `PoolService.add_repo_build_bundle` (which reuses
     `push_build_bundle` — no duplicated pack/encrypt/send logic),
     packs, hashes, encrypts, and sends the bundle over
     Iroh-over-Tor and prints the contributor-signed manifest plus a
     ready-to-paste deliver command.
  2. The contributor's agent pastes that command to the maintainer
     bot: `/add-repo --deliver <transfer_ticket> --manifest <json>
     --repo <repo_id>` (service: `add_repo_deliver`). The maintainer gateway fetches the bytes over
     Iroh-over-Tor, verifies the contributor signature, pool
     membership (ACL), and BLAKE3, runs the sandbox audit, and
     records the version as `submitted` — awaiting owner approval.
     It is NOT accepted here, and no `manifest_chain` row is written.
     A flagged audit keeps the submitted row as evidence and records
     a `repo_submission_refused` event; a later re-submission mints a
     fresh version. Refusals:
     `add-repo deliver refused: only the pool maintainer gateway may accept`.
  3. The owner promotes the version with
     `/add-repo approve <repo_id> [--version N]` (owner-only; service:
     `add_repo_approve`): DEK
     rotation, the maintainer gateway signs the accepted manifest,
     and the chain append happens (the stale submitted row is
     dropped first). Denial is recorded with
     `scripts/pool-approve.py --repo <repo_id> --deny` (service
     `add_repo_deny`).
  4. `/add-repo submissions [--repo <repo_id>]` (service:
     `add_repo_submissions`) lists the submitted
     (awaiting approval) queue; `pool_status` surfaces the same queue
     across both ingestion paths as `awaiting_approval`.
  5. The deliver reply carries an ephemeral `agent_prompt`: the agent
     notifies the operator (quoted verdict + audit id), asks for the
     decision, and on approval invokes the accept path above, then
     merges the accepted version into the canonical checkout itself
     with git.

## Workflow C — claim from the contributor gateway

The claim originates on the contributor's gateway (Bot #2) and lands on
the maintainer gateway (Bot #1) — the contributor's agent sends the
claim text where Bot #1 can see it, exactly like messaging any other
bot:

1. **Enroll the contributor's gateway key as a real member.** On the
   contributor's gateway, `/pool members --mykeys` prints the gateway's
   public key id + box/verify public keys (public material, safe over
   Discord). The maintainer gateway enrolls them with
   `/pool members --enroll <key_id> <box_pubkey_hex>
   <verify_pubkey_hex>` (owner-only). Existing accepted versions are
   re-wrapped so the new member holds a DEK entry for them. No local
   data-dir workaround is needed.
2. **Claim + pull in one flow** — `scripts/contributor-claim.py
   TICKET_ID` prints the exact text (`/claim TICKET_ID`) for the
   contributor's agent to send to Bot #1. Bot #1 validates and replies
   with the accepted repo's Tor download link (`transfer_ticket`) and
   the accepted manifest directly — no local ledger lookup on the
   contributor's machine. (If the manifest exceeds the 4096-byte reply
   guard, the reply carries a preview + `manifest_hash` plus the
   `/fetch repo <id>` instruction instead.)
3. The contributor's agent saves Bot #1's reply and runs
   `scripts/contributor-claim.py TICKET_ID --reply-file REPLY.json
   --dest DIR`: the script extracts the link + manifest, downloads the
   bytes over Iroh-over-Tor, unwraps the DEK, verifies BLAKE3, and
   git-clones the bundle into `DIR`.

`/fetch repo list` and `/fetch task list` stay maintainer-gateway
verbs; their answers reach Bot #2 through the maintainer's replies
(the agent relays them).

`--link` stays rejected: `add-repo: --link is not supported; use
/add-repo --path <checkout>`. On-disk state is `submitted`; user-facing
copy says "awaiting approval".

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
- No pending ticket state and no proposal packets. Repo submissions
  awaiting owner approval are `submitted` repo_versions rows listed by
  `/add-repo submissions` (not a ticket state). The old chat-binding
  surface is gone; any old table left on disk is unread.

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

Dependency split (do not mix the two layers):

- **Python deps** (`pynacl>=1.5,<2`, `blake3>=0.4,<2`, declared in
  `pyproject.toml` with upper bounds) are installed by **Hermes** into the
  interpreter the gateway actually runs:
  `hermes plugins validate <plugin-path> --install-deps` or
  `hermes pm install`. The plugin never installs Python packages itself.
  If the gateway interpreter cannot import `nacl`/`blake3`, commands fail
  closed naming exactly these Hermes commands.
- **System services** (ClamAV + `clamav-daemon`, the Tor daemon, and the
  tor-enabled `send_hermes` sidecar) are provisioned by the documented
  first-run bootstrap: `bash scripts/install-deps.sh` (system services
  only; `--check` reports per-component status without changing anything,
  `--system-only` skips the gateway-interpreter verify and is what the
  plugin's one-shot self-provisioning hook invokes). Where privilege
  escalation is needed, escalate with your usual admin mechanism. The
  script verifies the gateway interpreter via `/proc/<gateway-pid>/exe`
  and never guesses a shell interpreter.

Then:

1. `send_hermes` binary on PATH (hermes-iroh-interconnect sendme fork).
2. Tor (mandatory — Iroh-over-Tor is the only byte transport):
   `apt install tor` + `scripts/install-tor.sh` (writes `ops/torrc`,
   builds the tor-enabled sidecar, enables the tor service). Both
   endpoints in a pool must enable Tor; the plugin fails closed at
   service construction (lazy, first use) and pool registration
   until then — plugin registration itself is pure wiring and loads
   even with Tor down. No relay fallback exists.
3. Symlink the plugin into `~/.hermes/plugins/agent-pool`.

### Per-profile setup (one gateway per profile)

Plugin discovery is per profile (`HERMES_HOME`): a plugin under
`~/.hermes/plugins/` is visible to the default profile only. A named
profile needs its own copy (or a symlink) and an explicit enable, and
its own ledger/identity under its profile plugin-data dir. Each bot
syncs its slash-command tree from its own profile's enabled plugins,
so setup is per profile:

- **New profile:** install the plugin, then create the profile from
  the default so it inherits the install:
  `hermes profile create <name> --clone --clone-from default`
  (copies config + plugin install; re-apply that profile's bot token
  in its `.env` afterwards). Or symlink an existing install in place
  (below) — symlinking also avoids duplicate plugin-source snapshots.
- **Existing blank profile (no `config.yaml`, no plugins): enable in
  place without recreating the profile** (recreating would strip the
  profile's `.env`, e.g. its bot token):
  1. Symlink the installed plugin into the profile:
     `ln -s ~/.hermes/plugins/agent-pool ~/.hermes/profiles/<name>/plugins/agent-pool`
  2. Enable it: `hermes -p <name> plugins enable agent-pool`
     (the CLI creates a minimal `config.yaml` if missing).
  3. Verify: `hermes -p <name> plugins list` shows `agent-pool`
     enabled. (A copied tree instead of a symlink can trip a
     duplicate plugin-source name at enable time; the symlink is the
     reliable in-place route.)
- **Restart rule:** slash-command trees re-sync only after a gateway
  restart. After enabling a plugin in a profile, restart that
  profile's gateway (`hermes -p <name> gateway restart`, or the shared
  `hermes gateway restart` when profiles share one multiplexer
  process) before expecting the plugin's commands on that bot.

## Tests / Demo

- `env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin /usr/bin/python3.10 -m pytest tests -q`
- `scripts/demo.py` — end-to-end demo of the current model: add repo →
  add task → claim → push (grant + build + deliver) → task completed →
  verify chain. `--real` uses the real transport (send_hermes on PATH).
- `scripts/contributor-push.py` — contributor push step (8b) off Discord:
  `python3 scripts/contributor-push.py /path/to/checkout --repo <repo_id> [--pool <pool_id>] [--json]`
  prints `manifest_json` + `transfer_ticket` + the `/push repo --deliver` command to paste on the pool gateway.
- `scripts/contributor-add.py` — contributor add-repo step off Discord:
  `python3 scripts/contributor-add.py /path/to/checkout --repo <repo_id> [--pool <pool_id>] [--version N] [--json]`
  prints `manifest_json` + `transfer_ticket` + the `/add-repo --deliver` command to paste on the maintainer gateway.
- `scripts/contributor-claim.py` — Workflow C claim+pull off Discord:
  `python3 scripts/contributor-claim.py TICKET_ID [--as KEY_ID]` prints
  the `/claim` text for Bot #1; with `--reply-file REPLY.json --dest DIR`
  it downloads the accepted bundle from Bot #1's reply over Tor.
- `scripts/pool-approve.py` — agent-side approval path (no slash
  command): `python3 scripts/pool-approve.py --ticket TICKET_ID | --repo REPO_ID [--deny]`
  accepts (and completes, for pushes) or records the denial of a
  submitted version. Invoked by the gateway agent after the operator
  decides.
- `scripts/live-discord-check.md` — operator runbook for a live Discord
  guild.

## Bundled skill — operating and joining pools

The repo ships a contributor/maintainer skill at `skills/agent-pool/`
(install it to `~/.hermes/skills/` to give any agent the full operating
manual: roles, whitelist/enrollment, all 10 commands verbatim, the
add-repo link flow, the claim→push→approve flow, version rules, and what
fails closed). It is written for any agent running the plugin on any
machine — no host-specific paths, ports, or test procedures.

## Known housekeeping issue — TODO

Every outbound Tor transfer leaves a `.sendme-send-<id>/` directory next
to the repo (the sendme fork's staging area: bundle + ticket + manifest
for one delivery). They are NOT cleaned up automatically and accumulate —
safe to delete once the transfer completed. TODO: add automatic cleanup
after a confirmed transfer (on the sender side, post-`transfer_id`
ack), plus a housekeeping verb or documented manual sweep for old
directories. Until then: `rm -rf .sendme-send-*` is safe when nothing is
in flight.

## See also

- `docs/architecture.md` — command-surface chain, Tor-routed Iroh
  flow, one-pool model + `pool_gateways`.
- `docs/threat-model.md` — anonymity/residual-risk, guarantees.
- `docs/fallback-transport.md` — why there is no relay fallback
  (fork-only escape hatch), Radicle seed, Tor ceilings.
- `scripts/live-discord-check.md` — operator runbook.

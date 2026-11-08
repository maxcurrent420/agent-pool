# Architecture — Agent Pool (gateway-native, 2.7.0)

Hermes posts; the plugin only returns content. This document traces the
paths with file/function evidence (no brittle line citations).
`H = Hermes checkout (read-only)`; `P = this plugin`.

## Command-surface chain

How a command typed in Discord reaches this plugin's handler and comes
back as a Discord reply:

1. Hermes plugin context registers each command with a handler,
   description, args hint, and text argument mode. Handler contract: a
   function taking the raw argument string and the session-context
   kwargs it declares.
2. The registration entry is stored in the plugin manager. Name
   collisions with built-ins warn and skip.
3. The Discord adapter registers slash commands when it connects.
4. Plugin commands are auto-registered into the native picker: Discord
   name is the lowercased command name; one optional string option is
   added when an args hint is present. This plugin passes an args hint
   plus text mode for all ten commands in `_COMMANDS` in `__init__.py`.
5. Invocation goes through the slash proxy, the authorization gate,
   event building (carrying guild/thread identifiers for profile
   routing), gateway dispatch, and the plugin-handler runner. The
   runner returns the handler's string — **that string is the Discord
   reply**. Plugin commands short-circuit before the agent loop (no
   LLM call); Hermes posts the string.

Constraints: the plugin must be loaded before Discord connects (else
the command misses the next tree sync); a gateway restart or
Discord-adapter reconnect is required for new/changed registrations to
reach the picker; slash authorization is the gateway allowlist, never
per-command Discord permissions.

### Identity the handler receives

Handlers declare the session source and use it for authorization (the
whitelisted user id) and thread-reference recording. Pool selection is
explicit `--pool` among hosted pools; there is no chat-binding
resolution.

## Reply contract

- **Return value**: a plain string or `None`. `replies.py:format_result`
  renders the result dict to the markdown Hermes posts;
  `replies.py:check_content_size` keeps the 4 KB coordination-content
  guard (content guard, not transport).
- Replies over 4096 bytes fail loudly:
  `reply exceeds 4096-byte gateway limit`. They are not silently
  truncated.
- **Markdown**: rendered client-side by Discord. **Embeds are not
  available** — the contract is a single string (the JSON payload).
- **Ticket `thread_ref`**: the handler cannot capture the gateway's
  reply-message id from the return value. The plugin records
  `(chat_id, thread_id, guild_id)` from the session source instead.
- **Unsolicited notices**: no plugin push channel exists outside a live
  call. This plugin ships (1) notices folded into the triggering
  command's returned string, and (2) a ledger `events` queue surfaced
  on the next command reply or via `pool_status`. There is no
  background Discord push.

## Whitelist membership (no onboarding command)

There is no separate onboarding command. Membership is
the env/config whitelist of Discord user ids:

- Env `AGENT_POOL_ALLOWED_USERS` (comma-separated) wins over config
  `allowed_users` (a list); the pool owner from
  `maintainer_discord_user_id` config/env is always whitelisted.
- `ledger.sync_whitelist` syncs the whitelist into `pool_authorized`:
  inserts missing users, never deletes. Removal is `/pool remove`.
- Key attribution is first-claim binding in `pool_memberships`: a
  user's first claim binds the key they present; a different presented
  key is refused with `claim refused: user {user} already bound to key
  {bound} on pool {pool} — presented key {presented} differs;
  first-claim binding is permanent`.
- Absence of a record is refusal on every verb:
  `not authorized for pool {pool_id} — ask the maintainer`. There is no
  legacy skip.

## Maintainer == gateway owner

The user who runs the gateway owns the pool on that gateway; there is
no second owner and no second pool on one gateway.

- Registration refuses `register refused: maintainer must be the
  gateway owner` when the maintainer is not the gateway identity.
- Owner verbs require both the Discord owner match and the same
  gateway (`pools.owner_gateway_key_id`).
- Gateway identity rotation is refused, never auto-updated:
  `gateway identity changed: caller is not the pool's owner gateway —
  re-register the pool to continue`.
- `push_deliver` requires the owner gateway:
  `push deliver refused: only the pool maintainer gateway may accept`.
- Cross-gateway interaction against a remote pool (fetch/claim/
  release/complete) is unaffected.

## One pool per gateway + `pool_gateways`

A gateway hosts and manages exactly one pool (`create_pool` refuses a
second). Pool resolution implements: explicit `--pool` → the single
hosted pool → refusal naming the hosted pools. No chat binding, no
peer/anchor fallback.

Foreign-pool interaction records a `pool_gateways` row (pool id,
gateway endpoint, transport address, role, timestamps) via
`record_gateway`, so an agent can track which pools/gateways it is
working on. Listing gateways is service-level only (no slash command).

## Freeze scope

While frozen, the `pool_status` tool still answers; every other slash
verb is blocked with `pool frozen — contact the maintainer`,
including read verbs. Read-only queries do not survive a freeze.

Caveats, stated honestly: the wrapper guard is best-effort — an
exception inside the guard falls through to the handler; and verbs run
before any pool exists (e.g. register) or naming a foreign pool skip
the freeze check; there is nothing to resolve. Freeze/unfreeze
commands themselves are owner-only and are never blocked by the freeze
they control.

## Agent worktree / branch / budget

The plugin is a passive add-on: it tells the agent how to push and
pull and exposes controls to maintain the pool; the agent runs the
work. On claim the plugin hands the agent `worktree_path`
(`<dest_root>/<repo_id>/worktree-<ticket_id>`, dest_root default
`/tmp/agent-pool-worktrees`, env `AGENT_POOL_WORKTREE_ROOT`),
`branch` (`pool/<ticket_id>`), `base_ref` (`v{accepted_version}`), the
budget dict, and `stop_condition`
`{"condition": "budget_exhausted", "enforce": true}`. The agent runs
`git worktree add`, commit, and push at that path/branch; the plugin
does not run git for the agent. On release/complete the plugin emits
`release worktree {worktree_path} and delete branch {branch}` and the
agent cleans up.

Budgets belong to agents, not pools. Allocation precedence: env
`AGENT_POOL_TASK_ALLOCATIONS` > config `agent_task_allocations` >
config `max_tasks_per_day` > 5. A completed-task count reaching the
allocation refuses claims and pushes (`agent budget exhausted: ...`).
Turns/tokens/wall-clock are secondary: the agent may report them via
the `pool_report_usage` tool, and only reported usage is checked
(`budget exhausted: <field> (<used>/<limit>) — release or push now`).
The plugin does not meter turns. There is no auto-release on
exhaustion.

## Tor-routed Iroh flow

Substrate: Iroh (endpoint, transfer tickets, end-to-end
encryption) over Tor — Iroh-over-Tor is the only byte transport.

- `transport_mode: tor` (default, only valid value) — Iroh Tor custom
  transport over hidden services; neither peer learns the other's IP;
  no relay in the connection path. Both endpoints must enable Tor;
  `tor_ready()` is a hard gate at service construction, plugin
  registration, and pool registration, and fails closed with no
  fallback.
- `relay` and `direct` modes do not exist and cannot be constructed.

Fetch (read): the fetch handler dispatches to `fetch_repo_list`,
`fetch_task_list`, `fetch_repo`, and `fetch_repo_download` on the
service. The first three are freeze+authz-gated;
`fetch_repo_download` is wrapped-key ACL-gated (and verifies the
manifest signature — see below) rather than pool-authz gated, because
it runs on the requester's own gateway.

Push (write): the push handler dispatches to `push_repo` (grant,
claimant-only), then the contributor's agent builds the bundle via
`push_build_bundle` (service-level, no slash command), then
`push_deliver` on the pool gateway: verifies grant one-time +
still-claimed, ticket/manifest match, submitter signature +
membership + claimant, and the owner gateway; fetch → maintainer-
gateway DEK decrypt → BLAKE3 → sandbox audit; clean ⇒ `accept` +
consume grant + complete task; flagged ⇒ task stays claimed,
`push_flagged` event, grant NOT consumed.

Push and pull are never completed from Discord. Discord carries
paths; the agent on the gateway does the pull/push.

## Download-path guarantee

Full chain verification happens on the hosted-pool `fetch()`. The
cross-gateway `/fetch repo --manifest` download verifies the manifest
signature, the wrapped-key ACL, and the BLAKE3 content hash, and
refuses with `fetch refused: manifest signature does not verify`.
There is no local-pool branch in the download path.

## Isolation model

1. **Per-profile ledgers.** `get_db` in `ledger.py` opens the ledger
   via Hermes `plugin_db("agent-pool")`; outside Hermes it falls back
   to a local SQLite file. Two profiles on one host ⇒ disjoint
   databases.
2. **Hosted pool rule.** A gateway answers only for pools whose rows
   exist in its own ledger.
3. **Foreign bytes arrive only by fetch.** The requesting agent's
   propagator fetches over the configured transport; the owner's ledger
   is never written remotely.
4. **Authorization is the whitelisted Discord user id, per pool** —
   owner plus `pool_authorized` rows, enforced by the owner/authorized
   requirement helpers. Authorized-level verbs pass for any authorized
   user; owner-only verbs refuse an authorized non-owner. A key in an
   authorization decision is a bug.
5. **Remote requester authorization** — three independent checks:
   whitelist (people), membership + DEK wrap (bytes: `wrapped_keys`
   must contain the requester's box pubkey), and manifest-chain
   integrity on the hosted-pool fetch (append-only
   `prev_manifest_hash` linkage; every chain entry's signature by the
   maintainer gateway is re-verified).

## Why Iroh QUIC + `send_hermes` bearer tickets

See the fallback-transport document. Summary: no third-party
gatekeeper; the network sees ciphertext + routing metadata only;
transfers are always Iroh-over-Tor; ticket = bearer capability for
ciphertext only; per-version DEK rotation on top. Radicle and
git-over-I2P are documented deployment alternatives.

## Exact network path of repo bytes

1. `git bundle --all` packs the repo (propagator submit).
2. Plaintext BLAKE3-hashed; fresh per-version random DEK (NaCl
   SecretBox) encrypts it — DEKs never reused across versions.
3. DEK wrapped per current member (sealed box).
4. Ciphertext → `send_hermes` provider (transport).
5. Transfers are **always Iroh-over-Tor** (no relay, no direct mode).
6. Receiver fetch: chain + signature verify on the hosted-pool path,
   or manifest-signature + ACL verify on the download path → Iroh
   fetch → BLAKE3 verify vs manifest `content_hash` → DEK unwrap →
   decrypt → `git clone` into `worktree-v<N>`.

No throughput figure is claimed here: transfer speed depends on the
Tor path, network conditions, and repo size, and is not pinned
to a number in these docs.

## What the plugin does vs what Hermes does

The plugin owns ledger, crypto, transport, and decisions; Hermes owns
the bot, the picker, the allowlist, identity delivery, thread
ownership, and all posting.

## Security model

Summary: the threat-model document. Three honest residuals:

1. Malware that defeats both the scan and the owner's review is out of
   scope.
2. Revocation is forward-separation only.
3. Bubblewrap-absent fallback mode runs the static checks unsandboxed
   and reports that mode.

## Passive plumbing (remote pool view)

The plugin is a pool, not a bot: it never observes channels, never
pushes/pulls itself, never executes work. Agents communicate via
Discord gateway commands; the maintainer's agent is the pool authority.

- **Reads (fetch)**: no remote view, no local copies.
  `/fetch repo list` answers with the hosted pool's projects,
  `/fetch task list` with its open/claimed tickets, `/fetch repo <id>`
  with the latest accepted manifest string (on the pool's gateway);
  `/fetch repo --manifest <json>` runs on the requester's own gateway
  (own box key, BLAKE3-verified, manifest signature verified).
  Per-profile DB isolation preserved.
- **Writes (push)**: a contributor's bytes reach the pool only via the
  grant → build → deliver flow (`push_repo` / `push_build_bundle` /
  `push_deliver`); the deliver step downloads the bytes, audits, and
  records the version as submitted — acceptance and task completion
  follow only through the agent-driven approval path (`push_approve`),
  never automatically. The agent on the contributor's gateway builds
  the bundle from its local checkout path; Discord carries only the
  manifest JSON, transfer ticket, and grant. No local copy of a
  foreign pool.
- **Claims (Workflow C)**: the claim originates on the contributor's
  gateway and lands on the maintainer gateway; the maintainer's claim
  reply carries the accepted repo's Tor download link and manifest
  directly, so the claimant needs no local lookup. Remote gateway
  keys enroll as real members (`pool members --enroll` /
  `--mykeys`), and accepted versions are re-wrapped for them.
- **Sync is agent-driven** — no plugin timers or background sync.
- **Authorization is the whitelisted Discord user id, period.** A pool
  records its owner's Discord id at register and whitelisted users
  from env/config. Keys are crypto-only (sign manifests, wrap DEKs).
  A key appearing in an authorization decision is a bug.

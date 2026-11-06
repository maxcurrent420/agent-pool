# Architecture — Agent Pool (gateway-native, 2.6.0)

Hermes posts; the plugin only returns content. This document traces the
exact paths with file:line evidence. `H = /home/maxcurrent/.hermes/hermes-agent`
(Hermes checkout, read-only); `P = /home/maxcurrent/Documents/AntiGravityProjects/agent-pool`.

## Command-surface chain

How a `/pool` typed in Discord reaches this plugin's handler and comes
back as a Discord reply:

1. `H/hermes_cli/plugins.py:649` — `PluginContext.register_command(name,
   handler, description, args_hint, argument_mode)`. Handler contract:
   `fn(raw_args: str) -> str | None`, with the session-context kwargs it
   declares (`plugin_command_dispatch`).
2. `H/hermes_cli/plugins.py:666-674` — the entry
   `{handler, description, plugin, plugin_key, args_hint, argument_mode}`
   is stored in `_manager._plugin_commands` (`plugins.py:1111`). Name
   collisions with built-ins warn + skip (`plugins.py:660-665`).
3. `H/plugins/platforms/discord/adapter.py:1556-1558` — at Discord
   `connect()`, `_register_slash_commands()` runs (in a thread).
4. `H/plugins/platforms/discord/adapter.py:4584-4588` — plugin commands
   are auto-registered into the native picker: Discord name =
   `name.lower()[:32]`; one optional string option `args` is added iff
   `args_hint` is non-empty (`adapter.py:4554-4557`). This plugin passes
   `args_hint` + `argument_mode="text"` for all ten commands
   (`P/__init__.py:_COMMANDS`, `register`).
5. Invocation: `_slash_proxy` → `_run_simple_slash`
   (`adapter.py:4454-4488`) → authorization gate
   `_check_slash_authorization` (`adapter.py:4159-4167`) → `_build_slash_event`
   (`adapter.py:4757+`; carries `guild_id`/`parent_chat_id` for
   profile-routes matching) → `handle_message` → gateway dispatch
   `H/gateway/run_inbound.py:1164` → `_hm_dispatch_plugin_command`
   (`H/gateway/run_inbound_hooks.py:245-260`) → `_hm_run_plugin_handler`
   (`run_inbound_hooks.py:262-302`).
6. `run_inbound_hooks.py:302` returns `str(result) if result else None` —
   **that string is the Discord reply**. Plugin commands short-circuit
   before the agent loop (no LLM call); Hermes posts the string.

Constraints: the plugin must be loaded before Discord `connect()` (else the
command misses the next tree sync); the 100-command global cap
(`adapter.py:4542-4602`, one slot reserved for `/skill`) counts this
plugin's 10; slash authorization is the gateway allowlist, never
per-command Discord permissions (`adapter.py:4603-4607`); a **gateway
restart or Discord-adapter reconnect is required** for new/changed
registrations to reach the picker.

### Identity the handler receives

Handlers declare `source: SessionSource` (`H/gateway/session.py:69-93`:
`platform`, `chat_id`, `user_id` (:76), `thread_id` (:78),
`scope_id`/`guild_id` (:85-86), `profile` (:91), `parent_chat_id` (:87),
`message_id` (:88)). This plugin's handlers declare exactly
`source/session_id/session_key/platform` (`P/__init__.py`, wrapped by
`_wrap_command`) and use `source` for authorization (the whitelisted
`user_id`) and `thread_ref` recording. There is no chat-binding
resolution: pool selection is explicit `--pool` among hosted pools.

## Reply contract

- **Return value**: a plain string or `None`
  (`H/hermes_cli/plugins.py:653`; `run_inbound_hooks.py:288-302`).
  `P/replies.py:format_result` renders the result dict to the markdown
  Hermes posts; `P/replies.py:check_content_size` keeps the 4 KB
  coordination-content guard (content guard, not transport).
- **Markdown**: rendered client-side by Discord; length is clamped by
  the adapter's post path (`H/gateway/plugin_messages.py:38-49`).
  **Embeds are not available** — the contract is a single string.
- **Multi-part output** (optional): `ctx.post_message(text)`
  (`plugins.py:607-612`) posts into the bound session and returns
  `{ok, message_id}` — the id *is* returned to the plugin. Only valid
  while `bind_live_source` is active (`run_inbound_hooks.py:294`).
- **Ticket `thread_ref`**: the handler cannot capture the gateway's
  reply-message id from the return value. The plugin records
  `(chat_id, thread_id, guild_id)` from `source` instead
  (`P/tickets.py:add_ticket`, `pool_resolver` in `P/__init__.py:_service_for`).
  If the command ran in a thread, `source.thread_id` is the thread and
  the reply posts in it. Core exposes no `create_thread` to plugins;
  the native `/thread` command is the way to start a forum post.
- **Unsolicited notices**: no plugin push channel exists outside a live
  call. This plugin ships (1) notices folded into the triggering
  command's returned string, and (2) a ledger `events` queue surfaced on
  the next `/pool`/`/task` reply or via `pool_status`
  (`P/__init__.py:_result`). There is no background Discord push.

## One pool per gateway + `pool_gateways`

A gateway hosts and manages exactly one pool (`L.create_pool` refuses a
second, `ledger.py:264-268`). `_resolve_pool` (`tickets.py:112-143`)
implements: explicit `--pool` → the single hosted pool → refusal naming
the hosted pools. No chat binding, no peer/anchor fallback.

Foreign-pool interaction records a `pool_gateways` row
(`ledger.py:144-156`; schema: `pool_id TEXT PRIMARY KEY, gateway_endpoint,
transport_addr, role, added_at, last_seen`) via `record_gateway`
(`tickets.py:145-149`), so an agent can track which pools/gateways it is
working on. `list_gateways` (`tickets.py:151-153`) is service-level only
(no slash command).

## Tor-routed Iroh flow

Substrate: Iroh (endpoint, relay, transfer tickets, end-to-end encryption)
— unchanged. Anonymity layer: iroh's Tor custom transport via our sidecar.

- `transport_mode: relay` (default) — transfers are relay-only on BOTH
  sender and receiver; the peer learns no IP; the relay sees both peers'
  IPs + metadata, never plaintext.
- `transport_mode: tor` (opt-in) — Iroh Tor custom transport over hidden
  services; neither peer learns the other's IP; no relay in the
  connection path. Both endpoints must enable Tor; `tor_ready()` fails
  closed (names `systemctl start agent-pool-tor`) and never silently
  downgrades to relay.
- `direct` mode does not exist (`transport.py:161-163`).

Fetch (read): `_handle_fetch` (`__init__.py:481`) →
`svc.fetch_repo_list` / `fetch_task_list` / `fetch_repo` /
`fetch_repo_download` (`tickets.py:168-264`). First three are
freeze+authz-gated; `fetch_repo_download` is wrapped-key ACL-gated only.

Push (write): `_handle_push` (`__init__.py:518`) → `push_repo` (grant,
claimant-only, `tickets.py:847-890`) → contributor builds bundle
(`push_build_bundle`, `tickets.py:892-910`, service-level, no slash
command) → `push_deliver` (`tickets.py:912-989`): verifies grant
one-time + still-claimed, ticket/manifest match, submitter signature +
membership + claimant, fetch → maintainer-DEK decrypt → BLAKE3 → sandbox
audit; clean ⇒ `accept` + consume grant + complete task; flagged ⇒ task
stays claimed, `push_flagged` event, grant NOT consumed.

## Isolation model

1. **Per-profile ledgers.** `ledger.py:147-152` opens via
   `plugin_db("agent-pool")`; `H/plugins/plugin_storage.py:27-46`
   resolves `get_hermes_home()` on every call — two profiles on one host
   ⇒ disjoint `data.db` files.
2. **Hosted pool rule.** A gateway answers only for pools whose rows
   exist in its own ledger. `_resolve_pool` (`tickets.py:112-143`)
   implements: explicit `--pool` → the single hosted pool → refusal naming
   the hosted pools. No chat binding, no peer/anchor fallback.
3. **Foreign bytes arrive only by fetch.** The requesting agent's
   `RepoPropagator` (`propagation.py`) fetches over
   `RealSendHermesTransport` (`transport.py`); the owner's ledger is never
   written remotely.
4. **Authorization is the whitelisted Discord user id, per pool** —
   owner (`pools.owner_discord_user_id`) + `pool_authorized` rows,
   enforced by `_require_owner` / `_require_authorized`
   (`tickets.py`). Authorized-level verbs pass for any authorized user;
   owner-only verbs refuse an authorized non-owner. A key in an
   authorization decision is a bug.
5. **Remote requester authorization** — three independent checks:
   - Whitelist (people): driving Discord id must be owner or authorized.
   - Membership + DEK wrap (bytes): `wrapped_keys` must contain the
     requester's `box_pubkey` — the byte boundary.
   - Manifest-chain integrity: append-only `prev_manifest_hash` linkage
     (`ledger.py`); `fetch` re-verifies every chain entry's maintainer
     signature.

## Why Iroh QUIC + `send_hermes` bearer tickets

See [README.md](../README.md#repo-sharing--the-architecture-decision).
Summary: no third-party gatekeeper; relay sees ciphertext + routing
metadata only; transfers are always relay-only on both ends; ticket =
bearer capability for ciphertext only; per-version DEK rotation on top.
Radicle and git-over-I2P are documented fallbacks
([fallback-transport.md](fallback-transport.md)).

## Exact network path of repo bytes

1. `git bundle --all` packs the repo (`propagation.py` `submit`).
2. Plaintext BLAKE3-hashed; fresh per-version random DEK (NaCl
   SecretBox) encrypts it — DEKs never reused across versions.
3. DEK wrapped per current member (sealed box).
4. Ciphertext → `send_hermes` provider (`transport.py`).
5. Transfers are **always relay-only on both ends** (no direct mode).
6. Receiver `fetch` (`propagation.py`): chain + signature verify →
   wrapped-DEK ACL → Iroh fetch → BLAKE3 verify vs manifest
   `content_hash` → DEK unwrap → decrypt → `git clone`.

Throughput (measured): **~2 MB/s via relay** (50 MB in 25.9 s,
`.hermes/reports/phase0-spikes.md:5`). Gateway payloads ≤ ~1.6 KB
(`.hermes/reports/phase2-propagation.md:20`).

## What the plugin does vs what Hermes does

The plugin owns ledger, crypto, transport, and decisions; Hermes owns the
bot, the picker, the allowlist, identity delivery, thread ownership, and
all posting. The old `GatewayAdapter`/`gateway.py` file is **deleted**
(not deprecated).

## Security model

Summary: [threat-model.md](threat-model.md). Three honest residuals:

1. Malware that defeats both the scan and the owner's review is out of
   scope.
2. Revocation is forward-separation only.
3. bubblewrap-absent fallback mode runs the static checks unsandboxed.

## Passive plumbing (remote pool view)

The plugin is a pool, not a bot: it never observes channels
(`on_message` not implemented), never pushes/pulls itself, never executes
work. Agents communicate via Discord gateway commands; the maintainer's
agent is the pool authority.

- **Reads (fetch)**: no remote view, no local copies. `/fetch repo list`
  answers with the hosted pool's projects, `/fetch task list` with its
  open/claimed tickets, `/fetch repo <id>` with the latest accepted
  manifest string; `/fetch repo --manifest <json>` runs on the requester's
  OWN gateway (own box key, BLAKE3-verified). Per-profile DB isolation
  preserved.
- **Writes (push)**: a contributor's bytes reach the pool only via the
  grant → build → deliver flow (`push_repo` / `push_build_bundle` /
  `push_deliver`); the deliver step audits and completes the task. No
  import, no local copy of a foreign pool.
- **Sync is agent-driven** — no plugin timers or background sync.
- **Authorization is the whitelisted Discord user id, period.** A pool
  records its owner's Discord id at register and each invitee's at
  `/pool invite --discord-user`; owner-only verbs compare that id. Keys
  are crypto-only (sign manifests, wrap DEKs). A key appearing in an
  authorization decision is a bug.

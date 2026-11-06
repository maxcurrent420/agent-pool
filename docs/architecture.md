# Architecture — Agent Pool (gateway-native)

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

Constraints that follow from the chain: the plugin must be loaded before
Discord `connect()` (else the command misses the next tree sync); the
100-command global cap (`adapter.py:4542-4602`, one slot reserved for
`/skill`) counts this plugin's 10; slash authorization is the gateway
allowlist, never per-command Discord permissions
(`adapter.py:4603-4607`); a **gateway restart or Discord-adapter
reconnect is required** for new/changed registrations to reach the
picker.

### Identity the handler receives

Handlers declare `source: SessionSource` (`H/gateway/session.py:69-93`:
`platform`, `chat_id`, `user_id` (:76), `thread_id` (:78),
`scope_id`/`guild_id` (:85-86), `profile` (:91), `parent_chat_id` (:87),
`message_id` (:88)). This plugin's handlers declare exactly
`source/session_id/session_key/platform`
(`P/__init__.py:_handle_pool` etc., wrapped by `_wrap_command`) and use
`source` for authorization (the whitelisted `user_id`) and `thread_ref`
recording. There is no chat-binding resolution: pool selection is
explicit `--pool` among hosted pools (2109 Phase 2).

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
  command's returned string, and (3) a ledger `events` queue surfaced on
  the next `/pool`/`/task` reply or via `pool_status`
  (`P/__init__.py:_result`). There is no background Discord push.

## Button gate

Requirement: a click lands on the **Hermes Discord gateway bot** and is
routed to the plugin. No plugin-owned discord.py client.

### Why `register_platform_handler` (path a)

- `H/hermes_cli/plugins.py:831-846` — `register_platform_handler(platform,
  factory)`; the factory receives `(native, adapter)` where `native` is
  discord.py `commands.Bot` for Discord. Adapter is read-only.
- `H/gateway/platforms/base.py:2211-2241` `_wire_plugin_handlers` —
  idempotent per native client, keyed `(plugin, qualname)`, each factory
  isolated; called from Discord `connect()` at `adapter.py:1573`.
- From the gateway's own bot the plugin adds `native.add_listener(on_interaction)`
  and a `discord.ui.View` subclass. Component clicks route to the View's
  callbacks; the listener covers messages from a previous process run.
- `discord` is imported **inside the factory** only
  (`P/buttons.py:make_discord_button_factory`), per the
  `plugins.py:835` guidance.

### Why not core `_HermesView` (path b — rejected)

`_HermesView` is module-private inside `_define_discord_view_classes()`
(`adapter.py:6314-6407`): allowlist auth, single-use `resolved`, `_gate`,
`_finalize_embed`, `_disable_all`, `on_timeout`. It is not exported and
has no plugin registration point — reusing it would require a **core
change**. The plugin copies the *pattern* (auth gate → gate → finalize)
in its own View class instead. Need zero core edits.

### `ask_choice` fallback (path c)

`H/hermes_cli/plugins.py:618-622`; gateway binds `chat_choices`
(`run_inbound_hooks.py:295`, `H/gateway/plugin_command_choice.py:27-50`):
a messaging chat gets `adapter.send_clarify` — real Discord buttons — a
tap resolves through `resolve_gateway_clarify`; undeliverable cards fall
back to a numbered text list. Synchronous and bound to the invoking
command, so it is the instant path inside `/integrate`
(`P/__init__.py:_handle_integrate`), while (a) is the persistent
ticket-thread control. Both share `PoolService.resolve_integration`.

### `custom_id` format and identity resolution

- `custom_id = agent-pool:{decision}:{ticket_id}`,
  `decision ∈ {approve, edit, discard}` (`P/buttons.py:TicketDecisionView`;
  parsed by `P/bridge.py:parse_button_event`, `agent-pool:` prefix
  optional). The `agent-pool:` prefix namespaces against other plugins.
- The listener filters `InteractionType.component` + the prefix, and
  defers within 3 s. **Unrelated interactions are ignored** — core flows
  keep working.
- Identity: the whitelist is the authorization — the clicker's
  `interaction.user.id` must be the ticket pool's **owner** (the Discord
  id recorded at `/pool register --discord-user`). The check is
  `PoolService._require_owner` (`P/tickets.py`), driven from both the
  factory's `_dispatch_to_service` (`P/__init__.py:register`) and the
  `bridge.dispatch_button_event` dispatch core (`P/bridge.py`), then
  defense-in-depth again inside `resolve_integration`
  (`P/tickets.py`). An `identity_map` of platform id → agent key stays
  available for crypto **attribution** (who acted), never for access.
  Unknown or non-owner clickers get an ephemeral refusal, never a
  dispatch. See "Discord user ≠ agent identity" below.
- The `gateway_platform_event` hook stays registered for compatibility
  but is behaviour-neutral today: it fires only for normalized events
  (`H/gateway/run_adapters.py:1645-1651`), and Discord button
  interactions are not among them in this Hermes version.

## Isolation model

1. **Per-profile ledgers.** `P/ledger.py:147-152` opens via
   `plugin_db("agent-pool")`; `H/plugins/plugin_storage.py:27-46`
   resolves `get_hermes_home()` on every call and follows the active
   profile. Two profiles on one host ⇒ disjoint `data.db` files.
   `_service_for` uses `L.default_db_path().parent` as the primary data
   dir (`P/__init__.py:62`) — no hard-coded `~/.hermes` fallback.
   `ctx.profile_name` is available (`plugins.py:405-411`).
2. **Hosted pool rule.** A gateway answers only for pools whose rows
   exist in its own ledger (one pool by default; `--pool` disambiguates
   a multi-pool host). `_resolve_pool` (`P/tickets.py`) implements
   exactly: explicit `--pool` → the single hosted pool → refusal naming
   the hosted pools. There is no chat binding, no peer/anchor fallback,
   and no resolution chain — cross-gateway addressing is the platform's
   job (Hermes routes the command to the addressed pool's gateway), so a
   friend's command lands on Pool A's bot, not Gateway B's.
3. **Named pool not hosted ⇒ `pool_not_local` refusal.** No command ever
   opens another agent's ledger.
4. **Foreign bytes arrive only by fetch.** The requesting agent's
   `RepoPropagator` (`P/propagation.py:193`) fetches over
   `RealSendHermesTransport` (`P/transport.py:137-255`); the owner's
   ledger is never written remotely. A friend types commands through
   *their own* agent's gateway; Discord carries no repo bytes. There is
   no import and no local copy of a foreign pool — reads are
   `/pool fetch` + `/work fetch --manifest`.
5. **Authorization is the whitelisted Discord user id, per pool** —
   owner (`pools.owner_discord_user_id`) + `pool_authorized` rows,
   enforced by `_require_owner` / `_require_authorized` in
   `P/tickets.py`. Authorized-level verbs (claim, submit, list/show,
   fetch, members) pass for any authorized user; owner-only verbs
   (register, invite, remove, freeze/unfreeze, auto-approve, the
   `/task add` mint, approve/deny/modify, `/add-repo`, `/integrate` +
   its decisions, packet mint) refuse an authorized non-owner. A key in
   an authorization decision is a bug.
6. **Remote requester authorization** — three independent checks,
   each at a distinct layer:
   - *Whitelist* (people): the driving Discord id must be owner or
     authorized — the wrapper (`_wrap_command`, `P/__init__.py`) is the
     gate; the service keeps defense-in-depth via the same helpers.
   - *Membership + DEK wrap* (bytes): `wrapped_keys` must contain the
     requester's `box_pubkey` (`propagation.py` `submit`;
     `_is_member`; refusal at `fetch`). This ACL is the byte boundary.
   - *Manifest-chain integrity*: append-only `prev_manifest_hash`
     linkage (`ledger.py`); `fetch` re-verifies every chain entry's
     maintainer signature and refuses contributor-signed "accepted"
     manifests. Owner-side gate: `PoolService.integrate`
     (`P/tickets.py`).
7. **Discord user ≠ agent key.** The key (`ensure_identity`,
   0600) is crypto-only — it signs artifacts and unwraps DEKs. The
   platform-id → key binding recorded at invite is **attribution**
   (who acted), consulted for records and overlap checks, never for
   access. Unmapped users are refused. See the residual risk in
   [threat-model.md](threat-model.md).

## Why Iroh QUIC + `send_hermes` bearer tickets instead of the alternatives

| Alternative | Why not the default |
|---|---|
| **GitHub** | A third party becomes gatekeeper of pool metadata and history (takedown/ToS risk on coordination). Account ACLs have no analogue for the per-member DEK model: everyone with repo access can read everything, ever. |
| **Self-hosted git server** | You run and patch the server. ACLs are account-based, not cryptographic — no confidentiality against the server admin. NAT traversal and off-LAN access become your problem. |
| **Discord attachments** | 25 MB attachment cap; Discord becomes the de-facto store (ToS/limits risk); zero confidentiality. Measured gateway payloads are ≤ ~1.6 KB with `gateway_calls_over_4KB=0` (`.hermes/reports/phase2-propagation.md:20`) — bytes were never meant to flow there. |
| **Radicle** | Strong store-and-forward + TCP-only support, but: you run a seed; ACL is per-repo/peer, not per-version DEK; no direct maintainer↔contributor transfer semantics. Kept as fallback #1 (see [fallback-transport.md](fallback-transport.md)). |
| **IPFS / git-over-I2P** | No built-in access control or confidentiality; content addressing is not an ACL. git-over-I2P kept as fallback #2 for QUIC-blocked networks. |
| **Chosen: Iroh + `send_hermes`** | No third-party gatekeeper; the relay sees ciphertext + routing metadata only; transfers are **always relay-only on both ends** (`--ticket-type relay` ticket *and* a relay-only endpoint policy on send and receive, so the peer learns no IP; there is no direct mode); ticket = bearer capability for ciphertext only; per-version DEK rotation on top. Cost: sender must stay online during transfer; repos >~1 GB out of scope for v1; relay throughput ~2 MB/s vs ~97 MB/s for the (removed) direct path. |

## Exact network path of repo bytes

1. `git bundle --all` packs the repo (`P/propagation.py:271` `submit`).
2. Plaintext BLAKE3-hashed; fresh per-version random DEK (NaCl
   SecretBox) encrypts it — DEKs never reused across versions.
3. DEK wrapped per current member (sealed box, `propagation.py` submit).
4. Ciphertext → `send_hermes` provider (`P/transport.py:152`).
5. Transfers are **always relay-only on both ends**: the send argv
   always uses `--ticket-type relay` (peer learns no IP) and the
   `send_hermes` endpoints on both send and receive are built with
   `clear_ip_transports()` + `addr_filter(AddrFilter::relay_only())`
   so neither side publishes or accepts direct IP addresses. The relay
   (n0 by default, or an operator-configured `relay_url` pointing both
   ends at a self-hosted/trusted relay) learns both peers' IPs and
   metadata, never plaintext. `HERMES_IROH_RELAY=off` is a no-op (there
   is no direct fallback); `tor` fails closed. See README
   "Transport privacy".
6. Receiver `fetch` (`propagation.py:471`): chain + signature verify →
   wrapped-DEK ACL → Iroh fetch → BLAKE3 verify vs manifest
   `content_hash` → DEK unwrap → decrypt → `git clone` bundle →
   chain-signature verification.

Throughput (measured on the build host): **~2 MB/s via relay**
(50 MB in 25.9 s, `.hermes/reports/phase0-spikes.md:5`). The direct path
measured ~97 MB/s (314,572,800 bytes in 3.2 s, BLAKE3 verified,
`.hermes/reports/phase6-ship.md:53`) but disclosed the peer's IP, so it
was removed — the relay-only cost is the documented price of peer-IP
anonymity. Sender stays online during
transfer (Phase 0). **Discord carries only manifests/notices/tickets**;
gateway payloads ≤ ~1.6 KB, `gateway_calls_over_4KB=0`
(`phase2-propagation.md:20`).

## What the plugin does vs what Hermes does

See the division-of-labour table in [README.md](../README.md#division-of-labour--who-does-what).
The short version: the plugin owns ledger, crypto, transport, and
decisions; Hermes owns the bot, the picker, the allowlist, identity
delivery, thread ownership, and all posting. The old
`GatewayAdapter`/`gateway.py` file is **deleted** (not deprecated) —
a leftover adapter would keep the "plugin posts" assumption compilable.

## Security model

Summary: [docs/threat-model.md](threat-model.md). Three honest residuals
to carry into any review:

1. **Malware that defeats both the scan and the owner's review is out of
   scope.** Containment is by construction (contributors never write to
   canonical; workers sandboxed); merging without reading the diff is on
   you.
2. **Revocation is forward-separation only.** Already-fetched versions
   cannot be revoked retroactively.
3. **bubblewrap-absent fallback mode.** Without bwrap the static checks
   run unsandboxed and the report says so
   (`sandbox: fallback:unsandboxed`); network-off containment does not
   apply.

## Passive plumbing (remote pool view)

The plugin is a pool, not a bot: it never observes channels (`on_message`
is not implemented), never pushes/pulls itself, and never executes work.
Agents communicate via Discord gateway commands; the maintainer's agent
is the pool authority.

- **Reads (fetch)**: no remote view, no local copies. `/pool fetch` answers
  with the hosted pool's projects, its open/claimed tickets, and for a repo
  the latest accepted manifest string; `/work fetch --manifest <json>`
  downloads, unwraps the DEK with the requesting agent's own box key,
  BLAKE3-verifies against the manifest, and clones. `remote_repos` remains
  only as the human-readable `--link` operator note (never a fetch source).
  Per-profile DB isolation is preserved — no command opens
  another agent's ledger.
- **Access**: authorization is the whitelisted Discord user id only
  (`_require_owner` / `_require_authorized` in tickets.py): owner +
  `pool_authorized` ids per pool. Authorized-level verbs (claim,
  submit, list/show, fetch) pass for any authorized user; owner-only
  verbs (register, invite, remove, freeze, auto-approve, the `/task add`
  mint, approve/deny/modify, `/add-repo`, `/integrate` + its decisions,
  packet mint) refuse an authorized non-owner. Canonical writes stay on
  the owning ledger.
- **Submit-to-maintainer (push)**: a contributor's bytes still cannot
  teleport. `/work submit` builds a signed proposal packet
  (`crypto.build_proposal`) carrying the transfer ticket; the agent
  copies it to the maintainer's gateway (`/task add --packet`), where
  the maintainer verifies (`verify_proposal`), runs the sandbox audit,
  and mints. `/work submit` on the pool gateway is the normal path;
  packets are the cross-gateway write path.
- **Pull (read)**: `/pool fetch` returns the hosted pool's projects,
  its open/claimed tickets (never `pending`), and for a repo the latest
  accepted manifest string (`transfer_ticket` + `wrapped_keys` +
  maintainer verify key). `/work fetch --manifest <json>` runs on the
  requesting agent's OWN gateway: it unwraps the DEK with its own box
  key, BLAKE3-verifies against the manifest, and clones. The
  wrapped-key ACL is the byte-authorization boundary. Agents pull when
  starting work (`/work start` reuses this fetch on the pool gateway)
  and push when uploading — operator discipline, no timers, no hooks,
  no background sync.
- **`/add-repo --link`**: registers link + repo row and defers byte
  fetch; no `RepoPropagator.fetch` on registration. The link is stored
  in `remote_repos` as a human-readable operator note and is NEVER a
  fetch source — `/pool fetch` is the real read path.
- **`/task add`**: mints ticket_id in the local ledger; per-pool
  `auto_approve` (meta `auto_approve:<pool_id>`) sets `open` vs
  `pending`. The mint is owner-only in the whitelist model.

### Passive plumbing — corrected model (2.5.0)

The 2.4.0 mechanism was superseded: the import/anchor layer and the
Discord-ID → key → maintainer comparison are deleted, and authorization
is the whitelisted Discord user id only.

- **Whitelist authorization:** pools carry `owner_discord_user_id`
  (set at register, which refuses without `--discord-user`) and a
  `pool_authorized` table (rows written at register for the owner and at
  each `/pool invite --discord-user`; `/pool remove` deauthorizes).
  `_require_owner` / `_require_authorized` are one indexed query each —
  no `identities`/`members` join, no key lookups. The wrapper
  (`_wrap_command`, `P/__init__.py`) is the gate; the service methods
  keep defense-in-depth with the same helpers. A key appearing in an
  authorization decision is a bug.
- **Keys are crypto-only:** the ed25519 signing key signs manifests and
  proposal packets (authorship of an artifact); the box keypair wraps
  each version's DEK so only the invited agent's machine can decrypt
  fetched bytes. The fetching agent uses its own gateway's box private
  key; no key is a login.
- **Fetch replaces import:** `/pool import`, `/pool trust`, the `peers`
  table, trust anchors, and the `imported_*` tables are removed — reads
  are `/pool fetch` + `/work fetch`. Old tables stay on disk unread (no
  destructive migration); new DBs never create them. `remote_repos`
  survives only as the `--link` operator note.
- **Resolver collapse:** `_resolve_pool` is explicit `--pool` → the
  single hosted pool → refusal naming hosted pools. `pool_chats` and the
  chat binding recorded at register are dropped.
- **Freeze:** one guard in `_wrap_command`; owner-only on every path
  (both service `freeze`/`freeze_global` and the wrapper), and absolute
  for everything else — frozen returns exactly
  `pool frozen — contact the maintainer` and records a `freeze_blocked`
  event. Only freeze/unfreeze runs while frozen.
- **Approval queue:** non-owners see open/accepted only; the owner sees
  `pending` with approve/deny/modify `ask_choice` → `approve_ticket`.
- **Verify-key threading (unchanged):** `/pool invite --verify-key <hex>`
  records `members.verify_pubkey_hex`; `/task add --packet` verifies the
  signature against it before mint, checks current membership
  (revocation) and replays (`packet_hash` in `events`).

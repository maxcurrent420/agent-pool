# Agent Pool — Hermes plugin

Friends on Discord form an agent team. Anyone submits a repo to a pool, the
maintainer (or their agent) audits and approves it, approved members' agents
claim and work tasks on it, and finished work is audited and merged back.

**This is a Hermes plugin. Hermes posts; the plugin only returns content.**
Every slash-command handler returns a plain string (or `None`); the Hermes
gateway posts that string to Discord. The plugin never opens a Discord
client, never imports `discord` outside the button factory (`buttons.py`,
imported lazily inside the factory), and never stages repo bytes on Discord.
See "Division of labour" below.

## Division of labour — who does what

The Discord gateway belongs to Hermes. The plugin's job is ledger,
crypto, transport, and decisions; posting and button routing are Hermes's.

| The plugin does | Hermes (the gateway) does |
|---|---|
| Owns the pool ledger (SQLite via `plugin_db`), manifest chain, ticket store, budgets | Owns the Discord bot, guild/channel routing, and the allowlist ingress gate |
| Packs repos (`git bundle --all`), encrypts with a fresh per-version DEK, wraps the DEK per member, sends bytes over Iroh via `send_hermes` | Renders slash commands in the Discord picker from `ctx.register_command` registrations |
| Returns a markdown **string** from each command handler — the reply Hermes posts | Posts that string as the Discord reply (plugin commands short-circuit before the agent loop; no LLM call) |
| Decides who may act (pool ownership, maintainer checks, `pool_not_local` refusals) | Delivers identity: `source.user_id`, `chat_id`, `guild_id`, `thread_id` arrive with every command |
| Registers a Discord button gate via `ctx.register_platform_handler("discord", factory)` — the factory receives **the gateway's own bot** and adds a listener + `discord.ui.View` | Runs the bot that owns the message; button clicks are `Component` interactions routed by Hermes's bot to the plugin's listener |
| Falls back to `ctx.ask_choice` inside `/integrate` (Hermes renders clarify buttons in the invoking chat) | Renders the clarify buttons and routes the tap |
| Records the ticket's `thread_ref` from the invoking command's `source` (`chat_id`/`thread_id`/`guild_id`) | Owns threads; the plugin creates none |

Evidence for the command chain: `ctx.register_command` stores handlers in
`hermes_cli/plugins.py:649-674`; the Discord adapter mirrors them into the
native slash picker at `plugins/platforms/discord/adapter.py:4584-4588`
(registration runs at connect, `adapter.py:1556-1558`); dispatch runs through
`gateway/run_inbound_hooks.py:262-302`, which calls the handler and returns
`str(result) if result else None` — that string is the Discord reply. The
plugin-platform-handler seam is wired at
`gateway/platforms/base.py:2211-2241` (called from Discord `connect()` at
`adapter.py:1573`). Full chain: [docs/architecture.md](docs/architecture.md).

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
- **No unsolicited push**: the plugin has no background Discord push
  channel. Notices from an action are folded into that action's returned
  string; asynchronous events are queued in the ledger `events` table and
  surfaced on the next command reply or via the `pool_status` tool.

## Repo sharing — the architecture decision

**Repo bytes travel over Iroh QUIC (hole-punched direct, n0 public relay as
fallback) as BLAKE3-verified bearer tickets via `send_hermes`. Discord
carries only manifests, notices, and tickets. GitHub, a self-hosted git
server, Discord attachments, Radicle, and IPFS were all considered and
rejected as the default.** The full rationale, with trade-offs, is in
[docs/architecture.md](docs/architecture.md#why-iroh-quic--send_hermes-bearer-tickets-instead-of-the-alternatives);
the short version:

- **GitHub** would make a third party the gatekeeper of pool metadata and
  history, and its account ACLs have no analogue for the per-member DEK
  confidentiality model. **A self-hosted git server** makes you run and
  patch that server, and its ACLs are account-based, not cryptographic —
  the server admin sees everything. **Discord attachments** cap at 25 MB,
  make Discord the de-facto store, and offer no confidentiality; measured
  gateway payloads are ≤ ~1.6 KB with zero repo bytes on Discord
  (`.hermes/reports/phase2-propagation.md:20`), so bytes were never meant
  to flow there. **Radicle / IPFS** are kept as documented fallbacks
  ([docs/fallback-transport.md](docs/fallback-transport.md)) for
  store-and-forward or TCP-only networks, not the default, because v1
  needs direct maintainer↔contributor transfer with per-version DEK
  rotation.

### The exact network path of repo bytes

1. `git bundle --all` packs the repo (`propagation.py:271` `submit`).
2. Plaintext is BLAKE3-hashed; a fresh per-version DEK encrypts it
   (NaCl SecretBox) — DEKs are never reused across versions.
3. The DEK is wrapped (sealed box) per *current* member's box pubkey.
4. The ciphertext goes to a `send_hermes` provider (`transport.py:152`).
5. QUIC: direct hole-punched path where possible, n0 public relay
   fallback. The relay sees ciphertext + routing metadata only.
6. The receiver's `fetch` (`propagation.py:471`) re-verifies the manifest
   chain and maintainer signatures, checks the wrapped-DEK ACL, fetches
   over Iroh, verifies BLAKE3 against the manifest `content_hash`,
   unwraps the DEK, decrypts, and `git clone`s the bundle.

Measured on this host: **~2 MB/s over the n0 public relay path**
(50 MB in 25.9 s, `.hermes/reports/phase0-spikes.md:5`) and
**~97 MB/s over the direct/LAN path** (314,572,800 bytes in 3.2 s, BLAKE3
verified, `.hermes/reports/phase6-ship.md:53`). Gateway payloads stay
≤ ~1.6 KB — the gateway never carries repo bytes. The sender's
`send_hermes` provider must stay online during the transfer; fetches retry
with bounded backoff and then fail with a structured
`TransportUnavailable`. Repos well above ~1 GB are out of scope for v1.

## Quick start

```bash
# 1. Install + enable (Python deps pynacl/blake3 install at enable time;
#    a hermes-runtime-venv WARN about blake3 is expected — tests use the
#    system interpreter, see below).
hermes plugins install <path-or-url-to-this-repo>   # or clone into ~/.hermes/plugins/
hermes plugins enable agent-pool

# 2. Restart the gateway. REQUIRED: the Discord slash-command tree sync
#    (adapter.py:1556-1558) and the plugin platform-handler wiring
#    (adapter.py:1573) both run at connect(). New commands do not appear
#    in the Discord picker until then.

# 3. In Discord (bot allowlist must contain you), run the flow:
#    /pool register <pool_id> --name <name> --discord-user <your Discord user id>
#    /pool invite <pool_id> <member key_id> --box-pubkey <hex> --discord-user <their id>
#    /task add --pool-id <pool_id> --repo <repo> --title "..."
#    /claim <ticket> --paths a.py,b.py
#    /work submit <ticket> --repo-path <path> --version 1
#    /integrate <ticket>     → audit; then click Approve (buttons), or use
#    /integrate approve <ticket>   (same code path + maintainer check)
#    /audit show <ticket>
```

Requirements: `git`; `bubblewrap` (audit/worker sandbox — without it the
scanner says so and runs unsandboxed, see
[docs/threat-model.md](docs/threat-model.md)); `send_hermes` on PATH for
the real transport (`hermes-iroh-interconnect` builds it from source with
cargo).

> Version note: the gateway-native migration is a **breaking
> communication-layer change** — the plugin no longer posts to Discord
> itself (the old `GatewayAdapter`/`gateway.py` architecture is deleted,
> not deprecated). The manifest is version **2.0.0**; `gateway_channel_id`
> no longer exists (gateway routing is Hermes's).

## Commands

| Command | What it does |
|---|---|
| `/pool register <pool_id> --name <name> --discord-user <id>` | Create a pool; caller is maintainer; binds this chat to the pool and records the maintainer's Discord user id |
| `/pool invite <pool_id> <key_id> --role member\|maintainer --box-pubkey <hex> --discord-user <id>` | Add a member (records the Discord-user → key_id binding the button gate uses) |
| `/pool members <pool_id>` | List members |
| `/pool remove <pool_id> <key_id>` | Revoke a member (forward separation) |
| `/pool freeze [--global] [--unfreeze]` | Per-pool freeze; **no pool id / `--global` = global kill-switch** across every pool |
| `/task add --pool-id … --repo … --title … --brief … [--max-turns …]` | Create a ticket |
| `/task list\|show [--pool-id …]` | List/show tickets |
| `/claim <ticket> --paths a.py,b.py [--max-turns …] [--override]` | Claim (declares paths; overlap refused) |
| `/release <ticket>` | Release a claim back to open |
| `/work submit <ticket> --repo-path PATH --version N` | Submit finished work (contributor-signed, pending; bytes go over Iroh) |
| `/integrate <ticket>` | Maintainer gate: fetch branch, sandbox diff scan, audit, then offer approve/edit/discard (buttons + `ask_choice`) |
| `/integrate approve\|edit\|discard <ticket> [--edit-file PATH --edit-content TEXT] [--reason …]` | **The actionable decision** (same code path as a button click) |
| `/audit show <ticket>` | Show the audit report for a ticket |
| `pool_status` (tool) | Relay URL, budgets, ledger/identity readiness, queued notices |

Every command registers with an `args_hint` + `argument_mode="text"`, so
each appears in the Discord picker with one optional `args` string option.

### The approve/edit/discard buttons — read this

Buttons are real: `buttons.py` registers a Discord platform handler with
Hermes, and the **gateway's own bot** renders a `discord.ui.View` with
`custom_id = agent-pool:{decision}:{ticket_id}` (`approve|edit|discard`).
A click lands on that bot, reaches the plugin's listener, resolves the
clicker (Discord user id → `key_id` via the ledger binding recorded at
`/pool invite --discord-user`), verifies the clicker **is the pool
maintainer**, and dispatches to `PoolService.resolve_integration`.
Unmapped or non-maintainer clickers get an ephemeral refusal and nothing
is dispatched. If buttons are unavailable, `/integrate approve|edit|discard`
runs the exact same code path with the same maintainer check, and
`/integrate <ticket>` also offers `ctx.ask_choice` clarify buttons inside
the invoking chat.

### Freeze semantics

`/pool freeze` with no pool id (or `--global`) sets the **global
kill-switch**: every pool in the process is frozen (persisted in the
ledger `meta` table, so it survives restarts), and all pool-mutating and
claim/work/integrate actions raise. `/pool freeze <pool_id>` freezes only
that pool. `/pool freeze --unfreeze` (optionally with a pool id) clears
it. Read-only queries (`/task list|show`, `/pool members`, `/audit show`)
still work while frozen, and `/work cancel` still works (a kill path
must not be blockable by the switch).

## Isolation model

Agents running separate pools cannot act on each other's pools:

- **Per-profile ledgers.** The ledger opens via Hermes's profile-aware
  `plugin_db("agent-pool")` (`ledger.py:147-152`), which resolves
  `get_hermes_home()` on every call — two profiles on one host get
  physically disjoint `data.db` files. Keys (ed25519/box, 0600) live under
  the same profile-scoped plugin data dir.
- **Pool ownership rule.** A pool is *local-owned* iff its row exists in
  this profile's ledger **and** `maintainer_key_id` equals this agent's
  host `key_id` (`tickets.py:PoolService._assert_local_owned`).
- **Resolution order, never a guess:** explicit `--pool` → the invoking
  chat's pool binding (`pool_chats`, recorded at `/pool register`) → the
  profile's single local-owned pool → refusal: *no pool bound to this
  chat — pass --pool*.
- **Named pool not local ⇒ hard refusal `pool_not_local`.** No command
  ever opens another agent's ledger (they are different files on
  different machines anyway).
- **Remote pools via Iroh only.** A foreign pool is reached by the
  *requesting* agent fetching bytes over `send_hermes` on its own
  machine — never by mutating the owner's ledger remotely, and never
  through Discord. From the maintainer's gateway, remote pools appear
  only as manifests/notices received over Iroh: each agent's Discord
  surface is exactly the pools it hosts.

Full model: [docs/architecture.md](docs/architecture.md#isolation-model).

## Config

| Key | Default | Meaning |
|---|---|---|
| `relay_url` | `""` | Iroh relay URL; `off` = direct/LAN (fast path on one LAN) |
| `max_turns` | 25 | Per-claim budget: max agent turns |
| `max_tasks_per_day` | 5 | Per-claim budget: max tasks/day |
| `token_cap` | 50000 | Per-claim budget: token cap |
| `wall_clock_seconds` | 1800 | Per-claim budget: wall clock |
| `pools` | `{}` | Per-pool settings (budgets, relay overrides) |
| `transport` | `real` | `real` (send_hermes/Iroh) or `fake` (in-memory; tests/CLI) |
| `work_root` | `~/.hermes/plugin-data/agent-pool/work` | Worktrees + integration checkouts |

There is **no `gateway_channel_id`** — gateway routing is Hermes's, not
the plugin's.

## Tests

```bash
env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin \
  /usr/bin/python3.10 -m pytest tests -q
```

(The Hermes runtime venv lacks blake3/PyNaCl/pytest; always use this form
from the repo root.) The Discord button gate is covered by
`tests/test_discord_e2e.py` (real `discord.ui.View` + real listener +
real dispatch against a fake native bot; the Hermes checkout is
read-only and not modified) plus `tests/test_buttons.py`.

## Demo

`scripts/demo.sh` runs the full loop end-to-end (fake transport for a
fast, hermetic run; `--real` uses `send_hermes`) and prints measured
wall-clock time. `scripts/measure_300mb.py` does the real 300 MB
transfer measurement.

## Security

Read [docs/threat-model.md](docs/threat-model.md) before running this with
real repos: the relay cannot read content; malware that defeats both the
scan and the owner's review is out of scope; revocation is
forward-separation only; the Discord-user → agent-key binding is
operator-recorded, so a mis-bound mapping is the button gate's residual
risk. Full design: [docs/architecture.md](docs/architecture.md).

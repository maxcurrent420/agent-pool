---
name: agent-pool
description: Use when joining or running an agent pool over Iroh-over-Tor.
category: dev
---

# Agent Pool — operating a pool and interacting with one

Agent Pool is a **passive plugin**: it tells an agent how to push and pull
repo bytes and exposes the controls to maintain a pool. It never runs
worktrees and never merges by itself — **your agent does the git work**,
acting on your approval and on your local checkout paths.

Two properties define it:

- **No third-party dependency.** Repo bytes move only over
  **Iroh-over-Tor** (onion-routed; neither peer learns the other's IP; no
  relay in the connection path). There is no plaintext/relay mode — bytes
  are refused, not downgraded, when Tor is not ready.
- **Friends, not platforms.** A pool is one gateway, one maintainer, one
  whitelist. Coordination happens over any chat gateway the maintainer
  runs (e.g. Discord), but bytes never travel through it: Discord carries
  paths (manifest JSON, transfer tickets, grant tokens) — never file
  content.

## Decide your role first

| I want to… | Role | Start here |
|---|---|---|
| host a pool friends push work into | **Maintainer** | Run your own pool (below) |
| contribute a repo or take a task on a friend's pool | **Contributor** | Joining a pool (below) |

Both are the same plugin; the role comes from which gateway you run and
who whitelisted you. An agent may run **many** profiles, one gateway
each, and interact with a pool through any gateway the maintainer told
them to address — there is no router or platform that dispatches for you.

## Running your own pool (maintainer)

You own the pool on the gateway you run. There is no second owner and no
second pool on one gateway. Registration binds **both** your Discord user
id and the gateway bot's id to the gateway key; a maintainer who is not
the gateway identity is refused.

1. **Install** the plugin and its deps (`hermes plugins validate
   --install-deps` / the documented bootstrap script). The plugin needs a
   Tor-enabled Iroh transport (sidecar + tor daemon) and an AV engine for
   ingestion audits; the first-run bootstrap provisions them.
2. **Configure** (env or `~/.hermes/.env` — values are secrets; the plugin
   only ever discusses variable *names*):
   - `AGENT_POOL_MAINTAINER_DISCORD_USER_ID` — your Discord user id.
   - `AGENT_POOL_MAINTAINER_BOT_DISCORD_USER_ID` — the gateway bot's id.
   - `AGENT_POOL_ALLOWED_USERS` — comma-separated Discord ids allowed on
     the pool. **There is no invite command** — this list *is* the
     membership. The owner is always included. An empty list = owner only.
   - `AGENT_POOL_TASK_ALLOCATIONS` — optional per-agent task budgets
     (`key_id=N`). Defaults to 5 completed tasks per agent.
3. **Register the pool**: `/pool register` (or let your agent call the
   service). Pools live in the profile's plugin-data dir; the pool
   maintainer is whoever registered it, and only that identity can
   approve, enroll, freeze, or remove.
4. **Start the gateway** for that profile, then add tasks
   (`/task_add --repo <repo_id> --title "..."`) or invite a repo with
   `/add-repo <repo_id> --path <abs/path>` (owner fast path).

## Joining a pool (contributor)

1. **Be whitelisted.** The maintainer adds your Discord id to their
   `AGENT_POOL_ALLOWED_USERS` (or config) and restarts/reloads. Until
   then every verb against that pool is refused:
   `not authorized for pool <pool_id> — ask the maintainer`.
2. **Hand your gateway key to the maintainer.** On your gateway,
   `/pool members --mykeys` prints your key id and box/verify public keys
   (public material, safe to send over chat). The maintainer enrolls you:
   `/pool members --enroll <key_id> <box_pubkey_hex> <verify_pubkey_hex>`.
   Enrollment binds *your* Discord id to *your* key, re-wraps accepted
   versions so you hold DEK entries, and is required before you can claim
   or add repos.
3. **Do the work** — see the two flows below.

## The commands

All commands are prefixed with `/` on the gateway and addressed to the
pool's bot (mention the bot if the channel requires it).

| Command | Args | Use it to |
|---|---|---|
| `pool` | `register`, `members`, `remove`, `freeze`, `list` | bare `/pool` → pool status JSON as text; `members --mykeys` (your keys), `members --enroll <key> <box> <verify>` (owner), `remove <discord_id>` (owner), `freeze` (owner, fails closed) |
| `task` | `add`, `list`, `show` | `task_add --repo <repo_id> --title "..." [--brief "..."]` opens a ticket; `task_list [--repo ...] [--state open]` shows them; `task show <id>` details |
| `task_add` | flat alias of `task add` | same as above, shorter |
| `task_list` | flat alias of `task list` | same as above |
| `claim` | `[ticket]` or `--any-project --max-tasks N` | Claim a ticket. The reply carries `worktree_path`, `branch` (`pool/<ticket_id>`), `base_ref`, and your budget. Bulk claim needs `--paths` or `--any-project`; a bare ticket is whole-ticket |
| `release` | `[ticket]` | Give a claim back (claimant and owner only). The reply tells you to clean up the worktree and branch |
| `fetch` | `repo list`, `task list [--ticket <id>]`, `repo <repo_id>`, `repo --manifest <json>` | Read the pool: what repos exist, what tasks, or download a repo from a signed manifest |
| `add-repo` | `<repo_id> --path <abs/path>` (owner fast path), `--deliver <transfer_ticket> --manifest <json> [--repo <id>]`, `approve <repo_id> [--version N]` (owner), `submissions [--repo <id>]` | Contribute a repo (link flow), inspect the approval queue, or approve (owner) |
| `push` | `repo <repo_id> --ticket <id>` (grant), `repo --deliver <transfer_ticket> --token <grant_token> --manifest <json>` (deliver) | Two steps: get a grant for your ticket, then deliver the bundle for audit |
| `audit` | `show [ticket]` | Show the audit report for a repo version or claim |

`pool_status` and `pool_report_usage` are **agent tools** (toolset
`agent_pool`), not slash commands: `pool_status` reports transport state,
budgets, and the `awaiting_approval` queue; `pool_report_usage` records
usage on a claim (the plugin records what you report; it does not meter).

## Contributing a repo (link flow)

Repo **bytes** go over Iroh-over-Tor; Discord only carries the manifest
and ticket. Never pass a local file path to another pool — `--link` is
rejected (`add-repo: --link is not supported`).

1. On your gateway, package your checkout:
   `scripts/contributor-add.py /path/to/checkout --repo <repo_id>`
   (pass `--version N` when the pool already has accepted versions — see
   *Versions* below). It prints a signed `manifest_json`, an Iroh-over-Tor
   `transfer_ticket`, and a ready-to-send deliver command.
2. Send the deliver command to the maintainer's bot:
   `/add-repo --deliver <transfer_ticket> --manifest '<json>'
   --repo <repo_id>`.
3. The maintainer gateway fetches over Tor, verifies your signature, the
   pool ACL, and the content hash, audits the bundle, and records the
   version as **awaiting approval** — it is *not* accepted yet.
4. The reply's `agent_prompt` tells the maintainer's agent to ask you (the
   operator) for the decision. You approve with
   `scripts/pool-approve.py --repo <repo_id>` (DEK rotation, chain
   append), or deny with `--deny`.
5. After acceptance, **you** merge: `git fetch <bundle>` then `git merge`
   into your canonical checkout. The plugin never merges.

## Taking a task (claim → edit → push)

1. Find work: `/task_list --repo <repo_id>` (or `/fetch task list`).
2. Claim: `/claim <ticket_id>`. The reply gives you the accepted repo's
   Tor download link and manifest. Save it; your agent runs
   `scripts/contributor-claim.py <ticket_id> --reply-file <reply.json>
   --dest <dir>` to fetch, verify, and git-clone the accepted version
   into a worktree.
3. Do the work in the worktree on `pool/<ticket_id>` (the plugin never
   touches your files).
4. Ask for a push grant on the maintainer's bot:
   `/push repo <repo_id> --ticket <id>`. The grant token is yours; a
   deliver never consumes it, so a redelivery is legal.
5. Package the worktree: `scripts/contributor-push.py <worktree_path>
   --repo <repo_id> --pool <pool_id> --version N` (see *Versions*), then
   deliver: `/push repo --deliver <transfer_ticket> --token <grant_token>
   --manifest '<json>'`.
6. The deliver reply records the version as **awaiting approval** (clean
   audit) and asks you for the decision. Approve with
   `scripts/pool-approve.py --ticket <id>` — that accepts (DEK rotation +
   chain append), consumes the grant, and **completes the ticket** — or
   deny with `--deny` (the claim stays live). Then merge the accepted
   bundle into your checkout yourself.

There is no `/complete`: a ticket completes only via a clean, approved
push.

## Versions

Versions are per-repo and append-only in the chain. If the pool already
has v1/v2 accepted, a fresh submission must be v3: contributor scripts
resolve the next version from pool-side data (your enrollment context, or
an explicit `--version N`) — a local ledger with no enrolled repos
defaults to v1. Delivering a version that already exists as *accepted*
never overwrites it.

## What always fails closed

Refusals are the contract, not bugs — read them literally:

- `not authorized for pool <id> — ask the maintainer` — you are not on
  the whitelist for that pool.
- `claim refused: user <id> already bound to key <key> ... a bound key is
  immutable` — your first-claim binding is permanent; present the same
  key.
- `manifest signature does not verify` / transport errors — Tor is not
  ready, the ticket expired, or the manifest was altered. Re-read, retry,
  re-issue.
- `agent budget exhausted: <n> of <allocation> tasks completed on pool
  <id>` — agent allocations are task counts; there are **no pool
  budgets**. Ask the maintainer to raise your allocation.

## Where things live

- Ledger (pools, members, versions, grants, claims): the profile's
  plugin-data dir (`plugin_db("agent-pool")`); there is no file fallback.
- Worktrees: `<root>/<repo_id>/worktree-<ticket_id>` (`/tmp/agent-pool-worktrees`
  by default), canonical checkouts under the pool's
  `work/integrate/<repo_id>/`, accepted bundles as `bundle-v<N>.git`.
- Gateway/chat sessions: your Hermes state store (`state.db`);
  read it WAL-fresh with `mode=ro` — **never** `immutable=1`, which
  skips the WAL and returns stale rows.

## One-line rules

The maintainer is the user who runs the gateway. There are no other
owners or pools on one gateway. Membership is a whitelist, not a command.
Budget belongs to agents, not pools. Discord carries paths; bytes move
over Iroh-over-Tor or they don't move.

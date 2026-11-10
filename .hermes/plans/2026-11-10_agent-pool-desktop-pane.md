# Agent Pool — Hermes Desktop Pane ("Pool Console") Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a Hermes Desktop UI pane to the agent-pool plugin that ties the plugin's existing 10 slash commands, 2 agent tools, and operator scripts into form-driven flows (add-repo, fetch task list, claim/assign, pull, push, approve+merge) plus an operation tracker — composing the existing architecture, never reimplementing or bypassing it.

**Architecture:** A **unified-package desktop plugin**: `desktop/plugin.js` (plain ESM, no build step) ships inside the agent-pool repo and loads through the disk door inside the plugin folder (`~/.hermes/plugins/agent-pool/desktop/plugin.js`), which the existing rsync deploy already keeps in sync. Three channels, per the authoritative pane spec (`references/panes.md`, desktop section — the operator-supplied mojomast copy was fetched and matches this file):

1. **UI → agent (actions):** the pane composes a prompt for the desktop agent session (via `host.composer`) and the agent executes it exactly as the user would type it — so authorization (whitelist, owner gating, budget, Tor gate) stays entirely inside the plugin.
2. **Agent → UI (state):** the documented pattern — *a tool writes state (plugin-data / sidecar); the pane polls or subscribes*. A new agent tool `pool_pane_report` records operation outcomes (command replies, `pool_status` snapshots, refusals) into a sidecar JSON under `plugin_data_dir("agent-pool")`; the pane polls it.
3. **UI → backend (read-only):** a `dashboard/plugin_api.py` FastAPI router mounts `/api/plugins/agent-pool/` (the namespace shared by the desktop and dashboard SDKs). It serves **read-only** routes: the same read paths `pool_status` uses (via the service layer / `plugin_db`, exactly as `pool_status` does — never the ledger file directly, never in-process globals) and the agent-written sidecar. Because the ledger already lives in `plugin_db`, this route works under `plugins.isolation: host` (separate per-profile plugin-host process) with no shared globals.

The pane is **passive UI + composer + tracker**: it never mutates pool state itself. The pane reads state only from these channels and keeps its own operation log in `ctx.storage`.

**Tech Stack:** `@hermes/plugin-sdk` + `react/jsx-runtime` (only allowed imports in the pane), `host.composer` (UI→agent), `ctx.rest` → `dashboard/plugin_api.py` (UI→backend, read-only), `pool_pane_report` agent tool + `plugin_data_dir` sidecar (agent→UI), `host.state` (session binding), `ctx.storage` (op tracking), existing plugin commands/scripts (unchanged except one new tool).

---

## Current context / verified facts (HEAD `32ec833`)

- Repo: `/home/maxcurrent/Documents/AntiGravityProjects/agent-pool` (clean). Installed copy: `~/.hermes/plugins/agent-pool`, synced by rsync.
- Registered surface (`__init__.py:register()` at line 1092): tools `pool_status`, `pool_report_usage` (toolset `agent_pool`); commands `pool, fetch, task, add-repo, task_add, task_list, claim, release, push, audit` via `ctx.register_command`. `plugin.yaml` `provides_tools`/`provides_commands` must stay equal to real registrations.
- `tickets.py` `PoolService` methods (line refs): `register_pool:603`, `add_ticket:755`, `claim:1186`, `claim_any:1296`, `release:1342`, `push_repo:1382`, `add_repo_build_bundle:1541`, `add_repo_approve:1712`, `enroll_member:1818`, `push_deliver:1894`, `push_approve:2090`, `merge_approved:2249`, `fetch_repo_download:470`.
- Operator scripts: `scripts/contributor-add.py`, `contributor-claim.py`, `contributor-push.py`, `pool-approve.py`.
- Test suite: `env -u PYTHONNOUSERSITE -u PYTHONPATH /usr/bin/python3.10 -m pytest -q` from repo root; **392 passing** at HEAD.
- Two live gateway profiles: `default` (maintainer bot) and `pool-contrib` (contributor bot), shared Discord channel. Bytes move only over Iroh-over-Tor; the plugin never posts to Discord itself — command handlers return strings the agent posts.
- Desktop SDK contract (verified against live docs, `desktop-plugin-sdk`): plugin = default-exported `HermesPlugin { id, name, register(ctx) }`; only importable specifiers are `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`; file is loaded **uncompiled** (no JSX syntax — use `jsx()`/`jsxs()` calls). `ctx.register({ id, area: PANES_AREA, title, data: { placement, width }, render })` for the pane; `ROUTES_AREA` + `SIDEBAR_NAV_AREA` for a full page; `host.composer.setDraft/submit(sessionId, text)` sends a prompt to the agent session fail-closed; `host.state.focusedSessionId` / `host.state.gateway` read app state; `ctx.storage` is plugin-scoped JSON persistence; `ctx.registerSettingsPage` for preferences; catalog rule 8 forbids prototype patching, `eval`, app-chunk imports, DOM scraping.
- **Note:** the working examples named in the dispatch brief (`~/.hermes/plugins/kanban/dashboard/…`, `project-explorer`) are **not installed on this host** — this plan follows the documented desktop-plugin-sdk contract (which supersedes dashboard-era patterns) instead of copying them.

## Hard constraints (every task)

1. **No new transport, no direct ledger writes.** The pane and any helper code never import `tickets.PoolService` from the desktop side or read `~/.hermes/plugin-data/agent-pool/data.db`. All state comes from agent replies; all actions go through the agent.
2. **No second authorization path.** Owner-only verbs (`/add-repo approve`, `pool-approve.py`, merge) are composed into prompts; the plugin's own identity checks still refuse non-owners. The pane displays the plugin's refusal verbatim — it never "knows better".
3. **No bytes outside Iroh-over-Tor.** The pane never carries file content — only paths, repo ids, ticket ids, and prompt text.
4. **All host API calls go through the SDK transport** — `ctx.rest` / `host.composer` / `host.request` / `host.notify`. Never a raw `fetch` with credentials or hand-attached authorization headers; never extract stored grants. Any binary/asset the pane needs arrives as JSON via a `ctx.rest` route into a Blob URL that is revoked on unmount. Dashboard and desktop transports are never mixed (no `window.__HERMES_PLUGIN_SDK__` code in `desktop/plugin.js`, no bridge calls in dashboard code).
5. **`provides_tools`/`provides_commands` changes are explicit and mirrored**: this plan adds exactly ONE agent tool (`pool_pane_report`, Task 4) — `plugin.yaml` gains it in `provides_tools` and the catalog entry mirrors the change; no new commands.

---

## Task 1: Create `desktop/plugin.js` skeleton (pane + route + nav + settings page)

**Objective:** A loadable desktop plugin that registers the pane, a full-page Pool Console route, sidebar nav, and a settings page, hot-reloadable in Hermes Desktop.

**Files:**
- Create: `desktop/plugin.js` (in the **plugin repo**; deploy lands it in the installed tree)

**Steps:**
1. Write `desktop/plugin.js` with:
   - `export default { id: 'agent-pool', name: 'Agent Pool', register(ctx) { … } }`
   - Pane contribution: `{ id: 'pane', area: PANES_AREA, title: 'Agent Pool', data: { placement: 'right', width: '420px' }, render: () => jsx(PoolPane, {}) }`
   - Route + nav: `{ id: 'page', area: ROUTES_AREA, data: { path: '/agent-pool' }, render }` paired with `{ id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/agent-pool', label: 'Agent Pool', codicon: 'repo' } }`
   - Settings page via `ctx.registerSettingsPage?.({ id: 'settings', title: 'Agent Pool', render })` — sub-pages: `gateways` (list/profile mapping), `display`.
   - All UI built with `jsx()`/`jsxs()` from `react/jsx-runtime`; dark-mode-friendly (use SDK `ui.*` theme variables / `text-(--ui-text-*)` classes, never hardcoded colors).
2. Gate ALL logic behind a single `register(ctx)`; wrap the pane body in try/catch with an inline error card naming the contribution id (SDK error boundaries cover it; belt-and-suspenders).

**Acceptance:**
- `node --check desktop/plugin.js` passes (plain ESM parses).
- Grep: file imports **only** `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`; contains no `eval(`, `new Function`, `document.`, `window.` (except none), no `__HERMES_PLUGIN_SDK__`, no `prototype`.
- Plugin still loads (see Task 11 doctor step).

**Commit:** `feat(desktop): agent-pool pane skeleton (pane + route + nav + settings)`

---

## Task 2: Session binding + gateway/profile selector

**Objective:** The pane binds to the focused chat session and knows which gateway profile each operation targets.

**Files:**
- Modify: `desktop/plugin.js`

**Steps:**
1. Read `host.state.focusedSessionId` (nanostore; use `useValue` from the SDK) to bind the pane to the session whose composer it will submit into. Show "bound session: <id or 'none'>" — if none, all forms render but Submit is disabled.
2. Gateway selector: a two-way toggle/`SegmentedControl` with values `default` (maintainer gateway) and `pool-contrib` (contributor gateway), persisted in `ctx.storage` under `gateways.active` and remembered per-op. Every composed prompt names the profile explicitly (e.g. "Using the pool-contrib gateway profile: …") so the agent routes to the right bot.
3. Status strip: show `useValue(host.state.gateway)` (socket status) and a `tor ready?` probe note — the pane itself never probes Tor; it renders whatever the agent's `pool_status` reply last said.

**Acceptance:** Switching the selector survives pane close/reopen (storage round-trip); with no focused session, Submit buttons are disabled with a tooltip explaining why. **Pane UX (per spec):** every op submission fires `host.notify` (toast) and highlights its tracker row; dark mode uses the app's theme variables only; any user toggle in the pane (polling on/off, collapsed sections, hidden views) is persisted in `ctx.storage` and checked before every agent-driven change — the agent/pane never re-enables something the user turned off.

**Commit:** `feat(desktop): session binding and gateway selector`

---

## Task 3: The action pipeline — compose → submit → track

**Objective:** One helper every form uses: build a prompt, submit it to the bound session, and record the operation in the tracker.

**Files:**
- Create: `desktop/prompts.js` — pure functions that turn form state into prompt text (one prompt per operation; see Tasks 4–6). Pure = no SDK import needed, trivially testable.
- Modify: `desktop/plugin.js`

**Steps:**
1. `prompts.js` exports per-operation builders, e.g.
   ```js
   export function addRepoOwnerPrompt({ gateway, repoId, path }) {
     return `On the ${gateway} gateway, register this repo in the pool and ingest it as a new version: run /add-repo ${repoId} --path ${path}. Return the plugin's reply verbatim.`
   }
   ```
   Every builder includes: (a) the gateway profile, (b) the exact slash command or script invocation with literal args, (c) "return the plugin's reply verbatim" so the pane can parse state from it. **Never let the prompt free-hand the operation** — the args are the form's fields, quoted.
2. `submitOp(op)` in `plugin.js`: builds `{ id, kind, gateway, args, status: 'submitted', at: Date.now() }`, `await host.composer.submit(sessionId, text)`; on `false` (no surface answered) mark the op `failed: composer unreachable` and toast via `host.notify`. On success append the op to `ctx.storage['ops']` (keep the last 200, newest first) and mark it `awaiting agent`.
3. The pane's tracker list renders ops with status pills: `submitted → awaiting agent → done / refused / failed` (status transitions come from refresh, Task 8).

**Acceptance:** A submitted op appears in the tracker immediately even if the agent is slow; a failed `submit` (returns false) never records a phantom "sent" op.

**Commit:** `feat(desktop): prompt builders and operation pipeline`

---

### Task 4: Read-only backend (`dashboard/plugin_api.py`) + agent-side state tool `pool_pane_report`

**Objective:** Give the pane a documented read channel into pool state (the same data `pool_status` already exposes) and an agent→pane state bridge, per the pane spec's Agent↔pane patterns table.

**Files:**
- Create: `dashboard/manifest.json` — minimal, desktop-only package: `{"name": "agent-pool", "label": "Agent Pool", "api": "plugin_api.py"}` (no `tab`/`entry` — this plugin does NOT add a web dashboard tab; the manifest exists only so the shared `/api/plugins/agent-pool/` namespace mounts).
- Create: `dashboard/plugin_api.py` — FastAPI router, **GET-only**:
  - `GET /api/plugins/agent-pool/status` → calls the exact read path `_pool_status` uses (`__init__.py:_pool_status`) — service/`plugin_db` reads only, no writes, no Tor probe.
  - `GET /api/plugins/agent-pool/pane-state` → returns the agent-written sidecar JSON (below) with its `updated_at`.
  - No POST/PUT/DELETE routes exist. No route mutates anything. Comment at top of file states the read-only contract.
- Modify: `__init__.py` `register()` — register one new tool `pool_pane_report` (toolset `agent_pool`): writes `{"ops": [...], "pool_status": {...}, "updated_at": ...}` to `pane-state.json` under `plugins.plugin_data_dir("agent-pool")` (profile-aware storage — the documented Agent→UI channel). It writes ONLY this sidecar; it never touches the ledger.
- Modify: `plugin.yaml` — add `pool_pane_report` to `provides_tools`; bump `requires_hermes: ">=0.23.0"` (SDK-transport floor for the desktop pane) and `version: "2.8.0"`.
- Modify: `plugin-catalog/agent-pool.yaml` — mirror `requires_hermes` and `version` so catalog metadata matches the pin (per catalog rules).
- Test: `tests/test_desktop_pane.py` (see Task 10).

**Steps:**
1. Implement the router by reusing `_pool_status`'s internals (import from `__init__.py`, or extract a shared `_status_snapshot(ctx)` helper both use — DRY).
2. `pool_pane_report` accepts `{op_id, kind, status, detail}` and upserts the op into the sidecar; the agent calls it after each command reply it wants the pane to see (the pane's prompts instruct it to).
3. Tests: route handlers return dicts and never raise on empty state; `pool_pane_report` registered name appears in `provides_tools` (fake-ctx test, the single most valuable one per the testing skill); no route method other than GET.

**Acceptance:**
- Fake-ctx test: registered tools == `provides_tools` (now 3).
- `grep "post\|put\|delete" dashboard/plugin_api.py` (case-insensitive, as decorators) → no mutating route.
- `hermes plugins validate` passes with the new `provides_tools`.

**Commit:** `feat(pane): read-only /api/plugins/agent-pool routes + pool_pane_report state tool`

> **Deploy note (from the spec):** changing/adding `plugin_api.py` does not take effect until the gateway/dashboard process restarts (route-table prefix skip) — Task 11 includes the restart. With `plugins.isolation: host` the router runs in a separate per-profile plugin-host process; both it and `pool_pane_report` share state only through `plugin_data_dir` files / `plugin_db`, never in-process globals.

---

## Task 5: Forms — add-repo (own pool + designated pool), fetch task list, claim/assign

**Objective:** The three discoverability forms.

**Files:**
- Modify: `desktop/prompts.js`, `desktop/plugin.js`

**Forms and exact mappings:**

1. **Add repo.** Mode toggle: *My pool (owner fast path)* vs *Contribute to a designated pool (link flow)*.
   - Owner fast path inputs: `repo_id`, absolute local `path`, optional `--version N` (required when the pool already has accepted versions — the prompt instructs the agent to first ask the maintainer gateway `/fetch repo <id>` for `accepted_version` if the user didn't supply one).
     Prompt maps to: `/add-repo <repo_id> --path <abs/path>` (+ `--version N` when given).
   - Contributor link flow inputs: `repo_id`, local checkout `path`, target pool maintainer's gateway (which profile talks to that bot), optional explicit `--version`. Prompt maps to a **two-step** agent instruction: run `scripts/contributor-add.py <path> --repo <id> [--version N]`, capture the printed `manifest_json` + `transfer_ticket`, then send `/add-repo --deliver <transfer_ticket> --manifest '<json>' --repo <repo_id>` to the maintainer's bot. The pane shows the two steps as one tracked op of kind `add-repo-link` with substatus.
   - Failure states surfaced: `add-repo: --link is not supported` (pane forbids remote URLs — its path field validates absolute local path shape only), whitelist refusal `not authorized for pool <id>`, signature/transport failures, version-too-low refusal ("pass an explicit --version greater than the tip").

2. **Fetch task list.** Inputs: pool selector (from last `pool_status`/`fetch repo list` results the pane has seen), optional repo filter.
   Prompt maps to: `/fetch repo list` (bootstraps the pool/repo catalog) then `/fetch task list [--repo <id>]`. The pane instructs the agent to reply with the task table; the pane stores the latest reply snapshot in `ctx.storage['snapshot.tasks']` with a timestamp for offline display.

3. **Claim / assign to profile.** Inputs: pick ticket (dropdown from the fetched task snapshot; free-text fallback), target profile (the gateway selector plus an optional "claimant identity" note — the plugin binds claim to key + Discord id automatically).
   Prompt maps to: `/claim <ticket_id>` (or `/claim --any-project --max-tasks N` when the user picks "auto-assign N tasks"). The pane reminds the agent to save the reply's `worktree_path`, `branch` (`pool/<ticket_id>`), `base_ref`, and budget into the reply text so the pane can record them; pane stores `snapshot.claims[<ticket>]`.
   Failure states surfaced: `claim refused: user <id> already bound to key <key>` (immutable key binding), `agent budget exhausted: <n> of <allocation> tasks completed`, not-whitelisted.

**Acceptance:** Each form's Submit produces one tracked op whose prompt contains the literal command string from the skill's command table; the claim form cannot be submitted without a ticket id.

**Commit:** `feat(desktop): add-repo, fetch-tasks, and claim forms`

---

## Task 6: Forms — pull repo, push/submit completed work

**Objective:** The contributor work loop.

**Files:** `desktop/prompts.js`, `desktop/plugin.js`

**Forms and exact mappings:**

1. **Pull repo (fetch accepted bytes into a worktree).** Inputs: ticket id (prefilled from a claim op), destination directory.
   Prompt maps to a **one-step agent instruction**: run `scripts/contributor-claim.py <ticket_id> --reply-file <saved claim reply json> --dest <dir>`. The claim reply file path is part of the claim op's recorded context (the agent was told to save it); the pull form pre-fills from the selected claim. Show resulting `worktree_path` + `branch` in the tracker op.
   Failure states: manifest signature does not verify / transport errors ("Tor is not ready, the ticket expired" — pane tells the user to re-read/re-issue), destination not writable.

2. **Push / submit completed work.** Three sub-steps rendered as one guided card (the plugin's two-step push + packaging):
   1. Grant: `/push repo <repo_id> --ticket <id>` on the contributor gateway → the grant token.
   2. Package: agent runs `scripts/contributor-push.py <worktree_path> --repo <repo_id> --pool <pool_id> --version N` — with the same version-snapshot caveat: take `next_version` from the grant reply or re-ask `/fetch repo <repo_id>` for `accepted_version`; never trust the enroll-time `chain_tips`.
   3. Deliver: `/push repo --deliver <transfer_ticket> --token <grant_token> --manifest '<json>'`.
   The pane tracks this as one op of kind `push` with three substatuses; the deliver reply's `awaiting approval` state moves the op to `done (awaiting approval)` and creates the pending-approval entry (Task 8). Explicitly note in the prompt: a redelivery is legal (the grant is not consumed by deliver) so "Retry deliver" is offered on transport failure.
   Failure states: unknown tip (refuse-and-re-ask), budget exhausted, transport not ready, `manifest signature does not verify`.

**Acceptance:** The push card cannot fire step 3 before steps 1–2 statuses are filled in (fields for ticket/token/manifest path are linked); every sub-step is still a single agent prompt the user could have typed.

**Commit:** `feat(desktop): pull and push/submit forms`

---

## Task 7: Forms — approve + merge (owner)

**Objective:** Owner-gated acceptance and merge, still owner-gated in the plugin.

**Files:** `desktop/prompts.js`, `desktop/plugin.js`

**Forms and exact mappings:**

1. **Approve.** Inputs: what is being approved — a submitted repo version (`repo_id`, optional `--version N`) or a pushed claim (`ticket_id`); Deny toggle.
   Prompt maps to: `scripts/pool-approve.py --repo <repo_id> [--version N]` / `--ticket <id>` (or `--deny`). The pane labels this **OWNER ONLY** and states in the prompt: "If the plugin refuses because this profile is not the pool owner, report the refusal verbatim and do not retry."
2. **Merge.** Inputs: repo/ticket, canonical checkout path (defaults to the pool's `work/integrate/<repo_id>/` convention as shown in the agent's reply).
   Prompt maps to: agent performs `git fetch <bundle>` + `git merge` into the canonical checkout — **the plugin never merges**; the pane's prompt says so and asks the agent to run the merge itself on the approved bundle. Where the maintainer flow calls the `merge_approved` service path (`tickets.py:2249`), it does so through the agent executing the plugin's documented flow, never via a pane-side import.
   Failure states surfaced: non-owner refusal (shown verbatim), audit not clean, no accepted bundle for the version.

**Acceptance:** Approve/Merge forms are visibly marked owner-only; a non-owner's composed prompt still runs and the plugin's refusal is what the pane records (`refused` status with the plugin's message).

**Commit:** `feat(desktop): owner approve/merge forms with refusal surfacing`

---

## Task 8: Tracking — the operation log and refresh loop

**Objective:** "Keep track of these operations" without duplicating the ledger.

**Files:** Modify: `desktop/plugin.js`; Create: `desktop/track.js`

**Steps:**
1. `ctx.storage` schema (pane-local, NOT the ledger):
   - `ops[]`: `{ id, kind, gateway, args, status, at, replyRef }` — `kind ∈ {add-repo-owner, add-repo-link, fetch-tasks, claim, pull, push, approve, merge}`; `replyRef` is the transcript position/session id where the agent's reply landed.
   - `snapshot.tasks`, `snapshot.claims`, `snapshot.poolStatus`: last parsed agent replies with timestamps.
2. **Refresh (two channels, per the pane spec):**
   - **Agent→UI (primary):** the pane polls `ctx.rest('/pane-state')` — `dashboard/plugin_api.py` returns the `pane-state.json` sidecar that the `pool_pane_report` tool keeps updated (poll interval: manual Refresh button + optional 60 s toggle, default off; respect the user's choice — never re-enable polling the user turned off). Each polled op outcome updates the tracker status.
   - **UI→agent (fallback/initial):** one batched prompt to the agent: "Report pool status now: run pool_status and /fetch repo list, call pool_pane_report with the results." The pane parses the resulting sidecar `pool_status` snapshot into `transport state / budgets / awaiting_approval queue` and marks tracker ops `done`/`refused`: a repo/ticket appearing in `awaiting_approval` flips its push/add op to `done (awaiting approval)`; a budget line flips related claim ops to `refused`.
3. **Views:** filter tabs — Active ops, Awaiting approval (from the `pool_status` snapshot), Claims & budgets, Audit (button composing `/audit show [ticket]`).

**Acceptance:** Tracker survives reload (storage); the Awaiting-approval view is populated **only** from `pool_status` output, never guessed; a sidecar written by `pool_pane_report` in one profile is only ever read by that profile's pane (profile-aware `plugin_data_dir`); polling respects the user's on/off choice across reloads.

**Commit:** `feat(desktop): operation tracker and status refresh`

---

## Task 9: Security & ownership review of the pane code

**Objective:** Verify the pane is not a second authorization path.

**Files:** Review-only + fixes in `desktop/*`.

**Checklist (each becomes an assertion in Task 10's tests):**
- The `dashboard/plugin_api.py` backend is **read-only**: GET routes only; it reads the service layer / `plugin_db` the same way `pool_status` does — it can never become a write path around the command/agent flow. No mutating route may ever be added without re-planning.
- No file reads/writes by the pane; no paths to the ledger; no SQLite access from JS. The pane reaches state only via `ctx.rest` to its own namespace and `ctx.storage`.
- All host API calls go through the SDK transport (`ctx.rest`, `host.composer`, `host.notify`) — never a raw `fetch` carrying credentials or hand-attached authorization headers; never extracting stored grants; assets only via JSON routes → Blob URLs, revoked on unmount.
- All mutating operations go through `host.composer.submit` → the agent → plugin commands/scripts. The agent session doing the executing is the same one the user is talking to — same identity, same gating.
- The gateway selector selects **which bot the agent addresses**, not an identity override; the prompt never claims an owner identity the profile doesn't have.
- Scanner hygiene: the install scanner reads every file including the JS — no `sudo` wording, no env dumps, no `file://` access in comments or strings.

**Acceptance:** Written checklist in the plan file's "Validation record" section, each item checked with a grep command and its output.

**Commit:** `chore(desktop): security review notes + fixes`

---

## Task 10: Tests

**Objective:** Automated checks that fit the repo's Python suite conventions.

**Files:**
- Create: `tests/test_desktop_pane.py`

**Steps:**
1. Test the **artifact**, not a browser (no Node test runner in this repo):
   - `desktop/plugin.js`, `desktop/prompts.js`, `desktop/ops-manifest.json`, `dashboard/plugin_api.py`, `dashboard/manifest.json` exist in the repo (and, post-deploy, in the installed tree).
   - Import allowlist: `desktop/plugin.js` import specifiers ⊆ `{@hermes/plugin-sdk, react, react/jsx-runtime}`.
   - Forbidden-pattern scan over `desktop/` and `dashboard/`: `eval(`, `new Function`, `.prototype`, `document.`, `window.__`, `__HERMES_PLUGIN_SDK__`, `data.db`, `PoolService`, `register_command` — zero matches; plus: `plugin_api.py` contains no `@router.post|put|delete` and no ledger-file path strings; `desktop/` contains no raw `fetch(` outside the SDK (allowlist `ctx.rest`/`host.` only).
   - Fake-ctx test (the most valuable one per the testing skill): registered tool names == `provides_tools` (now `[pool_status, pool_report_usage, pool_pane_report]`); commands unchanged.
   - `pool_pane_report` writes only to `plugin_data_dir`/`pane-state.json` and its output round-trips through the `/pane-state` route shape.
2. `desktop/ops-manifest.json` — machine-readable list: `{ kind, command(s), script, fields }` per operation. `prompts.js` builds prompts **from** this manifest (single source of truth); the Python test asserts every manifest entry's command appears in the documented command surface (`pool, fetch, task, add-repo, task_add, task_list, claim, release, push, audit` + the four scripts) and that no entry invents a new verb.
3. `node --check desktop/plugin.js` invoked via `subprocess` in the test, skipped with `pytest.skip` when `node` is absent.

**Run:** `env -u PYTHONNOUSERSITE -u PYTHONPATH /usr/bin/python3.10 -m pytest -q tests/test_desktop_pane.py` → all pass; then the full suite → **392 + N new passing, 0 failures**.

**Commit:** `test(desktop): pane artifact, import allowlist, and ops-manifest parity tests`

---

## Task 11: Validate, deploy, smoke-test

**Steps:**
1. `hermes plugins validate /home/maxcurrent/Documents/AntiGravityProjects/agent-pool --install-deps` — green (watch for scanner findings on the new JS/markdown; reword, don't suppress).
2. `hermes plugins doctor agent-pool --ci` — real runtime contracts green.
3. Deploy: re-run the established rsync so `~/.hermes/plugins/agent-pool/{desktop,dashboard}/` exist and match the repo (diff the trees). Desktop plugins hot-reload; if not visible: ⌘K → *Reload desktop plugins*.
4. **Restart the gateway** (and `hermes dashboard` if running): the new `/api/plugins/agent-pool/` routes do not mount while the prefix-skip sees an old route table. Then `hermes plugins doctor agent-pool --ci` again.
5. Smoke test (manual, recorded in the plan's Validation record): open the pane → select gateway `pool-contrib` → submit a `/fetch repo list` op → the agent executes on that gateway, calls `pool_pane_report`, and the pane's polled tracker shows the outcome; switch to `default`, run `pool_status`, confirm the awaiting-approval view populates. **Do not** exercise approve/merge against a live pool during smoke test.
6. `plugin.yaml` `version`/`requires_hermes` were bumped in Task 4 — verify `plugin-catalog/agent-pool.yaml` matches, and add the pane to the description's surface list ("2+1 tools … desktop Pool Console pane").

**Acceptance:** validate + doctor green; installed tree contains `desktop/plugin.js`; tracker shows a real `fetch repo list` round-trip on the live gateway.

**Commit:** `chore(release): 2.8.0 — desktop Pool Console pane`

---

## Where each piece lives

| Piece | Repo path | Installed path |
|---|---|---|
| Pane/plugin JS | `desktop/plugin.js`, `desktop/prompts.js`, `desktop/track.js`, `desktop/ops-manifest.json` | `~/.hermes/plugins/agent-pool/desktop/…` |
| Read-only backend | `dashboard/manifest.json`, `dashboard/plugin_api.py` | `~/.hermes/plugins/agent-pool/dashboard/…` |
| Agent state tool | `__init__.py` (`pool_pane_report`) | rsynced |
| Manifest/catalog | `plugin.yaml`, `plugin-catalog/agent-pool.yaml` | rsynced |
| Tests | `tests/test_desktop_pane.py` | repo only |
| Docs | README section + `docs/` note | rsynced |

## Remaining gaps / open questions

1. **Verification record for the operator-supplied spec URL.** Fetched `https://raw.githubusercontent.com/mojomast/hermes-plugin-dev/main/skills/hermes-plugin-development/references/panes.md` in full; it is byte-for-byte the same content as the local skill's `references/panes.md` (dashboard vs desktop split, SandboxedFrame, catalog rule 8, session binding, agent↔pane patterns table, isolation:host / route-mount-skip notes, UX lessons). **Two operator message points are NOT present in that file** and are honored here as operator requirements rather than documented contract: (a) the `ctx.rest('/proxy/v1/...')` path and Blob-URL asset rule, (b) the `requires_hermes: ">=0.23.0"` floor tied to "this transport". The plan satisfies both (constraint 5; Task 4 bump mirrored in the catalog entry) but the implementer should re-check the live `desktop-plugin-sdk` doc at build time for the current `ctx.rest` proxy spelling before shipping routes.
2. **`slash.exec` alternative path:** `host.request('slash.exec', { session_id, command })` could run read commands without visibly driving the composer. Deferred — the composer path keeps the user in the loop and is the documented user-visible pattern; switching read-only traffic to `slash.exec` is a cheap follow-up if the transcript noise annoys.
3. **Parsing agent replies:** reply text is prose + embedded JSON; v1 matches known reply shapes (version lines, refusal strings from the skill's "fails closed" list) with tolerant regexes and marks anything unparsed as `review manually` rather than guessing status. The `pool_pane_report` tool makes this deterministic over time (the agent reports structured status directly).
4. **Multi-pool "designated pool" addressing:** the pane's gateway selector covers the two known profiles; adding a pool beyond one-per-gateway needs no pane change (the plugin enforces one pool per gateway) but a free-text "address as told by the maintainer" field is included in add-repo-link.
5. **`when:` visibility:** the pane could hide itself on hosts without the plugin installed; v1 always renders and shows a "plugin not loaded on this profile" empty state from the `/status` route failure.
6. **Kanban cross-link** (`KANBAN_CARD_AREAS.badges` for pool tickets): out of scope; noted as future work.

## Validation record — security review (Task 9)

Reviewed `desktop/plugin.js`, `desktop/prompts.js`, `desktop/track.js` (and read-only `dashboard/plugin_api.py`). All checks pass; no fixes required.

1. **dashboard/plugin_api.py is READ-ONLY** — `grep -n '@router\.' dashboard/plugin_api.py` →
   `40:@router.get("/status")` and `50:@router.get("/pane-state")`. Two GET routes only; no POST/PUT/PATCH/DELETE decorators. Both handlers call the same read path `pool_status` uses (`mod._status_snapshot(None)` / `mod._pane_state_read()` — service-layer/plugin_db reads); no mutating route added. File not modified.
2. **No file reads/writes by the pane; no SQLite from JS** — `grep -nE 'fs|require\(|node:|file://|data\.db|sqlite' desktop/*.js` → no matches (exit 1). State reached only via `ctx.rest('/pane-state')` (plugin.js:945) and `ctx.storage` (ops/snapshot.* keys). Comment in track.js:5 documents the sidecar read path via ctx.rest.
3. **All host API calls via SDK transport** — `grep -niE 'fetch\(|authorization|credential|localStorage|sessionStorage|createObjectURL|revokeObjectURL' desktop/*.js` → only prose matches at plugin.js:590, 631, 659, 1007 (the words "authorization"/"credential"-adjacent text describing the plugin's own authz gate — no actual headers, credentials, or storage). No raw `fetch(`, no `localStorage`/`sessionStorage`, no hand-attached auth. No `ObjectURL`/`createObjectURL`/`Blob` usage anywhere — the pane loads no binary assets, so there is nothing to revoke; the only network-shaped call is `ctx.rest('/pane-state')` (plugin.js:945). `host.notify` (plugin.js:77) and `host.composer.submit` (plugin.js:104, 508, 555, 793, 818) are the only other host channels.
4. **Mutations go through host.composer.submit → agent → plugin commands/scripts** — the five submit sites above are the sole mutation channel; each submits a composed prompt (prompts.js builders or BATCHED_STATUS_PROMPT/auditShowPrompt) naming a literal slash command or script (`/add-repo`, `/fetch`, `/claim`, `/push`, `/audit show`, `scripts/contributor-add.py`, `scripts/contributor-claim.py`, `scripts/contributor-push.py`, `scripts/pool-approve.py`). No other mutation channel exists in the pane (ctx.storage writes are pane-local only; ctx.rest is GET-only).
5. **Gateway selector addresses the bot, not an identity override; no claimed owner identity** — read of prompts.js: every builder names the gateway as addressee (`On the ${gateway} gateway, …`) and only OWNER ONLY labels on approve/merge mark the plugin's gate (`prompts.js:65,71`: "If the plugin refuses because this profile is not the pool owner, report the refusal verbatim and do not retry") — a description of the plugin's gate, never a claimed identity. UI copy (plugin.js:631) likewise states "The pane has no owner check of its own — authorization lives only in the plugin." track.js `auditShowPrompt` (line 163) says "Using the ${gateway} gateway profile" — addressee, not identity claim. No prompt asserts an owner/maintainer identity the profile doesn't have.
6. **Scanner hygiene (desktop/*, dashboard/*)** — `grep -niE 'sudo|AGENT_POOL_|process\.env|file://' desktop/*.js dashboard/*.py dashboard/*.json` → one match: `dashboard/plugin_api.py:34: "agent_pool_plugin", _PLUGIN_ROOT / "__init__.py")` — a path-join for importlib, not a `file://` URL and not an env dump. No `sudo`, no `AGENT_POOL_`, no `process.env` anywhere. No fixes required.

Fixes made: none. Checks re-run after review: all outputs above are the post-review (unchanged-tree) outputs.

## Validation record — Task 11 deploy + validation

Date: 2026-11-10. HEAD at run time: `508c315` (clean tree). Executor: implementer subagent (validate/deploy only; no restarts performed).

### 1. `hermes plugins validate /home/maxcurrent/Documents/AntiGravityProjects/agent-pool --install-deps`
Result: **Validation passed** (all checks green): manifest parses; requires_hermes '>=0.23.0'; loadable (entry __init__.py, desktop/plugin.js); capability probe register() ran in isolation; declared tools match registrations; **security scan — safe**; desktop surface inside SDK surface; isolation: host. **No scanner findings on any new JS/JSON/markdown file; no rewording required, nothing suppressed.**
Caveat: the `--install-deps` resolver reported `error: Two workspace members are both named 'agent-pool'` under `~/.hermes/installs/7655b27799177977/.../workspace/plugin-sources/` (an `uv lock` workspace clash inside the installer's own install dir, unrelated to this repo). Report as `dependency preparation failed`; validation proper passed. Operator may want to `hermes pm doctor` / prune stale install dirs.

### 2. `hermes plugins doctor agent-pool --ci`
- By plugin id: `ERROR: Plugin 'agent-pool' was not found as a path or installed plugin id` — the installed copy at `~/.hermes/plugins/agent-pool` is not registered as an enabled plugin id in this profile (not listed by `hermes plugins list`).
- By path: `hermes plugins doctor /home/maxcurrent/Documents/AntiGravityProjects/agent-pool --ci` → `manifest: agent-pool 2.8.0 (standalone)`; `OK: runtime discovery, manifest parsing, import, and registration passed`; `registrations: 3 tool(s), 0 hook(s)`. **Green by path.**

### 3. Deploy (established rsync, verbatim flags)
`rsync -a --delete -x --exclude=.git --exclude=.hermes --exclude=.pytest_cache --exclude=__pycache__ /home/maxcurrent/Documents/AntiGravityProjects/agent-pool/ ~/.hermes/plugins/agent-pool/`
Then: `diff -rq -x .git -x .hermes -x .pytest_cache -x __pycache__ <repo> ~/.hermes/plugins/agent-pool` → **no output (DIFF-CLEAN)**; `~/.hermes/plugins/agent-pool/desktop/` and `dashboard/` exist and match the repo.

### 4. Restart instruction for the operator (NOT performed by the executor)
The new `/api/plugins/agent-pool/` routes (`/status`, `/pane-state`) do not mount while the gateway's prefix-skip sees the old route table. **Operator action: restart the gateway** (`hermes gateway` — or the gateway embedded in the desktop app), and `hermes dashboard` if it is running. **Post-restart verification:** re-run `hermes plugins doctor agent-pool --ci` (or by path, see §2) and confirm `OK` / `registrations: 3 tool(s)`; optionally confirm the routes respond (`GET /api/plugins/agent-pool/status`, `GET /api/plugins/agent-pool/pane-state`).

### 5. Static verification of smoke-test dependencies (all pass)
- `node --check` (node v22.22.2): `desktop/plugin.js`, `desktop/prompts.js`, `desktop/track.js` — all OK.
- ops-manifest parity: `desktop/ops-manifest.json` commands (`/add-repo`, `/add-repo --deliver`, `/fetch repo list`, `/fetch task list`, `/claim`, `/push repo`, `/push repo --deliver`) are all within `plugin.yaml provides_commands` (pool, fetch, task, add-repo, task_add, task_list, claim, release, push, audit); scripts referenced (`scripts/contributor-add.py`, `contributor-claim.py`, `contributor-push.py`, `pool-approve.py`) all exist. No verb outside the documented surface.
- Route handlers against a tmp `AGENT_POOL_DATA_DIR` sidecar: `/status` → dict with `ok, plugin, version, ...`; `/pane-state` → dict with `ops, pool_status, updated_at`.
- `pool_pane_report` writes the parsePaneState-compatible sidecar: after `{op_id, kind, status, detail}` the sidecar contains `{ops, pool_status, updated_at}` with `ops[0] = {op_id, kind, status, detail, updated_at}`; track.js `parsePaneState` reads `raw.pool_status` / `raw.updated_at` and tolerates the string timestamp (falls back to `Date.now()`). parsePaneState-compatible. ✓
- Static check script: `~/.hermes/profiles/implementer/cache/scratch/task11_static_check.py` → `ALL STATIC CHECKS PASS`.

### 6. Smoke test — AWAITING OPERATOR MANUAL RUN (not performed; needs desktop UI + operator's gateway)
Checklist:
- [ ] Restart gateway (see §4); re-run `hermes plugins doctor` and record result.
- [ ] Open the Pool Console pane in Hermes Desktop (⌘K → Reload desktop plugins if not visible).
- [ ] Select gateway `pool-contrib`; submit a `/fetch repo list` op.
- [ ] Agent on pool-contrib executes, calls `pool_pane_report`; pane's polled tracker shows the outcome.
- [ ] Switch to `default` gateway; run `pool_status`; confirm the awaiting-approval view populates.
- [ ] Do NOT exercise approve/merge against a live pool.

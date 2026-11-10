# Agent Pool — Pool Console UX Redesign (Dropdowns, Buttons, Confirm Gates)

> **For Hermes:** Use subagent-driven-development to implement this plan task-by-task.

**Goal:** Replace the current hand-typed CLI-in-a-wig console (`desktop/plugin.js`, 1365 lines) with an interaction model where **the operator sees lists and buttons; the agent supplies the data; typing is the rare fallback.** Repo discovery replaces the path field. Owner-only flows get confirm cards. The tracker becomes the dashboard, showing picked names (repo/ticket labels), not raw args.

**Architecture (unchanged, re-asserted):** the pane is a SANDBOXED FRAME — no filesystem access, no native file picker, no drag-drop of host paths. Every click composes a prompt and submits it via `host.composer.submit(sessionId, text)` to the bound agent session; the agent executes the plugin command; state returns via the `pool_pane_report` sidecar (`ctx.rest('/pane-state')`) and `pool_status`. **Authorization lives only in the plugin.** All rendered values stay stringified through the existing `str()` guard; `PoolErrorBoundary` stays; imports stay `@hermes/plugin-sdk` + `react`/`react/jsx-runtime` only (zero relative imports — the runtime-loader allowlist killed `prompts.js`/`track.js` once already; the redesign keeps everything inlined in `desktop/plugin.js`).

**Repo:** `/home/maxcurrent/Documents/AntiGravityProjects/agent-pool`. Full suite: `env -u PYTHONNOUSERSITE -u PYTHONPATH /usr/bin/python3.10 -m pytest -q` (412 passing at the pane plan's validation record).

## Hard constraints (every task)

1. **Sandboxed frame.** No `fs`, no native file picker, no drag-drop. "Browse" means: the pane asks the agent to DISCOVER via a composed prompt; the agent replies; the pane stores a snapshot in `ctx.storage` and renders a dropdown. Discovery prompts are the only way a repo path or worktree path enters the system.
2. **Authorization never leaves the plugin.** The pane never calls a mutating route; never inspects the ledger. `dashboard/plugin_api.py` stays GET-only (`/status`, `/pane-state`). Mutations go only through `host.composer.submit`.
3. **Literal command strings.** Every composed prompt keeps the exact command text from the agent-pool skill's command table (as enforced today by `test_ops_manifest_to_prompts_parity` / `test_prompts_to_manifest_parity`). This redesign changes *who supplies the args* (snapshots instead of keyboards), never *what the prompt says* beyond the arg values.
4. **Code-shape contract.** `str()` guard, `PoolErrorBoundary`, import allowlist, single file `desktop/plugin.js`, no `PANES_AREA` dock, console on its own `/agent-pool` page — all unchanged.
5. **Empty states, never blank dropdowns.** Every dropdown has a non-empty option list: at minimum a disabled placeholder option like "No pools loaded yet — click Refresh", plus an "enter manually…" fallback option that reveals a text field.
6. **No new agent tools, no `provides_tools`/`provides_commands` changes.** Discovery reuses the existing command surface; only prompt text and sidecar snapshot keys change. `plugin.yaml` untouched unless a task explicitly says otherwise (none do).

---

## Task 1: Snapshot schema + discovery prompts (the data layer)

**Objective:** Define the snapshots that feed every dropdown and the agent prompts that refresh them, including the **repo discovery** prompt that replaces the path field.

**Files:** Modify `desktop/plugin.js` (new inlined section after the sidecar parsers); extend `tests/test_desktop_pane.py`.

**Steps:**
1. **Snapshot keys in `ctx.storage`** (all with `{ at: <ms>, items: [...] }` shape, defensively parsed — reuse the `parsePaneState` style):
   - `snapshot.repos` — pool repo catalog (already seeded by `/fetch repo list`; now formalized).
   - `snapshot.tasks` — from `/fetch task list` replies / sidecar (exists; formalize `items[] = { ticket_id, repo, title }`).
   - `snapshot.claims` — per-claim records `{ ticket, worktree_path, branch, replyFile }` (exists; keep).
   - `snapshot.discovered` — **new:** local git repos discovered by the agent on the host (below).
   - `snapshot.poolStatus` — existing (awaitingApproval, budgets, transport).
   - `snapshot.gateways` — for the maintainer-gateway pick in the link flow (seeded from `KNOWN_GATEWAYS`, extended by operator settings).
2. **Discovery prompt (the centerpiece):**
   ```js
   export function discoverReposPrompt() {
     return 'Discover local git repositories for the pool console: run `git -C ~/Documents rev-parse --is-inside-work-tree` style discovery — list the git repositories under ~/Documents and ~/projects with their absolute paths (one per line, format: <path> — <first remote or directory name>). Do not open, read, or modify any files inside them; just enumerate the worktrees. Reply with the list.'
   }
   ```
   A **"Discover repos"** button in the Add-repo card submits this prompt (via the standard `submitOp` pipeline, tracked as op kind `discover`). The agent's reply text is captured by the existing refresh loop / `pool_pane_report` sidecar channel (`pool_pane_report` already accepts free-form `detail`; the refresh loop stores the latest reply under `snapshot.discovered`). No filesystem access from the pane, ever — the agent does the walking, the pane only renders the list.
3. **Refresh orchestration:** one `refreshSnapshot(name)` helper; the existing `BATCHED_STATUS_PROMPT` stays; add a `FULL_REFRESH_PROMPT` that asks the agent to run `pool_status`, `/fetch repo list`, `/fetch task list` and call `pool_pane_report` with all results (one click, four data sources, still one literal prompt).
4. Every snapshot read goes through a `readSnapshot(ctx, key)` that returns `[]` on anything malformed (never throws; mirrors `parsePaneState`).

**Acceptance:**
- `node --check desktop/plugin.js` passes; `discoverReposPrompt` contains no filesystem jargon the pane itself executes (it instructs the *agent*).
- Empty-sidecar / malformed-snapshot unit tests in `tests/test_desktop_pane.py` parse to `[]`, no raise.
- Prompt-parity tests still green (discovery prompt names no slash command, so it is outside the manifest parity set; document this in the test file).

**Operator sees:** a "Discover repos" button. Click → a toast → the repo list appears in a dropdown a few seconds later. No typing.

---

## Task 2: Dropdown-fed selectors (replacing every typed id field)

**Objective:** Every identifier the operator currently types becomes a dropdown fed by a snapshot, with named empty states and an explicit manual fallback.

**Files:** Modify `desktop/plugin.js`; extend `tests/test_desktop_pane.py`.

**Steps:**
1. Build one reusable `SnapshotSelect` component: props `{ label, items, value, onChange, emptyHint, refreshAction, allowManual, manualPlaceholder, manualValue, onManualChange }`.
   - Renders a `<select>` (existing `inputClass` styling). Options: placeholder option (disabled when items exist, selectable-to-empty when not) → item options with **labels, not raw ids** where a label exists (`my-repo v3 — awaiting approval`, `ticket 42: fix parser (repo: my-repo)`), else the id.
   - When `items` is empty, the placeholder text IS the empty state: e.g. `No pools loaded yet — click Refresh pools`.
   - A trailing option `(enter manually…)` — only when `allowManual` — flips to a text input prefilled empty. **Typing is opt-in per field, per session** (never persisted as the default).
   - A small inline `↻` refresh button next to the label triggers the snapshot's refresh prompt (`refreshAction`), disabled while an op of that refresh kind is `awaiting agent`.
2. Replace the typed fields:
   | Field (current) | Fed by | Refresh prompt | Empty state text |
   |---|---|---|---|
   | pool selector (fetch-tasks, push pool_id) | `snapshot.repos` + `pool_status.pools` | `fetchRepoListPrompt` / `FULL_REFRESH_PROMPT` | "No pools loaded yet — click Refresh pools" |
   | repo_id (claim filter, push, approve, merge) | `snapshot.repos` | `fetchRepoListPrompt` | "No repos in the pool yet — click Refresh pools or Add repo" |
   | ticket_id (claim, approve, merge, audit) | `snapshot.tasks` | `fetchTaskListPrompt` | "No tasks fetched yet — click Fetch tasks" |
   | claim prefill (pull, push) | op log `kind==='claim'` rows (existing `claimOps`) | — (op log, no prompt) | "No claims yet — claim a ticket first" |
   | awaiting-approval target (approve/merge default) | `snapshot.poolStatus.awaitingApproval` | `FULL_REFRESH_PROMPT` | "Nothing awaiting approval right now" |
   | grant state (push deliver) | parsed grant reply stored on the push op (`args.grantToken`) | — (op-linked) | "Request the grant first (Step 1)" |
   | version (add-repo, push) | agent auto-resolved (Task 3/5) + `--version` manual fallback | — | "Version resolves automatically" |
   | maintainer gateway (link flow) | `snapshot.gateways` | — (static + settings) | defaults to `default` |
3. Labels: extend `repoIdOf`/`ticketIdOf` with `labelFor(entry)` producing `id — title` when a title/repo field exists. Option labels always `str()`-ed.

**Acceptance:**
- Test: inject a fake `snapshot.tasks` (`ctx.storage` fixture) → render helper returns option list containing `ticket 42: fix parser` (test via a pure `optionsFor(snapshot, kind)` function extracted so tests don't need a renderer).
- Test: empty snapshot → options = exactly `[placeholder, '(enter manually…)']` with the empty-hint text in the placeholder label.
- No PANES_AREA / import-allowlist / no-relative-import tests stay green.

**Operator sees:** dropdowns everywhere; each one names what to click if it's empty. Typing only after deliberately choosing "(enter manually…)".

---

## Task 3: Repo add flow WITHOUT typing a path

**Objective:** Owner fast path and contributor link flow become: discover → dropdown → dropdown → (auto version) → one button.

**Files:** Modify `desktop/plugin.js` (rework `AddRepoForm`); extend `tests/test_desktop_pane.py`.

**Steps:**
1. **Owner fast path** (`add-repo-owner`):
   1. "Discover repos" button (Task 1 prompt) → `snapshot.discovered` → **repo dropdown** (labels: `~/projects/my-repo`).
   2. **Target pool dropdown** from `snapshot.repos`/`pool_status` (empty state: "No pools loaded yet — click Refresh pools").
   3. **Version auto-resolved:** the composed prompt already instructs the agent to ask the maintainer gateway `/fetch repo <repo_id>` for `accepted_version` when the pool has versions and no explicit `--version` was given — that instruction in `addRepoOwnerPrompt` stays verbatim; the pane now simply **omits the version field from the default view** and shows a note "version auto-resolves (agent asks /fetch repo for accepted_version)". A manual `--version` field hides behind an "enter manually" disclosure.
   4. One **"Add repo"** button. The path comes from the discovered entry (`args: { repoId, path: discovered.path }`) — the pane never asks for a path and `isAbsoluteLocalPath` validation moves to the discovery parser (a discovered entry that fails the shape check is filtered out of the dropdown, and the pane shows "N entries skipped (non-local paths)").
2. **Contributor link flow** (`add-repo-link`):
   1. Discovered repo dropdown (same as above).
   2. Maintainer gateway profile dropdown (`snapshot.gateways`; labels "default — Maintainer", "pool-contrib — Contributor").
   3. Same one-tracked-op composition as today (`addRepoLinkPrompt` verbatim), rendered with **visible sub-step progress** in the tracker: `build bundle ✓ → deliver ✓` (op `args.substeps = ['build bundle', 'deliver']`; the tracker row renders ✓ per substep once the sidecar reports it — the existing `substatus` string becomes a list).
   4. One **"Contribute repo"** button.
3. The manual path field survives only inside the "(enter manually…)" disclosure of the repo dropdown — inline validation stays `isAbsoluteLocalPath` with the existing "must be absolute / must not look like a URL" message.

**Acceptance:**
- Test: `addRepoOwnerPrompt`/`addRepoLinkPrompt` output unchanged for identical args (golden-string test — parity with the skill is the contract).
- Test: `AddRepoForm` flow-state machine (`{ discoveredId, poolId, manualPath? }`) cannot produce a prompt without a discovered-or-manual path; the composed text contains `/add-repo <repo_id> --path <path>` verbatim.
- Test: non-local-path discovery entries are filtered by `filterDiscovered(items)`.

**Operator sees:** "Discover repos → pick `~/projects/my-repo` → pick pool `my-pool` → Add repo." Zero typing. The two-step link flow shows its two ✓s as it progresses.

---

## Task 4: Action-button model — one primary button per flow + confirm gates

**Objective:** One primary button per operation; Approve/Merge behind a two-step confirm card; free text strictly fallback.

**Files:** Modify `desktop/plugin.js` (all seven forms); extend `tests/test_desktop_pane.py`.

**Steps:**
1. **Primary buttons** (one per flow, replacing today's multi-step button walls):
   - Add repo (Task 3) · **Fetch tasks** · **Claim** · **Pull repo** · **Submit work (push)** · **Approve / Deny** · **Merge**. Each button is disabled with a **reason tooltip** naming the missing dropdown choice ("pick a ticket first").
2. **Push card simplification:** the three sub-step buttons remain (grant → package → deliver are genuinely sequential and each is its own agent prompt), but each input in them is fed by dropdowns/op-linked state (claim dropdown → ticket + worktree prefill from the claim record; grant token auto-filled from the grant op reply when the sidecar carries it; manifest/transfer ticket auto-filled from the package op reply). Only a missing piece surfaces a manual disclosure. **Step 3 gating stays**: deliver cannot fire before grant+package are done (existing logic, kept).
3. **Confirm gates (Approve / Merge only):** a `ConfirmCard` component — after the operator picks targets and presses the primary button, the pane renders a confirm card naming the target in plain words:
   > **Approve `my-repo v3` on gateway `default`?** This asks the agent to run `scripts/pool-approve.py --repo my-repo --version 3`. [Confirm] [Cancel]
   The submit only fires on **Confirm**; Cancel returns to the form. The card shows the literal command string the prompt will carry (so the operator sees what the agent will execute) and the OWNER ONLY banner. Deny keeps its toggle and its own confirm wording.
4. **Inline per-field validation** only where no dropdown can cover: manual path disclosure (`isAbsoluteLocalPath`), manual version (`/^\d+$/`), max-tasks (positive integer), destination dir for pull (absolute). Everything else: no inline errors, just disabled buttons with reason tooltips.
5. **Free-text fallback discipline:** every dropdown-able field's manual disclosure is closed by default and un-checks itself on flow reset; the `(enter manually…)` choice is never persisted.

**Acceptance:**
- Test: `buildConfirmCard(target, gateway, commandText)` returns text containing the repo/version/ticket names and the literal command; Approve/Merge op composition requires `confirmed === true` (pure function `finalizeOp(draft, confirmState)` returns null without confirm).
- Test: every one of the 7 primary flows has exactly one primary button label in the source (grep-level assertion on the button-label constants table).
- Confirm-gate test: finalize with `confirmState.confirmed=false` → null op; with true → the composed prompt.

**Operator sees:** a form with two dropdowns and one button. Press → a card naming exactly what will happen → Confirm. Owner actions feel like approvals, not command entry.

---

## Task 5: Tracker as the operational dashboard

**Objective:** The tracker leads the page; rows show what was picked; details fold carries the agent's verbatim reply; Retry/Redeliver where legal.

**Files:** Modify `desktop/plugin.js` (move `TrackerSection` to the top of `buildPaneBody`, rework `opRow`); extend `tests/test_desktop_pane.py`.

**Steps:**
1. Move the tracker above the flow cards (dashboard-first ordering). Tabs stay: `Active / Awaiting approval / Claims & budgets / Audit` (SegmentedControl).
2. **Rich rows:** `opRow` renders a **what-was-picked summary** from `args`: e.g. `push · my-repo v3 · ticket 42 · gateway pool-contrib · 12:04` — human names from snapshot lookups (repo title from `snapshot.repos`, ticket title from `snapshot.tasks`), falling back to raw ids, always `str()`-ed. Substep lists render as `build bundle ✓ → deliver ✓`.
3. **Details fold:** a per-row collapsed `<details>`-style fold (a toggled div — sandbox-safe) containing the agent's **verbatim reply** (`op.reply` — populated by the refresh loop from the sidecar's per-op `detail`; keep last reply per op id) plus the composed prompt text. Default collapsed; "review manually" marker when the reply couldn't be parsed to a status.
4. **Retry/Redeliver where legal:**
   - `push` deliver failure → **Retry deliver** (existing `retryDeliver` logic, now surfaced on the failed row itself, with the existing "grant not consumed" tooltip).
   - Any op `failed: composer unreachable` → **Retry** re-submits the exact same `op.text`.
   - Refused rows get **no retry button** (a plugin refusal is not transient) — instead a hint: "read the refusal; fix the cause, then re-run the flow."
5. Awaiting-approval tab stays sourced **only** from `snapshot.poolStatus.awaitingApproval`; each entry gets an inline **Approve / Deny** pair that deep-links into the Approve confirm card pre-filled (still confirm-gated, Task 4).
6. Audit tab: ticket becomes a dropdown fed by `snapshot.tasks` (`(all tickets)` default option) + Run button (existing `auditShowPrompt` verbatim).

**Acceptance:**
- Test: `summarizeOp(op, snapshotLookup)` returns the human string for a push op with repo/ticket labels and falls back to raw ids when lookups miss.
- Test: retry-legal mapping: `{push, failed}` → retry allowed; `{approve, refused}` → no retry action.
- Test: awaiting tab data source is `parsed.awaitingApproval` only (existing test tightened: an op in the log never creates an awaiting row).

**Operator sees:** the page opens on the dashboard. "push · my-repo v3 · ticket 42" with a green pill — click to unfold exactly what the agent said back.

---

## Task 6: Empty / loading / error states everywhere

**Objective:** First-run never shows a wall of dead forms.

**Files:** Modify `desktop/plugin.js`; extend `tests/test_desktop_pane.py`.

**Steps:**
1. **First-run state:** when no snapshot has ever landed (`!snapshot.poolStatus && !snapshot.repos.length && !ops.length`), the flow cards collapse into a single onboarding card: "Welcome to your Pool Console — ① Click **Refresh pools** to load your pools, repos and approval queue ② (optional) **Discover repos** to index local git worktrees. Everything after that is dropdowns and buttons." The Refresh/Discover buttons are inside the card.
2. **Loading:** each refresh action sets a per-snapshot `loading` flag (existing `refreshing` generalized); the affected dropdown shows `Loading…` as its disabled placeholder; the tracker Refresh button keeps its existing `Refreshing…` state.
3. **Errors:** `ctx.rest('/pane-state')` failure keeps today's silent-null behavior but now also surfaces a one-line strip: "sidecar unreachable — state may be stale (last update: <ts>)". Composer-unreachable keeps the existing failed-op + toast.
4. Toasts on every op submission stay (`notifyOp`, untouched). Refusal text surfaced verbatim on the row stays (Task 5's fold makes it visible).
5. All of this rides the existing theme variables; no new colors.

**Acceptance:**
- Test: `firstRun(ctx)` predicate true on an empty storage, false once any snapshot/ops key exists.
- Test: stale-strip string renders `sidecar unreachable` + last timestamp; absent when a snapshot exists.

**Operator sees:** on first open, one card telling them the two buttons to press — not seven forms of empty inputs.

---

## Task 7: Tests — extend `tests/test_desktop_pane.py`

**Objective:** All UX-model guarantees enforced at the artifact level (no browser, per repo convention).

**Steps (new tests; all existing ones must stay green — especially `test_plugin_js_import_allowlist`, `test_plugin_js_no_relative_imports`, `test_ops_manifest_to_prompts_parity`, `test_prompts_to_manifest_parity`, `test_artifacts_exist_in_repo`, `test_node_check_js_files`):**
1. `test_discovery_prompt_exists_and_agent_scoped` — `discoverReposPrompt()` mentions `~/Documents` and `~/projects`, and plugin.js contains no fs/drag-drop patterns (forbidden-pattern scan extended with `FileReader|DataTransfer|drop`).
2. `test_dropdown_options_from_snapshot` — `optionsFor()` on a fake snapshot yields labels; on empty yields placeholder + manual-fallback with the empty-hint.
3. `test_primary_button_per_flow` — button-label constants table has exactly one primary entry per op kind in `{add-repo-owner, add-repo-link, fetch-tasks, claim, pull, push, approve, merge, discover}`.
4. `test_confirm_gate_on_owner_actions` — finalize without confirm → null; with confirm → prompt containing the literal command; applies to `approve` and `merge` kinds only.
5. `test_prompt_golden_strings` — every existing prompt builder's output for fixed args is byte-stable (guards the parity contract through the refactor).
6. `test_first_run_and_stale_state` — predicates per Task 6.
7. `test_retry_legality` — retry-allowed map per Task 5.
8. Run: targeted file → full suite; expect **412 + N new passing, 0 failures**.

**Acceptance:** full suite green; `node --check desktop/plugin.js` green.

---

## Task 8: Validation record (no restarts, no pushes)

**Steps:**
1. `hermes plugins validate <repo>` green; `hermes plugins doctor <repo path> --ci` green (by path, per the known by-id gap).
2. Grep the security checklist from the original plan's Task 9 (GET-only routes, no raw fetch, no fs, mutations only via `host.composer.submit`, gateway-as-addressee) and re-record outputs in this file's Validation record section.
3. **Do not** rsync/restart/smoke-test — the parent operator owns deploy. List the manual smoke checklist (open pane → Refresh pools → Discover repos → Add repo via dropdowns → push flow → confirm gate) for the operator.

**Acceptance:** validate/doctor outputs recorded; no deploy actions taken.

---

## What stays typed (the honest list)

- **(enter manually…) disclosures only:** raw ids the snapshots can't name yet, an explicit `--version`, the pull destination directory, the claimant identity note, the merge canonical-checkout override (prefilled `work/integrate/<repo_id>/`).
- Everything else — repo paths (discovery), pool/repo/task/ticket/claim/approval/grant/gateway selections (snapshots + op log) — is buttons and dropdowns.

## Key UX decisions

1. **Repo discovery replaces the path field.** The pane cannot touch the filesystem, so "Browse" = a tracked `discover` op whose prompt tells the agent to enumerate git worktrees under `~/Documents` and `~/projects` (paths + names only); the reply lands in `snapshot.discovered` and renders as the repo dropdown. `isAbsoluteLocalPath` filtering moves to the discovery parser; manual path entry survives only behind the disclosure.
2. **Version auto-resolution moves off the form.** The `--version` field disappears from the default view; the prompt's existing "ask the maintainer gateway `/fetch repo <id>` for accepted_version" instruction does the work. Explicit version hides behind the manual disclosure (fresh chain state always from the maintainer gateway — skill rule honored).
3. **Confirm gates only on owner-only actions** (Approve/Merge): a card naming the target + the literal command before Confirm. Contributor flows stay one-click — the plugin's refusals are the safety net there, surfaced verbatim on the row.
4. **Dashboard-first ordering:** tracker on top, flows below; rows show picked names with a details fold for the verbatim agent reply.
5. **Retry only where legal:** redeliver (grant not consumed) and composer-unreachable retries; plugin refusals get guidance, never an auto-retry.
6. **Single-file contract preserved:** everything stays inlined in `desktop/plugin.js` (the runtime-loader allowlist lesson from the previous plan's §7); new pure helpers (`optionsFor`, `summarizeOp`, `finalizeOp`, `firstRun`) are exported for the Python artifact tests.

---

## Validation record — console UX redesign (Task 8)

Date: 2026-11-10. HEAD at run time: `5ccc742`. Executor: implementer subagent (validation only; **no deploy actions taken** — no rsync, no restart, no smoke test, no push, no ledger touch). Note: `.hermes/plans/2026-11-10_agent-pool-desktop-pane.md` carries a pre-existing uncommitted modification (Task 7 addendum) that is NOT part of this record and was left untouched.

### 1. `hermes plugins validate /home/maxcurrent/Documents/AntiGravityProjects/agent-pool`
Result: **Validation passed** (exit 0). All checks green: manifest parses; requires_hermes '>=0.23.0'; loadable (entry __init__.py, desktop/plugin.js); python deps resolved; capability probe ran in isolation; declared tools match registrations; **security scan — safe**; desktop surface inside the plugin SDK surface; isolation: host. Note: the validate run also performed a source-update pass on the host (v0.23.0, "Code updated!") before validating — an inherent side effect of the command as-invoked, recorded here for honesty; no plugin files were modified by it. No scanner findings; nothing reworded, nothing suppressed.

### 2. `hermes plugins doctor /home/maxcurrent/Documents/AntiGravityProjects/agent-pool --ci` (by path)
Result: **green** (exit 0) — `manifest: agent-pool 2.8.0 (standalone)`; `OK: runtime discovery, manifest parsing, import, and registration passed`; `registrations: 3 tool(s), 0 hook(s)`. (By-id lookup reports not-found in this profile context — known gap; not chased.)

### 3. Security checklist re-run (from the original pane plan's Task 9)

**(a) dashboard/plugin_api.py has only GET route decorators** — `grep -n '@router\.' dashboard/plugin_api.py` →
```
40:@router.get("/status")
50:@router.get("/pane-state")
```
Two GET routes only; no POST/PUT/PATCH/DELETE. PASS.

**(b) No raw fetch with credentials / hand-attached authorization headers in desktop/plugin.js (only ctx.rest/host.* transports)** — `grep -niE 'fetch\(|authorization|credential|localStorage|sessionStorage|createObjectURL' desktop/plugin.js` → three prose matches only (plugin.js:1599, 1699, 1776 — the words describing the plugin's own authz gate / "authorization lives only in the plugin"); **zero** raw `fetch(`, zero `localStorage`/`sessionStorage`, zero hand-attached auth. Transport call sites: `grep -nE 'ctx\.rest|host\.' desktop/plugin.js` → `host.composer.submit` (lines 430, 715, 1428, 1476, 2034, 2041, 2107, 2132), `host.notify` (687), `ctx.rest('/pane-state')` (2333), plus `host.state.focusedSessionId`/`host.state.gateway` reads (2302–2303). PASS.

**(c) No filesystem access patterns in desktop/plugin.js (incl. FileReader|DataTransfer|createObjectURL|drop)** — `grep -niE 'FileReader|DataTransfer|createObjectURL|drop|fs\b|require\(|node:|file://' desktop/plugin.js` → matches only inside the word "dropdown" (lines 678, 763, 858, 2183) and prose (1439); **zero** FileReader/DataTransfer/createObjectURL/require/node:/file:///fs usage. PASS.

**(d) All mutating ops go through host.composer.submit** — `grep -c 'host.composer.submit' desktop/plugin.js` → **8** call sites (lines 430, 715, 1428, 1476, 2034, 2041, 2107, 2132). No other mutation channel exists (ctx.rest is GET-only; ctx.storage writes are pane-local). PASS.

**(e) Gateway selector selects the addressee bot, never claims an owner identity** — `grep -n 'On the ${gateway} gateway' desktop/plugin.js` → every prompt builder addresses the gateway as addressee, e.g. line 576: `On the ${gateway} gateway, list the pools and repos: run /fetch repo list.` and line 616 (approve): `On the ${gateway} gateway, OWNER ONLY: run scripts/pool-approve.py … If the plugin refuses because this profile is not the pool owner, report the refusal verbatim and do not retry.` The OWNER ONLY label describes the plugin's gate, never a claimed identity — UI copy line 1699: `OWNER ONLY. The pane has no owner check of its own — authorization lives only in the plugin. A non-owner profile still submits; the plugin refuses and the pane records the refusal verbatim as op status refused.` PASS.

**(f) Scanner hygiene across desktop/ + dashboard/: no sudo, no env dumps, no file://** — `grep -niE 'sudo|process\.env|file://|AGENT_POOL_|env dump' desktop/plugin.js dashboard/*.py dashboard/*.json` → one match: `dashboard/plugin_api.py:34: "agent_pool_plugin", _PLUGIN_ROOT / "__init__.py")` — a path-join for importlib, not a `file://` URL and not an env dump. No `sudo`, no `process.env`, no `AGENT_POOL_` anywhere. PASS.

### 4. MANUAL SMOKE CHECKLIST — AWAITING THE OPERATOR (not performed by the executor)
No deploy has happened as part of this record. When the operator is ready (after the parent's deploy step):
- [ ] Reload desktop plugins (⌘K → Reload desktop plugins) and open the Agent Pool page.
- [ ] First-run onboarding card renders (empty state with Refresh pools / Discover repos guidance).
- [ ] Click **Refresh pools** — pools/repos/approval queue populate via the tracked refresh op.
- [ ] Click **Discover repos** — the discover op is tracked and the discovered list lands in the repo dropdown.
- [ ] Pick a repo + pool in **Add repo** and submit — watch the tracked op row and the reply fold (verbatim agent reply).
- [ ] Claim flow from a fetched task — pick task → claim → observe tracked op + reply fold.
- [ ] Push flow — three substeps (grant → package → deliver), each tracked.
- [ ] **Approve/Merge confirm card** — click Cancel first, then re-open and Confirm; observe that no op composes without Confirm.
- [ ] Refused-row hint inspection — trigger/observe a plugin refusal row and confirm the guidance hint renders verbatim.

**Do NOT exercise approve/merge against a live pool during smoke testing.** Cancel paths only; Confirm on Approve/Merge stays untested against live state until the operator explicitly chooses to.

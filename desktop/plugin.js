// Agent Pool — Hermes Desktop plugin ("Pool Console").
//
// The console lives on its own full page at /agent-pool, reachable from the left
// sidebar nav row (no right-dock pane). The desktop host loads this file UNCOMPILED,
// so there is no JSX syntax here — every element is built with jsx()/jsxs()
// from 'react/jsx-runtime'.
//
// Allowed import specifiers (SDK rule): '@hermes/plugin-sdk', 'react' and
// 'react/jsx-runtime'. Nothing else resolves.
//
// Styling: the app's own theme tokens only (--ui-text-primary /
// --ui-text-secondary / --ui-text-tertiary), so light and dark mode both
// come out of the host's palette. No literal colours.

import { host, ListRow, ROUTES_AREA, SegmentedControl, SIDEBAR_NAV_AREA, ToggleRow, useValue } from '@hermes/plugin-sdk'
import { Component, useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

// Every dynamic value rendered as text goes through str(): null/undefined →
// 'none', strings pass through, objects stringify (JSON, with an id fallback).
// Anything less invites React error #31 (object as a React child).
function str(v) {
  if (v == null) return 'none'
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (typeof v === 'object') {
    if (typeof v.id === 'string' || typeof v.id === 'number') return String(v.id)
    try { return JSON.stringify(v) } catch { return String(v) }
  }
  return String(v)
}
// --- inlined from track.js (runtime import allowlist: no relative imports) ---
// Agent Pool — tracker logic (pure, no SDK imports).
//
// Pane-local operation log. NOT the ledger: everything here lives in
// ctx.storage under 'ops' / 'snapshot.*' and is derived from agent replies
// and the pool_pane_report sidecar (read via ctx.rest('/pane-state')).
//
// Op kinds: {add-repo-owner, add-repo-link, fetch-tasks, claim, pull, push,
// approve, merge}. Older builds wrote 'add-repo' — migrateOps() upgrades
// those records in place so prior ops survive.

export const OP_KINDS = ['add-repo-owner', 'add-repo-link', 'add-task', 'fetch-tasks', 'claim', 'pull', 'push', 'approve', 'merge', 'prune']

export const OPS_KEY = 'ops'
export const OPS_KEEP = 200

const KIND_MIGRATION = {
  'add-repo': 'add-repo-owner'
}

export function normalizeKind(kind) {
  if (typeof kind !== 'string') return kind
  return KIND_MIGRATION[kind] || kind
}

// Migrate stored ops in place (called on hydrate). Returns { ops, changed }.
export function migrateOps(stored) {
  const list = Array.isArray(stored) ? stored : []
  let changed = false
  const ops = list.map(op => {
    if (!op || typeof op !== 'object') return op
    const kind = normalizeKind(op.kind)
    if (kind !== op.kind) {
      changed = true
      return { ...op, kind }
    }
    return op
  })
  return { ops, changed }
}

export function isAwaitingApprovalStatus(status) {
  return status === 'done (awaiting approval)'
}

export function isActiveStatus(status) {
  return status === 'submitted' || status === 'awaiting agent'
}

// --- sidecar parsing (defensive: empty/missing never throws) ----------------

export function parsePaneState(raw) {
  const empty = { poolStatus: null, tasks: null, claims: null, discovered: null, awaitingApproval: [], budgets: [], at: null, substeps: null, opReplies: {}, listsError: null, listsWarning: null, worktrees: null, tor: null, raw: null }
  if (!raw || typeof raw !== 'object') return empty
  const ps = raw.pool_status || raw.poolStatus || raw.status || null
  const iso = typeof raw.updated_at === 'string' ? Date.parse(raw.updated_at) : NaN
  const at = Number.isFinite(iso) ? iso : (typeof raw.at === 'number' && Number.isFinite(raw.at) ? raw.at : null)
  const parsed = { ...empty, at }
  if (ps && typeof ps === 'object') {
    parsed.poolStatus = ps
    parsed.awaitingApproval = extractAwaitingApproval(ps)
    parsed.budgets = extractBudgets(ps)
  }
  const tasks = raw.tasks || raw.task_list || null
  if (tasks) parsed.tasks = tasks
  const repos = raw.repos || raw.repo_list || null
  if (repos) parsed.repos = repos
  const claims = raw.claims || null
  if (claims) parsed.claims = claims
  // (No 'discovered' handling: the discover op and snapshot.discovered are gone.)
  // Sub-step progress reported by the sidecar (e.g. link flow:
  // substeps: ['build bundle', 'deliver']). A flat string list.
  const substeps = raw.substeps || raw.sub_steps || null
  if (Array.isArray(substeps)) parsed.substeps = substeps.map(s => str(s)).filter(s => s && s !== 'none')
  // Op-linked auto-fill payloads for the push card (grant reply / package reply).
  const grantToken = raw.grant_token || raw.grantToken || null
  if (grantToken) parsed.grantToken = str(grantToken)
  const transferTicket = raw.transfer_ticket || raw.transferTicket || null
  if (transferTicket) parsed.transferTicket = str(transferTicket)
  const manifest = raw.manifest || raw.manifest_json || null
  if (manifest) parsed.manifest = typeof manifest === 'string' ? manifest : JSON.stringify(manifest)
  // Per-op verbatim replies from the sidecar: either an id→reply map object
  // or a list of { id, reply|detail|text } entries. Last write wins per id.
  const repliesRaw = raw.op_replies || raw.opReplies || raw.replies || raw.op_details || null
  const opReplies = {}
  if (repliesRaw && typeof repliesRaw === 'object' && !Array.isArray(repliesRaw)) {
    for (const [k, v] of Object.entries(repliesRaw)) {
      if (v == null) continue
      opReplies[String(k)] = typeof v === 'string' ? v : str((v && (v.reply || v.detail || v.text)) ?? JSON.stringify(v))
    }
  } else if (Array.isArray(repliesRaw)) {
    for (const entry of repliesRaw) {
      if (!entry || typeof entry !== 'object') continue
      const id = entry.id ?? entry.op_id ?? entry.opId
      const reply = entry.reply ?? entry.detail ?? entry.text
      if (id != null && reply != null) opReplies[String(id)] = str(reply)
    }
  }
  parsed.opReplies = opReplies
  const worktreePath = raw.worktree_path || raw.worktreePath || null
  if (worktreePath) parsed.worktreePath = str(worktreePath)
  if (raw.lists_error) parsed.listsError = str(raw.lists_error)
  if (raw.lists_warning) parsed.listsWarning = str(raw.lists_warning)
  if (Array.isArray(raw.worktrees)) parsed.worktrees = raw.worktrees
  if (ps && typeof ps === 'object' && ps.tor && typeof ps.tor === 'object') parsed.tor = ps.tor
  if (raw.error) parsed.error = str(raw.error)
  return parsed
}

function asArray(v) { return Array.isArray(v) ? v : [] }

export function extractAwaitingApproval(ps) {
  if (!ps || typeof ps !== 'object') return []
  const direct = ps.awaiting_approval || ps.awaitingApproval || ps.pending_approval || ps.pending
  const out = []
  for (const entry of asArray(direct)) {
    if (entry == null) continue
    if (typeof entry === 'string') { out.push({ id: entry, raw: entry }); continue }
    if (typeof entry === 'object') {
      out.push({
        id: String(entry.repo_id || entry.repo || entry.ticket_id || entry.ticket || entry.id || entry.version || ''),
        repo: entry.repo_id || entry.repo || null,
        ticket: entry.ticket_id || entry.ticket || null,
        version: entry.version != null ? entry.version : null,
        raw: entry
      })
    }
  }
  return out.filter(e => e.id)
}

// Budget lines: entries mentioning an allocation/used/exhausted shape, or a
// plain string line containing 'budget'.
export function extractBudgets(ps) {
  if (!ps || typeof ps !== 'object') return []
  const direct = ps.budgets || ps.budget || ps.agent_budgets
  const out = []
  if (Array.isArray(direct)) {
    for (const entry of direct) {
      if (entry == null) continue
      if (typeof entry === 'string') { out.push({ line: entry, exhausted: /exhaust|refus/i.test(entry) }); continue }
      if (typeof entry === 'object') {
        const used = Number(entry.used ?? entry.completed ?? entry.spent ?? NaN)
        const alloc = Number(entry.allocation ?? entry.max ?? entry.limit ?? NaN)
        const exhausted = entry.exhausted === true || (Number.isFinite(used) && Number.isFinite(alloc) && alloc > 0 && used >= alloc)
        out.push({ ...entry, line: String(entry.line || entry.agent || entry.key || 'budget'), exhausted })
      }
    }
    return out
  }
  // Free-text fallback: scan top-level string fields for budget mentions.
  for (const value of Object.values(ps)) {
    if (typeof value === 'string' && /budget/i.test(value)) {
      out.push({ line: value, exhausted: /exhaust|refus/i.test(value) })
    }
  }
  return out
}

function opMatchesAwaiting(op, entry) {
  const args = (op && op.args) || {}
  const candidates = [args.repoId, args.repo, args.ticketId, args.ticket, args.key].filter(Boolean).map(String)
  if (!candidates.length) return false
  const ids = [entry.repo, entry.ticket, entry.id].filter(Boolean).map(String)
  if (ids.some(id => candidates.includes(id))) return true
  // version-only match for repo pushes
  if (entry.version != null && args.version != null && String(entry.version) === String(args.version) && entry.repo && candidates.includes(String(entry.repo))) return true
  return false
}

// Apply a parsed pane-state to the op list. Returns { ops, changed }.
// Flip rules:
//  - push/add-* op whose repo/ticket appears in awaiting_approval
//      → 'done (awaiting approval)'
//  - claim op when any budget line is exhausted → 'refused'
export function applyPaneStateToOps(ops, parsed) {
  const list = Array.isArray(ops) ? ops : []
  if (!parsed) return { ops: list, changed: false }
  const awaiting = asArray(parsed.awaitingApproval)
  const budgetsExhausted = asArray(parsed.budgets).some(b => b && b.exhausted)
  let changed = false
  const reportedSubsteps = asArray(parsed.substeps).map(String)
  const next = list.map(op => {
    if (!op || typeof op !== 'object') return op
    // Merge sidecar-reported sub-steps into any op that declares a substeps list.
    if (reportedSubsteps.length && op.args && Array.isArray(op.args.substeps) && isActiveStatus(op.status)) {
      const merged = [...new Set([...asArray(op.args.substepsDone).map(String), ...reportedSubsteps])]
      if (merged.length !== asArray(op.args.substepsDone).length) {
        changed = true
        op = { ...op, args: { ...op.args, substepsDone: merged } }
      }
    }
    // Merge sidecar-reported auto-fill payloads into active push/claim ops.
    const autoFill = {}
    if (parsed.grantToken) autoFill.grantToken = str(parsed.grantToken)
    if (parsed.transferTicket) autoFill.transferTicket = str(parsed.transferTicket)
    if (parsed.manifest) autoFill.manifest = str(parsed.manifest)
    if (parsed.worktreePath && op.kind === 'claim') autoFill.worktreePath = str(parsed.worktreePath)
    if (Object.keys(autoFill).length && isActiveStatus(op.status)
        && ((op.kind === 'push' && !autoFill.worktreePath) || (op.kind === 'claim' && autoFill.worktreePath))) {
      changed = true
      op = { ...op, args: { ...op.args, ...autoFill } }
    }
    if (op.status === 'done' || op.status === 'failed') return op
    let status = op.status
    if ((op.kind === 'push' || op.kind === 'add-repo-owner' || op.kind === 'add-repo-link')
        && awaiting.some(entry => opMatchesAwaiting(op, entry))) {
      status = 'done (awaiting approval)'
    } else if (op.kind === 'claim' && budgetsExhausted && isActiveStatus(op.status)) {
      status = 'refused'
    }
    if (status !== op.status) {
      changed = true
      return { ...op, status, replyRef: parsed.at || op.replyRef || null }
    }
    return op
  })
  return { ops: next, changed }
}

// Merge sidecar-reported per-op replies into the op list. Last reply per op
// id wins; ops not mentioned are untouched. Pure.
export function applyOpReplies(ops, opReplies) {
  const list = Array.isArray(ops) ? ops : []
  const replies = opReplies && typeof opReplies === 'object' ? opReplies : {}
  const ids = Object.keys(replies)
  if (!ids.length) return { ops: list, changed: false }
  let changed = false
  const next = list.map(op => {
    if (!op || typeof op !== 'object' || op.id == null) return op
    const reply = replies[String(op.id)]
    if (reply == null) return op
    if (op.reply === reply) return op
    changed = true
    return { ...op, reply: str(reply) }
  })
  return { ops: next, changed }
}

// summarizeOp(op, snapshotLookup): the what-was-picked one-liner for a row.
// Human names come from snapshot lookups (repo title from snapshot.repos,
// ticket title from snapshot.tasks), falling back to raw ids. Always str()-ed.
// snapshotLookup = { repos: [...], tasks: [...] } (plain item arrays).
export function summarizeOp(op, snapshotLookup) {
  if (!op || typeof op !== 'object') return ''
  const lookup = snapshotLookup && typeof snapshotLookup === 'object' ? snapshotLookup : {}
  const repos = asArray(lookup.repos)
  const tasks = asArray(lookup.tasks)
  const args = op.args && typeof op.args === 'object' ? op.args : {}
  const parts = [str(op.kind || 'op')]
  const repoId = args.repoId ?? args.repo ?? args.repo_id ?? null
  if (repoId != null && str(repoId) !== 'none') {
    const hit = repos.find(r => r && typeof r === 'object' && str(r.repo_id ?? r.repo ?? r.id ?? r.name ?? '') === str(repoId))
    const name = hit ? str(hit.title || hit.name || hit.repo_id || hit.repo || hit.id || repoId) : str(repoId)
    const version = args.version != null ? args.version : (hit && hit.version != null ? hit.version : null)
    parts.push(version != null ? `${name} v${str(version)}` : name)
  }
  // add-task ops carry a structured payload: show the title (and pool) so the
  // row reads like a sentence instead of raw ids.
  if (op.kind === 'add-task') {
    if (args.title != null && str(args.title) !== 'none') parts.push(`"${str(args.title)}"`)
    if (args.poolId != null && str(args.poolId) !== 'none') parts.push(`pool ${str(args.poolId)}`)
  }
  const ticketId = args.ticketId ?? args.ticket ?? args.key ?? null
  if (ticketId != null && str(ticketId) !== 'none') {
    const hit = tasks.find(t => t && typeof t === 'object' && str(t.ticket_id ?? t.ticket ?? t.id ?? '') === str(ticketId))
    const title = hit ? (hit.title || hit.name || null) : null
    parts.push(title ? `ticket ${str(ticketId)}: ${str(title)}` : `ticket ${str(ticketId)}`)
  }
  if (op.gateway && str(op.gateway) !== 'none') parts.push(`gateway ${str(op.gateway)}`)
  if (op.at) {
    const d = new Date(op.at)
    if (!Number.isNaN(d.getTime())) {
      parts.push(`${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`)
    }
  }
  return parts.join(' · ')
}

// --- ctx.storage snapshot schema (defensive: malformed reads return []) -----
// Every snapshot lives under a 'snapshot.*' key as { at: <ms>, items: [...] }.
// Snapshots are a cache of the plugin's local read of its own state — never a
// source of truth, rewritten on every /pane-state refresh.
export const SNAPSHOT_KEYS = ['snapshot.repos', 'snapshot.tasks', 'snapshot.claims', 'snapshot.poolStatus', 'snapshot.gateways']

// readSnapshot(ctx, key): returns the snapshot's items array, or [] on
// anything malformed (missing key, wrong shape, non-array items). Never throws.
export function readSnapshot(ctx, key) {
  try {
    const snap = ctx && ctx.storage ? ctx.storage.get(key, null) : null
    if (!snap || typeof snap !== 'object') return []
    if (Array.isArray(snap)) return snap
    if (Array.isArray(snap.items)) return snap.items
    if (Array.isArray(snap.table)) return snap.table
    if (Array.isArray(snap.repos)) return snap.repos
    if (Array.isArray(snap.tasks)) return snap.tasks
    if (Array.isArray(snap.claims)) return snap.claims
    return []
  } catch {
    return []
  }
}

// --- first-run predicate + stale-sidecar strip (pure; testable) ----------
// firstRun(storage): true when no snapshot has ever landed and no op was ever
// submitted. Accepts a ctx-like object ({ storage }), a storage-like object
// ({ get }), or a plain object map of keys. Never throws.
export function firstRun(source) {
  try {
    const store = source && typeof source === 'object' && source.storage && typeof source.storage.get === 'function'
      ? source.storage
      : (source && typeof source === 'object' && typeof source.get === 'function' ? source : null)
    const get = (k, dflt) => {
      if (store) return store.get(k, dflt)
      return source && typeof source === 'object' && k in source ? source[k] : dflt
    }
    const ps = get('snapshot.poolStatus', null)
    const hasPoolStatus = ps && typeof ps === 'object' && (ps.poolStatus || ps.at != null || (Array.isArray(ps.items) && ps.items.length > 0))
    const repos = get('snapshot.repos', null)
    const hasRepos = Array.isArray(repos) ? repos.length > 0 : Boolean(repos && Array.isArray(repos.items) && repos.items.length > 0)
    const ops = get('ops', null)
    const hasOps = Array.isArray(ops) && ops.length > 0
    return !(hasPoolStatus || hasRepos || hasOps)
  } catch {
    return false
  }
}

// staleStripText(lastAt): the one-line strip shown when /pane-state is
// unreachable. Pure — the renderer decides when to show it (sidecar down AND
// no snapshot exists).
export function staleStripText(lastAt) {
  const ts = typeof lastAt === 'number' && Number.isFinite(lastAt) ? new Date(lastAt).toISOString() : str(lastAt ?? 'never')
  return `sidecar unreachable — state may be stale (last update: ${ts})`
}

// --- per-snapshot loading flags (generalizes the tracker's `refreshing`) ---
const loadingState = { map: {}, listeners: new Set() }
export function setSnapshotLoading(name, on) {
  loadingState.map = { ...loadingState.map, [name]: Boolean(on) }
  for (const listener of [...loadingState.listeners]) listener()
}
export function snapshotLoading(name) {
  return Boolean(loadingState.map[name])
}
function clearSnapshotLoading() {
  loadingState.map = {}
  for (const listener of [...loadingState.listeners]) listener()
}
function useSnapshotLoading(name) {
  const [, force] = useState(0)
  useEffect(() => {
    if (!name) return undefined
    const listener = () => force(v => v + 1)
    loadingState.listeners.add(listener)
    return () => { loadingState.listeners.delete(listener) }
  }, [name])
  return name ? snapshotLoading(name) : false
}

// --- SnapshotSelect support (pure option builders; testable without a renderer)
// labelFor(entry): 'id: title' when a title exists, 'id — repo/version' when a
// repo/version exists, otherwise the bare id. Always str()-ed.
export function labelFor(entry) {
  if (entry == null) return ''
  if (typeof entry === 'string' || typeof entry === 'number') return str(entry)
  const id = entry.ticket_id ?? entry.ticket ?? entry.repo_id ?? entry.repo
    ?? entry.pool_id ?? entry.pool ?? entry.id ?? entry.name ?? ''
  const title = entry.title || entry.name || null
  if (title && str(title) !== str(id)) return `${str(id)}: ${str(title)}`
  const repo = entry.repo
  if (repo && str(repo) !== str(id)) return `${str(id)} — ${str(repo)}`
  if (entry.version != null && str(entry.version) !== str(id)) return `${str(id)} — v${str(entry.version)}`
  return str(id)
}

export const SNAPSHOT_KIND_EMPTY = {
  pools: 'No pools loaded yet — click Refresh pools',
  repos: 'No repos in the pool yet — click Refresh pools or Add repo',
  tasks: 'No tasks fetched yet — click Fetch tasks',
  claims: 'No claims yet — claim a ticket first',
  awaiting: 'Nothing awaiting approval right now',
  grant: 'Request the grant first (Step 1)',
  version: 'Version resolves automatically',
  gateways: 'default'
}

const SNAPSHOT_KIND_SELECT = {
  pools: '(select a pool)',
  repos: '(select a repo)',
  tasks: '(select a ticket)',
  claims: '(pick a claim)',
  awaiting: '(pick an awaiting-approval target)',
  grant: '(grant token)',
  version: '(auto)',
  gateways: '(select a gateway)'
}

export const MANUAL_OPTION = '(enter manually…)'

function entryId(entry, fallback) {
  if (entry == null) return str(fallback)
  if (typeof entry === 'string' || typeof entry === 'number') return str(entry)
  return str(entry.id ?? entry.ticket_id ?? entry.repo_id ?? entry.pool_id ?? entry.ticket ?? entry.repo ?? entry.name ?? fallback)
}

// itemsFor(snapshot, kind): pull the item list for a kind out of a plain
// snapshot object (never ctx, never a renderer). Malformed input → [].
export function itemsFor(snapshot, kind) {
  const s = snapshot && typeof snapshot === 'object' ? snapshot : {}
  switch (kind) {
    case 'pools': {
      const fromStatus = s.poolStatus && Array.isArray(s.poolStatus.pools) ? s.poolStatus.pools : []
      const fromRepos = asArray(s.repos).map(r => (r && typeof r === 'object') ? (r.pool || r.pool_id) : null).filter(Boolean).map(p => ({ id: p }))
      const seen = new Set()
      const out = []
      for (const e of [...fromStatus, ...fromRepos]) {
        const id = entryId(e, '')
        if (!id || seen.has(id)) continue
        seen.add(id)
        out.push(typeof e === 'string' ? { id: e } : e)
      }
      return out
    }
    case 'repos': return asArray(s.repos)
    case 'tasks': return asArray(s.tasks)
    case 'claims':
      return asArray(s.claims).map(op => {
        if (!op || typeof op !== 'object') return { id: str(op), title: str(op) }
        const ticket = str((op.args && (op.args.ticket || op.args.key)) || op.id || '')
        const when = op.at ? new Date(op.at).toLocaleString() : ''
        return { id: str(op.id), title: when ? `${ticket} · ${when}` : ticket }
      })
    case 'awaiting': return asArray(s.awaitingApproval ?? (s.poolStatus && s.poolStatus.awaitingApproval))
    case 'grant': return s.grantToken ? [{ id: str(s.grantToken), title: 'grant token' }] : []
    case 'version': return asArray(s.versions)
    case 'gateways': return asArray(s.gateways)
    default: return []
  }
}

// buildOptions(items, emptyHint, selectLabel, allowManual): the flat option
// list — placeholder first (label IS the empty hint when items is empty,
// disabled once items exist), one str()-ed label per item, then the opt-in
// manual fallback.
export function buildOptions(items, emptyHint, selectLabel, allowManual) {
  const list = asArray(items)
  const placeholder = list.length
    ? (selectLabel || '(select…)')
    : (emptyHint || 'Nothing loaded yet')
  const options = [{ value: '', label: str(placeholder) }]
  list.forEach((entry, i) => {
    options.push({ value: entryId(entry, i), label: labelFor(entry) })
  })
  if (allowManual) options.push({ value: '__manual__', label: MANUAL_OPTION })
  return options
}

// optionsFor(snapshot, kind, allowManual): pure — the option list for a kind,
// straight from a snapshot object. Tests call this directly.
export function optionsFor(snapshot, kind, allowManual = true) {
  return buildOptions(
    itemsFor(snapshot, kind),
    SNAPSHOT_KIND_EMPTY[kind] || 'Nothing loaded yet',
    SNAPSHOT_KIND_SELECT[kind] || '(select…)',
    allowManual
  )
}

// --- inlined from prompts.js (runtime import allowlist: no relative imports) ---
// --- prompt companions (copied to the clipboard; never submitted by the pane) ---
// The pane never sends anything through a chat composer. Each builder below
// produces the exact text the operator pastes to their Desktop agent in chat;
// the agent executes per the agent-pool skill and replies in chat. Every
// prompt names the gateway profile (when a gateway command is involved),
// carries the literal command, and ends by asking for a verbatim report.
// <gateway> is the pane's Maintainer/Contributor selector value.

export function addRepoOwnerPrompt({ gateway, repoId, path, version }) {
  const v = version != null && version !== '' ? ` --version ${version}` : ''
  return `Add the local checkout ${path} to the agent pool as repo ${repoId}: on the ${gateway} gateway run /add-repo ${repoId} --path ${path}${v}. If the pool already has accepted versions, first run /fetch repo ${repoId} to get accepted_version and pass --version <next>. Report the plugin's reply verbatim.`
}

export function addRepoLinkPrompt({ gateway, repoId, path }) {
  return `Contribute my local checkout ${path} to the maintainer's pool as repo ${repoId}: (1) run scripts/contributor-add.py ${path} --repo ${repoId} to mint a signed manifest and Tor transfer ticket; (2) post into the shared Discord channel, mention-first, manifest single-quoted: <@<maintainer_bot_id>> /add-repo --deliver <transfer_ticket> --manifest '<manifest_json>' --repo ${repoId}. Local paths only — never a remote URL. Report the replies verbatim.`
}

export function fetchRepoListPrompt({ gateway }) {
  return `On the ${gateway} gateway run /fetch repo list. Report the table verbatim.`
}

export function fetchTaskListPrompt({ gateway, repoId }) {
  const filter = repoId ? ` --repo ${repoId}` : ''
  return `On the ${gateway} gateway run /fetch repo list, then /fetch task list${filter}. Report both tables verbatim so I can paste them or so the pane's Refresh can pick up the sidecar.`
}

// 'add-task' companion: the /task_add flat alias (never the spaced
// task-add subcommand, whose add-branch handler drops --pool). The gateway
// parses with shlex, so values are JSON.stringify'd.
export function taskAddPrompt({ gateway, repoId, title, brief, poolId }) {
  const q = s => JSON.stringify(String(s))
  const r = repoId ? str(repoId).trim() : '<repo_id>'
  const t = title ? String(title) : '<title>'
  const parts = [`--repo ${r}`, `--title ${q(t)}`]
  if (brief && String(brief).trim()) parts.push(`--brief ${q(brief)}`)
  if (poolId && String(poolId).trim()) parts.push(`--pool ${String(poolId).trim()}`)
  return `Open a pool ticket on repo ${r}: run /task_add ${parts.join(' ')} (use the flat alias task_add — the spaced \`task add\` drops --pool). Report the new ticket_id verbatim.`
}

// 'prune' companion: enumerate, classify, print the list, delete only stale.
export function prunePrompt({ gateway }) {
  return `Prune stale agent-pool worktrees under ~/.hermes/plugin-data/agent-pool/work/integrate/. Step 1: enumerate every directory and gate-t-*.json file there. Step 2: classify — KEEP: every bundle-v*.git whose version is accepted in the ledger, every worktree tied to an open/claimed ticket, and anything you cannot positively classify as stale. STALE (the only things you may delete): branch-t-* directories and gate-t-*.json files with no matching live ticket. Step 3: print the full keep/delete list with counts before deleting anything. Step 4: delete ONLY the stale candidates. Touch nothing else. Report the before/after listing and the exact commands you ran.`
}

export function claimPrompt({ gateway, ticketId, maxTasks, dest }) {
  const destRef = dest && String(dest).trim() ? String(dest).trim() : '<work dir>'
  if (maxTasks && !ticketId) {
    return `Claim pool tickets (auto-assign): on the ${gateway} gateway run /claim --any-project --max-tasks ${maxTasks}. The reply carries worktree_path, branch (pool/<ticket_id>), base_ref and budget. Then run scripts/contributor-claim.py <ticket_id> --reply-file <saved reply.json> --dest <work dir> to fetch the accepted bytes and clone the worktree. Report the worktree path and branch verbatim.`
  }
  return `Claim pool ticket ${ticketId}: on the ${gateway} gateway run /claim ${ticketId}. The reply carries worktree_path, branch (pool/${ticketId}), base_ref and budget. Then run scripts/contributor-claim.py ${ticketId} --reply-file <saved reply.json> --dest ${destRef} to fetch the accepted bytes and clone the worktree. Report the worktree path and branch verbatim.`
}

export function pullPrompt({ gateway, repoId, ticketId, replyFile, dest }) {
  const ticket = ticketId || repoId
  const replyRef = replyFile ? ` --reply-file ${replyFile}` : ' --reply-file <saved claim reply json>'
  const destRef = dest || '<destination dir>'
  return `On the ${gateway} gateway, fetch the accepted bytes for ticket ${ticket} into a worktree: run scripts/contributor-claim.py ${ticket}${replyRef} --dest ${destRef}. Report the worktree path and branch verbatim.`
}

export function pushPrompt({ gateway, repoId, ticketId, worktreePath, poolId, version }) {
  const v = version != null && version !== '' ? version : '<N>'
  return `Push the completed work for ticket ${ticketId || '<ticket_id>'} on repo ${repoId || '<repo_id>'}, pool ${poolId || '<pool_id>'}: (1) request a grant: /push repo ${repoId || '<repo_id>'} --ticket ${ticketId || '<ticket_id>'}; (2) package the worktree ${worktreePath || '<worktree_path>'}: scripts/contributor-push.py ${worktreePath || '<worktree_path>'} --repo ${repoId || '<repo_id>'} --pool ${poolId || '<pool_id>'} --version ${v} — take next_version from the grant reply or re-ask /fetch repo ${repoId || '<repo_id>'} for accepted_version; never trust enroll-time chain_tips; (3) deliver: /push repo --deliver <transfer_ticket> --token <grant_token> --manifest '<manifest_json>'. A redelivery is legal — the grant is not consumed by deliver. Report each step's reply verbatim.`
}

export function approvePrompt({ gateway, repoId, version, ticketId, deny }) {
  const denyFlag = deny ? ' --deny' : ''
  if (ticketId) {
    return `OWNER ONLY: accept the pushed claim for ticket ${ticketId}: run scripts/pool-approve.py --ticket ${ticketId}${denyFlag}. If the plugin refuses because this profile is not the pool owner, report the refusal verbatim and do not retry. Report the plugin's reply verbatim.`
  }
  const r = repoId || '<repo_id>'
  const vFlag = version != null && version !== '' ? ` --version ${version}` : ''
  const vText = version != null && version !== '' ? ` (version ${version})` : ''
  return `OWNER ONLY: accept the submitted version for repo ${r}${vText}: run scripts/pool-approve.py --repo ${r}${vFlag}${denyFlag}. If the plugin refuses because this profile is not the pool owner, report the refusal verbatim and do not retry. Report the plugin's reply verbatim.`
}

export function mergePrompt({ gateway, repoId, ticketId, checkoutPath, version }) {
  const repo = repoId || '<repo_id>'
  const dest = checkoutPath || `work/integrate/${repo}/`
  const n = version != null && version !== '' ? version : '<N>'
  const target = ticketId ? `pushed claim ${ticketId}` : `repo ${repo}`
  return `OWNER ONLY: merge the approved bundle for ${target} into the canonical checkout at ${dest} — the plugin never merges, so do it as your own git work: git -C ${dest} fetch <bundle> 'refs/heads/master:refs/accepted/${repo}-v${n}' then git -C ${dest} merge --no-ff --allow-unrelated-histories refs/accepted/${repo}-v${n}. (Unrelated histories are normal for a fresh pool root; identical content is a correct no-op.) If the audit is not clean or no accepted bundle exists, report verbatim and stop.`
}

// Read-only audit companion.
export function auditShowPrompt(gateway, ticketId) {
  const t = (ticketId || '').trim()
  return `Read-only: on the ${gateway} gateway run /audit show${t ? ` ${t}` : ''} and report the result verbatim. No approval implied.`
}

// PROMPT_ACTIONS: the surface manifest — one entry per removed op. Tests pin
// this so skill and UI cannot diverge silently.
export const PROMPT_ACTIONS = {
  'add-repo-owner': { label: 'Add repo', builder: addRepoOwnerPrompt, where: 'repo-row / repos-empty-state' },
  'add-repo-link': { label: 'Contribute repo', builder: addRepoLinkPrompt, where: 'page-header / repos' },
  'fetch-tasks': { label: 'Fetch the catalog', builder: fetchTaskListPrompt, where: 'tasks-empty-state' },
  'claim': { label: 'Claim ticket', builder: claimPrompt, where: 'task-row' },
  'claim-auto': { label: 'Auto-assign tickets', builder: claimPrompt, where: 'claims' },
  'push': { label: 'Push work', builder: pushPrompt, where: 'claims-tab / worktree' },
  'approve': { label: 'Copy approve prompt', builder: approvePrompt, where: 'awaiting-tab' },
  'deny': { label: 'Copy deny prompt', builder: approvePrompt, where: 'awaiting-tab' },
  'merge': { label: 'Copy merge prompt', builder: mergePrompt, where: 'awaiting-tab' },
  'prune': { label: 'Prune stale worktrees', builder: prunePrompt, where: 'worktrees-card' },
  'audit-show': { label: 'Copy audit prompt', builder: auditShowPrompt, where: 'audit-tab' },
  'add-task': { label: 'Ask your agent to open a ticket', builder: taskAddPrompt, where: 'tasks-header' }
}

// Contribution ids are namespaced by the host as '<plugin id>:<local id>'.
// The error card names the contribution it came from.
const CONTRIBUTION_IDS = {
  nav: 'agent-pool:nav',
  page: 'agent-pool:page'
}

// The gateway profiles this plugin knows about. The live profile inventory
// arrives with the gateway selector; until then the settings page shows the
// known pool mapping only.
const KNOWN_GATEWAYS = [
  {
    description: 'Pool owner bot — registers repos, approves versions, merges.',
    id: 'default',
    role: 'Maintainer'
  },
  {
    description: 'Contributor bot — claims tickets, pushes completed work.',
    id: 'pool-contrib',
    role: 'Contributor'
  }
]

const DISPLAY_COMPACT_KEY = 'display.compact'

const STORAGE_KEYS = {
  gateway: 'gateways.active',
  polling: 'display.polling',
  highlighted: 'ops.highlighted',
  collapsed: id => `display.collapsed.${id}`
}

// Module-level holder for pane state, hydrated from plugin-scoped storage at
// register time so it survives close/reopen of the pane.
export const poolPaneStore = {
  gateway: 'default',
  polling: true,
  highlight: null,
  collapsed: {}
}

function hydratePaneStore(storage) {
  poolPaneStore.gateway = storage.get(STORAGE_KEYS.gateway, 'default')
  // Polling defaults ON; respect whatever the user last persisted.
  poolPaneStore.polling = Boolean(storage.get(STORAGE_KEYS.polling, true))
  poolPaneStore.highlight = storage.get(STORAGE_KEYS.highlighted, null)
  // Migrate any pre-Task-8 op records (e.g. kind 'add-repo') in place.
  const { ops, changed } = migrateOps(storage.get(OPS_KEY, []))
  if (changed) storage.set(OPS_KEY, ops)
  // Seed the gateways snapshot from the known mapping + any operator
  // settings, so the link flow always has a non-empty dropdown source.
  const existingGateways = storage.get('snapshot.gateways', null)
  if (!existingGateways || !Array.isArray(existingGateways.items)) {
    storage.set('snapshot.gateways', { at: Date.now(), items: KNOWN_GATEWAYS.map(g => ({ ...g })) })
  }
}

// Notify the user of an op result and persist which op is highlighted.
function notifyOp(ctx, kind, message, detail) {
  // The host SDK types notification detail as string; an object detail is put
  // straight into a React child by the toast renderer and crashes the whole
  // renderer (React #31, toasts live outside the pane's error boundary).
  // Coerce defensively so a future call site can never re-crash the app.
  const detailText = typeof detail === 'string' ? detail : detail != null ? str(detail) : undefined
  host.notify({
    title: kind,
    message,
    detail: detailText,
    kind: kind === 'failed' || kind === 'refused' ? 'error' : 'info'
  })
  // Keep the row-highlight behaviour: object details carry {id}; string
  // details that are op ids highlight that op directly.
  const highlightId = detail != null && typeof detail === 'object' && detail.id != null
    ? detail.id
    : typeof detail === 'string' && /^op-/.test(detail) ? detail : null
  if (highlightId != null) {
    poolPaneStore.highlight = highlightId
    ctx.storage.set(STORAGE_KEYS.highlighted, highlightId)
  }
}

// copyText(ctx, text): write to the system clipboard via the host-curated OS
// door (ctx.os.writeClipboard — desktop-plugin-sdk.md: ctx.os.writeClipboard).
// Resolves false (never throws) on hosts without the capability; the toast
// says so instead of dying silently.
export async function copyText(ctx, text) {
  let ok = false
  try {
    ok = Boolean(await ctx.os.writeClipboard(text))
  } catch {
    ok = false
  }
  notifyOp(ctx, 'info', ok
    ? 'Prompt copied — paste it to your Hermes agent'
    : 'Clipboard unavailable on this host — open the details fold and copy the prompt manually')
  return ok
}

// Action ids whose copied prompt leads to an agent reply with a home in the
// history panel. EVERY companion copy appends a read-only status:'copied'
// record — never 'submitted' or 'awaiting agent'; the pane never submits.
const COPIED_RECORD_ACTIONS = { 'add-repo-owner': 'add-repo-owner', 'add-repo-link': 'add-repo-link', 'add-task': 'add-task', 'fetch-tasks': 'fetch-tasks', 'claim': 'claim', 'claim-auto': 'claim', 'pull': 'pull', 'push': 'push', 'approve': 'approve', 'deny': 'approve', 'merge': 'merge', 'prune': 'prune', 'audit-show': 'audit-show' }

// copyPrompt(ctx, actionId, args): build the companion prompt via PROMPT_ACTIONS,
// copy it, toast, and (for approve/deny/push/prune) append a 'copied' op record.
export async function copyPrompt(ctx, actionId, args) {
  const action = PROMPT_ACTIONS[actionId]
  if (!action) return null
  const a = args || {}
  const gateway = a.gateway || poolPaneStore.gateway
  let text = ''
  try {
    if (actionId === 'audit-show') text = auditShowPrompt(gateway, a.ticketId)
    else if (actionId === 'deny') text = approvePrompt({ ...a, gateway, deny: true })
    else text = action.builder({ gateway, ...a })
  } catch {
    text = ''
  }
  if (!text) {
    notifyOp(ctx, 'failed', 'Could not compose the prompt')
    return null
  }
  await copyText(ctx, text)
  const kind = COPIED_RECORD_ACTIONS[actionId]
  if (kind) {
    const record = {
      id: `op-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind,
      gateway,
      args: { ...a, actionId },
      status: 'copied',
      at: Date.now(),
      replyRef: null,
      text
    }
    const ops = ctx.storage.get(OPS_KEY, [])
    const next = [record, ...(Array.isArray(ops) ? ops : [])].slice(0, OPS_KEEP)
    ctx.storage.set(OPS_KEY, next)
    return record
  }
  return null
}

// promptForOp(op, gateway): rebuild a companion prompt from a stored op's args
// (used by "Copy prompt again"); falls back to the stored op.text.
export function promptForOp(op, gateway) {
  const a = (op && op.args) || {}
  const gw = gateway || (op && op.gateway) || poolPaneStore.gateway
  const fallback = op && op.text ? str(op.text) : ''
  try {
    switch (op && op.kind) {
      case 'add-repo-owner': return addRepoOwnerPrompt({ gateway: gw, repoId: str(a.repoId || '<repo_id>'), path: str(a.path || '<abs_path>'), version: a.version })
      case 'add-repo-link': return addRepoLinkPrompt({ gateway: gw, repoId: str(a.repoId || '<repo_id>'), path: str(a.path || '<abs_path>') })
      case 'add-task': return taskAddPrompt({ gateway: gw, repoId: str(a.repoId || '<repo_id>'), title: str(a.title || '<title>'), brief: a.brief, poolId: a.poolId })
      case 'fetch-tasks': return fetchTaskListPrompt({ gateway: gw, repoId: a.repo || undefined })
      case 'claim': return a.autoAssign ? claimPrompt({ gateway: gw, maxTasks: a.maxTasks || 1 }) : claimPrompt({ gateway: gw, ticketId: str(a.ticket || a.ticketId || '<ticket_id>') })
      case 'pull': return pullPrompt({ gateway: gw, ticketId: str(a.ticket || '<ticket_id>'), replyFile: a.replyFile, dest: a.dest })
      case 'push': return pushPrompt({ gateway: gw, repoId: a.repoId, ticketId: a.ticketId || a.ticket, worktreePath: a.worktreePath, poolId: a.poolId, version: a.version })
      case 'approve': return approvePrompt({ gateway: gw, repoId: a.repoId, version: a.version, ticketId: a.ticketId || a.ticket, deny: Boolean(a.deny) })
      case 'merge': return mergePrompt({ gateway: gw, repoId: a.repoId, ticketId: a.ticketId || a.ticket, checkoutPath: a.checkoutPath, version: a.version })
      case 'prune': return prunePrompt({ gateway: gw })
      default: return fallback
    }
  } catch {
    return fallback
  }
}

const OP_STATUS_PILL = {
  submitted: 'submitted',
  'awaiting agent': 'awaiting agent',
  'done (awaiting approval)': 'done (awaiting approval)',
  done: 'done',
  refused: 'refused',
  failed: 'failed',
  copied: 'copied'
}

function opPillClass(status) {
  if (status === 'done' || status === 'done (awaiting approval)') return 'rounded-full px-2 py-0.5 text-xs text-(--ui-text-primary)'
  if (status === 'refused' || status === 'failed') {
    return 'rounded-full px-2 py-0.5 text-xs text-(--ui-text-secondary)'
  }
  return 'rounded-full px-2 py-0.5 text-xs text-(--ui-text-tertiary)'
}

// Absolute local path shape only: must start with '/', no scheme, no '://',
// no git@host: scp-like syntax. The pane never carries remote URLs.
function isAbsoluteLocalPath(value) {
  const v = (value || '').trim()
  if (!v.startsWith('/')) return false
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(v)) return false
  if (v.includes('://')) return false
  if (/^[^\s/]+@[^\s/]+:/.test(v)) return false
  if (/[\s]/.test(v)) return false
  return true
}

// --- discovered-repo helpers (pure; testable without a renderer) ------------
// Discovery replies arrive as free text or objects; normalize to
// { id, path, name } where id === path (the dropdown value) and name is the
// basename. Entries failing the local-absolute-path shape check are filtered
// out — the pane never carries remote URLs.
export function repoNameFromPath(path) {
  const parts = String(path || '').split('/').filter(Boolean)
  return parts.length ? parts[parts.length - 1] : ''
}

function normalizeDiscoveredEntry(raw) {
  if (raw == null) return null
  let path = ''
  if (typeof raw === 'string' || typeof raw === 'number') {
    const line = String(raw).trim()
    // Format: '<path> — <first remote or directory name>'
    path = (line.includes(' — ') ? line.split(' — ')[0] : line).trim()
  } else if (typeof raw === 'object') {
    path = str(raw.path ?? raw.repo_path ?? raw.id ?? '').trim()
  }
  if (!path) return null
  return { id: path, path, name: repoNameFromPath(path) }
}

export function filterDiscovered(items) {
  const out = []
  for (const raw of asArray(items)) {
    const entry = normalizeDiscoveredEntry(raw)
    if (entry && isAbsoluteLocalPath(entry.path)) out.push(entry)
  }
  return out
}

// Flow-state machine for the add-repo flows: { discoveredId, poolId,
// manualPath? }. Returns { path, repoId } or null — a prompt cannot be
// produced without a discovered-or-manual (absolute, local) path.
export function resolveAddRepoSelection({ discoveredId, manualPath, discovered }) {
  const manual = (manualPath || '').trim()
  if (manual) {
    if (!isAbsoluteLocalPath(manual)) return null
    return { path: manual, repoId: repoNameFromPath(manual) }
  }
  const hit = asArray(discovered).find(e => e && str(e.id ?? e.path) === str(discoveredId))
  if (!hit || !isAbsoluteLocalPath(hit.path)) return null
  return { path: hit.path, repoId: hit.name || repoNameFromPath(hit.path) }
}

const inputClass = 'rounded-md px-2 py-1 text-(--ui-text-primary)'
const labelClass = 'text-(--ui-text-secondary)'

// Shared button classes (theme vars only — no literal colours). Every clickable
// element gets a border/background so it LOOKS clickable:
// - Primary: filled — the one main action per form (mirrors TaskTable's Assign).
// - Secondary: outlined — everything else.
// - Ghost: bordered pill for small expander/aux controls (chevron carriers).
// Buttons there hover to --ui-bg-quaternary and focus to a 2px --ui-accent
// outline, matching the host app's own button contract.
const btnPrimary = 'inline-flex cursor-pointer items-center gap-1 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) px-3 py-1.5 text-(--ui-text-primary) hover:bg-(--ui-bg-quaternary) focus-visible:outline focus-visible:outline-2 focus-visible:outline-(--ui-accent) disabled:cursor-not-allowed disabled:opacity-50'
const btnSecondary = 'inline-flex cursor-pointer items-center gap-1 rounded-md border border-(--ui-stroke-tertiary) px-2 py-1 text-(--ui-text-secondary) hover:border-(--ui-stroke-secondary) hover:text-(--ui-text-primary) focus-visible:outline focus-visible:outline-2 focus-visible:outline-(--ui-accent) disabled:cursor-not-allowed disabled:opacity-50'
const btnGhost = 'inline-flex cursor-pointer items-center gap-1 rounded-md border border-(--ui-stroke-tertiary) bg-transparent px-2 py-0.5 text-(--ui-text-secondary) hover:border-(--ui-stroke-secondary) hover:text-(--ui-text-primary) focus-visible:outline focus-visible:outline-2 focus-visible:outline-(--ui-accent)'

// Reusable snapshot-fed dropdown. Opt-in manual fallback per field per
// session (never persisted); refresh button disabled while an op of the
// refresh kind is 'awaiting agent'.
function SnapshotSelect({ label, items, value, onChange, emptyHint, allowManual, manualPlaceholder, manualValue, onManualChange, selectLabel, resetKey, loadingKey }) {
  const [manual, setManual] = useState(false)
  const loading = useSnapshotLoading(loadingKey)
  // Flow reset: the manual disclosure is closed by default and un-checks
  // itself whenever the flow's reset key changes.
  useEffect(() => { setManual(false) }, [resetKey])
  const list = asArray(items)
  const options = buildOptions(list, emptyHint || 'Nothing loaded yet', selectLabel, Boolean(allowManual))
  const childNodes = [
    jsxs('div', {
      key: 'row',
      className: 'flex items-center justify-between gap-2',
      children: [
        jsx('span', { className: labelClass, children: label }),
      ]
    })
  ]
  if (manual) {
    childNodes.push(jsxs('div', {
      key: 'manual',
      className: 'flex items-center gap-1',
      children: [
        jsx('input', { className: inputClass, value: manualValue || '', onChange: e => onManualChange && onManualChange(e.target.value), placeholder: manualPlaceholder || '', autoFocus: true }),
        jsx('button', { className: btnGhost, title: 'Back to the list', onClick: () => setManual(false), children: 'list' })
      ]
    }))
  } else if (loading) {
    childNodes.push(jsx('select', {
      key: 'select',
      className: inputClass,
      value: '',
      disabled: true,
      children: jsx('option', { value: '', children: 'Loading…' })
    }))
  } else {
    childNodes.push(jsx('select', {
      key: 'select',
      className: inputClass,
      value: value || '',
      onChange: e => {
        const v = e.target.value
        if (v === '__manual__') {
          setManual(true)
          onManualChange && onManualChange('')
          return
        }
        onChange(v)
      },
      children: options.map(o => jsx('option', {
        key: `${o.value}::${o.label}`,
        value: o.value,
        disabled: o.value === '' && list.length > 0,
        children: str(o.label)
      }))
    }))
  }
  return jsx('div', { className: 'flex flex-col gap-1', children: childNodes })
}

// Snapshot source builders (read ctx.storage defensively; never throw).
function snapshotSources(ctx) {
  const poolStatus = ctx.storage.get('snapshot.poolStatus', null)
  const ps = poolStatus && typeof poolStatus === 'object' ? (poolStatus.poolStatus && typeof poolStatus.poolStatus === 'object' ? poolStatus.poolStatus : poolStatus) : null
  const awaiting = poolStatus && Array.isArray(poolStatus.awaitingApproval)
    ? poolStatus.awaitingApproval
    : (ps && Array.isArray(ps.awaitingApproval) ? ps.awaitingApproval : [])
  const repos = readSnapshot(ctx, 'snapshot.repos')
  const tasks = readSnapshot(ctx, 'snapshot.tasks')
  const claimsRaw = ctx.storage.get('snapshot.claims', null)
  const claims = Array.isArray(claimsRaw) ? claimsRaw : (claimsRaw && Array.isArray(claimsRaw.items) ? claimsRaw.items : [])
  const gateways = readSnapshot(ctx, 'snapshot.gateways')
  return { repos, tasks, claims, poolStatus: ps, awaitingApproval: awaiting, gateways }
}

function snapshotRepos(ctx) {
  return snapshotSources(ctx).repos
}

function field(label, input) {
  return jsxs('label', {
    className: 'flex flex-col gap-1',
    children: [jsx('span', { className: labelClass, children: label }), input]
  })
}

// One muted caption line under each form header: plain-language guidance for
// what the operation does and what to expect back.
function formCaption(text) {
  return jsx('div', { className: 'text-(--ui-text-tertiary)', children: text })
}

const TRACKER_TABS = [
  { id: 'active', label: 'Active ops' },
  { id: 'awaiting', label: 'Awaiting approval' },
  { id: 'claims', label: 'Claims & budgets' },
  { id: 'audit', label: 'Audit' }
]

// Per-row details fold: sandbox-safe toggled div (no <details> assumptions).
// Carries the agent's verbatim reply (op.reply, kept last-per-op-id by the
// refresh loop) plus the composed prompt text. Default collapsed.
function OpDetailsFold({ op }) {
  const [open, setOpen] = useState(false)
  const unparsed = Boolean(op && op.reply) && isActiveStatus(op && op.status)
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('button', {
        className: btnGhost,
        'aria-expanded': open,
        onClick: () => setOpen(v => !v),
        children: [
          jsx('span', { 'aria-hidden': 'true', children: open ? '▾' : '▸' }),
          jsx('span', { children: 'details' }),
          unparsed ? jsx('span', { className: 'text-(--ui-text-secondary)', children: '· review manually' }) : null
        ]
      }),
      open
        ? jsxs('div', {
            className: 'flex flex-col gap-1 rounded-md px-2 py-1 text-(--ui-text-secondary)',
            children: [
              jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'agent reply (verbatim)' }),
              jsx('pre', { className: 'whitespace-pre-wrap text-(--ui-text-primary)', children: op && op.reply ? str(op.reply) : '(no reply captured yet)' }),
              jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'composed prompt' }),
              jsx('pre', { className: 'whitespace-pre-wrap text-(--ui-text-primary)', children: op && op.text ? str(op.text) : '(prompt text not recorded for this op)' })
            ]
          })
        : null
    ]
  })
}

function opRow(op, lookup, handlers) {
  const hs = handlers || {}
  const substepsLine = op && op.args && Array.isArray(op.args.substeps)
    ? op.args.substeps.map(s => {
        const done = asArray(op.args.substepsDone).map(String).includes(String(s))
        return `${str(s)} ${done ? '✓' : '○'}`
      }).join(' → ')
    : null
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-md px-2 py-1',
    key: str(op.id),
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'flex flex-col',
            children: [
              jsx('div', { className: 'text-(--ui-text-primary)', children: summarizeOp(op, lookup) }),
              jsx('div', {
                className: 'text-(--ui-text-tertiary)',
                children: `${str(op.gateway) === 'none' ? '' : str(op.gateway)}${op.at ? ` · ${new Date(op.at).toLocaleString()}` : ''}${op.status === 'copied' ? ' · prompt copied' : ''}`
              }),
              op.error ? jsx('div', { className: 'text-(--ui-text-secondary)', children: `error: ${str(op.error)}` }) : null,
              substepsLine
                ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: substepsLine })
                : (op.args && op.args.substatus ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: `substatus: ${str(op.args.substatus)}` }) : null)
            ]
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              hs.extra || null,
              jsx('button', {
                className: btnGhost,
                title: 'Rebuilds the companion prompt and copies it to the clipboard',
                onClick: () => hs.onCopyPrompt && hs.onCopyPrompt(op),
                children: 'Copy prompt again'
              }),
              op.status === 'refused'
                ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: 'read the refusal; fix the cause, then copy the prompt again.' })
                : null,
              jsx('span', { className: opPillClass(op.status), children: str(OP_STATUS_PILL[op.status] || op.status) })
            ]
          })
        ]
      }),
      jsx(OpDetailsFold, { op })
    ]
  })
}

// The tracker: filter tabs over the pane-local op log plus the last parsed
// pool_status snapshot. The Awaiting-approval tab renders ONLY entries from
// snapshot.poolStatus.awaitingApproval (i.e. real pool_status output), never
// inferred from ops.
// The tracker, demoted to a read-only history panel ("Activity & replies").
// Stored op rows render with status pills and OpDetailsFold (verbatim reply +
// composed prompt); statuses still update from the sidecar. No submit/retry
// actions remain — only copy-prompt companions.
function TrackerSection({ ctx, ops, poolStatus, gateway, onRefresh, refreshing, copyAndLog, copyText }) {
  const [tab, setTab] = useState('active')
  const [auditTicket, setAuditTicket] = useState('')
  const list = Array.isArray(ops) ? ops : []
  const awaiting = (poolStatus && Array.isArray(poolStatus.awaitingApproval)) ? poolStatus.awaitingApproval : []
  const budgets = (poolStatus && Array.isArray(poolStatus.budgets)) ? poolStatus.budgets : []
  const claimList = list.filter(op => op && op.kind === 'claim')
  const lookup = (() => {
    const s = snapshotSources(ctx)
    return { repos: s.repos, tasks: s.tasks }
  })()
  const onCopyPrompt = op => copyText(promptForOp(op, gateway))
  const renderRow = (op, extra) => opRow(op, lookup, { onCopyPrompt, extra })

  let body = null
  if (tab === 'active') {
    const active = list.filter(op => op && isActiveStatus(op.status))
    body = active.length
      ? jsx('div', { className: 'flex flex-col gap-1', children: active.slice(0, 20).map(op => renderRow(op)) })
      : jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No recorded activity yet. Copy any prompt and paste it to your agent in chat — replies appear here.' })
  } else if (tab === 'awaiting') {
    body = awaiting.length
      ? jsx('div', { className: 'flex flex-col gap-1', children: awaiting.map((entry, i) => {
          const repoId = entry && entry.repo != null ? str(entry.repo) : undefined
          const ticketId = entry && entry.ticket != null ? str(entry.ticket) : undefined
          const version = entry && entry.version != null ? entry.version : undefined
          const target = ticketId ? { ticketId, gateway } : { repoId: repoId || str(entry && entry.id), version, gateway }
          return jsxs('div', { key: str(entry && entry.id) !== 'none' ? str(entry.id) : i, className: 'flex items-center justify-between gap-2 rounded-md px-2 py-1 text-(--ui-text-primary)', children: [
            jsx('span', { children: `${str(entry && entry.id)}${entry && entry.version != null ? ` · v${str(entry.version)}` : ''} (from pool_status)` }),
            jsxs('div', {
              className: 'flex items-center gap-2',
              children: [
                jsx('button', { className: btnPrimary, title: 'Copies the approve prompt — paste it to your agent in chat', onClick: () => copyAndLog('approve', target), children: 'Copy approve prompt' }),
                jsx('button', { className: btnSecondary, title: 'Copies the deny prompt (--deny) — paste it to your agent in chat', onClick: () => copyAndLog('deny', target), children: 'Copy deny prompt' }),
                jsx('button', { className: btnGhost, title: 'Copies the merge prompt — paste it to your agent in chat', onClick: () => copyAndLog('merge', { ...target, checkoutPath: repoId ? `work/integrate/${repoId}/` : undefined }), children: 'Copy merge prompt' })
              ]
            })
          ] })
        }) })
      : jsx('div', { className: 'text-(--ui-text-tertiary)', children: poolStatus ? 'Nothing awaiting approval in the last pool_status snapshot.' : 'No pool_status snapshot yet — hit Refresh or request a status report.' })
  } else if (tab === 'claims') {
    body = jsxs('div', {
      className: 'flex flex-col gap-1',
      children: [
        jsx('button', { className: btnGhost, onClick: () => copyAndLog('claim-auto', { gateway, maxTasks: 1 }), children: 'Copy auto-assign prompt (/claim --any-project --max-tasks 1)' }),
        jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Budgets (from pool_status)' }),
        budgets.length
          ? budgets.map((b, i) => jsx('div', { key: i, className: 'rounded-md px-2 py-1 text-(--ui-text-primary)', children: `${str(b && b.line)}${b && b.exhausted ? ' — EXHAUSTED' : ''}` }))
          : jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No budget lines in the last pool_status snapshot.' }),
        jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Claim ops' }),
        claimList.length
          ? claimList.slice(0, 20).map(op => renderRow(op, jsx('button', {
              key: 'push',
              className: btnGhost,
              title: 'Copies the push (grant → package → deliver) prompt — paste it to your agent in chat',
              onClick: () => copyAndLog('push', { gateway, ticketId: op && op.args && (op.args.ticket || op.args.key), repoId: op && op.args && op.args.repoId, worktreePath: op && op.args && op.args.worktreePath, poolId: op && op.args && op.args.poolId }),
              children: 'Copy push prompt'
            })))
          : jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No claim ops recorded.' })
      ]
    })
  } else if (tab === 'audit') {
    body = jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        jsx(SnapshotSelect, {
          label: 'ticket id (optional)',
          items: snapshotSources(ctx).tasks,
          value: auditTicket,
          onChange: setAuditTicket,
          emptyHint: SNAPSHOT_KIND_EMPTY.tasks,
          selectLabel: '(all tickets)',
          allowManual: true,
          manualPlaceholder: 'ticket_id',
          manualValue: auditTicket,
          onManualChange: setAuditTicket
        }),
        formCaption('Read-only audit report for a ticket or repo. Copy the prompt and paste it to your agent in chat — no approval implied.'),
        jsx('button', {
          className: btnPrimary,
          title: 'Copies the audit prompt — paste it to your agent in chat',
          onClick: () => copyAndLog('audit-show', { gateway, ticketId: auditTicket }),
          children: 'Copy audit prompt'
        })
      ]
    })
  }

  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Activity & replies' }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('button', { className: btnSecondary, onClick: onRefresh, disabled: refreshing, children: refreshing ? 'Refreshing…' : 'Refresh' })
            ]
          })
        ]
      }),
      jsx('div', { className: 'text-(--ui-text-tertiary)', role: 'status', children: `pool_status snapshot: ${poolStatus && poolStatus.at ? new Date(poolStatus.at).toLocaleString() : 'no snapshot yet'}` }),
      jsx(SegmentedControl, {
        options: TRACKER_TABS,
        value: tab,
        onChange: setTab
      }),
      body
    ]
  })
}

// variant: 'primary' (filled) for the single most-likely action per section,
// 'secondary' (outlined) for everything else.
function submitButton({ disabled, title, children, onClick, variant = 'secondary' }) {
  return jsx('button', {
    className: variant === 'primary' ? btnPrimary : btnSecondary,
    disabled,
    title: disabled ? title : undefined,
    onClick,
    children
  })
}

// --- prompt companions: input widgets that compose clipboard prompts --------
// Every field below feeds a copy-to-clipboard prompt built from the CURRENT
// values. Nothing submits; nothing records 'submitted'. Pasting the prompt
// into a Hermes chat is the operator's act, not the pane's.
const PASTE_HINT = 'Paste this in your Hermes chat — the agent runs it against the maintainer gateway/ledger.'

function PromptGroup({ title, children }) {
  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: str(title) }),
      children
    ]
  })
}

function PromptCompanionSection({ ctx, ops, poolStatus, gateway, copyAndLog }) {
  const sources = snapshotSources(ctx)
  const poolsRaw = itemsFor({ poolStatus: sources.poolStatus, repos: sources.repos }, 'pools')
  const pools = poolsRaw.length ? poolsRaw : [{ id: 'demo-pool' }]
  const openTickets = sources.tasks.filter(t => t && t.state === 'open')
  const latestClaim = asArray(ops).find(o => o && o.kind === 'claim' && o.args && (o.args.worktreePath || o.args.ticket || o.args.ticketId))
  const granted = poolStatus && poolStatus.grantToken ? str(poolStatus.grantToken) : null

  // add-repo (target is a NEW repo: id/path entry, not a pool pick)
  const [repoPath, setRepoPath] = useState('')
  const [repoId, setRepoId] = useState('')
  const [repoPool, setRepoPool] = useState('demo-pool')
  // add-task
  const [taskRepo, setTaskRepo] = useState('')
  const [taskTitle, setTaskTitle] = useState('')
  const [taskBrief, setTaskBrief] = useState('')
  const [taskPool, setTaskPool] = useState('demo-pool')
  // claim
  const [claimTicket, setClaimTicket] = useState('')
  const [claimDest, setClaimDest] = useState('')
  // push
  const [pushRepo, setPushRepo] = useState('')
  const [pushTicket, setPushTicket] = useState('')
  const [pushVersion, setPushVersion] = useState('')
  const [pushPool, setPushPool] = useState('demo-pool')
  const [pushWorktree, setPushWorktree] = useState(() => (latestClaim && latestClaim.args && latestClaim.args.worktreePath) ? str(latestClaim.args.worktreePath) : '')
  // fetch
  const [fetchRepo, setFetchRepo] = useState('')
  // pull
  const [pullTicket, setPullTicket] = useState('')
  const [pullDest, setPullDest] = useState('')
  // approve / deny (shared fields; maintainer-only)
  const [approveRepo, setApproveRepo] = useState('')
  const [approveVersion, setApproveVersion] = useState('')
  // merge (maintainer-only)
  const [mergeRepo, setMergeRepo] = useState('')
  const [mergeVersion, setMergeVersion] = useState('')
  const [mergeCheckout, setMergeCheckout] = useState('')

  const repoPathOk = isAbsoluteLocalPath(repoPath)
  const addRepoId = repoId.trim() || repoNameFromPath(repoPath)
  const poolSelect = (value, onChange, label) => jsx(SnapshotSelect, {
    label: label || 'pool',
    items: pools,
    value,
    onChange,
    emptyHint: SNAPSHOT_KIND_EMPTY.pools,
    selectLabel: '(select a pool)',
    allowManual: true,
    manualPlaceholder: 'pool_id',
    manualValue: value,
    onManualChange: onChange
  })

  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      jsx(PromptGroup, {
        title: 'Add a repo to the pool',
        children: [
          field('path (absolute, local checkout)', jsx('input', { className: inputClass, value: repoPath, onChange: e => setRepoPath(e.target.value), placeholder: '/home/you/src/repo' })),
          poolSelect(repoPool, setRepoPool, 'pool'),
          field('repo id (optional — defaults to the directory name)', jsx('input', { className: inputClass, value: repoId, onChange: e => setRepoId(e.target.value), placeholder: repoNameFromPath(repoPath) || 'my-repo' })),
          formCaption('New repo: enter its id (or leave blank to use the directory name). If the pool already has accepted versions, the agent should run /fetch repo <id> first and pass --version <next>. ' + PASTE_HINT),
          jsx('button', {
            className: btnPrimary,
            disabled: !repoPathOk,
            title: repoPathOk ? 'Copy the /add-repo prompt' : 'absolute local path only',
            onClick: () => copyAndLog('add-repo-owner', { gateway, repoId: addRepoId, path: repoPath.trim(), poolId: repoPool.trim() }),
            children: 'Copy /add-repo prompt'
          })
        ]
      }),
      jsx(PromptGroup, {
        title: 'Open a task (ticket)',
        children: [
          jsx(SnapshotSelect, {
            label: 'repo',
            items: sources.repos,
            value: taskRepo,
            onChange: setTaskRepo,
            emptyHint: SNAPSHOT_KIND_EMPTY.repos,
            selectLabel: '(select a repo)',
            allowManual: true,
            manualPlaceholder: 'repo_id',
            manualValue: taskRepo,
            onManualChange: setTaskRepo
          }),
          field('title', jsx('input', { className: inputClass, value: taskTitle, onChange: e => setTaskTitle(e.target.value), placeholder: 'Fix the thing' })),
          field('brief', jsx('textarea', { className: inputClass, value: taskBrief, onChange: e => setTaskBrief(e.target.value), placeholder: 'What the ticket should say…', rows: 2 })),
          poolSelect(taskPool, setTaskPool, 'pool'),
          formCaption('Composes /task_add (flat alias — the spaced `task add` drops --pool). ' + PASTE_HINT),
          jsx('button', {
            className: btnPrimary,
            disabled: !(taskRepo.trim() && taskTitle.trim()),
            title: 'Copy the /task_add prompt',
            onClick: () => copyAndLog('add-task', { gateway, repoId: taskRepo.trim(), title: taskTitle.trim(), brief: taskBrief.trim(), poolId: taskPool.trim() }),
            children: 'Copy /task_add prompt'
          })
        ]
      }),
      jsx(PromptGroup, {
        title: 'Claim a task',
        children: [
          jsx(SnapshotSelect, {
            label: 'open ticket',
            items: openTickets,
            value: claimTicket,
            onChange: setClaimTicket,
            emptyHint: 'No open tickets in the snapshot — Refresh or fetch the catalog',
            selectLabel: '(select an open ticket)',
            allowManual: true,
            manualPlaceholder: 'ticket_id',
            manualValue: claimTicket,
            onManualChange: setClaimTicket
          }),
          field('work dir (optional)', jsx('input', { className: inputClass, value: claimDest, onChange: e => setClaimDest(e.target.value), placeholder: '<work dir>' })),
          formCaption('Composes /claim <ticket>. ' + PASTE_HINT),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('button', {
                className: btnPrimary,
                disabled: !claimTicket.trim(),
                title: 'Copy the /claim prompt',
                onClick: () => copyAndLog('claim', { gateway, ticketId: claimTicket.trim(), dest: claimDest.trim() || undefined }),
                children: 'Copy /claim prompt'
              }),
              jsx('button', {
                className: btnGhost,
                title: 'Copy the auto-assign prompt',
                onClick: () => copyAndLog('claim-auto', { gateway, maxTasks: 1 }),
                children: 'Copy auto-assign prompt'
              })
            ]
          })
        ]
      }),
      jsx(PromptGroup, {
        title: 'Push work (grant → package → deliver)',
        children: [
          jsx(SnapshotSelect, {
            label: 'repo',
            items: sources.repos,
            value: pushRepo,
            onChange: setPushRepo,
            emptyHint: SNAPSHOT_KIND_EMPTY.repos,
            selectLabel: '(select a repo)',
            allowManual: true,
            manualPlaceholder: 'repo_id',
            manualValue: pushRepo,
            onManualChange: setPushRepo
          }),
          jsx(SnapshotSelect, {
            label: 'ticket',
            items: sources.tasks,
            value: pushTicket,
            onChange: setPushTicket,
            emptyHint: SNAPSHOT_KIND_EMPTY.tasks,
            selectLabel: '(select a ticket)',
            allowManual: true,
            manualPlaceholder: 'ticket_id',
            manualValue: pushTicket,
            onManualChange: setPushTicket
          }),
          field('version (optional — defaults to accepted_version + 1)', jsx('input', { className: inputClass, value: pushVersion, onChange: e => setPushVersion(e.target.value), placeholder: 'auto' })),
          poolSelect(pushPool, setPushPool, 'pool'),
          field('worktree path (auto-filled from your latest claim)', jsx('input', { className: inputClass, value: pushWorktree, onChange: e => setPushWorktree(e.target.value), placeholder: '/path/to/worktree' })),
          granted ? formCaption(`grant token detected in pool_status (${granted.slice(0, 8)}…) — the deliver step will use it. ` + PASTE_HINT) : formCaption('No grant token in the last pool_status snapshot — the agent requests the grant first. ' + PASTE_HINT),
          jsx('button', {
            className: btnPrimary,
            disabled: !(pushRepo.trim() && pushTicket.trim()),
            title: 'Copy the combined grant/package/deliver prompt',
            onClick: () => copyAndLog('push', {
              gateway,
              repoId: pushRepo.trim(),
              ticketId: pushTicket.trim(),
              worktreePath: pushWorktree.trim() || undefined,
              poolId: pushPool.trim(),
              version: pushVersion.trim() || undefined,
              grantToken: poolStatus && poolStatus.grantToken || undefined,
              transferTicket: poolStatus && poolStatus.transferTicket || undefined,
              manifest: poolStatus && poolStatus.manifest || undefined
            }),
            children: 'Copy push prompt'
          })
        ]
      }),
      jsx(PromptGroup, {
        title: 'Fetch the catalog',
        children: [
          jsx(SnapshotSelect, {
            label: 'repo (optional)',
            items: sources.repos,
            value: fetchRepo,
            onChange: setFetchRepo,
            emptyHint: SNAPSHOT_KIND_EMPTY.repos,
            selectLabel: '(all repos)',
            allowManual: true,
            manualPlaceholder: 'repo_id',
            manualValue: fetchRepo,
            onManualChange: setFetchRepo
          }),
          formCaption('Read-only fetch of repo/task lists. ' + PASTE_HINT),
          jsx('button', {
            className: btnSecondary,
            title: 'Copy the /fetch prompt',
            onClick: () => copyAndLog('fetch-tasks', { gateway, repoId: fetchRepo.trim() || undefined }),
            children: 'Copy /fetch prompt'
          })
        ]
      }),
      jsx(PromptGroup, {
        title: 'Pull accepted bytes',
        children: [
          jsx(SnapshotSelect, {
            label: 'ticket',
            items: sources.tasks,
            value: pullTicket,
            onChange: setPullTicket,
            emptyHint: SNAPSHOT_KIND_EMPTY.tasks,
            selectLabel: '(select a ticket)',
            allowManual: true,
            manualPlaceholder: 'ticket_id',
            manualValue: pullTicket,
            onManualChange: setPullTicket
          }),
          field('destination (optional)', jsx('input', { className: inputClass, value: pullDest, onChange: e => setPullDest(e.target.value), placeholder: '<destination dir>' })),
          formCaption('Runs scripts/contributor-claim.py to fetch the accepted bytes. ' + PASTE_HINT),
          jsx('button', {
            className: btnSecondary,
            disabled: !pullTicket.trim(),
            title: 'Copy the pull prompt',
            onClick: () => copyAndLog('pull', { gateway, ticketId: pullTicket.trim(), dest: pullDest.trim() || undefined }),
            children: 'Copy pull prompt'
          })
        ]
      }),
      jsx(PromptGroup, {
        title: 'Approve or deny a version (maintainer-only)',
        children: [
          jsx(SnapshotSelect, {
            label: 'repo',
            items: sources.repos,
            value: approveRepo,
            onChange: setApproveRepo,
            emptyHint: SNAPSHOT_KIND_EMPTY.repos,
            selectLabel: '(select a repo)',
            allowManual: true,
            manualPlaceholder: 'repo_id',
            manualValue: approveRepo,
            onManualChange: setApproveRepo
          }),
          field('version (optional)', jsx('input', { className: inputClass, value: approveVersion, onChange: e => setApproveVersion(e.target.value), placeholder: 'auto' })),
          formCaption('Maintainer-only (OWNER): the maintainer gateway accepts or refuses the version; the plugin refuses for non-owners. ' + PASTE_HINT),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('button', {
                className: btnPrimary,
                disabled: !approveRepo.trim(),
                title: 'Copy the approve prompt',
                onClick: () => copyAndLog('approve', { gateway, repoId: approveRepo.trim(), version: approveVersion.trim() || undefined }),
                children: 'Copy approve prompt'
              }),
              jsx('button', {
                className: btnSecondary,
                disabled: !approveRepo.trim(),
                title: 'Copy the deny prompt (--deny)',
                onClick: () => copyAndLog('deny', { gateway, repoId: approveRepo.trim(), version: approveVersion.trim() || undefined }),
                children: 'Copy deny prompt'
              })
            ]
          })
        ]
      }),
      jsx(PromptGroup, {
        title: 'Merge an approved bundle (maintainer-only)',
        children: [
          jsx(SnapshotSelect, {
            label: 'repo',
            items: sources.repos,
            value: mergeRepo,
            onChange: setMergeRepo,
            emptyHint: SNAPSHOT_KIND_EMPTY.repos,
            selectLabel: '(select a repo)',
            allowManual: true,
            manualPlaceholder: 'repo_id',
            manualValue: mergeRepo,
            onManualChange: setMergeRepo
          }),
          field('version (optional)', jsx('input', { className: inputClass, value: mergeVersion, onChange: e => setMergeVersion(e.target.value), placeholder: '<N>' })),
          field('checkout path (optional)', jsx('input', { className: inputClass, value: mergeCheckout, onChange: e => setMergeCheckout(e.target.value), placeholder: mergeRepo.trim() ? `work/integrate/${mergeRepo.trim()}/` : 'work/integrate/<repo>/' })),
          formCaption('Maintainer-only (OWNER): the plugin never merges — the agent does it as its own git work. ' + PASTE_HINT),
          jsx('button', {
            className: btnSecondary,
            disabled: !mergeRepo.trim(),
            title: 'Copy the merge prompt',
            onClick: () => copyAndLog('merge', { gateway, repoId: mergeRepo.trim(), version: mergeVersion.trim() || undefined, checkoutPath: mergeCheckout.trim() || undefined }),
            children: 'Copy merge prompt'
          })
        ]
      }),
      jsx(PromptGroup, {
        title: 'Prune stale worktrees',
        children: [
          formCaption('Read-only audit first: the agent prints the keep/delete list before deleting anything. ' + PASTE_HINT),
          jsx('button', {
            className: btnSecondary,
            title: 'Copy the prune prompt',
            onClick: () => copyAndLog('prune', { gateway }),
            children: 'Copy prune prompt'
          })
        ]
      })
    ]
  })
}

// --- console body -------------------------------------------------------------

// The console body is a plain builder (no hooks of its own) so the guard in
// PoolConsole can catch anything thrown while the tree is assembled. Hook values
// are read in PoolConsole and passed in.
// First-run onboarding card: replaces the flow-card wall when no snapshot
// has ever landed. Both actions live inside the card.
function OnboardingCard({ onRefresh }) {
  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: 'Welcome to your Pool Console' }),
      jsx('div', {
        className: 'text-(--ui-text-secondary)',
        children: [
          jsx('div', { children: 'Pick any action in this console — add a repo, claim a ticket, approve a version — and the pane copies a ready-made prompt to your clipboard.' }),
          jsx('div', { children: 'Paste it to your agent in a Desktop chat (open any chat and paste). The agent runs the command on the gateway and replies; the reply appears here under Activity & replies.' }),
          jsx('div', { children: 'The tables at the top (Repos, Tasks, Worktrees) are live local reads — Refresh updates them without any agent round-trip. You can also just tell the agent in chat — e.g. "add the injection expansion task to demo-repo on demo-pool".' })
        ]
      }),
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          submitButton({ disabled: false, title: 'Re-read the plugin’s local state (no agent round-trip)', onClick: onRefresh, children: 'Refresh state' })
        ]
      })
    ]
  })
}

// --- pane sections (state, not commands) ------------------------------------
// Every element below reads the plugin's local computed state (via ctx.storage
// slots filled from /pane-state). No agent round-trip, no prompts.

function SectionCard({ title, action, children }) {
  return jsxs('section', {
    className: 'flex flex-col gap-2 rounded-md p-3',
    role: 'region',
    'aria-label': str(title),
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('h2', { className: 'text-sm font-semibold text-(--ui-text-primary)', children: title }),
          action || null
        ]
      }),
      children
    ]
  })
}

function Badge({ tone, children }) {
  const toneClass = tone === 'ok' ? 'text-(--ui-text-primary)' : tone === 'warn' ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-tertiary)'
  return jsx('span', { className: `rounded-full px-2 py-0.5 text-xs ${toneClass}`, children })
}

function StatChip({ label, value, tone }) {
  const valueClass = tone === 'ok' ? 'text-(--ui-text-primary)' : tone === 'bad' ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-tertiary)'
  return jsxs('div', {
    className: 'flex min-w-0 flex-col gap-0.5 rounded-md p-2',
    children: [
      jsx('span', { className: 'uppercase tracking-wide text-(--ui-text-tertiary)', style: { fontSize: '11px' }, children: label }),
      jsx('span', { className: `truncate ${valueClass}`, children: str(value) })
    ]
  })
}

function StatusStrip({ poolStatus }) {
  const ps = poolStatus && typeof poolStatus === 'object' ? poolStatus.poolStatus : null
  const tor = ps && ps.tor && typeof ps.tor === 'object' ? ps.tor : null
  const torLabel = !tor
    ? 'not reported'
    : (tor.ready && typeof tor.ready === 'object' && tor.ready.error) ? 'not ready' : tor.ready ? 'ready' : 'unknown'
  const torTone = tor ? (torLabel === 'ready' ? 'ok' : 'bad') : undefined
  const awaiting = poolStatus && Array.isArray(poolStatus.awaitingApproval) ? poolStatus.awaitingApproval.length : 0
  return jsxs('div', {
    className: 'grid grid-cols-2 gap-2 sm:grid-cols-4',
    role: 'status',
    'aria-live': 'polite',
    children: [
      jsx(StatChip, { label: 'ledger', value: ps ? str(ps.ledger || 'unknown') : 'unknown', tone: ps && ps.ledger === 'ready' ? 'ok' : undefined }),
      jsx(StatChip, { label: 'identity', value: ps ? str(ps.identity || 'unknown') : 'unknown', tone: ps && ps.identity === 'ready' ? 'ok' : undefined }),
      jsx(StatChip, { label: 'tor ready', value: torLabel + (tor && tor.socks_port ? ` (${str(tor.socks_port)}/${str(tor.control_port)})` : ''), tone: torTone }),
      jsx(StatChip, { label: 'awaiting approval', value: String(awaiting), tone: awaiting > 0 ? 'bad' : 'ok' })
    ]
  })
}

function RunBanner({ ops, refreshing }) {
  if (refreshing) {
    return jsx('div', { role: 'status', 'aria-live': 'polite', className: 'rounded-md px-2 py-1 text-xs text-(--ui-text-secondary)', children: 'Refreshing local state…' })
  }
  const active = asArray(ops).filter(o => o && (o.status === 'awaiting agent' || o.status === 'submitted'))
  if (!active.length) return null
  return jsx('div', {
    role: 'status',
    'aria-live': 'polite',
    className: 'rounded-md px-2 py-1 text-xs text-(--ui-text-secondary)',
    children: `${active.length} operation${active.length === 1 ? '' : 's'} awaiting the agent — this pane reads local state; no round-trip is pending for display.`
  })
}

function ListsBanner({ meta, sidecarDownAt, lastUpdate, hasSnapshot }) {
  if (sidecarDownAt && !hasSnapshot) {
    return jsx('div', { role: 'status', className: 'rounded-md border border-(--ui-stroke-secondary) px-2 py-1 text-xs text-(--ui-text-secondary)', children: staleStripText(lastUpdate ?? sidecarDownAt) })
  }
  const m = meta && typeof meta === 'object' ? meta : null
  if (m && m.error) {
    return jsx('div', { role: 'alert', className: 'rounded-md border border-(--ui-stroke-secondary) px-2 py-1 text-xs text-(--ui-text-primary)', children: `lists error: ${str(m.error)}` })
  }
  if (m && m.warning) {
    return jsx('div', { role: 'status', className: 'rounded-md border border-(--ui-stroke-tertiary) px-2 py-1 text-xs text-(--ui-text-secondary)', children: `lists: ${str(m.warning)}` })
  }
  return null
}

// Per-row companion: one small button that reveals a single path field and a
// copy button. Copies prompt 1 (add repo owner) with repo_id prefilled.
function RepoAddCompanion({ gateway, repoId, copyAndLog }) {
  const [open, setOpen] = useState(false)
  const [path, setPath] = useState('')
  if (!open) {
    return jsx('button', { className: btnGhost, title: 'Copy a prompt to add this repo via your agent', onClick: () => setOpen(true), children: 'Add repo' })
  }
  const pathOk = isAbsoluteLocalPath(path)
  return jsxs('div', {
    className: 'flex items-center gap-1',
    children: [
      jsx('input', { className: inputClass, value: path, onChange: e => setPath(e.target.value), placeholder: '/home/you/src/repo', autoFocus: true }),
      jsx('button', {
        className: btnGhost,
        disabled: !pathOk,
        title: pathOk ? 'Copy the add-repo prompt' : 'absolute local path only',
        onClick: () => copyAndLog('add-repo-owner', { gateway, repoId, path }),
        children: 'Copy prompt'
      })
    ]
  })
}

function RepoTable({ repos, worktrees, loading, gateway, copyAndLog }) {
  const list = asArray(repos)
  const wt = asArray(worktrees)
  const bundleCount = id => {
    const hit = wt.find(w => w && str(w.repo_id) === str(id))
    return hit && Array.isArray(hit.bundles) ? hit.bundles.length : 0
  }
  if (loading) return jsx('div', { role: 'status', className: 'text-(--ui-text-tertiary)', children: 'Loading repos…' })
  if (!list.length) {
    return jsxs('div', {
      className: 'flex flex-col items-start gap-2',
      children: [
        jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No repos in the pool yet.' }),
        jsx('button', { className: btnPrimary, onClick: () => copyAndLog('add-repo-owner', { gateway, repoId: '<repo_id>', path: '<abs_path>' }), children: 'Ask your agent to add a repo to the pool' })
      ]
    })
  }
  const cols = ['Repo', 'Name', 'Accepted', 'Open', 'Claimed', 'Total', 'Bundles', 'Ask your agent']
  return jsx('div', {
    className: 'max-h-64 overflow-auto overscroll-contain',
    children: jsxs('table', {
      className: 'w-full text-left text-xs text-(--ui-text-primary)',
      children: [
        jsx('thead', {
          children: jsx('tr', {
            children: cols.map(h => jsx('th', { key: h, className: 'sticky top-0 bg-(--ui-bg-primary) px-2 py-1 font-medium text-(--ui-text-tertiary)', children: h }))
          })
        }),
        jsx('tbody', {
          children: list.map((r, i) => jsx('tr', {
            key: `${str(r && r.repo_id)}:${i}`,
            className: 'border-t border-(--ui-stroke-tertiary)',
            children: [
              jsx('td', { key: 'id', className: 'px-2 py-1', children: str(r && r.repo_id) }),
              jsx('td', { key: 'name', className: 'px-2 py-1', children: str(r && (r.display_name || r.name)) }),
              jsx('td', { key: 'acc', className: 'px-2 py-1', children: r && r.accepted_version != null ? `v${str(r.accepted_version)}` : '—' }),
              jsx('td', { key: 'open', className: 'px-2 py-1', children: str(r && r.open_tickets) }),
              jsx('td', { key: 'claimed', className: 'px-2 py-1', children: str(r && r.claimed_tickets) }),
              jsx('td', { key: 'total', className: 'px-2 py-1', children: str(r && r.total_tickets) }),
              jsx('td', { key: 'bundles', className: 'px-2 py-1', children: str(bundleCount(r && r.repo_id)) }),
              jsx('td', { key: 'action', className: 'px-2 py-1', children: jsx(RepoAddCompanion, { gateway, repoId: str(r && r.repo_id), copyAndLog }) })
            ]
          }))
        })
      ]
    })
  })
}

function stateTone(state) {
  if (state === 'completed') return 'ok'
  if (state === 'claimed') return 'warn'
  return undefined
}

function TaskTable({ tasks, loading, gateway, copyAndLog }) {
  const [openId, setOpenId] = useState(null)
  const list = asArray(tasks)
  if (loading) return jsx('div', { role: 'status', className: 'text-(--ui-text-tertiary)', children: 'Loading tasks…' })
  if (!list.length) {
    return jsxs('div', {
      className: 'flex flex-col items-start gap-2',
      children: [
        jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No tasks fetched yet.' }),
        jsx('button', { className: btnPrimary, onClick: () => copyAndLog('fetch-tasks', { gateway }), children: 'Ask your agent to fetch the catalog' })
      ]
    })
  }
  const cols = ['', 'Ticket', 'Title', 'Repo', 'State']
  return jsx('div', {
    className: 'max-h-64 overflow-auto overscroll-contain',
    children: jsxs('table', {
      className: 'w-full text-left text-xs text-(--ui-text-primary)',
      children: [
        jsx('thead', {
          children: jsx('tr', {
            children: cols.map(h => jsx('th', { key: h, className: 'sticky top-0 bg-(--ui-bg-primary) px-2 py-1 font-medium text-(--ui-text-tertiary)', children: h }))
          })
        }),
        jsx('tbody', {
          children: list.flatMap((t, i) => {
            const id = str(t && t.ticket_id)
            const open = openId === id
            const rows = [jsx('tr', {
              key: `${id}:${i}`,
              className: 'cursor-pointer border-t border-(--ui-stroke-tertiary) hover:bg-(--ui-bg-quaternary)',
              onClick: () => setOpenId(open ? null : id),
              'aria-expanded': open,
              children: [
                jsx('td', { key: 'chev', className: 'px-2 py-1 text-(--ui-text-tertiary)', 'aria-hidden': 'true', children: open ? '▾' : '▸' }),
                jsx('td', { key: 'id', className: 'px-2 py-1', children: id }),
                jsx('td', { key: 'title', className: 'px-2 py-1', children: str(t && t.title) }),
                jsx('td', { key: 'repo', className: 'px-2 py-1', children: str(t && t.repo_id) }),
                jsx('td', { key: 'state', className: 'px-2 py-1', children: jsx(Badge, { tone: stateTone(t && t.state), children: str(t && t.state) }) })
              ]
            })]
            if (open) {
              const claimedCopy = t && t.state === 'completed'
                ? 'n/a — completed'
                : (t && t.state === 'claimed' ? 'true' : str(t && t.claimed))
              const detailChildren = [`pool: ${str(t && t.pool_id)} · claimant: ${str(t && t.claimant_key_id)} · claimed: ${claimedCopy}`]
              if (t && t.state === 'open') {
                detailChildren.push(jsx('div', {
                  key: 'assign',
                  className: 'mt-1',
                  children: jsx('button', {
                    className: btnGhost,
                    title: 'Copies the claim prompt — paste it to your agent in chat',
                    onClick: () => copyAndLog('claim', { gateway, ticketId: id }),
                    children: `Claim ticket ${id}`
                  })
                }))
              }
              rows.push(jsx('tr', {
                key: `${id}:${i}:detail`,
                className: 'border-t border-(--ui-stroke-tertiary) text-(--ui-text-tertiary)',
                children: jsx('td', {
                  colSpan: 5,
                  className: 'px-2 py-1',
                  children: detailChildren
                })
              }))
            }
            return rows
          })
        })
      ]
    })
  })
}

function WorktreeCard({ worktrees, gateway, copyAndLog }) {
  const list = asArray(worktrees)
  const pruneButton = jsx('div', {
    className: 'mt-2',
    children: jsx('button', {
      className: btnPrimary,
      title: 'Copies the prune prompt — paste it to your agent in chat',
      onClick: () => copyAndLog('prune', { gateway }),
      children: 'Prune stale worktrees'
    })
  })
  if (!list.length) {
    return jsxs('div', {
      className: 'flex flex-col',
      children: [
        jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No bundles or worktrees on file yet (work/integrate is empty).' }),
        pruneButton
      ]
    })
  }
  return jsxs('div', {
    className: 'flex flex-col',
    children: [
      jsx('ul', {
        className: 'flex flex-col gap-1 text-xs text-(--ui-text-secondary)',
        children: list.map(w => jsxs('li', {
          key: str(w && w.repo_id),
          className: 'flex items-center justify-between gap-2',
          children: [
            jsx('span', { children: `${str(w && w.repo_id)} — bundles: ${asArray(w && w.bundles).length ? asArray(w && w.bundles).map(str).join(', ') : 'none'} · worktrees: ${asArray(w && w.worktrees).length ? asArray(w && w.worktrees).map(str).join(', ') : 'none'}` }),
            jsx('button', { className: btnGhost, title: 'Copies the push prompt — paste it to your agent in chat', onClick: () => copyAndLog('push', { gateway, repoId: str(w && w.repo_id) }), children: 'Copy push prompt' })
          ]
        }))
      }),
      pruneButton
    ]
  })
}

function buildPaneBody(ctx, { gatewayState, gateway, setGateway, polling, setPolling, ops, copyAndLog, copyText, poolStatus, onRefresh, refreshing, sidecarDownAt }) {
  const isFirstRun = firstRun(ctx)
  const repos = readSnapshot(ctx, 'snapshot.repos')
  const tasks = readSnapshot(ctx, 'snapshot.tasks')
  const worktreesSnap = ctx.storage.get('snapshot.worktrees', null)
  const worktrees = worktreesSnap && Array.isArray(worktreesSnap.items) ? worktreesSnap.items : []
  const listsMeta = ctx.storage.get('snapshot.listsMeta', null)
  const hasSnapshot = Boolean(poolStatus) || repos.length > 0 || tasks.length > 0
  const lastUpdate = poolStatus && typeof poolStatus === 'object' && poolStatus.at != null ? poolStatus.at : null
  const loading = refreshing && !hasSnapshot

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-3 overflow-y-auto overscroll-contain p-3 text-sm text-(--ui-text-primary)',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('div', { className: 'font-medium', children: 'Agent Pool' }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('button', {
                className: btnGhost,
                title: 'Copies the contribute (link flow) prompt — paste it to your agent in chat',
                onClick: () => copyAndLog('add-repo-link', { gateway, repoId: '<repo_id>', path: '<abs_path>' }),
                children: 'Contribute repo'
              }),
              jsx('button', {
                className: btnPrimary,
                onClick: onRefresh,
                disabled: refreshing,
                children: refreshing ? 'Refreshing…' : 'Refresh'
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center justify-between gap-2',
        children: [
          jsx('div', { className: 'text-(--ui-text-tertiary)', children: `gateway: ${str(gatewayState)} — prompts name this profile` }),
          jsx(SegmentedControl, {
            options: [
              { id: 'default', label: 'Maintainer' },
              { id: 'pool-contrib', label: 'Contributor' }
            ],
            value: str(gateway),
            onChange: id => {
              poolPaneStore.gateway = id
              ctx.storage.set(STORAGE_KEYS.gateway, id)
              setGateway(id)
            }
          })
        ]
      }),
      jsx(StatusStrip, { poolStatus }),
      jsx(RunBanner, { ops, refreshing }),
      jsx(ToggleRow, {
        checked: polling,
        description: 'Poll the plugin’s local state every 60s. Saved in this profile’s plugin storage.',
        label: 'Polling',
        onChange: value => {
          poolPaneStore.polling = value
          ctx.storage.set(STORAGE_KEYS.polling, value)
          setPolling(value)
        }
      }),
      jsx(ListsBanner, { meta: listsMeta, sidecarDownAt, lastUpdate, hasSnapshot }),
      jsx(SectionCard, {
        title: 'Repos',
        children: jsx(RepoTable, { repos, worktrees, loading, gateway, copyAndLog })
      }),
      jsx(SectionCard, {
        title: 'Tasks',
        action: jsx('button', {
          className: btnGhost,
          title: 'Copies the ticket prompt — paste it to your agent in chat',
          onClick: () => copyAndLog('add-task', { gateway }),
          children: 'Ask your agent to open a ticket'
        }),
        children: jsx(TaskTable, { tasks, loading, gateway, copyAndLog })
      }),
      jsx(SectionCard, {
        title: 'Worktrees & bundles (local)',
        children: jsx(WorktreeCard, { worktrees, gateway, copyAndLog })
      }),
      jsx(SectionCard, {
        title: 'Ask your agent — compose a prompt',
        children: jsx(PromptCompanionSection, { ctx, ops, poolStatus, gateway, copyAndLog })
      }),
      jsx(TrackerSection, { ctx, ops, poolStatus, gateway, onRefresh, refreshing, copyAndLog, copyText }),
      isFirstRun
        ? jsx(OnboardingCard, { onRefresh })
        : null
    ]
  })
}

function PaneErrorCard({ contributionId, error }) {
  const detail = error instanceof Error ? error.message : str(error)

  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-md p-3 text-sm',
    children: [
      jsx('div', {
        className: 'font-medium text-(--ui-text-primary)',
        children: `Agent Pool console failed (${str(contributionId)})`
      }),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: str(detail) })
    ]
  })
}

// Error boundary: any render error in the console tree renders a plain string
// card instead of taking down the chat UI.
class PoolErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  render() {
    if (this.state.error) {
      return jsx(PaneErrorCard, { contributionId: this.props.contributionId, error: this.state.error })
    }
    return this.props.children
  }
}

function PoolConsole({ ctx }) {
  const gatewayState = useValue(host.state.gateway)
  const [gateway, setGateway] = useState(() => str(poolPaneStore.gateway))
  const [polling, setPolling] = useState(() => poolPaneStore.polling)
  const [ops, setOps] = useState(() => {
    const stored = ctx.storage.get(OPS_KEY, [])
    return Array.isArray(stored) ? stored : []
  })
  const [poolStatus, setPoolStatus] = useState(() => ctx.storage.get('snapshot.poolStatus', null))
  const [refreshing, setRefreshing] = useState(false)
  const [sidecarDownAt, setSidecarDownAt] = useState(null)
  // Agent→UI refresh: poll the pane-state sidecar (written by pool_pane_report,
  // served by this profile's own /pane-state route). Parsed defensively — a
  // missing/empty sidecar never throws.
  const refreshFromSidecar = async () => {
    setRefreshing(true)
    try {
      let raw = null
      try {
        raw = await ctx.rest('/pane-state')
        setSidecarDownAt(null)
        clearSnapshotLoading()
      } catch {
        raw = null
        setSidecarDownAt(Date.now())
      }
      const parsed = parsePaneState(raw)
      const { ops: nextOps } = applyOpReplies(applyPaneStateToOps(ctx.storage.get(OPS_KEY, []), parsed).ops, parsed.opReplies)
      ctx.storage.set(OPS_KEY, nextOps)
      setOps(nextOps)
      const snap = {
        at: parsed.at,
        awaitingApproval: parsed.awaitingApproval,
        budgets: parsed.budgets,
        poolStatus: parsed.poolStatus || null,
        grantToken: parsed.grantToken || null,
        transferTicket: parsed.transferTicket || null,
        manifest: parsed.manifest || null
      }
      ctx.storage.set('snapshot.poolStatus', snap)
      setPoolStatus(snap)
      const listsMeta = { at: snap.at, error: parsed.listsError || parsed.error || null, warning: parsed.listsWarning || null }
      ctx.storage.set('snapshot.listsMeta', listsMeta)
      if (parsed.tasks != null || parsed.listsError) {
        const items = Array.isArray(parsed.tasks) ? parsed.tasks : (parsed.tasks && (parsed.tasks.items || parsed.tasks.table)) || []
        ctx.storage.set('snapshot.tasks', { at: snap.at, items })
      }
      if (parsed.repos != null || parsed.listsError) {
        const items = Array.isArray(parsed.repos) ? parsed.repos : (parsed.repos && (parsed.repos.items || parsed.repos.repos)) || []
        ctx.storage.set('snapshot.repos', { at: snap.at, items })
      }
      if (parsed.claims) ctx.storage.set('snapshot.claims', { at: snap.at, items: Array.isArray(parsed.claims) ? parsed.claims : Object.values(parsed.claims) })
      if (parsed.worktrees) ctx.storage.set('snapshot.worktrees', { at: snap.at, items: parsed.worktrees })
    } finally {
      setRefreshing(false)
    }
  }
  // Mount refresh: the pane-state route is a live computed read, so one
  // re-read on mount keeps the pane honest without any agent round-trip.
  useEffect(() => { refreshFromSidecar() }, [])
  // 60s poll — only while the user left polling ON. Check storage before
  // every timer set so a user-disabled toggle is never re-enabled.
  useEffect(() => {
    if (!polling) return undefined
    if (!poolPaneStore.polling || !ctx.storage.get(STORAGE_KEYS.polling, true)) return undefined
    const id = setInterval(() => { refreshFromSidecar() }, 60000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polling])
  // Copy a companion prompt to the clipboard; approve/push/prune copies also
  // append a status:'copied' history record (no submit, ever).
  const copyAndLog = async (actionId, args) => {
    const rec = await copyPrompt(ctx, actionId, { gateway, ...(args || {}) })
    if (rec) setOps(prev => [rec, ...prev].slice(0, OPS_KEEP))
    return rec
  }
  const copyTextTo = text => copyText(ctx, text)

  try {
    return buildPaneBody(ctx, { gatewayState, gateway, setGateway, polling, setPolling, ops, copyAndLog, copyText: copyTextTo, poolStatus, onRefresh: refreshFromSidecar, refreshing, sidecarDownAt })
  } catch (error) {
    return jsx(PaneErrorCard, { contributionId: CONTRIBUTION_IDS.page, error })
  }
}

// --- full page -------------------------------------------------------------
// The /agent-pool route renders the full console (gateway selector, status strip,
// tables, prompt companions, the Activity & replies history) inside an error boundary.

// --- settings --------------------------------------------------------------

function GeneralSettings() {
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsx(ListRow, {
        description: 'Open the full console at /agent-pool (left sidebar nav row / deep link).',
        title: 'Where the console lives'
      }),
      jsx(ListRow, {
        description: 'This plugin contributes a page and a sidebar row; each is namespaced agent-pool:<id>.',
        title: 'Contributions'
      })
    ]
  })
}

function GatewaysSettings() {
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: KNOWN_GATEWAYS.map(gateway =>
      jsx(ListRow, {
        description: gateway.description,
        key: gateway.id,
        title: `${gateway.id} — ${gateway.role}`
      })
    )
  })
}

function DisplaySettings({ storage }) {
  const [compact, setCompact] = useState(() => Boolean(storage.get(DISPLAY_COMPACT_KEY, false)))

  const update = value => {
    setCompact(value)
    storage.set(DISPLAY_COMPACT_KEY, value)
  }

  return jsx(ToggleRow, {
    checked: compact,
    description: 'Render the console rows compactly. Saved in this profile’s plugin storage.',
    label: 'Compact rows',
    onChange: update
  })
}

// --- plugin ----------------------------------------------------------------

export default {
  id: 'agent-pool',
  name: 'Agent Pool',
  register(ctx) {
    hydratePaneStore(ctx.storage)

    // The full-page Pool Console: a /agent-pool route plus the left sidebar nav
    // row that links to it. No right-dock pane — the console is a full page.
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/agent-pool' },
        render: () => jsx(PoolErrorBoundary, {
          contributionId: CONTRIBUTION_IDS.page,
          children: jsx(PoolConsole, { ctx })
        })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/agent-pool', label: 'Agent Pool', codicon: 'repo' }
      }
    ])

    // Settings ▸ Plugins ▸ Agent Pool (▸ Gateways, ▸ Display). Feature-detected
    // so the plugin still loads on hosts that predate the helper.
    ctx.registerSettingsPage?.({
      id: 'settings',
      title: 'Agent Pool',
      icon: 'repo',
      order: 0,
      render: () => jsx(GeneralSettings, {}),
      children: [
        {
          id: 'gateways',
          title: 'Gateways',
          render: () => jsx(GatewaysSettings, {})
        },
        {
          id: 'display',
          title: 'Display',
          render: () => jsx(DisplaySettings, { storage: ctx.storage })
        }
      ]
    })
  }
}

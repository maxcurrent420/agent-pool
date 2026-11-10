// Agent Pool — Hermes Desktop plugin ("Pool Console").
//
// The console lives on its own full page at /agent-pool (sidebar row: Agent
// Pool). The desktop host loads this file UNCOMPILED,
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
// approve, merge, discover}. Older builds wrote 'add-repo' — migrateOps() upgrades
// those records in place so prior ops survive.

export const OP_KINDS = ['add-repo-owner', 'add-repo-link', 'fetch-tasks', 'claim', 'pull', 'push', 'approve', 'merge', 'discover']

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
  const empty = { poolStatus: null, tasks: null, claims: null, discovered: null, awaitingApproval: [], budgets: [], at: null, substeps: null, opReplies: {} }
  if (!raw || typeof raw !== 'object') return empty
  const ps = raw.pool_status || raw.poolStatus || raw.status || null
  const parsed = { ...empty, at: typeof raw.at === 'number' ? raw.at : (typeof raw.updated_at === 'number' ? raw.updated_at : Date.now()) }
  if (ps && typeof ps === 'object') {
    parsed.poolStatus = ps
    parsed.awaitingApproval = extractAwaitingApproval(ps)
    parsed.budgets = extractBudgets(ps)
  }
  const tasks = raw.tasks || raw.task_list || null
  if (tasks) parsed.tasks = tasks
  const claims = raw.claims || null
  if (claims) parsed.claims = claims
  // Free-form discovery reply (agent-enumerated git repos). Accept either a
  // pre-split list or a raw detail string; the refresh loop stores it under
  // snapshot.discovered.
  const discovered = raw.discovered || raw.discovery || raw.detail || null
  if (discovered) parsed.discovered = discovered
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

// retryLegality(op): which retry action (if any) a row may offer.
//   {push, failed}                → 'deliver'  (grant is not consumed by deliver)
//   any op failed 'composer unreachable' → 'resubmit' (re-submits op.text verbatim)
//   refused / anything else       → null (a plugin refusal is not transient)
export function retryLegality(op) {
  if (!op || typeof op !== 'object') return null
  if (op.status === 'refused') return null
  if (op.status !== 'failed') return null
  const err = str(op.error || '')
  const sub = str((op.args && op.args.substatus) || '')
  if (/composer unreachable/i.test(err)) return 'resubmit'
  if (op.kind === 'push') return 'deliver'
  if (/deliver/i.test(err) || /deliver/i.test(sub)) return 'deliver'
  return null
}

// --- ctx.storage snapshot schema (defensive: malformed reads return []) -----
// Every snapshot lives under a 'snapshot.*' key as { at: <ms>, items: [...] }.
// Snapshots are a cache of what the agent last reported — never a source of
// truth, never written by the pane except as a timestamped slot.
export const SNAPSHOT_KEYS = ['snapshot.repos', 'snapshot.tasks', 'snapshot.claims', 'snapshot.discovered', 'snapshot.poolStatus', 'snapshot.gateways']

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

// One literal prompt asking the agent to enumerate local git repositories.
// This instructs the AGENT — the pane itself never touches the filesystem.
// Discovery is outside the slash-command manifest parity set on purpose: it
// names no plugin command.
export function discoverReposPrompt() {
  return 'Discover local git repositories for the pool console: run `git -C ~/Documents rev-parse --is-inside-work-tree` style discovery — list the git repositories under ~/Documents and ~/projects with their absolute paths (one per line, format: <path> — <first remote or directory name>). Do not open, read, or modify any files inside them; just enumerate the worktrees. Reply with the list.'
}

// One click, four data sources, still one literal prompt.
export const FULL_REFRESH_PROMPT = 'Report pool status and lists now: run pool_status, run /fetch repo list, run /fetch task list, and call pool_pane_report with all the results (pool_status, repo list, task list).'

// refreshSnapshot(ctx, sessionId, name, gateway): submit the refresh prompt
// for one snapshot via the composer. 'discover' is tracked as an op of kind
// 'discover'; other names ride the plain refresh path. Returns the submit
// result (or null for unknown names / static snapshots like gateways).
export function refreshSnapshot(ctx, sessionId, name, gateway) {
  const gw = gateway || 'default'
  const prompts = {
    repos: fetchRepoListPrompt({ gateway: gw }),
    tasks: fetchTaskListPrompt({ gateway: gw }),
    poolStatus: BATCHED_STATUS_PROMPT,
    full: FULL_REFRESH_PROMPT,
    discovered: discoverReposPrompt()
  }
  const text = prompts[name]
  if (!text) return Promise.resolve(null)
  const flagNames = name === 'full' ? ['full', 'repos', 'tasks', 'poolStatus'] : [name]
  for (const flag of flagNames) setSnapshotLoading(flag, true)
  const clear = () => { for (const flag of flagNames) setSnapshotLoading(flag, false) }
  if (name === 'discovered') {
    return submitOp(ctx, sessionId, { kind: 'discover', gateway: gw, args: { snapshot: 'snapshot.discovered' }, text })
      .then(result => { if (!result || result.status === 'failed') clear(); return result })
  }
  return Promise.resolve(host.composer.submit(sessionId, text)).then(ok => { if (!ok) clear(); return ok })
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

// The one batched UI→agent prompt used for the initial/fallback refresh.
export const BATCHED_STATUS_PROMPT = 'Report pool status now: run pool_status and /fetch repo list, call pool_pane_report with the results.'

// Audit view prompt (composed, submitted through the composer like any op).
export function auditShowPrompt(gateway, ticketId) {
  const t = (ticketId || '').trim()
  return `Using the ${gateway} gateway profile: run /audit show${t ? ` ${t}` : ''} and report the result verbatim.`
}

// --- inlined from prompts.js (runtime import allowlist: no relative imports) ---
// Agent Pool — prompt builders for the desktop pane.
//
// Pure functions, no SDK import: each builder turns form state into the exact
// prompt text the pane submits to the bound agent session. Every prompt
// names the gateway profile, carries the literal slash command or script
// invocation with the form's fields as args, and asks the agent to return
// the plugin's reply verbatim. The pane never free-hands an operation.

function replySuffix() {
  return 'Return the plugin\'s reply verbatim.'
}

export function addRepoOwnerPrompt({ gateway, repoId, path, version }) {
  const versionFlag = version ? ` --version ${version}` : ''
  return `On the ${gateway} gateway, register this repo in the pool and ingest it as a new version: run /add-repo ${repoId} --path ${path}${versionFlag}. If the user did not supply a version and the pool already has accepted versions, first ask the maintainer gateway /fetch repo ${repoId} for accepted_version and use the next one. ${replySuffix()}`
}

export function addRepoLinkPrompt({ gateway, repoId, path, version, maintainerGateway }) {
  const versionFlag = version ? ` --version ${version}` : ''
  const target = maintainerGateway || 'the designated pool maintainer'
  return `On the ${gateway} gateway, link this repo into a designated pool (two steps, one tracked operation): (1) run scripts/contributor-add.py ${path} --repo ${repoId}${versionFlag} and capture the printed manifest_json and transfer_ticket; (2) send /add-repo --deliver <transfer_ticket> --manifest '<manifest_json>' --repo ${repoId} to ${target}'s bot. Do not use remote URLs — local paths only. ${replySuffix()}`
}

export function fetchRepoListPrompt({ gateway }) {
  return `On the ${gateway} gateway, list the pools and repos: run /fetch repo list. ${replySuffix()}`
}

export function fetchTaskListPrompt({ gateway, repoId }) {
  const filter = repoId ? ` --repo ${repoId}` : ''
  return `On the ${gateway} gateway, bootstrap the catalog and list tasks: run /fetch repo list, then run /fetch task list${filter}. Reply with the task table. ${replySuffix()}`
}

export function claimPrompt({ gateway, ticketId, maxTasks }) {
  if (maxTasks && !ticketId) {
    return `On the ${gateway} gateway, auto-assign work: run /claim --any-project --max-tasks ${maxTasks}. Save the reply's worktree_path, branch (pool/<ticket_id>), base_ref and budget into the reply text so the pane can record them. ${replySuffix()}`
  }
  return `On the ${gateway} gateway, claim this ticket: run /claim ${ticketId}. Save the reply's worktree_path, branch (pool/${ticketId}), base_ref and budget into the reply text so the pane can record them. ${replySuffix()}`
}

export function pullPrompt({ gateway, repoId, ticketId, replyFile, dest }) {
  const ticket = ticketId || repoId
  const replyRef = replyFile ? ` --reply-file ${replyFile}` : ' --reply-file <saved claim reply json>'
  const destRef = dest || '<destination dir>'
  return `On the ${gateway} gateway, fetch the accepted bytes for ticket ${ticket} into a worktree: run scripts/contributor-claim.py ${ticket}${replyRef} --dest ${destRef}. Show the resulting worktree_path and branch. ${replySuffix()}`
}

export function pushGrantPrompt({ gateway, repoId, ticketId }) {
  return `On the ${gateway} gateway, request a push grant: run /push repo ${repoId} --ticket ${ticketId}. Return the grant token verbatim. ${replySuffix()}`
}

export function pushPackagePrompt({ gateway, worktreePath, repoId, poolId, version }) {
  return `On the ${gateway} gateway, package the completed work: run scripts/contributor-push.py ${worktreePath} --repo ${repoId} --pool ${poolId} --version ${version}. Take next_version from the grant reply or re-ask /fetch repo ${repoId} for accepted_version; never trust enroll-time chain_tips. ${replySuffix()}`
}

export function pushDeliverPrompt({ gateway, repoId, version, ticketJson, transferTicket, grantToken }) {
  const tokenFlag = grantToken ? ` --token ${grantToken}` : ' --token <grant_token>'
  return `On the ${gateway} gateway, deliver the packaged repo ${repoId} version ${version}: run /push repo --deliver ${transferTicket}${tokenFlag} --manifest '${ticketJson}'. Note: a redelivery is legal — the grant is not consumed by deliver — so offer "Retry deliver" on transport failure. ${replySuffix()}`
}

export function approvePrompt({ gateway, repoId, version, ticketId, deny }) {
  const denyFlag = deny ? ' --deny' : ''
  const target = ticketId
    ? `--ticket ${ticketId}`
    : `--repo ${repoId}${version ? ` --version ${version}` : ''}`
  return `On the ${gateway} gateway, OWNER ONLY: run scripts/pool-approve.py ${target}${denyFlag}. If the plugin refuses because this profile is not the pool owner, report the refusal verbatim and do not retry. ${replySuffix()}`
}

export function mergePrompt({ gateway, repoId, ticketId, checkoutPath }) {
  const target = ticketId ? `ticket ${ticketId}` : `repo ${repoId}`
  const dest = checkoutPath || `work/integrate/${repoId}/`
  return `On the ${gateway} gateway, OWNER ONLY: merge the approved bundle for ${target}: the plugin never merges — run the merge yourself. git fetch <approved bundle> and git merge it into the canonical checkout at ${dest}. Where the maintainer flow calls the merge_approved service path (tickets.py:2249), do it through the plugin's documented flow, never a side import. If the audit is not clean, there is no accepted bundle for the version, or the plugin refuses because this profile is not the pool owner, report the refusal/failure verbatim and do not retry. ${replySuffix()}`
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
  tor: 'status.tor',
  highlighted: 'ops.highlighted',
  collapsed: id => `display.collapsed.${id}`
}

// Module-level holder for pane state, hydrated from plugin-scoped storage at
// register time so it survives close/reopen of the pane.
export const poolPaneStore = {
  gateway: 'default',
  polling: false,
  tor: null,
  highlight: null,
  collapsed: {}
}

function hydratePaneStore(storage) {
  poolPaneStore.gateway = storage.get(STORAGE_KEYS.gateway, 'default')
  // Default OFF; respect whatever the user last persisted.
  poolPaneStore.polling = Boolean(storage.get(STORAGE_KEYS.polling, false))
  poolPaneStore.tor = storage.get(STORAGE_KEYS.tor, null)
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
  host.notify({
    title: kind,
    message,
    detail,
    kind: kind === 'failed' || kind === 'refused' ? 'error' : 'info'
  })
  if (detail != null && typeof detail === 'object' && detail.id != null) {
    poolPaneStore.highlight = detail.id
    ctx.storage.set(STORAGE_KEYS.highlighted, detail.id)
  }
}

// Build, submit and track one operation. On a falsy composer result the op is
// marked failed and nothing is recorded as sent; on success it is appended to
// ctx.storage['ops'] (last 200, newest first) as 'awaiting agent'. Status
// transitions beyond that arrive with the Task 8 refresh loop.
export async function submitOp(ctx, sessionId, op) {
  const record = {
    id: op.id || `op-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    kind: normalizeKind(op.kind),
    gateway: op.gateway,
    args: op.args || {},
    status: 'submitted',
    at: Date.now(),
    replyRef: null,
    text: op.text != null ? str(op.text) : ''
  }

  const ok = await host.composer.submit(sessionId, op.text)

  if (!ok) {
    record.status = 'failed'
    record.error = 'composer unreachable'
    notifyOp(ctx, 'failed', 'composer unreachable', { id: record.id })
    return record
  }

  record.status = 'awaiting agent'
  const ops = ctx.storage.get(OPS_KEY, [])
  const next = [record, ...(Array.isArray(ops) ? ops : [])].slice(0, OPS_KEEP)
  ctx.storage.set(OPS_KEY, next)
  notifyOp(ctx, 'info', `${record.kind} submitted — awaiting agent`, { id: record.id })
  return record
}

const OP_STATUS_PILL = {
  submitted: 'submitted',
  'awaiting agent': 'awaiting agent',
  'done (awaiting approval)': 'done (awaiting approval)',
  done: 'done',
  refused: 'refused',
  failed: 'failed'
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

// --- Task 4: one primary button per flow + confirm gates -------------------
// The seven primary flows. Exactly one primary button per flow carries one of
// these labels; everything else is a sub-step or a secondary action.
export const PRIMARY_BUTTONS = {
  addRepo: 'Add repo',
  addRepoLink: 'Contribute repo',
  discover: 'Discover repos',
  fetchTasks: 'Fetch tasks',
  claim: 'Claim',
  pull: 'Pull repo',
  push: 'Submit work (push)',
  approve: 'Approve',
  deny: 'Deny',
  merge: 'Merge'
}

// buildConfirmCard(target, gateway, commandText): pure. The confirm card that
// stands between the primary button and the submit. `text` names the target in
// plain words and carries the literal command the prompt will run.
export function buildConfirmCard(target, gateway, commandText) {
  const t = str(target)
  const gw = str(gateway)
  const cmd = str(commandText)
  const text = `${t} on gateway \`${gw}\`? This asks the agent to run \`${cmd}\`.`
  return { target: t, gateway: gw, commandText: cmd, banner: 'OWNER ONLY', text }
}

// The literal command strings the confirm card shows and the golden tests pin.
export function approveCommandText({ repoId, version, ticketId, deny } = {}) {
  const target = ticketId
    ? `--ticket ${ticketId}`
    : `--repo ${repoId}${version != null && version !== '' ? ` --version ${version}` : ''}`
  return `scripts/pool-approve.py ${target}${deny ? ' --deny' : ''}`
}

export function mergeCommandText({ repoId, ticketId, checkoutPath } = {}) {
  const target = ticketId ? `ticket ${ticketId}` : `repo ${repoId}`
  return `git fetch <approved bundle> && git merge into ${checkoutPath || `work/integrate/${repoId}/`} (${target})`
}

// finalizeOp(draft, confirmState): pure. Returns null unless the operator
// pressed Confirm — the op can never compose without confirmed === true.
export function finalizeOp(draft, confirmState) {
  if (!confirmState || confirmState.confirmed !== true) return null
  return draft
}

const inputClass = 'rounded-md px-2 py-1 text-(--ui-text-primary)'
const labelClass = 'text-(--ui-text-secondary)'

// Reusable snapshot-fed dropdown. Opt-in manual fallback per field per
// session (never persisted); refresh button disabled while an op of the
// refresh kind is 'awaiting agent'.
function SnapshotSelect({ label, items, value, onChange, emptyHint, refreshAction, refreshKind, ops, allowManual, manualPlaceholder, manualValue, onManualChange, selectLabel, resetKey, loadingKey }) {
  const [manual, setManual] = useState(false)
  const loading = useSnapshotLoading(loadingKey)
  // Flow reset: the manual disclosure is closed by default and un-checks
  // itself whenever the flow's reset key changes.
  useEffect(() => { setManual(false) }, [resetKey])
  const list = asArray(items)
  const options = buildOptions(list, emptyHint || 'Nothing loaded yet', selectLabel, Boolean(allowManual))
  const refreshAwaiting = refreshKind
    ? asArray(ops).some(op => op && op.kind === refreshKind && op.status === 'awaiting agent')
    : false
  const childNodes = [
    jsxs('div', {
      key: 'row',
      className: 'flex items-center justify-between gap-2',
      children: [
        jsx('span', { className: labelClass, children: label }),
        refreshAction
          ? jsx('button', {
              className: 'rounded-md px-1.5 py-0.5 text-(--ui-text-secondary)',
              disabled: refreshAwaiting,
              title: refreshAwaiting ? 'Refresh already awaiting agent' : 'Refresh',
              onClick: refreshAction,
              children: '↻'
            })
          : null
      ]
    })
  ]
  if (manual) {
    childNodes.push(jsxs('div', {
      key: 'manual',
      className: 'flex items-center gap-1',
      children: [
        jsx('input', { className: inputClass, value: manualValue || '', onChange: e => onManualChange && onManualChange(e.target.value), placeholder: manualPlaceholder || '', autoFocus: true }),
        jsx('button', { className: 'rounded-md px-2 py-1 text-(--ui-text-secondary)', title: 'Back to the list', onClick: () => setManual(false), children: 'list' })
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
  const discRaw = readSnapshot(ctx, 'snapshot.discovered')
  const discovered = filterDiscovered(discRaw)
  return { repos, tasks, claims, poolStatus: ps, awaitingApproval: awaiting, gateways, discovered, discoveredRaw: discRaw.length }
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

function failureHints(items) {
  return jsxs('div', {
    className: 'flex flex-col gap-0.5 text-(--ui-text-tertiary)',
    children: items.map(item => jsx('div', { key: item, children: `• ${item}` }))
  })
}

// The two-step confirm gate for owner actions (Approve / Deny / Merge).
// Shows the plain-words target, the literal command the prompt will carry and
// the OWNER ONLY banner. The op fires only on Confirm.
function ConfirmCard({ card, onConfirm, onCancel }) {
  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: card.banner }),
      jsx('div', { className: 'text-(--ui-text-primary)', children: str(card.text) }),
      jsxs('div', {
        className: 'text-(--ui-text-secondary)',
        children: [jsx('span', { children: 'command: ' }), jsx('code', { children: str(card.commandText) })]
      }),
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          submitButton({ disabled: false, title: undefined, onClick: onConfirm, children: 'Confirm' }),
          jsx('button', { className: 'rounded-md px-3 py-1.5 text-(--ui-text-secondary)', title: 'Back to the form', onClick: onCancel, children: 'Cancel' })
        ]
      })
    ]
  })
}

function submitButton({ disabled, title, children, onClick }) {
  return jsx('button', {
    className: 'rounded-md px-3 py-1.5 text-left text-(--ui-text-primary)',
    disabled,
    title: disabled ? title : undefined,
    onClick,
    children
  })
}

// --- form: add repo ---------------------------------------------------------

const ADD_REPO_FAILURES = [
  'add-repo: --link is not supported (the pane forbids remote URLs — local absolute paths only)',
  'not authorized for pool <id> (whitelist refusal)',
  'manifest signature / transport failure',
  'version too low — pass an explicit --version greater than the tip'
]

function AddRepoForm({ ctx, sessionId, gateway, noSession, submitTitle, recordOp, ops }) {
  const snapSrc = snapshotSources(ctx)
  const discovered = snapSrc.discovered
  const skipped = Math.max(0, snapSrc.discoveredRaw - discovered.length)
  const pools = itemsFor({ repos: snapSrc.repos, poolStatus: snapSrc.poolStatus }, 'pools')
  const gatewayItems = snapSrc.gateways.map(g => ({ id: str(g && g.id), repo: str((g && g.role) || '') }))
  const discoveredItems = discovered.map(d => ({ id: d.path, title: d.path }))
  const [mode, setMode] = useState('owner')
  const [discoveredId, setDiscoveredId] = useState('')
  const [manualPath, setManualPath] = useState('')
  const [poolId, setPoolId] = useState('')
  const [version, setVersion] = useState('')
  const [maintainerGateway, setMaintainerGateway] = useState('default')

  const selection = resolveAddRepoSelection({ discoveredId, manualPath, discovered })
  const pathOk = selection !== null
  const manualPathActive = Boolean(manualPath.trim())
  const versionOk = !version.trim() || /^\d+$/.test(version.trim())
  const ready = !noSession && pathOk && versionOk
  const disabledTitle = noSession
    ? submitTitle
    : !pathOk
      ? (manualPathActive
          ? 'path must be an absolute local path (no URLs)'
          : 'pick a discovered repo first')
    : !versionOk ? '--version must be a positive integer'
    : undefined

  const onSubmit = async () => {
    if (!ready || !selection) return
    const v = version.trim() ? Number(version.trim()) : undefined
    const text = mode === 'owner'
      ? addRepoOwnerPrompt({ gateway, repoId: selection.repoId, path: selection.path, version: v })
      : addRepoLinkPrompt({ gateway, repoId: selection.repoId, path: selection.path, version: v, maintainerGateway })
    await recordOp({
      kind: mode === 'owner' ? 'add-repo-owner' : 'add-repo-link',
      gateway,
      args: mode === 'owner'
        ? { mode, repoId: selection.repoId, path: selection.path, poolId: poolId || null, version: v ?? null }
        : { mode, repoId: selection.repoId, path: selection.path, version: v ?? null, maintainerGateway, substeps: ['build bundle', 'deliver'], substepsDone: [] },
      text
    })
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: 'Add repo' }),
      jsx(SegmentedControl, {
        options: [
          { id: 'owner', label: 'My pool (owner)' },
          { id: 'link', label: 'Contribute (link flow)' }
        ],
        value: mode,
        onChange: setMode
      }),
      jsx(SnapshotSelect, {
        label: 'repo (discovered) — or enter manually…',
        items: discoveredItems,
        value: manualPathActive ? '' : discoveredId,
        onChange: setDiscoveredId,
        emptyHint: 'No discovered repos yet — click Discover repos',
        refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'discovered', gateway) : undefined,
        loadingKey: 'discovered',
        refreshKind: 'discover',
        ops,
        allowManual: true,
        manualPlaceholder: '/home/you/src/my-repo',
        manualValue: manualPath,
        onManualChange: setManualPath,
        selectLabel: '(select a discovered repo)',
        resetKey: mode
      }),
      skipped > 0 ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: `${skipped} entries skipped (non-local paths)` }) : null,
      manualPathActive && !isAbsoluteLocalPath(manualPath)
        ? jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Path must be absolute (starts with /) and must not look like a URL.' })
        : null,
      mode === 'owner'
        ? jsx(SnapshotSelect, {
            label: 'target pool',
            items: pools,
            value: poolId,
            onChange: setPoolId,
            emptyHint: SNAPSHOT_KIND_EMPTY.pools,
            refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'full', gateway) : undefined,
        loadingKey: 'poolStatus',
            ops,
            allowManual: false,
            selectLabel: '(select a pool)',
            resetKey: mode
          })
        : null,
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'version auto-resolves (agent asks /fetch repo for accepted_version)' }),
      version.trim() && !/^\d+$/.test(version.trim())
        ? jsx('div', { className: 'text-(--ui-text-secondary)', children: '--version must be a positive integer.' })
        : null,
      jsx(SnapshotSelect, {
        label: '--version (optional)',
        items: [],
        value: version,
        onChange: setVersion,
        emptyHint: SNAPSHOT_KIND_EMPTY.version,
        allowManual: true,
        manualPlaceholder: 'N',
        manualValue: version,
        onManualChange: setVersion,
        resetKey: mode
      }),
      mode === 'link'
        ? jsx(SnapshotSelect, {
            label: 'maintainer gateway (profile)',
            items: gatewayItems,
            value: maintainerGateway,
            onChange: setMaintainerGateway,
            emptyHint: SNAPSHOT_KIND_EMPTY.gateways,
            allowManual: false,
            selectLabel: '(select a gateway)',
            resetKey: mode
          })
        : null,
      jsx('div', { className: 'flex items-center gap-2', children: [
        submitButton({
          disabled: noSession,
          title: noSession ? submitTitle : 'Ask the agent to enumerate local git repositories (read-only)',
          onClick: async () => {
            if (noSession) return
            await recordOp({ kind: 'discover', gateway, args: { snapshot: 'snapshot.discovered' }, text: discoverReposPrompt() })
          },
          children: 'Discover repos'
        })
      ] }),
      failureHints(ADD_REPO_FAILURES),
      submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: mode === 'owner' ? PRIMARY_BUTTONS.addRepo : PRIMARY_BUTTONS.addRepoLink })
    ]
  })
}

// --- form: fetch task list --------------------------------------------------

function FetchTasksForm({ ctx, gateway, noSession, submitTitle, recordOp, sessionId, ops }) {
  const repos = snapshotSources(ctx).repos
  const [repoFilter, setRepoFilter] = useState('')
  const ready = !noSession

  const onSubmit = async () => {
    if (!ready) return
    const filter = repoFilter.trim() || undefined
    const text = fetchTaskListPrompt({ gateway, repoId: filter })
    const record = await recordOp({
      kind: 'fetch-tasks',
      gateway,
      args: { repo: filter || null, sequence: ['/fetch repo list', `/fetch task list${filter ? ` --repo ${filter}` : ''}`] },
      text
    })
    // Agent reply snapshot is stored by the refresh loop (Task 8); seed the
    // key with a timestamp so offline display has a slot to read.
    ctx.storage.set('snapshot.tasks', { at: Date.now(), opId: record?.id || null, table: null })
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: 'Fetch task list' }),
      jsx(SnapshotSelect, {
        label: 'repo filter (optional)',
        items: repos,
        value: repoFilter,
        onChange: setRepoFilter,
        emptyHint: SNAPSHOT_KIND_EMPTY.repos,
        selectLabel: '(all repos)',
        refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'repos', gateway) : undefined,
        loadingKey: 'repos',
        ops,
        allowManual: true,
        manualPlaceholder: 'repo_id',
        manualValue: repoFilter,
        onManualChange: setRepoFilter,
        resetKey: 'fetch'
      }),
      submitButton({ disabled: !ready, title: noSession ? submitTitle : undefined, onClick: onSubmit, children: PRIMARY_BUTTONS.fetchTasks })
    ]
  })
}

// --- form: claim / assign ---------------------------------------------------

const CLAIM_FAILURES = [
  'claim refused: user <id> already bound to key <key> (immutable key binding)',
  'agent budget exhausted: <n> of <allocation> tasks completed',
  'not whitelisted for this pool'
]

function ClaimForm({ ctx, gateway, noSession, submitTitle, recordOp, sessionId, ops }) {
  const tasks = snapshotSources(ctx).tasks
  const [ticket, setTicket] = useState('')
  const [note, setNote] = useState('')
  const [autoAssign, setAutoAssign] = useState(false)
  const [maxTasks, setMaxTasks] = useState('1')

  const ticketReady = autoAssign ? (/^\d+$/.test(maxTasks.trim()) && Number(maxTasks.trim()) > 0) : Boolean(ticket.trim())
  const ready = !noSession && ticketReady
  const disabledTitle = noSession
    ? submitTitle
    : autoAssign ? (ticketReady ? undefined : 'max-tasks must be a positive integer')
    : 'pick a ticket first'

  const onSubmit = async () => {
    if (!ready) return
    const noteSuffix = note.trim() ? ` Claimant identity note: ${note.trim()}.` : ''
    const text = autoAssign
      ? claimPrompt({ gateway, maxTasks: Number(maxTasks.trim()) }) + noteSuffix
      : claimPrompt({ gateway, ticketId: ticket.trim() }) + noteSuffix
    const key = autoAssign ? `auto-${maxTasks.trim()}` : ticket.trim()
    const record = await recordOp({
      kind: 'claim',
      gateway,
      args: { ticket: autoAssign ? null : key, autoAssign, maxTasks: autoAssign ? Number(maxTasks.trim()) : null, note: note.trim() || null },
      text
    })
    const claims = ctx.storage.get('snapshot.claims', {})
    ctx.storage.set('snapshot.claims', { ...(claims && typeof claims === 'object' ? claims : {}), [key]: { at: Date.now(), opId: record?.id || null, reply: null } })
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: 'Claim / assign' }),
      jsx(ToggleRow, {
        checked: autoAssign,
        description: 'Use /claim --any-project --max-tasks N instead of claiming one ticket.',
        label: 'Auto-assign',
        onChange: setAutoAssign
      }),
      autoAssign
        ? jsxs('div', { className: 'flex flex-col gap-1', children: [
            field('--max-tasks N', jsx('input', { className: inputClass, value: maxTasks, onChange: e => setMaxTasks(e.target.value), inputMode: 'numeric' })),
            !/^\d+$/.test(maxTasks.trim()) || Number(maxTasks.trim()) <= 0
              ? jsx('div', { className: 'text-(--ui-text-secondary)', children: 'max-tasks must be a positive integer.' })
              : null
          ] })
        : jsxs('div', {
            className: 'flex flex-col gap-2',
            children: [
              jsx(SnapshotSelect, {
                label: 'ticket (from fetched task snapshot)',
                items: tasks,
                value: ticket,
                onChange: setTicket,
                emptyHint: SNAPSHOT_KIND_EMPTY.tasks,
                refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'tasks', gateway) : undefined,
        loadingKey: 'tasks',
                refreshKind: 'fetch-tasks',
                ops,
                allowManual: true,
                manualPlaceholder: 'ticket_id',
                manualValue: ticket,
                onManualChange: setTicket,
                resetKey: String(autoAssign)
              })
            ]
          }),
      field('claimant identity note (optional)', jsx('input', { className: inputClass, value: note, onChange: e => setNote(e.target.value), placeholder: 'who is claiming, for the record' })),
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: `target profile: ${str(gateway)} (plugin binds claim to key + Discord id automatically)` }),
      failureHints(CLAIM_FAILURES),
      submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: PRIMARY_BUTTONS.claim })
    ]
  })
}

// Update a tracked op in ctx.storage (merge args, patch status). Used by the
// push card to keep ONE op of kind 'push' across its three sub-steps.
function updateOp(ctx, id, patch) {
  const ops = ctx.storage.get(OPS_KEY, [])
  if (!Array.isArray(ops)) return null
  let updated = null
  const next = ops.map(op => {
    if (op.id !== id) return op
    updated = { ...op, ...patch, args: { ...(op.args || {}), ...((patch && patch.args) || {}) } }
    return updated
  })
  ctx.storage.set(OPS_KEY, next)
  return updated
}

function claimOps(ops) {
  return (Array.isArray(ops) ? ops : []).filter(op => op && op.kind === 'claim')
}

const PULL_FAILURES = [
  'manifest signature does not verify — do not use the bytes; re-read or re-issue the ticket',
  'transport errors: "Tor is not ready, the ticket expired" — re-read the ticket or ask the maintainer to re-issue it',
  'destination not writable — pick another --dest directory'
]

// --- form: pull repo --------------------------------------------------------

function PullRepoForm({ ctx, gateway, noSession, submitTitle, recordOp, ops }) {
  const claims = claimOps(ops)
  const claimItems = claims.map(op => ({
    id: str(op && op.id),
    title: `${str((op && op.args && (op.args.ticket || op.args.key)) || (op && op.id) || '')}${op && op.at ? ` · ${new Date(op.at).toLocaleString()}` : ''}`
  }))
  const [claimId, setClaimId] = useState('')
  const [ticketId, setTicketId] = useState('')
  const [replyFile, setReplyFile] = useState('')
  const [dest, setDest] = useState('')

  const selected = claims.find(op => op.id === claimId) || null
  const effectiveTicket = (selected && selected.args && (selected.args.ticket || selected.args.key)) || ticketId.trim()
  const effectiveReplyFile = replyFile.trim() || (selected && selected.args && selected.args.replyFile) || ''
  const ready = !noSession && Boolean(effectiveTicket) && Boolean(dest.trim()) && isAbsoluteLocalPath(dest)
  const disabledTitle = noSession
    ? submitTitle
    : !effectiveTicket ? 'pick a ticket first'
    : !dest.trim() ? 'enter an absolute destination directory'
    : !isAbsoluteLocalPath(dest) ? 'destination must be an absolute local path'
    : undefined

  const onPickClaim = id => {
    setClaimId(id)
    const op = claims.find(o => o.id === id)
    if (op && op.args) {
      setTicketId(op.args.ticket || op.args.key || '')
      setReplyFile(op.args.replyFile || '')
    }
  }

  const onSubmit = async () => {
    if (!ready) return
    const text = pullPrompt({ gateway, ticketId: effectiveTicket, replyFile: effectiveReplyFile || undefined, dest: dest.trim() })
    await recordOp({
      kind: 'pull',
      gateway,
      args: { ticket: effectiveTicket, replyFile: effectiveReplyFile || null, dest: dest.trim(), substatus: 'pull' },
      text
    })
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: 'Pull repo (contributor-claim.py)' }),
      jsx(SnapshotSelect, {
        label: 'prefill from claim op',
        items: claimItems,
        value: claimId,
        onChange: onPickClaim,
        emptyHint: SNAPSHOT_KIND_EMPTY.claims,
        allowManual: false,
        selectLabel: '(pick a claim op)'
      }),
      field('ticket id', jsx('input', { className: inputClass, value: effectiveTicket, onChange: e => { setClaimId(''); setTicketId(e.target.value) }, placeholder: 'ticket_id' })),
      field('saved claim reply json (--reply-file)', jsx('input', { className: inputClass, value: effectiveReplyFile, onChange: e => setReplyFile(e.target.value), placeholder: '/path/to/claim-reply.json' })),
      field('destination directory (--dest, absolute)', jsx('input', { className: inputClass, value: dest, onChange: e => setDest(e.target.value), placeholder: '/home/you/work/ticket' })),
      dest.trim() && !isAbsoluteLocalPath(dest)
        ? jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Destination must be absolute (starts with /) and must not look like a URL.' })
        : null,
      failureHints(PULL_FAILURES),
      submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: PRIMARY_BUTTONS.pull })
    ]
  })
}

const PUSH_FAILURES = [
  'unknown tip — the plugin refuses; re-ask /fetch repo <repo_id> for accepted_version and retry with the right --version',
  'agent budget exhausted — the push is refused until the budget resets',
  'transport not ready — "Tor is not ready, the ticket expired"; wait for Tor or re-issue the ticket',
  'manifest signature does not verify — repackage and redeliver'
]

// --- form: push / submit (one op, three guided sub-steps) -------------------

function PushSubmitCard({ ctx, gateway, noSession, submitTitle, recordOp, ops, sessionId, refreshOps }) {
  const snapSrc = snapshotSources(ctx)
  const repos = snapSrc.repos
  const tasks = snapSrc.tasks
  const pools = itemsFor({ repos, poolStatus: snapSrc.poolStatus }, 'pools')
  const existing = (Array.isArray(ops) ? ops : []).find(op => op && op.kind === 'push' && op.status !== 'done' && op.status !== 'done (awaiting approval)' && op.status !== 'failed') || null
  const claims = claimOps(ops)
  const claimItems = claims.map(op => ({
    id: str(op && op.id),
    title: `${str((op && op.args && (op.args.ticket || op.args.key)) || (op && op.id) || '')}${op && op.at ? ` · ${new Date(op.at).toLocaleString()}` : ''}`
  }))
  const [pushOpId, setPushOpId] = useState(() => existing && existing.id != null ? str(existing.id) : undefined)
  const [repoId, setRepoId] = useState(() => str((existing && existing.args && existing.args.repoId) || ''))
  const [ticketId, setTicketId] = useState(() => str((existing && existing.args && existing.args.ticketId) || ''))
  const [worktreePath, setWorktreePath] = useState(() => str((existing && existing.args && existing.args.worktreePath) || ''))
  const [poolId, setPoolId] = useState(() => str((existing && existing.args && existing.args.poolId) || ''))
  const [version, setVersion] = useState(() => (existing && existing.args && existing.args.version != null ? String(existing.args.version) : ''))
  const [grantToken, setGrantToken] = useState(() => str((existing && existing.args && existing.args.grantToken) || ''))
  const [transferTicket, setTransferTicket] = useState(() => str((existing && existing.args && existing.args.transferTicket) || ''))
  const [manifest, setManifest] = useState(() => str((existing && existing.args && existing.args.manifest) || ''))
  const [grantDone, setGrantDone] = useState(() => Boolean(existing && existing.args && existing.args.grantDone))
  const [packageDone, setPackageDone] = useState(() => Boolean(existing && existing.args && existing.args.packageDone))
  const [manualTransfer, setManualTransfer] = useState(false)
  const [manualManifest, setManualManifest] = useState(false)

  const livePushOp = (Array.isArray(ops) ? ops : []).find(op => op && op.id != null && str(op.id) === str(pushOpId || (existing && existing.id) || '')) || existing
  const liveArgs = (livePushOp && livePushOp.args) || {}
  const autoGrantToken = liveArgs.grantToken != null ? str(liveArgs.grantToken) : ''
  const autoTransferTicket = liveArgs.transferTicket != null ? str(liveArgs.transferTicket) : ''
  const autoManifest = liveArgs.manifest != null ? str(liveArgs.manifest) : ''
  const effectiveGrantToken = grantToken.trim() || autoGrantToken
  const effectiveTransferTicket = transferTicket.trim() || autoTransferTicket
  const effectiveManifest = manifest.trim() || autoManifest

  const onPickClaim = id => {
    const op = claims.find(o => str(o && o.id) === str(id))
    if (!op || !op.args) return
    const t = op.args.ticket || op.args.key || ''
    if (t) setTicketId(str(t))
    if (op.args.worktreePath) setWorktreePath(str(op.args.worktreePath))
  }

  const substatus = grantDone && packageDone ? 'deliver' : grantDone ? 'package' : 'grant'

  const ensureOp = async (argsPatch, text) => {
    if (pushOpId) {
      const ok = await host.composer.submit(sessionId, text)
      const updated = updateOp(ctx, pushOpId, { args: argsPatch, status: ok ? 'awaiting agent' : 'failed', ...(ok ? {} : { error: 'composer unreachable' }) })
      refreshOps?.()
      return updated
    }
    const record = await recordOp({ kind: 'push', gateway, args: { ...argsPatch }, text })
    if (record && record.id) setPushOpId(record.id)
    return record
  }

  // Step 3 gating: deliver cannot fire before steps 1-2 are done (grant+package)
  // AND the transfer ticket, grant token and manifest are present — via dropdown/
  // op-linked auto-fill or the manual disclosure.
  const deliverReady = !noSession && grantDone && packageDone && Boolean(effectiveTransferTicket) && Boolean(effectiveGrantToken) && Boolean(effectiveManifest)
  const deliverTitle = noSession ? submitTitle
    : !grantDone ? 'step 1 (grant) must complete first — the grant token comes from the /push repo reply'
    : !packageDone ? 'step 2 (package) must complete first — the manifest comes from scripts/contributor-push.py'
    : !effectiveTransferTicket ? 'transfer ticket is required (from the package reply)'
    : !effectiveGrantToken ? 'grant token is required (from the /push repo reply)'
    : !effectiveManifest ? 'manifest json (or path) is required'
    : undefined

  const submitGrant = async () => {
    if (noSession || !repoId.trim() || !ticketId.trim()) return
    const text = pushGrantPrompt({ gateway, repoId: repoId.trim(), ticketId: ticketId.trim() })
    await ensureOp({ repoId: repoId.trim(), ticketId: ticketId.trim(), grantDone: true, substatus: 'grant' }, text)
    setGrantDone(true)
  }

  const submitPackage = async () => {
    if (noSession || !grantDone || !worktreePath.trim() || !repoId.trim() || !poolId.trim() || !/^\d+$/.test(version.trim())) return
    const text = pushPackagePrompt({ gateway, worktreePath: worktreePath.trim(), repoId: repoId.trim(), poolId: poolId.trim(), version: Number(version.trim()) })
    await ensureOp({ worktreePath: worktreePath.trim(), poolId: poolId.trim(), version: Number(version.trim()), packageDone: true, substatus: 'package' }, text)
    setPackageDone(true)
  }

  const submitDeliver = async () => {
    if (!deliverReady) return
    const text = pushDeliverPrompt({ gateway, repoId: repoId.trim(), version: version.trim(), ticketJson: effectiveManifest, transferTicket: effectiveTransferTicket, grantToken: effectiveGrantToken })
    await ensureOp({ transferTicket: effectiveTransferTicket, grantToken: effectiveGrantToken, manifest: effectiveManifest, substatus: 'deliver' }, text)
  }

  // Retry deliver: re-submits ONLY the deliver prompt. The grant is not
  // consumed by deliver, so redelivery is legal on transport failure.
  const retryDeliver = async () => {
    if (noSession || !(pushOpId || existing) || !effectiveGrantToken || !effectiveTransferTicket || !effectiveManifest) return
    const text = pushDeliverPrompt({ gateway, repoId: repoId.trim(), version: version.trim(), ticketJson: effectiveManifest, transferTicket: effectiveTransferTicket, grantToken: effectiveGrantToken })
    const id = pushOpId || (existing && existing.id)
    const ok = await host.composer.submit(sessionId, text)
    updateOp(ctx, id, { args: { substatus: 'deliver' }, status: ok ? 'awaiting agent' : 'failed', ...(ok ? {} : { error: 'composer unreachable' }) })
    refreshOps?.()
    notifyOp(ctx, ok ? 'info' : 'failed', ok ? 'push deliver re-submitted — awaiting agent' : 'composer unreachable', { id })
  }

  const grantReady = !noSession && repoId.trim() && ticketId.trim()
  const packageReady = !noSession && grantDone && worktreePath.trim() && repoId.trim() && poolId.trim() && /^\d+$/.test(version.trim())

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: 'Push / submit work (grant → package → deliver)' }),
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: `one tracked op of kind push · substatus: ${str(substatus)}` }),
      jsx(SnapshotSelect, {
        label: 'claim (prefills ticket + worktree)',
        items: claimItems,
        value: '',
        onChange: onPickClaim,
        emptyHint: SNAPSHOT_KIND_EMPTY.claims,
        allowManual: false,
        selectLabel: '(pick a claim)',
        resetKey: substatus
      }),
      jsx(SnapshotSelect, {
        label: 'repo_id',
        items: repos,
        value: repoId,
        onChange: setRepoId,
        emptyHint: SNAPSHOT_KIND_EMPTY.repos,
        refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'repos', gateway) : undefined,
        loadingKey: 'repos',
        ops,
        allowManual: true,
        manualPlaceholder: 'my-repo',
        manualValue: repoId,
        onManualChange: setRepoId,
        resetKey: substatus
      }),
      jsx(SnapshotSelect, {
        label: 'ticket id',
        items: tasks,
        value: ticketId,
        onChange: setTicketId,
        emptyHint: SNAPSHOT_KIND_EMPTY.tasks,
        refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'tasks', gateway) : undefined,
        loadingKey: 'tasks',
        refreshKind: 'fetch-tasks',
        ops,
        allowManual: true,
        manualPlaceholder: 'ticket_id',
        manualValue: ticketId,
        onManualChange: setTicketId,
        resetKey: substatus
      }),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Step 1 — grant (/push repo)' }),
      submitButton({ disabled: !grantReady, title: noSession ? submitTitle : (!repoId.trim() || !ticketId.trim() ? 'repo_id and ticket id are required' : undefined), onClick: submitGrant, children: grantDone ? 'Step 1 — grant submitted ✓ (re-request)' : 'Step 1 — request grant (/push repo)' }),
      jsx(SnapshotSelect, {
        label: autoGrantToken && !grantToken.trim() ? 'grant token (auto-filled from the grant op reply)' : 'grant token (from the /push repo reply)',
        items: effectiveGrantToken ? [{ id: effectiveGrantToken, title: 'grant token' }] : [],
        value: effectiveGrantToken,
        onChange: setGrantToken,
        emptyHint: SNAPSHOT_KIND_EMPTY.grant,
        allowManual: true,
        manualPlaceholder: 'grant_token',
        manualValue: grantToken,
        onManualChange: setGrantToken,
        resetKey: substatus
      }),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Step 2 — package (scripts/contributor-push.py). Caveat: take next_version from the grant reply or re-ask /fetch repo <repo_id> for accepted_version; never trust the enroll-time chain_tips.' }),
      field('worktree path', jsx('input', { className: inputClass, value: worktreePath, onChange: e => setWorktreePath(e.target.value), placeholder: '/path/to/worktree' })),
      jsx(SnapshotSelect, {
        label: 'pool_id',
        items: pools,
        value: poolId,
        onChange: setPoolId,
        emptyHint: SNAPSHOT_KIND_EMPTY.pools,
        refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'full', gateway) : undefined,
        loadingKey: 'poolStatus',
        ops,
        allowManual: true,
        manualPlaceholder: 'pool_id',
        manualValue: poolId,
        onManualChange: setPoolId,
        resetKey: substatus
      }),
      version.trim() && !/^\d+$/.test(version.trim())
        ? jsx('div', { className: 'text-(--ui-text-secondary)', children: '--version must be a positive integer.' })
        : null,
      jsx(SnapshotSelect, {
        label: '--version N (auto-resolves; manual fallback)',
        items: [],
        value: version,
        onChange: setVersion,
        emptyHint: SNAPSHOT_KIND_EMPTY.version,
        allowManual: true,
        manualPlaceholder: 'N',
        manualValue: version,
        onManualChange: setVersion,
        resetKey: substatus
      }),
      submitButton({ disabled: !packageReady, title: noSession ? submitTitle : !grantDone ? 'step 1 (grant) must complete first' : 'worktree, repo_id, pool_id and a numeric --version are required', onClick: submitPackage, children: packageDone ? 'Step 2 — package submitted ✓ (re-package)' : 'Step 2 — package (scripts/contributor-push.py)' }),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Step 3 — deliver (/push repo --deliver). Redelivery is legal: the grant is not consumed by deliver.' }),
      autoTransferTicket && !transferTicket.trim() && !manualTransfer
        ? jsxs('div', { className: 'flex items-center gap-2', children: [
            jsx('div', { className: 'text-(--ui-text-tertiary)', children: `transfer ticket auto-filled from the package op reply: ${str(autoTransferTicket)}` }),
            jsx('button', { className: 'rounded-md px-2 py-0.5 text-(--ui-text-secondary)', title: 'Enter the transfer ticket manually', onClick: () => setManualTransfer(true), children: '(enter manually…)' })
          ] })
        : field('transfer ticket (from the package reply)', jsx('input', { className: inputClass, value: transferTicket, onChange: e => setTransferTicket(e.target.value), placeholder: autoTransferTicket ? `auto-filled: ${autoTransferTicket}` : 'transfer_ticket' })),
      autoManifest && !manifest.trim() && !manualManifest
        ? jsxs('div', { className: 'flex items-center gap-2', children: [
            jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'manifest auto-filled from the package op reply' }),
            jsx('button', { className: 'rounded-md px-2 py-0.5 text-(--ui-text-secondary)', title: 'Enter the manifest manually', onClick: () => setManualManifest(true), children: '(enter manually…)' })
          ] })
        : field('manifest json (or path)', jsx('input', { className: inputClass, value: manifest, onChange: e => setManifest(e.target.value), placeholder: autoManifest ? 'auto-filled from the package op reply' : '{"manifest": …} or /path/to/manifest.json' })),
      submitButton({ disabled: !deliverReady, title: deliverTitle, onClick: submitDeliver, children: PRIMARY_BUTTONS.push }),
      submitButton({ disabled: noSession || !(pushOpId || existing) || !effectiveGrantToken || !effectiveTransferTicket || !effectiveManifest, title: 'Re-submits only the deliver prompt (grant not consumed by deliver)', onClick: retryDeliver, children: 'Retry deliver (grant not consumed)' }),
      failureHints(PUSH_FAILURES)
    ]
  })
}

const APPROVE_FAILURES = [
  'non-owner refusal — the plugin refuses; the pane records the plugin\'s message verbatim as op status refused (authorization lives only in the plugin)',
  'audit not clean — fix the audit findings before approving',
  'no accepted bundle for the version — push/deliver a version first, then approve'
]

// --- form: approve (OWNER ONLY) --------------------------------------------

function ApproveForm({ ctx, gateway, noSession, submitTitle, recordOp, sessionId, ops, prefill, onPrefillConsumed }) {
  const snapSrc = snapshotSources(ctx)
  const repos = snapSrc.repos
  const tasks = snapSrc.tasks
  const awaiting = snapSrc.awaitingApproval
  const [targetKind, setTargetKind] = useState('repo')
  const [repoId, setRepoId] = useState('')
  const [version, setVersion] = useState('')
  const [ticketId, setTicketId] = useState('')
  const [deny, setDeny] = useState(false)
  const [pendingConfirm, setPendingConfirm] = useState(null)

  // Deep-link from the tracker's Awaiting-approval tab: pre-fill the form
  // (still confirm-gated — nothing submits until the operator confirms).
  useEffect(() => {
    if (!prefill) return
    if (prefill.ticket) {
      setTargetKind('ticket')
      setTicketId(str(prefill.ticket))
    } else {
      setTargetKind('repo')
      setRepoId(str(prefill.repo || prefill.id || ''))
      if (prefill.version != null) setVersion(str(prefill.version))
    }
    setDeny(Boolean(prefill.deny))
    if (onPrefillConsumed) onPrefillConsumed()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill])

  const onPickAwaiting = id => {
    const entry = awaiting.find(e => str(e && e.id) === str(id))
    if (!entry) return
    if (entry.ticket) {
      setTargetKind('ticket')
      setTicketId(str(entry.ticket))
    } else if (entry.repo) {
      setTargetKind('repo')
      setRepoId(str(entry.repo))
      if (entry.version != null) setVersion(str(entry.version))
    } else {
      setTargetKind('repo')
      setRepoId(str(entry.id))
      if (entry.version != null) setVersion(str(entry.version))
    }
  }

  const versionOk = !version.trim() || /^\d+$/.test(version.trim())
  const ready = !noSession && versionOk && (targetKind === 'repo' ? Boolean(repoId.trim()) : Boolean(ticketId.trim()))
  const disabledTitle = noSession
    ? submitTitle
    : !versionOk ? '--version must be a number'
    : targetKind === 'repo' ? (!repoId.trim() ? 'pick a repo first' : undefined)
    : (!ticketId.trim() ? 'pick a ticket first' : undefined)

  // The primary button only OPENS the confirm card; the op composes and
  // submits strictly on Confirm (finalizeOp returns null without confirmed).
  const onSubmit = () => {
    if (!ready) return
    const v = targetKind === 'repo' && version.trim() ? Number(version.trim()) : undefined
    const args = targetKind === 'repo'
      ? { repoId: repoId.trim(), ...(v != null ? { version: v } : {}), deny }
      : { ticketId: ticketId.trim(), deny }
    const text = approvePrompt({
      gateway,
      repoId: targetKind === 'repo' ? repoId.trim() : undefined,
      version: v,
      ticketId: targetKind === 'ticket' ? ticketId.trim() : undefined,
      deny
    })
    const commandText = approveCommandText({
      repoId: targetKind === 'repo' ? repoId.trim() : undefined,
      version: v,
      ticketId: targetKind === 'ticket' ? ticketId.trim() : undefined,
      deny
    })
    const target = targetKind === 'repo'
      ? `${deny ? 'Deny' : 'Approve'} \`${repoId.trim()}${v != null ? ` v${v}` : ''}\``
      : `${deny ? 'Deny' : 'Approve'} pushed claim \`${ticketId.trim()}\``
    const card = buildConfirmCard(target, gateway, commandText)
    setPendingConfirm({ draft: { kind: 'approve', gateway, args: { ...args, target: targetKind }, text }, card })
  }

  const onConfirmOp = async () => {
    const op = finalizeOp(pendingConfirm && pendingConfirm.draft, { confirmed: true })
    setPendingConfirm(null)
    if (!op) return
    await recordOp(op)
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: 'OWNER ONLY — Approve / deny (pool-approve.py)' }),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: 'OWNER ONLY. The pane has no owner check of its own — authorization lives only in the plugin. A non-owner profile still submits; the plugin refuses and the pane records the refusal verbatim as op status refused.' }),
      jsx(SegmentedControl, {
        options: [
          { id: 'repo', label: 'Repo version' },
          { id: 'ticket', label: 'Pushed claim' }
        ],
        value: targetKind,
        onChange: id => setTargetKind(id)
      }),
      jsx(SnapshotSelect, {
        label: 'awaiting approval (from pool_status)',
        items: awaiting,
        value: '',
        onChange: onPickAwaiting,
        emptyHint: SNAPSHOT_KIND_EMPTY.awaiting,
        refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'full', gateway) : undefined,
        loadingKey: 'poolStatus',
        ops,
        allowManual: false,
        selectLabel: '(pick an awaiting-approval target)',
        resetKey: targetKind
      }),
      targetKind === 'repo'
        ? jsx(SnapshotSelect, {
            label: 'repo_id',
            items: repos,
            value: repoId,
            onChange: setRepoId,
            emptyHint: SNAPSHOT_KIND_EMPTY.repos,
            refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'repos', gateway) : undefined,
        loadingKey: 'repos',
            ops,
            allowManual: true,
            manualPlaceholder: 'my-repo',
            manualValue: repoId,
            onManualChange: setRepoId,
            resetKey: targetKind
          })
        : jsx(SnapshotSelect, {
            label: 'ticket_id',
            items: tasks,
            value: ticketId,
            onChange: setTicketId,
            emptyHint: SNAPSHOT_KIND_EMPTY.tasks,
            refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'tasks', gateway) : undefined,
        loadingKey: 'tasks',
            refreshKind: 'fetch-tasks',
            ops,
            allowManual: true,
            manualPlaceholder: 'ticket_id',
            manualValue: ticketId,
            onManualChange: setTicketId,
            resetKey: targetKind
          }),
      targetKind === 'repo'
        ? jsx('div', { className: 'flex flex-col gap-1', children: [
            field('--version N (optional)', jsx('input', { className: inputClass, value: version, onChange: e => setVersion(e.target.value), inputMode: 'numeric', placeholder: 'N' })),
            version.trim() && !/^\d+$/.test(version.trim())
              ? jsx('div', { className: 'text-(--ui-text-secondary)', children: '--version must be a positive integer.' })
              : null
          ] })
        : null,
      jsx(ToggleRow, {
        checked: deny,
        description: 'Pass --deny instead of approving.',
        label: 'Deny',
        onChange: value => setDeny(value)
      }),
      failureHints(APPROVE_FAILURES),
      pendingConfirm
        ? jsx(ConfirmCard, { card: pendingConfirm.card, onConfirm: onConfirmOp, onCancel: () => setPendingConfirm(null) })
        : submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: deny ? PRIMARY_BUTTONS.deny : PRIMARY_BUTTONS.approve })
    ]
  })
}

const MERGE_FAILURES = [
  'non-owner refusal — the plugin refuses; the pane records the plugin\'s message verbatim as op status refused (authorization lives only in the plugin)',
  'audit not clean — the merge must not proceed until the audit is clean',
  'no accepted bundle for the version — approve an accepted bundle first'
]

// --- form: merge (OWNER ONLY) ----------------------------------------------

function MergeForm({ ctx, gateway, noSession, submitTitle, recordOp, sessionId, ops }) {
  const snapSrc = snapshotSources(ctx)
  const repos = snapSrc.repos
  const tasks = snapSrc.tasks
  const awaiting = snapSrc.awaitingApproval
  const [targetKind, setTargetKind] = useState('repo')
  const [repoId, setRepoId] = useState('')
  const [ticketId, setTicketId] = useState('')
  const [checkoutPath, setCheckoutPath] = useState('')
  const [pendingConfirm, setPendingConfirm] = useState(null)

  const onPickAwaiting = id => {
    const entry = awaiting.find(e => str(e && e.id) === str(id))
    if (!entry) return
    if (entry.ticket) {
      setTargetKind('ticket')
      setTicketId(str(entry.ticket))
    } else {
      setTargetKind('repo')
      setRepoId(str(entry.repo || entry.id))
    }
  }

  // Default canonical checkout: the pool's work/integrate/<repo_id>/ convention.
  const effectiveCheckout = checkoutPath.trim() || (repoId.trim() ? `work/integrate/${repoId.trim()}/` : '')
  const ready = !noSession && (targetKind === 'repo' ? Boolean(repoId.trim()) : Boolean(ticketId.trim())) && Boolean(effectiveCheckout)
  const disabledTitle = noSession
    ? submitTitle
    : targetKind === 'repo' ? (!repoId.trim() ? 'pick a repo first' : undefined)
    : (!ticketId.trim() ? 'pick a ticket first' : undefined)

  // Primary button opens the confirm card; the op composes strictly on Confirm.
  const onSubmit = () => {
    if (!ready) return
    const args = targetKind === 'repo'
      ? { repoId: repoId.trim(), checkoutPath: effectiveCheckout }
      : { ticketId: ticketId.trim(), checkoutPath: effectiveCheckout }
    const text = mergePrompt({
      gateway,
      repoId: targetKind === 'repo' ? repoId.trim() : undefined,
      ticketId: targetKind === 'ticket' ? ticketId.trim() : undefined,
      checkoutPath: effectiveCheckout
    })
    const commandText = mergeCommandText({
      repoId: targetKind === 'repo' ? repoId.trim() : undefined,
      ticketId: targetKind === 'ticket' ? ticketId.trim() : undefined,
      checkoutPath: effectiveCheckout
    })
    const target = targetKind === 'repo'
      ? `Merge approved bundle for \`${repoId.trim()}\``
      : `Merge approved bundle for pushed claim \`${ticketId.trim()}\``
    const card = buildConfirmCard(target, gateway, commandText)
    setPendingConfirm({ draft: { kind: 'merge', gateway, args: { ...args, target: targetKind }, text }, card })
  }

  const onConfirmOp = async () => {
    const op = finalizeOp(pendingConfirm && pendingConfirm.draft, { confirmed: true })
    setPendingConfirm(null)
    if (!op) return
    await recordOp(op)
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: 'OWNER ONLY — Merge approved bundle' }),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: 'OWNER ONLY. The plugin never merges — the agent performs git fetch <approved bundle> + git merge into the canonical checkout itself. Where the maintainer flow uses the merge_approved service path (tickets.py:2249), it goes through the agent executing the plugin\'s documented flow, never a pane-side import. A non-owner profile still submits; the plugin refuses and the pane records the refusal verbatim as op status refused.' }),
      jsx(SegmentedControl, {
        options: [
          { id: 'repo', label: 'Repo' },
          { id: 'ticket', label: 'Ticket' }
        ],
        value: targetKind,
        onChange: id => setTargetKind(id)
      }),
      jsx(SnapshotSelect, {
        label: 'awaiting approval (from pool_status)',
        items: awaiting,
        value: '',
        onChange: onPickAwaiting,
        emptyHint: SNAPSHOT_KIND_EMPTY.awaiting,
        refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'full', gateway) : undefined,
        loadingKey: 'poolStatus',
        ops,
        allowManual: false,
        selectLabel: '(pick an awaiting-approval target)',
        resetKey: targetKind
      }),
      targetKind === 'repo'
        ? jsx(SnapshotSelect, {
            label: 'repo_id',
            items: repos,
            value: repoId,
            onChange: setRepoId,
            emptyHint: SNAPSHOT_KIND_EMPTY.repos,
            refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'repos', gateway) : undefined,
        loadingKey: 'repos',
            ops,
            allowManual: true,
            manualPlaceholder: 'my-repo',
            manualValue: repoId,
            onManualChange: setRepoId,
            resetKey: targetKind
          })
        : jsx(SnapshotSelect, {
            label: 'ticket_id',
            items: tasks,
            value: ticketId,
            onChange: setTicketId,
            emptyHint: SNAPSHOT_KIND_EMPTY.tasks,
            refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'tasks', gateway) : undefined,
        loadingKey: 'tasks',
            refreshKind: 'fetch-tasks',
            ops,
            allowManual: true,
            manualPlaceholder: 'ticket_id',
            manualValue: ticketId,
            onManualChange: setTicketId,
            resetKey: targetKind
          }),
      field('canonical checkout path (default: work/integrate/<repo_id>/)', jsx('input', { className: inputClass, value: checkoutPath, onChange: e => setCheckoutPath(e.target.value), placeholder: repoId.trim() ? `work/integrate/${repoId.trim()}/` : 'work/integrate/<repo_id>/' })),
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: `effective checkout: ${str(effectiveCheckout) || '(enter a repo_id to prefill the work/integrate/ convention)'}` }),
      failureHints(MERGE_FAILURES),
      pendingConfirm
        ? jsx(ConfirmCard, { card: pendingConfirm.card, onConfirm: onConfirmOp, onCancel: () => setPendingConfirm(null) })
        : submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: PRIMARY_BUTTONS.merge })
    ]
  })
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
        className: 'rounded-md px-2 py-0.5 text-left text-(--ui-text-tertiary)',
        onClick: () => setOpen(v => !v),
        children: [
          jsx('span', { children: open ? '▾ details' : '▸ details' }),
          unparsed ? jsx('span', { className: 'text-(--ui-text-secondary)', children: ' · review manually' }) : null
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
  const retry = retryLegality(op)
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
                children: `${str(op.gateway) === 'none' ? '' : str(op.gateway)}${op.at ? ` · ${new Date(op.at).toLocaleString()}` : ''}`
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
              retry === 'deliver'
                ? jsx('button', {
                    className: 'rounded-md px-2 py-0.5 text-xs text-(--ui-text-primary)',
                    title: 'grant not consumed',
                    onClick: () => hs.onRetryDeliver && hs.onRetryDeliver(op),
                    children: 'Retry deliver'
                  })
                : null,
              retry === 'resubmit'
                ? jsx('button', {
                    className: 'rounded-md px-2 py-0.5 text-xs text-(--ui-text-primary)',
                    title: 're-submits the exact same op text',
                    onClick: () => hs.onResubmit && hs.onResubmit(op),
                    children: 'Retry'
                  })
                : null,
              op.status === 'refused'
                ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: 'read the refusal; fix the cause, then re-run the flow.' })
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
function TrackerSection({ ctx, ops, poolStatus, gateway, sessionId, noSession, submitTitle, onRefresh, refreshing, onApproveRequest }) {
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
  const onRetryDeliver = async op => {
    if (noSession) return
    const a = (op && op.args) || {}
    if (a.repoId == null || a.manifest == null || a.transferTicket == null || a.grantToken == null) return
    const text = pushDeliverPrompt({ gateway, repoId: str(a.repoId), version: a.version != null ? str(a.version) : '', ticketJson: str(a.manifest), transferTicket: str(a.transferTicket), grantToken: str(a.grantToken) })
    const ok = await host.composer.submit(sessionId, text)
    updateOp(ctx, op.id, { args: { substatus: 'deliver' }, status: ok ? 'awaiting agent' : 'failed', ...(ok ? {} : { error: 'composer unreachable' }) })
    onRefresh()
    notifyOp(ctx, ok ? 'info' : 'failed', ok ? 'push deliver re-submitted — awaiting agent' : 'composer unreachable', { id: op.id })
  }
  const onResubmit = async op => {
    if (noSession || !op || !op.text) return
    const ok = await host.composer.submit(sessionId, op.text)
    updateOp(ctx, op.id, { status: ok ? 'awaiting agent' : 'failed', ...(ok ? {} : { error: 'composer unreachable' }) })
    onRefresh()
    notifyOp(ctx, ok ? 'info' : 'failed', ok ? `${str(op.kind)} re-submitted — awaiting agent` : 'composer unreachable', { id: op.id })
  }
  const rowHandlers = { onRetryDeliver, onResubmit }
  const renderRow = op => opRow(op, lookup, rowHandlers)

  let body = null
  if (tab === 'active') {
    const active = list.filter(op => op && isActiveStatus(op.status))
    body = active.length
      ? jsx('div', { className: 'flex flex-col gap-1', children: active.slice(0, 20).map(renderRow) })
      : jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No active operations.' })
  } else if (tab === 'awaiting') {
    body = awaiting.length
      ? jsx('div', { className: 'flex flex-col gap-1', children: awaiting.map((entry, i) => jsxs('div', { key: str(entry && entry.id) !== 'none' ? str(entry.id) : i, className: 'flex items-center justify-between gap-2 rounded-md px-2 py-1 text-(--ui-text-primary)', children: [
          jsx('span', { children: `${str(entry && entry.id)}${entry && entry.version != null ? ` · v${str(entry.version)}` : ''} (from pool_status)` }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('button', { className: 'rounded-md px-2 py-0.5 text-xs text-(--ui-text-primary)', disabled: noSession, title: noSession ? submitTitle : 'Pre-fill the Approve confirm card', onClick: () => onApproveRequest && onApproveRequest(entry, false), children: 'Approve' }),
              jsx('button', { className: 'rounded-md px-2 py-0.5 text-xs text-(--ui-text-secondary)', disabled: noSession, title: noSession ? submitTitle : 'Pre-fill the Deny confirm card', onClick: () => onApproveRequest && onApproveRequest(entry, true), children: 'Deny' })
            ]
          })
        ] })) })
      : jsx('div', { className: 'text-(--ui-text-tertiary)', children: poolStatus ? 'Nothing awaiting approval in the last pool_status snapshot.' : 'No pool_status snapshot yet — hit Refresh or request a status report.' })
  } else if (tab === 'claims') {
    body = jsxs('div', {
      className: 'flex flex-col gap-1',
      children: [
        jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Budgets (from pool_status)' }),
        budgets.length
          ? budgets.map((b, i) => jsx('div', { key: i, className: 'rounded-md px-2 py-1 text-(--ui-text-primary)', children: `${str(b && b.line)}${b && b.exhausted ? ' — EXHAUSTED' : ''}` }))
          : jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No budget lines in the last pool_status snapshot.' }),
        jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Claim ops' }),
        claimList.length
          ? claimList.slice(0, 20).map(renderRow)
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
          refreshAction: sessionId ? () => refreshSnapshot(ctx, sessionId, 'tasks', gateway) : undefined,
        loadingKey: 'tasks',
          refreshKind: 'fetch-tasks',
          ops: list,
          allowManual: true,
          manualPlaceholder: 'ticket_id',
          manualValue: auditTicket,
          onManualChange: setAuditTicket
        }),
        submitButton({
          disabled: noSession,
          title: noSession ? submitTitle : undefined,
          onClick: async () => {
            if (noSession) return
            await host.composer.submit(sessionId, auditShowPrompt(gateway, auditTicket))
          },
          children: 'Run /audit show'
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
          jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Operations' }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('button', { className: 'rounded-md px-2 py-1 text-(--ui-text-primary)', onClick: onRefresh, disabled: refreshing, children: refreshing ? 'Refreshing…' : 'Refresh' }),
              jsx('button', {
                className: 'rounded-md px-2 py-1 text-(--ui-text-primary)',
                disabled: noSession,
                title: noSession ? submitTitle : 'Ask the agent to run pool_status and report it via pool_pane_report',
                onClick: async () => {
                  if (noSession) return
                  await host.composer.submit(sessionId, BATCHED_STATUS_PROMPT)
                  onRefresh()
                },
                children: 'Report status'
              })
            ]
          })
        ]
      }),
      poolStatus && poolStatus.at ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: `pool_status snapshot: ${new Date(poolStatus.at).toLocaleString()}` }) : null,
      jsx(SegmentedControl, {
        options: TRACKER_TABS,
        value: tab,
        onChange: setTab
      }),
      body
    ]
  })
}

function isCollapsed(storage, id) {
  if (id in poolPaneStore.collapsed) {
    return Boolean(poolPaneStore.collapsed[id])
  }
  return Boolean(storage.get(STORAGE_KEYS.collapsed(id), false))
}

function setCollapsed(storage, id, value) {
  poolPaneStore.collapsed[id] = value
  storage.set(STORAGE_KEYS.collapsed(id), value)
}

// --- console body -------------------------------------------------------------

// The console body is a plain builder (no hooks of its own) so the guard in
// PoolConsole can catch anything thrown while the tree is assembled. Hook values
// are read in PoolConsole and passed in.
// First-run onboarding card: replaces the flow-card wall when no snapshot
// has ever landed. Both actions live inside the card.
function OnboardingCard({ ctx, sessionId, gateway, recordOp, noSession }) {
  const onRefresh = () => { if (sessionId) refreshSnapshot(ctx, sessionId, 'full', gateway) }
  const onDiscover = async () => {
    if (noSession) return
    await recordOp({ kind: 'discover', gateway, args: { snapshot: 'snapshot.discovered' }, text: discoverReposPrompt() })
  }
  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md p-2',
    children: [
      jsx('div', { className: 'font-medium text-(--ui-text-primary)', children: 'Welcome to your Pool Console' }),
      jsx('div', {
        className: 'text-(--ui-text-secondary)',
        children: '① Click Refresh pools to load your pools, repos and approval queue ② (optional) Discover repos to index local git worktrees. Everything after that is dropdowns and buttons.'
      }),
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          submitButton({ disabled: noSession, title: noSession ? 'no focused chat session — focus a chat to submit' : 'Load pools, repos and the approval queue', onClick: onRefresh, children: 'Refresh pools' }),
          submitButton({ disabled: noSession, title: noSession ? 'no focused chat session — focus a chat to submit' : 'Ask the agent to enumerate local git repositories (read-only)', onClick: onDiscover, children: PRIMARY_BUTTONS.discover })
        ]
      })
    ]
  })
}

function buildPaneBody(ctx, { sessionId, gatewayState, gateway, setGateway, polling, setPolling, ops, recordOp, refreshOps, poolStatus, onRefresh, refreshing, approvePrefill, onApproveRequest, onApprovePrefillConsumed, sidecarDownAt }) {
  const noSession = !sessionId
  const submitTitle = noSession ? 'no focused chat session — focus a chat to submit' : undefined
  const isFirstRun = firstRun(ctx)
  const hasSnapshot = Boolean(poolStatus) || readSnapshot(ctx, 'snapshot.repos').length > 0 || readSnapshot(ctx, 'snapshot.tasks').length > 0
  const lastUpdate = poolStatus && typeof poolStatus === 'object' && poolStatus.at != null ? poolStatus.at : null
  const showStaleStrip = Boolean(sidecarDownAt) && !hasSnapshot

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-sm text-(--ui-text-primary)',
    children: [
      jsx('div', { className: 'font-medium', children: 'Agent Pool' }),
      jsx('div', {
        className: 'text-(--ui-text-secondary)',
        children: `bound session: ${str(sessionId)}`
      }),
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
      }),
      jsxs('div', {
        className: 'flex flex-col gap-1 text-(--ui-text-secondary)',
        children: [
          jsx('div', { children: `gateway: ${str(gatewayState)}` }),
          jsx('div', {
            className: 'text-(--ui-text-tertiary)',
            children: `tor ready? ${poolPaneStore.tor == null ? 'unknown — awaiting agent' : str(poolPaneStore.tor)}`
          })
        ]
      }),
      jsx(ToggleRow, {
        checked: polling,
        description: 'Poll the gateway for status updates. Saved in this profile’s plugin storage.',
        label: 'Polling',
        onChange: value => {
          poolPaneStore.polling = value
          ctx.storage.set(STORAGE_KEYS.polling, value)
          setPolling(value)
        }
      }),
      showStaleStrip
        ? jsx('div', { className: 'text-(--ui-text-secondary)', children: staleStripText(lastUpdate ?? sidecarDownAt) })
        : null,
      jsx(TrackerSection, { ctx, ops, poolStatus, gateway, sessionId, noSession, submitTitle, onRefresh, refreshing, onApproveRequest }),
      isFirstRun
        ? jsx(OnboardingCard, { ctx, sessionId, gateway, recordOp, noSession })
        : jsxs('div', {
            className: 'flex flex-col gap-3',
            children: [
              jsx(AddRepoForm, { ctx, sessionId, gateway, noSession, submitTitle, recordOp, ops }),
              jsx(FetchTasksForm, { ctx, gateway, noSession, submitTitle, recordOp, sessionId, ops }),
              jsx(ClaimForm, { ctx, gateway, noSession, submitTitle, recordOp, sessionId, ops }),
              jsx(PullRepoForm, { ctx, gateway, noSession, submitTitle, recordOp, ops }),
              jsx(PushSubmitCard, { ctx, gateway, noSession, submitTitle, recordOp, ops, sessionId, refreshOps }),
              jsx(ApproveForm, { ctx, gateway, noSession, submitTitle, recordOp, sessionId, ops, prefill: approvePrefill, onPrefillConsumed: onApprovePrefillConsumed }),
              jsx(MergeForm, { ctx, gateway, noSession, submitTitle, recordOp, sessionId, ops })
            ]
          })
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
  const sessionId = useValue(host.state.focusedSessionId)
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
  const [approvePrefill, setApprovePrefill] = useState(null)
  const onApproveRequest = (entry, deny) => {
    if (!entry) return
    setApprovePrefill({
      id: entry.id != null ? str(entry.id) : '',
      repo: entry.repo != null ? str(entry.repo) : null,
      ticket: entry.ticket != null ? str(entry.ticket) : null,
      version: entry.version != null ? entry.version : null,
      deny: Boolean(deny),
      nonce: Date.now()
    })
  }
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
        at: parsed.at || Date.now(),
        awaitingApproval: parsed.awaitingApproval,
        budgets: parsed.budgets,
        poolStatus: parsed.poolStatus || null
      }
      ctx.storage.set('snapshot.poolStatus', snap)
      setPoolStatus(snap)
      if (parsed.tasks) ctx.storage.set('snapshot.tasks', { at: snap.at, items: Array.isArray(parsed.tasks) ? parsed.tasks : (parsed.tasks.items || parsed.tasks.table || []) })
      if (parsed.claims) ctx.storage.set('snapshot.claims', { at: snap.at, items: Array.isArray(parsed.claims) ? parsed.claims : Object.values(parsed.claims) })
      if (parsed.discovered) {
        const items = Array.isArray(parsed.discovered)
          ? parsed.discovered
          : String(parsed.discovered).split('\n').map(l => l.trim()).filter(Boolean)
        ctx.storage.set('snapshot.discovered', { at: snap.at, items })
      }
    } finally {
      setRefreshing(false)
    }
  }
  // 60s poll — only while the user left polling ON. Check storage before
  // every timer set so a user-disabled toggle is never re-enabled.
  useEffect(() => {
    if (!polling) return undefined
    if (!poolPaneStore.polling || !ctx.storage.get(STORAGE_KEYS.polling, false)) return undefined
    const id = setInterval(() => { refreshFromSidecar() }, 60000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polling])
  const recordOp = async op => {
    const record = await submitOp(ctx, sessionId, op)
    setOps(prev => [record, ...prev].slice(0, OPS_KEEP))
    return record
  }

  try {
    const refreshOps = () => {
      const stored = ctx.storage.get(OPS_KEY, [])
      setOps(Array.isArray(stored) ? stored : [])
    }
    return buildPaneBody(ctx, { sessionId, gatewayState, gateway, setGateway, polling, setPolling, ops, recordOp, refreshOps, poolStatus, onRefresh: refreshFromSidecar, refreshing, approvePrefill, onApproveRequest, onApprovePrefillConsumed: () => setApprovePrefill(null), sidecarDownAt })
  } catch (error) {
    return jsx(PaneErrorCard, { contributionId: CONTRIBUTION_IDS.page, error })
  }
}

// --- full page -------------------------------------------------------------
// The /agent-pool route renders the full console (bound-session line, gateway
// selector, status strip, every form, the tracker) inside an error boundary.

// --- settings --------------------------------------------------------------

function GeneralSettings() {
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsx(ListRow, {
        description: 'Open the full console from the sidebar (Agent Pool).',
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

    // The full-page Pool Console, paired with its sidebar nav row.
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

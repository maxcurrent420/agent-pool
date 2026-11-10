// Agent Pool — tracker logic (pure, no SDK imports).
//
// Pane-local operation log. NOT the ledger: everything here lives in
// ctx.storage under 'ops' / 'snapshot.*' and is derived from agent replies
// and the pool_pane_report sidecar (read via ctx.rest('/pane-state')).
//
// Op kinds: {add-repo-owner, add-repo-link, fetch-tasks, claim, pull, push,
// approve, merge}. Older builds wrote 'add-repo' — migrateOps() upgrades
// those records in place so prior ops survive.

export const OP_KINDS = ['add-repo-owner', 'add-repo-link', 'fetch-tasks', 'claim', 'pull', 'push', 'approve', 'merge']

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
  const empty = { poolStatus: null, tasks: null, claims: null, awaitingApproval: [], budgets: [], at: null }
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
  const next = list.map(op => {
    if (!op || typeof op !== 'object') return op
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

// The one batched UI→agent prompt used for the initial/fallback refresh.
export const BATCHED_STATUS_PROMPT = 'Report pool status now: run pool_status and /fetch repo list, call pool_pane_report with the results.'

// Audit view prompt (composed, submitted through the composer like any op).
export function auditShowPrompt(gateway, ticketId) {
  const t = (ticketId || '').trim()
  return `Using the ${gateway} gateway profile: run /audit show${t ? ` ${t}` : ''} and report the result verbatim.`
}

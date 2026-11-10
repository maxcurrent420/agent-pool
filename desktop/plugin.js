// Agent Pool — Hermes Desktop pane ("Pool Console").
//
// Task 1: loadable skeleton only. The desktop host loads this file UNCOMPILED,
// so there is no JSX syntax here — every element is built with jsx()/jsxs()
// from 'react/jsx-runtime'.
//
// Allowed import specifiers (SDK rule): '@hermes/plugin-sdk', 'react' and
// 'react/jsx-runtime'. Nothing else resolves.
//
// Styling: the app's own theme tokens only (--ui-text-primary /
// --ui-text-secondary / --ui-text-tertiary), so light and dark mode both
// come out of the host's palette. No literal colours.

import { host, ListRow, PANES_AREA, ROUTES_AREA, SegmentedControl, SIDEBAR_NAV_AREA, ToggleRow, useValue } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'
import { auditShowPrompt, applyPaneStateToOps, BATCHED_STATUS_PROMPT, isActiveStatus, migrateOps, normalizeKind, OPS_KEEP, OPS_KEY, parsePaneState } from './track.js'
import { addRepoLinkPrompt, addRepoOwnerPrompt, approvePrompt, claimPrompt, fetchTaskListPrompt, mergePrompt, pullPrompt, pushDeliverPrompt, pushGrantPrompt, pushPackagePrompt } from './prompts.js'

// Contribution ids are namespaced by the host as '<plugin id>:<local id>'.
// The pane's error card names the contribution it came from.
const CONTRIBUTION_IDS = {
  nav: 'agent-pool:nav',
  page: 'agent-pool:page',
  pane: 'agent-pool:pane'
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
    replyRef: null
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

const inputClass = 'rounded-md px-2 py-1 text-(--ui-text-primary)'
const labelClass = 'text-(--ui-text-secondary)'

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

function AddRepoForm({ ctx, sessionId, gateway, noSession, submitTitle, recordOp }) {
  const [mode, setMode] = useState('owner')
  const [repoId, setRepoId] = useState('')
  const [path, setPath] = useState('')
  const [version, setVersion] = useState('')
  const [maintainerGateway, setMaintainerGateway] = useState('default')

  const pathOk = isAbsoluteLocalPath(path)
  const versionOk = !version.trim() || /^\d+$/.test(version.trim())
  const ready = !noSession && repoId.trim() && pathOk && versionOk
  const disabledTitle = noSession
    ? submitTitle
    : !repoId.trim() ? 'repo_id is required'
    : !pathOk ? 'path must be an absolute local path (no URLs)'
    : !versionOk ? '--version must be a positive integer'
    : undefined

  const onSubmit = async () => {
    if (!ready) return
    const v = version.trim() ? Number(version.trim()) : undefined
    const text = mode === 'owner'
      ? addRepoOwnerPrompt({ gateway, repoId: repoId.trim(), path: path.trim(), version: v })
      : addRepoLinkPrompt({ gateway, repoId: repoId.trim(), path: path.trim(), version: v, maintainerGateway })
    await recordOp({
      kind: mode === 'owner' ? 'add-repo-owner' : 'add-repo-link',
      gateway,
      args: { mode, repoId: repoId.trim(), path: path.trim(), version: v ?? null, maintainerGateway: mode === 'link' ? maintainerGateway : undefined, substatus: mode === 'link' ? 'build bundle → deliver' : undefined },
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
      field('repo_id', jsx('input', { className: inputClass, value: repoId, onChange: e => setRepoId(e.target.value), placeholder: 'my-repo' })),
      field('local path (absolute)', jsx('input', { className: inputClass, value: path, onChange: e => setPath(e.target.value), placeholder: '/home/you/src/my-repo' })),
      !pathOk && path.trim() ? jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Path must be absolute (starts with /) and must not look like a URL.' }) : null,
      field('--version (optional)', jsx('input', { className: inputClass, value: version, onChange: e => setVersion(e.target.value), placeholder: 'N', inputMode: 'numeric' })),
      mode === 'link'
        ? field('maintainer gateway (profile)', jsx('input', { className: inputClass, value: maintainerGateway, onChange: e => setMaintainerGateway(e.target.value), placeholder: 'default' }))
        : null,
      failureHints(ADD_REPO_FAILURES),
      submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: mode === 'owner' ? 'Add repo (owner fast path)' : 'Add repo (link flow: build bundle → deliver)' })
    ]
  })
}

// --- form: fetch task list --------------------------------------------------

function snapshotRepos(ctx) {
  const snap = ctx.storage.get('snapshot.repos', null)
  if (Array.isArray(snap)) return snap
  if (snap && Array.isArray(snap.repos)) return snap.repos
  return []
}

function repoIdOf(entry) {
  if (entry == null) return ''
  if (typeof entry === 'string') return entry
  return entry.repo_id || entry.repo || entry.id || entry.name || ''
}

function FetchTasksForm({ ctx, gateway, noSession, submitTitle, recordOp }) {
  const repos = snapshotRepos(ctx)
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
      repos.length
        ? field('repo filter (from last pool_status / fetch repo list)', jsx('select', {
            className: inputClass,
            value: repoFilter,
            onChange: e => setRepoFilter(e.target.value),
            children: [
              jsx('option', { key: '__all', value: '', children: '(all repos)' }),
              ...repos.map(entry => {
                const id = repoIdOf(entry)
                return jsx('option', { key: id, value: id, children: id })
              })
            ]
          }))
        : field('repo filter (optional — no snapshot yet)', jsx('input', { className: inputClass, value: repoFilter, onChange: e => setRepoFilter(e.target.value), placeholder: 'repo_id' })),
      submitButton({ disabled: !ready, title: noSession ? submitTitle : undefined, onClick: onSubmit, children: 'Fetch tasks (/fetch repo list → /fetch task list)' })
    ]
  })
}

// --- form: claim / assign ---------------------------------------------------

const CLAIM_FAILURES = [
  'claim refused: user <id> already bound to key <key> (immutable key binding)',
  'agent budget exhausted: <n> of <allocation> tasks completed',
  'not whitelisted for this pool'
]

function snapshotTasks(ctx) {
  const snap = ctx.storage.get('snapshot.tasks', null)
  if (!snap) return []
  const table = Array.isArray(snap) ? snap : (Array.isArray(snap.table) ? snap.table : (Array.isArray(snap.tasks) ? snap.tasks : []))
  return table
}

function ticketIdOf(entry) {
  if (entry == null) return ''
  if (typeof entry === 'string') return entry
  return entry.ticket_id || entry.ticket || entry.id || ''
}

function ClaimForm({ ctx, gateway, noSession, submitTitle, recordOp }) {
  const tasks = snapshotTasks(ctx)
  const [ticket, setTicket] = useState('')
  const [note, setNote] = useState('')
  const [autoAssign, setAutoAssign] = useState(false)
  const [maxTasks, setMaxTasks] = useState('1')

  const ticketReady = autoAssign ? (/^\d+$/.test(maxTasks.trim()) && Number(maxTasks.trim()) > 0) : Boolean(ticket.trim())
  const ready = !noSession && ticketReady
  const disabledTitle = noSession
    ? submitTitle
    : autoAssign ? (ticketReady ? undefined : 'max-tasks must be a positive integer')
    : 'a ticket id is required — pick one from the snapshot or type it'

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
        ? field('--max-tasks N', jsx('input', { className: inputClass, value: maxTasks, onChange: e => setMaxTasks(e.target.value), inputMode: 'numeric' }))
        : jsxs('div', {
            className: 'flex flex-col gap-2',
            children: [
              tasks.length
                ? field('ticket (from fetched task snapshot)', jsx('select', {
                    className: inputClass,
                    value: ticket,
                    onChange: e => setTicket(e.target.value),
                    children: [
                      jsx('option', { key: '__none', value: '', children: '(select a ticket)' }),
                      ...tasks.map(entry => {
                        const id = ticketIdOf(entry)
                        return jsx('option', { key: id, value: id, children: id })
                      })
                    ]
                  }))
                : null,
              field('ticket id (free-text fallback)', jsx('input', { className: inputClass, value: ticket, onChange: e => setTicket(e.target.value), placeholder: 'ticket_id' }))
            ]
          }),
      field('claimant identity note (optional)', jsx('input', { className: inputClass, value: note, onChange: e => setNote(e.target.value), placeholder: 'who is claiming, for the record' })),
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: `target profile: ${gateway} (plugin binds claim to key + Discord id automatically)` }),
      failureHints(CLAIM_FAILURES),
      submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: autoAssign ? 'Auto-assign (/claim --any-project)' : 'Claim ticket (/claim)' })
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
    : !effectiveTicket ? 'ticket id is required — pick a claim op or type it'
    : !dest.trim() ? 'destination directory is required'
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
      claims.length
        ? field('prefill from claim op', jsx('select', {
            className: inputClass,
            value: claimId,
            onChange: e => onPickClaim(e.target.value),
            children: [
              jsx('option', { key: '__none', value: '', children: '(pick a claim op)' }),
              ...claims.map(op => jsx('option', { key: op.id, value: op.id, children: `${(op.args && (op.args.ticket || op.args.key)) || op.id} · ${new Date(op.at).toLocaleString()}` }))
            ]
          }))
        : jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No claim ops recorded yet — type the ticket id manually.' }),
      field('ticket id', jsx('input', { className: inputClass, value: effectiveTicket, onChange: e => { setClaimId(''); setTicketId(e.target.value) }, placeholder: 'ticket_id' })),
      field('saved claim reply json (--reply-file)', jsx('input', { className: inputClass, value: effectiveReplyFile, onChange: e => setReplyFile(e.target.value), placeholder: '/path/to/claim-reply.json' })),
      field('destination directory (--dest, absolute)', jsx('input', { className: inputClass, value: dest, onChange: e => setDest(e.target.value), placeholder: '/home/you/work/ticket' })),
      failureHints(PULL_FAILURES),
      submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: 'Pull repo (scripts/contributor-claim.py)' })
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
  const existing = (Array.isArray(ops) ? ops : []).find(op => op && op.kind === 'push' && op.status !== 'done' && op.status !== 'done (awaiting approval)' && op.status !== 'failed') || null
  const [pushOpId, setPushOpId] = useState(() => existing && existing.id)
  const [repoId, setRepoId] = useState(() => (existing && existing.args && existing.args.repoId) || '')
  const [ticketId, setTicketId] = useState(() => (existing && existing.args && existing.args.ticketId) || '')
  const [worktreePath, setWorktreePath] = useState(() => (existing && existing.args && existing.args.worktreePath) || '')
  const [poolId, setPoolId] = useState(() => (existing && existing.args && existing.args.poolId) || '')
  const [version, setVersion] = useState(() => (existing && existing.args && existing.args.version != null ? String(existing.args.version) : ''))
  const [grantToken, setGrantToken] = useState(() => (existing && existing.args && existing.args.grantToken) || '')
  const [transferTicket, setTransferTicket] = useState(() => (existing && existing.args && existing.args.transferTicket) || '')
  const [manifest, setManifest] = useState(() => (existing && existing.args && existing.args.manifest) || '')
  const [grantDone, setGrantDone] = useState(() => Boolean(existing && existing.args && existing.args.grantDone))
  const [packageDone, setPackageDone] = useState(() => Boolean(existing && existing.args && existing.args.packageDone))

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

  // Step 3 gating: disabled until steps 1-2 statuses are filled (ticket,
  // grant token and manifest path linked/filled).
  const deliverReady = !noSession && grantDone && packageDone && Boolean(transferTicket.trim()) && Boolean(grantToken.trim()) && Boolean(manifest.trim())
  const deliverTitle = noSession ? submitTitle
    : !grantDone ? 'step 1 (grant) must complete first — the grant token comes from the /push repo reply'
    : !packageDone ? 'step 2 (package) must complete first — the manifest comes from scripts/contributor-push.py'
    : !transferTicket.trim() ? 'transfer ticket is required (from the package reply)'
    : !grantToken.trim() ? 'grant token is required (from the /push repo reply)'
    : !manifest.trim() ? 'manifest json (or path) is required'
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
    const text = pushDeliverPrompt({ gateway, repoId: repoId.trim(), version: version.trim(), ticketJson: manifest.trim(), transferTicket: transferTicket.trim(), grantToken: grantToken.trim() })
    await ensureOp({ transferTicket: transferTicket.trim(), grantToken: grantToken.trim(), manifest: manifest.trim(), substatus: 'deliver' }, text)
  }

  // Retry deliver: re-submits ONLY the deliver prompt. The grant is not
  // consumed by deliver, so redelivery is legal on transport failure.
  const retryDeliver = async () => {
    if (noSession || !(pushOpId || existing) || !grantToken.trim() || !transferTicket.trim() || !manifest.trim()) return
    const text = pushDeliverPrompt({ gateway, repoId: repoId.trim(), version: version.trim(), ticketJson: manifest.trim(), transferTicket: transferTicket.trim(), grantToken: grantToken.trim() })
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
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: `one tracked op of kind push · substatus: ${substatus}` }),
      field('repo_id', jsx('input', { className: inputClass, value: repoId, onChange: e => setRepoId(e.target.value), placeholder: 'my-repo' })),
      field('ticket id', jsx('input', { className: inputClass, value: ticketId, onChange: e => setTicketId(e.target.value), placeholder: 'ticket_id' })),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Step 1 — grant (/push repo)' }),
      submitButton({ disabled: !grantReady, title: noSession ? submitTitle : (!repoId.trim() || !ticketId.trim() ? 'repo_id and ticket id are required' : undefined), onClick: submitGrant, children: grantDone ? 'Step 1 — grant submitted ✓ (re-request)' : 'Step 1 — request grant (/push repo)' }),
      field('grant token (from the /push repo reply)', jsx('input', { className: inputClass, value: grantToken, onChange: e => setGrantToken(e.target.value), placeholder: 'grant_token' })),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Step 2 — package (scripts/contributor-push.py). Caveat: take next_version from the grant reply or re-ask /fetch repo <repo_id> for accepted_version; never trust the enroll-time chain_tips.' }),
      field('worktree path', jsx('input', { className: inputClass, value: worktreePath, onChange: e => setWorktreePath(e.target.value), placeholder: '/path/to/worktree' })),
      field('pool_id', jsx('input', { className: inputClass, value: poolId, onChange: e => setPoolId(e.target.value), placeholder: 'pool_id' })),
      field('--version N (next_version from grant reply or /fetch repo)', jsx('input', { className: inputClass, value: version, onChange: e => setVersion(e.target.value), inputMode: 'numeric', placeholder: 'N' })),
      submitButton({ disabled: !packageReady, title: noSession ? submitTitle : !grantDone ? 'step 1 (grant) must complete first' : 'worktree, repo_id, pool_id and a numeric --version are required', onClick: submitPackage, children: packageDone ? 'Step 2 — package submitted ✓ (re-package)' : 'Step 2 — package (scripts/contributor-push.py)' }),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Step 3 — deliver (/push repo --deliver). Redelivery is legal: the grant is not consumed by deliver.' }),
      field('transfer ticket (from the package reply)', jsx('input', { className: inputClass, value: transferTicket, onChange: e => setTransferTicket(e.target.value), placeholder: 'transfer_ticket' })),
      field('manifest json (or path)', jsx('input', { className: inputClass, value: manifest, onChange: e => setManifest(e.target.value), placeholder: '{"manifest": …} or /path/to/manifest.json' })),
      submitButton({ disabled: !deliverReady, title: deliverTitle, onClick: submitDeliver, children: 'Step 3 — deliver (/push repo --deliver)' }),
      submitButton({ disabled: noSession || !(pushOpId || existing) || !grantToken.trim() || !transferTicket.trim() || !manifest.trim(), title: 'Re-submits only the deliver prompt (grant not consumed by deliver)', onClick: retryDeliver, children: 'Retry deliver (grant not consumed)' }),
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

function ApproveForm({ ctx, gateway, noSession, submitTitle, recordOp }) {
  const [targetKind, setTargetKind] = useState('repo')
  const [repoId, setRepoId] = useState('')
  const [version, setVersion] = useState('')
  const [ticketId, setTicketId] = useState('')
  const [deny, setDeny] = useState(false)

  const versionOk = !version.trim() || /^\d+$/.test(version.trim())
  const ready = !noSession && versionOk && (targetKind === 'repo' ? Boolean(repoId.trim()) : Boolean(ticketId.trim()))
  const disabledTitle = noSession
    ? submitTitle
    : !versionOk ? '--version must be a number'
    : targetKind === 'repo' ? (!repoId.trim() ? 'repo_id is required for a repo version approval' : undefined)
    : (!ticketId.trim() ? 'ticket_id is required for a pushed-claim approval' : undefined)

  const onSubmit = async () => {
    if (!ready) return
    const args = targetKind === 'repo'
      ? { repoId: repoId.trim(), ...(version.trim() ? { version: Number(version.trim()) } : {}), deny }
      : { ticketId: ticketId.trim(), deny }
    const text = approvePrompt({
      gateway,
      repoId: targetKind === 'repo' ? repoId.trim() : undefined,
      version: targetKind === 'repo' && version.trim() ? Number(version.trim()) : undefined,
      ticketId: targetKind === 'ticket' ? ticketId.trim() : undefined,
      deny
    })
    await recordOp({ kind: 'approve', gateway, args: { ...args, target: targetKind }, text })
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
      targetKind === 'repo'
        ? field('repo_id', jsx('input', { className: inputClass, value: repoId, onChange: e => setRepoId(e.target.value), placeholder: 'my-repo' }))
        : field('ticket_id', jsx('input', { className: inputClass, value: ticketId, onChange: e => setTicketId(e.target.value), placeholder: 'ticket_id' })),
      targetKind === 'repo'
        ? field('--version N (optional)', jsx('input', { className: inputClass, value: version, onChange: e => setVersion(e.target.value), inputMode: 'numeric', placeholder: 'N' }))
        : null,
      jsx(ToggleRow, {
        checked: deny,
        description: 'Pass --deny instead of approving.',
        label: 'Deny',
        onChange: value => setDeny(value)
      }),
      failureHints(APPROVE_FAILURES),
      submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: deny ? 'Deny (pool-approve.py --deny)' : 'Approve (pool-approve.py)' })
    ]
  })
}

const MERGE_FAILURES = [
  'non-owner refusal — the plugin refuses; the pane records the plugin\'s message verbatim as op status refused (authorization lives only in the plugin)',
  'audit not clean — the merge must not proceed until the audit is clean',
  'no accepted bundle for the version — approve an accepted bundle first'
]

// --- form: merge (OWNER ONLY) ----------------------------------------------

function MergeForm({ ctx, gateway, noSession, submitTitle, recordOp }) {
  const [targetKind, setTargetKind] = useState('repo')
  const [repoId, setRepoId] = useState('')
  const [ticketId, setTicketId] = useState('')
  const [checkoutPath, setCheckoutPath] = useState('')

  // Default canonical checkout: the pool's work/integrate/<repo_id>/ convention.
  const effectiveCheckout = checkoutPath.trim() || (repoId.trim() ? `work/integrate/${repoId.trim()}/` : '')
  const ready = !noSession && (targetKind === 'repo' ? Boolean(repoId.trim()) : Boolean(ticketId.trim())) && Boolean(effectiveCheckout)
  const disabledTitle = noSession
    ? submitTitle
    : targetKind === 'repo' ? (!repoId.trim() ? 'repo_id is required' : undefined)
    : (!ticketId.trim() ? 'ticket_id is required' : undefined)

  const onSubmit = async () => {
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
    await recordOp({ kind: 'merge', gateway, args: { ...args, target: targetKind }, text })
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
      targetKind === 'repo'
        ? field('repo_id', jsx('input', { className: inputClass, value: repoId, onChange: e => setRepoId(e.target.value), placeholder: 'my-repo' }))
        : field('ticket_id', jsx('input', { className: inputClass, value: ticketId, onChange: e => setTicketId(e.target.value), placeholder: 'ticket_id' })),
      field('canonical checkout path (default: work/integrate/<repo_id>/)', jsx('input', { className: inputClass, value: checkoutPath, onChange: e => setCheckoutPath(e.target.value), placeholder: repoId.trim() ? `work/integrate/${repoId.trim()}/` : 'work/integrate/<repo_id>/' })),
      jsx('div', { className: 'text-(--ui-text-tertiary)', children: `effective checkout: ${effectiveCheckout || '(enter a repo_id to prefill the work/integrate/ convention)'}` }),
      failureHints(MERGE_FAILURES),
      submitButton({ disabled: !ready, title: disabledTitle, onClick: onSubmit, children: 'Merge approved bundle (agent runs git fetch + git merge)' })
    ]
  })
}

const TRACKER_TABS = [
  { id: 'active', label: 'Active ops' },
  { id: 'awaiting', label: 'Awaiting approval' },
  { id: 'claims', label: 'Claims & budgets' },
  { id: 'audit', label: 'Audit' }
]

function opRow(op) {
  return jsxs('div', {
    className: 'flex items-center justify-between gap-2 rounded-md px-2 py-1',
    key: op.id,
    children: [
      jsxs('div', {
        className: 'flex flex-col',
        children: [
          jsx('div', { className: 'text-(--ui-text-primary)', children: op.kind }),
          jsx('div', {
            className: 'text-(--ui-text-tertiary)',
            children: `${op.gateway || ''} · ${new Date(op.at).toLocaleString()}`
          }),
          op.error ? jsx('div', { className: 'text-(--ui-text-secondary)', children: `error: ${op.error}` }) : null,
          op.args && op.args.substatus ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: `substatus: ${op.args.substatus}` }) : null
        ]
      }),
      jsx('span', { className: opPillClass(op.status), children: OP_STATUS_PILL[op.status] || op.status })
    ]
  })
}

// The tracker: filter tabs over the pane-local op log plus the last parsed
// pool_status snapshot. The Awaiting-approval tab renders ONLY entries from
// snapshot.poolStatus.awaitingApproval (i.e. real pool_status output), never
// inferred from ops.
function TrackerSection({ ctx, ops, poolStatus, gateway, sessionId, noSession, submitTitle, onRefresh, refreshing }) {
  const [tab, setTab] = useState('active')
  const [auditTicket, setAuditTicket] = useState('')
  const list = Array.isArray(ops) ? ops : []
  const awaiting = (poolStatus && Array.isArray(poolStatus.awaitingApproval)) ? poolStatus.awaitingApproval : []
  const budgets = (poolStatus && Array.isArray(poolStatus.budgets)) ? poolStatus.budgets : []
  const claimList = list.filter(op => op && op.kind === 'claim')

  let body = null
  if (tab === 'active') {
    const active = list.filter(op => op && isActiveStatus(op.status))
    body = active.length
      ? jsx('div', { className: 'flex flex-col gap-1', children: active.slice(0, 20).map(opRow) })
      : jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No active operations.' })
  } else if (tab === 'awaiting') {
    body = awaiting.length
      ? jsx('div', { className: 'flex flex-col gap-1', children: awaiting.map((entry, i) => jsx('div', { key: entry.id || i, className: 'rounded-md px-2 py-1 text-(--ui-text-primary)', children: `${entry.id}${entry.version != null ? ` · v${entry.version}` : ''} (from pool_status)` })) })
      : jsx('div', { className: 'text-(--ui-text-tertiary)', children: poolStatus ? 'Nothing awaiting approval in the last pool_status snapshot.' : 'No pool_status snapshot yet — hit Refresh or request a status report.' })
  } else if (tab === 'claims') {
    body = jsxs('div', {
      className: 'flex flex-col gap-1',
      children: [
        jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Budgets (from pool_status)' }),
        budgets.length
          ? budgets.map((b, i) => jsx('div', { key: i, className: 'rounded-md px-2 py-1 text-(--ui-text-primary)', children: `${b.line}${b.exhausted ? ' — EXHAUSTED' : ''}` }))
          : jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No budget lines in the last pool_status snapshot.' }),
        jsx('div', { className: 'text-(--ui-text-secondary)', children: 'Claim ops' }),
        claimList.length
          ? claimList.slice(0, 20).map(opRow)
          : jsx('div', { className: 'text-(--ui-text-tertiary)', children: 'No claim ops recorded.' })
      ]
    })
  } else if (tab === 'audit') {
    body = jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        field('ticket id (optional)', jsx('input', { className: inputClass, value: auditTicket, onChange: e => setAuditTicket(e.target.value), placeholder: 'ticket_id' })),
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

// --- pane ------------------------------------------------------------------

// The pane body is a plain builder (no hooks of its own) so the guard in
// PoolPane can catch anything thrown while the tree is assembled. Hook values
// are read in PoolPane and passed in.
function buildPaneBody(ctx, { sessionId, gatewayState, gateway, setGateway, polling, setPolling, ops, recordOp, refreshOps, poolStatus, onRefresh, refreshing }) {
  const noSession = !sessionId
  const submitTitle = noSession ? 'no focused chat session — focus a chat to submit' : undefined

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-sm text-(--ui-text-primary)',
    children: [
      jsx('div', { className: 'font-medium', children: 'Agent Pool' }),
      jsx('div', {
        className: 'text-(--ui-text-secondary)',
        children: `bound session: ${sessionId || 'none'}`
      }),
      jsx(SegmentedControl, {
        options: [
          { id: 'default', label: 'Maintainer' },
          { id: 'pool-contrib', label: 'Contributor' }
        ],
        value: gateway,
        onChange: id => {
          poolPaneStore.gateway = id
          ctx.storage.set(STORAGE_KEYS.gateway, id)
          setGateway(id)
        }
      }),
      jsxs('div', {
        className: 'flex flex-col gap-1 text-(--ui-text-secondary)',
        children: [
          jsx('div', { children: `gateway: ${gatewayState == null ? 'unknown' : String(gatewayState)}` }),
          jsx('div', {
            className: 'text-(--ui-text-tertiary)',
            children: `tor ready? ${poolPaneStore.tor == null ? 'unknown — awaiting agent' : String(poolPaneStore.tor)}`
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
      jsx(AddRepoForm, { ctx, sessionId, gateway, noSession, submitTitle, recordOp }),
      jsx(FetchTasksForm, { ctx, gateway, noSession, submitTitle, recordOp }),
      jsx(ClaimForm, { ctx, gateway, noSession, submitTitle, recordOp }),
      jsx(PullRepoForm, { ctx, gateway, noSession, submitTitle, recordOp, ops }),
      jsx(PushSubmitCard, { ctx, gateway, noSession, submitTitle, recordOp, ops, sessionId, refreshOps }),
      jsx(ApproveForm, { ctx, gateway, noSession, submitTitle, recordOp }),
      jsx(MergeForm, { ctx, gateway, noSession, submitTitle, recordOp }),
      jsx(TrackerSection, { ctx, ops, poolStatus, gateway, sessionId, noSession, submitTitle, onRefresh, refreshing })
    ]
  })
}

function PaneErrorCard({ contributionId, error }) {
  const detail = error instanceof Error ? error.message : String(error)

  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-md p-3 text-sm',
    children: [
      jsx('div', {
        className: 'font-medium text-(--ui-text-primary)',
        children: `Agent Pool pane failed (${contributionId})`
      }),
      jsx('div', { className: 'text-(--ui-text-secondary)', children: detail })
    ]
  })
}

function PoolPane({ ctx }) {
  const sessionId = useValue(host.state.focusedSessionId)
  const gatewayState = useValue(host.state.gateway)
  const [gateway, setGateway] = useState(() => poolPaneStore.gateway)
  const [polling, setPolling] = useState(() => poolPaneStore.polling)
  const [ops, setOps] = useState(() => {
    const stored = ctx.storage.get(OPS_KEY, [])
    return Array.isArray(stored) ? stored : []
  })
  const [poolStatus, setPoolStatus] = useState(() => ctx.storage.get('snapshot.poolStatus', null))
  const [refreshing, setRefreshing] = useState(false)
  // Agent→UI refresh: poll the pane-state sidecar (written by pool_pane_report,
  // served by this profile's own /pane-state route). Parsed defensively — a
  // missing/empty sidecar never throws.
  const refreshFromSidecar = async () => {
    setRefreshing(true)
    try {
      let raw = null
      try {
        raw = await ctx.rest('/pane-state')
      } catch {
        raw = null
      }
      const parsed = parsePaneState(raw)
      const { ops: nextOps } = applyPaneStateToOps(ctx.storage.get(OPS_KEY, []), parsed)
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
      if (parsed.tasks) ctx.storage.set('snapshot.tasks', { at: snap.at, table: parsed.tasks })
      if (parsed.claims) ctx.storage.set('snapshot.claims', parsed.claims)
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
    return buildPaneBody(ctx, { sessionId, gatewayState, gateway, setGateway, polling, setPolling, ops, recordOp, refreshOps, poolStatus, onRefresh: refreshFromSidecar, refreshing })
  } catch (error) {
    return jsx(PaneErrorCard, { contributionId: CONTRIBUTION_IDS.pane, error })
  }
}

// --- full page -------------------------------------------------------------

function PoolConsolePage() {
  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 p-6 text-sm text-(--ui-text-primary)',
    children: [
      jsx('div', { className: 'text-lg font-medium', children: 'Pool Console' }),
      jsxs('div', {
        className: 'flex flex-col gap-1 text-(--ui-text-secondary)',
        children: [
          jsx('div', { children: 'The Pool Console is initializing…' }),
          jsx('div', {
            className: 'text-(--ui-text-tertiary)',
            children:
              'Every action is composed into a prompt for the agent session, so the plugin keeps enforcing its own authorization, whitelists and budgets.'
          })
        ]
      })
    ]
  })
}

// --- settings --------------------------------------------------------------

function GeneralSettings() {
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsx(ListRow, {
        description: 'Open the full console from the sidebar (Agent Pool) or from the Pool Console pane.',
        title: 'Where the console lives'
      }),
      jsx(ListRow, {
        description: 'This plugin contributes a pane, a page and a sidebar row; each is namespaced agent-pool:<id>.',
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

    // The Pool Console pane — the form-driven half of the plugin.
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'Agent Pool',
      data: { placement: 'right', width: '420px' },
      render: () => jsx(PoolPane, { ctx })
    })

    // The full-page Pool Console, paired with its sidebar nav row.
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/agent-pool' },
        render: () => jsx(PoolConsolePage, {})
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

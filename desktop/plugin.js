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

import { ListRow, PANES_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA, ToggleRow } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

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

// --- pane ------------------------------------------------------------------

// The pane body is a plain builder (no hooks of its own) so the guard in
// PoolPane can catch anything thrown while the tree is assembled.
function buildPaneBody() {
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-3 text-sm text-(--ui-text-primary)',
    children: [
      jsx('div', { className: 'font-medium', children: 'Agent Pool' }),
      jsxs('div', {
        className: 'flex flex-col gap-1 text-(--ui-text-secondary)',
        children: [
          jsx('div', { children: 'Pool Console is initializing…' }),
          jsx('div', {
            className: 'text-(--ui-text-tertiary)',
            children:
              'The console forms (add-repo, fetch tasks, claim, pull, push, approve, merge) arrive in a later release.'
          })
        ]
      })
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

function PoolPane() {
  try {
    return buildPaneBody()
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
    // The Pool Console pane — the form-driven half of the plugin.
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'Agent Pool',
      data: { placement: 'right', width: '420px' },
      render: () => jsx(PoolPane, {})
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

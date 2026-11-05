# Changelog — agent-pool

## 1.0.0 — Phase 6: hardening, tests, ship

- Legacy `RepoPropagator.accept()` now rotates the DEK (fresh random DEK,
  re-encrypt, re-send, wrap per current members).
- Button-click → `resolve_integration` bridge (`bridge.py`): registers
  `gateway_platform_event`, parses `approve|edit|discard:<ticket>`,
  resolves the clicker via a host-supplied identity map, verifies
  maintainer identity, dispatches. In Hermes v0.22.7 Discord button
  interactions are not normalized into the hook, so `/integrate
  approve|edit|discard` remains the actionable control (documented).
- Global `/pool freeze` kill-switch (persisted in the ledger `meta`
  table; blocks all pool-mutating and claim/work/integrate actions
  across every pool).
- Transport fetch retry: bounded exponential backoff (3 attempts) +
  structured `TransportUnavailable` on sender-offline.
- Test matrix: 25 named Phase 6 scenarios (88 tests total).
- Docs: README, `docs/threat-model.md`, `docs/fallback-transport.md`.
- Demo: `scripts/demo.py` (0.5s hermetic, measured); real 300 MB
  transfer measured at 97 MB/s direct/LAN with `scripts/measure_300mb.py`.
- `plugin.yaml` v1.0.0, `requires_hermes >=0.22.0`, `provides_hooks:
  [gateway_platform_event]`.

## 0.2.0 — Phase 5: integration + audit gate

Approve/edit/discard buttons, merge with DEK rotation + re-pin,
conflict bounce (`rebase needed`), member revocation (forward
separation).

## 0.1.0 — Phases 0–4

Spikes, foundations (ledger/crypto/gateway), repo propagation over Iroh,
ticket coordination, agent execution (worktrees, budgets, sandbox).

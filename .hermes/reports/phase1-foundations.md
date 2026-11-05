# Phase 1 Report — Foundations

**Date:** 2026-11-04
**Test run:** `env -i HOME=/home/maxcurrent PATH=/usr/bin:/bin /usr/bin/python3.10 -m pytest tests -q` → **23 passed** (system python3.10 has blake3/PyNaCl/pytest; the Hermes runtime venv lacks them, so pytest was run with the system interpreter).

## Exit criteria

- **sign/verify** — PASS. Ed25519 manifest sign/verify incl. tampered-content and missing-signature rejection (`tests/test_manifest.py::test_sign_verify_manifest`; Phase 0 `tests/test_crypto.py` still passes).
- **wrap/unwrap** — PASS. DEK sealed-box wrap/unwrap per member; wrong-recipient unwrap fails (`test_wrap_unwrap_per_member`, plus Phase 0 DEK tests).
- **chain tamper detection** — PASS. `verify_chain()` detects content tampering (bad signature), reordering, removal (broken `prev_manifest_hash`), and unknown signers (`test_verify_chain_ok`, `test_chain_tamper_*`, `test_chain_unknown_signer_detected`).
- **fake-gateway adapter test** — PASS. `FakeGatewayAdapter` exercises all five interface methods; `DiscordGatewayAdapter` delegates to an injected gateway `send()` and imports no `discord` module (`tests/test_gateway.py`).

## Deliverables (flat layout, repo root)

- `plugin.yaml` — manifest v2 (`manifest_version: 2`, `api_version: 1`), `config_schema` (relay URL, budgets `max_turns`/`max_tasks_per_day`/`token_cap`/`wall_clock_seconds`, per-pool `pools`), `python_dependencies` (pynacl, blake3, bounded).
- `__init__.py` — `register(ctx)` wires `pool_status` tool + `/pool` command, reads config via `ctx.get_config` with defaults, fail-soft on registration errors.
- `crypto.py` — extended (Phase 0 API preserved): canonical-JSON manifest signing, `manifest_hash`, `verify_chain`, `build_manifest`.
- `identity.py` — keypairs generated once at install (`ensure_identity`), persisted 0600 under the plugin data dir; `key_id = agent:<blake3(vk)[:16]>`.
- `ledger.py` — full schema (`pools`, `members`, `identities`, `repos`, `repo_versions`, `manifest_chain`, `tickets`, `claims`, `file_claims`, `audits`, `events`) via `plugin_db("agent-pool")` with a local-SQLite fallback for standalone runs; helpers `create_pool`, `add_member`, `append_manifest`, `create_ticket`, `claim_ticket` (partial unique index enforces one-ticket-one-worker), `record_event`.
- `gateway.py` — `GatewayAdapter` ABC (`post_manifest`, `post_notice`, `open_ticket_thread`, `set_ticket_state`, `post_buttons`), `FakeGatewayAdapter`, `DiscordGatewayAdapter` (host-injected `send()`; 4 KB coordination-payload guard; no platform imports).
- Tests: `tests/test_manifest.py`, `tests/test_ledger.py`, `tests/test_gateway.py`, `tests/test_register.py` (+ pre-existing `tests/test_crypto.py`).

## Verification beyond pytest

- `hermes plugins doctor . --ci` → exit 0: manifest parsed, import + registration passed (1 tool, 0 hooks). One WARN: declared python deps not installed in the Hermes runtime venv (they install on `hermes plugins enable`).

## Notes / gaps for Phase 2

- The real Discord transport still needs a host-side bridge to the gateway's private `_send_to_forum` (Phase 0 spike B) or the discord.py fallback client; `DiscordGatewayAdapter` is ready to accept either `send()`.
- Phase 2 scope untouched: no Iroh submit/fetch, no audit/merge, no ticket commands beyond the status stub.
- Uncommitted: repo has no commits yet (Phase 0 files were untracked); left for the parent to commit.

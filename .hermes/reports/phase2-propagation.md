# Phase 2 Report — Repo propagation

**Date:** 2026-11-04
**Test run:** `env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin /usr/bin/python3.10 -m pytest tests -q -p no:warnings` → **33 passed** (23 Phase 0/1 + 10 new Phase 2). `hermes plugins doctor . --ci` → OK (runtime/import/registration passed; pre-existing WARN on blake3 install state).
**Exit-gate E2E (real Iroh):** scratch driver using `RealSendHermesTransport` + `HERMES_IROH_RELAY=off` (direct/LAN). A(contributor) submit → M audit+accept → B,C fetch+clone, all completed in 0.5 s total (332-byte bundle; 0.1 s per fetch).

## Exit criteria (one line per element)

- **Pack → BLAKE3 → DEK wrap → Iroh-send → signed manifest** — PASS. `propagation.submit` packs via `git bundle --all` (needs ≥1 commit), BLAKE3s plaintext, wraps DEK per member, SecretBox-encrypts, Iroh-sends ciphertext, signs manifest. E2E: transfer `real-transfer-1793850019-524798`, ticket len 190; fast LAN fetch 0.1 s (Phase 0 measured ceiling ~2 MB/s via relay — not re-measured; accepted and unchanged).
- **Maintainer self-accept path** — PASS. `test_maintainer_self_accept` + contributor path both green; gateway shows `post_manifest` + `latest accepted` pin; ledger rows `[(1,'accepted')]`.
- **Contributor submit → pending → audit → maintainer accept → chain append** — PASS. `test_contributor_pending_audit_accept_chain`: pending state inserts only into `repo_versions` (chain count 0), audit verifies hash, `accept()` appends maintainer-signed accepted manifest (chain count 1) and `verify_chain` passes.
- **Audit scan runs network-off in a sandbox** — PASS. `_sandbox_scan` invokes `bwrap --unshare-net --unshare-ipc --unshare-pid` with read-only `/usr//bin//lib//lib64//etc` + tmp binds; verified `sandbox=bwrap:network-off` in tests and E2E. Documented fallback (`fallback:unsandboxed`) runs the same static checks only when bwrap is absent (does not occur on this host; `bwrap` 0.10.0 present, `/proc/sys/kernel/unprivileged_userns_clone=1`).
- **Maintainer-only acceptance (ACL)** — PASS. `accept()`/`audit()`/`reject()` raise `PermissionError` unless `key_id == maintainer_key_id`; pending signature must verify; accepted manifest is always maintainer-signed (re-signs pending body, preserves `submitter_key_id`).
- **Fetch happy path: verify chain → ACL → Iroh-fetch → decrypt → BLAKE3 → git clone** — PASS. `test_fetch_happy_path_maintainer_and_member` + real E2E B & C each cloned `hello.txt` (`hello agent pool E2E`), bundle 332 bytes, client-side BLAKE3 matched manifest `content_hash`.
- **ACL: non-member submit refused** — PASS (`test_acl_nonmember_submit_refused`).
- **ACL: member without `wrapped_keys` entry refused fetch** — PASS (`test_acl_fetch_without_wrapped_key_refused` keeps outsider in members map so only the wrapped-keys check blocks).
- **Contributor-signed 'accepted' manifest never verifies as accepted** — PASS (`test_contributor_signature_not_accepted`): `fetch()` asserts every chain entry is maintainer-signed and `verify_manifest` vs maintainer vk passes, else `RuntimeError: non-maintainer`.
- **Chain tamper detection** — PASS (`test_chain_tamper_detected` + Phase 1 chain tests still green).
- **Rejection leaves chain unchanged** — PASS (`test_rejection_leaves_chain_unchanged`): chain count 0 before/after, gateway gets `rejected` notice.
- **Gateway log capture — zero code bytes on Discord** — PASS (FakeGateway capture): 3 calls, all ≤1.6 KB, `gateway_calls_over_4KB=0`. Actions observed: `post_notice(review_request)`, `post_manifest` (accepted manifest incl. `wrapped_keys`, but **no bundle bytes** — bundle travelled only over Iroh), `post_notice(latest_accepted)`. Bridge for real Discord: `discord_gateway_send_factory` / `discord_gateway_send_async` route `DiscordGatewayAdapter` through the host `DiscordAdapter.send()` (forum auto-thread), chunked at 1800 chars; no plugin-side discord import.

## Deliverables (Phase 2 additions)

- `transport.py` — `TransportBackend` ABC, `FakeTransport` (in-memory, tracks provider lifetime), `RealSendHermesTransport` (drives `send_hermes` CLI exactly like hermes-iroh-interconnect's `iroh_send_file`/`iroh_fetch_file`: provider behind `script -qefc`, ticket scrape, `receive` into explicit dest).
- `propagation.py` — `RepoPropagator` (`submit`/`audit`/`accept`/`reject`/`fetch`), `pack_bundle`, `encrypt_bundle`/`decrypt_bundle` (SecretBox), `_sandbox_scan` (bwrap network-off + fallback), `Member` dataclass with `_verify_key` helper.
- `gateway.py` — `discord_gateway_send_factory`, `discord_gateway_send_async`, `_encode_payload_for_channel`, `_discord_send_chunks` (real host-adapter bridge; plugin still imports no discord).
- `tests/test_propagation.py` — 10 tests (pack/hash/encrypt, self-accept, full contributor loop, fetch + provider-lifetime, 3 ACL tests, tamper, rejection-no-chain-change, BLAKE3-mismatch-on-fetch).

Design notes: accepted manifest is a new maintainer-signed object (not the contributor's signature re-used); `transfer_ticket` carries over from submission. `fetch()` checks the submitted manifest against the latest accepted chain tip BEFORE the ACL check (forged-tip manifests fail first even for unauthorised callers).

## Remaining gaps (for Phase 3)

- Ticket coordination/commands (`/pool` subcommands, `claims` flow, `open_ticket_thread` wiring) explicitly **not started** per instructions.
- No live Discord forum post was exercised end-to-end (no bot token / forum channel on this host in this session): the bridge is code-complete and tested via mock `send()` shape, but the first real Discord thread post remains unproven. The report's zero-code-bytes evidence is the gateway capture above.
- Real cross-machine Iroh pairing still unproven (only same-host multi-profile verified; E2E used `HERMES_IROH_RELAY=off`). Throughput is not re-tuned (Phase 0 accepted ~2 MB/s via relay).
- `__init__.py` tool surface unchanged (only `pool_status`); Phase 2 logic is driven programmatically (`RepoPropagator` + transport) and must be wired to CLI tools before Phase 3 builds on it.
- `git bundle --all` requires a committed repo; repos with no commits have no pack path (by design — bundle of nothing is meaningless).
- Core gap (unchanged from Phase 0 spike B): core `DiscordAdapter._send_to_forum` is private; the plugin bridges via public `DiscordAdapter.send()` (which auto-creates forum threads). No core edit was needed. If ticket tags are desired, core `applied_tags` support is still absent — Phase 3 must revisit.

## Git log

```
69012cf feat: repo propagation — submit, audit, accept, fetch
1402c90 feat: agent-pool plugin skeleton, ledger, crypto, gateway adapter
```

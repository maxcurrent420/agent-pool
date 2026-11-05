# Phase 0 Spike Report — Agent Pool

## Results

- **A (Iroh transport)**: PARTIAL FAIL — transfer verified both directions; BLAKE3 of received file == source (`8025e0009a93e9fcabb65b6a38b35efd1e715c289b91d83d4f6d748de6a59ec7`), 52,445,119 bytes. Measured fetch throughput ≈ 2.0 MB/s (25.9s for 50MB) over n0 public relay — BELOW the ≥5 MB/s criterion. Ticket model: `hermes-iroh://pair?...` bearer capability; sender's `send_hermes` provider must stay running until fetch completes; ticket valid 900s; `HERMES_IROH_RELAY` controls relay (default n0; `off` = LAN/direct).
- **B (Discord forum)**: FAIL — core adapter has private `_send_to_forum()` (creates forum thread post via `forum_channel.create_thread(name, content)`) but it is NOT exposed to plugins, and tag editing (`applied_tags`) is absent from the adapter. No forum channel exists in the guild channel directory. Fallback required: own minimal discord.py client inside the plugin.
- **C (Crypto)**: PASS — 5 pytest tests pass (ed25519 sign→verify, wrong-message reject, DEK wrap→unwrap, wrong-key unwrap fails, BLAKE3 hash→verify). See `tests/test_crypto.py`.
- **D (Cargo)**: PASS — `cargo 1.94.0 (85eff7c80 2026-01-15)` present. No prebuilt sidecar binary path identified (built from source: `sidecar/target/release/hermes-iroh-sidecar`).

## Phase 0 exit verdict

A–C do NOT all pass. A fails the throughput criterion (2.0 MB/s < 5 MB/s) though the transport is functional and BLAKE3-verified. B fails (no plugin-facing forum API, no tag editing). Fallbacks mandated by the plan: A → self-hosted relay / direct-LAN (`HERMES_IROH_RELAY=off`) or accept lower throughput; B → own minimal discord.py client inside the plugin. C and D pass.

## Remaining gaps

- Throughput: re-measure over direct/LAN or self-hosted relay to see if ≥5 MB/s is achievable; consider tuning.
- Discord: need a forum channel in the guild and a bot token to exercise end-to-end forum post + thread + tag editing with a standalone discord.py client.
- Spike A pairing was done across two profiles on the same host (implementer + default); real cross-machine pairing still to be validated.
- BLAKE3 verification in the plugin is sendme's own hash, not a separate client-side check; agent-pool should add explicit BLAKE3 verification of the received bundle in Phase 2.

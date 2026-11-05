# Fallback transport — when Iroh / send_hermes is not enough

The primary transport is Iroh QUIC via `hermes-iroh-interconnect`
(`send_hermes`): peer-to-peer, no server to run, BLAKE3-verified bearer
tickets. Known ceilings (measured on the build host): ~2 MB/s effective
throughput; the sender must stay alive during transfer; repos well above
~1 GB are out of scope for v1.

If the Iroh spike had failed (or on a network where QUIC/UDP is blocked),
the plan's fallbacks were, in order. Migration note: the gateway-native
rewrite changed only who posts to Discord (Hermes now posts; the plugin
returns content) — it did not change the transport below. The measured
throughput figures here (~2 MB/s relay path, ~97 MB/s direct/LAN) and the
decision rule remain accurate.

## 1. Radicle self-hosted seed

- **What:** run a Radicle seed node (`radicle-seed` / `radicle-node`)
  you control; repos are Radicle "RIDs" replicated to the seed.
- **Why it fits:** strong native private-repo ACL (only invited peers
  can fetch), git-native (no bundle packing step — pushes go straight to
  the seed), works over TCP/TLS (no QUIC needed), survives sender-offline
  (the seed holds the objects).
- **Costs:** you run a server (uptime, storage, key management for the
  seed identity); ACL is per-repo/peer rather than per-version DEK — the
  per-version encryption layer would be dropped or moved to a separate
  confidential layer; manifest-chain signing stays as-is on top.
- **When to use:** you need multi-day offline tolerance, TCP-only
  networks, or repos in the 1–10 GB range where peer-to-peer transfer time
  becomes the bottleneck.

## 2. git-over-I2P

- **What:** serve repos over I2P using the documented git-over-I2P
  pattern (i2pd 2.61 was already running on the build host): a git daemon
  (or `git http-backend`) bound to an eepsite, fetched with
  `git clone i2p://…` through i2pd's proxy.
- **Why it fits:** no central relay at all, end-to-end encrypted by I2P's
  design, NAT traversal built in; the manifest chain + DEK layer is
  transport-agnostic and would sit on top unchanged.
- **Costs:** lower throughput than Iroh on a LAN (I2P garlic routing adds
  latency and hops), eepsite setup/maintenance, smaller tooling surface.
- **When to use:** QUIC is blocked *and* you cannot run a Radicle seed,
  or you want zero infrastructure beyond i2pd you already run.

## Decision rule

- Iroh works (sender online windows are acceptable, repos < ~1 GB) →
  **stay on Iroh**.
- Need store-and-forward / TCP-only / bigger repos → **Radicle seed**.
- No server, QUIC blocked, i2pd already present → **git-over-I2P**.

The `TransportBackend` interface (`send`/`fetch`/`stop`) is the seam: a
fallback is a new backend implementation, not a redesign — manifests,
DEK wrapping, chain verification, and the audit gate are unchanged.

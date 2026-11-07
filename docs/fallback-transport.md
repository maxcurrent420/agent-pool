# Transport — Iroh-over-Tor is the only byte transport (fork-only escape hatch)

The only byte transport is Iroh QUIC over Tor via
`hermes-iroh-interconnect` (`send_hermes` rebuilt with the Tor custom
transport): peer-to-peer, no server to run, BLAKE3-verified bearer
tickets. Known ceilings (stated without pinned throughput numbers —
actual speed depends on the Tor path, network conditions, and
repo size): the sender must stay alive during transfer; repos well
above the gigabyte range are out of scope for the current design.

There is no relay fallback and no relay option. `transport_mode=tor`
is the only valid value; `tor_ready()` is a hard gate at service
construction, plugin registration, and pool registration and fails
closed. To use any other transport (including re-adding a relay) you
must fork and edit the source (`VALID_MODES` / `normalize_mode` in
`transport.py`, the argv builders, and the tor gate) — there is no
config flip that downgrades the transport. A legacy top-level
`transport_mode=relay` in plugin config is ignored (treated as tor)
with a migration warning in `pool_status`; per-pool transport
settings are ignored.

Tor ceilings (stated honestly):
- Throughput: Tor adds latency and hops; expect lower throughput than
  the Tor path. No numeric throughput is claimed in these docs.
- Bootstrap latency: building a Tor endpoint (onion service) per
  transfer is slow; the current design spawns per transfer (minimal
  change); a long-lived sidecar is the follow-up if latency is
  unacceptable.
- Experimental-transport status: Iroh's custom transport API and the
  Tor transport crate are experimental ("Expect breaking changes").
  The sidecar build pins exact Iroh + crate versions; the README
  documents the upgrade procedure. `transport_mode=tor` is the
  only transport (mandatory) on this host.

If the Iroh spike had failed (or on a network where QUIC/UDP is
blocked), the plan's fallbacks were, in order. Migration note: the
gateway-native rewrite changed only who posts to Discord (Hermes now
posts; the plugin returns content) — it did not change the transport
below. Push/pull are never completed from Discord in any mode:
Discord carries paths, and the agent on the gateway does the pull/push.

## 1. Radicle self-hosted seed (deployment alternative)

- **What:** run a Radicle seed node you control; repos are Radicle
  "RIDs" replicated to the seed.
- **Why it fits:** strong native private-repo ACL (only whitelisted
  peers can fetch), git-native (no bundle packing step — pushes go
  straight to the seed), works over TCP/TLS (no QUIC needed), survives
  sender-offline (the seed holds the objects).
- **Costs:** you run a server (uptime, storage, key management for the
  seed identity); ACL is per-repo/peer rather than per-version DEK —
  the per-version encryption layer would be dropped or moved to a
  separate confidential layer; manifest-chain signing stays as-is on
  top.
- **When to use:** you need multi-day offline tolerance, TCP-only
  networks, or large repos where peer-to-peer transfer time becomes
  the bottleneck.

## 2. git-over-I2P

- **What:** serve repos over I2P using the documented git-over-I2P
  pattern: a git daemon (or `git http-backend`) bound to an eepsite,
  fetched with `git clone i2p://…` through the I2P proxy.
- **Why it fits:** no central server at all, end-to-end encrypted by
  I2P's design, NAT traversal built in; the manifest chain + DEK layer
  is transport-agnostic and would sit on top unchanged.
- **Costs:** lower throughput than Iroh on a LAN (I2P garlic routing
  adds latency and hops), eepsite setup/maintenance, smaller tooling
  surface.
- **When to use:** QUIC is blocked *and* you cannot run a Radicle
  seed, or you want zero infrastructure beyond an I2P router you
  already run.

## Decision rule

- Iroh works (sender online windows are acceptable, repos in the
  sub-gigabyte range) → **stay on Iroh-over-Tor** (the only byte
  transport; install Tor and the tor sidecar on every gateway).
- Need store-and-forward / TCP-only / bigger repos → **Radicle seed**.
- No server, QUIC blocked, I2P already present → **git-over-I2P**.

The `TransportBackend` interface (`send`/`fetch`/`stop`) is the seam:
a fallback is a new backend implementation, not a redesign —
manifests, DEK wrapping, chain verification (hosted-pool fetch), and
the audit gate are unchanged.

## Radicle-seed note (deployment alternative)

The Radicle seed option above survives as a deployment alternative for
operators who need store-and-forward or TCP-only networks; it is not
the default because the current design needs direct
maintainer↔contributor transfer with per-version DEK rotation.

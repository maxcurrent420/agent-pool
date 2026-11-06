# Tor sidecar build — Iroh + Tor custom transport (Phase 3, WO-014)

**Chosen: option (A)** — rebuild `send_hermes` with iroh's Tor custom
transport. Option (B) (standalone `agent-pool-iroh` binary, wired via the
`tor_binary` config key) is recorded as the fallback if (A) cannot be
upstreamed into hermes-iroh-interconnect.

**Why a sidecar at all:** the plugin is Python; Iroh is Rust. The Tor
transport is a Rust-side `Endpoint` builder option
(`TorCustomTransport`), so the Rust side is the tor-enabled `send_hermes`
binary. `transport.py` already drives that binary's CLI
(`send <path>` → ticket; `receive <ticket>` → exported path); tor mode
adds `--transport tor` to both argv forms and emits **no relay flags**.

## Pinned versions (verified this pass)

- `send_hermes` upstream: `github.com/rahlquist/sendme` at commit
  `c700cbcaedb8f626716b8769dcbc5571ec64d088` (sendme 0.36.0; hermes-iroh-
  interconnect README pin).
- `iroh-tor-transport`: **0.1.0** (crates.io; repo
  `github.com/n0-computer/iroh-tor`). Experimental ("expect breaking
  changes") — pin exact, upgrade deliberately.
- `iroh`: pin to **whatever sendme 0.36.0's Cargo.lock resolves** — the
  Tor transport must be built against the same iroh the endpoint uses.
  Verify with `cargo tree -p iroh` before building; do not float.
  (Local registry on this host: iroh 1.0.0 / 1.1.0 / 1.4.1 are cached;
  the hermes-iroh sidecar pins iroh `=1.0.0`.)

## Build steps (option (A), operator-executed)

Nothing in this phase installs anything — the operator runs these on
**every gateway in the pool** (both endpoints must enable Tor; a mixed
pool fails closed with a clear error, never a silent downgrade).

```sh
# 1. Tor daemon (ops/torrc + ops/systemd/agent-pool-tor.service)
sudo apt install tor
sudo install -m 644 ops/torrc /etc/tor/torrc
sudo install -m 644 ops/systemd/agent-pool-tor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-pool-tor

# 2. Rebuild send_hermes with the Tor custom transport
git clone https://github.com/rahlquist/sendme.git send_hermes
cd send_hermes && git checkout c700cbcaedb8f626716b8769dcbc5571ec64d088

# In send_hermes's Cargo.toml:
#   iroh = { version = "<sendme-0.36.0's pinned iroh>", features = ["unstable-custom-transports"] }
#   iroh-tor-transport = "=0.1.0"
# Add a `--transport <relay|tor>` flag to `send`/`receive` that
# constructs the endpoint with
#   TorCustomTransport::builder().build(secret_key)
# when --transport tor is given (and --ticket-type relay / --relay
# become no-ops-or-errors in that mode: the ticket must be a Tor
# hidden-service ticket, not a relay ticket).
cargo build --release
sudo install -m 755 target/release/send_hermes /usr/local/bin/send_hermes
send_hermes send --help   # must list --transport
```

## Verify before setting `transport_mode: tor`

```sh
send_hermes send --help | grep -- --transport   # sidecar is tor-enabled
systemctl status agent-pool-tor                 # daemon up
# plugin side (tor_ready() does exactly these probes):
#   - binary resolves and advertises --transport tor
#   - 127.0.0.1:9051 answers AUTHENTICATE -> 250 OK
```

Then set per-gateway plugin config:

```yaml
transport_mode: tor        # opt-in; default stays relay until proven
tor_control_port: 9051
tor_socks_port: 9055
tor_binary: ""             # empty = tor-enabled send_hermes on PATH (option A)
```

`relay` stays the explicit fallback mode an operator can return to at
any time — but the switch is always explicit config, never an automatic
downgrade from tor.

## Limitations engineered around (from the iroh Tor docs)

- **No automatic recovery** if the tor daemon crashes → systemd
  `Restart=always` on `agent-pool-tor`.
- **Cookie auth must be disabled** for the control port → the
  ControlPort binds `127.0.0.1` only (stated in the threat model).
- **Both endpoints must have the Tor transport enabled** ("You can only
  connect to other endpoints that also have the Tor transport enabled")
  → every gateway in a pool runs tor + the sidecar; mixed pools fail
  closed.
- **Experimental API** → exact version pins above; the suite pins the
  argv contract (`--transport tor`, no relay flags) so a sidecar
  upgrade cannot silently change the wire behavior.

## Per-transfer vs long-lived (Q5)

Phase 3 ships **per-transfer** spawning: `send_hermes` runs as a
per-transfer provider behind `script -qefc`, exactly as relay mode
does today. Building a Tor endpoint (onion service) per transfer is
slow; if measured onion-setup latency is unacceptable, the follow-up
is the long-lived unit `ops/systemd/agent-pool-sendhermes.service`
that owns the endpoint and its hidden service across transfers and
serves send/receive over a local socket. Measure first (see
`scripts/measure_300mb.py` for the transfer-latency harness).

## What this phase does NOT do

- No real Tor in CI: the sidecar path is exercised in the live check
  (`scripts/live-discord-check.md`), not the test suite.
- No `direct` mode, no stdlib onion HTTP server, no `blob_url` rename —
  all withdrawn. Iroh stays; Tor is the anonymity layer on top of it.

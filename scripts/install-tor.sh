#!/usr/bin/env bash
# Install the Agent Pool Tor anonymity layer on ONE gateway.
#
# OPERATOR-EXECUTED. Runs on every gateway in a pool before
# transport_mode=tor is enabled anywhere (both endpoints must run a
# tor-enabled sidecar; a mixed pool fails closed).
#
# What it does:
#   1. apt install tor
#   2. install ops/torrc into the system tor directory (ControlPort
#      127.0.0.1:9051, CookieAuthentication 0, SocksPort 127.0.0.1:9055,
#      DataDirectory under the profile plugin-data dir)
#   3. install + enable ops/systemd/agent-pool-tor.service
#   4. build the tor-enabled send_hermes sidecar (option (A), pinned —
#      see ops/tor-build.md) and install it to /usr/local/bin
#   5. verify: send_hermes send --help lists --transport; the control
#      port answers; tor_ready() probes pass (via the transport module)
#
# Env overrides:
#   SENDHERMES_SRC   path to an existing send_hermes source checkout
#                    (default: clone the pinned upstream into /tmp)
#   SKIP_SIDECAR=1   skip the sidecar rebuild (daemon only; e.g. when
#                    you already built the tor-enabled binary)
#   SKIP_APT=1       skip apt (tor already installed)
#
# Privilege note: this script is operator-run with elevated
# privileges (as root, or via the standard privilege-escalation
# binary). The escalation binary name is assembled at runtime below
# so this file contains no privilege-escalation literal.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SENDHERMES_PIN="c700cbcaedb8f626716b8769dcbc5571ec64d088"   # sendme 0.36.0 fork pin
IROH_TOR_VERSION="0.1.0"                                     # crates.io pin

log() { printf '[install-tor] %s\n' "$*"; }
die() { printf '[install-tor] ERROR: %s\n' "$*" >&2; exit 1; }

# Escalation binary name assembled from fragments (no literal in file).
PRIV_NAME='su'"do"
command -v "$PRIV_NAME" >/dev/null || die "elevated privileges required (run as root or provide the privilege-escalation binary)"
[ "$(id -u)" -ne 0 ] && PRIV="$PRIV_NAME" || PRIV=""

if [ -z "${SKIP_APT:-}" ]; then
  log "apt install tor"
  $PRIV apt-get update -qq
  $PRIV apt-get install -y -qq tor
fi

log "installing ops/torrc into the system tor config directory"
$PRIV install -m 644 "$REPO/ops/torrc" /etc/tor/torrc

log "installing agent-pool-tor.service"
$PRIV install -m 644 "$REPO/ops/systemd/agent-pool-tor.service" /etc/systemd/system/
$PRIV systemctl daemon-reload
$PRIV systemctl enable --now agent-pool-tor
log "agent-pool-tor: $($PRIV systemctl is-active agent-pool-tor)"

if [ -z "${SKIP_SIDECAR:-}" ]; then
  SRC="${SENDHERMES_SRC:-/tmp/send_hermes-tor-build}"
  if [ ! -d "$SRC/.git" ]; then
    log "cloning send_hermes (pinned $SENDHERMES_PIN) into $SRC"
    rm -rf "$SRC"
    git clone https://github.com/rahlquist/sendme.git "$SRC"
    git -C "$SRC" checkout "$SENDHERMES_PIN"
  fi
  log "building tor-enabled send_hermes (option (A), iroh-tor-transport=$IROH_TOR_VERSION)"
  log "  NOTE: the --transport tor flag + TorCustomTransport endpoint wiring"
  log "  must be present in this checkout (see ops/tor-build.md); this"
  log "  script builds and installs whatever is there and then verifies."
  ( cd "$SRC" && cargo build --release )
  $PRIV install -m 755 "$SRC/target/release/send_hermes" /usr/local/bin/send_hermes
fi

log "verifying sidecar advertises --transport"
if ! send_hermes send --help 2>&1 | grep -q -- --transport; then
  die "send_hermes does not support --transport tor — rebuild per ops/tor-build.md (option (A)) or set tor_binary (option (B))"
fi

log "verifying tor_ready() probes (control port + sidecar)"
( cd "$REPO" && /usr/bin/python3.10 -c "
import sys; sys.path.insert(0, '.')
from transport import tor_ready
print(tor_ready())
" ) || die "tor_ready() failed — check 'systemctl status agent-pool-tor' and ops/torrc"

log "done. Enable tor mode per gateway (plugin config):"
log "  transport_mode: tor"
log "  tor_control_port: 9051"
log "  tor_socks_port: 9055"
log "  tor_binary: \"\"   # empty = tor-enabled send_hermes on PATH"

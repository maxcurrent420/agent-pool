#!/usr/bin/env bash
# Install the Agent Pool Tor anonymity layer on ONE gateway.
#
# OPERATOR-EXECUTED. Runs on every gateway in a pool before
# transport_mode=tor is enabled anywhere (both endpoints must run a
# tor-enabled sidecar; a mixed pool fails closed).
#
# What it does:
#   1. apt install tor
#   2. render a dedicated /etc/tor/agent-pool.torrc for the installing
#      gateway user (ControlPort 127.0.0.1:9051, CookieAuthentication 1
#      with a 0600 cookie, SocksPort 127.0.0.1:9055, DataDirectory under
#      that user's plugin-data dir) — the host torrc is never touched
#   3. install + enable ops/systemd/agent-pool-tor.service
#   4. build the tor-enabled send_hermes sidecar (option (A), pinned —
#      see ops/tor-build.md) and install it to /usr/local/bin. This is a
#      third-party fork (github.com/rahlquist/sendme) at a pinned commit,
#      not authored by the plugin maintainers; installed as root by this
#      operator-run script. Upstream n0-computer/sendme has no custom
#      transport, so the pin is required (see the README disclosure).
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
# Privilege note: operator-run with elevated privileges (as root, or via
# sudo). The scanner's caution finding is expected and disclosed in the PR.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO/scripts/lib/tor-config.sh"
SENDHERMES_PIN="c700cbcaedb8f626716b8769dcbc5571ec64d088"   # sendme 0.36.0 fork pin
IROH_TOR_VERSION="0.1.0"                                     # crates.io pin

log() { printf '[install-tor] %s\n' "$*"; }
die() { printf '[install-tor] ERROR: %s\n' "$*" >&2; exit 1; }

# Escalation command written literally (no assembled fragments).
command -v sudo >/dev/null || die "elevated privileges required (run as root or provide sudo)"
[ "$(id -u)" -ne 0 ] && PRIV="sudo" || PRIV=""

if [ -z "${SKIP_APT:-}" ]; then
  log "apt install tor"
  $PRIV apt-get update -qq
  $PRIV apt-get install -y -qq tor
fi

log "rendering dedicated Tor config for $(tor_target_user) (data dir $(tor_data_dir))"
render_tor_config
$PRIV systemctl enable --now agent-pool-tor
log "agent-pool-tor: $($PRIV systemctl is-active agent-pool-tor)"

log "disclosure: building third-party fork github.com/rahlquist/sendme @ $SENDHERMES_PIN (not plugin-authored)"
log "installing as root to /usr/local/bin/send_hermes; upstream n0-computer/sendme has no custom transport"
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
" ) || die "tor_ready() failed — check 'systemctl status agent-pool-tor', the 0600 cookie in the gateway user's plugin data dir, and ops/agent-pool.torrc"

log "done. Enable tor mode per gateway (plugin config):"
log "  transport_mode: tor"
log "  tor_control_port: 9051"
log "  tor_socks_port: 9055"
log "  tor_binary: \"\"   # empty = tor-enabled send_hermes on PATH"

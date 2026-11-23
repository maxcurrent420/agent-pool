#!/usr/bin/env bash
# Agent Pool first-run bootstrap: SYSTEM services only (ClamAV, Tor daemon,
# tor-enabled sidecar). Python dependencies are NOT installed here — they
# are declared in pyproject.toml and installed by Hermes itself
# (`hermes plugins validate --install-deps` / `hermes pm install`), which
# targets the interpreter the gateway actually runs. This script never
# runs a package installer for Python; it only verifies the gateway
# interpreter can import the declared deps and prints the exact Hermes
# remediation commands when it cannot.
#
# Usage:
#   bash scripts/install-deps.sh            # install/verify system services
#   bash scripts/install-deps.sh --check    # report status only, exit 1 if
#                                           # anything is missing (no changes)
#
# Privilege note: this script is operator-run. System package/unit changes
# use the literal privilege-escalation command below; a warning from the
# plugin install scanner is expected and is explained in the catalog PR.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO/scripts/lib/tor-config.sh"
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "[install-deps] unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

log() { printf '[install-deps] %s\n' "$*"; }
# Escalation command written literally (no assembled fragments).
PRIV=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  PRIV="sudo"
fi
FAILURES=0

note_fail() { log "MISSING: $*"; FAILURES=$((FAILURES + 1)); }

# --- 1. Gateway interpreter detection + pip-layer verify -------------------
detect_gateway_python() {
  # The gateway process cmdline contains the hermes agent path and ends
  # with "gateway run". Read its interpreter from /proc so the verify
  # targets the interpreter that actually runs the plugin — never a
  # shell python3.10 or other guessed interpreter.
  local pid=""
  pid="$(pgrep -f '[h]ermes.*gateway run' 2>/dev/null | head -1 || true)"
  if [ -n "$pid" ] && [ -e "/proc/$pid/exe" ]; then
    readlink "/proc/$pid/exe" 2>/dev/null || true
    return 0
  fi
  # Fallback: resolve the hermes launcher's interpreter.
  if command -v hermes >/dev/null 2>&1; then
    head -1 "$(command -v hermes)" 2>/dev/null | sed -n 's/^#!//p' || true
  fi
  return 0
}

verify_pip_layer() {
  local py
  py="$(detect_gateway_python)"
  if [ -z "$py" ] || [ ! -x "$py" ]; then
    note_fail "gateway interpreter not detected (no running hermes gateway?)"
    return 1
  fi
  log "gateway interpreter: $py"
  if "$py" -c 'import nacl, blake3' >/dev/null 2>&1; then
    log "python deps OK in gateway interpreter (nacl, blake3 importable)"
    return 0
  fi
  note_fail "python deps missing in gateway interpreter ($py cannot import nacl/blake3)"
  log "remediation (Hermes installs the declared deps into the gateway env):"
  log "  hermes plugins validate <plugin-path> --install-deps"
  log "  # or, in the gateway environment: hermes pm install"
  log "this script never installs Python packages itself"
  return 1
}

# --- 2. ClamAV --------------------------------------------------------------
clamav_ok() { command -v clamscan >/dev/null 2>&1 && clamscan --version >/dev/null 2>&1; }

ensure_clamav() {
  if clamav_ok; then
    log "clamav OK: $(clamscan --version 2>/dev/null | head -1)"
    return 0
  fi
  if [ "$CHECK" = 1 ]; then note_fail "clamav (clamscan not usable)"; return 1; fi
  log "installing clamav + clamav-daemon (system package manager)"
  if command -v apt-get >/dev/null 2>&1; then
    $PRIV apt-get update -qq && $PRIV apt-get install -y -qq clamav clamav-daemon
  else
    note_fail "no supported system package manager found for clamav"
    return 1
  fi
  # Signature DB refresh; tolerate failure here (daemon may provision it),
  # the verify below is authoritative.
  $PRIV freshclam >/dev/null 2>&1 || log "freshclam refresh deferred (run it manually if scans report no signatures)"
  if clamav_ok; then
    log "clamav OK after install: $(clamscan --version 2>/dev/null | head -1)"
    return 0
  fi
  note_fail "clamav still unusable after install"
  return 1
}

# --- 3. Tor daemon ----------------------------------------------------------
tor_unit_active() {
  command -v systemctl >/dev/null 2>&1 && \
    systemctl is-active --quiet agent-pool-tor 2>/dev/null
}

ensure_tor() {
  if ! command -v tor >/dev/null 2>&1; then
    if [ "$CHECK" = 1 ]; then note_fail "tor daemon binary"; return 1; fi
    log "installing tor (system package manager)"
    if command -v apt-get >/dev/null 2>&1; then
      $PRIV apt-get update -qq && $PRIV apt-get install -y -qq tor
    else
      note_fail "no supported system package manager found for tor"
      return 1
    fi
  fi
  # Refresh the rendered config + unit on every operator run (never writes
  # the host torrc; plugin data is per-profile, not shared).
  if [ "$CHECK" = 0 ]; then
    log "rendering dedicated Tor config for $(tor_target_user) (data dir $(tor_data_dir))"
    render_tor_config
  fi
  if tor_unit_active; then
    log "tor unit OK: agent-pool-tor is active"
    return 0
  fi
  if [ "$CHECK" = 1 ]; then note_fail "agent-pool-tor unit not active"; return 1; fi
  $PRIV systemctl enable --now agent-pool-tor
  if tor_unit_active; then
    log "tor unit OK after enable: agent-pool-tor is active"
    return 0
  fi
  note_fail "agent-pool-tor unit not active after enable"
  return 1
}

# --- 4. Tor-enabled sidecar -------------------------------------------------
sidecar_ok() {
  local bin=""
  if command -v send_hermes >/dev/null 2>&1; then
    bin="$(command -v send_hermes)"
    if "$bin" send --help 2>&1 | grep -q -- '--transport'; then
      log "tor sidecar OK: $bin advertises the tor transport"
      return 0
    fi
  fi
  if command -v send_hermes-tor >/dev/null 2>&1; then
    bin="$(command -v send_hermes-tor)"
    if "$bin" send --help 2>&1 | grep -q -- '--transport'; then
      log "tor sidecar OK: $bin advertises the tor transport"
      return 0
    fi
  fi
  return 1
}

ensure_sidecar() {
  if sidecar_ok; then return 0; fi
  if [ "$CHECK" = 1 ]; then note_fail "tor-enabled send_hermes sidecar"; return 1; fi
  log "building the tor-enabled sidecar via scripts/install-tor.sh"
  bash "$REPO/scripts/install-tor.sh"
  if sidecar_ok; then return 0; fi
  note_fail "no tor-enabled send_hermes sidecar after build"
  return 1
}

# --- Run --------------------------------------------------------------------
log "mode: $([ "$CHECK" = 1 ] && echo check || echo install) (system services only; python deps are Hermes-managed)"
verify_pip_layer || true
ensure_clamav || true
ensure_tor || true
ensure_sidecar || true

if [ "$FAILURES" -ne 0 ]; then
  log "install-deps: $FAILURES component(s) missing — see MISSING lines above"
  exit 1
fi
log "install-deps: all components present"
exit 0

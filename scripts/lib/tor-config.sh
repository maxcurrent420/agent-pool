# Shared Tor config rendering helper for the operator-run install scripts.
# Source this AFTER REPO is set. It renders the agent-pool-specific torrc
# and systemd unit for the installing gateway user; the host torrc is
# never written.

# Resolve the installing gateway user and its plugin-data directory.
tor_target_user() { printf '%s\n' "${SUDO_USER:-$(id -un)}"; }
tor_target_home() {
  local u; u="$(tor_target_user)"
  getent passwd "$u" 2>/dev/null | cut -d: -f6 || printf '%s\n' "$HOME"
}
tor_data_dir() {
  local base="${AGENT_POOL_DATA_DIR:-}"
  if [ -z "$base" ]; then
    base="${HERMES_HOME:-$(tor_target_home)/.hermes}/plugin-data/agent-pool"
  fi
  printf '%s/tor-data\n' "$base"
}
render_tor_config() {
  local data_dir; data_dir="$(tor_data_dir)"
  local user; user="$(tor_target_user)"
  local tmp; tmp="$(mktemp)"
  sed -e "s|@DATA_DIR@|$data_dir|g" "$REPO/ops/agent-pool.torrc" > "$tmp"
  ${PRIV:-} install -d -m 0700 -o "$user" "$data_dir"
  ${PRIV:-} install -m 0600 -o "$user" "$tmp" /etc/tor/agent-pool.torrc
  sed -e "s|@TOR_USER@|$user|g" "$REPO/ops/systemd/agent-pool-tor.service" > "$tmp"
  ${PRIV:-} install -m 0644 "$tmp" /etc/systemd/system/agent-pool-tor.service
  rm -f "$tmp"
  ${PRIV:-} systemctl daemon-reload
}

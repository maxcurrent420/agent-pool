#!/usr/bin/env bash
# Agent Pool demo: full loop end-to-end (see scripts/demo.py).
#   ./scripts/demo.sh          # hermetic (fake transport, no gateway needed)
#   ./scripts/demo.sh --real   # real send_hermes/Iroh transport
set -euo pipefail
cd "$(dirname "$0")/.."
exec env -u PYTHONNOUSERSITE HOME=/home/maxcurrent PATH=/usr/bin:/bin \
  /usr/bin/python3.10 scripts/demo.py "$@"

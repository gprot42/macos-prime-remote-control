#!/usr/bin/env bash
# Launch Prime Remote Control (Tauri desktop app).
#
# Usage:
#   ./launch.sh          # run production binary (builds if missing)
#   ./launch.sh --dev    # Vite + Tauri dev mode (hot reload)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LGTV_FUN_DIR="$SCRIPT_DIR"

if [[ "${1:-}" == "--dev" ]]; then
  echo "Starting in dev mode (npm run tauri dev)..."
  cd "$SCRIPT_DIR"
  exec npm run tauri dev
else
  BINARY="$SCRIPT_DIR/src-tauri/target/release/prime-remote-control"
  if [[ ! -f "$BINARY" ]]; then
    echo "Binary not found. Building..."
    cd "$SCRIPT_DIR"
    npm run tauri build -- --no-bundle
  fi
  echo "Launching Prime Remote Control..."
  exec "$BINARY"
fi

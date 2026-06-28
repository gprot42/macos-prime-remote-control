#!/usr/bin/env bash
# Launch Prime Remote Control (Tauri desktop app).
#
# Usage:
#   ./launch.sh          # open the .app bundle (builds if missing)
#   ./launch.sh --dev    # Vite + Tauri dev mode (hot reload)
#   ./launch.sh --binary # run raw binary (no .app wrapper)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LGTV_FUN_DIR="$SCRIPT_DIR"

APP_NAME="Prime Remote Control"
APP_BUNDLE="$SCRIPT_DIR/src-tauri/target/release/bundle/macos/${APP_NAME}.app"
BINARY="$SCRIPT_DIR/src-tauri/target/release/prime-remote-control"

if [[ "${1:-}" == "--dev" ]]; then
  echo "Starting in dev mode (npm run tauri dev)..."
  cd "$SCRIPT_DIR"
  exec npm run tauri dev
fi

if [[ "${1:-}" == "--binary" ]]; then
  if [[ ! -f "$BINARY" ]]; then
    "$SCRIPT_DIR/build.sh" --binary
  fi
  echo "Launching Prime Remote Control (binary)..."
  exec "$BINARY"
fi

if [[ ! -d "$APP_BUNDLE" ]]; then
  "$SCRIPT_DIR/build.sh"
fi

echo "Launching ${APP_NAME}.app..."
exec open "$APP_BUNDLE"
#!/usr/bin/env bash
# Build Prime Remote Control (Tauri desktop app).
#
# Usage:
#   ./build.sh              # release .app bundle (default)
#   ./build.sh --binary     # release binary only (no .app wrapper)
#   ./build.sh --frontend   # Vite/TypeScript only (no Tauri/Rust)
#   ./build.sh --icons      # regenerate app icons only
#   ./build.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LGTV_FUN_DIR="$SCRIPT_DIR"

APP_NAME="Prime Remote Control"
APP_BUNDLE="$SCRIPT_DIR/src-tauri/target/release/bundle/macos/${APP_NAME}.app"
BINARY="$SCRIPT_DIR/src-tauri/target/release/prime-remote-control"

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

ensure_node_deps() {
  if ! command -v npm &>/dev/null; then
    echo "error: npm is required but not found in PATH" >&2
    exit 1
  fi
  if [[ ! -d "$SCRIPT_DIR/node_modules" ]]; then
    echo "Installing npm dependencies..."
    (cd "$SCRIPT_DIR" && npm install)
  fi
}

build_icons() {
  echo "Rendering app icons..."
  python3 "$SCRIPT_DIR/scripts/render-icons.py"
}

build_frontend() {
  ensure_node_deps
  echo "Building frontend (tsc + vite)..."
  (cd "$SCRIPT_DIR" && npm run build)
}

build_release_bundle() {
  ensure_node_deps
  build_icons
  echo "Building release .app bundle..."
  (cd "$SCRIPT_DIR" && npm run tauri build)
  echo ""
  echo "Done."
  echo "  App bundle: $APP_BUNDLE"
  echo "Launch with: ./launch.sh"
}

build_release_binary() {
  ensure_node_deps
  build_icons
  echo "Building release binary (no bundle)..."
  (cd "$SCRIPT_DIR" && npm run tauri build -- --no-bundle)
  echo ""
  echo "Done."
  echo "  Binary: $BINARY"
  echo "Launch with: ./launch.sh --binary"
}

MODE="bundle"

for arg in "$@"; do
  case "$arg" in
    --help|-h)
      usage
      exit 0
      ;;
    --binary)
      MODE="binary"
      ;;
    --frontend)
      MODE="frontend"
      ;;
    --icons)
      MODE="icons"
      ;;
    *)
      echo "error: unknown option: $arg" >&2
      echo "Run ./build.sh --help for usage." >&2
      exit 2
      ;;
  esac
done

cd "$SCRIPT_DIR"

case "$MODE" in
  bundle)
    build_release_bundle
    ;;
  binary)
    build_release_binary
    ;;
  frontend)
    build_frontend
    echo ""
    echo "Done. Frontend assets: $SCRIPT_DIR/dist/"
    ;;
  icons)
    build_icons
    echo ""
    echo "Done. Icons updated under public/ and src-tauri/icons/"
    ;;
esac
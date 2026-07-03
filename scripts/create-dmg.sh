#!/usr/bin/env bash
# Create a distributable .dmg without AppleScript (works on macOS 26+).
#
# Tauri's bundle_dmg.sh uses Finder AppleScript that fails on macOS 26 because
# the statusbar-visible property was removed from container windows.
#
# Usage:
#   scripts/create-dmg.sh <app-bundle> <output.dmg> [volume-icon.icns]

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <app-bundle> <output.dmg> [volume-icon.icns]" >&2
  exit 2
fi

APP_BUNDLE="$1"
DMG_OUTPUT="$2"
VOLICON="${3:-}"

if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "error: app bundle not found: $APP_BUNDLE" >&2
  exit 1
fi

APP_NAME="$(basename "$APP_BUNDLE" .app)"
DMG_DIR="$(cd "$(dirname "$DMG_OUTPUT")" && pwd)"
DMG_NAME="$(basename "$DMG_OUTPUT")"
DMG_PATH="$DMG_DIR/$DMG_NAME"
TEMP_DMG="$DMG_DIR/rw.$$.${DMG_NAME}"

STAGING="$(mktemp -d)"
cleanup() {
  rm -rf "$STAGING"
  rm -f "$TEMP_DMG"
}
trap cleanup EXIT

echo "Staging DMG contents..." >&2
ditto "$APP_BUNDLE" "$STAGING/$APP_NAME.app"
ln -s /Applications "$STAGING/Applications"

mkdir -p "$DMG_DIR"
rm -f "$DMG_PATH" "$TEMP_DMG"

echo "Creating disk image..." >&2
hdiutil create \
  -srcfolder "$STAGING" \
  -volname "$APP_NAME" \
  -fs HFS+ \
  -format UDRW \
  -ov \
  "$TEMP_DMG" >/dev/null

if [[ -n "$VOLICON" && -f "$VOLICON" ]]; then
  echo "Setting volume icon..." >&2
  MOUNT="/Volumes/$APP_NAME"
  DEV="$(hdiutil attach -readwrite -noverify -noautoopen -nobrowse \
    -mountpoint "$MOUNT" "$TEMP_DMG" | awk '/^\/dev\// {print $1; exit}')"
  if [[ -n "$DEV" && -d "$MOUNT" ]]; then
    cp "$VOLICON" "$MOUNT/.VolumeIcon.icns"
    if command -v SetFile &>/dev/null; then
      SetFile -a C "$MOUNT"
    fi
    hdiutil detach "$DEV" >/dev/null
  else
    echo "warning: could not mount DMG to set volume icon; continuing" >&2
    [[ -n "$DEV" ]] && hdiutil detach "$DEV" >/dev/null 2>&1 || true
  fi
fi

echo "Compressing disk image..." >&2
hdiutil convert "$TEMP_DMG" -format UDZO -imagekey zlib-level=9 -o "$DMG_PATH" >/dev/null

echo "DMG ready: $DMG_PATH" >&2
#!/usr/bin/env bash
#
# fix-tv-connection.sh — diagnose and repair a "TV unreachable" condition from
# this Mac, for the Prime Remote Control app.
#
# Background
# ----------
# The app controls the LG TV over TCP ports 3000/3001 at the TV's IPv4 address.
# A common failure is: the TV is powered ON and visible via mDNS/Bonjour
# (multicast, which the router forwards), yet the Mac cannot exchange a single
# *unicast* packet with it — `ping` fails and ARP for the TV stays "incomplete".
# That is a Wi-Fi layer-2 delivery problem (the TV roamed to a different
# radio/AP, client/AP isolation, a stale neighbor/reject route, or a stuck
# association), NOT an app bug.
#
# What this script does (Mac side only — it cannot change the TV or router):
#   1. Reads the TV IP/MAC from the app config.
#   2. Re-discovers the TV's *current* IP via mDNS (handles DHCP changes) and
#      offers to update the app config if it moved.
#   3. Flushes the stale ARP entry / REJECT host route for the TV (sudo).
#   4. Re-triggers ARP resolution.
#   5. Optionally power-cycles the Mac's Wi-Fi to force a fresh association
#      (--restart-wifi).
#   6. Sends a Wake-on-LAN magic packet to the TV's MAC.
#   7. Re-tests reachability and prints clear next steps for anything that must
#      be fixed on the TV or router.
#
# Usage:
#   scripts/fix-tv-connection.sh [--ip <addr>] [--restart-wifi] [--yes]
#                                [--no-sudo] [-h|--help]
#
set -uo pipefail

# ── Pretty output ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YEL=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YEL=""; CYN=""; RST=""
fi
say()   { printf '%s\n' "$*"; }
info()  { printf '%s•%s %s\n' "$CYN" "$RST" "$*"; }
ok()    { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn()  { printf '%s!%s %s\n' "$YEL" "$RST" "$*"; }
err()   { printf '%s✗%s %s\n' "$RED" "$RST" "$*"; }
step()  { printf '\n%s== %s ==%s\n' "$BOLD" "$*" "$RST"; }

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="$HOME/.config/prime-remote-control.json"

PY="python3"
[[ -x "$ROOT_DIR/.venv/bin/python3" ]] && PY="$ROOT_DIR/.venv/bin/python3"

# ── Options ──────────────────────────────────────────────────────────────────
OVERRIDE_IP=""
RESTART_WIFI=0
ASSUME_YES=0
USE_SUDO=1

usage() {
  sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ip)            OVERRIDE_IP="${2:-}"; shift 2 ;;
    --restart-wifi)  RESTART_WIFI=1; shift ;;
    --yes|-y)        ASSUME_YES=1; shift ;;
    --no-sudo)       USE_SUDO=0; shift ;;
    -h|--help)       usage 0 ;;
    *) err "unknown option: $1"; usage 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  err "This script is for macOS only."
  exit 1
fi

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  local reply
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

run_root() {
  # Run a command as root, via sudo when needed (unless --no-sudo).
  if [[ $EUID -eq 0 ]]; then
    "$@"
  elif [[ $USE_SUDO -eq 1 ]]; then
    sudo "$@"
  else
    warn "skipping (needs root, --no-sudo set): $*"
    return 1
  fi
}

cfg_get() {
  [[ -f "$CONFIG" ]] || { echo ""; return; }
  "$PY" -c 'import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(d.get(sys.argv[2],"") or "")
except Exception:
    print("")' "$CONFIG" "$1" 2>/dev/null
}

cfg_set_ip() {
  "$PY" -c 'import json,sys
p,ip=sys.argv[1],sys.argv[2]
d=json.load(open(p))
d["tv_ip"]=ip
json.dump(d,open(p,"w"),indent=2)
print("updated")' "$CONFIG" "$1" 2>/dev/null
}

# ── Wi-Fi interface ──────────────────────────────────────────────────────────
wifi_iface() {
  # The Device name for the Wi-Fi hardware port (usually en0).
  networksetup -listallhardwareports 2>/dev/null \
    | awk '/Wi-Fi|AirPort/{getline; print $2; exit}'
}

# ── Reachability helpers ─────────────────────────────────────────────────────
ping_ok() { ping -c 1 -t 2 "$1" >/dev/null 2>&1; }

port_open() { nc -z -G 3 "$1" "$2" >/dev/null 2>&1; }

tv_reachable() {
  local ip="$1"
  ping_ok "$ip" || return 1
  port_open "$ip" 3000 || port_open "$ip" 3001 || return 2
  return 0
}

report_reachable() {
  local ip="$1"
  step "Testing reachability of $ip"
  if ping_ok "$ip"; then ok "ping reply received"; else err "no ping reply (TV not answering at layer 2)"; fi
  if port_open "$ip" 3000; then ok "WebOS control port 3000 open"
  elif port_open "$ip" 3001; then ok "WebOS control port 3001 open"
  else warn "port 3000/3001 closed (enable 'LG Connect Apps' on the TV once reachable)"; fi
}

# ── mDNS discovery (find the TV's CURRENT IP, even if DHCP moved it) ──────────
discover_ip() {
  local host="lgwebostv.local" ip=""
  # 1) Directory-service cache (fast, reliable for .local)
  ip="$(dscacheutil -q host -a name "$host" 2>/dev/null \
        | awk -F': ' '/^ip_address:/{print $2; exit}')"
  if [[ -z "$ip" ]]; then
    # 2) ping resolves the name via mDNS; grab the IP it printed even on failure
    ip="$(ping -c 1 -t 1 "$host" 2>/dev/null \
          | sed -n 's/.*(\([0-9.]*\)).*/\1/p' | head -1)"
  fi
  if [[ -z "$ip" ]]; then
    # 3) Browse Bonjour for an LG TV and resolve its advertised hostname.
    local inst
    inst="$(script -q /dev/null bash -c \
      'dns-sd -B _airplay._tcp & p=$!; sleep 3; kill $p 2>/dev/null' 2>/dev/null \
      | tr -d '\r' | sed -n 's/.*_airplay\._tcp\.[[:space:]]*\(\[LG\].*\)$/\1/p' | head -1)"
    if [[ -n "$inst" ]]; then
      local h
      h="$(script -q /dev/null bash -c \
        "dns-sd -L \"$inst\" _airplay._tcp local. & p=\$!; sleep 3; kill \$p 2>/dev/null" 2>/dev/null \
        | tr -d '\r' | sed -n 's/.*can be reached at \([A-Za-z0-9.-]*\.local\)\.\?:.*/\1/p' | head -1)"
      [[ -n "$h" ]] && ip="$(dscacheutil -q host -a name "$h" 2>/dev/null \
        | awk -F': ' '/^ip_address:/{print $2; exit}')"
    fi
  fi
  echo "$ip"
}

# ── Wake-on-LAN (inline, no TV connection attempt) ───────────────────────────
send_wol() {
  local mac="$1"
  "$PY" - "$mac" <<'PY'
import socket, sys, re
mac = re.sub(r'[^0-9A-Fa-f]', '', sys.argv[1])
if len(mac) != 12:
    sys.exit("invalid MAC")
pkt = bytes.fromhex('ff' * 6 + mac * 16)
for port in (9, 7):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.sendto(pkt, ('255.255.255.255', port))
    finally:
        s.close()
print("magic packet sent")
PY
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
say "${BOLD}Prime Remote Control — TV connection repair${RST}"
say "${DIM}Config: $CONFIG${RST}"

CFG_IP="$(cfg_get tv_ip)"
CFG_MAC="$(cfg_get tv_mac)"
IFACE="$(wifi_iface)"; [[ -z "$IFACE" ]] && IFACE="en0"

[[ -n "$OVERRIDE_IP" ]] && CFG_IP="$OVERRIDE_IP"

info "Configured TV IP : ${CFG_IP:-<none>}"
info "Configured TV MAC: ${CFG_MAC:-<none>}"
info "Wi-Fi interface  : $IFACE"

if [[ -z "$CFG_IP" && -z "$OVERRIDE_IP" ]]; then
  warn "No TV IP in config — will rely on mDNS discovery."
fi

# 1) Already fine?
if [[ -n "$CFG_IP" ]] && tv_reachable "$CFG_IP"; then
  report_reachable "$CFG_IP"
  ok "TV is reachable. Nothing to fix."
  exit 0
fi

# 2) Discover current IP via mDNS and reconcile with config
step "Discovering the TV on the network (mDNS/Bonjour)"
DISC_IP="$(discover_ip)"
if [[ -n "$DISC_IP" ]]; then
  ok "TV is advertising on the network at ${BOLD}$DISC_IP${RST}"
  if [[ -n "$CFG_IP" && "$DISC_IP" != "$CFG_IP" ]]; then
    warn "TV's current IP ($DISC_IP) differs from the configured IP ($CFG_IP)."
    if confirm "Update the app config to use $DISC_IP?"; then
      if [[ "$(cfg_set_ip "$DISC_IP")" == "updated" ]]; then
        ok "Config updated: tv_ip = $DISC_IP"
        CFG_IP="$DISC_IP"
      else
        err "Could not update config; continuing with $DISC_IP for this run."
        CFG_IP="$DISC_IP"
      fi
    else
      CFG_IP="$DISC_IP"
    fi
  else
    CFG_IP="${CFG_IP:-$DISC_IP}"
  fi
  # If the (possibly corrected) IP now works, we're done.
  if tv_reachable "$CFG_IP"; then
    report_reachable "$CFG_IP"
    ok "TV is reachable at $CFG_IP."
    exit 0
  fi
  warn "TV is visible via mDNS but not answering unicast at $CFG_IP."
  warn "This is the classic Wi-Fi 'visible but unreachable' state. Repairing…"
else
  warn "Could not find the TV via mDNS. It may be on a different network/band,"
  warn "powered off, or fully isolated. Will still try Mac-side repairs + WoL."
fi

TARGET_IP="${CFG_IP:-}"

# 3) Flush stale ARP entry and REJECT host route
if [[ -n "$TARGET_IP" ]]; then
  step "Clearing stale neighbor / reject route for $TARGET_IP"
  RFLAGS="$(route -n get "$TARGET_IP" 2>/dev/null | awk -F': ' '/flags:/{print $2}')"
  [[ "$RFLAGS" == *REJECT* ]] && warn "kernel has a REJECT route for $TARGET_IP (blackholed after failed ARP)"
  run_root arp -d "$TARGET_IP" 2>/dev/null && ok "flushed ARP entry" || info "no ARP entry to flush"
  run_root route -n delete "$TARGET_IP" >/dev/null 2>&1 && ok "deleted host route" || true
fi

# 4) Optional Wi-Fi power-cycle (forces a fresh association)
if [[ $RESTART_WIFI -eq 1 ]]; then
  step "Power-cycling Wi-Fi ($IFACE) to force re-association"
  warn "Your Mac will briefly drop off Wi-Fi."
  networksetup -setairportpower "$IFACE" off 2>/dev/null && info "Wi-Fi off" || warn "could not turn Wi-Fi off"
  sleep 3
  networksetup -setairportpower "$IFACE" on 2>/dev/null && info "Wi-Fi on" || warn "could not turn Wi-Fi on"
  info "Waiting for the network to come back…"
  for _ in $(seq 1 15); do
    ping_ok "$(route -n get default 2>/dev/null | awk -F': ' '/gateway:/{print $2}')" && break
    sleep 1
  done
  ok "Wi-Fi back up"
fi

# 5) Wake-on-LAN
if [[ -n "$CFG_MAC" ]]; then
  step "Sending Wake-on-LAN to $CFG_MAC"
  send_wol "$CFG_MAC" && ok "WoL sent (wakes the TV if it supports network standby)" \
                       || warn "WoL send failed"
else
  step "Wake-on-LAN"
  warn "No TV MAC in config — skipping WoL. (Power the TV on once via the app so it can learn the MAC.)"
fi

# 6) Re-trigger ARP and re-test
if [[ -n "$TARGET_IP" ]]; then
  step "Re-testing $TARGET_IP"
  for _ in 1 2 3 4 5; do ping -c 1 -t 1 "$TARGET_IP" >/dev/null 2>&1; done
  if tv_reachable "$TARGET_IP"; then
    report_reachable "$TARGET_IP"
    ok "Fixed — the TV is reachable at $TARGET_IP."
    exit 0
  fi
fi

# 7) Still failing → what the user must do on the TV / router
step "Still unreachable — this part must be fixed on the TV or router"
cat <<EOF
The TV is online but your Mac cannot reach it by unicast. macOS-side repairs
can't cross Wi-Fi client isolation or a bad AP association. Try, in order:

  ${BOLD}1.${RST} Reboot the TV's network: on the TV go to
       Settings → Connection/Network → Wi-Fi → disconnect & reconnect
       (or simply reboot the TV). This forces it to re-associate and
       re-announce itself. Fixes it most of the time.

  ${BOLD}2.${RST} Reboot your router / mesh nodes if step 1 doesn't help.

  ${BOLD}3.${RST} Put the TV and Mac on the ${BOLD}same band/AP${RST}. Your Mac is often on
       5 GHz; many TVs sit on 2.4 GHz. Band steering or a mesh repeater can
       block unicast between them while still forwarding mDNS.

  ${BOLD}4.${RST} Disable ${BOLD}client / AP isolation${RST} (a.k.a. "device isolation",
       "guest mode") for the network the TV is on. Make sure the TV is NOT
       on a guest SSID.

  ${BOLD}5.${RST} Wire the TV to the router via ${BOLD}Ethernet${RST} — removes Wi-Fi
       isolation/roaming entirely.

  ${BOLD}6.${RST} On the TV, ensure Settings → General → ${BOLD}LG Connect Apps${RST} is ON
       (needed for the app's control port 3000) and "Mobile TV On" /
       "Quick Start+" is enabled for Wake-on-LAN.

Re-run this script after step 1:  ${CYN}scripts/fix-tv-connection.sh --restart-wifi${RST}
Quick manual check:               ${CYN}ping -c 3 ${TARGET_IP:-<tv-ip>}${RST}
EOF
exit 3

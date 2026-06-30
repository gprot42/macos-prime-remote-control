# Troubleshooting: "TV unreachable"

When playback fails and the app shows **TV unreachable** (or the remote bar
turns red), the Mac cannot talk to the LG TV over the network. This page
explains why it happens, how to diagnose it, and how to fix it — including a
one-shot repair script.

## How the app reaches the TV

The app controls the TV with LG's WebOS protocol over **TCP ports 3000/3001**
at the TV's **IPv4** address (set in Settings). IPv6 is not used. So three
things must all be true:

1. The TV is powered on with networking active.
2. The TV is reachable from the Mac by **unicast** (ping/ARP/TCP), not just
   discoverable.
3. The TV's **LG Connect Apps** (network control) feature is enabled, so port
   3000 is listening.

## The confusing case: "visible but unreachable"

The most common and most confusing failure is when the TV is **on and visible**
but still unreachable:

- It shows up in Bonjour/AirPlay device lists, and your router lists it as
  "connected/live".
- Yet the app — and a plain `ping` — cannot reach it.

This happens because **discovery uses multicast** (mDNS/Bonjour), which the
router forwards across the whole network, while **control uses unicast**, which
can be blocked between Wi-Fi clients. So the TV is *seen* but cannot be *talked
to*.

Tell-tale signs (all observed together):

- `ping 192.168.0.79` → 100% packet loss.
- `arp -n 192.168.0.79` → `(incomplete)` — the Mac asks "who has this IP?" and
  gets no answer.
- IPv6 link-local ping to the TV also fails (rules out the VPN and IPv4-only
  issues — this is pure layer-2).
- `route -n get <tv-ip>` shows a `REJECT` flag (macOS blackholes the route
  after ARP repeatedly fails).
- `nc -z <tv-ip> 3000` → closed, on the TV's IP and everywhere on the subnet.
- mDNS still lists `[LG] webOS TV …` / `lgwebostv.local`.

### Root causes

In order of likelihood for this pattern:

1. **Wi-Fi roaming / band or AP separation.** The Mac is often on 5 GHz while
   the TV sits on 2.4 GHz, or they're on different mesh nodes. Band steering or
   a mesh repeater can forward multicast but not bridge unicast between them.
2. **Client / AP isolation** (a.k.a. "device isolation", "guest mode") on the
   network the TV is attached to. Guest SSIDs isolate clients by default.
3. **Stuck association / stale neighbor entry** after the TV roamed — clears
   with a reconnect on either side.
4. **DHCP moved the TV** to a new IP that no longer matches Settings (separate
   problem: discoverable *and* pingable, just at a different address).
5. **LG Connect Apps disabled** on the TV → reachable but port 3000 closed.

A VPN on the Mac (e.g. a `utun` interface in the `100.64.0.0/10` range) is
rarely the cause if other LAN devices are reachable, but you can disconnect it
once as a quick rule-out.

## Quick diagnosis (manual)

```bash
# Is the TV answering unicast?
ping -c 3 192.168.0.79
arp -n 192.168.0.79                 # "(incomplete)" == not answering at L2

# Is the WebOS control port open?
nc -z -G 3 192.168.0.79 3000 && echo open || echo closed

# Is the TV at all visible / at a new IP?
dscacheutil -q host -a name lgwebostv.local
dns-sd -B _airplay._tcp             # look for "[LG] webOS TV …"

# Does the kernel have a REJECT route (failed ARP)?
route -n get 192.168.0.79 | grep -i flags
```

## One-shot repair script

```bash
scripts/fix-tv-connection.sh                 # diagnose + Mac-side repairs
scripts/fix-tv-connection.sh --restart-wifi  # also power-cycle Wi-Fi
scripts/fix-tv-connection.sh --yes           # auto-confirm config IP update
scripts/fix-tv-connection.sh --ip 192.168.0.50   # override the IP to test
```

What it does (Mac side only — it cannot reconfigure the TV or router):

1. Reads the TV IP/MAC from `~/.config/prime-remote-control.json`.
2. Re-discovers the TV's **current** IP via mDNS and offers to update the app
   config if DHCP moved it.
3. Flushes the stale ARP entry / `REJECT` host route (uses `sudo`).
4. Re-triggers ARP resolution.
5. With `--restart-wifi`, power-cycles the Mac's Wi-Fi to force a fresh
   association.
6. Sends a Wake-on-LAN magic packet to the TV's MAC.
7. Re-tests reachability and, if it still fails, prints exactly what to change
   on the TV or router.

If the script reports the TV is reachable, the app will connect immediately and
the "TV unreachable" indicator turns green.

## Fixes that must happen on the TV or router

macOS-side repairs can't cross Wi-Fi client isolation or a bad AP association.
If the script can't restore reachability, do these in order:

1. **Reboot the TV's network** — on the TV: Settings → Connection/Network →
   Wi-Fi → disconnect & reconnect, or reboot the TV. Forces re-association and
   re-announces ARP. Fixes it most of the time.
2. **Reboot the router / mesh nodes.**
3. **Put the TV and Mac on the same band/AP** (test by joining the Mac to the
   band the TV uses).
4. **Disable client/AP isolation** for the TV's network; make sure the TV is
   not on a guest SSID.
5. **Wire the TV via Ethernet** to the main router — removes Wi-Fi
   isolation/roaming entirely.
6. On the TV, enable **Settings → General → LG Connect Apps** (for port 3000)
   and **Mobile TV On / Quick Start+** (for Wake-on-LAN).

## If the TV simply moved IP

If it's pingable at a new address, just update **Settings → TV IP** in the app
(or let `fix-tv-connection.sh` do it). To avoid recurrence, set a **DHCP
reservation** on your router so the TV keeps a fixed IP.

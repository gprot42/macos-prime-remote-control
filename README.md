# Prime Remote Control

A Mac app for browsing Prime Video and sending what you want to watch straight to your LG TV — with a remote control built in.

## What it does

Browse Prime Video collections on your Mac, search for titles, and play them on your TV with one click. For TV shows, pick the episode you want. While something is playing, use the on-screen remote to pause, skip, adjust volume, and more.

By default, the catalog shows titles included with your Prime subscription. You can turn on channel add-ons and rent/buy titles in Settings if you want them.

## Screenshots

**Browse your Prime catalog**

![Browse Prime Video collections](docs/screengrab1.png)

**Pick a title and send it to your TV**

![Choose an episode and play on your LG TV](docs/screengrab2.png)

## Getting started

1. Open the app settings and enter your LG TV’s IP address.
2. Browse or search for something to watch.
3. Click a title, then hit Play.

To launch the app:

```bash
./launch.sh
```

## Browse categories

The tab bar includes Prime collections and genre pages:

- **Included with Prime**, **New & Upcoming**, **Top Rated**
- **Genres:** Action, Anime, Comedy, Documentary, Drama, Fantasy, Historical, Horror, Romance, Sci-Fi, Suspense

Use the **Movies** / **TV Shows** filters to narrow a row. Top Rated mixes films and series — filter to Movies if you only want films.

List genres from the command line:

```bash
python3 amazon/prime-catalog.py --list-genres
```

## Bookmarks

Save titles from the right-click menu or the bookmark button on a card. Open **Bookmarks** from the top-right header.

Saved titles are grouped into **Movies** and **TV Series** rows. Episode bookmarks appear under TV Series.

## Playback: TV or Mac

By default, titles play on your **LG TV**. You can also play on your **Mac** in a dedicated in-app Prime Video window.

- **Play on TV** — launches the title on your LG TV (requires TV IP and Prime profile in Settings)
- **Play on Mac** — opens Prime Video inside the app; sign in with your Amazon account the first time

Set the default in **Settings → Default playback**. The play dialog always offers both options.

Mac playback uses Prime’s web player (not a custom video player). The TV remote bar only controls LG TV playback. To sign out of Amazon on Mac, use **Settings → Clear Prime login**.

## Right-click menu

Right-click any title card (or an episode in the play dialog) for:

- **Play on TV** / **Play on Mac**
- **Look up on TMDB** — opens a TMDB search in your browser
- **Show trailer on TMDB** — opens the TMDB Videos tab for that title
- **Bookmark** / **Remove bookmark**

## Cache and VPN regions

Catalog data is cached locally (default: 6 hours) so browsing stays fast. Prime Video content depends on your **IP region** (UK, SE, US, etc.).

With **Detect VPN region changes** enabled in Settings (on by default), the app:

- Shows your region in the header (**Region UK**, etc.)
- Stores cache **per region**
- **Clears catalog cache automatically** when the region changes (for example after switching VPN)

Turn that option off to use one shared cache regardless of VPN — useful if you switch regions often and prefer to refresh manually.

If titles look wrong after a VPN change:

1. Check the **Region** badge in the header matches your VPN
2. Click **Refresh** to re-download from Prime Video
3. Or use **Settings → Clear Cache**, then reload

The circular-arrow button reloads from cache; **Refresh** always fetches live data from Prime Video.

## Building

```bash
./build.sh              # release .app bundle (default)
./build.sh --binary     # release binary only
./build.sh --frontend   # Vite/TypeScript only
```

Requires Node.js, Rust, and Python 3.

## Troubleshooting

### "TV unreachable" / playback won't start

If the app shows **TV unreachable** (the remote bar turns red) or a play fails
to connect, the Mac can't reach the LG TV over the network. A common, confusing
case is when the TV is **on and visible** (it shows up in Bonjour/your router)
but still unreachable — because discovery uses multicast while control uses
unicast, which Wi-Fi client isolation or band/AP separation can block.

Run the repair script:

```bash
scripts/fix-tv-connection.sh                 # diagnose + Mac-side repairs
scripts/fix-tv-connection.sh --restart-wifi  # also power-cycle Wi-Fi
```

It re-discovers the TV's current IP via mDNS (and offers to update the config if
DHCP moved it), clears the stale ARP/reject route, sends Wake-on-LAN, and
re-tests — then tells you exactly what to change on the TV or router if needed.

See [docs/troubleshooting-tv-connection.md](docs/troubleshooting-tv-connection.md)
for the full diagnosis, manual commands, and fixes.
#!/usr/bin/env python3
"""
lg-tv-connect.py — Connect to an LG WebOS TV via aiowebostv.

Usage:
  python lg-tv-connect.py [IP]           # pair & save key, then show system info
  python lg-tv-connect.py [IP] --info    # show system info (already paired)
  python lg-tv-connect.py [IP] --apps    # list installed apps
  python lg-tv-connect.py [IP] --launch youtube.leanback.v4
  python lg-tv-connect.py [IP] --launch amazon --profile 1
  python lg-tv-connect.py [IP] --launch amazon --content-id B09L5V3KJY --profile 0
  python lg-tv-connect.py [IP] --launch amazon --content-id 0P3ONZ4IHQ75ZC4ZMIZ9D4NE7Q --profile 0

Find content IDs: python amazon/prime-catalog.py --search "dune"

On first run the TV will display a pairing prompt — accept it.
The client key is saved to ~/.lg-tv-key and reused on subsequent connections.
"""

import argparse
import asyncio
import json
import os
import platform
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

_VENV_PYTHON = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python3"


def _bootstrap_venv() -> None:
    """Re-exec with project .venv when aiowebostv is not on the current interpreter."""
    if os.environ.get("LG_TV_VENV_REEXEC") == "1":
        return
    try:
        import aiowebostv  # noqa: F401
    except ImportError:
        if _VENV_PYTHON.exists():
            os.environ["LG_TV_VENV_REEXEC"] = "1"
            os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), *sys.argv])
        raise


_bootstrap_venv()

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from amazon.prime_profiles import (
    format_profiles_table,
    list_profiles,
    resolve_profile_name,
    upsert_profile,
)
from amazon.prime_entitlement import (
    format_entitlement,
    has_watchable_play_button,
    list_episodes_from_html,
    lookup_entitlement,
    parse_entitlement,
    playback_labels_from_html,
    playback_launch_target_from_html,
    resolve_asins_for_content_id,
    resolve_episode_content_id,
    resolve_gti_for_content_id,
    resume_start_seconds_from_html,
    title_type_for_content_id,
)

if TYPE_CHECKING:
    from aiowebostv import WebOsClient

KEY_FILE = Path.home() / ".lg-tv-key"
# App-private cache (not ~/.cache/prime-catalog-ui used by lgtv-fun).
APP_CACHE_DIR = Path.home() / ".cache" / "prime-remote-control"
# Cross-process SSAP lock so this app and our CLI do not share the TV socket
# with a second copy of the same stack.
TV_SSAP_LOCK_PATH = APP_CACHE_DIR / "tv.ssap.lock"
DEFAULT_IP = "192.168.0.79"
PRIME_VIDEO_APP_ID = "amazon"
PRIME_BROWSER_APP_ID = "com.webos.app.browser"
AMAZOFF_APP_ID = "com.amazoff.patcher"
# Cold start: webOS home then Prime. We wait until Prime is foreground
# plus a short extra (not a fixed 14s) so a warm start is not stuck on the picker.
DEFAULT_PROFILE_DELAY = 6.0
DEFAULT_CONTENT_DELAY = 4.0
DEFAULT_PLAY_DELAY = 5.0
DEFAULT_PLAY_FOCUS_UP = 5
DEFAULT_PLAY_FOCUS_DOWN = 2
DEFAULT_PLAY_FOCUS_LEFT = 2
MIN_PROFILE_DELAY = 3.0
PROFILE_KEY_DELAY = 0.35
DEFAULT_PIN_DELAY = 2.0
DEFAULT_PROFILE_STEP_DELAY = 3.0
# After profile ENTER, wait for the title hub (not Home) before Watch/Resume.
DEFAULT_POST_PROFILE_HUB_DELAY = 4.0
# Last-used is pre-focused. Home toward the first avatar. This list is
# 3 profiles + a trailing "+ / New" tile — 5 UPs can wrap onto New and
# ENTER leaves you stuck on the picker. 2 UPs reach the top from D/Kids.
PROFILE_HOME_STEPS = 2
PLAY_KEY_DELAY = 0.45
SUBTITLE_KEY_DELAY = 0.4
DEFAULT_SUBTITLE_DELAY = 0.0
# DOWNs to surface/focus the transport controls row (the three options row).
# 0 = try without extra downs (bar may appear on first action).
DEFAULT_SUBTITLE_FOCUS_DOWN = 1
# LEFT presses *after RIGHT-home to Audio* to reach Subtitles CC.
# Transport bar is left→right (device photos):
#   Start again → [Next episode] → Subtitles CC → Audio
# Subtitles is always immediately left of Audio, with or without Next.
# We deliberately home RIGHT (to Audio) then LEFT — never LEFT-home to
# "Start again" then ENTER, which restarts the title at 00:00.
DEFAULT_SUBTITLE_FOCUS_RIGHT = 1
# When the picker that opens is a *combined* Audio+Subtitles panel, these move
# focus from the Audio side/column/tab over to Subtitles. 0 = skip (default —
# device opens a dedicated Subtitles CC panel).
DEFAULT_SUBTITLE_SECTION_UP = 0
DEFAULT_SUBTITLE_SECTION_LEFT = 0
# Device video docs/caption-select-0.mp4 + photos 0–2.jpg:
# Opening Subtitles CC shows a collapsed pill (Off / language) with
# "Press Select … to see language and style options."
# Select expands a *horizontal* panel (not a vertical language list):
#   Subtitles (Off|On) | Languages (English [CC]|…) | Sizes | Styles
# The Subtitles column is only Off (0) / On (1). Language is a separate column.
# menu-down steps in the Subtitles column: 0=Off, 1=On for any language code.
# Override with --subtitle-menu-down if needed.
SUBTITLE_LANGUAGE_DOWN: dict[str, int] = {
    "off": 0,
    "en": 1,
    "en-us": 1,
    "en-gb": 1,
    "english": 1,
    "en-cc": 1,  # On + language is chosen in the Languages column
    "de": 1,
    "fr": 1,
    "es": 1,
    "it": 1,
    "pt": 1,
    "nl": 1,
    "sv": 1,
    "no": 1,
    "da": 1,
    "fi": 1,
    "pl": 1,
    "ja": 1,
    "ko": 1,
}
PRIME_PLAY_METHODS = ("auto", "media", "watch", "enter")
PRIME_PROFILE_TYPES = ("adult", "kid", "none")
PRIME_DETAIL_ID_RE = re.compile(r"^[0-9A-Z]{26,32}$")
PRIME_ASIN_RE = re.compile(r"^B[A-Z0-9]{8,10}$")
PRIME_GTI_RE = re.compile(r"^amzn1\.dv\.gti\.[0-9a-f-]+$", re.I)
PRIME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# For public scraping / catalog pages use www; for deep-link contentTarget to the
# native Prime app on webOS/TV, app.primevideo.com is the domain that works for
# client deep links (per community reports and Amazon deep link notes).
PRIME_WWW_BASE = "https://www.primevideo.com"
PRIME_DEEP_LINK_BASE = "https://app.primevideo.com"


def load_key() -> str | None:
    if KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
        return key if key else None
    return None


def save_key(key: str) -> None:
    KEY_FILE.write_text(key + "\n")
    KEY_FILE.chmod(0o600)
    print(f"  client key saved to {KEY_FILE}")


DEFAULT_CONNECT_TIMEOUT = float(os.environ.get("LG_TV_CONNECT_TIMEOUT", "15"))


_tv_ssap_lock_fh: object | None = None


def acquire_tv_ssap_lock() -> None:
    """Exclusive flock for the WebOS client socket (one process at a time)."""
    global _tv_ssap_lock_fh
    if _tv_ssap_lock_fh is not None:
        return
    import fcntl

    TV_SSAP_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(TV_SSAP_LOCK_PATH, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    _tv_ssap_lock_fh = fh


def release_tv_ssap_lock() -> None:
    global _tv_ssap_lock_fh
    fh = _tv_ssap_lock_fh
    _tv_ssap_lock_fh = None
    if fh is None:
        return
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


async def _safe_disconnect(client: "WebOsClient | None") -> None:
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception:
        pass


def _format_connect_error(exc: BaseException) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _print_connect_troubleshooting(ip: str) -> None:
    print("  Check:", file=sys.stderr)
    print("    • TV is on (not deep standby) and on the same Wi‑Fi", file=sys.stderr)
    print("    • Settings → General → LG Connect Apps → On", file=sys.stderr)
    print("    • For remote power-on: enable Mobile TV On / Wake on LAN on the TV", file=sys.stderr)
    print("    • If fully off, set the TV MAC in app Settings for Wake-on-LAN", file=sys.stderr)
    print(f"    • IP is correct (try: ./lg-tv-probe  or  LG_TV_IP=<ip> ./play)", file=sys.stderr)
    print(
        f"    • Test: python amazon/lg-tv-connect.py {ip} --info",
        file=sys.stderr,
    )


async def connect(ip: str) -> "WebOsClient":
    """Connect and pair with TV, returning a connected client.

    Transient failures (timeouts, momentary refusals, a TV still waking from
    standby) are retried a few times with a short backoff, since a single
    immediate attempt otherwise surfaces a spurious "Could not connect" error
    even though the TV becomes reachable a second later. A pairing rejection is
    NOT retried — it needs the user to accept the prompt.
    """
    from aiowebostv import WebOsClient, WebOsTvPairError

    key = load_key()
    attempts = max(1, int(os.environ.get("LG_TV_CONNECT_ATTEMPTS", "3")))
    backoff = float(os.environ.get("LG_TV_CONNECT_BACKOFF", "1.5"))

    acquire_tv_ssap_lock()
    print(f"Connecting to {ip}:3000 ...")
    if not key:
        print("  No saved key — TV will show a pairing prompt. Accept it now.")

    for attempt in range(1, attempts + 1):
        client = WebOsClient(ip, client_key=key)
        try:
            await asyncio.wait_for(client.connect(), timeout=DEFAULT_CONNECT_TIMEOUT)
        except WebOsTvPairError:
            await _safe_disconnect(client)
            print("  ERROR: Pairing rejected. Accept the prompt on the TV and retry.")
            sys.exit(1)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            await _safe_disconnect(client)
            if attempt < attempts:
                print(
                    f"  Connect attempt {attempt}/{attempts} timed out; retrying ...",
                    file=sys.stderr,
                )
                await asyncio.sleep(backoff)
                continue
            print(
                f"  ERROR: Timed out connecting to {ip}:3000 "
                f"(>{DEFAULT_CONNECT_TIMEOUT:g}s, {attempts} attempts)",
                file=sys.stderr,
            )
            _print_connect_troubleshooting(ip)
            sys.exit(1)
        except Exception as exc:
            await _safe_disconnect(client)
            if attempt < attempts:
                print(
                    f"  Connect attempt {attempt}/{attempts} failed "
                    f"({_format_connect_error(exc)}); retrying ...",
                    file=sys.stderr,
                )
                await asyncio.sleep(backoff)
                continue
            print(
                f"  ERROR: Could not reach TV at {ip}:3000 — {_format_connect_error(exc)}",
                file=sys.stderr,
            )
            _print_connect_troubleshooting(ip)
            sys.exit(1)

        if client.client_key and client.client_key != key:
            save_key(client.client_key)

        print(f"  Connected. paired={client.is_registered()}")
        return client

    # Unreachable: every failure path above either returns or exits.
    raise RuntimeError("connect() exhausted retries without resolving")


async def cmd_info(client: "WebOsClient") -> None:
    info = client.tv_info
    print("\n--- TV Info ---")
    for attr in ("model_name", "serial_number", "program_mode"):
        val = getattr(info, attr, None)
        if val:
            print(f"  {attr}: {val}")

    state = client.tv_state
    print("\n--- TV State ---")
    for attr in (
        "software_info",
        "sound_output",
        "current_app_id",
        "muted",
        "volume",
        "current_channel",
    ):
        val = getattr(state, attr, None)
        if val is not None:
            print(f"  {attr}: {val}")


async def cmd_apps(client: "WebOsClient") -> None:
    apps = client.tv_state.apps
    if not apps:
        print("No apps found (state may not have loaded yet).")
        return
    print(f"\n--- Installed Apps ({len(apps)}) ---")
    for app_id, val in sorted(apps.items(), key=lambda x: (x[1] if isinstance(x[1], str) else x[1].get("title", x[0])).lower()):
        title = val if isinstance(val, str) else val.get("title", app_id)
        print(f"  {title:<30} {app_id}")


def amazoff_match_from_apps(apps: object) -> dict[str, str] | None:
    """Return {app_id, title, version} if AmazOff is in a listApps / launchPoints payload."""
    entries: list[object] = []
    if isinstance(apps, dict):
        nested = apps.get("apps")
        if isinstance(nested, list):
            entries = list(nested)
        else:
            nested = apps.get("launchPoints")
            if isinstance(nested, list):
                entries = list(nested)
            else:
                for key, val in apps.items():
                    if isinstance(val, dict):
                        merged = dict(val)
                        merged.setdefault("id", val.get("id") or key)
                        entries.append(merged)
                    else:
                        entries.append({"id": str(key), "title": str(val)})
    elif isinstance(apps, list):
        entries = list(apps)

    for app in entries:
        if not isinstance(app, dict):
            continue
        aid = str(app.get("id") or app.get("appId") or "").strip()
        title = str(app.get("title") or app.get("name") or "").strip()
        blob = f"{aid} {title}".lower()
        if aid.lower() == AMAZOFF_APP_ID or "amazoff" in blob:
            version = str(app.get("version") or "").strip()
            out = {
                "app_id": aid or AMAZOFF_APP_ID,
                "title": title or "AmazOff",
            }
            if version:
                out["version"] = version
            return out
    return None


async def cmd_detect_amazoff(client: "WebOsClient") -> None:
    """Print JSON: whether AmazOff (com.amazoff.patcher) is installed on the TV."""
    match: dict[str, str] | None = None
    try:
        match = amazoff_match_from_apps(await client.get_apps_all())
    except Exception as exc:
        print(f"  listApps failed ({exc})", file=sys.stderr)
    if match is None:
        try:
            match = amazoff_match_from_apps(await client.get_apps())
        except Exception as exc:
            print(f"  listLaunchPoints failed ({exc})", file=sys.stderr)
    if match is None:
        match = amazoff_match_from_apps(getattr(client.tv_state, "apps", None))
    out: dict[str, object] = {"detected": match is not None}
    if match:
        out.update(match)
    print(json.dumps(out))


def _prime_detail_url(content_id: str) -> str:
    return f"{PRIME_WWW_BASE}/detail/{content_id}"


def _fetch_prime_html(content_id: str) -> str:
    url = _prime_detail_url(content_id)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": PRIME_USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def report_prime_entitlement(content_id: str, *, html: str | None = None) -> None:
    """Print rent/buy/Prime inclusion for a detail page content ID."""
    content_id = content_id.strip()
    if not PRIME_DETAIL_ID_RE.match(content_id):
        return
    try:
        if html is None:
            ent = lookup_entitlement(content_id)
        else:
            ent = parse_entitlement(html, content_id=content_id)
        print(format_entitlement(ent))
        if ent.entitlement_type == "Unentitled":
            if ent.prime_catalog:
                print(
                    "  Warning: title is in the Prime catalog but this check is not "
                    "signed in — the TV will show 'unavailable' without an active Prime "
                    "subscription on the selected profile.",
                    file=sys.stderr,
                )
            elif not ent.included_with_channel:
                print(
                    "  Warning: playback may stop or show 'unavailable' unless rented, "
                    "purchased, or covered by a channel subscription.",
                    file=sys.stderr,
                )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"  Warning: could not check Prime availability ({exc})", file=sys.stderr)


def _append_launch_id(candidates: list[str], seen: set[str], value: str | None) -> None:
    if not value or value in seen:
        return
    seen.add(value)
    candidates.append(value)


def _is_autoplay_target(target: str) -> bool:
    """True when `target` is a deep link that itself starts playback (autoplay=1).

    A bare GTI/ASIN/contentId only opens the title page, so playback there still
    needs a Watch/Resume keypress.
    """
    return isinstance(target, str) and "autoplay=" in target


def _prime_target_uses_params(target: str) -> bool:
    return (
        target.startswith("/detail/")
        or "primevideo.com/detail/" in target
        or target.startswith("primevideo://")
        or "autoplay=" in target
    )


def bare_prime_launch_ids(
    content_id: str,
    *,
    html: str | None = None,
    episode: int | None = None,
    prefer_episode: bool = False,
) -> list[str]:
    """GTI / ASIN / detail IDs only — no HTTPS contentTarget URLs.

    AmazOff ignores ``contentTarget`` HTTPS /autoplay links (Home carousel only).
    A second launch after the profile gate re-opens the profile picker.
    """
    ids: list[str] = []
    seen: set[str] = set()
    raw = (content_id or "").strip()
    try:
        resolved = resolve_prime_launch_ids(
            raw,
            html=html,
            episode=episode,
            prefer_episode=prefer_episode,
            autoplay=False,
            start=0,
        )
        for lid in resolved:
            if lid and not _prime_target_uses_params(lid):
                _append_launch_id(ids, seen, lid)
    except ValueError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  Warning: could not resolve bare launch IDs ({exc})", file=sys.stderr)
    if raw and not _prime_target_uses_params(raw):
        _append_launch_id(ids, seen, raw)
    return ids or ([raw] if raw else [])


def storefront_launch_id(
    content_id: str,
    *,
    html: str | None,
    episode: int | None,
    play: bool,
) -> str:
    """Catalog detail id the signed-in TV profile can entitle.

    ``amzn1.dv.gti.*`` as SSAP contentId often opens the title shell with
    "this video is currently unavailable". Season hubs are ignored (Home).
    Movies keep the card id. Series use the chosen episode's 0… detail id.
    """
    raw = (content_id or "").strip()
    ep_num = episode if episode is not None and episode >= 1 else None
    if html:
        try:
            resolved = resolve_episode_content_id(html, raw, episode=ep_num)
        except Exception as exc:
            print(f"  Warning: episode id resolve failed ({exc})", file=sys.stderr)
            resolved = None
        if (
            resolved
            and resolved != raw
            and PRIME_DETAIL_ID_RE.match(resolved)
        ):
            print(
                f"  Storefront launch: episode detail {resolved} "
                f"(not season {raw}, not GTI)",
                file=sys.stderr,
            )
            return resolved
    if PRIME_DETAIL_ID_RE.match(raw):
        print(f"  Storefront launch: catalog detail {raw}", file=sys.stderr)
        return raw
    ids = bare_prime_launch_ids(
        raw, html=html, episode=ep_num, prefer_episode=bool(play) or ep_num is not None
    )
    for lid in ids:
        if lid and PRIME_DETAIL_ID_RE.match(lid):
            print(f"  Storefront launch: first detail in candidates {lid}", file=sys.stderr)
            return lid
    fallback = (ids[0] if ids else raw)
    print(f"  Storefront launch: fallback {fallback}", file=sys.stderr)
    return fallback


def amazoff_play_launch_id(
    content_id: str,
    *,
    html: str | None,
    episode: int | None,
    play: bool,
) -> str:
    """contentTarget string AmazOff/Prime ignition actually opens.

    AmazOff (github.com/azoffshowy/AmazOff) does **not** rewrite launches.
    It only hijacks ``cloudfront.xp-assets.aiv-cdn.net`` to serve a patched
    ``ATVUnfPlayerBundle.js`` (strip ads). ``appinfo.json`` maps
    ``contentId`` → ``contentTarget: $CONTENTID`` unchanged.

    On this TV that means:
      • season ``0…`` id → ignored (Home / Reacher)
      • ``/detail/0…`` or bare ``0…`` → title shell / "currently unavailable"
      • episode ``amzn1.dv.gti.*`` as contentId → the chosen episode page
    """
    raw = (content_id or "").strip()
    ep_num = episode if episode is not None and episode >= 1 else None
    ids = bare_prime_launch_ids(
        raw,
        html=html,
        episode=ep_num,
        prefer_episode=bool(play) or ep_num is not None,
    )
    for lid in ids:
        if lid and PRIME_GTI_RE.match(lid):
            print(f"  AmazOff launch: GTI {lid}", file=sys.stderr)
            return lid
    print(
        f"  AmazOff launch: no GTI in {ids[:4]!r}; falling back to catalog id",
        file=sys.stderr,
    )
    return storefront_launch_id(raw, html=html, episode=episode, play=play)


def none_picker_real_count() -> int:
    """How many real avatars sit above the trailing + / New tile."""
    try:
        n = max(
            (entry.index for ptype, entry in list_profiles() if ptype == "none"),
            default=-1,
        )
        if n >= 0:
            return n + 1
    except Exception:
        pass
    return 3


def _last_profile_focus_path() -> Path:
    return APP_CACHE_DIR / "last-prime-profile.txt"


def clear_last_profile_focus() -> None:
    """Drop the guessed last-avatar file."""
    path = _last_profile_focus_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read_last_profile_focus() -> int | None:
    path = _last_profile_focus_path()
    try:
        n = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return n if n >= 0 else None


def write_last_profile_focus(index: int) -> None:
    path = _last_profile_focus_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{index}\n")
    except OSError:
        pass


def profile_nav_keys(
    profile: int,
    *,
    row: int = 0,
    profile_type: str = "none",
    list_size: int | None = None,
    last_focused: int | None = None,
) -> list[str]:
    """D-pad keys to a real profile. Never move onto + / New.

    Highlighting New opens Adult/Kids. After a failed run last-used is New.
    From New, one UP is D. From D, do not DOWN (that is New).
    """
    if profile < 0:
        raise ValueError("profile must be >= 0")
    if row < 0:
        raise ValueError("profile-row must be >= 0")
    if profile_type not in PRIME_PROFILE_TYPES:
        raise ValueError(
            f"profile-type must be one of {PRIME_PROFILE_TYPES}, not {profile_type!r}"
        )
    if profile_type != "none":
        keys: list[str] = []
        keys.extend(["LEFT"] * PROFILE_HOME_STEPS)
        keys.extend(["DOWN"] * row)
        keys.extend(["RIGHT"] * profile)
        return keys

    size = list_size if list_size is not None else none_picker_real_count()
    size = max(size, profile + 1, 1)
    last_real = size - 1
    new_index = size
    target = min(profile, last_real)
    start = new_index if last_focused is None else last_focused
    if start > last_real:
        start = new_index

    keys: list[str] = []
    if start == target:
        pass
    elif start == new_index:
        keys.extend(["UP"] * (new_index - target))
    elif start < target:
        keys.extend(["DOWN"] * (target - start))
    else:
        keys.extend(["UP"] * (start - target))
    return keys


def _prime_target_with_start_offset(target: str, pos: int) -> str:
    """Return a Prime autoplay deep link that starts playback at `pos` seconds.

    Prime Video on webOS honours the ``?t=<seconds>`` query param of the
    contentTarget deep link. Use the app.primevideo.com domain for native app
    deep links (more reliable per deep-link reports). Bare startTime param is
    ignored by Prime.
    """
    if not _prime_target_uses_params(target):
        # Bare content / detail ID → build using the deep link domain.
        return f"{PRIME_DEEP_LINK_BASE}/detail/{target}?autoplay=1&t={pos}"
    # Already a deep link: set/replace t= and ensure autoplay is requested.
    if re.search(r"[?&]t=\d+", target):
        target = re.sub(r"([?&]t=)\d+", rf"\g<1>{pos}", target)
    else:
        target = f"{target}{'&' if '?' in target else '?'}t={pos}"
    if "autoplay=" not in target:
        target = f"{target}&autoplay=1"
    # Normalize relative /detail/ targets to full deep-link URL for contentTarget.
    if target.startswith("/detail/"):
        target = PRIME_DEEP_LINK_BASE + target
    return target


def resolve_prime_launch_ids(
    content_id: str,
    *,
    html: str | None = None,
    episode: int | None = None,
    prefer_episode: bool = False,
    autoplay: bool = False,
    start: int = 0,
) -> list[str]:
    """Return Prime content IDs to try, best-first (GTI is what the TV app often expects)."""
    content_id = content_id.strip()
    print(f"[LAUNCH-RESOLVE] ENTER content_id={content_id} episode={episode} autoplay={autoplay} start={start} prefer_episode={prefer_episode}", file=sys.stderr)
    if PRIME_GTI_RE.match(content_id) or PRIME_ASIN_RE.match(content_id):
        print(f"[LAUNCH-RESOLVE] fast-path GTI/ASIN: {content_id}", file=sys.stderr)
        return [content_id]

    candidates: list[str] = []
    seen: set[str] = set()
    if not PRIME_DETAIL_ID_RE.match(content_id):
        print(f"[LAUNCH-RESOLVE] not a detail id, returning raw: {content_id}", file=sys.stderr)
        return [content_id]

    page_html = html
    try:
        if page_html is None:
            page_html = _fetch_prime_html(content_id)

        title_type = title_type_for_content_id(page_html, content_id)
        episodes = list_episodes_from_html(page_html, season_content_id=content_id)
        # When the requested content_id is itself an episode page, launch it
        # directly. The page also lists its sibling episodes, so re-deriving an
        # episode here (with no explicit number) would wrongly default to E1.
        use_episode = (
            episode is not None
            or (prefer_episode and title_type != "episode")
            or title_type in {"season", "series"}
        )
        selected_episode = None
        if use_episode and episodes:
            if episode is not None:
                if episode < 1 or episode > len(episodes):
                    raise ValueError(
                        f"episode {episode} out of range; this title has {len(episodes)} episode(s)"
                    )
                selected_episode = episodes[episode - 1]
            else:
                selected_episode = episodes[0]
            if selected_episode and title_type == "season":
                label = selected_episode.get("title") or "episode"
                seq = selected_episode.get("sequence_number")
                seq_note = f" {seq}" if seq else ""
                print(
                    f"  Season deep links are unreliable on TV — using episode{seq_note} "
                    f"({label}) for launch."
                )

        launch_targets: list[tuple[str, str | None]] = []
        if selected_episode:
            launch_targets.append(
                ("episode", selected_episode.get("content_id"))
            )
        launch_targets.append(("requested", content_id))
        print(f"[LAUNCH-RESOLVE] initial launch_targets order: {launch_targets}", file=sys.stderr)

        if not (start and start > 0):
            if episode is None:
                launch_targets = list(reversed(launch_targets))
                print(f"[LAUNCH-RESOLVE] reversed to series-first for resume (no specific ep): {launch_targets}", file=sys.stderr)
            else:
                print(f"[LAUNCH-RESOLVE] keeping episode-first for specific ep resume: {launch_targets}", file=sys.stderr)

        if autoplay:
            print(f"[LAUNCH-RESOLVE] building autoplay targets (autoplay=True)", file=sys.stderr)
            for _, target_id in launch_targets:
                if not isinstance(target_id, str):
                    continue
                label_html = page_html
                if target_id != content_id:
                    try:
                        episode_html = _fetch_prime_html(target_id)
                    except (urllib.error.URLError, TimeoutError, OSError):
                        episode_html = None
                    if episode_html:
                        label_html = episode_html
                autoplay_target = playback_launch_target_from_html(
                    label_html, target_id
                )
                if start and start > 0:
                    # Begin playback at the chosen position via the same
                    # ?t=<seconds> deep link the seek path uses. Force a
                    # /detail/<id>?autoplay=1&t=<pos> link even when the unsigned
                    # page exposes no Watch-now playbackURL, so positioning still
                    # works (mirrors cmd_seek).
                    autoplay_target = _prime_target_with_start_offset(
                        autoplay_target or target_id, int(start)
                    )
                # Do NOT synthesize bare ?autoplay=1 HTTPS links when the
                # unsigned page has no Watch button. On many webOS/AmazOff
                # builds those URLs open the app but never start media;
                # bare GTI contentId launches do. Prefer GTI (appended below).
                if autoplay_target:
                    _append_launch_id(candidates, seen, autoplay_target)
                    print(f"[LAUNCH-RESOLVE] chose autoplay_target as first: {autoplay_target}", file=sys.stderr)
                    break

        for _, target_id in launch_targets:
            if not isinstance(target_id, str):
                continue
            _append_launch_id(
                candidates, seen, resolve_gti_for_content_id(page_html, target_id)
            )
            for asin in resolve_asins_for_content_id(page_html, target_id):
                _append_launch_id(candidates, seen, asin)
            _append_launch_id(candidates, seen, target_id)

        print(f"[LAUNCH-RESOLVE] final candidates: {candidates}", file=sys.stderr)
    except ValueError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  Warning: could not resolve GTI/ASIN ({exc}); using detail ID only.", file=sys.stderr)
        _append_launch_id(candidates, seen, content_id)

    if not candidates:
        candidates.append(content_id)
    return candidates


def fetch_prime_detail_html(content_id: str | None) -> str | None:
    if not content_id or not PRIME_DETAIL_ID_RE.match(content_id.strip()):
        return None
    try:
        return _fetch_prime_html(content_id.strip())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  Warning: could not fetch Prime detail page ({exc})", file=sys.stderr)
        return None


async def close_app(client: "WebOsClient", app_id: str) -> bool:
    """Try to close an app. Returns True when the TV accepted the close."""
    from aiowebostv.exceptions import WebOsTvResponseTypeError

    print(f"  Closing {app_id} ...")
    try:
        result = await client.close_app(app_id)
    except WebOsTvResponseTypeError as exc:
        payload = exc.args[0] if exc.args else {}
        err = payload.get("error", exc) if isinstance(payload, dict) else exc
        print(f"  Close skipped ({err}); continuing ...", file=sys.stderr)
        return False
    if not result.get("returnValue", True):
        print(f"  Close failed (continuing): {json.dumps(result)}")
        return False
    return True


async def launch_app(
    client: "WebOsClient",
    app_id: str,
    *,
    content_id: str | None = None,
) -> dict:
    if content_id:
        # Prime appinfo deeplinkingParams map launch contentId -> contentTarget.
        print(f"Launching {app_id} (contentId={content_id}) ...")
        print(f"[LUNA-EQUIV] luna-send -n 1 -f luna://com.webos.applicationManager/launch '{{ \"id\": \"{app_id}\", \"contentId\": \"{content_id}\" }}'", file=sys.stderr)
        return await client.launch_app_with_content_id(app_id, content_id)
    print(f"Launching {app_id} ...")
    print(f"[LUNA-EQUIV] luna-send -n 1 -f luna://com.webos.applicationManager/launch '{{ \"id\": \"{app_id}\" }}'", file=sys.stderr)
    return await client.launch_app(app_id)


async def launch_prime_browser(
    client: "WebOsClient",
    content_id: str,
) -> dict:
    url = _prime_detail_url(content_id)
    print(f"Opening Prime detail page in browser ({url}) ...")
    return await client.launch_app_with_params(
        PRIME_BROWSER_APP_ID,
        {"target": url},
    )


async def launch_prime_content(
    client: "WebOsClient",
    content_id: str,
    *,
    cold_start: bool = True,
) -> dict:
    """Cold-start Prime with a content deep link (relaunch drops the link)."""
    print(f"[LAUNCH] launch_prime_content content_id={content_id} cold_start={cold_start} uses_params={_prime_target_uses_params(content_id)}", file=sys.stderr)
    if cold_start:
        if await close_app(client, PRIME_VIDEO_APP_ID):
            await asyncio.sleep(1.5)
    if _prime_target_uses_params(content_id):
        print(f"[LAUNCH] -> launch_app_with_params amazon contentTarget={content_id}", file=sys.stderr)
        print(f"[LUNA-EQUIV] luna-send -n 1 -f luna://com.webos.applicationManager/launch '{{ \"id\": \"{PRIME_VIDEO_APP_ID}\", \"params\": {{\"contentTarget\": \"{content_id}\"}} }}'", file=sys.stderr)
        res = await client.launch_app_with_params(
            PRIME_VIDEO_APP_ID,
            {"contentTarget": content_id},
        )
        print("[LAUNCH] Deep link / contentTarget sent to Prime (may still be applying profile or title page).", file=sys.stderr)
        return res
    print(f"[LAUNCH] -> launch_app_with_content_id amazon contentId={content_id}", file=sys.stderr)
    print(f"[LUNA-EQUIV] luna-send -n 1 -f luna://com.webos.applicationManager/launch '{{ \"id\": \"{PRIME_VIDEO_APP_ID}\", \"contentId\": \"{content_id}\" }}'", file=sys.stderr)
    res = await launch_app(client, PRIME_VIDEO_APP_ID, content_id=content_id)
    print("[LAUNCH] contentId launch sent to Prime.", file=sys.stderr)
    return res


async def launch_prime_content_candidates(
    client: "WebOsClient",
    content_id: str,
    *,
    try_all_ids: bool = False,
    cold_start: bool = True,
    detail_html: str | None = None,
    episode: int | None = None,
    prefer_episode: bool = False,
    autoplay: bool = False,
    start: int = 0,
) -> tuple[str, bool]:
    """Launch Prime with the best resolved content ID (or try each candidate)."""
    print(f"[LAUNCH-CANDS] content_id={content_id} episode={episode} autoplay={autoplay} start={start} cold_start={cold_start}", file=sys.stderr)
    candidates = resolve_prime_launch_ids(
        content_id,
        html=detail_html,
        episode=episode,
        prefer_episode=prefer_episode,
        autoplay=autoplay,
        start=start,
    )
    if len(candidates) > 1:
        print(f"  Resolved launch IDs: {', '.join(candidates)}")

    if not try_all_ids:
        launch_id = candidates[0]
        if launch_id != content_id:
            print(f"  TV launch target: {launch_id}")
        print(f"[LAUNCH-CANDS] launching first candidate: {launch_id} (is_autoplay_target={_is_autoplay_target(launch_id)})", file=sys.stderr)
        result = await launch_prime_content(
            client, launch_id, cold_start=cold_start
        )
        _check_launch_result(result)
        # Only an explicit autoplay deep link (autoplay=1) starts playback on its
        # own. A bare GTI/ASIN/contentId merely opens the title page — and for a
        # title with a saved position it shows "Resume" but does NOT auto-play —
        # so in that case start_playback must still press the Watch/Resume button.
        used_auto = _is_autoplay_target(launch_id)
        print(f"[LAUNCH-CANDS] launch result returnValue={result.get('returnValue')} used_autoplay={used_auto}", file=sys.stderr)
        return launch_id, used_auto

    last_id = candidates[-1]
    for idx, launch_id in enumerate(candidates):
        print(f"  Attempt {idx + 1}/{len(candidates)}: {launch_id}")
        result = await launch_prime_content(
            client, launch_id, cold_start=cold_start and idx == 0
        )
        _check_launch_result(result)
        last_id = launch_id
        if idx < len(candidates) - 1:
            await asyncio.sleep(4.0)
    # As above: only an autoplay=1 deep link starts on its own; otherwise the
    # title page needs a Watch/Resume keypress.
    return last_id, _is_autoplay_target(last_id)


async def enter_profile_pin(
    client: "WebOsClient",
    pin: str,
    *,
    delay: float = DEFAULT_PIN_DELAY,
) -> None:
    pin = pin.strip()
    if not pin.isdigit():
        raise ValueError("profile PIN must be digits only")

    print(f"  Waiting {delay:.1f}s for PIN prompt, then entering PIN ...")
    await asyncio.sleep(delay)
    for digit in pin:
        await client.button(digit)
        await asyncio.sleep(PROFILE_KEY_DELAY)
    await client.button("ENTER")
    print("  PIN entered.")


async def select_profile_type(
    client: "WebOsClient",
    profile_type: str,
    *,
    type_right: int | None = None,
    highlight_only: bool = False,
) -> None:
    """Pick Adult/Kids on Prime's first profile-type screen."""
    if profile_type not in PRIME_PROFILE_TYPES or profile_type == "none":
        return

    if type_right is None:
        type_right = 1 if profile_type == "adult" else 0

    action = "highlighting" if highlight_only else "selecting"
    print(
        f"  {action.capitalize()} profile type '{profile_type}' "
        f"(RIGHT×{type_right}) ..."
    )
    for _ in range(type_right):
        await client.button("RIGHT")
        await asyncio.sleep(PROFILE_KEY_DELAY)

    if highlight_only:
        print(
            f"  Profile type '{profile_type}' highlighted (no ENTER). "
            "Check the TV before the name step."
        )
        return

    await client.button("ENTER")
    print(f"  Profile type '{profile_type}' selected.")


def resolve_profile_selection(
    *,
    profile: int | None,
    profile_name: str | None,
    profile_type: str | None,
    profile_row: int,
    profile_pin: str | None,
) -> tuple[int, int, str | None, str | None, str]:
    """Return (index, row, pin, display_name, picker_type) from --profile or --profile-name."""
    if profile_name and profile is None:
        resolved_type, entry = resolve_profile_name(
            profile_name,
            profile_type=profile_type,
        )
        row = profile_row if profile_row else entry.row
        pin = profile_pin or entry.pin
        return entry.index, row, pin, entry.name, resolved_type
    if profile is None:
        raise ValueError("either --profile or --profile-name is required")
    # Settings sends both index and name. The index is the TV slot; do not let
    # a leftover Adult/Kids mapping change the picker type or the slot.
    display = profile_name.strip() if profile_name else None
    return profile, profile_row, profile_pin, display, profile_type or "none"


async def wait_for_prime_profile_picker(
    client: "WebOsClient",
    *,
    extra: float = 2.0,
    timeout: float = 10.0,
) -> None:
    """Wait until Prime is in front, then a short beat for the avatar list.

    A fixed 14s sleep made warm starts look stuck on the profile screen.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(timeout, extra)
    while loop.time() < deadline:
        app_id = await _current_app_id(client)
        if _is_prime_app(app_id):
            print(
                f"  Prime in foreground ({app_id or 'amazon'}); "
                f"waiting {extra:.1f}s for the profile list ...",
                file=sys.stderr,
            )
            await asyncio.sleep(max(0.0, extra))
            return
        await asyncio.sleep(0.35)
    print(
        "  Prime not reported in foreground yet; sending profile keys anyway ...",
        file=sys.stderr,
    )


async def select_profile(
    client: "WebOsClient",
    profile: int,
    *,
    delay: float = DEFAULT_PROFILE_DELAY,
    row: int = 0,
    profile_type: str = "none",
    profile_type_right: int | None = None,
    profile_step_delay: float = DEFAULT_PROFILE_STEP_DELAY,
    pin: str | None = None,
    pin_delay: float = DEFAULT_PIN_DELAY,
    highlight_only: bool = False,
    profile_display_name: str | None = None,
) -> None:
    """Pick a Prime Video profile on the name picker (after optional type step).

    Default is a single-screen picker (``profile_type="none"``): a vertical list
    of profiles (device capture: Margaret Evans / Kids / D / New). Index moves
    with DOWN, not RIGHT.

    Last-used is pre-focused, so we home to the first avatar (UP / LEFT) before
    applying the index. One ENTER — a second ENTER re-opens the picker or
    activates Home.

    Two-step Adult/Kids UIs use ``--profile-type adult|kid`` then a horizontal
    name row (RIGHT for index).
    """
    if profile < 0:
        raise ValueError("profile must be >= 0")
    if row < 0:
        raise ValueError("profile-row must be >= 0")
    if profile_type not in PRIME_PROFILE_TYPES:
        raise ValueError(
            f"profile-type must be one of {PRIME_PROFILE_TYPES}, not {profile_type!r}"
        )

    size = none_picker_real_count() if profile_type == "none" else 0
    # Prime pre-focuses last-used. Do not trust a saved "we are on D" after a
    # play that actually selected Kids — ENTER-only then keeps picking Kids.
    # Never BACK (that quits Prime). Never UP off D onto Kids.
    last = read_last_profile_focus()
    if last is None and profile_type == "none":
        # Prime pre-focuses last-used. After a successful D select that is D.
        # Do not assume Kids (DOWN from D is New).
        last = profile
    nav = profile_nav_keys(
        profile,
        row=row,
        profile_type=profile_type,
        list_size=size or None,
        last_focused=last,
    )
    label = profile_display_name or f"profile {profile}"
    action = "highlighting" if highlight_only else "selecting"
    print(
        f"  Waiting for profile picker, then {action} "
        f"{label!r} (type={profile_type}, index {profile}, "
        f"assume-focus={last}, keys={'+'.join(nav) or 'ENTER only'}) ...",
        file=sys.stderr,
    )
    await wait_for_prime_profile_picker(
        client,
        extra=9.0,
        timeout=18.0,
    )

    await select_profile_type(
        client,
        profile_type,
        type_right=profile_type_right,
        highlight_only=highlight_only and profile_type != "none",
    )
    if profile_type != "none" and not highlight_only and profile_step_delay > 0:
        print(f"  Waiting {profile_step_delay:.1f}s for profile name picker ...")
        await asyncio.sleep(profile_step_delay)

    print(
        f"  Profile keys → slot {profile}: {nav or ['ENTER']}",
        file=sys.stderr,
    )
    for key in nav:
        print(f"  → {key}", file=sys.stderr)
        await client.button(key)
        await asyncio.sleep(0.7)
    if nav:
        await asyncio.sleep(0.9)

    if highlight_only:
        print(
            f"  {label!r} highlighted (no ENTER). "
            "Check the TV, then rerun without --profile-highlight."
        )
        return

    await client.button("ENTER")
    print(
        f"  {label!r} selected (type={profile_type}, ENTER×1, nav={'+'.join(nav) or 'none'}). "
        "No BACK (that quits Prime).",
        file=sys.stderr,
    )
    # Only record last-used when we actually moved onto the Settings slot.
    # Writing the target after ENTER-only is how we kept selecting Kids.
    if profile_type == "none" and not highlight_only:
        write_last_profile_focus(profile)

    if pin:
        await enter_profile_pin(client, pin, delay=pin_delay)


def _check_launch_result(result: dict) -> None:
    if not result.get("returnValue", True):
        print(f"  Launch failed: {json.dumps(result, indent=2)}")
        sys.exit(1)


def _label_requires_purchase(label: str) -> bool:
    lowered = label.lower()
    return any(
        marker in lowered
        for marker in (
            "join prime",
            "subscribe",
            "rent",
            "buy",
            "purchase",
            "free trial",
            "start your free trial",
        )
    )


def _prime_playback_plan(
    labels: list[str],
    *,
    method: str,
) -> tuple[str, str | None]:
    if method != "auto":
        return method, None

    if has_watchable_play_button(labels):
        watch_labels = [
            label
            for label in labels
            if any(
                marker in label.lower()
                for marker in ("watch now", "resume", "play movie", "play episode")
            )
            or (
                label.lower().startswith("watch")
                and "trailer" not in label.lower()
            )
        ]
        note = watch_labels[0] if watch_labels else labels[0]
        return "enter", note

    if labels and not has_watchable_play_button(labels):
        if any("trailer" in label.lower() for label in labels):
            return (
                "blocked",
                "No Watch/Resume button in catalog — only trailer/subscribe/rent offers.",
            )
        if all(_label_requires_purchase(label) for label in labels):
            return (
                "blocked",
                "No Watch/Resume button — this episode needs Prime, rent, or a channel subscription.",
            )

    if not labels:
        return "enter", None

    return "watch", None


async def _focus_prime_watch_button(
    client: "WebOsClient",
    *,
    up: int,
    down: int,
    left: int,
    highlight_only: bool = False,
) -> None:
    print(
        f"  Navigating focus to Watch (UP×{up}, DOWN×{down}, LEFT×{left}) ..."
    )
    for _ in range(up):
        await client.button("UP")
        await asyncio.sleep(PLAY_KEY_DELAY)
    for _ in range(down):
        await client.button("DOWN")
        await asyncio.sleep(PLAY_KEY_DELAY)
    for _ in range(left):
        await client.button("LEFT")
        await asyncio.sleep(PLAY_KEY_DELAY)
    if highlight_only:
        print("  Watch area focused (no ENTER). Check the TV highlight.")


async def _media_play_state(client: "WebOsClient") -> str | None:
    """Return playState from the foreground media session, if any.

    Values seen on webOS: "playing", "paused", "buffering". None = no session.
    """
    try:
        media = await client.get_media_foreground_app()
    except Exception as exc:
        print(f"  media foreground check failed: {exc}", file=sys.stderr)
        return None
    if not media:
        return None
    if isinstance(media, list):
        for item in media:
            if not isinstance(item, dict):
                continue
            state = str(item.get("playState") or "").lower().strip()
            if state:
                return state
        return None
    return None


async def _media_is_playing(client: "WebOsClient") -> bool:
    """True only when media is actively playing (not merely paused/focused)."""
    state = await _media_play_state(client)
    return state in {"playing", "buffering"}


async def _press_keys(client: "WebOsClient", *keys: str) -> None:
    for key in keys:
        await client.button(key)
        await asyncio.sleep(PLAY_KEY_DELAY)


async def _activate_prime_resume_or_watch(
    client: "WebOsClient",
    *,
    note: str = "",
) -> None:
    """Start or resume playback without hitting player chrome accessory buttons.

    Device video (docs / 0.mp4): once an episode is playing, Prime shows a
    floating **"Turn on subtitles"** button (and In scene / Cast). ENTER or
    D-pad navigation on that chrome opens captions or cast — not Resume/Watch.

    SSAP media state is unreliable for Prime (often unknown while video plays),
    so we must **not** fall through to DOWN/LEFT/ENTER when the player is up.
    Prefer PLAY only; use D-pad+ENTER only when we are clearly still on a
    title/hub page (non-Prime SSAP can confirm that).
    """
    print(f"  Starting/resuming playback{note} ...")

    state = await _media_play_state(client)
    if state in {"playing", "buffering"}:
        print(f"  Media already {state}; leaving it alone.")
        return
    if state == "paused":
        await client.button("PLAY")
        print("  Media was paused — sent PLAY to resume.")
        await asyncio.sleep(1.5)
        if await _media_is_playing(client):
            return

    # Prime: never D-pad/ENTER into the player. "Turn on subtitles" / Cast sit
    # on the same chrome and steal ENTER (see caption-select play video).
    if await _prefer_remote_keys(client):
        print(
            "  Prime: PLAY only (skip DOWN/ENTER — avoids Turn on subtitles / Cast)",
            file=sys.stderr,
        )
        await client.button("PLAY")
        await asyncio.sleep(2.0)
        if await _media_is_playing(client):
            print("  Playback started after PLAY.")
            return
        # Second PLAY nudge; still no ENTER.
        await client.button("PLAY")
        await asyncio.sleep(1.5)
        if await _media_is_playing(client):
            print("  Playback started after second PLAY.")
        else:
            print(
                "  Warning: media state unknown after PLAY — if video is already "
                "playing, that is OK (Prime SSAP often cannot report state).",
                file=sys.stderr,
            )
        return

    # Non-Prime players: D-pad to Watch CTA then ENTER is still useful.
    print("  Focus: DOWN → LEFT×4 → UP → ENTER (select Resume/Watch)")
    await _press_keys(client, "DOWN", "LEFT", "LEFT", "LEFT", "LEFT", "UP", "ENTER")
    await asyncio.sleep(2.5)
    state = await _media_play_state(client)
    if state in {"playing", "buffering"}:
        print("  Playback started after Resume focus + ENTER.")
        return
    if state == "paused":
        await client.button("PLAY")
        print("  ENTER left media paused — sent PLAY.")
        await asyncio.sleep(1.5)
        if await _media_is_playing(client):
            return

    print("  Retry: ENTER only")
    await client.button("ENTER")
    await asyncio.sleep(2.5)
    if await _media_is_playing(client):
        print("  Playback started after plain ENTER.")
        return

    await client.button("PLAY")
    print("  Sent PLAY fallback.")
    await asyncio.sleep(1.5)
    if await _media_is_playing(client):
        print("  Playback started after PLAY fallback.")
    else:
        print(
            "  Warning: media still not playing — Resume/Watch may not be focused.",
            file=sys.stderr,
        )


_MEDIA_INFO_ENDPOINTS = (
    ("ssap://com.webos.service.media.player/getInfo", {}),
    ("ssap://com.webos.service.cepswm.media.player/getInfo", {}),
    ("ssap://media.infoAction.getInfoPerApp", {"id": "amazon"}),
    ("ssap://com.webos.service.media.player/getPlayInfo", {}),
    ("ssap://media/getPlaybackState", {}),
    ("ssap://com.webos.service.tv.playback/getPlaybackInfo", {}),
    ("ssap://media.controls/getMediaInfo", {}),
)


async def _playback_position(client: "WebOsClient") -> float | None:
    """Best-effort current playback position (seconds) from the TV, or None.

    Availability is WebOS/app-version dependent (Prime in particular rarely
    exposes via the standard SSAP player endpoints), so callers must treat
    ``None`` as *unknown*. Raw results are logged for diagnostics.
    """
    for uri, payload in _MEDIA_INFO_ENDPOINTS:
        try:
            result = await client.request(uri, payload)
        except Exception as exc:
            print(f"[POS-TRY] {uri} exception: {exc}", file=sys.stderr)
            continue
        # Always log what Prime/webOS actually returns (very useful when all null)
        try:
            print(f"[POS-TRY] {uri} -> returnValue={result.get('returnValue')} keys={list(result.keys())[:8]} sample={json.dumps({k: result.get(k) for k in list(result)[:6]})}", file=sys.stderr)
        except Exception:
            pass
        if not result.get("returnValue"):
            continue
        pos = (
            result.get("currentTime")
            or result.get("position")
            or result.get("mediaCurrentTime")
            or result.get("playTime")
        )
        if pos is not None:
            try:
                return float(pos)
            except (TypeError, ValueError):
                return None
    return None


async def _wait_until_playing(client: "WebOsClient", timeout: float) -> bool:
    """Poll the TV up to ``timeout`` seconds; return True once playback starts.

    Distinguishes a freshly-launched episode that auto-played (the media player
    is already up) from one that landed on its detail page showing "Resume"
    (which still needs a keypress to start).
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, timeout)
    while True:
        if await _playback_position(client) is not None:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(1.0)


async def start_playback(
    client: "WebOsClient",
    *,
    delay: float = DEFAULT_PLAY_DELAY,
    method: str = "auto",
    play_labels: list[str] | None = None,
    used_autoplay_launch: bool = False,
    play_focus_up: int = DEFAULT_PLAY_FOCUS_UP,
    play_focus_down: int = DEFAULT_PLAY_FOCUS_DOWN,
    play_focus_left: int = DEFAULT_PLAY_FOCUS_LEFT,
    play_highlight: bool = False,
) -> None:
    """Start playback without touching Prime player chrome.

    Once the episode player is up, Prime shows accessory controls (In scene,
    Cast, Turn on subtitles). Any D-pad/ENTER there opens those menus — users
    reported In scene selected on episode start without requesting it.

    For Prime we only wait + PLAY. No UP/DOWN/LEFT/ENTER, no SSAP position
    probes (those also re-open chrome and steal focus).
    """
    labels = play_labels or []
    resolved_method, note = _prime_playback_plan(labels, method=method)

    if resolved_method == "blocked":
        print(f"  Note: {note}", file=sys.stderr)
        print(
            "  Public catalog shows no Watch/Play — TV may still offer Play if "
            "this profile has Prime. Will try ENTER on the primary CTA.",
            file=sys.stderr,
        )
        resolved_method, note = "enter", None

    # ── Prime start ──────────────────────────────────────────────────────────
    # Never call _focus_prime_watch_button (UP×5/DOWN×2) on Prime when the
    # player is already open — those keys land on In scene / Cast / captions.
    #
    # Branch on whether launch used an autoplay=1 deep link:
    #   • autoplay used  → player should be up; PLAY only (ENTER steals chrome)
    #   • no autoplay    → title hub with Play/Watch/Resume CTA; PLAY alone
    #                      does not select that button — ENTER activates it
    if await _prefer_remote_keys(client):
        settle = delay if delay > 0 else min(DEFAULT_PLAY_DELAY, 4.0)
        why = "autoplay=1" if used_autoplay_launch else f"method={resolved_method}"
        if play_highlight:
            print(
                f"  Prime start ({why}): wait {settle:.1f}s, highlight only "
                f"(no PLAY/ENTER).",
                file=sys.stderr,
            )
            print("[START-PLAYBACK] Prime highlight-only path", file=sys.stderr)
            await asyncio.sleep(settle)
            return

        if used_autoplay_launch:
            print(
                f"  Prime start ({why}): wait {settle:.1f}s + PLAY only "
                f"(no ENTER — avoids In scene / Cast / Turn on subtitles).",
                file=sys.stderr,
            )
            print("[START-PLAYBACK] Prime PLAY-only path (autoplay launch)", file=sys.stderr)
            await asyncio.sleep(settle)
            await client.button("PLAY")
            print("  Sent PLAY after autoplay launch settle (Prime).")
            # No _playback_position() — SSAP getInfo interrupts Prime and resurfaces
            # player chrome where In scene steals focus.
            return

        # Title-page path (no autoplay deep link). Movies often land here when the
        # unsigned web page has no Watch URL (e.g. "Join Prime" / rent offers only)
        # even though the signed-in TV profile has a Play button. PLAY alone leaves
        # focus off Play; ENTER activates the primary CTA (Play / Watch / Resume).
        print(
            f"  Prime start ({why}): wait {settle:.1f}s + ENTER on primary CTA "
            f"(title page — Play was not auto-selected).",
            file=sys.stderr,
        )
        print("[START-PLAYBACK] Prime ENTER path (no autoplay launch)", file=sys.stderr)
        await asyncio.sleep(settle)
        await client.button("ENTER")
        print("  Sent ENTER to select primary Play/Watch CTA (Prime).")
        await asyncio.sleep(1.5)
        # If ENTER opened the player paused, or focus was already in-player,
        # PLAY resumes without reopening chrome menus.
        await client.button("PLAY")
        print("  Sent PLAY after ENTER (Prime title page).")
        return

    # ── Non-Prime players ────────────────────────────────────────────────────
    print(
        f"[START-PLAYBACK] non-Prime keys (method={resolved_method}) delay={delay}",
        file=sys.stderr,
    )

    if resolved_method in ("watch", "enter") and not play_highlight:
        probe = delay if delay > 0 else 2.0
        print(
            f"  Waiting up to {probe:.1f}s for the title page / autoplay, "
            "then starting playback only if needed ..."
        )
        if await _wait_until_playing(client, probe):
            print("  Playback already started after launch; no extra keys needed.")
            return
        print("  Not playing yet — pressing the Watch/Resume button.")
    elif delay > 0:
        detail = f" ({note})" if note else ""
        print(
            f"  Waiting {delay:.1f}s for title page, then starting playback "
            f"({resolved_method}{detail}) ..."
        )
        await asyncio.sleep(delay)

    if resolved_method == "media":
        result = await client.play()
        if not result.get("returnValue", True):
            print(f"  media.controls/play failed: {json.dumps(result)}")
        await asyncio.sleep(PLAY_KEY_DELAY)
        await client.button("PLAY")
        print("  Sent media play + PLAY key.")
    elif resolved_method == "watch":
        await _focus_prime_watch_button(
            client,
            up=play_focus_up,
            down=play_focus_down,
            left=play_focus_left,
            highlight_only=play_highlight,
        )
        if not play_highlight:
            await client.button("ENTER")
            print("  Sent ENTER after focus navigation.")
    elif resolved_method == "enter":
        note_detail = f" ({note})" if note else ""
        if play_highlight:
            print(
                f"  Watch/Resume button should be focused by default{note_detail}; "
                "rerun without --play-highlight to press ENTER "
                "(use --play-method watch if it is not focused)."
            )
        else:
            await _activate_prime_resume_or_watch(client, note=note_detail)
    try:
        pos = await _playback_position(client)
        if pos is not None:
            print(json.dumps({"resume_position_from_tv": pos}))
    except Exception:
        pass



async def cmd_launch_prime(
    client: "WebOsClient",
    *,
    content_id: str | None = None,
    profile: int | None = None,
    profile_name: str | None = None,
    profile_delay: float = DEFAULT_PROFILE_DELAY,
    profile_row: int = 0,
    profile_type: str | None = None,
    profile_type_right: int | None = None,
    profile_step_delay: float = DEFAULT_PROFILE_STEP_DELAY,
    profile_pin: str | None = None,
    profile_pin_delay: float = DEFAULT_PIN_DELAY,
    profile_highlight: bool = False,
    content_delay: float = DEFAULT_CONTENT_DELAY,
    play: bool = False,
    play_delay: float = DEFAULT_PLAY_DELAY,
    play_method: str = "auto",
    play_focus_up: int = DEFAULT_PLAY_FOCUS_UP,
    play_focus_down: int = DEFAULT_PLAY_FOCUS_DOWN,
    play_focus_left: int = DEFAULT_PLAY_FOCUS_LEFT,
    play_highlight: bool = False,
    browser: bool = False,
    try_all_ids: bool = False,
    close_after_profile: bool = False,
    skip_entitlement_check: bool = False,
    episode: int | None = None,
    title: str | None = None,
    start: int = 0,
    set_subtitles: str | None = None,
    subtitle_delay: float = DEFAULT_SUBTITLE_DELAY,
    subtitle_focus_down: int = DEFAULT_SUBTITLE_FOCUS_DOWN,
    subtitle_focus_right: int = DEFAULT_SUBTITLE_FOCUS_RIGHT,
    subtitle_section_up: int = DEFAULT_SUBTITLE_SECTION_UP,
    subtitle_section_left: int = DEFAULT_SUBTITLE_SECTION_LEFT,
    subtitle_menu_down: int | None = None,
) -> None:
    profile_display_name: str | None = None
    # Default single-screen vertical picker (see select_profile docstring / 0.mp4).
    effective_profile_type = profile_type or "none"
    detail_html: str | None = None
    used_autoplay_launch = False
    title_page_settled = False
    skip_watch_enter = False
    html_fetch: asyncio.Task[str | None] | None = None
    if content_id and not skip_entitlement_check:
        html_fetch = asyncio.create_task(
            asyncio.to_thread(fetch_prime_detail_html, content_id)
        )
    print(
        f"[CMD-LAUNCH] content_id={content_id} episode={episode} play={play} "
        f"start={start} profile={profile} profile_type={effective_profile_type}",
        file=sys.stderr,
    )

    if browser:
        if html_fetch is not None:
            html_fetch.cancel()
        if not content_id:
            print("error: --browser requires --content-id", file=sys.stderr)
            sys.exit(2)
        result = await launch_prime_browser(client, content_id)
        _check_launch_result(result)
        if play:
            await start_playback(client, delay=play_delay, method=play_method)
        print("  Done.")
        return

    if profile_name is not None or profile is not None:
        if profile is not None and profile_name:
            print(
                f"  Using Settings slot {profile} ({profile_name!r}); "
                "single-screen picker (no Adult/Kids step).",
                file=sys.stderr,
            )
        (
            profile,
            profile_row,
            profile_pin,
            profile_display_name,
            effective_profile_type,
        ) = resolve_profile_selection(
            profile=profile,
            profile_name=profile_name if profile is None else profile_name,
            profile_type=profile_type or "none",
            profile_row=profile_row,
            profile_pin=profile_pin,
        )

    if profile_delay < MIN_PROFILE_DELAY:
        print(
            f"  Warning: --profile-delay {profile_delay:g}s is short; "
            f"try >= {DEFAULT_PROFILE_DELAY:g}s if profile selection misses.",
            file=sys.stderr,
        )

    if profile is not None and content_id is not None:
        # AmazOff does not rewrite launches (only the player JS bundle).
        # Season 0-ids and /detail/0-ids miss or show "unavailable".
        # Episode GTI as contentId opened the series; a second launch
        # re-opened the picker (handlesRelaunch: true). One GTI → D → Watch.
        if html_fetch is not None:
            try:
                detail_html = await asyncio.wait_for(html_fetch, timeout=15.0)
            except Exception as exc:
                print(f"  Warning: detail fetch failed ({exc})", file=sys.stderr)
                detail_html = None
            if detail_html:
                report_prime_entitlement(content_id, html=detail_html)

        launch_id = amazoff_play_launch_id(
            content_id.strip(),
            html=detail_html,
            episode=episode,
            play=bool(play),
        )
        print(
            f"[PLAY] series gate content_id={content_id} episode={episode} "
            f"launch_id={launch_id}",
            file=sys.stderr,
        )

        result = await launch_prime_content(
            client, launch_id, cold_start=False
        )
        _check_launch_result(result)
        await select_profile(
            client,
            profile,
            delay=profile_delay,
            row=profile_row,
            profile_type=effective_profile_type,
            profile_type_right=profile_type_right,
            profile_step_delay=profile_step_delay,
            pin=profile_pin,
            pin_delay=profile_pin_delay,
            highlight_only=profile_highlight,
            profile_display_name=profile_display_name,
        )
        if profile_highlight:
            print("  Done (profile highlight only).")
            return

        # One launch only. A second amazon launch (even with the GTI) returns
        # to the profile picker and play stops there.
        print(
            "  Waiting 6s on the title after profile "
            "(no second launch — that re-opens the picker) ...",
            file=sys.stderr,
        )
        await asyncio.sleep(6.0)

        if close_after_profile:
            print("  --close-after-profile: closing and re-launching content ...")
            if await close_app(client, PRIME_VIDEO_APP_ID):
                await asyncio.sleep(1.5)
            result = await launch_prime_content(
                client, launch_id, cold_start=False
            )
            _check_launch_result(result)
            await asyncio.sleep(5.0)

        # Title page is up (user: series appears after D). ENTER Watch.
        # Not a second launch. Not Home hero.
        used_autoplay_launch = False
        title_page_settled = True
        skip_watch_enter = not play
        _ = title
    elif content_id is not None:
        if html_fetch is not None:
            try:
                detail_html = await html_fetch
            except Exception as exc:
                print(f"  Warning: detail fetch failed ({exc})", file=sys.stderr)
                detail_html = None
        print(f"[PLAY] no-profile direct launch path: content_id={content_id} episode={episode} play={play} start={start}", file=sys.stderr)
        use_autoplay = bool(play) or bool(start and start > 0)
        _, used_autoplay_launch = await launch_prime_content_candidates(
            client,
            content_id,
            try_all_ids=try_all_ids,
            cold_start=False,
            detail_html=detail_html,
            episode=episode,
            prefer_episode=play or episode is not None,
            autoplay=use_autoplay,
            start=start,
        )
        print(f"[PLAY] direct path used_autoplay_launch={used_autoplay_launch}", file=sys.stderr)
        if profile is not None:
            await select_profile(
                client,
                profile,
                delay=profile_delay,
                row=profile_row,
                profile_type=effective_profile_type,
                profile_type_right=profile_type_right,
                profile_step_delay=profile_step_delay,
                pin=profile_pin,
                pin_delay=profile_pin_delay,
                highlight_only=profile_highlight,
                profile_display_name=profile_display_name,
            )
            if profile_highlight:
                print("  Done (profile highlight only).")
                return
    else:
        if html_fetch is not None:
            html_fetch.cancel()
        result = await launch_app(client, PRIME_VIDEO_APP_ID)
        _check_launch_result(result)
        if profile is not None:
            await select_profile(
                client,
                profile,
                delay=profile_delay,
                row=profile_row,
                profile_type=effective_profile_type,
                profile_type_right=profile_type_right,
                profile_step_delay=profile_step_delay,
                pin=profile_pin,
                pin_delay=profile_pin_delay,
                highlight_only=profile_highlight,
                profile_display_name=profile_display_name,
            )
            if profile_highlight:
                print("  Done (profile highlight only).")
                return

    if play and content_id is not None:
        play_labels: list[str] = []
        channel_only = False
        channel_name: str | None = None
        try:
            play_html = detail_html or fetch_prime_detail_html(content_id)
            if play_html:
                play_id = resolve_episode_content_id(
                    play_html, content_id, episode=episode
                )
                label_html = play_html
                if play_id != content_id:
                    episode_html = fetch_prime_detail_html(play_id)
                    if episode_html:
                        label_html = episode_html
                play_labels = playback_labels_from_html(label_html, play_id)
                if play_labels:
                    print(f"  TV actions: {', '.join(play_labels)}")
                ent = parse_entitlement(label_html, content_id=play_id)
                channel_only = (
                    bool(ent.included_with_channel)
                    and not ent.included_with_prime
                    and not ent.prime_catalog
                )
                channel_name = ent.included_with_channel or ent.channel
        except (OSError, ValueError) as exc:
            print(f"  Warning: could not read Prime play actions ({exc})", file=sys.stderr)
        if channel_only:
            label = channel_name or "a channel subscription"
            print(
                f"  Skipping auto-play: this title is only available via {label}. "
                "Opened the title page so you can start it manually."
            )
        elif skip_watch_enter:
            print(
                "  Skipping Watch ENTER — not on a title page "
                "(avoids Adult/Kids and the Home hero).",
                file=sys.stderr,
            )
        else:
            await start_playback(
                client,
                delay=0 if title_page_settled else play_delay,
                method=play_method,
                play_labels=play_labels,
                used_autoplay_launch=used_autoplay_launch,
                play_focus_up=play_focus_up,
                play_focus_down=play_focus_down,
                play_focus_left=play_focus_left,
                play_highlight=play_highlight,
            )
            if set_subtitles is not None:
                await cmd_set_subtitles(
                    client,
                    language=set_subtitles,
                    delay=subtitle_delay,
                    focus_down=subtitle_focus_down,
                    focus_right=subtitle_focus_right,
                    section_up=subtitle_section_up,
                    section_left=subtitle_section_left,
                    menu_down=subtitle_menu_down,
                )

    print("  Done.")


async def cmd_launch(
    client: "WebOsClient",
    app_id: str,
    *,
    content_id: str | None = None,
    profile: int | None = None,
    profile_name: str | None = None,
    profile_delay: float = DEFAULT_PROFILE_DELAY,
    profile_row: int = 0,
    profile_type: str | None = None,
    profile_type_right: int | None = None,
    profile_step_delay: float = DEFAULT_PROFILE_STEP_DELAY,
    profile_pin: str | None = None,
    profile_pin_delay: float = DEFAULT_PIN_DELAY,
    profile_highlight: bool = False,
    content_delay: float = DEFAULT_CONTENT_DELAY,
    play: bool = False,
    play_delay: float = DEFAULT_PLAY_DELAY,
    play_method: str = "auto",
    play_focus_up: int = DEFAULT_PLAY_FOCUS_UP,
    play_focus_down: int = DEFAULT_PLAY_FOCUS_DOWN,
    play_focus_left: int = DEFAULT_PLAY_FOCUS_LEFT,
    play_highlight: bool = False,
    browser: bool = False,
    try_all_ids: bool = False,
    close_after_profile: bool = False,
    skip_entitlement_check: bool = False,
    episode: int | None = None,
    title: str | None = None,
    start: int = 0,
    set_subtitles: str | None = None,
    subtitle_delay: float = DEFAULT_SUBTITLE_DELAY,
    subtitle_focus_down: int = DEFAULT_SUBTITLE_FOCUS_DOWN,
    subtitle_focus_right: int = DEFAULT_SUBTITLE_FOCUS_RIGHT,
    subtitle_section_up: int = DEFAULT_SUBTITLE_SECTION_UP,
    subtitle_section_left: int = DEFAULT_SUBTITLE_SECTION_LEFT,
    subtitle_menu_down: int | None = None,
) -> None:
    if app_id == PRIME_VIDEO_APP_ID:
        await cmd_launch_prime(
            client,
            content_id=content_id,
            profile=profile,
            profile_name=profile_name,
            profile_delay=profile_delay,
            profile_row=profile_row,
            profile_type=profile_type,
            profile_type_right=profile_type_right,
            profile_step_delay=profile_step_delay,
            profile_pin=profile_pin,
            profile_pin_delay=profile_pin_delay,
            profile_highlight=profile_highlight,
            content_delay=content_delay,
            play=play,
            play_delay=play_delay,
            play_method=play_method,
            play_focus_up=play_focus_up,
            play_focus_down=play_focus_down,
            play_focus_left=play_focus_left,
            play_highlight=play_highlight,
            browser=browser,
            try_all_ids=try_all_ids,
            close_after_profile=close_after_profile,
            skip_entitlement_check=skip_entitlement_check,
            episode=episode,
            title=title,
            start=start,
            set_subtitles=set_subtitles,
            subtitle_delay=subtitle_delay,
            subtitle_focus_down=subtitle_focus_down,
            subtitle_focus_right=subtitle_focus_right,
            subtitle_section_up=subtitle_section_up,
            subtitle_section_left=subtitle_section_left,
            subtitle_menu_down=subtitle_menu_down,
        )
        return

    if browser:
        print("error: --browser is only supported for Prime Video (amazon)", file=sys.stderr)
        sys.exit(2)

    result = await launch_app(client, app_id, content_id=content_id)
    _check_launch_result(result)
    profile_display_name: str | None = None
    effective_profile_type = profile_type or "none"
    if profile_name is not None:
        (
            profile,
            profile_row,
            profile_pin,
            profile_display_name,
            effective_profile_type,
        ) = resolve_profile_selection(
            profile=None,
            profile_name=profile_name,
            profile_type=profile_type,
            profile_row=profile_row,
            profile_pin=profile_pin,
        )
    if profile is not None:
        await select_profile(
            client,
            profile,
            delay=profile_delay,
            row=profile_row,
            profile_type=effective_profile_type,
            profile_type_right=profile_type_right,
            profile_step_delay=profile_step_delay,
            pin=profile_pin,
            pin_delay=profile_pin_delay,
            highlight_only=profile_highlight,
            profile_display_name=profile_display_name,
        )
    if play:
        await start_playback(client, delay=play_delay, method=play_method)
    print("  Done.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control an LG WebOS TV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""examples:
  %(prog)s                    pair (if needed) and show TV info
  %(prog)s 192.168.0.79 --info
  %(prog)s --apps
  %(prog)s --launch youtube.leanback.v4
  %(prog)s --list-profiles
  %(prog)s --profile-save "Adult" --profile 0 --profile-type adult
  %(prog)s --launch {PRIME_VIDEO_APP_ID} --profile-name "Adult" --profile-highlight
  %(prog)s --launch {PRIME_VIDEO_APP_ID} --content-id B09L5V3KJY --profile-name "Adult" --play

On first run the TV shows a pairing prompt — accept it.
The client key is saved to ~/.lg-tv-key and reused automatically.

Prime profiles are stored in ~/.lg-tv-prime-profiles.json. Map a name to the
picker index with --profile-save, then use --profile-name instead of --profile 0.
Use --profile-highlight to verify the mapped index on TV.""",
    )
    parser.add_argument("ip", nargs="?", default=DEFAULT_IP, help="TV IP address")
    parser.add_argument("--info", action="store_true", help="Show TV info & state")
    parser.add_argument("--apps", action="store_true", help="List installed apps")
    parser.add_argument(
        "--detect-amazoff",
        action="store_true",
        help="Print JSON whether AmazOff (com.amazoff.patcher) is installed on the TV",
    )
    parser.add_argument("--launch", metavar="APP_ID", help="Launch an app by ID")
    parser.add_argument(
        "--content-id",
        metavar="ID",
        help="Optional contentId passed to the launched app (e.g. Prime ASIN, YouTube v=...)",
    )
    parser.add_argument(
        "--title",
        metavar="NAME",
        help="Title to open via Prime in-app search after the profile picker (AmazOff ignores most deep links)",
    )
    parser.add_argument(
        "--profile",
        type=int,
        metavar="N",
        help="Prime profile picker index (use --profile-name when configured)",
    )
    parser.add_argument(
        "--profile-name",
        metavar="NAME",
        help='Configured profile name (see --list-profiles and --profile-save)',
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="Show configured Prime profile names and indices",
    )
    parser.add_argument(
        "--profile-save",
        metavar="NAME",
        help="Save a profile name → index mapping to ~/.lg-tv-prime-profiles.json",
    )
    parser.add_argument(
        "--profile-type",
        choices=PRIME_PROFILE_TYPES,
        default=None,
        help=(
            "Prime profile picker: none = single vertical list (default; DOWN for index); "
            "adult/kid = two-step Adult/Kids then horizontal names (RIGHT for index). "
            "Auto from --profile-name when saved."
        ),
    )
    parser.add_argument(
        "--profile-type-right",
        type=int,
        metavar="N",
        help=(
            "RIGHT presses on the Adult/Kids screen before ENTER "
            "(default: 1 for adult, 0 for kid)"
        ),
    )
    parser.add_argument(
        "--profile-step-delay",
        type=float,
        default=DEFAULT_PROFILE_STEP_DELAY,
        metavar="SECS",
        help=(
            "Seconds to wait between profile type and profile name screens "
            f"(default: {DEFAULT_PROFILE_STEP_DELAY:g})"
        ),
    )
    parser.add_argument(
        "--profile-row",
        type=int,
        default=0,
        metavar="N",
        help="Press DOWN N times before moving RIGHT on the profile picker (default: 0)",
    )
    parser.add_argument(
        "--profile-pin",
        metavar="PIN",
        help="Prime profile PIN digits to enter after profile selection (adult profiles)",
    )
    parser.add_argument(
        "--profile-pin-delay",
        type=float,
        default=DEFAULT_PIN_DELAY,
        metavar="SECS",
        help=f"Seconds to wait for PIN prompt before typing (default: {DEFAULT_PIN_DELAY:g})",
    )
    parser.add_argument(
        "--profile-highlight",
        action="store_true",
        help="Move to --profile but do not press ENTER (find the right index on TV)",
    )
    parser.add_argument(
        "--close-after-profile",
        action="store_true",
        help=(
            "Close Prime after profile pick before content launch (often resets to kids; "
            "not recommended)"
        ),
    )
    parser.add_argument(
        "--profile-delay",
        type=float,
        default=DEFAULT_PROFILE_DELAY,
        metavar="SECS",
        help=f"Seconds to wait for the profile picker before sending keys (default: {DEFAULT_PROFILE_DELAY:g})",
    )
    parser.add_argument(
        "--content-delay",
        type=float,
        default=DEFAULT_CONTENT_DELAY,
        metavar="SECS",
        help=(
            "Seconds to wait after profile selection before sending the content "
            f"deep link (default: {DEFAULT_CONTENT_DELAY:g}; Prime + --profile only)"
        ),
    )
    parser.add_argument(
        "--play",
        action="store_true",
        help="Start playback after the title page loads (Prime: media PLAY by default)",
    )
    parser.add_argument(
        "--play-delay",
        type=float,
        default=DEFAULT_PLAY_DELAY,
        metavar="SECS",
        help=f"Seconds to wait before --play (default: {DEFAULT_PLAY_DELAY:g})",
    )
    parser.add_argument(
        "--play-method",
        choices=PRIME_PLAY_METHODS,
        default="auto",
        metavar="MODE",
        help=(
            "How to start playback: auto=autoplay deep link + focus Watch (default), "
            "media=SSAP play+PLAY key, watch=focus+ENTER, enter=focus+ENTER"
        ),
    )
    parser.add_argument(
        "--play-focus-up",
        type=int,
        default=DEFAULT_PLAY_FOCUS_UP,
        metavar="N",
        help=f"UP key presses before Watch button (default: {DEFAULT_PLAY_FOCUS_UP})",
    )
    parser.add_argument(
        "--play-focus-down",
        type=int,
        default=DEFAULT_PLAY_FOCUS_DOWN,
        metavar="N",
        help=f"DOWN key presses before Watch button (default: {DEFAULT_PLAY_FOCUS_DOWN})",
    )
    parser.add_argument(
        "--play-focus-left",
        type=int,
        default=DEFAULT_PLAY_FOCUS_LEFT,
        metavar="N",
        help=f"LEFT key presses to land on Watch now (default: {DEFAULT_PLAY_FOCUS_LEFT})",
    )
    parser.add_argument(
        "--play-highlight",
        action="store_true",
        help="Navigate focus to Watch but do not press ENTER (tune --play-focus-*)",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Open Prime detail URL in the LG web browser instead of the Prime app",
    )
    parser.add_argument(
        "--episode",
        type=int,
        metavar="N",
        help="Prime TV episode number to launch (season detail IDs need an episode on TV)",
    )
    parser.add_argument(
        "--try-all-ids",
        action="store_true",
        help="Try GTI, ASIN, then detail ID in sequence (Prime app only)",
    )
    parser.add_argument(
        "--skip-entitlement-check",
        action="store_true",
        help="Do not fetch Prime web page to report rent/buy/Prime inclusion",
    )
    parser.add_argument(
        "--media-pause",
        action="store_true",
        help="Send media pause to whatever is playing on the TV",
    )
    parser.add_argument(
        "--media-play",
        action="store_true",
        help="Send media play/resume to the TV",
    )
    parser.add_argument(
        "--media-toggle",
        action="store_true",
        help="Toggle play/pause on the TV",
    )
    parser.add_argument(
        "--media-stop",
        action="store_true",
        help="Stop playback on the TV",
    )
    # ── Volume / audio ────────────────────────────────────────────────────────
    parser.add_argument(
        "--volume-get",
        action="store_true",
        help="Print current volume and mute state as JSON",
    )
    parser.add_argument(
        "--volume-set",
        type=int,
        metavar="N",
        help="Set absolute volume level (0–100)",
    )
    parser.add_argument(
        "--volume-up",
        type=int,
        nargs="?",
        const=1,
        metavar="N",
        help="Increase volume by N steps (default 1)",
    )
    parser.add_argument(
        "--volume-down",
        type=int,
        nargs="?",
        const=1,
        metavar="N",
        help="Decrease volume by N steps (default 1)",
    )
    parser.add_argument(
        "--mute",
        action="store_true",
        help="Mute the TV",
    )
    parser.add_argument(
        "--unmute",
        action="store_true",
        help="Unmute the TV",
    )
    parser.add_argument(
        "--media-skip-back",
        type=int,
        nargs="?",
        const=1,
        metavar="N",
        help="Rewind / skip back N remote steps (default 1, ~10s each on Prime)",
    )
    parser.add_argument(
        "--media-skip-forward",
        type=int,
        nargs="?",
        const=1,
        metavar="N",
        help="Fast-forward / skip ahead N remote steps (default 1, ~10s each on Prime)",
    )
    # ── Subtitles (Prime Video player) ────────────────────────────────────────
    parser.add_argument(
        "--set-subtitles",
        metavar="LANG",
        help=(
            "Set Prime subtitles: 'off' to disable, or a language code "
            "(en, sv, de, …). Applied after --play or to current playback."
        ),
    )
    parser.add_argument(
        "--subtitle-delay",
        type=float,
        default=DEFAULT_SUBTITLE_DELAY,
        metavar="SECS",
        help=f"Seconds to wait before opening the subtitle menu (default: {DEFAULT_SUBTITLE_DELAY:g})",
    )
    parser.add_argument(
        "--subtitle-focus-down",
        type=int,
        default=DEFAULT_SUBTITLE_FOCUS_DOWN,
        metavar="N",
        help=(
            "DOWN-key presses to surface/focus the transport row that contains "
            "Start again / Subtitles / Audio options "
            f"(default: {DEFAULT_SUBTITLE_FOCUS_DOWN})"
        ),
    )
    parser.add_argument(
        "--subtitle-focus-right",
        type=int,
        default=DEFAULT_SUBTITLE_FOCUS_RIGHT,
        metavar="N",
        help=(
            "LEFT presses *after RIGHT-home to Audio* to reach Subtitles CC "
            "(bar: Start again → [Next] → Subtitles → Audio; use 1). "
            "We home right so ENTER never hits Start again (would restart at 00:00). "
            f"(-1 = default {DEFAULT_SUBTITLE_FOCUS_RIGHT})"
        ),
    )
    parser.add_argument(
        "--subtitle-section-up",
        type=int,
        default=DEFAULT_SUBTITLE_SECTION_UP,
        metavar="N",
        help=(
            "UP presses inside the panel to highlight Subtitles instead of Audio "
            f"(when using vertical tabs; default: {DEFAULT_SUBTITLE_SECTION_UP}; 0=skip)"
        ),
    )
    parser.add_argument(
        "--subtitle-section-left",
        type=int,
        default=DEFAULT_SUBTITLE_SECTION_LEFT,
        metavar="N",
        help=(
            "LEFT presses inside the panel (after ENTER on the bar button) to reach "
            "the Subtitles column *only if* combined Audio+Subs panel opens on Audio side "
            "(see screengrab.jpg for dedicated 'Subtitles CC' button; default 0 = skip). "
            f"(default: {DEFAULT_SUBTITLE_SECTION_LEFT})"
        ),
    )
    parser.add_argument(
        "--subtitle-menu-down",
        type=int,
        default=-1,
        metavar="N",
        help=(
            "DOWN presses in the Subtitles column after UP-home "
            "(-1 = auto: Off=0, On=1; override if list differs)"
        ),
    )
    # ── Power ─────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--power-off",
        action="store_true",
        help="Power off the TV (requires network connection)",
    )
    parser.add_argument(
        "--power-state",
        action="store_true",
        help="Print TV power state as JSON {on, state}",
    )
    parser.add_argument(
        "--power-on",
        action="store_true",
        help="Power on the TV (optional --tv-mac for Wake-on-LAN when fully off)",
    )
    parser.add_argument(
        "--tv-mac",
        metavar="MAC",
        help="TV MAC address for Wake-on-LAN (e.g. AA:BB:CC:DD:EE:FF)",
    )
    parser.add_argument(
        "--get-mac",
        action="store_true",
        help="Print TV MAC from the local ARP table as JSON (TV should be on)",
    )
    # ── Seek / position ───────────────────────────────────────────────────────
    parser.add_argument(
        "--seek",
        type=float,
        metavar="SECONDS",
        help="Seek to absolute position in seconds",
    )
    parser.add_argument(
        "--start",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Start playback at this position (seconds) when used with --play",
    )
    parser.add_argument(
        "--get-position",
        action="store_true",
        help="Get current playback position and duration as JSON",
    )
    parser.add_argument(
        "--resume-position",
        action="store_true",
        help="Read saved resume position (seconds) from a Prime detail page as JSON",
    )
    parser.add_argument(
        "--list-episodes",
        action="store_true",
        help="List episodes for a TV season/series --content-id as JSON (no TV connection needed)",
    )
    return parser.parse_args()


def _is_prime_app(app_id: str | None) -> bool:
    if not app_id:
        return False
    return app_id in {PRIME_VIDEO_APP_ID, "com.amazon.firebat"} or app_id.startswith("amazon")


async def _current_app_id(client: "WebOsClient") -> str | None:
    try:
        app_id = await client.get_current_app()
        if isinstance(app_id, str) and app_id:
            return app_id
    except Exception:
        pass
    return client.tv_state.current_app_id or None


async def _prefer_remote_keys(client: "WebOsClient") -> bool:
    """Prime and unknown players need LG remote keys — SSAP media.controls won't work."""
    app_id = await _current_app_id(client)
    print(f"  foreground app: {app_id or 'unknown'}", file=sys.stderr)
    if app_id is None:
        return True
    return _is_prime_app(app_id)


async def _ime_is_focused(client: "WebOsClient") -> bool:
    """True when an on-screen keyboard has a text field (add-profile name)."""
    try:
        result = await client.request(
            "com.webos.service.ime/insertText",
            {"text": "", "replace": False},
        )
        return bool(result.get("returnValue", False))
    except Exception as exc:
        print(
            f"  IME focus check failed ({exc}) — assuming no keyboard.",
            file=sys.stderr,
        )
        return False


async def dismiss_add_profile_keyboard(client: "WebOsClient") -> bool:
    """If ENTER hit + / New, the add-profile keyboard is up. BACK to the list.

    Do not type a name and do not ENTER — that submits the wizard and opens
    Adult/Kids. One BACK only: BACK on the avatar list can leave Prime.
    """
    if not await _ime_is_focused(client):
        return False
    print(
        "  Keyboard after profile ENTER — Add profile (New), not Search. "
        "BACK (not typing a name).",
        file=sys.stderr,
    )
    await client.button("BACK")
    await asyncio.sleep(1.4)
    return True


async def _ime_insert_text(client: "WebOsClient", text: str, *, replace: bool = True) -> bool:
    """Type into the focused IME field via com.webos.service.ime/insertText."""
    try:
        result = await client.request(
            "com.webos.service.ime/insertText",
            {"text": text, "replace": replace},
        )
        ok = bool(result.get("returnValue", False))
        print(f"  IME insertText ok={ok} text={text!r}", file=sys.stderr)
        return ok
    except Exception as exc:
        print(f"  IME insertText failed: {exc}", file=sys.stderr)
        return False


async def _ime_send_enter(client: "WebOsClient") -> bool:
    try:
        result = await client.request("com.webos.service.ime/sendEnterKey", {})
        ok = bool(result.get("returnValue", True))
        print(f"  IME sendEnterKey ok={ok}", file=sys.stderr)
        return ok
    except Exception as exc:
        print(f"  IME sendEnterKey failed: {exc}", file=sys.stderr)
        return False


def _prime_search_targets(title: str) -> list[str]:
    """Launch targets that should open Prime's search page for `title`."""
    q = urllib.parse.quote(title)
    return [
        f"/search?phrase={q}",
        f"{PRIME_DEEP_LINK_BASE}/search?phrase={q}",
        f"{PRIME_WWW_BASE}/search?phrase={q}",
    ]


async def prime_search_open_title(
    client: "WebOsClient",
    title: str,
) -> bool:
    """Open a title via Prime search + IME. Return True if a result was opened.

    Call only after the avatar list is gone. Never hardware-ENTER to submit
    search — that plays the Home hero (Reacher) if Search is not focused.
    """
    title = (title or "").strip()
    if not title:
        print("  prime_search_open_title: empty title, skip", file=sys.stderr)
        return False

    print(
        f"  Opening Prime search and typing {title!r} ...",
        file=sys.stderr,
    )
    typed = False

    # Prefer an in-app search page (no D-pad to the header Search chip).
    for target in _prime_search_targets(title):
        try:
            print(f"  Search launch {target}", file=sys.stderr)
            await client.launch_app_with_params(
                PRIME_VIDEO_APP_ID,
                {"contentTarget": target},
            )
            await asyncio.sleep(2.4)
            typed = await _ime_insert_text(client, title, replace=True)
        except Exception as exc:
            print(f"  Search launch failed ({exc})", file=sys.stderr)
            typed = False
        if typed:
            break

    if not typed:
        for attempt in range(2):
            try:
                await client.button("SEARCH")
                await asyncio.sleep(2.2)
                typed = await _ime_insert_text(client, title, replace=True)
            except Exception as exc:
                print(f"  SEARCH key failed ({exc})", file=sys.stderr)
                typed = False
            if typed:
                break
            print("  IME not focused after SEARCH — retrying ...", file=sys.stderr)
            await asyncio.sleep(0.8)

    if not typed:
        print(
            "  IME did not take the title — not pressing ENTER "
            "(that starts Reacher on Home).",
            file=sys.stderr,
        )
        return False

    await asyncio.sleep(0.6)
    if not await _ime_send_enter(client):
        print(
            "  IME Enter failed — not sending remote ENTER "
            "(that starts Reacher on Home).",
            file=sys.stderr,
        )
        return False
    await asyncio.sleep(3.5)

    await client.button("DOWN")
    await asyncio.sleep(0.4)
    await client.button("ENTER")
    print(f"  Search: opened first result for {title!r}", file=sys.stderr)
    await asyncio.sleep(4.0)
    return True


async def _send_button(client: "WebOsClient", name: str) -> bool:
    """Send a remote key, reconnecting once if the input socket dropped."""
    for attempt in range(2):
        try:
            await client.button(name)
            print(f"  Sent {name} key.", file=sys.stderr)
            return True
        except Exception as exc:
            print(f"  button {name} failed (attempt {attempt + 1}): {exc}", file=sys.stderr)
            if attempt == 0:
                try:
                    await _safe_disconnect(client)
                    await asyncio.sleep(0.4)
                    await asyncio.wait_for(client.connect(), timeout=DEFAULT_CONNECT_TIMEOUT)
                    await asyncio.sleep(0.2)
                except Exception as reconnect_exc:
                    print(f"  reconnect failed: {reconnect_exc}", file=sys.stderr)
                    return False
    return False


def _resolve_subtitle_menu_down(language: str, menu_down: int | None) -> int:
    """DOWN presses in the expanded captions list after UP-home (Off=0)."""
    if menu_down is not None and menu_down >= 0:
        return menu_down
    lang = (language or "en").strip().lower()
    if lang in ("off", "none", "disabled"):
        return 0
    return SUBTITLE_LANGUAGE_DOWN.get(lang, SUBTITLE_LANGUAGE_DOWN.get("en", 1))


async def _focus_subtitles_section(
    client: "WebOsClient",
    *,
    section_up: int,
    section_left: int,
) -> None:
    """Optional focus nudge for rare combined Audio+Subtitles panels."""
    if section_left > 0:
        print(
            f"  Select Subtitles section: LEFT×{section_left} ...",
            file=sys.stderr,
        )
        for _ in range(section_left):
            await _send_button(client, "LEFT")
            await asyncio.sleep(SUBTITLE_KEY_DELAY)
    if section_up > 0:
        print(
            f"  Select Subtitles section: UP×{section_up} ...",
            file=sys.stderr,
        )
        for _ in range(section_up):
            await _send_button(client, "UP")
            await asyncio.sleep(SUBTITLE_KEY_DELAY)


def _normalize_subtitle_left_from_audio(focus_right: int) -> int:
    """LEFT steps from Audio (rightmost) to Subtitles CC.

    ``focus_right`` is the Settings "Focus right" value. Navigation no longer
    LEFT-homes onto "Start again" (ENTER there restarts the title at 00:00).
    Instead we RIGHT-home to Audio, then LEFT×N.

    Legacy configs stored RIGHT counts from Start again (1 without Next, 2 with
    Next). Both meant Subtitles and map to LEFT×1 from Audio. Values ≥ 3 are
    kept for unusual bars (extra icons right of Audio).
    """
    if focus_right < 0:
        return DEFAULT_SUBTITLE_FOCUS_RIGHT
    if focus_right in (1, 2):
        return 1
    return focus_right


async def _ensure_paused_for_subtitles(client: "WebOsClient") -> None:
    """Pause playback so the transport / captions bar can be focused.

    Prefer SSAP pause() when playing — the remote PAUSE key *toggles*, so if we
    are already paused it would resume and every following key hits the wrong UI.
    """
    state = await _media_play_state(client)
    if state == "paused":
        print("  Already paused — ready for subtitle menu.", file=sys.stderr)
        await asyncio.sleep(0.3)
        return
    if state in {"playing", "buffering"}:
        try:
            result = await client.pause()
            if result.get("returnValue", True):
                print("  SSAP pause for subtitle menu.", file=sys.stderr)
                await asyncio.sleep(1.0)
                if await _media_play_state(client) == "paused":
                    return
                print("  Still not paused after SSAP — trying PAUSE key.", file=sys.stderr)
        except Exception as exc:
            print(f"  SSAP pause failed ({exc}); trying PAUSE key.", file=sys.stderr)
    if await _media_play_state(client) != "paused":
        await _send_button(client, "PAUSE")
        await asyncio.sleep(1.0)


async def _open_prime_subtitle_picker(
    client: "WebOsClient",
    *,
    focus_down: int,
    focus_right: int,
) -> bool:
    """Open the Subtitles CC panel from the player transport bar.

    Bar (left→right): Start again → [Next] → Subtitles CC → Audio.

    **Important:** never LEFT-home to "Start again" and ENTER — that restarts
    the title at 00:00. Home RIGHT to Audio (rightmost), then LEFT to Subtitles
    (always adjacent, with or without Next). Opens the captions overlay with
    the Off pill focused; caller then presses Select to expand languages.
    """
    await _ensure_paused_for_subtitles(client)

    left_from_audio = _normalize_subtitle_left_from_audio(focus_right)
    down_steps = max(0, focus_down)

    if down_steps:
        print(
            f"  Focus transport row: DOWN×{down_steps} ...",
            file=sys.stderr,
        )
        for _ in range(down_steps):
            await _send_button(client, "DOWN")
            await asyncio.sleep(SUBTITLE_KEY_DELAY)

    # Home to Audio (rightmost). Extra RIGHTs at the edge are no-ops (no wrap).
    # Avoids ever focusing "Start again" before ENTER.
    print("  Homing right to Audio (avoid Start again) ...", file=sys.stderr)
    for _ in range(5):
        await _send_button(client, "RIGHT")
        await asyncio.sleep(SUBTITLE_KEY_DELAY)

    print(
        f"  Opening Subtitles CC: LEFT×{left_from_audio} + ENTER ...",
        file=sys.stderr,
    )
    for _ in range(left_from_audio):
        await _send_button(client, "LEFT")
        await asyncio.sleep(SUBTITLE_KEY_DELAY)
    if not await _send_button(client, "ENTER"):
        return False
    # Panel opens with Off pill focused + "Press Select … language options".
    await asyncio.sleep(1.0)
    return True


async def _dismiss_subtitle_config_panel(client: "WebOsClient") -> None:
    """Close the captions overlay (one BACK only — more would leave the player)."""
    print("  Closing subtitle config panel (BACK once) ...", file=sys.stderr)
    await _send_button(client, "BACK")
    # Panel must fully tear down before resume — a PLAY/ENTER while Off/On is
    # still focused toggles the value (Off → On or On → Off).
    await asyncio.sleep(1.4)


async def _resume_after_subtitles(
    client: "WebOsClient",
    *,
    play_key_fallback: bool = False,
) -> None:
    """Resume playback after subtitle menu navigation.

    Prefer SSAP play() only. A remote PLAY key while the captions row is still
    focused acts like ENTER and toggles On/Off — never use it after disable.
    """
    state = await _media_play_state(client)
    if state == "playing":
        print("  Already playing after subtitles — skip resume.", file=sys.stderr)
        return
    try:
        result = await client.play()
        if not result.get("returnValue", True):
            print("  SSAP play returned false", file=sys.stderr)
        else:
            print("  SSAP play to resume after subtitles.", file=sys.stderr)
            return
    except Exception as exc:
        print(f"  media.controls/play during subs resume: {exc}", file=sys.stderr)
    if play_key_fallback:
        await asyncio.sleep(0.5)
        if await _send_button(client, "PLAY"):
            print("  Sent PLAY key fallback to resume after subtitles.", file=sys.stderr)
    else:
        print(
            "  Skipping PLAY key fallback (avoids re-toggling captions).",
            file=sys.stderr,
        )


async def _select_captions_from_off_panel(
    client: "WebOsClient",
    *,
    enabled: bool,
    steps_down: int,
    off_up_from_language: int = 1,
) -> None:
    """Set Subtitles Off/On on the expanded captions panel (device video).

    docs/caption-select-0.mp4 expanded chrome:
      Subtitles (Off|On) | Languages (English [CC]) | Sizes | Styles

    The Subtitles control is a two-state field. Changing it with DOWN/UP updates
    the value in place. A further ENTER on that field **toggles** again — that
    was the on→off bug (DOWN sets On, ENTER flips back to Off).

    Enable:  ENTER expand → LEFT to Subtitles → DOWN (Off→On) → stop (no ENTER)
    Disable: ENTER expand → LEFT to Subtitles → UP (On→Off) → stop (no ENTER)
    """
    target = "On" if enabled and steps_down > 0 else "Off"
    _ = off_up_from_language

    print(
        f"  Captions panel: expand → Subtitles → {target} (no confirm ENTER) ...",
        file=sys.stderr,
    )
    # Expand collapsed Off/On pill → multi-column panel.
    await _send_button(client, "ENTER")
    await asyncio.sleep(1.2)

    # Leftmost column = Subtitles (not Languages / Sizes / Styles).
    for _ in range(3):
        await _send_button(client, "LEFT")
        await asyncio.sleep(SUBTITLE_KEY_DELAY)

    if target == "On":
        # Off → On. Do NOT press ENTER afterward (toggles back to Off).
        await _send_button(client, "DOWN")
        await asyncio.sleep(0.9)
        print("  Captions → On via DOWN (skipped ENTER to avoid toggle-off).", file=sys.stderr)
    else:
        # On → Off. One UP is enough when focused on On; extra UPs can wrap.
        await _send_button(client, "UP")
        await asyncio.sleep(0.9)
        print("  Captions → Off via UP (skipped ENTER to avoid toggle-on).", file=sys.stderr)

    # Let the pill value settle before BACK.
    await asyncio.sleep(1.8)


async def apply_subtitles(
    client: "WebOsClient",
    *,
    enabled: bool,
    language: str = "en",
    delay: float = DEFAULT_SUBTITLE_DELAY,
    focus_down: int = DEFAULT_SUBTITLE_FOCUS_DOWN,
    focus_right: int = DEFAULT_SUBTITLE_FOCUS_RIGHT,
    section_up: int = DEFAULT_SUBTITLE_SECTION_UP,
    section_left: int = DEFAULT_SUBTITLE_SECTION_LEFT,
    menu_down: int | None = None,
) -> None:
    """Open Prime captions UI and set Off or On (device video + photos).

    Path:
      1) Pause → DOWN → RIGHT-home Audio → LEFT Subtitles CC → ENTER (pill)
      2) ENTER expand → LEFT Subtitles → DOWN=On / UP=Off (no extra ENTER)
      3) Long settle → BACK once → settle → SSAP play (no remote PLAY key)
    """
    raw = (language or "en").strip().lower()
    preferred_lang = "en"
    if raw.startswith("off:"):
        preferred_lang = raw.split(":", 1)[1].strip() or "en"
        enabled = False
        lang = "off"
    elif not enabled or raw in ("off", "none", "disabled"):
        preferred_lang = raw if raw not in ("off", "none", "disabled", "") else "en"
        lang = "off"
        enabled = False
    else:
        lang = raw
        preferred_lang = raw
    steps_down = _resolve_subtitle_menu_down(lang, menu_down)
    off_up = _resolve_subtitle_menu_down(preferred_lang, menu_down)
    if off_up <= 0:
        off_up = 1

    print(
        f"  Applying subtitles: {'on' if enabled else 'off'}"
        f"{f' ({preferred_lang})' if enabled else ''} ...",
        file=sys.stderr,
    )

    if delay > 0:
        await asyncio.sleep(delay)

    if not await _open_prime_subtitle_picker(
        client, focus_down=focus_down, focus_right=focus_right
    ):
        print("  Warning: could not open subtitle picker.", file=sys.stderr)
        await _resume_after_subtitles(client, play_key_fallback=False)
        print(json.dumps({"subtitles": enabled, "language": lang if enabled else "off"}))
        return

    if section_up > 0 or section_left > 0:
        await _focus_subtitles_section(
            client, section_up=section_up, section_left=section_left
        )

    await _select_captions_from_off_panel(
        client,
        enabled=enabled,
        steps_down=steps_down,
        off_up_from_language=off_up,
    )

    # BACK only after the On/Off value has settled — early BACK can cancel.
    print("  Captions: settle then BACK (dismiss overlay) ...", file=sys.stderr)
    await asyncio.sleep(1.0)
    await _dismiss_subtitle_config_panel(client)
    await asyncio.sleep(1.0)
    # SSAP play only — remote PLAY acts like ENTER on a dying captions row.
    await _resume_after_subtitles(client, play_key_fallback=False)

    print(json.dumps({"subtitles": enabled, "language": lang if enabled else "off"}))


async def cmd_set_subtitles(
    client: "WebOsClient",
    *,
    language: str,
    delay: float = DEFAULT_SUBTITLE_DELAY,
    focus_down: int = DEFAULT_SUBTITLE_FOCUS_DOWN,
    focus_right: int = DEFAULT_SUBTITLE_FOCUS_RIGHT,
    section_up: int = DEFAULT_SUBTITLE_SECTION_UP,
    section_left: int = DEFAULT_SUBTITLE_SECTION_LEFT,
    menu_down: int | None = None,
) -> None:
    """Configure Prime Video subtitles on the current playback."""
    lang = (language or "off").strip().lower()
    # Accept "off" or "off:<preferred-lang>" (preferred used for UP count to Off).
    if lang.startswith("off:"):
        enabled = False
    else:
        enabled = lang not in ("off", "none", "disabled")
    await apply_subtitles(
        client,
        enabled=enabled,
        language=lang if enabled else (lang if lang.startswith("off") else "off"),
        delay=delay,
        focus_down=focus_down,
        focus_right=focus_right,
        section_up=section_up,
        section_left=section_left,
        menu_down=menu_down,
    )


async def cmd_media_stop(client: "WebOsClient") -> None:
    """Stop playback — remote keys first (Prime), SSAP fallback."""
    print("Stopping playback...")
    sent = False

    if await _prefer_remote_keys(client):
        # BACK x2 exits Prime player → detail/browse; EXIT/STOP as fallback.
        for key in ("BACK", "BACK", "EXIT", "STOP"):
            if await _send_button(client, key):
                sent = True
                await asyncio.sleep(PLAY_KEY_DELAY)
        try:
            result = await client.close()
            if result.get("returnValue", True):
                sent = True
                print("  media.viewer/close succeeded.", file=sys.stderr)
        except Exception as exc:
            print(f"  media.viewer/close: {exc}", file=sys.stderr)
    else:
        try:
            result = await client.stop()
            if result.get("returnValue", True):
                sent = True
        except Exception as exc:
            print(f"  media.controls/stop: {exc}", file=sys.stderr)
        if await _send_button(client, "STOP"):
            sent = True

    if not sent:
        print("error: could not stop TV playback", file=sys.stderr)
        sys.exit(1)
    print("Stopped.")


async def cmd_seek(
    client: "WebOsClient",
    seconds: float,
    content_id: str | None = None,
    episode: int | None = None,
    profile: int | None = None,
) -> None:
    """Seek to an absolute position.

    Tries, in order of reliability for the Prime Video native app:
      1. Re-launch Prime Video with an ?autoplay=1&t=<pos> contentTarget deep
         link (the same mechanism the play path uses — actually moves Prime's
         player to <pos>)
      2. SSAP media.controls/seek  (LG built-in player / non-Prime fallback)
      3. Open the Prime Video detail page in the browser with ?autoplay=1&t=N

    NOTE: SSAP seek is tried *after* the re-launch for Prime, because Prime's
    player ignores SSAP seek yet the call can falsely report success — which
    previously made the seek silently do nothing. The old {"startTime": N}
    launch param is likewise ignored by Prime, so we use ?t=<pos> instead.
    """
    pos = max(0, int(seconds))
    print(f"[SEEK] === Seeking to {pos}s for content_id={content_id} episode={episode} ===", file=sys.stderr)

    # ── Method 1: Re-launch native app at ?t=<pos> (Prime) ───────────────────
    # Use the same launch-ID resolution as the play path (GTI / playbackURL /
    # episode detail IDs) so seeking works for series and saved resume points.
    if content_id:
        # Use the resolver (same as play) with start=pos so it picks best ID (GTI, ASIN, episode, playback target)
        # and forces the t= on it. This is consistent and uses the best launch target.
        try:
            candidates = resolve_prime_launch_ids(
                content_id,
                episode=episode,
                autoplay=True,
                start=pos,
            )
            launch_id = candidates[0] if candidates else f"/detail/{content_id}?autoplay=1&t={pos}"
            print(f"[SEEK] using resolver, launch_id: {launch_id}", file=sys.stderr)
        except Exception as exc:
            print(f"[SEEK] resolver failed, falling back: {exc}", file=sys.stderr)
            launch_id = f"{PRIME_DEEP_LINK_BASE}/detail/{content_id}?autoplay=1&t={pos}"
        print(f"[SEEK] FINAL launch_id for contentTarget: {launch_id}", file=sys.stderr)
        # For explicit seek-to-position (especially resume-from-bookmark cases), always
        # cold-start (close+relaunch) to ensure the ?t= override is applied by the
        # Prime player rather than any in-memory resume position winning.
        print(f"[SEEK] cold relaunch with t={pos}s (ensures t= applies on resume items) ...", file=sys.stderr)
        if await close_app(client, PRIME_VIDEO_APP_ID):
            await asyncio.sleep(1.2)
        result = await launch_prime_content(
            client, launch_id, cold_start=False  # already closed above
        )
        print(f"[SEEK] launch result: {result}", file=sys.stderr)
        if result.get("returnValue"):
            used_autoplay_launch = _is_autoplay_target(launch_id)
            if not used_autoplay_launch:
                await start_playback(
                    client,
                    delay=2.0,
                    used_autoplay_launch=False,
                )

            await asyncio.sleep(7.0)

            ssap_succeeded = False
            try:
                ssap = await client.request("ssap://media.controls/seek", {"position": pos})
                if ssap.get("returnValue"):
                    ssap_succeeded = True
                    print(json.dumps({"seeked_to": pos, "success": True, "method": "relaunch+ssap"}))
            except Exception:
                pass

            if not ssap_succeeded:
                print(
                    json.dumps(
                        {
                            "seeked_to": pos,
                            "success": True,
                            "method": "relaunch",
                            "launch_id": launch_id,
                        }
                    )
                )
            return
        print(f"[SEEK] Re-launch returned failure: {result}", file=sys.stderr)

        # Fallback identifiers for browser/SSAP paths (relaunch path returned early on success).
        seek_id = content_id
    else:
        seek_id = None

    # ── Method 2: SSAP seek (LG built-in player / non-Prime) ─────────────────
    print(f"[SEEK] falling back to pure SSAP seek (no content_id or launch failed)", file=sys.stderr)
    try:
        result = await client.request("ssap://media.controls/seek", {"position": pos})
        if result.get("returnValue"):
            print(json.dumps({"seeked_to": pos, "success": True, "method": "ssap"}))
            return
        print(f"  SSAP seek returned: {result}", file=sys.stderr)
    except Exception as exc:
        print(f"  SSAP seek unavailable: {exc}", file=sys.stderr)

    # ── Method 3: Browser deeplink with ?autoplay=1&t=N ──────────────────────
    # Opens the Prime Video website in the LG browser; less seamless but reliable.
    if seek_id:
        print(f"[SEEK] Trying browser deeplink with t={pos} ...", file=sys.stderr)
        try:
            url = f"https://app.primevideo.com/detail/{seek_id}?autoplay=1&t={pos}"
            result = await client.launch_app_with_params(
                PRIME_BROWSER_APP_ID,
                {"target": url},
            )
            print(json.dumps({"seeked_to": pos, "success": result.get("returnValue", False), "method": "browser"}))
            return
        except Exception as exc:
            print(f"  Browser deeplink failed: {exc}", file=sys.stderr)

    print(json.dumps({"seeked_to": pos, "success": False, "method": "none"}))
    print(f"[SEEK] === ALL METHODS FAILED for pos={pos} ===", file=sys.stderr)


def cmd_resume_position(content_id: str, episode: int | None = None) -> None:
    """Return the saved resume offset for a title (no TV connection needed)."""
    content_id = content_id.strip()
    print(f"[RESUME-POS] input content_id={content_id} episode={episode}", file=sys.stderr)
    html = _fetch_prime_html(content_id)
    play_id = content_id
    if episode is not None and episode >= 1:
        try:
            resolved = resolve_episode_content_id(html, content_id, episode=episode)
            if resolved and resolved != content_id:
                play_id = resolved
                html = _fetch_prime_html(play_id)
                print(f"[RESUME-POS] resolved episode page play_id={play_id}", file=sys.stderr)
        except Exception as exc:
            print(f"[RESUME-POS] resolve_episode failed: {exc}", file=sys.stderr)
    print(f"[RESUME-POS] fetching resume from play_id={play_id}", file=sys.stderr)
    seconds = resume_start_seconds_from_html(html, play_id)
    print(f"[RESUME-POS] resume_start_seconds_from_html returned: {seconds}", file=sys.stderr)
    # Extra diagnostics: did we see any resume-like patterns?
    has_resume_label = any(lbl in html for lbl in ("Resume", "Continue watching", "Continue Watching"))
    has_time_offset = "timeOffsetInSeconds" in html or "resumeTime" in html
    print(f"[RESUME-POS] html signals: has_resume_label={has_resume_label} has_time_offset={has_time_offset} html_len={len(html)}", file=sys.stderr)
    print(json.dumps({"position": seconds}))


async def cmd_get_position(client: "WebOsClient") -> None:
    """Try to get current playback position and duration from the TV."""
    # Try multiple SSAP endpoints — availability depends on WebOS version / app
    # Prime titles commonly return nulls here; raw attempts are logged by _playback_position too.
    candidates = [
        ("ssap://com.webos.service.media.player/getInfo", {}),
        ("ssap://com.webos.service.cepswm.media.player/getInfo", {}),
        ("ssap://media.infoAction.getInfoPerApp", {"id": "amazon"}),
        ("ssap://com.webos.service.media.player/getPlayInfo", {}),
        ("ssap://media/getPlaybackState", {}),
    ]
    for uri, payload in candidates:
        try:
            result = await client.request(uri, payload)
            # Debug the raw response for Prime troubleshooting
            try:
                print(f"[GET-POS] {uri} -> rv={result.get('returnValue')} sample={json.dumps({k:result.get(k) for k in list(result.keys())[:5]})}", file=sys.stderr)
            except Exception:
                pass
            if not result.get("returnValue"):
                continue
            position = (
                result.get("currentTime")
                or result.get("position")
                or result.get("mediaCurrentTime")
                or result.get("playTime")
            )
            duration = (
                result.get("duration")
                or result.get("totalTime")
                or result.get("mediaDuration")
            )
            print(json.dumps({
                "position": float(position) if position is not None else None,
                "duration": float(duration) if duration is not None else None,
            }))
            return
        except Exception as exc:
            print(f"[GET-POS] {uri} exc: {exc}", file=sys.stderr)
            continue
    # Fallback: position unavailable
    print(json.dumps({"position": None, "duration": None}))


async def cmd_volume_get(client: "WebOsClient") -> None:
    """Print current volume and mute state as a single JSON line."""
    volume = await client.get_volume()
    muted  = await client.get_muted()
    print(json.dumps({"volume": volume, "muted": bool(muted)}))


async def cmd_volume_set(client: "WebOsClient", level: int) -> None:
    """Set absolute volume via SSAP; fall back to key presses."""
    level = max(0, min(100, level))

    # ── Method 1: SSAP setVolume (instantaneous) ──────────────────────────
    try:
        result = await client.set_volume(level)
        if result.get("returnValue"):
            # set_volume succeeded → the TV will be at `level`. Reading the volume
            # back immediately races on a just-woken TV (it can briefly still
            # report the previous level, e.g. show 8 right after we set 13), so
            # trust the requested value rather than a stale read-back.
            print(json.dumps({"volume": level, "muted": False}))
            return
        print(f"  set_volume SSAP: {result}", file=sys.stderr)
    except Exception as exc:
        print(f"  set_volume SSAP failed: {exc}", file=sys.stderr)

    # ── Method 2: key presses to reach the target ─────────────────────────
    try:
        current = int(await client.get_volume() or 50)
        delta = level - current
        if delta != 0:
            key = "VOLUMEUP" if delta > 0 else "VOLUMEDOWN"
            for _ in range(min(abs(delta), 50)):
                await client.button(key)
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.15)
    except Exception as exc:
        print(f"  volume key press failed: {exc}", file=sys.stderr)

    try:
        vol_now = await client.get_volume()
        print(json.dumps({"volume": vol_now, "muted": False}))
    except Exception:
        print(json.dumps({"volume": level, "muted": False}))


async def cmd_volume_step(client: "WebOsClient", direction: str, steps: int) -> None:
    """Change volume by N steps — key presses first, SSAP as fallback."""
    steps = max(1, steps)
    key = "VOLUMEUP" if direction == "up" else "VOLUMEDOWN"

    # Key presses are the most reliable method across all LG WebOS versions
    try:
        for _ in range(steps):
            await client.button(key)
            await asyncio.sleep(0.08)
        await asyncio.sleep(0.12)
    except Exception as exc:
        print(f"  volume key press failed: {exc}; trying SSAP", file=sys.stderr)
        # SSAP fallback
        try:
            fn = client.volume_up if direction == "up" else client.volume_down
            for _ in range(steps):
                await fn()
                await asyncio.sleep(0.08)
        except Exception as exc2:
            print(f"  SSAP volume step also failed: {exc2}", file=sys.stderr)

    try:
        vol_now = await client.get_volume()
        muted   = await client.get_muted()
        print(json.dumps({"volume": vol_now, "muted": bool(muted)}))
    except Exception:
        print(json.dumps({"volume": None, "muted": False}))


async def cmd_set_mute(client: "WebOsClient", muted: bool) -> None:
    """Toggle mute — MUTE key press is the most reliable method."""

    # Read current state so we can avoid double-toggling
    current_muted: bool | None = None
    try:
        val = await client.get_muted()
        current_muted = bool(val) if val is not None else None
    except Exception:
        pass

    # Only send the key if state needs to change (or if we can't tell)
    if current_muted is None or current_muted != bool(muted):
        # Try SSAP set_mute first (clean, no toggle ambiguity)
        ssap_ok = False
        try:
            result = await client.set_mute(muted)
            ssap_ok = bool(result.get("returnValue"))
        except Exception as exc:
            print(f"  set_mute SSAP: {exc}", file=sys.stderr)

        if not ssap_ok:
            # MUTE key is a physical toggle — only send if state still differs
            try:
                check = await client.get_muted()
                if bool(check) != bool(muted):
                    await client.button("MUTE")
                    await asyncio.sleep(0.25)
            except Exception:
                # Last resort: just send it
                await client.button("MUTE")
                await asyncio.sleep(0.25)

    try:
        muted_now = await client.get_muted()
        vol_now   = await client.get_volume()
        print(json.dumps({"volume": vol_now, "muted": bool(muted_now)}))
    except Exception:
        print(json.dumps({"volume": None, "muted": bool(muted)}))


async def cmd_media_pause(client: "WebOsClient") -> None:
    """Pause playback via SSAP and the PAUSE key (works for Prime and built-in players)."""
    print("Pausing playback...")
    sent = False

    try:
        result = await client.pause()
        if result.get("returnValue", True):
            sent = True
    except Exception as exc:
        print(f"  media.controls/pause: {exc}", file=sys.stderr)

    await asyncio.sleep(PLAY_KEY_DELAY)
    if await _send_button(client, "PAUSE"):
        sent = True

    if not sent:
        print("error: could not pause TV playback", file=sys.stderr)
        sys.exit(1)
    print("Paused.")


async def cmd_media_resume(client: "WebOsClient") -> None:
    """Resume playback via SSAP and the PLAY key."""
    print("Resuming playback...")
    sent = False

    try:
        result = await client.play()
        if result.get("returnValue", True):
            sent = True
    except Exception as exc:
        print(f"  media.controls/play: {exc}", file=sys.stderr)

    await asyncio.sleep(PLAY_KEY_DELAY)
    if await _send_button(client, "PLAY"):
        sent = True

    if not sent:
        print("error: could not resume TV playback", file=sys.stderr)
        sys.exit(1)
    print("  Resumed.")


async def cmd_media_toggle(client: "WebOsClient") -> None:
    """Toggle play/pause by sending the PLAY key (acts as toggle on WebOS)."""
    print("Toggling play/pause...")
    if not await _send_button(client, "PLAY"):
        print("error: could not toggle TV playback", file=sys.stderr)
        sys.exit(1)
    print("  Sent PLAY (toggle).")


async def cmd_media_skip(client: "WebOsClient", direction: str, steps: int = 1) -> None:
    """Skip backward/forward during playback (remote REWIND / FASTFORWARD).

    Prefer dedicated WebOS methods when available; fall back to remote keys.
    One step is one remote press (typically ~10s on Prime). Absolute seek
    (``--seek``) is still used for the scrubber / typed time.
    """
    direction = (direction or "").strip().lower()
    if direction not in {"back", "backward", "rewind", "forward", "ff"}:
        print(f"error: unknown skip direction: {direction!r}", file=sys.stderr)
        sys.exit(2)
    going_back = direction in {"back", "backward", "rewind"}
    steps = max(1, min(int(steps), 12))
    label = "rewind" if going_back else "fast-forward"
    print(f"Skipping {label} ×{steps} ...")

    sent = 0
    for i in range(steps):
        ok = False
        try:
            if going_back and hasattr(client, "rewind"):
                await client.rewind()
                ok = True
            elif not going_back and hasattr(client, "fast_forward"):
                await client.fast_forward()
                ok = True
        except Exception as exc:
            print(f"  {label} method failed ({exc}); trying key.", file=sys.stderr)
        if not ok:
            key = "REWIND" if going_back else "FASTFORWARD"
            ok = await _send_button(client, key)
        if ok:
            sent += 1
            await asyncio.sleep(0.2)
        else:
            break

    if sent == 0:
        print(f"error: could not {label} on TV", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"skip": label, "steps": sent}))


_MAC_RE = re.compile(
    r"\b([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})\b",
)


def normalize_mac(mac: str) -> str:
    """Normalize a MAC to AA:BB:CC:DD:EE:FF uppercase."""
    parts = re.split(r"[:-]", mac.strip())
    if len(parts) != 6:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return ":".join(p.zfill(2) for p in parts).upper()


def discover_tv_mac(ip: str) -> str | None:
    """Resolve the TV MAC from the local ARP/neighbor table (TV must be reachable)."""
    if platform.system() == "Darwin":
        ping = ["ping", "-c", "1", "-W", "1000", ip]
        arp_cmd = ["arp", "-n", ip]
    else:
        ping = ["ping", "-c", "1", "-W", "1", ip]
        arp_cmd = ["ip", "neigh", "show", ip]

    subprocess.run(ping, capture_output=True, text=True)
    result = subprocess.run(arp_cmd, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    match = _MAC_RE.search(output)
    if not match:
        return None
    try:
        return normalize_mac(match.group(1))
    except ValueError:
        return None


def send_wol(mac: str, broadcast: str = "255.255.255.255") -> None:
    """Send a Wake-on-LAN magic packet to the TV."""
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(cleaned) != 12:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    mac_bytes = bytes.fromhex(cleaned)
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, 9))


def _power_state_is_on(state: dict | None) -> bool:
    if not state:
        return False
    return state.get("state") not in {
        None,
        "Power Off",
        "Suspend",
        "Active Standby",
    }


async def cmd_power_state(client: "WebOsClient") -> None:
    """Print TV power state as JSON."""
    state = await client.get_power_state()
    if isinstance(state, dict):
        await client.set_power_state(state)
    print(json.dumps({
        "on": _power_state_is_on(state if isinstance(state, dict) else None),
        "state": state,
    }))


async def cmd_power_off(client: "WebOsClient") -> None:
    """Power off the TV (requires an active network connection)."""
    from aiowebostv import endpoints as ep

    print("Powering off TV...")
    state_before = await client.get_power_state()
    print(f"  Power state before: {state_before}", file=sys.stderr)

    if isinstance(state_before, dict):
        await client.set_power_state(state_before)

    if not _power_state_is_on(state_before if isinstance(state_before, dict) else None):
        print("  TV already off.", file=sys.stderr)
        print(json.dumps({"ok": True, "action": "power_off", "already_off": True}))
        return

    # Always send turnOff — aiowebostv.power_off() can skip if is_on is stale.
    await client.command("request", ep.POWER_OFF)
    print("  Sent system/turnOff.", file=sys.stderr)
    await asyncio.sleep(1.0)
    try:
        state_after = await client.get_power_state()
    except Exception:
        state_after = {"state": "unknown"}
    print(json.dumps({
        "ok": True,
        "action": "power_off",
        "state_before": state_before,
        "state_after": state_after,
    }))


async def cmd_power_on(ip: str, tv_mac: str | None = None) -> None:
    """Wake the TV (WoL optional) and turn it on via SSAP."""
    from aiowebostv import endpoints as ep

    if not tv_mac:
        tv_mac = discover_tv_mac(ip)
        if tv_mac:
            print(f"  Discovered TV MAC {tv_mac} from ARP table.", file=sys.stderr)

    if tv_mac:
        print(f"  Sending Wake-on-LAN to {tv_mac}...", file=sys.stderr)
        try:
            send_wol(tv_mac)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        await asyncio.sleep(8.0)

    client = await connect(ip)
    try:
        print("Powering on TV...")
        state = await client.get_power_state()
        print(f"  Power state: {state}", file=sys.stderr)

        if not client.tv_state.is_on:
            result = await client.power_on()
            print(f"  system/turnOn: {result}", file=sys.stderr)
            await asyncio.sleep(2.0)
            state = await client.get_power_state()
        elif not client.tv_state.is_screen_on:
            result = await client.request(ep.TURN_ON_SCREEN)
            print(f"  turnOnScreen: {result}", file=sys.stderr)
            await asyncio.sleep(1.0)
            state = await client.get_power_state()

        print(json.dumps({
            "ok": True,
            "action": "power_on",
            "state": state,
            "wol_sent": bool(tv_mac),
            "mac": tv_mac,
        }))
    finally:
        await client.disconnect()
        release_tv_ssap_lock()


async def main() -> None:
    args = parse_args()

    if args.list_profiles:
        print(format_profiles_table())
        return

    if args.profile_save:
        if args.profile is None:
            print("error: --profile-save requires --profile", file=sys.stderr)
            sys.exit(2)
        try:
            save_type = args.profile_type or "adult"
            entry = upsert_profile(
                args.profile_save,
                index=args.profile,
                profile_type=save_type,
                row=args.profile_row,
                pin=args.profile_pin,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        print(
            f"Saved {save_type} profile {entry.name!r} "
            f"→ index {entry.index}, row {entry.row}"
        )
        return

    if args.list_episodes:
        if not args.content_id:
            print("error: --list-episodes requires --content-id", file=sys.stderr)
            sys.exit(2)
        try:
            html = _fetch_prime_html(args.content_id)
            episodes = list_episodes_from_html(html, season_content_id=args.content_id)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"error: could not list episodes ({exc})", file=sys.stderr)
            print("[]")
            sys.exit(1)
        print(json.dumps([
            {
                "content_id": ep.get("content_id"),
                "gti": ep.get("gti"),
                "sequence_number": ep.get("sequence_number"),
                "title": ep.get("title"),
                "runtime_min": ep.get("runtime_min"),
            }
            for ep in episodes
        ]))
        return

    if args.resume_position:
        if not args.content_id:
            print("error: --resume-position requires --content-id", file=sys.stderr)
            sys.exit(2)
        try:
            cmd_resume_position(args.content_id, episode=args.episode)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"error: could not read resume position ({exc})", file=sys.stderr)
            print(json.dumps({"position": None}))
            sys.exit(1)
        return

    profile_name = args.profile_name
    if (
        args.launch == PRIME_VIDEO_APP_ID
        and args.profile is None
        and profile_name is None
        and not args.list_profiles
        and not args.profile_save
    ):
        saved = list_profiles()
        if len(saved) == 1:
            _ptype, entry = saved[0]
            profile_name = entry.name
            print(
                f"  Using saved profile {entry.name!r} "
                f"(picker {saved[0][0]}, index {entry.index})",
                file=sys.stderr,
            )

    if args.get_mac:
        mac = discover_tv_mac(args.ip)
        print(json.dumps({"mac": mac}))
        return

    if args.power_on:
        await cmd_power_on(args.ip, tv_mac=args.tv_mac)
        return

    client = await connect(args.ip)
    try:
        if args.content_id and not (args.launch or args.seek is not None or args.resume_position or args.get_position or args.list_episodes):
            print("error: --content-id requires --launch (or is only valid with seek/resume/get/list)", file=sys.stderr)
            sys.exit(2)
        if args.profile is not None and args.profile < 0:
            print("error: --profile must be >= 0", file=sys.stderr)
            sys.exit(2)
        if args.profile is None and args.profile_name is None and args.profile_highlight:
            print("error: --profile-highlight requires --profile or --profile-name", file=sys.stderr)
            sys.exit(2)
        if args.profile is None and args.profile_name is None and args.launch == PRIME_VIDEO_APP_ID:
            if args.content_id or args.profile_pin:
                pass  # content launch without profile still allowed in some flows
        if args.profile_name and args.profile_save:
            print("error: use --profile-save alone to add mappings", file=sys.stderr)
            sys.exit(2)
        if args.profile_row < 0:
            print("error: --profile-row must be >= 0", file=sys.stderr)
            sys.exit(2)
        if args.profile_type_right is not None and args.profile_type_right < 0:
            print("error: --profile-type-right must be >= 0", file=sys.stderr)
            sys.exit(2)
        if args.profile_step_delay < 0:
            print("error: --profile-step-delay must be >= 0", file=sys.stderr)
            sys.exit(2)
        if args.play_method not in PRIME_PLAY_METHODS:
            print(f"error: --play-method must be one of {PRIME_PLAY_METHODS}", file=sys.stderr)
            sys.exit(2)
        if args.episode is not None and args.episode < 1:
            print("error: --episode must be >= 1", file=sys.stderr)
            sys.exit(2)
        for name in ("play_focus_up", "play_focus_down", "play_focus_left"):
            if getattr(args, name) < 0:
                print(f"error: --{name.replace('_', '-')} must be >= 0", file=sys.stderr)
                sys.exit(2)
        if args.subtitle_focus_down < 0:
            print("error: --subtitle-focus-down must be >= 0", file=sys.stderr)
            sys.exit(2)
        if args.subtitle_focus_right < -1:
            print("error: --subtitle-focus-right must be >= -1", file=sys.stderr)
            sys.exit(2)
        if args.subtitle_section_up < 0:
            print("error: --subtitle-section-up must be >= 0", file=sys.stderr)
            sys.exit(2)
        if args.subtitle_section_left < 0:
            print("error: --subtitle-section-left must be >= 0", file=sys.stderr)
            sys.exit(2)
        if args.subtitle_menu_down < -1:
            print("error: --subtitle-menu-down must be >= -1", file=sys.stderr)
            sys.exit(2)
        subtitle_menu_down = (
            None if args.subtitle_menu_down < 0 else args.subtitle_menu_down
        )
        if args.launch:
            await cmd_launch(
                client,
                args.launch,
                content_id=args.content_id,
                profile=args.profile,
                profile_name=profile_name,
                profile_delay=args.profile_delay,
                profile_row=args.profile_row,
                profile_type=args.profile_type,
                profile_type_right=args.profile_type_right,
                profile_step_delay=args.profile_step_delay,
                profile_pin=args.profile_pin,
                profile_pin_delay=args.profile_pin_delay,
                profile_highlight=args.profile_highlight,
                content_delay=args.content_delay,
                play=args.play,
                play_delay=args.play_delay,
                play_method=args.play_method,
                play_focus_up=args.play_focus_up,
                play_focus_down=args.play_focus_down,
                play_focus_left=args.play_focus_left,
                play_highlight=args.play_highlight,
                browser=args.browser,
                try_all_ids=args.try_all_ids,
                close_after_profile=args.close_after_profile,
                skip_entitlement_check=args.skip_entitlement_check,
                episode=args.episode,
                title=args.title,
                start=int(args.start) if args.start else 0,
                set_subtitles=args.set_subtitles,
                subtitle_delay=args.subtitle_delay,
                subtitle_focus_down=args.subtitle_focus_down,
                subtitle_focus_right=args.subtitle_focus_right,
                subtitle_section_up=args.subtitle_section_up,
                subtitle_section_left=args.subtitle_section_left,
                subtitle_menu_down=subtitle_menu_down,
            )
        elif args.set_subtitles is not None:
            await cmd_set_subtitles(
                client,
                language=args.set_subtitles,
                delay=args.subtitle_delay,
                focus_down=args.subtitle_focus_down,
                focus_right=args.subtitle_focus_right,
                section_up=args.subtitle_section_up,
                section_left=args.subtitle_section_left,
                menu_down=subtitle_menu_down,
            )
        elif args.media_pause:
            await cmd_media_pause(client)
        elif args.media_play:
            await cmd_media_resume(client)
        elif args.media_toggle:
            await cmd_media_toggle(client)
        elif args.media_stop:
            await cmd_media_stop(client)
        elif args.media_skip_back is not None:
            await cmd_media_skip(client, "back", args.media_skip_back)
        elif args.media_skip_forward is not None:
            await cmd_media_skip(client, "forward", args.media_skip_forward)
        elif args.power_off:
            await cmd_power_off(client)
        elif args.power_state:
            await cmd_power_state(client)
        elif args.volume_get:
            await cmd_volume_get(client)
        elif args.volume_set is not None:
            await cmd_volume_set(client, args.volume_set)
        elif args.volume_up is not None:
            await cmd_volume_step(client, "up", args.volume_up)
        elif args.volume_down is not None:
            await cmd_volume_step(client, "down", args.volume_down)
        elif args.mute:
            await cmd_set_mute(client, True)
        elif args.unmute:
            await cmd_set_mute(client, False)
        elif args.seek is not None:
            await cmd_seek(
                client,
                args.seek,
                content_id=args.content_id,
                episode=args.episode,
                profile=args.profile,
            )
        elif args.get_position:
            await cmd_get_position(client)
        elif args.apps:
            await cmd_apps(client)
        elif args.detect_amazoff:
            await cmd_detect_amazoff(client)
        else:
            await cmd_info(client)
    finally:
        await client.disconnect()
        release_tv_ssap_lock()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ValueError as exc:
        # Expected user-input errors (e.g. unknown profile name) — show a clean
        # message instead of a traceback.
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)

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
    title_type_for_content_id,
)

if TYPE_CHECKING:
    from aiowebostv import WebOsClient

KEY_FILE = Path.home() / ".lg-tv-key"
DEFAULT_IP = "192.168.0.79"
PRIME_VIDEO_APP_ID = "amazon"
PRIME_BROWSER_APP_ID = "com.webos.app.browser"
DEFAULT_PROFILE_DELAY = 8.0
DEFAULT_CONTENT_DELAY = 6.0
DEFAULT_PLAY_DELAY = 8.0
DEFAULT_PLAY_FOCUS_UP = 5
DEFAULT_PLAY_FOCUS_DOWN = 2
DEFAULT_PLAY_FOCUS_LEFT = 2
MIN_PROFILE_DELAY = 3.0
PROFILE_KEY_DELAY = 0.35
DEFAULT_PIN_DELAY = 2.0
DEFAULT_PROFILE_STEP_DELAY = 3.0
PLAY_KEY_DELAY = 0.45
PRIME_PLAY_METHODS = ("auto", "media", "watch", "enter")
PRIME_PROFILE_TYPES = ("adult", "kid", "none")
PRIME_DETAIL_ID_RE = re.compile(r"^[0-9A-Z]{26,32}$")
PRIME_ASIN_RE = re.compile(r"^B[A-Z0-9]{8,10}$")
PRIME_GTI_RE = re.compile(r"^amzn1\.dv\.gti\.[0-9a-f-]+$", re.I)
PRIME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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
    """Connect and pair with TV, returning a connected client."""
    from aiowebostv import WebOsClient, WebOsTvPairError

    key = load_key()
    client = WebOsClient(ip, client_key=key)

    print(f"Connecting to {ip}:3000 ...")
    if not key:
        print("  No saved key — TV will show a pairing prompt. Accept it now.")

    try:
        await asyncio.wait_for(client.connect(), timeout=DEFAULT_CONNECT_TIMEOUT)
    except WebOsTvPairError:
        await _safe_disconnect(client)
        print("  ERROR: Pairing rejected. Accept the prompt on the TV and retry.")
        sys.exit(1)
    except (asyncio.TimeoutError, TimeoutError):
        await _safe_disconnect(client)
        print(
            f"  ERROR: Timed out connecting to {ip}:3000 "
            f"(>{DEFAULT_CONNECT_TIMEOUT:g}s)",
            file=sys.stderr,
        )
        _print_connect_troubleshooting(ip)
        sys.exit(1)
    except Exception as exc:
        await _safe_disconnect(client)
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


def _prime_detail_url(content_id: str) -> str:
    return f"https://www.primevideo.com/detail/{content_id}"


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


def _prime_target_uses_params(target: str) -> bool:
    return (
        target.startswith("/detail/")
        or target.startswith("primevideo://")
        or "autoplay=" in target
    )


def _prime_target_with_start_offset(target: str, pos: int) -> str:
    """Return a Prime autoplay deep link that starts playback at `pos` seconds.

    Prime Video on webOS honours the ``?t=<seconds>`` query param of the
    contentTarget deep link (the same mechanism the normal play path uses with
    ``t=0``). The plain ``{"startTime": N}`` launch param, by contrast, is
    ignored by Prime's player — which made seeking silently do nothing.
    """
    if not _prime_target_uses_params(target):
        # Bare content / detail ID → build the detail deep link used for play.
        return f"/detail/{target}?autoplay=1&t={pos}"
    # Already a deep link: set/replace t= and ensure autoplay is requested.
    if re.search(r"[?&]t=\d+", target):
        target = re.sub(r"([?&]t=)\d+", rf"\g<1>{pos}", target)
    else:
        target = f"{target}{'&' if '?' in target else '?'}t={pos}"
    if "autoplay=" not in target:
        target = f"{target}&autoplay=1"
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
    if PRIME_GTI_RE.match(content_id) or PRIME_ASIN_RE.match(content_id):
        return [content_id]

    candidates: list[str] = []
    seen: set[str] = set()
    if not PRIME_DETAIL_ID_RE.match(content_id):
        return [content_id]

    page_html = html
    try:
        if page_html is None:
            page_html = _fetch_prime_html(content_id)

        title_type = title_type_for_content_id(page_html, content_id)
        episodes = list_episodes_from_html(page_html, season_content_id=content_id)
        use_episode = (
            episode is not None
            or prefer_episode
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

        if autoplay:
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
                if autoplay_target:
                    _append_launch_id(candidates, seen, autoplay_target)
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
    except ValueError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  Warning: could not resolve GTI/ASIN ({exc}); using detail ID only.")
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
        return await client.launch_app_with_content_id(app_id, content_id)
    print(f"Launching {app_id} ...")
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
    if cold_start:
        if await close_app(client, PRIME_VIDEO_APP_ID):
            await asyncio.sleep(1.5)
    if _prime_target_uses_params(content_id):
        print(f"Launching {PRIME_VIDEO_APP_ID} (contentTarget={content_id}) ...")
        return await client.launch_app_with_params(
            PRIME_VIDEO_APP_ID,
            {"contentTarget": content_id},
        )
    return await launch_app(client, PRIME_VIDEO_APP_ID, content_id=content_id)


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
        result = await launch_prime_content(
            client, launch_id, cold_start=cold_start
        )
        _check_launch_result(result)
        # A Prime content deep link (contentId / GTI / contentTarget) auto-plays
        # on the TV. Report autoplay=True so start_playback never sends an extra
        # key — doing so would land on the playing video and pause it.
        return launch_id, True

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
    # As above: the deep link auto-plays, so suppress any follow-up keypress.
    return last_id, True


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
    if profile_name:
        resolved_type, entry = resolve_profile_name(
            profile_name,
            profile_type=profile_type,
        )
        row = profile_row if profile_row else entry.row
        pin = profile_pin or entry.pin
        return entry.index, row, pin, entry.name, resolved_type
    if profile is None:
        raise ValueError("either --profile or --profile-name is required")
    return profile, profile_row, profile_pin, None, profile_type or "adult"


async def select_profile(
    client: "WebOsClient",
    profile: int,
    *,
    delay: float = DEFAULT_PROFILE_DELAY,
    row: int = 0,
    profile_type: str = "adult",
    profile_type_right: int | None = None,
    profile_step_delay: float = DEFAULT_PROFILE_STEP_DELAY,
    pin: str | None = None,
    pin_delay: float = DEFAULT_PIN_DELAY,
    highlight_only: bool = False,
    profile_display_name: str | None = None,
) -> None:
    """Pick a Prime Video profile on the name picker (after optional type step).

    Newer Prime builds ask for profile type (Adult/Kids) first, then profile name.
    Use --profile-type adult and --profile 0 for the first adult name. Legacy
    single-screen pickers can use --profile-type none.
    """
    if profile < 0:
        raise ValueError("profile must be >= 0")
    if row < 0:
        raise ValueError("profile-row must be >= 0")
    if profile_type not in PRIME_PROFILE_TYPES:
        raise ValueError(
            f"profile-type must be one of {PRIME_PROFILE_TYPES}, not {profile_type!r}"
        )

    row_note = f", row {row}" if row else ""
    label = profile_display_name or f"profile {profile}"
    action = "highlighting" if highlight_only else "selecting"
    print(
        f"  Waiting {delay:.1f}s for profile picker, then {action} "
        f"{label!r} (index {profile}{row_note}) ..."
    )
    await asyncio.sleep(delay)

    await select_profile_type(
        client,
        profile_type,
        type_right=profile_type_right,
        highlight_only=highlight_only and profile_type != "none",
    )
    if profile_type != "none" and not highlight_only and profile_step_delay > 0:
        print(f"  Waiting {profile_step_delay:.1f}s for profile name picker ...")
        await asyncio.sleep(profile_step_delay)

    for _ in range(row):
        await client.button("DOWN")
        await asyncio.sleep(PROFILE_KEY_DELAY)

    for _ in range(profile):
        await client.button("RIGHT")
        await asyncio.sleep(PROFILE_KEY_DELAY)

    if highlight_only:
        print(
            f"  {label!r} highlighted (no ENTER). "
            "Check the TV, then rerun without --profile-highlight."
        )
        return

    await client.button("ENTER")
    print(f"  {label!r} selected.")

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
    """Start playback on a Prime title screen without navigating away."""
    labels = play_labels or []
    resolved_method, note = _prime_playback_plan(labels, method=method)

    if resolved_method == "blocked":
        # The catalog entitlement check is unsigned and explicitly unreliable
        # ("your signed-in Prime account may differ"), so it must not hard-block
        # playback. Warn, but still press the title page's primary button and let
        # the TV decide: a subscribed profile shows Watch/Resume (plays); an
        # unentitled one shows Join Prime / Rent (simply won't start).
        print(f"  Note: {note}", file=sys.stderr)
        print(
            "  Pressing the title's primary button anyway — the TV is the source "
            "of truth. If it offers Join Prime or Rent, playback will not start.",
            file=sys.stderr,
        )
        resolved_method, note = "enter", None

    if used_autoplay_launch:
        # The autoplay=1 deeplink already starts the player. Sending any extra
        # ENTER / PLAY / media key here lands on the *already-playing* video and
        # toggles it straight to PAUSE ("plays for a few seconds then pauses"),
        # so we stop here and let it run uninterrupted.
        print("  Launched with autoplay=1; letting the player start on its own (no extra keys).")
        return

    if delay > 0:
        detail = f" ({note})" if note else ""
        print(
            f"  Waiting {delay:.1f}s for title page, then starting playback "
            f"({resolved_method}{detail}) ..."
        )
        await asyncio.sleep(delay)

    if resolved_method == "media":
        # SSAP play only works once the player is already open.
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
        # The Prime detail page focuses its primary Watch/Resume button by
        # default, so ENTER resumes immediately. The UP/DOWN/LEFT focus dance is
        # only needed for the uncertain "watch" fallback — here it overshoots up
        # into the top-nav profile avatar, which reopens the profile picker and
        # never starts playback. Select the already-focused button directly.
        note_detail = f" ({note})" if note else ""
        if play_highlight:
            print(
                f"  Watch/Resume button should be focused by default{note_detail}; "
                "rerun without --play-highlight to press ENTER "
                "(use --play-method watch if it is not focused)."
            )
        else:
            await client.button("ENTER")
            print(f"  Sent ENTER on focused Watch/Resume button{note_detail}.")
    else:
        raise ValueError(f"unknown play method: {resolved_method}")


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
    start: int = 0,
) -> None:
    profile_display_name: str | None = None
    effective_profile_type = profile_type or "adult"
    detail_html: str | None = None
    used_autoplay_launch = False
    if content_id and not skip_entitlement_check:
        detail_html = fetch_prime_detail_html(content_id)
        if detail_html:
            report_prime_entitlement(content_id, html=detail_html)

    if browser:
        if not content_id:
            print("error: --browser requires --content-id", file=sys.stderr)
            sys.exit(2)
        result = await launch_prime_browser(client, content_id)
        _check_launch_result(result)
        if play:
            await start_playback(client, delay=play_delay, method=play_method)
        print("  Done.")
        return

    if profile_name is not None:
        if profile is not None:
            print(
                "  Note: --profile-name overrides --profile index.",
                file=sys.stderr,
            )
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

    if profile_delay < MIN_PROFILE_DELAY:
        print(
            f"  Warning: --profile-delay {profile_delay:g}s is short; "
            f"try >= {DEFAULT_PROFILE_DELAY:g}s if profile selection misses.",
            file=sys.stderr,
        )

    if profile is not None and content_id is not None:
        if close_after_profile:
            # Legacy: close resets the profile session on many Prime builds.
            print("  Prime flow: launch → profile → close → cold content launch")
            result = await launch_app(client, PRIME_VIDEO_APP_ID)
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
            if content_delay > 0:
                print(f"  Waiting {content_delay:.1f}s after profile selection ...")
                await asyncio.sleep(content_delay)
            _, used_autoplay_launch = await launch_prime_content_candidates(
                client,
                content_id,
                try_all_ids=try_all_ids,
                cold_start=False,
                detail_html=detail_html,
                episode=episode,
                prefer_episode=play or episode is not None,
                autoplay=play,
                start=start,
            )
        else:
            # This Prime build re-shows the profile picker whenever it receives
            # a content deep link. So deep-link the title FIRST and let the
            # picker act as a gate: once the profile is chosen, Prime lands
            # straight on the title page. (Selecting the profile first and
            # deep-linking afterwards just bounces back to the picker.)
            print("  Prime flow: content deep link → profile gate → title page")
            _, used_autoplay_launch = await launch_prime_content_candidates(
                client,
                content_id,
                try_all_ids=try_all_ids,
                cold_start=False,
                detail_html=detail_html,
                episode=episode,
                prefer_episode=play or episode is not None,
                # Normally this flow deep-links without autoplay and lets the
                # profile picker gate playback. When a start offset is requested
                # we need the autoplay ?t=<pos> deep link so Prime begins at that
                # position once the profile is chosen.
                autoplay=(start or 0) > 0,
                start=start,
            )
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
            if content_delay > 0:
                print(f"  Waiting {content_delay:.1f}s for the title page ...")
                await asyncio.sleep(content_delay)
    elif content_id is not None:
        _, used_autoplay_launch = await launch_prime_content_candidates(
            client,
            content_id,
            try_all_ids=try_all_ids,
            cold_start=False,
            detail_html=detail_html,
            episode=episode,
            prefer_episode=play or episode is not None,
            autoplay=play,
            start=start,
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
            if profile_highlight:
                print("  Done (profile highlight only).")
                return
    else:
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
        else:
            await start_playback(
                client,
                delay=play_delay,
                method=play_method,
                play_labels=play_labels,
                used_autoplay_launch=used_autoplay_launch,
                play_focus_up=play_focus_up,
                play_focus_down=play_focus_down,
                play_focus_left=play_focus_left,
                play_highlight=play_highlight,
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
    start: int = 0,
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
            start=start,
        )
        return

    if browser:
        print("error: --browser is only supported for Prime Video (amazon)", file=sys.stderr)
        sys.exit(2)

    result = await launch_app(client, app_id, content_id=content_id)
    _check_launch_result(result)
    profile_display_name: str | None = None
    effective_profile_type = profile_type or "adult"
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
    parser.add_argument("--launch", metavar="APP_ID", help="Launch an app by ID")
    parser.add_argument(
        "--content-id",
        metavar="ID",
        help="Optional contentId passed to the launched app (e.g. Prime ASIN, YouTube v=...)",
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
            "Prime profile picker step 1: adult, kid, or none for legacy single-screen "
            "pickers (default adult with --profile; auto from --profile-name when saved)"
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
    print(f"Seeking to {pos}s ...")

    # For a series/season, resolve the specific episode's detail ID so the seek
    # deep link targets the episode being watched — otherwise it would relaunch
    # the series landing page instead of moving the episode's player.
    seek_id = content_id
    if content_id and episode is not None and episode >= 1:
        try:
            html = fetch_prime_detail_html(content_id)
            if html:
                resolved = resolve_episode_content_id(html, content_id, episode=episode)
                if resolved and resolved != content_id:
                    print(f"  Resolved episode {episode} → {resolved}")
                    seek_id = resolved
        except (OSError, ValueError) as exc:
            print(f"  Warning: could not resolve episode {episode} ({exc})", file=sys.stderr)

    # ── Method 1: Re-launch native app at ?t=<pos> (Prime) ───────────────────
    # Amazon Prime Video on LG WebOS honours the ?t=<seconds> query param of the
    # contentTarget deep link — the exact mechanism the normal play path uses
    # (with t=0). The older {"startTime": N} launch param is ignored by Prime's
    # player, which made the seek silently do nothing.
    if seek_id:
        target = _prime_target_with_start_offset(seek_id, pos)
        print(f"  Re-launching at t={pos}s (contentTarget={target}) ...")
        try:
            if await close_app(client, PRIME_VIDEO_APP_ID):
                await asyncio.sleep(1.5)

            result = await client.launch_app_with_params(
                PRIME_VIDEO_APP_ID, {"contentTarget": target}
            )
            if result.get("returnValue"):
                print(json.dumps({"seeked_to": pos, "success": True, "method": "relaunch"}))
                return
            print(f"  Re-launch returned: {result}", file=sys.stderr)
        except Exception as exc:
            print(f"  Re-launch failed: {exc}", file=sys.stderr)

    # ── Method 2: SSAP seek (LG built-in player / non-Prime) ─────────────────
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
        print(f"  Trying browser deeplink with t={pos} ...")
        try:
            url = f"https://www.primevideo.com/detail/{seek_id}?autoplay=1&t={pos}"
            result = await client.launch_app_with_params(
                PRIME_BROWSER_APP_ID,
                {"target": url},
            )
            print(json.dumps({"seeked_to": pos, "success": result.get("returnValue", False), "method": "browser"}))
            return
        except Exception as exc:
            print(f"  Browser deeplink failed: {exc}", file=sys.stderr)

    print(json.dumps({"seeked_to": pos, "success": False, "method": "none"}))


async def cmd_get_position(client: "WebOsClient") -> None:
    """Try to get current playback position and duration from the TV."""
    # Try multiple SSAP endpoints — availability depends on WebOS version / app
    candidates = [
        ("ssap://com.webos.service.media.player/getInfo", {}),
        ("ssap://com.webos.service.cepswm.media.player/getInfo", {}),
        ("ssap://media.infoAction.getInfoPerApp", {"id": "amazon"}),
    ]
    for uri, payload in candidates:
        try:
            result = await client.request(uri, payload)
            if not result.get("returnValue"):
                continue
            position = (
                result.get("currentTime")
                or result.get("position")
                or result.get("mediaCurrentTime")
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
        except Exception:
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
        if args.content_id and not args.launch:
            print("error: --content-id requires --launch", file=sys.stderr)
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
                start=int(args.start) if args.start else 0,
            )
        elif args.media_pause:
            await cmd_media_pause(client)
        elif args.media_play:
            await cmd_media_resume(client)
        elif args.media_toggle:
            await cmd_media_toggle(client)
        elif args.media_stop:
            await cmd_media_stop(client)
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
            await cmd_seek(client, args.seek, content_id=args.content_id, episode=args.episode)
        elif args.get_position:
            await cmd_get_position(client)
        elif args.apps:
            await cmd_apps(client)
        else:
            await cmd_info(client)
    finally:
        await client.disconnect()


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

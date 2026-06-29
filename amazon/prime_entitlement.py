"""Scrape Prime Video pages for rent/buy/Prime/channel inclusion status."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

BASE_URL = "https://www.primevideo.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DETAIL_ID_RE = re.compile(r"^[0-9A-Z]{26,32}$")
GTI_RE = re.compile(r"^amzn1\.dv\.gti\.[0-9a-f-]+$", re.I)
JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)
RENT_TEXT_RE = re.compile(
    r'(?:Rent(?:ing)?(?:\s+(?:movie|Movie))?\s*(?:[A-Z]{2,3})?\s*|[\'"])'
    r"([£$€][0-9]+(?:\.[0-9]{2})?)",
    re.I,
)
BUY_TEXT_RE = re.compile(
    r"(?:Buy|Purchasing)(?:\s+(?:movie|Movie))?\s*(?:[A-Z]{2,3})?\s*([£$€][0-9]+(?:\.[0-9]{2})?)",
    re.I,
)
CHANNEL_TRIAL_RE = re.compile(
    r"(\d+[- ]day free trial)[^<]{0,160}?([£$€][0-9]+(?:\.[0-9]{2})?)\s*/\s*month",
    re.I,
)
PRICE_IN_LABEL_RE = re.compile(r"([£$€][0-9]+(?:\.[0-9]{2})?)")
BENEFIT_ID_NAMES = {
    "Prime": "Prime",
    "lionsgatepluses": "Lionsgate+",
    "flixolees": "flixolé",
    "skyshowtimees": "SkyShowtime",
    "maxes": "Max",
    "hbomaxes": "HBO Max",
    "atresplayeres": "Atresplayer",
}


@dataclass
class PrimeEntitlement:
    content_id: str
    title: str | None = None
    gti: str | None = None
    entitlement_type: str | None = None
    included_with_prime: bool = False
    included_with_channel: str | None = None
    rent_from: str | None = None
    buy_from: str | None = None
    channel: str | None = None
    channel_note: str | None = None
    focus_message: str | None = None
    glance_message: str | None = None
    summary: str = ""
    prime_catalog: bool = False
    watchable_free: bool = False
    unsigned_catalog: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def detail_url(content_id: str) -> str:
    if content_id.startswith("http"):
        match = re.search(r"/detail/([^/?#]+)", content_id)
        content_id = match.group(1) if match else content_id
    return f"{BASE_URL}/detail/{content_id}"


def fetch_detail_html(
    content_id: str,
    *,
    locale: str = "en-GB,en;q=0.9",
    timeout: float = 20.0,
) -> str:
    url = detail_url(content_id)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": locale},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not fetch {url}: {exc.reason}") from exc


def parse_json_blobs(html: str) -> list[object]:
    blobs: list[object] = []
    for match in JSON_SCRIPT_RE.finditer(html):
        try:
            blobs.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return blobs


def _dedupe_prices(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _lowest_price(values: list[str]) -> str | None:
    prices = _dedupe_prices(values)
    return prices[0] if prices else None


def _title_from_html(html: str) -> str | None:
    match = re.search(r"<title>Prime Video:\s*([^<]+)</title>", html)
    return match.group(1).strip() if match else None


def _gti_from_html(html: str, *, content_id: str | None = None) -> str | None:
    if content_id:
        if resolved := resolve_gti_for_content_id(html, content_id):
            return resolved
    match = re.search(r'"gti":"(amzn1\.dv\.gti\.[^"]+)"', html)
    return match.group(1) if match else None


def _prime_link_content_id(link: object) -> str | None:
    if not isinstance(link, str):
        return None
    match = re.search(r"/detail/([^/?#]+)", link)
    return match.group(1) if match else None


def resolve_gti_for_content_id(html: str, content_id: str) -> str | None:
    """Return the GTI that belongs to a specific Prime detail ID."""
    content_id = content_id.strip()

    def walk(node: object) -> str | None:
        if isinstance(node, dict):
            gti = node.get("gti")
            link_id = _prime_link_content_id(node.get("link"))
            if link_id == content_id and isinstance(gti, str) and gti.startswith("amzn1.dv.gti."):
                return gti
            if node.get("compactGTI") == content_id and isinstance(gti, str):
                return gti
            for value in node.values():
                if found := walk(value):
                    return found
        elif isinstance(node, list):
            for value in node:
                if found := walk(value):
                    return found
        return None

    for blob in parse_json_blobs(html):
        if found := walk(blob):
            return found

    for match in re.finditer(re.escape(content_id) + r".{0,400}", html):
        if gti_match := re.search(r'"gti":"(amzn1\.dv\.gti\.[^"]+)"', match.group(0)):
            return gti_match.group(1)
    return None


def resolve_asins_for_content_id(html: str, content_id: str) -> list[str]:
    """Return Amazon ASINs associated with a Prime detail ID."""
    content_id = content_id.strip()
    found: list[str] = []

    def collect_asins(node: object) -> None:
        if not isinstance(node, dict):
            return
        link_id = _prime_link_content_id(node.get("link"))
        matches = link_id == content_id or node.get("compactGTI") == content_id
        if matches:
            asins = node.get("asins")
            if isinstance(asins, list):
                for asin in asins:
                    if isinstance(asin, str) and re.fullmatch(r"B[A-Z0-9]{8,10}", asin):
                        found.append(asin)
            asin = node.get("asin")
            if isinstance(asin, str) and re.fullmatch(r"B[A-Z0-9]{8,10}", asin):
                found.append(asin)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            collect_asins(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for blob in parse_json_blobs(html):
        walk(blob)

    return _dedupe_prices(found)


def title_type_for_content_id(html: str, content_id: str) -> str | None:
    """Return Prime titleType for a detail ID (movie, season, episode, etc.)."""
    content_id = content_id.strip()
    title_type: str | None = None

    def walk(node: object) -> None:
        nonlocal title_type
        if not isinstance(node, dict):
            return
        link_id = _prime_link_content_id(node.get("link"))
        if link_id == content_id or node.get("compactGTI") == content_id:
            value = node.get("titleType")
            if isinstance(value, str) and value.strip():
                title_type = value.strip()
        for value in node.values():
            walk(value)

    for blob in parse_json_blobs(html):
        walk(blob)
    return title_type


def _episode_runtime_min(node: dict) -> int | None:
    """Episode runtime in whole minutes from a Prime episode node.

    Prime stores runtime the same way as the catalog entity: an int/float that
    is already in MINUTES, or a string like "54 min" / "1 h 2 min". Some nodes
    instead expose explicit seconds/millis fields, which we convert."""
    raw = node.get("runtime")
    if raw is None:
        raw = node.get("runtimeMinutes")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        total = 0
        h_match = re.search(r"(\d+)\s*h", text)
        m_match = re.search(r"(\d+)\s*min", text)
        if h_match:
            total += int(h_match.group(1)) * 60
        if m_match:
            total += int(m_match.group(1))
        if total > 0:
            return total
        if text.isdigit():
            return int(text)
    for key, divisor in (
        ("runtimeSeconds", 60),
        ("durationSeconds", 60),
        ("runtimeMillis", 60000),
        ("durationMillis", 60000),
    ):
        v = node.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return max(1, int(v / divisor))
    return None


def list_episodes_from_html(
    html: str,
    *,
    season_content_id: str | None = None,
) -> list[dict]:
    """Return episode detail IDs/GTIs from a season or episode page."""
    episodes: list[dict] = []
    seen: set[str] = set()
    # Prime splits an episode's data across two node shapes: a "link" node that
    # carries the detail ID + GTI but no metadata, and a richer node that has
    # title/runtime/duration keyed by episode number but no link/GTI. Collect the
    # metadata here and merge it onto the linked episodes by sequence number, so
    # the seek bar gets a real episode runtime instead of None.
    meta_by_seq: dict[int, dict] = {}

    def _seq_of(node: dict) -> int | None:
        seq = node.get("sequenceNumber") or node.get("episodeNumber")
        if isinstance(seq, bool):
            return None
        return int(seq) if isinstance(seq, (int, float)) else None

    def walk(node: object) -> None:
        if isinstance(node, list):
            for value in node:
                walk(value)
            return
        if not isinstance(node, dict):
            return
        if node.get("titleType") != "episode":
            for value in node.values():
                walk(value)
            return

        seq_num = _seq_of(node)

        # Record any metadata this node carries, even when it lacks the link/GTI
        # needed to launch the episode (the rich node has no link).
        if seq_num is not None:
            rt = _episode_runtime_min(node)
            title = node.get("title") or node.get("displayTitle")
            meta = meta_by_seq.setdefault(seq_num, {})
            if rt and not meta.get("runtime_min"):
                meta["runtime_min"] = rt
            if title and not meta.get("title"):
                meta["title"] = title

        content_id = _prime_link_content_id(node.get("link")) or node.get("compactGTI")
        gti = node.get("gti")
        if not isinstance(content_id, str) or content_id in seen:
            for value in node.values():
                walk(value)
            return
        if not isinstance(gti, str) or not gti.startswith("amzn1.dv.gti."):
            for value in node.values():
                walk(value)
            return

        seen.add(content_id)
        asins = node.get("asins") if isinstance(node.get("asins"), list) else []
        episodes.append(
            {
                "content_id": content_id,
                "gti": gti,
                "sequence_number": seq_num,
                "title": node.get("title") or node.get("displayTitle"),
                "runtime_min": _episode_runtime_min(node),
                "asins": [
                    asin
                    for asin in asins
                    if isinstance(asin, str) and re.fullmatch(r"B[A-Z0-9]{8,10}", asin)
                ],
            }
        )
        for value in node.values():
            walk(value)

    for blob in parse_json_blobs(html):
        walk(blob)

    # Backfill title/runtime from the metadata nodes for episodes captured from a
    # bare link node.
    for ep in episodes:
        meta = meta_by_seq.get(ep.get("sequence_number"))
        if not meta:
            continue
        if ep.get("runtime_min") is None and meta.get("runtime_min"):
            ep["runtime_min"] = meta["runtime_min"]
        if not ep.get("title") and meta.get("title"):
            ep["title"] = meta["title"]

    episodes.sort(key=lambda item: item.get("sequence_number") or 999)
    return episodes


def _clean_label(label: str) -> str:
    """Strip Amazon rich-text markup tokens like {conditionalLineBreak}{bold}…{end}."""
    cleaned = re.sub(r"\{[^}]*\}", "", label)
    # Collapse whitespace left behind by removed tokens.
    return re.sub(r"\s+", " ", cleaned).strip()


def _action_label(action: dict) -> str | None:
    presentation = action.get("presentation")
    if isinstance(presentation, dict):
        primary_label = presentation.get("primaryLabel")
        if isinstance(primary_label, str) and _clean_label(primary_label):
            return _clean_label(primary_label)
    payload = action.get("payload")
    if isinstance(payload, dict):
        for key in ("playback", "transaction", "subscription"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                label = nested.get("label")
                if isinstance(label, str) and _clean_label(label):
                    return _clean_label(label)
    return None


def playback_labels_from_html(html: str, content_id: str) -> list[str]:
    """Return user-visible play/watch labels from a Prime detail page buybox."""
    content_id = content_id.strip()
    gti = resolve_gti_for_content_id(html, content_id)
    buybox = _find_buybox_in_blobs(parse_json_blobs(html), gti)
    labels: list[str] = []

    def collect_actions(actions: object) -> None:
        if not isinstance(actions, list):
            return
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_type = action.get("actionType")
            label = _action_label(action)
            if label:
                labels.append(label)
            payload = action.get("payload")
            if isinstance(payload, dict):
                expanding = payload.get("expandingCard")
                if isinstance(expanding, dict):
                    collect_actions(expanding.get("actions"))

    if isinstance(buybox, dict):
        collect_actions(buybox.get("primaryActions"))
        collect_actions(buybox.get("secondaryActions"))

    return _dedupe_prices(labels)


def has_watchable_play_button(labels: list[str]) -> bool:
    joined = " ".join(labels).lower()
    if "trailer" in joined and "watch now" not in joined and "resume" not in joined:
        for label in labels:
            lowered = label.lower()
            if "trailer" in lowered:
                continue
            if any(marker in lowered for marker in ("watch now", "resume", "play movie", "play episode")):
                return True
        return False
    return any(
        any(marker in label.lower() for marker in ("watch now", "resume", "play movie", "play episode"))
        or (label.lower().startswith("watch") and "trailer" not in label.lower())
        for label in labels
    )


def _unescape_prime_url(url: str) -> str:
    return (
        url.replace("\\u0026", "&")
        .replace("\\u003d", "=")
        .replace("\\u003f", "?")
        .replace("\\/", "/")
    )


def _entitlement_is_channel_only(html: str, content_id: str) -> bool:
    """True when a title is watchable only through a Prime Video channel add-on
    (e.g. MGM+, Qello Concerts) — not included with Prime and not a free /
    Prime-catalog title. Used to avoid auto-selecting a channel subscription
    when launching playback."""
    try:
        ent = parse_entitlement(html, content_id=content_id.strip())
    except Exception:
        # If entitlement can't be resolved, don't block the existing behaviour.
        return False
    return (
        bool(ent.included_with_channel)
        and not ent.included_with_prime
        and not ent.prime_catalog
    )


def playback_launch_target_from_html(html: str, content_id: str) -> str | None:
    """Return a Prime contentTarget that requests autoplay when Watch now is available."""
    content_id = content_id.strip()
    # Never auto-select a channel add-on (MGM+, Qello Concerts, etc.). When a
    # title is only available through a channel subscription, decline autoplay
    # so the caller falls back to opening the detail page for a manual choice.
    if _entitlement_is_channel_only(html, content_id):
        return None
    for match in re.finditer(r'"label":"Watch now"', html):
        segment = html[match.start() : match.start() + 5000]
        if '"isTrailer":true' in segment:
            continue
        url_match = re.search(r'"playbackURL":"((?:\\.|[^"\\])*)"', segment)
        if not url_match:
            continue
        playback_url = _unescape_prime_url(url_match.group(1))
        if "autoplay=1" in playback_url:
            return playback_url
    labels = playback_labels_from_html(html, content_id)
    if has_watchable_play_button(labels):
        return f"/detail/{content_id}?autoplay=1&t=0"
    return None


def resolve_episode_content_id(
    html: str,
    content_id: str,
    *,
    episode: int | None = None,
) -> str:
    """Return the episode detail ID when launching a season with --episode."""
    title_type = title_type_for_content_id(html, content_id)
    if title_type not in {"season", "series"}:
        return content_id
    episodes = list_episodes_from_html(html, season_content_id=content_id)
    if not episodes:
        return content_id
    if episode is not None:
        if episode < 1 or episode > len(episodes):
            raise ValueError(
                f"episode {episode} out of range; this title has {len(episodes)} episode(s)"
            )
        selected = episodes[episode - 1]
    else:
        selected = episodes[0]
    return selected.get("content_id") or content_id


def _message_from_cue(cue: object) -> str | None:
    if isinstance(cue, dict):
        msg = cue.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        dv = cue.get("dvMessage")
        if isinstance(dv, dict):
            string = dv.get("string")
            if isinstance(string, str) and string.strip():
                return string.strip()
    return None


def _channel_from_provider_logo(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"benefit-id/(?:[^/]+/)*([^/]+)/logos/", url)
    if not match:
        return None
    benefit = match.group(1)
    if benefit in {"Prime", "amazon"}:
        return "Prime"
    return BENEFIT_ID_NAMES.get(benefit, benefit)


def _channel_from_provider_alt(alt_text: str | None) -> str | None:
    if not alt_text:
        return None
    normalized = alt_text.strip().lower()
    if normalized in {"prime video", "prime"}:
        return "Prime"
    for benefit_id, name in BENEFIT_ID_NAMES.items():
        if normalized == name.lower() or normalized == benefit_id.lower():
            return name
    if normalized.endswith("+"):
        return alt_text.strip()
    return None


def _provider_logo_url(provider_logo: object) -> str | None:
    if not isinstance(provider_logo, dict):
        return None
    for key in ("imageUrl", "image", "url"):
        value = provider_logo.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _channel_from_benefit_id(benefit_id: str | None) -> str | None:
    if not benefit_id or benefit_id in {"Prime", "amazon"}:
        return None
    return BENEFIT_ID_NAMES.get(benefit_id, benefit_id)


def _prices_from_html(html: str) -> tuple[str | None, str | None]:
    rent_prices = RENT_TEXT_RE.findall(html)
    buy_prices = BUY_TEXT_RE.findall(html)
    if not rent_prices:
        rent_prices = re.findall(
            r"Rent(?:\s+[A-Z]{2,3})?\s*([£$€][0-9]+(?:\.[0-9]{2})?)",
            html,
        )
    if not buy_prices:
        buy_prices = re.findall(
            r"Buy(?:\s+[A-Z]{2,3})?\s*([£$€][0-9]+(?:\.[0-9]{2})?)",
            html,
        )
    return _lowest_price(rent_prices), _lowest_price(buy_prices)


def _trial_from_text(text: str) -> str | None:
    match = CHANNEL_TRIAL_RE.search(text)
    if match:
        return f"{match.group(1)}, then {match.group(2)}/month"
    return None


def _messages_indicate_prime_inclusion(*messages: str | None) -> bool:
    joined = " ".join(m for m in messages if m).lower()
    prime_markers = (
        "included with prime",
        "included with your prime",
        "watch with prime",
        "free with prime",
        "stream free with prime",
        "prime member",
    )
    if any(marker in joined for marker in prime_markers):
        if "trial" in joined and "included with prime" not in joined:
            return False
        return True
    return False


def _messages_indicate_rent_or_buy(*messages: str | None) -> bool:
    joined = " ".join(m for m in messages if m).lower()
    if not joined:
        return False
    # Carousel glance text — appears on Prime-catalog titles, not rent/buy storefront.
    if "free trial or buy" in joined:
        return False
    rent_buy_markers = (
        "available to rent",
        "available to buy",
        "rent from",
        "buy from",
        "to rent",
        "to buy",
        "purchase for",
        "purchase ",
    )
    return any(marker in joined for marker in rent_buy_markers)


def _messages_indicate_channel_addon(*messages: str | None) -> bool:
    joined = " ".join(m for m in messages if m).lower()
    if not joined:
        return False
    channel_markers = (
        "subscribe to hbo",
        "subscribe to max",
        "subscribe to sky",
        "subscribe to lionsgate",
        "subscribe to atres",
        "subscribe to flix",
        "hbo max channel",
        "skyshowtime channel",
    )
    return any(marker in joined for marker in channel_markers)


def _messages_indicate_prime_subscription_offer(
    *messages: str | None,
    compact_focus_message: str | None = None,
) -> bool:
    """True when cues advertise Prime membership streaming (not rent/buy)."""
    if _messages_indicate_rent_or_buy(*messages):
        return False
    if _messages_indicate_channel_addon(*messages):
        return False

    compact = (compact_focus_message or "").lower()
    if compact:
        if "free trial of prime" in compact:
            return True
        if "auto-renews at" in compact or "auto renews at" in compact:
            return True

    joined = " ".join(m for m in messages if m).lower()
    if not joined:
        return False

    # Prime membership renewal on collection cards (regional copy may omit "Prime").
    if "after trial" in joined and (
        "auto-renews at" in joined or "auto renews at" in joined
    ):
        return True

    strong_focus_markers = (
        "watch with a",
        "watch with prime",
        "included with prime",
        "included with your prime",
        "free with prime",
        "stream free with prime",
        "first episode free",
    )
    if any(marker in joined for marker in strong_focus_markers):
        return True

    # Bare "auto-renews at …" on search cards is often a generic Prime upsell
    # shown even on rent/buy titles; only trust it with compactFocusMessage.
    return False


def _provider_logo_indicates_prime_subscription(provider_logo: object) -> bool:
    if not isinstance(provider_logo, dict):
        return False
    link = provider_logo.get("link")
    if isinstance(link, str) and "/storefront/subscription/prime" in link:
        return True
    alt_text = provider_logo.get("altText")
    if isinstance(alt_text, str) and alt_text.strip().lower() in {"prime video", "prime"}:
        image = provider_logo.get("image") or provider_logo.get("imageUrl")
        if isinstance(image, str) and "/benefit-id/" in image and "/Prime/" in image:
            return True
    return False


def _infer_prime_membership_catalog(
    *,
    focus_message: str | None,
    glance_message: str | None,
    compact_focus_message: str | None = None,
    rent_from: str | None = None,
    buy_from: str | None = None,
    channel: str | None = None,
    provider_logo: object | None = None,
) -> bool:
    """True for films/series in the base Prime membership catalog (not rent/buy)."""
    if rent_from or buy_from:
        return False
    if channel and channel != "Prime":
        return False

    messages = (focus_message, glance_message, compact_focus_message)
    if _messages_indicate_rent_or_buy(*messages):
        return False
    if _messages_indicate_channel_addon(*messages):
        return False
    if _messages_indicate_prime_subscription_offer(
        *messages, compact_focus_message=compact_focus_message
    ):
        return True
    if _messages_indicate_prime_inclusion(*messages):
        return True
    if channel == "Prime" and _provider_logo_indicates_prime_subscription(provider_logo):
        return True
    return False


def _benefit_id_from_actions(actions: object) -> str | None:
    if not isinstance(actions, list):
        return None
    for action in actions:
        if not isinstance(action, dict):
            continue
        payload = action.get("payload")
        if not isinstance(payload, dict):
            continue
        for key in ("subscription", "expandingCard"):
            nested = payload.get(key)
            if not isinstance(nested, dict):
                continue
            if key == "expandingCard":
                for card_action in nested.get("actions") or []:
                    found = _benefit_id_from_actions([card_action])
                    if found:
                        return found
                continue
            benefit_id = nested.get("benefitId")
            if isinstance(benefit_id, str) and benefit_id.strip():
                return benefit_id.strip()
    return None


def _prices_from_buybox_actions(actions: object) -> tuple[str | None, str | None]:
    rent_prices: list[str] = []
    buy_prices: list[str] = []

    def visit(action: object) -> None:
        if not isinstance(action, dict):
            return
        payload = action.get("payload")
        if isinstance(payload, dict):
            transaction = payload.get("transaction")
            if isinstance(transaction, dict):
                for label_key in ("label", "text"):
                    label = transaction.get(label_key)
                    if not isinstance(label, str):
                        continue
                    lowered = label.lower()
                    for price in PRICE_IN_LABEL_RE.findall(label):
                        if "rent" in lowered:
                            rent_prices.append(price)
                        elif "buy" in lowered or "purchase" in lowered:
                            buy_prices.append(price)
            expanding = payload.get("expandingCard")
            if isinstance(expanding, dict):
                for card_action in expanding.get("actions") or []:
                    visit(card_action)
        presentation = action.get("presentation")
        if isinstance(presentation, dict):
            primary_label = presentation.get("primaryLabel")
            if isinstance(primary_label, str):
                lowered = primary_label.lower()
                for price in PRICE_IN_LABEL_RE.findall(primary_label):
                    if "rent" in lowered:
                        rent_prices.append(price)
                    elif "buy" in lowered or "purchase" in lowered:
                        buy_prices.append(price)

    if isinstance(actions, list):
        for action in actions:
            visit(action)

    return _lowest_price(rent_prices), _lowest_price(buy_prices)


def _channel_offer_from_messages(
    *,
    channel: str | None,
    focus_message: str | None,
    glance_message: str | None,
) -> bool:
    if not channel or channel == "Prime":
        return False
    joined = " ".join(m for m in (focus_message, glance_message) if m).lower()
    channel_lower = channel.lower()
    markers = (
        f"free trial of {channel_lower}",
        f"trial of {channel_lower}",
        f"free trial to {channel_lower}",
        f"subscribe to {channel_lower}",
        f"included with {channel_lower}",
        f"watch with {channel_lower}",
        f"{channel_lower} channel",
    )
    return any(marker in joined for marker in markers)


def entitlement_from_cues(
    cues: dict,
    *,
    html: str | None = None,
    benefit_id: str | None = None,
    is_prime_catalog: bool | None = None,
    primary_actions: list | None = None,
) -> dict:
    """Derive entitlement fields from a Prime entitlementCues object."""
    entitlement_type = cues.get("entitlementType")
    if isinstance(entitlement_type, str):
        entitlement_type = entitlement_type.strip() or None

    focus_message = _message_from_cue(cues.get("focusMessage"))
    glance_message = _message_from_cue(cues.get("glanceMessage"))
    compact_focus_message = _message_from_cue(cues.get("compactFocusMessage"))
    provider_logo = cues.get("providerLogo")
    logo_url = _provider_logo_url(provider_logo)
    alt_text = provider_logo.get("altText") if isinstance(provider_logo, dict) else None
    action_benefit_id = _benefit_id_from_actions(primary_actions)
    channel = (
        _channel_from_provider_logo(logo_url)
        or _channel_from_provider_alt(alt_text)
        or _channel_from_benefit_id(benefit_id)
        or _channel_from_benefit_id(action_benefit_id)
    )

    channel_note = None
    for text in (focus_message, glance_message, compact_focus_message):
        if text:
            channel_note = _trial_from_text(text) or channel_note

    included_with_prime = entitlement_type == "Entitled"
    included_with_channel = None

    if _messages_indicate_prime_inclusion(
        focus_message, glance_message, compact_focus_message
    ):
        included_with_prime = True

    if entitlement_type == "Entitled" and channel and channel != "Prime":
        included_with_prime = False
        included_with_channel = channel
    elif channel and channel != "Prime" and not included_with_prime:
        joined = " ".join(
            m for m in (focus_message, glance_message, compact_focus_message) if m
        ).lower()
        if f"included with {channel.lower()}" in joined:
            included_with_channel = channel
        elif _channel_offer_from_messages(
            channel=channel,
            focus_message=focus_message,
            glance_message=glance_message,
        ) or action_benefit_id:
            included_with_channel = channel

    rent_from = buy_from = None
    if primary_actions:
        rent_from, buy_from = _prices_from_buybox_actions(primary_actions)
    if html and (rent_from is None or buy_from is None):
        html_rent, html_buy = _prices_from_html(html)
        rent_from = rent_from or html_rent
        buy_from = buy_from or html_buy

    prime_catalog = _infer_prime_membership_catalog(
        focus_message=focus_message,
        glance_message=glance_message,
        compact_focus_message=compact_focus_message,
        rent_from=rent_from,
        buy_from=buy_from,
        channel=channel,
        provider_logo=provider_logo,
    )

    return {
        "entitlement_type": entitlement_type,
        "included_with_prime": included_with_prime,
        "included_with_channel": included_with_channel,
        "prime_catalog": prime_catalog,
        "rent_from": rent_from,
        "buy_from": buy_from,
        "channel": channel,
        "channel_note": channel_note,
        "focus_message": focus_message,
        "glance_message": glance_message,
    }


def _content_id_from_entity_link(node: dict) -> str | None:
    link = node.get("link")
    if not isinstance(link, dict):
        return None
    url = link.get("url")
    if not isinstance(url, str):
        return None
    match = re.search(r"/detail/([^/?#]+)", url)
    return match.group(1) if match else None


def _find_buybox_in_blobs(blobs: list[object], gti: str | None) -> dict | None:
    if not gti:
        return None

    candidates: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict) and gti in node:
            entry = node[gti]
            if isinstance(entry, dict) and (
                isinstance(entry.get("messages"), dict)
                or entry.get("isPrime") is not None
                or entry.get("primaryActions")
            ):
                candidates.append(entry)
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for blob in blobs:
        walk(blob)

    if not candidates:
        return None

    merged: dict = {}
    for entry in candidates:
        for key, value in entry.items():
            if key not in merged or merged[key] in (None, {}, []):
                merged[key] = value
            elif key == "isPrime" and value is True:
                merged[key] = True

    if not isinstance(merged.get("messages"), dict):
        return None
    return merged


def _find_entity_cues_in_blobs(
    blobs: list[object],
    *,
    title: str | None,
    gti: str | None,
    content_id: str | None = None,
) -> dict | None:
    matches: list[tuple[int, dict]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            node_title = node.get("displayTitle") or node.get("title")
            node_gti = node.get("titleID") or node.get("gti") or node.get("impressionId")
            cues = node.get("entitlementCues")
            if isinstance(cues, dict):
                score = 0
                if content_id and _content_id_from_entity_link(node) == content_id:
                    score += 4
                if title and node_title == title:
                    score += 3
                if gti and node_gti and str(node_gti) == gti:
                    score += 2
                elif gti and node_gti and gti in str(node_gti):
                    score += 1
                if score:
                    matches.append((score, cues))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for blob in blobs:
        walk(blob)

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def _find_gti_cues_in_html(html: str, gti: str) -> dict:
    """Parse atf/detail cues embedded next to the title GTI."""
    fields: dict = {}
    for match in re.finditer(re.escape(gti) + r".{0,2500}", html):
        chunk = match.group(0)
        if ent := re.search(r'"entitlementType":"([^"]+)"', chunk):
            fields["entitlement_type"] = ent.group(1)
        if benefit := re.search(r'"benefitId":"([^"]+)"', chunk):
            fields["benefit_id"] = benefit.group(1)
        for msg_match in re.finditer(r'"message":"([^"]*)"', chunk):
            message = msg_match.group(1).strip()
            if not message:
                continue
            if "focus_message" not in fields:
                fields["focus_message"] = message
            elif "glance_message" not in fields:
                fields["glance_message"] = message
    return fields


def _build_summary(
    *,
    included_with_prime: bool,
    included_with_channel: str | None,
    prime_catalog: bool = False,
    rent_from: str | None,
    buy_from: str | None,
    channel: str | None,
    channel_note: str | None,
    entitlement_type: str | None,
    focus_message: str | None,
) -> str:
    if included_with_prime:
        return "Included with Prime"

    if prime_catalog and entitlement_type != "Entitled":
        parts = ["Available with Prime subscription"]
        if channel_note:
            parts.append(f"({channel_note})")
        if rent_from:
            parts.append(f"Rent from {rent_from}")
        if buy_from:
            parts.append(f"Buy from {buy_from}")
        return " / ".join(parts)

    if included_with_channel:
        if entitlement_type == "Entitled":
            label = f"Included with {included_with_channel} (Prime Video channel)"
        else:
            label = f"Available with {included_with_channel} (Prime Video channel)"
        parts = [label]
        if rent_from:
            parts.append(f"Rent from {rent_from}")
        if buy_from:
            parts.append(f"Buy from {buy_from}")
        return " / ".join(parts)

    parts: list[str] = []
    if channel and channel != "Prime":
        if channel_note:
            parts.append(f"{channel} channel ({channel_note})")
        else:
            parts.append(f"{channel} channel")

    if rent_from:
        parts.append(f"Rent from {rent_from}")
    if buy_from:
        parts.append(f"Buy from {buy_from}")

    if parts:
        return " / ".join(parts)

    if focus_message:
        return focus_message

    if entitlement_type == "Unentitled":
        return "Not included with Prime (purchase/channel required)"
    if entitlement_type:
        return entitlement_type
    return "Availability unknown"


def parse_entitlement(
    html: str,
    *,
    content_id: str,
    title: str | None = None,
    entitlement_cues: dict | None = None,
) -> PrimeEntitlement:
    title = title or _title_from_html(html)
    gti = _gti_from_html(html, content_id=content_id)
    blobs = parse_json_blobs(html)

    buybox = _find_buybox_in_blobs(blobs, gti)
    primary_actions = None
    benefit_id = None
    cues = entitlement_cues

    if buybox:
        primary_actions = buybox.get("primaryActions")
        buybox_title = buybox.get("title")
        if isinstance(buybox_title, str) and buybox_title.strip():
            title = buybox_title.strip()
        cues = buybox.get("messages")
        benefit_id = _benefit_id_from_actions(primary_actions)

    if not isinstance(cues, dict):
        cues = _find_entity_cues_in_blobs(
            blobs, title=title, gti=gti, content_id=content_id
        )

    if isinstance(cues, dict):
        if benefit_id is None and gti:
            gti_fields = _find_gti_cues_in_html(html, gti)
            benefit_id = gti_fields.get("benefit_id")
        parsed = entitlement_from_cues(
            cues,
            html=html,
            benefit_id=benefit_id,
            primary_actions=primary_actions,
        )
    elif gti and buybox and isinstance(buybox.get("messages"), dict):
        parsed = entitlement_from_cues(
            buybox["messages"],
            html=html,
            benefit_id=benefit_id,
            primary_actions=primary_actions,
        )
    else:
        rent_from, buy_from = _prices_from_html(html)
        parsed = {
            "entitlement_type": None,
            "included_with_prime": "Included with Prime" in html,
            "included_with_channel": None,
            "prime_catalog": False,
            "rent_from": rent_from,
            "buy_from": buy_from,
            "channel": None,
            "channel_note": None,
            "focus_message": None,
            "glance_message": None,
        }

    included_with_prime = bool(parsed.get("included_with_prime"))
    included_with_channel = parsed.get("included_with_channel")
    prime_catalog = bool(parsed.get("prime_catalog"))
    summary = _build_summary(
        included_with_prime=included_with_prime,
        included_with_channel=included_with_channel,
        prime_catalog=prime_catalog,
        rent_from=parsed.get("rent_from"),
        buy_from=parsed.get("buy_from"),
        channel=parsed.get("channel"),
        channel_note=parsed.get("channel_note"),
        entitlement_type=parsed.get("entitlement_type"),
        focus_message=parsed.get("focus_message"),
    )

    return PrimeEntitlement(
        content_id=content_id,
        title=title,
        gti=gti,
        entitlement_type=parsed.get("entitlement_type"),
        included_with_prime=included_with_prime,
        included_with_channel=included_with_channel,
        rent_from=parsed.get("rent_from"),
        buy_from=parsed.get("buy_from"),
        channel=parsed.get("channel"),
        channel_note=parsed.get("channel_note"),
        focus_message=parsed.get("focus_message"),
        glance_message=parsed.get("glance_message"),
        summary=summary,
        prime_catalog=prime_catalog,
        watchable_free=included_with_prime
        or (bool(included_with_channel) and parsed.get("entitlement_type") == "Entitled"),
        unsigned_catalog=True,
    )


def entitlement_from_search_cues(
    cues: dict,
    *,
    content_id: str,
    title: str,
) -> PrimeEntitlement:
    parsed = entitlement_from_cues(cues)
    included_with_prime = bool(parsed.get("included_with_prime"))
    included_with_channel = parsed.get("included_with_channel")
    prime_catalog = bool(parsed.get("prime_catalog"))
    summary = _build_summary(
        included_with_prime=included_with_prime,
        included_with_channel=included_with_channel,
        prime_catalog=prime_catalog,
        rent_from=parsed.get("rent_from"),
        buy_from=parsed.get("buy_from"),
        channel=parsed.get("channel"),
        channel_note=parsed.get("channel_note"),
        entitlement_type=parsed.get("entitlement_type"),
        focus_message=parsed.get("focus_message"),
    )
    return PrimeEntitlement(
        content_id=content_id,
        title=title,
        entitlement_type=parsed.get("entitlement_type"),
        included_with_prime=included_with_prime,
        included_with_channel=included_with_channel,
        rent_from=parsed.get("rent_from"),
        buy_from=parsed.get("buy_from"),
        channel=parsed.get("channel"),
        channel_note=parsed.get("channel_note"),
        focus_message=parsed.get("focus_message"),
        glance_message=parsed.get("glance_message"),
        summary=summary,
        prime_catalog=prime_catalog,
        watchable_free=included_with_prime
        or (bool(included_with_channel) and parsed.get("entitlement_type") == "Entitled"),
        unsigned_catalog=True,
    )


def lookup_entitlement(
    content_id: str,
    *,
    locale: str = "en-GB,en;q=0.9",
) -> PrimeEntitlement:
    if not DETAIL_ID_RE.match(content_id) and not content_id.startswith("http"):
        raise ValueError(
            f"Cannot check entitlement for {content_id!r}; use a Prime detail ID"
        )
    html = fetch_detail_html(content_id, locale=locale)
    parsed_id = content_id
    if match := re.search(r"/detail/([^/?#]+)", content_id):
        parsed_id = match.group(1)
    elif DETAIL_ID_RE.match(content_id):
        parsed_id = content_id
    return parse_entitlement(html, content_id=parsed_id)


def format_entitlement(ent: PrimeEntitlement) -> str:
    title = ent.title or ent.content_id
    lines = [f"{title} — Prime availability: {ent.summary}"]
    if ent.entitlement_type:
        lines.append(f"  entitlement: {ent.entitlement_type}")
    if ent.focus_message:
        lines.append(f"  offer: {ent.focus_message}")
    if ent.included_with_prime:
        lines.append("  stream: included with Prime subscription")
    elif ent.prime_catalog and ent.entitlement_type != "Entitled":
        lines.append("  stream: requires active Prime subscription on this profile")
    elif ent.included_with_channel:
        if ent.entitlement_type == "Entitled":
            lines.append(f"  stream: included with {ent.included_with_channel} channel")
        else:
            lines.append(
                f"  stream: requires {ent.included_with_channel} channel subscription"
            )
    else:
        if ent.rent_from:
            lines.append(f"  rent: from {ent.rent_from}")
        if ent.buy_from:
            lines.append(f"  buy: from {ent.buy_from}")
        if ent.channel or ent.channel_note:
            note = ent.channel_note or "subscription may be required"
            lines.append(f"  channel: {ent.channel or 'add-on'} ({note})")
        if not ent.rent_from and not ent.buy_from and not ent.channel:
            lines.append("  stream: not included with base Prime")
    if ent.unsigned_catalog and not ent.included_with_prime:
        lines.append(
            "  note: unsigned catalog check — your signed-in Prime account may differ"
        )
    return "\n".join(lines)
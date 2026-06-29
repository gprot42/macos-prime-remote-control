#!/usr/bin/env python3
"""
prime-catalog.py — Search and list Prime Video titles with LG-TV-ready content IDs.

Prime Video does not expose a catalog through the LG WebOS API. This script scrapes
public Prime Video web pages, which embed title metadata in JSON blobs.

Content IDs usable with lg-tv-connect.py --content-id:
  - Detail ID from /detail/<ID> URLs (recommended; from search/collection pages)
  - Amazon ASIN (B0XXXXXXXX) from detail pages (--resolve-asin)

Examples:
  python amazon/prime-catalog.py --search dune
  python amazon/prime-catalog.py --collection IncludedwithPrime
  python amazon/prime-catalog.py --url "https://www.primevideo.com/detail/0P3ONZ4IHQ75ZC4ZMIZ9D4NE7Q"
  python amazon/prime-catalog.py --search superman --resolve-asin --json

Launch on TV:
  python lg-tv-connect.py 192.168.0.79 --launch amazon --content-id <ID> --profile 0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from amazon.prime_entitlement import (
    detail_url,
    entitlement_from_search_cues,
    fetch_detail_html,
    lookup_entitlement,
    parse_entitlement,
    playback_launch_target_from_html,
    resolve_episode_content_id,
)

BASE_URL = "https://www.primevideo.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DETAIL_ID_RE = re.compile(r"^[0-9A-Z]{26,32}$")
ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
ASIN_IN_TEXT_RE = re.compile(r"B0[A-Z0-9]{8}")
DETAIL_PATH_RE = re.compile(r"/detail/([^/?#]+)")
JSON_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


@dataclass
class PrimeTitle:
    title: str
    content_id: str
    entity_type: str | None = None
    year: int | None = None
    runtime_min: int | None = None
    runtime_str: str | None = None
    asin: str | None = None
    gti: str | None = None
    source: str | None = None
    container: str | None = None
    availability: str | None = None
    included_with_prime: bool | None = None
    included_with_channel: str | None = None
    rent_from: str | None = None
    buy_from: str | None = None
    focus_message: str | None = None
    prime_catalog: bool | None = None
    image_url: str | None = None
    title_logo_url: str | None = None
    synopsis: str | None = None

    def launch_cmd(self, tv_ip: str = "192.168.0.79", profile: int = 0) -> str:
        launch_id = self.asin or self.content_id
        return (
            f"python lg-tv-connect.py {tv_ip} --launch amazon "
            f"--content-id {launch_id} --profile {profile}"
        )

    def access_label(self) -> str:
        """Short token for how the title can be watched."""
        if self.included_with_prime or self.prime_catalog:
            return "Prime"
        if self.included_with_channel:
            return "Channel"
        if self.rent_from and self.buy_from:
            return "Rent/Buy"
        if self.rent_from:
            return "Rent"
        if self.buy_from:
            return "Buy"
        summary = (self.availability or "").lower()
        if summary:
            if "rent" in summary or "buy" in summary:
                return "Rent/Buy"
            if "channel" in summary:
                return "Channel"
            # "Available with Prime", "Watch with a 30 day free Prime trial",
            # "Auto-renews ... after trial" — all Prime-catalog (watchable on a
            # Prime subscription), the unsigned check just couldn't confirm it.
            if "prime" in summary or "trial" in summary or "auto-renew" in summary or "auto renew" in summary:
                return "Prime"
            return "?"
        return "-"


def fetch_html(url: str, *, timeout: float = 20.0) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not fetch {url}: {exc.reason}") from exc


def parse_json_blobs(html: str) -> list[Any]:
    blobs: list[Any] = []
    for match in JSON_SCRIPT_RE.finditer(html):
        try:
            blobs.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return blobs


def normalize_gti(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("amzn1.dv.gti."):
        return value.removeprefix("amzn1.dv.gti.")
    return value


def content_id_from_link(url: str | None) -> str | None:
    if not url:
        return None
    match = DETAIL_PATH_RE.search(url)
    return match.group(1) if match else None


def parse_content_id(value: str) -> str:
    value = value.strip()
    if match := DETAIL_PATH_RE.search(value):
        return match.group(1)
    if DETAIL_ID_RE.match(value) or ASIN_RE.match(value):
        return value
    raise ValueError(f"Unrecognized content id or URL: {value!r}")


_SKIP_IMAGE_KEYS = frozenset({"titleLogo"})
_IMAGE_KEY_RANK = {
    "hero": 0,
    "keyart": 1,
    "landscape": 2,
    "poster": 3,
    "poster2x3": 4,
    "packshot": 5,
    "cover": 6,
}


def _is_branded_carousel_image(url: str) -> bool:
    """Prime carousel composites (pv-target-images) bake in title/Prime logos."""
    return "pv-target-images" in url


def _sonata_folder(url: str) -> str | None:
    match = re.search(r"sonata-images-prod/([^/]+)/", url)
    return match.group(1) if match else None


def _is_play_cta_url(url: str) -> bool:
    """Sonata hero banners are full-frame “Play on TV” CTAs — not poster art."""
    if "sonata-images-prod" not in url:
        return False
    folder = _sonata_folder(url) or ""
    # Still/cover variants (…_RM_UI_CO1, …_CO3) are safe for cards.
    if re.search(r"_CO\d*$", folder) or "_CO_" in folder:
        return False
    # Bare *_RM_UI folders are rotating hero banners with a giant play button.
    return folder.endswith("_RM_UI") or bool(re.search(r"_Hero_.*_RM_UI$", folder))


def _is_play_cta_image(url: str, key: str) -> bool:
    return key == "hero" and _is_play_cta_url(url)


def _image_url_rank(url: str, key: str) -> tuple[int, int, int]:
    """Lower rank = better card art (prefer scene stills over marketing overlays)."""
    cta = 1 if _is_play_cta_image(url, key) else 0
    branded = 1 if _is_branded_carousel_image(url) else 0
    return (cta, branded, _IMAGE_KEY_RANK.get(key, 99))


def _image_url_rank_auto(url: str) -> tuple[int, int, int]:
    """Rank a URL when merging across carousel rows (key may be unknown)."""
    if _is_play_cta_url(url):
        return (9, 9, 99)
    return min(_image_url_rank(url, k) for k in _IMAGE_KEY_RANK)


def _extract_image_url(images: Any) -> str | None:
    """Return the best landscape card image from an entity's images dict."""
    if not isinstance(images, dict):
        return None
    best: tuple[tuple[int, int, int], str] | None = None
    for key, img in images.items():
        if key in _SKIP_IMAGE_KEYS or not isinstance(img, dict):
            continue
        url = img.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        rank = _image_url_rank(url, key)
        if best is None or rank < best[0]:
            best = (rank, url)
    return best[1] if best else None


def _prefer_image_url(current: str | None, candidate: str | None) -> str | None:
    """Keep the higher-quality image when the same title appears in multiple carousels."""
    if not candidate:
        return current
    if not current:
        return candidate
    cur_rank = _image_url_rank_auto(current)
    cand_rank = _image_url_rank_auto(candidate)
    return candidate if cand_rank < cur_rank else current


def _merge_title_item(existing: PrimeTitle, new: PrimeTitle) -> PrimeTitle:
    """Merge duplicate carousel rows for the same content_id."""
    existing.image_url = _prefer_image_url(existing.image_url, new.image_url)
    if not existing.synopsis and new.synopsis:
        existing.synopsis = new.synopsis
    if not existing.container and new.container:
        existing.container = new.container
    return existing


def _parse_runtime_str(runtime: Any) -> tuple[int | None, str | None]:
    """Return (runtime_min, runtime_str) from entity runtime field (int or string)."""
    if isinstance(runtime, (int, float)):
        return int(runtime), None
    if isinstance(runtime, str) and runtime.strip():
        raw = runtime.strip()
        # Parse "1 h 54 min" or "54 min" or "1 h"
        total = 0
        h_match = re.search(r"(\d+)\s*h", raw)
        m_match = re.search(r"(\d+)\s*min", raw)
        if h_match:
            total += int(h_match.group(1)) * 60
        if m_match:
            total += int(m_match.group(1))
        return (total if total > 0 else None), raw
    return None, None


def entity_to_title(entity: dict[str, Any], *, source: str) -> PrimeTitle | None:
    title = entity.get("displayTitle") or entity.get("title")
    link = entity.get("link") if isinstance(entity.get("link"), dict) else {}
    content_id = content_id_from_link(link.get("url"))
    if not title or not content_id:
        return None

    gti = normalize_gti(entity.get("titleID") or entity.get("impressionId"))
    runtime_min, runtime_str = _parse_runtime_str(entity.get("runtime"))
    year_raw = entity.get("releaseYear")
    year = int(year_raw) if isinstance(year_raw, (int, str)) and str(year_raw).isdigit() else None
    images = entity.get("images")
    synopsis = entity.get("synopsis")
    item = PrimeTitle(
        title=str(title),
        content_id=content_id,
        entity_type=entity.get("entityType"),
        year=year,
        runtime_min=runtime_min,
        runtime_str=runtime_str,
        gti=gti,
        source=source,
        image_url=_extract_image_url(images),
        title_logo_url=None,
        synopsis=str(synopsis).strip() if synopsis else None,
    )
    cues = entity.get("entitlementCues")
    if isinstance(cues, dict) and cues.get("entitlementType"):
        apply_entitlement_cues(item, cues)
    return item


def walk_entities(obj: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if "displayTitle" in node and "link" in node:
                found.append(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(obj)
    return found


def dedupe_titles(items: list[PrimeTitle]) -> list[PrimeTitle]:
    seen: dict[str, PrimeTitle] = {}
    order: list[str] = []
    for item in items:
        if item.content_id in seen:
            seen[item.content_id] = _merge_title_item(seen[item.content_id], item)
            continue
        seen[item.content_id] = item
        order.append(item.content_id)
    return [seen[content_id] for content_id in order]


def extract_titles_from_html(html: str, *, source: str) -> list[PrimeTitle]:
    titles: list[PrimeTitle] = []
    for blob in parse_json_blobs(html):
        for entity in walk_entities(blob):
            if parsed := entity_to_title(entity, source=source):
                titles.append(parsed)
    return dedupe_titles(titles)


def search_prime(query: str) -> list[PrimeTitle]:
    url = f"{BASE_URL}/search/ref=atv_sr_sug?{urllib.parse.urlencode({'phrase': query})}"
    html = fetch_html(url)
    return extract_titles_from_html(html, source=f"search:{query}")


@dataclass
class SwiftRequestParams:
    page_type: str
    page_id: str
    decoration_scheme: str
    feature_scheme: str
    widget_scheme: str
    dynamic_features: list[str]
    variant: str = "Desktop"


def fetch_json(url: str, *, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "x-requested-with": "XMLHttpRequest",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not fetch {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {url}") from exc


def extract_swift_params(html: str) -> SwiftRequestParams | None:
    """Page-level scheme/variant params required by paginateCollection."""
    for blob in parse_json_blobs(html):
        if not isinstance(blob, dict):
            continue
        body = (
            blob.get("init", {})
            .get("preparations", {})
            .get("body", {})
        )
        if not isinstance(body, dict):
            continue
        swift = body.get("swiftPageParameters")
        if not isinstance(swift, dict):
            continue
        pagination = body.get("pagination")
        qp: dict[str, Any] = {}
        if isinstance(pagination, dict):
            raw_qp = pagination.get("queryParameters")
            if isinstance(raw_qp, dict):
                qp = raw_qp
        dynamic = qp.get("dynamicFeatures")
        return SwiftRequestParams(
            page_type=str(swift.get("pageType") or ""),
            page_id=str(swift.get("pageId") or ""),
            decoration_scheme=str(
                qp.get("decorationScheme")
                or body.get("decorationScheme")
                or "web-decoration-gti-v4"
            ),
            feature_scheme=str(
                qp.get("featureScheme")
                or body.get("featureScheme")
                or "web-features-v6"
            ),
            widget_scheme=str(
                qp.get("widgetScheme")
                or body.get("widgetScheme")
                or "web-explore-v33"
            ),
            dynamic_features=list(dynamic) if isinstance(dynamic, list) else [],
            variant=str(qp.get("variant") or "Desktop"),
        )
    return None


def _build_paginate_collection_url(
    swift: SwiftRequestParams,
    *,
    service_token: str,
    start_index: int,
    target_id: str,
    journey_ingress: str | None = None,
) -> str:
    params: list[tuple[str, str]] = [
        ("pageType", swift.page_type),
        ("pageId", swift.page_id),
        ("collectionType", "Container"),
        ("paginationTargetId", target_id),
        ("serviceToken", service_token),
        ("startIndex", str(start_index)),
        ("actionScheme", "default"),
        ("payloadScheme", "default"),
        ("decorationScheme", swift.decoration_scheme),
        ("featureScheme", swift.feature_scheme),
        ("widgetScheme", swift.widget_scheme),
        ("variant", swift.variant),
    ]
    if journey_ingress:
        params.append(("journeyIngressContext", journey_ingress))
    for feature in swift.dynamic_features:
        params.append(("dynamicFeatures", str(feature)))
    return f"{BASE_URL}/api/paginateCollection?{urllib.parse.urlencode(params)}"


def paginate_carousel_entities(
    container: dict[str, Any],
    swift: SwiftRequestParams,
    *,
    max_rounds: int = 10,
) -> list[dict[str, Any]]:
    """Fetch all entities for a horizontally paginated carousel row."""
    entities: list[dict[str, Any]] = [
        entity
        for entity in (container.get("entities") or [])
        if isinstance(entity, dict)
    ]
    token = container.get("paginationServiceToken")
    if not isinstance(token, str) or not token.strip():
        return entities

    start_index = container.get("paginationStartIndex", len(entities))
    if not isinstance(start_index, int):
        try:
            start_index = int(start_index)
        except (TypeError, ValueError):
            start_index = len(entities)

    target_id = container.get("paginationTargetId") or ""
    if not isinstance(target_id, str):
        target_id = str(target_id)

    journey = container.get("journeyIngressContext")
    journey_ingress = str(journey) if journey is not None else None

    seen_ids: set[str] = set()
    for entity in entities:
        link = entity.get("link") if isinstance(entity.get("link"), dict) else {}
        if cid := content_id_from_link(link.get("url")):
            seen_ids.add(cid)

    for _ in range(max_rounds):
        url = _build_paginate_collection_url(
            swift,
            service_token=token,
            start_index=start_index,
            target_id=target_id,
            journey_ingress=journey_ingress,
        )
        try:
            data = fetch_json(url)
        except RuntimeError:
            break
        if not isinstance(data, dict):
            break

        batch = data.get("entities") or []
        if not isinstance(batch, list) or not batch:
            break

        for entity in batch:
            if not isinstance(entity, dict):
                continue
            link = entity.get("link") if isinstance(entity.get("link"), dict) else {}
            cid = content_id_from_link(link.get("url"))
            if cid and cid in seen_ids:
                continue
            if cid:
                seen_ids.add(cid)
            entities.append(entity)

        if not data.get("hasMoreItems"):
            break

        pagination = data.get("pagination")
        if not isinstance(pagination, dict):
            break
        next_token = pagination.get("serviceToken")
        if not isinstance(next_token, str) or not next_token.strip():
            break
        token = next_token

        next_start = data.get("startIndex", pagination.get("startIndex"))
        if not isinstance(next_start, int):
            try:
                next_start = int(next_start)
            except (TypeError, ValueError):
                break
        start_index = next_start

    return entities


def _entities_to_titles(
    entities: list[dict[str, Any]],
    *,
    source: str,
    container_label: str,
) -> list[PrimeTitle]:
    titles: list[PrimeTitle] = []
    for entity in entities:
        if parsed := entity_to_title(entity, source=source):
            parsed.container = container_label
            titles.append(parsed)
    return dedupe_titles(titles)


COLLECTION_ALIASES: dict[str, str] = {
    # Prime Video's /collection/TopRatedMovies page is empty; TopRated has the catalog.
    "TopRatedMovies": "TopRated",
    "topratedmovies": "TopRated",
}

GENRE_LABELS: dict[str, str] = {
    "science-fiction": "Sci-Fi",
}


def resolve_collection_slug(slug: str) -> str:
    slug = slug.strip("/")
    return COLLECTION_ALIASES.get(slug, slug)


def parse_catalog_slug(slug: str) -> tuple[str, str]:
    """Return (storefront_kind, slug) for collection or genre pages."""
    slug = resolve_collection_slug(slug.strip("/"))
    if slug.startswith("genre/"):
        return "genre", slug.removeprefix("genre/").strip("/")
    return "collection", slug


def resolve_play_url(content_id: str, *, episode: int | None = None) -> dict[str, str]:
    """Return a Prime Video web URL suitable for in-browser playback on Mac."""
    content_id = content_id.strip()
    if not content_id:
        raise ValueError("content_id is required")

    html = fetch_detail_html(content_id)
    play_id = content_id
    if episode is not None and episode >= 1:
        play_id = resolve_episode_content_id(html, content_id, episode=episode)
        if play_id != content_id:
            html = fetch_detail_html(play_id)

    target = playback_launch_target_from_html(html, play_id)
    if target:
        if target.startswith("http"):
            url = target
        elif target.startswith("/"):
            url = f"{BASE_URL}{target}"
        else:
            url = f"{BASE_URL}/{target}"
    else:
        url = detail_url(play_id)

    title = ""
    for blob in parse_json_blobs(html):
        for entity in walk_entities(blob):
            link = entity.get("link") if isinstance(entity.get("link"), dict) else {}
            cid = content_id_from_link(link.get("url"))
            if cid == play_id:
                if name := entity.get("displayTitle") or entity.get("title"):
                    title = str(name).strip()
                    break
        if title:
            break

    return {"url": url, "content_id": play_id, "title": title}


def list_genres() -> list[tuple[str, str]]:
    """Return (slug, label) pairs scraped from Prime Video /categories."""
    html = fetch_html(f"{BASE_URL}/categories")
    slugs = sorted(set(re.findall(r"/genre/([a-z0-9-]+)", html)))
    return [
        (
            f"genre/{slug}",
            GENRE_LABELS.get(slug, slug.replace("-", " ").title()),
        )
        for slug in slugs
    ]


def _storefront_url(kind: str, slug: str, *, page: int = 1) -> str:
    slug = slug.strip("/")
    base = f"{BASE_URL}/{kind}/{slug}"
    if page <= 1:
        return base
    return f"{base}?page={page}"


def _merge_storefront_pages(
    slug: str,
    *,
    kind: str = "collection",
    max_pages: int = 8,
    full_carousels: bool = False,
    max_carousel_rounds: int = 10,
) -> tuple[list[PrimeTitle], str]:
    """Fetch a collection or genre storefront across paginated ?page=N views."""
    slug = slug.strip("/")
    source = f"{kind}:{slug}"
    seen: dict[str, PrimeTitle] = {}
    order: list[str] = []
    last_html = ""
    completed_carousel_targets: set[str] = set()

    for page in range(1, max_pages + 1):
        url = _storefront_url(kind, slug, page=page)
        html = fetch_html(url)
        last_html = html
        groups = extract_collection_groups(
            html,
            source=source,
            full_carousels=full_carousels,
            max_carousel_rounds=max_carousel_rounds,
            completed_carousel_targets=completed_carousel_targets,
        )
        page_items: list[PrimeTitle] = []
        if groups:
            for _label, titles in groups:
                page_items.extend(titles)
        else:
            page_items = extract_titles_from_html(html, source=source)

        added = 0
        for item in page_items:
            if item.content_id in seen:
                seen[item.content_id] = _merge_title_item(seen[item.content_id], item)
                continue
            seen[item.content_id] = item
            order.append(item.content_id)
            added += 1
        if page > 1 and added == 0:
            break

    return [seen[content_id] for content_id in order], last_html


def list_collection(
    slug: str,
    *,
    full_carousels: bool = True,
    max_carousel_rounds: int = 10,
) -> list[PrimeTitle]:
    kind, bare_slug = parse_catalog_slug(slug)
    items, html = _merge_storefront_pages(
        bare_slug,
        kind=kind,
        full_carousels=full_carousels,
        max_carousel_rounds=max_carousel_rounds,
    )
    return merge_hero_banner_images(html, items)


# StandardHero is the rotating top banner — skip it as a catalog row (titles
# repeat below), but still mine it for cleaner hero art in merge_hero_banner_images.
SKIP_CONTAINER_TYPES = {"StandardHero"}
HERO_BANNER_CONTAINER_TYPES = {"StandardHero"}


def _find_container_lists(node: Any) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(node, dict):
        containers = node.get("containers")
        if isinstance(containers, list):
            found.append(containers)
        for value in node.values():
            found.extend(_find_container_lists(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_find_container_lists(value))
    return found


def merge_hero_banner_images(html: str, items: list[PrimeTitle]) -> list[PrimeTitle]:
    """Upgrade card art using the top StandardHero banner (cleaner scene stills)."""
    if not items:
        return items
    by_id = {item.content_id: item for item in items}
    for blob in parse_json_blobs(html):
        for containers in _find_container_lists(blob):
            for container in containers:
                if not isinstance(container, dict):
                    continue
                if container.get("containerType") not in HERO_BANNER_CONTAINER_TYPES:
                    continue
                entities = container.get("entities")
                if not isinstance(entities, list):
                    continue
                for entity in entities:
                    if not isinstance(entity, dict):
                        continue
                    if parsed := entity_to_title(entity, source="hero-banner"):
                        if (
                            parsed.content_id in by_id
                            and parsed.image_url
                            and not _is_play_cta_url(parsed.image_url)
                        ):
                            by_id[parsed.content_id] = _merge_title_item(
                                by_id[parsed.content_id], parsed
                            )
    return [by_id[item.content_id] for item in items]


def _carousel_target_id(container: dict[str, Any]) -> str:
    target = container.get("paginationTargetId") or ""
    return str(target) if target else ""


def _resolve_container_entities(
    container: dict[str, Any],
    swift: SwiftRequestParams | None,
    *,
    full_carousels: bool,
    max_carousel_rounds: int,
    completed_targets: set[str] | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return (entities, carousel_target_id) or (None, _) if already paginated."""
    raw_entities = container.get("entities")
    if not isinstance(raw_entities, list):
        return None, None

    target_id = _carousel_target_id(container) or None
    if (
        full_carousels
        and completed_targets is not None
        and target_id
        and target_id in completed_targets
    ):
        return None, target_id

    if full_carousels and swift and container.get("paginationServiceToken"):
        entities = paginate_carousel_entities(
            container,
            swift,
            max_rounds=max_carousel_rounds,
        )
        return entities, target_id

    return [entity for entity in raw_entities if isinstance(entity, dict)], target_id


def extract_collection_groups(
    html: str,
    *,
    source: str,
    full_carousels: bool = False,
    max_carousel_rounds: int = 10,
    completed_carousel_targets: set[str] | None = None,
) -> list[tuple[str, list[PrimeTitle]]]:
    """Parse a collection page into (carousel label, titles) groups."""
    swift = extract_swift_params(html) if full_carousels else None
    completed_targets = (
        completed_carousel_targets
        if completed_carousel_targets is not None
        else set()
    )

    pending: list[tuple[str, dict[str, Any], bool]] = []
    for blob in parse_json_blobs(html):
        for containers in _find_container_lists(blob):
            for container in containers:
                if not isinstance(container, dict):
                    continue
                if container.get("containerType") in SKIP_CONTAINER_TYPES:
                    continue
                if not isinstance(container.get("entities"), list):
                    continue
                label = str(
                    container.get("text")
                    or container.get("title")
                    or container.get("containerType")
                    or "Titles"
                )
                needs_api = bool(
                    full_carousels
                    and swift
                    and container.get("paginationServiceToken")
                    and _carousel_target_id(container) not in completed_targets
                )
                pending.append((label, container, needs_api))

    entities_by_label: list[tuple[str, list[dict[str, Any]]]] = []
    api_jobs = [(label, container) for label, container, needs_api in pending if needs_api]
    inline_jobs = [
        (label, container)
        for label, container, needs_api in pending
        if not needs_api
    ]

    if api_jobs and swift:
        workers = min(6, len(api_jobs))

        def fetch_row(
            job: tuple[str, dict[str, Any]],
        ) -> tuple[str, list[dict[str, Any]] | None, str | None]:
            label, container = job
            entities, target_id = _resolve_container_entities(
                container,
                swift,
                full_carousels=True,
                max_carousel_rounds=max_carousel_rounds,
            )
            return label, entities, target_id

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for label, entities, target_id in pool.map(fetch_row, api_jobs):
                if target_id:
                    completed_targets.add(target_id)
                if entities:
                    entities_by_label.append((label, entities))

    for label, container in inline_jobs:
        entities, _target_id = _resolve_container_entities(
            container,
            swift,
            full_carousels=full_carousels,
            max_carousel_rounds=max_carousel_rounds,
            completed_targets=completed_targets,
        )
        if entities:
            entities_by_label.append((label, entities))

    groups: list[tuple[str, list[PrimeTitle]]] = []
    for label, entities in entities_by_label:
        titles = _entities_to_titles(entities, source=source, container_label=label)
        if titles:
            groups.append((label, titles))
    return groups


def _apply_entitlement_fields(item: PrimeTitle, ent) -> None:
    item.availability = ent.summary
    item.included_with_prime = ent.included_with_prime
    item.included_with_channel = ent.included_with_channel
    item.rent_from = ent.rent_from
    item.buy_from = ent.buy_from
    item.focus_message = ent.focus_message
    item.prime_catalog = getattr(ent, "prime_catalog", None)
    if ent.gti and not item.gti:
        item.gti = ent.gti.removeprefix("amzn1.dv.gti.")


def apply_entitlement_cues(item: PrimeTitle, cues: dict) -> None:
    ent = entitlement_from_search_cues(cues, content_id=item.content_id, title=item.title)
    _apply_entitlement_fields(item, ent)


def apply_entitlement(item: PrimeTitle, html: str | None = None) -> None:
    try:
        cues = None
        if html is not None:
            from amazon.prime_entitlement import parse_json_blobs, _find_entity_cues_in_blobs

            blobs = parse_json_blobs(html)
            gti = f"amzn1.dv.gti.{item.gti}" if item.gti else None
            cues = _find_entity_cues_in_blobs(
                blobs,
                title=item.title,
                gti=gti,
                content_id=item.content_id,
            )
        if html is None:
            ent = lookup_entitlement(item.content_id)
        else:
            ent = parse_entitlement(
                html,
                content_id=item.content_id,
                title=item.title,
                entitlement_cues=cues,
            )
    except (RuntimeError, ValueError) as exc:
        item.availability = f"unknown ({exc})"
        return
    _apply_entitlement_fields(item, ent)


def enrich_entitlements_detail(
    items: list[PrimeTitle],
    *,
    limit: int | None = None,
    workers: int = 6,
) -> None:
    """Fetch each title's detail page for rent/buy prices (slow)."""
    batch = items if limit is None else items[:limit]
    total = len(batch)
    if not total:
        return

    print(
        f"Resolving entitlement from detail pages ({total} title(s))...",
        file=sys.stderr,
    )

    def resolve(item: PrimeTitle) -> PrimeTitle:
        apply_entitlement(item)
        return item

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(resolve, item) for item in batch]
        for future in as_completed(futures):
            item = future.result()
            done += 1
            print(f"  [{done}/{total}] {item.title}", file=sys.stderr, flush=True)


def lookup_url(url: str) -> PrimeTitle:
    content_id = parse_content_id(url)
    if url.startswith("http"):
        page_url = url
    else:
        page_url = f"{BASE_URL}/detail/{content_id}"
    html = fetch_html(page_url)
    titles = extract_titles_from_html(html, source=page_url)
    for item in titles:
        if item.content_id == content_id:
            apply_entitlement(item, html)
            return item
    title_match = re.search(r"<title>Prime Video:\s*([^<]+)</title>", html)
    title = title_match.group(1).strip() if title_match else content_id
    item = PrimeTitle(title=title, content_id=content_id, source=page_url)
    apply_entitlement(item, html)
    return item


def resolve_asin(content_id: str) -> str | None:
    url = f"{BASE_URL}/detail/{content_id}"
    html = fetch_html(url)
    asins = ASIN_IN_TEXT_RE.findall(html)
    return asins[0] if asins else None


def enrich_asins(items: list[PrimeTitle], *, limit: int | None = None) -> None:
    for item in items[:limit]:
        if item.asin:
            continue
        item.asin = resolve_asin(item.content_id)


TITLE_COL_WIDTH = 43
TYPE_COL_WIDTH = 8
ACCESS_COL_WIDTH = 9


def format_title_row(
    item: PrimeTitle,
    *,
    show_asin: bool = False,
    show_availability: bool = False,
) -> str:
    line = (
        f"{item.title:<{TITLE_COL_WIDTH}}"
        f"{(item.entity_type or ''):<{TYPE_COL_WIDTH}}"
        f"{str(item.year or ''):>4}  "
        f"{item.content_id}"
    )
    if show_asin:
        line += f"  {item.asin or ''}"
    if show_availability:
        line += f"  {item.access_label():<{ACCESS_COL_WIDTH}}{item.availability or ''}"
    return line


def print_table(
    items: list[PrimeTitle],
    *,
    show_asin: bool,
    show_availability: bool,
    show_header: bool = False,
) -> None:
    if not items:
        print("No titles found.")
        return

    header = (
        f"{'title':<{TITLE_COL_WIDTH}}"
        f"{'type':<{TYPE_COL_WIDTH}}"
        f"{'year':>4}  "
        "content_id"
    )
    if show_asin:
        header += "  asin"
    if show_availability:
        header += f"  {'access':<{ACCESS_COL_WIDTH}}availability"

    grouped = any(item.container for item in items)
    last_container: str | None = None
    for item in items:
        if grouped and item.container != last_container:
            print(f"\n== {item.container or 'Other'} ==")
            if show_header:
                print(header)
                print("-" * len(header))
            last_container = item.container
        elif not grouped and show_header and last_container is None:
            print(header)
            print("-" * len(header))
            last_container = ""
        print(format_title_row(item, show_asin=show_asin, show_availability=show_availability))
    print(f"\n{len(items)} title(s). Use content_id with lg-tv-connect.py --content-id")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search/list Prime Video titles and extract LG-TV content IDs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  %(prog)s --search dune
  %(prog)s --collection newandupcoming
  %(prog)s --url https://www.primevideo.com/detail/0P3ONZ4IHQ75ZC4ZMIZ9D4NE7Q
  %(prog)s --search superman --resolve-asin --json

Known collections (region-dependent): newandupcoming, IncludedwithPrime, TopRated
  Genre slugs: genre/action, genre/anime, genre/comedy, genre/documentary, genre/drama,
    genre/fantasy, genre/historical, genre/horror, genre/romance, genre/science-fiction,
    genre/suspense (use --list-genres to refresh from Prime Video)""",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--search", metavar="QUERY", help="Search Prime Video")
    group.add_argument(
        "--collection",
        metavar="SLUG",
        help="List titles from a Prime Video collection or genre page",
    )
    group.add_argument(
        "--list-genres",
        action="store_true",
        help="List genre slugs from Prime Video /categories",
    )
    group.add_argument(
        "--url",
        metavar="URL_OR_ID",
        help="Look up one title from a Prime Video URL or detail ID",
    )
    group.add_argument(
        "--play-url",
        metavar="CONTENT_ID",
        help="Resolve a Prime Video web playback URL for Mac/browser",
    )
    parser.add_argument(
        "--resolve-asin",
        action="store_true",
        help="Fetch each detail page to resolve Amazon ASIN (slower)",
    )
    parser.add_argument(
        "--resolve-entitlement",
        action="store_true",
        help="Show rent/buy/Prime inclusion from listing-page cues (fast; default for --url)",
    )
    parser.add_argument(
        "--resolve-entitlement-detail",
        action="store_true",
        help="Fetch each detail page for rent/buy prices (slow; use --limit on large collections)",
    )
    parser.add_argument(
        "--no-entitlement",
        action="store_true",
        help="Skip availability lookup",
    )
    parser.add_argument(
        "--show-availability",
        action="store_true",
        help="Add availability column to table output (off by default)",
    )
    parser.add_argument(
        "--show-header",
        action="store_true",
        help="Print column headers above the table",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument(
        "--episode",
        type=int,
        metavar="N",
        help="Episode number for --play-url on TV seasons/series (1-based)",
    )
    parser.add_argument("--limit", type=int, metavar="N", help="Limit results")
    parser.add_argument(
        "--launch-cmd",
        action="store_true",
        help="Include example lg-tv-connect.py command per title (JSON only)",
    )
    parser.add_argument(
        "--no-full-carousels",
        action="store_true",
        help="Only first screen of each carousel row (~20 titles; faster)",
    )
    parser.add_argument(
        "--carousel-rounds",
        type=int,
        default=10,
        metavar="N",
        help="Max horizontal pagination API calls per carousel row (default 10)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        if args.list_genres:
            for slug, label in list_genres():
                print(f"{slug}\t{label}")
            return
        if args.play_url:
            result = resolve_play_url(args.play_url, episode=args.episode)
            print(json.dumps(result, indent=2 if args.json else None))
            return
        if args.search:
            items = search_prime(args.search)
        elif args.collection:
            items = list_collection(
                args.collection,
                full_carousels=not args.no_full_carousels,
                max_carousel_rounds=max(1, args.carousel_rounds),
            )
        else:
            items = [lookup_url(args.url)]
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.limit is not None:
        if args.limit < 1:
            print("error: --limit must be >= 1", file=sys.stderr)
            sys.exit(2)
        items = items[: args.limit]

    if args.resolve_asin:
        enrich_asins(items, limit=args.limit)

    resolve_availability = not args.no_entitlement and (
        args.resolve_entitlement
        or args.resolve_entitlement_detail
        or args.url is not None
    )
    if args.resolve_entitlement_detail:
        enrich_entitlements_detail(items, limit=args.limit)

    show_availability_table = args.show_availability and resolve_availability
    show_availability_detail = resolve_availability and args.url is not None

    if args.json:
        payload = []
        for item in items:
            row = asdict(item)
            if args.launch_cmd:
                row["launch_cmd"] = item.launch_cmd()
            payload.append(row)
        print(json.dumps(payload, indent=2))
    else:
        print_table(
            items,
            show_asin=args.resolve_asin,
            show_availability=show_availability_table,
            show_header=args.show_header,
        )
        if len(items) == 1 and show_availability_detail and items[0].availability:
            item = items[0]
            print(f"\n{item.title} — Prime availability: {item.availability}")
            if item.included_with_prime:
                print("  stream: included with Prime subscription")
            elif item.included_with_channel:
                if item.focus_message and "trial" in item.focus_message.lower():
                    print(
                        f"  stream: available with {item.included_with_channel} "
                        f"channel subscription (see offer)"
                    )
                else:
                    print(f"  stream: included with {item.included_with_channel} channel")
            else:
                if item.rent_from:
                    print(f"  rent: from {item.rent_from}")
                if item.buy_from:
                    print(f"  buy: from {item.buy_from}")
                if item.focus_message:
                    print(f"  offer: {item.focus_message}")
                if not item.rent_from and not item.buy_from:
                    print("  stream: not included with base Prime")
                print(
                    "  note: unsigned catalog check — signed-in Prime may show different access"
                )


if __name__ == "__main__":
    main()
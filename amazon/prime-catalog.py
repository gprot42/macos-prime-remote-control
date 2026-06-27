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
    entitlement_from_search_cues,
    lookup_entitlement,
    parse_entitlement,
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
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
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


def _extract_image_url(images: Any) -> str | None:
    """Return a hero image URL from an entity's images dict."""
    if not isinstance(images, dict):
        return None
    # Try keys in preference order; Amazon uses different keys per carousel type.
    # 'cover'     – used by genre rows (Binge-worthy, Romance TV, Drama TV, etc.)
    # 'hero'      – used by Amazon Originals, Top 10, etc.
    # 'poster2x3' – portrait poster used in some contexts
    # others      – legacy / regional variants
    for key in ("cover", "hero", "packshot", "poster2x3", "poster", "landscape", "keyart"):
        img = images.get(key)
        if isinstance(img, dict):
            url = img.get("url")
            if isinstance(url, str) and url.startswith("http"):
                return url
    return None


def _extract_title_logo_url(images: Any) -> str | None:
    """Return the title logo URL from an entity's images dict."""
    if not isinstance(images, dict):
        return None
    img = images.get("titleLogo")
    if isinstance(img, dict):
        url = img.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
    return None


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
        title_logo_url=_extract_title_logo_url(images),
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
    seen: set[str] = set()
    unique: list[PrimeTitle] = []
    for item in items:
        if item.content_id in seen:
            continue
        seen.add(item.content_id)
        unique.append(item)
    return unique


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


def list_collection(slug: str) -> list[PrimeTitle]:
    slug = slug.strip("/")
    url = f"{BASE_URL}/collection/{slug}"
    html = fetch_html(url)
    groups = extract_collection_groups(html, source=f"collection:{slug}")
    if groups:
        # The collection page is a storefront of carousels (hero, Top 10, genre
        # rows, ...). Keep each title once, tagged with the first meaningful row
        # it appears in, so the listing is grouped instead of an undifferentiated
        # flat dump of every carousel.
        seen: set[str] = set()
        items: list[PrimeTitle] = []
        for _label, titles in groups:
            for item in titles:
                if item.content_id in seen:
                    continue
                seen.add(item.content_id)
                items.append(item)
        if items:
            return items
    return extract_titles_from_html(html, source=f"collection:{slug}")


# Rotating banner that just repeats titles from the real rows below it.
SKIP_CONTAINER_TYPES = {"StandardHero"}


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


def extract_collection_groups(
    html: str, *, source: str
) -> list[tuple[str, list[PrimeTitle]]]:
    """Parse a collection page into (carousel label, titles) groups."""
    groups: list[tuple[str, list[PrimeTitle]]] = []
    for blob in parse_json_blobs(html):
        for containers in _find_container_lists(blob):
            for container in containers:
                if not isinstance(container, dict):
                    continue
                if container.get("containerType") in SKIP_CONTAINER_TYPES:
                    continue
                entities = container.get("entities")
                if not isinstance(entities, list):
                    continue
                label = (
                    container.get("text")
                    or container.get("title")
                    or container.get("containerType")
                    or "Titles"
                )
                titles: list[PrimeTitle] = []
                for entity in entities:
                    if not isinstance(entity, dict):
                        continue
                    if parsed := entity_to_title(entity, source=source):
                        parsed.container = str(label)
                        titles.append(parsed)
                titles = dedupe_titles(titles)
                if titles:
                    groups.append((str(label), titles))
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

Known collections (region-dependent): newandupcoming, IncludedwithPrime, TopRatedMovies""",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--search", metavar="QUERY", help="Search Prime Video")
    group.add_argument(
        "--collection",
        metavar="SLUG",
        help="List titles from a Prime Video collection page",
    )
    group.add_argument(
        "--url",
        metavar="URL_OR_ID",
        help="Look up one title from a Prime Video URL or detail ID",
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
    parser.add_argument("--limit", type=int, metavar="N", help="Limit results")
    parser.add_argument(
        "--launch-cmd",
        action="store_true",
        help="Include example lg-tv-connect.py command per title (JSON only)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        if args.search:
            items = search_prime(args.search)
        elif args.collection:
            items = list_collection(args.collection)
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
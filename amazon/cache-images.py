#!/usr/bin/env python3
"""
cache-images.py — Download and cache Prime Video poster images locally.

Reads a JSON array of {content_id, url} objects from stdin.
For each item, downloads the image (resized to a card-friendly size) and saves
it to ~/.cache/prime-catalog-ui/images/<content_id>.jpg.

Progress is reported to stdout one line per image:
  CACHED\t<content_id>\t<absolute_path>
  FAILED\t<content_id>

Usage (called by the Tauri backend):
  echo '[{"content_id":"...", "url":"..."}]' | python cache-images.py
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "prime-catalog-ui" / "images"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_WORKERS = 8
CARD_WIDTH = 640
CARD_HEIGHT = 360


def safe_filename(content_id: str) -> str:
    """Strip any chars that would be unsafe in a filename."""
    return re.sub(r"[^\w\-]", "_", content_id) + ".jpg"


def meta_filename(content_id: str) -> str:
    """Sidecar recording which remote URL was cached for a content_id."""
    return re.sub(r"[^\w\-]", "_", content_id) + ".meta"


def resize_amazon_url(url: str, w: int, h: int) -> str:
    """Replace the Amazon CDN size parameter with the requested dimensions."""
    return re.sub(r"\._UR\d+,\d+_\.", f"._UR{w},{h}_.", url)


def download_image(content_id: str, url: str) -> tuple[str, str | None]:
    """
    Download *url* and save to disk.
    Returns (content_id, local_path) on success, (content_id, None) on failure.
    """
    dest = CACHE_DIR / safe_filename(content_id)
    meta = CACHE_DIR / meta_filename(content_id)

    # Reuse only when the on-disk JPEG matches this exact source URL.
    if dest.exists() and dest.stat().st_size > 512 and meta.exists():
        try:
            if meta.read_text().strip() == url:
                return content_id, str(dest)
        except OSError:
            pass

    sized_url = resize_amazon_url(url, CARD_WIDTH, CARD_HEIGHT)
    try:
        req = urllib.request.Request(
            sized_url,
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if len(data) < 512:
            return content_id, None  # suspiciously small – probably an error page
        dest.write_bytes(data)
        meta.write_text(url)
        return content_id, str(dest)
    except (urllib.error.URLError, OSError, TimeoutError):
        return content_id, None


def main() -> None:
    # Read items from stdin
    try:
        items: list[dict] = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"ERROR\tinvalid JSON input: {exc}", flush=True)
        sys.exit(1)

    if not items:
        return

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(download_image, item["content_id"], item["url"]): item["content_id"]
            for item in items
            if isinstance(item.get("url"), str) and item["url"].startswith("http")
        }
        for future in as_completed(futures):
            try:
                content_id, path = future.result()
            except Exception:
                content_id = futures[future]
                path = None

            if path:
                print(f"CACHED\t{content_id}\t{path}", flush=True)
            else:
                print(f"FAILED\t{content_id}", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Render app icons from public/logo.svg."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import cairosvg
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "public" / "logo.svg"
ICONS = ROOT / "src-tauri" / "icons"
PUBLIC = ROOT / "public"


def render_png(size: int) -> Image.Image:
    png_bytes = cairosvg.svg2png(
        bytestring=SVG.read_bytes(),
        output_width=size,
        output_height=size,
    )
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    background = Image.new("RGBA", img.size, (16, 27, 45, 255))
    return Image.alpha_composite(background, img)


def save(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)


def main() -> None:
    if not SVG.exists():
        raise SystemExit(f"Missing source SVG: {SVG}")

    # Master icon for `tauri icon` (generates icns/ico + standard sizes).
    master = PUBLIC / "app-icon.png"
    save(render_png(1024), master)
    print(f"wrote {master}")

    for size in (32, 64, 128, 256, 512):
        out = ICONS / (f"{size}x{size}.png" if size != 512 else "icon.png")
        save(render_png(size), out)
        print(f"wrote {out}")

    save(render_png(64), PUBLIC / "logo-64.png")
    print(f"wrote {PUBLIC / 'logo-64.png'}")

    # Generate platform bundles (icon.icns, icon.ico, @2x variants).
    try:
        subprocess.run(
            ["npx", "tauri", "icon", str(master), "-o", str(ICONS)],
            cwd=ROOT,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        print(f"warning: tauri icon failed ({err}); PNGs still updated", file=sys.stderr)


if __name__ == "__main__":
    main()
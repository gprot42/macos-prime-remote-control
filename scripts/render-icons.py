#!/usr/bin/env python3
"""Render app icons from public/logo.svg as opaque 8-bit RGBA PNGs."""

from __future__ import annotations

import io
from pathlib import Path

import cairosvg
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "public" / "logo.svg"
OUT_DIRS = [
    (ROOT / "src-tauri" / "icons", [32, 128, 256, 512]),
    (ROOT / "public", [64]),
]


def render_png(size: int) -> Image.Image:
    png_bytes = cairosvg.svg2png(
        bytestring=SVG.read_bytes(),
        output_width=size,
        output_height=size,
    )
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    # Flatten any residual transparency onto the icon background color.
    background = Image.new("RGBA", img.size, (30, 58, 95, 255))
    return Image.alpha_composite(background, img)


def save(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)


def main() -> None:
    if not SVG.exists():
        raise SystemExit(f"Missing source SVG: {SVG}")

    for out_dir, sizes in OUT_DIRS:
        for size in sizes:
            img = render_png(size)
            if size == 512:
                name = "icon.png"
            elif out_dir.name == "public":
                name = f"logo-{size}.png"
            else:
                name = f"{size}x{size}.png"
            save(img, out_dir / name)
            print(f"wrote {out_dir / name}")


if __name__ == "__main__":
    main()
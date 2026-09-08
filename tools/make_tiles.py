#!/usr/bin/env python3
"""Turn one big map image into a proper zoom pyramid.

Serving a single large image and letting the browser scale it is why panning
felt slow. A pyramid means the browser only ever fetches 256px tiles at the
resolution it is actually showing.

    python tools/make_tiles.py lands_between.png
    python tools/make_tiles.py m1-underground.png --out viewer/tiles-underground

Uses pyvips if available (fastest by far), otherwise Pillow. WebP output is
roughly a third the size of PNG at visually identical quality.

The underground is a second pyramid rather than a second projection: Siofra,
Ainsel and Deeproot sit under the Lands Between at the same world
coordinates, so the same [projection] places a route on either image -- as
long as the two images are the same crop at the same scale, which is the
thing to check before believing it.
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "viewer" / "tiles"


def with_pyvips(src: Path, out: Path, tile_size: int, quality: int) -> bool:
    try:
        import pyvips
    except ImportError:
        return False
    print("using pyvips")
    img = pyvips.Image.new_from_file(str(src), access="sequential")
    img.dzsave(
        str(out / "pyramid"),
        layout="google",
        suffix=f".webp[Q={quality}]",
        tile_size=tile_size,
        overlap=0,
        background=[0, 0, 0, 0],
    )
    made = out / "pyramid"
    for child in made.iterdir():
        shutil.move(str(child), str(out / child.name))
    made.rmdir()
    return True


def with_pillow(src: Path, out: Path, tile_size: int, quality: int) -> bool:
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    print("using Pillow (slower; install pyvips for large images)")
    img = Image.open(src).convert("RGBA")
    w, h = img.size
    levels = max(1, math.ceil(math.log2(max(w, h) / tile_size)) + 1)

    for z in range(levels):
        scale = 2 ** (levels - 1 - z)
        lw, lh = max(1, w // scale), max(1, h // scale)
        level = img.resize((lw, lh), Image.LANCZOS)
        cols = math.ceil(lw / tile_size)
        rows = math.ceil(lh / tile_size)
        print(f"  zoom {z}: {lw}x{lh}  ({cols}x{rows} tiles)")
        for cx in range(cols):
            d = out / str(z) / str(cx)
            d.mkdir(parents=True, exist_ok=True)
            for cy in range(rows):
                box = (
                    cx * tile_size, cy * tile_size,
                    min((cx + 1) * tile_size, lw), min((cy + 1) * tile_size, lh),
                )
                tile = level.crop(box)
                if tile.size != (tile_size, tile_size):
                    canvas = Image.new("RGBA", (tile_size, tile_size), (0, 0, 0, 0))
                    canvas.paste(tile, (0, 0))
                    tile = canvas
                tile.save(d / f"{cy}.webp", "WEBP", quality=quality, method=4)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--tile-size", type=int, default=256)
    ap.add_argument("--quality", type=int, default=82)
    ap.add_argument("--clean", action="store_true", help="wipe existing tiles first")
    ap.add_argument(
        "--out", type=Path, default=OUT,
        help="where the pyramid goes; viewer/tiles-underground for the "
             "Siofra/Ainsel map, which the viewer's Underground button looks "
             "for by name",
    )
    a = ap.parse_args()

    out = a.out if a.out.is_absolute() else ROOT / a.out

    if not a.image.exists():
        print(f"no such file: {a.image}")
        return 1
    if a.clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    ok = with_pyvips(a.image, out, a.tile_size, a.quality)
    if not ok:
        ok = with_pillow(a.image, out, a.tile_size, a.quality)
    if not ok:
        print("install one of: pip install pyvips  /  pip install pillow")
        return 1

    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        w, h = Image.open(a.image).size
    except Exception:
        w = h = None

    print(f"\ntiles written to {out}")
    if w:
        # One projection and one set of image bounds serve both planes, so a
        # second map of a different size would be clipped to the first one's.
        # Worth saying at the point where it could still be fixed.
        print("set these in config.toml under [viewer]:")
        print(f"  image_width  = {w}")
        print(f"  image_height = {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Stitch Elden Ring's extracted map tiles into one image.

The game stores its map as 256px tiles named like:

    MENU_MapTile_M00_L0_16_16_0000000c.png
                 |   |  |  |  |
                 |   |  |  |  +-- map-fragment flags (8 hex digits)
                 |   |  |  +----- Y, 00 at the BOTTOM
                 |   |  +-------- X, 00 at the left
                 |   +----------- detail level, L0 is most detailed
                 +--------------- M00 overworld, M01 underground

Two wrinkles this handles:

Multiple variants per coordinate. The flags say which map fragments the player
has, and the archive stores a separate tile for each combination. For a fully
revealed map you want the variant with the most flag bits set. That also
happens to skip the known junk tiles, which carry fewer bits than the real
one at the same coordinate.

Y runs bottom-to-top, the opposite of image rows, so it gets flipped.

    python tools/build_map.py path/to/extracted/tiles -o lands_between.png
    python tools/build_map.py path/to/extracted/tiles -o underground.png --map M01

Then feed the result to make_tiles.py.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

NAME = re.compile(
    r"MENU_MapTile_M(?P<m>\d{2})_L(?P<l>\d)_(?P<x>\d{2})_(?P<y>\d{2})_(?P<flags>[0-9A-Fa-f]{8})",
    re.IGNORECASE,
)

# Fragment names, useful for reporting what a chosen tile covers.
FRAGMENTS = {
    "M00": {
        0x1: "Limgrave W", 0x2: "Weeping Peninsula", 0x4: "Limgrave E",
        0x8: "Liurnia E", 0x10: "Liurnia N", 0x20: "Liurnia W",
        0x40: "Altus Plateau", 0x80: "Leyndell", 0x100: "Mt Gelmir",
        0x200: "Caelid", 0x400: "Dragonbarrow",
        0x800: "Mountaintops W", 0x1000: "Mountaintops E",
        0x2000: "Consecrated Snowfield",
    },
    "M01": {
        0x1: "Lake of Rot", 0x2: "Ainsel River", 0x4: "Mohgwyn Palace",
        0x8: "Siofra River", 0x10: "Deeproot Depths",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path, help="folder of extracted map tile images")
    ap.add_argument("-o", "--output", type=Path, default=Path("map.png"))
    ap.add_argument("--map", default="M00", choices=["M00", "M01"],
                    help="M00 overworld (default), M01 underground")
    ap.add_argument("--level", type=int, default=0,
                    help="detail level; 0 is highest quality and largest")
    ap.add_argument("--tile-size", type=int, default=256)
    a = ap.parse_args()

    try:
        from PIL import Image
    except ImportError:
        print("needs Pillow:  pip install Pillow")
        return 1
    Image.MAX_IMAGE_PIXELS = None

    if not a.folder.is_dir():
        print(f"not a folder: {a.folder}")
        return 1

    want_m = a.map[1:]
    # coordinate -> list of (flag_popcount, flags, path)
    grid: dict[tuple[int, int], list] = defaultdict(list)
    seen_levels: set[int] = set()
    scanned = 0

    for p in a.folder.rglob("*"):
        if p.suffix.lower() not in (".png", ".dds", ".tif", ".tiff", ".tga", ".jpg"):
            continue
        m = NAME.search(p.name)
        if not m:
            continue
        scanned += 1
        seen_levels.add(int(m["l"]))
        if m["m"] != want_m or int(m["l"]) != a.level:
            continue
        flags = int(m["flags"], 16)
        grid[(int(m["x"]), int(m["y"]))].append((bin(flags).count("1"), flags, p))

    if not scanned:
        print("no files matching the MENU_MapTile_... naming scheme were found.")
        print("Point this at the folder of converted PNG/DDS tiles, not the")
        print("packed .tpfbhd archive. See the README for the extraction steps.")
        return 1
    if not grid:
        print(f"scanned {scanned} tiles, but none for {a.map} at level {a.level}.")
        print(f"levels present: {sorted(seen_levels) or 'none'}")
        return 1

    xs = [c[0] for c in grid]
    ys = [c[1] for c in grid]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    cols, rows = x1 - x0 + 1, y1 - y0 + 1
    ts = a.tile_size
    print(f"{a.map} level {a.level}: {len(grid)} coordinates, {cols}x{rows} grid")

    variants = sum(len(v) for v in grid.values())
    print(f"{variants} tile variants; picking the most-revealed one per coordinate")

    canvas = Image.new("RGBA", (cols * ts, rows * ts), (0, 0, 0, 0))
    placed = failed = 0

    for (gx, gy), options in sorted(grid.items()):
        # Most flag bits set == most map fragments revealed.
        _, flags, path = max(options, key=lambda o: (o[0], o[1]))
        try:
            tile = Image.open(path).convert("RGBA")
        except Exception as e:
            print(f"  could not read {path.name}: {e}")
            failed += 1
            continue
        if tile.size != (ts, ts):
            tile = tile.resize((ts, ts), Image.LANCZOS)
        # Y counts up from the bottom; image rows count down from the top.
        canvas.paste(tile, ((gx - x0) * ts, (y1 - gy) * ts))
        placed += 1

    print(f"placed {placed} tiles" + (f", {failed} failed" if failed else ""))

    a.output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(a.output)
    print(f"\nwrote {a.output}  ({canvas.width}x{canvas.height})")
    print("\nnext:")
    print(f"  python tools/make_tiles.py {a.output} --clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Solve the world-metres to map-pixels transform.

Rather than hardcoding constants that depend on whichever map image you
tiled, you stand at two recognisable landmarks, note where they are on your
map image, and this fits the transform.

    python tools/calibrate.py --db data/routes.db

Pick two points as far apart as possible, ideally diagonal across the map --
the further apart they are, the less any pixel-picking error matters.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.coords import MapConfig, MapId, solve_projection  # noqa: E402
from tracker.store import Store  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "routes.db"))
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "config.toml")
    a = ap.parse_args()

    with open(a.config, "rb") as f:
        cfg = tomllib.load(f)
    maps = MapConfig(cfg)
    store = Store(a.db)

    print("Calibration\n")
    print("For each point: stand somewhere recognisable in game, let it record,")
    print("then find that same spot on your map image and read off its pixel")
    print("coordinates (any image editor shows them as you hover).\n")
    print("Two points is enough, and further apart is better. Type 'done' when")
    print("you have them.\n")

    pairs: list[tuple[float, float, float, float]] = []
    while True:
        n = len(pairs) + 1
        try:
            raw = input(
                f"point {n} -- press Enter to capture your position, or 'done' to finish: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n\ncancelled; nothing written.")
            store.close()
            return 1

        if raw in ("done", "d", "q", "quit", "stop", "exit"):
            if len(pairs) >= 2:
                break
            print(f"  need at least two points, have {len(pairs)}.")
            continue

        row = store.db.execute(
            "SELECT map_id, x, y, z, wx, wz FROM samples "
            "WHERE wx IS NOT NULL ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("  nothing recorded yet -- is `record --source game` running?")
            continue

        m = MapId.unpack(row["map_id"])
        print(f"  captured in {m}, world position {row['wx']:.1f}, {row['wz']:.1f}")
        if any(abs(row["wx"] - p[0]) < 1 and abs(row["wz"] - p[1]) < 1 for p in pairs):
            print("  that's the same spot as an earlier point. Move somewhere")
            print("  further away first -- the fit needs separation.")
            continue
        def ask_pixel(axis: str):
            """Keep asking rather than throwing away the captured position."""
            while True:
                try:
                    v = input(f"  pixel {axis} on your map image (or 'skip'): ").strip()
                except (EOFError, KeyboardInterrupt):
                    return None
                if v.lower() in ("skip", "s", "cancel"):
                    return None
                try:
                    return float(v)
                except ValueError:
                    print("    needs a number. Open your map image in any editor and")
                    print("    hover the same landmark to read its pixel coordinates.")

        px = ask_pixel("x")
        if px is None:
            print("  point discarded.\n")
            continue
        py = ask_pixel("y")
        if py is None:
            print("  point discarded.\n")
            continue
        pairs.append((row["wx"], row["wz"], px, py))
        print(f"  point {len(pairs)} saved.\n")

    result = solve_projection(pairs)
    print("\nPaste this into config.toml, replacing the [projection] section:\n")
    print("[projection]")
    for k in ("scale_x", "offset_x", "scale_y", "offset_y"):
        print(f"{k:9s}= {result[k]:.6f}")

    resid = []
    for wx, wz, px, py in pairs:
        ex = wx * result["scale_x"] + result["offset_x"]
        ey = wz * result["scale_y"] + result["offset_y"]
        resid.append(((ex - px) ** 2 + (ey - py) ** 2) ** 0.5)
    sx, sy = abs(result["scale_x"]), abs(result["scale_y"])
    skew = abs(sx - sy) / max(sx, sy)
    if len(pairs) < 3:
        print("\nWith two points the fit is exact by construction, so the residual")
        print("proves nothing. Add a third point if you want a real check.")
    else:
        print(f"\nworst point is {max(resid):.1f} px off. Under a few px is fine.")
    if skew > 0.05:
        print(f"\nWARNING: |scale_x| and |scale_y| differ by {skew*100:.0f}%.")
        print("They should match, since the map is not stretched on one axis.")
        print("One of your points is probably wrong. Recalibrate further apart.")
    if result["scale_x"] < 0:
        print("\nWARNING: scale_x is negative, which is unexpected.")

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

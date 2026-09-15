#!/usr/bin/env python3
"""Re-file samples recorded before the underground rivers were a plane.

Siofra, Ainsel and Deeproot are area 12: their own maps, with their own
origins, not rooms inside the overworld. Until `underground_areas` existed
they matched `area_labels` and were classified as interiors -- stored with a
NULL world position and drawn like a legacy dungeon lying on top of Limgrave,
which is what a session recorded in Siofra looks like on the surface map.

Nothing is lost in that: the local coordinates were always recorded. This
pass reclassifies those rows and fills in the world position from the origin
in `[maps.area_origin]`, so an existing route appears on the underground map
without being walked again. Run it whenever an origin is added or corrected:
a map it could not place before becomes placeable the moment there is one, and
a map recorded under the wrong origin is moved to the right one.

    python tools/repair_underground.py            # say what would change
    python tools/repair_underground.py --write    # change it
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tracker.coords import MapConfig, MapId, UNDERGROUND  # noqa: E402
from tracker.main import load_config  # noqa: E402
from tracker.store import Store, kept_a_copy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "routes.db"))
    ap.add_argument("--config", default=str(ROOT / "config" / "config.toml"))
    ap.add_argument("--write", action="store_true",
                    help="apply the changes; without it, only report them")
    a = ap.parse_args()

    cfg = load_config(a.config)
    maps = MapConfig(cfg)
    if not maps.underground_areas:
        print("no underground_areas configured; nothing to do")
        return 0

    store = Store(a.db)
    db = store.db

    areas = ",".join(str(int(x)) for x in sorted(maps.underground_areas))
    rows = db.execute(
        f"SELECT id, map_id, layer, x, z, wx, wz FROM samples "
        f"WHERE ((map_id >> 24) & 255) IN ({areas})"
    ).fetchall()
    if not rows:
        print("no samples in any underground area; nothing to do")
        return 0

    placed: dict[int, int] = {}
    unplaced: dict[int, int] = {}
    moved: dict[int, float] = {}
    relayered = 0
    updates = []
    for r in rows:
        m = MapId.unpack(r["map_id"])
        world = maps.to_world(m, r["x"], r["z"])
        if world is None:
            unplaced[r["map_id"]] = unplaced.get(r["map_id"], 0) + 1
        else:
            placed[r["map_id"]] = placed.get(r["map_id"], 0) + 1
        if r["layer"] != UNDERGROUND:
            relayered += 1
        wx, wz = (world if world else (None, None))
        # The config is what says where an underground map sits, so a row
        # whose world position is not the one the config now computes is
        # out of date -- whether it was never placed, or placed at an
        # origin that has since been corrected. Rewriting only the
        # never-placed rows meant that adding [maps.map_origin] for a map
        # already recorded under its area's origin changed nothing at
        # all: every row kept the wrong position it was written with, and
        # the pass reported "0 rows change" for a map drawn 4 km out.
        # Compared exactly, because both sides are the same sum of the
        # same two doubles -- an unchanged origin cannot differ in a bit.
        stale = (wx != r["wx"])
        if stale and wx is not None and r["wx"] is not None:
            moved[r["map_id"]] = math.dist((wx, wz), (r["wx"], r["wz"]))
        if r["layer"] != UNDERGROUND or stale:
            updates.append((UNDERGROUND, wx, wz, r["id"]))

    for map_id, n in sorted(placed.items()):
        m = MapId.unpack(map_id)
        origin = maps.underground_origin(m)
        print(f"  {m}: {n} samples placed at "
              f"({origin[0]:.2f}, {origin[1]:.2f})")
    for map_id, d in sorted(moved.items()):
        m = MapId.unpack(map_id)
        print(f"  {m}: moves {d:.0f} m to the origin now configured "
              f"for it")
    for map_id, n in sorted(unplaced.items()):
        m = MapId.unpack(map_id)
        print(f"  {m}: {n} samples have no origin, so they stay off the "
              f"map. Walk into it on foot and the recorder prints the value "
              f"to add under [maps.area_origin].")
    print(f"\n{relayered} sample(s) move from their old layer to "
          f"'{UNDERGROUND}', {len(updates)} row(s) change in total")

    # The map events say which layer a map is on too, and interior_visits()
    # reads them: left alone, Siofra keeps its marker and its dungeon drawing
    # on the surface even after the samples move.
    ev = db.execute(
        f"SELECT COUNT(*) n FROM map_events "
        f"WHERE ((map_id >> 24) & 255) IN ({areas}) AND layer != ?",
        (UNDERGROUND,),
    ).fetchone()["n"]
    print(f"{ev} map event(s) change layer as well")

    if not a.write:
        print("\nnothing written. Pass --write to apply.")
        return 0

    kept_a_copy(store, "underground")
    db.executemany(
        "UPDATE samples SET layer = ?, wx = ?, wz = ? WHERE id = ?", updates
    )
    db.execute(
        f"UPDATE map_events SET layer = ? "
        f"WHERE ((map_id >> 24) & 255) IN ({areas})",
        (UNDERGROUND,),
    )
    db.commit()
    print("\nwritten. Restart the recorder to serve the change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

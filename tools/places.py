#!/usr/bin/env python3
"""Where every place the recording has been inside is drawn, and how it knows.

    python tools/places.py
    python tools/places.py --todo          # only the ones still to do
    python tools/places.py --db other.db

Most dungeons place themselves: the step across the threshold is a surface
position and a local one a moment apart, so walking in through a door measures
where the door is. Some cannot. A Divine Tower entered from inside Stormveil
and left the same way never touches the surface; the Roundtable Hold has no way
in on foot anywhere in the game. Those are placed by hand, by dragging the
marker -- and a route that ships with a database wants that done for all of
them, or somebody else's copy draws those places nowhere.

This is the list to work through. It says which places have a position, where
it came from, and which have none at all.

The tiers, strongest first:

    nowhere        you have said this place is not anywhere in the world
    entrance       the surface position recorded walking in       (measured)
    exit           the first surface position after coming out    (measured)
    by hand        you dragged its marker                         (a guess)
    another visit  borrowed from another visit to the same place
    the doorway    worked out from where you left the last dungeon
    the way in     the place you came in from -- the right part of the map
    unknown        nothing in the recording says where this is

`by hand` sits below the two measured tiers: a drag is somebody's best guess at
where a doorway is, and the recorder measuring one is not. Walk in through a
door and the guess retires itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.coords import MapConfig, MapId  # noqa: E402
from tracker.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# What each tier is worth. Everything from `by hand` down is somebody's guess
# or an inference; the two above it are readings.
MEASURED = ("entrance", "exit")
ORDER = ["nowhere", "entrance", "exit", "by hand", "from config",
         "another visit", "the doorway", "the way in", "unknown"]

WHY = {
    "nowhere": "you have said it is not anywhere in the world",
    "entrance": "measured walking in",
    "exit": "measured coming out",
    "by hand": "you dragged it here",
    "from config": "put here in config.toml, and ships with the tool",
    "another visit": "borrowed from another visit",
    "the doorway": "worked out from the dungeon you came from",
    "the way in": "the place you came in from -- roughly right",
    "unknown": "nothing in the recording says where it is",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "routes.db"))
    ap.add_argument("--config", default=str(ROOT / "config" / "config.toml"))
    ap.add_argument("--todo", action="store_true",
                    help="only the places with no position on the map")
    args = ap.parse_args()

    store = Store(Path(args.db))
    try:
        import tomllib
        with open(args.config, "rb") as fh:
            cfg = tomllib.load(fh)
    except Exception as e:                       # noqa: BLE001
        print(f"could not read {args.config}: {e}")
        return 1
    maps = MapConfig(cfg)
    # The positions that ship with the tool, the same way the server hands
    # them over: without this the tool would report a place as unplaced that
    # every viewer draws.
    store.shipped = dict(maps.map_places)
    names = store.names()

    # One row per place, not per visit: where a dungeon is is a fact about the
    # dungeon. The tier is the best any visit to it achieved, which is what
    # the viewer draws every visit in.
    places: dict[int, dict] = {}
    for v in store.interior_visits():
        # Interiors only. A visit is any stay in any map, so the list also
        # holds every overworld tile you have walked across -- those are the
        # world plane and are drawn by the route itself.
        if v["layer"] not in ("interior", "unknown"):
            continue
        p = places.setdefault(v["map_id"], {
            "map_id": v["map_id"], "map": str(MapId.unpack(v["map_id"])),
            "visits": 0,
            "tier": "unknown", "wx": None, "wz": None, "seconds": 0.0,
        })
        p["visits"] += 1
        p["seconds"] += (v["duration_ms"] or 0) / 1000.0
        if ORDER.index(v["placed"]) < ORDER.index(p["tier"]):
            p["tier"] = v["placed"]
            p["wx"], p["wz"] = v["wx"], v["wz"]

    rows = sorted(places.values(),
                  key=lambda p: (ORDER.index(p["tier"]), -p["seconds"]))
    todo = [p for p in rows if p["wx"] is None and p["tier"] != "nowhere"]
    guessed = [p for p in rows
               if p["wx"] is not None and p["tier"] not in MEASURED
               and p["tier"] != "nowhere"]

    def label(p):
        m = MapId.unpack(p["map_id"])
        return names.get(p["map_id"], maps.label(m))

    def show(group, title):
        if not group:
            return
        print(f"\n{title}")
        for p in group:
            where = ("" if p["wx"] is None
                     else f"  at {p['wx']:8.0f},{p['wz']:8.0f}")
            mins = p["seconds"] / 60.0
            print(f"  {label(p)[:24]:24} {p['map']:15} "
                  f"{p['visits']:3} visit{'s' if p['visits'] != 1 else ' '} "
                  f"{mins:5.0f} min  {p['tier']:14}{where}")

    show(todo, "NO POSITION ON THE MAP -- these are the ones to place")
    if not args.todo:
        show(guessed, "PLACED, BUT NOT FROM A DOORWAY THE ROUTE WALKED")
        show([p for p in rows if p["tier"] in MEASURED],
             "PLACED BY THE RECORDING")
        show([p for p in rows if p["tier"] == "nowhere"],
             "NOT ANYWHERE IN THE WORLD")

    # The line to paste, for anything standing on a drag: a drag lives in
    # this database and nowhere else, and a released route wants it in
    # config where it travels with the tool.
    hands = [p for p in rows if p["tier"] == "by hand"]
    if hands:
        print("\nTo ship these with the tool rather than with this "
              "database, put them in [maps.map_place] in config.toml:\n")
        for p in hands:
            print('  "%s" = [%.2f, %.2f]   # %s'
                  % (p["map"], p["wx"], p["wz"], label(p)))
    print()
    print(f"{len(rows)} place(s) recorded, {len(todo)} with no position, "
          f"{len(guessed)} placed by something other than a doorway.")
    if todo:
        print("Open the map, find each one in the corner of the screen, and "
              "press Put on map -- or walk in through its door and it places "
              "itself.")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

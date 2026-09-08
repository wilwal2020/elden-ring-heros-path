#!/usr/bin/env python3
"""Fold old per-session route files into the single database.

    python tools/import_legacy.py ~/Downloads/ER_Route_Tracker/routes/*.json

Each input file becomes one session, so old runs join the continuous path
instead of staying stranded in separate files.

The points are replayed through the same Sampler the live recorder uses rather
than being written to the database directly. Writing rows here would mean a
second copy of the rules that matter -- teleport breaks, the movement gate,
load screens dropped, interiors kept with no world position -- and two copies
drift. Replaying means an imported route and a recorded one are the same thing
by construction.

Handles a few common shapes (a list of points, or an object with a
"points"/"path"/"positions" key) and says what it saw if it can't find
coordinates.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.coords import MapConfig, MapId, is_no_map  # noqa: E402
from tracker.sampler import Sampler  # noqa: E402
from tracker.source import Reading  # noqa: E402
from tracker.store import Store  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
POINT_KEYS = ("points", "path", "positions", "samples", "route", "data")


class ReplaySource:
    """Feeds saved points to the Sampler as if they were arriving live."""

    def __init__(self, name: str, readings: list[tuple[int, Reading]]):
        self.name = name
        self.readings = readings
        self.i = 0
        # The Sampler asks for the time through clock(); it has to be the
        # time this point was recorded, not now, or every gap turns into an
        # implausible speed and the whole route draws as broken segments.
        self.ts = readings[0][0] if readings else 0

    def describe(self) -> str:
        return f"imported from {self.name}"

    def clock(self) -> int:
        return self.ts

    def read(self) -> Optional[Reading]:
        if self.i >= len(self.readings):
            return None
        self.ts, r = self.readings[self.i]
        self.i += 1
        return r

    def gone(self) -> bool:
        # A file does not quit.
        return False

    def close(self) -> None:
        pass


def extract_points(doc) -> list[dict]:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for k in POINT_KEYS:
            if isinstance(doc.get(k), list):
                return doc[k]
    return []


def get(p: dict, *names, default=None):
    for n in names:
        if n in p:
            return p[n]
        if n.upper() in p:
            return p[n.upper()]
    return default


def to_readings(points: list[dict]) -> tuple[list[tuple[int, Reading]], int]:
    """(timestamp, Reading) pairs in time order, plus the count of unusable rows.

    Local x/y/z is what gets read, never any world position the old tool
    stored alongside it: the local pair plus the map ID is the input this
    project's coordinate handling is built on, and it is the only version that
    is right for interiors.
    """
    out: list[tuple[int, Reading]] = []
    bad = 0
    for i, p in enumerate(points):
        if not isinstance(p, dict):
            bad += 1
            continue
        x = get(p, "x")
        y = get(p, "y", default=0.0)
        z = get(p, "z")
        if x is None or z is None:
            bad += 1
            continue
        raw_map = get(p, "map_id", "mapId", "map", default=0)
        if isinstance(raw_map, str):
            try:
                raw_map = int(raw_map.replace("m", "").replace("_", ""), 16)
            except ValueError:
                bad += 1
                continue
        ts = int(get(p, "timestamp_ms", "ts_ms", "ts", "time", "timestamp",
                     default=i * 250))
        if ts < 10_000_000_000:  # looks like seconds
            ts *= 1000
        out.append(
            (ts, Reading(map_id=int(raw_map), x=float(x), y=float(y), z=float(z)))
        )
    out.sort(key=lambda pair: pair[0])
    return out, bad


def world_check(maps: MapConfig, points: list[dict],
                readings: list[tuple[int, Reading]]) -> Optional[float]:
    """How far our world position lands from the one the old tool stored.

    Only meaningful for open-world points. A few centimetres means the two
    agree about the coordinate space; metres mean they do not, and the import
    should not be trusted without a look.
    """
    worst = None
    for p in points:
        if not isinstance(p, dict):
            continue
        gx, gz = get(p, "global_x"), get(p, "global_z")
        x, z = get(p, "x"), get(p, "z")
        raw = get(p, "map_id", default=0)
        if gx is None or gz is None or x is None or z is None:
            continue
        if not isinstance(raw, int) or is_no_map(raw):
            continue
        w = maps.to_world(MapId.unpack(raw), float(x), float(z))
        if w is None:
            continue
        d = math.dist(w, (float(gx), float(gz)))
        worst = d if worst is None else max(worst, d)
    return worst


def already_imported(store: Store, readings: list) -> tuple[int, int]:
    """How many of these points are already in the database.

    The old tool saved cumulative snapshots: route_15-31, route_15-45 and
    route_15-47 from one afternoon are nested, each containing every point of
    the last. Importing all three draws that afternoon three times, and there
    is no sign of it afterwards except a path that looks oddly heavy. Matching on
    the timestamp alone is enough and deliberately strict: these are exact
    millisecond values from one clock, so a handful of matches is proof, not
    coincidence. Only a fraction match even for a file that is entirely a
    duplicate, because the movement gate dropped the rest on the way in.
    """
    stamps = [ts for ts, _ in readings]
    found = 0
    for i in range(0, len(stamps), 400):
        chunk = stamps[i:i + 400]
        marks = ",".join("?" * len(chunk))
        found += store.db.execute(
            f"SELECT COUNT(*) c FROM samples WHERE ts_ms IN ({marks})", chunk
        ).fetchone()["c"]
    return found, len(stamps)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--db", default=str(ROOT / "data" / "routes.db"))
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "config.toml")
    ap.add_argument("--dry-run", action="store_true",
                    help="read and report, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="import even if these points are already in the database")
    a = ap.parse_args()

    with open(a.config, "rb") as f:
        cfg = tomllib.load(f)
    maps = MapConfig(cfg)
    store = Store(a.db)
    total = 0

    for path in a.files:
        if not path.exists():
            print(f"skip {path.name}: not found")
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"skip {path.name}: {e}")
            continue

        points = extract_points(doc)
        if not points:
            keys = list(doc)[:8] if isinstance(doc, dict) else type(doc).__name__
            print(f"skip {path.name}: no point list found (saw {keys})")
            continue

        readings, bad = to_readings(points)
        if not readings:
            print(f"skip {path.name}: {len(points)} entries, none with x and z")
            continue

        print(f"\n{path.name}: {len(points)} points")
        if bad:
            print(f"  {bad} entry(s) had no usable coordinates")

        worst = world_check(maps, points, readings)
        if worst is not None:
            if worst < 0.5:
                print(f"  world positions agree with the old file "
                      f"(worst {worst:.2f} m)")
            else:
                print(f"  WARNING: our world position differs from the old "
                      f"file's by up to {worst:.1f} m. The two disagree about "
                      f"the coordinate space; check [maps] position_space "
                      f"before trusting this route.")

        seen, of = already_imported(store, readings)
        if seen >= 3 and not a.force:
            print(f"  skipped: {seen} of these {of} points are already in the "
                  f"database, matched on exact timestamps. This file has been "
                  f"imported, or it is a shorter snapshot of one that has -- "
                  f"the old tool saved cumulative files, so an early one is "
                  f"usually contained in a later one. Pass --force to import "
                  f"it anyway.")
            continue
        if seen:
            print(f"  note: {seen} of {of} points already appear in the "
                  f"database")

        if a.dry_run:
            interiors = sum(
                1 for _, r in readings
                if not is_no_map(r.map_id)
                and maps.to_world(MapId.unpack(r.map_id), r.x, r.z) is None
            )
            blanks = sum(1 for _, r in readings if is_no_map(r.map_id))
            print(f"  would import: {len(readings) - blanks} readings "
                  f"({interiors} inside dungeons, {blanks} load screens dropped)")
            continue

        source = ReplaySource(path.name, readings)
        try:
            sampler = Sampler(source, store, cfg, clock=source.clock)
            for _ in range(len(readings)):
                sampler.step()
            sampler.finish()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e):
                raise
            print(f"  could not write: {e}.")
            print("  Something else is holding the database open for writing, "
                  "almost always a `record` still running. Stop it with "
                  "Ctrl-C, which commits what it has, then run this again.")
            store.close()
            return 1

        c = sampler.counts
        total += c["samples"]
        print(f"  session {sampler.session_id}: {c['samples']} points kept, "
              f"{c['interior_samples']} of them inside dungeons")
        print(f"  {c['breaks']} line break(s), {c['events']} map change(s), "
              f"{c['skipped']} reading(s) dropped as load screens or "
              f"standing still")

    if a.dry_run:
        print("\ndry run: nothing written.")
    else:
        print(f"\nimported {total} points. They now share one continuous path.")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

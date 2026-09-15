#!/usr/bin/env python3
"""End-to-end check: simulate a route, serve it, hit every endpoint.

Run this after setup to confirm the pipeline works before you touch the game.

    python tools/selftest.py
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

from tracker.coords import MapConfig, MapId, is_no_map, solve_projection  # noqa: E402
from tracker.launch import (  # noqa: E402
    LAUNCHERS, PROCESS_NAME, find_game, steam_libraries,
)
from tracker.sampler import Sampler  # noqa: E402
from tracker.server import (  # noqa: E402
    Server, rdp, tile_levels_present, tile_pyramid_extent, tile_warnings,
)
from tracker.source import Reading, SimSource  # noqa: E402
from tracker.store import BREAK_RELOAD, BREAK_SPEED, Store  # noqa: E402
import import_legacy  # noqa: E402  (tools/, added to the path above)
import repair_jumps  # noqa: E402
import repair_underground  # noqa: E402

PORT = 8912
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


def build_db(cfg: dict, path: Path, steps: int = 20000) -> Store:
    try:
        if path.exists():
            path.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                p.unlink()
    except PermissionError:
        # Windows won't unlink a file another process has open, and the usual
        # culprit is a viewer left running against this same database.
        raise SystemExit(
            f"{path} is open in another process. Stop any 'tracker serve --db "
            f"{path}' still running and try again."
        )
    store = Store(path)
    import tracker.sampler as sp
    import tracker.store as st

    clock = [1_700_000_000_000]
    st.now_ms = lambda: clock[0]
    sp.now_ms = st.now_ms
    s = Sampler(SimSource(cfg), store, cfg)
    for _ in range(steps):
        clock[0] += 250
        s.step()
    s.finish()
    print(f"  simulated {s.counts['samples']} points, "
          f"{s.counts['breaks']} breaks, {s.counts['events']} map changes")
    return store


def main() -> int:
    cfg = tomllib.load(open(ROOT / "config" / "config.toml", "rb"))

    print("\ncoordinates")
    m = MapId.unpack((60 << 24) | (43 << 16) | (36 << 8) | 0)
    check("map id round-trips", MapId.unpack(m.pack()) == m, str(m))
    maps = MapConfig(cfg)
    check("surface classified", maps.classify(m, 40.0) == "surface")
    check("underground classified", maps.classify(m, -200.0) == "underground")
    cave = MapId.unpack((31 << 24) | (5 << 16))
    check("cave classified as interior", maps.classify(cave, 0.0) == "interior")
    # A legacy dungeon is an interior too, but the world map draws the castle,
    # so its path is drawn there permanently rather than on hover.
    check("a legacy dungeon is part of the visible world",
          maps.is_world_visible(MapId.unpack(10 << 24)))
    check("a cave is not", not maps.is_world_visible(cave))
    check("the overworld is not an interior at all",
          not maps.is_world_visible(m))
    check("cave has no world position", maps.to_world(cave, 40, 40) is None)
    w = maps.to_world(m, 10.0, 20.0)
    check("tile expansion", w == (43 * 256 + 10, 36 * 256 + 20), str(w))

    print("\nprojection")
    p = solve_projection([(0, 0, 100, 200), (1000, 500, 1100, 700)])
    check("scale solved", abs(p["scale_x"] - 1.0) < 1e-9 and abs(p["scale_y"] - 1.0) < 1e-9)
    check("offset solved", abs(p["offset_x"] - 100) < 1e-9 and abs(p["offset_y"] - 200) < 1e-9)

    print("\nsimplification")
    line = [(i, 0.0, 0.0, 0) for i in range(500)]
    check("straight line collapses", len(rdp(line, 1.0)) == 2, f"{len(rdp(line, 1.0))} pts")
    import math
    wiggle = [(i, math.sin(i / 3) * 40, 0.0, 0) for i in range(5000)]
    check("detail preserved when needed", len(rdp(wiggle, 0.5)) > 1000)
    check("detail dropped when coarse", len(rdp(wiggle, 100.0)) < 50)

    print("\nlaunchers")
    # The way this gets started day to day is a double-click, not a terminal,
    # so the .bat files are part of the tool rather than a convenience lying
    # beside it: if one gets renamed or points at a command that no longer
    # exists, nobody finds out until the next time they want to play.
    # Finding the game must fail quietly rather than throw: a machine with no
    # Steam, or a path typed in wrong, is a message to the user, not a stack
    # trace in the middle of starting a session.
    check("a configured path that is not there finds nothing",
          find_game("D:/definitely/not/here/eldenring.exe") is None)
    check("looking for Steam libraries does not raise",
          isinstance(steam_libraries(), list))
    # Reading another process's memory is only safe offline with anti-cheat
    # off, so the launcher that gets you there is the one to prefer.
    check("the game is started offline by default",
          LAUNCHERS[0] == "start_game_in_offline_mode.exe",
          " then ".join(LAUNCHERS))
    check("whatever starts it, we wait for the game itself",
          PROCESS_NAME == "eldenring.exe")

    for name, command in (("Record route.bat", "record --source game --launch --open"),
                          ("Open map.bat", "serve --open")):
        f = ROOT / name
        body = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
        check(f"{name} launches the right thing",
              f"-m tracker.main {command}" in body,
              "missing" if not f.exists() else "")

    print("\nmap config")
    levels = tile_levels_present()
    if not levels:
        print("  --    no tiles generated yet; skipping map size checks")
    else:
        top = cfg["viewer"]["tile_max_zoom"]
        check("tile_max_zoom exists on disk", top in levels,
              f"levels {levels[0]}..{levels[-1]}")
        extent = tile_pyramid_extent(levels[-1])
        check("pyramid measurable", extent is not None,
              f"{extent[0]}x{extent[1]} tiles" if extent else "")
        # Leaflet clips the tile layer to [viewer] image_width/height, so a
        # stale value silently hides part of the map. This is the check that
        # would have caught 4096x4096 sitting in front of a 9728x9216 pyramid.
        warnings = tile_warnings(cfg)
        check("configured size matches the tiles", warnings == [],
              "; ".join(warnings)[:160])
        bad = copy.deepcopy(cfg)
        bad["viewer"]["image_width"] = 4096
        bad["viewer"]["image_height"] = 4096
        check("a wrong size is reported", tile_warnings(bad) != [])

        # The two planes share one projection and one set of image bounds,
        # because Siofra and Ainsel are at the same world coordinates as the
        # ground above them. That only works while the two images are the same
        # crop at the same scale; a second map of a different size is clipped
        # to the first one's bounds and the route lands on the wrong terrain,
        # which reads as a bad calibration rather than a mismatched image.
        under = ROOT / "viewer" / "tiles-underground"
        if under.is_dir():
            u = tile_pyramid_extent(levels[-1], "tiles-underground")
            check("the underground map is the same size as the surface",
                  u is not None and u == extent,
                  f"{u[0]}x{u[1]} against {extent[0]}x{extent[1]}"
                  if u else "no tiles at that zoom")
        else:
            print("  --    no underground tiles yet; skipping the plane check")

        # And the check itself works: a level the underground pyramid does not
        # have has to be reported, not silently drawn as blank tiles.
        missing = tile_pyramid_extent(99, "tiles-underground")
        check("a missing underground level measures as absent", missing is None)

    # The underground rivers are a plane, not a room. Siofra is m12_07: its
    # own map with its own origin, reached by a lift. Listed among the dungeon
    # areas it was classified as an interior, which drew the whole of Siofra
    # as a legacy dungeon lying on top of Limgrave.
    print("\nthe underground rivers")
    maps = MapConfig(cfg)
    siofra = MapId.unpack((12 << 24) | (7 << 16))
    check("an underground river is a plane, not a dungeon",
          maps.classify(siofra, -900.0) == "underground",
          maps.classify(siofra, -900.0))
    check("and is not drawn on the surface like a legacy dungeon",
          not maps.is_world_visible(siofra))
    # The origin is measured from the way in: the lift shaft is an open-world
    # position and the first reading below is the same place in the river's
    # own coordinates.
    origin = maps.underground_origin(siofra)
    check("Siofra has an origin to be drawn at", origin is not None,
          f"{origin}" if origin else "nothing for area 12")
    if origin:
        w = maps.to_world(siofra, 737.481, 1165.039)
        check("its local coordinates land where the lift came down",
              w is not None and abs(w[0] - 11616.4) < 1.0
              and abs(w[1] - 9486.2) < 1.0,
              f"({w[0]:.1f}, {w[1]:.1f}) against (11616.4, 9486.2)" if w else "")

    # Siofra and Nokron are one coordinate space, chunked the way the overworld
    # is: five crossings between them moved the local coordinates by the two
    # metres actually walked. So one measurement places the area, and crossing
    # between chunks must not break the line -- that put five holes in a
    # twelve-minute walk.
    nokron = MapId.unpack((12 << 24) | (2 << 16))
    check("one measurement places the whole underground",
          maps.underground_origin(nokron) == origin)
    w = maps.to_world(nokron, 959.92, 1326.65)
    check("crossing into the next chunk stays continuous",
          w is not None and abs(w[0] - 11835.84) < 10.0
          and abs(w[1] - 9652.08) < 10.0,
          f"({w[0]:.2f}, {w[1]:.2f}) against the step before at "
          f"(11835.84, 9652.08)" if w else "")
    check("and does not break the line",
          maps.same_plane(siofra, nokron))
    check("while a cave off it still does",
          not maps.same_plane(siofra, MapId.unpack((31 << 24) | (5 << 16))))

    # An area nobody has walked into has no origin, and inventing one would put
    # a path on terrain it was never on.
    blank = copy.deepcopy(cfg)
    blank["maps"]["area_origin"] = {}
    blank["maps"]["map_origin"] = {}
    check("a river with no measured origin is not placed",
          MapConfig(blank).to_world(siofra, 100.0, 100.0) is None)
    # Ground truth from the field: m18_00_00_00 is the Stranded Graveyard, the
    # cave you wake up in after the Chapel of Anticipation. Session 27 walks it
    # in order -- m10_01 at 17:25, m18_00 at 17:27, out onto m60_42_36 at
    # 17:39 -- so it is under the ground, and drawing its path permanently over
    # the terrain is the bug this whole tool exists to fix.
    check("the Stranded Graveyard is a cave, not a castle on the map",
          not maps.is_world_visible(MapId(18, 0, 0, 0)),
          f"drawn as {maps.classify(MapId(18, 0, 0, 0), 10.0)}, "
          f"world-visible {maps.is_world_visible(MapId(18, 0, 0, 0))}")

    # What someone sees when they unzip it. The two launchers are the whole of
    # what a new install has to understand, so the root holds those and
    # nothing else it has to read past. CLAUDE.md stays because that is where
    # Claude Code looks for it.
    print("\nthe shape of the folder")
    at_root = sorted(q.name for q in ROOT.iterdir()
                     if q.is_file() and not q.name.startswith("."))
    # README.md is here for GitHub, which renders the one at the root of a
    # repository and nothing else -- so without it the project page has no
    # description at all. It is a stub: what this is, the two files to press,
    # and links into docs/. Everything that could go stale lives in docs/.
    check("the root is the two launchers, the front page and the notes",
          at_root == ["CLAUDE.md", "Open map.bat", "README.md",
                      "Record route.bat"],
          ", ".join(at_root))
    stub = (ROOT / "README.md").read_text(encoding="utf-8")
    check("and the stub points into the folder rather than repeating it",
          all(f"docs/{d}" in stub
              for d in ("README.md", "SETUP.md", "MANUAL.md", "NOTICE.md"))
          and len(stub.splitlines()) < 60,
          f"{len(stub.splitlines())} lines")
    missing = [n for n in ("docs", "config", "tools", "tracker", "viewer")
               if not (ROOT / n).is_dir()]
    check("and everything else is in a folder", not missing,
          f"missing {missing}" if missing else "")
    for doc in ("README.md", "SETUP.md", "MANUAL.md", "NOTICE.md"):
        check(f"docs/{doc} is where the front page says it is",
              (ROOT / "docs" / doc).is_file())

    # Moving files breaks the links between them, and a dead link in the
    # install instructions is the one place it costs somebody an evening.
    broken = []
    for doc in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
        text = doc.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#:]+\.md)(?:#[^)]*)?\)", text):
            if not (doc.parent / target).resolve().is_file():
                broken.append(f"{doc.name} -> {target}")
    check("every link between the docs still resolves", not broken,
          "; ".join(broken) if broken else "")

    # And the paths the docs and the launcher tell you to type.
    setup = (ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")
    bat = (ROOT / "Record route.bat").read_text(encoding="utf-8")
    check("the requirements file is where they say it is",
          (ROOT / "tools" / "requirements.txt").is_file()
          and "tools/requirements.txt" in setup
          and "tools" + chr(92) + "requirements.txt" in bat)

    # The config is read top to bottom by whoever opens it, so what you might
    # change comes first and the addresses come last.
    toml = (ROOT / "config" / "config.toml").read_text(encoding="utf-8")
    tables = [t for t in ("[recording]", "[game]", "[projection]", "[viewer]",
                          "[maps]", "[memory]") if t in toml]
    order = [toml.index(t) for t in tables]
    check("the config puts the settings first and the addresses last",
          len(tables) == 6 and order == sorted(order),
          " then ".join(tables))
    check("and the everyday recording settings come before the measured ones",
          0 < toml.index("interval_s") < toml.index("max_speed_mps")
          < toml.index("[memory.pointers.player_position]"))

    check("the repair pass for routes recorded before this exists",
          (ROOT / "tools" / "repair_underground.py").exists())

    # A map that is placed can still be placed wrong, and saying so in the
    # console while drawing it anyway is not saying it. Reported from the
    # field: walking down into m12_01 drew the whole river at the bottom of
    # the map image, 3,937 m from the lift, because area 12's origin is
    # Siofra's and m12_01 does not share it. An unplaceable map is already
    # kept off the map rather than drawn somewhere invented; a map placed
    # where the way in says it is not is that same case with a number on it.
    class DownALift:
        """Surface, then a descent into `under`, then the same again."""

        def __init__(self, under, agrees):
            tile = MapId(60, 38, 46, 0).pack()
            here = MapConfig(cfg).to_world(MapId(60, 38, 46, 0), 89.0, 70.0)
            # Chosen so the way in implies exactly the origin area 12 is
            # configured at: the same crossing, agreeing rather than not.
            ox, oz = MapConfig(cfg).area_origin[12]
            self.i = -1
            self.script = (
                [Reading(tile, 80.0 + i * 3.0, 248.9, 70.0) for i in range(4)]
                + [Reading(under, 401.3 + i * 3.0, -34.7, -122.7)
                   for i in range(4)]
                + [Reading(tile, 80.0 + i * 3.0, 248.9, 70.0) for i in range(4)]
                + [Reading(agrees, here[0] - ox + i * 3.0, -34.7, here[1] - oz)
                   for i in range(4)]
            )

        def read(self):
            self.i += 1
            return self.script[self.i] if self.i < len(self.script) else None

        def describe(self):
            return "wrong lift fixture"

        def close(self):
            pass

    wrong_db = ROOT / "data" / "selftest_wronglift.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(wrong_db) + suffix)
        if q.exists():
            q.unlink()
    wrong_store = Store(wrong_db)
    adrift_map = MapId(12, 5, 0, 0).pack()
    agrees_map = MapId(12, 6, 0, 0).pack()
    src_wrong = DownALift(adrift_map, agrees_map)
    clock_wrong = [1_700_000_000_000]
    wrong_s = Sampler(src_wrong, wrong_store, cfg, clock=lambda: clock_wrong[0])
    for _ in range(len(src_wrong.script)):
        clock_wrong[0] += 250
        wrong_s.step()
    wrong_store.commit()
    off_map = wrong_store.db.execute(
        "SELECT COUNT(*) c, COUNT(wx) w FROM samples WHERE map_id = ?",
        (adrift_map,),
    ).fetchone()
    check("a river the way in says is somewhere else is still recorded",
          off_map["c"] > 0, f"{off_map['c']} samples")
    check("and is kept off the map rather than drawn 4 km away",
          off_map["w"] == 0,
          f"{off_map['w']} of {off_map['c']} carry a world position")
    agreed = wrong_store.db.execute(
        "SELECT COUNT(*) c, COUNT(wx) w FROM samples WHERE map_id = ?",
        (agrees_map,),
    ).fetchone()
    check("while one the way in agrees with is drawn as before",
          agreed["c"] > 0 and agreed["w"] == agreed["c"],
          f"{agreed['w']} of {agreed['c']} placed")

    # And adding the origin has to reach the rows already written under the
    # wrong one. The pass only rewrote rows that had never been placed, so
    # correcting an origin reported "0 rows change" for a map drawn 4 km out.
    stale_db = ROOT / "data" / "selftest_stale.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(stale_db) + suffix)
        if q.exists():
            q.unlink()
    stale_store = Store(stale_db)
    ainsel = MapId(12, 1, 0, 0)
    right = maps.to_world(ainsel, 401.3, -122.7)
    wrong = (401.3 + maps.area_origin[12][0], -122.7 + maps.area_origin[12][1])
    sess = stale_store.start_session("recorded at the area origin")
    for i in range(4):
        stale_store.add_sample(
            session_id=sess, ts_ms=1_700_000_000_000 + i * 250,
            map_id=ainsel.pack(), layer="underground",
            x=401.3, y=-34.7, z=-122.7,
            wx=wrong[0], wz=wrong[1], break_before=0)
    stale_store.commit()
    argv = sys.argv
    sys.argv = ["repair_underground.py", "--db", str(stale_db),
                "--config", str(ROOT / "config" / "config.toml"), "--write"]
    try:
        repair_underground.main()
    finally:
        sys.argv = argv
    moved = stale_store.db.execute(
        "SELECT wx, wz FROM samples WHERE map_id = ?", (ainsel.pack(),)
    ).fetchall()
    # The pass copies before it writes, which is the point of it -- but a
    # copy per selftest run would pile up in data/ for ever.
    for old_copy in (ROOT / "data").glob("selftest_stale-before-*.db"):
        old_copy.unlink()
    check("correcting an origin moves the rows recorded under the old one",
          right is not None and all(
              abs(r["wx"] - right[0]) < 0.01 and abs(r["wz"] - right[1]) < 0.01
              for r in moved),
          f"{len(moved)} rows now at "
          f"({moved[0]['wx']:.0f}, {moved[0]['wz']:.0f}), "
          f"was ({wrong[0]:.0f}, {wrong[1]:.0f})")
    wrong_store.close()
    stale_store.close()

    print("\nrecording")
    db = ROOT / "data" / "selftest.db"
    store = build_db(cfg, db)
    b = store.bounds()
    check("samples stored", b and b["n"] > 1000, f"{b['n']} points")
    breaks = store.db.execute(
        "SELECT COUNT(*) c FROM samples WHERE break_before=1"
    ).fetchone()["c"]
    check("teleports broke the line", breaks > 0, f"{breaks} breaks")
    # Crossing a 256 m tile boundary changes the map ID without anything
    # happening. Breaking there puts a hole in the line at every seam, which
    # is invisible at a quarter-second poll and obvious in a route sampled
    # every five seconds.
    tile_breaks = store.db.execute(
        """SELECT COUNT(*) c FROM samples a JOIN samples b ON b.id = a.id - 1
           WHERE a.break_before = 1   -- 1 = the map changed; reloads are 3
             AND a.wx IS NOT NULL AND b.wx IS NOT NULL
             AND a.session_id = b.session_id
             AND a.map_id / 16777216 = b.map_id / 16777216
             AND ((a.wx - b.wx) * (a.wx - b.wx)
                  + (a.wz - b.wz) * (a.wz - b.wz)) < 40000"""
    ).fetchone()["c"]
    check("crossing a tile boundary is not a teleport", tile_breaks == 0,
          f"{tile_breaks} break(s) within 200 m of the last point")
    interiors_on_plane = store.db.execute(
        "SELECT COUNT(*) c FROM samples WHERE layer='interior' AND wx IS NOT NULL"
    ).fetchone()["c"]
    check("no interior coords on the world plane", interiors_on_plane == 0)
    interiors_kept = store.db.execute(
        "SELECT COUNT(*) c FROM samples WHERE layer='interior' AND wx IS NULL"
    ).fetchone()["c"]
    check("interior paths are kept, not discarded", interiors_kept > 0,
          f"{interiors_kept} samples")
    under = store.db.execute(
        "SELECT COUNT(*) c FROM samples WHERE layer='underground'"
    ).fetchone()["c"]
    check("underground layer separated", under > 0, f"{under} points")
    check("interior visits logged", len(store.interior_visits()) > 0,
          f"{len(store.interior_visits())} visits")
    sessions = store.sessions()
    check("one session, one file", len(sessions) == 1 and sessions[0]["samples"] > 0)
    # How long each session was, on the same capped clock stats() totals --
    # it has to be the same rule, or the rows in the panel would not add up to
    # the total above them. A row of points is a fact about the sampling rate
    # as much as about the session.
    check("and how long it ran, capped the way the total is",
          sessions[0]["active_ms"] > 0
          and sessions[0]["active_ms"] <= store.stats()["active_ms"],
          f"{sessions[0]['active_ms'] / 1000:.0f} s of "
          f"{store.stats()['active_ms'] / 1000:.0f} s")
    check("the session records what recorded it",
          "sim" in (sessions[0]["note"] or "").lower(), sessions[0]["note"] or "no note")

    print("\ndeaths")
    dead = store.deaths()
    check("deaths are recorded", len(dead) > 0, f"{len(dead)} deaths")
    check("a death knows where it happened",
          all(d["anchor_wx"] is not None for d in dead))
    # Dying puts you at a grace, which is somewhere you did not walk to. The
    # load screen in between is the only reliable sign of it, since the two
    # places can be close enough for the speed check to wave the line through.
    reload_breaks = store.db.execute(
        "SELECT COUNT(*) c FROM samples WHERE break_before = 3"
    ).fetchone()["c"]
    check("a load screen breaks the line", reload_breaks >= len(dead),
          f"{reload_breaks} breaks after a reload")
    joined = store.db.execute(
        """SELECT COUNT(*) c FROM samples a
           JOIN map_events e ON e.kind = 'death' AND e.ts_ms < a.ts_ms
           JOIN samples b ON b.id = a.id - 1 AND b.ts_ms <= e.ts_ms
           WHERE a.break_before = 0"""
    ).fetchone()["c"]
    check("no line is drawn from where you died to where you got up",
          joined == 0, f"{joined} joined pair(s)")

    # Warping straight into a dungeon: the last surface position is where you
    # stood before the warp, which is not the entrance and must not be used as
    # one. The load screen in between is what gives it away.
    surface = (60 << 24) | (42 << 16) | (36 << 8)
    cave = (31 << 24) | (5 << 16)

    class WarpIntoCave:
        def __init__(self):
            self.i = -1
            self.script = (
                [Reading(surface, 5.0 + i * 4, 80.0, 5.0) for i in range(4)]
                + [Reading(0xFFFFFFFF, 0.0, 0.0, 0.0, 0)]
                + [Reading(cave, 10.0 + i * 4, 0.0, 10.0) for i in range(4)]
            )

        def read(self):
            self.i += 1
            return self.script[self.i] if self.i < len(self.script) else None

        def describe(self):
            return "warp fixture"

        def close(self):
            pass

    warp_db = ROOT / "data" / "selftest_warp.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(warp_db) + suffix)
        if q.exists():
            q.unlink()
    warp_store = Store(warp_db)
    src3 = WarpIntoCave()
    clock3 = [1_700_000_000_000]
    ws = Sampler(src3, warp_store, cfg, clock=lambda: clock3[0])
    for _ in range(len(src3.script)):
        clock3[0] += 250
        ws.step()
    warp_store.commit()
    entered = warp_store.db.execute(
        "SELECT anchor_wx FROM map_events WHERE kind = 'enter' AND map_id = ?",
        (cave,),
    ).fetchone()
    check("a dungeon warped into is not anchored where you warped from",
          entered is not None and entered["anchor_wx"] is None)
    walked = warp_store.db.execute(
        "SELECT anchor_wx FROM map_events WHERE kind = 'enter' AND map_id = ?",
        (surface,),
    ).fetchone()
    check("the first map of a session is still recorded", walked is not None)

    # A Divine Tower reached from inside Stormveil never touches the surface,
    # so it has no entrance of its own -- but the castle it opens off does.
    tower = (34 << 24) | (10 << 16)

    class ThroughOneIntoAnother:
        def __init__(self):
            self.i = -1
            self.script = (
                [Reading(surface, 5.0 + i * 4, 80.0, 5.0) for i in range(4)]
                + [Reading(cave, 10.0 + i * 4, 0.0, 10.0) for i in range(4)]
                + [Reading(0xFFFFFFFF, 0.0, 0.0, 0.0, 0)]
                + [Reading(tower, 3.0 + i * 4, 20.0, 3.0) for i in range(4)]
            )

        def read(self):
            self.i += 1
            return self.script[self.i] if self.i < len(self.script) else None

        def describe(self):
            return "tower fixture"

        def close(self):
            pass

    src5 = ThroughOneIntoAnother()
    clock5 = [1_700_000_500_000]
    ts = Sampler(src5, warp_store, cfg, clock=lambda: clock5[0])
    for _ in range(len(src5.script)):
        clock5[0] += 250
        ts.step()
    ts.finish()
    warp_store.commit()
    towers = [v for v in warp_store.interior_visits()
              if v["map_id"] == tower]
    # Walking from one dungeon into another is still walking: the step before
    # the map changed was a real position in a map whose own position is
    # known, so the door between them can be worked out rather than guessed.
    # The fixture walks 12 m into the cave before going through, so the tower
    # lands 12 m from the cave's own anchor rather than on top of it.
    cave_anchor = 42 * 256 + 17
    check("a dungeon opening off another is placed at the door you used",
          len(towers) == 1 and towers[0]["placed"] == "the doorway"
          and abs(towers[0]["wx"] - (cave_anchor + 12)) < 1e-6,
          f'{towers[0]["placed"]} at {towers[0]["wx"]:.0f}' if towers else "none")

    # Go back into it from somewhere with no position of its own: the tower is
    # now a known place itself, so this visit can use it. Borrowing from a
    # guess stays a guess, though, and has to keep saying so.
    src6 = ThroughOneIntoAnother()
    src6.script = ([Reading(0xFFFFFFFF, 0.0, 0.0, 0.0, 0)]
                   + [Reading(tower, 9.0 + i, 20.0, 9.0) for i in range(4)])
    src6.i = -1
    clock6 = [1_700_001_000_000]
    ts2 = Sampler(src6, warp_store, cfg, clock=lambda: clock6[0])
    for _ in range(len(src6.script)):
        clock6[0] += 250
        ts2.step()
    ts2.finish()
    warp_store.commit()
    towers2 = [v for v in warp_store.interior_visits() if v["map_id"] == tower]
    check("a second visit to it is placed too",
          len(towers2) == 2 and all(v["wx"] is not None for v in towers2),
          ", ".join(v["placed"] for v in towers2))

    warp_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(warp_db) + suffix)
        if q.exists():
            q.unlink()

    # A teleporter inside the dungeon you came from looks exactly like a door:
    # same map change, same load screen. It gives a doorway nowhere near the
    # real one, and drew that visit off on its own -- so the ways in have to
    # agree with each other, and the odd one out loses.
    tele_db = ROOT / "data" / "selftest_teleporter.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(tele_db) + suffix)
        if q.exists():
            q.unlink()

    def walk(map_id, xs, y=0.0, z=10.0):
        return [Reading(map_id, x, y, z) for x in xs]

    blank_read = Reading(0xFFFFFFFF, 0.0, 0.0, 0.0, 0)
    script = (
        walk(surface, [5.0, 9.0, 13.0, 17.0], y=80.0, z=5.0)
        + walk(cave, [10.0, 14.0, 18.0, 22.0])          # in on foot
        + [blank_read] + walk(tower, [3.0, 6.0, 9.0])   # through the door
        + [blank_read] + walk(cave, [22.0, 24.0])
        + [blank_read] + walk(tower, [3.0, 6.0, 9.0])   # and again
        + [blank_read] + walk(cave, [200.0, 204.0])     # the teleporter
        + [blank_read] + walk(tower, [3.0, 6.0, 9.0])   # in from over there
        + walk(tower, [400.0, 403.0])                   # a lift inside it
    )

    class Scripted:
        def __init__(self, script):
            self.script, self.i = script, -1

        def read(self):
            self.i += 1
            return self.script[self.i] if self.i < len(self.script) else None

        def describe(self):
            return "teleporter fixture"

        def close(self):
            pass

    tele_store = Store(tele_db)
    src7 = Scripted(script)
    clock7 = [1_700_000_000_000]
    tele = Sampler(src7, tele_store, cfg, clock=lambda: clock7[0])
    for _ in range(len(script)):
        clock7[0] += 250
        tele.step()
    tele.finish()
    tele_store.commit()

    # A teleporter inside one dungeon, with no load screen and no map change
    # to give it away: the only thing that says it happened is the speed, and
    # the recorder was measuring it against the surface's ceiling. Reported
    # from the field on 2026-09-09 as "I left a cave through a teleporter and
    # it just drew a path between them" -- 175.3 m in 6.5 s, 26.9 m/s, the
    # fastest unbroken interior step in routes.db by a factor of 2.4, and
    # 40 m/s let it through.
    warp_db = ROOT / "data" / "selftest_inside_warp.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(warp_db) + suffix)
        if q.exists():
            q.unlink()
    tele2 = Store(warp_db)
    wid = tele2.start_session("a teleporter inside a cave")
    # A metre a quarter-second is four metres a second, which is walking.
    # The other fixtures step four metres at a time because the only ceiling
    # that ever applied to them was the surface's 40 m/s -- inside a dungeon
    # that is 16 m/s, faster than anything in routes.db, and it would now be
    # a teleport rather than a walk.
    src_warp = Scripted(
        walk(surface, [5.0, 9.0], y=80.0, z=5.0)
        + walk(cave, [10.0, 12.0, 14.0])          # walked in, 8 m/s
        + walk(cave, [189.0, 191.0])              # and then 175 m in one step
    )
    clock_warp = [1_700_000_000_000]
    # Six and a half seconds between the last step and the arrival, which is
    # the animation: fast enough to be impossible, slow enough that 40 m/s
    # never noticed.
    gaps = [250, 250, 250, 250, 250, 6500, 250]
    warp_s = Sampler(src_warp, tele2, cfg, clock=lambda: clock_warp[0])
    for g in gaps:
        clock_warp[0] += g
        warp_s.step()
    warp_s.finish()
    tele2.commit()
    inside_rows = [r for r in tele2.db.execute(
        "SELECT break_before, x FROM samples WHERE wx IS NULL ORDER BY id")]
    jumped = [r for r in inside_rows if r["x"] > 180]
    check("a teleporter inside a dungeon breaks the line",
          jumped and jumped[0]["break_before"] == BREAK_SPEED,
          f"break {jumped[0]['break_before'] if jumped else 'no row'} on the arrival")
    # And walking in there is still walking: nothing else broke.
    check("and walking around in one still does not",
          sum(1 for r in inside_rows if r["break_before"] == BREAK_SPEED) == 1,
          f"{sum(1 for r in inside_rows if r['break_before'] == BREAK_SPEED)} speed breaks")
    tele2.close()

    # And the same teleporter taken from a standstill, which is how a
    # transporter chest is taken: you stop, you open it, and the animation
    # holds you still for several seconds. Reported from the field on
    # 2026-09-15 -- "I just took a transporter chest in a cave, and it drew a
    # path instead of making it a teleport" -- and the recording says why:
    # 45.2 m in 7.01 s inside m30_13, which is 6.4 m/s, walking pace, under
    # every ceiling there is.
    #
    # The speed was measured against the last sample the movement gate had
    # let through, and the gate lets nothing through while you stand still.
    # So the denominator was however long you had been standing, and the
    # longer you waited the more walkable the jump looked. Against the
    # reading half a second before it, it is 90 m/s.
    chest_db = ROOT / "data" / "selftest_chest.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(chest_db) + suffix)
        if q.exists():
            q.unlink()
    chest = Store(chest_db)
    chest.start_session("a transporter chest inside a cave")
    src_chest = Scripted(
        walk(surface, [5.0, 9.0], y=80.0, z=5.0)
        + walk(cave, [10.0, 12.0, 14.0])       # walked in
        # Standing at the chest: the readings keep coming and the gate keeps
        # dropping them, because nothing has moved 1.5 m.
        + walk(cave, [14.2, 14.1, 14.2, 14.1, 14.2, 14.1, 14.2, 14.1])
        + walk(cave, [59.0, 60.0])             # and then 45 m in one reading
    )
    clock_chest = [1_700_000_000_000]
    chest_s = Sampler(src_chest, chest, cfg, clock=lambda: clock_chest[0])
    for _ in range(15):
        clock_chest[0] += 500              # the recorder's own half-second
        chest_s.step()
    chest_s.finish()
    chest.commit()
    chest_rows = [r for r in chest.db.execute(
        "SELECT break_before, x, ts_ms FROM samples WHERE wx IS NULL ORDER BY id")]
    landed = [r for r in chest_rows if r["x"] > 50]
    check("a teleport taken from a standstill is still a teleport",
          landed and landed[0]["break_before"] == BREAK_SPEED,
          f"break {landed[0]['break_before'] if landed else 'nothing stored'} "
          f"on the arrival")
    # The gate is what hid it: seven seconds of stored nothing before the
    # jump, so measuring from the last stored sample gave walking pace.
    if landed:
        before = [r for r in chest_rows if r["ts_ms"] < landed[0]["ts_ms"]]
        held = (landed[0]["ts_ms"] - before[-1]["ts_ms"]) / 1000 if before else 0
        check("and the standstill it was taken from is what hid it",
              held >= 4.0,
              f"{held:.1f} s between the last stored sample and the arrival")
    # A mark for it, too: 45 m is under the 50 m floor that tells a tile
    # crossing from a warp, and that floor has nothing to say about a speed
    # break -- the recorder has already measured something unwalkable.
    chest_warps = [w for w in chest.warps() if w["break_before"] == BREAK_SPEED]
    check("and it is drawn as one rather than dropped for being short",
          len(chest_warps) == 1 and 40 < chest_warps[0]["distance_m"] < 50,
          f"{len(chest_warps)} speed jump(s)"
          + (f" of {chest_warps[0]['distance_m']:.1f} m" if chest_warps else ""))
    chest.close()

    tele_visits = [v for v in tele_store.interior_visits()
                   if v["map_id"] == tower]
    anchors = {(round(v["wx"], 3), round(v["wz"], 3)) for v in tele_visits}
    check("every way into a dungeon agrees on one door",
          len(tele_visits) == 3 and len(anchors) == 1,
          f"{len(tele_visits)} visits, {len(anchors)} anchor(s)")
    # The two walked-in visits leave from a couple of metres apart, so either
    # is the door; what matters is that the one 190 m away loses.
    door = 42 * 256 + 17 + 12          # walked in: last step 12 m inside
    teleported = 42 * 256 + 17 + 194   # arrived somewhere else entirely
    got = next(iter(anchors))[0] if anchors else None
    check("the door the teleporter dropped you at is the one rejected",
          got is not None and abs(got - door) < 5 and abs(got - teleported) > 100,
          f"{got:.0f} against a real door at {door} and a teleport at {teleported}"
          if got else "")
    # A lift or a teleporter inside a dungeon is a jump with no world position
    # at either end. Left out of the warp list, it is invisible: there is
    # nowhere else it could ever be drawn.
    jumps = [w for w in tele_store.warps() if w["inside"]]
    check("a teleport inside a dungeon is a teleport",
          len(jumps) == 1 and jumps[0]["map_id"] == tower
          and abs(jumps[0]["distance_m"] - 391.0) < 1.0,
          f"{len(jumps)} inside, "
          f"{jumps[0]['distance_m']:.0f} m" if jumps else "none")
    check("and it is reported in the dungeon's own metres",
          jumps and jumps[0]["wx"] is None and jumps[0]["x"] is not None)

    tele_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(tele_db) + suffix)
        if q.exists():
            q.unlink()

    # A transporter trap: a chest on the surface that takes you somewhere
    # else entirely. It is a map change with a load screen, like walking into
    # a cave, except that the place you end up has nothing to do with where
    # you were standing -- so the chest must not become the tunnel's door.
    print("\ntransporter traps")
    trap_db = ROOT / "data" / "selftest_trap.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(trap_db) + suffix)
        if q.exists():
            q.unlink()

    tunnel = (32 << 24) | (8 << 16)
    far = (60 << 24) | (49 << 16) | (39 << 8)     # a tile a long way east
    chest_x = 42 * 256 + 30
    trap_script = (
        walk(surface, [10.0, 14.0, 18.0, 22.0, 26.0, 30.0], y=80.0, z=5.0)
        + [blank_read] * 2                        # the chest, and the load
        + walk(tunnel, [38.0, 40.0, 42.0])        # dropped in, far away
        + walk(far, [100.0, 104.0, 108.0], y=80.0, z=5.0)   # and walk out
    )
    trap_store = Store(trap_db)
    src8 = Scripted(trap_script)
    clock8 = [1_700_100_000_000]
    trap = Sampler(src8, trap_store, cfg, clock=lambda: clock8[0])
    for _ in range(len(trap_script)):
        clock8[0] += 250
        trap.step()
    trap.finish()
    trap_store.commit()

    trapped = [v for v in trap_store.interior_visits() if v["map_id"] == tunnel]
    got = trapped[0]["wx"] if trapped and trapped[0]["wx"] is not None else None
    check("a trap does not put the dungeon at the chest",
          len(trapped) == 1 and (got is None or abs(got - chest_x) > 100),
          f"{got:.0f} against a chest at {chest_x}" if got else "not placed")
    check("it is placed where you walked out instead",
          len(trapped) == 1 and trapped[0]["placed"] == "exit",
          trapped[0]["placed"] if trapped else "no visit")

    # And the same trap taken before this tool could see load screens. An
    # imported route records the chest as an ordinary entrance; one live
    # observation of the same move is enough to know better, and it has to
    # reach back and correct the old visit as well as the new one.
    old_visit = (
        walk(surface, [26.0, 30.0], y=80.0, z=5.0)
        + walk(tunnel, [38.0, 40.0])              # no load screen recorded
        + walk(far, [100.0, 104.0], y=80.0, z=5.0)
    )
    src9 = Scripted(old_visit)
    clock9 = [1_700_090_000_000]
    old_run = Sampler(src9, trap_store, cfg, clock=lambda: clock9[0])
    for _ in range(len(old_visit)):
        clock9[0] += 250
        old_run.step()
    old_run.finish()
    trap_store.commit()

    both = [v for v in trap_store.interior_visits() if v["map_id"] == tunnel]
    at_chest = [v for v in both
                if v["wx"] is not None and abs(v["wx"] - chest_x) < 15]
    check("a trap taken before load screens were recorded is corrected too",
          len(both) == 2 and not at_chest,
          f"{len(at_chest)} of {len(both)} visits still at the chest")

    trap_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(trap_db) + suffix)
        if q.exists():
            q.unlink()

    # Walking out of a dungeon puts you at its door. Warping out puts you
    # wherever you chose next, and reading that as the door is what drew the
    # Roundtable Hold -- a place with no way in on foot at all -- as a path in
    # the middle of nowhere that moved every time you left it differently.
    # A lift inside a dungeon covers a hundred metres in five seconds and
    # rises forty. Recorded before the speed check could see inside one, it
    # has no break at all and is drawn as a straight walked line through the
    # rock, with no teleport mark. The repair pass has to find that and leave
    # everything a player can actually do alone.
    print("\nlifts recorded as walks")
    lift_db = ROOT / "data" / "selftest_lift.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(lift_db) + suffix)
        if q.exists():
            q.unlink()
    lift_store = Store(lift_db)
    sid = lift_store.start_session("lift fixture")
    # Five seconds apart, the way the old tool sampled. A run across the room,
    # then the lift, then a run at the far end.
    steps = [(0.0, 0.0), (20.0, 0.0), (40.0, 0.0),        # 4 m/s, walked
             (100.0, 0.0),                                 # 12 m/s, a gallop
             (245.0, 60.0),                                # the lift: 31 m/s
             (260.0, 60.0), (275.0, 60.0)]                 # 3 m/s, walked
    for i, (x, z) in enumerate(steps):
        lift_store.add_sample(session_id=sid, ts_ms=1_700_000_000_000 + i * 5000,
                              map_id=(30 << 24) | (2 << 16), layer="interior",
                              x=x, y=0.0, z=z, wx=None, wz=None, break_before=0)
    lift_store.commit()

    found, spared = repair_jumps.find_jumps(lift_store, 15.0)
    walked = spared["speed"]
    check("a lift inside a dungeon is found",
          len(found) == 1 and found[0][3] > 25.0,
          f"{len(found)} found at {found[0][3]:.1f} m/s" if found else "none")
    # The fixture gallops at 12 m/s, which is Torrent flat out and the fastest
    # thing in 39,484 real steps on the world plane. It must survive.
    check("a gallop is not mistaken for one",
          len(walked) == 5 and 11.0 < max(walked) < 13.0,
          f"{len(walked)} spared, fastest {max(walked):.1f} m/s"
          if walked else "")
    # The margin is the whole safety argument, so assert there is one rather
    # than trusting that the constant was chosen well.
    check("with a wide margin either side of the line",
          found and walked and found[0][3] / max(walked) > 2,
          f"{max(walked):.1f} m/s ridden against {found[0][3]:.1f} m/s "
          f"jumped" if found and walked else "")
    lift_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(lift_db) + suffix)
        if q.exists():
            q.unlink()

    # Dying is slow. The animation and the load screen take ten seconds and
    # the grace is close, so the implied speed is walking pace and the speed
    # rule never sees it. What gives it away is the hole: a session sampling
    # twice a second that reports nothing for twelve, then comes back ninety
    # metres away, did not walk there. Standing still leaves a hole too, so
    # the distance is what separates them.
    print("\ndeaths recorded as walks")
    dead_db = ROOT / "data" / "selftest_deadwalk.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(dead_db) + suffix)
        if q.exists():
            q.unlink()
    dead_store = Store(dead_db)
    sid = dead_store.start_session("respawn fixture")
    cave = (30 << 24) | (1 << 16)
    t = 1_700_000_000_000

    def step(x, z, after_ms):
        nonlocal t
        t += after_ms
        dead_store.add_sample(session_id=sid, ts_ms=t, map_id=cave,
                              layer="interior", x=x, y=0.0, z=z,
                              wx=None, wz=None, break_before=0)

    for i in range(12):                       # walking, twice a second
        step(i * 2.0, 0.0, 500)
    step(90.0, 20.0, 12_000)                  # died, got up at the grace
    for i in range(12):                       # walking again
        step(90.0 + i * 2.0, 20.0, 500)
    # Half a minute at a grace: the same hole in the recording, and the only
    # thing that says it was not a death is that you are still standing where
    # you were. The movement gate is why it is 1.6 m and not 0.
    step(113.6, 20.4, 30_000)
    for i in range(6):
        step(115.0 + i * 2.0, 20.4, 500)
    dead_store.commit()

    found, spared = repair_jumps.find_jumps(dead_store)
    holes = [h for h in found if h[4] == "a hole in the recording"]
    check("a death and respawn inside a dungeon is found",
          len(holes) == 1 and holes[0][1] > 60,
          f"{len(found)} found, {len(holes)} of them holes")
    check("and it was too slow for the speed rule to have caught it",
          holes and holes[0][3] < 15.0,
          f"{holes[0][3]:.1f} m/s" if holes else "")
    # The half-minute of standing still is a hole of exactly the same shape.
    # Only the distance tells them apart, so that is the check that matters.
    check("standing still is not mistaken for one",
          spared["gap"] and max(spared["gap"]) < 10,
          f"{len(spared['gap'])} quiet stretches left alone, furthest "
          f"{max(spared['gap']):.1f} m" if spared["gap"] else "none")

    # The repair pass cannot tell a death from a teleporter -- both are a
    # hole with a position either side, and the HP reading that would settle
    # it is not in the old data. So it draws a teleport and this is how you
    # correct it, one jump at a time.
    # find_jumps only reports; the tool writes. Do what --write does, so the
    # rest of this reads the database the user would be looking at.
    for h in holes:
        dead_store.db.execute("UPDATE samples SET break_before = 3 WHERE id = ?",
                              (h[0]["id"],))
    dead_store.commit()
    jump = [w for w in dead_store.warps() if w["inside"]]
    check("the hole is drawn as a teleport until you say otherwise",
          len(jump) == 1, f"{len(jump)} teleport(s) inside")
    marked = dead_store.mark_death(jump[0]["ts_ms"])
    check("a jump can be called a death", marked and not marked["already"])
    mine = [d for d in dead_store.deaths() if d["by_hand"]]
    check("the death goes where you went down, not where you got up",
          len(mine) == 1 and abs(mine[0]["local_x"] - 22.0) < 0.1,
          f"local x {mine[0]['local_x']:.1f}" if mine else "none")
    check("and the respawn falls out of it",
          mine and mine[0]["respawn"] is not None
          and abs(mine[0]["respawn"]["local_x"] - 90.0) < 0.1,
          f"at {mine[0]['respawn']['local_x']:.1f}"
          if mine and mine[0]["respawn"] else "none")
    check("the teleport stops being one",
          not [w for w in dead_store.warps() if w["inside"]])
    check("saying it twice changes nothing",
          (dead_store.mark_death(jump[0]["ts_ms"]) or {}).get("already"))
    check("and it can be taken back",
          dead_store.clear_death(mine[0]["ts_ms"])
          and not [d for d in dead_store.deaths() if d["by_hand"]]
          and len([w for w in dead_store.warps() if w["inside"]]) == 1)
    check("a death the HP reading found cannot be taken back that way",
          not dead_store.clear_death(1))

    # A transit is a jump reported across a whole dungeon stay, for the case
    # where no single pair of samples shows one -- so the row before its
    # arrival is the last step *inside* the dungeon, minutes after you went
    # down. Marking one as a death therefore put the death and the grace both
    # inside a tunnel the player had been dead for the whole of: reported from
    # the field on 5 August, where the death belonged at 03:17:17 with the
    # grace at 03:17:32 and landed at 03:17:59 and 03:18:02.
    tr_db = ROOT / "data" / "selftest_transit.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(tr_db) + suffix)
        if q.exists():
            q.unlink()
    tr = Store(tr_db)
    tsid = tr.start_session("transit fixture")
    t0 = 1_700_000_000_000
    surf = MapId(60, 40, 40, 0).pack()
    tunnel = MapId(32, 8, 0, 0).pack()
    for i in range(8):
        tr.add_sample(session_id=tsid, ts_ms=t0 + i * 500, map_id=surf,
                      layer="surface", x=0.0, y=60.0, z=0.0,
                      wx=1000.0 + i * 2.0, wz=1000.0, break_before=0)
    died_ms = t0 + 7 * 500
    # Inside, and barely moving: two metres of walking cannot account for the
    # ninety the surface positions are apart, so the stay is a jump and not a
    # walk through a cave with two mouths.
    inside_at = [(15_000, 1), (40_000, 0), (42_000, 0)]
    for k, (off, brk) in enumerate(inside_at):
        tr.add_sample(session_id=tsid, ts_ms=died_ms + off, map_id=tunnel,
                      layer="interior", x=float(k), y=0.0, z=0.0,
                      wx=None, wz=None, break_before=brk)
    tr.add_sample(session_id=tsid, ts_ms=died_ms + 45_000, map_id=surf,
                  layer="surface", x=0.0, y=60.0, z=0.0,
                  wx=1104.0, wz=1000.0, break_before=1)
    tr.commit()

    across = [w for w in tr.warps() if w["break_before"] == 4]
    check("a stay you could not have walked is reported as one jump",
          len(across) == 1 and across[0]["from_ts"] == died_ms,
          f"{len(across)} transit(s)")
    if across:
        got = tr.mark_death(across[0]["ts_ms"], across[0]["from_ts"])
        check("a transit called a death puts it where the jump set off",
              got and got["ts_ms"] == died_ms,
              f"{(got or {}).get('ts_ms', 0) - died_ms} ms out of place")
        # The grace is whatever came next, which inside a tunnel is the
        # tunnel: the game put you there, and the arrival on the far side
        # is where you walked out three quarters of a minute later.
        check("and the grace on the first thing recorded after it",
              got and got["respawn_ts"] == died_ms + 15_000,
              f"{((got or {}).get('respawn_ts') or 0) - died_ms} ms in")
        back = [d for d in tr.deaths() if d["by_hand"]]
        check("the mark reads back at both ends",
              len(back) == 1 and back[0]["respawn"]
              and back[0]["respawn"]["ts_ms"] == died_ms + 15_000)
        check("and taking it back leaves the transit where it was",
              tr.clear_death(died_ms)
              and len([w for w in tr.warps() if w["break_before"] == 4]) == 1)
    tr.close()

    # Three layers, and the middle one is written by hand: the store can carry
    # the departure and the viewer can ask for it while the server drops the
    # field between them, which is the exact shape of the plane bug.
    srv = (ROOT / "tracker" / "server.py").read_text(encoding="utf-8")
    check("the server hands the jump's departure to the viewer",
          '"from_ts": w["from_ts"],' in srv)
    check("and passes it back to the store when a jump is called a death",
          "mark_death(ts, came_from)" in srv)
    # Same three layers, same hand-written middle: the store carries the
    # interior end's own metres and the viewer draws them, so the field
    # between has to exist. It is sent for one end of a gate now, not only
    # for both ends of a lift inside one dungeon.
    check("and where the route put the doorway reaches the viewer too",
          '"door_xy": [round(c, 2)' in srv
          and '"door_placed": v["door_placed"]' in srv)
    check("the local position of an end inside a dungeon reaches the viewer",
          'if w.get("to_inside") and w.get("x") is not None:' in srv
          and 'if w.get("from_inside") and w.get("from_x") is not None:' in srv
          and '"from_map_id"' in srv)
    page = (ROOT / "viewer" / "app.js").read_text(encoding="utf-8")
    # Every call that marks a jump as a death has to carry the departure --
    # a transit's is not the row before its arrival. Counted by looking at
    # each call site rather than by counting the substring, which went red
    # the day one of them stopped being written out twice.
    marking = []
    for name in ("callDeath({ ts:", "playReclassify({ ts:"):
        at = page.find(name)
        while at != -1:
            marking.append(page[at:at + 90])
            at = page.find(name, at + 1)
    jumps = [c for c in marking
             if "clear: true" not in c and "at: true" not in c]
    check("and every 'This was a death' sends the jump, not just its end",
          len(jumps) >= 3 and all("from_ts:" in c for c in jumps),
          f"{len(jumps)} of {len(marking)} call sites mark a jump")

    dead_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(dead_db) + suffix)
        if q.exists():
            q.unlink()

    # The warp that does not announce itself. A load screen normally shows up
    # as the no-map sentinel, but on 2026-09-06 the recorder simply could not
    # read the game for 73 seconds -- the map menu, then the warp -- and came
    # back inside a cave believing it had walked there. The anchor written was
    # the grace 2,900 m away, and the cave's whole path was drawn at it.
    # Dying in a cave, with the load screen never announcing itself. The body
    # settles first -- a step or two of a few metres -- and only then are you
    # at the grace. The break belongs to the grace, and HP coming back is the
    # only thing that says exactly where that is.
    # Every cave is called "Cave" until you say otherwise, and the map ID is
    # the only thing telling two of them apart.
    # A reading taken during a load screen that names the map you were in
    # ten minutes ago. It looks like an ordinary dungeon reading, so it
    # became a visit, a marker, and a place to file a death you were nowhere
    # near.
    print("\ndungeons that never happened")
    gh_db = ROOT / "data" / "selftest_ghost.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(gh_db) + suffix)
        if q.exists():
            q.unlink()
    gh = Store(gh_db)
    gid = gh.start_session("a load screen")
    t = 1_701_200_000_000
    surf = (60 << 24) | (43 << 16) | (37 << 8)
    for i in range(6):
        gh.add_sample(session_id=gid, ts_ms=t + i * 2500, map_id=surf,
                      layer="surface", x=0.0, y=60.0, z=0.0,
                      wx=100.0 + i, wz=200.0, break_before=0)
    # The load screen, still naming the tunnel from earlier.
    gh.add_sample(session_id=gid, ts_ms=t + 20_000, map_id=cave,
                  layer="interior", x=111.0, y=33.0, z=14.0,
                  wx=None, wz=None, break_before=BREAK_RELOAD)
    gh.add_map_event(session_id=gid, ts_ms=t + 20_000, map_id=cave,
                     layer="interior", kind="enter",
                     anchor_wx=105.0, anchor_wz=200.0)
    gh.add_map_event(session_id=gid, ts_ms=t + 40_000, map_id=cave,
                     layer="interior", kind="leave",
                     anchor_wx=None, anchor_wz=None)
    # A death filed on it, which is the thing that has to move.
    gh.add_map_event(session_id=gid, ts_ms=t + 20_000, map_id=cave,
                     layer="interior", kind="death_by_hand",
                     anchor_wx=None, anchor_wz=None,
                     local_x=111.0, local_z=14.0)
    for i in range(6):
        gh.add_sample(session_id=gid, ts_ms=t + 40_000 + i * 2500, map_id=surf,
                      layer="surface", x=0.0, y=88.0, z=0.0,
                      wx=-90.0 + i, wz=150.0, break_before=(3 if not i else 0))
    gh.commit()
    ghosts = gh.ghost_stays()
    check("a one-reading dungeon with solid ground either side is not a visit",
          len(ghosts) == 1 and ghosts[0]["samples"] == 1,
          f"{len(ghosts)} found")
    check("and it says where you actually were",
          ghosts and round(ghosts[0]["stood_at"][0]) == 105)
    gh.drop_ghost_stay(ghosts[0])
    check("putting it back leaves no visit behind",
          not any(v["map_id"] == cave for v in gh.interior_visits()))
    dead = gh.deaths()[0]
    check("and the death moves to where you were standing",
          dead["layer"] == "surface" and round(dead["anchor_wx"]) == 105,
          f'{dead["layer"]} at {dead["anchor_wx"]}')
    check("with the grace still at the other end",
          dead["respawn"] and round(dead["respawn"]["wx"]) == -90)
    check("and nothing left to put back",
          not gh.ghost_stays())

    # The expensive answers are kept until something writes. Read far more
    # often than the database changes -- one boot of the map wants the visits
    # four times over -- and each is a walk over the whole history: measured
    # on a database ten times the size of routes.db, interior_visits 234 ms,
    # warps 1.85 s, stats 3.7 s, time_quantiles 490 ms.
    # Counted rather than timed: a stopwatch check would pass on a fast
    # machine whatever the cache did.
    builds = []
    real_stats = gh._stats
    gh._stats = lambda *a, **k: (builds.append(1), real_stats(*a, **k))[1]
    first = gh.stats()
    again = gh.stats()
    gh._stats = real_stats
    check("an expensive answer is worked out once, not once per ask",
          len(builds) == 1, f"{len(builds)} walk(s) over the history")
    check("and the second ask gets the same answer", first == again)
    cached_visits = gh.interior_visits()
    check("and so do the visits", cached_visits == gh.interior_visits())
    # Invalidation is not a flag anybody has to remember to set: the key is
    # asked for, not stored, so a write nobody told the cache about still
    # invalidates it.
    before_n = first["deaths"]
    gh.add_map_event(session_id=gid, ts_ms=t + 500_000, map_id=surf,
                     layer="surface", kind="death", anchor_wx=1.0,
                     anchor_wz=2.0)
    gh.commit()
    check("and a write nobody told the cache about still invalidates it",
          gh.stats()["deaths"] == before_n + 1,
          f'{gh.stats()["deaths"]} against {before_n}')

    # Walking through a door is not a teleport, and the only place it could
    # be mistaken for one is where a distance is measured against the marker
    # a dungeon is *drawn* at rather than a position anybody recorded.
    # Reported from the field: walk in one mouth of a cave and out of the
    # other, 94 m apart, and a teleport mark was drawn between them.
    door = gh.start_session("a cave with two mouths")
    cave2 = (31 << 24) | (5 << 16)
    base = t + 2_000_000
    # In at one mouth, a walk inside, out at the other 90 m away.
    gh.add_sample(session_id=door, ts_ms=base, map_id=surf, layer="surface",
                  x=0.0, y=88.0, z=0.0, wx=0.0, wz=0.0, break_before=0)
    gh.add_map_event(session_id=door, ts_ms=base + 500, map_id=cave2,
                     layer="interior", kind="enter", anchor_wx=0.0, anchor_wz=0.0)
    for i in range(20):
        gh.add_sample(session_id=door, ts_ms=base + 500 + i * 500, map_id=cave2,
                      layer="interior", x=i * 10.0, y=0.0, z=0.0, wx=None, wz=None,
                      break_before=(1 if i == 0 else 0))
    gh.add_map_event(session_id=door, ts_ms=base + 11_000, map_id=cave2,
                     layer="interior", kind="leave", anchor_wx=0.0, anchor_wz=0.0)
    gh.add_sample(session_id=door, ts_ms=base + 11_500, map_id=surf, layer="surface",
                  x=0.0, y=88.0, z=0.0, wx=90.0, wz=0.0, break_before=1)
    gh.commit()
    marks = [w for w in gh.warps()
             if base <= w["ts_ms"] <= base + 20_000]
    check("walking through a cave with two mouths is not a teleport",
          not marks, f"{len(marks)} mark(s): " +
          ", ".join(f"{round(m['distance_m'])} m code {m['break_before']}" for m in marks))
    # And a load screen still says you were put there. Four samples inside,
    # not one: a single interior reading between two world positions is a
    # ghost stay, which is a different fixture two blocks up.
    for i in range(4):
        gh.add_sample(session_id=door, ts_ms=base + 30_000 + i * 500,
                      map_id=cave2, layer="interior", x=float(i), y=0.0, z=0.0,
                      wx=None, wz=None,
                      break_before=(BREAK_RELOAD if not i else 0))
    gh.add_sample(session_id=door, ts_ms=base + 40_000, map_id=surf, layer="surface",
                  x=0.0, y=88.0, z=0.0, wx=900.0, wz=0.0, break_before=BREAK_RELOAD)
    gh.commit()
    check("but a load screen out of one still is",
          any(w["break_before"] == BREAK_RELOAD and w["ts_ms"] > base + 20_000
              for w in gh.warps()))

    # And the vote that settles disagreeing entrances must not eat a real
    # second mouth. Three anchors is enough to make it run, so a cave walked
    # through and then re-entered twice at the far mouth had the door it was
    # actually used through outvoted 2 to 1 -- the pin and the drawing moved
    # there after the fact. Two mouths of one dungeon cannot be further apart
    # than the dungeon is, and the recording says how big it is.
    # `last_surface` is carried forward unchanged from one interior into the
    # next, so a dungeon entered from inside a cave was anchored at the cave's
    # own mouth -- and called an `entrance`, the most trusted tier there is.
    # Reported from the field: it put m14_00's door 104 m from where it is,
    # and then gave frameTurn() a 104 m baseline between one real door and a
    # leftover, which turned the whole place 77 degrees.
    leftover = gh.start_session("one cave into another, on foot")
    outer = (31 << 24) | (20 << 16)
    innerm = (14 << 24) | (9 << 16)
    lb = t + 4_000_000
    gh.add_map_event(session_id=leftover, ts_ms=lb, map_id=surf,
                     layer="surface", kind="enter", anchor_wx=None, anchor_wz=None)
    gh.add_sample(session_id=leftover, ts_ms=lb, map_id=surf, layer="surface",
                  x=0.0, y=60.0, z=0.0, wx=500.0, wz=500.0, break_before=0)
    # Into the first cave, anchored where you were standing outside it.
    gh.add_map_event(session_id=leftover, ts_ms=lb + 1000, map_id=outer,
                     layer="interior", kind="enter",
                     anchor_wx=500.0, anchor_wz=500.0)
    for i in range(4):
        gh.add_sample(session_id=leftover, ts_ms=lb + 1000 + i * 500,
                      map_id=outer, layer="interior", x=float(i), y=0.0,
                      z=0.0, wx=None, wz=None, break_before=(1 if not i else 0))
    gh.add_map_event(session_id=leftover, ts_ms=lb + 4000, map_id=outer,
                     layer="interior", kind="leave",
                     anchor_wx=500.0, anchor_wz=500.0)
    # And straight on into a second one, on foot. The anchor the sampler has
    # to hand is still (500, 500) -- the first cave's mouth.
    gh.add_map_event(session_id=leftover, ts_ms=lb + 4000, map_id=innerm,
                     layer="interior", kind="enter",
                     anchor_wx=500.0, anchor_wz=500.0)
    for i in range(4):
        gh.add_sample(session_id=leftover, ts_ms=lb + 4000 + i * 500,
                      map_id=innerm, layer="interior", x=float(i), y=0.0,
                      z=0.0, wx=None, wz=None, break_before=(1 if not i else 0))
    gh.add_map_event(session_id=leftover, ts_ms=lb + 7000, map_id=innerm,
                     layer="interior", kind="leave",
                     anchor_wx=500.0, anchor_wz=500.0)
    gh.commit()
    deep = [v for v in gh.interior_visits() if v["map_id"] == innerm]
    check("an anchor carried in from another dungeon is not an entrance",
          deep and deep[0]["placed"] != "entrance",
          f'placed {deep[0]["placed"] if deep else "nothing"}')
    # The cave you came *from* keeps its own, because you did walk into that
    # one from outdoors.
    first = [v for v in gh.interior_visits() if v["map_id"] == outer]
    check("while the one you walked into from outdoors keeps its",
          first and first[0]["placed"] == "entrance",
          f'placed {first[0]["placed"] if first else "nothing"}')

    twin = gh.start_session("a cave with two mouths, entered at both")
    cave3 = (31 << 24) | (9 << 16)
    tb = t + 3_000_000
    for i in range(30):            # 200 m of cave, so it is 200 m across
        gh.add_sample(session_id=twin, ts_ms=tb + i * 500, map_id=cave3,
                      layer="interior", x=i * 7.0, y=0.0, z=0.0,
                      wx=None, wz=None, break_before=(1 if not i else 0))
    for k, at in enumerate([(0.0, 0.0), (90.0, 0.0), (90.0, 1.0)]):
        gh.add_map_event(session_id=twin, ts_ms=tb + k * 1000, map_id=cave3,
                         layer="interior", kind="enter",
                         anchor_wx=at[0], anchor_wz=at[1])
        gh.add_map_event(session_id=twin, ts_ms=tb + k * 1000 + 500, map_id=cave3,
                         layer="interior", kind="leave",
                         anchor_wx=at[0], anchor_wz=at[1])
    # And one anchor far outside it, which is what the vote is for.
    gh.add_map_event(session_id=twin, ts_ms=tb + 5000, map_id=cave3,
                     layer="interior", kind="enter", anchor_wx=3000.0, anchor_wz=0.0)
    gh.add_map_event(session_id=twin, ts_ms=tb + 5500, map_id=cave3,
                     layer="interior", kind="leave", anchor_wx=3000.0, anchor_wz=0.0)
    gh.commit()
    doors = sorted({round(v["wx"]) for v in gh.interior_visits()
                    if v["map_id"] == cave3 and v["wx"] is not None})
    check("a second mouth survives the vote that settles the entrances",
          0 in doors and 90 in doors, f"doors at {doors}")
    check("and an anchor further off than the place is big does not",
          3000 not in doors, f"doors at {doors}")

    # A grace belongs to the session the death was in. Die, quit, and start
    # the game again inside three minutes and the first stored sample of the
    # new session carries a load screen -- starting the game *is* load
    # screens -- so yesterday's death claimed wherever you happened to load
    # in. Nothing in routes.db does it, which is why it took a fixture.
    cross = gh.start_session("restarted a minute later")
    gh.add_map_event(session_id=gid, ts_ms=t + 900_000, map_id=surf,
                     layer="surface", kind="death", anchor_wx=0.0, anchor_wz=0.0)
    gh.add_sample(session_id=cross, ts_ms=t + 960_000, map_id=surf,
                  layer="surface", x=0.0, y=88.0, z=0.0,
                  wx=900.0, wz=900.0, break_before=BREAK_RELOAD)
    gh.commit()
    late = [d for d in gh.deaths() if d["ts_ms"] == t + 900_000][0]
    check("a death does not take its grace from the next session",
          late["respawn"] is None,
          "claimed one" if late["respawn"] else "none")
    # And one in the same session is still found, which is the whole point.
    gh.add_sample(session_id=gid, ts_ms=t + 912_000, map_id=surf,
                  layer="surface", x=0.0, y=88.0, z=0.0,
                  wx=30.0, wz=40.0, break_before=BREAK_RELOAD)
    gh.commit()
    same = [d for d in gh.deaths() if d["ts_ms"] == t + 900_000][0]
    check("but it does take one from its own",
          same["respawn"] is not None and round(same["respawn"]["wx"]) == 30,
          f'{same["respawn"]["after_s"]}s later' if same["respawn"] else "none")

    # And where the rule is wrong, it can be told. The grace is the first
    # load screen after the death, which is right almost always and cannot be
    # right every time: reported from the field, a death at 17:27:06 whose
    # next load screen at 17:27:21 is still in the room it happened in, with
    # the place the game actually put the player at 17:27:26 carrying a map
    # change -- which respawn_after() refuses on purpose, so no tuning of it
    # reaches that reading. Measured on the live database: of 282 deaths with
    # a grace, 150 have a second candidate in the window, so there is no
    # counting rule to be had either.
    #
    # The fixture is that shape: the rule's answer, and one sample further on
    # that it would never pick.
    gh.add_sample(session_id=gid, ts_ms=t + 917_000, map_id=surf,
                  layer="surface", x=0.0, y=88.0, z=0.0,
                  wx=555.0, wz=666.0, break_before=1)
    gh.commit()
    moved = gh.step_grace(t + 900_000, 1)
    stepped = [d for d in gh.deaths() if d["ts_ms"] == t + 900_000][0]
    check("a grace the recording cannot place can be moved along by hand",
          moved and stepped["respawn"] is not None
          and round(stepped["respawn"]["wx"]) == 555,
          f'at {stepped["respawn"]["wx"] if stepped["respawn"] else None}')
    # On the death, not on the sample: the break the rule reads is the game's
    # word and not ours to move, which is the same reasoning that gave
    # `death_by_hand` a kind of its own rather than a flag.
    kept = gh.db.execute(
        "SELECT grace_step FROM map_events WHERE ts_ms = ? AND kind = 'death'",
        (t + 900_000,)).fetchone()
    broke = gh.db.execute(
        "SELECT break_before FROM samples WHERE ts_ms = ?",
        (t + 917_000,)).fetchone()
    check("and the answer is kept on the death, leaving the samples alone",
          kept and kept["grace_step"] == 1 and broke["break_before"] == 1)
    gh.step_grace(t + 900_000, 0)
    back = [d for d in gh.deaths() if d["ts_ms"] == t + 900_000][0]
    check("and putting it back gives the rule's own answer again",
          back["respawn"] is not None and round(back["respawn"]["wx"]) == 30)
    # Off the end of the session is not an answer: the mark stays where the
    # rule put it rather than disappearing.
    gh.step_grace(t + 900_000, 99)
    far = [d for d in gh.deaths() if d["ts_ms"] == t + 900_000][0]
    check("and stepping past the end of the recording changes nothing",
          far["respawn"] is not None and round(far["respawn"]["wx"]) == 30)
    gh.step_grace(t + 900_000, 0)

    # Almost every read here asks for one layer over a slice of time, and
    # with separate indexes on each SQLite took the layer one -- every
    # surface row -- and sorted the lot in a temp B-tree to satisfy the
    # ORDER BY, once per call. exit_anchor() makes 125 of those per
    # interior_visits(). Measured on routes.db: 813 ms -> 13.
    plan = " | ".join(r[-1] for r in gh.db.execute(
        "EXPLAIN QUERY PLAN SELECT wx, wz, break_before FROM samples "
        "WHERE wx IS NOT NULL AND layer = 'surface' AND ts_ms >= 1 "
        "AND ts_ms <= 2 ORDER BY ts_ms LIMIT 1"))
    check("a layer over a slice of time is one index seek, not a sort",
          "idx_samples_layer_ts" in plan and "TEMP B-TREE" not in plan, plan)

    # None of the repair passes can be undone, and the recording is the one
    # thing here that cannot be made again -- so each takes a copy of the
    # database before it changes a row, and says where it put it.
    before = gh.db.execute("SELECT COUNT(*) n FROM samples").fetchone()["n"]
    kept = gh.backup_once("selftest")
    copied = None
    if kept is not None and kept.exists():
        peek = sqlite3.connect(kept)
        copied = peek.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        peek.close()
    check("a pass that rewrites rows copies the database first",
          copied == before, f"{copied} of {before} samples")
    # One run is one thing to undo, however many places it writes:
    # repair_jumps has three.
    check("and one run makes one copy, not one per write",
          gh.backup_once("selftest") is None)
    # Deletable: `with sqlite3.connect(dest)` commits without closing, and an
    # open handle on Windows locks the very file the user was just told to
    # delete when they are happy.
    try:
        kept.unlink()
        gone = True
    except OSError as e:
        gone = False
        print(f"    (could not delete the copy: {e})")
    check("and the copy is not left locked by the process that made it", gone)
    passes = {
        "repair_jumps.py": 3,     # ghosts, graces, and the jumps themselves
        "repair_breaks.py": 1,
        "repair_underground.py": 1,
    }
    for name, n in passes.items():
        src = (ROOT / "tools" / name).read_text(encoding="utf-8")
        check(f"{name} takes one before writing",
              src.count("kept_a_copy(") == n
              and "from tracker.store import" in src, f"{src.count('kept_a_copy(')} calls")
    # A real visit is not touched, however short the recording of it.
    gh2 = gh.start_session("a real cave")
    for i in range(4):
        gh.add_sample(session_id=gh2, ts_ms=t + 100_000 + i * 2500,
                      map_id=surf, layer="surface", x=0.0, y=60.0, z=0.0,
                      wx=float(i), wz=0.0, break_before=0)
    for i in range(2):
        gh.add_sample(session_id=gh2, ts_ms=t + 110_000 + i * 2500,
                      map_id=cave, layer="interior", x=float(i), y=0.0,
                      z=0.0, wx=None, wz=None,
                      break_before=(1 if not i else 0))
    for i in range(3):
        gh.add_sample(session_id=gh2, ts_ms=t + 120_000 + i * 2500,
                      map_id=surf, layer="surface", x=0.0, y=60.0, z=0.0,
                      wx=float(i), wz=0.0, break_before=0)
    gh.commit()
    check("walking in is a visit however brief, because there was no load screen",
          not gh.ghost_stays())
    gh.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(gh_db) + suffix)
        if q.exists():
            q.unlink()

    # A hole a coarse recorder leaves. The multiple alone could never reach
    # one: eight times a five-second rhythm is forty seconds, and a death
    # costs fifteen, so every death in an imported route was invisible.
    from tools.repair_jumps import quiet_after
    check("a hole is measured against the rhythm, up to a point",
          abs(quiet_after(0.25) - 4.0) < 0.01
          and abs(quiet_after(5.0) - 8.0) < 0.01,
          f"{quiet_after(0.25)}s at a quarter second, "
          f"{quiet_after(5.0)}s at five")

    # However long you stand still, the gate puts the next sample just past
    # 1.5 m -- so anything further than that plus one reading's worth of
    # walking is somewhere you were put rather than somewhere you walked.
    print("\nfurther than standing still could take you")
    from tools.repair_jumps import find_gate_jumps
    gate_db = ROOT / "data" / "selftest_gate.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(gate_db) + suffix)
        if q.exists():
            q.unlink()
    gs = Store(gate_db)
    gsid = gs.start_session("standing about")
    t = 1_701_000_000_000
    # Four seconds of walking, at a quarter second.
    for i in range(16):
        gs.add_sample(session_id=gsid, ts_ms=t + i * 250, map_id=(60 << 24),
                      layer="surface", x=0.0, y=0.0, z=0.0,
                      wx=i * 1.6, wz=0.0, break_before=0)
    # Stood still for twelve seconds, then walked on: the gate lets the next
    # one through at 1.6 m, which is exactly what standing still looks like.
    stood = t + 16 * 250 + 12_000
    gs.add_sample(session_id=gsid, ts_ms=stood, map_id=(60 << 24),
                  layer="surface", x=0.0, y=0.0, z=0.0,
                  wx=15 * 1.6 + 1.6, wz=0.0, break_before=0)
    # Then a hole of the same length that ended 40 m away, which it does not.
    gs.add_sample(session_id=gsid, ts_ms=stood + 12_000, map_id=(60 << 24),
                  layer="surface", x=0.0, y=0.0, z=0.0,
                  wx=15 * 1.6 + 41.6, wz=0.0, break_before=0)
    # And a lift: twelve seconds, no ground covered, 80 m of height. The gate
    # measures the ground, so this is standing still as far as it knows --
    # and counting the height would call every lift in the database a warp.
    gs.add_sample(session_id=gsid, ts_ms=stood + 24_000, map_id=(60 << 24),
                  layer="surface", x=0.0, y=80.0, z=0.0,
                  wx=15 * 1.6 + 41.6, wz=0.0, break_before=0)
    gs.commit()
    caught = find_gate_jumps(gs)
    check("a hole you stood through is left alone",
          all(abs(h[0]["ts_ms"] - stood) > 1 for h in caught))
    check("and one you were carried through is not",
          any(abs(h[0]["ts_ms"] - (stood + 12_000)) < 1 for h in caught),
          f"{len(caught)} found")
    check("a lift is standing still, because the gate measures the ground",
          all(abs(h[0]["ts_ms"] - (stood + 24_000)) > 1 for h in caught))
    check("the ceiling is the gate plus one reading of running",
          caught and 5.0 < caught[0][3] < 6.0, f"{caught[0][3]:.1f} m")
    gs.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(gate_db) + suffix)
        if q.exists():
            q.unlink()

    # Saying so yourself, for the death no rule can find: in a cave whose
    # grace is seven metres away there is no displacement to see it by.
    print("\nsaying you died")
    said_db = ROOT / "data" / "selftest_said.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(said_db) + suffix)
        if q.exists():
            q.unlink()
    said = Store(said_db)
    sid = said.start_session("said so")
    base = 1_700_800_000_000
    for i in range(20):
        said.add_sample(session_id=sid, ts_ms=base + i * 1000,
                        map_id=cave, layer="interior",
                        x=float(i), y=0.0, z=0.0, wx=None, wz=None,
                        break_before=0)
    said.commit()
    placed = said.mark_death_at(base + 5400)
    check("a death lands on the sample nearest the moment you point at",
          placed and placed["ts_ms"] == base + 5000,
          f'{placed and placed["ts_ms"] - base} ms in')
    # And the next recorded point is where you got up. Written as a break
    # rather than an event of its own, which makes respawn_after() find it,
    # keeps warps() from calling it a teleport, and stops the line being
    # drawn from the body to the grace -- three jobs, one row.
    check("and the next point becomes the grace you got up at",
          placed["respawn_ts"] == base + 6000,
          f'{placed["respawn_ts"] - base} ms in')
    check("which the death carries with it",
          said.deaths()[0]["respawn"]
          and said.deaths()[0]["respawn"]["ts_ms"] == base + 6000)
    check("and the line breaks there, so it is not drawn as a walk",
          said.db.execute("SELECT break_before FROM samples WHERE ts_ms = ?",
                          (base + 6000,)).fetchone()[0] == BREAK_RELOAD)
    check("and it is a death like any other",
          any(d["ts_ms"] == base + 5000 and d["by_hand"]
              for d in said.deaths()))
    check("saying it twice does not make two",
          said.mark_death_at(base + 5400)["already"])
    check("and it can be taken back", said.clear_death(base + 5000)
          and not said.deaths())
    check("which puts the line back together too",
          said.db.execute("SELECT break_before FROM samples WHERE ts_ms = ?",
                          (base + 6000,)).fetchone()[0] == 0)
    # A break the game put there is not ours to remove.
    said.db.execute("UPDATE samples SET break_before = ? WHERE ts_ms = ?",
                    (BREAK_RELOAD, base + 8000))
    said.commit()
    said.mark_death_at(base + 7000)
    said.clear_death(base + 7000)
    check("a load screen that was already there survives the undo",
          said.db.execute("SELECT break_before FROM samples WHERE ts_ms = ?",
                          (base + 8000,)).fetchone()[0] == BREAK_RELOAD)
    check("pointing at a moment nothing was recorded near says so",
          said.mark_death_at(base + 900_000) is None)

    # A map change on the sample after is taken over. `respawn_after()`
    # refuses code 1 on purpose -- walking through a door is a map change too
    # -- but when you have just said you died, the map change on the next
    # sample is the load screen that moved you.
    said.db.execute("UPDATE samples SET break_before = ? WHERE ts_ms = ?",
                    (1, base + 11_000))
    said.commit()
    placed = said.mark_death_at(base + 10_000)
    check("a map change on the next point is claimed as the grace",
          said.deaths()[0]["respawn"]
          and said.deaths()[0]["respawn"]["ts_ms"] == base + 11_000)
    check("and giving the death back gives the map change back",
          said.clear_death(placed["ts_ms"])
          and said.db.execute(
              "SELECT break_before FROM samples WHERE ts_ms = ?",
              (base + 11_000,)).fetchone()[0] == 1)
    # Which is also how a death marked before that rule existed is put right.
    said.add_map_event(session_id=sid, ts_ms=base + 10_000, map_id=cave,
                       layer="interior", kind="death_by_hand",
                       anchor_wx=None, anchor_wz=None,
                       local_x=0.0, local_z=0.0)
    said.commit()
    lost = said.lost_graces()
    check("a death marked before it has a grace to recover",
          len(lost) == 1 and lost[0]["next_ts"] == base + 11_000,
          f"{len(lost)} found")
    said.claim_grace(lost[0]["ts_ms"], lost[0]["sample_id"])
    check("and recovering it is the same answer",
          not said.lost_graces()
          and said.deaths()[0]["respawn"]["ts_ms"] == base + 11_000)
    said.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(said_db) + suffix)
        if q.exists():
            q.unlink()

    # A hole in the recording out in the world. Reported, never written
    # without being asked: unlike the dungeon rules this one has no empty
    # band under it, because Torrent at a gallop covers the same ground in
    # the same time as a short warp.
    # Quitting the game takes the tracker with it, and the judgement in that
    # is telling "the game has closed" from "the game is on a load screen".
    print("\nquitting the game")
    from tracker.source import GameSource

    class FakeMem:
        def __init__(self):
            self.up = True
            self.version, self.base = "test", 0

        def alive(self):
            return self.up

    fake = GameSource.__new__(GameSource)
    fake.mem = FakeMem()
    check("a game that is answering has not gone", not fake.gone())
    fake.mem.up = False
    check("and one bad read is not the game closing", not fake.gone())
    check("two in a row is", fake.gone())
    fake.mem.up = True
    check("and it forgets as soon as the game answers again", not fake.gone())
    fake.mem.up = False
    check("so the count starts over", not fake.gone())

    main_py = (ROOT / "tracker" / "main.py").read_text(encoding="utf-8")
    check("the recorder asks whether the game has gone",
          "if stop_with_game and source.gone():" in main_py
          and "quit_with_game.set()" in main_py)
    check("and stops when it has, rather than at the next five-second tick",
          "while not quit_with_game.is_set():" in main_py
          and "await asyncio.sleep(0.1)" in main_py)
    conf = (ROOT / "config" / "config.toml").read_text(encoding="utf-8")
    check("and it can be turned off, since the recorder is the viewer too",
          "stop_with_game = true" in conf)

    print("\nholes in the recording out in the world")
    from tools.repair_jumps import came_back, find_surface_jumps
    hole_db = ROOT / "data" / "selftest_holes.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(hole_db) + suffix)
        if q.exists():
            q.unlink()
    hole_store = Store(hole_db)
    hid = hole_store.start_session("a hole")
    t = 1_700_700_000_000
    # Twenty seconds of quarter-second walking, then nothing at all for nine
    # seconds and 120 m on, then a walk back to where it left.
    for i in range(80):
        hole_store.add_sample(session_id=hid, ts_ms=t + i * 250,
                              map_id=(60 << 24), layer="surface",
                              x=0.0, y=0.0, z=0.0, wx=i * 0.5, wz=0.0,
                              break_before=0)
    t2 = t + 80 * 250 + 9000
    hole_store.add_sample(session_id=hid, ts_ms=t2, map_id=(60 << 24),
                          layer="surface", x=0.0, y=0.0, z=0.0,
                          wx=160.0, wz=0.0, break_before=0)
    for i in range(1, 60):
        hole_store.add_sample(session_id=hid, ts_ms=t2 + i * 250,
                              map_id=(60 << 24), layer="surface",
                              x=0.0, y=0.0, z=0.0, wx=160.0 - i * 2.0, wz=0.0,
                              break_before=0)
    hole_store.commit()
    holes = find_surface_jumps(hole_store)
    check("a quiet stretch that ends a long way off is found",
          len(holes) == 1, f"{len(holes)} found")
    if holes:
        r, dist, secs, speed, why, back = holes[0]
        check("with the distance and the hole it left",
              abs(dist - 120) < 1 and abs(secs - 9.25) < 0.1,
              f"{dist:.0f} m in {secs:.1f}s")
        check("and whether the route came back, which is what a death does",
              back is not None and back < 60, f"back after {back}s")
    check("walking on does not read as coming back",
          came_back(hole_store, hid, t2, 5000.0, 5000.0) is None)
    hole_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(hole_db) + suffix)
        if q.exists():
            q.unlink()

    # A jump the sampler filed as a map change. Every tile crossing is one,
    # and so is a warp between two places that happen to be in different
    # tiles: `warps()` used to drop the lot, so nine real jumps of 214 to
    # 775 m appeared on the map as nothing at all. The distance separates
    # them and it separates them completely.
    print("\njumps filed as a map change")
    mc_db = ROOT / "data" / "selftest_mapchange.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(mc_db) + suffix)
        if q.exists():
            q.unlink()
    mc = Store(mc_db)
    mcs = mc.start_session("tiles")
    t = 1_700_900_000_000
    mc.add_sample(session_id=mcs, ts_ms=t, map_id=(60 << 24) | (44 << 16) | (34 << 8),
                  layer="surface", x=0.0, y=0.0, z=0.0, wx=0.0, wz=0.0,
                  break_before=0)
    # Walking over a tile boundary: a map change, and two metres.
    mc.add_sample(session_id=mcs, ts_ms=t + 500,
                  map_id=(60 << 24) | (44 << 16) | (35 << 8),
                  layer="surface", x=0.0, y=0.0, z=0.0, wx=2.0, wz=0.0,
                  break_before=1)
    # A warp that happens to land in another tile: a map change, and 300 m.
    mc.add_sample(session_id=mcs, ts_ms=t + 9000,
                  map_id=(60 << 24) | (44 << 16) | (36 << 8),
                  layer="surface", x=0.0, y=0.0, z=0.0, wx=302.0, wz=0.0,
                  break_before=1)
    mc.commit()
    jumps = mc.warps()
    check("a warp filed as a map change is still a warp",
          len(jumps) == 1 and round(jumps[0]["distance_m"]) == 300,
          f"{len(jumps)} returned")
    check("and walking over a tile boundary is not",
          all(j["distance_m"] > 50 for j in jumps))
    mc.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(mc_db) + suffix)
        if q.exists():
            q.unlink()

    # Walk in one mouth of a tunnel and out of the other and the surface path
    # breaks by a couple of hundred metres, but no pair of samples shows it:
    # the rows either side of the hole are in different coordinate spaces, so
    # the main query -- which needs both ends in the same space -- is blind to
    # it. Fifteen jumps of 77 m to 1,911 m were invisible on `routes.db`.
    print("\njumps that happen across a dungeon")
    tr_db = ROOT / "data" / "selftest_transit.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(tr_db) + suffix)
        if q.exists():
            q.unlink()
    tr = Store(tr_db)
    tsid = tr.start_session("through a tunnel")
    t = 1_701_100_000_000
    surf = (60 << 24)
    tr.add_sample(session_id=tsid, ts_ms=t, map_id=surf, layer="surface",
                  x=0.0, y=0.0, z=0.0, wx=0.0, wz=0.0, break_before=0)
    tr.add_sample(session_id=tsid, ts_ms=t + 1000, map_id=(32 << 24),
                  layer="interior", x=5.0, y=0.0, z=5.0, wx=None, wz=None,
                  break_before=1)
    tr.add_sample(session_id=tsid, ts_ms=t + 60_000, map_id=(32 << 24),
                  layer="interior", x=40.0, y=0.0, z=5.0, wx=None, wz=None,
                  break_before=0)
    tr.add_sample(session_id=tsid, ts_ms=t + 61_000, map_id=surf,
                  layer="surface", x=0.0, y=0.0, z=0.0, wx=200.0, wz=0.0,
                  break_before=1)
    # And a cave you came back out of, which is not a jump at all.
    tr.add_sample(session_id=tsid, ts_ms=t + 90_000, map_id=(31 << 24),
                  layer="interior", x=1.0, y=0.0, z=1.0, wx=None, wz=None,
                  break_before=1)
    tr.add_sample(session_id=tsid, ts_ms=t + 120_000, map_id=surf,
                  layer="surface", x=0.0, y=0.0, z=0.0, wx=205.0, wz=0.0,
                  break_before=1)
    tr.commit()
    jumps = tr.warps()
    across = [w for w in jumps if w["break_before"] == 4]
    check("coming out of a dungeon somewhere else is a jump",
          len(across) == 1 and round(across[0]["distance_m"]) == 200,
          f"{len(across)} found")
    check("and coming back out of the mouth you went in is not",
          all(w["distance_m"] > 50 for w in jumps), f"{len(jumps)} jumps")
    check("it carries both ends, so both can be drawn",
          across and across[0]["from_wx"] == 0.0 and across[0]["wx"] == 200.0)
    tr.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(tr_db) + suffix)
        if q.exists():
            q.unlink()

    # A sending gate puts you inside a dungeon a long way from where you were
    # standing, so the two ends of the jump are in different coordinate spaces
    # -- and the query used to require one space or the other, which dropped
    # every one of them. On routes.db that was 24 real jumps drawn as nothing
    # at all, the longest 4,735 m.
    print("\nsending gates")
    g_db = ROOT / "data" / "selftest_gate.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(g_db) + suffix)
        if q.exists():
            q.unlink()
    gs = Store(g_db)
    gsid = gs.start_session("through a gate")
    t = 1_701_200_000_000
    surf = (60 << 24)
    cave = (31 << 24)
    gs.add_sample(session_id=gsid, ts_ms=t, map_id=surf, layer="surface",
                  x=0.0, y=0.0, z=0.0, wx=0.0, wz=0.0, break_before=0)
    # The load screen the gate puts you through, so the sampler stores no
    # anchor for the way in: nothing on the surface belongs to this arrival.
    gs.add_map_event(session_id=gsid, ts_ms=t + 12_000, map_id=cave,
                     layer="interior", kind="enter",
                     anchor_wx=None, anchor_wz=None)
    gs.add_sample(session_id=gsid, ts_ms=t + 12_000, map_id=cave,
                  layer="interior", x=3.0, y=0.0, z=3.0, wx=None, wz=None,
                  break_before=3)
    gs.add_sample(session_id=gsid, ts_ms=t + 90_000, map_id=cave,
                  layer="interior", x=9.0, y=0.0, z=9.0, wx=None, wz=None,
                  break_before=0)
    gs.add_map_event(session_id=gsid, ts_ms=t + 100_000, map_id=cave,
                     layer="interior", kind="leave",
                     anchor_wx=None, anchor_wz=None)
    # Walked back out of its mouth, two kilometres from where the gate was.
    gs.add_sample(session_id=gsid, ts_ms=t + 100_000, map_id=surf,
                  layer="surface", x=0.0, y=0.0, z=0.0,
                  wx=2000.0, wz=0.0, break_before=1)
    gs.commit()
    gate = [w for w in gs.warps() if w["ts_ms"] == t + 12_000]
    check("a gate into a dungeon a long way off is a jump",
          len(gate) == 1 and round(gate[0]["distance_m"]) == 2000,
          f"{len(gate)} found")
    check("and it is drawn between two places on the map",
          gate and not gate[0]["inside"]
          and gate[0]["from_wx"] == 0.0 and gate[0]["wx"] == 2000.0)
    check("on the plane the dungeon it lands in is drawn on",
          gate and gate[0]["layer"] == "surface")
    # The `wx` above is the pin the dungeon is drawn at, because the world map
    # has nowhere else to put an end that is inside one. The end itself knows
    # exactly where it is, in that dungeon's own metres -- and that was thrown
    # away, so a portal into a cave drew its mark at the cave's mouth and put
    # nothing at all on the cave. Reported from the field. On routes.db, 20 of
    # the 111 jumps have an end inside a dungeon; drawn where they happened
    # they move 10 to 527 m off the pin, median 87.
    # How big a place is, asked by three rules now -- whether a jump between
    # two maps is a door or a gate, whether an entrance anchor can be a second
    # mouth, and whether a *pin* can. One accessor, or they would be free to
    # disagree about the same cave. The fixture walks (3,3) to (9,9).
    check("how big a dungeon is is one number, not three",
          abs(gs.dungeon_extents().get(cave, 0) - (72 ** 0.5)) < 0.01,
          f"{gs.dungeon_extents().get(cave)}")
    check("and the same accessor is what the jump rule reads",
          "extents = self.dungeon_extents()" in
          (ROOT / "tracker" / "store.py").read_text(encoding="utf-8"))
    check("and the end inside it keeps the position it really has",
          # `.get`, not `[...]`: a missing key raises, and a check that
          # raises aborts the whole run instead of reporting one red line.
          gate and gate[0].get("to_inside")
          and not gate[0].get("from_inside", True)
          and gate[0]["x"] == 3.0 and gate[0]["z"] == 3.0)
    # The stay is not reported a second time as one arc from where you were to
    # where you came out: that is a teleport and a walk drawn as a teleport.
    check("and the stay is not marked again across the top of it",
          not [w for w in gs.warps() if w["break_before"] == 4],
          f"{len([w for w in gs.warps() if w['break_before'] == 4])} transits")
    gs.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(g_db) + suffix)
        if q.exists():
            q.unlink()

    print("\nnaming places")
    name_db = ROOT / "data" / "selftest_names.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(name_db) + suffix)
        if q.exists():
            q.unlink()
    name_store = Store(name_db)
    check("a name is tidied before it is stored",
          name_store.set_name(cave, "  Groveside   Cave  ") == "Groveside Cave")
    check("and comes back", name_store.names().get(cave) == "Groveside Cave")
    check("a name too long for a label is cut, not refused",
          len(name_store.set_name(cave, "x" * 500)) == Store.MAX_NAME)
    check("an empty name is the same as taking it back",
          name_store.set_name(cave, "   ") == "" and not name_store.names())
    name_store.set_name(cave, "Groveside Cave")
    check("taking it back leaves nothing behind",
          name_store.clear_name(cave) and not name_store.names())
    check("and clearing one that was never set says so",
          not name_store.clear_name(cave))
    name_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(name_db) + suffix)
        if q.exists():
            q.unlink()

    # What the route adds up to. The only part with a judgement in it is that
    # a teleport is not ground you covered.
    print("\nwhat it adds up to")
    sum_db = ROOT / "data" / "selftest_stats.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(sum_db) + suffix)
        if q.exists():
            q.unlink()
    sum_store = Store(sum_db)
    sid = sum_store.start_session("adding up")
    # 100 m walked in ten steps, then a 5,000 m teleport, then 100 m more.
    t = 1_700_600_000_000
    for i in range(11):
        sum_store.add_sample(session_id=sid, ts_ms=t + i * 1000,
                             map_id=(60 << 24), layer="surface",
                             x=0.0, y=0.0, z=0.0, wx=i * 10.0, wz=0.0,
                             break_before=0)
    sum_store.add_sample(session_id=sid, ts_ms=t + 12_000, map_id=(60 << 24),
                         layer="surface", x=0.0, y=0.0, z=0.0,
                         wx=5100.0, wz=0.0, break_before=2)
    for i in range(1, 11):
        sum_store.add_sample(session_id=sid, ts_ms=t + 12_000 + i * 1000,
                             map_id=(60 << 24), layer="surface",
                             x=0.0, y=0.0, z=0.0, wx=5100.0 + i * 10.0,
                             wz=0.0, break_before=0)
    # 50 m inside a cave and 20 m inside a castle, in their own metres.
    inside_t = t + 24_000
    for area, steps in ((31, 6), (10, 3)):
        for i in range(steps):
            sum_store.add_sample(
                session_id=sid, ts_ms=inside_t + i * 1000,
                map_id=(area << 24) | (5 << 16), layer="interior",
                x=i * 10.0, y=0.0, z=0.0, wx=None, wz=None,
                break_before=(1 if i == 0 else 0))
        inside_t += steps * 1000 + 1000
    # A second, shorter session, and a third that recorded a single reading --
    # the shape a port clash leaves behind.
    short = sum_store.start_session("a short one")
    for i in range(5):
        sum_store.add_sample(session_id=short, ts_ms=t + 90_000 + i * 1000,
                             map_id=(60 << 24), layer="surface",
                             x=0.0, y=0.0, z=0.0, wx=i * 5.0, wz=0.0,
                             break_before=0)
    stub = sum_store.start_session("debris")
    sum_store.add_sample(session_id=stub, ts_ms=t + 200_000, map_id=(60 << 24),
                         layer="surface", x=0.0, y=0.0, z=0.0,
                         wx=0.0, wz=0.0, break_before=0)
    sum_store.commit()
    figures = sum_store.stats(legacy_areas=(10,))
    check("the overworld distance counts the walking",
          abs(figures["overworld_m"] - 220) < 1,   # 200 m, plus the short one
          f'{figures["overworld_m"]} m')
    check("and not the teleport",
          figures["overworld_m"] < 5000, f'{figures["overworld_m"]} m')
    check("a dungeon's own metres count too",
          abs(figures["inside_m"] - 70) < 1, f'{figures["inside_m"]} m')
    check("and the total is the planes added up",
          figures["travelled_m"] == (figures["overworld_m"]
                                     + figures["underground_m"]
                                     + figures["inside_m"]))
    check("a castle is not counted among the caves",
          figures["dungeons"] == 1 and figures["legacy"] == 1,
          f'{figures["dungeons"]} caves, {figures["legacy"]} legacy')
    check("with no legacy areas named, every interior is a dungeon",
          sum_store.stats()["dungeons"] == 2)
    # How long you were tracked and how long you were going somewhere are two
    # different questions. Nothing is stored until you have moved, so a gap
    # always ends in a step -- what it cannot say by itself is whether you
    # walked through the whole of it or stood in a menu and then took one
    # pace. The ground covered says: the time that distance takes at walking
    # pace, and never more than the gap it happened in.
    check("active playtime is never more than the time recorded",
          0 < figures["moving_ms"] <= figures["active_ms"],
          f'{figures["moving_ms"]} of {figures["active_ms"]} ms')
    # The fixture stands still for one long gap, and standing still is the
    # whole of what this is meant to leave out.
    check("and standing still is left out of it",
          figures["moving_ms"] < figures["active_ms"],
          f'{figures["active_ms"] - figures["moving_ms"]} ms of it was still')
    # And it can never exceed the ground actually covered: every gap is
    # credited with at most the time its distance takes at walking pace, so
    # the whole total is bounded by the whole distance. A credit computed the
    # wrong way round would sail past this.
    ceiling = figures["travelled_m"] / Store.WALK_MPS * 1000.0
    check("and it is bounded by the ground covered",
          figures["moving_ms"] <= ceiling + 1,
          f'{figures["moving_ms"]} ms against {ceiling:.0f} ms of walking')

    # A fixture with the arithmetic done by hand, because the two cases the
    # rule exists to tell apart are a second of walking and a minute of
    # standing that ends in one pace, and they are the same gap to anything
    # that only counts time. Four half-second steps of exactly WALK_MPS are
    # credited in full; the eight-second gap that moved 1.6 m is credited
    # with the 400 ms that 1.6 m takes.
    still_db = ROOT / "data" / "selftest_still.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(still_db) + suffix)
        if q.exists():
            q.unlink()
    still = Store(still_db)
    ssid = still.start_session("standing about")
    base = 1_700_000_000_000
    pace = Store.WALK_MPS / 2.0        # metres per half second
    marks = [(0, 0.0), (500, pace), (1000, 2 * pace), (1500, 3 * pace),
             (9500, 3 * pace + 1.6)]   # eight seconds, one pace at the end
    for at, x in marks:
        still.add_sample(session_id=ssid, ts_ms=base + at,
                         map_id=MapId(60, 40, 40, 0).pack(), layer="surface",
                         x=0.0, y=60.0, z=0.0, wx=1000.0 + x, wz=1000.0,
                         break_before=0)
    still.commit()
    fig = still.stats()
    check("a second and a half of walking counts as a second and a half",
          abs(fig["moving_ms"] - (1500 + 400)) < 2,
          f'{fig["moving_ms"]} ms, wanted 1900')
    check("while the eight seconds of standing count as the pace they end in",
          fig["active_ms"] == 9500 and fig["moving_ms"] < 2000,
          f'{fig["active_ms"]} ms recorded, {fig["moving_ms"]} ms of it moving')
    still.close()

    # The age ramp and the time slider are drawn on this axis, and what it
    # measures decides what they are fair to. Counting samples is fair to the
    # clock and unfair to the cadence: the imported routes were recorded every
    # five seconds and live capture runs four times a second, so an hour of
    # imported play is twenty times fewer rows. Measured on `routes.db` before
    # this changed, the whole of 3 and 5 August -- two evenings of real play --
    # came to 2 of the 100 buckets. Two sittings of the same length, recorded
    # at different rates, must land on the same share of the ramp.
    axis_db = ROOT / "data" / "selftest_axis.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(axis_db) + suffix)
        if q.exists():
            q.unlink()
    axis = Store(axis_db)
    tile = MapId(60, 40, 40, 0).pack()
    base = 1_700_000_000_000
    slow = axis.start_session("five seconds apart")
    for i in range(13):                      # 60 s of play, 13 samples
        axis.add_sample(session_id=slow, ts_ms=base + i * 5_000, map_id=tile,
                        layer="surface", x=0.0, y=60.0, z=0.0,
                        wx=1000.0 + i, wz=1000.0, break_before=0)
    fast = axis.start_session("four a second")
    later = base + 30 * 86_400_000           # a month off in between
    for i in range(241):                     # the same 60 s of play
        axis.add_sample(session_id=fast, ts_ms=later + i * 250, map_id=tile,
                        layer="surface", x=0.0, y=60.0, z=0.0,
                        wx=2000.0 + i * 0.1, wz=1000.0, break_before=0)
    axis.commit()
    edges = axis.time_quantiles(100)
    slow_share = sum(1 for t in edges if t < later) / len(edges)
    check("the age axis is time played, not rows recorded",
          0.4 < slow_share < 0.6,
          f"the slow session takes {slow_share:.0%} of the ramp, "
          f"with {13}/{241} of the samples")
    # And the month in between passes in nothing, which is the whole reason
    # the wall clock cannot be used raw.
    gap_share = sum(1 for a, b in zip(edges, edges[1:])
                    if b - a > 86_400_000) / len(edges)
    check("and a month off passes in one step of it",
          gap_share < 0.03, f"{gap_share:.0%} of the ramp is the month")
    axis.close()

    check("the longest session is the longest one played",
          figures["session_max_ms"] == 33_000,
          f'{figures["session_max_ms"]} ms')
    check("a session that recorded no time is left out of the average",
          figures["session_mean_ms"] == (33_000 + 4_000) // 2
          and figures["sessions_played"] == 2,
          f'{figures["session_mean_ms"]} ms over {figures["sessions_played"]}')
    check("it says how much of everything there is",
          figures["samples"] == 37 and figures["sessions"] == 3
          and figures["jumps"] == 1)
    sum_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(sum_db) + suffix)
        if q.exists():
            q.unlink()

    print("\ndying where the game says nothing")
    hp_db = ROOT / "data" / "selftest_hp.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(hp_db) + suffix)
        if q.exists():
            q.unlink()

    class DiesQuietly:
        """Walk, die, settle, get up at a grace. No sentinel anywhere."""

        def __init__(self):
            self.script = (
                [Reading(cave, 10.0 + i * 2, 0.0, 10.0, 1200) for i in range(4)]
                + [Reading(cave, 18.0, 0.0, 10.0, 0)]          # hp hits zero
                + [Reading(cave, 20.0, 0.0, 12.0, 0)]          # the body settles
                + [Reading(cave, 22.0, 0.0, 14.0, 0)]
                + [Reading(cave, 90.0, 0.0, 60.0, 1200)]       # up at the grace
                + [Reading(cave, 92.0 + i, 0.0, 60.0, 1200) for i in range(3)]
            )
            self.i = -1

        def read(self):
            self.i += 1
            return self.script[self.i] if self.i < len(self.script) else None

        def describe(self):
            return "quiet death fixture"

        def close(self):
            pass

    hp_store = Store(hp_db)
    src_hp = DiesQuietly()
    clock_hp = [1_700_500_000_000]
    smp = Sampler(src_hp, hp_store, cfg, clock=lambda: clock_hp[0])
    for _ in range(len(src_hp.script)):
        clock_hp[0] += 500
        smp.step()
    smp.finish()
    hp_store.commit()

    rows = hp_store.db.execute(
        "SELECT x, z, break_before FROM samples ORDER BY ts_ms").fetchall()
    broken = [r for r in rows if r["break_before"] == 3]
    check("a death breaks the line even with no load screen",
          len(broken) == 1, f"{len(broken)} reload break(s)")
    check("and it breaks at the grace, not at the body",
          broken and broken[0]["x"] > 80,
          f'at x={broken[0]["x"]:.0f}' if broken else "nowhere")
    deaths = hp_store.deaths()
    check("the respawn is found from it",
          len(deaths) == 1 and deaths[0]["respawn"] is not None
          and deaths[0]["respawn"]["local_x"] > 80,
          f'{deaths[0]["respawn"]["local_x"]:.0f}'
          if deaths and deaths[0]["respawn"] else "none")
    # And nothing for the repair pass to do afterwards: the recorder got it.
    check("nothing left for the repair pass to find",
          not repair_jumps.find_respawns(hp_store))

    hp_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(hp_db) + suffix)
        if q.exists():
            q.unlink()

    print("\nwarps that do not announce themselves")
    blind_db = ROOT / "data" / "selftest_blind.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(blind_db) + suffix)
        if q.exists():
            q.unlink()

    class GoesQuiet:
        """Reads fine, then returns nothing at all, then is somewhere else.

        None is what GameSource gives for every kind of failure, which is
        what the map menu and a load screen look like from outside.
        """

        def __init__(self, quiet):
            self.script = (
                [Reading(surface, 5.0 + i * 4, 80.0, 5.0) for i in range(4)]
                + [None] * quiet
                + [Reading(cave, 40.0 + i, 0.0, 40.0) for i in range(4)]
            )
            self.i = -1

        def read(self):
            self.i += 1
            return self.script[self.i] if self.i < len(self.script) else None

        def describe(self):
            return "quiet fixture"

        def close(self):
            pass

    def run_quiet(store, quiet):
        src = GoesQuiet(quiet)
        clock = [1_700_400_000_000]
        smp = Sampler(src, store, cfg, clock=lambda: clock[0])
        for _ in range(len(src.script)):
            clock[0] += 500
            smp.step()
        smp.finish()
        store.commit()
        return smp

    blind_store = Store(blind_db)
    smp = run_quiet(blind_store, 40)          # long enough to be a warp
    visits = [v for v in blind_store.interior_visits() if v["map_id"] == cave]
    check("a warp the game never announced is still a warp",
          smp.blind_reads <= 40 and visits and visits[0]["placed"] != "entrance",
          f'placed by {visits[0]["placed"]}' if visits else "no visit")
    ev = blind_store.db.execute(
        "SELECT anchor_wx FROM map_events WHERE map_id = ? AND kind = 'enter'",
        (cave,)).fetchone()
    check("so the cave is not anchored where you left from",
          ev is not None and ev["anchor_wx"] is None,
          "anchored anyway" if ev and ev["anchor_wx"] is not None else "")
    blind_store.close()

    # And the other half: a couple of missed reads is jitter, not a warp.
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(blind_db) + suffix)
        if q.exists():
            q.unlink()
    jitter_store = Store(blind_db)
    run_quiet(jitter_store, 2)
    ev = jitter_store.db.execute(
        "SELECT anchor_wx FROM map_events WHERE map_id = ? AND kind = 'enter'",
        (cave,)).fetchone()
    check("but a read or two going missing is not",
          ev is not None and ev["anchor_wx"] is not None,
          "lost the anchor over nothing" if ev and ev["anchor_wx"] is None else "")
    jitter_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(blind_db) + suffix)
        if q.exists():
            q.unlink()

    print("\nwarping out is not walking out")
    hold_db = ROOT / "data" / "selftest_hold.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(hold_db) + suffix)
        if q.exists():
            q.unlink()
    hold = (11 << 24) | (10 << 16)
    far = (60 << 24) | (49 << 16) | (39 << 8)

    def run(store, script, t0):
        src = Scripted(script)
        clock = [t0]
        smp = Sampler(src, store, cfg, clock=lambda: clock[0])
        for _ in range(len(script)):
            clock[0] += 250
            smp.step()
        smp.finish()
        store.commit()

    hold_store = Store(hold_db)
    # Warp in, wander, warp out to somewhere else entirely.
    run(hold_store,
        walk(surface, [10.0, 14.0, 18.0], y=80.0, z=5.0)
        + [blank_read] * 2
        + walk(hold, [5.0, 9.0, 13.0])
        + [blank_read] * 2
        + walk(far, [200.0, 204.0, 208.0], y=80.0, z=5.0),
        1_700_200_000_000)
    visits = [v for v in hold_store.interior_visits() if v["map_id"] == hold]
    check("somewhere you only ever warp in and out of is left unplaced",
          len(visits) == 1 and visits[0]["wx"] is None,
          f'{visits[0]["placed"]} at {visits[0]["wx"]}' if visits else "no visit")

    # And the case it must not break: no entrance recorded, but you walked
    # out, so the first surface reading really is the door.
    walked_db = ROOT / "data" / "selftest_walkedout.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(walked_db) + suffix)
        if q.exists():
            q.unlink()
    walked_store = Store(walked_db)
    run(walked_store,
        walk(cave, [5.0, 9.0, 13.0])
        + walk(surface, [40.0, 44.0, 48.0], y=80.0, z=5.0),
        1_700_300_000_000)
    out = [v for v in walked_store.interior_visits() if v["map_id"] == cave]
    check("walking out still places it at the door",
          len(out) == 1 and out[0]["placed"] == "exit"
          and out[0]["wx"] is not None,
          f'{out[0]["placed"]}' if out else "no visit")

    for st_, path_ in ((hold_store, hold_db), (walked_store, walked_db)):
        st_.close()
        for suffix in ("", "-wal", "-shm"):
            q = Path(str(path_) + suffix)
            if q.exists():
                q.unlink()

    print("\nloading screens")
    check("the no-map sentinel is recognised",
          is_no_map(0xFFFFFFFF) and is_no_map(255 << 24))
    check("a real map id is not", not is_no_map((60 << 24) | (43 << 16)))

    class NoMapSource:
        """What the game reads as on a loading screen or the main menu."""

        def read(self):
            return Reading(map_id=0xFFFFFFFF, x=1.0, y=2.0, z=3.0)

        def close(self):
            pass

    blank = ROOT / "data" / "selftest_nomap.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(blank) + suffix)
        if q.exists():
            q.unlink()
    blank_store = Store(blank)
    s2 = Sampler(NoMapSource(), blank_store, cfg)
    for _ in range(50):
        s2.step()
    s2.finish()
    kept = blank_store.db.execute("SELECT COUNT(*) c FROM samples").fetchone()["c"]
    events = blank_store.db.execute("SELECT COUNT(*) c FROM map_events").fetchone()["c"]
    # A load screen is not a place. Recording it gives a phantom marker per
    # load and a console line telling you to classify "area 255".
    check("load screens are not recorded", kept == 0 and events == 0,
          f"{kept} samples, {events} events")
    check("skipped readings are counted", s2.counts["skipped"] == 50)

    # Standing still returns early, but the pending samples still have to
    # reach disk: otherwise they sit in an open write transaction that is lost
    # on a crash and locks the database against every other tool.
    idle = Sampler(NoMapSource(), blank_store, cfg)
    idle._pending = 3
    idle._last_commit = 0.0
    idle.step()
    check("an idle recorder still flushes what it is holding",
          idle._pending == 0)
    blank_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(blank) + suffix)
        if q.exists():
            q.unlink()

    print("\nlegacy import")
    # A file in the old tool's shape: the recording begins on a load screen
    # (so the sentinel has to be dropped) and then inside a cave, so the
    # entrance was never recorded -- and then walks out onto the surface. The
    # load screen sits at the front rather than between the cave and the
    # surface, because a load screen there would mean you warped out, and the
    # first reading afterwards would be wherever you warped to rather than
    # the cave mouth.
    legacy_points = [
        {"x": 0.0, "y": 0.0, "z": 0.0, "map_id": 0xFFFFFFFF,
         "map_id_str": "m255_255_255_255", "timestamp_ms": 1_700_000_000_000},
        {"x": 10.0, "y": 0.0, "z": 10.0, "map_id": (31 << 24) | (5 << 16),
         "map_id_str": "m31_05_00_00", "timestamp_ms": 1_700_000_005_000},
        {"x": 20.0, "y": -3.0, "z": 10.0, "map_id": (31 << 24) | (5 << 16),
         "map_id_str": "m31_05_00_00", "timestamp_ms": 1_700_000_010_000},
        {"x": 5.0, "y": 80.0, "z": 5.0, "map_id": (60 << 24) | (42 << 16) | (36 << 8),
         "map_id_str": "m60_42_36_00", "timestamp_ms": 1_700_000_015_000},
        {"x": 25.0, "y": 82.0, "z": 5.0, "map_id": (60 << 24) | (42 << 16) | (36 << 8),
         "map_id_str": "m60_42_36_00", "timestamp_ms": 1_700_000_020_000},
    ]
    readings, bad = import_legacy.to_readings(legacy_points)
    check("every legacy point is readable", len(readings) == 5 and bad == 0)

    imp_db = ROOT / "data" / "selftest_import.db"
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(imp_db) + suffix)
        if q.exists():
            q.unlink()
    imp_store = Store(imp_db)
    src = import_legacy.ReplaySource("route_old.json", readings)
    imp = Sampler(src, imp_store, cfg, clock=src.clock)
    for _ in range(len(readings)):
        imp.step()
    imp.finish()

    rows = imp_store.db.execute(
        "SELECT map_id, layer, x, z, wx, wz FROM samples ORDER BY ts_ms"
    ).fetchall()
    check("load screens do not survive the import",
          all(r["map_id"] != 0xFFFFFFFF for r in rows), f"{len(rows)} samples")
    cave = [r for r in rows if r["layer"] == "interior"]
    check("imported interiors keep their path, off the world plane",
          len(cave) == 2 and all(r["wx"] is None for r in cave))
    surf = [r for r in rows if r["layer"] == "surface"]
    # The old tool stored a world position too; this uses the local pair and
    # the map id, which is the only version that is right for interiors.
    check("imported surface points expand to world metres",
          len(surf) == 2 and abs(surf[0]["wx"] - (42 * 256 + 5)) < 1e-6
          and abs(surf[0]["wz"] - (36 * 256 + 5)) < 1e-6,
          f'{surf[0]["wx"]:.0f}, {surf[0]["wz"]:.0f}' if surf else "")

    sess = imp_store.sessions()[0]
    check("the session says where it was imported from",
          "route_old.json" in (sess["note"] or ""), sess["note"] or "no note")
    check("the session keeps the date it was recorded, not today's",
          sess["started_ms"] == 1_700_000_000_000)

    visits = [v for v in imp_store.interior_visits() if v["layer"] == "interior"]
    # Started inside, so there is no entrance to anchor to; coming out is the
    # next best thing, and the popup says which it is.
    check("a visit with no recorded entrance is placed at the exit",
          len(visits) == 1 and visits[0].get("placed") == "exit"
          and abs(visits[0]["wx"] - (42 * 256 + 5)) < 1e-6,
          visits[0].get("placed") if visits else "no visit")
    # The old tool saved cumulative files, so the same afternoon arrives
    # several times over. Exact millisecond timestamps are what catches it.
    seen, of = import_legacy.already_imported(imp_store, readings)
    check("a file already imported is recognised", seen > 0 and of == 5,
          f"{seen} of {of} points matched")
    fresh, _ = import_legacy.to_readings(
        [dict(p, timestamp_ms=p["timestamp_ms"] + 99_000_000)
         for p in legacy_points]
    )
    seen2, _ = import_legacy.already_imported(imp_store, fresh)
    check("a file never imported is not mistaken for one", seen2 == 0)

    # A file recorded entirely from inside: nothing to anchor to at either
    # end, so the visit has no place on the map at all. It must still come
    # back, or the path would be stored and unreachable.
    day = 24 * 60 * 60 * 1000
    # A different cave, so there is no earlier visit to borrow a position
    # from: this is the case where the place is genuinely unknown.
    other_cave = (31 << 24) | (9 << 16)
    only_inside, _ = import_legacy.to_readings(
        [dict(p, timestamp_ms=p["timestamp_ms"] + 10 * day, map_id=other_cave)
         for p in legacy_points[:2]]
    )
    src2 = import_legacy.ReplaySource("all_cave.json", only_inside)
    imp2 = Sampler(src2, imp_store, cfg, clock=src2.clock)
    for _ in range(len(only_inside)):
        imp2.step()
    imp2.finish()
    lost = [v for v in imp_store.interior_visits()
            if v["layer"] == "interior" and v["wx"] is None]
    check("a visit with no entrance and no exit is kept, not placed",
          len(lost) == 1 and lost[0].get("placed") == "unknown",
          lost[0].get("placed") if lost else "nothing returned")

    # Warping into a dungeon you have walked into before: the entrance is
    # unknown for that visit, but the place is not, so the position is
    # borrowed rather than the visit being stranded off the map.
    again, _ = import_legacy.to_readings(
        [dict(p, timestamp_ms=p["timestamp_ms"] + 20 * day)
         for p in legacy_points[:2]]
    )
    src4 = import_legacy.ReplaySource("returned.json", again)
    imp3 = Sampler(src4, imp_store, cfg, clock=src4.clock)
    for _ in range(len(again)):
        imp3.step()
    imp3.finish()
    borrowed = [v for v in imp_store.interior_visits()
                if v.get("placed") == "another visit"]
    check("a dungeon you have entered before keeps its place",
          len(borrowed) == 1 and borrowed[0]["wx"] is not None,
          f"{len(borrowed)} borrowed")

    imp_store.close()
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(imp_db) + suffix)
        if q.exists():
            q.unlink()

    print("\nserver")
    import asyncio

    async def run() -> None:
        server = Server(store, cfg, "127.0.0.1", PORT)
        await server.start()

        def _fetch(path: str):
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=20) as r:
                return r.status, r.read()

        async def get(path: str):
            # Off the loop: a blocking request from inside the loop would
            # deadlock, since the server needs that same loop to answer.
            status, body = await asyncio.to_thread(_fetch, path)
            return status, json.loads(body)

        async def get_text(path: str):
            status, body = await asyncio.to_thread(_fetch, path)
            return status, body.decode()

        def _headers(path: str):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}{path}", timeout=20
            ) as r:
                return dict(r.headers)

        def _post(path: str, body: dict):
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}{path}",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        async def post(path: str, body: dict):
            return await asyncio.to_thread(_post, path, body)

        def _delete(path: str):
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}{path}", method="DELETE"
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        async def delete(path: str):
            return await asyncio.to_thread(_delete, path)

        def _status(path: str) -> int:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}{path}", timeout=20
                ) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        st_, meta = await get("/api/meta")
        check("GET /api/meta", st_ == 200 and meta["bounds"]["n"] > 0)
        # The viewer refuses to trust a recorder older than itself, so the two
        # numbers have to agree or every session starts with a false warning.
        page = (ROOT / "viewer" / "app.js").read_text(encoding="utf-8")
        wanted = int(re.search(r"const NEEDS_API = (\d+)", page).group(1))
        # Recording is lumpy in time -- an afternoon in August, then a month
        # of nothing -- so both the age colouring and the time slider work in
        # sample counts. These are the positions where equal numbers fall.
        check("meta carries where the samples sit in time",
              len(meta.get("time_quantiles") or []) > 10,
              f"{len(meta.get('time_quantiles') or [])} steps")
        check("the quantiles are in order",
              all(a <= b for a, b in zip(meta["time_quantiles"],
                                         meta["time_quantiles"][1:])))
        check("meta says whether an underground map exists",
              isinstance(meta.get("underground_tiles"), bool))
        # A recorder must be able to tell a viewer holding the port from
        # another recorder: one should stand aside, the other is a mistake.
        check("meta says whether anything is being recorded",
              "recording" in meta, f"recording = {meta.get('recording')!r}")

        # This server has no active session, so it is the one that gives way.
        st_, gave = await post("/api/standdown", {})
        check("a viewer hands the port to a recorder", st_ == 200 and gave["ok"])
        server.active_session = 1
        st_, kept = await post("/api/standdown", {})
        check("a recorder does not", st_ == 409 and "session 1" in kept["error"])
        server.active_session = None
        server.stopping.clear()

        check("the viewer and the recorder agree on the API version",
              meta.get("api") == wanted, f"server {meta.get('api')}, page {wanted}")

        st_, route = await get("/api/route?layers=surface,underground&epsilon=4")
        check("GET /api/route", st_ == 200 and len(route["segments"]) > 0,
              f"{route['points_in']} -> {route['points_out']} points")
        check("simplification reduces payload", route["points_out"] < route["points_in"])
        seg = route["segments"][0]
        check("segments carry height", len(seg["h"]) == len(seg["xy"]))

        st_, only = await get("/api/route?layers=underground&epsilon=4")
        check("layer filter works",
              all(s["layer"] == "underground" for s in only["segments"]))

        # Samples come back ordered by time across every session, so two that
        # overlap -- an import covering an afternoon a live session also
        # covers -- would be strung into one line zig-zagging between them.
        # The first sample of a session carries a break, so nothing in
        # routes.db does this; the guard is so that neither fact has to keep
        # being true.
        overlap = store.start_session("an afternoon imported twice")
        base = 1_700_000_000_000
        for i in range(6):
            # Interleaved in time with the session above it, and a kilometre
            # away: joined, it would draw a line back and forth across the map.
            store.add_sample(session_id=overlap, ts_ms=base + i * 1000,
                             map_id=(60 << 24) | (43 << 16) | (36 << 8),
                             layer="surface", x=0.0, y=10.0, z=0.0,
                             wx=1000.0 + i, wz=1000.0, break_before=0)
            store.add_sample(session_id=overlap + 1000, ts_ms=base + i * 1000 + 500,
                             map_id=(60 << 24) | (43 << 16) | (36 << 8),
                             layer="surface", x=0.0, y=10.0, z=0.0,
                             wx=2000.0 + i, wz=2000.0, break_before=0)
        store.commit()
        st_, mixed = await get(f"/api/route?layers=surface&epsilon=0.1&t0={base}"
                               f"&t1={base + 6000}")
        far = [sg for sg in mixed["segments"]
               if any(abs(x - y) > 500 for x, y in
                      zip([p[0] for p in sg["xy"]][:-1], [p[0] for p in sg["xy"]][1:]))]
        check("two sessions recorded over the same minutes are two lines",
              not far, f"{len(far)} segment(s) jump between them")
        store.db.execute("DELETE FROM samples WHERE session_id IN (?, ?)",
                         (overlap, overlap + 1000))
        store.db.execute("DELETE FROM sessions WHERE id = ?", (overlap,))
        store.commit()

        # Every mark the viewer files by plane has to be told which plane it
        # is on. The version handshake cannot catch a field that is missing
        # from a payload -- both halves agreed on 7 while /api/warps quietly
        # never sent `layer`, so onThisPlane(undefined) filed every waygate in
        # Nokron onto the Lands Between and none of them appeared on the map
        # you were standing on.
        for path, key, field in (("/api/warps", "warps", "layer"),
                                 ("/api/deaths", "deaths", "layer"),
                                 ("/api/interiors", "visits", "plane")):
            _, payload = await get(path)
            items = payload[key]
            check(f"{path} says which map its marks belong on",
                  bool(items) and all(field in i for i in items),
                  f"{sum(1 for i in items if field in i)} of {len(items)} "
                  f"carry {field}")
        # How many times a place has killed you is the thing you want to know
        # before going back in, and it is a different fact from how many
        # times you went in -- so it gets its own badge rather than a number
        # folded into the visit count.
        check("a dungeon marker carries its death toll",
              "function deathsInDungeon" in page
              and 'class="deaths"' in page)
        check("counted through the same time window as everything else",
              "state.range[0] && d.ts <= state.range[1]" in
              page[page.index("function deathsInDungeon"):
                   page.index("function inWindow")])
        check("and it moves the moment you die, not on the next zoom",
              ".then(() => loadInteriors())" in page)
        _, css_now = await get_text("/style.css")
        check("the toll is in the death colour, opposite the visit count",
              ".cave-mark .deaths" in css_now
              and "top: -6px" in css_now[css_now.index(".cave-mark .deaths"):
                                         css_now.index(".cave-mark .deaths") + 400])

        # A frame is what makes two runs through one cave line up: without it
        # each visit pins its own first step to the shared anchor, so a run
        # that began at a grace 32 m inside is drawn setting off from the
        # door. It was learned inside drawWorldVisible, which is only ever
        # handed the legacy dungeons, so no cave ever had one.
        body = page[page.index("async function drawWorldVisible"):]
        body = body[:body.index("async function ", 10)]
        check("frames are learned for every dungeon, not just visible ones",
              "learnDungeonFrame(data.visits)" in page
              and "learnDungeonFrame" not in body)
        # And standing in a cave should show every run through it, the way
        # hovering its marker does -- not just the one you are walking.
        check("the live overlay draws every run of the dungeon you are in",
              "showInside([open, ...here], true)" in page)

        # Walking out of a cave while the five-second refresh is in flight let
        # the answer land afterwards and put the overlay back up -- pinned,
        # because that is how the live overlay draws -- so the world stayed
        # dim and the cave stayed on screen until you clicked the map.
        check("leaving a dungeon cancels a refresh already in flight",
              "refreshes.insideLive" in page
              and page.count("seq !== refreshes.insideLive") >= 3
              and "refreshes.insideLive++" in page)

        # And the viewer has to be filtering on exactly those names, or the
        # field arrives and is read off the wrong key.
        check("the viewer files a dungeon's pin by v.plane",
              "onThisPlane(v.plane)" in page)
        # A mark goes by the plane the server worked out, not by its own
        # layer: "interior" says what kind of place it happened in, not which
        # of the two maps it is drawn on. 58 of the 138 deaths in routes.db
        # carry that layer, and they land correctly today only because every
        # dungeon in it opens off the surface -- one off Siofra would put its
        # deaths over Limgrave.
        check("and a death or a teleport by the plane it belongs to",
              "function markPlane(" in page
              and "onThisPlane(mark.plane || mark.layer)" in page
              and page.count("markPlane(d)") == 1
              and page.count("markPlane(r)") == 1
              and page.count("markPlane(w)") == 1)
        # The server is the half that knows: the store reports a layer,
        # interior_visits() works out the plane, and this joins them.
        srv = (ROOT / "tracker" / "server.py").read_text(encoding="utf-8")
        check("which the server sends with every mark it hands over",
              "def _plane_of(" in srv
              and srv.count("self._plane_of(") == 3
              and "def map_planes(" in
                  (ROOT / "tracker" / "store.py").read_text(encoding="utf-8"))

        # Dying puts you back at a grace, and where that is matters as much
        # as where you went down. It needs no column of its own: a death is
        # followed by a load screen, and the first sample carrying break code
        # 3 after one is the grace you appeared at.
        _, dd = await get("/api/deaths")
        with_respawn = [d for d in dd["deaths"] if d.get("respawn")]
        check("deaths say where you got up again",
              bool(with_respawn),
              f"{len(with_respawn)} of {len(dd['deaths'])} deaths")
        if with_respawn:
            r = with_respawn[0]["respawn"]
            check("a respawn carries a time, a place and a plane",
                  r["after_s"] > 0 and r.get("layer")
                  and (r.get("xy") or r.get("local")),
                  f"{r['after_s']}s later on the {r['layer']}")
            check("and it is somewhere other than where you died",
                  all(d["respawn"]["ts"] > d["ts"] for d in with_respawn))
        # Playing the route back walks played time, not the calendar: this
        # database spans 34 days and 13 hours of walking, so anything paced
        # by the clock spends the playback watching a stationary dot.
        check("playback walks played time, not the wall clock",
              "PLAY_GAP_CAP_MS" in page and "function buildAxis(" in page)
        check("and a redraw ends it rather than leaving it on a stale path",
              "if (play.on) exitPlayback();" in page)
        check("the viewer draws them, on the map and inside dungeons",
              "respawnsInside" in page
              and "function respawnPopup(" in page
              and "function insideRespawnPopup(" in page
              and "{ cls: 'respawn', glyph: '\\u2739' }" in page
              and "cls: 'respawn', glyph: '\\u2739'," in page)

        st_, dths = await get("/api/deaths")
        check("GET /api/deaths", st_ == 200 and len(dths["deaths"]) > 0,
              f"{len(dths['deaths'])} marks")
        check("death marks carry a place and a time",
              all(d["xy"] and d["ts"] for d in dths["deaths"]))

        st_, wps = await get("/api/warps")
        check("GET /api/warps", st_ == 200 and len(wps["warps"]) > 0,
              f"{len(wps['warps'])} arrivals")
        check("a warp knows both ends and how far it was",
              all(w["xy"] and w["from_xy"] and w["distance_m"] >= 50
                  for w in wps["warps"]))
        # A respawn is a warp too, but it already carries a death mark; two
        # icons on one spot say less than one.
        death_ts = {d["ts"] for d in dths["deaths"]}
        check("a respawn is left to its death mark",
              not any(w["ts"] in death_ts for w in wps["warps"]))

        st_, ints = await get("/api/interiors")
        check("GET /api/interiors", st_ == 200 and len(ints["visits"]) > 0,
              f"{len(ints['visits'])} markers")
        # The viewer draws one marker per dungeon, so it needs the map id to
        # group by. Two visits to one cave are one place, not two.
        check("visits carry the map they were in",
              all(isinstance(v["map_id"], int) for v in ints["visits"]))
        check("visits say whether the map already shows them",
              all("world_visible" in v for v in ints["visits"]))
        check("interiors that cannot be placed come back separately",
              "unplaced" in ints and all(v["xy"] for v in ints["visits"]))

        # The interior path: stored with a NULL world position so it can never
        # reach the world plane, and only reachable through this endpoint.
        v = ints["visits"][0]
        q = f"/api/interior?map_id={v['map_id']}&t0={v['entered_ms']}"
        if v["left_ms"]:
            q += f"&t1={v['left_ms']}"
        st_, inside = await get(q)
        check("GET /api/interior", st_ == 200 and inside["ok"] and inside["points_in"] > 0,
              f"{inside['points_in']} -> {inside['points_out']} points")
        stored = store.interior_path(v["map_id"], v["entered_ms"], v["left_ms"])
        check("interior path returns every sample stored for the visit",
              inside["points_in"] == len(stored), f"{len(stored)} stored")
        local = all(abs(x) < 1000 and abs(z) < 1000
                    for seg in inside["segments"] for x, z in seg["xy"])
        check("interior path is in local axes, not world metres", local)
        # Off the loop for the same reason as get(): a blocking request from
        # inside it would wait on the server that needs that loop to answer.
        bad_status = await asyncio.to_thread(_status, "/api/interior")
        check("interior path needs its visit's handle", bad_status == 400)

        # Where a route can measure a place, the measurement wins -- and where
        # it cannot, the last word is the user's.
        #
        # A hand placement is somebody's best guess at where a doorway is. The
        # `entrance` and `exit` tiers are not guesses: they are the surface
        # position recorded on the step across the threshold. So a drag is
        # what stands in until something walks through a door, and retires the
        # moment one does. Reported from the field on Leyndell, placed by hand
        # from a teleport in when nothing had ever walked its front gate: the
        # day something did, the drag was 520 m from the door the recorder had
        # just measured, and the castle was drawn there.
        walked = next((v for v in store.interior_visits()
                       if v["placed"] == "entrance"), None)
        check("the fixture has a dungeon somebody walked into", walked is not None)
        st_, put = await post("/api/place",
                              {"map_id": walked["map_id"], "wx": 1234.0, "wz": 5678.0})
        check("POST /api/place records where a dungeon is", st_ == 200 and put["ok"])
        moved = [x for x in store.interior_visits()
                 if x["map_id"] == walked["map_id"]]
        # This one is made now, which is after everything the fixture
        # recorded -- so it is you looking at where the route put the pin and
        # saying no, and it wins. Four of the six placements in the live
        # database were being thrown away, every one of them made after the
        # doorway it was losing to and two of them within the half hour
        # before it was reported: dragging a pin did nothing at all.
        check("a hand placement made after the route measured the door wins",
              moved and all(x["placed"] == "by hand"
                            and abs(x["wx"] - 1234.0) < 1e-9 for x in moved),
              f"{[x['placed'] for x in moved]}")
        # And the other way round, which is the case the original rule was
        # written for: Leyndell, placed from a teleport in when nothing had
        # ever walked its front gate, and beaten the day something did --
        # because that walk is newer than the drag. Backdated rather than
        # re-recorded, since POST /api/place always stamps the moment it runs.
        store.db.execute("UPDATE map_places SET set_ms = ? WHERE map_id = ?",
                         (walked["entered_ms"] - 1, walked["map_id"]))
        store.db.commit()
        older = [x for x in store.interior_visits()
                 if x["map_id"] == walked["map_id"]]
        check("and one made before it gives way to the doorway",
              older and all(x["placed"] == "entrance"
                            and abs(x["wx"] - 1234.0) > 1e-6 for x in older),
              f"{[x['placed'] for x in older]}")
        st_, cleared = await post("/api/place",
                                  {"map_id": walked["map_id"], "clear": True})
        check("and it can be taken back", st_ == 200 and cleared["ok"]
              and not store.places())
        # And the other half: somewhere the route has no doorway for. Then the
        # hand placement is the answer, and it keeps the anchor it replaced --
        # a pin stands at a doorway, so dragging it says where that doorway is
        # and nothing else, and the drawing goes on being pinned by the same
        # point inside and simply moves. Overwriting the anchor outright left
        # the viewer nothing to pin to, so it centred the whole shape on the
        # pin instead: on the cave that was reported the drawing then sat
        # 96 m from the marker, whichever way you dragged it.
        # By map, not by visit: one measured doorway retires the guess for
        # every visit to that place, so a map with any measured visit at all
        # is not the case this is about.
        tiers = {}
        for x in store.interior_visits():
            tiers.setdefault(x["map_id"], set()).add(x["placed"])
        guessed_map = next((m for m, t in tiers.items()
                            if not ({"entrance", "exit"} & t)), None)
        guessed = ({"map_id": guessed_map} if guessed_map is not None else None)
        if guessed is not None:
            st_, put2 = await post(
                "/api/place",
                {"map_id": guessed["map_id"], "wx": 4321.0, "wz": 8765.0})
            hand = [x for x in store.interior_visits()
                    if x["map_id"] == guessed["map_id"]]
            check("and a place it cannot measure is still yours to put down",
                  st_ == 200 and put2["ok"]
                  and hand and all(x["placed"] == "by hand"
                                   and abs(x["wx"] - 4321.0) < 1e-9
                                   for x in hand),
                  f"{[x['placed'] for x in hand]}")
            # What the route said is carried through beside it, whatever
            # that was -- including "nothing", which is the case a hand
            # placement now mostly exists for. Where there was a position,
            # it comes with it.
            check("and the anchor it replaced is kept, to go on being pinned by",
                  hand
                  and all(x.get("door_placed") not in (None, "by hand")
                          for x in hand)
                  and all(x.get("door_wx") is not None for x in hand
                          if x.get("door_placed") != "unknown"),
                  f"{[x.get('door_placed') for x in hand]}")
            await post("/api/place", {"map_id": guessed["map_id"], "clear": True})
        st_, bad_place = await post("/api/place", {"wx": 1.0})
        check("placing needs to say which map", st_ == 400)
        # The rule itself, in the one place it is decided: per map, and only
        # the two tiers that are readings rather than inferences.
        store_py_src = (ROOT / "tracker" / "store.py").read_text(encoding="utf-8")
        check("the rule is per map, and only a measured tier retires the drag",
              'measured = {v["map_id"] for v in out' in store_py_src
              and 'if v["placed"] in ("entrance", "exit")' in store_py_src
              and 'and v["entered_ms"] > set_at.get(v["map_id"], -1)}'
                  in store_py_src
              and 'if v["map_id"] in hands and v["map_id"] not in measured:'
                  in store_py_src)
        # A place a route can never measure is put down once and kept in
        # config, so it ships with the tool rather than with one database. A
        # drag lives in `map_places`, which travels with that file and nothing
        # else: without this a fresh install draws Farum Azula and the Chapel
        # in the corner of the screen however many times somebody has already
        # worked out where they go. Measured on a copy with the drags deleted:
        # `unknown` and no position without it, `from config` at the right
        # place with it.
        coords_src = (ROOT / "tracker" / "coords.py").read_text(encoding="utf-8")
        server_src = (ROOT / "tracker" / "server.py").read_text(encoding="utf-8")
        check("a position can ship with the tool instead of with a database",
              'maps.get("map_place", {})' in coords_src
              and "def parse(cls, name: str)" in coords_src
              and "self.store.shipped = dict(self.maps.map_places)" in server_src
              and "[maps.map_place]" in
                  (ROOT / "config" / "config.toml").read_text(encoding="utf-8"))
        # Yours over the tool's, and a measured doorway over both.
        check("and your own drag still wins over the one that shipped",
              'hands = {**{k: (v, "from config") for k, v in self.shipped.items()},'
              in store_py_src
              and '**{k: (v, "by hand") for k, v in by_hand.items()}}'
                  in store_py_src)
        # And a list of what is left to do by hand, for a database that is
        # going to be shipped: every place with no position, and every place
        # standing on something weaker than a doorway. It has to agree with
        # the store about which tiers those are.
        places_py = (ROOT / "tools" / "places.py")
        places_src = places_py.read_text(encoding="utf-8") if places_py.exists() else ""
        check("and a list of which places still need putting on the map",
              'MEASURED = ("entrance", "exit")' in places_src
              # Interiors only: a visit is any stay in any map, and the
              # overworld tiles are drawn by the route itself.
              and 'if v["layer"] not in ("interior", "unknown"):' in places_src
              and "tools/places.py" in
                  (ROOT / "docs" / "MANUAL.md").read_text(encoding="utf-8"))

        # Saying a place is nowhere is a different answer from not knowing
        # where it is, and clearing cannot stand in for it: clearing hands the
        # question back to the tiers, and they always answer. On routes.db the
        # Roundtable Hold then takes an `exit` anchor from the one time
        # leaving it did not look like a warp. It outranks a measured doorway
        # as well, which a hand placement no longer does -- "this is not
        # anywhere" is not a guess at a position, it is a statement that there
        # is none.
        v0 = walked
        st_, nw = await post("/api/place",
                             {"map_id": v0["map_id"], "nowhere": True})
        check("a dungeon can be said to be nowhere at all",
              st_ == 200 and nw.get("nowhere") is True
              and store.nowhere() == {v0["map_id"]})
        gone = [x for x in store.interior_visits()
                if x["map_id"] == v0["map_id"]]
        check("and then it has no position, whatever the route implies",
              gone and all(x["wx"] is None and x["wz"] is None for x in gone),
              f"{len(gone)} visit(s)")
        check("which is not a position, so places() does not report one",
              not store.places())
        st_, back = await post("/api/place",
                               {"map_id": v0["map_id"], "wx": 12.0, "wz": 34.0})
        check("and putting it on the map takes it back",
              st_ == 200 and store.places() and not store.nowhere())
        await post("/api/place", {"map_id": v0["map_id"], "clear": True})
        # Somewhere you can only warp to has no marker to drag, so the one
        # mechanism that could fix it was unreachable for exactly the places
        # that need it. The unplaced list places by click instead.
        # The path visuals were stored and nothing else was, which reads as
        # "it forgets my settings": the ones you notice resetting are the
        # checkboxes, not the line thickness.
        for key in ("deaths", "respawns", "warps", "interior", "follow",
                    "plane", "fold"):
            check(f"the panel remembers {key}",
                  f"'{key}'" in page and "function savePref" in page)
        check("every marker toggle goes through the remembering wrapper",
              page.count("rememberToggle(") >= 4)
        # And a death drawn on a castle's own path is still a death: it lives
        # in a different pane, so the toggle has to reach it too.
        check("turning deaths off turns off the ones inside dungeons",
              "state.deaths ? deathsInside(v) : []" in page)

        # And the same round trip over the wire, since the viewer only ever
        # sees the endpoint.
        _, warps_now = await get("/api/warps")
        # Any jump: the endpoint does not care whether it happened inside a
        # dungeon, and depending on the simulator to produce one there would
        # be three checks that quietly stop running.
        one = warps_now["warps"][0] if warps_now["warps"] else None
        check("the fixture has a jump to reinterpret", one is not None)
        if one:
            st_, said = await post("/api/death", {"ts": one["ts"]})
            check("POST /api/death calls a jump a death",
                  st_ == 200 and said["ok"])
            _, ds = await get("/api/deaths")
            check("and the death comes back marked as yours",
                  any(d["by_hand"] for d in ds["deaths"]))
            st_, undone = await post("/api/death",
                                     {"ts": said["ts_ms"], "clear": True})
            check("and can be taken back over the wire",
                  st_ == 200 and undone["cleared"])
            _, ds2 = await get("/api/deaths")
            check("leaving none of yours behind",
                  not any(d["by_hand"] for d in ds2["deaths"]))
        st_, nope = await post("/api/death", {})
        check("marking a death needs to say which jump", st_ == 400)
        st_, missing = await post("/api/death", {"ts": 1})
        check("and refuses a jump that is not there", st_ == 404)

        # A place with no place on the map has no marker to click, and its
        # panel row is three sections down -- so the one that matters most was
        # the hardest of all of them to reach. One button per map, pinned to
        # the corner of the screen, opening the inset and moving nothing.
        check("somewhere that is nowhere gets a corner of the screen",
              "function buildOffMap(" in page
              and "buildOffMap(data.unplaced || []);" in page
              and 'id="offmap"' in
                  (ROOT / "viewer" / "index.html").read_text(encoding="utf-8"))
        # Area 11 is "Legacy Dungeon" for all of Leyndell, the Roundtable Hold
        # included -- so the one place in the route that is nowhere in the
        # world also had the least useful name on it. A map can be named in
        # config; a name you give it yourself still wins over that.
        named = MapConfig(cfg)
        check("a map can be named where its area label cannot say it",
              named.map_labels.get("m11_10_00_00") == "Roundtable Hold")
        check("and that name is what everything calls it",
              named.label(MapId.unpack(185204736)) == "Roundtable Hold"
              and named.label(MapId.unpack(503316480)) == "Catacombs")
        sheet = (ROOT / "viewer" / "style.css").read_text(encoding="utf-8")
        disc = sheet[sheet.index("#offmap button {"):]
        disc = disc[:disc.index("}")]
        # The window comes out of the disc rather than appearing in the far
        # corner: same left edge, sitting just above it, grown from that
        # corner -- and capped, or at 535px tall it runs off a short screen.
        check("and the window it opens comes out of it",
              "#inset.from-corner" in sheet
              and "@keyframes inset-open" in sheet
              and "transform-origin: 0 100%;" in sheet
              and "max-height: calc(100vh" in sheet)
        check("the corner button is a disc with the name beside it",
              "b.dataset.name = v.label;" in page
              and "#offmap button::after" in sheet
              and "border-radius: 50%;" in disc)
        check("one button per place, not per visit",
              "const best = new Map();" in
              page[page.index("function buildOffMap("):
                   page.index("function insetOffMap(")])
        off = page[page.index("function buildOffMap("):
                   page.index("function insetOffMap(")]
        check("and it opens the inset rather than moving the map",
              "openInterior(v, true);" in off)
        # Pressing it again puts the window away: the button opened it, so the
        # button is where it is closed.
        check("and a second press closes it again",
              "state.insetKey === insideKey(v)" in off
              and "closeInterior();" in off)
        check("with a way to send a placed dungeon there",
              "'Take it off the map'" in page
              and "async function unplaceDungeon(" in page
              and "{ map_id, nowhere: true }" in page)
        # And everything about such a place hangs off that one button. The
        # panel used to carry a section listing every visit -- the same list
        # in two places, one of them three sections down from the button that
        # already stood for the place. It is the window's now, which is also
        # the only way in for the control that can fix an unplaceable
        # dungeon: no marker, so no popup, so nowhere else to put it.
        inset_off = page[page.index("function insetOffMap("):
                         page.index("async function unplaceDungeon(")]
        check("every visit to a place that is nowhere hangs off its button",
              "function insetOffMap(" in page
              and "insetOffMap(v);" in page
              and "state.offmap.get(v.map_id)" in inset_off
              and "buildUnplaced" not in page
              and 'id="unplaced-box"'
                  not in (ROOT / "viewer" / "index.html").read_text(encoding="utf-8"))
        # A dungeon with no marker has nothing to drag, so this window is the
        # only way to give one a position. It stays.
        check("an unplaced dungeon can still be put somewhere by hand",
              "function startPlacing(v) {" in page
              and "put.textContent = 'Put on map';" in inset_off
              and "placeAt(e.latlng)" in page)
        # But not for a place the game itself puts nowhere. The Roundtable
        # Hold has no way in on foot and the game's own map screen draws it
        # off the terrain in a corner, so an offer to correct that is an offer
        # to make it wrong. Which places those are is a fact about the game,
        # so it is config rather than a map ID written into the viewer.
        server_py = (ROOT / "tracker" / "server.py").read_text(encoding="utf-8")
        coords_py = (ROOT / "tracker" / "coords.py").read_text(encoding="utf-8")
        check("and somewhere that is nowhere for good is not asked about",
              "if (!v.fixed) {" in inset_off
              and '"fixed": self.maps.is_nowhere(m),' in server_py
              and "def is_nowhere(" in coords_py
              and 'nowhere_maps = ["m11_10_00_00"]' in
                  (ROOT / "config" / "config.toml").read_text(encoding="utf-8"))
        # `nowhere` and `unknown` are two different answers -- one you gave,
        # one nothing could give -- and the endpoint used to send "unknown"
        # for both, throwing away what interior_visits() had worked out.
        check("and the two kinds of unplaced are told apart on the way out",
              '"placed": ("nowhere" if v["placed"] == "nowhere"' in server_py
              and 'else "unknown"),' in server_py)
        check("and escape gets you out of it without placing anything",
              "stopPlacing()" in page)

        # Deleting a session has to take its samples and map events with it:
        # samples left pointing at a session row that is gone are invisible in
        # the session list and still drawn on the map.
        temp_id = store.start_session("throwaway")
        store.add_sample(session_id=temp_id, ts_ms=1, map_id=(60 << 24),
                         layer="surface", x=1.0, y=2.0, z=3.0,
                         wx=4.0, wz=5.0, break_before=0)
        store.add_map_event(session_id=temp_id, ts_ms=1, map_id=(60 << 24),
                            layer="surface", kind="enter",
                            anchor_wx=None, anchor_wz=None)
        store.commit()
        st_, gone = await delete(f"/api/session/{temp_id}")
        check("DELETE /api/session removes the session",
              st_ == 200 and gone["ok"] and gone["samples"] == 1
              and gone["events"] == 1)
        leftover = store.db.execute(
            "SELECT (SELECT COUNT(*) FROM samples WHERE session_id = :s) "
            "+ (SELECT COUNT(*) FROM map_events WHERE session_id = :s) c",
            {"s": temp_id},
        ).fetchone()["c"]
        check("nothing is left pointing at a deleted session", leftover == 0)
        st_, again = await delete(f"/api/session/{temp_id}")
        check("deleting it twice says so, not silently", st_ == 404)

        live = store.sessions()[0]["id"]
        server.active_session = live
        st_, busy = await delete(f"/api/session/{live}")
        check("the session being recorded now is refused", st_ == 409,
              (busy.get("error") or "")[:60])
        server.active_session = None
        check("refusing it changed nothing",
              store.db.execute(
                  "SELECT COUNT(*) c FROM sessions WHERE id = ?", (live,)
              ).fetchone()["c"] == 1)

        # Opening the map a second time should find the first one rather than
        # failing on the port: a recorder serves the same viewer, so one being
        # up is the answer, not a clash.
        from tracker.main import already_serving   # noqa: E402
        check("a viewer that is already up is recognised",
              await asyncio.to_thread(already_serving, "127.0.0.1", PORT))
        check("and a port with nothing on it is not",
              not await asyncio.to_thread(already_serving, "127.0.0.1", PORT + 7))

        _, html = await get_text("/")
        check("viewer page served", '<div id="map">' in html)
        check("interior inset present in the page", 'id="inset-canvas"' in html)
        check("deaths can be toggled off", 'id="l-deaths"' in html)
        check("teleport arrivals can be toggled off", 'id="l-warps"' in html)
        check("the map can be asked to follow you", 'id="l-follow"' in html)
        check("surface and underground are one choice, not two layers",
              'id="plane-surface"' in html and 'id="plane-underground"' in html
              and 'id="l-surface"' not in html)
        check("the time window is one slider with two ends",
              'id="t-fill"' in html and 'id="t-from"' in html
              and 'id="t-to"' in html)
        check("naming a place is offered where the place is",
              "function nameDungeon" in page and "Name this place" in page)
        check("the numbers have somewhere to go",
              'id="numbers"' in html and "function buildStats" in page)
        check("the panel calls it respawning",
              "Where you respawned" in html and "Where you got up" not in html)
        # A legacy dungeon is drawn out in the open at all times, so whether it
        # also wears a pin is a different question from whether a cave does --
        # and answering it must not take the castle's path off with the icon.
        # Off by default: the place is already there to see.
        check("a legacy dungeon's pin is its own switch, and only the pin",
              'id="l-legacy"' in html
              and 'id="l-legacy" checked' not in html
              and "rememberToggle('l-legacy', 'legacy'" in page
              and "if (group[0].world_visible && !state.layers.legacy) continue;"
                  in page
              and "state.layers.legacy = on;" in page
              # The pins and nothing else: the castles are put back on the map
              # by drawWorldVisible(), which this does not reach.
              and "if (!first) loadInteriors();" in page)
        check("the session list can be expanded rather than growing forever",
              'id="more-sessions"' in html)
        # Playing one session back is the two grips plus repeat on the axis the
        # playback already has, not a second playback that knows about
        # sessions -- and a session that is not ticked is not in that axis at
        # all, since playWorld() asks the server for the same filter the map
        # does, so pressing play on a hidden one ticks it first.
        ps = (page[page.index("async function playSession("):]
              if "async function playSession(" in page else "")
        check("a session can be played back on its own",
              bool(ps)
              and "one.className = 'ghost play-one';" in page
              and "playSession(s)" in page
              and "play.from = from;" in ps and "play.until = until;" in ps
              and "playTrimUI();" in ps
              and "play.loop = true;" in ps
              and "state.sessions.add(s.id);" in ps)
        # And the list comes to the map while the playback runs. The panel has
        # one already, but during a playback it is three sections down a column
        # you have to scroll -- and the one thing you want while watching is
        # "play that evening instead". Everything that does not belong out
        # there is off it: no checkbox, since what is drawn is the playback's
        # business now, and no delete, which is not a thing to press by
        # accident while watching.
        psr = (page[page.index("function playSessionRow("):
                    page.index("function buildPlaySessions(")]
               if "function playSessionRow(" in page
               and "function buildPlaySessions(" in page else "")
        trim = (page[page.index("function playTrimUI("):][:200]
                if "function playTrimUI(" in page else "")
        check("the playback carries its own list of sessions",
              bool(psr)
              and 'id="play-sessions" hidden' in html
              and "control('play-sessions').hidden = false;" in page
              and "control('play-sessions').hidden = true;" in page
              and "row.addEventListener('click', () => playSession(s));" in psr
              and "checkbox" not in psr
              and "confirmDelete" not in psr
              # The mark says which session the two grips are around, so it is
              # kept by the one function that runs whenever they move.
              and "playSessionsMark();" in trim
              # Folded away if you do not want it there, and it remembers.
              and "savePref('psShut', shut);" in page)
        # Where the playhead is, which is a different question from what the
        # grips are around -- and one the clock cannot answer, because
        # sessions overlap: an imported afternoon and the live session
        # covering it both contain the same moment, and on routes.db four
        # sessions contain one. route() already cuts its segments at the
        # session boundary, so the run under the playhead knows.
        check("the playback says which session you are in, from the recording",
              '"session": seg.get("session"),' in server_py
              and "session: seg.session," in page
              and "play.nowRun = run;" in page
              and "function playSessionNow(" in page
              and "const s = all.find((x) => x.id === run.session);" in page
              # And the way back to the whole route, which only offers itself
              # when there is something to undo.
              and 'id="ps-all"' in html
              and "play.from = 0;\n    play.until = play.to;" in page
              and "control('ps-all').hidden = play.from <= 0 "
                  "&& play.until >= play.to;" in page
              # And it says what it is: the session on screen, not where the
              # reader is. "Where you are" was the playhead's answer written
              # as if it were the user's.
              and '<p class="ps-cap">Current session displayed</p>' in html)
        # It is the one action in that panel rather than another row of it,
        # and it is only up while there is something to undo.
        check("and the way back to the whole route is the accent",
              "#play-sessions #ps-all {" in sheet
              and "background: var(--route);" in
                  sheet[sheet.index("#play-sessions #ps-all {"):][:400])
        # Five at a time grew the panel five rows per press, and its top is
        # pinned 16px down -- so the button doing the growing walked off the
        # bottom of the screen and could not be pressed again. All of them at
        # once, with the list scrolling inside a panel that has a ceiling.
        check("and the whole list opens at once and scrolls",
              "playSessShown = Infinity;" in page
              and "more.textContent = `Show all ${newest.length}`;" in page
              and "max-height: calc(100vh - 132px);" in
                  sheet[sheet.index("#play-sessions {"):][:900]
              and "#ps-list { flex: 1 1 auto; min-height: 0; "
                  "overflow-y: auto; }" in sheet)
        # Reaching for the thickness while watching threw you out of the
        # playback, because redrawEverything() ends it before it redraws --
        # and the weight was baked into the script at build time, so it could
        # not have followed the slider anyway.
        check("the path controls reach a playback instead of ending it",
              "if (play.on) { playLookChanged(key); return; }" in page
              and "function playWeight(run)" in page
              and "weight: playLineWeight(run, open, faint)," in page
              and "function playRestyle(" in page
              # The outline is a layer per run rather than a width on one, so
              # one appearing has to be laid down under the lines, not over.
              and "if (on !== play.casingOn) {" in page
              and "state.casing > 0" in
                  page[page.index("function playLine("):
                       page.index("function playRestyle(")])
        # The mark on the timeline says where the path under the cursor was
        # walked, so it has to go when the cursor is somewhere else -- and a
        # press that begins on the path is a map drag, which can carry the
        # pointer onto the bar with the map none the wiser.
        check("the hover mark goes when the cursor reaches the bar",
              "for (const id of ['timeline', 'play-sessions', 'toll', 'inset']) {"
              in page
              and "control(id).addEventListener('pointerenter', "
                  "() => playHover(null));" in page
              and "map.getContainer().addEventListener('mouseleave', "
                  "() => playHover(null));" in page)
        # The window for a place that is nowhere is opened from a button in
        # the corner, so it has no marker to click off -- and the drawing in
        # it is the only picture of that place there is, so it zooms.
        check("the window that is nowhere closes on the map and zooms",
              "closeInterior();" in
                  page[page.index("map.on('click', (e) => {"):
                       page.index("map.on('mousemove'")]
              and "function insetZoomAt(" in page
              and "insetView.zoom * factor" in page
              and "cv.addEventListener('wheel'" in page
              and "cv.addEventListener('dblclick'" in page
              and "if (state.insetKey !== insideKey(v)) insetReset();" in page)
        # Amber at a low alpha over the dark teal mixes to an olive that is
        # neither of the two colours the page is made of. A row lifts with
        # neutral light and says it is the chosen one with the accent at full
        # strength, in a bar down its edge.
        def body(sel):
            r = sheet[sheet.index(sel):] if sel in sheet else ""
            return r[:r.index("}")] if r else ""
        check("the accent is used at full strength or not at all",
              # The chosen row: neutral light, and the accent in a bar down
              # its edge rather than washed across it.
              "inset 2px 0 0 var(--route)" in body(".ps-row.on {")
              and "rgba(224" not in body(".ps-row.on {")
              and "rgba(224" not in body(".ps-row:hover {")
              and "rgba(224" not in body("details.fold > summary:hover {")
              and "rgba(224" not in body(".picker-list button:hover {")
              # And the playback button answers with a standing state that
              # leaves the plate alone. Not the sheen travelling across it,
              # not the ring that replaced the sheen -- both of those were
              # something that happens -- and not brightening the plate
              # either: the light goes around it. Asked for as "a constant
              # hover state", then "I don't really like the brighten effect".
              and "box-shadow: 0 0 0 1px var(--acc-ring), "
                  "0 0 24px -2px var(--acc-glow);"
                  in body(".play-btn:hover {")
              and "background:" not in body(".play-btn:hover {")
              and "left: 115%" not in sheet
              and "play-ring" not in sheet)
        for control, what in (("tint-mode", "how the path is coloured"),
                              ("line-weight", "how thick it is"),
                              ("line-casing", "how much outline it carries"),
                              ("line-colour", "what colour it is"),
                              ("age-depth", "how far back the gradient reaches"),
                              ("follow-pad", "how near the edge you may get"),
                              ("dim", "how dark the map is")):
            check(f"you can set {what}", f'id="{control}"' in html)
        # Set once and then left alone, so they fold away under the controls
        # that get touched every session.
        fold = html[html.index('<details class="fold">'):]
        fold = fold[:fold.index("</details>")]
        check("thickness and outline are folded away",
              'id="line-weight"' in fold and 'id="line-casing"' in fold)
        check("the gradient's horizon is not folded away",
              'id="age-depth"' not in fold)
        # The colour-by control is no longer a <select>: the browser drew its
        # list in its own colours and left a focus ring on the closed control
        # afterwards. What replaces it has to keep the contract the rest of
        # the page reads it through -- a `value` that can be set and read, and
        # an `input` event -- or the look map silently stops restoring it and
        # the inset stops following the map's tint.
        check("the colour-by control still answers like the select it replaced",
              "<select" not in html
              and "function wirePicker(" in page
              and "Object.defineProperty(el, 'value'" in page
              and "el.dispatchEvent(new Event('input', { bubbles: true }));" in page
              and "el.dispatchEvent(new Event('change', { bubbles: true }));" in page
              and "wirePicker();" in page)
        # And a range styled with `appearance: none` is drawn by nobody, so
        # Chrome paints no filled part: the value has to reach CSS as a custom
        # property. Every place a value is set rather than dragged has to say
        # so, which is why there is a pass over all of them as well.
        check("a styled slider is told how much of it is filled",
              "function paintRange(" in page
              and "el.style.setProperty('--fill'" in page
              # Wired once, and repainted wherever a value is set rather than
              # dragged -- the align sliders going back to zero.
              and "wireRangeFills();" in page
              and "paintRanges();" in page
              and "var(--fill, 0.5)" in sheet
              # Painted on every drag, not once at boot: without this the
              # dot moved and the trail stayed where the page loaded it.
              and "el.addEventListener('input', () => paintRange(el));"
                  in page)
        # Drawn rather than shipped, so it works with no binary asset -- and
        # replaced by viewer/compass.png the moment there is one.
        check("there is a compass", 'id="compass"' in html
              and 'id="compass-svg"' in html)
        # Fetched as bytes: get_text decodes, and a PNG is not text.
        png_status, png_bytes = await asyncio.to_thread(_fetch, "/compass.png")
        check("the compass image is served",
              png_status == 200 and png_bytes[1:4] == b"PNG",
              f"{len(png_bytes)} bytes")
        check("the interior overlay has somewhere to explain itself",
              'id="caption"' in html)

        page_js = (ROOT / "viewer" / "app.js").read_text(encoding="utf-8")
        # The horizon exists because a month-old afternoon should not have to
        # share the ramp with tonight. Everything older than it is the oldest
        # colour rather than being dropped.
        check("the age gradient has a horizon to pull in",
              "AGE_DEPTHS" in page_js and "function ageWindow" in page_js)
        check("the first horizon is everything recorded",
              "[null, 'everything recorded']" in page_js)
        # And the horizon is measured back from the newest thing *drawn*, which
        # while you are playing is the sample that just arrived. Nothing moved
        # it: `state.span` is written by drawSegments(), which runs only from
        # reload(), which runs only when you zoom or change a filter -- so the
        # near end of the gradient sat where your last scroll left it.
        # Measured on a simulated session at the fifteen-minute horizon: over
        # 131 s of recording the newest end advanced 0 ms and not one drawn
        # stretch changed colour, and then one scroll moved it 203 s and
        # redrew the lot. After: 134 s over the 130 s that passed, 8 stretches
        # changing colour along the way, and the legend's older end walking
        # from 12:12 to 12:19.
        # And wherever you are standing. It sat in the branch that handles a
        # sample with a world position, so walking into a dungeon froze the
        # near end: every sample inside was then newer than the newest end,
        # clamped to the top of the ramp, and an hour in a castle came out
        # entirely in the newest colour. Reported from the field. It is above
        # every type branch now, which is also why the guard on `s.t` -- a
        # death message carries `ts` and no position at all.
        feed = (page_js[page_js.index("ws.onmessage = (ev) => {"):]
                if "ws.onmessage = (ev) => {" in page_js else "")
        check("the newest end of the gradient follows the live feed",
              "if (typeof s.t === 'number' && state.span && s.t > state.span[1]) {"
              in feed
              and feed.index("state.span[1] = s.t;")
                  < feed.index("if (s.type === 'interior') {"))
        # And it reaches what is drawn inside a dungeon. Those samples are
        # not in the route's segments -- they have no world position, which is
        # the whole reason interiors are drawn separately -- so the span came
        # from the surface alone, and a visit running past the last surface
        # sample sat beyond the near end of the ramp: everything in it clamped
        # to the newest colour, and since every horizon is measured back from
        # that same end, no setting of the slider changed anything in there.
        # Reported from the field on an offline map. Measured on routes.db at
        # the fifteen-minute horizon: the castle's points used 1 band before
        # and 12 after, and the drawn layers carry 9 distinct colours where
        # they carried one.
        check("the age ramp counts what is drawn inside a dungeon too",
              "function spanWithInsides(" in page
              and "state.span = spanWithInsides(timeSpan(segments));" in page
              and "function noteInsideSpan(" in page
              and "noteInsideSpan(data.visits);" in page
              and "state.insideSpan = t0 === Infinity ? null : [t0, t1];" in page)
        # Recoloured, not refetched: a refetch is a hundred times the work to
        # recover a picture already on the screen. playRecolour()'s idea, for
        # the map. 0.3 ms median and 1 ms worst over 340 drawn stretches,
        # against a tick of a second.
        recol = ""
        if "function recolourRoute(" in page_js:
            recol = page_js[page_js.index("function recolourRoute("):]
            recol = recol[:recol.index("\n}")]
        check("and what is already drawn is recoloured rather than fetched again",
              "item.line.setStyle({ color: colour });" in recol
              and "if (!item.line._map) { gone++; continue; }" in recol
              and "recolourRoute();" in page_js)
        # The live tail is the newest part of the route and is banded like the
        # rest of it. One flat line was right while the tail was a second of
        # uncommitted samples; nothing refetches the route while you play, so
        # it is really everything since your last scroll -- a 15-minute tail
        # drew as one colour where it should be twelve. Measured on a full
        # 4,000-point buffer: 12 runs, 12 colours, each starting on the point
        # the one before it ends on, rebuilt in 1.92 ms.
        check("and the live tail is banded like the route, not one flat line",
              "state.liveT.push(s.t);" in page_js
              and ": state.liveT.map((t) => (t === null ? null : ageBand(t)));"
                  in page_js
              and "runs.push({ from: start, upto: bandEnds ? i + 1 : i,"
                  in page_js)
        # ...and one line again in the two modes that have no bands. Written
        # the other way round -- `!flat && bands[i] === bands[start]` -- the
        # test was false all the way down where there is nothing to band, so
        # an 18-point tail came back as 18 polylines of identical colour.
        # Caught by counting the runs in each mode, not by reading it.
        check("and one line again where there is nothing to band",
              "const bandEnds = !end && !gap && !flat && bands[i] !== bands[start];"
              in page_js)
        # A break splits the tail; it does not end it. Emptying the buffer took
        # every drawn point of it off the map -- and everything since the last
        # refetch is drawn *only* there, so dying wiped the path back to
        # wherever you last happened to zoom, and zooming fetched it back.
        # Measured on a simulated session: 59 drawn points to 0 across one
        # break. The null is what `state.liveInside` has always pushed for a
        # load screen inside a dungeon.
        check("a break splits the live tail rather than taking it off the map",
              "state.live.push(null);" in page_js
              and "state.liveT.push(null);" in page_js
              and "const gap = !end && pts[i] === null;" in page_js
              and "state.live = [];" not in
                  page_js[page_js.index("if (s.type !== 'sample') return;"):])
        # And at the thickness the panel asks for, outline and all. It was
        # frozen at `weight: 3.5`, which is the default plus a little and
        # nothing like the setting at any other value: with the slider at 8
        # the committed route was 8 with a 13 outline and everything since the
        # last refetch was 3.5 with none -- which during play is everything
        # since you last happened to zoom. Nudging the slider fetched the
        # route again and the tail shrank to a couple of points, which is why
        # "adjust it back and forth" worked. The tail inside a dungeon had
        # been following the setting all along, which is the tell.
        check("and drawn at the thickness and outline the panel asks for",
              "function liveWeight() {" in page_js
              and "return Math.max(1, state.weight) + 0.6;" in page_js
              and page_js.count("liveWeight()") == 3      # both tails and it
              # The tail's own line, not the route's:  appears in drawSegments() too, so a check
              # aimed at it could not fail.
              and "renderer, color: '#0d0b08', weight: weight + state.casing,"
                  in page_js)
        # Every outline first and every line after, for the reason the route
        # keeps its casings in a group of their own: within one canvas the
        # order they go on is the order they are painted, so a run's outline
        # drawn after its neighbour's line would sit on top of it at the seam.
        check("and its outline goes under every run, not just its own",
              ("if (state.casing > 0) {" + chr(10)
               + "    for (const r of runs) {") in page_js)
        # Three things ask which band a moment is in -- the route, the tail,
        # and the pass that brings both up to date -- and they must not be
        # able to disagree.
        check("and all three ask one function which band a moment is in",
              "function ageBand(t) {" in page_js
              # Named one by one rather than counted: the count says three
              # things mention it, not that these three do.
              and "if (state.tint === 'age') return ageBand(seg.t[i]);"
                  in page_js                              # the route
              and "null : ageBand(t)" in page_js          # the live tail
              and "bandColor(ageBand(item.t))" in page_js)  # and the pass
        # A margin nobody can set is a magic number: it was 90 px in two
        # places, and there was no way to find out what 90 px looked like.
        check("the edge margin is the one the slider sets",
              "padding: effectivePad()" in page_js
              and "padding: [90, 90]" not in page_js)
        # And it is a fraction of the way to the centre, not a pixel count:
        # pixels reach the middle on the shorter axis first, so the top of the
        # slider left a wide flat slot on a wide window instead of a point.
        check("the margin reaches the centre on both axes at once",
              "function effectivePad()" in page_js
              and "state.followPad" in page_js
              and "half - from" in page_js)

        # Two CSS rules the map cannot work without, and both were broken by
        # styling that looked harmless. A marker forced out of absolute
        # positioning falls into normal flow, which offsets it by a fixed
        # number of screen pixels and makes every mark slide as you zoom; a
        # full-map canvas that takes pointer events makes everything under it
        # unclickable.
        _, css = await get_text("/style.css")
        bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)   # rules, not prose
        marks = bare[bare.index(".death-mark, .warp-mark, .cave-mark"):]
        marks = marks[:marks.index("}")]
        check("map marks stay absolutely positioned",
              "position: absolute" in marks and "position: relative" not in marks)
        check("the interior overlay does not swallow clicks",
              ".inside-pane { pointer-events: none; }" in css)
        # .slider and .ramp set a display, which beats the browser's own
        # [hidden] rule: without this every row meant to be hidden stayed up.
        check("hidden elements are actually hidden",
              "[hidden] { display: none !important; }" in css)
        # The edge margin is a boundary you set, not scenery: it shows while
        # the slider is in use and must never take a click meant for the map.
        box = bare[bare.index(".follow-box {"):]
        box = box[:box.index("}")]
        check("the edge margin stays out of the way",
              "pointer-events: none" in box and "opacity: 0" in box)
        check("the edge margin can be shown",
              ".follow-box.show" in bare)

        # Playback is a mode, and the three things that make it one are easy
        # to lose in a refactor: the route comes off the map, the timeline
        # and the toll come up, and a gap in the calendar is squeezed rather
        # than played. Each has exactly one line saying so.
        # The map shows one plane at a time, so `state.drawn` holds one plane
        # -- and the playback inherited that, playing 4,506 underground
        # samples in routes.db as a hole in the route. It asks for both and
        # switches the map as it goes, the way the live feed does.
        check("the playback covers the underground as well as the surface",
              "async function playWorld(" in page
              and "layers: 'surface,underground'," in page
              and "const world = await playWorld();" in page)
        check("and every stretch and mark knows which map it is drawn on",
              "plane: plane || (under ? 'underground' : 'surface')," in page
              and "function planeOf(layer)" in page
              and "plane: planeOf(d.layer)" in page)
        check("the map goes underground with the route",
              "function playPlane(" in page
              and "if (state.autoPlane && play.onPlane "
                  "&& play.onPlane !== state.plane) {" in page)
        # Unless told not to. Then the map stays where it was put, and the
        # other plane's path is not drawn on it -- which is the whole reason
        # the two are separate maps rather than two layers stacked.
        check("and stays put when you tell it not to",
              "rememberToggle('auto-plane', 'autoPlane'" in page
              and "if (open && (run.plane || 'surface') !== state.plane) "
                  "return null;" in page
              and 'id="auto-plane" checked' in
                  (ROOT / "viewer" / "index.html").read_text(encoding="utf-8"))
        pp = page[page.index("function playPlane("):page.index("function playDropHead(")]
        check("and takes the path and the marks with it",
              "play.group.clearLayers();" in pp
              and "play.marks.clearLayers();" in pp
              and "play.pinned.clear();" in pp)
        # The guard has to sit above the `found` return, or every cave on the
        # surface keeps its pin standing over the black of Siofra.
        pf = page[page.index("function playFlash("):page.index("function playMarkPopup(")]
        check("a pin belongs to its plane too",
              "e.plane !== state.plane" in pf and "kind === 'found'" in pf
              and pf.index("e.plane !== state.plane")
                  < pf.index("kind === 'found'"))
        # Every mark, not only the ones drawn on the world plane. A legacy
        # dungeon is `open`, so its marks go on the map -- and 45 of them in
        # routes.db carried no plane at all, which left Stormveil's deaths
        # standing over Siofra.
        check("and so does a mark made inside a dungeon",
              "plane: hit.f.plane" in page
              and "if (e.plane && e.plane !== state.plane) return;" in page)
        # The line a jump draws is the journey being made, so it belongs to
        # the moment. Drawn on a seek too, scrubbing strung one across the map
        # for every teleport in the route and left them there.
        # Sliced on the departure branch, and asked for with `in` before it
        # is cut: `.index()` raises rather than failing, which takes the
        # whole run down instead of reporting one red line.
        warp = ""
        if "  if (departs) {" in page and "// The lasting mark" in page:
            warp = page[page.index("  if (departs) {"):
                        page.index("// The lasting mark, so the playback")]
        check("the line a jump draws is only there while it is being made",
              "if (animate && arrives) {" in warp
              and "const arc = L.polyline(" in warp
              and warp.index("if (animate && arrives) {")
                  < warp.index("const arc = L.polyline("))
        # Clicking the map puts away a pinned dungeon overlay -- but during
        # playback the overlay is what the playback is drawing, so a click
        # took the dimming off and nothing put it back.
        click = page[page.index("map.on('click', (e) => {"):]
        click = click[:click.index("rememberToggle('l-interior'")]
        check("a click on the map does not undim a playback",
              "if (play.on) { playGoToHover(); return; }" in click
              and "if (play.on) return;             // Escape leaves the "
                  "playback instead" in click)
        # Hovering the drawn path says on the timeline when you were there,
        # and clicking goes to it. Hit-tested against the points rather than
        # through Leaflet's layer events: the interior overlay's pane takes no
        # pointer events at all, and it is the nearest recorded point that
        # carries the timestamp.
        check("hovering the path says when you were there",
              "function playMomentAt(" in page
              and "if (play.on) { playHover(e.latlng); return; }" in page
              and "box: boxOf(seg.xy, start, stop, tf)," in page)
        # Points, not segments, was the first try: anywhere between two
        # recorded points -- which at a coarse zoom is most of the path --
        # the hover found nothing at all.
        check("anywhere along the line counts, not just its corners",
              "function nearSegment(" in page
              and "Math.round(t[i] + (t[i + 1] - t[i]) * u)" in page)
        check("and the cursor says the path can be pressed",
              "classList.toggle('on-path', !!hit);" in page
              and "#map.on-path" in sheet)
        check("and clicking it goes there",
              "function playGoToHover(" in page
              and "playSeek(playElapsedFor(play.hoverTs));" in page
              and "control('play-hover').addEventListener('click', "
                  "playGoToHover);" in page)
        # One thin red line per death, so the timeline says where they are.
        check("the timeline marks where the deaths are",
              "function playTicks(" in page
              and "e.kind !== 'death'" in page
              and "playTicks();" in page and "#play-ticks i {" in css)
        check("and the plane you were looking at is given back",
              "play.plane0 = state.plane;" in page
              and "if (play.plane0) playPlane(play.plane0);" in page)
        # A death has to register at a speed where it is on screen for a
        # fiftieth of a second, so the whole badge answers, not just the
        # number -- and only on the way up, since playReset() calls it with 0
        # and a seek is a reset.
        check("a death makes the whole counter answer, not just the number",
              "@keyframes toll-flash" in css and "@keyframes toll-skull" in css
              and "#toll.bump { animation: toll-flash" in css)
        check("and it only jumps when the count goes up, while playing",
              "const rose = n > play.deaths;" in page
              and "if (rose && animate && box.classList) {" in page)
        check("playback takes the finished route off the map",
              "function playLayers()" in page
              and "for (const g of playLayers()) map.removeLayer(g);" in page)
        check("and puts it back when you are done",
              "for (const g of playLayers()) g.addTo(map);" in page)
        check("a cave played back dims the world, like hovering one does",
              "dimBackground(true);" in page[page.index("function playEnter("):
                                             page.index("function playLine(")])
        # And a legacy dungeon does not. It is drawn on the map at the place
        # it occupies, so its path accumulates the way the world's does --
        # never dimmed to be shown, never faint, never taken off to make room
        # for the next visit.
        enter = page[page.index("function playEnter("):
                     page.index("function playAgeAt(")]
        check("a legacy dungeon played back is not dimmed or taken away",
              "function playOpen(" in page
              and "if (playOpen(where)) {" in enter
              and "dimBackground(false);" in enter)
        line = page[page.index("function playLine("):
                    page.index("const PLAY_GLYPH")]
        check("and its path is drawn on the map, not in the cave overlay",
              "line.addTo(open ? play.group : insideGroup);" in line
              and "if (faint && !open) style.opacity = 0.4;" in line)
        check("and a mark made in one lasts like the world's",
              "const where = playOpen(e.where);" in
              page[page.index("function playFlash("):
                   page.index("function playMarkPopup(")])
        check("which the frame has to carry",
              "world_visible: !!v.world_visible," in page)
        # The age ramp is measured against the playhead, so the newest thing
        # drawn is always in the newest colour. Built once at the end of the
        # route, the whole path showed its final colours from the first frame.
        check("the age ramp follows the playhead",
              "play.rankNow = ageRank(now);" in page
              and "run.rank / play.rankNow" in page
              and "rank: ageRank(seg.t[stop - 1])," in page)
        check("and the drawn path is brought up to date as it moves",
              "function playRecolour(" in page
              and "playRecolour(!animate);" in page
              and "item.line.setStyle({ color: colour });" in page)
        # Continuously, not in the twelve bands the finished map is drawn in.
        # A band is a fact about a route that has stopped changing; during
        # playback the denominator moves under every stretch at once, so
        # twelve steps had the whole path changing colour together a few
        # times a minute, which is the snap. The ramp is continuous here and
        # the pass runs every tick -- it skips a stretch whose colour rounds
        # to the same rgb() as last time, so what it costs is a number per
        # drawn stretch and a style on the few that crossed a value.
        # And the ranks it divides are continuous too. `ageRank()` returned
        # the quantile bucket, a hundredth of the route -- so early in a
        # playback, where the playhead's own rank is the denominator, every
        # stretch on the map shared one value and 119 playhead steps in a row
        # changed nothing at all. Measured on routes.db at rankNow 0.0004: 26
        # stretches in 1 colour before, 14 after; near the end, 255 stretches
        # in at most 12 colours before and 104 after, at 0.3 ms a pass
        # against a 16 ms tick.
        rank_fn = page[page.index("function ageRank("):
                       page.index("const AGE_DEPTHS")]
        check("the age ramp is not quantised to a hundredth of the route",
              "const a = q[lo - 1], b = q[lo];" in rank_fn
              and "return (lo - 1 + f) / steps;" in rank_fn
              and "return lo / (q.length - 1);" not in rank_fn)
        # And the play since the quantiles were fetched is not all one rank.
        # It is newer than the last quantile, so it came back as exactly 1 --
        # which on the surface is the head of a long route and inside a
        # dungeon is the whole drawing, since the only thing on screen is the
        # visit you are in. Measured on a cave entered a second after the
        # last quantile: 1 colour at every horizon before, 12 at the
        # fifteen-minute one after, with the old rule put back giving 2.
        check("and the play since the quantiles were fetched gets a share of it",
              "const newest = state.span ? state.span[1] : last;" in rank_fn
              and "const extra = newest > last ? 1 : 0;" in rank_fn
              and "const steps = q.length - 1 + extra;" in rank_fn
              and "Math.min(1, (t - last) / (newest - last))" in rank_fn)
        check("and it drifts rather than stepping between twelve colours",
              "ageColor(playAgeAt(run))" in page
              and "const PLAY_RECOLOUR_MS = PLAY_TICK_MS;" in page
              and "if (colour === item.colour) continue;" in page
              and "bandColor" not in
                  page[page.index("function playAgeAt("):
                       page.index("function playLine(")])
        # A mark made inside a dungeon has only that dungeon's own metres.
        # The world position the endpoint hands back for it is the *entrance*,
        # so falling back to it drew deaths at the cave mouth with the cave's
        # real path on screen beside them.
        pe = page[page.index("function playEvents("):page.index("async function buildPlayback(")]
        check("a death inside a dungeon is never drawn at its entrance",
              "playVisitAt(frames, d.map_id, d.ts)" in pe
              and "if (!hit) continue;" in pe)
        # And so is a teleport. Reported from the field: "during playback,
        # when I teleported into a legacy dungeon, it showed that I
        # teleported to the entrance, but I actually teleported inside."
        # The playback asked one question of a jump -- `w.inside`, which
        # means *both* ends in one dungeon -- and sent everything else to the
        # world on `w.xy`, which for an end inside a dungeon is that
        # dungeon's pin. The arrival's own local position was in the payload
        # the whole time; the finished map was fixed for this and the
        # playback never was. Measured on routes.db: 15 arrivals inside a
        # dungeon, now drawn 7 to 440 px from the pin they were drawn at.
        check("and neither is a teleport that lands inside one",
              "const inHit = w.local ? playVisitAt(frames, w.map_id, w.ts) "
              ": null;" in pe
              and "playVisitAt(frames, w.from_map_id, w.from_ts) : null;" in pe
              and "const to = inHit ? inHit.f.tf(w.local[0], w.local[1]) "
                  ": w.xy;" in pe
              and "where: inHit ? inHit.key : 'world'," in pe)
        # Each end of a jump is in its own place, and resolving both against
        # the arrival's put a mark drawn in a cave's own frame into the
        # world's marks -- where it stayed for the rest of the playback,
        # standing on open ground at a spot inside a cave that had come off
        # the map. Measured on routes.db: six such departures, 14 to 270 px
        # adrift, and all six now on their cave's pin, which is the stand-in
        # the finished map draws for the same end.
        flash = page[page.index("function playFlash("):
                     page.index("// The mark arriving: too small to see")]
        check("and each end of one is drawn in the place it belongs to",
              "from_where: outHit ? outHit.key : 'world'," in pe
              and "from_pin: w.from_xy," in pe
              and "const fromOpen = playOpen(e.from_where || 'world');" in flash
              and "const fromInside = !fromOpen && fromHere;" in flash
              and "const fromXY = fromHere ? e.from : e.from_pin;" in flash
              and "playMark(kind, fromXY, fromInside ? 'playinside' "
                  ": 'playmarks'," in flash
              and "fromInside ? play.insideMarks : play.marks, e);" in flash)
        # A jump seeked past can have an arrival there is no frame for -- the
        # cave is not on the screen -- while the surface it set off from is
        # right there. The map draws that end whether or not the other one
        # can be drawn; the playback used to lose both.
        check("and one end being undrawable does not take the other with it",
              "const arrives = home || playSamePlace(play.where, e.where);"
                  in flash
              and "const departs = kind === 'warp' && !!fromXY;" in flash
              and "if (!arrives && !departs) return;" in flash
              and "if (!arrives) return;" in flash
              # The line and the pan belong to a journey with both ends on
              # the screen.
              and "if (animate && arrives) {" in flash)
        check("and it is placed by the visit it happened in, not the map",
              "frames.set(key," in page and "insideKey(v)" in
              page[page.index("let runs = playRuns(world"):
                   page.index("runs.sort((a, b) => a.t[0] - b.t[0]);")])
        # Without an HP reading a death and a teleporter are the same event,
        # and the playback is where you can see which: jump, then walk
        # straight back to where you jumped from, and you died there.
        check("a teleport can be called a death while it plays",
              "function playMarkPopup(" in page
              and "'This was a death'" in page
              and "async function playReclassify(" in page)
        check("and called back",
              "'Not a death after all'" in page and "clear: true" in
              page[page.index("function playMarkPopup("):
                   page.index("function playToll(")])
        check("the viewer shows the reason it was sent, not one it guessed",
              "reason === 'load screen'" not in page
              and "const why = list[0].reason;" in page)
        # Five minutes a second goes past a death in a fiftieth of a second,
        # so marking one by hand needs a way to walk up to it.
        # A visit with no path never drew a run, so the playback never
        # entered it and a mark made in there was never drawn -- counted on
        # the toll and invisible on the map.
        check("where the playback is comes from the clock, not from the path",
              "function playPlaceAt(" in page
              and "if (place !== play.where) playEnter(place);" in page)
        check("you can step through the path a point at a time",
              "function playStep(" in page
              and "control('play-back')" in page
              and "control('play-fwd')" in page)
        # Where the mark stands is written down as the runs are walked, not
        # read back off the line afterwards. The line is a drawing and it
        # comes and goes -- playEnter() clears the group an interior head
        # lives in, and a run reached at its first point draws no head at all
        # -- so reading the position out of it fell back to the end of the
        # *previous* run on half the steps through routes.db. A death marked
        # by hand goes where the clock is, so the mark and the death were a
        # point apart.
        head = page[page.index("function playHeadPoint()"):]
        head = head[:head.index("\n}")]
        check("the position mark reads the clock, not the drawn line",
              "play.here = tip || run.xy[n - 1];" in page
              and "getLatLngs()" not in head
              and "play.cursor - 1" not in head)
        # And it sits between two recorded points rather than on the last one
        # it went past. Points are a quarter second apart and the tick is a
        # sixtieth, so without this the head waits on the wrong side of a
        # point for several frames and then jumps -- which is what "it snaps
        # from point to point" was.
        check("and lands between two points, not on the last one passed",
              "const u = span > 0 ? (now - run.t[n - 1]) / span : 0;" in page
              and "tip = [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u];"
              in page)
        check("stepped by the time that really passed, not the nominal tick",
              "let dt = wall - play.last;" in page
              and "play.at + play.speed * dt * rate" in page)
        # The timeline is the playback's one measurement of the whole route,
        # so it is the width of the map: an inset panel made it one control
        # among several. Flush left to the sidebar, flush right and flush
        # bottom to the window.
        bar = bare[bare.index("#timeline {"):]
        bar = bar[:bar.index("}")]
        check("the timeline spans the whole map",
              "left: 348px" in bar and "right: 0" in bar and "bottom: 0" in bar)
        # The timeline is the top of the bar and everything that drives it is
        # underneath, with a step button either side of play. Nothing is on
        # the track: sitting on it covered the one thing the bar is for and
        # took every click over the middle tenth of the route with it.
        markup = (ROOT / "viewer" / "index.html").read_text(encoding="utf-8")
        check("the timeline is above the controls",
              markup.index('<div class="play-track">')
              < markup.index('<div class="play-row">'))
        mid = markup[markup.index('<div class="play-mid">'):]
        mid = mid[:mid.index("play-died")]
        check("and play sits in the middle of them, a step button either side",
              all(x in mid for x in ("play-back", "play-toggle", "play-fwd"))
              and mid.index("play-back") < mid.index("play-toggle")
                  < mid.index("play-fwd"))
        track = markup[markup.index('<div class="play-track">'):]
        track = track[:track.index("</div>")]
        check("and nothing sits on the track to swallow a seek",
              "<button" not in track and "play-scrub" in track)
        # The drag redraws as it goes rather than at the end of it, coalesced
        # to one rebuild a frame.
        drag = page[page.index("scrub.addEventListener('input'"):]
        drag = drag[:drag.index("document.addEventListener('keydown'")]
        check("scrubbing redraws the path as you drag",
              "dragging = setTimeout(" in drag
              and "if (dragging !== null) return;" in drag
              and drag.count("playSeek(") == 2)
        # Speed beside play, the moment on the left. The slider and the label
        # are the same width or the button is not on the map's middle.
        # The reading belongs to its slider, not to the far side of the play
        # button.
        check("the speed reads next to the slider it belongs to",
              all(x in mid for x in ("play-speed", "play-rate", "play-toggle"))
              and mid.index("play-speed") < mid.index("play-rate")
                  < mid.index("play-toggle"))
        # Which puts both on one side, so what goes on the other has to be
        # exactly as wide or the play button drifts off the middle of the map.
        # It used to be an element with nothing in it; it carries how long is
        # left now, which is the reading you want beside the button anyway.
        def width_of(sel):
            r = bare[bare.index(sel):]
            r = r[:r.index("}")]
            m = re.search(r"width:\s*(\d+)px", r)
            return int(m.group(1)) if m else None
        gap = 8
        # What has to hold is that the play toggle lands on the middle of the
        # map, which is half the mid group's width from its left edge. Stated
        # that way rather than as "the two flanks are equal", because the
        # flanks were unequal for as long as the transport was -- and the
        # arithmetic is the thing that has to survive the next button.
        speed = width_of("#play-speed {") + gap + width_of("#play-rate {")
        left = width_of(".play-left {")
        step = width_of(".play-transport .step {")
        toggle = width_of("#play-toggle {")
        skull = width_of(".skull-btn {")
        tgap = 6                       # .play-transport's own gap
        # reverse, back, toggle, forward, repeat, deaths
        steps_before = 2               # how many of them sit left of the toggle
        transport = step * 4 + skull + toggle + tgap * 5
        mid_gap = 12                   # .play-mid's gap
        total = speed + mid_gap + transport + mid_gap + left
        # Where the toggle's centre falls, measured from the mid group's left.
        at = (speed + mid_gap
              + steps_before * (step + tgap) + toggle / 2)
        check("and the play button is still centred on the map",
              "play-left" in mid and abs(at - total / 2) < 0.51,
              f"toggle centre {at} of {total} wide, middle is {total / 2}")
        # "5 min a second" is what you would say out loud and it is the answer
        # already; a multiplier has to be converted before it means anything,
        # since nothing else on the screen is in multiples of real time. So
        # the sentence is the label and the multiplier is on the hover.
        check("the speed reads in minutes, with the multiplier on hover",
              "min a second`" in page
              and "function speedLong(" in page
              and "rate.dataset.long = speedLong(play.speed);" in page
              and "#play-rate::after" in sheet)
        # A slider from 30 seconds a second to an hour, over detents rather
        # than a linear range -- linear spends most of its travel above half
        # an hour, where every position looks the same.
        check("the speed is a slider from 30 seconds to an hour a second",
              "const PLAY_SPEEDS = [30," in page
              and "3600];" in page[page.index("const PLAY_SPEEDS = ["):
                                   page.index("const PLAY_SPEED_DEFAULT")]
              and '<input type="range" id="play-speed"' in markup)
        speeds = page[page.index("const PLAY_SPEEDS = ["):]
        speeds = speeds[speeds.index("[") + 1:speeds.index("]")]
        stops = [int(x) for x in speeds.replace("\n", " ").split(",")]
        want = f'id="play-speed" min="0" max="{len(stops) - 1}"'
        check("and the markup agrees with the ladder about how many stops",
              want in markup.replace("\n", " ").replace("  ", " "),
              f"{len(stops)} stops")
        # One button for the mode: it starts the playback and then says how to
        # stop it. A Done button on the bar was a second place to look.
        check("one button starts the playback and stops it again",
              "function togglePlayback(" in page
              and "control('play').addEventListener('click', togglePlayback);"
              in page
              and "play-exit" not in page and "play-exit" not in markup)
        # A place announcing something is held on screen for the length of
        # the flash -- from when the drawing *entered* it, not from when the
        # event fired. Measured from the event, a death near the end of a cave
        # you had been walking through for twenty seconds kept the world dim
        # for a second and a half after the clock had left it.
        check("a place the clock played through is let go of quickly",
              "play.shown.has(e.where) ? PLAY_POP_MS : PLAY_FLASH_MS" in page
              and "play.shown.add(place);" in page)
        check("and a pin, drawn on the world map, holds no dungeon at all",
              "markKind(e) !== 'found'" in page)
        check("and marking a death rebuilds the path, not just the marks",
              "async function playRebuild(" in page
              and "await playRebuild(when);" in page)
        check("you can say you died where the recorder saw nothing",
              "async function playDiedHere(" in page
              and "callDeath({ ts: when, at: true })" in page)
        check("changing one pauses rather than walking away from the popup",
              "lasting.on('click', () => { if (play.timer) playPause(); });"
              in page)
        check("and rebuilds the playback instead of the map behind it",
              "if (play.on) return d;" in page)
        # A recorder from before the respawn half existed answers without the
        # field. The version banner says the halves disagree; this says which
        # half of what you just pressed did not happen.
        check("a recorder too old to mark the grace says so",
              "!('respawn_ts' in done)" in page)

        check("a night off is squeezed, not played",
              "Math.min(t - ts[ts.length - 1], PLAY_GAP_CAP_MS)" in page)
        # A mark arrives by growing into place: too small to see, out past
        # the size it settles at, and back to it.
        check("deaths and teleports announce themselves",
              "@keyframes mark-pop" in bare and ".pop-holder.landed > *" in bare
              and "function playLand(" in page)
        pop = bare[bare.index("@keyframes mark-pop"):]
        pop = pop[:pop.index("}", pop.index("100%")) + 1]
        check("and it overshoots before it settles",
              "scale(0.05)" in pop and "scale(1.5)" in pop
              and "scale(1)" in pop)
        # Leaflet writes `transform: translate3d(...)` on every marker icon,
        # and a keyframe setting `transform` beats an inline style -- the mark
        # would be scaled beautifully at the corner of the pane. The `scale`
        # property is its own thing and composes with the transform.
        # Leaflet writes `transform: translate3d(...)` on the icon element,
        # and the individual `scale` property multiplies it -- 23 px of slide
        # at 1.5x, measured. The pop scales what is *inside* the icon, so
        # neither the transform nor the scale property is ever set on it.
        holder = bare[bare.index(".pop-holder.landed > *"):]
        holder = holder[:holder.index("}") + 1]
        check("and it scales inside what Leaflet positions, not it",
              "animation:" in holder and "> *" in ".pop-holder.landed > *")
        check("so the marks are wrapped",
              "className: 'pop-holder'" in page
              and '<b class="${kind}-mark">' in page)
        check("the toll jumps when it changes",
              "@keyframes toll-bump" in bare and "#toll.bump b" in bare)
        # The map's own pins come off with everything else: leaving them up
        # marked every cave you were about to find, from the first frame.
        check("the dungeon pins come off the map with the rest of it",
              "deathGroup, respawnGroup, warpGroup, markerGroup]" in page)
        check("and are put back one at a time, as you walk into them",
              "function playPin(" in page and "play.pinned.has(e.map)" in page
              and "kind: 'found'" in page)
        # Where you left from and where you got up: the far half of a teleport
        # and the far half of a death, both of which used to be missing.
        # Where you went is only half of a teleport. The flash that used to
        # do this went with the rings; the lasting marks at both ends, and
        # the pop on each, are what say it now.
        flash = page[page.index("function playFlash("):page.index("function playLand(")]
        check("both ends of a teleport are marked",
              "playMark(kind, fromXY," in flash
              and "playMark(kind, e.xy," in flash
              and "playLand(left);" in flash
              and "playLand(lasting)" in flash)
        # Aimed at the loop that finds the earlier runs rather than at the
        # caption that used to describe it: the caption is gone, and a check
        # standing on a sentence rather than on the behaviour it describes
        # goes red for a wording change and green for a broken redraw.
        check("every run through a cave is drawn, not just the newest",
              "playLine(run, run.xy.length, false);" in page
              and "if (!f || f.map_id !== frame.map_id) continue;" in page)
        # A twenty-second visit is crossed inside one step at five minutes a
        # second, so a mark made in there needs the dungeon held on screen
        # long enough to be seen.
        # A death is the thing that just happened; the dot is only where you
        # are. The dot was on top and hid the mark at the one moment it is for.
        check("a mark that has just happened sits over the position mark",
              "map.createPane('playmarks').style.zIndex = 690;" in page
              and "map.createPane('playinside').style.zIndex = 700;" in page
              and "map.createPane('you').style.zIndex = 680;" in page)
        check("and the world's marks still dim when you go inside",
              "'playmarks'" in page[page.index("function dimBackground("):
                                    page.index("function interiorTransform(")])
        # The run being walked is one line handed more points each step;
        # clearing the group it lives in takes it off the map without telling
        # the loop, which then feeds points to a layer nobody can see.
        ent = page[page.index("function playEnter("):
                   page.index("function playAgeAt(")]
        check("the growing line is not left feeding a layer off the map",
              "function playDropHead()" in page
              and ent.count("insideGroup.clearLayers();")
                  == ent.count("playDropHead();") == 1)
        # And a head drawn on the map rather than in the overlay is kept, or
        # the next step draws that stretch a second time on top of itself.
        check("but a head on the map itself is not forgotten",
              "if (play.head && !playOpen(play.head.run.where)) play.head = null;"
              in page)
        # interiorTransform() reads a frame that loadInteriors() learns, and
        # that is the last thing boot does: pressing Play first drew a cave
        # from the wrong end until you pressed it again.
        check("the frames are learned before the playback is built",
              "await learnDungeonFrame(visits);" in
              page[page.index("async function buildPlayback("):
                   page.index("let runs = playRuns(world")])
        check("and a dungeon something happened in is not stepped over",
              "? PLAY_POP_MS : PLAY_FLASH_MS" in page
              and "const holding = play.hold && Date.now() < play.hold.until;"
              in page)
        # Only while playing. A seek replays every event up to the target, and
        # letting each take the place left the drawing inside whichever cave
        # had the last interior event, with the map dimmed behind it.
        check("but a seek ends where the clock says, not where the last event was",
              "if (animate && e.where !== 'world'" in page)
        # Walking in one mouth of a tunnel and out of the far one breaks the
        # surface path like a teleport and is not one.
        # A dungeon with two mouths gets an anchor at whichever one each
        # visit used, and the frame was taken from the first visit that
        # walked in -- which can be the mouth used once against one used four
        # times, with the whole dungeon drawn from the odd one out.
        srv_doors = (ROOT / "tracker" / "server.py").read_text(encoding="utf-8")
        srv_doors_store = (ROOT / "tracker" / "store.py").read_text(encoding="utf-8")
        check("a dungeon's marker goes to the doorway its visits agree on",
              "function agreedDoorway(" in page
              and "const doorway = agreedDoorway(data.visits, "
                  "group[0].map_id, routeDoor);" in page
              and "const list = doorwaysOf(here, at, false);" in page
              and "const SAME_DOOR_M = 15;" in page)
        # Which mouth is one question and which reading of it to believe is
        # another, and telling them apart needs the half of a doorway that
        # nothing was sending: where it puts you *inside*. On m31_15 two
        # readings of one mouth are 19 m apart outside and 10 m apart inside,
        # while its two real mouths are 177 m apart outside and 184 m inside
        # -- so no distance measured outside can answer it, and the coarse
        # reading was drawn as a mouth of its own standing on the real one.
        # Reported: "the cave m31_15_00_00 has the exit icon on the entrance,
        # and the normal icon is just kinda beside it."
        check("and what counts as one doorway is decided inside the place",
              "function doorwaysOf(" in page
              and "function sameMouth(" in page
              and "if (a.local && b.local && metresApart(a.local, b.local) "
                  "< SAME_DOOR_M) {" in page
              and "local: v.first_local || null" in page
              and '**({"first_local": [round(v["first_x"], 2),' in srv_doors)
        # The way out is a doorway too, and for a cave walked in one mouth and
        # out the far one it is the only reading that mouth ever gets: the
        # `exit` tier only consults it for a visit with no entrance of its
        # own. Reported in the same breath as "there is nothing in the actual
        # exit". Measured after: that far mouth lands 0 m from where the
        # recording puts it, 181 m from the pin in a place 233 m across.
        check("and the way out is one of them",
              "if (withExits && v.exit_xy && v.exit_local) {" in page
              and "found = exit_anchor(left)" in srv_doors_store
              and 'v["exit_wx"], v["exit_wz"] = found' in srv_doors_store
              and '"exit_xy": [round(c, 2) for c in' in srv_doors)
        # Bounded to one visit. A stay that stored nothing at all took the
        # first sample of a *later* stay to the same map, which on this cave
        # is 2,076 seconds afterwards and in another part of the place.
        check("and a visit's doorway is its own",
              "def first_local(map_id: int, at_or_after: int, before=None):"
                  in srv_doors_store
              and "def last_local(map_id: int, before: int, at_or_after=0):"
                  in srv_doors_store
              and "gone = last_local(v[\"map_id\"], left, v[\"entered_ms\"])"
                  in srv_doors_store)
        # Which mouth you use a place through, and which *reading* of that
        # mouth to hang the drawing on, are two questions. Time answers the
        # first and agreement answers the second: a doorway read five times
        # -- three in quarter-second capture agreeing to a metre, one in a
        # five-second import 18.6 m short of it outside and 10.2 m past it
        # inside -- hung the whole cave off the coarse reading, because the
        # visit behind it lasted longer than the other two together. The
        # readings are compared by the frame each implies, which two readings
        # of one doorway share and so do two real mouths of a dungeon whose
        # inside matches the ground above it.
        check("and is drawn from the reading of it that the others agree with",
              "const originOf = (c) => [c.xy[0] - c.local[0] * "
              "state.meta.projection.scale_x," in page
              and "metresApart(originOf(c), originOf(o)) < SAME_DOOR_M" in page
              and "if (agree.length > bestN || (agree.length === bestN "
                  "&& secs > bestT)) {" in page)
        check("and its marker goes to the same door",
              "atDoor(a) - atDoor(b)" in page)
        # A second mouth cannot be further from the first than the dungeon is
        # big -- the same test that tells a door from a gate in `warps()` and
        # stops the entrance vote overwriting a real mouth. The pins were the
        # third question of that shape and the only one not asking it, so a
        # warp into a catacomb put a pin 504 m from its door in a place 169 m
        # across. Reported: "I teleported from the overworld into a cave, and
        # it created an exit icon for the cave in the overworld where I
        # teleported from."
        #
        # Measured on routes.db: the two that cannot be mouths go (504 in 169
        # and 178 in 147), and the four that can all stay -- 19 m in a place
        # 233 across, 94 in 134, 147 in 234 and Stormveil's 543 in 654.
        mouths = ""
        if "function otherMouths(" in page:
            mouths = page[page.index("function otherMouths("):
                          page.index("function agreedDoorway(")]
        #
        # The `room &&` is load-bearing and is part of the same string: a
        # dungeon with nothing recorded inside says nothing about its own
        # size, and the rule here is the placement tiers' -- drop an anchor
        # against evidence, never for the lack of it.
        check("a pin is not put at a mouth the place is too small to have",
              "const room = group.reduce((m, v) => Math.max(m, v.extent_m || 0), 0);"
              in mouths
              and "if (room && off > room) continue;" in mouths)
        srv_i = (ROOT / "tracker" / "server.py").read_text(encoding="utf-8")
        check("and the size reaches the viewer, which has no samples to measure",
              '"extent_m": round(extents.get(v["map_id"], 0.0), 1),' in srv_i
              and "v.extent_m" in page)
        # A jump with one end inside a dungeon is drawn at that dungeon's pin
        # on the world map, for want of anywhere better -- and was left off
        # the dungeon's own drawing entirely, because the filter asked for
        # `w.inside`, which means *both* ends. Reported: "I took a portal that
        # took me into a cave, then the teleport marker was placed at the
        # entrance, instead of the position inside."
        ins = ""
        if "function warpsInside(" in page:
            ins = page[page.index("function warpsInside("):
                       page.index("function deathsInside(")]
        # Where the recording cannot say which reading is the grace, the mark
        # asks. One at a time, because a cluster can hold a dozen and only
        # you know which of them landed wrong; and the same button puts it
        # back, so there is never a choice of two directions to make.
        resp = ""
        if "function respawnPopup(" in page:
            resp = page[page.index("function respawnPopup("):]
            resp = resp[:resp.index("\n}")]
        check("a grace that landed in the wrong place can be corrected",
              "const one = n === 1 ? c.list[0] : null;" in resp
              and "'The grace is the next point'" in resp
              and "'Put the grace back'" in resp
              and "callGrace(one.died_ts, one.step ? 0 : one.step + 1);" in resp
              and "function callGrace(" in page
              and "popup: markPopupEl(m)," in page
              and "fetch('/api/grace'" in page
              and 'web.post("/api/grace", self.grace),' in srv_i
              and '"grace_step": d.get("grace_step", 0),' in srv_i)

        check("a jump with one end in a dungeon is drawn where it happened",
              "if (w.local && w.map_id === v.map_id && within(w.ts))" in ins
              and "w.from_local && w.from_map_id === v.map_id" in ins
              and "const local = which === 'to' ? jump.local : jump.from_local;"
                  in page)
        # And where it goes is a hover answer, wherever it is drawn. The world's
        # teleport marks have always drawn a line to the far end while you
        # point at them; the ones inside a dungeon had no hover at all, and a
        # legacy dungeon -- which is on the map at all times -- carried a
        # *permanent* line instead. Reported as both halves at once: "certain
        # teleport markers, in legacy dungeons at least, don't show where they
        # lead to when hovering, and some always show where, even when you're
        # not hovering."
        check("where a jump goes is shown while you point at it, and not else",
              # The cell carries the pairs and wireMarkHover() draws them, so
              # a mark that shares its spot with a death answers for both.
              "warps: always ? []" in page
              and ": c.list.map((e) => e.pair).filter((p) => p[0] && p[1])," in page
              and "if (warps.length) showWarpLines(warps, into, lineRenderer);"
                  in page
              and "function showWarpLines(pairs, into, lineRenderer) {" in page)
        # One end in this dungeon's frame and one out on the surface is still a
        # line: they are different coordinate spaces right up until both are
        # projected, and then they are the same pixels. Saying there was
        # "nothing here to join it to" left those marks pointing nowhere.
        check("and a jump with one end on the surface still says where it went",
              "const endAt = (which) => {" in page
              and "return which === 'to' ? jump.xy : jump.from_xy;" in page)
        # And which way it went. Asked for: "could you make the dotted lines
        # between teleport markers move in the teleport direction". The pair
        # is always [where you left, where you arrived] and an SVG path is
        # drawn from its first point to its last, so walking the dash offset
        # towards negative runs the dashes along the way you travelled --
        # measured on the live viewer, the path's first point is the
        # departure and the offset goes 0 to -24 over the cycle.
        #
        # SVG rather than the canvas the rest of the drawing is on: canvas
        # has no dash offset, and stepping one by hand would repaint the
        # whole route sixty times a second to shift a few dashes.
        css_jump = sheet[sheet.index("@keyframes jump-run"):] \
            if "@keyframes jump-run" in sheet else ""
        check("and the dashes run the way the jump went",
              "function jumpLine(lineRenderer) {" in page
              and "renderer: dashesIn(lineRenderer)," in page
              and "className: 'jump-line'," in page
              and "dashPanes.set(pane, L.svg({ pane, padding: 0.5 }))" in page
              # One place builds them, so a line cannot be drawn some other
              # way and quietly stand still: the world's hover, a castle's
              # hover and the line standing inside a cave all ask for it.
              and page.count("dashArray: '5,7'") == 1
              and page.count("jumpLine(lineRenderer))") == 2
              and page.count("function jumpLine(") == 1
              and "L.polyline([toLatLng(a), toLatLng(b)], "
                  "jumpLine(lineRenderer))" in page
              # -24 is two whole dash periods of 5 + 7, so the pattern
              # repeats seamlessly rather than jumping at the end of a cycle.
              and "stroke-dashoffset: -24;" in css_jump
              and "path.jump-line { animation: jump-run 1.1s linear infinite; }"
                  in css_jump
              # The direction is the extra here -- the line still says where
              # it goes without it.
              and "@media (prefers-reduced-motion: reduce) {" in css_jump)
        # The moment to test a departure against is its own: a jump *out* of
        # here carries the arrival's timestamp, which is after you left. On
        # routes.db that is all ten of them.
        check("and a jump out of one is matched by when it left, not when it landed",
              "within(w.from_ts)" in ins)
        # Saying it twice is worse than saying it once in the rougher place.
        # A castle is drawn in the open at all times and now carries that end
        # where it actually happened, so the stand-in at its pin comes off:
        # 13 of the 20 on routes.db. A cave needs no rule -- its drawing is
        # only up while you hover it, and hovering dims the world's marks.
        check("and the stand-in at the pin comes off when the place is drawn",
              "state.alwaysDrawn && state.alwaysDrawn.has(map_id)" in page
              and "const list = warpEnds(end);" in page)
        # ...but only while it is drawn. Unticking Caves and dungeons takes
        # the castles off the map, and the marks that stood down in favour of
        # them are not caves and must come back.
        # Fetch first, clear second -- the rule the route and the teleport
        # marks already follow, and the one place still doing it the other way
        # round. Clearing at the top took every castle off the map for the
        # length of the fetches, which is nothing from outside (every finished
        # visit is cached) and a flicker every five seconds while you are
        # inside one: refreshLiveInside() drops the open visit from the cache
        # each time, exactly because it is still being written, so that one is
        # a real round trip with the whole place missing for it. Measured with
        # a 60 ms delay on each interior fetch: blank from the first frame
        # before, 0 blank frames of 404 after.
        wv = (page[page.index("async function drawWorldVisible("):
                   page.index("/* --- interiors drawn where they happened")]
              if "async function drawWorldVisible(" in page
              and "/* --- interiors drawn where they happened" in page else "")
        check("the castles stay on the map while they are being redrawn",
              bool(wv)
              and "const built = [];" in wv
              and "built.push([v, d]);" in wv
              # Two clears and no more: one for the layer being switched off,
              # one for the swap. A third at the top is the regression, and
              # counting is what catches it -- asking where the *last* one
              # falls does not, since an early clear leaves that one alone.
              and wv.count("placedGroup.clearLayers();") == 2
              and wv.index("if (!state.layers.interior) {")
                  < wv.index("placedGroup.clearLayers();")
              and wv.index("built.push([v, d]);")
                  < wv.rindex("placedGroup.clearLayers();")
                  < wv.index("drawInteriorInto(layer"))
        check("and goes back on when the dungeons are turned off",
              page.count("state.alwaysDrawn = new Set();") == 2
              and "loadInteriors();" in page
              and "if (state.layers.interior) loadInteriors();" not in page)
        # Where a dungeon is, is borrowed by every mark inside it that has
        # nowhere else to be drawn. Moving it has to move all of them, and the
        # drag moved the pin and the path alone.
        # Counted against the ways a placement can change rather than against
        # a number, so adding a fifth says so instead of quietly passing.
        placers = ("async function placeDungeon(", "async function placeAt(",
                   "async function unplaceDungeon(",
                   "async function resetPlacement(")
        check("moving a dungeon moves everything drawn at its position",
              "async function afterPlacing() {" in page
              and all(f in page for f in placers)
              and page.count("await afterPlacing();") == len(placers)
              and "await loadWarps();" in
                  page[page.index("async function afterPlacing() {"):][:400])
        # A position is half a frame. The other half is which point inside the
        # place that position belongs to -- and it used to come from whatever
        # the route called the doorway at the moment of drawing, so the
        # meaning of a hand placement changed under it whenever the route
        # learned a better door.
        #
        # Leyndell is the case, reported from the field: placed by hand on
        # 5 September, when the only visit was a teleport in and the drawing
        # was therefore centred on its own extent; on the 13th somebody walked
        # in the front door, the frame rebased onto that door 440 m away
        # inside the place, and the castle -- the new run and every old one --
        # moved by that much. Measured after: the frame keeps the point the
        # drag named however the route's own door moves.
        store_py = (ROOT / "tracker" / "store.py").read_text(encoding="utf-8")
        check("a drag records which point inside the place it put there",
              "ALTER TABLE map_places ADD COLUMN local_x" in store_py
              and "def place_bases(" in store_py
              and 'local = body.get("local")' in server_py
              and '"hand_local"' in server_py
              and "async function handBase(" in page
              and "local: await handBase(v) }" in page
              and "const base = mine.length ? mine[0].hand_local : null;" in page)
        # And the way back out of one the route has outgrown. Clearing hands
        # the question to the tiers, which is exactly what is wanted once they
        # can answer it -- and it is not the same as saying a place is
        # nowhere, which is the other button on that popup.
        check("and there is a way back to the route's own answer",
              "async function resetPlacement(" in page
              and "{ map_id, clear: true }" in page
              and "Use the route's own position" in page
              and "{ map_id, nowhere: true }" in page)
        # Dragging a marker says where its doorway is, not what the drawing
        # should be pinned by. The frame is learned from the route for every
        # dungeon now -- the doorway inside, and the turn if a second door
        # gives one -- and a hand placement moves it and nothing else, so the
        # drawing travels exactly as far as the marker did. Measured on the
        # cave that was reported: the pin moved 57 m and the path now moves
        # 57 m, where before it moved 43 m in another direction and came to
        # rest 96 m from the marker.
        learn2 = ""
        if "async function learnDungeonFrame(" in page:
            learn2 = page[page.index("async function learnDungeonFrame("):
                          page.index("function drawInteriorInto(")]
        check("a hand placement moves the frame, it does not replace it",
              "const doorOf = (v) => (v.door_xy ? v.door_xy : v.xy);" in learn2
              and "frame.xy = mine[0].xy;" in learn2
              and "if (byHand.has(v.map_id)" not in learn2)
        # And where the route knows no doorway at all -- warped in, never
        # walked out -- there is nothing to pin to and the shape is centred on
        # the position given, which is what "somewhere around here" means.
        # Measured: the Chapel of Anticipation and Leyndell keep the centred
        # frame to 0 m, the cave picks up its door.
        check("and centres it only where the route knows no doorway",
              "if (frame && mine.length) {" in learn2
              and "local: [(x0 + x1) / 2, (z0 + z1) / 2], xy: mine[0].xy," in learn2)
        # A dungeon you place by hand is one place too, and it had no frame
        # at all: interiorTransform() skipped the lookup for that tier and
        # centred each drawing on its own `d.bounds` instead. So every run
        # through it was drawn somewhere different, and the run being walked
        # crawled, because its bounds grow with every step. Measured live in
        # the Chapel of Anticipation, which is hand-placed: three runs put
        # the same point inside it 25, 108 and 126 m apart, and the open run
        # slid 100 m over the five minutes it lasted, in jumps of up to 66 m.
        # After: 0, 0 and 0.
        learn = ""
        if "async function learnDungeonFrame(" in page:
            learn = page[page.index("async function learnDungeonFrame("):
                         page.index("function drawInteriorInto(")]
        tf = ""
        if "function interiorTransform(" in page:
            tf = page[page.index("function interiorTransform("):
                      page.index("async function nameDungeon(")]
        check("a dungeon put somewhere by hand is drawn in one frame like any other",
              "for (const map_id of byHand) {" in learn
              and "local: [(x0 + x1) / 2, (z0 + z1) / 2], xy: mine[0].xy," in learn
              and "if (v.placed !== 'by hand') {" not in tf)
        # How big a place is is a fact about the place. A run you are in the
        # middle of has not finished saying, and letting it decide is what
        # moved the drawing under the player. Verified by handing the open
        # run a path 500 m longer: the frame moves 0 m.
        check("and the extent that centres it comes from the runs that finished",
              "const done = mine.filter((v) => v.left_ms);" in learn
              and "for (const v of (done.length ? done : mine)) {" in learn)
        # Better no frame than the one from before the drag, which would move
        # the marker and leave the path where the inference had put it.
        check("and a drag can never be outvoted by a frame learned before it",
              "if (x0 === Infinity) { state.dungeonFrame.delete(map_id); continue; }"
              in learn
              and learn.index("for (const map_id of byHand) {")
                  > learn.index("const turn = frameTurn(frame, other, pr);"))
        # Both ends drawn at once say "these two places are related", not
        # "you went from this one to that one".
        # Inside a dungeon the position mark moved on every sample and the
        # line behind it waited for the recorder to commit and the overlay to
        # refresh, so the path trailed the mark by up to five seconds.
        check("the line inside a dungeon keeps up with the position mark",
              "function redrawLiveInside(" in page
              and "state.liveInside.push(s.local);" in page
              and "insideLiveGroup = L.layerGroup().addTo(map);" in page)
        draw_inside = page[page.index("function drawInside("):][:2600]
        # In the same frame as the path, not a task later. Within one canvas
        # the order layers go on is the order they are painted, so re-adding
        # the path puts it over the tail; putting the tail back on a
        # setTimeout left a window where a paint could catch the whole cave
        # wearing the drawing underneath, casing and all. Once every five
        # seconds, which is what "every few seconds the path flickers once"
        # was.
        check("and it survives the overlay being redrawn under it",
              "redrawLiveInside();" in draw_inside
              and "inside.drawnTo = drawnUpTo(d);" in draw_inside
              and "setTimeout(redrawLiveInside" not in page)
        # The tail used to hold everything since you walked in and nothing
        # ever took any of it back, so after nine minutes in a cave it was
        # 751 points in one hardcoded colour lying over a committed path of
        # 778 across the same 204 by 225 m -- two drawings of one walk, with
        # the flat one on top. That is the whole of "the cave I'm in, the 'by
        # age' doesn't seem to work in it": the banded drawing was never the
        # one you could see, and the height ramp was hidden just as
        # completely. Measured after: 2 to 10 points, 0.5 to 5 s of walking,
        # and the first of them is the last point of the drawing underneath,
        # so the two join rather than leaving a gap.
        tail_fn = page[page.index("function redrawLiveInside("):
                       page.index("// Looking at the world from inside a cave.")]
        check("the tail inside a dungeon starts where the drawing stops",
              "const from = inside.drawnTo || 0;" in tail_fn
              and "if (t !== null && t !== undefined && t < from)" in tail_fn)
        check("and it is banded by age like the route it is extending",
              "const flat = state.tint !== 'age';" in tail_fn
              and "ts.map((t) => (t === null || t === undefined ? null : ageBand(t)))"
              in tail_fn
              and "color: flat ? (state.tint === 'solid' ? state.colour : '#f7d488')"
              in tail_fn)
        check("and the moments it is banded by are kept with the points",
              "state.liveInsideT.push(s.t);" in page
              and "state.liveInsideT.shift();" in page
              and "state.liveInsideT = [];" in page)
        # The ramp's newest end moves on every sample wherever you are
        # standing, and nothing brought the colours up to date while that end
        # was inside a dungeon: recolourRoute() is called from the branch
        # that handles a sample with a world position, which an interior
        # sample is not. It rebuilds the surface tail on the way past and
        # ends by naming its last run as the one to append to, so the guard
        # that stops the cave being joined to the line you walked in on has
        # to be put back after it.
        inside_branch = page[page.index("if (s.type === 'interior') {"):
                             page.index("if (s.type !== 'sample') return;")]
        # Anchored on the whole line rather than on the name: commenting the
        # call out leaves "// recolourRoute();" behind, which contains the
        # name and would have kept a dead check green -- the negative test
        # said so.
        called = "\n        recolourRoute();\n"
        check("the ramp keeps moving while you are inside a dungeon",
              called in inside_branch
              and inside_branch.rindex("state.liveLayer = null;")
                  > inside_branch.index(called))
        # It arrives over four hundred milliseconds and used to vanish between
        # one frame and the next, which reads as a glitch rather than as the
        # end of something. On its own canvas, so fading it repaints one line
        # instead of the whole route.
        check("and the line it draws fades out rather than blinking off",
              "function playFadeOut(" in page
              and "playFadeOut(arc, PLAY_TRAVEL_MS + PLAY_FLASH_MS," in page
              and "line.setStyle({ opacity: ARC_OPACITY * (1 - k) });" in page)
        check("on a canvas of its own, which takes no clicks",
              "arcRenderer = L.canvas({ pane: 'playarc' });" in page
              and ".leaflet-pane.arc-pane { pointer-events: none; }" in css)
        # Following is never animated, and that is the whole of what made
        # it feel choppy. Leaflet's animated panBy eases over 250 ms and
        # PosAnimation.run() stops whatever is running before it starts, so
        # at a 16 ms tick each pan was cancelled and restarted fifteen times
        # before it could finish: the map was always part-way through an
        # ease that never completed, lagging the mark and juddering while it
        # did. Confirmed in the running viewer -- the animation begun by one
        # tick is still in progress when the next tick's call arrives.
        #
        # It takes the leap rule with it: panBy with `animate` not true
        # already sends the map straight there when the offset is wider than
        # the viewport, which is all the pixel measurement was for.
        check("following the mark is never animated, so it cannot judder",
              "map.panInside(ll, { padding: effectivePad(), animate: false });"
              in page
              and "PLAY_LEAP_SHARE" not in page
              and "play.lastAt" not in page)
        # Following is the right shape for walking -- a few pixels at a time,
        # with the ground you came from still on screen -- and the wrong one
        # for a jump, where the clock reaches the far end in a single step.
        # The minimum pan then leaves the place you have just arrived at
        # pressed against whichever edge you came in by. Measured over eight
        # teleports at zoom 5 on a 1052x900 map, the arrival settled 173 to
        # 499 px from the middle against a half-diagonal of 692; centred, it
        # is 0 px on all eight.
        check("and a teleport puts the place it took you in the middle",
              "play.centre = { xy: e.xy, until: Date.now() + PLAY_TRAVEL_MS,"
              in page
              and "setTimeout(() => { playLand(lasting); playCentre(); }, wait)"
                  in page
              and "map.setView(ll, z, { animate: false });" in page)
        # But only for a jump you cannot already see. Throwing the map across
        # for a place that was in front of you is jarring for nothing, and the
        # ordinary follow handles it: it nudges the arrival inside the edge
        # margin if it needs to and does nothing at all if it does not.
        # Measured over the 98 surface jumps on a 1052x900 map: at zoom 3, 96
        # of them are left alone and 2 still centre; at zoom 5, 64 and 34; at
        # zoom 8, 9 and 89. In a real playback at zoom 4 an 89 m jump moved
        # the map 0 px and a 4,735 m one landed dead centre.
        check("and only when the place it took you is not already on screen",
              "if (state.follow && !map.getBounds().contains(toLatLng(e.xy))) {"
              in page)
        # Asked for: a jump that goes off screen can put where it took you
        # at the same place on the screen you left from, rather than in the
        # middle, so the eye does not have to move. A setting, because which
        # of the two reads better is a matter of how you like to watch.
        #
        # The departure's screen position is taken when the jump fires, not
        # when it lands: the follow has four hundred milliseconds to move the
        # map under it in between. And it is clamped into the edge margin,
        # because a jump can fire on the frame the mark is crossing that
        # margin and landing the arrival off the screen to honour where the
        # departure was would be the opposite of the point.
        check("and it can hold the spot on the screen instead of centring",
              'id="l-hold-spot"' in markup
              and "rememberToggle('l-hold-spot', 'holdSpot', "
                  "(on) => { state.holdSpot = on; });" in page
              and "at: state.holdSpot" in page)
        check("and the arrival lands on the point the departure was at",
              "const centre = map.project(ll, z).subtract(at)"
              ".add(size.divideBy(2));" in page
              and "Math.min(Math.max(c.at.x, pad[0]), size.x - pad[0])" in page)
        # And nothing moves until the line gets there. Panning at once takes
        # the end you left from off the screen before it has finished
        # landing, which is the half of a jump the travel line exists to
        # show; panning in between creeps towards the arrival and then jumps
        # again when it lands, which is two movements for one journey.
        # Measured across the same eight: the centre does not change once in
        # the 380 ms after the jump fires.
        reset = ""
        if "function playReset() {" in page:
            reset = page[page.index("function playReset() {"):]
            reset = reset[:reset.index("\n}")]
        check("and holds the follow still while the jump is in the air",
              "const flying = play.centre && Date.now() < play.centre.until;"
              in page
              and "if (state.follow && !flying) {" in page
              and "play.centre = null;" in reset)
        # Easing off belongs to the teleport, not to the pixels. Measured
        # over one playback of routes.db at zoom 8, the pixel rule fired on
        # 246 steps against 64 actual jumps -- at a close zoom every
        # 24-second step is most of a screen -- so the whole playback was
        # slower and the teleport did not stand out at all.
        check("and the playback eases off for a teleport, not for any step",
              "if (animate && markKind(e) === 'warp') {" in page
              and "play.brake = Math.max(play.brake, Date.now() "
                  "+ playBrakeFor(e));" in page
              and "const rate = wall < play.brake ? PLAY_BRAKE_RATE : 1;"
              in page)
        check("for as long as the jump is wide on screen",
              "function playBrakeFor(" in page
              and "PLAY_BRAKE_MIN_MS + (px / diag) * PLAY_BRAKE_SPAN_MS" in page)
        # Measured in pixels, because the same jump is nothing at the overview
        # zoom and most of a screen close in -- which is where it was reported.
        brake_fn = page[page.index("function playBrakeFor("):
                        page.index("const play = {")]
        check("measured against the screen, not against the ground",
              "map.latLngToContainerPoint(toLatLng(e.from))" in brake_fn
              and "const diag = Math.hypot(sz.x, sz.y) || 1;" in brake_fn)
        # The way out of a mode that has taken the whole map over.
        stop = bare[bare.index(".play-btn.on {"):]
        stop = stop[:stop.index("}")]
        check("the button that stops the playback is red",
              "#ec4444" in stop)
        check("a teleport plays as going from one place to the other",
              "function playTravel(" in page
              and "const PLAY_TRAVEL_MS = 420;" in page
              and "const wait = departs ? PLAY_TRAVEL_MS : 0;" in page
              and "if (wait) setTimeout(() => { playLand(lasting);" in page)
        check("and a respawn is an event of its own",
              "kind: 'respawn'" in page and "state.respawnList" in
              page[page.index("function playEvents("):
                   page.index("async function buildPlayback(")])

        # Map options that are each one word long and each fix a thing that
        # was reported as a drawing bug. Nothing else in the viewer reads
        # them, so removing one breaks the map quietly and only on a gesture.
        opts = page[page.index("map = L.map('map', {"):]
        opts = opts[:opts.index("});")]
        check("the map does not glide after a flick",
              "inertia: false" in opts)
        check("zooming redraws the path instead of stretching it",
              "zoomAnimation: false" in opts)
        # One wheel notch is 1.25 zoom levels, so every notch swaps the tile
        # level. Leaflet fades each new tile in over 200 ms, which buys
        # nothing for a local file and shows the blurry coarse layer through
        # the half-transparent tiles until the next notch interrupts it.
        check("and the tiles arrive rather than fading in",
              "fadeAnimation: false" in opts)
        # Where a mark sits and how it clusters are both in map pixels, so a
        # zoom cannot change either -- but the reload asked all three mark
        # endpoints again and rebuilt every marker element on the map, on
        # every notch of every scroll. Measured on routes.db: three notches
        # made four requests and replaced all 264 marker elements; now one
        # request and none, the same DOM nodes before and after.
        zoomed = page[page.index("map.on('zoomend'"):]
        zoomed = zoomed[:zoomed.index("});")]
        check("a zoom asks for the path again and leaves the marks alone",
              "scheduleReload(200, false)" in zoomed
              and "function scheduleReload(delay = 250, marks = true)" in page
              and "if (!marks) return;" in page)
        # And the queue is shared: a filter change and then a zoom are two
        # calls and one timer, and the zoom must not drop the marks the
        # filter change asked for.
        sched = page[page.index("function scheduleReload("):]
        sched = sched[:sched.index("\nasync function reload(")]
        check("a zoom landing on a queued filter change keeps its marks",
              "state.reloadMarks = state.reloadMarks || marks;" in sched)
        # A grace you keep warping back to is a grace you keep getting up
        # at, so the teleport mark and the respawn mark land on the same
        # pixel and one is simply behind the other. Reported as exactly that.
        # Warping out of a grace you also warp into is the same collision
        # between two marks of one kind, and it had never been noticed.
        #
        # They cannot be merged into one mark: each kind has its own switch
        # and has to come off the map without taking the others with it. So
        # they step aside, evenly around the point they share, and a mark
        # with the spot to itself does not move at all.
        #
        # Measured on routes.db: 329 marks on 286 spots, of which 40 are
        # shared -- 19 warp-out over warp-in, 15 respawn under a teleport,
        # three of them three deep. Before, 23 pairs on screen sat exactly
        # on top of each other; after, the closest two icons are 18.7 px
        # apart, which is one icon's width.
        # They were nudged apart first, which worked and read as two places
        # rather than as one place with two things to say. Asked for instead:
        # "a split symbol, with one symbol on each side. Hovering over it
        # should show the dashed line for both the teleport and death." So a
        # shared spot is one marker, a cell per kind, sitting exactly on the
        # point -- which is the thing a nudge could not do.
        check("marks that would stand on each other are one mark",
              "function drawWorldMarks(" in page
              and "function addSplitMark(spot, group, pane) {" in page
              # And the world hands its clusters to it. Asserting the
              # register exists is not the same as asserting it is
              # used: drawing every mark singly left this green.
              and "placeMarks(wanted.map(worldCell), deathGroup, 'deaths');"
                  in page
              and "if (cells.length === 1) addSingleMark(cells[0]);"
                  in page
              and "else addSplitMark({ xy: sp.xy, cells }, splitGroup, splitPane);"
                  in page
              and "className: 'split-mark'," in page
              # The machinery it replaces, gone rather than left unused.
              # Named exactly, because the word survives in the comment that
              # explains why it went.
              and "slotAnchor(" not in page and "slotAt(" not in page
              and ".split-mark .half {" in sheet)
        # And it is a disc the size of a single mark. A cell used to be as
        # wide as the glyph and its count side by side, which made a pair 40
        # to 52 px and a triple 72 -- a lozenge lying across the terrain
        # beside marks a third of its size. Reported: "the split markers are
        # way too wide, they could probably be a circle split down the
        # middle, with the respective numbers in their corners."
        #
        # Two cells of half the height wide are a square box with a radius
        # bigger than either side, which is a circle; the counts move out to
        # the corners the single marks already put them in. Measured on
        # routes.db: every split 24 px, against 40 to 72, and 30 px at its
        # widest with both counts hanging off it, against 26 for a single
        # mark carrying one.
        check("and it is a disc cut down the middle, not a pill",
              "const SPLIT_CELL = 12;" in page
              and "const width = SPLIT_CELL * cells.length;" in page
              and "iconSize: [width, SPLIT_CELL * 2]," in page
              and "iconAnchor: [width / 2, SPLIT_CELL]," in page
              # One place says how wide a cell is. The other half of the
              # shape is the radius, which is larger than either side of a
              # cell and so is clamped to exactly half of it.
              and 'style="flex:0 0 ${SPLIT_CELL}px"' in page
              and "border-radius: 999px 0 0 999px;" in sheet)
        # Both ends of a jump are one thing at one place. A grace you warp
        # away from is usually one you warp back to, so the pair landed on a
        # spot constantly and was drawn as two cells saying the same word
        # twice -- and with a respawn there as well that made a three-cell
        # pill where a disc would do. Reported: "if a split marker combines
        # three different markers, the teleport from, the teleport to, and
        # the respawn, then make both teleport markers into one, since you
        # can see where they go to and from by hovering."
        #
        # Wherever they meet, not only at three deep: the hover draws every
        # jump at the spot in both directions either way, and the popup keeps
        # the two sections it always had.
        #
        # Measured on routes.db: the world's splits go 33 to 19 and every one
        # of them is a pair, with the 14 that were nothing but a jump's two
        # ends becoming single marks; the castles go 9 splits to 7 and their
        # three-cell one disappears; the caves keep one three-cell, which is
        # a teleport, a death and a grace -- three things rather than one
        # thing twice. And the busiest teleport now wears the number the
        # panel gives it, 6, where it used to read 4 beside 2.
        check("and a jump's two ends are one cell, not two",
              "function mergeFamilies(cells) {" in page
              and "const cells = mergeFamilies(sp.cells);" in page
              and "family: m.kind === 'warp' ? 'warp' : null," in page
              and "xy: c.xy, n: c.list.length, family: 'warp'," in page
              # The count is every end at the spot; the face is the first
              # gathered, which is the arrival where there is one.
              and "into.n += c.n;" in page
              and "const into = c.family && out.find((o) => o.family === "
                  "c.family);" in page
              # What each end was is still in the popup, joined the way a
              # split joins its sections -- one function for both.
              and "function popupBox(parts) {" in page
              and "for (const c of out) if (c.parts) c.popup = popupBox(c.parts);"
                  in page
              and "mark.bindPopup(popupBox(cells.map((c) => c.popup)));" in page)
        check("and its counts are in the corners, not beside the glyphs",
              '<i class="count">${c.n}</i>' in page
              and ".split-mark .half .count {" in sheet
              and ".split-mark .half:first-child .count { left: -4px; }"
                  in sheet
              and ".split-mark .half:last-child .count { right: -4px; }"
                  in sheet
              # The inline number that made it wide, gone rather than left
              # to be styled by nothing.
              and ".split-mark .half b {" not in sheet)
        # And a spot is a smaller thing than a cluster. Two marks twelve
        # pixels apart are two 24 px discs overlapping by half -- you can
        # see there are two of them, and calling that one place said they
        # were together when they were only near. Reported as "it should
        # only split if they are directly on top of each other".
        #
        # Measured over every cross-kind pair on routes.db: 21 at 0.15 px or
        # less, then 1.39, 2.1, 2.5, 2.7, 2.8, 2.9, 3.2, 3.9, 4.0, 5.0, 5.4,
        # 5.7, 7.2 and up with no band to cut at, so the number comes off the
        # mark rather than the data: a quarter of the disc. 40 splits before,
        # 33 after, and every one of them a pair -- three deep needs eight
        # pixels of tolerance and there is none.
        check("and they only share one when they are on top of each other",
              "const SPLIT_PX = 6;" in page
              and "Math.abs(sp.xy[0] - c.xy[0]) < SPLIT_PX" in page
              and "Math.abs(sp.xy[1] - c.xy[1]) < SPLIT_PX);" in page
              # Still its own number for two marks of one kind, which is a
              # different question: that one is a count, not a hiding.
              and "const CLUSTER_PX = 12;" in page
              and "px || CLUSTER_PX" in page)
        # And it answers with everything it stands for. The lines are
        # gathered and drawn in one call per kind, because showDeathLines()
        # clears the last set first -- a death cell and a respawn cell each
        # calling it would leave only the second.
        check("and it draws every line it has to draw",
              "function markLines(m) {" in page
              and "function wireMarkHover(mark, cells) {" in page
              and "if (deaths.length) showDeathLines(deaths, into, lineRenderer);"
                  in page
              and "if (warps.length) showWarpLines(warps, into, lineRenderer);"
                  in page
              and "wireMarkHover(mark, cells);" in page
              and "wireMarkHover(mark, [c]);" in page)
        # And pressing one keeps them. Asked for: "clicking on a marker
        # should make the path from one marker to another persist on screen
        # even when panning." Hung on the popup rather than on the click,
        # because the popup is already what says which mark you are looking
        # at: Leaflet opens it on the click, closes it on a map click or on
        # Escape, swaps it when you press another mark and toggles it when
        # you press the same one -- so the lines follow all four without a
        # second set of rules to keep in step.
        #
        # While one is pinned a hover does nothing, or a cursor crossing the
        # map on its way somewhere -- which is what a drag is -- would take
        # it down or swap it. The dungeon overlay has had that rule since it
        # was written.
        redraw_clears = (chr(10) + "  lines.pinned = false;"
                         + chr(10) + "  hideDeathLines();"
                         + chr(10) + "  hideWarpLines();")
        pin_after_draw = ("    lines.pinned = false;" + chr(10)
                          + "    draw();" + chr(10)
                          + "    lines.pinned = true;")
        check("and pressing a mark keeps them up while you pan",
              "const lines = { pinned: false };" in page
              and "mark.on('mouseover', () => { if (!lines.pinned) draw(); });"
                  in page
              and "if (!lines.pinned) { hideDeathLines(); hideWarpLines(); }"
                  in page
              and "mark.on('popupopen', () => {" in page
              and "mark.on('popupclose', () => {" in page
              # Drawn afresh and then pinned, so pressing one mark while
              # another is pinned replaces rather than being refused.
              and pin_after_draw in page
              # A mark inside a cave is drawn on a drawing that only exists
              # while you point at it; pinning the lines without pinning that
              # would take both away the moment you panned.
              and "if (into === insideGroup) inside.pinned = true;" in page
              # And a rebuild of the marks drops it: the mark it was pinned
              # to may not be among the new ones.
              and redraw_clears in page)
        # And a dungeon's own marks go through the same register. They are
        # drawn by nobody but drawInteriorInto(), so they went on stacking
        # after the world's were brought together -- reported as "the split
        # markers don't seem to work in legacy dungeons at least". Measured
        # on routes.db: 99 marks on the four castles, of which 93 pairs are
        # within six pixels of each other, 57 of one kind and 36 of two.
        # After: 76 markers, 5 of them split, and across the 37 caves with
        # anything in them 305 marks come to 156, 8 of them split.
        check("and so do the marks inside a dungeon",
              "placeMarks(cells, group, markPane);" in page
              and "const home = { group, pane: markPane, lineGroup: group,"
                  " lineRenderer };" in page
              # Through the same two rules: one kind at one point is a count,
              # two kinds at one point is a split.
              and "for (const c of clusterMarks(acc.fallen, (x) => x.pt)) {"
                  in page
              and "for (const c of clusterMarks(acc.got, (x) => x.pt)) {"
                  in page
              and "for (const c of clusterMarks(mine, (e) => e.pt)) {" in page
              # And they say what they stand for when there is more than one
              # of them, which a mark per death never had to.
              and "function insideDeathPopup(list, v) {" in page
              and "function insideWarpPopup(list, which, v) {" in page
              and "`Died ${n} times here`" in page
              and "`Arrived here ${n} times`" in page)
        # One function rather than one per loader, because the answer depends
        # on all three lists at once -- whichever fetch came back last, the
        # picture has to be the same. It costs one pass of 9.1 ms, twice on a
        # reload, against a reload that is already a fetch and a full redraw
        # of the route.
        check("and every kind of mark is drawn knowing about the others",
              "const wanted = [];" in page
              and "deathGroup.clearLayers();\n  respawnGroup.clearLayers();"
                  "\n  warpGroup.clearLayers();" in page
              # Both loaders end in it and nothing else calls it: the two
              # fetches are the only things that can change the answer.
              and page.count("  drawWorldMarks();") == 2)
        # One rule for what counts as one spot, because the register is only
        # sound while every kind agrees. It was the same loop written out
        # four times.
        check("and what counts as one spot is decided in one place",
              "const CLUSTER_PX = 12;" in page
              and "function clusterMarks(list, xyOf, px) {" in page
              # One loop, and a radius it is told: a dungeon's own
              # metres are not map pixels, and the busiest-spot rows ask
              # the same question in that space.
              and "const near_px = px || CLUSTER_PX;" in page
              and page.count("Math.abs(c.xy[0] - p[0]) < near_px") == 1)

        # The teleport marks used to be taken off the map before the request
        # for them was sent, so they were gone for as long as it took -- the
        # same flicker the route was fixed for, on the same gesture.
        warps_js = page[page.index("async function loadWarps("):]
        warps_js = warps_js[:warps_js.index("\nfunction warpEnds(")]
        check("the teleport marks stay up while they are being refetched",
              "fetch('/api/warps')" in warps_js
              and "\n  warpGroup.clearLayers();" in warps_js
              and warps_js.index("fetch('/api/warps')")
                  < warps_js.index("\n  warpGroup.clearLayers();"))
        # Two CSS facts no Python test would otherwise reach. The playback
        # section's own padding was written as `#playback-box`, which loses to
        # `#panel > section` -- an id and an element beat an id -- so a
        # section with no heading in it kept the heading rhythm and the button
        # sat 22px from the top and 5px from the bottom of it.
        check("the playback button is centred in its own section",
              "#panel > section#playback-box {" in sheet
              and "#playback-box { padding-top: 0; border-top: 0; }" not in sheet)
        # And the death marks on the timeline need a boundary rather than more
        # light: legible on the unplayed grey, lost on the amber the played
        # part is painted in. A dark ring, no blur, so the mark is three
        # pixels wide and one of them is red -- where two of the 137 marks are
        # close enough for their rings to meet, what merges is the dark.
        ticks = sheet[sheet.index("#play-ticks i {"):]
        ticks = ticks[:ticks.index("}")]
        check("and a death on the timeline carries a dark ring, not a glow",
              "box-shadow: 0 0 0 1px rgba(6, 16, 15, 0.85);" in ticks
              and "background: #ff6a4d;" in ticks)

        # A death inside a cave is placed at that cave's entrance for want of
        # anywhere better, and the pin already standing there counts its
        # deaths in a badge. Drawing both is the same fact twice, a few pixels
        # apart, with the cluster on the spot the deaths did not happen.
        # Walking back into a cave shows the cave, marks included. The paths
        # of earlier visits have been redrawn on the way in for a while; the
        # marks were not, so a cave you had died in twelve times started its
        # tally again at one every time you went back through the door.
        # Measured on routes.db, map 520749056: 12 deaths by the end of the
        # 20:58 visit, 12 still showing on re-entering at 21:27, and 21 by the
        # end of that visit -- which is what the database holds.
        check("going back into a cave carries its tally with it",
              "function playSamePlace(" in page
              and "fa.map_id === fb.map_id" in page
              and "const arrives = home || playSamePlace(play.where, e.where);"
              in page
              and "if (!arrives) return;" in page)
        ent2 = page[page.index("function playEnter("):
                    page.index("function playLine(")]
        check("and the marks of earlier visits are put back with their paths",
              "for (let i = 0; i < play.event; i++) {" in ent2
              and "playFlash(e, false);" in ent2)
        # The event loop steps its cursor before flashing, so without this the
        # mark about to be made would be replayed here as well and counted
        # twice.
        check("without counting the one that is about to be made",
              "if (i === skip) continue;" in ent2
              and "playEnter(e.where, play.event - 1);" in page)
        check("a mark a dungeon is already showing is not drawn twice",
              "function hiddenInside(" in page
              and page.count("hiddenInside(") >= 3)
        # A mark lives in two places -- its own layer group, and drawn onto a
        # legacy dungeon's permanent path -- so a checkbox has to reach both.
        # It reached one: unticking Deaths cleared the 61 on the world and
        # left 20 on the castles, and Teleports left 8, measured on
        # routes.db. Two halves to it, and both are needed: the drawing has
        # to ask, and something has to redraw it.
        into = page[page.index("function drawInteriorInto("):
                    page.index("function drawInside(")]
        check("a dungeon's own marks obey the marker checkboxes",
              "state.warps ? warpsInside(v) : []" in into
              and "state.deaths ? deathsInside(v) : []" in into
              and "if (state.respawns) {" in into)
        check("and a checkbox redraws the dungeons, not just its own layer",
              "const redrawMarks = (fetchList) => fetchList().then(() => loadInteriors());"
              in page
              and page.count("redrawMarks(loadDeaths)") == 2
              and page.count("redrawMarks(loadWarps)") == 1)
        # state.warpList is not the teleport layer's alone: the dungeon
        # drawings read it. Returning before the fetch left it holding
        # whatever it held before the session filter or the window changed.
        warps_fn = page[page.index("async function loadWarps("):
                        page.index("\nfunction warpEnds(")]
        # The layer gate moved into warpEnds(), so loadWarps() has no early
        # return left to put the assignment in front of: it fetches, it
        # stores, it draws. Asked as "and nothing returns before the store",
        # which is the property the ordering was standing in for -- and asked
        # with `in` before any slicing, because `.index()` raises rather than
        # failing and takes the whole run down with it, which is exactly what
        # it did when the gate moved.
        check("and the list behind them is kept fresh either way",
              "state.warpList = data.warps.filter(" in warps_fn
              and "if (!state.warps) return;" not in warps_fn
              and "if (!state.warps) return null;" in page
              and warps_fn.index("fetch('/api/warps')")
                  < warps_fn.index("state.warpList = data.warps.filter("))
        # The pins are built from the answer already in hand; the frame
        # learning that follows fetches a path per dungeon, and doing that
        # first left the map with no pins on it for the length of two dozen
        # round trips.
        marks_at = page.index("mark.addTo(markerGroup);")
        # Boot asked for everything twice. reload() fetches the marks as
        # well, so the three calls after it were a second round -- and a
        # third for the deaths, which drawWorldVisible() reclusters -- while
        # wiring the time slider queued a full reload 250 ms in on top.
        # Invisible at a tenth of a second a call; measured on a database ten
        # times the size of routes.db, boot spent 55 s in requests and now
        # spends 2.1, settling in 2.6 s instead of 8.
        boot_js = page[page.index("async function boot()"):
                       page.index("function selectPlane(")]
        check("boot fetches the route once and the marks once",
              "await reload({ marks: false });" in boot_js
              and boot_js.count("await loadDeaths();") == 1
              and boot_js.count("await loadWarps();") == 1
              and boot_js.count("await loadInteriors();") == 1)
        # And the time slider's own set-up does not count as a change to it.
        check("and wiring the time window does not reload the map",
              "function applyRange(first) {" in page
              and "if (!first) scheduleReload();" in page
              and "applyRange(true);" in page
              and "() => applyRange());" in page)

        # Stopping the recorder with the map open is the failure this page
        # meets most often, and it said nothing at all: the zoom threw
        # `Failed to fetch` into the void, the status line still read Live,
        # and a checkbox just unticked stayed drawn because the loader threw
        # before it cleared anything. Measured after: one banner naming the
        # command that brings it back, the status reading "Not answering",
        # and no rejection escaping.
        check("a recorder that stops answering says so, once",
              "function lostRecorder(" in page
              and "if (lostShown) return;" in page
              and "'Not answering'" in page)
        check("and no failed fetch is left to die in the console",
              "window.addEventListener('unhandledrejection'" in page
              and "why.includes('Failed to fetch')" in page
              and "e.preventDefault();" in page)

        # A viewer serving a database answers the websocket like a recorder
        # -- it is the same server class -- so the socket opened, the page
        # said Live, and nothing ever arrived on it. `meta.recording` is the
        # session being written, or null, which is the question being asked.
        live = page[page.index("function connectLive("):
                    page.index("function setYouMark(")]
        check("a viewer with no recorder behind it does not claim to be live",
              "if (!state.meta || !state.meta.recording) {" in live
              and "'Offline map'" in live
              and live.index("state.meta.recording")
                  < live.index("new WebSocket"))
        check("the cave pins go up before anything else is fetched",
              marks_at < page.index("await learnDungeonFrame("))
        for asset in ("app.js", "style.css"):
            status, _ = await get_text("/" + asset)
            check(f"{asset} served", status == 200)

        # index.html declares the controls app.js wires, so a browser holding
        # one of the two and not the other means every lookup returns null:
        # wireControls() threw on the first missing checkbox and boot() never
        # reached reload(), leaving a blank map with dead buttons. Two guards,
        # because either alone leaves a way to get there.
        page_html = (ROOT / "viewer" / "index.html").read_text(encoding="utf-8")
        stamp = re.search(r'name="page-build" content="(\d+)"', page_html)
        wanted = re.search(r"const PAGE_BUILD = (\d+);", page)
        check("the page and its script are stamped with the same build",
              stamp and wanted and stamp.group(1) == wanted.group(1),
              f"index.html {stamp.group(1) if stamp else '?'}, "
              f"app.js {wanted.group(1) if wanted else '?'}")
        for path in ("/", "/app.js", "/style.css"):
            headers = await asyncio.to_thread(_headers, path)
            check(f"{path} is never cached",
                  "no-store" in (headers.get("Cache-Control") or ""),
                  headers.get("Cache-Control") or "no directive at all")
        # And a missing control has to be survivable, not fatal: the guard is
        # what keeps one stale checkbox from taking the route down with it.
        # Dimming has to reach every pane a mark can be drawn into, and the
        # list of them lives away from the panes themselves -- `respawns` was
        # created after it was written and never added, so hovering a cave
        # dimmed the world and left 35 respawn marks standing over it at full
        # strength. And a legacy dungeon's own marks are drawn on the map for
        # good, so they dim with the world; the ones belonging to the dungeon
        # being hovered are the thing being revealed. That was decided by
        # comparing the layer group against `placedGroup` until each dungeon
        # was given a group of its own inside it, at which point the
        # comparison was false for all of them and 51 more stayed bright.
        check("dimming reaches the respawn marks as well",
              "'deaths', 'respawns'," in page)
        check("and a permanent drawing's marks go in a pane that dims",
              "opts && opts.permanent ? 'deaths' : 'insideMarks'" in page
              and "permanent: true," in page,
              "still decided from the layer group"
              if "group === placedGroup ? 'deaths'" in page else "")

        # An earlier run through the same cave is the same walking, so there
        # is nothing to rank one above the other: drawing them at 0.4 read as
        # the place fading out every time you stepped back into it.
        check("going back into a cave does not dim the run before",
              "playLine(run, run.xy.length, false);" in page
              and "drawn faintly" not in page)

        # The speed reads in the units you would say out loud again, with the
        # multiplier moved to the hover.
        check("the playback speed says how much route goes past in a second",
              "min a second`" in page and "real time`" in page)

        # And how long is left counts the easing it is going to do. Measured
        # at an hour a second, where the whole route is 15.7 s of clock, the
        # brakes add 28.4 s at zoom 3 -- so the clock alone read low the whole
        # way down and then refused to reach zero.
        check("the time left counts the teleport easing, not just the clock",
              "function playTimeLeft(" in page
              and "PLAY_BRAKE_RATE)" in page
              and "playLeftText(playTimeLeft(elapsed))" in page)
        check("and it sits beside the play button, in minutes and seconds",
              'id="play-left"' in page_html and "min ${r} second" in page,
              "no slot for it in the page"
              if 'id="play-left"' not in page_html else "")

        # Both drawing complaints are the same thing underneath: a tile the
        # browser has not got yet. With no directive at all the tiles are
        # cached heuristically and revalidated, so every page load starts
        # cold; and the first visit to a zoom level had nothing to show but
        # the coarse layer for 45 to 61 ms.
        check("the tiles say they may be kept",
              "public, max-age=" in srv and "TILE_CACHE" in srv)
        check("and the level either side is fetched while nothing happens",
              "function warmNeighbours(" in page
              and "map.on('moveend zoomend', warmNeighbours)" in page)
        # The coarse layer is the net under all of that: where the detail
        # tiles are not in yet, it shows a blurry version of the right place
        # rather than the container behind the map. It was loaded like any
        # other tile layer, on demand around wherever you happened to be, so a
        # pan that outran it showed the background for a frame or two --
        # reported from the field as exactly that. The whole level is 25
        # images of a few kB on this map, so it is simply held.
        #
        # Measured over eight screen-wide jumps, 80 frames sampled: 0 with a
        # hole in the coarse layer, against a detail layer down to 32 of its
        # 45 tiles at its worst.
        # And under both of them, one image that is never rebuilt. Warming
        # the cache makes the coarse layer's tiles arrive sooner and cannot
        # make them always be there: Leaflet creates and prunes them as you
        # move, so the hole is in the bookkeeping, not the network -- measured
        # over nine zoom changes, there is a frame where 0 of the detail
        # layer's 16 tiles are up. An overlay has nothing to prune. The
        # pyramid's zoom 0 is a single tile with the map in its corner, so
        # this needs no new asset. Measured after: 72 frames of zooming and
        # 32 of panning, 0 without it.
        check("and one image under all of it that is never rebuilt",
              "function drawNet(" in page
              and "L.imageOverlay(" in page
              and "pane: 'tilePane', zIndex: -1" in page
              # The definition, the first draw, and the other plane's pyramid.
              and page.count("drawNet(") == 3)
        check("and the coarse layer under it is held whole",
              "function warmBase(" in page
              and "baseHeld = want.slice(0, 200)" in page
              # At boot, and again when the other plane's pyramid goes up.
              and page.count("warmBase();") == 2)

        # Reported from the field, and all one shape: something the playback
        # draws was being taken away again before it could be seen.
        #
        # The run being walked is drawn by the run loop and `playEnter()` then
        # clears the group it lives in and puts back only the *finished* runs.
        # While playing that costs a frame; on a seek `playDrawTo` runs once,
        # so scrubbing into a cave showed the position mark moving with no
        # path behind it until a finished run or an event happened to put one
        # there. Held and drawn after the place check now.
        check("the run being walked survives the place check",
              "if (n > 1 || tip) pending = { run, n, tip };" in page
              and "let pending = null;" in page
              and page.index("if (place !== play.where) playEnter(place);")
                  < page.index("if (pending) {"))

        # A seek is a reset followed by a replay of every death up to the
        # target, so the count goes 0 -> N on every frame of a drag. The
        # number still lands; the announcement belongs to the moment.
        check("the death toll flashes for a death, not for a scrub",
              "function playToll(n, animate)" in page
              and "if (rose && animate && box.classList)" in page
              and "playToll(died, animate);" in page)

        # state.warpList is not the teleport layer's: the dungeon drawings
        # read it and so does the playback, which walks both planes. Filtered
        # by plane on the way in, every jump made underground was missing
        # from the playback -- two of them on routes.db.
        check("the warp list keeps both planes, and the drawing filters",
              "(w) => !state.range || (w.ts >= state.range[0]" in page
              and "!w.inside && markPlane(w)" in page)

        # A dungeon can have more than one mouth, and the pin stands at the
        # one the route agrees on -- so a cave walked in at one end and out of
        # the other had nothing on the map where you came out.
        check("every mouth of a dungeon gets a pin",
              "function otherMouths(" in page
              and "for (const mouth of otherMouths(group, doorway, routeDoor, "
                  "shift))" in page)
        # Wearing the dungeon's own pin. It was smaller, dashed and faded, on
        # the reasoning that a door is the lesser of the two things -- and
        # drawn that way it reads as a different kind of place rather than as
        # the same one seen from its other side. Reported: "make the exit icon
        # look the same as the normal one."
        mouth_css = ""
        if ".cave-mark.other-mouth" in sheet:
            mouth_css = sheet[sheet.index(".cave-mark.other-mouth"):][:400]
        check("and it is the same pin, not a lesser one",
              "iconSize: [size, size]," in page[page.index(
                  "for (const mouth of otherMouths("):][:900]
              and "border-style: dashed" not in mouth_css
              and "MOUTH_PX" not in page)
        # And it moves with a drag, because the drag moves the place. The
        # drawing has been rigid under one since the round that made it so;
        # the pins beside it should not be the one thing left behind.
        check("and a drag takes the other mouths with it",
              "const shift = (group[0].door_xy && group[0].xy)" in page
              and "for (const m of out) m.xy = [m.xy[0] + move[0], "
                  "m.xy[1] + move[1]];" in page)

        # Room to zoom past the pyramid: what you are looking at that close is
        # the path, which is drawn from the route and stays sharp.
        check("the map zooms in past the finest tiles",
              "maxZoom: state.nativeZoom + 4," in page)

        # A teleport knows where it is going before the map is sent there.
        check("a jump pays for its tiles while the line is still travelling",
              "function warmAhead(" in page and "warmAhead(e.xy);" in page)

        # A compass that is not in the corner is a compass somebody moved.
        # The supplied one is 219x256, so `contain` letterboxes it inside a
        # square box: without an object-position the artwork sat 11 px further
        # in than the box, which is 57 px of gap on the right against 14 on
        # the top.
        check("the compass sits in the corner it is drawn for",
              "right: 14px;" in sheet[sheet.index("#compass {"):]
              and "object-position: top right;" in sheet)

        # AGE_DEPTHS runs widest first, so the index and the slider disagree
        # about which way is more -- and the slider is the one you look at.
        check("the age slider reaches further back as it goes right",
              "AGE_DEPTHS.length - 1 - +el.value" in page
              and 'id="age-depth" min="0" max="9" step="1" value="9"' in markup)
        # The index keeps its meaning, so a setting saved before this still
        # names the same window.
        check("and what is remembered is the window, not the thumb",
              "localStorage.setItem(`route.${key}`, state[key])" in page)

        # Where the map was pointed, kept across restarts. Coming back to the
        # whole of the Lands Between when you were three zooms deep on one
        # cave is a small chore repeated every session.
        check("the map comes back to where you were looking",
              "function saveView()" in page and "function savedView()" in page
              and "const back = savedView();" in page
              and "else map.fitBounds(dataBounds || imageBounds" in page
              and "map.on('moveend zoomend', saveView);" in page)
        # The playback sweeps the map from one end of the route to the other,
        # and where it happened to stop is not where you were looking.
        check("and the playback does not get to decide what that was",
              "if (play && play.on) return;" in
              page[page.index("function saveView()"):
                   page.index("function savedView()")])
        # A map image of a different size, or a zoom ladder that has changed,
        # would otherwise put you off the edge of the world at a zoom the map
        # will not hold. Checked against the same limits a drag is.
        check("and a remembered view that no longer means anything is dropped",
              all(x in page[page.index("function savedView()"):
                            page.index("// A checkbox that remembers")]
                  for x in ("Number.isFinite", "map.getMinZoom()",
                            "map.getMaxZoom()", "state.panLimits")))

        # Two grips on the timeline saying where the playback starts and
        # ends. The track underneath stays a picture of everything recorded,
        # so what is cut off reads as something you chose to leave out.
        check("the timeline has a grip at each end of the playback",
              'id="trim-a"' in markup and 'id="trim-b"' in markup
              and 'id="trim-cut-a"' in markup and 'id="trim-cut-b"' in markup
              and "#play-trim .grip" in sheet
              and "#play-trim.whole .grip" in sheet)
        # Run it again at the end grip rather than stopping. A mode, so it
        # stays lit while it is on and says which way it is set rather than
        # what pressing it would do -- and remembered, because it is a way of
        # watching rather than a thing you do once.
        check("the playback can be told to run it again",
              'id="play-repeat"' in markup
              and 'aria-pressed' in markup
              and "if (play.loop) playSeek(play.dir < 0 ? play.until : play.from);"
                  in page
              and "#play-repeat.on" in sheet)
        check("and which way it is set is remembered",
              "play.loop = pref('loop') === 'true';" in page
              and "savePref('loop', play.loop);" in page)
        # Stopping is still what happens when it is off, or the button would
        # be a decoration.
        # `in` before `index`, or a check that has lost its string raises and
        # takes the whole run with it instead of reporting one red line.
        loop_line = ("if (play.loop) playSeek("
                     "play.dir < 0 ? play.until : play.from);")
        after = (page[page.index(loop_line):][:300]
                 if loop_line in page else "")
        check("and it still stops at the end when it is not",
              "else {" in after and "playPause();" in after)

        # Backwards. The drawing only ever grows forwards, so a step back is
        # a seek -- which is why this is a direction the loop reads rather
        # than a second drawing routine, and why it waits out what the last
        # rebuild cost instead of pinning the thread.
        check("the playback can be run backwards",
              'id="play-rev"' in markup
              and "play.dir = -play.dir;" in page
              and "if (wall - play.last0 < play.seekMs) return;" in page
              and "#play-rev.on" in sheet)
        # The deaths on the track are most of what the bar is for -- until
        # there are a hundred and forty of them over one evening.
        check("and the deaths on the timeline can be put away",
              'id="play-ticks-on"' in markup
              and 'class="skull-btn"' in markup
              and "box.hidden = !play.ticksOn;" in page
              and ".skull-btn" in sheet
              # One skull and no word: red on the track, pale off it.
              and ".skull-btn.off { color: var(--text); }" in sheet
              and "color: #ff6a4d;" in
                  sheet[sheet.index(".skull-btn {"):sheet.index(".skull-btn:hover")])
        check("and both of them are remembered",
              "pref('dir') === '-1'" in page and "savePref('dir', play.dir);" in page
              and "pref('ticks') !== 'false'" in page
              and "savePref('ticks', play.ticksOn);" in page)

        # A flash is a length of wall clock, and what it costs is route. At
        # five minutes a second, holding the drawing for one is seven minutes
        # of route and reads as intended; at an hour a second it is an hour
        # and a half, on a playback that is fifteen seconds long from end to
        # end -- so the dot is out in the open and the world is still dark for
        # the cave it left.
        check("a flash does not hold the drawing a tenth of the route back",
              "PLAY_HOLD_SPEED / (play.speed || 1)" in page
              and "const PLAY_HOLD_SPEED = 300;" in page)

        # Nothing here scrolls the page: the map fills the window and the
        # panel scrolls inside itself. Two pixels of a grip at the far end of
        # the timeline were enough to put a scrollbar across the whole window.
        check("the page itself never scrolls",
              "overflow: hidden;" in
                  sheet[sheet.index("html, body {"):sheet.index("#map {")]
              and "overflow: hidden;" in
                  sheet[sheet.index("#play-trim {"):sheet.index("#play-trim .cut")])
        # And how long is left sits against the button it belongs to rather
        # than against the edge of the map.
        check("the reading beside play is next to it, not a flank away",
              "text-align: left;" in
                  sheet[sheet.index(".play-left {"):sheet.index("#play-speed {")])

        # A frame pins one point inside a dungeon to one point on the map.
        # Which way the inside is *turned* takes a second door, and until
        # there is one the shape is right and its bearing is a guess -- right
        # at the door it is pinned to and further out the further in you go.
        # Raya Lucaria was 77 degrees and 133 m out at its second door for two
        # days, drawn as confidently as anything else and saying nothing.
        check("a drawing says whether it knows which way the place faces",
              "turned to line up with the two ways in" in page
              and "not known until you come in by a second door" in page
              and "the dungeon's own axes set the orientation" not in page)
        # And the three tests that ask "is this the same door?" agree, in the
        # same unit: the door vote, the second-mouth pins and the pair the
        # turn is measured from.
        # Named one by one rather than counted: a count says three lines
        # mention it, not that these three do.
        # The vote and the pins now go through sameMouth(), which asks inside
        # first and outside second; the turn pair still asks outside, which is
        # the space it measures its angle in.
        check("one doorway is one doorway to all three tests that ask",
              "metresApart(k.xy, door) < SAME_DOOR_M" in page       # the turn pair
              and "const mouth = list.filter((b) => sameMouth(a, b));" in page
              and "const near = out.find((m) => sameMouth(m, c));" in page
              and "Math.hypot(k.xy[0] - v.xy[0]" not in page)

        # A knob above the bar and a stem through it. The track, the death
        # ticks and the thumb all live in the middle of that box, so the knob
        # needed room at the top that a 40px track did not have -- and
        # everything that measures itself against the timeline's height had to
        # move with it.
        # An upright pull with two grooves across it rather than a circle:
        # the cut only ever moves along one axis, and a round knob says
        # "drag me anywhere". The grooves are hairlines, which is how the
        # rest of this page says things.
        check("the grip is a pull you can take hold of and a stem to sight by",
              "#play-trim .grip::before" in sheet
              and "#play-trim .grip::after" in sheet
              and "border-radius: 2.5px;" in
                  sheet[sheet.index("#play-trim .grip::before"):
                        sheet.index("#play-trim .grip::after")]
              and ".play-track { position: relative; height: 48px; }" in sheet)
        # One colour held in a custom property, for a reason that is not
        # tidiness: the hover used to restate `background` on each pseudo,
        # which is fine for a flat fill and wipes the grooves out of a
        # layered one.
        check("and lighting it up does not wipe the grooves out of it",
              "--grip: #a07d35;" in sheet          # muted amber, not the
              and "--grip: var(--route);" in sheet  # underground's steel blue
              and "var(--route-deep)" not in
                  sheet[sheet.index("#play-trim .grip {"):
                        sheet.index(".play-transport {")]
              and "#play-trim .grip:hover::before" not in sheet
              and "#play-trim.whole .grip::before" not in sheet)
        check("and the timeline's neighbours moved up with it",
              "body.playing #caption { bottom: 118px; }" in sheet
              and "body.playing #offmap { bottom: 118px; }" in sheet
              and "bottom: 186px;" in sheet)
        # Asked for: bigger buttons, reverse swapped with the left step, and
        # the deaths switch moved over beside repeat. Which makes the two
        # modes the ends of the group and the two steps the pair either side
        # of play -- and the flank arithmetic above is what keeps play on the
        # middle of the map through all of it.
        check("the transport is big enough to hit without looking down",
              width_of(".play-transport .step {") >= 34
              and width_of("#play-toggle {") >= 46
              and width_of(".skull-btn {") >= 34)
        # `in` before `index`, always: index() raises where a check should
        # go red, and the first run of this took the whole selftest down with
        # a ValueError rather than reporting one line.
        want = ("rev", "back", "toggle", "fwd", "repeat", "ticks-on", "died")
        here = [f'id="play-{x}"' for x in want]
        order = [markup.index(k) for k in here if k in markup]
        check("with the modes at the ends and the steps beside play",
              len(order) == len(want) and order == sorted(order),
              " then ".join(want))

        # Asked for: the box at the bottom of the map naming the cave you are
        # in during playback. Hovering a dungeon is a question you asked and
        # the caption is the answer; the playback walks into one every few
        # seconds without being asked.
        enter = page[page.index("function playEnter("):
                     page.index("function playAgeAt(")]
        check("the playback names no dungeon at the bottom of the map",
              "setCaption(" not in enter
              # and the hover on the finished map still does
              and "turned to line up with the two ways in" in page)

        # Asked for: pressing Underground during playback swapped the terrain
        # and left the whole of Limgrave drawn over Siofra -- the committed
        # route is off the map while it plays, so a reload fills a group
        # nobody can see and the playback's own group is untouched.
        check("switching the map by hand during playback takes the path too",
              "function playRedrawPlane(plane) {" in page
              and "if (play.on) { playRedrawPlane(which); return; }" in page
              and "playRedrawPlane(plane);" in page)
        # And the marker switches are the finished map's: the playback puts
        # its marks down out of its own script, so unticking Deaths mid-play
        # cleared a group it is not drawing into.
        check("and the marker switches are off while it plays",
              "function playLockMarks(" in page
              and "playLockMarks(true);" in page
              and "playLockMarks(false);" in page
              and ".toggle:has(input:disabled)" in sheet)

        # Asked for: hovering a cave draws it, and then the drawing has to be
        # reachable -- leaving the pin lit a 140 ms fuse whatever the cursor
        # did next, so moving towards the thing that had just appeared was
        # what took it away.
        check("the drawing a hover puts up is a region you can move into",
              "function insideHover(" in page
              and "insideHover(e.latlng);" in page
              and "inside.box = insideBox(corners);" in page
              and "const INSIDE_REACH_PX = 30;" in page)
        # The fuse is nulled as it fires. Without that a cursor moving away
        # from the drawing restarted the timer on every mouse move and the
        # hide never happened at all.
        check("and moving away from it still puts it away",
              "else if (!inside.timer) hideInside(false);" in page
              and "inside.timer = null;" in
                  page[page.index("function hideInside("):
                       page.index("function insideHover(")])
        # Drawn twice: a dark line under the amber one, because a hairline of
        # amber over painted terrain reads as part of the map. The edge
        # margin's box is ringed for the same reason; this one is canvas, so
        # the ring is a second rectangle laid down first rather than a shadow.
        check("and the region is drawn, so you can see where it reaches",
              "const at = [toLatLng([x0, y0]), toLatLng([x1, y1])];" in page
              and "if (inside.box && !inside.pinned) {" in page
              and page.count("L.rectangle(at, {") == 2
              and "color: '#07100e', weight: 4," in page)
        # A cave's drawing only exists while you are looking at it, and
        # looking at it is the asking -- so its teleports say where they go
        # without a second hover. A legacy dungeon is on the map at all
        # times and a line standing between two of its rooms for ever is
        # one more thing nobody asked for.
        check("a teleport inside a cave says where it goes without asking",
              "const always = !(opts && opts.permanent);" in page
              and "if (always && pair[0] && pair[1]) {" in page
              # And on a castle it waits to be asked, which is the same
              # sentence said the other way round.
              and "warps: always ? []" in page)
        # And a death inside one says what it cost you. The world's death
        # marks have drawn a line to the grace since they were written; the
        # ones drawn inside a cave or a castle had no hover at all, which is
        # the same half-fix the teleport marks needed -- the line takes the
        # group and the renderer, because a mark inside a dungeon is in that
        # dungeon's own frame and only the caller is holding it. Measured on
        # routes.db: 32 of the 33 death marks on the castles draw a line and
        # all 33 clear it, the one that does not being the only death in the
        # database with no grace at all; and in one catacomb's overlay all
        # ten marks, five deaths and five graces, draw and clear.
        check("and a death inside one says what it cost you",
              "function showDeathLines(pairs, into, lineRenderer) {" in page
              and "deathHome = into || deathGroup;" in page
              # Carried by the cell, so a death sharing its spot with a
              # teleport still draws both.
              and ".map((x) => ({ from: c.xy, to: x.grace }))," in page
              and ".map((x) => ({ from: x.from, to: c.xy }))," in page)
        # Either end of a death line can be somewhere this frame cannot
        # reach. Out on the surface it is a recorded world position, which is
        # the same pixels once projected -- the lesson the teleport marks
        # already taught, where a line was called undrawable for want of a
        # point that was never made up. Inside *another* dungeon there is no
        # world position at all, and that dungeon's pin is the one thing on
        # the map standing for it, which is what warps() does server-side.
        # On routes.db, of 145 deaths inside a dungeon: 138 graces in the
        # same place, 2 out on the surface, 4 in another dungeon, 1 with none.
        check("wherever the other end of it is",
              "function markEndAt(map_id, local, xy, v, at) {" in page
              and "if (local && map_id === v.map_id) return at(local[0], "
                  "local[1]);" in page
              and "if (local && state.dungeonAt.has(map_id)) "
                  "return state.dungeonAt.get(map_id);" in page
              # The whole line, guard included: gating the fill out leaves
              # the name behind, and the negative test said so.
              and "    if (v.xy) state.dungeonAt.set(v.map_id, v.xy);" in page)

        # Asked for: the dashed box appears while the cursor is on the edge
        # margin slider and not a moment longer. A timer meant a flick past
        # the control left it standing and holding the cursor still on the
        # control made it go away -- both of them about a timer rather than
        # about where you are pointing.
        check("the edge margin box is up while the cursor is on the slider",
              "function hideFollowBox(" in page
              and "pad.addEventListener('pointerenter', showFollowBox);" in page
              and "if (!padHeld) hideFollowBox();" in page
              and "followBoxTimer" not in page)

        # Walking -- or dying -- out of one dungeon into another takes the
        # first one's drawing with it. Reported from the field: a Hero's Grave
        # under a legacy dungeon, a death in it, and the respawn puts you back
        # in the castle. A castle is world-visible, so refreshLiveInside()
        # takes the branch that redraws the permanent paths and returns, and
        # that branch has nothing to say about an overlay because it assumes
        # there is none -- so the world stayed dimmed and the grave stayed
        # drawn while the position mark walked around the castle. In the
        # recording: 15:41 into m35_00, dead at 15:43:20, back in m11_00
        # thirteen seconds later. Measured after: dimmed 0.18 and 7 layers in
        # the grave, undimmed and 0 the moment the castle is entered.
        check("leaving one dungeon for another takes its drawing with it",
              "hideInside(true);" in
              page[page.index("async function startLiveInside("):
                   page.index("function stopLiveInside(")]
              # And a forced hide happens now rather than on a zero-delay
              # timer, or the clearing lands after whatever was drawn next.
              and "if (force) { put(); return; }" in page
              and "inside.timer = setTimeout(put, 140);" in page)

        # Asked for: a way to look at the world without leaving the cave,
        # with moving again as the way back.
        check("you can look outside from inside a cave",
              'id="peek"' in markup
              and "function setPeek(" in page
              and "b.textContent = state.peek ? 'Back to the cave' "
                  ": 'Show outside';" in page
              and "if (state.peek) setPeek(false);" in page
              and "if (state.peek) return;" in page
              and "#peek {" in sheet)

        # Asked for: the window the corner button opens is for the shape of a
        # place. Four labelled numbers under a picture that answers three of
        # them better, and a list of every visit standing open above the
        # fold, were most of its height.
        check("the window that is nowhere shows the place, not a table",
              'id="inset-figures"' not in markup
              and "function insetFigures(" not in page
              and "#inset-figures" not in sheet)
        # And it fits what is in it. `box-sizing: border-box` is global, so
        # the canvas has to fit inside the width minus the border and the
        # padding -- two pixels over and the used value of overflow-x becomes
        # auto, and the global `scrollbar-color: var(--route)` paints that as
        # a bright amber bar across the bottom of the window on every window
        # size. Which is what the reported scrollbar turned out to be: the
        # horizontal one, not the vertical one I went looking for.
        def px(block, prop):
            r = sheet[sheet.index(block):]
            r = r[:r.index("}")]
            m = re.search(prop + r":\s*(\d+)px", r)
            return int(m.group(1)) if m else None
        canvas = px("#inset canvas {", "width")
        pad = re.search(r"padding: 14px (\d+)px",
                        sheet[sheet.index("#inset {"):])
        check("the window that is nowhere fits what is in it",
              canvas + int(pad.group(1)) * 2 + 2 == px("#inset {", "width")
              and "overflow: hidden auto;" in
                  sheet[sheet.index("#inset {"):sheet.index("#inset.from-corner")],
              f"canvas {canvas} + padding {int(pad.group(1)) * 2} + border 2 "
              f"vs width {px('#inset {', 'width')}")
        check("and the visits behind it are folded away, shut",
              "fold.className = 'fold';" in inset_off
              and "fold.append(...rows);" in inset_off
              and "fold.open" not in inset_off)

        # Every section in the panel folds, and the heading is the control:
        # there is nothing else in a section that means "this section".
        check("the panel's sections fold away",
              "function wireSections(" in page
              and "wireSections();" in page
              and "#panel > section.shut > *:not(h2) { display: none; }"
                  in sheet
              and "#panel > section > h2::before" in sheet)
        check("and which of them you left standing is remembered",
              "savePref(key, !sec.classList.contains('shut'));" in page
              and "if (pref(key) === 'false') sec.classList.add('shut');" in page
              and all(f'id="sec-{x}"' in markup for x in
                      ("map", "markers", "follow", "path", "time", "stats",
                       "sessions")))

        # Statistics: one row fewer, and how many sessions there were sits
        # with how long they ran rather than three rows below.
        # Sliced out of buildStats() rather than off the first `const rows`
        # in the file, which is insetOffMap's -- and asked with `in` before
        # `index`, because index() raises where a check should go red: the
        # first version of this took the whole run down with a ValueError
        # instead of reporting one line. That trap is already written up in
        # CLAUDE.md for the ordering checks; it applies to every check that
        # slices.
        stats_rows = page[page.index("async function buildStats("):]
        stats_rows = stats_rows[stats_rows.index("const rows = ["):]
        stats_rows = stats_rows[:stats_rows.index("];")]
        ordered = all(x in stats_rows for x in
                      ("'Longest session'", "'Sessions'", "'Deaths'"))
        check("the statistics drop the legacy count and group the sessions",
              "'Legacy dungeons'" not in stats_rows and ordered
              and stats_rows.index("'Longest session'")
                  < stats_rows.index("'Sessions'")
                  < stats_rows.index("'Deaths'"))

        # And two rows that are not the server's. Asked for: "a new stat
        # that's for the most used teleport, and most used respawn, and add a
        # button that takes you to where that is on the map."
        #
        # Counted off the same lists the map draws its marks from, and
        # clustered at the same radius, so the number in the panel and the
        # number on the badge are one fact. Which means they are rebuilt
        # whenever those lists change rather than once at boot, when neither
        # had arrived.
        world_marks = ""
        if "function drawWorldMarks() {" in page:
            world_marks = page[page.index("function drawWorldMarks() {"):]
            end = world_marks.find(chr(10) + "function ")
            world_marks = world_marks[:end] if end > 0 else world_marks
        check("the busiest teleport and grace are counted like the marks are",
              "function busiestWarp(" in page
              and "function busiestRespawn(" in page
              and "function bestSpot(items) {" in page
              and "['Most used teleport', `${jump.n}`," in page
              and "['Most used respawn', `${grace.n}`," in page
              and "function renderStats() {" in page
              # And called from drawWorldMarks, which is what runs when
              # a list changes -- asked of the function rather than of
              # the file, where buildStats() calls it too.
              and "renderStats();" in world_marks
              # Both ends of a jump: a grace you warp away from and come back
              # to is one teleport used twice. On routes.db the busiest spot
              # is 4 arrivals and 2 departures, and counting arrivals alone
              # would have called it a tie between six places used 4 times.
              and "ends.push({ at: w.local, map_id: w.map_id, which: 'to', w });"
                  in page
              and "ends.push({ at: w.from_local, map_id: w.from_map_id, "
                  "which: 'from', w });" in page
              # A place used once is not a place you used most.
              and "return best && best.n > 1 ? best : null;" in page)
        # In both coordinate spaces, because the answer lives in both: on
        # routes.db the busiest jump end is a grace in Limgrave with six ends
        # on it, and the busiest respawn is 31 deep inside a catacomb against
        # 7 for the busiest anywhere on the surface. Counting only what has a
        # world position would have named the wrong place.
        #
        # A dungeon's frame is a turn and a shift, never a stretch, so
        # CLUSTER_PX / scale asks the same question in local metres that the
        # drawing asks in pixels. Measured over all 31 dungeons with a
        # respawn in them: the panel's count and the drawn badge agree on
        # every one.
        check("and inside a dungeon as well as out on the map",
              "function localClusterPx() {" in page
              and "return CLUSTER_PX / (Math.abs(pr.scale_x) || 1);" in page
              and "keep(clusterMarks(list, (i) => i.at, localClusterPx()), "
                  "map_id);" in page
              and "if (r.layer === 'interior' && r.local) {" in page)
        # And the button goes there. A world spot needs only the view; one
        # inside a dungeon has no world position at all, so the place is
        # drawn first and the point read back through the frame it is drawn
        # in -- then the mark's own popup is opened, because a screen of
        # discs with no way to tell which one was meant is not an answer.
        check("and pressing the number takes you to it",
              "async function goToSpot(spot) {" in page
              and "map.setView(toLatLng(spot.at), zoom, { animate: false });"
                  in page
              and "await showInside(visits, true);" in page
              and "const at = tf.at(spot.at[0], spot.at[1]);" in page
              and "function openMarkAt(xy, groups) {" in page
              and "found.openPopup();" in page
              and ".stats .stat-go {" in sheet)
        # The marks of a drawing are gathered across every visit in it, not
        # per visit: a place you went back to is one place. A grace you got
        # up at 21 times over nine visits was nine marks on one pixel, each
        # counting its own visit -- the busiest showing 9 while the panel,
        # which counts the place, said 21. Measured on the four castles:
        # 76 markers per visit, 63 per place.
        check("and a place you went back to is one place, not one a visit",
              "function insideAccumulator() {" in page
              and "function placeInsideMarks(acc, v, group, markPane, "
                  "lineRenderer, always) {" in page
              and "const acc = (opts && opts.marks) || insideAccumulator();"
                  in page
              # Both drawings gather: the castles drawn in the open, one
              # accumulator per dungeon, and the overlay a hover puts up.
              and "placeInsideMarks(g.marks, g.v, g.layer, 'deaths', "
                  "renderer, false);" in page
              and "placeInsideMarks(marks, v, insideGroup, 'insideMarks', "
                  "insideRenderer, true);" in page
              # And a drawing of one visit still places its own.
              and "if (!(opts && opts.marks)) {" in page)

        # Sessions: show fewer at any point, not only once every one of them
        # is on screen -- and a row says how long you played rather than how
        # many rows the sampler happened to write.
        check("the session list folds back up without being opened fully",
              'id="fewer-sessions"' in markup
              and "fewer.hidden = state.shownSessions <= SESSIONS_SHOWN;" in page
              and "Math.min(state.shownSessions, newest.length) - 20" in page)
        check("and a session row says how long you played",
              "count.textContent = s.active_ms ? playedFor(s.active_ms)" in page
              and "function playedFor(" in page
              and "count.title = `${s.samples.toLocaleString()} points`;" in page)

        # And the edge margin explains itself by drawing the boundary, so the
        # sentence under it was one more thing to read every time.
        check("the edge margin has no paragraph under it",
              'id="follow-label"' not in markup
              and "How close to the edge of the screen" not in markup)

        # Everything the playback measures is the window between them, not the
        # whole route: where it stops, where it starts again, how far through
        # it is and how long is left.
        check("and the playback runs between them and nowhere else",
              "play.at = Math.min(play.until," in page
              and "play.at = Math.max(play.from, play.at - play.speed" in page
              and "play.dir < 0 ? play.at <= play.from : play.at >= play.until"
                  in page
              and "play.at = Math.max(play.from, Math.min(play.until, elapsed));"
                  in page)
        check("the clock counts through the window, the thumb along the route",
              "(elapsed - play.from) / span" in page
              and "elapsed / play.to" in page
              and "(play.until - elapsed) / play.speed" in page)
        # A window narrower than the thumb is one you cannot put the playhead
        # inside, and two grips on one pixel cannot be told apart.
        check("and the two of them cannot cross or meet",
              "PLAY_THUMB_PX / span" in page
              and "play.until / play.to - gap" in page
              and "play.from / play.to + gap" in page)
        # The grips, the death ticks and the hover mark are all placed against
        # the same track, so they are placed by the same function.
        check("everything drawn on the track is placed the same way",
              page.count("playTrackAt(") >= 4)

        check("the stub answers every call the wiring makes on a control",
              all(m in page[page.index("const MISSING = {"):
                            page.index("function control(id)")]
                  for m in ("addEventListener()", "setAttribute()",
                            "add() {}", "remove() {}", "setProperty()",
                            "replaceChildren()")))
        check("a control the page does not have is stubbed, not thrown on",
              "function control(id)" in page
              and "document.getElementById(" not in
                  page[page.index("function wireControls() {"):
                       page.index("\nfunction ",
                                  page.index("function wireControls() {") + 10)])

    asyncio.run(run())
    store.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} check(s) failed: {', '.join(FAILS)}")
        return 1
    print("all checks passed. The pipeline works; plug in signatures for live capture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

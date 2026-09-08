#!/usr/bin/env python3
"""End-to-end check: simulate a route, serve it, hit every endpoint.

Run this after setup to confirm the pipeline works before you touch the game.

    python tools/selftest.py
"""

from __future__ import annotations

import copy
import json
import re
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
from tracker.store import BREAK_RELOAD, Store  # noqa: E402
import import_legacy  # noqa: E402  (tools/, added to the path above)
import repair_jumps  # noqa: E402

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
    check("the repair pass for routes recorded before this exists",
          (ROOT / "tools" / "repair_underground.py").exists())

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
        for field in ("w.layer", "d.layer", "v.plane"):
            check(f"the viewer files marks by {field}",
                  f"onThisPlane({field})" in page)

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
              "'respawn-mark'" in page and "respawnsInside" in page
              and "addRespawnMark" in page)

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

        # Some dungeons cannot be located from a route at all, so the last
        # word is the user's: a place set by hand outranks everything worked
        # out from the recording, and applies to every visit to that map.
        v0 = ints["visits"][0]
        st_, put = await post("/api/place",
                              {"map_id": v0["map_id"], "wx": 1234.0, "wz": 5678.0})
        check("POST /api/place records where a dungeon is", st_ == 200 and put["ok"])
        moved = [x for x in store.interior_visits() if x["map_id"] == v0["map_id"]]
        check("a place set by hand wins over anything inferred",
              moved and all(x["placed"] == "by hand" and abs(x["wx"] - 1234.0) < 1e-9
                            for x in moved),
              f"{len(moved)} visit(s)")
        st_, cleared = await post("/api/place",
                                  {"map_id": v0["map_id"], "clear": True})
        check("and it can be taken back", st_ == 200 and cleared["ok"]
              and not store.places())
        st_, bad_place = await post("/api/place", {"wx": 1.0})
        check("placing needs to say which map", st_ == 400)

        # Saying a place is nowhere is a different answer from not knowing
        # where it is, and clearing cannot stand in for it: clearing hands the
        # question back to the tiers, and they always answer. On routes.db the
        # Roundtable Hold then takes an `exit` anchor from the one time
        # leaving it did not look like a warp.
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
                   page.index("function buildUnplaced(")])
        off = page[page.index("function buildOffMap("):
                   page.index("function buildUnplaced(")]
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
        check("an unplaced dungeon can still be put somewhere by hand",
              "function startPlacing" in page and "Put on map" in page
              and "placeAt(e.latlng)" in page)
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
        check("the session list can be expanded rather than growing forever",
              'id="more-sessions"' in html)
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
        warp = page[page.index("if (kind === 'warp' && e.from) {"):
                    page.index("// The lasting mark, so the playback")]
        check("the line a jump draws is only there while it is being made",
              "if (animate) {" in warp and "const arc = L.polyline(" in warp
              and warp.index("if (animate) {")
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
              and "map.on('mousemove', (e) => { if (play.on) "
                  "playHover(e.latlng); });" in page
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
        check("and it only jumps when the count goes up",
              "const rose = n > play.deaths;" in page
              and "if (rose && box.classList) {" in page)
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
                     page.index("function playBand(")]
        check("a legacy dungeon played back is not dimmed or taken away",
              "function playOpen(" in page
              and "if (playOpen(where)) {" in enter
              and "dimBackground(false);" in enter)
        line = page[page.index("function playLine("):
                    page.index("const PLAY_GLYPH")]
        check("and its path is drawn on the map, not in the cave overlay",
              "line.addTo(open ? play.group : insideGroup);" in line
              and "if (faint && !open) {" in line)
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
              and "item.line.setStyle({ color: bandColor(band) });" in page)
        # A mark made inside a dungeon has only that dungeon's own metres.
        # The world position the endpoint hands back for it is the *entrance*,
        # so falling back to it drew deaths at the cave mouth with the cave's
        # real path on screen beside them.
        pe = page[page.index("function playEvents("):page.index("async function buildPlayback(")]
        check("a death inside a dungeon is never drawn at its entrance",
              "playVisitAt(frames, d.map_id, d.ts)" in pe
              and "if (!hit) continue;" in pe)
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
        check("the speed reads next to the play button",
              all(x in mid for x in ("play-speed", "play-toggle", "play-rate"))
              and mid.index("play-speed") < mid.index("play-toggle")
                  < mid.index("play-rate"))
        widths = [r for r in bare.split("}")
                  if "#play-speed {" in r or "#play-rate {" in r]
        check("and the two sides of it are the same width",
              len(widths) == 2 and all("width: 124px" in r for r in widths))
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
              "playMark(kind, e.from," in flash
              and "playMark(kind, e.xy," in flash
              and "playLand(left);" in flash
              and "playLand(lasting)" in flash)
        check("every run through a cave is drawn, not just the newest",
              "playLine(run, run.xy.length, run.where !== where);" in page
              and "you have been in here" in page)
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
                   page.index("function playBand(")]
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
        check("a dungeon is drawn from the doorway its visits agree on",
              "function agreedDoor(" in page
              and page.count("agreedDoor(") >= 3
              and "const SAME_DOOR_M = 15;" in page)
        check("and its marker goes to the same door",
              "atDoor(a) - atDoor(b)" in page)
        # Both ends drawn at once say "these two places are related", not
        # "you went from this one to that one".
        # Inside a dungeon the position mark moved on every sample and the
        # line behind it waited for the recorder to commit and the overlay to
        # refresh, so the path trailed the mark by up to five seconds.
        check("the line inside a dungeon keeps up with the position mark",
              "function redrawLiveInside(" in page
              and "state.liveInside.push(s.local);" in page
              and "insideLiveGroup = L.layerGroup().addTo(map);" in page)
        check("and it survives the overlay being redrawn under it",
              "setTimeout(redrawLiveInside, 0);" in
              page[page.index("function drawInside("):][:400])
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
        # A jump across the map at speed left the follow pan chasing a target
        # that moved again every tick, so it never arrived.
        check("a leap across the map is gone to at once, not glided to",
              "map.panInside(ll, { padding: effectivePad(), animate: !leap });"
              in page)
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
        check("measured against the screen, not against the ground",
              "Math.hypot(size.x, size.y) * PLAY_LEAP_SHARE" in page)
        # The way out of a mode that has taken the whole map over.
        stop = bare[bare.index(".play-btn.on {"):]
        stop = stop[:stop.index("}")]
        check("the button that stops the playback is red",
              "#ff8a72" in stop)
        check("a teleport plays as going from one place to the other",
              "function playTravel(" in page
              and "const PLAY_TRAVEL_MS = 420;" in page
              and "setTimeout(() => playLand(lasting), wait)" in page)
        check("and a respawn is an event of its own",
              "kind: 'respawn'" in page and "state.respawnList" in
              page[page.index("function playEvents("):
                   page.index("async function buildPlayback(")])

        # Three map options that are each one word long and each fix a thing
        # that was reported as a drawing bug. Nothing else in the viewer reads
        # them, so removing one breaks the map quietly and only on a gesture.
        opts = page[page.index("map = L.map('map', {"):]
        opts = opts[:opts.index("});")]
        check("the map does not glide after a flick",
              "inertia: false" in opts)
        check("zooming redraws the path instead of stretching it",
              "zoomAnimation: false" in opts)
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
              and "if (!home && !playSamePlace(play.where, e.where)) return;"
              in page)
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
        # The pins are built from the answer already in hand; the frame
        # learning that follows fetches a path per dungeon, and doing that
        # first left the map with no pins on it for the length of two dozen
        # round trips.
        marks_at = page.index("mark.addTo(markerGroup);")
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

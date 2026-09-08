"""The recording loop.

Two rules produce a line break rather than a drawn segment:

  1. The map ID changed. Catches every grace warp and every cave entry with
     no thresholds involved.
  2. Implied speed exceeds a ceiling. Speed rather than raw distance, so a
     stalled writer or an alt-tab producing a 30-second gap doesn't get
     mistaken for a teleport.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Optional

from .coords import (INTERIOR, MapConfig, MapId, SURFACE, UNDERGROUND,
                     UNKNOWN, is_no_map)
from .store import BREAK_MAP, BREAK_RELOAD, BREAK_SPEED, Store, now_ms

# The break codes live in store.py, next to the column they are written into:
# reading them back is half of what they are for.


class Sampler:
    def __init__(
        self,
        source,
        store: Store,
        cfg: dict,
        on_sample: Optional[Callable[[dict], None]] = None,
        clock: Optional[Callable[[], int]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        # Injected so a replay (tools/import_legacy.py) can stamp samples with
        # the times they were originally recorded, and go through this class
        # rather than reimplementing its rules. Resolved here rather than as a
        # default argument, which would bind now_ms before a test can replace
        # it.
        self.clock = clock if clock is not None else now_ms
        self.source = source
        self.store = store
        self.maps = MapConfig(cfg)
        self.on_sample = on_sample
        self.on_event = on_event
        # None until HP is read at all, so a database recorded without the HP
        # pointer simply has no deaths rather than a wrong first one.
        self.prev_hp: Optional[int] = None

        rec = cfg.get("recording", {})
        self.interval = float(rec.get("interval_s", 0.25))
        self.min_move = float(rec.get("min_move_m", 1.5))
        self.max_speed = float(rec.get("max_speed_mps", 40.0))
        self.idle_flush_s = float(rec.get("idle_flush_s", 5.0))
        # How many reads in a row may fail before whatever comes back counts
        # as somewhere you were put. Counted in reads rather than seconds
        # because a replay of an imported route has no failed reads at all --
        # every point in the file is a successful one -- and measuring the
        # wall clock instead made a file sampled every five seconds look like
        # one long load screen.
        blind_s = float(rec.get("blind_reload_s", 3.0))
        self.blind_reads = max(4, int(blind_s / max(self.interval, 1e-3)))

        # Which source recorded this. A database ends up holding both
        # simulator runs and live capture, and once they are all one path
        # there is otherwise nothing to tell them apart.
        describe = getattr(source, "describe", None)
        self.session_id = store.start_session(
            describe() if describe else None, started_ms=self.clock()
        )
        self.prev: Optional[dict] = None
        self.last_surface: Optional[tuple[float, float]] = None
        self.current_map: Optional[int] = None
        self.unknown_areas: set[int] = set()
        # (map id, why) pairs already printed, so a repeated crossing does
        # not repeat the advice four times a second.
        self.said: set[tuple[int, str]] = set()
        # Set while a load screen is on. Whatever comes back afterwards is
        # somewhere you were put, not somewhere you walked.
        self.reloaded = False
        # True from the moment HP hits zero until it comes back. The break
        # belongs to neither end of that: not to the death, which is the last
        # thing you walked to, and not to the body settling afterwards, which
        # on 2026-09-06 stored steps of 1.6 to 5.5 m before the grace. It
        # belongs to the reading where you are alive again.
        self.dead = False
        # Reads that came back with nothing, since the last that did not.
        # Standing still reads fine and stores nothing; a load screen or the
        # map menu cannot be read at all. Only the second one means you were
        # not being watched.
        self.blind = 0
        self._pending = 0
        self._last_commit = time.time()
        self.counts = {"samples": 0, "breaks": 0, "events": 0, "skipped": 0,
                       "interior_samples": 0, "reloads": 0, "deaths": 0}

    # Above walking noise -- a crossing is half a second of movement, a few
    # metres -- and far below the distance a genuinely different frame would
    # be out by, which is the whole width of the map.
    ORIGIN_TOLERANCE_M = 25.0

    def _say_once(self, map_id: int, why: str) -> bool:
        if (map_id, why) in self.said:
            return False
        self.said.add((map_id, why))
        return True

    def _check_origin(self, m, r, world) -> None:
        """Say what an underground map's origin is, or that it disagrees.

        Said once per map per reason, not once per map: warping in cannot
        measure anything, and if that were the last word on a map then walking
        in later -- which does measure it -- would print nothing, and the one
        line worth having would be the one suppressed.
        """
        prev = self.prev
        walked_in = (prev is not None and prev.get("wx") is not None
                     and not self.reloaded)
        if not walked_in:
            # A warp down measures nothing: where you were standing before it
            # says nothing about where you arrived.
            if world is None and self._say_once(r.map_id, "unplaced"):
                print(
                    f"\n  {m} has no origin, so its path is recorded but not "
                    f"drawn. Walk into it on foot -- down the lift, or across "
                    f"from a river that is already placed -- and this will "
                    f"print the value to add.\n"
                )
            return

        implied = (prev["wx"] - r.x, prev["wz"] - r.z)
        if world is None:
            if not self._say_once(r.map_id, "measured"):
                return
            print(
                f"\n  {m} has no origin yet. Measured from the way you just "
                f"came in:\n"
                f"    [maps.area_origin]\n"
                f"    {m.area} = [{implied[0]:.2f}, {implied[1]:.2f}]\n"
                f"  Add that under [maps] in config.toml and restart, then "
                f"tools/repair_underground.py --write places what is already "
                f"recorded.\n"
            )
            return

        # It is placed. Does the way you came in agree with where it is drawn?
        off = math.dist(implied, (world[0] - r.x, world[1] - r.z))
        if off > self.ORIGIN_TOLERANCE_M and self._say_once(r.map_id, "adrift"):
            print(
                f"\n  {m} is drawn {off:.0f} m from where walking into it "
                f"says it is, so it does not share its area's frame. Give it "
                f"its own entry:\n"
                f"    [maps.map_origin]\n"
                f'    "{m}" = [{implied[0]:.2f}, {implied[1]:.2f}]\n'
            )

    def _speed(self, wx: float, wz: float, ts: int) -> float:
        p = self.prev
        if not p or p.get("wx") is None:
            return 0.0
        dt = max((ts - p["ts"]) / 1000.0, 1e-3)
        return math.dist((wx, wz), (p["wx"], p["wz"])) / dt

    def step(self) -> None:
        self._step()
        # Runs even when _step recorded nothing. Standing still returns early
        # at the movement gate, and if the flush lived only on the path that
        # stores a sample, the last few would sit in an open write transaction
        # for as long as the player stayed put: lost on a crash, invisible to
        # the viewer, and holding a write lock that blocks every other tool
        # touching the database.
        self._maybe_commit()

    def _maybe_commit(self) -> None:
        if not self._pending:
            return
        if (self._pending >= 20
                or time.time() - self._last_commit > self.idle_flush_s):
            self.store.commit()
            self._pending = 0
            self._last_commit = time.time()

    def _step(self) -> None:
        r = self.source.read()
        if r is None:
            # GameSource returns None for every kind of failure: the pointer
            # chain unresolvable while the world is torn down, the process
            # gone, the read itself throwing. All of them mean the same thing.
            self.blind += 1
            return
        if is_no_map(r.map_id):
            # No map loaded, so the position that came with it is stale or
            # garbage. Checked here rather than in the source so it holds for
            # every source, present and future.
            #
            # The reading is useless but the fact of it is not: nothing crosses
            # a load screen on foot. Dying is the case that matters -- you go
            # down in one place and get up at a grace, often close enough that
            # the speed check waves it through, and the line gets drawn as if
            # you walked back.
            self.reloaded = True
            self.counts["skipped"] += 1
            return
        ts = self.clock()

        # A load screen is supposed to announce itself as the no-map sentinel,
        # and usually does. It did not on 2026-09-06 at 20:13: the recorder
        # read nothing at all for 73 seconds -- the map menu and the warp --
        # and came back inside a cave with `reloaded` still false, so the
        # anchor written for that cave was the grace 2,900 m away the warp
        # started from, and the cave's whole path was drawn there.
        #
        # Not being able to read the game is the more reliable signal, because
        # it does not depend on the game reporting anything: if the recorder
        # could not see you, it cannot claim you walked.
        if self.blind >= self.blind_reads:
            self.reloaded = True
            self.counts["reloads"] += 1
        self.blind = 0

        # And the most reliable signal of all is HP, where there is any. The
        # load screen after a death is supposed to announce itself and mostly
        # does not: nine deaths in one cave on 2026-09-06 produced two
        # sentinels between them, so seven respawns were drawn as a walk from
        # the body to the grace. Coming back to life needs no threshold and
        # cannot be missed -- and it is the right moment, which the reading
        # straight after the death is not, because the body is still settling.
        if self.dead and r.hp is not None and r.hp > 0:
            self.dead = False
            self.reloaded = True

        m = MapId.unpack(r.map_id)
        layer = self.maps.classify(m, r.y)

        if layer == UNKNOWN and m.area not in self.unknown_areas:
            self.unknown_areas.add(m.area)
            print(
                f"  unclassified area {m.area} ({m}) -- add it to "
                f"[maps.area_labels] in config.toml to place it correctly"
            )

        # Map transition: close the previous map, open this one.
        #
        # last_surface is where the marker goes, and it is only the entrance if
        # you walked in. Warp straight from a grace into a dungeon and the last
        # surface position is wherever you were standing before the warp --
        # which would put the cave's marker, and the path drawn at it, in the
        # wrong part of the map. A load screen immediately before the change
        # says exactly that happened, so the visit is left unplaced instead;
        # walking back out gives interior_visits() a real position to use.
        entrance = None if self.reloaded else self.last_surface
        prev_map = self.current_map
        if r.map_id != self.current_map:
            if self.current_map is not None:
                pm = MapId.unpack(self.current_map)
                self.store.add_map_event(
                    session_id=self.session_id,
                    ts_ms=ts,
                    map_id=self.current_map,
                    layer=self.maps.classify(pm, r.y),
                    kind="leave",
                    anchor_wx=entrance[0] if entrance else None,
                    anchor_wz=entrance[1] if entrance else None,
                )
                self.counts["events"] += 1
            self.store.add_map_event(
                session_id=self.session_id,
                ts_ms=ts,
                map_id=r.map_id,
                layer=layer,
                kind="enter",
                anchor_wx=entrance[0] if entrance else None,
                anchor_wz=entrance[1] if entrance else None,
            )
            self.counts["events"] += 1
            if layer in (INTERIOR, UNKNOWN):
                print(f"\n  entered {self.maps.label(m)} ({m}) — path stored, "
                      f"shown as a marker\n")
            self.current_map = r.map_id
            map_changed = True
        else:
            map_changed = False

        world = self.maps.to_world(m, r.x, r.z)

        # Every step you walk into an underground map measures where that map
        # sits: the reading before is a real world position and the reading
        # after is the same place in the map's own coordinates, so the
        # difference is the origin. Worth doing whether or not one is
        # configured -- without one it is the value to add, and with one it is
        # a check that the map really does share the frame.
        if layer == UNDERGROUND and map_changed:
            self._check_origin(m, r, world)

        # Interiors have their own local axes; recording their raw coordinates
        # on the world plane is exactly the bug that draws cave crawling as an
        # overworld walk. Keep the sample, drop the world position.
        if world is None:
            wx = wz = None
        else:
            wx, wz = world
            if layer == SURFACE:
                self.last_surface = (wx, wz)

        # Death, before the movement gate: you usually die standing still, or
        # close enough, and the gate would drop the reading that carries it.
        # Recorded where you fell -- the next position is a grace, which is
        # why the line has to break there too.
        if r.hp is not None:
            if self.prev_hp is not None and self.prev_hp > 0 and r.hp <= 0:
                place = (wx, wz) if wx is not None else (
                    self.last_surface if self.last_surface else (None, None))
                self.store.add_map_event(
                    session_id=self.session_id,
                    ts_ms=ts,
                    map_id=r.map_id,
                    layer=layer,
                    kind="death",
                    anchor_wx=place[0],
                    anchor_wz=place[1],
                    # Only meaningful inside a dungeon, where the anchor is
                    # the entrance rather than the spot.
                    local_x=r.x,
                    local_z=r.z,
                )
                self.counts["deaths"] += 1
                self._pending += 1
                # Not self.reloaded: that would break the line at the death
                # itself, and the death is the last thing you walked to.
                self.dead = True
                print(f"  died in {self.maps.label(m)} ({m})")
                if self.on_event:
                    self.on_event({
                        "type": "death", "ts": ts, "map": str(m),
                        "label": self.maps.label(m),
                        "wx": place[0], "wz": place[1],
                    })
            self.prev_hp = r.hp

        # Movement gate. Interiors have no world position, so gate them on
        # their own local coordinates instead of skipping them.
        if not map_changed and self.prev:
            if wx is not None and self.prev.get("wx") is not None:
                if math.dist((wx, wz), (self.prev["wx"], self.prev["wz"])) < self.min_move:
                    self.counts["skipped"] += 1
                    return
            elif wx is None and self.prev.get("wx") is None:
                if math.dist((r.x, r.z), (self.prev["lx"], self.prev["lz"])) < self.min_move:
                    self.counts["skipped"] += 1
                    return

        if wx is not None:
            speed = self._speed(wx, wz, ts)
        elif (self.prev and not map_changed
                and self.prev.get("wx") is None):
            # Inside a dungeon there is no world position, but the local
            # coordinates are metres too: a lift or a teleporter in there
            # moves you further than anything can walk, and without this the
            # line was drawn straight through the rock.
            dt = max((ts - self.prev["ts"]) / 1000.0, 1e-3)
            speed = math.dist(
                (r.x, r.z), (self.prev["lx"], self.prev["lz"])
            ) / dt
        else:
            speed = 0.0
        # A map change means a teleport only when the two maps are not tiles
        # of the same world. Crossing an overworld tile boundary is walking,
        # and the speed check still catches a warp between two tiles.
        crossed_tile = self.maps.same_plane(
            MapId.unpack(prev_map) if prev_map is not None else None, m
        )
        # Most specific reason wins: a death is a reload even though the map
        # also changed, and that distinction is what lets a later repair pass
        # tell a stale tile-crossing break from one that was always real.
        if self.reloaded:
            break_before = BREAK_RELOAD
            self.counts["reloads"] += 1
            self.reloaded = False
        elif speed > self.max_speed:
            break_before = BREAK_SPEED
        elif map_changed and not crossed_tile:
            break_before = BREAK_MAP
        else:
            break_before = 0
        if break_before:
            self.counts["breaks"] += 1

        # Interiors are stored with a NULL world position: kept, so the path
        # inside isn't lost, but excluded from the world map by every query.
        self.store.add_sample(
            session_id=self.session_id,
            ts_ms=ts,
            map_id=r.map_id,
            layer=layer,
            x=r.x, y=r.y, z=r.z,
            wx=wx, wz=wz,
            break_before=break_before,
        )
        self.counts["samples"] += 1
        if wx is None:
            self.counts["interior_samples"] += 1
        self._pending += 1

        if self.on_sample:
            self.on_sample(
                {
                    "ts": ts, "layer": layer, "y": r.y,
                    "wx": wx, "wz": wz,
                    # Local coordinates too: inside a dungeon they are the only
                    # position there is, and the viewer draws that path.
                    "x": r.x, "z": r.z,
                    "break": break_before, "map_id": r.map_id,
                    "map": str(m), "label": self.maps.label(m),
                    "session_id": self.session_id,
                }
            )
        self.prev = {"ts": ts, "wx": wx, "wz": wz, "lx": r.x, "lz": r.z}

    def finish(self) -> None:
        if self.current_map is not None:
            self.store.add_map_event(
                session_id=self.session_id,
                ts_ms=self.clock(),
                map_id=self.current_map,
                layer=INTERIOR,
                kind="leave",
                anchor_wx=self.last_surface[0] if self.last_surface else None,
                anchor_wz=self.last_surface[1] if self.last_surface else None,
            )
        self.store.end_session(self.session_id, ended_ms=self.clock())
        self.store.commit()

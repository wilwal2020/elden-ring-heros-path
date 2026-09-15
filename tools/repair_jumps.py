#!/usr/bin/env python3
"""Mark jumps inside dungeons that were recorded as if you had walked them.

    python tools/repair_jumps.py            # say what would change
    python tools/repair_jumps.py --write    # change it

The speed check used to read the world position, which is NULL inside a
dungeon, so a lift or a teleporter in a cave produced no break at all: the
line was drawn straight through the rock from one end of the ride to the
other, and no teleport mark was made. The recorder falls back to the
dungeon's own local metres now. Databases written before that keep the
straight lines, and this puts the breaks in.

The threshold is not the recorder's `max_speed_mps`. That one is 40 m/s
because a quarter-second sample interval turns small position noise into
large implied speeds; these routes were sampled every five seconds, where
40 m/s would mean a 200 m stride. It comes from the data instead. Measured
on routes.db:

  * on the world plane, 39,484 unbroken pairs, the fastest 15.9 m/s --
    Torrent at a gallop, and no gap anywhere to separate anything from
    anything else. Surface jumps were never the broken case: the speed check
    always worked where there was a world position. So this pass leaves the
    world plane alone.
  * inside dungeons, 13,875 unbroken pairs. Five sit between 17 and 26 m/s,
    and the band from 10 to 16 m/s is completely empty. All five rose
    between 19 and 55 m while covering 107 to 143 m of ground in five
    seconds, which is a lift, not a walk.

15 m/s sits in the empty band, half the slowest jump and half again above
the fastest ordinary movement.

Speed is not the only shape a missed jump takes. Dying is slow: the animation
and the load screen together take eight to fifteen seconds, and the grace you
get up at is close enough that the implied speed is five to eight metres a
second -- walking pace. What gives it away is the hole. A session sampling
every quarter second that reports nothing for thirteen seconds, and comes back
ninety metres away, did not walk: there would be fifty samples along the path
if it had. So the second rule is a gap in the recording, measured against the
session's own rhythm rather than a fixed number, since the same database holds
sessions sampled every quarter second and every five.

Standing still also leaves a hole -- the movement gate stores nothing while
you do not move -- so the distance is what separates the two, and on routes.db
it separates them completely: of 343 quiet stretches inside dungeons, 337 end
within 16.5 m of where they began (standing at a grace, reading an item) and
six end between 32.9 and 96.1 m away. Nothing lands in between. Five of those
six are one visit to m30_01, each rising the same 21 m back to the same grace:
five deaths.

A third needs no rule at all. Where the HP reading survives, the deaths are
already in the database, and the first sample after one is the grace you got
up at -- never a step you took. The load screen that should mark it is not
reliable: nine deaths in one cave on 2026-09-06 produced two sentinels
between them, and the other seven respawns were drawn as a walk from the body
to the grace at four or five metres a second, which no speed or gap rule
would ever flag. This one has no threshold in it.

The first two rules run inside dungeons only, and neither is a guess about
what the jump was -- a death and a teleporter look identical once the HP
reading is gone. Nothing is deleted: the samples are untouched and only the break flag
changes, so `tools/repair_breaks.py` can undo it if a threshold turns out to
be wrong for your data.
"""

from __future__ import annotations

import argparse
import datetime
import math
import statistics
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tracker.coords import MapId  # noqa: E402
from tracker.store import BREAK_RELOAD, BREAK_SPEED, Store, kept_a_copy  # noqa: E402

# In the empty band the data leaves between walking and riding a lift.
DEFAULT_MAX_MPS = 15.0

# A hole in the recording counts when it is this many times the session's own
# rhythm, and at least this many seconds -- a quarter-second session goes
# quiet for two seconds all the time, and it means nothing.
GAP_MULTIPLE = 8
GAP_MIN_S = 4.0

# ...but the multiple cannot go up for ever. A death costs a roughly fixed
# eight to twenty seconds of wall clock -- the animation and then the load --
# however often the recorder is sampling, so against a five-second import
# eight times the rhythm is forty seconds and no death can reach it. That is
# why every death in the imported sessions was invisible to this pass.
# Measured over thirteen deaths reported by hand, across sampling from a
# quarter second to five seconds, the smallest hole is 8.3 s.
GAP_CAP_S = 8.0


def quiet_after(rhythm: float) -> float:
    """How long a hole has to be, for a session sampling every `rhythm`."""
    return max(GAP_MIN_S, min(GAP_MULTIPLE * rhythm, GAP_CAP_S))

# And how far you have to have moved during the hole for it to be a jump
# rather than standing still. In the empty band between 16.5 m and 32.9 m.
DEFAULT_GAP_M = 25.0

FIND = """
SELECT a.id, a.session_id, a.ts_ms, a.map_id, a.x, a.z, a.y,
       b.ts_ms AS bts, b.x AS bx, b.z AS bz, b.y AS by_
FROM samples a JOIN samples b ON b.id = a.id - 1
WHERE a.break_before = 0
  AND a.session_id = b.session_id
  -- Inside one dungeon, where local metres are the only position there is.
  AND a.wx IS NULL AND b.wx IS NULL
  AND a.map_id = b.map_id
ORDER BY a.ts_ms
"""

# The same hole, out on the surface. Kept apart from FIND and behind its own
# flag because the two are not equally safe, and the difference is measured:
#
# Inside a dungeon the speeds leave an empty band -- 13,875 unbroken steps,
# five of them between 17 and 26 m/s and nothing at all from 10 to 16 -- so a
# threshold can sit in the gap and be right. On the surface there is no such
# band. Measured over the 41,149 surface steps where the recording is dense
# enough to trust (a gap of a second or less, so nothing can be hiding in
# between), the fastest ground speed reached is 15.6 m/s, and the fastest
# unbroken step in the whole database is 15.9 m/s. Torrent at a gallop covers
# the same ground a short warp does. No speed rule can separate them, and one
# that claims to would be marking real rides as teleports.
#
# What is left is the hole itself: 135 m with nothing recorded in between, in
# a session sampling four times a second, is thirty-odd readings that never
# arrived -- which is a load screen, not a ride. That is a strong hint and not
# a proof, because a stalled recorder leaves the same hole, so this pass
# reports and does not write unless asked.
FIND_SURFACE = """
SELECT a.id, a.session_id, a.ts_ms, a.map_id, a.wx AS x, a.wz AS z, a.y,
       b.ts_ms AS bts, b.wx AS bx, b.wz AS bz, b.y AS by_
FROM samples a JOIN samples b ON b.id = a.id - 1
WHERE a.break_before = 0
  AND a.session_id = b.session_id
  AND a.wx IS NOT NULL AND b.wx IS NOT NULL
ORDER BY a.ts_ms
"""

# The ceiling standing still can reach.
#
# Nothing is stored until you have moved `min_move_m` *across the ground*
# from the last stored sample. So however long a hole is, if you stood through it the next sample
# lands the moment you crossed that line -- 1.5 m, plus however far you got in
# the one reading it took to notice. Being carried through the hole owes the
# gate nothing and lands wherever it put you.
#
# That is a ceiling derived from how the recorder works rather than a
# threshold picked out of a histogram, and it is different for every session:
# a quarter of a second of sprinting is 4 m, five seconds of it is 78. Which
# is also its limit -- against an imported route sampled every five seconds
# the ceiling is 80 m, so a death whose grace is seven metres away sits far
# below it and cannot be found this way. Nothing can find that one.
#
# TOP_MPS is measured, not assumed: over the 41,149 surface steps sampled
# densely enough that nothing can hide between them, the fastest ground speed
# reached is 15.6 m/s.
#
# Measured against thirteen deaths reported by hand, this finds ten. The three
# it misses are all in imported routes, for the reason above: their ceiling is
# forty to eighty metres and the graces were seven.
TOP_MPS = 15.6
DEFAULT_GATE_M = 1.5

# Two arrivals this close together are the same place. Used to say which
# candidates land somewhere you have been put before, which is the strongest
# thing in the report: on `routes.db` those come in sets whose step lengths
# agree to a tenth of a metre -- 20.6, 20.6, 20.7, 20.7 -- because it is one
# boss and one grace, over and over. Standing still does not repeat a
# distance.
SAME_PLACE_M = 8.0


# How near the departure point counts as having come back, and how long you
# have to do it in. This is the user's own test and it is a good one: a
# teleport takes you somewhere you meant to go, so you carry on from there. A
# death puts you at a grace and the first thing you do is walk back to your
# corpse. Reported rather than acted on -- it says which of the two a jump
# probably was, and only you can say for certain.
BACK_M = 20.0
BACK_WINDOW_S = 420.0
SURFACE_GAP_M = 25.0


def cadences(store) -> dict:
    """Each session's own sampling rhythm, as the median stored gap.

    Taken from the data rather than from config, because the same database
    holds live capture at a quarter second and imported routes at five, and a
    hole only means something measured against the rhythm around it.
    """
    out = {}
    for (sid,) in store.db.execute(
            "SELECT DISTINCT session_id FROM samples").fetchall():
        gaps = [row[0] for row in store.db.execute(
            "SELECT ts_ms - LAG(ts_ms) OVER (ORDER BY ts_ms) FROM samples "
            "WHERE session_id = ?", (sid,)).fetchall()
            if row[0] is not None and row[0] > 0]
        if gaps:
            out[sid] = statistics.median(gaps) / 1000.0
    return out


# What separates the grace from the body still settling. Measured over the
# nine deaths in m31_10 on 2026-09-06: the corpse produced steps of 1.6 to
# 5.5 m at gaps of 0.5 to 4.0 s, and every grace was 21.3 m or more away
# after a gap of 8.0 s or more. Both lines sit in the empty band, with room
# on each side.
RESPAWN_GAP_S = 6.0
RESPAWN_M = 10.0


def find_respawns(store, gap_s: float = RESPAWN_GAP_S,
                  metres: float = RESPAWN_M):
    """The grace you got up at, for deaths whose respawn was never marked.

    Driven by the recorded deaths rather than by looking for jumps, so it
    finds the ones no speed or gap rule can: a respawn inside a cave is four
    or five metres a second over eight seconds, which is walking pace.

    The first sample after a death is not it. The body settles first, and
    that is stored as one or two small steps -- marking those would break the
    line in the wrong place and leave the walk to the grace still drawn.
    Returns the same shape as find_jumps, so the two report and write
    together.
    """
    out = []
    for d in store.db.execute(
        "SELECT ts_ms FROM map_events WHERE kind IN ('death', 'death_by_hand') "
        "ORDER BY ts_ms"
    ).fetchall():
        rows = store.db.execute(
            "SELECT a.id, a.ts_ms, a.map_id, a.x, a.z, a.y, a.break_before, "
            "       b.ts_ms AS bts, b.x AS bx, b.z AS bz, b.y AS by_ "
            "FROM samples a JOIN samples b ON b.id = a.id - 1 "
            "WHERE a.ts_ms > ? AND a.ts_ms <= ? AND a.session_id = b.session_id "
            "ORDER BY a.ts_ms",
            (d["ts_ms"], d["ts_ms"] + Store.RESPAWN_WINDOW_MS),
        ).fetchall()
        for r in rows:
            dt = max((r["ts_ms"] - r["bts"]) / 1000.0, 1e-3)
            dist = math.dist((r["x"], r["z"]), (r["bx"], r["bz"]))
            if dt < gap_s or dist < metres:
                continue          # the body settling, not the grace
            if r["break_before"] == 0:
                out.append((r, dist, dt, dist / dt,
                            "the grace you got up at"))
            break                 # only the first one is the respawn
    return out


def find_jumps(store, max_speed: float = DEFAULT_MAX_MPS,
               gap_metres: float = DEFAULT_GAP_M):
    """Steps inside a dungeon that were not walked.

    Two shapes, because a missed jump has two. Too fast to walk, or a hole in
    the recording you came out of a long way from where you went in.

    Returned rather than acted on, so the rules can be tested without a
    database full of real lifts to point them at. Each hit is
    (row, distance_m, seconds, speed_mps, why); `spared` carries what each
    rule declined to flag, which is what makes the margin visible.
    """
    cad = cadences(store)
    hits = []
    spared = {"speed": [], "gap": []}
    for r in store.db.execute(FIND).fetchall():
        dt = max((r["ts_ms"] - r["bts"]) / 1000.0, 1e-3)
        d = math.dist((r["x"], r["z"]), (r["bx"], r["bz"]))
        speed = d / dt
        rhythm = cad.get(r["session_id"], 0.5)
        quiet = dt >= quiet_after(rhythm)
        if speed > max_speed:
            hits.append((r, d, dt, speed, "too fast to walk"))
        elif quiet and d >= gap_metres:
            hits.append((r, d, dt, speed, "a hole in the recording"))
        else:
            spared["speed" if not quiet else "gap"].append(
                speed if not quiet else d)
    return hits, spared


def came_back(store, session_id: int, ts_ms: int, x: float, z: float):
    """Did the route return to (x, z) shortly after leaving it?

    Seconds if it did, None if it did not. This is what tells a death from a
    teleport once the HP reading is gone: you go back for your runes.
    """
    row = store.db.execute(
        "SELECT MIN(ts_ms) t FROM samples "
        "WHERE session_id = ? AND ts_ms > ? AND ts_ms <= ? "
        "  AND wx IS NOT NULL "
        "  AND (wx - ?) * (wx - ?) + (wz - ?) * (wz - ?) <= ?",
        (session_id, ts_ms, ts_ms + int(BACK_WINDOW_S * 1000),
         x, x, z, z, BACK_M * BACK_M),
    ).fetchone()
    return None if row["t"] is None else (row["t"] - ts_ms) / 1000.0


def gate_of(cfg_path: Path = ROOT / "config" / "config.toml") -> float:
    """The recorder's movement gate, read from config so the two stay in step."""
    try:
        import tomllib
        with open(cfg_path, "rb") as fh:
            return float(tomllib.load(fh).get("recording", {})
                         .get("min_move_m", DEFAULT_GATE_M))
    except Exception:
        return DEFAULT_GATE_M


def find_gate_jumps(store, gate: Optional[float] = None):
    """Steps further than standing still could have put you.

    Works on both planes and needs no distance threshold of its own: the
    ceiling comes from the gate and the session's own rhythm. Each hit is
    (row, distance_m, seconds, ceiling_m, arrivals) where `arrivals` is how
    many of the candidates land within SAME_PLACE_M of this one -- 1 means
    only itself.
    """
    if gate is None:
        gate = gate_of()
    cad = cadences(store)
    rows = store.db.execute(
        "SELECT a.id, a.session_id, a.ts_ms, a.map_id, a.layer, "
        "       a.x, a.y, a.z, a.wx, a.wz, "
        "       b.ts_ms AS bts, b.x AS bx, b.y AS by_, b.z AS bz, "
        "       b.wx AS bwx, b.wz AS bwz, b.map_id AS bmap "
        "FROM samples a JOIN samples b ON b.id = a.id - 1 "
        "WHERE a.break_before = 0 AND a.session_id = b.session_id "
        "ORDER BY a.ts_ms"
    ).fetchall()
    out = []
    for r in rows:
        if r["wx"] is not None and r["bwx"] is not None:
            flat = math.dist((r["wx"], r["wz"]), (r["bwx"], r["bwz"]))
        elif (r["wx"] is None and r["bwx"] is None
                and r["map_id"] == r["bmap"]):
            flat = math.dist((r["x"], r["z"]), (r["bx"], r["bz"]))
        else:
            continue
        # Across the ground only, because that is the axis the gate measures:
        # `Sampler._step` compares (wx, wz) and ignores height entirely. Which
        # means a lift is indistinguishable from standing still as far as the
        # gate is concerned -- you do not move horizontally, nothing is
        # stored, and the height runs away. Counting height here turned this
        # into a lift detector: 56 hits on `routes.db`, all but three of them
        # pure vertical, including Siofra's 380 m descent four times over.
        d = flat
        dt_s = (r["ts_ms"] - r["bts"]) / 1000.0
        rhythm = cad.get(r["session_id"], 0.5)
        if dt_s < quiet_after(rhythm):
            continue
        ceiling = gate + TOP_MPS * rhythm
        if d > ceiling:
            out.append([r, d, dt_s, ceiling, 1])

    # Which of them arrive somewhere another one arrives.
    for i, hit in enumerate(out):
        a = hit[0]
        n = 0
        for j, other in enumerate(out):
            b = other[0]
            if a["wx"] is not None and b["wx"] is not None:
                near = math.dist((a["wx"], a["wz"]), (b["wx"], b["wz"]))
            elif (a["wx"] is None and b["wx"] is None
                    and a["map_id"] == b["map_id"]):
                near = math.dist((a["x"], a["z"]), (b["x"], b["z"]))
            else:
                continue
            if near < SAME_PLACE_M:
                n += 1
        hit[4] = n
    return [tuple(h) for h in out]


def find_surface_jumps(store, gap_metres: float = SURFACE_GAP_M):
    """Holes in the recording out in the world, with what they look like.

    Each hit is (row, distance_m, seconds, speed_mps, why) like the others,
    plus a `back` seconds-or-None on the end saying whether the route returned
    to where it left.
    """
    cad = cadences(store)
    out = []
    for r in store.db.execute(FIND_SURFACE).fetchall():
        dt = max((r["ts_ms"] - r["bts"]) / 1000.0, 1e-3)
        d = math.dist((r["x"], r["z"]), (r["bx"], r["bz"]))
        rhythm = cad.get(r["session_id"], 0.5)
        if dt < quiet_after(rhythm) or d < gap_metres:
            continue
        out.append((r, d, dt, d / dt, "a hole in the recording",
                    came_back(store, r["session_id"], r["ts_ms"],
                              r["bx"], r["bz"])))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "routes.db"))
    ap.add_argument("--max-speed", type=float, default=DEFAULT_MAX_MPS,
                    help=f"metres per second above which a step inside a "
                         f"dungeon is a jump, not a walk (default "
                         f"{DEFAULT_MAX_MPS})")
    ap.add_argument("--gap-metres", type=float, default=DEFAULT_GAP_M,
                    help=f"how far you must have moved during a hole in the "
                         f"recording for it to be a jump rather than standing "
                         f"still (default {DEFAULT_GAP_M})")
    ap.add_argument("--ghosts", action="store_true",
                    help="put back readings taken during a load screen that "
                         "named a map you had already left. Reported either "
                         "way")
    ap.add_argument("--graces", action="store_true",
                    help="put right deaths you marked whose grace the map "
                         "change swallowed. Reported either way")
    ap.add_argument("--gate", action="store_true",
                    help="also break the line where a step is further than "
                         "standing still could have carried you. Reported "
                         "either way")
    ap.add_argument("--surface", action="store_true",
                    help="also look for holes in the recording out in the "
                         "world. Reported either way; this includes them in "
                         "what --write applies")
    ap.add_argument("--write", action="store_true",
                    help="apply the changes; without it, only report them")
    a = ap.parse_args()

    store = Store(a.db)
    ghosts = store.ghost_stays()
    lost = store.lost_graces()
    surface = find_surface_jumps(store)
    surface_ids = {r["id"] for r, *_ in surface}
    gated = find_gate_jumps(store)
    gate_ids = {r["id"] for r, *_ in gated}
    hits, spared = find_jumps(store, a.max_speed, a.gap_metres)
    seen = {h[0]["id"] for h in hits}
    hits += [r for r in find_respawns(store) if r[0]["id"] not in seen]
    hits.sort(key=lambda h: h[0]["ts_ms"])
    if ghosts:
        print(
            f"{len(ghosts)} stay(s) in a dungeon that never happened.\n\n"
            f"The position chain does not go blank while the game loads -- it "
            f"can go on\nreporting a map you were in earlier, with the "
            f"position you were last at inside\nit. That looks like an "
            f"ordinary dungeon reading, so it became a visit, a marker\non "
            f"the map, and a place for a death to be filed that you were "
            f"nowhere near.\n\n"
            f"The tell is that it lasts no time at all with a load screen in "
            f"front of it and\nsolid ground either side. Every real stay in "
            f"this database is at least 34\nsamples over 17 seconds.\n")
        print(f"  {'when':<21}{'map':<16}{'samples':>8}{'lasted':>9}"
              f"{'you were':>22}{'came back':>22}")
        for g in ghosts:
            t = datetime.datetime.fromtimestamp(g["from_ts"] / 1000)
            at = f"({g['stood_at'][0]:.0f}, {g['stood_at'][1]:.0f})"
            back = f"({g['came_back_at'][0]:.0f}, {g['came_back_at'][1]:.0f})"
            print(f"  {t:%Y-%m-%d %H:%M:%S} "
                  f"{str(MapId.unpack(g['map_id'])):<16}{g['samples']:>8}"
                  f"{g['seconds']:>8.1f}s{at:>22}{back:>22}")
        if a.ghosts and a.write:
            kept_a_copy(store, "ghosts")
            n = sum(store.drop_ghost_stay(g) for g in ghosts)
            print(f"\n  {n} reading(s) put back where you were standing. The "
                  f"visit and its marker\n  are gone, and any death filed on "
                  f"one has moved with it.")
        else:
            print("\n  left alone. Pass --ghosts --write to put them back.")
        print()

    if lost:
        print(
            f"{len(lost)} death(s) you marked have no grace, or the wrong "
            f"one.\n\n"
            f"Where you got up is read back as the first broken sample after "
            f"the death, and\na map change is deliberately not counted -- "
            f"walking through a door is one too.\nBut when you have just said "
            f"\"I died here\", the map change on the sample after is\nthe load "
            f"screen that moved you, so it should be. These were marked "
            f"before that\nwas true.\n")
        print(f"  {'the death':<21}{'next point':>12}{'grace it shows':>18}")
        for g in lost:
            t = datetime.datetime.fromtimestamp(g["ts_ms"] / 1000)
            nxt = datetime.datetime.fromtimestamp(g["next_ts"] / 1000)
            shown = ("none" if g["shown_ts"] is None else
                     datetime.datetime.fromtimestamp(
                         g["shown_ts"] / 1000).strftime("%H:%M:%S"))
            print(f"  {t:%Y-%m-%d %H:%M:%S}    {nxt:%H:%M:%S}"
                  f"{shown:>18}")
        if a.graces and a.write:
            kept_a_copy(store, "graces")
            for g in lost:
                store.claim_grace(g["ts_ms"], g["sample_id"])
            print(f"\n  put right. Each grace is now the point after the "
                  f"death, and taking the\n  death back puts the map change "
                  f"back with it.")
        else:
            print("\n  left alone. Pass --graces --write to put them right.")
        print()

    if gated:
        gate_m = gate_of()
        repeats = [h for h in gated if h[4] > 1]
        print(
            f"{len(gated)} steps further than standing still could have put "
            f"you.\n\n"
            f"Nothing is stored until you have moved {gate_m:g} m, so however "
            f"long you stand\nstill, the next sample lands the moment you "
            f"cross that line -- {gate_m:g} m plus one\nreading's worth of "
            f"walking. Being carried lands wherever you were put. The\n"
            f"ceiling is per session, because a quarter second of sprinting "
            f"is 4 m and five\nseconds of it is 78 -- which is why this "
            f"finds little in an imported route.\n\n"
            f"Measured across the ground only, because that is what the gate "
            f"measures. A lift\nmoves you without moving you horizontally, "
            f"so counting height here finds every\nlift in the database and "
            f"calls it a teleport.\n")
        print(f"  {'when':<21}{'across':>9}{'ceiling':>9} {'in':>7} "
              f"{'rose':>9}  arrived here")
        for r, d, secs, ceiling, arrivals in gated:
            t = datetime.datetime.fromtimestamp(r["ts_ms"] / 1000)
            said = (f"{arrivals} times" if arrivals > 1 else "once")
            print(f"  {t:%Y-%m-%d %H:%M:%S} {d:7.1f} m {ceiling:7.1f} m "
                  f"{secs:6.2f}s {r['y'] - r['by_']:+7.1f} m  {said}")
        if repeats:
            print(f"\n  {len(repeats)} of them land within {SAME_PLACE_M:g} m "
                  f"of another. That is the strongest thing\n  here: one boss "
                  f"and one grace, over and over, and the step lengths agree "
                  f"to\n  a tenth of a metre. Standing still does not repeat "
                  f"a distance.")
        if a.gate:
            hits += [(r, d, s_, d / max(s_, 1e-3), "further than standing still")
                     for r, d, s_, _c, _n in gated
                     if r["id"] not in surface_ids]
            hits.sort(key=lambda h: h[0]["ts_ms"])
        else:
            print("\n  left alone. Pass --gate to break the line at these "
                  "too.")
        print()

    if surface:
        print(
            f"{len(surface)} holes in the recording out in the world.\n\n"
            f"No speed rule can sort these. Measured over the surface steps "
            f"where the\nrecording is dense enough that nothing can be "
            f"hiding in between, the fastest\nground speed reached is "
            f"15.6 m/s, and the fastest unbroken step in the whole\n"
            f"database is 15.9 m/s: Torrent at a gallop covers the same "
            f"ground a short warp\ndoes. What is odd about these is the "
            f"hole -- readings that never arrived,\nwhich is what a load "
            f"screen looks like, and also what a stalled recorder looks\n"
            f"like. So they are reported and not acted on.\n\n"
            f"Two columns to read them by. 'came back' is whether the route "
            f"returned to\nwithin {BACK_M:.0f} m of where it left, inside "
            f"{BACK_WINDOW_S / 60:.0f} minutes: coming back is what a death "
            f"looks\nlike, because you go back for your runes, where a "
            f"teleport usually carries on\nfrom wherever it put you. And "
            f"'rose' is worth as much -- a drop of 90 m is\nthe fall that "
            f"killed you.\n")
        print(f"  {'when':<21}{'across':>9} {'in':>7} {'speed':>10} "
              f"{'rose':>9}  came back")
        for r, d, dt, speed, _why, back in surface:
            t = datetime.datetime.fromtimestamp(r["ts_ms"] / 1000)
            said = f"after {back:.0f}s" if back is not None else "no"
            print(f"  {t:%Y-%m-%d %H:%M:%S} {d:7.1f} m {dt:6.2f}s "
                  f"{speed:8.1f} m/s {r['y'] - r['by_']:+7.1f} m  {said}")
        if a.surface:
            hits += [(r, d, dt, sp, why) for r, d, dt, sp, why, _b in surface]
            hits.sort(key=lambda h: h[0]["ts_ms"])
        else:
            print("\n  left alone. Pass --surface to break the line at these "
                  "too, which puts a\n  teleport mark on each -- and every "
                  "teleport mark offers 'This was a death'.")
        print()

    # Counted apart from the surface ones above, because the two passes are
    # not equally sure of themselves and a single number would hide that.
    dungeon_hits = sum(1 for h in hits
                       if h[0]["id"] not in surface_ids
                       and h[0]["id"] not in gate_ids)
    total = dungeon_hits + sum(len(v) for v in spared.values())

    print(f"{total} unbroken steps inside dungeons, {dungeon_hits} of them "
          f"not walked\n")
    if not hits:
        print("nothing to do: every step inside a dungeon reads as walked.")
        store.close()
        return 0

    print(f"{'when':<21}{'map':<15} {'across':>9} {'in':>7} {'speed':>10} "
          f"{'rose':>9}  why")
    for r, d, dt, speed, why in hits:
        t = datetime.datetime.fromtimestamp(r["ts_ms"] / 1000)
        print(f"  {t:%Y-%m-%d %H:%M:%S} {str(MapId.unpack(r['map_id'])):<15} "
              f"{d:7.1f} m {dt:6.2f}s {speed:8.1f} m/s "
              f"{r['y'] - r['by_']:+7.1f} m  {why}")

    # The margins, so what is being left alone is visible rather than
    # promised: each rule is only as safe as the gap under its threshold.
    if spared["speed"]:
        print(f"\nfastest step left alone: {max(spared['speed']):.1f} m/s "
              f"(the line is {a.max_speed:g} m/s)")
    if spared["gap"]:
        print(f"furthest quiet stretch left alone: {max(spared['gap']):.1f} m "
              f"(the line is {a.gap_metres:g} m)")

    if not a.write:
        print("\nnothing written. Pass --write to apply, or --max-speed / "
              "--gap-metres to move a line.")
        store.close()
        return 0

    kept_a_copy(store, "jumps")
    # A jump that was too fast is a speed break; a hole in the recording is
    # what a load screen looks like from here, which is break code 3.
    by_code: dict = {}
    for r, _d, _dt, _speed, why in hits:
        # A death and a hole in the recording are both load screens as far as
        # the line is concerned; only the speed rule means something else.
        code = BREAK_SPEED if why == "too fast to walk" else BREAK_RELOAD
        by_code.setdefault(code, []).append(r["id"])
    for code, ids in by_code.items():
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            store.db.execute(
                f"UPDATE samples SET break_before = ? WHERE id IN ({marks})",
                [code, *chunk],
            )
    store.commit()
    # Where the marks will be, which is a question about the samples rather
    # than about which pass found them: the gate rule finds jumps on both
    # planes, so "out in the world" cannot be read off the rule.
    outside = sum(1 for h in hits if h[0]["wx"] is not None)
    print(f"\nmarked {len(hits)} jump(s), {outside} out in the world and "
          f"{len(hits) - outside} inside a dungeon.\nThe line breaks there "
          f"now and each one gets a teleport mark on the map or on\nthat "
          f"dungeon's own path; reload the viewer to see it. Every teleport "
          f"mark\noffers 'This was a death', on the map and during playback.")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Single-database storage for the whole route history.

One file, forever. Sessions are a column, not a filename, so the default view
is your entire path and session filtering is opt-in. WAL mode is what makes
that safe -- a hard game crash mid-write can't corrupt the database the way it
can corrupt an open JSON file, which is the real reason tools end up writing
one file per session.
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .coords import is_no_map

# Why a line breaks before a sample, as stored in samples.break_before. Kept
# here rather than in the sampler because reading them back is what they are
# for: a load screen is the difference between a door and a teleporter, and
# only the samples remember which happened.
BREAK_MAP = 1       # moved to a map that is not part of the same world
BREAK_SPEED = 2     # covered more ground than anything can
BREAK_RELOAD = 3    # a load screen came between: death, warp, rest

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ms  INTEGER NOT NULL,
    ended_ms    INTEGER,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    ts_ms       INTEGER NOT NULL,
    map_id      INTEGER NOT NULL,
    layer       TEXT    NOT NULL,
    x           REAL    NOT NULL,   -- chunk-local
    y           REAL    NOT NULL,
    z           REAL    NOT NULL,
    wx          REAL,               -- flat world metres, NULL for interiors
    wz          REAL,
    -- 0 = joined to the previous sample. Non-zero means the line breaks
    -- here, and says why: 1 the map changed, 2 the implied speed was
    -- impossible, 3 a load screen came between (a death, a warp, a rest).
    -- The viewer only cares whether it is zero; the reason is what lets
    -- tools/repair_breaks.py tell a stale tile-crossing break from a real one.
    break_before INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts_ms);
CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id);
CREATE INDEX IF NOT EXISTS idx_samples_layer ON samples(layer);

-- Discrete "entered/left this map" events, written by the sampler rather than
-- inferred by the viewer. Doubles as an exploration log.
CREATE TABLE IF NOT EXISTS map_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    ts_ms       INTEGER NOT NULL,
    map_id      INTEGER NOT NULL,
    layer       TEXT    NOT NULL,
    kind        TEXT    NOT NULL,   -- 'enter' | 'leave'
    -- last known surface position before entering, for placing the marker
    anchor_wx   REAL,
    anchor_wz   REAL,
    -- Where it happened in the map's own coordinates. Only interesting for
    -- deaths inside a dungeon: the anchor puts the mark at the entrance, and
    -- this puts it where you actually fell once that dungeon is drawn.
    local_x     REAL,
    local_z     REAL,
    -- 1 when marking this death also broke the line at the sample after it,
    -- so taking the death back can put that sample the way it was. Only ever
    -- set when the break was 0 to begin with: a real load screen is not ours
    -- to undo.
    broke_next  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON map_events(ts_ms);

-- Where a dungeon is, when you have said so yourself by dragging its marker.
-- Some places can never be worked out from a route: a Divine Tower reached
-- through Stormveil and left the same way never touches the surface, so
-- nothing recorded says where it is. You know, though.
-- What you call a place. The area labels in config.toml can only say what
-- kind of thing a map is -- every one of them is "Cave" -- and the map ID is
-- the only thing telling two caves apart, which is no use for remembering
-- which one had the bears in it.
CREATE TABLE IF NOT EXISTS map_names (
    map_id   INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    set_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS map_places (
    map_id   INTEGER PRIMARY KEY,
    wx       REAL NOT NULL,
    wz       REAL NOT NULL,
    set_ms   INTEGER NOT NULL,
    -- Not a position at all: somewhere that is nowhere in the world. The
    -- Roundtable Hold has no way in on foot, so every tier that works a
    -- position out from the route is guessing about it -- and clearing the
    -- hand placement does not help, because the inference then simply guesses
    -- again. Saying so is a decision, and it lives in the same table as the
    -- other decision about where a map is.
    nowhere  INTEGER NOT NULL DEFAULT 0
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One database, several tools: the recorder writes while the viewer
        # reads and an import may be running beside both. WAL allows that, but
        # a writer still has to wait its turn -- without a timeout, "database
        # is locked" comes back instantly instead of after the other writer's
        # next commit, which is a fraction of a second away.
        self.db = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30.0
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        # Columns added after the first release. CREATE TABLE IF NOT EXISTS
        # leaves an existing table alone, so a database made by an older
        # version would be missing them.
        have = {r[1] for r in self.db.execute("PRAGMA table_info(map_events)")}
        for col in ("local_x", "local_z"):
            if col not in have:
                self.db.execute(f"ALTER TABLE map_events ADD COLUMN {col} REAL")
        if "broke_next" not in have:
            self.db.execute("ALTER TABLE map_events ADD COLUMN broke_next "
                            "INTEGER NOT NULL DEFAULT 0")
        places = {r[1] for r in self.db.execute("PRAGMA table_info(map_places)")}
        if "nowhere" not in places:
            self.db.execute("ALTER TABLE map_places ADD COLUMN nowhere "
                            "INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    # -- writing ---------------------------------------------------------

    def start_session(
        self, note: Optional[str] = None, started_ms: Optional[int] = None
    ) -> int:
        # started_ms exists for imports: a route recorded last August should
        # appear in the session list under its own date, not the date it was
        # imported.
        cur = self.db.execute(
            "INSERT INTO sessions (started_ms, note) VALUES (?, ?)",
            (started_ms if started_ms is not None else now_ms(), note),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def end_session(self, session_id: int, ended_ms: Optional[int] = None) -> None:
        self.db.execute(
            "UPDATE sessions SET ended_ms = ? WHERE id = ?",
            (ended_ms if ended_ms is not None else now_ms(), session_id),
        )
        self.db.commit()

    def add_sample(self, **kw) -> int:
        cur = self.db.execute(
            """INSERT INTO samples
               (session_id, ts_ms, map_id, layer, x, y, z, wx, wz, break_before)
               VALUES (:session_id, :ts_ms, :map_id, :layer, :x, :y, :z,
                       :wx, :wz, :break_before)""",
            kw,
        )
        return int(cur.lastrowid)

    def add_map_event(self, **kw) -> None:
        kw.setdefault("local_x", None)
        kw.setdefault("local_z", None)
        kw.setdefault("broke_next", 0)
        self.db.execute(
            """INSERT INTO map_events
               (session_id, ts_ms, map_id, layer, kind, anchor_wx, anchor_wz,
                local_x, local_z, broke_next)
               VALUES (:session_id, :ts_ms, :map_id, :layer, :kind,
                       :anchor_wx, :anchor_wz, :local_x, :local_z,
                       :broke_next)""",
            kw,
        )

    def commit(self) -> None:
        self.db.commit()

    # -- reading ---------------------------------------------------------

    def sessions(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT s.id, s.started_ms, s.ended_ms, s.note,
                      COUNT(p.id) AS samples
               FROM sessions s LEFT JOIN samples p ON p.session_id = s.id
               GROUP BY s.id ORDER BY s.started_ms"""
        ).fetchall()
        return [dict(r) for r in rows]

    def bounds(self) -> Optional[dict]:
        r = self.db.execute(
            """SELECT MIN(wx) x0, MAX(wx) x1, MIN(wz) z0, MAX(wz) z1,
                      MIN(ts_ms) t0, MAX(ts_ms) t1, COUNT(*) n
               FROM samples WHERE wx IS NOT NULL"""
        ).fetchone()
        return dict(r) if r and r["n"] else None

    # A gap longer than this is the game paused, the map menu, a meal. Counted
    # in full it would make "time recorded" mean "how long the recorder was
    # left running", which is a different and much less interesting number.
    ACTIVE_GAP_CAP_MS = 10_000

    def stats(self, legacy_areas: Iterable[int] = ()) -> dict:
        """What the route adds up to.

        One pass over the samples in order. Distance skips every break --
        a teleport is not ground you covered, and counting it would put the
        Roundtable Hold's warps into the total as kilometres walked. Inside a
        dungeon the local coordinates are metres like any other, so those
        count too, in their own frame.

        The three planes are kept apart rather than summed on the way in.
        They are measured differently -- two of them from a world position and
        the third from a dungeon's own metres -- so the split is the honest
        thing to report, and the total is their sum.

        Session lengths use the same capped clock as the total: a session left
        running overnight would otherwise be the longest one you ever played.
        """
        surface = 0.0
        under = 0.0
        inside = 0.0
        active_ms = 0
        per_session: dict[int, int] = {}
        prev = None
        for r in self.db.execute(
            "SELECT session_id, ts_ms, map_id, layer, x, z, wx, wz, "
            "       break_before FROM samples ORDER BY session_id, ts_ms"
        ):
            if prev is not None and prev["session_id"] == r["session_id"]:
                gap = r["ts_ms"] - prev["ts_ms"]
                if 0 < gap <= self.ACTIVE_GAP_CAP_MS:
                    active_ms += gap
                    per_session[r["session_id"]] = (
                        per_session.get(r["session_id"], 0) + gap)
                if not r["break_before"]:
                    if r["wx"] is not None and prev["wx"] is not None:
                        step = math.dist((r["wx"], r["wz"]),
                                         (prev["wx"], prev["wz"]))
                        if r["layer"] == "underground":
                            under += step
                        else:
                            surface += step
                    elif (r["wx"] is None and prev["wx"] is None
                            and r["map_id"] == prev["map_id"]):
                        inside += math.dist((r["x"], r["z"]),
                                            (prev["x"], prev["z"]))
            prev = r

        # A session that recorded no time at all is not a short session, it is
        # a session that did not happen: the four one-sample rows a port clash
        # left behind would drag an average of real afternoons down by a third.
        played = sorted(per_session.values())

        # Legacy dungeons are counted apart from caves because they are a
        # different kind of thing -- Stormveil is on the map and drawn there,
        # a cave is a hole in the ground -- and one castle among six catacombs
        # makes "caves and dungeons" mean less, not more.
        legacy = tuple(sorted(set(legacy_areas)))
        holes = ",".join(str(int(a)) for a in legacy) or "-1"
        one = self.db.execute(
            "SELECT COUNT(*) samples, COUNT(DISTINCT session_id) sessions, "
            "  MIN(ts_ms) first_ms, MAX(ts_ms) last_ms, "
            "  COUNT(DISTINCT CASE WHEN layer = 'interior' "
            f"    AND ((map_id >> 24) & 255) NOT IN ({holes}) "
            "    THEN map_id END) dungeons, "
            "  COUNT(DISTINCT CASE WHEN layer = 'interior' "
            f"    AND ((map_id >> 24) & 255) IN ({holes}) "
            "    THEN map_id END) legacy "
            "FROM samples"
        ).fetchone()
        deaths = self.db.execute(
            "SELECT COUNT(*) n FROM map_events "
            "WHERE kind IN ('death', 'death_by_hand')"
        ).fetchone()["n"]
        # The same list the map draws marks from, rather than a count of
        # break flags: those include every door you walked through, and a
        # panel saying 190 teleports beside a map showing 74 is worse than
        # either number alone.
        warps = len(self.warps())
        hours = active_ms / 3_600_000
        return {
            "travelled_m": round(surface + under + inside),
            "overworld_m": round(surface),
            "underground_m": round(under),
            "inside_m": round(inside),
            "active_ms": active_ms,
            "session_mean_ms": round(sum(played) / len(played)) if played else 0,
            "session_max_ms": played[-1] if played else 0,
            "sessions_played": len(played),
            "samples": one["samples"],
            "sessions": one["sessions"],
            "dungeons": one["dungeons"],
            "legacy": one["legacy"],
            "deaths": deaths,
            "jumps": warps,
            "deaths_per_hour": round(deaths / hours, 1) if hours > 0.1 else None,
            "first_ms": one["first_ms"],
            "last_ms": one["last_ms"],
        }

    def time_quantiles(self, steps: int = 100) -> list[int]:
        """Timestamps that split the samples into equal-sized groups.

        Recording is not spread evenly over time -- an afternoon in August and
        then a month of nothing -- so anything that treats time linearly puts
        almost every sample in the last sliver: colouring by age came out one
        colour, and the time slider did nothing until the very end of its
        travel. These are the positions where equal numbers of samples fall,
        which is what makes both of them move evenly.
        """
        rows = self.db.execute(
            f"""SELECT MIN(ts_ms) AS edge FROM (
                    SELECT ts_ms, NTILE({int(steps)}) OVER (ORDER BY ts_ms) AS bucket
                    FROM samples WHERE wx IS NOT NULL
                ) GROUP BY bucket ORDER BY bucket"""
        ).fetchall()
        edges = [int(r["edge"]) for r in rows if r["edge"] is not None]
        if not edges:
            return []
        last = self.db.execute(
            "SELECT MAX(ts_ms) AS t FROM samples WHERE wx IS NOT NULL"
        ).fetchone()["t"]
        return edges + [int(last)]

    def iter_samples(
        self,
        layers: Optional[Iterable[str]] = None,
        session_ids: Optional[Iterable[int]] = None,
        t0: Optional[int] = None,
        t1: Optional[int] = None,
    ) -> Iterator[sqlite3.Row]:
        sql = ["SELECT ts_ms, session_id, map_id, layer, y, wx, wz, break_before",
               "FROM samples WHERE wx IS NOT NULL"]
        args: list = []
        if layers:
            layers = list(layers)
            sql.append(f"AND layer IN ({','.join('?' * len(layers))})")
            args += layers
        if session_ids:
            session_ids = list(session_ids)
            sql.append(f"AND session_id IN ({','.join('?' * len(session_ids))})")
            args += session_ids
        if t0 is not None:
            sql.append("AND ts_ms >= ?")
            args.append(t0)
        if t1 is not None:
            sql.append("AND ts_ms <= ?")
            args.append(t1)
        sql.append("ORDER BY ts_ms")
        yield from self.db.execute(" ".join(sql), args)

    def delete_session(self, session_id: int) -> Optional[dict]:
        """Remove one session and everything recorded under it.

        Irreversible: there is no undo and no soft delete, which is why the
        viewer names the session and its size before it calls this. Returns
        None when there is no such session, so the caller can say so rather
        than report having successfully deleted nothing.
        """
        row = self.db.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        # One transaction: a half-deleted session would leave samples pointing
        # at a session row that no longer exists, and those are invisible to
        # the session list while still being drawn on the map.
        with self.db:
            samples = self.db.execute(
                "DELETE FROM samples WHERE session_id = ?", (session_id,)
            ).rowcount
            events = self.db.execute(
                "DELETE FROM map_events WHERE session_id = ?", (session_id,)
            ).rowcount
            self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return {"samples": samples, "events": events}

    def interior_path(
        self, map_id: int, t0: int, t1: Optional[int] = None
    ) -> list[sqlite3.Row]:
        """One visit's path inside a discrete map, in that map's local axes.

        Interiors are stored with a NULL world position on purpose, so they
        can never be reached by the world-plane queries. That also means the
        only handle on them is map + time window, which is what a visit from
        interior_visits() provides.
        """
        sql = ["SELECT ts_ms, x, y, z, break_before FROM samples",
               "WHERE wx IS NULL AND map_id = ? AND ts_ms >= ?"]
        args: list = [map_id, t0]
        if t1 is not None:
            sql.append("AND ts_ms <= ?")
            args.append(t1)
        sql.append("ORDER BY ts_ms")
        return self.db.execute(" ".join(sql), args).fetchall()

    def warps(self, min_m: float = 50.0) -> list[dict]:
        """Jumps the line refused to draw: where you left and where you landed.

        Read back out of the samples rather than logged separately, because
        the break flag already records both that it happened and why (2 an
        impossible speed, 3 a load screen). Anything shorter than min_m is not
        a journey worth marking -- resting at a grace reloads the area and
        puts you back where you were standing.
        """
        rows = self.db.execute(
            """SELECT a.ts_ms, a.map_id, a.break_before, a.wx, a.wz, a.layer,
                      a.x, a.z,
                      b.ts_ms AS from_ts, b.map_id AS from_map,
                      b.wx AS from_wx, b.wz AS from_wz,
                      b.x AS from_x, b.z AS from_z
               FROM samples a JOIN samples b ON b.id = a.id - 1
               -- Code 1 as well as 2 and 3. A tile crossing is a map change
               -- and so is a warp between two places that happen to be in
               -- different tiles, and the sampler cannot tell which -- so
               -- nine real jumps of 214 to 775 m were broken as map changes
               -- and then dropped here, appearing on the map as nothing at
               -- all. What separates them is the distance, and it separates
               -- them completely: of the twelve map-change breaks with a
               -- world position at both ends, nine are 214 m or more and
               -- three are 5 m or less, with nothing in between. The min_m
               -- filter below already sits in that band.
               -- No condition on the coordinate spaces. Requiring both
               -- ends in one of them dropped every jump that crosses between
               -- them -- which is what a sending gate is: you are standing in
               -- Limgrave, a load screen, and you are inside a dungeon
               -- kilometres away. On routes.db that was 24 real jumps drawn
               -- as nothing at all, the longest 4,735 m. The loop below
               -- measures each end where it can, using the dungeon's own
               -- place on the map for an end that is inside one.
               WHERE a.break_before IN (1, 2, 3)
                 AND a.session_id = b.session_id
               ORDER BY a.ts_ms"""
        ).fetchall()
        # A respawn is a warp too, but it already has a death mark on it and
        # two icons on one spot say less than one.
        deaths = [d["ts_ms"] for d in self.deaths()]
        anchors = None      # built once, and only if a crossing turns up
        out = []
        for r in rows:
            # A lift or a teleporter within one dungeon: both ends are in that
            # map's own metres, which is where it is drawn.
            inside = (r["wx"] is None and r["from_wx"] is None
                      and r["map_id"] == r["from_map"])
            row = dict(r)
            if inside:
                d = ((r["x"] - r["from_x"]) ** 2
                     + (r["z"] - r["from_z"]) ** 2) ** 0.5
            elif r["wx"] is not None and r["from_wx"] is not None:
                d = ((r["wx"] - r["from_wx"]) ** 2
                     + (r["wz"] - r["from_wz"]) ** 2) ** 0.5
            else:
                # One end inside a dungeon and the other somewhere else. The
                # two are in different coordinate spaces, so the dungeon's own
                # place on the map stands in for the end that is inside it --
                # the same position its marker is drawn at. Where a dungeon
                # has no place on the map there is nothing to draw an arc to,
                # and it is left alone rather than guessed at.
                if anchors is None:
                    anchors = self._dungeon_places()
                to = self._end_place(r["wx"], r["wz"], r["map_id"], anchors)
                frm = self._end_place(r["from_wx"], r["from_wz"],
                                      r["from_map"], anchors)
                if to is None or frm is None:
                    continue
                d = ((to[0] - frm[0]) ** 2 + (to[1] - frm[1]) ** 2) ** 0.5
                # Filled in so this draws as an ordinary jump between two
                # places on the map, which is what it is.
                row["wx"], row["wz"] = to[0], to[1]
                row["from_wx"], row["from_wz"] = frm[0], frm[1]
                # The plane the mark belongs on: the dungeon's, when that is
                # the end you arrived at, and otherwise the arrival's own.
                row["layer"] = to[2] or r["layer"]
            if d < min_m:
                continue
            if any(r["from_ts"] - 30_000 <= t <= r["ts_ms"] for t in deaths):
                continue
            out.append({**row, "distance_m": d, "inside": inside})
        # A stay whose own steps already showed the jump needs no second mark
        # across it: the transit is for the case where no single pair of
        # samples shows one.
        out += self._transits(min_m, deaths, [w["ts_ms"] for w in out])
        out.sort(key=lambda w: w["ts_ms"])
        return out

    def _dungeon_places(self) -> dict:
        """Where each dungeon is drawn, for measuring a jump into one."""
        places = {}
        for v in self.interior_visits():
            if v["wx"] is not None:
                places.setdefault(v["map_id"], (v["wx"], v["wz"], v["plane"]))
        return places

    @staticmethod
    def _end_place(wx, wz, map_id, places):
        if wx is not None:
            return (wx, wz, None)
        return places.get(map_id)

    def _transits(self, min_m: float, deaths: list[int],
                  already: list[int] | None = None) -> list[dict]:
        """Jumps that happen across a dungeon rather than in one step.

        Walk into a tunnel and out of its far mouth and the surface path
        breaks by a couple of hundred metres, but no single pair of samples
        shows it: the two rows either side of the hole are in different
        coordinate spaces, so the query above -- which needs both ends in the
        same space -- cannot see it at all. On `routes.db` fifteen jumps of
        77 m to 1,911 m were invisible for that reason, including one through
        a tunnel that came out 192 m away with nothing on the map to say so.

        The threshold is the same 50 m, and the data leaves room for it: of
        the forty dungeon stays with a surface position at both ends,
        twenty-five come out within 25.6 m of where they went in and fifteen
        come out 77 m or more, with nothing between.
        """
        rows = self.db.execute(
            "SELECT id, session_id, ts_ms, map_id, layer, x, z, wx, wz "
            "FROM samples ORDER BY session_id, ts_ms"
        ).fetchall()
        out: list[dict] = []
        i = 0
        while i < len(rows):
            if rows[i]["wx"] is not None:
                i += 1
                continue
            j = i
            while j < len(rows) and rows[j]["wx"] is None:
                j += 1
            before = rows[i - 1] if i > 0 else None
            after = rows[j] if j < len(rows) else None
            i = j
            if (before is None or after is None
                    or before["wx"] is None or after["wx"] is None
                    or before["session_id"] != after["session_id"]):
                continue
            # The no-map sentinel is not a dungeon. Early sessions stored a
            # few of those before the sampler learned to drop them.
            if is_no_map(rows[j - 1]["map_id"]):
                continue
            d = ((after["wx"] - before["wx"]) ** 2
                 + (after["wz"] - before["wz"]) ** 2) ** 0.5
            if d < min_m:
                continue
            if any(before["ts_ms"] - 30_000 <= t <= after["ts_ms"]
                   for t in deaths):
                continue
            # A gate into a dungeon is reported where it happens, so reporting
            # the whole stay as a jump as well would draw the same journey
            # twice -- and worse, as one arc from where you were to where you
            # came out, which is a teleport and a walk drawn as a teleport.
            if already and any(before["ts_ms"] <= t <= after["ts_ms"]
                               for t in already):
                continue
            out.append({
                "ts_ms": after["ts_ms"], "map_id": after["map_id"],
                # Not a load screen and not a speed: the line broke because
                # you were somewhere else in between. Its own code so the
                # reason can say so.
                "break_before": 4,
                "wx": after["wx"], "wz": after["wz"], "layer": after["layer"],
                "x": after["x"], "z": after["z"],
                "from_ts": before["ts_ms"], "from_map": before["map_id"],
                "from_wx": before["wx"], "from_wz": before["wz"],
                "from_x": before["x"], "from_z": before["z"],
                "distance_m": d, "inside": False,
            })
        return out

    # Long enough for "Groveside Cave, the one with the bears", short enough
    # that it cannot be used to hide a novel in the database.
    MAX_NAME = 80
    _names = None          # dropped on every write, rebuilt on the next read

    def set_name(self, map_id: int, name: str) -> str:
        """Give a map a name of your own. Returns what was actually stored."""
        clean = " ".join(str(name).split())[: self.MAX_NAME]
        if not clean:
            self.clear_name(map_id)
            return ""
        self.db.execute(
            "INSERT INTO map_names (map_id, name, set_ms) VALUES (?,?,?) "
            "ON CONFLICT(map_id) DO UPDATE SET name=excluded.name, "
            "set_ms=excluded.set_ms",
            (int(map_id), clean, now_ms()),
        )
        self._names = None
        self.db.commit()
        return clean

    def clear_name(self, map_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM map_names WHERE map_id = ?", (int(map_id),)
        )
        self._names = None
        self.db.commit()
        return cur.rowcount > 0

    def names(self) -> dict:
        """Every name you have given, cached.

        Read once per label and there are dozens of labels in a single
        /api/interiors, so this would otherwise be a query per marker, per
        death, per teleport. Written rarely enough that dropping the cache on
        a write is the whole of the invalidation.
        """
        if self._names is None:
            self._names = {
                r["map_id"]: r["name"]
                for r in self.db.execute("SELECT map_id, name FROM map_names")
            }
        return self._names

    def set_place(self, map_id: int, wx: float, wz: float) -> None:
        """Record where a map really is. Overrides anything inferred."""
        self.db.execute(
            "INSERT INTO map_places (map_id, wx, wz, set_ms, nowhere) "
            "VALUES (?,?,?,?,0) "
            "ON CONFLICT(map_id) DO UPDATE SET wx=excluded.wx, wz=excluded.wz, "
            "set_ms=excluded.set_ms, nowhere=0",
            (int(map_id), float(wx), float(wz), now_ms()),
        )
        self.db.commit()

    def set_nowhere(self, map_id: int) -> None:
        """Record that a map is not anywhere in the world.

        Which is a different thing from not knowing where it is. Clearing a
        hand placement only sends the question back to the tiers, and on
        `routes.db` they answer it: the Hold gets an `exit` anchor from the one
        time leaving it did not look like a warp, 3 km from where it would be
        if it were anywhere. Nought is stored for the coordinates because the
        columns are NOT NULL and nothing reads them for these rows.
        """
        self.db.execute(
            "INSERT INTO map_places (map_id, wx, wz, set_ms, nowhere) "
            "VALUES (?,0,0,?,1) "
            "ON CONFLICT(map_id) DO UPDATE SET wx=0, wz=0, "
            "set_ms=excluded.set_ms, nowhere=1",
            (int(map_id), now_ms()),
        )
        self.db.commit()

    def clear_place(self, map_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM map_places WHERE map_id = ?", (int(map_id),)
        )
        self.db.commit()
        return cur.rowcount > 0

    def places(self) -> dict:
        return {
            r["map_id"]: (r["wx"], r["wz"])
            for r in self.db.execute(
                "SELECT map_id, wx, wz FROM map_places WHERE nowhere = 0")
        }

    def nowhere(self) -> set:
        """Maps you have said are not anywhere in the world."""
        return {
            r["map_id"]
            for r in self.db.execute(
                "SELECT map_id FROM map_places WHERE nowhere = 1")
        }

    # How long after dying a load screen still counts as getting up from that
    # death. Measured on routes.db: every respawn lands 11 to 15 seconds
    # later. Three minutes is generous enough for a slow load and short
    # enough that quitting to the menu and coming back tomorrow is not
    # mistaken for one.
    RESPAWN_WINDOW_MS = 3 * 60 * 1000

    def respawn_after(self, ts_ms: int) -> Optional[dict]:
        """Where you got up, for a death at ts_ms.

        A respawn needs no column of its own: the samples already say it
        happened. Dying is followed by a load screen and the first reading
        after one carries break code 3, so the first such sample after the
        death is the grace you appeared at. Only the first -- resting at that
        grace later reloads too.

        Speed counts as well as a load screen, because a death marked by hand
        on a jump the repair pass found by speed still put you somewhere. A
        map change does not: walking through a door is code 1, and reading
        that as a respawn turned one death on the surface into a respawn
        inside the catacomb it was standing next to.

        None when there is no such sample in the window, which happens: a
        death recorded as you crossed into a dungeon has samples running
        straight on afterwards, and inventing a grace for it would be worse
        than leaving it unmarked.
        """
        r = self.db.execute(
            "SELECT ts_ms, map_id, layer, x, z, wx, wz FROM samples "
            "WHERE ts_ms > ? AND ts_ms <= ? AND break_before IN (?, ?) "
            "ORDER BY ts_ms LIMIT 1",
            (ts_ms, ts_ms + self.RESPAWN_WINDOW_MS, BREAK_SPEED, BREAK_RELOAD),
        ).fetchone()
        if r is None:
            return None
        return {
            "ts_ms": r["ts_ms"],
            "map_id": r["map_id"],
            "layer": r["layer"],
            "wx": r["wx"],
            "wz": r["wz"],
            "local_x": r["x"],
            "local_z": r["z"],
            "after_s": round((r["ts_ms"] - ts_ms) / 1000, 1),
        }

    def deaths(self) -> list[dict]:
        """Where and when HP hit zero, and where you got up afterwards.

        Stored as map_events rather than a table of their own: a death is an
        event at a place and a time, which is exactly what that table holds,
        and interior_visits() only looks at enter/leave so these pass it by.
        """
        rows = self.db.execute(
            "SELECT ts_ms, map_id, layer, anchor_wx, anchor_wz, local_x, "
            "local_z, kind FROM map_events "
            "WHERE kind IN ('death', 'death_by_hand') ORDER BY ts_ms"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Its own kind rather than a flag on the same one, so a death you
            # marked can be taken back without touching a death the HP
            # reading found, and so the popup can say which it is looking at.
            d["by_hand"] = r["kind"] == "death_by_hand"
            d.pop("kind", None)
            d["respawn"] = self.respawn_after(r["ts_ms"])
            out.append(d)
        return out

    def _claim_grace(self, sample_id: int, break_before: int) -> int:
        """Make this sample readable as the grace you got up at.

        `respawn_after()` takes break codes 2 and 3 and refuses 1, because
        walking through a door is a map change too and reading that as a
        respawn moved a death on the surface into the catacomb beside it. But
        when you have just said "I died here", the map change on the sample
        after is not a door -- it is the load screen that put you somewhere
        else, and it happens to have changed the map on the way. So a code 1
        is taken over; a 2 or 3 is already the grace and is left alone.

        Returns what to store in `broke_next`: the break to put back, plus
        one, with 0 meaning nothing was changed.
        """
        if break_before not in (0, BREAK_MAP):
            return 0
        self.db.execute("UPDATE samples SET break_before = ? WHERE id = ?",
                        (BREAK_RELOAD, sample_id))
        return break_before + 1

    def mark_death(self, ts_ms: int) -> Optional[dict]:
        """Say that the jump arriving at ts_ms was a death.

        Without the HP reading a death and a teleporter are the same event:
        a hole in the recording with a position either side. The repair pass
        cannot tell them apart and does not pretend to, so this is where you
        say which it was. The death goes at the step *before* the jump --
        where you went down -- and the respawn falls out of the machinery
        that was already there, since the arrival is the first sample after
        it that was not walked to.
        """
        r = self.db.execute(
            "SELECT a.id, a.session_id, a.break_before AS abreak, "
            "       b.ts_ms AS bts, b.map_id AS bmap, "
            "       b.layer AS blayer, b.x AS bx, b.z AS bz, "
            "       b.wx AS bwx, b.wz AS bwz "
            "FROM samples a JOIN samples b ON b.id = a.id - 1 "
            "WHERE a.ts_ms = ? AND a.session_id = b.session_id",
            (int(ts_ms),),
        ).fetchone()
        if r is None:
            return None
        if self.db.execute(
            "SELECT 1 FROM map_events WHERE ts_ms = ? AND kind = 'death_by_hand'",
            (r["bts"],),
        ).fetchone():
            return {"ts_ms": r["bts"], "already": True}
        # The arrival is the grace. It is already broken -- that is how the
        # jump was found -- but a jump broken as a map change is one
        # `respawn_after()` will not read, so it is claimed the same way.
        broke = self._claim_grace(r["id"], r["abreak"])
        self.add_map_event(
            session_id=r["session_id"],
            ts_ms=r["bts"],
            map_id=r["bmap"],
            layer=r["blayer"],
            kind="death_by_hand",
            anchor_wx=r["bwx"],
            anchor_wz=r["bwz"],
            local_x=r["bx"],
            local_z=r["bz"],
            broke_next=broke,
        )
        self.db.commit()
        return {"ts_ms": r["bts"], "already": False,
                "respawn_ts": int(ts_ms)}

    def mark_death_at(self, ts_ms: int, window_ms: int = 60_000) -> Optional[dict]:
        """Say that you died at roughly this moment.

        `mark_death()` needs a jump to hang the death on, which is fine when
        the recorder noticed something and wrong when it noticed nothing at
        all. A death in a cave whose grace is seven metres away leaves a hole
        in the recording and no displacement to find it by, so no rule will
        ever produce a mark to correct -- and then there is nothing to click.
        This takes the moment instead, and puts the death on the sample
        nearest it.
        """
        r = self.db.execute(
            "SELECT id, session_id, ts_ms, map_id, layer, x, z, wx, wz "
            "FROM samples WHERE ts_ms BETWEEN ? AND ? "
            "ORDER BY ABS(ts_ms - ?) LIMIT 1",
            (int(ts_ms) - window_ms, int(ts_ms) + window_ms, int(ts_ms)),
        ).fetchone()
        if r is None:
            return None
        if self.db.execute(
            "SELECT 1 FROM map_events WHERE ts_ms = ? AND kind = 'death_by_hand'",
            (r["ts_ms"],),
        ).fetchone():
            return {"ts_ms": r["ts_ms"], "already": True}

        # And the next sample is where you got up. Written as a break rather
        # than as an event of its own, which does three jobs with one row:
        # `respawn_after()` already reads the first broken sample after a
        # death as the grace, so the respawn mark falls out with no new kind
        # to teach anything about; `warps()` already drops jumps near a death,
        # so it does not also become a teleport; and the line stops being
        # drawn from the body to the grace, which is the thing that made these
        # look like a walk in the first place.
        #
        # A map change is taken over, because it is not an answer to the
        # question. Die on the way through a door and the sample after
        # carries code 1: the line already breaks, so nothing looks wrong,
        # but `respawn_after()` skips code 1 on purpose -- walking through a
        # door is a break too -- and goes hunting for the next load screen,
        # which on `routes.db` was 73 seconds and a teleport away. So the
        # grace ended up somewhere that had nothing to do with the death.
        #
        # A code 2 or 3 already there is left alone: `respawn_after()` takes
        # both, so it is already the grace, and overwriting it would be
        # inventing history rather than reading it.
        #
        # `broke_next` is the break to put back, plus one -- 0 meaning we
        # changed nothing. The old rows that stored a plain 1 meant "we set
        # it from 0", which is what 1 means here too.
        nxt = self.db.execute(
            "SELECT id, ts_ms, break_before FROM samples "
            "WHERE session_id = ? AND ts_ms > ? ORDER BY ts_ms LIMIT 1",
            (r["session_id"], r["ts_ms"]),
        ).fetchone()
        broke = 0
        if nxt is not None:
            broke = self._claim_grace(nxt["id"], nxt["break_before"])
        self.add_map_event(
            session_id=r["session_id"],
            ts_ms=r["ts_ms"],
            map_id=r["map_id"],
            layer=r["layer"],
            kind="death_by_hand",
            anchor_wx=r["wx"],
            anchor_wz=r["wz"],
            local_x=r["x"],
            local_z=r["z"],
            broke_next=broke,
        )
        self.db.commit()
        return {"ts_ms": r["ts_ms"], "already": False,
                "respawn_ts": nxt["ts_ms"] if nxt is not None else None}

    def clear_death(self, ts_ms: int) -> bool:
        """Take back a death you marked. Only ever removes your own."""
        row = self.db.execute(
            "SELECT session_id, broke_next FROM map_events "
            "WHERE ts_ms = ? AND kind = 'death_by_hand'", (int(ts_ms),),
        ).fetchone()
        if row is None:
            return False
        # Put the line back together, but only where marking the death is what
        # took it apart.
        if row["broke_next"]:
            nxt = self.db.execute(
                "SELECT id FROM samples WHERE session_id = ? AND ts_ms > ? "
                "ORDER BY ts_ms LIMIT 1", (row["session_id"], int(ts_ms)),
            ).fetchone()
            if nxt is not None:
                self.db.execute(
                    "UPDATE samples SET break_before = ? WHERE id = ?",
                    (row["broke_next"] - 1, nxt["id"]))
        cur = self.db.execute(
            "DELETE FROM map_events WHERE ts_ms = ? AND kind = 'death_by_hand'",
            (int(ts_ms),),
        )
        self.db.commit()
        return cur.rowcount > 0

    def lost_graces(self) -> list[dict]:
        """Deaths you marked whose grace the map change swallowed.

        Marking a death used to leave a map-change break alone, and
        `respawn_after()` will not read one -- so the grace was either the
        wrong sample, minutes away, or missing altogether. Returned rather
        than fixed, because writing to samples is something a tool should
        say out loud first.
        """
        out = []
        for e in self.db.execute(
            "SELECT ts_ms, session_id, broke_next FROM map_events "
            "WHERE kind = 'death_by_hand' ORDER BY ts_ms"
        ).fetchall():
            if e["broke_next"]:
                continue
            nxt = self.db.execute(
                "SELECT id, ts_ms, break_before FROM samples "
                "WHERE session_id = ? AND ts_ms > ? ORDER BY ts_ms LIMIT 1",
                (e["session_id"], e["ts_ms"]),
            ).fetchone()
            if nxt is None or nxt["break_before"] != BREAK_MAP:
                continue
            shown = self.respawn_after(e["ts_ms"])
            out.append({
                "ts_ms": e["ts_ms"], "sample_id": nxt["id"],
                "next_ts": nxt["ts_ms"],
                "shown_ts": shown["ts_ms"] if shown else None,
            })
        return out

    def claim_grace(self, ts_ms: int, sample_id: int) -> None:
        """Take over that map change, and remember that we did."""
        self.db.execute("UPDATE samples SET break_before = ? WHERE id = ?",
                        (BREAK_RELOAD, sample_id))
        self.db.execute(
            "UPDATE map_events SET broke_next = ? "
            "WHERE ts_ms = ? AND kind = 'death_by_hand'",
            (BREAK_MAP + 1, int(ts_ms)))
        self.db.commit()

    # A stay this short is not a visit. Every real stay in `routes.db` is at
    # least 34 samples over 17 seconds; these are one and two.
    GHOST_SAMPLES = 2

    def ghost_stays(self) -> list[dict]:
        """Readings taken during a load screen that name the map you just left.

        The position chain does not go blank while the game loads: it can go
        on reporting a map you were in earlier, with the position you were
        last at inside it. `is_no_map()` catches the honest version of that --
        0xFFFFFFFF -- but not this one, which looks like a perfectly ordinary
        dungeon reading and becomes a visit, a marker on the map, and a place
        for a death to be filed that you were nowhere near.

        The signature is all of: a load screen immediately before it, one or
        two samples, and a world position on both sides. On `routes.db` that
        is two stays, both naming maps the player had spent minutes in
        earlier, one of them a single reading lasting no time at all --
        against 58 real stays whose shortest is 34 samples over 17 seconds.
        """
        rows = self.db.execute(
            "SELECT id, session_id, ts_ms, map_id, layer, x, y, z, wx, wz, "
            "break_before FROM samples ORDER BY session_id, ts_ms"
        ).fetchall()
        out = []
        i = 0
        while i < len(rows):
            if rows[i]["layer"] != "interior":
                i += 1
                continue
            j = i
            while (j < len(rows)
                    and rows[j]["session_id"] == rows[i]["session_id"]
                    and rows[j]["layer"] == "interior"
                    and rows[j]["map_id"] == rows[i]["map_id"]):
                j += 1
            first, last, count = rows[i], rows[j - 1], j - i
            before = rows[i - 1] if i > 0 else None
            after = rows[j] if j < len(rows) else None
            i = j
            if count > self.GHOST_SAMPLES:
                continue
            if first["break_before"] != BREAK_RELOAD:
                continue
            if (before is None or after is None
                    or before["wx"] is None or after["wx"] is None
                    or before["session_id"] != first["session_id"]
                    or after["session_id"] != first["session_id"]):
                continue
            out.append({
                "map_id": first["map_id"],
                "session_id": first["session_id"],
                "from_ts": first["ts_ms"],
                "to_ts": last["ts_ms"],
                "samples": count,
                "seconds": (last["ts_ms"] - first["ts_ms"]) / 1000.0,
                "stood_at": (before["wx"], before["wz"]),
                "came_back_at": (after["wx"], after["wz"]),
                "apart_m": ((after["wx"] - before["wx"]) ** 2
                            + (after["wz"] - before["wz"]) ** 2) ** 0.5,
                "before_id": before["id"],
            })
        return out

    def drop_ghost_stay(self, ghost: dict) -> int:
        """Put those readings back where the player actually was.

        Not deleted: `warps()` pairs each sample with the row before it by id,
        and a hole there would lose the jump on either side of this. They are
        given the last position anybody knew about instead, which is the
        honest reading of a load screen -- you were last here -- and the same
        thing the rest of the file does when it anchors a dungeon to the
        surface position before it.

        The visit it invented goes with them, and so does any death filed on
        one of them, which is the point: a death marked during a load screen
        belongs where you were standing, not in a tunnel across the map.
        """
        prev = self.db.execute(
            "SELECT map_id, layer, x, y, z, wx, wz FROM samples WHERE id = ?",
            (ghost["before_id"],),
        ).fetchone()
        if prev is None:
            return 0
        cur = self.db.execute(
            "UPDATE samples SET map_id = ?, layer = ?, x = ?, y = ?, z = ?, "
            "wx = ?, wz = ? WHERE session_id = ? AND ts_ms BETWEEN ? AND ?",
            (prev["map_id"], prev["layer"], prev["x"], prev["y"], prev["z"],
             prev["wx"], prev["wz"], ghost["session_id"],
             ghost["from_ts"], ghost["to_ts"]),
        )
        self.db.execute(
            "DELETE FROM map_events WHERE kind IN ('enter', 'leave') "
            "AND map_id = ? AND ts_ms BETWEEN ? AND ?",
            (ghost["map_id"], ghost["from_ts"], ghost["to_ts"] + 1),
        )
        self.db.execute(
            "UPDATE map_events SET map_id = ?, layer = ?, anchor_wx = ?, "
            "anchor_wz = ?, local_x = ?, local_z = ? "
            "WHERE kind IN ('death', 'death_by_hand') "
            "AND ts_ms BETWEEN ? AND ?",
            (prev["map_id"], prev["layer"], prev["wx"], prev["wz"],
             prev["x"], prev["z"], ghost["from_ts"], ghost["to_ts"]),
        )
        self.db.commit()
        return cur.rowcount

    def interior_visits(self) -> list[dict]:
        """Aggregated cave/dungeon markers: where you went in, and how long for."""
        rows = self.db.execute(
            """SELECT map_id, layer, anchor_wx, anchor_wz, ts_ms, kind, session_id
               FROM map_events ORDER BY ts_ms"""
        ).fetchall()
        # How long after leaving a surface reading still counts as "that is
        # where the entrance was". Seconds, normally: you walk out and the
        # next sample is outside. Anything much later is a different trip, and
        # a marker placed from it would be confidently wrong.
        EXIT_WINDOW_MS = 5 * 60 * 1000

        def exit_anchor(ts_ms: int):
            """First surface position after leaving, for visits with no
            entrance recorded.

            A session that begins inside a dungeon -- or an imported route
            that does -- has no previous surface position to hang the marker
            on, so the visit would be stored but unreachable. *Walking* out
            puts you at the entrance, near enough for a marker, and the popup
            says so rather than implying it was measured going in.

            Warping out puts you anywhere at all, and that is the whole story
            of the Roundtable Hold: you can only ever warp in and warp out, so
            the first surface reading afterwards is whichever grace you chose
            next, and the Hold was drawn there -- a path in the middle of
            nowhere that moved every time. A load screen on that first reading
            is the tell, and it is the same one used everywhere else here. On
            routes.db 45 exits were walked and 7 warped, every warp with a
            zero-second gap.
            """
            r = self.db.execute(
                "SELECT wx, wz, break_before FROM samples WHERE wx IS NOT NULL "
                "AND layer = 'surface' AND ts_ms >= ? AND ts_ms <= ? "
                "ORDER BY ts_ms LIMIT 1",
                (ts_ms, ts_ms + EXIT_WINDOW_MS),
            ).fetchone()
            if r is None or r["break_before"] == BREAK_RELOAD:
                return None
            return (r["wx"], r["wz"])

        def arrived_by_load(map_id: int, at_or_after: int) -> bool:
            """Was this map reached across a load screen?

            Walking into a cave is seamless: the map ID changes and the
            samples run straight on, so the step before the change is a real
            position at the door. A transporter trap is a load screen, and
            after one of those nothing about where you just were says
            anything about where you are -- the chest is in a cave in Limgrave
            and it puts you in a tunnel in Caelid.

            Measured on routes.db: of 55 interior entries, every one walked
            into carries break code 1 on its first sample, and every warp,
            death and trap carries 3.
            """
            r = self.db.execute(
                "SELECT break_before FROM samples WHERE map_id = ? "
                "AND ts_ms >= ? ORDER BY ts_ms LIMIT 1",
                (map_id, at_or_after),
            ).fetchone()
            return r is not None and r["break_before"] == BREAK_RELOAD

        open_by_map: dict[int, dict] = {}
        out: list[dict] = []
        # What you were in when you went through the door. A Divine Tower
        # reached from inside Stormveil never touches the surface, so it has
        # no entrance of its own to be placed by -- but the castle it opens
        # off does, and that is near enough to draw it at.
        came_from: Optional[int] = None
        session = None
        for r in rows:
            # Only within one session: the last place you were in yesterday is
            # not the way into the first place you go today.
            if r["session_id"] != session:
                session = r["session_id"]
                came_from = None
            if is_no_map(r["map_id"]):
                # Databases recorded before load screens were filtered out
                # carry an enter/leave pair per loading screen. Skipping them
                # here means those routes stop sprouting phantom markers
                # without anyone having to edit their history.
                continue
            if r["kind"] == "enter":
                entry = dict(r)
                entry["came_from"] = came_from
                open_by_map[r["map_id"]] = entry
                came_from = r["map_id"]
            elif r["kind"] == "leave":
                start = open_by_map.pop(r["map_id"], None)
                if not start:
                    continue
                wx, wz = start["anchor_wx"], start["anchor_wz"]
                placed = "entrance"
                if wx is None:
                    found = exit_anchor(r["ts_ms"])
                    if found:
                        wx, wz = found
                        placed = "exit"
                # Kept even with no position: the path inside was recorded,
                # and a visit that cannot go on the map should still be
                # reachable rather than silently dropped.
                out.append(
                    {
                        "map_id": r["map_id"],
                        "layer": start["layer"],
                        "wx": wx,
                        "wz": wz,
                        "entered_ms": start["ts_ms"],
                        "duration_ms": r["ts_ms"] - start["ts_ms"],
                        "placed": placed if wx is not None else "unknown",
                        "came_from": start.get("came_from"),
                    }
                )
        # Still inside one when the log ends.
        for start in open_by_map.values():
            out.append(
                {
                    "map_id": start["map_id"],
                    "layer": start["layer"],
                    "wx": start["anchor_wx"],
                    "wz": start["anchor_wz"],
                    "entered_ms": start["ts_ms"],
                    "duration_ms": None,
                    "placed": ("entrance" if start["anchor_wx"] is not None
                               else "unknown"),
                    "came_from": start.get("came_from"),
                }
            )
        # Where a dungeon is, is a property of the dungeon. A visit you warped
        # into has no entrance of its own, but if you have ever walked into
        # that same place the position is already known -- so borrow it rather
        # than leaving the visit stranded off the map.
        #
        # And a place reached from inside another place, like a Divine Tower
        # off the top of Stormveil, never touches the surface at all: it can
        # only be put near whatever it opens off.
        #
        # Those two feed each other, so it runs until nothing more can be
        # filled in: the tower placed by the way in becomes a position the
        # other visits to that tower can use.
        # A trap chest sits in one spot and always throws you to the same
        # place, so standing where it is and arriving in map X across a load
        # screen says that spot is not X's door -- and it says so about every
        # other visit that set off from the same spot.
        #
        # This exists for imported routes, which cannot see a load screen at
        # all: the old tool sampled every five seconds and never recorded the
        # 0xFFFFFFFF, so a trap taken before this tracker existed looks like
        # walking through a door and anchors the dungeon at the chest. On
        # routes.db that is the chest in Limgrave at (11027, 9161) and Sellia
        # Crystal Tunnel, whose own mouth is 1,780 m away in Caelid.
        #
        # Matched on the departure position rather than on the map you came
        # from: a warp lands you in the same overworld tile you would have
        # walked in from often enough that the coarser test threw away
        # Stormveil's front gate and moved the castle 540 m. Fifteen metres is
        # for sampling noise, not for geography -- the two readings of that
        # chest are 0.8 m apart.
        # What kind of place each map is, taken from the events themselves so
        # this needs no second opinion about map IDs.
        layer_of = {v["map_id"]: v["layer"] for v in out}

        TRAP_TOL_M = 15.0

        def departure_point(entered_ms: int):
            r = self.db.execute(
                "SELECT wx, wz FROM samples WHERE wx IS NOT NULL "
                "AND ts_ms < ? ORDER BY ts_ms DESC LIMIT 1", (entered_ms,)
            ).fetchone()
            return (r["wx"], r["wz"]) if r else None

        def off_the_surface(v: dict) -> bool:
            """Did this visit begin outdoors?

            The anchor only claims to be a door for a dungeon entered from
            the surface. Walking from one interior into another carries the
            last surface position forward unchanged, so that anchor is a
            leftover rather than a claim, and reading it as one made the
            teleporter fixture lose all three of its doors.
            """
            return layer_of.get(v.get("came_from")) == "surface"

        traps: dict[int, list[tuple[float, float]]] = {}
        for v in out:
            if v["layer"] != "interior" or not off_the_surface(v):
                continue
            if not arrived_by_load(v["map_id"], v["entered_ms"]):
                continue
            spot = departure_point(v["entered_ms"])
            if spot:
                traps.setdefault(v["map_id"], []).append(spot)

        for v in out:
            if v["placed"] != "entrance" or v["layer"] != "interior":
                continue
            if not off_the_surface(v):
                continue
            here = (v["wx"], v["wz"])
            if not any(math.dist(here, t) <= TRAP_TOL_M
                       for t in traps.get(v["map_id"], ())):
                continue
            # Walking back out is still a real position, and for a trap it is
            # the only one there is.
            v["wx"] = v["wz"] = None
            v["placed"] = "unknown"
            if v["duration_ms"] is not None:
                found = exit_anchor(v["entered_ms"] + v["duration_ms"])
                if found:
                    v["wx"], v["wz"] = found
                    v["placed"] = "exit"

        # What you have said yourself outranks everything worked out from the
        # route, and applies to every visit to that map.
        by_hand = self.places()
        for v in out:
            if v["map_id"] in by_hand:
                v["wx"], v["wz"] = by_hand[v["map_id"]]
                v["placed"] = "by hand"

        def first_local(map_id: int, at_or_after: int):
            r = self.db.execute(
                "SELECT x, z FROM samples WHERE map_id = ? AND ts_ms >= ? "
                "ORDER BY ts_ms LIMIT 1", (map_id, at_or_after)
            ).fetchone()
            return (r["x"], r["z"]) if r else None

        def last_local(map_id: int, before: int):
            r = self.db.execute(
                "SELECT x, z FROM samples WHERE map_id = ? AND ts_ms <= ? "
                "ORDER BY ts_ms DESC LIMIT 1", (map_id, before)
            ).fetchone()
            return (r["x"], r["z"]) if r else None

        def frame_of(map_id: int):
            """Local metres to world metres for one map, if anything ties them.

            A visit that walked in gives both ends of that tie: the anchor is
            where the door is in the world, and the first sample inside is
            where the door is in the map's own coordinates.
            """
            for v in out:
                if v["map_id"] != map_id or v["wx"] is None:
                    continue
                if v["placed"] not in ("entrance", "by hand", "the doorway"):
                    continue
                local = first_local(map_id, v["entered_ms"])
                if local:
                    return (local[0], local[1], v["wx"], v["wz"])
            return None

        def through_the_door(v: dict) -> bool:
            """Place a dungeon by where you were standing when you left the
            last one.

            Walking from Stormveil into the Divine Tower is walking: the map
            switches, but the step before it was a real position in a map
            whose own position is known, so the doorway can be worked out
            rather than guessed at. This is the difference between drawing the
            tower at the castle's front gate and drawing it where you actually
            went through.
            """
            came = v.get("came_from")
            if came is None:
                return False
            # The door has a position only if you went through it on foot --
            # and on the surface, walking in is seamless, so a load screen
            # means you were put there. Not so between two interiors: the
            # door out of Stormveil into a Divine Tower loads like any other,
            # which is why this only applies coming off the surface.
            if (layer_of.get(came) == "surface"
                    and arrived_by_load(v["map_id"], v["entered_ms"])):
                return False
            frame = frame_of(came)
            if frame is None:
                return False
            leaving = last_local(came, v["entered_ms"])
            if leaving is None:
                return False
            local_x, local_z, wx, wz = frame
            v["wx"] = wx + (leaving[0] - local_x)
            v["wz"] = wz + (leaving[1] - local_z)
            v["placed"] = "the doorway"
            return True

        def known_positions() -> dict:
            best: dict = {}
            rank = {"by hand": 0, "entrance": 1, "the doorway": 2, "exit": 3,
                    "another visit": 4, "the way in": 5}
            for v in out:
                if v["wx"] is None:
                    continue
                r = rank.get(v["placed"], 3)
                cur = best.get(v["map_id"])
                if cur is None or r < cur[2]:
                    best[v["map_id"]] = (v["wx"], v["wz"], r, v["placed"])
            return best

        while True:
            known = known_positions()
            changed = False
            for v in out:
                if v["wx"] is not None:
                    continue
                source, how = known.get(v["map_id"]), "another visit"
                if source is None and through_the_door(v):
                    # Worked out rather than borrowed: nothing more to do.
                    changed = True
                    continue
                # Same reasoning one tier down: a trap's chest is a place
                # you came from, but it is not the way in to anywhere.
                if (source is None and v.get("came_from") in known
                        and not (layer_of.get(v.get("came_from")) == "surface"
                                 and arrived_by_load(v["map_id"],
                                                     v["entered_ms"]))):
                    source, how = known[v["came_from"]], "the way in"
                if source is None:
                    continue
                v["wx"], v["wz"] = source[0], source[1]
                # Borrowing from a guess does not make it a measurement.
                v["placed"] = "the way in" if source[3] == "the way in" else how
                if source[3] == "by hand":
                    v["placed"] = "by hand"
                changed = True
            if not changed:
                break
        # There is more than one way into a place, and a teleporter inside the
        # dungeon you came from gives a doorway nowhere near the others: on
        # routes.db, five ways in agree to within two metres and the sixth is
        # 83 m off, which drew that one visit 83 m from the rest of the same
        # tower. Take the door that agrees with the others -- the one you used
        # most -- and draw every visit from it, so they line up.
        # Which map a dungeon's marker belongs on. A cave you walked into
        # off the Lands Between is drawn on the surface; one entered from
        # Siofra belongs down there, and drawing all of them on both made the
        # underground map a copy of the surface's markers over black.
        for v in out:
            came = layer_of.get(v.get("came_from"))
            v["plane"] = "underground" if came == "underground" else "surface"

        # Entrances vote the same way, for the same reason. A warp the game
        # never announced -- the recorder blind through the map menu and the
        # load -- leaves an anchor at the grace you left from, and it is
        # recorded as an entrance because nothing said otherwise. On
        # routes.db that put one cave's two entrances 3,242 m apart and drew
        # the whole place at whichever came first. Voting cannot settle two
        # (there is no majority in a pair) and does not pretend to; from the
        # third visit on, the odd one out loses.
        for tier in ("the doorway", "entrance"):
            self._agree_on_one(out, tier)

        # The last word of all: somewhere you have said is nowhere has no
        # position, whatever the tiers worked out. It has to run after every
        # one of them -- put next to the hand placement, where it belongs by
        # meaning, four later tiers filled the position straight back in.
        #
        # Clearing a hand placement cannot do this job either: it only sends
        # the question back to the tiers, and they always answer. On
        # routes.db the Roundtable Hold then takes an `exit` anchor from the
        # one time leaving it did not look like a warp.
        nowhere = self.nowhere()
        for v in out:
            if v["map_id"] in nowhere:
                v["wx"] = v["wz"] = None
                v["placed"] = "nowhere"
        return out

    @staticmethod
    def _agree_on_one(out: list[dict], tier: str) -> None:
        """Draw every visit to a map from the anchor that agrees with the rest.

        The medoid, not the mean: an average of a real door and a warp is a
        third place that is neither, while the medoid is always one of the
        anchors actually recorded.
        """
        found: dict[int, list[tuple]] = {}
        for v in out:
            if v["placed"] == tier and v["wx"] is not None:
                found.setdefault(v["map_id"], []).append((v["wx"], v["wz"]))
        for map_id, points in found.items():
            if len(points) < 3 and tier == "entrance":
                continue
            agreed = min(
                points, key=lambda a: sum(math.dist(a, b) for b in points)
            )
            for v in out:
                if v["map_id"] == map_id and v["placed"] in (
                    tier, "another visit"
                ):
                    v["wx"], v["wz"] = agreed

    def close(self) -> None:
        self.db.commit()
        self.db.close()

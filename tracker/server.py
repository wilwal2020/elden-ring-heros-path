"""Serves the viewer, the route data, and the live feed.

Path simplification happens here rather than in the browser. Ramer-Douglas-
Peucker with an epsilon tied to the requested zoom turns 200k points into a
couple of thousand visible segments, which is the difference between a map
that scrolls and one that doesn't.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web

from .coords import MapConfig, MapId, Projection
from .store import Store

VIEWER_DIR = Path(__file__).resolve().parent.parent / "viewer"

# Bumped whenever the viewer starts needing something this file did not send
# before. Python loads server.py once at startup and serves viewer/ fresh from
# disk, so a recorder left running across an update pairs a new page with an
# old server: endpoints it has never heard of 404 loudly, but a field it does
# not send yet fails silently, and the feature simply does not appear. The
# viewer checks this number and says which half is out of date.
API_VERSION = 17


def tile_pyramid_extent(zoom: int, which: str = "tiles") -> Optional[tuple[int, int]]:
    """Columns and rows of tiles present on disk at `zoom`, or None if that
    level was never generated."""
    level = VIEWER_DIR / which / str(zoom)
    if not level.is_dir():
        return None
    cols = [d for d in level.iterdir() if d.is_dir() and d.name.isdigit()]
    if not cols:
        return None
    rows = 0
    for d in cols:
        for f in d.iterdir():
            if f.suffix == ".webp" and f.stem.isdigit():
                rows = max(rows, int(f.stem) + 1)
    return max(int(d.name) for d in cols) + 1, rows


def tile_levels_present(which: str = "tiles") -> list[int]:
    root = VIEWER_DIR / which
    if not root.is_dir():
        return []
    return sorted(int(d.name) for d in root.iterdir()
                  if d.is_dir() and d.name.isdigit())


def tile_warnings(cfg: dict) -> list[str]:
    """The pyramid on disk is ground truth for the map's size; the [viewer]
    dimensions are typed in by hand from what make_tiles.py printed, so they
    drift. Leaflet clips the tile layer to those dimensions, so a stale
    value looks like a half-generated map rather than a config error --
    which is exactly the kind of thing that costs an hour. Say it plainly.
    """
    v = cfg.get("viewer", {})
    if not (VIEWER_DIR / "tiles").is_dir():
        return []
    out: list[str] = []
    levels = tile_levels_present()
    if not levels:
        return []

    top = v.get("tile_max_zoom", 6)
    if top not in levels:
        out.append(
            f"tile_max_zoom = {top} but viewer/tiles has no level {top} "
            f"(present: {levels[0]}..{levels[-1]}). Tiles will 404 at the "
            f"route's native zoom. Set tile_max_zoom = {levels[-1]}."
        )
        top = levels[-1]

    extent = tile_pyramid_extent(top)
    if extent is None:
        return out
    cols, rows = extent
    w = int(v.get("image_width", 0)) or 0
    h = int(v.get("image_height", 0)) or 0
    # A tile is 256px, and the last one in each direction is padded, so the
    # source image is somewhere in ((cols-1)*256, cols*256]. Anything
    # outside that window is a mismatch, not rounding.
    wrong_w = not ((cols - 1) * 256 < w <= cols * 256)
    wrong_h = not ((rows - 1) * 256 < h <= rows * 256)
    if wrong_w or wrong_h:
        out.append(
            f"[viewer] image_width/image_height = {w}x{h}, but the tiles at "
            f"zoom {top} cover {cols}x{rows} tiles ({cols * 256}x{rows * 256}px). "
            f"Leaflet clips the map to the configured size, so part of it "
            f"will not render. Set image_width = {cols * 256} and "
            f"image_height = {rows * 256} (or the exact size make_tiles.py "
            f"printed), then restart."
        )

    # One projection and one set of image bounds serve both planes, because
    # Siofra and Ainsel sit under the Lands Between at the same world
    # coordinates. That only holds while the two images are the same crop at
    # the same scale: a second map of a different size is silently clipped to
    # the first one's bounds and the route lands on the wrong terrain, which
    # looks like a bad calibration rather than a mismatched image.
    if (VIEWER_DIR / "tiles-underground").is_dir():
        under = tile_pyramid_extent(top, "tiles-underground")
        if under is None:
            out.append(
                f"viewer/tiles-underground has no level {top}, so the "
                f"Underground button will show blank tiles at the route's "
                f"native zoom. Re-run make_tiles.py on the underground image."
            )
        elif under != extent:
            out.append(
                f"viewer/tiles-underground covers {under[0]}x{under[1]} tiles "
                f"against the surface map's {cols}x{rows}. Both planes are "
                f"drawn with one projection, so the two images have to be the "
                f"same crop at the same scale; this one will be clipped and "
                f"the route will not line up with it."
            )
    return out


def rdp(points: list[tuple], epsilon: float) -> list[tuple]:
    """Iterative Ramer-Douglas-Peucker. Iterative because a long route will
    blow the recursion limit."""
    n = len(points)
    if n < 3 or epsilon <= 0:
        return points
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = points[i][0], points[i][1]
        bx, by = points[j][0], points[j][1]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        best_d, best_k = -1.0, -1
        for k in range(i + 1, j):
            px, py = points[k][0], points[k][1]
            if norm == 0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best_d:
                best_d, best_k = d, k
        if best_d > epsilon:
            keep[best_k] = True
            stack.append((i, best_k))
            stack.append((best_k, j))
    return [p for p, k in zip(points, keep) if k]


class Server:
    def __init__(self, store: Store, cfg: dict, host: str, port: int):
        self.store = store
        self.cfg = cfg
        self.maps = MapConfig(cfg)
        self.proj = Projection(cfg)
        self.host = host
        self.port = port
        self.clients: set[web.WebSocketResponse] = set()
        # Set by `record` to the session it is writing. Deleting that one
        # would leave the sampler inserting samples under a session row that
        # no longer exists, so the endpoint refuses it.
        self.active_session: Optional[int] = None
        # Set when a recorder asks a read-only viewer for the port back.
        # Created here rather than at import time: an Event binds to the
        # running loop, and there is not one yet.
        self.stopping = asyncio.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.app = web.Application()
        routes = [
            web.get("/", self.index),
            web.get("/{name:(?:app\\.js|style\\.css|index\\.html)}",
                    self.viewer_asset),
            web.get("/api/meta", self.meta),
            web.get("/api/route", self.route),
            web.get("/api/interiors", self.interiors),
            web.get("/api/deaths", self.deaths),
            web.get("/api/warps", self.warps),
            web.get("/api/interior", self.interior),
            web.get("/api/last", self.last),
            web.delete("/api/session/{sid}", self.delete_session),
            web.post("/api/place", self.place),
            web.post("/api/death", self.death),
            web.post("/api/name", self.name),
            web.get("/api/stats", self.stats),
            web.post("/api/standdown", self.standdown),
            web.get("/ws", self.ws),
        ]
        if (VIEWER_DIR / "tiles").exists():
            routes.append(
                web.static("/tiles", str(VIEWER_DIR / "tiles"), show_index=False)
            )
        else:
            routes.append(web.get("/tiles/{tail:.*}", self.no_tiles))
        # Catch-all last so it can't shadow the routes above.
        routes.append(web.static("/", str(VIEWER_DIR), show_index=False))
        self.app.add_routes(routes)

    # index.html, app.js and style.css are one thing in three files: the
    # script looks up elements the page declares. Served with no directive at
    # all they get heuristic caching, and a browser holding yesterday's
    # index.html against today's app.js means getElementById returns null,
    # wireControls() throws on the first missing control, and boot() dies
    # before it ever draws a route -- a blank map with dead buttons, from a
    # change that works everywhere it is tested with a cache-buster. The
    # tiles are still cached: they are large and never change.
    NO_STORE = {"Cache-Control": "no-store, must-revalidate"}

    async def index(self, request):
        return web.FileResponse(VIEWER_DIR / "index.html",
                                headers=self.NO_STORE)

    async def viewer_asset(self, request):
        name = request.match_info["name"]
        path = VIEWER_DIR / name
        if not path.is_file():
            return web.Response(status=404, text=f"no {name} in viewer/")
        return web.FileResponse(path, headers=self.NO_STORE)

    async def no_tiles(self, request):
        return web.Response(status=404, text="no tiles generated yet")

    async def meta(self, request):
        b = self.store.bounds()
        return web.json_response(
            {
                "api": API_VERSION,
                # Which session is being written right now, or null for a
                # read-only viewer. `record` asks before it takes the port:
                # a viewer should stand aside for a recorder, a second
                # recorder is a mistake worth refusing.
                "recording": self.active_session,
                "sessions": self.store.sessions(),
                # Where the samples fall in time, so the viewer can colour by
                # age and filter by date evenly rather than by the clock.
                "time_quantiles": self.store.time_quantiles(),
                "underground_tiles": (VIEWER_DIR / "tiles-underground").is_dir(),
                "bounds": b,
                "projection": {
                    "scale_x": self.proj.scale_x, "scale_y": self.proj.scale_y,
                    "offset_x": self.proj.offset_x, "offset_y": self.proj.offset_y,
                },
                "map": self.cfg.get("viewer", {}),
            }
        )

    async def route(self, request):
        q = request.query
        layers = q.get("layers", "surface,underground").split(",")
        sessions = q.get("sessions")
        session_ids = [int(s) for s in sessions.split(",")] if sessions else None
        t0 = int(q["t0"]) if "t0" in q else None
        t1 = int(q["t1"]) if "t1" in q else None
        epsilon = float(q.get("epsilon", 2.0))
        raw = q.get("raw") == "1"

        # Split into segments at every break, then simplify each segment.
        segments: list[dict] = []
        current: list[tuple] = []
        current_layer = None
        for r in self.store.iter_samples(layers, session_ids, t0, t1):
            if r["break_before"] or r["layer"] != current_layer:
                if len(current) > 1:
                    segments.append({"layer": current_layer, "points": current})
                current = []
                current_layer = r["layer"]
            current.append((r["wx"], r["wz"], r["y"], r["ts_ms"]))
        if len(current) > 1:
            segments.append({"layer": current_layer, "points": current})

        out = []
        total_in = total_out = 0
        for seg in segments:
            total_in += len(seg["points"])
            pts = rdp(seg["points"], epsilon)
            total_out += len(pts)
            # raw=1 hands back world metres so the viewer can apply a candidate
            # transform live, without a config edit and restart per attempt.
            coords = (
                [[round(p[0], 2), round(p[1], 2)] for p in pts]
                if raw
                else [[round(a, 2), round(b, 2)] for a, b in
                      (self.proj.apply(p[0], p[1]) for p in pts)]
            )
            out.append(
                {
                    "layer": seg["layer"],
                    "xy": coords,
                    "h": [round(p[2], 1) for p in pts],
                    "t": [p[3] for p in pts],
                }
            )
        return web.json_response(
            {"segments": out, "points_in": total_in, "points_out": total_out,
             "raw": raw}
        )

    async def interiors(self, request):
        """Interior visits: those that can go on the map, and those that can't.

        A route that starts inside a dungeon has no entrance to anchor to, so
        the visit has no world position at all. Its path is still recorded, so
        it is handed back separately rather than dropped -- the viewer lists
        those instead of inventing a place for them.
        """
        out = []
        unplaced = []
        for v in self.store.interior_visits():
            m = MapId.unpack(v["map_id"])
            if v["layer"] not in ("interior", "unknown"):
                continue
            if v["wx"] is None:
                unplaced.append(
                    {
                        "label": self._label(m),
                        "map": str(m),
                        "map_id": v["map_id"],
                        "entered_ms": v["entered_ms"],
                        "left_ms": (v["entered_ms"] + v["duration_ms"]
                                    if v["duration_ms"] is not None else None),
                        "duration_ms": v["duration_ms"],
                        "placed": "unknown",
                    }
                )
                continue
            x, y = self.proj.apply(v["wx"], v["wz"])
            out.append(
                {
                    # The plane its entrance is on: a cave off Limgrave is
                    # drawn on the surface, one off Siofra down there.
                    "plane": v["plane"],
                    # Your name for it, separately from the label, so the
                    # rename field shows what you typed rather than the
                    # generic "Cave" it falls back to.
                    "name": self.store.names().get(v["map_id"], ""),
                    "xy": [round(x, 2), round(y, 2)],
                    "label": self._label(m),
                    "map": str(m),
                    # The raw id and the time window are what /api/interior
                    # needs to find this visit's path: interiors have no world
                    # position to look them up by.
                    "map_id": v["map_id"],
                    "entered_ms": v["entered_ms"],
                    "left_ms": (v["entered_ms"] + v["duration_ms"]
                                if v["duration_ms"] is not None else None),
                    "duration_ms": v["duration_ms"],
                    "placed": v.get("placed", "entrance"),
                    # Legacy dungeons are drawn on the map at all times; caves
                    # only when you ask for them.
                    "world_visible": self.maps.is_world_visible(m),
                }
            )
        return web.json_response({"visits": out, "unplaced": unplaced})

    def _respawn_item(self, d: dict):
        """Where you got up, in the same shape as the death itself.

        None when there was no load screen to read it from, and None again
        when there is nowhere to draw it: a respawn inside a dungeon has only
        that dungeon's own metres, which the viewer draws on the dungeon's
        path the way it draws a death there.
        """
        r = d.get("respawn")
        if r is None:
            return None
        m = MapId.unpack(r["map_id"])
        item = {
            "map": str(m),
            "map_id": r["map_id"],
            "label": self._label(m),
            "layer": r["layer"],
            "ts": r["ts_ms"],
            "after_s": r["after_s"],
            "local": ([r["local_x"], r["local_z"]]
                      if r["local_x"] is not None else None),
        }
        if r["wx"] is not None:
            x, y = self.proj.apply(r["wx"], r["wz"])
            item["xy"] = [round(x, 2), round(y, 2)]
        return item

    async def deaths(self, request):
        """Where HP hit zero, as map pixels.

        A death inside a dungeon has no world position of its own, so it is
        placed at the last surface position -- the same anchor the dungeon's
        own marker uses -- and carries the map name so the popup can say where
        it really happened.
        """
        out = []
        for d in self.store.deaths():
            m = MapId.unpack(d["map_id"])
            # A death with no anchor is still a death. It has nowhere to go on
            # the world map, but it has the dungeon's own metres and is drawn
            # on that dungeon's path -- dropping it here meant a death marked
            # by hand inside a cave vanished entirely.
            xy = None
            if d["anchor_wx"] is not None:
                x, y = self.proj.apply(d["anchor_wx"], d["anchor_wz"])
                xy = [round(x, 2), round(y, 2)]
            out.append(
                {
                    "xy": xy,
                    "by_hand": d["by_hand"],
                    "map": str(m),
                    "map_id": d["map_id"],
                    "label": self._label(m),
                    "layer": d["layer"],
                    "ts": d["ts_ms"],
                    # Where it happened in the dungeon's own metres, when that
                    # was recorded: the entrance anchor above is only where
                    # the mark sits until the dungeon itself is drawn.
                    "local": ([d["local_x"], d["local_z"]]
                              if d["local_x"] is not None else None),
                    "respawn": self._respawn_item(d),
                }
            )
        return web.json_response({"deaths": out})

    async def warps(self, request):
        """Both ends of every jump the line refused to draw."""
        out = []
        for w in self.store.warps():
            item = {
                "map": str(MapId.unpack(w["map_id"])),
                "map_id": w["map_id"],
                "from_map": str(MapId.unpack(w["from_map"])),
                "ts": w["ts_ms"],
                "gap_s": round((w["ts_ms"] - w["from_ts"]) / 1000, 1),
                "distance_m": round(w["distance_m"]),
                "reason": {1: "the map changed",
                           3: "a load screen came between",
                           4: "you came out of somewhere else"}.get(
                               w["break_before"], "too fast to have walked"),
                "inside": bool(w["inside"]),
                # Which map it belongs on. A waygate in Nokron is not a mark
                # on the Lands Between, and without this the viewer read it
                # as undefined and put every underground jump on the surface,
                # where it was drawn in the middle of Limgrave and nowhere at
                # all on the map you were standing on.
                "layer": w["layer"],
            }
            if w["inside"]:
                # A lift or a teleporter within one dungeon: local metres, to
                # be drawn on that dungeon's own drawing.
                item["local"] = [round(w["x"], 2), round(w["z"], 2)]
                item["from_local"] = [round(w["from_x"], 2),
                                      round(w["from_z"], 2)]
            else:
                to_x, to_y = self.proj.apply(w["wx"], w["wz"])
                from_x, from_y = self.proj.apply(w["from_wx"], w["from_wz"])
                item["xy"] = [round(to_x, 2), round(to_y, 2)]
                item["from_xy"] = [round(from_x, 2), round(from_y, 2)]
            out.append(item)
        return web.json_response({"warps": out})

    async def interior(self, request):
        """One interior visit's path, in the interior's own local metres.

        Kept out of /api/route deliberately: these coordinates share no frame
        with the world plane or with each other, so they are only meaningful
        drawn on their own axes.
        """
        q = request.query
        try:
            map_id = int(q["map_id"])
            t0 = int(q["t0"])
        except (KeyError, ValueError):
            return web.json_response(
                {"ok": False,
                 "error": "needs map_id and t0, both from /api/interiors"},
                status=400,
            )
        t1 = int(q["t1"]) if q.get("t1") else None
        epsilon = float(q.get("epsilon", 0.4))

        segments: list[list[tuple]] = []
        current: list[tuple] = []
        for r in self.store.interior_path(map_id, t0, t1):
            if r["break_before"] and current:
                segments.append(current)
                current = []
            current.append((r["x"], r["z"], r["y"], r["ts_ms"]))
        if current:
            segments.append(current)

        out = []
        total_in = total_out = 0
        x0 = z0 = y0 = float("inf")
        x1 = z1 = y1 = float("-inf")
        for seg in segments:
            total_in += len(seg)
            pts = rdp(seg, epsilon) if len(seg) > 2 else seg
            total_out += len(pts)
            for px, pz, py, _ in pts:
                x0, x1 = min(x0, px), max(x1, px)
                z0, z1 = min(z0, pz), max(z1, pz)
                y0, y1 = min(y0, py), max(y1, py)
            out.append(
                {
                    "xy": [[round(p[0], 2), round(p[1], 2)] for p in pts],
                    "h": [round(p[2], 1) for p in pts],
                    "t": [p[3] for p in pts],
                }
            )

        m = MapId.unpack(map_id)
        return web.json_response(
            {
                "ok": True,
                "map": str(m),
                "label": self._label(m),
                "segments": out,
                "points_in": total_in,
                "points_out": total_out,
                "bounds": (None if not total_out else
                           {"x0": x0, "x1": x1, "z0": z0, "z1": z1,
                            "y0": y0, "y1": y1}),
            }
        )

    async def last(self, request):
        """Latest recorded position, in raw world metres. Used by the viewer's
        click-to-calibrate flow, which needs the uncalibrated value."""
        r = self.store.db.execute(
            "SELECT ts_ms, map_id, x, y, z, wx, wz FROM samples "
            "WHERE wx IS NOT NULL ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
        if not r:
            return web.json_response({"ok": False})
        m = MapId.unpack(r["map_id"])
        return web.json_response(
            {
                "ok": True,
                "ts": r["ts_ms"],
                "map": str(m),
                "wx": r["wx"],
                "wz": r["wz"],
                "age_s": round((__import__("time").time() * 1000 - r["ts_ms"]) / 1000, 1),
            }
        )

    async def place(self, request):
        """Record where a dungeon actually is, in world metres.

        For the places a route can never locate: a Divine Tower entered from
        inside Stormveil and left the same way never touches the surface, so
        nothing in the recording says where it is. Dragging its marker does.
        """
        try:
            body = await request.json()
            map_id = int(body["map_id"])
        except Exception:
            return web.json_response(
                {"ok": False, "error": "needs map_id, and wx/wz in metres"},
                status=400,
            )
        if body.get("clear"):
            self.store.clear_place(map_id)
            return web.json_response({"ok": True, "cleared": True})
        # Not a position: somewhere that is nowhere in the world. A different
        # answer from clearing, which only hands the question back to the
        # tiers -- and they always have an answer, whether or not there is one.
        if body.get("nowhere"):
            self.store.set_nowhere(map_id)
            return web.json_response({"ok": True, "nowhere": True})
        try:
            wx, wz = float(body["wx"]), float(body["wz"])
        except Exception:
            return web.json_response(
                {"ok": False, "error": "wx and wz must be numbers"}, status=400
            )
        self.store.set_place(map_id, wx, wz)
        return web.json_response({"ok": True, "map_id": map_id,
                                  "wx": wx, "wz": wz})

    def _label(self, m: MapId) -> str:
        """What to call a map: your name for it, or what kind of thing it is.

        One helper rather than five call sites, so naming a cave renames it
        on its marker, in its popup, on every death and teleport inside it,
        in the unplaced list and in the live status line at once.
        """
        return self.store.names().get(m.pack(), self.maps.label(m))

    async def stats(self, request):
        """What the whole route adds up to. Its own endpoint because it walks
        every sample, and nothing else needs to."""
        # Which areas are castles is a config question, not a storage one, so
        # the store is told rather than left to guess.
        return web.json_response(self.store.stats(self.maps.legacy_areas))

    async def name(self, request):
        """Name a map, or take the name back."""
        try:
            body = await request.json()
            map_id = int(body["map_id"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return web.json_response(
                {"ok": False, "error": "need a map_id"}, status=400)
        if body.get("clear"):
            return web.json_response(
                {"ok": True, "cleared": self.store.clear_name(map_id)})
        stored = self.store.set_name(map_id, body.get("name", ""))
        return web.json_response({"ok": True, "map_id": map_id, "name": stored})

    async def death(self, request):
        """Say that a jump was a death, or take that back.

        Without the HP reading a death and a teleporter are the same event,
        so the tracker does not guess: it draws a teleport and this is how
        you correct it. `ts` is the arrival -- the sample the jump lands on,
        which is what the teleport mark is drawn from.
        """
        try:
            body = await request.json()
            ts = int(body["ts"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return web.json_response(
                {"ok": False, "error": "need the arrival time of the jump "
                                       "as 'ts'"}, status=400)

        if body.get("clear"):
            gone = self.store.clear_death(ts)
            return web.json_response({"ok": True, "cleared": gone})

        # `at` means "I died at about this moment" rather than "that jump was
        # a death". The playback sends it, because the case it is for is the
        # one with no jump to point at.
        if body.get("at"):
            placed = self.store.mark_death_at(ts)
            if placed is None:
                return web.json_response(
                    {"ok": False,
                     "error": f"nothing was recorded within a minute of {ts}"},
                    status=404)
            return web.json_response({"ok": True, **placed})

        placed = self.store.mark_death(ts)
        if placed is None:
            return web.json_response(
                {"ok": False,
                 "error": f"no jump arriving at {ts} in this database"},
                status=404)
        return web.json_response({"ok": True, **placed})

    async def delete_session(self, request):
        """Delete one session and everything recorded under it."""
        try:
            sid = int(request.match_info["sid"])
        except ValueError:
            return web.json_response(
                {"ok": False, "error": "session id must be a number"}, status=400
            )
        if self.active_session is not None and sid == self.active_session:
            return web.json_response(
                {"ok": False,
                 "error": f"session {sid} is the one being recorded right now. "
                          f"Stop the recorder first, then delete it."},
                status=409,
            )
        removed = self.store.delete_session(sid)
        if removed is None:
            return web.json_response(
                {"ok": False, "error": f"no session {sid} in this database"},
                status=404,
            )
        return web.json_response({"ok": True, "id": sid, **removed})

    async def standdown(self, request):
        """Give up the port, if there is nothing being recorded on it.

        A recorder serves this same viewer plus the live feed, so a read-only
        viewer holding the port is the lesser of the two and should step
        aside rather than make the user hunt for the window it is running in.
        A recorder must not: two of them on one database is a mistake, and
        the one already running has samples the other does not.
        """
        if self.active_session is not None:
            return web.json_response(
                {"ok": False,
                 "error": f"a recorder is writing session {self.active_session} "
                          f"on this port"},
                status=409,
            )
        self.stopping.set()
        return web.json_response({"ok": True})

    async def ws(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
        return ws

    def push_event(self, ev: dict) -> None:
        """Called from the sampler thread when something happens that isn't a
        position: a death, so far."""
        if not self.loop or not self.clients:
            return
        payload = dict(ev)
        if ev.get("wx") is not None:
            x, y = self.proj.apply(ev["wx"], ev["wz"])
            payload["xy"] = [round(x, 2), round(y, 2)]
        payload.pop("wx", None)
        payload.pop("wz", None)
        asyncio.run_coroutine_threadsafe(
            self._broadcast(json.dumps(payload)), self.loop
        )

    def push(self, sample: dict) -> None:
        """Called from the sampler thread."""
        if not self.loop or not self.clients:
            return
        if sample["wx"] is None:
            # Inside a cave or dungeon: no world position to draw, but the
            # viewer still needs to say where you are and refresh its markers.
            payload = json.dumps(
                {
                    "type": "interior",
                    "label": sample.get("label", ""),
                    "map": sample.get("map", ""),
                    "map_id": sample.get("map_id"),
                    # The dungeon's own coordinates, so the viewer can move the
                    # position mark along the path it is drawing rather than
                    # leaving it parked at the entrance.
                    "local": [round(sample.get("x", 0.0), 2),
                              round(sample.get("z", 0.0), 2)],
                    # Carried here as well as on a world sample: getting up
                    # from a death inside a castle is a load screen like any
                    # other, and it is what tells the viewer to go and look
                    # for the grace you appeared at.
                    "break": sample["break"],
                    "t": sample["ts"],
                }
            )
            asyncio.run_coroutine_threadsafe(self._broadcast(payload), self.loop)
            return
        x, y = self.proj.apply(sample["wx"], sample["wz"])
        payload = json.dumps(
            {
                "type": "sample",
                "xy": [round(x, 2), round(y, 2)],
                "h": round(sample["y"], 1),
                "layer": sample["layer"],
                "break": sample["break"],
                "t": sample["ts"],
            }
        )
        asyncio.run_coroutine_threadsafe(self._broadcast(payload), self.loop)

    async def _broadcast(self, payload: str) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_str(payload)
            except Exception:
                self.clients.discard(ws)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        for w in tile_warnings(self.cfg):
            print(f"map config: {w}")
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()

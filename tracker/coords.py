"""Map ID unpacking, layer classification, and world-to-pixel projection.

FromSoft map IDs pack four bytes: area, block, region, index. For Elden Ring
the overworld uses area 60 (Lands Between) and 61 (Shadow of the Erdtree),
where block/region are the tile's grid coordinates and index is the grid
scale level. Everything else -- caves, catacombs, legacy dungeons -- is a
discrete map with its own local coordinate space that does NOT belong on the
world map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# The game reports this map ID whenever no map is loaded: loading screens, the
# main menu, and the moment before a character spawns. It is a sentinel, not an
# area, so readings carrying it are dropped rather than recorded -- otherwise
# every load screen becomes a 1-second "visit" to m255_255_255_255 with a
# marker on the map, and the console advises classifying area 255, which is
# advice that cannot be followed.
NO_MAP = 0xFFFFFFFF


def is_no_map(raw: int) -> bool:
    return raw == NO_MAP or ((raw >> 24) & 0xFF) == 0xFF


# Layer names used throughout the pipeline and the viewer.
SURFACE = "surface"
UNDERGROUND = "underground"
INTERIOR = "interior"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class MapId:
    area: int
    block: int
    region: int
    index: int

    @classmethod
    def unpack(cls, raw: int) -> "MapId":
        return cls(
            area=(raw >> 24) & 0xFF,
            block=(raw >> 16) & 0xFF,
            region=(raw >> 8) & 0xFF,
            index=raw & 0xFF,
        )

    def pack(self) -> int:
        return (
            (self.area & 0xFF) << 24
            | (self.block & 0xFF) << 16
            | (self.region & 0xFF) << 8
            | (self.index & 0xFF)
        )

    def __str__(self) -> str:
        return f"m{self.area:02d}_{self.block:02d}_{self.region:02d}_{self.index:02d}"

    @classmethod
    def parse(cls, name: str) -> Optional["MapId"]:
        """The inverse of __str__, for the places config names a map.

        Returns None rather than raising: a typo in a config table should
        cost that one entry, not the whole file.
        """
        parts = str(name).strip().lower().lstrip("m").split("_")
        if len(parts) != 4:
            return None
        try:
            a, b, c, d = (int(p) for p in parts)
        except ValueError:
            return None
        return cls(area=a, block=b, region=c, index=d)


class MapConfig:
    """Wraps the [maps] section of config.toml.

    Everything here is data-driven on purpose. Area numbers for dungeon types
    are easy to verify but easy to get subtly wrong from memory, so unknown
    areas are logged rather than guessed at, and you classify them once as you
    encounter them.
    """

    def __init__(self, cfg: dict):
        maps = cfg.get("maps", {})
        self.open_world_areas = set(maps.get("open_world_areas", [60, 61]))
        # Tile edge length in metres at grid scale index 0.
        self.tile_size = float(maps.get("tile_size_m", 256.0))
        # Y below this, while in an open-world area, means Siofra / Ainsel /
        # Deeproot rather than the surface.
        self.underground_y_max = float(maps.get("underground_y_max", -60.0))
        # Areas that are a plane of the world rather than a room in it. The
        # underground rivers are their own maps -- Siofra is m12_07, not a
        # patch of m60 with a low Y -- so the height test never sees them, and
        # listing 12 as a dungeon drew the whole of Siofra as a legacy dungeon
        # sitting on top of Limgrave.
        self.underground_areas = set(maps.get("underground_areas", [12]))
        # An override for one map that turns out not to share its area's
        # frame, keyed by full map ID. Normally empty: the underground rivers
        # measured so far are one coordinate space, and the area origin
        # covers them.
        self.map_origin = {
            str(k): (float(v[0]), float(v[1]))
            for k, v in maps.get("map_origin", {}).items()
        }
        # area -> human label, e.g. {30: "Catacombs"}
        self.area_labels = {int(k): v for k, v in maps.get("area_labels", {}).items()}
        # A name for one particular map, where the area label cannot say it:
        # every map in area 11 is a "Legacy Dungeon", the Roundtable Hold
        # included.
        self.map_labels = dict(maps.get("map_labels", {}))
        # Maps that are nowhere in the world for good, by name. Not the same
        # question as `map_places.nowhere`, which is an answer you gave about
        # one database and can be taken back: this is a fact about the game,
        # and what it decides is whether the viewer offers to correct it.
        self.nowhere_maps = set(maps.get("nowhere_maps", []))
        # Places nobody can measure from a route, put on the map by hand
        # once and shipped with the tool. A drag lives in `map_places`,
        # which is a table in one database: it travels with that file and
        # with nothing else, so a fresh install draws Farum Azula and the
        # Chapel of Anticipation in the corner of the screen however many
        # times somebody has already worked out where they go. This is
        # the same answer written where it belongs -- beside the labels
        # and the nowhere list, which are facts about the game.
        self.map_places = {}
        for name, v in maps.get("map_place", {}).items():
            m = MapId.parse(name)
            if m is None or not isinstance(v, (list, tuple)) or len(v) != 2:
                print(f"  ignoring [maps.map_place] entry {name!r}: "
                      f"expected a map name and [wx, wz]")
                continue
            self.map_places[m.pack()] = (float(v[0]), float(v[1]))
        # Which areas are legacy dungeons, read off the labels rather than
        # kept as a second list beside them -- two lists of the same fact
        # drift, and the label is what the map already calls the place.
        # Deliberately not world_visible_areas, which also holds the Divine
        # Towers: a tower is a small dungeon you clear, not a castle.
        self.legacy_areas = {
            a for a, lab in self.area_labels.items() if "legacy" in lab.lower()
        }
        # Interiors that the world map already shows: legacy dungeons sit on
        # the surface and are worth drawing there permanently, where a cave is
        # a hole in the ground with nothing to draw on.
        self.world_visible_areas = set(
            maps.get("world_visible_areas", [10, 11, 12, 13, 14, 15, 16, 18, 19])
        )
        # Per-area origin offset in world metres, applied after tile expansion.
        # Lets you slide the DLC map beside the base map on one plane.
        self.area_origin = {
            int(k): (float(v[0]), float(v[1]))
            for k, v in maps.get("area_origin", {}).items()
        }
        # "tile_local": coordinates are relative to the tile named by the map
        #   ID, so the tile's grid position has to be added.
        # "absolute":   coordinates are already world-wide; adding the tile
        #   origin would double-count.
        # "auto":       decide per reading from the magnitude.
        self.position_space = str(maps.get("position_space", "auto")).lower()

    def is_absolute(self, x: float, z: float) -> bool:
        if self.position_space == "absolute":
            return True
        if self.position_space == "tile_local":
            return False
        # A tile is 256m, so a tile-local reading stays well under a few
        # hundred even allowing for boundary overhang. Absolute coordinates in
        # the Lands Between run into the thousands.
        return abs(x) > 600 or abs(z) > 600

    def classify(self, m: MapId, y: float) -> str:
        if m.area in self.open_world_areas:
            return UNDERGROUND if y < self.underground_y_max else SURFACE
        # Checked before the dungeon labels: an underground river is a place
        # you walk around in on its own map, not a hole with a marker.
        if m.area in self.underground_areas:
            return UNDERGROUND
        if m.area in self.area_labels:
            return INTERIOR
        return UNKNOWN

    def is_world_visible(self, m: MapId) -> bool:
        """True for an interior the world map draws anyway, like Stormveil."""
        return (m.area not in self.open_world_areas
                and m.area not in self.underground_areas
                and m.area in self.world_visible_areas)

    def origin_key(self, m: MapId) -> str:
        """How a map names itself in [maps.map_origin]."""
        return str(m)

    def underground_origin(self, m: MapId) -> Optional[tuple[float, float]]:
        """Where this map's local (0, 0) sits in world metres.

        Area first, map second. Siofra and Nokron are m12_07 and m12_02 and
        they are one coordinate space: walking between them moves the local
        coordinates by the two metres you actually walked, five crossings in
        a row, and their x ranges adjoin at 945-957. So the underground is
        chunked the way the overworld is, and one measurement places all of
        it. A map that turns out to disagree gets its own entry, and the
        recorder says so when a crossing implies a different value.
        """
        by_map = self.map_origin.get(self.origin_key(m))
        return by_map if by_map is not None else self.area_origin.get(m.area)

    def has_origin(self, m: MapId) -> bool:
        return self.underground_origin(m) is not None

    def is_underground(self, m: MapId) -> bool:
        return m.area in self.underground_areas

    def same_plane(self, a: Optional[MapId], b: MapId) -> bool:
        """True when two map IDs are tiles of one continuous world.

        The overworld is cut into 256 m tiles, so walking west out of
        m60_42_36 into m60_41_36 changes the map ID without anything having
        happened: the coordinates stay continuous across the seam. Treating
        that as a teleport puts a hole in the line every time you cross a
        tile -- invisible at a quarter-second poll, a 50 m hole in a route
        sampled every five seconds.
        """
        if a is None:
            return False
        if a.area != b.area:
            return False
        # The underground is chunked like the overworld: walking from Siofra
        # into Nokron changes the map ID with the coordinates running straight
        # on. Breaking the line there put five holes in a twelve-minute walk.
        return (a.area in self.open_world_areas
                or a.area in self.underground_areas)

    def is_nowhere(self, m: MapId) -> bool:
        """Whether this map is nowhere in the world however it was reached."""
        return str(m) in self.nowhere_maps

    def placed(self, m: MapId) -> Optional[tuple[float, float]]:
        """Where this map was put by hand by whoever built the config."""
        return self.map_places.get(m.pack())

    def label(self, m: MapId) -> str:
        named = self.map_labels.get(str(m))
        if named:
            return named
        return self.area_labels.get(m.area, str(m))

    def to_world(self, m: MapId, x: float, z: float) -> Optional[tuple[float, float]]:
        """Player coordinates -> flat world metres.

        Returns None for discrete interiors, whose local axes are meaningless
        on the world plane. This is the single check that stops cave crawling
        from being drawn as an overworld stroll.
        """
        # An underground river has its own origin, measured once from the
        # lift you came down. Without one it is stored like an interior --
        # kept, but off the map -- rather than drawn somewhere invented.
        if m.area in self.underground_areas:
            origin = self.underground_origin(m)
            return None if origin is None else (x + origin[0], z + origin[1])
        if m.area not in self.open_world_areas:
            return None
        ox, oz = self.area_origin.get(m.area, (0.0, 0.0))
        if self.is_absolute(x, z):
            return (x + ox, z + oz)
        scale = self.tile_size * (2 ** m.index)
        return (m.block * scale + x + ox, m.region * scale + z + oz)


class Projection:
    """Affine transform from world metres to map-image pixels.

    Solved from calibration points rather than hardcoded, because the constant
    depends on whichever map image you generated tiles from. Run
    tools/calibrate.py to produce these numbers.
    """

    def __init__(self, cfg: dict):
        p = cfg.get("projection", {})
        self.scale_x = float(p.get("scale_x", 1.0))
        self.scale_y = float(p.get("scale_y", 1.0))
        self.offset_x = float(p.get("offset_x", 0.0))
        self.offset_y = float(p.get("offset_y", 0.0))

    def apply(self, wx: float, wz: float) -> tuple[float, float]:
        return (wx * self.scale_x + self.offset_x, wz * self.scale_y + self.offset_y)


def solve_projection(pairs: list[tuple[float, float, float, float]]) -> dict:
    """Least-squares fit of an axis-aligned affine transform.

    pairs: (world_x, world_z, pixel_x, pixel_y). Two well-separated points are
    enough; more improves the fit. Axis-aligned is correct here because the
    game's world axes and the map image axes are parallel -- only scale and
    offset differ.
    """
    if len(pairs) < 2:
        raise ValueError("need at least two calibration points")

    def fit(src: list[float], dst: list[float]) -> tuple[float, float]:
        n = len(src)
        mean_s = sum(src) / n
        mean_d = sum(dst) / n
        num = sum((s - mean_s) * (d - mean_d) for s, d in zip(src, dst))
        den = sum((s - mean_s) ** 2 for s in src)
        if den == 0:
            raise ValueError("calibration points are not separated on one axis")
        scale = num / den
        return scale, mean_d - scale * mean_s

    sx, ox = fit([p[0] for p in pairs], [p[2] for p in pairs])
    sy, oy = fit([p[1] for p in pairs], [p[3] for p in pairs])
    return {"scale_x": sx, "offset_x": ox, "scale_y": sy, "offset_y": oy}

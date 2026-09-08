"""Position sources.

Everything downstream consumes Reading objects, so the live game reader and
the simulator are interchangeable. Build and debug the viewer against the
simulator, then swap in the real source once your signatures are in.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from .memory import GameMemory, PointerSpec


@dataclass
class Reading:
    map_id: int
    x: float
    y: float
    z: float
    # None when the game is not being read for HP at all, which is the case
    # whenever [memory.pointers.player_hp] is absent or does not resolve.
    hp: Optional[int] = None


class PositionSource(Protocol):
    def read(self) -> Optional[Reading]: ...
    def close(self) -> None: ...
    # True once the thing being read has gone away for good. A source with
    # nothing to outlive -- the simulator, a file being replayed -- never
    # says yes, so nothing has to know which kind it is holding.
    def gone(self) -> bool: ...


class GameSource:
    """Reads player coordinates out of the running game."""

    def __init__(self, cfg: dict):
        mem_cfg = cfg["memory"]
        self.mem = GameMemory(mem_cfg.get("process_name", "eldenring.exe"))
        self.pos = self._spec("player_position", mem_cfg)
        self.map = self._spec("map_id", mem_cfg)
        # Optional: death marks need it, nothing else does.
        self.hp = None
        if "player_hp" in mem_cfg.get("pointers", {}):
            try:
                self.hp = self._spec("player_hp", mem_cfg)
            except Exception as e:
                print(f"  player_hp is configured but unusable ({e}). "
                      f"Deaths will not be marked.")
        self._hp_warned = False
        f = mem_cfg.get("field_offsets", {})
        self.off_x = int(f.get("x", 0x0))
        self.off_y = int(f.get("y", 0x4))
        self.off_z = int(f.get("z", 0x8))
        self.off_map = int(f.get("map_id", 0x0))

    @staticmethod
    def _spec(name: str, mem_cfg: dict) -> PointerSpec:
        s = mem_cfg["pointers"][name]
        if "??" not in s["pattern"] and len(s["pattern"].split()) < 4:
            raise ValueError(f"pointer '{name}' has no usable pattern in config.toml")
        return PointerSpec(
            name=name,
            pattern=s["pattern"],
            rip_offset=int(s["rip_offset"]),
            instruction_length=int(s["instruction_length"]),
            chain=[int(c) for c in s.get("chain", [])],
        )

    def describe(self) -> str:
        return f"game v{self.mem.version} at 0x{self.mem.base:X}"

    # Two in a row before believing it. `alive()` reads a byte at the module
    # base, which is as cheap a question as there is, but the answer during a
    # tear-down can be no for one read and yes for the next -- and stopping
    # the recorder is not something to do on a single flake.
    GONE_READS = 2

    def gone(self) -> bool:
        """True when the game has exited, as opposed to merely not answering.

        A load screen, the main menu, and a character not yet spawned all make
        `read()` return None, and none of them mean the game has closed. The
        module base going unreadable does.
        """
        if self.mem.alive():
            self._gone = 0
            return False
        self._gone = getattr(self, "_gone", 0) + 1
        return self._gone >= self.GONE_READS

    def read(self) -> Optional[Reading]:
        if not self.mem.alive():
            return None
        pos = self.mem.resolve(self.pos)
        mp = self.mem.resolve(self.map)
        if pos is None or mp is None:
            return None  # loading screen, main menu, or no character loaded
        try:
            return Reading(
                map_id=self.mem.read_u32(mp + self.off_map),
                x=self.mem.read_f32(pos + self.off_x),
                y=self.mem.read_f32(pos + self.off_y),
                z=self.mem.read_f32(pos + self.off_z),
                hp=self._read_hp(),
            )
        except Exception:
            return None

    def _read_hp(self) -> Optional[int]:
        """Current HP, or None if it isn't configured or can't be read.

        A bad HP chain must not take the route down with it, so a failure here
        is reported once and then simply means no death marks.
        """
        if self.hp is None:
            return None
        try:
            addr = self.mem.resolve(self.hp)
            if addr is None:
                return None
            return self.mem.read_i32(addr)
        except Exception as e:
            if not self._hp_warned:
                self._hp_warned = True
                print(f"  could not read HP ({e}). Deaths will not be marked.")
            return None

    def close(self) -> None:
        pass


class SimSource:
    """Synthetic walk that exercises every edge case the viewer has to handle.

    Produces surface wandering, a descent into an underground river, entry into
    a discrete cave map, and periodic grace warps, so you can verify layer
    separation and teleport-gap detection without launching anything.
    """

    def __init__(self, cfg: dict, seed: int = 7):
        self.rng = random.Random(seed)
        self.t = 0
        self.map = (60 << 24) | (43 << 16) | (36 << 8)
        self.x, self.y, self.z = 120.0, 12.0, 90.0
        self.heading = 0.6
        self.mode = "surface"
        self.mode_left = 400
        self.hp = 1200
        # 0 = alive, 1 = load screen next, 2 = get up at a grace next.
        self.dying = 0

    def _next_mode(self) -> None:
        self.mode = self.rng.choice(
            ["surface", "surface", "surface", "underground", "cave", "warp"]
        )
        self.mode_left = self.rng.randint(120, 500)
        if self.mode == "underground":
            self.map = (60 << 24) | (self.rng.randint(42, 45) << 16) | (35 << 8)
            self.y = -180.0
        elif self.mode == "cave":
            self.map = (31 << 24) | (self.rng.randint(1, 18) << 16)
            self.x, self.y, self.z = 40.0, 0.0, 40.0
            self.mode_left = self.rng.randint(80, 240)
        elif self.mode == "warp":
            # Instant jump to a distant grace: the case that must not be
            # drawn as a walked line.
            self.map = (60 << 24) | (self.rng.randint(38, 48) << 16) | (
                self.rng.randint(30, 42) << 8
            )
            self.x = self.rng.uniform(0, 250)
            self.z = self.rng.uniform(0, 250)
            self.y = self.rng.uniform(5, 90)
            self.mode = "surface"
            self.mode_left = self.rng.randint(200, 600)
        else:
            self.map = (60 << 24) | (self.rng.randint(40, 46) << 16) | (
                self.rng.randint(33, 39) << 8
            )
            self.y = self.rng.uniform(5, 120)

    def _warp(self) -> None:
        self.map = (60 << 24) | (self.rng.randint(38, 48) << 16) | (
            self.rng.randint(30, 42) << 8
        )
        self.x = self.rng.uniform(0, 250)
        self.z = self.rng.uniform(0, 250)
        self.y = self.rng.uniform(5, 90)
        self.mode = "surface"
        self.mode_left = self.rng.randint(200, 600)

    def read(self) -> Optional[Reading]:
        # Dying: HP hits zero where you fell, then a load screen, then you get
        # up at a grace somewhere else. Two positions that must not be joined
        # by a line, and a death worth marking.
        if self.dying == 2:
            self.dying = 0
            self.hp = 1200
            self._warp()
            return Reading(self.map, self.x, self.y, self.z, self.hp)
        if self.dying == 1:
            self.dying = 2
            return Reading(0xFFFFFFFF, 0.0, 0.0, 0.0, 0)
        if self.mode == "surface" and self.rng.random() < 0.002:
            self.dying = 1
            self.hp = 0
            return Reading(self.map, self.x, self.y, self.z, 0)

        self.t += 1
        self.mode_left -= 1
        if self.mode_left <= 0:
            self._next_mode()
        self.heading += self.rng.gauss(0, 0.25)
        step = 5.5 if self.mode == "surface" else 2.5
        self.x += math.cos(self.heading) * step
        self.z += math.sin(self.heading) * step
        self.y += self.rng.gauss(0, 0.8)
        # Roll over tile boundaries the way the game does.
        area = (self.map >> 24) & 0xFF
        blk = (self.map >> 16) & 0xFF
        reg = (self.map >> 8) & 0xFF
        idx = self.map & 0xFF
        while self.x >= 256 and blk < 60:
            self.x -= 256; blk += 1
        while self.x < 0 and blk > 30:
            self.x += 256; blk -= 1
        while self.z >= 256 and reg < 60:
            self.z -= 256; reg += 1
        while self.z < 0 and reg > 30:
            self.z += 256; reg -= 1
        self.x = min(max(self.x, 0.0), 255.9)
        self.z = min(max(self.z, 0.0), 255.9)
        self.map = (area << 24) | (blk << 16) | (reg << 8) | idx
        self.hp = max(1, min(1200, self.hp + self.rng.randint(-40, 30)))
        return Reading(self.map, self.x, self.y, self.z, self.hp)

    def describe(self) -> str:
        return "simulator (no game attached)"

    def gone(self) -> bool:
        return False

    def close(self) -> None:
        pass


def make_source(kind: str, cfg: dict):
    if kind == "sim":
        return SimSource(cfg)
    if kind == "game":
        return GameSource(cfg)
    raise ValueError(f"unknown source: {kind}")

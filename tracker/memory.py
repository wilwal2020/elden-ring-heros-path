"""Process memory access via AOB signature scanning.

Hardcoded addresses are why memory-reading tools die on every patch. A byte
pattern around the instruction that touches a structure normally survives
patches even though its address moves, so we scan for the pattern, then read
the RIP-relative displacement out of the matched instruction to find the
structure's static address.

This module is version-agnostic. The patterns live in config.toml.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Optional

try:
    import pymem
    import pymem.process
except ImportError:  # allows the rest of the tool to run without the game
    pymem = None


class MemoryError_(RuntimeError):
    pass


def parse_pattern(pattern: str) -> tuple[bytes, bytes]:
    """'48 8B 05 ?? ?? ?? ??' -> (bytes, mask) where mask 0xFF means 'must match'."""
    tokens = pattern.split()
    raw = bytearray()
    mask = bytearray()
    for t in tokens:
        if t in ("??", "?"):
            raw.append(0x00)
            mask.append(0x00)
        else:
            raw.append(int(t, 16))
            mask.append(0xFF)
    return bytes(raw), bytes(mask)


def pattern_to_regex(pattern: str) -> re.Pattern:
    raw, mask = parse_pattern(pattern)
    parts = []
    for b, m in zip(raw, mask):
        parts.append(re.escape(bytes([b])) if m else b".")
    return re.compile(b"".join(parts), re.DOTALL)


@dataclass
class PointerSpec:
    """A named static pointer: how to find it, and how to walk from it."""
    name: str
    pattern: str
    # Byte offset within the match where the 4-byte RIP-relative displacement sits.
    rip_offset: int
    # Total length of the instruction containing that displacement.
    instruction_length: int
    # Offsets to follow after dereferencing the static address.
    chain: list[int]


class GameMemory:
    def __init__(self, process_name: str = "eldenring.exe"):
        if pymem is None:
            raise MemoryError_(
                "pymem is not installed. Run: pip install pymem  "
                "(Windows only; use --source sim elsewhere)"
            )
        try:
            self.pm = pymem.Pymem(process_name)
        except Exception as e:
            raise MemoryError_(f"could not attach to {process_name}: {e}") from e
        self.module = pymem.process.module_from_name(
            self.pm.process_handle, process_name
        )
        self.base = self.module.lpBaseOfDll
        self.size = self.module.SizeOfImage
        self._text: Optional[bytes] = None
        self._resolved: dict[str, int] = {}

    @property
    def version(self) -> str:
        """File version of the running executable, e.g. '2.6.1.0'.

        Uses ctypes directly so this works without pywin32 installed.
        """
        try:
            import ctypes
            import struct

            # Full path of the running executable.
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                ctypes.c_void_p(self.pm.process_handle), 0, buf, ctypes.byref(size)
            )
            path = buf.value if ok else None
            if not path:
                return "unknown"

            n = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
            if not n:
                return "unknown"
            data = ctypes.create_string_buffer(n)
            ctypes.windll.version.GetFileVersionInfoW(path, 0, n, data)

            ptr = ctypes.c_void_p()
            length = ctypes.c_uint()
            if not ctypes.windll.version.VerQueryValueW(
                data, "\\", ctypes.byref(ptr), ctypes.byref(length)
            ):
                return "unknown"
            info = ctypes.string_at(ptr.value, length.value)
            ms, ls = struct.unpack("<II", info[8:16])
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        except Exception:
            return "unknown"

    def _image(self) -> bytes:
        if self._text is None:
            self._text = self.pm.read_bytes(self.base, self.size)
        return self._text

    def scan(self, spec: PointerSpec) -> int:
        """Find the static address a pointer spec describes."""
        if spec.name in self._resolved:
            return self._resolved[spec.name]
        rx = pattern_to_regex(spec.pattern)
        m = rx.search(self._image())
        if not m:
            # The image is cached, and that cache is a trap when the game was
            # only just started: read too early it is a partial module, the
            # pattern is not in it, and every later attempt would search the
            # same stale bytes and fail forever. Throw it away so the next try
            # reads the process again.
            self._text = None
            raise MemoryError_(
                f"pattern for '{spec.name}' not found. The game may still be "
                f"starting; if it is up and this persists, the game updated "
                f"and the signature needs re-scanning."
            )
        match_addr = self.base + m.start()
        disp_addr = match_addr + spec.rip_offset
        disp = struct.unpack("<i", self.pm.read_bytes(disp_addr, 4))[0]
        static = match_addr + spec.instruction_length + disp
        self._resolved[spec.name] = static
        return static

    def resolve(self, spec: PointerSpec) -> Optional[int]:
        """Walk the pointer chain. Returns None if any link is null."""
        addr = self.pm.read_ulonglong(self.scan(spec))
        for off in spec.chain[:-1] if spec.chain else []:
            if not addr:
                return None
            addr = self.pm.read_ulonglong(addr + off)
        if not addr:
            return None
        return addr + (spec.chain[-1] if spec.chain else 0)

    def read_f32(self, addr: int) -> float:
        return struct.unpack("<f", self.pm.read_bytes(addr, 4))[0]

    def read_i32(self, addr: int) -> int:
        # HP is signed in the game's own struct, and reads negative for a
        # frame or two on death.
        return struct.unpack("<i", self.pm.read_bytes(addr, 4))[0]

    def read_u32(self, addr: int) -> int:
        return struct.unpack("<I", self.pm.read_bytes(addr, 4))[0]

    def alive(self) -> bool:
        try:
            self.pm.read_bytes(self.base, 1)
            return True
        except Exception:
            return False

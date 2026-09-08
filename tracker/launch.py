"""Starting the game, so recording is one thing to click rather than two.

Finding it is worth doing properly: an installed path typed into a config file
goes stale the first time the library moves, and asking someone to hunt for
eldenring.exe is exactly the friction this is meant to remove. Steam already
knows where its libraries are, so ask it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

# What we attach to. Whatever gets started, this is the process that appears.
PROCESS_NAME = "eldenring.exe"

# Relative to a Steam library root.
GAME_DIR = Path("steamapps") / "common" / "ELDEN RING" / "Game"

# What to start, in order of preference. Offline mode first on purpose: this
# tool reads the game's memory, which is only safe with anti-cheat off and the
# game offline, and that launcher is how you get there. eldenring.exe is the
# fallback for an install that does not have it.
LAUNCHERS = ("start_game_in_offline_mode.exe", "eldenring.exe")


def steam_roots() -> list[Path]:
    """Steam's own install directory, from the registry where possible."""
    roots: list[Path] = []
    try:
        import winreg

        for hive, key, value in (
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam",
             "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    p = Path(winreg.QueryValueEx(k, value)[0])
                    if p.is_dir():
                        roots.append(p)
            except OSError:
                continue
    except ImportError:  # not Windows
        pass

    for guess in (
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Steam",
        Path("C:/Steam"),
    ):
        if guess.is_dir():
            roots.append(guess)
    return roots


def steam_libraries() -> list[Path]:
    """Every library folder Steam knows about, including ones on other drives."""
    libs: list[Path] = []
    for root in steam_roots():
        if root not in libs:
            libs.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.exists():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # The file is Valve's own key-value format; the paths are all we want,
        # so a full parser would be more machinery than the job needs.
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(match.group(1).replace("\\\\", "\\"))
            if p.is_dir() and p not in libs:
                libs.append(p)
    return libs


def find_game(configured: Optional[str] = None) -> Optional[Path]:
    """The executable to start, or None if it cannot be found.

    A configured path wins and is never second-guessed: someone who has typed
    one in has a reason, and silently starting a different copy of the game
    would be worse than saying nothing was found.
    """
    if configured:
        p = Path(configured).expanduser()
        return p if p.exists() else None
    for lib in steam_libraries():
        game_dir = lib / GAME_DIR
        for name in LAUNCHERS:
            exe = game_dir / name
            if exe.exists():
                return exe
    return None


def is_running(name: str = PROCESS_NAME) -> bool:
    try:
        import pymem
        import pymem.process

        return pymem.process.process_from_name(name) is not None
    except ImportError:
        pass
    except Exception:
        return False
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return name.lower() in out.lower()
    except Exception:
        return False


def start(exe: Path) -> None:
    """Start the game and let go of it.

    Detached on purpose: closing the recorder's window should not take the
    game down with it.
    """
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    subprocess.Popen(
        [str(exe)], cwd=str(exe.parent), creationflags=flags,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_process(
    timeout_s: float, on_tick: Optional[Callable[[int], None]] = None
) -> bool:
    """Wait for the game's process to exist. True if it turned up in time."""
    deadline = time.time() + timeout_s
    waited = 0
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(1.0)
        waited += 1
        if on_tick and waited % 5 == 0:
            on_tick(waited)
    return False

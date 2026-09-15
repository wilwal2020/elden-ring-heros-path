# Elden Ring Hero's Path

**Hero's Path from Breath of the Wild and Tears of the Kingdom, for Elden
Ring.** It reads your position out of the running game, keeps it, and draws
every step you have taken on a map you can scroll -- your whole history as one
continuous path, not a pile of per-session files. Then it plays it back.

> **Not ready for other people yet.** It has run on one machine, against one
> version of the game, for one person. No packaging, no installer, no support.

## How to use

1. **Turn EasyAntiCheat off**, with [this toggler](https://www.nexusmods.com/eldenring/mods/90)
   or any other bypass. Reading the game's memory only works offline with
   anti-cheat off, the same condition every practice tool has.

2. **Install the mod.**
   [![Download](https://img.shields.io/badge/Download-ZIP-e0a33c?style=for-the-badge&logo=github&logoColor=1a1712&labelColor=2a251d)](https://github.com/wilwal2020/elden-ring-heros-path/archive/refs/heads/main.zip)
   Unzip it anywhere. You also need [Python 3.10 or newer](https://www.python.org/downloads/),
   with **Add python.exe to PATH** ticked in its installer; the first run of
   the tracker installs the rest for you.

3. **Start Elden Ring**, or have the tracker do it -- the section below.

4. **Double-click `Record route.bat`.** It attaches to the running game,
   records while you play, and serves the map in your browser at the same
   time. Leave its window open, and close it when you are done.

To look back at what you have already recorded with the game shut, double-click
**`Open map.bat`** instead.

### Letting it start the game for you

`Record route.bat` does step 3 as well, if it can find the game: with Elden
Ring not already running it searches your Steam libraries for
`start_game_in_offline_mode.exe` -- anti-cheat off is the whole point --
falling back to `eldenring.exe`, and waits up to three minutes for it.

If it says it could not find the game, which is a non-Steam install or a
launcher of your own, put the path in `config/config.toml`, forward slashes:

```toml
[game]
exe = "D:/SteamLibrary/steamapps/common/ELDEN RING/Game/start_game_in_offline_mode.exe"
```

## Everything else

- **[docs/README.md](docs/README.md)** -- the full front page, and what this
  does that Zelda's does not
- **[docs/SETUP.md](docs/SETUP.md)** -- installing it, and what usually goes wrong
- **[docs/MANUAL.md](docs/MANUAL.md)** -- calibration, imports, playback, faults
- **[docs/NOTICE.md](docs/NOTICE.md)** -- the map artwork is the game's, not mine

## Safety

A single-player mapping tool. It **reads** the game's memory and never writes
to it, never modifies the game, and has nothing to do with online play. Your
route database lives in `data/` and stays on your machine.

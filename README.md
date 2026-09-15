# Elden Ring Hero's Path

**Hero's Path from Breath of the Wild and Tears of the Kingdom, for Elden
Ring.** It reads your position out of the running game, keeps it, and draws
every step you have taken on a map you can scroll -- your whole history as one
continuous path, not a pile of per-session files. Then it plays it back.

> **Not ready for other people yet.** It works, and there is a large suite of
> automated checks behind it, but it has run on one machine, against one
> version of the game, for one person. No packaging, no installer, no support.

## Running it

Turn EasyAntiCheat off first: reading the game's memory only works offline
with anti-cheat off, which is the same condition every practice tool has.

Then double-click one of the two files here.

| | |
|---|---|
| **`Record route.bat`** | Starts the game, records while you play, and serves the map at the same time. The first run installs what it needs. |
| **`Open map.bat`** | Looks at what you have already recorded, with the game shut. |

## Everything else

- **[docs/README.md](docs/README.md)** -- the full front page: what it does,
  and what it does that Zelda's does not
- **[docs/SETUP.md](docs/SETUP.md)** -- installing it, and what usually goes
  wrong
- **[docs/MANUAL.md](docs/MANUAL.md)** -- how each part works and why:
  calibration, importing old routes, the underground, playback, troubleshooting
- **[docs/NOTICE.md](docs/NOTICE.md)** -- the map artwork is the game's, not
  mine

## Safety

A single-player mapping tool. It **reads** the game's memory and never writes
to it, never modifies the game, and has nothing to do with online play.

Your route database lives in `data/` and stays on your machine.

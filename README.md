# Elden Ring Hero's Path

**Hero's Path from Breath of the Wild and Tears of the Kingdom, for Elden
Ring.** Zelda draws the last two hundred hours of your wandering on the map as
one long line, and lets you watch it play back from the beginning. The Lands
Between has no such thing. This makes one.

It reads your position out of the running game, keeps it, and draws every step
you have taken on a map you can scroll -- your whole history as one continuous
path, not a pile of per-session files.

> **Not ready for other people yet.** It works, and there are 375 automated
> checks behind it, but it has run on one machine, against one version of the
> game, for one person. No packaging, no installer, no support. It is public
> because there is no reason to hide it, not because it is finished.

---

## Installation

1. **Turn EasyAntiCheat off.** Use [this toggler](https://www.nexusmods.com/eldenring/mods/90)
   or any other EAC bypass. Reading the game's memory only works with anti-
   cheat off, and that means playing offline -- the same condition every
   practice tool has.

2. **Install Python 3.10 or newer** from [python.org](https://www.python.org/downloads/),
   ticking **Add python.exe to PATH** in the installer.

3. **Download the tracker.**

   ```bash
   git clone https://github.com/wilwal2020/elden-ring-heros-path.git
   ```

   Or press **Code → Download ZIP** on this page and unzip it somewhere you
   can find again.

4. **Double-click `Record route.bat`.** It starts the game, records while you
   play, and serves the map at the same time. On the first run it installs the
   Python packages it needs.

5. **Double-click `Open map.bat`** any time afterwards to look at what you have
   recorded, with the game shut.

That is all of it. The map background and the calibration that lines it up with
the world come with the repository, so there is nothing to build.

Recording is Windows only -- it reads the game's memory through `pymem`. The
viewer runs anywhere Python does, so a recorded route can be looked at on any
machine.

**[SETUP.md](SETUP.md)** is the longer walk through, including what to do when
it does not work.

## What it does that Zelda's doesn't

Because Elden Ring needs it to.

| | |
|---|---|
| **Caves are their own maps** | A catacomb is not a scribble on the overworld. Interiors get their own coordinate space and their own drawing, with a pin on the map where the door is. |
| **Teleports break the line** | A grace warp is not a walk. The line stops, both ends are marked, and the playback runs the jump across the map so you can see where you went. |
| **The underground is a second map** | Siofra, Ainsel and Deeproot sit under the Lands Between at the same coordinates. The map follows you down and back up. |
| **Deaths are marked** | With the grace you got up at, so the walk back is part of the record. Where the recorder cannot tell a death from a teleport, you can tell it. |
| **It plays back** | The whole route from the beginning, at up to an hour a second -- easing off at each teleport, dimming the world when you go into a cave. |
| **It survives patches** | Signature scanning rather than hardcoded offsets, which is the thing that killed the tool this replaces. |

## The rest

- **[SETUP.md](SETUP.md)** -- installing it, and what usually goes wrong
- **[MANUAL.md](MANUAL.md)** -- everything else: how each part works and why,
  calibration, importing old routes, the underground, playback, troubleshooting
- **[NOTICE.md](NOTICE.md)** -- the map artwork is the game's, not mine

## Safety

A single-player mapping tool. It **reads** the game's memory and never writes
to it, never modifies the game, and has nothing to do with online play. Run it
offline with anti-cheat off, which is what reading memory requires anyway.

Your route database lives in `data/` and stays on your machine.

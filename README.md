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

## Getting started

```bash
git clone https://github.com/wilwal2020/elden-ring-heros-path.git
```

Then double-click **`Record route.bat`** -- it starts the game, records while
you play, and serves the map at the same time. **`Open map.bat`** opens the map
on its own, with the game shut.

Both find Python themselves and install what is missing on the first run. The
map background and its calibration come with the repository, so there is
nothing to build.

**[SETUP.md](SETUP.md)** is the full walk through, including what to do when it
does not work.

You need Windows and Python 3.10 or newer to record; the viewer runs anywhere.
Anti-cheat has to be off, which means offline play -- the same condition every
practice tool has.

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

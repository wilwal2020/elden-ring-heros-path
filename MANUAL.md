# Manual

Everything the tracker does, and why it does it that way. The front page is
[README.md](README.md); getting it running is [SETUP.md](SETUP.md).

Most of this is not needed to use the thing -- the viewer explains itself --
but the reasoning behind the awkward parts is written down here rather than
lost, because every one of them cost something to work out.

- [Start it](#start-it)
- [Install](#install)
- [Try it with no game attached](#try-it-with-no-game-attached)
- [The map background](#the-map-background)
- [Line up the map with the world](#line-up-the-map-with-the-world)
- [Bring your old routes across](#bring-your-old-routes-across)
- [Live capture](#live-capture)
- [Signatures](#signatures)
- [How the fixes work](#how-the-fixes-work)
- [The underground map](#the-underground-map)
- [Jumps the recorder missed](#jumps-the-recorder-missed)
- [Playback and numbers](#playback-and-numbers)
- [Making the path readable](#making-the-path-readable)
- [Surface and underground](#surface-and-underground)
- [Marks on the map](#marks-on-the-map)
- [Death marks](#death-marks)
- [Layout](#layout)
- [Tuning](#tuning)
- [Troubleshooting](#troubleshooting)

---

## Start it

Double-click **`Record route.bat`** to record while you play, or **`Open map.bat`**
to look at what you have already recorded. **The game does not need to be
running for the map** -- `Open map.bat` starts nothing but the viewer, and if
a recorder is already serving it opens that one instead of failing on the
port. Both find
Python themselves, install anything missing on first run, and stay open long
enough to read the message if something goes wrong.

`Record route.bat` starts Elden Ring too, so it is the only thing you need to
click. It looks through your Steam libraries for
`steamapps/common/ELDEN RING/Game` and starts
**`start_game_in_offline_mode.exe`**, falling back to `eldenring.exe` if that
launcher isn't there -- offline, because reading the game's memory is only
safe offline with anti-cheat off. Whatever starts it, the process it waits for
and attaches to is `eldenring.exe`, and if the game is already running it just
attaches. Set `[game] exe` in `config/config.toml` to override the choice.

For a shortcut somewhere handier: right-click the file, **Send to → Desktop
(create shortcut)**.

Quitting Elden Ring stops the recorder, which closes the map with it, since
the recorder is what serves it. Set `stop_with_game = false` under
`[recording]` in `config/config.toml` if you would rather the map stayed up
after you quit.

Closing the window stops the recorder. Ctrl-C does too, and Windows will ask
"Terminate batch job (Y/N)?" afterwards -- either answer is fine. Nothing is
lost either way: samples reach the database within a few seconds of being
recorded.

Everything below is the same thing from a terminal, plus the setup you only do
once.

## Install

```bash
pip install -r requirements.txt
```

Windows needs `pymem` for live capture (it's in the requirements file, guarded
by platform). Everything else runs anywhere.

Check it works before touching the game:

```bash
python tools/selftest.py
```

That simulates a route, serves it, and exercises every endpoint. All checks
should pass.

## Try it with no game attached

```bash
python -m tracker.main record --source sim --open
```

The simulator walks a synthetic route including underground stretches, cave
entries, and periodic warps, so you can see the layer separation and teleport
handling working. Ctrl-C to stop.

Flags go **after** the subcommand: `record --port 9000`, not `--port 9000 record`.

## The map background

It comes with the repository -- both tile pyramids, surface and underground,
already cut and already lined up with the world by the `[projection]` block in
`config/config.toml`. Download it and the map draws.

That artwork is the game's, not mine; [NOTICE.md](NOTICE.md) says so properly.
If you ever need to rebuild it -- a different game version, a different detail
level -- step 4 of [SETUP.md](SETUP.md) has the extract-stitch-tile run
through, and `tools/build_map.py --help` explains the flags. A rebuilt image
needs its own calibration, which is the next section.

## Line up the map with the world

**Already done for the map that ships with this** -- `[projection]` in
`config/config.toml` was fitted against exactly these tiles, worst point 1.1 px
out. This section is for a map you rebuilt yourself: the transform from world
metres to map pixels depends on which image you tiled, so it is fitted rather
than hardcoded. Three ways to do it, and the first two are buttons in the
viewer.

### Align by eye (most reliable)

With the map tiled and `record --source game` running, open the viewer and
click **Align by eye**. Drag the sliders until the route sits on terrain you
recognise — roads, coastlines, the lake — then press **Use these values**,
paste the block into `config.toml`, and restart the tracker.

This works where landmark clicking fails. Clicking assumes the map image is a
faithful, unstretched copy of the game world, and map packs are often cropped
and resized to a square canvas, which stretches one axis. Untick **Lock aspect
ratio** to scale each axis separately when that has happened. You are matching
what you can see rather than trusting a model of the image.

The route is re-projected in the browser as you drag, so there is no restart
per attempt.

### Click landmarks

With `record --source game` running, open the viewer and click **Click
landmarks** in the side panel.

1. Stand somewhere unmistakable in game — a Site of Grace, a bridge, a ruin.
2. Click that same spot on the map.
3. Travel somewhere far away, ideally close to diagonally opposite, and click
   again. Distance matters: the further apart, the less a misclick distorts
   the fit.
4. Press **Compute projection**, paste the block into `config.toml`, restart.

It reports how far off each point landed. A few pixels is fine; much more
usually means a misclick.

Standing still produces no new samples, which is normal and expected while
you line up a click — the tool only complains if the last position is old
enough to be from a previous session.

### From the terminal

```bash
python tools/calibrate.py
```

Same idea, but you read the pixel coordinates off your map image in an editor
and type them in. Press Enter to capture your in-game position, type `done`
when you have at least two points.

## Bring your old routes across

```bash
python tools/import_legacy.py ~/Downloads/ER_Route_Tracker/routes/*.json
```

Each old file becomes one session in the database, so your previous runs join
the continuous path. It handles a few common JSON shapes and tells you what it
found if it can't locate coordinates. Add `--dry-run` to see what a file
contains without writing anything.

The points are replayed through the same recording code the live capture uses,
so an imported route gets the same treatment as one you record now: line
breaks on warps, load screens dropped, and dungeon interiors kept with no world
position instead of being drawn across the surface. That last one is why an
imported route can look different from what the old tool drew -- the old tool
plotted cave coordinates on the overworld, and this doesn't.

It reads each point's local `x`/`y`/`z` plus its map ID, not any world position
the old file stored, since the local pair is the only version that is right
inside a dungeon. Where the old file does store a world position, the importer
compares its own against it and prints the worst disagreement, so a mismatch
in coordinate space shows up as a warning rather than a quietly wrong route.

Sessions are dated by the timestamps inside the file, not by when you ran the
import, so old runs sort into place.

Files you have already imported are recognised by their exact timestamps and
skipped. This matters more than it sounds: the old tool saved cumulative
snapshots, so `route_15-31`, `route_15-45` and `route_15-47` from one afternoon
are nested, each containing every point of the one before. Importing the folder
wholesale would otherwise draw that afternoon three times. Pass `--force` if
you want a file in anyway. If a route begins inside a dungeon there
is no entrance to put the marker on, so it goes where you came out and the
popup says so.

## Live capture

```bash
python -m tracker.main record --source game --open
```

or double-click `Record route.bat`, which runs that with `--launch` so the game
starts as well.

`--launch` starts the game if it isn't running, waits for its process, and then
waits again for the signature scan to find its footing -- the process existing
and the module being readable are a few seconds apart.

Requires signatures (below). Run the game offline with anti-cheat off.
Position reading touches the game process, so going online with it risks a ban.

---

## Signatures

**These are already filled in.** `config.toml` ships with patterns and offset
chains derived from veeenu's practice tool, correct for game versions up to
2.07.0. You should not need to touch them unless a patch moves things.

Check them against your game before recording:

```bash
python -m tracker.main probe
```

It resolves the pattern, reads ten positions, and flags anything implausible.
Chunk-local coordinates should stay inside a 0-256 range and the map ID should
read as `m60_xx_yy_00` in the overworld.

### If a patch breaks it

Two failure modes, with different fixes.

**"pattern not found"** — rare. The byte signature itself moved. Pull a fresh
pattern from `xtask/src/codegen/aob_scans.rs` in the practice tool repo. The
two numbers after the pattern array there are exactly `rip_offset` and
`instruction_length`.

**Pattern resolves but `probe` shows nonsense** — much more likely. The raw
offsets shifted. Update `chain` only, from
`lib/libeldenring/src/pointers.rs`:

- `player_ins` is the first chain entry (`0x1E508` since 1.07)
- `map_id_offset` is the second entry of the map_id chain (`0x6D0` since 1.08)
- the `0x190, 0x68, 0x70` tail is the chunk position and has been stable

Both files list values per version, so find the block matching your build.

### Why global position, not chunk

The practice tool exposes several position chains, and picking the wrong one
produces a route that looks plausible but is quietly broken.

`chunk_position` is the physics position. The game rebases its origin every few
seconds so float precision doesn't degrade across a huge world, so it sawtooths:
a route drawn from it advances for a few seconds, snaps back, advances again.

`global_position` (0x6C0) sits in the same struct as the map ID (0x6D0) and is
the pair meant to be read together. That's what this uses.

`probe` now flags large jumps within one map tile, which is the signature of a
rebasing source, so this failure gets caught before it reaches your route.

Whether those coordinates are tile-local or already world-wide is auto-detected
from their magnitude; `position_space` under `[maps]` forces it either way if
the guess is ever wrong.

## How the fixes work

**One path.** `data/routes.db`, WAL mode. WAL is what makes a single file safe
against a hard game crash mid-write, which is the real reason tools end up
writing one file per session. Sessions are rows; the viewer selects everything
unless you untick some. Each session records which source produced it, and the
viewer marks simulated ones `(sim)` -- a simulator run and a real one draw the
same way, so without that the combined path is ambiguous. Sessions recorded
before this was added carry no note and are shown plainly.

Hovering a session in the list shows a `x` that deletes it, along with its
samples and map events, after asking once and naming how many points go. There
is no undo, so copy `data/routes.db` somewhere first if you are clearing out a
lot. The session the recorder is currently writing to cannot be deleted --
stop the recorder first.

**Fast map.** Three things compound. Tiles come from a proper pyramid so the
browser only fetches 256px tiles at the resolution it's showing, with
`keepBuffer: 4` preloading a ring outside the viewport. The route draws to
canvas, since SVG makes a DOM node per segment and dies well before 100k
points. And the server runs Ramer-Douglas-Peucker with an epsilon tied to your
current zoom — in testing that took 36,500 points down to 3,006 at low zoom
with nothing visibly different.

**Caves.** Handled structurally, not by height guessing. Caves, catacombs and
legacy dungeons aren't overworld coordinates at all; they're separate maps with
their own local axes, which is exactly why plotting them raw drew you strolling
across the surface. `to_world()` returns nothing for any non-open-world area,
so those coordinates never reach the world plane. They become markers at your
last surface position, sized by how long you spent inside.

Siofra, Ainsel and Deeproot are different — they genuinely share the overworld
grid but sit far below it, so they use a Y threshold and get their own
toggleable layer. Height gradient is on top of that as a secondary cue.

Any area number the tool doesn't recognise gets reported once in the console so
you can classify it in `[maps.area_labels]` as you meet it. Better than
guessing at a table of numbers up front.

Interior samples are still stored, with a NULL world position -- the path
inside is kept rather than thrown away, and every world-map query filters it
out.

Warping into a dungeon you have walked into before keeps its place: the
entrance is unknown for that visit, but the dungeon's position is already
known from the visit you walked in on, so it is borrowed -- and so is the
frame the path is drawn in, which is what stops a warped-in visit being drawn
as though it set off from the door.

A place reached from inside another place -- a Divine Tower off the top of
Stormveil -- never touches the surface, but going through the door is still
walking: the step before the map changed was a real position inside a dungeon
whose own position is known, so the door between them is worked out from it.
The tower lands at the door you used rather than at the castle's front gate.

There is usually more than one way in, and a teleporter inside the dungeon
you came from looks exactly like a door: same map change, same load screen.
So the ways in have to agree with each other -- the door that sits closest to
all the others wins, and every visit is drawn from that one, which is what
keeps them lined up.

Only when even that is impossible -- nothing recorded ties either place to the
world -- does a dungeon fall back to sitting near its neighbour, centred and
dashed to say so.

**Drag its marker to where it belongs.** That is stored against the map, wins
over anything worked out from the route, applies to every visit to that
dungeon and survives restarts -- and the dashes go away, because it is no
longer a guess. Drag it again to correct it. Walking out of a dungeon onto the
surface also fixes it by itself, but a tower you only ever reach from inside a
castle never gets that chance. Only somewhere you
have never once entered on foot stays off the map.

Legacy dungeons are the exception to hiding them. Stormveil, Raya Lucaria and
Leyndell are drawn on the game's own map, so their paths are drawn on ours at
all times, alongside the route -- every visit, not just the longest, drawn at
the same weight as the surface path, with any death in them shown where it
happened rather than at the door. Hovering one shows the same path brighter without
dimming the world -- there is nothing to pick it out from, since it is already
out there in the open. Caves still dim, because a cave's path has the whole
route around it to get lost in. `[maps] world_visible_areas` is the list; a cave is
not on it, because a cave is a hole in the ground with nothing to draw on.

For everything else: hover a dungeon marker and the path inside is drawn on the
map at the entrance, at the same metres-per-pixel as the terrain around it, with
everything else dimmed behind it; click to keep it up. Any death that happened
in there moves from the entrance to where it actually happened. The one thing
the drawing can't tell you is which way the dungeon faces -- its axes have no
fixed relation to the world's -- and the caption says so rather than letting
you assume.

A route recorded from inside a dungeon has no entrance to draw from, so those
visits are listed in the side panel and open in an inset instead. Warping
straight from a grace into a dungeon lands there too: your last position
outside was wherever you warped from, which is not the entrance and would put
the whole dungeon in the wrong place. Walk out of it once and the marker
appears where you came out.

Inside, the position mark follows the path being drawn rather than sitting at
the entrance -- those local metres are the only position there is down there.

While you're inside, the recorder prints what you entered and the viewer's
status reads "Inside <name>", so it's clear it hasn't stopped recording.

Both ends of a teleport get a mark, and repeat jumps from one grace cluster
into a single mark with a count rather than stacking. A lift or teleporter
*inside* a dungeon counts too: it has no world position at either end, so it
is drawn on that dungeon's own path, with a dashed line between the ends. Anything shorter than
50 m is not marked: resting at a grace reloads the area and puts you back
beside it, which is a jump the line has to break for but not a journey.

**Teleports.** A break is inserted when the map changes to somewhere that
isn't the same continuous world -- a cave, a legacy dungeon, the DLC -- or when
implied speed exceeds `max_speed_mps`. Speed rather than raw distance, so a
stalled writer or a long alt-tab doesn't get mistaken for a warp.

A load screen also breaks the line. The reading during one is useless -- no
map, no position -- but the fact of it is not: nothing crosses a load screen on
foot. Dying is the case that needs it. You go down in one place and get up at a
grace, often close enough that the speed check waves it through, and without
this the line is drawn as though you walked back.

Crossing between two overworld tiles is explicitly *not* a break. The map ID
changes every 256 m out there, and cutting the line at every seam leaves a hole
the width of one sampling interval: a couple of metres at a quarter-second poll,
50 m in a route imported from a tool that sampled every five seconds. A warp
between two tiles still breaks, because it trips the speed check.

## The underground map

Siofra, Ainsel and Deeproot are a second map rather than a layer on the first,
so **Map** in the panel has two buttons and switching swaps the terrain, the
route and the marks together.

Build the second pyramid the same way as the first:

```bash
python tools/make_tiles.py m1-underground.png --out viewer/tiles-underground
```

then restart the recorder -- the button only appears once the tiles are on
disk. **The projection is the same one.** Siofra is directly beneath the
Lands Between at the same world coordinates, so nothing about
`[projection]` changes; what has to match is the image, which must be the
same crop at the same scale as the surface map. The startup check measures
both pyramids and complains if they differ, because a mismatched second image
is clipped to the first one's bounds and the route quietly lands on the wrong
terrain.

Marks belong to the map they were made on, so the underground does not show
the surface's caves and deaths over black. **The viewer switches for you**:
ride the lift down and the map follows the recorder, and follows it back up
again.

### The underground has to be measured once

The rivers are their own maps, so where they sit in the world cannot be
guessed -- it has to be measured. Walking down into one on foot is the
measurement, and the recorder does it for you: the last reading in the lift
shaft and the first reading in the river are the same place, so the difference
is the origin. Siofra's is already in `config.toml`, and Siofra, Nokron and
everything else in area 12 turned out to share one coordinate space, so that
one measurement places all of it.

If you reach somewhere that does not, the recorder says so rather than drawing
it in the wrong place, and prints the line to paste:

```
  m12_01_00_00 has no origin yet. Measured from the way you just came in:
    [maps.area_origin]
    12 = [10878.94, 8321.17]
```

Add it under `[maps]` in `config.toml` and restart. Until then the path is
still recorded -- nothing is lost -- it just is not drawn. A route recorded
before the origin existed is placed retroactively, and this is worth running
each time you add one:

```bash
python tools/repair_underground.py --write
```

Warping down to a grace measures nothing, so the recorder only offers a value
when you walked in.

**"It stopped drawing but the console is still counting points"** means
exactly this: you have walked somewhere with no origin. The console says which
map and what to add.

## Jumps the recorder missed

`tools/repair_jumps.py` looks for steps that were not walked and are not
marked as anything, so they are drawn as a line through the rock. Run it with
no arguments and it only reports.

```bash
python tools/repair_jumps.py
```

Inside a dungeon it is confident, because the data leaves it room to be: the
speeds there have an empty band between walking and riding a lift, so a
threshold can sit in the gap. It also looks for a hole in the recording, and
how long a hole has to be depends on how often that session was sampling --
but only up to a point. A death costs a roughly fixed eight to twenty seconds
of wall clock however fast the recorder is going, so against an imported route
sampled every five seconds a rule of "eight times the rhythm" asks for forty
seconds and can never fire. That is capped at eight seconds now, which is what
makes the deaths in imported routes findable at all.

Out in the world it is not, and says so. Measured over the surface steps where
the recording is dense enough that nothing can be hiding in between, the
fastest ground speed reached is 15.6 m/s -- and the fastest unbroken step in
the whole database is 15.9 m/s. Torrent at a gallop covers the same ground in
the same time as a short warp, so no speed rule can separate them. What it
looks for instead is the hole: a stretch where the recorder went quiet for
many times its own rhythm and you came out of it a long way from where you
went in. That is what a load screen looks like from the outside -- and also
what a stalled recorder looks like, which is why these are reported and left
alone unless you ask.

There is a third rule, and it is the sharpest of the three because it is not a
threshold at all. Nothing is recorded until you have moved 1.5 m across the
ground, so however long you stand still, the next sample lands the moment you
cross that line -- 1.5 m, plus however far you got in the one reading it took
to notice. That is a ceiling the recorder guarantees, and being carried
somewhere owes it nothing. Anything past it is somewhere you were put.

The ceiling is different for every session, because it depends on how often
that one was sampling: a quarter second of sprinting is 4 m, five seconds of
it is 78. So it is sharp on live capture and nearly useless on an imported
route -- which is exactly where the deaths it misses are.

It measures across the ground only, because that is what the gate measures. A
lift moves you without moving you horizontally, so counting height finds every
lift in the database and calls each one a teleport.

```bash
python tools/repair_jumps.py --gate --write
```

If you marked deaths before the grace was written alongside them, the same
tool reports the ones whose grace the map change swallowed, and
`--graces --write` puts them right without your having to mark them again.

It also reports **stays in a dungeon that never happened**. The position chain
does not go blank while the game loads: it can go on reporting a map you were
in earlier, with the position you were last at inside it. That looks like an
ordinary dungeon reading, so it becomes a visit, a marker on the map, and a
place for a death to be filed that you were nowhere near. The tell is that it
lasts no time at all with a load screen in front of it and solid ground either
side -- every real stay in this database is at least 34 samples over 17
seconds. `--ghosts --write` gives those readings back the last position
anybody knew about, and any death filed on one moves with them.

The report gives you two things to judge them by. **came back** is whether the
route returned to within 20 m of where it left, inside seven minutes: coming
back is what a death looks like, because you go back for your runes, where a
teleport usually carries on from wherever it put you. And **rose** is worth as
much -- a drop of 90 m is the fall that killed you.

```bash
python tools/repair_jumps.py --surface --write
```

That breaks the line at them, which puts a teleport mark on each. From there
you can say which were deaths, on the map or during playback.

A sending gate counts as a teleport like any other, including the ones that
drop you inside a dungeon a long way off: the arc is drawn to where that
dungeon sits on the map, because there is no other way to measure a surface
position against a dungeon's own metres.

### Jumps across a dungeon

There is one more kind of jump, and it is not a teleport: walk in one mouth of
a tunnel and out of the far one, and the surface path breaks by a couple of
hundred metres. No pair of samples shows it -- the rows either side of the gap
are in different coordinate spaces -- so these were invisible. `/api/warps`
returns them now and both ends are marked like any other teleport -- because
some of them are one: a warp back to a cave with a walk either side of it
looks exactly the same from here.

A dungeon is drawn pinned at one doorway, and where a second doorway is known
and the two agree on how far apart they are, it is turned to line up at both.
Where they disagree -- Sellia Crystal Tunnel's two mouths are 86 m apart on
the map and 13 m apart inside -- the interior is simply not laid out to match
the ground above it, no turn will put both right, and it stays correct at the
one door it is pinned at.

## Playback and numbers

**Playback** is the button at the top of the panel. Pressing it clears the
map and walks the route again in the order you made it: the path draws as you
go, and a count of the deaths so far sits at the top.

The timeline runs the whole width of the map along the top of the bar, since
it is the one thing measuring where you are against the whole route, and
nothing sits on it -- the whole width seeks, and the path redraws as you drag
rather than when you let go. Underneath it, in the middle of the map, is play
with a step button either side and the speed either side of that; where you
are in the route is on the left and **I died here** on the right. The speed
slider runs from 30 seconds a second to an hour a second over fourteen
detents, so every position it stops at is a speed worth watching -- spread
evenly instead, most of its travel would sit above half an hour, where one
position looks like the next.

With **By age** as the path colour, the ramp follows the playhead rather than
the end of the route: whatever has just been drawn is in the newest colour and
everything behind it slides down the ramp as the playback goes on. Built once
at the end, the whole path showed its final colours from the first frame,
which gave away how much of the route was still to come.

Walking back into a cave shows the cave, not just the run you are on: the
earlier visits' paths are drawn faintly underneath and their deaths and
teleports are put back, so the count on a mark carries on from where it was
rather than starting again.

A cave dims the world to show itself, because it is under the terrain and
there is nowhere else to draw it. A legacy dungeon does not: it is on the map
at the place it occupies, so its path is drawn there and left there, and every
run through it accumulates the way the world's does.

A jump that would send the view most of a screen or more is gone to at once
rather than glided to, and the playback eases off at every teleport so you can
watch the line drawn across and see where you have landed. How long depends on
how far the jump is on screen: barely a beat at the overview zoom, most of a
second zoomed right in. At the fastest setting the whole route takes 15.7 s
without the pauses and 44 s with them at the overview zoom, 68 s zoomed right
in; at five minutes a second they add about 18%.

The same panel button stops it again -- it reads **Back to the map** while
the playback is on -- and so does Escape. Space pauses, `,` and `.` step a
point at a time.

Every death is drawn on the timeline as a thin red line, so you can see them
coming and scrub to one. Hovering a stretch of path puts a mark on the
timeline for when it was walked, with the moment written above it; clicking
the path, or that mark, goes there.

It plays both planes: where the route goes underground the map goes with it,
terrain, path and marks together, and comes back up afterwards. The session
ticks and the time window still apply, and it follows *played* time rather than the calendar -- a gap longer
than ten seconds passes in a moment, because a route spanning a month of
evenings would otherwise spend most of the playback on a stationary dot.

The map starts empty of everything, marks included. A cave's pin appears the
first time you walk into it, not before -- a playback of a route is partly the
finding, and pins standing there from the first frame give away every place
you are about to reach. Deaths, teleports and the graces you got up at arrive
the same way, and stay. Where several land on the same spot they become one
mark with a count, the way the finished map draws them.

Both ends of a teleport are marked, and where you got up is its own moment
about twelve seconds after the death, so a death reads as the two halves it
is: the mark where you went down, and then the grace it put you back at.

Caves are played as caves. Walk into one and the world dims and the dungeon
appears in its own frame, the same way hovering its marker does, and the
drawing carries on in there until you come out -- at which point the dungeon
goes away and the world comes back up. Go back into one you have been in
before and every earlier run through it is drawn faint under the one you are
walking, the way hovering its marker on the finished map draws them all.
A dungeon something happened in is never stepped over: at five minutes a
second one step covers twenty-four seconds, so a death in a tunnel you were
in for twenty would flash by unseen. The dungeon comes up, the mark
announces itself, and the world returns a moment later. Deaths and teleports announce
themselves as they arrive: the mark starts too small to see, grows out past
the size it will settle at, and comes back to it. A teleport plays as a
journey rather than as two marks at once -- the end you left from lands, a
line runs across, and the arrival lands when it gets there. A mark made inside a dungeon belongs to that dungeon and goes
when it does; one made in the world stays for the rest of the playback.

The **&#8249;** and **&#8250;** buttons either side of play walk the route one
recorded point at a time, backwards and forwards -- `,` and `.` do the same.
At five minutes a second a death goes past in a fiftieth of a second, so
finding one means being able to walk up to it. The line under the scrubber
says which point you are on out of how many.

**I died here** on the timeline is the last resort, and the one that always
works. Some deaths leave nothing to correct: die in a cave whose grace is
seven metres away and the recording has a hole in it and no displacement at
all, which is the same shape as standing still -- and there are hundreds of
those. So step to the point where it happened and say so. `D` does the same
thing.

It marks two things: the death on the point you are standing on, and **the
next recorded point as the grace you got up at**, even when the map changed on
the way -- dying while crossing a tile boundary or a doorway is still dying. That second half is written
as a break in the line rather than as a mark of its own, which does three jobs
at once -- the respawn mark appears, the jump is not also counted as a
teleport, and the path stops being drawn from your body to the grace as though
you had walked it. Clicking the mark takes both back, and puts the line back
together.

Click any death or teleport mark while it plays and it tells you what it
thinks happened, and offers to be corrected. Watching stops while the popup is
open. **This was a death** on a teleport writes the death where you went down
and puts the respawn at the other end; **Not a death after all** on one you
marked takes it back. A death the recorder found from your health reading
offers nothing, because it is not a guess.

That correction is worth having because the tracker often cannot tell.
Without a health reading a death and a teleporter are the same event -- a hole
with a position either side -- and the playback is where the difference shows:
if you jump and then walk straight back to where you jumped from, you died
there. A teleport takes you somewhere you meant to go, and you carry on from
it.

Speeds run from thirty seconds a second, which is slow enough to follow a
single fight, to two hours a second.

Dragging the scrubber moves the clock, and letting go moves the drawing: it
replays from the beginning to wherever you dropped it, without the
animations, so you get the same picture whichever direction you came from.
**Follow the route between maps** decides whether the map switches itself
when the route goes underground and back -- live and during playback. On by
default. With it off the map stays where you put it, and the other plane's
path is not drawn on it.

Somewhere that is nowhere in the world -- the Roundtable Hold has no way in on
foot at all -- gets a disc in the bottom-left corner of the screen instead of a
spot on the map, with its name sliding out beside it on hover. Pressing it
opens a window out of the disc itself -- the path, how far it runs, how much
height it covers -- without moving the map, during playback as well; pressing
it again puts the window away. One disc per
place; the panel lists a row per visit. If one of them is on the map now, its popup has **Take it
off the map**; **Put on map** in the panel brings it back.

**Keep me in view** works during playback too.

**Numbers** is what the whole route adds up to. Distance travelled is broken
out by plane -- overworld, underground if you have been down there, and inside
dungeons -- because the three are measured differently: the first two from a
world position, the third from a dungeon's own metres. Then time recorded,
the average and longest session, deaths and deaths an hour, teleports, caves
and dungeons entered, legacy dungeons, and sessions.

Three things it does deliberately. Distance skips every teleport, since a warp
is not ground you covered. Time recorded caps each gap at ten seconds, so it
means time played rather than time the recorder was left running -- and the
session lengths use the same clock, or a recorder left on overnight would be
the longest session you ever played. And a session that recorded no time at
all is left out of the average, because the one-sample rows a port clash
leaves behind are not short sessions, they are sessions that did not happen.

Legacy dungeons are counted apart from caves: they are the areas whose label
in `[maps.area_labels]` says so, which keeps the split in step with what the
markers call them. Divine Towers stay with the caves -- a tower is a small
dungeon you clear, not a castle.

## Making the path readable

The map is a painting, and a thin line over the busy parts of it disappears.
**The path** section in the side panel is there for that:

- **Type** -- *By elevation* (the height ramp), *By age* (see below), or
  *One colour*, which reveals a colour picker.
- **Oldest** -- how far back the age gradient reaches. See below.
- The **slider down the right-hand edge of the map** is the light on the
  terrain: sun at the top, moon at the bottom. Drag it down and the painting
  recedes; the route, the marks and the compass stay where they are.
- **Line thickness and outline**, folded away at the bottom of the section --
  how heavy the line is, and how much dark edge it carries. The outline is
  what makes a pale line readable over pale terrain, so it is worth more than
  thickness when something is hard to see. Both are set once and then left
  alone, which is why they are out of the way.

All of it is remembered between sessions, and none of it refetches anything --
it is a redraw.

**By age** colours the line by when you walked it: the newest end is the warm
colour of the position mark, and the further back a stretch is the further it
drifts towards a cold blue. The ramp under the controls shows the span. It is
the answer to "which of these tracks did I make today".

**Oldest** decides how far back that gradient reaches. It starts at
*everything recorded*; move it up and the horizon comes in -- the last week,
the last day, the last three hours, down to the last fifteen minutes -- and
the whole ramp is spent on what is inside it. Anything older is drawn in the
oldest colour rather than being hidden: the route is all still there, it just
stops competing for the gradient. Use it when a single evening's play is the
thing you want to read and the rest is background. The ramp says where the
horizon fell, e.g. `Sep 4, 09:31 PM and older`.

Both the age colouring and the **Time** slider move by *sample count*, not by
the clock. Recording is lumpy -- an afternoon in August and then a month of
nothing -- so by the clock almost every sample sits at the very end: the whole
colour ramp went to one old afternoon, and the slider did nothing until its
last few pixels. Each step now moves the same amount of route. The window has
both ends on one track, and it filters the dungeon paths and the marks along
with the route.

A compass sits in the top-right corner, from `viewer/compass.png`. Replace
that file to change it -- anything with a transparent background works, and it
is fitted rather than stretched. If the file is missing the viewer falls back
to one drawn in SVG, so this still works with no image at all.

## Surface and underground

Siofra, Ainsel and Deeproot share the overworld's grid but sit far below it,
so they are a different map rather than a layer on top of one. **Map** in the
side panel switches between them; the route, its marks and the background all
follow. The underground pyramid ships with the repository, so the terrain
switches too -- built with `build_map.py --map M01` and tiled into
`viewer/tiles-underground`, which is where the viewer looks for it by name. If
that directory is ever missing, the surface stays up behind the route and the
panel says so rather than showing a black screen.

## Marks on the map

| | |
|---|---|
| **▲** | A cave, catacomb or tunnel -- one mark per place however often you go back, with a count when you have. The pin's tip is the entrance, and it grows with the time you have spent inside. Hover to see the path in there, or click for a popup with a **Show path** button per visit |
| **♜** | A legacy dungeon or Divine Tower, draggable if it is in the wrong place. Same behaviour, paler, and every path through it is on the map at all times rather than only on hover |
| **✕** | You died. A number means you died there more than once, and the popup lists when |
| **✦** | You arrived here by teleport |
| **✧** | You left from here by teleport -- the dimmer twin of the mark above. Hover either end to draw the jump between them |
| **✹** | Where you respawned afterwards, in the amber of a grace |
|  | Hover either mark and the line between them shows what dying cost you |
| **◉** | Where you are now, while the recorder is running |

**Name a place** from its own popup: type into the field and press Enter. The
name replaces "Cave" everywhere that place appears -- its marker, its popup,
every death and teleport inside it, the unplaced list, and the status line
while you are in it. Clearing the field puts the generic label back. Every
cave is called "Cave" until you say otherwise, and the map ID is the only
thing telling two of them apart.

A dungeon's marker carries two numbers: how many times you have been in, on
the bottom right, and how many times it has killed you, on the top right in
the red the death marks use. The popup says the second one in words. Both
follow the **Time** window, so they always agree with the marks around them.

Marks appear as they happen -- a death, a warp, a dungeon you just came out of
-- rather than waiting for the next time the route reloads.

**Where you got up** is the other half of a death: dying puts you back at a
grace, and the pair says how much ground that cost you. It arrives about
twelve seconds after the death, because that is how long the load screen
takes, and it appears on its own without a zoom. Deaths in the same place
cluster into one mark with a count, and so do respawns -- five deaths to the
same boss is one mark and one grace, both reading 5. Die inside a castle and
the grace is usually in there too, so both marks are drawn on that castle's
own path rather than piled on its entrance.

A death inside a cave is drawn the same way, on that cave's own path, and the
count on the cave's pin is the other half of it. It is deliberately not also
drawn as a mark at the cave mouth: it has no world position of its own, so the
only place to put such a mark is the entrance, where it would repeat the pin's
badge a few pixels away and claim a spot the death did not happen in. Untick
**Caves and dungeons** and those deaths come back to the entrance, since with
the pins gone there is nothing left showing them.

A death with no load screen after it -- which happens, rarely -- gets no
respawn mark rather than a guessed one.

The map does not glide. There is no flick momentum after a drag and no
animated zoom: both of them move the map while the route sits still, because
the path is drawn on a canvas that is stretched during an animation and only
redrawn once it settles. The tiles are files on your disk, so arriving at the
new zoom straight away costs nothing, and the path is the right size in every
frame it appears in.

**Keep me in view** pans the map when your position would otherwise leave the
screen. It only moves when it has to, so you can still look around while it's
on; the mark is allowed to sit near the edge.

**Edge margin**, under it, is how near the edge that is. Drag it and a dashed
boundary appears on the map showing exactly where the line falls -- a number
of pixels means nothing until you can see it. A small margin lets you walk
almost to the edge before the map moves, which is calm but leaves you little
warning of what's ahead; a large one keeps you nearer the middle at the cost
of moving more often, and at the top of the slider you are simply held in the
centre. It works in fractions of the way to the middle rather than in pixels,
so the top of the range centres you on both axes however the window is
shaped. The boundary fades out on its own a moment
after you stop dragging, and is remembered between sessions.

While you are inside a dungeon, the viewer draws that dungeon live -- every
run through it, not just this one -- and dims the world behind it, so what you
are walking is the bright thing on screen. It stops when you come out.

## Death marks

Every time your HP hits zero, a mark goes on the map where you fell, with the
time and the place in its popup. Toggle them with **Deaths** in the side panel.
They appear the moment you die, not on the next refresh. Dying in the same
place twice gives one mark with a count rather than two marks stacked, and a
death inside a dungeon sits at its entrance until you hover the dungeon, which
puts it where it happened.

This is the one feature that needs a pointer beyond position and map ID, so it
is optional: `[memory.pointers.player_hp]` in `config.toml`, taken from the
practice tool's `character_points` chain. Everything else works without it, and
a wrong chain costs you nothing but the marks.

Check it before trusting it:

```bash
python -m tracker.main probe
```

It prints HP beside each reading and says plainly whether the number looks like
health. If it doesn't, comment the block out.

Routes imported from the old Nexus tool have no marks: it never recorded HP.
Their deaths still break the line, because it did record the load screens.

## Layout

```
tracker/
  coords.py    map IDs, layer classification, world/pixel transforms
  memory.py    AOB scanning and pointer resolution
  source.py    game reader and simulator (interchangeable)
  sampler.py   recording loop, teleport breaks, map events
  store.py     SQLite schema and queries
  server.py    HTTP + WebSocket, path simplification
  main.py      CLI
viewer/        Leaflet front end (Leaflet vendored, works offline)
tools/         make_tiles, calibrate, import_legacy, repair_breaks, selftest
*.bat          double-click launchers for Windows
config/        config.toml
```

## Tuning

In `config/config.toml`:

- `interval_s` — poll rate. 0.25 is plenty; lower it for finer traces at the cost of database size.
- `min_move_m` — don't record until you've moved this far. Raise it if the database grows faster than you like.
- `max_speed_mps` — teleport threshold. Set a bit above Torrent's sprint. Lower it if warps still connect; raise it if normal riding gets chopped up.
- `underground_y_max` — the surface/underground split. Stand in a river area and check the height readout in the viewer, then set it between that and your usual surface height.

## Troubleshooting

**Viewer says the recorder is an older version** — you replaced the files while
`record` was running. Python loads the server code once at startup but serves
the viewer's files fresh from disk, so you end up with a new page talking to an
old server. Stop the recorder, start it again, hard-refresh the browser.

**Only part of the map renders, or it's cut off at an edge** — `[viewer]
image_width` / `image_height` are smaller than the image you actually tiled.
Leaflet clips the tile layer to those numbers, so the rest never gets
requested. The recorder measures the tile pyramid at startup and prints the
correct values when they disagree; paste them in and restart.

**"database is locked" while importing** — a `record` is still running and
holding the write lock. Stop it with Ctrl-C, which commits what it has, then
import.

**I deleted the wrong session** — there is no undo; restore `data/routes.db`
from a copy. The deletion is a single transaction, so a session is either
fully gone or untouched: you will not find half of one left behind.

**build_map.py finds no files** — point it at the folder of converted
PNG/DDS tiles, not the packed `.tpfbhd` archive.

**"pattern for 'player_position' not found"** — the game updated, or the
signature was wrong to begin with. Pull fresh patterns.

**"could not attach to eldenring.exe"** — game isn't running, or it needs the
same privilege level. Run the tracker as administrator.

**The inset says no path was stored for a visit** — either the visit predates
interior paths being kept, or you never moved `min_move_m` while you were in
there. The marker is drawn from the map event, which exists either way.

**Nothing appears while I'm in a cave** — expected. Interiors have their own
local coordinate space, so they're stored but drawn as a marker at the entrance
rather than as a line on the world map. The viewer's status reads "Inside
<name>" while you're in one, and the marker appears when you come out. Click
it and hover the marker to see where you walked in there, drawn at the
entrance.

**Route advances a few seconds then snaps back** — the position chain is the
rebasing physics one. `player_position` should be `[0x1E508, 0x6C0]`. Run
`probe` to confirm.

**Route sits off the map, or nothing shows at all** — before calibration the
projection is 1:1, so the route lands far from the image. The viewer opens on
the route rather than the image for this reason; "Fit to route" re-centres it.
Once the map is tiled, calibrate so the two line up.

**"only one usage of each socket address"** -- something already has port
8731, and almost always that something is this tool. If it is a recorder, a
second one would split the route between two windows, so the new one stops and
says which session the first is writing; go back to that window, its map is
already live. If it is just a map you left open, the recorder takes the port
off it and that window closes itself. Anything else, or two games recorded at
once on purpose, wants a different port:

```bash
python -m tracker.main record --source game --port 8732 --open
```

None of this touches the database: the port is checked before the session is
started, so a refused launch leaves nothing behind.

**The Roundtable Hold, or anywhere else you only warp to**, has no place the
route can work out -- there is no way in on foot, so nothing recorded says
where it is. It appears under *Interiors with no place on the map*, with
**Show path** to draw it in the inset and **Put on map** if you know where it
belongs: click that, then click the spot, and it stays there. Escape cancels.
A position set that way outranks everything the tracker infers, and the marker
can be dragged afterwards like any other.

The Hold ships placed in the bottom-left corner, which is where the game's own
map screen shows it.

**A dungeon drawn where you warped from** was recorded before the tracker
noticed that being unable to read the game is itself a load screen. It cannot
happen again, but an anchor already recorded stays until something outvotes
it: from the third visit to that place the odd one out loses automatically,
and until then dragging its marker fixes it for good -- a position set by hand
outranks everything.

**A dungeon is drawn in the wrong place** -- if you got there through a
transporter trap, this is now handled: the chest is not treated as the
dungeon's door, and the dungeon is placed where you walked out instead. Old
routes imported from the Nexus tool are corrected too, the first time the same
trap is recorded live. If something is still wrong -- a place you only ever
warped into and warped out of has nothing to be placed by -- drag its marker
to where it belongs. A position set by hand outranks everything worked out
from the route.

**A straight line through the rock inside a cave**, with no teleport mark on
it, is a lift or a teleporter recorded before the tracker could see inside a
dungeon. It is fixed going forward; for routes already recorded:

```bash
python tools/repair_jumps.py
```

That reports what it would change and writes nothing. It looks for two
things, both inside dungeons only and both only at steps with no break
already: a step too fast to have been walked, which is a lift or a
teleporter, and a hole in the recording you came out of a long way from where
you went in, which is a death or a warp. It prints the fastest step and the
furthest quiet stretch it is *leaving alone*, so you can see the margin before
running it again with `--write`. Nothing is deleted; only the break flag
changes, and `tools/repair_breaks.py` undoes it.

Standing still leaves a hole in the recording too -- the tracker stores
nothing while you do not move -- so only holes you came out of far away are
counted.

**A line drawn from where you died to the grace** means the load screen was
never recorded. Going forward the tracker uses HP instead, which cannot miss
it; `python tools/repair_jumps.py` finds the ones already recorded, from the
deaths themselves.

**A death and a teleporter look identical** once the HP reading is gone, so
the repair pass draws a teleport and lets you correct it: click either end of
one and choose **This was a death**. The mark moves to where you went down,
the place it jumped to becomes a respawn mark, and the teleport pair
disappears. Click the death and choose **Not a death after all** to undo it --
that only ever removes marks you made, never one the tracker found itself.

**A banner says the page is out of date with its own script** -- your browser
kept an older `index.html`. Reload with Ctrl-Shift-R once and it will be
right; the viewer's files are now sent with `no-store`, so it cannot happen
again. The map still works while the banner is up, minus whatever control is
missing.

**A banner says the recorder is older than the page** — it is. Python loads
`server.py` once at startup and serves the viewer fresh from disk, so a
recorder left running across an update pairs a new page with an old server.
The two now exchange a version number and the page says so plainly instead of
quietly missing features. Stop the recorder, start it again, reload the page.

**Nothing on the map is clickable, and the markers drift as I zoom** — fixed;
if you still see it, hard-refresh. Two CSS faults: the marker styling knocked
the icons out of absolute positioning so they were laid out in normal flow and
displaced by a fixed number of screen pixels, and the interior overlay's canvas
covered the whole map and absorbed every click.

**The map flickers or lags when I zoom** — the route is simplified on the
server with an epsilon tied to zoom, so it is only refetched when the zoom
crosses a whole level, and the old line now stays up until the new one has
arrived rather than being cleared first. Tiles are fetched during the zoom
animation rather than after it, since they are local files and cost nothing.

**The live route stops moving until I zoom** — fixed. Reloading the route from
the server used to clear away the polyline the live feed was still drawing
into, so new positions were appended to a layer that was no longer on the map.
The feed now has its own layer group and is redrawn after every reload. If you
see it again, the recorder is the thing to check: `record` prints each sample
it stores.

**Everything shows as one layer** — `underground_y_max` needs tuning for your
readings.

**Console says "unclassified area N"** — add `N = "Some Name"` under
`[maps.area_labels]`. Area 255 is the exception and never appears: the game
reports map ID `0xFFFFFFFF` while no map is loaded, so those readings are
dropped rather than recorded. Older databases that captured them stop drawing
the phantom markers on their own.

**The path has holes in it** — short gaps where the line stops and restarts,
most visible on imported routes. Those are line breaks left by an older version
that treated every overworld tile crossing as a teleport. The recorder no
longer does that; to clear the stale flags out of a database written before the
fix:

```bash
python tools/repair_breaks.py --dry-run
```

It reports what it would change, and drops `--dry-run` to do it. Only breaks
between two points on the same map plane within 200 m of each other are
cleared, and no samples are touched.

A gap where you entered a cave is different and stays: interiors have their own
coordinates, so the surface line genuinely stops at the entrance and picks up
where you came out, with a marker in between.

**Deaths aren't marked** — run `probe`. Either `player_hp` isn't in
`config.toml`, or it doesn't resolve, and the probe says which. HP is read
through `net_players_ins` rather than `PlayerIns`, so it is a different chain
from the position one and can break independently of it.

**Warps still draw a line** — lower `max_speed_mps`. If they persist through
that, the map ID read is probably wrong, since a warp almost always changes it.

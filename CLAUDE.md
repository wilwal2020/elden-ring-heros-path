# CLAUDE.md

Context for working on this repo. `docs/README.md` is the front page,
`docs/SETUP.md` gets it running and `docs/MANUAL.md` is the reference; this
covers the things that cost time to discover and are not obvious from reading
the code.

The layout, from 2026-09-10: the root holds the two launchers, a README stub
for GitHub, and nothing else a newcomer has to read past -- with
`docs/`, `config/`, `tools/`, `tracker/`, `viewer/`, `assets/` and `data/`
beside them. This file stays at the root because that is where Claude Code
looks for it; move it and it stops being loaded.

## What this is

A route tracker for Elden Ring: reads the player's position out of the running
game, stores it, and draws it on a map in a browser. Built to replace a Nexus
mod (Route Tracker, #9294) that broke on patch 1.17, fixing four specific
complaints:

1. Every session wrote a separate file, so seeing the full path meant opening
   all of them → one SQLite database, sessions as a column.
2. The map reloaded slowly on every scroll → tile pyramid, canvas rendering,
   zoom-aware simplification.
3. Caves looked like overworld wandering → interiors excluded from the world
   plane, drawn as markers.
4. Teleports drew a line as if walked → line breaks on map change or
   implausible speed.

Single user (William), Windows, Git Bash, Python 3.14. Game version 2.7.0.0.

## Verify changes with

```bash
python tools/selftest.py
```

Simulates a route, serves it, exercises every endpoint. No game needed. If you
change sampler or storage semantics, update the assertions — they encode
intent, and one of them already caught a regression when interior handling
changed.

`--source sim` on `record` runs the whole pipeline without the game. Use it.

## Gotchas that cost real time

**Position chain.** Use `global_position` (`[0x1E508, 0x6C0]`), NOT the
practice tool's `chunk_position` (`[0x1E508, 0x190, 0x68, 0x70]`). The latter
is the physics position, whose origin the game rebases every few seconds so
float precision doesn't degrade. It sawtooths: the route advances ~3s, snaps
back, advances again. This was live for several rounds before being caught,
because the numbers look perfectly plausible in isolation — it's only visible
watching it draw. `probe` now detects it (large jumps within one map tile).

**Restart after editing.** Python loads `server.py` and `config.toml` once at
startup but serves `viewer/` fresh from disk. Editing files while `record` is
running gives a new viewer talking to an old server, which produces confusing
404s rather than an obvious failure. The viewer detects this specific case and
says so.

**`[viewer] image_width/height` are hand-copied from `make_tiles.py` output
and drift.** Leaflet clips the tile layer to them, so a stale value hides part
of the map and reads as a half-finished pyramid rather than a config error.
`tile_warnings()` in `server.py` now measures the pyramid on disk at startup
and says what the values should be; `selftest.py` asserts both that the config
agrees with the tiles and that a wrong one is reported.

**Two-point calibration fits are always exact.** Two unknowns per axis, two
equations, residual always 0. Reporting that residual gave false confidence
for several rounds. Three points minimum for a meaningful check.

**Signature values come from veeenu's practice tool.** In
`xtask/src/codegen/aob_scans.rs` the two integers after each pattern array are
exactly `rip_offset` and `instruction_length`. Offset chains are in
`lib/libeldenring/src/pointers.rs`, listed per game version. The AOB pattern
normally survives patches; the raw offsets do not.

**Coordinate space is auto-detected.** `MapConfig.is_absolute()` decides from
magnitude whether coordinates are tile-local (add the tile's grid origin) or
already world-wide. `position_space` under `[maps]` forces it. This exists
because it was not possible to determine in advance which `global_position`
returns.

## Known-uncertain, worth verifying against reality

- **`[maps.area_labels]`** — the mapping from area number to dungeon type
  (30 = Catacombs, 31 = Cave, etc.) is inferred, not verified. Unknown areas
  print `unclassified area N` once rather than being guessed at. If the user
  reports one, that's ground truth; add it. Area 255 is not one of them:
  `0xFFFFFFFF` is the no-map sentinel (loading screen, main menu, character
  not yet spawned) and `is_no_map()` drops those readings in the sampler.
  Before that, every load screen became a 1-second visit with a marker, and
  the console told the user to classify an area that does not exist.
  `interior_visits()` filters them out of existing databases too.
- **`underground_y_max = -60.0`** — the surface/underground split. Still not
  tuned against real readings: the live sessions in `routes.db` run y -10 to
  129, all surface, so nothing has exercised the threshold yet. The
  underground cluster visible in that database is simulator output (the
  simulator parks underground at y = -180). Siofra and Ainsel are the test.
- **The `player_hp` chain** is copied from the practice tool but has never been
  read against the running game here. If `probe` prints something that isn't
  health, that is the thing to fix; the simulator produces deaths, so the rest
  of the path (event, endpoint, marker) is exercised without it.
- **`[maps.area_origin]` for area 61** (Shadow of the Erdtree) is a placeholder
  that parks the DLC map beside the base map. Untested.
- **`build_map.py` flag heuristic** — picks the tile variant with the most
  map-fragment bits set. Derived from a community write-up
  (nexusmods.com/eldenring/articles/79), tested only against synthetic tiles.
  Symptom of it being wrong: patches of the stitched map in the brown
  undiscovered style.

**Imports replay through `Sampler`, they do not write rows.** `import_legacy.py`
builds a `ReplaySource` and hands `Sampler` a `clock` that returns each point's
own timestamp. Writing samples directly is the obvious shortcut and it means a
second copy of the movement gate, the teleport break, the load-screen drop and
the interior rule -- which then drift apart. The clock is resolved in
`__init__` rather than as a default argument, because a default binds `now_ms`
before `selftest.py` can replace it.

**The old Nexus tool's `global_x`/`global_z` are only right on the overworld.**
Inside a dungeon it added a tile origin to local coordinates anyway, which is
the "caves look like overworld wandering" bug this project exists to fix. The
importer therefore reads local `x`/`z` plus the map ID and lets `to_world()`
decide. On overworld points the two agree to 0.00 m, which is worth checking on
any new import -- `world_check()` reports the worst disagreement.

**The respawn was already being detected, and thrown away.** `warps()` finds
the jump from where you died to where you got up and then drops it on
purpose, because a death mark already sits on one end and two icons on one
spot say less than one. That was the right call for the warp list and the
wrong one for the question "where did that put me". `Store.respawn_after()`
reads it back out of the samples -- the first row after the death carrying
break code 3, within three minutes -- so a respawn needs no column, no event
and no second fetch: it comes back attached to the death it belongs to and
cannot go stale against it. On `routes.db` 31 of 33 deaths have one, 11-15 s
later and 25-174 m away. The other two have no load screen after them at all
and are left unmarked rather than given an invented grace.

**A respawn cannot be drawn when the death happens.** The mark for it does
not exist yet -- the load screen is twelve seconds away -- so the death
handler draws a death and nothing else, and it is the *reload break* that
brings the respawn in: `if (s.break === BREAK_RELOAD) loadDeaths()`. The
interior sample payload carries `break` for the same reason, since getting up
inside a castle is a load screen like any other and would otherwise wait for
the next zoom.

**HP is the only reliable sign of a respawn, and the break belongs to the
moment you come back.** The load screen after a death mostly does not
announce itself: nine deaths in m31_10 on 2026-09-06 produced two sentinels
between them, so seven respawns were drawn as a walk from the body to the
grace -- 21 to 47 m at four or five metres a second, which no speed rule
catches and no gap rule reaches without a threshold.

Two wrong places to put that break, both tried. Not at the death: that is the
last thing you walked to. And not at the reading straight after it either --
the body settles first, and on that data it stored steps of 1.6 to 5.5 m
before the grace arrived, so marking those broke the line in the wrong place
and left the walk to the grace still drawn. The sampler sets `self.dead` when
HP hits zero and turns it into `reloaded` on the first reading where HP is
back, which needs no threshold and cannot be missed.

For routes already recorded, `find_respawns()` in `repair_jumps.py` does the
same job from the recorded deaths, and there it does need to tell the corpse
from the grace: 6 s and 10 m, chosen from the empty band the data leaves --
the corpse within 4.0 s and 5.5 m, every grace beyond 8.0 s and 21.3 m.

**A load screen is only a sign of anything when the game reports one.**
`self.reloaded` was set on exactly one thing: a reading that came back with
the `0xFFFFFFFF` sentinel. But `GameSource.read()` returns None for every kind
of failure -- the pointer chain unresolvable while the world is torn down, an
exception, the process gone -- and `_step` returns early on None without
marking anything. On 2026-09-06 at 20:13 the recorder read *nothing at all*
for 73.7 seconds, which is the map menu and then a warp, and came back inside
a cave with `reloaded` still false. So the cave's anchor was written as the
grace 3,242 m away that the warp started from, and the whole cave was drawn
there.

The sampler now counts consecutive failed reads and treats a run of them as a
load screen. Counted in reads, not seconds: a replay of an imported route
never fails a read -- every point in the file is a successful one -- and a
wall-clock threshold made a file sampled every five seconds look like one
continuous load screen, which broke three placement checks at once. The floor
is four reads, so ordinary jitter is not a teleport; the fixture proves both
halves, forty missed reads withholding the anchor and two leaving it alone.

**Entrances vote, from the third visit on.** The medoid that settles
disagreeing doorways now settles disagreeing entrances too, because a warp the
game never announced produces an anchor that looks exactly like one. It is
skipped for a pair: two anchors 3,242 m apart have no majority and picking
either would be a coin toss dressed up as an inference. With three or more it
works -- on `routes.db` it pulled two m31_15 anchors that were 775 m and 517 m
from that cave's mouth back onto it.

**A load screen is the only reliable sign of a respawn.** Dying puts you back
at a grace that can be a few hundred metres away, reached in the seconds the
death animation and load take -- under `max_speed_mps`, so the speed check
passes it and the line gets drawn from where you died to where you got up. The
map ID often does not change either, since the grace is usually in the same
tile. What always happens is a load screen: `0xFFFFFFFF`, which the sampler
already drops. `self.reloaded` remembers that it saw one and breaks the line on
the next real reading. `samples.break_before` now carries the reason (1 map,
2 speed, 3 reload) so a repair pass can tell a stale tile-crossing break from
one that was always real.

**HP comes from a different chain than position.** `player_hp` walks
`WorldChrMan -> net_players_ins (0x10EF8) -> [0] -> 0x190 -> [0] -> 0x138`,
which is the practice tool's `character_points`; position walks `PlayerIns`.
They can break independently across a patch. HP is optional everywhere: absent
or unresolvable means no death marks and nothing else changes, `probe` reports
what it read, and `GameSource._read_hp()` warns once rather than per reading.

**An overworld tile crossing is not a teleport.** The map ID changes every
256 m in the open world, and the break rule used to fire on any map change.
Live at 0.25 s that leaves 2 m holes nobody notices; replayed from the Nexus
tool's 5 s samples it left 69 holes of up to 65 m in 1,833 points, which is
what "gaps in the path" turned out to be. `MapConfig.same_plane()` decides
whether two IDs are tiles of one continuous world; the speed check still
catches a warp between two tiles. `selftest.py` asserts no break exists
between two same-area points within 200 m, and `tools/repair_breaks.py` clears
the stale flags out of databases written before the fix.

**The Nexus tool's route files are cumulative, not consecutive.** Three files
from one afternoon were nested: 73 points, 236, 262, each a byte-identical
prefix of the next. Importing the folder draws that session three times, and
nothing shows it afterwards except a path that looks heavier than it should.
`already_imported()` matches exact millisecond timestamps -- only a fraction
match even for a pure duplicate, because the movement gate dropped the rest on
the way in, so the threshold is deliberately low.

**The recorder used to hold an open write transaction while idle.** The commit
lived at the end of the path that stores a sample, so standing still (an early
return at the movement gate) meant `idle_flush_s` never came around: samples
sat uncommitted indefinitely, invisible to the viewer, lost on a crash, and
holding a write lock that made every other tool fail with "database is locked".
`step()` now always calls `_maybe_commit()`. Found because an import could not
write while a recorder had been idle for twenty minutes with nothing committed.

**And so does the live feed inside a dungeon.** The position mark moves on
every websocket sample; the line behind it came from `/api/interior`, which
only knows what the recorder has committed, redrawn by an overlay that asks
every five seconds. So inside a cave the path trailed the mark by up to five
seconds and looked like it was struggling to keep up. The surface has had a
live tail for exactly this since the beginning -- `state.liveInside` is the
same idea in the dungeon's own metres, drawn through `inside.transform`, which
is the transform the position mark uses, so the two cannot disagree about
where a local point is.

It lives in `insideLiveGroup` rather than `insideGroup` for the reason the
surface one lives outside `routeGroup`: drawing the dungeon clears
`insideGroup`, and a tail in there would be taken off the map every five
seconds by the very redraw meant to be keeping it current. `drawInside()`
redraws the tail on the next tick instead, so it comes back over whatever
frame the dungeon is now drawn in. A `break` on a sample pushes a null into
the buffer and splits the line there, because a load screen inside a dungeon
is a death or a lift and the line should not be drawn through the rock.

**The live feed needs its own layer group.** `reload()` calls
`routeGroup.clearLayers()`, and the websocket's polyline used to live in that
group: after any reload the feed was appending to a layer that had been taken
off the map, so the route stopped moving until the next zoom pulled it back
from the server. The symptom reads as "the recorder stopped", which is the
wrong thing to go looking at. `liveGroup` is separate now and `redrawLive()`
rebuilds it after each reload, keeping the uncommitted tail visible.

**`last_surface` is only the entrance if you walked in.** Warp from a grace
straight into a dungeon and the last surface position is where you were
standing before the warp, which put the cave's marker -- and the path drawn at
it -- somewhere else entirely on the map. `self.reloaded` is still set when the
map-change block runs, so the enter event is written with no anchor at all
rather than a plausible wrong one; the exit fallback in `interior_visits()`
places it properly the first time you walk out.

**The frame was only ever learned for dungeons you can see.**
`learnDungeonFrame()` was called from inside `drawWorldVisible()`, which is
handed `visits.filter(v => v.world_visible)` -- so Stormveil and the Divine
Towers got a frame and no cave ever did. Without one, `interiorTransform()`
falls through to pinning each visit's own first step to the shared anchor, and
that is right only for a visit you walked in on. On `routes.db`, m31_10 has
eight visits: five walked in at local (2-3, 84-86) and three warped to a grace
at (27, 64). All eight were drawn setting off from the same pixel, so the runs
fanned out in different directions from a common origin -- which is exactly
how it reads on the map. Learned from every visit now: the door pins to the
anchor, and the grace lands 32 m inside it where it belongs.

**Standing in a dungeon should show every run through it.**
`refreshLiveInside()` drew `showInside([open], true)` -- a group of one -- so
walking back into a cave you knew well showed a single fresh line and nothing
you had ever done there, while hovering its marker showed all of them. It
passes the whole group now, the open visit first because the position mark
rides on `drawn[0]`.

**A visit does not begin at the door.** The interior drawing pinned each
visit's own first recorded point to the entrance marker, which is right only
for a visit you walked in on. Warp into Stormveil and the first thing recorded
is the grace you appeared at: in `routes.db` that is local (-260.5, 111.9)
against the doorway's (30.65, 45.88), so the whole castle was drawn about
300 m from where it belongs, setting off from the entrance while the player
was somewhere else. The frame is now a property of the dungeon --
`state.dungeonFrame` holds one (local origin, map position) pair taken from a
visit whose `placed` is `entrance` -- and every visit to that map is drawn in
it.

**Dimming is for things that would otherwise be lost.** A cave's path has the
whole route around it to disappear into; a legacy dungeon is drawn out in the
open at all times, so dimming the world to show one turns the lights off for
nothing. `drawInside()` dims only when the dungeon is not world-visible, and
takes that dungeon's permanent drawing off the map while the overlay is up so
the two are not stacked.

**Going through a door is still walking, so the door has a position.** A
Divine Tower entered from inside Stormveil looked unlocatable -- no surface
sample belongs to it -- but the sample before the map changed is a real
position in Stormveil's own coordinates, and Stormveil's frame ties those to
the world. `through_the_door()` reads the last local sample in the map you
came from, offsets it by that frame, and places the tower at the door you used
rather than at the castle's front gate. On `routes.db` that lands 4.6 m from
where the user had placed the same tower by hand, against 130 m for the
neighbour-anchor guess it replaced. It needs no new columns: the samples were
always there.

**Where the tracker cannot know, it asks rather than guesses.** A death and a
teleporter are the same event once the HP reading is gone: a hole with a
position either side. `repair_jumps.py` finds the hole and cannot say which it
was, so it draws a teleport and every teleport mark offers *This was a death*.
Saying so writes a `death_by_hand` event at the step before the jump -- where
you went down -- and everything else falls out of machinery that already
existed: `warps()` drops jumps within thirty seconds of a death, so the
teleport marks disappear; `respawn_after()` finds the arrival, so the respawn
mark appears at the other end. Its own event kind, not a flag, so taking it
back can never delete a death the HP reading found.

That needed `respawn_after()` to accept break code 2 as well as 3 -- a death
marked on a jump the repair pass found by speed still put you somewhere. Not
code 1: walking through a door is a break too, and reading that as a respawn
turned one death on the surface into a respawn inside the catacomb it was
standing next to. Measured across 46 deaths: codes 2 and 3 change nothing that
was already right.

And `/api/deaths` had to stop dropping deaths with no anchor. A death marked
inside a cave has only the dungeon's own metres -- there is nowhere on the
world map to put it -- and it is drawn on that dungeon's path like any other
interior death, but the endpoint skipped it entirely and the mark vanished the
moment it was made.

**A death is slow, so no speed rule will ever find one.** The animation and
the load screen take eight to fifteen seconds together and the grace is close,
so a respawn inside a catacomb implies five to eight metres a second --
walking pace. `repair_jumps.py` at 15 m/s went straight past five of them in
one visit to m30_01. What gives it away is the hole in the recording: that
session sampled every quarter second and reported nothing for thirteen, then
came back ninety metres away, where walking it would have left fifty samples
along the path. So the second rule is a gap measured against the session's own
median stored interval -- the same database holds quarter-second capture and
five-second imports, and a fixed number of seconds means nothing across both.

Standing still leaves an identical hole, because the movement gate stores
nothing while you do not move, so the distance is the only thing separating
them -- and on `routes.db` it separates them completely: of 343 quiet
stretches inside dungeons, 337 end within 16.5 m of where they began and six
end between 32.9 and 96.1 m away, with nothing in between. Five of the six are
that one visit to m30_01, each rising the same 21 m back to the same grace.

The rule stays inside dungeons for the same reason the speed rule does. The
surface has twelve stretches of the same shape and every one of them is real:
Torrent at a gallop covering 135 m in 8.5 s, a 104 m fall, and a 25-second
stall that moved 65 m at walking pace -- which is the alt-tab the speed check
was written to tolerate in the first place.

**The recorder's speed ceiling is wrong for repairing old data.** 40 m/s is
right live, where a quarter-second interval turns small position noise into
large implied speeds. Replayed at the old tool's five seconds it means a 200 m
stride, and `tools/repair_jumps.py` scanning `routes.db` with it found nothing
at all -- while five lifts sat there drawn as walked lines through the rock.
The threshold has to come from the data, and the data gives a clean one:
inside dungeons, 13,875 unbroken steps, five of them between 17 and 26 m/s and
the band from 10 to 16 completely empty; on the world plane, 39,484 unbroken
steps whose fastest is 15.9 m/s, which is Torrent at a gallop, with no gap
anywhere. So the pass runs inside dungeons only -- surface jumps were never
the broken case, since the speed check always worked where there was a world
position -- at 15 m/s, and on `routes.db` the fastest step it leaves alone is
9.0 m/s against a slowest jump of 17.2. All five rose 19 to 55 m while
covering 107 to 143 m, which is a lift.

**Everything that filters on a world position forgets about dungeons.** The
speed check read `wx`, which is NULL inside a dungeon, so a teleporter in
Stormveil produced no break at all and the line was drawn straight through the
rock; `warps()` required a world position at both ends, so those jumps were
never returned and could not be drawn even where there was a path to draw them
on. Both now fall back to local metres when the map has not changed -- inside
one dungeon those are metres like any other. `routes.db` had 22 such jumps,
none of them visible.

**Marks have to be loaded before the thing that draws them.** The interior
drawings put deaths and teleports on their paths by reading `state.deathList`
and `state.warpList`, and boot fetched those *after* `loadInteriors()`, so the
first draw of every dungeon had neither -- they appeared only once something
else triggered a redraw. Deaths and warps are fetched first now.

**A load screen means a warp coming off the surface, and nothing at all
between two interiors.** Walking into a cave is seamless -- on `routes.db`
every dungeon entered on foot carries break code 1 on its first sample -- so a
load screen there says you were put inside rather than walked in. The door out
of Stormveil into a Divine Tower loads like any other door, and `through_the_
door()` is right about it: the existing teleporter fixture has a `0xFFFFFFFF`
in the middle of walking through, and asserting on the load screen alone
turned all six of that tower's doorways into nothing. Both guards therefore
ask `layer_of[came_from] == "surface"` first.

**A transporter trap is a door whose far side is a thousand metres away.** The
chest in Limgrave at (11027, 9161) throws you into Sellia Crystal Tunnel,
whose own mouth is 1,780 m off in Caelid at (12563, 10101) -- so the tunnel,
and the whole path walked inside it, was drawn on top of the chest. The
sampler already withholds the anchor when a load screen precedes the map
change, but `through_the_door()` reconstructed the same position from the
samples afterwards, which defeated it.

The half that needs care is the imported half: the old tool sampled every five
seconds and never recorded a `0xFFFFFFFF`, so a trap taken before this tracker
existed looks exactly like walking through a door and its anchor is kept.
`interior_visits()` collects the departure point of every load-screen arrival
into a dungeon and drops any *other* visit's entrance anchor within 15 m of
one -- the two readings of that chest are 0.8 m apart, so the tolerance is for
sampling noise, not geography. Matching on the position rather than on the map
you came from matters: a grace warp lands you in the same overworld tile you
would have walked in from often enough that the coarser test threw away
Stormveil's front gate and moved the castle 540 m. Distance alone cannot do
this job either -- entrance-to-exit disagreement across `routes.db` runs
smoothly from 0.2 m to 1,911 m with no gap to put a threshold in, because a
dungeon with two mouths looks the same as a trap.

Measured after the change: five visits corrected, everything else within a
metre, and today's trapped visit moved from the chest in Limgrave to the
tunnel's real mouth.

**A teleporter is indistinguishable from a door, so the doors have to vote.**
Working the tower's position out from where you left the last dungeon is right
until the route in goes through a teleporter: same map change, same load
screen, but the two places are nowhere near each other. On `routes.db` five
ways into the tower agree to within 2 m and the sixth is 83 m off, and since
each visit was anchored at its own door, that one visit drew 83 m from the
rest of the same tower. `interior_visits()` takes the medoid -- the door
closest to all the others -- and draws every visit to that map from it.

**A position set by hand outranks a frame, not just an anchor.** Dragging a
dungeon set its marker but the drawing still hung off the frame the inference
had learned, so the path stayed where it was and only the pin moved.
`interiorTransform()` checks `by hand` before consulting any frame, and
`learnDungeonFrame()` skips those maps entirely.

**Warping out is not walking out, and the exit tier assumed it was.**
`exit_anchor()` takes the first surface reading within five minutes of
leaving, on the reasoning that coming out puts you at the door. Walking out
does. Warping out puts you at whichever grace you picked, and the Roundtable
Hold is the case that makes it obvious: there is no way in on foot at all, so
both of its anchors were places it had nothing to do with, and it was drawn
as a path in the middle of nowhere that moved depending on where you warped
next. The tell is the one used everywhere else here -- the first reading
afterwards carries break code 3 -- and on `routes.db` it separates cleanly:
45 exits were walked, 7 warped, every warp with a zero-second gap.

With that in, the Hold is unplaceable from the route, which is the truth: it
goes to the unplaced list and is drawn in the inset. Three other visits
improved on the way past, each falling back from a warp-out point to a real
entrance borrowed from another visit -- Stormveil by 114 m, a cave by 218 m.
Note that the legacy import fixture had a load screen between its cave and the
surface while claiming to model walking out; it now puts that load screen at
the front, where it still exercises dropping the sentinel without also
claiming the exit was a door.

**The one mechanism that could fix an unplaceable dungeon was unreachable
for exactly those dungeons.** A position set by hand outranks every inferred
tier, but the only way to set one was to drag a marker -- and a dungeon with
no position has no marker. So the Roundtable Hold, the case the whole tier
system cannot solve, was also the case with no way to solve it manually. The
unplaced list now carries a **Put on map** button that arms a click: the next
click on the map is the position, Escape cancels, and it goes through the same
`POST /api/place` a drag does. Placed that way it becomes an ordinary marker
and can be dragged afterwards like any other.

The Hold sits in the bottom-left corner of the map image, which is where the
game's own map screen puts it -- off the terrain entirely, because it is not
anywhere in the world. The Chapel of Anticipation (m10_01) is still unplaced,
and left that way: it is not the tracker's guess to make.

**Saying a place is nowhere is a different answer from not knowing where it
is, and clearing cannot stand in for it.** The Roundtable Hold had been put at
the bottom-left corner of the *map image* by hand, which is where the game's
own map screen draws it -- but that is a place on the terrain, and the Hold is
not anywhere. Taking the hand placement off does not help: clearing only hands
the question back to the tiers, and they always have an answer. Measured on
`routes.db`, cleared, the Hold takes an `exit` anchor at (6010, 6180) from the
one time leaving it did not look like a warp -- a confident position for
somewhere that has none.

So `map_places` grew a `nowhere` column (with the usual ALTER TABLE for
databases that predate it), `POST /api/place` takes `nowhere: true`, and
`interior_visits()` clears the position for those maps. That last part has to
run **after every tier**, not next to the hand placement where it belongs by
meaning: put there, four later tiers filled the position straight back in and
the selftest caught it. Undo is `Put on map`, which is the tier it was
overriding.

The viewer then draws those in the corner of the *screen* rather than on the
map: `#offmap`, bottom-left of the map area, one button per map rather than
per visit -- four trips to the Hold are four rows in the panel and one door in
the corner -- opening the inset and moving nothing, during playback as well.
Measured: the map's centre and zoom are identical before and after the click.
A placed dungeon gets there through **Take it off the map** in its popup.

**Following the route between maps is now a choice.** The map switching under
you is right by default -- being shown the plane you are not on is the same as
not being tracked -- but not always wanted, so `auto-plane` in the Map section
gates it, live and in playback. With it off the map stays where it was put,
`playLine()` draws none of the other plane's path on it, and the position mark
comes off while the route is over there: a dot wandering across Limgrave while
you are in Siofra would be saying something untrue. Measured with it off, at a
moment the route is underground: plane surface, surface tiles, 205 surface
stretches drawn and 0 underground, mark off the map.

**Some dungeons cannot be located from a route at all, so the last word is
the user's.** A Divine Tower entered from inside Stormveil and left the same
way never produces a single surface position tied to it: no entrance, no exit,
and every visit inherits the same guess. That is not a bug to fix with a
cleverer inference, it is missing information -- so `map_places` stores a
position per map ID, `POST /api/place` writes it from a marker drag, and it
outranks every inferred tier. `interior_visits()` applies it to every visit to
that map. Undo is the same endpoint with `clear: true`.

**An approximate position is centred, not pinned.** Every other interior is
drawn by pinning its first recorded step to its entrance, which is right
because that step is the doorway. A dungeon placed at the neighbour it opens
off has no doorway of its own on the map, and pinning its first step to the
neighbour's door drew the tower setting off out of Stormveil's entrance --
claiming a continuity that is not there. Those are centred on the anchor
instead, and dashed (casing included, or a dashed line over a solid casing
just looks smudged) so the drawing says what it knows.

**Placement iterates until it stops changing.** The tiers feed each other: a
tower placed by the way in becomes a position that the other four visits to
that tower can borrow. Running the borrowing once left two of five stranded.
Borrowing from a guess stays a guess -- the `placed` value carries through as
`the way in` rather than being upgraded to `another visit`.

**Placement has four tiers, and the loose ones say so.** In order:
the entrance recorded on the way in; the first surface position after coming
out, within five minutes; another visit to the same map; and finally the place
you came in from, for somewhere like a Divine Tower reached through Stormveil
that never touches the surface. Only the last is a guess, it is labelled `the
way in` and the popup says it is the right part of the map rather than the
exact spot, and `came_from` is reset per session -- the last place you were in
yesterday is not the way into the first place you go today.

**Where a dungeon is belongs to the dungeon, not to one visit.** Warp into
Stormveil and that visit has no entrance to anchor to, so it went to the
unplaced list -- even though you had walked in an hour earlier and the place
was perfectly well known. `interior_visits()` now borrows a position from
another visit to the same map ID, preferring one whose entrance was actually
recorded, and marks it `placed: "another visit"`. Only somewhere never entered
on foot stays unplaced. The live overlay depended on this too: it looks for an
open visit among the *placed* ones, so a warped-into dungeon drew nothing at
all while you were in it, which reads as the tracker having stopped. It now
falls back to the unplaced list and opens the inset for the genuinely unknown
case.

**The version handshake cannot catch a field the server never sends.** It
compares two numbers, and both halves can agree on the number while the
payload between them is missing something: `store.warps()` selected
`a.layer`, `app.js` filtered on `w.layer`, and `server.warps()` -- which
builds the item dict by hand, field by field -- was never given the line. So
`onThisPlane(undefined)` filed every waygate in Nokron onto the Lands
Between, where they were drawn in the middle of Limgrave, and none appeared
on the map you were standing on. `/api/interiors` was missing `plane` the
same way. There are three layers here and the middle one is hand-written, so
`selftest.py` now asserts that every payload the viewer files by plane
carries the field, and that the viewer reads it off the name the server
sends. Removing one line from `server.warps()` fails it.

**index.html and app.js are one thing in two files, and the browser will
cache half of it.** Neither was served with any `Cache-Control` at all, so a
browser applied heuristic caching and could hold yesterday's `index.html`
against today's `app.js`. Every `getElementById` for a newly added control
then returns null, `wireControls()` throws on the first one -- adding the
respawn checkbox put it at line 1872, before the plane buttons at 1898 and
before `boot()` reaches `reload()` at 347 -- and the result is a blank map
whose buttons do nothing, from a change that works every time it is tested,
because testing it always used a cache-buster.

Three guards, because each covers a different hole. The server sends
`no-store` on `/`, `/app.js` and `/style.css` (not the tiles: large and
immutable). `index.html` carries `<meta name="page-build">` and `app.js` a
matching `PAGE_BUILD`, so a mismatched pair says so in a banner rather than
being inferred from the wreckage -- `selftest.py` asserts they agree, the
same way it does for `API_VERSION`. And every lookup inside `wireControls()`
goes through `control()`, which warns and returns a stub, so one missing
checkbox can no longer take the route with it. Verified by rolling
`index.html` back to a version without the control: 424 route layers still
drawn, plane buttons still working, banner up.

**A missing field is how a stale recorder shows up.** The version check only
caught a missing endpoint (404). Adding a field to a payload is the quiet
version: an old recorder simply does not send it, the viewer filters on it,
and the feature never appears -- the symptom being "the thing you just built
does not work", twice in a row. `server.API_VERSION` and `NEEDS_API` in
`app.js` are compared at boot and the page says which half is behind. Bump
both when the viewer starts needing something new; `selftest.py` asserts they
match, so they cannot drift apart in the repo.

**Do not take the permanent drawing off the map to draw the same place.** The
live overlay used to remove a dungeon's always-drawn path while it drew that
dungeon itself, to avoid stacking two copies. The effect was that walking back
into a castle wiped every previous run through it, which came back only when
you hovered the entrance. They coexist now: the overlay is heavier and sits in
its own pane, and while you are inside a world-visible dungeon there is no
overlay at all -- `refreshLiveInside()` redraws the permanent paths instead,
since the open visit is one of them and grows as you walk.

**A legacy dungeon is an interior you can see from outside.** Caves are hidden
under the terrain, so drawing one permanently would be a shape floating over
grass; Stormveil is on the map, so its path belongs there next to the route.
`[maps] world_visible_areas` decides which, `is_world_visible()` reads it, and
`/api/interiors` passes it to the viewer as `world_visible`. Those paths are
drawn into `placedGroup` at load, their deaths with them -- and those deaths
are then skipped by the entrance clustering, or the same death would appear
twice. Tint them in banded runs like the world route, not one polyline per
pair of points: per-pair put 4,232 layers on the map for four castles, banding
puts 146.

**So can a refresh and walking out of the dungeon it is about.**
`refreshLiveInside()` awaits `/api/interiors` and then calls
`showInside(..., true)` unconditionally. Leave during that fetch and
`stopLiveInside()` runs first, the answer lands second, and the overlay goes
back up -- *pinned*, because that is how the live overlay draws, so
`hideInside(false)` on a mouseout cannot clear it either. The world stays dim
with the cave drawn on it until you happen to click the map, which is exactly
what "it looked like I was still in the cave, then it worked fine after some
time" is.

It takes a token like the other loaders, and `stopLiveInside()` bumps it, with
a check after each await -- including one after `showInside`, which fetches
again a level down. Reproduced deliberately by slowing `/api/interiors` to
1.5 s and leaving 200 ms into it: the old build came back `dimmed: true,
pinned: true, 2 layers`, the new one `dimmed: false, pinned: false, 0 layers`.

**Two refreshes of the same marks can race.** A zoom, a death and a dungeon
being drawn all ask for the marks again, and the slower answer used to clear
the faster one's work before adding its own -- which showed as more death
marks than there are deaths. Each loader takes a token now and drops its
result if another call started meanwhile.

**Hovering a dungeon means "show me this place", and a place is all of it.**
`showInside()` drew `group[0]` and nothing else, so a cave visited five times
showed one run on hover and the other four only through the Show path button
in the popup -- which reads as the marker being broken, since the popup
plainly lists five. It draws every visit in the group now, in the dungeon's
own frame, the way a legacy dungeon's permanent drawings already overlay.
Show path stays the exception: it asks for one and gets one, and because that
is a deliberate "this run, not the others", it is also what dims the rest of
the map -- hovering a legacy dungeon still does not, since that would turn the
lights off for something already drawn in the open.

A visit can also be real and have no path at all: on `routes.db`, two of the
five visits to m31_15 are 21 and 35 seconds with no sample stored, because the
movement gate had nothing to record. The caption says `3 of 5 visits (2
recorded no movement)` rather than `all 3`, which would be a small lie told
right next to a popup listing five.

**A cave's death toll is a different fact from its visit count.** Both live
on the same pin, so they are two badges rather than one number: visits bottom
right in the marker's own amber, deaths top right in the red the death marks
use, at (24, -4) and (24, 22) on a 34 px pin so they cannot collide. Counted
through the same time window as everything else -- a badge that disagreed
with the marks beside it would be worse than no badge -- and rebuilt on the
death event itself, because `loadDeaths()` alone leaves the markers holding
yesterday's number until something else redraws them.

**One dungeon, one marker: group by map ID, not by position.** Grouping the
markers by rounded anchor pixels meant going back into the same cave from a
slightly different spot produced a second marker with its own path, for what is
one place. The map ID is the identity. Within a group the visit shown on hover
is the one whose entrance was actually recorded, then the longest, because the
whole group is drawn at the first one's position.

**Marks are refetched on live events, not built from them.** The websocket
message for a death carries one death; the map draws deaths clustered by
proximity with a count, which is a property of the whole list. Building a mark
from the message produced a mark of the wrong shape and left the clustering
stale, so the handlers call `loadDeaths()` / `loadWarps()` / `loadInteriors()`
instead -- small JSON, and the grouping stays the server's list to decide.
Before that, nothing new appeared until a zoom happened to trigger a reload.

**The live dungeon overlay finds its visit by "no leave event yet".** While you
are inside, `interior_visits()` returns that visit with `duration_ms` null, so
`refreshLiveInside()` picks the newest open visit for the map the recorder
says you are in, and redraws every 5 s because the path is still growing. The
enter event may not be committed when the first sample inside arrives, hence
the retry rather than a one-shot.

**The game closing is not the same as the game not answering.** `read()`
returns None for a load screen, the main menu, a character not yet spawned,
an unresolvable chain, and a process that has exited -- five things, one
answer, which is right for sampling and useless for deciding whether to stop.
`GameSource.gone()` asks the one question that separates the last from the
others: whether the module base is still readable. Two failures in a row
before it says yes, because the answer during a tear-down can be no once and
yes again, and stopping the recorder is not a thing to do on a single flake.

`SimSource` and the importer's `ReplaySource` answer no for ever, so the
worker does not have to know which kind of source it is holding.

The serve loop ticks in tenths and prints its counts every fiftieth tick,
rather than sleeping five seconds at a time: quitting the game should close
the window now, not up to five seconds later. And `stop_with_game` is in
config because the recorder is also the viewer -- stopping it takes the map
down, which is what was asked for and not what everyone would want.

**A failed AOB scan must drop its cached image.** `GameMemory._image()` reads
the whole module once and keeps it. Started by hand that is fine, because the
game has been up for a while; started by `record --launch` the process exists
seconds after the exe does, the image read then is a partial module, the
pattern is not in it -- and every later attempt searched the same stale bytes,
so the scan could never recover for the rest of the session. `scan()` now
clears `_text` on a miss. The recorder's worker also prints a repeated read
error once rather than four times a second, since that error is what you get
for the whole time the game sits on its splash screen.

**Playback has the same problem the age ramp had, and the same shape of
answer.** `routes.db` spans 34 days of calendar and about 13 hours of
walking, so a playback paced by the clock spends almost all of itself on a
stationary dot -- the first test of it jumped from 3 August to 5 August in one
step while the progress read 0%. Each point carries a third axis, elapsed
*played* time, built by capping every gap at ten seconds (the same cap the
statistics use), and playback walks that. Pacing inside a session stays real;
the nights off pass in a moment.

**A sending gate crosses between coordinate spaces, and `warps()` required
one.** The query took a pair of samples only if both had a world position, or
both were inside the same map -- so every jump between the surface and the
inside of a dungeon fell through the hole between those two clauses. That is
exactly what a gate is: standing in Limgrave, a load screen, and you are
inside a dungeon kilometres away.

Measured over `routes.db` against every break in the samples: 35 jumps with a
dungeon at an end went unreported. Eight were within thirty seconds of a death
and dropped on purpose, three were covered by the transit across their stay,
and **24 were drawn as nothing at all** -- the longest 4,735 m, then 4,603,
3,241 and two of 1,801. (The other 51 unreported breaks are all world-to-world
and all within thirty seconds of a death, which is the respawn rule doing its
job.)

There is no measuring a surface position against a dungeon's own metres, so
the dungeon's place on the map -- the position its marker is drawn at --
stands in for the end that is inside one, and the jump draws as an ordinary
arc between two places. Where a dungeon has no place on the map there is
nothing to draw an arc to and it is left alone. The plane comes from the
dungeon when that is the end you arrived at, or the arrival's own layer
otherwise: taking it from the resolved end unconditionally left `layer` null
on every jump that came *out* of a dungeon.

One thing had to give way. `_transits()` reports a whole stay as one jump for
the case where no single pair of samples shows one -- and a gated entry now
shows one, so the same journey was marked twice, the second time as a single
arc from where you were to where you came out, which is a teleport and a walk
drawn as a teleport. It skips a stay that already reported a jump of its own.
On `routes.db` the count went 69 to 95 and the transits 5 to 1.

**A jump can happen across a dungeon rather than in one step.** `warps()`
pairs each sample with the one before it and needs both ends in the same
coordinate space -- so a route that goes into a tunnel and out of its far
mouth is invisible to it: the row before the hole is world metres and the row
after is too, but the rows between are the tunnel's own, and no adjacent pair
spans the gap. `_transits()` walks the interior stays instead and pairs the
surface sample before with the surface sample after. Fifteen such jumps on
`routes.db`, from 77 m to 1,911 m, none of them drawn anywhere.

The same 50 m minimum works without being re-derived, because the data leaves
room for it: of the forty dungeon stays with a surface position at both ends,
twenty-five come out within 25.6 m of the way in and fifteen come out 77 m or
more, with nothing between. Break code 4 marks them so the reason can say what
happened, and the popup offers no "This was a death" -- walking through a
tunnel is the one kind of jump you can be sure about.

**Bumping `API_VERSION` is not optional, and I skipped it.** Marking a death
started writing the grace as well, and all of that lives in `store.py` --
which Python loads once at startup while serving `viewer/` fresh from disk. So
a recorder left running paired a new page with an old store, marked the death,
wrote no respawn, left the line drawn from the body to the grace, and said
nothing. That is the exact failure the handshake exists to catch; it caught
nothing because both halves still read 13. Anything that changes what an
endpoint writes or returns bumps the number, not just a new endpoint or a new
field in a payload the viewer filters on.

The banner tells you the halves disagree, which is one step removed from the
thing you just pressed, so `playDiedHere()` also checks its own reply: no
`respawn_ts` key means an older recorder and says so; a null one means nothing
was recorded after that point in the session, which is a real answer rather
than a fault.

**A seek must not be steered by the events it replays.** `playSeek()` clears
everything and replays from zero, so the events loop runs through every death
and teleport up to the target -- and once an event was allowed to take the
place it happened in, a seek ended inside whichever cave had the last interior
event before the target, with the whole map dimmed behind it. That is what
"scrubbing randomly darkens everything and selects a random cave" was: not
random, just the last one. Taking the place is gated on `animate`, which is
false for a seek and true only while playing, so a seek ends where the clock
says and the place check above it is the last word. Measured after: 99 scrubs
across the timeline, none in the wrong place, none wrongly dimmed.

The hold is set on *any* interior event while playing, not only when the event
had to move the drawing to get there. Setting it only on a move missed the
common case -- the step that reaches a dungeon usually reaches it through the
clock, so the event found itself already in the right place, set no hold, and
the next step left again after eighty milliseconds. Measured after: the tunnel
stays up for 1.56 s, which is the flash.

**Half a plane fix is worse than none.** Tagging the *world* marks with the
plane they were made on left the interior ones untagged -- and a legacy
dungeon is `open`, so its marks are drawn on the map like the world's. 45
events in `routes.db` carried no plane at all, which is Stormveil's deaths
standing over Siofra. Every mark carries one now, from its visit's frame, and
the guard in `playFlash()` asks about all of them rather than only the ones on
the world plane. Measured while underground: 30 marks on screen before, 3
after, and those three are the underground's own.

**The line a jump draws belongs to the moment, not to the picture.** It was
created outside the `if (animate)` and only removed inside it, so a seek left
one lying across the map -- and scrubbing replays every event up to where you
land, which strung one across for every teleport in the route. Making the arc
itself animation-only fixes both: measured, 15 scrubs across the timeline now
leave 0 arcs behind, against one per teleport before.

**A click on the map put the lights back on in a cave.** `map.on('click')`
calls `hideInside(true)`, which is right for a pinned overlay you want to put
away and wrong for the playback, where the overlay *is* what is being drawn:
the world came back to full brightness with the cave still on it, and nothing
put the dimming back until the playback walked out and in again. Both that and
the Escape handler return early while `play.on` -- Escape has its own meaning
during playback, which is to leave it.

**Deaths are drawn on the timeline.** One 1px line each, inset by half the
thumb's width so they line up with where the thumb will be. No glow: a 1px
line with a 3px shadow is a 7px line, and 130 deaths bunched into the recent
sessions read as one solid red band rather than as the deaths they are.

Worth noting about `selftest.py` while this went in: an ordering check written
as `a.index(x) < a.index(y)` raises rather than failing when the string is
gone, so removing the plane guard aborted the whole run instead of reporting
one red line. The ordering checks now ask `in` first.

**The playback was built out of what was on screen, and the map shows one
plane at a time.** `buildPlayback()` took `state.drawn`, which `reload()`
fills by asking for `layers=[state.plane]` -- so the underground was simply
not in it. On `routes.db` that is 4,506 samples, sessions 49 to 55, two hours
of Siofra and Nokron on 6 September, played back as a hole in the route.

It asks for both planes now and switches the map as it goes, which is what the
live feed already does for a sample on the other plane. Every stretch and
every mark carries the plane it belongs to -- `seg.layer` for the world, the
visit's own plane for a dungeon -- and `playPlane()` takes all three things
with it: the tiles, the path (`play.group` is cleared and the runs walked so
far on the new plane redrawn) and the marks (cleared, and the ones for the new
plane replayed, `play.pinned` with them). Leaving the path behind put the whole
of Limgrave on top of Siofra with the river's own twenty-one stretches lost in
it, which is the mistake `state.live` made before it.

The one place that needed care is the pin. `playFlash()` returns early for a
`found`, so a plane guard added below that line let every cave on the surface
keep its pin standing over the black of the underground map. The guard goes
above it.

Measured on `routes.db`: 21 underground stretches and 8 underground marks now
in the playback where there were none; four plane switches at 12:52:24,
13:25:07, 13:27:37 and 14:50:18, which match the recording; the tiles served
from `tiles-underground` while down there and `tiles` after; and 21 of 21
drawn stretches belonging to the underground while the map is on it, with no
surface pins left standing. The plane you were looking at is given back on the
way out.

**A death has to register at a speed where it is on screen for a fiftieth of a
second.** The toll scaled its number to 1.5x and changed its colour, which is
not enough to catch at an hour a second. The whole badge answers now: the pill
flares red and throws a glow, the count punches to 1.84x in near-white and
settles back through the death red, and the skull kicks 1.4x and rotates.
Driven through the Web Animations API rather than sampled, because a CSS
animation does not advance while the window is not compositing -- the same
thing that rules out `requestAnimationFrame` for the scrub drag.

Six hundred milliseconds, which is under the gap between two deaths in the
worst stretch of `routes.db` played at an hour a second. And it fires only on
a rise: `playReset()` calls `playToll(0)`, a seek is a reset, and live
scrubbing made a seek per frame -- so dragging the scrubber had the whole
badge flashing continuously.

**Going back into a cave brings its tally with it.** `playEnter()` has
redrawn the earlier visits' *paths* on the way in since "going back into a
cave should show the cave, not just this run" -- but the marks were still
being cleared and their spots dropped, on the reasoning that leaving them
would have the next visit counting up a tally whose marker was no longer on
the map. True, and the answer was to put the markers back rather than to drop
the count: a cave you had died in twelve times started again at one every time
you walked through the door.

`playSamePlace()` is the idea the rest of the file already had for paths --
two visits to one map are one place for anything drawn in it -- applied to
`playFlash()`'s guard and to a replay of the earlier events at the top of
`playEnter()`. The one trap is that the event loop steps its cursor *before*
flashing, so the mark about to be made is already inside `0..play.event` and
would be drawn twice; the loop passes its index as `skip`.

Measured on `routes.db`, map 520749056 -- which is the one that was reported:
12 deaths showing by the end of the 20:58 visit (3 from earlier visits plus
9), 12 still showing on re-entering at 21:27 where it used to fall to 0, and
21 by the end of that visit, which is exactly what the database holds. Across
a whole playback, 25 dungeons, none of them ever shows more deaths than it
has.

**Hovering the path asks a question the timeline can answer.** The route on
screen and the bar along the bottom are the same journey drawn two ways, and
until now nothing connected them: you could see a stretch of path and have no
way to find the moment it belonged to. Hovering now puts a mark on the
timeline where that bit was walked, with the moment above it, and clicking
goes there.

Against the *segments*, not the points. Testing the distance to each recorded
point was the first try, and it meant that anywhere between two of them the
hover found nothing -- which at a coarse zoom is most of the path: the longest
gap between two drawn points on `routes.db` at zoom 4 is 438 px. The clamp at
either end of a segment covers its corners too, so the points need no pass of
their own, and the moment is read off how far along the segment the cursor
falls rather than snapping to one end. It costs 0.082 ms a mouse move against
0.006 for the point test -- both nothing against a 16 ms frame.

The cursor says so as well: `#map.on-path` turns the grab hand into a pointer
while the cursor is over the path, which outranks Leaflet's own
`.leaflet-container` and `.leaflet-grab` rules without needing to shout,
because `#map` *is* the Leaflet container.

Hit-tested here rather than through Leaflet's layer events, for two reasons
that are both load-bearing. The interior overlay's pane is
`pointer-events: none` -- it has to be, or a full-map canvas swallows every
click on the map -- so a run drawn inside a dungeon would never receive one.
And what is wanted is the nearest recorded *point*, because that is what
carries a timestamp; the nearest line does not.

The cost is kept down by a box per run, computed once when the runs are built,
so a cursor is ruled out of a stretch without looking at any of its points.
Measured with everything drawn -- 312 runs, 9,730 points -- a mouse move costs
0.006 ms on a hit and 0.003 ms on a miss, against a 16 ms frame. Only what is
actually on the map is searched, which is also the only thing you can see.

A click on the map during playback had been doing nothing since it was stopped
from undimming a cave, so it is free to mean this.

**Following the mark was animated, and that is the whole of why it felt
choppy.** `map.panInside()` with the default options eases the pan over
250 ms, and Leaflet's `PosAnimation.run()` stops whatever is running before
it starts one. The playback ticks every 16 ms, so each pan was cancelled and
restarted fifteen times before it could ever finish: the map was permanently
part-way through an ease that never completed, which lags the mark and
judders while it does.

Measured on a 1052x900 map, with the mark 12 px outside the box `Keep me in
view` draws:

- animated, the call moves the map **0 px** in the frame it is made, leaves
  an animation running, and the next tick's call restarts it;
- unanimated, it moves exactly the **12 px** needed, in that frame, with
  nothing left running.

So the follow passes `animate: false`. A few pixels sixty times a second, in
the same frame as the mark it is following, is what a smooth pan is -- and it
cannot lag, because there is nothing left over to interrupt.

That deleted the leap machinery with it. `PLAY_LEAP_SHARE`, `PLAY_LEAP_MIN_PX`
and `play.lastAt` existed to notice a teleport by its width in pixels and turn
the animation off for it -- but `panBy` with `animate` not true already sends
the map straight there when the offset is wider than the viewport. Measured:
a target three screens away arrives in one call, 3,027 px in 41 ms, on screen
at the end of it, no animation left. Leaflet was always going to do that for
us; the rule was written around an option that should never have been on.

The live feed's follow still animates and is left alone: samples arrive every
half second or so, which is longer than the ease, so nothing interrupts it
there and a glide reads better at that rate.

**Following is the right shape for walking and the wrong one for a jump.**
Asked for: "during playback, when a teleport happens, make it so that the
place you teleport to is in the center of the screen."

`panInside` moves the map the smallest distance that puts the mark back inside
the follow box, which is exactly right while you are walking -- a few pixels a
frame, and the ground you came from stays on screen. A teleport is not
walking. The clock reaches the far end in a single step, so the minimum pan
leaves the place you have just arrived at pressed against whichever edge you
came in by: measured over eight jumps at zoom 5 on a 1052x900 map, the arrival
settled 173 to 499 px from the middle against a half-diagonal of 692, median
446. Centred, it is 0 px on all eight.

The part that took thought is *when*. Panning the moment the jump fires takes
the end you left from off the screen before it has finished landing, which is
the half of a teleport `playTravel()` exists to show; panning in between
creeps towards the arrival and then jumps again when the line lands, which is
two movements for one journey. So the follow is held from the jump firing
until the line gets there and the map arrives with it -- `play.centre` is
armed beside `warmAhead()`, which is already fetching the tiles for exactly
that destination, and applied off the same timer that lands the arrival mark,
so a pause mid-jump still ends up looking at the place the jump went to.
Measured across the same eight: the map's centre does not change once in the
380 ms in between.

Two things it deliberately does not do. It is gated on `Keep me in view`, like
every other pan the playback makes -- with that off the map stays where it was
put, which is a promise made elsewhere in this file, and a map that moved only
for teleports would be a strange way to keep it. And it is `map.setView(...,
{ animate: false })` rather than `panTo`: `panTo` puts its options under `pan`
and leaves `animate` undefined at the top level, so `panBy` animates after all
-- the exact ease the follow was fixed to stop. `panInside` hands the same
options to `panBy` directly, which is why *it* has been unanimated all along
while the obvious call beside it would not have been.

A seek clears it. `playReset()` sets it to null and the land timer then finds
nothing to do, so scrubbing away mid-jump does not pull the map to where that
jump was going a moment later. Verified: centre unchanged, `play.centre` null.

And the heredoc trap took a fourth turn on the way past. A bash heredoc *with
a quoted delimiter* still ate a backslash here, so `"\\n}"` in the patch
script reached `selftest.py` as a real newline inside a string literal and
took the whole file out with a syntax error. Write the script with the editor,
or build the backslash with `chr(92)`.

**A jump at speed is a problem about pixels, not about metres.**
`map.panInside()` glides, and the playback calls it every 80 ms tick -- so a
teleport set the map gliding across the world and the next tick restarted the
glide from wherever it had got to. It never arrived, which is what "it cannot
keep up" was. Nothing about that depends on how far the jump was in metres: it
depends on how far it is on screen, which is why it was reported as happening
when zoomed in. Measured over the 51 surface jumps in `routes.db` on a
1,203 px diagonal, the median jump is 79 px at zoom 3 and 1,272 px at zoom 7.

Past 45% of the diagonal the map is sent straight there -- there is nothing to
watch in a glide that will be interrupted anyway.

**Easing off, though, belongs to the teleport and not to the pixels**, which
took a second go to get right. Braking on the on-screen distance of each step
fired on 246 steps of one playback of `routes.db` at zoom 8 against 64 actual
jumps -- because at a close zoom *every* 24-second step is most of a screen.
So the whole playback was uniformly slower and the teleport, the one thing the
brake exists to mark, did not stand out at all. Which is exactly how it came
back: "a teleport still doesn't really slow it down as much as it should."

It is on the event now. How long still comes from the distance on screen,
because that is what the eye has to cross -- at zoom 3 the median jump is
79 px of a 1,178 px diagonal, at zoom 8 it is 2,017 px -- but the floor is
`PLAY_TRAVEL_MS`, since easing for less than the line takes to draw itself
means the playback has moved on before the jump has finished being made. The
clock runs at 6% while it lasts: eased rather than stopped, because a playback
that halts reads as broken.

Measured at an hour a second, where the whole route is 15.7 s: the pauses add
28.4 s at zoom 3, 36.8 s at zoom 5, 43.3 s at zoom 6 and 52.5 s at zoom 8. At
five minutes a second, which is 156 s of route, the same 28.4 s is 18%. Live,
over fourteen seconds of real playback at that speed, five teleports went past
and the clock was eased for 16.2% of the time in three spells -- three rather
than five because `Math.max` merges the brakes of jumps that arrive together.

`PLAY_BRAKE_SPAN_MS` and `PLAY_BRAKE_MIN_MS` are the two numbers to turn if
that is too much or too little. And note that `PLAY_BRAKE_MIN_MS` is written
as 420 rather than as `PLAY_TRAVEL_MS`, which is what it means: that constant
is declared seven hundred lines further down, and reading it here would throw
before the page finished loading. `node --check` does not catch that.

**The line a jump draws now goes out rather than blinking off.** It arrives
over 420 ms and used to be removed by a `setTimeout`, so the end of it was one
frame, which reads as a glitch. It fades over 480 ms instead -- and moved to a
canvas of its own (`playarc`, z-index 685, `pointer-events: none` for the
reason the inside pane has it) because fading it on the route's own canvas
would repaint the whole route for every step of the fade. Measured on the live
viewer: 0.80, 0.59, 0.38, 0.17, 0.04, 0, then off the map and off the arc
canvas. It is taken out of its layer group as well, since `remove()` alone
leaves a dead layer in the group's bookkeeping.

Note that a *seek* still leaves its jump lines drawn: the arc is created
outside the `if (animate)`, so the picture at that moment shows the jumps that
have happened, and `playReset()` clears them on the next seek rather than
letting them pile up.

**The playback treated every interior as a cave.** `drawInside()` has known
since the beginning that dimming is for things that would otherwise be lost --
a cave is under the terrain, a legacy dungeon is drawn out in the open, and
turning the lights off to reveal something already on the map helps nobody.
`playEnter()` knew none of it: it dimmed the world for Stormveil, drew the
castle into `insideGroup`, and cleared that group on the way out, so the
twelfth run through it wiped the eleven before and the whole thing vanished
when you walked outside.

`playOpen()` is the split, and everything follows from it. An open place draws
into `play.group` with the world's own renderer and pane, so it accumulates
and is never taken away; it is never faint, because there is no overlay for it
to compete with; its marks are `home` in `playFlash()`, so they last like the
world's; and `playDropHead()` keeps a head drawn there, or clearing
`insideGroup` would leave that stretch drawn twice. Measured across
`routes.db`: never dimmed at any point in the playback, and the stretches of
one legacy dungeon on the map go 5, 7, 8, 21 as the runs pile up rather than
dropping back. A cave still dims, and still comes down when you leave.

**The age ramp was a fact about the finished route, shown from the first
frame.** `playRuns()` bands each stretch by `agePosition()`, which normalises
against the newest sample in the database -- so a playback ten minutes in was
already wearing the colours it would end with, and there was no way to see
which part had just been drawn. Each run now carries the rank of its own last
point, `play.rankNow` is the rank of the playhead, and `playBand()` divides
one by the other: whatever was drawn last is band 11 and everything behind it
slides down as the denominator grows.

The pass that brings the drawn stretches up to date measures 0.2 ms median and
1.2 ms worst over ~250 layers on `routes.db`, against an 80 ms tick, because
Leaflet's canvas renderer batches all of it into one redraw. It is throttled
to 250 ms anyway and forced on a seek, so scrubbing lands on the colours of
the moment rather than the ones from a quarter of a second ago. What it cannot
fix is the *banding*, which is decided once at build time against the final
ramp: ten per cent in, the drawn path spans three bands rather than twelve.
The head is the newest colour throughout, which is the thing being asked for;
re-segmenting every run as the playhead moves would buy a smoother ramp early
on for a great deal more work.

**Dragging the scrubber redraws as it goes, coalesced by a timer and not by
`requestAnimationFrame`.** One rebuild in flight at a time, and the one that
runs reads the thumb wherever it has got to. A full seek measures 42 ms median
and 63 ms worst on `routes.db`, so it paces itself: aligning that to a frame
buys nothing, and rAF does not run at all when the window is not compositing,
which turns "the path follows the thumb" back into "nothing happens until you
let go" for reasons the user would have no way to see. Verified with 25 moves
of the thumb and no release: the drawing followed every one.

**Where the mark stands is a fact about the clock, not about the drawing.**
`playHeadPoint()` read the position back off the head line -- the last point
of the run currently half drawn -- and fell back to the end of the previous
run when there was no head. Both halves of that fail, and they fail together:
a run reached at its first point draws no head (`if (n > 1)`), and
`playEnter()` calls `playDropHead()`, which on *every seek* takes away the
head the run loop had just built one block earlier in the same call.
`playReset()` sets `play.where = null`, so the place check below always
differs and always fires.

So on any seek that did not land on a run's final point, the mark showed the
end of the run before. Measured over `routes.db`: wrong on 195 of 397 steps,
and inside m18_00 it sat still through a whole stretch while the clock walked
on, 106 m adrift and growing. The half that looked right was an accident --
landing on a run's last point completes it, and the fallback then happens to
name the correct point.

It surfaced as a death marked by hand landing a point away from the mark,
which is the honest symptom: `playDiedHere()` sends `playClock(play.at)` and
the sample nearest that moment is the right one. The clock was never wrong;
the mark was.

`play.here` is written as the runs are walked, in both branches of the loop --
the last point of a completed run, or `run.xy[n - 1]` of the one being walked
-- and cleared at the top, so a gap between runs still leaves the last point
walked. Measured after: 397 forward steps, 249 back, 193 random scrubs and
1,858 playing ticks across the whole route, none of them off. And the death
marked at the mark now lands 0.004 m from it, which is the float noise.

**A place is held on screen for two different reasons, and they want two
different lengths.** `play.hold` kept the drawing where an event happened for
a flash's length, and skipped the place check entirely while it did -- so a
death near the end of a cave the playback had been walking through for twenty
seconds kept the world dim for a second and a half after the clock had left
it. Measured on `routes.db` at five minutes a second: tails of 416, 761, 1502
and 1522 ms after four of ten cave visits, with the mark long since landed.

Dropping the hold is not available. A step covers 24 s of route time at that
speed, and 25 of the 80 marks made inside a dungeon fall in the last step of
the visit they belong to -- they fire after the place check has already left,
so without the hold `playFlash()` refuses to draw them and a third of the
deaths and teleports inside dungeons stop being announced at all.

So `play.shown` records the places the *clock* put on screen, and the hold
reads it. A place the clock stepped over is on screen for this hold and never
again, so it gets the whole `PLAY_FLASH_MS` to be read in; a place the clock
played through has been up all along and needs only `PLAY_POP_MS`, the mark's
own landing. Measured after, across 34 hours of route time at three speeds:
every tail 0 ms except one of 998, which is a run of marks landing one after
another and has something to show throughout.

A `found` event holds nothing now. `playFlash()` returns before the interior
branch for one -- the pin goes on the world map -- so holding a dungeon on
screen for it showed nothing and only kept the world dark.

**A twenty-second visit is crossed between two frames.** Fixing the place to
come from the clock rather than from the runs was necessary and not
sufficient: at five minutes a second one step of the playback covers
twenty-four seconds, so a death marked inside a tunnel you were in for twenty
was entered, drawn and cleared inside a single call, and never appeared. An
event now takes the place it happened in -- `playEnter(e.where)` before its
flash -- and sets `play.hold`, which the place check honours for as long as
the flash lasts. So the tunnel comes up, the death announces itself, and the
world returns a second and a half later.

Two things fell out of that. The run loop no longer announces dungeons at
all, which means `playEnter()` has to draw everything known about a place
rather than only the earlier visits' runs -- it is idempotent now, and a
replay can pile every visit's runs into one group because entering at the end
puts back exactly the right set. And leaving to the world clears
unconditionally rather than only when we knew we were inside, since after a
reset nobody else would.

Worth knowing when testing this: a loop of ticks run synchronously never lets
`Date.now()` advance, so the hold never expires and the playback looks stuck
inside a dungeon for ever. It is the harness, not the code -- put real wall
clock between the ticks and the pattern comes back as `wiwiwiw`.

**A dungeon with two mouths should be drawn from the one you actually use.**
`learnDungeonFrame()` took the first visit of the best tier -- the first one
that walked in -- and that is a coin toss when a dungeon has two doors.
Sellia Crystal Tunnel has five visits: one entered at (12604, 10026) and four
at (12563, 10102), and the whole tunnel was drawn from the first, which is the
one used once. `agreedDoor()` counts the anchors instead and the frame comes
from the door most of them share; the marker is sorted to it too, since the
whole group is drawn at the first visit's position.

A count and not a medoid, deliberately. The medoid answers "where is the
middle of these", which is the right question for several readings of one
doorway and the wrong one for two real mouths -- the middle of those is a
place with no door in it. Measured on that tunnel: the door the player had
just walked in through was 110 m out before and 0 m out after, and the mean
across all five anchors went from 77 m to 41 m. What is left is the other
mouth, 110 m away, which no single frame can reach for the reason below.

**Two doorways give a dungeon its orientation; two doorways that disagree
say the question was wrong.** A frame pins one point inside a dungeon to one
point on the map, which lines the place up at that door and nowhere else,
because a dungeon's own axes have no fixed relation to the world's.
`frameTurn()` takes the angle between the line joining two doors inside and
the line joining them out on the map -- and refuses unless the two lines are
the same length, which is where the value is.

Measured over `routes.db`, of the four dungeons with two known doors:
Stormveil's are 543.4 m apart on the map and 542.8 m apart inside, and
m18_00's are 147.2 and 140.8. Both are a rigid turn away from each other.
m30_11's are 178.3 against 15.2, and m32_08's -- Sellia Crystal Tunnel -- are
86.1 against 13.5. Those are not orientation problems: the interior is simply
not laid out to match the ground above it, and no turn will ever put both
mouths right. They keep the frame they had and stay correct at one door.

What it actually buys is small and it is measured rather than hoped for: the
turn fires on two dungeons, m18_00 by -3.2 degrees, which takes its mean
doorway error from 5 m to 3 m and its worst from 10 m to 6 m; Stormveil's
comes out at 0.2 degrees and changes nothing, because its remaining 127 m is
not orientation either. Every other dungeon is untouched. A rule that can only
help is worth having even when it helps a little.

**The playback ran at twelve frames a second, and the head only ever sat on a
recorded point.** Both halves of "it snaps from point to point": the tick was
80 ms, and `play.here` was `run.xy[n - 1]`, the last point the clock had gone
past. The drawn route is simplified server-side, so those points can be many
seconds apart -- median 21.8 map units between neighbours on `routes.db` --
and the head jumped a whole simplified segment at a time.

Both are fixed and neither was expensive, because the assumption that made the
tick slow turned out to be wrong. A full repaint of the route canvas with the
whole of `routes.db` drawn measures **0.5 ms median and 1.7 ms worst**, not the
19 ms that the zoom-animation note reports for reprojecting 39k points at
zoom 3 -- so sixty ticks a second costs 3% of a second, and no separate
renderer for the head was needed. `PLAY_TICK_MS` is 16, and the head is
interpolated between the two points either side of the clock. Measured at 30
seconds a second: 199 of 199 ticks moved the mark, median step 0.69 units
against the 21.8 it used to jump, longest stall zero.

**And the loop steps by the time that really passed.** `play.at += speed *
PLAY_TICK_MS` assumes a timer asked for every 16 ms delivers every 16 ms,
which no timer does; the playback was quietly slower than the speed on the
label. It takes the wall-clock delta now.

The guard on that delta wants care and I got it wrong once. Falling back to
the nominal tick when the gap was long -- `if (dt > 250) dt = PLAY_TICK_MS` --
means any late tick loses almost all of its elapsed time, and the playback
measured **86% of the speed it claimed**. Capping instead (`dt = 250`) keeps a
late tick worth the time it took while still stopping a tab that was in the
background for a minute from skipping a minute of route in one step.

Worth knowing when measuring this: the ratio does not come out at 1.0 even
when it is right, because the teleport easing is deliberately slowing the
clock. Measured over 2.5 s at five minutes a second: 17.5% of the window
eased, ratio predicted from that alone 0.835, ratio observed 0.836. Check
against the prediction rather than against 1.

The clock line is throttled to ten updates a second. It is read, not watched,
and rewriting it and the scrub thumb sixty times a second is DOM work for
something the eye cannot follow -- but a seek is never throttled, because it
has to land where it landed.

**A jump plays as a journey, not as two related points.** Both ends of a
teleport used to land at the same instant with the line already drawn between
them, which says "these two places have something to do with each other" and
not "you went from this one to that one". It happens in order now: the end you
left from lands, `playTravel()` runs the line across to the arrival over
420 ms, and the arrival lands when the line gets there.

Stepped on a timer rather than a frame callback. The canvas is redrawn per
change either way, and a timer keeps going in a window nobody is painting --
so a playback left in a background tab does not come back with half-drawn
jumps strung across the map.

A jump whose middle is a dungeon was drawn in its own colours for a while, on
the reasoning that walking in one mouth and out of the far one is not a
teleport. That turned out to be wrong about the case that prompted it: once
the tunnel was drawn at the door it is actually used, the user could see it
was a warp back to the cave with a walk either side of it. There is no way to
tell those apart from here, so they are all teleports again and the popup says
what the recorder saw.

**A load screen can report the map you were in ten minutes ago.**
`is_no_map()` catches the honest version -- 0xFFFFFFFF -- and the sampler
drops it. What it does not catch is a reading that names a real map with a
real-looking position in it: on 5 August the chain reported m32_01 with local
(111, 14) for exactly one sample, fifteen seconds after the player was last
seen on the surface and twenty seconds before they turned up 193 m away. They
had spent 529 seconds in that tunnel ten minutes earlier, which is where the
reading came from.

It became a visit, a marker on the map, and then a place to file a death that
happened out in the open -- which is how a death marked by hand ended up
"randomly in a cave". `Store.ghost_stays()` finds them by all of: a load
screen immediately before, one or two samples, and a world position on both
sides. Two on `routes.db`, against 58 real stays whose shortest is 34 samples
over 17 seconds, so the threshold has an enormous band under it.

`drop_ghost_stay()` does not delete the rows: `warps()` pairs each sample with
the one before it *by id*, and a hole there would lose the jump on either
side. They are given the last position anybody knew about instead -- the same
thing the rest of the file does when it anchors a dungeon to the surface
position before it -- and the invented visit and any death filed on one move
with them. After it, that death sits at (11113, 9574) where the player was
standing, with the grace 20 s later at (10927, 9524).

The sampler is left alone. Telling a stale reading from a real entrance needs
hindsight -- you only know it was one sample once the next one arrives -- and
the recorder has read HP since session 30, so a death during a load screen is
found without any of this. It is the old sessions that need repairing.

**A visit with no path is still a place you were.** `playEnter()` was driven
by the runs -- the playback announced a dungeon by drawing a line in it -- and
a twenty-second stop in a tunnel with one sample stored produces no run at
all. So the playback never entered it, `playFlash()` refused to draw an
interior mark while it was somewhere else, and a death marked in there was
counted on the toll and drawn nowhere. `playPlaceAt()` asks the visits
directly, since a visit is a window in time and knows its own bounds, and
`playDrawTo()` checks it after the run loop and before the events: after, so a
replay still steps through each dungeon in turn and clears the last one's
drawing; before, so a mark made in here has somewhere to be drawn.

**And the grace has to be claimed off a map change.** `respawn_after()` takes
break codes 2 and 3 and refuses 1, for a good reason -- walking through a door
is a map change, and reading that as a respawn once moved a death on the
surface into the catacomb beside it. But when you have just said "I died
here", the map change on the sample after is not a door: it is the load screen
that moved you, which happened to change the map on the way. `_claim_grace()`
takes over a 0 or a 1 and leaves a 2 or 3 alone, both `mark_death()` and
`mark_death_at()` go through it, and `broke_next` stores the code to put back
plus one so undo restores exactly what was there.

Four deaths on `routes.db` were marked before that: one had a grace 73 seconds
and a teleport away, three had none at all. `Store.lost_graces()` finds them
by the same rule and `repair_jumps.py --graces` puts them right, rather than
asking for each to be un-marked and marked again.

**A death marked by hand writes a break, not a second event.** The user asks
for the next recorded point to become the grace, and the obvious shape for
that is a `respawn_by_hand` kind -- which would need `deaths()` taught about
it, `warps()` taught about it, and the drawing taught about it. Setting
`break_before = 3` on that sample instead does all three with a row that
already exists: `respawn_after()` reads the first broken sample after a death
as the grace, `warps()` already drops jumps within thirty seconds of a death,
and the line stops being drawn from the body to the grace -- which is the
thing being corrected in the first place.

It is only set when the sample was unbroken, because a real load screen there
is the game's word and not ours, and `map_events.broke_next` remembers whether
we were the ones who broke it so `clear_death()` can put back exactly what it
took. `selftest.py` asserts both halves, including that a pre-existing load
screen survives the undo.

The viewer has to rebuild the whole script after that, not just the events:
the route is split on breaks server-side, so the shape of the path changes
too. `playRebuild()` refetches, rebuilds, and seeks back by wall clock rather
than by elapsed -- the axis it is seeking on is the one that just changed.

**Five minutes a second goes past a death in a fiftieth of a second.** Which
makes "watch for the moment and press the button" useless on its own, so
`playStep()` walks the playback's own axis one entry at a time in either
direction, and the clock line says which point you are on out of how many.
The axis is already every timestamp in the playback in order, so this needed
no new structure -- just the inverse of `playClock()`, which `playRebuild()`
wanted anyway.

**HP-detected deaths begin at session 30.** Every session before that -- 11
through 29, which is all the imported routes and all of the early live capture
-- carries zero `kind='death'` events, because the HP chain was not being read
yet. So every death reported by hand so far has come from that stretch, and
none of them can be found by any rule when the grace is close: 00:06:29 on
5 September is six quiet stretches in a row that each moved 1.5 to 1.9 m,
which is exactly the movement gate and exactly what standing still looks like.
There is nothing in the recording to find. Anything recorded since
2026-09-05 02:16 has its deaths from HP and needs none of this.

**A jump filed as a map change was dropped on the floor.** `warps()`
selected `break_before IN (2, 3)`, so every jump the sampler had classified as
code 1 produced no mark anywhere -- and nine of them on `routes.db` are real
warps of 214 to 775 m, drawn as nothing at all. The sampler cannot tell a tile
crossing from a warp that happens to land in a different tile, because both
are a map change. The distance can, and it separates them completely: of the
twelve map-change breaks with a world position at both ends, nine are 214 m or
more and three are 5.0 m or less, with nothing between. The 50 m minimum
`warps()` already applied sits in that band, so including code 1 needed no new
threshold. Three of the thirteen deaths reported by hand were this.

Not `respawn_after()`, which still takes only codes 2 and 3 -- walking through
a door is a map change too, and reading that as a respawn moved a death on the
surface into the catacomb beside it.

**A hole in the recording is a fixed number of seconds, not a fixed number of
missed readings.** `repair_jumps.py` asked for a gap of eight times the
session's own rhythm, which is right at a quarter-second cadence and useless
at five seconds: it asks for forty, and a death costs fifteen. So every death
in the imported routes was invisible to the pass -- the gap rule could not
reach one however far the distance threshold moved. A death costs the same
eight to twenty seconds of animation and load whatever the recorder is doing,
so `quiet_after()` caps the multiple at `GAP_CAP_S = 8.0`. Measured over
thirteen deaths reported by hand, across sampling from a quarter second to
five, the smallest hole is 8.3 s. With the cap in, the dungeon pass finds the
two cave deaths on 3 August that it had been walking straight past.

**Eleven of thirteen is where the rules stop, and the twelfth needs you.**
Those thirteen were reported by hand against the recording, which is the only
ground truth this project has ever had for a death without an HP reading.
Every one of them sits in a hole of at least 8.3 s. So does standing still:
with a hole of 8 s or more, `routes.db` holds 435 stretches that ended within
5 m of where they began, 20 within 10 m, and 66 beyond. Two of the thirteen
are in that first group -- a cave death whose grace was 2.4 m away, another
7.2 m -- and no rule that catches them leaves the 435 alone.

That is not a threshold to hunt for, it is missing information, and the answer
is the same one the rest of the project reaches for: ask. `Store.mark_death_at()`
takes a moment rather than a jump and puts the death on the nearest sample,
`POST /api/death` with `at: true` calls it, and the playback carries an **I
died here** button. Watching the route back is the one context where you can
actually see the moment go past.

**The movement gate is a ceiling, and it is worth more than any threshold
picked out of a histogram.** Nothing is stored until you have moved
`min_move_m` from the last stored sample, so however long a hole is, if you
stood through it the next sample lands the moment you cross that line: 1.5 m,
plus one reading's worth of walking. Being carried through owes the gate
nothing. `find_gate_jumps()` uses `gate + 15.6 * rhythm` -- 5.4 m for a
quarter-second session, 9.3 m for a half-second one -- and it needed no
tuning, which is the point.

Two things about it are load-bearing.

It is measured **across the ground only**, because that is the axis
`Sampler._step` gates on: it compares `(wx, wz)` and ignores height. The first
version used three dimensions and found 56 candidates on `routes.db`, of which
53 were lifts -- Siofra's 380 m descent four times over, Stormveil's, the
Divine Tower's -- because a lift is standing still as far as a horizontal gate
is concerned. The tell was that `rose` almost equalled the whole step in every
row. On the correct axis it finds three, and `selftest.py` puts a lift in the
fixture so counting height fails a check rather than a review.

And it is sharp only where the sampling is. The ceiling scales with the
session's rhythm, so an imported route sampled every five seconds has a
ceiling of 80 m: a death whose grace is seven metres away sits far below it.
Measured against the thirteen deaths reported by hand it finds ten, and all
three misses are imported. That is the same wall every rule here hits, and why
**I died here** exists.

**On the surface, no speed rule can find a missed jump, and the measurement
says so.** Inside a dungeon the speeds leave an empty band -- 13,875 unbroken
steps, five between 17 and 26 m/s, nothing at all from 10 to 16 -- so
`repair_jumps.py` can put a threshold in the gap and be right. The surface has
no such band. Over the 41,149 surface steps sampled densely enough that
nothing can hide between them (a gap of a second or less), the fastest ground
speed reached is 15.6 m/s; the fastest unbroken step in the whole database is
15.9 m/s. Torrent at a gallop and a short warp are the same number.

What is left is the hole. `find_surface_jumps()` looks for a stretch that went
quiet for eight times the session's own rhythm and ended 25 m or more away:
135 m with nothing recorded in between, in a session sampling four times a
second, is thirty readings that never arrived. On `routes.db` that finds
eleven, all in the early live sessions from before the sampler counted blind
reads. It is a hint and not a proof -- a stalled recorder leaves the same hole
-- so the pass reports and writes nothing without `--surface`, unlike the
dungeon rules which have a band behind them.

The two columns that actually sort them are the user's: `came_back()` asks
whether the route returned to within 20 m of where it left inside seven
minutes, because you go back for your runes and a teleport carries on from
where it put you; and the height change, since four of the eleven fell 28 to
104 m. Four more sit at 1.8 to 2.7 m/s over 17 to 25 s, which is the shape of
a stalled recorder rather than a load screen, and the report does not pretend
to know.

**A place, for playback, is a visit and not a map.** `interiorTransform()` can
return a different frame for two visits to the same cave -- one walked in, one
warped to a grace inside it -- and `buildPlayback()` keyed its frames by
`map_id`, so whichever visit came last in the list decided where that cave's
deaths were drawn. The one that lost put them on the entrance. Keyed by
`insideKey(v)` now, and `playEvents()` finds the visit a mark belongs to by
its own timestamp, which is not a guess: a death has one moment and a visit
has a window.

The other half of the same bug was the fallback. An interior death carries
`d.xy` as well as `d.local`, and that `xy` is the *entrance* -- it is what
`/api/deaths` hands back for want of anywhere better. Falling through to it
when a frame was missing drew the death at the cave mouth with the cave's real
path on screen beside it. There is no fallback now: a mark made inside is
drawn in its visit's frame or not at all, and it still counts on the toll
either way.

**Correcting a mark during playback must not redraw the map behind it.**
`callDeath()` ends with `loadInteriors()` and a rebuild of the hover overlay,
which is right for the map and wrong here -- the overlay is what the playback
is drawing into. It returns early when `play.on`, and `playReclassify()`
rebuilds only the events: marking a death writes its own event and does not
touch a single break, so the path is exactly the path it was, and re-seeking
to where you were puts the corrected mark on screen without losing your place.

**Going back into a cave should show the cave, not just this run.**
`playEnter()` cleared the interior drawing on every entry, so the twelfth run
through Stormveil was one thin line in an empty frame -- the shape of the
place, which is most of what there is to recognise, thrown away at the moment
you stepped back into it. It now redraws every completed run belonging to the
same map at 0.4 opacity underneath, and the caption says how many. The runs
are already sorted by time and `play.cursor` is the boundary between done and
not, so "every earlier run" is a scan of `runs[0..cursor)` filtered by map --
no extra state.

**The growing line is one layer, and clearing its group orphans it.** The run
being walked is drawn once and handed more points on every step. `playEnter()`
clears `insideGroup` when the drawing moves to another place -- which takes
that line off the map without telling the loop, and the loop goes on calling
`setLatLngs` on a layer nobody can see. So walking into a cave showed the
position mark and nothing else until the run finished and was redrawn whole,
which on the first cave in `routes.db` is eight minutes later. `playDropHead()`
forgets an interior head whenever that group is cleared, so the next step
draws a fresh one. Measured after: 43 ticks inside with a head, none of them
detached, and two ticks at the mouth with nothing drawn because the run does
not have two points yet.

**Playback marks belong above the position mark.** A death is the thing that
just happened; the dot is only where you are. The dot sat in pane `you` (680)
and the marks in `deaths` (590), so the dot covered a death at the moment it
arrived, which is the one moment it is for. Playback marks have their own
panes now, `playmarks` at 690 and `playinside` at 700 -- two of them because
dimming has to tell them apart: the world's marks go down with the world when
you walk into a dungeon, the marks made inside that dungeon do not.

**`buildPlayback()` has to learn the frames itself.** `interiorTransform()`
reads `state.dungeonFrame`, which `loadInteriors()` learns, which is the last
thing `boot()` does. Press Play before that finishes and every dungeon is
drawn by pinning each visit's own first step to its own anchor -- so a cave
whose first visit was placed by where you came *out* was drawn from the wrong
end, and pressing Play again after boot settled fixed it. That is the shape of
a race, and the report of it was precise: "if I let it play until I reenter
from the other side, and then go back to the start, it lines up". Measured on
m18_00_00_00, whose two visits are placed `exit` and `entrance`: the exit
visit is drawn 96 m out without the frame and 0 m out with it, and the
entrance visit -- the one the frame is taken from -- is unaffected either way,
which is exactly why entering from the other side looked like the cure.
`buildPlayback()` awaits `learnDungeonFrame()` itself now; the paths are all
cached by then so it costs nothing, and it cannot be skipped.

**A mark arriving is the mark growing, and neither obvious way to scale it
works.** The first version drew rings out of the mark: expanded with
`transform: scale()`, which scales the border too, so a 2px ring arrived five
pixels thick and got heavier the further it went -- the opposite of a ring on
water, and most of why it read as odd. The second drew no rings and grew the
mark itself, which is the effect wanted, and hit the two traps that stand
between you and scaling a Leaflet marker:

`transform` on the icon element is out, because that is where Leaflet writes
`translate3d(...)` to position it, and a keyframe beats an inline style -- the
mark scales beautifully at the corner of the pane. The individual `scale`
property looks like the answer, since it composes rather than replaces, but
the individual transform properties apply *before* `transform`, so the
translate gets scaled with everything else: measured, a mark 47 px from its
pane origin slid 23 px across the screen at 1.5x, and one at 7,094 px would
have left the map.

So a playback mark is wrapped. `.pop-holder` is what Leaflet positions, the
element inside it is the mark you can see, and the pop scales that. Measured
on a holder at (-7094, -1730): grows to 1.5x with the centre moving 0 px on
both axes. A cave pin takes `transform-origin: 50% calc(100% + 7px)` so it
grows out of its tip and the place it is naming stays put.

**A playback that starts with the map already marked gives the route away.**
The dungeon pins were deliberately left on the map while it played, as
landmarks. They are the wrong thing to leave: every cave you are about to find
is already pinned before you set off, which is most of what there is to watch.
`markerGroup` comes off with the rest now, and the pins are put back one at a
time as an event of their own -- kind `found`, at the first visit to each map,
deduped by map ID so going back into a cave does not put a second pin down.

**Half of a teleport and half of a death were missing.** The playback drew a
teleport's arrival and a death's mark, and neither has much meaning alone: a
jump with one end drawn reads as appearing out of nowhere, and a death without
the grace it put you at loses the walk back that is most of what a death costs
you. Both ends of a jump are marked now, and a respawn is its own event at its
own moment -- about twelve seconds after the death, which is when it happened.

That doubled the marks, and where you warp out of the same grace a dozen times
they landed a dozen deep on one pixel. `playMark()` clusters within 12 px per
kind and counts up, the way the finished map does -- the difference being that
these arrive one at a time, so the cluster is built as it goes rather than from
a finished list. `play.spots` holds them, and the ones inside a dungeon are
dropped when `playEnter()` clears that dungeon's marks: leaving them would have
the next visit counting up a tally whose marker is no longer on the map.

**Playback is a mode, and the awkward half of it is the caves.** The world
path is one line in one coordinate space and reveals itself point by point
without trouble. A cave is not on that plane at all, so it cannot be part of
the line: its path is in the dungeon's own metres, and the only honest way to
show it is the way hovering a marker already shows it -- dim the world, put
the dungeon up in its own frame, and carry on drawing in there. So the unit
of playback is a *run* -- a stretch of one colour, the same unit
`drawSegments()` bands the route into -- and every run carries which place it
belongs to. Entering and leaving a dungeon falls out of that field changing;
there is no separate schedule of visits to keep in step.

Two things were got wrong on the way to that. Fifty visits, each needing its
own `/api/interior`, were fetched in a row: fifty round trips before the first
frame, which left the button reading "Building" for long enough to look
broken. They do not depend on each other -- `Promise.all` and it is 2.7 s.
And the marks made inside a dungeon were put in the same group as the world's,
which outlives the visit: a teleporter in Stormveil left its arc lying across
Limgrave for the rest of the playback, drawn at the castle's position on a
pane that dimming does not reach. Interior marks now live in
`play.insideMarks`, cleared by `playEnter()` along with the path they belong
to, and `playFlash()` refuses to draw one at all when the playback is not
currently in that dungeon -- which is what happens on a seek, where every
event up to the target fires in one go after the runs have settled.

**Seeking is a rebuild, not a rewind.** `playSeek()` clears everything and
replays from zero to the target with the animations suppressed. Undoing would
mean unwinding the toll, the marks, the dungeon the drawing is inside and the
half-drawn run, and getting the same picture whichever direction you arrived
from. Replaying 1,252 runs measures well under the frame it has, because only
the run being walked is ever redrawn -- completed ones are handed to the
canvas once and left alone.

**The timeline is the playback, so it is the width of the map.** It began as
an inset panel with rounded corners floating 18 px off the bottom, which made
it one control among several -- and the thing it measures is the whole route
against the whole screen. It is flush to the sidebar, to the right edge and to
the bottom now, and the track inside it is full-bleed.

Three things fell out of that. Chrome will not fill the played part of a range
input (`::-moz-range-progress` has no webkit twin), so the track paints it as a
gradient stop and `playClockText()` moves it -- driven off the clock rather
than off the drag, so a seek moves it too. The transport went *on* the track
to begin with and came off again: it covered the one thing the bar is for, and
took every click over the middle tenth of the route with it. The track is the
top of the bar now and everything that drives it is underneath, out of flow
(`.play-mid` is absolute at `left: 50%`) so that neither the clock on one side
nor the button on the other can push play off the middle of the map. The
speed sits to the left of it and an empty element of exactly that width to
the right, for the same reason. And the toll was centred with
`left: calc(348px + 50%)`, where the 50% is half the *window* -- half the
sidebar's width right of where it belonged, and clipped off the edge at a
narrow window. Half of what is left after the sidebar is `calc(174px + 50%)`.

Where you are in the route needs 429 px at its longest and the slot beside a
centred transport is 275 px, so the clock is two lines: the moment on top and
what it is out of underneath. Two elements rather than one string, because
`playResume()` also writes there when the playback finishes, and a
`textContent` on the container would have taken the other line with it.

The speed is a slider over fourteen detents rather than a linear range: 30
seconds a second to an hour is two orders of magnitude, and spread evenly the
thumb spends most of its travel above half an hour where every position looks
the same. The stored preference is still seconds a second, because that is
what it always meant -- only the control changed -- so an old value the ladder
does not have (the select offered two hours) lands on the nearest one it does.

And there is one button for the mode. **Done** on the bar was a second place
to look for the same thing, so the panel button starts it and then says
**Back to the map**. Which is what turned up the hole in `MISSING`, the stub
that keeps one absent control from taking the route down with it: it carried
`classList.toggle` alone, while `playPause()` had been calling
`classList.add`/`remove` on the play toggle since the playback was written --
so the stub would have thrown from inside the very handler it exists to keep
alive. It answers everything the wiring calls now, and `selftest.py` checks
that rather than trusting it.

**The speed is a multiplier, and the reading belongs to the slider.**
"5 min a second" is what you would say out loud and it is three words to read
at a glance, where the same thing is a number you can compare between two
settings: 300 seconds of route in a second is x300 of real time. So the label
is the multiplier and the sentence is on the hover, and the reading moved
from the far side of the play button to sit against the slider it belongs to
-- which puts both on one side, so `.play-balance` is an empty element of
exactly that width on the other and the button still lands on the middle of
the map. Measured on a 1400 px window: the play toggle's centre and the
timeline's are both 874. The hover is not rounded to whole minutes, because
three of the fourteen detents are halves and "2 minutes" beside a x90 would
be the explanation disagreeing with the thing it explains.

**`#caption` and `#timeline` both want the bottom of the map.** The caption
sits at `bottom: 18px` and the playback bar landed on exactly the same line,
so the one thing telling you which dungeon you were in was behind the one
thing you were reading. `body.playing #caption` lifts it to 92px. The class
goes on `body`, not on `#map`: the caption is a sibling of the map, not a
child, so the map's own class cannot reach it.

**A mark placed at a dungeon's mouth for want of anywhere better is the
dungeon's to show, not the world map's.** A death in a cave has no world
position, so `/api/deaths` hands back the cave's entrance to put it on -- and
the pin standing at that entrance already counts its deaths in a badge. So a
cave you died in nine times wore a red 9 and stood under a red 9, saying the
same thing twice a few pixels apart, and the cluster was the one of the two in
the wrong place, since the deaths did not happen at the mouth. `hiddenInside()`
drops interior deaths and respawns from the world clustering, but only while
`state.layers.interior` is on: with Caves and dungeons unticked there is no pin
and no badge, and a death is not a cave, so it goes back to the entrance rather
than disappearing along with the layer that was standing in for it. On
`routes.db` that is 49 of 96 deaths, all of them still drawn on their dungeon's
own path where they belong.

**The pins are built from the answer already in hand.** `loadInteriors()`
cleared `markerGroup`, then awaited `learnDungeonFrame()`, which fetches a path
per dungeon in sequence, and only then drew the markers. So every redraw --
a zoom, a plane switch, a death -- took the cave pins off the map for the
length of two dozen round trips. That is "the cave icons disappear for a little
bit": nothing was wrong with them, they were queued behind somebody else's
fetches. The marker loop now runs straight off `/api/interiors` and the frame
learning happens after, since nothing the pins draw needs a frame -- only the
paths do, and those are drawn on hover, by which time it is long since learned.
`selftest.py` asserts the order.

**And the turn was measured against a door that was never there.** The first
answer above was wrong in the way that matters: the -77 degrees was real
arithmetic on a fictitious input, and "it lines up at both doors" was a
circular check -- of course it does, the rotation was fitted to exactly those
two points. What it says nothing about is the shape in between, which is what
was reported next: "it's still turned the wrong way, and I tried to leave and
enter it again, but it didn't fix it."

m14_00 is **15 by 17 m** as walked on 8 September, and its two "doors" were
104 m apart. A room cannot do that. The 8 September visit's `came_from` is
m31_06 -- **a cave** -- so the player walked from one interior straight into
another, and the anchor written for it was `last_surface`: where they were
standing before they went into *the cave*, 104 m from m14's actual door and
1.2 m from where they came back out of the cave twenty minutes later.

The file already knew. `off_the_surface()` sits in `interior_visits()` and its
docstring says it outright -- "the anchor only claims to be a door for a
dungeon entered from the surface. Walking from one interior into another
carries the last surface position forward unchanged, so that anchor is a
leftover rather than a claim" -- and it was used for the trap rule and never
consulted when the tier was assigned. So a leftover was labelled `entrance`,
the most trusted tier there is, and then `frameTurn()` measured an angle
between it and a real door and turned the whole place 77 degrees.

The anchor is dropped now, and dropped rather than corrected: `the doorway`
tier below is the one that does this properly, working the position out from
where you left the last dungeon in that dungeon's own frame. The fixture
confirms it lands there.

Two narrowings, both learned from the selftest rather than reasoned out
first. Not `not off_the_surface()`, which also catches a visit whose
provenance is unknown -- the first dungeon of a session, or any history whose
enter events do not reach back that far -- and throwing away a real anchor
because nothing vouched for it is the worse mistake of the two. And not when
`came_from` is the same map: coming back into the one you were just in says
the chain lost track of where you went in between, which is missing
information rather than evidence. The rule drops an anchor only against
evidence, never for the lack of it.

Measured on `routes.db`: m14_00's five visits now agree on one door, the turn
is **0** instead of -77, and the frame is based at the door actually walked
through. m31_06 -- the cave m14 was entered from, which had the same leftover
-- went from **132 m out at its own door to inside 10**. What is left above
10 m is m30_11 at 193 (two doors 178 m apart on the map and 15 m apart
inside, which is not orientation and no turn will fix), m31_10 at 33 and
m31_15 at 29. The three turns still in use are -3.2, 0.2 and 1.8 degrees.

The lesson worth keeping: **a rotation fitted to two points always satisfies
those two points.** Checking it there proves nothing. What has to be checked
is whether both points are things you measured or one of them is something
you inferred -- and this codebase has a tier system that already answers that
question, if you ask it.

**Which mouth you use a place through and which *reading* of that mouth to
trust are two questions, and one vote was answering both.** Reported, after
the drag was made rigid: "the path is now accurate, but the icon is too far
away -- it should be placed exactly where you go from the overworld into the
cave."

The marker sat **13.3 m** past the mouth. It had to: the pin is the frame's
base point, and the frame was hung off a reading of that doorway taken 10 m
inside it. m31_15's doorway has been read five times --

| when | cadence | surface anchor | first step inside | seconds |
| ---- | ------- | -------------- | ----------------- | ------- |
| 3 Aug | 5 s (import) | **18.6 m short** | **10.2 m past** | 405 |
| 4 Sep 22:18 | live | 776 m off (a warp) | (nothing stored) | 21 |
| 4 Sep 22:25 | live | 518 m off (a warp) | (nothing stored) | 35 |
| 4 Sep 22:53 | live | 0.9 m | 0.1 m | 18 |
| 12 Sep 13:32 | live | 0 m | 0 m | 213 |

-- and the three live ones agree to a metre while the imported one straddles
the threshold by 18.6 m outside and 10.2 m inside, because five seconds of
walking separates its last surface reading from its first inside one. That is
one doorway sampled coarsely, not two doorways. `agreedDoor()` weights by time
inside, 405 s beat 231, and the whole cave hung off the worst measurement of
it -- while 18.6 m being just over `SAME_DOOR_M` kept it in a cluster of its
own so nothing outvoted it.

Time is the right question for *which mouth*: that rule was measured, and a
283-second traversal being outvoted by two 30-second pokes at the far mouth is
what it exists to stop. It is the wrong question for which reading of a mouth
to believe, where the evidence is agreement between independent readings and
nothing else.

So the drawing now hangs off the reading the most other readings agree with,
and the comparison is by the frame each one *implies* -- the door's map
position minus its local one, which is where the dungeon's own origin lands.
Two readings of one doorway imply the same origin to within sampling noise,
and so do two real mouths of a dungeon whose inside is laid out to match the
ground above it, which is what makes it the right thing to count. A reading
that implies a different origin is a bad reading however plausible it looks
from outside. Time survives as the tie-break.

Measured over the 14 dungeons in `routes.db` with more than one reading of a
doorway: **two change and twelve do not**, and no dungeon's worst door error
rises. m31_15 goes from exact at the coarse reading and 28-29 m out at the two
good ones to exact at both good ones. m18_00 swaps between two readings 10 m
apart, which is neither better nor worse. Everything else -- Stormveil at 2 m,
m14_00 at 1 m, m31_10's nine doors, m30_11's un-turnable 193 m -- is
untouched.

For the cave that was reported the frame is now based at local (48.7, -41.3),
the point four of its five visits recorded as their first step inside, and the
marker stands exactly on it: 0 m between the pin and the frame's doorway,
where it was 13.3 m. Dragging the pin those 13.3 m onto the real mouth leaves
the near mouth **0.2 m** out and the far one, 180 m away through the cave,
**4.6 m** out.

One limit worth knowing, since it is the same 18.6 m: the *marker* still goes
where `agreedDoor()` says, and with the hand placement cleared that would be
the coarse reading -- so the pin would sit 18.6 m from the mouth while the
drawing was right. Clustering the marker vote by implied origin as well would
merge the two real mouths of any unturned dungeon into one, which is exactly
what the second-mouth pins exist to keep apart. Left alone rather than traded.

**A pin stands at a doorway, so dragging it says where the doorway is -- and
nothing else.** Reported: "I was trying to fix a cave position, but when I
moved the icon, suddenly the path moved a large amount."

`interior_visits()` overwrote every visit's anchor with the hand position and
labelled it `by hand`, which left the viewer nothing to pin the drawing *by* --
so `learnDungeonFrame()` fell back to centring the whole shape on the pin. That
is right for a place the route knows no doorway for, and wrong for every place
it does: the frame changed *kind* at the moment of the drag, from pinned-at-the
-door to centred-on-the-extent, and the drawing jumped by the distance between
those two points. On m31_15, the cave that was reported, that is **96 m** --
whichever way you dragged, and however far.

Measured on that cave: the pin moved 57 m from the door the route had agreed
on, and the path moved **43 m in another direction**. After: the pin moves
57 m and the path moves 57 m. Moving the frame's `xy` by 361 units moves the
drawing 361 units. It is rigid now, which is the only behaviour a drag can
have that anyone can aim.

So the anchor the hand placement replaces is kept beside it -- `door_wx`,
`door_wz` and `door_placed` on the visit, `door_xy` and `door_placed` in the
payload -- and `learnDungeonFrame()` learns the frame from the route for every
dungeon, hand-placed or not, then moves it: `frame.xy = the hand position`. The
point inside it is tied to, and the turn measured from a second door, are the
route's measurements and are not a drag's to change. `agreedDoor()` takes an
accessor for the same reason, because for a hand-placed map every visit's `xy`
is the one position you gave and the doorways being voted on are elsewhere.

Where the route knows no doorway at all -- warped in, never walked out -- there
is still nothing to pin to, and the centred frame from the round before stays.
Measured: the Chapel of Anticipation and Leyndell keep it to 0 m; the cave
picks up its door.

**Two things found on the way, both left alone.**

The doors sent are the ones the tiers inferred *before* `_agree_on_one()` runs,
because the hand placement is applied early and the votes come at the end. So
m31_15's five doors reach the viewer with two of them 776 m and 518 m out --
warps the game never announced, which is exactly what that vote exists to pull
back in. It is harmless today and measured to be: `agreedDoor()` weights by
time inside and both are seconds long, and `frameTurn()` refuses a pair whose
two lines disagree about their length, which those do by hundreds of metres.
Moving the hand placement to the end, after the votes, would need
`through_the_door()` handed the hand position separately -- it reads
`frame_of()` before the votes run -- and that is more surgery than the payload
tidiness is worth.

And the route's own answer for this cave is **18 m off**, which is why it
looked wrong enough to drag in the first place. Five entrances: 3 August at
18 m from the real mouth with 405 seconds behind it, 4 September at 776 and
518 m (warps, 21 and 35 s), 4 September at 1 m (18 s) and 12 September at 0 m
(213 s). `agreedDoor()` weights by time, and 18 m is just over `SAME_DOOR_M`,
so the 405-second visit is its own cluster and beats the 231 seconds that
land on the real door. Nothing here is wrong -- the vote is doing what it was
measured to do -- it is a boundary case of a 15 m tolerance, and the answer
for it is the drag, which now works.

**Worth knowing: a cave you walk in one mouth and out the other gives only one
door to the turn.** m31_15's two mouths are 184.1 m apart inside and 179.4 m
apart on the map -- 2.5%, a rigid turn of 0.2 degrees, exactly the case
`frameTurn()` exists for. It never sees them, because the visit that walked
through is tier `entrance`, and the door loop only takes an exit anchor from a
visit whose *tier* is `exit`. So the far mouth of a through-walk is never
collected. It costs nothing on this cave, whose turn is 0.2 degrees, and it is
the reason the second door of every through-walked cave is invisible to the
rotation.

**A jump you can already see needs no help finding.** Asked for: "currently
during playback it centres your position on the screen during a teleport, like
I wanted it to do, but if you're teleporting to somewhere close by it can look
a bit jarring, so make it so that if the teleport is within what is visible on
screen, then it won't recentre."

One condition on the arming: the arrival has to be off the map's own bounds
before the centring is set up at all. Left alone, the ordinary follow does
what it always did -- nudges the arrival inside the edge margin if it needs
to, and nothing at all if it does not -- so a short jump now costs no movement
whatever.

Measured over the 98 surface jumps on a 1052x900 map, with the map sitting on
the departure end the way the playback leaves it:

| zoom | median jump | left alone | still centred |
| ---- | ----------- | ---------- | ------------- |
| 3 | 86 px | **96** | 2 |
| 4 | 173 px | 80 | 18 |
| 5 | 345 px | 64 | 34 |
| 6 | 691 px | 39 | 59 |
| 7 | 1,382 px | 16 | 82 |
| 8 | 2,764 px | 9 | 89 |

Which is the right shape: at the overview zoom almost nothing moves the map,
and close in almost everything does, because close in almost every jump really
does leave the screen. Verified in a real playback at zoom 4: an 89 m jump
moved the map **0 px** and a 4,735 m one landed **dead centre**.

**And a jump can put you back where you were looking instead of in the
middle.** Asked for: "add a new setting that when enabled, will place the
marker where you teleported to at the relative position of the screen -- if
you teleport from the bottom left of the screen, then the teleport-to marker
should also be the bottom left of the screen, so you stay focused on the same
spot while viewing. This would only happen if the teleport is off screen, like
the current centring."

`Keep the same spot on a jump`, in the Follow section, because it is only ever
consulted while following and it changes what following does. Off, a jump that
leaves the screen centres its arrival, as before; on, the arrival lands on the
point the departure was standing at.

One move rather than a centring and then a nudge. The container point of a
projected point p is `p - project(centre) + size/2`, so the centre that puts
the arrival on `at` is `project(arrival) - at + size/2` -- and the map is sent
there once, unanimated, for the reason every other pan in the playback is.

Two things it has to get right and both are about *when* and *where the eye
is*. The departure's screen position is read when the jump fires, not when it
lands: `PLAY_TRAVEL_MS` of follow-panning happens in between and the map is
not where it was. And the point is clamped into the edge margin -- the same
one the follow uses -- because a jump can fire on the frame the mark is
crossing that margin, and honouring a departure that was half off the screen
by landing the arrival off the screen would be the exact opposite of the
point.

Measured on a 1052x900 map at zoom 5, on a 6,963 m jump:

| | departure was at | arrival landed at |
| --- | --- | --- |
| centring | (896, 132) | (526, 450), which is the middle |
| holding the spot | (894, 253) | **(894, 253)** |

The same pixel, to zero. And the clamp: a departure recorded at (-200, -300)
or at (1452, 1000) lands the arrival at the corner of the edge margin --
(146, 127) and (906, 773) on that window -- rather than off the screen, while
a departure in the middle is honoured exactly.

The on-screen guard is shared, so nothing changed for a jump you can already
see: on the 89 m jump at zoom 4, nothing is armed with the setting on or off.

**And where a jump goes is a hover answer, wherever the jump is drawn.**
Reported in the same breath: "certain teleport markers, in legacy dungeons at
least, don't show where they lead to when hovering, and some always show
where, even when you're not hovering." Both halves are the same inconsistency
seen from two sides.

The world's teleport marks have drawn a dashed line to the far end on
`mouseover` since they were written. The marks drawn *inside* a dungeon never
had a hover at all -- and instead the line between a jump's two ends was drawn
into the dungeon's group at build time, which for a cave means "while you are
hovering the cave" and for a legacy dungeon means **always**, because a castle
is on the map at all times. So one kind of mark answered when asked, one kind
answered without being asked, and one kind never answered.

`showWarpLines()` now takes pairs already resolved to map pixels, plus the
group and renderer to draw into, and every teleport mark calls it the same
way. Measured on `routes.db`: the seven lines that used to stand permanently
on the castles are **0 at rest, 1 while you point at a mark, 0 after you leave**.

The third kind was mine, from the round that put a gate's interior end where
it happened. I wrote that the line "is only drawn when both of its ends are in
this frame. Where one end is out on the surface there is nothing here to join
it to, and a line to a made-up point would be worse than none" -- which is
wrong, and the give-away is in the sentence: the surface end is a *recorded
position*, not a made-up one. The two ends are in different coordinate spaces
right up until both are projected, and then they are the same pixels. So the
13 gate ends on those castles pointed nowhere for want of a line that was
always drawable. They say where they went now -- verified on the Chapel of
Anticipation's 2,463 m jump.

**The line still being written ignored the thickness and the outline.**
Reported: "there is a bug where the line thickness/outline is forgotten, so I
have to adjust it back and forth to get it to update."

Not the preference -- that stores and restores correctly, which is what the
first hour of looking proved and it was the wrong hour. `redrawLive()` drew
the tail at `weight: 3.5` with no casing at all. That is the default thickness
plus a little, and nothing like the setting at any other value:

| the panel says | the committed route | the live tail |
| -------------- | ------------------- | ------------- |
| 3 line, 3 outline | 3, outline 6 | 3.5, **no outline** |
| 8 line, 5 outline | 8, outline 13 | **3.5**, no outline |
| 1 line, 0 outline | 1, no outline | **3.5** |

And the tail is not a couple of uncommitted samples -- nothing refetches the
route while you play, so it is everything since you last happened to zoom.
Which is also the whole of the workaround: nudging the slider calls
`reload()`, the route comes back from the server with those points in it, the
tail shrinks to nothing, and the mismatch stops being visible. "Adjust it back
and forth" was not making the setting apply. It was hiding the part of the
path that had never been asked.

The tell was one function away, again: the tail *inside* a dungeon has been
drawn at `Math.max(1, state.weight) + 0.6` since it was written. Two live
tails, one following the setting and one frozen. `liveWeight()` is that one
rule now and both ask it, so the surface tail goes from 3.5 to 3.6 at the
default -- a tenth of a pixel, against being wrong by five at any other
setting.

The outline needed the same structure the route's does. Every run's casing is
laid down before any run's line, because within one canvas the order layers go
on is the order they are painted, and a run's outline drawn after its
neighbour's line would sit on top of it at the seam. The route solves this
with a whole group of its own (`casingGroup` added before `routeGroup`); the
tail does it in two passes over the same group, which is the same statement
without a second group to keep in step. Measured on a 600-point tail with two
breaks across a fifteen-minute horizon: 14 runs, 14 outlines, every outline
added before every line, and the head's outline growing with its line as
samples arrive.

**And a check that cannot fail is worse than no check.** The first version of
the outline check asserted `"weight: weight + state.casing," in page` -- which
is also the line `drawSegments()` uses for the route's own casing, so it
matched whatever the tail did. The negative test caught it: sabotaging the
tail's outline changed nothing. Aimed at the tail's own text
(`renderer, color: '#0d0b08', weight: weight + state.casing,`) it bites. This
file already says to sabotage the string the check actually looks for; the new
shape of the trap is that the string was real, in the right file, and
belonged to something else.

The heredoc ate a backslash again on the way past, for the fifth recorded
time, turning `"\\n"` in a patch script into a real newline inside a Python
string literal. Write the patch script with the editor.

**A break splits the live tail; it does not throw it away.** Reported: "there
is now a bug with the path when you die, where parts of it disappear, and when
you scroll it gets back."

Mine, from the round before. The tail used to be cleared on a break with
`state.live = []; newLiveLine();` -- which left the polyline already on the
map exactly where it was, as an orphan nobody held a reference to, and started
a new one beside it. That looked like nothing happening and was doing the
right thing by accident: everything since the last refetch is drawn *only* in
the tail, so those orphans were the only copy of that stretch of route.
Rewriting the tail as a list of runs gave it a real `redrawLive()`, and the
first thing that does is `liveGroup.clearLayers()`. So a break now took the
whole drawn tail off the map, and a scroll fetched it back from the server --
which is the report, exactly.

Measured on a simulated session: one break took the drawn tail from **59
points to 0**. After: a real break from the feed took it from 187 to 189, and
the line split in two.

The answer was already in the file one function away. `state.liveInside`
pushes a **null** into its buffer for a load screen inside a dungeon and
`redrawLiveInside()` splits its runs there, "because a load screen inside a
dungeon is a death or a lift and the line should not be drawn through the
rock". The surface tail does the same thing now: a break pushes a null, and
`redrawLive()` ends a run at a gap, at the end of the buffer, or where the
band changes.

The two reasons to end a run are not the same and the seam says which. A
change of colour shares its boundary point with the next run, so the line is
never broken by its own gradient; a gap does not, because breaking there is
the point. Measured on a 600-point tail with two breaks in it across the whole
of a fifteen-minute horizon: **14 runs, 12 colours, 11 seams joined and 2
broken**, and 611 drawn points for 600 real ones -- the eleven extra being the
shared seams. All three colourings give one run per stretch when there is
nothing to band.

The lesson worth keeping: **an orphaned layer nobody holds a reference to can
be load-bearing.** Tidying it up is the obvious right thing and it deleted a
drawing that was the only copy of what it showed. What made it invisible in
review is that the old code said `newLiveLine()` where it meant "and leave the
last one where it is".

**A second mouth cannot be further from the first than the dungeon is big,
and the pins were the one rule not asking.** Reported: "I just teleported from
the overworld into a cave, and it created an exit icon for the cave in the
overworld where I teleported from."

`otherMouths()` took every visit's anchor that was more than `SAME_DOOR_M`
from the agreed door and drew a pin at it. That is right when the anchor is a
doorway and says nothing at all when it is not -- and this codebase has
measured the test that tells the difference twice already, for `warps()`
(is this map change a door or a gate?) and for `_agree_on_one()` (can the
entrance vote overwrite this anchor?). Both ask the size of the place. The
pins are the third question of that shape, and they were asking nothing.

On `routes.db`, with the anchors the route actually holds:

| dungeon | mouths apart | the place is | |
| --- | --- | --- | --- |
| m30_12 Catacombs | **504 m** | 169 m across | dropped |
| m30_11 Catacombs | **178 m** | 147 m across | dropped |
| m31_15 Cave | 19 m | 233 m | kept |
| m31_05 Cave | 94 m | 134 m | kept |
| m18_00 | 147 m | 234 m | kept |
| m10_00 Stormveil | 543 m | 654 m | kept |

m30_12 is the one that was reported. m30_11 came out with it and deserves to:
this file already records that its two anchors are 178 m apart on the map
while the points just inside them are 15.2 m apart, which is not two doors by
any reading -- it was being drawn as a second mouth for want of anyone asking.

The extent had to be lifted out of `warps()` to get there. `Store.
dungeon_extents()` is the cached accessor now, `_warps()` reads it instead of
reaching for `_cached("extents", ...)` itself, and `/api/interiors` sends
`extent_m` with every visit, because the viewer has no samples of its own to
measure a dungeon with. One number, three rules, no way for them to disagree
about the same cave.

Nothing is dropped for want of an extent: a dungeon with nothing recorded
inside says nothing about its own size, and `room &&` is the whole of that --
the same rule the placement tiers follow, which is to drop an anchor against
evidence and never for the lack of it.

**What actually produced that anchor is a ghost stay, and the existing
detector does not see this shape.** Worth writing down because the pin was the
symptom and this is the disease.

The recording: at 00:12:15 on 12 September the player is on the surface at
(9077, 13653). At 00:12:21 -- six seconds later, one sample, **break code 1**
-- the chain reports m30_12 with local (-36.6, 118.8). At 00:12:29 they are on
the surface again 530 m away with break code 3. They had spent **583 seconds
in m30_12 forty-four minutes earlier**, and that reading is 14.3 m from where
they were last seen in it.

That is exactly the pattern this file already describes -- "a load screen can
report the map you were in ten minutes ago" -- with the sentinel on the other
side. `Store.ghost_stays()` requires the load screen on the *first sample of
the stay*, and here it is on the sample after, so the stay was kept, written
with an entrance anchor at the place the warp left from, and drawn as a mouth.

Relaxing that clause to "a load screen on either side" finds three stays on
`routes.db` and no real one competes: the shortest genuine stay of any length
is 3 samples, and the three flagged are 1, 2 and 2. It is not done, because
dropping a recorded stay rewrites history through a repair pass and that is
the user's call, not a thing to slip into a round about an icon.

**And the sampler cannot tell these apart at the moment it happens.** Worth a
measurement so it is not tried again. The obvious rule is that walking into a
cave is seamless while a warp sits in a hole -- and the hole is known when the
map change is processed, no hindsight needed. Measured over every crossing
from the surface into a dungeon on `routes.db`: 53 with no load screen, whose
gaps run 0.25 s to 73.74 s, and 16 with one, whose gaps run 4.01 s to 81.75 s.
They overlap across the whole of 4 to 10 s, which is where this warp sits at
6.01 s. There is no band to put a threshold in, because standing still at a
cave mouth and being carried through one leave the same hole. The sampler
stays as it is.

**The age ramp is a statement about now, and nothing was moving it.**
Reported: "when using 'by age' during normal gameplay, the gradient won't
update until you do a scroll on the page."

`state.span` -- the oldest and newest moment drawn, which the whole ramp is
measured back from -- is written by `drawSegments()`, and `drawSegments()`
runs only from `reload()`, and `reload()` runs only when you zoom or change a
filter. So while you play, the near end of the gradient sits wherever your
last scroll left it, and every drawn stretch keeps the colour it was given
then. The scroll is not a workaround, it is the only thing that ever
recomputed the answer.

Measured on a simulated session at the fifteen-minute horizon, over 131
seconds of recording with the map untouched:

|                               | before | after |
| ----------------------------- | ------ | ----- |
| the newest end advanced        | **0 ms** | 134 s over the 130 s that passed |
| drawn stretches changing colour | **0**, one state throughout | 8, in four states |
| the legend's older end          | frozen | 12:12 → 12:19 |

And then one scroll moved the newest end **203 seconds in a single step** and
redrew the lot, which is exactly what the report describes.

Three things, and they are one statement said in three places.

**The feed advances the newest end.** One line in the websocket handler. The
ramp is deliberately measured back from the newest thing *drawn* rather than
from the wall clock -- with the recorder off, "now" is hours after the last
sample and the whole route slides into the oldest colour -- and while you are
playing the newest thing drawn is the sample that just arrived.

**What is on screen is recoloured, not fetched again.** `recolourRoute()` is
`playRecolour()`'s idea for the map: work out what each stretch should be now
and `setStyle` only the ones that have crossed into another band. Each drawn
stretch is registered with the moment its colour came from, which is all that
is needed to work out the new one. A refetch would be a hundred times the work
to recover a picture already on the screen: 0.3 ms median and 1 ms worst over
340 stretches, against a tick of one second. The legend goes with it, since it
is the same statement written in words.

**And the live tail is banded like the rest of the route.** One flat line was
right while the tail was a second or two of uncommitted samples. Nothing
refetches the route while you play, so the tail is really *everything since
the last time you happened to zoom* -- and drawn in a single colour it is the
loudest possible way of saying the gradient has stopped. Measured on a full
4,000-point buffer spanning the horizon: one polyline before, **12 runs in 12
colours** after, each starting on the point the one before it ends on, rebuilt
in 1.92 ms -- of which 1.87 ms is the banding itself, so there is nothing to
be saved by trying to skip the rebuild. The whole tick is 2.8 ms mean and
4.3 ms worst with that buffer and 340 stretches, once a second.

Only under `by age`. In the other two modes the tail's brightness is what
marks it as the live one, and neither height nor a solid colour changes with
time.

The head of the tail follows the feed on every sample and the banding behind
it is brought up to date on the tick, so it is a second stale at worst. The
same compromise the playback already documents: what a pass like this cannot
fix is where one band ends and the next begins, only what colour each one is
-- except here the tail is rebuilt rather than restyled, so it gets both.

**A condition written the wrong way round put 18 polylines where one belongs.**
Found by counting the runs in each mode rather than by reading the loop.
`if (i !== pts.length && !flat && bands[i] === bands[start]) continue;` --
where `flat` means the mode has no bands -- is false all the way down when
`flat` is true, so every point ended its own run: an 18-point tail came back
as 18 polylines of identical colour. It wants `(flat || bands[i] ===
bands[start])`. Both forms look right and only one of them is, which is the
argument for measuring the thing rather than the code.

**What this does not fix, and the measurement that says when it starts to
matter.** `state.quantiles` comes from `/api/meta`, fetched once at boot, and
the ramp's *ranks* are read off it. Everything recorded after that lands past
the last quantile at rank 1.0 -- so under `everything recorded`, an hour of
new play is one flat newest colour at the head of the ramp rather than an
hour's worth of gradient, and no scroll fixes that either, only a page reload.
On `routes.db` an hour is about 5% of the 19.4 hours of capped play, so that
is 5% of the ramp flat. Refetching the quantiles would recompute them on the
recorder's own thread -- `time_quantiles()` is cached against a version that
every stored sample invalidates -- so it is not free, and it is a different
(smaller) complaint from the one that was made. Left alone deliberately.

**A jump's end inside a dungeon knew exactly where it was, and the number was
thrown away.** Reported: "I took a portal that took me into a cave, then the
teleport marker was placed at the entrance, instead of the position inside.
When you're outside the cave the marker can display the general cave entrance,
but when you hover over it, it should show the exact position" -- and, in the
same breath, the same thing about legacy dungeons.

`warps()` has measured a jump with one end inside a dungeon against that
dungeon's pin since sending gates were first drawn, because the two ends are
in different coordinate spaces and the pin is the only thing on the map that
stands for the far one. That is right for the arc and right for the mark seen
from outside. What was wrong is that it was the *only* answer available: the
arrival's own local position is in the row the whole time, and `server.warps()`
sent `local` only when `inside` was true -- which means **both** ends in one
dungeon, a lift or a teleporter, not a gate. `warpsInside()` filtered on the
same flag, so a gate was left off the dungeon's own drawing entirely.

On `routes.db`: 111 jumps, 27 with both ends inside one place, and **20 with
exactly one** -- 10 arrivals and 10 departures. Drawn where they actually
happened they move **10 to 527 m off the pin, median 87**.

Three things had to change together, and they are the three layers this file
keeps warning about. `_warps()` records `to_inside` / `from_inside` *before*
the branch below overwrites `wx` with the pin, because after that there is
nothing left to tell a reading from an inference. `server.warps()` sends the
local position of whichever end has one, plus `from_map_id` so the departure
can be matched to a visit the way the arrival is matched by `map_id`.
`warpsInside()` returns the *ends* that belong to a visit rather than whole
jumps, and the dashed line between them is drawn only when both are here --
one end out on the surface has nothing in this frame to join it to.

The moment each end is tested against is its own. A jump *out* of a dungeon
carries the arrival's timestamp, which is after you left, so testing `w.ts`
throws away every departure -- all ten of them.

**And what stands in for a thing must stand down when the thing itself is
there.** A cave needs no rule: its drawing only goes up while you hover it,
and hovering dims the world's marks anyway. A legacy dungeon is drawn in the
open at all times, so both would be on screen at once, bright, a few hundred
metres apart -- 13 of the 20. `loadWarps()` drops an end whose map is in
`state.alwaysDrawn`, the set `drawWorldVisible()` builds of what is actually
drawn out there. Measured: 114 world clusters without the rule, 106 with, and
all 13 now on their castle at the true spot.

That set is load-bearing in both directions, and getting the second one right
took two goes. It is now cleared at the top of `drawWorldVisible()` rather
than filled in below, so turning the dungeons off cannot leave it naming seven
castles that have come off the map -- which is the trap `hiddenInside()` is
written around: a mark that stood down in favour of a drawing must come back
when the drawing goes. And `drawWorldVisible()` ends with `loadWarps()` beside
the `loadDeaths()` that was already there, because at boot the marks are
clustered before anything knows which castles are drawn.

**Unticking Caves and dungeons left every castle on the map.** Found while
measuring the above, and older than any of it. `reload()` called
`loadInteriors()` only `if (state.layers.interior)` -- but that function asks
the same question on its first line and answers it by clearing the map, so the
guard meant the one thing that takes the legacy dungeons off was never reached
on the reload that turns them off. Measured before: 39 pins gone, all 7
castles still drawn. After: 0 and 0, and the 13 stand-ins back at their pins.

**Moving a dungeon has to move everything that borrowed its position.**
Reported: "with the one I just moved manually, the teleport marker stayed put."
It did. A death inside a dungeon is handed that dungeon's position because
there is nowhere else to draw it, so is a respawn, and so is the end of any
jump that crossed into it -- but the drag called `loadInteriors()` alone, so
the pin and the path moved and the marks stayed. `placeAt()` had half of it
(deaths, not warps) and `unplaceDungeon()` had none. One `afterPlacing()` now,
called by all three. Reproduced on a copy of the database with the refetch
suppressed: the pin moves 354 units and the teleport end stays exactly where
it was; with it, they move together to the metre.

**Farum Azula in the corner is not a bug.** Also reported, in the same
message. m13_00 was reached by teleporter, so the first sample carries a load
screen and the sampler wrote no anchor; it has one visit, so there is no other
one to borrow from; and it is a legacy dungeon, so there is no exit on foot to
fall back to either. Every tier declines, and a place that declines goes to
the unplaced list and is drawn in the inset off a button in the corner. That
is the design working -- the same as the Roundtable Hold, and the same answer:
**Put on map**. Worth knowing that this is what it looks like from the outside,
because "it started to display it as a window in the corner and not on the
map" is a reasonable way to describe a feature you have not met before.

**A dungeon you placed by hand had no frame, so it was drawn once per run and
the run you were walking moved under you.** Reported live, from inside the
Chapel of Anticipation: "I just entered a portal that takes me back to the
starting area of the game, but it doesn't match up with where I am on the
map."

Every other tier draws a dungeon in one frame that belongs to the dungeon --
"where a dungeon is belongs to the dungeon, not to one visit". A hand
placement was the exception. `interiorTransform()` skipped the frame lookup
for that tier and fell through to centring the drawing on `d.bounds`, which
is *this run's* extent, so the frame was per visit and, worse, per moment:
the bounds grow with every step, and `refreshLiveInside()` refetches the open
run every five seconds.

Measured on m10_01 as it stood when it was reported. Three runs through it put
the same point inside the place **25, 108 and 126 m apart** on the map. And
the run in progress, reconstructed from the samples, centred on local
(20.4, -0.6) five seconds in and (-23.1, -90.4) by the end -- so the whole
Chapel, and the dot standing in it, **slid 100 m across Liurnia over the five
minutes the visit lasted**, in jumps of up to 66 m. After: 0, 0 and 0, and
handing the open run a path 500 m longer moves the frame 0 m.

The fix is to stop treating the tier as a different kind of drawing. A hand
placement is a *position*, so it belongs in a frame like every other position:
`learnDungeonFrame()` builds one for those maps out of the position given, and
`interiorTransform()` reads the frame for every tier. It runs after the door
loop -- which skips hand-placed maps anyway -- so a drag always overwrites
whatever the route had inferred, which is the whole point of the tier. Where
nothing at all is recorded inside, the frame is *deleted* rather than left
alone: a stale frame from before the drag would move the pin and leave the
path, which is the bug the tier was given priority to fix.

What centres it is the extent of **the runs that have finished**. How big a
place is is a fact about the place, and a run you are in the middle of has not
finished saying -- letting it decide is exactly what moved the drawing under
the player. The first time you ever go somewhere the run you are on is all
there is, and then it is used.

`the way in` shares the same `d.bounds` centring and therefore the same bug,
and is left alone: there is nothing in `routes.db` at that tier to measure
against, and unlike a hand placement its `xy` can differ between visits to one
map, so "one frame per dungeon" is not obviously the right statement for it.
Worth doing the moment a route produces one.

**And where that dungeon is remains the user's answer, not the tracker's.**
Both entries into the Chapel that day carry break code 3 on their first
sample, so the sampler withheld the anchor exactly as it should -- a portal is
not a door, and `anchor_wx` is null on both enter events. The position it is
drawn at is a hand placement made on 2026-09-06, 2,461 m from the portal used
to get there. Nothing here can improve on that, because the Chapel is not
anywhere in the Lands Between: it is the Roundtable Hold's case, and
**Take it off the map** is the answer if that is what is wanted -- `nowhere`
clears the position, which drops it out of `/api/interiors`'s placed list, off
the terrain and into the corner. This file already said the Chapel was left
unplaced on purpose; it was placed by hand later, which is allowed and is why
the tier exists.

One thing that falls out of the audit: `world_visible_areas` holds area 10,
which is right for m10_00 -- Stormveil, 7,347 samples -- and wrong for m10_01,
which is nowhere. That is the m18 case again (a legacy area holding something
that is not a castle you can see), one level further down: at map granularity
rather than area. Marking a map `nowhere` already takes it off the terrain, so
nothing is broken today, and a per-map visibility list is not worth adding
until something is.

**A frame is a pin, and a pin does not say which way a thing is facing.**
Reported live, from inside Raya Lucaria: "I just entered a legacy dungeon, but
the path doesn't match up with where I am on the map."

m14_00 has two doors. The 8 September visit walked in at local (60.1, -90.8)
with the surface at (8775.8, 11640.6); the 10 September one at local
(-0.6, 2.3) with the surface at (8848.3, 11716.0). That is 111.1 m apart
inside and 104.5 m apart on the map -- 6.3% disagreement, so the two lines are
a rigid turn apart and `frameTurn()` can measure it: **-77 degrees**.

Which it now has. The point is what the drawing did *before* the second door
existed. A frame is one point inside tied to one point on the map, so with one
door there is no angle to be had and the shape is laid down unturned: right at
the door it is pinned to, and further out the further in you go. Measured by
rebuilding the frame from only the visits that predate the walk in, then
transforming the new visit's first step: **133 m from where the player
actually was**. Re-learn with both doors and it is **5 m**.

So Raya Lucaria had been drawn at 77 degrees to the truth for two days, and
the moment a second door was walked through it quietly straightened out. None
of that is a bug in the machinery -- `refreshLiveInside()` calls
`loadInteriors()` every five seconds while you are in a world-visible dungeon,
which re-learns the frame, so it corrects itself. The bug is that the drawing
said nothing about which of the two it was, and the caption actively claimed
the wrong one: "the dungeon's own axes set the orientation", which is the one
thing a single-door frame cannot know.

The caption now says which. Turned: "turned to line up with the two ways in".
Pinned: "pinned at the way in -- which way it faces is not known until you
come in by a second door." Both the hover caption and the playback one, since
they were making the same claim. Verified on all three cases: Raya Lucaria
(-77 degrees) and Stormveil (0.2) read as turned, a one-door cave reads as
pinned.

Nothing is dashed for it. Most dungeons have one door, most are caves under
the terrain where the far end being 60 m out cannot be seen against anything,
and dashing almost every path to say so would cost more than it tells. What
makes a legacy dungeon different is that it is drawn in the open over ground
you can compare it against -- and there the caption is what you read.

An audit fell out of the same pass. Over every visit whose `placed` is
`entrance` -- the only case where "the first step should be drawn on the
anchor" is a true statement, because the anchor *is* the door you walked in
through -- four dungeons are more than 10 m out: m30_11 at 193 m and m31_15 at
29, both two-door places whose lengths disagree too much for a turn (178 m
against 15 inside, which is not orientation); m31_06 at 132 m and m31_10 at 33
with one door each. Everything else lands within 10 m of the door it was
walked in through.

**And the three tests that ask "is this the same door?" now agree.** The door
vote and the second-mouth pins both use `metresApart() < SAME_DOOR_M`, and the
pair `frameTurn()` measures from used a bare `Math.hypot` over map pixels
against 10. That agrees with 15 m only because this map happens to be about a
pixel to the metre; tile a map at another scale and the three would disagree
about what one doorway is. Harmless today, wrong in the way that waits.

**A flash is a length of wall clock, and what it costs is route.** Reported:
"if you use a fast playback speed, the screen will stay darkened as if you were
in a cave, even when you're not; it will eventually brighten again if you
haven't been in a cave for a little."

The first diagnosis was wrong and worth writing down. `play.hold` is set by
every interior event, so at speed the obvious story is that the events renew it
faster than it expires and the drawing is held for ever. A guard against that
-- measure how long the drawing has been showing somewhere the clock is not,
from the moment it left, renewed by nothing -- was written, and measured as
**no difference at all**: 55% dimmed against 56, longest 3,150 ms against
3,100. It came out again.

What is actually wrong is that the flash is a second and a half *of wall
clock*, whatever the speed. At five minutes a second that is seven minutes of
route and reads as intended. At an hour a second it is an hour and a half of
route, on a playback that is fifteen seconds long from end to end -- so
holding the drawing for one flash holds it a tenth of the whole route behind
the clock, and the dot is out in the open while the world is still dark for
the cave it left. The "eventually" in the report is the last hold expiring
once the events happen to stop.

So the hold is scaled below the speed the flash lengths were judged at:
`span * Math.min(1, PLAY_HOLD_SPEED / play.speed)`, with `PLAY_HOLD_SPEED` at
300, which is the default. At or under that nothing changes; at an hour a
second the hold is 125 ms.

Measured over the densest stretch of `routes.db` -- 59% to 70%, where 44 of
the 145 interior events fall -- ten seconds at an hour a second:

|                                   | before | after |
| --------------------------------- | ------ | ----- |
| dimmed                            | 53%    | 23%   |
| longest unbroken dim              | 3,150 ms | 1,650 ms |
| **frames showing a place the clock had left** | **39%** | **3%** |

The last row is the one that matters, and it is the one to measure next time:
"dimmed" is partly the truth, since the route really is in caves for much of
that stretch. What was wrong was the drawing being somewhere the playhead was
not, and that is 39% to 3%.

**Three smaller ones from the same report.**

Nothing here should scroll the page -- the map fills the window and the panel
scrolls inside itself -- and a trim grip is 18 px wide on a centre that stops
7 px in from the end of the track, so at the far end it reaches 2 px past and
put a horizontal scrollbar across the whole window. `overflow: hidden` on
`html, body`, which is right for a full-viewport app regardless, and on
`#play-trim`, which is where the two pixels actually are. Verified both ways:
no scrollbar, `scrollWidth` back to `clientWidth`, and the page cannot be
scrolled by script either.

How long is left was right-aligned in its 210 px flank, so it sat against the
edge of the map with a hand's width of nothing between it and the button it
belongs to. The width is the counterweight that keeps play centred; where the
words sit inside it is a separate question, and next to the thing they are
about is the answer. 120 px of gap down to 22.

And the deaths switch is one skull, in the death's own red while they are on
the track and pale while they are not -- the same mark the toll wears, so it
needs no label. The `✕` it used to carry had been rendering as `¹5` since it
went in: the escape was written `'¹5'` in a patch script, the heredoc ate
one backslash, and Python read `¹` as an octal escape. Third time that
particular trap has cost something here.

**Backwards, and the deaths on the bar.** Two more controls on the timeline:
one that runs the route the other way, and one that puts the red lines away.

Reverse is a *direction the loop reads*, not a second drawing routine, because
of the thing `playSeek()` has said since the playback was written: the picture
cannot be unwound, only replayed. Forwards, a step costs almost nothing -- the
drawing only grows and one run is redrawn. Backwards, every step is a full
seek.

Measured before building it, seeking cold across the whole route: 69 ms median
and 117 ms at the far end, which would be nine frames a second. Measured after,
actually reversing: 13 ms median and 25 ms worst, because the steps are small
and adjacent and almost nothing changes between them -- so about forty redraws
a second even from 98% of the way in. The cold number was the wrong measurement
for the question; the two are three seeks apart and five times different.

Left to itself a seek a frame fills the thread and the controls stop
answering, so a reverse tick is skipped until as long again has passed as the
last rebuild took: half the thread to the playback and half to everything
else. What that costs is frames, not speed, because the clock is stepped by
the wall time either way. Measured from 98%: 284x against the 300x asked for,
and the missing 5% is the teleport easing, which is deliberate.

It composes with the two things already on the bar. The end it stops at is
whichever end it is running towards, `playResume()` starts from the other one,
and repeat wraps to the far grip rather than to zero -- so reverse, repeat and
a trimmed window together do the obvious thing. Measured on a 4% window at an
hour a second: twenty laps backwards in ten seconds, never leaving the window.

The deaths switch is `#play-ticks` hidden and nothing else. They earn their
place -- knowing where the deaths are before you reach them is most of what
the bar is for -- but a hundred and forty of them over one evening is a wall,
and then the shape of the route underneath is what you want back.

**And the centring arithmetic, one more time.** The transport is symmetric
again: reverse to the left of the toggle mirrors repeat to its right, five
buttons, 28 + 6 + 28 + 6 + 38 + 6 + 28 + 6 + 28 = 174. So the two flanks are
equal once more at 210 -- 210 + 12 + 174 + 12 + 210 = 618, half of it 309, and
`#play-clock`'s max-width follows it to `calc(50% - 324px)`. Measured on a
1400 px window: the toggle's centre and the track's are both 874.

The check for this was rewritten *again* on the way past, and this time into
the form that should survive: it computes where the toggle's centre falls out
of the CSS widths and asserts it lands on half the mid group. Written as "the
two flanks are equal" it was true, then false, then true again while the
property it was standing in for never changed.

Worth knowing when writing the negative test for a check like these: sabotage
the string the check actually looks for. Removing a standalone
`box.hidden = !play.ticksOn;` line did nothing, because in the file it shares
a line with its guard -- so the check went on passing and the negative test
proved nothing until it was aimed at the real text.

**Repeat, and the arithmetic that keeps the play button on the middle of the
map.** Asked for: a button that runs the playback again when it reaches the
end. `play.loop`, and the whole of the behaviour is one line in the tick --
`playSeek(play.from)` instead of `playPause()` -- because seeking is a rebuild
rather than a rewind and it leaves the timer alone. It goes round from the
*start grip*, not from zero, so repeat and the trim mean what you would expect
together.

A mode rather than an action, so it stays lit while it is on: what the button
shows is the way the playback is set, not what pressing it would do. Remembered
through `savePref`, because it is a way of watching rather than a thing you do
once, and `r` toggles it the way `d` marks a death.

The part that needed care is the layout. The transport was symmetric -- back,
play, forward -- so the play toggle sat on the transport's own centre, and the
timeline's centring was two equal 210 px flanks either side of it. A fourth
button breaks that: at 28 px plus a 6 px gap the transport goes 106 to 140 and
the toggle ends up 17 px left of its middle. So the right flank is narrower by
twice that: 210 + 12 + 140 + 12 + **176** = 550, and the toggle lands on 275,
which is the middle of the map. Measured on a 1400 px window: the toggle's
centre and the track's are both 874, which is where they have always been.

`selftest.py` used to check this by comparing the two flanks for equality,
which is now the wrong statement. It computes the toggle's centre out of the
CSS widths instead and asserts it falls on half the mid group -- the property
that actually matters, and one that survives the next button. Put the flank
back to 210 and it says "toggle centre 275.0 of 584 wide, middle is 292.0".

Measured on `routes.db`: a 3% window at an hour a second went round five times
in twelve seconds without ever stopping; with repeat off the same window
stopped at the far grip and said Finished; and the setting came back lit after
a reload.

One thing to copy: two of the new checks index into `app.js` to look at what
follows a line, and `str.index` *raises* when the line is gone -- which aborts
the whole run instead of reporting one red check. That trap is already written
down here for the ordering checks; it applies to every check that slices. Ask
`in` first and slice second.

**Two grips on the timeline, and the playback is what lies between them.**
Asked for: "add two drag bars on both sides of the timeline, and moving these
decides what part is the start and end for the playback."

`play.from` and `play.until` beside `play.at` and `play.to`, and the rule is
that every question the playback answers is about the window rather than about
the route: the loop stops at `until` and starting again goes back to `from`,
`playSeek()` clamps into the pair rather than into `[0, to]`, the percentage in
the clock line is how far through the *window* you are, and `playTimeLeft()`
counts the brakes only as far as `until`. Trim off the first three weeks and
what you want to know is how much of what you asked for is left.

The one thing deliberately measured against the whole route is the thumb. The
track stays a picture of everything recorded with a window drawn on it -- the
part outside is shaded rather than removed, so it reads as something you chose
to leave out rather than as something that is not there -- and a thumb placed
against the window would sit somewhere the grips disagree with. So the scrub
position and the death ticks are absolute and the counting is relative. The
fill needed a second gradient stop for the same reason (`--start`), and
Firefox's `::-moz-range-progress` had to go with it: it fills from zero, which
would paint the head the first grip has cut off as though it had been played.

Everything drawn on that track now goes through one `playTrackAt()`, because
the thumb travels between its own half-widths and anything placed without that
inset lines up with nothing. The ticks and the hover mark were each doing the
same `calc()` by hand.

Two constraints on the pair, and both are about pixels rather than about time.
They may not cross, and they may not come closer than the width of a thumb: two
grips on one pixel cannot be told apart, and a window narrower than the thumb
is one you cannot put the playhead inside. So the clamp is computed from the
track's own width at the moment of the drag rather than from a constant in
milliseconds, which would mean something different on a route of two hours and
one of two hundred. Dragging a grip past the playhead moves the playhead, since
leaving it outside the thing it is the playhead of says nothing true.

**And the grip is a pin, not a tab.** Asked for: "make the grip taller, with a
circle on the top that you can grab, so it's easier to see." The first one was
a 9x20 tab sitting on the bar, which is hard to pick out among a hundred and
forty death ticks and gives the eye nothing to say *where the cut lands*.

A 16 px knob above the bar and a 3 px stem running down through it and a little
past -- so the thing you take hold of and the line it draws are the same
object. The button itself is 18 px wide, draws nothing, and is only the hit
area: grabbing it should be a gesture rather than an aiming exercise.

That needed room the bar did not have. The 8 px track, the 14 px death ticks
and the thumb all live in the middle of a 40 px box, so a knob "on top" had
nowhere to be that was not on top of one of them. `.play-track` is 48 px now
with the eight added at the top, and the pieces measure out as knob 0-16,
ticks 17-31, the bar itself 20-28, stem 13-44 -- one pixel of clearance between
the knob and everything else. Everything that measures itself against the
timeline's height moved with it: the caption, the corner button and the inset.

A drag does not stay inside a nine-pixel handle. `setPointerCapture` is the
browser's way of saying so, and the first version asked `hasPointerCapture` in
the move handler -- which makes the drag *depend* on it, and anything that
cannot grant capture gets a grip that can be pressed and not moved. It is a
local flag now, with the moves coming off the window; capture is still taken,
as the optimisation it is.

A fresh playback plays the whole route: carrying a trim over would be a setting
you cannot see until you press play. A *rebuild* keeps it, clamped into
whatever the new axis turned out to be, because marking a death rebuilds the
axis and can change its length a little.

Measured on `routes.db`: grips dragged to 25% and 60% pull the playhead from 0
to 25%; seeking to either end of the route lands on 25% and 60%; dragging one
grip past the other stops it a thumb short; flicking both to the ends restores
the whole route and the pair goes quiet again; a window of 40% to 44% played at
an hour a second stops at exactly 44% and says Finished with the 65 deaths that
are inside it; and pressing play from there goes back to 40%.

**The map forgot where you were looking.** Asked for: "make it remember your
zoom level and position when stopping the tracker, so when you start it again,
you go right back to where you left off."

A centre and a zoom rather than the bounds, because a window that is not the
size it was last time should keep the middle and the scale it had rather than
re-fit itself to a rectangle and land at some third zoom nobody chose. Three
decimals, which is finer than a pixel at any zoom this map has. It goes
through `savePref()` like everything else the panel remembers.

Two things it has to refuse.

The playback drives the map from one end of the route to the other, and where
it happened to stop is not where you were looking -- the view from before you
pressed play is. So `saveView()` returns early while `play.on`, and the view
from before survives the whole playback. Verified: parked at (-116.25, 60.5)
zoom 7, played, moved the map to the far side of the world mid-playback, and
the stored value never changed; the first move after leaving playback was
remembered again.

And a remembered view is only worth restoring while it still means something.
A map image of a different size, or a zoom ladder that has changed -- both of
which have happened here -- would put you off the edge of the world at a zoom
the map will not hold. It is checked against `map.getMinZoom()`/`getMaxZoom()`
and against the same `panLimits` a drag is, and simply ignored when it fails,
which leaves the old behaviour of fitting the route. Verified against garbage,
a centre 5,000 units off the map, a zoom of 42, an empty string and a truncated
one: all five ignored, the good one restored to the metre.

Both `moveend` and `zoomend`, because a zoom with no drag in it fires both and
a drag fires only `moveend`; writing the same two numbers twice costs nothing
and missing one of the two gestures is the whole feature. Nothing is stored
until you actually move: the fit that boot does fires `moveend` before the
listener is attached, so a map you open and never touch goes on fitting itself
to the route as the route grows, which is the better of the two behaviours to
have by default.

Worth knowing: this is `localStorage`, so it belongs to the origin. Everything
the panel remembers has always worked that way, and the port is part of an
origin -- run the viewer on `--port 8907` and it has its own settings,
separate from the 8731 the launchers use.

**A compass contained in a square box is not in the corner.** Reported as
"the compass isn't aligned correctly, it's too much to the left". It was at
`right: 46px` against `top: 14px`, which is already lopsided -- and the
supplied `compass.png` is 219x256, taller than it is wide, so `object-fit:
contain` letterboxed it inside the 148 px square and put another 11 px of
nothing between the drawing and the edge. Measured: 57 px of gap on the right
against 14 on the top.

`right: 14px` and `object-position: top right`. Contained but not centred: an
image that is not square leaves its spare room somewhere, and the corner is
the one place it must not be. The drawn fallback is square and fills the box,
so nothing changes for it. Measured after, on a 1052 px map: 14 and 14, and
clear of the brightness slider, which is vertically centred at y 330-570.

**A ladder that runs widest-first and a slider that runs left-to-right
disagree about which way is "more".** `AGE_DEPTHS[0]` is everything recorded
and `[9]` is the last fifteen minutes, and the slider was wired straight to
that index -- so dragging right *narrowed* the window. Reported as exactly
that: "if the slider is all the way to the right, then it should consider
everything, and if it's to the left, it should cover less, but now it's
inverted."

The control is reversed, not the ladder and not the value: `read` is
`AGE_DEPTHS.length - 1 - +el.value` and the restore does the same in reverse.
`state.ageDepth` therefore keeps the meaning it always had, so `route.ageDepth`
saved before this still names the same window -- which is the lesson from
`route.bright`, where reinterpreting a stored key would have turned the map
off on first load. The one thing that had to change with it is what gets
saved: the loop stored `el.value`, and for a reversed control that is the
thumb rather than the window, so it stores `state[key]` now. Equivalent for
every other control in the map, since there `state[key]` *is* `el.value`.

Measured: right = "everything recorded", left = "the last fifteen minutes",
middle = "the last twelve hours", and a stored 4 comes back as a thumb at 5,
which is the same window it was.

**The folder someone unzips.** Reported: "clean up the folder structure, cause
it's a bit messy for someone new wanting to install the mod. It should only
really show the files: open map.bat, and record route.bat."

The root now holds those two and `CLAUDE.md`, with `docs/`, `config/`,
`tools/`, `tracker/`, `viewer/`, `assets/` and `data/` beside them.
`README.md`, `SETUP.md`, `MANUAL.md` and `NOTICE.md` moved into `docs/`;
`m1-underground.png` (34 MB) and the stray `Untitled-1 copy.png` -- which is
709x799 and looks like the compass artwork at full size -- into `assets/`;
`requirements.txt` into `tools/`, beside the scripts that are the other thing
you only run occasionally.

CLAUDE.md stays at the root because that is where Claude Code looks for it.
Moving it would quietly stop it being loaded, which costs more than the line
it takes up.

What moving files actually breaks is the paths inside them, so `selftest.py`
now checks the shape rather than trusting it: what is at the root, that each
doc is where the front page says it is, that every `.md` link between them
resolves, and that the requirements path the launcher types and the one
`SETUP.md` prints are both real. Verified by parsing the config before and
after the tidy and diffing the result: one key different, `[viewer] title`,
which nothing reads.

GitHub renders a `README.md` at the root of a repository and nothing else, so
one went back -- as a stub rather than as the front page: what this is, the two
files to press, and links into `docs/`. Forty lines, no version numbers, no
step-by-step, nothing that can drift away from the real documents. The full
front page stays at `docs/README.md`, and the link check above covers the stub
too, since a dead link in the one thing a newcomer reads is the place it costs
somebody an evening.

**A config file is read top to bottom by whoever opens it.** Reported: "there
is a lot of information there that most people don't need to know at all, and
also move the values that someone would actually want to change to the top."

Three parts now, named in the header: **settings** you might change,
**your map** (the projection and the tile pyramid, which only matter if you
build your own), and **game internals** -- area numbers and the memory
signatures. The tiering has to be by table rather than by key, because a TOML
table can only be written once, so `[recording]` and `[game]` come first,
`[projection]` and `[viewer]` next, `[maps]` and `[memory]` last.

Inside `[recording]` the everyday three come first and the three measured
thresholds after, under a line saying they are measurements and not
preferences. The long stories behind them live here and in `docs/MANUAL.md`;
what is left in the config is the one sentence that says what moving the
number would do. 242 lines to 237, with most of the difference being
explanation that had a better home and a stray three-line paste of
`calibrate.py` output that had been sitting under `[projection]`.

`selftest.py` asserts the table order and that `interval_s` comes before
`max_speed_mps` comes before the pointer chains -- scrambling the file fails
both.

**The Stranded Graveyard is not a castle you can see.** Reported from the
field: "the legacy dungeon m18_00_00_00 appears in the overworld, but it's
actually underground." It is, and the recording says so plainly -- session 27
walks m10_01 (the Chapel of Anticipation) from 17:25, m18_00 from 17:27, and
comes out onto m60_42_36 in Limgrave at 17:39. That is the cave you wake up in
after the Grafted Scion. Area 18 was in `world_visible_areas`, so its path was
drawn permanently over the terrain, which is the "caves look like overworld
wandering" bug this whole tool exists to fix, reached from the other side. It
is a marker and a hover overlay now, like every other hole in the ground.

Area 19 is the other one on that list I cannot vouch for and there is nothing
in `routes.db` to check it against, so it stays until somebody walks into it.

**The run being walked was drawn and then taken away again.** Reported as
three things that are one: "when I scrub through I can see my position change,
but the path suddenly pops in at 6:50:10 when I die, and then it stops making
the path until 6:53:35", and "it appears every cave has the scrub issue where
the path isn't there when you start scrubbing".

`playDrawTo()` walks the runs and draws the one the clock is inside, then
checks where the clock says it is and calls `playEnter()`, which clears the
group an interior head lives in and puts back the runs that are *finished*.
The one being walked is not finished, so it went on the map and came straight
off it. While playing that costs a single frame and nobody sees it. A seek
calls `playDrawTo` once, so what is left on screen is whatever `playEnter()`
redrew: nothing at all early in a visit, and the completed runs only after
that -- which is exactly "the path pops in when I die" (the death ends a run)
"and then stops again" (the next run is the one being walked).

The head is held now and drawn after the place check, so it goes on top of a
drawing that is already in the right place. Measured on the cave that was
reported, seeking to four moments inside one visit: 4 points on screen, then
26, 45, 66. Before, the first three of those were nothing.

**A seek is a reset, so every death in the route is a death that just
happened.** `playToll()` fires the badge on a rise, and `playReset()` sets the
count to 0 -- so a scrub replays every death up to the target and the count
goes 0 to N on every frame of the drag, which is a rise every time. The note
beside it claimed this was handled; it was not, and the report was "when I
scrub through, the death counter keeps playing the effect as if you died".

The number still lands, because that is a fact about where you are. The
announcement is the part that belongs to the moment, so it is gated on
`animate` like the other things that only happen while the clock is running.
Measured: 31 seeks across the whole route, 0 flashes, and the count arrives at
143 which is what the database holds.

**The warp list is not the teleport layer's.** `loadWarps()` filtered by plane
on the way in, and `state.warpList` is read by the dungeon drawings and by the
playback -- which walks both planes and switches the map as it goes. So every
jump made underground was missing from the playback entirely: two on
`routes.db`, at 13:54 and 14:07 on 6 September, reported as teleports with no
markers. The deaths have always kept the whole list and filtered at the
drawing; the warps do now too. Measured after: 94 in the list against 87, 7 of
them underground against 0, and both reported jumps drawn with marks at each
end.

**Counting samples is fair to the clock and unfair to the cadence.** The age
ramp measures time in samples because the calendar is 75% nothing -- and that
fix has its own skew, because the sampling rate is not a constant either. The
imported routes were recorded every five seconds and live capture runs four
times a second, so an hour of imported play is twenty times fewer rows than an
hour of live play. Measured on `routes.db`: q0 was 3 August 17:39, q1 was
5 August 03:32 and q2 was 4 September 22:23 -- two whole evenings of real play
in 2 of the 100 buckets, and everything since in the other 98.

In the playback that reads exactly as it was reported: "there is a lot of
yellow at 7:01:17 PM, but the next point at 3:04:19 AM, then every path is
blue." The playback colours each stretch by `run.rank / play.rankNow`, so when
the denominator is 0.0004 and then 0.0099 -- a twenty-five-fold step -- every
stretch behind the playhead drops to the oldest colour in one move.

`time_quantiles()` now splits the recording into equal spans of *play*, with
each gap capped the way the statistics and the playback already cap theirs.
Two evenings a month apart are two evenings whatever cadence they were
recorded at, and the month between them still passes in nothing. Measured
after: 3 August 19:01 is rank 0.030 and 5 August 03:04 is 0.040, against
0.0004 and 0.0099 before. The fixture is two sessions of sixty seconds each,
one at five seconds a sample and one at four a second -- 13 rows against 241 --
and the slow one takes 5% of the ramp counted in rows and 50% counted in play.

**A dungeon can have more than one mouth, and only one of them had a pin.**
The marker has been one per map ID since "grouping by position put a second
marker on the map every time you went back in from a slightly different spot",
which is right about several readings of one doorway and wrong about a cave
you walk into at one end and out of the other: there was nothing on the map
where you came out. Four dungeons in `routes.db` have two mouths -- Stormveil's
543 m apart, the Stranded Graveyard's 147, m30_11's 178, and the cave that was
reported at 94 -- clustered at the same `SAME_DOOR_M` the door vote uses, so
several readings of one doorway still make one.

The other mouths get a pin of their own: smaller, dashed, no badges, not
draggable. The visits and the deaths belong to the place rather than to a
door, and saying them twice a few hundred metres apart would be saying them
twice; dragging sets where the dungeon *is*, which is the main pin's to carry.
Hovering either one shows the whole dungeon, because it is one place.

What this does not fix is the *path* at the far mouth. The drawing is pinned
at one door and scaled in metres, and `frameTurn()` refuses to rotate a
dungeon whose two doors disagree about how far apart they are -- m31_05's are
94 m apart on the map and the interior is not laid out to match. So walking
out of the far end still draws the last step inside somewhere other than where
the surface picks you up, and now there is at least a pin standing at the
place you actually came out.

**Room to zoom.** `maxZoom` was `nativeZoom + 2`; it is `+ 4`. Past the
pyramid the terrain is upscaled and soft, but what you are looking at that
close is the path, which is drawn from the route and stays sharp at any zoom.

**A jump knows where it is going before the map is sent there.** Reported:
"a teleport between a distance that starts to move the map, makes things start
to lag a little." A whole screen of tiles arriving at once is what that is, and
the playback already has the one thing needed to pay for it early -- the line
takes `PLAY_TRAVEL_MS` to cross to the arrival and the clock is eased for at
least as long again. `warmAhead()` asks for the tiles around the destination at
the start of that, so they are decoded before the follow-pan gets there. Same
machinery as `warmNeighbours()`, without the idle timer, because the moment
before a jump is exactly when there is no idle.

**One reported thing is left unfixed, and the measurement says why.** "The
first death appears at 5:27:06 PM, but the respawn appears at 5:27:21 PM, even
though that position is just wrong, cause you actually respawn at 5:27:26 PM."
That is right: the death is in the Chapel of Anticipation, the reading at
17:27:21 is still in the Chapel 52 m away, and 17:27:26 is the Stranded
Graveyard, which is where the game put you.

`respawn_after()` takes the first broken sample after the death, and there is
no rule in the recording that separates these two. Measured over all 142
deaths that have a grace, 3 have another break immediately after the one
chosen -- and of those three, this one should move to the second, the 09-06
19:09:34 death must *not* (its grace is inside a Divine Tower and the second
break 14.5 s later is warping out afterwards, a separate event), and the third
is unknowable from here. The only thing separating the case that should move
from the case that must not is 5.0 s against 14.5 s, which is one point either
side of a threshold and not a band.

That is the wall this project keeps hitting, and the answer everywhere else is
to ask rather than guess -- `I died here` exists for the same reason. The
correction that belongs here is a control on the respawn mark saying the grace
is the next point, which needs somewhere to record it: the break the rule reads
was not ours to move, so it wants a column on the event rather than a flag in
the samples. Not built.

**A list of panes kept away from the panes themselves goes stale, and a
group identity is not a kind of drawing.** Reported from the field: "when
hovering over caves, there are certain things that aren't dimmed, that are
outside the cave, like deaths, respawns, and certain teleport points."
Measured on `routes.db` while hovering one: `respawns` at full strength with
35 marks on it, and `insideMarks` at full strength with 53 -- 86 marks
standing bright over a world dimmed to 18%.

Two separate causes with the same shape. `dimBackground()` names the panes it
dims, and `respawns` was created after that list was written and never added
to it. And `drawInteriorInto()` decided which pane a dungeon's own marks go in
by asking whether the group it was handed *is* `placedGroup` -- true when it
was written, and false from the moment each legacy dungeon was given a group
of its own inside `placedGroup`, so all four castles' deaths, respawns and
teleports went to `insideMarks`, which dimming deliberately does not reach.
That is the pane for the dungeon you are hovering, and its marks must stay
bright; a castle drawn on the map for good is the world's and goes down with
it. The caller knows which it is making, so it says: `{ permanent: true }`.
Measured after: respawns and deaths both at 0.18 over 35 and 111 marks, and
`insideMarks` bright with 4 -- the hovered cave's own.

**"Time recorded" and "how long you were going somewhere" are different
questions.** The first is the gaps between stored samples, capped at ten
seconds; the second has to leave out standing still, and standing still is
invisible to a clock. The recording does hold the answer, though, in the
place the gate rule already lives: nothing is stored until you have moved
`min_move_m`, so a gap always *ends* in a step. What a gap cannot say by
itself is whether you walked through the whole of it or stood in a menu and
took one pace at the end -- and the ground covered says exactly that. Each gap
is credited with the time its distance takes at walking pace, and never more
than the gap it happened in. Thirty seconds ending in a metre and a half is
the half-second it took; a quarter-second stride counts in full. A break
counts as nothing: a load screen is time in front of the game and it is not
time spent moving.

The one number in it is the walking speed, and the data gives it. Over the
68,206 steps in `routes.db` short enough that nothing can have happened
between them -- a gap no longer than the session's own median -- the implied
speed runs p10 3.96 m/s, median 7.89 and p99 11.76. `WALK_MPS` is 4.0, taken
from the bottom of that rather than the middle, because the number decides
what counts as movement and being generous is the right way to be wrong: at
4 m/s nine of every ten steps you were certainly walking are credited with
the whole of their gap. It carries the same ten-second cap as the total above,
so active playtime is always a part of the time recorded and never more.
Measured on `routes.db`: 19.41 h tracked, 13.01 h moving, which is 67%.

**A transit is not the row before its arrival, and marking one as a death put
both marks a minute late.** Reported from the field with the times attached:
"3:17:17 AM is supposed to be where i died, and 3:17:32 AM is where i got up,
but instead it says i died 3:17:59, and got up 3:18:02."

`mark_death()` puts the death at the step the jump left from, and found that
step as the row before the arrival by id. For a jump found between two
adjacent samples that is exactly right, and those were the only jumps there
were when it was written. A **transit** is not one: it is a whole dungeon
stay reported as a single jump, for the case where no adjacent pair shows one
-- so the row before its arrival is the last step *inside* the dungeon,
minutes after you went down. On 5 August the stay was m32_08, entered at
03:17:32 and left at 03:18:02, and the death landed on 03:17:59.

The departure has always been in the payload as `from_ts`; it simply was not
passed. It is now, and the grace follows from it rather than being assumed to
be the arrival: **the grace is the next sample stored after the death**, which
for an adjacent jump is the arrival, exactly as before, and for a transit is
the first reading inside the dungeon -- which is where the game actually put
you. Three layers and the middle one is written by hand, so `server.warps()`
had to be given the field as well; `selftest.py` asserts all three, the way it
does for `plane`.

One death in `routes.db` was marked that way -- found by asking which
hand-marked deaths sit on the *last* sample of a stay whose surface ends are
50 m or more apart, which is one -- and it now reads 03:17:17 with the grace
at 03:17:32, fifteen seconds later, which is what was reported. The other four
deaths marked inside a dungeon were marked from jumps inside it, where the
adjacent pair is the jump, and none of them moved.

**A countdown that ignores the animations is wrong by a quarter and never
reaches zero.** The time left was the route left over the speed, which is only
the half of it the clock controls: the playback also eases off at every
teleport, running at `PLAY_BRAKE_RATE` for the length of each. Measured
mid-playback on `routes.db` at five minutes a second, the clock alone said
179 s where the truth was 224 s.

All of it is computable, because a brake's length comes from how far its jump
is on screen and nothing else. `playTimeLeft()` walks the events still to come,
merging brakes the way the tick merges them -- `play.brake` is a `Math.max`,
so two jumps arriving together are one brake -- and pushing each one further
out by the easing before it, which is why it is a forward pass and not a sum.
Measured over 30 s of real playback, sampled every 3 s: the reading fell 29.7 s,
never more than half a second out at any sample.

It moved to the far side of the play button, where an element of exactly the
speed group's width had been sitting empty to keep the button on the middle of
the map -- so the reading now does that job as well as its own. Both flanks
are 110 + 8 + 92 = 210, and the play toggle's centre and the timeline's are
both 874 on a 1400 px window, which is where they were.

And the speed reads in minutes again. A multiplier is fewer characters and it
is the worse reading of the two: nothing else on the screen is in multiples of
real time, so x300 has to be converted before it means anything while "5 min a
second" is already the answer. The multiplier is on the hover, where the
sentence used to be.

**An earlier run through the same cave is the same walking.** `playEnter()`
drew the visits before this one at 0.4 opacity, to keep the run being walked
legible over them. What that reads as is the place fading out every time you
step back into it, which is the opposite of the thing the redraw exists for --
and there is nothing to rank one visit above another, since they are the same
cave and the same feet. They are drawn like the current one now, and the
caption no longer says "drawn faintly".

**Both drawing complaints were the same thing: a tile the browser has not got
yet.** "There is still a little flicker when zooming", and "sometimes when
panning you can start seeing the background slightly, since it is not loading
fast enough."

Measured, and the measurement took two goes to get right -- the first one
panned off the edge of the map image and reported a third of the screen blank
in every configuration, which is the edge of the world and not a tile that
failed to load. Sampling only where the image actually is:

- Zooming to a level for the first time in a page's life leaves 6 to 26 per
  cent of the screen with no tile for 45 to 61 ms. With `zoomAnimation` off
  the map arrives at the new level at once, so what fills that gap is the
  coarse layer stretched over everything, and then a snap to sharp. Three
  cold trials.
- A fast drag over ground the browser has not seen leaves a hole in about two
  frames of thirty, and the coarse layer covered every one of them -- so what
  is seen there is the 8x-upscaled blur, not the container.

So: nothing is wrong with the loading, the tiles are simply cold. Two answers,
both about paying for them earlier. The tiles are served with
`Cache-Control: public, max-age=86400` -- with no directive at all a browser
caches them heuristically *and revalidates*, so every page load started cold;
a day is long enough that a session never pays twice and short enough that
rebuilding the pyramid shows up the next time you sit down. And
`warmNeighbours()` fetches the level either side of the current one while
nothing is happening, 400 ms after the map settles, bounded at 80 images so an
odd window size cannot turn an idle moment into a flood. Measured cold, three
trials each: 6, 26 and 26 per cent blank without it, 0 per cent with it; and
on the live page after the change, four notches in and out of three levels,
0 per cent every time.

`keep_buffer` went 4 to 8 in the same pass. It measured as noise on its own --
two frames of thirty either way -- and it is a bounded ring of tiles fetched
when the map is idle, so it can only help.

Note that `web.static` had to give way to a handler to set a header on the
tiles, and a path out of a URL is not to be trusted: `tile()` resolves it and
refuses anything that is not under `viewer/tiles`.

**A map placed where it demonstrably is not was still drawn there.**
Reported from the field: "I entered the underground through a new entrance,
but the location doesn't match up -- it placed it all the way down on the map,
in the middle." That is m12_01, entered on 2026-09-09 at 18:08 down a lift
from m60_38_46. Area 12's origin is Siofra's, measured at the Siofra River
Well, and m12_01 does not share it: the way in says the origin is
(9415.36, 11975.58) and the area says (10878.94, 8321.17), which is 3,937 m
apart. Drawn at the area's, the whole river landed at pixel (4240, 8321) of a
9728x9216 map image -- bottom centre, exactly as reported -- when it belongs
at (2783, 4648), the pixel the lift is on.

`_check_origin()` knew. It has measured every walked crossing into an
underground map since the frame check went in, it got 3,937 m, and it printed
the `[maps.map_origin]` line to paste. Then `to_world()` placed the map at the
area origin anyway and the map drew it there. Saying so in a console nobody is
looking at while drawing it wrong is the worst of both: the whole point of
refusing to place an unmeasured river -- "kept, but off the map, rather than
drawn somewhere invented" -- applies at least as strongly to a river that has
been measured and found to be somewhere else. The sampler now remembers those
maps in `self.adrift` and stores them with no world position for the rest of
the session, which is the same treatment, reached by a number instead of by
an absence.

Remembered per session rather than decided per reading, because the
measurement exists only on the step across: every reading after it would
otherwise go on being drawn 4 km away. Wrong the new way costs a path kept
but not drawn until the recorder restarts, and it says so; wrong the old way
was the report above.

**And adding the origin could not reach the rows already written.**
`repair_underground.py` rewrote a row only when it had never been placed
(`wx is not None and r["wx"] is None`), which is right for the case it was
written for -- rivers recorded before `underground_areas` existed -- and does
nothing at all for a row placed at an origin since corrected. So the config
line alone would have fixed the map from the next session on and left the
1,843 samples already recorded sitting 3,937 m away, with the pass reporting
"0 row(s) change in total". The config is what says where an underground map
is, so the rule is now simply that a row whose world position is not the one
the config computes is out of date. Compared exactly rather than with a
tolerance: both sides are the same sum of the same two doubles, so an
unchanged origin cannot differ in a bit, and an origin that did change differs
by kilometres.

Measured on `routes.db`: 1,843 rows moved, the four other area-12 maps
untouched, and the served underground route now draws in two places -- the new
one spanning pixels x 2192-2787, y 4368-4751 with its east end on the lift,
and Siofra/Nokron where they were.

Worth knowing about the measurement itself, since a wrong one would be drawn
just as confidently: the descent is 283 m in 5.5 seconds with no sample stored
in between, and nothing was stored because the movement gate had nothing to
store -- the reads succeeded throughout (break code 2, not 3), so the player
moved less than `min_move_m` horizontally across the whole ride. The origin is
therefore good to about a metre and a half, which is what the Siofra one was
measured to. The other four area-12 crossings that session were all load
screens (code 3), which measure nothing and are skipped, and the one walked
crossing among them -- m12_07 into m12_02, 5.3 m in half a second -- agrees
with the frame those two have always shared.

**A cave with two mouths is walked through, and two rules called that a
teleport.** Reported from the field: "I entered a cave, went through it, and
where I came out it added a teleport marker from there to the entrance. Then I
went in again and after I teleported out, the cave's icon moved to the exit,
and the path moved with it." Both halves are m31_05 in session 65, walked in
at (9380, 10529) and out at (9324, 10454), 94 m apart.

**The mark.** `warps()` has one branch where a distance is measured between a
real position and an *inference*: a jump with a dungeon at one end is measured
against the marker the dungeon is drawn at, because there is nothing else to
measure against. So walking in one mouth and out of the other reports the two
mouths as a jump -- and which end it lands on depends on where the pin is,
which is why the mark appeared at the exit and later moved to the entrance.

A map change alone cannot say a door from a gate here. What can is the size of
the place: two mouths of one dungeon cannot be further apart than the dungeon
is, and the recording says how big it is. Measured over the nine such jumps in
`routes.db`, five are doors and every one is inside its own dungeon's extent
(93 m in a place 134 m across, 132 m in one 1,064 m across, 543 m in one
654 m), while the two real warps are far outside it -- 1,801 m into a tunnel
117 m across, 3,241 m into a cave 114 m across. A load screen needs none of
this and is trusted as before: it says you were put there.

That took 104 marks to 87. The eighteen it removed are all doors, and eleven
of them are the *same* door -- the one between Stormveil and its Divine Tower,
which had been drawing a teleport mark every time it was walked through.

The other half of it is `_transits()`, which reports a whole stay as one jump
when no single pair of samples shows one. With the door rule in place that is
where the same stay arrives instead, so it needed the same answer in its own
terms: nothing broke the line inside, so the path in there is one continuous
walk, *and* the walking recorded inside covers the distance between the mouths
-- 773 m walked against 94 m apart -- so walking is a complete explanation.
Both halves are needed: the Roundtable Hold's 1,911 m stay is unbroken inside
too, but only 237 m was ever walked in there, so its mark stays.

**The pin.** `_agree_on_one()` takes the medoid of three or more entrance
anchors and rewrites every one of them to it, which is right for several
readings of one doorway and wrong for two real mouths: A, B, B made B the
medoid and the door actually used was overwritten. The same size test settles
it -- an anchor further from the medoid than the dungeon is big cannot be a
mouth. Measured on every dungeon in `routes.db` with three or more entrances:
the second mouths are 87, 93 and 212 m in places 117, 134 and 225 m across,
and every anchor the vote was written for is far outside its own dungeon --
517 m and 775 m into a cave 232 m across, 1,799 m into a tunnel 117 m across,
3,241 m into one 114 m across.

The guard is for the `entrance` tier only. A `doorway` anchor is worked out
from where you left the last dungeon, and the case *that* vote exists for --
five ways into a Divine Tower agreeing to within 2 m and a sixth 83 m off,
because that route in went through a teleporter -- is the same 83 m as a real
second mouth. Nothing in a distance can separate those two, so the doorway
tier votes exactly as it did.

**And the pin goes to the mouth you used the place through.** `agreedDoor()`
counted visits, so a 283-second traversal was outvoted by two pokes of 25 and
34 seconds through the far mouth. It weights by time inside now, which asks
the better question and keeps every case counting got right: over the five
dungeons in `routes.db` whose visits disagree about the door, count and time
pick the same one in all five -- Sellia Crystal Tunnel is 4 visits and 1,914 s
at one mouth against 1 visit and 30 s at the other.

**The recorder used the surface's speed ceiling inside a dungeon, and a
teleporter walked straight under it.** Reported from the field: "I left a cave
through a teleporter, but it just drew a path between them instead of leaving
a mark." The path was not the exit -- walking out of m31_04 carries break
code 1 and the cave's mouth is 1 m from where the walk resumed. It was the
teleporter *inside* the cave, seven minutes earlier: local (112, -184) at
15:32:50 and local (15, -38) at 15:32:57, **175.3 m in 6.5 s**, break code 0,
drawn as a line through the rock.

26.9 m/s. The ceiling was `max_speed_mps`, 40, which is the surface's number
and right there: a quarter-second interval turns small position noise into
large implied speeds, and Torrent gallops at 15.9. Inside a dungeon none of
that applies, and `repair_jumps.py` has used 15 m/s for exactly this since it
was written -- the recorder was the half still using the wrong one. The two
rules disagreed about the same data for as long as both existed.

Measured over every unbroken interior step in `routes.db` -- 19,835 of them:
one at 26.9 m/s, which is this teleporter, one at 11.0, and **nothing at all
in between**. So `max_speed_inside_mps = 15.0` sits in an empty band a
factor of two wide on both sides, and it is the same number, from the same
measurement, the repair pass already uses.

Why the load-screen rule did not catch it either: a short teleport inside one
map has no `0xFFFFFFFF` and no map change, and the position chain kept
answering throughout -- so the blind-read counter never moved. The player
stood still through the animation, the movement gate dropped those readings,
and `self.prev` was left pointing at the last stored sample 6.5 s earlier.
The speed is the only signal there is, which is why the number matters.

Worth knowing when writing a fixture for this: the other interior fixtures
step four metres per 250 ms, which is 16 m/s -- under the old ceiling, over
the new one. A fixture that means to model walking has to walk at a speed
something could walk at, or it now models a teleport.

**A grace belongs to the session the death was in.** `respawn_after()`
looked for the first broken sample within three minutes and never asked which
session it was in -- so die, quit, and start the game again inside three
minutes, and yesterday's death claimed wherever you happened to load in as
the place it put you. Starting the game *is* load screens, so the first
stored sample of the new session carries break code 3, which is exactly what
the rule reads. Reproduced deliberately: a death at the end of one session
took a sample 60 s later and 1,273 m away in the next. Nothing in `routes.db`
does it -- 138 deaths, 136 graces, the same before and after -- which is why
it took a fixture to find rather than a query.

**Two things from the same audit were left alone on purpose.**

The server and the sampler share one SQLite connection, which means a read on
the server thread runs inside the sampler's open write transaction and
therefore *sees uncommitted samples*. That contradicts what this file says
elsewhere about `/api/interior` only knowing what has been committed -- and
the reality is the better of the two, because the live dungeon overlay gets
the newest samples instead of waiting up to `idle_flush_s` for them.
Splitting the viewer onto its own read-only connection would tidy the model
and make that overlay worse. Verified rather than assumed: an uncommitted
sample is visible to a read on the same connection.

And the two `0xFFFFFFFF` rows still in session 15, from before `is_no_map()`
existed, are 2 of 76,404 samples: `layer` is `unknown` so nothing draws them,
the dungeon counts filter on `interior` so they inflate nothing anybody
reads, and they move the quantiles by a fraction of a percent of one bucket.
A repair pass to delete two rows is more risk than the rows are.

**The tool's founding decision is one database forever, and nobody had
measured what that costs.** So: a copy of `routes.db` multiplied ten times --
764,040 samples, 480 sessions, 96 MB, which is about a year of the same play
-- and the viewer pointed at it. Boot spent **55 seconds in requests**,
`/api/stats` alone taking 7.0 s, `/api/warps` 3.9 and `/api/route` 2.8, with
13,438 layers on the map at the end of it. On today's 9.5 MB database every
one of those is a tenth of a second and invisible, which is exactly why it
had never been looked at.

Two thirds of it was work done twice.

**Boot asked for everything two or three times.** `reload()` fetches the
marks as well as the route, and `boot()` then fetched all three again; the
deaths a third time, because `drawWorldVisible()` reclusters them once the
legacy dungeons are known -- which is a real dependency and stays. On top of
that, wiring the panel calls `applyRange()` once to set the time-window
label, and that ended in `scheduleReload()`, so a full reload of everything
was queued 250 ms into boot. `applyRange(first)` now takes the same flag
`rememberToggle` uses, boot's reload asks for `{ marks: false }`, and the
handlers are wrapped rather than passed straight to `addEventListener`, which
would hand the event object in as `first` and make every drag of the slider
the silent one.

**And the expensive answers are kept until something writes.** `stats()`,
`time_quantiles()`, `interior_visits()` and `warps()` are pure functions of
the samples, read far more often than the database changes -- one boot wants
the visits four times over, and `warps()` asks for them twice by itself, once
through `_dungeon_places()` and once through the `map_planes()` added for the
plane fix.

The cache key is the thing worth copying elsewhere: it is *asked for*, not
remembered. A dirty flag set by every write works until somebody adds a write
and forgets, and a stale total read as a fresh one is worse than a slow one.
`_version()` returns the connection's own row counter plus the highest sample
and event ids -- two indexed MAXes, 0.006 ms on 764k samples -- so a write
through this connection and an append from another process both invalidate
it without anyone having to say so. What it does not catch is another process
*deleting* rows while this one holds an answer, which is a page reload away
and is said plainly in the docstring rather than left to be discovered.

Measured after, on the same 10x database: boot makes one route call, one
warps, one interiors and two deaths, **2.1 s of requests against 55**, and
the map settles at **2.6 s against 8.0**. `/api/stats` 7.0 s -> 2 ms,
`/api/warps` 12.3 s across two calls -> 11 ms, `/api/interiors` 6.2 s -> 15
ms. On `routes.db` itself the whole boot is 1.04 s and draws exactly what it
drew before: 667 route runs, 197 casings, 27 pins, 7 legacy dungeons, 81
deaths, 52 respawns, 104 teleports.

What is left at that size is `/api/route`: 1.45 s for 531,920 points
simplified to 55,210, and 1.8 MB of JSON. That one is real work rather than
repeated work -- the RDP pass and the transfer -- and it is the next thing to
look at if the database ever gets that big.

**A repair pass copies the database before it changes a row.** Every one of
them rewrites rows in place and none can be undone, and the recording is the
one thing here that cannot be made again -- a threshold wrong by a metre
rewrites the history the whole tool exists to keep. `Store.backup()` goes
through SQLite's own backup rather than copying the file, because the
database runs in WAL mode and a copy of `routes.db` alone can be missing
whatever is still in `routes.db-wal`, which is exactly the recent play you
would most want back. `backup_once()` makes it one copy per run, not one per
write: `repair_jumps.py` writes in three places and one run is one thing to
undo.

Two things that cost a minute each. The copy is `close()`d rather than left
to `with sqlite3.connect(dest)`, which commits a connection and does not
close it -- on Windows the open handle locks the very file the user was told
to delete when they are happy, and the selftest that deletes it caught that
on the first run. And `repair_underground.py` went through a bare
`sqlite3.connect`, so it had neither WAL nor the busy timeout nor this; it
opens a `Store` like every other tool now.

**A marker checkbox has to reach both places a mark is drawn.** Deaths,
respawns and teleports appear in their own layer group *and* drawn onto the
legacy dungeons' permanent paths, and a toggle only refetched its own list --
so unticking Deaths cleared the 61 on the world and left 20 sitting on the
castles, and unticking Teleports left 8. Measured both ways on `routes.db`.
Two halves to the fix, and neither works alone: the drawing has to ask
(`state.warps ? warpsInside(v) : []`, which the deaths and respawns beside it
already did), and the toggle has to redraw the dungeons, which is
`loadDeaths().then(() => loadInteriors())`. Measured after: every box goes to
exactly 0 and back to 81, 52 and 104.

`loadWarps()` also stopped returning before its fetch. `state.warpList` is
not the teleport layer's alone -- the dungeon drawings read it -- so
returning early left it holding whatever it held before the session filter or
the time window changed.

**The recorder going away said nothing at all.** Stop it with the map open
and every zoom threw `TypeError: Failed to fetch` into the console, the
status line still read Live, and a checkbox you had just unticked stayed
drawn, because the loader threw before it reached `clearLayers()` -- so the
page was lying about what was on it. `lostRecorder()` says it once, in a
banner, and names the command that brings it back. It hangs off a global
`unhandledrejection` listener rather than a try/catch per call site, because
a fetch can fail at twenty of them and they all mean the same thing -- and a
call site added later cannot forget to ask.

**One index, and three endpoints stop blocking the event loop.** Almost
every read here asks for one layer over a slice of time. With separate
indexes on `layer` and on `ts_ms`, SQLite took the layer one -- 53k surface
rows -- and sorted all of them in a temp B-tree to satisfy the `ORDER BY`,
6.61 ms, once per call; `exit_anchor()` makes 125 of those per
`interior_visits()`, and `warps()` runs `interior_visits()` again through
`_dungeon_places()`. `idx_samples_layer_ts` on `(layer, ts_ms)` makes it one
seek. Measured on `routes.db`: `interior_visits` 813 ms -> 13, `warps` 959 ->
152, `stats` 1129 -> 323, and over HTTP `/api/interiors` 811 ms -> 13. The
schema runs on every open, so an existing database picks it up on the next
start: 33 ms, once.

It matters more than a fifth of a second sounds, because these are
synchronous calls on the asyncio loop -- while one is running the live feed
is not being served. What is left is `/api/stats` at ~600 ms, which is a
Python walk over all 76k samples and would need a cache rather than an index;
an index on `(session_id, ts_ms)` was measured at 101 ms -> 89 and is not
worth the write cost.

**A mark's plane is not its layer.** `/api/interiors` sends `plane` and the
viewer files pins by it; `/api/deaths` and `/api/warps` sent `layer`, which
for a dungeon is the string `interior`, and `onThisPlane('interior')` means
surface. 58 of the 138 deaths in `routes.db` carry that layer. They land
correctly today only because every dungeon in it opens off the surface -- one
off Siofra would put its deaths over Limgrave, which is the same bug as "Half
a plane fix is worse than none" in a place the fix did not reach.
`Store.map_planes()` answers it from the visits, `Server._plane_of()` joins
the two, and the viewer reads `mark.plane || mark.layer` so an older recorder
behaves exactly as it does now. Measured after: 0 marks change map.

**Three smaller ones from the same pass.** `finish()` wrote the closing leave
event with `layer=INTERIOR` hardcoded, whatever map you were on -- 28 leave
events in `routes.db` claim a Lands Between tile is the inside of something.
The reload counter was raised twice for one load screen. And `route()` now
ends a segment on a session boundary as well as on a break: samples come back
ordered by time across every session, so two that overlap -- an import
covering an afternoon a live session also covers -- would be strung into one
line zig-zagging between them. Nothing in `routes.db` overlaps and every
session's first sample carries a break, so the drawing is byte-identical
(197 segments, 4,228 of 53,192 points); the guard is so that neither fact has
to keep being true.

**An id loses to an id plus an element, and the rule that lost was
invisible.** `#playback-box { padding-top: 0; border-top: 0; }` has been in
the sheet since the panel was written, and `#panel > section` (0,1,0,1) beats
it (0,1,0,0) -- so the playback section kept the 21px/5px rhythm meant for a
section with a heading in it, and it has no heading. With the paragraph under
the button gone that became visible as the button sitting low: measured, 22px
above it and 5px below. Written as `#panel > section#playback-box` now, with
nothing above (the masthead's own 16px margin is the space there) and the same
16px below. `selftest.py` asserts the selector, because the failing version
looked exactly like a rule that works.

**A dark ring is not a glow.** The death marks on the timeline are legible
against the unplayed grey and lost against the amber the played part is
painted in, which is what "hard to see when it's on top of the yellow" is.
A glow was tried when the marks went in and rejected for a reason that still
holds -- 137 marks whose median gap is 4.3px, 47 of them closer than 2px, so
a red halo merges into one red band. A dark ring cannot do that: where two
rings meet what merges is the dark, and the red lines stay separate on top of
it. No blur, so a mark is three pixels wide and one of them is red.

The rest of that round was naming and weight: `Numbers` is `Statistics`, the
section headings went from 10.5px to 12.5px (they carry the hierarchy the
boxes used to, so they have to read as headings rather than as fine print),
and the scrollbar thumb is the route's amber instead of `--edge`.

**The age ramp snapped because two of its three quantisations were coarse.**
The playback measures every stretch against the playhead -- `run.rank /
play.rankNow` -- so the denominator moves under all of them at once, and
whatever the ratio is quantised to, the whole path changes colour together
each time it ticks over. There were three: `ageRank()` returned the quantile
bucket, a hundredth of the route; `playBand()` cut the ratio into the same
twelve bands the finished map is drawn in; and the pass that applied it ran
four times a second.

The bands and the throttle are the obvious two and they were not the worst
one. Early in a playback the playhead's own rank is *small* -- rankNow
0.0004 on `routes.db` -- so with ranks rounded to a hundredth every drawn
stretch had rank 0.00 and the ratio was the same number for all of them:
measured, 26 stretches in one colour, and 119 playhead steps in a row that
changed nothing at all before everything moved at once. `ageRank()` now
interpolates between the two quantiles either side by the clock, which leaves
its value at every quantile exactly what it was; inside one bucket the
samples are evenly spaced by count, so time is the only signal there is, and
a bucket is 1% of the route.

With that in, `playColour()` takes `ageColor()` on the continuous ratio
rather than `bandColor()` on a band -- a band is a fact about a route that
has stopped changing -- and the pass runs every tick instead of every 250 ms.
It skips a stretch whose colour rounds to the same `rgb()` as last time, so
what it does per tick is a number per drawn stretch and a `setStyle` on the
few that crossed a value: 0.3 ms median and 1.4 ms worst over 255 stretches
against a 16 ms tick. Measured near the end of `routes.db`: 104 distinct
colours on screen where twelve was the ceiling before.

**A place that is nowhere had its list in two places, and the one that
mattered was the far one.** `#unplaced-box` was a panel section listing every
visit to every dungeon with no position -- three sections down from the
corner button that already stood for the place, and the only way to reach
`Put on map`, which is the one control that can fix an unplaceable dungeon.
The section is gone and the window the corner button opens carries both:
every visit to that map with `Show path` (the one on screen says `shown`
rather than offering to draw itself again), and `Put on map` under them. A
dungeon with no marker has no popup either, so this window is the only thing
standing for the place and everything about it belongs on it.

**A viewer is not a recorder, and the page said Live anyway.** `serve` and
`record` are the same server class, so a viewer answers `/ws` like a recorder:
the socket opened, `onopen` wrote **Live**, and then nothing ever arrived on
it -- a page claiming to watch a game that is not running. `meta.recording`
is the session being written, or null, and that is the question actually
being asked; with no recorder behind it the status reads **Offline map** and
no socket is opened at all.

**Names and the weight of the chrome.** The title is *Tarnished Path*, after
the Breath of the Wild feature this is the same idea as. `Play the route
back` is `Route Playback` and `Follow the route between maps` is `Auto switch
map`, both without the paragraph that used to sit under them: a sentence
explaining a control you have already understood is read once and then in the
way for ever. The playback button carries a face of its own -- uppercase,
letterspaced, `"Trajan Pro", Cinzel, Perpetua, Constantia, ...` -- which
resolves to Perpetua on this machine (measured, not assumed: the text is
exactly Perpetua's width). `--rule` went from 9% white to 20% for the
divisions of the column, with the old value kept as `--rule-soft` for the
dividers *inside* a section, so the hierarchy the boxes note describes is now
visible rather than notional. And the `Line thickness and outline` fold reads
as a control: full column width, the route's amber on its marker, a hover
that answers.

**A zoom changes how much of the path is drawn and nothing else.** Where a
mark sits and how it clusters are both in map pixels -- `d.xy`, and a 12 px
radius in that same space -- so a zoom cannot move one or merge two. The
reload asked `/api/deaths`, `/api/warps` and `/api/interiors` for them again
anyway and rebuilt every one, and `drawWorldVisible()` redrew the legacy
dungeons on the end of that. One wheel notch is 1.25 zoom levels at Leaflet's
default `wheelPxPerZoomLevel`, so the rounded level changes on *every* notch
and all of it ran on every notch of every scroll. Measured on `routes.db`:
three notches made four requests and replaced all 264 marker elements; now
one request and none, the same DOM nodes before and after.

`scheduleReload()` therefore takes a `marks` flag, false only from the
zoomend handler. The flag is sticky across a queued reload -- a filter change
and then a zoom are two calls and one timer, and the zoom must not drop the
marks the filter change asked for. And `reload()` stopped clearing
`markerGroup`: the cave pins are not the route's to take off the map, and
clearing them there left the map with no pins at all once the
`loadInteriors()` that used to put them back stopped running. That one showed
up in the first measurement after the change -- 264 marker elements before a
zoom and 237 after, the 27 pins gone -- which is why the check counts them.

**And the teleport marks were taken off the map before they were asked for.**
`loadWarps()` opened with `warpGroup.clearLayers()` and then went to the
server, so all 96 of them were gone for the length of the request, on every
zoom. That is the same flicker `reload()` was fixed for ("Fetch first, clear
second") and the same gesture; `loadDeaths()` had it right and this one never
did. Verified by holding `/api/warps` open by hand: 96 marks on the map
before, during and after.

**A fade on a local tile is nothing but flicker.** Leaflet brings each new
tile in over 200 ms of opacity, which earns its place over a network. Here
the tiles are files on disk and a notch moves 1.25 zoom levels, so every
notch swaps the tile level and starts a fade the next notch interrupts, with
the coarse blurry layer showing through the half-transparent tiles in
between. `fadeAnimation: false` joins `inertia` and `zoomAnimation` in the
list of one-word map options that each fix a reported drawing bug.

What is left is the cost of the notch itself, which is Leaflet's: reprojecting
the drawn path measures 6.3 ms for 31k points across 1,424 canvas layers, and
a whole `setZoom` 13 to 50 ms. The refetch and redraw of the route on a level
crossing is 11 ms to clear and 56 ms to draw. Neither is waste -- the path is
re-simplified for the new zoom -- and both are debounced 200 ms behind the
gesture.

**A canvas does not redraw during a zoom animation.** Leaflet stretches the
canvas the route was painted on with a CSS transform for the 250 ms of the
glide and reprojects only at `zoomend`, so the whole path -- line thickness
included -- is briefly the size it was at the old zoom and then snaps, which is
what "the paths lag behind before changing size" is. Redrawing per frame is the
other way out and it does not fit: reprojecting the 39k points on screen at
zoom 3 measures 19 ms, a whole frame's budget spent every frame for the length
of the animation. So `zoomAnimation: false`. The tiles are local files, so
arriving at the new zoom immediately costs nothing, and the path is now right
in every frame it is drawn in -- measured, `renderer._zoom` equals
`map.getZoom()` before the next tick.

`inertia: false` is the same class of problem reached by a different gesture:
the map kept gliding after a flick and the canvas caught up only when it
stopped.

**A total across three planes has to be a sum, not a measurement.** The
overworld and the underground are metres between world positions; a dungeon is
metres in its own frame with no world position at all. `stats()` keeps them
apart and adds them at the end, so "distance travelled" is the three rows
above it added up and can be checked by eye -- `selftest.py` asserts exactly
that. Underground is only shown when there is any: a nought there reads as a
broken reading rather than as somewhere you have not been.

**An average session is only over sessions that happened.** `routes.db` has
44 sessions with samples and 37 that recorded any time at all; the other seven
are the one-sample rows a port clash left behind plus a couple of four-second
starts. Counting those as zero-length sessions pulls the mean from 21 minutes
to 18, which is a worse answer to "how long do I usually play". Session length
uses the same ten-second gap cap as the total, because the longest session by
wall clock is whichever one was left running overnight -- capped, the longest
is 1 h 34 min, which is a real evening.

**Which areas are legacy dungeons is read off the labels.**
`world_visible_areas` is the wrong list for it -- that one also holds area 34,
and a Divine Tower is a small dungeon you clear rather than a castle. Nor a
third list in config, which would drift from the labels beside it.
`MapConfig.legacy_areas` is the areas whose `area_labels` entry says "Legacy",
so the split in the Numbers panel is the same fact the markers show. The store
is told the set rather than working it out: what counts as a castle is a
config question, and `Store` has no `MapConfig`.

**A window opened by a button should come out of that button.** The inset has
always appeared in the bottom-right corner, which is right for something
opened from a marker anywhere on the map and wrong for something opened from a
disc in the opposite corner -- the eye leaves the button and has to find the
window. `#inset.from-corner` shares the disc's left edge, sits just above it
and grows from `transform-origin: 0 100%`, so it unfolds out of the thing that
was pressed. Pressing that thing again puts it away: `state.insetKey` is the
visit on screen, and the disc compares before deciding whether to open or
close.

It is 535 px tall with the canvas at 352, so it carries a `max-height` and
scrolls inside itself -- three of them, because the corner variant sits 84 px
up normally and 170 px up while the timeline is there. Without it a 620 px
window pushed the title off the top of the screen.

The figures are figures now rather than a sentence. `240 of 438 points - 92 by
66 m - 14 m of height` asked to be parsed; four labelled numbers in a grid do
not, and the map ID moved into one of them because it was cluttering the
subtitle where the duration and the date belong. `/api/interior` answers about
a path and not about a place, so the ID comes from the visit rather than from
the payload.

**An area label cannot name a particular map.** Every map in area 11 is a
"Legacy Dungeon" -- Leyndell, the Divine Tower, and the Roundtable Hold -- so
the one place in the route that is nowhere in the world also carried the least
useful name in it, on its marker, in the unplaced list and on the button that
now stands for it. `[maps.map_labels]` names one map by its ID, `MapConfig.
label()` prefers it over the area label, and a name you give a map yourself
still wins over both. Seeded with the Hold and the Chapel of Anticipation,
which are the two in `routes.db` that area labels get wrong.

**One label helper, not five call sites.** `server._label()` prefers a name
you have given over the area label from config, and every place the viewer
shows a label -- the marker, the popup, deaths and teleports inside a
dungeon, the unplaced list, the live status line -- reads that one field. So
naming a cave renames it everywhere at once, and nothing in the viewer had to
change to make that true. `Store.names()` is cached because it is read once
per label and there are dozens in a single `/api/interiors`; the cache is
dropped on every write, which is the whole of the invalidation.

**`id="stats"` was already taken.** The Numbers section went in as `#stats`
and quietly replaced the points-drawn line at the bottom of the panel, which
`setStats()` writes to by that id. It is `#numbers` now. Two things wanting
the same obvious name is not rare in a panel this size.

**Time has to be measured in samples, not in hours.** `routes.db` holds one
August afternoon and then a month of nothing, so by the clock 75% of the
timeline contains 3% of the samples: colouring by age gave one colour to
everything recent, and the time slider did nothing until its last few pixels.
`Store.time_quantiles()` returns 101 timestamps with equal numbers of samples
between them (SQLite `NTILE`), `/api/meta` carries them, and both the ramp and
the slider index into that instead of interpolating between t0 and t1.

**A horizon has to keep the ramp even, not go back to the clock.** The `Oldest`
slider pulls the age gradient's far end in, and the obvious implementation --
spread the colours linearly over the chosen window -- undoes the quantile fix
inside that window, which is where it matters most. `ageWindow()` instead
takes the rank of the cut-off and renormalises the remaining ranks over it, so
the ramp stays even by sample count however far in it is pulled; anything
older clamps to 0 and gets the oldest colour rather than disappearing. The one
case that cannot work that way is a horizon shorter than a single quantile
step (1% of the samples), where there are no ranks left to spread: `flat` is
set and the colour falls back to plain elapsed time. The window is measured
back from the newest sample *drawn*, not from `Date.now()` -- with the
recorder off, "now" is hours after the last sample and the whole route lands
in the oldest colour.

**A boundary you cannot see is a magic number.** `panInside`'s padding was 90
px in two places, and there was no way to find out what 90 px looked like
without walking to the edge of the screen and watching. The `Edge margin`
slider sets it, and a dashed box (`.follow-box`, one div in the map container
at z-index 800, `pointer-events: none`) appears at exactly that inset while
the slider is in use and fades after 1.4 s. A 1 px amber hairline over painted
terrain reads as part of the map, so it carries a dark ring on both sides of
the border; measured on the live viewer, the four insets land at exactly the
value set.

**Half the panel was remembered and half was not, which reads as none of it.**
The `look` map stored the path visuals -- tint, thickness, outline, colour,
the age horizon -- and the four marker checkboxes, Keep me in view, the plane
and the thickness fold stored nothing. The settings you notice resetting are
the checkboxes, so "it does not remember your settings" was an accurate
report of a viewer that remembered five things out of twelve. `rememberToggle()`
wraps a checkbox, its key and what to do when it changes, and `pref()` /
`savePref()` are the only two places that touch localStorage. The plane is
restored only when there are underground tiles to restore it to: coming back
to an empty black screen would be worse than forgetting.

**A pixel margin cannot reach the centre of a window that is not square.**
The edge margin clamped its pixel value per axis, so the top of the slider
centred the mark on the shorter axis first: measured on a 1252x700 map, 600 px
left a 52 x 4 slot rather than a point -- the width "significantly larger than
the height". It is a *fraction of the way to the centre* now, from a 20 px
floor to half the side, so both axes arrive together whatever the window is
shaped like, and the box you drag keeps the screen's proportions on the way
in. Same measurement after: 6 x 6, dead centre. Stored under `followPct`,
because the old key holds pixels and 250 read as a percentage would pin you to
the middle for ever.

**Boxes were the wrong answer to "the sections blend together".** Giving each
one a border, a darker ground and a header strip made eight bordered panels
stacked down a 348 px column -- more chrome than the controls inside them, and
at odds with a map that is a painting. What separates them now is a full-bleed
hairline (`--rule`, 9% white) and space, with the heading carrying the weight:
10.5 px, uppercase, 0.16 em of letterspacing, trailing a rule that starts in
the route's amber and fades out within a third of its width. Nothing is drawn
around anything. The rules inside a section -- the thickness fold, the footer
-- use the same hairline, because an inner divider heavier than the ones
dividing the sections inverts the hierarchy.

**Any class that sets a display beats the `hidden` attribute.** The browser's
own `[hidden] { display: none }` is the weakest rule there is, so `.slider`,
`.ramp` and `.toggle` quietly overrode it and every row meant to be hidden
stayed on screen -- including the ramp legend in solid-colour mode and the
colour picker in every other mode. One `[hidden] { display: none !important }`
at the top of `style.css` fixes the lot, and `selftest.py` asserts it is there.

**A vertical range input runs the other way from what you would guess.** With
`writing-mode: vertical-lr`, `direction: rtl` puts the low end at the
*bottom*. Which one you want depends on what the number means, and the answer
flipped when the meaning did: while the value was *dimness*, `ltr` was right
(low at the top, so dragging down darkened). The slider now carries
*brightness* -- sun at the top, moon at the bottom -- so `rtl` is right, and
it also puts the filled amber part of the track under the thumb, which is
what makes the light read as the light you have left. Measured both times;
reasoning about vertical ranges does not work. The stored preference moved to
`route.bright` rather than being reinterpreted: the old key holds the
opposite number, and reading it as this one turns the map off on first load.

**`style.css` loads after `leaflet.css`, so it wins ties.** Styling the map
marks as flex discs set `position: relative` on them, which beat Leaflet's
`.leaflet-marker-icon { position: absolute }` at equal specificity and dropped
every icon into normal flow: each one was then displaced by the ones before it
(measured: 21, 27, 33, 41 px and growing), in screen pixels that do not scale
with the map, so the marks appeared to slide across the terrain as you zoomed.
The first marker in the pane looks fine, which is what made an early
measurement say the placement was exact -- check several, not one.

**A full-map canvas eats every click under it.** The interior overlay needs its
own pane above the markers so dimming the world does not dim it, and
`L.canvas({pane})` fills that pane with a canvas the size of the map. From the
first hover onwards nothing on the map could be clicked and the cursor stayed
the drag hand. The pane is `pointer-events: none` now and the marks drawn
inside the dungeon live in `insideMarks` (660) instead. `selftest.py` asserts
both CSS rules, because neither shows up in any Python test.

**A disc marker cannot say where it points.** At zoom 3 a 28 px icon covers
about 450 m of ground, so "is that circle on the cave mouth or beside it" has
no answer, and the apparent offset changes with zoom -- which reads as the
icons drifting. Dungeon marks are pins whose tip is the anchor
(`iconAnchor: [size/2, size + 7]`, tail drawn with `::after`). Measured after
the change: the anchor lands on its projected point to 0 px at every zoom, and
stays there through a zoom animation, so anything that still looks off is the
tile layer catching up rather than the mark being wrong.

**Redrawing on every zoom step is what flickers.** `reload()` used to clear the
route and then await the fetch, leaving the map blank for the round trip, and
`zoomend` fired it on every quarter step even though `epsilonForZoom()` only
changes meaningfully across whole levels. Now: fetch first and swap, and skip
the refetch unless `Math.round(zoom)` changed. The tile layer sets
`updateWhenZooming: true` because the tiles are local files.

**Marks are panes, not z-index guesses.** Deaths (590) and warps (570) sit
under the dungeon markers (Leaflet's markerPane, 600) so a death at a cave
mouth can never make the cave unclickable, and the interior overlay gets its
own pane (650) plus its own canvas renderer so dimming the world does not dim
it. Dimming is `pane.style.opacity` on overlayPane/markerPane/deaths/warps.

**Check the port before anything that leaves a mark.** `record` opened the
database, started a session and let the sampler write a sample before it tried
to bind, so double-clicking the launcher while a recorder was already running
produced a traceback *and* a one-point session -- `routes.db` collected four
of them (41, 42, 44, 45) before this was noticed, each looking like a real
session in the list. `claim_port()` now runs at the top of `cmd_record`,
before `load_config`, before `Store`, before `--launch` starts the game.
`port_holder()` distinguishes the four cases by asking `/api/meta` rather than
by reading the process list, because what matters is what is answering: free,
a read-only viewer, a recorder, or a stranger. A viewer is asked to stand down
(`POST /api/standdown`) since a recorder serves the same page plus the live
feed; a recorder refuses that request and the new process stops with the
running session named, since two recorders on one database each get half the
route. A server too old to carry `meta.recording` is treated as a recorder --
being wrong that way costs a message, the other way costs a stolen port.

**An underground river is a plane, not a room.** Siofra is `m12_07_00_00`:
its own map with its own coordinate origin, reached by a lift. Area 12 was
listed in `area_labels` and `world_visible_areas`, so it classified as an
interior and got drawn as a legacy dungeon lying on top of Limgrave -- the
path was on the surface map, and the underground map was empty. `[maps]
underground_areas` now names those areas and `classify()` checks it before
the dungeon labels.

`underground_y_max` is unrelated and still earns its place: it catches the
*shaft*, which is an m60 tile with a low Y. In `routes.db` the descent is one
sample at y = -76.5 in `m60_45_37` and then the river's own map at y = -530.
That one sample is why the two planes join up at the lift rather than the
underground starting from nowhere.

**A river's origin can only be measured by walking down into it.** m12 local
coordinates are neither tile-local nor world-wide -- Siofra runs x ≈ 735-957,
z ≈ 1143-1344 where the world there is (11616, 9486) -- so `to_world()` needs
an origin. The lift measures it: the last open-world reading in the shaft and
the first reading below are the same place, 2.5 s apart, so the difference is
the origin. Siofra's came out (10878.94, 8321.17) and `to_world()` puts its
first sample back at (11616.4, 9486.2), the shaft, to a tenth of a metre. The
sampler prints that line ready to paste the first time it meets a river it
cannot place, and refuses to place one at all otherwise -- a warp down there
measures nothing. `tools/repair_underground.py` re-files what was recorded
before, so a session already walked appears without walking it again; run it
again each time an origin is added.

**The underground is one coordinate space, chunked like the overworld.** The
origin started out per map, which meant walking from Siofra into Nokron
stopped the drawing dead: 162 points recorded and none drawable, the recorder
still counting up in the console. It is not per map. `m12_07` and `m12_02`
share a frame -- five crossings in one session, both directions, each moving
the local coordinates by the 1.5-5.2 m actually walked, and their x ranges
adjoining at 945-957. So `underground_origin()` takes `[maps.area_origin]`
first and `[maps.map_origin]` only as an override for a map that proves
different, and `same_plane()` is true within an underground area, or every
chunk boundary is a hole in the line -- the m60 tile-crossing bug, again,
five holes in a twelve-minute walk.

Which leaves the question of how a genuinely different frame would ever be
noticed, since it would now be drawn confidently in the wrong place.
`_check_origin()` runs on *every* walked crossing into an underground map,
not just the first: it recomputes the origin from the step across and
compares. Agreement is a few metres; a foreign frame is thousands, so 25 m
separates them with room to spare. The fixture walks into an m12_05 on its
own frame and gets told it is 5,308 m out, with the `[maps.map_origin]` line
to paste.

**Switching planes has to take the live tail with it.** `state.live` holds up
to 4000 recent points and `reload()` never touches `liveGroup`, so after
riding the lift down the walk *to* the lift was still drawn across Siofra --
which reads as the underground map showing surface paths. `selectPlane()`
clears it, and is also what the live feed calls: a sample whose layer is a
plane different from the one on screen switches the map, because being shown
the plane you are not on is indistinguishable from the tracker having
stopped. Interiors deliberately do not switch anything -- a cave is drawn on
whichever plane its entrance is on.

**The underground needs a second image, not a second projection.** Siofra,
Ainsel and Deeproot sit under the Lands Between at the same world
coordinates, so `[projection]` places a route on either plane unchanged --
what has to match is the image. `m1-underground.png` came out 9728x9216, the
same as the surface source, and tiled to the same 38x36 grid at zoom 6, so
the two overlay exactly. `tile_warnings()` now measures the second pyramid
against the first and says so if they differ, because one projection and one
set of image bounds serve both: a second map at a different size is silently
clipped to the first one's bounds and the route lands on the wrong terrain,
which reads as a bad calibration rather than a mismatched image. What none of
this proves is that the two images are the same *crop* -- only walking into
Siofra and seeing where the dot lands does that.

**`setUrl()` on a tile layer asks for a fractional zoom.** Switching planes
went through `L.TileLayer.setUrl()`, which redraws via `GridLayer.redraw()`;
in Leaflet 1.9.4 that sets `_tileZoom` from `_clampZoom(map.getZoom())` with
no `Math.round`, unlike every other path into it. With `zoomSnap: 0.25` the
next request is for `/tiles/2.25/1/0.webp`, every tile 404s and the map goes
black -- for the plane you switched to and for the one you switch back to,
which is what made it look like the underground tiles were the broken half.
`swapTiles()` rebuilds both layers instead; `onAdd` goes through `_resetView`,
which rounds. The two layers carry explicit `zIndex` so the coarse one stays
underneath whatever order they are added in.

**A mark belongs to the plane it was made on.** With no underground map there
was nothing to notice, but the moment one existed every cave, death and
teleport from the Lands Between was drawn over Siofra. Deaths and warps carry
`layer` already; `interior_visits()` now reports a `plane` taken from the
layer of the map you entered from, so a cave opening off Siofra belongs down
there and one off Limgrave does not. Everything in `routes.db` is `surface`,
so the underground currently draws empty -- which is the truth.

**A port already in use is usually our own viewer.** `serve` used to fall over
with an address-in-use traceback when a recorder was already up -- which is
the one case where the thing being asked for already exists. `already_serving()`
asks `/api/meta` first and opens that one instead; a genuine clash with
something else still fails, with the command to use another port.

**A round of what the panel and the bar look like, and one real bug hiding
inside a cosmetic complaint.** Thirteen things asked for at once. The ones
worth writing down are the ones where the measurement said something other
than what the request did.

**The scrollbar in the corner window was horizontal.** Reported: "there is a
scroll bar there that doesn't need to be there", among a list of things to
tidy in the window the corner button opens. I went looking for the vertical
one -- the window was 535 px tall and `overflow-y: auto`, so that was the
obvious answer, and taking the four figures out and folding the visit list
brought it to 487 with `scrollHeight === clientHeight`. Which was true and was
not the bug.

`box-sizing: border-box` is global, so a 384 px window with a 1 px border and
16 px of padding each side has a **350 px** content box -- and the canvas in
it is 352. Two pixels. Setting `overflow-y` alone makes the used value of
`overflow-x` `auto` as well, and `* { scrollbar-color: var(--route) }` painted
those two pixels as a **bright amber bar straight across the bottom of the
window, on every window size, permanently**. Measured: `clientWidth` 382,
`scrollWidth` 384, `offsetHeight - clientHeight` 12.

352 + 32 + 2 = 386, and `overflow: hidden auto` so a future two pixels cannot
put it back. `selftest.py` asserts the arithmetic out of the CSS rather than
the number, since the number is the thing that will move.

The lesson is the one this file keeps relearning from the other direction: I
found a real thing (the window was too tall) that was not the reported thing,
fixed it, and it made the report *look* addressed. What settled it was reading
`scrollWidth` as well as `scrollHeight`, which cost one line.

**The playback's caption, and what a caption is for.** Reported: "during the
playback there is a text box at the bottom saying if you're in a legacy
dungeon or cave, this doesn't need to be there." It does not. Hovering a
dungeon on the finished map is a question you asked and the caption is the
answer; the playback walks into one every few seconds without being asked, so
the same text is a box appearing and going at the bottom of the map --
something to read while you are trying to watch. `playEnter()` sets no caption
now. The hover still does, unchanged, and the selftest asserts both halves.

**Switching the plane by hand during playback left the other plane's path
drawn.** Reported. `playPlane()` has taken the drawing with it since the
playback learned about the underground -- but that is the function the *tick*
calls, and the panel's buttons go through `selectPlane()`, which reloads the
committed route. During playback the committed route is off the map entirely
(`playLayers()` are all removed), so that reload filled a group nobody can
see and `play.group` was never touched.

Measured on `routes.db`, paused at a moment the route is in Siofra:

| | tiles | stretches drawn |
| --- | --- | --- |
| before, pressing Underground | underground | **191 surface**, 0 underground |
| after | underground | 0 surface, **27 underground** |

The redraw is `playRedrawPlane()` now and both callers use it. Note that with
`Auto switch map` on the tick will put you back on the route's own plane
within a frame, which is what that setting means; the fix is about the map
telling the truth in the meantime, and about the case where auto-switch is off.

**The marker switches are the finished map's.** Also asked for. The playback
puts its marks down as they happen out of its own script, so unticking Deaths
mid-playback cleared `deathGroup`, which is not on the map, and changed
nothing you could see. They are disabled while it plays and the label dims
with the box -- a bright word beside a greyed checkbox reads as a rendering
fault rather than as a control that is not available.

**A drawing you have to keep pointing at a pin to see.** Reported: "hovering
over a cave should create a sort of square around the path, that you can have
your cursor inside, and it won't collapse the path." Leaving the marker lit a
140 ms fuse whatever the cursor did next, so moving towards the thing that had
just appeared was the gesture that took it away.

The drawn extent is a region now: `inside.box`, the corners of every visit's
bounds through its own transform, plus each visit's pin -- a hand placement
can put the doorway outside the path's own extent, and a region that does not
contain the thing you hovered to open it would close the moment you set off.
Padded 22 map units or 4% of its own size, with another 30 *screen* pixels of
slack in the hit test, because at the overview zoom a cave is thirty pixels
across.

Hit-tested on `mousemove` rather than with a transparent rectangle, for the
reason the path hover already is: the interior pane is `pointer-events: none`,
since a full-map canvas that takes the pointer swallows every click on the
map. The rectangle that *is* drawn is inert.

One trap, and it is the same shape as the run-splitting one: `hideInside()`
clears and re-arms its timer on every call, so a cursor moving away from the
drawing restarted the fuse sixty times a second and the hide never happened at
all. The timer nulls itself as it fires and the hover only lights one that is
not already burning. Measured, in order: leaving the pin arms it; a move
inside the region puts it out and the drawing is still up 400 ms later; a move
outside arms it again; eight further moves outside do not postpone it and the
drawing goes.

**A teleport inside a cave should not need a second hover.** Asked for in the
same breath, and it is the other half of the round that made those lines
answer on hover. A legacy dungeon is drawn on the map at all times, so a line
standing between two of its rooms for ever is one more thing on a busy map
that nobody asked for -- those still wait to be asked. A cave's drawing only
exists while you are looking at it, and *looking at it is the asking*.
Measured: a cave with one jump end inside it draws 1 dashed line at rest and a
castle on the map draws 0.

**A box that appears on a timer is about the timer.** Reported: the edge
margin's dashed boundary "will stay on screen for a little" after a flick past
the slider, and "will also disappear" if you hold the cursor on it. Both are
one 1,400 ms `setTimeout` saying something about itself rather than about
where you are pointing. It is up while the cursor is on the control and not a
moment longer. The one thing that outlives the pointer leaving is a drag,
because dragging a slider takes the cursor off it immediately, so
`pointerdown` holds it until `pointerup`. Measured: nothing at rest, up on
enter, still up after two seconds, gone on leave, up throughout a drag that
leaves the control, gone when that drag ends. And the paragraph under the
slider went with it -- the box *is* the explanation.

**Looking outside without leaving the cave.** Asked for: a button at the top
of the map, and "if you start moving it will automatically take you back to
the cave, but you can also press the button to do the same thing."

The way back is free, and it is worth saying why: nothing is stored until you
have moved `min_move_m`, so **every sample the recorder sends is a sample
where you moved** -- "the next sample" and "you started walking again" are the
same event. Standing still sends nothing and you stay outside. Being left
looking at the world with your own position mark nowhere on it is not a state
to end up in by accident, which is what makes the automatic return the right
default rather than a convenience.

Offered only while a cave is actually drawn over the world: a legacy dungeon
is on the map in the open, so there is no overlay to take down and nothing to
show you that is not already there. The five-second refresh has to know about
it too, or it puts the cave straight back and the button reads as broken.

Measured against a live simulated recorder standing in m31_02: pressed, the
world is undimmed and the overlay is 0 layers; two clean refresh ticks with no
sample in between leave it that way; the next sample brings the cave back at
5 layers with the world dim again. Getting the simulator into a cave at all
took forcing it -- `SimSource._next_mode()` picks one from six with `mode_left`
between 120 and 500 ticks, and fifteen minutes of it never went in.

**How long a session was, not how many rows it wrote.** Asked for. The point
count is a fact about the sampling rate as much as about the session: an hour
of imported route at five seconds a sample and three minutes of live capture
are the same number. `Store.sessions()` carries `active_ms` now, on the same
capped clock `stats()` totals -- it has to be the same rule or the rows would
not add up to the total above them, and measured on `routes.db` they do
exactly: 28.9 h either way.

It is a window function over every sample and it is not cheap: 112 ms against
8 for the counting half, because it sorts by `(session_id, ts_ms)` and the
index on `session_id` alone cannot give it that order. An index that could
takes it to 87, and this file already measured that index against `stats()`
and decided it was not worth the write cost -- so it is cached on the same
asked-for version key as everything else. 122 ms then 8, and `/api/meta` is
asked once at boot.

The point count is still what the delete confirmation asks about, because that
is what deleting removes, and it is on the row's hover.

**Show fewer, without having to show everything first.** One button that said
"Show fewer" only once every session was on screen meant opening a hundred
rows to look at one was a thing you could not undo until you had opened all of
them. Two buttons, and the same twenty either way: measured 6 → 26 → 46 → 26 →
6, with Show fewer disappearing at the floor.

**The transport, and the arithmetic that keeps play on the middle of the map,
for the fourth time.** Bigger buttons (34 px steps, a 46 px toggle), reverse
swapped with the left step, and the deaths switch moved into the group beside
repeat -- which puts the two *modes* at the ends and the two *steps* either
side of play, and is why reverse belongs there rather than in among the
controls that move one point at a time.

Six buttons: 34*5 + 46 + 6*5 = 246, with the toggle's centre 103 in from the
left. So the flanks are unequal again -- 210 + 12 + 246 + 12 + **170** = 650,
half of it 325, and 210 + 12 + 103 = 325. Measured on a 1400 px window: the
toggle's centre and the track's are both 874, which is where they have always
been. The check computes the same thing out of the CSS widths, and it now has
to read `.skull-btn`'s width as well.

**The grip was the right shape drawn in the wrong colour.** Reported as
looking "weird and unfinished". Two things: a 16 px circle says "drag me
anywhere" when the cut only ever moves along one axis, and it was painted in
`--route-deep`, which is **the colour the underground route is drawn in** --
a steel blue, on an amber bar, reading as a control belonging to something
else. It is an 11x16 upright pull with two hairline grooves across it and a
2 px stem down through the track, in a muted amber, with one colour held in a
custom property.

That last part is not tidiness. The old hover restated `background` on each
pseudo-element, which is fine for a flat fill and **wipes the grooves out of a
layered one** -- the check asserts that no `:hover::before` sets a background,
because that is the version that would look right until you pointed at it.

**A check that could not fail, again, and it was mine from this round.** The
first version of the "Put on map is gone" check asserted `"Put on map" in
inset_off` -- and matched the *comment* saying it had been taken out. Real
text, in the right file, saying the opposite of what the check claimed. Aimed
at the rendered rows instead.

Two more of the same family fell out of the negative pass: an ordering check
built out of `markup.index(...)` took the whole run down with a `ValueError`
when the id it looked for was gone (that trap is already written up here for
the ordering checks -- **ask `in` before you slice, every time**), and
`"function startPlacing" in page` went on passing when the function was
renamed to `startPlacingX`, because one is a prefix of the other. 24 sabotages,
24 red, after those three were fixed.

**Which places get asked where they belong, and which do not.** **Put on map**
came off the corner window and then went straight back on with a condition,
which is the better answer and is the user's: "the Roundtable Hold should
always be in the same spot no matter what, so that one doesn't need it, but
the other ones can have it."

There are two kinds of place in that window and the code was treating them as
one. Somewhere **unplaced** is a question nothing could answer -- Farum Azula
is reached by teleporter, has one visit and no way out on foot, so every tier
declines -- and you may well know where it is, which is what the whole
hand-placement tier exists for. Somewhere **nowhere** is not a question at
all: the Roundtable Hold has no way in on foot anywhere in the game and the
game's own map screen draws it off the terrain in a corner, so an offer to
correct that is an offer to make it wrong.

`nowhere_maps` under `[maps]` says which, by map name, beside the
`[maps.map_labels]` entry that already names the same place -- config, because
which places those are is a fact about the game and not about this database,
and a map ID written into the viewer would be the hardcoded value this file
keeps arguing against. `MapConfig.is_nowhere()` reads it and `/api/interiors`
sends it as `fixed`.

The endpoint had been flattening the distinction on the way out, which is why
it could not have been done in the viewer alone: the unplaced branch wrote
`"placed": "unknown"` by hand for every one of them, throwing away the
`nowhere` that `interior_visits()` had just worked out three lines earlier.

Measured on a copy of `routes.db` with Farum Azula's hand placement taken back
so there is one of each: the Hold's window has no **Put on map** and Azula's
does, and both keep the folded visit list.

Note that this leaves `Take it off the map` with an undo for every place
except the ones config names -- which is the point, since those are the ones
where there is nothing to undo to.

`Legacy dungeons` came off the Statistics panel for a related reason -- four
castles beside twenty-five caves was a number nobody was reading -- and
`d.legacy` is still counted and still sent.

**Two drawings of one walk, and the flat one was on top.** Reported from
inside a cave: "the cave I'm in, the 'by age' doesn't seem to work in it. also
every few seconds the path seems to flicker once." Both halves are the same
object.

`state.liveInside` is the tail inside a dungeon, and nothing ever took any of
it back. It is filled from the websocket, it is never trimmed, and the
committed drawing underneath is refetched every five seconds and drawn over
the *same ground* -- so after nine minutes in one cave the tail held **751
points spanning 204 by 225 m** against a committed path of 778 spanning the
same 204 by 225 m. Two drawings of one walk. And the one on top was
`color: '#f7d488'`, a single hardcoded amber at `liveWeight()`, which is
wider than the path it was covering.

So the banded drawing was never the one you could see. That is the whole of
"by age doesn't work in it" -- and it hid the height ramp exactly as
completely, which is worth saying because it means the bug was never about
age at all.

The tail now starts where the drawing underneath stops. `inside.drawnTo` is
the newest moment the overlay has fetched -- `drawnUpTo(d)`, taken off the
payload, since RDP keeps the ends of a segment and the last simplified point
is the last real one -- and the point *at* that moment is kept rather than
dropped, so the two join rather than leaving a gap. Measured live: the tail
falls to 2 points at each refresh and grows to 10, which is 0.5 to 5 s of
walking, and its first point is the drawing's last on every sample of
fourteen. It is banded by age like the surface tail, and it carries the
casing the path beside it carries -- once it is short, a head with no outline
against terrain is a seam rather than a style.

The legacy-dungeon branch of `refreshLiveInside()` needed the same two lines.
It never goes near `drawInside()` -- it redraws the permanent paths and
returns -- so a castle you were standing in had the same full-length flat
copy laid over it, and nothing in the cave fix would have reached it.

**And the flicker is the other end of it.** Within one canvas the order
layers go on is the order they are painted. `drawInside()` cleared
`insideGroup` and re-added the path, which put the path *over* the tail --
and then put the tail back on a `setTimeout(redrawLiveInside, 0)`, a task
later. Measured: the path's last layer went on 3.4 ms before the tail came
back, and a frame can land in that window, so once every five seconds the
whole cave was liable to be painted wearing the drawing underneath, casing
and all, for one frame. A whole-path flash, because the tail was the whole
path.

`redrawLiveInside()` is called inline at the end of `drawInside()` now,
0.1 to 0.2 ms after the last path layer and in the same task, so no frame can
land between them. It was on a timer because it sat at the *top* of the
function, before `inside.transform` was assigned; moving it below the
assignment is all it needed.

**A rank of exactly 1 is not a position on a ramp.** With the tail out of the
way the cave was still one colour, and that is a second bug with the same
symptom. `ageRank()` clamps anything newer than the last quantile to 1 --
`state.quantiles` comes from `/api/meta` at boot, so that is everything you
have recorded since. This file already noted that as about 5% of the ramp
going flat and left it alone. Inside a dungeon it is not 5%, it is **all of
it**: the only thing on screen is the visit you are standing in, and on the
cave measured here that visit began one second after the last quantile, so
every point of it ranked 1.0.

Which also breaks the one control that exists for exactly this. `ageWindow()`
renormalises the surviving ranks over the horizon, and with every rank in the
window pinned at 1 there is nothing to spread: at `the last fifteen minutes`,
whose entire job is to put the ramp on recent play, the cave came out in one
colour.

The newest sample drawn is the end of the ramp and the feed advances it on
every sample wherever you are standing, so the play past the last quantile is
one more bucket and the clock splits it -- which is what splits every other
bucket here too. One bucket however long it has been: the quantiles are equal
spans of play and this side of them cannot be measured from the viewer, so it
gets the same share as its neighbours rather than an invented one. Everything
else gives way by a hundredth.

Measured on the cave that was reported, at one moment, by putting the old
rule back on the running page and taking it off again:

| horizon | old | new |
| ------- | --- | --- |
| everything recorded | 2 colours | 2 |
| the last hour | **2** | **9** |
| the last fifteen minutes | **2** | **12** |

Twelve is the whole ramp. The surface is unchanged at 12 colours across the
route, which is the check that the extra bucket costs nothing where it was
already working.

`everything recorded` stays at 2 and should: a thirteen-minute visit is 1% of
19.4 hours of play, and one colour is the truthful answer to "how old is
this against everything I have ever walked".

**The ramp does not move while you are inside, either.** `recolourRoute()` is
called from the branch of the websocket handler that handles a sample with a
world position, and an interior sample is not one -- so the newest end
advanced on every sample in a cave and nothing ever recoloured anything. It
is called from the interior branch now.

That one has a trap in it and the measurement caught it. `recolourRoute()`
rebuilds the surface tail under `by age`, and `redrawLive()` ends by naming
its last run as the one the next sample appends to -- which is the exact
thing `state.liveLayer = null` is set at the top of the interior branch to
prevent, because the next surface sample is you coming *out* and joining it
to where you went in draws a line through the hill. Verified rather than
reasoned about: calling `redrawLive()` by hand while inside sets
`state.liveLayer`, so the guard has something real to guard against. And
watched across an actual walk out of the cave: `state.liveLayer` null on all
160 observations while inside, the last break in the surface buffer one point
from the end at the moment of the change, and the longest step in that buffer
still 5.5 px -- so the line broke where it should and nothing was drawn from
the way in to the way out. It is put back after the call, and `selftest.py`
asserts the order -- anchored on
the whole line rather than on the name, because commenting the call out
leaves `// recolourRoute();` behind, which contains the name and kept a dead
check green until the negative test said so.

**How this was measured at all**, since it is all live-only behaviour: a copy
of `routes.db` in the scratchpad, `record --source sim` against it with
`SimSource` subclassed to walk on the surface for ten seconds and then stay
in m31_10 -- a cave `routes.db` already knows nine visits to, so the
placement tiers have something to work with. The base class rolls x and z
over tile boundaries whatever mode it is in, so a long stay creeps the block
byte and ends the visit; the subclass pins the map id and keeps the walk
inside the room.

Worth knowing for the next time: in the in-app browser `document.hidden` is
true, so `requestAnimationFrame` never fires and Leaflet's canvas never
repaints -- nothing about painting can be measured there, and a `setInterval`
is throttled to something like one tick in six. What can be measured is the
*structure*: wrapping `clearLayers`/`addLayer` on each group and timestamping
the calls is what showed the 3.4 ms window and then showed it closed at
0.1 ms. Fronting the pane and taking a screenshot composites for a burst of
frames, which is enough to look at the drawing and not enough to catch a
flicker.

And the heredoc ate a backslash again, for the sixth recorded time -- a
`"\\n"` inside a patch script written with a quoted-delimiter heredoc reached
Python as a real newline inside a string literal. Write the patch script with
the editor, or build the newline with `chr(10)`.

**A doorway has two halves and only one of them was being sent.** Reported:
"the cave m31_15_00_00 has the exit icon on the entrance, and the normal icon
is just kinda beside it, a little away, and there is nothing in the actual
exit."

Three complaints, one cause. Every test here that asks "is this a second
mouth or a second reading of the first one" was asking it as a distance on
the surface against `SAME_DOOR_M`, and that question has no answer. m31_15's
doorway has been read three times -- 3 August at (10535.9, 9156.3) from the
five-second import, 4 September at (10544.3, 9173.4) and 12 September at
(10543.8, 9172.2), the last two in quarter-second capture and agreeing to a
metre. The coarse one is **19 m** from them, which is over the tolerance, so
it was drawn as a mouth of its own standing beside the real one; and because
`agreedDoor()` weighted by time inside, its 405-second visit beat the other
two together and the *main* pin stood on it. Hence "the exit icon on the
entrance, and the normal icon a little away".

Inside, those two readings are **10 m** apart, and the cave's real second
mouth is at local (13.8, -222.0), **184 m** away. So: 19 m outside is 10 m
inside, and 177 m outside is 184 m inside. Nothing measured on the surface
separates them and inside they are not close.

`interior_visits()` therefore sends `first_local` with every visit -- the
first sample of that stay, bounded by its own leave time, which mattered: an
entry that stored nothing at all was taking the first sample of a *later*
stay to the same map, 2,076 seconds afterwards on this cave. `sameMouth()`
asks inside when both readings can say, and outside when one of them recorded
nothing in there and has nothing to say about where it put you.

**And the way out is a doorway too.** That is the third complaint. For a cave
walked in one mouth and out the far one, the exit is the only reading that
far mouth will ever get: the visit's anchor is the mouth it came *in* by, and
the `exit` tier only consults `exit_anchor()` for a visit with no entrance of
its own. The reading has been in the recording since the beginning -- the
first surface position after leaving, refused when it follows a load screen
because warping out puts you at whichever grace you chose. It is sent now as
`exit_xy` with the last step inside beside it, and `doorwaysOf()` offers both
halves of every visit to the vote and to the pins.

**Which mouth and which reading of it are still two questions**, and now they
are asked in the two spaces that can answer them. The mouths are gathered by
where they put you inside; time picks which mouth, which is the rule measured
in the round about Sellia Crystal Tunnel and is untouched; and within the
winning mouth the reading that the most *other* readings of it agree with --
by the origin each implies -- is the one the pin stands at. That last part is
the same vote `learnDungeonFrame()` has used since the round that fixed the
drawing, so the pin and the drawing are answering one question rather than
two. Implied origin cannot be used for the first question and this is worth
knowing: m31_15's far mouth implies an origin **4.7 m** from its near mouth's,
because the inside is laid out to match the ground above it. It merges real
mouths, which is exactly what the pins exist to keep apart.

Measured across all 49 dungeons, before against after, from the same payload:

| dungeon | what changed |
| ------- | ------------ |
| m31_15 | pin **19 m** onto the real mouth; its second-mouth pin off the entrance and onto the far mouth, **0 m** from where the recording puts it, 181 m away in a place 233 m across |
| m14_00 Raya Lucaria | a second mouth **appears**, 105 m from the pin in a place 341 m across -- which is the door `frameTurn()` has been measuring its -77 degrees from all along, with nothing on the map to show for it |
| m18_00 | its second mouth moves 1-2 m, to the better of two readings |
| m30_13, m30_10 | pin moves 1.7 m and 1.6 m, same reason |
| the other 44 | untouched |

One trap found by counting rather than by reading. Clustering the survivors by
their inside position alone split Stormveil's second door into **two pins on
the same pixel**: it is read once on the way in and once on the way out, and
those land on the same surface point while being 20 m apart inside, because
you walk a little before the map changes. Close in *either* space is enough to
say "same mouth" -- only a genuinely different one is far away in both.

**And the exit pin is the dungeon's own pin now.** Asked for in the same
breath: "make the exit icon look the same as the normal one." It was 17 px,
dashed and at 0.85 opacity, on the reasoning that a door is the lesser of the
two things -- and drawn that way it reads as a different kind of place rather
than as the same one seen from its other side. Same size, same border, same
colour; still no badges, because the visits and the deaths belong to the place
and saying them twice a few hundred metres apart would be saying them twice,
and still not draggable, because dragging says where the dungeon *is*. A drag
does move it, though: the whole place shifts rigidly, which the drawing has
done since the round that made it so and the pins beside it had not.

**Four of six hand placements were being thrown away in silence.** Found while
looking at the cave above, and it is the reason the user could not simply fix
it themselves.

`_interior_visits()` retires a hand placement for any map the route has
measured a doorway for. That rule is right and the case it was written for is
real -- Leyndell, placed from a teleport in when nothing had ever walked its
front gate, beaten the day something did, with the drag 520 m from the door
the recorder had just measured. What it does not ask is *when* the drag was
made. A drag made **after** the measurement is not a guess at all: it is you
looking at where the route put the pin and saying no.

Measured on `routes.db`: six placements, four ignored, and every one of the
four was made after the doorway it was losing to -- m18_00, m30_04, m31_01,
and m31_15 itself, the last two at 18:53 and 19:06 on the day this was
reported, minutes before the message. Dragging a pin did nothing at all and
said nothing about why, which is the worst way for a control to fail.

`map_places.set_ms` already held the answer, so the rule is now that a
measurement retires a drag only if the measurement is newer. Leyndell comes
out the same way, because that walk *is* newer than that drag. `selftest.py`
asserts both halves: a placement made now wins, and the same placement
backdated to before the visit gives way. Backdated rather than re-recorded,
since `POST /api/place` always stamps the moment it runs.

Worth knowing about the interaction with the round above, because it is
visible: m31_15's drag was a correction of a pin that is now correct, so with
the drag honoured its marker sits 32 m from the mouth the route finally
agrees on. There is no way to tell that from here -- a drag says where a place
is and this one says 32 m west -- and the undo is one click, **Use the
route's own position** in its popup.

**A grace the recording cannot place is asked about rather than guessed at.**
Reported: "at the start of the game i died at 8/3/2026, 5:27:06 PM, but the
respawn was positioned wrong at 8/3/2026, 5:27:21 PM, when i actually
respawned at 8/3/2026, 5:27:26 PM."

This file has carried that as an open thread since it was first reported, with
the measurement that says why no rule reaches it. The recording, in full:

```
17:27:06  m10_01  Chapel of Anticipation  local (-38.1, -37.1)  break 0   <- died
17:27:21  m10_01  Chapel of Anticipation  local (-89.3, -26.4)  break 3   <- the rule's answer
17:27:26  m18_00  Stranded Graveyard      local (-57.3,  21.6)  break 1   <- where the game put you
```

`respawn_after()` takes the first break-2-or-3 sample, and 17:27:21 is a
load screen that looks like every real grace in the database. The reading
that is actually right carries break code **1**, a map change, which the rule
refuses on purpose -- reading one as a respawn once moved a death on the
surface into the catacomb beside it. So no tuning of the rule reaches it, and
counting does not help either: of the 282 deaths here that have a grace, 150
have a second candidate inside the window.

The answer is the one the rest of this file reaches for, and the open thread
already named it: ask. `map_events.grace_step` says how many stored samples
past the rule's answer the grace really is, `respawn_after()` walks forward by
it, and the respawn mark's popup carries **The grace is the next point** --
which becomes **Put the grace back** once it has been moved, so there is never
a choice of two directions to make. One at a time: a cluster can hold a dozen
graces and only you know which of them landed wrong.

On the death rather than on the sample, which the open thread also called
correctly: the break the rule reads is the game's word and not ours to move.
That is the same reasoning that gave `death_by_hand` a kind of its own instead
of a flag, and it means undoing is setting a number back to zero and touching
nothing else.

Verified end to end against the reported death: the grace reads 17:27:21 in
the Chapel, one step moves it to 17:27:26 in m18_00, it survives a refetch,
and putting it back gives 17:27:21 again. Stepping past the end of the
recording changes nothing rather than losing the mark.

**And four things about the playback's chrome.**

`Where you are` on the session panel is **Current session displayed**. It was
the playhead's answer written as though it were the reader's.

`Play the whole route` is the accent. It is the one *action* in that panel
rather than another row of it, and it only appears while the grips are in,
which makes it the thing you are looking for when you want out.

`5 more` is **Show all 70**, and the list scrolls. Five at a time grew the
panel five rows a press, and its top is pinned 16 px down -- so the button
doing the growing walked off the bottom of the screen and there was no way to
press it again. The panel has a ceiling of `calc(100vh - 132px)`, which is
16 px of margin at the top and the same clear of the timeline, and only
`#ps-list` scrolls, so the heading, the session you are watching and the
buttons stay where they are. Measured at 720 px and at 520 px of window
height: 70 rows open, the panel ends 10 px above the timeline both times, and
nothing scrolls the page.

**And the Route Playback button's hover took four goes.** Worth writing down
as a sequence, because each correction ruled out an axis rather than a
detail.

1. A white sheen travelling the width of the button over 0.55 s. "I don't
   like the sheen, I want a temporary effect."
2. A ring expanding out of it once, 0.45 s, no repeat -- which I read too
   literally off the word *temporary*. "I misspoke, I don't want a temporary
   effect, I want a constant hover state."
3. The plate brightening to `--acc-hi` and holding. "I don't really like the
   brighten effect."
4. The light goes *around* it instead: the plate keeps its colour and the
   accent bleeds out behind it, with a hairline of the same colour holding
   the edge -- `0 0 0 1px var(--acc-ring), 0 0 24px -2px var(--acc-glow)`.

The first two were the same mistake twice: *something happening* is not what
a hover is for. The third was right about that and wrong about which
property to move, and the fourth is the one axis of the four that leaves the
plate alone. It follows the accent variables like everything else on this
button, so the state that stops the playback glows in its own red without a
second rule.

The brightening was worth trying and the measurement behind it still stands,
since the reason originally given for the sheen was that brightening "washed
the dark text with it": it does the opposite, because the text is dark on
light, and lighting the plate takes the contrast from **7.88 to 10.45** and
from **4.67 to 6.02** in the stop state. It was dropped on taste, not on
legibility, which is the user's call to make and not a number's.

Measured with a real pointer: on the button, plate unchanged at
`rgb(224, 163, 60)` with `rgba(224, 163, 60, 0.55)` at 24 px behind it; in
the stop state, plate `rgb(236, 68, 68)` with `rgba(236, 68, 68, 0.6)`.

**Two traps in measuring a hover, both cost time here.** Reading
`getComputedStyle` in the same tick as forcing the state gives the value
*before* the transition, so it looks as though nothing happened. And the
in-app browser pane does not composite except while a screenshot is being
taken -- which this file already records for `requestAnimationFrame` and
which is just as true of **CSS transitions**: the stop state's red measured
as amber for three tries in a row, at 400 ms and then at 600 ms, because the
0.14 s ease had not advanced a frame. Take a screenshot first and read after.

One note on the checks from this round. The negative harness matched check
names with `endswith()`, which silently reported **MISSING as if it were a
pass** -- two of fifteen sabotages came back green for that reason alone.
Match on containment: the harness prints a detail after some names.

**A death inside a dungeon never said what it cost you.** Reported: "hovering
over a death in a legacy dungeon or cave doesn't draw a line to the respawn
location."

The world's death and respawn marks have drawn a dashed line between the two
since they were written -- the pair is one event and the line is how far back
dying put you. The marks drawn *inside* a dungeon had a popup and no hover at
all. That is the same half-fix the teleport marks needed and the same
sentence fixes it: `showDeathLines()` takes the group and the renderer now,
because a mark drawn inside a dungeon is in that dungeon's own frame and only
the caller is holding the frame.

On hover in both kinds of drawing, which is where this differs from the
teleports. A cave's line is part of its drawing -- the drawing only exists
while you are looking at it, and looking at it is the asking -- but a cave has
far more deaths than teleporters in it, and a dozen red lines laid over a
path 200 m across buries the thing they are drawn on. A castle is on the map
at all times, so a standing line per death would be a permanent scribble
across it. Both ends answer, the way both ends of a teleport do: the death
draws to the grace and the grace draws back to the death.

**The other end is not always in this frame, and all three cases happen.**
`markEndAt()` is the one rule: inside the dungeon being drawn it is that
dungeon's own metres through `at`; out on the surface it is a recorded world
position, which is the same pixels once both are projected -- the lesson from
the round that found 13 gate ends pointing nowhere because a line to a real
recorded position had been called undrawable; and inside *another* dungeon
there is no world position at all, so that dungeon's pin stands in, which is
what `warps()` has done server-side for a jump with an end inside somewhere.

Of the 145 deaths inside a dungeon on `routes.db`: **138** graces in the same
place, **2** out on the surface, **4** in another dungeon -- Stormveil into a
Divine Tower twice and m35_00 into Leyndell twice -- and **1** with no grace
at all. So the rare branches are not hypothetical, and the pin was known for
all four of the ones that need it.

`state.dungeonAt` is the map id to pin lookup that last case wants, filled in
`loadInteriors()` from the same payload the pins are built from.

Measured after: on the castles, **32 of 33** death marks draw a line and all
33 clear it -- the one that does not is that single death with no grace, which
should draw nothing. In one catacomb's overlay, all **10** marks draw and
clear, five deaths and five graces. The line starts **0 px** from the mark it
was drawn for, runs 73.3 m inside the place, and goes into the cave's own
group with `insideRenderer`, so it dims and comes down with the cave rather
than being left on the world.

One more check that could not fail, caught by the negative pass and worth the
pattern: asserting `"state.dungeonAt.set(v.map_id, v.xy);" in page` stayed
green when the fill was gated out with `if (false)`, because the name is still
there. The check asks for the whole line, guard included.

**A grace you warp back to is a grace you get up at, and both marks were
drawn on the same pixel.** Reported: "make a better solution for places you've
teleported to but also respawned at, now either one is over the other."

Each kind of mark was clustered and drawn by its own loader, each knowing
nothing about the others, so two kinds landing within the clustering radius
simply stacked -- whichever pane sits higher wins and the other is gone. On
`routes.db`, **40 of 286 spots** hold more than one mark, and 83 marks were
standing on each other:

| what is sharing the spot | spots |
| ------------------------ | ----- |
| warping out of a grace you also warp into | 19 |
| a respawn under a teleport arrival | 15 |
| three deep (a respawn and both ends of a jump) | 3 |
| a death with a respawn or a jump end | 3 |

The reported case is the fifteen. The nineteen are the same collision between
two marks of *one* kind and had never been noticed at all, because ✦ and ✧
sitting on each other look like one mark that is simply there.

**They were nudged apart first, and that was the wrong answer.** `slotAt(i, n)`
put the i-th of n marks sharing a point evenly around a circle of 11 px, which
worked -- 23 pairs exactly on top of each other before, 0 after, the closest
two icons 18.7 px apart -- and read as *two places* a few pixels apart rather
than as one place with two things to say about it. Asked for instead: "try a
split symbol, with one symbol on each side. Hovering over it should show the
dashed line for both the teleport and death."

So a shared spot is **one marker with a cell per kind**: a disc cut down the
middle, each half keeping its own glyph, its own colour and its own count,
with a dark hairline between them. It sits exactly on the point -- measured
over all of them, the worst is 0.34 px out, which is the rounding in the DOM
rectangle -- and sitting on the point is the one thing a nudge could not do.

**A cell is half a disc, not the width of what is in it.** The first version
sized each cell to its glyph and its count side by side, 20 px or 26 with a
number, which made a pair 40 to 52 px and a triple 72 -- a lozenge lying
across the terrain beside marks a third of its size. Reported: "the split
markers are way too wide, they could probably be a circle split down the
middle, with the respective numbers in their corners."

Which is the whole of it. `SPLIT_CELL` is 12, every cell is that wide, and two
of them make a 24 px box whose radius is bigger than either side -- so the
halves close into a circle the size of a single mark carrying a count. The
numbers move out to the corner on their own side, which is the corner a single
mark of that kind already puts its count in, and they wear that half's colour
so which number goes with which glyph needs no working out. They had to be
pushed clear first: a badge where the single marks put theirs covered four
pixels of the very glyph it was counting, because a cell is twelve pixels wide
and the badge is eleven.

Measured on the map: **every split 24 x 24 px**, against 40 to 72 before; with
the counts hanging off it the widest footprint is 30 px, against 26 for a
single mark carrying one and 18 for a bare one. No glyph is covered.

The objection to merging them was real, and `drawWorldMarks()` is what answers
it. Each kind has its own switch in the panel and has to come off the map
without taking the others with it; nothing here is built from a previous
drawing, so a switch coming off simply rebuilds the spot with one fewer cell.
Measured on `routes.db`: **33 splits holding 66 cells**, every one of them a
pair. With Deaths unticked: 31 splits, 62 cells, no death cell among them, and
the two spots that were a death beside one other thing back to being single
marks. Ticking it again gives 33 and 66 again, the numbers it started with.
With Teleports unticked there is no split left at all, which is the truth
about this database: every shared spot has one end of a jump in it.

**And it answers with everything it stands for.** The lines are *gathered*
rather than drawn as each cell is built, because `showDeathLines()` clears the
last set before drawing a new one -- a death cell and a respawn cell each
calling it would leave only the second on the map. `markLines()` says what one
kind has to draw, `wireMarkHover()` collects them and makes one call per kind.
Measured on six splits: a death-and-teleport disc draws 1 death line and 1
warp line, another 3 and 2, a respawn-and-teleport 3 and 2, and every one of
them is back to nothing on the way out.

`markFace()` and `markPopupEl()` are the other half of that: what a kind looks
like and what it says are written once, so a cell of a split and a mark
standing on its own cannot drift apart.

**One function draws all of them**, because the answer depends on all three
lists at once and the loaders finish in whatever order their fetches do.
`drawWorldMarks()` clears the three groups, gathers every cluster of every
kind, works out which are sharing a spot, and only then builds the markers;
`loadDeaths()` and `loadWarps()` each end by calling it. That is one pass of
**9.1 ms** median and 16.5 ms worst over 329 marks, run twice on a reload --
against a reload that is already a fetch and a full redraw of the route, and
against the thing it replaces, which drew each kind once anyway.

The clustering loop itself was written out four times, once per kind, which
is a bad place for four copies: the spot register is only sound while every
kind agrees about what one spot is. `clusterMarks()` is that loop and
`CLUSTER_PX` is that number.

**A spot is a smaller thing than a cluster, though, and it took the report to
see it.** The register used `CLUSTER_PX` as well, on the reasoning that a spot
is a spot whoever is looking at it -- and twelve pixels is right for "these
two deaths are one mark with a 2 on it" and wrong for "one of these is hiding
the other". Two 24 px discs twelve pixels apart overlap by half and you can
see perfectly well that there are two of them; calling that one place says
they are together when they are merely near. Reported as "it should only split
if they are like directly on top of each other".

There is no band in the data to cut at. Measured over every cross-kind pair on
`routes.db` closer than 30 px: **21 at 0.15 px or less** -- a grace you warp
into and get up at is the same recorded position, so those are exact -- then a
gap to 1.39, and after that 2.1, 2.5, 2.7, 2.8, 2.9, 3.2, 3.9, 4.0, 5.0, 5.4,
5.7, 7.2 and up with nothing to separate them. So `SPLIT_PX` is read off the
mark instead of off the data: **6 px, a quarter of the disc**, which is about
where the thing behind stops looking like a rim and starts looking like a
second mark. 40 splits before, 33 after.

One thing falls out of that number and is worth knowing: three kinds on one
spot needs eight pixels of tolerance, and at six there are none. Every split
on this database is a pair, which is what makes "a circle cut down the middle"
the shape rather than an approximation of one. Three cells still work -- they
make a 36 px pill, with the middle count above rather than below -- there is
simply nothing here that draws one.

**And a dungeon's own marks are drawn by nobody else, so they went on
stacking.** Reported: "the split markers don't seem to work in legacy dungeons
at least." They did not, and neither did the count that had been on the
world's marks since long before any of this: `drawInteriorInto()` has its own
three loops and put down one marker per death, per grace and per end of a
jump, in the dungeon's own frame, with nothing asking what was already there.

Measured over the four castles on `routes.db`: **99 marks, and 93 pairs of
them within six pixels of each other** -- 57 of one kind and 36 of two. So a
grace you got up at nine times was nine markers on one pixel, and the waygate
beside it was underneath them.

What was missing is one idea rather than one function. A **cell** is what one
kind has to say at one point -- its face, its count, its popup, and the lines
it draws while you point at it -- and `placeMarks()` is the register, which
knows about nothing else. The world builds its cells from the three lists it
already gathers; `drawInteriorInto()` builds them from the visit it is
drawing, and hands them to the same function with its own group and pane. The
lines come with them (`lineGroup`, `lineRenderer`), because a line in a
dungeon's frame belongs in that dungeon's group and only the drawing is
holding the frame.

Measured after, on the same database:

| | marks | markers | of which split |
| --- | --- | --- | --- |
| four castles, drawn in the open | 99 | **76** | 5 |
| 37 caves, drawn on hover | 305 | **156** | 8 |
| the world map | unchanged | 296 | 33 |

The world's numbers are identical before and after -- 33 splits over 263
single marks -- which is the check that the refactor moved the interior into
the register rather than changing what the register does.

One of those eight is **three deep**, which is the case the last round left
standing on purpose: three cells make a 36 px pill with the middle count
above rather than below. There was nothing on the world map that drew one and
there is something inside a cave.

The popups had to learn to count with them. A marker per death never needed a
plural, and a cluster of nine does: `insideDeathPopup()`, `insideRespawnPopup()`
and `insideWarpPopup()` are the world's popups in the words that are true in
here -- there is no world position to name, and the place is the one being
drawn -- with the action kept for the case where there is exactly one thing to
act on. A cluster of twelve deaths has no single moment to clear, and only you
know which of them was not a death.

**The dashes run the way you went.** Asked for in the same breath: "could you
make the dotted lines between teleport markers move in the teleport
direction."

The line is drawn from where you left to where you arrived -- the pair has
been `[from, to]` everywhere since jumps were first drawn -- so an SVG path
runs from its first point to its last, and walking `stroke-dashoffset`
towards negative moves each dash along that direction. Verified on the live
viewer rather than assumed: the path's first point is the departure to the
pixel (`M394 634 L538 86` against a departure at 394, 634), and the offset
goes 0, -6, -12, -18, -24 across the cycle.

**-24 rather than any other number**: the pattern is `5,7`, so twelve pixels
is one dash plus one gap and two of them repeat seamlessly. A cycle that ends
anywhere else jumps at the wrap.

It has to be SVG. The canvas renderer has no dash offset at all, and stepping
one by hand would mean repainting the canvas the line shares -- which on the
world map is the whole route, sixty times a second, to shift a few dashes.
`dashesIn()` keeps one `L.svg` renderer per pane the jump lines are drawn in,
made the first time it is wanted; there are never more than a handful of these
paths at once, which is the size SVG is good at. They are `interactive: false`
as well, since nothing is bound to them and a line lying across the map should
not be able to take a click meant for what is under it.

`jumpLine()` is the one place a jump line is built, and there are three that
draw one: the world's hover, a castle's hover, and the line that stands inside
a cave while you are looking at it. `selftest.py` asserts that `dashArray:
'5,7'` appears exactly once in the file, so a fourth cannot be written that
quietly stands still.

Worth knowing for the next time something is animated here: **the in-app
browser cannot show it.** `document.hidden` is true, so the animation reports
`playState: "running"` with a `currentTime` that never leaves 0 and a computed
offset frozen at 0px -- which looks exactly like a keyframe that does not
work. Drive `getAnimations()[0].currentTime` by hand and read the value back;
that is what the five samples above are.

**Two of the numbers are places, and they are not the server's to answer.**
Asked for: "add a new stat that's for the most used teleport, and most used
respawn, and add a button that takes you to where that is on the map."

Every other row in Statistics comes from `/api/stats`. These two cannot: the
question is which *spot* you used most, a spot is a cluster of marks, and the
map clusters in pixels, which the server does not have. So they are counted in
the viewer off the same three lists the marks are drawn from, at the same
radius, and `drawWorldMarks()` rebuilds them whenever those lists change --
which makes the number in the panel and the number on the badge one fact
rather than two that can drift.

**Both ends of a jump count.** A grace you warp away from and come back to is
one teleport used twice. On `routes.db` the busiest spot is 4 arrivals and 2
departures at one grace in Limgrave; counting arrivals alone would have called
it a tie between six places used four times each -- and the mark standing
there is the split disc reading 4 and 2, so the panel's 6 is what the two
halves add up to.

**And both coordinate spaces, because the answer lives in both.** The busiest
respawn on this database is **31, deep inside a catacomb**, against 7 for the
busiest anywhere on the surface. A stat that counted only what has a world
position would have named the wrong place by a factor of four and then offered
to take you to it.

Clustering inside a dungeon is done in that dungeon's own metres at
`CLUSTER_PX / scale`, because an interior frame is a turn and a shift and
never a stretch -- so that is the same question the drawing asks in pixels,
give or take the turn. Measured over all 31 dungeons with a respawn in them:
the panel's count and the badge on the drawn mark **agree on every one**.

A spot used once is left out rather than reported: every place you have ever
warped to is a place you warped to once, and "most used" for that is noise.

**The button is the interesting half.** A world spot needs only the view. A
spot inside a dungeon has no world position at all, so `goToSpot()` draws the
place first -- `showInside(visits, true)`, the same thing hovering its pin
does, pinned so it stays -- and then reads the point through
`inside.transform`, which is the frame it has just been drawn in. Either way
it ends by opening that mark's own popup, because a screen of discs with no
way to tell which one was meant is not an answer. Measured: the map centres
**0 px** from the mark in both cases, and it works with `Caves and dungeons`
unticked, since the overlay is not that layer.

**And then the stat caught a bug the marks had all along.** The two numbers
disagreed on exactly two dungeons -- m31_10 said 21 in the panel and 9 on the
badge, m11_00 said 7 and 4 -- because `drawInteriorInto()` was called once per
*visit* and clustered what it found in that call. Nine visits to one catacomb
put nine marks on one pixel, each counting only its own run.

Which is the stacking this round began with, one level up: a place you went
back to is one place, and hovering a cave draws every run through it. The
accumulator now belongs to the *drawing* -- `insideAccumulator()`, passed in
as `opts.marks`, placed once by `placeInsideMarks()` when every visit has been
walked. The permanent castles gather one per dungeon, the hover overlay one
per group, and a caller that draws a single visit still places its own.

Measured on the four castles: 99 marks before any of this, 76 markers once
kinds shared a disc, **63 once visits did**. And the panel and the badge now
agree on all 31 dungeons.

The heredoc ate a backslash again on the way past -- seventh time in this
file's history, and this one was a `"\nfunction "` in a patch script that
reached `selftest.py` as a real newline inside a string literal and broke the
whole run with a `SyntaxError`. The rule stands: write the patch script with
the editor, or build the newline with `chr(10)`.

**A line you have to keep pointing at is a line you cannot pan.** Asked for:
"clicking on a marker should make the path from one marker to another persist
on screen even when panning."

Hovering has always been the whole of it: `mouseover` draws the lines and
`mouseout` takes them away, so following a jump across the map was impossible
-- the moment you take hold of the map to drag it, the cursor leaves the mark
and the answer goes with it.

**Hung on the popup rather than on the click**, which is the part worth
keeping. The popup is already the thing that says which mark you are looking
at, and Leaflet maintains it: it opens on the click, closes on a map click or
on Escape, swaps when you press another mark, and toggles when you press the
same one again. So `popupopen` pins the lines and `popupclose` puts them away,
and all four gestures work without a second set of rules to keep in step with
the first. Measured with a real pointer: press a mark, 1 line and pinned;
drag the map 120 by 110 px, still 1 line and the popup still open; press the
map, 0 lines and unpinned. Escape does the same.

While one is pinned a hover does nothing at all -- neither draws nor clears.
That is not a detail: a drag sweeps the cursor across whatever marks lie
between where you took hold and where you let go, and without the guard the
pinned answer would be swapped for each of them in turn and then wiped. The
dungeon overlay has had exactly that rule since it was written.

**And a mark inside a cave needs the cave pinned too.** Its lines are in that
dungeon's own group, and that drawing exists only while you are pointing at
it -- so pinning the lines alone would have both vanish on the first pan.
`inside.pinned = true` when the lines belong to `insideGroup`; a castle needs
none of it, being on the map at all times. Measured: press a mark inside a
cave, pan 420 by 300 px and let the hide fuse run -- the cave is still drawn
(21 layers), the world still dim, the line still there; press the map and all
three go together.

A rebuild of the marks drops the pin, because the mark it belongs to may not
be among the new ones -- a filter change is how a mark stops existing.

**And a jump's two ends are one thing at one place.** Reported: "if a split
marker combines three different markers, the teleport from, the teleport to,
and the respawn, then make both teleport markers into one, since you can see
where they go to and from by hovering."

A grace you warp away from is usually a grace you warp back to, so a spot very
often holds both ends of one family -- and they were drawn as two cells saying
the same word twice, the bright arrival beside the dim departure. With a
respawn there as well that is a three-cell pill where a disc would do.

`mergeFamilies()` runs in `placeMarks()`, between the spot register and the
decision to draw one mark or a split, so the rule is stated once for the world
map and for the inside of a dungeon. A cell carries a `family` -- only the two
jump ends have one, because only a jump has two ends; a death and a grace at
one place are two different things and stay two cells.

**Wherever they meet, not only at three deep.** The reason given for the merge
is true of the pair on its own: hovering draws every jump at the spot in both
directions, and the popup keeps the two sections it always had, joined by the
same `popupBox()` a split uses. Merging only when a third kind turned up would
have meant a mark that changes what it says depending on what else is nearby.

The face is the first cell gathered and the gather order is arrival then
departure, so a place you both arrive at and leave from wears the bright face
and one you only ever left from keeps the dim one. The count is every end at
the spot -- which is the number the panel's **Most used teleport** reports, so
the two now agree exactly: the busiest spot on `routes.db` reads **6** where
it used to read 4 beside 2.

Measured across the database:

| | splits | cells | three-cell |
| --- | --- | --- | --- |
| the world map, before | 33 | 66 | 0 |
| the world map, after | **19** | 38 | 0 |
| the castles, before | 9 | 19 | 1 |
| the castles, after | **7** | 14 | **0** |
| the caves, before | 8 | 18 | 1 |
| the caves, after | **7** | 15 | 1 |

The 14 splits the world lost were nothing but a jump's two ends, and they are
single marks now -- 263 single marks to 277. The one three-cell left is in a
cave and deserves to be: a teleport, a death and a grace, which is three
things rather than one thing twice. 17 marks in all now carry both sections in
their popup.

Worth knowing for the next measurement through the browser pane: **the click
frame is not the CSS frame.** A screenshot came back 800x600 for a 1024x768
viewport and said so only in the scaled shot's own note, so the first three
"real pointer" clicks landed 28% away from the marks they were aimed at and
opened whatever happened to be there -- which read as the handler not firing.
Multiply by `800 / window.innerWidth` before clicking, or take the scaled
screenshot and use the frame it reports.

One thing fell out of it. Three checks in `selftest.py` sliced the file on
`addWarpMarks`, which no longer exists -- one of them with `.index()`, which
**raises** rather than failing and took the whole run down instead of
reporting one red line. That trap is already written up here twice. The rule
is the same every time: ask `in` before you slice, and when a check is really
about a property, assert the property rather than a line ordering that happens
to imply it.

**A teleport into a dungeon was drawn at its door, and the playback was the
half that never got the fix.** Reported in the same breath as the split above:
"during playback, I noticed that when I teleported into a legacy dungeon, it
showed that I teleported to the entrance, but I actually teleported inside."

`playEvents()` asked one question of a jump -- `w.inside`, which means *both*
ends in one dungeon -- and sent everything else to the world plane on `w.xy`.
For a gate, `w.xy` is the dungeon's **pin**: `/api/warps` has no other way to
say where an end inside a dungeon is, so it hands back the place the marker
stands. The arrival's own local position was in the payload the whole time,
and the rule written three lines above `playEvents()` says it outright --
never fall back to the world position for a mark made inside, because that
position is the entrance. The map was fixed for exactly this a round earlier
and the playback was not.

Each end is resolved in its own visit's frame now, on **its own moment**: a
jump out of a dungeon carries the arrival's timestamp, which is after you
left, so testing `w.ts` against the departure throws away every one of them.
Measured on `routes.db`: 15 arrivals inside a dungeon, every one now drawn at
the spot it happened at, **7 to 440 units** from the pin it used to be drawn
on -- Leyndell 440, Stormveil 399 and 299, Raya Lucaria 230, the Chapel 103.

**And then each end has to be drawn in the place it belongs to, which is not
always the place the other one is in.** That half was mine to find, and it is
this file's own warning reached from the other side: a mark drawn in a
dungeon's frame and left in the world's marks is "an arc still lying across
Limgrave an hour after you left".

`playFlash()` worked out one home for the whole event, from the arrival. So a
jump *out* of a cave -- arrival on the world, departure in the cave's own
metres -- put the departure mark in `play.marks`, where it stayed for the rest
of the playback: standing on open ground at a spot inside a cave that had long
since come off the map. Measured on `routes.db`, six such jumps, **14 to 270
units** from the cave they belong to.

So each end asks about itself. Out on the world, or inside a dungeon drawn in
the open, it lasts like the world's own marks; inside the dungeon currently on
screen it goes up and comes down with that dungeon; and inside one that is not
on screen there is no frame here to draw it in at all, so it falls back to
that dungeon's **pin** -- which is not a guess, it is the same stand-in the
finished map draws for the same end. Measured after: all six on their cave's
pin, none at the stranded pixel.

And one end being undrawable no longer takes the other with it. The guard was
one `return` for the whole event, so seeking past a jump *into* a cave -- an
arrival with no frame on screen, because a seek ends where the clock says --
lost the surface departure as well, which was right there and is what the map
draws. Measured across the five, once the playback has walked back out: a mark
stands at the departure point on the world in all five now; before, three of
them had it in the cave's own marks, which come down when you leave, and two
had nothing anywhere.

What it deliberately does not do is draw the arrival at the pin when the
dungeon is not on screen. That is the reported bug written down as a fallback,
and the rule stands: a mark made inside is drawn in its visit's frame or not
at all.

## Open threads

- `nowhere_maps` holds one entry, the Roundtable Hold. The Chapel of
  Anticipation is arguably the same case -- it is not anywhere in the Lands
  Between either -- but it was deliberately placed by hand, so it is left
  alone; adding it would take that placement's undo away. Anything else the
  game draws off its own map belongs in that list when it turns up.
- The interior overlay is anchored at one doorway and scaled in metres. Where
  a second doorway is known and the two agree, the rotation is now derived
  from them -- see below -- but most dungeons have only one, and some have two
  that cannot both be right.
- The interior inset is static. `/api/interior` returns a visit's path in
  local metres, keyed by map ID plus the visit's time window (interiors have
  no world position to look them up by), and the inset draws it with one
  scale on both axes -- but it does not follow the live feed, so while you
  are inside it shows the visit as it stood when you opened it.
- The underground map is switched to rather than layered: **Map** in the panel
  swaps the route filter, the marks and the background together, and the live
  feed switches it for you. `viewer/tiles-underground` is built from
  `m1-underground.png` and overlays the surface pyramid exactly. One real
  Siofra descent is now recorded (session 49, 63 samples) and draws on it;
  whether it lands on the right rock is the user's to judge, since the origin
  comes from a single lift and the image alignment from the two images being
  the same size. A second descent would confirm the origin -- the medoid trick
  from the dungeon doorways applies here too, and is not implemented.
- A dungeon's path is drawn in one frame pinned at one mouth, so a cave with
  two mouths lines up at one of them and nowhere else. Both mouths carry a pin
  now, but the far end of the path still lands where the interior's own
  geometry puts it rather than where the surface picks you up. `frameTurn()`
  can only help where the two doors agree about how far apart they are, and
  m31_05's do not (94 m on the map, 13 m inside). Where it *can* help it is
  worth a great deal -- Raya Lucaria was 77 degrees and 133 m out until a
  second door gave it an angle -- so the ones still to measure are the
  one-door dungeons, and the caption now says which those are.
- `frameTurn()` still only sees a second door where the *tiers* gave one, and
  the doorways the pins are drawn from now include every walked exit. Those
  are two lists of the same thing built two different ways: the turn is
  measured from `doors`, filled by the tier loop in `learnDungeonFrame()`,
  while the pins come from `doorwaysOf()`. Feeding the turn from the second
  list would give a through-walked cave its orientation for the first time --
  m31_15's two mouths are 184.1 m apart inside and 179.4 m apart on the map,
  2.5% and a rigid turn of 0.2 degrees, exactly what that function is for.
  Not done, because the turn changes where every point of a drawing lands and
  this round had already moved the pins.
- The grace after a death is the first broken sample, and sometimes that is
  the wrong reading -- 3 August 17:27:06 in the Chapel of Anticipation is the
  one that was reported. **Done**: `map_events.grace_step` holds the answer
  and the respawn mark asks for it. What is still open is that nothing finds
  them for you: 150 of the 282 deaths here have a second candidate in the
  window, and only the ones you notice while watching get corrected.
- One origin covers area 12, measured at the Siofra lift, and Nokron and
  m12_08 share that frame. m12_01 does not: walking down into it on
  2026-09-09 measured it 3,937 m away, and it has its own `[maps.map_origin]`
  entry. So the area origin is a default that holds until a lift says
  otherwise, not a fact about area 12 -- the remaining maps (Deeproot,
  Mohgwyn, and whatever else area 12 holds) are each still unknown until
  walked into, Mohgwyn being usually reached by warp, which measures nothing.
  The recorder checks, says what to add, and now keeps a map it finds adrift
  off the map until it has one, rather than drawing it where it is not.
- No test covers the live game path — only `--source sim`. That said, the
  live path has now run: `routes.db` sessions 4 onward are real capture, with
  negative chunk coordinates, load-screen sentinels and plausible heights,
  none of which the simulator produces. Sessions 1-3 are simulator runs, and
  the two are mixed into one drawn path — sessions now carry the source in
  their note so new ones can be told apart.

## Conventions

- Comments explain *why*, especially where a choice looks arbitrary but is
  load-bearing (the position chain, the null world position for interiors,
  simplification happening server-side).
- Failures should name the specific cause and the fix, not just report an
  error. Several rounds were lost to messages like "Could not reach the
  recorder" that pointed nowhere.
- The tool should refuse to claim success it can't back up: `probe` rejects ten
  identical readings, calibration won't report a meaningless residual, and
  `DELETE /api/session/{id}` returns 404 rather than reporting a successful
  deletion of nothing. It also refuses (409) the session `record` is writing
  to, which `main.py` hands the server as `active_session` -- deleting that one
  leaves the sampler inserting samples under a session row that is gone, and
  those are invisible in the session list while still drawing on the map.
- Prefer data-driven config over hardcoded values, and log unknowns rather than
  guessing.

## Safety

Single-player mapping tool. Read-only memory access, no game modification.
Runs offline with anti-cheat off. Keep it that way — nothing here should write
to the game process or touch online play.

# CLAUDE.md

Context for working on this repo. README.md is the front page, SETUP.md gets
it running and MANUAL.md is the reference; this covers the things that cost
time to discover and are not obvious from reading the code.

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
nor the button on the other can push play off the middle of the map, with the
speed slider and its label either side of it at the same 124 px width for the
same reason. And the toll was centred with
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

## Open threads

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
- One origin covers area 12, measured at the Siofra lift, and Nokron confirmed
  it shares that frame. Whether Ainsel, Deeproot and Mohgwyn do is unknown --
  they are reached by their own lifts and Mohgwyn usually by warp, so the
  first walk into each is the test. The recorder checks and complains rather
  than drawing them wrong, which is the part that matters.
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

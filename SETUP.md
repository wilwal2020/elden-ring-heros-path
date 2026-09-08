# Getting it running

A walk through from a fresh download to a map with your path on it. The map
background and its calibration come with the repository, so this is shorter
than it looks: steps 4 and 5 are already done for you.

**Read this first:** this is a personal tool that happens to be public. It has
been run on exactly one machine, by one person, against one version of the
game. Nothing about it is packaged, signed, or supported. If it does not work
for you, it is probably because of something the author has never had to think
about. See [What is likely to go wrong](#what-is-likely-to-go-wrong).

---

## 1. What you need

| | |
|---|---|
| **Windows** | The recorder reads the game's memory through `pymem`, which is Windows-only. The **viewer** works anywhere Python does, so you can look at a recorded route on any machine. |
| **Python 3.10 or newer** | From [python.org](https://www.python.org/downloads/). Tick **Add python.exe to PATH** in the installer. Developed and run on 3.14. |
| **Elden Ring** | Only for recording. Version 2.7.0.0 is what the signatures were taken against; see [Signatures](MANUAL.md#signatures) if yours is different. |
| **About 100 MB of disk** | Mostly the map tiles, which come with it. |

Anti-cheat has to be off for the recorder to read the game. That is the same
condition every practice tool has, and it means offline play. The tracker only
ever *reads* memory -- it never writes to the game.

## 2. Download it

Either clone it:

```bash
git clone https://github.com/wilwal2020/elden-ring-heros-path.git
```

or press **Code -> Download ZIP** on the repository page and unzip it
somewhere you can find again.

## 3. Install the Python packages

```bash
pip install -r requirements.txt
```

You can skip this: `Record route.bat` and `Open map.bat` install what is
missing on their first run.

## 4. The map background -- already done

**Skip this step.** Both tile pyramids are in the repository, and the
calibration that lines them up with the world is in `config/config.toml`. Down
loading is enough; the map draws the moment you open it.

Those tiles are the game's own artwork, included so the tracker is usable
straight away rather than after an afternoon of extraction. See
[NOTICE.md](NOTICE.md).

<details>
<summary>Building them yourself, if you ever need to</summary>

Only worth doing if the tiles here are the wrong version for you, or you want
a different detail level.

1. Extract the map images from the game with a tool that can read its archives
   -- [UXM Selective Unpacker](https://github.com/Nordgaren/UXM-Selective-Unpack)
   is the usual one. You want `menu/71_dlc02/` (the world map), or `menu/71/`
   on a game without the DLC.
2. Stitch them into one image -- the folder is a positional argument:

   ```bash
   python tools/build_map.py <where you extracted> -o m1.png
   python tools/make_tiles.py m1.png --out viewer/tiles
   ```

3. And the underground -- Siofra, Ainsel, Deeproot -- with `--map M01`:

   ```bash
   python tools/build_map.py <where you extracted> --map M01 -o m1-underground.png
   python tools/make_tiles.py m1-underground.png --out viewer/tiles-underground
   ```

4. `make_tiles.py` prints the finished size. Put those numbers into
   `config/config.toml` under `[viewer]` as `image_width` and `image_height`.
   The server measures the pyramid on disk at startup and tells you what they
   should be, so you will know either way.

Both images have to be the same crop at the same scale, because one projection
serves both planes -- the server compares the two pyramids and complains if
they differ. And a rebuilt map needs step 5, because your image will not be
the same one the shipped `[projection]` was fitted to.
</details>

## 5. Lining up -- also already done

The route is recorded in the game's metres and drawn in the map image's
pixels, and `[projection]` in `config/config.toml` is what ties the two
together. The values in there were fitted against the tiles that ship with the
repository, worst point 1.1 px out, so they are already right for them.

You only need this if you rebuilt the tiles in step 4:

```bash
python tools/calibrate.py
```

It runs while `record --source game` is going: you stand somewhere
recognisable, let it record, find that same spot on your map image, and type
the pixel coordinates. It writes the `[projection]` section for you.

Two points is the minimum it accepts, but use at least three. With two the fit
is exact by construction -- two unknowns per axis, two equations -- so the
residual it reports is always zero and tells you nothing about whether the
answer is right. The long version is in
[Line up the map with the world](MANUAL.md#line-up-the-map-with-the-world).

## 6. Try it without the game

Before going near live capture, check the whole thing works:

```bash
python tools/selftest.py
```

That simulates a route, serves it, and exercises every endpoint -- 375 checks,
no game needed. Then watch a simulated route draw itself:

```bash
python -m tracker.main record --source sim --open
```

If that opens a browser with a path moving across the map, everything except
the memory reading is working.

## 7. Record for real

Start the game, then double-click **`Record route.bat`** -- or:

```bash
python -m tracker.main record --source game --open
```

It records while you play and serves the map at the same time. Quitting the
game closes it. **`Open map.bat`** opens the map on its own, with the game
shut, to look at what you have already recorded.

Check the memory reading first if you want to be sure:

```bash
python -m tracker.main probe
```

That prints your position, your map, and your health, and refuses to claim
success on ten identical readings.

---

## What is likely to go wrong

**The signatures do not match.** Every game patch can move them. `probe` says
so plainly rather than reading rubbish. The patterns come from
[veeenu's practice tool](https://github.com/veeenu/eldenring-practice-tool)
and usually survive a patch even when the raw offsets do not --
[Signatures](MANUAL.md#signatures) has the details.

**The map is in the wrong place.** Only if you rebuilt the tiles: the shipped
`[projection]` was fitted to the shipped tiles, and a differently built image
needs its own. Run `tools/calibrate.py`.

**Nothing is drawn.** Run `tools/selftest.py`. If that passes, the pipeline is
fine and the problem is the memory reading or the calibration.

**It says the recorder is out of date.** The viewer and the server check a
version number against each other. Restart the recorder -- Python loads the
server once at startup while serving the page fresh from disk, so an update
can leave a new page talking to an old server.

More of these, with what each symptom actually means, are in
[Troubleshooting](MANUAL.md#troubleshooting).

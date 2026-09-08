# About the map images

`viewer/tiles/`, `viewer/tiles-underground/` and `viewer/compass.png` are the
world map from **ELDEN RING**, cut into a tile pyramid so it can be scrolled.

That artwork is **© Bandai Namco Entertainment / FromSoftware, Inc.** It is not
mine, it is not covered by whatever terms apply to the rest of this repository,
and including it here is not a grant of any right to it. It is here so that the
tracker draws something recognisable the moment you download it, instead of
making everyone extract and stitch the same images from their own copy of the
game.

If you are the rights holder and would rather it were not, say so on the issue
tracker and it will come out -- `tools/build_map.py` and `tools/make_tiles.py`
rebuild it from a local install in a few minutes, which is how it was made in
the first place, and [SETUP.md](SETUP.md) already explains how.

Everything else in the repository -- the recorder, the server, the viewer, the
tools and the documentation -- is my own work.

`viewer/vendor/` is [Leaflet](https://leafletjs.com) 1.9.4, © 2010-2023
Vladimir Agafonkin and CloudMade, under the BSD 2-Clause licence, vendored so
the viewer works with no network.

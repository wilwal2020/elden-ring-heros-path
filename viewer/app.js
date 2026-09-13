/* Route viewer.
 *
 * Three things keep this fast where the original was slow:
 *   1. L.canvas() instead of the default SVG renderer. SVG creates a DOM node
 *      per segment and dies well before 100k points.
 *   2. The server simplifies the path with an epsilon derived from the current
 *      zoom, so we draw thousands of segments rather than hundreds of thousands.
 *   3. keepBuffer preloads tiles outside the viewport so panning doesn't wait
 *      on the network.
 */

// How many session rows stand before "Show more", and the floor "Show
// fewer" comes back down to. Declared here rather than beside the other
// constants two hundred lines down, because `state` reads it: a const is
// in the temporal dead zone until its own line runs, and node --check
// does not catch that.
const SESSIONS_SHOWN = 6;

const state = {
  meta: null,
  stalePage: false,   // index.html and app.js came from different versions
  plane: 'surface',   // 'surface' or 'underground': one map or the other
  livePlane: null,    // the plane the recorder last reported, to follow it
  autoPlane: true,    // and whether the map follows the route between the two
  peek: false,        // looking at the world from inside a cave, on purpose
  layers: { interior: true, legacy: false },
  deaths: true,       // death marks are their own layer, not a route layer
  respawns: true,     // and where you got up afterwards
  respawnList: [],    // every respawn, for the dungeon drawings to read
  warps: true,        // and so are the places you arrived by teleport
  deathList: [],      // every death, whether or not the layer is drawn
  warpList: [],       // every teleport, including the ones inside dungeons
  alwaysDrawn: new Set(),  // dungeons whose path is on the map at all times
  placedByMap: new Map(),  // map id -> its permanent drawing
  dungeonFrame: new Map(), // map id -> where its local coordinates sit
  follow: false,      // keep the live position on screen
  followPad: 90,      // and how far from the edge it is allowed to get, in px
  // Where a jump that goes off screen puts you: in the middle, or at the same
  // place on the screen you left from.
  holdSpot: false,
  insideMap: null,    // the dungeon the recorder is in right now
  insideTimer: null,  // redraws that dungeon while you are still in it
  inset: null,
  insetKey: null,     // which visit the inset is showing, so it can be toggled
  insetMap: null,     // and which map that is, for the figure        // interior visit currently drawn in the inset
  sessions: new Set(),
  tint: 'height',     // 'height' | 'age' | 'solid'
  weight: 3,          // how thick the path is drawn
  casing: 3,          // and how much dark outline it carries
  colour: '#e0a33c',  // used when the path is one colour
  ageDepth: 0,        // index into AGE_DEPTHS: how far back the gradient runs
  mapDim: 0,          // how far the terrain is dimmed, 0 to 0.8
  drawn: [],          // the segments as last drawn, for playback to walk
  hExtent: null,      // the height range they cover, for the legend
  span: null,         // [oldest, newest] timestamp drawn, for colouring by age
  quantiles: [],      // where the samples fall in time, evenly by count
  shownSessions: SESSIONS_SHOWN,   // rows shown before "Show more"
  range: null,
  live: [],
  liveT: [],          // when each of those points was walked, for the age ramp
  liveRuns: [],       // the tail, one polyline per band
  liveLayer: null,    // the run being appended to, or null to start a fresh one
  tinted: [],         // stretches whose colour is a fact about now, not forever
  tintedAt: 0,        // when they were last brought up to date
  // Local metres of the dungeon you are in, straight off the websocket. The
  // committed path lags: /api/interior only knows what the recorder has
  // written, and the overlay only asks every five seconds, so the position
  // mark -- which moves on every sample -- ran ahead of the line behind it.
  liveInside: [],
  liveInsideLine: null,
  offmap: new Map(),  // visits to places with no position, by map id
  reloadTimer: null,
  reloadMarks: false, // whether the queued reload has to rebuild the marks too
  drawnZoom: null,    // zoom level the drawn route was simplified for
};

const renderer = L.canvas({ padding: 0.5 });
let map, casingGroup, routeGroup, liveGroup, markerGroup, deathGroup,
    respawnGroup, warpGroup, insideGroup, insideLiveGroup, insideRenderer, arcRenderer,
    placedGroup, youMark,
    tiles, baseTiles, baseOpts, tileOpts, followBox;

/* --- how the path is coloured -------------------------------------------- */

// Oldest to newest. The newest end is the colour of the position mark, so the
// line you are drawing right now matches where you are, and the further back
// in time a stretch is the further it drifts from it.
const AGE_STOPS = ['#3f5d78', '#5c6f7d', '#8a7a68', '#c79a58', '#f7d488'];

function ageColor(t) {
  return mix(AGE_STOPS, t);
}

// Where a moment sits among the samples, not on the clock. An afternoon in
// August and then a month of nothing means that by the clock almost every
// sample is at the very end -- so colouring by raw time gave one colour for
// everything recent and the whole ramp to a single old afternoon.
function agePosition(t) {
  const w = ageWindow();
  const p = ageRank(t);
  if (w.cut === null) return p;
  // Everything older than the horizon is simply the oldest colour: the point
  // of moving it up is to spend the whole ramp on recent play rather than on
  // an afternoon in August.
  if (w.flat) {
    return Math.max(0, Math.min(1, (t - w.cut) / (w.newest - w.cut || 1)));
  }
  return Math.max(0, (p - w.at) / (1 - w.at));
}

// Where a moment sits among the samples: the raw rank, before any horizon.
function ageRank(t) {
  const q = state.quantiles;
  if (!q || q.length < 2) {
    const [t0, t1] = state.span || [t, t];
    return (t - t0) / (t1 - t0 || 1);
  }
  let lo = 0, hi = q.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (q[mid] < t) lo = mid + 1; else hi = mid;
  }
  if (lo === 0) return 0;
  // Between the two quantiles either side, by the clock, rather than the
  // bucket the moment landed in. There are a hundred buckets, which is coarse
  // enough to see: the playback measures every stretch against the playhead's
  // own rank, so early on -- rankNow = 0.05 -- the ratio can only take six
  // values, and every stretch on the map changes colour together each time
  // that denominator ticks over. That is the snap. Inside one bucket the
  // samples are evenly spaced by count, so splitting it by time is as close
  // as this gets without sending every rank to the viewer, and the value at
  // each quantile is unchanged.
  const a = q[lo - 1], b = q[lo];
  const f = b > a ? Math.max(0, Math.min(1, (t - a) / (b - a))) : 1;
  return (lo - 1 + f) / (q.length - 1);
}

// How far back the gradient reaches. null is everything recorded; the rest
// are horizons measured back from the newest sample drawn, not from the wall
// clock -- with the recorder off, "now" would push the whole route into the
// oldest colour.
const AGE_DEPTHS = [
  [null, 'everything recorded'],
  [7 * 864e5, 'the last week'],
  [3 * 864e5, 'the last three days'],
  [864e5, 'the last day'],
  [12 * 36e5, 'the last twelve hours'],
  [6 * 36e5, 'the last six hours'],
  [3 * 36e5, 'the last three hours'],
  [36e5, 'the last hour'],
  [18e5, 'the last thirty minutes'],
  [9e5, 'the last fifteen minutes'],
];

let ageCache = { key: null, win: null };

function ageWindow() {
  const key = `${state.ageDepth}|${state.span && state.span[1]}|${state.quantiles.length}`;
  if (ageCache.key === key) return ageCache.win;
  const q = state.quantiles;
  const newest = (state.span && state.span[1])
    || (q.length ? q[q.length - 1] : Date.now());
  const span = AGE_DEPTHS[state.ageDepth] || AGE_DEPTHS[0];
  const cut = span[0] === null ? null : newest - span[0];
  const at = cut === null ? 0 : ageRank(cut);
  // A horizon shorter than one quantile step has no ranks left to spread the
  // ramp over, so inside it the colour goes back to plain elapsed time.
  const win = { cut, at, newest, flat: cut !== null && 1 - at < 1e-6,
                label: span[1] };
  ageCache = { key, win };
  return win;
}

function mix(stops, t) {
  t = Math.max(0, Math.min(1, t));
  const span = 1 / (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(t / span));
  const f = (t - i * span) / span;
  const a = hex(stops[i]), b = hex(stops[i + 1]);
  return `rgb(${Math.round(a[0] + (b[0] - a[0]) * f)},` +
         `${Math.round(a[1] + (b[1] - a[1]) * f)},` +
         `${Math.round(a[2] + (b[2] - a[2]) * f)})`;
}

function hex(c) {
  return [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16),
          parseInt(c.slice(5, 7), 16)];
}

// One answer to "what colour is this bit of path", so the world route, the
// dungeons drawn on the map and the inset cannot drift apart.
function bandOf(seg, i, lo, hi) {
  if (state.tint === 'height') {
    return Math.min(11, Math.floor(((seg.h[i] - lo) / (hi - lo)) * 12));
  }
  if (state.tint === 'age') return ageBand(seg.t[i]);
  return -1;                       // one colour: no banding to do
}

// Which band a moment falls in. Its own function because three things ask --
// the route, the live tail and the pass that brings both up to date as the
// ramp slides -- and they must not be able to disagree.
function ageBand(t) {
  return Math.min(11, Math.floor(agePosition(t) * 12));
}

function bandColor(band) {
  const t = (band + 0.5) / 12;
  return state.tint === 'age' ? ageColor(t) : rampColor(t);
}

/* --- height ramp --------------------------------------------------------- */

const RAMP = [
  [0.00, [74, 109, 140]],
  [0.35, [127, 160, 122]],
  [0.70, [224, 163, 60]],
  [1.00, [216, 97, 58]],
];

function rampColor(t) {
  t = Math.max(0, Math.min(1, t));
  for (let i = 1; i < RAMP.length; i++) {
    if (t <= RAMP[i][0]) {
      const [a, ca] = RAMP[i - 1], [b, cb] = RAMP[i];
      const f = (t - a) / (b - a);
      const c = ca.map((v, j) => Math.round(v + (cb[j] - v) * f));
      return `rgb(${c[0]},${c[1]},${c[2]})`;
    }
  }
  return 'rgb(216,97,58)';
}

/* --- boot ---------------------------------------------------------------- */

// What this page needs the recorder to speak. Kept next to the boot check
// rather than hidden in a module, because the whole point is that someone
// reading either half can see the pair.
const NEEDS_API = 25;

// Why the line broke, as the recorder reports it. 3 is a load screen, which
// is the only sign of a respawn there is -- see store.py, where these are
// written.
const BREAK_RELOAD = 3;

// index.html declares the controls this file wires. A browser can hold one of
// the two from cache and not the other, and then every lookup here returns
// null: wireControls() threw on the first missing checkbox, boot() never
// reached reload(), and the result was a blank map whose buttons did nothing
// -- with no clue that the page itself was half a version old.
const PAGE_BUILD = 66;

async function boot() {
  checkPageBuild();
  state.meta = await (await fetch('/api/meta')).json();
  if ((state.meta.api || 0) < NEEDS_API) showStaleRecorder();
  state.quantiles = state.meta.time_quantiles || [];
  const v = state.meta.map || {};
  const W = v.image_width || 4096, H = v.image_height || 4096;

  // Coordinates coming from the server are pixels in the source map image,
  // which is the tile pyramid's native resolution. unproject() at that zoom is
  // what makes the route line up with the tiles instead of drifting as you zoom.
  state.nativeZoom = v.tile_max_zoom ?? 6;

  map = L.map('map', {
    crs: L.CRS.Simple,
    minZoom: -6,
    // Four levels past the finest tiles rather than two. Past the pyramid
    // the terrain is upscaled and soft, but what you are looking at that
    // close is the path -- which is drawn from the route and stays sharp at
    // any zoom -- so the limit is about the picture and not about the tiles.
    maxZoom: state.nativeZoom + 4,
    zoomSnap: 0.25,
    preferCanvas: true,
    // No flick panning. Leaflet keeps moving the map after you let go, and
    // the canvas the route is drawn on only catches up when the glide stops,
    // so a flick smears the path away from the terrain and snaps it back --
    // and a flick that lands during a zoom leaves the two disagreeing about
    // where the map is. Dragging still works; it just stops when you do.
    inertia: false,
    // No zoom glide either, for a reason particular to canvas. During
    // Leaflet's 250 ms zoom animation the route is not redrawn: the canvas it
    // was painted on is stretched by CSS and put back to scale only at the
    // end, so the whole path -- line thickness included -- is briefly the size
    // it was at the old zoom, then snaps. Redrawing it per frame is the other
    // way out and it does not fit: reprojecting the 39k points on screen at
    // zoom 3 measures 19 ms, which is a whole frame's budget spent every frame
    // for the length of the animation. The tiles are local files, so arriving
    // at the new zoom immediately costs nothing and the path is right in every
    // frame it is drawn in.
    zoomAnimation: false,
    // Nor a fade on the tiles. Leaflet brings each new tile in over 200 ms of
    // opacity, which is worth it over a network and is nothing but flicker
    // here: the tiles are local files, and one wheel notch moves 1.25 zoom
    // levels, so every notch swaps the tile level and starts a fade the next
    // notch interrupts -- with the coarse blurry layer showing through the
    // half-transparent tiles in between.
    fadeAnimation: false,
    attributionControl: false,
    zoomControl: false,     // the wheel and the trackpad already do this
  });

  const imageBounds = L.latLngBounds(
    map.unproject([0, 0], state.nativeZoom),
    map.unproject([W, H], state.nativeZoom)
  );

  // Where the recorded route actually lands, in pixels. Before calibration the
  // projection is 1:1, so world metres put the route far outside the image --
  // fitting to the image alone would show an empty screen.
  const pr = state.meta.projection || {};
  const b = state.meta.bounds;
  let dataBounds = null;
  if (b && b.n) {
    dataBounds = L.latLngBounds(
      map.unproject([b.x0 * pr.scale_x + pr.offset_x, b.z0 * pr.scale_y + pr.offset_y], state.nativeZoom),
      map.unproject([b.x1 * pr.scale_x + pr.offset_x, b.z1 * pr.scale_y + pr.offset_y], state.nativeZoom)
    );
  }
  state.dataBounds = dataBounds;
  state.imageBounds = imageBounds;

  // The edge margin is invisible until you touch its slider: it is a boundary,
  // not decoration, and a dashed box permanently drawn over the terrain would
  // be one more thing between you and the map.
  followBox = L.DomUtil.create('div', 'follow-box', map.getContainer());
  followBox.style.inset = `${state.followPad}px`;

  if (v.tile_url) {
    // A coarse copy of the whole map underneath everything, at a zoom level
    // small enough to stay in memory. Panning and zooming then reveals a
    // blurry version of the right place instead of the empty background,
    // which is what made the loading obvious.
    // Kept so switching planes can build the same layers against the other
    // pyramid. zIndex rather than insertion order, so which one is on top
    // does not depend on the order they happen to be added in.
    baseOpts = {
      minNativeZoom: v.tile_min_zoom ?? 0,
      maxNativeZoom: Math.min(3, state.nativeZoom),
      tileSize: 256,
      noWrap: true,
      bounds: imageBounds,
      keepBuffer: 12,
      updateWhenIdle: false,
      updateWhenZooming: true,
      className: 'base-tiles',
      zIndex: 1,
    };
    tileOpts = {
      minNativeZoom: v.tile_min_zoom ?? 0,
      maxNativeZoom: state.nativeZoom,
      tileSize: 256,
      noWrap: true,
      bounds: imageBounds,
      keepBuffer: v.keep_buffer ?? 8,
      updateWhenIdle: false,
      // The tiles are local files, so fetching them mid-animation costs
      // nothing and stops the terrain arriving a beat after the markers.
      updateWhenZooming: true,
      zIndex: 2,
    };
    baseTiles = L.tileLayer(v.tile_url, baseOpts).addTo(map);
    tiles = L.tileLayer(v.tile_url, tileOpts);
    warmNeighbours();
    let loaded = 0;
    tiles.on('tileload', () => { loaded++; });
    tiles.on('load', () => { if (loaded === 0) showTileHint(); });
    tiles.addTo(map);
    map.on('moveend zoomend', warmNeighbours);
  } else {
    showTileHint();
  }

  // Panning is limited to whatever exists: the map, the route, or both.
  const limits = dataBounds ? L.latLngBounds(imageBounds).extend(dataBounds) : imageBounds;
  map.setMaxBounds(limits.pad(0.3));
  // Kept, because a remembered centre is worth restoring exactly when it is
  // somewhere you could have dragged to.
  state.panLimits = limits.pad(0.3);

  // Order is draw order. The casing is a dark, wider copy of the line
  // underneath the coloured one: the map is a warm painting and an amber
  // route disappears into it, doubly so once the line is shaded by height
  // and half of it goes pale. Its own group, so one segment's casing can
  // never land on top of another segment's colour.
  // Panes fix what sits on top of what. Deaths and warps go under the
  // dungeon markers so a mark can never make one unclickable, and the
  // interior overlay goes above everything so it stays legible while the rest
  // of the map is dimmed behind it.
  map.createPane('warps').style.zIndex = 570;
  map.createPane('respawns').style.zIndex = 585;
  map.createPane('deaths').style.zIndex = 590;
  const insidePane = map.createPane('inside');
  insidePane.style.zIndex = 650;
  insidePane.classList.add('inside-pane');   // pointer-events: none
  map.createPane('insideMarks').style.zIndex = 660;
  map.createPane('you').style.zIndex = 680;
  // Above the position mark, both of them. A death is the thing that just
  // happened and the dot is only where you are, so the dot went on top and
  // hid the mark at the moment it arrived -- which is the one moment it is
  // for. Two panes because dimming has to tell them apart: the world's marks
  // go down with the world when you walk into a dungeon, and the marks made
  // inside that dungeon do not.
  map.createPane('playmarks').style.zIndex = 690;
  map.createPane('playinside').style.zIndex = 700;
  // The line a jump draws gets a canvas of its own, under the marks at either
  // end of it. Fading it on the route's canvas would repaint the whole route
  // for every step of the fade.
  const arcPane = map.createPane('playarc');
  arcPane.style.zIndex = 685;
  arcPane.classList.add('arc-pane');         // pointer-events: none
  insideRenderer = L.canvas({ pane: 'inside' });
  arcRenderer = L.canvas({ pane: 'playarc' });

  casingGroup = L.layerGroup().addTo(map);
  routeGroup = L.layerGroup().addTo(map);
  // The live feed draws into its own group so a reload can rebuild the route
  // without throwing away the polyline the websocket is still appending to.
  liveGroup = L.layerGroup().addTo(map);
  // Legacy dungeons live here: drawn like the rest of the route rather than
  // waiting behind a hover, because the map already shows the place they are
  // in and a castle you walked through is part of the path.
  placedGroup = L.layerGroup().addTo(map);
  markerGroup = L.layerGroup().addTo(map);
  deathGroup = L.layerGroup().addTo(map);
  respawnGroup = L.layerGroup().addTo(map);
  warpGroup = L.layerGroup().addTo(map);
  insideGroup = L.layerGroup().addTo(map);
  // The tail of the path you are walking right now, inside a dungeon. Its own
  // group for the same reason the surface's live feed has one: drawing the
  // dungeon clears insideGroup, and a tail that lived in there would be taken
  // off the map every five seconds by the very redraw that is meant to be
  // keeping it up to date.
  insideLiveGroup = L.layerGroup().addTo(map);

  // Where you were looking last time, if it is still a place on this map.
  // Coming back to the whole of the Lands Between when you were three zooms
  // deep on one cave is a small chore, and it is repeated every session.
  const back = savedView();
  if (back) map.setView(back.centre, back.zoom, { animate: false });
  else map.fitBounds(dataBounds || imageBounds, { padding: [20, 20] });
  // Both, because a zoom with no drag in it fires zoomend and moveend and a
  // drag fires only moveend; writing the same two numbers twice costs nothing
  // and missing one of the two gestures is the whole feature.
  map.on('moveend zoomend', saveView);

  buildSessions();
  buildStats();
  wireControls();
  wireCalibration();
  wireInset();
  wireAlign();
  connectLive();

  // The route is simplified server-side with an epsilon tied to zoom, so it
  // only needs refetching when that epsilon changes enough to matter. Nudging
  // the zoom a quarter step redrew the whole path for no visible difference,
  // and the redraw is what flickers.
  //
  // And the marks are not refetched at all. Where they sit and how they
  // cluster are both in map pixels, so a zoom cannot change either -- but
  // the reload asked /api/deaths, /api/warps and /api/interiors for them
  // again anyway and rebuilt every one: 264 marker elements taken off the
  // map and made afresh, for an answer identical to the one already drawn.
  // One wheel notch is 1.25 zoom levels at these settings, so that ran on
  // every notch of every scroll.
  map.on('zoomend', () => {
    const level = Math.round(map.getZoom());
    if (level === state.drawnZoom) return;
    state.drawnZoom = level;
    scheduleReload(200, false);
  });
  // The route only. reload() would fetch the marks as well and then this
  // would fetch them again -- three times over for the deaths, since
  // drawWorldVisible() ends by reclustering them. Invisible while each call
  // costs a tenth of a second; measured on a database ten times the size of
  // routes.db, boot spent 55 seconds in requests, most of it asking twice.
  await reload({ marks: false });
  // Deaths and teleports first: the interior drawings put those marks on
  // their paths, and a list that arrives afterwards leaves the first draw
  // without them until something else happens to redraw it.
  await loadDeaths();
  await loadWarps();
  await loadInteriors();
  await placeYouFromLast();
}

function selectPlane(which) {
  if (state.plane === which) return;
  state.plane = which;
  savePref('plane', which);
  for (const k of ['surface', 'underground']) {
    document.getElementById('plane-' + k).classList.toggle('on', k === which);
  }
  swapTiles(which, document.getElementById('plane-note'));
  // During playback the map belongs to the playback: the committed route is
  // off the map entirely and what you can see is `play.group`. Reloading it
  // would fill a group nobody is looking at and leave the other plane's path
  // drawn over this one.
  if (play.on) { playRedrawPlane(which); return; }
  // The live tail belongs to the plane it was drawn on, and half of it is
  // usually the walk to the lift.
  state.live = [];
  state.liveT = [];
  redrawLive();
  scheduleReload(0);
  loadInteriors();
  loadDeaths();
  loadWarps();
}

// The first time a zoom level is reached, its tiles are not in the browser
// yet -- and with zoomAnimation off the map arrives at the new level at once,
// so what you get is the coarse layer stretched over the whole screen and
// then a snap to sharp. Measured on genuinely cold tiles, three trials each:
// 6, 26 and 26 per cent of the screen with no tile for 45 to 61 ms; with the
// level fetched beforehand, 0 per cent every time.
//
// So the level either side is fetched while nothing is happening. Held in a
// small array because an Image the page has dropped can be collected and
// decoded again, and bounded because the whole point is that this is the
// cheap thing to do when the map is idle -- one screen's worth per level, two
// levels, and never while a gesture is still running.
let warmTimer = null;
let warmHeld = [];

// One screen of tiles around a point, at the level already on screen, asked
// for straight away rather than on the idle timer: this is for the moment
// before the map is sent somewhere, which is exactly when there is no idle.
function warmAhead(xy) {
  if (!tiles || !xy) return;
  const z = tiles._tileZoom;
  if (z === undefined) return;
  const size = tiles.getTileSize().x;
  const b = map.getPixelBounds(toLatLng(xy), z);
  const min = b.min.divideBy(size).floor();
  const max = b.max.divideBy(size).floor();
  const want = [];
  for (let x = min.x; x <= max.x; x++) {
    for (let y = min.y; y <= max.y; y++) {
      want.push(L.Util.template(tiles._url, { z, x, y, s: '' }));
    }
  }
  warmHeld = want.slice(0, 60).map((src) => {
    const im = new Image();
    im.src = src;
    return im;
  });
}

function warmNeighbours() {
  if (!tiles) return;
  clearTimeout(warmTimer);
  warmTimer = setTimeout(() => {
    const z = tiles._tileZoom;
    if (z === undefined) return;
    const size = tiles.getTileSize().x;
    const lo = tiles.options.minNativeZoom ?? 0;
    const hi = tiles.options.maxNativeZoom ?? z;
    const want = [];
    for (const level of [z + 1, z - 1]) {
      if (level < lo || level > hi) continue;
      const b = map.getPixelBounds(map.getCenter(), level);
      const min = b.min.divideBy(size).floor();
      const max = b.max.divideBy(size).floor();
      for (let x = min.x; x <= max.x; x++) {
        for (let y = min.y; y <= max.y; y++) {
          want.push(L.Util.template(tiles._url, { z: level, x, y, s: '' }));
        }
      }
    }
    // A screen is about twenty tiles a level, so this is a ceiling and not a
    // budget: it exists so an odd window size can never turn an idle moment
    // into a hundred requests.
    warmHeld = want.slice(0, 80).map((src) => {
      const im = new Image();
      im.src = src;
      return im;
    });
  }, 400);
}

function swapTiles(plane, note) {
  const v = state.meta.map || {};
  const has = state.meta.underground_tiles;
  if (plane === 'underground' && !has) {
    note.textContent =
      'No underground map has been built yet, so the surface terrain stays ' +
      'up behind the route. tools/build_map.py --map M01 makes one, tiled ' +
      'into viewer/tiles-underground.';
    note.hidden = false;
    return;
  }
  // Nothing to say about a map that is simply there. The note is for the
  // case where the plane you asked for does not exist yet.
  note.textContent = '';
  note.hidden = true;
  if (!tiles || !has) return;
  const url = plane === 'underground'
    ? (v.underground_tile_url || '/tiles-underground/{z}/{x}/{y}.webp')
    : v.tile_url;

  // Both layers are rebuilt rather than pointed at the new URL, because
  // setUrl() goes through GridLayer.redraw(), and in Leaflet 1.9.4 that sets
  // the tile zoom from an unrounded map zoom: at zoomSnap 0.25 the very next
  // request is for /tiles/2.25/1/0.webp, every tile 404s, and the map goes
  // blank -- for whichever plane you switched to, and for the one you switch
  // back to. Building a layer goes through the normal view path, which
  // rounds. The coarse layer moves with it: the underground is a dark render
  // of three regions, and the Lands Between showing behind it reads as the
  // two being drawn on top of each other.
  if (baseTiles) map.removeLayer(baseTiles);
  if (tiles) map.removeLayer(tiles);
  baseTiles = L.tileLayer(url, baseOpts).addTo(map);
  tiles = L.tileLayer(url, tileOpts).addTo(map);
}

// Everything the panel remembers goes through here. The path visuals were
// already stored and the rest were not, which reads as "it forgets my
// settings" -- because the ones you notice resetting are the checkboxes, not
// the line thickness.
function pref(key) {
  try { return localStorage.getItem(`route.${key}`); } catch (e) { return null; }
}

function savePref(key, value) {
  try { localStorage.setItem(`route.${key}`, String(value)); }
  catch (e) { /* private mode: settings just do not persist */ }
}

// Where the map is pointed, remembered across restarts.
//
// A centre and a zoom rather than the bounds: a window that is not the size it
// was last time should keep the middle and the scale it had, not re-fit itself
// to a rectangle and land at some third zoom nobody chose. Three decimals is
// finer than a pixel at any zoom this map has.
function saveView() {
  // Not while the playback is driving. It sweeps the map from one end of the
  // route to the other, and where it happened to stop is not where you were
  // looking -- the view from before you pressed play is.
  if (play && play.on) return;
  if (!map) return;
  const c = map.getCenter();
  savePref('view', `${c.lat.toFixed(3)},${c.lng.toFixed(3)},${map.getZoom()}`);
}

// And read back only if it still means something. A map image of a different
// size, or a zoom ladder that has changed, would otherwise put you off the
// edge of the world at a zoom the map will not hold -- so it is checked
// against the same limits a drag is, and simply ignored when it fails, which
// leaves the old behaviour of fitting the route.
function savedView() {
  const raw = pref('view');
  if (!raw) return null;
  const [lat, lng, zoom] = String(raw).split(',').map(Number);
  if (![lat, lng, zoom].every(Number.isFinite)) return null;
  if (zoom < map.getMinZoom() || zoom > map.getMaxZoom()) return null;
  const centre = L.latLng(lat, lng);
  if (state.panLimits && !state.panLimits.contains(centre)) return null;
  return { centre, zoom };
}

// A checkbox that remembers, and says what to do when it changes.
function rememberToggle(id, key, apply) {
  const box = control(id);
  const saved = pref(key);
  if (saved !== null) box.checked = saved === 'true';
  apply(box.checked, true);
  box.addEventListener('change', () => {
    savePref(key, box.checked);
    apply(box.checked, false);
  });
}

function checkPageBuild() {
  const tag = document.querySelector('meta[name="page-build"]');
  const build = tag ? Number(tag.content) : 0;
  if (build === PAGE_BUILD) return;
  state.stalePage = true;
  showBanner(
    'This page is out of date with its own script: the browser kept an older ' +
    'index.html. Reload with Ctrl-Shift-R (or Cmd-Shift-R) and it will be ' +
    'right. Some controls may be missing until you do.'
  );
}

// Every control this file wires. A missing one is reported once and stubbed
// out, rather than throwing and taking the rest of the wiring -- and the
// route, and the map buttons -- down with it.
const MISSING = {
  addEventListener() {}, setAttribute() {}, removeAttribute() {},
  replaceChildren() {},
  classList: { toggle() {}, add() {}, remove() {} },
  style: { setProperty() {} },
  checked: false, value: 0, hidden: true, disabled: false,
  textContent: '', innerHTML: '', title: '', max: 0, min: 0,
};

function control(id) {
  const el = document.getElementById(id);
  if (el) return el;
  if (!state.stalePage) {
    console.warn(`viewer: index.html has no #${id}, so that control does ` +
                 `nothing. app.js and index.html are out of step.`);
  }
  return MISSING;
}

function showBanner(text) {
  const el = document.createElement('div');
  el.className = 'no-tiles stale';
  el.textContent = text;
  document.getElementById('map').appendChild(el);
}

function showStaleRecorder() {
  // Not the status line: this is the difference between "a feature is broken"
  // and "the thing serving this page is older than the page", and it is worth
  // saying where it cannot be missed.
  showBanner(
    'The running recorder is older than this page, so some of what you see ' +
    'here will not work: dungeons may be missing from the map. Stop it, ' +
    'start it again, then reload.'
  );
}

// The server going away is the failure this page hits most often -- you
// stop the recorder and leave the map open -- and until now it said nothing
// at all: a zoom threw `TypeError: Failed to fetch` into the void, the
// status line still read Live, and a checkbox you had just unticked stayed
// drawn, because the loader threw before it got to clearing anything. The
// page then lies about what is on it.
//
// One place, because a fetch can fail at twenty call sites and they all mean
// the same thing. The banner says which command brings it back.
let lostShown = false;
function lostRecorder() {
  const status = document.getElementById('status');
  if (status) {
    status.textContent = 'Not answering';
    status.dataset.live = '0';
  }
  if (lostShown) return;
  lostShown = true;
  showBanner(
    'The recorder stopped answering, so nothing on this page can be '
    + 'refreshed: what you are looking at is the last thing it sent. Start it '
    + 'again -- Record route.bat, or python -m tracker.main serve -- and '
    + 'reload this page.'
  );
}

// Nothing is allowed to fail silently, including a call site added later that
// forgets to ask. Every rejected fetch lands here whether or not it was
// caught on the way, and the ones that are handled deliberately -- the
// playback's "no recorder: play what is on screen" -- catch their own and
// never reach it.
window.addEventListener('unhandledrejection', (e) => {
  const why = String(e && e.reason);
  if (why.includes('Failed to fetch') || why.includes('NetworkError')) {
    lostRecorder();
    e.preventDefault();
  }
});

let hintShown = false;
function showTileHint() {
  if (hintShown) return;
  hintShown = true;
  const el = document.createElement('div');
  el.className = 'no-tiles';
  el.textContent = 'No map tiles found. Run tools/make_tiles.py on a map image to add the background; the route draws either way.';
  document.getElementById('map').appendChild(el);
}

/* --- data ---------------------------------------------------------------- */

function epsilonForZoom() {
  // Allow about one screen pixel of error. At low zoom that discards most
  // points invisibly; at full zoom it keeps nearly all of them.
  return Math.max(0.4, Math.pow(2, state.nativeZoom - map.getZoom()));
}

// `marks` is false for a reload a zoom asked for: see the zoomend handler.
function scheduleReload(delay = 250, marks = true) {
  clearTimeout(state.reloadTimer);
  // Two reloads can be queued before either runs -- a filter change and then
  // a zoom -- and the second must not drop the marks the first one asked for.
  state.reloadMarks = state.reloadMarks || marks;
  state.reloadTimer = setTimeout(() => {
    const withMarks = state.reloadMarks;
    state.reloadMarks = false;
    reload({ marks: withMarks });
  }, delay);
}

async function reload({ marks = true } = {}) {
  if (align.on) return;
  const layers = [state.plane];

  if (!layers.length) {
    markerGroup.clearLayers();
    routeGroup.clearLayers();
    casingGroup.clearLayers();
    setStats('No path layers shown');
    return;
  }

  const p = new URLSearchParams({
    layers: layers.join(','),
    epsilon: epsilonForZoom().toFixed(3),
  });
  if (state.sessions.size) p.set('sessions', [...state.sessions].join(','));
  if (state.range) { p.set('t0', state.range[0]); p.set('t1', state.range[1]); }

  // Fetch first, clear second. Clearing up front left the map empty for as
  // long as the request took, which reads as a flicker on every zoom.
  const data = await (await fetch('/api/route?' + p)).json();
  // The cave pins are not the route's to take off the map. They were cleared
  // here and put back by the loadInteriors() at the bottom, which is fine
  // while every reload runs both halves -- and leaves the map with no pins at
  // all on a reload that is only about the path.
  if (marks) markerGroup.clearLayers();
  routeGroup.clearLayers();
  casingGroup.clearLayers();
  drawSegments(data.segments);
  state.drawn = data.segments;
  // The last few samples may not have been committed yet, so the live buffer
  // is redrawn on top rather than discarded. Drawing those points twice is
  // invisible; losing them until the next commit is not.
  redrawLive();
  setStats(`${data.points_out.toLocaleString()} of ${data.points_in.toLocaleString()} points drawn`);
  if (!marks) return;
  await loadDeaths();
  await loadWarps();
  // Not guarded on the layer being on. `loadInteriors()` asks that question
  // first and answers it by taking everything off the map -- so guarding it
  // here meant the one function that clears the legacy dungeons was never
  // reached on the reload that turns them off. Measured: unticking Caves and
  // dungeons took all 39 pins off and left every one of the seven castles
  // drawn, which is not what the box says it does.
  loadInteriors();
}

function heightExtent(segments) {
  let lo = Infinity, hi = -Infinity;
  for (const s of segments) for (const h of s.h) { if (h < lo) lo = h; if (h > hi) hi = h; }
  return (lo === Infinity) ? [0, 1] : [lo, hi === lo ? lo + 1 : hi];
}

function drawSegments(segments) {
  const [lo, hi] = heightExtent(segments);
  state.span = timeSpan(segments);
  // Kept so the legend can be rewritten as the ramp slides without going back
  // to the segments for a number that has not changed.
  state.hExtent = [lo, hi];
  showRamp(lo, hi);
  // Whatever was drawn last time went off the map with `routeGroup`.
  state.tinted = [];

  for (const seg of segments) {
    const under = seg.layer === 'underground';
    const base = state.tint === 'solid'
      ? (under ? shade(state.colour, -0.35) : state.colour)
      : (under ? '#6f8fa8' : state.colour);
    const weight = Math.max(0.5, state.weight - (under ? 0.5 : 0));
    const opacity = under ? 0.85 : 0.95;

    if (seg.xy.length > 1 && state.casing > 0) {
      L.polyline(seg.xy.map(toLatLng), {
        renderer,
        color: '#0d0b08',
        weight: weight + state.casing,
        opacity: 0.55,
        lineJoin: 'round',
        lineCap: 'round',
      }).addTo(casingGroup);
    }

    if (state.tint === 'solid') {
      L.polyline(seg.xy.map(toLatLng), {
        renderer, color: base, weight, opacity, lineJoin: 'round',
      }).addTo(routeGroup);
      continue;
    }

    /* Leaflet polylines are one colour each, so a gradient means splitting
       into runs of the same band. 12 bands reads as smooth and keeps the
       object count low. */
    let start = 0;
    for (let i = 1; i <= seg.xy.length; i++) {
      const end = i === seg.xy.length;
      if (end || bandOf(seg, i, lo, hi) !== bandOf(seg, start, lo, hi)) {
        if (i - start > 1) {
          const line = L.polyline(seg.xy.slice(start, i + 1).map(toLatLng), {
            renderer,
            color: bandColor(bandOf(seg, start, lo, hi)),
            weight, opacity, lineJoin: 'round',
          }).addTo(routeGroup);
          // Which moment decided that colour. Under `by age` the answer
          // changes while you play -- see recolourRoute() -- and the only
          // thing needed to work out the new one is the moment itself.
          if (state.tint === 'age') {
            rememberTint(line, seg.t[start]);
          }
        }
        start = i;
      }
    }
  }
}

function timeSpan(segments) {
  let t0 = Infinity, t1 = -Infinity;
  for (const s of segments) {
    for (const t of s.t) { if (t < t0) t0 = t; if (t > t1) t1 = t; }
  }
  return t0 === Infinity ? null : [t0, t1];
}

function showRamp(lo, hi) {
  const ramp = document.getElementById('ramp');
  ramp.hidden = state.tint === 'solid';
  if (ramp.hidden) return;
  const bar = ramp.querySelector('i');
  if (state.tint === 'age') {
    const [t0, t1] = state.span || [Date.now(), Date.now()];
    const w = ageWindow();
    const start = w.cut === null ? t0 : Math.max(t0, w.cut);
    // A horizon of a few hours makes both ends the same date, so the date
    // alone stops telling you anything -- and the clock alone reads as the
    // same evening when the two are a day apart. Which half to show depends
    // on the span, not on a fixed format.
    const sameDay = new Date(start).toDateString() === new Date(t1).toDateString();
    const opts = sameDay ? { hour: '2-digit', minute: '2-digit' }
      : t1 - start < 2 * 864e5
        ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
        : { month: 'short', day: 'numeric' };
    const day = (t) => new Date(t).toLocaleString(undefined, opts);
    document.getElementById('ramp-low').textContent =
      w.cut === null ? day(start) : `${day(start)} and older`;
    document.getElementById('ramp-high').textContent = 'now';
    bar.style.background = `linear-gradient(90deg, ${AGE_STOPS.join(', ')})`;
    ramp.title = `${day(start)} to ${day(t1)}`;
  } else {
    document.getElementById('ramp-low').textContent = `${Math.round(lo)}m`;
    document.getElementById('ramp-high').textContent = `${Math.round(hi)}m`;
    bar.style.background = '';
    ramp.title = '';
  }
}

function shade(colour, amount) {
  const [r, g, b] = hex(colour);
  const f = (v) => Math.round(Math.max(0, Math.min(255,
    amount < 0 ? v * (1 + amount) : v + (255 - v) * amount)));
  return `rgb(${f(r)},${f(g)},${f(b)})`;
}

async function loadInteriors() {
  const seq = ++refreshes.interiors;
  if (!state.layers.interior) {
    markerGroup.clearLayers();
    placedGroup.clearLayers();
    // Nothing is drawn in the open any more, and this is the path that says
    // so: `drawWorldVisible()` is never reached from here, so the set it
    // keeps would still be naming seven castles that have just come off the
    // map. A jump's end that stood down in favour of one of them has to come
    // back, or unticking Caves and dungeons takes marks with it that are not
    // caves and not dungeons.
    state.alwaysDrawn = new Set();
    state.placedByMap.clear();
    loadWarps();
    return;
  }
  const data = await (await fetch('/api/interiors')).json();
  if (seq !== refreshes.interiors) return;
  markerGroup.clearLayers();

  // One marker per dungeon, keyed by its map ID. Grouping by position instead
  // put a second marker on the map every time you went back in from a slightly
  // different spot, each with its own path, for what is one place.
  const byAnchor = new Map();
  for (const v of data.visits.filter(
    (v) => inWindow(v) && onThisPlane(v.plane))) {
    if (!byAnchor.has(v.map_id)) byAnchor.set(v.map_id, []);
    byAnchor.get(v.map_id).push(v);
  }

  buildOffMap(data.unplaced || []);

  for (const group of byAnchor.values()) {
    // A legacy dungeon is drawn out in the open at all times, so its pin
    // is a separate question from a cave's: taking it off leaves the
    // castle and its path exactly where they are. Off by default. The
    // guard is here rather than beside `mark.addTo()` so the second-mouth
    // pins below it go with it -- they are the same place.
    if (group[0].world_visible && !state.layers.legacy) continue;
    // Hovering can only show one visit, so it should be the one worth seeing:
    // stepping in and straight back out leaves a three-second visit on the
    // same dungeon as the hour you actually spent down there. A visit whose
    // entrance was actually recorded outranks one placed from the exit, since
    // the whole group is drawn at the first one's position.
    // The whole group is drawn at the first one's position, so the first one
    // should be at the doorway the route agrees on rather than at whichever
    // visit happens to have walked in first.
    const door = agreedDoor(data.visits, group[0].map_id);
    const atDoor = (v) => (door && v.xy && metresApart(v.xy, door) < SAME_DOOR_M
                           ? 0 : 1);
    group.sort((a, b) => (
      atDoor(a) - atDoor(b)
      || (a.placed === 'entrance' ? 0 : 1) - (b.placed === 'entrance' ? 0 : 1)
      || (b.duration_ms || 0) - (a.duration_ms || 0)
    ));
    const total = group.reduce(
      (t, v) => t + (v.duration_ms ? v.duration_ms / 60000 : 1), 0);
    // Big enough to find on a busy map, and bigger the longer you were down
    // there. The old circle at four pixels across was invisible over terrain.
    const size = Math.round(Math.min(34, 20 + Math.sqrt(total) * 2.4));
    const badge = group.length > 1
      ? `<i class="count">${group.length}</i>` : '';
    // How many times this place has killed you, which is the thing you
    // actually want to know before going back in. Its own badge rather than
    // a number folded into the visit count: they are different facts, and a
    // cave you went into twice and died in nine times should say so.
    const died = deathsInDungeon(group[0].map_id);
    const toll = died
      ? `<i class="deaths" title="${died} death${died > 1 ? 's' : ''} in here"
           >${died}</i>` : '';
    // A pin, not a disc. A 28 px disc covers 450 m of ground at low zoom, so
    // "is that circle on the cave or next to it" has no answer; a tip does.
    // A castle you can see on the map should not wear a cave's icon: the
    // point of the distinction is that one is a place out in the world and
    // the other is a hole in the ground.
    const visible = group[0].world_visible;
    const mark = L.marker(toLatLng(group[0].xy), {
      icon: L.divIcon({
        className: visible ? 'cave-mark keep-mark' : 'cave-mark',
        html: `<span>${visible ? '\u265C' : '\u25B2'}</span>${badge}${toll}`,
        iconSize: [size, size],
        iconAnchor: [size / 2, size + 7],
        popupAnchor: [0, -(size + 7)],
      }),
      riseOnHover: true,
      // Some places cannot be worked out from a route at all -- a Divine
      // Tower reached through Stormveil and left the same way never touches
      // the surface -- so the last word is yours: drag it where it belongs.
      draggable: true,
      autoPan: false,
    });
    mark.on('dragend', () => placeDungeon(group[0], mark));
    mark.bindPopup(interiorPopup(group));
    // Hover shows the path where it happened; clicking pins it so you can
    // reach the marks inside without the overlay vanishing under the cursor.
    mark.on('mouseover', () => showInside(group, false));
    mark.on('mouseout', () => hideInside(false));
    mark.on('click', () => showInside(group, true));
    mark.addTo(markerGroup);

    // A dungeon can have more than one mouth, and the pin stands at the one
    // the route agrees on -- so a cave walked in at one end and out of the
    // other was marked at one end only, with nothing on the map where you
    // came out. Every other mouth gets a pin of its own now. Four dungeons in
    // `routes.db` have two: Stormveil's doors 543 m apart, the Stranded
    // Graveyard's 147, m30_11's 178, and the cave that was reported.
    //
    // Smaller, and without the badges. The visits and the deaths belong to
    // the place and not to a door, so saying them twice a few hundred metres
    // apart would be saying them twice. It is not draggable either: dragging
    // sets where the dungeon *is*, which is one fact about the map, and the
    // main pin is the one that carries it.
    for (const mouth of otherMouths(group, door)) {
      const alt = L.marker(toLatLng(mouth.xy), {
        icon: L.divIcon({
          className: 'cave-mark other-mouth',
          html: `<span>${visible ? '\u265C' : '\u25B2'}</span>`,
          iconSize: [MOUTH_PX, MOUTH_PX],
          iconAnchor: [MOUTH_PX / 2, MOUTH_PX + 6],
          popupAnchor: [0, -(MOUTH_PX + 6)],
        }),
        riseOnHover: true,
        autoPan: false,
      });
      alt.bindPopup(
        `<b>${group[0].label}</b><br>` +
        `<span class="hint">another way in \u2014 ${mouth.n} `
        + `visit${mouth.n === 1 ? '' : 's'} came through here.<br>`
        + `The pin ${Math.round(metresApart(mouth.xy, group[0].xy))} m away `
        + `is the same place.</span>`);
      alt.on('mouseover', () => showInside(group, false));
      alt.on('mouseout', () => hideInside(false));
      alt.on('click', () => showInside(group, true));
      alt.addTo(markerGroup);
    }
  }

  // Everything above is drawn from the one answer already in hand, and only
  // then does this go back to the server. It used to run first, between
  // clearing the markers and putting the new ones up -- and it fetches a path
  // per dungeon, so on this route the pins were off the map for the length of
  // two dozen round trips every time anything asked for a redraw. That is the
  // "cave icons disappear for a little bit": nothing was wrong with them, they
  // were waiting behind somebody else's fetches.
  await learnDungeonFrame(data.visits);
  if (seq !== refreshes.interiors) return;
  drawWorldVisible(data.visits.filter((v) => v.world_visible), seq);
}

// A mark belongs to the map it was made on. Everything recorded so far is
// on the surface, so the underground starts empty rather than showing the
// Lands Between's caves and deaths floating over Siofra.
function onThisPlane(layer) {
  return layer === 'underground'
    ? state.plane === 'underground'
    : state.plane === 'surface';
}

// Which of the two maps a mark belongs on. A dungeon's own layer is
// "interior", which says what kind of place it is and not where its pin
// stands: a cave off Siofra is drawn on the underground, and filing its
// deaths by "interior" put them on the Lands Between. The server works the
// plane out from the visit and sends it; `layer` is the fallback for a
// recorder too old to, where interior has always meant surface.
function markPlane(mark) {
  return onThisPlane(mark.plane || mark.layer);
}

// Deaths recorded inside one dungeon, counted the way everything else on the
// map is: only the ones the time window is showing, so the badge and the
// marks it sits next to can never disagree.
function deathsInDungeon(map_id) {
  return (state.deathList || []).filter(
    (d) => d.map_id === map_id
      && (!state.range || (d.ts >= state.range[0] && d.ts <= state.range[1]))
  ).length;
}

function inWindow(v) {
  // A dungeon belongs to the window if you were in it during the window.
  if (!state.range) return true;
  const start = v.entered_ms;
  const end = v.left_ms || Date.now();
  return end >= state.range[0] && start <= state.range[1];
}

async function drawWorldVisible(visits, seq) {
  placedGroup.clearLayers();
  state.placedByMap.clear();
  // What is actually drawn in the open, which is a question other things ask:
  // a jump's end standing at a castle's pin comes off the world map only
  // because the castle itself is carrying it. Cleared here rather than set
  // below, or unticking Caves and dungeons would leave the old set behind and
  // those marks would vanish along with the drawing that was standing in for
  // them -- which is the trap `hiddenInside()` is written around.
  state.alwaysDrawn = new Set();
  if (!state.layers.interior) return;
  // Every path in here is fetched, so a second call can start while this one
  // is still drawing -- and then both add their layers to the same groups,
  // which showed up as every teleport inside a dungeon drawn twice.
  if (seq !== undefined && seq !== refreshes.interiors) return;
  // Every visit, not just the longest. They are different runs through the
  // same castle and they share one frame now, so drawing them together is the
  // same thing the world route does with several sessions: one path made of
  // everywhere you have been.
  visits = visits.filter((v) => inWindow(v) && onThisPlane(v.plane));
  state.alwaysDrawn = new Set(visits.map((v) => v.map_id));
  for (const v of visits) {
    const d = await fetchInterior(v);
    if (seq !== undefined && seq !== refreshes.interiors) return;
    if (!d.ok || !d.bounds) continue;
    // A group per dungeon, so the live overlay can take one dungeon's
    // permanent drawing off the map while it draws the same place itself.
    let layer = state.placedByMap.get(v.map_id);
    if (!layer) {
      layer = L.layerGroup().addTo(placedGroup);
      state.placedByMap.set(v.map_id, layer);
    }
    drawInteriorInto(layer, v, d, renderer, { permanent: true });
  }
  // The entrance marks were clustered before this list was known -- and so
  // were the ends of every jump that stands at one of these castles rather
  // than where it happened inside it, which is a thing only `alwaysDrawn`
  // can say and it was empty until the line above.
  loadDeaths();
  loadWarps();
}

/* --- interiors drawn where they happened ---------------------------------

   A dungeon has its own coordinate space, so its path cannot be projected
   onto the world plane -- that is the bug this whole project exists to fix.
   What it can do is be drawn at the entrance, at the same metres-per-pixel as
   the map around it, so the size and shape of the place read against the
   terrain you walked in from. Only the orientation is unknowable, because the
   dungeon's axes have no fixed relationship to the world's; the caption says
   so rather than letting you assume otherwise.
   ------------------------------------------------------------------------ */

const inside = { key: null, pinned: false, cache: new Map(), timer: null,
                 last: null,
                 // The extent of what is drawn, in map pixels. Hovering a pin
                 // puts a cave on screen and then the cursor has to be able
                 // to get to it, which it could not: leaving the pin started
                 // the fuse whatever the cursor did next, so moving towards
                 // the thing that had just appeared was what took it away.
                 box: null,
                 live: false };

// How far outside the drawn extent still counts as being in it, in screen
// pixels. The box is in map pixels, so this is divided by the zoom scale --
// otherwise at the overview zoom a cave is thirty pixels across and the
// region around it is unhittable.
const INSIDE_REACH_PX = 30;

function insideKey(v) { return `${v.map_id}:${v.entered_ms}`; }

async function fetchInterior(v) {
  const key = insideKey(v);
  if (inside.cache.has(key)) return inside.cache.get(key);
  const p = new URLSearchParams({ map_id: v.map_id, t0: v.entered_ms });
  if (v.left_ms) p.set('t1', v.left_ms);
  const res = await fetch('/api/interior?' + p);
  const d = await res.json();
  inside.cache.set(key, d);
  return d;
}

async function showInside(group, pin, only) {
  clearTimeout(inside.timer);
  inside.timer = null;
  const v = only || group[0];
  if (inside.pinned && !pin && inside.key !== insideKey(v)) return;
  inside.pinned = pin || inside.pinned;

  // Hovering a marker means "show me this place", and one cave visited six
  // times is one place: drawing only the first left five runs invisible
  // unless you found the Show path button for each. Every visit in the group
  // is drawn, in the dungeon's own frame, so they overlay the way the
  // permanent drawings of a legacy dungeon do. Show path is the exception --
  // it asks for one visit and gets one.
  const wanted = only ? [v] : group;
  const drawn = [];
  for (const each of wanted) {
    const d = await fetchInterior(each);
    if (d.ok && d.bounds) drawn.push({ v: each, d });
  }
  if (!drawn.length) return;
  inside.key = insideKey(v);
  inside.last = { group, only };
  drawInside(drawn, group, !!only);
}

function hideInside(force) {
  if (inside.pinned && !force) return;
  clearTimeout(inside.timer);
  inside.timer = setTimeout(() => {
    // Nulled as it fires, so `insideHover()` can tell a fuse that is already
    // burning from one it has to light. Without that, a cursor moving away
    // from the drawing restarted the timer on every mouse move and the fuse
    // never reached the end.
    inside.timer = null;
    insideGroup.clearLayers();
    insideLiveGroup.clearLayers();
    state.liveInsideLine = null;
    inside.key = null;
    inside.transform = null;
    inside.box = null;
    inside.pinned = false;
    dimBackground(false);
    setCaption(null);
  }, force ? 0 : 140);
}

// The drawing is a region you can put the cursor in, not a thing you have to
// keep pointing at a pin to see. Inside it the pending hide is put out;
// outside it, one is lit.
//
// Hit-tested here rather than with a transparent rectangle on the map, for
// the reason the path hover already is: the interior pane is
// `pointer-events: none`, because a full-map canvas that takes the pointer
// swallows every click on the map.
function insideHover(latlng) {
  if (!inside.box || inside.pinned || !inside.key) return;
  const p = map.project(latlng, state.nativeZoom);
  const near = INSIDE_REACH_PX / Math.pow(2, map.getZoom() - state.nativeZoom);
  const b = inside.box;
  const over = p.x >= b[0] - near && p.x <= b[2] + near
            && p.y >= b[1] - near && p.y <= b[3] + near;
  if (over) { clearTimeout(inside.timer); inside.timer = null; }
  else if (!inside.timer) hideInside(false);
}

function dimBackground(on) {
  // The route and the marks go right down; the terrain only part way, because
  // where the dungeon sits in the world is half of what the drawing is for.
  // Every pane a mark can be drawn into, and the list has to be kept beside
  // the panes themselves: `respawns` was created after this was written and
  // never added, so hovering a cave dimmed the world and left 35 respawn
  // marks standing over it at full strength.
  for (const name of ['overlayPane', 'markerPane', 'deaths', 'respawns',
                      'warps', 'playmarks']) {
    const pane = map.getPane(name);
    if (pane) pane.style.opacity = on ? '0.18' : '';
  }
  state.dimmed = on;
  applyDim();
}

function interiorTransform(v, d) {
  // Metres to pixels, the same conversion the world route uses, so a 200 m
  // cave is 200 m of map.
  //
  // What that scale hangs off is the part that has to be right. A visit does
  // not begin at the door: warp into a castle and the first thing recorded is
  // the grace you appeared at, a few hundred metres inside. Pinning each
  // visit's own first point to the entrance marker therefore slid the whole
  // drawing by the distance between that grace and the door -- the path
  // looked like it was setting off from the entrance while you were standing
  // somewhere else entirely.
  //
  // So the frame belongs to the dungeon: one pair of (local origin, map
  // position) taken from the visit that actually walked in, and every visit
  // to that dungeon drawn in it.
  // A position set by hand is the last word, and the way it gets the last
  // word is that `learnDungeonFrame()` builds that dungeon's frame out of it
  // and lets nothing else touch the map -- so there is one frame here and
  // every visit is drawn in it, hand-placed or not. Asking the tier instead
  // and centring on `d.bounds` below meant a hand-placed dungeon had no
  // frame at all: one drawing per run, each centred on its own extent, so
  // they did not agree with each other and the one still being walked moved
  // under the player. See the note in `learnDungeonFrame()` for the numbers.
  const frame = state.dungeonFrame.get(v.map_id);
  if (frame) return frameTransform(frame);

  if (v.placed === 'by hand' || v.placed === 'the way in') {
    // Placed at the dungeon it opens off, which is a neighbourhood rather
    // than a doorway. Pinning the first step to that doorway drew the tower
    // setting off out of the castle's front door, which claims a continuity
    // that is not there -- so it is centred on the anchor instead, which says
    // "somewhere around here" and nothing more.
    const b = d.bounds;
    return frameTransform({
      local: [(b.x0 + b.x1) / 2, (b.z0 + b.z1) / 2], xy: v.xy,
    });
  }
  return frameTransform({ local: d.segments[0].xy[0], xy: v.xy });
}

async function nameDungeon(map_id, name) {
  const clean = String(name || '').trim();
  try {
    const res = await fetch('/api/name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(clean ? { map_id, name: clean }
                                 : { map_id, clear: true }),
    });
    const d = await res.json();
    if (!res.ok || !d.ok) {
      setStats(d.error || 'The recorder would not save that name.');
      return;
    }
  } catch (e) {
    setStats('Could not reach the recorder to save that name.');
    return;
  }
  setStats(clean ? `Named "${clean}".` : 'Name removed.');
  // The name is on the marker, in its popup, on every mark inside it and in
  // the status line, so everything that draws a label is rebuilt.
  await loadInteriors();
  await loadDeaths();
  await loadWarps();
}

async function placeDungeon(v, mark) {
  const pr = state.meta.projection;
  const px = map.project(mark.getLatLng(), state.nativeZoom);
  // Back to world metres, which is what the recorder stores: pixels are a
  // property of whichever map image happens to be tiled.
  const wx = (px.x - pr.offset_x) / pr.scale_x;
  const wz = (px.y - pr.offset_y) / pr.scale_y;
  try {
    const res = await fetch('/api/place', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ map_id: v.map_id, wx, wz }),
    });
    const d = await res.json();
    if (!res.ok || !d.ok) {
      setStats(d.error || 'The recorder would not save that position.');
      return;
    }
  } catch (e) {
    setStats('Could not reach the recorder to save that position.');
    return;
  }
  // Its drawing hangs off the marker, so both are rebuilt from the new place.
  state.dungeonFrame.delete(v.map_id);
  await afterPlacing();
  setStats(`${v.label} placed by hand. Drag it again to correct it.`);
}

function frameTransform(frame) {
  const pr = state.meta.projection;
  const turn = frame.turn || 0;
  if (!turn) {
    return (x, z) => [
      frame.xy[0] + (x - frame.local[0]) * pr.scale_x,
      frame.xy[1] + (z - frame.local[1]) * pr.scale_y,
    ];
  }
  // Turned about the doorway it is pinned at. The rotation is worked out in
  // metres and only then projected, because the two axes of the projection
  // need not have the same scale and rotating in pixels would shear the
  // dungeon.
  const cos = Math.cos(turn), sin = Math.sin(turn);
  return (x, z) => {
    const dx = x - frame.local[0], dz = z - frame.local[1];
    return [
      frame.xy[0] + (dx * cos - dz * sin) * pr.scale_x,
      frame.xy[1] + (dx * sin + dz * cos) * pr.scale_y,
    ];
  };
}

// How near two anchors have to be to count as the same doorway. Metres, not
// pixels, so it does not change meaning with the map image.
const SAME_DOOR_M = 15;

function metresApart(a, b) {
  const pr = state.meta.projection;
  return Math.hypot((a[0] - b[0]) / pr.scale_x, (a[1] - b[1]) / pr.scale_y);
}

// Which of a dungeon's doorways the route agrees on.
//
// A dungeon with two mouths gets an anchor at whichever one each visit
// happened to use, and the frame was taken from the first visit that walked
// in -- which can be the mouth used once, against another used four times.
// Sellia Crystal Tunnel is exactly that: one visit in at (12604, 10026), and
// four at (12563, 10102), with the whole tunnel drawn from the first.
//
// So they are counted. The most-used doorway is where the dungeon is drawn
// from, and the marker goes there too. It is a count and not a medoid because
// the question is not "where is the middle of these" -- two real mouths have
// no meaningful middle -- but "which of these doors is the one you use".
// How wide a second pin is. Smaller than the main one, which is 20 to 34 px
// by how long you have spent in the place: this one is a door and not the
// place, so it should read as the lesser of the two.
const MOUTH_PX = 17;

// The mouths of a dungeon that are not the one its pin stands at. Clustered
// at the same tolerance the door vote uses, so several readings of one doorway
// stay one doorway and only a genuinely different way in earns a pin.
function otherMouths(group, door) {
  const out = [];
  const at = door || (group[0] && group[0].xy);
  if (!at) return out;
  // How big the place is, in its own metres. Two mouths of one dungeon cannot
  // be further apart than the dungeon is -- which is the test that already
  // tells a door from a gate in `warps()` and stops the entrance vote
  // overwriting a real second mouth. This was the third question of that shape
  // and the only one that was not asking it.
  //
  // Reported from the field: "I teleported from the overworld into a cave, and
  // it created an exit icon for the cave in the overworld where I teleported
  // from." The anchor behind that pin was 504 m from the real door of a
  // catacomb 169 m across, so it was never a mouth whatever else it was. On
  // routes.db the rule drops that one and m30_11's, whose two anchors are
  // 178 m apart on the map for a place 147 m across while the points just
  // inside them are 15 m apart -- which cannot be two doors either. The three
  // real second mouths all pass: 147 m in a place 234 across, 543 in 654,
  // 94 in 134.
  //
  // Nothing is dropped for want of an extent. A dungeon with no samples
  // recorded inside says nothing about its own size, and the rule is the one
  // the placement tiers use: drop an anchor against evidence, never for the
  // lack of it.
  const room = group.reduce((m, v) => Math.max(m, v.extent_m || 0), 0);
  for (const v of group) {
    if (!v.xy) continue;
    const off = metresApart(v.xy, at);
    if (off < SAME_DOOR_M) continue;
    if (room && off > room) continue;
    const near = out.find((m) => metresApart(m.xy, v.xy) < SAME_DOOR_M);
    if (near) near.n += 1;
    else out.push({ xy: v.xy, n: 1 });
  }
  return out;
}

// `doorOf` is how to read a visit's doorway. It defaults to where the visit
// is drawn, which is the same thing everywhere except a dungeon placed by
// hand -- there every visit carries the one position you gave, and the route's
// own doorways are the thing being voted on.
function agreedDoor(visits, map_id, doorOf) {
  const at = doorOf || ((v) => v.xy);
  const here = visits.filter((v) => v.map_id === map_id && at(v));
  // Weighted by the time spent inside on each visit, not by the number of
  // visits. Reported from the field: a cave walked through in one door and
  // out the other, then re-entered twice at the far mouth for 25 and 34
  // seconds to look at something -- which outvoted the 283-second traversal
  // two to one and moved the pin, and the drawing with it, to a door that
  // had barely been used. Time asks the better question: which mouth did you
  // do this place through.
  //
  // It keeps every case counting got right. Measured over routes.db, of the
  // five dungeons whose visits disagree about the door, count and time pick
  // the same one in all five -- Sellia Crystal Tunnel is 4 visits and 1,914 s
  // at one mouth against 1 visit and 30 s at the other, and agrees either way.
  const weight = (v) => Math.max(1, (v.duration_ms || 0) / 1000);
  let best = null, bestW = 0;
  for (const a of here) {
    const w = here.filter((b) => metresApart(at(a), at(b)) < SAME_DOOR_M)
                  .reduce((sum, b) => sum + weight(b), 0);
    if (w > bestW) { bestW = w; best = at(a); }
  }
  return best;
}

// How far a dungeon has to be turned for its second doorway to land on the
// second doorway.
//
// A frame pins one point inside a dungeon to one point on the map, which
// lines the place up at that door and nowhere else -- the orientation of a
// dungeon's own axes has no fixed relation to the world's. Two doors give it:
// the angle between the line joining them inside and the line joining them
// out on the map.
//
// It is only taken when the two lines are the same length, and that check
// does most of the work here. Measured over `routes.db`: Stormveil's two
// doors are 543.4 m apart on the map and 542.8 m apart inside, and m18's are
// 147.2 and 140.8 -- both a rigid turn away from each other. The other two
// dungeons with two known doors are 178.3 against 15.2 and 86.1 against 13.5,
// which is not an orientation problem at all: those interiors are simply not
// laid out to match the ground above them, and no turn will ever make both
// mouths land. Those keep the frame they had and stay right at one door.
const TURN_MIN_M = 25;          // too short a baseline and the angle is noise
const TURN_TOLERANCE = 0.12;    // how far the two lengths may disagree

function frameTurn(frame, other, pr) {
  const lx = other.local[0] - frame.local[0];
  const lz = other.local[1] - frame.local[1];
  const wx = (other.xy[0] - frame.xy[0]) / pr.scale_x;
  const wz = (other.xy[1] - frame.xy[1]) / pr.scale_y;
  const inside = Math.hypot(lx, lz), above = Math.hypot(wx, wz);
  if (inside < TURN_MIN_M || above < TURN_MIN_M) return 0;
  if (Math.abs(inside - above) > TURN_TOLERANCE * above) return 0;
  return Math.atan2(wz, wx) - Math.atan2(lz, lx);
}

async function learnDungeonFrame(visits) {
  // A frame needs one point tied to one map position. Three kinds of visit
  // provide that: walking in ties the anchor to the first step inside, coming
  // through from another dungeon does the same, and walking out ties it to
  // the last step before the door. In that order, because that is the order
  // of how directly each was measured.
  const byHand = new Set(visits.filter((v) => v.placed === 'by hand')
                               .map((v) => v.map_id));
  // What the *route* says a visit's doorway is, which for a dungeon somebody
  // has placed by hand is not what `xy` and `placed` say: those carry the
  // hand position, the same one for every visit to that map. The frame is
  // learned from the route either way and only its position on the map comes
  // from the hand -- see the end of this function.
  const doorOf = (v) => (v.door_xy ? v.door_xy : v.xy);
  const tierOf = (v) => (v.door_xy ? v.door_placed : v.placed);
  // Every doorway anybody walked through, as a pair: where it is inside, and
  // where it is on the map.
  const doors = new Map();
  // The same pairs, kept whole rather than deduped, because which one the
  // dungeon hangs off is decided below by how many of them agree.
  const cand = new Map();
  for (const want of ['entrance', 'the doorway', 'exit']) {
    for (const v of visits) {
      const door = doorOf(v);
      if (!door || tierOf(v) !== want) continue;
      const d = await fetchInterior(v);
      if (!d.ok || !d.segments.length) continue;
      const seg = want === 'exit' ? d.segments[d.segments.length - 1]
                                  : d.segments[0];
      const local = want === 'exit' ? seg.xy[seg.xy.length - 1] : seg.xy[0];
      if (!doors.has(v.map_id)) doors.set(v.map_id, []);
      const known = doors.get(v.map_id);
      // Two readings of one doorway are one doorway -- and at the same
      // tolerance the door vote and the second-mouth pins use, in metres.
      // This was a bare hypot over map pixels against 10, which agrees with
      // 15 m only because this map happens to be about a pixel to the metre;
      // tile a map at another scale and the three tests would disagree about
      // what counts as one door.
      if (!known.some((k) => metresApart(k.xy, door) < SAME_DOOR_M)) {
        known.push({ local, xy: door });
      }
      // Kept as a candidate. Which of them the dungeon is hung off is
      // settled after every tier has been walked, because the answer is a
      // property of the set and not of the order they arrive in.
      if (!cand.has(v.map_id)) cand.set(v.map_id, []);
      const mine = cand.get(v.map_id);
      // The best tier that offers anything, and only that one: the order of
      // the loop is the order of how directly each was measured.
      if (!mine.length || mine[0].tier === want) {
        mine.push({ local, xy: door, tier: want,
                    secs: Math.max(1, (v.duration_ms || 0) / 1000) });
      }
    }
  }
  const pr = state.meta.projection;
  // Which reading of the doorway the dungeon hangs off. Not the first of the
  // best tier, which is the order the visits happen to come in, and not the
  // longest visit either -- what settles it is how many of the readings agree
  // with each other.
  //
  // They are compared by the frame each *implies*: where the dungeon's own
  // origin lands, which is the door's map position minus its local one. Two
  // readings of one doorway imply the same origin to within sampling noise,
  // and so do two real mouths of a dungeon whose inside is laid out to match
  // the ground above it -- which is what makes this the right thing to count.
  // A reading that implies a different origin is a bad reading whatever it
  // looks like from outside.
  //
  // Reported: a cave whose marker sat 13 m past its own mouth once the path
  // had been dragged into line. Its doorway had been read five times: three
  // in quarter-second capture agreeing to a metre, and one in a five-second
  // import that landed 18.6 m short of the door on the surface and 10.2 m
  // past it inside -- which is one doorway sampled coarsely, not two
  // doorways. That reading won because the visit behind it lasted 405
  // seconds against the others' 231, so the whole cave hung off the worst
  // measurement of it.
  //
  // Measured over the 14 dungeons in `routes.db` with more than one reading:
  // two change. m31_15's frame goes from being exact at the coarse reading
  // and 28-29 m out at the two good ones to exact at both good ones; m18_00
  // swaps between two readings 10 m apart, which is neither better nor
  // worse. The other twelve are untouched, and no dungeon's worst door error
  // rises.
  const originOf = (c) => [c.xy[0] - c.local[0] * state.meta.projection.scale_x,
                           c.xy[1] - c.local[1] * state.meta.projection.scale_y];
  for (const [map_id, list] of cand) {
    let best = null, bestN = 0, bestT = 0;
    for (const c of list) {
      const agree = list.filter(
        (o) => metresApart(originOf(c), originOf(o)) < SAME_DOOR_M);
      const secs = agree.reduce((sum, o) => sum + o.secs, 0);
      // Time only breaks a tie. It is the right question for *which mouth*
      // you use a place through and the wrong one for which reading of a
      // mouth to trust, which is what this is.
      if (agree.length > bestN || (agree.length === bestN && secs > bestT)) {
        bestN = agree.length; bestT = secs; best = c;
      }
    }
    if (best) state.dungeonFrame.set(map_id, { local: best.local, xy: best.xy });
  }
  for (const [map_id, known] of doors) {
    const frame = state.dungeonFrame.get(map_id);
    if (!frame || known.length < 2) continue;
    for (const other of known) {
      const turn = frameTurn(frame, other, pr);
      if (turn) { frame.turn = turn; break; }
    }
  }
  // A dungeon somebody placed by hand gets a frame like every other one,
  // built from the position they gave it rather than from a doorway the
  // route thinks it knows. Last, so it overwrites anything the loop above
  // learned: drag a dungeon that already had a door frame and the drag has
  // to win, which is the whole of what the tier is for.
  //
  // Centred on the position, and the word carrying the weight is *the
  // dungeon's* extent. Centring each drawing on its own extent -- which is
  // what this did, from `d.bounds` inside `interiorTransform()` -- draws
  // every run through the place somewhere different, and makes the run you
  // are walking crawl, because its bounds grow with every step you take.
  // Measured on the Chapel of Anticipation: three runs put the same point
  // inside it 25, 108 and 126 m apart on the map, and the run in progress
  // slid 100 m across Liurnia over the five minutes it lasted, in jumps of
  // up to 66 m. Which is what "it doesn't match up with where I am on the
  // map" was.
  //
  // The extent therefore comes from the runs that have *finished*. How big a
  // place is is a fact about the place, and a run you are in the middle of
  // has not finished saying; the first time you ever go somewhere that run
  // is all there is, and then it is what gets used.
  for (const map_id of byHand) {
    const mine = visits.filter((v) => v.map_id === map_id && v.xy);
    const frame = state.dungeonFrame.get(map_id);
    if (frame && mine.length) {
      // The route knows a doorway here, and the pin has been standing at it.
      // Dragging the pin therefore says where that doorway is and nothing
      // else: the point inside it ties to, and the turn measured from a
      // second door, are the route's and are not the drag's to change. So the
      // drawing moves exactly as far as the marker did.
      frame.xy = mine[0].xy;
      continue;
    }
    const done = mine.filter((v) => v.left_ms);
    let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    for (const v of (done.length ? done : mine)) {
      const d = await fetchInterior(v);
      if (!d.ok || !d.bounds) continue;
      x0 = Math.min(x0, d.bounds.x0); x1 = Math.max(x1, d.bounds.x1);
      z0 = Math.min(z0, d.bounds.z0); z1 = Math.max(z1, d.bounds.z1);
    }
    // Nothing recorded anywhere in there. Better no frame than a stale one
    // from before the drag, which would move the pin and leave the path.
    if (x0 === Infinity) { state.dungeonFrame.delete(map_id); continue; }
    state.dungeonFrame.set(map_id, {
      local: [(x0 + x1) / 2, (z0 + z1) / 2], xy: mine[0].xy,
    });
  }
}

function drawInteriorInto(group, v, d, lineRenderer, opts) {
  // The permanent drawings are part of the route, not an annotation on it, so
  // they are drawn at the same weight as the surface path. Only the hover
  // overlay is heavier, because it is deliberately the loud one.
  const faint = opts && opts.faint;
  // Dashed when we only know roughly where this is: the shape is real, the
  // place it sits is a neighbour's.
  // Dashed only while nobody knows where this is. Once you have said, it is
  // as solid as anywhere you walked into.
  const approx = v.placed === 'the way in'
    && !state.dungeonFrame.has(v.map_id);
  const dash = approx ? '7,5' : null;
  const at = interiorTransform(v, d);
  const b = d.bounds;
  const lo = b.y0, hi = b.y1 > b.y0 ? b.y1 : b.y0 + 1;
  // A legacy dungeon's own deaths, respawns and teleports are drawn on the
  // map for good, so they dim with the world like every other mark; the ones
  // belonging to the dungeon being hovered are the thing being revealed and
  // must not. That was decided by comparing the group against `placedGroup`
  // -- and then each dungeon was given a group of its own *inside* it, so the
  // comparison quietly became false for every one of them and 51 marks sat
  // over four dimmed castles. Asked outright now, because the caller is the
  // only one that knows which kind of drawing it wants.
  const markPane = opts && opts.permanent ? 'deaths' : 'insideMarks';

  for (const seg of d.segments) {
    if (seg.xy.length < 2) continue;
    const pts = seg.xy.map(([x, z]) => toLatLng(at(x, z)));
    L.polyline(pts, {
      renderer: lineRenderer, color: '#0d0b08',
      weight: state.weight + Math.max(state.casing, faint ? 3 : 0),
      opacity: faint ? 0.8 : 0.55,
      lineJoin: 'round', lineCap: 'round',
      // The casing has to break where the line does, or a dashed path sits on
      // a solid dark one and only looks smudged.
      dashArray: dash,
    }).addTo(group);
    if (state.tint === 'solid') {
      L.polyline(pts, {
        renderer: lineRenderer,
        color: faint ? shade(state.colour, 0.25) : state.colour,
        weight: state.weight, opacity: faint ? 1 : 0.95, lineJoin: 'round',
        dashArray: dash,
      }).addTo(group);
      continue;
    }
    // Runs of one band, not one polyline per pair of points. A legacy dungeon
    // is thousands of points and these are drawn permanently now, so a line
    // each would put thousands of layers on the map for one castle.
    let start = 0;
    for (let i = 1; i <= pts.length; i++) {
      const end = i === pts.length;
      if (end || bandOf(seg, i, lo, hi) !== bandOf(seg, start, lo, hi)) {
        if (i - start > 1) {
          const band = L.polyline(pts.slice(start, i + 1), {
            renderer: lineRenderer,
            color: bandColor(bandOf(seg, start, lo, hi)),
            weight: state.weight, opacity: faint ? 1 : 0.95,
            lineJoin: 'round', dashArray: dash,
          }).addTo(group);
          // A castle drawn in the open is part of the route and ages with it.
          // Only the permanent drawings: the hover overlay is put up and taken
          // down constantly, and registering that would be churn for a picture
          // that is redrawn whenever it matters anyway.
          if (state.tint === 'age' && opts && opts.permanent) {
            rememberTint(band, seg.t[start]);
          }
        }
        start = i;
      }
    }
  }

  // Lifts and teleporters inside the place. They have no world position --
  // nothing in a dungeon does -- so this drawing is the only frame they can
  // be shown in, and without it a jump inside a castle was invisible.
  //
  // Gated on the toggle like the deaths and respawns below: these are drawn
  // into the dungeon's own pane rather than into warpGroup, so unticking
  // Teleports emptied that group and left eight of them on the castles.
  for (const { w: jump, ends: here } of (state.warps ? warpsInside(v) : [])) {
    const spots = here.map((which) => {
      const local = which === 'to' ? jump.local : jump.from_local;
      return [which, at(local[0], local[1]),
              which === 'to' ? '\u2726' : '\u2727'];
    });
    // Where it goes, in whatever frame each end is drawn in: this dungeon's
    // for an end inside it, and the world's for one out on the surface, which
    // is the same pixels once both are projected. Said before that there was
    // "nothing here to join it to" for a gate, which was wrong -- the surface
    // end is a real recorded position, not a made-up one -- so those marks
    // pointed nowhere at all.
    const endAt = (which) => {
      if (here.includes(which)) {
        const local = which === 'to' ? jump.local : jump.from_local;
        return at(local[0], local[1]);
      }
      return which === 'to' ? jump.xy : jump.from_xy;
    };
    const pair = [endAt('from'), endAt('to')];
    // A legacy dungeon is drawn on the map at all times, so a line standing
    // between two of its rooms for ever is one more thing on a busy map that
    // nobody asked for: those answer on hover, like the world's do. A cave's
    // drawing only exists while you are looking at it, and looking at it *is*
    // the asking -- so in there the line is part of the drawing.
    const always = !(opts && opts.permanent);
    if (always && pair[0] && pair[1]) {
      L.polyline([toLatLng(pair[0]), toLatLng(pair[1])], {
        renderer: lineRenderer,
        color: '#8fb7cc', weight: 2, opacity: 0.75, dashArray: '5,7',
      }).addTo(group);
    }
    for (const [which, pt, glyph] of spots) {
      const mark = L.marker(toLatLng(pt), {
        pane: markPane,
        icon: L.divIcon({
          className: which === 'to' ? 'warp-mark' : 'warp-mark warp-from',
          html: `<span>${glyph}</span>`,
          iconSize: [18, 18], iconAnchor: [9, 9],
        }),
        keyboard: false,
      })
        // Inside a dungeon is where the ambiguity actually lives: no HP
        // reading survives in old routes, so a lift, a teleporter and a death
        // all look the same. This is where you say which.
        .bindPopup(popupWithAction(
          `<b>${which === 'to' ? 'Arrived here' : 'Left from here'}</b><br>` +
          `Inside ${v.label} (${v.map})<br>${jump.distance_m} m<br>` +
          `${new Date(jump.ts).toLocaleString()}`,
          'This was a death',
          () => { map.closePopup(); callDeath({ ts: jump.ts, from_ts: jump.from_ts }); }))
        .addTo(group);
      // The same answer the world's teleport marks give, which these did not.
      // Drawn on the hover rather than left standing: a legacy dungeon is on
      // the map at all times, so a permanent line meant its jumps were always
      // announcing where they went while a cave's said nothing until you
      // asked. Reported as both halves of that at once.
      if (!always) {
        mark.on('mouseover', () => showWarpLines([pair], group, lineRenderer));
        mark.on('mouseout', hideWarpLines);
      }
    }
  }

  // Deaths that happened in there, put where they happened rather than left
  // stacked on the entrance. Gated on the toggle like every other death mark:
  // these are drawn into the dungeon's own pane, so turning deaths off
  // emptied deathGroup and left six of them sitting on the castles.
  for (const dead of (state.deaths ? deathsInside(v) : [])) {
    let pt = null;
    if (dead.local) {
      pt = at(dead.local[0], dead.local[1]);
    } else {
      // Recorded before local coordinates were kept: fall back to the point
      // on the path closest in time, which is where you were when it landed.
      const near = nearestInTime(d, dead.ts);
      if (near) pt = at(near[0], near[1]);
    }
    if (!pt) continue;
    L.marker(toLatLng(pt), {
      pane: markPane,
      icon: L.divIcon({
        className: 'death-mark', html: '<span>\u2715</span>',
        iconSize: [18, 18], iconAnchor: [9, 9],
      }),
    })
      .bindPopup(dead.by_hand
        ? popupWithAction(
            `<b>Died</b><br>Inside ${v.label} (${v.map})<br>` +
            `${new Date(dead.ts).toLocaleString()}` +
            `<br><span class="hint">You marked this one.</span>`,
            'Not a death after all',
            () => { map.closePopup();
                    callDeath({ ts: dead.ts, clear: true }); })
        : `<b>Died</b><br>Inside ${v.label} (${v.map})<br>` +
          `${new Date(dead.ts).toLocaleString()}`)
      .addTo(group);
  }

  // And where you got up again, when that was in here too.
  if (state.respawns) {
    for (const back of respawnsInside(v)) {
      const pt = at(back.local[0], back.local[1]);
      L.marker(toLatLng(pt), {
        pane: markPane,
        icon: L.divIcon({
          className: 'respawn-mark', html: '<span>\u2739</span>',
          iconSize: [18, 18], iconAnchor: [9, 9],
        }),
      })
        .bindPopup(`<b>Got up here</b><br>Inside ${v.label} (${v.map})<br>` +
                   `${back.after_s}s after dying<br>` +
                   `${new Date(back.ts).toLocaleString()}`)
        .addTo(group);
    }
  }
  return at;
}

// The extent of what has just been drawn, with a little air around it, in
// map pixels. Padded in map units rather than in screen pixels so the frame
// drawn from it does not have to be redrawn on every zoom -- the hit test
// adds its own screen-pixel slack on top, which is what makes a small cave
// reachable at a coarse zoom.
function insideBox(corners) {
  if (!corners.length) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const q of corners) {
    if (!q) continue;
    x0 = Math.min(x0, q[0]); x1 = Math.max(x1, q[0]);
    y0 = Math.min(y0, q[1]); y1 = Math.max(y1, q[1]);
  }
  if (!Number.isFinite(x0)) return null;
  const air = Math.max(22, (x1 - x0 + y1 - y0) * 0.04);
  return [x0 - air, y0 - air, x1 + air, y1 + air];
}

function drawInside(drawn, group, single) {
  insideGroup.clearLayers();
  // Whatever the frame is now, the tail belongs in it.
  setTimeout(redrawLiveInside, 0);
  const { v, d } = drawn[0];
  // Dimming exists to stop a cave's path being lost in the route around it.
  // A legacy dungeon is already drawn out there in the open, so hovering one
  // does not turn the lights off for nothing. Asking for a single run of it
  // is different: that is a deliberate "show me this one", and everything
  // else on the map is what it has to be picked out from.
  dimBackground(!v.world_visible || (single && inside.pinned));
  // The permanent drawing stays put underneath. Taking it off the map while
  // this one was up meant every other run through the castle vanished the
  // moment you walked back in, and came back when you hovered the entrance.

  // Kept so the live position can be placed on this same drawing: inside a
  // dungeon these local metres are the only position there is.
  const corners = [];
  const keep = (at, bounds, anchor) => {
    if (bounds) {
      for (const x of [bounds.x0, bounds.x1]) {
        for (const z of [bounds.z0, bounds.z1]) corners.push(at(x, z));
      }
    }
    // The pin too. It stands at the doorway, which a hand placement can put
    // outside the path's own extent -- and a region that does not contain the
    // thing you hovered to open it would close the moment you set off.
    if (anchor) corners.push(anchor);
  };
  const at0 = drawInteriorInto(insideGroup, v, d, insideRenderer,
                               { faint: true });
  inside.transform = { map_id: v.map_id, at: at0 };
  keep(at0, d.bounds, v.xy);
  for (const other of drawn.slice(1)) {
    const at = drawInteriorInto(insideGroup, other.v, other.d, insideRenderer,
                                { faint: true });
    keep(at, other.d.bounds, other.v.xy);
  }
  inside.box = insideBox(corners);

  if (inside.box && !inside.pinned) {
    const [x0, y0, x1, y1] = inside.box;
    L.rectangle([toLatLng([x0, y0]), toLatLng([x1, y1])], {
      renderer: insideRenderer,
      color: 'rgba(224, 163, 60, 0.45)', weight: 1, dashArray: '4,5',
      fill: true, fillColor: '#e0a33c', fillOpacity: 0.03,
      interactive: false,
    }).addTo(insideGroup);
  }

  const b = d.bounds;
  const w = Math.round(b.x1 - b.x0), h = Math.round(b.z1 - b.z0);
  // A visit can be real and have no path: walk in, stand still, walk out,
  // and the movement gate records nothing. Saying "all 3 visits" when the
  // popup lists five is the kind of small lie that costs trust in the rest.
  const blank = group.length - drawn.length;
  const visits = single && group.length > 1
    ? `, one of ${group.length} visits`
    : (group.length > 1
        ? `, ${drawn.length === group.length ? `all ${drawn.length}` :
             `${drawn.length} of ${group.length}`} visits` +
          (blank ? ` (${blank} recorded no movement)` : '')
        : '');
  const how = {
    'by hand': `Drawn to the map's scale where you put it; which way it ` +
               `faces is the dungeon's own, and only a second way in can ` +
               `measure that.`,
    'the way in': `Drawn to the map's scale, near the dungeon it opens off -- ` +
                  `nothing recorded says where this one is, so drag its ` +
                  `marker to put it right.`,
  };
  // A frame pins one point inside to one point on the map. Which way the
  // inside is *turned* takes a second door, and until there is one the shape
  // is right and its bearing is a guess: correct at the door it is pinned to
  // and further out the further in you go. Raya Lucaria was 77 degrees and
  // 133 m out at its second door for two days, drawn as confidently as
  // anything else. So the caption says which of the two this is.
  const held = state.dungeonFrame.get(v.map_id);
  const where = how[v.placed] && !held
    ? how[v.placed]
    : (held && held.turn
        ? `Drawn to the map's scale, turned to line up with the two ways in.`
        : `Drawn to the map's scale, pinned at the way in -- which way it ` +
          `faces is not known until you come in by a second door.`);
  setCaption(
    `${v.label} ${v.map} - ${w} by ${h} m${visits}. ${where}` +
    (inside.pinned ? ' Click the map to let go.' : '')
  );
}

function nearestInTime(d, ts) {
  let best = null, bestGap = Infinity;
  for (const seg of d.segments) {
    for (let i = 0; i < seg.t.length; i++) {
      const gap = Math.abs(seg.t[i] - ts);
      if (gap < bestGap) { bestGap = gap; best = seg.xy[i]; }
    }
  }
  return best;
}

// Which *ends* of which jumps happened in here, in this place's own metres.
// A lift or a teleporter inside one dungeon has both ends here and is drawn
// with a line between them. A gate has one: step through a portal into a cave
// and the arrival is a real position inside it, while the departure is out on
// the surface with no frame to draw it in. Asking only for `w.inside` left
// every one of those off the dungeon entirely and standing at its mouth on
// the world map instead -- reported as exactly that, "the teleport marker was
// placed at the entrance, instead of the position inside".
//
// The moment to test against the visit is the end's own: a jump *out* of here
// carries the arrival's timestamp, which is after you left, so testing `w.ts`
// would have thrown away all ten of the departures on routes.db.
function warpsInside(v) {
  const end = v.left_ms || Infinity;
  const within = (ts) => ts >= v.entered_ms && ts <= end;
  const out = [];
  for (const w of (state.warpList || [])) {
    const ends = [];
    if (w.local && w.map_id === v.map_id && within(w.ts)) ends.push('to');
    if (w.from_local && w.from_map_id === v.map_id && within(w.from_ts)) {
      ends.push('from');
    }
    if (ends.length) out.push({ w, ends });
  }
  return out;
}

function deathsInside(v) {
  const end = v.left_ms || Infinity;
  return (state.deathList || []).filter(
    (d) => d.map_id === v.map_id && d.ts >= v.entered_ms && d.ts <= end);
}

function respawnsInside(v) {
  // Die in a castle and the grace you get up at is usually in the same
  // castle, in its own metres. It belongs on that castle's drawing, next to
  // the death it goes with, not on the entrance.
  const end = v.left_ms || Infinity;
  return (state.respawnList || []).filter(
    (r) => r.map_id === v.map_id && r.local
      && r.ts >= v.entered_ms && r.ts <= end);
}

function setCaption(text) {
  const el = document.getElementById('caption');
  el.hidden = !text;
  if (text) el.textContent = text;
}

/* --- interior inset ------------------------------------------------------

   Interior coordinates are local to their own map: two caves both start near
   zero, and neither shares a frame with the world plane. That is why they are
   stored with a NULL world position and drawn here, on their own axes, rather
   than being projected onto the map beside the route.
   ------------------------------------------------------------------------ */

function insideFor(v) {
  return v.duration_ms
    ? `${Math.max(1, Math.round(v.duration_ms / 60000))} min inside`
    : 'still inside';
}

function interiorPopup(group) {
  // Nodes rather than an HTML string: the labels come out of config.toml, and
  // the buttons need real listeners anyway.
  const el = document.createElement('div');
  const name = document.createElement('b');
  name.textContent = group.length === 1
    ? group[0].label
    : `${group[0].label} - ${group.length} visits`;
  el.appendChild(name);

  for (const v of [...group].sort((a, b) => a.entered_ms - b.entered_ms)) {
    const row = document.createElement('div');
    row.className = 'visit';
    const when = document.createElement('span');
    when.textContent = `${new Date(v.entered_ms).toLocaleString()} - ${insideFor(v)}`;
    row.append(when);
    // Every visit gets a button, not just the ones sharing an anchor: hover
    // is easy to miss, and a popup with no way to act on it reads as broken.
    const btn = document.createElement('button');
    btn.className = 'ghost';
    btn.textContent = 'Show path';
    btn.addEventListener('click', () => showInside(group, true, v));
    row.append(btn);
    el.appendChild(row);
  }

  // Naming it. Every cave is called "Cave" until you say otherwise, and the
  // map ID is the only thing telling two of them apart -- which is no use for
  // remembering which one had the bears in it.
  const naming = document.createElement('div');
  naming.className = 'naming';
  const field = document.createElement('input');
  field.type = 'text';
  field.placeholder = 'Name this place';
  field.maxLength = 80;
  field.value = group[0].name || '';
  const save = document.createElement('button');
  save.className = 'ghost';
  save.textContent = 'Save';
  const commit = () => {
    map.closePopup();
    nameDungeon(group[0].map_id, field.value);
  };
  save.addEventListener('click', commit);
  field.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') commit();
    // Leaflet turns keys on the map into shortcuts, and Escape would shut
    // the popup out from under a half-typed name.
    e.stopPropagation();
  });
  naming.append(field, save);
  el.appendChild(naming);

  const died = deathsInDungeon(group[0].map_id);
  if (died) {
    const toll = document.createElement('div');
    toll.className = 'toll';
    toll.textContent = died === 1
      ? 'Died here once' : `Died here ${died} times`;
    el.appendChild(toll);
  }

  const map_ = document.createElement('div');
  map_.className = 'hint';
  map_.textContent = [...new Set(group.map((v) => v.map))].join(', ');
  el.appendChild(map_);

  // Where the recording started inside, the entrance was never seen and the
  // marker sits where you came out instead. Say so rather than implying a
  // precision that isn't there.
  const placedNote = {
    exit: 'Placed where you came out: no entrance was recorded.',
    'another visit': 'Placed from an earlier visit: you warped into this one.',
    'the doorway': 'Placed at the door you came through, worked out from ' +
                   'where you were standing in the dungeon you came from.',
    'the way in': 'Placed at the dungeon it opens off, because nothing ' +
                  'recorded says where this one is. Drag this marker to ' +
                  'where it belongs and it will stay there.',
    'by hand': 'You placed this one. Drag it again to move it, or take it '
               + 'off the map if it does not belong on one.',
  };
  for (const how of Object.keys(placedNote)) {
    if (!group.some((v) => v.placed === how)) continue;
    const note = document.createElement('div');
    note.className = 'hint';
    note.textContent = placedNote[how];
    el.appendChild(note);
  }
  // Somewhere like the Roundtable Hold is not anywhere in the world, so a
  // corner of the map picked for it is a place it is not. This is the way
  // back out of a placement -- it goes to the corner of the screen instead,
  // and Put on map brings it back.
  if (group.some((v) => v.placed === 'by hand')) {
    const off = document.createElement('button');
    off.className = 'ghost';
    off.textContent = 'Take it off the map';
    off.addEventListener('click', () => {
      map.closePopup();
      unplaceDungeon(group[0].map_id, group[0].label);
    });
    el.appendChild(off);
  }
  return el;
}

// `corner` says it was opened from the disc in the corner of the screen, in
// which case it is drawn out of that disc rather than in its usual place.
async function openInterior(v, corner) {
  const box = control('inset');
  box.hidden = false;
  box.classList.toggle('from-corner', !!corner);
  // Restart the animation on a second opening: without the reflow the class
  // goes back on in the same frame and the keyframe does not replay.
  if (corner) { box.classList.remove('from-corner'); void box.offsetWidth;
                box.classList.add('from-corner'); }
  // Which visit is on screen, so pressing the same button again can put it
  // away rather than redrawing what is already there.
  state.insetKey = insideKey(v);
  state.insetMap = v.map;
  control('inset-title').textContent = v.label;
  control('inset-sub').textContent =
    `${insideFor(v)} \u00b7 ${new Date(v.entered_ms).toLocaleString()}`;
  control('inset-stats').textContent = 'Loading';
  // Before the fetch, not after: this is about the place, and it should be
  // there while the path is still coming.
  insetOffMap(v);

  const p = new URLSearchParams({ map_id: v.map_id, t0: v.entered_ms });
  if (v.left_ms) p.set('t1', v.left_ms);
  const res = await fetch('/api/interior?' + p);
  const d = await res.json();
  if (!res.ok || !d.ok) {
    control('inset-stats').textContent =
      d.error || 'The recorder could not return that path.';
    return;
  }
  state.inset = d;
  drawInterior(d);
}

function closeInterior() {
  control('inset').hidden = true;
  control('inset-offmap').hidden = true;
  control('inset').classList.remove('from-corner');
  state.inset = null;
  state.insetKey = null;
  state.insetMap = null;
}

function wireInset() {
  document.getElementById('inset-close').addEventListener('click', closeInterior);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeInterior();
  });
  // Follow the map's height tint so the two read as the same route.
  document.getElementById('tint-mode').addEventListener('change', () => {
    if (state.inset) drawInterior(state.inset);
  });
}

function niceStep(metresPerPixel, targetPx) {
  for (const m of [1, 2, 5, 10, 20, 25, 50, 100, 200, 500]) {
    if (m / metresPerPixel >= targetPx) return m;
  }
  return 1000;
}

function drawInterior(d) {
  const cv = document.getElementById('inset-canvas');
  const stats = document.getElementById('inset-stats');
  const size = 352;
  const dpr = window.devicePixelRatio || 1;
  cv.width = size * dpr;
  cv.height = size * dpr;
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, size, size);

  if (!d.bounds || !d.points_out) {
    stats.textContent =
      'No path stored for this visit: either it was recorded before interior ' +
      'paths were kept, or you never moved min_move_m while inside.';
    return;
  }

  const b = d.bounds;
  const pad = 16;
  const w = Math.max(b.x1 - b.x0, 1);
  const h = Math.max(b.z1 - b.z0, 1);
  // One scale for both axes. Stretching each to fill the box would draw a
  // corridor as a room, and the shape is the only thing this panel is for.
  const sc = Math.min((size - pad * 2) / w, (size - pad * 2) / h);
  const ox = pad + (size - pad * 2 - w * sc) / 2 - b.x0 * sc;
  const oy = pad + (size - pad * 2 - h * sc) / 2 - b.z0 * sc;
  const at = (x, z) => [x * sc + ox, z * sc + oy];

  const step = niceStep(1 / sc, 40);
  g.strokeStyle = 'rgba(222,213,194,0.07)';
  g.lineWidth = 1;
  for (let x = Math.ceil(b.x0 / step) * step; x <= b.x1; x += step) {
    const px = at(x, 0)[0];
    g.beginPath(); g.moveTo(px, 0); g.lineTo(px, size); g.stroke();
  }
  for (let z = Math.ceil(b.z0 / step) * step; z <= b.z1; z += step) {
    const pz = at(0, z)[1];
    g.beginPath(); g.moveTo(0, pz); g.lineTo(size, pz); g.stroke();
  }

  const lo = b.y0;
  const hi = b.y1 > b.y0 ? b.y1 : b.y0 + 1;
  g.lineWidth = Math.max(1.5, state.weight - 1);
  g.lineJoin = 'round';
  g.lineCap = 'round';
  for (const seg of d.segments) {
    if (seg.xy.length < 2) continue;
    if (state.tint === 'solid') {
      g.strokeStyle = state.colour;
      g.beginPath();
      seg.xy.forEach((pt, i) => {
        const p2 = at(pt[0], pt[1]);
        if (i) g.lineTo(p2[0], p2[1]); else g.moveTo(p2[0], p2[1]);
      });
      g.stroke();
      continue;
    }
    // Per-pair strokes: an interior path is hundreds of points, not hundreds
    // of thousands, so the simple version is fast enough here.
    for (let i = 1; i < seg.xy.length; i++) {
      const a = at(seg.xy[i - 1][0], seg.xy[i - 1][1]);
      const c = at(seg.xy[i][0], seg.xy[i][1]);
      g.strokeStyle = state.tint === 'age'
        ? ageColor(agePosition(seg.t[i]))
        : rampColor((seg.h[i] - lo) / (hi - lo));
      g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(c[0], c[1]); g.stroke();
    }
  }

  const firstSeg = d.segments[0].xy;
  const lastSeg = d.segments[d.segments.length - 1].xy;
  const dot = (pt, fill, r) => {
    const p2 = at(pt[0], pt[1]);
    g.beginPath();
    g.arc(p2[0], p2[1], r, 0, Math.PI * 2);
    g.fillStyle = fill;
    g.fill();
  };
  dot(firstSeg[0], '#9fbf7a', 4);                  // first step inside
  dot(lastSeg[lastSeg.length - 1], '#d8613a', 3.5); // last one before leaving

  // Scale bar: "how big is this place" is otherwise unanswerable, since these
  // axes mean nothing outside this one map.
  const barM = niceStep(1 / sc, 60);
  const barPx = barM * sc;
  g.strokeStyle = 'rgba(222,213,194,0.5)';
  g.lineWidth = 1.5;
  g.beginPath();
  g.moveTo(12, size - 14); g.lineTo(12 + barPx, size - 14);
  g.moveTo(12, size - 18); g.lineTo(12, size - 10);
  g.moveTo(12 + barPx, size - 18); g.lineTo(12 + barPx, size - 10);
  g.stroke();
  g.fillStyle = 'rgba(222,213,194,0.6)';
  g.font = '11px system-ui, sans-serif';
  g.fillText(`${barM} m`, 12 + barPx + 6, size - 10);

  // No figures. Points drawn, metres across, metres of height and the map ID
  // were four labelled numbers under a picture that answers three of them
  // better than any of them did -- the scale bar says how big it is and the
  // shape says the rest. What is worth reading is in the subtitle, which is
  // how long you were in there and when.
  stats.textContent = '';
}

// Each of these can be asked for again while the last one is still in the
// air -- a zoom, a death, and a dungeon being drawn all trigger a refresh.
// Without a token the slower answer clears the faster one's work and then
// adds its own on top, which showed up as more marks on the map than there
// are deaths in the database.
const refreshes = { deaths: 0, warps: 0, interiors: 0, insideLive: 0 };

// Whether a mark made inside a dungeon is already being shown by that dungeon,
// and so has no business being drawn on the world map as well.
//
// A death in a cave has no world position of its own, so the endpoint hands
// back the cave's entrance to put the mark on. That is one place too many:
// the pin at that entrance already carries a death count in its own badge, so
// a cave you died in nine times wore a red 9 and stood under a red 9, saying
// the same thing twice a few pixels apart -- and the cluster is the one of the
// two that is in the wrong spot, since the deaths did not happen at the mouth.
// The pin has it, and hovering the pin draws the deaths where they really
// were, on the dungeon's own path.
//
// Only while the dungeon is actually on the map. With Caves and dungeons
// unticked there is no pin and no badge, and a death is not a cave: it goes
// back to the entrance rather than disappearing with the layer that was
// standing in for it.
function hiddenInside(mark) {
  return state.layers.interior && mark.layer === 'interior';
}

async function loadDeaths() {
  const seq = ++refreshes.deaths;
  const data = await (await fetch('/api/deaths')).json();
  if (seq !== refreshes.deaths) return;
  deathGroup.clearLayers();
  respawnGroup.clearLayers();
  // Kept whether or not the layer is shown: the interior overlay needs to
  // know which deaths belong inside it.
  state.deathList = data.deaths;
  // A respawn is the other half of a death and comes back with it, so it
  // needs no fetch of its own -- and cannot go stale against the deaths.
  state.respawnList = data.deaths
    .filter((d) => d.respawn)
    .map((d) => ({ ...d.respawn, died_ts: d.ts, died_map: d.map,
                   died_xy: d.xy }));
  drawRespawns();
  if (!state.deaths) return;

  // Dying twice to the same thing is normal, and two marks on one spot look
  // like one. Cluster anything within a few metres and count it instead.
  const clusters = [];
  for (const d of data.deaths.filter(
    (d) => d.xy && markPlane(d)
      && (!state.range || (d.ts >= state.range[0] && d.ts <= state.range[1]))
  )) {
    if (hiddenInside(d)) continue;
    const near = clusters.find(
      (c) => Math.abs(c.xy[0] - d.xy[0]) < 12 && Math.abs(c.xy[1] - d.xy[1]) < 12);
    if (near) near.list.push(d);
    else clusters.push({ xy: d.xy, list: [d] });
  }
  for (const c of clusters) addDeathMark(c);
}

function drawRespawns() {
  if (!state.respawns) return;
  // Clustered like deaths, and for the same reason: dying twice to the same
  // boss puts you back at the same grace twice, and two marks on one spot
  // read as one.
  const clusters = [];
  for (const r of state.respawnList) {
    if (!r.xy || !markPlane(r)) continue;
    if (state.range && (r.ts < state.range[0] || r.ts > state.range[1])) continue;
    if (hiddenInside(r)) continue;
    const near = clusters.find(
      (c) => Math.abs(c.xy[0] - r.xy[0]) < 12 && Math.abs(c.xy[1] - r.xy[1]) < 12);
    if (near) near.list.push(r);
    else clusters.push({ xy: r.xy, list: [r] });
  }
  for (const c of clusters) addRespawnMark(c);
}

function addRespawnMark(c) {
  const n = c.list.length;
  const size = n > 1 ? 22 : 18;
  const mark = L.marker(toLatLng(c.xy), {
    pane: 'respawns',
    icon: L.divIcon({
      className: 'respawn-mark',
      html: `<span>\u2739</span>${n > 1 ? `<i class="count">${n}</i>` : ''}`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    }),
    keyboard: false,
  });
  const when = c.list.slice(0, 8).map(
    (r) => `${new Date(r.ts).toLocaleString()} - ${r.after_s}s after dying`
  ).join('<br>');
  const more = n > 8 ? `<br>and ${n - 8} more` : '';
  mark.bindPopup(
    `<b>${n > 1 ? `Got up here ${n} times` : 'Got up here'}</b><br>` +
    `${c.list[0].map}<br>${when}${more}`
  );
  // Hovering either end draws what it cost you, the way hovering a teleport
  // draws the jump.
  mark.on('mouseover', () => showDeathLines(
    c.list.map((r) => ({ from: r.died_xy, to: r.xy })).filter((l) => l.from)));
  mark.on('mouseout', hideDeathLines);
  mark.addTo(respawnGroup);
}

let deathLines = [];

function showDeathLines(pairs) {
  hideDeathLines();
  // Dashed and in the death's own red, running from where you went down to
  // where you got up: the two marks are one event and the line is how far
  // back it put you.
  deathLines = pairs.map((l) => L.polyline(
    [toLatLng(l.from), toLatLng(l.to)],
    { renderer, color: '#ff8a72', weight: 2, opacity: 0.8, dashArray: '4,6' }
  ).addTo(deathGroup));
}

function hideDeathLines() {
  for (const l of deathLines) deathGroup.removeLayer(l);
  deathLines = [];
}

function addDeathMark(c) {
  const n = c.list.length;
  const size = n > 1 ? 22 : 18;
  const mark = L.marker(toLatLng(c.xy), {
    pane: 'deaths',
    icon: L.divIcon({
      className: 'death-mark',
      html: `<span>\u2715</span>${n > 1 ? `<i class="count">${n}</i>` : ''}`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    }),
    keyboard: false,
  });
  const first = c.list[0];
  const where = first.layer === 'interior'
    ? `Inside ${first.label} (${first.map}) - hover the dungeon to see where`
    : first.map;
  const when = c.list.slice(0, 8)
    .map((d) => new Date(d.ts).toLocaleString()).join('<br>');
  const more = n > 8 ? `<br>and ${n - 8} more` : '';
  const back = c.list.filter((d) => d.respawn && d.respawn.xy);
  const html =
    `<b>${n > 1 ? `Died ${n} times here` : 'Died'}</b><br>${where}<br>${when}${more}` +
    (back.length
      ? `<br><span class="hint">${back.length === n ? '' : `${back.length} of ${n}: `}` +
        `respawned ${Math.round(back[0].respawn.after_s)}s later, ` +
        `${Math.round(metresBetween(c.xy, back[0].respawn.xy))} m away</span>`
      : '');
  const mine = c.list.filter((d) => d.by_hand);
  mark.bindPopup(mine.length === 1 && n === 1
    ? popupWithAction(html + '<br><span class="hint">You marked this one.</span>',
                      'Not a death after all',
                      () => { map.closePopup();
                              callDeath({ ts: mine[0].ts, clear: true }); })
    : html);
  mark.on('mouseover', () => showDeathLines(
    back.map((d) => ({ from: c.xy, to: d.respawn.xy }))));
  mark.on('mouseout', hideDeathLines);
  mark.addTo(deathGroup);
}

// Map pixels back to metres, so a popup can say how far back dying put you
// without the server being asked a second time.
function metresBetween(a, b) {
  const pr = state.meta.projection || { scale_x: 1, scale_y: 1 };
  const dx = (a[0] - b[0]) / (pr.scale_x || 1);
  const dz = (a[1] - b[1]) / (pr.scale_y || 1);
  return Math.hypot(dx, dz);
}

async function loadWarps() {
  const seq = ++refreshes.warps;
  // Fetch first, clear second. Clearing up front took every teleport mark off
  // the map for the length of the request -- which is the same flicker the
  // route had before it was fixed the same way, and it ran on every zoom.
  //
  // And fetched whether or not the layer is on, because state.warpList is not
  // only this layer's: the dungeon drawings put jumps on their own paths from
  // it. Returning early left that list holding whatever it held before the
  // session filter or the time window changed.
  const data = await (await fetch('/api/warps')).json();
  if (seq !== refreshes.warps) return;
  warpGroup.clearLayers();
  // Kept whole: the ones inside a dungeon have no world position and are
  // drawn on that dungeon's own drawing instead.
  // Not filtered by plane. `state.warpList` is not the teleport layer's --
  // the dungeon drawings read it, and so does the playback, which walks both
  // planes and switches the map as it goes. Filtering here meant every jump
  // made underground was missing from the playback entirely: two of them on
  // `routes.db`, at 13:54 and 14:07 on 6 September, reported as teleports
  // with no markers. The plane belongs to the drawing, which is where the
  // deaths have always applied it.
  state.warpList = data.warps.filter(
    (w) => !state.range || (w.ts >= state.range[0] && w.ts <= state.range[1]));
  if (!state.warps) return;
  // An end inside a dungeon is drawn on the world map at that dungeon's pin,
  // for want of anywhere better -- which is the right answer while the place
  // itself is not on the screen, and the wrong one when it is. A legacy
  // dungeon is drawn in the open at all times and now carries that end where
  // it actually happened, so the stand-in comes off: 13 of the 20 jumps with
  // an end inside a dungeon are into or out of one of those, and they would
  // otherwise wear two marks for one event a few hundred metres apart. A
  // cave needs no rule -- its drawing only appears while you hover it, and
  // hovering dims the world's marks anyway.
  const shown = (w, end) => {
    const inside = end === 'to' ? w.local : w.from_local;
    const map_id = end === 'to' ? w.map_id : w.from_map_id;
    return !(inside && state.alwaysDrawn && state.alwaysDrawn.has(map_id));
  };
  const outside = state.warpList.filter((w) => !w.inside && markPlane(w));
  // Both ends: where you went is only half of a teleport, and a mark only at
  // the arrival leaves the other end of the jump unaccounted for.
  addWarpMarks(outside.filter((w) => shown(w, 'to')), 'to');
  addWarpMarks(outside.filter((w) => shown(w, 'from')), 'from');
}

function addWarpMarks(warps, end) {
  const at = end === 'to' ? 'xy' : 'from_xy';
  // Warping out of the same grace a dozen times is normal, so the ends are
  // clustered the way deaths are rather than stacked into one unreadable pile.
  const clusters = [];
  for (const w of warps) {
    const p = w[at];
    const near = clusters.find(
      (c) => Math.abs(c.xy[0] - p[0]) < 12 && Math.abs(c.xy[1] - p[1]) < 12);
    if (near) near.list.push(w);
    else clusters.push({ xy: p, list: [w] });
  }

  for (const c of clusters) {
    const n = c.list.length;
    const size = n > 1 ? 22 : 18;
    const mark = L.marker(toLatLng(c.xy), {
      pane: 'warps',
      icon: L.divIcon({
        className: end === 'to' ? 'warp-mark' : 'warp-mark warp-from',
        html: `<span>${end === 'to' ? '\u2726' : '\u2727'}</span>` +
              (n > 1 ? `<i class="count">${n}</i>` : ''),
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
      }),
      keyboard: false,
    });
    // Without the HP reading a death and a teleporter are the same event, so
    // the tracker draws a teleport and offers to be corrected rather than
    // guessing. One jump at a time: a cluster can hold several, and only you
    // know which of them killed you.
    mark.bindPopup(c.list.length === 1
      ? popupWithAction(warpPopup(c.list, end), 'This was a death',
                        () => { map.closePopup(); callDeath({ ts: c.list[0].ts, from_ts: c.list[0].from_ts }); })
      : warpPopup(c.list, end));
    // Hovering either end draws the jump itself, so the pair reads as one
    // event rather than two unrelated marks.
    mark.on('mouseover', () => showWarpLines(
      c.list.map((w) => [w.from_xy, w.xy])));
    mark.on('mouseout', hideWarpLines);
    mark.addTo(warpGroup);
  }
}

function warpPopup(list, end) {
  const n = list.length;
  const title = end === 'to'
    ? (n > 1 ? `Arrived here ${n} times` : 'Arrived here')
    : (n > 1 ? `Left from here ${n} times` : 'Left from here');
  const rows = list.slice(0, 8).map((w) => {
    const km = (w.distance_m / 1000).toFixed(2);
    const other = end === 'to' ? `from ${w.from_map}` : `to ${w.map}`;
    return `${new Date(w.ts).toLocaleString()} - ${km} km ${other}`;
  }).join('<br>');
  const more = n > 8 ? `<br>and ${n - 8} more` : '';
  // The server sends the whole phrase now, because there are three of them:
  // a load screen, a speed nothing can walk, and a map change that turned out
  // to be a warp. Matching on one string here and adding a third there is how
  // the third one silently reads as the second.
  const why = list[0].reason;
  return `<b>${title}</b><br>${rows}${more}<br><span class="hint">${why}</span>`;
}

async function callDeath(body) {
  // Marking a death changes where the line breaks, and the cached interior
  // paths were fetched with the old breaks in them.
  inside.cache.clear();
  try {
    const res = await fetch('/api/death', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    var d = await res.json();
    if (!res.ok || !d.ok) {
      setStats(d.error || 'The recorder would not change that.');
      return false;
    }
  } catch (e) {
    setStats('Could not reach the recorder to change that.');
    return false;
  }
  // The death filter in warps() takes the teleport off the map, the death
  // mark appears where you went down and the respawn where you got up, so
  // both lists have to come back.
  await loadDeaths();
  await loadWarps();
  // The playback has its own drawing of all of this and rebuilds it itself.
  // Reloading the interiors here would redraw the map underneath it, which
  // is not on screen, and re-enter the dungeon overlay, which is.
  if (play.on) return d;
  await loadInteriors();
  // The dungeon overlay draws its own marks from those lists, so if one is
  // up it has to be rebuilt too -- otherwise the mark you just changed keeps
  // its old shape until you happen to hover something else.
  if (inside.key && inside.last) {
    await showInside(inside.last.group, inside.pinned, inside.last.only);
  }
  return d;
}

// A button inside a popup, which is a place Leaflet will happily swallow
// clicks in unless the wiring is real DOM rather than an HTML string.
function popupWithAction(html, label, onClick) {
  const el = document.createElement('div');
  el.innerHTML = html;
  const btn = document.createElement('button');
  btn.className = 'ghost';
  btn.textContent = label;
  btn.addEventListener('click', onClick);
  el.appendChild(btn);
  return el;
}

// Where a jump goes, shown while you point at it and not otherwise.
//
// The pairs arrive already resolved to map pixels, because an end inside a
// dungeon is only in this space once that dungeon's frame has been applied,
// and the caller is the one holding the frame. That is also what lets a jump
// with one end on the surface draw a line at all: the two ends are in
// different coordinate spaces right up until they are both projected, and
// then they are the same pixels.
function showWarpLines(pairs, into, lineRenderer) {
  hideWarpLines();
  warpHome = into || warpGroup;
  warpLines = pairs
    .filter(([a, b]) => a && b)
    .map(([a, b]) => L.polyline([toLatLng(a), toLatLng(b)], {
      renderer: lineRenderer || renderer,
      color: '#8fb7cc', weight: 2, opacity: 0.75, dashArray: '5,7',
    }).addTo(warpHome));
}

function hideWarpLines() {
  for (const l of warpLines) warpHome.removeLayer(l);
  warpLines = [];
}

let warpLines = [];
let warpHome = null;

// The same list, pinned to the corner of the screen. A dungeon with no place
// on the map has no marker to click, and the panel row for it is three
// sections down -- so the one that matters most, the Roundtable Hold, was the
// hardest of all of them to reach. This opens the inset and moves nothing.
function buildOffMap(list) {
  const box = control('offmap');
  if (!box.replaceChildren) return;
  // One button per place, not per visit: four trips to the Roundtable Hold
  // are four rows in the panel and one door in the corner. The longest visit
  // stands for the group, because it is the one with a path worth opening.
  const best = new Map();
  for (const v of list) {
    const held = best.get(v.map_id);
    if (!held || (v.duration_ms || 0) > (held.duration_ms || 0)) {
      best.set(v.map_id, v);
    }
  }
  // Every visit, keyed by map: the window this button opens lists them.
  state.offmap = new Map();
  for (const v of list) {
    if (!state.offmap.has(v.map_id)) state.offmap.set(v.map_id, []);
    state.offmap.get(v.map_id).push(v);
  }
  const places = [...best.values()];
  box.hidden = !places.length;
  box.replaceChildren(...places.map((v) => {
    const n = list.filter((x) => x.map_id === v.map_id).length;
    const b = document.createElement('button');
    // The same rook the map draws on a dungeon you can see from outside: this
    // is the marker for a place that has nowhere to put one.
    b.textContent = '♜';
    b.dataset.name = v.label;
    b.setAttribute('aria-label', v.label);
    b.title = `${v.label} - ${v.map} - nowhere on the map - `
              + `${n} visit${n === 1 ? '' : 's'}, longest ${insideFor(v)}`;
    // A second press puts it away. The window is a thing this button opens,
    // so the button is where it is closed.
    b.addEventListener('click', () => {
      if (!control('inset').hidden && state.insetKey === insideKey(v)) {
        closeInterior();
        return;
      }
      openInterior(v, true);
    });
    return b;
  }));
}

// Every visit to a place that is nowhere, drawn into the window the corner
// button opens. This was a section in the panel, which meant the four trips
// to the Roundtable Hold were four rows three sections down from a button
// that already stood for the place -- the same list in two places, one of
// them nowhere near the thing it was about. Attached to the button now: one
// door in the corner, and everything about that place behind it.
function insetOffMap(v) {
  const box = control('inset-offmap');
  if (!box.replaceChildren) return;
  const group = (state.offmap.get(v.map_id) || [])
    .slice().sort((a, b) => a.entered_ms - b.entered_ms);
  box.hidden = !group.length;
  if (!group.length) return;

  // Folded, and closed to begin with. The window is opened to look at the
  // shape of a place; a list of the times you have been there is a second
  // question, and standing open it was the taller half of the window.
  const fold = document.createElement('details');
  fold.className = 'fold';
  const head = document.createElement('summary');
  head.textContent = group.length === 1
    ? '1 visit' : `${group.length} visits`;
  fold.append(head);
  const rows = [];
  for (const other of group) {
    const row = document.createElement('div');
    row.className = 'visit';
    const when = document.createElement('span');
    when.textContent =
      `${new Date(other.entered_ms).toLocaleString()} - ${insideFor(other)}`;
    row.append(when);
    // The one on screen says so rather than offering to draw itself again.
    if (insideKey(other) === state.insetKey) {
      const here = document.createElement('i');
      here.className = 'off-here';
      here.textContent = 'shown';
      row.append(here);
    } else {
      const btn = document.createElement('button');
      btn.className = 'ghost';
      btn.textContent = 'Show path';
      btn.addEventListener('click', () => openInterior(other, true));
      row.append(btn);
    }
    rows.push(row);
  }

  fold.append(...rows);
  const out = [fold];

  // Nothing in the route says where these are, and for most of them nothing
  // ever will: you only reach them by warping. But you may well know -- and a
  // position set by hand outranks every inference. It had no way in for a
  // dungeon with no marker to drag, which is why it lives here.
  //
  // Not for a place the game itself puts nowhere. The Roundtable Hold has no
  // way in on foot at all and the game's own map screen draws it off the
  // terrain in a corner, so it is in the corner of the screen for good: an
  // offer to correct that would be an offer to make it wrong. `nowhere_maps`
  // in config says which, because which places those are is a fact about the
  // game rather than about this database.
  if (!v.fixed) {
    const put = document.createElement('button');
    put.className = 'ghost put-on-map';
    put.textContent = 'Put on map';
    put.addEventListener('click', () => { closeInterior(); startPlacing(v); });
    out.push(put);
  }
  box.replaceChildren(...out);
}

async function unplaceDungeon(map_id, label) {
  try {
    const res = await fetch('/api/place', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // Not `clear`, which only hands the question back to the tiers that
      // were guessing in the first place: on routes.db the Hold then gets an
      // `exit` anchor 3 km from anywhere it could be. This says there is no
      // answer, which is the truth about it.
      body: JSON.stringify({ map_id, nowhere: true }),
    });
    const d = await res.json();
    if (!res.ok || !d.ok) {
      setStats(d.error || 'The recorder would not take that one off the map.');
      return;
    }
  } catch (e) {
    setStats('Could not reach the recorder to take that one off the map.');
    return;
  }
  setStats(`${label} taken off the map. It is in the corner now; `
           + `Put on map in the panel brings it back.`);
  await afterPlacing();
}

// Placing by click rather than by drag: there is no marker yet, so there is
// nothing to take hold of.
const placing = { map_id: null, label: '' };

function startPlacing(v) {
  placing.map_id = v.map_id;
  placing.label = `${v.label} (${v.map})`;
  document.getElementById('map').classList.add('placing');
  setCaption(`Click where ${placing.label} belongs. Escape to cancel; you ` +
             `can drag it afterwards.`);
}

function stopPlacing() {
  placing.map_id = null;
  document.getElementById('map').classList.remove('placing');
  setCaption(null);
}

async function placeAt(latlng) {
  const pr = state.meta.projection;
  const px = map.project(latlng, state.nativeZoom);
  const wx = (px.x - pr.offset_x) / pr.scale_x;
  const wz = (px.y - pr.offset_y) / pr.scale_y;
  const map_id = placing.map_id;
  const label = placing.label;
  stopPlacing();
  try {
    const res = await fetch('/api/place', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ map_id, wx, wz }),
    });
    const d = await res.json();
    if (!res.ok || !d.ok) {
      setStats(d.error || 'The recorder would not save that position.');
      return;
    }
  } catch (e) {
    setStats('Could not reach the recorder to save that position.');
    return;
  }
  setStats(`${label} placed. Drag its marker to move it.`);
  await afterPlacing();
}

// Where a dungeon is, is borrowed by everything that happened inside it and
// has nowhere else to be drawn: a death in there is handed the dungeon's
// position, so is a respawn, and so is the end of any jump that crossed into
// it. So moving the dungeon has to move all three, and the drag moved only
// the pin and the path -- reported as the teleport marker staying where it
// was after the place it belonged to had been dragged away from under it.
// The same shape of mistake as a marker checkbox reaching one of the two
// places a mark is drawn, and the same answer: say all of it in one place.
async function afterPlacing() {
  await loadInteriors();
  await loadDeaths();
  await loadWarps();
}

// How many points of tail are kept. Everything older has been committed and
// comes back from the server on the next reload.
const LIVE_MAX = 4000;

// How often the colours are brought up to date. The ramp moves at the speed a
// route is walked, so a second is far finer than the eye needs and the pass
// costs a fraction of a millisecond.
const RECOLOUR_MS = 1000;

// How heavy the line still being written is drawn. A little more than the
// route it is extending, so the newest stretch reads as the live one -- and
// one rule for both tails, because the surface one had it frozen at 3.5.
// That is this at the default thickness and nothing like it at any other, so
// with the slider at 8 the committed route was 8 and everything since your
// last refetch was 3.5, which during play is everything since you last
// happened to zoom. Nudging the slider fetched the route again and the tail
// shrank to a couple of points, which is why the workaround was to "adjust it
// back and forth".
function liveWeight() {
  return Math.max(1, state.weight) + 0.6;
}

function rememberTint(line, t) {
  state.tinted.push({ line, t, colour: line.options.color });
}

function redrawLive() {
  // reload() rebuilds the route from the server, which does not know about
  // samples the recorder has not committed yet. The buffer survives that, and
  // the polylines the websocket appends to are recreated here -- appending to
  // a layer that had been cleared away was why the map stopped moving until
  // you zoomed.
  //
  // Banded like the rest of the route rather than drawn as one flat line.
  // That was right while the tail was a second or two of uncommitted samples
  // -- but nothing refetches the route while you play, so the tail is really
  // everything since the last time you happened to zoom, and in one colour it
  // says the gradient has stopped moving. Only under `by age`: in the other
  // two modes the tail's brightness is what marks it as the live one, and
  // neither of those colourings changes with time anyway.
  liveGroup.clearLayers();
  state.liveRuns = [];
  state.liveLayer = null;
  if (!state.live || !state.live.length) return;
  if (state.live.length > LIVE_MAX) {
    // Trimmed here rather than where the points are pushed, because the cut
    // has to take the timestamps with it and the runs are built from both.
    state.live = state.live.slice(-LIVE_MAX);
    state.liveT = state.liveT.slice(-LIVE_MAX);
  }
  const pts = state.live;
  const flat = state.tint !== 'age';
  // A null in the buffer is a break -- a load screen, a teleport, coming back
  // out of a dungeon -- and the line stops there rather than being drawn
  // through it. It is a gap in the tail and not the end of it: everything
  // since the last refetch is drawn *only* here.
  const bands = flat ? null
    : state.liveT.map((t) => (t === null ? null : ageBand(t)));
  const runs = [];
  let start = -1;
  for (let i = 0; i <= pts.length; i++) {
    const end = i === pts.length;
    const gap = !end && pts[i] === null;
    if (start < 0) {
      if (!end && !gap) start = i;
      continue;
    }
    // A run ends at a gap, at the end of the buffer, or where the band
    // changes. Written as `!flat && bands[i] === bands[start]` the band test
    // was false all the way down in the two modes that have no bands, so a
    // 21-point tail came out as 21 polylines.
    const bandEnds = !end && !gap && !flat && bands[i] !== bands[start];
    if (!end && !gap && !bandEnds) continue;
    // A change of colour shares its boundary point with the next run, so the
    // line is never broken by it. A gap does not: breaking there is the
    // point.
    runs.push({ from: start, upto: bandEnds ? i + 1 : i, band: bands && bands[start] });
    start = bandEnds ? i : -1;
  }
  const weight = liveWeight();
  // Every outline first and every line after, for the reason the route keeps
  // its casings in a group of their own: within one canvas the order they go
  // on is the order they are painted, so a run's outline drawn after its
  // neighbour's line would sit on top of it at the seam.
  if (state.casing > 0) {
    for (const r of runs) {
      r.casing = L.polyline(pts.slice(r.from, r.upto), {
        renderer, color: '#0d0b08', weight: weight + state.casing,
        opacity: 0.55, lineJoin: 'round', lineCap: 'round',
      }).addTo(liveGroup);
    }
  }
  for (const r of runs) {
    r.line = L.polyline(pts.slice(r.from, r.upto), {
      renderer,
      color: flat ? '#f7d488' : bandColor(r.band),
      weight, opacity: 1, lineJoin: 'round',
    }).addTo(liveGroup);
    state.liveRuns.push(r);
    if (!flat) rememberTint(r.line, state.liveT[r.from]);
    state.liveLayer = r.line;
  }
}

// The gradient is a statement about now, and now moves while you are playing.
// Nothing was moving it: `state.span` is written by drawSegments(), which only
// runs from reload(), and reload() only runs when you zoom or change a filter
// -- so the ramp's near end sat where your last scroll left it and every
// stretch kept the colour it was given then. Measured on a simulated session
// with the horizon at fifteen minutes: over 131 seconds of recording the
// newest end advanced 0 ms and not one of the drawn stretches changed colour,
// and then a single scroll moved it 203 seconds and redrew the lot.
//
// A refetch is the wrong answer -- it is a hundred times the work to recover a
// picture that is already on the screen. This is playRecolour()'s idea for the
// map: work out what each stretch should be now and set only the ones that
// have actually crossed into another band.
function recolourRoute(force) {
  const now = Date.now();
  if (!force && now - state.tintedAt < RECOLOUR_MS) return;
  state.tintedAt = now;
  // The tail is rebuilt rather than restyled: its *bands* move as well as its
  // colours, so where one run ends and the next begins is part of the answer.
  // In the other modes it is one run that never changes colour, and the only
  // reason to touch it is to hold it to its cap.
  if (state.tint === 'age' || state.live.length > LIVE_MAX) redrawLive();
  if (state.tint !== 'age') return;
  // The legend says what the two ends of the gradient mean, so it is part of
  // the same statement and goes stale in the same way.
  if (state.hExtent) showRamp(state.hExtent[0], state.hExtent[1]);
  let gone = 0;
  for (const item of state.tinted) {
    if (!item.line._map) { gone++; continue; }
    const colour = bandColor(ageBand(item.t));
    if (colour === item.colour) continue;
    item.colour = colour;
    item.line.setStyle({ color: colour });
  }
  // A drawing that has been taken off the map is not coming back: the group
  // it was in was cleared and whatever replaced it registered itself.
  if (gone) state.tinted = state.tinted.filter((i) => i.line._map);
}

function toLatLng(xy) {
  return map.unproject([xy[0], xy[1]], state.nativeZoom);
}

/* --- live feed ----------------------------------------------------------- */

function connectLive() {
  const status = document.getElementById('status');
  // A viewer serving a database is not a recorder. It answers the websocket
  // like one -- the same server class serves both -- so the socket opened,
  // said Live, and then nothing ever arrived on it: the page claiming to be
  // watching a game that is not running. `meta.recording` is the session
  // being written, or null, which is the question actually being asked.
  if (!state.meta || !state.meta.recording) {
    status.textContent = 'Offline map';
    status.dataset.live = '0';
    return;
  }
  const ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => { status.textContent = 'Live'; status.dataset.live = '1'; };
  ws.onclose = () => {
    status.textContent = 'Not recording';
    status.dataset.live = '0';
    stopLiveInside();
    setTimeout(connectLive, 3000);
  };
  ws.onmessage = (ev) => {
    const s = JSON.parse(ev.data);

    if (s.type === 'death') {
      // Refetched rather than drawn from the message: deaths in the same spot
      // are clustered into one mark with a count, and that is the server's
      // list to group, not a single event's to guess at.
      status.textContent = 'Died';
      // The dungeon markers carry the toll, so they have to be rebuilt too --
      // otherwise the number is right only after the next zoom.
      loadDeaths()
        .then(() => loadInteriors())
        .then(() => { if (inside.key) refreshLiveInside(); });
      return;
    }

    if (s.type === 'interior' && s.break === BREAK_RELOAD) loadDeaths();

    if (s.type === 'interior') {
      // No world position to draw, so say where you are instead of appearing
      // to have stopped recording -- and put the dungeon itself on screen.
      status.textContent = `Inside ${s.label || 'a dungeon'}`;
      status.dataset.live = '1';
      // You moved. Whatever you stepped out to look at, you are walking
      // again, and the cave is what you are walking in.
      if (state.peek) setPeek(false);
      state.liveLayer = null;   // don't join the cave to the surface line
      if (!state.wasInside || state.insideMap !== s.map) {
        state.wasInside = true;
        state.insideMap = s.map;
        startLiveInside(s.map);
      }
      // Inside, the only position is the dungeon's own. Put the mark on the
      // path being drawn rather than leaving it parked at the entrance.
      if (s.local && inside.transform && inside.transform.map_id === s.map_id) {
        setYouMark(toLatLng(inside.transform.at(s.local[0], s.local[1])), null);
        // A load screen inside a dungeon is a death or a lift, and the line
        // should break there rather than being drawn through the rock.
        if (s.break) state.liveInside.push(null);
        state.liveInside.push(s.local);
        if (state.liveInside.length > 4000) state.liveInside.shift();
        redrawLiveInside();
      }
      return;
    }
    if (s.type !== 'sample') return;

    if (state.wasInside) {
      state.wasInside = false;
      state.insideMap = null;
      status.textContent = 'Live';
      stopLiveInside();
      // The visit just closed: its marker, and any death that happened in
      // there, belong on the map now rather than after the next zoom.
      loadInteriors();
      loadDeaths();
    }

    // "Died" and "Inside" are moments, not states: the next position you
    // report is you walking around again.
    if (status.textContent !== 'Live') status.textContent = 'Live';

    // Arriving somewhere you did not walk to leaves a warp to mark.
    if (s.break) loadWarps();
    // And a load screen is how getting up from a death looks. The death mark
    // appeared twelve seconds ago with no respawn to go with it, because the
    // respawn had not happened yet; this is the moment it has.
    if (s.break === BREAK_RELOAD) loadDeaths();

    // Going down the lift into Siofra changes which map you are on, and
    // being shown the one you are not on is the same as not tracking. An
    // interior does not count: a cave is drawn on whichever plane its
    // entrance is on, and switching maps under you for one would be wrong.
    if (state.autoPlane
        && (s.layer === 'surface' || s.layer === 'underground')
        && s.layer !== state.livePlane) {
      state.livePlane = s.layer;
      selectPlane(s.layer);
    }

    if (s.layer !== state.plane) return;

    const here = toLatLng(s.xy);
    // A break splits the line; it does not end the tail. Emptying the buffer
    // here took every point of it off the map -- and the committed route does
    // not cover those points until something refetches, so dying wiped the
    // path back to wherever you last happened to zoom and zooming fetched it
    // back. Reported as exactly that. The null is the same thing
    // `state.liveInside` has always pushed for a load screen inside a
    // dungeon.
    const broke = (s.break || !state.liveLayer) && state.live.length > 0;
    if (broke) {
      state.live.push(null);
      state.liveT.push(null);
    }
    state.live.push(here);
    state.liveT.push(s.t);
    // The newest thing drawn on the map is now this sample, and the newest
    // thing drawn is exactly what the age ramp is measured back from -- the
    // wall clock is deliberately not used, or the whole route would slide into
    // the oldest colour whenever the recorder was off. Without this line the
    // near end of the gradient stayed wherever the last refetch put it.
    if (state.span && s.t > state.span[1]) state.span[1] = s.t;
    // The head follows the feed at once. The banding behind it is a second
    // stale at worst, which is the recolour tick. A break is the one thing
    // that cannot be appended to: the run it closed stays where it is and the
    // next point needs one of its own, which costs the one rebuild.
    const run = broke ? null : state.liveRuns[state.liveRuns.length - 1];
    if (run) {
      const pts = state.live.slice(run.from);
      run.line.setLatLngs(pts);
      if (run.casing) run.casing.setLatLngs(pts);
    } else redrawLive();
    recolourRoute();
    setYouMark(here, s);
  };
}

/* --- playback -------------------------------------------------------------

   Watching the route back rather than looking at it finished. The map is
   cleared and the path is drawn again in the order it was made, at a chosen
   multiple of real time, with the caves drawn as caves and the deaths and
   teleports announcing themselves as they arrive.

   Three things about this are not obvious.

   It walks *played* time, not the calendar. `routes.db` spans 34 days and
   thirteen hours of walking, so a playback paced by the clock spends almost
   all of itself on a stationary dot -- the first version of this jumped from
   3 August to 5 August in one step while the progress read 0%. Every gap is
   capped at ten seconds, the same cap the statistics use, and everything is
   positioned on that axis instead.

   A cave is not on the world plane, so it cannot simply be part of the line.
   Its path is in the dungeon's own metres and is drawn the way hovering a
   marker draws it: the world dims, the dungeon appears in its own frame, and
   the drawing continues in there until you come out. The run list carries
   which place each run belongs to and the switch falls out of that.

   And seeking is a rebuild, not a rewind. Dragging the scrubber clears
   everything and replays up to the new position with the animations
   suppressed, which is both simpler than undoing and the only version that
   gives the same picture whichever direction you came from.
   ------------------------------------------------------------------------ */

// A gap longer than this is a night off, or lunch.
const PLAY_GAP_CAP_MS = 10_000;
// Sixty a second rather than the twelve it ran at, which is what "it snaps
// from point to point" was: at thirty seconds a second the head only advances
// one recorded point a tick, so the step *was* the tick. Affordable because a
// full repaint of the route canvas measures 0.5 ms median and 1.7 ms worst
// with the whole of routes.db drawn -- 3% of a second at this rate.
const PLAY_TICK_MS = 16;          // wall clock between steps
const PLAY_FLASH_MS = 1500;       // how long a death or a teleport announces
// Easing off, on the other hand, belongs to the teleport and not to the
// pixels. Measured over one playback of routes.db at zoom 8: the pixel rule
// fired on 246 steps against 64 actual jumps, because at a close zoom every
// 24-second step is most of a screen -- so the playback was uniformly slower
// and the teleport, the one thing it was meant to mark, did not stand out at
// all. Which is exactly how it was reported.
//
// How long still comes from the distance on screen, because that is what the
// eye has to cross: at zoom 3 the median jump is 79 px of a 1,178 px diagonal
// and barely needs a pause; at zoom 8 it is 2,017 px, nearly two screens.
// The floor is PLAY_TRAVEL_MS, because that is how long the line takes to
// draw itself from one end to the other: easing off for less than that means
// the playback has moved on before the jump has finished being made, which is
// the thing that reads as not slowing down at all.
// (PLAY_TRAVEL_MS is declared further down, and a const cannot be read before
// its own line runs -- so the number is here rather than the name.)
const PLAY_BRAKE_MIN_MS = 420;
const PLAY_BRAKE_SPAN_MS = 700;   // and a screen's worth of jump adds this
const PLAY_BRAKE_MS = 950;        // capped, so a route-long warp is not a stop
const PLAY_BRAKE_RATE = 0.06;     // how fast the clock runs while easing off

function playBrakeFor(e) {
  let px = 0;
  if (e.from && e.xy) {
    const a = map.latLngToContainerPoint(toLatLng(e.from));
    const b = map.latLngToContainerPoint(toLatLng(e.xy));
    px = Math.hypot(a.x - b.x, a.y - b.y);
  }
  const sz = map.getSize();
  const diag = Math.hypot(sz.x, sz.y) || 1;
  return Math.min(PLAY_BRAKE_MS,
                  PLAY_BRAKE_MIN_MS + (px / diag) * PLAY_BRAKE_SPAN_MS);
}

const play = {
  on: false, timer: null, token: 0,
  // `at` is the clock and `to` is the whole route. `from` and `until` are the
  // part of it the playback actually runs -- the two grips on the timeline --
  // and they start out as the whole thing.
  at: 0, to: 0, from: 0, until: 0, speed: 300,
  // Run it again at the end grip rather than stopping. Remembered,
  // because it is a way of watching rather than a thing you do once.
  loop: false,
  // Which way the clock runs. Forwards costs almost nothing -- the drawing
  // only grows -- and backwards costs a rebuild a frame, for the reason
  // `playSeek()` gives: the picture cannot be unwound, only replayed.
  dir: 1,
  // What the last of those rebuilds cost, so the next one can wait it out
  // rather than pinning the thread.
  seekMs: 0,
  ticksOn: true,
  runs: [], events: [], axis: null,
  cursor: 0, event: 0,
  head: null,                     // the run currently half drawn
  group: null,                    // the world path so far
  marks: null, insideMarks: null, // what happened, out there and in here
  visits: [], pinned: new Set(),  // the caves, and which have been found yet
  spots: [],                      // marks already standing, so they count up
  hold: null,                     // a place kept on screen while it announces
  where: null,                    // 'world', or the map_id we are inside
  shown: new Set(),               // places the clock itself has put on screen
  mark: null, deaths: 0,
  here: null,                     // the point the clock is on, for the mark
  hoverTs: null,                  // the moment under the cursor, if any
  clocked: 0,                     // when the clock line was last rewritten
  last: 0,                        // wall clock at the previous step
  onPlane: null,                  // and which map that point is drawn on
  plane0: null,                   // the plane to give back when this is over
  brake: 0,                       // running slowly until this moment
  centre: null,                   // where a jump in the air is going to land
  lines: [],                      // every stretch drawn, for recolouring
  rankNow: 1,                     // where the playhead sits among the samples
  tinted: 0,                      // when the colours were last brought up to date
  scrubbing: false,
};

/* --- the time axis --------------------------------------------------------

   One number line for the whole playback, built from every timestamp in it
   with the long gaps squeezed out. Everything else -- the scrubber, the
   clock, when an event fires -- is a position on it.
   ------------------------------------------------------------------------ */

function buildAxis(stamps) {
  stamps.sort((a, b) => a - b);
  const ts = [], el = [];
  let e = 0;
  for (const t of stamps) {
    if (ts.length) {
      if (t === ts[ts.length - 1]) continue;
      e += Math.min(t - ts[ts.length - 1], PLAY_GAP_CAP_MS);
    }
    ts.push(t);
    el.push(e);
  }
  return { ts, el, total: e };
}

function axisIndex(arr, want) {
  let lo = 0, hi = arr.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (arr[mid] <= want) lo = mid; else hi = mid - 1;
  }
  return lo;
}

// Elapsed playback time -> the wall clock it stands for. Inside a step the
// two run together, because a gap is only squeezed when it is longer than the
// cap and everything shorter than that is played at its real length.
function playClock(elapsed) {
  const a = play.axis;
  if (!a || !a.ts.length) return 0;
  const i = axisIndex(a.el, elapsed);
  if (i >= a.ts.length - 1) return a.ts[a.ts.length - 1];
  const over = elapsed - a.el[i];
  return a.ts[i] + Math.min(over, a.ts[i + 1] - a.ts[i]);
}

/* --- building the script ------------------------------------------------- */

// One run is a stretch of path of a single colour, which is the unit the
// drawing already works in: this is the same banding drawSegments does, so a
// played-back route looks like the route it is playing.
function boxOf(xy, start, stop, tf) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let i = start; i < stop; i++) {
    const q = tf ? tf(xy[i][0], xy[i][1]) : xy[i];
    if (q[0] < x0) x0 = q[0];
    if (q[0] > x1) x1 = q[0];
    if (q[1] < y0) y0 = q[1];
    if (q[1] > y1) y1 = q[1];
  }
  return [x0, y0, x1, y1];
}

function playRuns(segments, where, tf, lo, hi, plane) {
  const out = [];
  for (const seg of segments) {
    if (!seg.xy || seg.xy.length < 2) continue;
    const under = seg.layer === 'underground';
    const base = state.tint === 'solid'
      ? (under ? shade(state.colour, -0.35) : state.colour)
      : (under ? '#6f8fa8' : state.colour);
    const weight = Math.max(0.5, state.weight - (under ? 0.5 : 0));
    let start = 0;
    for (let i = 1; i <= seg.xy.length; i++) {
      const end = i === seg.xy.length;
      if (!end && bandOf(seg, i, lo, hi) === bandOf(seg, start, lo, hi)) continue;
      const stop = Math.min(i + 1, seg.xy.length);
      if (stop - start > 1) {
        const band = bandOf(seg, start, lo, hi);
        out.push({
          where,
          // Which map this stretch is drawn on. A world stretch says so
          // itself; a dungeon's belongs to the plane its mouth opens off.
          plane: plane || (under ? 'underground' : 'surface'),
          xy: seg.xy.slice(start, stop).map((q) => (tf ? tf(q[0], q[1]) : q)),
          t: seg.t.slice(start, stop),
          colour: state.tint === 'solid' ? base : bandColor(band),
          // Where this stretch sits among all the samples, so the age ramp
          // can be renormalised against the playhead rather than against the
          // end of a route the playback has not reached yet.
          rank: ageRank(seg.t[stop - 1]),
          // The box it occupies, so hovering can rule a run out without
          // looking at any of its points.
          box: boxOf(seg.xy, start, stop, tf),
          weight,
        });
      }
      start = i;
    }
  }
  return out;
}

// Which visit a mark made inside a dungeon belongs to. A death has one
// moment and a visit has a window, so this is not a guess.
function playVisitAt(frames, map_id, ts) {
  for (const [key, f] of frames) {
    if (f.map_id === map_id && ts >= f.from && ts <= f.to) return { key, f };
  }
  return null;
}

// Deaths and teleports, placed the way the map places them: on the world
// plane if they have a world position, and in the frame of the visit they
// happened in if all they have is a dungeon's own metres.
//
// The one thing this refuses to do is fall back to the world position for a
// mark made inside. That position is the dungeon's *entrance* -- it is what
// /api/deaths hands back for want of anywhere better -- so the fallback drew
// deaths at the cave mouth, several hundred metres from where they happened,
// with the cave's real path on screen next to them.
function planeOf(layer) {
  return layer === 'underground' ? 'underground' : 'surface';
}

function playEvents(frames) {
  const out = [];
  for (const d of (state.deathList || [])) {
    if (state.range && (d.ts < state.range[0] || d.ts > state.range[1])) continue;
    if (d.layer === 'interior') {
      const hit = playVisitAt(frames, d.map_id, d.ts);
      if (!hit) continue;
      // Recorded before local coordinates were kept: the point on that
      // visit's path closest in time is where you were when it landed.
      const local = d.local || nearestInTime(hit.f.d, d.ts);
      if (!local) continue;
      out.push({ ts: d.ts, kind: 'death', where: hit.key, by_hand: d.by_hand,
                 xy: hit.f.tf(local[0], local[1]), label: d.label,
                 plane: hit.f.plane });
    } else if (d.xy) {
      out.push({ ts: d.ts, kind: 'death', where: 'world', by_hand: d.by_hand,
                 xy: d.xy, label: d.label, plane: planeOf(d.layer) });
    }
  }
  // Where you got up. Its own event at its own moment rather than something
  // drawn with the death, because it is twelve seconds later and the walk
  // back from the grace is half of what a death looks like on a map.
  for (const r of (state.respawnList || [])) {
    if (state.range && (r.ts < state.range[0] || r.ts > state.range[1])) continue;
    if (r.layer === 'interior') {
      const hit = playVisitAt(frames, r.map_id, r.ts);
      if (!hit || !r.local) continue;
      out.push({ ts: r.ts, kind: 'respawn', where: hit.key,
                 xy: hit.f.tf(r.local[0], r.local[1]), plane: hit.f.plane,
                 label: r.label, after_s: r.after_s });
    } else if (r.xy) {
      out.push({ ts: r.ts, kind: 'respawn', where: 'world', xy: r.xy,
                 label: r.label, after_s: r.after_s, plane: planeOf(r.layer) });
    }
  }
  for (const w of (state.warpList || [])) {
    if (w.inside) {
      const hit = playVisitAt(frames, w.map_id, w.ts);
      if (!hit || !w.local) continue;
      out.push({ ts: w.ts, kind: 'warp', where: hit.key, label: w.map,
                 from: hit.f.tf(w.from_local[0], w.from_local[1]),
                 xy: hit.f.tf(w.local[0], w.local[1]), plane: hit.f.plane,
                 // Carried so the popup can say this was a death: the jump
                 // is what gets marked, and a transit's departure is not
                 // the row before its arrival.
                 from_ts: w.from_ts,
                 distance_m: w.distance_m, reason: w.reason });
    } else if (w.xy) {
      out.push({ ts: w.ts, kind: 'warp', where: 'world', label: w.map,
                 from: w.from_xy, xy: w.xy, plane: planeOf(w.layer),
                 from_ts: w.from_ts,
                 distance_m: w.distance_m, reason: w.reason });
    }
  }
  // A cave appears when you first go into it. The pins used to stand on the
  // map from the first frame of the playback, which gives away every place
  // you are about to find -- and a playback of a route is partly the finding.
  // One per map, at the first visit to it, so a cave you go back into does
  // not put a second pin on the map.
  const first = new Map();
  for (const v of (play.visits || [])) {
    if (!v.xy) continue;
    const seen = first.get(v.map_id);
    if (!seen || v.entered_ms < seen.entered_ms) first.set(v.map_id, v);
  }
  for (const v of first.values()) {
    if (state.range && (v.entered_ms < state.range[0]
                        || v.entered_ms > state.range[1])) continue;
    out.push({ ts: v.entered_ms, kind: 'found', where: 'world', xy: v.xy,
               label: v.label, map: v.map, world_visible: v.world_visible,
               plane: planeOf(v.plane) });
  }
  out.sort((a, b) => a.ts - b.ts);
  return out;
}

// Both planes. `state.drawn` holds only the one you are looking at, because
// the map shows one at a time -- so the underground was simply missing from
// the playback: on routes.db that is 4,506 samples and two hours of Siofra and
// Nokron, played as a hole in the route.
async function playWorld() {
  const p = new URLSearchParams({
    layers: 'surface,underground',
    epsilon: epsilonForZoom().toFixed(3),
  });
  // The window and the session filter used to come for free with state.drawn.
  // Asking for both planes means asking for them again, by hand.
  if (state.sessions.size) p.set('sessions', [...state.sessions].join(','));
  if (state.range) { p.set('t0', state.range[0]); p.set('t1', state.range[1]); }
  try {
    const data = await (await fetch('/api/route?' + p)).json();
    return data.segments || [];
  } catch (e) {
    return state.drawn || [];      // no recorder: play what is on screen
  }
}

async function buildPlayback() {
  const world = await playWorld();
  const visits = [];
  try {
    const data = await (await fetch('/api/interiors')).json();
    for (const v of (data.visits || [])) {
      if (inWindow(v)) visits.push(v);
    }
  } catch (e) { /* no recorder: the world half still plays */ }

  // All at once. One request per visit and fifty visits, asked for in a row,
  // is fifty round trips before the first frame of the playback -- long
  // enough that the button sat on "Building" and the whole thing read as
  // broken. They do not depend on each other, so they do not need to queue.
  play.visits = visits;
  const inner = [];
  const answers = await Promise.all(visits.map((v) => fetchInterior(v)));
  visits.forEach((v, i) => {
    const d = answers[i];
    if (d.ok && d.bounds && d.segments.length) inner.push({ v, d });
  });

  // The frames, before anything is drawn in them.
  //
  // `interiorTransform()` asks `state.dungeonFrame` which frame a dungeon is
  // drawn in, and that is learned by `loadInteriors()` -- which is the last
  // thing boot does. Press Play before it finishes and every cave is drawn by
  // pinning each visit's own first step to its own anchor instead, so a cave
  // whose first visit was placed by where you came *out* was drawn from the
  // wrong end. Pressing Play again later fixed it, which is exactly the shape
  // of a race. The paths are all cached by now, so this costs nothing and it
  // cannot be skipped.
  await learnDungeonFrame(visits);

  // One height ramp over the whole thing. Elevation is elevation, and giving
  // the caves their own extent would colour a flat cave floor as a mountain.
  const [lo, hi] = heightExtent(
    world.concat(...inner.map((x) => x.d.segments)));

  // Keyed by visit, not by map. `interiorTransform()` can hand back a
  // different frame for two visits to the same cave -- one walked in, one
  // warped to a grace inside it -- and keying by map ID meant whichever visit
  // came last in the list decided where that cave's deaths were drawn. The
  // one that lost put them on the entrance, which is what "the death played
  // at the mouth of the cave and not where I died" was.
  let runs = playRuns(world, 'world', null, lo, hi);
  const frames = new Map();
  for (const { v, d } of inner) {
    const tf = interiorTransform(v, d);
    const key = insideKey(v);
    frames.set(key, { tf, d, label: v.label, map_id: v.map_id,
                      world_visible: !!v.world_visible,
                      plane: planeOf(v.plane),
                      from: v.entered_ms, to: v.left_ms || Infinity });
    runs = runs.concat(playRuns(d.segments, key, tf, lo, hi, planeOf(v.plane)));
  }
  runs.sort((a, b) => a.t[0] - b.t[0]);

  const events = playEvents(frames);

  const stamps = [];
  for (const r of runs) for (const t of r.t) stamps.push(t);
  for (const e of events) stamps.push(e.ts);
  play.frames = frames;
  // Events without a path are not a playback: there would be nothing to
  // walk, and the position mark has no first point to start from.
  if (!runs.length || stamps.length < 2) return null;
  return { runs, events, axis: buildAxis(stamps), frames };
}

/* --- drawing it ---------------------------------------------------------- */

// The run being walked is drawn as one line that is handed more points each
// step. Clearing the group it lives in takes that line off the map without
// telling the loop, which then goes on feeding points to a layer nobody can
// see -- so walking into a cave showed the position mark and nothing else
// until the run finished and was redrawn whole, minutes later. Forgetting it
// here makes the next step draw a fresh one.
// A legacy dungeon is drawn on the map at the place it occupies, so its path
// belongs to the map the way the world's does: it accumulates, it is never
// taken away to show something else, and nothing is dimmed to reveal it --
// dimming the world for a castle already drawn out in the open turns the
// lights off for nothing. A cave is under the terrain and only exists on
// screen while you are in it. `drawInside()` has drawn that distinction on
// the finished map from the beginning; the playback was treating every
// interior as a cave.
// Two visits to the same cave are the same place, for anything drawn in it.
// `playEnter()` has redrawn the earlier visits' paths since "going back into a
// cave should show the cave, not just this run"; the marks are the other half
// of that, and without it a cave you had died in eight times started counting
// again at one the moment you walked back through the door.
function playSamePlace(a, b) {
  if (a === b) return true;
  if (!a || !b || a === 'world' || b === 'world') return false;
  const fa = play.frames.get(a), fb = play.frames.get(b);
  return !!(fa && fb && fa.map_id === fb.map_id);
}

function playOpen(where) {
  if (where === 'world') return true;
  const f = play.frames && play.frames.get(where);
  return !!(f && f.world_visible);
}

// The route goes underground and the map goes with it, the way the live feed
// switches planes for a sample on the other one: being shown the plane you are
// not on is indistinguishable from the tracker having stopped.
function playPlane(plane) {
  if (state.plane === plane) return;
  state.plane = plane;
  for (const k of ['surface', 'underground']) {
    control('plane-' + k).classList.toggle('on', k === plane);
  }
  swapTiles(plane, control('plane-note'));
  playRedrawPlane(plane);
}

// What is drawn, once the map underneath has changed. Its own function
// because the tiles can be swapped by the playback following the route or by
// you pressing Underground, and both have to take the drawing with them:
// pressing it by hand swapped the terrain and left the whole of Limgrave
// standing over Siofra with the river's own stretches never drawn, which is
// the same mistake `state.live` made before it and the one `playPlane()` was
// already written to avoid.
function playRedrawPlane(plane) {
  if (!play.on || !play.marks) return;
  // The path belongs to the plane it was walked on. Leaving the surface route
  // drawn over Siofra is the mistake `state.live` once made, and it is worse
  // here: the whole of Limgrave was drawn on the underground map, and the
  // river's own twenty-one stretches were lost in it.
  play.group.clearLayers();
  play.head = null;
  for (let i = 0; i < play.cursor; i++) {
    const run = play.runs[i];
    // Cave paths live in insideGroup and come and go with the cave.
    if (!playOpen(run.where)) continue;
    if ((run.plane || 'surface') !== plane) continue;
    playLine(run, run.xy.length);
  }
  play.lines = play.lines.filter((i) => i.line._map);
  // A mark belongs to the plane it was made on. The ones for the plane being
  // left come off with it and the ones for the plane arrived at go back up,
  // so a tally carries across a trip underground the way it carries across a
  // trip into a cave. The pins with them, hence clearing `pinned`.
  play.marks.clearLayers();
  play.pinned.clear();
  play.spots = play.spots.filter((sp) => !sp.key.endsWith(':w'));
  for (let i = 0; i < play.event; i++) {
    const e = play.events[i];
    if (!playOpen(e.where)) continue;
    if (e.plane && e.plane !== plane) continue;
    playFlash(e, false);
  }
}

function playDropHead() {
  // Only a head drawn into insideGroup, which is what was just cleared. One
  // in play.group is still on the map, and forgetting it would leave the
  // stretch drawn twice.
  if (play.head && !playOpen(play.head.run.where)) play.head = null;
}

// Where you are at a given moment, from the visits rather than from the path.
//
// The run loop announces a dungeon by drawing a run in it, which works right
// up until a visit has no path to draw: twenty seconds in a tunnel with a
// single sample stored produces no run at all, so the playback never entered
// it, and a death marked in there was never drawn -- the toll counted it and
// nothing appeared, because playFlash() refuses to draw an interior mark
// while the playback is somewhere else. A visit is a window in time and knows
// its own bounds, so this asks the question directly.
function playPlaceAt(now) {
  for (const [key, f] of play.frames) {
    if (now >= f.from && now <= f.to) return key;
  }
  return 'world';
}

// Which place the drawing is currently in. Coming out of a dungeon puts the
// world back; going into one dims the world and clears the space the dungeon
// is drawn in, exactly as hovering its marker does.
// `skip` is the event about to be flashed by the caller. The event loop has
// already stepped its cursor past it, so without this the mark it is about to
// make would also be replayed here and counted twice.
function playEnter(where, skip) {
  if (play.where === where) return;
  play.where = where;
  // Whatever a cave had on screen belongs to the cave you have left.
  // Unconditionally, not only when we knew we were inside one: a replay
  // draws every visit's runs into this group on its way past, and if it ends
  // up outside there is nobody else to clear them.
  insideGroup.clearLayers();
  play.insideMarks.clearLayers();
  playDropHead();
  // Those marks are gone, so the spots that were counting them have to go
  // too -- otherwise the next visit to that cave adds to a tally whose
  // marker is no longer on the map.
  play.spots = play.spots.filter((sp) => !sp.key.endsWith(':i'));
  if (playOpen(where)) {
    // The world, or a dungeon that is part of it. Nothing to reveal and
    // nothing to put back: the path simply carries on into the castle.
    dimBackground(false);
    return;
  }
  dimBackground(true);
  const frame = play.frames.get(where);
  // Entering draws everything known about the place so far, this visit
  // included. That makes it idempotent, which is what lets the run loop stop
  // announcing dungeons: a replay can pile every visit's runs into one group
  // and this puts back exactly the right set at the end.
  if (frame) {
    for (let i = 0; i < play.cursor; i++) {
      const run = play.runs[i];
      if (run.where === 'world') continue;
      const f = play.frames.get(run.where);
      if (!f || f.map_id !== frame.map_id) continue;
      // Not faint. Drawing the earlier runs at 0.4 was meant to keep this
      // visit legible over them, and reads instead as the place fading out
      // every time you step back into it: they are the same cave and the
      // same walking, and there is nothing to rank one above the other.
      playLine(run, run.xy.length, false);
    }
  }
  // And everything that happened in here on the way through, so a tally goes
  // on from where it was rather than starting again. Drawn without animation:
  // these are not arriving now, they are already here.
  for (let i = 0; i < play.event; i++) {
    if (i === skip) continue;
    const e = play.events[i];
    if (e.where === 'world' || markKind(e) === 'found') continue;
    if (!playSamePlace(where, e.where)) continue;
    playFlash(e, false);
  }
  // No caption. Hovering a dungeon on the finished map is a question you
  // asked and the caption is the answer; the playback walks into one every
  // few seconds without being asked, and a box of text appearing and going
  // again at the bottom of the map is something to read rather than
  // something to watch. What it said is on the hover, where it was asked
  // for.
}

// Where a stretch sits on the age ramp *now* -- measured against the
// playhead, not against the end of the route. So the newest thing drawn is
// always in the newest colour, and what came before it slides down the ramp
// as the playback goes on.
function playAgeAt(run) {
  return play.rankNow > 0
    ? Math.max(0, Math.min(1, run.rank / play.rankNow)) : 1;
}

// Not one of the twelve bands the finished map is drawn in. A band is a fact
// about a route that has stopped changing; during playback the denominator
// moves under every stretch at once, so twelve steps meant the whole path
// changed colour together a few times a minute -- which is the snap. The ramp
// is continuous here, and the only quantisation left is the eight bits the
// colour is written in.
function playColour(run) {
  return state.tint === 'age' && run.rank !== undefined
    ? ageColor(playAgeAt(run)) : run.colour;
}

// Bring the drawn stretches up to date with where the playhead is. Only worth
// doing for the age ramp -- height and one colour do not move.
//
// Every tick rather than four times a second, because that is what makes it a
// drift rather than a step. It costs less than the throttle saved: a stretch
// whose colour rounds to the same rgb() as last time is skipped, so what the
// pass actually does per tick is recompute a number for each drawn stretch
// and set a style on the few that crossed a value.
const PLAY_RECOLOUR_MS = PLAY_TICK_MS;

function playRecolour(force) {
  if (state.tint !== 'age') return;
  const now = Date.now();
  if (!force && now - play.tinted < PLAY_RECOLOUR_MS) return;
  play.tinted = now;
  let gone = 0;
  for (const item of play.lines) {
    if (!item.line._map) { gone++; continue; }
    const colour = playColour(item.run);
    if (colour === item.colour) continue;
    item.colour = colour;
    item.line.setStyle({ color: colour });
  }
  if (gone) play.lines = play.lines.filter((i) => i.line._map);
}

function playLine(run, upto, faint, tip) {
  const open = playOpen(run.where);
  // Told not to follow the route between maps, the map stays where it was put
  // -- and Siofra's path does not get drawn on Limgrave, which is the whole
  // reason the two are separate maps rather than two layers.
  if (open && (run.plane || 'surface') !== state.plane) return null;
  const raw = run.xy.slice(0, upto);
  if (tip) raw.push(tip);
  const pts = raw.map(toLatLng);
  const style = {
    renderer: open ? renderer : insideRenderer,
    pane: open ? undefined : 'inside',
    color: playColour(run),
    weight: open ? run.weight : run.weight + 0.6,
    opacity: 0.95,
    lineJoin: 'round',
    lineCap: 'round',
  };
  // An earlier run through the same place: there to be recognised, not
  // followed, so it goes under the one being walked rather than competing
  // with it. Never for a legacy dungeon, whose path is simply the map's.
  if (faint && !open) {
    style.weight = Math.max(1, run.weight - 0.5);
    style.opacity = 0.4;
  }
  const line = L.polyline(pts, style);
  line.addTo(open ? play.group : insideGroup);
  play.lines.push({ run, line, colour: style.color });
  return line;
}

const PLAY_GLYPH = { death: '\u2620', warp: '\u2726', respawn: '\u2739' };

// A jump whose middle is a dungeon was drawn in its own colours for a while,
// on the reasoning that walking in one mouth and out of the far one is not a
// teleport. Which is true of some of them and not of others -- once the
// tunnel was drawn at the door it is actually used, the one that prompted
// that distinction turned out to be a warp back to the cave with a walk
// through it either side. There is no way to tell those apart from here, so
// they are all teleports again and the popup says what the recorder saw.
function markKind(e) {
  return e.kind;
}
const PLAY_SPOT_PX = 12;

// One mark per spot per kind, counting up, rather than one per event stacked
// on the same pixel. The map clusters its marks for the same reason; the
// difference here is that the marks arrive one at a time, so the cluster has
// to be built as it goes rather than from a finished list.
function playMark(kind, xy, pane, into, popup) {
  const key = `${kind}:${into === play.marks ? 'w' : 'i'}`;
  let spot = null;
  for (const s of play.spots) {
    if (s.key === key && Math.abs(s.xy[0] - xy[0]) < PLAY_SPOT_PX
        && Math.abs(s.xy[1] - xy[1]) < PLAY_SPOT_PX) { spot = s; break; }
  }
  if (spot) {
    spot.n += 1;
    spot.at.push(popup);
    const size = 22;
    spot.mark.setIcon(L.divIcon({
      className: 'pop-holder',
      html: `<b class="${kind}-mark"><span>${PLAY_GLYPH[kind]}</span>`
            + `<i class="count">${spot.n}</i></b>`,
      iconSize: [size, size], iconAnchor: [size / 2, size / 2],
    }));
    return spot.mark;
  }
  const mark = L.marker(toLatLng(xy), {
    pane,
    icon: L.divIcon({
      className: 'pop-holder',
      html: `<b class="${kind}-mark"><span>${PLAY_GLYPH[kind]}</span></b>`,
      iconSize: [18, 18], iconAnchor: [9, 9],
    }),
    keyboard: false,
  }).addTo(into);
  play.spots.push({ key, xy, n: 1, mark, at: [popup] });
  return mark;
}

function playFlash(e, animate) {
  const kind = markKind(e);
  const where = playOpen(e.where);
  // Before anything is drawn, including a pin: a mark is only drawn on the
  // map it happened on. Below the `found` return, every cave on the surface
  // kept its pin standing over the black of Siofra.
  if (e.plane && e.plane !== state.plane) return;
  if (kind === 'found') return playPin(e, animate);
  // A mark made inside a dungeon is drawn in that dungeon's frame, and that
  // frame is only on the screen while you are in there -- so it goes up with
  // the dungeon and comes down with it, in its own group that playEnter()
  // clears alongside the path. Anywhere else it would be a mark floating over
  // the world at the cave's position, which is exactly what a teleporter in
  // Stormveil looked like: an arc still lying across Limgrave an hour after
  // you left. The world's own marks last, because the world is still there.
  // A mark made in a legacy dungeon is on the map like the castle it is in,
  // so it lasts the way the world's marks do rather than being cleared with
  // the overlay when you walk out.
  const home = where;
  if (!home && !playSamePlace(play.where, e.where)) return;
  const into = home ? play.marks : play.insideMarks;
  const pane = home ? 'playmarks' : 'playinside';
  if (kind === 'warp' && e.from) {
    // The end you left from, which stays. The map marks both ends of every
    // jump for the same reason: where you went is only half of a teleport.
    const left = playMark(kind, e.from, pane, into, e);
    left.bindPopup(() => playMarkPopup(e, 'from'));
    // Both ends and the line between them are the whole of what a teleport
    // is -- but drawn all at once they say "these two places are related",
    // not "you went from this one to that one". So it happens in order: the
    // end you left from lands, a line runs from it to where you arrived, and
    // the arrival lands when the line gets there.
    // The line is the journey being made, so it belongs to the moment and
    // not to the picture. Drawn on a seek as well, it was left lying across
    // the map -- and scrubbing, which replays every event up to where you
    // land, strung one across for every teleport in the route.
    //
    // Its own canvas, so drawing it and fading it out cost one line's worth
    // of repaint rather than the whole route's.
    if (animate) {
      // The map is about to be sent somewhere it has never drawn, and a whole
      // screen of tiles arriving at once is what the jump feels like it is
      // waiting for. The line takes PLAY_TRAVEL_MS to get there, and the
      // clock is eased for at least as long again, so there is time to pay
      // for them before the map moves.
      warmAhead(e.xy);
      // And the map is sent there rather than shuffled the smallest distance
      // that gets it inside the follow box. Following is right for walking --
      // a few pixels at a time, and the ground you came from stays on screen
      // -- and it is the wrong shape for a jump: the clock reaches the far
      // end in one step, so the minimum pan leaves the place you have just
      // arrived at pressed against whichever edge you came in by. The place
      // a teleport put you is the whole of what there is to look at, so it
      // goes in the middle.
      //
      // Armed here and applied when the line gets there, with the follow
      // held in between: the departure lands, the line sets off from where
      // you actually were, and the map arrives with it. Panning at once
      // would take the end you left from off the screen before it had
      // finished landing, which is the half of a jump this was all built to
      // show.
      // Only when the place it is taking you is not already on the screen.
      // A jump you can see both ends of needs no help finding: throwing the
      // map across for something that was already in front of you is the
      // whole of "if you're teleporting to somewhere close by it can look a
      // bit jarring". Left alone, the ordinary follow nudges it inside the
      // edge margin if it needs to and does nothing at all if it does not.
      if (state.follow && !map.getBounds().contains(toLatLng(e.xy))) {
        // Where on the screen the end you are leaving from sits. Kept now
        // rather than worked out on landing, because by then the follow has
        // had four hundred milliseconds to move the map under it.
        play.centre = { xy: e.xy, until: Date.now() + PLAY_TRAVEL_MS,
                        at: state.holdSpot
                          ? map.latLngToContainerPoint(toLatLng(e.from))
                          : null };
      }
      const arc = L.polyline([toLatLng(e.from), toLatLng(e.from)], {
        renderer: home ? arcRenderer : insideRenderer,
        pane: home ? 'playarc' : 'inside',
        color: '#8fb7cc', weight: 2, opacity: ARC_OPACITY, dashArray: '4,7',
      }).addTo(home ? play.group : insideGroup);
      playLand(left);
      playTravel(arc, e.from, e.xy);
      // It arrives over four hundred milliseconds and used to vanish between
      // one frame and the next, which reads as a glitch rather than as the
      // end of something.
      playFadeOut(arc, PLAY_TRAVEL_MS + PLAY_FLASH_MS, PLAY_ARC_FADE_MS,
                  home ? play.group : insideGroup);
    }
  }
  // The lasting mark, so the playback accumulates a record the way the map
  // does. The flash on top of it is the part that is only for the moment.
  const lasting = playMark(kind, e.xy, pane, into, e);
  lasting.bindPopup(() => playMarkPopup(e));
  // The arrival lands when the line reaches it, not when it sets off.
  if (animate) {
    const wait = kind === 'warp' && e.from ? PLAY_TRAVEL_MS : 0;
    // The map arrives when the mark does, off the same timer, so a pause
    // mid-jump still ends up looking at the place the jump went to.
    if (wait) setTimeout(() => { playLand(lasting); playCentre(); }, wait);
    else playLand(lasting);
  }
  // A popup you have to chase across the map is no use, so watching stops
  // while you are reading one.
  lasting.on('click', () => { if (play.timer) playPause(); });
}

// The mark arriving: too small to see, out past the size it will settle at,
// and back to it. Its own function because restarting a CSS animation on an
// element that already carries the class means taking the class off and
// reading the layout back before it goes on again -- and because a mark that
// counts up needs to do it again on every arrival, not only the first.
const PLAY_POP_MS = 420;
// The speed the flash lengths were judged at. Above it a flash covers more
// route than it is worth holding the drawing back for, so the hold shrinks in
// proportion; at or below it nothing changes.
const PLAY_HOLD_SPEED = 300;

function playLand(mark) {
  const el = mark && mark._icon;
  if (!el) return;
  el.classList.remove('landed');
  void el.offsetWidth;
  el.classList.add('landed');
  setTimeout(() => el.classList.remove('landed'), PLAY_POP_MS + 60);
}

// How long the line takes to get there. Long enough to be followed, short
// enough that the next thing does not have to wait for it.
const PLAY_TRAVEL_MS = 420;
const PLAY_ARC_FADE_MS = 480;     // and how long it takes to go again
const ARC_OPACITY = 0.8;

// Wait, then fade to nothing and take it off. Stepped on a timer like the
// travel above it, so a playback in a background tab does not come back with
// half-faded lines lying across the map.
function playFadeOut(line, after, ms, group) {
  line._group = group;
  setTimeout(() => {
    if (!line._map) return;
    const began = Date.now();
    const step = setInterval(() => {
      if (!line._map) { clearInterval(step); return; }
      const k = Math.min(1, (Date.now() - began) / ms);
      line.setStyle({ opacity: ARC_OPACITY * (1 - k) });
      // Off the group as well as off the map: `remove()` alone leaves it in
      // the group's own bookkeeping, holding a layer nothing will draw again.
      if (k >= 1) {
        clearInterval(step);
        if (line._group) line._group.removeLayer(line); else line.remove();
      }
    }, 40);
  }, after);
}

// The line running from one end of a jump to the other. Stepped on a timer
// rather than a frame callback: the canvas is redrawn per change either way,
// and a timer keeps going in a window nobody is painting, so a playback left
// in a background tab does not come back with half-drawn jumps strung across
// the map.
function playTravel(line, from, to) {
  const began = Date.now();
  const step = setInterval(() => {
    if (!line._map) { clearInterval(step); return; }
    const k = Math.min(1, (Date.now() - began) / PLAY_TRAVEL_MS);
    // Quick off the mark and easing in, so it reads as leaving somewhere
    // rather than as a bar filling up.
    const e = 1 - Math.pow(1 - k, 2);
    line.setLatLngs([toLatLng(from),
                     toLatLng([from[0] + (to[0] - from[0]) * e,
                               from[1] + (to[1] - from[1]) * e])]);
    if (k >= 1) clearInterval(step);
  }, 25);
}

// Put the arrival of a jump in the middle of the screen. Never animated, for
// the reason the follow is not: the playback ticks every 16 ms and Leaflet
// stops a running pan to start another, so an eased one would be restarted
// before it could ever finish. Straight there, in one frame, is what the
// tiles were warmed for.
//
// `setView` rather than `panTo`, and the difference is not cosmetic: `panTo`
// puts the options under `pan` and leaves `animate` undefined at the top
// level, so `panBy` animates after all. `setView` copies the flag down.
function playCentre() {
  const c = play.centre;
  play.centre = null;
  if (!c || !play.on || !state.follow) return;
  const ll = toLatLng(c.xy);
  const z = map.getZoom();
  if (!c.at) {
    map.setView(ll, z, { animate: false });
    return;
  }
  // Asked for: "if you teleport from the bottom left of the screen, then the
  // teleport-to marker should also be the bottom left of the screen, so you
  // stay focused on the same spot while viewing."
  //
  // Clamped into the edge margin first. The departure is normally well inside
  // it -- the follow keeps it there -- but a jump can fire on the frame the
  // mark is still crossing it, and landing the arrival off the screen to
  // honour where the departure was would be the opposite of the point.
  const size = map.getSize();
  const pad = effectivePad();
  const at = L.point(
    Math.min(Math.max(c.at.x, pad[0]), size.x - pad[0]),
    Math.min(Math.max(c.at.y, pad[1]), size.y - pad[1]));
  // One move, not a centring followed by a nudge: the container point of a
  // projected point p is `p - project(centre) + size/2`, so the centre that
  // puts the arrival at `at` is `project(arrival) - at + size/2`.
  const centre = map.project(ll, z).subtract(at).add(size.divideBy(2));
  map.setView(map.unproject(centre, z), z, { animate: false });
}

// A cave you have just walked into. Not draggable and not renameable the way
// the map's own pin is -- this one is a record of having got there.
function playPin(e, animate) {
  if (play.pinned.has(e.map)) return;
  play.pinned.add(e.map);
  const mark = L.marker(toLatLng(e.xy), {
    pane: 'playmarks',
    icon: L.divIcon({
      className: 'pop-holder',
      html: `<b class="${e.world_visible ? 'cave-mark keep-mark' : 'cave-mark'}">`
            + `<span>${e.world_visible ? '\u265C' : '\u25B2'}</span></b>`,
      iconSize: [24, 24], iconAnchor: [12, 31], popupAnchor: [0, -31],
    }),
    keyboard: false,
  }).addTo(play.marks);
  mark.bindPopup(`<b>${e.label}</b><br>${e.map}<br>` +
                 `First went in ${new Date(e.ts).toLocaleString()}`);
  if (animate) playLand(mark);
}

// What a mark says when you click it during playback, and what you can do
// about it. This is the same correction the map already offers on a teleport
// -- but the playback is where you can see the thing that gives it away:
// jump, and then walk straight back to where you jumped from, and you died
// there. A teleport takes you somewhere you meant to go.
function playMarkPopup(e, end) {
  const when = new Date(e.ts).toLocaleString();
  if (e.kind === 'respawn') {
    const el = document.createElement('div');
    el.innerHTML = `<b>Got up here</b><br>${e.label}<br>${when}<br>` +
                   `<span class="hint">${e.after_s}s after dying</span>`;
    return el;
  }
  if (e.kind === 'warp') {
    const km = (e.distance_m / 1000).toFixed(2);
    return popupWithAction(
      `<b>${end === 'from' ? 'Left from here' : 'Teleported'}</b><br>` +
      `${km} km to ${e.label}<br>${when}<br>` +
      `<span class="hint">${e.reason}` +
      ` \u2014 which is what a death looks like too</span>`,
      'This was a death',
      () => { map.closePopup();
                playReclassify({ ts: e.ts, from_ts: e.from_ts }); });
  }
  if (e.by_hand) {
    return popupWithAction(
      `<b>Died</b><br>${e.label}<br>${when}<br>` +
      `<span class="hint">marked by hand</span>`,
      'Not a death after all',
      () => { map.closePopup(); playReclassify({ ts: e.ts, clear: true }); });
  }
  const el = document.createElement('div');
  el.innerHTML = `<b>Died</b><br>${e.label}<br>${when}<br>` +
                 `<span class="hint">your health reached zero</span>`;
  return el;
}

// Where a moment sits on the playback's own axis. The inverse of
// playClock(), and needed because a rebuild changes the axis: the way to come
// back to where you were watching is to remember the wall clock and look it
// up again afterwards.
function playElapsedFor(ts) {
  const a = play.axis;
  if (!a || !a.ts.length) return 0;
  const i = axisIndex(a.ts, ts);
  if (i >= a.ts.length - 1) return a.el[a.el.length - 1];
  return a.el[i] + Math.min(ts - a.ts[i], a.el[i + 1] - a.el[i]);
}

// One recorded point at a time, in either direction.
//
// Playing at five minutes a second goes past a death in a fiftieth of a
// second, so "watch for the moment and press the button" only works if you
// can also walk up to it. The axis is every timestamp in the playback in
// order, so stepping along it is stepping along the path a point at a time.
function playStep(dir) {
  if (!play.on) return;
  playPause();
  const a = play.axis;
  let i = axisIndex(a.el, play.at);
  // A click forward from mid-step should land on the next point, not on the
  // one already behind the mark.
  if (dir > 0 && a.el[i] <= play.at) i += 1;
  else if (dir < 0) i -= (a.el[i] < play.at ? 0 : 1);
  i = Math.max(0, Math.min(a.el.length - 1, i));
  playSeek(a.el[i]);
}

// Rebuild the whole script and come back to the same moment.
//
// Marking a death breaks the line at the sample after it, and the route is
// simplified and split on breaks by the server -- so the path itself changes
// shape, not just the marks on it. Rebuilding only the events would leave the
// line still drawn from the body to the grace, which is the thing being
// corrected.
async function playRebuild(atClock) {
  await reload();
  const script = await buildPlayback();
  if (!script) return;
  play.runs = script.runs;
  play.events = script.events;
  play.axis = script.axis;
  play.frames = script.frames;
  play.to = script.axis.total;
  // Marking a death rebuilds the axis, and it can come out a little shorter
  // or longer. The trim is kept where it was in real terms and clamped into
  // whatever the new axis turned out to be.
  play.until = Math.min(play.until || play.to, play.to);
  play.from = Math.max(0, Math.min(play.from, play.until));
  playTicks();
  playTrimUI();
  playSeek(playElapsedFor(atClock));
}

// "I died here", for a death that left no mark to correct.
//
// Everything else in this file works by finding something the recorder
// noticed and asking which of two things it was. That runs out: a death in a
// cave whose grace is seven metres away leaves a hole in the recording and
// nowhere near enough displacement for any rule to separate it from standing
// still, so there is no mark, and nothing to click. Measured over thirteen
// deaths reported by hand, every one sits in a hole of 8.3 s or more -- but
// so do 435 stretches where somebody simply stood still, and 435 wrong
// teleports would be worse than the eleven right ones are worth.
//
// So the last resort is you. Watch it back, and when you see the moment, say
// so: the death goes on the sample nearest the point on screen.
async function playDiedHere() {
  if (!play.on) return;
  playPause();
  const when = playClock(play.at);
  const done = await callDeath({ ts: when, at: true });
  if (!done) return;
  await playRebuild(when);
  // A recorder from before this existed answers without the field and marks
  // the death alone -- no grace, and the line still drawn from one to the
  // other. The version banner says the two halves disagree; this says which
  // half of what you just pressed did not happen.
  if (!('respawn_ts' in done)) {
    setStats('Death marked, but this recorder is too old to also mark where '
             + 'you got up. Restart it and mark it again.');
    return;
  }
  if (done.respawn_ts === null) {
    setStats('Death marked. Nothing was recorded after it in that session, '
             + 'so there is no grace to mark.');
    return;
  }
  setStats(`Death marked at ${new Date(when).toLocaleTimeString()}, with the `
           + `next recorded point as the grace you got up at. Click the mark `
           + `to take both back.`);
}

// Changing a mark changes both lists, and the playback is built from them.
// Only the events are rebuilt: marking a death writes its own event and does
// not touch a single break, so the path is exactly the path it was. Then it
// replays to where it was, which puts the corrected mark on screen at the
// position you were watching.
async function playReclassify(body) {
  const when = playClock(play.at);
  if (!(await callDeath(body))) return;
  await playRebuild(when);
  setStats(body.clear ? 'Back to a teleport.'
                      : 'Marked as a death; the respawn is at the other end.');
}

function playRepeatUI() {
  const rep = control('play-repeat');
  if (rep.classList) rep.classList.toggle('on', play.loop);
  if (rep.setAttribute) rep.setAttribute('aria-pressed', String(play.loop));
}

// The two other modes on the bar. A pressed state rather than a label that
// changes: what they show is the way the playback is set.
function playModesUI() {
  const rev = control('play-rev');
  if (rev.classList) rev.classList.toggle('on', play.dir < 0);
  if (rev.setAttribute) rev.setAttribute('aria-pressed', String(play.dir < 0));
  const tk = control('play-ticks-on');
  if (tk.classList) tk.classList.toggle('off', !play.ticksOn);
  if (tk.setAttribute) tk.setAttribute('aria-pressed', String(play.ticksOn));
  const box = control('play-ticks');
  if (box.hidden !== undefined) box.hidden = !play.ticksOn;
}

function playToll(n, animate) {
  const rose = n > play.deaths;
  play.deaths = n;
  control('toll-n').textContent = String(n);
  const box = control('toll');
  // Only on the way up, and only while the clock is running. A seek is a
  // reset followed by a replay of every death up to the target, so the count
  // goes 0 -> N on every single one -- which is a rise, and had the whole
  // badge flaring through a scrub as if you were dying all the way along it.
  // The number still lands; it is the announcement that belongs to the moment.
  if (rose && animate && box.classList) {
    box.classList.remove('bump');
    // Reading offsetWidth is what restarts the animation: without it the
    // class goes back on in the same frame it came off and nothing replays.
    void box.offsetWidth;
    box.classList.add('bump');
  }
}

function playReset() {
  if (play.group) play.group.clearLayers();
  if (play.marks) play.marks.clearLayers();
  if (play.insideMarks) play.insideMarks.clearLayers();
  insideGroup.clearLayers();
  dimBackground(false);
  setCaption(null);
  play.cursor = 0;
  play.event = 0;
  play.head = null;
  play.where = null;
  play.pinned.clear();
  play.spots.length = 0;
  play.hold = null;
  play.shown.clear();
  play.here = null;
  play.onPlane = null;
  play.clocked = 0;
  play.brake = 0;
  play.centre = null;
  play.lines.length = 0;
  play.tinted = 0;
  playToll(0);
}

// Draw everything that has happened by `elapsed`. `animate` is off when this
// is catching up after a seek: the same picture, without fifty deaths all
// flashing at once.
function playDrawTo(elapsed, animate) {
  const now = playClock(elapsed);
  // The ramp is measured against the playhead, so this has to be right before
  // anything is drawn or coloured.
  play.rankNow = ageRank(now);
  // Where the clock says you are, written down as the runs are walked rather
  // than read back off the drawn line afterwards. The line is a drawing and
  // it comes and goes: `playEnter()` below clears the group the interior head
  // lives in, and `n === 1` draws no head at all, so reading the position out
  // of it fell back to the end of the *previous* run -- on 49% of steps
  // through `routes.db`, drifting to 106 m inside one legacy dungeon while
  // the mark sat still. Which is what put a death marked by hand a point away
  // from where the mark was standing: the death goes where the clock is.
  play.here = null;
  let pending = null;
  while (play.cursor < play.runs.length) {
    const run = play.runs[play.cursor];
    if (run.t[0] > now) break;
    if (run.t[run.t.length - 1] <= now) {
      playLine(run, run.xy.length);
      play.here = run.xy[run.xy.length - 1];
      play.onPlane = run.plane || 'surface';
      play.cursor++;
      play.head = null;
      continue;
    }
    // The run being walked right now: redrawn each step as it grows. Only
    // this one is ever redrawn, which is why a route of 40,000 points can be
    // played at all.
    let n = 1;
    while (n < run.t.length && run.t[n] <= now) n++;
    // Where the clock actually is, rather than the last point it went past.
    // Recorded points are a quarter of a second apart and the tick is a
    // sixtieth, so without this the head waits on the wrong side of a point
    // for several frames and then jumps to it.
    let tip = null;
    if (n < run.t.length) {
      const span = run.t[n] - run.t[n - 1];
      const u = span > 0 ? (now - run.t[n - 1]) / span : 0;
      if (u > 0.001) {
        const a = run.xy[n - 1], b = run.xy[n];
        tip = [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u];
      }
    }
    play.here = tip || run.xy[n - 1];
    play.onPlane = run.plane || 'surface';
    // `|| tip` and not `n > 1` alone: a run reached at its first point now has
    // a line to draw as soon as the clock is any way into it, instead of
    // nothing on screen until the second point is passed.
    //
    // Held rather than drawn here, because `playEnter()` below clears the
    // group an interior head lives in and then puts back only the runs that
    // are *finished* -- so the one being walked was drawn, cleared, and never
    // redrawn. While playing that costs a frame and nobody sees it; on a seek
    // `playDrawTo` is called once, and the result was a cave with a position
    // mark moving through it and no path behind it, until an event or a
    // finished run happened to put one back. Reported as exactly that, and as
    // "every cave has the scrub issue".
    if (n > 1 || tip) pending = { run, n, tip };
    break;
  }

  // The map the route is on right now. Before the place check, so a dungeon
  // reached on the other plane is entered with the right terrain behind it.
  if (state.autoPlane && play.onPlane && play.onPlane !== state.plane) {
    playPlane(play.onPlane);
  }

  // Where the clock says you are -- unless something just happened somewhere
  // else and is still being shown. See the hold below.
  const place = playPlaceAt(now);
  const holding = play.hold && Date.now() < play.hold.until;
  if (!holding) {
    // Which places the clock itself has put on screen, as opposed to ones an
    // event dragged the drawing into. The hold below is the only reason that
    // difference matters, and it is the whole of it.
    play.shown.add(place);
    if (place !== play.where) playEnter(place);
  }

  // Now that the drawing is in the right place, the run being walked can go
  // on top of it. Only this one is ever redrawn, which is what lets a route
  // of forty thousand points be played at all.
  if (pending) {
    const { run, n, tip } = pending;
    if (play.head && play.head.run === run) {
      const pts = run.xy.slice(0, n);
      if (tip) pts.push(tip);
      play.head.line.setLatLngs(pts.map(toLatLng));
    } else {
      const line = playLine(run, n, false, tip);
      play.head = line ? { run, line } : null;
    }
  }

  let died = play.deaths;
  while (play.event < play.events.length && play.events[play.event].ts <= now) {
    const e = play.events[play.event++];
    // A mark made in a dungeon can only be drawn while that dungeon is on
    // screen, and at five minutes a second one step of the playback covers
    // twenty-four seconds -- so a twenty-second visit to a tunnel is crossed
    // entirely between two frames and a death marked in there was created
    // and cleared without ever being drawn. The event takes the place it
    // happened in, and holds it for as long as its flash lasts, so the thing
    // it is announcing can actually be seen.
    //
    // Only while playing. A seek replays every event up to the target, and
    // letting each one take the place left the drawing wherever the last
    // interior event happened to be -- a cave chosen by nothing, with the
    // whole map dimmed behind it, which is what "it randomly darkens
    // everything and selects a random cave" was. The place check above
    // already ran, so on a seek the place is where the clock says and the
    // marks that belong there are the ones drawn.
    // A pin is drawn on the world map -- playFlash() returns before it ever
    // reaches the interior branch -- so holding a dungeon on screen for one
    // shows nothing and only keeps the world dark.
    if (animate && e.where !== 'world' && markKind(e) !== 'found') {
      // The hold is set whether or not we had to move to get here. Setting
      // it only when the place changed meant the common case missed it: the
      // step that reaches the dungeon usually reaches it through the clock,
      // so the event found itself already in the right place, set no hold,
      // and the next step -- twenty-four seconds later -- left again after
      // eighty milliseconds on screen.
      if (e.where !== play.where) playEnter(e.where, play.event - 1);
      // Two lengths, for two different questions. A place the clock stepped
      // over is on screen for this hold and never again, so it gets the whole
      // flash to be read in. A place the clock played through has been up all
      // along and only needs the mark to land -- holding that one for a full
      // flash is the world brightening a second and a half after you left,
      // with nothing happening in the meantime.
      //
      // It is not a rare case: at five minutes a second a step covers 24 s of
      // route time, and 25 of the 80 marks made inside a dungeon in routes.db
      // fall in the last step of the visit they belong to. Dropping the hold
      // outright would stop announcing all of them.
      // And measured against the route it lets pass, not against the wall.
      // A flash is a second and a half whatever the speed -- which at five
      // minutes a second is seven minutes of route and reads as intended,
      // and at an hour a second is an hour and a half of it. The whole route
      // is fifteen seconds at that speed, so holding the drawing for a flash
      // is holding it a tenth of the playback behind the clock: the dot is
      // out in the open and the world is still dark for the cave it left.
      // Reported as exactly that. Scaled below the speed the flash was
      // judged at, so nothing changes at or under it.
      const span = play.shown.has(e.where) ? PLAY_POP_MS : PLAY_FLASH_MS;
      play.hold = {
        until: Date.now()
               + span * Math.min(1, PLAY_HOLD_SPEED / (play.speed || 1)),
      };
    }
    // A teleport is the thing worth easing off for, so it is the thing that
    // does it -- the line takes 420 ms to draw itself across the map and the
    // marks at either end another beat after that, and at five minutes a
    // second the clock covers twenty-four seconds of route in every eighty
    // milliseconds. Without this the playback is somewhere else before the
    // jump has finished being drawn.
    if (animate && markKind(e) === 'warp') {
      play.brake = Math.max(play.brake, Date.now() + playBrakeFor(e));
    }
    playFlash(e, animate);
    if (e.kind === 'death') died++;
  }
  if (died !== play.deaths) playToll(died, animate);

  // Where you are, on whichever plane you are on.
  const at = playHeadPoint();
  if (at) {
    const ll = toLatLng(at);
    play.mark.setLatLng(ll);
    // Only on the map it is actually on. With the switch turned off the route
    // carries on underground while the map stays up here, and a dot wandering
    // over Limgrave would be saying something untrue.
    const showMark = !play.onPlane || play.onPlane === state.plane;
    if (showMark && !play.mark._map) play.mark.addTo(map);
    if (!showMark && play.mark._map) play.mark.remove();
    // Held while a jump is in the air, so the map does not creep towards the
    // arrival and then jump again when the line lands. playCentre() is what
    // ends it, and it clears the hold whether it moves the map or not.
    const flying = play.centre && Date.now() < play.centre.until;
    if (state.follow && !flying) {
      // Never animated, and that is the whole of what made following feel
      // choppy. Leaflet's animated `panBy` eases over 250 ms, and
      // `PosAnimation.run()` stops whatever is running before it starts --
      // so at a 16 ms tick each pan was cancelled and restarted fifteen
      // times before it could finish. The map was always part-way through
      // an ease that never completed: it lagged the mark and juddered while
      // it did. Checked in the running viewer -- an animation begun by one
      // tick is still in progress when the next tick's call arrives.
      //
      // Unanimated, `panInside` applies exactly the offset needed, in the
      // same frame as the mark it is following, so it cannot lag and there
      // is nothing to interrupt. A few pixels sixty times a second is what
      // a smooth pan is.
      //
      // It takes the leap case with it. `panBy` with `animate` not true
      // sends the map straight there when the offset is wider than the
      // viewport, which is what the measured-in-pixels leap test was
      // written to do -- Leaflet was always going to do it for us.
      map.panInside(ll, { padding: effectivePad(), animate: false });
    }
  }
  // Last, so everything drawn this step is included, and forced on a seek so
  // scrubbing shows the colours of the moment it lands on rather than the
  // ones from a quarter of a second ago.
  playRecolour(!animate);
  // The clock is read, not watched. Rewriting it and the scrub thumb sixty
  // times a second is DOM work for something the eye cannot follow anyway;
  // ten is plenty. A seek is not throttled -- it has to land where it landed.
  const wall = Date.now();
  if (!animate || wall - play.clocked > 100) {
    play.clocked = wall;
    playClockText(elapsed, now);
  }
}

// The speed slider runs from 30 seconds a second to an hour a second. Not a
// linear range over that: the useful values span two orders of magnitude, so
// a linear thumb spends most of its travel above half an hour, where every
// position looks the same. These are detents, each about half again the last,
// so everywhere the thumb can stop is a speed worth watching at.
const PLAY_SPEEDS = [30, 45, 60, 90, 120, 180, 300, 450, 600,
                     900, 1200, 1800, 2700, 3600];
const PLAY_SPEED_DEFAULT = 300;

// How much route goes past in a second, which is the thing you are actually
// choosing. The multiplier says the same in fewer characters and it is the
// worse reading of the two: nothing else on the screen is in multiples of
// real time, so x300 has to be converted before it means anything, while
// "5 min a second" is already the answer. The multiplier is on the hover.
//
// Not rounded to whole minutes: three of the fourteen detents are halves, so
// rounding would have 90 read as "2 min" next to a hover saying x90.
function speedLabel(s) {
  if (s < 60) return `${s} sec a second`;
  if (s === 3600) return '1 hour a second';
  return `${+(s / 60).toFixed(1)} min a second`;
}

// And how many times faster than you walked it, for the hover: 300 seconds of
// route in a second is 300x real time.
function speedLong(s) {
  return `\u00d7${s} real time`;
}

// The stored preference is still seconds a second, because that is what it
// always meant -- only the control changed. An old value the ladder does not
// have (the select offered 2 hours) lands on the nearest one it does.
function nearestSpeed(s) {
  let best = 0;
  for (let i = 1; i < PLAY_SPEEDS.length; i++) {
    if (Math.abs(PLAY_SPEEDS[i] - s) < Math.abs(PLAY_SPEEDS[best] - s)) best = i;
  }
  return best;
}

function playHeadPoint() {
  return play.here;
}

// The moment on top, what it is out of underneath. Two elements rather than
// one string, so nothing writing to the clock can blow the other line away.
function setClock(when, pos) {
  control('play-when').textContent = when;
  control('play-pos').textContent = pos;
}

// One thin red line per death, laid on the timeline so you can see them
// coming. The thumb travels between its own half-widths, so the ticks are
// inset by the same amount or they would not line up with it.
const PLAY_THUMB_PX = 14;

// How near the cursor has to be to a recorded point to be asking about it.
const PLAY_HOVER_PX = 14;

// How near the cursor is to the line between two recorded points, and how
// far along it. Points, not segments, was the first try -- and it meant that
// anywhere between two of them, which at a coarse zoom is most of the path,
// the hover found nothing at all.
function nearSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const len = dx * dx + dy * dy;
  let u = len ? ((px - ax) * dx + (py - ay) * dy) / len : 0;
  u = u < 0 ? 0 : u > 1 ? 1 : u;
  const ex = px - (ax + dx * u), ey = py - (ay + dy * u);
  return [ex * ex + ey * ey, u];
}

// Which moment the path under the cursor belongs to.
//
// Hit-tested here rather than through Leaflet's own layer events, for two
// reasons: the interior overlay's pane is `pointer-events: none` -- it has to
// be, or the canvas swallows every click on the map -- so a run drawn in a
// dungeon would never receive one; and what is wanted is the nearest recorded
// *point*, since that is what carries the timestamp, not the nearest line.
//
// Only what is actually drawn is searched, which is also what you can see.
function playMomentAt(latlng) {
  if (!play.on || !play.axis || !play.lines) return null;
  const p = map.project(latlng, state.nativeZoom);
  const scale = Math.pow(2, map.getZoom() - state.nativeZoom);
  const near = PLAY_HOVER_PX / scale;
  let best = null;
  for (const item of play.lines) {
    if (!item.line._map) continue;
    const b = item.run.box;
    if (!b || p.x < b[0] - near || p.x > b[2] + near
        || p.y < b[1] - near || p.y > b[3] + near) continue;
    const { xy, t } = item.run;
    if (xy.length === 1) {
      const dx = xy[0][0] - p.x, dy = xy[0][1] - p.y;
      const d = dx * dx + dy * dy;
      if (d <= near * near && (!best || d < best.d)) best = { d, ts: t[0] };
      continue;
    }
    // Every segment, with the moment read off where along it the cursor
    // falls: the clamp at either end covers the corners too, so the points
    // need no pass of their own.
    for (let i = 0; i + 1 < xy.length; i++) {
      const [d, u] = nearSegment(p.x, p.y, xy[i][0], xy[i][1],
                                 xy[i + 1][0], xy[i + 1][1]);
      if (d <= near * near && (!best || d < best.d)) {
        best = { d, ts: Math.round(t[i] + (t[i + 1] - t[i]) * u) };
      }
    }
  }
  return best;
}

function playHover(latlng) {
  const el = control('play-hover');
  const hit = latlng ? playMomentAt(latlng) : null;
  play.hoverTs = hit ? hit.ts : null;
  // The map says what a press would do before you press it.
  control('map').classList.toggle('on-path', !!hit);
  if (!hit) { el.hidden = true; return; }
  const at = play.to ? playElapsedFor(hit.ts) / play.to : 0;
  el.style.left = playTrackAt(at);
  el.dataset.when = new Date(hit.ts).toLocaleString();
  el.title = `Go to ${new Date(hit.ts).toLocaleString()}`;
  el.hidden = false;
}

function playGoToHover() {
  if (play.hoverTs === null || play.hoverTs === undefined) return;
  playSeek(playElapsedFor(play.hoverTs));
}

// Where on the track a fraction of the route falls. The thumb travels between
// its own half-widths, so everything drawn on the track is inset by the same
// amount or it lines up with nothing.
function playTrackAt(f) {
  return `calc(${PLAY_THUMB_PX / 2}px + `
         + `(100% - ${PLAY_THUMB_PX}px) * ${Math.max(0, Math.min(1, f)).toFixed(5)})`;
}

// The two grips, and the shading over the part of the route they cut off.
function playTrimUI() {
  const a = play.to ? play.from / play.to : 0;
  const b = play.to ? play.until / play.to : 1;
  const grip = (id, f) => { const el = control(id); if (el.style) el.style.left = playTrackAt(f); };
  grip('trim-a', a);
  grip('trim-b', b);
  const cutA = control('trim-cut-a');
  const cutB = control('trim-cut-b');
  if (cutA.style) cutA.style.width = playTrackAt(a);
  if (cutB.style) cutB.style.left = playTrackAt(b);
  const box = control('play-trim');
  // Nothing trimmed is the ordinary case, and a shading of zero width either
  // side of it should not put two bright handles on a bar that means nothing
  // yet -- so the whole thing sits back until it has something to say.
  if (box.classList) box.classList.toggle('whole', a <= 0 && b >= 1);
}

// One grip moved, as a fraction of the whole route.
//
// They may not cross, and they may not come closer than the width of a thumb:
// two grips on one pixel cannot be told apart, and a window narrower than the
// thumb is one you cannot put the playhead inside.
function playSetTrim(which, f, span) {
  if (!play.to) return;
  const gap = span > 0 ? PLAY_THUMB_PX / span : 0.02;
  if (which === 'from') {
    play.from = Math.max(0, Math.min(f, play.until / play.to - gap)) * play.to;
  } else {
    play.until = Math.min(1, Math.max(f, play.from / play.to + gap)) * play.to;
  }
  playTrimUI();
  // The clock has to be somewhere the playback will actually go. Dragging a
  // grip past the playhead moves the playhead, rather than leaving it outside
  // the thing it is the playhead of.
  if (play.at < play.from || play.at > play.until) {
    playSeek(play.at < play.from ? play.from : play.until);
  } else {
    playClockText(play.at, playClock(play.at));
  }
}

function wireTrim() {
  const track = document.querySelector('.play-track');
  if (!track) return;
  for (const [id, which] of [['trim-a', 'from'], ['trim-b', 'until']]) {
    const grip = control(id);
    if (!grip.addEventListener) continue;
    const measure = (clientX) => {
      const r = track.getBoundingClientRect();
      const span = r.width - PLAY_THUMB_PX;
      let f = span > 0 ? (clientX - r.left - PLAY_THUMB_PX / 2) / span : 0;
      // Snapped at the ends, so putting a grip back where it started is a
      // flick rather than an exercise in single pixels.
      if (f < 0.01) f = 0;
      if (f > 0.99) f = 1;
      playSetTrim(which, f, span);
    };
    // A flag rather than the capture, and the moves come off the window. A
    // drag does not stay inside a nine-pixel handle, and pointer capture is
    // the browser's way of saying so -- but it is an optimisation here, not
    // the mechanism: asking `hasPointerCapture` made the drag depend on it,
    // and anything that cannot grant it (a synthetic event, an old browser)
    // got a grip that could be pressed and not moved.
    let held = false;
    const stop = (ev) => {
      if (!held) return;
      held = false;
      grip.classList.remove('held');
      try { grip.releasePointerCapture(ev.pointerId); } catch (e) { /* none */ }
    };
    grip.addEventListener('pointerdown', (ev) => {
      held = true;
      grip.classList.add('held');
      try { grip.setPointerCapture(ev.pointerId); } catch (e) { /* no capture */ }
      ev.preventDefault();
      ev.stopPropagation();
    });
    window.addEventListener('pointermove', (ev) => { if (held) measure(ev.clientX); });
    window.addEventListener('pointerup', stop);
    window.addEventListener('pointercancel', stop);
    // A grip you can reach with the keyboard, since it is a button and looks
    // like one. A step of a hundredth of the route, ten with shift.
    grip.addEventListener('keydown', (ev) => {
      const r = track.getBoundingClientRect();
      const span = r.width - PLAY_THUMB_PX;
      const now = (which === 'from' ? play.from : play.until) / (play.to || 1);
      const step = (ev.shiftKey ? 0.1 : 0.01);
      if (ev.key === 'ArrowLeft') playSetTrim(which, now - step, span);
      else if (ev.key === 'ArrowRight') playSetTrim(which, now + step, span);
      else if (ev.key === 'Home') playSetTrim(which, 0, span);
      else if (ev.key === 'End') playSetTrim(which, 1, span);
      else return;
      ev.preventDefault();
    });
  }
}

function playTicks() {
  const box = control('play-ticks');
  if (!box.replaceChildren) return;
  const marks = [];
  for (const e of (play.events || [])) {
    if (e.kind !== 'death' || !play.to) continue;
    const at = playElapsedFor(e.ts) / play.to;
    if (at < 0 || at > 1) continue;
    const i = document.createElement('i');
    i.style.left = playTrackAt(at);
    i.title = new Date(e.ts).toLocaleString();
    marks.push(i);
  }
  box.replaceChildren(...marks);
}

// How long the playback still has to run, in wall-clock milliseconds.
//
// The route left over the speed is only the half of it the clock controls.
// The playback also eases off at every teleport, running at PLAY_BRAKE_RATE
// for the length of each one, and that is not a rounding error: measured at
// an hour a second, where the whole route is 15.7 s of clock, the brakes add
// 28.4 s at zoom 3 and 52.5 s at zoom 8. Counting only the clock is why the
// number read low and then sat there refusing to reach zero.
//
// The brakes ahead are all computable, because a brake's length comes from
// how far its jump is on screen and nothing else. They are merged the way
// the tick merges them -- `play.brake` is a Math.max, so two jumps arriving
// together are one brake -- and each one pushes the ones after it further
// out, which is why this is a forward pass rather than a sum.
function playTimeLeft(elapsed) {
  const wall = Math.max(0, (play.until - elapsed) / play.speed);
  if (!play.events || !play.events.length) return wall;
  let extra = 0;     // wall ms the easing ahead will add
  let until = -1;    // how far the brake running at that point reaches
  for (let i = play.event; i < play.events.length; i++) {
    const e = play.events[i];
    if (markKind(e) !== 'warp') continue;
    const el = playElapsedFor(e.ts);
    if (el > play.until) break;      // past the end of what will be played
    const at = (el - elapsed) / play.speed + extra;
    if (at < 0) continue;
    const end = at + playBrakeFor(e);
    if (end <= until) continue;
    extra += (end - Math.max(until, at)) * (1 - PLAY_BRAKE_RATE);
    until = end;
  }
  return wall + extra;
}

// Said the way you would say it, because it is a length of time to sit
// through rather than a figure to compare: "2 min 34 seconds", not "154s".
function playLeftText(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s >= 3600) {
    const h = Math.floor(s / 3600);
    return `${h} hr ${Math.round((s % 3600) / 60)} min left`;
  }
  if (s >= 60) {
    const r = s % 60;
    return `${Math.floor(s / 60)} min ${r} second${r === 1 ? '' : 's'} left`;
  }
  return `${s} second${s === 1 ? '' : 's'} left`;
}

function playClockText(elapsed, now) {
  // How far through the *playback*, which is the window between the grips and
  // not the whole route: trim off the first three weeks and you want to know
  // how much of what you asked for is left, not how much of what you did not.
  const span = Math.max(1, play.until - play.from);
  const done = Math.max(0, Math.min(1, (elapsed - play.from) / span));
  const i = play.axis ? axisIndex(play.axis.el, elapsed) + 1 : 0;
  const of = play.axis ? play.axis.ts.length : 0;
  // Beside the play button rather than tucked on the end of the counting: it
  // is the one number you look at to decide whether to sit through the rest.
  control('play-left').textContent = playLeftText(playTimeLeft(elapsed));
  setClock(new Date(now).toLocaleString(),
           `${Math.round(done * 100)}% \u00b7 point ${i.toLocaleString()} of `
           + `${of.toLocaleString()}`);
  // The thumb, though, is placed against the whole route. The track is a
  // picture of everything recorded with a window drawn on it, so a thumb
  // measured against the window would sit somewhere the grips disagree with.
  const whole = play.to ? Math.max(0, Math.min(1, elapsed / play.to)) : 0;
  const scrub = control('play-scrub');
  if (!play.scrubbing) scrub.value = String(Math.round(whole * 1000));
  // Chrome will not fill the played part of a range input, so the track
  // paints it as a gradient stop and this is what moves it. Set from the
  // clock rather than from the drag, so it follows a seek as well. Two stops
  // now: the fill starts at the grip, because the route before it is not
  // something the playback has drawn.
  const start = play.to ? Math.max(0, Math.min(1, play.from / play.to)) : 0;
  scrub.style.setProperty('--start', `${(start * 100).toFixed(2)}%`);
  scrub.style.setProperty('--done', `${(whole * 100).toFixed(2)}%`);
}

/* --- the mode ------------------------------------------------------------ */

// What comes off the map while the playback is on -- the dungeon pins
// included. Leaving them up meant every cave you were about to find was
// already marked on the map from the first frame, which gives away the whole
// route; they are put back one at a time, as you walk into them.
function playLayers() {
  return [casingGroup, routeGroup, liveGroup, placedGroup,
          deathGroup, respawnGroup, warpGroup, markerGroup];
}

// One button for the mode, in the panel: it starts the playback and then
// says how to stop it. A second Done button on the bar was a second place to
// look for the same thing.
function togglePlayback() {
  if (play.on) exitPlayback(); else enterPlayback();
}

// The marker switches during playback. They belong to the finished map: the
// playback puts its marks down as they happen, out of its own script, and
// unticking Deaths while it runs clears a group it is not drawing into and
// changes nothing you can see. Off rather than absent, so the section still
// says what the map will look like when you come back to it.
function playLockMarks(on) {
  for (const id of ['l-interior', 'l-legacy', 'l-deaths', 'l-respawns',
                    'l-warps']) {
    const el = document.getElementById(id);
    if (el) el.disabled = on;
  }
}

function playButton(state) {
  const b = control('play');
  b.disabled = state === 'building';
  b.classList.toggle('on', state === 'playing');
  const label = b.querySelector ? b.querySelector('.lbl') : null;
  (label || b).textContent = state === 'building' ? 'Building\u2026'
                           : state === 'playing' ? 'Back to the map'
                           : 'Route Playback';
}

async function enterPlayback() {
  if (play.on) return;
  const token = ++play.token;
  playButton('building');
  const script = await buildPlayback();
  if (token !== play.token) return;
  if (!script) {
    playButton('idle');
    setStats('Nothing drawn to play back.');
    return;
  }
  playButton('playing');

  play.on = true;
  play.runs = script.runs;
  play.events = script.events;
  play.axis = script.axis;
  play.frames = script.frames;
  play.to = script.axis.total;
  // A fresh playback plays the whole route. Carrying a trim over from the
  // last one would be a setting you cannot see until you press play.
  play.from = 0;
  play.until = play.to;
  play.at = 0;
  playTrimUI();

  play.plane0 = state.plane;
  hideInside(true);
  stopLiveInside();
  for (const g of playLayers()) map.removeLayer(g);
  document.body.classList.add('playing');
  playLockMarks(true);
  play.group = L.layerGroup().addTo(map);
  play.marks = L.layerGroup().addTo(map);
  play.insideMarks = L.layerGroup().addTo(map);
  play.mark = L.marker(toLatLng(script.runs[0].xy[0]), {
    pane: 'you',
    icon: L.divIcon({
      className: 'play-mark', html: '<span>\u25CF</span>',
      iconSize: [18, 18], iconAnchor: [9, 9],
    }),
    keyboard: false, interactive: false,
  });
  control('timeline').hidden = false;
  control('toll').hidden = false;
  playTicks();
  playReset();
  setStats(`Playing ${script.runs.length} stretches of path, `
           + `${script.events.length} things that happened.`);
  playResume();
}

function exitPlayback() {
  if (!play.on) return;
  play.token++;
  playPause();
  play.on = false;
  playReset();
  if (play.mark && play.mark._map) play.mark.remove();
  if (play.group) { map.removeLayer(play.group); play.group = null; }
  if (play.marks) { map.removeLayer(play.marks); play.marks = null; }
  if (play.insideMarks) {
    map.removeLayer(play.insideMarks);
    play.insideMarks = null;
  }
  play.mark = null;
  // Back to the map you were looking at before, whatever the route ended on.
  if (play.plane0) playPlane(play.plane0);
  playButton('idle');
  control('timeline').hidden = true;
  control('toll').hidden = true;
  control('play-ticks').replaceChildren();
  playHover(null);
  document.body.classList.remove('playing');
  playLockMarks(false);
  for (const g of playLayers()) g.addTo(map);
  redrawEverything();
}

// The toggle is a glyph now, so what it does has to be said in the label
// rather than read off the face of it. U+FE0E keeps both characters as text:
// without it Chrome draws the pause sign as a colour emoji.
function playPause() {
  clearInterval(play.timer);
  play.timer = null;
  const b = control('play-toggle');
  b.innerHTML = '&#9654;&#65038;';
  b.title = 'Play (space)';
  b.setAttribute('aria-label', 'Play');
  b.classList.remove('on');
}

function playResume() {
  if (!play.on || play.timer) return;
  // At the end it has run to, whichever end that is.
  if (play.dir < 0 ? play.at <= play.from : play.at >= play.until) {
    playSeek(play.dir < 0 ? play.until : play.from);
  }
  const b = control('play-toggle');
  b.innerHTML = '&#9208;&#65038;';
  b.title = 'Pause (space)';
  b.setAttribute('aria-label', 'Pause');
  b.classList.add('on');
  play.last = Date.now();
  play.last0 = 0;
  play.timer = setInterval(() => {
    // Advanced by the time that actually passed, not by the nominal tick: a
    // timer asked for every 16 ms does not get every 16 ms, and stepping the
    // nominal amount makes the playback quietly slower than the speed it
    // claims. A gap longer than a quarter second is a tab that was in the
    // background, and stepping the whole of it would skip a piece of route.
    const wall = Date.now();
    let dt = wall - play.last;
    play.last = wall;
    // Capped, not reset: a late tick is still worth the time it took. Falling
    // back to the nominal tick here made the playback run at 86% of the speed
    // it claimed, because any tick that arrived late lost almost all of its
    // elapsed time. The cap is what stops a tab that was in the background
    // for a minute skipping a minute of route in one step.
    if (dt > 250) dt = 250;
    // Eased off after a teleport, rather than stopped: a playback that halts
    // reads as broken, one that slows reads as arriving.
    const rate = wall < play.brake ? PLAY_BRAKE_RATE : 1;
    if (play.dir < 0) {
      // Backwards is a seek a frame, and a seek on this route measures 69 ms
      // median and 117 at the far end. Left to itself that fills the thread
      // and the controls stop answering, so a tick is skipped until as long
      // again has passed as the last rebuild took: half the thread to the
      // playback, half to everything else. The clock is stepped by the wall
      // time either way, so what this costs is frames and not speed.
      if (wall - play.last0 < play.seekMs) return;
      play.last0 = wall;
      play.at = Math.max(play.from, play.at - play.speed * dt * rate);
      const t0 = performance.now();
      playSeek(play.at);
      play.seekMs = performance.now() - t0;
    } else {
      play.at = Math.min(play.until, play.at + play.speed * dt * rate);
      playDrawTo(play.at, true);
    }
    const done = play.dir < 0 ? play.at <= play.from : play.at >= play.until;
    if (done) {
      // Round again from the other grip -- whichever end that is. `playSeek()`
      // is a rebuild and not a rewind, and it leaves the timer alone, so this
      // is the whole of it.
      if (play.loop) playSeek(play.dir < 0 ? play.until : play.from);
      else {
        playPause();
        setClock('Finished',
                 `${play.deaths} death${play.deaths === 1 ? '' : 's'}`
                 + ' \u00b7 Play runs it again.');
      }
    }
  }, PLAY_TICK_MS);
}

function playSeek(elapsed) {
  playReset();
  // Into the window, not into the route. Dragging the thumb past a grip is
  // asking for a moment that is not part of this playback, and the honest
  // answer is the nearest one that is.
  play.at = Math.max(play.from, Math.min(play.until, elapsed));
  playDrawTo(play.at, false);
}

function wirePlayback() {
  wireTrim();
  // A mode, so it says which way it is set rather than what pressing it would
  // do -- the same reasoning as the plane buttons and the marker checkboxes.
  const rep = control('play-repeat');
  play.loop = pref('loop') === 'true';
  playRepeatUI();
  rep.addEventListener('click', () => {
    play.loop = !play.loop;
    savePref('loop', play.loop);
    playRepeatUI();
  });
  // Direction, and whether the deaths are drawn on the track. Both are modes
  // and both are remembered, for the same reason repeat is.
  play.dir = pref('dir') === '-1' ? -1 : 1;
  play.ticksOn = pref('ticks') !== 'false';
  playModesUI();
  control('play-rev').addEventListener('click', () => {
    play.dir = -play.dir;
    savePref('dir', play.dir);
    playModesUI();
  });
  control('play-ticks-on').addEventListener('click', () => {
    play.ticksOn = !play.ticksOn;
    savePref('ticks', play.ticksOn);
    playModesUI();
  });
  control('play').addEventListener('click', togglePlayback);
  control('play-died').addEventListener('click', playDiedHere);
  control('play-hover').addEventListener('click', playGoToHover);
  control('play-back').addEventListener('click', () => playStep(-1));
  control('play-fwd').addEventListener('click', () => playStep(1));
  control('play-toggle').addEventListener('click', () => {
    if (play.timer) playPause(); else playResume();
  });

  const speed = control('play-speed');
  const rate = control('play-rate');
  const saved = +pref('playSpeed');
  speed.max = String(PLAY_SPEEDS.length - 1);
  speed.value = String(nearestSpeed(saved || PLAY_SPEED_DEFAULT));
  const setSpeed = (save) => {
    play.speed = PLAY_SPEEDS[+speed.value] || PLAY_SPEED_DEFAULT;
    rate.textContent = speedLabel(play.speed);
    rate.dataset.long = speedLong(play.speed);
    speed.title = speedLong(play.speed);
    if (save) savePref('playSpeed', String(play.speed));
    if (play.on) playClockText(play.at, playClock(play.at));
  };
  setSpeed(false);
  // On input rather than change: the speed takes effect under a running
  // playback, so dragging the thumb should show you what you are choosing.
  speed.addEventListener('input', () => setSpeed(true));

  const scrub = control('play-scrub');
  // The drag redraws as it goes. A seek replays the runs from zero, and mouse
  // moves arrive faster than frames do, so the rebuild is coalesced to one a
  // frame -- the drag then costs a frame's work per frame instead of one per
  // pixel moved, and the path follows the thumb.
  // One rebuild in flight at a time: further moves of the thumb while one is
  // pending are dropped, and the one that runs reads the thumb where it has
  // got to. A timer rather than requestAnimationFrame, which only runs while
  // the browser is actually painting -- a rebuild measures 42 ms, so there is
  // nothing to gain by lining it up with a frame, and plenty to lose by not
  // redrawing at all when the window happens not to be compositing.
  let dragging = null;
  scrub.addEventListener('input', () => {
    play.scrubbing = true;
    const want = (+scrub.value / 1000) * play.to;
    playClockText(want, playClock(want));
    if (dragging !== null) return;
    dragging = setTimeout(() => {
      dragging = null;
      playSeek((+scrub.value / 1000) * play.to);
    }, 0);
  });
  scrub.addEventListener('change', () => {
    play.scrubbing = false;
    playSeek((+scrub.value / 1000) * play.to);
  });

  document.addEventListener('keydown', (ev) => {
    if (!play.on) return;
    if (ev.target && /^(INPUT|SELECT|TEXTAREA)$/.test(ev.target.tagName)) return;
    if (ev.key === 'Escape') { exitPlayback(); ev.preventDefault(); }
    if (ev.key === 'r' || ev.key === 'R') {
      control('play-repeat').click();
      ev.preventDefault();
    }
    if (ev.key === 'b' || ev.key === 'B') {
      control('play-rev').click();
      ev.preventDefault();
    }
    if (ev.key === 't' || ev.key === 'T') {
      control('play-ticks-on').click();
      ev.preventDefault();
    }
    if (ev.key === 'd' || ev.key === 'D') {
      playDiedHere();
      ev.preventDefault();
    }
    if (ev.key === ',') { playStep(-1); ev.preventDefault(); }
    if (ev.key === '.') { playStep(1); ev.preventDefault(); }
    if (ev.key === ' ') {
      if (play.timer) playPause(); else playResume();
      ev.preventDefault();
    }
  });
}

/* --- where you are -------------------------------------------------------

   One mark that follows the live feed, and an option to keep it on screen.
   Panning only when it would otherwise leave the view: a map that recentres
   on every step is unusable for looking at anything else.
   ------------------------------------------------------------------------ */

function setYouMark(latlng, s) {
  if (!youMark) {
    youMark = L.marker(latlng, {
      pane: 'you',
      icon: L.divIcon({
        className: 'you-mark', html: '<span>\u25C9</span>',
        iconSize: [20, 20], iconAnchor: [10, 10],
      }),
      keyboard: false,
      interactive: false,
    }).addTo(map);
  } else {
    youMark.setLatLng(latlng);
  }
  if (s && s.h !== undefined) youMark.getElement().title = `${Math.round(s.h)} m`;
  if (state.follow) {
    map.panInside(latlng, { padding: effectivePad(), animate: true });
  }
}

async function placeYouFromLast() {
  // On load there is no live feed yet, so ask the recorder where it last saw
  // you. Anything older than a few minutes is a previous session, not a
  // position, and showing it as "you are here" would be a lie.
  try {
    const r = await (await fetch('/api/last')).json();
    if (!r.ok || r.age_s > 300) return;
    const pr = state.meta.projection;
    setYouMark(toLatLng([r.wx * pr.scale_x + pr.offset_x,
                         r.wz * pr.scale_y + pr.offset_y]), null);
  } catch (e) { /* no recorder: no mark */ }
}

/* --- the dungeon you are standing in ------------------------------------- */

// The tail, in the frame the dungeon is being drawn in. Rebuilt whole rather
// than appended to: it is at most a few seconds of walking, and rebuilding is
// what makes it survive the overlay being redrawn under it with a frame that
// may have changed.
function redrawLiveInside() {
  if (state.liveInsideLine) {
    insideLiveGroup.removeLayer(state.liveInsideLine);
    state.liveInsideLine = null;
  }
  const at = inside.transform && inside.transform.at;
  if (!at || state.liveInside.length < 2) return;
  const runs = [[]];
  for (const p of state.liveInside) {
    if (p === null) { runs.push([]); continue; }
    runs[runs.length - 1].push(toLatLng(at(p[0], p[1])));
  }
  const drawn = runs.filter((r) => r.length > 1);
  if (!drawn.length) return;
  state.liveInsideLine = L.polyline(drawn, {
    renderer: insideRenderer,
    pane: 'inside',
    color: state.tint === 'solid' ? state.colour : '#f7d488',
    weight: liveWeight(),
    opacity: 1,
    lineJoin: 'round',
    lineCap: 'round',
  }).addTo(insideLiveGroup);
}

// Looking at the world from inside a cave.
//
// The overlay is the right thing to be shown while you are in one -- the
// path is in the dungeon's own metres and there is nothing else to draw it
// on -- but it dims the world and covers the ground the cave sits under, and
// sometimes that ground is the thing you want. This takes the overlay down
// without pretending you have left.
//
// It comes back when you move. Every sample the recorder sends is a sample it
// stored, which is to say a sample where you had moved `min_move_m` since the
// last one, so "the next sample" and "you started walking again" are the same
// event -- and being left looking at the world with your own position mark
// nowhere on it is not a state to end up in by accident.
function setPeek(on) {
  state.peek = on;
  peekFace();
  if (on) hideInside(true);
  else refreshLiveInside();
}

function peekFace() {
  const b = control('peek');
  if (!b.classList) return;
  b.textContent = state.peek ? 'Back to the cave' : 'Show outside';
  b.classList.toggle('on', state.peek);
}

// Whether the button is on offer at all: only while a cave is actually being
// drawn over the world. A legacy dungeon is on the map in the open, so there
// is no overlay to take down and nothing to show you that is not already
// there.
function peekOffer(show) {
  const b = control('peek');
  if (!show && state.peek) state.peek = false;
  b.hidden = !show;
  peekFace();
}

async function startLiveInside(mapStr) {
  state.liveInside = [];
  redrawLiveInside();
  clearInterval(state.insideTimer);
  await refreshLiveInside(mapStr);
  // The path grows while you are down there, and the enter event may not have
  // been committed when the first sample inside arrived.
  state.insideTimer = setInterval(() => refreshLiveInside(mapStr), 5000);
}

function stopLiveInside() {
  clearInterval(state.insideTimer);
  state.insideTimer = null;
  peekOffer(false);
  // Anything already in flight is now answering a question about a dungeon
  // you have left. Without this, walking out during a refresh let the
  // continuation come back and put the overlay up again -- pinned, because
  // that is how the live overlay draws -- so the world stayed dimmed and the
  // cave stayed on screen until you happened to click the map.
  refreshes.insideLive++;
  state.liveInside = [];
  redrawLiveInside();
  if (inside.live) { inside.live = false; hideInside(true); }
}

async function refreshLiveInside(mapStr) {
  const which = mapStr || state.insideMap;
  if (!which) return;
  const seq = ++refreshes.insideLive;
  const data = await (await fetch('/api/interiors')).json();
  if (seq !== refreshes.insideLive) return;
  const newest = (list) => list
    .filter((v) => v.map === which && v.duration_ms === null)
    .sort((a, b) => b.entered_ms - a.entered_ms)[0];

  const open = newest(data.visits);
  if (open) {
    inside.cache.delete(insideKey(open));   // it is still being written
    inside.live = true;
    if (open.world_visible) {
      peekOffer(false);
      // Its path is on the map already, along with every other run through
      // the place. Redraw those -- the open visit is one of them and grows
      // as you walk -- rather than covering them with an overlay of one.
      await loadInteriors();
      if (seq !== refreshes.insideLive) return;
      // The position mark still needs somewhere to sit. interiorTransform
      // uses the dungeon's frame when there is one and the visit's own first
      // point when there is not -- a Divine Tower placed from the way in has
      // no walked-in visit to take a frame from.
      const d = await fetchInterior(open);
      if (seq !== refreshes.insideLive) return;
      if (d.ok && d.segments.length) {
        inside.transform = {
          map_id: open.map_id, at: interiorTransform(open, d),
        };
      }
      return;
    }
    peekOffer(true);
    // Looking at the world on purpose. The five-second refresh would put the
    // cave straight back up, which would read as the button not working.
    if (state.peek) return;
    // Every run through this place, the way hovering its marker shows them.
    // Only the open one used to be drawn, so walking back into a cave you
    // knew well showed a single fresh line and nothing you had done before.
    // The open visit goes first: the position mark rides on drawn[0].
    const here = data.visits.filter(
      (v) => v.map === which && insideKey(v) !== insideKey(open));
    await showInside([open, ...here], true);
    // showInside fetches too, so the window is still open one level down:
    // if you walked out while it was drawing, take it straight back off.
    if (seq !== refreshes.insideLive) { inside.live = false; hideInside(true); }
    return;
  }

  // Nowhere on the map to draw it: warped into somewhere you have never
  // walked into, so there is no entrance to hang it on. The inset is the
  // fallback -- without it the map just sits there while you play, which
  // looks like the recorder has stopped.
  peekOffer(false);
  const homeless = newest(data.unplaced || []);
  if (homeless && seq === refreshes.insideLive) openInterior(homeless);
}

/* --- controls ------------------------------------------------------------ */

async function buildStats() {
  // Not 'stats': that id was already taken by the points-drawn line at the
  // bottom of the panel, and appending rows to it replaced the count.
  const box = control('numbers');
  if (!box.appendChild) return;          // an older page without the section
  let d;
  try {
    d = await (await fetch('/api/stats')).json();
  } catch (e) {
    return;                              // no recorder: the map still works
  }
  const km = (m) => (m >= 1000 ? `${(m / 1000).toFixed(1)} km`
                               : `${Math.round(m)} m`);
  const spell = (ms) => {
    const h = Math.floor(ms / 3600000), m = Math.round((ms % 3600000) / 60000);
    return h ? `${h} h ${m} min` : `${m} min`;
  };
  const rows = [
    ['Distance travelled', km(d.travelled_m)],
    ['Overworld', km(d.overworld_m)],
    ['Inside dungeons', km(d.inside_m)],
    // Gaps longer than ten seconds are the game paused or the map open, so
    // this is time played rather than time the recorder was left running.
    ['Time spent tracking', spell(d.active_ms)],
    // How much of that you were actually going somewhere. Nothing is stored
    // until you have moved, so a gap always ends in a step -- what it cannot
    // say by itself is whether you walked through the whole of it. The ground
    // covered says: the time that distance takes at walking pace, never more
    // than the gap it happened in. On routes.db it is 13.1 h of 19.4.
    ['Active playtime', spell(d.moving_ms)],
    ['Average session', spell(d.session_mean_ms)],
    ['Longest session', spell(d.session_max_ms)],
    // With the two session lengths, not three rows below them: how many
    // there were and how long they ran are one question asked three ways.
    ['Sessions', `${d.sessions}`],
    ['Deaths', `${d.deaths}`],
    ['Deaths an hour', d.deaths_per_hour === null ? '-' : `${d.deaths_per_hour}`],
    ['Teleports', `${d.jumps}`],
    // `d.legacy` is still sent and still counted; it is not shown, because
    // four castles beside twenty-five caves was a number nobody was reading.
    ['Caves and dungeons', `${d.dungeons}`],
  ];
  // Only once there is any: with nothing recorded down there a nought reads
  // as a broken reading rather than as somewhere you have not been.
  if (d.underground_m > 0) rows.splice(2, 0, ['Underground', km(d.underground_m)]);
  box.textContent = '';
  for (const [what, value] of rows) {
    const row = document.createElement('div');
    const label = document.createElement('span');
    label.textContent = what;
    const num = document.createElement('b');
    num.textContent = value;
    row.append(label, num);
    box.appendChild(row);
  }
}

function buildSessions() {
  const box = document.getElementById('sessions');
  const all = state.meta.sessions || [];
  box.textContent = '';
  if (!all.length) {
    const p = document.createElement('p');
    p.className = 'hint';
    p.textContent = 'Nothing recorded yet.';
    box.appendChild(p);
    return;
  }
  // Newest first, and only a handful: this list grows by one every time the
  // recorder starts, and a hundred rows push everything else off the panel.
  const newest = [...all].sort((a, b) => b.started_ms - a.started_ms);
  const shown = newest.slice(0, state.shownSessions);
  for (const s of shown) box.appendChild(sessionRow(s));

  // Two buttons rather than one that changes its mind. The single button
  // said "Show fewer" only once every session was on screen, so opening a
  // hundred rows to look at one was a thing you could not undo until you had
  // opened all of them.
  const more = control('more-sessions');
  const fewer = control('fewer-sessions');
  const rest = newest.length - shown.length;
  more.hidden = rest <= 0;
  if (rest > 0) more.textContent = `Show ${Math.min(rest, 20)} more (${rest} hidden)`;
  fewer.hidden = state.shownSessions <= SESSIONS_SHOWN;
  fewer.textContent = 'Show fewer';
  if (!more.dataset.wired) {
    more.dataset.wired = '1';
    more.addEventListener('click', () => {
      state.shownSessions = Math.min(
        (state.meta.sessions || []).length, state.shownSessions + 20);
      buildSessions();
    });
    fewer.addEventListener('click', () => {
      // Down the same step it went up, and never past the default: the same
      // number of presses back as it took to get here.
      state.shownSessions = Math.max(
        SESSIONS_SHOWN,
        Math.min(state.shownSessions, newest.length) - 20);
      buildSessions();
    });
  }

  // Wired once: buildSessions runs again after every delete.
  if (!box.dataset.wired) {
    box.dataset.wired = '1';
    box.addEventListener('change', () => {
      const boxes = [...box.querySelectorAll('input')];
      const off = boxes.filter((b) => !b.checked);
      state.sessions = off.length
        ? new Set(boxes.filter((b) => b.checked).map((b) => +b.dataset.session))
        : new Set();
      scheduleReload();
    });
  }
}

// The same capped clock the Statistics panel totals, written short enough
// to sit at the end of a row: "1 h 12" rather than "1 h 12 min".
function playedFor(ms) {
  const mins = Math.round(ms / 60000);
  if (mins < 60) return `${mins} min`;
  return `${Math.floor(mins / 60)} h ${String(mins % 60).padStart(2, '0')}`;
}

function sessionRow(s) {
  const when = new Date(s.started_ms).toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
  const row = document.createElement('div');
  row.className = 'session-row';

  const label = document.createElement('label');
  label.className = 'toggle';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.dataset.session = s.id;
  // Keep whatever was ticked across a rebuild; an empty set means all of them.
  cb.checked = !state.sessions.size || state.sessions.has(s.id);
  const text = document.createElement('span');
  text.className = 'session-when';
  // A simulated run and a real one draw identically, so say which is which
  // rather than leaving the combined path ambiguous.
  text.textContent = /sim/i.test(s.note || '') ? `${when} (sim)` : when;
  if (s.note) label.title = s.note;
  const count = document.createElement('span');
  // How long you played, not how many points it stored. The point count is a
  // fact about the sampling rate as much as about the session -- an hour at
  // five seconds a sample and three minutes at four a second are the same
  // number -- and it is still what the delete confirmation asks about,
  // because that is what deleting removes.
  count.textContent = s.active_ms ? playedFor(s.active_ms) : '\u2013';
  count.title = `${s.samples.toLocaleString()} points`;
  label.append(cb, text, count);

  // Play this one back. The playback's axis is the whole route, so a
  // session is a window on it and this is the two grips plus repeat.
  const one = document.createElement('button');
  one.className = 'ghost play-one';
  one.textContent = '\u25B6\uFE0E';
  one.title = 'Play this session back';
  one.setAttribute('aria-label', `Play back the session from ${when}`);
  one.addEventListener('click', () => playSession(s));

  const del = document.createElement('button');
  del.className = 'ghost del';
  del.textContent = '\u00d7';
  del.title = 'Delete this session';
  del.setAttribute('aria-label', `Delete the session from ${when}`);
  del.addEventListener('click', () => confirmDelete(row, s));

  row.append(label, one, del);
  return row;
}

// One session, played back on its own.
//
// Nothing new drives this: the playback already runs between two grips,
// and a session is a window on the same axis. So the button enters the
// playback if it is not already in it, puts the grips on that session's
// own span, and turns repeat on -- one session is a thing you watch round
// again rather than a route that ends.
//
// The two filters it has to reckon with are the ones playWorld() asks the
// server for. A session that is not ticked is not in the playback either,
// so it is ticked first: pressing play on it says plainly enough that you
// want to see it. The time window is left alone -- widening it silently
// would change what the whole map means -- so a session outside it is
// reported rather than played, which is also what happens to a session
// with nothing drawn from it at all.
async function playSession(s) {
  if (state.sessions.size && !state.sessions.has(s.id)) {
    state.sessions.add(s.id);
    const box = document.querySelector(
      `#sessions input[data-session="${s.id}"]`);
    if (box) box.checked = true;
    await reload();
  }
  if (!play.on) {
    await enterPlayback();
    if (!play.on) return;          // nothing drawn, or a later press won
  }
  const from = playElapsedFor(s.started_ms);
  const until = s.ended_ms ? playElapsedFor(s.ended_ms) : play.to;
  // Both ends land on the same point when the session is not on the axis:
  // outside the time window, or a session that stored nothing.
  if (!(until > from)) {
    setStats(state.range
      ? 'That session is outside the time window.'
      : 'Nothing of that session is drawn.');
    return;
  }
  play.from = from;
  play.until = until;
  playTrimUI();
  if (!play.loop) {
    play.loop = true;
    savePref('loop', true);
    playRepeatUI();
  }
  playSeek(play.from);
  playResume();
}

function confirmDelete(row, s) {
  // Asked in the row rather than through confirm(): it can name the session
  // and its size, which is the whole question. There is no undo.
  row.textContent = '';
  row.classList.add('confirming');
  const msg = document.createElement('span');
  msg.className = 'hint';
  msg.textContent = `Delete ${s.samples.toLocaleString()} points? Permanent.`;
  const yes = document.createElement('button');
  yes.className = 'ghost danger';
  yes.textContent = 'Delete';
  yes.addEventListener('click', () => deleteSession(s));
  const no = document.createElement('button');
  no.className = 'ghost';
  no.textContent = 'Keep';
  no.addEventListener('click', () => buildSessions());
  row.append(msg, yes, no);
}

async function deleteSession(s) {
  let d = {};
  try {
    const res = await fetch(`/api/session/${s.id}`, { method: 'DELETE' });
    d = await res.json();
    if (!res.ok || !d.ok) {
      await refreshSessions();
      setStats(d.error || 'The recorder would not delete that session.');
      return;
    }
  } catch (e) {
    await refreshSessions();
    setStats('Could not reach the recorder to delete that session.');
    return;
  }
  state.sessions.delete(s.id);
  await refreshSessions();
  setStats(`Deleted ${d.samples.toLocaleString()} points and ` +
           `${d.events.toLocaleString()} map events.`);
}

async function refreshSessions() {
  state.meta = await (await fetch('/api/meta')).json();
  buildSessions();
  await reload();
  await loadInteriors();
}

// Every section in the panel folds away, and the heading is the control.
// A real tab stop and a real role, because it behaves like a button whatever
// element it is written as -- and remembered per section, since which parts
// of a panel this long you want standing is a preference like the rest.
function wireSections() {
  for (const sec of document.querySelectorAll('#panel > section')) {
    const h = sec.querySelector(':scope > h2');
    if (!h || !sec.id) continue;
    const key = `sec.${sec.id}`;
    if (pref(key) === 'false') sec.classList.add('shut');
    h.tabIndex = 0;
    h.setAttribute('role', 'button');
    const say = () => h.setAttribute(
      'aria-expanded', String(!sec.classList.contains('shut')));
    say();
    const flip = () => {
      sec.classList.toggle('shut');
      savePref(key, !sec.classList.contains('shut'));
      say();
    };
    h.addEventListener('click', flip);
    h.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); }
    });
  }
}

// The filled part of a slider. A range styled with `appearance: none` is
// drawn by nobody, so Chrome paints no progress: the value reaches CSS as
// a custom property instead. Firefox has ::-moz-range-progress and ignores
// this. Only the bars in the panel and the speed on the timeline are
// styled that way -- the brightness, the time window and the scrub each
// draw their own track.
const RANGE_BARS = '.slider input[type=range], #play-speed';

function paintRange(el) {
  const lo = +el.min || 0;
  const hi = el.max === '' ? 100 : +el.max;
  const at = hi === lo ? 0 : ((+el.value - lo) / (hi - lo)) * 100;
  el.style.setProperty('--fill', `${at}%`);
}

// Every one of them, for the moments a value is set rather than dragged:
// a preference restored at boot, or the align sliders going back to zero.
function paintRanges() {
  for (const el of document.querySelectorAll(RANGE_BARS)) paintRange(el);
}

// The colour-by control. A <select> is drawn by the browser in the
// browser's own colours, and leaves a focus ring standing on the closed
// control until you click somewhere else -- so this is a button and a
// panel. It carries a `value` and fires `input` and `change` like the
// select it replaces, which is why nothing that reads it had to change:
// the look map in wireControls() sets and reads `.value`, and wireInset()
// listens for `change`.
function wirePicker() {
  const el = control('tint-mode');
  if (!el.querySelector) return;                 // an older page
  const list = el.querySelector('.picker-list');
  const btn = el.querySelector('.picker-btn');
  const now = el.querySelector('.now');
  const opts = [...el.querySelectorAll('.picker-list button')];
  const shut = () => {
    el.classList.remove('open');
    list.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
  };
  Object.defineProperty(el, 'value', {
    get: () => el.dataset.value,
    set: (v) => {
      const opt = opts.find((o) => o.dataset.value === v) || opts[0];
      el.dataset.value = opt.dataset.value;
      now.textContent = opt.dataset.label;
      for (const o of opts) o.classList.toggle('on', o === opt);
    },
  });
  el.value = el.dataset.value;                   // paint the initial choice
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = !el.classList.contains('open');
    el.classList.toggle('open', open);
    list.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
  });
  for (const o of opts) {
    o.addEventListener('click', () => {
      el.value = o.dataset.value;
      shut();
      // What everything downstream is listening for. `input` is what the
      // look map uses and `change` is what the inset uses, so both.
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }
  document.addEventListener('click', shut);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') shut();
  });
}

function wireControls() {
  wireSections();
  wirePicker();
  control('peek').addEventListener('click', () => setPeek(!state.peek));
  // The marks live in two places -- their own layer group, and drawn onto
  // the legacy dungeons' permanent paths -- so refetching the list is only
  // half of a toggle. Without the redraw, unticking Deaths cleared the 61 on
  // the world and left 20 sitting on the castles, and unticking Teleports
  // left 8. Measured on routes.db, both ways.
  const redrawMarks = (fetchList) => fetchList().then(() => loadInteriors());

  rememberToggle('l-deaths', 'deaths', (on, first) => {
    state.deaths = on;
    if (!first) redrawMarks(loadDeaths);
  });

  const followCheck = control('l-follow');
  const savedFollow = pref('follow');
  if (savedFollow !== null) followCheck.checked = savedFollow === 'true';
  state.follow = followCheck.checked;
  followCheck.addEventListener('change', () => {
    savePref('follow', followCheck.checked);
    state.follow = followCheck.checked;
    if (state.follow && youMark) {
      map.panInside(youMark.getLatLng(), { padding: effectivePad() });
    }
    showFollowBox();
  });

  // Only ever consulted while following, so it lives with the follow.
  rememberToggle('l-hold-spot', 'holdSpot', (on) => { state.holdSpot = on; });

  rememberToggle('l-respawns', 'respawns', (on, first) => {
    state.respawns = on;
    if (!first) redrawMarks(loadDeaths);
  });

  rememberToggle('l-warps', 'warps', (on, first) => {
    state.warps = on;
    if (!first) redrawMarks(loadWarps);
  });

  // A pinned dungeon stays until you dismiss it -- unless a click is being
  // used to say where something goes, which is the one case where clicking
  // the map means something else.
  // Clicking the map puts away a pinned dungeon overlay. During playback the
  // overlay is not pinned, it is *what the playback is drawing* -- so a click
  // took the dimming off and left the cave lit like the world, and nothing
  // put it back until the playback walked out and in again.
  map.on('click', (e) => {
    if (placing.map_id !== null) { placeAt(e.latlng); return; }
    // During playback a click on the path goes to the moment it was walked.
    if (play.on) { playGoToHover(); return; }
    hideInside(true);
  });
  map.on('mousemove', (e) => {
    if (play.on) { playHover(e.latlng); return; }
    insideHover(e.latlng);
  });
  map.on('mouseout', () => playHover(null));
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (placing.map_id !== null) { stopPlacing(); return; }
    if (play.on) return;             // Escape leaves the playback instead
    hideInside(true);
  });

  // Whether the map follows the route between the surface and the
  // underground -- live, and during playback. On by default, because being
  // shown the map you are not on is the same as not being tracked; off is for
  // when you want to stay looking at one of them.
  rememberToggle('auto-plane', 'autoPlane', (on) => {
    state.autoPlane = on;
    // A playback already on screen is drawn under the old rule, so it is
    // replayed to where it stands under the new one.
    if (play.on) playSeek(play.at);
  });

  rememberToggle('l-interior', 'interior', (on, first) => {
    state.layers.interior = on;
    if (!first) scheduleReload(0);
  });

  // Only the pin. loadInteriors() draws the markers and then hands the
  // legacy dungeons to drawWorldVisible() either way, so the castles keep
  // their paths -- which is the whole point of the switch being separate
  // from the one above it.
  rememberToggle('l-legacy', 'legacy', (on, first) => {
    state.layers.legacy = on;
    if (!first) loadInteriors();
  });

  // Surface or underground: one map or the other, not two layers stacked.
  // Siofra and Ainsel are a different plane of the world with their own
  // terrain, and drawing both at once was always a compromise.
  for (const which of ['surface', 'underground']) {
    control('plane-' + which)
      .addEventListener('click', () => selectPlane(which));
  }
  // Which map you had up last. Restored only if there is one to restore to:
  // remembering "underground" and then showing an empty black screen because
  // the tiles were never built would be worse than forgetting.
  if (pref('plane') === 'underground' && state.meta.underground_tiles) {
    selectPlane('underground');
  }

  // How the path looks. Everything here is a redraw, not a refetch, and is
  // remembered: it is a matter of taste and of what your map image looks
  // like, so it should not have to be set again every session.
  const look = {
    'tint-mode': ['tint', (el) => el.value],
    'line-weight': ['weight', (el) => +el.value],
    'line-casing': ['casing', (el) => +el.value],
    'line-colour': ['colour', (el) => el.value],
    // Backwards against the ladder, on purpose. AGE_DEPTHS runs widest first,
    // so the index and the slider disagree about which way is "more" -- and
    // the slider is the one you look at: dragging right should reach further
    // back, not less far. The index keeps its meaning, so `route.ageDepth`
    // holds what it always held and a setting saved before this still means
    // the same window; it is the control that is reversed, not the value.
    'age-depth': ['ageDepth', (el) => AGE_DEPTHS.length - 1 - +el.value,
                  (v) => String(AGE_DEPTHS.length - 1 - +v)],
  };
  for (const [id, [key, read, write]] of Object.entries(look)) {
    const el = control(id);
    const saved = localStorage.getItem(`route.${key}`);
    if (saved !== null) el.value = write ? write(saved) : saved;
    state[key] = read(el);
    el.addEventListener('input', () => {
      state[key] = read(el);
      // The state and not the control: for a reversed slider those are two
      // different numbers, and the one worth keeping is the one that means
      // something on its own.
      try { localStorage.setItem(`route.${key}`, state[key]); } catch (e) { /* private mode */ }
      syncPathRows();
      redrawEverything();
    });
  }
  syncPathRows();

  wirePlayback();

  const fold = document.querySelector('details.fold');
  if (fold) {
    if (pref('fold') === 'true') fold.open = true;
    fold.addEventListener('toggle', () => savePref('fold', fold.open));
  }

  // How close to the edge the live mark may get before the map moves. The
  // boundary itself is shown while the slider is in use, because a number of
  // pixels means nothing until you see where it lands.
  const pad = control('follow-pad');
  const savedPad = pref('followPct');
  if (savedPad !== null) pad.value = savedPad;
  state.followPad = +pad.value;
  if (followBox) {
    const [px, py] = effectivePad();
    followBox.style.inset = `${py}px ${px}px`;
  }
  pad.addEventListener('input', () => {
    state.followPad = +pad.value;
    savePref('followPct', pad.value);
    showFollowBox();
    if (state.follow && youMark) {
      map.panInside(youMark.getLatLng(), { padding: effectivePad() });
    }
  });
  // While the cursor is on the slider, and not a moment longer. A timer meant
  // a flick past the control left the box standing for a second and a half,
  // and holding the cursor still on the control made it go away -- both of
  // them saying something about a timer rather than about where you are
  // pointing. The drag is the one thing that outlives the pointer leaving:
  // dragging a slider takes the cursor off it constantly.
  pad.addEventListener('pointerenter', showFollowBox);
  pad.addEventListener('pointerleave', () => { if (!padHeld) hideFollowBox(); });
  pad.addEventListener('pointerdown', () => { padHeld = true; showFollowBox(); });
  window.addEventListener('pointerup', () => {
    if (!padHeld) return;
    padHeld = false;
    if (!pad.matches(':hover')) hideFollowBox();
  });
  // Keyboard: the arrows move it with no pointer anywhere near.
  pad.addEventListener('focus', showFollowBox);
  pad.addEventListener('blur', () => { if (!pad.matches(':hover')) hideFollowBox(); });

  // Darkening the terrain: the map is a painting, and a route drawn over the
  // busiest parts of it needs the painting turned down rather than the line
  // turned up.
  // The slider reads as brightness -- sun at the top, moon at the bottom --
  // so the amber part of the track is the light you have left. Stored under
  // its own key: the old one held the opposite number and reading it as this
  // one would turn the map off on first load.
  const dim = control('dim');
  const savedBright = localStorage.getItem('route.bright');
  if (savedBright !== null) dim.value = savedBright;
  const readBright = () => { state.mapDim = (100 - +dim.value) / 100; };
  readBright();
  applyDim();
  dim.addEventListener('input', () => {
    readBright();
    try { localStorage.setItem('route.bright', dim.value); } catch (e) { /* ignore */ }
    applyDim();
  });

  // A compass.png in viewer/ wins; the drawn one is the fallback. Checked as
  // well as listened for, because a missing file has usually failed before
  // this code runs -- and the SVG is unhidden by attribute, since `hidden` as
  // a property is an HTML element's, not an SVG one's.
  const png = control('compass-png');
  const useDrawn = () => {
    png.hidden = true;
    control('compass-svg').removeAttribute('hidden');
  };
  if (png.complete && png.naturalWidth === 0) useDrawn();
  png.addEventListener('error', useDrawn);

  const from = control('t-from');
  const to = control('t-to');
  const label = control('range-label');
  const fill = control('t-fill');

  // The handles move through samples, not through hours. By the clock, a
  // month of not playing is most of the track and the slider does nothing
  // until its last few pixels; by sample count every step moves the same
  // amount of route.
  function atStep(step) {
    const q = state.quantiles;
    const b = state.meta.bounds;
    if (!q.length) return b ? b.t0 + ((b.t1 - b.t0) * step) / 100 : 0;
    return q[Math.max(0, Math.min(q.length - 1,
      Math.round((step / 100) * (q.length - 1))))];
  }

  // `first` is the call that only sets the label up at boot. Without it
  // wiring the panel queued a full reload 250 ms in -- the route and all
  // three mark lists, fetched again a moment after boot had just fetched
  // them. At a tenth of a second a call that is invisible; measured on a
  // database ten times the size of routes.db it was 4.6 s of the 8 before
  // the map settled.
  function applyRange(first) {
    let a = +from.value, c = +to.value;
    if (a > c) { [a, c] = [c, a]; }
    fill.style.left = `${a}%`;
    fill.style.right = `${100 - c}%`;
    const t0 = atStep(a), t1 = atStep(c);
    state.range = (a === 0 && c === 100)
      ? null : [Math.round(t0), Math.round(t1)];
    const when = (t) => new Date(t).toLocaleDateString(
      undefined, { month: 'short', day: 'numeric', hour: '2-digit',
                   minute: '2-digit' });
    label.textContent = state.range
      ? `${when(t0)} to ${when(t1)}`
      : 'Everything recorded';
    if (!first) scheduleReload();
  }

  // Bound rather than passed straight to addEventListener, which would hand
  // the event object in as `first` and make every drag of the slider the
  // silent one.
  from.addEventListener('input', () => applyRange());
  to.addEventListener('input', () => applyRange());
  control('reset-time').addEventListener('click', () => {
    from.value = 0; to.value = 100; applyRange();
  });
  applyRange(true);

  // Last, so every preference restored above is on its slider by now.
  paintRanges();
}

async function redrawEverything() {
  // The runs it is walking were built from the last draw; changing what is
  // drawn out from under it would leave the mark following a path that is no
  // longer there. Playback rebuilds from scratch, so it is ended rather than
  // patched up -- and exitPlayback() is what calls this, so only a redraw
  // that arrives from somewhere else has anything to stop.
  if (play.on) exitPlayback();
  await reload();          // the world route, with the new settings
  await loadInteriors();   // and the dungeons drawn on the map
  if (state.inset) drawInterior(state.inset);
}

function syncPathRows() {
  // Each colouring mode has its own one control: a colour picker for the solid
  // line, a horizon for the age gradient, and nothing for elevation.
  document.getElementById('colour-row').hidden = state.tint !== 'solid';
  document.getElementById('age-depth-row').hidden = state.tint !== 'age';
  const label = document.getElementById('age-depth-label');
  label.hidden = state.tint !== 'age';
  ageCache.key = null;
  const w = AGE_DEPTHS[state.ageDepth] || AGE_DEPTHS[0];
  label.textContent = w[0] === null
    ? 'The gradient covers everything recorded.'
    : `The gradient covers ${w[1]}; anything older is drawn in the oldest colour.`;
}

// How far in from the edge, as a fraction of the way to the centre rather
// than a number of pixels.
//
// Pixels do not survive the shape of the window. Clamping a pixel margin per
// axis means the top of the slider centres the mark on the shorter axis
// first: on a 1600x900 map, 600 px collapses the height to nothing while
// leaving 400 px of slack across the width, so "the middle" was a wide flat
// slot rather than a point. A fraction reaches the centre on both axes at
// once, whatever the window is shaped like, and the box you see while
// dragging keeps the screen's own proportions on the way there.
const FOLLOW_MIN_PX = 20;   // never right up against the edge

function effectivePad() {
  const size = map ? map.getSize() : { x: 800, y: 600 };
  const f = Math.max(0, Math.min(100, state.followPad)) / 100;
  const reach = (side) => {
    const half = Math.max(0, Math.floor(side / 2) - 2);
    const from = Math.min(FOLLOW_MIN_PX, half);
    return Math.round(from + (half - from) * f);
  };
  return [reach(size.x), reach(size.y)];
}

// Whether the edge-margin slider is being dragged. A drag takes the cursor
// off the control almost immediately, so the pointer leaving is not the same
// question as the drag being over.
let padHeld = false;

function showFollowBox() {
  if (!followBox) return;
  const [px, py] = effectivePad();
  followBox.style.inset = `${py}px ${px}px`;
  followBox.classList.add('show');
}

function hideFollowBox() {
  if (followBox) followBox.classList.remove('show');
}

function applyDim() {
  // Two things dim the terrain and they multiply: the slider, which is a
  // standing preference, and standing inside a cave, which is temporary.
  const tile = map.getPane('tilePane');
  if (tile) {
    tile.style.opacity = String((1 - state.mapDim) * (state.dimmed ? 0.45 : 1));
  }
}

function setStats(text) {
  document.getElementById('stats').textContent = text;
}

boot().catch((e) => {
  document.getElementById('status').textContent = 'Could not load route data';
  console.error(e);
});

/* --- click-to-calibrate --------------------------------------------------
 *
 * Removes the image-editor step. Stand somewhere recognisable in game, click
 * that spot on the map, repeat somewhere far away, and the affine transform
 * from world metres to map pixels falls out of the two pairs.
 */

const cal = { active: false, points: [] };

function fitAxis(src, dst) {
  const n = src.length;
  const ms = src.reduce((a, b) => a + b, 0) / n;
  const md = dst.reduce((a, b) => a + b, 0) / n;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (src[i] - ms) * (dst[i] - md);
    den += (src[i] - ms) ** 2;
  }
  if (den === 0) return null;
  const scale = num / den;
  return [scale, md - scale * ms];
}

function calRender() {
  const box = document.getElementById('cal-points');
  box.innerHTML = cal.points
    .map((p, i) =>
      `<div class="cal-pt"><span>${i + 1}.</span>` +
      `<span>world ${p.wx.toFixed(0)}, ${p.wz.toFixed(0)}</span>` +
      `<span>&rarr; px ${p.px.toFixed(0)}, ${p.py.toFixed(0)}</span></div>`)
    .join('');
  document.getElementById('cal-finish').hidden = cal.points.length < 2;
  document.getElementById('cal-step').textContent = cal.points.length === 0
    ? 'Stand somewhere recognisable, then click that spot on the map.'
    : cal.points.length === 1
      ? 'Now travel somewhere far away and click that spot. Further is better.'
      : `${cal.points.length} points. Add more, or compute the projection.`;
}

function calStop() {
  cal.active = false;
  document.getElementById('map').classList.remove('map-picking');
  document.getElementById('cal-live').hidden = true;
  document.getElementById('cal-start').hidden = false;
}

async function calClick(e) {
  if (!cal.active) return;
  const step = document.getElementById('cal-step');
  let last;
  try {
    const res = await fetch('/api/last');
    if (res.status === 404) {
      // Python loads server.py once at startup, but serves app.js fresh from
      // disk -- so a recorder started before an update serves the new viewer
      // against the old server, and this endpoint is missing.
      step.textContent =
        'The running recorder is an older version. Stop it (Ctrl-C), start it ' +
        'again, then reload this page.';
      return;
    }
    if (!res.ok) {
      step.textContent = `Recorder returned ${res.status}. Check its console.`;
      return;
    }
    last = await res.json();
  } catch (err) {
    step.textContent =
      'Lost contact with the recorder — has it stopped? Restart it and reload.';
    console.error(err);
    return;
  }
  if (!last.ok) {
    step.textContent =
      'Nothing recorded yet. Is `record --source game` running, with a save loaded?';
    return;
  }
  // Standing still produces no new samples (min_move_m), and standing still
  // is exactly what you do while lining up a click -- so age alone is not a
  // problem. Only block when it is old enough to likely be a stale session.
  if (last.age_s > 600) {
    step.textContent =
      `Last position is ${Math.round(last.age_s / 60)} min old — probably from an ` +
      `earlier session. Move in game so a fresh sample is recorded.`;
    return;
  }
  // Pixels in the source map image, which is what the projection maps onto.
  const p = map.project(e.latlng, state.nativeZoom);
  const dup = cal.points.some(
    (q) => Math.abs(q.wx - last.wx) < 1 && Math.abs(q.wz - last.wz) < 1);
  if (dup) {
    step.textContent = 'Same in-game spot as an earlier point. Move further away first.';
    return;
  }
  cal.points.push({ wx: last.wx, wz: last.wz, px: p.x, py: p.y, age: last.age_s });
  calRender();
  if (last.age_s > 90) {
    document.getElementById('cal-step').textContent +=
      ` (that position was ${Math.round(last.age_s)}s old — fine if you have been standing still)`;
  }
}

function calCompute() {
  const fx = fitAxis(cal.points.map((p) => p.wx), cal.points.map((p) => p.px));
  const fy = fitAxis(cal.points.map((p) => p.wz), cal.points.map((p) => p.py));
  const out = document.getElementById('cal-out');
  if (!fx || !fy) {
    out.hidden = false;
    out.textContent = 'Points are not separated on one axis.\nMove further apart.';
    return;
  }
  let worst = 0;
  for (const p of cal.points) {
    const ex = p.wx * fx[0] + fx[1];
    const ey = p.wz * fy[0] + fy[1];
    worst = Math.max(worst, Math.hypot(ex - p.px, ey - p.py));
  }

  // The map is not stretched on one axis, so |scale_x| and |scale_y| should
  // agree. A gap means a click landed in the wrong place -- and with only two
  // points this is the ONLY available check, because a two-point fit is always
  // exact and its residual is therefore meaningless.
  const sx = Math.abs(fx[0]), sy = Math.abs(fy[0]);
  const skew = Math.abs(sx - sy) / Math.max(sx, sy);
  const notes = [];
  if (cal.points.length < 3) {
    notes.push('# 2 points: the fit is exact by construction, so the');
    notes.push('# residual above proves nothing. Add a third to check it.');
  }
  if (skew > 0.05) {
    notes.push(`# WARNING: scales differ by ${(skew * 100).toFixed(0)}%.`);
    notes.push('# They should match — the map is not stretched. One of your');
    notes.push('# clicks was probably off. Recalibrate with points further apart.');
  }
  if (fx[0] < 0) {
    notes.push('# WARNING: scale_x is negative, which is unexpected.');
    notes.push('# Your two points may be swapped on the X axis.');
  }

  out.hidden = false;
  out.textContent =
    `[projection]\n` +
    `scale_x  = ${fx[0].toFixed(6)}\n` +
    `offset_x = ${fx[1].toFixed(3)}\n` +
    `scale_y  = ${fy[0].toFixed(6)}\n` +
    `offset_y = ${fy[1].toFixed(3)}\n\n` +
    (cal.points.length >= 3 ? `# worst point ${worst.toFixed(1)} px off\n` : '') +
    notes.join('\n') +
    (notes.length ? '\n' : '') +
    `# paste into config.toml, replacing the old block, then restart the tracker`;
  calStop();
}

function wireCalibration() {
  document.getElementById('cal-start').addEventListener('click', () => {
    cal.active = true;
    cal.points = [];
    document.getElementById('cal-start').hidden = true;
    document.getElementById('cal-live').hidden = false;
    document.getElementById('cal-out').hidden = true;
    document.getElementById('map').classList.add('map-picking');
    calRender();
  });
  document.getElementById('cal-cancel').addEventListener('click', calStop);
  document.getElementById('cal-finish').addEventListener('click', calCompute);
  map.on('click', calClick);
}

/* --- align by eye --------------------------------------------------------
 *
 * Clicking landmarks needs four accurate clicks and assumes the map image is a
 * faithful, unstretched copy of the world. Map packs are often cropped and
 * resized, which breaks that assumption. Dragging sliders while watching the
 * route sit on the terrain sidesteps both problems: you are matching what you
 * can see, not trusting a model.
 *
 * The route is fetched once in raw world metres and re-projected in the
 * browser on every change, so there is no config edit or restart per attempt.
 */

const align = { on: false, raw: null, t: null, saved: null };

function alignTransform() {
  const lock = document.getElementById('align-lock').checked;
  const flip = document.getElementById('align-flip').checked;
  const base = align.base;
  const sx = base.sx * Math.pow(2, +document.getElementById('a-scale').value);
  const syMag = lock
    ? sx
    : base.sx * Math.pow(2, +document.getElementById('a-scaley').value);
  const sy = flip ? -syMag : syMag;
  const W = state.meta.map.image_width, H = state.meta.map.image_height;
  const ox = base.cx - base.wxMid * sx + (+document.getElementById('a-x').value) * W;
  const oy = base.cy - base.wzMid * sy + (+document.getElementById('a-y').value) * H;
  return { sx, sy, ox, oy };
}

function alignDraw() {
  const t = alignTransform();
  align.t = t;
  routeGroup.clearLayers();
  casingGroup.clearLayers();
  const segs = align.raw.segments;
  for (const seg of segs) {
    const pts = seg.xy.map(([wx, wz]) =>
      map.unproject([wx * t.sx + t.ox, wz * t.sy + t.oy], state.nativeZoom));
    L.polyline(pts, {
      renderer,
      color: seg.layer === 'underground' ? '#6f8fa8' : '#e0a33c',
      weight: seg.layer === 'underground' ? 2 : 2.5,
      opacity: 0.95,
    }).addTo(routeGroup);
  }
  setStats(`scale ${t.sx.toFixed(4)} / ${t.sy.toFixed(4)}`);
}

function alignReset() {
  for (const id of ['a-scale', 'a-scaley', 'a-x', 'a-y']) {
    document.getElementById(id).value = 0;
  }
  paintRanges();
  alignDraw();
  map.fitBounds(state.imageBounds, { padding: [10, 10] });
}

async function alignStart() {
  const p = new URLSearchParams({ layers: 'surface,underground', epsilon: '6', raw: '1' });
  const res = await fetch('/api/route?' + p);
  align.raw = await res.json();
  if (!align.raw.segments.length) {
    document.getElementById('cal-step').textContent = 'No route recorded yet.';
    return;
  }

  // Start from a guess that puts the route across the middle of the map, so
  // the sliders have somewhere sensible to move from.
  let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
  for (const s of align.raw.segments) {
    for (const [wx, wz] of s.xy) {
      if (wx < x0) x0 = wx; if (wx > x1) x1 = wx;
      if (wz < z0) z0 = wz; if (wz > z1) z1 = wz;
    }
  }
  const W = state.meta.map.image_width, H = state.meta.map.image_height;
  const guess = Math.min(W / Math.max(x1 - x0, 1), H / Math.max(z1 - z0, 1)) * 0.6;
  align.base = { sx: guess, wxMid: (x0 + x1) / 2, wzMid: (z0 + z1) / 2,
                 cx: W / 2, cy: H / 2 };

  align.on = true;
  document.getElementById('cal-start').hidden = true;
  document.getElementById('align-start').hidden = true;
  document.getElementById('align-live').hidden = false;
  document.getElementById('cal-out').hidden = true;
  document.getElementById('a-scaley-row').hidden =
    document.getElementById('align-lock').checked;
  alignReset();
}

function alignStop() {
  align.on = false;
  document.getElementById('align-live').hidden = true;
  document.getElementById('cal-start').hidden = false;
  document.getElementById('align-start').hidden = false;
  scheduleReload(0);
}

function alignAccept() {
  const t = align.t;
  const out = document.getElementById('cal-out');
  out.hidden = false;
  out.textContent =
    `[projection]\n` +
    `scale_x  = ${t.sx.toFixed(6)}\n` +
    `offset_x = ${t.ox.toFixed(3)}\n` +
    `scale_y  = ${t.sy.toFixed(6)}\n` +
    `offset_y = ${t.oy.toFixed(3)}\n\n` +
    `# paste into config.toml, replacing the old block,\n` +
    `# then restart the tracker`;
  align.on = false;
  document.getElementById('align-live').hidden = true;
  document.getElementById('cal-start').hidden = false;
  document.getElementById('align-start').hidden = false;
}

function wireAlign() {
  document.getElementById('align-start').addEventListener('click', alignStart);
  document.getElementById('align-cancel').addEventListener('click', alignStop);
  document.getElementById('align-done').addEventListener('click', alignAccept);
  document.getElementById('align-fit').addEventListener('click', alignReset);
  document.getElementById('align-lock').addEventListener('change', (e) => {
    document.getElementById('a-scaley-row').hidden = e.target.checked;
    alignDraw();
  });
  for (const id of ['a-scale', 'a-scaley', 'a-x', 'a-y', 'align-flip']) {
    document.getElementById(id).addEventListener('input', alignDraw);
    document.getElementById(id).addEventListener('change', alignDraw);
  }
}

// @ts-check
// The labelling cell: one Sentinel-2 pixel, named the same way in every place
// that has to agree about it.
//
// Split out of app.js because this is the code with a SECOND IMPLEMENTATION --
// `src/label_cell.py` -- and the two drifting apart is a silent scientific
// fault: the interpreter judges one footprint while the builders reduce over
// another. `tests/test_label_cell.py` runs the two against each other.
//
// That test used to reach into label_app.html and cut these functions out with
// regular expressions that stopped at the first `}` in column 0 -- so a
// reindent could hand it the wrong source, or silently a shorter one. It now
// `require()`s this file and calls the same functions the browser calls,
// because of the export at the foot.
//
// A CLASSIC SCRIPT, loaded before app.js, so these names are globals there
// exactly as they were when this was one file.

// The edge of the unit being labelled, in metres. The original sampling called
// each 10 m cell by MAJORITY COVER, and the model this trains is a 10 m model,
// so the app must ask for the same thing the targets were defined as. WHICH
// 10 m square that is, is the next block: it is a Sentinel-2 pixel, not a
// square around the point. It is drawn on the map (the `cell` layer), it is
// painted into every chip, it is the pixel the dense series reads
// (`denseFetchLive`, and `build_batch_dense.py`), and it is what the brief
// tells the interpreter to judge.
const LABEL_CELL_M = 10;

// ── THE LABELLING CELL IS ONE SENTINEL-2 PIXEL ──────────────────────────────
// `src/label_cell.py` is this code in Python and its header is the reasoning;
// `tests/test_label_cell.py` runs the two against each other in node. In one
// line: a square *centred on the point* straddles four Sentinel-2 pixels and
// covers no one of them, so the interpreter judged one footprint, the dense
// series read a second and the model predicts a third. Snapping the point's
// UTM coordinates down to a multiple of 10 m names the same square in the
// imagery, in the evidence and in the deployed raster (which is written on
// exactly this grid: EPSG:32632, 10 m, origin an exact multiple of 10).
//
// A batch from build_label_batches.py carries its cells BAKED, and that is
// what gets drawn. This path is for a .csv/.geojson dropped on the window,
// which has no bake and is still owed the same unit.
const WGS_A = 6378137.0, WGS_F = 1 / 298.257223563;
const WGS_E2 = WGS_F * (2 - WGS_F), WGS_EP2 = WGS_E2 / (1 - WGS_E2);
const UTM_K0 = 0.9996, DEG = Math.PI / 180;

/** The EPSG code of the UTM zone MGRS puts this point in — including the 32V
 *  and Svalbard exceptions, which are not academic here: the study area is
 *  Norway, and getting the zone wrong snaps to a grid Sentinel-2 does not use. */
function utmEpsg(lon, lat) {
  let z = Math.floor((lon + 180) / 6) + 1;
  if (lat >= 56 && lat < 64 && lon >= 3 && lon < 12) z = 32;
  else if (lat >= 72 && lat < 84) {
    if (lon >= 0 && lon < 9) z = 31;
    else if (lon >= 9 && lon < 21) z = 33;
    else if (lon >= 21 && lon < 33) z = 35;
    else if (lon >= 33 && lon < 42) z = 37;
  }
  z = Math.min(Math.max(z, 1), 60);
  return (lat >= 0 ? 32600 : 32700) + z;
}

// The zone's central meridian. On its own lines because the test pulls
// these functions out of this file by brace, and a one-line body has no
// closing brace in column zero to stop at.
function utmLon0(epsg) {
  return ((epsg % 100) - 1) * 6 - 180 + 3;
}

/** WGS84 -> UTM. Snyder's series, which is good to well under a millimetre
 *  inside a zone — three orders of magnitude finer than the 10 m it is about
 *  to be floored to. */
function utmForward(lon, lat, epsg) {
  const ph = lat * DEG, sp = Math.sin(ph), cp = Math.cos(ph), tp = Math.tan(ph);
  let dl = (lon - utmLon0(epsg)) * DEG;
  while (dl > Math.PI) dl -= 2 * Math.PI;
  while (dl < -Math.PI) dl += 2 * Math.PI;
  const N = WGS_A / Math.sqrt(1 - WGS_E2 * sp * sp);
  const T = tp * tp, C = WGS_EP2 * cp * cp, A1 = cp * dl;
  const e2 = WGS_E2, e4 = e2 * e2, e6 = e4 * e2;
  const M = WGS_A * ((1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * ph
    - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * Math.sin(2 * ph)
    + (15 * e4 / 256 + 45 * e6 / 1024) * Math.sin(4 * ph)
    - (35 * e6 / 3072) * Math.sin(6 * ph));
  const x = UTM_K0 * N * (A1 + (1 - T + C) * Math.pow(A1, 3) / 6
      + (5 - 18 * T + T * T + 72 * C - 58 * WGS_EP2) * Math.pow(A1, 5) / 120)
    + 500000;
  let y = UTM_K0 * (M + N * tp * (A1 * A1 / 2
    + (5 - T + 9 * C + 4 * C * C) * Math.pow(A1, 4) / 24
    + (61 - 58 * T + T * T + 600 * C - 330 * WGS_EP2) * Math.pow(A1, 6) / 720));
  if (epsg >= 32700) y += 10000000;
  return [x, y];
}

/** UTM -> WGS84. The corners go back one at a time and not as a lon/lat box:
 *  grid convergence rotates the square by up to ~3° at a zone edge, which is
 *  0.45 m over a 10 m cell — 4.5% of the thing being judged. */
function utmInverse(x, y, epsg) {
  const e2 = WGS_E2, e4 = e2 * e2, e6 = e4 * e2;
  x -= 500000;
  if (epsg >= 32700) y -= 10000000;
  const M = y / UTM_K0;
  const mu = M / (WGS_A * (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256));
  const e1 = (1 - Math.sqrt(1 - e2)) / (1 + Math.sqrt(1 - e2));
  const p1 = mu
    + (3 * e1 / 2 - 27 * Math.pow(e1, 3) / 32) * Math.sin(2 * mu)
    + (21 * e1 * e1 / 16 - 55 * Math.pow(e1, 4) / 32) * Math.sin(4 * mu)
    + (151 * Math.pow(e1, 3) / 96) * Math.sin(6 * mu)
    + (1097 * Math.pow(e1, 4) / 512) * Math.sin(8 * mu);
  const s1 = Math.sin(p1), c1 = Math.cos(p1), t1 = Math.tan(p1);
  const C1 = WGS_EP2 * c1 * c1, T1 = t1 * t1;
  const N1 = WGS_A / Math.sqrt(1 - e2 * s1 * s1);
  const R1 = WGS_A * (1 - e2) / Math.pow(1 - e2 * s1 * s1, 1.5);
  const D = x / (N1 * UTM_K0);
  const lat = p1 - (N1 * t1 / R1) * (D * D / 2
    - (5 + 3 * T1 + 10 * C1 - 4 * C1 * C1 - 9 * WGS_EP2) * Math.pow(D, 4) / 24
    + (61 + 90 * T1 + 298 * C1 + 45 * T1 * T1 - 252 * WGS_EP2 - 3 * C1 * C1)
      * Math.pow(D, 6) / 720);
  const lon = utmLon0(epsg) * DEG + (D - (1 + 2 * T1 + C1) * Math.pow(D, 3) / 6
    + (5 - 2 * C1 + 28 * T1 - 3 * C1 * C1 + 8 * WGS_EP2 + 24 * T1 * T1)
      * Math.pow(D, 5) / 120) / c1;
  return [lon / DEG, lat / DEG];
}

/** The Sentinel-2 pixel containing (lon, lat), as a lon/lat ring. */
function s2Cell(lon, lat, epsg) {
  epsg = Number(epsg) || utmEpsg(lon, lat);
  const [x, y] = utmForward(lon, lat, epsg);
  const x0 = Math.floor(x / LABEL_CELL_M) * LABEL_CELL_M;
  const y0 = Math.floor(y / LABEL_CELL_M) * LABEL_CELL_M;
  const m = LABEL_CELL_M;
  const ring = [[x0, y0], [x0 + m, y0], [x0 + m, y0 + m], [x0, y0 + m]]
    .map(c => utmInverse(c[0], c[1], epsg));
  ring.push(ring[0].slice());
  return { epsg: epsg, x0: x0, y0: y0, ring: ring };
}

// Loadable by node without a bundler and without touching the browser path:
// `module` is undefined in the browser, so this is dead code there. It is what
// lets tests/test_label_cell.py require the real file instead of a regex slice
// of it.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { LABEL_CELL_M, WGS_A, WGS_E2, UTM_K0,
                     utmEpsg, utmLon0, utmForward, utmInverse, s2Cell };
}

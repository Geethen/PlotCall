// @ts-check
// The chip vis scheme and the display ramp.
//
// Split out of app.js for the same reason as js/cell.js: this is code with a
// SECOND IMPLEMENTATION in Python -- `combo_bounds()` and the combo table in
// `src/build_batch_chips.py` -- and the two drifting apart does not look like a
// bug. It looks like the imagery changed. A sprite baked through one ramp and a
// tint painted through another are two exposures of the same pixel, and
// `tests/test_chip_ramp.py` is what stops that, by running this file in node
// against the Python on random stretch tables.
//
// That test used to rebuild these functions by cutting them out of the page
// with regular expressions and pasting them together with stubs. It now loads
// this file whole.
//
// `chipStretch()` is deliberately NOT here: it reads `S.batch` and `chipVis`,
// which is app state, so it stays in app.js and is a free global to the one
// function here that calls it. In the browser that resolves exactly as it did
// when this was one file; in node the test supplies it. That seam is the only
// thing standing between this file and being pure.
//
// A CLASSIC SCRIPT, loaded before app.js.

// ── the vis scheme: three-band mixes and single-index ramps ─────────────────
// The scheme is ONE setting with two kinds of member, and it drives four
// things: the chip pixels, the colour of every dot on the annual chart, the
// colour of a chip cell before its image lands, and — for an index — which
// series the chart plots. Picking a scheme is therefore the interpreter's main
// question ("show me moisture") rather than a rendering preference.
const CHIP_RGB = {
  'SWIR1/NIR/GREEN': { bands: ['B11', 'B8', 'B3'], min: 0, max: 3500,
    tip: 'The default. Bare ground, built surface and vegetation separate here in a way they do not in natural colour.' },
  'NIR/RED/GREEN':   { bands: ['B8', 'B4', 'B3'], min: 0, max: 3000,
    tip: 'Classic false colour: vegetation red, crops bright.' },
  'NIR/SWIR1/RED':   { bands: ['B8', 'B11', 'B4'], min: 0, max: 3500,
    tip: 'Agriculture: bare soil, senesced crop and green crop are three clearly different colours.' },
  'RED/GREEN/BLUE':  { bands: ['B4', 'B3', 'B2'], min: 0, max: 2500,
    tip: 'Natural colour, for showing somebody else what you saw.' }
};

//: A single normalised difference, colour-ramped. The two band keys are the
//: numerator and denominator of `(a - b) / (a + b)`, so the same definition
//: serves the chip, the dot colour and the plotted series with no second
//: recipe to drift.
const CHIP_INDEX = {
  'NDVI': { bands: ['B8', 'B4'],  min: 0.0,  max: 0.9, series: 'ndvi',
    tip: 'Greenness. The dashed line at 0.31 is roughly where bare or built ground ends and green cover begins — fitted on this project’s own plots, and only a rough guide (it separates them about two times in three).' },
  'NDMI': { bands: ['B8', 'B11'], min: -0.2, max: 0.6, series: 'ndmi',
    tip: 'Moisture. Irrigation and wet-season cropping show here first.' },
  'NBR':  { bands: ['B8', 'B12'], min: 0.0,  max: 0.8, series: 'nbr',
    tip: 'Burn / clearance. Right for a felling, wrong as a default on this legend.' }
};
const INDEX_RAMP = ['#7c4a13', '#b8860b', '#e6d200', '#9acd32', '#3f9e3f', '#0b6b2e'];

function chipIsIndex(combo) { return combo in CHIP_INDEX; }

/** The CHIP_INDEX key whose `series` is the one on the chart right now.
 *  This is what paints the chart dots (AL11.8), and the lookup from a series
 *  name back to its scheme generally -- re-deriving it inline is how the two
 *  band lists for one word called NDVI get created. */
function indexComboForSeries(series) {
  return Object.keys(CHIP_INDEX)
    .find(k => CHIP_INDEX[k].series === (series || evSeries)) || null;
}
function chipSpec(combo) {
  return CHIP_INDEX[combo] || CHIP_RGB[combo] || CHIP_RGB['SWIR1/NIR/GREEN'];
}

/** A hex colour sampled from a stop list at 0..1, linearly. */
function rampColor(stops, t) {
  t = Math.max(0, Math.min(1, isFinite(t) ? t : 0));
  const seg = t * (stops.length - 1);
  const k = Math.min(stops.length - 2, Math.floor(seg));
  const f = seg - k;
  const a = stops[k].replace('#', ''), b = stops[k + 1].replace('#', '');
  const hx = (s, o) => parseInt(s.substr(o, 2), 16);
  const mix = o => Math.round(hx(a, o) + (hx(b, o) - hx(a, o)) * f);
  const h2 = x => x.toString(16).padStart(2, '0');
  return '#' + h2(mix(0)) + h2(mix(2)) + h2(mix(4));
}

// ── the display ramp ────────────────────────────────────────────────────────
//: MUST MATCH `STRETCH_MIN_SPAN` in build_batch_chips.py.
const STRETCH_MIN_SPAN = 120;

/** The min/max this point is drawn through, under the current scheme.
 *
 *  MUST MATCH `combo_bounds()` in build_batch_chips.py, and the property that
 *  matters is that the THREE CHANNELS SHARE ONE RAMP. Per-band bounds are the
 *  textbook stretch and they were tried first: they are a decorrelation
 *  stretch, they move hue, and hue here is a convention the tips and the legend
 *  teach ("vegetation is green in SWIR/NIR/GREEN"). They also turn the few
 *  hundred DN of atmospheric drift between two years into a full-scale colour
 *  swing — manufacturing change, in the one instrument that exists to measure
 *  it. One affine transform on all three channels is an exposure adjustment,
 *  not a recolouring. */
function comboBounds(p, combo) {
  const spec = chipSpec(combo);
  if (chipIsIndex(combo)) return spec;
  const st = chipStretch(p);
  if (!st) return spec;
  const pairs = spec.bands.map(b => st[b]);
  if (pairs.some(x => !x)) return spec;
  let lo = Math.min(...pairs.map(x => x[0]));
  let hi = Math.max(...pairs.map(x => x[1]));
  if (hi - lo < STRETCH_MIN_SPAN) {
    const mid = (hi + lo) / 2;
    lo = Math.max(mid - STRETCH_MIN_SPAN / 2, 0);
    hi = mid + STRETCH_MIN_SPAN / 2;
  }
  return { bands: spec.bands, min: lo, max: hi, tip: spec.tip };
}

// See js/cell.js: dead code in the browser, and what lets the test require the
// real file. `chipStretch` is not exported because it is not defined here.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { CHIP_RGB, CHIP_INDEX, INDEX_RAMP, chipIsIndex,
                     indexComboForSeries, chipSpec, rampColor,
                     STRETCH_MIN_SPAN, comboBounds };
}

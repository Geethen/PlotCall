"""Bake the per-point evidence into a labelling batch, at build time.

Why baked, and why this file exists
-----------------------------------
``app/label_app.html`` is hosted statically -- a folder on a bucket, no
always-on service -- and Earth Engine sign-in is on the critical path for
*imagery only*. Everything an interpreter needs that is not a picture is
therefore computed **once, here, at batch build time** and carried inside the
batch JSON: point values, an annual index timeline, an annual land-cover
sequence, terrain and water. The whole labelling loop renders and works with
Earth Engine never signed in. Losing the Sentinel-2 chips is acceptable; losing
the app is not.

What goes in, and why each of them
----------------------------------
Section P of ``https://github.com/Geethen/recoverHabloss/blob/main/docs/research/TWOTOWER_RESEARCH.md`` is the standing verdict
here: a single-date auxiliary path is bought on **error independence**, not on
accuracy. Every dataset below gets its answers wrong in ways that have nothing
to do with how the deployed AlphaEarth two-tower gets them wrong, which is the
only reason any of it is worth an interpreter's attention. The model's own
posterior is not in this file and is not evidence -- it is the thing being
corrected, and on the two visible map errors it is confident and wrong.

1. **Core point values.** Dynamic World 2018/2024 mode plus built/crop/tree
   probability means; ESA WorldCover 2020 (v100) *and* 2021 (v200), because the
   two disagree and the disagreement is itself informative; Hansen ``lossyear``
   from the 2025 v1_13 vintage -- v1_11 stops at 23 and the label window ends in
   2024; GHSL built surface per epoch; per-year clear-scene counts, so a flat
   timeline can be told apart from an empty one.
2. **Annual index timeline 2017-2025.** Growing-season medians of NDVI, NDMI and
   NBR plus the six raw S2 SR band medians (B2, B3, B4, B8, B11, B12), so the
   spectral profile in the app needs no further network call. The cloud recipe
   is the inspector's: ``COPERNICUS/S2_SR_HARMONIZED`` joined to
   ``S2_CLOUD_PROBABILITY``, probability < 40, SCL shadow/cloud/cirrus dropped.
3. **ESRI 10 m Annual Land Cover 2017-2023.** Annual and 10 m with an explicit
   Crops class, so it names a change *year* rather than bracketing one.
4. **Terrain and water.** Copernicus DEM slope, a WorldCover bare-ground flag,
   JRC Global Surface Water occurrence. These are the two §AL-T stable-class map
   errors -- mountains read as built-up, wetlands read as cropland -- made
   visible at the point where they are being labelled.

Two things this gets right that a naive version does not
--------------------------------------------------------
**The growing season is latitude-aware.** ``c2c_ts_server.py``, which the S2
recipe is taken from, hardcodes June-September because it is a Europe-only tool.
These points are drawn globally: a southern-hemisphere point composited over
June-September is its *dry* season, and the whole timeline is then misleading in
a way that looks like data. The window flips by hemisphere, the tropics get the
full year, and which was used is recorded in the payload rather than assumed.

**The extraction is batched -- but on two different axes, and the difference is
the whole lesson here.**

For the *point-value* datasets (Dynamic World, WorldCover, Hansen, GHSL, ESRI,
terrain) the batching axis is **points**: one ``reduceRegions`` over a chunk of
them per dataset. A hundred points times twenty datasets times a round trip each
is an afternoon; chunked it is a minute.

For the *Sentinel-2 timeline* that axis does not work at all, and the reason is
structural rather than a matter of tuning: **Earth Engine evaluates in tiles.**
A request is costed by the tiles it has to touch, so a batch of points that fall
in the same tile is nearly free to add to, and a batch of points that do not is
one request that must materialise every one of their tiles at once. These points
are a **global equal-area draw** -- consecutive points are on different
continents by construction -- so a request covering twenty of them makes the
cloud-probability join materialise scenes across several continents, and Earth
Engine answers **"User memory limit exceeded"**. At 100 points, and still at 20:
lowering the chunk size does not fix a problem whose cost is the spread.

So **widely-spread points are mapped over independently**: the timeline makes
one request per **(point, year)**, with ``filterBounds`` on that single point
keeping each year's collection to the scenes that actually touch it. Measured,
at one point: a single year is **2.4 s**; nine years of one band each is 5.7 s;
nine years of all ten bands **times out**. So the unit is the point-year.

**Budget it as roughly 20 s per point, not 2.4.** The ~900 point-years of a
100-point batch go through a thread pool, but a measured end-to-end run took
**36 minutes**, not the ~2 the per-request timing predicts: Earth Engine
rate-limits interactive `getInfo` calls, so sixteen workers buy far less than
sixteen times the throughput. That is fine -- this is a build step run once per
round, not something an interpreter waits on -- but do not size a labelling
round on the optimistic arithmetic.

The chunked, points-batched path survives only for the point-value datasets,
which are plain per-pixel lookups with no collection to reduce and are therefore
cheap to sample anywhere.

Usage
-----
    G=python   # whichever interpreter has the deps

    # normally: called from build_label_batches.py with --evidence
    $G src/build_label_batches.py --placeholder --evidence

    # or added to batches that already exist
    $G src/build_batch_evidence.py --batch app/batches/b001.json

    # the parts that need no Earth Engine, for a check
    $G src/build_batch_evidence.py --show-seasons
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

#: Stamped into every batch this writes. **Bump it whenever a recipe below
#: changes** -- a silently changed recipe is the one way a point-values table can
#: be wrong and still look right, and the app renders this next to the heading.
EVIDENCE_VERSION = "ev2"

#: The window the timeline covers. 2017 is the first full Sentinel-2 year;
#: the far end runs past 2024 so a change dated to the last year of the label
#: window still has a year of context after it.
YEAR_FIRST, YEAR_LAST = 2017, 2025

#: Sentinel-2 SR bands kept. Selected BEFORE any median()/count(): early scenes
#: (2017 especially) carry a different band set, and reducing the raw collection
#: raises "Expected a homogeneous image collection".
S2_BANDS = ("B2", "B3", "B4", "B8", "B11", "B12")

#: Asset ids, in one place, with the vintage visible. Every one of these has an
#: end year and the app displays it; see EE_PRESETS in label_app.html.
ASSETS = {
    "dw": "GOOGLE/DYNAMICWORLD/V1",
    "wc_2020": "ESA/WorldCover/v100",
    "wc_2021": "ESA/WorldCover/v200",
    # v1_11's `lossyear` stops at 23. The label window ends in 2024.
    "hansen": "UMD/hansen/global_forest_change_2025_v1_13",
    "ghsl": "JRC/GHSL/P2023A/GHS_BUILT_S",
    "esri_lc": "projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m_TS",
    # EC JRC global forest cover 2020, the EU deforestation-regulation
    # baseline. 10 m, and it EXCLUDES agricultural plantations -- the one
    # distinction Hansen's tree-cover threshold cannot make, and the one that
    # lands on this legend's Cropland / Nature line. V1 and V2 are deprecated.
    "gfc2020": "JRC/GFC2020/V3",
    # GLO30 proper is deprecated in favour of this; same "DEM" band.
    "dem": "COPERNICUS/DEM/GLO30_2024_1",
    "gsw": "JRC/GSW1_4/GlobalSurfaceWater",
    "s2": "COPERNICUS/S2_SR_HARMONIZED",
    "s2_clouds": "COPERNICUS/S2_CLOUD_PROBABILITY",
}

#: Dynamic World's nine classes, in band order.
DW_NAMES = ["water", "trees", "grass", "flooded vegetation", "crops",
            "shrub & scrub", "built", "bare", "snow & ice"]

#: ESA WorldCover, both versions (the class table is unchanged between them).
WC_NAMES = {10: "tree cover", 20: "shrubland", 30: "grassland", 40: "cropland",
            50: "built-up", 60: "bare / sparse", 70: "snow & ice", 80: "water",
            90: "herbaceous wetland", 95: "mangroves", 100: "moss & lichen"}

#: ESRI 10 m Annual Land Cover. Annual, 10 m, and with an explicit Crops class,
#: which is what makes it worth carrying alongside Dynamic World.
ESRI_NAMES = {1: "water", 2: "trees", 4: "flooded vegetation", 5: "crops",
              7: "built area", 8: "bare ground", 9: "snow/ice", 10: "clouds",
              11: "rangeland"}
#: 2017-2025. The collection ended at 2023 when this was written and now runs
#: to 2025, which makes it the ONLY annual 10 m product that can answer for
#: both ends of a 2018 -> 2024 question. That is the end-year rule paying off
#: in the other direction: a vintage that moved forward and was not noticed is
#: a dataset answering 2023 to a 2024 question for no reason.
ESRI_YEARS = tuple(range(2017, 2026))

#: The two dates the label is about.
LABEL_YEARS = (2018, 2024)


# ---------------------------------------------------------------------------
# the growing season -- pure, and the part most worth getting right
# ---------------------------------------------------------------------------
def growing_season(lat: float) -> dict:
    """The compositing window for a point at this latitude.

    Three regimes, and the reason for each:

    * **|lat| <= 15** -- the full year. There is no single growing window worth
      picking in the humid tropics, and a four-month one throws away most of the
      few cloud-free scenes there are.
    * **lat > 15** -- June to September, the northern growing season. This is the
      window ``c2c_ts_server.py`` hardcodes, and it is correct for the half of
      the world that tool was written for.
    * **lat < -15** -- December to March, which for the southern hemisphere means
      the window *starts in the previous calendar year*. Hence ``year_offset``:
      the composite labelled 2020 runs December 2019 to March 2020. Compositing
      a southern point over June-September instead gives its dry season, and a
      dry-season NDVI series read as a growing-season one says "vegetation loss"
      about a place where nothing happened.

    Returned as data rather than applied here, because the app's live chip
    builder has to use the *same* window or the filmstrip and the chart disagree.
    """
    if abs(lat) <= 15:
        return {"name": "tropical (full year)", "start_month": 1, "months": 12,
                "year_offset": 0}
    if lat > 15:
        return {"name": "northern (Jun-Sep)", "start_month": 6, "months": 4,
                "year_offset": 0}
    return {"name": "southern (Dec-Mar)", "start_month": 12, "months": 4,
            "year_offset": -1}


def season_key(lat: float) -> str:
    return growing_season(lat)["name"]


def season_groups(points: list[dict]) -> dict[str, list[int]]:
    """Point indices grouped by season, so each window is composited once."""
    groups: dict[str, list[int]] = {}
    for i, point in enumerate(points):
        groups.setdefault(season_key(float(point["lat"])), []).append(i)
    return groups


# ---------------------------------------------------------------------------
# the schema: written once per batch, not once per point
# ---------------------------------------------------------------------------
#: One row per dataset-year, and `pair` / `seq` say how the app should COLLAPSE
#: them for display. Written out flat this is twenty-three rows, which pushed
#: the annual-index chart -- the more useful instrument -- clean off the bottom
#: of the panel. Rows sharing a `pair` render as one line, `a -> b`, which is
#: also the shape of the question being asked; rows sharing a `seq` render as
#: one line of the whole sequence.
#:
#: `end` is the last year the dataset can answer for and is rendered beside
#: every value -- the rule the app establishes is that nothing goes on screen
#: without one.
VALUE_ROWS = [
    {"group": "state", "group_label": "What is here", "key": "dw_2018", "label": "Dynamic World 2018", "dataset": ASSETS["dw"],
     "end": 2018, "res": "10 m", "pair": "dw", "pair_label": "Dynamic World"},
    {"group": "state", "group_label": "What is here", "key": "dw_2024", "label": "Dynamic World 2024", "dataset": ASSETS["dw"],
     "end": 2024, "res": "10 m", "pair": "dw", "pair_label": "Dynamic World"},
    {"group": "built", "group_label": "Built", "key": "dw_built_2018", "label": "built prob. 2018", "dataset": ASSETS["dw"],
     "end": 2018, "res": "10 m", "pair": "dwbuilt", "pair_label": "built probability"},
    {"group": "built", "group_label": "Built", "key": "dw_built_2024", "label": "built prob. 2024", "dataset": ASSETS["dw"],
     "end": 2024, "res": "10 m", "pair": "dwbuilt", "pair_label": "built probability"},
    {"group": "veg", "group_label": "Farmed or felled", "key": "dw_crop_2018", "label": "crop prob. 2018", "dataset": ASSETS["dw"],
     "end": 2018, "res": "10 m", "pair": "dwcrop", "pair_label": "crop probability"},
    {"group": "veg", "group_label": "Farmed or felled", "key": "dw_crop_2024", "label": "crop prob. 2024", "dataset": ASSETS["dw"],
     "end": 2024, "res": "10 m", "pair": "dwcrop", "pair_label": "crop probability"},
    {"group": "veg", "group_label": "Farmed or felled", "key": "dw_tree_2018", "label": "tree prob. 2018", "dataset": ASSETS["dw"],
     "end": 2018, "res": "10 m", "pair": "dwtree", "pair_label": "tree probability"},
    {"group": "veg", "group_label": "Farmed or felled", "key": "dw_tree_2024", "label": "tree prob. 2024", "dataset": ASSETS["dw"],
     "end": 2024, "res": "10 m", "pair": "dwtree", "pair_label": "tree probability"},
    {"group": "state", "group_label": "What is here", "key": "wc_2020", "label": "WorldCover 2020", "dataset": ASSETS["wc_2020"],
     "end": 2020, "res": "10 m",
     "note": "v100 and v200 disagree on this boundary often enough that the "
             "disagreement is itself worth seeing.", "pair": "wc", "pair_label": "ESA WorldCover"},
    {"group": "state", "group_label": "What is here", "key": "wc_2021", "label": "WorldCover 2021", "dataset": ASSETS["wc_2021"],
     "end": 2021, "res": "10 m", "pair": "wc", "pair_label": "ESA WorldCover"},
    {"group": "veg", "group_label": "Farmed or felled", "key": "hansen_lossyear", "label": "Hansen loss year",
     "dataset": ASSETS["hansen"], "end": 2025, "res": "30 m",
     "note": "0 means no loss recorded, not 'no loss'."},
    {"group": "built", "group_label": "Built", "key": "ghsl_built_2015", "label": "GHSL built 2015 (m²)",
     "dataset": ASSETS["ghsl"], "end": 2015, "res": "100 m", "pair": "ghsl", "pair_label": "GHSL built (m²)"},
    {"group": "built", "group_label": "Built", "key": "ghsl_built_2020", "label": "GHSL built 2020 (m²)",
     "dataset": ASSETS["ghsl"], "end": 2020, "res": "100 m",
     "note": "Epochs are 2015 and 2020 against a 2018 -> 2024 question. The "
             "asset also carries 2025 and 2030, which are EXTRAPOLATED, not "
             "observed -- which is why 2020 is the last one read here.",
     "pair": "ghsl", "pair_label": "GHSL built (m²)"},
    {"group": "veg", "group_label": "Farmed or felled",
     "key": "gfc2020_forest", "label": "JRC forest 2020",
     "dataset": ASSETS["gfc2020"], "end": 2020, "res": "10 m",
     "note": "The EU deforestation-regulation definition: >0.5 ha, >5 m, >10% "
             "canopy, agricultural plantations EXCLUDED. Where this says no "
             "and Hansen says tree cover, the difference is usually a "
             "plantation -- which on this legend is Cropland, not Nature."},
    {"group": "terrain", "group_label": "Terrain & water", "key": "slope_deg", "label": "slope (°)", "dataset": ASSETS["dem"],
     "end": 2021, "res": "30 m",
     "note": "A weaker clue than it looks: the bare-ground-read-as-built-up "
             "error actually gets rarer as the ground steepens. What predicts "
             "it is bare ground, not steepness."},
    {"group": "terrain", "group_label": "Terrain & water", "key": "bare_flag", "label": "WorldCover bare here",
     "dataset": ASSETS["wc_2021"], "end": 2021, "res": "10 m",
     "note": "The model is about three times more likely to misread bare "
             "ground than anything else, and it is more sure of itself when "
             "it does."},
    {"group": "terrain", "group_label": "Terrain & water", "key": "water_occurrence", "label": "water occurrence (%)",
     "dataset": ASSETS["gsw"], "end": 2021, "res": "30 m",
     "note": "The wetland-read-as-a-field half of the problem. 0 means no "
             "water was ever recorded at this pixel -- which is also what open "
             "ocean reads, because the layer answers everywhere."},
]

VALUE_ROWS += [
    {"group": "state", "group_label": "What is here",
     "key": f"esri_{y}", "label": f"ESRI land cover {y}",
     "dataset": ASSETS["esri_lc"], "end": y, "res": "10 m",
     # One line of seven years, not seven lines. The sequence IS the reading:
     # it names a change year rather than bracketing one, and that is only
     # visible when the years sit next to each other.
     "seq": "esri", "seq_label": "ESRI annual land cover", "at": y,
     "note": "Annual, so it names a change year rather than bracketing one."}
    for y in ESRI_YEARS
]


def evidence_schema(points: list[dict]) -> dict:
    """The batch-level half of the contract.

    Dataset names, end years and resolutions are written once here rather than
    repeated on all hundred points -- which is most of what keeps a batch under
    the ~1 MB the app is built around.
    """
    seasons = sorted({season_key(float(p["lat"])) for p in points})
    return {
        "version": EVIDENCE_VERSION,
        "rows": VALUE_ROWS,
        "timeline": {
            "years": list(range(YEAR_FIRST, YEAR_LAST + 1)),
            "series": ["ndvi", "ndmi", "nbr"],
            "bands": list(S2_BANDS),
            # The app's live chip builder reads this so its composite matches
            # the chart's. Where a batch spans hemispheres there is more than
            # one; the chip builder falls back to the point's own latitude.
            "season": growing_season(float(points[0]["lat"])) if points else None,
            "seasons_used": seasons,
            "recipe": "S2_SR_HARMONIZED joined to S2_CLOUD_PROBABILITY, "
                      "probability < 40, SCL 3/8/9/10/11 dropped, median",
        },
    }


# ---------------------------------------------------------------------------
# Earth Engine
# ---------------------------------------------------------------------------
def _ee():
    """Import and initialise Earth Engine, late, so the pure half of this module
    stays importable (and testable) on a machine with no credentials."""
    import ee
    try:
        ee.Initialize()
    except Exception:
        ee.Initialize(project=None)
    return ee


def _fc(ee, points: list[dict], indices: list[int] | None = None):
    """The points as one FeatureCollection. Everything below reduces over this
    once per dataset rather than once per point."""
    use = indices if indices is not None else range(len(points))
    return ee.FeatureCollection([
        ee.Feature(ee.Geometry.Point([float(points[i]["lon"]),
                                      float(points[i]["lat"])]),
                   {"_i": i})
        for i in use
    ])


#: Points per Earth Engine request. Small enough that a chunk of globally
#: scattered points does not blow the per-request memory limit on the S2 join,
#: large enough that a 100-point batch is a handful of round trips per dataset
#: rather than a hundred.
CHUNK = 20

#: Splits each request's work into more, smaller tiles. Slower, and the reason a
#: chunk that would otherwise die with "User memory limit exceeded" completes.
TILE_SCALE = 4

#: Concurrent Sentinel-2 timeline requests, one per (point, year). Earth Engine
#: rate-limits interactive calls, so this buys much less than its number
#: suggests: a measured 100-point batch took 36 minutes against the ~2 that 900
#: requests x 2.4 s / 16 workers would predict. Raising it further mostly buys
#: retries.
TIMELINE_WORKERS = 16


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _reduce(ee, image, fc, scale: int, names: list[str]) -> dict[int, dict]:
    """One reduceRegions, one getInfo, results keyed by the point's index.

    ``names`` is the image's bands, and passing them is not decoration.
    ``reduceRegions`` labels its output by BAND NAME only when the image has two
    or more bands; with exactly one it labels it by the REDUCER name -- ``first``
    -- so every single-band dataset came back under a key nothing was looking
    for and was silently dropped. That cost six of the sixteen point-value rows
    (both WorldCover vintages, the bare flag, Hansen and both GHSL epochs), and
    it showed up as "no data" rather than as an error.
    """
    reducer = ee.Reducer.first()
    if len(names) == 1:
        reducer = reducer.setOutputs(names)      # ...and only then; see above
    out = image.reduceRegions(collection=fc, reducer=reducer, scale=scale,
                              tileScale=TILE_SCALE)
    got = out.getInfo()
    result: dict[int, dict] = {}
    for feature in got.get("features", []):
        props = dict(feature.get("properties") or {})
        i = props.pop("_i", None)
        if i is not None:
            result[int(i)] = props
    return result


def _empty_s2(ee):
    """A fully-masked image carrying the six band names.

    ``median()`` of an EMPTY collection returns an image with **no bands**, and
    the next ``.select('B2')`` dies with *"Band pattern 'B2' was applied to an
    Image with no bands"*. Empty is not an exceptional case here: the southern
    growing window for 2017 starts in December **2016**, before Sentinel-2 SR
    coverage begins, and a global batch always has some point-year with no clear
    scene at all. A masked image is the honest answer -- it reduces to "no
    value", which is what ``_round`` turns into ``None`` and the chart draws as a
    gap rather than as a zero.
    """
    return (ee.Image.constant([0] * len(S2_BANDS)).rename(list(S2_BANDS))
            .updateMask(ee.Image.constant(0)).toFloat())


def _composite(ee, col):
    """Median of the collection, or a fully-masked stand-in when it is empty."""
    return ee.Image(ee.Algorithms.If(col.size().gt(0),
                                     col.median().toFloat(), _empty_s2(ee)))


def _scene_count(ee, col):
    """Clear scenes per pixel, and 0 rather than a bandless image when empty.

    This is what separates "flat because nothing changed" from "flat because
    there was never anything to see", so it must not itself go missing.
    """
    return ee.Image(ee.Algorithms.If(col.size().gt(0),
                                     col.select("B8").count(),
                                     ee.Image.constant(0))).rename("n")


def _masked_s2(ee, year: int, season: dict, region):
    """Growing-season cloud-masked S2 SR scenes, the inspector's recipe."""
    start = ee.Date.fromYMD(year + season["year_offset"],
                            season["start_month"], 1)
    end = start.advance(season["months"], "month")
    col = (ee.ImageCollection(ASSETS["s2"]).filterDate(start, end)
           .filterBounds(region))
    clouds = (ee.ImageCollection(ASSETS["s2_clouds"]).filterDate(start, end)
              .filterBounds(region))
    joined = ee.Join.saveFirst("c").apply(
        col, clouds,
        ee.Filter.equals(leftField="system:index", rightField="system:index"))

    def mask(image):
        image = ee.Image(image)
        prob = ee.Image(image.get("c")).select("probability")
        scl = image.select("SCL")
        bad = scl.eq(3).Or(scl.gte(8).And(scl.lte(11)))
        # Bands selected BEFORE the median -- see the module docstring.
        return image.updateMask(prob.lt(40).And(bad.Not())).select(list(S2_BANDS))

    return ee.ImageCollection(joined.map(mask))


def timeline_for(ee, points: list[dict], indices: list[int], season: dict,
                 scale: int = 10, workers: int = TIMELINE_WORKERS,
                 say=None) -> dict[int, dict]:
    """The annual series for one season group, one request per (point, year).

    See the module docstring for why neither points nor years is the right thing
    to batch on here. The requests are independent, so they go through a thread
    pool; without it this is ~900 sequential round trips.
    """
    years = list(range(YEAR_FIRST, YEAR_LAST + 1))
    out = {i: {"ndvi": [None] * len(years), "ndmi": [None] * len(years),
               "nbr": [None] * len(years), "n": [None] * len(years),
               "bands": {b: [None] * len(years) for b in S2_BANDS}}
           for i in indices}
    tasks = [(i, k, year) for i in indices for k, year in enumerate(years)]
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_timeline_cell, ee, points[i], season, scale,
                               year): (i, k)
                   for i, k, year in tasks}
        for future in as_completed(futures):
            i, k = futures[future]
            cell = future.result()
            for key in ("ndvi", "ndmi", "nbr", "n"):
                out[i][key][k] = cell[key]
            for band in S2_BANDS:
                out[i]["bands"][band][k] = cell["bands"][band]
            done += 1
            if say and done % 100 == 0:
                say(f"    {done} / {len(tasks)} point-years")
    return out


def _timeline_cell(ee, point: dict, season: dict, scale: int,
                   year: int, tries: int = 3) -> dict:
    """One point, one year. Retried: transient Earth Engine failures are normal
    under concurrency, and losing a year to one would show up as a gap in the
    chart that looks exactly like a cloudy season."""
    geom = ee.Geometry.Point([float(point["lon"]), float(point["lat"])])
    blank = {"ndvi": None, "ndmi": None, "nbr": None, "n": None,
             "bands": {b: None for b in S2_BANDS}}
    for attempt in range(tries):
        try:
            col = _masked_s2(ee, year, season, geom)
            median = _composite(ee, col)
            image = (median.normalizedDifference(["B8", "B4"]).rename("ndvi")
                     .addBands(median.normalizedDifference(["B8", "B11"]).rename("ndmi"))
                     .addBands(median.normalizedDifference(["B8", "B12"]).rename("nbr"))
                     .addBands(median.select(list(S2_BANDS)))
                     .addBands(_scene_count(ee, col)))
            # reduceRegion, not reduceRegions: a plain dict keyed by band name,
            # with none of the single-band output-naming trap `_reduce` dodges.
            got = image.reduceRegion(reducer=ee.Reducer.first(), geometry=geom,
                                     scale=scale, tileScale=TILE_SCALE
                                     ).getInfo() or {}
            return {"ndvi": _round(got.get("ndvi"), 4),
                    "ndmi": _round(got.get("ndmi"), 4),
                    "nbr": _round(got.get("nbr"), 4),
                    "n": _int(got.get("n")),
                    "bands": {b: _int(got.get(b)) for b in S2_BANDS}}
        except Exception:
            if attempt == tries - 1:
                return blank
            time.sleep(2 ** attempt)
    return blank


def point_values(ee, points: list[dict], scale: int = 10,
                 chunk: int = CHUNK) -> dict[int, dict]:
    """The core point values, in chunks of `chunk` points."""
    out: dict[int, dict] = {}
    for part in _chunks(list(range(len(points))), chunk):
        out.update(_point_values_chunk(ee, points, part, scale))
    return out


def _point_values_chunk(ee, points: list[dict], indices: list[int],
                        scale: int) -> dict[int, dict]:
    """One reduceRegions per dataset, over this chunk."""
    fc = _fc(ee, points, indices)
    out: dict[int, dict] = {i: {} for i in indices}

    def put(got, mapping):
        for i, props in got.items():
            for src, (dst, cast) in mapping.items():
                if src in props:
                    out[i][dst] = cast(props[src])

    # ── Dynamic World: the modal class and three probabilities, per year ────
    for year in LABEL_YEARS:
        col = (ee.ImageCollection(ASSETS["dw"])
               .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
               .filterBounds(fc.geometry()))     # the points, not their bounds
        image = (col.select("label").mode().rename("label")
                 .addBands(col.select("built").mean().rename("built"))
                 .addBands(col.select("crops").mean().rename("crops"))
                 .addBands(col.select("trees").mean().rename("trees")))
        got = _reduce(ee, image, fc, scale, ["label", "built", "crops", "trees"])
        put(got, {
            "label": (f"dw_{year}", lambda v: _name(DW_NAMES, v)),
            "built": (f"dw_built_{year}", lambda v: _round(v, 3)),
            "crops": (f"dw_crop_{year}", lambda v: _round(v, 3)),
            "trees": (f"dw_tree_{year}", lambda v: _round(v, 3)),
        })

    # ── ESA WorldCover, both vintages ──────────────────────────────────────
    for key, asset in (("wc_2020", ASSETS["wc_2020"]),
                       ("wc_2021", ASSETS["wc_2021"])):
        # `.first()` is right here: both WorldCover collections hold exactly one
        # image, a global mosaic. (Checked -- it is not the usual tile-grid trap.)
        image = ee.ImageCollection(asset).first().select("Map").rename("wc")
        got = _reduce(ee, image, fc, scale, ["wc"])
        put(got, {"wc": (key, lambda v: WC_NAMES.get(_int(v), _int(v)))})
        if key == "wc_2021":
            for i, props in got.items():
                code = _int(props.get("wc"))
                # Absent, not "no": a point WorldCover cannot answer for is not
                # a point that is known not to be bare, and §AL-T's whole finding
                # is that bare ground is where the model is confidently wrong.
                if code is not None:
                    out[i]["bare_flag"] = "yes" if code == 60 else "no"

    # ── Hansen loss year ───────────────────────────────────────────────────
    # unmask(0) is required, not cosmetic: `reduceRegions` returns NOTHING for
    # this asset while the band is masked, even where `reduceRegion` on the same
    # point reports it unmasked. 0 is also the dataset's own encoding of "no loss
    # recorded", so the unmask says what the data says.
    ly = (ee.Image(ASSETS["hansen"]).select("lossyear").unmask(0)
          .rename("lossyear"))
    got = _reduce(ee, ly, fc, 30, ["lossyear"])
    put(got, {"lossyear": ("hansen_lossyear",
                           lambda v: 2000 + _int(v) if _int(v) else "none")})

    # ── GHSL built surface, both epochs ────────────────────────────────────
    ghsl = ee.ImageCollection(ASSETS["ghsl"])
    for epoch in ("2015", "2020"):
        image = (ghsl.filter(ee.Filter.eq("system:index", epoch)).first()
                 .select("built_surface").rename("built"))
        got = _reduce(ee, image, fc, 100, ["built"])
        put(got, {"built": (f"ghsl_built_{epoch}", _int)})

    # ── ESRI annual land cover, one band per year, one round trip ──────────
    esri = ee.ImageCollection(ASSETS["esri_lc"])
    bands = None
    for year in ESRI_YEARS:
        one = (esri.filterDate(f"{year}-01-01", f"{year + 1}-01-01")
               .mosaic().rename(f"esri_{year}"))
        bands = one if bands is None else bands.addBands(one)
    got = _reduce(ee, bands, fc, scale, [f"esri_{y}" for y in ESRI_YEARS])
    put(got, {f"esri_{y}": (f"esri_{y}", lambda v: ESRI_NAMES.get(_int(v), ""))
              for y in ESRI_YEARS})

    # ── JRC global forest cover 2020 ───────────────────────────────────────
    # unmask(0) for the same reason as Hansen and GSW: the band is masked
    # off-forest and `reduceRegions` returns nothing at all rather than a zero,
    # so every non-forest point would come back absent -- which the app renders
    # as "no answer" when the answer is "not forest".
    forest = (ee.Image(ASSETS["gfc2020"]).select("Map").unmask(0)
              .rename("forest"))
    got = _reduce(ee, forest, fc, scale, ["forest"])
    put(got, {"forest": ("gfc2020_forest",
                         lambda v: "yes" if _int(v) == 1 else "no")})

    # ── terrain and water ──────────────────────────────────────────────────
    dem = (ee.ImageCollection(ASSETS["dem"]).select("DEM").mosaic()
           .setDefaultProjection("EPSG:3857", None, 30))
    slope = ee.Terrain.slope(dem).rename("slope")
    # unmask(0) for the same reason as Hansen: `reduceRegions` returns nothing
    # for this asset otherwise, at any point, land or water. 0 therefore means
    # "no water in the JRC record here", which the row's note says out loud --
    # over open ocean that reads 0 too, and a silent 0 there would be backwards.
    occ = (ee.Image(ASSETS["gsw"]).select("occurrence").rename("occ")
           .unmask(0))
    got = _reduce(ee, slope.addBands(occ), fc, 30, ["slope", "occ"])
    put(got, {"slope": ("slope_deg", lambda v: _round(v, 1)),
              "occ": ("water_occurrence", lambda v: _round(v, 0))})
    return out


# ---------------------------------------------------------------------------
def _round(value, digits):
    """None stays None. A masked pixel is 'no answer', which is not 0.0 -- and a
    0.0 NDVI plotted as a real value is a bare-ground reading that never
    happened."""
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _int(value):
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _name(names: list[str], value):
    i = _int(value)
    return names[i] if i is not None and 0 <= i < len(names) else ""


# ---------------------------------------------------------------------------
def add_evidence(batch: dict, *, scale: int = 10, chunk: int = CHUNK,
                 quiet: bool = False) -> dict:
    """Fill in ``evidence_schema`` on the batch and ``evidence`` on each point."""
    points = batch.get("points") or []
    if not points:
        return batch
    ee = _ee()
    say = (lambda *a: None) if quiet else print

    say(f"  {len(points)} points")
    groups = season_groups(points)
    for name, indices in groups.items():
        say(f"  season {name}: {len(indices)} points")

    say(f"  point values… ({len(points)} points, {chunk} per request)")
    values = point_values(ee, points, scale=scale, chunk=chunk)

    timelines: dict[int, dict] = {}
    for name, indices in groups.items():
        season = growing_season(float(points[indices[0]]["lat"]))
        say(f"  timeline {YEAR_FIRST}-{YEAR_LAST}, {name}…")
        timelines.update(timeline_for(ee, points, indices, season, scale=scale,
                                      say=say))

    for i, point in enumerate(points):
        point["evidence"] = {"v": values.get(i, {}), "t": timelines.get(i, {})}
    batch["evidence_schema"] = evidence_schema(points)
    batch["evidence_version"] = EVIDENCE_VERSION
    return batch


def size_note(batch: dict) -> str:
    """The app is built around a batch of ~100 points staying near 1 MB."""
    n = len(json.dumps(batch))
    warn = "  <- over 1 MB; drop a series or shorten the year range" \
        if n > 1_000_000 else ""
    return f"  batch JSON is {n / 1024:.0f} KiB{warn}"


# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", type=Path,
                        help="an existing batch JSON to add evidence to, in place")
    parser.add_argument("--out", type=Path,
                        help="write here instead of over --batch")
    parser.add_argument("--scale", type=int, default=10,
                        help="sampling scale in metres (default 10)")
    parser.add_argument("--chunk", type=int, default=CHUNK,
                        help=f"points per Earth Engine request (default {CHUNK}). "
                             "Lower it if a global batch still answers 'User "
                             "memory limit exceeded'.")
    parser.add_argument("--show-seasons", action="store_true",
                        help="print the season table and exit; needs no Earth "
                             "Engine, and is the part most worth checking")
    args = parser.parse_args()

    if args.show_seasons:
        print("latitude  window")
        for lat in (70, 45, 20, 10, 0, -10, -20, -35, -60):
            season = growing_season(lat)
            print(f"  {lat:>4}    {season['name']:<22} "
                  f"start month {season['start_month']:>2}, "
                  f"{season['months']} months, year offset "
                  f"{season['year_offset']}")
        print("\nThe southern offset is the one that matters: a composite "
              "labelled 2020\nruns December 2019 to March 2020. Compositing a "
              "southern point over\nJune-September gives its dry season, and a "
              "dry-season NDVI series read as\na growing-season one says "
              "'vegetation loss' about a place where nothing\nhappened.")
        return

    if not args.batch:
        parser.error("pass --batch (or --show-seasons)")
    batch = json.loads(args.batch.read_text())
    print(f"{args.batch}")
    add_evidence(batch, scale=args.scale, chunk=args.chunk)
    out = args.out or args.batch
    out.write_text(json.dumps(batch, indent=1))
    print(size_note(batch))
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()

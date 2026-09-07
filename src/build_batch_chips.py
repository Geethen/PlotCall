#!/usr/bin/env python
"""Bake a batch's Sentinel-2 filmstrips to static files before labelling.

Static hosting has no server-side image cache. Because all points in a batch
are known in advance, their filmstrips can be prepared once instead of making
the interpreter's first view wait for Earth Engine. Each point's annual images
are stored in one horizontal sprite and sliced in the browser.

The four RGB combinations can be baked. Index views (NDVI, NDMI and NBR) remain
live because they are inexpensive normalized differences. Display bounds are
estimated per point but shared across bands and years: this preserves relative
colour and prevents year-specific auto-stretching from creating apparent
change.

Usage
-----
    python src/build_batch_chips.py --batch app/batches/b001.json \
        --combo SWIR1/NIR/GREEN NIR/RED/GREEN NIR/SWIR1/RED RED/GREEN/BLUE
    python src/build_batch_chips.py --batch app/batches/b001.json --dry-run

Runs are resumable. Existing sprites made with the current
``CHIP_BAKE_VERSION`` are skipped unless ``--force`` is used.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import label_cell
from build_batch_evidence import growing_season

#: Bumped when the recipe below changes. The app compares it with what it finds
#: in the batch and falls back to live Earth Engine on a mismatch, so a stale
#: bake degrades to the old behaviour instead of showing the wrong picture.
#:
#: chip1 -> chip2: the per-point stretch below. A chip1 sprite was rendered
#: through the fixed bounds and would disagree with a chip2 tint, so the bump is
#: not cosmetic -- an un-rebaked batch must fall back rather than mix the two.
#:
#: chip2 -> chip3: the marks. A chip2 sprite carries one red ring at
#: `max(width_m * 0.02, 6)` m, which is 6x the area of the cell being called and
#: changes size with the width slider; chip3 carries the labelling cell itself
#: (`src/label_cell.py`) with the ring demoted to a white locator. The app draws
#: that same cell on the map, so a chip2 sprite next to a chip3 map shows the
#: interpreter two different footprints for one call.
CHIP_BAKE_VERSION = "chip3"

#: MUST MATCH `CHIP_SCENE_CAP` in label_app.html. The season comes from
#: `growing_season` above so baked and live paths use the same time window.
SCENE_CAP = 12

#: MUST MATCH `CHIP_DIM`. The strip cell is 88 CSS px; 176 covers a 2x display.
CELL = 176

#: Web Mercator. The mosaic is built in EPSG:3857 so each year can be shifted a
#: whole box to the east with `translate`, and 3857 "metres" are ground metres
#: only at the equator -- hence the cos(lat) scaling of the half-width.
R_EARTH = 6378137.0

S2_BANDS = ("B2", "B3", "B4", "B8", "B11", "B12")

#: MUST MATCH `CHIP_RGB` / `CHIP_INDEX`. Only the three-band schemes are
#: bakeable here; an index scheme is a normalised difference through a ramp and
#: is cheap enough live that baking it buys little.
COMBOS = {
    "SWIR1/NIR/GREEN": (["B11", "B8", "B3"], 0, 3500),
    "NIR/RED/GREEN":   (["B8", "B4", "B3"], 0, 3000),
    "NIR/SWIR1/RED":   (["B8", "B11", "B4"], 0, 3500),
    "RED/GREEN/BLUE":  (["B4", "B3", "B2"], 0, 2500),
}


#: THE STRETCH IS PER POINT, AND SHARED BY ALL NINE YEARS.
#:
#: The fixed `min`/`max` values in COMBOS can saturate bright or dark scenes.
#: Per-point bounds retain useful contrast across geographically varied data.
#:
#: Per band, per point, from the point's own nine years -- and NEVER per year.
#: A per-year auto-stretch would renormalise each cell independently, which is
#: precisely the thing a change filmstrip must not do: it makes a real
#: brightening invisible and a stable point flicker. The years share one ramp so
#: a difference between cells is a difference on the ground.
STRETCH_PCT = (2, 98)

#: The only guard, and it is against infinite gain rather than against a wide
#: ramp. A uniform surface -- open water, a dune field -- has a percentile pair
#: a few DN apart, and mapping four DN onto 0-255 shows sensor noise as though
#: it were ground texture. 120 DN is 0.012 reflectance, comfortably above the
#: noise floor at these radiances and small enough that a lake still gets real
#: contrast out of its shoreline. Floored spans are grown about their CENTRE,
#: so the surface keeps its brightness and only loses contrast it never had.
STRETCH_MIN_SPAN = 120.0
STRETCH_ABS_MAX = 10000.0

#: Scale of the percentile reduce. The picture is 10 m data; 20 m is enough for
#: a distribution and makes the extra request cheap.
STRETCH_SCALE = 20


def slug(combo: str) -> str:
    """`SWIR1/NIR/GREEN` -> `swir1_nir_green`. MUST MATCH `comboSlug()`."""
    return combo.lower().replace("/", "_")


def _ee():
    import ee
    try:
        ee.Number(1).getInfo()
    except Exception:
        ee.Initialize()
    return ee


def merc(lon: float, lat: float) -> tuple[float, float]:
    return (R_EARTH * math.radians(lon),
            R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def composite(ee, lat: float, year: int, box, cap: int = SCENE_CAP):
    """The app's `s2Chip`, in Python. Same season, same mask, same cap."""
    season = growing_season(lat)
    start = ee.Date.fromYMD(year + season["year_offset"],
                            season["start_month"], 1)
    end = start.advance(season["months"], "month")
    col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
           .filterDate(start, end).filterBounds(box))
    if cap:
        # `distinct` before `limit`, or the cap counts GRANULES. MGRS tiles
        # overlap by ~10 km and orbits overlap heavily at high latitude, so a
        # point in an overlap spends two of its twelve slots on one overpass
        # and the median is built from half the dates it looks like. Sorted
        # first, so what survives each date is that date's clearest granule.
        col = (col.sort("CLOUDY_PIXEL_PERCENTAGE")
               .distinct("DATATAKE_IDENTIFIER").limit(cap))
    clouds = (ee.ImageCollection("COPERNICUS/S2_CLOUD_PROBABILITY")
              .filterDate(start, end).filterBounds(box))
    joined = ee.Join.saveFirst("c").apply(col, clouds, ee.Filter.equals(
        leftField="system:index", rightField="system:index"))

    def mask(image):
        image = ee.Image(image)
        prob = ee.Image(image.get("c")).select("probability")
        scl = image.select("SCL")
        bad = scl.eq(3).Or(scl.gte(8).And(scl.lte(11)))
        # Bands selected BEFORE the median: early scenes carry a different band
        # set and reducing the raw collection raises "Expected a homogeneous
        # image collection".
        return (image.updateMask(prob.lt(40).And(bad.Not()))
                .select(list(S2_BANDS)))

    return ee.ImageCollection(joined).map(mask).median()


def box_for(ee, point: dict, width_m: float):
    """The point's square footprint, and the 3857 half-width that built it.

    3857 "metres" are ground metres only at the equator, hence the cos(lat).
    """
    lon, lat = float(point["lon"]), float(point["lat"])
    mx, my = merc(lon, lat)
    half = (width_m / 2) / math.cos(math.radians(lat))    # 3857 units
    return ee.Geometry.Rectangle([mx - half, my - half, mx + half, my + half],
                                 "EPSG:3857", False), half, mx, my, lat


def stretch_for(ee, point: dict, years: list[int], width_m: float) -> dict:
    """Per-band display bounds for one point, pooled over its nine years.

    ONE Earth Engine request for the whole point, whatever combos are baked
    afterwards: the nine yearly composites are stacked with `toBands()` and
    reduced once, so this is a per-point cost and not a per-combo one. It is
    cached in the batch (`chips.stretch`), so baking a second scheme re-uses it.

    Pooling is the MEDIAN of the per-year percentiles, not the min/max. The
    extremes would let one hazy year set the ramp for the other eight; the
    median gives a ramp the nine years agree on and clips a genuine one-year
    excursion slightly, which is the right way round for a change instrument.
    """
    box, _, _, _, lat = box_for(ee, point, width_m)
    stack = ee.ImageCollection([
        composite(ee, lat, y, box).set("system:index", str(i))
        for i, y in enumerate(years)
    ]).toBands()
    lo_pct, hi_pct = STRETCH_PCT
    stats = stack.reduceRegion(
        reducer=ee.Reducer.percentile([lo_pct, hi_pct]),
        geometry=box, scale=STRETCH_SCALE, maxPixels=int(1e7),
        bestEffort=True).getInfo() or {}

    out = {}
    for band in S2_BANDS:
        los, his = [], []
        for i in range(len(years)):
            lo = stats.get(f"{i}_{band}_p{lo_pct}")
            hi = stats.get(f"{i}_{band}_p{hi_pct}")
            if lo is not None and hi is not None:
                los.append(float(lo))
                his.append(float(hi))
        if not his:                       # no clear pixel in any year: keep the
            out[band] = None              # global ramp rather than invent one
            continue
        lo = sorted(los)[len(los) // 2]
        hi = sorted(his)[len(his) // 2]
        if hi - lo < STRETCH_MIN_SPAN:                  # grow about the centre
            mid = (hi + lo) / 2
            lo, hi = mid - STRETCH_MIN_SPAN / 2, mid + STRETCH_MIN_SPAN / 2
        lo = max(lo, 0.0)
        hi = min(max(hi, lo + STRETCH_MIN_SPAN), STRETCH_ABS_MAX)
        out[band] = [round(lo, 1), round(hi, 1)]
    return out


def combo_bounds(combo: str, stretch: dict | None):
    """`visualize` min/max for one combo: ONE ramp for all three channels.

    MUST MATCH `comboBounds()` in label_app.html -- the tint the app paints
    before an image lands is mixed through the same numbers, and a tint that
    disagrees with its own sprite is a lie about the pixel.

    The three channels share one ramp. Stretching each band independently is a
    decorrelation stretch that changes hue. Using narrow, year-specific bounds
    can also turn small atmospheric differences into large colour differences,
    which is inappropriate for a change-comparison filmstrip.

    An affine transform applied identically to all three channels is an exposure
    and contrast adjustment, not a recolouring. `lo` is the darkest of the three
    bands' floors and `hi` the brightest of their ceilings, so nothing clips and
    the relationship between the channels -- the part that carries the meaning
    -- is untouched.
    """
    bands, vmin, vmax = COMBOS[combo]
    if not stretch:
        return bands, vmin, vmax
    pairs = [stretch.get(b) for b in bands]
    if any(p is None for p in pairs):
        return bands, vmin, vmax
    lo = min(p[0] for p in pairs)
    hi = max(p[1] for p in pairs)
    if hi - lo < STRETCH_MIN_SPAN:
        mid = (hi + lo) / 2
        lo, hi = max(mid - STRETCH_MIN_SPAN / 2, 0.0), mid + STRETCH_MIN_SPAN / 2
    return bands, round(lo, 1), round(hi, 1)


def sprite_url(ee, point: dict, years: list[int], combo: str, width_m: float,
               cell: int, stretch: dict | None = None) -> str:
    """One thumbnail URL for the whole filmstrip.

    Each year is clipped to the point's box and shifted `i` boxes east, then
    mosaicked, so the returned PNG is `len(years)` cells wide and the browser
    slices it with `background-position`.
    """
    box, half, mx, my, lat = box_for(ee, point, width_m)
    lon = float(point["lon"])
    bands, vmin, vmax = combo_bounds(combo, stretch)

    # TWO marks, and they say different things. The RED SQUARE is the labelling
    # cell -- the actual Sentinel-2 pixel, `src/label_cell.py` -- so what the
    # interpreter judges on the map is outlined on the picture as well. The
    # WHITE RING is a locator and nothing more: its radius is a fixed fraction
    # of the chip width, which is a fixed ~7 screen pixels at every width, and
    # it names no ground area at all.
    #
    # It used to be one red ring at `max(width_m * 0.02, 6)` metres, which at
    # the default 640 m width is a 12.8 m radius against a 5 m cell -- so the
    # only footprint drawn on the imagery was 6x the area of the thing being
    # called, and it changed size when the width slider moved. `chipUrl` in
    # label_app.html paints the same two marks for the live path.
    paint = (ee.Image().byte()
             .paint(ee.Geometry.Point([lon, lat]).buffer(max(width_m * 0.02, 6)),
                    1, 1)
             .paint(label_cell.cell_geometry(ee, lon, lat), 2, 1))
    marker = (paint.visualize(palette=["ffffff", "ff2d2d"], min=1, max=2)
              .updateMask(paint.gt(0)))

    cells = []
    for i, year in enumerate(years):
        vis = (composite(ee, lat, year, box)
               .visualize(bands=bands, min=vmin, max=vmax)
               .blend(marker))
        cells.append(vis.clip(box).translate(i * 2 * half, 0, "meters",
                                             "EPSG:3857"))
    region = ee.Geometry.Rectangle(
        [mx - half, my - half, mx - half + len(years) * 2 * half, my + half],
        "EPSG:3857", False)
    return ee.ImageCollection(cells).mosaic().getThumbURL({
        "region": region, "dimensions": f"{cell * len(years)}x{cell}",
        "crs": "EPSG:3857", "format": "png"})


def bake_one(ee, point: dict, years: list[int], combo: str, width_m: float,
             cell: int, out: Path, fmt: str, quality: int,
             stretch: dict | None = None) -> tuple[str, int]:
    from PIL import Image
    import io
    url = sprite_url(ee, point, years, combo, width_m, cell, stretch)
    raw = urllib.request.urlopen(url, timeout=600).read()
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    want = (cell * len(years), cell)
    if image.size != want:                     # EE rounds; the slicer needs exact
        image = image.resize(want, Image.LANCZOS)
    buf = io.BytesIO()
    if fmt == "webp":
        image.save(buf, "WEBP", quality=quality, method=4)
    else:
        image.save(buf, "JPEG", quality=quality, optimize=True)
    # Write-then-rename, so an interrupted bake never leaves a half file that
    # the resume logic would then skip.
    tmp = out.with_suffix(out.suffix + ".part")
    tmp.write_bytes(buf.getvalue())
    os.replace(tmp, out)
    return point["id"], out.stat().st_size


def stretches(ee, batch: dict, points: list[dict], years: list[int],
              width_m: float, workers: int, force_stretch: bool) -> dict:
    """`{point_id: {band: [lo, hi]}}`, computed once and kept in the batch.

    Re-used across combos and across re-runs at the same width, because it is a
    property of the point and not of the scheme. A point whose reduce fails is
    dropped rather than defaulted: a missing entry means the global ramp, which
    is exactly the old behaviour for that one point.
    """
    old = (batch.get("chips") or {})
    have = old.get("stretch") or {}
    if float(old.get("stretch_width_m") or 0) != float(width_m) or force_stretch:
        have = {}
    todo = [p for p in points if p["id"] not in have]
    print(f"  stretch: {len(have)} cached, {len(todo)} to measure "
          f"(p{STRETCH_PCT[0]}-p{STRETCH_PCT[1]}, {STRETCH_SCALE} m)")
    if not todo:
        return have
    out = dict(have)
    done, failed, t0 = 0, 0, time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(stretch_for, ee, p, years, width_m): p
                   for p in todo}
        for fut in as_completed(futures):
            point = futures[fut]
            try:
                out[point["id"]] = fut.result()
            except Exception as err:
                failed += 1
                print(f"    stretch FAILED {point['id']}: "
                      f"{str(err).splitlines()[0][:100]}")
                continue
            done += 1
            if done % 20 == 0 or done == len(todo):
                print(f"    {done}/{len(todo)}  "
                      f"{done / max(time.time() - t0, 1e-9) * 60:.0f}/min",
                      flush=True)
    if failed:
        print(f"  {failed} point(s) keep the global ramp")
    return out


def bake(batch: dict, batch_path: Path, *, combo: str, width_m: float,
         cell: int, fmt: str, quality: int, workers: int, force: bool,
         dry_run: bool, stretch_mode: str = "point",
         force_stretch: bool = False, stale: bool | None = None) -> dict:
    schema = batch.get("evidence_schema") or {}
    years = ((schema.get("timeline") or {}).get("years")) or []
    if not years:
        raise SystemExit(
            "this batch declares no timeline years -- run build_batch_evidence.py "
            "first. The bake has to use the SAME year list the app renders cells "
            "for, or the sprite and the strip are off by one.")
    if combo not in COMBOS:
        raise SystemExit(f"unknown combo {combo!r}; one of {sorted(COMBOS)}")

    root = batch_path.parent / f"{batch['batch_id']}_chips" / slug(combo)
    points = batch["points"]
    # PASSED IN, not re-derived. `main()` bakes the schemes in one process and
    # carries the batch between them so the second reads the first's cached
    # ramp -- which means by the time scheme two asks, `batch["chips"]` already
    # says the new version and every remaining scheme reads as up to date. The
    # first scheme re-bakes, the other three keep their old marks, and the batch
    # ships stamped with a version three quarters of its sprites are not: the
    # app serves them, because the stamp is all it can check. Caught on the
    # chip2 -> chip3 re-bake of b001, three schemes deep.
    if stale is None:
        stale = (batch.get("chips") or {}).get("version") != CHIP_BAKE_VERSION
    todo = [p for p in points
            # Same rule as build_batch_dense.py: a sprite on disk is a skip
            # only if this recipe drew it. A CHIP_BAKE_VERSION bump means every
            # sprite in the directory carries the old marks.
            if force or stale or not (root / f"{p['id']}.{fmt}").exists()]

    print(f"{batch_path}")
    print(f"  {len(points)} points x {len(years)} years "
          f"({years[0]}-{years[-1]}) · {combo} · {width_m:g} m · {cell} px "
          f"cells · cap {SCENE_CAP}")
    if stale and not force:
        print(f"  bake version is now {CHIP_BAKE_VERSION} (this batch says "
              f"{(batch.get('chips') or {}).get('version')!r}) -- re-baking all")
    print(f"  -> {root}  ({len(todo)} to bake, "
          f"{len(points) - len(todo)} already there)")
    if dry_run:
        print("  --dry-run: nothing fetched")
        return {}

    root.mkdir(parents=True, exist_ok=True)
    ee = _ee()
    stretch = ({} if stretch_mode == "fixed"
               else stretches(ee, batch, points, years, width_m, workers,
                              force_stretch))
    done, failed, total_bytes = 0, [], 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(bake_one, ee, p, years, combo, width_m, cell,
                               root / f"{p['id']}.{fmt}", fmt, quality,
                               stretch.get(p["id"])): p
                   for p in todo}
        for fut in as_completed(futures):
            point = futures[fut]
            try:
                _, n = fut.result()
                done += 1
                total_bytes += n
            except Exception as err:                    # one point, not the run
                failed.append((point["id"], str(err).split("\n")[0][:120]))
                continue
            if done % 10 == 0 or done == len(todo):
                rate = done / max(time.time() - t0, 1e-9)
                print(f"    {done}/{len(todo)}  {total_bytes / 1e6:.1f} MB  "
                      f"{rate * 60:.0f}/min", flush=True)

    for pid, why in failed:
        print(f"    FAILED {pid}: {why}")
    if failed:
        print(f"  {len(failed)} point(s) failed -- re-run to retry just those")

    on_disk = sorted(root.glob(f"*.{fmt}"))
    meta = {
        "version": CHIP_BAKE_VERSION,
        "dir": f"{batch['batch_id']}_chips",
        "years": list(years),
        "cell": cell,
        "width_m": width_m,
        "cap": SCENE_CAP,
        "format": fmt,
        # A list, so a second scheme can be baked alongside without
        # invalidating the first.
        "combos": sorted(set((batch.get("chips") or {}).get("combos", []))
                         | {combo}),
        # Per point, per band. The app mixes its pre-image tint through these
        # too, so tint and sprite are one picture rather than two.
        "stretch": stretch,
        "stretch_pct": list(STRETCH_PCT),
        "stretch_width_m": width_m,
        # CACHE BUSTER, and it is not optional. A re-bake writes NEW pixels to
        # the SAME path, so every browser and CDN that already fetched a sprite
        # keeps serving the old one -- which is invisible from a fresh profile
        # and looks exactly like the re-bake not having worked. The app appends
        # this to the sprite URL.
        "built": int(time.time()),
    }
    size = sum(f.stat().st_size for f in on_disk)
    print(f"  {len(on_disk)} sprites, {size / 1e6:.2f} MB total "
          f"({size / max(len(on_disk), 1) / 1024:.0f} KB each)")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", type=Path, required=True)
    # Repeatable. Measured at 24 KB median per point per scheme, so all four
    # are ~10 MB for a 100-point batch -- the "default scheme only" call was
    # made on a ~40 KB estimate, and the reason it mattered is that switching
    # scheme on an unbaked one drops to live Earth Engine, or to nothing at all
    # when nobody is signed in.
    parser.add_argument("--combo", default=["SWIR1/NIR/GREEN"], nargs="+",
                        choices=sorted(COMBOS))
    parser.add_argument("--width", type=float, default=640.0,
                        help="chip footprint in metres; must match the width "
                             "the app is set to or it falls back to live EE")
    parser.add_argument("--cell", type=int, default=CELL)
    parser.add_argument("--format", default="webp", choices=("webp", "jpg"))
    parser.add_argument("--quality", type=int, default=80)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--stretch", default="point",
                        choices=("point", "fixed"),
                        help="'point' derives the display ramp from each "
                             "point's own nine years (default); 'fixed' is the "
                             "old global ramp, which saturates a quarter of a "
                             "global draw")
    parser.add_argument("--force", action="store_true",
                        help="re-bake points that already have a sprite")
    parser.add_argument("--force-stretch", action="store_true",
                        help="re-measure the per-point ramp as well; without "
                             "this a --force re-bake re-uses the cached one, "
                             "which is what makes a second scheme cheap")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be baked; no Earth Engine")
    args = parser.parse_args()

    batch = json.loads(args.batch.read_text())
    # Read the version ONCE, off the batch as it was on disk. Every scheme in
    # this run is stale or none of them are -- see the note in `bake`.
    stale = (batch.get("chips") or {}).get("version") != CHIP_BAKE_VERSION
    meta = None
    for i, combo in enumerate(args.combo):
        # Sequential, and the batch is carried between iterations so the second
        # scheme reads the first's cached ramp instead of re-measuring it.
        meta = bake(batch, args.batch, combo=combo, width_m=args.width,
                    cell=args.cell, fmt=args.format, quality=args.quality,
                    workers=args.workers, force=args.force,
                    dry_run=args.dry_run, stretch_mode=args.stretch,
                    force_stretch=args.force_stretch and i == 0,
                    stale=stale)
        if meta:
            batch["chips"] = meta
    if not meta:
        return
    # Write then rename so interruption cannot leave a truncated batch file.
    tmp = args.batch.with_suffix(".json.part")
    tmp.write_text(json.dumps(batch, indent=1))
    os.replace(tmp, args.batch)
    print(f"  wrote {args.batch}  (chips.combos = {meta['combos']})")


if __name__ == "__main__":
    main()

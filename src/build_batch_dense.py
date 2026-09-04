#!/usr/bin/env python
"""Bake a batch's dense Sentinel-2 series to static files, once, before anybody
labels.

WHY THIS EXISTS
---------------
It is `build_batch_chips.py` for the numbers instead of the pictures.

The dense series is every clear-sky observation at the point, all seasons --
not one composite a year -- and §AL9 put it in the app because it is the best
instrument this app can carry for the Cropland / Nature boundary the ledger says
caps change-F1. A cropped field is bare, then green, then cut; rough grazing is
flat. One composite a year cannot see that by construction.

It was live-and-on-demand, behind a button, on the grounds that it "cannot be
baked -- nine years of Sentinel-2 at a point is a few hundred rows, and a
hundred points of that is a batch file nobody can download". That is true of
the batch JSON, which has to stay near a megabyte, and it stopped being the
question the moment the chip bake established the SIDECAR: ~300 rows is ~10 KB
per point, and a hundred of those is one directory nobody has to download at
once.

Three things follow from baking it:

* It is **on by default**. A series the interpreter has to ask for is a series
  they ask for *after* they have already made the call, which is the wrong way
  round for the one instrument that separates the two classes it exists for.
* It works with **Earth Engine never signed in**, like the rest of the evidence.
* Nobody pays the 10-30 s. The live path is unchanged underneath, and a missing
  or malformed sidecar falls straight back to it.

THE RECIPE MUST NOT DRIFT
-------------------------
`series_for` below is `denseFetchLive()` in label_app.html, in Python. Same
collection, same pre-filter, same mask, same cell, same scale. Two recipes for
one line is the same hazard as `growingSeason()` one level up, and here it would
be worse: the baked series and the live fallback would disagree for the half of
the batch that has a sidecar.

THE FOOTPRINT IS THE LABELLING CELL
-----------------------------------
This series read a 30 m *radius* circle at 20 m until 2026-08-31 -- roughly 28x
the area of the thing being labelled. The call the interpreter makes is majority
cover of the 10 m cell (see the brief in label_app.html), so a chart describing a
60 m neighbourhood was answering a different question from the buttons: a hedge,
a track or a field margin outside the cell moved the line that was supposed to
justify the call. Worse, it was invisible -- two interpreters disagreeing because
one weighted the surroundings is indistinguishable from two interpreters
disagreeing about the legend, and the agreement number is what this campaign is
bought on.

The cell is the Sentinel-2 PIXEL the point falls in (`src/label_cell.py`), and
the read below is that pixel exactly: `reduceRegion` over the point at scale 10
returns the value of the pixel containing it, in the granule's own grid. The
square the app draws is the same pixel, snapped in UTM. Until 2026-08-31 this
was a 10 m square *centred on the point*, which straddles four pixels and is
none of them -- so the chart mixed up to four pixels' reflectance to justify a
call on one of them.

Deliberately NOT the s2cloudless join the composites use: it made a first
attempt take 103 s for 269 scenes, because the join materialises a pair per
scene. `MSK_CLDPRB` is the same s2cloudless product already carried as a band.

    G=python   # whichever interpreter has the deps

    $G src/build_batch_dense.py --batch app/batches/b001.json

    # what it would do, without touching Earth Engine
    $G src/build_batch_dense.py --batch app/batches/b001.json --dry-run

Re-runs are resumable: a point whose sidecar is on disk **and was baked by the
current recipe** is skipped. A `DENSE_BAKE_VERSION` bump re-bakes the whole
directory on its own -- the app refuses to serve a sidecar it does not know the
version of, so skipping them would strand the batch on the live path. `--force`
re-bakes regardless.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

#: Bumped when the recipe changes. MUST MATCH `DENSE_BAKE_VERSION` in
#: label_app.html; an unknown version falls back to live Earth Engine.
#: `dense3` is the Sentinel-2 pixel; `dense2` was a 10 m square centred on the
#: point (four pixels, none of them the cell) and `dense1` a 30 m circle. The
#: bump is what stops a stale sidecar being served against the new brief -- the
#: app falls back to live Earth Engine on a version it does not know.
DENSE_BAKE_VERSION = "dense3"

#: MUST MATCH `denseFetchLive`. `CELL_M` is the edge of the labelling cell and
#: is here for the record: the read is `reduceRegion` over the POINT at
#: `SCALE_M`, which is that cell without a geometry to get wrong.
CLOUDY_MAX = 85
CLDPRB_MAX = 40
CELL_M = 10
SCALE_M = 10
BANDS = ("B4", "B8", "B11", "B12")


def _ee():
    import ee
    try:
        ee.Number(1).getInfo()
    except Exception:
        ee.Initialize()
    return ee


def series_for(ee, point: dict, years: list[int]) -> dict:
    """`denseFetchLive()` in Python. One request per point."""
    pt = ee.Geometry.Point([float(point["lon"]), float(point["lat"])])
    col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
           .filterDate(ee.Date.fromYMD(years[0], 1, 1),
                       ee.Date.fromYMD(years[-1] + 1, 1, 1))
           .filterBounds(pt)
           .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", CLOUDY_MAX)))

    def mask(im):
        scl = im.select("SCL")
        bad = scl.eq(3).Or(scl.gte(8).And(scl.lte(11)))
        return (im.updateMask(im.select("MSK_CLDPRB").lt(CLDPRB_MAX)
                              .And(bad.Not()))
                .select(list(BANDS)))

    fc = ee.FeatureCollection(col.map(mask).map(
        lambda im: ee.Feature(None, ee.Image(im)
                              .reduceRegion(ee.Reducer.mean(), pt, SCALE_M)
                              .set("t", ee.Image(im).date().millis()))
    )).filter(ee.Filter.notNull(["B8"]))

    res = fc.reduceColumns(ee.Reducer.toList(5),
                           ["t", "B4", "B8", "B11", "B12"]).getInfo()
    rows = sorted((r for r in (res or {}).get("list", [])
                   if all(v is not None for v in r)), key=lambda r: r[0])

    def nd(a, b):
        return None if a + b == 0 else round((a - b) / (a + b), 4)

    return {
        "t":    [int(r[0]) for r in rows],
        "ndvi": [nd(r[2], r[1]) for r in rows],
        "ndmi": [nd(r[2], r[3]) for r in rows],
        "nbr":  [nd(r[2], r[4]) for r in rows],
    }


def bake_one(ee, point: dict, years: list[int], out: Path) -> tuple[str, int]:
    data = series_for(ee, point, years)
    # Write-then-rename, so an interrupted bake never leaves a half file that
    # the resume logic would then skip.
    tmp = out.with_suffix(out.suffix + ".part")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, out)
    return point["id"], len(data["t"])


def bake(batch: dict, batch_path: Path, *, workers: int, force: bool,
         dry_run: bool) -> dict | None:
    schema = batch.get("evidence_schema") or {}
    years = ((schema.get("timeline") or {}).get("years")) or []
    if not years:
        raise SystemExit(
            "this batch declares no timeline years -- run build_batch_evidence.py "
            "first. The dense series spans the same years the chart plots.")

    root = batch_path.parent / f"{batch['batch_id']}_dense"
    points = batch["points"]
    # A sidecar on disk is only a skip if it was baked by THIS recipe. The
    # batch's own `dense` block records which one, and a version bump means
    # every file in the directory is answering the old brief -- which the app
    # will refuse to serve, so a resumable re-run that skipped them all would
    # leave the batch permanently on the live path and say "100 already there".
    stale = (batch.get("dense") or {}).get("version") != DENSE_BAKE_VERSION
    todo = [p for p in points
            if force or stale or not (root / f"{p['id']}.json").exists()]

    print(f"{batch_path}")
    print(f"  {len(points)} points · {years[0]}-{years[-1]} · every clear "
          f"observation in the labelled pixel ({CELL_M} m)")
    if stale and not force:
        print(f"  bake version is now {DENSE_BAKE_VERSION} (this batch says "
              f"{(batch.get('dense') or {}).get('version')!r}) -- re-baking all")
    print(f"  -> {root}  ({len(todo)} to bake, "
          f"{len(points) - len(todo)} already there)")
    if dry_run:
        print("  --dry-run: nothing fetched")
        return None

    root.mkdir(parents=True, exist_ok=True)
    ee = _ee()
    done, failed, obs = 0, [], 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(bake_one, ee, p, years, root / f"{p['id']}.json"): p
                   for p in todo}
        for fut in as_completed(futures):
            point = futures[fut]
            try:
                _, n = fut.result()
            except Exception as err:                     # one point, not the run
                failed.append((point["id"], str(err).split("\n")[0][:120]))
                continue
            done += 1
            obs += n
            if done % 10 == 0 or done == len(todo):
                print(f"    {done}/{len(todo)}  {obs} observations  "
                      f"{done / max(time.time() - t0, 1e-9) * 60:.0f}/min",
                      flush=True)

    for pid, why in failed:
        print(f"    FAILED {pid}: {why}")
    if failed:
        print(f"  {len(failed)} point(s) failed -- re-run to retry just those")

    on_disk = sorted(root.glob("*.json"))
    size = sum(f.stat().st_size for f in on_disk)
    print(f"  {len(on_disk)} sidecars, {size / 1e6:.2f} MB total "
          f"({size / max(len(on_disk), 1) / 1024:.0f} KB each)")
    return {
        "version": DENSE_BAKE_VERSION,
        "dir": f"{batch['batch_id']}_dense",
        "years": list(years),
        "cell_m": CELL_M,
        "scale_m": SCALE_M,
        "cloudy_max": CLOUDY_MAX,
        "cldprb_max": CLDPRB_MAX,
        # Cache buster: a re-bake writes new numbers to the same path. See the
        # note on `built` in build_batch_chips.py.
        "built": int(time.time()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--force", action="store_true",
                        help="re-bake points that already have a sidecar")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be baked; no Earth Engine")
    args = parser.parse_args()

    batch = json.loads(args.batch.read_text())
    meta = bake(batch, args.batch, workers=args.workers, force=args.force,
                dry_run=args.dry_run)
    if not meta:
        return
    batch["dense"] = meta
    # Write-then-rename. /data is CIFS, and this project has already lost a path
    # permanently to an interrupted in-place rewrite.
    tmp = args.batch.with_suffix(".json.part")
    tmp.write_text(json.dumps(batch, indent=1))
    os.replace(tmp, args.batch)
    print(f"  wrote {args.batch}  (dense.version = {meta['version']})")


if __name__ == "__main__":
    main()

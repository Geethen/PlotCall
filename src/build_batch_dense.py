#!/usr/bin/env python
"""Bake dense Sentinel-2 index series for a PlotCall batch.

The dense series contains clear-sky observations throughout each year rather
than one annual composite. It can therefore show within-year variation that an
annual summary cannot represent. Each point is written as a small sidecar file,
so the batch JSON remains compact and the browser loads only the active point.

``series_for`` must match ``denseFetchLive()`` in ``label_app.html``: collection,
filters, mask, footprint and scale are one scientific recipe with a baked and a
live execution path. The footprint is the labelled Sentinel-2 pixel described
by ``src/label_cell.py``, not a neighbourhood around the point.

Usage
-----
    python src/build_batch_dense.py --batch app/batches/b001.json
    python src/build_batch_dense.py --batch app/batches/b001.json --dry-run

Runs are resumable. Existing sidecars made with the current
``DENSE_BAKE_VERSION`` are skipped unless ``--force`` is used.
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
    # Write then rename so interruption cannot leave a truncated batch file.
    tmp = args.batch.with_suffix(".json.part")
    tmp.write_text(json.dumps(batch, indent=1))
    os.replace(tmp, args.batch)
    print(f"  wrote {args.batch}  (dense.version = {meta['version']})")


if __name__ == "__main__":
    main()

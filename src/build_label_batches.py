"""Cut a ranked candidate table into labelling batches for ``app/label_app.html``.

Why batches, and why small ones
-------------------------------
This is not a packaging convenience. ``ACTIVE_LEARNING.md`` §AL4 measured the
**same 2,000 acquisitions** at **-0.003** change-F1 delivered as one batch and
**+0.031** delivered as twenty, against a paired floor of 0.016. A one-shot
campaign throws the model-in-the-loop half of the design away entirely -- and if
the labelling workflow genuinely cannot return batches, §AL4's own conclusion is
to delete that half rather than run it once.

§AL5 then separated batch *size* from the *schedule*: a hand-built
explore-then-exploit schedule tied random, while simply cutting the same budget
into more batches did not. §AL6 separated batch granularity from *start size* and
found the start size is what matters. So the default here is 100 -- small enough
that the model is refitted often, large enough to be a sitting for an
interpreter -- and there is deliberately no schedule parameter.

What this script does NOT decide
--------------------------------
The *ranking*. Candidates arrive already scored, from whichever channel produced
them, and this script preserves that order: batch 1 is ranks 1-100. The two
channels are kept in **separate batches**, not interleaved, because they are two
objectives with two different instruments and are priced on different metrics:

    change-F1                 -> more labels; `entropy` in many small batches
    the map's stable errors   -> coverage: `novelty` / `kcenter`, terrain-strat.
    plots of Artificial -> Cropland -> the retrieval channel, kept small

Mixing them into one batch makes the campaign unreadable: you cannot attribute a
movement in `natStab_as_art` to the coverage points if the batch also carried
retrieval points.

Usage
-----
    G=python   # whichever interpreter has the deps

    # a runnable demo batch from the 100-patch equal-area pilot draw
    $G src/build_label_batches.py --placeholder

    # real candidates, already ranked, 100 per batch
    $G src/build_label_batches.py \
        --candidates data/analysis_results/coverage_candidates.csv \
        --channel coverage --batch-size 100 --prefix cov

    # drop points already labelled in an earlier round before cutting
    $G src/build_label_batches.py --candidates ... \
        --exclude-labelled data/analysis_results/round1_labels.csv

    # a calibration batch from plots that already have an agreed answer, which
    # every interpreter works before their first real batch
    $G src/build_label_batches.py --candidates <table-with-transitions> \
        --calibration --reference-col transition --prefix cal

Writes ``app/batches/<prefix>NNN.json`` plus ``app/batches/index.json``, which is
the manifest the app's batch dropdown reads.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import label_cell
from project_paths import REPO_ROOT, project_data_dir

BATCH_DIR = REPO_ROOT / "app" / "batches"


def show(path: Path) -> str:
    """Repo-relative when it can be, absolute otherwise.

    ``--outdir`` is allowed to point anywhere -- a shared drive, a web root --
    and ``relative_to`` raises rather than falling back, which turned a progress
    line into a crash after the files were already written.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


#: Columns the app understands directly. Everything else on a candidate row is
#: passed through as ``meta`` and rendered in the "about this location" table --
#: which is where terrain stratum, biome and WorldCover class belong, because
#: §AL-T's coverage gap is the reason those points are in the batch. (``rank``
#: and ``score`` are NOT in that table: the app hides them until the point is
#: saved, because "rank 1, uncertainty" tells the interpreter the model finds
#: this point hard before they have looked at it.)
FIRST_CLASS = ("id", "lon", "lat", "channel", "rank", "score", "cell_km",
               "reference", "primary_expert", "required_readers")

#: Fraction of every batch that gets a deliberate second reading. §AL asks for
#: ~5%: inter-rater agreement is the campaign's only measurement of the label
#: noise the ledger says caps change-F1, and it is computed from exactly these
#: points. It is a property of the BATCH FILE, written here, because when it was
#: a checkbox in the app somebody forgetting it in one direction produced
#: duplicated work and forgetting it in the other produced zero overlap -- and
#: neither is visible until the round report.
DEFAULT_DOUBLE_FRAC = 0.05

#: The three acquisition channels the ledger recognises, and what each is bought
#: on. Used only to validate ``--channel`` and to stamp the batch.
#: What each calibration stage is for, in the words the interpreter reads when
#: the batch opens. Two stages, because one mixed set neither teaches reliably
#: nor measures anything: being told the answer is what makes the legend stick,
#: and being told the answer is also what makes the score meaningless.
CALIBRATION_NOTE = {
    "teach": (
        "Calibration, TEACHING stage. These points already have an agreed "
        "answer and you are told it after every call. Work them exactly as you "
        "would a real batch. The purpose is to line everyone up on the legend "
        "-- especially the Cropland / Nature boundary, which is where readers "
        "disagree and which the ledger says caps the model. Do the "
        "qualification set afterwards."
    ),
    "qualify": (
        "Calibration, QUALIFICATION stage. These points have an agreed answer "
        "and you will not be shown it until the end. Read the pattern in the "
        "report rather than the percentage: one interpreter consistently "
        "calling long fallow Cropland is a briefing that can be fixed in ten "
        "minutes, and it looks nothing like the same headline number made of "
        "scattered singletons."
    ),
}

CHANNELS = {
    "coverage": "novelty / k-center, stratified -- buys map quality in the "
                "stable classes, NOT change-F1",
    "uncertainty": "entropy / BALD -- buys change-F1, and only in many small "
                   "batches from a small start",
    "retrieval": "a similarity channel for one rare class -- bought on "
                 "confirmed plots per patch, kept separate and small",
    "random": "the equal-area baseline every other channel has to beat",
}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_candidates(path: Path) -> pd.DataFrame:
    """Read a candidate table from parquet, csv or GeoJSON."""
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        frame = pd.read_parquet(path)
    elif suffix in (".csv", ".tsv"):
        frame = pd.read_csv(path, sep="\t" if suffix == ".tsv" else ",")
    elif suffix in (".json", ".geojson"):
        obj = json.loads(path.read_text())
        if isinstance(obj, dict) and obj.get("type") == "FeatureCollection":
            rows = []
            for feature in obj["features"]:
                row = dict(feature.get("properties") or {})
                lon, lat = feature["geometry"]["coordinates"][:2]
                row["lon"], row["lat"] = lon, lat
                rows.append(row)
            frame = pd.DataFrame(rows)
        else:
            frame = pd.DataFrame(obj if isinstance(obj, list) else obj["points"])
    else:
        raise ValueError(f"unsupported candidate file type: {path.suffix}")
    return normalise(frame)


def normalise(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename the usual aliases onto ``lon``/``lat``/``id`` and check they exist."""
    aliases = {"longitude": "lon", "x": "lon", "latitude": "lat", "y": "lat",
               "point_id": "id", "plot_id": "id", "cell_id": "id",
               "patch_id": "id", "PLOTID": "id"}
    frame = frame.rename(columns={k: v for k, v in aliases.items()
                                  if k in frame.columns and v not in frame.columns})
    missing = {"lon", "lat"} - set(frame.columns)
    if missing:
        raise ValueError(f"candidates need lon/lat; missing {sorted(missing)}")
    frame = frame.loc[frame["lon"].notna() & frame["lat"].notna()].copy()
    # pandas 3 hands parquet floats back as nullable extension dtypes; json.dumps
    # cannot serialise those, and neither can the app. Cast early.
    for column in ("lon", "lat", "score"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    return frame


def placeholder_frame() -> pd.DataFrame:
    """The bundled demo draw, so the app is runnable before you have candidates.

    ``demo/demo_points.csv`` is a spread of real locations with no scores and no
    known answers -- enough to see the interface, never a substitute for a
    ranked candidate table.
    """
    path = REPO_ROOT / "demo" / "demo_points.csv"
    frame = normalise(pd.read_csv(path, dtype={"id": str}))
    frame["channel"] = "random"
    frame["score"] = float("nan")
    frame["cell_km"] = 5.0
    frame["note"] = "bundled demo draw -- not an acquisition"
    return frame


# ---------------------------------------------------------------------------
# cutting
# ---------------------------------------------------------------------------
def exclude_labelled(frame: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Drop candidates whose id already appears in an earlier round's labels."""
    prior = pd.read_csv(path) if path.suffix.lower() == ".csv" \
        else pd.read_parquet(path)
    key = next((c for c in ("point_id", "id", "plot_id") if c in prior.columns), None)
    if key is None:
        raise ValueError(f"{path} has no point_id/id column to exclude on")
    done = set(prior[key].astype(str))
    before = len(frame)
    frame = frame.loc[~frame["id"].astype(str).isin(done)].copy()
    print(f"  excluded {before - len(frame)} already-labelled of {before}")
    return frame


def assign(points: list[dict], experts: list[str], double_frac: float,
           seed: int = 0) -> None:
    """Give every point a ``primary_expert`` and its ``required_readers``.

    Round-robin over the ranked order rather than by blocks, so no expert gets
    the whole top of an acquisition surface -- the two channels are priced on
    different metrics and an expert who only ever sees high-uncertainty points
    calibrates to a different distribution than one who does not.

    The overlap sample is drawn deterministically from a seeded permutation, so
    rebuilding the same batch produces the same assignments. A round-trip that
    silently re-drew the overlap would make the agreement number a moving
    target.
    """
    if not experts:
        return
    import random
    order = list(range(len(points)))
    rng = random.Random(seed)
    doubled = set(rng.sample(order, k=min(len(order),
                                          round(double_frac * len(order)))))
    for i, point in enumerate(points):
        primary = experts[i % len(experts)]
        readers = [primary]
        if i in doubled and len(experts) > 1:
            # The second reader is the NEXT expert round, so the overlap is
            # spread over every pair rather than concentrated on one.
            readers.append(experts[(i + 1) % len(experts)])
        point["primary_expert"] = primary
        point["required_readers"] = readers


def assignment_counts(points: list[dict]) -> dict[str, int]:
    """Points each expert owes a reading on, for the manifest.

    The app reads this to answer "resume my assigned batch" without downloading
    every batch file in the campaign.
    """
    counts: dict[str, int] = {}
    for point in points:
        for expert in point.get("required_readers") or []:
            counts[expert] = counts.get(expert, 0) + 1
    return counts


def to_points(frame: pd.DataFrame, channel: str | None) -> list[dict]:
    """One app-shaped point per row, with unknown columns folded into ``meta``.

    ``reference`` is deliberately first-class rather than metadata: ``meta`` is
    rendered next to the buttons, and a calibration answer shown before the call
    measures nothing.
    """
    extra = [c for c in frame.columns if c not in FIRST_CLASS]
    points = []
    for rank, (_, row) in enumerate(frame.iterrows(), start=1):
        meta = {c: row[c] for c in extra
                if pd.notna(row[c]) and str(row[c]) != ""}
        point = {
            "id": str(row["id"]),
            "lon": float(row["lon"]),
            "lat": float(row["lat"]),
            "channel": str(row["channel"]) if "channel" in frame.columns
                       and pd.notna(row.get("channel")) else channel,
            "rank": int(row["rank"]) if "rank" in frame.columns
                    and pd.notna(row.get("rank")) else rank,
            "cell_km": float(row["cell_km"]) if "cell_km" in frame.columns
                       and pd.notna(row.get("cell_km")) else 5.0,
            "meta": {k: (float(v) if isinstance(v, (int, float)) else str(v))
                     for k, v in meta.items()},
            # THE UNIT BEING LABELLED, baked so the app draws the same square
            # the builders reduce over. `label_cell.utm_epsg` and not the draw's
            # own `meta.epsg`: the app computes this cell for a file dropped on
            # the window, and two rules for one grid is the drift this bake is
            # here to remove.
            "cell": cell_for(float(row["lon"]), float(row["lat"])),
        }
        if "score" in frame.columns and pd.notna(row.get("score")):
            point["score"] = float(row["score"])
        if "reference" in frame.columns and pd.notna(row.get("reference")):
            point["reference"] = str(row["reference"])
        for column in ("primary_expert", "required_readers"):
            if column in frame.columns and pd.notna(row.get(column)):
                value = row[column]
                point[column] = ([s.strip() for s in str(value).split("|")]
                                 if column == "required_readers" else str(value))
        points.append(point)
    return points


def cell_for(lon: float, lat: float) -> dict:
    """The Sentinel-2 pixel this point addresses, as the app will draw it.

    Trimmed to the ring and the zone: the app needs the polygon, and `x0`/`y0`
    are recoverable from either. Read `src/label_cell.py` for why the labelling
    unit is a pixel of the imagery grid and not a square around the point.
    """
    c = label_cell.cell(lon, lat)
    return {"epsg": c["epsg"], "ring": c["ring"]}


def cut(frame: pd.DataFrame, size: int) -> list[pd.DataFrame]:
    """Consecutive slices of the ranked table -- batch 1 is ranks 1..size.

    Consecutive, not shuffled: the rank order *is* the draw order, and cutting it
    any other way discards the ordering the acquisition surface was built to
    produce.
    """
    return [frame.iloc[i:i + size] for i in range(0, len(frame), size)]


def write_batches(frame: pd.DataFrame, *, campaign: str, channel: str | None,
                  size: int, prefix: str, outdir: Path,
                  instructions: str | None, calibration: bool = False,
                  feedback: str = "immediate", stage: str | None = None,
                  experts: list[str] | None = None,
                  double_frac: float = DEFAULT_DOUBLE_FRAC,
                  evidence: bool = False) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = []
    for index, chunk in enumerate(cut(frame, size), start=1):
        batch_id = f"{prefix}{index:03d}"
        points = to_points(chunk, channel)
        # Only assign what is not already assigned: a candidate table may carry
        # its own primary_expert (a re-cut of an earlier round, say), and
        # re-drawing over it would move the overlap sample.
        if experts and not any(p.get("primary_expert") for p in points):
            assign(points, experts, double_frac, seed=index)
        payload = {
            "campaign": campaign,
            "batch_id": batch_id,
            "channel": channel,
            "created": now,
            "instructions": instructions,
            "calibration": calibration,
            "feedback": feedback,
            "stage": stage,
            "experts": experts or None,
            "points": points,
        }
        if evidence:
            from build_batch_evidence import add_evidence, size_note
            print(f"  {batch_id}: baking evidence")
            add_evidence(payload)
            print(size_note(payload))
        path = outdir / f"{batch_id}.json"
        path.write_text(json.dumps(payload, indent=1))
        assigned = assignment_counts(points)
        manifest.append({"batch_id": batch_id, "file": path.name,
                         "n": len(points), "channel": channel,
                         "calibration": calibration, "stage": stage,
                         "created": now,
                         # Per expert, so the app can answer "resume my assigned
                         # batch" without downloading every batch in the
                         # campaign. `assigned_to` stays for a human reading the
                         # manifest.
                         "assigned": assigned,
                         "assigned_to": ", ".join(sorted(assigned))})
        extra = (f", {sum(1 for p in points if len(p.get('required_readers') or []) > 1)}"
                 " double-read" if experts else "")
        print(f"  {show(path)}  {len(points)} points{extra}")
    return manifest


def write_manifest(entries: list[dict], outdir: Path, *, merge: bool) -> None:
    """Write ``index.json``, keeping batches from earlier runs unless told not to.

    Merging is the default because a campaign accumulates rounds, and a labeller
    part-way through an older batch must not lose the entry that points at it.
    """
    path = outdir / "index.json"
    existing: list[dict] = []
    if merge and path.exists():
        try:
            existing = json.loads(path.read_text()).get("batches", [])
        except (json.JSONDecodeError, AttributeError):
            existing = []
    new_ids = {e["batch_id"] for e in entries}
    kept = [e for e in existing if e.get("batch_id") not in new_ids]
    path.write_text(json.dumps({"batches": kept + entries}, indent=1))
    print(f"  {show(path)}  {len(kept) + len(entries)} batches")


# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidates", type=Path,
                        help="ranked candidate table (.parquet/.csv/.geojson)")
    parser.add_argument("--placeholder", action="store_true",
                        help="build a demo batch from the equal-area pilot draw")
    parser.add_argument("--channel", choices=sorted(CHANNELS),
                        help="which acquisition channel produced these; stamped "
                             "on every point so the rounds stay attributable")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="points per batch (default 100; AL4/AL6 -- small "
                             "and sequential is the measured setting). Applies "
                             "to calibration batches too: one size everywhere, "
                             "so a batch is always a batch.")
    parser.add_argument("--max-points", type=int, default=None,
                        help="take only the top N candidates before cutting")
    parser.add_argument("--prefix", default="b", help="batch id prefix")
    parser.add_argument("--campaign", default="plotcall",
                        help="must match `campaign` in app/config.js -- it is "
                             "the first field of the annotation key")
    parser.add_argument("--exclude-labelled", type=Path,
                        help="labels from an earlier round; their ids are dropped")
    parser.add_argument("--instructions", default=None,
                        help="shown once when the batch opens")
    parser.add_argument("--calibration", action="store_true",
                        help="build a calibration batch: every point carries a "
                             "known answer and the app scores the interpreter "
                             "against it. Needs --reference-col.")
    parser.add_argument("--reference-col", default="transition",
                        help="column holding the known transition "
                             "(default 'transition')")
    parser.add_argument("--feedback", choices=("immediate", "end"),
                        default=None,
                        help="immediate teaches (told after each call); end "
                             "assesses (told once, at the end). Defaults from "
                             "--stage.")
    parser.add_argument("--stage", choices=("teach", "qualify"), default=None,
                        help="which calibration stage this batch is. `teach` "
                             "gives immediate feedback and teaches the legend; "
                             "`qualify` is blind with end-only feedback and "
                             "assesses. BOTH precede any real batch, in that "
                             "order -- a single mixed set neither teaches "
                             "reliably nor measures anything.")
    parser.add_argument("--experts",
                        help="comma-separated expert ids from config.js, e.g. "
                             "e1,e2. Points are assigned round-robin and the "
                             "overlap sample is drawn here, so the queues in "
                             "the app are a property of the batch file and are "
                             "correct offline.")
    parser.add_argument("--double-frac", type=float,
                        default=DEFAULT_DOUBLE_FRAC,
                        help=f"fraction of each batch given a deliberate second "
                             f"reading (default {DEFAULT_DOUBLE_FRAC}). §AL asks "
                             "for ~5%%: this is the campaign's only measurement "
                             "of the label noise that caps change-F1.")
    parser.add_argument("--evidence", action="store_true",
                        help="bake the point values and the annual timeline "
                             "into every point (src/build_batch_evidence.py). "
                             "Needs Earth Engine; the app never does.")
    parser.add_argument("--outdir", type=Path, default=BATCH_DIR)
    parser.add_argument("--replace-manifest", action="store_true",
                        help="drop batches from earlier runs from index.json")
    args = parser.parse_args()

    if args.placeholder:
        frame = placeholder_frame()
        channel = args.channel or "random"
        instructions = args.instructions or (
            "Demo batch. These points are here so you can try the app; they "
            "are not an acquisition and the calls are not worth keeping."
        )
    elif args.candidates:
        frame = load_candidates(args.candidates)
        channel = args.channel
        instructions = args.instructions
    else:
        parser.error("pass --candidates or --placeholder")

    stage = args.stage
    # `qualify` is blind with end-only feedback and `teach` tells you after every
    # call. Wiring feedback to the stage rather than leaving them independent is
    # the point: a qualification set that teaches measures nothing.
    feedback = args.feedback or ("end" if stage == "qualify" else "immediate")
    if stage and not args.calibration:
        parser.error("--stage only means anything with --calibration")

    experts = ([e.strip() for e in args.experts.split(",") if e.strip()]
               if args.experts else None)
    if experts and len(experts) < 2 and args.double_frac > 0:
        print("  note: one expert, so nothing can be double-read. The "
              "agreement\n  number this campaign is bought on needs at least "
              "two.")

    if args.calibration:
        if args.reference_col not in frame.columns:
            parser.error(f"--calibration needs a '{args.reference_col}' column; "
                         f"the table has {sorted(frame.columns)[:12]}")
        frame = frame.rename(columns={args.reference_col: "reference"})
        frame = frame.loc[frame["reference"].notna()
                          & (frame["reference"].astype(str) != "")].copy()
        instructions = instructions or CALIBRATION_NOTE[stage or "teach"]

    if "id" not in frame.columns:
        frame = frame.reset_index(drop=True)
        frame["id"] = [f"{args.prefix}{i:06d}" for i in range(len(frame))]
    if args.exclude_labelled:
        frame = exclude_labelled(frame, args.exclude_labelled)
    if args.max_points:
        frame = frame.head(args.max_points)
    if frame.empty:
        raise SystemExit("no candidates left after filtering")

    if channel:
        print(f"channel {channel}: {CHANNELS[channel]}")
    print(f"{len(frame)} candidates -> batches of {args.batch_size}")
    entries = write_batches(frame, campaign=args.campaign, channel=channel,
                            size=args.batch_size, prefix=args.prefix,
                            outdir=args.outdir, instructions=instructions,
                            calibration=args.calibration,
                            feedback=feedback, stage=stage, experts=experts,
                            double_frac=args.double_frac,
                            evidence=args.evidence)
    write_manifest(entries, args.outdir, merge=not args.replace_manifest)

    if not args.evidence:
        # Say it out loud. A batch built without this looks completely normal in
        # the app -- the evidence panel and the chip filmstrip simply hide
        # themselves, with nothing anywhere to say the data was never baked.
        # That is a confusing half-hour for whoever opens it.
        print("\n  ! built WITHOUT evidence. The app's point-value table, annual"
              "\n    timeline, spectral profile and chip filmstrip will not"
              "\n    appear -- they render from data baked in here, not fetched"
              "\n    at label time. Re-run with --evidence (needs Earth Engine;"
              "\n    budget ~35 min per 100 points), or add it afterwards with"
              "\n    src/build_batch_evidence.py --batch <file>.")


if __name__ == "__main__":
    main()

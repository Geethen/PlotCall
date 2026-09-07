"""Pull a PlotCall round from a Sheet or CSV and report its outcomes.

The tool deduplicates corrections on the four-part annotation key, keeps
independent readers distinct, and reports completion, timing, calibration,
inter-rater agreement, and observed transition yield. Transition classes are
derived from the returned rows rather than from a particular study legend.

Use ``--binding`` only when the study has specified a transition of interest in
advance. Its yield is then reported relative to the ``random`` control channel;
the tool does not impose a pass/fail threshold.

Usage
-----
    python src/label_rounds.py --url https://script.google.com/.../exec
    python src/label_rounds.py --csv exports/*.csv
    python src/label_rounds.py --url ... --exclude-out prior_ids.csv
"""
from __future__ import annotations

import argparse
import io
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from project_paths import project_data_dir

DEFAULT_OUT = project_data_dir("analysis_results") / "label_rounds.csv"

#: The random arm used for an optional relative-yield comparison.
BASELINE_CHANNEL = "random"


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
def fetch_sheet(url: str) -> pd.DataFrame:
    """GET ``?action=export`` from the Apps Script web app and parse the CSV."""
    parts = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["action"] = "export"
    full = parts._replace(query=urllib.parse.urlencode(query)).geturl()
    with urllib.request.urlopen(full, timeout=60) as response:
        text = response.read().decode("utf-8")
    if not text.lstrip().lower().startswith("campaign"):
        # Apps Script answers a deployment problem with an HTML error page, and
        # pandas will happily parse that into a one-column frame of nonsense.
        raise SystemExit(
            "the sheet returned something that is not the export CSV -- check "
            "the deployment is a Web app with access 'Anyone'. First 200 chars:\n"
            + text[:200])
    return pd.read_csv(io.StringIO(text))


def read_files(paths: list[Path]) -> pd.DataFrame:
    frames = [pd.read_csv(p) for p in paths]
    if not frames:
        raise SystemExit("no input files matched")
    return pd.concat(frames, ignore_index=True)


def who_column(frame: pd.DataFrame) -> str:
    """`expert_id` where the sheet has it, `labeller` where it does not.

    Rows written before the expert roster landed carry only a typed name. They
    are still readable -- but a round that mixes the two is a round whose
    agreement number cannot be trusted, so say so once, loudly, rather than
    quietly grouping on whatever is there.
    """
    if "expert_id" not in frame.columns:
        print("  ! no expert_id column: these rows predate the roster, and "
              "grouping\n    falls back to the typed name. 'Ann', 'ann' and "
              "'Ann ' are three experts\n    to this report and the agreement "
              "number cannot be read.")
        return "labeller"
    blank = frame["expert_id"].isna() | (frame["expert_id"].astype(str) == "")
    if blank.any():
        print(f"  ! {int(blank.sum())} of {len(frame)} rows have no expert_id "
              "and are grouped by\n    display name instead. Mixed-vintage "
              "round; read the agreement number with care.")
        frame.loc[blank, "expert_id"] = frame.loc[blank, "labeller"]
    return "expert_id"


def dedupe(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep the newest call per (campaign, batch, point, expert).

    One person re-labelling a point is a correction and only the last one counts;
    two *different* people labelling it is the agreement measurement and both are
    kept. Collapsing on (batch, point) alone would silently delete the second
    reading and invalidate the agreement calculation. This is the same key the
    Apps Script uses for upserts.
    """
    if "labelled_at" in frame.columns:
        frame = frame.sort_values("labelled_at")
    who = who_column(frame)
    keys = [c for c in ("campaign", "batch_id", "point_id", who)
            if c in frame.columns]
    return frame.drop_duplicates(subset=keys, keep="last").reset_index(drop=True)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def split_calibration(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Calibration rows out of the campaign rows.

    A calibration point is an exercise against a reference interpretation, not
    a sampled campaign result. It is excluded from the yield table.
    """
    if "calibration" not in frame.columns:
        return frame, frame.iloc[0:0]
    flag = pd.to_numeric(frame["calibration"], errors="coerce").fillna(0)
    return frame.loc[flag != 1].copy(), frame.loc[flag == 1].copy()


def calibration_report(frame: pd.DataFrame) -> None:
    """Agreement with the reference, per interpreter and confusion pair."""
    good = usable(frame)
    good = good.loc[good["reference"].notna()
                    & (good["reference"].astype(str) != "")]
    if good.empty:
        print("\ncalibration: no rows. Every interpreter should work one "
              "calibration batch\n  before their first real batch -- "
              "build_label_batches.py --calibration.")
        return
    who = who_column(good)
    for stage in ("teach", "qualify", None):
        block = stage_rows(good, stage)
        if block.empty:
            continue
        label = {"teach": "teaching", "qualify": "qualification"}.get(
            stage, "unstaged")
        print(f"\ncalibration ({label}): {len(block)} rows")
        _calibration_block(block, who)
    if not (good.get("stage", pd.Series(dtype=str)).astype(str)
            .isin(["qualify"]).any()):
        print("  ! no qualification rows. The teaching set tells you the answer "
              "after every\n    call, which is what makes the legend stick and "
              "what makes the score\n    meaningless. Run "
              "build_label_batches.py --calibration --stage qualify too.")


def stage_rows(frame: pd.DataFrame, stage: str | None) -> pd.DataFrame:
    """Rows for one calibration stage; `None` means rows with no stage at all."""
    if "stage" not in frame.columns:
        return frame if stage is None else frame.iloc[0:0]
    values = frame["stage"].fillna("").astype(str)
    return frame.loc[values == (stage or "")]


def _calibration_block(good: pd.DataFrame, who: str) -> None:
    agree = good["transition"].astype(str) == good["reference"].astype(str)
    # Report confusion pairs because a percentage cannot distinguish a
    # systematic class-boundary issue from scattered disagreements.
    for expert, block in good.groupby(who):
        hit = (block["transition"].astype(str)
               == block["reference"].astype(str)).sum()
        name = ""
        if who == "expert_id" and "labeller" in block.columns:
            names = sorted(set(block["labeller"].dropna().astype(str)) - {""})
            name = f"  ({', '.join(names)})" if names else ""
        print(f"  {str(expert):<20} {hit:>3} / {len(block):<3} "
              f"({hit / len(block):.0%}){name}")
        misses = block.loc[block["transition"].astype(str)
                           != block["reference"].astype(str)]
        pairs = (misses.groupby([misses["reference"].astype(str),
                                 misses["transition"].astype(str)])
                 .size().sort_values(ascending=False))
        for (ref, said), count in pairs.head(5).items():
            print(f"      {ref:<26} -> {said:<26} {count:>3}")
    misses = good.loc[~agree]
    if misses.empty:
        print("  no disagreements")
        return
    pairs = (misses.groupby([misses["reference"].astype(str),
                             misses["transition"].astype(str)])
             .size().sort_values(ascending=False))
    print("  all experts, reference -> called, most common first:")
    for (ref, said), count in pairs.head(10).items():
        print(f"    {ref:<26} -> {said:<26} {count:>3}")


def uninterpretable_report(frame: pd.DataFrame) -> None:
    """Why points came back unusable, counted per cause.

    These rows never reach the training set, so a countable cause is the only
    thing they can still buy -- and each cause points somewhere different.
    `cloud` says draw elsewhere; `no imagery at one date` says the Wayback
    archive is thin there; `capture dates too far from targets` says the label
    *window* is the problem rather than the points.
    """
    if "uninterpretable_reason" not in frame.columns:
        return
    reasons = frame["uninterpretable_reason"].fillna("").astype(str)
    reasons = reasons.loc[reasons != ""]
    if reasons.empty:
        return
    # "other: <free text>" collapses to `other` for the count; the text is in
    # the sheet for whoever wants it.
    heads = reasons.str.split(":").str[0].str.strip()
    print(f"\nnot interpretable: {len(reasons)} rows")
    for reason, count in heads.value_counts().items():
        print(f"  {reason:<32} {count:>5}")


def change_year_report(frame: pd.DataFrame) -> None:
    """How much of the change carries a date, and when it landed."""
    if "change_year" not in frame.columns:
        return
    good = usable(frame)
    changed = good.loc[pd.to_numeric(good["is_change"], errors="coerce") == 1]
    if changed.empty:
        return
    years = changed["change_year"].fillna("").astype(str)
    dated = years.loc[(years != "") & (years != "unclear")]
    print(f"\nchange year: {len(dated)} of {len(changed)} change plots dated "
          f"({len(dated) / len(changed):.0%})")
    for year, count in dated.value_counts().sort_index().items():
        print(f"  {year:<10} {count:>5}")
    if "flags" in frame.columns:
        transient = good["flags"].fillna("").astype(str).str.contains(
            "transient_change").sum()
        if transient:
            print(f"  {transient} plots where change was seen but the two "
                  "endpoints match --\n  invisible to a two-endpoint label, and "
                  "the reason to record the year at all")


def usable(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows that carry a transition -- 'cannot interpret' is not a label."""
    return frame.loc[frame["transition"].notna()
                     & (frame["transition"].astype(str) != "")].copy()


def change_classes(frame: pd.DataFrame) -> tuple[str, ...]:
    """Return observed change transitions in stable order."""
    good = usable(frame)
    if good.empty or "is_change" not in good.columns:
        return ()
    changed = pd.to_numeric(good["is_change"], errors="coerce").fillna(0).eq(1)
    return tuple(sorted(good.loc[changed, "transition"].astype(str).unique()))


def yield_by_channel(frame: pd.DataFrame,
                     classes: tuple[str, ...] | None = None) -> pd.DataFrame:
    """Confirmed plots per labelled point, per class, per channel.

    Per *point*, not per row: the denominator has to include the points that came
    back uninterpretable, because those consumed interpreter time and returned
    nothing. Dividing by usable rows only would flatter every channel by exactly
    its own failure rate.
    """
    frame = frame.copy()
    frame["channel"] = frame["channel"].fillna("(unstamped)")
    attempted = frame.groupby("channel")["point_id"].nunique()
    good = usable(frame)
    classes = classes if classes is not None else change_classes(frame)
    counts = (good.groupby(["channel", "transition"])["point_id"].nunique()
              .unstack(fill_value=0))
    counts = counts.reindex(index=attempted.index, columns=classes, fill_value=0)
    rate = counts.div(attempted, axis=0)
    rate.insert(0, "points_attempted", attempted)
    return rate


def enrichment(rate: pd.DataFrame, cls: str) -> pd.Series | None:
    """Each channel's per-point yield on `cls` as a multiple of the equal-area arm."""
    if BASELINE_CHANNEL not in rate.index or cls not in rate.columns:
        return None
    base = rate.loc[BASELINE_CHANNEL, cls]
    if base <= 0:
        return None
    return rate[cls] / base


def agreement(frame: pd.DataFrame) -> tuple[int, float, pd.DataFrame]:
    """Agreement on points two or more EXPERTS labelled, and where it breaks.

    Counted on distinct experts rather than on rows: after `dedupe` there is one
    row per (batch, point, expert), so a row count would be the same thing --
    but only as long as dedupe holds. Say explicitly what is meant.

    "CANNOT INTERPRET" IS A CALL, AND DISAGREEING WITH IT IS A DISAGREEMENT.
    Running this on `usable()` would drop rows with no transition, so a point
    where one expert made a class call and the other said the imagery would not
    support a call would not be a disagreement, an agreement, or a doubled
    point: it would leave the denominator entirely. That
    is the single most informative pair in the set, because it is the one that
    says the two readers are not deriving the same result from the evidence.
    The transition is compared as `(not interpretable)` so
    it lands in the confusion listing beside the legend pairs.
    """
    frame = frame.copy()
    call = frame["transition"].astype("string").fillna("").str.strip()
    frame["_call"] = call.where(call != "", "(not interpretable)").astype(str)
    who = who_column(frame)
    grouped = frame.groupby(["batch_id", "point_id"])["_call"]
    repeats = grouped.nunique(dropna=False)
    multi = frame.groupby(["batch_id", "point_id"])[who].nunique()
    doubled = multi[multi > 1].index
    if not len(doubled):
        return 0, float("nan"), pd.DataFrame()
    agreed = (repeats.loc[doubled] == 1).sum()
    # Which pairs of calls disagree, so the legend boundary at fault is visible
    # rather than just the headline rate.
    rows = []
    indexed = frame.set_index(["batch_id", "point_id"])
    for key in doubled:
        calls = sorted(set(indexed.loc[[key], "_call"]))
        if len(calls) > 1:
            rows.append({"batch_id": key[0], "point_id": key[1],
                         "calls": " | ".join(calls)})
    return len(doubled), agreed / len(doubled), pd.DataFrame(rows)


def report(all_rows: pd.DataFrame, binding: str | None = None) -> None:
    frame, calibration = split_calibration(all_rows)
    if not calibration.empty:
        calibration_report(calibration)
    if frame.empty:
        print("\nno campaign rows -- this round is calibration only")
        return
    good = usable(frame)
    changes = change_classes(frame)
    report_classes = changes + ((binding,) if binding and binding not in changes
                                else ())
    print(f"\n{len(frame)} rows, {frame['point_id'].nunique()} distinct points, "
          f"{good['point_id'].nunique()} with a usable transition")

    flags = frame.get("flags", pd.Series(dtype=str)).fillna("").astype(str)
    n = max(len(frame), 1)
    for flag in ("uninterpretable", "unsure", "mixed", "imagery_gap",
                 "transient_change"):
        hits = flags.str.contains(flag).sum()
        if hits:
            print(f"  {flag:<16} {hits:>5}  ({hits / n:.1%})")

    if "seconds_on_point" in frame.columns:
        seconds = pd.to_numeric(frame["seconds_on_point"], errors="coerce").dropna()
        if len(seconds):
            # The app pauses this clock while the tab is hidden, so the median is
            # a fair cost estimate. The tail still is not -- a point left open
            # over a phone call sits in it -- so price the round on the median.
            print(f"\nseconds per point: median {seconds.median():.0f}, "
                  f"p90 {seconds.quantile(0.9):.0f}")
            print(f"  {len(frame)} returned calls represent "
                  f"~{len(frame) * seconds.median() / 3600:.1f} "
                  "interpreter-hours at the median")

    who = who_column(frame)
    print("\nby expert:")
    for expert, count in frame[who].value_counts().items():
        name = ""
        if who == "expert_id" and "labeller" in frame.columns:
            names = sorted(set(frame.loc[frame[who] == expert, "labeller"]
                               .dropna().astype(str)) - {""})
            name = f"  ({', '.join(names)})" if names else ""
        print(f"  {str(expert):<20} {count:>5}{name}")

    uninterpretable_report(frame)

    print("\nby batch:")
    per_batch = frame.groupby("batch_id").agg(
        n=("point_id", "nunique"),
        change=("is_change", lambda s: pd.to_numeric(s, errors="coerce").sum()))
    for batch, row in per_batch.iterrows():
        print(f"  {batch:<12} {int(row['n']):>5} points, "
              f"{int(row['change']):>4} change")

    print("\ntransitions:")
    for cls, count in good["transition"].value_counts().items():
        mark = "  <- change" if cls in changes else ""
        print(f"  {cls:<26} {count:>5}{mark}")

    print("\nobserved change transitions per point, by channel:")
    rate = yield_by_channel(frame, report_classes)
    if report_classes:
        display = rate[["points_attempted"] + list(report_classes)]
        print(display.round(4).to_string())
    else:
        print("  no change transitions returned")

    if binding is None:
        print("\nrelative yield: pass --binding 'Class A -> Class B' to compare "
              f"one pre-specified transition with the '{BASELINE_CHANNEL}' "
              "control")
    else:
        boost = enrichment(rate, binding)
        print(f"\nrelative yield for {binding}:")
        if boost is None:
            print(f"  unavailable -- the '{BASELINE_CHANNEL}' control has no "
                  "non-zero yield for this transition")
        else:
            for channel, value in boost.items():
                if channel != BASELINE_CHANNEL:
                    print(f"  {channel:<14} {value:>6.2f}x the "
                          f"'{BASELINE_CHANNEL}' control")

    change_year_report(frame)

    n_double, rate_agree, disagreements = agreement(frame)
    print(f"\ninter-rater agreement: {n_double} points read twice or more", end="")
    if n_double:
        print(f", {rate_agree:.1%} agreed")
        if len(disagreements):
            print("  disagreements:")
            for _, row in disagreements.head(20).iterrows():
                print(f"    {row['point_id']:<12} {row['calls']}")
    else:
        print("\n  Nothing to read. Assign part of each batch to at least two "
              "interpreters to estimate inter-rater agreement.")


# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="Apps Script web app /exec URL")
    parser.add_argument("--csv", nargs="*", type=Path, default=[],
                        help="exported label CSVs instead of (or as well as) --url")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"where to write the pooled rows (default {DEFAULT_OUT})")
    parser.add_argument("--exclude-out", type=Path,
                        help="write the labelled point ids here, for the next "
                             "round's build_label_batches.py --exclude-labelled")
    parser.add_argument("--campaign", help="keep only this campaign's rows")
    parser.add_argument("--binding",
                        help="pre-specified transition to compare with the "
                             f"'{BASELINE_CHANNEL}' control, e.g. "
                             "'Class A -> Class B'")
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args()

    frames = []
    if args.url:
        frames.append(fetch_sheet(args.url))
    if args.csv:
        frames.append(read_files(args.csv))
    if not frames:
        parser.error("pass --url or --csv")

    frame = dedupe(pd.concat(frames, ignore_index=True))
    if args.campaign:
        frame = frame.loc[frame["campaign"] == args.campaign].copy()
    if frame.empty:
        raise SystemExit("no rows after filtering")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.out, index=False)
    print(f"wrote {len(frame)} rows -> {args.out}")

    if args.exclude_out:
        ids = frame[["point_id"]].drop_duplicates()
        args.exclude_out.parent.mkdir(parents=True, exist_ok=True)
        ids.to_csv(args.exclude_out, index=False)
        print(f"wrote {len(ids)} ids -> {args.exclude_out}")

    if not args.no_report:
        report(frame, binding=args.binding)


if __name__ == "__main__":
    main()

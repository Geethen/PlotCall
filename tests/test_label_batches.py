"""Invariants of the labelling round-trip.

These lock down the properties the campaign design leans on rather than the
numbers: that the ranked order survives the cut into batches, that the manifest
accumulates across rounds instead of orphaning a half-finished batch, that
unknown candidate columns reach the interpreter as context, that an earlier
round's points can be excluded from the next one, and -- on the read-back side --
that a re-label by the same person is a correction while a second person's read
is kept, because that is the only measurement of label noise the campaign makes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from build_batch_evidence import growing_season, season_groups  # noqa: E402
from build_label_batches import (  # noqa: E402
    assign, assignment_counts, cut, exclude_labelled, normalise, to_points,
    write_batches, write_manifest)
from label_rounds import (  # noqa: E402
    BASELINE_CHANNEL, agreement, dedupe, enrichment, stage_rows, usable,
    who_column, yield_by_channel)


def candidates(n=10, channel="coverage"):
    return pd.DataFrame({
        "id": [f"c{i:03d}" for i in range(n)],
        "lon": [10.0 + i for i in range(n)],
        "lat": [60.0 - i * 0.1 for i in range(n)],
        "score": [1.0 - i / n for i in range(n)],
        "channel": [channel] * n,
        # not a first-class column: must survive as interpreter-facing context
        "worldcover": ["bare"] * n,
    })


# ---------------------------------------------------------------------------
# cutting
# ---------------------------------------------------------------------------
def test_cut_preserves_rank_order():
    """Batch 1 is ranks 1..size. The rank order *is* the draw order."""
    frame = candidates(10)
    batches = cut(frame, 4)
    assert [len(b) for b in batches] == [4, 4, 2]
    assert list(batches[0]["id"]) == ["c000", "c001", "c002", "c003"]
    assert list(batches[2]["id"]) == ["c008", "c009"]


def test_cut_covers_every_candidate_exactly_once():
    frame = candidates(37)
    seen = pd.concat(cut(frame, 10))
    assert list(seen["id"]) == list(frame["id"])


def test_rank_is_per_table_not_per_batch():
    """A point's rank must mean the same thing in batch 3 as in batch 1.

    `to_points` is handed one batch at a time, so a rank invented from the
    positional index would restart at 1 in every batch and the returned labels
    could not be ordered against the surface that drew them.
    """
    frame = candidates(10)
    frame["rank"] = range(1, 11)
    last = to_points(cut(frame, 4)[2], "coverage")
    assert [p["rank"] for p in last] == [9, 10]


def test_unknown_columns_reach_the_interpreter_as_meta():
    points = to_points(candidates(3), "coverage")
    assert points[0]["meta"]["worldcover"] == "bare"
    assert points[0]["channel"] == "coverage"
    assert points[0]["score"] == pytest.approx(1.0)
    # first-class fields are not duplicated into meta
    assert "lon" not in points[0]["meta"]


def test_points_are_json_serialisable():
    """pandas 3 hands parquet floats back as extension dtypes json cannot dump."""
    json.dumps(to_points(candidates(3), "coverage"))


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def test_manifest_merges_across_rounds(tmp_path):
    """A second round must not orphan a batch someone is part-way through."""
    first = write_batches(candidates(4), campaign="c", channel="coverage", size=2,
                          prefix="r1", outdir=tmp_path, instructions=None)
    write_manifest(first, tmp_path, merge=True)
    second = write_batches(candidates(2), campaign="c", channel="retrieval",
                           size=2, prefix="r2", outdir=tmp_path, instructions=None)
    write_manifest(second, tmp_path, merge=True)

    entries = json.loads((tmp_path / "index.json").read_text())["batches"]
    assert [e["batch_id"] for e in entries] == ["r1001", "r1002", "r2001"]
    assert {e["channel"] for e in entries} == {"coverage", "retrieval"}


def test_manifest_rewrite_replaces_a_rebuilt_batch(tmp_path):
    """Rebuilding the same batch id updates its entry rather than doubling it."""
    entries = write_batches(candidates(2), campaign="c", channel="coverage",
                            size=2, prefix="r1", outdir=tmp_path, instructions=None)
    write_manifest(entries, tmp_path, merge=True)
    again = write_batches(candidates(4), campaign="c", channel="coverage",
                          size=4, prefix="r1", outdir=tmp_path, instructions=None)
    write_manifest(again, tmp_path, merge=True)
    listed = json.loads((tmp_path / "index.json").read_text())["batches"]
    assert [e["batch_id"] for e in listed] == ["r1001"]
    assert listed[0]["n"] == 4


def test_written_batch_round_trips(tmp_path):
    write_batches(candidates(3), campaign="recover", channel="coverage", size=3,
                  prefix="b", outdir=tmp_path, instructions="read this")
    payload = json.loads((tmp_path / "b001.json").read_text())
    assert payload["campaign"] == "recover"
    assert payload["channel"] == "coverage"
    assert payload["instructions"] == "read this"
    assert len(payload["points"]) == 3
    assert payload["points"][0]["cell_km"] == 5.0


# ---------------------------------------------------------------------------
# the next round excludes the last one
# ---------------------------------------------------------------------------
def test_exclude_labelled_drops_returned_points(tmp_path):
    prior = tmp_path / "round1.csv"
    pd.DataFrame({"point_id": ["c001", "c003"]}).to_csv(prior, index=False)
    left = exclude_labelled(candidates(5), prior)
    assert list(left["id"]) == ["c000", "c002", "c004"]


def test_normalise_accepts_the_usual_aliases():
    frame = normalise(pd.DataFrame({"longitude": [1.0], "latitude": [2.0],
                                    "plot_id": ["p1"]}))
    assert {"lon", "lat", "id"} <= set(frame.columns)


def test_normalise_refuses_a_table_with_no_coordinates():
    with pytest.raises(ValueError, match="lon/lat"):
        normalise(pd.DataFrame({"id": ["a"]}))


# ---------------------------------------------------------------------------
# read-back
# ---------------------------------------------------------------------------
def returned(rows):
    return pd.DataFrame(rows, columns=[
        "campaign", "batch_id", "point_id", "transition", "is_change", "flags",
        "channel", "labeller", "labelled_at"])


def test_dedupe_keeps_a_second_reader_and_collapses_a_correction():
    frame = returned([
        ("c", "b1", "p1", "Nature -> Nature", 0, "", "coverage", "ann", "1"),
        ("c", "b1", "p1", "Cropland -> Cropland", 0, "", "coverage", "ann", "2"),
        ("c", "b1", "p1", "Nature -> Nature", 0, "", "coverage", "bo", "3"),
    ])
    out = dedupe(frame)
    assert len(out) == 2                                    # ann collapsed, bo kept
    ann = out.loc[out["labeller"] == "ann", "transition"].item()
    assert ann == "Cropland -> Cropland"                    # the later call wins


def test_uninterpretable_is_not_a_label_but_still_costs_a_point():
    """The yield denominator counts attempts, not successes.

    Dividing by usable rows would flatter every channel by exactly its own
    failure rate -- a channel that sends interpreters to 50% cloud would score
    the same as one that does not.
    """
    frame = returned([
        ("c", "b1", "p1", "Cropland -> Artificial", 1, "", "coverage", "ann", "1"),
        ("c", "b1", "p2", "", "", "uninterpretable", "coverage", "ann", "2"),
    ])
    assert len(usable(frame)) == 1
    rate = yield_by_channel(frame)
    assert rate.loc["coverage", "points_attempted"] == 2
    assert rate.loc["coverage", "Cropland -> Artificial"] == pytest.approx(0.5)


def test_enrichment_is_unavailable_without_an_equal_area_arm():
    """A missing control is not a passing control."""
    frame = returned([
        ("c", "b1", "p1", "Cropland -> Artificial", 1, "", "coverage", "ann", "1"),
    ])
    assert enrichment(yield_by_channel(frame), "Cropland -> Artificial") is None


def test_enrichment_reads_against_the_equal_area_arm():
    rows = [("c", "b1", f"r{i}", "Cropland -> Artificial" if i < 1 else
             "Nature -> Nature", 0, "", BASELINE_CHANNEL, "ann", "1")
            for i in range(10)]
    rows += [("c", "b2", f"c{i}", "Cropland -> Artificial" if i < 3 else
              "Nature -> Nature", 0, "", "coverage", "ann", "1")
             for i in range(10)]
    boost = enrichment(yield_by_channel(returned(rows)), "Cropland -> Artificial")
    assert boost["coverage"] == pytest.approx(3.0)


def test_agreement_is_read_only_on_doubly_read_points():
    frame = returned([
        ("c", "b1", "p1", "Nature -> Nature", 0, "", "coverage", "ann", "1"),
        ("c", "b1", "p1", "Cropland -> Cropland", 0, "", "coverage", "bo", "2"),
        ("c", "b1", "p2", "Nature -> Nature", 0, "", "coverage", "ann", "3"),
        ("c", "b1", "p2", "Nature -> Nature", 0, "", "coverage", "bo", "4"),
        ("c", "b1", "p3", "Nature -> Nature", 0, "", "coverage", "ann", "5"),
    ])
    n_double, rate, disagreements = agreement(frame)
    assert n_double == 2                       # p3 was read once, so it is out
    assert rate == pytest.approx(0.5)
    assert list(disagreements["point_id"]) == ["p1"]


# ---------------------------------------------------------------------------
# assignments (§AL7 T1.1): the overlap is a property of the batch FILE
#
# It used to be a checkbox in the app. Forgetting it in one direction produces
# duplicate work; forgetting it in the other produces zero overlap -- and zero
# overlap means the campaign's only measurement of its own label noise silently
# reports nothing. Neither is visible until the round report.
# ---------------------------------------------------------------------------
def test_every_point_gets_a_primary_expert():
    points = [{"id": f"p{i}"} for i in range(20)]
    assign(points, ["e1", "e2"], 0.05, seed=1)
    assert all(p["primary_expert"] in ("e1", "e2") for p in points)
    assert all(p["primary_expert"] in p["required_readers"] for p in points)


def test_the_overlap_sample_is_the_requested_fraction():
    points = [{"id": f"p{i}"} for i in range(100)]
    assign(points, ["e1", "e2"], 0.05, seed=1)
    doubled = [p for p in points if len(p["required_readers"]) > 1]
    assert len(doubled) == 5
    # ...and both readers are named, so the queues are decidable offline
    for point in doubled:
        assert len(set(point["required_readers"])) == 2


def test_assignment_is_deterministic():
    """Rebuilding a batch must not re-draw the overlap.

    A moving overlap sample makes the agreement number a moving target: the
    points two people read would differ between two builds of the same batch.
    """
    a = [{"id": f"p{i}"} for i in range(50)]
    b = [{"id": f"p{i}"} for i in range(50)]
    assign(a, ["e1", "e2"], 0.1, seed=7)
    assign(b, ["e1", "e2"], 0.1, seed=7)
    assert [p["required_readers"] for p in a] == [p["required_readers"] for p in b]


def test_assignment_round_robins_rather_than_blocking():
    """No expert gets the whole top of an acquisition surface.

    The channels are priced on different metrics, and an expert who only ever
    sees the highest-uncertainty points calibrates to a different distribution
    than one who never does.
    """
    points = [{"id": f"p{i}"} for i in range(20)]
    assign(points, ["e1", "e2"], 0.0, seed=1)
    top_ten = [p["primary_expert"] for p in points[:10]]
    assert top_ten.count("e1") == 5 and top_ten.count("e2") == 5


def test_one_expert_cannot_produce_an_overlap():
    points = [{"id": f"p{i}"} for i in range(20)]
    assign(points, ["e1"], 0.5, seed=1)
    assert all(len(p["required_readers"]) == 1 for p in points)
    assert assignment_counts(points) == {"e1": 20}


def test_the_manifest_carries_per_expert_counts(tmp_path):
    """So the app can resume "my assigned batch" without downloading every
    batch file in the campaign."""
    frame = candidates(10)
    entries = write_batches(frame, campaign="c", channel="coverage", size=10,
                            prefix="x", outdir=tmp_path, instructions=None,
                            experts=["e1", "e2"])
    assert set(entries[0]["assigned"]) == {"e1", "e2"}
    assert sum(entries[0]["assigned"].values()) >= 10
    written = json.loads((tmp_path / "x001.json").read_text())
    assert written["experts"] == ["e1", "e2"]
    assert all("primary_expert" in p for p in written["points"])


def test_a_candidate_table_that_carries_assignments_keeps_them(tmp_path):
    """A re-cut of an earlier round must not move the overlap sample."""
    frame = candidates(4)
    frame["primary_expert"] = ["e2", "e2", "e1", "e1"]
    frame["required_readers"] = ["e2|e1", "e2", "e1", "e1"]
    write_batches(frame, campaign="c", channel="coverage", size=4, prefix="y",
                  outdir=tmp_path, instructions=None, experts=["e1", "e2"])
    written = json.loads((tmp_path / "y001.json").read_text())
    assert [p["primary_expert"] for p in written["points"]] == \
        ["e2", "e2", "e1", "e1"]
    assert written["points"][0]["required_readers"] == ["e2", "e1"]


# ---------------------------------------------------------------------------
# the round report groups on expert_id, never on the typed name
# ---------------------------------------------------------------------------
def returned_with_experts(rows):
    return pd.DataFrame(rows, columns=["campaign", "batch_id", "point_id",
                                       "transition", "is_change", "flags",
                                       "channel", "expert_id", "labeller",
                                       "labelled_at"])


def test_grouping_is_on_expert_id_not_the_display_name():
    """One person typing their name four ways is one expert.

    "Ann", "ann", "Ann " and "Anne" are four experts to a groupby, and the
    failure is silent: the agreement number is computed over nothing and comes
    back a clean 100%.
    """
    frame = returned_with_experts([
        ("c", "b1", "p1", "Nature -> Nature", 0, "", "coverage", "e1", "Ann", "1"),
        ("c", "b1", "p1", "Cropland -> Nature", 0, "", "coverage", "e1", "ann ", "2"),
        ("c", "b1", "p1", "Nature -> Nature", 0, "", "coverage", "e2", "Bo", "3"),
    ])
    assert who_column(frame) == "expert_id"
    out = dedupe(frame)
    # the two "Ann" rows are ONE expert correcting themselves
    assert len(out) == 2
    n_double, rate, _ = agreement(out)
    assert n_double == 1                      # e1 and e2, not three readers
    assert rate == pytest.approx(0.0)         # and they disagreed


def test_rows_without_an_expert_id_fall_back_to_the_name(capsys):
    """Older rows stay readable, and the report says so out loud."""
    frame = returned_with_experts([
        ("c", "b1", "p1", "Nature -> Nature", 0, "", "coverage", "", "Ann", "1"),
        ("c", "b1", "p1", "Cropland -> Nature", 0, "", "coverage", "", "Bo", "2"),
    ])
    assert who_column(frame) == "expert_id"
    assert list(frame["expert_id"]) == ["Ann", "Bo"]
    assert "no expert_id" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# calibration stages (§AL7 T1.7)
# ---------------------------------------------------------------------------
def test_the_two_calibration_stages_are_reported_apart():
    """A teaching set tells you the answer after every call, which is what makes
    the legend stick and what makes the score meaningless. Pooling the two would
    report a number that is neither."""
    frame = pd.DataFrame({
        "stage": ["teach", "teach", "qualify"],
        "transition": ["a", "b", "c"],
    })
    assert len(stage_rows(frame, "teach")) == 2
    assert len(stage_rows(frame, "qualify")) == 1
    assert len(stage_rows(frame, None)) == 0


# ---------------------------------------------------------------------------
# the growing season (§AL7 T2.1)
# ---------------------------------------------------------------------------
def test_the_growing_season_flips_by_hemisphere():
    """A southern point composited over Jun-Sep is its DRY season.

    The whole timeline is then misleading in a way that looks like data: a
    dry-season NDVI series read as a growing-season one says "vegetation loss"
    about a place where nothing happened. c2c_ts_server.py hardcodes Jun-Sep
    because it is a Europe-only tool; these points are drawn globally.
    """
    north = growing_season(59.9)
    south = growing_season(-33.9)
    assert north["start_month"] == 6 and north["year_offset"] == 0
    assert south["start_month"] == 12
    # ...and the southern window STARTS IN THE PREVIOUS CALENDAR YEAR
    assert south["year_offset"] == -1


def test_the_tropics_get_the_whole_year():
    """There is no single growing window worth picking in the humid tropics, and
    a four-month one throws away most of the few cloud-free scenes there are."""
    season = growing_season(2.0)
    assert season["start_month"] == 1 and season["months"] == 12


def test_points_are_grouped_so_each_window_is_composited_once():
    points = [{"lat": 60.0}, {"lat": 58.0}, {"lat": -30.0}, {"lat": 1.0}]
    groups = season_groups(points)
    assert sorted(len(v) for v in groups.values()) == [1, 1, 2]
    assert set(groups) == {"northern (Jun-Sep)", "southern (Dec-Mar)",
                           "tropical (full year)"}

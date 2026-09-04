"""`label_rounds.agreement` — the number the whole campaign is bought on.

Inter-rater agreement is the campaign's only measurement of the label noise that
`ACTIVE_LEARNING.md` says caps change-F1, so a fault in it is silent by
construction: the report prints a clean percentage over the wrong denominator and
nothing anywhere says so. There was no test file for this module until
2026-08-31 (§AL11.7).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_rounds import agreement  # noqa: E402


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def row(point, expert, transition):
    return dict(batch_id="b", point_id=point, expert_id=expert,
                transition=transition)


def test_a_cannot_interpret_against_a_call_is_a_disagreement():
    """THE REGRESSION THIS FILE EXISTS FOR.

    `agreement` ran on `usable()`, which drops rows with no transition. So a
    point where one expert read `Nature -> Cropland` and the other said the
    imagery would not support a call was not a disagreement, not an agreement,
    and not a doubled point -- it left the denominator entirely.

    That is the most informative pair in the set: it is the one that says the
    two of them are not looking at the same evidence. And dropping it does not
    merely lose information, it biases the headline *upwards* -- on this fixture
    the old code reported 2 doubled points at 50%, against the true 3 at 33%.
    """
    df = frame([
        row("p1", "e1", "Nature -> Nature"),
        row("p1", "e2", "Nature -> Nature"),      # agree
        row("p2", "e1", "Nature -> Cropland"),
        row("p2", "e2", "Nature -> Nature"),      # disagree on the legend
        row("p3", "e1", "Nature -> Cropland"),
        row("p3", "e2", ""),                      # disagree on interpretability
    ])
    n, rate, dis = agreement(df)
    assert n == 3, "the cannot-interpret pair must be a doubled point"
    assert rate == pytest.approx(1 / 3)
    assert set(dis["point_id"]) == {"p2", "p3"}
    calls = dict(zip(dis["point_id"], dis["calls"]))
    assert "not interpretable" in calls["p3"], calls["p3"]


def test_both_saying_cannot_interpret_is_an_agreement():
    """The other side of it. Two people independently deciding the imagery will
    not support a call agree with each other, and that is worth knowing -- it
    points at the evidence rather than at the legend."""
    df = frame([
        row("p1", "e1", ""),
        row("p1", "e2", ""),
    ])
    n, rate, dis = agreement(df)
    assert n == 1
    assert rate == pytest.approx(1.0)
    assert dis.empty


def test_missing_transitions_read_the_same_as_empty_ones():
    """A sheet export gives `''` and a parquet round-trip gives NaN. They mean
    the same thing and must not split one point into two calls."""
    df = frame([
        row("p1", "e1", ""),
        row("p1", "e2", None),
    ])
    n, rate, _ = agreement(df)
    assert n == 1
    assert rate == pytest.approx(1.0)


def test_one_expert_reading_a_point_twice_is_not_agreement():
    """Counted on distinct EXPERTS, not on rows. A person who re-saves a point
    is one reading; treating it as two would let a single labeller manufacture a
    perfect agreement score over nothing."""
    df = frame([
        row("p1", "e1", "Nature -> Nature"),
        row("p1", "e1", "Nature -> Nature"),
    ])
    n, rate, _ = agreement(df)
    assert n == 0
    assert rate != rate          # NaN: there is nothing to report


def test_the_same_point_id_in_two_batches_is_two_points():
    """`build_label_batches.py` numbers points from `range(len(frame))`, so
    `p0000` exists in every batch. Grouping on point_id alone would collide two
    unrelated places into one agreement measurement."""
    df = frame([
        dict(batch_id="b1", point_id="p0", expert_id="e1",
             transition="Nature -> Nature"),
        dict(batch_id="b1", point_id="p0", expert_id="e2",
             transition="Nature -> Nature"),
        dict(batch_id="b2", point_id="p0", expert_id="e1",
             transition="Nature -> Cropland"),
        dict(batch_id="b2", point_id="p0", expert_id="e2",
             transition="Artificial -> Artificial"),
    ])
    n, rate, dis = agreement(df)
    assert n == 2
    assert rate == pytest.approx(0.5)
    assert set(dis["batch_id"]) == {"b2"}


def test_a_point_only_one_person_read_is_not_counted():
    df = frame([
        row("p1", "e1", "Nature -> Nature"),
        row("p2", "e2", "Nature -> Cropland"),
    ])
    n, rate, dis = agreement(df)
    assert n == 0
    assert rate != rate
    assert dis.empty

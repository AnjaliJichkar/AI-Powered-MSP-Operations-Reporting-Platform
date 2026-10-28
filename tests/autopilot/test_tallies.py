"""The two tallies, and what each does with a row it cannot read.

The results page raised an `AttributeError` the first time anybody pressed
Useful, because one tally was handed every review and only had columns for
`correct`, `changed` and `wrong`. The fix is that each counts the fields it
owns; these pin both halves of that, and the behaviour on a verdict neither
knows — counted as unrecognised, logged, never raised.
"""

from __future__ import annotations

import logging

import pytest

from app.autopilot.suggestions import FindingTally, ReviewTally


def test_a_classification_tally_counts_its_own_three_verdicts() -> None:
    tally = ReviewTally()
    for verdict in ("correct", "correct", "changed", "wrong"):
        tally.add(verdict, field_name="issue_type")
    assert (tally.correct, tally.changed, tally.wrong) == (2, 1, 1)
    assert tally.reviewed == 4
    assert tally.share_correct == 0.5
    assert tally.unrecognised == 0


def test_a_findings_tally_counts_useful_and_not_useful() -> None:
    tally = FindingTally(shown=10)
    for verdict in ("useful", "useful", "useful", "not_useful"):
        tally.add(verdict, field_name="finding:device_offline")
    assert (tally.useful, tally.not_useful) == (3, 1)
    assert tally.reviewed == 4
    assert tally.share_useful == 0.75
    assert tally.shown == 10


def test_a_classification_tally_handed_a_findings_verdict_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """What the page did before: it raised, and the whole results page 500ed.

    Now it is counted as unrecognised and said out loud. A row the page cannot
    read is a thing somebody should know about; dropping it silently is how a
    figure quietly stops adding up.
    """
    tally = ReviewTally()
    with caplog.at_level(logging.WARNING):
        tally.add("useful", field_name="finding:device_offline")
    assert tally.unrecognised == 1
    assert tally.reviewed == 0
    assert "unrecognised" in caplog.text
    assert "finding:device_offline" in caplog.text


def test_a_findings_tally_handed_a_classification_verdict_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tally = FindingTally()
    with caplog.at_level(logging.WARNING):
        tally.add("changed", field_name="next_step")
    assert tally.unrecognised == 1
    assert tally.reviewed == 0
    assert "next_step" in caplog.text


def test_an_unrecognised_verdict_is_on_the_page_rather_than_hidden() -> None:
    """It travels in the payload, so the reader sees that something was not
    counted rather than wondering why the numbers do not add up."""
    tally = ReviewTally()
    tally.add("nonsense", field_name="urgency")
    assert tally.as_json()["unrecognised"] == 1
    finding = FindingTally()
    finding.add("nonsense", field_name="card")
    assert finding.as_json()["unrecognised"] == 1

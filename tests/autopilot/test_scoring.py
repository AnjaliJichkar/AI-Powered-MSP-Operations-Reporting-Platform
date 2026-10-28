"""Scoring, without a database: the split, the thresholds, the two proxies.

`score_rows` takes joined rows and returns a scorecard, so everything the gate
is read from can be pinned here against rows written by hand — which is the
only way to be sure a number means what the report says it means.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.autopilot.shadow.scoring import (
    Joined,
    at_threshold,
    issue_type_score,
    length_band,
    lift,
    projected_monthly,
    score_rows,
    split,
)

RUN = uuid.uuid4()
NEWEST = dt.datetime(2026, 9, 30, 12, tzinfo=dt.UTC)


def row(
    *,
    ticket: str = "1",
    days_ago: int = 0,
    predicted: str | None = "Email",
    raw: str | None = None,
    actual: str | None = "Email",
    confidence: float | None = 0.9,
    words: int = 5,
    path: str = "model",
    quarantined: bool = False,
    origin: str | None = "human",
    queue: str | None = "Help Desk",
    unassigned: bool = False,
    grouped: bool = False,
    urgency: str | None = "normal",
    urgency_expected: str | None = "normal",
    policy: str | None = "kept",
) -> Joined:
    return Joined(
        ticket_id=ticket,
        created_at=NEWEST - dt.timedelta(days=days_ago),
        path=path,
        quarantined=quarantined,
        predicted=predicted,
        raw=raw if raw is not None else predicted,
        policy=policy,
        confidence=confidence,
        actual=actual,
        urgency=urgency,
        urgency_expected=urgency_expected,
        urgency_confidence=0.6,
        origin=origin,
        title_words=words,
        queue_label=queue,
        unassigned=unassigned,
        grouped=grouped,
        has_description=None,
    )


# --- the temporal split ---------------------------------------------------


def test_the_split_puts_the_last_thirty_days_in_the_holdout() -> None:
    """A threshold chosen on the tickets it is then scored on is a memory, not
    a measurement."""
    rows = [row(ticket=str(n), days_ago=n) for n in (0, 5, 29, 31, 60, 89)]
    holdout = split(rows, period="holdout", holdout_days=30)
    development = split(rows, period="development", holdout_days=30)
    assert [r.ticket_id for r in holdout] == ["0", "5", "29"]
    assert [r.ticket_id for r in development] == ["31", "60", "89"]
    assert len(split(rows, period="all", holdout_days=30)) == 6
    # Nothing is in both, and nothing is lost.
    assert not {r.ticket_id for r in holdout} & {r.ticket_id for r in development}
    assert len(holdout) + len(development) == len(rows)


def test_the_split_is_from_the_newest_ticket_not_from_today() -> None:
    """ "The last thirty days" means of the data: a window scored a week later
    must not quietly become a 23-day holdout."""
    # Both are long past, and forty days apart: the holdout is measured from
    # the newest of them, so the older one is development even though today is
    # more than a year after both.
    rows = [row(ticket="old", days_ago=435), row(ticket="new", days_ago=395)]
    assert [r.ticket_id for r in split(rows, period="holdout", holdout_days=30)] == ["new"]
    assert [r.ticket_id for r in split(rows, period="development", holdout_days=30)] == ["old"]


# --- accuracy, coverage, thresholds ---------------------------------------


def test_coverage_is_reported_beside_accuracy() -> None:
    """An agent that answers four tickets perfectly is not better than one that
    answers four hundred well."""
    rows = [row(predicted="unsure") for _ in range(9)] + [row(predicted="Email")]
    score = issue_type_score(rows)
    assert score.judged == 10
    assert score.answered == 1
    assert score.accuracy == pytest.approx(1.0)
    assert score.coverage == pytest.approx(0.1)


def test_a_threshold_turns_low_confidence_into_a_decline() -> None:
    """Which is what a threshold is — so the denominator stays the whole
    judgeable population and coverage means what it says."""
    rows = [
        row(ticket="a", confidence=0.95, predicted="Email", actual="Email"),
        row(ticket="b", confidence=0.60, predicted="Email", actual="Server"),
        row(ticket="c", confidence=0.60, predicted="Email", actual="Email"),
    ]
    loose = at_threshold(rows, 0.0)
    assert (loose.answered, loose.correct, loose.judged) == (3, 2, 3)
    assert loose.coverage == pytest.approx(1.0)

    strict = at_threshold(rows, 0.9)
    assert (strict.answered, strict.correct, strict.judged) == (1, 1, 3)
    assert strict.accuracy == pytest.approx(1.0)
    assert strict.coverage == pytest.approx(1 / 3)


def test_the_policy_is_scored_before_and_after_from_one_run() -> None:
    """The raw answer is on the row, so the policy's worth is a measurement
    rather than a comparison of two runs that differ in other ways too."""
    rows = [
        # The model said the rarely-used label; the policy remapped it.
        row(
            predicted="User Access and Management",
            raw="Customer Onboarding Offboarding",
            actual="User Access and Management",
            policy="remapped",
        )
        for _ in range(4)
    ]
    assert issue_type_score(rows).accuracy == pytest.approx(1.0)
    assert issue_type_score(rows, use_raw=True).accuracy == pytest.approx(0.0)


def test_a_quarantined_row_is_predicted_on_and_never_scored() -> None:
    rows = [row(quarantined=True, predicted="Server", actual="Email"), row()]
    score = issue_type_score(rows)
    assert score.judged == 1
    assert score.excluded == 1
    assert score.accuracy == pytest.approx(1.0)


# --- by title length ------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "band"), [(1, "<=3"), (3, "<=3"), (4, "4-6"), (6, "4-6"), (7, "7+"), (23, "7+")]
)
def test_title_length_bands(words: int, band: str) -> None:
    assert length_band(words) == band


def test_accuracy_is_reported_by_title_length() -> None:
    """28% of this tenant's subjects are three words or fewer, which is a
    different task from classifying a sentence."""
    rows = [
        row(ticket="a", words=2, predicted="Email", actual="Server"),
        row(ticket="b", words=2, predicted="Email", actual="Server"),
        row(ticket="c", words=9, predicted="Email", actual="Email"),
    ]
    card = score_rows(rows, RUN)
    assert card.by_title_length["<=3"].accuracy == pytest.approx(0.0)
    assert card.by_title_length["7+"].accuracy == pytest.approx(1.0)


# --- origin: two proxies, both labelled ------------------------------------


def test_both_origin_proxies_are_counted_and_neither_is_a_score() -> None:
    rows = [
        row(ticket="a", origin="alert", unassigned=True, queue="Alerts"),
        row(ticket="b", origin="human", unassigned=False, queue="Help Desk"),
        row(ticket="c", origin="human", unassigned=True, queue="Monitoring Alert"),
    ]
    card = score_rows(rows, RUN)
    assert (card.proxy_unassigned, card.proxy_unassigned_alert) == (2, 1)
    assert (card.proxy_assigned, card.proxy_assigned_human) == (1, 1)
    assert (card.proxy_alert_queue, card.proxy_alert_queue_alert) == (2, 1)
    assert (card.proxy_other_queue, card.proxy_other_queue_human) == (1, 1)
    # The report calls them proxies; the card carries no "origin accuracy".
    assert not hasattr(card, "origin_accuracy")


# --- the variant's lift ---------------------------------------------------


def test_the_lift_separates_being_righter_from_being_bolder() -> None:
    """An extra input that only makes the agent answer more often is a coverage
    change, and reporting accuracy alone would dress it up as an improvement."""
    baseline = score_rows(
        [row(ticket="a", predicted="unsure"), row(ticket="b", predicted="Email")], RUN
    )
    bolder = score_rows(
        [row(ticket="a", predicted="Server", actual="Email"), row(ticket="b", predicted="Email")],
        RUN,
    )
    numbers = lift(baseline, bolder)["issue_type"]
    assert numbers["coverage_lift"] == pytest.approx(0.5)
    assert numbers["accuracy_lift"] == pytest.approx(-0.5)
    assert (numbers["baseline_answered"], numbers["variant_answered"]) == (1, 2)


# --- cost projection ------------------------------------------------------


def test_an_unpriced_run_projects_nothing_rather_than_zero() -> None:
    card = score_rows([row()], RUN)
    assert card.cost is None
    assert projected_monthly(card, days=90) is None


def test_a_projection_uses_the_whole_run_not_the_scored_period() -> None:
    """The tokens were spent on every ticket, not only the ones scored."""
    card = score_rows([row()], RUN)
    card.cost = 9.0
    assert projected_monthly(card, days=90, tickets=2700) == pytest.approx(3.0)
    assert projected_monthly(card, days=90, tickets=2700, franchisees=20) == pytest.approx(60.0)

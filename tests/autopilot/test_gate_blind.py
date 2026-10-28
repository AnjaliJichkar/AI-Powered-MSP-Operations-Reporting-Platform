"""The gate reads blind answers, and never an approval.

This is the change that makes the suggestion card safe to build: an engineer who
has read "Looks like User Access and Management" and pressed Correct has agreed
with the agent, and if that counted as accuracy the measurement would improve
every time the agent became more persuasive. So the two accuracy criteria are
pinned here against answers given blind, and the readiness rows — which do count
approvals — are pinned as never passing or failing anything.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.autopilot.gate import (
    NEXT_STEPS,
    AgentAnswer,
    BlindAnswers,
    Criterion,
    Gate,
    agent_answers,
    build_gate,
    issue_type_criteria,
    origin_criterion,
    readiness_rows,
)
from app.autopilot.labelling import GroupJudgements, OriginLabels
from app.autopilot.shadow.scoring import Joined, ThresholdRule
from app.autopilot.suggestions import LiveReviewCounts, ReviewTally
from app.models.suggestion import TicketSuggestion

NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.UTC)
RULE = ThresholdRule(min_accuracy=0.88, min_coverage=0.40)


def joined(
    ticket_id: str,
    *,
    days_ago: int = 60,
    predicted: str | None = "Email",
    actual: str | None = "Email",
    confidence: float | None = 0.95,
    origin: str | None = "human",
) -> Joined:
    return Joined(
        ticket_id=ticket_id,
        created_at=NOW - dt.timedelta(days=days_ago),
        path="model",
        quarantined=False,
        predicted=predicted,
        raw=predicted,
        policy=None,
        confidence=confidence,
        actual=actual,
        urgency=None,
        urgency_expected=None,
        urgency_confidence=None,
        origin=origin,
        title_words=5,
        queue_label=None,
        unassigned=False,
        grouped=False,
        has_description=False,
    )


def development_rows() -> list[Joined]:
    """Enough of a development period for the rule to pick a threshold from.

    Spread over ninety days so the split has something either side of it: the
    period is measured back from the newest ticket, so twenty rows a day apart
    would all be holdout and the rule would have nothing to choose on.
    Everything right and confident, because the threshold itself is not what
    these tests are about.
    """
    return [joined(f"d{n}", days_ago=89 - n * 4) for n in range(20)]


def blind(**answers: dict[str, str]) -> BlindAnswers:
    found = BlindAnswers(by_ticket=dict(answers))
    found.answered = len(found.by_ticket)
    return found


def suggestion(ticket_id: str, **over: object) -> TicketSuggestion:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "ticket_id": ticket_id,
        "issue_type": "Email",
        "issue_type_confidence": 0.93,
        "origin": "alert",
        "origin_confidence": 0.95,
        "display_threshold": 0.80,
        "blind_check": True,
        "created_at": NOW,
    }
    fields.update(over)
    return TicketSuggestion(**fields)


# --- issue type -------------------------------------------------------------


def test_with_no_blind_answers_the_accuracy_criteria_are_not_measured() -> None:
    """Not a zero, and not a pass: nobody has judged anything yet."""
    rows, _ = issue_type_criteria(
        development_rows(),
        rule=RULE,
        holdout_days=30,
        blind=BlindAnswers(),
        answers={},
    )
    assert [row.verdict for row in rows] == ["not_measured", "not_measured"]
    assert "no blind answer" in rows[0].detail


def test_accuracy_is_read_against_what_a_person_said_blind() -> None:
    said = blind(
        t1={"issue_type": "Email"},
        t2={"issue_type": "Email"},
        t3={"issue_type": "Printer/Scanner/Copier"},
    )
    answers = {
        "t1": AgentAnswer(issue_type="Email", issue_type_confidence=0.95),
        "t2": AgentAnswer(issue_type="Email", issue_type_confidence=0.91),
        "t3": AgentAnswer(issue_type="Email", issue_type_confidence=0.90),
    }
    rows, floor = issue_type_criteria(
        development_rows(), rule=RULE, holdout_days=30, blind=said, answers=answers
    )
    assert floor is not None
    accuracy, coverage = rows
    assert accuracy.value == "66.7%"
    assert accuracy.verdict == "fail"
    assert coverage.value == "100.0%"
    assert "blind" in accuracy.detail


def picky_development_rows() -> list[Joined]:
    """A development period where the rule has to pick a real floor.

    Half the answers are confident and right, half are hesitant and wrong, so
    0.00 reads 50% accuracy and only 0.60 clears the rule's bars.
    """
    rows = [joined(f"g{n}", days_ago=89 - n * 2, confidence=0.9) for n in range(10)]
    rows += [
        joined(f"b{n}", days_ago=88 - n * 2, confidence=0.5, predicted="Email", actual="Printer")
        for n in range(10)
    ]
    # And a holdout so the window has both halves.
    rows += [joined(f"h{n}", days_ago=10 - n % 5, confidence=0.9) for n in range(5)]
    return rows


def test_a_declined_ticket_is_out_of_the_accuracy_and_into_the_coverage() -> None:
    """The same rule the replay scores by: `unsure` is a decline, not an error,
    and declining is not free either. Below the chosen floor counts the same
    way — the agent said something, and we treated it as a decline."""
    said = blind(
        t1={"issue_type": "Email"},
        t2={"issue_type": "Email"},
        t3={"issue_type": "Email"},
        t4={"issue_type": "Email"},
    )
    answers = {
        "t1": AgentAnswer(issue_type="Email", issue_type_confidence=0.95),
        "t2": AgentAnswer(issue_type="Email", issue_type_confidence=0.95),
        # Below the floor the rule picked, and declined outright: neither is an
        # answer, and both are in the coverage denominator.
        "t3": AgentAnswer(issue_type="Email", issue_type_confidence=0.10),
        "t4": AgentAnswer(issue_type="unsure", issue_type_confidence=0.99),
    }
    rows, floor = issue_type_criteria(
        picky_development_rows(),
        rule=ThresholdRule(min_accuracy=0.88, min_coverage=0.40),
        holdout_days=30,
        blind=said,
        answers=answers,
    )
    assert floor == 0.6
    accuracy, coverage = rows
    assert accuracy.value == "100.0%"
    assert coverage.value == "50.0%"


def test_a_persons_unsure_is_not_ground_truth_either() -> None:
    """A labeller who cannot tell has said something about the subject line and
    nothing about the ticket, so it is out of the denominator like the agent's."""
    said = blind(t1={"issue_type": "unsure"}, t2={"issue_type": "Email"})
    answers = {
        "t1": AgentAnswer(issue_type="Printer/Scanner/Copier", issue_type_confidence=0.99),
        "t2": AgentAnswer(issue_type="Email", issue_type_confidence=0.99),
    }
    rows, _ = issue_type_criteria(
        development_rows(), rule=RULE, holdout_days=30, blind=said, answers=answers
    )
    assert rows[0].value == "100.0%"


# --- origin -----------------------------------------------------------------


def test_origin_precision_counts_labels_and_blind_checks_together() -> None:
    """The same question, asked of the same kind of person, with nothing of the
    agent's answer on screen either time."""
    labels = OriginLabels(by_ticket={"p1": "alert", "p2": "human"}, labelled=2)
    said = blind(t1={"origin": "alert"}, t2={"origin": "human"})
    answers = {
        "p1": AgentAnswer(origin="alert"),
        "p2": AgentAnswer(origin="alert"),
        "t1": AgentAnswer(origin="alert"),
        "t2": AgentAnswer(origin="human"),
    }
    row = origin_criterion(labels, said, answers)
    # Three called alert, two of them right.
    assert row.value == "66.7%"
    assert row.verdict == "fail"
    assert "2 past-ticket label(s) and 2 blind check(s)" in row.detail


def test_origin_is_not_measured_until_somebody_has_judged_something() -> None:
    row = origin_criterion(OriginLabels(), BlindAnswers(), {})
    assert row.verdict == "not_measured"
    assert "0 past-ticket label(s) and 0 blind check(s)" in row.detail


# --- where the agent's answer comes from ------------------------------------


def test_a_suggestion_outranks_a_replay_row_for_the_same_ticket() -> None:
    """It is the answer the engineer was actually shown, and the one the blind
    check was put against."""
    answers = agent_answers(
        [joined("t1", predicted="Email", confidence=0.5)],
        [suggestion("t1", issue_type="Printer/Scanner/Copier", issue_type_confidence=0.97)],
    )
    assert answers["t1"].issue_type == "Printer/Scanner/Copier"
    assert answers["t1"].issue_type_confidence == 0.97


# --- readiness --------------------------------------------------------------


def test_readiness_rows_report_approval_and_never_pass_or_fail() -> None:
    """Approval is a different measurement and is drawn as one: no verdict, and
    a target column that says so in words."""
    counts = LiveReviewCounts(by_field={"issue_type": ReviewTally(correct=7, changed=2, wrong=1)})
    rows = {row.key: row for row in readiness_rows(counts)}
    typed = rows["readiness_issue_type"]
    assert typed.value == "70.0%"
    assert typed.verdict == "not_measured"
    assert typed.target == "reported, not gated"
    assert "Approval, not accuracy" in typed.detail


def test_readiness_never_moves_whether_the_gate_is_met() -> None:
    """The whole point of keeping them apart: a hundred approvals cannot open
    Level 1, and no approval can close it either."""
    gate = build_gate(
        development_rows(),
        rule=RULE,
        holdout_days=30,
        labels=OriginLabels(),
        groups=GroupJudgements(),
        forward=_forward(),
        blind=BlindAnswers(),
        reviews=LiveReviewCounts(by_field={"issue_type": ReviewTally(correct=500)}),
    )
    assert gate.met is False
    assert all(row.verdict == "not_measured" for row in gate.readiness)
    assert "readiness_issue_type" not in {row.key for row in gate.criteria}


def _forward() -> object:
    from app.autopilot.gate import ForwardState

    return ForwardState()


# --- the headline -----------------------------------------------------------


def test_the_headline_counts_what_is_waiting_and_what_is_short() -> None:
    """The sentence at the top of the results page.

    Counts rather than names, because four criteria waiting for data is a
    different situation from one that is three points short, and the rows
    underneath say which is which. The wording is pinned here because it is the
    one sentence somebody reads before deciding whether to let an agent act.
    """
    gate = build_gate(
        development_rows(),
        rule=RULE,
        holdout_days=30,
        labels=OriginLabels(),
        groups=GroupJudgements(),
        forward=_forward(),
        blind=BlindAnswers(),
    )
    headline = gate.headline
    assert headline.ready is False
    # Nothing has been judged at all, so every criterion is waiting.
    waiting = sum(1 for one in gate.criteria if one.verdict == "not_measured")
    failing = sum(1 for one in gate.criteria if one.verdict == "fail")
    assert failing == 0
    assert headline.sentence == f"Not ready yet: {waiting} checks waiting on data."
    assert "below target" not in headline.sentence


def test_the_headline_counts_both_kinds_when_there_are_both() -> None:
    """One short and the rest unmeasured reads as both, in that order: what is
    failing first, because it is the thing somebody can act on."""
    gate = _gate_with_one_failing()
    failing = [one for one in gate.criteria if one.verdict == "fail"]
    waiting = [one for one in gate.criteria if one.verdict == "not_measured"]
    assert failing and waiting, "the fixture is meant to produce one of each"
    assert gate.headline.sentence == (
        f"Not ready yet: {len(waiting)} checks waiting on data, {len(failing)} below target."
    )


def test_the_headline_still_names_the_blocking_criterion() -> None:
    """The sentence counts; `blocking` and `next_step` name. A reader who
    fixes the named one gets a new headline naming the next, which is how five
    rows turn into one thing to do — and the page draws `next_step` under the
    sentence, so losing it would leave the count with nothing to do about it."""
    gate = _gate_with_one_failing()
    headline = gate.headline
    failing = [one for one in gate.criteria if one.verdict == "fail"]
    # The first failing criterion, not the first criterion of any kind.
    assert headline.blocking == failing[0].key
    assert headline.next_step, "a named criterion with nothing to do about it is half an answer"
    assert headline.next_step == NEXT_STEPS[headline.blocking]


def test_a_met_gate_says_so_and_asks_for_nothing() -> None:
    gate = Gate(
        criteria=[
            Criterion(
                key="issue_type_accuracy", label="Issue type accuracy", target="x", verdict="pass"
            ),
            Criterion(
                key="origin_precision",
                label="Origin precision on alert",
                target="x",
                verdict="pass",
            ),
        ]
    )
    assert gate.met is True
    assert gate.headline.ready is True
    assert gate.headline.sentence == "Ready for Level 1 on the measurements taken so far."
    assert gate.headline.blocking is None and gate.headline.next_step is None


def _gate_with_one_failing() -> object:
    """A gate with one criterion short and the rest waiting for data.

    Origin precision is the one that can be made to fail with nothing else
    measured: two tickets labelled by a person, both called `alert` by the
    agent and only one of them actually one.
    """
    return build_gate(
        development_rows(),
        rule=RULE,
        holdout_days=30,
        labels=OriginLabels(by_ticket={"p1": "alert", "p2": "human"}, labelled=2),
        groups=GroupJudgements(),
        forward=_forward(),
        blind=BlindAnswers(),
        suggestions=[
            suggestion("p1", origin="alert", blind_check=False),
            suggestion("p2", origin="alert", blind_check=False),
        ],
    )

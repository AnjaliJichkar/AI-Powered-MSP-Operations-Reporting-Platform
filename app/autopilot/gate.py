"""The gate: four criteria, each with its value, its target, and a verdict.

The point of putting this in one place is that a gate read off a page should be
the same gate read off the terminal, and that **"not measured" is a verdict**.
Three of the four criteria depend on work nobody has done yet — labels that
have not been written, a fortnight that has not run — and a panel that showed
those as 0% would read as a failure when it is an absence. So every criterion
returns one of `pass`, `fail` or `not_measured`, and a `not_measured` carries
the reason in words: "labelling 0 of 200".

The threshold is chosen by the rule, on the replay's **development** period.
Both of the accuracy criteria are then read against **blind answers** — a
person's judgement given before they were shown anything, whether that was an
engineer answering the quick check on a live ticket or somebody working through
the past-ticket sample. Never against approvals: an engineer who has read
"Looks like User Access and Management" and pressed Correct has agreed with the
agent, which is a useful thing to count and not an accuracy.

The replay's own accuracy against the ledger is still worth reading and is
still on the page, under past tickets. It is not a gate row, because the thing
being gated is an agent that answers tickets as they arrive, and the population
that measures is the one somebody judged blind.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.autopilot.labelling import GroupJudgements, OriginLabels
from app.autopilot.shadow.scoring import Joined, ThresholdRule, split
from app.autopilot.suggestions import LiveReviewCounts
from app.models.shadow import ShadowEnrichment, ShadowPrediction, ShadowRun
from app.models.suggestion import BlindCheck

Verdict = Literal["pass", "fail", "not_measured"]

#: The targets. Here rather than in settings because they are the product's
#: promise rather than a knob: moving one is a decision somebody writes down,
#: not a deployment variable.
ISSUE_TYPE_TARGET = 0.85
ISSUE_TYPE_COVERAGE_TARGET = 0.40
ALERT_PRECISION_TARGET = 0.95
GROUPING_TARGET = 0.90
FORWARD_DAYS_TARGET = 14


@dataclass
class BlindAnswers:
    """What people said about tickets before they were shown anything.

    One table, two ways in: an engineer answering the quick check on a live
    ticket, and somebody working through the past-ticket sample. They are the
    same act and they are scored together, because the question put to both is
    word for word the same question.
    """

    by_ticket: dict[str, dict[str, str]] = field(default_factory=dict)
    """ticket id -> {field: answer}, latest per person, disagreements dropped."""
    answered: int = 0
    """Tickets with at least one answer that counts."""
    disagreed: int = 0
    """Tickets two people answered differently. Left out rather than settled:
    an answer two people cannot agree on is not ground truth."""

    def answers_of(self, name: str) -> dict[str, str]:
        """The answers for one field, `unsure` dropped.

        A decline is an answer about the subject line and not an answer about
        the ticket, so it is out of the denominator here exactly as the agent's
        own `unsure` is out of its accuracy.
        """
        return {
            ticket: found[name]
            for ticket, found in self.by_ticket.items()
            if found.get(name) and found[name] != "unsure"
        }


async def blind_answers(session: AsyncSession, *, client_id: uuid.UUID) -> BlindAnswers:
    """Every blind answer this client has, as the gate reads them."""
    rows = (
        (
            await session.execute(
                select(BlindCheck)
                .where(BlindCheck.client_id == client_id)
                .order_by(BlindCheck.created_at, BlindCheck.id)
            )
        )
        .scalars()
        .all()
    )
    # The latest answer per person per field, then the people compared.
    per_person: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        per_person.setdefault((row.ticket_id, row.field), {})[row.reviewer_ref] = row.answer
    found = BlindAnswers()
    for (ticket_id, name), answers in sorted(per_person.items()):
        said = set(answers.values())
        if len(said) > 1:
            found.disagreed += 1
            continue
        found.by_ticket.setdefault(ticket_id, {})[name] = said.pop()
    found.answered = len(found.by_ticket)
    return found


@dataclass(frozen=True, slots=True)
class AgentAnswer:
    """What the agent said about one ticket, whichever path put it there.

    A blind answer can be about a live ticket the agent suggested on or about a
    past ticket from a replay, and the gate scores both together. So the two
    sources are flattened into one map here rather than each criterion knowing
    about both.
    """

    issue_type: str | None = None
    issue_type_confidence: float | None = None
    origin: str | None = None


def agent_answers(
    rows: list[Joined], suggestions: list[Any] | None = None
) -> dict[str, AgentAnswer]:
    """Ticket id -> what the agent said, from the replay and the suggestions.

    A suggestion wins where both exist: it is the answer an engineer was
    actually shown, and it is the one the blind check was put against.
    """
    found: dict[str, AgentAnswer] = {
        row.ticket_id: AgentAnswer(
            issue_type=row.predicted,
            issue_type_confidence=row.confidence,
            origin=row.origin,
        )
        for row in rows
    }
    for suggestion in suggestions or []:
        found[suggestion.ticket_id] = AgentAnswer(
            issue_type=suggestion.issue_type,
            issue_type_confidence=(
                float(suggestion.issue_type_confidence)
                if suggestion.issue_type_confidence is not None
                else None
            ),
            origin=suggestion.origin,
        )
    return found


@dataclass
class Criterion:
    """One line of the panel."""

    key: str
    label: str
    target: str
    """The target in words, as a reader would say it."""
    verdict: Verdict
    value: str | None = None
    detail: str = ""
    """Why it passed, failed, or could not be measured. Always written out:
    the panel is read by somebody deciding whether to let an agent act."""

    def as_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "target": self.target,
            "verdict": self.verdict,
            "value": self.value,
            "detail": self.detail,
        }


#: What to do next about each criterion, in the words of the person who would
#: do it. A panel that says "not measured" and leaves the reader to work out
#: who is supposed to do what is a panel that stays not measured.
NEXT_STEPS: dict[str, str] = {
    "issue_type_accuracy": (
        "Answer the quick check on more tickets. The accuracy is read against those "
        "answers, so until engineers have given a few dozen there is nothing to read."
    ),
    "issue_type_coverage": (
        "The agent is answering too few of the tickets people judged. Either it needs "
        "to be surer more often, or the threshold the rule picked is too high for this "
        "franchisee's tickets."
    ),
    "origin_precision": (
        "Answer the quick check on more tickets, or finish the past-ticket sample. "
        "Precision on alert is read against those and nothing else."
    ),
    "grouping": "Judge the sampled groups on the past-ticket page: same issue, or not.",
    "forward_shadow": (
        "Turn AUTOPILOT_SHADOW_LIVE on and leave it a fortnight. Nothing else here moves that row."
    ),
}


@dataclass
class Headline:
    """Ready or not, in one sentence, with the next thing to do.

    The top of the results page is read by somebody deciding whether to let an
    agent act. "Not ready yet" is only useful beside what is stopping it.
    """

    ready: bool
    sentence: str
    blocking: str | None = None
    next_step: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "sentence": self.sentence,
            "blocking": self.blocking,
            "next_step": self.next_step,
        }


@dataclass
class Gate:
    """The four criteria, and whether the lot is met."""

    criteria: list[Criterion] = field(default_factory=list)
    readiness: list[Criterion] = field(default_factory=list)
    """Level 1 readiness: how often a suggestion was approved, per field. Kept
    apart from `criteria` because `met` must not read them — approval is not
    accuracy, and a readiness row has no bar to clear."""
    chosen_threshold: float | None = None
    rule: dict[str, Any] = field(default_factory=dict)

    @property
    def met(self) -> bool:
        """Every criterion passed. A single `not_measured` is not a pass."""
        return bool(self.criteria) and all(one.verdict == "pass" for one in self.criteria)

    @property
    def headline(self) -> Headline:
        """What the top of the page says, and what to do about it.

        The first criterion that is not passing is the one named: a reader who
        fixes it gets a new headline naming the next, which is how a panel of
        five rows turns into one thing to do.
        """
        if self.met:
            return Headline(
                ready=True,
                sentence="Ready for Level 1 on the measurements taken so far.",
            )
        failing = [one for one in self.criteria if one.verdict == "fail"]
        waiting = [one for one in self.criteria if one.verdict == "not_measured"]
        ordered = failing or waiting
        if not ordered:
            return Headline(ready=False, sentence="Not ready yet: nothing has been measured.")
        # How many, and of which kind, because that is what somebody reads
        # first: four criteria waiting for data is a different situation from
        # one that is three points short, and the rows underneath say which is
        # which. The blocking one is still named — it is what `next_step`
        # answers — but it is not what the sentence counts.
        counts = [
            f"{len(waiting)} {'check' if len(waiting) == 1 else 'checks'} waiting on data"
            if waiting
            else None,
            f"{len(failing)} below target" if failing else None,
        ]
        return Headline(
            ready=False,
            sentence="Not ready yet: " + ", ".join(one for one in counts if one) + ".",
            blocking=ordered[0].key,
            next_step=NEXT_STEPS.get(ordered[0].key),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "met": self.met,
            "headline": self.headline.as_json(),
            "chosen_threshold": self.chosen_threshold,
            "rule": self.rule,
            "criteria": [one.as_json() for one in self.criteria],
            "readiness": [one.as_json() for one in self.readiness],
        }


def _percent(value: float | None) -> str | None:
    return None if value is None else f"{100 * value:.1f}%"


def issue_type_criteria(
    rows: list[Joined],
    *,
    rule: ThresholdRule,
    holdout_days: int,
    blind: BlindAnswers,
    answers: dict[str, AgentAnswer],
) -> tuple[list[Criterion], float | None]:
    """Accuracy and coverage against **blind answers**, at the chosen threshold.

    The threshold is still chosen the way it always was — by the fixed rule, on
    the replay's development period, before anybody looked at these numbers.
    What changed is what the accuracy is read against: a person's answer given
    before they were shown the agent's, rather than the ledger's eventual label
    or, worse, an engineer's approval of the suggestion they had just read.

    Two criteria from one threshold, because they trade against each other and
    a panel that showed only accuracy would reward an agent for answering four
    tickets.
    """
    development = split(rows, period="development", holdout_days=holdout_days)
    accuracy_row = Criterion(
        key="issue_type_accuracy",
        label="Issue type accuracy",
        target=f"at least {_percent(ISSUE_TYPE_TARGET)} on blind checks",
        verdict="not_measured",
    )
    coverage_row = Criterion(
        key="issue_type_coverage",
        label="Issue type coverage",
        target=f"at least {_percent(ISSUE_TYPE_COVERAGE_TARGET)} answered",
        verdict="not_measured",
    )
    if not development:
        reason = "the replay has no development period to choose a threshold on"
        accuracy_row.detail = coverage_row.detail = reason
        return [accuracy_row, coverage_row], None

    floor = rule.choose(development)
    if floor is None:
        reason = (
            f"no threshold on the development period reaches "
            f"{_percent(rule.min_accuracy)} accuracy at "
            f"{_percent(rule.min_coverage)} coverage, so the rule picks none"
        )
        accuracy_row.detail = coverage_row.detail = reason
        return [accuracy_row, coverage_row], None

    chose = (
        f"threshold {floor:.2f}, chosen on the replay's development period by the rule "
        f"(lowest with accuracy at least {_percent(rule.min_accuracy)} and coverage at "
        f"least {_percent(rule.min_coverage)})"
    )
    truth = blind.answers_of("issue_type")
    if not truth:
        reason = (
            "no blind answer about a ticket's type yet"
            + (f"; {blind.disagreed} answered differently by two people" if blind.disagreed else "")
            + f". {chose}"
        )
        accuracy_row.detail = coverage_row.detail = reason
        return [accuracy_row, coverage_row], floor

    judged = answered = correct = 0
    for ticket_id, said in truth.items():
        answer = answers.get(ticket_id)
        if answer is None:
            # Judged by a person, never answered by the agent: not part of
            # anything the agent can be scored on, and not silently a miss.
            continue
        judged += 1
        if (
            answer.issue_type is None
            or answer.issue_type == "unsure"
            or answer.issue_type_confidence is None
            or answer.issue_type_confidence < floor
        ):
            continue
        answered += 1
        if answer.issue_type == said:
            correct += 1
    if not judged:
        accuracy_row.detail = coverage_row.detail = (
            f"{len(truth)} blind answer(s), none of them about a ticket the agent answered. {chose}"
        )
        return [accuracy_row, coverage_row], floor

    accuracy = correct / answered if answered else None
    coverage = answered / judged
    accuracy_row.value = _percent(accuracy)
    accuracy_row.verdict = (
        "not_measured"
        if accuracy is None
        else ("pass" if accuracy >= ISSUE_TYPE_TARGET else "fail")
    )
    accuracy_row.detail = (
        f"{correct} of {answered} answered correctly against blind answers at {chose}"
        if accuracy is not None
        else f"nothing above the threshold was answered on the {judged} blind-checked tickets"
    )
    coverage_row.value = _percent(coverage)
    coverage_row.verdict = "pass" if coverage >= ISSUE_TYPE_COVERAGE_TARGET else "fail"
    coverage_row.detail = f"{answered} of {judged} blind-checked tickets answered at {chose}" + (
        f"; {blind.disagreed} left out, two people disagreed" if blind.disagreed else ""
    )
    return [accuracy_row, coverage_row], floor


def origin_criterion(
    labels: OriginLabels,
    blind: BlindAnswers,
    answers: dict[str, AgentAnswer],
) -> Criterion:
    """Precision on `alert`, against what people said blind and nothing else.

    Precision rather than accuracy because the gate's worry is a ticket the
    agent calls machine-generated that a person raised: that is the one that
    would be handled without a person if the agent were ever allowed to act.

    The truth is the past-ticket labels and the live blind checks together —
    the same question, asked of the same kind of person, with nothing of the
    agent's answer on screen either time. The two proxies on the results page
    are reported and never used here: a gate read off a proxy is a gate read
    off a guess, and an approval is not evidence at all.
    """
    blind_origins = blind.answers_of("origin")
    counted = (
        f"{len(labels.by_ticket)} past-ticket label(s) and {len(blind_origins)} blind check(s)"
    )
    truth: dict[str, str] = {**labels.by_ticket, **blind_origins}
    if not truth:
        return Criterion(
            key="origin_precision",
            label="Origin precision on alert",
            target=f"at least {_percent(ALERT_PRECISION_TARGET)} on blind answers",
            verdict="not_measured",
            detail=(
                f"nothing judged blind yet: {counted}"
                + (
                    f"; {labels.disagreed + blind.disagreed} disagreed and are left out"
                    if labels.disagreed or blind.disagreed
                    else ""
                )
            ),
        )
    called_alert = [
        ticket
        for ticket, said in truth.items()
        if (answers.get(ticket) or AgentAnswer()).origin == "alert"
    ]
    if not called_alert:
        return Criterion(
            key="origin_precision",
            label="Origin precision on alert",
            target=f"at least {_percent(ALERT_PRECISION_TARGET)} on blind answers",
            verdict="not_measured",
            detail=f"none of the {len(truth)} judged tickets was called alert ({counted})",
        )
    right = [ticket for ticket in called_alert if truth[ticket] == "alert"]
    precision = len(right) / len(called_alert)
    return Criterion(
        key="origin_precision",
        label="Origin precision on alert",
        target=f"at least {_percent(ALERT_PRECISION_TARGET)} on blind answers",
        verdict="pass" if precision >= ALERT_PRECISION_TARGET else "fail",
        value=_percent(precision),
        detail=(
            f"{len(right)} of {len(called_alert)} tickets the agent called alert were judged "
            f"alert, over {counted}"
        ),
    )


def grouping_criterion(judged: GroupJudgements) -> Criterion:
    if not judged.judged:
        return Criterion(
            key="grouping",
            label="Grouping spot-check",
            target=f"at least {_percent(GROUPING_TARGET)} of sampled groups correct",
            verdict="not_measured",
            detail=f"groups judged {judged.judged} of {judged.judged + judged.unjudged}",
        )
    share = judged.correct_share
    return Criterion(
        key="grouping",
        label="Grouping spot-check",
        target=f"at least {_percent(GROUPING_TARGET)} of sampled groups correct",
        verdict=(
            "not_measured" if share is None else ("pass" if share >= GROUPING_TARGET else "fail")
        ),
        value=_percent(share),
        detail=(
            f"{judged.same} of {judged.judged} judged groups are one thing; "
            f"{judged.partly} partly, {judged.not_same} not. A group somebody had to "
            f"carve up counts as wrong."
        ),
    )


@dataclass
class ForwardState:
    """What the forward shadow has done, for the gate and for the page."""

    live: bool = False
    runs: int = 0
    tickets: int = 0
    snapshots: int = 0
    wrong_site: int = 0
    wrong_site_tickets: list[str] = field(default_factory=list)
    no_device: int = 0
    first_at: dt.datetime | None = None
    last_at: dt.datetime | None = None
    not_assessed: dict[str, int] = field(default_factory=dict)
    per_day: dict[str, int] = field(default_factory=dict)

    @property
    def days_running(self) -> int:
        if self.first_at is None or self.last_at is None:
            return 0
        return (self.last_at.date() - self.first_at.date()).days + 1

    def as_json(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "runs": self.runs,
            "tickets": self.tickets,
            "snapshots": self.snapshots,
            "wrong_site": self.wrong_site,
            "wrong_site_tickets": self.wrong_site_tickets[:50],
            "no_device": self.no_device,
            "days_running": self.days_running,
            "first_at": self.first_at.isoformat() if self.first_at else None,
            "last_at": self.last_at.isoformat() if self.last_at else None,
            "not_assessed": self.not_assessed,
            "per_day": self.per_day,
        }


async def forward_state(session: AsyncSession, *, client_id: uuid.UUID, live: bool) -> ForwardState:
    """Every forward pass this client has had, as counts and ids."""
    state = ForwardState(live=live)
    runs = (
        (
            await session.execute(
                select(ShadowRun)
                .where(ShadowRun.client_id == client_id, ShadowRun.mode == "forward")
                .order_by(ShadowRun.started_at)
            )
        )
        .scalars()
        .all()
    )
    state.runs = len(runs)
    if not runs:
        return state
    state.first_at, state.last_at = runs[0].started_at, runs[-1].started_at
    state.tickets = sum(run.tickets_seen for run in runs)
    ids = [run.id for run in runs]

    per_day = (
        await session.execute(
            select(
                func.date(ShadowPrediction.created_at_vendor),
                func.count(),
            )
            .where(ShadowPrediction.run_id.in_(ids))
            .group_by(func.date(ShadowPrediction.created_at_vendor))
        )
    ).all()
    state.per_day = {str(day): count for day, count in per_day if day is not None}

    snapshots = (
        (await session.execute(select(ShadowEnrichment).where(ShadowEnrichment.run_id.in_(ids))))
        .scalars()
        .all()
    )
    state.snapshots = len(snapshots)
    for snapshot in snapshots:
        if snapshot.device_id is None:
            state.no_device += 1
        elif (
            snapshot.device_site_id is not None
            and snapshot.site_id is not None
            and snapshot.device_site_id != snapshot.site_id
        ):
            state.wrong_site += 1
            state.wrong_site_tickets.append(snapshot.ticket_id)
        for reason in (snapshot.not_assessed or {}).values():
            state.not_assessed[str(reason)] = state.not_assessed.get(str(reason), 0) + 1
    return state


def forward_criterion(state: ForwardState) -> Criterion:
    """A fortnight with no enrichment of the wrong customer's machine."""
    if not state.live and state.runs == 0:
        return Criterion(
            key="forward_shadow",
            label="Forward shadow",
            target=f"{FORWARD_DAYS_TARGET} days running with no wrong-site enrichment",
            verdict="not_measured",
            detail="the switch is off and no forward pass has run",
        )
    if state.days_running < FORWARD_DAYS_TARGET:
        return Criterion(
            key="forward_shadow",
            label="Forward shadow",
            target=f"{FORWARD_DAYS_TARGET} days running with no wrong-site enrichment",
            verdict="not_measured",
            value=f"{state.days_running} day(s)",
            detail=(
                f"{state.days_running} of {FORWARD_DAYS_TARGET} days, "
                f"{state.tickets} ticket(s) seen, {state.wrong_site} wrong-site "
                f"enrichment(s) so far"
            ),
        )
    return Criterion(
        key="forward_shadow",
        label="Forward shadow",
        target=f"{FORWARD_DAYS_TARGET} days running with no wrong-site enrichment",
        verdict="pass" if state.wrong_site == 0 else "fail",
        value=f"{state.days_running} day(s), {state.wrong_site} wrong-site",
        detail=(
            f"{state.tickets} ticket(s) over {state.days_running} days, "
            f"{state.snapshots} enrichment snapshot(s), "
            f"{state.wrong_site} of another customer's machine"
        ),
    )


#: What a field is called on the readiness rows, in the words the page uses.
FIELD_WORDS = {
    "issue_type": "Issue type",
    "origin": "Where it came from",
    "urgency": "Urgency",
    "repeat": "Repeat",
    "machine": "Which machine",
}


def readiness_rows(counts: LiveReviewCounts) -> list[Criterion]:
    """How often an engineer, having read a suggestion, thought it right.

    **Reported and not gated**, and the rows say so in their own target column.
    This is the Level 1 question — would an engineer have been helped or
    interrupted — and it is not the Level 0 question, which is whether the
    agent is right. An approval cannot answer the second: the engineer read the
    answer before they judged it.
    """
    rows: list[Criterion] = []
    for name, words in FIELD_WORDS.items():
        tally = counts.by_field.get(name)
        if tally is None or not tally.reviewed:
            rows.append(
                Criterion(
                    key=f"readiness_{name}",
                    label=f"{words}: marked correct",
                    target="reported, not gated",
                    verdict="not_measured",
                    detail="nothing reviewed yet",
                )
            )
            continue
        share = tally.share_correct
        rows.append(
            Criterion(
                key=f"readiness_{name}",
                label=f"{words}: marked correct",
                target="reported, not gated",
                # Never pass or fail: there is no bar here to clear, and
                # colouring approval like a gate row is how a figure that
                # measures agreement comes to be read as accuracy.
                verdict="not_measured",
                value=_percent(share),
                detail=(
                    f"{tally.correct} correct, {tally.changed} changed and {tally.wrong} wrong "
                    f"of {tally.reviewed} reviewed. Approval, not accuracy: the engineer had "
                    f"read the suggestion before they judged it."
                ),
            )
        )
    return rows


def build_gate(
    rows: list[Joined],
    *,
    rule: ThresholdRule,
    holdout_days: int,
    labels: OriginLabels,
    groups: GroupJudgements,
    forward: ForwardState,
    blind: BlindAnswers | None = None,
    suggestions: list[Any] | None = None,
    reviews: LiveReviewCounts | None = None,
) -> Gate:
    """The whole panel, in the order a reader works down it.

    The threshold is chosen on the replay's development period and the two
    accuracy criteria are read against **blind answers** — past-ticket labels
    and live quick checks together. Neither reads an approval, and neither
    reads a proxy.

    Origin and grouping are read over everything judged rather than over a
    holdout: nothing is tuned on a hand judgement, the samples are drawn across
    the window, and narrowing them would throw away most of what people
    actually did — the pilot's first reading was 6 labelled alert tickets
    instead of 19.
    """
    said = blind or BlindAnswers()
    answers = agent_answers(rows, suggestions)
    issue_type, floor = issue_type_criteria(
        rows, rule=rule, holdout_days=holdout_days, blind=said, answers=answers
    )
    return Gate(
        criteria=[
            *issue_type,
            origin_criterion(labels, said, answers),
            grouping_criterion(groups),
            forward_criterion(forward),
        ],
        readiness=readiness_rows(reviews or LiveReviewCounts()),
        chosen_threshold=floor,
        rule=rule.as_json(),
    )

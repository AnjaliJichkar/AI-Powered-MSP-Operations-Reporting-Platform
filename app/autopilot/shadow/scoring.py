"""Scoring a run against what actually happened.

The predictions are in `shadow_predictions`, the record is in `ticket_outcomes`,
and the join is the ticket id. Nothing here calls a model or a vendor: a score
is arithmetic over two tables, so it can be re-run as often as the question
changes without costing anything.

**A score is taken on a period, and the period matters more than the number.**
A label policy or a confidence threshold chosen by looking at the same tickets
it is then scored on is not a measurement, it is a memory. So the window is
split: the first stretch is where settings and thresholds are chosen
(`development`) and the last is scored and otherwise left alone (`holdout`).
The split is on **creation** time, because that is when triage happens.

**What each field is scored against, and how good that ground truth is:**

* `issue_type` — the stored final label. Good ground truth: somebody filed the
  ticket under it. Tickets the tenant left untyped are excluded from accuracy
  and reported separately.
* `urgency` — the stored final priority, mapped onto three bands. **Approximate
  ground truth**: priority can change after creation and we store only the
  final value. Reported, never gated.
* `origin` — no stored ground truth at all. The vendor's own `source` field
  would be it and the ledger does not keep it, so this reports two **proxies**,
  both labelled as such: a ticket nobody was ever assigned, and a ticket raised
  into an alert-shaped queue. Neither is a score; the labelled sheet is.
* grouping — no ground truth either. Counts out, and 30 groups exported.

**`unsure` is counted apart from right and wrong**, and never in the accuracy
denominator: an agent that declines is the behaviour the thresholds are being
chosen to produce. Which is why accuracy is always reported beside
**coverage** — the share of tickets answered at all. An agent that answers four
tickets perfectly is not better than one that answers four hundred well.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.autopilot.config import AutopilotConfig
from app.autopilot.shadow.replay import ALERT_QUEUES
from app.autopilot.shadow.schema import UNSURE
from app.models.outcome import TicketOutcome
from app.models.shadow import ShadowPrediction, ShadowRun

#: Which stretch of the window a score covers. `development` is where a policy
#: or a threshold may be chosen; `holdout` is what the gate is read from.
Period = Literal["all", "development", "holdout"]

#: The tenant's four priorities onto the three urgency bands. Critical and High
#: both read as "high" because the agent is shown a subject line, not an
#: agreement: the distinction between them is contractual, not textual.
PRIORITY_TO_URGENCY = {
    "Critical": "high",
    "High": "high",
    "Medium": "normal",
    "Low": "low",
}

#: Confidence bands for calibration, narrow at the top where a threshold would
#: be chosen.
BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 0.5),
    (0.5, 0.7),
    (0.7, 0.8),
    (0.8, 0.9),
    (0.9, 0.95),
    (0.95, 1.01),
)
#: The ladder a threshold is picked from. Accuracy and coverage at each.
FLOORS: tuple[float, ...] = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)
#: Title length bands, in words. A subject of three words is most of what this
#: tenant writes, and it is a different task from a sentence.
LENGTHS: tuple[tuple[str, int, int], ...] = (("<=3", 0, 3), ("4-6", 4, 6), ("7+", 7, 10_000))


def band_of(confidence: float | None) -> str:
    """The band a confidence falls in, named for a report."""
    if confidence is None:
        return "none"
    for low, high in BANDS:
        if low <= confidence < high:
            return f"{low:.2f}-{min(high, 1.0):.2f}"
    return "none"


def length_band(words: int) -> str:
    for name, low, high in LENGTHS:
        if low <= words <= high:
            return name
    return LENGTHS[-1][0]


@dataclass
class FieldScore:
    """One field's agreement with the record."""

    judged: int = 0
    """Rows with both a prediction and a ground truth to compare it to."""
    correct: int = 0
    unsure: int = 0
    """The agent declined. Neither right nor wrong."""
    excluded: int = 0
    """No ground truth (an untyped ticket), or the row is quarantined."""

    @property
    def answered(self) -> int:
        return self.judged - self.unsure

    @property
    def accuracy(self) -> float | None:
        """Over the rows it actually answered."""
        return self.correct / self.answered if self.answered else None

    @property
    def coverage(self) -> float | None:
        """The share of judgeable tickets it answered at all.

        Reported beside every accuracy, because an agent that answers four
        tickets perfectly is not better than one that answers four hundred
        well — and a threshold trades one for the other.
        """
        return self.answered / self.judged if self.judged else None

    def as_json(self) -> dict[str, Any]:
        return {
            "judged": self.judged,
            "answered": self.answered,
            "correct": self.correct,
            "unsure": self.unsure,
            "excluded": self.excluded,
            "accuracy": self.accuracy,
            "coverage": self.coverage,
        }


@dataclass(frozen=True, slots=True)
class Joined:
    """One prediction beside what the ticket turned out to be.

    Everything scoring needs, read once. The title is here as a **word count**
    and nothing else: the length is a fact about the task's difficulty, the
    words are the customer's.
    """

    ticket_id: str
    created_at: dt.datetime | None
    path: str
    quarantined: bool
    predicted: str | None
    """After the franchisee's label policy."""
    raw: str | None
    """What the model said before it."""
    policy: str | None
    confidence: float | None
    actual: str | None
    urgency: str | None
    urgency_expected: str | None
    urgency_confidence: float | None
    origin: str | None
    title_words: int
    queue_label: str | None
    unassigned: bool
    grouped: bool
    has_description: bool | None


async def joined_rows(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    client_id: uuid.UUID,
    policy: AutopilotConfig | None = None,
) -> list[Joined]:
    """Every prediction of a run, with the record beside it.

    `policy` re-derives each answer from the **raw** label the model gave,
    which is how a candidate label policy is scored on a run that did not use
    one: one set of model answers, two periods, and as many candidate policies
    as the question needs. The stored rows are untouched — a run records what
    it ran as, a score records what it was scored under.
    """
    predictions = (
        (
            await session.execute(
                select(ShadowPrediction)
                .where(ShadowPrediction.run_id == run_id)
                .order_by(ShadowPrediction.created_at_vendor)
            )
        )
        .scalars()
        .all()
    )
    truth = {
        row.ticket_id: row
        for row in (
            await session.execute(select(TicketOutcome).where(TicketOutcome.client_id == client_id))
        )
        .scalars()
        .all()
    }
    rows: list[Joined] = []
    for prediction in predictions:
        actual = truth.get(prediction.ticket_id)
        title = (actual.title or "") if actual else ""
        predicted, verdict = prediction.issue_type, prediction.issue_type_policy
        if policy is not None:
            predicted, verdict = policy.apply(prediction.issue_type_raw)
            if prediction.issue_type_raw is None:
                verdict = None
        rows.append(
            Joined(
                ticket_id=prediction.ticket_id,
                created_at=prediction.created_at_vendor,
                path=prediction.path,
                quarantined=prediction.title_quarantined,
                predicted=predicted,
                raw=prediction.issue_type_raw,
                policy=verdict,
                confidence=(
                    float(prediction.issue_type_confidence)
                    if prediction.issue_type_confidence is not None
                    else None
                ),
                actual=actual.issue_type_label if actual else None,
                urgency=prediction.urgency,
                urgency_expected=(
                    PRIORITY_TO_URGENCY.get(actual.priority_label or "") if actual else None
                ),
                urgency_confidence=(
                    float(prediction.urgency_confidence)
                    if prediction.urgency_confidence is not None
                    else None
                ),
                origin=prediction.origin,
                title_words=len(title.split()),
                # The forward shadow records the queue at arrival; a replay
                # cannot, so the ledger's completion-time value stands in and
                # is used only as an explicitly-labelled proxy.
                queue_label=prediction.queue_label_at_arrival
                or (actual.queue_label if actual else None),
                unassigned=bool(actual and actual.resolver_name is None),
                grouped=prediction.group_with is not None,
                has_description=prediction.has_description,
            )
        )
    return rows


def split(rows: list[Joined], *, period: Period, holdout_days: int) -> list[Joined]:
    """The stretch of the window a score covers.

    Split on creation, because triage happens when a ticket arrives — and from
    the newest ticket backwards, so "the last thirty days" means the last
    thirty days of the data rather than of the calendar.
    """
    if period == "all":
        return rows
    dated = [row for row in rows if row.created_at is not None]
    if not dated:
        return []
    newest = max(row.created_at for row in dated if row.created_at)
    boundary = newest - dt.timedelta(days=holdout_days)
    if period == "holdout":
        return [row for row in dated if row.created_at and row.created_at > boundary]
    return [row for row in dated if row.created_at and row.created_at <= boundary]


def _one(bucket: FieldScore, *, predicted: str | None, actual: str | None) -> None:
    if actual is None or predicted is None:
        bucket.excluded += 1
        return
    bucket.judged += 1
    if predicted == UNSURE:
        bucket.unsure += 1
        return
    if predicted == actual:
        bucket.correct += 1


def _score_into(
    scores: dict[str, FieldScore], key: str, *, predicted: str | None, actual: str | None
) -> None:
    _one(scores.setdefault(key, FieldScore()), predicted=predicted, actual=actual)


def issue_type_score(rows: list[Joined], *, use_raw: bool = False) -> FieldScore:
    """Accuracy over the rows that can be judged at all.

    `use_raw` scores what the model said *before* the franchisee's label
    policy, which is how the policy's worth is measured on the same run rather
    than by comparing two.
    """
    score = FieldScore()
    for row in rows:
        if row.quarantined:
            score.excluded += 1
            continue
        _one(score, predicted=row.raw if use_raw else row.predicted, actual=row.actual)
    return score


def at_threshold(rows: list[Joined], floor: float, *, use_raw: bool = False) -> FieldScore:
    """Accuracy and coverage for the rows at or above a confidence.

    Below the floor the agent is treated as having declined, which is what a
    threshold *is*: `judged` stays the whole judgeable population so coverage
    means what it says.
    """
    score = FieldScore()
    for row in rows:
        if row.quarantined:
            score.excluded += 1
            continue
        if row.actual is None:
            score.excluded += 1
            continue
        predicted = row.raw if use_raw else row.predicted
        score.judged += 1
        if predicted is None or predicted == UNSURE or (row.confidence or 0.0) < floor:
            score.unsure += 1
            continue
        if predicted == row.actual:
            score.correct += 1
    return score


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """How a confidence threshold is chosen, decided **before** a run.

    The lowest development-period threshold that clears both bars. Fixed in
    advance because the rule changes the verdict: on 2026-10-01 "the lowest
    threshold meeting the gate" failed the holdout at 82.2% while "the lowest
    with a margin" would have passed it at 85.3%, and choosing between those
    after seeing them is choosing the answer. The accuracy bar sits above the
    gate's own 85% for the same reason — the holdout came in 3.6 points under
    development at the same threshold, and a rule with no room for that gap
    passes on development and fails in use.
    """

    min_accuracy: float
    min_coverage: float

    @classmethod
    def from_settings(cls, settings: Any) -> ThresholdRule:
        return cls(
            min_accuracy=settings.autopilot_threshold_min_accuracy,
            min_coverage=settings.autopilot_threshold_min_coverage,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "rule": "lowest development threshold meeting both bars",
            "min_accuracy": self.min_accuracy,
            "min_coverage": self.min_coverage,
        }

    def choose(self, rows: list[Joined], floors: tuple[float, ...] = FLOORS) -> float | None:
        """The threshold this rule picks, or `None` when nothing clears both.

        `rows` are the **development** period's. Handing it the holdout would
        be the mistake the rule exists to prevent, and is why the caller names
        the period rather than this function guessing it.
        """
        for floor in sorted(floors):
            score = at_threshold(rows, floor)
            if score.accuracy is None or score.coverage is None:
                continue
            if score.accuracy >= self.min_accuracy and score.coverage >= self.min_coverage:
                return floor
        return None


@dataclass
class Scorecard:
    """Everything one run can be judged on over one period, counts only."""

    run_id: uuid.UUID
    period: Period = "all"
    scored_under_policy: bool = False
    """The label policy was applied at scoring time rather than at prediction
    time. Said on the card, because a number taken under a candidate policy is
    otherwise indistinguishable from one the run itself produced."""
    tickets: int = 0
    quarantined: int = 0
    errors: int = 0

    issue_type: FieldScore = field(default_factory=FieldScore)
    issue_type_raw: FieldScore = field(default_factory=FieldScore)
    """The same, before the franchisee's label policy."""
    urgency: FieldScore = field(default_factory=FieldScore)

    thresholds: dict[str, FieldScore] = field(default_factory=dict)
    thresholds_raw: dict[str, FieldScore] = field(default_factory=dict)
    by_issue_type: dict[str, FieldScore] = field(default_factory=dict)
    by_issue_type_raw: dict[str, FieldScore] = field(default_factory=dict)
    by_title_length: dict[str, FieldScore] = field(default_factory=dict)
    by_path: dict[str, FieldScore] = field(default_factory=dict)
    calibration: dict[str, FieldScore] = field(default_factory=dict)
    urgency_calibration: dict[str, FieldScore] = field(default_factory=dict)

    untyped_predictions: Counter[str] = field(default_factory=Counter)
    confusion: dict[str, Counter[str]] = field(default_factory=dict)
    policy_counts: Counter[str] = field(default_factory=Counter)
    rule_names: Counter[str] = field(default_factory=Counter)

    # --- origin: two proxies, and neither is a score ------------------------
    origin_counts: Counter[str] = field(default_factory=Counter)
    proxy_unassigned: int = 0
    proxy_unassigned_alert: int = 0
    proxy_assigned: int = 0
    proxy_assigned_human: int = 0
    proxy_alert_queue: int = 0
    proxy_alert_queue_alert: int = 0
    proxy_other_queue: int = 0
    proxy_other_queue_human: int = 0

    # --- grouping -----------------------------------------------------------
    grouped: int = 0
    group_leaders: int = 0
    grouped_by_issue_type: Counter[str] = field(default_factory=Counter)
    tickets_by_issue_type: Counter[str] = field(default_factory=Counter)
    alert_origin: int = 0
    alert_origin_grouped: int = 0

    # --- what it cost -------------------------------------------------------
    model_calls: int = 0
    latency_total_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float | None = None
    with_description: int = 0

    confident_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def latency_per_model_call_ms(self) -> float | None:
        return self.latency_total_ms / self.model_calls if self.model_calls else None

    def as_json(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "period": self.period,
            "scored_under_policy": self.scored_under_policy,
            "tickets": self.tickets,
            "quarantined": self.quarantined,
            "errors": self.errors,
            "issue_type": self.issue_type.as_json(),
            "issue_type_before_policy": self.issue_type_raw.as_json(),
            "urgency": self.urgency.as_json(),
            "thresholds": {
                floor: score.as_json() for floor, score in sorted(self.thresholds.items())
            },
            "thresholds_before_policy": {
                floor: score.as_json() for floor, score in sorted(self.thresholds_raw.items())
            },
            "by_issue_type": {
                name: score.as_json() for name, score in sorted(self.by_issue_type.items())
            },
            "by_issue_type_before_policy": {
                name: score.as_json() for name, score in sorted(self.by_issue_type_raw.items())
            },
            "by_title_length": {
                name: score.as_json() for name, score in sorted(self.by_title_length.items())
            },
            "by_path": {name: score.as_json() for name, score in sorted(self.by_path.items())},
            "calibration": {
                band: score.as_json() for band, score in sorted(self.calibration.items())
            },
            "urgency_calibration": {
                band: score.as_json() for band, score in sorted(self.urgency_calibration.items())
            },
            "untyped_predictions": dict(self.untyped_predictions.most_common()),
            "confusion": {
                predicted: dict(actuals.most_common())
                for predicted, actuals in sorted(self.confusion.items())
            },
            "label_policy_applied": dict(self.policy_counts.most_common()),
            "rules": dict(self.rule_names.most_common()),
            "origin": {
                "predicted": dict(self.origin_counts.most_common()),
                "PROXY_no_assignee": {
                    "unassigned": self.proxy_unassigned,
                    "called_alert": self.proxy_unassigned_alert,
                    "assigned": self.proxy_assigned,
                    "called_human": self.proxy_assigned_human,
                },
                "PROXY_alert_queue": {
                    "alert_queue": self.proxy_alert_queue,
                    "called_alert": self.proxy_alert_queue_alert,
                    "other_queue": self.proxy_other_queue,
                    "called_human": self.proxy_other_queue_human,
                },
            },
            "grouping": {
                "tickets": self.tickets,
                "groups": self.group_leaders,
                "joined": self.grouped,
                "by_issue_type": {
                    name: {
                        "tickets": self.tickets_by_issue_type[name],
                        "joined": self.grouped_by_issue_type[name],
                    }
                    for name in sorted(self.tickets_by_issue_type)
                },
                "alert_origin": self.alert_origin,
                "alert_origin_joined": self.alert_origin_grouped,
            },
            "tokens": {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "cache_read": self.cache_read_tokens,
            },
            "cost": self.cost,
            "latency_per_model_call_ms": self.latency_per_model_call_ms,
            "with_description": self.with_description,
            "confident_errors": self.confident_errors,
        }


def score_rows(rows: list[Joined], run_id: uuid.UUID, *, period: Period = "all") -> Scorecard:
    """Everything a period's rows can say. No database, so it is testable."""
    card = Scorecard(run_id=run_id, period=period, tickets=len(rows))
    confusion: dict[str, Counter[str]] = defaultdict(Counter)

    card.issue_type = issue_type_score(rows)
    card.issue_type_raw = issue_type_score(rows, use_raw=True)
    for floor in FLOORS:
        card.thresholds[f"{floor:.2f}"] = at_threshold(rows, floor)
        card.thresholds_raw[f"{floor:.2f}"] = at_threshold(rows, floor, use_raw=True)

    for row in rows:
        if row.quarantined:
            card.quarantined += 1
        if row.path == "error":
            card.errors += 1
        if row.policy:
            card.policy_counts[row.policy] += 1
        if row.origin:
            card.origin_counts[row.origin] += 1
        if row.has_description:
            card.with_description += 1

        kind = row.actual or "(not set)"
        card.tickets_by_issue_type[kind] += 1
        if row.grouped:
            card.grouped += 1
            card.grouped_by_issue_type[kind] += 1
        else:
            card.group_leaders += 1
        if row.origin == "alert":
            card.alert_origin += 1
            if row.grouped:
                card.alert_origin_grouped += 1

        if row.quarantined:
            continue

        # --- the two origin proxies, both labelled as proxies --------------
        if row.origin:
            if row.unassigned:
                card.proxy_unassigned += 1
                card.proxy_unassigned_alert += row.origin == "alert"
            else:
                card.proxy_assigned += 1
                card.proxy_assigned_human += row.origin == "human"
            if (row.queue_label or "").strip().lower() in ALERT_QUEUES:
                card.proxy_alert_queue += 1
                card.proxy_alert_queue_alert += row.origin == "alert"
            elif row.queue_label:
                card.proxy_other_queue += 1
                card.proxy_other_queue_human += row.origin == "human"

        # --- urgency, against a priority that may have moved ---------------
        _one(card.urgency, predicted=row.urgency, actual=row.urgency_expected)
        _score_into(
            card.urgency_calibration,
            band_of(row.urgency_confidence),
            predicted=row.urgency,
            actual=row.urgency_expected,
        )

        if row.actual is None:
            card.untyped_predictions[row.predicted or "none"] += 1
            continue

        _score_into(card.by_issue_type, row.actual, predicted=row.predicted, actual=row.actual)
        _score_into(card.by_issue_type_raw, row.actual, predicted=row.raw, actual=row.actual)
        _score_into(
            card.by_title_length,
            length_band(row.title_words),
            predicted=row.predicted,
            actual=row.actual,
        )
        _score_into(card.by_path, row.path, predicted=row.predicted, actual=row.actual)
        _score_into(
            card.calibration, band_of(row.confidence), predicted=row.predicted, actual=row.actual
        )
        if row.predicted and row.predicted != UNSURE:
            confusion[row.predicted][row.actual] += 1
            if row.predicted != row.actual and row.confidence is not None:
                card.confident_errors.append(
                    {
                        "ticket_id": row.ticket_id,
                        "confidence": row.confidence,
                        "predicted": row.predicted,
                        "actual": row.actual,
                        "path": row.path,
                    }
                )

    card.confusion = dict(confusion)
    card.confident_errors.sort(key=lambda row: -float(row["confidence"]))
    card.confident_errors = card.confident_errors[:10]
    return card


async def score_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    client_id: uuid.UUID,
    period: Period = "all",
    holdout_days: int = 30,
    policy: AutopilotConfig | None = None,
) -> Scorecard:
    """Join one run's predictions to the record and count, over one period."""
    run = await session.get(ShadowRun, run_id)
    if run is None or run.client_id != client_id:
        raise ValueError(f"no shadow run {run_id} for this client")
    rows = await joined_rows(session, run_id, client_id=client_id, policy=policy)
    card = score_rows(split(rows, period=period, holdout_days=holdout_days), run_id, period=period)
    card.scored_under_policy = policy is not None
    # The run's totals are the whole run's, whatever period is scored, and are
    # labelled as such rather than apportioned by a share nobody measured.
    card.input_tokens = run.input_tokens
    card.output_tokens = run.output_tokens
    card.cache_read_tokens = run.cache_read_tokens
    card.cost = float(run.cost) if run.cost is not None else None
    predictions = (
        (await session.execute(select(ShadowPrediction).where(ShadowPrediction.run_id == run_id)))
        .scalars()
        .all()
    )
    for prediction in predictions:
        if prediction.rule_name:
            card.rule_names[prediction.rule_name] += 1
        if prediction.path == "model" and prediction.latency_ms is not None:
            card.model_calls += 1
            card.latency_total_ms += prediction.latency_ms
    return card


def lift(baseline: Scorecard, variant: Scorecard) -> dict[str, Any]:
    """What the variant's extra input bought, field by field.

    A difference of accuracy *and* of coverage: an input that makes the agent
    bolder looks like a win on accuracy alone.
    """

    def pair(left: FieldScore, right: FieldScore) -> dict[str, Any]:
        return {
            "baseline_accuracy": left.accuracy,
            "variant_accuracy": right.accuracy,
            "accuracy_lift": (
                right.accuracy - left.accuracy
                if left.accuracy is not None and right.accuracy is not None
                else None
            ),
            "baseline_coverage": left.coverage,
            "variant_coverage": right.coverage,
            "coverage_lift": (
                right.coverage - left.coverage
                if left.coverage is not None and right.coverage is not None
                else None
            ),
            "baseline_answered": left.answered,
            "variant_answered": right.answered,
        }

    return {
        "period": baseline.period,
        "issue_type": pair(baseline.issue_type, variant.issue_type),
        "urgency": pair(baseline.urgency, variant.urgency),
        "origin_alert_called": {
            "baseline": baseline.origin_counts.get("alert", 0),
            "variant": variant.origin_counts.get("alert", 0),
        },
        "origin_unsure": {
            "baseline": baseline.origin_counts.get("unsure", 0),
            "variant": variant.origin_counts.get("unsure", 0),
        },
    }


def projected_monthly(
    card: Scorecard, *, days: int, tickets: int | None = None, franchisees: int = 1
) -> float | None:
    """What a month of this would cost, at the volume just replayed.

    `None` when the run was unpriced, which is the honest answer rather than a
    zero. The arithmetic is deliberately plain, and `tickets` is the whole
    run's count rather than a scored period's: the cost was paid on all of them.
    """
    counted = tickets if tickets is not None else card.tickets
    if card.cost is None or not counted or not days:
        return None
    per_ticket = card.cost / counted
    per_month = counted / days * 30
    return per_ticket * per_month * franchisees

"""The scorecard: a rule-computed status per area, from `scorecard.toml` and the facts.

Everything that decides a status is in the TOML file beside this module, which
also documents the rules. This module only applies them. It reads facts, never
a vendor, and the model has no part in it: a status is a judgement about the
customer, and judgements are made by rules a person can read (CLAUDE.md,
"Findings are decided by rules").
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.reports.tbr.model import (
    FactSet,
    ScorecardMetric,
    ScorecardRow,
    ScoreStatus,
    ScoreTrend,
)

RULES_FILE = Path(__file__).with_name("scorecard.toml")

#: Worst first. An area takes the first of these that any of its metrics has.
WORST_FIRST: tuple[ScoreStatus, ...] = ("action", "attention", "good")

STATUS_LABELS: dict[ScoreStatus, str] = {
    "good": "Good",
    "attention": "Attention",
    "action": "Action",
    "not_assessed": "Not yet assessed",
    "no_data": "No data",
}


class MetricRule(BaseModel):
    fact: str
    label: str
    better: Literal["higher", "lower"]
    good: float
    action: float
    per: str | None = None
    unit: str = ""
    tolerance: float = Field(default=1.0, ge=0)

    @model_validator(mode="after")
    def _thresholds_are_ordered(self) -> MetricRule:
        ordered = self.good > self.action if self.better == "higher" else self.good < self.action
        if not ordered:
            raise ValueError(
                f"{self.fact}: with better={self.better!r}, `good` ({self.good:g}) must be "
                f"{'above' if self.better == 'higher' else 'below'} `action` ({self.action:g})"
            )
        return self

    def status(self, value: float) -> ScoreStatus:
        if self.better == "higher":
            return (
                "good" if value >= self.good else "action" if value < self.action else "attention"
            )
        return "good" if value <= self.good else "action" if value > self.action else "attention"

    def rule_text(self) -> str:
        """The thresholds in words, as the review screen and the internal notes show them."""
        if self.better == "higher":
            return f"good at {self.good:g} or more, action below {self.action:g}"
        return f"good at {self.good:g} or less, action above {self.action:g}"


class AreaRule(BaseModel):
    key: str
    label: str
    assessed: bool = True
    waiting_for: str | None = None
    metric: list[MetricRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_what_it_needs(self) -> AreaRule:
        if self.assessed and not self.metric:
            raise ValueError(f"area {self.key!r} is assessed but has no metric")
        if not self.assessed and not self.waiting_for:
            raise ValueError(
                f"area {self.key!r} is not assessed and does not say what it waits for"
            )
        return self


class ScorecardRules(BaseModel):
    area: list[AreaRule]


@lru_cache(maxsize=4)
def load_rules(path: Path = RULES_FILE) -> ScorecardRules:
    with path.open("rb") as handle:
        return ScorecardRules.model_validate(tomllib.load(handle))


def _value(rule: MetricRule, facts: FactSet | None) -> float | None:
    """The number the rule scores: the fact, or the fact per hundred of its denominator."""
    if facts is None:
        return None
    value = facts.number(rule.fact)
    if value is None:
        return None
    if rule.per is None:
        return value
    whole = facts.number(rule.per)
    if not whole:
        return None
    return round(100 * value / whole, 1)


def _movement(rule: MetricRule, now: float, before: float) -> int:
    """+1 improved, -1 declined, 0 steady: in the direction that is better for this metric."""
    change = now - before if rule.better == "higher" else before - now
    if abs(change) < rule.tolerance:
        return 0
    return 1 if change > 0 else -1


def evaluate(
    facts: FactSet, prior: FactSet | None = None, *, rules: ScorecardRules | None = None
) -> list[ScorecardRow]:
    """One row per area in the rules file, in its order."""
    rows: list[ScorecardRow] = []
    for area in (rules or load_rules()).area:
        if not area.assessed:
            rows.append(
                ScorecardRow(
                    key=area.key,
                    label=area.label,
                    status="not_assessed",
                    status_label=STATUS_LABELS["not_assessed"],
                    reason=f"Waiting for {area.waiting_for}.",
                )
            )
            continue
        metrics: list[ScorecardMetric] = []
        movements: list[int] = []
        for rule in area.metric:
            value = _value(rule, facts)
            if value is None:
                continue
            before = _value(rule, prior)
            if before is not None:
                movements.append(_movement(rule, value, before))
            metrics.append(
                ScorecardMetric(
                    fact_id=rule.fact,
                    per_fact_id=rule.per,
                    label=rule.label,
                    value=value,
                    display=(
                        f"{value:g}{rule.unit}"
                        if rule.unit.startswith("%")
                        else f"{value:g} {rule.unit}".strip()
                    ),
                    unit=rule.unit,
                    status=rule.status(value),
                    rule=rule.rule_text(),
                    prior_value=before,
                )
            )
        if not metrics:
            rows.append(
                ScorecardRow(
                    key=area.key,
                    label=area.label,
                    status="no_data",
                    status_label=STATUS_LABELS["no_data"],
                    reason="None of this area's facts could be collected, so it is not scored.",
                )
            )
            continue
        status = next(s for s in WORST_FIRST if any(m.status == s for m in metrics))
        deciding = [m for m in metrics if m.status == status]
        trend: ScoreTrend | None = None
        if movements:
            total = sum(movements)
            trend = "up" if total > 0 else "down" if total < 0 else "steady"
        rows.append(
            ScorecardRow(
                key=area.key,
                label=area.label,
                status=status,
                status_label=STATUS_LABELS[status],
                trend=trend,
                metrics=metrics,
                reason="; ".join(f"{m.label}: {m.display} ({m.rule})" for m in deciding),
            )
        )
    return rows

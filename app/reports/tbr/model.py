"""The data a business review is made of: facts, findings, sections, the review document.

Everything here is plain JSON-serialisable Pydantic, because the whole document
is stored (`tbr_reports.document`) and read back for review, for re-rendering,
and as the *prior snapshot* the next period's report is compared against.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Source = Literal[
    "ninjaone",
    "autotask",
    "bitdefender",
    "connectsecure",
    "cove",
    "config",
    "engineer",
    "web",
    "derived",
]

#: Which set of records a fact counted. Two facts of different populations are
#: different things and must never share a sentence: ConnectSecure reports a
#: finding its scanner proved (`confirmed`) and one it inferred and has not
#: verified (`unconfirmed`), and the two totals differ by more than half. A
#: fact that is not about vulnerability findings is `universal`: it fits
#: beside either.
Population = Literal["universal", "confirmed", "unconfirmed"]

#: The severity band a fact counts. `all` means every band together; `None`
#: means the fact is not a count of findings at all (a date, a device count, a
#: number of updates), so it fits beside any of them. Two facts of different
#: bands are different things: "118 critical findings" and "4,409 suspected
#: findings" read as the same kind of number and are not, which is how a
#: caveat counting every severity came to sit under a critical headline.
SeverityScope = Literal["all", "critical", "high", "medium", "low"]

#: Most to least serious. Ordering and the quarter a recommendation lands in
#: both come from this, in code.
Severity = Literal["critical", "high", "medium", "low"]
SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class ReviewPeriod(BaseModel):
    """The months a review covers. `end` is exclusive: August 2026 ends 2026-09-01."""

    start: dt.datetime
    end: dt.datetime
    months: int
    label: str = Field(description='e.g. "August 2026" or "March to August 2026".')

    @classmethod
    def ending(cls, year: int, month: int, months: int = 1) -> ReviewPeriod:
        if months < 1:
            raise ValueError("a review period is at least one month")
        if not 1 <= month <= 12:
            raise ValueError("month must be between 1 and 12")
        end = dt.datetime(year + month // 12, month % 12 + 1, 1, tzinfo=dt.UTC)
        first_index = (year * 12 + month - 1) - (months - 1)
        start = dt.datetime(first_index // 12, first_index % 12 + 1, 1, tzinfo=dt.UTC)
        last = f"{dt.date(year, month, 1):%B %Y}"
        label = (
            last
            if months == 1
            else f"{start:%B} to {last}"
            if start.year == year
            else (f"{start:%B %Y} to {last}")
        )
        return cls(start=start, end=end, months=months, label=label)

    def previous(self) -> ReviewPeriod:
        """The period of the same length immediately before this one."""
        last = self.start - dt.timedelta(days=1)
        return ReviewPeriod.ending(last.year, last.month, self.months)

    @property
    def key(self) -> str:
        """Stable identity for storage: `2026-08/1`."""
        last = self.end - dt.timedelta(days=1)
        return f"{last:%Y-%m}/{self.months}"


# --- facts ----------------------------------------------------------------------


class Fact(BaseModel):
    """One measured value. The only place a number in the report can come from."""

    id: str = Field(description="Stable across periods, e.g. `ninjaone.devices.workstations`.")
    label: str
    value: int | float | str | None
    display: str = Field(description="The value as it is printed. Formatted by code.")
    definition: str = Field(description="Exactly what was counted, and how.")
    source: Source
    as_of: dt.datetime
    period_bound: bool = Field(
        default=False,
        description="True if it covers the review period; False if it is the state at `as_of`.",
    )
    population: Population = Field(
        default="universal",
        description="The set of records counted. A sentence may not mix two of them.",
    )
    severity: SeverityScope | None = Field(
        default=None,
        description="The severity band counted. A sentence may not mix two of them.",
    )
    denominator: str | None = Field(
        default=None,
        description=(
            "For a share, the id of the fact it is a share of. A percentage must name one, "
            "and the sentence that prints the percentage must print it too."
        ),
    )
    internal_only: bool = Field(
        default=False,
        description="Shown to the engineer and never rendered to the customer.",
    )

    @property
    def is_share(self) -> bool:
        """True when the value is a percentage of something, by its printed form."""
        return self.display.rstrip().endswith("%")

    @property
    def scope(self) -> tuple[str, str | None]:
        """What this counted, beyond the number: the population and the severity band."""
        return (self.population, self.severity)


class FactTable(BaseModel):
    """Rows that go on a slide as they are: devices, projects. Every cell is code-written."""

    id: str
    label: str
    definition: str
    source: Source
    columns: list[str]
    rows: list[list[str]]
    total: int = Field(description="Rows before any cap the slide applies.")
    as_of: dt.datetime
    population: Population = "universal"
    severity: SeverityScope | None = None
    denominator: str | None = None
    internal_only: bool = False

    @property
    def is_share(self) -> bool:
        return False

    @property
    def scope(self) -> tuple[str, str | None]:
        return (self.population, self.severity)


class ChartSeries(BaseModel):
    name: str
    values: list[float]


class ChartData(BaseModel):
    id: str
    title: str
    kind: Literal["line", "column", "bar", "pie"]
    categories: list[str]
    series: list[ChartSeries]
    definition: str
    source: Source
    number_format: str = "General"


class Gap(BaseModel):
    """Something the report wanted and could not get. Reported, never papered over."""

    what: str
    why: str
    source: Source


class FactSet(BaseModel):
    facts: dict[str, Fact] = Field(default_factory=dict)
    tables: dict[str, FactTable] = Field(default_factory=dict)
    charts: dict[str, ChartData] = Field(default_factory=dict)
    gaps: list[Gap] = Field(default_factory=list)
    names: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Every real name the collectors saw, by kind (`devices`, `contacts`), whether "
            "or not it reached a slide. What a demo render must never print."
        ),
    )

    groups: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Named sets of device names the cross-tool rules read: which devices NinjaOne "
            "patches, which BitLocker covers, which Bitdefender modules are off on. Names, "
            "so a demo render replaces them."
        ),
    )

    def add(self, fact: Fact) -> Fact:
        self.facts[fact.id] = fact
        return fact

    def get(self, fact_id: str) -> Fact | None:
        return self.facts.get(fact_id)

    def number(self, fact_id: str) -> float | None:
        fact = self.facts.get(fact_id)
        if fact is None or isinstance(fact.value, str) or fact.value is None:
            return None
        return float(fact.value)

    def ids(self) -> set[str]:
        return set(self.facts) | set(self.tables) | set(self.charts)


# --- findings -------------------------------------------------------------------


class Finding(BaseModel):
    """A conclusion a rule drew from facts. Phrased by the model, decided by code."""

    id: str
    section: str = Field(description="The section it belongs to first, e.g. `cybersecurity`.")
    kind: Literal["measurement", "gap", "pattern", "improvement", "regression"]
    severity: Severity
    headline: str = Field(description="Code-written, plain words. The fallback if no model runs.")
    detail: str = Field(description="Code-written sentence using fact tokens like {{fact.id}}.")
    recommendation: str | None = Field(
        default=None, description="Code-written. What the franchisee would propose."
    )
    fact_ids: list[str]
    sources: list[Source]
    corroborated: bool = Field(
        description="True when two or more tools support it. A `gap` must be, to be asserted."
    )
    needs_confirmation: bool = Field(
        default=False,
        description="An uncorroborated gap: shown to the engineer, rendered only if confirmed.",
    )
    confirmed_by: str | None = None
    service_key: str | None = Field(default=None, description="Service catalog entry it backs.")
    recommendation_type: str | None = Field(default=None, description="Price book key.")

    @property
    def assertable(self) -> bool:
        return not self.needs_confirmation or self.confirmed_by is not None


# --- sections and the review document ------------------------------------------------

SlideStatus = Literal["filled", "awaiting_engineer", "dropped", "deferred", "static"]
Author = Literal["model", "code", "engineer"]


class Item(BaseModel):
    """One paragraph on a slide, with where every part of it came from."""

    role: Literal[
        "bullet", "sub_bullet", "paragraph", "headline", "observation", "recommendation"
    ] = "bullet"
    lead: str | None = Field(default=None, description="Bold lead-in, e.g. `Recommendation:`.")
    text: str = Field(description="Final text, values substituted. What is rendered.")
    template: str | None = Field(
        default=None, description="As the model wrote it, with {{fact.id}} tokens."
    )
    cites: list[str] = Field(description="Fact, table, chart or finding ids this rests on.")
    author: Author
    url: str | None = Field(default=None, description="Source URL, for a web-researched item.")
    slot: str | None = Field(default=None, description="The brief slot this text filled.")
    cells: list[str] = Field(
        default_factory=list, description="For a table row: the cells, left to right."
    )


ScoreStatus = Literal["good", "attention", "action", "not_assessed", "no_data"]
ScoreTrend = Literal["up", "down", "steady"]


class ScorecardMetric(BaseModel):
    """One scored figure behind an area's status, with the rule that scored it."""

    fact_id: str
    per_fact_id: str | None = Field(
        default=None, description="Denominator fact: the value is then a percentage of it."
    )
    label: str
    value: float
    display: str
    unit: str = ""
    status: ScoreStatus
    rule: str = Field(description="The thresholds, in words, from scorecard.toml.")
    prior_value: float | None = Field(
        default=None, description="The same figure in the previous stored review, if any."
    )


class ScorecardRow(BaseModel):
    """One area of the scorecard. Computed by rules in code; never written by the model."""

    key: str
    label: str
    status: ScoreStatus
    status_label: str
    trend: ScoreTrend | None = Field(
        default=None,
        description="Against the previous stored review. None when there is none to compare. "
        "`up` always means better.",
    )
    metrics: list[ScorecardMetric] = Field(default_factory=list)
    reason: str = Field(description="The metric(s) that decided the status, or why there is none.")

    def cites(self) -> list[str]:
        ids = [i for m in self.metrics for i in (m.fact_id, m.per_fact_id) if i]
        return list(dict.fromkeys(ids))


class Section(BaseModel):
    key: str
    slide: int
    title: str
    status: SlideStatus
    reason: str = Field(description="Why it has that status, in a sentence.")
    narrative: Literal["model", "fallback", "code", "static", "none"] = "none"
    requires_approval: bool = False
    approved_by: str | None = None
    approved_at: dt.datetime | None = None
    items: list[Item] = Field(default_factory=list)
    engineer_items: list[Item] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    waiting_for: str | None = Field(
        default=None, description="What the engineer would need to supply or approve."
    )
    later_connector: str | None = Field(default=None, description="For a deferred slide.")
    talk_track: str | None = Field(
        default=None,
        description=(
            "Speaker notes an engineer wrote for this slide. The only thing a client "
            "deck's notes may hold."
        ),
    )
    talk_track_by: str | None = None
    problems: list[str] = Field(
        default_factory=list, description="Why a model draft was rejected, if one was."
    )
    data: dict[str, str] = Field(
        default_factory=dict,
        description="Named values a slide prints in fixed places, e.g. the CX scores.",
    )
    scorecard: list[ScorecardRow] = Field(
        default_factory=list, description="Key Takeaways only: the rule-computed scorecard."
    )

    @property
    def renderable(self) -> bool:
        """On the deck only when it is filled and, where approval is required, approved.

        A section the engineer supplies is `awaiting_engineer` until they do, and
        the review step moves it to `filled`; nothing else puts it on a slide.
        """
        if self.status == "static":
            return True
        if self.status != "filled":
            return False
        return not self.requires_approval or self.approved_by is not None

    def all_items(self) -> list[Item]:
        return [*self.items, *self.engineer_items]


class ReviewDocument(BaseModel):
    """One client's review for one period: everything needed to review and render it."""

    client_id: str
    client_slug: str
    client_name: str
    site_id: str
    site_slug: str
    site_name: str
    period: ReviewPeriod
    generated_at: dt.datetime
    generated_by: str
    model: str | None = None
    franchisee: dict[str, object] = Field(description="The config snapshot it was built with.")
    facts: FactSet
    prior_facts: FactSet | None = None
    prior_period_label: str | None = None
    findings: list[Finding]
    sections: list[Section]
    show_contact_names: bool | None = Field(
        default=None,
        description=(
            "An engineer's override, for this review, of the franchisee's `show_people_names` "
            "setting: True prints the customer's people by name on the tickets-by-contact "
            "chart, the lifecycle tables and a person-named location; False hides them; None "
            "follows the franchisee's default. A deck is passed around, and who raises the "
            "most tickets is about a person."
        ),
    )
    show_contact_names_by: str | None = None
    """Who set the override. A stored review from before the franchisee default existed
    carries `False` with nobody named, which was the old default and not a decision, so
    it is read as no override (`_legacy_contact_names`)."""
    released_by: str | None = Field(
        default=None,
        description=(
            "Who rendered the client deck, which is the act of releasing this review to "
            "the customer. Kept on the document rather than read back out of the audit "
            "log, because it is shown beside the review and the record is not a lookup "
            "table. Cleared with `renders` whenever the review changes: a review that is "
            "no longer released was not released by anybody."
        ),
    )
    released_at: dt.datetime | None = None
    """When that render happened. `None` on a review nobody has released, and on
    one released before this was recorded — which reads as unknown rather than as
    nobody, because rewriting the past is not what the record is for."""

    @model_validator(mode="after")
    def _legacy_contact_names(self) -> ReviewDocument:
        if self.show_contact_names is False and self.show_contact_names_by is None:
            self.show_contact_names = None
        return self

    @property
    def people_names_default(self) -> bool:
        """The franchisee's `show_people_names`, as the review's config snapshot holds it."""
        return bool(self.franchisee.get("show_people_names", False))

    @property
    def people_names_shown(self) -> bool:
        """Whether a client or internal render names people: the review's override when an
        engineer set one, else the franchisee's default. A demo render ignores both."""
        if self.show_contact_names is not None:
            return self.show_contact_names
        return self.people_names_default

    renders: dict[str, str] = Field(
        default_factory=dict, description="Render mode -> the file last written for it."
    )

    def section(self, key: str) -> Section:
        for section in self.sections:
            if section.key == key:
                return section
        raise KeyError(f"no section {key!r}")

    def finding(self, finding_id: str) -> Finding:
        for finding in self.findings:
            if finding.id == finding_id:
                return finding
        raise KeyError(f"no finding {finding_id!r}")

"""Slide by slide: what each section of the review holds, and what state it is in.

This follows `docs/reference/tbr-slide-map.md` row for row. For each slide it
decides one of:

- **filled** — facts or findings back it, and its content is drafted;
- **awaiting_engineer** — nothing in the connectors backs it, but the map names
  the engineer as a source (or it needs their approval, or a finding needs their
  confirmation). It is on the review screen, and off the deck until they act;
- **dropped** — no facts and nothing the engineer is expected to supply. Rule 5:
  no data, no slide;
- **deferred** — it needs a connector that is not built yet;
- **static** — dividers, the AI Essentials offering slide, the closing slide.

A divider is dropped with its section when every slide under it is.
"""

from __future__ import annotations

from dataclasses import dataclass

from anthropic import AsyncAnthropic

from app.reports.tbr.audit import ReportAudit
from app.reports.tbr.config import FranchiseeConfig
from app.reports.tbr.findings import overall_opinion, quarter_for
from app.reports.tbr.layout import HEADLINE_CHARS, SLIDE_BUDGETS
from app.reports.tbr.model import (
    SEVERITY_ORDER,
    FactSet,
    FactTable,
    Finding,
    Item,
    ReviewPeriod,
    Section,
)
from app.reports.tbr.narrative import Brief, Slot, research_trends, substitute, write_section
from app.reports.tbr.scorecard import evaluate as evaluate_scorecard
from app.settings import Settings

#: Template slide number -> (section key, title). The order is the deck's.
SLIDES: dict[int, tuple[str, str]] = {
    1: ("title", "Title"),
    2: ("agenda", "Agenda"),
    3: ("trends", "Industry Trends"),
    4: ("divider_service", "Service Review"),
    5: ("management", "Current Management and Maintenance"),
    6: ("dashboard", "Customer Success Dashboard"),
    7: ("takeaways", "Management Summary Report | Key Takeaways"),
    8: ("accomplishments", "Recent Accomplishments"),
    9: ("cx", "CX Metrics"),
    10: ("divider_technical", "Open Technical Items"),
    11: ("projects", "Current Project Review"),
    12: ("replacement", "Devices Under Management | Recommended for Replacement"),
    13: ("upgrade", "Devices Under Management | Recommended for Upgrade"),
    14: ("cybersecurity", "Cybersecurity"),
    15: ("backup", "Backup / Business Continuity"),
    16: ("divider_strategic", "Strategic Initiatives"),
    17: ("scheduled", "Scheduled Projects"),
    18: ("services", "Recommended Services"),
    19: ("priorities", "Near-Term Priorities"),
    20: ("ai_essentials", "AI Essentials"),
    21: ("divider_timeline", "Implementation Timeline"),
    22: ("budget", "Budget and Timeline"),
    23: ("thanks", "Thank you"),
}

#: Divider slide -> the content slides under it. AI Essentials is a standing
#: offering, not a finding about this customer, so it does not keep its section alive.
DIVIDERS: dict[int, tuple[int, ...]] = {
    4: (5, 6, 7, 8, 9),
    10: (11, 12, 13, 14, 15),
    16: (17, 18, 19),
    21: (22,),
}

#: Too broad to research: "trends in Services" is not a question.
GENERIC_INDUSTRIES = frozenset({"other", "services", "business services", "unknown", ""})

#: The "value delivered" block on the Accomplishments slide: the fact, and how its
#: line reads after the figure (one, many). A fact that was not collected has no
#: line, and neither has a zero: the block says what was done, in code-written
#: words, and nothing is estimated to fill it out.
VALUE_DELIVERED: tuple[tuple[str, str, str], ...] = (
    ("ninjaone.patches.installed", "Windows patch installed", "Windows patches installed"),
    ("ninjaone.alerts.resolved", "monitoring alert resolved", "monitoring alerts resolved"),
    ("autotask.tickets.completed", "ticket resolved", "tickets resolved"),
    (
        "autotask.tickets.completed_out_of_hours",
        "ticket resolved outside business hours",
        "tickets resolved outside business hours",
    ),
)
VALUE_SLOT = "value/"

MAX_DEVICE_ROWS = 8
MAX_BUDGET_ROWS = 6


@dataclass(slots=True)
class Context:
    facts: FactSet
    findings: list[Finding]
    config: FranchiseeConfig
    period: ReviewPeriod
    site_name: str
    client_name: str
    has_prior: bool
    model_client: AsyncAnthropic | None
    settings: Settings
    audit: ReportAudit
    prior: FactSet | None = None
    prior_label: str | None = None

    def assertable(self, section: str | None = None) -> list[Finding]:
        return [
            f
            for f in self.findings
            if f.assertable
            and f.section != "internal"
            and (section is None or f.section == section)
        ]

    def unconfirmed(self, section: str) -> list[Finding]:
        return [f for f in self.findings if f.section == section and not f.assertable]

    def problems(self) -> list[Finding]:
        """What needs doing, most serious first. Improvements are not problems."""
        return [f for f in self.assertable() if f.kind != "improvement"]


def _code(text: str, cites: list[str], **fields: object) -> Item:
    return Item(text=text, cites=cites, author="code", **fields)  # type: ignore[arg-type]


def _static(number: int) -> Section:
    key, title = SLIDES[number]
    return Section(
        key=key,
        slide=number,
        title=title,
        status="static",
        narrative="static",
        reason="Kept as it is in the template.",
    )


# --- one builder per content slide ---------------------------------------------------------


def title_slide(ctx: Context) -> Section:
    return Section(
        key="title",
        slide=1,
        title="Title",
        status="filled",
        narrative="code",
        reason="Client name, franchisee name, review period and the date it was prepared.",
        fact_ids=["report.period.label"],
        data={
            "client": ctx.site_name,
            "presented_by": (
                ctx.config.presenter and f"{ctx.config.presenter}, {ctx.config.display_name}"
            )
            or ctx.config.display_name,
            "period": ctx.period.label,
            "logo": str(ctx.config.logo() or ""),
        },
    )


async def trends(ctx: Context) -> Section:
    fact = ctx.facts.get("autotask.company.industry")
    industry = str(fact.value) if fact and fact.value else ""
    section = Section(
        key="trends",
        slide=3,
        title=SLIDES[3][1],
        status="awaiting_engineer",
        requires_approval=True,
        reason="",
        fact_ids=["autotask.company.industry"] if fact else [],
    )
    if industry.strip().casefold() in GENERIC_INDUSTRIES:
        section.reason = (
            f"Autotask gives this company's industry as {industry!r}, which is too broad to "
            "research."
            if industry
            else "Autotask holds no market segment for this company."
        )
        section.waiting_for = (
            "Name the customer's industry; the trends are then researched and come back here "
            "for approval."
        )
        return section

    draft = await research_trends(
        industry,
        never_in_a_query=[ctx.site_name, ctx.client_name],
        model_client=ctx.model_client,
        settings=ctx.settings,
        audit=ctx.audit,
    )
    section.items = draft.items
    section.problems = draft.problems
    section.narrative = "model" if draft.items else "none"
    section.data = {"industry": industry, "searches": " | ".join(draft.searches)}
    if draft.items:
        section.reason = (
            f"Researched for the {industry} industry from the web; every trend carries its "
            "source. Web research is never rendered without an engineer's approval."
        )
        section.waiting_for = "Read each trend against its source, edit or remove, then approve."
    else:
        section.reason = f"Research for the {industry} industry produced nothing usable."
        section.waiting_for = "Write the trends by hand, or re-run the research."
    return section


async def management(ctx: Context) -> Section:
    # (fact, label, label when the value is exactly one)
    rows = (
        ("ninjaone.devices.workstations", "Workstations and Laptops", "Workstation or Laptop"),
        ("ninjaone.devices.servers_physical", "Physical Servers", "Physical Server"),
        ("ninjaone.devices.servers_virtual", "Virtual Servers", "Virtual Server"),
        ("site.locations", "Locations", "Location"),
    )
    present = [
        (fact_id, one if ctx.facts.number(fact_id) == 1 else many)
        for fact_id, many, one in rows
        if ctx.facts.get(fact_id)
    ]
    section = Section(key="management", slide=5, title=SLIDES[5][1], status="dropped", reason="")
    if not any(fact_id.startswith("ninjaone.devices") for fact_id, _ in present):
        section.reason = "NinjaOne returned no device inventory for this customer."
        return section

    items: list[Item] = []
    for fact_id, label in present:
        fact = ctx.facts.facts[fact_id]
        items.append(_code(label, [fact_id], lead=fact.display))

    opinion = overall_opinion(ctx.findings)
    months = ctx.facts.facts["report.period.months"].display
    cites = ["report.period.months", *[f.id for f in ctx.problems()[:5]]]
    items.append(
        _code(
            f"Based on a technical review of the last {months}, all segments of "
            f"{ctx.config.display_name}'s services offering have been {opinion}.",
            cites,
            role="paragraph",
        )
    )

    improved = [f for f in ctx.assertable("accomplishments") if f.kind == "improvement"]
    narrative = "code"
    if improved:  # only with a prior snapshot: otherwise there is nothing to compare with
        brief = Brief(
            key="management",
            budget=SLIDE_BUDGETS["management"],
            title=SLIDES[5][1],
            purpose="One sentence naming the areas that improved since the last review.",
            fact_ids=[i for f in improved for i in f.fact_ids],
            findings=improved,
            slots=[
                Slot(
                    id="improvements",
                    role="paragraph",
                    guidance=(
                        f"Begin '{ctx.config.display_name} reports show improvements in the "
                        "areas of' and name the areas these findings cover, in plain words. "
                        "No figures."
                    ),
                    must_cite=tuple(f.id for f in improved[:3]),
                    fallback=(
                        f"{ctx.config.display_name} reports show improvements since the last "
                        "review in: "
                        + "; ".join(f.headline.split(":")[0] for f in improved[:3])
                        + "."
                    ),
                )
            ],
        )
        draft = await write_section(
            brief, ctx.facts, model_client=ctx.model_client, settings=ctx.settings, audit=ctx.audit
        )
        items.extend(draft.items)
        narrative = draft.narrative
        section.problems = draft.problems

    section.status = "filled"
    section.items = items
    section.narrative = narrative  # type: ignore[assignment]
    section.fact_ids = [fact_id for fact_id, _ in present]
    section.finding_ids = [f.id for f in improved]
    section.reason = (
        "Device and location counts from NinjaOne and Autotask. The template's Users line is "
        "left off until a Microsoft 365 connector can count users. "
    ) + (
        "Improvement areas come from the change since the last review."
        if improved
        else "No earlier review is stored, so the sentence about improvements is left out."
    )
    return section


#: A recommendation under an observation is one line; the slide-14 budget
#: is measured with it at this length.
RECOMMENDATION_CHARS = 100


def _finding_slots(findings: list[Finding], *, recommend: bool) -> list[Slot]:
    slots: list[Slot] = []
    for finding in findings:
        facts_cited = tuple(i for i in finding.fact_ids if "." in i)[:1]
        slots.append(
            Slot(
                id=f"{finding.id}/observation",
                role="observation",
                lead=f"{finding.headline}:",
                guidance=(
                    "State what was observed, using the finding's figures as tokens, and what "
                    "it means for the business. One sentence, two short ones at most."
                ),
                must_cite=(finding.id, *facts_cited),
                fallback=finding.detail,
            )
        )
        if recommend and finding.recommendation:
            slots.append(
                Slot(
                    id=f"{finding.id}/recommendation",
                    role="recommendation",
                    lead="Recommendation:",
                    guidance="State the recommendation in one sentence. No figures.",
                    # Tighter than the observation: the slide's budget is measured
                    # with the recommendation at this length (tests/reports/test_tbr_layout.py).
                    max_chars=RECOMMENDATION_CHARS,
                    must_cite=(finding.id,),
                    fallback=finding.recommendation,
                )
            )
    return slots


async def dashboard(ctx: Context) -> Section:
    charts = [chart_id for chart_id in ctx.facts.charts if chart_id.startswith("autotask.chart")]
    found = ctx.assertable("dashboard")[: SLIDE_BUDGETS["dashboard"].max_items]
    section = Section(key="dashboard", slide=6, title=SLIDES[6][1], status="dropped", reason="")
    if not charts:
        section.reason = (
            "Autotask returned no tickets for the period, so there is nothing to chart."
        )
        return section
    section.status = "filled"
    section.fact_ids = charts
    section.finding_ids = [f.id for f in found]
    section.reason = (
        f"{len(charts)} charts built from Autotask tickets replace the sample screenshot. "
        + (
            f"{len(found)} observations from ticket-pattern findings."
            if found
            else "No ticket pattern crossed a threshold, so the charts stand without observations."
        )
    )
    if found:
        brief = Brief(
            key="dashboard",
            budget=SLIDE_BUDGETS["dashboard"],
            title=SLIDES[6][1],
            purpose=(
                "Short observations beside the ticket charts: what the helpdesk pattern shows "
                "and what it suggests. Each is one sentence ending with the suggestion."
            ),
            fact_ids=[i for f in found for i in f.fact_ids],
            findings=found,
            slots=[
                Slot(
                    id=f"{f.id}/observation",
                    role="bullet",
                    guidance=(
                        "One sentence: the pattern, with its figures as tokens, then the "
                        "recommendation it points to."
                    ),
                    must_cite=(f.id,),
                    fallback=f"{f.detail} {f.recommendation or ''}".strip(),
                )
                for f in found
            ],
        )
        draft = await write_section(
            brief, ctx.facts, model_client=ctx.model_client, settings=ctx.settings, audit=ctx.audit
        )
        section.items, section.narrative = draft.items, draft.narrative
        section.problems = draft.problems
    else:
        section.narrative = "code"
    return section


async def takeaways(ctx: Context) -> Section:
    top = ctx.problems()[: SLIDE_BUDGETS["takeaways"].max_items]
    section = Section(key="takeaways", slide=7, title=SLIDES[7][1], status="dropped", reason="")
    # The scorecard is computed by rules (scorecard.toml), never drafted: it is not in the
    # brief, and the model neither sees nor writes a status.
    areas = evaluate_scorecard(ctx.facts, ctx.prior)
    scored = [row for row in areas if row.metrics]
    if scored:
        section.scorecard = areas
        section.fact_ids = list(dict.fromkeys(i for row in scored for i in row.cites()))
        if ctx.prior_label and any(row.trend for row in areas):
            section.data["scorecard_compared_with"] = ctx.prior_label
    card = (
        f"Scorecard: {', '.join(f'{row.label} {row.status_label.lower()}' for row in areas)}. "
        "Statuses are computed from this review's facts by the rules in scorecard.toml"
        + (
            f"; arrows compare with {ctx.prior_label}."
            if section.data.get("scorecard_compared_with")
            else "; no earlier review is stored, so there are no trend arrows."
        )
        if scored
        else ""
    )
    if not top:
        if not scored:
            section.reason = (
                "No finding crossed a threshold and no scorecard area could be scored, so "
                "there is nothing to state."
            )
            return section
        section.status, section.narrative = "filled", "code"
        section.reason = f"No finding crossed a threshold, so the slide is the scorecard. {card}"
        return section
    brief = Brief(
        key="takeaways",
        budget=SLIDE_BUDGETS["takeaways"],
        title=SLIDES[7][1],
        purpose=(
            "The few things leadership should take from this review, most serious first. For "
            "each finding: a headline of at most ten words, and a detail of two sentences."
        ),
        fact_ids=[i for f in top for i in f.fact_ids],
        findings=top,
        slots=[
            slot
            for f in top
            for slot in (
                Slot(
                    id=f"{f.id}/headline",
                    role="headline",
                    guidance="A headline of at most ten words. No figures.",
                    max_chars=HEADLINE_CHARS,
                    must_cite=(f.id,),
                    fallback=f.headline,
                ),
                Slot(
                    id=f"{f.id}/detail",
                    role="paragraph",
                    guidance="Two sentences: what was found, with figures as tokens, and why it "
                    "matters.",
                    must_cite=(f.id,),
                    fallback=f"{f.detail} {f.recommendation or ''}".strip(),
                ),
            )
        ],
    )
    draft = await write_section(
        brief, ctx.facts, model_client=ctx.model_client, settings=ctx.settings, audit=ctx.audit
    )
    # Pair each headline with its detail: one table row per finding.
    rows: list[Item] = []
    for index in range(0, len(draft.items) - 1, 2):
        head, detail = draft.items[index], draft.items[index + 1]
        rows.append(
            Item(
                role="paragraph",
                lead=head.text,
                text=detail.text,
                template=detail.template,
                cites=sorted({*head.cites, *detail.cites}),
                author=detail.author,
            )
        )
    section.status = "filled"
    section.items = rows
    section.narrative = draft.narrative
    section.problems = draft.problems
    section.finding_ids = [f.id for f in top]
    section.reason = f"The {len(top)} most serious findings across every section. {card}".strip()
    return section


def value_delivered(ctx: Context) -> list[Item]:
    """The block's heading and lines, or nothing when no figure is available."""
    lines: list[Item] = []
    for fact_id, one, many in VALUE_DELIVERED:
        fact = ctx.facts.get(fact_id)
        if fact is None or not isinstance(fact.value, int) or fact.value <= 0:
            continue
        text, cites = (one if fact.value == 1 else many), [fact_id]
        hours = ctx.facts.get("report.business_hours")
        if fact_id.endswith("out_of_hours") and hours is not None:
            text, cites = f"{text} ({hours.display})", [fact_id, hours.id]
        lines.append(_code(text, cites, lead=fact.display, slot=f"{VALUE_SLOT}{fact_id}"))
    if not lines:
        return []
    heading = _code(
        f"Value delivered in {ctx.period.label}",
        [cite for line in lines for cite in line.cites],
        role="headline",
        slot=f"{VALUE_SLOT}heading",
    )
    return [heading, *lines]


def accomplishments(ctx: Context) -> Section:
    improved = [f for f in ctx.assertable("accomplishments") if f.kind == "improvement"]
    value = value_delivered(ctx)
    table = ctx.facts.tables.get("autotask.projects.completed.list")
    items = [
        _code(f"{f.headline.split(':')[0]} went {ctx.facts.facts[f.fact_ids[0]].display}.", [f.id])
        for f in improved
        if f.fact_ids and f.fact_ids[0] in ctx.facts.facts
    ]
    if table:
        items.extend(
            _code(f"Completed project: {name} ({when}).", [table.id]) for name, when in table.rows
        )
    others = bool(items)
    items = [*value, *items]
    section = Section(
        key="accomplishments",
        slide=8,
        title=SLIDES[8][1],
        status="filled" if items else "awaiting_engineer",
        narrative="code" if items else "none",
        items=items,
        fact_ids=[*([table.id] if table else []), *(line.cites[0] for line in value[1:])],
        finding_ids=[f.id for f in improved],
        reason="",
    )
    if items:
        parts = []
        if value:
            missing = [
                many for fact_id, _, many in VALUE_DELIVERED if fact_id not in ctx.facts.facts
            ]
            parts.append(
                f"Value delivered: {len(value) - 1} figure(s) counted in code from the period's "
                "records"
                + (f"; not available and left out: {', '.join(missing)}." if missing else ".")
            )
        if others:
            parts.append("Improvements against the last review and projects completed.")
        section.reason = " ".join(parts)
        section.waiting_for = "Optional: add accomplishments the tools cannot see."
    else:
        section.reason = "No project was completed in the period" + (
            " and nothing improved measurably since the last review."
            if ctx.has_prior
            else " and no earlier review is stored to measure improvement against."
        )
        section.waiting_for = "Add the period's accomplishments, or the slide is left out."
    return section


def cx(ctx: Context) -> Section:
    refused = next((g for g in ctx.facts.gaps if "survey" in g.what.casefold()), None)
    return Section(
        key="cx",
        slide=9,
        title=SLIDES[9][1],
        status="awaiting_engineer",
        reason=(
            "No survey results are available from Autotask on this tenant"
            + (f" ({refused.why})." if refused else " (the Surveys entities answer HTTP 500).")
        ),
        waiting_for=(
            "Enter the NPS, CSAT and product-quality scores and up to three survey comments "
            "from the survey tool, or the slide is left out."
        ),
    )


async def projects(ctx: Context) -> Section:
    table = ctx.facts.tables.get("autotask.projects.current.list")
    section = Section(key="projects", slide=11, title=SLIDES[11][1], status="dropped", reason="")
    if not table:
        section.reason = "Autotask lists no client project in progress for this company."
        return section

    blocks: list[tuple[str | None, list[Item]]] = []
    slots: list[Slot] = []
    for index, row in enumerate(table.rows):
        name, phase, percent, due, description, status, late = row
        progress = f", {percent} complete" if percent != "not recorded" else ""
        where = (
            f"{ctx.site_name} is in the {phase} phase of the project{progress}."
            if phase
            else f"The project's status in Autotask is {status}{progress}."
        )
        if due == "not set":
            finish = "No completion date is set for this project in Autotask."
        elif late:
            finish = (
                f"The project was planned to complete by the end of {due} and is past "
                "that date; a revised date needs to be agreed."
            )
        else:
            finish = f"The project should be completed before the end of {due}."
        slot_id = f"project-{index}/scope" if description else None
        if slot_id:
            slots.append(
                Slot(
                    id=slot_id,
                    role="paragraph",
                    guidance=(
                        "One sentence beginning 'This includes' that says what the project "
                        "covers, from this description only: " + description
                    ),
                    must_cite=(table.id,),
                )
            )
        # The three code-written lines share the scope sentence's slot prefix, so
        # the renderer's fit drops or keeps a project whole.
        blocks.append(
            (
                slot_id,
                [
                    _code(name, [table.id], role="headline", slot=f"project-{index}/name"),
                    _code(where, [table.id], role="paragraph", slot=f"project-{index}/phase"),
                    _code(finish, [table.id], role="paragraph", slot=f"project-{index}/due"),
                ],
            )
        )

    narrative = "code"
    scopes: dict[str, Item] = {}
    if slots:
        brief = Brief(
            key="projects",
            budget=SLIDE_BUDGETS["projects"],
            title=SLIDES[11][1],
            purpose="Say in one sentence what each project covers, from its Autotask description.",
            fact_ids=[table.id],
            findings=[],
            slots=slots,
        )
        draft = await write_section(
            brief, ctx.facts, model_client=ctx.model_client, settings=ctx.settings, audit=ctx.audit
        )
        scopes = {item.slot: item for item in draft.items if item.slot}
        narrative = draft.narrative if draft.items else "code"
        section.problems = draft.problems
    # The scope sentence sits between the phase line and the completion line.
    items: list[Item] = []
    for slot_id, (head, phase_line, due_line) in ((s, tuple(b)) for s, b in blocks):
        items.extend([head, phase_line])
        if slot_id and slot_id in scopes:
            items.append(scopes[slot_id])
        items.append(due_line)

    overdue = ctx.assertable("projects")
    section.status = "filled"
    section.items = items
    section.narrative = narrative  # type: ignore[assignment]
    section.fact_ids = [table.id]
    section.finding_ids = [f.id for f in overdue]
    section.reason = f"{table.total} client project(s) in progress in Autotask."
    return section


def _device_items(
    ctx: Context, table: FactTable, *, recommendation_type: str, why_column: int | None
) -> list[Item]:
    hours, cost = ctx.config.quote(recommendation_type)
    items: list[Item] = []
    for row in table.rows[:MAX_DEVICE_ROWS]:
        detail = f" — {row[why_column]}" if why_column is not None else ""
        items.append(
            _code(
                f"{row[0]} ({row[1]}){detail}. Hours: {hours}. Cost: {cost}.",
                [table.id],
            )
        )
    hidden = table.total - len(items)
    if hidden > 0:
        items.append(
            _code(f"And {hidden} more; the full list is attached to this review.", [table.id])
        )
    return items


def replacement(ctx: Context) -> Section:
    table = ctx.facts.tables.get("ninjaone.windows11.ineligible.devices")
    section = Section(
        key="replacement",
        slide=12,
        title=SLIDES[12][1],
        status="dropped",
        reason="",
        later_connector="Autotask configuration items or ScalePad, for device age and warranty",
    )
    if not table:
        section.reason = (
            "No Windows 10 device fails the Windows 11 processor and memory check. Replacement "
            "by age needs purchase or warranty dates, which NinjaOne does not hold."
        )
        return section
    section.status = "filled"
    section.narrative = "code"
    section.items = _device_items(
        ctx, table, recommendation_type="device_replacement", why_column=3
    )
    section.fact_ids = [table.id, "ninjaone.windows11.ineligible"]
    section.finding_ids = [f.id for f in ctx.assertable("replacement")]
    section.reason = (
        f"{table.total} Windows 10 device(s) that cannot run Windows 11, judged by processor "
        "generation and memory. Device age is not available yet."
    )
    return section


def upgrade(ctx: Context) -> Section:
    sources = (
        ("ninjaone.windows11.eligible.devices", "windows11_upgrade", None, "Upgrade to Windows 11"),
        ("ninjaone.hardware.low_memory.devices", "memory_upgrade", 2, "Add memory"),
        ("ninjaone.disks.low.volumes", "storage_upgrade", 2, "Free or add storage"),
    )
    items: list[Item] = []
    used: list[str] = []
    for table_id, recommendation_type, why, heading in sources:
        table = ctx.facts.tables.get(table_id)
        if not table:
            continue
        used.append(table_id)
        items.append(_code(heading, [table_id], role="headline"))
        items.extend(
            _device_items(ctx, table, recommendation_type=recommendation_type, why_column=why)
        )
    section = Section(key="upgrade", slide=13, title=SLIDES[13][1], status="dropped", reason="")
    if not items:
        section.reason = (
            "No device is on Windows 10, short of memory or low on disk space against the "
            "franchisee's thresholds."
        )
        return section
    section.status = "filled"
    section.narrative = "code"
    section.items = items
    section.fact_ids = used
    section.finding_ids = [f.id for f in ctx.assertable("upgrade")]
    section.reason = "Devices from NinjaOne; hours and cost from the price book."
    return section


async def cybersecurity(ctx: Context) -> Section:
    every = [f for f in ctx.assertable("cybersecurity") if f.kind != "improvement"]
    # The template's slide is three Observation/Recommendation pairs, and a slide
    # of seven is not a slide. The most serious go on it; the rest stay on the
    # stored review for the engineer, and the reason line says how many.
    budget = SLIDE_BUDGETS["cybersecurity"]
    found, held_back = every[: budget.max_items], every[budget.max_items :]
    waiting = ctx.unconfirmed("cybersecurity")
    section = Section(
        key="cybersecurity",
        slide=14,
        title=SLIDES[14][1],
        status="dropped",
        reason="",
        later_connector="Blackpoint, for EDR corroboration",
        finding_ids=[f.id for f in [*every, *waiting]],
    )
    if waiting:
        section.waiting_for = (
            "Confirm or dismiss: "
            + "; ".join(f.headline for f in waiting)
            + ". Each rests on one tool only, and a gap is not asserted without a second."
        )
    if not found:
        if waiting:
            section.status = "awaiting_engineer"
            section.reason = (
                "The only security findings are unconfirmed gaps seen by a single tool."
            )
        else:
            bitdefender = ctx.facts.get("bitdefender.endpoints.total") is not None
            section.reason = (
                "NinjaOne shows no failed patch installation, no outstanding critical patch "
                "and no unprotected device for this customer"
                + (
                    ", and Bitdefender shows every managed computer with a current agent, "
                    "core protection on and encryption in place."
                    if bitdefender
                    else ". Bitdefender was not read for this site."
                )
            )
        return section
    brief = Brief(
        key="cybersecurity",
        budget=SLIDE_BUDGETS["cybersecurity"],
        title=SLIDES[14][1],
        purpose="Observation then recommendation, for each security finding, most serious first.",
        fact_ids=[i for f in found for i in f.fact_ids],
        findings=found,
        slots=_finding_slots(found, recommend=True),
    )
    draft = await write_section(
        brief, ctx.facts, model_client=ctx.model_client, settings=ctx.settings, audit=ctx.audit
    )
    section.status = "filled"
    section.items = draft.items
    section.narrative = draft.narrative
    section.problems = draft.problems
    section.fact_ids = brief.fact_ids
    bitdefender = ctx.facts.get("bitdefender.endpoints.total") is not None
    connectsecure = ctx.facts.get("connectsecure.assets.total") is not None
    # The scanner's other population, as one code-written caveat and nothing else:
    # it is a different set of records from every number above, so it never shares
    # a sentence with them and is never added to them.
    # It also counts the severity the slide's headline counts. The all-severity
    # figure is mostly medium and high, and under "118 critical findings open"
    # it reads as thousands more criticals.
    suspected = ctx.facts.number("connectsecure.vulnerabilities.unconfirmed_critical") or 0
    if suspected > 0:
        template = (
            "A further {{connectsecure.vulnerabilities.unconfirmed_critical}} critical findings "
            "are suspected but not yet verified by a scan, and are not counted above."
        )
        section.items.append(
            _code(
                substitute(template, ctx.facts),
                ["connectsecure.vulnerabilities.unconfirmed_critical"],
                role="paragraph",
                template=template,
            )
        )
    tools = (
        ["NinjaOne"]
        + (["Bitdefender"] if bitdefender else [])
        + (["ConnectSecure"] if connectsecure else [])
    )
    covered = ["antivirus state, failed installations and outstanding critical patches"]
    if bitdefender:
        covered.insert(0, "agent coverage by hostname, patching and encryption by either tool")
    if connectsecure:
        covered.append(
            "known-exploited and critical vulnerabilities, the fixes that resolve most of "
            "them, and scan coverage against NinjaOne's inventory"
        )
    missing = [t for t in ("Bitdefender", "ConnectSecure") if t not in tools]
    section.reason = (
        f"{len(found)} finding(s) from {' and '.join(tools)}: {'; '.join(covered)}. "
        + (f"{' and '.join(missing)} not read for this site. " if missing else "")
        + budget.held_back_reason([f.headline for f in held_back])
        + "Blackpoint comes later."
    )
    return section


def backup(ctx: Context) -> Section:
    return Section(
        key="backup",
        slide=15,
        title=SLIDES[15][1],
        status="deferred",
        reason="Backup coverage, offsite replication and Microsoft 365 backup come from Cove, "
        "which is not connected yet.",
        later_connector="N-able Cove",
    )


def scheduled(ctx: Context) -> Section:
    table = ctx.facts.tables.get("autotask.projects.scheduled.list")
    if not table:
        return Section(
            key="scheduled",
            slide=17,
            title=SLIDES[17][1],
            status="awaiting_engineer",
            reason="Autotask lists no project with a future start date, and accepted quotes "
            "cannot be read on this tenant (the Quotes entity answers HTTP 500).",
            waiting_for="Add the projects that are agreed and scheduled, or the slide is left out.",
        )
    items = [_code(f"{name}. Estimated start: {start}.", [table.id]) for name, start in table.rows]
    return Section(
        key="scheduled",
        slide=17,
        title=SLIDES[17][1],
        status="filled",
        narrative="code",
        items=items,
        fact_ids=[table.id],
        reason=f"{table.total} Autotask project(s) with a start date in the future.",
    )


def _backed_services(ctx: Context) -> list[tuple[str, list[Finding]]]:
    """Catalog services with at least one assertable finding behind them, by severity."""
    grouped: dict[str, list[Finding]] = {}
    for finding in ctx.problems():
        if finding.service_key and ctx.config.service(finding.service_key):
            grouped.setdefault(finding.service_key, []).append(finding)
    return sorted(grouped.items(), key=lambda pair: SEVERITY_ORDER[pair[1][0].severity])


async def services(ctx: Context) -> Section:
    every = _backed_services(ctx)
    section = Section(key="services", slide=18, title=SLIDES[18][1], status="dropped", reason="")
    if not every:
        section.reason = (
            "No finding backs a service in the catalog, and none is listed without one."
        )
        return section
    # The slide holds a few services, not the whole catalogue: the ones backed by
    # the most serious findings go on it, and the reason line names the rest.
    budget = SLIDE_BUDGETS["services"]
    backed, held_back = every[: budget.max_items], every[budget.max_items :]
    slots = []
    for key, behind in backed:
        service = ctx.config.service(key)
        assert service is not None
        slots.append(
            Slot(
                id=f"service/{key}",
                role="bullet",
                lead=f"{service.name}:",
                guidance=(
                    f"One sentence on why {service.name} is recommended for this customer, "
                    "from these findings: " + ", ".join(f.id for f in behind)
                ),
                must_cite=(behind[0].id,),
                fallback=behind[0].detail,
            )
        )
    found = [f for _, behind in backed for f in behind]
    brief = Brief(
        key="services",
        budget=SLIDE_BUDGETS["services"],
        title=SLIDES[18][1],
        purpose="Why each recommended service is recommended. Only services with a finding "
        "behind them appear.",
        fact_ids=[i for f in found for i in f.fact_ids],
        findings=found,
        slots=slots,
    )
    draft = await write_section(
        brief, ctx.facts, model_client=ctx.model_client, settings=ctx.settings, audit=ctx.audit
    )
    section.status = "filled"
    section.items = draft.items
    section.narrative = draft.narrative
    section.problems = draft.problems
    section.finding_ids = [f.id for _, behind in every for f in behind]
    section.reason = (
        f"{len(backed)} catalog service(s), each backed by a finding. "
        + budget.held_back_reason(
            [
                f"{name} ({behind[0].headline})"
                for key, behind in held_back
                if (service := ctx.config.service(key)) and (name := service.name)
            ]
        )
    ).strip()
    return section


def priorities(ctx: Context) -> Section:
    section = Section(key="priorities", slide=19, title=SLIDES[19][1], status="dropped", reason="")
    top = ctx.problems()[:3]
    if not top:
        section.reason = "No finding to prioritise."
        return section
    items: list[Item] = []
    seen: set[str] = set()
    for finding in top:
        service = ctx.config.service(finding.service_key)
        label = service.name if service else finding.headline
        if label in seen:
            continue
        seen.add(label)
        items.append(_code(label, [finding.id]))
    section.status = "filled"
    section.narrative = "code"
    section.items = items
    section.finding_ids = [f.id for f in top]
    section.reason = "The most serious findings, as the services or actions that address them."
    return section


def budget(ctx: Context) -> Section:
    section = Section(key="budget", slide=22, title=SLIDES[22][1], status="dropped", reason="")
    rows: list[Item] = []
    seen: set[str] = set()
    for finding in ctx.problems():
        key = finding.recommendation_type or finding.service_key
        if not key or key in seen:
            continue
        seen.add(key)
        service = ctx.config.service(finding.service_key)
        entry = ctx.config.price_book.get(key)
        label = (entry.label if entry and entry.label else None) or (
            service.name if service else finding.headline
        )
        quantity = int(ctx.facts.number(finding.fact_ids[0]) or 1) if finding.fact_ids else 1
        _, cost = ctx.config.quote(key, quantity=quantity)
        when = quarter_for(finding.severity, ctx.period)
        rows.append(_code(label, [finding.id], cells=[label, cost, when]))
        if len(rows) == MAX_BUDGET_ROWS:
            break
    if not rows:
        section.reason = "No recommendation to budget for."
        return section
    section.status = "filled"
    section.narrative = "code"
    section.items = rows
    section.finding_ids = [i.cites[0] for i in rows]
    priced = sum(1 for row in rows if row.cells[1] != "Quote pending")
    section.reason = (
        f"{len(rows)} recommendation(s). {priced} priced from the price book; the rest print "
        "'Quote pending'. The quarter follows each finding's severity."
    )
    return section


MONTHLY_SECTION = "monthly_report"
MONTHLY_BLOCKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # (block, what the item says, the facts, tables or charts it needs one of)
    (
        "Ticketing",
        "tickets created and completed by day, the SLA gauges, tickets by contact and by "
        "issue type",
        ("autotask.tickets.created", "autotask.chart.daily"),
    ),
    (
        "Monitoring: patches",
        "OS and third-party patch installs for the period, with the failed devices",
        ("ninjaone.patches.installed", "ninjaone.software_patches.installed"),
    ),
    (
        "Monitoring: lifecycle",
        "the workstation and server lifecycle tables (user, make, model, OS, memory, "
        "processor, location, storage, estimated age)",
        ("ninjaone.lifecycle.workstations", "ninjaone.lifecycle.servers"),
    ),
)


def monthly_report(ctx: Context) -> Section:
    """The Monthly Report PDF's approval step. Not a slide: `slide=0` keeps it off the
    deck. Its items are code-written lines saying which blocks the review holds, so an
    engineer approves a description of what the PDF will print; the PDF itself reads
    the facts, and a client render refuses until this is approved."""
    held = ctx.facts.facts.keys() | ctx.facts.tables.keys() | ctx.facts.charts.keys()
    items = [
        _code(f"{block}: {what}.", [i for i in ids if i in held])
        for block, what, ids in MONTHLY_BLOCKS
        if any(i in held for i in ids)
    ]
    items.append(
        _code(
            "Backup: not yet assessed until N-able Cove is connected; the section prints the "
            "shape it will fill.",
            [],
        )
    )
    section = Section(
        key=MONTHLY_SECTION,
        slide=0,
        title="Monthly Report (PDF)",
        status="filled" if len(items) > 1 else "awaiting_engineer",
        narrative="code",
        requires_approval=True,
        reason=(
            "The second output, rendered from the same facts as the deck with no model-written "
            "text. A client PDF renders only once an engineer approves it here."
            if len(items) > 1
            else "The review holds no ticket, patch or lifecycle facts to print."
        ),
        items=items,
        fact_ids=sorted({i for item in items for i in item.cites}),
        waiting_for=(
            "Read the sections against the internal PDF, then approve."
            if len(items) > 1
            else "Generate the review again once a connector answers."
        ),
    )
    return section


async def build_sections(ctx: Context) -> list[Section]:
    built: dict[int, Section] = {
        0: monthly_report(ctx),
        1: title_slide(ctx),
        3: await trends(ctx),
        5: await management(ctx),
        6: await dashboard(ctx),
        7: await takeaways(ctx),
        8: accomplishments(ctx),
        9: cx(ctx),
        11: await projects(ctx),
        12: replacement(ctx),
        13: upgrade(ctx),
        14: await cybersecurity(ctx),
        15: backup(ctx),
        17: scheduled(ctx),
        18: await services(ctx),
        19: priorities(ctx),
        22: budget(ctx),
    }
    for number in (20, 23, *DIVIDERS):
        built[number] = _static(number)
    built[2] = Section(
        key="agenda",
        slide=2,
        title="Agenda",
        status="filled",
        narrative="code",
        reason="Derived at render time from the slides that are on the deck.",
    )
    return [built[number] for number in sorted(built)]

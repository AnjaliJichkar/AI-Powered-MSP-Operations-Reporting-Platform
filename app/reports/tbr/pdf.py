"""The Monthly Report: the review's facts as a PDF, beside the deck.

The second output of a stored review. Same record, same facts, same three
render modes, same gate; no model-written sentence anywhere on it. Where the
deck is a conversation on a projector, the PDF is the month's numbers on
paper: Ticketing (tickets by day, the SLA gauges, who raised them, what
about), Monitoring (patch installs for the period, the workstation and
server lifecycle tables) and Backup (not yet assessed until Cove exists, the
shape it will fill in shown).

ReportLab draws it: pure Python, one wheel, no system package, so it runs on
Render's native Python runtime as the deck does; charts are ReportLab
graphics, so they are vectors. US Letter, landscape, the franchisee's logo on
the cover.

**Everything printed is a fact, a table cell or a chart the collectors
recorded**, looked up by id (`app/reports/tbr/monthly.py` lists the ids only
the PDF reads). A number the review does not hold is a line that says so,
never a zero. The gate runs on every string drawn (`Texts`): no fact token,
no template placeholder, no real name in a demo render, no contact or user
name while the review keeps them private.

The review's `monthly_report` section is the approval step: a client PDF
renders only once an engineer has approved it; an internal or demo PDF
renders before that, stamped as not approved.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Circle, Drawing, Rect, String, Wedge
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.reports.tbr.collect import ROLE_DISAGREEMENTS
from app.reports.tbr.demo import (
    NOT_A_PERSON,
    STAMP,
    Pseudonyms,
    contact_label,
    pseudonymise,
    real_name_checker,
    real_name_terms,
)
from app.reports.tbr.gate import NOTE_PLACEHOLDERS, RenderGateError
from app.reports.tbr.model import ChartData, Fact, FactTable, ReviewDocument, Section
from app.reports.tbr.monthly import (
    BACKUP_COVERAGE,
    BACKUP_TABLE,
    LIFECYCLE_SERVERS,
    LIFECYCLE_WORKSTATIONS,
    NOT_REPORTED,
    OS_FAILED_LIST,
    OS_TOP_FAILED,
    SOFTWARE_ATTEMPTED,
    SOFTWARE_FAILED,
    SOFTWARE_FAILED_DEVICES,
    SOFTWARE_FAILED_LIST,
    SOFTWARE_INSTALLED,
    SOFTWARE_SUCCESS,
    SOFTWARE_TOP_FAILED,
    free_band,
)
from app.reports.tbr.render import CONTACT_CHART, RenderMode, anonymised_contacts

MONTHLY_SECTION = "monthly_report"
PAGE = landscape(letter)
MARGIN = 0.5 * inch

# --- palette: the sample's, restrained ------------------------------------------------------
INK = colors.HexColor("#1F2933")
MUTED = colors.HexColor("#6B7280")
RULE = colors.HexColor("#E5E7EB")
BLUE = colors.HexColor("#6B8FE8")
GREEN = colors.HexColor("#5FBF8A")
GREEN_DARK = colors.HexColor("#1E8E5A")
AMBER = colors.HexColor("#E8A33D")
RED = colors.HexColor("#D9534F")
TINT_BLUE = colors.HexColor("#EEF3FE")
TINT_GREEN = colors.HexColor("#EDF9F1")
TINT_AMBER = colors.HexColor("#FFF7E8")
TINT_RED = colors.HexColor("#FDEDEC")
BAND_FILL = {
    "healthy": TINT_GREEN,
    "low": TINT_AMBER,
    "critical": TINT_RED,
    "unknown": colors.white,
}
BAND_INK = {"healthy": GREEN_DARK, "low": AMBER, "critical": RED, "unknown": MUTED}
SERIES_COLOURS = (BLUE, GREEN, GREEN_DARK)


def _style(
    name: str, font: str, size: float, leading: float, colour: Any, **kw: Any
) -> ParagraphStyle:
    return ParagraphStyle(
        name, fontName=font, fontSize=size, leading=leading, textColor=colour, **kw
    )


STYLES = {
    "title": _style("title", "Helvetica-Bold", 20, 24, INK, alignment=TA_CENTER),
    "cover": _style("cover", "Helvetica", 11, 16, INK, alignment=TA_CENTER),
    "h1": _style("h1", "Helvetica-Bold", 14, 18, INK),
    "sub": _style("sub", "Helvetica", 8.5, 11, MUTED),
    "h2": _style("h2", "Helvetica-Bold", 11, 14, INK),
    "body": _style("body", "Helvetica", 9, 12, INK),
    "small": _style("small", "Helvetica", 7.5, 10, MUTED),
    "cell": _style("cell", "Helvetica", 7.5, 9.5, INK),
    "cellhead": _style("cellhead", "Helvetica-Bold", 7.5, 9.5, MUTED),
    "big": _style("big", "Helvetica-Bold", 18, 22, INK, alignment=TA_CENTER),
    "trace": _style("trace", "Courier", 6.5, 8, MUTED),
    "note": _style("note", "Helvetica-Oblique", 8, 11, MUTED),
    "pending": _style("pending", "Helvetica-Oblique", 11, 22, MUTED, alignment=TA_CENTER),
}


@dataclass(slots=True)
class PdfResult:
    path: Path
    pages: int
    mode: RenderMode
    sections: list[str]
    """The sections rendered, in order."""
    omitted: list[tuple[str, str]]
    """(section, why) for a section or block left out because the review holds nothing for it."""
    texts_checked: int
    figures: dict[str, str] = field(default_factory=dict)
    """Every fact drawn, id -> display: what the agreement test holds against the deck."""
    charts: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    """Every chart drawn, id -> series -> category -> value."""
    warnings: list[str] = field(default_factory=list)


class Texts:
    """Every string drawn on the PDF, for the gate, and every figure drawn, for the
    agreement with the deck. Wrap a string once, at the edge."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.figures: dict[str, str] = {}
        """Fact id -> the display string drawn for it."""
        self.charts: dict[str, dict[str, dict[str, float]]] = {}
        """Chart id -> series name -> category -> the value drawn."""

    def t(self, value: object) -> str:
        text = "" if value is None else str(value)
        self.seen.append(text)
        return text

    def f(self, fact: Fact) -> str:
        """Draw a fact: its display string, as the deck prints it, recorded by id."""
        self.figures[fact.id] = fact.display
        return self.t(fact.display)

    def chart(self, chart: ChartData) -> ChartData:
        self.charts[chart.id] = {
            series.name: dict(zip(chart.categories, series.values, strict=False))
            for series in chart.series
        }
        return chart


_TOKEN = re.compile(r"\{\{|\}\}")


def gate_texts(
    texts: Sequence[str],
    *,
    private_names: Pseudonyms | None,
    real_names: Callable[[str], list[str]] | None,
) -> None:
    """Refuse the file if any drawn string is a placeholder, a token, or a name that
    must not appear in this mode. Same rules as the deck's gate, on the PDF's own
    strings rather than on shapes. `real_names` is the demo check
    (`demo.real_name_checker`): the mapping's whole-token survivors and every name
    the review holds found as a substring, case-insensitive, in any string drawn.
    """
    problems: list[str] = []
    for text in texts:
        if _TOKEN.search(text):
            problems.append(f"fact token left in text: {text[:60]!r}")
        lowered = text.casefold()
        for mark in NOTE_PLACEHOLDERS:
            if mark in text if mark == "XX" else mark.casefold() in lowered:
                problems.append(f"template placeholder {mark!r} in text: {text[:60]!r}")
        if private_names is not None:
            for name in private_names.survivors(text):
                problems.append(f"a contact's name while names are private: {name!r}")
        if real_names is not None:
            for name in real_names(text):
                problems.append(f"a real name in a demo render: {name!r}")
    if problems:
        raise RenderGateError(sorted(set(problems)))


# --- reading the review -------------------------------------------------------------------


def _fact(document: ReviewDocument, fact_id: str) -> Fact | None:
    return document.facts.facts.get(fact_id)


def _number(document: ReviewDocument, fact_id: str) -> float | None:
    return document.facts.number(fact_id)


def _table(document: ReviewDocument, table_id: str) -> FactTable | None:
    return document.facts.tables.get(table_id)


def _chart(document: ReviewDocument, chart_id: str) -> ChartData | None:
    return document.facts.charts.get(chart_id)


def monthly_section(document: ReviewDocument) -> Section | None:
    return next((s for s in document.sections if s.key == MONTHLY_SECTION), None)


def user_labels(document: ReviewDocument) -> dict[str, str]:
    """Logged-in users as `User A`, `User B` by their order in the review, for a
    render that keeps people private (the deck's contact setting)."""
    users = document.facts.names.get("users", [])
    return {name: f"User {contact_label(index).split()[-1]}" for index, name in enumerate(users)}


def private_names_in(document: ReviewDocument) -> Pseudonyms:
    """Every contact and logged-in user the review names: what a private render
    must not print. The deck's gate checks contacts; the PDF has users too."""
    names = set(document.facts.names.get("contacts", [])) | set(
        document.facts.names.get("users", [])
    )
    chart = _chart(document, CONTACT_CHART)
    if chart is not None:
        names.update(name for name in chart.categories if name not in NOT_A_PERSON)
    return Pseudonyms(exact={name: "" for name in names if name})


# --- drawing helpers ---------------------------------------------------------------------------


def _line_chart(chart: ChartData, width: float, height: float, tx: Texts) -> Drawing:
    tx.chart(chart)
    drawing = Drawing(width, height)
    plot = LinePlot()
    plot.x, plot.y = 40, 30
    plot.width, plot.height = width - 60, height - 50  # type: ignore[assignment]
    plot.data = [[(i + 1, v) for i, v in enumerate(series.values)] for series in chart.series] or [
        [(1, 0)]
    ]
    top = max((v for series in chart.series for v in series.values), default=0)
    plot.yValueAxis.valueMin = 0
    plot.yValueAxis.valueMax = max(5, math.ceil(top * 1.15))
    plot.xValueAxis.valueMin = 1
    plot.xValueAxis.valueMax = max(2, len(chart.categories))
    plot.xValueAxis.valueStep = 1 if len(chart.categories) <= 16 else 5
    plot.xValueAxis.labels.fontName = plot.yValueAxis.labels.fontName = "Helvetica"
    plot.xValueAxis.labels.fontSize = plot.yValueAxis.labels.fontSize = 6.5
    plot.xValueAxis.labels.fillColor = plot.yValueAxis.labels.fillColor = MUTED
    plot.xValueAxis.strokeColor = plot.yValueAxis.strokeColor = RULE
    plot.yValueAxis.visibleGrid = True
    plot.yValueAxis.gridStrokeColor = RULE
    plot.xValueAxis.labelTextFormat = lambda v: str(int(v))
    for index, series in enumerate(chart.series):
        plot.lines[index].strokeColor = SERIES_COLOURS[index % len(SERIES_COLOURS)]
        plot.lines[index].strokeWidth = 1.4
        plot.lines[index].symbol = None
        tx.t(series.name)
    plot.joinedLines = True
    drawing.add(plot)
    drawing.add(
        String(
            width / 2,
            6,
            tx.t("Day of the month"),
            fontName="Helvetica",
            fontSize=6.5,
            fillColor=MUTED,
            textAnchor="middle",
        )
    )
    return drawing


def _bar_chart(
    chart: ChartData, width: float, height: float, tx: Texts, *, horizontal: bool
) -> Drawing:
    tx.chart(chart)
    drawing = Drawing(width, height)
    bars: Any = HorizontalBarChart() if horizontal else VerticalBarChart()
    bars.x, bars.y = (110 if horizontal else 40), 24
    bars.width, bars.height = width - bars.x - 20, height - 34
    bars.data = [series.values for series in chart.series] or [[0]]
    bars.categoryAxis.categoryNames = [tx.t(c) for c in chart.categories]
    bars.categoryAxis.labels.fontName = bars.valueAxis.labels.fontName = "Helvetica"
    bars.categoryAxis.labels.fontSize = bars.valueAxis.labels.fontSize = 6.5
    bars.categoryAxis.labels.fillColor = bars.valueAxis.labels.fillColor = MUTED
    bars.categoryAxis.strokeColor = bars.valueAxis.strokeColor = RULE
    bars.valueAxis.valueMin = 0
    top = max((v for series in chart.series for v in series.values), default=0)
    bars.valueAxis.valueMax = max(5, math.ceil(top * 1.15))
    bars.valueAxis.visibleGrid = True
    bars.valueAxis.gridStrokeColor = RULE
    if horizontal:
        bars.categoryAxis.labels.boxAnchor = "e"
        bars.categoryAxis.labels.dx = -4
    else:
        # Long category names (issue types) are angled so they never overprint;
        # the chart is given the room they take below the axis.
        longest = max((len(c) for c in chart.categories), default=0)
        if longest > 12:
            bars.categoryAxis.labels.boxAnchor = "ne"
            bars.categoryAxis.labels.angle = 25
            bars.categoryAxis.labels.dy = -2
            bars.y = 24 + min(60, longest * 2.2)
            bars.height = height - bars.y - 10
        else:
            bars.categoryAxis.labels.boxAnchor = "n"
            bars.categoryAxis.labels.dy = -4
            bars.categoryAxis.labels.angle = 0
    bars.bars[0].fillColor = BLUE
    bars.bars[0].strokeColor = None
    bars.barLabelFormat = "%d"
    bars.barLabels.fontName = "Helvetica"
    bars.barLabels.fontSize = 6.5
    bars.barLabels.fillColor = INK
    bars.barLabels.nudge = 6
    drawing.add(bars)
    return drawing


def _gauge(
    percent: float, display: str, label: str, target: str, tx: Texts, size: float = 120
) -> Drawing:
    drawing = Drawing(size, size + 30)
    cx, cy, radius = size / 2, size / 2 + 22, size / 2 - 14
    drawing.add(Circle(cx, cy, radius, strokeColor=RULE, strokeWidth=7, fillColor=None))
    share = max(0.0, min(100.0, percent))
    colour = GREEN if share >= 90 else AMBER if share >= 70 else RED
    if share > 0:
        # A wedge from the top, clockwise, over a white inner disc: an arc.
        drawing.add(
            Wedge(
                cx, cy, radius + 3.5, 90 - 360 * share / 100, 90, fillColor=colour, strokeColor=None
            )
        )
        drawing.add(Circle(cx, cy, radius - 3.5, strokeColor=None, fillColor=colors.white))
    drawing.add(
        String(
            cx,
            cy - 5,
            display,
            fontName="Helvetica-Bold",
            fontSize=15,
            fillColor=INK,
            textAnchor="middle",
        )
    )
    drawing.add(
        String(
            cx,
            14,
            tx.t(label),
            fontName="Helvetica",
            fontSize=7.5,
            fillColor=INK,
            textAnchor="middle",
        )
    )
    drawing.add(
        String(
            cx,
            4,
            tx.t(target),
            fontName="Helvetica",
            fontSize=6.5,
            fillColor=MUTED,
            textAnchor="middle",
        )
    )
    return drawing


def _bar(x: float, y: float, w: float, h: float, fill: Any) -> Any:
    rect: Any = Rect(x, y, w, h)
    rect.fillColor, rect.strokeColor, rect.rx, rect.ry = fill, None, 2, 2
    return rect


def _progress(percent: float, width: float, tx: Texts) -> Drawing:
    drawing = Drawing(width, 12)
    drawing.add(_bar(0, 4, width, 4, RULE))
    drawing.add(_bar(0, 4, width * max(0.0, min(100.0, percent)) / 100, 4, BLUE))
    return drawing


def _grid(rows: list[list[Any]], widths: list[float], *, head: bool = True) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1 if head else 0)
    style: list[Any] = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if head:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F6F7F9"))]
    table.setStyle(TableStyle(style))
    return table


NOT_ASSESSED = "not yet assessed"


def _card(title: str, value: str, tx: Texts, width: float, tint: Any = colors.white) -> Table:
    value_style = STYLES["big"] if value != NOT_ASSESSED else STYLES["pending"]
    inner = Table(
        [[Paragraph(tx.t(title), STYLES["sub"])], [Paragraph(tx.t(value), value_style)]],
        colWidths=[width - 12],
    )
    inner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), tint),
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return inner


def _card_with_note(title: str, value: str, note: str, tx: Texts, width: float) -> Table:
    """A summary card whose figure needs the number it is a share of.

    A percentage on its own is not checkable; "251 of 252 days" under it is.
    """
    inner = Table(
        [
            [Paragraph(tx.t(title), STYLES["sub"])],
            [Paragraph(tx.t(value), STYLES["big"])],
            [Paragraph(tx.t(note), STYLES["note"])],
        ],
        colWidths=[width],
    )
    inner.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return inner


def _trace(mode: RenderMode, ids: Iterable[str], tx: Texts) -> list[Any]:
    """Internal mode prints the ids a block rests on under it; the others print nothing."""
    if mode != "internal":
        return []
    listed = ", ".join(sorted(set(ids)))
    return [Paragraph(tx.t(f"facts: {listed}"), STYLES["trace"])] if listed else []


# --- the sections -----------------------------------------------------------------------------


@dataclass(slots=True)
class _Build:
    document: ReviewDocument
    mode: RenderMode
    tx: Texts
    hide_people: bool
    width: float
    private: Pseudonyms | None = None
    """The people a private render may not name, for free text that might."""
    sections: list[str] = field(default_factory=list)
    omitted: list[tuple[str, str]] = field(default_factory=list)

    def para(self, text: str, style: str = "body") -> Paragraph:
        return Paragraph(self.tx.t(text), STYLES[style])

    def heading(self, title: str, sub: str) -> list[Any]:
        return [self.para(title, "h1"), self.para(sub, "sub"), Spacer(1, 10)]


def _cover(b: _Build, now: dt.datetime) -> list[Any]:
    d = b.document
    story: list[Any] = []
    logo = ""
    title = next((s for s in d.sections if s.key == "title"), None)
    if title is not None and b.mode != "demo":
        logo = str(title.data.get("logo") or "")
    if logo and Path(logo).is_file():
        try:
            story.append(
                Image(logo, width=1.6 * inch, height=0.8 * inch, kind="proportional", hAlign="LEFT")
            )
        except Exception:
            story.append(Spacer(1, 0.8 * inch))
    else:
        story.append(Spacer(1, 0.8 * inch))
    organisation = str(d.franchisee.get("display_name") or d.client_name)
    story += [
        Spacer(1, 1.4 * inch),
        b.para("Monthly Report", "title"),
        Spacer(1, 8),
        b.para(f"<b>Company:</b> {d.site_name}", "cover"),
        b.para(f"<b>Organization:</b> {organisation}", "cover"),
        b.para(f"<b>Period:</b> {d.period.label}", "cover"),
        b.para(f"<b>Generated On:</b> {now:%B %d, %Y}".replace(" 0", " "), "cover"),
    ]
    section = monthly_section(d)
    if section is None or not section.approved_by:
        story += [Spacer(1, 10), b.para("Draft: not yet approved by an engineer.", "note")]
    return story


def _ticketing(b: _Build) -> list[Any]:
    d, tx = b.document, b.tx
    daily = _chart(d, "autotask.chart.daily")
    created = _fact(d, "autotask.tickets.created")
    if created is None and daily is None:
        b.omitted.append(("Ticketing", "the review holds no Autotask ticket facts"))
        return []
    b.sections.append("Ticketing")
    story: list[Any] = [PageBreak()]
    story += b.heading(
        "Ticketing", "Ticket volume, SLA performance, who raised tickets and what about"
    )

    # --- daily created vs completed ---
    if daily is not None:
        story.append(b.para("Daily tickets created vs completed", "h2"))
        story.append(_line_chart(daily, b.width, 190, tx))
        # The totals are the review's facts, the same numbers the deck's dashboard
        # prints; the series sums only stand in for a review that has no such fact.
        totals: list[str] = []
        for series, fact_id in zip(
            daily.series, ("autotask.tickets.created", "autotask.tickets.completed"), strict=False
        ):
            swatch = SERIES_COLOURS[len(totals) % len(SERIES_COLOURS)].hexval()[2:]
            fact = _fact(d, fact_id)
            total = tx.f(fact) if fact is not None else str(int(sum(series.values)))
            totals.append(f'<font color="#{swatch}">&#9632;</font> {series.name}: {total}')
        story.append(b.para("   ".join(totals), "small"))
        story += _trace(
            b.mode,
            ["autotask.chart.daily", "autotask.tickets.created", "autotask.tickets.completed"],
            tx,
        )
    else:
        story.append(
            b.para(
                "No tickets were created or completed in the period, so there is no daily chart.",
                "note",
            )
        )
    story.append(Spacer(1, 12))

    # --- gauges ---
    target = d.franchisee.get("sla_target_percent")
    target_text = (
        f"Target: {float(target):g}%" if isinstance(target, int | float) else "Target: not set"
    )
    gauges: list[Any] = []
    definitions: list[Any] = []
    for fact_id, label in (
        ("autotask.sla.first_response_met_percent", "First response met"),
        ("autotask.sla.resolution_met_percent", "SLA met (resolution)"),
    ):
        fact = _fact(d, fact_id)
        measured = _fact(d, fact_id.replace("_met_percent", "_measured"))
        if fact is None or not isinstance(fact.value, int | float):
            definitions.append(
                b.para(
                    f"{label}: not measured this period (no ticket carried the due time).", "note"
                )
            )
            continue
        gauges.append(_gauge(float(fact.value), tx.f(fact), label, target_text, tx))
        out_of = f" Out of {tx.f(measured)} tickets." if measured is not None else ""
        definitions.append(b.para(f"<b>{label}.</b> {fact.definition}{out_of}", "small"))
        definitions += _trace(b.mode, [fact_id, measured.id if measured else fact_id], tx)
    story.append(b.para("SLA performance", "h2"))
    if gauges:
        row = Table([gauges], colWidths=[140] * len(gauges), hAlign="LEFT")
        story.append(row)
    story += definitions
    story.append(PageBreak())

    # --- by contact ---
    contacts = _chart(d, CONTACT_CHART)
    automated = _fact(d, "autotask.tickets.automated")
    people = _fact(d, "autotask.tickets.from_people")
    left: list[Any] = [b.para("Tickets created by contact", "h2")]
    if contacts is not None:
        shown = anonymised_contacts(contacts) if b.hide_people else contacts
        people_only = [
            (name, value)
            for name, value in zip(shown.categories, shown.series[0].values, strict=True)
            if name not in NOT_A_PERSON
        ]
        chart = shown.model_copy(
            update={
                "categories": [n for n, _ in people_only],
                "series": [
                    shown.series[0].model_copy(update={"values": [v for _, v in people_only]})
                ],
            }
        )
        if people_only:
            left.append(
                _bar_chart(chart, b.width * 0.55, 150 + 8 * len(people_only), tx, horizontal=True)
            )
        else:
            left.append(
                b.para("No tickets were raised by the customer's people in the period.", "note")
            )
        counts = []
        if people is not None:
            counts.append(f"Tickets from people: {tx.f(people)}")
        if automated is not None:
            counts.append(
                f"Automated and integration contacts, shown separately: {tx.f(automated)}"
            )
        left.append(b.para(" · ".join(counts), "small"))
        left.append(
            b.para(
                f"The {len(people_only)} contacts with the most tickets created in the period; "
                "monitoring, automation and integration accounts are counted separately above.",
                "small",
            )
        )
        left += _trace(
            b.mode,
            [CONTACT_CHART, "autotask.tickets.from_people", "autotask.tickets.automated"],
            tx,
        )
    else:
        left.append(b.para("The review holds no tickets-by-contact chart for this period.", "note"))
        b.omitted.append(("Ticketing: tickets by contact", "no chart recorded"))
    story += left
    story.append(Spacer(1, 12))

    # --- by issue type ---
    issue = _chart(d, "autotask.chart.issue_type")
    story.append(b.para("Tickets by issue type", "h2"))
    if issue is not None:
        total = tx.f(created) if created is not None else ""
        if total:
            story.append(b.para(f"Total tickets created: {total}", "small"))
        story.append(_bar_chart(issue, b.width * 0.7, 240, tx, horizontal=False))
        story += _trace(b.mode, ["autotask.chart.issue_type", "autotask.tickets.created"], tx)
        unset = _fact(d, "autotask.tickets.issue_type_unset_share")
        sub = _chart(d, "autotask.chart.sub_issue_type")
        if unset is not None and isinstance(unset.value, int | float) and unset.value > 50:
            if sub is not None:
                story.append(
                    b.para(
                        f"Sub-issue types ({tx.f(unset)} of tickets have no issue type "
                        "or are filed as Other)",
                        "h2",
                    )
                )
                story.append(_bar_chart(sub, b.width * 0.7, 240, tx, horizontal=False))
                story += _trace(b.mode, ["autotask.chart.sub_issue_type", unset.id], tx)
            else:
                story.append(
                    b.para(
                        f"{tx.f(unset)} of tickets have no issue type or are filed as "
                        "Other, and the sub-issue type is largely unset too, so no breakdown "
                        "is shown.",
                        "note",
                    )
                )
                story += _trace(b.mode, [unset.id], tx)
    else:
        story.append(
            b.para("The review holds no tickets-by-issue-type chart for this period.", "note")
        )
        b.omitted.append(("Ticketing: tickets by issue type", "no chart recorded"))
    return story


def _patch_card(b: _Build, title: str, ids: dict[str, str], failed_list: str) -> list[Any]:
    d, tx = b.document, b.tx
    installed = _fact(d, ids["installed"])
    failed = _fact(d, ids["failed"])
    if installed is None and failed is None:
        return [b.para(f"{title}: not recorded in this review.", "note")]
    attempted = _fact(d, ids["attempted"])
    total = tx.f(attempted) if attempted is not None else "0"
    success_fact = _fact(d, ids["success"])
    success = tx.f(success_fact) if success_fact is not None else "n/a"
    top = _fact(d, ids["top"])
    failed_n = (
        int(failed.value) if failed is not None and isinstance(failed.value, int | float) else 0
    )
    failed_devices = _fact(d, ids["failed_devices"])
    badge = (
        "All devices successful"
        if failed_n == 0
        else f"{tx.f(failed_devices) if failed_devices is not None else '?'} device(s) failed"
    )
    # A device name is not a person: the top failed device stays in every mode.
    top_text = tx.f(top) if top is not None else "No failed devices"
    head = Table(
        [
            [
                Paragraph(tx.t(title), STYLES["h2"]),
                Paragraph(
                    tx.t(badge),
                    ParagraphStyle(
                        "badge",
                        parent=STYLES["small"],
                        textColor=GREEN_DARK if failed_n == 0 else RED,
                        alignment=2,
                    ),
                ),
            ]
        ],
        colWidths=[b.width * 0.30, b.width * 0.16],
    )
    numbers = Table(
        [
            [
                Paragraph(
                    tx.f(installed) if installed else tx.t("0"),
                    ParagraphStyle("g", parent=STYLES["big"], textColor=GREEN_DARK),
                ),
                Paragraph(
                    tx.f(failed) if failed else tx.t("0"),
                    ParagraphStyle("r", parent=STYLES["big"], textColor=RED if failed_n else INK),
                ),
            ],
            [
                Paragraph(
                    tx.t("Successful"),
                    ParagraphStyle("c1", parent=STYLES["small"], alignment=TA_CENTER),
                ),
                Paragraph(
                    tx.t("Failed"),
                    ParagraphStyle("c2", parent=STYLES["small"], alignment=TA_CENTER),
                ),
            ],
        ],
        colWidths=[b.width * 0.23] * 2,
    )
    numbers.setStyle(
        TableStyle(
            [("BACKGROUND", (0, 0), (-1, -1), colors.white), ("BOX", (0, 0), (-1, -1), 0.5, RULE)]
        )
    )
    facts_rows = [
        [
            Paragraph(tx.t("Total patches:"), STYLES["small"]),
            Paragraph(tx.t(total), ParagraphStyle("ra", parent=STYLES["cell"], alignment=2)),
        ],
        [
            Paragraph(tx.t("Top failed device:"), STYLES["small"]),
            Paragraph(tx.t(top_text), ParagraphStyle("rb", parent=STYLES["cell"], alignment=2)),
        ],
        [
            Paragraph(tx.t("Success rate"), STYLES["small"]),
            Paragraph(
                tx.t(success),
                ParagraphStyle("rc", parent=STYLES["cell"], alignment=2, textColor=BLUE),
            ),
        ],
    ]
    facts = Table(facts_rows, colWidths=[b.width * 0.23, b.width * 0.23])
    rate = _number(d, ids["success"])
    bar = _progress(rate if rate is not None else 0.0, b.width * 0.46, tx)
    block: list[Any] = [head, Spacer(1, 4), numbers, Spacer(1, 4), facts, bar]
    listed = _table(d, failed_list)
    if listed is not None and listed.rows:
        block.append(Paragraph(tx.t("Failed devices:"), STYLES["small"]))
        rows = [[Paragraph(tx.t(c), STYLES["cell"]) for c in listed.columns]] + [
            [Paragraph(tx.t(c), STYLES["cell"]) for c in row] for row in listed.rows[:12]
        ]
        block.append(_grid(rows, [b.width * 0.34, b.width * 0.12]))
        if len(listed.rows) > 12:
            block.append(Paragraph(tx.t(f"and {len(listed.rows) - 12} more"), STYLES["small"]))
    block += _trace(b.mode, [*ids.values(), failed_list], tx)
    outer = Table([[block]], colWidths=[b.width * 0.48])
    outer.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FAFBFC")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return [outer]


def _age_band(text: str) -> str:
    """The sample's age bands: under 4 years, 4 to 6, more than 6; unknown when blank."""
    try:
        years = int(text)
    except ValueError:
        return "unknown"
    return "healthy" if years < 4 else "low" if years <= 6 else "critical"


LIFECYCLE_WIDTHS = (0.12, 0.09, 0.06, 0.09, 0.10, 0.05, 0.12, 0.08, 0.06, 0.06, 0.06, 0.055, 0.055)
HEADINGS = {"Est. age (years)": "Est. age (yrs)"}
STORAGE_COLUMNS = ("Storage used", "Storage total", "Storage free", "Free %")
AGE_COLUMN = "Est. age (years)"


def _lifecycle(b: _Build, table_id: str, title: str, users: dict[str, str]) -> list[Any]:
    d, tx = b.document, b.tx
    table = _table(d, table_id)
    if table is None:
        b.omitted.append((f"Monitoring: {title}", "the review holds no lifecycle table"))
        return [b.para(f"{title}: the review holds no lifecycle table for this period.", "note")]
    story: list[Any] = [b.para(title, "h2")]
    if not table.rows:
        # The collector wrote the table and found nothing for it: a statement, not a
        # blank grid. The word is the table's: servers or workstations.
        what = "servers" if table_id == LIFECYCLE_SERVERS else "workstations"
        story.append(b.para(f"No {what} at this site.", "body"))
        story.append(b.para(table.definition, "small"))
        story += _trace(b.mode, [table_id], tx)
        return story
    legend = (
        "Estimated age: <font color='#1E8E5A'>less than 4 years</font> · "
        "<font color='#E8A33D'>4 to 6 years</font> · <font color='#D9534F'>more than 6 "
        "years</font>   |   Storage, by free share and the row's tint: "
        "<font color='#1E8E5A'>healthy (40%+ free)</font> · "
        "<font color='#E8A33D'>low (20 to 39% free)</font> · "
        "<font color='#D9534F'>critical (below 20% free)</font>"
    )
    story.append(b.para(legend, "small"))
    columns = table.columns
    idx = {name: i for i, name in enumerate(columns)}
    widths = [b.width * w for w in LIFECYCLE_WIDTHS][: len(columns)]
    if len(columns) > len(LIFECYCLE_WIDTHS):
        widths = [b.width / len(columns)] * len(columns)
    rows: list[list[Any]] = [
        [Paragraph(tx.t(HEADINGS.get(c, c)), STYLES["cellhead"]) for c in columns]
    ]
    styles: list[Any] = []
    for number, row in enumerate(table.rows, start=1):
        cells = list(row)
        if b.hide_people and "Last logged-in user" in idx:
            name = cells[idx["Last logged-in user"]]
            cells[idx["Last logged-in user"]] = users.get(
                name, name if name == NOT_REPORTED else "User"
            )
        if b.private is not None and "Location" in idx:
            # A NinjaOne location is free text and can be named after a person
            # ("Dana's office"); while people are private it is withheld too.
            if b.private.survivors(cells[idx["Location"]]):
                cells[idx["Location"]] = "Location withheld (named after a person)"
        share_text = cells[idx["Free %"]] if "Free %" in idx else NOT_REPORTED
        try:
            share = float(share_text.rstrip("%")) if share_text != NOT_REPORTED else None
        except ValueError:
            share = None
        band = free_band(share)
        styles.append(("BACKGROUND", (0, number), (-1, number), BAND_FILL[band]))
        rendered = []
        for i, cell in enumerate(cells):
            style = STYLES["cell"]
            cell_band = "unknown"
            if columns[i] in STORAGE_COLUMNS:
                cell_band = band
            elif columns[i] == AGE_COLUMN:
                cell_band = _age_band(cell)
            if cell_band != "unknown":
                style = ParagraphStyle(
                    f"band{number}{i}", parent=STYLES["cell"], textColor=BAND_INK[cell_band]
                )
            rendered.append(Paragraph(tx.t(cell), style))
        rows.append(rendered)
    grid = _grid(rows, widths)
    grid.setStyle(TableStyle(styles))
    story.append(grid)
    story.append(b.para(table.definition, "small"))
    story += _trace(b.mode, [table_id], tx)
    return story


def _monitoring(b: _Build, users: dict[str, str]) -> list[Any]:
    d = b.document
    has_patches = any(_fact(d, i) for i in ("ninjaone.patches.installed", SOFTWARE_INSTALLED))
    has_lifecycle = any(_table(d, i) for i in (LIFECYCLE_WORKSTATIONS, LIFECYCLE_SERVERS))
    if not has_patches and not has_lifecycle:
        b.omitted.append(("Monitoring", "the review holds no NinjaOne patch or lifecycle facts"))
        return []
    b.sections.append("Monitoring")
    story: list[Any] = [PageBreak()]
    story += b.heading(
        "Monitoring", "Patch installations for the period, and the machines' age and storage"
    )
    story.append(b.para("Status of patches deployed", "h2"))
    os_card = _patch_card(
        b,
        "Status of OS patches",
        {
            "installed": "ninjaone.patches.installed",
            "failed": "ninjaone.patches.failed",
            "attempted": "ninjaone.patches.attempted",
            "success": "ninjaone.patches.success_percent",
            "failed_devices": "ninjaone.patches.failed_devices",
            "top": OS_TOP_FAILED,
        },
        OS_FAILED_LIST,
    )
    sw_card = _patch_card(
        b,
        "Status of third-party patches",
        {
            "installed": SOFTWARE_INSTALLED,
            "failed": SOFTWARE_FAILED,
            "attempted": SOFTWARE_ATTEMPTED,
            "success": SOFTWARE_SUCCESS,
            "failed_devices": SOFTWARE_FAILED_DEVICES,
            "top": SOFTWARE_TOP_FAILED,
        },
        SOFTWARE_FAILED_LIST,
    )
    cards = Table([[os_card, sw_card]], colWidths=[b.width * 0.5, b.width * 0.5])
    cards.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(cards)
    story.append(PageBreak())
    story += _lifecycle(b, LIFECYCLE_WORKSTATIONS, "Workstation lifecycle details", users)
    story.append(PageBreak())
    story += _lifecycle(b, LIFECYCLE_SERVERS, "Server lifecycle details", users)
    story += _role_disagreements(b)
    return story


def _role_disagreements(b: _Build) -> list[Any]:
    """Internal only: the devices the operating system counted differently from
    NinjaOne's node class, so an engineer can see why the counts are what they are."""
    table = _table(b.document, ROLE_DISAGREEMENTS)
    if b.mode != "internal" or table is None:
        return []
    story: list[Any] = [Spacer(1, 14), b.para(table.label, "h2")]
    if not table.rows:
        story.append(b.para("None: every device's node class agrees with its OS.", "note"))
    else:
        rows = [[Paragraph(b.tx.t(c), STYLES["cellhead"]) for c in table.columns]] + [
            [Paragraph(b.tx.t(c), STYLES["cell"]) for c in row] for row in table.rows
        ]
        story.append(_grid(rows, [b.width * w for w in (0.22, 0.34, 0.24, 0.20)]))
    story.append(b.para(table.definition, "small"))
    story += _trace(b.mode, [ROLE_DISAGREEMENTS], b.tx)
    return story


def _backup(b: _Build) -> list[Any]:
    """Backup coverage, status and storage, from N-able Cove.

    The page carries numbers and the words needed to act on them; what each
    number counts is one footnote at the end of the report, because a
    method paragraph under every table is a page nobody finishes.
    """
    d, tx = b.document, b.tx
    b.sections.append("Backup")
    story: list[Any] = [PageBreak()]
    story += b.heading("Backup", "Backup coverage, status and storage")

    total = _fact(d, "cove.devices.total")
    if total is None:
        gap = next((g for g in d.facts.gaps if g.source == "cove"), None)
        why = (
            gap.why
            if gap
            else "This site has no N-able Cove customer mapped, so backup is not assessed."
        )
        machines = _fact(d, "ninjaone.devices.workstations")
        story.append(b.para("Not yet assessed", "h2"))
        here = f" NinjaOne manages {tx.f(machines)} workstations at this site." if machines else ""
        story.append(b.para(f"{why}{here}", "note"))
        return story

    story.append(b.para(tx.f(total), "h2"))

    # --- the two figures somebody reads first ---------------------------------
    rate = _fact(d, "cove.success.rate")
    days = _fact(d, "cove.success.days")
    good = _fact(d, "cove.success.good_days")
    storage = _fact(d, "cove.storage.used")
    machines_count = _fact(d, "cove.devices.machines")
    has_machines = bool(machines_count and machines_count.value)

    cards = [_card("Used storage", tx.f(storage) if storage else NOT_ASSESSED, tx, b.width / 2 - 8)]
    if rate is not None and days is not None and good is not None:
        cards.append(
            _card_with_note(
                rate.label, tx.f(rate), f"{tx.f(good)} of {tx.f(days)} days", tx, b.width / 2 - 8
            )
        )
    grid = Table([cards], colWidths=[b.width / 2] * 2)
    grid.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)])
    )
    story += [Spacer(1, 6), grid, Spacer(1, 10)]

    if not has_machines:
        story.append(b.para("No computers are backed up by Cove; Microsoft 365 only.", "body"))
    elif rate is None:
        # Say which of the two reasons it is: no history to judge, or every
        # machine here reported as possibly retired.
        why = next((g.why for g in d.facts.gaps if g.source == "cove"), "")
        story.append(b.para(why or "No success rate for these machines.", "note"))

    unreadable = _fact(d, "cove.success.unreadable_days")
    if (
        unreadable is not None
        and days is not None
        and isinstance(unreadable.value, int)
        and unreadable.value
    ):
        story.append(
            b.para(
                f"The rate rests on {tx.f(days)} days: {tx.f(unreadable)} could not be read.",
                "note",
            )
        )

    # --- Microsoft 365 --------------------------------------------------------
    tenants = _fact(d, "cove.devices.tenants")
    if tenants is not None and tenants.value:
        m365 = [
            _card(
                label,
                tx.f(fact) if (fact := _fact(d, fact_id)) else NOT_ASSESSED,
                tx,
                b.width / 3 - 8,
            )
            for fact_id, label in (
                ("cove.mailboxes.user", "User mailboxes"),
                ("cove.mailboxes.shared", "Shared mailboxes"),
                ("cove.onedrive.users", "OneDrive users"),
            )
        ]
        row = Table([m365], colWidths=[b.width / 3] * 3)
        row.setStyle(
            TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)])
        )
        story += [row, Spacer(1, 10)]
    else:
        story.append(b.para("No Microsoft 365 backup.", "note"))

    trend = _chart(d, "cove.success.trend")
    if trend is not None:
        story.append(Spacer(1, 4))
        story.append(_line_chart(trend, b.width, 120, tx))
        story.append(_numbers_under(trend, b.width, tx))

    # --- the lists, in the order somebody works them --------------------------
    for table_id in (
        "cove.attention",
        "cove.worst_days",
        "cove.retired",
        "cove.devices.list",
        "cove.retention",
    ):
        found = _table(d, table_id)
        if found is None or not found.rows:
            continue
        story.append(Spacer(1, 10))
        story.append(b.para(found.label, "h2"))
        story.append(_table_grid(found, b.width, tx))

    # --- the coverage gap, or the fact that nobody has been through it --------
    story.append(Spacer(1, 10))
    story.append(b.para("Not backed up by Cove", "h2"))
    scope = _fact(d, "cove.coverage.scope")
    if scope is not None:
        story.append(b.para(tx.f(scope), "note"))
    # How we measured, when it changed. Never on the customer's copy: it is
    # ours to explain, not theirs to puzzle over.
    changed = _fact(d, "cove.coverage.scope_changed")
    if changed is not None and b.mode != "client":
        story.append(b.para(tx.f(changed), "note"))
    reviewed = _fact(d, "cove.coverage.reviewed")
    pending = reviewed is not None and reviewed.value == "no" and b.mode == "client"
    not_backed_up = _table(d, "cove.not_backed_up")
    coverage = _fact(d, BACKUP_COVERAGE)
    if pending:
        story.append(b.para("Coverage review pending.", "body"))
    elif not_backed_up is not None and not_backed_up.rows:
        story.append(_table_grid(not_backed_up, b.width, tx))
        if coverage is not None:
            story.append(b.para(f"{tx.f(coverage)} of machines have a Cove device.", "note"))
    elif coverage is not None:
        story.append(b.para(f"{tx.f(coverage)} of machines have a Cove device.", "body"))
    else:
        # Three different reasons there is no list, and they are not the same
        # thing: nothing to measure, nothing missing, or nothing read.
        measured = _fact(d, "cove.coverage.machines")
        if measured is None:
            story.append(b.para("Not assessed: NinjaOne did not answer for this site.", "note"))
        elif not measured.value:
            story.append(
                b.para(
                    "No machines to measure against, under this site's backup settings.",
                    "body",
                )
            )
        else:
            story.append(b.para("Every machine measured has a Cove device.", "body"))

    return story


def _backup_notes(b: _Build) -> list[Any]:
    """What the Backup section's numbers count, once, at the end.

    The page itself carries figures and the words needed to act on them. The
    definitions live here because a method paragraph under every table is a
    page nobody finishes — and leaving them out altogether would be a report
    whose numbers cannot be checked.
    """
    tx = b.tx
    if _fact(b.document, "cove.devices.total") is None:
        return []
    rate = _fact(b.document, "cove.success.rate")
    window = rate.label.lower() if rate else "the last 28 days"
    lines = [
        f"<b>Backup success</b> is measured over {window}, which is the history N-able Cove "
        "keeps. It is the state when this review was generated, not the review period.",
        "A day counts as a success when the backup completed, including one that completed "
        "with errors. A day that failed, was interrupted or was aborted, and a day with no "
        "session at all, both count against it.",
        "The share is of the days each machine could have backed up, so a machine younger "
        "than the window is measured over its own life. A session still running today, and a "
        "day whose entry could not be read, count for neither side.",
        "<b>Devices</b>: Cove counts a customer's Microsoft 365 tenant as a device of its own. "
        "It is listed as a tenant and is in no count of machines.",
        "<b>Possibly retired</b>: a machine Cove holds that no NinjaOne device matches and "
        "that has not backed up for 30 days or more. Left out of the success rate and of "
        "Needs attention.",
        "<b>Not backed up by Cove</b>: a NinjaOne machine with no Cove device paired to it, "
        "matched by name with Cove's own suffix removed. A name rule, so this is what Cove "
        "appears not to hold rather than proof a machine has no backup.",
    ]
    story: list[Any] = [Spacer(1, 16), b.para("About the backup figures", "h2")]
    story += [b.para(line, "note") for line in lines]
    # The fact-id trace goes under the notes rather than mid-page: it is for
    # an engineer checking a number, and it is the last thing anybody reads.
    story += _trace(
        b.mode,
        [
            "cove.devices.total",
            "cove.devices.machines",
            "cove.devices.tenants",
            "cove.storage.used",
            "cove.mailboxes.user",
            "cove.mailboxes.shared",
            "cove.onedrive.users",
            "cove.retention.policies",
            "cove.success.rate",
            "cove.success.days",
            "cove.success.good_days",
            "cove.success.warning_days",
            "cove.success.unreadable_days",
            "cove.stale.count",
            "cove.retired.count",
            "cove.coverage.machines",
            BACKUP_COVERAGE,
            BACKUP_TABLE,
            "ninjaone.devices.workstations",
        ],
        tx,
    )
    return story


def _table_grid(found: FactTable, width: float, tx: Texts) -> Table:
    """One fact table as a grid, every cell code-written."""
    rows = [[Paragraph(tx.t(c), STYLES["cellhead"]) for c in found.columns]] + [
        [Paragraph(tx.t(c), STYLES["cell"]) for c in row] for row in found.rows
    ]
    return _grid(rows, [width / len(found.columns)] * len(found.columns))


def _numbers_under(chart: ChartData, width: float, tx: Texts) -> Table:
    """The chart's own numbers, printed. Length is never the only way to read
    a figure, which is the rule every other chart here follows."""
    head = [Paragraph(tx.t(""), STYLES["cellhead"])] + [
        Paragraph(tx.t(c), STYLES["cellhead"]) for c in chart.categories
    ]
    body = [
        [Paragraph(tx.t(series.name), STYLES["cell"])]
        + [Paragraph(f"{value:g}%", STYLES["cell"]) for value in series.values]
        for series in chart.series
    ]
    columns = len(chart.categories) + 1
    return _grid([head, *body], [width / columns] * columns)


# --- the render ----------------------------------------------------------------------------------


def render_pdf(
    document: ReviewDocument,
    output: Path,
    *,
    mode: RenderMode = "client",
    now: dt.datetime | None = None,
) -> PdfResult:
    """Write the Monthly Report for a stored review. Refuses a client render of an
    unapproved review, and any render the gate refuses."""
    now = now or dt.datetime.now(dt.UTC)
    section = monthly_section(document)
    approved = section is not None and section.approved_by is not None
    if mode == "client" and not approved:
        raise RenderGateError(
            [
                "the Monthly Report has not been approved by an engineer"
                if section is not None
                else "this review predates the Monthly Report; generate it again"
            ]
        )
    hide_people = mode != "demo" and not document.people_names_shown
    private = private_names_in(document) if hide_people else None
    check_real: Callable[[str], list[str]] | None = None
    if mode == "demo":
        terms = real_name_terms(document)  # from the real review, before anything is replaced
        document, names = pseudonymise(document)
        check_real = real_name_checker(names, terms)
    tx = Texts()
    width = PAGE[0] - 2 * MARGIN
    build = _Build(
        document=document,
        mode=mode,
        tx=tx,
        hide_people=hide_people,
        width=width,
        private=private,
    )
    users = user_labels(document) if hide_people else {}

    story: list[Any] = []
    story += _cover(build, now)
    story += _ticketing(build)
    story += _monitoring(build, users)
    story += _backup(build)
    story += _backup_notes(build)

    stamp = STAMP if mode == "demo" else ""
    draft = "" if approved else "Draft: not approved"
    footer_left = tx.t(f"{document.site_name} · Monthly Report · {document.period.label}")

    def on_page(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 0.3 * inch, footer_left)
        canvas.drawRightString(PAGE[0] - MARGIN, 0.3 * inch, f"Page {doc.page}")
        canvas.setStrokeColor(GREEN_DARK)
        canvas.setLineWidth(3)
        canvas.line(MARGIN, 0.45 * inch, PAGE[0] - MARGIN, 0.45 * inch)
        if stamp or draft:
            canvas.setFont("Helvetica-Bold", 9)
            canvas.setFillColor(RED)
            canvas.drawCentredString(
                PAGE[0] / 2, PAGE[1] - 0.3 * inch, " · ".join(x for x in (stamp, draft) if x)
            )
        canvas.restoreState()

    tx.t(stamp)
    tx.t(draft)
    gate_texts(
        tx.seen,
        private_names=private,
        real_names=check_real,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output),
        pagesize=PAGE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=0.6 * inch,
        title=f"Monthly Report {document.period.label}",
        author=str(document.franchisee.get("display_name") or document.client_name),
    )
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    pages = int(getattr(doc, "page", 0)) or _count_pages(output)
    return PdfResult(
        path=output,
        pages=pages,
        mode=mode,
        sections=list(build.sections),
        omitted=list(build.omitted),
        texts_checked=len(tx.seen),
        figures=dict(tx.figures),
        charts=dict(tx.charts),
    )


def _count_pages(path: Path) -> int:
    data = path.read_bytes()
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


def default_pdf_output(
    document: ReviewDocument, directory: Path, *, now: dt.datetime, mode: RenderMode = "client"
) -> Path:
    last = document.period.end - dt.timedelta(days=1)
    who = "sample-client" if mode == "demo" else document.site_slug
    suffix = "" if mode == "client" else f"-{mode}"
    return directory / f"{who}-monthly-{last:%Y-%m}-{now:%Y%m%d-%H%M%S}{suffix}.pdf"


def keep_together(items: list[Any]) -> KeepTogether:
    return KeepTogether(items)

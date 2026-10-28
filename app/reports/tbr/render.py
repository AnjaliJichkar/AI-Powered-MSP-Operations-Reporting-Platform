"""Render a reviewed business review into the 2026 template with python-pptx.

What is preserved: every slide's layout, title, position in the deck and the
TeamLogic branding. Nothing is drawn from scratch except the charts on slide 6,
which the template has no native version of (its dashboard is a pasted
screenshot), so they are created with `add_chart` in the screenshot's place.

How text is replaced: a placeholder body is not edited run by run. Its
paragraphs are removed and rebuilt from the section's items, reusing the
template's own paragraph properties for each bullet level (so bullets, indents
and spacing are the template's) with two changes — the red default run colour
is stripped, and every run written carries an explicit colour and size. That
way no run can inherit red from the paragraph default or the shape's list
style, whichever of the three levels the template set it at.

What is dropped: a slide whose section is not renderable is removed from the
deck, its divider goes with it when the whole section is empty, and its agenda
line is removed. Nothing blank or sample is ever shipped (slide map, rule 5).

How text is sized: a model-written slide's body is measured against its
placeholder before it is written (`layout.fit`), with the template's font and
spacing, and takes the largest size in its budget's ladder that fits; if even
the floor does not fit, whole items go from the least serious end with a
code-written line saying so, then sentences. Sizing by counting paragraphs is
what put a fifth service under the footer, so a model-written slide is never
sized that way now. The code-written slides (accomplishments, replacement,
upgrade, budget) still take a size from their line count: their lines are
short and fixed in shape.

The last step is `gate.enforce` on the presentation about to be saved. If it
finds anything, no file is written.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from app.reports.tbr import gate, layout
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
from app.reports.tbr.model import (
    ChartData,
    Item,
    ReviewDocument,
    ScorecardRow,
    Section,
)
from app.reports.tbr.sections import DIVIDERS, SLIDES

TEMPLATE = gate.TEMPLATE

TEXT_COLOUR = "000000"
BRAND_GREEN = RGBColor(0x8B, 0xAE, 0x6C)
BRAND_BLUE = RGBColor(0x00, 0x95, 0xDA)
BRAND_DARK_GREEN = RGBColor(0x01, 0x87, 0x52)
SERIES_COLOURS = (BRAND_BLUE, BRAND_GREEN, BRAND_DARK_GREEN)

#: Agenda line (as the template words it) -> the slides that keep it on the agenda.
AGENDA: dict[str, tuple[int, ...]] = {
    "industry trends": (3,),
    "services review": (4,),
    "open technical items": (10,),
    "strategic initiatives": (16,),
    "implementation timeline": (21,),
    "budget summary": (22,),
    "management summary report of technical infrastructure": (7,),
    "completed ticket performance by issue type": (6,),
}
AGENDA_PARENTS = {"reports": ("management summary", "completed ticket performance")}


#: Who the deck is for.
#:
#: - `client` (the default): what is handed to the customer. Speaker notes are empty
#:   unless an engineer wrote a talk track for the slide. No fact ids, no definitions,
#:   none of the template's own presenter instructions.
#: - `internal`: the same slides, with the fact-id trace of every statement in the
#:   notes, for the engineer preparing to present or checking a figure.
#: - `demo`: client-style notes, every real name replaced by a consistent pseudonym,
#:   every number kept, and "Sample data" stamped on every slide.
#:
#: The trace itself never depends on the mode: it lives on the stored review (each
#: item's `cites`, each fact's definition) and is what the review screen shows.
RenderMode = Literal["client", "internal", "demo"]


@dataclass(slots=True)
class Para:
    text: str = ""
    lead: str | None = None
    level: int = 0
    bullet: bool = True
    bold: bool = False
    italic: bool = False
    size: float | None = None
    space_before: int | None = None


@dataclass(slots=True)
class RenderResult:
    path: Path
    kept: list[int]
    dropped: list[int]
    gate: gate.GateReport
    warnings: list[str] = field(default_factory=list)
    mode: RenderMode = "client"


# --- low-level text ------------------------------------------------------------------------


def _run(paragraph: Any, text: str, *, size: float | None, bold: bool, italic: bool) -> None:
    run = etree.SubElement(paragraph, qn("a:r"))
    properties = etree.SubElement(run, qn("a:rPr"))
    properties.set("lang", "en-US")
    properties.set("dirty", "0")
    if size:
        properties.set("sz", str(round(size * 100)))
    properties.set("b", "1" if bold else "0")
    properties.set("i", "1" if italic else "0")
    fill = etree.SubElement(properties, qn("a:solidFill"))
    etree.SubElement(fill, qn("a:srgbClr")).set("val", TEXT_COLOUR)
    etree.SubElement(run, qn("a:t")).text = text


def _prototypes(body: Any, *, inherits_bullets: bool) -> dict[tuple[int, bool], Any]:
    """The template's own paragraph properties per (level, bulleted), minus any red.

    A paragraph that declares neither a bullet nor `buNone` takes whatever its
    shape inherits: a content placeholder inherits the layout's bullets, a
    plain text box has none.
    """
    found: dict[tuple[int, bool], Any] = {}
    for paragraph in body.findall(qn("a:p")):
        properties = paragraph.find(qn("a:pPr"))
        if properties is None:
            continue
        level = int(properties.get("lvl", "0"))
        if properties.find(qn("a:buNone")) is not None:
            bulleted = False
        elif properties.find(qn("a:buChar")) is not None or (
            properties.find(qn("a:buAutoNum")) is not None
        ):
            bulleted = True
        else:
            bulleted = inherits_bullets
        clean = copy.deepcopy(properties)
        for default in clean.findall(qn("a:defRPr")):
            clean.remove(default)
        found.setdefault((level, bulleted), clean)
    return found


def write_body(shape: Any, paragraphs: list[Para], *, keep_first: int = 0) -> None:
    """Replace a text body's paragraphs. `keep_first` leaves that many as they are."""
    body = shape.text_frame._txBody
    prototypes = _prototypes(body, inherits_bullets=bool(shape.is_placeholder))
    existing = body.findall(qn("a:p"))
    for paragraph in existing[keep_first:]:
        body.remove(paragraph)
    # A red default in the shape's list style would colour anything without its own.
    styles = body.find(qn("a:lstStyle"))
    if styles is not None:
        for fill in styles.iter(qn("a:solidFill")):
            colour = fill.find(qn("a:srgbClr"))
            if colour is not None and str(colour.get("val")).upper() == gate.RED:
                colour.set("val", TEXT_COLOUR)

    for para in paragraphs:
        element = etree.SubElement(body, qn("a:p"))
        prototype = prototypes.get((para.level, para.bullet))
        if prototype is not None:
            properties = copy.deepcopy(prototype)
        else:
            properties = etree.Element(qn("a:pPr"))
            if para.level:
                properties.set("lvl", str(para.level))
            if not para.bullet:
                properties.set("marL", "0")
                properties.set("indent", "0")
                etree.SubElement(properties, qn("a:buNone"))
        if para.space_before is not None:
            for old in properties.findall(qn("a:spcBef")):
                properties.remove(old)
            spacing = etree.Element(qn("a:spcBef"))
            etree.SubElement(spacing, qn("a:spcPts")).set("val", str(para.space_before * 100))
            properties.insert(0, spacing)
        element.append(properties)
        if para.lead:
            _run(element, para.lead + " ", size=para.size, bold=True, italic=False)
        if para.text or not para.lead:
            _run(element, para.text, size=para.size, bold=para.bold, italic=para.italic)


def set_text(paragraph: Any, text: str) -> None:
    """Replace a paragraph's text, keeping its first run's formatting and no red."""
    runs = paragraph.findall(qn("a:r"))
    for extra in [*runs[1:], *paragraph.findall(qn("a:br"))]:
        paragraph.remove(extra)
    if runs:
        run = runs[0]
    else:
        run = etree.SubElement(paragraph, qn("a:r"))
        etree.SubElement(run, qn("a:t"))
        end = paragraph.find(qn("a:endParaRPr"))
        if end is not None:  # a run must come before the end-of-paragraph properties
            paragraph.remove(run)
            end.addprevious(run)
    run.find(qn("a:t")).text = text
    for holder in (run.find(qn("a:rPr")), paragraph.find(qn("a:pPr"))):
        if holder is None:
            continue
        for fill in holder.iter(qn("a:solidFill")):
            colour = fill.find(qn("a:srgbClr"))
            if colour is not None and str(colour.get("val")).upper() == gate.RED:
                colour.set("val", TEXT_COLOUR)


def _size_for(count: int) -> float:
    """A size from a line count, for the code-written slides only. A
    model-written slide is measured (`layout.fit`), not counted."""
    return 16 if count <= 5 else 14 if count <= 8 else 12 if count <= 12 else 10.5


def _body(slide: Any) -> Any:
    """The slide's content placeholder: the body that is not the title."""
    for shape in slide.placeholders:
        if shape.placeholder_format.idx == 1:
            return shape
    raise LookupError("slide has no content placeholder")


def _remove(shape: Any) -> None:
    shape._element.getparent().remove(shape._element)


def _source(url: str | None) -> str:
    host = urlparse(url or "").netloc.removeprefix("www.")
    return f" (Source: {host})" if host else ""


def items_as_paragraphs(items: Sequence[Item], size: float | None = None) -> list[Para]:
    """The renderer's paragraphs for a body slide, at a body size. Without one,
    the size is the code-written slides' line-count tier."""
    if size is None:
        size = _size_for(len(items))
    paragraphs: list[Para] = []
    for item in items:
        if item.role == "headline":
            paragraphs.append(Para(item.text, bold=True, size=size, space_before=8))
        elif item.role in ("recommendation", "sub_bullet"):
            paragraphs.append(
                Para(item.text, lead=item.lead, level=1, italic=True, size=max(size - 2, 10))
            )
        elif item.role == "paragraph":
            paragraphs.append(Para(item.text, lead=item.lead, bullet=False, size=size))
        else:
            paragraphs.append(Para(item.text + _source(item.url), lead=item.lead, size=size))
    return paragraphs


# --- slide fillers ---------------------------------------------------------------------------


def fill_title(slide: Any, section: Section, document: ReviewDocument) -> None:
    client = section.data["client"]
    logo = section.data.get("logo") or ""
    name_boxes = [
        shape
        for shape in slide.shapes
        if shape.has_text_frame and "name or logo here" in shape.text_frame.text.casefold()
    ]
    picture = next(
        (
            s
            for s in slide.placeholders
            if s.placeholder_format.type is not None and "PICTURE" in str(s.placeholder_format.type)
        ),
        None,
    )
    if logo and picture is not None:
        picture.insert_picture(logo)
        for shape in name_boxes:
            _remove(shape)
    else:
        if picture is not None:
            _remove(picture)
        # Two boxes carry the same prompt, one behind the other: fill one, drop the rest.
        for index, shape in enumerate(name_boxes):
            if index == 0:
                set_text(shape.text_frame.paragraphs[0]._p, client)
            else:
                _remove(shape)
    for shape in slide.shapes:
        if not shape.has_text_frame or "presented to" not in shape.text_frame.text.casefold():
            continue
        lines = [
            f"Presented to: {client}",
            f"Presented by: {section.data['presented_by']}",
            f"Review period: {section.data['period']}",
            f"Prepared: {document.generated_at:%d %B %Y}".replace(" 0", " "),
        ]
        body = shape.text_frame._txBody
        paragraphs = body.findall(qn("a:p"))
        while len(paragraphs) < len(lines):
            clone = copy.deepcopy(paragraphs[-1])
            paragraphs[-1].addnext(clone)
            paragraphs = body.findall(qn("a:p"))
        for paragraph, line in zip(paragraphs, lines, strict=False):
            set_text(paragraph, line)
        shape.top = Emu(shape.top - Inches(0.15))


def fill_agenda(slide: Any, kept: set[int]) -> None:
    body = _body(slide).text_frame._txBody
    paragraphs = body.findall(qn("a:p"))

    def text_of(paragraph: Any) -> str:
        return "".join(t.text or "" for t in paragraph.iter(qn("a:t"))).strip().casefold()

    removed: set[int] = set()
    for index, paragraph in enumerate(paragraphs):
        text = text_of(paragraph)
        slides = AGENDA.get(text)
        if slides is not None:
            if not any(number in kept for number in slides):
                removed.add(index)
        elif text not in AGENDA_PARENTS:
            removed.add(index)  # sample lines such as "Other applicable reports"
    for parent, children in AGENDA_PARENTS.items():
        for index, paragraph in enumerate(paragraphs):
            if text_of(paragraph) != parent:
                continue
            alive = [
                i
                for i, p in enumerate(paragraphs)
                if i not in removed and text_of(p).startswith(children)
            ]
            if not alive:
                removed.add(index)
    for index in sorted(removed, reverse=True):
        body.remove(paragraphs[index])


def _fitted(
    section: Section, box: layout.TextBox, paragraphs_of: layout.Paragrapher
) -> tuple[list[Item], float | None, str | None]:
    """A budgeted section's items fitted to a box: the items to write, the
    size to write them at, and a warning when anything had to give. An
    unbudgeted section comes back as it is."""
    budget = layout.budget_for(section.key)
    items = section.all_items()
    if budget is None:
        return items, None, None
    result = layout.fit(items, box, budget=budget, paragraphs_of=paragraphs_of)
    warning = None
    if result.changed:
        warning = f"slide {section.slide} ({section.key}): {result.describe()} to fit"
    return result.items, result.size, warning


def fill_bullets(slide: Any, section: Section) -> str | None:
    body = _body(slide)
    items, size, warning = _fitted(section, layout.box_of(body), items_as_paragraphs)
    write_body(body, items_as_paragraphs(items, size))
    return warning


def fill_accomplishments(slide: Any, section: Section) -> None:
    """The value-delivered block first, under its heading; then everything else.

    Engineer-written accomplishments are stored after the code-written items,
    which would put them under the block's heading. They are not figures, so
    they go under their own.
    """
    items = section.all_items()
    block = [item for item in items if (item.slot or "").startswith("value/")]
    others = [item for item in items if item not in block]
    if block and others:
        heading = Item(role="headline", text="Also in this period", cites=[], author="code")
        items = [*block, heading, *others]
    paragraphs = items_as_paragraphs(items)
    for paragraph, item in zip(paragraphs, items, strict=True):
        if item.role == "headline":
            paragraph.bullet = False  # a heading over its lines, not one of them
    write_body(_body(slide), paragraphs)


#: Facts no longer collected. A user count waits for a Microsoft 365 connector; an
#: Autotask contact count is not a user count and is not printed in its place.
WITHDRAWN_FACTS = frozenset({"site.users", "autotask.contacts.active"})


def _management_paragraphs(items: Sequence[Item], size: float) -> list[Para]:
    paragraphs = [
        Para(item.text, lead=item.lead, size=size) for item in items if item.role == "bullet"
    ]
    for item in items:
        if item.role == "paragraph":
            paragraphs.append(Para(item.text, bullet=False, size=size, space_before=14))
    return paragraphs


def fill_management(slide: Any, section: Section) -> str | None:
    # Reviews stored before the Users line was withdrawn still carry it. Contact
    # counts are not printed, whatever the stored review says.
    body = _body(slide)
    kept = [
        item
        for item in section.all_items()
        if item.role != "bullet" or not WITHDRAWN_FACTS & set(item.cites)
    ]
    trimmed = section.model_copy(update={"items": kept, "engineer_items": []})
    items, size, warning = _fitted(trimmed, layout.box_of(body), _management_paragraphs)
    write_body(body, _management_paragraphs(items, size or 15.5))
    return warning


#: Scorecard chip colours: (fill, text). Never FF0000: that is the template's
#: placeholder red, and the gate refuses it wherever it is.
SCORE_COLOURS: dict[str, tuple[str, str]] = {
    "good": ("018752", "FFFFFF"),
    "attention": ("F2A900", "1F1F1F"),
    "action": ("B3261E", "FFFFFF"),
    "not_assessed": ("E3E3E3", "595959"),
    "no_data": ("E3E3E3", "595959"),
}
#: Up always means better (scorecard.toml), whichever way the number moved.
TREND_ARROWS = {"up": "\u2191", "down": "\u2193", "steady": "\u2192"}


def _plain_run(paragraph: Any, text: str, *, size: float, bold: bool, colour: str) -> None:
    run = paragraph.add_run()
    run.text = text
    run.font.size, run.font.bold = Pt(size), bold
    run.font.color.rgb = RGBColor.from_string(colour)


def draw_scorecard(
    slide: Any, rows: list[ScorecardRow], *, left: int, top: int, width: int, caption: str
) -> int:
    """One chip per area, the status in words as well as colour. Returns the bottom edge."""
    gap, height = Inches(0.07), Inches(0.52)
    chip_width = (width - gap * (len(rows) - 1)) // len(rows)
    for index, row in enumerate(rows):
        chip = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, left + (chip_width + gap) * index, top, chip_width, height
        )
        chip.name = f"Scorecard: {row.label}"
        fill, text = SCORE_COLOURS[row.status]
        chip.fill.solid()
        chip.fill.fore_color.rgb = RGBColor.from_string(fill)
        chip.line.fill.background()
        chip.shadow.inherit = False
        frame = chip.text_frame
        frame.margin_top = frame.margin_bottom = Inches(0.02)
        frame.margin_left = frame.margin_right = Inches(0.04)
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        frame.word_wrap = True
        first = frame.paragraphs[0]
        first.alignment = PP_ALIGN.CENTER
        _plain_run(first, row.label, size=11, bold=True, colour=text)
        second = frame.add_paragraph()
        second.alignment = PP_ALIGN.CENTER
        arrow = f" {TREND_ARROWS[row.trend]}" if row.trend else ""
        _plain_run(second, f"{row.status_label}{arrow}", size=9.5, bold=False, colour=text)
    note = slide.shapes.add_textbox(left, top + height, width, Inches(0.22))
    note.name = "Scorecard: how it is computed"
    note.text_frame.margin_top = note.text_frame.margin_bottom = Inches(0.01)
    _plain_run(note.text_frame.paragraphs[0], caption, size=8, bold=False, colour="595959")
    return int(top + height + Inches(0.24))


def _takeaway_paragraphs(items: Sequence[Item], size: float) -> list[Para]:
    """One row's cell: the bold headline on its own line, the detail under it,
    a point smaller. Measured as two paragraphs with no space between."""
    paragraphs: list[Para] = []
    for item in items:
        paragraphs.append(Para(item.lead or "", bold=True, size=size + 1, space_before=0))
        paragraphs.append(Para(item.text, size=size, space_before=0))
    return paragraphs


def fill_takeaways(slide: Any, section: Section) -> str | None:
    frame = next(s for s in slide.shapes if getattr(s, "has_table", False) and s.has_table)
    table = frame.table
    rows = list(table.rows)
    items = section.all_items()
    lead_size, text_size = 12.0, 11.0
    warnings: list[str] = []
    if section.scorecard:
        compared = section.data.get("scorecard_compared_with")
        caption = "Status is computed by fixed rules from this review's measurements." + (
            f" Arrows compare with {compared}; up is better." if compared else ""
        )
        bottom = draw_scorecard(
            slide,
            section.scorecard,
            left=frame.left,
            top=frame.top - Inches(0.06),
            width=frame.width,
            caption=caption,
        )
        frame.top = bottom
        lead_size, text_size = 11.0, 10.0  # the table has less room under the scorecard
        for row in rows:
            row.height = Inches(0.85)  # three rows end above the footer, at 4.93in
    if not items:
        _remove(frame)  # a scorecard with no takeaways: no empty table under it
        return None
    budget = layout.budget_for(section.key)
    if budget is not None:
        items = items[: budget.max_items]
    for index, row in enumerate(rows):
        if index >= len(items):
            row._tr.getparent().remove(row._tr)
            continue
        item = items[index]
        if budget is not None:
            # A table row grows to hold its text and pushes the rows under it
            # down the slide, so each row is fitted as a box of its own.
            cell = layout.TextBox(
                width=Emu(table.columns[1].width).inches * layout.POINTS_PER_INCH
                - 2 * layout.DEFAULT_INSET_X,
                height=Emu(row.height).inches * layout.POINTS_PER_INCH - 2 * layout.DEFAULT_INSET_Y,
                indents=(0.0,),
            )
            # Without the scorecard the rows are the template's taller ones.
            ladder = budget if section.scorecard else replace(budget, sizes=(12.0, 11.0))
            fitted = layout.fit([item], cell, budget=ladder, paragraphs_of=_takeaway_paragraphs)
            item = fitted.items[0]
            text_size, lead_size = fitted.size, fitted.size + 1
            if fitted.changed:
                warnings.append(
                    f"slide {section.slide} (takeaways) row {index + 1}: {fitted.describe()}"
                )
        set_text(row.cells[0].text_frame.paragraphs[0]._p, str(index + 1))
        paragraph = row.cells[1].text_frame.paragraphs[0]._p
        for child in list(paragraph):
            if child.tag in (qn("a:r"), qn("a:br")):
                paragraph.remove(child)
        properties = paragraph.find(qn("a:pPr"))
        if properties is not None:
            for default in properties.findall(qn("a:defRPr")):
                properties.remove(default)
        _run(paragraph, item.lead or "", size=lead_size, bold=True, italic=False)
        etree.SubElement(paragraph, qn("a:br"))
        _run(paragraph, item.text, size=text_size, bold=False, italic=False)
        end = paragraph.find(qn("a:endParaRPr"))
        if end is not None:
            paragraph.remove(end)
            paragraph.append(end)
    return "; ".join(warnings) or None


def fill_budget(slide: Any, section: Section) -> None:
    table = next(s for s in slide.shapes if getattr(s, "has_table", False) and s.has_table).table
    body_rows = list(table.rows)[1:]
    items = [item for item in section.all_items() if item.cells]
    while len(body_rows) < len(items):
        clone = copy.deepcopy(body_rows[-1]._tr)
        body_rows[-1]._tr.addnext(clone)
        body_rows = list(table.rows)[1:]
    for index, row in enumerate(body_rows):
        if index >= len(items):
            row._tr.getparent().remove(row._tr)
            continue
        for cell, value in zip(row.cells, items[index].cells, strict=False):
            set_text(cell.text_frame.paragraphs[0]._p, value)
            for extra in cell.text_frame._txBody.findall(qn("a:p"))[1:]:
                cell.text_frame._txBody.remove(extra)


def fill_cx(slide: Any, section: Section) -> None:
    scores = {
        "nps": section.data.get("nps", ""),
        "csat": section.data.get("csat", ""),
        "pqual": section.data.get("pqual", ""),
    }
    boxes = [
        shape
        for shape in slide.shapes
        if shape.has_text_frame and shape.text_frame.text.strip() in gate.SAMPLE_SCORES
    ]
    labels = {
        key: next(
            (s for s in slide.shapes if s.has_text_frame and words in s.text_frame.text), None
        )
        for key, words in (("nps", "Net Promoter"), ("csat", "(CSAT)"), ("pqual", "(PQUAL)"))
    }
    for shape in boxes:
        sample = shape.text_frame.text.strip()
        key = "nps" if sample == "88" else "csat" if Emu(shape.left).inches < 2 else "pqual"
        label = labels[key]
        if not scores[key]:
            _remove(shape)
            if label is not None:
                _remove(label)  # a label with no score under it says nothing
            continue
        set_text(shape.text_frame.paragraphs[0]._p, scores[key])
        if key == "nps" and label is not None:
            # In the template this box sits on top of the CSAT score. Its place is
            # centred above its own label.
            shape.left = label.left + (label.width - shape.width) // 2
            shape.top = label.top - shape.height + Inches(0.12)
    for shape in slide.shapes:
        if shape.has_text_frame and "insert survey comments" in shape.text_frame.text.casefold():
            comments = [item for item in section.engineer_items if item.text.strip()]
            if not comments:
                _remove(shape)
                continue
            paragraphs = [Para("What we’re hearing from your team:", bullet=False, size=15)]  # noqa: RUF001
            paragraphs += [Para(f"“{c.text.strip()}”", italic=True, size=14) for c in comments]
            write_body(shape, paragraphs)


# --- slide 6: native charts in place of the screenshot -----------------------------------------


def _style_chart(chart: Any, data: ChartData) -> None:
    chart.has_title = True
    chart.chart_title.text_frame.text = data.title
    title_run = chart.chart_title.text_frame.paragraphs[0].runs[0]
    title_run.font.size, title_run.font.bold = Pt(9), True
    chart.has_legend = len(data.series) > 1
    if chart.has_legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(7)
    for axis_name in ("category_axis", "value_axis"):
        axis = getattr(chart, axis_name, None)
        if axis is None:
            continue
        axis.tick_labels.font.size = Pt(7)
        axis.format.line.color.rgb = RGBColor(0xBF, 0xBF, 0xBF)
        if axis_name == "value_axis":
            axis.has_major_gridlines = True
            axis.major_gridlines.format.line.color.rgb = RGBColor(0xE3, 0xE3, 0xE3)
            axis.minimum_scale = 0
            if data.number_format != "General":
                axis.maximum_scale = 100
                axis.tick_labels.number_format = data.number_format
                axis.tick_labels.number_format_is_linked = False
    plot = chart.plots[0]
    for index, series in enumerate(plot.series):
        colour = SERIES_COLOURS[index % len(SERIES_COLOURS)]
        if data.kind == "line":
            series.format.line.color.rgb = colour
            series.format.line.width = Pt(1.75)
            series.smooth = False
        else:
            series.format.fill.solid()
            series.format.fill.fore_color.rgb = colour
    if data.kind == "line":
        # A month of days: label every fifth so the axis stays readable.
        skip = etree.SubElement(chart.category_axis._element, qn("c:tickLblSkip"))
        skip.set("val", "5")
    else:
        plot.gap_width = 60
        plot.has_data_labels = True
        plot.data_labels.font.size = Pt(7)
        plot.data_labels.number_format = data.number_format
        plot.data_labels.number_format_is_linked = False


CHART_TYPES = {
    "line": XL_CHART_TYPE.LINE,
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "bar": XL_CHART_TYPE.BAR_CLUSTERED,
    "pie": XL_CHART_TYPE.PIE,
}

#: Reading order on the slide, and how much of a row each chart wants.
CHART_LAYOUT: tuple[tuple[str, float], ...] = (
    ("autotask.chart.daily", 1.65),
    ("autotask.chart.open_priority", 1.0),
    ("autotask.chart.issue_type", 1.15),
    ("autotask.chart.sla", 0.75),
    ("autotask.chart.top_contacts", 1.1),
)


CONTACT_CHART = "autotask.chart.top_contacts"


def anonymised_contacts(data: ChartData) -> ChartData:
    """The tickets-by-contact chart with people as `Contact A`, `Contact B`, by rank.

    `Contact A` raised the most. The figures and their order are untouched, and
    bars that are not a person (automation, an unnamed contact) keep their label.
    The stored chart keeps the real names: the engineer can still turn them on.
    """
    ranked = [name for name in reversed(data.categories) if name not in NOT_A_PERSON]
    labels = {name: contact_label(rank) for rank, name in enumerate(ranked)}
    return data.model_copy(
        update={"categories": [labels.get(name, name) for name in data.categories]}
    )


def contact_names_in(document: ReviewDocument) -> Pseudonyms:
    """Every contact name the review knows, as a checker for the gate."""
    names = set(document.facts.names.get("contacts", []))
    chart = document.facts.charts.get(CONTACT_CHART)
    if chart is not None:
        names.update(name for name in chart.categories if name not in NOT_A_PERSON)
    return Pseudonyms(exact={name: "" for name in names if name})


def _observation_paragraphs(items: Sequence[Item], size: float) -> list[Para]:
    return [Para(item.text, lead=item.lead, size=size, space_before=3) for item in items]


def fill_dashboard(
    slide: Any, section: Section, document: ReviewDocument, *, anonymise: bool = False
) -> str | None:
    area: tuple[int, int, int, int] | None = None
    for shape in list(slide.shapes):
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            area = (shape.left, shape.top, shape.width, shape.height)
            _remove(shape)  # the sample screenshot: never shipped, with or without charts
    left, top, width, height = area or (Inches(0.68), Inches(1.55), Inches(8.65), Inches(3.66))

    charts = [
        (
            anonymised_contacts(document.facts.charts[chart_id])
            if anonymise and chart_id == CONTACT_CHART
            else document.facts.charts[chart_id],
            weight,
        )
        for chart_id, weight in CHART_LAYOUT
        if chart_id in document.facts.charts
    ]
    rows = [charts] if len(charts) <= 2 else [charts[:2], charts[2:]]
    gap = Inches(0.08)
    row_height = (height - gap * (len(rows) - 1)) // len(rows)
    for row_index, row in enumerate(rows):
        total = sum(weight for _, weight in row)
        x = left
        usable = width - gap * (len(row) - 1)
        for data, weight in row:
            chart_width = int(usable * weight / total)
            chart_data = CategoryChartData()
            chart_data.categories = data.categories
            for series in data.series:
                chart_data.add_series(series.name, series.values)
            frame = slide.shapes.add_chart(
                CHART_TYPES[data.kind],
                x,
                top + (row_height + gap) * row_index,
                chart_width,
                row_height,
                chart_data,
            )
            frame.name = f"Chart: {data.title}"
            _style_chart(frame.chart, data)
            x += chart_width + gap

    observations = _body(slide)
    if not section.all_items():
        _remove(observations)
        return None
    # The "Observations" heading is the template's own and stays as the first
    # paragraph, so the box the items are fitted to is the box under it.
    box = layout.box_of(observations)
    heading = observations.text_frame.paragraphs[0]
    heading_size = next((r.font.size.pt for r in heading.runs if r.font.size), 12.0)
    box = layout.TextBox(
        width=box.width,
        height=box.height - heading_size * layout.LINE_HEIGHT,
        space_before=3.0,
    )
    items, size, warning = _fitted(section, box, _observation_paragraphs)
    write_body(observations, _observation_paragraphs(items, size or 10.5), keep_first=1)
    return warning


# --- assembling the deck ----------------------------------------------------------------------


def _set_notes(slide: Any, text: str) -> None:
    """Replace the slide's notes. Never creates a notes page just to leave it empty."""
    if not text and not slide.has_notes_slide:
        return
    slide.notes_slide.notes_text_frame.text = text


def stamp(slide: Any, presentation: Any) -> None:
    """ "Sample data", top right, on a demo deck's every slide."""
    width, height = Inches(1.45), Inches(0.3)
    box = slide.shapes.add_textbox(
        presentation.slide_width - width - Inches(0.12), Inches(0.08), width, height
    )
    box.name = "Sample data stamp"
    box.fill.solid()
    box.fill.fore_color.rgb = RGBColor(0xFF, 0xF2, 0xCC)
    box.line.color.rgb = RGBColor(0xB7, 0x79, 0x1F)
    frame = box.text_frame
    frame.margin_top = frame.margin_bottom = Inches(0.02)
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    paragraph = frame.paragraphs[0]
    paragraph.alignment = PP_ALIGN.CENTER
    run = paragraph.add_run()
    run.text = STAMP
    run.font.size, run.font.bold = Pt(11), True
    run.font.color.rgb = RGBColor(0x7A, 0x4E, 0x0B)


def _trace(section: Section, document: ReviewDocument) -> str:
    """Where each statement came from: the fact-id trace, for an internal deck's notes."""
    lines = []
    if section.talk_track:
        lines += [section.talk_track, ""]
    lines.append(f"Sources for this slide ({section.narrative} narrative). {section.reason}")
    for item in section.all_items():
        where = ", ".join(item.cites) or "engineer"
        lines.append(f"- {(item.lead or item.text)[:70]} [{item.author}: {where}]")
        if item.url:
            lines.append(f"  {item.url}")
    for row in section.scorecard:
        lines.append(f"# Scorecard, {row.label}: {row.status_label}. {row.reason}")
        for metric in row.metrics:
            source = metric.fact_id + (f" per {metric.per_fact_id}" if metric.per_fact_id else "")
            lines.append(
                f"  {metric.label}: {metric.display}, {metric.status} ({metric.rule}) [{source}]"
            )
    for fact_id in section.fact_ids:
        fact = document.facts.get(fact_id)
        if fact is not None:
            lines.append(f"* {fact.id} = {fact.display}: {fact.definition}")
    # Where the device counts came from: the machines the operating system placed
    # differently from NinjaOne's node class, under the slide that prints the counts.
    disagreements = document.facts.tables.get(ROLE_DISAGREEMENTS)
    if (
        disagreements is not None
        and disagreements.rows
        and "ninjaone.devices.workstations" in section.fact_ids
    ):
        lines.append(f"# {disagreements.label} ({len(disagreements.rows)}):")
        lines += [
            f"  {row[0]}: {row[1]} (NinjaOne says {row[2]}), counted as a {row[3]}"
            for row in disagreements.rows
        ]
    return "\n".join(lines)[:6000]


def slides_to_keep(document: ReviewDocument) -> set[int]:
    """The deck's slides that render. A section with no slide on the deck (the Monthly
    Report, slide 0) is not a slide and is never kept."""
    sections = {section.slide: section for section in document.sections}
    keep = {
        number
        for number, section in sections.items()
        if section.renderable and number in SLIDES and number not in DIVIDERS
    }
    for divider, children in DIVIDERS.items():
        if any(child in keep for child in children):
            keep.add(divider)
    return keep


def _drop(presentation: Any, slide: Any) -> None:
    id_list = presentation.slides._sldIdLst
    for entry in list(id_list):
        if presentation.part.related_part(entry.rId) is slide.part:
            presentation.part.drop_rel(entry.rId)
            id_list.remove(entry)
            return


def render(
    document: ReviewDocument,
    output: Path,
    *,
    mode: RenderMode = "client",
    template: Path = TEMPLATE,
) -> RenderResult:
    # Contact privacy: people on the tickets-by-contact chart are Contact A, B... unless
    # an engineer chose names for this review. A demo deck has its own pseudonyms for
    # everyone and is left as it is.
    hide_contacts = mode != "demo" and not document.people_names_shown
    private = contact_names_in(document) if hide_contacts else None
    check_real = None
    if mode == "demo":
        terms = real_name_terms(document)  # from the real review, before anything is replaced
        document, names = pseudonymise(document)  # a copy: the stored review is untouched
        check_real = real_name_checker(names, terms)
    presentation = Presentation(str(template))
    slides = {number: presentation.slides[number - 1] for number in SLIDES}
    sections = {section.slide: section for section in document.sections}
    keep = slides_to_keep(document)
    warnings: list[str] = []

    for number in sorted(keep):
        slide, section = slides[number], sections[number]
        if section.status == "static":
            continue
        warning: str | None = None
        if number == 1:
            fill_title(slide, section, document)
        elif number == 2:
            fill_agenda(slide, keep)
        elif number == 5:
            warning = fill_management(slide, section)
        elif number == 6:
            warning = fill_dashboard(slide, section, document, anonymise=hide_contacts)
        elif number == 7:
            warning = fill_takeaways(slide, section)
        elif number == 9:
            fill_cx(slide, section)
        elif number == 8:
            fill_accomplishments(slide, section)
        elif number == 22:
            fill_budget(slide, section)
        else:
            warning = fill_bullets(slide, section)
        if warning:
            warnings.append(warning)
        if number == 12:
            # The template titles slides 12 and 13 alike; 12 is about replacement.
            for paragraph in slide.shapes.title.text_frame.paragraphs:
                for run in paragraph.runs:
                    if "Upgrade" in run.text:
                        run.text = run.text.replace("Upgrade", "Replacement")

    # Notes, for every slide that stays: the template's own presenter instructions
    # are cleared in every mode, then the mode decides what, if anything, replaces them.
    allowed_notes: dict[int, str] = {}
    for number in sorted(keep):
        section = sections[number]
        talk = (section.talk_track or "").strip()
        if mode == "internal" and section.status != "static" and number not in (1, 2):
            _set_notes(slides[number], _trace(section, document))
        else:
            _set_notes(slides[number], talk)
        allowed_notes[number] = talk
        if mode == "demo":
            stamp(slides[number], presentation)

    for number in sorted(set(SLIDES) - keep, reverse=True):
        _drop(presentation, slides[number])

    surveyed = sections.get(9)
    real_scores = frozenset(
        value for value in (surveyed.data.values() if surveyed else []) if value
    )
    report = gate.enforce(
        [(number, slides[number]) for number in sorted(keep)],
        real_scores=real_scores,
        allowed_notes=None if mode == "internal" else allowed_notes,
        real_names=check_real,
        private_names=private.survivors if private is not None else None,
        template=template,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(str(output))
    return RenderResult(
        path=output,
        kept=sorted(keep),
        dropped=sorted(set(SLIDES) - keep),
        gate=report,
        warnings=warnings,
        mode=mode,
    )


def default_output(
    document: ReviewDocument, directory: Path, *, now: dt.datetime, mode: RenderMode = "client"
) -> Path:
    last = document.period.end - dt.timedelta(days=1)
    # A demo deck's file name must not name the customer either.
    who = "sample-client" if mode == "demo" else document.site_slug
    suffix = "" if mode == "client" else f"-{mode}"
    return directory / f"{who}-tbr-{last:%Y-%m}-{now:%Y%m%d-%H%M%S}{suffix}.pptx"

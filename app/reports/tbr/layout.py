"""How much text a slide can hold, decided once and enforced three times.

Slide 18 ran its fifth service past the placeholder and under the footer. The
renderer picked a font size by counting items and never looked at the box, so
five sentences that were each within their own limit added up to twice the
height available. This module is the fix, and it is shared: every model-written
slide declares its budget here, and the same measurement is used to set that
budget, to brief the model, and to fit the text at render time.

**The budget** (`SLIDE_BUDGETS`) is two numbers per slide: how many items it
may carry and how long each may be. They are not chosen by eye. A test renders
every budgeted slide with items at exactly the budget and asserts the text fits
its placeholder with the template's own font, size and spacing — so a budget
that does not fit is a failing test, not a deck that does not fit.

**The measurement** (`measure`) wraps each paragraph at the placeholder's text
width, with the font's real glyph widths where the font is on the machine and
a conservative average where it is not, and sums line heights at the master's
line spacing plus the space before each paragraph. It is an estimate of what
PowerPoint will do, and it is deliberately a slightly pessimistic one: the
cost of guessing long is a little white space, and the cost of guessing short
is text under the footer.

**The fit** (`fit`) runs at render time, after the model has written to its
budget, as the last line of defence. It tries the slide's sizes from the
template's own down to a readable floor, so a slide with one item gets the
template's size rather than the size the fullest slide needs; at the floor it
drops whole items from the least serious end and says so in a code-written
line; then it shortens what is left at sentence boundaries. The order matters.
Fewer complete items read better on a wall than the same number of items each
cut short, and the serious findings are the ones that must survive.

Three places, one number. The model is told its budget up front rather than
finding out on retry; the validator refuses a draft over it; the renderer
measures anyway.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from pptx.util import Emu

from app.reports.tbr.model import Item

logger = logging.getLogger(__name__)

EMU_PER_INCH = 914400
POINTS_PER_INCH = 72.0

#: Line height as a multiple of the font size at the master's 100% spacing.
#: Helvetica and Arial both lay out at roughly 1.2; the template's own text
#: does in PowerPoint. Measured text is compared against the box with a margin
#: on top of this, so a face that sits taller does not tip a slide over.
LINE_HEIGHT = 1.2

#: How much of the box's height the measurement may claim before a slide is
#: considered full. The estimate is not PowerPoint's layout engine, and the
#: difference is spent here rather than under the footer.
SAFETY = 0.94

#: What the fitter writes when it has to drop items on the deck itself. It is
#: one short paragraph so it never becomes the thing that overflows.
HELD_LINE = "{count} further item{s} from this section are on the full review."


# --- the budget ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlideBudget:
    """How much one model-written slide may carry.

    `max_items` is the number of things — findings, services, projects — the
    slide holds, each of which may be more than one paragraph (an observation
    and its recommendation are one item here). `chars_per_item` is the
    ceiling on any single slot the model fills for it, and `chars_per_slide`
    the ceiling on all of them together, so a slide of short items cannot
    become a slide of one long one.
    """

    section: str
    slide: int
    max_items: int
    chars_per_item: int
    chars_per_slide: int
    what: str
    """What an item is called in the reason line: "finding", "service"."""
    sizes: tuple[float, ...]
    """Body font sizes to try, in points, largest first: the template's own
    size, then steps down to a floor the room can still read. The renderer
    used to pick a size by counting paragraphs, which is how four services
    stayed at 16pt; a count of paragraphs is not a measure of text. The
    character budget is calibrated at the floor, `sizes[-1]`, so that the
    fullest slide the budget allows fits without dropping anything."""

    @property
    def floor(self) -> float:
        return self.sizes[-1]

    def held_back_reason(self, held: Sequence[str]) -> str:
        """The review-screen sentence naming what did not make the slide."""
        if not held:
            return ""
        return (
            f"{len(held)} less serious {self.what}(s) are on the review and not the slide: "
            + "; ".join(held)
            + ". "
        )


#: A headline slot (slide 7) is ten words at most; this is the character cap the
#: brief states for it, and the slide total counts it.
HEADLINE_CHARS = 60

#: Every model-written slide, and only those. A section not listed here is
#: code-written and sized by its own filler; a model-written one must be here,
#: which `tests/reports/test_tbr_layout.py` checks by name.
#:
#: The numbers were set by the same test rendering each slide at its budget and
#: measuring. Change one, and run it.
SLIDE_BUDGETS: dict[str, SlideBudget] = {
    # The template's own slide is three observation/recommendation pairs. Five
    # was the cap and five did not fit at any size; three at 420 characters
    # did not either, and was the overflow on the first live deck. An
    # observation is a slot, its recommendation another, and both count.
    "cybersecurity": SlideBudget(
        section="cybersecurity",
        slide=14,
        max_items=3,
        chars_per_item=140,
        chars_per_slide=720,
        what="finding",
        sizes=(16, 14, 13, 12),
    ),
    # One bullet per service, the catalogue name in bold as a lead. The lead is
    # code-written and the longest is a hundred characters; the budget is for
    # what the model writes after it, and the test measures with that lead on.
    "services": SlideBudget(
        section="services",
        slide=18,
        max_items=4,
        chars_per_item=160,
        chars_per_slide=640,
        what="service",
        sizes=(16, 15, 14),
    ),
    # Headline (the project name, code-written), then where it stands and when
    # it finishes: two model slots per project.
    "projects": SlideBudget(
        section="projects",
        slide=11,
        max_items=2,
        chars_per_item=120,
        chars_per_slide=240,
        what="project",
        sizes=(16, 14, 12),
    ),
    # A three-row table under the scorecard: a bold headline (its own slot, ten
    # words, capped at HEADLINE_CHARS) over two sentences, in a 0.85-inch row.
    # The slide total is three of each.
    "takeaways": SlideBudget(
        section="takeaways",
        slide=7,
        max_items=3,
        chars_per_item=150,
        chars_per_slide=3 * (150 + 60),
        what="takeaway",
        sizes=(11, 10),
    ),
    # Trends with a source each, in the same body as slide 14. The source
    # suffix the renderer appends is about thirty characters and is measured.
    "trends": SlideBudget(
        section="trends",
        slide=3,
        max_items=4,
        chars_per_item=220,
        chars_per_slide=880,
        what="trend",
        sizes=(16, 15, 14),
    ),
    # One sentence naming improvement areas, under the device counts.
    "management": SlideBudget(
        section="management",
        slide=5,
        max_items=1,
        chars_per_item=220,
        chars_per_slide=220,
        what="sentence",
        sizes=(15.5, 14),
    ),
    # Up to three short observations in a 1.15-inch box beside the charts.
    "dashboard": SlideBudget(
        section="dashboard",
        slide=6,
        max_items=3,
        chars_per_item=90,
        chars_per_slide=270,
        what="observation",
        sizes=(10.5, 9.5, 9),
    ),
}


def budget_for(section: str) -> SlideBudget | None:
    return SLIDE_BUDGETS.get(section)


# --- the box ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBox:
    """A placeholder's geometry as text sees it, in points."""

    width: float
    height: float
    space_before: float = 8.0
    """The master's `spcBef` for a body paragraph, in points."""
    indents: tuple[float, ...] = (15.0, 31.9)
    """Left margin per level, in points: the master's `marL` for levels 0 and 1."""
    line_height: float = LINE_HEIGHT

    def text_width(self, level: int) -> float:
        indent = self.indents[min(level, len(self.indents) - 1)]
        return max(self.width - indent, 36.0)


#: PowerPoint's default text insets when the shape declares none.
DEFAULT_INSET_X = 0.1 * POINTS_PER_INCH
DEFAULT_INSET_Y = 0.05 * POINTS_PER_INCH


def box_of(shape: Any) -> TextBox:
    """The geometry of a python-pptx shape's text area."""
    width = Emu(shape.width).inches * POINTS_PER_INCH - 2 * DEFAULT_INSET_X
    height = Emu(shape.height).inches * POINTS_PER_INCH - 2 * DEFAULT_INSET_Y
    return TextBox(width=width, height=height)


# --- measuring ---------------------------------------------------------------------

#: Where a metric-compatible face for the template's body font may be. Helvetica
#: is the theme's minor font; Arial and Liberation Sans share its metrics, and
#: DejaVu is wider, which errs the right way.
FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)

#: Average advance of mixed English prose in Arial, as a fraction of the font
#: size, when no font file can be read. Measured over the template's own
#: sample text; rounded up, because guessing long costs white space and
#: guessing short costs text under the footer.
FALLBACK_EM_PER_CHAR = 0.53

Measurer = Callable[[str, float], float]


@lru_cache(maxsize=1)
def _font_path() -> Path | None:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


@lru_cache(maxsize=32)
def _font(size_tenths: int) -> Any:
    """A PIL font at a size, or None when PIL or the face is unavailable."""
    path = _font_path()
    if path is None:
        return None
    try:
        from PIL import ImageFont

        return ImageFont.truetype(str(path), size_tenths / 10)
    except Exception:  # pragma: no cover - PIL present but face unreadable
        return None


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    """The width of a run in points, at a size, with the real face if it is here.

    Bold Arial is about six percent wider than regular; the bold face itself is
    not looked for, because a second file to find is a second way to be wrong
    on a machine without it.
    """
    font = _font(round(size * 10))
    if font is not None:
        width = float(font.getlength(text))
    else:
        width = len(text) * size * FALLBACK_EM_PER_CHAR
    return width * (1.06 if bold else 1.0)


def wrap_lines(text: str, size: float, width: float, *, lead: str | None = None) -> int:
    """How many lines a paragraph takes when wrapped at `width` points.

    Words are placed greedily, as PowerPoint does; a word wider than the line
    takes a line of its own and is counted at its width. The lead, when there
    is one, is bold and on the first line.
    """
    words = text.split()
    if lead:
        words = [*lead.split(), *words]
    if not words:
        return 1
    lines = 1
    used = 0.0
    space = text_width(" ", size)
    lead_words = len(lead.split()) if lead else 0
    for index, word in enumerate(words):
        advance = text_width(word, size, bold=index < lead_words)
        if used == 0.0:
            used = advance
        elif used + space + advance <= width:
            used += space + advance
        else:
            lines += 1
            used = advance
    return lines


@dataclass(frozen=True, slots=True)
class Measured:
    height: float
    lines: int


def measure(paragraphs: Sequence[Any], box: TextBox) -> Measured:
    """The height a list of `Para`s takes in a box, in points.

    A `Para` is the renderer's paragraph: `text`, `lead`, `level`, `size`,
    `space_before`. Anything with those attributes will do, which is what lets
    the test measure the same objects the renderer writes.
    """
    height = 0.0
    lines = 0
    for index, para in enumerate(paragraphs):
        size = float(para.size or 16.0)
        count = wrap_lines(para.text, size, box.text_width(para.level), lead=para.lead)
        before = box.space_before if index else 0.0
        if para.space_before is not None:
            before = float(para.space_before) if index else 0.0
        height += before + count * size * box.line_height
        lines += count
    return Measured(height=height, lines=lines)


def fits(paragraphs: Sequence[Any], box: TextBox) -> bool:
    return measure(paragraphs, box).height <= box.height * SAFETY


@dataclass(frozen=True, slots=True)
class WrittenPara:
    """A paragraph read back from a saved shape, as `measure` sees it."""

    text: str
    lead: str | None
    level: int
    size: float | None
    space_before: float | None


def read_paragraphs(shape: Any, *, default_size: float = 16.0) -> list[WrittenPara]:
    """The paragraphs a shape holds, as they were written: text, a bold lead
    when the first run is bold and others follow, level, size and spacing.
    This is how a test measures the file rather than the intent."""
    from pptx.oxml.ns import qn

    found: list[WrittenPara] = []
    for paragraph in shape.text_frame._txBody.findall(qn("a:p")):
        properties = paragraph.find(qn("a:pPr"))
        level = int(properties.get("lvl", "0")) if properties is not None else 0
        before: float | None = None
        if properties is not None:
            spacing = properties.find(qn("a:spcBef"))
            points = spacing.find(qn("a:spcPts")) if spacing is not None else None
            if points is not None and points.get("val"):
                before = int(points.get("val")) / 100
        # A line break inside a paragraph (the takeaway rows) is a new line with
        # no space before it; each stretch between breaks is measured on its own.
        stretches: list[list[Any]] = [[]]
        for child in paragraph:
            if child.tag == qn("a:br"):
                stretches.append([])
            elif child.tag == qn("a:r"):
                stretches[-1].append(child)
        for index, runs in enumerate(stretches):
            if not runs and index:
                continue
            texts = ["".join(t.text or "" for t in run.iter(qn("a:t"))) for run in runs]
            lead: str | None = None
            if len(runs) > 1:
                first = runs[0].find(qn("a:rPr"))
                if first is not None and first.get("b") == "1":
                    lead = texts[0].strip()
                    texts = texts[1:]
            size: float | None = None
            for run in runs:
                run_properties = run.find(qn("a:rPr"))
                if run_properties is not None and run_properties.get("sz"):
                    size = int(run_properties.get("sz")) / 100
                    break
            found.append(
                WrittenPara(
                    text=" ".join(texts).strip(),
                    lead=lead,
                    level=level,
                    size=size or default_size,
                    space_before=0.0 if index else before,
                )
            )
    return found


def measure_shape(shape: Any, box: TextBox | None = None) -> Measured:
    """How tall a saved shape's text stands, against its own box by default."""
    return measure(read_paragraphs(shape), box or box_of(shape))


# --- fitting ------------------------------------------------------------------------


@dataclass(slots=True)
class FitResult:
    items: list[Item]
    size: float
    dropped: list[Item] = field(default_factory=list)
    shortened: int = 0
    held_line: Item | None = None

    @property
    def changed(self) -> bool:
        return bool(self.dropped or self.shortened)

    def describe(self) -> str:
        parts = []
        if self.dropped:
            parts.append(f"dropped {len(self.dropped)} item(s) from the end")
        if self.shortened:
            parts.append(f"shortened {self.shortened} item(s) at a sentence boundary")
        return "; ".join(parts)


def _group_key(item: Item) -> str | None:
    """Items that belong together: an observation and its recommendation share
    a slot prefix (`<finding>/observation`, `<finding>/recommendation`), and a
    project's headline and its lines share one. Dropping half of a pair would
    leave a recommendation for nothing.

    An item with no slot is pinned: code wrote it for the slide (the suspected-
    findings caveat) or an engineer did, and the fitter measures it but never
    drops or shortens it. The model's text is what gives way."""
    if not item.slot:
        return None
    return item.slot.rsplit("/", 1)[0] if "/" in item.slot else item.slot


def groups(items: Sequence[Item]) -> list[list[Item]]:
    """Consecutive slotted items with the same group key, in order. Pinned
    items are in no group."""
    grouped: list[list[Item]] = []
    last: str | None = None
    for item in items:
        key = _group_key(item)
        if key is None:
            last = None
            continue
        if grouped and key == last:
            grouped[-1].append(item)
        else:
            grouped.append([item])
        last = key
    return grouped


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def shorten(text: str, limit: int) -> str:
    """Cut at a sentence boundary so the result is still under `limit`.

    Never mid-sentence: a slide is read aloud in a room, and half a sentence
    is worse than one fewer. If even the first sentence is over the limit it
    is kept whole — a longer line is a lesser wrong than a broken one — and
    the caller's next step is to drop the item.
    """
    if len(text) <= limit:
        return text
    sentences = _SENTENCE_END.split(text.strip())
    kept: list[str] = []
    for sentence in sentences:
        candidate = " ".join([*kept, sentence])
        if kept and len(candidate) > limit:
            break
        kept.append(sentence)
    return " ".join(kept)


Paragrapher = Callable[[Sequence[Item], float], Sequence[Any]]
"""Turns items into the renderer's paragraphs at a body size: what is measured
is exactly what would be written, leads and sub-sizes included."""


def _held_line(dropped: Sequence[Item]) -> Item:
    count = len({_group_key(item) for item in dropped})
    return Item(
        text=HELD_LINE.format(count=count, s="" if count == 1 else "s"),
        cites=[],
        author="code",
        role="paragraph",
    )


def _assemble(
    items: Sequence[Item], dropped: set[int], shortened: dict[int, str], held: Item | None
) -> list[Item]:
    """The items in their original order, minus the dropped, with the shortened
    texts substituted (copies, never the stored review's own objects) and the
    held line after the last slotted item it accounts for."""
    out: list[Item] = []
    last_slotted = -1
    for item in items:
        if id(item) in dropped:
            continue
        if id(item) in shortened:
            item = item.model_copy(update={"text": shortened[id(item)]})
        out.append(item)
        if item.slot:
            last_slotted = len(out) - 1
    if held is not None:
        out.insert(last_slotted + 1, held)
    return out


def fit(
    items: Sequence[Item],
    box: TextBox,
    *,
    budget: SlideBudget,
    paragraphs_of: Paragrapher,
    min_groups: int = 1,
) -> FitResult:
    """Make a slide's items fit its box, or come as close as dropping allows.

    Order: the largest size in the budget's ladder at which everything fits;
    failing that, at the floor size, drop whole groups from the end (least
    serious last) and say so in a code-written line; then shorten each
    remaining slotted item at sentence boundaries to its character budget.
    Never below `min_groups` groups. Pinned items (no slot) are measured and
    kept whatever happens.
    """
    current = list(items)
    for size in budget.sizes:
        if fits(paragraphs_of(current, size), box):
            return FitResult(items=current, size=size)

    size = budget.floor
    grouped = groups(current)
    dropped: set[int] = set()
    dropped_items: list[Item] = []
    held: Item | None = None
    while len(grouped) > min_groups:
        for item in grouped.pop():
            dropped.add(id(item))
            dropped_items.append(item)
        held = _held_line(dropped_items)
        candidate = _assemble(current, dropped, {}, held)
        if fits(paragraphs_of(candidate, size), box):
            return FitResult(items=candidate, size=size, dropped=dropped_items, held_line=held)

    # Down to the floor and still over. Shorten what is left, longest first.
    kept = [item for group in grouped for item in group]
    shortened: dict[int, str] = {}
    for item in sorted(kept, key=lambda i: -len(i.text)):
        shorter = shorten(item.text, budget.chars_per_item)
        if shorter != item.text:
            shortened[id(item)] = shorter
        candidate = _assemble(current, dropped, shortened, held)
        if fits(paragraphs_of(candidate, size), box):
            break
    return FitResult(
        items=_assemble(current, dropped, shortened, held),
        size=size,
        dropped=dropped_items,
        shortened=len(shortened),
        held_line=held,
    )

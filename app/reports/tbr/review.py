"""The engineer review step: what a person may do to a drafted review before it renders.

Every operation here changes the stored document and names who made the change.
Three of them decide whether something reaches a slide at all:

- **approve** — Industry Trends came from the web, so it is never rendered on
  the model's say-so. Approval puts the engineer's name on it.
- **confirm** — a `gap` finding that only one tool supports is held back until
  an engineer confirms it, or dismisses it, having checked the other console.
- **add / supply** — slides the connectors cannot fill (accomplishments, CX
  scores, scheduled projects, an industry) are filled by the engineer, and are
  left out of the deck if they are not.

Engineer text is trusted as a source (the map's **E**), so it may contain
figures the engineer typed. It is still checked for template placeholder text,
because the render gate would refuse it anyway and saying so now is kinder.
"""

from __future__ import annotations

import datetime as dt

from app.reports.tbr.gate import FORBIDDEN_STRINGS, TEMPLATE_SAMPLES, note_problems
from app.reports.tbr.model import Item, ReviewDocument, Section


class ReviewError(ValueError):
    """The requested change cannot be made; the message says why."""


def _check_text(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ReviewError("the text is empty")
    if len(cleaned) > 600:
        raise ReviewError("keep it under 600 characters: it has to fit on a slide")
    lowered = cleaned.casefold()
    for forbidden in (*FORBIDDEN_STRINGS, *TEMPLATE_SAMPLES):
        hit = forbidden in cleaned if forbidden == "XX" else forbidden.casefold() in lowered
        if hit:
            raise ReviewError(f"the text contains template placeholder text {forbidden!r}")
    return cleaned


def _section(document: ReviewDocument, key: str) -> Section:
    try:
        return document.section(key)
    except KeyError as exc:
        raise ReviewError(f"no section {key!r} in this review") from exc


def _editable(section: Section) -> None:
    if section.status == "static":
        raise ReviewError(f"{section.title} is a static slide and is kept as it is")
    if section.status == "deferred":
        raise ReviewError(
            f"{section.title} waits on {section.later_connector}; it is not filled by hand"
        )


def _touch(section: Section) -> None:
    """Any change after approval withdraws it: what was approved is no longer what is there."""
    if section.requires_approval:
        section.approved_by = None
        section.approved_at = None


def add_item(document: ReviewDocument, key: str, text: str, *, by: str) -> Item:
    section = _section(document, key)
    _editable(section)
    item = Item(text=_check_text(text), cites=[f"engineer:{by}"], author="engineer")
    section.engineer_items.append(item)
    if section.status in ("awaiting_engineer", "dropped"):
        section.status = "filled"
        section.reason = f"Supplied by {by} at review."
        section.waiting_for = None
    _touch(section)
    return item


def edit_item(document: ReviewDocument, key: str, index: int, text: str, *, by: str) -> Item:
    section = _section(document, key)
    _editable(section)
    items = section.all_items()
    if not 0 <= index < len(items):
        raise ReviewError(f"{section.title} has no item {index}")
    item = items[index]
    item.text = _check_text(text)
    item.author = "engineer"
    item.cites = [*[c for c in item.cites if not c.startswith("engineer:")], f"engineer:{by}"]
    _touch(section)
    return item


def remove_item(document: ReviewDocument, key: str, index: int, *, by: str) -> None:
    section = _section(document, key)
    _editable(section)
    drafted = len(section.items)
    if 0 <= index < drafted:
        del section.items[index]
    elif drafted <= index < drafted + len(section.engineer_items):
        del section.engineer_items[index - drafted]
    else:
        raise ReviewError(f"{section.title} has no item {index}")
    if not section.all_items() and not section.fact_ids:
        section.status = "awaiting_engineer"
        section.reason = f"Emptied by {by} at review."
    _touch(section)


def approve(document: ReviewDocument, key: str, *, by: str) -> Section:
    section = _section(document, key)
    if not section.requires_approval:
        raise ReviewError(f"{section.title} does not need approval")
    if not section.all_items():
        raise ReviewError(f"{section.title} has nothing to approve yet")
    section.approved_by = by
    section.approved_at = dt.datetime.now(dt.UTC)
    section.status = "filled"
    section.waiting_for = None
    return section


def set_talk_track(document: ReviewDocument, key: str, text: str, *, by: str) -> Section:
    """The engineer's speaker notes for one slide. An empty text clears them.

    These are the only notes a client deck carries. They are the engineer's own
    words for presenting the slide, so they are held to what the gate holds a deck's
    notes to and no more: no fill-in marks, none of the template's own notes text.
    Ordinary phrases that are refused on a slide ("talk track", "insert") are fine.
    """
    section = _section(document, key)
    cleaned = " ".join(text.split())
    if not cleaned:
        section.talk_track, section.talk_track_by = None, None
        return section
    if len(cleaned) > 2000:
        raise ReviewError("keep speaker notes under 2,000 characters")
    problems = note_problems(cleaned)
    if problems:
        raise ReviewError(f"the notes contain {problems[0]}")
    section.talk_track, section.talk_track_by = cleaned, by
    return section


def set_contact_names(document: ReviewDocument, show: bool | None, *, by: str) -> None:
    """Override the franchisee's `show_people_names` for this review: True names the
    customer's people (the contact chart, the lifecycle tables' users, a person-named
    location), False prints Contact A, User A and withholds the location, None clears
    the override so the franchisee's default applies again.

    The choice belongs to the engineer who knows who will see the deck, it is made
    per review, and who made it is recorded with it.
    """
    document.show_contact_names = show
    document.show_contact_names_by = None if show is None else by


def set_scores(
    document: ReviewDocument,
    *,
    nps: str | None,
    csat: str | None,
    pqual: str | None,
    comments: list[str],
    by: str,
) -> Section:
    section = _section(document, "cx")
    scores = {"nps": nps, "csat": csat, "pqual": pqual}
    for name, value in scores.items():
        if value is None or value == "":
            continue
        if not value.lstrip("-+").replace(".", "", 1).isdigit():
            raise ReviewError(f"{name} must be a number, as the survey tool reports it")
    section.data = {name: value for name, value in scores.items() if value}
    section.engineer_items = [
        Item(text=_check_text(c), cites=[f"engineer:{by}"], author="engineer")
        for c in comments[:3]
        if c.strip()
    ]
    if section.data or section.engineer_items:
        section.status = "filled"
        section.reason = f"Survey results entered by {by} at review."
        section.waiting_for = None
    return section


def confirm_finding(document: ReviewDocument, finding_id: str, *, by: str) -> None:
    try:
        finding = document.finding(finding_id)
    except KeyError as exc:
        raise ReviewError(f"no finding {finding_id!r} in this review") from exc
    if not finding.needs_confirmation:
        raise ReviewError(f"{finding_id} does not need confirming: it is already asserted")
    finding.confirmed_by = by


def dismiss_finding(document: ReviewDocument, finding_id: str, *, by: str) -> None:
    before = len(document.findings)
    document.findings = [f for f in document.findings if f.id != finding_id]
    if len(document.findings) == before:
        raise ReviewError(f"no finding {finding_id!r} in this review")
    for section in document.sections:
        if finding_id in section.finding_ids:
            section.finding_ids.remove(finding_id)


def status_report(document: ReviewDocument) -> list[dict[str, str | int]]:
    """One line per slide: what state it is in and why. What the run prints."""
    from app.reports.tbr.render import slides_to_keep  # a cycle otherwise

    keep = slides_to_keep(document)
    rows: list[dict[str, str | int]] = []
    for section in sorted(document.sections, key=lambda s: s.slide):
        status = section.status
        if section.status == "static" and section.slide not in keep:
            status, why = "dropped", "Divider for a section with no slides left under it."
        elif section.status == "filled" and section.requires_approval and not section.approved_by:
            status, why = "awaiting_engineer", section.reason
        else:
            why = section.reason
        rows.append(
            {
                "slide": section.slide,
                "title": section.title,
                "status": status,
                "on_deck": "yes" if section.slide in keep else "no",
                "narrative": section.narrative,
                "why": why,
                "waiting_for": section.waiting_for or "",
                "later_connector": section.later_connector or "",
            }
        )
    return rows

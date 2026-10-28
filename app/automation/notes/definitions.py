"""What a note workflow is: a fixed template, filled from facts read from the
tools, each with the time it was read.

A note workflow is the engine's first real kind of work. It is triggered by
an alert rule on the new-ticket poll, reads a handful of facts through
`Tools`, and returns a `Draft`: the title and body of one internal note, the
facts it was filled from, and the reads that did not answer. No model and no
generated text anywhere: the body is Python string formatting of values a
tool returned, and the words around them are in this package's source.

**The reads are injected.** `Tools` is a protocol; the engine imports no
connector and the tests hand in a fake. The live implementation is
`app/tickets/alert_tools.py`, which reads through the same connector tools
the ticket page uses. Every read comes back as a `Read`: the value and the
time it was read, or the reason it was not — never a blank that could be
mistaken for a fact.

**What a draft may say about a read that failed.** One line, in the note,
saying which tool did not answer. What it may not do is guess: a workflow
whose *core* fact did not come back returns `Skip` and no note is written.
Each workflow says which fact is core.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.automation.definitions import Trigger
from app.automation.triggers import Arrival

# --- reads -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Read[T]:
    """One read of one tool: what came back and when, or why nothing did."""

    source: str
    read_at: dt.datetime
    value: T | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def of(cls, source: str, value: T, *, at: dt.datetime) -> Read[T]:
        return cls(source=source, read_at=at, value=value)

    @classmethod
    def failed(cls, source: str, error: str, *, at: dt.datetime) -> Read[T]:
        return cls(source=source, read_at=at, error=error)


@dataclass(frozen=True, slots=True)
class DeviceMatch:
    """Which machine the ticket is about, by the existing device resolution."""

    device_id: str
    name: str
    site_slug: str | None
    confidence: str
    """`high`, `medium` or `low`, as `app.tickets.resolve` grades it."""
    method: str

    @property
    def confident(self) -> bool:
        return self.confidence in ("high", "medium")


@dataclass(frozen=True, slots=True)
class DeviceState:
    online: bool | None
    last_contact: dt.datetime | None
    last_boot: dt.datetime | None


@dataclass(frozen=True, slots=True)
class AlertFact:
    subject: str
    created_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class VolumeFact:
    name: str
    capacity_gb: float | None
    free_gb: float | None
    free_percent: float | None


@dataclass(frozen=True, slots=True)
class TicketFact:
    ticket_id: str
    number: str
    title: str
    status: str | None
    is_open: bool
    created_at: dt.datetime | None
    description: str | None = None
    site_slug: str | None = None


@dataclass(frozen=True, slots=True)
class ProtectionFact:
    state: str | None
    last_seen: dt.datetime | None
    malware_last_24h: bool | None
    still_infected: bool | None
    isolated: bool | None


@dataclass(frozen=True, slots=True)
class DetectionFact:
    name: str
    action: str | None
    status: str | None
    created_at: dt.datetime | None


class Tools(Protocol):
    """The reads a note may make. Implemented live in `app/tickets/alert_tools.py`
    and by a fake in the tests; the engine never implements it."""

    async def resolve_device(self, ticket: Arrival) -> DeviceMatch | None: ...

    async def device_state(self, device: DeviceMatch) -> Read[DeviceState]: ...

    async def open_alerts(self, device: DeviceMatch) -> Read[list[AlertFact]]: ...

    async def volumes(self, device: DeviceMatch) -> Read[list[VolumeFact]]: ...

    async def device_tickets(self, device: DeviceMatch, *, days: int) -> Read[list[TicketFact]]: ...

    async def endpoint_protection(self, device: DeviceMatch) -> Read[ProtectionFact]: ...

    async def detections(self, device: DeviceMatch, *, hours: int) -> Read[list[DetectionFact]]: ...

    async def alert_tickets(self, *, days: int) -> Read[list[TicketFact]]: ...


# --- what a workflow produces -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fact:
    """One value the template was filled from, with where and when it was read."""

    id: str
    label: str
    value: Any
    source: str
    read_at: dt.datetime

    def as_dict(self) -> dict[str, Any]:
        value = self.value
        if isinstance(value, dt.datetime):
            value = value.isoformat()
        return {
            "id": self.id,
            "label": self.label,
            "value": value,
            "source": self.source,
            "read_at": self.read_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ReadFailure:
    source: str
    what: str
    reason: str
    read_at: dt.datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "what": self.what,
            "reason": self.reason,
            "read_at": self.read_at.isoformat(),
        }

    @property
    def line(self) -> str:
        """The one line the note carries for a read that did not answer."""
        return f"{self.what}: {self.source} did not answer ({self.reason})."


@dataclass(frozen=True, slots=True)
class Draft:
    """One note, ready to keep: the title without its `Desk:` prefix (the
    service adds it), the body, and what it was made from."""

    title: str
    body: str
    facts: tuple[Fact, ...] = ()
    failures: tuple[ReadFailure, ...] = ()
    device: DeviceMatch | None = None


@dataclass(frozen=True, slots=True)
class Skip:
    """No note, and why: the core fact was missing, the device match was not
    confident, the format was not one we parse."""

    reason: str


@dataclass(frozen=True, slots=True)
class NoteContext:
    ticket: Arrival
    tools: Tools
    settings: Mapping[str, Any]
    now: dt.datetime


BuildFn = Callable[[NoteContext], Awaitable[Draft | Skip]]


@dataclass(frozen=True, slots=True)
class ReportContext:
    tools: Tools
    settings: Mapping[str, Any]
    now: dt.datetime
    period_start: dt.datetime
    period_end: dt.datetime


ReportFn = Callable[[ReportContext], Awaitable[dict[str, Any]]]


class InvalidNoteWorkflow(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NoteWorkflow:
    """A note workflow: a trigger, the reads it makes, a template, a builder."""

    name: str
    version: int
    title: str
    description: str
    trigger: Trigger
    """An `alert` rule for a note workflow; a `schedule` for a report."""
    reads: tuple[str, ...]
    """What it reads, in words, for the workflow page: one entry per tool read."""
    template: str
    """The note's fixed wording with `<placeholders>`, for the workflow page.
    The builder's output is this with values in; nothing else."""
    build: BuildFn | None = None
    report: ReportFn | None = None
    settings_used: tuple[tuple[str, str], ...] = ()
    writes: str = "one internal note on the ticket"
    """What leaves the app, in words. A report writes nothing anywhere."""
    definition_hash: str = ""
    """The SHA-256 of the canonical form of the definition this workflow was
    compiled from (`library.canonical`)."""
    step_views: tuple[dict[str, Any], ...] = ()
    """The definition's steps as the workflow page lists them: id, action,
    title, whether it writes, its connectors."""

    def __post_init__(self) -> None:
        if self.version < 1:
            raise InvalidNoteWorkflow(f"{self.name}: version must be 1 or more")
        if (self.build is None) == (self.report is None):
            raise InvalidNoteWorkflow(f"{self.name}: exactly one of build or report")
        if self.build is not None and self.trigger.kind != "alert":
            raise InvalidNoteWorkflow(f"{self.name}: a note workflow is started by an alert rule")
        if self.report is not None and self.trigger.kind != "schedule":
            raise InvalidNoteWorkflow(f"{self.name}: a report workflow is started by a schedule")
        if not self.reads or not self.template.strip():
            raise InvalidNoteWorkflow(f"{self.name}: needs its reads and its template in words")

    @property
    def kind(self) -> str:
        return "note" if self.build is not None else "report"

    @property
    def manual(self) -> bool:
        return False


_NOTE_WORKFLOWS: dict[str, NoteWorkflow] = {}


def register_note_workflow(workflow: NoteWorkflow) -> NoteWorkflow:
    _NOTE_WORKFLOWS[workflow.name] = workflow
    return workflow


def note_workflow(name: str) -> NoteWorkflow:
    try:
        return _NOTE_WORKFLOWS[name]
    except KeyError:
        raise KeyError(f"no note workflow called {name!r}") from None


def note_workflows() -> tuple[NoteWorkflow, ...]:
    return tuple(_NOTE_WORKFLOWS[name] for name in sorted(_NOTE_WORKFLOWS))


# --- shared words ---------------------------------------------------------------------


def when(moment: dt.datetime | None) -> str:
    """A time as a note prints it: the minute, in UTC, or `unknown`."""
    if moment is None:
        return "unknown"
    return moment.astimezone(dt.UTC).strftime("%d %b %Y %H:%M UTC")


def minutes_between(earlier: dt.datetime | None, later: dt.datetime | None) -> int | None:
    if earlier is None or later is None:
        return None
    return max(0, int((later - earlier).total_seconds() // 60))


def about(minutes: int | None) -> str:
    """`about 43 min`, `about 3 h 10 min`, `about 2 days`."""
    if minutes is None:
        return "an unknown time"
    if minutes < 60:
        return f"about {minutes} min"
    if minutes < 48 * 60:
        hours, rest = divmod(minutes, 60)
        return f"about {hours} h {rest} min" if rest else f"about {hours} h"
    return f"about {minutes // (24 * 60)} days"


def normalised_kind(title: str, device_name: str | None = None) -> str:
    """The kind of an alert from its title: the device's name taken out,
    then lower case, digits and percentages folded, whitespace collapsed —
    so `Disk free space below 10%` and `Disk free space below 5%` are one
    kind and `Device offline` is another, and `Device offline: CON-LT-043`
    and `Device offline: con-lt-043.tailwind.local` are one kind too.

    The name comes out *before* the digits fold, because a name whose digits
    have been folded is every name like it: `CON-LT-043` and `CON-LT-044`
    would both read `con lt #`, and a ticket about the neighbouring machine
    would then be a repeat of this one. The device decides which tickets are
    read; the kind decides only whether they are the same alert."""
    return _fold(strip_device_name(title, device_name))


def _fold(text: str) -> str:
    out: list[str] = []
    for ch in text.casefold():
        if ch.isdigit():
            if out and out[-1] == "#":
                continue
            out.append("#")
        elif ch.isalnum() or ch == " ":
            out.append(ch)
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def _name_forms(device_name: str) -> list[str]:
    """The ways a device's name is written in a ticket: the name as held and
    its host part, longest first so a qualified form is taken whole."""
    host = device_name.split(".")[0]
    forms = {device_name.strip(), host.strip()}
    return sorted((f for f in forms if f), key=len, reverse=True)


def strip_device_name(title: str, device_name: str | None) -> str:
    """The title with every mention of the device taken out: the name as the
    identity layer holds it, its host part, and either followed by a domain
    (`con-lt-043.tailwind.local`). Case-insensitive, whole words only, so
    `CON-LT-04` inside `CON-LT-043` is left alone."""
    if not device_name:
        return title
    forms = "|".join(re.escape(f) for f in _name_forms(device_name))
    pattern = re.compile(rf"(?<![A-Za-z0-9])(?:{forms})(?:\.[A-Za-z0-9.-]+)?(?![A-Za-z0-9-])", re.I)
    return pattern.sub(" ", title)


def names_another_device(title: str, device_name: str) -> bool:
    """True when the title names a machine whose name is this device's with
    different digits: `CON-LT-044` on a ticket being compared with
    `CON-LT-043`'s. The digits fold makes the two kinds equal; this is what
    keeps the neighbouring machine's ticket from counting as a repeat."""
    host = device_name.split(".")[0]
    wanted = _fold(host)
    if not any(ch.isdigit() for ch in host):
        return False
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9.-]*", title):
        head = token.split(".")[0]
        if head.casefold() != host.casefold() and _fold(head) == wanted:
            return True
    return False


def contains_any(text: str | None, phrases: tuple[str, ...]) -> str | None:
    """The first phrase found in the text, case-insensitively, or None."""
    folded = (text or "").casefold()
    for phrase in phrases:
        if phrase.casefold() in folded:
            return phrase
    return None


def settings_tuple(
    settings: Mapping[str, Any], key: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    """A per-client list from settings, or the code default. Strings only."""
    given = settings.get(key)
    if isinstance(given, list | tuple) and all(isinstance(v, str) for v in given) and given:
        return tuple(given)
    return default


__all__ = [
    "AlertFact",
    "BuildFn",
    "DetectionFact",
    "DeviceMatch",
    "DeviceState",
    "Draft",
    "Fact",
    "InvalidNoteWorkflow",
    "NoteContext",
    "NoteWorkflow",
    "ProtectionFact",
    "Read",
    "ReadFailure",
    "ReportContext",
    "Skip",
    "TicketFact",
    "Tools",
    "VolumeFact",
    "about",
    "contains_any",
    "minutes_between",
    "normalised_kind",
    "note_workflow",
    "note_workflows",
    "register_note_workflow",
    "settings_tuple",
    "when",
]

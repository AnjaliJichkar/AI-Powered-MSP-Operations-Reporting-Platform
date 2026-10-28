"""Building, posting and measuring Desk notes.

The request side and the poll side of the note workflows, in one place:

* **Modes.** A workflow is in `preview` for a client until somebody switches
  it to `auto`, and switching to auto needs the franchisee's approver
  recorded — name, how, when, on the approver list — exactly as a run
  approval does, because it is the one approval that covers every note the
  workflow then posts. Switching back records nobody and is immediate.
* **Building** (`evaluate`). For every alert ticket the poll read, every
  note workflow whose rule matches builds its draft through the injected
  `Tools`; a draft becomes an `AutomationNote` row (the unique constraint
  keeps one per workflow per ticket), audited `built`. In `auto` mode, with
  the kill switch clear, it is posted at once. Every evaluation — matched or
  not, built or skipped and why — is a row in the trigger log.
* **Posting** (`post`). The one call that leaves the app: the writer's
  `add_internal_note`, under the client's kill switch. Who posted is on the
  row and the audit: the engineer who pressed Post, or `auto`.
* **Measuring.** Every matched alert ticket is remembered with when it
  closed, so time-to-close can be compared for tickets with and without a
  note (`closures`, `report`).

Nothing here imports a connector; the writer and the tools arrive as
protocols from the script or the API layer.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import statistics
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.automation import controls, triggers
from app.automation.config import load_automation_config
from app.automation.notes.definitions import (
    Draft,
    NoteContext,
    NoteWorkflow,
    ReportContext,
    Skip,
    Tools,
    note_workflow,
    note_workflows,
)
from app.automation.service import Actor, NotFound, Refused
from app.automation.triggers import Arrival, Evaluated
from app.models.automation import (
    AutomationAlertTicket,
    AutomationNote,
    AutomationNoteMode,
    AutomationReport,
)
from app.models.client import Client
from app.models.enums import AuditEventType, AuditOutcome, Interface

logger = logging.getLogger(__name__)

TITLE_PREFIX = "Desk: "
NOTE_ACTOR = "automation-notes"
PREVIEW = "preview"
AUTO = "auto"


class NoteWriter(Protocol):
    """What posting needs: the Autotask note writer, or a fake."""

    async def add_internal_note(self, ticket_id: str | int, *, title: str, body: str) -> Any: ...


WriterFor = Callable[[uuid.UUID], Awaitable[NoteWriter]]
"""Opens a writer for a client, when and only when a note is to be posted."""


def _context(client: Client) -> AuditContext:
    return AuditContext.new(
        actor_id=NOTE_ACTOR, interface=Interface.SYSTEM, client_id=client.id, client_ref=client.slug
    )


async def _note_event(
    session: AsyncSession,
    context: AuditContext,
    note: AutomationNote,
    action: str,
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    **extra: Any,
) -> None:
    await record_event(
        session,
        context,
        event_type=AuditEventType.AUTOMATION_NOTE,
        outcome=outcome,
        payload={
            "action": action,
            "note_id": str(note.id),
            "workflow": note.workflow_name,
            "version": note.workflow_version,
            "ticket_id": note.ticket_id,
            "ticket_number": note.ticket_number,
            "mode": note.mode,
            "state": note.state,
            **extra,
        },
    )


# --- modes --------------------------------------------------------------------------------


async def modes_for(session: AsyncSession, client_id: uuid.UUID) -> dict[str, AutomationNoteMode]:
    rows = (
        (
            await session.execute(
                select(AutomationNoteMode).where(AutomationNoteMode.client_id == client_id)
            )
        )
        .scalars()
        .all()
    )
    return {row.workflow_name: row for row in rows}


async def mode_of(session: AsyncSession, client_id: uuid.UUID, workflow_name: str) -> str:
    row = await session.get(AutomationNoteMode, (client_id, workflow_name))
    return row.mode if row is not None else PREVIEW


async def set_mode(
    session: AsyncSession,
    actor: Actor,
    *,
    workflow_name: str,
    mode: str,
    authoriser_name: str | None = None,
    authoriser_method: str | None = None,
    authoriser_at: dt.datetime | None = None,
    now: dt.datetime,
) -> AutomationNoteMode:
    """Switch a note workflow's mode for the actor's client.

    `auto` needs the franchisee's approver: a name on the client's approver
    list, how they agreed, and when — the same record a run approval makes,
    and refused the same ways. `preview` needs nobody and takes effect now.
    """
    try:
        definition = note_workflow(workflow_name)
    except KeyError as exc:
        raise Refused(str(exc)) from None
    if definition.kind != "note":
        raise Refused(f"{definition.title} writes nothing to Autotask; it has no posting mode.")
    if mode not in (PREVIEW, AUTO):
        raise Refused(f"A note workflow is in preview or auto, not {mode!r}.")
    row = await session.get(AutomationNoteMode, (actor.client_id, workflow_name))
    was = row.mode if row is not None else PREVIEW
    if row is None:
        row = AutomationNoteMode(client_id=actor.client_id, workflow_name=workflow_name)
        session.add(row)
    approver = None
    if mode == AUTO:
        if not authoriser_name:
            raise Refused(
                "Auto-posting needs the franchisee's approver recorded: who agreed, how, and when."
            )
        config = await load_automation_config(session, actor.client_id)
        approver = config.approver(authoriser_name)
        if approver is None:
            names = ", ".join(a.name for a in config.approvers) or "nobody"
            raise Refused(
                f"{authoriser_name!r} is not on this client's approver list ({names}). "
                f"An approver is added in the client's settings, not here."
            )
        method = " ".join((authoriser_method or "").split()).lower()
        if not method:
            raise Refused("Say how the approver agreed: phone, email, teams, in person.")
        row.authoriser_name = approver.name
        row.authoriser_method = method
        row.authoriser_at = authoriser_at or now
    else:
        row.authoriser_name = None
        row.authoriser_method = None
        row.authoriser_at = None
    row.mode = mode
    row.changed_by_user_id = actor.user_id
    row.changed_by = actor.email
    row.changed_at = now
    await session.flush()
    await record_event(
        session,
        actor.context(),
        event_type=AuditEventType.AUTOMATION_CONTROL,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "control": "note_mode",
            "workflow": workflow_name,
            "before": was,
            "after": mode,
            "by": actor.email,
            "authoriser": (
                {
                    "name": row.authoriser_name,
                    "role": approver.role if approver else None,
                    "method": row.authoriser_method,
                    "at": row.authoriser_at.isoformat() if row.authoriser_at else None,
                }
                if mode == AUTO
                else None
            ),
        },
    )
    return row


# --- reading notes --------------------------------------------------------------------------


async def get_note(
    session: AsyncSession, client_id: uuid.UUID, note_id: uuid.UUID
) -> AutomationNote:
    note = (
        await session.execute(
            select(AutomationNote).where(
                AutomationNote.id == note_id, AutomationNote.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if note is None:
        raise NotFound("There is no such note for this client.")
    return note


async def notes_for_ticket(
    session: AsyncSession, client_id: uuid.UUID, ticket_ref: str
) -> list[AutomationNote]:
    """The notes on one ticket, by the vendor's id or the number a person
    reads — the ticket page holds whichever it was opened with."""
    return list(
        (
            await session.execute(
                select(AutomationNote)
                .where(
                    AutomationNote.client_id == client_id,
                    (AutomationNote.ticket_id == ticket_ref)
                    | (AutomationNote.ticket_number == ticket_ref),
                )
                .order_by(AutomationNote.built_at)
            )
        )
        .scalars()
        .all()
    )


async def existing_note(
    session: AsyncSession, client_id: uuid.UUID, workflow_name: str, ticket_id: str
) -> AutomationNote | None:
    return (
        await session.execute(
            select(AutomationNote).where(
                AutomationNote.client_id == client_id,
                AutomationNote.workflow_name == workflow_name,
                AutomationNote.ticket_id == ticket_id,
            )
        )
    ).scalar_one_or_none()


# --- building ---------------------------------------------------------------------------------


@dataclass
class _Gate:
    client: Client
    enabled: dict[str, bool]
    trigger_enabled: dict[tuple[str, str], bool]
    halted: bool
    overrides: dict[str, dict[str, Any]]
    settings: dict[str, dict[str, Any]]
    modes: dict[str, AutomationNoteMode]

    @classmethod
    async def read(cls, session: AsyncSession, client_id: uuid.UUID) -> _Gate:
        client = await session.get(Client, client_id)
        if client is None:
            raise Refused("There is no such client.")
        switches = await controls.switches_for(session, client_id)
        trigger_switches = await triggers.trigger_switches_for(session, client_id)
        config = await load_automation_config(session, client_id)
        return cls(
            client=client,
            enabled={name: row.enabled for name, row in switches.items()},
            trigger_enabled={key: row.enabled for key, row in trigger_switches.items()},
            halted=await controls.is_halted(session, client_id),
            overrides={
                name: dict(settings.get("triggers") or {})
                for name, settings in config.workflows.items()
            },
            settings={name: dict(settings) for name, settings in config.workflows.items()},
            modes=await modes_for(session, client_id),
        )

    def rule_for(self, definition: NoteWorkflow) -> Any:
        assert definition.trigger.rule is not None
        given = (self.overrides.get(definition.name) or {}).get(definition.trigger.key) or {}
        return definition.trigger.rule.with_overrides(given)

    def why_not(self, definition: NoteWorkflow) -> str | None:
        if not self.enabled.get(definition.name):
            return f"the {definition.title} workflow is switched off for this client"
        if not self.trigger_enabled.get((definition.name, definition.trigger.key)):
            word = triggers.KIND_WORDS[definition.trigger.kind]
            return f"the {word} trigger is switched off for this client"
        return None

    def mode(self, definition: NoteWorkflow) -> str:
        row = self.modes.get(definition.name)
        return row.mode if row is not None else PREVIEW


async def _remember_alert_ticket(
    session: AsyncSession,
    client_id: uuid.UUID,
    workflow_name: str,
    arrival: Arrival,
    note: AutomationNote | None,
) -> None:
    row = await session.get(AutomationAlertTicket, (client_id, arrival.ticket_id))
    if row is None:
        row = AutomationAlertTicket(
            client_id=client_id,
            ticket_id=arrival.ticket_id,
            ticket_number=arrival.ticket_number,
            workflow_name=workflow_name,
            created_at=arrival.created_at,
        )
        session.add(row)
    if note is not None and row.note_id is None:
        row.note_id = note.id
    await session.flush()


async def build_note(
    session: AsyncSession,
    client: Client,
    definition: NoteWorkflow,
    arrival: Arrival,
    *,
    tools: Tools,
    settings: Mapping[str, Any],
    mode: str,
    now: dt.datetime,
) -> tuple[AutomationNote | None, str]:
    """Build one workflow's note for one ticket and keep it, or say why not.

    Idempotent: a note that already exists for the workflow and ticket is
    returned with the reason, and the unique constraint stands behind that
    for two polls that race. The draft is the workflow's; this adds the
    title's prefix, the row and the audit.
    """
    assert definition.build is not None
    already = await existing_note(session, client.id, definition.name, arrival.ticket_id)
    if already is not None:
        return (
            already,
            f"a note already exists for this ticket ({already.state}); one per workflow per ticket",
        )
    result = await definition.build(
        NoteContext(ticket=arrival, tools=tools, settings=settings, now=now)
    )
    if isinstance(result, Skip):
        return None, f"no note: {result.reason}"
    draft: Draft = result
    note = AutomationNote(
        client_id=client.id,
        workflow_name=definition.name,
        workflow_version=definition.version,
        definition_hash=definition.definition_hash or None,
        ticket_id=arrival.ticket_id,
        ticket_number=arrival.ticket_number,
        ticket_created_at=arrival.created_at,
        site_slug=arrival.site_slug or (draft.device.site_slug if draft.device else None),
        device_name=draft.device.name if draft.device else None,
        mode=mode,
        state="built",
        title=TITLE_PREFIX + draft.title,
        body=draft.body,
        facts=[f.as_dict() for f in draft.facts],
        read_failures=[f.as_dict() for f in draft.failures],
        built_at=now,
    )
    session.add(note)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        again = await existing_note(session, client.id, definition.name, arrival.ticket_id)
        return again, "a note already exists for this ticket; one per workflow per ticket"
    await _note_event(
        session,
        _context(client),
        note,
        "built",
        facts=note.facts,
        read_failures=note.read_failures,
        device=note.device_name,
    )
    return note, "built"


async def post(
    session: AsyncSession,
    client: Client,
    note: AutomationNote,
    *,
    writer: NoteWriter,
    by: Actor | None,
    now: dt.datetime,
) -> AutomationNote:
    """Post one note to Autotask: the only call that leaves the app.

    Refused while the client's kill switch is thrown — it stops all posting,
    by an engineer or by auto-mode alike — and for a note that is already
    posted or dismissed. A writer that fails leaves the note `post_failed`
    with the error, to be posted again or dismissed; the failure is audited
    in a transaction of its own by the caller's design (one note per call).
    """
    if await controls.is_halted(session, client.id):
        raise Refused(
            "Automation is halted for this client; nothing is posted until it is cleared."
        )
    if note.state not in ("built", "post_failed"):
        raise Refused(f"This note is {note.state.replace('_', ' ')} and cannot be posted.")
    context = by.context() if by is not None else _context(client)
    who = by.email if by is not None else "auto"
    try:
        posted = await writer.add_internal_note(note.ticket_id, title=note.title, body=note.body)
    except Exception as exc:
        note.state = "post_failed"
        note.post_error = f"{type(exc).__name__}: {exc}"
        await _note_event(
            session,
            context,
            note,
            "post_failed",
            outcome=AuditOutcome.ERROR,
            by=who,
            reason=note.post_error,
        )
        return note
    note.state = "posted"
    note.posted_at = now
    note.posted_by = who
    note.posted_by_user_id = by.user_id if by is not None else None
    note.autotask_note_id = str(getattr(posted, "note_id", "") or "") or None
    note.post_error = None
    await _note_event(
        session,
        context,
        note,
        "posted",
        by=who,
        autotask_note_id=note.autotask_note_id,
        publish=getattr(posted, "publish", None),
        note_type=getattr(posted, "note_type", None),
    )
    return note


async def mark_not_useful(
    session: AsyncSession, client: Client, note: AutomationNote, *, by: Actor, now: dt.datetime
) -> AutomationNote:
    if note.state == "not_useful":
        raise Refused("This note is already marked not useful.")
    note.state = "not_useful"
    note.not_useful_at = now
    note.not_useful_by = by.email
    note.not_useful_by_user_id = by.user_id
    await _note_event(session, by.context(), note, "not_useful", by=by.email)
    return note


async def evaluate(
    session: AsyncSession,
    client_id: uuid.UUID,
    arrivals: list[Arrival],
    *,
    tools: Tools,
    writer_for: WriterFor | None,
    now: dt.datetime,
) -> list[Evaluated]:
    """Run every note workflow's alert rule over the tickets that arrived;
    build and keep a note for each match that may have one; post it when the
    workflow is in auto for this client and the kill switch is clear. Every
    evaluation is a trigger-log row. The caller owns the transaction."""
    gate = await _Gate.read(session, client_id)
    found: list[Evaluated] = []
    for definition in note_workflows():
        if definition.kind != "note":
            continue
        rule = gate.rule_for(definition)
        made = functools.partial(
            Evaluated, workflow=definition.name, trigger=definition.trigger.key, kind="alert"
        )
        for arrival in arrivals:
            matched, why = triggers.match(rule, arrival)
            if not matched:
                result = made(matched=False, outcome="no_match", reason=why, ticket=arrival)
            else:
                skipped = gate.why_not(definition)
                if skipped is not None:
                    result = made(
                        matched=True,
                        outcome="skipped",
                        reason=f"{why}; skipped: {skipped}",
                        ticket=arrival,
                    )
                else:
                    result = await _build_and_maybe_post(
                        session, gate, definition, arrival, tools, writer_for, now, made, why
                    )
            await triggers._log(session, client_id, result, rule=rule, now=now)
            found.append(result)
    await triggers.prune_log(session, client_id)
    return found


async def _build_and_maybe_post(
    session: AsyncSession,
    gate: _Gate,
    definition: NoteWorkflow,
    arrival: Arrival,
    tools: Tools,
    writer_for: WriterFor | None,
    now: dt.datetime,
    made: functools.partial[Evaluated],
    why: str,
) -> Evaluated:
    mode = gate.mode(definition)
    note, reason = await build_note(
        session,
        gate.client,
        definition,
        arrival,
        tools=tools,
        settings=gate.settings.get(definition.name, {}),
        mode=mode,
        now=now,
    )
    await _remember_alert_ticket(session, gate.client.id, definition.name, arrival, note)
    if note is None:
        return made(matched=True, outcome="skipped", reason=f"{why}; {reason}", ticket=arrival)
    if reason != "built":
        return made(
            matched=True, outcome="skipped", reason=f"{why}; skipped: {reason}", ticket=arrival
        )
    words = f"{why}; built a Desk note ({mode})"
    if mode == AUTO:
        if gate.halted:
            words += "; not posted: automation is halted for this client"
        elif writer_for is None:
            words += "; not posted: no writer in this run"
        else:
            try:
                writer = await writer_for(gate.client.id)
                await post(session, gate.client, note, writer=writer, by=None, now=now)
                words += (
                    "; posted" if note.state == "posted" else f"; post failed: {note.post_error}"
                )
            except Refused as refused:
                words += f"; not posted: {refused}"
    return made(matched=True, outcome="noted", reason=words, ticket=arrival)


# --- the report workflow ----------------------------------------------------------------------


async def latest_report(
    session: AsyncSession, client_id: uuid.UUID, workflow_name: str
) -> AutomationReport | None:
    return (
        await session.execute(
            select(AutomationReport)
            .where(
                AutomationReport.client_id == client_id,
                AutomationReport.workflow_name == workflow_name,
            )
            .order_by(AutomationReport.built_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def build_due_reports(
    session: AsyncSession,
    client_id: uuid.UUID,
    *,
    tools: Tools,
    now: dt.datetime,
    force: bool = False,
) -> list[AutomationReport]:
    """Build every report workflow that is on for this client and due — its
    last report older than its schedule's interval — over the interval's
    length. Called by the poll, which has the tools; logged like a trigger."""
    gate = await _Gate.read(session, client_id)
    built: list[AutomationReport] = []
    for definition in note_workflows():
        if definition.kind != "report":
            continue
        assert definition.report is not None and definition.trigger.every is not None
        skipped = gate.why_not(definition)
        last = await latest_report(session, client_id, definition.name)
        if not force and last is not None and last.built_at + definition.trigger.every > now:
            continue
        made = functools.partial(
            Evaluated, workflow=definition.name, trigger=definition.trigger.key, kind="schedule"
        )
        if skipped is not None:
            if last is None or force:
                await triggers._log(
                    session,
                    client_id,
                    made(matched=True, outcome="skipped", reason=f"due; skipped: {skipped}"),
                    rule=None,
                    now=now,
                )
            continue
        period_start = now - definition.trigger.every
        body = await definition.report(
            ReportContext(
                tools=tools,
                settings=gate.settings.get(definition.name, {}),
                now=now,
                period_start=period_start,
                period_end=now,
            )
        )
        report = AutomationReport(
            client_id=client_id,
            workflow_name=definition.name,
            period_start=period_start,
            period_end=now,
            built_at=now,
            body=body,
        )
        session.add(report)
        await session.flush()
        await record_event(
            session,
            _context(gate.client),
            event_type=AuditEventType.AUTOMATION_NOTE,
            outcome=AuditOutcome.SUCCESS if not body.get("error") else AuditOutcome.ERROR,
            payload={
                "action": "report_built",
                "workflow": definition.name,
                "report_id": report.id,
                "devices": len(body.get("devices") or []),
                "tickets": body.get("tickets"),
                "error": body.get("error"),
            },
        )
        await triggers._log(
            session,
            client_id,
            made(
                matched=True,
                outcome="noted",
                reason=f"due; built the report ({len(body.get('devices') or [])} devices)",
            ),
            rule=None,
            now=now,
        )
        built.append(report)
    return built


# --- measurement -------------------------------------------------------------------------------


ClosureLookup = Callable[[list[str]], Awaitable[dict[str, dt.datetime | None]]]
"""Given ticket ids, when each completed (None if still open). Injected: the
poll reads Autotask; the tests hand in a dict."""


async def measure_closures(
    session: AsyncSession,
    client_id: uuid.UUID,
    lookup: ClosureLookup,
    *,
    now: dt.datetime,
    limit: int = 200,
) -> int:
    """Stamp `closed_at` on the alert tickets that have none, oldest-checked
    first, at most `limit` a call. Notes with a closure get it too."""
    rows = list(
        (
            await session.execute(
                select(AutomationAlertTicket)
                .where(
                    AutomationAlertTicket.client_id == client_id,
                    AutomationAlertTicket.closed_at.is_(None),
                )
                .order_by(AutomationAlertTicket.checked_at.asc().nulls_first())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    closed = await lookup([row.ticket_id for row in rows])
    stamped = 0
    for row in rows:
        row.checked_at = now
        when = closed.get(row.ticket_id)
        if when is not None:
            row.closed_at = when
            stamped += 1
            if row.note_id is not None:
                note = await session.get(AutomationNote, row.note_id)
                if note is not None:
                    note.ticket_closed_at = when
    await session.flush()
    return stamped


def _median_hours(pairs: list[tuple[dt.datetime | None, dt.datetime | None]]) -> float | None:
    hours = [
        (closed - created).total_seconds() / 3600
        for created, closed in pairs
        if created is not None and closed is not None and closed >= created
    ]
    return round(statistics.median(hours), 1) if hours else None


async def report(
    session: AsyncSession, client_id: uuid.UUID, *, now: dt.datetime, weeks: int = 8
) -> dict[str, Any]:
    """The Notes view: per workflow, notes built, posted (by whom), marked not
    useful and with read failures, per week; and per workflow the shares and
    the median time to close with and without a note."""
    since = now - dt.timedelta(weeks=weeks)
    notes = list(
        (
            await session.execute(
                select(AutomationNote).where(
                    AutomationNote.client_id == client_id, AutomationNote.built_at >= since
                )
            )
        )
        .scalars()
        .all()
    )
    alert_rows = list(
        (
            await session.execute(
                select(AutomationAlertTicket).where(AutomationAlertTicket.client_id == client_id)
            )
        )
        .scalars()
        .all()
    )

    def week_of(moment: dt.datetime) -> str:
        monday = moment.date() - dt.timedelta(days=moment.weekday())
        return monday.isoformat()

    weeks_seen: dict[str, dict[str, dict[str, int]]] = {}
    totals: dict[str, dict[str, int]] = {}
    for note in notes:
        bucket = weeks_seen.setdefault(week_of(note.built_at), {}).setdefault(
            note.workflow_name,
            {
                "built": 0,
                "posted_by_engineer": 0,
                "posted_auto": 0,
                "not_useful": 0,
                "read_failures": 0,
                "post_failed": 0,
            },
        )
        total = totals.setdefault(
            note.workflow_name,
            {
                "built": 0,
                "preview_built": 0,
                "posted_by_engineer": 0,
                "posted_auto": 0,
                "not_useful": 0,
                "read_failures": 0,
                "post_failed": 0,
            },
        )
        for target in (bucket, total):
            target["built"] += 1
            if note.state == "posted" and note.posted_by == "auto":
                target["posted_auto"] += 1
            elif note.state == "posted":
                target["posted_by_engineer"] += 1
            if note.state == "not_useful":
                target["not_useful"] += 1
            if note.state == "post_failed":
                target["post_failed"] += 1
            if note.read_failures:
                target["read_failures"] += 1
        if note.mode == PREVIEW:
            total["preview_built"] += 1

    by_workflow: list[dict[str, Any]] = []
    for definition in note_workflows():
        if definition.kind != "note":
            continue
        total = totals.get(definition.name, {})
        built = total.get("built", 0)
        preview_built = total.get("preview_built", 0)
        with_note = [
            (r.created_at, r.closed_at)
            for r in alert_rows
            if r.workflow_name == definition.name and r.note_id is not None
        ]
        without = [
            (r.created_at, r.closed_at)
            for r in alert_rows
            if r.workflow_name == definition.name and r.note_id is None
        ]
        by_workflow.append(
            {
                "workflow": definition.name,
                "title": definition.title,
                "built": built,
                "posted_by_engineer": total.get("posted_by_engineer", 0),
                "posted_auto": total.get("posted_auto", 0),
                "not_useful": total.get("not_useful", 0),
                "read_failures": total.get("read_failures", 0),
                "post_failed": total.get("post_failed", 0),
                "share_posted_in_preview": (
                    round(total.get("posted_by_engineer", 0) / preview_built, 3)
                    if preview_built
                    else None
                ),
                "share_not_useful": round(total.get("not_useful", 0) / built, 3) if built else None,
                "alert_tickets_with_note": len(with_note),
                "alert_tickets_without_note": len(without),
                "median_hours_to_close_with_note": _median_hours(with_note),
                "median_hours_to_close_without_note": _median_hours(without),
                "closed_with_note": sum(1 for _, c in with_note if c is not None),
                "closed_without_note": sum(1 for _, c in without if c is not None),
            }
        )
    return {
        "since": since.isoformat(),
        "weeks": [
            {"week": week, "workflows": workflows}
            for week, workflows in sorted(weeks_seen.items(), reverse=True)
        ],
        "by_workflow": by_workflow,
        "definitions": {
            "built": "notes built from facts read from the tools, in the window",
            "share_posted_in_preview": (
                "of notes built in preview mode, the share an engineer posted to Autotask"
            ),
            "share_not_useful": "of notes built, the share an engineer marked not useful",
            "median_hours_to_close": (
                "median hours from the alert ticket's creation to Autotask marking it "
                "complete, over tickets this workflow's rule matched; with a note and "
                "without, closed tickets only"
            ),
            "read_failures": "notes whose body carries a line saying a tool did not answer",
        },
    }


async def count_notes(session: AsyncSession, client_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(AutomationNote)
                .where(AutomationNote.client_id == client_id)
            )
        ).scalar_one()
    )

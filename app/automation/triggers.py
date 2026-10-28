"""What starts a run without a person: ticket and alert rules, and schedules.

A trigger drafts a run and nothing more. The run it drafts is dry-run,
submitted and approved by two people exactly as a run an engineer drafted,
so every control is still between a matched ticket and an action.

**Rules are evaluated in code.** A ticket rule is fixed conditions on a
ticket's fields — queue, issue type, sub-issue type, source, a title that
equals or contains a phrase — and a ticket matches when every condition set
holds. No model reads a ticket here, and there is no regex: the conditions a
franchisee can be shown are the conditions there are.

**Tickets arrive through the shared Autotask reader.** Nothing in this module
reads a vendor. `scripts/automation.py poll --client <slug> --once` reads the
tickets that arrived since the last poll through `app/tickets/poller.py` —
the same reader and the same hourly request budget the forward shadow uses —
and hands them to `poll_once` here as `Arrival`s. The reader is injected, so
this module imports no connector; the switch is `AUTOMATION_TRIGGERS_LIVE`,
separate from the shadow's, and the per-client switches still both apply.

**Both switches, and everything else.** A matched ticket drafts a run only
when the workflow is allowed on this kind of client, the workflow's switch is
on, the trigger's own switch is on, the kill switch is clear, and no run —
of any state — already exists for this ticket and workflow. Each of those
refusals is a row in the trigger log saying so, as is every ticket that did
not match and which condition it failed.

**A schedule is a tick of the worker.** The worker's loop already runs in the
background; `tick_schedules` is called from it and drafts a run for every
enabled schedule whose last drafted run is older than its interval. The
same loop cancels stale drafts and expires old approvals (`housekeeping`).
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.automation import controls, service
from app.automation.config import load_automation_config
from app.automation.definitions import TicketRule, Trigger, Workflow, workflows
from app.automation.states import RunState, advance
from app.models.automation import AutomationRun, AutomationTriggerLog, AutomationTriggerSwitch
from app.models.client import Client
from app.models.enums import AuditEventType, AuditOutcome, Interface

logger = logging.getLogger(__name__)

TRIGGER_ACTOR = "automation-trigger"

#: How many log rows are kept per client. The page shows the last hundred;
#: the rest is room for a busy pass before the oldest go.
LOG_KEEP = 1000
LOG_SHOWN = 100

#: A draft, or a run awaiting approval, older than this is cancelled by the
#: worker with an audit row: a checklist nobody approved in a week is about a
#: situation that has moved on.
STALE_AFTER = dt.timedelta(days=7)

KIND_WORDS = {
    "manual": "by hand",
    "ticket": "ticket rule",
    "alert": "alert rule",
    "schedule": "schedule",
}


@dataclass(frozen=True, slots=True)
class Arrival:
    """A ticket as the poller saw it: the fields a rule may read, as labels."""

    ticket_id: str
    ticket_number: str | None
    title: str
    queue: str | None = None
    issue_type: str | None = None
    sub_issue_type: str | None = None
    source: str | None = None
    contact: str | None = None
    company: str | None = None
    site_slug: str | None = None
    created_at: dt.datetime | None = None
    description: str | None = None
    """The body, scrubbed, for the note workflows that read it (a backup
    notice's job and error; whether a threat alert names Blackpoint). Never
    stored by the log."""

    @property
    def label(self) -> str:
        return self.ticket_number or self.ticket_id


@dataclass(frozen=True, slots=True)
class Evaluated:
    """What one trigger made of one arrival (or one tick)."""

    workflow: str
    trigger: str
    kind: str
    matched: bool
    outcome: str
    reason: str
    run_id: uuid.UUID | None = None
    ticket: Arrival | None = None


def _fold(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def match(rule: TicketRule, arrival: Arrival) -> tuple[bool, str]:
    """Whether the arrival meets every condition set, and why not when not.

    Labels are compared case-insensitively on collapsed whitespace. The
    reason names the first condition that failed, with what the ticket had,
    so the log answers "why not this one" without a second look.
    """
    conditions = rule.conditions()
    for name, wanted in conditions.items():
        if name == "title_equals":
            if _fold(arrival.title) != _fold(wanted):
                return False, f"title is not {wanted!r}"
        elif name == "title_contains":
            if _fold(wanted) not in _fold(arrival.title):
                return False, f"title does not contain {wanted!r}"
        elif name == "title_contains_any":
            if not any(_fold(phrase) in _fold(arrival.title) for phrase in wanted):
                return False, f"title contains none of {list(wanted)!r}"
        else:
            had = getattr(arrival, name)
            if _fold(had) != _fold(wanted):
                return False, f"{name.replace('_', ' ')} is {had!r}, not {wanted!r}"
    return True, "matched on " + ", ".join(
        f"{k.replace('_', ' ')} {v!r}" for k, v in conditions.items()
    )


def inputs_from(definition: Workflow, arrival: Arrival | None) -> dict[str, Any]:
    """The inputs a triggered draft fills from the ticket, per `input_sources`.
    A source with nothing on the arrival is left out, so the field takes its
    default; a field the schema requires then refuses the draft, which the
    log records."""
    if arrival is None:
        return {}
    values = {
        "ticket.number": arrival.ticket_number,
        "ticket.title": arrival.title,
        "ticket.contact": arrival.contact,
        "ticket.company": arrival.company,
        "ticket.site": arrival.site_slug,
    }
    found: dict[str, Any] = {}
    for name, source in definition.input_sources.items():
        value = values.get(source)
        if value is not None:
            found[name] = value
    return found


def sentence(trigger: Trigger, rule: TicketRule | None, *, client_name: str) -> str:
    """The trigger as one sentence for the workflow page."""
    if trigger.kind == "manual":
        return f"When somebody starts it from the Automations page for {client_name}."
    if trigger.kind == "schedule":
        assert trigger.every is not None
        hours = trigger.every.total_seconds() / 3600
        if hours == 24:
            every = "every day"
        elif hours % 24 == 0:
            every = f"every {int(hours // 24)} days"
        elif hours == 1:
            every = "every hour"
        else:
            every = f"every {hours:g} hours"
        return f"On a schedule, {every}, for {client_name}."
    assert rule is not None
    parts = []
    names = {
        "queue": "in queue",
        "issue_type": "with issue type",
        "sub_issue_type": "with sub-issue type",
        "source": "from source",
        "title_equals": "titled",
        "title_contains": "whose title contains",
        "title_contains_any": "whose title contains one of",
    }
    for name, value in rule.conditions().items():
        shown = ", ".join(f"“{v}”" for v in value) if isinstance(value, list) else value
        parts.append(f"{names[name]} {shown}")
    what = "an alert raises a ticket" if trigger.kind == "alert" else "a ticket arrives"
    return f"When {what} for {client_name} " + " and ".join(parts) + "."


def system_context(client: Client) -> AuditContext:
    return AuditContext.new(
        actor_id=TRIGGER_ACTOR,
        interface=Interface.SYSTEM,
        client_id=client.id,
        client_ref=client.slug,
    )


# --- the switches ---------------------------------------------------------------------


async def trigger_switches_for(
    session: AsyncSession, client_id: uuid.UUID
) -> dict[tuple[str, str], AutomationTriggerSwitch]:
    rows = (
        (
            await session.execute(
                select(AutomationTriggerSwitch).where(
                    AutomationTriggerSwitch.client_id == client_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {(row.workflow_name, row.trigger_key): row for row in rows}


async def set_trigger_switch(
    session: AsyncSession,
    context: AuditContext,
    *,
    client_id: uuid.UUID,
    workflow_name: str,
    trigger_key: str,
    enabled: bool,
    who: controls.Who,
    now: dt.datetime,
) -> AutomationTriggerSwitch:
    """Turn one trigger on or off for one client. Audited like the other two
    controls; the caller owns the transaction."""
    row = await session.get(AutomationTriggerSwitch, (client_id, workflow_name, trigger_key))
    was = bool(row is not None and row.enabled)
    if row is None:
        row = AutomationTriggerSwitch(
            client_id=client_id, workflow_name=workflow_name, trigger_key=trigger_key
        )
        session.add(row)
    row.enabled = enabled
    row.changed_by_user_id = who.user_id
    row.changed_by = who.label
    row.changed_at = now
    await session.flush()
    await record_event(
        session,
        context,
        event_type=AuditEventType.AUTOMATION_CONTROL,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "control": "trigger_switch",
            "workflow": workflow_name,
            "trigger": trigger_key,
            "before": was,
            "after": enabled,
            "by": who.label,
        },
    )
    return row


# --- evaluating arrivals ------------------------------------------------------------


async def _log(
    session: AsyncSession,
    client_id: uuid.UUID,
    found: Evaluated,
    *,
    rule: TicketRule | None,
    now: dt.datetime,
) -> AutomationTriggerLog:
    row = AutomationTriggerLog(
        client_id=client_id,
        workflow_name=found.workflow,
        trigger_key=found.trigger,
        kind=found.kind,
        ticket_id=found.ticket.ticket_id if found.ticket else None,
        ticket_number=found.ticket.ticket_number if found.ticket else None,
        matched=found.matched,
        outcome=found.outcome,
        reason=found.reason,
        rule=rule.conditions() if rule is not None else None,
        run_id=found.run_id,
        evaluated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def prune_log(session: AsyncSession, client_id: uuid.UUID) -> None:
    """Keep the newest `LOG_KEEP` rows for the client."""
    keep = (
        select(AutomationTriggerLog.id)
        .where(AutomationTriggerLog.client_id == client_id)
        .order_by(AutomationTriggerLog.evaluated_at.desc(), AutomationTriggerLog.id.desc())
        .limit(LOG_KEEP)
    )
    await session.execute(
        delete(AutomationTriggerLog).where(
            AutomationTriggerLog.client_id == client_id,
            AutomationTriggerLog.id.not_in(keep),
        )
    )


async def recent_log(
    session: AsyncSession, client_id: uuid.UUID, *, limit: int = LOG_SHOWN
) -> list[AutomationTriggerLog]:
    return list(
        (
            await session.execute(
                select(AutomationTriggerLog)
                .where(AutomationTriggerLog.client_id == client_id)
                .order_by(AutomationTriggerLog.evaluated_at.desc(), AutomationTriggerLog.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


@dataclass
class _Gate:
    """What stands between a match and a draft for one client, read once."""

    client: Client
    enabled: dict[str, bool]
    trigger_enabled: dict[tuple[str, str], bool]
    halted: bool
    overrides: dict[str, dict[str, Any]]

    @classmethod
    async def read(cls, session: AsyncSession, client_id: uuid.UUID) -> _Gate:
        client = await session.get(Client, client_id)
        if client is None:
            raise service.Refused("There is no such client.")
        switches = await controls.switches_for(session, client_id)
        trigger_switches = await trigger_switches_for(session, client_id)
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
        )

    def rule_for(self, definition: Workflow, trigger: Trigger) -> TicketRule | None:
        if trigger.rule is None:
            return None
        given = (self.overrides.get(definition.name) or {}).get(trigger.key) or {}
        return trigger.rule.with_overrides(given)

    def why_not(self, definition: Workflow, trigger: Trigger) -> str | None:
        """The first reason a matched trigger drafts nothing, or None."""
        reason = definition.allowed_on(self.client.kind)
        if reason:
            return f"the workflow is not allowed on this client: {reason}"
        if not self.enabled.get(definition.name):
            return f"the {definition.title} workflow is switched off for this client"
        if not self.trigger_enabled.get((definition.name, trigger.key)):
            return f"the {KIND_WORDS[trigger.kind]} trigger is switched off for this client"
        if self.halted:
            return "automation is halted for this client"
        return None


async def _existing_run(
    session: AsyncSession, client_id: uuid.UUID, workflow_name: str, ticket_number: str | None
) -> AutomationRun | None:
    if ticket_number is None:
        return None
    return (
        await session.execute(
            select(AutomationRun)
            .where(
                AutomationRun.client_id == client_id,
                AutomationRun.workflow_name == workflow_name,
                AutomationRun.ticket_number == ticket_number,
            )
            .order_by(AutomationRun.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def evaluate(
    session: AsyncSession,
    client_id: uuid.UUID,
    arrivals: list[Arrival],
    *,
    now: dt.datetime,
) -> list[Evaluated]:
    """Run every ticket and alert rule over the tickets that arrived, draft
    what matches and may be drafted, and log every evaluation. The caller
    owns the transaction."""
    gate = await _Gate.read(session, client_id)
    found: list[Evaluated] = []
    for definition in workflows():
        for trigger in definition.triggers:
            if trigger.kind not in ("ticket", "alert"):
                continue
            rule = gate.rule_for(definition, trigger)
            assert rule is not None
            for arrival in arrivals:
                result = await _evaluate_one(session, gate, definition, trigger, rule, arrival, now)
                await _log(session, client_id, result, rule=rule, now=now)
                found.append(result)
    await prune_log(session, client_id)
    return found


async def _evaluate_one(
    session: AsyncSession,
    gate: _Gate,
    definition: Workflow,
    trigger: Trigger,
    rule: TicketRule,
    arrival: Arrival,
    now: dt.datetime,
) -> Evaluated:
    matched, why = match(rule, arrival)
    made = functools.partial(
        Evaluated, workflow=definition.name, trigger=trigger.key, kind=trigger.kind
    )
    if not matched:
        return made(matched=False, outcome="no_match", reason=why, ticket=arrival)
    skipped = gate.why_not(definition, trigger)
    if skipped is None:
        existing = await _existing_run(
            session, gate.client.id, definition.name, arrival.ticket_number
        )
        if existing is not None:
            skipped = (
                f"a run already exists for this ticket ({existing.state}, "
                f"{str(existing.id)[:8]}); one draft per ticket per workflow"
            )
    if skipped is not None:
        return made(
            matched=True,
            outcome="skipped",
            reason=f"{why}; skipped: {skipped}",
            ticket=arrival,
        )
    detail = f"{KIND_WORDS[trigger.kind]} on {arrival.label}"
    try:
        run = await service.draft_from_trigger(
            session,
            gate.client,
            definition,
            trigger,
            inputs=inputs_from(definition, arrival),
            ticket_number=arrival.ticket_number or arrival.ticket_id,
            detail=detail,
            now=now,
        )
    except service.Refused as refused:
        return made(
            matched=True,
            outcome="skipped",
            reason=f"{why}; skipped: {refused}",
            ticket=arrival,
        )
    return made(
        matched=True,
        outcome="drafted",
        reason=f"{why}; drafted run {str(run.id)[:8]}",
        run_id=run.id,
        ticket=arrival,
    )


# --- one poll --------------------------------------------------------------------------

ArrivalReader = Callable[[dt.datetime], Awaitable[list[Arrival]]]
"""What arrived since this moment, as arrivals. Built outside this package
(`app/tickets/poller.py`), so the engine itself never reads a vendor."""

POLL_ACTION = "poll"


@dataclass(frozen=True, slots=True)
class Polled:
    """What one poll did. Counts only, as ever."""

    since: dt.datetime
    tickets: int
    evaluations: int
    matched: int
    drafted: int
    noted: int = 0
    posted: int = 0
    reports: int = 0
    closures: int = 0


async def last_poll_at(session: AsyncSession, client_id: uuid.UUID) -> dt.datetime | None:
    """When this client was last polled — the moment that poll started, from
    the audit row it wrote — so the next poll reads from there."""
    from app.models.audit_log import AuditLog

    payload = (
        await session.execute(
            select(AuditLog.payload)
            .where(
                AuditLog.client_id == client_id,
                AuditLog.event_type == AuditEventType.AUTOMATION_CONTROL,
                AuditLog.payload["control"].astext == POLL_ACTION,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not payload or not payload.get("polled_at"):
        return None
    return dt.datetime.fromisoformat(payload["polled_at"])


async def poll_once(
    session: AsyncSession,
    client_id: uuid.UUID,
    read: ArrivalReader,
    *,
    since: dt.datetime,
    now: dt.datetime,
    tools: Any | None = None,
    writer_for: Any | None = None,
    closures: Any | None = None,
) -> Polled:
    """Read what arrived since `since`, evaluate every ticket and alert rule
    over it, and record the poll as an `automation_control` row so the next
    poll knows where to start. The caller owns the transaction and the
    budget: `read` is expected to spend from it and raise when it is out.

    With `tools` the note workflows run too (`app.automation.notes.service`):
    their alert rules over the same arrivals, the reports that are due, and
    — with `closures` — the measurement of when matched alert tickets closed.
    `writer_for` is what posts a note in auto mode; without it nothing posts.

    Running this twice over the same tickets drafts nothing twice: the
    one-draft-per-ticket rule is checked in `evaluate` and kept by a unique
    index, so a second poller — or the forward shadow, were it ever to
    evaluate again — cannot double a run. A note is one per workflow per
    ticket the same way.
    """
    arrivals = await read(since)
    found = await evaluate(session, client_id, arrivals, now=now)
    noted = posted = reports = closed = 0
    if tools is not None:
        from app.automation.notes import service as notes

        note_results = await notes.evaluate(
            session, client_id, arrivals, tools=tools, writer_for=writer_for, now=now
        )
        found.extend(note_results)
        noted = sum(1 for f in note_results if f.outcome == "noted")
        posted = sum(1 for f in note_results if f.outcome == "noted" and "; posted" in f.reason)
        reports = len(await notes.build_due_reports(session, client_id, tools=tools, now=now))
        if closures is not None:
            closed = await notes.measure_closures(session, client_id, closures, now=now)
    client = await session.get(Client, client_id)
    assert client is not None
    polled = Polled(
        since=since,
        tickets=len(arrivals),
        evaluations=len(found),
        matched=sum(1 for f in found if f.matched),
        drafted=sum(1 for f in found if f.outcome == "drafted"),
        noted=noted,
        posted=posted,
        reports=reports,
        closures=closed,
    )
    await record_event(
        session,
        system_context(client),
        event_type=AuditEventType.AUTOMATION_CONTROL,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "control": POLL_ACTION,
            "polled_at": now.isoformat(),
            "since": since.isoformat(),
            "tickets": polled.tickets,
            "evaluations": polled.evaluations,
            "matched": polled.matched,
            "drafted": polled.drafted,
            "noted": polled.noted,
            "posted": polled.posted,
            "reports": polled.reports,
            "closures": polled.closures,
        },
    )
    return polled


# --- schedules, stale drafts, expired approvals: the worker's housekeeping ------------


async def tick_schedules(session: AsyncSession, *, now: dt.datetime) -> list[Evaluated]:
    """Draft a run for every enabled schedule that is due. Only clients that
    turned a schedule on are looked at, and only a draft or a refusal is
    logged — a schedule that is simply not yet due is not news every tick."""
    rows = (
        (
            await session.execute(
                select(AutomationTriggerSwitch).where(AutomationTriggerSwitch.enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    found: list[Evaluated] = []
    gates: dict[uuid.UUID, _Gate] = {}
    for row in rows:
        try:
            definition = next(w for w in workflows() if w.name == row.workflow_name)
            trigger = definition.trigger(row.trigger_key)
        except (StopIteration, KeyError):
            continue
        if trigger.kind != "schedule":
            continue
        assert trigger.every is not None
        gate = gates.get(row.client_id)
        if gate is None:
            gate = gates[row.client_id] = await _Gate.read(session, row.client_id)
        last = (
            await session.execute(
                select(AutomationRun.created_at)
                .where(
                    AutomationRun.client_id == row.client_id,
                    AutomationRun.workflow_name == definition.name,
                    AutomationRun.trigger_key == trigger.key,
                )
                .order_by(AutomationRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last is not None and last + trigger.every > now:
            continue
        made = functools.partial(
            Evaluated, workflow=definition.name, trigger=trigger.key, kind="schedule"
        )
        skipped = gate.why_not(definition, trigger)
        if skipped is None:
            try:
                run = await service.draft_from_trigger(
                    session,
                    gate.client,
                    definition,
                    trigger,
                    inputs={},
                    ticket_number=None,
                    detail=f"schedule, {trigger.title}",
                    now=now,
                )
                result = made(
                    matched=True,
                    outcome="drafted",
                    reason=f"due; drafted run {str(run.id)[:8]}",
                    run_id=run.id,
                )
            except service.Refused as refused:
                result = made(matched=True, outcome="skipped", reason=f"due; skipped: {refused}")
        else:
            result = made(matched=True, outcome="skipped", reason=f"due; skipped: {skipped}")
        await _log(session, row.client_id, result, rule=None, now=now)
        found.append(result)
    return found


async def cancel_stale(session: AsyncSession, *, now: dt.datetime) -> list[uuid.UUID]:
    """Cancel drafts and runs awaiting approval older than `STALE_AFTER`, each
    with an audit row saying why."""
    stale = (
        (
            await session.execute(
                select(AutomationRun).where(
                    AutomationRun.state.in_(
                        [
                            RunState.DRAFTED.value,
                            RunState.DRY_RUN_READY.value,
                            RunState.AWAITING_APPROVAL.value,
                        ]
                    ),
                    AutomationRun.created_at < now - STALE_AFTER,
                )
            )
        )
        .scalars()
        .all()
    )
    cancelled: list[uuid.UUID] = []
    for run in stale:
        client = await session.get(Client, run.client_id)
        assert client is not None
        was = run.state
        advance(run, RunState.CANCELLED, now=now)
        run.failure = (
            f"cancelled by the worker: {was.replace('_', ' ')} for more than "
            f"{STALE_AFTER.days} days without an approval"
        )
        await service.run_event(
            session,
            system_context(client),
            run,
            "cancelled",
            reason=run.failure,
            was=was,
            stale_after_days=STALE_AFTER.days,
        )
        cancelled.append(run.id)
    return cancelled


async def expire_approvals(session: AsyncSession, *, now: dt.datetime) -> list[uuid.UUID]:
    """Put approved runs that did not start inside the window back to awaiting
    approval. The approval that lapsed goes on the audit row; the run's
    approval fields are cleared so the next approval is recorded fresh."""
    lapsed = (
        (
            await session.execute(
                select(AutomationRun).where(
                    AutomationRun.state == RunState.APPROVED.value,
                    AutomationRun.approval_expires_at.is_not(None),
                    AutomationRun.approval_expires_at < now,
                )
            )
        )
        .scalars()
        .all()
    )
    expired: list[uuid.UUID] = []
    for run in lapsed:
        client = await session.get(Client, run.client_id)
        assert client is not None
        previous = service.approval_record(run)
        advance(run, RunState.AWAITING_APPROVAL, now=now)
        service.clear_approval(run)
        await service.run_event(
            session,
            system_context(client),
            run,
            "approval_expired",
            outcome=AuditOutcome.ERROR,
            previous_approval=previous,
            reason="the run did not start inside the approval window",
        )
        expired.append(run.id)
    return expired


async def housekeeping(session: AsyncSession, *, now: dt.datetime) -> dict[str, int]:
    """Everything the worker does between pieces of work. The caller commits."""
    return {
        "cancelled_stale": len(await cancel_stale(session, now=now)),
        "expired_approvals": len(await expire_approvals(session, now=now)),
        "scheduled": len(
            [e for e in await tick_schedules(session, now=now) if e.outcome == "drafted"]
        ),
    }

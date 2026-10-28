"""The request side of a run: drafting, the dry run, submitting, approving,
withdrawing, cancelling, resuming, abandoning and asking for an undo.

Everything here is a short transaction a page or a script makes. Nothing here
runs a step: the worker (`runner.py`) does that, in the background, one run at
a time per client. The division is the point — a web request that executed an
action against a client's tools would be an action whose outcome depended on
somebody's browser staying open.

Every function takes the session and an `Actor`, writes its audit row through
`record_event` in the caller's transaction, and refuses with `Refused` — a
sentence somebody can act on — rather than letting a bad request through to
the worker.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.automation import controls
from app.automation.config import load_automation_config
from app.automation.definitions import Facts, StepContext, Trigger, Workflow, workflow
from app.automation.states import UNDOABLE, InvalidTransition, RunState, advance
from app.models.automation import AutomationRun, AutomationStep
from app.models.client import Client
from app.models.enums import AuditEventType, AuditOutcome, Interface

TRIGGER_ACTOR = "automation-trigger"


class Refused(ValueError):
    """The request cannot be carried out, and this is why, in words."""


class NotFound(Refused):
    """No such run for this client. Another client's run is this too: whether
    it exists is not something the caller is told."""


#: A clock a minute ahead of ours is not a time in the future.
APPROVAL_TIME_SKEW = dt.timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Actor:
    """The signed-in person making the request."""

    user_id: uuid.UUID
    email: str
    client_id: uuid.UUID
    client_slug: str

    def context(self) -> AuditContext:
        return AuditContext.new(
            actor_id=str(self.user_id),
            actor_user_id=self.user_id,
            interface=Interface.API,
            client_id=self.client_id,
            client_ref=self.client_slug,
        )

    def who(self) -> controls.Who:
        return controls.Who(user_id=self.user_id, label=self.email)


async def run_event(
    session: AsyncSession,
    context: AuditContext,
    run: AutomationRun,
    action: str,
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    **extra: Any,
) -> None:
    """One `automation_run` audit row: what was done to the run, by the
    context's actor, with the run's state as it now is."""
    await record_event(
        session,
        context,
        event_type=AuditEventType.AUTOMATION_RUN,
        outcome=outcome,
        payload={
            "action": action,
            "run_id": str(run.id),
            "workflow": run.workflow_name,
            "version": run.workflow_version,
            "state": run.state,
            **extra,
        },
    )


async def get_run(session: AsyncSession, client_id: uuid.UUID, run_id: uuid.UUID) -> AutomationRun:
    """A run, looked up together with its client. Another client's id is not found."""
    run = (
        await session.execute(
            select(AutomationRun).where(
                AutomationRun.id == run_id, AutomationRun.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise NotFound("There is no such run for this client.")
    return run


async def steps_of(session: AsyncSession, run: AutomationRun) -> list[AutomationStep]:
    return list(
        (
            await session.execute(
                select(AutomationStep)
                .where(AutomationStep.run_id == run.id)
                .order_by(AutomationStep.position)
            )
        )
        .scalars()
        .all()
    )


def approval_record(run: AutomationRun) -> dict[str, Any] | None:
    """The approval as it stands on the run, for an audit row."""
    if run.authoriser_name is None:
        return None
    return {
        "authoriser": run.authoriser_name,
        "method": run.authoriser_method,
        "at": run.authoriser_at.isoformat() if run.authoriser_at else None,
        "ref": run.authoriser_ref,
        "approved_by": run.approved_by,
        "approved_at": run.approved_at.isoformat() if run.approved_at else None,
        "expires_at": run.approval_expires_at.isoformat() if run.approval_expires_at else None,
    }


def clear_approval(run: AutomationRun) -> None:
    """Take the approval off the run, once it is on the audit row. The next
    one is recorded fresh rather than over the top of it."""
    run.authoriser_name = None
    run.authoriser_method = None
    run.authoriser_at = None
    run.authoriser_ref = None
    run.approved_by_user_id = None
    run.approved_by = None
    run.approved_at = None
    run.approval_expires_at = None


# --- drafting -------------------------------------------------------------------


async def _new_run(
    session: AsyncSession,
    *,
    client: Client,
    definition: Workflow,
    inputs: dict[str, Any],
    ticket_number: str | None,
    now: dt.datetime,
    **starter: Any,
) -> AutomationRun:
    """The run and its step rows, in `drafted`, however it was asked for.
    `starter` is the who: a user id and address, or a trigger and its words."""
    reason = definition.allowed_on(client.kind)
    if reason:
        raise Refused(reason)
    if not await controls.is_enabled(session, client.id, definition.name):
        raise Refused(
            f"The {definition.title} workflow is switched off for {client.name}. "
            f"Turn it on from the Automations page first."
        )
    if await controls.is_halted(session, client.id):
        raise Refused(
            f"Automation is halted for {client.name}. Clear the kill switch before starting a run."
        )
    try:
        validated = definition.inputs.model_validate(inputs)
    except ValidationError as exc:
        raise Refused(f"The inputs were not accepted: {exc.errors()[0]['msg']}") from None

    run = AutomationRun(
        client_id=client.id,
        workflow_name=definition.name,
        workflow_version=definition.version,
        definition_hash=definition.definition_hash or None,
        state=RunState.DRAFTED.value,
        inputs=validated.model_dump(mode="json"),
        ticket_number=ticket_number,
        created_at=now,
        updated_at=now,
        **starter,
    )
    session.add(run)
    try:
        # A savepoint, so the one-draft-per-ticket index refusing leaves the
        # caller's transaction usable.
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise Refused(
            f"a run of {definition.name} already exists for ticket {ticket_number}"
        ) from None
    for position, step in enumerate(definition.steps, start=1):
        session.add(
            AutomationStep(
                run_id=run.id,
                step_key=step.key,
                position=position,
                title=step.title,
                depends_on=step.depends_on,
                reversible=step.reversible,
                state="pending",
                attempts=0,
            )
        )
    await session.flush()
    return run


async def draft(
    session: AsyncSession,
    actor: Actor,
    *,
    workflow_name: str,
    inputs: dict[str, Any],
    ticket_number: str | None = None,
    now: dt.datetime,
) -> AutomationRun:
    """Create a run in `drafted` for a person: the workflow at its current
    version, the inputs validated against its schema, one pending step row
    per step.

    Refused when the workflow has no manual trigger, is not switched on for
    this client, may not run on this kind of client, when the client's kill
    switch is thrown, or when the inputs do not validate.
    """
    try:
        definition = workflow(workflow_name)
    except KeyError as exc:
        raise Refused(str(exc)) from None
    if not definition.manual:
        starts = ", ".join(t.kind for t in definition.triggers) or "nothing"
        raise Refused(
            f"The {definition.title} workflow is not started by hand; it has no manual "
            f"trigger. It starts from: {starts}."
        )
    client = await session.get(Client, actor.client_id)
    if client is None:
        raise Refused("There is no such client.")
    run = await _new_run(
        session,
        client=client,
        definition=definition,
        inputs=inputs,
        ticket_number=ticket_number,
        now=now,
        started_by_user_id=actor.user_id,
        started_by=actor.email,
    )
    await run_event(session, actor.context(), run, "drafted", inputs=run.inputs)
    return run


async def draft_from_trigger(
    session: AsyncSession,
    client: Client,
    definition: Workflow,
    trigger: Trigger,
    *,
    inputs: dict[str, Any],
    ticket_number: str | None,
    detail: str,
    now: dt.datetime,
) -> AutomationRun:
    """Create a run in `drafted` because a trigger said so. The same checks as
    a person's draft, the same step rows, and an audit row by the system
    naming the trigger and the ticket; nothing beyond `drafted`."""
    words = {"ticket": "ticket rule", "alert": "alert rule", "schedule": "schedule"}
    run = await _new_run(
        session,
        client=client,
        definition=definition,
        inputs=inputs,
        ticket_number=ticket_number,
        now=now,
        started_by=words.get(trigger.kind, trigger.kind),
        trigger_key=trigger.key,
        trigger_detail=detail,
    )
    context = AuditContext.new(
        actor_id=TRIGGER_ACTOR,
        interface=Interface.SYSTEM,
        client_id=client.id,
        client_ref=client.slug,
    )
    await run_event(
        session,
        context,
        run,
        "drafted",
        inputs=run.inputs,
        trigger=trigger.key,
        trigger_kind=trigger.kind,
        ticket_number=ticket_number,
        detail=detail,
    )
    return run


# --- the dry run ----------------------------------------------------------------


async def _context_for(
    session: AsyncSession, run: AutomationRun, step: AutomationStep, *, before: Facts | None = None
) -> StepContext:
    config = await load_automation_config(session, run.client_id)
    return StepContext(
        session=session,
        run_id=run.id,
        client_id=run.client_id,
        step_key=step.step_key,
        inputs=run.inputs,
        settings=config.settings_for(run.workflow_name),
        attempt=step.attempts + 1,
        before=before,
        dry_run=True,
    )


async def dry_run(
    session: AsyncSession, actor: Actor, run: AutomationRun, *, now: dt.datetime
) -> dict[str, Any]:
    """Run every precondition and every plan, change nothing, and put the
    checklist on the run.

    Every precondition passing moves the run to `dry_run_ready`. One failing
    leaves it `drafted` with the failure on the step and in the checklist, so
    it can be fixed and run again. Either way the checklist says, per step,
    what would change and whether it could be undone — the not-reversible
    steps are the thing an approver most needs to have read.
    """
    if run.state not in (RunState.DRAFTED.value, RunState.DRY_RUN_READY.value):
        raise Refused(f"A dry run is for a drafted run; this one is {run.state}.")
    definition = workflow(run.workflow_name)
    _check_version(run, definition)
    context = actor.context()
    checklist: list[dict[str, Any]] = []
    all_ok = True
    for row in await steps_of(session, run):
        step = definition.step(row.step_key)
        ctx = await _context_for(session, run, row)
        check = await step.precondition(ctx)
        row.precondition = {"ok": check.ok, "facts": check.facts, "reason": check.reason}
        entry: dict[str, Any] = {
            "step": row.step_key,
            "title": row.title,
            "ok": check.ok,
            "reason": check.reason,
            "reversible": row.reversible,
            "depends_on": row.depends_on,
        }
        if check.ok:
            plan = await step.plan(ctx)
            row.planned = plan.as_dict()
            entry["plan"] = row.planned
        else:
            all_ok = False
            row.planned = None
        checklist.append(entry)
        await record_event(
            session,
            context,
            event_type=AuditEventType.AUTOMATION_STEP,
            outcome=AuditOutcome.SUCCESS if check.ok else AuditOutcome.ERROR,
            payload={
                "action": "dry_run",
                "run_id": str(run.id),
                "step": row.step_key,
                "precondition": row.precondition,
                "plan": row.planned,
            },
        )
    run.dry_run = {
        "ok": all_ok,
        "steps": checklist,
        "not_reversible": list(definition.not_reversible),
        "undo_window_hours": definition.undo_window.total_seconds() / 3600,
    }
    run.dry_run_at = now
    if all_ok and run.state == RunState.DRAFTED.value:
        advance(run, RunState.DRY_RUN_READY, now=now)
    elif not all_ok and run.state == RunState.DRY_RUN_READY.value:
        # The world moved since the last dry run: back to drafted, and the
        # checklist says which step would no longer go.
        advance(run, RunState.DRAFTED, now=now)
    await run_event(
        session,
        context,
        run,
        "dry_run",
        outcome=AuditOutcome.SUCCESS if all_ok else AuditOutcome.ERROR,
        ok=all_ok,
    )
    return run.dry_run


def _check_version(run: AutomationRun, definition: Workflow) -> None:
    if run.workflow_version != definition.version:
        raise Refused(
            f"This run was drafted against {run.workflow_name} version {run.workflow_version} "
            f"and the current definition is version {definition.version}. Cancel it and draft "
            f"a new one: a run is executed against the steps it was approved with, never "
            f"against steps that changed underneath it."
        )


# --- submitting, approving, withdrawing, cancelling ------------------------------


async def submit(
    session: AsyncSession, actor: Actor, run: AutomationRun, *, now: dt.datetime
) -> None:
    """Put the checklist in front of the client: `dry_run_ready` → `awaiting_approval`."""
    _advance_or_refuse(run, RunState.AWAITING_APPROVAL, now=now)
    await run_event(session, actor.context(), run, "submitted")


async def withdraw(
    session: AsyncSession, actor: Actor, run: AutomationRun, *, now: dt.datetime
) -> None:
    """Take it back to `drafted`, from `dry_run_ready` or `awaiting_approval`."""
    _advance_or_refuse(run, RunState.DRAFTED, now=now)
    await run_event(session, actor.context(), run, "withdrawn")


async def approve(
    session: AsyncSession,
    actor: Actor,
    run: AutomationRun,
    *,
    authoriser_name: str,
    authoriser_method: str,
    authoriser_at: dt.datetime | None,
    authoriser_ref: str | None = None,
    now: dt.datetime,
) -> None:
    """Record the client's yes and the engineer's countersignature.

    Three things are refused here and nowhere later: an engineer approving a
    run they started themselves; an authoriser who is not on the client's
    approver list; a workflow that has been switched off since the run was
    drafted. The authoriser is the client's person — name, how they said it,
    when — and the approving engineer is the account recording it; both go on
    the run and in the audit row.
    """
    if run.state != RunState.AWAITING_APPROVAL.value:
        raise Refused(f"Only a run awaiting approval can be approved; this one is {run.state}.")
    if run.started_by_user_id == actor.user_id:
        raise Refused(
            "You started this run, so somebody else has to approve it. Two people, every time."
        )
    config = await load_automation_config(session, actor.client_id)
    approver = config.approver(authoriser_name)
    if approver is None:
        names = ", ".join(a.name for a in config.approvers) or "nobody"
        raise Refused(
            f"{authoriser_name!r} is not on this client's approver list ({names}). "
            f"An approver is added in the client's settings, not here."
        )
    if not await controls.is_enabled(session, actor.client_id, run.workflow_name):
        raise Refused(
            f"The {run.workflow_name} workflow has been switched off for this client since "
            f"this run was drafted. Turn it back on, or cancel the run."
        )
    method = " ".join(authoriser_method.split()).lower()
    if not method:
        raise Refused("Say how the authoriser approved it: phone, email, teams, in person.")
    # The time the client said yes is theirs; it defaults to now and may be
    # earlier, but never later than now and never before the dry run, which
    # is what they said yes to.
    if authoriser_at is not None:
        if authoriser_at.tzinfo is None:
            authoriser_at = authoriser_at.replace(tzinfo=dt.UTC)
        if authoriser_at > now + APPROVAL_TIME_SKEW:
            raise Refused("The approval time is in the future.")
        if run.dry_run_at is not None and authoriser_at < run.dry_run_at:
            raise Refused("The approval time is before the dry run it approves.")
    run.authoriser_name = approver.name
    run.authoriser_method = method
    run.authoriser_at = authoriser_at or now
    run.authoriser_ref = authoriser_ref
    run.approved_by_user_id = actor.user_id
    run.approved_by = actor.email
    run.approved_at = now
    run.approval_expires_at = now + workflow(run.workflow_name).approval_window
    _advance_or_refuse(run, RunState.APPROVED, now=now)
    await run_event(
        session,
        actor.context(),
        run,
        "approved",
        authoriser={
            "name": run.authoriser_name,
            "role": approver.role,
            "method": run.authoriser_method,
            "at": run.authoriser_at.isoformat() if run.authoriser_at else None,
            "ref": run.authoriser_ref,
        },
        approved_at=run.authoriser_at.isoformat() if run.authoriser_at else None,
        recorded_at=now.isoformat(),
        approved_by=actor.email,
        started_by=run.started_by,
        expires_at=run.approval_expires_at.isoformat() if run.approval_expires_at else None,
    )


async def cancel(
    session: AsyncSession, actor: Actor, run: AutomationRun, *, reason: str | None, now: dt.datetime
) -> None:
    """Stop a run that has not started. Terminal."""
    _advance_or_refuse(run, RunState.CANCELLED, now=now)
    run.failure = reason
    await run_event(session, actor.context(), run, "cancelled", reason=reason)


async def resume(
    session: AsyncSession, actor: Actor, run: AutomationRun, *, now: dt.datetime
) -> None:
    """Hand a failed run back to the worker, from the step that failed.

    The failed step goes back to `pending` with its attempt count kept, so the
    step can tell it is being tried again. Completed steps are not touched and
    will not run again: the worker takes the first step that is not `done`.
    """
    if run.state != RunState.FAILED.value:
        raise Refused(f"Only a failed run can be resumed; this one is {run.state}.")
    if await controls.is_halted(session, actor.client_id):
        raise Refused("Automation is halted for this client. Clear the kill switch first.")
    deadline = resume_deadline(run)
    if deadline is not None and now > deadline:
        hours = workflow(run.workflow_name).approval_window.total_seconds() / 3600
        raise Refused(
            f"This run stopped more than {hours:g} hours ago, so its approval no longer "
            f"covers a resume. Ask for a fresh approval: the run goes back to awaiting "
            f"approval and is approved the same way again."
        )
    for row in await steps_of(session, run):
        if row.state == "failed":
            row.state = "pending"
            row.error = None
    _advance_or_refuse(run, RunState.RUNNING, now=now)
    # The worker claims it by the lease; a resumed run is handed over
    # unleased so any worker may take it up.
    run.lease_until = None
    await run_event(session, actor.context(), run, "resumed", from_step=run.current_step)


def resume_deadline(run: AutomationRun) -> dt.datetime | None:
    """Until when a failed run may be resumed on the approval it has: the
    approval window counted from when it stopped."""
    if run.finished_at is None:
        return None
    return run.finished_at + workflow(run.workflow_name).approval_window


async def resubmit(
    session: AsyncSession, actor: Actor, run: AutomationRun, *, now: dt.datetime
) -> None:
    """Ask for a fresh approval of a failed run: `failed` → `awaiting_approval`.

    The failed step goes back to pending as a resume would put it; the
    approval that covered the run goes on the audit row and comes off the
    run, so the next one is recorded fresh. Approved again, the worker takes
    it from the failed step and re-checks before acting.
    """
    if run.state != RunState.FAILED.value:
        raise Refused(
            f"Only a failed run can be sent for a fresh approval; this one is {run.state}."
        )
    previous = approval_record(run)
    for row in await steps_of(session, run):
        if row.state == "failed":
            row.state = "pending"
            row.error = None
    _advance_or_refuse(run, RunState.AWAITING_APPROVAL, now=now)
    clear_approval(run)
    run.lease_until = None
    await run_event(
        session,
        actor.context(),
        run,
        "resubmitted",
        from_step=run.current_step,
        previous_approval=previous,
    )


async def abandon(
    session: AsyncSession, actor: Actor, run: AutomationRun, *, reason: str | None, now: dt.datetime
) -> None:
    """Give up on a failed run, keeping what it did: `failed` → `partly_completed`.
    Undo stays open for the completed steps until the deadline."""
    _advance_or_refuse(run, RunState.PARTLY_COMPLETED, now=now)
    run.failure = reason or run.failure
    await run_event(session, actor.context(), run, "abandoned", reason=reason)


async def request_undo(
    session: AsyncSession, actor: Actor, run: AutomationRun, *, now: dt.datetime
) -> None:
    """Ask the worker to undo what this run did, in reverse order.

    Refused past the workflow's undo window, and for a run in a state with
    nothing to undo. Steps with no undo are left as they are and the result
    says so — the dry run said so before approval, which is when it mattered.
    """
    if RunState(run.state) not in UNDOABLE:
        raise Refused(f"A {run.state} run has nothing to undo.")
    if run.undo_deadline_at is not None and now > run.undo_deadline_at:
        raise Refused(
            f"The undo window closed at {run.undo_deadline_at:%d %b %Y %H:%M} UTC. What this "
            f"run changed has stood since then and is put back by hand, if at all."
        )
    if run.undo_requested_at is not None:
        raise Refused("An undo is already requested for this run.")
    if await controls.is_halted(session, actor.client_id):
        raise Refused("Automation is halted for this client. Clear the kill switch first.")
    run.undo_requested_at = now
    run.undo_requested_by = actor.email
    run.undo_failure = None
    await run_event(session, actor.context(), run, "undo_requested")


def _advance_or_refuse(run: AutomationRun, wanted: RunState, *, now: dt.datetime) -> None:
    try:
        advance(run, wanted, now=now)
    except InvalidTransition as exc:
        raise Refused(str(exc)) from None

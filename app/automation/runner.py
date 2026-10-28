"""The worker: executes approved runs, step by step, in the background.

Nothing in a web request runs a step. A process started by
`scripts/automation_worker.py` calls `run_once` on a loop; each call takes one
piece of work for one client and does it until it stops — the run completes,
a step fails, or the client's kill switch holds it — and returns.

Three properties, and how each is kept:

* **One run at a time per client.** A run enters `running` through a partial
  unique index on `(client_id) WHERE state = 'running'`, so a second worker
  claiming the same client's next run gets an integrity error and moves on.
  Nobody has to remember who is running what.
* **Each step is its own transaction, and a failed step leaves nothing of
  itself.** The step row is marked `running` and committed first, so a worker
  that dies mid-action leaves a row that says so. Then precondition, before,
  action and verify run in one session: if any raises or fails, that session
  is rolled back — the action's own writes with it — and the failure is
  recorded on a connection of its own, the way every failed vendor read is.
  A step whose verify passes is `done`, committed with its before and after.
* **It survives a restart.** A running run whose lease has lapsed was left by
  a worker that died; `recover` puts its `running` step back to `pending`
  (attempts kept) and the next `run_once` takes it up from that step. The
  action runs again, which is why the step contract says an action must be
  idempotent for one (run, step): the second run must leave the world as the
  first did. Completed steps are never repeated — the worker takes the first
  step that is not `done`.

The kill switch is read before every step. Thrown, the run stays `running`
with `paused_at` set and the worker lets go of it; cleared, the next
`run_once` carries on. Half a step is worse than a whole one, so the step in
flight finishes.

An undo is the same loop backwards: the last `done` step that has an undo,
each in its own transaction, until none is left; then the run is `undone`.
Steps with no undo are left as they are and listed, as the dry run listed
them before anybody approved. **An undo looks before it puts anything back**:
the step's verify is run again and its facts compared with the after-state
recorded when the step was done. Anything different means somebody changed it
since the run — a licence re-added by hand, a status moved on — and the step
is marked `changed` and left alone, because an undo must never overwrite a
newer change. The others are still undone.

A step being tried again — after a failure, after a worker died — re-checks
before it acts: its own precondition (which runs before every action anyway)
and the verify of the step it depends on. A dependency that no longer
verifies stops the run with the reason rather than acting on a situation
that has changed since the run stopped.

Between pieces of work the worker does the engine's housekeeping
(`triggers.housekeeping`): drafts due scheduled runs, cancels drafts nobody
approved in a week, and puts runs whose approval lapsed before they started
back to awaiting approval.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.recorder import AuditContext, record_event
from app.automation import controls, triggers
from app.automation.config import load_automation_config
from app.automation.definitions import Facts, Step, StepContext, Workflow, workflow
from app.automation.service import run_event
from app.automation.states import UNDOABLE, RunState, advance
from app.models.automation import AutomationRun, AutomationStep
from app.models.client import Client
from app.models.enums import AuditEventType, AuditOutcome, Interface

logger = logging.getLogger(__name__)

WORKER = "automation-worker"
#: How long a worker's claim on a run lasts without being renewed. Renewed
#: after every step; a run whose lease has lapsed is one whose worker died.
LEASE = dt.timedelta(minutes=5)

Maker = async_sessionmaker[AsyncSession]
Clock = Callable[[], dt.datetime]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class StepFailed(Exception):
    """A step's precondition or verify did not pass, or its action raised."""

    def __init__(self, phase: str, reason: str) -> None:
        super().__init__(f"{phase}: {reason}")
        self.phase = phase
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one call to `run_once` did."""

    run_id: uuid.UUID | None
    result: str
    """`idle`, `stepped`, `completed`, `failed`, `paused`, `undone`,
    `undo_failed`, `undo_refused`."""


async def _client_context(session: AsyncSession, client_id: uuid.UUID) -> AuditContext:
    slug = (await session.execute(select(Client.slug).where(Client.id == client_id))).scalar_one()
    return AuditContext.new(
        actor_id=WORKER,
        interface=Interface.SYSTEM,
        client_id=client_id,
        client_ref=slug,
    )


async def _step_event(
    session: AsyncSession,
    context: AuditContext,
    run: AutomationRun,
    row: AutomationStep,
    action: str,
    *,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    **extra: Any,
) -> None:
    await record_event(
        session,
        context,
        event_type=AuditEventType.AUTOMATION_STEP,
        outcome=outcome,
        payload={
            "action": action,
            "run_id": str(run.id),
            "workflow": run.workflow_name,
            "step": row.step_key,
            "attempt": row.attempts,
            **extra,
        },
    )


# --- recovery and claiming ----------------------------------------------------------


async def recover(session: AsyncSession, *, now: dt.datetime) -> list[uuid.UUID]:
    """Find the runs a dead worker left, and put their in-flight step back.

    A run is orphaned when it is `running` and its lease has lapsed. Its step
    marked `running` goes back to `pending` with its attempts kept — the
    attempt count is how a step knows it is being run again — and the run is
    left unleased for the next worker. Returns the runs touched.
    """
    orphans = list(
        (
            await session.execute(
                select(AutomationRun).where(
                    AutomationRun.state == RunState.RUNNING.value,
                    AutomationRun.lease_until.is_not(None),
                    AutomationRun.lease_until < now,
                )
            )
        )
        .scalars()
        .all()
    )
    for run in orphans:
        await session.execute(
            update(AutomationStep)
            .where(AutomationStep.run_id == run.id, AutomationStep.state == "running")
            .values(state="pending", error="the worker stopped during this step")
        )
        run.lease_until = None
        context = await _client_context(session, run.client_id)
        await run_event(session, context, run, "recovered", from_step=run.current_step)
    return [run.id for run in orphans]


async def _claim(session: AsyncSession, run: AutomationRun, *, now: dt.datetime) -> bool:
    """Move an approved run to `running` and lease it. False if the client
    already has one running: the partial unique index refuses, and that is the
    one-at-a-time rule being kept by the database rather than by us."""
    context = await _client_context(session, run.client_id)
    advance(run, RunState.RUNNING, now=now)
    run.lease_until = now + LEASE
    run.current_step = None
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return False
    await run_event(session, context, run, "started")
    return True


async def _next_work(
    session: AsyncSession, *, now: dt.datetime
) -> tuple[str, AutomationRun] | None:
    """The one thing to do next, in this order: carry on a running run that
    nobody holds; do an undo somebody asked for; start an approved run. A
    client with a running run gets nothing else started."""
    busy = {
        row
        for row in (
            await session.execute(
                select(AutomationRun.client_id).where(AutomationRun.state == RunState.RUNNING.value)
            )
        ).scalars()
    }
    unheld = (
        (
            await session.execute(
                select(AutomationRun)
                .where(
                    AutomationRun.state == RunState.RUNNING.value,
                    AutomationRun.lease_until.is_(None),
                )
                .order_by(AutomationRun.updated_at)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if unheld is not None:
        return "execute", unheld
    # A client with a run in flight gets nothing else started on it.
    free = [AutomationRun.client_id.not_in(busy)] if busy else []
    undo = (
        (
            await session.execute(
                select(AutomationRun)
                .where(
                    AutomationRun.undo_requested_at.is_not(None),
                    AutomationRun.state.in_([s.value for s in UNDOABLE]),
                    *free,
                )
                .order_by(AutomationRun.undo_requested_at)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if undo is not None:
        return "undo", undo
    approved = (
        (
            await session.execute(
                select(AutomationRun)
                .where(
                    AutomationRun.state == RunState.APPROVED.value,
                    # An approval that has lapsed is not started on; the
                    # housekeeping puts such a run back to awaiting approval.
                    (AutomationRun.approval_expires_at.is_(None))
                    | (AutomationRun.approval_expires_at >= now),
                    *free,
                )
                .order_by(AutomationRun.approved_at)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if approved is not None:
        return "start", approved
    return None


async def run_once(maker: Maker, *, clock: Clock = _utcnow) -> Outcome:
    """Do one piece of work and return. The worker script calls this on a loop;
    a test calls it as many times as it wants to watch."""
    now = clock()
    async with maker() as session:
        await recover(session, now=now)
        await session.commit()
    async with maker() as session:
        try:
            await triggers.housekeeping(session, now=now)
            await session.commit()
        except Exception:
            # Housekeeping may not stop the work: a schedule that cannot draft
            # is logged and the next pass tries again.
            logger.exception("automation housekeeping failed")
            await session.rollback()

    async with maker() as session:
        found = await _next_work(session, now=now)
        if found is None:
            return Outcome(None, "idle")
        kind, run = found
        run_id = run.id
        if kind == "start":
            if not await _claim(session, run, now=now):
                return Outcome(run_id, "idle")
        elif kind == "execute":
            run.lease_until = now + LEASE
        await session.commit()

    if kind == "undo":
        return Outcome(run_id, await _undo(maker, run_id, clock=clock))
    return Outcome(run_id, await _execute(maker, run_id, clock=clock))


# --- executing ---------------------------------------------------------------------


async def _execute(maker: Maker, run_id: uuid.UUID, *, clock: Clock) -> str:
    """Run steps until the run completes, a step fails, or the kill switch holds it."""
    while True:
        result = await _one_step(maker, run_id, clock=clock)
        if result != "stepped":
            return result


async def _one_step(maker: Maker, run_id: uuid.UUID, *, clock: Clock) -> str:
    now = clock()
    # First: decide, mark the step running, and commit — so a death during the
    # action leaves a row that says which step was in flight.
    async with maker() as session:
        run = await session.get(AutomationRun, run_id, with_for_update=True)
        if run is None or run.state != RunState.RUNNING.value:
            return "stopped"
        context = await _client_context(session, run.client_id)
        if await controls.is_halted(session, run.client_id):
            if run.paused_at is None:
                run.paused_at = now
                await run_event(session, context, run, "paused", reason="kill switch")
            run.lease_until = None
            await session.commit()
            return "paused"
        if run.paused_at is not None:
            run.paused_at = None
            await run_event(session, context, run, "unpaused")
        rows = await _rows(session, run.id)
        # The next step is the first that can be run: pending, or failed and
        # being tried again. Done steps are never repeated. A step in any
        # other state — undone after a partial undo, undo_failed — is not
        # runnable and not done, and a run that reaches one stops rather than
        # stepping over it.
        row = next((r for r in rows if r.state in ("pending", "failed")), None)
        if row is None:
            stuck = next((r for r in rows if r.state != "done"), None)
            if stuck is None:
                return await _finish(session, context, run, now=now)
            # A refusal is recorded by `_fail` in a session of its own, which
            # takes the row lock this session holds: so decide here, let go,
            # and record after the `with`.
            refusal = (
                stuck.id,
                "order",
                f"{stuck.step_key} is {stuck.state}, which is neither done nor runnable",
            )
        else:
            refusal = None
            definition = workflow(run.workflow_name)
            if definition.version != run.workflow_version:
                refusal = (
                    row.id,
                    "version",
                    f"drafted against version {run.workflow_version}, the code is version "
                    f"{definition.version}; cancel and draft again",
                )
            elif row.depends_on is not None:
                dependency = next(r for r in rows if r.step_key == row.depends_on)
                if dependency.state != "done":
                    refusal = (
                        row.id,
                        "dependency",
                        f"{row.step_key} depends on {row.depends_on}, which is "
                        f"{dependency.state}, not done",
                    )
        if refusal is not None:
            await session.rollback()
        else:
            assert row is not None
            row.state = "running"
            row.attempts += 1
            row.started_at = now
            row.error = None
            run.current_step = row.step_key
            run.lease_until = now + LEASE
            run.updated_at = now
            step_id, step_key = row.id, row.step_key
            await session.commit()

    if refusal is not None:
        refused_step, phase, reason = refusal
        return await _fail(maker, run_id, refused_step, phase, reason, phases=[], clock=clock)

    # Then: the step itself, in one transaction that either all lands or none does.
    phases: list[dict[str, Any]] = []
    async with maker() as session:
        run = await session.get(AutomationRun, run_id)
        row = await session.get(AutomationStep, step_id)
        assert run is not None and row is not None
        context = await _client_context(session, run.client_id)
        definition = workflow(run.workflow_name)
        step = definition.step(step_key)
        try:
            if row.attempts > 1:
                # Being tried again: the step this one depends on was done
                # before the run stopped, and the world may have moved. It is
                # verified again before anything acts.
                rechecked, problem = await _recheck_dependencies(session, run, row, definition)
                phases.append({"phase": "recheck", "steps": rechecked})
                await _step_event(
                    session,
                    context,
                    run,
                    row,
                    "recheck",
                    outcome=AuditOutcome.SUCCESS if problem is None else AuditOutcome.ERROR,
                    steps=rechecked,
                )
                if problem is not None:
                    raise StepFailed("recheck", problem)
            ctx = await _context_for(session, run, row)
            check = await step.precondition(ctx)
            row.precondition = {"ok": check.ok, "facts": check.facts, "reason": check.reason}
            phases.append({"phase": "precondition", **row.precondition})
            await _step_event(
                session, context, run, row, "precondition", precondition=row.precondition
            )
            if not check.ok:
                raise StepFailed("precondition", check.reason or "did not pass")

            before: Facts | None = None
            if step.before is not None:
                before = await step.before(ctx)
                row.before = before
                phases.append({"phase": "before", "facts": before})
            ctx = replace(ctx, before=before)

            result = await step.action(ctx)
            phases.append({"phase": "action", "result": result})
            await _step_event(session, context, run, row, "action", before=before, result=result)

            verified = await step.verify(ctx)
            row.verification = {
                "ok": verified.ok,
                "facts": verified.facts,
                "reason": verified.reason,
            }
            row.after = verified.facts
            phases.append({"phase": "verify", **row.verification})
            await _step_event(
                session,
                context,
                run,
                row,
                "verify",
                outcome=AuditOutcome.SUCCESS if verified.ok else AuditOutcome.ERROR,
                before=before,
                after=row.after,
                verification=row.verification,
            )
            if not verified.ok:
                raise StepFailed("verify", verified.reason or "did not pass")

            row.state = "done"
            row.finished_at = clock()
            row.error = None
            run.lease_until = clock() + LEASE
            run.updated_at = clock()
            await session.commit()
            return "stepped"
        except StepFailed as failed:
            await session.rollback()
            return await _fail(
                maker, run_id, step_id, failed.phase, failed.reason, phases=phases, clock=clock
            )
        except Exception as exc:
            await session.rollback()
            logger.exception("automation step %s of run %s raised", step_key, run_id)
            return await _fail(
                maker,
                run_id,
                step_id,
                "action",
                f"{type(exc).__name__}: {exc}",
                phases=phases,
                clock=clock,
            )


async def _recheck_dependencies(
    session: AsyncSession, run: AutomationRun, row: AutomationStep, definition: Workflow
) -> tuple[list[dict[str, Any]], str | None]:
    """Verify, again, the step this one depends on: what it found, and the
    reason it no longer holds when it does not.

    Only the declared dependency, not the chain behind it: a step further
    back may since have been changed on purpose by a step in between (the
    status a step set to offboarded is closed once the next step ran), and
    its verify would fail for the right reason. The dependency a step
    declares is the state it needs to be true when it acts.
    """
    if row.depends_on is None:
        return [], None
    rows = {r.step_key: r for r in await _rows(session, run.id)}
    dependency = rows[row.depends_on]
    ctx = await _context_for(session, run, dependency, before=dependency.before)
    found = await definition.step(row.depends_on).verify(ctx)
    rechecked = [
        {"step": row.depends_on, "ok": found.ok, "facts": found.facts, "reason": found.reason}
    ]
    if found.ok:
        return rechecked, None
    return rechecked, (
        f"{row.depends_on}, which {row.step_key} depends on, no longer verifies: "
        f"{found.reason or 'did not pass'}. The situation changed since the run "
        f"stopped; look before resuming again."
    )


async def _finish(
    session: AsyncSession, context: AuditContext, run: AutomationRun, *, now: dt.datetime
) -> str:
    definition = workflow(run.workflow_name)
    advance(run, RunState.COMPLETED, now=now)
    run.current_step = None
    run.lease_until = None
    run.undo_deadline_at = now + definition.undo_window
    await run_event(session, context, run, "completed", undo_until=run.undo_deadline_at.isoformat())
    await session.commit()
    return "completed"


async def _fail(
    maker: Maker,
    run_id: uuid.UUID,
    step_id: int,
    phase: str,
    reason: str,
    *,
    phases: list[dict[str, Any]],
    clock: Clock,
) -> str:
    """Record a failed step and a failed run, on a connection of their own.

    The step's transaction has been rolled back by now — its action's writes
    with it — so the phases that did run are written here, in this payload,
    rather than lost with it. A failure is logged on its own connection for
    the reason every failed vendor read is: the thing that failed is usually
    the last thing in its caller's transaction.
    """
    now = clock()
    async with maker() as session:
        run = await session.get(AutomationRun, run_id, with_for_update=True)
        row = await session.get(AutomationStep, step_id)
        assert run is not None and row is not None
        context = await _client_context(session, run.client_id)
        row.state = "failed"
        row.error = f"{phase}: {reason}"
        row.finished_at = now
        run.failure = f"{row.step_key} — {phase}: {reason}"
        run.current_step = row.step_key
        run.lease_until = None
        advance(run, RunState.FAILED, now=now)
        # The steps that did complete can be undone for the same window a
        # finished run gets, counted from the failure.
        run.undo_deadline_at = now + workflow(run.workflow_name).undo_window
        await _step_event(
            session,
            context,
            run,
            row,
            "failed",
            outcome=AuditOutcome.ERROR,
            phase=phase,
            reason=reason,
            phases=phases,
        )
        await run_event(
            session,
            context,
            run,
            "failed",
            outcome=AuditOutcome.ERROR,
            step=row.step_key,
            reason=reason,
            undo_until=run.undo_deadline_at.isoformat(),
        )
        await session.commit()
    return "failed"


# --- undoing -----------------------------------------------------------------------


async def _undo(maker: Maker, run_id: uuid.UUID, *, clock: Clock) -> str:
    while True:
        result = await _one_undo(maker, run_id, clock=clock)
        if result != "stepped":
            return result


async def _one_undo(maker: Maker, run_id: uuid.UUID, *, clock: Clock) -> str:
    now = clock()
    async with maker() as session:
        run = await session.get(AutomationRun, run_id, with_for_update=True)
        if run is None or run.undo_requested_at is None or RunState(run.state) not in UNDOABLE:
            return "stopped"
        context = await _client_context(session, run.client_id)
        if run.undo_deadline_at is not None and now > run.undo_deadline_at:
            run.undo_requested_at = None
            run.undo_failure = (
                f"the undo window closed at {run.undo_deadline_at:%d %b %Y %H:%M} UTC"
            )
            await run_event(
                session,
                context,
                run,
                "undo_refused",
                outcome=AuditOutcome.ERROR,
                reason=run.undo_failure,
            )
            await session.commit()
            return "undo_refused"
        if await controls.is_halted(session, run.client_id):
            run.lease_until = None
            await session.commit()
            return "paused"
        rows = await _rows(session, run.id)
        row = next((r for r in reversed(rows) if r.state == "done" and r.reversible), None)
        if row is None:
            skipped = [r.step_key for r in rows if r.state == "done" and not r.reversible]
            changed = [r.step_key for r in rows if r.state == "changed"]
            advance(run, RunState.UNDONE, now=now)
            run.undo_requested_at = None
            run.lease_until = None
            await run_event(
                session, context, run, "undone", not_reversible=skipped, changed_since=changed
            )
            await session.commit()
            return "undone"
        run.lease_until = now + LEASE
        step_id, step_key = row.id, row.step_key
        await session.commit()

    async with maker() as session:
        run = await session.get(AutomationRun, run_id)
        row = await session.get(AutomationStep, step_id)
        assert run is not None and row is not None
        context = await _client_context(session, run.client_id)
        step = workflow(run.workflow_name).step(step_key)
        assert step.undo is not None
        try:
            ctx = await _context_for(session, run, row, before=row.before)
            # Look first. The verify's facts are the after-state as it was
            # recorded; the same facts now, or the step is left alone.
            current = await step.verify(ctx)
            differences = _differences(row.after or {}, current.facts)
            if not current.ok or differences:
                row.state = "changed"
                row.error = "changed since the run, left alone: " + (
                    "; ".join(differences) or current.reason or "no longer verifies"
                )
                await _step_event(
                    session,
                    context,
                    run,
                    row,
                    "undo_skipped",
                    recorded=row.after,
                    found=current.facts,
                    reason=row.error,
                )
                await session.commit()
                return "stepped"
            result = await step.undo(ctx)
            row.state = "undone"
            row.undone_at = clock()
            await _step_event(
                session,
                context,
                run,
                row,
                "undo",
                before=row.after,
                after=row.before,
                result=result,
            )
            await session.commit()
            return "stepped"
        except Exception as exc:
            await session.rollback()
            logger.exception("undo of step %s of run %s raised", step_key, run_id)
            async with maker() as own:
                run = await own.get(AutomationRun, run_id, with_for_update=True)
                row = await own.get(AutomationStep, step_id)
                assert run is not None and row is not None
                context = await _client_context(own, run.client_id)
                row.state = "undo_failed"
                row.error = f"undo: {type(exc).__name__}: {exc}"
                run.undo_failure = f"{row.step_key} — {row.error}"
                run.undo_requested_at = None
                run.lease_until = None
                await _step_event(
                    session=own,
                    context=context,
                    run=run,
                    row=row,
                    action="undo_failed",
                    outcome=AuditOutcome.ERROR,
                    reason=row.error,
                )
                await run_event(
                    own,
                    context,
                    run,
                    "undo_failed",
                    outcome=AuditOutcome.ERROR,
                    reason=run.undo_failure,
                )
                await own.commit()
            return "undo_failed"


# --- shared --------------------------------------------------------------------------


def _differences(recorded: Facts, found: Facts) -> list[str]:
    """What differs between the after-state a step recorded and what is there
    now, as "key: recorded → found" lines. A key the step recorded and the
    verify no longer reports counts as a difference."""
    lines: list[str] = []
    for key in sorted(recorded):
        if key not in found or found[key] != recorded[key]:
            lines.append(f"{key}: {recorded[key]!r} → {found.get(key)!r}")
    return lines


async def _rows(session: AsyncSession, run_id: uuid.UUID) -> list[AutomationStep]:
    return list(
        (
            await session.execute(
                select(AutomationStep)
                .where(AutomationStep.run_id == run_id)
                .order_by(AutomationStep.position)
            )
        )
        .scalars()
        .all()
    )


async def _context_for(
    session: AsyncSession, run: AutomationRun, row: AutomationStep, *, before: Facts | None = None
) -> StepContext:
    config = await load_automation_config(session, run.client_id)
    return StepContext(
        session=session,
        run_id=run.id,
        client_id=run.client_id,
        step_key=row.step_key,
        inputs=run.inputs,
        settings=config.settings_for(run.workflow_name),
        attempt=row.attempts,
        before=before,
    )


__all__ = [
    "LEASE",
    "WORKER",
    "Outcome",
    "Step",
    "Workflow",
    "recover",
    "run_once",
]

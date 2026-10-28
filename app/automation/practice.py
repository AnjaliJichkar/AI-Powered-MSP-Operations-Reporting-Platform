"""The practice workflow: every control, proven on a run that touches nothing real.

It acts on `automation_practice_records`, a table the engine owns, on the
demo tenant only. Four steps, chosen so that the engine's rules each have a
step to be seen on:

1. **create_record** — makes the record this run is about. Idempotent: a
   second attempt finds it and leaves it. Undo deletes it.
2. **set_status** — depends on `create_record` being done. Captures the
   record's status *before* changing it to `offboarded`, so the undo puts
   back exactly what was there, and the audit shows both.
3. **close_out** — can be made to fail on demand. `inputs.fail_at` names a
   step and `inputs.fail_times` says how many attempts should fail; the
   default is one, so a run that fails here succeeds when resumed, which is
   the resume path demonstrated end to end. Depends on `set_status`, has a
   before state, has an undo.
4. **write_note** — has no undo. A note written is a thing that happened;
   the dry run lists this step as not reversible before anybody approves,
   and an undo of the run leaves the note and says so.

Three ways to start it, so each kind of trigger can be seen working: by hand
from the Automations page or a ticket page; a ticket rule that matches an offboarding ticket
(issue type "User Access and Management", sub-issue type "Offboarding", the
demo tenant's own labels, and the workflow is demo-only so no live ticket
ever starts one); and a schedule that drafts one a day. A trigger drafts and
nothing more: the run it makes is dry-run, submitted and approved like any
other.

The workflow is `app/automation/definitions/practice.yaml`, assembled from
the four practice actions in `app/automation/actions/practice.py`, each of
which wraps one step's functions below. Whoever writes a new action should
read this file beside `docs/automation-workflows.md`: the same functions per
step, the same rules about what they may do, and a worked example of each.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.automation.definitions import Change, Check, Facts, Plan, StepContext
from app.models.automation import PracticeRecord
from app.models.client import ClientKind

NAME = "practice"
VERSION = 1


class PracticeInputs(BaseModel):
    """What a practice run is told. Nothing real: a label for the record it
    makes, and two knobs for making a step fail so the failure path is
    something anybody can walk through."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        default="practice",
        min_length=1,
        max_length=60,
        description="A word for the record this run creates; the record is named "
        "<label>-<first eight characters of the run id>.",
    )
    fail_at: str | None = Field(
        default=None,
        description="A step key whose action should fail: close_out, usually. "
        "Null, and nothing fails.",
    )
    fail_times: int = Field(
        default=1,
        ge=1,
        le=10,
        description="How many attempts of that step should fail before it succeeds. "
        "One, and a resume gets through; more, and it fails again.",
    )


def record_name(ctx: StepContext) -> str:
    label = str(ctx.inputs.get("label") or "practice")
    return f"{label}-{str(ctx.run_id)[:8]}"


async def _record(ctx: StepContext) -> PracticeRecord | None:
    return (
        await ctx.session.execute(
            select(PracticeRecord).where(
                PracticeRecord.client_id == ctx.client_id,
                PracticeRecord.name == record_name(ctx),
            )
        )
    ).scalar_one_or_none()


def _snapshot(record: PracticeRecord | None) -> Facts:
    if record is None:
        return {"exists": False}
    return {"exists": True, "name": record.name, "status": record.status, "note": record.note}


def _should_fail(ctx: StepContext) -> bool:
    """Whether this attempt of this step is one the inputs asked to fail."""
    return ctx.inputs.get("fail_at") == ctx.step_key and ctx.attempt <= int(
        ctx.inputs.get("fail_times") or 1
    )


class MadeToFail(RuntimeError):
    """The failure the inputs asked for. Named so a reader of the audit row
    knows it was deliberate."""


# --- 1. create_record -------------------------------------------------------------


async def create_precondition(ctx: StepContext) -> Check:
    record = await _record(ctx)
    # Already there from an earlier attempt is fine: the action is idempotent
    # and the verify will pass. What is not fine is somebody else's record of
    # that name, which would make this run's "create" a lie.
    if record is not None and record.created_by_run_id not in (None, ctx.run_id):
        return Check.failed(
            f"a record called {record.name} already exists from another run", **_snapshot(record)
        )
    return Check.passed(**_snapshot(record))


async def create_plan(ctx: StepContext) -> Plan:
    name = record_name(ctx)
    return Plan(
        f"Create the practice record {name} with status new.",
        (Change("record", None, f"{name}, status new"),),
    )


async def create_action(ctx: StepContext) -> Facts:
    if _should_fail(ctx):
        raise MadeToFail(f"made to fail on attempt {ctx.attempt}, as the inputs asked")
    record = await _record(ctx)
    if record is None:
        record = PracticeRecord(
            client_id=ctx.client_id,
            name=record_name(ctx),
            status="new",
            created_by_run_id=ctx.run_id,
        )
        ctx.session.add(record)
        await ctx.session.flush()
        return {"created": True, **_snapshot(record)}
    return {"created": False, **_snapshot(record)}


async def create_verify(ctx: StepContext) -> Check:
    # Verify's facts are the step's after-state, and the undo compares them
    # with what is there when it comes to put things back: so each step
    # reports only what it owns. This one made the record exist with status
    # new — so a record somebody has since changed by hand is not deleted.
    record = await _record(ctx)
    if record is None:
        return Check.failed("the record is not there after creating it", exists=False)
    return Check.passed(exists=True, name=record.name, status=record.status)


async def create_undo(ctx: StepContext) -> Facts:
    record = await _record(ctx)
    if record is None:
        return {"deleted": False}
    await ctx.session.delete(record)
    await ctx.session.flush()
    return {"deleted": True, "name": record_name(ctx)}


# --- 2. set_status ------------------------------------------------------------------


async def _record_or_pending(ctx: StepContext, doing: str) -> Check:
    """The precondition the three dependent steps share.

    The record is made by `create_record`. At execution it is there, or this
    step may not run. In a dry run nothing has run yet, so its absence is not
    a failure: the step passes on the strength of its dependency and says so,
    and the checklist shows the approver what each step would do to a record
    the first step would make.
    """
    record = await _record(ctx)
    if record is not None:
        return Check.passed(**_snapshot(record))
    if ctx.dry_run:
        return Check.passed(exists=False, pending="create_record")
    return Check.failed(f"there is no record to {doing}", exists=False)


async def status_precondition(ctx: StepContext) -> Check:
    return await _record_or_pending(ctx, "set the status of")


async def status_plan(ctx: StepContext) -> Plan:
    record = await _record(ctx)
    current = record.status if record is not None else "new"
    return Plan(
        f"Set {record_name(ctx)} from {current} to offboarded.",
        (Change("status", current, "offboarded"),),
    )


async def status_before(ctx: StepContext) -> Facts:
    record = await _record(ctx)
    return {"status": record.status if record is not None else None}


async def status_action(ctx: StepContext) -> Facts:
    if _should_fail(ctx):
        raise MadeToFail(f"made to fail on attempt {ctx.attempt}, as the inputs asked")
    record = await _record(ctx)
    assert record is not None  # the precondition said so
    record.status = "offboarded"
    await ctx.session.flush()
    return {"status": record.status}


async def status_verify(ctx: StepContext) -> Check:
    record = await _record(ctx)
    if record is None or record.status != "offboarded":
        return Check.failed(
            f"status is {record.status if record else 'missing'}, not offboarded",
            status=record.status if record else None,
        )
    return Check.passed(status=record.status)


async def status_undo(ctx: StepContext) -> Facts:
    record = await _record(ctx)
    if record is None:
        return {"restored": False}
    was = (ctx.before or {}).get("status") or "new"
    record.status = was
    await ctx.session.flush()
    return {"restored": True, "status": was}


# --- 3. close_out --------------------------------------------------------------------


async def close_precondition(ctx: StepContext) -> Check:
    check = await _record_or_pending(ctx, "close out")
    if not check.ok or check.facts.get("pending"):
        return check
    # At execution the record is there, and it has to be offboarded first: the
    # dry run plans from "offboarded" because set_status will have run by then.
    if check.facts.get("status") != "offboarded" and not ctx.dry_run:
        return Check.failed(
            f"the record is {check.facts.get('status')}; close_out follows offboarding",
            **check.facts,
        )
    return check


async def close_plan(ctx: StepContext) -> Plan:
    return Plan(f"Mark {record_name(ctx)} closed.", (Change("status", "offboarded", "closed"),))


async def close_before(ctx: StepContext) -> Facts:
    record = await _record(ctx)
    return {"status": record.status if record is not None else None}


async def close_action(ctx: StepContext) -> Facts:
    if _should_fail(ctx):
        raise MadeToFail(f"made to fail on attempt {ctx.attempt}, as the inputs asked")
    record = await _record(ctx)
    assert record is not None
    record.status = "closed"
    await ctx.session.flush()
    return {"status": record.status}


async def close_verify(ctx: StepContext) -> Check:
    record = await _record(ctx)
    if record is None or record.status != "closed":
        return Check.failed("the record is not closed", status=record.status if record else None)
    return Check.passed(status=record.status)


async def close_undo(ctx: StepContext) -> Facts:
    record = await _record(ctx)
    if record is None:
        return {"restored": False}
    was = (ctx.before or {}).get("status") or "offboarded"
    record.status = was
    await ctx.session.flush()
    return {"restored": True, "status": was}


# --- 4. write_note (no undo) -----------------------------------------------------------


async def note_precondition(ctx: StepContext) -> Check:
    return await _record_or_pending(ctx, "write a note on")


async def note_plan(ctx: StepContext) -> Plan:
    return Plan(
        f"Write a note on {record_name(ctx)} saying the run closed it.",
        (Change("note", None, "closed by the practice run"),),
    )


async def note_action(ctx: StepContext) -> Facts:
    if _should_fail(ctx):
        raise MadeToFail(f"made to fail on attempt {ctx.attempt}, as the inputs asked")
    record = await _record(ctx)
    assert record is not None
    # Set, not appended: a second attempt writes the same note, not two.
    record.note = f"closed by practice run {str(ctx.run_id)[:8]}"
    await ctx.session.flush()
    return {"note": record.note}


async def note_verify(ctx: StepContext) -> Check:
    record = await _record(ctx)
    if record is None or not record.note:
        return Check.failed("the note is not on the record", note=None)
    return Check.passed(note=record.note)


# --- the workflow ---------------------------------------------------------------------


def demo_only(kind: str) -> str | None:
    if kind == ClientKind.DEMO:
        return None
    return (
        "The practice workflow runs on the demo tenant only. It exists to prove the "
        "controls, and a live client is not where that is done."
    )


# The workflow itself is `app/automation/definitions/practice.yaml`, assembled
# from the four practice actions (`app/automation/actions/practice.py`) that
# wrap the functions above. The engine registers it from the file.


def as_dict(record: PracticeRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "status": record.status,
        "note": record.note,
        "created_by_run_id": str(record.created_by_run_id) if record.created_by_run_id else None,
    }

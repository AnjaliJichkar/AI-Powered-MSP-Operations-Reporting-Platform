"""The practice actions: four blocks over the engine's own table.

They exist so every control — the dry run, the approval, a failure and a
resume, an undo, a step that cannot be undone — has a step to be seen on
without touching anything real. The functions themselves live in
`app/automation/practice.py`, where they always did, written against the
engine's `StepContext`; each action here hands that context in. The write
scope is the engine's own table and nothing else.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.automation import practice
from app.automation.actions.base import Action, ActionContext, Input, Output, register_action
from app.automation.definitions import Check, Facts, Plan, StepContext

SCOPE = "practice record on the engine's own table"

INPUTS = {
    "label": Input("string", "A word for the record this run creates."),
    "fail_at": Input("string", "A step id whose action should fail, for the demonstration.", False),
    "fail_times": Input("integer", "How many attempts of that step should fail.", False),
}


def _step_context(ctx: ActionContext) -> StepContext:
    if ctx.run_id is None:
        raise RuntimeError(f"{ctx.step_id}: a practice action runs inside a run")
    return StepContext(
        session=ctx.need_session(),
        run_id=ctx.run_id,
        client_id=ctx.client_id,
        step_key=ctx.step_id,
        inputs=dict(ctx.inputs),
        settings=ctx.settings,
        attempt=ctx.attempt,
        before=ctx.before,
        dry_run=ctx.dry_run,
    )


def _adapt[T](fn: Callable[[StepContext], Awaitable[T]]) -> Callable[[ActionContext], Awaitable[T]]:
    async def call(ctx: ActionContext) -> T:
        return await fn(_step_context(ctx))

    return call


def _practice_action(
    name: str,
    *,
    title: str,
    outputs: dict[str, Output],
    precondition: Callable[[StepContext], Awaitable[Check]],
    plan: Callable[[StepContext], Awaitable[Plan]],
    run: Callable[[StepContext], Awaitable[Facts]],
    verify: Callable[[StepContext], Awaitable[Check]],
    before: Callable[[StepContext], Awaitable[Facts]] | None = None,
    undo: Callable[[StepContext], Awaitable[Facts]] | None = None,
    checks: str,
    does: str,
    confirms: str,
    undoes: str | None = None,
) -> Action:
    return register_action(
        Action(
            name=name,
            version=1,
            title=title,
            connectors=("practice",),
            mode="write",
            write_scope=SCOPE,
            inputs=INPUTS,
            outputs=outputs,
            idempotency=("label",),
            precondition=_adapt(precondition),
            plan=_adapt(plan),
            before=_adapt(before) if before is not None else None,
            run=_adapt(run),
            verify=_adapt(verify),
            undo=_adapt(undo) if undo is not None else None,
            checks=checks,
            does=does,
            confirms=confirms,
            undoes=undoes,
        )
    )


CREATE_RECORD = _practice_action(
    "practice.create_record",
    title="Create the practice record",
    outputs={
        "name": Output(
            "string", "The record's name: the label and the run's first eight characters."
        ),
        "status": Output("string", "The record's status after the step: new."),
    },
    precondition=practice.create_precondition,
    plan=practice.create_plan,
    run=practice.create_action,
    verify=practice.create_verify,
    undo=practice.create_undo,
    checks="no other run owns a record of this name",
    does="creates the record with status new, or finds the one this run made",
    confirms="reads the record back by name",
    undoes="deletes the record",
)

SET_STATUS = _practice_action(
    "practice.set_status",
    title="Mark it offboarded",
    outputs={"status": Output("string", "The record's status after the step: offboarded.")},
    precondition=practice.status_precondition,
    plan=practice.status_plan,
    before=practice.status_before,
    run=practice.status_action,
    verify=practice.status_verify,
    undo=practice.status_undo,
    checks="the record exists",
    does="sets its status to offboarded, having noted what it was",
    confirms="reads the status back and finds offboarded",
    undoes="puts the status back to what it was before",
)

CLOSE_OUT = _practice_action(
    "practice.close_out",
    title="Close it out",
    outputs={"status": Output("string", "The record's status after the step: closed.")},
    precondition=practice.close_precondition,
    plan=practice.close_plan,
    before=practice.close_before,
    run=practice.close_action,
    verify=practice.close_verify,
    undo=practice.close_undo,
    checks="the record exists and is offboarded",
    does="sets its status to closed; fails on demand when the inputs ask it to",
    confirms="reads the status back and finds closed",
    undoes="puts the status back to what it was before",
)

WRITE_NOTE = _practice_action(
    "practice.write_note",
    title="Write the closing note",
    outputs={"note": Output("string", "The note on the record.")},
    precondition=practice.note_precondition,
    plan=practice.note_plan,
    run=practice.note_action,
    verify=practice.note_verify,
    checks="the record exists",
    does="writes one closing note on it, the same note on every attempt",
    confirms="reads the note back",
)


__all__ = ["CLOSE_OUT", "CREATE_RECORD", "SET_STATUS", "WRITE_NOTE"]

"""The one write: add an internal note to an Autotask ticket.

The only write action in the library, and its scope is the only line on the
allow-list that touches a client's system. It goes through the `NoteWriter`
the caller hands in — the engine's own write client, with its own credential,
which can add a note and refuses every other write — and is idempotent for
one (run, step, ticket): the note service keeps one note per workflow per
ticket and never asks twice for the same one.

A note added cannot be undone: a note on a ticket is a thing that happened.
The verify is the writer's answer — Autotask returns the note's id when the
note is on the ticket, and the writer raises when it is not.
"""

from __future__ import annotations

from app.automation.actions.base import (
    Action,
    ActionContext,
    Input,
    Output,
    register_action,
)
from app.automation.definitions import Check, Facts, Plan
from app.automation.notes.definitions import Draft, Skip

SCOPE = "add internal ticket note"
TITLE_PREFIX = "Desk: "


def _draft(ctx: ActionContext) -> Draft | None:
    note = ctx.inputs.get("note")
    return note if isinstance(note, Draft) else None


async def precondition(ctx: ActionContext) -> Check:
    note = ctx.inputs.get("note")
    if isinstance(note, Skip):
        return Check.failed(f"no note to add: {note.reason}", skipped=True)
    draft = _draft(ctx)
    if draft is None:
        return Check.failed("no note to add", skipped=True)
    if not draft.title.strip() or not draft.body.strip():
        return Check.failed("the note has no title or no body")
    return Check.passed(ticket_id=str(ctx.inputs.get("ticket_id")), title=draft.title)


async def plan(ctx: ActionContext) -> Plan:
    draft = _draft(ctx)
    title = draft.title if draft is not None else "(no note)"
    return Plan(f"Add an internal note to ticket {ctx.inputs.get('ticket_id')}: {title}.")


async def run(ctx: ActionContext) -> Facts:
    draft = _draft(ctx)
    if draft is None:
        raise RuntimeError(f"{ctx.step_id}: no note to add")
    title = draft.title if draft.title.startswith(TITLE_PREFIX) else TITLE_PREFIX + draft.title
    posted = await ctx.need_writer().add_internal_note(
        str(ctx.inputs["ticket_id"]), title=title, body=draft.body
    )
    return {
        "note_id": str(posted.note_id),
        "publish": posted.publish,
        "note_type": posted.note_type,
        "title": title,
    }


async def verify(ctx: ActionContext) -> Check:
    """The writer returns the note's id from Autotask's own answer and raises
    on anything but a 2xx: a note id is the note being on the ticket. The
    writer can make no other request, so there is nothing more it could read."""
    note_id = (ctx.outputs or {}).get("note_id")
    if note_id:
        return Check.passed(note_id=str(note_id))
    return Check.failed("Autotask returned no note id", note_id=None)


ADD_NOTE = register_action(
    Action(
        name="autotask.add_note",
        version=1,
        title="Autotask: add an internal note to the ticket",
        connectors=("autotask",),
        mode="write",
        write_scope=SCOPE,
        inputs={
            "ticket_id": Input("string", "The Autotask ticket id."),
            "note": Input("note", "The note to add: title and body, or the reason none is."),
        },
        outputs={
            "note_id": Output("string", "The note's id at Autotask."),
            "title": Output("string", "The title as posted, under the Desk: prefix."),
        },
        idempotency=("ticket_id",),
        precondition=precondition,
        plan=plan,
        run=run,
        verify=verify,
        checks="there is a note to add, with a title and a body",
        does="adds it to the ticket as an internal note, under the Desk: title",
        confirms="Autotask returned the note's id",
        undoes=None,
    )
)

__all__ = ["ADD_NOTE", "SCOPE", "TITLE_PREFIX"]

"""The Desk notes, as JSON: the note on a ticket, Post and Not useful, the
mode per workflow, the Notes view, the chronic report, and the note
workflows for the Automations and workflow pages.

Who may do what: the Automations and workflow pages, the modes and the
reports are Inputiv staff only, like the rest of the engine. The note on a
ticket, and posting or dismissing it, are **Inputiv staff only** unless the
client's `automation.notes.franchisee_can_post` setting is on, when the
franchisee's administrators and technicians (the roles that work a ticket,
`CAN_CHAT`) may as well. Off, a franchisee user is refused the note's JSON
too (403), so the ticket page shows them no Desk note card: whether a note
exists is not something they are told until the franchisee has agreed to
act on them. Every post and dismissal is audited with who.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CAN_CHAT, INPUTIV_ANY
from app.auth.scope import Scope
from app.automation import controls, library, service, triggers
from app.automation.config import AutomationConfig, load_automation_config
from app.automation.notes import service as notes
from app.automation.notes.definitions import NoteWorkflow, note_workflow, note_workflows
from app.db.session import get_session
from app.models.automation import AutomationNote, AutomationNoteMode, AutomationReport
from app.models.client import Client

router = APIRouter(prefix="/automation", tags=["automation"])

APPROVAL_RULES = {
    "preview": (
        "Notes in preview need no run approval: nothing leaves the app. An engineer "
        "posting a note to Autotask is the approval for that note."
    ),
    "auto": (
        "Auto-posting is approved once, at the workflow level, by the franchisee's "
        "approver recorded when the mode was switched; every note the workflow builds is "
        "then posted at once, unless the client's kill switch is thrown."
    ),
    "report": "Nothing leaves the app: the report is a page in Desk and needs no approval.",
}


class ModeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field(pattern="^(preview|auto)$")
    authoriser_name: str | None = Field(default=None, max_length=120)
    authoriser_method: str | None = Field(default=None, max_length=40)
    authoriser_at: dt.datetime | None = None


def _actor(scope: Scope) -> service.Actor:
    assert scope.client_id is not None and scope.client_slug is not None
    return service.Actor(
        user_id=scope.user.id,
        email=scope.user.email,
        client_id=scope.client_id,
        client_slug=scope.client_slug,
    )


async def _may_act_on_notes(db: AsyncSession, scope: Scope) -> AutomationConfig:
    """Inputiv staff always; the franchisee's own engineers only when the
    client's `automation.notes.franchisee_can_post` is on. Otherwise 403,
    the same answer the engine gives a franchisee everywhere else."""
    config = await load_automation_config(db, scope.require_client())
    if scope.is_inputiv or config.notes.franchisee_can_post:
        return config
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Desk notes are Inputiv staff's to post for this client",
    )


def _refused(exc: service.Refused) -> HTTPException:
    code = (
        status.HTTP_404_NOT_FOUND if isinstance(exc, service.NotFound) else status.HTTP_409_CONFLICT
    )
    return HTTPException(status_code=code, detail=str(exc))


def note_view(note: AutomationNote) -> dict[str, Any]:
    return {
        "id": str(note.id),
        "workflow": note.workflow_name,
        "workflow_title": _title(note.workflow_name),
        "version": note.workflow_version,
        "ticket_id": note.ticket_id,
        "ticket_number": note.ticket_number,
        "device": note.device_name,
        "mode": note.mode,
        "state": note.state,
        "title": note.title,
        "body": note.body,
        "facts": note.facts,
        "read_failures": note.read_failures,
        "built_at": note.built_at.isoformat(),
        "posted_at": note.posted_at.isoformat() if note.posted_at else None,
        "posted_by": note.posted_by,
        "autotask_note_id": note.autotask_note_id,
        "post_error": note.post_error,
        "not_useful_at": note.not_useful_at.isoformat() if note.not_useful_at else None,
        "not_useful_by": note.not_useful_by,
        "ticket_closed_at": note.ticket_closed_at.isoformat() if note.ticket_closed_at else None,
        "can": {
            "post": note.state in ("built", "post_failed"),
            "dismiss": note.state in ("built", "post_failed"),
        },
    }


def _title(name: str) -> str:
    try:
        return note_workflow(name).title
    except KeyError:
        return name


def mode_view(row: AutomationNoteMode | None) -> dict[str, Any]:
    mode = row.mode if row is not None else notes.PREVIEW
    return {
        "mode": mode,
        "word": "Auto-post" if mode == notes.AUTO else "Preview",
        "authoriser": (
            {
                "name": row.authoriser_name,
                "method": row.authoriser_method,
                "at": row.authoriser_at.isoformat() if row.authoriser_at else None,
            }
            if row is not None and row.mode == notes.AUTO and row.authoriser_name
            else None
        ),
        "changed_by": row.changed_by if row is not None else None,
        "changed_at": row.changed_at.isoformat() if row is not None else None,
        "approval_rule": APPROVAL_RULES[mode],
    }


async def note_workflow_views(db: AsyncSession, client: Client) -> list[dict[str, Any]]:
    """The note and report workflows as the Automations page lists them,
    beside the run workflows: kind, trigger sentence, switch, mode."""
    switches = await controls.switches_for(db, client.id)
    trigger_switches = await triggers.trigger_switches_for(db, client.id)
    modes = await notes.modes_for(db, client.id)
    config = await load_automation_config(db, client.id)
    found = []
    for definition in note_workflows():
        found.append(
            _note_workflow_view(definition, client, switches, trigger_switches, modes, config)
        )
    return found


def _note_workflow_view(
    definition: NoteWorkflow,
    client: Client,
    switches: dict[str, Any],
    trigger_switches: dict[tuple[str, str], Any],
    modes: dict[str, AutomationNoteMode],
    config: Any,
) -> dict[str, Any]:
    overrides = config.settings_for(definition.name).get("triggers") or {}
    rule = (
        definition.trigger.rule.with_overrides(overrides.get(definition.trigger.key) or {})
        if definition.trigger.rule is not None
        else None
    )
    row = switches.get(definition.name)
    trigger_row = trigger_switches.get((definition.name, definition.trigger.key))
    mode = mode_view(modes.get(definition.name)) if definition.kind == "note" else None
    return {
        "name": definition.name,
        "kind": definition.kind,
        "version": definition.version,
        "title": definition.title,
        "description": definition.description,
        "manual": False,
        "enabled": bool(row is not None and row.enabled),
        "switched_by": row.changed_by if row is not None else None,
        "allowed_here": True,
        "not_allowed_because": None,
        "writes": definition.writes,
        "reads": list(definition.reads),
        "template": definition.template,
        "definition_hash": definition.definition_hash or None,
        "steps": [
            {
                "key": s["id"],
                "title": s["title"],
                "action": s["action"],
                "writes": s["writes"],
                "reversible": False,
                "depends_on": None,
            }
            for s in definition.step_views
        ],
        "missing_settings": [
            {"key": m.key, "type": m.type, "description": m.description}
            for m in library.missing_settings_for(
                definition.name, config.settings_for(definition.name)
            )
        ],
        "not_reversible": [],
        "undo_window_hours": None,
        "approval_window_hours": None,
        "triggers": [
            {
                "key": definition.trigger.key,
                "kind": definition.trigger.kind,
                "kind_word": triggers.KIND_WORDS[definition.trigger.kind],
                "title": definition.trigger.title,
                "sentence": triggers.sentence(definition.trigger, rule, client_name=client.name),
                "rule": rule.conditions() if rule is not None else None,
                "every_hours": (
                    definition.trigger.every.total_seconds() / 3600
                    if definition.trigger.every
                    else None
                ),
                "enabled": bool(trigger_row is not None and trigger_row.enabled),
                "switched_by": trigger_row.changed_by if trigger_row is not None else None,
                "switched_at": trigger_row.changed_at.isoformat() if trigger_row else None,
            }
        ],
        "mode": mode,
        "approval_rule": mode["approval_rule"] if mode else APPROVAL_RULES["report"],
        "settings_used": [
            {"key": key, "what_for": what_for, "set": key in config.settings_for(definition.name)}
            for key, what_for in definition.settings_used
        ],
    }


# --- the note on a ticket -------------------------------------------------------------------


@router.get("/notes/for-ticket/{ticket_id}")
async def notes_for_ticket(
    ticket_id: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """The Desk notes on one ticket, by the vendor's ticket id, with each
    workflow's mode so the page can say "Desk note, not posted"."""
    client_id = scope.require_client()
    await _may_act_on_notes(db, scope)
    rows = await notes.notes_for_ticket(db, client_id, ticket_id)
    halted = await controls.is_halted(db, client_id)
    return {
        "notes": [note_view(n) for n in rows],
        "halted": halted,
        "approval_rules": APPROVAL_RULES,
    }


@router.post("/notes/{note_id}/post")
async def post_note(
    note_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """Post one preview note to Autotask, as the signed-in engineer. The one
    request on the pages that leaves the app; the writer is the engine's."""
    from app.tickets.alert_tools import writer_for

    actor = _actor(scope)
    await _may_act_on_notes(db, scope)
    client = await db.get(Client, actor.client_id)
    assert client is not None
    try:
        note = await notes.get_note(db, actor.client_id, note_id)
        writer = await writer_for(actor.client_id)
        await notes.post(db, client, note, writer=writer, by=actor, now=dt.datetime.now(dt.UTC))
    except service.Refused as exc:
        raise _refused(exc) from None
    await db.commit()
    return note_view(note)


@router.post("/notes/{note_id}/not-useful")
async def dismiss_note(
    note_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    actor = _actor(scope)
    await _may_act_on_notes(db, scope)
    client = await db.get(Client, actor.client_id)
    assert client is not None
    try:
        note = await notes.get_note(db, actor.client_id, note_id)
        await notes.mark_not_useful(db, client, note, by=actor, now=dt.datetime.now(dt.UTC))
    except service.Refused as exc:
        raise _refused(exc) from None
    await db.commit()
    return note_view(note)


# --- the mode, Inputiv staff only ----------------------------------------------------------------


@router.post("/note-modes/{workflow_name}")
async def set_note_mode(
    workflow_name: str,
    payload: ModeIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    actor = _actor(scope)
    try:
        row = await notes.set_mode(
            db,
            actor,
            workflow_name=workflow_name,
            mode=payload.mode,
            authoriser_name=payload.authoriser_name,
            authoriser_method=payload.authoriser_method,
            authoriser_at=payload.authoriser_at,
            now=dt.datetime.now(dt.UTC),
        )
    except service.Refused as exc:
        raise _refused(exc) from None
    await db.commit()
    return {"workflow": workflow_name, **mode_view(row)}


# --- the Notes view and the chronic report -----------------------------------------------------


@router.get("/notes/report")
async def notes_report(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    weeks: int = 8,
) -> dict[str, Any]:
    client_id = scope.require_client()
    return await notes.report(
        db, client_id, now=dt.datetime.now(dt.UTC), weeks=max(1, min(weeks, 26))
    )


@router.get("/notes/recent")
async def recent_notes(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    limit: int = 50,
) -> dict[str, Any]:
    """The newest notes for this client, for the Notes view's list."""
    from sqlalchemy import select

    client_id = scope.require_client()
    rows = (
        (
            await db.execute(
                select(AutomationNote)
                .where(AutomationNote.client_id == client_id)
                .order_by(AutomationNote.built_at.desc())
                .limit(max(1, min(limit, 200)))
            )
        )
        .scalars()
        .all()
    )
    return {"notes": [note_view(n) for n in rows]}


@router.get("/reports/{workflow_name}")
async def latest_report(
    workflow_name: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """The latest report a scheduled workflow built for this client."""
    try:
        definition = note_workflow(workflow_name)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    if definition.kind != "report":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not a report workflow")
    client_id = scope.require_client()
    row: AutomationReport | None = await notes.latest_report(db, client_id, workflow_name)
    return {
        "workflow": {
            "name": definition.name,
            "title": definition.title,
            "description": definition.description,
        },
        "report": (
            {
                "id": row.id,
                "built_at": row.built_at.isoformat(),
                "period_start": row.period_start.isoformat(),
                "period_end": row.period_end.isoformat(),
                "body": row.body,
            }
            if row is not None
            else None
        ),
    }

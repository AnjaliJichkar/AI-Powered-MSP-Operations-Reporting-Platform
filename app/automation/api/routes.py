"""The Automations page, the run page and the workflow page, as JSON.

The workflow page is read-only and provably so: every route under
`/automation/workflows` is a GET, asserted on the router's own method list in
`tests/api/test_automation_pages.py`. The switches — the workflow's, a
trigger's, the kill switch — are written under `/automation/switches` and
`/automation/kill-switch`, and nowhere else."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import INPUTIV_ANY
from app.auth.scope import Scope
from app.automation import controls, library, service, triggers
from app.automation.api.notes import APPROVAL_RULES, note_workflow_views
from app.automation.config import load_automation_config
from app.automation.definitions import INPUT_SOURCES, Workflow, workflow, workflows
from app.automation.states import UNDOABLE, WORDS, RunState
from app.db.session import get_session
from app.models.automation import AutomationRun, AutomationStep, AutomationTriggerLog
from app.models.client import Client

router = APIRouter(prefix="/automation", tags=["automation"])


# --- bodies ---------------------------------------------------------------------


class SwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class KillSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    halted: bool
    reason: str | None = Field(default=None, max_length=300)


class TicketIn(BaseModel):
    """The ticket a run is started from, as the ticket page holds it. The
    workflow's `input_sources` say which of these fill which input; an input
    given explicitly wins."""

    model_config = ConfigDict(extra="forbid")
    number: str = Field(min_length=1, max_length=40)
    title: str | None = Field(default=None, max_length=500)
    contact: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    site: str | None = Field(default=None, max_length=200)


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    ticket_number: str | None = Field(default=None, max_length=40)
    ticket: TicketIn | None = None


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authoriser_name: str = Field(min_length=1, max_length=120)
    authoriser_method: str = Field(min_length=1, max_length=40)
    authoriser_at: dt.datetime | None = None
    authoriser_ref: str | None = Field(default=None, max_length=300)


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=300)


# --- views ------------------------------------------------------------------------


def _actor(scope: Scope) -> service.Actor:
    assert scope.client_id is not None and scope.client_slug is not None
    return service.Actor(
        user_id=scope.user.id,
        email=scope.user.email,
        client_id=scope.client_id,
        client_slug=scope.client_slug,
    )


def _state_view(state: str) -> dict[str, str]:
    word, tone = WORDS[RunState(state)]
    return {"key": state, "word": word, "tone": tone}


def _run_view(run: AutomationRun) -> dict[str, Any]:
    definition = workflow(run.workflow_name)
    return {
        "id": str(run.id),
        "workflow": run.workflow_name,
        "workflow_title": definition.title,
        "version": run.workflow_version,
        "definition_hash": run.definition_hash,
        "state": _state_view(run.state),
        "inputs": run.inputs,
        "ticket_number": run.ticket_number,
        "started_by": run.started_by,
        "started_by_user_id": str(run.started_by_user_id) if run.started_by_user_id else None,
        "started_how": "trigger" if run.trigger_key else "engineer",
        "trigger_key": run.trigger_key,
        "trigger_detail": run.trigger_detail,
        "approved_by": run.approved_by,
        "approval_expires_at": (
            run.approval_expires_at.isoformat() if run.approval_expires_at else None
        ),
        "resume_by": _resume_by(run),
        "authoriser": (
            {
                "name": run.authoriser_name,
                "method": run.authoriser_method,
                "at": run.authoriser_at.isoformat() if run.authoriser_at else None,
                "ref": run.authoriser_ref,
            }
            if run.authoriser_name
            else None
        ),
        "dry_run": run.dry_run,
        "dry_run_at": run.dry_run_at.isoformat() if run.dry_run_at else None,
        "current_step": run.current_step,
        "failure": run.failure,
        "paused_at": run.paused_at.isoformat() if run.paused_at else None,
        "undo_deadline_at": run.undo_deadline_at.isoformat() if run.undo_deadline_at else None,
        "undo_requested_at": run.undo_requested_at.isoformat() if run.undo_requested_at else None,
        "undo_failure": run.undo_failure,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "undone_at": run.undone_at.isoformat() if run.undone_at else None,
    }


def _resume_by(run: AutomationRun) -> str | None:
    if run.state != RunState.FAILED.value:
        return None
    deadline = service.resume_deadline(run)
    return deadline.isoformat() if deadline is not None else None


def _step_view(row: AutomationStep) -> dict[str, Any]:
    return {
        "key": row.step_key,
        "position": row.position,
        "title": row.title,
        "depends_on": row.depends_on,
        "reversible": row.reversible,
        "state": row.state,
        "precondition": row.precondition,
        "planned": row.planned,
        "before": row.before,
        "after": row.after,
        "verification": row.verification,
        "inputs": row.inputs,
        "outputs": row.outputs,
        "attempts": row.attempts,
        "error": row.error,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "undone_at": row.undone_at.isoformat() if row.undone_at else None,
    }


def _missing_settings_view(name: str, config: Any) -> list[dict[str, Any]]:
    """The required settings the client has not set, for the page's
    "Needs settings" word and the list behind it."""
    return [
        {"key": m.key, "type": m.type, "description": m.description}
        for m in library.missing_settings_for(name, config.settings_for(name))
    ]


def _settings_view(name: str, config: Any) -> list[dict[str, Any]]:
    """Every setting the definition declares, with whether the client set it."""
    loaded = library.find(name)
    given = config.settings_for(name)
    if loaded is None:
        return []
    return [
        {
            "key": s.key,
            "type": s.type,
            "what_for": s.description,
            "required": s.required,
            "default": s.default,
            "set": s.key in given,
        }
        for s in loaded.settings
    ]


def _definition_view(name: str) -> dict[str, Any] | None:
    """The definition a workflow came from: the file, the hash, each step's
    action and the references it is given — the data, as reviewed."""
    loaded = library.find(name)
    if loaded is None:
        return None
    return {
        "file": loaded.file,
        "hash": loaded.hash,
        "kind": loaded.kind,
        "steps": [
            {
                "id": s.id,
                "action": s.action,
                "title": s.title,
                "inputs": dict(s.inputs),
                "depends_on": s.depends_on,
                "when": [c.model_dump(by_alias=True, exclude_none=True) for c in s.conditions()],
            }
            for s in loaded.steps
        ],
    }


def _workflow_view(name: str) -> dict[str, Any]:
    definition = workflow(name)
    return {
        "name": definition.name,
        "version": definition.version,
        "title": definition.title,
        "description": definition.description,
        "manual": definition.manual,
        "definition_hash": definition.definition_hash or None,
        "inputs_schema": definition.inputs.model_json_schema(),
        "steps": [
            {
                "key": step.key,
                "title": step.title,
                "action": step.action_name,
                "depends_on": step.depends_on,
                "reversible": step.reversible,
            }
            for step in definition.steps
        ],
        "not_reversible": list(definition.not_reversible),
        "undo_window_hours": definition.undo_window.total_seconds() / 3600,
        "approval_window_hours": definition.approval_window.total_seconds() / 3600,
    }


async def _triggers_view(
    db: AsyncSession, client: Client, definition: Workflow
) -> list[dict[str, Any]]:
    """Each trigger as the pages draw it: one sentence, with the rule as this
    client has it and whether it is on here."""
    switches = await triggers.trigger_switches_for(db, client.id)
    config = await load_automation_config(db, client.id)
    overrides = config.settings_for(definition.name).get("triggers") or {}
    found = []
    for trigger in definition.triggers:
        rule = (
            trigger.rule.with_overrides(overrides.get(trigger.key) or {})
            if trigger.rule is not None
            else None
        )
        row = switches.get((definition.name, trigger.key))
        found.append(
            {
                "key": trigger.key,
                "kind": trigger.kind,
                "kind_word": triggers.KIND_WORDS[trigger.kind],
                "title": trigger.title,
                "sentence": triggers.sentence(trigger, rule, client_name=client.name),
                "rule": rule.conditions() if rule is not None else None,
                "every_hours": trigger.every.total_seconds() / 3600 if trigger.every else None,
                "enabled": bool(row is not None and row.enabled),
                "switched_by": row.changed_by if row is not None else None,
                "switched_at": row.changed_at.isoformat() if row is not None else None,
            }
        )
    return found


def _inputs_view(definition: Workflow) -> list[dict[str, Any]]:
    schema = definition.inputs.model_json_schema()
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    found = []
    for name, spec in properties.items():
        source = definition.source_of(name)
        found.append(
            {
                "name": name,
                "description": spec.get("description"),
                "required": name in required,
                "default": spec.get("default"),
                "source": source,
                "source_words": INPUT_SOURCES[source],
            }
        )
    return found


def _steps_view(definition: Workflow) -> list[dict[str, Any]]:
    return [
        {
            "position": position,
            "key": step.key,
            "title": step.title,
            "action": step.action_name,
            "checks": step.checks,
            "does": step.does,
            "confirms": step.confirms,
            "undoes": step.undoes,
            "reversible": step.reversible,
            "depends_on": step.depends_on,
            "depends_on_title": definition.step(step.depends_on).title if step.depends_on else None,
        }
        for position, step in enumerate(definition.steps, start=1)
    ]


def _log_view(row: AutomationTriggerLog) -> dict[str, Any]:
    title = (
        workflow(row.workflow_name).title if row.workflow_name in _names() else row.workflow_name
    )
    return {
        "id": row.id,
        "workflow": row.workflow_name,
        "workflow_title": title,
        "trigger": row.trigger_key,
        "kind": row.kind,
        "kind_word": triggers.KIND_WORDS.get(row.kind, row.kind),
        "ticket_id": row.ticket_id,
        "ticket_number": row.ticket_number,
        "matched": row.matched,
        "outcome": row.outcome,
        "reason": row.reason,
        "rule": row.rule,
        "run_id": str(row.run_id) if row.run_id else None,
        "evaluated_at": row.evaluated_at.isoformat(),
    }


def _names() -> set[str]:
    return {w.name for w in workflows()}


def _what_can_be_done(run: AutomationRun, scope: Scope, *, now: dt.datetime) -> dict[str, bool]:
    """Which buttons the page draws. The service refuses anyway; this is so the
    page does not offer what will be refused."""
    state = RunState(run.state)
    own = run.started_by_user_id == scope.user.id
    resume_by = service.resume_deadline(run)
    return {
        "dry_run": state in (RunState.DRAFTED, RunState.DRY_RUN_READY),
        "submit": state is RunState.DRY_RUN_READY,
        "withdraw": state in (RunState.DRY_RUN_READY, RunState.AWAITING_APPROVAL),
        "approve": state is RunState.AWAITING_APPROVAL and not own,
        "cancel": state
        in (
            RunState.DRAFTED,
            RunState.DRY_RUN_READY,
            RunState.AWAITING_APPROVAL,
            RunState.APPROVED,
        ),
        "resume": state is RunState.FAILED and (resume_by is None or now <= resume_by),
        "resubmit": state is RunState.FAILED and resume_by is not None and now > resume_by,
        "abandon": state is RunState.FAILED,
        "undo": state in UNDOABLE
        and run.undo_requested_at is None
        and (run.undo_deadline_at is None or now <= run.undo_deadline_at),
    }


def _known(workflow_name: str) -> tuple[str, ...]:
    """The trigger keys of a run or note workflow, or a 404: the switches are
    for every kind of workflow the engine has."""
    from app.automation.notes.definitions import note_workflow

    try:
        return tuple(t.key for t in workflow(workflow_name).triggers)
    except KeyError:
        pass
    try:
        return (note_workflow(workflow_name).trigger.key,)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None


def _refused(exc: service.Refused) -> HTTPException:
    code = (
        status.HTTP_404_NOT_FOUND if isinstance(exc, service.NotFound) else status.HTTP_409_CONFLICT
    )
    return HTTPException(status_code=code, detail=str(exc))


# --- the overview ------------------------------------------------------------------


@router.get("/overview")
async def overview(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """Everything the Automations page shows for the session's client: the runs
    newest first, the switch per workflow, the kill switch, and the workflows
    that exist with what each may run on."""
    client_id = scope.require_client()
    client = await db.get(Client, client_id)
    assert client is not None
    runs = (
        (
            await db.execute(
                select(AutomationRun)
                .where(AutomationRun.client_id == client_id)
                .order_by(AutomationRun.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    switches = await controls.switches_for(db, client_id)
    kill = await controls.kill_switch_for(db, client_id)
    config = await load_automation_config(db, client_id)
    return {
        "client": {"slug": client.slug, "name": client.name, "kind": client.kind},
        "runs": [_run_view(run) for run in runs],
        "workflows": [
            {
                **_workflow_view(definition.name),
                "enabled": bool(definition.name in switches and switches[definition.name].enabled),
                "switched_by": switches[definition.name].changed_by
                if definition.name in switches
                else None,
                "switched_at": switches[definition.name].changed_at.isoformat()
                if definition.name in switches
                else None,
                "allowed_here": definition.allowed_on(client.kind) is None,
                "not_allowed_because": definition.allowed_on(client.kind),
                "triggers": await _triggers_view(db, client, definition),
                "missing_settings": _missing_settings_view(definition.name, config),
            }
            for definition in workflows()
        ]
        + await note_workflow_views(db, client),
        "trigger_log": [_log_view(row) for row in await triggers.recent_log(db, client_id)],
        "kill_switch": {
            "halted": bool(kill is not None and kill.halted),
            "reason": kill.reason if kill is not None else None,
            "by": kill.changed_by if kill is not None else None,
            "at": kill.changed_at.isoformat() if kill is not None else None,
        },
        "approvers": [a.model_dump() for a in config.approvers],
        "me": {"user_id": str(scope.user.id), "email": scope.user.email},
    }


@router.post("/switches/{workflow_name}")
async def set_switch(
    workflow_name: str,
    payload: SwitchIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    _known(workflow_name)
    actor = _actor(scope)
    try:
        row = await controls.set_switch(
            db,
            actor.context(),
            client_id=actor.client_id,
            workflow_name=workflow_name,
            enabled=payload.enabled,
            who=actor.who(),
            now=dt.datetime.now(dt.UTC),
        )
    except controls.NeedsSettings as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    await db.commit()
    return {"workflow": workflow_name, "enabled": row.enabled, "by": row.changed_by}


@router.post("/kill-switch")
async def set_kill_switch(
    payload: KillSwitchIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    actor = _actor(scope)
    row = await controls.set_kill_switch(
        db,
        actor.context(),
        client_id=actor.client_id,
        halted=payload.halted,
        reason=payload.reason,
        who=actor.who(),
        now=dt.datetime.now(dt.UTC),
    )
    await db.commit()
    return {"halted": row.halted, "reason": row.reason, "by": row.changed_by}


@router.post("/switches/{workflow_name}/triggers/{trigger_key}")
async def set_trigger_switch(
    workflow_name: str,
    trigger_key: str,
    payload: SwitchIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    if trigger_key not in _known(workflow_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{workflow_name} has no trigger called {trigger_key!r}",
        )
    actor = _actor(scope)
    row = await triggers.set_trigger_switch(
        db,
        actor.context(),
        client_id=actor.client_id,
        workflow_name=workflow_name,
        trigger_key=trigger_key,
        enabled=payload.enabled,
        who=actor.who(),
        now=dt.datetime.now(dt.UTC),
    )
    await db.commit()
    return {
        "workflow": workflow_name,
        "trigger": trigger_key,
        "enabled": row.enabled,
        "by": row.changed_by,
    }


# --- the workflow page: read only ------------------------------------------------------


@router.get("/workflows/{workflow_name}")
async def read_workflow(
    workflow_name: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """One workflow as this client has it: what starts it, what it is told and
    where that comes from, its steps in words, the approval rule, the undo
    and approval windows, the version, and the client settings it reads."""
    client_id = scope.require_client()
    client = await db.get(Client, client_id)
    assert client is not None
    try:
        definition = workflow(workflow_name)
    except KeyError:
        found = [w for w in await note_workflow_views(db, client) if w["name"] == workflow_name]
        if not found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no workflow called {workflow_name!r}",
            ) from None
        return await _note_workflow_page(db, client, found[0])
    switches = await controls.switches_for(db, client_id)
    config = await load_automation_config(db, client_id)
    recent = (
        (
            await db.execute(
                select(AutomationRun)
                .where(
                    AutomationRun.client_id == client_id,
                    AutomationRun.workflow_name == definition.name,
                )
                .order_by(AutomationRun.created_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    row = switches.get(definition.name)
    return {
        "client": {"slug": client.slug, "name": client.name, "kind": client.kind},
        "workflow": {
            **_workflow_view(definition.name),
            "enabled": bool(row is not None and row.enabled),
            "switched_by": row.changed_by if row is not None else None,
            "allowed_here": definition.allowed_on(client.kind) is None,
            "not_allowed_because": definition.allowed_on(client.kind),
        },
        "triggers": await _triggers_view(db, client, definition),
        "inputs": _inputs_view(definition),
        "steps": _steps_view(definition),
        "approval": {
            "rule": (
                "The client's authoriser — somebody on the approver list — says yes, and an "
                "Inputiv engineer other than the one who started the run records it."
            ),
            "approvers": [a.model_dump() for a in config.approvers],
            "window_hours": definition.approval_window.total_seconds() / 3600,
            "undo_window_hours": definition.undo_window.total_seconds() / 3600,
            "stale_after_days": triggers.STALE_AFTER.days,
        },
        "settings_used": _settings_view(definition.name, config)
        or [
            {
                "key": key,
                "what_for": what_for,
                "set": key in config.settings_for(definition.name),
            }
            for key, what_for in definition.settings_used
        ],
        "missing_settings": _missing_settings_view(definition.name, config),
        "definition": _definition_view(definition.name),
        "recent_runs": [_run_view(run) for run in recent],
    }


async def _note_workflow_page(
    db: AsyncSession, client: Client, view: dict[str, Any]
) -> dict[str, Any]:
    """The workflow page for a note or report workflow: the trigger, the
    reads, the template, the mode and the approval rule, and recent notes."""
    from sqlalchemy import select as _select

    from app.automation.api.notes import note_view
    from app.models.automation import AutomationNote

    config = await load_automation_config(db, client.id)
    recent = (
        (
            await db.execute(
                _select(AutomationNote)
                .where(
                    AutomationNote.client_id == client.id,
                    AutomationNote.workflow_name == view["name"],
                )
                .order_by(AutomationNote.built_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    return {
        "client": {"slug": client.slug, "name": client.name, "kind": client.kind},
        "workflow": view,
        "triggers": view["triggers"],
        "inputs": [],
        "steps": [],
        "reads": view["reads"],
        "template": view["template"],
        "mode": view["mode"],
        "approval": {
            "rule": view["approval_rule"],
            "rules": APPROVAL_RULES,
            "approvers": [a.model_dump() for a in config.approvers],
            "window_hours": None,
            "undo_window_hours": None,
            "stale_after_days": None,
        },
        "settings_used": view["settings_used"],
        "missing_settings": view.get("missing_settings", []),
        "definition": _definition_view(view["name"]),
        "recent_runs": [],
        "recent_notes": [note_view(n) for n in recent],
    }


@router.get("/triggers/log")
async def trigger_log(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """This client's last hundred trigger evaluations, newest first."""
    client_id = scope.require_client()
    return {
        "shown": triggers.LOG_SHOWN,
        "rows": [_log_view(row) for row in await triggers.recent_log(db, client_id)],
    }


# --- runs ----------------------------------------------------------------------------


@router.get("/startable")
async def startable(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """The workflows a person may start by hand for this client right now:
    allowed on this kind of client, switched on, with a manual trigger. The
    ticket page's "Start a workflow" menu reads it; the service refuses the
    rest anyway."""
    client_id = scope.require_client()
    client = await db.get(Client, client_id)
    assert client is not None
    switches = await controls.switches_for(db, client_id)
    halted = await controls.is_halted(db, client_id)
    found = [
        {
            "name": w.name,
            "title": w.title,
            "description": w.description,
            "from_ticket": sorted(
                {source for source in w.input_sources.values() if source != "engineer"}
            ),
        }
        for w in workflows()
        if w.manual
        and w.allowed_on(client.kind) is None
        and w.name in switches
        and switches[w.name].enabled
    ]
    return {"workflows": found, "halted": halted}


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def draft_run(
    payload: DraftIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    actor = _actor(scope)
    inputs = dict(payload.inputs)
    ticket_number = payload.ticket_number
    if payload.ticket is not None:
        # Started from a ticket: the workflow's input_sources fill what the
        # ticket can, and anything given explicitly stands.
        ticket_number = payload.ticket.number
        try:
            definition = workflow(payload.workflow)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
        arrival = triggers.Arrival(
            ticket_id=payload.ticket.number,
            ticket_number=payload.ticket.number,
            title=payload.ticket.title or "",
            contact=payload.ticket.contact,
            company=payload.ticket.company,
            site_slug=payload.ticket.site,
        )
        inputs = {**triggers.inputs_from(definition, arrival), **inputs}
    try:
        run = await service.draft(
            db,
            actor,
            workflow_name=payload.workflow,
            inputs=inputs,
            ticket_number=ticket_number,
            now=dt.datetime.now(dt.UTC),
        )
    except service.Refused as exc:
        raise _refused(exc) from None
    await db.commit()
    return _run_view(run)


@router.get("/runs/{run_id}")
async def read_run(
    run_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    try:
        run = await service.get_run(db, scope.require_client(), run_id)
    except service.Refused as exc:
        raise _refused(exc) from None
    steps = await service.steps_of(db, run)
    config = await load_automation_config(db, run.client_id)
    return {
        "run": _run_view(run),
        "steps": [_step_view(row) for row in steps],
        "workflow": _workflow_view(run.workflow_name),
        "can": _what_can_be_done(run, scope, now=now),
        "approvers": [a.model_dump() for a in config.approvers],
        "me": {"user_id": str(scope.user.id), "email": scope.user.email},
    }


async def _act(
    db: AsyncSession,
    scope: Scope,
    run_id: uuid.UUID,
    do: Any,
) -> dict[str, Any]:
    """Load the run under its client, do one thing to it, commit, and return
    the run page's payload so the page redraws from what was written."""
    actor = _actor(scope)
    now = dt.datetime.now(dt.UTC)
    try:
        run = await service.get_run(db, actor.client_id, run_id)
        await do(run, actor, now)
    except service.Refused as exc:
        raise _refused(exc) from None
    await db.commit()
    return await read_run(run_id, db, scope)


@router.post("/runs/{run_id}/dry-run")
async def dry_run(
    run_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    async def do(run: AutomationRun, actor: service.Actor, now: dt.datetime) -> None:
        await service.dry_run(db, actor, run, now=now)

    return await _act(db, scope, run_id, do)


@router.post("/runs/{run_id}/submit")
async def submit(
    run_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    async def do(run: AutomationRun, actor: service.Actor, now: dt.datetime) -> None:
        await service.submit(db, actor, run, now=now)

    return await _act(db, scope, run_id, do)


@router.post("/runs/{run_id}/withdraw")
async def withdraw(
    run_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    async def do(run: AutomationRun, actor: service.Actor, now: dt.datetime) -> None:
        await service.withdraw(db, actor, run, now=now)

    return await _act(db, scope, run_id, do)


@router.post("/runs/{run_id}/approve")
async def approve(
    run_id: uuid.UUID,
    payload: ApproveIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    async def do(run: AutomationRun, actor: service.Actor, now: dt.datetime) -> None:
        await service.approve(
            db,
            actor,
            run,
            authoriser_name=payload.authoriser_name,
            authoriser_method=payload.authoriser_method,
            authoriser_at=payload.authoriser_at,
            authoriser_ref=payload.authoriser_ref,
            now=now,
        )

    return await _act(db, scope, run_id, do)


@router.post("/runs/{run_id}/cancel")
async def cancel(
    run_id: uuid.UUID,
    payload: ReasonIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    async def do(run: AutomationRun, actor: service.Actor, now: dt.datetime) -> None:
        await service.cancel(db, actor, run, reason=payload.reason, now=now)

    return await _act(db, scope, run_id, do)


@router.post("/runs/{run_id}/resume")
async def resume(
    run_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    async def do(run: AutomationRun, actor: service.Actor, now: dt.datetime) -> None:
        await service.resume(db, actor, run, now=now)

    return await _act(db, scope, run_id, do)


@router.post("/runs/{run_id}/resubmit")
async def resubmit(
    run_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    async def do(run: AutomationRun, actor: service.Actor, now: dt.datetime) -> None:
        await service.resubmit(db, actor, run, now=now)

    return await _act(db, scope, run_id, do)


@router.post("/runs/{run_id}/abandon")
async def abandon(
    run_id: uuid.UUID,
    payload: ReasonIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    async def do(run: AutomationRun, actor: service.Actor, now: dt.datetime) -> None:
        await service.abandon(db, actor, run, reason=payload.reason, now=now)

    return await _act(db, scope, run_id, do)


@router.post("/runs/{run_id}/undo")
async def undo(
    run_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    async def do(run: AutomationRun, actor: service.Actor, now: dt.datetime) -> None:
        await service.request_undo(db, actor, run, now=now)

    return await _act(db, scope, run_id, do)

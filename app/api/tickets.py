"""/tickets/*: the open tickets, and a workspace for working one of them.

The page's JSON is `/tickets/list`, not `/tickets`: that path is the page.
Everything here is `CAN_CHAT` — Inputiv staff and a franchisee's
administrator, the roles that may make the engine call a model on the
customer's data — because a workspace generates a plan. A viewer has no
nav entry and is refused (audited) at the door. The client comes from the
session (hard rule 1); a workspace or plan of another client is a 404.

Read-only to every vendor: the workspace reads a ticket, its history, the
device's state and the documentation, and writes only our own tables
(`app/models/ticket.py`). Every vendor read is a `tool_call` row; every
generation, device choice, review and outcome is its own audit row.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import ModelNotConfigured, get_model_client
from app.audit.recorder import AuditContext
from app.auth.dependencies import CAN_CHAT
from app.auth.scope import Scope
from app.connectors.errors import ConnectorError, ConnectorNotConfigured
from app.db.session import get_session
from app.models.device import Device
from app.models.enums import Interface
from app.models.site import Site
from app.models.ticket import TicketPlan, TicketWorkspace
from app.tickets import statistics
from app.tickets import workspace as service
from app.tickets.plan import PlanUnavailable, generate_plan

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _context(scope: Scope) -> AuditContext:
    return AuditContext.new(
        actor_id=scope.actor_id,
        actor_user_id=scope.user_id,
        interface=Interface.API,
        client_id=scope.require_client(),
        client_ref=scope.client_slug,
    )


def _vendor_error(exc: ConnectorError) -> HTTPException:
    if isinstance(exc, ConnectorNotConfigured):
        return HTTPException(status_code=409, detail=f"not configured for this client: {exc}")
    return HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")


async def _workspace(db: AsyncSession, scope: Scope, workspace_id: uuid.UUID) -> TicketWorkspace:
    row = (
        await db.execute(
            select(TicketWorkspace).where(
                TicketWorkspace.id == workspace_id,
                TicketWorkspace.client_id == scope.require_client(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no such workspace for this client")
    return row


async def _plan(db: AsyncSession, workspace: TicketWorkspace, version: int) -> TicketPlan:
    row = (
        await db.execute(
            select(TicketPlan).where(
                TicketPlan.workspace_id == workspace.id, TicketPlan.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"this ticket has no plan v{version}")
    return row


def _plan_view(plan: TicketPlan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "version": plan.version,
        "author": plan.author,
        "generated_at": plan.generated_at,
        "generation_ms": plan.generation_ms,
        "model": plan.model,
        "device_id": str(plan.device_id) if plan.device_id else None,
        "steps": plan.steps,
        "dropped": plan.dropped,
        "problems": plan.problems,
        "outcome": plan.outcome,
        "evidence": {
            "counts": {
                kind: sum(1 for e in plan.evidence.get("items", []) if e.get("kind") == kind)
                for kind in ("resolved_step", "ticket", "fact")
            },
            "queries": plan.evidence.get("queries", []),
            "urls": plan.evidence.get("urls", []),
            "enrichment_as_of": plan.evidence.get("enrichment_as_of"),
        },
        "citations": sum(len(step.get("citations", [])) for step in plan.steps),
    }


async def _view(db: AsyncSession, scope: Scope, workspace: TicketWorkspace) -> dict[str, Any]:
    site = await db.get(Site, workspace.site_id) if workspace.site_id else None
    device = await db.get(Device, workspace.device_id) if workspace.device_id else None
    plans = (
        (
            await db.execute(
                select(TicketPlan)
                .where(TicketPlan.workspace_id == workspace.id)
                .order_by(TicketPlan.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "id": str(workspace.id),
        "ticket": {
            # The vendor's own id as well as the number a person reads: the
            # suggestions endpoint is keyed by id, as every autopilot table is.
            "ticket_id": workspace.ticket_id,
            "ticket_number": workspace.ticket_number,
            "title": workspace.title,
            "description": workspace.description,
            "contact": workspace.contact,
            "status": workspace.status,
            "priority": workspace.priority,
            "assigned_to": workspace.assigned_to,
            "created_at": workspace.ticket_created_at,
            "scrubbed": workspace.scrubbed,
            "status_checked_at": workspace.status_checked_at,
            "closed_at": workspace.closed_at,
            "site": site.name if site else None,
            "site_slug": site.slug if site else None,
        },
        "device": {
            "device_id": str(device.id) if device else None,
            "device": device.canonical_name if device else None,
            "status": device.status if device else None,
            "method": workspace.device_method,
            "confidence": workspace.device_confidence,
            "candidates": workspace.device_candidates,
            "hostnames": workspace.enrichment.get("hostnames", []),
            "chosen_at": workspace.device_chosen_at,
            "note": workspace.device_note,
        },
        "enrichment": {
            k: v for k, v in workspace.enrichment.items() if k in ("as_of", "facts", "errors")
        },
        "plans": [_plan_view(plan) for plan in plans],
        "may_generate": True,
    }


# --- the list ------------------------------------------------------------------------


@router.get("/list")
async def list_tickets(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """The client's open Autotask tickets, oldest first, with SLA state and workspaces."""
    try:
        data = await service.list_open(db, _context(scope), scope.require_client())
    except ConnectorError as exc:
        await db.commit()
        raise _vendor_error(exc) from exc
    await db.commit()
    return data


@router.get("/statistics")
async def ticket_statistics(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """How much is open, how urgent it is and whose it is, with every figure's
    definition beside it and the time it was worked out.

    Read-only: two Autotask listings, audited as one `tool_call`, and kept for a
    few minutes so a page somebody leaves open is not a vendor read a second.
    """
    try:
        return await statistics.statistics(
            db, _context(scope), scope.require_client(), client_name=scope.client_name
        )
    except ConnectorError as exc:
        raise _vendor_error(exc) from exc
    finally:
        await db.commit()  # the tool_call row, whether it succeeded or not


@router.get("/pending")
async def pending(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> list[dict[str, Any]]:
    """Closed tickets whose plan this user wrote and has not yet marked."""
    rows = await service.pending_outcomes(db, scope.require_client(), user_id=scope.user_id)
    return [
        {
            "workspace_id": str(workspace.id),
            "ticket_number": workspace.ticket_number,
            "title": workspace.title,
            "closed_at": workspace.closed_at,
            "version": plan.version,
            "steps": [
                {"id": s["id"], "text": s.get("review", {}).get("text") or s["text"]}
                for s in plan.steps
            ],
        }
        for workspace, plan in rows
    ]


@router.post("/poll")
async def poll(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """Ask Autotask about every open workspace with a plan; say which closed."""
    try:
        closed = await service.poll_statuses(db, _context(scope), scope.require_client())
    except ConnectorError as exc:
        await db.commit()
        raise _vendor_error(exc) from exc
    await db.commit()
    return {"closed": [str(w.id) for w in closed]}


# --- a workspace ---------------------------------------------------------------------


class OpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_number: str = Field(min_length=1)


@router.post("/workspaces", status_code=201)
async def open_workspace(
    body: OpenRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """Open (or refresh) the workspace for one ticket: read it, scrub it, find the device,
    and compute the enrichment panel."""
    context = _context(scope)
    try:
        opened = await service.open_workspace(
            db,
            context,
            client_id=scope.require_client(),
            ticket_number=body.ticket_number.strip(),
            opened_by_user_id=scope.user_id,
        )
    except service.WorkspaceError as exc:
        await db.commit()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConnectorError as exc:
        await db.commit()
        raise _vendor_error(exc) from exc
    await service.refresh_enrichment(db, context, opened.workspace)
    await db.commit()
    return await _view(db, scope, opened.workspace)


@router.get("/workspaces/{workspace_id}")
async def get_workspace(
    workspace_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    return await _view(db, scope, await _workspace(db, scope, workspace_id))


@router.post("/workspaces/{workspace_id}/enrich")
async def enrich_workspace(
    workspace_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """Recompute the enrichment panel as of now."""
    workspace = await _workspace(db, scope, workspace_id)
    await service.refresh_enrichment(db, _context(scope), workspace)
    await db.commit()
    return await _view(db, scope, workspace)


@router.get("/workspaces/{workspace_id}/devices")
async def choosable_devices(
    workspace_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> list[dict[str, Any]]:
    """The machines at the ticket's site an engineer may choose from."""
    workspace = await _workspace(db, scope, workspace_id)
    query = select(Device).where(
        Device.client_id == scope.require_client(), Device.status != "retired"
    )
    if workspace.site_id is not None:
        query = query.where(Device.site_id == workspace.site_id)
    rows = (await db.execute(query.order_by(Device.canonical_name))).scalars().all()
    return [{"device_id": str(d.id), "device": d.canonical_name, "status": d.status} for d in rows]


class DeviceChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: uuid.UUID | None = Field(
        default=None, description="None: the ticket is about no machine."
    )
    note: str | None = None


@router.post("/workspaces/{workspace_id}/device")
async def choose_device(
    workspace_id: uuid.UUID,
    body: DeviceChoice,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """The engineer chooses or corrects the device. Recorded and audited."""
    workspace = await _workspace(db, scope, workspace_id)
    try:
        await service.choose_device(
            db,
            _context(scope),
            workspace,
            device_id=body.device_id,
            note=body.note,
            user_id=scope.user_id,
        )
    except service.WorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()
    return await _view(db, scope, workspace)


# --- plans ---------------------------------------------------------------------------


@router.post("/workspaces/{workspace_id}/plans", status_code=201)
async def generate(
    workspace_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """Generate a new plan version from the current evidence. Audited."""
    workspace = await _workspace(db, scope, workspace_id)
    context = _context(scope)
    if not workspace.enrichment.get("facts") and workspace.device_id is not None:
        await service.refresh_enrichment(db, context, workspace)
    device = await db.get(Device, workspace.device_id) if workspace.device_id else None
    site = await db.get(Site, workspace.site_id) if workspace.site_id else None
    try:
        model_client = get_model_client()
    except ModelNotConfigured:
        model_client = None
    try:
        generation = await generate_plan(
            db,
            context,
            workspace,
            author=scope.user.email,
            author_user_id=scope.user_id,
            device_name=device.canonical_name if device else None,
            site_name=site.name if site else None,
            client_name=scope.client_name,
            model_client=model_client,
        )
    except PlanUnavailable as exc:
        await db.commit()  # the audit rows of the attempt
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await db.commit()
    return {
        **_plan_view(generation.plan),
        "steps_written": generation.steps_written,
        "web_searches": generation.queries,
    }


class StepReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(description="accepted, edited or rejected")
    text: str | None = Field(default=None, description="The new wording, for an edit.")


@router.post("/workspaces/{workspace_id}/plans/{version}/steps/{step_id}/review")
async def review(
    workspace_id: uuid.UUID,
    version: int,
    step_id: str,
    body: StepReview,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """Accept, edit or reject one step. Audited."""
    workspace = await _workspace(db, scope, workspace_id)
    plan = await _plan(db, workspace, version)
    try:
        step = await service.review_step(
            db,
            _context(scope),
            plan,
            step_id=step_id,
            status=body.status,
            text=body.text,
            reviewer=scope.user.email,
            user_id=scope.user_id,
        )
    except service.WorkspaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return step


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved_steps: list[str] = Field(default_factory=list)
    none: bool = Field(default=False, description="No step resolved it.")
    note: str | None = None


@router.post("/workspaces/{workspace_id}/plans/{version}/outcome")
async def outcome(
    workspace_id: uuid.UUID,
    version: int,
    body: Outcome,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> dict[str, Any]:
    """Which steps resolved the ticket, from its engineer. Stored against the plan; audited."""
    workspace = await _workspace(db, scope, workspace_id)
    plan = await _plan(db, workspace, version)
    try:
        await service.record_outcome(
            db,
            _context(scope),
            plan,
            resolved_steps=body.resolved_steps,
            none_helped=body.none,
            note=body.note,
            by=scope.user.email,
            user_id=scope.user_id,
        )
    except service.WorkspaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return _plan_view(plan)

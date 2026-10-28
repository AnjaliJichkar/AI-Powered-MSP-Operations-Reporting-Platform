"""Opening a ticket, keeping it current, and closing the loop when it closes.

- `list_open` is the Tickets page: the client's open Autotask tickets with
  number, title, site, priority, age, assignee and SLA state, and whether a
  workspace exists for each. One vendor read, audited.
- `open_workspace` reads one ticket (`get_ticket`), scrubs its text, stores
  the workspace row and runs the device resolution; nothing on the ticket is
  written.
- `refresh_enrichment` recomputes the facts panel as of now.
- `choose_device` records an engineer's choice, audited.
- `poll_statuses` asks Autotask about every open workspace that has a plan
  and stamps `closed_at` on the ones that have closed; `pending_outcomes` is
  what the page then prompts the plan's engineer with, and `record_outcome`
  stores the answer against the plan, audited.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.connectors.autotask.client import AutotaskClient
from app.connectors.autotask.tools import get_ticket
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for
from app.connectors.matching import normalise_hostname
from app.connectors.ninjaone.tools import list_logged_on_users
from app.models.device import Device, DeviceVendorLink
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName
from app.models.site import Site
from app.models.ticket import TicketPlan, TicketWorkspace
from app.resolved.sla import calculator_for
from app.tickets import resolve
from app.tickets.enrich import Enrichment, audited_read, enrich
from app.tickets.scrub import scrub

REVIEW_STATUSES = ("proposed", "accepted", "edited", "rejected")


class WorkspaceError(ValueError):
    """A request the workspace cannot honour: no such ticket, plan or step."""


# --- the list -------------------------------------------------------------------------


async def list_open(
    session: AsyncSession, context: AuditContext, client_id: uuid.UUID
) -> dict[str, Any]:
    """The client's open tickets, oldest first, each saying whether a workspace exists.

    The SLA state on every row is computed from the franchisee's own targets and
    business hours (`app/resolved/sla.py`), not from whatever due times Autotask
    happened to carry: on the pilot tenant those are populated on two thirds of
    tickets and `serviceLevelAgreementHasBeenMet` on none of them, so a breach
    count drawn from them counts the tickets somebody remembered to configure.
    """
    now = dt.datetime.now(dt.UTC)
    calculator = await calculator_for(session, client_id)

    async def read() -> Any:
        async with connector_for(client_id, ConnectorName.AUTOTASK) as built:
            connector = cast(AutotaskClient, built)
            rows, truncated = await connector.list_tickets(open_only=True)
            ids = [t.assigned_resource_id for _, t in rows if t.assigned_resource_id]
            resources = await connector.get_resources(ids)
            out = []
            for site, ticket in rows:
                # The labels, not the numbers: a priority number means different
                # things in different Autotask databases, and the targets are
                # configured against the tenant's own labels.
                priority = await connector.label_for("Tickets", "priority", ticket.priority)
                status = await connector.label_for("Tickets", "status", ticket.status)
                out.append(
                    {
                        "ticket_id": str(ticket.id),
                        "ticket_number": ticket.ticket_number,
                        "title": scrub(ticket.title).text,
                        "site": site.name,
                        "site_slug": site.slug,
                        "priority": priority,
                        "status": status,
                        "assigned_to": (
                            resources[ticket.assigned_resource_id].name
                            if ticket.assigned_resource_id in resources
                            else None
                        ),
                        "created_at": ticket.create_date,
                        "age_days": round(ticket.age_days(now=now) or 0.0, 1),
                        "sla": calculator.assess(
                            ticket, priority_label=priority, status_label=status, now=now
                        ).as_json(),
                    }
                )
            return {"tickets": out, "truncated": truncated}

    found, error = await audited_read(
        session, context, connector=ConnectorName.AUTOTASK, tool="list_open_tickets", call=read
    )
    if found is None:
        raise ConnectorError(error or "Autotask did not answer", connector="autotask")
    data: dict[str, Any] = found
    numbers = [t["ticket_number"] for t in data["tickets"] if t["ticket_number"]]
    existing: dict[str, TicketWorkspace] = {}
    if numbers:
        for row in (
            await session.execute(
                select(TicketWorkspace).where(
                    TicketWorkspace.client_id == client_id,
                    TicketWorkspace.ticket_number.in_(numbers),
                )
            )
        ).scalars():
            existing[row.ticket_number or ""] = row
    plans = await _plan_counts(session, [w.id for w in existing.values()])
    for ticket in data["tickets"]:
        workspace = existing.get(ticket["ticket_number"] or "")
        ticket["workspace_id"] = str(workspace.id) if workspace else None
        ticket["plans"] = plans.get(workspace.id, 0) if workspace else 0
        ticket["device"] = None
    data["tickets"].sort(key=lambda t: -(t["age_days"] or 0))
    data["checked_at"] = now
    return data


async def _plan_counts(
    session: AsyncSession, workspace_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not workspace_ids:
        return {}
    counts: dict[uuid.UUID, int] = {}
    for row in (
        await session.execute(
            select(TicketPlan.workspace_id).where(TicketPlan.workspace_id.in_(workspace_ids))
        )
    ).all():
        counts[row[0]] = counts.get(row[0], 0) + 1
    return counts


# --- opening --------------------------------------------------------------------------


@dataclass(slots=True)
class Opened:
    workspace: TicketWorkspace
    created: bool
    resolution: resolve.Resolution


async def _site_devices(
    session: AsyncSession, client_id: uuid.UUID, site_id: uuid.UUID | None
) -> dict[str, Device]:
    query = select(Device).where(Device.client_id == client_id, Device.status != "retired")
    if site_id is not None:
        query = query.where(Device.site_id == site_id)
    return {
        normalise_hostname(d.canonical_name): d for d in (await session.execute(query)).scalars()
    }


async def resolve_device(
    session: AsyncSession, context: AuditContext, workspace: TicketWorkspace
) -> resolve.Resolution:
    """Hostnames in the text, then the contact against last logged-on users."""
    candidates, hostnames = await resolve.by_hostname(
        session,
        workspace.client_id,
        site_id=workspace.site_id,
        texts=[workspace.title, workspace.description],
    )
    if workspace.contact and not any(c.confidence == "high" for c in candidates):
        users, _ = await audited_read(
            session,
            context,
            connector=ConnectorName.NINJAONE,
            tool="list_logged_on_users",
            call=lambda: list_logged_on_users(
                str(workspace.client_id),
                site_id=str(workspace.site_id) if workspace.site_id else None,
            ),
            payload={"purpose": "device resolution by contact"},
        )
        if users is not None:
            devices = await _site_devices(session, workspace.client_id, workspace.site_id)
            rows = [(r.device, r.user_name) for r in users.rows]
            candidates += resolve.by_contact(workspace.contact, rows, devices)
    return resolve.decide(candidates, hostnames)


async def open_workspace(
    session: AsyncSession,
    context: AuditContext,
    *,
    client_id: uuid.UUID,
    ticket_number: str,
    opened_by_user_id: uuid.UUID | None,
    now: dt.datetime | None = None,
) -> Opened:
    """Read the ticket, scrub it, store or refresh the workspace, and find its device."""
    now = now or dt.datetime.now(dt.UTC)
    detail, error = await audited_read(
        session,
        context,
        connector=ConnectorName.AUTOTASK,
        tool="get_ticket",
        call=lambda: get_ticket(str(client_id), ticket_number),
        payload={"purpose": "open workspace"},
    )
    if detail is None:
        raise ConnectorError(error or "Autotask did not answer", connector="autotask")
    d: Any = detail
    if not d.found or d.ticket is None:
        raise WorkspaceError(
            d.message or f"Autotask has no ticket {ticket_number!r} for this client"
        )
    summary = d.ticket
    site = (
        await session.execute(
            select(Site).where(Site.client_id == client_id, Site.slug == summary.site_slug)
        )
    ).scalar_one_or_none()
    (title, description), scrubbed = _scrub_pair(summary.title, d.description)

    workspace = (
        await session.execute(
            select(TicketWorkspace).where(
                TicketWorkspace.client_id == client_id, TicketWorkspace.ticket_id == ticket_number
            )
        )
    ).scalar_one_or_none()
    created = workspace is None
    if workspace is None:
        workspace = TicketWorkspace(
            client_id=client_id,
            ticket_id=ticket_number,
            opened_by_user_id=opened_by_user_id,
        )
        session.add(workspace)
    workspace.site_id = site.id if site else None
    workspace.ticket_number = summary.ticket_number or ticket_number
    workspace.title = title
    workspace.description = description
    workspace.contact = d.contact
    workspace.contact_email = (d.contact_email or "").strip().lower() or None
    workspace.status = summary.status
    workspace.priority = summary.priority
    workspace.assigned_to = summary.assigned_to
    workspace.ticket_created_at = summary.created_at
    workspace.scrubbed = scrubbed
    workspace.status_checked_at = now
    if not summary.is_open and workspace.closed_at is None:
        workspace.closed_at = now
    await session.flush()

    resolution = await resolve_device(session, context, workspace)
    workspace.device_candidates = [c.as_dict() for c in resolution.candidates]
    workspace.enrichment = {**workspace.enrichment, "hostnames": resolution.hostnames}
    # An engineer's choice stands; the resolution fills in only where nobody chose.
    if workspace.device_chosen_by_user_id is None:
        workspace.device_id = resolution.device_id
        workspace.device_method = resolution.method
        workspace.device_confidence = resolution.confidence
    await session.flush()
    return Opened(workspace=workspace, created=created, resolution=resolution)


def _scrub_pair(title: str | None, description: str | None) -> tuple[tuple[str, str], int]:
    a, b = scrub(title), scrub(description)
    return (a.text, b.text), a.count + b.count


async def workspace_links(
    session: AsyncSession, device_id: uuid.UUID | None
) -> list[DeviceVendorLink]:
    if device_id is None:
        return []
    return list(
        (
            await session.execute(
                select(DeviceVendorLink).where(
                    DeviceVendorLink.device_id == device_id, DeviceVendorLink.active.is_(True)
                )
            )
        ).scalars()
    )


async def refresh_enrichment(
    session: AsyncSession,
    context: AuditContext,
    workspace: TicketWorkspace,
    *,
    now: dt.datetime | None = None,
) -> Enrichment:
    device = await session.get(Device, workspace.device_id) if workspace.device_id else None
    links = await workspace_links(session, workspace.device_id)
    result = await enrich(
        session,
        context,
        client_id=workspace.client_id,
        device=device,
        links=links,
        contact=workspace.contact,
        contact_email=workspace.contact_email,
        site_id=workspace.site_id,
        ticket_number=workspace.ticket_number,
        now=now,
    )
    workspace.enrichment = {
        **result.as_dict(),
        "hostnames": workspace.enrichment.get("hostnames", []),
    }
    workspace.enriched_at = result.as_of
    await session.flush()
    return result


# --- the engineer's decisions ---------------------------------------------------------


async def choose_device(
    session: AsyncSession,
    context: AuditContext,
    workspace: TicketWorkspace,
    *,
    device_id: uuid.UUID | None,
    note: str | None,
    user_id: uuid.UUID,
    now: dt.datetime | None = None,
) -> TicketWorkspace:
    """The engineer says which machine the ticket is about (or that it is none). Audited."""
    now = now or dt.datetime.now(dt.UTC)
    device: Device | None = None
    if device_id is not None:
        device = (
            await session.execute(
                select(Device).where(
                    Device.id == device_id, Device.client_id == workspace.client_id
                )
            )
        ).scalar_one_or_none()
        if device is None:
            raise WorkspaceError("no such device for this client")
    previous = str(workspace.device_id) if workspace.device_id else None
    workspace.device_id = device.id if device else None
    workspace.device_method = "engineer"
    workspace.device_confidence = "high" if device else None
    workspace.device_chosen_by_user_id = user_id
    workspace.device_chosen_at = now
    workspace.device_note = " ".join((note or "").split()) or None
    await session.flush()
    await record_event(
        session,
        context,
        event_type=AuditEventType.TICKET_DEVICE_CHOSEN,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "workspace_id": str(workspace.id),
            "ticket_number": workspace.ticket_number,
            "device_id": str(device.id) if device else None,
            "previous_device_id": previous,
            "note": workspace.device_note,
        },
    )
    return workspace


async def review_step(
    session: AsyncSession,
    context: AuditContext,
    plan: TicketPlan,
    *,
    step_id: str,
    status: str,
    text: str | None,
    reviewer: str,
    user_id: uuid.UUID,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Accept, edit or reject one step of one plan version. The step keeps its text;
    an edit is stored beside it with the reviewer. Audited."""
    now = now or dt.datetime.now(dt.UTC)
    if status not in ("accepted", "edited", "rejected"):
        raise WorkspaceError("a review is accepted, edited or rejected")
    steps = [dict(step) for step in plan.steps]
    step = next((s for s in steps if s.get("id") == step_id), None)
    if step is None:
        raise WorkspaceError(f"plan v{plan.version} has no step {step_id!r}")
    edited = " ".join((text or "").split()) or None
    if status == "edited" and not edited:
        raise WorkspaceError("an edit needs the new text")
    step["review"] = {
        "status": status,
        "text": edited if status == "edited" else None,
        "by": reviewer,
        "at": now.isoformat(),
    }
    plan.steps = steps
    await session.flush()
    await record_event(
        session,
        context,
        event_type=AuditEventType.TICKET_PLAN_REVIEWED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "workspace_id": str(plan.workspace_id),
            "plan_id": plan.id,
            "version": plan.version,
            "step": step_id,
            "status": status,
            "text": edited,
        },
    )
    return step


# --- outcomes -------------------------------------------------------------------------


async def poll_statuses(
    session: AsyncSession,
    context: AuditContext,
    client_id: uuid.UUID,
    *,
    now: dt.datetime | None = None,
) -> list[TicketWorkspace]:
    """Ask Autotask about every open workspace with a plan; stamp the ones that closed."""
    now = now or dt.datetime.now(dt.UTC)
    with_plans = select(TicketPlan.workspace_id).where(TicketPlan.client_id == client_id)
    rows = (
        (
            await session.execute(
                select(TicketWorkspace).where(
                    TicketWorkspace.client_id == client_id,
                    TicketWorkspace.closed_at.is_(None),
                    TicketWorkspace.id.in_(with_plans),
                )
            )
        )
        .scalars()
        .all()
    )
    closed: list[TicketWorkspace] = []
    for workspace in rows:
        number = workspace.ticket_number or workspace.ticket_id
        detail, _ = await audited_read(
            session,
            context,
            connector=ConnectorName.AUTOTASK,
            tool="get_ticket",
            call=partial(get_ticket, str(client_id), number),
            payload={"purpose": "poll status"},
        )
        if detail is None or not getattr(detail, "found", False):
            continue
        summary: Any = detail.ticket
        workspace.status = summary.status
        workspace.status_checked_at = now
        if not summary.is_open:
            workspace.closed_at = now
            closed.append(workspace)
    await session.flush()
    return closed


async def pending_outcomes(
    session: AsyncSession, client_id: uuid.UUID, *, user_id: uuid.UUID
) -> list[tuple[TicketWorkspace, TicketPlan]]:
    """Closed workspaces whose latest plan this user wrote and has not yet marked."""
    rows = (
        await session.execute(
            select(TicketWorkspace, TicketPlan)
            .join(TicketPlan, TicketPlan.workspace_id == TicketWorkspace.id)
            .where(
                TicketWorkspace.client_id == client_id,
                TicketWorkspace.closed_at.is_not(None),
            )
            .order_by(TicketWorkspace.closed_at.desc(), TicketPlan.version.desc())
        )
    ).all()
    latest: dict[uuid.UUID, tuple[TicketWorkspace, TicketPlan]] = {}
    for workspace, plan in rows:
        latest.setdefault(workspace.id, (workspace, plan))
    return [
        (workspace, plan)
        for workspace, plan in latest.values()
        if not plan.outcome
        and (plan.author_user_id == user_id or workspace.opened_by_user_id == user_id)
    ]


async def record_outcome(
    session: AsyncSession,
    context: AuditContext,
    plan: TicketPlan,
    *,
    resolved_steps: list[str],
    none_helped: bool,
    note: str | None,
    by: str,
    user_id: uuid.UUID,
    now: dt.datetime | None = None,
) -> TicketPlan:
    """Which steps resolved the ticket, from its engineer, stored against the plan. Audited."""
    now = now or dt.datetime.now(dt.UTC)
    known = {step.get("id") for step in plan.steps}
    unknown = [s for s in resolved_steps if s not in known]
    if unknown:
        raise WorkspaceError(f"plan v{plan.version} has no step {unknown[0]!r}")
    if not resolved_steps and not none_helped:
        raise WorkspaceError("name the steps that resolved it, or say that none did")
    plan.outcome = {
        "resolved_steps": [] if none_helped else list(dict.fromkeys(resolved_steps)),
        "none": none_helped,
        "note": " ".join((note or "").split()) or None,
        "by": by,
        "by_user_id": str(user_id),
        "at": now.isoformat(),
    }
    await session.flush()
    await record_event(
        session,
        context,
        event_type=AuditEventType.TICKET_OUTCOME_RECORDED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "workspace_id": str(plan.workspace_id),
            "plan_id": plan.id,
            "version": plan.version,
            **{k: v for k, v in plan.outcome.items() if k != "by_user_id"},
        },
    )
    return plan

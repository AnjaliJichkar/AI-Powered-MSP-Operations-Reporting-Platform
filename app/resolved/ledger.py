"""Filling the outcome ledger: read Autotask, measure, write one row a ticket.

Run by the daily collector after the events and before the coverage snapshot,
and by `scripts/resolved.py ledger` for a backfill. Read-only to every vendor:
three listings and nothing written anywhere but our own table.

* **Completed tickets** in the window — the outcomes.
* **Open tickets** we already hold a completed row for — a ticket that came back
  is not an outcome, and the row says so. Only those completed inside
  `reopened_within_days`: the mark is decided by that window, so a ticket
  completed before it cannot earn one and is not asked about.
* **Time entries** against both sets, which is what the cost is computed from.

**Idempotent.** The row is keyed `(client_id, ticket_id)` and written with an
upsert, so running the collector twice changes nothing. A ticket that closes,
reopens and closes again stays one row: the reopen is remembered (`reopened`,
`reopened_at`) and the later completion overwrites the times, so the ledger
says both that it was closed and that it did not stay closed the first time.

Every figure it writes is computed by the same code the Tickets page uses
(`app/resolved/sla.py`) over the same franchisee settings, so the ledger and the
queue cannot disagree about whether something breached.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext
from app.connectors.autotask.client import AutotaskClient
from app.connectors.autotask.models import AutotaskResource, AutotaskTicket, AutotaskTimeEntry
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for
from app.connectors.scope import SiteRef
from app.db.session import session_scope
from app.models.client import Client
from app.models.enums import ConnectorName, Interface
from app.models.outcome import TicketOutcome
from app.models.ticket import TicketPlan, TicketWorkspace
from app.reports.tbr.config import FranchiseeConfig, load_config
from app.resolved.clock import franchisee_zone
from app.resolved.sla import SlaCalculator
from app.tickets.enrich import audited_read
from app.tickets.scrub import scrub

#: Autotask caps an `in` filter, so time entries are read in batches of ticket
#: ids rather than one enormous clause (docs/api-notes/autotask.md).
TIME_ENTRY_BATCH = 400


@dataclass(frozen=True)
class LedgerRun:
    """What one pass over a window did."""

    window_days: int
    completed_seen: int
    rows_written: int
    reopened: int
    truncated: bool
    time_entries: int
    costed: int
    """Rows that got a cost. The rest had no cost book or no logged time."""


def _identifiers(resource: AutotaskResource | None) -> tuple[str | None, ...]:
    if resource is None:
        return ()
    return (resource.name, resource.user_name, resource.email)


async def _read(
    session: AsyncSession,
    context: AuditContext,
    client_id: uuid.UUID,
    *,
    since: dt.datetime,
    known_open_ids: list[int],
) -> dict[str, Any]:
    """Three listings in one connector, audited as one tool call."""

    async def call() -> dict[str, Any]:
        async with connector_for(client_id, ConnectorName.AUTOTASK) as built:
            connector = cast(AutotaskClient, built)
            completed, completed_truncated = await connector.list_tickets(
                open_only=False, completed_since=since
            )
            # Tickets we recorded as complete that are open again. Asked for by
            # id, so a ticket that reopened outside the window is still seen.
            reopened: list[tuple[SiteRef, AutotaskTicket]] = []
            if known_open_ids:
                reopened = await connector.get_tickets_by_ids(known_open_ids)
            rows = {t.id: (site, t) for site, t in [*completed, *reopened]}
            entries: list[AutotaskTimeEntry] = []
            entries_truncated = False
            ids = list(rows)
            for start in range(0, len(ids), TIME_ENTRY_BATCH):
                batch = ids[start : start + TIME_ENTRY_BATCH]
                found, truncated = await connector.list_time_entries(batch, worked_since=since)
                entries.extend(found)
                entries_truncated = entries_truncated or truncated
            assignees = [t.assigned_resource_id for _, t in rows.values() if t.assigned_resource_id]
            assignees += [e.resource_id for e in entries if e.resource_id]
            resources = await connector.get_resources(assignees)
            labels: dict[str, dict[int, str | None]] = {
                "priority": {},
                "status": {},
                "queueID": {},
                "issueType": {},
            }
            for _, ticket in rows.values():
                for field, value in (
                    ("priority", ticket.priority),
                    ("status", ticket.status),
                    ("queueID", ticket.queue_id),
                    ("issueType", ticket.issue_type),
                ):
                    if value is not None and value not in labels[field]:
                        labels[field][value] = await connector.label_for("Tickets", field, value)
            return {
                "tickets": rows,
                "entries": entries,
                "resources": resources,
                "labels": labels,
                "truncated": completed_truncated or entries_truncated,
            }

    found, error = await audited_read(
        session, context, connector=ConnectorName.AUTOTASK, tool="ticket_outcomes", call=call
    )
    if found is None:
        raise ConnectorError(error or "Autotask did not answer", connector="autotask")
    return cast(dict[str, Any], found)


async def _plans(
    session: AsyncSession, client_id: uuid.UUID, numbers: set[str]
) -> dict[str, tuple[uuid.UUID, int | None, dict[str, Any] | None]]:
    """The workspace and latest plan for each ticket number we worked on."""
    if not numbers:
        return {}
    workspaces = (
        (
            await session.execute(
                select(TicketWorkspace).where(
                    TicketWorkspace.client_id == client_id,
                    TicketWorkspace.ticket_number.in_(numbers),
                )
            )
        )
        .scalars()
        .all()
    )
    if not workspaces:
        return {}
    plans = (
        (
            await session.execute(
                select(TicketPlan)
                .where(TicketPlan.workspace_id.in_([w.id for w in workspaces]))
                .order_by(TicketPlan.workspace_id, TicketPlan.version)
            )
        )
        .scalars()
        .all()
    )
    latest: dict[uuid.UUID, TicketPlan] = {}
    for plan in plans:  # ordered by version, so the last one written wins
        latest[plan.workspace_id] = plan
    out: dict[str, tuple[uuid.UUID, int | None, dict[str, Any] | None]] = {}
    for workspace in workspaces:
        newest = latest.get(workspace.id)
        # Only an outcome somebody actually recorded: an empty `outcome` on a
        # plan nobody has marked is not "no steps helped", it is not asked yet.
        outcome = newest.outcome if newest and newest.outcome and newest.outcome.get("at") else None
        out[workspace.ticket_number or ""] = (
            workspace.id,
            newest.version if newest else None,
            outcome,
        )
    return out


async def build_ledger(
    session: AsyncSession,
    context: AuditContext,
    client_id: uuid.UUID,
    *,
    config: FranchiseeConfig,
    zone: dt.tzinfo,
    since: dt.datetime,
    now: dt.datetime | None = None,
) -> LedgerRun:
    """Measure every ticket completed since `since`, and write the rows."""
    now = now or dt.datetime.now(dt.UTC)
    resolved = config.resolved
    calculator = SlaCalculator(resolved, config.business_hours, zone)

    reopen_window = dt.timedelta(days=resolved.reopened_within_days)

    # Rows we already hold a completion for: if one of these is open again, it
    # did not stay closed.
    held = {
        row.ticket_id: row
        for row in (
            await session.execute(select(TicketOutcome).where(TicketOutcome.client_id == client_id))
        )
        .scalars()
        .all()
    }
    # **Only the ones that could still reopen**, which is the same window the
    # mark below is decided by: a ticket completed longer ago than
    # `reopened_within_days` cannot become `reopened` however it is read, so
    # asking the vendor about it is a question whose answer we would discard.
    #
    # It is also the difference between a read that settles and one that grows
    # for ever. This list was every row the ledger had ever written — 2,721
    # after one live backfill, climbing every day — and it is sent as an `in`
    # filter, so the query grew with the table until the vendor refused it
    # (api-notes, 2026-10-01: 544 conditions, HTTP 500). The condition budget
    # in the connector now splits such a filter instead of failing, so this is
    # no longer what keeps the run working; it is what keeps it proportionate.
    #
    # Nothing is lost by not asking. A reopened ticket keeps its mark whether
    # or not it is re-read (the mark is read back from the row), and when it
    # completes again that completion arrives through the completed listing
    # like any other, which is what overwrites the times and keeps the mark.
    completed_before = [
        int(ticket_id)
        for ticket_id, row in held.items()
        if row.completed_at is not None
        and ticket_id.isdigit()
        and now - row.completed_at <= reopen_window
    ]

    data = await _read(session, context, client_id, since=since, known_open_ids=completed_before)
    tickets: dict[int, tuple[SiteRef, AutotaskTicket]] = data["tickets"]
    resources: dict[int, AutotaskResource] = data["resources"]
    labels: dict[str, dict[int, str | None]] = data["labels"]

    hours: dict[int, float] = defaultdict(float)
    cost: dict[int, Decimal] = defaultdict(Decimal)
    costed_tickets: set[int] = set()
    for entry in data["entries"]:
        if entry.ticket_id is None or not entry.hours_worked:
            continue
        hours[entry.ticket_id] += entry.hours_worked
        rate = resolved.cost.hourly_for(*_identifiers(resources.get(entry.resource_id or -1)))
        if rate is not None:
            cost[entry.ticket_id] += Decimal(str(entry.hours_worked)) * rate
            costed_tickets.add(entry.ticket_id)

    numbers = {t.ticket_number for _, t in tickets.values() if t.ticket_number}
    plans = await _plans(session, client_id, numbers)

    written = 0
    reopened_count = 0
    for ticket_id, (site, ticket) in tickets.items():
        priority = labels["priority"].get(ticket.priority or -1)
        status = labels["status"].get(ticket.status or -1)
        queue = labels["queueID"].get(ticket.queue_id or -1)
        issue = labels["issueType"].get(ticket.issue_type or -1)
        sla = calculator.assess(ticket, priority_label=priority, status_label=status, now=now)
        resource = resources.get(ticket.assigned_resource_id or -1)
        previous = held.get(str(ticket_id))

        # Did it come back? Only a row we already recorded as complete can
        # reopen, and only inside the franchisee's own window.
        reopened = bool(previous and previous.reopened)
        reopened_at = previous.reopened_at if previous else None
        if (
            previous is not None
            and previous.completed_at is not None
            and ticket.completed_date is None
            and not reopened
            and now - previous.completed_at <= reopen_window
        ):
            reopened, reopened_at = True, now
            reopened_count += 1

        pickup = (
            calculator.clock.elapsed_minutes(ticket.create_date, ticket.first_response_at)
            if ticket.create_date and ticket.first_response_at
            else None
        )
        resolve = (
            calculator.clock.elapsed_minutes(ticket.create_date, ticket.completed_date)
            if ticket.create_date and ticket.completed_date
            else None
        )
        workspace_id, plan_version, plan_outcome = plans.get(
            ticket.ticket_number or "", (None, None, None)
        )
        values = {
            "client_id": client_id,
            "site_id": site.site_id,
            "ticket_id": str(ticket_id),
            "ticket_number": ticket.ticket_number,
            "title": scrub(ticket.title).text if ticket.title else None,
            "ticket_class": resolved.classes.classify(
                priority=priority, queue=queue, issue_type=issue
            ),
            "resolver": resolved.resolvers.side(*_identifiers(resource)),
            "resolver_name": resource.name if resource else None,
            "priority_label": priority,
            "status_label": status,
            "queue_label": queue,
            "issue_type_label": issue,
            "created_at_vendor": ticket.create_date,
            "first_response_at": ticket.first_response_at,
            "completed_at": ticket.completed_date,
            "pickup_minutes": pickup,
            "resolve_minutes": resolve,
            "first_response_met": sla.first_response.met,
            "resolution_met": sla.resolution.met,
            "reopened": reopened,
            "reopened_at": reopened_at,
            "hours_logged": round(hours[ticket_id], 2) if ticket_id in hours else None,
            "cost": cost[ticket_id] if ticket_id in costed_tickets else None,
            "workspace_id": workspace_id,
            "plan_version": plan_version,
            "plan_outcome": plan_outcome,
            "collected_at": now,
        }
        statement = insert(TicketOutcome).values(**values)
        await session.execute(
            statement.on_conflict_do_update(
                constraint="uq_ticket_outcomes_ticket",
                set_={k: v for k, v in values.items() if k not in ("client_id", "ticket_id")},
            )
        )
        written += 1

    return LedgerRun(
        window_days=max(1, (now - since).days),
        completed_seen=sum(1 for _, t in tickets.values() if t.completed_date is not None),
        rows_written=written,
        reopened=reopened_count,
        truncated=bool(data["truncated"]),
        time_entries=len(data["entries"]),
        costed=len(costed_tickets),
    )


async def collect_outcomes(
    client_id: uuid.UUID,
    *,
    actor: str = "collector",
    days: int = 7,
    now: dt.datetime | None = None,
) -> LedgerRun:
    """The ledger for one client, over the last `days`. Opens its own session.

    A week by default rather than a day: a ticket completed on Friday evening
    and a collector that failed over the weekend should still be measured on
    Monday, and the write is an upsert so re-measuring the same tickets costs a
    row update and changes nothing.
    """
    now = now or dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        client = await session.get(Client, client_id)
        config = await load_config(session, client_id, client_name=client.name if client else "")
        zone, _ = franchisee_zone(config.business_hours.timezone)
        context = AuditContext.new(
            actor_id=actor,
            actor_user_id=None,
            interface=Interface.CLI,
            client_id=client_id,
            client_ref=client.slug if client else None,
        )
        run = await build_ledger(
            session,
            context,
            client_id,
            config=config,
            zone=zone,
            since=now - dt.timedelta(days=days),
            now=now,
        )
        await session.commit()
        return run

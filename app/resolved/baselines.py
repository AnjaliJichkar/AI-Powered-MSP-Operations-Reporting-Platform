"""What a franchisee's service actually looks like, before we change it.

Two questions, for any franchisee and any date range:

* **How long does a ticket take?** Median and 90th percentile working minutes to
  first response and to completion, by class. The median is the typical ticket;
  the 90th is the one the customer remembers, and the gap between them is the
  thing a per-outcome service is bought to close.
* **What does a ticket cost?** Logged hours at each engineer's loaded hourly
  cost, per ticket, by class.

Read from Autotask rather than from the ledger, so a range that predates the
ledger still answers — and computed by the same classification, the same clock
and the same cost book the ledger uses, so the two can never disagree about a
ticket they both hold.

**A percentile over very few tickets is not a baseline**, so every figure says
how many tickets it was taken over and the caller decides whether to believe it.
A class nobody logged time against gets no cost rather than a zero, and a
franchisee with no cost book gets no money anywhere: a figure from a guessed
hourly rate would be worse than no figure.
"""

from __future__ import annotations

import datetime as dt
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext
from app.connectors.autotask.client import AutotaskClient
from app.connectors.autotask.models import AutotaskResource, AutotaskTicket, AutotaskTimeEntry
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for
from app.models.enums import ConnectorName
from app.reports.tbr.config import FranchiseeConfig
from app.resolved.config import TICKET_CLASSES
from app.resolved.ledger import TIME_ENTRY_BATCH, _identifiers
from app.resolved.sla import SlaCalculator
from app.tickets.enrich import audited_read


def percentile(values: list[int], share: float) -> int | None:
    """The value at `share` of the way through, nearest rank.

    Nearest rank rather than an interpolation: these are minutes on real
    tickets, and "the 90th percentile is 247 minutes" should name a ticket that
    existed rather than a number between two of them.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, min(len(ordered), int(-(-share * len(ordered) // 1))))
    return ordered[rank - 1]


@dataclass
class ClassBaseline:
    """One class of ticket, over one range."""

    ticket_class: str
    tickets: int = 0
    pickup: list[int] = field(default_factory=list)
    resolve: list[int] = field(default_factory=list)
    hours: float = 0.0
    cost: Decimal = Decimal(0)
    costed_tickets: int = 0
    timed_tickets: int = 0

    def as_json(self, *, currency: str) -> dict[str, Any]:
        costed = self.costed_tickets
        return {
            "class": self.ticket_class,
            "tickets": self.tickets,
            "pickup_minutes": {
                "measured": len(self.pickup),
                "median": percentile(self.pickup, 0.5),
                "p90": percentile(self.pickup, 0.9),
            },
            "resolve_minutes": {
                "measured": len(self.resolve),
                "median": percentile(self.resolve, 0.5),
                "p90": percentile(self.resolve, 0.9),
            },
            "hours_logged": round(self.hours, 2),
            "hours_per_ticket": (
                round(self.hours / self.timed_tickets, 2) if self.timed_tickets else None
            ),
            "tickets_with_time": self.timed_tickets,
            "cost": round(float(self.cost), 2) if costed else None,
            "cost_per_ticket": round(float(self.cost / costed), 2) if costed else None,
            "tickets_with_cost": costed,
            "currency": currency,
        }


@dataclass
class Baselines:
    """Every class, and what the range held."""

    since: dt.datetime
    until: dt.datetime
    classes: dict[str, ClassBaseline]
    tickets: int
    truncated: bool
    has_cost_book: bool

    def as_json(self, *, currency: str) -> dict[str, Any]:
        return {
            "since": self.since,
            "until": self.until,
            "tickets": self.tickets,
            "truncated": self.truncated,
            "has_cost_book": self.has_cost_book,
            "definitions": {
                "pickup_minutes": "Working minutes from the moment Autotask stamped the ticket "
                "created to the moment it stamped a first response, on this franchisee's business "
                "hours, holidays and time zone. Tickets never answered are left out.",
                "resolve_minutes": "Working minutes from created to the completed date Autotask "
                "stamped, on the same clock.",
                "median": "The middle ticket. Half took less.",
                "p90": "Nearest rank: the ticket nine tenths of the way through the sorted list, "
                "so it names a real ticket rather than a number between two of them.",
                "cost_per_ticket": "Hours logged against the ticket at each engineer's loaded "
                "hourly cost, averaged over the tickets that had both. Absent where the "
                "franchisee has no cost book.",
            },
            "by_class": [
                self.classes[name].as_json(currency=currency)
                for name in TICKET_CLASSES
                if name in self.classes
            ],
        }


async def _read(
    session: AsyncSession,
    context: AuditContext,
    client_id: uuid.UUID,
    *,
    since: dt.datetime,
    until: dt.datetime,
) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        async with connector_for(client_id, ConnectorName.AUTOTASK) as built:
            connector = cast(AutotaskClient, built)
            rows, truncated = await connector.list_tickets(
                open_only=False, completed_since=since, completed_before=until
            )
            entries: list[AutotaskTimeEntry] = []
            entry_truncated = False
            ids = [ticket.id for _, ticket in rows]
            for start in range(0, len(ids), TIME_ENTRY_BATCH):
                found, capped = await connector.list_time_entries(
                    ids[start : start + TIME_ENTRY_BATCH], worked_since=since
                )
                entries.extend(found)
                entry_truncated = entry_truncated or capped
            people = [t.assigned_resource_id for _, t in rows if t.assigned_resource_id]
            people += [e.resource_id for e in entries if e.resource_id]
            resources = await connector.get_resources(people)
            labels: dict[str, dict[int, str | None]] = {
                "priority": {},
                "status": {},
                "queueID": {},
                "issueType": {},
            }
            for _, ticket in rows:
                for field_name, value in (
                    ("priority", ticket.priority),
                    ("status", ticket.status),
                    ("queueID", ticket.queue_id),
                    ("issueType", ticket.issue_type),
                ):
                    if value is not None and value not in labels[field_name]:
                        labels[field_name][value] = await connector.label_for(
                            "Tickets", field_name, value
                        )
            return {
                "tickets": rows,
                "entries": entries,
                "resources": resources,
                "labels": labels,
                "truncated": truncated or entry_truncated,
            }

    found, error = await audited_read(
        session, context, connector=ConnectorName.AUTOTASK, tool="ticket_baselines", call=call
    )
    if found is None:
        raise ConnectorError(error or "Autotask did not answer", connector="autotask")
    return cast(dict[str, Any], found)


async def baselines(
    session: AsyncSession,
    context: AuditContext,
    client_id: uuid.UUID,
    *,
    config: FranchiseeConfig,
    zone: dt.tzinfo,
    since: dt.datetime,
    until: dt.datetime,
) -> Baselines:
    """Time and cost by class, for one franchisee over one range."""
    resolved = config.resolved
    calculator = SlaCalculator(resolved, config.business_hours, zone)
    data = await _read(session, context, client_id, since=since, until=until)
    resources: dict[int, AutotaskResource] = data["resources"]
    labels: dict[str, dict[int, str | None]] = data["labels"]

    hours: dict[int, float] = defaultdict(float)
    cost: dict[int, Decimal] = defaultdict(Decimal)
    costed: set[int] = set()
    for entry in data["entries"]:
        if entry.ticket_id is None or not entry.hours_worked:
            continue
        hours[entry.ticket_id] += entry.hours_worked
        rate = resolved.cost.hourly_for(*_identifiers(resources.get(entry.resource_id or -1)))
        if rate is not None:
            cost[entry.ticket_id] += Decimal(str(entry.hours_worked)) * rate
            costed.add(entry.ticket_id)

    by_class: dict[str, ClassBaseline] = {name: ClassBaseline(name) for name in TICKET_CLASSES}
    rows: list[tuple[Any, AutotaskTicket]] = data["tickets"]
    for _, ticket in rows:
        name = resolved.classes.classify(
            priority=labels["priority"].get(ticket.priority or -1),
            queue=labels["queueID"].get(ticket.queue_id or -1),
            issue_type=labels["issueType"].get(ticket.issue_type or -1),
        )
        bucket = by_class[name]
        bucket.tickets += 1
        if ticket.create_date and ticket.first_response_at:
            bucket.pickup.append(
                calculator.clock.elapsed_minutes(ticket.create_date, ticket.first_response_at)
            )
        if ticket.create_date and ticket.completed_date:
            bucket.resolve.append(
                calculator.clock.elapsed_minutes(ticket.create_date, ticket.completed_date)
            )
        if ticket.id in hours:
            bucket.hours += hours[ticket.id]
            bucket.timed_tickets += 1
        if ticket.id in costed:
            bucket.cost += cost[ticket.id]
            bucket.costed_tickets += 1

    return Baselines(
        since=since,
        until=until,
        classes={name: bucket for name, bucket in by_class.items() if bucket.tickets},
        tickets=len(rows),
        truncated=bool(data["truncated"]),
        has_cost_book=bool(resolved.cost.default_hourly or resolved.cost.by_resource),
    )


def mean_minutes(values: list[int]) -> float | None:
    """The arithmetic mean, for a caller that wants one beside the median."""
    return statistics.fmean(values) if values else None

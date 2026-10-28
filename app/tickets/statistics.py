"""The numbers above the ticket queue: how much is open, how urgent, whose.

Read-only, like everything else in Phase 1, and computed from two Autotask
listings and nothing else:

* every **open** ticket, which is what the queue itself is drawn from;
* every ticket **completed** inside the window, which is what closes the loop
  on the created-and-resolved chart.

Two reads cover the whole window, and the reason is worth writing down: a
ticket created inside the window either is still open (so the first read has
it) or was completed after it was created and therefore inside the window too
(so the second read has it). There is no third case, so there is no third read.

**Every figure carries its own definition**, in the payload rather than on the
page, because the definition belongs with the thing that computed it: a card
that says "6" and a page that says what 6 means, written in two files, drift.
That is the same rule the business review's facts follow.

**The answer is kept for a few minutes** (`TICKET_STATISTICS_TTL_SECONDS`).
The queue is a page somebody leaves open on a second monitor, and without this
every reload of it would be two tenant-wide Autotask listings. The cache is in
memory and therefore per process — written down rather than hidden, the same as
the sign-in rate limit — and is keyed by client, never shared across clients.
One lock per client single-flights a cold read, so ten people opening the page
at once make one pair of vendor calls and not twenty.

Nothing here decides who may see it: `/tickets/statistics` resolves the session
and passes a `client_id` in, like every other endpoint (hard rule 1).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections import Counter
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext
from app.connectors.autotask.client import AutotaskClient
from app.connectors.autotask.models import AutotaskTicket
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for
from app.connectors.scope import SiteRef
from app.models.enums import ConnectorName
from app.reports.tbr.config import load_config
from app.resolved.clock import franchisee_zone
from app.resolved.sla import SlaCalculator
from app.settings import get_settings
from app.tickets.enrich import audited_read
from app.tickets.scrub import scrub

#: A ticket is "due very soon" inside this. The queue's own badge uses the same
#: four hours, so the card and the rows it filters to cannot disagree.
DUE_SOON = dt.timedelta(hours=4)

#: How many customers the by-customer chart names. The rest are one bar, so the
#: chart still adds up to the open count rather than quietly dropping the tail.
TOP_CUSTOMERS = 5

#: Whoever a ticket is not assigned to, on the per-technician chart.
NOBODY = "Unassigned"

#: The filters the queue draws chips for. A card names one of these, or `none`
#: (the unfiltered queue) or `resolved_week` (a list of its own, because those
#: tickets are closed and the queue holds open ones). Named here rather than
#: only in the page, so `tests/api/test_ticket_statistics.py` can prove a card
#: never points at a filter nothing implements — which is exactly the drift
#: that makes a figure and the rows it opens two different populations.
QUEUE_FILTERS = ("breached", "four_hours", "unassigned", "created_today", "paused")
CARD_FILTERS = ("none", "resolved_week", *QUEUE_FILTERS)


class Statistics:
    """One client's figures, and when they were worked out."""

    __slots__ = ("computed_at", "payload")

    def __init__(self, computed_at: dt.datetime, payload: dict[str, Any]) -> None:
        self.computed_at = computed_at
        self.payload = payload


#: Per client: the last answer, and a lock so a cold read happens once.
_CACHE: dict[uuid.UUID, Statistics] = {}
_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}


def forget(client_id: uuid.UUID | None = None) -> None:
    """Drop the cache, for a test or for a client whose credentials changed."""
    if client_id is None:
        _CACHE.clear()
        _LOCKS.clear()
    else:
        _CACHE.pop(client_id, None)
        _LOCKS.pop(client_id, None)


# --- the window a day belongs to -------------------------------------------------------


def _day_start(moment: dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    local = moment.astimezone(zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(moment: dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    start = _day_start(moment, zone)
    return start - dt.timedelta(days=start.weekday())


# --- the two reads ---------------------------------------------------------------------


Row = tuple[SiteRef, AutotaskTicket]


async def _read_tickets(
    session: AsyncSession, context: AuditContext, client_id: uuid.UUID, *, since: dt.datetime
) -> dict[str, Any]:
    """Open tickets and tickets completed since `since`, with the names they need.

    One connector, one `async with`, so the two listings share a token and a
    session rather than authenticating twice.
    """

    async def read() -> dict[str, Any]:
        async with connector_for(client_id, ConnectorName.AUTOTASK) as built:
            connector = cast(AutotaskClient, built)
            open_rows, open_truncated = await connector.list_tickets(open_only=True)
            done_rows, done_truncated = await connector.list_tickets(
                open_only=False, completed_since=since
            )
            assignees = [
                ticket.assigned_resource_id
                for _, ticket in [*open_rows, *done_rows]
                if ticket.assigned_resource_id
            ]
            resources = await connector.get_resources(assignees)
            priorities = {
                ticket.priority: await connector.label_for("Tickets", "priority", ticket.priority)
                for _, ticket in open_rows
            }
            # Statuses too, because the SLA clock stops in some of them and the
            # franchisee names which by label, never by number.
            statuses = {
                ticket.status: await connector.label_for("Tickets", "status", ticket.status)
                for _, ticket in open_rows
            }
            return {
                "open": open_rows,
                "done": done_rows,
                "truncated": open_truncated or done_truncated,
                "resources": {rid: resource.name for rid, resource in resources.items()},
                "priorities": priorities,
                "statuses": statuses,
            }

    found, error = await audited_read(
        session,
        context,
        connector=ConnectorName.AUTOTASK,
        tool="ticket_statistics",
        call=read,
    )
    if found is None:
        raise ConnectorError(error or "Autotask did not answer", connector="autotask")
    return cast(dict[str, Any], found)


# --- the figures ------------------------------------------------------------------------


def _card(
    key: str,
    label: str,
    value: int,
    definition: str,
    *,
    tone: str = "",
    filter_key: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": value,
        "definition": definition,
        "tone": tone,
        "filter": filter_key,
    }


def _series(label: str, value: int, note: str = "") -> dict[str, Any]:
    return {"label": label, "value": value, "note": note}


def _assignee(ticket: AutotaskTicket, resources: dict[int, str]) -> str:
    if not ticket.assigned_resource_id:
        return NOBODY
    return resources.get(ticket.assigned_resource_id) or NOBODY


def build(
    data: dict[str, Any],
    *,
    now: dt.datetime,
    zone: dt.tzinfo,
    zone_name: str,
    window_days: int,
    calculator: SlaCalculator,
    max_resolved: int = 200,
) -> dict[str, Any]:
    """The payload, from the two listings.

    No I/O and no settings lookup: this is the arithmetic, so a test can hand it
    recorded rows and assert every number without a database, a vendor or a
    frozen settings object in the way. The SLA calculator is passed in for the
    same reason — it is the franchisee's targets and clock, built once by the
    caller from their settings.
    """
    open_rows: list[Row] = data["open"]
    done_rows: list[Row] = data["done"]
    resources: dict[int, str] = data["resources"]
    priorities: dict[Any, str | None] = data["priorities"]

    today = _day_start(now, zone)
    week = _week_start(now, zone)
    window_start = today - dt.timedelta(days=window_days - 1)

    statuses: dict[Any, str | None] = data.get("statuses", {})
    states = [
        calculator.assess(
            ticket,
            priority_label=priorities.get(ticket.priority),
            status_label=statuses.get(ticket.status),
            now=now,
        )
        for _, ticket in open_rows
    ]
    breached = sum(1 for state in states if state.state == "breached")
    due_soon = sum(
        1
        for state in states
        if state.state != "breached" and state.due_at is not None and state.due_at - now <= DUE_SOON
    )
    paused = sum(1 for state in states if state.state == "paused")
    unassigned = sum(1 for _, ticket in open_rows if not ticket.assigned_resource_id)

    # Created inside the window: still open, or completed inside it. A ticket
    # created inside the window and completed cannot have been completed before
    # it, so these two lists are the whole population and there is no third read.
    created_by_day: Counter[str] = Counter()
    created_today = 0
    # A ticket completed inside the window is in both listings when it is also
    # still open, which it cannot be — but a tenant that returns a row twice
    # would double a bar, so each id is counted once whatever the vendor sent.
    counted: set[int] = set()
    for _, ticket in [*open_rows, *done_rows]:
        if ticket.id in counted or ticket.create_date is None:
            continue
        counted.add(ticket.id)
        created = ticket.create_date.astimezone(zone)
        if created >= window_start:
            created_by_day[created.date().isoformat()] += 1
        if created >= today:
            created_today += 1

    resolved_by_day: Counter[str] = Counter()
    resolved_week = 0
    resolved_rows: list[dict[str, Any]] = []
    for site, ticket in done_rows:
        completed = ticket.completed_date
        if completed is None:
            continue
        local = completed.astimezone(zone)
        if local >= window_start:
            resolved_by_day[local.date().isoformat()] += 1
        if local >= week:
            resolved_week += 1
            resolved_rows.append(
                {
                    "ticket_id": str(ticket.id),
                    "ticket_number": ticket.ticket_number,
                    "title": scrub(ticket.title).text,
                    "site": site.name,
                    "site_slug": site.slug,
                    "assigned_to": None
                    if not ticket.assigned_resource_id
                    else resources.get(ticket.assigned_resource_id),
                    "resolved_at": completed,
                }
            )
    resolved_rows.sort(key=lambda row: row["resolved_at"], reverse=True)
    resolved_cap = max_resolved
    resolved_listed = resolved_rows[:resolved_cap]

    days = [
        {
            "day": (window_start + dt.timedelta(days=offset)).date().isoformat(),
            "created": created_by_day.get(
                (window_start + dt.timedelta(days=offset)).date().isoformat(), 0
            ),
            "resolved": resolved_by_day.get(
                (window_start + dt.timedelta(days=offset)).date().isoformat(), 0
            ),
        }
        for offset in range(window_days)
    ]

    by_priority: Counter[str] = Counter()
    for _, ticket in open_rows:
        by_priority[priorities.get(ticket.priority) or "No priority set"] += 1
    by_customer: Counter[str] = Counter()
    for site, _ in open_rows:
        by_customer[site.name] += 1
    by_technician: Counter[str] = Counter()
    for _, ticket in open_rows:
        by_technician[_assignee(ticket, resources)] += 1

    top = by_customer.most_common(TOP_CUSTOMERS)
    remainder = sum(by_customer.values()) - sum(count for _, count in top)
    customer_series = [_series(name, count) for name, count in top]
    # Everyone else is a sentence under the chart, not a sixth bar. Live, on a
    # franchisee with 58 customers, the top five held 16, 12, 9, 8 and 6 and the
    # rest held 92: as a bar that is the whole chart and the five it exists to
    # compare are slivers. The number is still printed, so nothing is hidden —
    # it is just not drawn against a scale it would destroy.
    customer_rest = {
        "count": remainder,
        "customers": max(0, len(by_customer) - len(top)),
        "note": (
            f"and {remainder} more open across the other {len(by_customer) - len(top)} "
            "customers, which is not drawn: one bar for all of them would be longer than "
            "every named bar and the five would be unreadable."
            if remainder
            else ""
        ),
    }

    # Unassigned last on the technician chart: it is a gap, not a person.
    technicians = sorted(
        ((name, count) for name, count in by_technician.items() if name != NOBODY),
        key=lambda item: (-item[1], item[0]),
    )
    technician_series = [_series(name, count) for name, count in technicians]
    if by_technician.get(NOBODY):
        technician_series.append(
            _series(NOBODY, by_technician[NOBODY], "Autotask has nobody down as working on these.")
        )

    where = f"Days are counted in {zone_name}."
    return {
        "window_days": window_days,
        "time_zone": zone_name,
        "truncated": data["truncated"],
        # The instants the day and week figures were counted from. The queue
        # filters against these rather than against the browser's own midnight,
        # so a card that says six and the rows it opens cannot be a different
        # six because the reader is in another country from the customer.
        "boundaries": {"today": today, "week": week, "window": window_start},
        "cards": [
            _card(
                "open",
                "Open",
                len(open_rows),
                "Tickets Autotask has not stamped with a completed date, across every customer "
                "of this franchisee. This is the queue below, unfiltered.",
                filter_key="none",
            ),
            _card(
                "breached",
                "Breached",
                breached,
                "Open tickets where the soonest SLA clock Autotask set — the due date, the "
                "first-reply clock while no reply has been logged, or the resolution clock — "
                "has already run out.",
                tone="bad",
                filter_key="breached",
            ),
            _card(
                "due_soon",
                "Due within 4 hours",
                due_soon,
                "Open tickets not yet breached whose soonest SLA clock runs out in the next four "
                "hours. Counted from the moment below, not from when you opened the page.",
                tone="warn",
                filter_key="four_hours",
            ),
            _card(
                "unassigned",
                "Unassigned",
                unassigned,
                "Open tickets with no assigned resource in Autotask. Nobody is down as working "
                "on these.",
                tone="warn",
                filter_key="unassigned",
            ),
            _card(
                "created_today",
                "Created today",
                created_today,
                f"Tickets raised since midnight, open or already closed again. {where}",
                filter_key="created_today",
            ),
            _card(
                "paused",
                "Clock stopped",
                paused,
                "Open tickets whose status is one this franchisee named as pausing the clock — "
                "waiting on the customer, on a part, on a vendor. Autotask tells us nothing about "
                "how long a ticket has been waiting, so these are counted apart and no breach is "
                "asserted from them.",
                tone="",
                filter_key="paused",
            ),
            _card(
                "resolved_week",
                "Resolved this week",
                resolved_week,
                "Tickets Autotask stamped completed since Monday. These are not in the open "
                f"queue below; the card opens them as their own list. {where}",
                tone="good",
                filter_key="resolved_week",
            ),
        ],
        "charts": {
            "by_priority": {
                "title": "Open tickets by priority",
                "definition": "Every open ticket, counted under this tenant's own priority "
                "labels. The labels are Autotask's picklist, never numbers we chose.",
                "series": [
                    _series(label, count)
                    for label, count in sorted(by_priority.items(), key=lambda i: (-i[1], i[0]))
                ],
            },
            "by_customer": {
                "title": "Open tickets by customer",
                "definition": f"The {TOP_CUSTOMERS} customers with the most open tickets. "
                "Everyone else is counted and stated under the chart rather than drawn, "
                "because one bar for all of them would be longer than every named bar.",
                "series": customer_series,
                "remainder": customer_rest,
            },
            "by_technician": {
                "title": "Open workload per technician",
                "definition": "Open tickets by the person Autotask has assigned them to. "
                "Unassigned is last, because it is a gap rather than a person.",
                "series": technician_series,
            },
            "created_vs_resolved": {
                "title": f"Created and resolved, last {window_days} days",
                "definition": "Per day: tickets raised that day, whether or not they are still "
                f"open, against tickets stamped completed that day. {where}",
                "days": days,
            },
        },
        "resolved_this_week": {
            "count": resolved_week,
            "listed": len(resolved_listed),
            "truncated": len(resolved_rows) > len(resolved_listed),
            "truncation_note": (
                f"The count is every one; this list stops at {resolved_cap}."
                if len(resolved_rows) > len(resolved_listed)
                else ""
            ),
            "tickets": resolved_listed,
        },
    }


async def statistics(
    session: AsyncSession,
    context: AuditContext,
    client_id: uuid.UUID,
    *,
    client_name: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """This client's figures, from the cache when they are fresh enough.

    The answer always says when it was worked out and whether it came from the
    cache, because a number on a wall with no time on it is a number nobody can
    argue with.
    """
    settings = get_settings()
    ttl = dt.timedelta(seconds=settings.ticket_statistics_ttl_seconds)
    now = dt.datetime.now(dt.UTC)
    held = _CACHE.get(client_id)
    if held is not None and not force and now - held.computed_at < ttl:
        return {**held.payload, "computed_at": held.computed_at, "cached": True}

    lock = _LOCKS.setdefault(client_id, asyncio.Lock())
    async with lock:
        # Somebody else may have filled it while we waited for the lock.
        held = _CACHE.get(client_id)
        now = dt.datetime.now(dt.UTC)
        if held is not None and not force and now - held.computed_at < ttl:
            return {**held.payload, "computed_at": held.computed_at, "cached": True}

        config = await load_config(session, client_id, client_name=client_name or "")
        zone, zone_name = franchisee_zone(config.business_hours.timezone)
        window_days = settings.ticket_statistics_window_days
        window_start = _day_start(now, zone) - dt.timedelta(days=window_days - 1)
        data = await _read_tickets(session, context, client_id, since=window_start)
        payload = build(
            data,
            now=now,
            zone=zone,
            zone_name=zone_name,
            window_days=window_days,
            calculator=SlaCalculator(config.resolved, config.business_hours, zone),
            max_resolved=settings.ticket_statistics_max_resolved,
        )
        payload["ttl_seconds"] = settings.ticket_statistics_ttl_seconds
        _CACHE[client_id] = Statistics(now, payload)
        return {**payload, "computed_at": now, "cached": False}

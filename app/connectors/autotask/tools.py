"""The Autotask tools the model can call.

Each docstring is what the model reads when choosing a tool, so it says what
the tool returns and when to reach for it.

Every tool is read-only (hard rule 2), takes `client_id` (hard rule 1), and
reports truncation honestly rather than implying a partial list is complete
(hard rule 7). Status and priority are shown as this tenant's own labels,
because the numbers mean different things in different Autotask databases.

Every tool also takes an optional `site_id`. The model never sees it: it names a
site by slug and the loop resolves that within the current client. With a site,
the call covers that end customer alone. Without one, it covers every active
site of the client, and each row carries the site it came from.

Results are compact by convention (`app/connectors/results.py`): only the
fields an engineer triaging needs per row, at most the tool's row cap, and the
true total. Filters on status label, priority label and age exist so the model
can ask for the tickets that matter rather than the whole queue.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.registry import tool
from app.connectors.autotask.client import AutotaskClient
from app.connectors.autotask.models import AutotaskTicket
from app.connectors.errors import NotFound, UpstreamError
from app.connectors.factory import connector_for
from app.connectors.results import RowCapped, SiteTagged, capped
from app.connectors.scope import SiteRef
from app.db.session import session_scope
from app.models.enums import ConnectorName
from app.resolved.sla import calculator_for

#: Notes can run to 8000 characters; keep an answer readable.
NOTE_EXCERPT_LIMIT = 1500

#: How many notes count as "recent" on a ticket.
RECENT_NOTE_LIMIT = 10


def _utcnow() -> dt.datetime:
    """The clock the tools measure against. A seam so tests can freeze it."""
    return dt.datetime.now(dt.UTC)


def _excerpt(text: str | None, limit: int = NOTE_EXCERPT_LIMIT) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "… [truncated]"


class TicketSummary(SiteTagged):
    """One ticket: only what an engineer triaging needs. `get_ticket` has the rest."""

    ticket_number: str | None = None
    title: str | None = None
    status: str | None = Field(default=None, description="This tenant's status label.")
    priority: str | None = Field(default=None, description="This tenant's priority label.")
    queue: str | None = Field(default=None, description="This tenant's queue label.")
    assigned_to: str | None = None
    created_at: dt.datetime | None = None
    age_days: float | None = None
    last_activity_at: dt.datetime | None = None
    is_open: bool = True


class OpenTicketList(RowCapped):
    """The answer to "what is open?"."""

    client_id: str
    sites_covered: list[str] = Field(
        description="The site slugs this answer covers. More than one means a merged answer."
    )
    ticket_count: int = Field(description="Same as `total`: tickets matching, before the cap.")
    filtered_by: dict[str, str] = Field(
        default_factory=dict,
        description="The filters actually applied, so a narrow answer is not mistaken for all.",
    )
    tickets: list[TicketSummary]


class TicketNoteSummary(BaseModel):
    """One note on a ticket."""

    note_id: int
    created_at: dt.datetime | None = None
    author: str | None = None
    title: str | None = None
    excerpt: str | None = None


class TicketDetail(BaseModel):
    """The answer to "what is going on with ticket T2026…?"."""

    client_id: str
    sites_covered: list[str]
    found: bool
    ticket: TicketSummary | None = None
    description: str | None = None
    resolution: str | None = None
    contact: str | None = None
    contact_email: str | None = Field(
        default=None,
        description="The contact's email address, where Autotask holds one. It is what a "
        "Microsoft 365 account is matched by: a name matches several people and an address "
        "matches one.",
    )
    device: str | None = None
    note_count: int = 0
    notes: list[TicketNoteSummary] = Field(default_factory=list)
    message: str | None = Field(
        default=None, description="Set when the ticket was not found, saying so plainly."
    )


class DeviceTicketList(RowCapped):
    """The answer to "what tickets has this machine had?"."""

    client_id: str
    sites_covered: list[str]
    device_name_searched: str
    matched_devices: list[str] = Field(default_factory=list)
    ticket_count: int = Field(default=0, description="Same as `total`.")
    tickets: list[TicketSummary] = Field(default_factory=list)
    message: str | None = None


class TimeEntrySummary(BaseModel):
    """One logged piece of work."""

    site: str
    site_slug: str
    entry_id: int
    ticket_number: str | None = None
    ticket_title: str | None = None
    worked_by: str | None = None
    date_worked: dt.datetime | None = None
    hours: float | None = None
    summary: str | None = None


class TimeEntryList(RowCapped):
    """The answer to "how much time have we put in lately?"."""

    client_id: str
    sites_covered: list[str]
    window_days: int
    since: dt.datetime
    entry_count: int = Field(description="Same as `total`: entries in the window, before the cap.")
    total_hours: float = Field(
        description="Summed over every entry in the window, not just those listed."
    )
    entries: list[TimeEntrySummary]


async def _summarise(
    connector: AutotaskClient,
    site: SiteRef,
    ticket: AutotaskTicket,
    resources: dict[int, str],
    *,
    now: dt.datetime,
) -> TicketSummary:
    age = ticket.age_days(now=now)
    return TicketSummary(
        site=site.name,
        site_slug=site.slug,
        ticket_number=ticket.ticket_number,
        title=ticket.title,
        status=await connector.label_for("Tickets", "status", ticket.status),
        priority=await connector.label_for("Tickets", "priority", ticket.priority),
        queue=await connector.label_for("Tickets", "queueID", ticket.queue_id),
        assigned_to=resources.get(ticket.assigned_resource_id or -1),
        created_at=ticket.create_date,
        age_days=round(age, 1) if age is not None else None,
        last_activity_at=ticket.last_activity_date,
        is_open=ticket.is_open,
    )


async def _resource_names(
    connector: AutotaskClient, tickets: list[AutotaskTicket]
) -> dict[int, str]:
    ids = [ticket.assigned_resource_id for ticket in tickets if ticket.assigned_resource_id]
    resources = await connector.get_resources(ids)
    return {rid: resource.name for rid, resource in resources.items()}


def _covered(connector: AutotaskClient) -> list[str]:
    return connector.scope.target_slugs


async def _resources_named(connector: AutotaskClient, name: str) -> list[Any]:
    """The engineers matching a name, or a NotFound that lists who exists."""
    matched = await connector.find_resources(name)
    if matched:
        return matched
    everyone = sorted(resource.name for resource in await connector.list_active_resources())
    known = ", ".join(everyone[:25]) + (" ..." if len(everyone) > 25 else "")
    raise NotFound(
        f"no active Autotask resource matches {name!r}. Active resources: {known or 'none'}",
        connector=connector.name.value,
        details={"resource_ref": name},
    )


@tool(ConnectorName.AUTOTASK)
async def list_open_tickets(
    client_id: str,
    older_than_days: int | None = None,
    priority: str | None = None,
    status: str | None = None,
    assigned_to: str | None = None,
    queue: str | None = None,
    site_id: str | None = None,
) -> OpenTicketList:
    """List the tickets currently open in Autotask, optionally filtered.

    Use this for "what is open", "what is outstanding", "what has been sitting
    for a fortnight", "what is in Dan's queue". Open means Autotask has not
    marked the ticket complete. Pass `site` for one end customer, or leave it
    out to cover every customer of this MSP, in which case each ticket names
    its customer. For "how many" prefer `count_tickets`.

    Filter rather than fetch the whole queue: `older_than_days` keeps only
    tickets raised at least that many days ago; `priority` and `status` take
    the labels this MSP's Autotask uses, such as "High" or "In Progress" — if
    a label does not exist the tool says which ones do; `assigned_to` is an
    engineer's name or login (partial, case-insensitive) and gives their queue.

    Rows are capped, oldest first: `total` is the real count and `truncated`
    says whether every ticket is listed.

    Not for counting — `count_tickets` answers "how many" by status, priority,
    queue, site or engineer without listing anything. Not for closed tickets or
    "have we seen this before" (`search_tickets`), nor for what came in this week
    (`list_recent_tickets`).
    """
    if older_than_days is not None and older_than_days < 0:
        raise ValueError("older_than_days cannot be negative")

    now = _utcnow()
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)

        applied: dict[str, str] = {"state": "open (not completed)"}
        created_before = None
        if older_than_days is not None:
            created_before = now - dt.timedelta(days=older_than_days)
            applied["older_than_days"] = str(older_than_days)

        priority_value = None
        if priority is not None:
            priority_value = await connector.resolve_picklist_value("Tickets", "priority", priority)
            applied["priority"] = priority

        status_value = None
        if status is not None:
            status_value = await connector.resolve_picklist_value("Tickets", "status", status)
            applied["status"] = status

        queue_value = None
        if queue is not None:
            # From this tenant's picklist, never a hardcoded id: queues are per database.
            queue_value = await connector.resolve_picklist_value("Tickets", "queueID", queue)
            applied["queue"] = queue

        assignee_ids = None
        if assigned_to is not None:
            matched = await _resources_named(connector, assigned_to)
            assignee_ids = [resource.id for resource in matched]
            applied["assigned_to"] = ", ".join(resource.name for resource in matched)

        rows, page_truncated = await connector.list_tickets(
            open_only=True,
            created_before=created_before,
            priority=priority_value,
            status=status_value,
            queue_id=queue_value,
            assigned_resource_ids=assignee_ids,
        )
        resources = await _resource_names(connector, [ticket for _, ticket in rows])
        summaries = [
            await _summarise(connector, site, ticket, resources, now=now) for site, ticket in rows
        ]
        summaries.sort(key=lambda item: item.age_days or 0.0, reverse=True)
        kept, counting = capped(
            "list_open_tickets", summaries, noun="open tickets", page_truncated=page_truncated
        )

        return OpenTicketList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            ticket_count=counting["total"],
            filtered_by=applied,
            tickets=kept,
            **counting,
        )


@tool(ConnectorName.AUTOTASK)
async def get_ticket(
    client_id: str, ticket_number: str, site_id: str | None = None
) -> TicketDetail:
    """Get one Autotask ticket by its number, with its recent notes.

    Use this when an engineer names a ticket, for example "what is happening on
    T20260915.0042" or "read me the last few notes on that ticket". Ticket
    numbers are unique across this MSP, so `site` is rarely needed; pass it only
    to restrict the search to one end customer.

    Returns the ticket's title, full description, status, priority, assignee, the
    contact who raised it, the device it concerns if any, and the most recent
    notes newest first. If no ticket with that number belongs to this MSP,
    `found` is false and `message` says so — do not guess at a different ticket.

    Not for finding a ticket — it needs the number. Search by text with
    `search_tickets`, by person with `list_tickets_for_contact`, by machine with
    `list_tickets_for_device`.
    """
    now = _utcnow()
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)

        found = await connector.get_ticket_by_number(ticket_number)
        if found is None:
            return TicketDetail(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                found=False,
                message=f"Autotask has no ticket numbered {ticket_number!r} for this MSP.",
            )
        site, ticket = found

        notes = await connector.list_ticket_notes(ticket.id)
        recent = notes[:RECENT_NOTE_LIMIT]

        author_ids = [note.creator_resource_id for note in recent if note.creator_resource_id]
        resource_ids = list(author_ids)
        if ticket.assigned_resource_id:
            resource_ids.append(ticket.assigned_resource_id)
        resources = await connector.get_resources([rid for rid in resource_ids if rid])
        names = {rid: resource.name for rid, resource in resources.items()}

        contact_name = None
        contact_email = None
        if ticket.contact_id:
            contacts = await connector.get_contacts([ticket.contact_id])
            contact = contacts.get(ticket.contact_id)
            contact_name = contact.name if contact else None
            contact_email = (contact.email_address or None) if contact else None

        device_name = None
        if ticket.configuration_item_id:
            device_name = f"configuration item {ticket.configuration_item_id}"

        return TicketDetail(
            client_id=str(client_id),
            sites_covered=[site.slug],
            found=True,
            ticket=await _summarise(connector, site, ticket, names, now=now),
            description=ticket.description,
            resolution=ticket.resolution,
            contact=contact_name,
            contact_email=contact_email,
            device=device_name,
            note_count=len(notes),
            notes=[
                TicketNoteSummary(
                    note_id=note.id,
                    created_at=note.create_date_time,
                    author=names.get(note.creator_resource_id or -1),
                    title=note.title,
                    excerpt=_excerpt(note.description),
                )
                for note in recent
            ],
        )


@tool(ConnectorName.AUTOTASK)
async def list_tickets_for_device(
    client_id: str, device_name: str, site_id: str | None = None
) -> DeviceTicketList:
    """List Autotask tickets raised against a named device.

    Use this to answer "has this machine had trouble before", "what tickets has
    PC-014 had", or to check a device's history while working a ticket. The name
    is matched against the device's Autotask title, its RMM hostname and its
    reference number, so a partial name usually works.

    Pass `site` when the same machine name could exist at more than one end
    customer. If no device matches, `matched_devices` is empty and `message`
    says so rather than returning unrelated tickets.

    Not for the machine's live state or what happened on it, which are NinjaOne's
    `get_device` and `list_device_activities`. It depends on Autotask configuration
    items, which some API users cannot read; if it errors, `search_tickets` with
    the machine name is the fallback.
    """
    now = _utcnow()
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)

        devices = await connector.find_configuration_items(device_name)
        if not devices:
            return DeviceTicketList(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                device_name_searched=device_name,
                total=0,
                returned=0,
                message=(
                    f"Autotask has no device matching {device_name!r} for this MSP, "
                    "so there are no tickets to report against it."
                ),
            )

        rows, page_truncated = await connector.list_tickets(
            open_only=False,
            configuration_item_ids=[device.id for _, device in devices],
        )
        resources = await _resource_names(connector, [ticket for _, ticket in rows])
        summaries = [
            await _summarise(connector, site, ticket, resources, now=now) for site, ticket in rows
        ]
        summaries.sort(
            key=lambda item: item.created_at or dt.datetime.min.replace(tzinfo=dt.UTC),
            reverse=True,
        )
        kept, counting = capped(
            "list_tickets_for_device", summaries, noun="tickets", page_truncated=page_truncated
        )

        return DeviceTicketList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            device_name_searched=device_name,
            matched_devices=[f"{device.name} ({site.slug})" for site, device in devices],
            ticket_count=counting["total"],
            tickets=kept,
            **counting,
        )


@tool(ConnectorName.AUTOTASK)
async def list_recent_time_entries(
    client_id: str, days: int = 7, site_id: str | None = None
) -> TimeEntryList:
    """List the time logged against tickets in the last `days` days.

    Use this for "how much time have we spent", "what did we work on this week",
    or to see where effort went before a review. Pass `site` for one end
    customer, or leave it out for every customer of this MSP — in which case do
    not present the total as belonging to any one customer without saying it is
    a combined figure.

    Returns each entry's date, hours, who logged it, the ticket it belongs to
    and its summary note, plus the total hours. Autotask time entries carry no
    customer of their own, so this is the time logged against tickets belonging
    to the sites covered. Time booked to a project or to no ticket will not
    appear.

    Not for scheduled or upcoming work (`list_service_calls`) and not a list of
    tickets (`list_recent_tickets`). It reports hours already logged.
    """
    if days < 1:
        raise ValueError("days must be at least 1")

    now = _utcnow()
    since = now - dt.timedelta(days=days)
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)

        rows, tickets_truncated = await connector.list_tickets(open_only=False, active_since=since)
        by_id = {ticket.id: (site, ticket) for site, ticket in rows}
        entries, entries_truncated = await connector.list_time_entries(
            list(by_id), worked_since=since
        )

        resources = await connector.get_resources(
            [entry.resource_id for entry in entries if entry.resource_id]
        )
        names = {rid: resource.name for rid, resource in resources.items()}

        summaries: list[TimeEntrySummary] = []
        for entry in entries:
            pair = by_id.get(entry.ticket_id) if entry.ticket_id is not None else None
            if pair is None:
                # Scoped out already; never report a row we cannot place.
                continue
            site, ticket = pair
            summaries.append(
                TimeEntrySummary(
                    site=site.name,
                    site_slug=site.slug,
                    entry_id=entry.id,
                    ticket_number=ticket.ticket_number,
                    ticket_title=ticket.title,
                    worked_by=names.get(entry.resource_id or -1),
                    date_worked=entry.date_worked,
                    hours=entry.hours_worked,
                    summary=_excerpt(entry.summary_notes, 500),
                )
            )
        summaries.sort(
            key=lambda item: item.date_worked or dt.datetime.min.replace(tzinfo=dt.UTC),
            reverse=True,
        )
        # Hours are totalled before the cap: the figure covers the whole window.
        total_hours = round(sum(entry.hours or 0.0 for entry in summaries), 2)
        kept, counting = capped(
            "list_recent_time_entries",
            summaries,
            noun="time entries",
            page_truncated=tickets_truncated or entries_truncated,
        )

        return TimeEntryList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            window_days=days,
            since=since,
            entry_count=counting["total"],
            total_hours=total_hours,
            entries=kept,
            **counting,
        )


# --- counting -----------------------------------------------------------------

TicketGroupBy = Literal["status", "priority", "site", "assignee", "queue"]


class TicketCounts(BaseModel):
    """The answer to "how many ...?" without listing a ticket."""

    client_id: str
    sites_covered: list[str]
    group_by: TicketGroupBy
    filtered_by: dict[str, str]
    total: int
    counts: dict[str, int] = Field(description="Group label -> ticket count, largest first.")
    truncated: bool = Field(
        default=False,
        description="True when the vendor page cap stopped the read: counts are lower bounds.",
    )
    truncation_note: str | None = None


@tool(ConnectorName.AUTOTASK)
async def count_tickets(
    client_id: str,
    group_by: TicketGroupBy = "status",
    days: int | None = None,
    site_id: str | None = None,
) -> TicketCounts:
    """Count Autotask tickets by status, priority, site or assignee. Numbers only.

    Use this for every "how many" question — "how many open P1s", "how many
    tickets per customer", "who has the biggest queue" — instead of listing
    tickets and counting them. Without `days` it counts the tickets open right
    now. With `days` it counts every ticket *created* in the last N days, open
    or closed, which answers "how many came in this week".

    Labels are this MSP's own status and priority names. `total` is the
    overall figure; if `truncated` is true the read was cut short and every
    number is a lower bound — say so.

    Not for seeing the tickets — numbers only; use `list_open_tickets` with the same
    filter, or `list_recent_tickets`, to read them. Not for devices
    (`count_devices`).
    """
    if days is not None and days < 1:
        raise ValueError("days must be at least 1")
    now = _utcnow()
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)
        if days is None:
            rows, truncated = await connector.list_tickets(open_only=True)
            applied = {"state": "open (not completed)"}
        else:
            since = now - dt.timedelta(days=days)
            rows, truncated = await connector.list_tickets(open_only=False, created_since=since)
            applied = {"created_since": since.isoformat(), "days": str(days)}

        labels: list[str] = []
        if group_by == "assignee":
            names = await _resource_names(connector, [ticket for _, ticket in rows])
        for site, ticket in rows:
            if group_by == "site":
                labels.append(site.name)
            elif group_by == "priority":
                labels.append(
                    await connector.label_for("Tickets", "priority", ticket.priority) or "unknown"
                )
            elif group_by == "assignee":
                labels.append(names.get(ticket.assigned_resource_id or -1, "unassigned"))
            elif group_by == "queue":
                labels.append(
                    await connector.label_for("Tickets", "queueID", ticket.queue_id) or "no queue"
                )
            else:
                labels.append(
                    await connector.label_for("Tickets", "status", ticket.status) or "unknown"
                )
        tally = Counter(labels)
        return TicketCounts(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            group_by=group_by,
            filtered_by=applied,
            total=sum(tally.values()),
            counts=dict(sorted(tally.items(), key=lambda item: (-item[1], item[0]))),
            truncated=truncated,
            truncation_note=(
                "The vendor listing stopped at the page cap, so every count is a lower bound."
                if truncated
                else None
            ),
        )


# --- SLA --------------------------------------------------------------------


class BreachRow(BaseModel):
    ticket: TicketSummary
    breach: Literal["first_response", "resolution"]
    due_at: dt.datetime | None = Field(
        default=None,
        description="When the target ran out, on the franchisee's business hours. Absent when "
        "a closed week or a very long target puts it beyond the year the clock walks.",
    )
    overdue_hours: float = Field(description="Working hours used past the target.")


class SlaBreachList(RowCapped):
    """The answer to "what is past its SLA?"."""

    client_id: str
    sites_covered: list[str]
    checked_at: dt.datetime
    open_tickets_checked: int
    breach_counts: dict[str, int] = Field(description="Breaches per kind, before the row cap.")
    paused: int = Field(
        default=0,
        description="Open tickets whose status stops the clock. Not breaching, and not judged: "
        "Autotask does not record how long a ticket has been waiting.",
    )
    clocks_without_a_target: int = Field(
        default=0,
        description="Clocks on tickets whose priority the franchisee has set no target for. "
        "Never counted as breaching or as met.",
    )
    rows: list[BreachRow]


@tool(ConnectorName.AUTOTASK)
async def list_sla_breaches(client_id: str, site_id: str | None = None) -> SlaBreachList:
    """List open tickets past an SLA clock: first response, or resolution.

    Use this for "what is breaching", "anything past due", "which tickets
    have had no response yet". Two clocks are checked on every open ticket:
    `first_response` (no first response recorded and the target used up) and
    `resolution` (not completed and the target used up). A ticket can appear
    once per clock. Most overdue first.

    The targets are the franchisee's own, by priority, and the clock runs only
    inside their business hours in their own time zone, skipping their
    holidays. A ticket whose current status pauses the clock — waiting on the
    customer, on a part — is **not** reported as breaching and is counted in
    `paused`, because how long it has been waiting is not something Autotask
    records. A priority with no configured target is counted in `no_target`
    and never as a breach.

    Not a general open-ticket list (`list_open_tickets`) and not ageing by creation
    date (`list_open_tickets` with `older_than_days`): this is strictly tickets past
    a target the franchisee has set.
    """
    now = _utcnow()
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)
        rows, page_truncated = await connector.list_tickets(open_only=True)
        resources = await _resource_names(connector, [ticket for _, ticket in rows])
        async with session_scope() as session:
            calculator = await calculator_for(session, uuid.UUID(str(client_id)))

        breaches: list[BreachRow] = []
        paused = 0
        no_target = 0
        for site, ticket in rows:
            summary = await _summarise(connector, site, ticket, resources, now=now)
            verdict = calculator.assess(
                ticket,
                priority_label=await connector.label_for("Tickets", "priority", ticket.priority),
                status_label=await connector.label_for("Tickets", "status", ticket.status),
                now=now,
            )
            if verdict.state == "paused":
                paused += 1
            for clock in (verdict.first_response, verdict.resolution):
                if clock.state == "no_target":
                    no_target += 1
                if clock.state != "breached":
                    continue
                over = clock.used_minutes or 0
                target = clock.target_minutes or 0
                breaches.append(
                    BreachRow(
                        ticket=summary,
                        breach=clock.kind,  # type: ignore[arg-type]
                        due_at=clock.due_at,
                        overdue_hours=round(max(0, over - target) / 60, 1),
                    )
                )
        breaches.sort(key=lambda row: row.overdue_hours, reverse=True)
        counts = Counter(row.breach for row in breaches)
        kept, counting = capped(
            "list_sla_breaches", breaches, noun="breaches", page_truncated=page_truncated
        )
        return SlaBreachList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            checked_at=now,
            open_tickets_checked=len(rows),
            breach_counts=dict(counts.most_common()),
            paused=paused,
            clocks_without_a_target=no_target,
            rows=kept,
            **counting,
        )


# --- recent tickets ---------------------------------------------------------


class RecentTicketList(RowCapped):
    """The answer to "what came in (or got closed) this week?"."""

    client_id: str
    sites_covered: list[str]
    days: int
    since: dt.datetime
    created_or_closed: Literal["created", "closed"]
    status_counts: dict[str, int] = Field(description="Tickets per status, before the row cap.")
    tickets: list[TicketSummary]


@tool(ConnectorName.AUTOTASK)
async def list_recent_tickets(
    client_id: str,
    days: int = 7,
    created_or_closed: Literal["created", "closed"] = "created",
    site_id: str | None = None,
) -> RecentTicketList:
    """List tickets created — or closed — in the last `days` days.

    Use this for "what came in this week", "what did we close yesterday",
    "anything new from Acme today". `created` (the default) is by creation
    date and includes tickets since closed; `closed` is by completion date.
    Newest first. `status_counts` gives the breakdown for the whole window,
    so "how many" comes from there, not from the capped rows.

    Not for searching by content (`search_tickets`) or for everything still open
    regardless of age (`list_open_tickets`). For just the numbers, `count_tickets`
    with `days`.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    now = _utcnow()
    since = now - dt.timedelta(days=days)
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)
        if created_or_closed == "closed":
            rows, page_truncated = await connector.list_tickets(
                open_only=False, completed_since=since
            )
        else:
            rows, page_truncated = await connector.list_tickets(
                open_only=False, created_since=since
            )
        resources = await _resource_names(connector, [ticket for _, ticket in rows])
        summaries = [
            await _summarise(connector, site, ticket, resources, now=now) for site, ticket in rows
        ]
        key = "created_at" if created_or_closed == "created" else "last_activity_at"
        summaries.sort(
            key=lambda item: getattr(item, key) or dt.datetime.min.replace(tzinfo=dt.UTC),
            reverse=True,
        )
        counts = Counter(item.status or "unknown" for item in summaries)
        kept, counting = capped(
            "list_recent_tickets", summaries, noun="tickets", page_truncated=page_truncated
        )
        return RecentTicketList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            days=days,
            since=since,
            created_or_closed=created_or_closed,
            status_counts=dict(counts.most_common()),
            tickets=kept,
            **counting,
        )


# --- contacts ---------------------------------------------------------------


class ContactSummary(SiteTagged):
    name: str
    email: str | None = None
    phone: str | None = None
    mobile: str | None = None
    title: str | None = None
    primary_contact: bool | None = None
    billing_contact: bool | None = None


class ContactList(RowCapped):
    """The answer to "who is calling?"."""

    client_id: str
    sites_covered: list[str]
    name_searched: str | None = None
    contacts: list[ContactSummary]


def _contact_summary(site: SiteRef, contact: Any) -> ContactSummary:
    return ContactSummary(
        site=site.name,
        site_slug=site.slug,
        name=contact.name or f"contact {contact.id}",
        email=contact.email_address,
        phone=contact.phone,
        mobile=contact.mobile_phone,
        title=contact.title,
        primary_contact=contact.primary_contact,
        billing_contact=contact.billing_contact,
    )


@tool(ConnectorName.AUTOTASK)
async def list_contacts(
    client_id: str, name: str | None = None, site_id: str | None = None
) -> ContactList:
    """List a customer's contacts in Autotask, with role, email and phone.

    Use this to identify who is calling — "who is Sarah at Acme", "what is the
    billing contact's email", "who is the primary contact". `name` matches
    first name, last name or email address (partial, case-insensitive).
    Pass `site` for one end customer; unscoped lists every customer's
    contacts, which is a long list. Active contacts only.

    Not for which machine someone uses — that is NinjaOne's `list_logged_on_users`.
    Not for their ticket history (`list_tickets_for_contact`).
    """
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)
        rows, page_truncated = await connector.list_contacts(name=name)
        summaries = [_contact_summary(site, contact) for site, contact in rows]
        summaries.sort(key=lambda item: (item.site.casefold(), item.name.casefold()))
        kept, counting = capped(
            "list_contacts", summaries, noun="contacts", page_truncated=page_truncated
        )
        return ContactList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            name_searched=name,
            contacts=kept,
            **counting,
        )


class ContactTicketList(RowCapped):
    """The answer to "what has this person raised before?"."""

    client_id: str
    sites_covered: list[str]
    contact_name_searched: str
    matched_contacts: list[str] = Field(default_factory=list)
    tickets: list[TicketSummary] = Field(default_factory=list)
    message: str | None = None


@tool(ConnectorName.AUTOTASK)
async def list_tickets_for_contact(
    client_id: str, contact_name: str, site_id: str | None = None
) -> ContactTicketList:
    """List the tickets a named contact has raised, open or closed, newest first.

    Use this for "has Sarah reported this before", "what has this user's
    history been". The contact is matched by first name, last name or email
    (partial, case-insensitive); every matching contact's tickets are included
    and `matched_contacts` says who they were. Pass `site` when the name could
    exist at more than one customer. If nobody matches, `message` says so.

    Not for identifying the person (`list_contacts`), and not for tickets about a
    topic rather than a person (`search_tickets`).
    """
    now = _utcnow()
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)
        contacts, _ = await connector.list_contacts(name=contact_name, active_only=False)
        if not contacts:
            return ContactTicketList(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                contact_name_searched=contact_name,
                total=0,
                returned=0,
                message=(
                    f"Autotask has no contact matching {contact_name!r} for "
                    f"{connector.scope.describe_targets()} of this MSP."
                ),
            )
        rows, page_truncated = await connector.list_tickets(
            open_only=False, contact_ids=[contact.id for _, contact in contacts]
        )
        resources = await _resource_names(connector, [ticket for _, ticket in rows])
        summaries = [
            await _summarise(connector, site, ticket, resources, now=now) for site, ticket in rows
        ]
        summaries.sort(
            key=lambda item: item.created_at or dt.datetime.min.replace(tzinfo=dt.UTC),
            reverse=True,
        )
        kept, counting = capped(
            "list_tickets_for_contact", summaries, noun="tickets", page_truncated=page_truncated
        )
        return ContactTicketList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            contact_name_searched=contact_name,
            matched_contacts=[
                f"{contact.name or contact.id} ({site.slug})" for site, contact in contacts
            ],
            tickets=kept,
            **counting,
        )


# --- configuration items ----------------------------------------------------


class ConfigurationItemSummary(SiteTagged):
    name: str
    reference_number: str | None = None
    serial_number: str | None = None
    rmm_hostname: str | None = None
    type: str | None = Field(default=None, description="This tenant's configuration item type.")
    operating_system: str | None = None
    last_user: str | None = None
    ip_address: str | None = None
    install_date: dt.datetime | None = None
    warranty_expires: dt.datetime | None = None


class ConfigurationItemList(RowCapped):
    """The answer to "what does Autotask think this customer owns?"."""

    client_id: str
    sites_covered: list[str]
    name_searched: str | None = None
    items: list[ConfigurationItemSummary]


@tool(ConnectorName.AUTOTASK)
async def list_configuration_items(
    client_id: str, name: str | None = None, site_id: str | None = None
) -> ConfigurationItemList:
    """List Autotask's asset records (configuration items) for a customer.

    Use this for "what assets does Autotask have for Acme", "is PC-014 in
    Autotask", "what is the serial of the finance server per the PSA". These
    are the PSA's records, which often disagree with what NinjaOne sees — the
    disagreement is itself the answer to some questions, so do not merge the
    two silently. `name` matches title, RMM hostname, reference number or
    serial (partial, case-insensitive). Active items only.

    Not the live inventory: what is actually out there, and its state, is NinjaOne's
    `list_devices`, `get_device` and `get_hardware`. Use this for the PSA's record —
    warranty, install date, contract linkage — and to spot disagreement between
    the two.
    """
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)
        rows, page_truncated = await connector.list_configuration_items(name=name)
        summaries = [
            ConfigurationItemSummary(
                site=site.name,
                site_slug=site.slug,
                name=item.name,
                reference_number=item.reference_number,
                serial_number=item.serial_number,
                rmm_hostname=item.rmm_hostname,
                type=await connector.label_for(
                    "ConfigurationItems", "configurationItemType", item.configuration_item_type
                ),
                operating_system=item.rmm_operating_system,
                last_user=item.rmm_last_user,
                ip_address=item.rmm_ip_address,
                install_date=item.install_date,
                warranty_expires=item.warranty_expiration_date,
            )
            for site, item in rows
        ]
        summaries.sort(key=lambda item: (item.site.casefold(), item.name.casefold()))
        kept, counting = capped(
            "list_configuration_items", summaries, noun="items", page_truncated=page_truncated
        )
        return ConfigurationItemList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            name_searched=name,
            items=kept,
            **counting,
        )


# --- the company card -------------------------------------------------------


class CompanyCard(BaseModel):
    """The answer to "tell me about this customer"."""

    client_id: str
    sites_covered: list[str]
    found: bool
    message: str | None = None
    site: str | None = None
    site_slug: str | None = None
    name: str | None = None
    company_number: str | None = None
    company_type: str | None = None
    classification: str | None = Field(
        default=None, description="The tenant's account grade, e.g. Managed Services. Not industry."
    )
    industry: str | None = Field(
        default=None, description="Autotask's market segment, e.g. Legal Services."
    )
    locations: list[str] = Field(
        default_factory=list, description="Active company locations, primary first."
    )
    is_active: bool | None = None
    phone: str | None = None
    address: str | None = None
    web_address: str | None = None
    account_manager: str | None = Field(default=None, description="The owner resource's name.")
    other_companies: list[str] = Field(
        default_factory=list,
        description=(
            "The site's other Autotask companies, when it holds more than one. The card "
            "is the first company's record; contracts are counted across all of them."
        ),
    )
    active_contracts: int | None = Field(
        default=None, description="None when Autotask refused the contracts query; see note."
    )
    contracts_note: str | None = None
    customer_since: dt.datetime | None = None
    last_activity: dt.datetime | None = None


@tool(ConnectorName.AUTOTASK)
async def get_company(client_id: str, site_id: str | None = None) -> CompanyCard:
    """The summary card for one end customer: address, phone, account manager, contracts.

    Use this for "tell me about Acme", "who is the account manager for", "how
    many contracts does this customer have", "what is their address", "what
    industry are they in", "how many offices do they have". Needs a
    `site`: with none, or more than one in scope, it asks for one rather than
    guessing. `active_contracts` is null with a `contracts_note` when Autotask
    refuses the contracts query — say so; do not report zero.

    Not for the customer's people (`list_contacts`), its tickets (`list_open_tickets`,
    `count_tickets`) or its devices (`count_devices`). This is the account card.
    """
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)
        if not connector.scope.is_single_site:
            return CompanyCard(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                found=False,
                message=(
                    f"This covers {connector.scope.describe_targets()}. Name one end customer "
                    "with `site` to get its company card."
                ),
            )
        # A site may hold two Autotask companies: the card is the first one's
        # record, names the others, and counts contracts across all of them.
        site = connector.scope.targets[0]
        companies = await connector.get_site_companies(site.site_id)
        if not companies:
            return CompanyCard(
                client_id=str(client_id),
                sites_covered=[site.slug],
                found=False,
                message=f"Autotask returned no company for site {site.slug!r}.",
            )
        site, company = companies[0]
        other_companies = [
            other.company_name or f"company {other.id}" for _, other in companies[1:]
        ]

        manager = None
        if company.owner_resource_id:
            owners = await connector.get_resources([company.owner_resource_id])
            owner = owners.get(company.owner_resource_id)
            manager = owner.name if owner else None

        contracts: int | None
        contracts_note: str | None = None
        try:
            contracts = await connector.count_site_active_contracts(site.site_id)
        except UpstreamError as error:
            contracts = None
            contracts_note = (
                f"Autotask refused the contracts query ({error}); the contract count is "
                "unknown, not zero."
            )

        try:
            placed_locations = await connector.list_company_locations()
        except UpstreamError:
            # Some API users cannot read an entity at all (Contracts, on the pilot).
            placed_locations = []
        ordered = sorted(placed_locations, key=lambda pair: (not pair[1].is_primary, pair[1].id))
        locations = [
            " — ".join(
                part
                for part in (
                    location.name,
                    ", ".join(p for p in (location.city, location.state) if p) or None,
                )
                if part
            )
            for _, location in ordered
        ]

        address_parts = [
            part
            for part in (
                company.address1,
                company.address2,
                ", ".join(p for p in (company.city, company.state) if p) or None,
                company.postal_code,
            )
            if part
        ]
        return CompanyCard(
            client_id=str(client_id),
            sites_covered=[site.slug],
            found=True,
            site=site.name,
            site_slug=site.slug,
            name=company.company_name,
            company_number=company.company_number,
            company_type=await connector.label_for(
                "Companies", "companyType", company.company_type
            ),
            classification=await connector.label_for(
                "Companies", "classification", company.classification
            ),
            industry=await connector.label_for(
                "Companies", "marketSegmentID", company.market_segment_id
            ),
            locations=locations,
            is_active=company.is_active,
            phone=company.phone,
            address=", ".join(address_parts) or None,
            web_address=company.web_address,
            account_manager=manager,
            other_companies=other_companies,
            active_contracts=contracts,
            contracts_note=contracts_note,
            customer_since=company.create_date,
            last_activity=company.last_activity_date,
        )


# --- search: "have we seen this before?" ----------------------------------------

SearchOrder = Literal["recency", "relevance"]

SNIPPET_RADIUS = 70
MAX_NOTE_SCORE = 3


class TicketMatch(SiteTagged):
    """One ticket that matched a search, with where and how strongly."""

    ticket_number: str | None = None
    title: str | None = None
    status: str | None = Field(default=None, description="This tenant's status label.")
    created_at: dt.datetime | None = None
    completed_at: dt.datetime | None = Field(
        default=None, description="Empty while the ticket is open."
    )
    matched_in: list[str] = Field(
        description="Where the text was found: any of title, description, notes."
    )
    note_matches: int = Field(default=0, description="How many of the ticket's notes matched.")
    snippet: str | None = Field(
        default=None, description="The text around the first match, from the strongest field."
    )
    score: int = Field(
        description="Relevance: title 3, description 2, each matching note 1 (at most 3)."
    )


class TicketSearchResult(RowCapped):
    """The answer to "have we seen this before?"."""

    client_id: str
    sites_covered: list[str]
    query: str
    days: int | None = None
    searched: list[str]
    order_by: SearchOrder
    ranking_note: str
    notes_outside_scope: int = Field(
        default=0,
        description="Matching notes on tickets that are not this client's. Dropped, not shown.",
    )
    tickets: list[TicketMatch]


def _snippet(text: str | None, query: str) -> str | None:
    """The text around the first case-insensitive match, on one line."""
    if not text:
        return None
    flat = " ".join(text.split())
    at = flat.casefold().find(query.casefold())
    if at < 0:
        return None
    start = max(at - SNIPPET_RADIUS, 0)
    end = min(at + len(query) + SNIPPET_RADIUS, len(flat))
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _contains(text: str | None, query: str) -> bool:
    return bool(text) and query.casefold() in str(text).casefold()


@tool(ConnectorName.AUTOTASK)
async def search_tickets(
    client_id: str,
    query: str,
    days: int | None = None,
    include_notes: bool = True,
    order_by: SearchOrder = "recency",
    site_id: str | None = None,
) -> TicketSearchResult:
    """Search past and present tickets for a word or phrase: title, description and notes.

    THE tool for "have we seen this before", "how did we fix this last time",
    "any other tickets mentioning error 0x80070005 / the Brother driver / VPN
    drops". It searches open and closed tickets alike and returns where the
    text matched and a snippet around it; follow up with `get_ticket` for the
    full notes of a promising one. `query` is matched as a phrase,
    case-insensitively, so search for the distinctive part — an error code, a
    product name — not a whole sentence.

    `days` limits it to tickets (and notes) created in the last N days; leave
    it out to search all history. `include_notes` also searches ticket notes,
    which is where fixes are usually written down; it costs a second query.
    `order_by` is "recency" (newest first, the default) or "relevance"
    (title match 3, description 2, each matching note 1 up to 3, then
    newest). Autotask itself cannot rank or sort, so the ranking is done here
    and `score` is on every row.

    Not for listing a queue or what is open (`list_open_tickets`), what came
    in this week (`list_recent_tickets`), counting (`count_tickets`), or a
    ticket whose number you already have (`get_ticket`).
    """
    phrase = " ".join(query.split())
    if len(phrase) < 2:
        raise ValueError("query must be at least 2 characters")
    if days is not None and days < 1:
        raise ValueError("days must be at least 1")

    now = _utcnow()
    since = now - dt.timedelta(days=days) if days is not None else None
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)

        # Pass 1: tickets, through the company filter.
        rows, tickets_truncated = await connector.list_tickets(
            open_only=False, created_since=since, text=phrase
        )
        owned: dict[int, tuple[SiteRef, AutotaskTicket]] = {
            ticket.id: (site, ticket) for site, ticket in rows
        }

        # Pass 2: notes. They carry no company, and Autotask cannot join them to
        # tickets in one query, so the note search is tenant-wide; each note's
        # ticket is then fetched *through the company filter*, and a note whose
        # ticket does not come back is not this client's and is dropped.
        notes_by_ticket: dict[int, list[str]] = {}
        notes_truncated = False
        outside = 0
        if include_notes:
            notes, notes_truncated = await connector.search_ticket_notes(
                phrase, created_since=since
            )
            unknown = [n.ticket_id for n in notes if n.ticket_id and n.ticket_id not in owned]
            for site, ticket in await connector.get_tickets_by_ids(unknown):
                owned[ticket.id] = (site, ticket)
            for note in notes:
                if note.ticket_id is None or note.ticket_id not in owned:
                    outside += 1
                    continue
                body = note.description if _contains(note.description, phrase) else note.title
                notes_by_ticket.setdefault(note.ticket_id, []).append(body or "")

        matches: list[TicketMatch] = []
        for ticket_id, (site, ticket) in owned.items():
            in_title = _contains(ticket.title, phrase)
            in_description = _contains(ticket.description, phrase)
            note_bodies = notes_by_ticket.get(ticket_id, [])
            if not (in_title or in_description or note_bodies):
                continue  # the vendor matched something we cannot see; do not claim a hit
            matched_in = [
                label
                for label, hit in (
                    ("title", in_title),
                    ("description", in_description),
                    ("notes", bool(note_bodies)),
                )
                if hit
            ]
            snippet = (
                _snippet(ticket.title, phrase)
                if in_title
                else _snippet(ticket.description, phrase)
                if in_description
                else next((s for s in (_snippet(b, phrase) for b in note_bodies) if s), None)
            )
            # A title hit shows the title anyway, so prefer the body text that explains it.
            if in_title and (in_description or note_bodies):
                snippet = (
                    _snippet(ticket.description, phrase)
                    or next((s for s in (_snippet(b, phrase) for b in note_bodies) if s), None)
                    or snippet
                )
            matches.append(
                TicketMatch(
                    site=site.name,
                    site_slug=site.slug,
                    ticket_number=ticket.ticket_number,
                    title=ticket.title,
                    status=await connector.label_for("Tickets", "status", ticket.status),
                    created_at=ticket.create_date,
                    completed_at=ticket.completed_date,
                    matched_in=matched_in,
                    note_matches=len(note_bodies),
                    snippet=snippet,
                    score=3 * in_title + 2 * in_description + min(len(note_bodies), MAX_NOTE_SCORE),
                )
            )

        oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
        matches.sort(key=lambda m: m.created_at or oldest, reverse=True)
        if order_by == "relevance":
            matches.sort(key=lambda m: m.score, reverse=True)  # stable: recency breaks ties

        kept, counting = capped(
            "search_tickets",
            matches,
            noun="matching tickets",
            page_truncated=tickets_truncated or notes_truncated,
        )
        return TicketSearchResult(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            query=phrase,
            days=days,
            searched=["title", "description", *(["notes"] if include_notes else [])],
            order_by=order_by,
            ranking_note=(
                "Autotask's query API has no sort or relevance ranking; results are ordered "
                "here. `score`: title 3, description 2, each matching note 1 (max 3)."
            ),
            notes_outside_scope=outside,
            tickets=kept,
            **counting,
        )


# --- service calls --------------------------------------------------------------


class ServiceCallRow(SiteTagged):
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    duration_hours: float | None = None
    status: str | None = Field(default=None, description="This tenant's service call status.")
    complete: bool | None = None
    description: str | None = None
    resources: list[str] = Field(default_factory=list, description="Who is assigned.")
    tickets: list[str] = Field(default_factory=list, description="Ticket number and title.")


class ServiceCallList(RowCapped):
    """The answer to "who is on site where today?"."""

    client_id: str
    sites_covered: list[str]
    days: int
    since: dt.datetime
    rows: list[ServiceCallRow]


@tool(ConnectorName.AUTOTASK)
async def list_service_calls(
    client_id: str, days: int = 1, site_id: str | None = None
) -> ServiceCallList:
    """List Autotask service calls — scheduled visits and booked work — with who and which tickets.

    Use this for "who is on site at Acme today", "what is scheduled this
    week", "which tickets is tomorrow's visit for". Covers service calls
    starting from `days` days ago onwards, upcoming ones included, earliest
    first. Each row gives the time, the customer, the engineers assigned and
    the tickets the call is for.

    An MSP that does not schedule through Autotask service calls has none, and
    an empty list then means exactly that — not that nothing is happening.

    Not for time already logged (`list_recent_time_entries`) or for an
    engineer's ticket queue (`list_open_tickets` with `assigned_to`).
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    now = _utcnow()
    since = now - dt.timedelta(days=days)
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)
        calls, page_truncated = await connector.list_service_calls(starting_since=since)
        ticket_links, resource_links = await connector.service_call_links(
            [call.id for _, call in calls]
        )

        # The join rows have no company. The tickets they name are fetched through
        # the company filter, so one that is not this client's is never shown.
        tickets = {
            ticket.id: ticket
            for _, ticket in await connector.get_tickets_by_ids(
                [link.ticket_id for link in ticket_links if link.ticket_id]
            )
        }
        resources = await connector.get_resources(
            [link.resource_id for link in resource_links if link.resource_id]
        )
        links_by_call: dict[int, list[Any]] = {}
        for link in ticket_links:
            if link.service_call_id is not None:
                links_by_call.setdefault(link.service_call_id, []).append(link)
        resources_by_link: dict[int, list[int]] = {}
        for resource_link in resource_links:
            if resource_link.service_call_ticket_id and resource_link.resource_id:
                resources_by_link.setdefault(resource_link.service_call_ticket_id, []).append(
                    resource_link.resource_id
                )

        rows: list[ServiceCallRow] = []
        for site, call in calls:
            call_links = links_by_call.get(call.id, [])
            names = {
                resources[rid].name
                for link in call_links
                for rid in resources_by_link.get(link.id, [])
                if rid in resources
            }
            rows.append(
                ServiceCallRow(
                    site=site.name,
                    site_slug=site.slug,
                    start=call.start_date_time,
                    end=call.end_date_time,
                    duration_hours=call.duration,
                    status=await connector.label_for("ServiceCalls", "status", call.status),
                    complete=bool(call.is_complete) if call.is_complete is not None else None,
                    description=_excerpt(call.description, 200),
                    resources=sorted(names),
                    tickets=[
                        f"{tickets[link.ticket_id].ticket_number} — {tickets[link.ticket_id].title}"
                        for link in call_links
                        if link.ticket_id in tickets
                    ],
                )
            )
        rows.sort(key=lambda r: r.start or dt.datetime.max.replace(tzinfo=dt.UTC))
        kept, counting = capped(
            "list_service_calls", rows, noun="service calls", page_truncated=page_truncated
        )
        return ServiceCallList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            days=days,
            since=since,
            rows=kept,
            **counting,
        )


# --- projects -------------------------------------------------------------------

ProjectState = Literal["open", "scheduled", "completed", "all"]

#: Project types that are not work for a customer, by this tenant's label.
NON_CLIENT_PROJECT_TYPES = ("template", "baseline", "internal")


class ProjectRow(SiteTagged):
    project: str
    number: str | None = None
    status: str | None = Field(default=None, description="This tenant's status label.")
    project_type: str | None = None
    phase: str | None = Field(
        default=None, description="The phase in progress today, else the next one to start."
    )
    percent_complete: int | None = None
    start_date: dt.datetime | None = None
    end_date: dt.datetime | None = None
    completed_date: dt.datetime | None = None
    overdue: bool = Field(default=False, description="Open and past its end date.")
    description: str | None = None


class ProjectList(RowCapped):
    """The answer to "what projects are under way, or coming up?"."""

    client_id: str
    sites_covered: list[str]
    state: ProjectState
    status_counts: dict[str, int] = Field(description="Projects per status, before the row cap.")
    projects: list[ProjectRow]


def current_phase(phases: list[Any], now: dt.datetime) -> str | None:
    """The phase whose dates contain today; else the next to start; else the last one."""
    dated = sorted(
        (phase for phase in phases if phase.title),
        key=lambda phase: phase.start_date or phase.due_date or now,
    )
    if not dated:
        return None
    for phase in dated:
        if phase.start_date and phase.due_date and phase.start_date <= now <= phase.due_date:
            return str(phase.title)
    upcoming = [phase for phase in dated if phase.start_date and phase.start_date > now]
    return str((upcoming[0] if upcoming else dated[-1]).title)


@tool(ConnectorName.AUTOTASK)
async def list_projects(
    client_id: str, state: ProjectState = "open", site_id: str | None = None
) -> ProjectList:
    """Autotask projects for the customer: status, current phase, percent complete, dates.

    Use this for "what projects do we have on for Acme", "how far along is the
    migration", "what is scheduled to start next quarter", "which projects are
    overdue". `state` is "open" (the default: not complete, already started or
    undated), "scheduled" (not complete, start date in the future), "completed"
    or "all". Statuses are this tenant's own labels. Internal, template and
    baseline projects are left out.

    Not for tickets, which are separate from project work (`list_open_tickets`,
    `count_tickets`), nor for on-site visits (`list_service_calls`). Budgets
    and revenue are not read by any tool.
    """
    now = _utcnow()
    async with connector_for(client_id, ConnectorName.AUTOTASK, site_id) as connector:
        assert isinstance(connector, AutotaskClient)
        types = await connector.picklist("Projects", "projectType")
        wanted_types = [
            value
            for value, label in types.items()
            if label.strip().casefold() not in NON_CLIENT_PROJECT_TYPES
        ]
        complete = await connector.resolve_picklist_value("Projects", "status", "Complete")
        placed, page_truncated = await connector.list_projects(project_types=wanted_types)

        def wanted(project: Any) -> bool:
            done = project.status == complete or project.completed_date_time is not None
            future = project.start_date_time is not None and project.start_date_time > now
            if state == "all":
                return True
            if state == "completed":
                return done
            if state == "scheduled":
                return not done and future
            return not done and not future

        kept_projects = [(site, project) for site, project in placed if wanted(project)]
        phases = await connector.list_phases([project.id for _, project in kept_projects])
        by_project: dict[int, list[Any]] = {}
        for phase in phases:
            if phase.project_id is not None:
                by_project.setdefault(phase.project_id, []).append(phase)

        rows: list[ProjectRow] = []
        for site, project in kept_projects:
            done = project.status == complete or project.completed_date_time is not None
            rows.append(
                ProjectRow(
                    site=site.name,
                    site_slug=site.slug,
                    project=project.project_name or "(unnamed project)",
                    number=project.project_number,
                    status=await connector.label_for("Projects", "status", project.status),
                    project_type=types.get(project.project_type or -1),
                    phase=current_phase(by_project.get(project.id, []), now),
                    percent_complete=project.completed_percentage,
                    start_date=project.start_date_time,
                    end_date=project.end_date_time,
                    completed_date=project.completed_date_time,
                    overdue=bool(
                        not done and project.end_date_time and project.end_date_time < now
                    ),
                    description=_excerpt(project.description, 300),
                )
            )
        far = dt.datetime.max.replace(tzinfo=dt.UTC)
        rows.sort(key=lambda row: (not row.overdue, row.end_date or far, row.project.casefold()))
        counts = Counter(row.status or "unknown" for row in rows)
        kept, counting = capped(
            "list_projects", rows, noun="projects", page_truncated=page_truncated
        )
        return ProjectList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            state=state,
            status_counts=dict(counts.most_common()),
            projects=kept,
            **counting,
        )

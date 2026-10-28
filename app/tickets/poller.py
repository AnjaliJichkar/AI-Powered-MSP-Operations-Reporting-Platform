"""The Autotask read every poller shares, and the tickets as the automation
triggers want them.

Two callers, and neither owns the other: the forward shadow
(`app/autopilot/wiring.py`) classifies what arrived, and the automation
triggers' poll (`scripts/automation.py poll`) evaluates its rules over it.
Both read a ticket through this one function, under a `RequestBudget` they
count themselves, so a ticket's labels mean one thing wherever they are read.
Read-only, like every path to a vendor.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.triggers import Arrival
from app.connectors.autotask.client import AutotaskClient
from app.connectors.factory import connector_for
from app.models.enums import ConnectorName
from app.models.site import Site
from app.tickets.arrivals import NewTicket
from app.tickets.scrub import scrub

logger = logging.getLogger(__name__)


async def read_new_tickets(
    client_id: uuid.UUID, *, created_since: dt.datetime | None
) -> list[NewTicket]:
    """The client's open tickets, created since `created_since` (or all of
    them when None), as a poller wants them.

    One reader for every caller — the forward shadow's poll and backfill, the
    automation triggers' poll — because "what arrived since then" and "what is
    open now" differ by one filter and nothing else: two copies of this would
    be two opinions about what a ticket's queue label means.

    Goes through `list_tickets`, so it inherits the condition budget that
    splits an over-long filter (api-notes) and the scope guard that drops a row
    belonging to a company this client does not hold. Read-only, like every
    Phase 1 path.
    """
    async with connector_for(client_id, ConnectorName.AUTOTASK) as built:
        connector = cast(AutotaskClient, built)
        rows, _ = await connector.list_tickets(open_only=True, created_since=created_since)
        found: list[NewTicket] = []
        queues: dict[int, str | None] = {}
        # Three picklists for the trigger rules, read once per connector
        # instance and cached by it: three requests a pass, counted like the
        # rest.
        for site, ticket in rows:
            # The queue as it is **now**, which for a ticket that arrived
            # minutes ago is the queue it was raised into — the one thing
            # the forward shadow can honestly record and a replay cannot.
            # On a backfill the ticket may be weeks old, so the label is the
            # queue it sits in today and is worth less; it is recorded the
            # same way either way, and the run's mode says which this was.
            if ticket.queue_id is not None and ticket.queue_id not in queues:
                queues[ticket.queue_id] = await connector.label_for(
                    "Tickets", "queueID", ticket.queue_id
                )
            found.append(
                NewTicket(
                    ticket_id=str(ticket.id),
                    ticket_number=ticket.ticket_number,
                    title=ticket.title or "",
                    site_id=site.site_id,
                    site_slug=site.slug,
                    created_at=ticket.create_date,
                    # The contact is used only to find the machine, as the
                    # workspace uses it; the classifier never sees it.
                    contact=None,
                    queue_label=queues.get(ticket.queue_id) if ticket.queue_id else None,
                    # Scrubbed on the way to the model by `forward_pass`,
                    # and stored nowhere.
                    description=ticket.description,
                    issue_type_label=await connector.label_for(
                        "Tickets", "issueType", ticket.issue_type
                    ),
                    sub_issue_type_label=await connector.label_for(
                        "Tickets", "subIssueType", ticket.sub_issue_type
                    ),
                    source_label=await connector.label_for("Tickets", "source", ticket.source),
                    contact_id=ticket.contact_id,
                )
            )
        return found


async def arrivals_for(
    session: AsyncSession, client_id: uuid.UUID, tickets: list[NewTicket]
) -> list[Arrival]:
    """The tickets a pass read, as the automation triggers want them.

    Everything a rule reads is already on the ticket as a label. The company
    is the site's name from our own table. The contact's name is the one
    extra vendor read — one `get_contacts` call for every ticket that has a
    contact, so a workflow that fills an input from the contact can — and a
    read that fails leaves the contact blank rather than failing the pass.
    """
    names: dict[uuid.UUID, str] = {}
    site_ids = [t.site_id for t in tickets if t.site_id is not None]
    if site_ids:
        rows = await session.execute(select(Site.id, Site.name).where(Site.id.in_(site_ids)))
        names = {site_id: name for site_id, name in rows.all()}
    contacts: dict[int, str] = {}
    wanted = [t.contact_id for t in tickets if t.contact_id is not None]
    if wanted:
        try:
            async with connector_for(client_id, ConnectorName.AUTOTASK) as built:
                connector = cast(AutotaskClient, built)
                for contact_id, contact in (await connector.get_contacts(wanted)).items():
                    full = " ".join(p for p in (contact.first_name, contact.last_name) if p)
                    if full:
                        contacts[contact_id] = full
        except Exception:  # the pass goes on; a rule that needs the name sees none
            logger.exception("contacts for the automation triggers could not be read")
    return [
        Arrival(
            ticket_id=t.ticket_id,
            ticket_number=t.ticket_number,
            title=t.title,
            queue=t.queue_label,
            issue_type=t.issue_type_label,
            sub_issue_type=t.sub_issue_type_label,
            source=t.source_label,
            contact=contacts.get(t.contact_id) if t.contact_id is not None else None,
            company=names.get(t.site_id) if t.site_id is not None else None,
            site_slug=t.site_slug,
            created_at=t.created_at,
            # The body, scrubbed as the workspace scrubs it, for the note
            # workflows that read it; never stored by the log.
            description=scrub(t.description).text if t.description else None,
        )
        for t in tickets
    ]

"""Where the forward shadow meets the real tools. The only place it does.

`app/autopilot/shadow/` imports no connector, so a replay provably cannot call
a vendor. The forward shadow does need to, and this module is the seam: it
builds the two callables `forward_pass` takes — "what is new" and "enrich
this" — out of code that already exists, and hands them in.

**The enrichment is the ticket workspace's, not a copy of it.** `resolve_device`
decides which machine a ticket is about and `enrich` asks the tools about it;
both are called here exactly as the workspace page calls them, so the forward
shadow and the page cannot drift into two opinions about one machine. The
workspace's resolution takes a `TicketWorkspace`, so one is built as a value
and never saved: the shadow measures, and opening a workspace is something a
person does.

Configuration items are deliberately not used. They return 500 for this tenant
(api-notes), and the workspace's hostname-and-contact matching is what works.

Blackpoint is not built. The snapshot names it as not assessed rather than
leaving it out, because a tool nobody asked is a gap in the evidence and not a
clean bill of health — and that is also why a tool the customer does not own is
said in those words rather than in ours.

**The Autotask read itself lives in `app/tickets/poller.py`**, shared with
the automation triggers' own poll (`scripts/automation.py poll`), so the two
pollers read a ticket one way and neither owns the other.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext
from app.autopilot.shadow.forward import (
    LIVE_CONNECTORS,
    DeviceAnswer,
    EnrichmentAnswer,
    NewTicket,
    reduce_facts,
)
from app.connectors.factory import why_not_configured
from app.models.device import Device
from app.models.enums import ConnectorName
from app.models.site import Site
from app.models.ticket import TicketWorkspace
from app.tickets.enrich import enrich
from app.tickets.poller import read_new_tickets
from app.tickets.scrub import scrub
from app.tickets.workspace import resolve_device, workspace_links


def autotask_source(session: AsyncSession, context: AuditContext, client_id: uuid.UUID) -> Any:
    """ "Which tickets arrived since then", for a polling pass."""

    async def read(since: dt.datetime) -> list[NewTicket]:
        return await read_new_tickets(client_id, created_since=since)

    return read


def autotask_open_tickets(
    session: AsyncSession, context: AuditContext, client_id: uuid.UUID
) -> Any:
    """ "Which tickets are open now", for the one-time backfill.

    The same read with the creation filter dropped. A backfill is not catching
    up on what a poller missed — it is the work sitting in the queue today,
    whenever it arrived, because that is what an engineer will open next.
    """

    async def read(since: dt.datetime) -> list[NewTicket]:
        del since  # a backfill is about what is open, not about when it arrived
        return await read_new_tickets(client_id, created_since=None)

    return read


def workspace_enricher(session: AsyncSession, context: AuditContext, client_id: uuid.UUID) -> Any:
    """ "What do the tools say about this ticket's machine", the workspace's way."""

    async def read(ticket: NewTicket) -> EnrichmentAnswer:
        # A value, never saved: `resolve_device` wants a workspace and the
        # shadow has no business creating one.
        standing_in = TicketWorkspace(
            client_id=client_id,
            site_id=ticket.site_id,
            ticket_number=ticket.ticket_number or ticket.ticket_id,
            title=ticket.title,
            # Scrubbed, and on a value that is never saved. The resolver reads
            # the body for machine names exactly as the ticket page's does, and
            # most tickets name the machine in the body rather than the
            # subject: without this the forward shadow resolved almost nothing
            # and every card said "no machine stood out".
            description=scrub(ticket.description).text if ticket.description else None,
            contact=ticket.contact,
        )
        resolution = await resolve_device(session, context, standing_in)
        device = await session.get(Device, resolution.device_id) if resolution.device_id else None
        links = await workspace_links(session, resolution.device_id)
        found = await enrich(
            session,
            context,
            client_id=client_id,
            device=device,
            links=links,
            contact=ticket.contact,
            site_id=ticket.site_id,
            ticket_number=ticket.ticket_number,
        )

        # Which tools were not asked, and why. A database read per connector
        # and no HTTP: the factory is asked whether it could build one at all
        # (`why_not_configured`, the same function the enrichment's
        # `backup.not_assessed` and the Integrations page use), and the words
        # are decided by `tools_not_asked` below.
        held = {link.connector_name.value for link in links}
        unset = {
            name
            for name in LIVE_CONNECTORS
            if await why_not_configured(client_id, ConnectorName(name), ticket.site_id) is not None
        }
        not_assessed = tools_not_asked(unset=unset, device_found=device is not None, held=held)
        return EnrichmentAnswer(
            device=DeviceAnswer(
                device_id=resolution.device_id,
                device_site_id=device.site_id if device else None,
                method=resolution.method,
                confidence=resolution.confidence,
                candidates=len(resolution.candidates),
            ),
            read_at=found.as_of,
            facts=reduce_facts(found.facts),
            reads=found.reads,
            # The error's kind, not its message: a message could quote a record.
            errors=[
                {
                    "source": entry.get("source"),
                    "error_type": (entry.get("error") or "").split(":")[0],
                }
                for entry in found.errors
            ],
            not_assessed=not_assessed,
        )

    return read


#: Why a tool was not asked, in the order the answers rule each other out.
#: Each is a different claim and the card prints it as one sentence, so the
#: precedence is the whole of the decision and is written down rather than
#: implied by the order of three `if`s inside a loop.
NOT_SET_UP = "not set up for this customer"
NO_DEVICE = "no device resolved for this ticket"
NO_LINK = "no active vendor link for this machine"


def tools_not_asked(*, unset: set[str], device_found: bool, held: set[str]) -> dict[str, str]:
    """Which of the live tools were not asked about this ticket, and why.

    A tool nobody asked is a gap in the evidence rather than a clean bill of
    health, so it is named on the snapshot instead of left out — that is the
    coverage board's rule on one card.

    **Not set up comes first, because it is a different claim about a
    different thing.** A customer who does not buy backup has no Cove link on
    any machine, and "no active vendor link for this machine" says that about
    the laptop when it is true of the customer: it reads as a finding about a
    machine rather than as a tool we were never able to ask. Before Cove's
    connector existed the same line read "Cove (connector not built)", which
    was a sentence about us; now there is a connector and the customer may
    simply not own it, and that is what it says.
    """
    answers: dict[str, str] = {}
    for name in LIVE_CONNECTORS:
        if name in unset:
            answers[name] = NOT_SET_UP
        elif not device_found:
            answers[name] = NO_DEVICE
        elif name not in held:
            answers[name] = NO_LINK
    return answers


async def site_ids(session: AsyncSession, client_id: uuid.UUID) -> set[uuid.UUID]:
    """This client's sites, for a caller that wants to check a device's one."""
    rows = (await session.execute(select(Site.id).where(Site.client_id == client_id))).scalars()
    return set(rows)

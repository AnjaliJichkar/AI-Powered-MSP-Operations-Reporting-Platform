"""The note workflows' reads, live: the `Tools` protocol over the real connectors.

`app/automation/notes/` builds a note from facts it is handed; this is where
the facts come from when the poll runs. Every read goes through the same
connector tools the ticket page uses — `get_device`, `list_open_alerts`,
`get_endpoint_protection`, `list_recent_detections`, the Autotask client's
own `list_tickets` and `get_tickets_by_ids` — and every read is a
`tool_call` audit row, a failed one on a connection of its own, as the
enrichment panel's are. The device is found through the same resolution the
ticket workspace uses (`app.tickets.resolve`): hostname tokens in the title
and body against the identity layer, graded `high`, `medium` or `low`, and
never guessed onto the nearest machine.

Nothing here calls a model, and the import guard on `scripts/automation.py`
checks that it cannot: this module imports the connector tools and the
resolution, not the enrichment panel (which carries the autopilot's finding
rules) and not the plan.

Every answer is a `Read`: the value with the time it was read, or the error
with the time it was tried. A tool that does not answer is a line in the
note or no note at all — never a blank.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.recorder import AuditContext, record_event
from app.automation.notes.definitions import (
    AlertFact,
    DetectionFact,
    DeviceMatch,
    DeviceState,
    ProtectionFact,
    Read,
    TicketFact,
    VolumeFact,
)
from app.automation.triggers import Arrival
from app.connectors.autotask.client import AutotaskClient
from app.connectors.autotask.notes import AutotaskNoteWriter, build_note_writer
from app.connectors.bitdefender.tools import get_endpoint_protection, list_recent_detections
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for
from app.connectors.matching import normalise_hostname
from app.connectors.ninjaone.tools import get_device, list_open_alerts
from app.db.session import session_scope
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface
from app.models.site import Site
from app.tickets import resolve

logger = logging.getLogger(__name__)

NOTE_ACTOR = "automation-notes"
Call = Callable[[], Awaitable[Any]]


def _context(client_id: uuid.UUID, slug: str) -> AuditContext:
    return AuditContext.new(
        actor_id=NOTE_ACTOR, interface=Interface.SYSTEM, client_id=client_id, client_ref=slug
    )


class AlertTools:
    """The live `Tools`. One per client per poll; opens its own short sessions
    for the identity reads and the audit rows, and a connector per read."""

    def __init__(
        self, client_id: uuid.UUID, slug: str, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        self.client_id = client_id
        self.slug = slug
        self.maker = maker
        self.context = _context(client_id, slug)
        self._site_ids: dict[str, uuid.UUID] = {}

    # --- the audited read ------------------------------------------------------------

    async def _read(
        self, connector: ConnectorName, tool: str, call: Call, *, payload: dict[str, Any]
    ) -> Read[Any]:
        started = time.monotonic()
        at = dt.datetime.now(dt.UTC)
        try:
            value = await call()
        except (ConnectorError, ValueError) as exc:
            try:
                async with session_scope() as session:
                    await record_event(
                        session,
                        self.context,
                        event_type=AuditEventType.TOOL_CALL,
                        outcome=AuditOutcome.ERROR,
                        connector_name=connector,
                        tool_name=tool,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:300],
                        payload={"source": "alert notes", **payload},
                    )
            except Exception:
                logger.exception("could not record the failed %s read of %s", connector.value, tool)
            return Read.failed(_name(connector), f"{type(exc).__name__}: {exc}"[:200], at=at)
        async with session_scope() as session:
            await record_event(
                session,
                self.context,
                event_type=AuditEventType.TOOL_CALL,
                outcome=AuditOutcome.SUCCESS,
                connector_name=connector,
                tool_name=tool,
                duration_ms=int((time.monotonic() - started) * 1000),
                payload={"source": "alert notes", **payload},
            )
        return Read.of(_name(connector), value, at=at)

    async def _site_id(self, slug: str | None) -> uuid.UUID | None:
        if slug is None:
            return None
        if slug not in self._site_ids:
            async with self.maker() as session:
                found = (
                    await session.execute(
                        select(Site.id).where(Site.client_id == self.client_id, Site.slug == slug)
                    )
                ).scalar_one_or_none()
            if found is None:
                return None
            self._site_ids[slug] = found
        return self._site_ids[slug]

    # --- the protocol --------------------------------------------------------------------

    async def resolve_device(self, ticket: Arrival) -> DeviceMatch | None:
        site_id = await self._site_id(ticket.site_slug)
        async with self.maker() as session:
            candidates, hostnames = await resolve.by_hostname(
                session, self.client_id, site_id=site_id, texts=[ticket.title, ticket.description]
            )
        decided = resolve.decide(candidates, hostnames)
        if decided.device_id is None:
            top = decided.candidates[0] if decided.candidates else None
            if top is None:
                return None
            return DeviceMatch(
                device_id=top.device_id,
                name=top.name,
                site_slug=ticket.site_slug,
                confidence="low",
                method=top.method,
            )
        top = next(c for c in decided.candidates if c.device_id == str(decided.device_id))
        return DeviceMatch(
            device_id=top.device_id,
            name=top.name,
            site_slug=ticket.site_slug,
            confidence=decided.confidence or "low",
            method=decided.method,
        )

    async def device_state(self, device: DeviceMatch) -> Read[DeviceState]:
        site_id = await self._site_id(device.site_slug)

        async def call() -> DeviceState:
            detail = await get_device(
                str(self.client_id), device.name, str(site_id) if site_id else None
            )
            if not detail.found:
                raise ValueError(detail.message or "NinjaOne does not hold this device")
            return DeviceState(
                online=None if detail.offline is None else not detail.offline,
                last_contact=detail.last_contact,
                last_boot=detail.last_boot,
            )

        return await self._read(
            ConnectorName.NINJAONE, "get_device", call, payload={"device": device.name}
        )

    async def open_alerts(self, device: DeviceMatch) -> Read[list[AlertFact]]:
        site_id = await self._site_id(device.site_slug)
        key = normalise_hostname(device.name)

        async def call() -> list[AlertFact]:
            found = await list_open_alerts(str(self.client_id), str(site_id) if site_id else None)
            return [
                AlertFact(
                    subject=a.subject or a.condition or a.message or "alert",
                    created_at=a.created_at,
                )
                for a in found.alerts
                if a.device_name and normalise_hostname(a.device_name) == key
            ]

        return await self._read(
            ConnectorName.NINJAONE, "list_open_alerts", call, payload={"device": device.name}
        )

    async def volumes(self, device: DeviceMatch) -> Read[list[VolumeFact]]:
        site_id = await self._site_id(device.site_slug)

        async def call() -> list[VolumeFact]:
            detail = await get_device(
                str(self.client_id), device.name, str(site_id) if site_id else None
            )
            if not detail.found:
                raise ValueError(detail.message or "NinjaOne does not hold this device")
            return [
                VolumeFact(
                    name=v.volume,
                    capacity_gb=v.capacity_gb,
                    free_gb=v.free_gb,
                    free_percent=v.free_percent,
                )
                for v in detail.volumes
            ]

        return await self._read(
            ConnectorName.NINJAONE,
            "get_device",
            call,
            payload={"device": device.name, "want": "volumes"},
        )

    async def device_tickets(self, device: DeviceMatch, *, days: int) -> Read[list[TicketFact]]:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        key = normalise_hostname(device.name)

        async def call() -> list[TicketFact]:
            async with connector_for(self.client_id, ConnectorName.AUTOTASK) as built:
                connector = cast(AutotaskClient, built)
                rows, _ = await connector.list_tickets(
                    open_only=False, created_since=since, text=device.name
                )
                found: list[TicketFact] = []
                for site, ticket in rows:
                    text = f"{ticket.title or ''} {ticket.description or ''}"
                    if not any(normalise_hostname(t) == key for t in resolve.hostnames_in(text)):
                        continue
                    status = await connector.label_for("Tickets", "status", ticket.status)
                    found.append(
                        TicketFact(
                            ticket_id=str(ticket.id),
                            number=ticket.ticket_number or str(ticket.id),
                            title=ticket.title or "",
                            status=status,
                            is_open=ticket.is_open,
                            created_at=ticket.create_date,
                            site_slug=site.slug,
                        )
                    )
                return found

        return await self._read(
            ConnectorName.AUTOTASK,
            "list_tickets",
            call,
            payload={"device": device.name, "days": days},
        )

    async def endpoint_protection(self, device: DeviceMatch) -> Read[ProtectionFact]:
        site_id = await self._site_id(device.site_slug)

        async def call() -> ProtectionFact:
            found = await get_endpoint_protection(
                str(self.client_id), device.name, str(site_id) if site_id else None
            )
            return ProtectionFact(
                state=found.state,
                last_seen=found.last_seen,
                malware_last_24h=found.malware_detected_last_24h,
                still_infected=found.still_infected,
                isolated=found.isolated,
            )

        return await self._read(
            ConnectorName.BITDEFENDER,
            "get_endpoint_protection",
            call,
            payload={"device": device.name},
        )

    async def detections(self, device: DeviceMatch, *, hours: int) -> Read[list[DetectionFact]]:
        site_id = await self._site_id(device.site_slug)
        key = normalise_hostname(device.name)
        since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)

        async def call() -> list[DetectionFact]:
            found = await list_recent_detections(
                str(self.client_id),
                days=max(1, -(-hours // 24)),
                site_id=str(site_id) if site_id else None,
            )
            return [
                DetectionFact(
                    name=d.detection or "detection",
                    action=d.action,
                    status=d.status,
                    created_at=d.created,
                )
                for d in found.detections
                if d.endpoint
                and normalise_hostname(d.endpoint) == key
                and (d.created is None or d.created >= since)
            ]

        return await self._read(
            ConnectorName.BITDEFENDER,
            "list_recent_detections",
            call,
            payload={"device": device.name, "hours": hours},
        )

    async def alert_tickets(self, *, days: int) -> Read[list[TicketFact]]:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)

        async def call() -> list[TicketFact]:
            async with connector_for(self.client_id, ConnectorName.AUTOTASK) as built:
                connector = cast(AutotaskClient, built)
                rows, _ = await connector.list_tickets(open_only=False, created_since=since)
                sources: dict[int, str | None] = {}
                statuses: dict[int, str | None] = {}
                found: list[TicketFact] = []
                for site, ticket in rows:
                    if ticket.source is not None and ticket.source not in sources:
                        sources[ticket.source] = await connector.label_for(
                            "Tickets", "source", ticket.source
                        )
                    source = sources.get(ticket.source) if ticket.source is not None else None
                    if (source or "").casefold() != "monitoring alert":
                        continue
                    if ticket.status is not None and ticket.status not in statuses:
                        statuses[ticket.status] = await connector.label_for(
                            "Tickets", "status", ticket.status
                        )
                    found.append(
                        TicketFact(
                            ticket_id=str(ticket.id),
                            number=ticket.ticket_number or str(ticket.id),
                            title=ticket.title or "",
                            status=statuses.get(ticket.status)
                            if ticket.status is not None
                            else None,
                            is_open=ticket.is_open,
                            created_at=ticket.create_date,
                            description=ticket.description,
                            site_slug=site.slug,
                        )
                    )
                return found

        return await self._read(
            ConnectorName.AUTOTASK, "list_tickets", call, payload={"days": days, "want": "alerts"}
        )

    # --- beyond the protocol: what the poll also needs -----------------------------------

    async def closures(self, ticket_ids: list[str]) -> dict[str, dt.datetime | None]:
        """When each ticket completed, or None while open. One read."""

        async def call() -> dict[str, dt.datetime | None]:
            async with connector_for(self.client_id, ConnectorName.AUTOTASK) as built:
                connector = cast(AutotaskClient, built)
                rows = await connector.get_tickets_by_ids(
                    [int(t) for t in ticket_ids if t.isdigit()]
                )
                return {str(ticket.id): ticket.completed_date for _, ticket in rows}

        read = await self._read(
            ConnectorName.AUTOTASK, "get_tickets_by_ids", call, payload={"count": len(ticket_ids)}
        )
        if not read.ok or read.value is None:
            return {}
        return cast(dict[str, dt.datetime | None], read.value)


class OneShotWriter:
    """The note writer for a client, built and opened for each post and
    closed after it, so nothing holds a vendor connection between notes."""

    def __init__(self, client_id: uuid.UUID) -> None:
        self.client_id = client_id

    async def add_internal_note(self, ticket_id: str | int, *, title: str, body: str) -> Any:
        async with session_scope() as session:
            writer: AutotaskNoteWriter = await build_note_writer(session, self.client_id)
        async with writer:
            return await writer.add_internal_note(ticket_id, title=title, body=body)


async def writer_for(client_id: uuid.UUID) -> OneShotWriter:
    return OneShotWriter(client_id)


def _name(connector: ConnectorName) -> str:
    return {
        ConnectorName.NINJAONE: "NinjaOne",
        ConnectorName.AUTOTASK: "Autotask",
        ConnectorName.BITDEFENDER: "Bitdefender",
    }.get(connector, connector.value)

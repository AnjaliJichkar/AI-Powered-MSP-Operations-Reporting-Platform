"""The daily collector: vendor events into `collected_events`, idempotently, with coverage.

Run once a day (`scripts/collect_events.py`), per client. For every active site
with a mapping for the connector it reads the events of a window that ends now
and starts `days` back (a day, by default, with a day of overlap so a late
update to yesterday's event is still caught). Each event is written by its
vendor id: seen before, the row is updated; new, it is inserted. Every window
asked for is recorded in `collection_coverage` with how it ended, so nobody
mistakes a day that was never collected for a quiet one.

The first kind is Bitdefender detections. A later connector adds a `Reader`
for its own kind and nothing else changes. Everything here is scoped to the
client the run was asked for (hard rule 1), read-only (hard rule 2), and
audited per site (hard rule 4).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for, list_client_sites, resolve_client
from app.db.session import session_scope
from app.models import CollectedEvent, CollectionCoverage, Site, ToolMapping
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface

logger = logging.getLogger(__name__)

KIND_DETECTION = "detection"

#: A window is re-read this far back so an event updated after its day was
#: collected is caught. Rows are idempotent, so the overlap costs nothing.
OVERLAP = dt.timedelta(days=1)


class NotApplicable(Exception):
    """The site cannot have this kind of event: no licence for it. Not a failure."""


@dataclass(slots=True)
class VendorEvent:
    """One event as the collector stores it: the vendor's id, when, and a compact record."""

    external_id: str
    occurred_at: dt.datetime
    payload: dict[str, Any]
    vendor_updated_at: dt.datetime | None = None


@dataclass(slots=True)
class SiteOutcome:
    site_slug: str
    status: Literal["collected", "failed", "skipped", "not_applicable"]
    seen: int = 0
    new: int = 0
    detail: str = ""


@dataclass(slots=True)
class CollectionResult:
    connector: ConnectorName
    kind: str
    window_start: dt.datetime
    window_end: dt.datetime
    outcomes: list[SiteOutcome] = field(default_factory=list)

    @property
    def failed(self) -> list[SiteOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]

    @property
    def collected(self) -> list[SiteOutcome]:
        return [o for o in self.outcomes if o.status == "collected"]

    @property
    def skipped(self) -> list[SiteOutcome]:
        return [o for o in self.outcomes if o.status == "skipped"]

    @property
    def not_applicable(self) -> list[SiteOutcome]:
        return [o for o in self.outcomes if o.status == "not_applicable"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector.value,
            "kind": self.kind,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "collected": len(self.collected),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "not_applicable": len(self.not_applicable),
            "events_seen": sum(o.seen for o in self.outcomes),
            "events_new": sum(o.new for o in self.outcomes),
            "sites": [
                {
                    "site": o.site_slug,
                    "status": o.status,
                    "seen": o.seen,
                    "new": o.new,
                    "detail": o.detail,
                }
                for o in self.outcomes
            ],
        }


async def read_bitdefender_detections(
    client_id: uuid.UUID, site: Site, since: dt.datetime, until: dt.datetime
) -> list[VendorEvent]:
    """Incidents updated in the window, for one site, as compact events.

    The payload keeps what a report or an engineer asks for and leaves the
    vendor's counters and links out; the console link stays, since it is the
    way back to the full record.
    """
    async with connector_for(client_id, ConnectorName.BITDEFENDER, site.id) as connector:
        assert isinstance(connector, BitdefenderClient)
        # A site with two companies: detections can exist if either has EDR.
        licences = await connector.licences()
        if all(licence.edr_licensed is False for licence in licences.values()):
            raise NotApplicable("EDR is not licensed for this company (manageEventCorrelator)")
        # Ask only the company that holds the licence; the other has nothing to list.
        rows, truncated = await connector.list_incidents(
            since=since, until=until, sites=await connector.companies_with_edr()
        )
    if truncated:
        raise ConnectorError(
            "the incident listing stopped at the page cap; the window is not fully collected",
            connector=ConnectorName.BITDEFENDER.value,
        )
    events: list[VendorEvent] = []
    for _, incident in rows:
        occurred = incident.created or incident.last_updated
        if occurred is None:
            continue
        events.append(
            VendorEvent(
                external_id=incident.incident_id,
                occurred_at=occurred,
                vendor_updated_at=incident.last_updated,
                payload={
                    "incident_number": incident.incident_number,
                    "incident_type": incident.incident_type,
                    "status": incident.status,
                    "main_action": incident.main_action,
                    "priority": incident.priority,
                    "severity_score": incident.severity_score,
                    "attack_types": list(incident.attack_types or []),
                    "detection_name": incident.details.detection_name if incident.details else None,
                    "endpoint_id": incident.endpoint_id,
                    "endpoint_name": incident.endpoint_name,
                    "link": incident.incident_link,
                },
            )
        )
    return events


Reader = Callable[[uuid.UUID, Site, dt.datetime, dt.datetime], Any]

#: What the collector knows how to read: (connector, kind) -> reader.
READERS: dict[tuple[ConnectorName, str], Reader] = {
    (ConnectorName.BITDEFENDER, KIND_DETECTION): read_bitdefender_detections,
}


async def store_events(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    site_id: uuid.UUID,
    connector: ConnectorName,
    kind: str,
    events: list[VendorEvent],
    now: dt.datetime,
) -> int:
    """Upsert by vendor id. Returns how many were new."""
    if not events:
        return 0
    existing = {
        row
        for (row,) in (
            await session.execute(
                select(CollectedEvent.external_id).where(
                    CollectedEvent.connector_name == connector,
                    CollectedEvent.kind == kind,
                    CollectedEvent.external_id.in_([e.external_id for e in events]),
                )
            )
        ).all()
    }
    statement = insert(CollectedEvent).values(
        [
            {
                "client_id": client_id,
                "site_id": site_id,
                "connector_name": connector,
                "kind": kind,
                "external_id": event.external_id,
                "occurred_at": event.occurred_at,
                "vendor_updated_at": event.vendor_updated_at,
                "payload": event.payload,
                "first_collected_at": now,
                "last_collected_at": now,
            }
            for event in events
        ]
    )
    # A second sight updates what the vendor may have changed and never the
    # first-collected time or the site the event was first placed at.
    statement = statement.on_conflict_do_update(
        constraint="uq_collected_events_vendor_event",
        set_={
            "payload": statement.excluded.payload,
            "vendor_updated_at": statement.excluded.vendor_updated_at,
            "occurred_at": statement.excluded.occurred_at,
            "last_collected_at": statement.excluded.last_collected_at,
        },
    )
    await session.execute(statement)
    return sum(1 for event in events if event.external_id not in existing)


async def collect(
    client_ref: str | uuid.UUID,
    *,
    connector: ConnectorName = ConnectorName.BITDEFENDER,
    kind: str = KIND_DETECTION,
    actor: str,
    days: int = 1,
    now: dt.datetime | None = None,
    reader: Reader | None = None,
) -> CollectionResult:
    """Collect one kind of event for every mapped, active site of one client.

    A site that fails does not stop the run; its coverage row says `failed`
    and why. A site with no mapping for the connector is skipped and gets no
    coverage row: there was nothing to ask.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    read = reader or READERS.get((connector, kind))
    if read is None:
        raise ValueError(f"no reader for {connector.value} {kind}")
    now = now or dt.datetime.now(dt.UTC)
    window_start = now - dt.timedelta(days=days) - OVERLAP
    result = CollectionResult(
        connector=connector, kind=kind, window_start=window_start, window_end=now
    )

    async with session_scope() as session:
        client = await resolve_client(session, client_ref)
        sites = await list_client_sites(session, client.id)
        mapped = {
            site_id
            for (site_id,) in (
                await session.execute(
                    select(ToolMapping.site_id).where(
                        ToolMapping.client_id == client.id,
                        ToolMapping.site_id.is_not(None),
                        ToolMapping.connector_name == connector,
                    )
                )
            ).all()
        }
        client_id, client_slug = client.id, client.slug

    audit = AuditContext.new(
        actor_id=actor, interface=Interface.SYSTEM, client_id=client_id, client_ref=client_slug
    )
    for site in sites:
        if site.id not in mapped:
            result.outcomes.append(
                SiteOutcome(site.slug, "skipped", detail=f"no {connector.value} mapping")
            )
            continue
        outcome = SiteOutcome(site.slug, "collected")
        error: str | None = None
        reason: str | None = None
        try:
            events = await read(client_id, site, window_start, now)
            async with session_scope() as session:
                outcome.new = await store_events(
                    session,
                    client_id=client_id,
                    site_id=site.id,
                    connector=connector,
                    kind=kind,
                    events=events,
                    now=now,
                )
            outcome.seen = len(events)
        except NotApplicable as exc:
            reason = str(exc)[:500]
            outcome.status, outcome.detail = "not_applicable", reason
        except Exception as exc:  # one site must not stop the rest
            error = f"{type(exc).__name__}: {exc}"[:500]
            outcome.status, outcome.detail = "failed", error
            logger.warning(
                "events %s %s site=%s failed: %s", connector.value, kind, site.slug, error
            )
        async with session_scope() as session:
            session.add(
                CollectionCoverage(
                    client_id=client_id,
                    site_id=site.id,
                    connector_name=connector,
                    kind=kind,
                    window_start=window_start,
                    window_end=now,
                    status=outcome.status,
                    events_seen=outcome.seen,
                    events_new=outcome.new,
                    error=error or reason,
                    collected_at=now,
                    collected_by=actor,
                )
            )
            await record_event(
                session,
                audit,
                event_type=AuditEventType.EVENTS_COLLECTED,
                outcome=AuditOutcome.ERROR if error else AuditOutcome.SUCCESS,
                connector_name=connector,
                error_type=error.split(":", 1)[0] if error else None,
                error_message=error,
                payload={
                    "site": site.slug,
                    "kind": kind,
                    "window_start": window_start.isoformat(),
                    "window_end": now.isoformat(),
                    "events_seen": outcome.seen,
                    "events_new": outcome.new,
                    "status": outcome.status,
                },
            )
        result.outcomes.append(outcome)
    return result


async def coverage_for(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    site_id: uuid.UUID,
    connector: ConnectorName,
    kind: str,
    start: dt.datetime,
    end: dt.datetime,
) -> tuple[bool, list[tuple[dt.datetime, dt.datetime]]]:
    """Is `[start, end)` fully covered by successful collection windows? And the gaps if not.

    A report asks this before it counts events for a period: an uncovered day
    is a day nobody asked the vendor about, not a day with no events.
    """
    rows = (
        await session.execute(
            select(CollectionCoverage.window_start, CollectionCoverage.window_end)
            .where(
                CollectionCoverage.client_id == client_id,
                CollectionCoverage.site_id == site_id,
                CollectionCoverage.connector_name == connector,
                CollectionCoverage.kind == kind,
                CollectionCoverage.status == "collected",
                CollectionCoverage.window_end > start,
                CollectionCoverage.window_start < end,
            )
            .order_by(CollectionCoverage.window_start)
        )
    ).all()
    gaps: list[tuple[dt.datetime, dt.datetime]] = []
    cursor = start
    for window_start, window_end in rows:
        if window_start > cursor:
            gaps.append((cursor, min(window_start, end)))
        cursor = max(cursor, window_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return not gaps, gaps

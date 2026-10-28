"""GravityZone's last-seen for the rows paired with lost devices: read once, kept.

GravityZone's endpoint list carries no last-seen. It does carry, on request
(`includeScanLogs`), the date of the last successful scan, and a row scanned
within the RMM threshold is alive without another call. For the rest,
`getManagedEndpointDetails.lastSeen` is one call per row, so:

- only the rows the join paired with a device NinjaOne has lost are asked,
  and only when the device's last NinjaOne contact is younger than
  `DEVICE_RETIRE_DAYS`: a machine lost for longer is not checked, and its
  verdict says so;
- the answer is written on the record's `DeviceVendorLink`
  (`vendor_last_seen_at`, `vendor_checked_at`) and read back from there for
  `BITDEFENDER_LAST_SEEN_REFRESH_HOURS`, so the board, the resolution and the
  review share one read a day rather than one each;
- calls run under `BITDEFENDER_DETAIL_CONCURRENCY`, and every run logs how
  many rows were wanted, alive by scan date, taken from links, fetched, or
  not checked.

`read_last_seen` is the one entry point; `candidates_for` picks the rows out
of an assessment the same way for every reader.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.errors import ArgumentRejected, ConnectorError, NotFound
from app.connectors.factory import connector_for
from app.connectors.scope import SiteRef
from app.coverage.core import (
    Assessment,
    ManagedDevice,
    ToolEndpoint,
    endpoint_alive,
    lost_pairings,
)
from app.db.session import session_scope
from app.models.device import DeviceVendorLink
from app.models.enums import ConnectorName
from app.settings import get_settings

logger = logging.getLogger(__name__)

TOOL = "bitdefender"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One vendor row a lost device paired with, and how long the device has been lost."""

    external_id: str
    ref: SiteRef
    device_id: str
    lost_days: int | None
    """Days since NinjaOne's last contact; None when it never had one."""
    row: ToolEndpoint


@dataclass(slots=True)
class LastSeenReads:
    """What one run of the read did, for the log and the snapshot."""

    lost_devices: int = 0
    alive_by_scan: int = 0
    """Rows whose bulk scan date already said alive: not asked."""
    beyond_window: int = 0
    """Devices lost for longer than DEVICE_RETIRE_DAYS: not checked."""
    from_links: int = 0
    """Dates taken from links checked within the refresh window."""
    fetched: int = 0
    """Detail calls made this run."""
    failed: bool = False
    unchecked_devices: list[str] = field(default_factory=list)
    fetched_dates: dict[str, dt.datetime | None] = field(default_factory=dict)
    """What the vendor answered this run, by vendor id: a caller that holds links
    the store could not see yet (the resolution, on its first day) writes them."""

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "lost_devices": self.lost_devices,
            "alive_by_scan": self.alive_by_scan,
            "beyond_window": self.beyond_window,
            "from_links": self.from_links,
            "fetched": self.fetched,
            "failed": self.failed,
        }


def candidates_for(
    assessment: Assessment,
    *,
    rows_by_id: Mapping[str, ToolEndpoint],
    refs: Mapping[str, SiteRef],
    devices_by_id: Mapping[str, ManagedDevice],
    now: dt.datetime,
    threshold_days: int,
    reads: LastSeenReads,
    lost_days_by_device: Mapping[str, int | None] | None = None,
) -> list[Candidate]:
    """The rows worth a last-seen read: paired with a lost device inside the
    retire window, carrying an agent, and not already alive by their scan date.
    `lost_days_by_device` overrides the age computed from the device's last
    contact, for a reader that holds the age but not the date (the review)."""
    retire_days = get_settings().device_retire_days
    found: list[Candidate] = []
    for device_id, ids in lost_pairings(assessment, TOOL).items():
        reads.lost_devices += 1
        device = devices_by_id.get(device_id)
        rows = [rows_by_id[i] for i in ids if i in rows_by_id]
        if any(endpoint_alive(r, now=now, days=threshold_days) for r in rows):
            reads.alive_by_scan += 1
            continue
        if lost_days_by_device is not None:
            lost_days = lost_days_by_device.get(device_id)
        else:
            lost_days = (
                (now - device.last_contact).days
                if device is not None and device.last_contact is not None
                else None
            )
        if lost_days is not None and lost_days > retire_days:
            reads.beyond_window += 1
            reads.unchecked_devices.append(device_id)
            continue
        for row in rows:
            if row.state == "absent" or not row.external_id or row.external_id not in refs:
                continue
            found.append(
                Candidate(row.external_id, refs[row.external_id], device_id, lost_days, row)
            )
    return found


async def read_last_seen(
    client_id: uuid.UUID,
    candidates: Sequence[Candidate],
    *,
    now: dt.datetime,
    reads: LastSeenReads,
) -> dict[str, dt.datetime | None]:
    """`lastSeen` per vendor id for the candidates: from the links where fresh
    enough, from the vendor otherwise, written back to the links. A vendor
    that does not answer sets `reads.failed` and dates nothing."""
    settings = get_settings()
    fresh_after = now - dt.timedelta(hours=settings.bitdefender_last_seen_refresh_hours)
    ids = sorted({c.external_id for c in candidates})
    if not ids:
        _log(client_id, reads)
        return {}
    found: dict[str, dt.datetime | None] = {}
    links = await _load_links(client_id, ids)
    for external_id, link in links.items():
        checked = link.vendor_checked_at
        if checked is not None and checked >= fresh_after:
            found[external_id] = link.vendor_last_seen_at
    reads.from_links = len(found)
    missing = [c for c in candidates if c.external_id not in found]
    by_id = {c.external_id: c for c in missing}
    if not by_id:
        _log(client_id, reads)
        return found
    semaphore = asyncio.Semaphore(settings.bitdefender_detail_concurrency)

    async def one(connector: BitdefenderClient, c: Candidate) -> tuple[str, dt.datetime | None]:
        async with semaphore:
            try:
                detail = await connector.get_endpoint_details(c.ref, c.external_id)
            except (NotFound, ArgumentRejected):
                return c.external_id, None
            return c.external_id, detail.last_seen

    try:
        async with connector_for(client_id, ConnectorName.BITDEFENDER) as connector:
            assert isinstance(connector, BitdefenderClient)
            answers = await asyncio.gather(*(one(connector, c) for c in by_id.values()))
    except ConnectorError as exc:
        reads.failed = True
        logger.warning("last-seen: endpoint details did not answer for %s: %s", client_id, exc)
        _log(client_id, reads)
        return found
    reads.fetched = len(answers)
    fetched = dict(answers)
    reads.fetched_dates = fetched
    found.update(fetched)
    await _store_links(links, fetched, now)
    _log(client_id, reads)
    return found


async def _load_links(client_id: uuid.UUID, ids: Sequence[str]) -> dict[str, DeviceVendorLink]:
    """The client's Bitdefender links for these vendor ids, by vendor id."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(DeviceVendorLink).where(
                    DeviceVendorLink.client_id == client_id,
                    DeviceVendorLink.connector_name == ConnectorName.BITDEFENDER,
                    DeviceVendorLink.external_id.in_(list(ids)),
                )
            )
        ).scalars()
        return {link.external_id: link for link in rows}


async def _store_links(
    links: Mapping[str, DeviceVendorLink],
    fetched: Mapping[str, dt.datetime | None],
    now: dt.datetime,
) -> None:
    """The dates just read, onto the links that exist; a record with no link yet
    (the resolution has not run) is read again next time."""
    to_store = [(links[i], when) for i, when in fetched.items() if i in links]
    if not to_store:
        return
    async with session_scope() as session:
        for link, when in to_store:
            kept = await session.merge(link)
            kept.vendor_last_seen_at = when
            kept.vendor_checked_at = now


def _log(client_id: uuid.UUID, reads: LastSeenReads) -> None:
    logger.info(
        "last-seen: client=%s lost devices %d, alive by scan date %d, beyond the retire "
        "window %d, dates from links %d, detail calls %d%s",
        client_id,
        reads.lost_devices,
        reads.alive_by_scan,
        reads.beyond_window,
        reads.from_links,
        reads.fetched,
        " (the vendor did not answer)" if reads.failed else "",
    )

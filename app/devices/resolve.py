"""Daily device resolution: the coverage join, written down.

`resolve_devices(client)` reads the same cached inventories the coverage
board reads — NinjaOne devices with their MACs and serials, Bitdefender
endpoints, ConnectSecure assets — and runs the same four passes
(`core.assess`: exact hostname, NetBIOS key, MAC, serial; within a site;
one-to-one per pass, twins told apart by MAC or serial). What the board
computes and forgets, this keeps:

- every NinjaOne device is a `Device` with a `ninjaone` link; a vendor
  record a pass pairs with a device becomes a link on it, `matched_by` the
  pass; a record nothing pairs becomes a device of its own with a
  `vendor_only` link; a record a pass could not choose a device for goes to
  the unresolved queue with the candidates named;
- a record already linked is left alone: reruns update `last_seen_at` and
  nothing else, so the job is idempotent on the same inventories, and a
  person's `manual` link is never undone by the job;
- a vendor that did not answer changes nothing: its passes do not run, its
  links are neither made, moved nor deactivated, and a lost device keeps the
  status it had; a NinjaOne key query (MACs, serials) that did not answer
  stops every tool's passes this run, because they would pair less and write
  the difference down as vendor-only devices;
- a NinjaOne device past the coverage threshold is `rmm_stale`, or
  `rmm_agent_down` when a vendor's row it is linked to is still alive; a
  device no vendor has returned for `DEVICE_RETIRE_DAYS` is `retired`; a
  record a vendor stops returning goes `active = False`. Nothing is deleted.

One audit row per run (`devices_resolved`) with the counts. Names stay in the
tables; the result the script prints is counts and ids.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.connectsecure.client import ConnectSecureClient
from app.connectors.cove.client import CoveClient
from app.connectors.errors import ConnectorError, ConnectorNotConfigured
from app.connectors.factory import connector_for, resolve_client
from app.connectors.matching import normalise_hostname
from app.connectors.ninjaone.client import NinjaOneClient
from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.scope import SiteRef
from app.coverage.compute import device_macs, device_serials
from app.coverage.config import COVERAGE_TOOLS, rmm_stale_days
from app.coverage.core import (
    ManagedDevice,
    ToolEndpoint,
    ToolInventory,
    assess,
    endpoint_alive,
)
from app.coverage.naming import connector_name_of
from app.db.session import session_scope
from app.devices.lastseen import LastSeenReads, candidates_for, read_last_seen
from app.models.device import Device, DeviceUnresolved, DeviceVendorLink
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface
from app.settings import get_settings

logger = logging.getLogger(__name__)

NINJAONE = ConnectorName.NINJAONE.value


@dataclass(slots=True)
class ResolutionResult:
    client_slug: str
    devices_created: int = 0
    links_created: Counter[str] = field(default_factory=Counter)
    """New links by `matched_by`."""
    links_seen: int = 0
    links_moved: int = 0
    """`vendor_only` links a pass claimed for a managed device this run."""
    links_deactivated: int = 0
    unresolved_open: int = 0
    unresolved_new: int = 0
    rmm_stale: int = 0
    rmm_agent_down: int = 0
    """Devices NinjaOne has lost that a linked vendor row still hears from."""
    retired: int = 0
    tools_read: dict[str, str] = field(default_factory=dict)
    """Per tool: `read` or why not."""
    incomplete: str | None = None
    """Why no tool's passes ran this run: a NinjaOne key query did not answer."""
    undated: list[str] = field(default_factory=list)
    """Tools whose last-seen read failed this run: their lost devices keep their status."""
    last_seen_reads: dict[str, int | bool] = field(default_factory=dict)
    """What the last-seen read did this run (`LastSeenReads.as_dict`)."""
    last_seen_dates: dict[str, dt.datetime | None] = field(default_factory=dict)
    """The dates the vendor answered this run, written onto the links this run holds
    (the store cannot see a link made in the same transaction). Not reported."""
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.client_slug,
            "devices_created": self.devices_created,
            "links_created": dict(self.links_created),
            "links_seen": self.links_seen,
            "links_moved": self.links_moved,
            "links_deactivated": self.links_deactivated,
            "unresolved_open": self.unresolved_open,
            "unresolved_new": self.unresolved_new,
            "rmm_stale": self.rmm_stale,
            "rmm_agent_down": self.rmm_agent_down,
            "retired": self.retired,
            "tools": self.tools_read,
            "incomplete": self.incomplete,
            "undated": self.undated,
            "last_seen_reads": self.last_seen_reads,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class VendorRecord:
    """One vendor row as the resolution sees it: what a link or a device is made from."""

    connector: str
    """The **connector**'s name, not the coverage board's tool key. They are
    the same word for Bitdefender and ConnectSecure and not for Backup, which
    reads Cove — and a link row stores a connector."""
    external_id: str
    site_id: uuid.UUID
    name: str
    node_class: str | None = None
    os: str | None = None
    macs: frozenset[str] = frozenset()
    serial: str | None = None
    last_contact: dt.datetime | None = None
    endpoint: ToolEndpoint | None = None
    ref: SiteRef | None = None
    """The vendor organisation the row came from, for a per-endpoint read."""


# --- reading -------------------------------------------------------------------------


async def read_inventories(
    client_id: uuid.UUID, *, now: dt.datetime, result: ResolutionResult
) -> tuple[
    list[tuple[SiteRef, NinjaDevice]],
    dict[int, frozenset[str]],
    dict[int, str],
    dict[str, tuple[list[VendorRecord], frozenset[uuid.UUID]]],
    set[uuid.UUID],
]:
    """Every cached inventory: NinjaOne with MACs and serials, and each tool's records.

    A NinjaOne key query that does not answer leaves `result.incomplete` set
    and reads no tool: the passes do not run against half a population.
    """
    async with connector_for(client_id, ConnectorName.NINJAONE) as ninja:
        assert isinstance(ninja, NinjaOneClient)
        devices, _ = await ninja.list_devices()
        ninja_sites = {ref.site_id for ref in ninja.scope.targets}
        macs: dict[int, frozenset[str]] = {}
        serials: dict[int, str] = {}
        try:
            macs = await device_macs(ninja)
            serials = await device_serials(ninja)
        except ConnectorError as exc:
            result.incomplete = f"NinjaOne did not answer for the devices' MACs or serials: {exc}"
            logger.warning("device resolution: %s", result.incomplete)
    tools: dict[str, tuple[list[VendorRecord], frozenset[uuid.UUID]]] = {}
    for tool in COVERAGE_TOOLS:
        if result.incomplete is not None:
            result.tools_read[tool.key] = f"not run: {result.incomplete}"
            continue
        if tool.connector is None or tool.reader is None:
            result.tools_read[tool.key] = "not assessed"
            continue
        try:
            async with connector_for(client_id, tool.connector) as connector:
                if isinstance(connector, BitdefenderClient):
                    rows, _ = await connector.list_endpoints()
                    endpoints = tool.reader(rows, now)
                    records = [
                        VendorRecord(
                            connector=tool.connector.value,
                            external_id=endpoint.id,
                            site_id=site.site_id,
                            name=endpoint.name or "",
                            os=endpoint.operating_system_version,
                            macs=judged.macs,
                            endpoint=judged,
                            ref=site,
                        )
                        for (site, endpoint), judged in zip(rows, endpoints, strict=True)
                    ]
                elif isinstance(connector, ConnectSecureClient):
                    assets, _ = await connector.list_assets()
                    endpoints = tool.reader(assets, now)
                    records = [
                        VendorRecord(
                            connector=tool.connector.value,
                            external_id=asset.id,
                            site_id=site.site_id,
                            name=asset.host_name or "",
                            os=asset.os_name,
                            macs=judged.macs,
                            serial=judged.serial,
                            endpoint=judged,
                        )
                        for (site, asset), judged in zip(assets, endpoints, strict=True)
                    ]
                elif isinstance(connector, CoveClient):
                    # Only the machines: Cove counts a customer's Microsoft
                    # 365 tenant as a device, and a tenant is not a machine
                    # the identity layer should ever hold a link for.
                    backups = [
                        pair
                        for pair in await connector.list_fleet_with_sites()
                        if pair[1].is_machine
                    ]
                    judged_backups = tool.reader(backups, now)
                    records = [
                        VendorRecord(
                            connector=tool.connector.value,
                            external_id=str(machine.account_id),
                            site_id=where.site_id,
                            name=machine.match_key,
                            os=machine.os_version,
                            macs=judged.macs,
                            endpoint=judged,
                        )
                        for (where, machine), judged in zip(backups, judged_backups, strict=True)
                    ]
                else:  # pragma: no cover - a tool entry naming a connector this cannot read
                    result.tools_read[tool.key] = "no reader"
                    continue
                sites = frozenset(ref.site_id for ref in connector.scope.targets)
        except ConnectorNotConfigured as exc:
            result.tools_read[tool.key] = f"not configured: {exc}"
            continue
        except ConnectorError as exc:
            result.tools_read[tool.key] = f"did not answer: {exc}"
            continue
        tools[tool.key] = (records, sites)
        result.tools_read[tool.key] = "read"
    return devices, macs, serials, tools, ninja_sites


# --- the resolution --------------------------------------------------------------------


def _key(name: str) -> str:
    return normalise_hostname(name)


def _netbios(name: str) -> str:
    return _key(name)[:15]


class _State:
    """The client's devices and links, loaded once and updated in memory."""

    def __init__(
        self,
        client_id: uuid.UUID,
        devices: list[Device],
        links: list[DeviceVendorLink],
        unresolved: list[DeviceUnresolved],
    ) -> None:
        self.client_id = client_id
        self.devices: dict[uuid.UUID, Device] = {d.id: d for d in devices}
        self.links: dict[tuple[str, str], DeviceVendorLink] = {
            (link.connector_name.value, link.external_id): link for link in links
        }
        self.links_by_device: dict[uuid.UUID, list[DeviceVendorLink]] = defaultdict(list)
        for link in links:
            self.links_by_device[link.device_id].append(link)
        self.unresolved: dict[tuple[str, str], DeviceUnresolved] = {
            (u.connector_name.value, u.external_id): u for u in unresolved
        }

    def link(self, connector: str, external_id: str) -> DeviceVendorLink | None:
        return self.links.get((connector, external_id))

    def add_link(self, link: DeviceVendorLink) -> None:
        self.links[(link.connector_name.value, link.external_id)] = link
        self.links_by_device[link.device_id].append(link)

    def move_link(self, link: DeviceVendorLink, device: Device) -> None:
        """The same row, now another device's: a record has one link row, ever."""
        self.links_by_device[link.device_id] = [
            other for other in self.links_by_device[link.device_id] if other is not link
        ]
        link.device_id = device.id
        self.links_by_device[device.id].append(link)

    def ninja_link(self, device_id: uuid.UUID) -> DeviceVendorLink | None:
        for link in self.links_by_device.get(device_id, []):
            if link.connector_name is ConnectorName.NINJAONE and link.active:
                return link
        return None


def _new_device(
    session: AsyncSession, state: _State, record: VendorRecord, *, now: dt.datetime
) -> Device:
    device = Device(
        id=uuid.uuid4(),
        client_id=state.client_id,
        site_id=record.site_id,
        canonical_name=record.name,
        netbios_key=_netbios(record.name),
        serials=[record.serial] if record.serial else [],
        macs=sorted(record.macs),
        node_class=record.node_class,
        os=record.os,
        status="active",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(device)
    state.devices[device.id] = device
    return device


def _new_link(
    session: AsyncSession,
    state: _State,
    device: Device,
    record: VendorRecord,
    *,
    matched_by: str,
    now: dt.datetime,
) -> DeviceVendorLink:
    link = DeviceVendorLink(
        client_id=state.client_id,
        device_id=device.id,
        connector_name=ConnectorName(record.connector),
        external_id=record.external_id,
        vendor_name=record.name or None,
        matched_by=matched_by,
        first_seen_at=now,
        last_seen_at=now,
        active=True,
    )
    session.add(link)
    state.add_link(link)
    return link


def _touch(device: Device, record: VendorRecord, *, now: dt.datetime) -> None:
    """A vendor returned the record: the device was seen, and learns what the row carries."""
    device.last_seen_at = max(device.last_seen_at, now)
    if record.macs:
        device.macs = sorted(set(device.macs) | record.macs)
    if record.serial and record.serial not in device.serials:
        device.serials = [*device.serials, record.serial]
    if record.connector == NINJAONE:
        # NinjaOne's name is the canonical one, and a rename follows.
        if record.name and device.canonical_name != record.name:
            device.canonical_name = record.name
            device.netbios_key = _netbios(record.name)
        device.node_class = record.node_class or device.node_class
        device.os = record.os or device.os
    elif not device.os and record.os:
        device.os = record.os


def _seen(link: DeviceVendorLink, record: VendorRecord, *, now: dt.datetime) -> None:
    link.last_seen_at = now
    link.active = True
    if record.name:
        link.vendor_name = record.name


async def resolve_devices(
    client_ref: str | uuid.UUID, *, actor: str, now: dt.datetime | None = None
) -> ResolutionResult:
    """Run the day's resolution for one client. Read-only at the vendors; writes here only."""
    started = time.perf_counter()
    now = now or dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        client = await resolve_client(session, client_ref)
        client_id, client_slug = client.id, client.slug
    result = ResolutionResult(client_slug=client_slug)
    devices_rows, macs, serials, tools, _ninja_sites = await read_inventories(
        client_id, now=now, result=result
    )
    threshold = rmm_stale_days()
    retire_after = dt.timedelta(days=get_settings().device_retire_days)

    async with session_scope() as session:
        state = _State(
            client_id,
            list(
                (await session.execute(select(Device).where(Device.client_id == client_id)))
                .scalars()
                .all()
            ),
            list(
                (
                    await session.execute(
                        select(DeviceVendorLink).where(DeviceVendorLink.client_id == client_id)
                    )
                )
                .scalars()
                .all()
            ),
            list(
                (
                    await session.execute(
                        select(DeviceUnresolved).where(
                            DeviceUnresolved.client_id == client_id,
                            DeviceUnresolved.resolved_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            ),
        )
        seen_links: set[tuple[str, str]] = set()

        # --- 1. NinjaOne: the population's own records -----------------------------
        ninja_records = [
            VendorRecord(
                connector=NINJAONE,
                external_id=str(device.id),
                site_id=site.site_id,
                name=device.hostname,
                node_class=device.node_class,
                os=device.os_family,
                macs=macs.get(device.id, frozenset()),
                serial=serials.get(device.id),
                last_contact=device.last_contact,
            )
            for site, device in devices_rows
        ]
        last_contact_of: dict[uuid.UUID, dt.datetime | None] = {}
        # The MACs and serial NinjaOne reports this run, which is what the board's
        # MAC and serial passes use; a device also remembers the MACs and serials
        # its other vendors carried, and those are data for `show`, not keys for a
        # pass.
        ninja_macs_of: dict[uuid.UUID, frozenset[str]] = {}
        ninja_serials_of: dict[uuid.UUID, frozenset[str]] = {}
        for record in ninja_records:
            link = state.link(NINJAONE, record.external_id)
            if link is None:
                adopted = _adopt_vendor_only(state, record)
                if adopted is None:
                    device = _new_device(session, state, record, now=now)
                    result.devices_created += 1
                else:
                    device = adopted
                link = _new_link(session, state, device, record, matched_by=NINJAONE, now=now)
                result.links_created[NINJAONE] += 1
            else:
                device = state.devices[link.device_id]
                _seen(link, record, now=now)
                result.links_seen += 1
            _touch(device, record, now=now)
            seen_links.add((NINJAONE, record.external_id))
            last_contact_of[device.id] = record.last_contact
            ninja_macs_of[device.id] = frozenset(record.macs)
            ninja_serials_of[device.id] = frozenset({record.serial} if record.serial else ())

        # --- 2. each tool: the passes over the records not yet linked ---------------
        for tool_key, (records, answered) in tools.items():
            # A link row and a queue row store a **connector**; the coverage
            # board's tool key is a different word for Backup, which reads
            # Cove. Keying either by the tool makes every record look new.
            connector = connector_name_of(tool_key)
            by_external = {r.external_id: r for r in records}
            # A link a pass or a person made stands. A `vendor_only` link is the
            # absence of a pairing, remembered, and is offered to the passes again
            # every run: the machine may have come back to NinjaOne since.
            for record in records:
                link = state.link(connector, record.external_id)
                if link is not None and link.matched_by != "vendor_only":
                    _seen(link, record, now=now)
                    _touch(state.devices[link.device_id], record, now=now)
                    seen_links.add((connector, record.external_id))
                    result.links_seen += 1
            unlinked = [
                r
                for r in records
                if r.endpoint
                and (
                    (link := state.link(connector, r.external_id)) is None
                    or link.matched_by == "vendor_only"
                )
            ]
            open_ids = {r.external_id for r in unlinked}
            # The passes are the board's exactly: the same population — the devices
            # NinjaOne returned this run, with NinjaOne's MACs, serial and last
            # contact, an RMM-stale device taking part as it does there — and every
            # record, linked or not, so a device an exact pairing takes is not free
            # for a weaker pass to give away. A device made from another vendor's
            # record is not a candidate, or the outcome would depend on which tool
            # ran first.
            managed = [d for d in state.devices.values() if d.id in last_contact_of]
            population = [
                ManagedDevice(
                    site_id=d.site_id,
                    site_slug=str(d.site_id),
                    site_name=str(d.site_id),
                    device_id=str(d.id),
                    name=d.canonical_name,
                    node_class=d.node_class,
                    last_contact=last_contact_of[d.id],
                    macs=ninja_macs_of[d.id],
                    serials=ninja_serials_of[d.id],
                )
                for d in managed
            ]
            inventory = ToolInventory(
                endpoints=[r.endpoint for r in records if r.endpoint is not None],
                sites=answered,
            )
            assessment = assess(
                population,
                {tool_key: inventory},
                now=now,
                ninjaone_stale_days=threshold,
            )
            if tool_key == "bitdefender":
                # GravityZone's list carries no last-seen: read it for the rows paired
                # with lost devices, as the board does, and judge again with the dates.
                # A read that failed leaves those devices undecided, links and all.
                dated, failed = await _bitdefender_dates(
                    client_id,
                    assessment,
                    by_external,
                    population,
                    now=now,
                    threshold=threshold,
                    result=result,
                )
                if failed:
                    result.undated.append(tool_key)
                if dated or failed:
                    inventory = ToolInventory(
                        endpoints=[r.endpoint for r in records if r.endpoint is not None],
                        sites=answered,
                    )
                    assessment = assess(
                        population,
                        {tool_key: inventory},
                        now=now,
                        ninjaone_stale_days=threshold,
                        undated_tools=(tool_key,) if failed else (),
                    )
            claimed: set[str] = set()
            candidates_of: dict[str, list[dict[str, str]]] = defaultdict(list)
            pass_of: dict[str, str] = {}
            for outcome in assessment.devices:
                verdict = outcome.tools[tool_key]
                device = state.devices[uuid.UUID(outcome.device.device_id)]
                if verdict.matched_by and verdict.endpoint_ids:
                    for external_id in verdict.endpoint_ids:
                        record = by_external[external_id]
                        former = state.link(connector, external_id)
                        if former is None:
                            _new_link(
                                session,
                                state,
                                device,
                                record,
                                matched_by=verdict.matched_by,
                                now=now,
                            )
                            result.links_created[verdict.matched_by] += 1
                        elif former.matched_by == "manual":
                            pass  # a person's word
                        elif former.device_id == device.id:
                            if former.matched_by == "vendor_only":
                                # The device NinjaOne adopted for its name was this
                                # record's vendor-only device: the same link, now a
                                # pairing the pass made.
                                former.matched_by = verdict.matched_by
                                former.note = f"claimed by the {verdict.matched_by} pass"
                                result.links_moved += 1
                        else:
                            # A vendor-only link the pass now places, or a pairing the
                            # inventories have since moved. The device it leaves
                            # keeps nothing else and retires in time; nothing is deleted.
                            state.move_link(former, device)
                            former.matched_by = verdict.matched_by
                            former.note = f"claimed by the {verdict.matched_by} pass"
                            _seen(former, record, now=now)
                            result.links_moved += 1
                        _touch(device, record, now=now)
                        seen_links.add((connector, external_id))
                        claimed.add(external_id)
                elif verdict.state in ("unmatched", "rmm_stale") and verdict.endpoint_ids:
                    # Ambiguous, for a live device or a lost one: the records a pass
                    # could not choose between, queued with every device it named.
                    for external_id in verdict.endpoint_ids:
                        if external_id not in open_ids:
                            continue  # a standing link is kept; the queue is for the open
                        candidates_of[external_id].append(
                            {"device_id": str(device.id), "name": device.canonical_name}
                        )
                        pass_of[external_id] = verdict.found_by or "exact"
            # 2b. the queue, and devices for what nothing claimed
            for record in unlinked:
                if record.external_id in claimed:
                    continue
                key = (connector, record.external_id)
                kept = state.link(connector, record.external_id)
                if kept is not None:
                    # Still nobody's but its own: the vendor-only link stands, seen.
                    _seen(kept, record, now=now)
                    _touch(state.devices[kept.device_id], record, now=now)
                    seen_links.add(key)
                    result.links_seen += 1
                if record.external_id in candidates_of:
                    entry = state.unresolved.get(key)
                    if entry is None:
                        entry = DeviceUnresolved(
                            client_id=client_id,
                            site_id=record.site_id,
                            connector_name=ConnectorName(connector_name_of(tool_key)),
                            external_id=record.external_id,
                            vendor_name=record.name or None,
                            found_by=pass_of[record.external_id],
                            candidates=candidates_of[record.external_id],
                            first_seen_at=now,
                            last_seen_at=now,
                        )
                        session.add(entry)
                        state.unresolved[key] = entry
                        result.unresolved_new += 1
                    else:
                        entry.last_seen_at = now
                        entry.candidates = candidates_of[record.external_id]
                        entry.found_by = pass_of[record.external_id]
                    continue
                if kept is not None:
                    continue
                if record.endpoint is not None and (
                    record.endpoint.probe or record.endpoint.external
                ):
                    continue  # a probe is not a machine; an external target is not ours
                # Another vendor's record of a machine NinjaOne does not manage, with
                # the same name at the site: one device, not one per vendor. Exact
                # name only, and only when exactly one such device exists.
                adopted = _adopt_vendor_only(state, record)
                if adopted is not None:
                    device = adopted
                    _touch(device, record, now=now)
                else:
                    device = _new_device(session, state, record, now=now)
                    result.devices_created += 1
                _new_link(session, state, device, record, matched_by="vendor_only", now=now)
                seen_links.add(key)
                result.links_created["vendor_only"] += 1
            # 2c. queue entries this run no longer has: linked meanwhile, or gone
            for key, entry in list(state.unresolved.items()):
                if key[0] == connector and key[1] not in candidates_of:
                    entry.resolved_at = now
                    del state.unresolved[key]

        # --- 3. links the vendors did not return, and statuses ---------------------
        answered_tools = {NINJAONE, *(connector_name_of(key) for key in tools)}
        for (connector, external_id), link in state.links.items():
            if connector in answered_tools and (connector, external_id) not in seen_links:
                if link.active and link.matched_by != "manual":
                    link.active = False
                    result.links_deactivated += 1
        # A lost device whose linked vendor row a tool still hears from is alive
        # with its agent down: the board's rule (`core.endpoint_alive`), on the
        # rows this run read.
        rows_by_link: dict[tuple[str, str], ToolEndpoint] = {
            (tool_key, r.external_id): r.endpoint
            for tool_key, (records, _) in tools.items()
            for r in records
            if r.endpoint is not None
        }
        for device in state.devices.values():
            active_links = [link for link in state.links_by_device[device.id] if link.active]
            if active_links:
                device.last_seen_at = max(
                    device.last_seen_at, max(link.last_seen_at for link in active_links)
                )
            ninja = state.ninja_link(device.id)
            if ninja is not None and device.id in last_contact_of:
                contact = last_contact_of[device.id]
                lost = contact is None or (now - contact).days > threshold
                if lost:
                    # Undecidable this run (a tool it is linked in did not answer or
                    # its dates went unread, or no passes ran at all): a device already
                    # lost keeps the status it had; one newly lost is lost until a run
                    # can say otherwise.
                    undecided = result.incomplete is not None or any(
                        link.connector_name.value in result.undated
                        or link.connector_name.value not in answered_tools
                        for link in active_links
                    )
                    if undecided and device.status in ("rmm_stale", "rmm_agent_down"):
                        pass
                    elif undecided:
                        device.status = "rmm_stale"
                    else:
                        alive = any(
                            endpoint_alive(row, now=now, days=threshold)
                            for link in active_links
                            if (
                                row := rows_by_link.get(
                                    (link.connector_name.value, link.external_id)
                                )
                            )
                            is not None
                        )
                        device.status = "rmm_agent_down" if alive else "rmm_stale"
                else:
                    device.status = "active"
            elif now - device.last_seen_at > retire_after:
                device.status = "retired"
            elif device.status == "retired":
                device.status = "active"
            if device.status == "rmm_stale":
                result.rmm_stale += 1
            elif device.status == "rmm_agent_down":
                result.rmm_agent_down += 1
            elif device.status == "retired":
                result.retired += 1
        result.unresolved_open = len(state.unresolved)
        for external_id, when in result.last_seen_dates.items():
            link = state.link("bitdefender", external_id)
            if link is not None:
                link.vendor_last_seen_at = when
                link.vendor_checked_at = now
        await session.flush()

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        await record_event(
            session,
            AuditContext.new(
                actor_id=actor,
                interface=Interface.SYSTEM,
                client_id=client_id,
                client_ref=client_slug,
            ),
            event_type=AuditEventType.DEVICES_RESOLVED,
            outcome=AuditOutcome.SUCCESS,
            duration_ms=result.duration_ms,
            payload=result.as_dict(),
        )
    return result


async def _bitdefender_dates(
    client_id: uuid.UUID,
    assessment: Any,
    by_external: dict[str, VendorRecord],
    population: Sequence[ManagedDevice],
    *,
    now: dt.datetime,
    threshold: int,
    result: ResolutionResult,
) -> tuple[bool, bool]:
    """`lastSeen` onto the records the join paired with lost devices, through the
    shared read (`app/devices/lastseen.py`: the links first, the vendor for the
    rest, the answer kept on the links): (any record gained a date, the read
    failed). A failed read dates nothing and is reported so those devices stay
    undecided rather than being called lost."""
    reads = LastSeenReads()
    rows_by_id = {r.external_id: r.endpoint for r in by_external.values() if r.endpoint}
    refs = {r.external_id: r.ref for r in by_external.values() if r.ref is not None}
    candidates = candidates_for(
        assessment,
        rows_by_id=rows_by_id,
        refs=refs,
        devices_by_id={d.device_id: d for d in population},
        now=now,
        threshold_days=threshold,
        reads=reads,
    )
    seen = await read_last_seen(client_id, candidates, now=now, reads=reads)
    result.last_seen_reads = reads.as_dict()
    result.last_seen_dates.update(reads.fetched_dates)
    changed = False
    for endpoint_id, when in seen.items():
        record = by_external[endpoint_id]
        if when is not None and record.endpoint is not None:
            record.endpoint = replace(record.endpoint, last_seen=when)
            changed = True
    return changed, reads.failed


def _adopt_vendor_only(state: _State, record: VendorRecord) -> Device | None:
    """A record new to us whose name a vendor-only device at the site already carries:
    that device is the machine, rather than a second row for it. For a NinjaOne
    record the device becomes managed; for another vendor's it gains a link. Only
    when exactly one such device exists; otherwise a new device, and the vendor
    record stays where it is for a person to look at."""
    key = _key(record.name)
    if not key:
        return None
    matches = [
        d
        for d in state.devices.values()
        if d.site_id == record.site_id
        and d.status != "retired"
        and state.ninja_link(d.id) is None
        and _key(d.canonical_name) == key
    ]
    return matches[0] if len(matches) == 1 else None

"""Reading through the identity layer: the board, the review and the lookups, flag on.

`assess_from_identity` answers what `core.assess` answers — every managed
device's state for every tool, and the unpaired endpoints — but from the
stored links rather than by pairing inventories again: a device's endpoint
is the row its link names, its verdict is that row's current state, and an
open unresolved entry naming the device is `unmatched` with the candidates.
The population is still NinjaOne's current inventory (so node class and
last contact are today's), joined to devices through their `ninjaone` link.

`find_devices` and `linked_records` are what the device lookup tools use to
turn a name into the vendor's own ids for the machine, so a renamed machine
paired by MAC is found under the name the engineer knows.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.matching import normalise_hostname
from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.scope import SiteRef
from app.coverage.config import ninjaone_population
from app.coverage.core import (
    NAME_PASSES,
    Assessment,
    DeviceCoverage,
    Exclusion,
    ManagedDevice,
    SiteKey,
    ToolEndpoint,
    ToolInventory,
    ToolState,
    Unmanaged,
    _best,
    group_machines,
    lost_state,
    ninjaone_state,
    unmanaged_entry,
)
from app.coverage.naming import connector_name_of
from app.models.device import Device, DeviceUnresolved, DeviceVendorLink
from app.models.enums import ConnectorName

NINJAONE = ConnectorName.NINJAONE.value


@dataclass(slots=True)
class IdentityView:
    """One client's devices, links and queue, loaded once."""

    devices: dict[uuid.UUID, Device]
    links: list[DeviceVendorLink]
    unresolved: list[DeviceUnresolved]
    by_record: dict[tuple[str, str], DeviceVendorLink] = field(default_factory=dict)
    by_device: dict[uuid.UUID, list[DeviceVendorLink]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_record = {
            (link.connector_name.value, link.external_id): link for link in self.links
        }
        grouped: dict[uuid.UUID, list[DeviceVendorLink]] = defaultdict(list)
        for link in self.links:
            grouped[link.device_id].append(link)
        self.by_device = dict(grouped)

    def device_for(self, connector: str, external_id: str) -> Device | None:
        link = self.by_record.get((connector, external_id))
        if link is None or not link.active:
            return None
        return self.devices.get(link.device_id)

    def active_links(self, device_id: uuid.UUID, connector: str) -> list[DeviceVendorLink]:
        return [
            link
            for link in self.by_device.get(device_id, [])
            if link.active and link.connector_name.value == connector
        ]

    def has_ninjaone(self, device_id: uuid.UUID) -> bool:
        return bool(self.active_links(device_id, NINJAONE))


async def load_identity(session: AsyncSession, client_id: uuid.UUID) -> IdentityView:
    devices = (
        (await session.execute(select(Device).where(Device.client_id == client_id))).scalars().all()
    )
    links = (
        (
            await session.execute(
                select(DeviceVendorLink).where(DeviceVendorLink.client_id == client_id)
            )
        )
        .scalars()
        .all()
    )
    unresolved = (
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
    )
    return IdentityView(
        devices={d.id: d for d in devices}, links=list(links), unresolved=list(unresolved)
    )


def assess_from_identity(
    view: IdentityView,
    ninja_rows: Sequence[tuple[SiteRef, NinjaDevice]],
    inventories: dict[str, ToolInventory | None],
    *,
    exclusions: Iterable[Exclusion] = (),
    now: dt.datetime,
    ninjaone_stale_days: int | None,
    ninja_sites: Iterable[SiteKey] | None = None,
    macs: dict[int, frozenset[str]] | None = None,
    serials: dict[int, str] | None = None,
    undated_tools: Iterable[str] = (),
) -> Assessment:
    """The board from the stored links. Same shape and same rules as `core.assess`."""
    undated = set(undated_tools)
    devices = ninjaone_population(ninja_rows, macs, serials)
    population_sites = set(ninja_sites) if ninja_sites is not None else {d.site_id for d in devices}
    excluded = {(e.device_id, e.tool): e for e in exclusions}
    # The join's own two refusals, applied here as well: a device with no hostname,
    # and two managed devices at one site sharing one, are unmatched whatever the
    # links say — unless a person linked the record by hand, or a pass that tells
    # twins apart (MAC, serial) made the link; a name pass cannot have.
    names_per_site: Counter[tuple[SiteKey, str]] = Counter(
        (d.site_id, d.key) for d in devices if d.key
    )
    ambiguous = {key for key, count in names_per_site.items() if count > 1}
    rows_by_record: dict[str, dict[str, ToolEndpoint]] = {}
    # Per tool, the vendor rows of each hostname no device holds a link to: what
    # twins would be fighting over. Twins with nothing to fight over are absent,
    # as the join leaves them, rather than unmatched.
    open_by_key: dict[str, dict[tuple[SiteKey, str], list[str]]] = {}
    for tool, inventory in inventories.items():
        if inventory is None:
            continue
        rows_by_record[tool] = {e.external_id: e for e in inventory.endpoints if e.external_id}
        connector = connector_name_of(tool)
        linked = {
            link.external_id
            for link in view.links
            if link.active and link.connector_name.value == connector
        }
        open_by_key[tool] = defaultdict(list)
        for e in inventory.endpoints:
            if e.pairable and e.external_id and e.external_id not in linked:
                open_by_key[tool][(e.site_id, e.key)].append(e.external_id)
    # Open queue entries by (tool, candidate device id): what makes a device unmatched.
    queued: dict[tuple[str, uuid.UUID], list[DeviceUnresolved]] = defaultdict(list)
    for entry in view.unresolved:
        for candidate in entry.candidates:
            try:
                device_id = uuid.UUID(str(candidate["device_id"]))
            except (KeyError, ValueError):
                continue
            queued[(entry.connector_name.value, device_id)].append(entry)

    results: list[DeviceCoverage] = []
    claimed: dict[str, set[int]] = defaultdict(set)
    for managed in devices:
        state, detail = ninjaone_state(managed, now=now, stale_days=ninjaone_stale_days or 0)
        result = DeviceCoverage(device=managed, ninjaone_state=state, ninjaone_detail=detail)
        identity = view.device_for(NINJAONE, managed.device_id)
        lost = ninjaone_stale_days is not None and (
            managed.last_contact is None or (now - managed.last_contact).days > ninjaone_stale_days
        )
        for tool, inventory in inventories.items():
            exclusion = excluded.get((managed.device_id, tool))
            if exclusion is not None:
                result.tools[tool] = ToolState("excluded", exclusion.reason)
                continue
            if inventory is None:
                result.tools[tool] = ToolState("not_assessed", "tool not assessed")
                continue
            if managed.site_id not in inventory.sites:
                result.tools[tool] = ToolState("not_assessed", "tool has no company for this site")
                continue
            if not managed.key:
                result.tools[tool] = (
                    ToolState("rmm_stale", f"no NinjaOne contact in {ninjaone_stale_days} days")
                    if lost
                    else ToolState("unmatched", "NinjaOne gives the device no hostname")
                )
                continue
            if identity is None:
                verdict = ToolState("unmatched", "not yet resolved: run the device resolution")
                paired: list[ToolEndpoint] = []
            else:
                verdict, paired = _verdict_from_links(
                    view,
                    identity,
                    managed,
                    tool,
                    rows_by_record[tool],
                    ambiguous,
                    devices,
                    queued,
                    open_by_key[tool].get((managed.site_id, managed.key), []),
                )
            if lost:
                verdict = lost_state(
                    verdict,
                    paired,
                    tool=tool,
                    now=now,
                    stale_days=ninjaone_stale_days or 0,
                    dated=tool not in undated,
                )
            if verdict.matched_by is not None:
                claimed[tool].update(id(row) for row in paired)
            result.tools[tool] = verdict
        results.append(result)

    unmanaged: list[Unmanaged] = []
    discovered: list[Unmanaged] = []
    not_assessable: list[Unmanaged] = []
    external: list[Unmanaged] = []
    for tool, inventory in inventories.items():
        if inventory is None:
            continue
        for rows in group_machines(inventory.endpoints).values():
            if any(id(row) in claimed[tool] for row in rows):
                continue
            unpaired = unmanaged_entry(tool, rows)
            best = _best(rows)
            if best.external:
                external.append(unpaired)
            elif best.site_id not in population_sites:
                not_assessable.append(unpaired)
            elif best.discovered or best.probe:
                discovered.append(unpaired)
            else:
                unmanaged.append(unpaired)
    return Assessment(
        devices=results,
        unmanaged=unmanaged,
        discovered=discovered,
        not_assessable=not_assessable,
        external=external,
    )


def _verdict_from_links(
    view: IdentityView,
    identity: Device,
    managed: ManagedDevice,
    tool: str,
    rows_by_record: dict[str, ToolEndpoint],
    ambiguous: set[tuple[SiteKey, str]],
    devices: Sequence[ManagedDevice],
    queued: dict[tuple[str, uuid.UUID], list[DeviceUnresolved]],
    contested: Sequence[str],
) -> tuple[ToolState, list[ToolEndpoint]]:
    """What the device's links say for one tool, and the rows they name.

    A pairing stands when the rows are at the device's site and either a
    person made it, a pass that tells twins apart made it, or the device is
    not a twin. Otherwise the join's refusals — a twin is unmatched only while
    the tool holds a row of the shared name nobody has (`contested`), as in
    the join — then the queue, then absent.
    """
    links = view.active_links(identity.id, connector_name_of(tool))
    rows = [
        rows_by_record[link.external_id]
        for link in links
        if link.external_id in rows_by_record
        and rows_by_record[link.external_id].site_id == managed.site_id
    ]
    twin = (managed.site_id, managed.key) in ambiguous
    stands = any(link.matched_by not in NAME_PASSES for link in links)
    if rows and (stands or not twin):
        best = _best(rows)
        return (
            ToolState(
                best.state,
                best.detail,
                best.last_seen,
                matched_by=links[0].matched_by,
                endpoint_ids=tuple(r.external_id for r in rows if r.external_id),
            ),
            rows,
        )
    if twin and (contested or rows):
        twins = sorted(
            d.device_id
            for d in devices
            if d.site_id == managed.site_id
            and d.key == managed.key
            and d.device_id != managed.device_id
        )
        named = sorted({rows_by_record[i].name or "" for i in contested if i in rows_by_record})
        return (
            ToolState(
                "unmatched",
                "two managed devices at this site share the hostname",
                candidates=tuple(named) + tuple(f"device {other}" for other in twins),
                endpoint_ids=tuple(sorted(contested)),
                found_by="exact",
            ),
            [],
        )
    entries = queued.get((tool, identity.id))
    if entries:
        return (
            ToolState(
                "unmatched",
                f"more than one candidate in the {entries[0].found_by} pass",
                candidates=tuple(sorted(e.vendor_name or "" for e in entries)),
                endpoint_ids=tuple(sorted(e.external_id for e in entries)),
                found_by=entries[0].found_by,
            ),
            [],
        )
    return ToolState("absent", "no endpoint linked to this device"), []


# --- lookups for the device tools ----------------------------------------------------


def _match_key(name: str) -> tuple[str, str]:
    key = normalise_hostname(name)
    return key, key[:15]


async def find_devices(
    session: AsyncSession, client_id: uuid.UUID, name: str, *, site_id: uuid.UUID | None = None
) -> list[Device]:
    """Devices whose canonical name or NetBIOS key is `name`'s, not retired."""
    key, key15 = _match_key(name)
    if not key:
        return []
    query = select(Device).where(
        Device.client_id == client_id, Device.status != "retired", Device.netbios_key == key15
    )
    if site_id is not None:
        query = query.where(Device.site_id == site_id)
    found = list((await session.execute(query)).scalars().all())
    exact = [d for d in found if normalise_hostname(d.canonical_name) == key]
    return exact or found


async def linked_records(
    session: AsyncSession,
    client_id: uuid.UUID,
    connector: ConnectorName,
    name: str,
    *,
    site_id: uuid.UUID | None = None,
) -> list[str]:
    """The vendor's ids for the machine called `name`, through its device and links."""
    devices = await find_devices(session, client_id, name, site_id=site_id)
    if not devices:
        return []
    rows = (
        await session.execute(
            select(DeviceVendorLink.external_id).where(
                DeviceVendorLink.client_id == client_id,
                DeviceVendorLink.connector_name == connector,
                DeviceVendorLink.active.is_(True),
                DeviceVendorLink.device_id.in_([d.id for d in devices]),
            )
        )
    ).all()
    return [row[0] for row in rows]


def managed_from_identity(view: IdentityView) -> list[ManagedDevice]:
    """Every non-retired device as a `ManagedDevice`, for callers that need the list."""
    return [
        ManagedDevice(
            site_id=d.site_id,
            site_slug=str(d.site_id),
            site_name=str(d.site_id),
            device_id=str(d.id),
            name=d.canonical_name,
            node_class=d.node_class,
            macs=frozenset(d.macs),
        )
        for d in view.devices.values()
        if d.status != "retired"
    ]

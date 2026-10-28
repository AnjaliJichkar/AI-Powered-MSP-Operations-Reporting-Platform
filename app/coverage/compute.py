"""`compute_coverage(client_id)`: the board for one client, from the cached inventories.

One computation, three listings and one query. NinjaOne's tenant inventory
is the population; Bitdefender's and ConnectSecure's are each tool's answer;
every one is the same cached, scoped listing the chat tools read, so a board
load after a question costs nothing at the vendor and a cold one costs one
listing per tool. The `network-interfaces` query adds each device's MACs for
the third pass and `computer-systems` its serial for the fourth; each is one
cursor walk, cached with the inventory. GravityZone's list carries no
last-seen, so for the rows the join pairs with devices NinjaOne has lost —
and only those — `getManagedEndpointDetails` is read for its `lastSeen`,
cached the same way, and the join runs again with the dates: that is what
decides `rmm_agent_down` against `rmm_stale`. A tool with no credentials for
the client, or none configured, is `not_assessed` for every device rather
than an error, and the report says why.

**A vendor that did not answer is not assessed this run**, never absent and
never unmanaged: a tool whose listing failed or timed out is `not_assessed`
for every device with the reason on its `ToolReport`; a failed last-seen
read leaves the lost devices it was for `not_assessed` rather than lost or
alive (`undated_tools`); and a NinjaOne MAC or serial query that failed makes
every tool `not_assessed` this run, because the passes would pair less and
report the difference as gaps. A day like that is a gap in the trend, not a
day the fleet got worse. A tool that answers for some sites and not
others is `not_assessed` only at the sites it has no company for.

Exclusions come from the database unless the caller passes them (the tests
do). Nothing here writes.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.connectsecure.client import ConnectSecureClient
from app.connectors.cove.client import CoveClient
from app.connectors.cove.models import CoveDevice
from app.connectors.errors import ConnectorError, ConnectorNotConfigured
from app.connectors.factory import connector_for
from app.connectors.ninjaone.client import QUERY_PATH, NinjaOneClient
from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.scope import SiteRef
from app.coverage.config import (
    COVERAGE_TOOLS,
    CoverageTool,
    mac_key,
    ninjaone_population,
    rmm_stale_days,
    serial_key,
)
from app.coverage.core import (
    GAP_STATES,
    STATES,
    Assessment,
    DeviceCoverage,
    Exclusion,
    ManagedDevice,
    ToolInventory,
    Unmanaged,
    assess,
    is_agent_class,
    mark_unchecked,
    with_last_seen,
)
from app.coverage.scope import BACKUP
from app.db.session import session_scope
from app.devices.lastseen import LastSeenReads, candidates_for, read_last_seen
from app.models.client import Client
from app.models.enums import ConnectorName
from app.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolReport:
    """How one tool took part in the computation."""

    key: str
    label: str
    assessed: bool
    reason: str = ""
    """Why it was not assessed, when it was not."""
    truncated: bool = False
    endpoints: int = 0


@dataclass(frozen=True, slots=True)
class OrganisationCount:
    """One vendor organisation's rows at one site: what the duplication report groups."""

    tool: str
    site_id: uuid.UUID
    external_id: str
    endpoints: int
    with_agent: int


@dataclass(slots=True)
class ToolCounts:
    counts: Counter[str] = field(default_factory=Counter)

    def __getitem__(self, state: str) -> int:
        return self.counts.get(state, 0)

    @property
    def assessable(self) -> int:
        """Devices the tool had a verdict on: not excluded, not assessed or RMM-stale."""
        return sum(self.counts[s] for s in ("covered", "stale", "absent", "unmatched"))

    @property
    def covered_percent(self) -> float | None:
        """Covered as a share of what could be covered. None when nothing was assessed."""
        if not self.assessable:
            return None
        return round(100 * self["covered"] / self.assessable, 1)

    @property
    def gaps(self) -> int:
        return sum(self.counts[s] for s in GAP_STATES)

    def as_dict(self) -> dict[str, Any]:
        return {
            **{state: self[state] for state in STATES},
            "assessable": self.assessable,
            "covered_percent": self.covered_percent,
            "gaps": self.gaps,
        }


@dataclass(slots=True)
class SiteSummary:
    site_id: uuid.UUID
    slug: str
    name: str
    managed: int
    tools: dict[str, ToolCounts]
    unmanaged: int
    rmm_stale: int = 0
    """Managed devices NinjaOne itself has lost and no tool hears from: a line of their own."""
    rmm_agent_down: int = 0
    """Managed devices NinjaOne has lost that a tool still hears from: a finding, per site."""
    discovered: int = 0
    """Rows a tool discovered rather than installed on, unpaired, at this site."""
    external: int = 0
    """Scanner rows that are external targets (an IP or an ISP's name) at this site."""
    backup_scope_mixed: bool = False
    """`auto` guessed servers only for this site's backup scope while somebody
    is backing up a workstation here, so the gap list is shorter than it might
    be. A flag for us, never for a customer: one setting settles it."""

    @property
    def worst_percent(self) -> float | None:
        """The lowest covered share across the assessed tools: the sort key."""
        shares = [c.covered_percent for c in self.tools.values() if c.covered_percent is not None]
        return min(shares) if shares else None

    @property
    def gaps(self) -> int:
        return sum(c.gaps for c in self.tools.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "site_slug": self.slug,
            "site": self.name,
            "managed": self.managed,
            "rmm_stale": self.rmm_stale,
            "rmm_agent_down": self.rmm_agent_down,
            "unmanaged": self.unmanaged,
            "discovered": self.discovered,
            "external": self.external,
            "gaps": self.gaps,
            "worst_percent": self.worst_percent,
            "backup_scope_mixed": self.backup_scope_mixed,
            "tools": {key: counts.as_dict() for key, counts in self.tools.items()},
        }


@dataclass(slots=True)
class CoverageReport:
    client_id: uuid.UUID
    computed_at: dt.datetime
    duration_ms: int
    tools: list[ToolReport]
    devices: list[DeviceCoverage]
    unmanaged: list[Unmanaged]
    sites: list[SiteSummary]
    exclusions: list[Exclusion]
    not_computers: int
    """NinjaOne devices left out of the population: network gear, phones, hosts."""
    population_truncated: bool = False
    rmm_stale_days: int = 14
    discovered: list[Unmanaged] = field(default_factory=list)
    not_assessable: list[Unmanaged] = field(default_factory=list)
    external: list[Unmanaged] = field(default_factory=list)
    """Scanner rows that are external targets, never endpoints: reported apart."""
    organisations: list[OrganisationCount] = field(default_factory=list)
    """Every vendor organisation each tool answered for, with its row counts."""
    site_names: dict[uuid.UUID, tuple[str, str]] = field(default_factory=dict)
    """Every site the board knows, id to (slug, name): what a stored board rebuilds from."""
    reads: dict[str, int | bool] = field(default_factory=dict)
    """What the last-seen read did this run (`LastSeenReads.as_dict`), for the log."""

    def duplicated(self) -> dict[tuple[str, uuid.UUID], list[OrganisationCount]]:
        """Sites with more than one organisation on one tool: the vendor duplication."""
        grouped: dict[tuple[str, uuid.UUID], list[OrganisationCount]] = {}
        for entry in self.organisations:
            grouped.setdefault((entry.tool, entry.site_id), []).append(entry)
        return {key: rows for key, rows in grouped.items() if len(rows) > 1}

    @property
    def rmm_stale(self) -> int:
        """Devices NinjaOne has lost and no tool hears from."""
        return sum(1 for d in self.devices if d.ninjaone_state == "stale" and not d.agent_down)

    @property
    def rmm_agent_down(self) -> int:
        """Devices NinjaOne has lost that a tool still hears from."""
        return sum(1 for d in self.devices if d.agent_down)

    def tool(self, key: str) -> ToolReport:
        return next(t for t in self.tools if t.key == key)

    def totals(self) -> dict[str, ToolCounts]:
        """Per tool, the whole client."""
        totals: dict[str, ToolCounts] = {t.key: ToolCounts() for t in self.tools}
        for device in self.devices:
            for key, state in device.tools.items():
                totals[key].counts[state.state] += 1
        return totals

    def site(self, slug: str) -> SiteSummary | None:
        return next((s for s in self.sites if s.slug == slug), None)

    def devices_at(self, slug: str) -> list[DeviceCoverage]:
        return [d for d in self.devices if d.device.site_slug == slug]

    def unmanaged_at(self, site_id: uuid.UUID) -> list[Unmanaged]:
        return [u for u in self.unmanaged if u.site_id == site_id]

    def discovered_at(self, site_id: uuid.UUID) -> list[Unmanaged]:
        return [u for u in self.discovered if u.site_id == site_id]

    def external_at(self, site_id: uuid.UUID) -> list[Unmanaged]:
        return [u for u in self.external if u.site_id == site_id]

    def gap_keys(self) -> set[tuple[str, str, str, str]]:
        """Every (site slug, device id, tool, state) that is a gap: what a snapshot keeps."""
        return {
            (d.device.site_slug, d.device.device_id, tool, state.state)
            for d in self.devices
            for tool, state in d.tools.items()
            if state.state in GAP_STATES
        }


#: The `network-interfaces` query is 7,199 rows on the pilot and `computer-systems`
#: one row per device, one cursor walk each per computation; each is kept per
#: client for the inventory's own TTL, keyed by the credential version like the
#: inventory, so a warm board stays warm.
_MACS: dict[tuple[uuid.UUID, str | None], tuple[float, dict[int, frozenset[str]]]] = {}
_SERIALS: dict[tuple[uuid.UUID, str | None], tuple[float, dict[int, str]]] = {}


async def device_macs(connector: NinjaOneClient) -> dict[int, frozenset[str]]:
    """Each device's interface MACs from the `network-interfaces` query, placed
    through the inventory like every device-keyed row. Cached per client."""
    key = (connector.scope.client_id, connector.credential_version)
    cached = _MACS.get(key)
    if cached is not None and cached[0] > time.monotonic() and connector.scope.covers_every_site:
        return cached[1]
    found = await _read_device_macs(connector)
    if connector.scope.covers_every_site and connector.inventory_ttl_seconds > 0:
        _MACS[key] = (time.monotonic() + connector.inventory_ttl_seconds, found)
    return found


async def device_serials(connector: NinjaOneClient) -> dict[int, str]:
    """Each device's serial number from the `computer-systems` query (the system
    serial, else the BIOS serial), normalised with `serial_key`; placeholders
    are left out. Placed through the inventory; cached per client."""
    key = (connector.scope.client_id, connector.credential_version)
    cached = _SERIALS.get(key)
    if cached is not None and cached[0] > time.monotonic() and connector.scope.covers_every_site:
        return cached[1]
    found = await _read_device_serials(connector)
    if connector.scope.covers_every_site and connector.inventory_ttl_seconds > 0:
        _SERIALS[key] = (time.monotonic() + connector.inventory_ttl_seconds, found)
    return found


async def _read_device_serials(connector: NinjaOneClient) -> dict[int, str]:
    raw, _, _ = await connector.query_scoped("computer-systems")
    rows = [row for row in raw if isinstance(row, dict)]
    placed = await connector.place_by_device(
        rows,
        [row.get("deviceId") for row in rows],
        endpoint=QUERY_PATH.format(report="computer-systems"),
    )
    serials: dict[int, str] = {}
    for _, device, row in placed:
        value = row.get("serialNumber") or row.get("biosSerialNumber")
        serial = serial_key(value if isinstance(value, str) else None)
        if serial:
            serials[device.id] = serial
    return serials


async def _read_device_macs(connector: NinjaOneClient) -> dict[int, frozenset[str]]:
    raw, _, _ = await connector.query_scoped("network-interfaces")
    rows = [row for row in raw if isinstance(row, dict)]
    placed = await connector.place_by_device(
        rows,
        [row.get("deviceId") for row in rows],
        endpoint=QUERY_PATH.format(report="network-interfaces"),
    )
    macs: dict[int, set[str]] = {}
    for _, device, row in placed:
        values = row.get("macAddress")
        for value in values if isinstance(values, list) else [values]:
            key = mac_key(value if isinstance(value, str) else None)
            if key:
                macs.setdefault(device.id, set()).add(key)
    return {device_id: frozenset(found) for device_id, found in macs.items()}


async def _bitdefender_dates(
    client_id: uuid.UUID,
    inventories: dict[str, ToolInventory | None],
    assessment: Assessment,
    refs: Mapping[str, SiteRef],
    *,
    devices: Sequence[ManagedDevice],
    now: dt.datetime,
    stale_days: int,
) -> tuple[dict[str, ToolInventory | None], LastSeenReads]:
    """The inventories with GravityZone's `lastSeen` on the rows paired with lost
    devices (`app/devices/lastseen.py`: from the links where fresh, from the
    vendor otherwise), and what the read did. `reads.failed` says the vendor
    did not answer, so the join can leave those devices undecided."""
    reads = LastSeenReads()
    inventory = inventories.get("bitdefender")
    if inventory is None:
        return inventories, reads
    rows_by_id = {e.external_id: e for e in inventory.endpoints if e.external_id}
    candidates = candidates_for(
        assessment,
        rows_by_id=rows_by_id,
        refs=refs,
        devices_by_id={d.device_id: d for d in devices},
        now=now,
        threshold_days=stale_days,
        reads=reads,
    )
    seen = await read_last_seen(client_id, candidates, now=now, reads=reads)
    if not seen:
        return inventories, reads
    return {**inventories, "bitdefender": with_last_seen(inventory, seen)}, reads


def organisation_counts(
    tool: str, rows: Sequence[tuple[SiteRef, Any]], *, with_agent: Callable[[Any], bool]
) -> list[OrganisationCount]:
    """Rows per vendor organisation, from the site ref each row was tagged with."""
    totals: Counter[tuple[uuid.UUID, str]] = Counter()
    agents: Counter[tuple[uuid.UUID, str]] = Counter()
    for site, row in rows:
        totals[(site.site_id, site.external_id)] += 1
        if with_agent(row):
            agents[(site.site_id, site.external_id)] += 1
    return [
        OrganisationCount(tool, site_id, external_id, count, agents[(site_id, external_id)])
        for (site_id, external_id), count in sorted(totals.items())
    ]


@dataclass(slots=True)
class Population:
    devices: list[ManagedDevice]
    not_computers: int
    truncated: bool
    site_names: SiteNames
    organisations: list[OrganisationCount]
    rows: list[tuple[SiteRef, NinjaDevice]]
    macs: dict[int, frozenset[str]]
    serials: dict[int, str] = field(default_factory=dict)
    incomplete: str | None = None
    """Why the population's keys are not whole this run (a NinjaOne query that did
    not answer): nothing is assessed against an incomplete population."""


async def _apply_backup_scope(
    client_id: uuid.UUID,
    assessment: Assessment,
    cove_rows: Mapping[uuid.UUID, list[CoveDevice]],
    rows: Sequence[tuple[SiteRef, NinjaDevice]],
    site_names: SiteNames,
) -> dict[uuid.UUID, bool]:
    """Move out-of-scope backup gaps to `not_in_scope`, by the review's rule.

    Whether a machine is a server is `device_role`, the same function the
    review's lifecycle tables and the ticket panel read — but given the node
    class alone, because the board's population comes from the device listing
    and the OS name is a second tenant read it does not make. `device_role`
    falls back to the node class, which is its documented path for a device
    whose OS it cannot see.

    That leaves one disagreement possible, and it is the conservative one: a
    machine whose OS and node class disagree (live, a Windows 10 IoT till
    classed `WINDOWS_SERVER`) is read as a server here, so it stays in scope
    and stays a gap rather than being quietly excluded. Those devices are
    already listed by name in the review's internal-only `role_disagreements`
    table.

    Not called at all when Cove did not answer for this client: with no backup
    inventory every device is `not_assessed` for it, the pass would change
    nothing, and this is the one place the board reads the franchisee's
    settings — so a board computed without a database (the agreement test's
    recorded inventories, among others) does not acquire a dependency on one
    to produce the same answer.
    """
    from app.connectors.ninjaone.hardware import device_role
    from app.coverage.scope import apply_backup_scope, site_scopes
    from app.reports.tbr.config import load_config

    async with session_scope() as session:
        client = await session.get(Client, client_id)
        config = await load_config(session, client_id, client_name=client.name if client else "")
    is_server = {
        str(device.id): device_role(None, device.node_class).role == "server" for _, device in rows
    }
    # The site's own workstations: the denominator `auto` asks about. Without
    # it the rule can only ask whether *any* workstation is backed up, and one
    # laptop at a 107-machine customer makes the other 106 gaps.
    workstations: dict[uuid.UUID, int] = {}
    for site, device in rows:
        if isinstance(site.site_id, uuid.UUID) and not is_server[str(device.id)]:
            workstations[site.site_id] = workstations.get(site.site_id, 0) + 1
    scopes = site_scopes(
        settings=config.backup,
        machines_by_site=cove_rows,
        slugs={site_id: slug for site_id, (slug, _) in site_names.items()},
        workstations=workstations,
    )
    apply_backup_scope(assessment, scopes=scopes, is_server=is_server)
    return {site_id: scope.mixed for site_id, scope in scopes.items() if scope.mixed}


async def _population(client_id: uuid.UUID) -> Population:
    async with connector_for(client_id, ConnectorName.NINJAONE) as connector:
        assert isinstance(connector, NinjaOneClient)
        rows, truncated = await connector.list_devices()
        names = {site.site_id: (site.slug, site.name) for site in connector.scope.targets}
        organisations = organisation_counts(
            "ninjaone", rows, with_agent=lambda d: is_agent_class(d.node_class)
        )
        incomplete = None
        macs: dict[int, frozenset[str]] = {}
        serials: dict[int, str] = {}
        try:
            macs = await device_macs(connector)
            serials = await device_serials(connector)
        except ConnectorError as exc:
            incomplete = f"NinjaOne did not answer for the devices' MACs or serials: {exc}"
            logger.warning("coverage: %s (client=%s)", incomplete, client_id)
    devices = ninjaone_population(rows, macs, serials)
    return Population(
        devices=devices,
        not_computers=len(rows) - len(devices),
        truncated=truncated,
        site_names=names,
        organisations=organisations,
        rows=list(rows),
        macs=macs,
        serials=serials,
        incomplete=incomplete,
    )


SiteNames = dict[uuid.UUID, tuple[str, str]]


async def _inventory(
    client_id: uuid.UUID,
    tool: CoverageTool,
    *,
    now: dt.datetime,
    site_names: SiteNames,
    organisations: list[OrganisationCount],
    refs: dict[str, SiteRef],
    cove_rows: dict[uuid.UUID, list[CoveDevice]],
) -> tuple[ToolInventory | None, ToolReport]:
    """One tool's endpoints for the client, or None with the reason it was not assessed.

    The sites the tool has a company for are added to `site_names`, so a
    site only a tool knows still has a row on the board (all unmanaged).
    `refs` learns which vendor organisation each Bitdefender row came from,
    for the per-endpoint detail read.
    """
    if tool.reader is None or tool.connector is None:
        return None, ToolReport(
            tool.key, tool.label, assessed=False, reason=tool.not_assessed_reason
        )
    rows: Sequence[tuple[SiteRef, Any]]
    try:
        async with connector_for(client_id, tool.connector) as connector:
            if tool.connector is ConnectorName.BITDEFENDER:
                assert isinstance(connector, BitdefenderClient)
                rows, truncated = await connector.list_endpoints()
                organisations += organisation_counts(
                    tool.key, rows, with_agent=lambda e: bool(e.has_agent)
                )
                refs.update({endpoint.id: site for site, endpoint in rows if endpoint.id})
            elif tool.connector is ConnectorName.CONNECTSECURE:
                assert isinstance(connector, ConnectSecureClient)
                rows, truncated = await connector.list_assets()
                organisations += organisation_counts(
                    tool.key, rows, with_agent=lambda a: a.agent is not None
                )
            elif tool.connector is ConnectorName.COVE:
                assert isinstance(connector, CoveClient)
                # Kept per site for the backup scope: `auto` reads whether a
                # customer buys workstation backup from what Cove already
                # holds, which is the review's rule and not a second one.
                cove_rows.clear()
                # One read of the whole tree beneath our own partner, which is
                # what the vendor's own scoping gives us; the rows carry their
                # partner and the client places them. A customer's Microsoft
                # 365 tenant is a row in that list and is not a machine, so it
                # is dropped here rather than counted as an endpoint nobody
                # manages.
                rows = [
                    (site, device)
                    for site, device in await connector.list_fleet_with_sites()
                    if device.is_machine
                ]
                for site, device in rows:
                    if isinstance(site.site_id, uuid.UUID):
                        cove_rows.setdefault(site.site_id, []).append(device)
                truncated = False
                organisations += organisation_counts(tool.key, rows, with_agent=lambda d: True)
            else:  # pragma: no cover - a tool entry names a connector this cannot read
                return None, ToolReport(
                    tool.key, tool.label, assessed=False, reason=f"no reader for {tool.connector}"
                )
            sites = frozenset(site.site_id for site in connector.scope.targets)
            for site in connector.scope.targets:
                site_names.setdefault(site.site_id, (site.slug, site.name))
    except ConnectorNotConfigured as exc:
        # The tool's own sentence, not the exception's. `ConnectorNotConfigured`
        # says "[cove] the demo tenant has no cove connector", which names a
        # connector key and a class of client and is written for whoever is
        # reading a traceback; this reason is printed on the board, in a tile
        # and in a review, where it has to read as English. The exception is
        # still logged with everything it knows.
        logger.info("coverage: %s is not configured for client=%s: %s", tool.key, client_id, exc)
        return None, ToolReport(
            tool.key,
            tool.label,
            assessed=False,
            reason=tool.not_assessed_reason or str(exc),
        )
    except ConnectorError as exc:
        logger.warning("coverage: %s did not answer for client=%s: %s", tool.key, client_id, exc)
        return None, ToolReport(
            tool.key, tool.label, assessed=False, reason=f"{tool.label} did not answer: {exc}"
        )
    endpoints = tool.reader(rows, now)
    inventory = ToolInventory(endpoints=endpoints, sites=sites, truncated=truncated)
    return inventory, ToolReport(
        tool.key, tool.label, assessed=True, truncated=truncated, endpoints=len(endpoints)
    )


def summarise_sites(
    assessment: Assessment,
    *,
    site_names: SiteNames,
    tools: Sequence[ToolReport],
    mixed_scopes: Mapping[uuid.UUID, bool] | None = None,
) -> list[SiteSummary]:
    """One row per site the population knows, worst first."""
    summaries: dict[uuid.UUID, SiteSummary] = {}
    for site_id, (slug, name) in site_names.items():
        summaries[site_id] = SiteSummary(
            site_id=site_id,
            slug=slug,
            name=name,
            managed=0,
            tools={t.key: ToolCounts() for t in tools},
            unmanaged=0,
            backup_scope_mixed=bool((mixed_scopes or {}).get(site_id)),
        )
    for device in assessment.devices:
        summary = summaries.get(device.device.site_id)  # type: ignore[arg-type]
        if summary is None:
            continue
        summary.managed += 1
        if device.agent_down:
            summary.rmm_agent_down += 1
        elif device.ninjaone_state == "stale":
            summary.rmm_stale += 1
        for key, state in device.tools.items():
            summary.tools[key].counts[state.state] += 1
    for endpoint in assessment.unmanaged:
        summary = summaries.get(endpoint.site_id)  # type: ignore[arg-type]
        if summary is not None:
            summary.unmanaged += 1
    for endpoint in assessment.discovered:
        summary = summaries.get(endpoint.site_id)  # type: ignore[arg-type]
        if summary is not None:
            summary.discovered += 1
    for endpoint in assessment.external:
        summary = summaries.get(endpoint.site_id)  # type: ignore[arg-type]
        if summary is not None:
            summary.external += 1
    return sorted(
        summaries.values(),
        key=lambda s: (
            s.worst_percent if s.worst_percent is not None else 101.0,
            -s.gaps,
            s.name,
        ),
    )


async def compute_coverage(
    client_id: uuid.UUID,
    *,
    exclusions: Sequence[Exclusion] | None = None,
    now: dt.datetime | None = None,
    through_identity: bool | None = None,
) -> CoverageReport:
    """Every managed device's state for every tool, for one client. Read-only.

    Raises `ConnectorNotConfigured` only when NinjaOne itself is not set up:
    without the population there is no board. Any other tool that is not set
    up is reported as not assessed.

    `through_identity` (default: `DEVICE_IDENTITY_ENABLED`) reads the pairings
    from the device identity layer's stored links instead of running the
    passes; the agreement test and script call both ways and compare.
    """
    started = time.perf_counter()
    now = now or dt.datetime.now(dt.UTC)
    if through_identity is None:
        through_identity = get_settings().device_identity_enabled
    if exclusions is None:
        from app.coverage.exclusions import live_exclusions

        exclusions = await live_exclusions(client_id)

    population = await _population(client_id)
    devices, not_computers, population_truncated = (
        population.devices,
        population.not_computers,
        population.truncated,
    )
    site_names, organisations = population.site_names, population.organisations
    ninja_sites = set(site_names)
    inventories: dict[str, ToolInventory | None] = {}
    reports: list[ToolReport] = []
    refs: dict[str, SiteRef] = {}
    cove_rows: dict[uuid.UUID, list[CoveDevice]] = {}
    for tool in COVERAGE_TOOLS:
        inventory, report = await _inventory(
            client_id,
            tool,
            now=now,
            site_names=site_names,
            organisations=organisations,
            refs=refs,
            cove_rows=cove_rows,
        )
        inventories[tool.key] = inventory
        reports.append(report)
    if population.incomplete is not None:
        # Nothing is judged against a population missing its keys: every tool is
        # not assessed this run, and the report says why (never absent or unmanaged).
        inventories = dict.fromkeys(inventories)
        reports = [
            ToolReport(
                r.key,
                r.label,
                assessed=False,
                reason=f"not assessed this run: {population.incomplete}",
            )
            if r.assessed
            else r
            for r in reports
        ]

    stale_days = rmm_stale_days()
    if through_identity:
        from app.devices.identity import assess_from_identity, load_identity

        async with session_scope() as session:
            view = await load_identity(session, client_id)

        def join(
            current: dict[str, ToolInventory | None], undated: tuple[str, ...] = ()
        ) -> Assessment:
            return assess_from_identity(
                view,
                population.rows,
                current,
                exclusions=exclusions,
                now=now,
                ninjaone_stale_days=stale_days,
                ninja_sites=ninja_sites,
                macs=population.macs,
                serials=population.serials,
                undated_tools=undated,
            )
    else:

        def join(
            current: dict[str, ToolInventory | None], undated: tuple[str, ...] = ()
        ) -> Assessment:
            return assess(
                devices,
                current,
                exclusions=exclusions,
                now=now,
                ninjaone_stale_days=stale_days,
                ninja_sites=ninja_sites,
                undated_tools=undated,
            )

    # Once to find the rows paired with lost devices, once more with their dates;
    # a read that failed leaves those devices undecided rather than lost, and a
    # device lost for longer than the retire window says it was not checked.
    assessment = join(inventories)
    dated, reads = await _bitdefender_dates(
        client_id, inventories, assessment, refs, devices=devices, now=now, stale_days=stale_days
    )
    if reads.failed:
        assessment = join(inventories, ("bitdefender",))
    elif dated is not inventories:
        assessment = join(dated)
    mark_unchecked(
        assessment, "bitdefender", reads.unchecked_devices, get_settings().device_retire_days
    )
    # Backup is the one tool on this board that is sold per machine rather than
    # rolled out to a fleet, so a machine its site does not buy it for is not a
    # gap. The rule is the review's own `effective_scope`, applied to the
    # verdicts rather than reimplemented. Before this the pilot's Backup column
    # read 5.4%, which was the share of their fleet that happens to be servers.
    mixed_scopes: dict[uuid.UUID, bool] = {}
    if inventories.get(BACKUP) is not None:
        mixed_scopes = await _apply_backup_scope(
            client_id, assessment, cove_rows, population.rows, site_names
        )
    return CoverageReport(
        client_id=client_id,
        computed_at=now,
        duration_ms=int((time.perf_counter() - started) * 1000),
        tools=reports,
        devices=assessment.devices,
        unmanaged=assessment.unmanaged,
        sites=summarise_sites(
            assessment, site_names=site_names, tools=reports, mixed_scopes=mixed_scopes
        ),
        exclusions=list(exclusions),
        not_computers=not_computers,
        population_truncated=population_truncated,
        rmm_stale_days=stale_days,
        discovered=assessment.discovered,
        not_assessable=assessment.not_assessable,
        external=assessment.external,
        organisations=organisations,
        site_names=site_names,
        reads=reads.as_dict(),
    )

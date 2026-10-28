"""Facts: every number, date and name a review can print, computed here in code.

A fact has a stable id (so this period can be compared with the last), a
display string formatted here (so the model never formats a number), and a
definition saying exactly what was counted. Two kinds of fact must not be
confused, and each says which it is:

- **period-bound** — tickets created in August, installs that failed in August.
  The vendor can be asked about a past window.
- **as-of** — device counts, pending patches, antivirus state. NinjaOne keeps no
  history, so these are the state at the moment the report was generated,
  whatever period the report is for. Their `as_of` is printed with them.

A source that refuses or fails produces a `Gap`, not a zero: the slide that
needed it then has no facts and is dropped or handed to the engineer.

All reads go through `connector_for`, the same factory the agent's tools use,
scoped to one client and one site (hard rule 1, hard rule 5).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.connectors.autotask.client import AutotaskClient
from app.connectors.autotask.tools import NON_CLIENT_PROJECT_TYPES, current_phase
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for
from app.connectors.ninjaone.client import QUERY_PATH, NinjaOneClient
from app.connectors.ninjaone.hardware import (
    BASIS as WINDOWS11_BASIS,
)
from app.connectors.ninjaone.hardware import (
    DeviceRole,
    device_role,
)
from app.connectors.ninjaone.models import (
    NinjaAntivirusStatus,
    NinjaOSPatch,
    NinjaPatchInstall,
    NinjaVolume,
)
from app.connectors.ninjaone.tools import collect_hardware
from app.coverage.compute import device_macs
from app.coverage.config import COVERAGE_TOOLS, rmm_stale_days
from app.coverage.core import NAME_PASSES
from app.db.session import session_scope
from app.models.enums import ConnectorName
from app.reports.tbr.audit import ReportAudit
from app.reports.tbr.backup import COVE_GAP, collect_cove, derive_backup
from app.reports.tbr.config import FranchiseeConfig, TimeZoneUnavailable, resolve_zone
from app.reports.tbr.model import (
    ChartData,
    ChartSeries,
    Fact,
    FactSet,
    FactTable,
    Gap,
    Population,
    ReviewPeriod,
    SeverityScope,
    Source,
)
from app.reports.tbr.monthly import (
    OS_FAILED_LIST,
    OS_TOP_FAILED,
    collect_lifecycle,
    collect_software_patch_installs,
    record_backup_gap,
    record_failed_devices,
)
from app.reports.tbr.security import (
    BITDEFENDER_GAP,
    BITLOCKER_GAP,
    GROUP_NINJA_COMPUTERS,
    GROUP_NINJA_LOST_DAYS,
    GROUP_NINJA_MACS,
    GROUP_NINJA_PATCHING,
    GROUP_NINJA_RMM_STALE,
    IDENTITY_LOADED,
    IDENTITY_MANUAL,
    IDENTITY_PAIRED,
    IDENTITY_STANDS,
    IDENTITY_TOOL,
    IDENTITY_UNMATCHED,
    PATCH_COVERAGE_GAP,
    collect_bitdefender_endpoints,
    collect_bitdefender_incidents,
    collect_bitlocker,
    collect_patch_coverage,
    derive_coverage,
    is_agent_class,
)
from app.reports.tbr.vulnerabilities import (
    CONNECTSECURE_GAP,
    PATCH_QUEUE_GAP,
    collect_connectsecure,
    collect_software_patch_queue,
    derive_scan_coverage,
)
from app.resolved.clock import franchisee_zone
from app.resolved.sla import SlaCalculator
from app.settings import get_settings

#: The internal-only table of devices the operating system counted differently
#: from NinjaOne's node class (`hardware.device_role`).
ROLE_DISAGREEMENTS = "ninjaone.devices.role_disagreements"

#: A ticket source whose label matches is a machine talking, not a person.
AUTOMATION_SOURCE = re.compile(r"monitor|alert|rmm|automat|ninja|backup|system", re.IGNORECASE)
#: A contact that is a mailbox or an integration rather than a person.
SYSTEM_CONTACT = re.compile(
    r"automation|no-?reply|do-?not-?reply|alerts?\b|monitor|notification|system|"
    r"support\b|helpdesk|service ?desk",
    re.IGNORECASE,
)
PASSWORD_TICKET = re.compile(
    r"password|passcode|locked out|lockout|account lock|mfa reset|reset mfa", re.IGNORECASE
)

MAX_CHART_CATEGORIES = 8

#: What a gap is called when one of the "value delivered" figures cannot be supplied.
ALERTS_RESOLVED = "NinjaOne alerts resolved in the period"
OUT_OF_HOURS = "Tickets resolved outside business hours"
TOP_CONTACTS = 5


def _date(value: dt.datetime | None) -> str:
    return f"{value:%d %b %Y}".lstrip("0") if value else "not set"


def _month(value: dt.datetime | None) -> str:
    return f"{value:%B %Y}" if value else "not set"


def _percent(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


logger = logging.getLogger(__name__)


class Collector:
    """Builds one `FactSet` for one site and period."""

    def __init__(
        self,
        *,
        client_id: str,
        site_id: str,
        site_slug: str = "",
        period: ReviewPeriod,
        config: FranchiseeConfig,
        audit: ReportAudit,
        now: dt.datetime | None = None,
        bitdefender_mapped: bool = True,
        connectsecure_mapped: bool = True,
        cove_mapped: bool = True,
        backup_trend: Sequence[tuple[dt.datetime, float]] = (),
        previous_backup_scope: str | None = None,
    ) -> None:
        self.client_id = client_id
        self.site_id = site_id
        self.site_slug = site_slug
        """The site's own slug, for settings keyed by site (the backup
        exclusion list). Empty in tests that do not need one."""
        self.period = period
        self.config = config
        self.audit = audit
        self.now = now or dt.datetime.now(dt.UTC)
        self.facts = FactSet()
        self._ninja_ids: dict[str, str] = {}
        # What the NinjaOne collectors read, kept for the monthly report's lifecycle
        # tables (`monthly.collect_lifecycle`) so nothing is read twice.
        self.ninja_devices: list[tuple[Any, Any]] = []
        self.ninja_profiles: list[Any] = []
        self.ninja_roles: dict[int, DeviceRole] = {}
        """Device id -> workstation or server and why, decided once (`device_role`)
        for the deck's counts and the PDF's lifecycle tables alike."""
        self.ninja_volumes: list[tuple[Any, Any, Any]] = []
        self.ninja_locations: dict[int, str] = {}
        self.bitdefender_mapped = bitdefender_mapped
        """Whether the site has a GravityZone company: decided by the pipeline, from the
        mappings, so a site without one records a gap instead of a factory error."""
        self.connectsecure_mapped = connectsecure_mapped
        """Whether the site has a ConnectSecure company, likewise."""
        self.previous_backup_scope = previous_backup_scope
        """Which machines the last review for this site counted as a backup
        gap. `auto` can change its mind between two reviews without anybody
        touching a setting, and a figure that moved for that reason is not a
        figure that moved."""
        self.cove_devices: list[Any] = []
        """What Cove returned for this site, kept for the derivations that
        need the RMM's answer as well (`app/reports/tbr/backup.py`)."""
        self.backup_trend = list(backup_trend)
        """What earlier reviews measured for backup success, oldest first. One
        point per stored review, each with the moment it was generated."""
        self.cove_mapped = cove_mapped
        """Whether the site has a Cove customer, likewise. Without one the
        Backup section says so in a line rather than printing zeros."""

    # --- helpers ------------------------------------------------------------------

    def fact(
        self,
        fact_id: str,
        label: str,
        value: int | float | str | None,
        definition: str,
        source: Source,
        *,
        display: str | None = None,
        period_bound: bool = False,
        population: Population = "universal",
        severity: SeverityScope | None = None,
        denominator: str | None = None,
        internal_only: bool = False,
    ) -> Fact:
        if display is None:
            display = f"{value:,}" if isinstance(value, int) else str(value)
        return self.facts.add(
            Fact(
                id=fact_id,
                label=label,
                value=value,
                display=display,
                definition=definition,
                source=source,
                as_of=self.now,
                period_bound=period_bound,
                population=population,
                severity=severity,
                denominator=denominator,
                internal_only=internal_only,
            )
        )

    async def _guarded(
        self, what: str, source: Source, work: Callable[[], Awaitable[None]]
    ) -> None:
        """Run one collector. A vendor failure becomes a recorded gap, never a guess."""
        try:
            await work()
        except ConnectorError as exc:
            status = f"HTTP {exc.status_code}" if exc.status_code else type(exc).__name__
            self.facts.gaps.append(Gap(what=what, why=f"{status}: {exc}"[:300], source=source))

    # --- run ------------------------------------------------------------------------

    async def collect(self) -> FactSet:
        self.fact(
            "report.period.label",
            "Review period",
            self.period.label,
            "The months this review covers.",
            "config",
            period_bound=True,
        )
        self.fact(
            "report.period.months",
            "Length of the review period in months",
            self.period.months,
            "From the franchisee's report settings unless the run asked for another length.",
            "config",
            display=_count_words(self.period.months, "month"),
            period_bound=True,
        )
        await self._guarded("NinjaOne devices and hardware", "ninjaone", self._ninja_inventory)
        await self._guarded("NinjaOne locations", "ninjaone", self._ninja_locations)
        await self._guarded("NinjaOne antivirus state", "ninjaone", self._ninja_antivirus)
        await self._guarded("NinjaOne patch history", "ninjaone", self._ninja_patch_installs)
        await self._guarded("NinjaOne outstanding patches", "ninjaone", self._ninja_pending)
        await self._guarded("NinjaOne disk space", "ninjaone", self._ninja_disks)
        await self._guarded(
            "NinjaOne third-party patch history",
            "ninjaone",
            lambda: collect_software_patch_installs(self),
        )
        await self._guarded("NinjaOne lifecycle", "ninjaone", lambda: collect_lifecycle(self))
        if self.cove_mapped:
            await self._guarded(COVE_GAP, "cove", lambda: collect_cove(self))
        else:
            record_backup_gap(self)
        await self._guarded(ALERTS_RESOLVED, "ninjaone", self._ninja_alerts_resolved)
        await self._guarded(BITLOCKER_GAP, "ninjaone", lambda: collect_bitlocker(self))
        await self._guarded(PATCH_COVERAGE_GAP, "ninjaone", lambda: collect_patch_coverage(self))
        if self.bitdefender_mapped:
            await self._guarded(
                BITDEFENDER_GAP, "bitdefender", lambda: collect_bitdefender_endpoints(self)
            )
            await self._guarded(
                "Bitdefender detections",
                "bitdefender",
                lambda: collect_bitdefender_incidents(self),
            )
        else:
            self.facts.gaps.append(
                Gap(
                    what=BITDEFENDER_GAP,
                    why="This site has no Bitdefender GravityZone company mapped (or the "
                    "franchisee has no Bitdefender credentials). Run sync_sites for "
                    "bitdefender once an API key is stored.",
                    source="bitdefender",
                )
            )
        if self.connectsecure_mapped:
            # NinjaOne's software patch queue first: the reconciliation reads it.
            await self._guarded(
                PATCH_QUEUE_GAP, "ninjaone", lambda: collect_software_patch_queue(self)
            )
            await self._guarded(
                CONNECTSECURE_GAP, "connectsecure", lambda: collect_connectsecure(self)
            )
        else:
            self.facts.gaps.append(
                Gap(
                    what=CONNECTSECURE_GAP,
                    why="This site has no ConnectSecure company mapped (or the franchisee has "
                    "no ConnectSecure credentials). Run sync_sites for connectsecure once a "
                    "key is stored.",
                    source="connectsecure",
                )
            )
        await self._guarded("Autotask company", "autotask", self._autotask_company)
        await self._guarded("Autotask tickets", "autotask", self._autotask_tickets)
        await self._guarded("Autotask projects", "autotask", self._autotask_projects)
        self._derive_locations()
        await self._load_identity()
        derive_coverage(self)
        derive_scan_coverage(self)
        derive_backup(self)
        return self.facts

    async def _load_identity(self) -> None:
        """Flag on: the site's device links, as name groups the coverage derivations read.

        `identity.<tool>.paired.<device>` names the endpoints linked to the
        device, `identity.<tool>.unmatched.<device>` the candidates of an open
        queue entry naming it. Flag off: nothing, and the derivations run the
        passes themselves.
        """
        if not get_settings().device_identity_enabled or not self._ninja_ids:
            return
        from app.devices.identity import load_identity

        async with session_scope() as session:
            view = await load_identity(session, uuid.UUID(self.client_id))
        candidates: dict[tuple[str, uuid.UUID], list[str]] = {}
        for entry in view.unresolved:
            for candidate in entry.candidates:
                try:
                    key = (entry.connector_name.value, uuid.UUID(str(candidate["device_id"])))
                except (KeyError, ValueError):
                    continue
                candidates.setdefault(key, []).append(entry.vendor_name or "")
        # Every tool the board assesses, not a hard-coded pair: adding one to
        # COVERAGE_TOOLS is meant to be the only change a new tool needs, and
        # a tool missing from this loop silently becomes a fleet-wide gap.
        assessed = [
            (t.key, t.connector.value) for t in COVERAGE_TOOLS if t.assessed and t.connector
        ]
        has_links = {key: False for key, _ in assessed}
        for name, external_id in self._ninja_ids.items():
            device = view.device_for("ninjaone", external_id)
            for tool, connector in assessed:
                if device is None:
                    self.facts.groups[IDENTITY_PAIRED.format(tool=tool, name=name)] = []
                    continue
                links = view.active_links(device.id, connector)
                has_links[tool] = has_links[tool] or bool(links)
                self.facts.groups[IDENTITY_PAIRED.format(tool=tool, name=name)] = sorted(
                    link.vendor_name or "" for link in links
                )
                if any(link.matched_by == "manual" for link in links):
                    self.facts.groups[IDENTITY_MANUAL.format(tool=tool, name=name)] = ["yes"]
                if any(link.matched_by not in NAME_PASSES for link in links):
                    self.facts.groups[IDENTITY_STANDS.format(tool=tool, name=name)] = ["yes"]
                queued = candidates.get((tool, device.id))
                if queued:
                    self.facts.groups[IDENTITY_UNMATCHED.format(tool=tool, name=name)] = sorted(
                        queued
                    )
        for tool, found in has_links.items():
            if found:
                self.facts.groups[IDENTITY_TOOL.format(tool=tool)] = ["yes"]
            else:
                logger.info(
                    "tbr: the identity layer holds no %s link for this site; its coverage "
                    "comes from the inline join this run",
                    tool,
                )
        self.facts.groups[IDENTITY_LOADED] = ["yes"]

    # --- NinjaOne -------------------------------------------------------------------

    async def _ninja_inventory(self) -> None:
        async with connector_for(self.client_id, ConnectorName.NINJAONE, self.site_id) as c:
            assert isinstance(c, NinjaOneClient)
            devices, _ = await self.audit.read(
                "ninjaone.devices",
                ConnectorName.NINJAONE,
                c.list_devices,
                summarise=lambda r: {"devices": len(r[0]), "truncated": r[1]},
            )
            profiles, _ = await self.audit.read(
                "ninjaone.hardware",
                ConnectorName.NINJAONE,
                lambda: collect_hardware(c),
                summarise=lambda r: {"profiles": len(r[0]), "truncated": r[1]},
            )
            macs = await self.audit.read(
                "ninjaone.network_interfaces",
                ConnectorName.NINJAONE,
                lambda: device_macs(c),
                summarise=lambda r: {"devices_with_macs": len(r)},
            )

        # Every device name seen, used or not, under either name NinjaOne gives it:
        # a demo render must not print any of them.
        self.facts.names["devices"] = sorted(
            {d.name for _, d in devices if d.name} | {d.hostname for _, d in devices}
        )
        # The machines a protection agent belongs on: what the coverage join reads,
        # under the hostname it keys on (the full dnsName, else systemName).
        self.facts.groups[GROUP_NINJA_COMPUTERS] = sorted(
            {d.hostname for _, d in devices if is_agent_class(d.node_class)}
        )
        # What the board's passes need beside the names: which devices the RMM
        # itself has lost (never counted as absent from another tool), and each
        # device's interface MACs for the third pass.
        rmm_days = rmm_stale_days()
        self.facts.groups[GROUP_NINJA_RMM_STALE] = sorted(
            {
                d.hostname
                for _, d in devices
                if is_agent_class(d.node_class)
                and (d.last_contact is None or (self.now - d.last_contact).days > rmm_days)
            }
        )
        for _, d in devices:
            if d.hostname in self.facts.groups[GROUP_NINJA_RMM_STALE] and d.last_contact:
                self.facts.groups[GROUP_NINJA_LOST_DAYS.format(name=d.hostname)] = [
                    str((self.now - d.last_contact).days)
                ]
        for _, d in devices:
            found = macs.get(d.id)
            if found:
                self.facts.groups[GROUP_NINJA_MACS.format(name=d.hostname)] = sorted(found)
        # For the identity layer: which NinjaOne record each hostname is.
        self._ninja_ids = {d.hostname: str(d.id) for _, d in devices}
        self.ninja_devices = list(devices)
        self.ninja_profiles = list(profiles)
        by_device = {profile.device.id: profile for profile in profiles}
        # Workstation or server is decided by the operating system where it can be
        # (`device_role`): NinjaOne's node class put a Windows 10 IoT till among the
        # servers. The same decision feeds the PDF's lifecycle tables.
        self.ninja_roles = {
            d.id: device_role(by_device[d.id].os_name if d.id in by_device else None, d.node_class)
            for _, d in devices
        }
        workstations = [d for _, d in devices if self.ninja_roles[d.id].role == "workstation"]
        servers = [d for _, d in devices if self.ninja_roles[d.id].role == "server"]
        kinds = Counter(by_device[d.id].kind if d.id in by_device else "unknown" for d in servers)
        other = len(devices) - len(workstations) - len(servers)
        disagreeing = sorted(
            (
                [
                    d.name,
                    by_device[d.id].os_name or "not reported",
                    d.node_class or "not reported",
                    self.ninja_roles[d.id].role,
                ]
                for _, d in devices
                if self.ninja_roles[d.id].disagrees
            ),
            key=lambda r: r[0].lower(),
        )
        self.facts.tables[ROLE_DISAGREEMENTS] = FactTable(
            id=ROLE_DISAGREEMENTS,
            label="Devices whose operating system and NinjaOne node class disagree",
            definition=(
                "Devices counted by their operating system where NinjaOne's node class "
                "would have put them elsewhere: Windows is a server only when the OS name "
                "says Windows Server, macOS is a workstation, and the node class decides "
                "the rest. Internal only: it explains the counts, it is not a finding."
            ),
            source="ninjaone",
            columns=["Device", "Operating system", "NinjaOne node class", "Counted as"],
            rows=disagreeing,
            total=len(disagreeing),
            as_of=self.now,
            internal_only=True,
        )

        self.fact(
            "ninjaone.devices.total",
            "Devices under management",
            len(devices),
            "Every device NinjaOne lists for this customer's organisation, any class.",
            "ninjaone",
        )
        self.fact(
            "ninjaone.devices.workstations",
            "Workstations and laptops",
            len(workstations),
            "Devices running a Windows edition other than Windows Server, or macOS, "
            "whatever NinjaOne's node class says; Linux and other systems by node class "
            "(Windows, Mac or Linux workstation).",
            "ninjaone",
        )
        self.fact(
            "ninjaone.devices.servers_physical",
            "Physical servers",
            kinds["physical"],
            "Devices running Windows Server, or a non-Windows system with a server node "
            "class, that NinjaOne does not flag as virtual and whose manufacturer and model "
            "are not a hypervisor's.",
            "ninjaone",
        )
        self.fact(
            "ninjaone.devices.servers_virtual",
            "Virtual servers",
            kinds["virtual"],
            "Devices running Windows Server, or a non-Windows system with a server or "
            "VM-guest node class, that NinjaOne flags as virtual or whose manufacturer or "
            "model is a hypervisor's (VMware, Hyper-V, KVM and so on).",
            "ninjaone",
        )
        if kinds["unknown"]:
            self.fact(
                "ninjaone.devices.servers_unclassified",
                "Servers NinjaOne reported no hardware for",
                kinds["unknown"],
                "Server-class devices with no computer-system row, so neither physical nor "
                "virtual can be said.",
                "ninjaone",
            )
        if other:
            self.fact(
                "ninjaone.devices.other",
                "Other managed devices",
                other,
                "Network devices, mobile devices and anything else that is neither a "
                "workstation nor a server by node class.",
                "ninjaone",
            )

        # --- Windows 10 and the hardware under it (slides 12 and 13) ---
        statuses: Counter[str] = Counter(p.windows11.status for p in profiles)
        for status, label, definition in (
            (
                "ineligible",
                "Windows 10 devices that cannot run Windows 11",
                "On Windows 10, with a processor older than Windows 11 supports or under 4 GB "
                "of memory. " + WINDOWS11_BASIS,
            ),
            (
                "eligible",
                "Windows 10 devices that can run Windows 11",
                "On Windows 10, with a supported processor generation and at least 4 GB of "
                "memory. " + WINDOWS11_BASIS,
            ),
            (
                "unknown",
                "Windows 10 devices whose Windows 11 eligibility could not be judged",
                "On Windows 10, but the processor generation or memory could not be read.",
            ),
        ):
            self.fact(
                f"ninjaone.windows11.{status}", label, statuses[status], definition, "ninjaone"
            )

        def device_rows(wanted: str) -> list[list[str]]:
            rows = [
                [
                    p.device.name,
                    " ".join(x for x in (p.system.manufacturer, p.system.model) if x) or "unknown",
                    p.processor or "unknown",
                    "; ".join(p.windows11.reasons) or "meets the processor and memory requirement",
                ]
                for p in profiles
                if p.windows11.status == wanted
            ]
            return sorted(rows)

        for status, label in (
            ("ineligible", "Windows 10 devices to replace"),
            ("eligible", "Windows 10 devices to upgrade to Windows 11"),
        ):
            rows = device_rows(status)
            if rows:
                self.facts.tables[f"ninjaone.windows11.{status}.devices"] = FactTable(
                    id=f"ninjaone.windows11.{status}.devices",
                    label=label,
                    definition=WINDOWS11_BASIS,
                    source="ninjaone",
                    columns=["Device", "Model", "Processor", "Why"],
                    rows=rows,
                    total=len(rows),
                    as_of=self.now,
                )

        threshold = self.config.low_memory_gb
        low_memory = sorted(
            [
                p.device.name,
                " ".join(x for x in (p.system.manufacturer, p.system.model) if x) or "unknown",
                f"{(p.system.total_physical_memory or 0) / 2**30:.0f} GB",
            ]
            for p in profiles
            if self.ninja_roles.get(p.device.id, device_role(None, None)).role == "workstation"
            and p.system.total_physical_memory
            # Reported memory is a little under what is fitted: 8 GB reads as 7.x.
            and p.system.total_physical_memory / 2**30 < threshold - 0.75
        )
        self.fact(
            "ninjaone.hardware.low_memory",
            f"Workstations with less than {threshold:g} GB of memory",
            len(low_memory),
            f"Workstations whose installed memory, as NinjaOne reports it, is below "
            f"{threshold:g} GB (the franchisee's threshold).",
            "ninjaone",
        )
        if low_memory:
            self.facts.tables["ninjaone.hardware.low_memory.devices"] = FactTable(
                id="ninjaone.hardware.low_memory.devices",
                label="Workstations to upgrade memory on",
                definition=f"Installed memory below {threshold:g} GB.",
                source="ninjaone",
                columns=["Device", "Model", "Memory"],
                rows=low_memory,
                total=len(low_memory),
                as_of=self.now,
            )

    async def _ninja_locations(self) -> None:
        async with connector_for(self.client_id, ConnectorName.NINJAONE, self.site_id) as c:
            assert isinstance(c, NinjaOneClient)
            locations = await self.audit.read(
                "ninjaone.locations",
                ConnectorName.NINJAONE,
                c.list_locations,
                summarise=lambda r: {"locations": len(r)},
            )
        self.ninja_locations = {
            location.id: location.name or f"location {location.id}" for _, location in locations
        }
        self.fact(
            "ninjaone.locations",
            "Locations in NinjaOne",
            len(locations),
            "Locations defined on this customer's NinjaOne organisation.",
            "ninjaone",
        )

    async def _ninja_antivirus(self) -> None:
        async with connector_for(self.client_id, ConnectorName.NINJAONE, self.site_id) as c:
            assert isinstance(c, NinjaOneClient)

            async def read() -> list[tuple[Any, Any, NinjaAntivirusStatus]]:
                raw, _, _ = await c.query_scoped("antivirus-status")
                rows = [NinjaAntivirusStatus.model_validate(item) for item in raw]
                return await c.place_by_device(
                    rows,
                    [row.device_id for row in rows],
                    endpoint=QUERY_PATH.format(report="antivirus-status"),
                )

            placed = await self.audit.read(
                "ninjaone.antivirus",
                ConnectorName.NINJAONE,
                read,
                summarise=lambda r: {"rows": len(r)},
            )

        # A machine with Defender off beside another product on is protected:
        # judge the device, not the row.
        per_device: dict[int, list[NinjaAntivirusStatus]] = {}
        names: dict[int, str] = {}
        for _, device, row in placed:
            per_device.setdefault(device.id, []).append(row)
            names[device.id] = device.name
        unprotected = sorted(
            names[device_id]
            for device_id, rows in per_device.items()
            if not any(row.healthy for row in rows)
        )
        self.fact(
            "ninjaone.antivirus.devices_reporting",
            "Devices reporting antivirus state",
            len(per_device),
            "Devices with at least one row in NinjaOne's antivirus-status report.",
            "ninjaone",
        )
        self.fact(
            "ninjaone.antivirus.devices_unprotected",
            "Devices with no healthy antivirus product",
            len(unprotected),
            "Devices where no antivirus product is both switched on and up to date, as the "
            "NinjaOne agent sees it. A device with one product off and another on and current "
            "is counted as protected.",
            "ninjaone",
        )
        if per_device:
            self.fact(
                "ninjaone.antivirus.protected_percent",
                "Share of reporting devices with a healthy antivirus product",
                _percent(len(per_device) - len(unprotected), len(per_device)),
                "Protected devices as a percentage of devices reporting antivirus state.",
                "ninjaone",
                display=f"{_percent(len(per_device) - len(unprotected), len(per_device)):g}%",
                denominator="ninjaone.antivirus.devices_reporting",
            )
        if unprotected:
            self.facts.tables["ninjaone.antivirus.unprotected.devices"] = FactTable(
                id="ninjaone.antivirus.unprotected.devices",
                label="Devices with no healthy antivirus product",
                definition="See ninjaone.antivirus.devices_unprotected.",
                source="ninjaone",
                columns=["Device"],
                rows=[[name] for name in unprotected],
                total=len(unprotected),
                as_of=self.now,
            )

    async def _ninja_patch_installs(self) -> None:
        params = {
            "installedAfter": f"{self.period.start:%Y-%m-%d}",
            "installedBefore": f"{self.period.end:%Y-%m-%d}",
        }
        async with connector_for(self.client_id, ConnectorName.NINJAONE, self.site_id) as c:
            assert isinstance(c, NinjaOneClient)

            async def read() -> list[tuple[Any, Any, NinjaPatchInstall]]:
                raw, _, _ = await c.query_scoped("os-patch-installs", params=params)
                rows = [NinjaPatchInstall.model_validate(item) for item in raw]
                return await c.place_by_device(
                    rows,
                    [row.device_id for row in rows],
                    endpoint=QUERY_PATH.format(report="os-patch-installs"),
                )

            placed = await self.audit.read(
                "ninjaone.patch_installs",
                ConnectorName.NINJAONE,
                read,
                summarise=lambda r: {"rows": len(r)},
            )

        # Bounded again here: the vendor's date filters are day-granular.
        in_period = [
            (device, row)
            for _, device, row in placed
            if row.installed_at is None or self.period.start <= row.installed_at < self.period.end
        ]
        installed = [x for x in in_period if (x[1].status or "").upper() == "INSTALLED"]
        failed = [x for x in in_period if (x[1].status or "").upper() == "FAILED"]
        self.fact(
            "ninjaone.patches.installed",
            "Windows patches installed in the period",
            len(installed),
            "Rows of NinjaOne's OS patch installation history with status INSTALLED and an "
            "install time inside the review period.",
            "ninjaone",
            period_bound=True,
        )
        self.fact(
            "ninjaone.patches.failed",
            "Windows patch installations that failed in the period",
            len(failed),
            "Rows of the same history with status FAILED inside the review period. One patch "
            "failing on one device twice is two rows.",
            "ninjaone",
            period_bound=True,
        )
        record_failed_devices(
            self,
            failed,
            devices_fact="ninjaone.patches.failed_devices",
            top_fact=OS_TOP_FAILED,
            table_id=OS_FAILED_LIST,
            what="Windows patch",
        )
        # Devices NinjaOne is patching: any install attempt in the period counts.
        patched = set(self.facts.groups.get(GROUP_NINJA_PATCHING, []))
        patched.update(device.name for device, _ in in_period if device.name)
        self.facts.groups[GROUP_NINJA_PATCHING] = sorted(patched)
        attempts = len(installed) + len(failed)
        if attempts:
            self.fact(
                "ninjaone.patches.attempted",
                "Patch installations attempted in the period",
                attempts,
                "Installed plus failed rows in the period: what the success rate is out of.",
                "ninjaone",
                period_bound=True,
            )
            self.fact(
                "ninjaone.patches.success_percent",
                "Patch installation success rate in the period",
                _percent(len(installed), attempts),
                "Installed rows as a percentage of installed plus failed rows in the period.",
                "ninjaone",
                display=f"{_percent(len(installed), attempts):g}%",
                period_bound=True,
                denominator="ninjaone.patches.attempted",
            )

    async def _ninja_alerts_resolved(self) -> None:
        """Monitoring conditions that cleared in the period, if the log still covers it.

        NinjaOne lists only *active* alerts; what was resolved is in the activity
        log, which the vendor keeps for a limited time and truncates silently.
        So the log is first asked whether it reaches back before the period. If
        it does not, or the page cap cut the walk short, there is no figure: a
        partial count would look like a quiet month.
        """
        start, end = self.period.start, self.period.end
        async with connector_for(self.client_id, ConnectorName.NINJAONE, self.site_id) as c:
            assert isinstance(c, NinjaOneClient)
            covered = await self.audit.read(
                "ninjaone.activities.coverage",
                ConnectorName.NINJAONE,
                lambda: c.activity_log_reaches(start),
                summarise=lambda reached: {"log_reaches_period_start": reached},
            )
            if not covered:
                self.facts.gaps.append(
                    Gap(
                        what=ALERTS_RESOLVED,
                        why="NinjaOne's activity log for this site holds nothing from before "
                        f"{_date(start)}, so it no longer covers the whole period. The figure "
                        "is left out rather than undercounted. Generate the review within "
                        "the log's retention to include it.",
                        source="ninjaone",
                    )
                )
                return
            placed, truncated = await self.audit.read(
                "ninjaone.activities.condition_reset",
                ConnectorName.NINJAONE,
                lambda: c.list_activities(
                    activity_type="condition", status="reset", after=start, before=end
                ),
                summarise=lambda r: {"rows": len(r[0]), "truncated": r[1]},
            )
        if truncated:
            self.facts.gaps.append(
                Gap(
                    what=ALERTS_RESOLVED,
                    why="The activity log ran past the page cap, so the count would be partial.",
                    source="ninjaone",
                )
            )
            return
        resolved = [
            (device, row)
            for _, device, row in placed
            if row.activity_time is not None and start <= row.activity_time < end
        ]
        self.fact(
            "ninjaone.alerts.resolved",
            "Monitoring alerts resolved in the period",
            len(resolved),
            "Entries in NinjaOne's activity log of type CONDITION with status RESET and a time "
            "inside the review period: a monitoring condition that had triggered an alert and "
            "then cleared, whether by itself, by an automation or by a technician. One "
            "condition clearing twice on one device is two entries.",
            "ninjaone",
            period_bound=True,
        )
        self.fact(
            "ninjaone.alerts.resolved_devices",
            "Devices with at least one alert resolved in the period",
            len({device.id for device, _ in resolved}),
            "Distinct devices behind ninjaone.alerts.resolved.",
            "ninjaone",
            period_bound=True,
        )

    async def _ninja_pending(self) -> None:
        async with connector_for(self.client_id, ConnectorName.NINJAONE, self.site_id) as c:
            assert isinstance(c, NinjaOneClient)

            async def read() -> list[tuple[Any, Any, NinjaOSPatch]]:
                # One request per status and the vendor's exact spelling: api-notes.
                raw, _, _ = await c.query_scoped(
                    "os-patches", params={"status": "APPROVED", "severity": "CRITICAL"}
                )
                rows = [
                    row
                    for row in (NinjaOSPatch.model_validate(item) for item in raw)
                    if (row.status or "").upper() == "APPROVED"
                    and (row.severity or "").upper() == "CRITICAL"
                ]
                return await c.place_by_device(
                    rows,
                    [row.device_id for row in rows],
                    endpoint=QUERY_PATH.format(report="os-patches"),
                )

            placed = await self.audit.read(
                "ninjaone.critical_pending",
                ConnectorName.NINJAONE,
                read,
                summarise=lambda r: {"rows": len(r)},
            )
        # An approved patch waiting on a device is NinjaOne patching it too.
        patched = set(self.facts.groups.get(GROUP_NINJA_PATCHING, []))
        patched.update(device.name for _, device, _ in placed if device.name)
        self.facts.groups[GROUP_NINJA_PATCHING] = sorted(patched)
        self.fact(
            "ninjaone.patches.critical_pending",
            "Critical Windows patches approved and not yet installed",
            len(placed),
            "Rows of NinjaOne's pending OS patch report with status APPROVED and severity "
            "CRITICAL, at the time the report was generated.",
            "ninjaone",
        )
        self.fact(
            "ninjaone.patches.critical_pending_devices",
            "Devices waiting on a critical Windows patch",
            len({device.id for _, device, _ in placed}),
            "Distinct devices behind ninjaone.patches.critical_pending.",
            "ninjaone",
        )

    async def _ninja_disks(self) -> None:
        limit = self.config.low_disk_percent
        async with connector_for(self.client_id, ConnectorName.NINJAONE, self.site_id) as c:
            assert isinstance(c, NinjaOneClient)

            async def read() -> list[tuple[Any, Any, NinjaVolume]]:
                raw, _, _ = await c.query_scoped("volumes")
                rows = [NinjaVolume.model_validate(item) for item in raw]
                return await c.place_by_device(
                    rows,
                    [row.device_id for row in rows],
                    endpoint=QUERY_PATH.format(report="volumes"),
                )

            placed = await self.audit.read(
                "ninjaone.volumes",
                ConnectorName.NINJAONE,
                read,
                summarise=lambda r: {"rows": len(r)},
            )
        self.ninja_volumes = list(placed)
        low = sorted(
            [
                device.name,
                volume.drive_letter or volume.name or "?",
                f"{volume.free_percent:.0f}% free",
            ]
            for _, device, volume in placed
            if (volume.device_type or "") == "Local Disk"
            and volume.capacity
            and volume.free_percent is not None
            and volume.free_percent < limit
        )
        self.fact(
            "ninjaone.disks.low",
            f"Fixed disks with less than {limit:g}% free",
            len(low),
            f"Local Disk volumes with a reported capacity whose free space is under {limit:g}% "
            "(the franchisee's threshold).",
            "ninjaone",
        )
        if low:
            self.facts.tables["ninjaone.disks.low.volumes"] = FactTable(
                id="ninjaone.disks.low.volumes",
                label="Disks running out of space",
                definition=f"Free space below {limit:g}%.",
                source="ninjaone",
                columns=["Device", "Volume", "Free"],
                rows=low,
                total=len(low),
                as_of=self.now,
            )

    # --- Autotask -------------------------------------------------------------------

    async def _autotask_company(self) -> None:
        async with connector_for(self.client_id, ConnectorName.AUTOTASK, self.site_id) as c:
            assert isinstance(c, AutotaskClient)
            # Every company the site holds, not the first mapping: the industry
            # is the one they agree on, or each of them named.
            site = c.scope.targets[0]
            companies = await self.audit.read(
                "autotask.company",
                ConnectorName.AUTOTASK,
                lambda: c.get_site_companies(site.site_id),
                summarise=lambda r: {"companies": len(r)},
            )
            labels: list[str] = []
            for _, company in companies:
                label = await c.label_for("Companies", "marketSegmentID", company.market_segment_id)
                if label and label not in labels:
                    labels.append(label)
            industry = " / ".join(labels) or None
            locations = await self.audit.read(
                "autotask.locations",
                ConnectorName.AUTOTASK,
                c.list_company_locations,
                summarise=lambda r: {"locations": len(r)},
            )
        if industry:
            self.fact(
                "autotask.company.industry",
                "Industry",
                industry,
                "The company's market segment in Autotask, by this tenant's label.",
                "autotask",
            )
        self.fact(
            "autotask.locations",
            "Active company locations in Autotask",
            len(locations),
            "CompanyLocations rows for this company with isActive true.",
            "autotask",
        )

    async def _autotask_tickets(self) -> None:
        start, end = self.period.start, self.period.end
        async with connector_for(self.client_id, ConnectorName.AUTOTASK, self.site_id) as c:
            assert isinstance(c, AutotaskClient)
            created, _ = await self.audit.read(
                "autotask.tickets.created",
                ConnectorName.AUTOTASK,
                lambda: c.list_tickets(open_only=False, created_since=start, created_before=end),
                summarise=lambda r: {"tickets": len(r[0]), "truncated": r[1]},
            )
            completed, _ = await self.audit.read(
                "autotask.tickets.completed",
                ConnectorName.AUTOTASK,
                lambda: c.list_tickets(
                    open_only=False, completed_since=start, completed_before=end
                ),
                summarise=lambda r: {"tickets": len(r[0]), "truncated": r[1]},
            )
            open_now, _ = await self.audit.read(
                "autotask.tickets.open",
                ConnectorName.AUTOTASK,
                lambda: c.list_tickets(open_only=True),
                summarise=lambda r: {"tickets": len(r[0]), "truncated": r[1]},
            )
            issue_types = await c.picklist("Tickets", "issueType")
            sub_issue_types = await c.picklist("Tickets", "subIssueType")
            priorities = await c.picklist("Tickets", "priority")
            # Statuses too: the SLA clock stops in some of them, and which is a
            # franchisee setting named by label rather than by picklist number.
            statuses = await c.picklist("Tickets", "status")
            sources = await c.picklist("Tickets", "source")
            contact_ids = [t.contact_id for _, t in created if t.contact_id]
            contacts = await c.get_contacts(list(dict.fromkeys(contact_ids)))

        self.facts.names["contacts"] = sorted(
            {
                " ".join(x for x in (person.first_name, person.last_name) if x)
                for person in contacts.values()
                if person.first_name or person.last_name
            }
        )
        created_tickets = [ticket for _, ticket in created]
        completed_tickets = [ticket for _, ticket in completed]
        open_tickets = [ticket for _, ticket in open_now]

        self.fact(
            "autotask.tickets.created",
            "Tickets created in the period",
            len(created_tickets),
            "Autotask tickets for this company whose create date is inside the review period.",
            "autotask",
            period_bound=True,
        )
        self.fact(
            "autotask.tickets.completed",
            "Tickets completed in the period",
            len(completed_tickets),
            "Autotask tickets whose completed date is inside the review period, whenever they "
            "were created.",
            "autotask",
            period_bound=True,
        )
        self._out_of_hours(completed_tickets)
        self.fact(
            "autotask.tickets.open",
            "Tickets open now",
            len(open_tickets),
            "Tickets with no completed date at the time the report was generated.",
            "autotask",
        )

        # --- created against completed, by day ---
        days = [start + dt.timedelta(days=n) for n in range((end - start).days)]
        if created_tickets or completed_tickets:
            made = Counter(t.create_date.date() for t in created_tickets if t.create_date)
            done = Counter(t.completed_date.date() for t in completed_tickets if t.completed_date)
            self.facts.charts["autotask.chart.daily"] = ChartData(
                id="autotask.chart.daily",
                title="Tickets created and completed by day",
                kind="line",
                categories=[f"{day:%d %b}".lstrip("0") for day in days],
                series=[
                    ChartSeries(name="Created", values=[float(made[d.date()]) for d in days]),
                    ChartSeries(name="Completed", values=[float(done[d.date()]) for d in days]),
                ],
                definition="Daily counts behind autotask.tickets.created and .completed (UTC).",
                source="autotask",
            )

        # --- by issue type ---
        by_type = Counter(
            issue_types.get(t.issue_type or -1, "No issue type") for t in created_tickets
        )
        if by_type:
            top = by_type.most_common(MAX_CHART_CATEGORIES)
            label, count = top[0]
            self.fact(
                "autotask.tickets.top_issue_type",
                "Most common issue type in the period",
                label,
                "The issue type label with the most tickets created in the period.",
                "autotask",
                period_bound=True,
            )
            self.fact(
                "autotask.tickets.top_issue_type_share",
                "Share of tickets in the most common issue type",
                _percent(count, len(created_tickets)),
                "Tickets of the most common issue type as a percentage of tickets created.",
                "autotask",
                display=f"{_percent(count, len(created_tickets)):g}%",
                period_bound=True,
                denominator="autotask.tickets.created",
            )
            self.facts.charts["autotask.chart.issue_type"] = ChartData(
                id="autotask.chart.issue_type",
                title="Tickets by issue type",
                kind="bar",
                categories=[name for name, _ in reversed(top)],
                series=[ChartSeries(name="Tickets", values=[float(n) for _, n in reversed(top)])],
                definition=f"Tickets created in the period by issue type; top "
                f"{MAX_CHART_CATEGORIES}.",
                source="autotask",
            )

        # --- sub-issue types, when the issue type says little ---
        # A tenant that files most tickets as "Other" or leaves the field empty has
        # nothing to show in the issue-type chart; the sub-issue type may say more,
        # and when it does not either the report says the field is largely unset.
        if created_tickets:
            vague = sum(
                count
                for label, count in by_type.items()
                if label == "No issue type" or label.casefold().startswith("other")
            )
            vague_share = _percent(vague, len(created_tickets))
            self.fact(
                "autotask.tickets.issue_type_unset_share",
                "Share of tickets with no issue type or filed as Other",
                vague_share,
                "Tickets created in the period whose issue type is empty or an 'Other' label, as "
                "a percentage of tickets created.",
                "autotask",
                display=f"{vague_share:g}%",
                period_bound=True,
                denominator="autotask.tickets.created",
            )
            if vague_share > 50:
                by_sub = Counter(
                    sub_issue_types.get(t.sub_issue_type or -1, "No sub-issue type")
                    for t in created_tickets
                )
                named = {k: v for k, v in by_sub.items() if k != "No sub-issue type"}
                self.fact(
                    "autotask.tickets.sub_issue_type_set",
                    "Tickets with a sub-issue type set",
                    sum(named.values()),
                    "Tickets created in the period whose sub-issue type is set to anything.",
                    "autotask",
                    period_bound=True,
                )
                if named:
                    top_sub = Counter(named).most_common(MAX_CHART_CATEGORIES)
                    self.facts.charts["autotask.chart.sub_issue_type"] = ChartData(
                        id="autotask.chart.sub_issue_type",
                        title="Tickets by sub-issue type",
                        kind="bar",
                        categories=[name for name, _ in reversed(top_sub)],
                        series=[
                            ChartSeries(
                                name="Tickets", values=[float(n) for _, n in reversed(top_sub)]
                            )
                        ],
                        definition="Tickets created in the period by sub-issue type, shown "
                        "because more than half the tickets have no issue type or are filed as "
                        f"Other; top {MAX_CHART_CATEGORIES}.",
                        source="autotask",
                    )

        # --- open by priority ---
        by_priority = Counter(priorities.get(t.priority or -1, "No priority") for t in open_tickets)
        if by_priority:
            ordered = by_priority.most_common()
            self.facts.charts["autotask.chart.open_priority"] = ChartData(
                id="autotask.chart.open_priority",
                title="Open tickets by priority",
                kind="column",
                categories=[name for name, _ in ordered],
                series=[ChartSeries(name="Open", values=[float(n) for _, n in ordered])],
                definition="Tickets open when the report was generated, by priority label.",
                source="autotask",
            )

        # --- SLA ---
        # Judged against the franchisee's own targets on their own business
        # hours, not against whatever due times Autotask happened to carry:
        # those are populated on about two thirds of tickets on the pilot and
        # `serviceLevelAgreementHasBeenMet` on none, so the old figure was a
        # percentage over the tickets somebody had remembered to configure.
        # A clock the calculation will not judge — a paused ticket, a priority
        # with no target — is left out of both the numerator and the
        # denominator rather than counted as met.
        sla_zone, _ = franchisee_zone(self.config.business_hours.timezone)
        calculator = SlaCalculator(self.config.resolved, self.config.business_hours, sla_zone)

        def _judge(tickets: list[Any], clock: str) -> tuple[int, int]:
            met = 0
            measured = 0
            for ticket in tickets:
                verdict = calculator.assess(
                    ticket,
                    priority_label=priorities.get(ticket.priority or -1),
                    status_label=statuses.get(ticket.status or -1),
                    now=self.now,
                )
                decided = getattr(verdict, clock).met
                if decided is None:
                    continue
                measured += 1
                met += 1 if decided else 0
            return met, measured

        resolution_count, resolution_measured = _judge(completed_tickets, "resolution")
        response_count, response_measured = _judge(created_tickets, "first_response")
        sla_names: list[str] = []
        sla_values: list[float] = []
        for fact_id, label, met, measured, definition in (
            (
                "autotask.sla.resolution_met_percent",
                "Resolution SLA met",
                resolution_count,
                resolution_measured,
                "Of tickets completed in the period whose priority has a resolution target, "
                "the percentage completed inside it. The clock runs on this franchisee's "
                "business hours, holidays and time zone, from the moment the ticket was "
                "raised. Tickets whose priority has no target are left out of both figures.",
            ),
            (
                "autotask.sla.first_response_met_percent",
                "First-response SLA met",
                response_count,
                response_measured,
                "Of tickets created in the period whose priority has a first-response target, "
                "the percentage first answered inside it, on the same clock. A ticket never "
                "answered counts as missed. A ticket still waiting on the customer is left "
                "out of both figures: how long it has been waiting is not something Autotask "
                "tells us.",
            ),
        ):
            if not measured:
                continue
            value = _percent(met, measured)
            self.fact(
                fact_id,
                label,
                value,
                definition,
                "autotask",
                display=f"{value:g}%",
                period_bound=True,
                denominator=fact_id.replace("_met_percent", "_measured"),
            )
            self.fact(
                fact_id.replace("_met_percent", "_measured"),
                f"{label}: tickets measured",
                measured,
                "How many tickets the percentage is out of.",
                "autotask",
                period_bound=True,
            )
            sla_names.append(label)
            sla_values.append(value)
        if sla_names:
            self.facts.charts["autotask.chart.sla"] = ChartData(
                id="autotask.chart.sla",
                title="SLA performance (%)",
                kind="column",
                categories=sla_names,
                series=[ChartSeries(name="Met", values=sla_values)],
                definition="See autotask.sla.*_met_percent.",
                source="autotask",
                number_format='0"%"',
            )

        # --- who asks, with machines kept apart from people ---
        def contact_name(contact_id: int | None) -> str:
            person = contacts.get(contact_id or -1)
            if person is None:
                return ""
            return " ".join(x for x in (person.first_name, person.last_name) if x)

        # A "contact" can be a mailbox an integration raises tickets from, or the
        # provider's own support desk. Neither is one of the customer's people.
        provider = self.config.display_name.split()[0].casefold()

        def automated(ticket: Any) -> bool:
            if AUTOMATION_SOURCE.search(sources.get(ticket.source or -1, "")):
                return True
            name = contact_name(ticket.contact_id)
            return bool(name) and (
                bool(SYSTEM_CONTACT.search(name))
                or (len(provider) > 3 and provider in name.casefold())
            )

        machine = [t for t in created_tickets if automated(t)]
        human = [t for t in created_tickets if not automated(t)]
        self.fact(
            "autotask.tickets.automated",
            "Tickets raised by monitoring or automation in the period",
            len(machine),
            "Tickets created in the period whose source label names monitoring, alerting or "
            "another system, or whose contact is a system account (a name containing "
            "automation, no-reply, alerts, monitoring, support or the provider's own name), "
            "rather than one of the customer's people.",
            "autotask",
            period_bound=True,
        )
        self.fact(
            "autotask.tickets.from_people",
            "Tickets raised by people in the period",
            len(human),
            "Tickets created in the period that are not counted as automated.",
            "autotask",
            period_bound=True,
        )
        by_contact = Counter(t.contact_id for t in human if t.contact_id)
        if by_contact or machine:
            names: list[str] = []
            values: list[float] = []
            for contact_id, count in by_contact.most_common(TOP_CONTACTS):
                names.append(contact_name(contact_id) or "Unnamed contact")
                values.append(float(count))
            if machine:
                names.append("Automation and system accounts")
                values.append(float(len(machine)))
            self.facts.charts["autotask.chart.top_contacts"] = ChartData(
                id="autotask.chart.top_contacts",
                title="Who raised tickets",
                kind="bar",
                categories=list(reversed(names)),
                series=[ChartSeries(name="Tickets", values=list(reversed(values)))],
                definition=f"The {TOP_CONTACTS} contacts with the most tickets created in the "
                "period, with tickets raised by monitoring or automation shown as its own bar.",
                source="autotask",
            )
            if by_contact:
                _, most = by_contact.most_common(1)[0]
                self.fact(
                    "autotask.tickets.top_contact_share",
                    "Share of people's tickets raised by the single most frequent contact",
                    _percent(most, len(human)),
                    "Tickets from the contact with the most, as a percentage of tickets raised "
                    "by people in the period.",
                    "autotask",
                    display=f"{_percent(most, len(human)):g}%",
                    period_bound=True,
                    denominator="autotask.tickets.from_people",
                )

        passwords = [
            t for t in human if PASSWORD_TICKET.search(f"{t.title or ''} {t.description or ''}")
        ]
        self.fact(
            "autotask.tickets.password",
            "Password and account-lockout tickets in the period",
            len(passwords),
            "Tickets raised by people in the period whose title or description mentions a "
            "password, a lockout or an MFA reset.",
            "autotask",
            period_bound=True,
        )

    def _out_of_hours(self, completed_tickets: list[Any]) -> None:
        """Completed tickets whose completion fell outside the customer's business hours."""
        hours = self.config.business_hours
        if not hours.timezone:
            self.facts.gaps.append(
                Gap(
                    what=OUT_OF_HOURS,
                    why="No business-hours time zone is set for this franchisee, and one is not "
                    "guessed. Set it with `manage_client.py report-config --timezone`.",
                    source="config",
                )
            )
            return
        try:
            zone = resolve_zone(hours.timezone)
        except TimeZoneUnavailable as exc:
            self.facts.gaps.append(Gap(what=OUT_OF_HOURS, why=str(exc)[:300], source="config"))
            return
        timed = [t for t in completed_tickets if t.completed_date is not None]
        outside = [t for t in timed if not hours.contains(t.completed_date, zone)]
        self.fact(
            "autotask.tickets.completed_out_of_hours",
            "Tickets resolved outside business hours in the period",
            len(outside),
            "Of the tickets behind autotask.tickets.completed, those whose completed time falls "
            f"outside {hours.describe()} in the {hours.timezone} time zone (the franchisee's "
            "report settings). It is when the ticket was completed, not when work on it was done.",
            "autotask",
            period_bound=True,
        )
        self.fact(
            "report.business_hours",
            "Business hours used for this review",
            hours.describe(),
            f"From the franchisee's report settings; times are in {hours.timezone}.",
            "config",
        )

    async def _autotask_projects(self) -> None:
        async with connector_for(self.client_id, ConnectorName.AUTOTASK, self.site_id) as c:
            assert isinstance(c, AutotaskClient)
            types = await c.picklist("Projects", "projectType")
            wanted = [
                value
                for value, label in types.items()
                if label.strip().casefold() not in NON_CLIENT_PROJECT_TYPES
            ]
            complete = await c.resolve_picklist_value("Projects", "status", "Complete")
            placed, _ = await self.audit.read(
                "autotask.projects",
                ConnectorName.AUTOTASK,
                lambda: c.list_projects(project_types=wanted),
                summarise=lambda r: {"projects": len(r[0])},
            )
            phases = await c.list_phases([project.id for _, project in placed])
            statuses = await c.picklist("Projects", "status")

        by_project: dict[int, list[Any]] = {}
        for phase in phases:
            if phase.project_id is not None:
                by_project.setdefault(phase.project_id, []).append(phase)

        current: list[list[str]] = []
        scheduled: list[list[str]] = []
        finished: list[list[str]] = []
        overdue = 0
        for _, project in placed:
            name = project.project_name or "Unnamed project"
            done = project.status == complete or project.completed_date_time is not None
            if done:
                when = project.completed_date_time
                if when is not None and self.period.start <= when < self.period.end:
                    finished.append([name, _date(when)])
                continue
            if project.start_date_time is not None and project.start_date_time > self.now:
                scheduled.append([name, _month(project.start_date_time)])
                continue
            if project.end_date_time is not None and project.end_date_time < self.now:
                overdue += 1
            percent = project.completed_percentage
            current.append(
                [
                    name,
                    current_phase(by_project.get(project.id, []), self.now) or "",
                    f"{percent}%" if percent is not None else "not recorded",
                    _month(project.end_date_time),
                    " ".join((project.description or "").split())[:400],
                    statuses.get(project.status or -1, "In progress"),
                    "overdue"
                    if project.end_date_time is not None and project.end_date_time < self.now
                    else "",
                ]
            )

        for fact_id, label, rows, definition, columns in (
            (
                "autotask.projects.current",
                "Projects in progress",
                current,
                "Client projects in Autotask that are not complete and whose start date is "
                "not in the future.",
                ["Project", "Phase", "Complete", "Due", "Description", "Status", "Late"],
            ),
            (
                "autotask.projects.scheduled",
                "Projects scheduled to start",
                scheduled,
                "Client projects in Autotask that are not complete and start after today.",
                ["Project", "Starts"],
            ),
            (
                "autotask.projects.completed",
                "Projects completed in the period",
                finished,
                "Client projects whose completed date is inside the review period.",
                ["Project", "Completed"],
            ),
        ):
            self.fact(
                fact_id,
                label,
                len(rows),
                definition,
                "autotask",
                period_bound=fact_id.endswith("completed"),
            )
            if rows:
                self.facts.tables[f"{fact_id}.list"] = FactTable(
                    id=f"{fact_id}.list",
                    label=label,
                    definition=definition,
                    source="autotask",
                    columns=columns,
                    rows=sorted(rows),
                    total=len(rows),
                    as_of=self.now,
                )
        self.fact(
            "autotask.projects.overdue",
            "Projects in progress past their end date",
            overdue,
            "Projects counted in autotask.projects.current whose end date has passed.",
            "autotask",
        )

    # --- derived ----------------------------------------------------------------------

    def _derive_locations(self) -> None:
        """Slide 5's location count: NinjaOne's first, Autotask's as the fallback.

        There is no user count. Licensed users come from Microsoft 365, which is not
        connected, and Autotask's active contacts are not a stand-in: the pilot site
        held 443 of them against 101 workstations. Nothing is printed until a connector
        can count users, and the contact count is not collected at all.
        """
        ninja = self.facts.get("ninjaone.locations")
        autotask = self.facts.get("autotask.locations")
        chosen = ninja if ninja and ninja.value else autotask
        if chosen is not None:
            self.fact(
                "site.locations",
                "Locations",
                chosen.value,
                f"From {chosen.id}: NinjaOne's organisation locations when it has any, "
                "otherwise the company's active locations in Autotask.",
                chosen.source,
                display=chosen.display,
            )


NUMBER_WORDS = "zero one two three four five six seven eight nine ten eleven twelve".split()


def _count_words(count: int, noun: str) -> str:
    """`the last month` reads better than `the last 1 months`; both are code-written."""
    if count == 1:
        return noun
    word = NUMBER_WORDS[count] if count < len(NUMBER_WORDS) else str(count)
    return f"{word} {noun}s"

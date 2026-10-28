"""The NinjaOne tools the model can call.

Each docstring below is what the model reads when it decides which tool to use,
so it says what the tool returns and when to reach for it.

Every tool is read-only (hard rule 2), takes `client_id` (hard rule 1), and
reports `truncated` honestly rather than implying a partial list is complete
(hard rule 7).

Every tool also takes an optional `site_id`. The model never sees it: it names a
site by slug and the loop resolves that within the current client. With a site,
the call covers that end customer alone. Without one, it covers every active
site of the client and each row carries the site it came from, so an answer
spanning customers can never read as though it came from one.

Results are compact by convention (`app/connectors/results.py`): a list tool
returns only the fields the prompt needs per row, at most its row cap, and the
true total. `count_devices` exists so "how many servers" is one small call
rather than a device dump the model then counts.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.registry import get_tool, tool
from app.connectors.factory import connector_for
from app.connectors.ninjaone.client import (
    DEVICE_ACTIVITIES_PATH,
    QUERY_PATH,
    NinjaOneClient,
)
from app.connectors.ninjaone.hardware import BASIS as WINDOWS11_BASIS
from app.connectors.ninjaone.hardware import (
    SERVER_CLASS_MARKERS,
    Eligibility,
    MachineKind,
    classify_machine,
    windows11_eligibility,
)
from app.connectors.ninjaone.models import (
    NinjaAlert,
    NinjaAntivirusStatus,
    NinjaComputerSystem,
    NinjaCustomFieldDefinition,
    NinjaDevice,
    NinjaLoggedOnUser,
    NinjaOperatingSystemRow,
    NinjaOSPatch,
    NinjaPatchInstall,
    NinjaProcessor,
    NinjaSoftware,
    NinjaSoftwarePatch,
    NinjaVolume,
    NinjaWindowsService,
)
from app.connectors.results import RowCapped, SiteTagged, capped
from app.connectors.scope import SiteRef
from app.devices.lookup import linked_ids
from app.models.enums import ConnectorName

GroupBy = Literal["site", "node_class", "os"]

#: Shorthands the model may pass for `node_class`, each matching by substring
#: of the vendor's enum; a tuple matches any of them. Anything else is matched
#: as the enum value itself.
#:
#: **"server" means what the business review means by a server**
#: (`hardware.SERVER_CLASS_MARKERS`): a virtual server is a server, so a VM
#: guest is counted here too. Asking chat "what servers does this customer
#: have" and reading the deck's server count must not give two answers.
NODE_CLASS_SHORTHANDS: dict[str, str | tuple[str, ...]] = {
    "server": SERVER_CLASS_MARKERS,
    "servers": SERVER_CLASS_MARKERS,
    "workstation": "WORKSTATION",
    "workstations": "WORKSTATION",
    "desktop": "WORKSTATION",
    "laptop": "WORKSTATION",
    "mac": "MAC",
    "macs": "MAC",
    "vm host": "VM_HOST",
    "hypervisor": "VM_HOST",
    "network": "NMS_",
    "switch": "NMS_SWITCH",
    "firewall": "NMS_FIREWALL",
}


class DeviceSummary(SiteTagged):
    """One device: only what an engineer asks about."""

    name: str
    device_class: str | None = Field(default=None, description="NinjaOne's nodeClass.")
    os: str | None = Field(
        default=None,
        description="OS family (Windows, macOS, Linux, ...) derived from the node class.",
    )
    offline: bool | None = Field(default=None, description="NinjaOne's own online/offline flag.")
    last_contact: dt.datetime | None = None
    last_contact_known: bool = True


class DeviceList(RowCapped):
    """The answer to "which machines does this customer have?"."""

    client_id: str
    sites_covered: list[str] = Field(
        description="The site slugs this answer covers. More than one means a merged answer."
    )
    filtered_by: dict[str, str] = Field(
        default_factory=dict,
        description="The filters actually applied, so a narrow answer is not mistaken for all.",
    )
    device_count: int = Field(description="Same as `total`: devices matching, before the cap.")
    devices: list[DeviceSummary]


class DeviceCounts(BaseModel):
    """The answer to "how many ...?" without listing anything."""

    client_id: str
    sites_covered: list[str]
    group_by: GroupBy
    filtered_by: dict[str, str] = Field(
        default_factory=dict, description="The node_class / os filter the counts are within."
    )
    total: int = Field(description="Devices counted across every group.")
    counts: dict[str, int] = Field(
        description=(
            "Group label -> device count, largest first. `unknown` when NinjaOne gave no value."
        )
    )
    truncated: bool = Field(
        default=False,
        description="True when the vendor page cap stopped the listing: counts are lower bounds.",
    )
    truncation_note: str | None = None


class StaleDevice(DeviceSummary):
    """A device plus how long it has been silent."""

    days_since_contact: float | None = None


class StaleDeviceList(RowCapped):
    """The answer to "what has not checked in?"."""

    client_id: str
    sites_covered: list[str]
    threshold_days: int
    checked_at: dt.datetime
    devices_checked: int
    devices_never_seen: int = Field(
        default=0,
        description="Devices NinjaOne reports with no last-contact timestamp at all.",
    )
    stale_device_count: int = Field(description="Same as `total`: stale devices, before the cap.")
    devices: list[StaleDevice]


class AlertSummary(SiteTagged):
    """One active alert."""

    device_name: str | None = None
    subject: str | None = None
    message: str | None = None
    severity: str | None = None
    priority: str | None = None
    condition: str | None = None
    created_at: dt.datetime | None = None


class AlertList(RowCapped):
    """The answer to "what is alerting right now?"."""

    client_id: str
    sites_covered: list[str]
    alert_count: int = Field(description="Same as `total`: active alerts, before the cap.")
    alerts: list[AlertSummary]


def _utcnow() -> dt.datetime:
    """The clock the tools measure against. A seam so tests can freeze it."""
    return dt.datetime.now(dt.UTC)


def _summarise(site: SiteRef, device: NinjaDevice) -> DeviceSummary:
    return DeviceSummary(
        site=site.name,
        site_slug=site.slug,
        name=device.name,
        device_class=device.node_class,
        os=device.os_family,
        offline=device.offline,
        last_contact=device.last_contact,
        last_contact_known=device.last_contact is not None,
    )


def _summarise_alert(site: SiteRef, alert: NinjaAlert) -> AlertSummary:
    return AlertSummary(
        site=site.name,
        site_slug=site.slug,
        device_name=alert.device.name if alert.device else None,
        subject=alert.subject,
        message=alert.message,
        severity=alert.severity,
        priority=alert.priority,
        condition=alert.condition_name,
        created_at=alert.create_time,
    )


def _covered(connector: NinjaOneClient) -> list[str]:
    return connector.scope.target_slugs


def _node_class_matcher(wanted: str) -> Callable[[NinjaDevice], bool]:
    """Match a node-class filter loosely: "server" means any class the review
    counts as a server, including a VM guest."""
    key = wanted.strip().lower()
    found = NODE_CLASS_SHORTHANDS.get(key, key.upper().replace(" ", "_"))
    needles = (found,) if isinstance(found, str) else found
    return lambda device: any(n in (device.node_class or "").upper() for n in needles)


def _os_matcher(wanted: str) -> Callable[[NinjaDevice], bool]:
    """Match an OS-family filter: "windows", "mac"/"macos", "linux", ..."""
    key = wanted.strip().casefold().replace(" ", "")
    aliases = {"mac": "macos", "osx": "macos", "win": "windows", "hyperv": "hyper-v"}
    key = aliases.get(key, key)
    return lambda device: (device.os_family or "").casefold().replace(" ", "") == key


def _sorted_devices(rows: list[tuple[SiteRef, NinjaDevice]]) -> list[tuple[SiteRef, NinjaDevice]]:
    """Site, then name: the order the cap keeps and the answer reads in."""
    return sorted(rows, key=lambda pair: (pair[0].name.casefold(), pair[1].name.casefold()))


@tool(ConnectorName.NINJAONE)
async def list_devices(
    client_id: str,
    node_class: str | None = None,
    os: str | None = None,
    site_id: str | None = None,
) -> DeviceList:
    """List the devices NinjaOne monitors, optionally filtered by class or OS.

    Use this for "which machines", "is there a device called X", or "list the
    servers at Acme". For "how many" questions prefer `count_devices`. Pass
    `site` to ask about one end customer; leave it out to cover every customer
    of this MSP at once, in which case each row names its customer.

    `node_class` narrows to a kind of device: "server", "workstation", "mac",
    "vm host", "network", or an exact NinjaOne class such as WINDOWS_SERVER.
    `os` narrows to an OS family: "windows", "macos", "linux", "vmware".

    Each row gives the site, name, class, OS family, NinjaOne's offline flag
    and the last contact time. Rows are capped: `total` is the real count and
    `truncated` is true when not every row is included — say so if it is.

    Not for counting — "how many servers" is `count_devices`, which returns numbers
    only. Not for one machine's details (`get_device`), hardware models and serials
    (`get_hardware`), or what has gone quiet (`list_devices_not_checked_in`).
    """
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        rows, page_truncated = await connector.list_devices()

        applied: dict[str, str] = {}
        if node_class:
            matches_class = _node_class_matcher(node_class)
            rows = [(site, device) for site, device in rows if matches_class(device)]
            applied["node_class"] = node_class
        if os:
            matches_os = _os_matcher(os)
            rows = [(site, device) for site, device in rows if matches_os(device)]
            applied["os"] = os

        summaries = [_summarise(site, device) for site, device in _sorted_devices(rows)]
        kept, counting = capped(
            "list_devices", summaries, noun="devices", page_truncated=page_truncated
        )
        return DeviceList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            filtered_by=applied,
            device_count=counting["total"],
            devices=kept,
            **counting,
        )


@tool(ConnectorName.NINJAONE)
async def count_devices(
    client_id: str,
    group_by: GroupBy = "site",
    node_class: str | None = None,
    os: str | None = None,
    site_id: str | None = None,
) -> DeviceCounts:
    """Count NinjaOne devices, grouped by site, node class or OS family.

    Use this for any "how many" question — "how many servers does Acme have",
    "how many Macs across all customers", "which customer has the most
    devices" — instead of listing devices and counting them. It returns only
    numbers, so it is always small.

    `group_by` is "site" (devices per end customer), "node_class" (WINDOWS_SERVER,
    WINDOWS_WORKSTATION, MAC, ...) or "os" (Windows, macOS, Linux, ...).
    `node_class` and `os` narrow what is counted first, with the same values
    `list_devices` takes ("server", "workstation", "mac", "windows", ...), so
    one call answers a two-way question: servers per customer is
    `group_by="site", node_class="server"`. Pass `site` to count within one
    end customer. `total` is the overall figure. If `truncated` is true the
    vendor listing was cut short and every number is a lower bound — say so.

    Not for naming the machines — this returns numbers only; use `list_devices` with
    the same filter to see which they are. Not for tickets (`count_tickets`).
    """
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        rows, page_truncated = await connector.list_devices()

        applied: dict[str, str] = {}
        if node_class:
            matches_class = _node_class_matcher(node_class)
            rows = [(site, device) for site, device in rows if matches_class(device)]
            applied["node_class"] = node_class
        if os:
            matches_os = _os_matcher(os)
            rows = [(site, device) for site, device in rows if matches_os(device)]
            applied["os"] = os

        def label(site: SiteRef, device: NinjaDevice) -> str:
            if group_by == "site":
                return site.name
            if group_by == "node_class":
                return device.node_class or "unknown"
            return device.os_family or "unknown"

        tally = Counter(label(site, device) for site, device in rows)
        return DeviceCounts(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            group_by=group_by,
            filtered_by=applied,
            total=sum(tally.values()),
            counts=dict(sorted(tally.items(), key=lambda item: (-item[1], item[0]))),
            truncated=page_truncated,
            truncation_note=(
                "The vendor listing stopped at the page cap, so every count is a lower bound."
                if page_truncated
                else None
            ),
        )


@tool(ConnectorName.NINJAONE)
async def list_devices_not_checked_in(
    client_id: str, days: int = 7, site_id: str | None = None
) -> StaleDeviceList:
    """List devices that have not contacted NinjaOne for at least `days` days.

    Use this for "what is offline", "what has gone quiet", or "what has not
    checked in this week". `days` defaults to 7. Pass `site` to ask about one
    end customer; leave it out to cover all of them, in which case each device
    says which customer it belongs to.

    Devices NinjaOne has no last-contact timestamp for are counted in
    `devices_never_seen` and are not reported as stale, because their silence
    cannot be measured. Rows are capped, longest-silent first: `total` is the
    real count of stale devices and `truncated` says whether all are listed.

    Not for what is alerting now (`list_open_alerts`), and not a general device list
    (`list_devices`). NinjaOne's own `offline` flag on `list_devices` is a different
    signal: this tool measures silence in days.
    """
    if days < 1:
        raise ValueError("days must be at least 1")

    checked_at = _utcnow()
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        rows, page_truncated = await connector.list_devices()

        stale: list[StaleDevice] = []
        never_seen = 0
        for site, device in rows:
            elapsed = device.days_since_contact(now=checked_at)
            if elapsed is None:
                never_seen += 1
                continue
            if elapsed >= days:
                stale.append(
                    StaleDevice(
                        **_summarise(site, device).model_dump(),
                        days_since_contact=round(elapsed, 2),
                    )
                )
        stale.sort(key=lambda item: item.days_since_contact or 0.0, reverse=True)
        kept, counting = capped(
            "list_devices_not_checked_in",
            stale,
            noun="stale devices",
            page_truncated=page_truncated,
        )

        return StaleDeviceList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            threshold_days=days,
            checked_at=checked_at,
            devices_checked=len(rows),
            devices_never_seen=never_seen,
            stale_device_count=counting["total"],
            devices=kept,
            **counting,
        )


@tool(ConnectorName.NINJAONE)
async def list_open_alerts(client_id: str, site_id: str | None = None) -> AlertList:
    """List the alerts currently firing in NinjaOne.

    Use this for "what is alerting", "what is broken right now", or to check
    whether a device an engineer is working on has an open condition. Pass
    `site` for one end customer, or leave it out to see every customer's alerts
    with the customer named against each.

    Returns the alert message, severity, priority and the device it belongs to,
    newest first, capped at the row limit with `total` the real count. An empty
    list means NinjaOne reports no active alerts, not that the check failed.

    Not for history — a cleared alert is gone from here; what happened on a machine
    over time is `list_device_activities`. Not for tickets raised from alerts
    (`list_open_tickets`), nor for service state (`list_windows_services`).
    """
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        rows = await connector.list_alerts()
        summaries = [_summarise_alert(site, alert) for site, alert in rows]
        summaries.sort(
            key=lambda item: item.created_at or dt.datetime.min.replace(tzinfo=dt.UTC),
            reverse=True,
        )
        kept, counting = capped("list_open_alerts", summaries, noun="alerts")
        return AlertList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            alert_count=counting["total"],
            alerts=kept,
            **counting,
        )


# --- one device in detail ---------------------------------------------------


class VolumeSummary(BaseModel):
    volume: str
    capacity_gb: float | None = None
    free_gb: float | None = None
    free_percent: float | None = None


class DeviceDetail(BaseModel):
    """The answer to "tell me about PC-014"."""

    client_id: str
    sites_covered: list[str]
    found: bool
    candidates: list[str] = Field(
        default_factory=list,
        description="When the name matched several devices, their names with sites. Ask which.",
    )
    message: str | None = None
    site: str | None = None
    site_slug: str | None = None
    name: str | None = None
    node_class: str | None = None
    offline: bool | None = None
    last_contact: dt.datetime | None = None
    approval_status: str | None = None
    os_name: str | None = None
    os_build: str | None = None
    os_release: str | None = None
    architecture: str | None = None
    last_boot: dt.datetime | None = None
    needs_reboot: bool | None = None
    last_logged_in_user: str | None = None
    ip_addresses: list[str] = Field(default_factory=list)
    public_ip: str | None = None
    mac_addresses: list[str] = Field(default_factory=list)
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    domain: str | None = None
    processors: int | None = None
    memory_gb: float | None = None
    virtual_machine: bool | None = None
    volumes: list[VolumeSummary] = Field(default_factory=list)
    agent_version: None = Field(
        default=None,
        description="NinjaOne's API does not report the agent version on any endpoint we read.",
    )


def _gb(value: int | None) -> float | None:
    return None if value is None else round(value / 1_000_000_000, 1)


async def _resolve_device(
    connector: NinjaOneClient, device_name: str
) -> tuple[SiteRef, NinjaDevice] | list[str]:
    """One device by name, or the list of names that matched (or none).

    With the identity layer on, the name is also resolved through `devices`
    and their NinjaOne links, so a machine an engineer knows by the name
    another tool shows is still found. The exact NinjaOne name wins as before.
    """
    matches = await connector.find_devices(device_name)
    linked = await linked_ids(
        connector.scope.client_id,
        ConnectorName.NINJAONE,
        device_name,
        connector.scope.site_ids[0] if connector.scope.is_single_site else None,
    )
    if linked:
        rows, _ = await connector.list_devices()
        known = {str(device.id) for _, device in matches}
        matches += [(site, device) for site, device in rows if str(device.id) in linked - known]
    if len(matches) == 1:
        return matches[0]
    return [f"{device.name} ({site.slug})" for site, device in matches]


def _not_one_device(
    client_id: str, connector: NinjaOneClient, device_name: str, candidates: list[str]
) -> tuple[list[str], str]:
    if not candidates:
        message = (
            f"NinjaOne has no device named like {device_name!r} for "
            f"{connector.scope.describe_targets()} of this MSP. Check the spelling, or ask for "
            "the device list."
        )
    else:
        message = (
            f"{len(candidates)} devices match {device_name!r}. Ask again with the exact name "
            "or the site."
        )
    return candidates, message


@tool(ConnectorName.NINJAONE)
async def get_device(client_id: str, device_name: str, site_id: str | None = None) -> DeviceDetail:
    """Get one device in detail: OS and build, last boot, last user, IPs, hardware, disks.

    Use this when an engineer names a machine — "what is PC-014", "what OS is
    the finance server on", "who was last on Sarah's laptop", "what's the
    serial number". The name is matched exactly first, then as a substring;
    if several match, `candidates` lists them and `found` is false. Pass
    `site` when the same name could exist at more than one end customer.

    NinjaOne reports no agent version through its API, so that field is
    always empty — say so rather than guessing.

    Not for finding machines — it needs a name; use `list_devices` or
    `list_logged_on_users` to find one first. Not for the whole fleet's hardware
    (`get_hardware`) or for what happened on the machine (`list_device_activities`).
    """
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        resolved = await _resolve_device(connector, device_name)
        if isinstance(resolved, list):
            candidates, message = _not_one_device(client_id, connector, device_name, resolved)
            return DeviceDetail(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                found=False,
                candidates=candidates,
                message=message,
            )
        site, device = resolved
        detail = await connector.get_device(site, device.id)
        os_block = detail.os
        system = detail.system
        return DeviceDetail(
            client_id=str(client_id),
            sites_covered=[site.slug],
            found=True,
            site=site.name,
            site_slug=site.slug,
            name=detail.name,
            node_class=detail.node_class,
            offline=detail.offline,
            last_contact=detail.last_contact,
            approval_status=detail.approval_status,
            os_name=os_block.name if os_block else None,
            os_build=os_block.build_number if os_block else None,
            os_release=os_block.release_id if os_block else None,
            architecture=os_block.architecture if os_block else None,
            last_boot=os_block.last_boot_time if os_block else None,
            needs_reboot=os_block.needs_reboot if os_block else None,
            last_logged_in_user=detail.last_logged_in_user,
            ip_addresses=detail.ip_addresses,
            public_ip=detail.public_ip,
            mac_addresses=detail.mac_addresses,
            manufacturer=system.manufacturer if system else None,
            model=system.model if system else None,
            serial_number=(system.serial_number or system.bios_serial_number) if system else None,
            domain=system.domain if system else None,
            processors=system.number_of_processors if system else None,
            memory_gb=_gb(system.total_physical_memory) if system else None,
            virtual_machine=system.virtual_machine if system else None,
            volumes=[
                VolumeSummary(
                    volume=volume.drive_letter or volume.name or "?",
                    capacity_gb=_gb(volume.capacity),
                    free_gb=_gb(volume.free_space),
                    free_percent=volume.free_percent,
                )
                for volume in detail.volumes
            ],
        )


# --- patching ---------------------------------------------------------------


def _vendor_value[V](argument: str, value: object, accepted: Mapping[str, V]) -> V:
    """Map a tool argument to the value the vendor is sent, or refuse it.

    Nothing reaches NinjaOne that is not in `accepted`. The two patch reports
    treat a filter value they do not know in opposite ways — the waiting list
    returns **no rows**, which reads as "fully patched"; the install history
    **ignores the filter** and returns everything, which reads as whatever was
    asked for (api-notes). Either way a typo becomes a confident wrong answer,
    so an unknown value is an error here, naming what is accepted.
    """
    key = str(value).strip().lower()
    if key not in accepted:
        raise ValueError(
            f"{argument} must be one of {', '.join(accepted)}; got {value!r}. "
            "Nothing was sent to NinjaOne."
        )
    return accepted[key]


PatchState = Literal["pending", "rejected", "all"]
PatchSeverity = Literal["critical", "important", "optional"]
PatchCategory = Literal["security", "rollup", "feature", "driver", "definition", "regular"]

#: What each `status` sends. The waiting-list reports take one status per
#: request (live: `APPROVED,MANUAL` and a repeated parameter both return no
#: rows), so "pending" is two requests. `None` sends no status filter.
PATCH_STATE_PARAMS: dict[str, tuple[str | None, ...]] = {
    "pending": ("APPROVED", "MANUAL"),
    "rejected": ("REJECTED",),
    "all": (None,),
}
#: Sent exactly as written: on this report the values are case-sensitive
#: (`severity=critical` returned no rows where `CRITICAL` returned nineteen).
PATCH_SEVERITY_PARAM: dict[str, str] = {
    "critical": "CRITICAL",
    "important": "IMPORTANT",
    "optional": "OPTIONAL",
}
PATCH_CATEGORY_PARAM: dict[str, str] = {
    "security": "SECURITY_UPDATES",
    "rollup": "UPDATE_ROLLUPS",
    "feature": "FEATURE_UPDATES",
    "driver": "DRIVER_UPDATES",
    "definition": "DEFINITION_UPDATES",
    "regular": "REGULAR_UPDATES",
}
SEVERITY_RANK = {"CRITICAL": 0, "IMPORTANT": 1, "OPTIONAL": 2}
MAX_WAITING_TALLIES = 25
MAX_SITE_COUNTS = 50

_KB_DIGITS = re.compile(r"^(?:KB)?(\d{4,8})$", re.IGNORECASE)


def _normalise_kb(kb: str) -> str:
    match = _KB_DIGITS.match(kb.strip().replace(" ", ""))
    if match is None:
        raise ValueError(f"kb must look like KB5060000 or 5060000; got {kb!r}")
    return f"KB{match.group(1)}"


class PatchRow(SiteTagged):
    device: str
    kind: Literal["os", "software"]
    patch: str
    kb: str | None = None
    severity: str | None = Field(default=None, description="OS severity, or software impact.")
    status: str | None = None
    type: str | None = None


class WaitingPatch(BaseModel):
    """One patch across the fleet: how many machines are still waiting for it."""

    patch: str
    kb: str | None = None
    kind: Literal["os", "software"]
    severity: str | None = None
    devices: int = Field(description="Distinct machines the patch is waiting on.")
    sites: int = Field(description="Distinct end customers those machines belong to.")


class SitePatchCount(BaseModel):
    site: str
    site_slug: str
    patches: int = Field(description="Waiting patch rows at this site.")
    devices: int = Field(description="Distinct machines with at least one.")


class PatchStatusList(RowCapped):
    """The answer to "what is waiting to be patched?"."""

    client_id: str
    sites_covered: list[str]
    status: PatchState
    filtered_by: dict[str, str] = Field(default_factory=dict)
    kinds_covered: list[str] = Field(
        description="`os`, `software` or both. Severity, category and KB exist on OS patches only."
    )
    coverage_note: str | None = None
    status_counts: dict[str, int] = Field(
        description="Rows per vendor status across everything matched, before the row cap."
    )
    severity_counts: dict[str, int] = Field(
        description="Rows per severity (OS) or impact (software), before the row cap."
    )
    devices_affected: int = Field(description="Distinct machines across everything matched.")
    patches: list[WaitingPatch] = Field(
        description=(
            "Per-patch totals across everything matched, before the row cap: most severe "
            f"first, then most machines. At most {MAX_WAITING_TALLIES}; `patches_total` is "
            "how many distinct patches there were."
        )
    )
    patches_total: int
    site_counts: list[SitePatchCount] = Field(
        description=f"Per end customer, most rows first, before the row cap. At most "
        f"{MAX_SITE_COUNTS}."
    )
    rows: list[PatchRow]


@tool(ConnectorName.NINJAONE)
async def list_patch_status(
    client_id: str,
    status: PatchState = "pending",
    severity: PatchSeverity | None = None,
    category: PatchCategory | None = None,
    kb: str | None = None,
    site_id: str | None = None,
) -> PatchStatusList:
    """Patches waiting to be installed: what is outstanding, how severe, and where.

    Use this for the waiting list — "any critical patches outstanding", "what
    is our security patch exposure", "is KB5060000 still missing anywhere",
    "what is outstanding at Acme", "what is waiting for approval".

    `status` is "pending" (the default: approved and not yet installed, plus
    anything awaiting approval), "rejected" (patches someone declined — only
    when asked) or "all". Narrow it, because the unfiltered list runs to
    thousands:

    - `severity`: "critical", "important" or "optional";
    - `category`: "security", "rollup", "feature", "driver", "definition" or
      "regular" — most of an unfiltered list is drivers;
    - `kb`: one KB number, to see which machines still lack it;
    - `site`: one end customer.

    Severity, category and KB exist on Windows patches only, so passing any of
    them leaves third-party software patches out; `kinds_covered` says what
    was read. Any other value for these arguments is refused, not sent.

    Read the totals first — they cover everything matched, before the row
    cap: `patches` is per patch with the machines and customers still waiting
    ("KB5060000: 143 devices across 12 customers"), `severity_counts`,
    `site_counts`, `devices_affected`. `rows` is the capped detail, most
    severe first, for naming machines.

    Not for history. This report holds no failures and no installed patches:
    an empty or all-pending answer says nothing about whether patch night went
    well. What failed, what went on and whether a KB installed are
    `list_patch_installs`. Not for reboots (`list_pending_reboot`).
    """
    statuses = _vendor_value("status", status, PATCH_STATE_PARAMS)
    base: dict[str, str] = {}
    applied: dict[str, str] = {}
    if severity is not None:
        base["severity"] = _vendor_value("severity", severity, PATCH_SEVERITY_PARAM)
        applied["severity"] = str(severity)
    if category is not None:
        base["type"] = _vendor_value("category", category, PATCH_CATEGORY_PARAM)
        applied["category"] = str(category)
    wanted_kb = _normalise_kb(kb) if kb else None
    if wanted_kb:
        applied["kb"] = wanted_kb
    os_only = bool(base) or wanted_kb is not None
    reports = ("os-patches",) if os_only else ("os-patches", "software-patches")

    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        raw: dict[str, list[dict[str, Any]]] = {report: [] for report in reports}
        page_truncated = False
        for report in reports:
            for vendor_status in statuses:
                params = dict(base)
                if vendor_status is not None:
                    params["status"] = vendor_status
                batch, truncated, _ = await connector.query_scoped(report, params=params or None)
                raw[report].extend(batch)
                page_truncated = page_truncated or truncated
            # "pending" is one request per status. A vendor that ignored the status
            # filter would hand the same rows back each time; count each once.
            unique: dict[tuple[Any, Any], dict[str, Any]] = {}
            anonymous: list[dict[str, Any]] = []
            for item in raw[report]:
                if item.get("id") is None:
                    anonymous.append(item)
                else:
                    unique.setdefault((item.get("id"), item.get("deviceId")), item)
            raw[report] = [*unique.values(), *anonymous]

        os_rows = [NinjaOSPatch.model_validate(item) for item in raw["os-patches"]]
        # Every filter is applied again here. The vendor is trusted to narrow the
        # read, never to be the only thing standing between a row and the answer.
        sent = {value for value in statuses if value is not None}
        if sent:
            os_rows = [row for row in os_rows if (row.status or "").upper() in sent]
        if "severity" in base:
            os_rows = [row for row in os_rows if (row.severity or "").upper() == base["severity"]]
        if "type" in base:
            os_rows = [row for row in os_rows if (row.type or "").upper() == base["type"]]
        if wanted_kb:
            os_rows = [
                row
                for row in os_rows
                if (row.kb_number or "").upper() == wanted_kb
                or wanted_kb in (row.name or "").upper()
            ]
        sw_rows = [
            NinjaSoftwarePatch.model_validate(item) for item in raw.get("software-patches", [])
        ]
        if sent:
            sw_rows = [row for row in sw_rows if (row.status or "").upper() in sent]

        placed_os = await connector.place_by_device(
            os_rows,
            [row.device_id for row in os_rows],
            endpoint=QUERY_PATH.format(report="os-patches"),
        )
        placed_sw = (
            await connector.place_by_device(
                sw_rows,
                [row.device_id for row in sw_rows],
                endpoint=QUERY_PATH.format(report="software-patches"),
            )
            if sw_rows
            else []
        )

        rows: list[PatchRow] = []
        device_of: list[int] = []
        for site, device, patch in placed_os:
            rows.append(
                PatchRow(
                    site=site.name,
                    site_slug=site.slug,
                    device=device.name,
                    kind="os",
                    patch=patch.name or "(unnamed)",
                    kb=patch.kb_number or None,
                    severity=patch.severity,
                    status=patch.status,
                    type=patch.type,
                )
            )
            device_of.append(device.id)
        for site, device, software in placed_sw:
            rows.append(
                PatchRow(
                    site=site.name,
                    site_slug=site.slug,
                    device=device.name,
                    kind="software",
                    patch=software.title or "(untitled)",
                    severity=software.impact,
                    status=software.status,
                    type=software.type,
                )
            )
            device_of.append(device.id)

        # Totals over everything matched, before the cap takes its share.
        waiting: dict[tuple[str, str, str | None], WaitingPatch] = {}
        waiting_devices: dict[tuple[str, str, str | None], set[int]] = {}
        waiting_sites: dict[tuple[str, str, str | None], set[str]] = {}
        per_site: dict[str, SitePatchCount] = {}
        per_site_devices: dict[str, set[int]] = {}
        for row, device_id in zip(rows, device_of, strict=True):
            key = (row.kind, row.patch, row.kb)
            if key not in waiting:
                waiting[key] = WaitingPatch(
                    patch=row.patch,
                    kb=row.kb,
                    kind=row.kind,
                    severity=row.severity,
                    devices=0,
                    sites=0,
                )
            waiting_devices.setdefault(key, set()).add(device_id)
            waiting_sites.setdefault(key, set()).add(row.site_slug)
            count = per_site.setdefault(
                row.site_slug,
                SitePatchCount(site=row.site, site_slug=row.site_slug, patches=0, devices=0),
            )
            count.patches += 1
            per_site_devices.setdefault(row.site_slug, set()).add(device_id)
        for patch_key, tally in waiting.items():
            tally.devices = len(waiting_devices[patch_key])
            tally.sites = len(waiting_sites[patch_key])
        for slug, count in per_site.items():
            count.devices = len(per_site_devices[slug])

        def severity_rank(value: str | None) -> int:
            return SEVERITY_RANK.get((value or "").upper(), 9)

        ranked = sorted(
            waiting.values(),
            key=lambda item: (severity_rank(item.severity), -item.devices, item.patch),
        )
        sites_ranked = sorted(per_site.values(), key=lambda item: (-item.patches, item.site))

        # Awaiting approval first (someone has to act), then most severe, then by
        # site and device, so a capped list is the urgent part. A FAILED row is
        # never asked for and the pilot tenant never sent one, but a row the
        # vendor does send is shown first, not hidden.
        state_rank = {"FAILED": 0, "MANUAL": 1, "APPROVED": 2, "REJECTED": 3}
        rows.sort(
            key=lambda r: (
                state_rank.get((r.status or "").upper(), 9),
                severity_rank(r.severity),
                r.site.casefold(),
                r.device.casefold(),
            )
        )
        kept, counting = capped(
            "list_patch_status", rows, noun="patches", page_truncated=page_truncated
        )
        return PatchStatusList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            status=status,
            filtered_by=applied,
            kinds_covered=["os"] if os_only else ["os", "software"],
            coverage_note=(
                "Third-party software patches were not read: severity, category and KB "
                "exist on Windows patches only."
                if os_only
                else None
            ),
            status_counts=dict(Counter(row.status or "unknown" for row in rows).most_common()),
            severity_counts=dict(
                Counter((row.severity or "unknown").upper() for row in rows).most_common()
            ),
            devices_affected=len(set(device_of)),
            patches=ranked[:MAX_WAITING_TALLIES],
            patches_total=len(ranked),
            site_counts=sites_ranked[:MAX_SITE_COUNTS],
            rows=kept,
            **counting,
        )


# --- reboots ----------------------------------------------------------------


class RebootRow(SiteTagged):
    device: str
    os_name: str | None = None
    last_boot: dt.datetime | None = None
    days_since_boot: float | None = None


class PendingRebootList(RowCapped):
    """The answer to "what still needs a reboot?"."""

    client_id: str
    sites_covered: list[str]
    devices_checked: int
    rows: list[RebootRow]


@tool(ConnectorName.NINJAONE)
async def list_pending_reboot(client_id: str, site_id: str | None = None) -> PendingRebootList:
    """List devices NinjaOne says need a reboot, with when they last booted.

    Use this after patch night or when "the update says restart" — "what still
    needs a reboot", "has the server been rebooted since patching". Reads the
    operating-systems report (`needsReboot`), tenant-wide when no site is
    named. Longest since boot first. `devices_checked` is how many devices
    reported an OS at all, so "none" is meaningful.

    Not for which patches are waiting (`list_patch_status`) or which installs failed
    (`list_patch_installs`). A reboot flag says nothing about why.
    """
    checked_at = _utcnow()
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        raw, page_truncated, _ = await connector.query_scoped("operating-systems")
        rows = [NinjaOperatingSystemRow.model_validate(item) for item in raw]
        placed = await connector.place_by_device(
            rows,
            [row.device_id for row in rows],
            endpoint=QUERY_PATH.format(report="operating-systems"),
        )
        pending = [
            RebootRow(
                site=site.name,
                site_slug=site.slug,
                device=device.name,
                os_name=os_row.name,
                last_boot=os_row.last_boot_time,
                days_since_boot=(
                    round((checked_at - os_row.last_boot_time).total_seconds() / 86_400, 1)
                    if os_row.last_boot_time
                    else None
                ),
            )
            for site, device, os_row in placed
            if os_row.needs_reboot
        ]
        pending.sort(key=lambda r: r.days_since_boot or 0.0, reverse=True)
        kept, counting = capped(
            "list_pending_reboot", pending, noun="devices", page_truncated=page_truncated
        )
        return PendingRebootList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            devices_checked=len(placed),
            rows=kept,
            **counting,
        )


# --- disk space -------------------------------------------------------------


class DiskRow(SiteTagged):
    device: str
    volume: str
    capacity_gb: float | None = None
    free_gb: float | None = None
    free_percent: float | None = None


class DiskSpaceList(RowCapped):
    """The answer to "what is running out of disk?"."""

    client_id: str
    sites_covered: list[str]
    below_percent: float
    volumes_checked: int
    rows: list[DiskRow]


@tool(ConnectorName.NINJAONE)
async def list_disk_space(
    client_id: str, below_percent: float = 10, site_id: str | None = None
) -> DiskSpaceList:
    """List local disk volumes with less than `below_percent` free.

    Use this for "what is running out of disk", "is C: full on the finance
    server", or before promising a user their machine is fine. Only fixed
    local disks with a reported capacity count; removable media and
    zero-capacity partitions are skipped. Fullest first. Raise
    `below_percent` (say 25) to see what is heading that way.

    Not for one machine's full disk layout, which `get_device` gives with everything
    else about it. Not for disk alerts NinjaOne has raised (`list_open_alerts`).
    """
    if not 0 < below_percent <= 100:
        raise ValueError("below_percent must be between 0 and 100")
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        raw, page_truncated, _ = await connector.query_scoped("volumes")
        volumes = [NinjaVolume.model_validate(item) for item in raw]
        placed = await connector.place_by_device(
            volumes, [v.device_id for v in volumes], endpoint=QUERY_PATH.format(report="volumes")
        )
        fixed = [
            (site, device, volume)
            for site, device, volume in placed
            if volume.capacity and (volume.device_type or "").casefold() == "local disk"
        ]
        low = [
            DiskRow(
                site=site.name,
                site_slug=site.slug,
                device=device.name,
                volume=volume.drive_letter or volume.name or "?",
                capacity_gb=_gb(volume.capacity),
                free_gb=_gb(volume.free_space),
                free_percent=volume.free_percent,
            )
            for site, device, volume in fixed
            if volume.free_percent is not None and volume.free_percent < below_percent
        ]
        low.sort(key=lambda r: r.free_percent or 0.0)
        kept, counting = capped(
            "list_disk_space", low, noun="volumes", page_truncated=page_truncated
        )
        return DiskSpaceList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            below_percent=below_percent,
            volumes_checked=len(fixed),
            rows=kept,
            **counting,
        )


# --- antivirus --------------------------------------------------------------


class AntivirusRow(SiteTagged):
    device: str
    product: str | None = None
    state: str | None = Field(default=None, description="ON or OFF, as the agent sees it.")
    definitions: str | None = Field(default=None, description="Up-to-Date or Out-of-Date.")
    version: str | None = None
    healthy: bool


class AntivirusStatusList(RowCapped):
    """The answer to "is antivirus on and current everywhere?"."""

    client_id: str
    sites_covered: list[str]
    unhealthy_only: bool
    devices_checked: int
    product_counts: dict[str, int] = Field(description="Rows per product, before any filter.")
    rows: list[AntivirusRow]


@tool(ConnectorName.NINJAONE)
async def list_antivirus_status(
    client_id: str, unhealthy_only: bool = True, site_id: str | None = None
) -> AntivirusStatusList:
    """List antivirus state per device as the NinjaOne agent sees it.

    Use this for "is AV running on that machine", "which devices have
    out-of-date definitions", "who is still on Defender rather than
    Bitdefender". A row is unhealthy when the product is OFF or its
    definitions are Out-of-Date. A device often carries two rows — Defender
    OFF beside a third-party product ON is normal. `unhealthy_only` is on by
    default; turn it off for the full picture. `product_counts` covers every
    row before the filter.

    Not for threats or detections: this is product state only, as the NinjaOne agent
    sees it, and no tool reads a security console yet. Not for whether the product
    is installed at all by name — `list_software` answers that.
    """
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        raw, page_truncated, _ = await connector.query_scoped("antivirus-status")
        statuses = [NinjaAntivirusStatus.model_validate(item) for item in raw]
        placed = await connector.place_by_device(
            statuses,
            [s.device_id for s in statuses],
            endpoint=QUERY_PATH.format(report="antivirus-status"),
        )
        product_counts = Counter(status.product_name or "unknown" for _, _, status in placed)
        rows = [
            AntivirusRow(
                site=site.name,
                site_slug=site.slug,
                device=device.name,
                product=status.product_name,
                state=status.product_state,
                definitions=status.definition_status,
                version=status.version,
                healthy=status.healthy,
            )
            for site, device, status in placed
            if not unhealthy_only or not status.healthy
        ]
        rows.sort(key=lambda r: (r.healthy, r.site.casefold(), r.device.casefold()))
        kept, counting = capped(
            "list_antivirus_status", rows, noun="antivirus rows", page_truncated=page_truncated
        )
        return AntivirusStatusList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            unhealthy_only=unhealthy_only,
            devices_checked=len({device.id for _, device, _ in placed}),
            product_counts=dict(product_counts.most_common()),
            rows=kept,
            **counting,
        )


# --- software ---------------------------------------------------------------


class SoftwareRow(SiteTagged):
    device: str
    name: str
    version: str | None = None
    publisher: str | None = None
    install_date: str | None = None


class SoftwareList(RowCapped):
    """The answer to "who has application X installed?"."""

    client_id: str
    sites_covered: list[str]
    name_searched: str
    rows_scanned: int = Field(description="Software rows read from NinjaOne before matching.")
    rows: list[SoftwareRow]


@tool(ConnectorName.NINJAONE)
async def list_software(client_id: str, name: str, site_id: str | None = None) -> SoftwareList:
    """List the devices that have an application installed, by (partial) name.

    Use this for "who has Chrome", "which machines still run TeamViewer",
    "is 7-Zip on the finance server". The match is a case-insensitive
    substring of the installed name, so "chrome" finds "Google Chrome".

    NinjaOne has no server-side name filter for this report, so an unscoped
    search reads the whole tenant's software inventory — well over a hundred
    thousand rows on a mid-sized franchisee — and the page cap may stop it
    early; `truncated` says so, and `rows_scanned` says how far it got. Pass
    `site` whenever you can: the report is then narrowed to that customer.

    Not for Windows services (`list_windows_services`) or patches
    (`list_patch_status`). Not for operating system versions — `get_device` for one
    machine, `count_devices` grouped by `os` for the fleet.
    """
    wanted = name.strip().casefold()
    if not wanted:
        raise ValueError("name must not be empty")
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        raw, page_truncated, _ = await connector.query_scoped("software")
        matching = [
            NinjaSoftware.model_validate(item)
            for item in raw
            if wanted in str(item.get("name") or "").casefold()
        ]
        placed = await connector.place_by_device(
            matching, [s.device_id for s in matching], endpoint=QUERY_PATH.format(report="software")
        )
        rows = [
            SoftwareRow(
                site=site.name,
                site_slug=site.slug,
                device=device.name,
                name=software.name or name,
                version=software.version,
                publisher=software.publisher,
                install_date=software.install_date,
            )
            for site, device, software in placed
        ]
        rows.sort(key=lambda r: (r.site.casefold(), r.device.casefold(), r.name.casefold()))
        kept, counting = capped(
            "list_software", rows, noun="installations", page_truncated=page_truncated
        )
        return SoftwareList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            name_searched=name,
            rows_scanned=len(raw),
            rows=kept,
            **counting,
        )


# --- logged-on users --------------------------------------------------------


class LoggedOnUserRow(SiteTagged):
    device: str
    user_name: str | None = None
    logon_time: dt.datetime | None = None


class LoggedOnUserList(RowCapped):
    """The answer to "which machine is Sarah on?"."""

    client_id: str
    sites_covered: list[str]
    user_searched: str | None = None
    rows: list[LoggedOnUserRow]


@tool(ConnectorName.NINJAONE)
async def list_logged_on_users(
    client_id: str, user: str | None = None, site_id: str | None = None
) -> LoggedOnUserList:
    """List the last logged-on user per device, most recent logon first.

    Use this for "which machine is Sarah on", "who uses PC-014", or to find
    a user's device before looking at it. `user` narrows to names containing
    that text (case-insensitive, so "sarah" matches "ACME\\\\sarah.chen").
    This is the *last* logon NinjaOne recorded per device, not a live session
    list: a user who logged on yesterday still shows.

    Not for who a person is or how to reach them — that is the PSA's
    `list_contacts`. Not live sessions: it is the last logon recorded per device.
    """
    wanted = (user or "").strip().casefold()
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        raw, page_truncated, _ = await connector.query_scoped("logged-on-users")
        logons = [NinjaLoggedOnUser.model_validate(item) for item in raw]
        placed = await connector.place_by_device(
            logons,
            [logon.device_id for logon in logons],
            endpoint=QUERY_PATH.format(report="logged-on-users"),
        )
        rows = [
            LoggedOnUserRow(
                site=site.name,
                site_slug=site.slug,
                device=device.name,
                user_name=logon.user_name,
                logon_time=logon.logon_time,
            )
            for site, device, logon in placed
            if not wanted or wanted in (logon.user_name or "").casefold()
        ]
        rows.sort(
            key=lambda r: r.logon_time or dt.datetime.min.replace(tzinfo=dt.UTC), reverse=True
        )
        kept, counting = capped(
            "list_logged_on_users", rows, noun="logons", page_truncated=page_truncated
        )
        return LoggedOnUserList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            user_searched=user,
            rows=kept,
            **counting,
        )


# --- device activity --------------------------------------------------------


class ActivityRow(BaseModel):
    time: dt.datetime | None = None
    activity_type: str | None = None
    status: str | None = None
    message: str | None = None


class DeviceActivityList(RowCapped):
    """The answer to "what happened on that machine this week?"."""

    client_id: str
    sites_covered: list[str]
    found: bool
    candidates: list[str] = Field(default_factory=list)
    message: str | None = None
    device: str | None = None
    site: str | None = None
    site_slug: str | None = None
    days: int
    since: dt.datetime
    rows: list[ActivityRow]


@tool(ConnectorName.NINJAONE)
async def list_device_activities(
    client_id: str, device_name: str, days: int = 7, site_id: str | None = None
) -> DeviceActivityList:
    """The NinjaOne activity log for one device over the last `days` days.

    Use this to answer "what happened on PC-014", "did anyone remote onto the
    server last night", "when was Chrome updated on that machine". Newest
    first: remote sessions, scripts and actions, software changes, monitor
    conditions raised and cleared. The device is matched by name as in
    `get_device`. One page is returned; `truncated` means there was more.

    Not for the current state of the machine (`get_device`) or alerts open right now
    (`list_open_alerts`). Not for ticket history on the device, which is the PSA's
    `list_tickets_for_device`.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    since = _utcnow() - dt.timedelta(days=days)
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        resolved = await _resolve_device(connector, device_name)
        if isinstance(resolved, list):
            candidates, message = _not_one_device(client_id, connector, device_name, resolved)
            return DeviceActivityList(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                found=False,
                candidates=candidates,
                message=message,
                days=days,
                since=since,
                total=0,
                returned=0,
                rows=[],
            )
        site, device = resolved
        page_size = get_tool("list_device_activities").row_cap
        activities, more = await connector.device_activities(
            device.id, since=since, page_size=page_size
        )
        # The endpoint is device-scoped by path; the row's own deviceId is checked
        # anyway, so a stray row could never be shown against the wrong machine.
        rows = [
            ActivityRow(
                time=activity.activity_time,
                activity_type=activity.type or activity.activity_type,
                status=activity.status,
                message=activity.message,
            )
            for activity in activities
            if activity.device_id in (None, device.id)
        ]
        rows.sort(key=lambda r: r.time or dt.datetime.min.replace(tzinfo=dt.UTC), reverse=True)
        kept, counting = capped(
            "list_device_activities", rows, noun="activities", page_truncated=more
        )
        _ = DEVICE_ACTIVITIES_PATH  # the endpoint this reads; named for the api-notes reader
        return DeviceActivityList(
            client_id=str(client_id),
            sites_covered=[site.slug],
            found=True,
            device=device.name,
            site=site.name,
            site_slug=site.slug,
            days=days,
            since=since,
            rows=kept,
            **counting,
        )


# --- patch installation history -----------------------------------------------

InstallStatus = Literal["failed", "installed", "all"]

#: What `status` is sent as. Only these two values ever reach the vendor: live,
#: an unrecognised `status` is not rejected but silently ignored, and the report
#: comes back unfiltered (api-notes). "all" sends no status at all (`None`).
INSTALL_STATUS_PARAM: dict[str, str | None] = {
    "failed": "FAILED",
    "installed": "INSTALLED",
    "all": None,
}

MAX_PATCH_TALLIES = 25


class PatchInstallRow(SiteTagged):
    device: str
    patch: str
    kb: str | None = None
    status: str | None = Field(default=None, description="INSTALLED or FAILED.")
    severity: str | None = None
    type: str | None = None
    installed_at: dt.datetime | None = Field(
        default=None, description="When the installation was attempted."
    )


class PatchTally(BaseModel):
    """One patch across the fleet: how many machines took it, and how many did not."""

    patch: str
    kb: str | None = None
    installed: int = 0
    failed: int = 0
    devices: int = Field(description="Distinct machines with an attempt in the window.")


class PatchInstallList(RowCapped):
    """The answer to "what was installed, and what failed, this week?"."""

    client_id: str
    sites_covered: list[str]
    days: int
    since: dt.datetime
    status: InstallStatus
    status_counts: dict[str, int] = Field(
        description="Installations per status across everything matched, before the row cap."
    )
    devices_affected: int = Field(description="Distinct machines across everything matched.")
    patches: list[PatchTally] = Field(
        description=(
            "Per-patch totals across everything matched, before the row cap: most failures "
            f"first, then most installations. At most {MAX_PATCH_TALLIES}; `patches_total` "
            "says how many distinct patches there were."
        )
    )
    patches_total: int
    rows: list[PatchInstallRow]


@tool(ConnectorName.NINJAONE)
async def list_patch_installs(
    client_id: str,
    days: int = 7,
    status: InstallStatus = "failed",
    site_id: str | None = None,
) -> PatchInstallList:
    """OS patch installation history for the last `days` days: what failed, or what went on.

    `status` is "failed" (the default), "installed" or "all".

    - "failed": installations NinjaOne attempted and could not complete — "what
      failed on patch night", "which machines keep failing updates".
    - "installed": what succeeded — "what went on this week", "did KB5060000
      install", "was the finance server patched on Tuesday".
    - "all": both, for a success rate.

    A week of successful installs is thousands of rows on a real tenant, so
    read the totals first: `status_counts` and `devices_affected` cover
    everything matched, and `patches` gives per-patch installed and failed
    counts. `rows` is the capped detail — failures first, then newest — and
    is for naming machines. Pass `site` whenever the question is about one
    customer.

    Not for patches still waiting to be installed or approved — that is
    `list_patch_status`, which holds no history. Not for "needs a reboot" —
    that is `list_pending_reboot`. Windows patches only; third-party software
    patch history is not read by any tool yet.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    since = _utcnow() - dt.timedelta(days=days)
    params = {"installedAfter": since.strftime("%Y-%m-%d")}
    # Refused here rather than sent: this report ignores a status it does not
    # know, so a typo would come back as every installation, labelled as asked.
    wanted = _vendor_value("status", status, INSTALL_STATUS_PARAM)
    if wanted is not None:
        params["status"] = wanted
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        raw, page_truncated, _ = await connector.query_scoped("os-patch-installs", params=params)
        installs = [NinjaPatchInstall.model_validate(item) for item in raw]
        # The vendor ignores a status it does not recognise rather than refusing it,
        # so the filter is applied again here: a failure list holds only failures.
        if wanted is not None:
            installs = [row for row in installs if (row.status or "").upper() == wanted]
        placed = await connector.place_by_device(
            installs,
            [row.device_id for row in installs],
            endpoint=QUERY_PATH.format(report="os-patch-installs"),
        )

        tallies: dict[tuple[str, str | None], PatchTally] = {}
        tally_devices: dict[tuple[str, str | None], set[int]] = {}
        rows: list[PatchInstallRow] = []
        for site, device, install in placed:
            name = install.name or "(unnamed)"
            kb = install.kb_number or None
            outcome = (install.status or "unknown").upper()
            rows.append(
                PatchInstallRow(
                    site=site.name,
                    site_slug=site.slug,
                    device=device.name,
                    patch=name,
                    kb=kb,
                    status=outcome,
                    severity=install.severity,
                    type=install.type,
                    installed_at=install.installed_at,
                )
            )
            key = (name, kb)
            tally = tallies.setdefault(key, PatchTally(patch=name, kb=kb, devices=0))
            if outcome == "FAILED":
                tally.failed += 1
            elif outcome == "INSTALLED":
                tally.installed += 1
            tally_devices.setdefault(key, set()).add(device.id)
        for key, tally in tallies.items():
            tally.devices = len(tally_devices[key])

        oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
        rows.sort(key=lambda r: r.installed_at or oldest, reverse=True)
        rows.sort(key=lambda r: r.status != "FAILED")  # stable: failures first, each newest first
        ranked = sorted(
            tallies.values(), key=lambda item: (-item.failed, -item.installed, item.patch)
        )
        counts = Counter(row.status or "unknown" for row in rows)
        kept, counting = capped(
            "list_patch_installs", rows, noun="installations", page_truncated=page_truncated
        )
        return PatchInstallList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            days=days,
            since=since,
            status=status,
            status_counts=dict(counts.most_common()),
            devices_affected=len({device.id for _, device, _ in placed}),
            patches=ranked[:MAX_PATCH_TALLIES],
            patches_total=len(ranked),
            rows=kept,
            **counting,
        )


# --- Windows services -----------------------------------------------------------

ServiceState = Literal["stopped", "running", "paused", "any"]


class WindowsServiceRow(SiteTagged):
    device: str
    service: str
    display_name: str | None = None
    state: str | None = None
    start_type: str | None = None
    run_as: str | None = None


class WindowsServiceList(RowCapped):
    """The answer to "which services are stopped on this customer's machines?"."""

    client_id: str
    sites_covered: list[str]
    found: bool
    message: str | None = None
    filtered_by: dict[str, str] = Field(default_factory=dict)
    auto_start_not_running: int = Field(
        default=0,
        description="Rows set to start automatically that are not running: the ones to look at.",
    )
    rows: list[WindowsServiceRow] = Field(default_factory=list)


@tool(ConnectorName.NINJAONE)
async def list_windows_services(
    client_id: str,
    device_name: str | None = None,
    state: ServiceState = "stopped",
    name: str | None = None,
    site_id: str | None = None,
) -> WindowsServiceList:
    """List Windows services on one end customer's machines, by state and name.

    Use this for "is the print spooler running on PC-014", "which services
    are stopped on the finance server", "where is SQL Server not running".
    A `site` is REQUIRED: the tenant holds hundreds of thousands of service
    rows, so without one the tool asks for it instead of reading them all.
    Narrow further with `device_name`, and with `name`, the service's short
    name as Windows knows it ("Spooler", "wuauserv", "MSSQLSERVER").

    Most stopped services are meant to be: they start on demand. The ones
    that matter are set to start automatically and are not running — those
    sort first and are counted in `auto_start_not_running`.

    Not for "what is alerting" (`list_open_alerts`), nor for what happened on
    a machine over time (`list_device_activities`). Read-only: this tool
    cannot start or restart anything.
    """
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        if not connector.scope.is_single_site:
            return WindowsServiceList(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                found=False,
                total=0,
                returned=0,
                message=(
                    f"This covers {connector.scope.describe_targets()}. Windows services are "
                    "far too many to read tenant-wide: name one end customer with `site`."
                ),
            )

        applied: dict[str, str] = {"state": state}
        params: dict[str, Any] = {}
        if state != "any":
            params["state"] = state.upper()
        if name:
            params["name"] = name
            applied["name"] = name

        wanted_device: int | None = None
        if device_name:
            resolved = await _resolve_device(connector, device_name)
            if isinstance(resolved, list):
                candidates, message = _not_one_device(client_id, connector, device_name, resolved)
                return WindowsServiceList(
                    client_id=str(client_id),
                    sites_covered=_covered(connector),
                    found=False,
                    total=0,
                    returned=0,
                    message=message
                    + (" Candidates: " + ", ".join(candidates) if candidates else ""),
                )
            wanted_device = resolved[1].id
            applied["device"] = resolved[1].name

        raw, page_truncated, _ = await connector.query_scoped("windows-services", params=params)
        services = [NinjaWindowsService.model_validate(item) for item in raw]
        if wanted_device is not None:
            services = [s for s in services if s.device_id == wanted_device]
        placed = await connector.place_by_device(
            services,
            [s.device_id for s in services],
            endpoint=QUERY_PATH.format(report="windows-services"),
        )
        rows = [
            WindowsServiceRow(
                site=site.name,
                site_slug=site.slug,
                device=device.name,
                service=service.name or "(unnamed)",
                display_name=service.display_name,
                state=service.state,
                start_type=service.start_type,
                run_as=service.user_name,
            )
            for site, device, service in placed
        ]
        concerning = [
            service.should_be_running and (service.state or "").upper() != "RUNNING"
            for _, _, service in placed
        ]
        order = sorted(
            range(len(rows)),
            key=lambda i: (
                not concerning[i],
                rows[i].device.casefold(),
                rows[i].service.casefold(),
            ),
        )
        rows = [rows[i] for i in order]
        kept, counting = capped(
            "list_windows_services", rows, noun="services", page_truncated=page_truncated
        )
        return WindowsServiceList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            found=True,
            filtered_by=applied,
            auto_start_not_running=sum(concerning),
            rows=kept,
            **counting,
        )


# --- hardware -------------------------------------------------------------------

Windows11Filter = Literal["already_windows_11", "eligible", "ineligible", "unknown", "not_windows"]


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    """One machine with everything the hardware classifications need, placed at its site."""

    site: SiteRef
    device: NinjaDevice
    system: NinjaComputerSystem
    processor: str | None
    os_name: str | None
    kind: MachineKind
    windows11: Eligibility


async def collect_hardware(connector: NinjaOneClient) -> tuple[list[HardwareProfile], bool]:
    """Three reports joined per device: computer-systems, processors, operating-systems.

    Shared by `get_hardware` and the business-review report so the two can
    never disagree about what is virtual or what can run Windows 11.
    """
    raw, truncated, _ = await connector.query_scoped("computer-systems")
    systems = [NinjaComputerSystem.model_validate(item) for item in raw]
    placed = await connector.place_by_device(
        systems,
        [s.device_id for s in systems],
        endpoint=QUERY_PATH.format(report="computer-systems"),
    )
    raw_cpu, cpu_truncated, _ = await connector.query_scoped("processors")
    raw_os, os_truncated, _ = await connector.query_scoped("operating-systems")
    # Keyed by device id and only ever read for a device that was placed above,
    # so a row for a device outside the scope is never used.
    processors: dict[int, str] = {}
    for item in raw_cpu:
        cpu = NinjaProcessor.model_validate(item)
        if cpu.device_id is not None and cpu.name:
            processors.setdefault(cpu.device_id, cpu.name)
    systems_os: dict[int, str] = {}
    for item in raw_os:
        os_row = NinjaOperatingSystemRow.model_validate(item)
        if os_row.device_id is not None and os_row.name:
            systems_os[os_row.device_id] = os_row.name

    profiles = [
        HardwareProfile(
            site=site,
            device=device,
            system=system,
            processor=processors.get(device.id),
            os_name=systems_os.get(device.id),
            kind=classify_machine(
                virtual_flag=system.virtual_machine,
                node_class=device.node_class,
                manufacturer=system.manufacturer,
                model=system.model,
            ),
            windows11=windows11_eligibility(
                os_name=systems_os.get(device.id),
                processor=processors.get(device.id),
                memory_bytes=system.total_physical_memory,
            ),
        )
        for site, device, system in placed
    ]
    return profiles, truncated or cpu_truncated or os_truncated


class HardwareRow(SiteTagged):
    device: str
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    chassis: str | None = None
    kind: MachineKind = Field(description="physical or virtual; unknown if nothing was reported.")
    processor: str | None = None
    memory_gb: float | None = None
    os_name: str | None = None
    windows11: Windows11Filter = Field(
        description="Windows 11 hardware eligibility by processor generation and memory."
    )
    windows11_reasons: list[str] = Field(default_factory=list)
    enrolled_at: dt.datetime | None = Field(
        default=None,
        description="When NinjaOne first saw the device. NOT its age: only a lower bound.",
    )
    age_years: None = Field(
        default=None, description="Always empty: NinjaOne reports no date to derive an age from."
    )


class HardwareList(RowCapped):
    """The answer to "what hardware does this customer have?"."""

    client_id: str
    sites_covered: list[str]
    filtered_by: dict[str, str] = Field(default_factory=dict)
    age_available: bool = False
    age_note: str
    windows11_note: str = WINDOWS11_BASIS
    model_counts: dict[str, int] = Field(
        description="Devices per manufacturer and model, across everything matched."
    )
    kind_counts: dict[str, int] = Field(description="physical / virtual / unknown, before the cap.")
    windows11_counts: dict[str, int] = Field(
        description="Devices per Windows 11 eligibility status, before the cap."
    )
    rows: list[HardwareRow]


AGE_UNAVAILABLE = (
    "NinjaOne reports no purchase date, BIOS date or warranty date for these devices, so "
    "their age cannot be determined. `enrolled_at` is when NinjaOne first saw each device, "
    "which is only a lower bound: a machine can be years old when it is enrolled. For "
    "purchase or warranty dates, check the PSA's configuration items."
)


@tool(ConnectorName.NINJAONE)
async def get_hardware(
    client_id: str,
    older_than_years: float | None = None,
    kind: Literal["physical", "virtual"] | None = None,
    windows11: Windows11Filter | None = None,
    site_id: str | None = None,
) -> HardwareList:
    """The hardware inventory: model, serial, chassis, processor, memory, per device.

    Use this for "what models does Acme run", "how many OptiPlex 7010s",
    "give me the serials for the warranty lookup", "which servers are virtual"
    (`kind`), "which machines cannot run Windows 11" (`windows11="ineligible"`)
    or "which Windows 10 machines can be upgraded" (`windows11="eligible"`).
    `model_counts`, `kind_counts` and `windows11_counts` answer fleet
    questions without reading rows.

    Windows 11 eligibility is judged from the processor generation and
    installed memory only. NinjaOne does not report TPM, Secure Boot or UEFI,
    so say "by processor and memory" and never "confirmed compatible".

    Hardware AGE is not available: NinjaOne sends no purchase, BIOS or
    warranty date, so `age_years` is always empty and `older_than_years`
    cannot be honoured — the tool says so and returns nothing rather than
    guess. `enrolled_at` is when NinjaOne first saw the device, a lower bound
    only; never present it as the machine's age.

    Not for one machine in full (OS build, disks, last user, IPs) — that is
    `get_device`. Not for counting devices by class or OS — `count_devices`.
    """
    if older_than_years is not None and older_than_years <= 0:
        raise ValueError("older_than_years must be greater than 0")
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        if older_than_years is not None:
            # Hard rule 7. Filtering on enrolment date would pass off "how long
            # we have managed it" as "how old it is".
            return HardwareList(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                age_note=(
                    f"Cannot list hardware older than {older_than_years:g} years. "
                    + AGE_UNAVAILABLE
                    + " Ask again without an age to list the hardware with enrolment dates."
                ),
                model_counts={},
                kind_counts={},
                windows11_counts={},
                rows=[],
                total=0,
                returned=0,
            )

        profiles, page_truncated = await collect_hardware(connector)
        applied: dict[str, str] = {}
        if kind is not None:
            profiles = [profile for profile in profiles if profile.kind == kind]
            applied["kind"] = kind
        if windows11 is not None:
            profiles = [p for p in profiles if p.windows11.status == windows11]
            applied["windows11"] = windows11
        rows = [
            HardwareRow(
                site=profile.site.name,
                site_slug=profile.site.slug,
                device=profile.device.name,
                manufacturer=profile.system.manufacturer,
                model=profile.system.model,
                serial_number=profile.system.serial_number or profile.system.bios_serial_number,
                chassis=profile.system.chassis_type,
                kind=profile.kind,
                processor=profile.processor,
                memory_gb=_gb(profile.system.total_physical_memory),
                os_name=profile.os_name,
                windows11=profile.windows11.status,
                windows11_reasons=list(profile.windows11.reasons),
                enrolled_at=profile.device.created,
            )
            for profile in profiles
        ]
        rows.sort(key=lambda r: (r.site.casefold(), r.device.casefold()))
        models = Counter(
            " ".join(part for part in (row.manufacturer, row.model) if part) or "unknown"
            for row in rows
        )
        kept, counting = capped("get_hardware", rows, noun="devices", page_truncated=page_truncated)
        return HardwareList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            filtered_by=applied,
            age_note=AGE_UNAVAILABLE,
            model_counts=dict(models.most_common()),
            kind_counts=dict(Counter(row.kind for row in rows).most_common()),
            windows11_counts=dict(Counter(row.windows11 for row in rows).most_common()),
            rows=kept,
            **counting,
        )


# --- custom fields ----------------------------------------------------------------

#: A field whose name or label contains one of these is treated as a secret even
#: when its type is plain text: engineers do stash recovery keys in text fields,
#: and a tool result is kept in the conversation thread.
SECRET_FIELD_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api key",
    "recovery",
    "bitlocker",
    "credential",
    "private key",
    "passphrase",
    "pin",
)

MAX_FIELD_VALUE_LENGTH = 300


class CustomFieldDefinitionRow(BaseModel):
    name: str
    label: str | None = None
    type: str | None = None
    description: str | None = None
    active: bool | None = None
    readable: bool = Field(description="False for encrypted or secret-looking fields.")


class CustomFieldValueRow(SiteTagged):
    device: str
    value: str


class CustomFieldList(RowCapped):
    """The answer to "what custom fields exist?" or "what is field X on each device?"."""

    client_id: str
    sites_covered: list[str]
    field: str | None = None
    message: str | None = None
    definitions: list[CustomFieldDefinitionRow] = Field(default_factory=list)
    devices_without_a_value: int = 0
    rows: list[CustomFieldValueRow] = Field(default_factory=list)


def _looks_secret(definition: NinjaCustomFieldDefinition) -> bool:
    haystack = f"{definition.name} {definition.label or ''}".casefold()
    return definition.is_encrypted or any(hint in haystack for hint in SECRET_FIELD_HINTS)


@tool(ConnectorName.NINJAONE)
async def list_custom_fields(
    client_id: str, field: str | None = None, site_id: str | None = None
) -> CustomFieldList:
    """Device custom fields: the fields this tenant defines, or one field's value per device.

    Without `field`, returns the definitions — name, label, type — and no
    device data: use that first to learn what exists. With `field` (its name
    or label), returns that field's value for each device that has one; pass
    `site` to keep it to one end customer. Use it for "what is the asset tag
    on PC-014", "who is the primary user", "which machines are flagged
    decommissioned".

    Encrypted fields, and fields whose name suggests a secret (password,
    recovery key, token, BitLocker, PIN), are never read: `readable` is false
    and asking for one returns a refusal, not a value. Do not try to work
    around that; tell the engineer to look in the NinjaOne console.

    Not for hardware details (`get_hardware`) or OS and network details
    (`get_device`): those come from NinjaOne's own inventory, not custom fields.
    """
    async with connector_for(client_id, ConnectorName.NINJAONE, site_id) as connector:
        assert isinstance(connector, NinjaOneClient)
        definitions = await connector.custom_field_definitions()

        if not field:
            listing = [
                CustomFieldDefinitionRow(
                    name=d.name,
                    label=d.label,
                    type=d.type,
                    description=d.description or None,
                    active=d.active,
                    readable=not _looks_secret(d),
                )
                for d in sorted(definitions, key=lambda d: (d.label or d.name).casefold())
            ]
            kept_definitions, counting = capped("list_custom_fields", listing, noun="fields")
            return CustomFieldList(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                definitions=kept_definitions,
                **counting,
            )

        wanted = field.strip().casefold()
        match = next(
            (
                d
                for d in definitions
                if d.name.casefold() == wanted or (d.label or "").casefold() == wanted
            ),
            None,
        )
        if match is None:
            known = ", ".join(sorted(d.label or d.name for d in definitions)[:40])
            return CustomFieldList(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                field=field,
                total=0,
                returned=0,
                message=f"This tenant has no custom field called {field!r}. It has: {known}",
            )
        if _looks_secret(match):
            return CustomFieldList(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                field=match.name,
                total=0,
                returned=0,
                message=(
                    f"{match.label or match.name!r} is an encrypted or secret-looking field "
                    f"(type {match.type}). Desk does not read those. Look it up in the "
                    "NinjaOne console."
                ),
            )

        # `fields=` asks the vendor for this one field only, so no other value
        # (least of all a secret one) is even transferred.
        raw, page_truncated, _ = await connector.query_scoped(
            "custom-fields", params={"fields": match.name}
        )
        placed = await connector.place_by_device(
            raw,
            [
                item.get("deviceId") if isinstance(item.get("deviceId"), int) else None
                for item in raw
            ],
            endpoint=QUERY_PATH.format(report="custom-fields"),
        )
        rows: list[CustomFieldValueRow] = []
        empty = 0
        for site, device, item in placed:
            raw_fields = item.get("fields")
            values: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
            value = values.get(match.name)
            if value is None or value == "":
                empty += 1
                continue
            text = str(value)
            if len(text) > MAX_FIELD_VALUE_LENGTH:
                text = text[:MAX_FIELD_VALUE_LENGTH] + "… [truncated]"
            rows.append(
                CustomFieldValueRow(
                    site=site.name, site_slug=site.slug, device=device.name, value=text
                )
            )
        rows.sort(key=lambda r: (r.site.casefold(), r.device.casefold()))
        kept, counting = capped(
            "list_custom_fields", rows, noun="devices", page_truncated=page_truncated
        )
        return CustomFieldList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            field=match.name,
            devices_without_a_value=empty,
            rows=kept,
            **counting,
        )

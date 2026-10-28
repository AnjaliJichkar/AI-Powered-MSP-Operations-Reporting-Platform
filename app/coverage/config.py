"""The tools a device should be covered by, and when coverage counts as stale.

Two things live here and nowhere else:

**The tool list** (`COVERAGE_TOOLS`). One entry per tool, in the order the
page shows them. An entry says which connector it reads, how to turn that
connector's inventory rows into `ToolEndpoint`s, and what to call a gap.
Backup is listed with no reader, so every device shows `not_assessed` for it
until Cove exists; wiring Cove in is one reader function on this entry and
nothing in the callers changes, because callers iterate the list.

**The thresholds** (`STALE_RULES`). Every number that decides `stale` is a
rule with a definition, so the page, the tools and the review can print why
a device counts as stale and an engineer can see what would change it. A
tool without a date on its inventory row (Bitdefender's endpoint list carries
no last-seen; that is one call per endpoint) is judged on what the row does
carry — agent present, agent current, core protection on — and says so.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.connectors.bitdefender.models import BdEndpoint
from app.connectors.connectsecure.models import CsAsset
from app.connectors.connectsecure.tools import STATUS_CURRENT, STATUS_NO_AGENT, scan_status
from app.connectors.cove.models import CoveDevice
from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.scope import SiteRef
from app.coverage.core import (
    EndpointState,
    ManagedDevice,
    ToolEndpoint,
    external_target,
    is_agent_class,
)
from app.models.enums import ConnectorName
from app.settings import get_settings

# --- thresholds ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaleRule:
    """One threshold behind a `stale` verdict, and what it means."""

    key: str
    tool: str
    label: str
    days: int | None
    """The threshold in days; None for a rule that is a flag, not an age."""
    definition: str

    def describe(self) -> str:
        return f"{self.label}: {self.definition}"


def _scan_days() -> int:
    return get_settings().connectsecure_stale_scan_days


#: The default for `COVERAGE_RMM_STALE_DAYS`: a NinjaOne device that has not
#: contacted the platform for more than this many days is `rmm_stale` on the
#: board. The live value is `rmm_stale_days()`.
NINJAONE_LAST_CONTACT_DAYS = 14


def rmm_stale_days() -> int:
    return get_settings().coverage_rmm_stale_days


#: A Cove device whose last *successful* backup is older than this is `stale`:
#: the agent is there and the backup is not current.
COVE_STALE_BACKUP_DAYS = 3


STALE_RULES: tuple[StaleRule, ...] = (
    StaleRule(
        key="ninjaone.last_contact",
        tool="ninjaone",
        label="NinjaOne last contact",
        days=NINJAONE_LAST_CONTACT_DAYS,
        definition=(
            f"A managed device with no NinjaOne contact for more than {NINJAONE_LAST_CONTACT_DAYS} "
            "days (COVERAGE_RMM_STALE_DAYS) is rmm_stale: the RMM itself has lost it, so it is "
            "its own line and never counted as absent from another tool. It still takes part "
            "in the join: paired with a row a tool has heard from within the same number of "
            "days (a ConnectSecure scan; for Bitdefender, whose list carries no last-seen, an "
            "agent installed), it is rmm_agent_down instead (machine alive per Bitdefender or "
            "ConnectSecure, NinjaOne not reporting), and that row is never an unmanaged endpoint."
        ),
    ),
    StaleRule(
        key="bitdefender.agent_outdated",
        tool="bitdefender",
        label="Bitdefender agent out of date",
        days=None,
        definition=(
            "GravityZone flags the agent as missing product updates (productOutdated). The "
            "endpoint list carries no last-seen or definitions date, so age is not assessed "
            "from it; this flag and the core modules are what the row says."
        ),
    ),
    StaleRule(
        key="bitdefender.protection_off",
        tool="bitdefender",
        label="Bitdefender core protection off",
        days=None,
        definition=(
            "The antimalware or Advanced Threat Control module is disabled by policy on an "
            "endpoint that has the agent: installed, and not protecting."
        ),
    ),
    StaleRule(
        key="cove.backup_age",
        tool="backup",
        label="Cove last successful backup",
        days=COVE_STALE_BACKUP_DAYS,
        definition=(
            f"A device whose last successful backup is more than {COVE_STALE_BACKUP_DAYS} days "
            "old: Cove holds the device and the backup is not current. A device Cove holds "
            "that has never backed up successfully is a gap rather than stale, because "
            "nothing has ever been kept for it."
        ),
    ),
    StaleRule(
        key="connectsecure.scan_age",
        tool="connectsecure",
        label="ConnectSecure scan age",
        days=_scan_days(),
        definition=(
            f"An asset whose last scan is {_scan_days()} days old or more "
            "(CONNECTSECURE_STALE_SCAN_DAYS), was never scanned, or whose agent is deprecated, "
            "retired or has stopped reporting: its findings are as old as that scan."
        ),
    ),
)


#: ConnectSecure agent types that are probes, not agents on the machine.
PROBE_AGENT_TYPES = frozenset({"PROBE", "NETWORK", "NETWORKPROBE", "SCAN"})


def rules_for(tool: str) -> list[StaleRule]:
    return [rule for rule in STALE_RULES if rule.tool == tool]


# --- per-tool readers: vendor rows into ToolEndpoints -----------------------------


def ninjaone_population(
    rows: Sequence[tuple[SiteRef, NinjaDevice]],
    macs: Mapping[int, frozenset[str]] | None = None,
    serials: Mapping[int, str] | None = None,
) -> list[ManagedDevice]:
    """The population: NinjaOne computers, the machines an agent belongs on.

    Network gear, phones and hypervisor hosts are left out, as the review
    leaves them out: a switch without a Bitdefender endpoint is not a gap.
    The name is `NinjaDevice.hostname`: the full `dnsName`, else `systemName`.
    `macs` is the device's interfaces from the `network-interfaces` query, for
    the third pass; `serials` the `computer-systems` serial, for the fourth.
    """
    return [
        ManagedDevice(
            site_id=site.site_id,
            site_slug=site.slug,
            site_name=site.name,
            device_id=str(device.id),
            name=device.hostname,
            node_class=device.node_class,
            last_contact=device.last_contact,
            macs=(macs or {}).get(device.id, frozenset()),
            serials=frozenset(s for s in (serial_key((serials or {}).get(device.id)),) if s),
        )
        for site, device in rows
        if is_agent_class(device.node_class)
    ]


def mac_key(value: str | None) -> str | None:
    """A MAC as twelve lowercase hex digits, or None for nothing usable.

    Three kinds are refused, because pairing two machines on one of them is
    worse than leaving both unpaired:

    - **all zeros or all ones**: a placeholder, not an address;
    - **locally administered** (the second-least-significant bit of the first
      octet): assigned by software rather than burned into a card. VPN
      clients, container bridges and QEMU all mint these, and they collide
      freely across machines;
    - **a hypervisor's own range** (`VIRTUAL_OUIS`): a VMware or Hyper-V
      virtual NIC. These are unique per machine *most* of the time, which is
      exactly what makes them dangerous — a cloned VM carries its template's
      address and the two machines become one.

    The join's own guard already drops a value two devices share. This one
    catches the case that guard cannot: a duplicate where only one of the two
    is in the data we are looking at.
    """
    if not value:
        return None
    hexed = re.sub(r"[^0-9a-f]", "", value.lower())
    if len(hexed) != 12 or hexed in ("000000000000", "ffffffffffff"):
        return None
    if int(hexed[:2], 16) & 0b10:  # locally administered
        return None
    if hexed[:6] in VIRTUAL_OUIS:
        return None
    return hexed


#: Hypervisor and virtual-adapter ranges. A MAC from one of these belongs to
#: a virtual NIC, which a clone or a template can duplicate onto a second
#: machine — and two machines sharing an address is the one thing the passes
#: must never treat as an identity.
VIRTUAL_OUIS = frozenset(
    {
        "00155d",  # Microsoft Hyper-V
        "0003ff",  # Microsoft virtual (legacy)
        "005056",  # VMware
        "000c29",  # VMware
        "000569",  # VMware
        "001c14",  # VMware
        "080027",  # VirtualBox
        "00163e",  # Xen
        "001c42",  # Parallels
        "00e04c",  # Realtek USB docks and dongles: one adapter, many laptops
    }
)

#: Serial numbers that are placeholders a board vendor left, not identities.
JUNK_SERIALS = frozenset(
    {
        "",
        "0",
        "NONE",
        "N/A",
        "DEFAULT STRING",
        "TO BE FILLED BY O.E.M.",
        "SYSTEM SERIAL NUMBER",
        "NOT SPECIFIED",
        "NOT APPLICABLE",
        "123456789",
    }
)


def serial_key(value: str | None) -> str | None:
    """A serial number upper-cased with its whitespace collapsed, or None for a
    placeholder (`JUNK_SERIALS`) or nothing at all."""
    if not value:
        return None
    cleaned = " ".join(str(value).split()).upper()
    return None if cleaned in JUNK_SERIALS else cleaned


def bitdefender_endpoints(rows: Sequence[tuple[SiteRef, BdEndpoint]]) -> list[ToolEndpoint]:
    """Every GravityZone endpoint row, judged on what the row carries.

    No agent is `absent`: GravityZone discovered the machine (network scan,
    Active Directory, a removed agent) and nothing protects it, which is the
    same gap as no row at all and is reported as one. An agent that is out of
    date or has core protection off is `stale`. A row with `isManaged: false`
    is discovered, not an endpoint: it still pairs with its device (which is
    then absent), but it is never an unmanaged endpoint.
    """
    found: list[ToolEndpoint] = []
    state: EndpointState
    for site, endpoint in rows:
        modules = endpoint.modules or {}
        core_off = any(
            modules.get(module) is False for module in ("antimalware", "advancedThreatControl")
        )
        if not endpoint.has_agent:
            state, detail = "absent", "known to GravityZone, no agent installed"
        elif endpoint.product_outdated:
            state, detail = "stale", "agent out of date"
        elif core_off:
            state, detail = "stale", "core protection off by policy"
        else:
            state, detail = "covered", "agent current, core protection on"
        found.append(
            ToolEndpoint(
                site_id=site.site_id,
                name=endpoint.name,
                state=state,
                detail=detail,
                last_seen=endpoint.last_successful_scan.date
                if endpoint.last_successful_scan
                else None,
                external_id=endpoint.id,
                macs=frozenset(k for k in (mac_key(m) for m in endpoint.macs or []) if k),
                discovered="isManaged false" if endpoint.is_managed is False else None,
            )
        )
    return found


def connectsecure_endpoints(
    rows: Sequence[tuple[SiteRef, CsAsset]], *, now: dt.datetime
) -> list[ToolEndpoint]:
    """Every ConnectSecure asset, judged by the scan status the connector's own tool uses.

    An asset whose name is an IP address or an ISP's reverse-DNS name is an
    external scan target (`core.external_target`): reported apart, never
    paired and never an unmanaged endpoint. The serial number rides along for
    the fourth pass.
    """
    stale_days = _scan_days()
    found: list[ToolEndpoint] = []
    state: EndpointState
    for site, asset in rows:
        status = scan_status(asset, now=now, stale_days=stale_days)
        if status == STATUS_CURRENT:
            state = "covered"
        elif status == STATUS_NO_AGENT:
            state = "absent"
        else:
            state = "stale"
        agent = asset.agent
        agent_type = (asset.agent_type or (agent.agent_type if agent else None) or "").upper()
        probe = agent_type in PROBE_AGENT_TYPES or (asset.asset_type or "").upper() == "PROBE"
        discovered = None
        if probe:
            discovered = f"probe ({agent_type or asset.asset_type})"
        elif agent is not None and (agent.is_deprecated or agent.is_retired or agent.agent_deleted):
            discovered = "agent deprecated, retired or deleted"
        elif asset.is_deprecated:
            discovered = "asset deprecated"
        mac = mac_key(asset.mac)
        found.append(
            ToolEndpoint(
                site_id=site.site_id,
                name=asset.host_name,
                state=state,
                detail=status,
                last_seen=asset.last_scan,
                external_id=asset.id,
                macs=frozenset({mac}) if mac else frozenset(),
                discovered=discovered,
                probe=probe,
                serial=serial_key(asset.serial_number),
                external=external_target(asset.host_name),
            )
        )
    return found


# --- the tool list ---------------------------------------------------------------

Reader = Callable[[Sequence[tuple[SiteRef, Any]], dt.datetime], list[ToolEndpoint]]


@dataclass(frozen=True, slots=True)
class CoverageTool:
    """One tool a managed device should be covered by."""

    key: str
    label: str
    connector: ConnectorName | None
    """Whose inventory is read. None: nothing reads it yet (Backup until Cove)."""
    reader: Reader | None
    """Vendor rows into `ToolEndpoint`s. None means every device is `not_assessed`."""
    gap: str
    """What a gap in this tool means, in the words the page and the deck use."""
    not_assessed_reason: str = ""

    @property
    def assessed(self) -> bool:
        return self.reader is not None


def cove_endpoints(
    rows: Sequence[tuple[SiteRef, CoveDevice]], *, now: dt.datetime
) -> list[ToolEndpoint]:
    """Every Cove device row, judged on when it last backed up successfully.

    **A Microsoft 365 tenant is not an endpoint.** Cove holds a customer's
    tenant as a device row with no OS and no physicality; it is not a machine
    and must never appear on this board, either as a covered device or as an
    unmanaged endpoint. `is_machine` is the test.

    **The name is the machine's own, with Cove's suffix taken off**
    (`jc-lt-25_pqpsp` -> `jc-lt-25`), preferring Cove's computer name over
    its device name where the two differ. It is a rule rather than a
    certainty, which is why a gap it produces is phrased as "not backed up by
    Cove" and never as a machine having no backup — and why the MACs go on
    the row too, so the join's third pass can pair a machine the names miss.
    Cove sends no serial number of any kind, so the fourth pass has nothing
    to work with here (docs/api-notes/cove.md).

    A device Cove holds that has never backed up successfully is `absent`:
    the row exists and nothing has been kept, which is the same gap as no row
    at all — the GravityZone rule, for the same reason.
    """
    found: list[ToolEndpoint] = []
    state: EndpointState
    cutoff = dt.timedelta(days=COVE_STALE_BACKUP_DAYS)
    for site, device in rows:
        if not device.is_machine:
            continue
        if device.last_successful_at is None:
            state, detail = "absent", "known to Cove, no successful backup on record"
        elif now - device.last_successful_at > cutoff:
            days = (now - device.last_successful_at).days
            state, detail = "stale", f"last successful backup {days} days ago"
        else:
            state, detail = "covered", "backed up successfully in the last three days"
        found.append(
            ToolEndpoint(
                site_id=site.site_id,
                name=device.match_key,
                state=state,
                detail=detail,
                last_seen=device.last_successful_at,
                external_id=str(device.account_id),
                macs=frozenset(k for k in (mac_key(m) for m in device.macs) if k),
            )
        )
    return found


COVERAGE_TOOLS: tuple[CoverageTool, ...] = (
    CoverageTool(
        key="bitdefender",
        label="Endpoint protection",
        connector=ConnectorName.BITDEFENDER,
        reader=lambda rows, now: bitdefender_endpoints(rows),
        gap="no protection agent",
        not_assessed_reason="No customer of this franchisee has a GravityZone company "
        "mapped, or the franchisee has no Bitdefender credentials.",
    ),
    CoverageTool(
        key="connectsecure",
        label="Vulnerability scanning",
        connector=ConnectorName.CONNECTSECURE,
        reader=lambda rows, now: connectsecure_endpoints(rows, now=now),
        gap="not vulnerability-scanned",
        not_assessed_reason="No customer of this franchisee has a ConnectSecure company "
        "mapped, or the franchisee has no ConnectSecure credentials.",
    ),
    CoverageTool(
        key="backup",
        label="Backup",
        connector=ConnectorName.COVE,
        reader=lambda rows, now: cove_endpoints(rows, now=now),
        gap="not backed up by Cove",
        not_assessed_reason="No customer of this franchisee has a Cove customer mapped, "
        "or the franchisee has no Cove credentials.",
    ),
)


def tool_by_key(key: str) -> CoverageTool | None:
    return next((tool for tool in COVERAGE_TOOLS if tool.key == key), None)

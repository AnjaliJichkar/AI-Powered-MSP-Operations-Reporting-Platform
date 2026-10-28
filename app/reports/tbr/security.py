"""Security facts for the review: Bitdefender coverage, BitLocker, and the cross-tool join.

Three reads, each guarded like every other collector (a vendor that fails is a
recorded gap, never a zero), and one derivation:

- **NinjaOne BitLocker** (`/v2/queries/volumes?include=bl`): whether every
  local disk of a device is fully encrypted. Checked live on 2026-09-22: the
  `include=bl` parameter adds a `bitLockerStatus` object to volume rows.
- **Bitdefender endpoints** (`getNetworkInventoryItems`): agent present, agent
  current, core modules on, every other module's state, and the company risk
  score.
- **Bitdefender incidents** (`getIncidentsList`): detections created in the
  period, and whether they were blocked.
- **The join**: NinjaOne devices and Bitdefender endpoints paired by normalised
  hostname within the site (`app/connectors/matching.py`). A NinjaOne computer
  with no Bitdefender endpoint is a machine with no protection agent; the
  finding rules read that, not either tool alone.

Device-name groups the rules need (`FactSet.groups`) are recorded beside the
facts: which devices NinjaOne patches, which BitLocker covers, which
Bitdefender modules are off on. They are names, so a demo render replaces them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.bitdefender.models import (
    LICENSED_MODULES,
    WORKLOAD_MODULES,
    BdEndpoint,
    module_status,
    modules_seen,
)
from app.connectors.bitdefender.tools import (
    STATUS_CORE_OFF,
    STATUS_NO_AGENT,
    STATUS_OUTDATED,
    protection_status,
    unprotected_reasons,
)
from app.connectors.connectsecure.models import kb_numbers
from app.connectors.errors import ConnectorError, UpstreamError
from app.connectors.factory import connector_for
from app.connectors.ninjaone.client import QUERY_PATH, NinjaOneClient
from app.coverage.config import bitdefender_endpoints, rmm_stale_days

# The population rule lives with the board now; the review and its tests still
# name it from here.
from app.coverage.core import AGENT_NODE_CLASSES as AGENT_NODE_CLASSES
from app.coverage.core import (
    Assessment,
    DeviceCoverage,
    ManagedDevice,
    ToolEndpoint,
    ToolInventory,
    ToolState,
    Unmanaged,
    assess,
    external_target,
    group_machines,
    lost_state,
    mark_unchecked,
    ninjaone_state,
    unmanaged_entry,
)
from app.coverage.core import is_agent_class as is_agent_class
from app.devices.lastseen import LastSeenReads, candidates_for, read_last_seen
from app.models.enums import ConnectorName
from app.reports.tbr.model import Fact, FactSet, FactTable, Gap
from app.settings import get_settings

if TYPE_CHECKING:
    from app.reports.tbr.collect import Collector


BITLOCKER_ENCRYPTED = "FULLY_ENCRYPTED"

#: Group keys, so the rules and the tests name them once.
GROUP_NINJA_COMPUTERS = "ninjaone.computers"
GROUP_NINJA_PATCHING = "ninjaone.patching.devices"
GROUP_BITLOCKER_ENCRYPTED = "ninjaone.bitlocker.encrypted"
GROUP_BITLOCKER_UNENCRYPTED = "ninjaone.bitlocker.unencrypted"
GROUP_BD_ENDPOINTS = "bitdefender.endpoints"
GROUP_BD_NO_AGENT = "bitdefender.no_agent"
GROUP_BD_CORE_OFF = "bitdefender.core_off"
GROUP_BD_OUTDATED = "bitdefender.agent_outdated"
GROUP_BD_MODULE_OFF = "bitdefender.module_off.{module}"  # licensed, disabled by policy
GROUP_BD_MODULE_ON = "bitdefender.module_on.{module}"
GROUP_BD_MODULE_UNLICENSED = "bitdefender.module_unlicensed.{module}"  # the licence lacks it
GROUP_COVERAGE_MATCHED = "coverage.matched"
GROUP_COVERAGE_NINJA_ONLY = "coverage.ninjaone_only"
GROUP_COVERAGE_BD_ONLY = "coverage.bitdefender_only"
#: The coverage board's own verdict on each endpoint row (`app/coverage/config.py`),
#: recorded here so the review's join reads exactly what the board reads.
GROUP_BD_TOOL_STALE = "bitdefender.coverage.stale"
GROUP_BD_TOOL_ABSENT = "bitdefender.coverage.absent"
GROUP_BD_TOOL_DISCOVERED = "bitdefender.coverage.discovered"
#: Per endpoint: `bitdefender.macs.{name}` -> its MACs, for the board's third pass.
GROUP_BD_MACS = "bitdefender.macs.{name}"
#: Per endpoint paired with a lost device: `bitdefender.last_seen.{name}` -> the
#: `lastSeen` of `getManagedEndpointDetails`, the one date GravityZone gives.
GROUP_BD_LAST_SEEN = "bitdefender.last_seen.{name}"
#: Set when the last-seen read did not answer: the lost devices it was for are
#: not assessed for Bitdefender in this review, neither lost nor alive.
GROUP_BD_LAST_SEEN_FAILED = "bitdefender.last_seen.failed"
#: Per lost device: `ninjaone.lost_days.{name}` -> days since NinjaOne last heard
#: from it, so the last-seen read can leave out machines lost past the retire window.
GROUP_NINJA_LOST_DAYS = "ninjaone.lost_days.{name}"
#: Lost devices whose last-seen was not read: lost for longer than the retire window.
GROUP_BD_UNCHECKED = "bitdefender.last_seen.unchecked"
#: Per name several rows carry: `bitdefender.rows.{name}` -> one entry per row,
#: `id|mac,mac|serial`, so the review's join sees the rows the board sees rather
#: than one machine per name (three machines a scanner knows by one truncated
#: name are three rows, three MACs, three pairings).
GROUP_BD_ROWS = "bitdefender.rows.{name}"
#: NinjaOne devices the RMM itself has not heard from in `rmm_stale_days()`, and per
#: device `ninjaone.macs.{name}` -> its interface MACs.
GROUP_NINJA_RMM_STALE = "ninjaone.rmm_stale"
GROUP_NINJA_MACS = "ninjaone.macs.{name}"
#: Flag on: the identity layer's pairings for the site, loaded by the collector.
IDENTITY_LOADED = "identity.loaded"
#: Set per tool the identity layer actually holds a link for. A tool
#: with none has not been resolved yet — a connector added since the
#: last daily run — and its devices must go through the inline join
#: rather than being reported as a fleet-wide gap.
IDENTITY_TOOL = "identity.{tool}.has_links"
IDENTITY_PAIRED = "identity.{tool}.paired.{name}"
IDENTITY_UNMATCHED = "identity.{tool}.unmatched.{name}"
IDENTITY_MANUAL = "identity.{tool}.manual.{name}"
#: A link that stands whatever the names say: a person made it, or a pass that
#: tells twins apart (MAC, serial) did. A name pass cannot have.
IDENTITY_STANDS = "identity.{tool}.stands.{name}"
#: NinjaOne's patch waiting list per KB: the devices it is approved or manual on.
GROUP_NINJA_WAITING_KB = "ninjaone.patching.waiting.{kb}"

BITDEFENDER_GAP = "Bitdefender endpoint protection"
PATCH_COVERAGE_GAP = "NinjaOne patch coverage"

#: How far back an install counts as NinjaOne patching a device. A machine can go a
#: month without an install and still be under the policy; ninety days without one,
#: and with no row on the waiting list, and it is not being patched.
PATCH_COVERAGE_DAYS = 90
BITLOCKER_GAP = "NinjaOne BitLocker state"


def _group(facts: FactSet, key: str, names: set[str]) -> None:
    facts.groups[key] = sorted(name for name in names if name)


# --- NinjaOne: BitLocker --------------------------------------------------------------------------


async def collect_bitlocker(run: Collector) -> None:
    """Which devices have every local disk fully encrypted, as NinjaOne reports it."""
    async with connector_for(run.client_id, ConnectorName.NINJAONE, run.site_id) as c:
        assert isinstance(c, NinjaOneClient)

        async def read() -> list[tuple[Any, Any, dict[str, Any]]]:
            raw, _, _ = await c.query_scoped("volumes", params={"include": "bl"})
            rows = [item for item in raw if isinstance(item, dict)]
            return await c.place_by_device(
                rows,
                [row.get("deviceId") for row in rows],
                endpoint=QUERY_PATH.format(report="volumes"),
            )

        placed = await run.audit.read(
            "ninjaone.bitlocker",
            ConnectorName.NINJAONE,
            read,
            summarise=lambda r: {"rows": len(r)},
        )
    per_device: dict[str, list[str | None]] = {}
    for _, device, row in placed:
        if row.get("deviceType") != "Local Disk" or not row.get("capacity"):
            continue
        status = row.get("bitLockerStatus")
        conversion = status.get("conversionStatus") if isinstance(status, dict) else None
        per_device.setdefault(device.name, []).append(conversion)
    reporting = {name: states for name, states in per_device.items() if any(states)}
    encrypted = {
        n for n, states in reporting.items() if all(s == BITLOCKER_ENCRYPTED for s in states)
    }
    unencrypted = set(reporting) - encrypted
    run.fact(
        "ninjaone.bitlocker.devices_reporting",
        "Devices reporting BitLocker state",
        len(reporting),
        "Devices with at least one local disk whose BitLocker status NinjaOne reports "
        "(volumes query with include=bl).",
        "ninjaone",
    )
    run.fact(
        "ninjaone.bitlocker.devices_encrypted",
        "Devices with every local disk fully encrypted",
        len(encrypted),
        "Of the devices reporting BitLocker state, those whose every local disk has "
        "conversionStatus FULLY_ENCRYPTED.",
        "ninjaone",
    )
    run.fact(
        "ninjaone.bitlocker.devices_unencrypted",
        "Devices with an unencrypted local disk",
        len(unencrypted),
        "Devices reporting BitLocker state with at least one local disk not fully encrypted "
        "(decrypted, in progress, paused or unknown).",
        "ninjaone",
    )
    _group(run.facts, GROUP_BITLOCKER_ENCRYPTED, encrypted)
    _group(run.facts, GROUP_BITLOCKER_UNENCRYPTED, unencrypted)


# --- NinjaOne: which devices its patch management covers ------------------------------------------


async def collect_patch_coverage(run: Collector) -> None:
    """Devices under NinjaOne patch management, for the "patched by neither tool" rule.

    Checked live on 2026-09-22: in one August only 40 of 101 devices installed a
    patch, while 99 installed one within ninety days and 90 carried rows on the
    waiting list (approved or rejected). So a device is patched by NinjaOne when
    it installed anything in the last `PATCH_COVERAGE_DAYS` days or has any row
    on the waiting list, not only when the review period saw an install.
    """
    since = run.now - dt.timedelta(days=PATCH_COVERAGE_DAYS)
    async with connector_for(run.client_id, ConnectorName.NINJAONE, run.site_id) as c:
        assert isinstance(c, NinjaOneClient)

        async def read() -> dict[str, list[Any]]:
            found: dict[str, list[Any]] = {}
            for label, report, params in (
                (
                    "installs",
                    "os-patch-installs",
                    {
                        "installedAfter": f"{since:%Y-%m-%d}",
                        "installedBefore": f"{run.now + dt.timedelta(days=1):%Y-%m-%d}",
                    },
                ),
                # The waiting list honours exactly these values (api-notes/ninjaone.md).
                ("approved", "os-patches", {"status": "APPROVED"}),
                ("manual", "os-patches", {"status": "MANUAL"}),
                ("rejected", "os-patches", {"status": "REJECTED"}),
            ):
                raw, _, _ = await c.query_scoped(report, params=params)
                rows = [row for row in raw if isinstance(row, dict)]
                found[label] = await c.place_by_device(
                    rows,
                    [row.get("deviceId") for row in rows],
                    endpoint=QUERY_PATH.format(report=report),
                )
            return found

        placed = await run.audit.read(
            "ninjaone.patch_coverage",
            ConnectorName.NINJAONE,
            read,
            summarise=lambda r: {k: len(v) for k, v in r.items()},
        )
    covered = {device.name for rows in placed.values() for _, device, _ in rows if device.name}
    patched = set(run.facts.groups.get(GROUP_NINJA_PATCHING, [])) | covered
    run.facts.groups[GROUP_NINJA_PATCHING] = sorted(patched)
    # The waiting list per KB (approved or manual, not rejected): a vulnerability
    # whose fix is one of these KBs on that device is a fix awaiting install.
    waiting_by_kb: dict[str, set[str]] = {}
    for label in ("approved", "manual"):
        for _, device, row in placed.get(label, []):
            for kb in kb_numbers(str(row.get("kbNumber") or ""), str(row.get("name") or "")):
                if device.name:
                    waiting_by_kb.setdefault(kb, set()).add(device.name)
    for kb, devices in waiting_by_kb.items():
        run.facts.groups[GROUP_NINJA_WAITING_KB.format(kb=kb)] = sorted(devices)
    run.fact(
        "ninjaone.patching.devices_managed",
        "Devices under NinjaOne patch management",
        len(patched),
        f"Devices with a patch installation in the last {PATCH_COVERAGE_DAYS} days or any row "
        "on NinjaOne's patch waiting list (approved or rejected): the machines NinjaOne is "
        "patching, whether or not the review period saw an install.",
        "ninjaone",
    )


# --- Bitdefender: endpoints, modules, risk --------------------------------------------------------


async def collect_bitdefender_endpoints(run: Collector) -> None:
    async with connector_for(run.client_id, ConnectorName.BITDEFENDER, run.site_id) as c:
        assert isinstance(c, BitdefenderClient)
        rows, truncated = await run.audit.read(
            "bitdefender.endpoints",
            ConnectorName.BITDEFENDER,
            c.list_endpoints,
            summarise=lambda r: {"endpoints": len(r[0]), "truncated": r[1]},
        )
        companies = await run.audit.read(
            "bitdefender.company",
            ConnectorName.BITDEFENDER,
            c.list_companies,
            summarise=lambda r: {"companies": len(r)},
        )
        licences = await run.audit.read(
            "bitdefender.licence",
            ConnectorName.BITDEFENDER,
            c.licences,
            summarise=lambda r: {"companies": len(r)},
        )
        licence = next(iter(licences.values()), None)
    if truncated:
        run.facts.gaps.append(
            Gap(
                what=BITDEFENDER_GAP,
                why="The endpoint listing stopped at the page cap, so every count would be "
                "partial.",
                source="bitdefender",
            )
        )
        return
    endpoints: list[BdEndpoint] = [endpoint for _, endpoint in rows]
    run.facts.names["bitdefender_endpoints"] = sorted({e.name for e in endpoints if e.name})
    statuses = Counter(protection_status(e) for e in endpoints)
    with_agent = [e for e in endpoints if e.has_agent]
    run.fact(
        "bitdefender.endpoints.total",
        "Endpoints in Bitdefender GravityZone",
        len(endpoints),
        "Computers and virtual machines under this customer's GravityZone company, with or "
        "without the security agent.",
        "bitdefender",
    )
    run.fact(
        "bitdefender.endpoints.with_agent",
        "Endpoints with the Bitdefender agent",
        len(with_agent),
        "Endpoints GravityZone reports as managed with Bitdefender Endpoint Security Tools.",
        "bitdefender",
    )
    for fact_id, label, status, definition in (
        (
            "bitdefender.endpoints.no_agent",
            "Endpoints known to GravityZone with no agent",
            STATUS_NO_AGENT,
            "Endpoints GravityZone discovered (network scan, Active Directory, a removed agent) "
            "that have no Bitdefender agent installed.",
        ),
        (
            "bitdefender.endpoints.core_off",
            "Endpoints with core protection disabled",
            STATUS_CORE_OFF,
            "Endpoints with an agent whose antimalware or Advanced Threat Control module is "
            "disabled by policy.",
        ),
        (
            "bitdefender.endpoints.agent_outdated",
            "Endpoints whose agent is out of date",
            STATUS_OUTDATED,
            "Endpoints with an agent that GravityZone flags as missing product updates "
            "(productOutdated), core protection being on.",
        ),
    ):
        run.fact(fact_id, label, statuses.get(status, 0), definition, "bitdefender")
    if endpoints:
        protected = statuses.get("protected", 0)
        run.fact(
            "bitdefender.endpoints.protected_percent",
            "Share of endpoints fully protected",
            round(100 * protected / len(endpoints), 1),
            "Endpoints with an agent that is current and has core protection on, as a "
            "percentage of every endpoint GravityZone knows for this customer.",
            "bitdefender",
            display=f"{round(100 * protected / len(endpoints), 1):g}%",
            denominator="bitdefender.endpoints.total",
        )
    # The licence first: which modules this company can have at all. A module the
    # licence lacks is `not_licensed` on every endpoint, whatever the rows say.
    if licence is not None:
        for module, flag in LICENSED_MODULES.items():
            value = licence.flag(flag)
            if value is None:
                continue
            run.fact(
                f"bitdefender.licence.{module}",
                f"{module} licensed for this customer",
                "yes" if value else "no",
                f"GravityZone's {flag} flag for the company's licence. When it is no, the "
                f"{module} module cannot be enabled on any endpoint, and its being off is a "
                "licence matter, not a policy choice.",
                "bitdefender",
            )
        run.fact(
            "bitdefender.licence.edr",
            "EDR licensed for this customer",
            "yes" if licence.edr_licensed else "no" if licence.edr_licensed is False else "unknown",
            "GravityZone's manageEventCorrelator flag: whether incidents (detections) can exist "
            "for this company.",
            "bitdefender",
        )
    # The module list is the tenant's own (what the rows report), never a fixed list;
    # a licence-gated module absent from every row is still counted, as not licensed.
    modules = modules_seen(with_agent)
    for module, flag in LICENSED_MODULES.items():
        if module not in modules and licence is not None and licence.flag(flag) is False:
            modules.append(module)
    for module in modules:
        states = Counter(module_status(e, module, licence) for e in with_agent)
        workload = (
            " It protects a workload not every machine has." if module in WORKLOAD_MODULES else ""
        )
        run.fact(
            f"bitdefender.modules.{module}.enabled",
            f"Endpoints with the {module} module enabled",
            states["enabled"],
            f"Endpoints with an agent whose {module} module GravityZone reports as enabled."
            f"{workload}",
            "bitdefender",
        )
        run.fact(
            f"bitdefender.modules.{module}.disabled",
            f"Endpoints with the {module} module disabled",
            states["disabled"],
            f"Endpoints with an agent whose {module} module is licensed for the company and "
            f"reported disabled: off by policy, not by licence.{workload}",
            "bitdefender",
        )
        if states["not_licensed"]:
            run.fact(
                f"bitdefender.modules.{module}.not_licensed",
                f"Endpoints without a {module} licence",
                states["not_licensed"],
                f"Endpoints with an agent whose company licence does not include {module}, so "
                "the module cannot be on. Distinct from disabled.",
                "bitdefender",
            )
        _group(
            run.facts,
            GROUP_BD_MODULE_OFF.format(module=module),
            {e.name or "" for e in with_agent if module_status(e, module, licence) == "disabled"},
        )
        _group(
            run.facts,
            GROUP_BD_MODULE_UNLICENSED.format(module=module),
            {
                e.name or ""
                for e in with_agent
                if module_status(e, module, licence) == "not_licensed"
            },
        )
        _group(
            run.facts,
            GROUP_BD_MODULE_ON.format(module=module),
            {e.name or "" for e in with_agent if module_status(e, module, licence) == "enabled"},
        )
    _group(run.facts, GROUP_BD_ENDPOINTS, {e.name or "" for e in endpoints})
    judged = bitdefender_endpoints(rows)
    _group(run.facts, GROUP_BD_TOOL_STALE, {e.name or "" for e in judged if e.state == "stale"})
    _group(run.facts, GROUP_BD_TOOL_ABSENT, {e.name or "" for e in judged if e.state == "absent"})
    _group(run.facts, GROUP_BD_TOOL_DISCOVERED, {e.name or "" for e in judged if e.discovered})
    for judged_endpoint in judged:
        if judged_endpoint.name and judged_endpoint.macs:
            run.facts.groups[GROUP_BD_MACS.format(name=judged_endpoint.name)] = sorted(
                judged_endpoint.macs
            )
        # The bulk scan date (includeScanLogs) is the first evidence of life; the
        # detail read adds lastSeen only where it is missing or old.
        if judged_endpoint.name and judged_endpoint.last_seen is not None:
            run.facts.groups[GROUP_BD_LAST_SEEN.format(name=judged_endpoint.name)] = [
                judged_endpoint.last_seen.isoformat()
            ]
    record_rows(run.facts, GROUP_BD_ROWS, judged)
    _group(
        run.facts,
        GROUP_BD_NO_AGENT,
        {e.name or "" for e in endpoints if protection_status(e) == STATUS_NO_AGENT},
    )
    _group(
        run.facts,
        GROUP_BD_CORE_OFF,
        {e.name or "" for e in endpoints if protection_status(e) == STATUS_CORE_OFF},
    )
    _group(
        run.facts,
        GROUP_BD_OUTDATED,
        {e.name or "" for e in endpoints if protection_status(e) == STATUS_OUTDATED},
    )
    try:
        await _bitdefender_liveness(run, rows)
    except ConnectorError as exc:
        # The endpoints are recorded; only the dates are missing. The lost devices
        # they were for are not assessed for this tool, never called lost or alive.
        run.facts.groups[GROUP_BD_LAST_SEEN_FAILED] = ["yes"]
        status = f"HTTP {exc.status_code}" if exc.status_code else type(exc).__name__
        run.facts.gaps.append(
            Gap(
                what="Bitdefender endpoint last-seen",
                why=f"{status}: {exc}"[:300],
                source="bitdefender",
            )
        )
    unprotected = [(e, unprotected_reasons(e)) for e in endpoints if unprotected_reasons(e)]
    if unprotected:
        run.facts.tables["bitdefender.endpoints.unprotected.devices"] = FactTable(
            id="bitdefender.endpoints.unprotected.devices",
            label="Endpoints without working core protection",
            definition="Every endpoint counted in bitdefender.endpoints.no_agent, .core_off and "
            ".agent_outdated, with the reason.",
            source="bitdefender",
            columns=["Device", "Why"],
            rows=[[e.name or e.id, ", ".join(reasons)] for e, reasons in unprotected],
            total=len(unprotected),
            as_of=run.now,
        )
    for _, company in companies:
        if company.risk_score is None or company.risk_score.value is None:
            continue
        run.fact(
            "bitdefender.company.risk_score",
            "GravityZone company risk score",
            company.risk_score.value,
            "Bitdefender's risk score for the company, in percent, from endpoint "
            "misconfigurations, application vulnerabilities and human risks, adjusted by the "
            "industry modifier. Higher is worse.",
            "bitdefender",
            display=f"{company.risk_score.value:g}%",
        )
        run.fact(
            "bitdefender.company.risk_impact",
            "GravityZone risk impact",
            company.risk_score.impact or "unknown",
            "Bitdefender's banding of the company risk score: Low, Medium or High.",
            "bitdefender",
        )
        break


async def collect_bitdefender_incidents(run: Collector) -> None:
    """Detections created in the period, and how many were blocked."""
    async with connector_for(run.client_id, ConnectorName.BITDEFENDER, run.site_id) as c:
        assert isinstance(c, BitdefenderClient)
        licences = await c.licences()
        if any(lic.edr_licensed is False for lic in licences.values()):
            run.facts.gaps.append(
                Gap(
                    what="Bitdefender detections",
                    why="EDR is not licensed for this customer in GravityZone "
                    "(manageEventCorrelator is false), so no detections can exist. Not a "
                    "quiet period: a count of zero would be wrong.",
                    source="bitdefender",
                )
            )
            return
        rows, truncated = await run.audit.read(
            "bitdefender.incidents",
            ConnectorName.BITDEFENDER,
            lambda: c.list_incidents(since=run.period.start, until=run.now),
            summarise=lambda r: {"incidents": len(r[0]), "truncated": r[1]},
        )
    if truncated:
        run.facts.gaps.append(
            Gap(
                what="Bitdefender detections",
                why="The incident listing stopped at the page cap, so the count would be partial.",
                source="bitdefender",
            )
        )
        return
    in_period = [
        incident
        for _, incident in rows
        if incident.created is not None and run.period.start <= incident.created < run.period.end
    ]
    blocked = [i for i in in_period if i.main_action == "blocked"]
    reported = [i for i in in_period if i.main_action != "blocked"]
    run.fact(
        "bitdefender.incidents.created",
        "Detections raised by Bitdefender in the period",
        len(in_period),
        "GravityZone incidents (EDR/XDR detections) whose creation time is inside the review "
        "period. Needs a licence with incidents; without one the count is zero.",
        "bitdefender",
        period_bound=True,
    )
    run.fact(
        "bitdefender.incidents.blocked",
        "Detections blocked automatically",
        len(blocked),
        "Of the period's detections, those GravityZone's prevention modules blocked.",
        "bitdefender",
        period_bound=True,
    )
    run.fact(
        "bitdefender.incidents.reported",
        "Detections reported for investigation",
        len(reported),
        "Of the period's detections, those only reported or partially blocked: no automatic "
        "action stopped them.",
        "bitdefender",
        period_bound=True,
    )
    run.fact(
        "bitdefender.incidents.open",
        "Detections still open",
        sum(1 for _, i in rows if i.status in ("open", "in_progress")),
        "Incidents with status open or in progress at the time the report was generated.",
        "bitdefender",
    )


# --- the cross-tool join --------------------------------------------------------------------------


def record_rows(facts: FactSet, group: str, judged: Sequence[ToolEndpoint]) -> None:
    """`{group}.{name}` -> `id|mac,mac|serial` per row, for every name more than
    one row carries. The review otherwise holds a tool's inventory as a set of
    names, which is one machine per name; the board splits same-named rows by
    their MACs and serials, and so must the review's join."""
    by_name: dict[str, list[ToolEndpoint]] = defaultdict(list)
    for endpoint in judged:
        if endpoint.name and endpoint.external_id:
            by_name[endpoint.name].append(endpoint)
    for name, rows in by_name.items():
        if len(rows) > 1:
            facts.groups[group.format(name=name)] = sorted(
                f"{r.external_id}|{','.join(sorted(r.macs))}|{r.serial or ''}" for r in rows
            )


def expand_rows(template: ToolEndpoint, recorded: Sequence[str]) -> list[ToolEndpoint]:
    """The rows `record_rows` kept for one name, as endpoints like `template`
    (same site, verdict and flags) with the row's own id, MACs and serial."""
    found: list[ToolEndpoint] = []
    for entry in recorded:
        external_id, macs, serial = [*entry.split("|"), "", ""][:3]
        found.append(
            replace(
                template,
                external_id=external_id,
                macs=frozenset(m for m in macs.split(",") if m),
                serial=serial or None,
            )
        )
    return found


async def _bitdefender_liveness(run: Collector, rows: Sequence[tuple[Any, BdEndpoint]]) -> None:
    """`lastSeen` for the GravityZone rows the join pairs with devices NinjaOne has
    lost, as the board reads it: the join once on the recorded names, the detail
    read for those rows only, the dates recorded for the derivation's second run.
    Without NinjaOne's computers there is nothing to pair; without the dates a
    lost device is `rmm_stale`, never `rmm_agent_down`."""
    if GROUP_NINJA_COMPUTERS not in run.facts.groups:
        return
    groups = run.facts.groups
    shape = dict(
        tool="bitdefender",
        tool_names=groups.get(GROUP_BD_ENDPOINTS, []),
        stale_names=groups.get(GROUP_BD_TOOL_STALE, []),
        absent_names=groups.get(GROUP_BD_TOOL_ABSENT, []),
        discovered_names=groups.get(GROUP_BD_TOOL_DISCOVERED, []),
        mac_group=GROUP_BD_MACS,
        rows_group=GROUP_BD_ROWS,
        last_seen_group=GROUP_BD_LAST_SEEN,
    )
    devices, inventory, _ = review_inventory(run, **shape)  # type: ignore[arg-type]
    first = assess(
        devices, {"bitdefender": inventory}, now=run.now, ninjaone_stale_days=rmm_stale_days()
    )
    # The review's rows are keyed by the vendor id where several rows share the
    # name and by the name where one row carries it; the vendor's ids and refs
    # come from the rows just read, so the read and the links see vendor ids.
    id_of: dict[str, str] = {}
    ref_of: dict[str, Any] = {}
    for site, endpoint in rows:
        if not endpoint.id:
            continue
        id_of.setdefault(endpoint.id, endpoint.id)
        if endpoint.name:
            id_of.setdefault(endpoint.name, endpoint.id)
        ref_of[endpoint.id] = site
    rows_by_id = {
        id_of[key]: replace(e, external_id=id_of[key])
        for e in inventory.endpoints
        if (key := e.external_id) is not None and key in id_of
    }
    lost_days = {
        name: int(days[0])
        for name in groups.get(GROUP_NINJA_RMM_STALE, [])
        if (days := groups.get(GROUP_NINJA_LOST_DAYS.format(name=name)))
    }
    reads = LastSeenReads()
    candidates = candidates_for(
        _with_ids(first, id_of),
        rows_by_id=rows_by_id,
        refs=ref_of,
        devices_by_id={d.device_id: d for d in devices},
        now=run.now,
        threshold_days=rmm_stale_days(),
        reads=reads,
        lost_days_by_device=lost_days,
    )
    if not candidates:
        for name in reads.unchecked_devices:
            groups.setdefault(GROUP_BD_UNCHECKED, []).append(name)
        return
    seen = await run.audit.read(
        "bitdefender.endpoint_details",
        ConnectorName.BITDEFENDER,
        lambda: read_last_seen(uuid.UUID(run.client_id), candidates, now=run.now, reads=reads),
        summarise=lambda r: {**reads.as_dict(), "dated": sum(1 for v in r.values() if v)},
    )
    if reads.failed:
        raise UpstreamError(
            "GravityZone did not answer the endpoint detail reads",
            connector=ConnectorName.BITDEFENDER.value,
        )
    for key, endpoint_id in id_of.items():
        when = seen.get(endpoint_id)
        if when is not None:
            groups[GROUP_BD_LAST_SEEN.format(name=key)] = [when.isoformat()]
    for name in reads.unchecked_devices:
        groups.setdefault(GROUP_BD_UNCHECKED, []).append(name)


def _with_ids(assessment: Assessment, id_of: Mapping[str, str]) -> Assessment:
    """The same assessment with the verdicts' endpoint keys turned into vendor ids."""
    devices = []
    for outcome in assessment.devices:
        tools = {
            tool: replace(v, endpoint_ids=tuple(id_of.get(k, k) for k in v.endpoint_ids))
            for tool, v in outcome.tools.items()
        }
        devices.append(
            DeviceCoverage(
                device=outcome.device,
                tools=tools,
                ninjaone_state=outcome.ninjaone_state,
                ninjaone_detail=outcome.ninjaone_detail,
            )
        )
    return Assessment(devices=devices, unmanaged=[], discovered=[])


def assess_review_site(
    run: Collector,
    *,
    tool: str,
    tool_names: Sequence[str],
    stale_names: Sequence[str],
    absent_names: Sequence[str],
    discovered_names: Sequence[str] = (),
    probe_names: Sequence[str] = (),
    mac_group: str = "",
    last_seen_group: str = "",
    rows_group: str = "",
    undated_group: str = "",
) -> Assessment:
    """The board's join, run on one site's collected names (`review_inventory`).

    The review holds names rather than vendor rows by the time it derives, so
    the tool's endpoints are rebuilt from the name groups the collectors
    recorded with the board's own per-row verdicts (`GROUP_*_TOOL_STALE`,
    `GROUP_*_TOOL_ABSENT`, `..._DISCOVERED`) and MACs (`mac_group`), and the
    devices from `GROUP_NINJA_COMPUTERS` (NinjaOne's hostnames: the full
    `dnsName`, else `systemName`) with their MACs and whether the RMM has
    lost them (`GROUP_NINJA_RMM_STALE`). The same passes run, without serials,
    which the review does not collect; an RMM-stale device is `rmm_stale` or
    `rmm_agent_down` on the deck as on the board; a scanner name that is an
    address or an ISP's name is external here as there. The site key is the
    review's site slug; the join never crosses it because there is only one.
    """
    groups = run.facts.groups
    devices, inventory, lost = review_inventory(
        run,
        tool=tool,
        tool_names=tool_names,
        stale_names=stale_names,
        absent_names=absent_names,
        discovered_names=discovered_names,
        probe_names=probe_names,
        mac_group=mac_group,
        last_seen_group=last_seen_group,
        rows_group=rows_group,
    )
    undated = bool(undated_group and groups.get(undated_group))
    # Only when the identity layer has actually resolved this tool. A tool
    # it has never seen has no pairings, and reading that as 'nothing is
    # paired' would report every machine as a gap (which is what adding
    # Cove did until this).
    if groups.get(IDENTITY_LOADED) and groups.get(IDENTITY_TOOL.format(tool=tool)):
        return assess_from_groups(run, devices, tool, inventory, lost=lost, dated=not undated)
    result = assess(
        devices,
        {tool: inventory},
        now=run.now,
        ninjaone_stale_days=rmm_stale_days(),
        undated_tools=(tool,) if undated else (),
    )
    if tool == "bitdefender":
        mark_unchecked(
            result, tool, groups.get(GROUP_BD_UNCHECKED, []), get_settings().device_retire_days
        )
    return result


def review_inventory(
    run: Collector,
    *,
    tool: str,
    tool_names: Sequence[str],
    stale_names: Sequence[str],
    absent_names: Sequence[str],
    discovered_names: Sequence[str] = (),
    probe_names: Sequence[str] = (),
    mac_group: str = "",
    last_seen_group: str = "",
    rows_group: str = "",
) -> tuple[list[ManagedDevice], ToolInventory, set[str]]:
    """One site's devices and one tool's rows, rebuilt from the recorded groups:
    what the join and the last-seen read both work on. Returns the devices, the
    inventory and the names of the devices NinjaOne has lost."""
    site = run.site_id
    groups = run.facts.groups
    lost = set(groups.get(GROUP_NINJA_RMM_STALE, []))
    devices = [
        ManagedDevice(
            site_id=site,
            site_slug=site,
            site_name=site,
            device_id=name,
            name=name,
            # The board decides rmm_stale from last contact; here it was decided
            # at collection with the same threshold, so a lost device gets no
            # contact and a kept one gets now.
            last_contact=None if name in lost else run.now,
            macs=frozenset(groups.get(GROUP_NINJA_MACS.format(name=name), [])),
        )
        for name in groups[GROUP_NINJA_COMPUTERS]
    ]
    stale, absent = set(stale_names), set(absent_names)
    discovered, probes = set(discovered_names), set(probe_names)

    def seen(name: str) -> dt.datetime | None:
        if not last_seen_group:
            return None
        recorded = groups.get(last_seen_group.format(name=name))
        return dt.datetime.fromisoformat(recorded[0]) if recorded else None

    endpoints: list[ToolEndpoint] = []
    for name in tool_names:
        template = ToolEndpoint(
            site_id=site,
            name=name,
            state="absent" if name in absent else "stale" if name in stale else "covered",
            detail="",
            external_id=name,
            macs=frozenset(groups.get(mac_group.format(name=name), []))
            if mac_group
            else frozenset(),
            discovered="discovered" if name in discovered else None,
            probe=name in probes,
            external=external_target(name),
            last_seen=seen(name),
        )
        recorded = groups.get(rows_group.format(name=name)) if rows_group else None
        if recorded:
            for row in expand_rows(template, recorded):
                endpoints.append(replace(row, last_seen=seen(row.external_id or name)))
        else:
            endpoints.append(template)
    return devices, ToolInventory(endpoints=endpoints, sites=frozenset({site})), lost


def assess_from_groups(
    run: Collector,
    devices: Sequence[ManagedDevice],
    tool: str,
    inventory: ToolInventory,
    *,
    lost: set[str],
    dated: bool = True,
) -> Assessment:
    """Flag on: the same assessment from the identity layer's pairings.

    A device's endpoints are the names its links carry (`IDENTITY_PAIRED`),
    its verdict the best of theirs; an open queue entry naming it is
    `unmatched` with the candidates; a device with neither is `absent`. What
    no device's links name is unmanaged, discovered or nothing, as the board
    decides it.
    """
    groups = run.facts.groups
    by_name: dict[str, list[ToolEndpoint]] = {}
    for endpoint in inventory.endpoints:
        if endpoint.external is None:
            by_name.setdefault(endpoint.name or "", []).append(endpoint)
    # The join's two refusals hold here too: no hostname, or two managed devices
    # sharing one, is unmatched unless a person linked the record by hand or a
    # pass that tells twins apart did (`IDENTITY_STANDS`).
    names = Counter(d.key for d in devices if d.key)
    keys_in_tool = {e.key for e in inventory.endpoints if e.pairable}
    claimed: set[str] = set()
    results: list[DeviceCoverage] = []
    for device in devices:
        state, detail = ninjaone_state(device, now=run.now, stale_days=rmm_stale_days())
        result = DeviceCoverage(device=device, ninjaone_state=state, ninjaone_detail=detail)
        paired = [
            e
            for name in groups.get(IDENTITY_PAIRED.format(tool=tool, name=device.name), [])
            for e in by_name.get(name, [])
        ]
        queued = groups.get(IDENTITY_UNMATCHED.format(tool=tool, name=device.name), [])
        stands = bool(groups.get(IDENTITY_MANUAL.format(tool=tool, name=device.name))) or bool(
            groups.get(IDENTITY_STANDS.format(tool=tool, name=device.name))
        )
        # A twin is unmatched only while the tool holds a row of the shared name,
        # as in the join; with nothing to fight over it is absent like any other.
        twin = bool(device.key) and names[device.key] > 1 and device.key in keys_in_tool
        verdict: ToolState
        if paired and (stands or (device.key and not twin)):
            best = min(paired, key=lambda e: ("covered", "stale", "absent").index(e.state))
            claimed.update(e.name or "" for e in paired)
            verdict = ToolState(best.state, best.detail, matched_by="identity")
        elif not device.key:
            paired = []
            verdict = (
                ToolState("rmm_stale", f"no NinjaOne contact in {rmm_stale_days()} days")
                if device.name in lost
                else ToolState("unmatched", "NinjaOne gives the device no hostname")
            )
        elif twin:
            paired = []
            verdict = ToolState(
                "unmatched",
                "two managed devices at this site share the hostname",
                found_by="exact",
            )
        elif queued:
            paired = []
            verdict = ToolState("unmatched", "more than one candidate", candidates=tuple(queued))
        else:
            paired = []
            verdict = ToolState("absent", "no endpoint linked to this device")
        if device.name in lost and device.key:
            verdict = lost_state(
                verdict,
                paired,
                tool=tool,
                now=run.now,
                stale_days=rmm_stale_days(),
                dated=dated,
            )
        result.tools[tool] = verdict
        results.append(result)
    unmanaged: list[Unmanaged] = []
    discovered: list[Unmanaged] = []
    external: list[Unmanaged] = []
    for rows in group_machines(inventory.endpoints).values():
        if any((e.name or "") in claimed for e in rows):
            continue
        entry = unmanaged_entry(tool, rows)
        best = rows[0]
        if best.external:
            external.append(entry)
        elif best.discovered or best.probe:
            discovered.append(entry)
        else:
            unmanaged.append(entry)
    return Assessment(
        devices=results, unmanaged=unmanaged, discovered=discovered, external=external
    )


def derive_coverage(run: Collector) -> None:
    """NinjaOne computers against Bitdefender endpoints, by hostname within the site.

    The join is the coverage board's (`app/coverage/core.py`), fed the same
    population and the same per-endpoint verdicts, so the deck's "no
    protection agent" count is the board's `absent` count for Bitdefender.
    Only when both tools answered: the count means nothing if Bitdefender did
    not answer or the site has no Bitdefender company.
    """
    facts = run.facts
    if (
        facts.get("bitdefender.endpoints.total") is None
        or GROUP_NINJA_COMPUTERS not in facts.groups
    ):
        return
    result = assess_review_site(
        run,
        tool="bitdefender",
        tool_names=facts.groups.get(GROUP_BD_ENDPOINTS, []),
        stale_names=facts.groups.get(GROUP_BD_TOOL_STALE, []),
        absent_names=facts.groups.get(GROUP_BD_TOOL_ABSENT, []),
        discovered_names=facts.groups.get(GROUP_BD_TOOL_DISCOVERED, []),
        mac_group=GROUP_BD_MACS,
        last_seen_group=GROUP_BD_LAST_SEEN,
        rows_group=GROUP_BD_ROWS,
        undated_group=GROUP_BD_LAST_SEEN_FAILED,
    )
    matched = [
        d.device.name
        for d in result.devices
        if d.tools["bitdefender"].state in ("covered", "stale")
    ]
    ninja_only = [d.device.name for d in result.devices if d.tools["bitdefender"].state == "absent"]
    bd_only = [u.name for u in result.unmanaged if u.tool == "bitdefender"]
    agent_down = [
        d.device.name for d in result.devices if d.tools["bitdefender"].state == "rmm_agent_down"
    ]
    for fact_id, label, value, definition in (
        (
            "coverage.matched",
            "Computers known to both NinjaOne and Bitdefender",
            len(matched),
            "NinjaOne workstations, servers and VM guests whose hostname (case-insensitive, "
            "domain suffix stripped) matches a Bitdefender endpoint of the same customer that "
            "has the agent installed.",
        ),
        (
            "coverage.ninjaone_only",
            "Computers in NinjaOne with no protection agent",
            len(ninja_only),
            "NinjaOne workstations, servers and VM guests with no Bitdefender endpoint paired "
            "by hostname, NetBIOS name or MAC within the site, or one GravityZone discovered "
            "with no agent on it: no protection agent, as far as either tool can see. Network "
            "devices, phones, hypervisor hosts and devices NinjaOne itself has not heard from "
            f"in {rmm_stale_days()} days are not counted. The same count the coverage board "
            "shows as absent.",
        ),
        (
            "coverage.bitdefender_only",
            "Bitdefender endpoints not in NinjaOne",
            len(bd_only),
            "Bitdefender endpoints with no NinjaOne device of the same hostname, MAC or "
            "serial. A default Windows name (DESKTOP-XXXXXXX) is noted as likely not yet "
            "onboarded on the coverage board.",
        ),
        (
            "coverage.rmm_agent_down",
            "Computers alive per Bitdefender that NinjaOne is not hearing from",
            len(agent_down),
            "NinjaOne computers with no NinjaOne contact in "
            f"{rmm_stale_days()} days whose Bitdefender endpoint is current: the machine is "
            "alive and the RMM agent is down. A finding about the RMM agent, not a "
            "protection gap; the same devices the coverage board shows as rmm_agent_down.",
        ),
    ):
        facts.add(
            Fact(
                id=fact_id,
                label=label,
                value=value,
                display=f"{value:,}",
                definition=definition,
                source="derived",
                as_of=run.now,
            )
        )
    _group(facts, GROUP_COVERAGE_MATCHED, set(matched))
    _group(facts, GROUP_COVERAGE_NINJA_ONLY, set(ninja_only))
    _group(facts, GROUP_COVERAGE_BD_ONLY, set(bd_only))
    if ninja_only:
        facts.tables["coverage.ninjaone_only.devices"] = FactTable(
            id="coverage.ninjaone_only.devices",
            label="NinjaOne computers with no Bitdefender endpoint",
            definition="See coverage.ninjaone_only.",
            source="derived",
            columns=["Device"],
            rows=[[name] for name in sorted(ninja_only)],
            total=len(ninja_only),
            as_of=run.now,
        )

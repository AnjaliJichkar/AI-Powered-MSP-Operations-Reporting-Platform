"""The coverage board as two chat tools: one that counts, one that names.

Both read `compute_coverage`, so the answer in Slack is the board on the
page and the join on the deck. `count_coverage` is small whatever the
fleet; `list_coverage_gaps` names devices and is capped like every list
tool, with the counts computed before the cap.

Registered under NinjaOne: the population is NinjaOne's managed computers,
and a tool needs a home connector for the registry's own bookkeeping. The
connectors package imports this module to register the tools, and the
computation imports the connectors, so `compute` is imported when a tool
runs rather than when the module loads.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from app.agent.registry import tool
from app.connectors.results import RowCapped, SiteTagged, capped
from app.coverage.config import COVERAGE_TOOLS
from app.coverage.core import GAP_STATES, STATES
from app.models.enums import ConnectorName

if TYPE_CHECKING:
    from app.coverage.compute import CoverageReport

CoverageGroupBy = Literal["site", "tool", "state"]
GapState = Literal["absent", "stale", "unmatched", "unmanaged", "rmm_agent_down"]
BackupRole = Literal["server", "workstation"]

#: The coverage board's key for the backup tool, in one place.
BACKUP = "backup"


class CoverageCounts(BaseModel):
    client_id: str
    site: str | None = Field(default=None, description="The site slug asked about, if one.")
    managed: int = Field(description="NinjaOne managed computers in scope: the population.")
    not_computers: int = Field(
        description="NinjaOne devices left out: network gear, phones, hypervisor hosts."
    )
    unmanaged: int = Field(
        description="Endpoints a tool holds with no managed computer of that hostname at the site."
    )
    group_by: CoverageGroupBy
    counts: dict[str, dict[str, int | float | None]] = Field(
        description=(
            "By site: managed, unmanaged and covered_percent per tool. By tool: every state's "
            "count, covered_percent and whether the tool was assessed. By state: per tool, how "
            "many devices are in each state."
        )
    )
    not_assessed: dict[str, str] = Field(
        description="Tools that were not assessed for this client, and why."
    )
    states: list[str] = Field(description="Every state a device can be in, for the wording.")
    computed_at: dt.datetime
    truncated: bool = Field(
        description="True when a vendor listing stopped at its page cap: counts are lower bounds."
    )


@dataclass(frozen=True, slots=True)
class _Role:
    role: str
    basis: str


async def _backup_population(
    client_id: str, report: CoverageReport
) -> tuple[list[tuple[Any, Any, _Role]], dict[str, str], int]:
    """The machines Cove holds nothing for that their customer expects it to.

    Three things decide a row and all three are somebody else's rule: the
    board's own join says which machines Cove has no device for, the
    franchisee's settings say which machines each customer expects to be
    backed up (`effective_scope`, which the review's gap list applies), and
    NinjaOne's role decision says whether a machine is a server. Nothing here
    joins anything itself, so this answer, the deck's and the ticket page's
    cannot be three views of one laptop.

    **A role is decided from the node class alone**, as the board's is.
    `Device.os` is not an OS name for a machine NinjaOne found: the
    org-scoped device listing carries no OS string, so the identity layer
    stores a *family* there — `"Windows"` — and `device_role` reads
    `"windows"` without `"windows server"` as a workstation. Every Windows
    server read that way was a workstation, which at a servers-only site is
    the opposite of right. The OS name lives in NinjaOne's own
    `operating-systems` read, which the review makes and this does not.
    """
    from app.connectors.cove.client import CoveClient
    from app.connectors.cove.columns import device_key
    from app.connectors.factory import connector_for
    from app.connectors.ninjaone.hardware import device_role
    from app.db.session import session_scope
    from app.models.client import Client
    from app.models.enums import ConnectorName
    from app.reports.tbr.backup import effective_scope, is_not_a_machine
    from app.reports.tbr.config import load_config

    async with session_scope() as session:
        client = await session.get(Client, uuid.UUID(client_id))
        config = await load_config(
            session, uuid.UUID(client_id), client_name=client.name if client else ""
        )
        settings = config.backup

    # One read per site the board covers: `auto` is read from what Cove already
    # holds there, so a site with no Cove customer cannot be scoped at all.
    by_site: dict[str, str] = {}
    machines: list[tuple[Any, Any, _Role]] = []
    left_out = 0
    for summary in report.sites:
        site_devices = [d for d in report.devices if d.device.site_id == summary.site_id]
        gaps = [
            (d, d.tools[BACKUP])
            for d in site_devices
            if BACKUP in d.tools and d.tools[BACKUP].state == "absent"
        ]
        if not gaps:
            continue
        try:
            async with connector_for(client_id, ConnectorName.COVE, str(summary.site_id)) as cove:
                assert isinstance(cove, CoveClient)
                held = [
                    device
                    for _ref, devices in await cove.list_devices_for_targets()
                    for device in devices
                    if device.is_machine
                ]
        except Exception as exc:
            by_site[summary.slug] = f"not assessed: {exc}"
            continue
        # Every workstation at the site, not only the ones with a gap: that is
        # the denominator `auto` asks about — how many of them are backed up,
        # rather than whether any is.
        site_workstations = sum(
            1 for d in site_devices if device_role(None, d.device.node_class).role != "server"
        )
        decision = effective_scope(
            settings.scope_for(summary.slug),
            machines=held,
            workstations=site_workstations,
        )
        scope, why = decision.scope, decision.why
        # Said in the answer, because the model is about to report a gap list
        # that this guess has shortened: a site where some workstations are
        # backed up and most are not is one somebody should settle.
        by_site[summary.slug] = (
            f"{why} Mixed: confirm this site's backup scope." if decision.mixed else why
        )
        excluded = settings.excluded_for(summary.slug)
        for d, verdict in gaps:
            name = d.device.name
            decided = device_role(None, d.device.node_class)
            if is_not_a_machine(name) or device_key(name) in excluded:
                left_out += 1
                continue
            if scope == "servers" and decided.role != "server":
                left_out += 1
                continue
            machines.append((d, verdict, _Role(decided.role, decided.basis)))
    return machines, by_site, left_out


async def _report(client_id: str, site_id: str | None) -> CoverageReport:
    from app.coverage.compute import compute_coverage

    return _in_site(await compute_coverage(uuid.UUID(client_id)), site_id)


def _in_site(report: CoverageReport, site_id: str | None) -> CoverageReport:
    from app.coverage.compute import CoverageReport

    if site_id is None:
        return report
    wanted = uuid.UUID(site_id)
    return CoverageReport(
        client_id=report.client_id,
        computed_at=report.computed_at,
        duration_ms=report.duration_ms,
        tools=report.tools,
        devices=[d for d in report.devices if d.device.site_id == wanted],
        unmanaged=[u for u in report.unmanaged if u.site_id == wanted],
        sites=[s for s in report.sites if s.site_id == wanted],
        exclusions=report.exclusions,
        not_computers=report.not_computers,
        population_truncated=report.population_truncated,
    )


@tool(ConnectorName.NINJAONE)
async def count_coverage(
    client_id: str, group_by: CoverageGroupBy = "site", site_id: str | None = None
) -> CoverageCounts:
    """Count how many managed computers each security tool covers, by site, tool or state.

    Use this for any "how many" or "what share" question about coverage across
    tools: "how many of Acme's computers have no Bitdefender agent", "what
    percentage of the fleet is vulnerability-scanned", "which sites have the
    worst coverage", "how many devices are stale in ConnectSecure". The
    population is NinjaOne's managed computers; each is matched to every
    tool's inventory by hostname within its site and given one state per
    tool: covered, stale (the tool has it but it is not current), absent (the
    tool does not have it, or has it with no agent), unmatched (no hostname
    to match on, or two candidates nothing tells apart), rmm_stale (NinjaOne
    has not heard from it and neither has the tool), rmm_agent_down (NinjaOne
    has not heard from it but the tool has: alive, RMM agent down), excluded
    (a person recorded it is not expected to have the tool, with a reason),
    or not_assessed (the tool is not connected, or has no company for the
    site; Backup until Cove exists). `unmanaged` is the reverse: endpoints a
    tool holds with no managed computer of that name, MAC or serial.

    `group_by`: "site" (managed, unmanaged and covered percent per tool, worst
    site first); "tool" (every state's count and covered percent per tool);
    "state" (per tool, how many devices are in each state). Pass `site` to
    count one end customer. Numbers only, so it is always small.

    Not for naming the machines: use `list_coverage_gaps`. Not for one
    tool's own inventory on its own terms (`count_endpoints`,
    `list_scan_coverage`, `count_devices`).
    """
    report = await _report(client_id, site_id)
    totals = report.totals()
    counts: dict[str, dict[str, int | float | None]] = {}
    if group_by == "site":
        for site in report.sites:
            counts[site.slug] = {
                "managed": site.managed,
                "unmanaged": site.unmanaged,
                "gaps": site.gaps,
                **{
                    f"{key}_covered_percent": tool.covered_percent
                    for key, tool in site.tools.items()
                },
            }
    elif group_by == "tool":
        for key, tool in totals.items():
            counts[key] = {**tool.as_dict(), "assessed": int(report.tool(key).assessed)}
    else:
        for state in STATES:
            counts[state] = {key: tool[state] for key, tool in totals.items()}
    return CoverageCounts(
        client_id=client_id,
        site=report.sites[0].slug if site_id and report.sites else None,
        managed=len(report.devices),
        not_computers=report.not_computers,
        unmanaged=len(report.unmanaged),
        group_by=group_by,
        counts=counts,
        not_assessed={t.key: t.reason for t in report.tools if not t.assessed},
        states=list(STATES),
        computed_at=report.computed_at,
        truncated=report.population_truncated or any(t.truncated for t in report.tools),
    )


class GapRow(SiteTagged):
    device: str
    tool: str
    state: str
    detail: str
    last_seen: dt.datetime | None = None
    ninjaone_last_contact: dt.datetime | None = Field(
        default=None, description="Absent for an unmanaged endpoint: NinjaOne does not have it."
    )


class BackupGapRow(SiteTagged):
    """One machine Cove holds no device for."""

    device: str
    role: str = Field(description="server or workstation.")
    role_basis: str = Field(
        description="What decided it: the operating system where it says, else NinjaOne's "
        "node class, which is the only signal for Linux and anything unknown."
    )
    ninjaone_last_contact: dt.datetime | None = None
    detail: str


class BackupGaps(RowCapped):
    client_id: str
    role: str | None
    scope: dict[str, str] = Field(
        description="Per end customer, which machines it expects to be backed up and why. "
        "A site set to servers only has its workstations left out of this list entirely."
    )
    left_out: int = Field(
        description="Machines with no Cove device that this customer does not expect to be "
        "backed up: out of its backup scope, or recorded as needing no backup."
    )
    by_role: dict[str, int] = Field(description="Every listed machine by role, before the cap.")
    by_site: dict[str, int] = Field(description="Every listed machine by end customer.")
    not_assessed: dict[str, str]
    machines: list[BackupGapRow]


class CoverageGaps(RowCapped):
    client_id: str
    tool: str | None
    state: str | None
    by_state: dict[str, int] = Field(description="Every matching gap by state, before the cap.")
    by_tool: dict[str, int] = Field(description="Every matching gap by tool, before the cap.")
    by_site: dict[str, int] = Field(description="Every matching gap by site, before the cap.")
    not_assessed: dict[str, str]
    gaps: list[GapRow]


@tool(ConnectorName.COVE)
async def list_machines_without_backup(
    client_id: str, site_id: str | None = None, role: BackupRole | None = None
) -> BackupGaps:
    """List the machines Cove is not backing up, under each customer's own backup scope.

    THE tool for "which servers have no backup", "what is not being backed
    up", "which machines is Cove missing", "is anything at Beacon unprotected
    by backup". Each row is one NinjaOne managed computer that no Cove device
    matches, with whether it is a server or a workstation and what decided
    that. `role`: "server" or "workstation" to narrow it.

    **Each end customer decides which machines count.** A site that backs up
    servers only has its workstations left out — they are not a gap, because
    nobody bought backup for them — and `scope` says, per customer, which rule
    applied and why. `left_out` counts the machines that rule excluded, plus
    any the franchisee has recorded as needing no backup. It is the board's
    own join and so is the same answer the deck gives.

    Say it as what it is: Cove appears to hold no device for these machines.
    The match is on the name, so it is a strong signal and not a certainty.

    Not for machines Cove holds whose backup is broken — that is
    `list_backup_failures`, and the two are different problems. Not for one
    machine (`get_device_backup`) or for other tools' coverage
    (`list_coverage_gaps`).
    """

    report = await _report(client_id, site_id)
    machines, scope_by_site, left_out = await _backup_population(client_id, report)
    rows = [
        BackupGapRow(
            site=d.device.site_name,
            site_slug=d.device.site_slug,
            device=d.device.name,
            role=decided.role,
            role_basis=decided.basis,
            ninjaone_last_contact=d.device.last_contact,
            detail=verdict.detail,
        )
        for d, verdict, decided in machines
        if role is None or decided.role == role
    ]
    rows.sort(key=lambda r: (r.role != "server", r.site, r.device.lower()))
    kept, fields = capped(
        "list_machines_without_backup",
        rows,
        noun="machines",
        page_truncated=report.population_truncated or any(t.truncated for t in report.tools),
    )
    return BackupGaps(
        client_id=client_id,
        role=role,
        scope=scope_by_site,
        left_out=left_out,
        by_role=dict(Counter(r.role for r in rows)),
        by_site=dict(Counter(r.site_slug for r in rows)),
        not_assessed={t.key: t.reason for t in report.tools if not t.assessed},
        machines=kept,
        **fields,
    )


@tool(ConnectorName.NINJAONE)
async def list_coverage_gaps(
    client_id: str,
    site_id: str | None = None,
    tool: str | None = None,
    state: GapState | None = None,
) -> CoverageGaps:
    """List the managed computers a security tool does not cover, worst first.

    Use this to name machines: "which of Acme's computers have no Bitdefender
    agent", "which devices are stale in ConnectSecure", "what is in Bitdefender
    that NinjaOne doesn't manage", "list the coverage gaps at Beacon". Each row
    is one device and one tool with its state — absent (the tool does not have
    it, or has it with no agent), stale (the tool has it but it is not
    current, with the detail saying why), unmatched (NinjaOne gives it no
    hostname, or two candidates nothing tells apart), unmanaged (an endpoint
    the tool holds with no managed computer of that hostname, MAC or serial at
    the site; a default Windows name is noted as likely not yet onboarded), or
    rmm_agent_down (the tool still hears from a machine NinjaOne has not heard
    from: alive, RMM agent down; asked for by name, never counted as a gap) —
    and the last time the tool saw it. Excluded devices are not gaps and are
    not listed. Counts by state, tool and site cover every gap before the row
    cap.

    `tool`: "bitdefender", "connectsecure" or "backup" (Cove). `state`: one
    of absent, stale, unmatched, unmanaged, rmm_agent_down. Pass `site` to
    narrow to one end customer.

    Not for counting: `count_coverage` is smaller. Not for a tool's own
    protection or scan detail (`list_unprotected_endpoints`,
    `list_scan_coverage`). For backup specifically, prefer
    `list_machines_without_backup`: it applies each end customer's own backup
    scope, so a laptop at a customer who backs up servers only is not listed
    as a gap.
    """
    known = {t.key for t in COVERAGE_TOOLS}
    if tool is not None and tool not in known:
        raise ValueError(f"tool must be one of {sorted(known)}, not {tool!r}")
    report = await _report(client_id, site_id)
    order = {"absent": 0, "unmanaged": 1, "stale": 2, "unmatched": 3, "rmm_agent_down": 4}
    rows: list[GapRow] = []
    if state != "unmanaged":
        listed = (*GAP_STATES, "rmm_agent_down") if state == "rmm_agent_down" else GAP_STATES
        for d in report.devices:
            for key, verdict in d.tools.items():
                if verdict.state not in listed:
                    continue
                if tool is not None and key != tool:
                    continue
                if state is not None and verdict.state != state:
                    continue
                rows.append(
                    GapRow(
                        site=d.device.site_name,
                        site_slug=d.device.site_slug,
                        device=d.device.name,
                        tool=key,
                        state=verdict.state,
                        detail=verdict.detail,
                        last_seen=verdict.last_seen,
                        ninjaone_last_contact=d.device.last_contact,
                    )
                )
    if state in (None, "unmanaged"):
        names = {s.site_id: (s.slug, s.name) for s in report.sites}
        for u in report.unmanaged:
            if tool is not None and u.tool != tool:
                continue
            slug, name = names.get(u.site_id, (str(u.site_id), str(u.site_id)))  # type: ignore[arg-type]
            rows.append(
                GapRow(
                    site=name,
                    site_slug=slug,
                    device=u.name,
                    tool=u.tool,
                    state="unmanaged",
                    detail=f"{u.detail}; {u.note}" if u.note else u.detail,
                    last_seen=u.last_seen,
                )
            )
    rows.sort(key=lambda r: (order[r.state], r.site, r.tool, r.device.lower()))
    kept, fields = capped(
        "list_coverage_gaps",
        rows,
        noun="gaps",
        page_truncated=report.population_truncated or any(t.truncated for t in report.tools),
    )
    return CoverageGaps(
        client_id=client_id,
        tool=tool,
        state=state,
        by_state=dict(Counter(r.state for r in rows)),
        by_tool=dict(Counter(r.tool for r in rows)),
        by_site=dict(Counter(r.site_slug for r in rows)),
        not_assessed={t.key: t.reason for t in report.tools if not t.assessed},
        gaps=kept,
        **fields,
    )

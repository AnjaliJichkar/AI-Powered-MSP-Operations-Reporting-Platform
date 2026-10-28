"""The Bitdefender GravityZone tools the model can call.

Each docstring is what the model reads when it chooses a tool. Every tool is
read-only (hard rule 2, enforced by the client's method allowlist), takes
`client_id` (hard rule 1) and an optional `site_id` the loop resolves from a
slug, and reports `truncated` honestly (hard rule 7). Results are compact
(`app/connectors/results.py`): a list tool returns the fields the prompt needs
per row, at most its row cap, and aggregates computed before the cap so "how
many" never depends on the rows that were kept.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.registry import tool
from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.bitdefender.models import (
    AGENT_TYPES,
    CORE_MODULES,
    LICENSE_STATES,
    LICENSED_MODULES,
    MACHINE_TYPES,
    POWER_STATES,
    BdEndpoint,
    BdIncident,
    BdLicense,
    module_status,
    modules_seen,
)
from app.connectors.factory import connector_for
from app.connectors.matching import normalise_hostname
from app.connectors.results import RowCapped, SiteTagged, capped
from app.connectors.scope import SiteRef
from app.devices.lookup import linked_ids
from app.models.enums import ConnectorName

EndpointGroupBy = Literal["site", "status", "module", "os"]
LICENSED_MODULES_ITEMS = tuple(LICENSED_MODULES.items())

#: Protection status of one endpoint, as `count_endpoints(group_by="status")` tallies it.
STATUS_NO_AGENT = "no agent"
STATUS_OUTDATED = "agent outdated"
STATUS_CORE_OFF = "core protection off"
STATUS_PROTECTED = "protected"


def _covered(connector: BitdefenderClient) -> list[str]:
    return connector.scope.target_slugs


def _truncation(page_truncated: bool) -> str | None:
    if not page_truncated:
        return None
    return "The vendor listing stopped at the page cap, so every count is a lower bound."


def protection_status(endpoint: BdEndpoint) -> str:
    """One label per endpoint: the first problem found, in order of severity."""
    if not endpoint.has_agent:
        return STATUS_NO_AGENT
    if any(endpoint.module_enabled(module) is False for module in CORE_MODULES):
        return STATUS_CORE_OFF
    if endpoint.product_outdated:
        return STATUS_OUTDATED
    return STATUS_PROTECTED


def unprotected_reasons(endpoint: BdEndpoint) -> list[str]:
    """Every reason an endpoint counts as unprotected. Empty when it is protected."""
    reasons: list[str] = []
    if not endpoint.has_agent:
        reasons.append("no security agent")
        return reasons
    for module in CORE_MODULES:
        if endpoint.module_enabled(module) is False:
            reasons.append(f"{module} disabled")
    if endpoint.product_outdated:
        reasons.append("agent outdated")
    return reasons


# --- count_endpoints -----------------------------------------------------------------------


class ModuleCounts(BaseModel):
    """One module's state across endpoints with an agent. The three non-enabled states
    are different facts and are never added together: `disabled` is a policy choice on a
    licensed module, `not_licensed` means the company's licence does not include it, and
    `not_reported` means the vendor gave no value and the licence did not say."""

    enabled: int
    disabled: int
    not_licensed: int
    not_reported: int


class EndpointCounts(BaseModel):
    """The answer to "how many endpoints ...?" without listing anything."""

    client_id: str
    sites_covered: list[str]
    group_by: EndpointGroupBy
    total: int = Field(description="Endpoints counted, before grouping.")
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Group label -> endpoint count, largest first. Empty for `module`.",
    )
    module_counts: dict[str, ModuleCounts] = Field(
        default_factory=dict,
        description="For `module`: every module any endpoint reports (the set is the tenant's, "
        "not a fixed list), with enabled, disabled, not-licensed and not-reported counts "
        "across endpoints that have an agent.",
    )
    edr_licensed: dict[str, bool | None] = Field(
        default_factory=dict,
        description="Site -> whether its GravityZone licence includes EDR (incidents).",
    )
    with_agent: int = Field(description="Endpoints with the Bitdefender agent installed.")
    truncated: bool = False
    truncation_note: str | None = None


@tool(ConnectorName.BITDEFENDER)
async def count_endpoints(
    client_id: str, group_by: EndpointGroupBy = "site", site_id: str | None = None
) -> EndpointCounts:
    """Count Bitdefender GravityZone endpoints by site, protection status, module or OS.

    Use this for any "how many" question about Bitdefender coverage: "how many
    endpoints does Acme have in Bitdefender", "how many are unprotected", "how
    many have the firewall module off", "how many Macs are in GravityZone".
    It returns numbers only, so it is always small.

    `group_by`: "site" (endpoints per end customer); "status" (protected, agent
    outdated, core protection off, no agent: one label per endpoint, worst
    first); "module" (every module the tenant reports, with how many endpoints
    have it enabled, disabled by policy, not licensed for the company, or not
    reported; say which of those a "no" is, they are different things); "os"
    (Windows, macOS, Linux, Other). Pass `site` to count within one end
    customer. If `truncated` is true every number is a lower bound; say so.

    Not for naming the machines: use `list_unprotected_endpoints` or
    `list_outdated_agents` for that. Not for NinjaOne's device inventory
    (`count_devices`) or antivirus rows (`list_antivirus_status`).
    """
    async with connector_for(client_id, ConnectorName.BITDEFENDER, site_id) as connector:
        assert isinstance(connector, BitdefenderClient)
        rows, page_truncated = await connector.list_endpoints()

        with_agent = [endpoint for _, endpoint in rows if endpoint.has_agent]
        counts: dict[str, int] = {}
        module_counts: dict[str, ModuleCounts] = {}
        edr: dict[str, bool | None] = {}
        if group_by == "module":
            licences = await connector.licences()
            edr = {slug: lic.edr_licensed for slug, lic in licences.items()}
            agents = [(site, endpoint) for site, endpoint in rows if endpoint.has_agent]
            for module in modules_seen(with_agent):
                states = Counter(
                    module_status(endpoint, module, licences.get(site.slug))
                    for site, endpoint in agents
                )
                module_counts[module] = ModuleCounts(
                    enabled=states["enabled"],
                    disabled=states["disabled"],
                    not_licensed=states["not_licensed"],
                    not_reported=states["not_reported"],
                )
        else:

            def label(site: SiteRef, endpoint: BdEndpoint) -> str:
                if group_by == "site":
                    return site.name
                if group_by == "status":
                    return protection_status(endpoint)
                return endpoint.os_family or "unknown"

            tally = Counter(label(site, endpoint) for site, endpoint in rows)
            counts = dict(sorted(tally.items(), key=lambda item: (-item[1], item[0])))
        return EndpointCounts(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            group_by=group_by,
            total=len(rows),
            counts=counts,
            module_counts=module_counts,
            edr_licensed=edr,
            with_agent=len(with_agent),
            truncated=page_truncated,
            truncation_note=_truncation(page_truncated),
        )


# --- list_unprotected_endpoints --------------------------------------------------------------


class EndpointRow(SiteTagged):
    name: str
    os: str | None = None
    machine_type: str | None = None
    policy: str | None = None
    last_successful_scan: dt.datetime | None = None


class UnprotectedEndpoint(EndpointRow):
    reasons: list[str] = Field(description="Why it counts as unprotected. Worst first.")


class UnprotectedEndpointList(RowCapped):
    """Endpoints without working core protection, with the counts behind the rows."""

    client_id: str
    sites_covered: list[str]
    endpoints_checked: int = Field(description="Every endpoint in scope, protected or not.")
    counts_by_reason: dict[str, int] = Field(
        description="Reason -> endpoints, over every unprotected endpoint, before the row cap."
    )
    counts_by_site: dict[str, int] = Field(
        description="Site -> unprotected endpoints, before the row cap."
    )
    endpoints: list[UnprotectedEndpoint]


def _row_fields(site: SiteRef, endpoint: BdEndpoint) -> dict[str, Any]:
    return {
        "site": site.name,
        "site_slug": site.slug,
        "name": endpoint.name or f"endpoint {endpoint.id}",
        "os": endpoint.operating_system_version,
        "machine_type": MACHINE_TYPES.get(endpoint.machine_type or -1),
        "policy": endpoint.policy.name if endpoint.policy else None,
        "last_successful_scan": (
            endpoint.last_successful_scan.date if endpoint.last_successful_scan else None
        ),
    }


@tool(ConnectorName.BITDEFENDER)
async def list_unprotected_endpoints(
    client_id: str, site_id: str | None = None
) -> UnprotectedEndpointList:
    """List GravityZone endpoints whose protection is missing, off or out of date.

    Use this for "what is unprotected", "which machines have no Bitdefender
    agent", "where is antimalware turned off", "what needs attention in
    Bitdefender". An endpoint is listed when it has no security agent, when
    antimalware or Advanced Threat Control is disabled, or when the agent is
    missing updates; each row says which. Worst first: no agent, then core
    protection off, then outdated. Counts by reason and by site are computed
    over every unprotected endpoint before the row cap, so they are exact even
    when the list is cut. Pass `site` for one end customer.

    Not for a count alone (`count_endpoints` with group_by="status"). Not for
    machines NinjaOne knows but Bitdefender does not: that is a cross-tool
    question the business review answers. Not for NinjaOne's antivirus rows
    (`list_antivirus_status`).
    """
    async with connector_for(client_id, ConnectorName.BITDEFENDER, site_id) as connector:
        assert isinstance(connector, BitdefenderClient)
        rows, page_truncated = await connector.list_endpoints()
        flagged = [
            (site, endpoint, unprotected_reasons(endpoint))
            for site, endpoint in rows
            if unprotected_reasons(endpoint)
        ]
        order = {STATUS_NO_AGENT: 0, STATUS_CORE_OFF: 1, STATUS_OUTDATED: 2}
        flagged.sort(
            key=lambda item: (
                order.get(protection_status(item[1]), 9),
                item[0].name,
                item[1].name or "",
            )
        )
        by_reason = Counter(reason for _, _, reasons in flagged for reason in reasons)
        by_site = Counter(site.name for site, _, _ in flagged)
        kept, fields = capped(
            "list_unprotected_endpoints",
            [
                UnprotectedEndpoint(**_row_fields(site, endpoint), reasons=reasons)
                for site, endpoint, reasons in flagged
            ],
            noun="unprotected endpoints",
            page_truncated=page_truncated,
        )
        return UnprotectedEndpointList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            endpoints_checked=len(rows),
            counts_by_reason=dict(by_reason.most_common()),
            counts_by_site=dict(by_site.most_common()),
            endpoints=kept,
            **fields,
        )


# --- list_outdated_agents --------------------------------------------------------------------


class OutdatedAgentList(RowCapped):
    """Endpoints whose Bitdefender agent is missing one or more updates."""

    client_id: str
    sites_covered: list[str]
    endpoints_with_agent: int
    counts_by_site: dict[str, int] = Field(description="Site -> outdated agents, before the cap.")
    endpoints: list[EndpointRow]


@tool(ConnectorName.BITDEFENDER)
async def list_outdated_agents(client_id: str, site_id: str | None = None) -> OutdatedAgentList:
    """List GravityZone endpoints whose Bitdefender agent is out of date.

    Use this for "which agents need updating", "is Bitdefender up to date at
    Acme", "how many outdated agents per customer". It reads the vendor's own
    `productOutdated` flag: the agent is installed but missing one or more
    product updates. Rows are sorted by site then name; `counts_by_site` is
    computed before the row cap. Pass `site` for one end customer.

    Not for endpoints with no agent at all or with modules disabled: those are
    `list_unprotected_endpoints`. Not for one machine's agent and signature
    versions (`get_endpoint_protection`). Not for Windows patching
    (`list_patch_status`).
    """
    async with connector_for(client_id, ConnectorName.BITDEFENDER, site_id) as connector:
        assert isinstance(connector, BitdefenderClient)
        rows, page_truncated = await connector.list_endpoints()
        with_agent = [(site, endpoint) for site, endpoint in rows if endpoint.has_agent]
        outdated = sorted(
            ((site, endpoint) for site, endpoint in with_agent if endpoint.product_outdated),
            key=lambda item: (item[0].name, item[1].name or ""),
        )
        kept, fields = capped(
            "list_outdated_agents",
            [EndpointRow(**_row_fields(site, endpoint)) for site, endpoint in outdated],
            noun="outdated agents",
            page_truncated=page_truncated,
        )
        return OutdatedAgentList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            endpoints_with_agent=len(with_agent),
            counts_by_site=dict(Counter(site.name for site, _ in outdated).most_common()),
            endpoints=kept,
            **fields,
        )


# --- list_recent_detections ------------------------------------------------------------------


class Detection(SiteTagged):
    incident_number: int | None = None
    detection: str | None = Field(default=None, description="The detection name.")
    endpoint: str | None = None
    status: str | None = None
    priority: str | None = None
    action: str | None = Field(default=None, description="blocked, reported or partially_blocked.")
    severity: int | None = Field(default=None, description="1 (lowest) to 100.")
    attack_types: list[str] = Field(default_factory=list)
    created: dt.datetime | None = None
    last_updated: dt.datetime | None = None
    link: str | None = None


class DetectionList(RowCapped):
    """EDR/XDR incidents GravityZone raised or updated in the window."""

    client_id: str
    sites_covered: list[str]
    days: int
    counts_by_status: dict[str, int]
    counts_by_priority: dict[str, int]
    counts_by_action: dict[str, int]
    counts_by_site: dict[str, int]
    edr_licensed: dict[str, bool | None] = Field(
        description="Site -> whether its GravityZone licence includes EDR. A site without "
        "it can have no detections, whatever happened; that is not a quiet week."
    )
    sites_without_edr: list[str] = Field(
        default_factory=list, description="Sites in scope whose licence has no EDR."
    )
    note: str | None = Field(
        default=None, description="Why the list may be empty. Repeat it to the user."
    )
    detections: list[Detection]


@tool(ConnectorName.BITDEFENDER, max_rows=100)
async def list_recent_detections(
    client_id: str, days: int = 7, site_id: str | None = None
) -> DetectionList:
    """List Bitdefender GravityZone detections (EDR/XDR incidents) from the last `days` days.

    Use this for "any threats at Acme this week", "what did Bitdefender catch",
    "open security incidents", "was anything blocked". Each row is one
    incident: what was detected, on which endpoint, whether it was blocked or
    only reported, its status and priority, and a console link. The window is
    on the incident's last update, so an older incident that changed in the
    window is included. Counts by status, priority, action and site are
    computed before the row cap. Newest first. `days` defaults to 7.

    Detections exist only where the customer's licence includes EDR: the result
    says per site (`edr_licensed`) and names the sites without it, so an empty
    list on an unlicensed site is "EDR is not licensed", never "a quiet week".
    Not for NinjaOne alerts (`list_open_alerts`) or NinjaOne's antivirus state
    (`list_antivirus_status`). Not for protection coverage
    (`list_unprotected_endpoints`).
    """
    if days < 1 or days > 90:
        raise ValueError("days must be between 1 and 90")
    until = dt.datetime.now(dt.UTC)
    since = until - dt.timedelta(days=days)
    async with connector_for(client_id, ConnectorName.BITDEFENDER, site_id) as connector:
        assert isinstance(connector, BitdefenderClient)
        licences = await connector.licences()
        edr = {slug: lic.edr_licensed for slug, lic in licences.items()}
        without = sorted(slug for slug, licensed in edr.items() if licensed is False)
        # Only licensed companies are asked: an unlicensed one has nothing to
        # list, and a site's other company may be the one with the licence.
        licensed_sites = await connector.companies_with_edr()
        if licensed_sites:
            rows, page_truncated = await connector.list_incidents(
                since=since, until=until, sites=licensed_sites
            )
        else:
            rows, page_truncated = [], False
        note: str | None = None
        if without and len(without) == len(edr):
            note = (
                "EDR is not licensed for "
                + ("this site" if len(without) == 1 else "any site in scope")
                + ", so GravityZone raises no detections for it. This is not a quiet week."
            )
        elif without:
            note = (
                f"EDR is not licensed for {len(without)} of the sites in scope: "
                + ", ".join(without)
                + "."
            )

        def row(site: SiteRef, incident: BdIncident) -> Detection:
            return Detection(
                site=site.name,
                site_slug=site.slug,
                incident_number=incident.incident_number,
                detection=incident.details.detection_name if incident.details else None,
                endpoint=incident.endpoint_name,
                status=incident.status,
                priority=incident.priority,
                action=incident.main_action,
                severity=incident.severity_score,
                attack_types=list(incident.attack_types or []),
                created=incident.created,
                last_updated=incident.last_updated,
                link=incident.incident_link,
            )

        ordered = sorted(
            rows,
            key=lambda item: item[1].last_updated or item[1].created or since,
            reverse=True,
        )
        kept, fields = capped(
            "list_recent_detections",
            [row(site, incident) for site, incident in ordered],
            noun="detections",
            page_truncated=page_truncated,
        )
        return DetectionList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            days=days,
            counts_by_status=dict(Counter(i.status or "unknown" for _, i in rows).most_common()),
            counts_by_priority=dict(
                Counter(i.priority or "unknown" for _, i in rows).most_common()
            ),
            counts_by_action=dict(
                Counter(i.main_action or "unknown" for _, i in rows).most_common()
            ),
            counts_by_site=dict(Counter(site.name for site, _ in rows).most_common()),
            edr_licensed=edr,
            sites_without_edr=without,
            note=note,
            detections=kept,
            **fields,
        )


# --- get_endpoint_protection -----------------------------------------------------------------


class EndpointProtection(BaseModel):
    """One machine's protection as GravityZone sees it now."""

    client_id: str
    site: str
    site_slug: str
    name: str
    matched_on: str = Field(description="The name asked for, and what it matched.")
    os: str | None = None
    state: str | None = Field(default=None, description="online, offline, suspended or unknown.")
    last_seen: dt.datetime | None = None
    agent_installed: bool
    agent_type: str | None = None
    agent_version: str | None = None
    agent_outdated: bool | None = None
    engine_version: str | None = None
    signatures_updated: dt.datetime | None = None
    signatures_outdated: bool | None = None
    licence: str | None = None
    policy: str | None = None
    policy_applied: bool | None = None
    modules_enabled: list[str] = Field(default_factory=list)
    modules_disabled: list[str] = Field(
        default_factory=list, description="Licensed for the company and off by policy."
    )
    modules_not_licensed: list[str] = Field(
        default_factory=list, description="The company's licence does not include these."
    )
    edr_licensed: bool | None = None
    malware_detected_last_24h: bool | None = None
    still_infected: bool | None = None
    isolated: bool | None = None
    risk_score: float | None = Field(default=None, description="Percent; higher is worse.")
    risk_impact: str | None = None
    last_successful_scan: dt.datetime | None = None
    candidates: list[str] = Field(
        default_factory=list,
        description="Other endpoints whose names also matched, if the name was ambiguous.",
    )


@tool(ConnectorName.BITDEFENDER)
async def get_endpoint_protection(
    client_id: str, device_name: str, site_id: str | None = None
) -> EndpointProtection:
    """Show one machine's Bitdefender protection: agent, signatures, modules, policy, risk.

    Use this when an engineer names a machine and asks about its security:
    "is TL-AUS-WS14 protected", "what modules are on the finance server",
    "when did Bitdefender last update on that laptop", "is it isolated". The
    name is matched on the hostname, ignoring case and any domain suffix, within
    the client's sites (or one site if given). If several endpoints share the
    name the first is shown and the rest are listed in `candidates`; ask with
    `site` to pick one.

    Not for a list of machines (`list_unprotected_endpoints`, `list_outdated_agents`)
    or for NinjaOne's view of the same machine (`get_device`,
    `list_antivirus_status`).
    """
    wanted = normalise_hostname(device_name)
    if not wanted:
        raise ValueError("device_name must be a hostname, e.g. TL-AUS-WS14")
    async with connector_for(client_id, ConnectorName.BITDEFENDER, site_id) as connector:
        assert isinstance(connector, BitdefenderClient)
        rows, _ = await connector.list_endpoints()
        # The identity layer's links first (flag on): the machine may carry
        # another name in GravityZone. Then the name itself, as always.
        linked = await linked_ids(
            connector.scope.client_id,
            ConnectorName.BITDEFENDER,
            device_name,
            connector.scope.site_ids[0] if connector.scope.is_single_site else None,
        )
        hits = [(site, endpoint) for site, endpoint in rows if endpoint.id in linked]
        hits += [
            (site, endpoint)
            for site, endpoint in rows
            if endpoint.id not in linked
            and (
                normalise_hostname(endpoint.name) == wanted
                or normalise_hostname(endpoint.fqdn) == wanted
            )
        ]
        if not hits:
            raise ValueError(
                f"no GravityZone endpoint named {device_name!r} among "
                f"{connector.scope.describe_targets()}; check the spelling or the site"
            )
        site, endpoint = hits[0]
        detail = await connector.get_endpoint_details(site, endpoint.id)
        licence: BdLicense | None = await connector.licence(site)
        merged = endpoint.model_copy(update={"modules": detail.modules or endpoint.modules})
        states = {m: module_status(merged, m, licence) for m in modules_seen([merged])}
        for module, flag in LICENSED_MODULES_ITEMS:
            if module not in states and licence is not None and licence.flag(flag) is False:
                states[module] = "not_licensed"
        agent = detail.agent
        return EndpointProtection(
            client_id=str(client_id),
            site=site.name,
            site_slug=site.slug,
            name=detail.name or endpoint.name or endpoint.id,
            matched_on=f"{device_name} -> {endpoint.name or endpoint.fqdn}",
            os=detail.operating_system or endpoint.operating_system_version,
            state=POWER_STATES.get(detail.state if detail.state is not None else -1),
            last_seen=detail.last_seen,
            agent_installed=bool(detail.managed_with_best or endpoint.has_agent),
            agent_type=AGENT_TYPES.get(agent.type if agent and agent.type is not None else -1),
            agent_version=agent.product_version if agent else None,
            agent_outdated=agent.product_outdated if agent else endpoint.product_outdated,
            engine_version=agent.engine_version if agent else None,
            signatures_updated=agent.last_update if agent else None,
            signatures_outdated=agent.signature_outdated if agent else None,
            licence=LICENSE_STATES.get(
                agent.licensed if agent and agent.licensed is not None else -1
            ),
            policy=detail.policy.name if detail.policy else None,
            policy_applied=detail.policy.applied if detail.policy else None,
            modules_enabled=[m for m, s in states.items() if s == "enabled"],
            modules_disabled=[m for m, s in states.items() if s == "disabled"],
            modules_not_licensed=[m for m, s in states.items() if s == "not_licensed"],
            edr_licensed=licence.edr_licensed if licence else None,
            malware_detected_last_24h=(
                detail.malware_status.detection if detail.malware_status else None
            ),
            still_infected=detail.malware_status.infected if detail.malware_status else None,
            isolated=detail.is_isolated,
            risk_score=detail.risk_score.value if detail.risk_score else None,
            risk_impact=detail.risk_score.impact if detail.risk_score else None,
            last_successful_scan=(
                detail.last_successful_scan.date if detail.last_successful_scan else None
            ),
            candidates=[f"{s.name}: {e.name}" for s, e in hits[1:]],
        )

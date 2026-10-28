"""The ConnectSecure tools the model can call.

Each docstring is what the model reads when it chooses a tool. Every tool is
read-only (hard rule 2, enforced by the client's GET-path allowlist), takes
`client_id` (hard rule 1) and an optional `site_id` the loop resolves from a
slug, and reports `truncated` honestly (hard rule 7). Results are compact:
aggregates are computed over every row before the row cap, so "how many"
never depends on the rows that were kept, and every count carries the scan
dates it came from, because a vulnerability count is only as current as the
last scan.

What the vendor gives, live (docs/api-notes/connectsecure.md): a finding row
names the CVE, its scores and the software, but no fix and no known-exploited
flag. The fix is joined from the remediation rows by asset and normalised
product ("fix not reported" when there is none); known-exploited counts come
from the assets' `cisa_vul`, and a listing of known-exploited CVEs from one
extra read filtered to CISA's problem group.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.registry import tool
from app.connectors.connectsecure.client import ConnectSecureClient
from app.connectors.connectsecure.models import (
    SEVERITY_ORDER,
    CsAsset,
    CsRemediation,
    CsVulnerability,
    RemediationKind,
    risk_grade,
)
from app.connectors.factory import connector_for
from app.connectors.matching import normalise_hostname
from app.connectors.results import RowCapped, SiteTagged, capped
from app.connectors.scope import SiteRef
from app.devices.lookup import linked_ids
from app.models.enums import ConnectorName
from app.settings import get_settings

VulnerabilityGroupBy = Literal["severity", "site", "product", "asset"]

#: How many device names a grouped row carries; the count is always complete.
NAMES_PER_ROW = 8

FIX_NOT_REPORTED = "fix not reported"

#: What a result says about which set of records it counted. The vendor reports
#: a finding its scanner proved on the machine and one it only suspects from an
#: inferred version; they differ by more than half, so every result says which
#: it is showing and the two are never added together.
POPULATION_CONFIRMED = (
    "confirmed findings only: ConnectSecure's scanner proved the vulnerable build is on the "
    "machine. Say so when you give these numbers; suspected findings are not included."
)
POPULATION_BOTH = (
    "confirmed and suspected findings together: the suspected ones were inferred from a "
    "version number and not verified on the machine. Say that plainly; this total does not "
    "match the vendor's own console, which shows confirmed only."
)


def _population(include_unconfirmed: bool) -> str:
    return POPULATION_BOTH if include_unconfirmed else POPULATION_CONFIRMED


def _covered(connector: ConnectSecureClient) -> list[str]:
    return connector.scope.target_slugs


def _truncation(page_truncated: bool) -> str | None:
    if not page_truncated:
        return None
    return "The vendor listing stopped at the row cap, so every count is a lower bound."


class ScanDates(BaseModel):
    """When the counted data was scanned. A count is as current as its oldest scan."""

    latest: dt.datetime | None = Field(default=None, description="The most recent asset scan.")
    oldest: dt.datetime | None = Field(
        default=None, description="The oldest scan among assets in scope, if any."
    )
    assets_never_scanned: int = 0


def _scan_dates(assets: list[CsAsset]) -> ScanDates:
    scans = [a.last_scan for a in assets if a.last_scan is not None]
    return ScanDates(
        latest=max(scans) if scans else None,
        oldest=min(scans) if scans else None,
        assets_never_scanned=sum(1 for a in assets if a.last_scan is None),
    )


def _severity_counts(rows: list[CsVulnerability]) -> dict[str, int]:
    tally = Counter(v.severity or "unknown" for v in rows)
    return {key: tally[key] for key in sorted(tally, key=lambda s: SEVERITY_ORDER.get(s, 9))}


# --- count_vulnerabilities -------------------------------------------------------------------


class VulnerabilityCounts(BaseModel):
    """The answer to "how many vulnerabilities ...?" without listing anything."""

    client_id: str
    sites_covered: list[str]
    group_by: VulnerabilityGroupBy
    total: int = Field(
        description="Vulnerability findings counted: one per (asset, vulnerability) pair."
    )
    unique_vulnerabilities: int | None = Field(
        default=None, description="Distinct CVEs or checks behind the findings, when known."
    )
    known_exploited: int = Field(
        description="Findings on CISA's Known Exploited Vulnerabilities list, summed over the "
        "assets in scope (the vendor's per-asset count)."
    )
    counts: dict[str, int] = Field(
        description="Group label -> findings, largest first (severity: worst first)."
    )
    assets_in_scope: int = Field(description="Scanned assets the counts cover.")
    scan_dates: ScanDates
    population: str = Field(description="Which set of findings these numbers count.")
    source: str = Field(
        description="Where the numbers came from: the vendor's per-company totals, or the "
        "per-asset findings."
    )
    truncated: bool = False
    truncation_note: str | None = None


@tool(ConnectorName.CONNECTSECURE)
async def count_vulnerabilities(
    client_id: str,
    group_by: VulnerabilityGroupBy = "severity",
    include_unconfirmed: bool = False,
    site_id: str | None = None,
) -> VulnerabilityCounts:
    """Count ConnectSecure vulnerability findings by severity, site, product or asset.

    Use this for any "how many" question about vulnerabilities, including one
    per customer or per severity: "how many critical vulnerabilities does Acme
    have", "how many critical vulnerabilities does each customer have", "which
    customers have the most", "what software carries the most findings",
    "which machines are worst". A finding is one vulnerability on one asset;
    the same CVE on ten machines is ten findings, and `unique_vulnerabilities`
    says how many distinct CVEs are behind them. `known_exploited` is how many
    findings are on CISA's Known Exploited Vulnerabilities list. Every count
    carries `scan_dates`: it is only as current as the scans it came from, so
    say when the data was scanned. Numbers only, so it is always small.

    `group_by`: "severity" (critical, high, medium, low), "site" (findings per
    end customer), "product" (the affected software), "asset" (per machine,
    largest first). Pass `site` to count within one end customer. If
    `truncated` is true every number is a lower bound; say so.

    **Counts confirmed findings only by default**, which is what the vendor's
    own console shows: the scanner proved the vulnerable build is on the
    machine. `include_unconfirmed` adds the ones it inferred from a version
    number and has not verified, which on a real tenant more than doubles the
    total. `population` says which you got; repeat it in the answer, and never
    add a confirmed count to an unconfirmed one.

    Not for naming the CVEs or the fix: use `list_critical_vulnerabilities`
    or `list_top_remediations`. Not for Windows patch backlogs in NinjaOne
    (`list_patch_status`) or Bitdefender's endpoint state (`count_endpoints`).
    """
    async with connector_for(client_id, ConnectorName.CONNECTSECURE, site_id) as connector:
        assert isinstance(connector, ConnectSecureClient)
        assets, assets_truncated = await connector.list_assets()
        asset_rows = [asset for _, asset in assets]
        known_exploited = sum(a.cisa_vul or 0 for a in asset_rows)
        if (
            not include_unconfirmed
            and len(connector.scope.site_ids) > 1
            and group_by in ("severity", "site")
        ):
            # A many-site question: the vendor's own totals, one row per company,
            # rather than every finding of every customer walked.
            stats = await connector.company_stats()
            tally: Counter[str] = Counter()
            total = 0
            for site, stat in stats:
                total += stat.total
                if group_by == "site":
                    tally[site.name] += stat.total
                else:
                    for level in ("critical", "high", "medium", "low"):
                        tally[level] += stat.count(level)
            ordered = (
                {k: tally[k] for k in ("critical", "high", "medium", "low")}
                if group_by == "severity"
                else dict(sorted(tally.items(), key=lambda item: (-item[1], item[0])))
            )
            return VulnerabilityCounts(
                client_id=str(client_id),
                sites_covered=_covered(connector),
                group_by=group_by,
                total=total,
                known_exploited=known_exploited,
                counts=ordered,
                assets_in_scope=len(asset_rows),
                scan_dates=_scan_dates(asset_rows),
                population=POPULATION_CONFIRMED,
                source="the vendor's per-company totals (company_stats)",
                truncated=assets_truncated,
                truncation_note=_truncation(assets_truncated),
            )
        rows, page_truncated = await connector.list_vulnerabilities(
            with_fixes=False, include_unconfirmed=include_unconfirmed
        )
        findings = [v for _, v in rows]

        def label(site: SiteRef, v: CsVulnerability) -> str:
            if group_by == "severity":
                return v.severity or "unknown"
            if group_by == "site":
                return site.name
            if group_by == "product":
                return v.product or "unknown"
            return v.host_name or f"asset {v.asset_id}"

        tally = Counter(label(site, v) for site, v in rows)
        if group_by == "severity":
            counts = {k: tally[k] for k in sorted(tally, key=lambda s: SEVERITY_ORDER.get(s, 9))}
        else:
            counts = dict(sorted(tally.items(), key=lambda item: (-item[1], item[0])))
        truncated = page_truncated or assets_truncated
        return VulnerabilityCounts(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            group_by=group_by,
            total=len(findings),
            unique_vulnerabilities=len({v.problem for v in findings}),
            known_exploited=known_exploited,
            counts=counts,
            assets_in_scope=len(asset_rows),
            scan_dates=_scan_dates(asset_rows),
            population=_population(include_unconfirmed),
            source="the per-asset findings (asset_wise_vulnerabilities)",
            truncated=truncated,
            truncation_note=_truncation(truncated),
        )


# --- list_critical_vulnerabilities ------------------------------------------------------------


class VulnerabilityRow(SiteTagged):
    vulnerability: str = Field(description="The CVE id or check name.")
    title: str | None = None
    severity: str | None = None
    cvss: float | None = Field(default=None, description="CVSS base score, 0 to 10.")
    epss: float | None = Field(
        default=None, description="Probability of exploitation in 30 days, 0 to 1."
    )
    known_exploited: bool | None = Field(
        default=None, description="On CISA's Known Exploited Vulnerabilities list."
    )
    product: str | None = None
    fix: str = Field(
        description="The remediation the vendor pairs with it on these devices, or "
        "'fix not reported'."
    )
    devices_affected: int
    devices: list[str] = Field(description=f"Up to {NAMES_PER_ROW} affected devices.")
    first_seen: dt.datetime | None = None


class CriticalVulnerabilityList(RowCapped):
    """Critical and known-exploited findings grouped by vulnerability, the most urgent first."""

    client_id: str
    sites_covered: list[str]
    known_exploited_only: bool
    findings: int = Field(
        description="Critical or known-exploited (asset, vulnerability) pairs, before grouping."
    )
    known_exploited_findings: int
    devices_affected: int = Field(description="Distinct devices with at least one.")
    fix_reported_for: int = Field(
        description="Findings the vendor pairs a fix with; the rest print 'fix not reported'."
    )
    counts_by_site: dict[str, int] = Field(description="Site -> findings.")
    counts_by_product: dict[str, int] = Field(description="Product -> findings.")
    scan_dates: ScanDates
    population: str = Field(description="Which set of findings these rows come from.")
    vulnerabilities: list[VulnerabilityRow]


class _Grouped:
    """One (site, problem) with every device it was found on and the earliest sighting."""

    __slots__ = ("devices", "first_seen", "fixes", "site", "vulnerability")

    def __init__(self, site: SiteRef, vulnerability: CsVulnerability) -> None:
        self.site = site
        self.vulnerability = vulnerability
        self.devices: list[str] = []
        self.fixes: Counter[str] = Counter()
        self.first_seen: dt.datetime | None = None


def _group_by_problem(rows: list[tuple[SiteRef, CsVulnerability]]) -> list[_Grouped]:
    """One entry per (site, problem) with the affected device names, worst first."""
    grouped: dict[tuple[str, str], _Grouped] = {}
    for site, v in rows:
        entry = grouped.setdefault((site.slug, v.problem), _Grouped(site, v))
        if v.known_exploited:
            entry.vulnerability = v
        name = v.host_name or (f"asset {v.asset_id}" if v.asset_id else None)
        if name and name not in entry.devices:
            entry.devices.append(name)
        entry.fixes[v.fix or FIX_NOT_REPORTED] += 1
        if v.first_discovered and (
            entry.first_seen is None or v.first_discovered < entry.first_seen
        ):
            entry.first_seen = v.first_discovered
    return sorted(
        grouped.values(),
        key=lambda g: (
            g.vulnerability.rank,
            -(g.vulnerability.epss_score or 0.0),
            -len(g.devices),
            -(g.vulnerability.base_score or 0.0),
            g.vulnerability.problem,
        ),
    )


def _fix_label(fixes: Counter[str]) -> str:
    """The commonest fix among the devices, and how many others there are."""
    if not fixes:
        return FIX_NOT_REPORTED
    (top, _), *rest = fixes.most_common()
    return top if not rest else f"{top} (+{len(rest)} other fix{'es' if len(rest) > 1 else ''})"


@tool(ConnectorName.CONNECTSECURE, max_rows=100)
async def list_critical_vulnerabilities(
    client_id: str,
    known_exploited_only: bool = False,
    include_unconfirmed: bool = False,
    site_id: str | None = None,
) -> CriticalVulnerabilityList:
    """List critical and known-exploited vulnerabilities ConnectSecure found, most urgent first.

    Use this for "what critical vulnerabilities does Acme have", "anything on
    the CISA known-exploited list", "which CVEs affect the most machines",
    "what is the most urgent thing to patch". One row per vulnerability with
    its CVSS score, EPSS exploit probability, whether it is on CISA's Known
    Exploited Vulnerabilities list, the affected software, the fix the vendor
    pairs with it on those devices (or "fix not reported"), and how many
    devices carry it (naming a few). Ordered: known exploited first, then by
    EPSS, then by devices affected. `known_exploited_only` narrows to the
    CISA list, which includes findings of any severity. Counts by site and
    product cover every listed finding before the row cap. `scan_dates` says
    how current the data is; say so.

    **Confirmed findings only by default**: the scanner proved the vulnerable
    build is present. `include_unconfirmed` adds the ones it inferred and has
    not verified. `population` says which you got; repeat it in the answer.

    Not for a count, even a count per customer or per severity: that is
    `count_vulnerabilities`, which is smaller and covers every severity. Not
    for the fixes ranked by how much they resolve (`list_top_remediations`).
    Not for one machine (`get_asset_vulnerabilities`). Not for Windows patches
    waiting in NinjaOne (`list_patch_status`).
    """
    async with connector_for(client_id, ConnectorName.CONNECTSECURE, site_id) as connector:
        assert isinstance(connector, ConnectSecureClient)
        assets, assets_truncated = await connector.list_assets()
        rows, page_truncated = await connector.list_vulnerabilities(
            with_known_exploited=True, include_unconfirmed=include_unconfirmed
        )
        critical = [
            (site, v)
            for site, v in rows
            if (v.severity == "critical" or v.known_exploited)
            and (not known_exploited_only or v.known_exploited)
        ]
        grouped = _group_by_problem(critical)
        kept, fields = capped(
            "list_critical_vulnerabilities",
            [
                VulnerabilityRow(
                    site=g.site.name,
                    site_slug=g.site.slug,
                    vulnerability=g.vulnerability.problem,
                    title=g.vulnerability.title,
                    severity=g.vulnerability.severity,
                    cvss=g.vulnerability.base_score,
                    epss=g.vulnerability.epss_score,
                    known_exploited=g.vulnerability.known_exploited,
                    product=g.vulnerability.product,
                    fix=_fix_label(g.fixes),
                    devices_affected=len(g.devices),
                    devices=g.devices[:NAMES_PER_ROW],
                    first_seen=g.first_seen,
                )
                for g in grouped
            ],
            noun="vulnerabilities",
            page_truncated=page_truncated or assets_truncated,
        )
        return CriticalVulnerabilityList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            known_exploited_only=known_exploited_only,
            findings=len(critical),
            known_exploited_findings=sum(1 for _, v in critical if v.known_exploited),
            devices_affected=len({(s.slug, v.asset_id or v.host_name) for s, v in critical}),
            fix_reported_for=sum(1 for _, v in critical if v.fix),
            counts_by_site=dict(Counter(s.name for s, _ in critical).most_common()),
            counts_by_product=dict(
                Counter(v.product or "unknown" for _, v in critical).most_common()
            ),
            scan_dates=_scan_dates([a for _, a in assets]),
            population=_population(include_unconfirmed),
            vulnerabilities=kept,
            **fields,
        )


# --- list_top_remediations ---------------------------------------------------------------------


class Remediation(BaseModel):
    """One fix across every asset it applies to: the vendor's rows grouped by solution."""

    solution_id: str
    instruction: str
    product: str | None
    kind: RemediationKind
    fix: str
    total: int
    critical: int
    high: int
    medium: int
    low: int
    devices: list[str]
    url: str | None


def group_remediations(rows: list[CsRemediation]) -> list[Remediation]:
    """Rows are one per (asset, solution); a solution is one fix. Ranked by what it resolves."""
    by_solution: dict[str, list[CsRemediation]] = defaultdict(list)
    for r in rows:
        by_solution[r.solution_id or f"{r.product_key}|{r.fix}"].append(r)
    grouped: list[Remediation] = []
    for key, members in by_solution.items():
        first = members[0]
        devices: list[str] = []
        for m in members:
            name = m.host_name or (f"asset {m.asset_id}" if m.asset_id else None)
            if name and name not in devices:
                devices.append(name)
        grouped.append(
            Remediation(
                solution_id=key,
                instruction=first.instruction,
                product=first.product,
                kind=first.kind,
                fix=first.fix,
                total=sum(m.total_vulnerabilities or 0 for m in members),
                critical=sum(m.critical or 0 for m in members),
                high=sum(m.high or 0 for m in members),
                medium=sum(m.medium or 0 for m in members),
                low=sum(m.low or 0 for m in members),
                devices=sorted(devices),
                url=first.url,
            )
        )
    grouped.sort(key=lambda g: (-g.critical, -g.high, -g.total, -len(g.devices), g.instruction))
    return grouped


def top_share(remediations: list[Remediation], *, top: int = 3) -> float | None:
    """The share of critical findings the `top` best-ranked fixes resolve, in percent."""
    critical = [g.critical for g in remediations]
    total = sum(critical)
    if total <= 0:
        return None
    return round(100 * sum(critical[:top]) / total, 1)


class RemediationRow(SiteTagged):
    instruction: str = Field(description="What to do: update, uninstall, or run a script.")
    product: str | None = None
    kind: RemediationKind = Field(
        description="patch (update to a version), uninstall (remove the software), script "
        "(the vendor's remediation script: an engineer action)."
    )
    target_version: str | None = None
    vulnerabilities_resolved: int = Field(
        description="Findings this one fix closes across the devices, all severities."
    )
    critical_resolved: int
    high_resolved: int
    devices_affected: int
    devices: list[str] = Field(default_factory=list, description=f"Up to {NAMES_PER_ROW} devices.")
    download: str | None = None


class RemediationList(RowCapped):
    """Fixes ranked by how much they resolve: one update can close many CVEs on many devices."""

    client_id: str
    sites_covered: list[str]
    remediations: int = Field(description="Distinct fixes in scope, before the cap.")
    counts_by_kind: dict[str, int] = Field(
        description="patch, uninstall, script -> distinct fixes of that kind."
    )
    critical_findings_addressed: int = Field(
        description="Critical findings the fixes in scope resolve, summed over every fix."
    )
    top_three_critical_share: float | None = Field(
        default=None,
        description="Share (0 to 100) of the critical findings the three highest-ranked fixes "
        "resolve: 'three updates resolve 70% of critical findings'.",
    )
    scan_dates: ScanDates
    population: str = Field(description="Which set of findings the fixes were ranked over.")
    fixes: list[RemediationRow]


@tool(ConnectorName.CONNECTSECURE, max_rows=50)
async def list_top_remediations(
    client_id: str, include_unconfirmed: bool = False, site_id: str | None = None
) -> RemediationList:
    """List the fixes that resolve the most ConnectSecure findings, best first.

    Use this for "what should we patch first", "which updates fix the most",
    "how do we bring Acme's vulnerability count down fastest", "what does one
    Chrome update close". Each row is one remediation (update a product to a
    version, uninstall it, or run the vendor's script) with how many findings
    it resolves by severity and on how many devices. Ranked by critical
    findings resolved, then high, then total. `top_three_critical_share` is
    the headline: the share of critical findings the top three fixes close.
    `kind` says whether it is an update, a removal, or a script an engineer
    would run. `scan_dates` says how current the data is.

    **Ranked over confirmed findings only by default**; `include_unconfirmed`
    ranks over both populations, which changes the order. `population` says
    which you got.

    Not for the vulnerabilities themselves (`list_critical_vulnerabilities`)
    or a count (`count_vulnerabilities`). Not for what NinjaOne has queued or
    installed (`list_patch_status`, `list_patch_installs`): a fix listed here
    may already be an approved NinjaOne software patch, which the business
    review reconciles by product and version.
    """
    async with connector_for(client_id, ConnectorName.CONNECTSECURE, site_id) as connector:
        assert isinstance(connector, ConnectSecureClient)
        assets, assets_truncated = await connector.list_assets()
        rows, page_truncated = await connector.list_remediations(
            include_unconfirmed=include_unconfirmed
        )
        by_site: dict[str, list[CsRemediation]] = defaultdict(list)
        sites = {site.slug: site for site, _ in rows}
        for site, r in rows:
            by_site[site.slug].append(r)
        ranked: list[tuple[SiteRef, Remediation]] = []
        for slug, members in by_site.items():
            ranked.extend((sites[slug], g) for g in group_remediations(members))
        ranked.sort(
            key=lambda item: (
                -item[1].critical,
                -item[1].high,
                -item[1].total,
                -len(item[1].devices),
                item[1].instruction,
            )
        )
        kept, fields = capped(
            "list_top_remediations",
            [
                RemediationRow(
                    site=site.name,
                    site_slug=site.slug,
                    instruction=g.instruction,
                    product=g.product,
                    kind=g.kind,
                    target_version=g.fix if g.kind == "patch" else None,
                    vulnerabilities_resolved=g.total,
                    critical_resolved=g.critical,
                    high_resolved=g.high,
                    devices_affected=len(g.devices),
                    devices=g.devices[:NAMES_PER_ROW],
                    download=g.url,
                )
                for site, g in ranked
            ],
            noun="fixes",
            page_truncated=page_truncated or assets_truncated,
        )
        return RemediationList(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            remediations=len(ranked),
            counts_by_kind=dict(Counter(g.kind for _, g in ranked).most_common()),
            critical_findings_addressed=sum(g.critical for _, g in ranked),
            top_three_critical_share=top_share([g for _, g in ranked]),
            scan_dates=_scan_dates([a for _, a in assets]),
            population=_population(include_unconfirmed),
            fixes=kept,
            **fields,
        )


# --- get_asset_vulnerabilities ---------------------------------------------------------------


class AssetVulnerability(BaseModel):
    vulnerability: str
    title: str | None = None
    severity: str | None = None
    cvss: float | None = None
    epss: float | None = None
    known_exploited: bool | None = None
    product: str | None = None
    fix: str = Field(description="The paired remediation, or 'fix not reported'.")
    first_seen: dt.datetime | None = None


class AssetVulnerabilities(RowCapped):
    """One machine's vulnerabilities as ConnectSecure sees them, worst first."""

    client_id: str
    site: str
    site_slug: str
    name: str
    matched_on: str
    ip: str | None = None
    os: str | None = None
    last_scanned: dt.datetime | None = Field(
        default=None, description="When the asset's agent last scanned. Findings are as of then."
    )
    days_since_scan: int | None = None
    risk_score: float | None = Field(default=None, description="0 to 100, higher is worse.")
    risk_grade: str | None = Field(default=None, description="A (best) to F (worst).")
    counts_by_severity: dict[str, int]
    known_exploited: int
    fix_reported_for: int
    population: str = Field(description="Which set of findings these rows come from.")
    candidates: list[str] = Field(
        default_factory=list, description="Other assets whose names also matched."
    )
    vulnerabilities: list[AssetVulnerability]


@tool(ConnectorName.CONNECTSECURE, max_rows=100)
async def get_asset_vulnerabilities(
    client_id: str,
    device_name: str,
    include_unconfirmed: bool = False,
    site_id: str | None = None,
) -> AssetVulnerabilities:
    """Show one machine's vulnerabilities from ConnectSecure, worst first, with its risk grade.

    Use this when an engineer names a machine: "what is wrong with TL-AUS-WS14",
    "why is the finance server graded F", "what vulnerabilities does that
    laptop have". The name is matched on the hostname, ignoring case and any
    domain suffix, within the client's sites (or one site if given; give it
    when you can, since two customers can share a hostname). If several assets
    share the name the first is shown and the rest are listed in `candidates`.
    Rows are ordered known exploited first, then by severity and EPSS, each
    with the fix the vendor pairs with it; counts by severity cover every
    finding before the row cap. `last_scanned` is the date the findings are as
    of; say it.

    **Confirmed findings only by default**; `include_unconfirmed` adds the ones
    the scanner inferred and has not verified. `population` says which you got.

    Not for many machines at once (`list_critical_vulnerabilities`,
    `count_vulnerabilities` with group_by="asset"). Not for the machine's
    antivirus (`get_endpoint_protection`) or its NinjaOne record (`get_device`).
    """
    wanted = normalise_hostname(device_name)
    if not wanted:
        raise ValueError("device_name must be a hostname, e.g. TL-AUS-WS14")
    async with connector_for(client_id, ConnectorName.CONNECTSECURE, site_id) as connector:
        assert isinstance(connector, ConnectSecureClient)
        assets, _ = await connector.list_assets()
        # The identity layer's links first (flag on), then the name, as always.
        linked = await linked_ids(
            connector.scope.client_id,
            ConnectorName.CONNECTSECURE,
            device_name,
            connector.scope.site_ids[0] if connector.scope.is_single_site else None,
        )
        hits = [(site, asset) for site, asset in assets if asset.id in linked]
        hits += [
            (site, asset)
            for site, asset in assets
            if asset.id not in linked and normalise_hostname(asset.host_name) == wanted
        ]
        if not hits:
            raise ValueError(
                f"no ConnectSecure asset named {device_name!r} among "
                f"{connector.scope.describe_targets()}; check the spelling or the site"
            )
        site, asset = hits[0]
        rows, page_truncated = await connector.list_vulnerabilities(
            sites=[site],
            with_known_exploited=True,
            include_unconfirmed=include_unconfirmed,
        )
        mine = [v for s, v in rows if s.slug == site.slug and v.asset_id == asset.id]
        mine.sort(key=lambda v: (v.rank, -(v.epss_score or 0.0), -(v.base_score or 0.0), v.problem))
        kept, fields = capped(
            "get_asset_vulnerabilities",
            [
                AssetVulnerability(
                    vulnerability=v.problem,
                    title=v.title,
                    severity=v.severity,
                    cvss=v.base_score,
                    epss=v.epss_score,
                    known_exploited=v.known_exploited,
                    product=v.product,
                    fix=v.fix or FIX_NOT_REPORTED,
                    first_seen=v.first_discovered,
                )
                for v in mine
            ],
            noun="vulnerabilities",
            page_truncated=page_truncated,
        )
        now = dt.datetime.now(dt.UTC)
        return AssetVulnerabilities(
            client_id=str(client_id),
            site=site.name,
            site_slug=site.slug,
            name=asset.host_name or f"asset {asset.id}",
            matched_on=f"{device_name} -> {asset.host_name}",
            ip=asset.ip,
            os=asset.os_name,
            last_scanned=asset.last_scan,
            days_since_scan=(now - asset.last_scan).days if asset.last_scan else None,
            risk_score=asset.risk_score,
            risk_grade=asset.security_grade or risk_grade(asset.risk_score),
            counts_by_severity=_severity_counts(mine),
            known_exploited=sum(1 for v in mine if v.known_exploited),
            fix_reported_for=sum(1 for v in mine if v.fix),
            population=_population(include_unconfirmed),
            candidates=[f"{s.name}: {a.host_name}" for s, a in hits[1:]],
            vulnerabilities=kept,
            **fields,
        )


# --- list_scan_coverage ----------------------------------------------------------------------


STATUS_NO_AGENT = "no agent"
STATUS_AGENT_NOT_REPORTING = "agent not reporting"
STATUS_AGENT_DEPRECATED = "agent deprecated"
STATUS_NEVER_SCANNED = "never scanned"
STATUS_STALE = "scan out of date"
STATUS_CURRENT = "current"


def scan_status(asset: CsAsset, *, now: dt.datetime, stale_days: int) -> str:
    """One label per asset: the first problem found, in order of seriousness."""
    agent = asset.agent
    if agent is None:
        return STATUS_NO_AGENT
    if agent.is_deprecated or agent.is_retired or agent.agent_deleted:
        return STATUS_AGENT_DEPRECATED
    if agent.reported is False:
        return STATUS_AGENT_NOT_REPORTING
    last = asset.last_scan
    if last is None:
        return STATUS_NEVER_SCANNED
    if (now - last).days >= stale_days:
        return STATUS_STALE
    return STATUS_CURRENT


class CoverageRow(SiteTagged):
    name: str
    os: str | None = None
    status: str
    agent_version: str | None = None
    last_scanned: dt.datetime | None = None
    days_since_scan: int | None = None
    last_reported: dt.datetime | None = Field(
        default=None, description="When the agent last checked in."
    )


class ScanCoverage(RowCapped):
    """Which assets the scanner covers, and how recently."""

    client_id: str
    sites_covered: list[str]
    stale_after_days: int
    assets: int
    counts_by_status: dict[str, int] = Field(
        description="Status -> assets, over every asset before the row cap. Worst first."
    )
    counts_by_site: dict[str, int] = Field(
        description="Site -> assets not current (no agent, not reporting, stale, never scanned)."
    )
    scan_dates: ScanDates
    rows: list[CoverageRow]


@tool(ConnectorName.CONNECTSECURE)
async def list_scan_coverage(client_id: str, site_id: str | None = None) -> ScanCoverage:
    """List ConnectSecure agent status and last scan date per asset, the stale ones first.

    Use this for "is everything at Acme being scanned", "which machines have
    no ConnectSecure agent", "when was the last vulnerability scan", "which
    agents stopped reporting". Each row is one asset with its status: no
    agent, agent deprecated, agent not reporting, never scanned, scan out of
    date (older than `stale_after_days`), or current. Counts by status and by
    site cover every asset before the row cap; `scan_dates` gives the newest
    and oldest scan. Assets that are current are listed last.

    Not for machines the scanner has never seen: that needs NinjaOne's
    inventory beside this list (the business review joins them). Not for
    Bitdefender agents (`list_unprotected_endpoints`) or NinjaOne check-ins
    (`list_devices_not_checked_in`).
    """
    stale_days = get_settings().connectsecure_stale_scan_days
    now = dt.datetime.now(dt.UTC)
    async with connector_for(client_id, ConnectorName.CONNECTSECURE, site_id) as connector:
        assert isinstance(connector, ConnectSecureClient)
        assets, assets_truncated = await connector.list_assets()
        order = {
            STATUS_NO_AGENT: 0,
            STATUS_AGENT_DEPRECATED: 1,
            STATUS_AGENT_NOT_REPORTING: 2,
            STATUS_NEVER_SCANNED: 3,
            STATUS_STALE: 4,
            STATUS_CURRENT: 5,
        }
        labelled = [
            (site, asset, scan_status(asset, now=now, stale_days=stale_days))
            for site, asset in assets
        ]
        labelled.sort(key=lambda item: (order[item[2]], item[0].name, item[1].host_name or ""))
        not_current = [item for item in labelled if item[2] != STATUS_CURRENT]

        def row(site: SiteRef, asset: CsAsset, status: str) -> CoverageRow:
            agent = asset.agent
            return CoverageRow(
                site=site.name,
                site_slug=site.slug,
                name=asset.host_name or f"asset {asset.id}",
                os=asset.os_name,
                status=status,
                agent_version=agent.agent_version if agent else asset.agent_version,
                last_scanned=asset.last_scan,
                days_since_scan=(now - asset.last_scan).days if asset.last_scan else None,
                last_reported=agent.last_reported if agent else None,
            )

        kept, fields = capped(
            "list_scan_coverage",
            [row(*item) for item in labelled],
            noun="assets",
            page_truncated=assets_truncated,
        )
        by_status: dict[str, int] = defaultdict(int)
        for _, _, status in labelled:
            by_status[status] += 1
        return ScanCoverage(
            client_id=str(client_id),
            sites_covered=_covered(connector),
            stale_after_days=stale_days,
            assets=len(assets),
            counts_by_status={s: by_status[s] for s in order if by_status.get(s)},
            counts_by_site=dict(Counter(site.name for site, _, _ in not_current).most_common()),
            scan_dates=_scan_dates([a for _, a in assets]),
            rows=kept,
            **fields,
        )

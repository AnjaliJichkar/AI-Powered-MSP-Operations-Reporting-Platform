"""Vulnerability facts for the review: ConnectSecure findings, fixes, scan coverage, the join.

One guarded read (`collect_connectsecure`), one NinjaOne read for the
reconciliation (`collect_software_patch_queue`) and one derivation
(`derive_scan_coverage`), beside `security.py`'s Bitdefender reads.

What the vendor gives, live (docs/api-notes/connectsecure.md):

- **Findings** (`asset_wise_vulnerabilities`): one row per CVE per asset, with
  severity, CVSS and EPSS, but **no fix and no known-exploited flag**.
- **Two populations.** The vendor reports a finding its scanner *confirmed* on
  the machine and one it only *suspects* from an inferred version. They differ
  by more than half (3,869 against 8,278 on the pilot site), and no other
  figure agrees with their sum. **Every number on the deck is confirmed only**
  (`unconfirmed=false`, which reconciles exactly with the per-asset counters);
  the suspected ones are one caveat sentence and nothing else.
- **Fixes** (`get_remediation`): one row per asset per solution, with the
  product, the target **version** (never a KB on the pilot: 0 of 300) and what
  kind of work it is. This is the primary source for what to do; the client
  joins a fix to a finding by asset id and normalised product, never hostname,
  and a finding with no such row reads "fix not reported".
- **Known exploited**: counted from each asset's own `cisa_vul` (a confirmed
  count); the CVEs come from one extra read filtered to CISA's problem group.

**Fix awaiting install** compares versions, not KB numbers: a ConnectSecure
patch whose normalised product matches a NinjaOne *software* patch that is
approved or manual on the same device, at or above the target version, is work
already queued. An OS update stays unmatched unless its fix text names a KB,
which the pilot's never do. The match rate is an internal fact: the engineer
sees how much of the reconciliation bit, the customer never does.

Uninstall remediations are their own finding (software to remove); "run
remediation script" is an engineer action and never a client recommendation.

Vulnerabilities are state, not events: every figure is as-of the scan it came
from (the scan dates are facts too), and the review's stored facts are the
monthly snapshot the next review compares against. Nothing goes to the daily
collector.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

from app.connectors.connectsecure.client import ConnectSecureClient
from app.connectors.connectsecure.models import (
    CsRemediation,
    CsVulnerability,
    kb_numbers,
    normalise_product,
    risk_grade,
    version_at_least,
)
from app.connectors.connectsecure.tools import (
    STATUS_CURRENT,
    STATUS_NEVER_SCANNED,
    Remediation,
    group_remediations,
    scan_status,
    top_share,
)
from app.connectors.factory import connector_for
from app.connectors.matching import normalise_hostname
from app.connectors.ninjaone.client import QUERY_PATH, NinjaOneClient
from app.coverage.config import connectsecure_endpoints
from app.models.enums import ConnectorName
from app.reports.tbr.model import Fact, FactSet, FactTable, Gap
from app.reports.tbr.security import (
    GROUP_NINJA_COMPUTERS,
    GROUP_NINJA_WAITING_KB,
    assess_review_site,
    record_rows,
)
from app.settings import get_settings

if TYPE_CHECKING:
    from app.reports.tbr.collect import Collector

#: Group keys, so the rules and the tests name them once.
GROUP_CS_ASSETS = "connectsecure.assets"
#: The coverage board's own verdict on each asset (`app/coverage/config.py`),
#: recorded so the review's join reads exactly what the board reads.
GROUP_CS_TOOL_STALE = "connectsecure.coverage.stale"
GROUP_CS_TOOL_ABSENT = "connectsecure.coverage.absent"
GROUP_CS_TOOL_DISCOVERED = "connectsecure.coverage.discovered"
GROUP_CS_TOOL_PROBE = "connectsecure.coverage.probe"
GROUP_CS_MACS = "connectsecure.macs.{name}"
#: Per name several assets carry: one entry per row (`security.record_rows`).
GROUP_CS_ROWS = "connectsecure.rows.{name}"
GROUP_CS_SCANNED = "connectsecure.assets.scanned_recently"
GROUP_CS_STALE = "connectsecure.assets.stale"
GROUP_CS_CRITICAL = "connectsecure.assets.critical"
GROUP_CS_KNOWN_EXPLOITED = "connectsecure.assets.known_exploited"
GROUP_CS_AWAITING_INSTALL = "connectsecure.assets.fix_awaiting_install"
GROUP_CS_UNINSTALL = "connectsecure.assets.software_to_remove"
GROUP_SCAN_MATCHED = "coverage.scan.matched"
GROUP_SCAN_NINJA_ONLY = "coverage.scan.ninjaone_only"
GROUP_SCAN_UNCOVERED = "coverage.scan.uncovered"

#: NinjaOne's software-patch waiting list, as `collect_software_patch_queue`
#: records it: one group per device, holding the queued patch titles.
PATCH_QUEUE = "ninjaone.software_patches.queued"

CONNECTSECURE_GAP = "ConnectSecure vulnerability scan"
PATCH_QUEUE_GAP = "NinjaOne software patch queue"

#: How many fixes the "a few updates resolve most" figure counts.
TOP_FIXES = 3


def _group(facts: FactSet, key: str, names: set[str]) -> None:
    facts.groups[key] = sorted(name for name in names if name)


def _date(value: dt.datetime | None) -> str:
    return f"{value:%d %b %Y}".replace(" 0", " ") if value else "unknown"


class QueuedPatch:
    """One NinjaOne software patch waiting on one device.

    NinjaOne's software-patch rows carry the product and version together in
    the title ("Google Chrome 129.0"), which is what ConnectSecure's product
    and target version are compared against.
    """

    __slots__ = ("device", "product", "title")

    def __init__(self, device: str, title: str) -> None:
        self.device = device
        self.title = title
        self.product = normalise_product(title)


def awaiting_install(
    remediation: CsRemediation, device: str | None, queue: dict[str, list[QueuedPatch]]
) -> bool:
    """True when NinjaOne already has this very update queued on this very device.

    A match needs the same device (by normalised hostname), the same normalised
    product, and a queued version at or above ConnectSecure's target. An OS
    update matches only through a KB number in both texts, which the pilot's
    fix texts never carry, so it stays unmatched and remains an open finding.
    """
    if not device or remediation.kind != "patch":
        return False
    queued = queue.get(normalise_hostname(device), [])
    if not queued:
        return False
    wanted_kbs = remediation.kbs
    if wanted_kbs:
        return any(wanted_kbs & kb_numbers(patch.title) for patch in queued)
    product = remediation.product_key
    if not product:
        return False
    return any(
        patch.product == product and version_at_least(patch.title, remediation.fix)
        for patch in queued
    )


async def collect_software_patch_queue(run: Collector) -> None:
    """NinjaOne's approved and manual *software* patches, per device, for the reconciliation.

    The OS waiting list is `collect_patch_coverage`'s; this is the third-party
    software list, which is what a ConnectSecure fix names (a product and a
    version). Rejected patches are not queued work and are left out.
    """
    async with connector_for(run.client_id, ConnectorName.NINJAONE, run.site_id) as c:
        assert isinstance(c, NinjaOneClient)

        async def read() -> list[Any]:
            found: list[Any] = []
            for status in ("APPROVED", "MANUAL"):
                raw, _, _ = await c.query_scoped("software-patches", params={"status": status})
                rows = [
                    row
                    for row in raw
                    if isinstance(row, dict) and (row.get("status") or "").upper() == status
                ]
                found.extend(
                    await c.place_by_device(
                        rows,
                        [row.get("deviceId") for row in rows],
                        endpoint=QUERY_PATH.format(report="software-patches"),
                    )
                )
            return found

        placed = await run.audit.read(
            "ninjaone.software_patches",
            ConnectorName.NINJAONE,
            read,
            summarise=lambda rows: {"rows": len(rows)},
        )
    queue: dict[str, list[str]] = defaultdict(list)
    for _, device, row in placed:
        title = str(row.get("title") or "")
        if device.name and title:
            queue[device.name].append(title)
    run.facts.groups.update({f"{PATCH_QUEUE}.{name}": titles for name, titles in queue.items()})
    run.fact(
        "ninjaone.software_patches.queued_devices",
        "Devices with a third-party software update waiting in NinjaOne",
        len(queue),
        "Devices with at least one software patch approved or set to manual in NinjaOne and not "
        "yet installed. A ConnectSecure fix for the same product at or below the queued version "
        "is work already under way, not a new gap.",
        "ninjaone",
    )


def _kb_of(group_key: str) -> str | None:
    """`ninjaone.patching.waiting.KB5030219` -> `KB5030219`."""
    prefix = GROUP_NINJA_WAITING_KB.format(kb="")
    return group_key[len(prefix) :] if group_key.startswith(prefix) else None


def _patch_queue(facts: FactSet) -> dict[str, list[QueuedPatch]]:
    """The queue groups back as `normalised hostname -> queued patches`."""
    queue: dict[str, list[QueuedPatch]] = defaultdict(list)
    prefix = f"{PATCH_QUEUE}."
    for key, titles in facts.groups.items():
        if key.startswith(prefix):
            device = key[len(prefix) :]
            queue[normalise_hostname(device)].extend(QueuedPatch(device, title) for title in titles)
            continue
        # NinjaOne's OS waiting list, recorded per KB by `collect_patch_coverage`:
        # an OS update matches only by KB, and only if the vendor names one.
        kb = _kb_of(key)
        if kb:
            for device in titles:
                queue[normalise_hostname(device)].append(QueuedPatch(device, kb))
    return queue


async def collect_connectsecure(run: Collector) -> None:
    stale_days = get_settings().connectsecure_stale_scan_days
    async with connector_for(run.client_id, ConnectorName.CONNECTSECURE, run.site_id) as c:
        assert isinstance(c, ConnectSecureClient)
        assets, assets_cut = await run.audit.read(
            "connectsecure.assets",
            ConnectorName.CONNECTSECURE,
            c.list_assets,
            summarise=lambda r: {"assets": len(r[0]), "truncated": r[1]},
        )
        findings, findings_cut = await run.audit.read(
            "connectsecure.vulnerabilities",
            ConnectorName.CONNECTSECURE,
            lambda: c.list_vulnerabilities(with_known_exploited=True),
            summarise=lambda r: {"confirmed_findings": len(r[0]), "truncated": r[1]},
        )
        everything, everything_cut = await run.audit.read(
            "connectsecure.vulnerabilities.both_populations",
            ConnectorName.CONNECTSECURE,
            lambda: c.list_vulnerabilities(include_unconfirmed=True, with_fixes=False),
            summarise=lambda r: {"all_findings": len(r[0]), "truncated": r[1]},
        )
        remediations, remediations_cut = await run.audit.read(
            "connectsecure.remediations",
            ConnectorName.CONNECTSECURE,
            c.list_remediations,
            summarise=lambda r: {"remediations": len(r[0]), "truncated": r[1]},
        )
        stats = await run.audit.read(
            "connectsecure.company_stats",
            ConnectorName.CONNECTSECURE,
            c.company_stats,
            summarise=lambda r: {"companies": len(r)},
        )
        requests_today = c.requests_today
    if assets_cut or findings_cut or remediations_cut or everything_cut:
        run.facts.gaps.append(
            Gap(
                what=CONNECTSECURE_GAP,
                why="A ConnectSecure listing stopped at the row cap, so every count would be "
                "partial.",
                source="connectsecure",
            )
        )
        return
    run.fact(
        "connectsecure.requests_today",
        "ConnectSecure API requests made today",
        requests_today,
        "Requests this process has made to the tenant today, against the vendor's limit of "
        "30,000 a day per key. A floor, not the vendor's own count.",
        "connectsecure",
        internal_only=True,
    )

    # --- assets and scan coverage -------------------------------------------------
    asset_rows = [asset for _, asset in assets]
    run.facts.names["connectsecure_assets"] = sorted(
        {a.host_name for a in asset_rows if a.host_name}
    )
    statuses = {
        (asset.host_name or asset.id): scan_status(asset, now=run.now, stale_days=stale_days)
        for asset in asset_rows
    }
    scanned = {name for name, status in statuses.items() if status == STATUS_CURRENT}
    never = {name for name, status in statuses.items() if status == STATUS_NEVER_SCANNED}
    stale = set(statuses) - scanned - never
    scans = [a.last_scan for a in asset_rows if a.last_scan]

    run.fact(
        "connectsecure.assets.total",
        "Assets in ConnectSecure",
        len(asset_rows),
        "Machines ConnectSecure holds for this customer, scanned or not.",
        "connectsecure",
    )
    run.fact(
        "connectsecure.assets.scanned_recently",
        f"Assets scanned in the last {stale_days} days",
        len(scanned),
        f"Assets whose agent is live and whose last scan is within {stale_days} days of the "
        "report being generated.",
        "connectsecure",
    )
    run.fact(
        "connectsecure.assets.stale",
        f"Assets not scanned for {stale_days} days or more",
        len(stale),
        f"Assets whose last scan is older than {stale_days} days, or whose agent is deprecated, "
        "retired or has stopped reporting: their findings are as old as that scan.",
        "connectsecure",
    )
    run.fact(
        "connectsecure.assets.never_scanned",
        "Assets never scanned",
        len(never),
        "Assets ConnectSecure holds with a live agent and no scan on record at all.",
        "connectsecure",
    )
    run.fact(
        "connectsecure.scan.latest",
        "Most recent vulnerability scan",
        _date(max(scans) if scans else None),
        "The newest scan date among this customer's assets. Every vulnerability figure in this "
        "review is as of the scans, not the generation date.",
        "connectsecure",
    )
    run.fact(
        "connectsecure.scan.oldest",
        "Oldest vulnerability scan still counted",
        _date(min(scans) if scans else None),
        "The oldest scan date among this customer's scanned assets.",
        "connectsecure",
    )
    _group(run.facts, GROUP_CS_ASSETS, set(statuses))
    _group(run.facts, GROUP_CS_SCANNED, scanned)
    _group(run.facts, GROUP_CS_STALE, stale | never)
    judged = connectsecure_endpoints(assets, now=run.now)
    _group(run.facts, GROUP_CS_TOOL_STALE, {e.name or "" for e in judged if e.state == "stale"})
    _group(run.facts, GROUP_CS_TOOL_ABSENT, {e.name or "" for e in judged if e.state == "absent"})
    _group(run.facts, GROUP_CS_TOOL_DISCOVERED, {e.name or "" for e in judged if e.discovered})
    _group(run.facts, GROUP_CS_TOOL_PROBE, {e.name or "" for e in judged if e.probe})
    for judged_asset in judged:
        if judged_asset.name and judged_asset.macs:
            run.facts.groups[GROUP_CS_MACS.format(name=judged_asset.name)] = sorted(
                judged_asset.macs
            )
    record_rows(run.facts, GROUP_CS_ROWS, judged)
    if stale or never:
        run.facts.tables["connectsecure.assets.stale.devices"] = FactTable(
            id="connectsecure.assets.stale.devices",
            label=f"Assets not scanned in {stale_days} days",
            definition="Every asset counted in connectsecure.assets.stale and .never_scanned, "
            "with its scan status.",
            source="connectsecure",
            columns=["Device", "Status"],
            rows=[[name, statuses[name]] for name in sorted(stale | never)],
            total=len(stale | never),
            as_of=run.now,
        )

    # --- findings, confirmed only -------------------------------------------------
    rows: list[CsVulnerability] = [v for _, v in findings]
    by_severity = Counter(v.severity or "unknown" for v in rows)
    by_severity_both = Counter(v.severity or "unknown" for _, v in everything)
    run.fact(
        "connectsecure.vulnerabilities.total",
        "Confirmed vulnerability findings",
        len(rows),
        "One finding per vulnerability per asset, where ConnectSecure's scanner proved the "
        "vulnerable build is on the machine, as of its last scan. Findings the scanner only "
        "suspects are counted separately and never added to this.",
        "connectsecure",
        population="confirmed",
        severity="all",
    )
    run.fact(
        "connectsecure.vulnerabilities.unconfirmed",
        "Suspected findings not yet verified by a scan",
        max(len(everything) - len(rows), 0),
        "Findings ConnectSecure inferred (from a version number, say) and has not verified on "
        "the machine. They are counted nowhere else in this review: on their own they are not "
        "evidence that a vulnerability is present.",
        "connectsecure",
        population="unconfirmed",
        severity="all",
    )
    run.fact(
        "connectsecure.vulnerabilities.unconfirmed_critical",
        "Suspected critical findings not yet verified by a scan",
        max(by_severity_both.get("critical", 0) - by_severity.get("critical", 0), 0),
        "Of the findings ConnectSecure only suspects, those it would rate critical. This is the "
        "one that qualifies a critical headline: the all-severity figure counts mostly medium "
        "and high findings and reads as if they were all critical.",
        "connectsecure",
        population="unconfirmed",
        severity="critical",
    )
    for level in ("critical", "high", "medium", "low"):
        run.fact(
            f"connectsecure.vulnerabilities.{level}",
            f"Confirmed {level} findings",
            by_severity.get(level, 0),
            f"Confirmed findings whose severity ConnectSecure reports as {level} (CVSS "
            + {
                "critical": "9.0 to 10.0",
                "high": "7.0 to 8.9",
                "medium": "4.0 to 6.9",
                "low": "0.1 to 3.9",
            }[level]
            + ").",
            "connectsecure",
            population="confirmed",
            severity=level,
            denominator="connectsecure.vulnerabilities.total",
        )
    run.fact(
        "connectsecure.vulnerabilities.unique",
        "Distinct confirmed vulnerabilities",
        len({v.problem for v in rows}),
        "Distinct CVEs or checks behind the confirmed findings: one CVE on ten machines is one "
        "here.",
        "connectsecure",
        population="confirmed",
    )
    kev_rows = [v for v in rows if v.known_exploited]
    run.fact(
        "connectsecure.vulnerabilities.known_exploited",
        "Confirmed findings on CISA's Known Exploited Vulnerabilities list",
        sum(a.cisa_vul or 0 for a in asset_rows),
        "The vendor's own per-asset count of confirmed findings whose vulnerability CISA lists "
        "as exploited in the wild: these are being used by attackers now, whatever their CVSS "
        "score.",
        "connectsecure",
        population="confirmed",
    )
    run.fact(
        "connectsecure.vulnerabilities.known_exploited_unique",
        "Distinct known-exploited vulnerabilities",
        len({v.problem for v in kev_rows}),
        "Distinct CVEs behind connectsecure.vulnerabilities.known_exploited, from the confirmed "
        "read filtered to CISA's problem group.",
        "connectsecure",
        population="confirmed",
    )
    _group(
        run.facts,
        GROUP_CS_KNOWN_EXPLOITED,
        {a.host_name or "" for a in asset_rows if (a.cisa_vul or 0) > 0},
    )
    _group(
        run.facts,
        GROUP_CS_CRITICAL,
        {a.host_name or "" for a in asset_rows if (a.critical or 0) > 0 or (a.cisa_vul or 0) > 0},
    )
    run.fact(
        "connectsecure.vulnerabilities.critical_assets",
        "Assets with a confirmed critical or known-exploited finding",
        len(run.facts.groups.get(GROUP_CS_CRITICAL, [])),
        "Assets the vendor's own counters put at least one confirmed critical or CISA "
        "known-exploited finding on.",
        "connectsecure",
        population="confirmed",
        severity="critical",
    )
    if kev_rows:
        per_problem: dict[str, tuple[CsVulnerability, set[str]]] = {}
        for v in kev_rows:
            entry = per_problem.setdefault(v.problem, (v, set()))
            if v.host_name:
                entry[1].add(v.host_name)
        ordered = sorted(
            per_problem.values(), key=lambda item: (-(item[0].epss_score or 0), -len(item[1]))
        )
        run.facts.tables["connectsecure.vulnerabilities.known_exploited.list"] = FactTable(
            id="connectsecure.vulnerabilities.known_exploited.list",
            label="Known-exploited vulnerabilities",
            definition="Every distinct CISA known-exploited vulnerability the scanner confirmed, "
            "by EPSS then by devices, with the fix the vendor pairs with it.",
            source="connectsecure",
            columns=["Vulnerability", "Product", "EPSS", "Fix", "Devices"],
            rows=[
                [
                    v.problem,
                    v.product or "",
                    f"{v.epss_score:.2f}" if v.epss_score is not None else "",
                    v.fix or "fix not reported",
                    ", ".join(sorted(devices)),
                ]
                for v, devices in ordered
            ],
            total=len(ordered),
            as_of=run.now,
            population="confirmed",
        )
    matched_fixes = sum(1 for v in rows if v.fix)
    run.fact(
        "connectsecure.vulnerabilities.fix_reported",
        "Confirmed findings the vendor pairs a fix with",
        matched_fixes,
        "Confirmed findings joined to a remediation row on the same asset for the same "
        "normalised product. The rest say 'fix not reported'; nothing is guessed.",
        "connectsecure",
        population="confirmed",
        internal_only=True,
    )

    # --- fixes, and what NinjaOne already has queued ------------------------------
    fixes: list[CsRemediation] = [r for _, r in remediations]
    queue = _patch_queue(run.facts)
    queued_flags = [awaiting_install(r, r.host_name, queue) for r in fixes]
    queued = [r for r, is_queued in zip(fixes, queued_flags, strict=True) if is_queued]
    open_fixes = [r for r, is_queued in zip(fixes, queued_flags, strict=True) if not is_queued]
    patches = group_remediations([r for r in open_fixes if r.kind == "patch"])
    uninstalls = group_remediations([r for r in fixes if r.kind == "uninstall"])
    scripts = group_remediations([r for r in fixes if r.kind == "script"])
    run.fact(
        "connectsecure.remediations.total",
        "Distinct fixes ConnectSecure recommends",
        len(patches) + len(uninstalls) + len(scripts),
        "Remediations (update a product to a version, remove it, or run the vendor's script) "
        "that would resolve the confirmed findings; one fix closes many on many devices.",
        "connectsecure",
        population="confirmed",
    )
    run.fact(
        "connectsecure.remediations.awaiting_install",
        "Fixes NinjaOne already has queued",
        len(queued),
        "ConnectSecure updates whose product matches a NinjaOne software patch approved or set "
        "to manual on that same device, at or above the target version: work already under way, "
        "not a new gap. An OS update matches only by KB number, which this vendor's fix text "
        "does not carry.",
        "connectsecure",
        population="confirmed",
    )
    rate = round(100 * len(queued) / len(fixes), 1) if fixes else 0.0
    run.fact(
        "connectsecure.remediations.awaiting_install_percent",
        "Share of fixes already queued in NinjaOne",
        rate,
        "connectsecure.remediations.awaiting_install as a percentage of every (asset, fix) row "
        "ConnectSecure reported: how much of the reconciliation with NinjaOne's software patch "
        "queue actually matched. For the engineer, to judge the reconciliation; never for the "
        "customer.",
        "connectsecure",
        display=f"{rate:g}%",
        population="confirmed",
        internal_only=True,
    )
    _group(run.facts, GROUP_CS_AWAITING_INSTALL, {r.host_name or "" for r in queued})
    _group(
        run.facts, GROUP_CS_UNINSTALL, {r.host_name or "" for r in fixes if r.kind == "uninstall"}
    )
    if uninstalls:
        run.fact(
            "connectsecure.remediations.uninstall",
            "Products ConnectSecure says to remove",
            len(uninstalls),
            "Remediations whose action is Uninstall: software that is no longer supported or "
            "should not be there, where the fix is removal, not an update.",
            "connectsecure",
            population="confirmed",
        )
        run.facts.tables["connectsecure.remediations.uninstall.list"] = FactTable(
            id="connectsecure.remediations.uninstall.list",
            label="Software to remove",
            definition="Every remediation whose action is Uninstall, with the devices it is on.",
            source="connectsecure",
            columns=["Software", "Findings", "Devices"],
            rows=[
                [g.product or g.instruction, str(g.total), str(len(g.devices))] for g in uninstalls
            ],
            total=len(uninstalls),
            as_of=run.now,
            population="confirmed",
        )
    if scripts:
        # An engineer action, not something to put to the customer.
        run.fact(
            "connectsecure.remediations.scripts",
            "Fixes needing the vendor's remediation script",
            len(scripts),
            "Remediations whose action is 'Run remediation script': work for an engineer in the "
            "ConnectSecure console, not a service to recommend to the customer.",
            "connectsecure",
            population="confirmed",
            internal_only=True,
        )
    share = top_share(patches, top=TOP_FIXES)
    if share is not None:
        run.fact(
            "connectsecure.remediations.top_count",
            "The fixes that resolve most critical findings",
            min(TOP_FIXES, len(patches)),
            f"The {TOP_FIXES} best-ranked updates (confirmed critical findings resolved, then "
            "high, then total), or fewer if fewer exist. Fixes NinjaOne already has queued are "
            "left out.",
            "connectsecure",
            population="confirmed",
        )
        run.fact(
            "connectsecure.remediations.top_critical_resolved",
            "Critical findings the top fixes resolve",
            sum(g.critical for g in patches[:TOP_FIXES]),
            "Confirmed critical findings the best-ranked updates resolve, as a count. The "
            "customer's sentence states this against connectsecure.vulnerabilities.critical_open, "
            "the number printed beside it, so the reader can see what it is out of.",
            "connectsecure",
            population="confirmed",
            severity="critical",
            denominator="connectsecure.vulnerabilities.critical_open",
        )
        run.fact(
            "connectsecure.remediations.top_critical_percent",
            "Share of critical findings the top fixes resolve",
            share,
            "Confirmed critical findings the best-ranked updates resolve, as a percentage of "
            "the confirmed critical findings every recommended update resolves. That "
            "denominator is a filtered subset and is not a fact on the deck, so the figure "
            "reads as a share of the headline critical total when it is not. For the engineer "
            "ranking the work; never for the customer.",
            "connectsecure",
            display=f"{share:g}%",
            population="confirmed",
            severity="critical",
            internal_only=True,
        )
        run.facts.tables["connectsecure.remediations.top"] = FactTable(
            id="connectsecure.remediations.top",
            label="Updates that resolve the most findings",
            definition="Every recommended update, best-ranked first, with the confirmed findings "
            "it resolves by severity and the devices it applies to. Updates NinjaOne already has "
            "queued are not here.",
            source="connectsecure",
            columns=["Update", "Critical", "High", "Total", "Devices"],
            rows=[
                [g.instruction, str(g.critical), str(g.high), str(g.total), str(len(g.devices))]
                for g in patches
            ],
            total=len(patches),
            as_of=run.now,
            population="confirmed",
        )
    run.fact(
        "connectsecure.vulnerabilities.critical_open",
        "Confirmed critical findings with no fix under way",
        sum(g.critical for g in patches),
        "Confirmed critical findings resolved by an update NinjaOne does not already have "
        "queued for that device.",
        "connectsecure",
        population="confirmed",
        severity="critical",
    )

    # --- the company's score ------------------------------------------------------
    for _, stat in stats:
        if stat.risk_score is None:
            continue
        run.fact(
            "connectsecure.company.risk_score",
            "ConnectSecure company risk score",
            stat.risk_score,
            "The vendor's company risk score, 0 to 100: the mean of the assets' maximum risk "
            "scores, computed from confirmed findings. Higher is worse; A is 0 to 40, F is 90 "
            "to 100 (docs/api-notes/connectsecure.md).",
            "connectsecure",
            display=f"{stat.risk_score:g}",
            population="confirmed",
        )
        run.fact(
            "connectsecure.company.risk_grade",
            "ConnectSecure risk grade",
            risk_grade(stat.risk_score) or "unknown",
            "The letter grade the vendor assigns to the company risk score: A (very low) to "
            "F (very critical).",
            "connectsecure",
            population="confirmed",
        )
        break


def derive_scan_coverage(run: Collector) -> None:
    """NinjaOne computers against ConnectSecure assets, by hostname within the site.

    The join is the coverage board's (`app/coverage/core.py`), fed the same
    population and the same per-asset verdicts, so the deck's "uncovered"
    count is the board's absent plus stale for ConnectSecure. Only when both
    tools answered.
    """
    facts = run.facts
    if facts.get("connectsecure.assets.total") is None or GROUP_NINJA_COMPUTERS not in facts.groups:
        return
    result = assess_review_site(
        run,
        tool="connectsecure",
        tool_names=facts.groups.get(GROUP_CS_ASSETS, []),
        stale_names=facts.groups.get(GROUP_CS_TOOL_STALE, []),
        absent_names=facts.groups.get(GROUP_CS_TOOL_ABSENT, []),
        discovered_names=facts.groups.get(GROUP_CS_TOOL_DISCOVERED, []),
        probe_names=facts.groups.get(GROUP_CS_TOOL_PROBE, []),
        mac_group=GROUP_CS_MACS,
        rows_group=GROUP_CS_ROWS,
    )
    states = {d.device.name: d.tools["connectsecure"].state for d in result.devices}
    matched = [name for name, state in states.items() if state in ("covered", "stale")]
    ninja_only = [name for name, state in states.items() if state == "absent"]
    uncovered = sorted(name for name, state in states.items() if state in ("absent", "stale"))
    agent_down = [name for name, state in states.items() if state == "rmm_agent_down"]
    external = [u.name for u in result.external if u.tool == "connectsecure"]
    stale_days = get_settings().connectsecure_stale_scan_days
    for fact_id, label, value, definition in (
        (
            "coverage.scan.matched",
            "Computers known to both NinjaOne and ConnectSecure",
            len(matched),
            "NinjaOne workstations, servers and VM guests whose hostname matches a "
            "ConnectSecure asset of the same customer.",
        ),
        (
            "coverage.scan.ninjaone_only",
            "Computers in NinjaOne with no ConnectSecure asset",
            len(ninja_only),
            "NinjaOne computers the vulnerability scanner has never seen.",
        ),
        (
            "coverage.scan.uncovered",
            f"Managed computers not vulnerability-scanned in {stale_days} days",
            len(uncovered),
            "NinjaOne computers with no ConnectSecure asset, plus those whose asset was not "
            f"scanned in the last {stale_days} days or has no live agent. The same count the "
            "coverage board shows as absent plus stale.",
        ),
        (
            "coverage.scan.rmm_agent_down",
            "Computers the scanner has seen recently that NinjaOne is not hearing from",
            len(agent_down),
            "NinjaOne computers with no NinjaOne contact in the coverage threshold whose "
            "ConnectSecure asset was scanned within it: the machine is alive and the RMM "
            "agent is down. The same devices the coverage board shows as rmm_agent_down.",
        ),
        (
            "connectsecure.assets.external",
            "ConnectSecure assets that are external scan targets",
            len(external),
            "Assets whose name is an IP address or an ISP's reverse-DNS name: the outside of "
            "the customer's network as the scanner sees it, never counted as unmanaged "
            "endpoints and never joined to a NinjaOne computer.",
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
    _group(facts, GROUP_SCAN_MATCHED, set(matched))
    _group(facts, GROUP_SCAN_NINJA_ONLY, set(ninja_only))
    _group(facts, GROUP_SCAN_UNCOVERED, set(uncovered))
    if uncovered:
        why: dict[str, str] = defaultdict(lambda: "not scanned recently")
        for name in ninja_only:
            why[name] = "no ConnectSecure asset"
        facts.tables["coverage.scan.uncovered.devices"] = FactTable(
            id="coverage.scan.uncovered.devices",
            label="Managed computers the scanner does not cover",
            definition="See coverage.scan.uncovered.",
            source="derived",
            columns=["Device", "Why"],
            rows=[[name, why[name]] for name in uncovered],
            total=len(uncovered),
            as_of=run.now,
        )


__all__ = [
    "CONNECTSECURE_GAP",
    "GROUP_CS_ASSETS",
    "GROUP_CS_AWAITING_INSTALL",
    "GROUP_CS_CRITICAL",
    "GROUP_CS_KNOWN_EXPLOITED",
    "GROUP_CS_SCANNED",
    "GROUP_CS_STALE",
    "GROUP_CS_UNINSTALL",
    "GROUP_SCAN_MATCHED",
    "GROUP_SCAN_NINJA_ONLY",
    "GROUP_SCAN_UNCOVERED",
    "PATCH_QUEUE",
    "PATCH_QUEUE_GAP",
    "QueuedPatch",
    "Remediation",
    "awaiting_install",
    "collect_connectsecure",
    "collect_software_patch_queue",
    "derive_scan_coverage",
]

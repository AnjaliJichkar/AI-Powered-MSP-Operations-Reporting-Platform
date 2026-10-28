"""ConnectSecure in the review: the collector, the join with NinjaOne, the rules, the scorecard.

The collector tests run the real connectors against respx-mocked vendors (the
documentation-shaped ConnectSecure fixtures, the scrubbed NinjaOne ones). The
rule tests write facts by hand.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx

from app.connectors.base import BaseConnector, ConnectorLimits
from app.connectors.errors import ConnectorNotConfigured
from app.connectors.factory import ConnectorFactory, set_factory
from app.models.enums import ConnectorName
from app.reports.tbr.audit import NullAudit
from app.reports.tbr.collect import Collector
from app.reports.tbr.config import FranchiseeConfig
from app.reports.tbr.findings import derive_findings
from app.reports.tbr.model import FactSet, ReviewPeriod
from app.reports.tbr.narrative import problems_with
from app.reports.tbr.scorecard import evaluate
from app.reports.tbr.security import (
    GROUP_NINJA_COMPUTERS,
    GROUP_NINJA_WAITING_KB,
    collect_patch_coverage,
)
from app.reports.tbr.vulnerabilities import (
    GROUP_CS_ASSETS,
    GROUP_CS_AWAITING_INSTALL,
    GROUP_CS_KNOWN_EXPLOITED,
    GROUP_CS_STALE,
    GROUP_CS_UNINSTALL,
    GROUP_SCAN_UNCOVERED,
    QueuedPatch,
    awaiting_install,
    collect_connectsecure,
    collect_software_patch_queue,
    derive_scan_coverage,
)
from tests.connectors.conftest import (
    CONNECTSECURE_BASE_URL,
    NINJA_BASE_URL,
    load_fixture,
    make_connectsecure_client,
    make_ninja_client,
    single_page,
)
from tests.connectors.test_connectsecure import answer as answer_connectsecure
from tests.reports.conftest import PERIOD, fact, make_config, make_document, make_facts

AUGUST = ReviewPeriod.ending(2026, 8)
LIMITS = ConnectorLimits(
    timeout_seconds=5.0,
    connect_timeout_seconds=2.0,
    max_retries=1,
    backoff_base_seconds=0.01,
    backoff_max_seconds=0.05,
    page_size=100,
    max_pages=5,
    jitter=False,
)


class TwoConnectorFactory(ConnectorFactory):
    def __init__(self, ninja: BaseConnector, connectsecure: BaseConnector) -> None:
        self.by_name = {ConnectorName.NINJAONE: ninja, ConnectorName.CONNECTSECURE: connectsecure}

    @asynccontextmanager
    async def open(self, client_id: Any, connector_name: ConnectorName, site_id: Any = None) -> Any:
        if connector_name not in self.by_name:
            raise ConnectorNotConfigured(
                f"no {connector_name.value} credentials in this test",
                connector=connector_name.value,
            )
        async with self.by_name[connector_name] as connector:
            yield connector


@pytest.fixture
def connectors() -> Any:
    ninja = make_ninja_client(limits=LIMITS)
    connectsecure = make_connectsecure_client(limits=LIMITS)
    previous = set_factory(TwoConnectorFactory(ninja, connectsecure))
    yield ninja, connectsecure
    set_factory(previous)


def collector() -> Collector:
    return Collector(
        client_id=str(uuid.uuid4()),
        site_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, "acme-dental")),
        period=AUGUST,
        config=FranchiseeConfig(display_name="Example IT Partners"),
        audit=NullAudit(),
        now=dt.datetime(2026, 9, 22, 9, tzinfo=dt.UTC),
    )


def mock_ninja() -> None:
    respx.post(f"{NINJA_BASE_URL}/ws/oauth/token").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )
    respx.get(f"{NINJA_BASE_URL}/v2/devices").mock(
        side_effect=single_page(load_fixture("ninjaone", "devices.json"))
    )
    for report in (
        "computer-systems",
        "processors",
        "operating-systems",
        "os-patch-installs",
        "software-patch-installs",
        "logged-on-users",
        "network-interfaces",
    ):
        respx.get(f"{NINJA_BASE_URL}/v2/queries/{report}").mock(
            return_value=httpx.Response(200, json=load_fixture("ninjaone", "query_empty.json"))
        )

    def waiting(request: httpx.Request) -> httpx.Response:
        # KB5030219 is approved on DC01 only: the same fix on WS14 is not under way.
        rows = (
            [{"id": "p1", "name": "2026-09 Cumulative Update KB5030219", "kbNumber": "KB5030219",
              "status": "APPROVED", "severity": "CRITICAL", "deviceId": 1001}]
            if request.url.params["status"] == "APPROVED"
            else []
        )  # fmt: skip
        return httpx.Response(200, json={"results": rows})

    respx.get(f"{NINJA_BASE_URL}/v2/queries/os-patches").mock(side_effect=waiting)

    def software(request: httpx.Request) -> httpx.Response:
        # Firefox 156.0 is approved on DC01 (1001) and not on WS14 (1002), so one of the
        # two Firefox fixes is already queued and the other is not.
        rows = (
            [{"id": "s1", "title": "Mozilla Firefox 156.0", "status": "APPROVED",
              "productIdentifier": "ff", "deviceId": 1001}]
            if request.url.params["status"] == "APPROVED"
            else []
        )  # fmt: skip
        return httpx.Response(200, json={"results": rows})

    respx.get(f"{NINJA_BASE_URL}/v2/queries/software-patches").mock(side_effect=software)


def mock_connectsecure() -> None:
    respx.post(f"{CONNECTSECURE_BASE_URL}/w/authorize").mock(
        return_value=httpx.Response(200, json=load_fixture("connectsecure", "authorize.json"))
    )
    respx.get(url__startswith=CONNECTSECURE_BASE_URL).mock(side_effect=answer_connectsecure)


# --- the collector -------------------------------------------------------------------------------


@respx.mock
async def test_connectsecure_findings_fixes_scan_dates_and_the_join(connectors: Any) -> None:
    mock_ninja()
    mock_connectsecure()
    run = collector()
    await run._ninja_inventory()
    await collect_patch_coverage(run)
    await collect_software_patch_queue(run)
    assert run.facts.groups[GROUP_NINJA_WAITING_KB.format(kb="KB5030219")] == ["TL-AUS-DC01"]
    assert run.facts.facts["ninjaone.software_patches.queued_devices"].value == 1

    await collect_connectsecure(run)
    derive_scan_coverage(run)

    facts = run.facts
    assert facts.facts["connectsecure.assets.total"].value == 5
    # WS07's agent is deprecated and MAC03's has stopped reporting; the rest are current.
    assert facts.facts["connectsecure.assets.scanned_recently"].value == 3
    assert facts.facts["connectsecure.assets.stale"].value == 2
    assert facts.facts["connectsecure.assets.never_scanned"].value == 0
    assert [row[0] for row in facts.tables["connectsecure.assets.stale.devices"].rows] == [
        "TL-AUS-MAC03",
        "TL-AUS-WS07",
    ]
    assert facts.facts["connectsecure.scan.latest"].value != "unknown"
    assert facts.facts["connectsecure.vulnerabilities.total"].value == 7
    assert facts.facts["connectsecure.vulnerabilities.critical"].value == 3
    assert facts.facts["connectsecure.vulnerabilities.unique"].value == 6
    # The known-exploited count is the assets' own `cisa_vul`; the CVEs come from the
    # group-34 read, which names one CVE on two assets.
    assert facts.facts["connectsecure.vulnerabilities.known_exploited"].value == sum(
        a["cisa_vul"] for a in load_fixture("connectsecure", "assets.json")["data"]
    )
    assert facts.facts["connectsecure.vulnerabilities.known_exploited_unique"].value == 1
    assert (
        facts.tables["connectsecure.vulnerabilities.known_exploited.list"]
        .rows[0][0]
        .startswith("CVE-")
    )
    # Firefox 156.0 is approved in NinjaOne for DC01 only, so one of the two Firefox fixes
    # is queued and the other is not; nothing else matches. One of seven rows.
    assert facts.facts["connectsecure.remediations.awaiting_install"].value == 1
    assert facts.facts["connectsecure.remediations.awaiting_install_percent"].display == "14.3%"
    assert facts.groups[GROUP_CS_AWAITING_INSTALL] == ["TL-AUS-DC01"]
    assert facts.facts["connectsecure.remediations.uninstall"].value == 1
    assert facts.groups[GROUP_CS_UNINSTALL] == ["TL-AUS-WS22"]
    assert facts.facts["connectsecure.remediations.scripts"].value == 1
    assert facts.facts["connectsecure.remediations.top_count"].value == 3
    assert facts.tables["connectsecure.remediations.top"].rows[0][0].startswith("Update ")
    assert facts.facts["connectsecure.vulnerabilities.fix_reported"].value >= 1
    assert facts.facts["connectsecure.requests_today"].value > 0
    assert facts.facts["connectsecure.company.risk_score"].value is not None
    assert facts.facts["connectsecure.company.risk_grade"].value in "ABCDEF"
    assert facts.names["connectsecure_assets"][0] == "TL-AUS-DC01"  # a demo render replaces these
    # The join: five NinjaOne computers, five assets. MAC03 has never contacted
    # NinjaOne, so it is rmm_stale and out of the join (its own line on the
    # board, never "not scanned"); the other four pair, and WS07 (deprecated
    # agent) is the one not covered.
    assert facts.facts["coverage.scan.matched"].value == 4
    assert facts.facts["coverage.scan.ninjaone_only"].value == 0
    assert facts.facts["coverage.scan.uncovered"].value == 1
    assert facts.groups[GROUP_SCAN_UNCOVERED] == ["TL-AUS-WS07"]
    kinds = [row[2].get("tool_name") for row in run.audit.rows]  # type: ignore[attr-defined]
    assert (
        "tbr.connectsecure.vulnerabilities" in kinds and "tbr.connectsecure.remediations" in kinds
    )


@respx.mock
async def test_a_site_with_no_connectsecure_company_records_a_gap_not_a_zero(
    connectors: Any,
) -> None:
    from app.reports.tbr.vulnerabilities import CONNECTSECURE_GAP

    mock_ninja()
    respx.get(f"{NINJA_BASE_URL}/v2/queries/antivirus-status").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "query_empty.json"))
    )
    respx.get(f"{NINJA_BASE_URL}/v2/queries/volumes").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "query_empty.json"))
    )
    respx.get(f"{NINJA_BASE_URL}/v2/activities").mock(
        return_value=httpx.Response(200, json={"lastActivityId": 1, "activities": []})
    )
    respx.get(url__regex=r".*/v2/organization/\d+/locations").mock(
        return_value=httpx.Response(200, json=[])
    )
    scanner = respx.get(url__startswith=CONNECTSECURE_BASE_URL)
    run = collector()
    run.bitdefender_mapped = False
    run.connectsecure_mapped = False

    facts = await run.collect()  # Autotask is not stubbed: its collectors fail into gaps

    assert scanner.call_count == 0
    assert CONNECTSECURE_GAP in [g.what for g in facts.gaps]
    assert "connectsecure.assets.total" not in facts.facts
    assert "coverage.scan.uncovered" not in facts.facts


def test_awaiting_install_compares_product_and_version_on_the_same_device() -> None:
    from app.connectors.connectsecure.models import CsRemediation

    queue = {
        "tl-aus-dc01": [
            QueuedPatch("TL-AUS-DC01", "Mozilla Firefox 156.0"),
            QueuedPatch("TL-AUS-DC01", "2026-09 Cumulative Update KB5030219"),
        ]
    }

    def fix(product: str, version: str, action: str = "Software Patch") -> CsRemediation:
        return CsRemediation.model_validate(
            {"solution_id": 1, "product": product, "fix": version, "remediation_action": action}
        )

    assert awaiting_install(fix("Mozilla Firefox (x64 en-US)", "156.0"), "TL-AUS-DC01", queue)
    # The queued version is newer than the target: still covered.
    assert awaiting_install(fix("Mozilla Firefox", "155.0"), "tl-aus-dc01.acme.local", queue)
    # Older than the target, another device, another product: none of them count.
    assert not awaiting_install(fix("Mozilla Firefox", "157.0"), "TL-AUS-DC01", queue)
    assert not awaiting_install(fix("Mozilla Firefox", "156.0"), "TL-AUS-WS14", queue)
    assert not awaiting_install(fix("Google Chrome", "129.0"), "TL-AUS-DC01", queue)
    # An OS update matches only by KB, and only when the vendor names one.
    assert awaiting_install(fix("Windows 11", "KB5030219", "OS Update"), "TL-AUS-DC01", queue)
    assert not awaiting_install(fix("Windows 11", "10.0.22631", "OS Update"), "TL-AUS-DC01", queue)
    # An uninstall is never "already queued": there is nothing to install.
    assert not awaiting_install(fix("Adobe Flash", "", "Uninstall"), "TL-AUS-DC01", queue)


# --- rules ---------------------------------------------------------------------------------------


#: What each ConnectSecure fact counts, matching `collect_connectsecure`.
SEVERITY_SCOPE: dict[str, str] = {
    "connectsecure.vulnerabilities.total": "all",
    "connectsecure.vulnerabilities.unconfirmed": "all",
    "connectsecure.vulnerabilities.unconfirmed_critical": "critical",
    "connectsecure.vulnerabilities.critical": "critical",
    "connectsecure.vulnerabilities.critical_open": "critical",
    "connectsecure.vulnerabilities.critical_assets": "critical",
    "connectsecure.remediations.top_critical_resolved": "critical",
    "connectsecure.remediations.top_critical_percent": "critical",
}
DENOMINATORS: dict[str, str] = {
    "connectsecure.vulnerabilities.critical": "connectsecure.vulnerabilities.total",
    "connectsecure.remediations.top_critical_resolved": (
        "connectsecure.vulnerabilities.critical_open"
    ),
}


def vulnerability_facts(**overrides: Any) -> FactSet:
    facts = make_facts(**overrides)
    for fact_id, value in (
        ("connectsecure.assets.total", 5),
        ("connectsecure.assets.stale", 2),
        ("connectsecure.scan.latest", "21 Sep 2026"),
        ("connectsecure.vulnerabilities.total", 7),
        ("connectsecure.vulnerabilities.critical", 3),
        ("connectsecure.vulnerabilities.unique", 5),
        ("connectsecure.vulnerabilities.known_exploited", 2),
        ("connectsecure.vulnerabilities.known_exploited_unique", 1),
        ("connectsecure.vulnerabilities.critical_open", 2),
        ("connectsecure.vulnerabilities.critical_assets", 2),
        ("connectsecure.remediations.total", 4),
        ("connectsecure.remediations.awaiting_install", 1),
        ("connectsecure.remediations.awaiting_install_percent", 14.3),
        ("connectsecure.remediations.uninstall", 1),
        ("connectsecure.remediations.top_count", 3),
        ("connectsecure.remediations.top_critical_resolved", 2),
        ("connectsecure.remediations.top_critical_percent", 100.0),
        ("connectsecure.vulnerabilities.unconfirmed", 9),
        ("connectsecure.vulnerabilities.unconfirmed_critical", 1),
        ("connectsecure.company.risk_score", 66.0),
        ("coverage.scan.matched", 5),
        ("coverage.scan.ninjaone_only", 0),
        ("coverage.scan.uncovered", 3),
    ):
        if facts.get(fact_id) is not None:
            continue
        built = fact(
            fact_id,
            value,
            source="connectsecure" if fact_id.startswith("connectsecure") else "derived",
        )
        if fact_id.startswith(("connectsecure.vulnerabilities", "connectsecure.remediations")):
            built = built.model_copy(update={"population": "confirmed"})
        if fact_id.startswith("connectsecure.vulnerabilities.unconfirmed"):
            built = built.model_copy(update={"population": "unconfirmed"})
        # The scope the live collector gives each of these, so the validator is
        # tested against the facts the review actually builds.
        if fact_id in SEVERITY_SCOPE:
            built = built.model_copy(update={"severity": SEVERITY_SCOPE[fact_id]})
        if fact_id in DENOMINATORS:
            built = built.model_copy(update={"denominator": DENOMINATORS[fact_id]})
        if fact_id.endswith(("_percent", "_share")):
            built = built.model_copy(update={"display": f"{value:g}%"})
        if fact_id.endswith(("awaiting_install_percent", "top_critical_percent")):
            built = built.model_copy(update={"internal_only": True})
        facts.add(built)
    facts.groups = {
        GROUP_NINJA_COMPUTERS: ["ACME-DC01", "ACME-WS01", "ACME-WS02", "ACME-WS03", "ACME-WS04"],
        GROUP_CS_ASSETS: ["ACME-DC01", "ACME-WS01", "ACME-WS02", "ACME-WS03", "ACME-WS04"],
        GROUP_CS_STALE: ["ACME-WS03", "ACME-WS04"],
        GROUP_CS_KNOWN_EXPLOITED: ["ACME-DC01", "ACME-WS01"],
        GROUP_CS_AWAITING_INSTALL: ["ACME-DC01"],
        GROUP_CS_UNINSTALL: ["ACME-WS02"],
        GROUP_SCAN_UNCOVERED: ["ACME-WS02", "ACME-WS03", "ACME-WS04"],
    }
    return facts


def findings_for(facts: FactSet) -> dict[str, Any]:
    found = derive_findings(
        facts, prior=None, prior_label=None, config=make_config(), period=PERIOD
    )
    return {finding.id: finding for finding in found}


def test_known_exploited_is_a_critical_measurement_ranked_first_without_the_awaiting_device() -> (
    None
):
    facts = vulnerability_facts()
    found = findings_for(facts)
    finding = found["security.vulnerabilities.known_exploited"]
    assert finding.severity == "critical" and finding.assertable
    assert finding.section == "cybersecurity"
    assert facts.facts["security.vulnerabilities.known_exploited_devices"].value == 1  # WS01
    assert facts.tables["security.vulnerabilities.known_exploited_devices.devices"].rows == [
        ["ACME-WS01"]
    ]
    ordered = [
        f.id
        for f in derive_findings(
            facts, prior=None, prior_label=None, config=make_config(), period=PERIOD
        )
    ]
    assert ordered[0] == "security.vulnerabilities.known_exploited"


def test_top_fixes_and_critical_findings_are_measurements_awaiting_install_is_low() -> None:
    found = findings_for(vulnerability_facts())
    top = found["security.vulnerabilities.top_fixes"]
    assert top.severity == "high" and top.assertable
    # Counts against the headline total, never a share of a subset nobody printed.
    # Here the top fixes resolve every open critical finding, so it says "all".
    assert "all {{connectsecure.vulnerabilities.critical_open}} open critical" in top.detail
    assert "connectsecure.remediations.top_critical_resolved" in top.fact_ids
    assert "top_critical_percent" not in top.detail
    assert "connectsecure.remediations.top_critical_percent" not in top.fact_ids
    assert top.recommendation_type == "patch_remediation"
    critical = found["security.vulnerabilities.critical"]
    assert critical.severity == "medium"
    assert "no update queued in NinjaOne" in critical.detail
    waiting = found["security.vulnerabilities.fix_awaiting_install"]
    assert waiting.severity == "low" and "work under way, not a new gap" in waiting.detail
    # The match rate is internal; the customer-facing sentence must not cite it.
    assert "awaiting_install_percent" not in waiting.detail
    remove = found["security.vulnerabilities.software_to_remove"]
    assert remove.kind == "measurement" and "removing" in remove.detail


def test_scan_coverage_is_a_gap_corroborated_by_both_tools() -> None:
    found = findings_for(vulnerability_facts())
    finding = found["security.vulnerability_scan_coverage"]
    assert finding.kind == "gap" and finding.corroborated and finding.assertable
    assert {"ninjaone", "connectsecure"} <= set(finding.sources)
    assert finding.service_key == "vulnerability_management"


def test_without_connectsecure_no_vulnerability_finding_is_written() -> None:
    found = findings_for(make_facts())
    assert not [fid for fid in found if fid.startswith("security.vulnerabilit")]


def test_nothing_open_writes_no_critical_findings() -> None:
    facts = vulnerability_facts()
    for fact_id in (
        "connectsecure.vulnerabilities.critical_open",
        "connectsecure.vulnerabilities.known_exploited",
        "coverage.scan.uncovered",
        "connectsecure.remediations.awaiting_install",
        "connectsecure.remediations.uninstall",
    ):
        facts.facts[fact_id] = facts.facts[fact_id].model_copy(update={"value": 0})
    facts.groups[GROUP_CS_KNOWN_EXPLOITED] = []
    found = findings_for(facts)
    assert not [fid for fid in found if fid.startswith("security.vulnerabilit")]


# --- the scorecard -------------------------------------------------------------------------------


def test_security_scorecard_gains_the_vulnerability_component() -> None:
    rows = {row.key: row for row in evaluate(vulnerability_facts())}
    security = rows["security"]
    labels = {m.fact_id: m.status for m in security.metrics}
    assert labels["connectsecure.vulnerabilities.known_exploited"] == "action"
    assert labels["connectsecure.vulnerabilities.critical_assets"] == "action"  # 2 of 5 = 40%
    assert labels["coverage.scan.uncovered"] == "attention"  # 3 of 48 managed devices = 6.3%
    assert labels["connectsecure.company.risk_score"] == "action"
    assert security.status == "action"


# --- populations: two sets of records never share a sentence ---------------------------------


def test_a_sentence_citing_confirmed_and_unconfirmed_facts_is_rejected() -> None:
    """The live failure of 2026-09-22: "240 critical findings across 4 devices".

    The 240 came from the findings view, which returns what the scanner
    confirmed *and* what it suspects; the 4 came from the per-asset counters,
    which are confirmed only. Both were true of their own population and the
    sentence was false.
    """
    facts = vulnerability_facts()
    facts.add(
        fact(
            "connectsecure.vulnerabilities.suspected_critical", 240, source="connectsecure"
        ).model_copy(update={"population": "unconfirmed"})
    )
    assert facts.facts["connectsecure.vulnerabilities.critical_assets"].population == "confirmed"
    allowed = {
        "connectsecure.vulnerabilities.suspected_critical",
        "connectsecure.vulnerabilities.critical_assets",
    }
    mixed = problems_with(
        "{{connectsecure.vulnerabilities.suspected_critical}} critical findings are open on "
        "{{connectsecure.vulnerabilities.critical_assets}} devices.",
        sorted(allowed),
        allowed=allowed,
        facts=facts,
    )
    assert any("different populations" in problem for problem in mixed), mixed
    assert any("confirmed" in problem and "unconfirmed" in problem for problem in mixed)

    # Either population on its own is fine.
    for fact_id in sorted(allowed):
        single = problems_with(
            f"{{{{{fact_id}}}}} findings were counted.", [fact_id], allowed=allowed, facts=facts
        )
        assert single == [], (fact_id, single)


def test_an_internal_only_fact_may_not_be_cited_in_a_sentence() -> None:
    facts = vulnerability_facts()
    problems = problems_with(
        "{{connectsecure.remediations.awaiting_install_percent}} of fixes are queued.",
        ["connectsecure.remediations.awaiting_install_percent"],
        allowed={"connectsecure.remediations.awaiting_install_percent"},
        facts=facts,
    )
    assert any("internal-only" in problem for problem in problems), problems


def test_every_connectsecure_finding_fact_declares_one_population() -> None:
    """A rule's sentence may only be built from facts that agree on their scope."""
    facts = vulnerability_facts()
    found = derive_findings(
        facts, prior=None, prior_label=None, config=make_config(), period=PERIOD
    )
    for finding in found:
        cited = [facts.facts[i] for i in finding.fact_ids if i in facts.facts]
        denominators = {f.denominator for f in cited if f.denominator}
        claims = [f for f in cited if f.id not in denominators]
        populations = {f.population for f in claims if f.population != "universal"}
        assert len(populations) <= 1, (finding.id, populations)
        severities = {f.severity for f in claims if f.severity is not None}
        assert len(severities) <= 1, (finding.id, severities)
        internal = [f.id for f in cited if f.internal_only]
        assert not internal, (finding.id, internal)


# --- a percentage names the total it is a share of -------------------------------------------


def test_a_percentage_of_a_filtered_subset_is_refused_in_client_mode() -> None:
    """The live slide 14 sentence of 2026-09-22:

    "3 updates would resolve 100% of the critical findings that ConnectSecure's
    recommended fixes address, across 4 devices."

    The 100% was a share of the critical findings the recommended updates
    happen to cover, which is not a number anywhere on the deck. Beside a
    headline of 118 open critical findings it reads as all of them.
    """
    facts = vulnerability_facts()
    share = facts.facts["connectsecure.remediations.top_critical_percent"]
    assert share.is_share and share.denominator is None
    allowed = {
        "connectsecure.remediations.top_count",
        "connectsecure.remediations.top_critical_percent",
        "connectsecure.remediations.top_critical_resolved",
        "connectsecure.vulnerabilities.critical_open",
        "connectsecure.vulnerabilities.critical_assets",
    }
    old = problems_with(
        "{{connectsecure.remediations.top_count}} updates resolve "
        "{{connectsecure.remediations.top_critical_percent}} of the critical findings "
        "ConnectSecure's recommended fixes address, across "
        "{{connectsecure.vulnerabilities.critical_assets}} devices.",
        [
            "connectsecure.remediations.top_count",
            "connectsecure.remediations.top_critical_percent",
            "connectsecure.vulnerabilities.critical_assets",
        ],
        allowed=allowed,
        facts=facts,
    )
    assert any("not a fact on this report" in problem for problem in old), old
    assert any("internal-only" in problem for problem in old), old

    # What it says instead: counts, against the total printed beside them.
    new = problems_with(
        "{{connectsecure.remediations.top_count}} updates resolve "
        "{{connectsecure.remediations.top_critical_resolved}} of the "
        "{{connectsecure.vulnerabilities.critical_open}} open critical findings, across "
        "{{connectsecure.vulnerabilities.critical_assets}} devices.",
        [
            "connectsecure.remediations.top_count",
            "connectsecure.remediations.top_critical_resolved",
            "connectsecure.vulnerabilities.critical_open",
            "connectsecure.vulnerabilities.critical_assets",
        ],
        allowed=allowed,
        facts=facts,
    )
    assert new == [], new


def test_a_percentage_without_its_denominator_in_the_sentence_is_rejected() -> None:
    """A share may be printed, but never on its own: the reader guesses otherwise."""
    facts = vulnerability_facts()
    facts.add(
        facts.facts["connectsecure.vulnerabilities.critical"].model_copy(
            update={
                "id": "connectsecure.vulnerabilities.critical_share",
                "display": "31%",
                "denominator": "connectsecure.vulnerabilities.total",
            }
        )
    )
    allowed = {
        "connectsecure.vulnerabilities.critical_share",
        "connectsecure.vulnerabilities.total",
    }
    alone = problems_with(
        "{{connectsecure.vulnerabilities.critical_share}} of findings are critical.",
        ["connectsecure.vulnerabilities.critical_share"],
        allowed=allowed,
        facts=facts,
    )
    assert any("the total it is a share of" in problem for problem in alone), alone

    together = problems_with(
        "{{connectsecure.vulnerabilities.critical_share}} of the "
        "{{connectsecure.vulnerabilities.total}} confirmed findings are critical.",
        sorted(allowed),
        allowed=allowed,
        facts=facts,
    )
    assert together == [], together


# --- a caveat counts what the headline above it counts ---------------------------------------


def test_the_suspected_caveat_counts_the_severity_of_the_headline_it_qualifies() -> None:
    """The live slide 14 footer of 2026-09-22:

    "A further 4,409 findings are suspected but not yet verified by a scan, and
    are not counted above."

    It sat under "118 critical findings are open on 4 devices". The 4,409 was
    every severity, mostly medium and high, and read as thousands more
    criticals. The caveat now counts the criticals among the suspected.
    """
    facts = vulnerability_facts()
    every_severity = facts.facts["connectsecure.vulnerabilities.unconfirmed"]
    criticals_only = facts.facts["connectsecure.vulnerabilities.unconfirmed_critical"]
    assert every_severity.severity == "all"
    assert criticals_only.severity == "critical"
    assert criticals_only.population == "unconfirmed"

    allowed = {
        "connectsecure.vulnerabilities.unconfirmed",
        "connectsecure.vulnerabilities.unconfirmed_critical",
        "connectsecure.vulnerabilities.critical_open",
    }
    mixed = problems_with(
        "{{connectsecure.vulnerabilities.critical_open}} critical findings are open, and a "
        "further {{connectsecure.vulnerabilities.unconfirmed}} are suspected.",
        [
            "connectsecure.vulnerabilities.critical_open",
            "connectsecure.vulnerabilities.unconfirmed",
        ],
        allowed=allowed,
        facts=facts,
    )
    assert any("different severities" in problem for problem in mixed), mixed
    assert any("different populations" in problem for problem in mixed), mixed

    # The caveat stands alone as its own sentence, and counts criticals.
    caveat = problems_with(
        "A further {{connectsecure.vulnerabilities.unconfirmed_critical}} critical findings "
        "are suspected but not yet verified by a scan, and are not counted above.",
        ["connectsecure.vulnerabilities.unconfirmed_critical"],
        allowed=allowed,
        facts=facts,
    )
    assert caveat == [], caveat


async def test_slide_14_writes_the_caveat_from_the_critical_count() -> None:
    """The rendered footer cites the critical fact, never the all-severity one."""
    document = await make_document(facts=vulnerability_facts())
    section = next(s for s in document.sections if s.key == "cybersecurity")
    caveats = [item for item in section.items if "suspected" in item.text]
    assert len(caveats) == 1, [item.text for item in section.items]
    assert caveats[0].author == "code"
    assert caveats[0].cites == ["connectsecure.vulnerabilities.unconfirmed_critical"]
    assert "critical findings" in caveats[0].text

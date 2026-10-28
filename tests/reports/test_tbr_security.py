"""Slide 14 with Bitdefender: the collectors, the cross-tool join, and the rules.

Every rule here needs two tools' evidence. The tests set up the device-name
groups the collectors record and check what is asserted, what is held, and
what is never a finding.
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
from app.reports.tbr.model import FactSet, FactTable, ReviewPeriod
from app.reports.tbr.scorecard import evaluate
from app.reports.tbr.security import (
    GROUP_BD_CORE_OFF,
    GROUP_BD_ENDPOINTS,
    GROUP_BD_MODULE_OFF,
    GROUP_BD_MODULE_ON,
    GROUP_BD_MODULE_UNLICENSED,
    GROUP_BD_NO_AGENT,
    GROUP_BITLOCKER_ENCRYPTED,
    GROUP_BITLOCKER_UNENCRYPTED,
    GROUP_COVERAGE_MATCHED,
    GROUP_COVERAGE_NINJA_ONLY,
    GROUP_NINJA_COMPUTERS,
    GROUP_NINJA_PATCHING,
    collect_bitdefender_endpoints,
    collect_bitdefender_incidents,
    collect_bitlocker,
    collect_patch_coverage,
    derive_coverage,
)
from tests.connectors.conftest import (
    NINJA_BASE_URL,
    load_fixture,
    make_bitdefender_client,
    make_ninja_client,
    single_page,
)
from tests.connectors.test_bitdefender import (
    COMPANIES,
    INCIDENTS,
    NETWORK,
    NETWORK_V1,
    answer_network,
    mock_licence,
)
from tests.reports.conftest import NOW, PERIOD, fact, make_config, make_facts

AUGUST = ReviewPeriod.ending(2026, 8)


class TwoConnectorFactory(ConnectorFactory):
    def __init__(self, ninja: BaseConnector, bitdefender: BaseConnector) -> None:
        self.by_name = {ConnectorName.NINJAONE: ninja, ConnectorName.BITDEFENDER: bitdefender}

    @asynccontextmanager
    async def open(self, client_id: Any, connector_name: ConnectorName, site_id: Any = None) -> Any:
        if connector_name not in self.by_name:
            raise ConnectorNotConfigured(
                f"no {connector_name.value} credentials in this test",
                connector=connector_name.value,
            )
        async with self.by_name[connector_name] as connector:
            yield connector


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


@pytest.fixture
def two_connectors() -> Any:
    ninja = make_ninja_client(limits=LIMITS)
    bitdefender = make_bitdefender_client(limits=LIMITS)
    previous = set_factory(TwoConnectorFactory(ninja, bitdefender))
    yield ninja, bitdefender
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


def mock_ninja() -> respx.Route:
    respx.post(f"{NINJA_BASE_URL}/ws/oauth/token").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )
    respx.get(f"{NINJA_BASE_URL}/v2/devices").mock(
        side_effect=single_page(load_fixture("ninjaone", "devices.json"))
    )
    for report in ("computer-systems", "processors", "operating-systems", "network-interfaces"):
        respx.get(f"{NINJA_BASE_URL}/v2/queries/{report}").mock(
            return_value=httpx.Response(200, json=load_fixture("ninjaone", "query_empty.json"))
        )
    return respx.get(f"{NINJA_BASE_URL}/v2/queries/volumes").mock(
        return_value=httpx.Response(
            200, json=load_fixture("ninjaone", "query_volumes_bitlocker.json")
        )
    )


def mock_bitdefender() -> None:
    respx.post(NETWORK).mock(side_effect=answer_network)
    # The per-endpoint detail read (lastSeen) goes to the v1.0 network service.
    respx.post(NETWORK_V1).mock(side_effect=answer_network)
    respx.post(COMPANIES).mock(
        return_value=httpx.Response(
            200, json=load_fixture("bitdefender", "company_details_partner.json")
        )
    )
    respx.post(INCIDENTS).mock(
        return_value=httpx.Response(200, json=load_fixture("bitdefender", "incidents_list.json"))
    )


# --- collectors -----------------------------------------------------------------------------------


@respx.mock
async def test_bitlocker_is_judged_per_device_over_local_disks_only(two_connectors: Any) -> None:
    route = mock_ninja()
    run = collector()

    await collect_bitlocker(run)

    assert route.calls[0].request.url.params["include"] == "bl"
    assert run.facts.facts["ninjaone.bitlocker.devices_reporting"].value == 2
    assert run.facts.facts["ninjaone.bitlocker.devices_encrypted"].value == 1
    assert run.facts.facts["ninjaone.bitlocker.devices_unencrypted"].value == 1
    assert run.facts.groups[GROUP_BITLOCKER_ENCRYPTED] == ["TL-AUS-WS14"]
    assert run.facts.groups[GROUP_BITLOCKER_UNENCRYPTED] == ["TL-AUS-DC01"]  # one disk decrypted
    # A device whose rows carry no status at all is not "unencrypted": it is not reporting.
    assert "TL-AUS-WS22" not in run.facts.groups[GROUP_BITLOCKER_UNENCRYPTED]
    assert "include=bl" in run.facts.facts["ninjaone.bitlocker.devices_reporting"].definition


@respx.mock
async def test_patch_coverage_counts_ninety_days_of_installs_and_the_waiting_list(
    two_connectors: Any,
) -> None:
    """A device NinjaOne patches need not have installed anything in the review month."""
    mock_ninja()
    installs = respx.get(f"{NINJA_BASE_URL}/v2/queries/os-patch-installs").mock(
        return_value=httpx.Response(
            200, json=load_fixture("ninjaone", "query_os_patch_installs_mixed.json")
        )
    )
    waiting = load_fixture("ninjaone", "query_os_patches.json")

    def by_status(request: httpx.Request) -> httpx.Response:
        status = request.url.params["status"]
        rows = [row for row in waiting["results"] if row["status"] == status]
        return httpx.Response(200, json={**waiting, "results": rows})

    pending = respx.get(f"{NINJA_BASE_URL}/v2/queries/os-patches").mock(side_effect=by_status)
    run = collector()
    run.facts.groups[GROUP_NINJA_PATCHING] = ["TL-AUS-DC01"]  # what August alone showed

    await collect_patch_coverage(run)

    assert installs.calls[0].request.url.params["installedAfter"] == "2026-06-24"  # 90 days
    assert sorted(c.request.url.params["status"] for c in pending.calls) == [
        "APPROVED",
        "MANUAL",
        "REJECTED",
    ]
    # DC01 from August; WS14, WS22 and WS07 from the wider history and the waiting list.
    assert run.facts.groups[GROUP_NINJA_PATCHING] == [
        "TL-AUS-DC01",
        "TL-AUS-WS07",
        "TL-AUS-WS14",
        "TL-AUS-WS22",
    ]
    assert run.facts.facts["ninjaone.patching.devices_managed"].value == 4
    assert "90 days" in run.facts.facts["ninjaone.patching.devices_managed"].definition
    assert not run.facts.facts["ninjaone.patching.devices_managed"].period_bound


@respx.mock
async def test_bitdefender_endpoints_become_facts_groups_and_the_risk_score(
    two_connectors: Any,
) -> None:
    mock_bitdefender()
    mock_licence()  # the pilot's licence: no EDR, encryption or patch management
    run = collector()

    await collect_bitdefender_endpoints(run)

    facts = run.facts
    assert facts.facts["bitdefender.endpoints.total"].value == 6
    assert facts.facts["bitdefender.endpoints.with_agent"].value == 5
    assert facts.facts["bitdefender.endpoints.no_agent"].value == 1
    assert facts.facts["bitdefender.endpoints.core_off"].value == 1
    assert facts.facts["bitdefender.endpoints.agent_outdated"].value == 1
    assert facts.facts["bitdefender.endpoints.protected_percent"].display == "50%"
    # The licence decides the state, whatever a row says: encryption is not licensed on
    # every endpoint (DC01's row says on, EXCH's says off, the rest say off).
    assert facts.facts["bitdefender.licence.encryption"].value == "no"
    assert facts.facts["bitdefender.licence.edr"].value == "no"
    assert facts.facts["bitdefender.modules.encryption.enabled"].value == 0
    assert facts.facts["bitdefender.modules.encryption.disabled"].value == 0
    assert facts.facts["bitdefender.modules.encryption.not_licensed"].value == 5
    assert "not by licence" in facts.facts["bitdefender.modules.encryption.disabled"].definition
    assert "Distinct from disabled" in (
        facts.facts["bitdefender.modules.encryption.not_licensed"].definition
    )
    # A licence-gated module no row reports is still counted, as not licensed; a
    # documented module the tenant neither reports nor licenses does not appear.
    assert facts.facts["bitdefender.modules.edrSensor.not_licensed"].value == 5
    assert "bitdefender.modules.relay.enabled" not in facts.facts
    # Modules the documentation does not name are read from the rows.
    assert facts.facts["bitdefender.modules.applicationControl.disabled"].value == 5
    assert facts.groups[GROUP_BD_MODULE_UNLICENSED.format(module="encryption")] == [
        "TL-AUS-DC01", "TL-AUS-EXCH", "TL-AUS-MAC03", "TL-AUS-WS14", "TL-AUS-WS22"
    ]  # fmt: skip
    assert facts.groups[GROUP_BD_MODULE_OFF.format(module="encryption")] == []
    assert facts.facts["bitdefender.company.risk_score"].display.endswith("%")
    assert facts.facts["bitdefender.company.risk_impact"].value in ("Low", "Medium", "High")
    assert facts.groups[GROUP_BD_NO_AGENT] == ["TL-AUS-WS07"]
    assert facts.groups[GROUP_BD_CORE_OFF] == ["TL-AUS-MAC03"]
    assert facts.tables["bitdefender.endpoints.unprotected.devices"].rows == [
        ["TL-AUS-WS22", "agent outdated"],
        ["TL-AUS-MAC03", "antimalware disabled"],
        ["TL-AUS-WS07", "no security agent"],
    ]
    assert facts.names["bitdefender_endpoints"][0] == "TL-AUS-DC01"  # a demo render replaces these
    kinds = [row[2].get("tool_name") for row in run.audit.rows]  # type: ignore[attr-defined]
    assert kinds == [
        "tbr.bitdefender.endpoints",
        "tbr.bitdefender.company",
        "tbr.bitdefender.licence",
    ]


@respx.mock
async def test_with_encryption_licensed_a_row_saying_off_is_disabled(two_connectors: Any) -> None:
    mock_bitdefender()
    mock_licence("license_info_edr.json")  # encryption, EDR and patch management licensed
    run = collector()

    await collect_bitdefender_endpoints(run)

    facts = run.facts
    assert facts.facts["bitdefender.modules.encryption.enabled"].value == 2
    assert facts.facts["bitdefender.modules.encryption.disabled"].value == 3
    assert "bitdefender.modules.encryption.not_licensed" not in facts.facts
    assert facts.groups[GROUP_BD_MODULE_ON.format(module="encryption")] == [
        "TL-AUS-DC01",
        "TL-AUS-EXCH",
    ]
    assert facts.groups[GROUP_BD_MODULE_OFF.format(module="encryption")] == [
        "TL-AUS-MAC03", "TL-AUS-WS14", "TL-AUS-WS22"
    ]  # fmt: skip
    # edrSensor is licensed but no row reports it: not reported, not disabled.
    assert "bitdefender.modules.edrSensor.enabled" not in facts.facts
    assert facts.groups.get(GROUP_BD_MODULE_OFF.format(module="edrSensor"), []) == []


@respx.mock
async def test_bitdefender_incidents_are_bounded_to_the_period(two_connectors: Any) -> None:
    mock_bitdefender()
    mock_licence("license_info_edr.json")
    run = collector()
    run.period = ReviewPeriod.ending(2026, 9)  # the fixture's incidents are in September

    await collect_bitdefender_incidents(run)

    assert run.facts.facts["bitdefender.incidents.created"].value == 3
    assert run.facts.facts["bitdefender.incidents.blocked"].value == 1
    assert run.facts.facts["bitdefender.incidents.reported"].value == 2
    assert run.facts.facts["bitdefender.incidents.open"].value == 2
    assert run.facts.facts["bitdefender.incidents.created"].period_bound


@respx.mock
async def test_without_an_edr_licence_detections_are_a_gap_never_zero(two_connectors: Any) -> None:
    mock_bitdefender()
    mock_licence()  # the live licence: manageEventCorrelator false
    incidents = respx.post(INCIDENTS)
    run = collector()

    await collect_bitdefender_incidents(run)

    assert incidents.call_count == 0
    assert "bitdefender.incidents.created" not in run.facts.facts
    assert [g.what for g in run.facts.gaps] == ["Bitdefender detections"]
    assert "EDR is not licensed" in run.facts.gaps[0].why


@respx.mock
async def test_the_join_pairs_by_hostname_and_names_the_ninjaone_only_computers(
    two_connectors: Any,
) -> None:
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    run = collector()
    await run._ninja_inventory()
    await collect_bitdefender_endpoints(run)

    derive_coverage(run)

    assert run.facts.groups[GROUP_NINJA_COMPUTERS] == [
        "TL-AUS-DC01",
        "TL-AUS-MAC03",
        "TL-AUS-WS07",
        "TL-AUS-WS14",
        "TL-AUS-WS22",
    ]
    # TL-AUS-WS07 is a GravityZone row with no agent on it: the same gap as no
    # row, and the coverage board counts it as absent, so the deck does too.
    # TL-AUS-MAC03 has never contacted NinjaOne, but its GravityZone row is a
    # current agent: alive per Bitdefender, NinjaOne not reporting. A finding of
    # its own, never counted as unprotected and never an unmanaged endpoint.
    assert run.facts.groups["ninjaone.rmm_stale"] == ["TL-AUS-MAC03"]
    assert run.facts.facts["coverage.matched"].value == 3
    assert run.facts.facts["coverage.ninjaone_only"].value == 1
    assert run.facts.groups["coverage.ninjaone_only"] == ["TL-AUS-WS07"]
    assert run.facts.facts["coverage.rmm_agent_down"].value == 1
    # TL-AUS-EXCH has no NinjaOne device; MAC03's endpoint is its device's.
    assert run.facts.facts["coverage.bitdefender_only"].value == 1
    assert run.facts.facts["coverage.matched"].source == "derived"


def test_the_join_is_skipped_when_either_tool_is_missing() -> None:
    run = collector()
    run.facts.groups[GROUP_NINJA_COMPUTERS] = ["TL-AUS-WS14"]
    derive_coverage(run)  # no Bitdefender facts: nothing derived, no zero
    assert "coverage.ninjaone_only" not in run.facts.facts


@respx.mock
async def test_a_site_with_no_bitdefender_company_records_a_gap_not_a_zero(
    two_connectors: Any,
) -> None:
    """`collect()` with bitdefender_mapped=False never asks Bitdefender: a gap is recorded."""
    from app.reports.tbr.security import BITDEFENDER_GAP

    mock_ninja()
    for report in (
        "antivirus-status",
        "os-patch-installs",
        "software-patch-installs",
        "logged-on-users",
        "os-patches",
        "activities",
        "software-patches",
    ):
        respx.get(f"{NINJA_BASE_URL}/v2/queries/{report}").mock(
            return_value=httpx.Response(200, json=load_fixture("ninjaone", "query_empty.json"))
        )
    respx.get(f"{NINJA_BASE_URL}/v2/activities").mock(
        return_value=httpx.Response(200, json={"lastActivityId": 1, "activities": []})
    )
    respx.get(url__regex=r".*/v2/organization/\d+/locations").mock(
        return_value=httpx.Response(200, json=[])
    )
    bitdefender_route = respx.post(NETWORK)
    run = collector()
    run.bitdefender_mapped = False

    # Autotask is not stubbed: its collectors fail into gaps, as designed.
    facts = await run.collect()

    assert bitdefender_route.call_count == 0
    assert BITDEFENDER_GAP in [g.what for g in facts.gaps]
    assert "bitdefender.endpoints.total" not in facts.facts
    assert "coverage.ninjaone_only" not in facts.facts  # no join without both tools


# --- rules: cross-tool evidence -------------------------------------------------------------------


def security_facts(**overrides: Any) -> FactSet:
    """NinjaOne facts plus Bitdefender facts and the groups the collectors record."""
    facts = make_facts(**overrides)
    for fact_id, value in (
        ("bitdefender.endpoints.total", 5),
        ("bitdefender.endpoints.with_agent", 4),
        ("bitdefender.endpoints.no_agent", 1),
        ("bitdefender.endpoints.core_off", 0),
        ("bitdefender.endpoints.protected_percent", 92.0),
        ("bitdefender.modules.patchManagement.disabled", 3),
        ("bitdefender.modules.encryption.disabled", 2),
        ("bitdefender.modules.edrSensor.disabled", 0),
        ("bitdefender.modules.exchange.disabled", 4),
        ("bitdefender.company.risk_score", 37.0),
        ("bitdefender.incidents.reported", 0),
        ("ninjaone.bitlocker.devices_reporting", 4),
        ("ninjaone.bitlocker.devices_unencrypted", 2),
        ("ninjaone.patching.devices_managed", 3),
        ("coverage.ninjaone_only", 0),
        ("coverage.matched", 4),
    ):
        if facts.get(fact_id) is not None:
            continue  # an override from make_facts stands
        facts.add(
            fact(
                fact_id,
                value,
                source="bitdefender"
                if fact_id.startswith("bitdefender")
                else "ninjaone"
                if fact_id.startswith("ninjaone")
                else "derived",
            )
        )
    facts.groups = {
        GROUP_NINJA_COMPUTERS: ["ACME-DC01", "ACME-WS01", "ACME-WS02", "ACME-WS03", "ACME-WS04"],
        GROUP_NINJA_PATCHING: ["ACME-DC01", "ACME-WS01", "ACME-WS02"],
        GROUP_BITLOCKER_ENCRYPTED: ["ACME-WS01", "ACME-WS02"],
        GROUP_BITLOCKER_UNENCRYPTED: ["ACME-DC01", "ACME-WS03"],
        GROUP_BD_ENDPOINTS: ["ACME-DC01", "ACME-WS01", "ACME-WS02", "ACME-WS03", "ACME-WS04"],
        GROUP_BD_NO_AGENT: ["ACME-WS04"],
        GROUP_BD_CORE_OFF: [],
        GROUP_BD_MODULE_OFF.format(module="patchManagement"): [
            "ACME-WS01",
            "ACME-WS02",
            "ACME-WS03",
        ],
        GROUP_BD_MODULE_ON.format(module="encryption"): ["ACME-DC01", "ACME-WS04"],
        GROUP_BD_MODULE_OFF.format(module="encryption"): ["ACME-WS01", "ACME-WS03"],
        GROUP_BD_MODULE_OFF.format(module="edrSensor"): [],
        GROUP_BD_MODULE_UNLICENSED.format(module="patchManagement"): [],
        GROUP_BD_MODULE_UNLICENSED.format(module="encryption"): [],
        GROUP_BD_MODULE_UNLICENSED.format(module="edrSensor"): [],
        GROUP_BD_MODULE_OFF.format(module="exchange"): [
            "ACME-WS01",
            "ACME-WS02",
            "ACME-WS03",
            "ACME-WS04",
        ],
        GROUP_COVERAGE_MATCHED: ["ACME-DC01", "ACME-WS01", "ACME-WS02", "ACME-WS03"],
        GROUP_COVERAGE_NINJA_ONLY: [],
    }
    return facts


def findings_for(facts: FactSet) -> dict[str, Any]:
    found = derive_findings(
        facts, prior=None, prior_label=None, config=make_config(), period=PERIOD
    )
    return {finding.id: finding for finding in found}


def test_a_ninjaone_only_computer_is_a_no_agent_gap_asserted_by_both_tools() -> None:
    facts = security_facts(coverage__ninjaone_only=2)
    facts.groups[GROUP_COVERAGE_NINJA_ONLY] = ["ACME-WS05", "ACME-WS06"]
    facts.tables["coverage.ninjaone_only.devices"] = FactTable(
        id="coverage.ninjaone_only.devices",
        label="x",
        definition="x",
        source="derived",
        columns=["Device"],
        rows=[["ACME-WS05"], ["ACME-WS06"]],
        total=2,
        as_of=NOW,
    )

    finding = findings_for(facts)["security.no_protection_agent"]

    assert finding.kind == "gap" and finding.assertable and finding.corroborated
    assert set(finding.sources) >= {"ninjaone", "bitdefender"}
    assert finding.section == "cybersecurity" and finding.severity == "medium"
    assert "security.no_protection_agent" not in findings_for(security_facts())  # zero: nothing


def test_a_disabled_patch_module_is_a_gap_only_where_ninjaone_is_not_patching() -> None:
    found = findings_for(security_facts())

    finding = found["security.unpatched_devices.disabled"]
    assert finding.assertable  # NinjaOne patch data plus Bitdefender module state
    assert finding.section == "cybersecurity" and "disabled" in finding.headline
    assert "not licensed" not in finding.detail and "disabled" in finding.detail
    # WS01 and WS02 have the module off but NinjaOne patched them: only WS03 is unpatched.
    assert found["security.unpatched_devices.disabled"].fact_ids[0] == (
        "security.unpatched_devices.disabled"
    )
    assert "security.unpatched_devices.not_licensed" not in found
    facts = security_facts()
    facts.groups[GROUP_NINJA_PATCHING] = ["ACME-DC01", "ACME-WS01", "ACME-WS02", "ACME-WS03"]
    assert not [f for f in findings_for(facts) if f.startswith("security.unpatched")]


def test_an_unlicensed_patch_module_is_a_services_finding_under_the_same_rule() -> None:
    facts = security_facts()
    facts.groups[GROUP_BD_MODULE_OFF.format(module="patchManagement")] = []
    facts.groups[GROUP_BD_MODULE_UNLICENSED.format(module="patchManagement")] = [
        "ACME-WS01", "ACME-WS03", "ACME-WS04"
    ]  # fmt: skip

    found = findings_for(facts)

    finding = found["security.unpatched_devices.not_licensed"]
    assert finding.section == "services" and finding.service_key == "patch_remediation"
    assert "not licensed" in finding.headline and "disabled" not in finding.detail
    assert "licence" in (finding.recommendation or "")
    assert "security.unpatched_devices.disabled" not in found
    # WS01 is patched by NinjaOne: only WS03 and WS04 are recommended.
    assert facts.tables["security.unpatched_devices.not_licensed.devices"].rows == [
        ["ACME-WS03"], ["ACME-WS04"]
    ]  # fmt: skip


def test_encryption_is_a_gap_only_when_neither_bitlocker_nor_bitdefender_protects() -> None:
    found = findings_for(security_facts())
    finding = found["security.unencrypted_devices.disabled"]

    # DC01: BitLocker off but Bitdefender encryption on -> protected. WS03: neither -> the gap.
    assert finding.assertable and finding.severity == "high"
    assert finding.section == "cybersecurity"
    assert finding.fact_ids[:2] == [
        "security.unencrypted_devices.disabled",
        "security.unencrypted_devices.disabled.devices",
    ]
    facts = security_facts()
    facts.groups[GROUP_BD_MODULE_ON.format(module="encryption")].append("ACME-WS03")
    assert not [f for f in findings_for(facts) if f.startswith("security.unencrypted")]
    # A device only one tool knows is never judged.
    facts = security_facts()
    facts.groups[GROUP_COVERAGE_MATCHED] = ["ACME-WS01"]
    assert not [f for f in findings_for(facts) if f.startswith("security.unencrypted")]


def test_unlicensed_encryption_is_recommended_only_for_devices_bitlocker_leaves_open() -> None:
    facts = security_facts()
    facts.groups[GROUP_BD_MODULE_OFF.format(module="encryption")] = []
    facts.groups[GROUP_BD_MODULE_ON.format(module="encryption")] = []
    facts.groups[GROUP_BD_MODULE_UNLICENSED.format(module="encryption")] = [
        "ACME-DC01", "ACME-WS01", "ACME-WS02", "ACME-WS03"
    ]  # fmt: skip

    found = findings_for(facts)

    finding = found["security.unencrypted_devices.not_licensed"]
    assert finding.section == "services" and finding.service_key == "full_disk_encryption"
    assert "not licensed" in finding.headline and "disabled" not in finding.detail
    # WS01 and WS02 are BitLocker-encrypted: only DC01 and WS03 are recommended.
    assert facts.tables["security.unencrypted_devices.not_licensed.devices"].rows == [
        ["ACME-DC01"], ["ACME-WS03"]
    ]  # fmt: skip
    assert "security.unencrypted_devices.disabled" not in found


def test_a_disabled_edr_sensor_is_held_for_the_engineer_until_blackpoint() -> None:
    facts = security_facts()
    facts.groups[GROUP_BD_MODULE_OFF.format(module="edrSensor")] = ["ACME-WS02"]

    finding = findings_for(facts)["security.edr_sensor_off.disabled"]

    assert finding.needs_confirmation and not finding.assertable
    assert finding.section == "cybersecurity" and "disabled" in finding.headline


def test_unlicensed_edr_is_a_held_services_finding() -> None:
    facts = security_facts()
    facts.groups[GROUP_BD_MODULE_UNLICENSED.format(module="edrSensor")] = ["ACME-WS01", "ACME-WS02"]

    found = findings_for(facts)
    finding = found["security.edr_sensor_off.not_licensed"]

    assert finding.needs_confirmation and not finding.assertable  # held until Blackpoint
    assert finding.section == "services" and finding.service_key == "mdr"
    assert "not licensed" in finding.headline and "Blackpoint" in (finding.recommendation or "")
    assert "security.edr_sensor_off.disabled" not in found


def test_workload_modules_are_never_findings() -> None:
    found = findings_for(
        security_facts()
    )  # exchange off on four endpoints, container off everywhere
    assert not [fid for fid in found if "exchange" in fid or "container" in fid]


def test_the_held_antivirus_finding_is_confirmed_by_bitdefender() -> None:
    facts = security_facts()
    facts.tables["ninjaone.antivirus.unprotected.devices"] = FactTable(
        id="ninjaone.antivirus.unprotected.devices",
        label="x",
        definition="x",
        source="ninjaone",
        columns=["Device"],
        rows=[["ACME-WS04"], ["ACME-WS01"]],
        total=2,
        as_of=NOW,
    )

    finding = findings_for(facts)["antivirus.unprotected"]

    assert finding.assertable and finding.confirmed_by == "bitdefender"  # WS04 has no agent
    assert "bitdefender" in finding.sources and "Bitdefender confirms" in finding.detail


def test_the_held_antivirus_finding_is_dismissed_when_bitdefender_protects_every_device() -> None:
    facts = security_facts()
    facts.tables["ninjaone.antivirus.unprotected.devices"] = FactTable(
        id="ninjaone.antivirus.unprotected.devices",
        label="x",
        definition="x",
        source="ninjaone",
        columns=["Device"],
        rows=[["ACME-WS01"], ["acme-ws02.acme.local"]],
        total=2,
        as_of=NOW,
    )

    found = findings_for(facts)

    assert "antivirus.unprotected" not in found
    assert found["internal.antivirus_dismissed"].section == "internal"  # for the engineer only


def test_the_held_antivirus_finding_stays_held_when_bitdefender_does_not_know_the_device() -> None:
    facts = security_facts()
    facts.tables["ninjaone.antivirus.unprotected.devices"] = FactTable(
        id="ninjaone.antivirus.unprotected.devices",
        label="x",
        definition="x",
        source="ninjaone",
        columns=["Device"],
        rows=[["ACME-WS99"]],
        total=1,
        as_of=NOW,
    )
    finding = findings_for(facts)["antivirus.unprotected"]
    assert finding.needs_confirmation and finding.confirmed_by is None


def test_the_held_antivirus_finding_is_confirmed_when_the_join_finds_no_endpoint() -> None:
    """A doubted computer with no Bitdefender endpoint at all: both tools say no agent."""
    facts = security_facts()
    facts.groups[GROUP_NINJA_COMPUTERS].append("ACME-WS99")
    facts.groups[GROUP_COVERAGE_NINJA_ONLY] = ["ACME-WS99"]
    facts.tables["ninjaone.antivirus.unprotected.devices"] = FactTable(
        id="ninjaone.antivirus.unprotected.devices",
        label="x",
        definition="x",
        source="ninjaone",
        columns=["Device"],
        rows=[["ACME-WS99"]],
        total=1,
        as_of=NOW,
    )

    finding = findings_for(facts)["antivirus.unprotected"]

    assert finding.assertable and finding.confirmed_by == "bitdefender"
    # The count the sentence cites is the doubted devices without an endpoint, not the
    # site-wide NinjaOne-only figure.
    assert "antivirus.unprotected.no_endpoint" in finding.fact_ids
    assert "coverage.ninjaone_only" not in finding.fact_ids
    assert facts.facts["antivirus.unprotected.no_endpoint"].value == 1
    assert facts.tables["antivirus.unprotected.no_endpoint.devices"].rows == [["ACME-WS99"]]
    assert "{{antivirus.unprotected.no_endpoint}} of them have no Bitdefender" in finding.detail


def test_without_bitdefender_the_antivirus_finding_is_held_as_before() -> None:
    finding = findings_for(make_facts())["antivirus.unprotected"]
    assert finding.needs_confirmation and not finding.assertable


# --- the scorecard -------------------------------------------------------------------------------


def test_security_is_scored_from_bitdefender_and_the_join() -> None:
    rows = {row.key: row for row in evaluate(security_facts())}
    security = rows["security"]

    assert security.status == "attention"  # 92% protected: below 97, above 90
    assert [m.fact_id for m in security.metrics] == [
        "bitdefender.endpoints.protected_percent",
        "coverage.ninjaone_only",
        "bitdefender.incidents.reported",
        "bitdefender.company.risk_score",
    ]
    good = evaluate(
        security_facts(
            bitdefender__endpoints__protected_percent=99.0, bitdefender__company__risk_score=20.0
        )
    )
    assert next(r for r in good if r.key == "security").status == "good"
    assert next(r for r in evaluate(make_facts()) if r.key == "security").status == "no_data"

"""Bitdefender GravityZone: the read-only allowlist, scoping, paging, the five tools.

Fixtures are scrubbed live responses from the pilot tenant (2026-09-22): every id,
name, address and MAC replaced, shapes and enums kept. Three rows were edited past
scrubbing to show states the pilot does not (a disabled antimalware module, an
enabled encryption module, an unmanaged endpoint under the site); api-notes names
them. `incidents_list.json` alone is still documentation-shaped: the tenant has no
EDR licence and therefore no incidents to record.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from app.agent.registry import get_tool, registered_tools
from app.connectors.base import ConnectorLimits
from app.connectors.bitdefender.client import (
    READ_ONLY_METHODS,
    BitdefenderClient,
    ReadOnlyViolation,
)
from app.connectors.bitdefender.tools import (
    count_endpoints,
    get_endpoint_protection,
    list_outdated_agents,
    list_recent_detections,
    list_unprotected_endpoints,
)
from app.connectors.errors import (
    ArgumentRejected,
    AuthError,
    NotFound,
    RateLimited,
)
from app.connectors.matching import match_by_hostname, normalise_hostname
from app.connectors.scope import VendorOrganisation
from tests.connectors.conftest import (
    BITDEFENDER_BASE_URL,
    BITDEFENDER_COMPANY_ID,
    BITDEFENDER_INACTIVE_SITE,
    BITDEFENDER_OTHER_SITE,
    BITDEFENDER_SITE,
    RecordingSleep,
    load_fixture,
    make_bitdefender_client,
)

CLIENT_ID = str(uuid.uuid4())
NETWORK = f"{BITDEFENDER_BASE_URL}/v1.1/jsonrpc/network"
NETWORK_V1 = f"{BITDEFENDER_BASE_URL}/v1.0/jsonrpc/network"
COMPANIES = f"{BITDEFENDER_BASE_URL}/v1.0/jsonrpc/companies"
INCIDENTS = f"{BITDEFENDER_BASE_URL}/v1.2/jsonrpc/incidents"
LICENSING = f"{BITDEFENDER_BASE_URL}/v1.0/jsonrpc/licensing"


def rpc_method(request: httpx.Request) -> str:
    return str(json.loads(request.content)["method"])


def rpc_params(request: httpx.Request) -> dict[str, Any]:
    return dict(json.loads(request.content)["params"])


def answer_network(request: httpx.Request) -> httpx.Response:
    """The Network API as the fixtures describe it, keyed by method and parentId."""
    method, params = rpc_method(request), rpc_params(request)
    if method == "getNetworkInventoryItems":
        if params.get("filters", {}).get("type", {}).get("companies"):
            return httpx.Response(
                200, json=load_fixture("bitdefender", "network_inventory_companies.json")
            )
        if params.get("parentId") == BITDEFENDER_COMPANY_ID:
            return httpx.Response(
                200, json=load_fixture("bitdefender", "network_inventory_site.json")
            )
        if params.get("parentId") is None:
            return httpx.Response(
                200, json=load_fixture("bitdefender", "network_inventory_root.json")
            )
        return httpx.Response(
            200,
            json={
                "id": "x",
                "jsonrpc": "2.0",
                "result": {"page": 1, "perPage": 1000, "hasMoreRecords": False, "items": []},
            },
        )
    if method == "getManagedEndpointDetails":
        return httpx.Response(200, json=load_fixture("bitdefender", "endpoint_details.json"))
    if method == "getCompaniesList":
        return httpx.Response(200, json=load_fixture("bitdefender", "companies_list.json"))
    raise AssertionError(f"unexpected method {method}")


def mock_licence(fixture: str = "license_info.json") -> respx.Route:
    return respx.post(LICENSING).mock(
        return_value=httpx.Response(200, json=load_fixture("bitdefender", fixture))
    )


def mock_world() -> tuple[respx.Route, respx.Route, respx.Route]:
    network = respx.post(NETWORK).mock(side_effect=answer_network)
    network_v1 = respx.post(NETWORK_V1).mock(side_effect=answer_network)
    companies = respx.post(COMPANIES).mock(
        return_value=httpx.Response(
            200, json=load_fixture("bitdefender", "company_details_partner.json")
        )
    )
    return network, network_v1, companies


@pytest.fixture
def bitdefender(stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    return stub_factory(make_bitdefender_client(limits=fast_limits, sleep=sleeper))


# --- read-only, enforced in code --------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "method"),
    [
        ("network", "createScanTask"),
        ("network", "moveEndpoints"),
        ("network", "moveEndpointsBetweenCompanies"),
        ("network", "deleteEndpoint"),
        ("network", "assignPolicy"),
        ("network", "createReconfigureClientTask"),
        ("policies", "setPolicy"),
        ("companies", "updateCompanyDetails"),
        ("companies", "deleteCompany"),
        ("incidents", "createIsolateEndpointTask"),
        ("incidents", "addToBlocklist"),
        ("quarantine", "createEmptyQuarantineTask"),
        ("network", "getEndpointsListWithATypo"),
        ("nonsense", "getEndpointsList"),
        # parameter shapes unknown: kept off the allowlist until recorded live
        ("incidents", "getIncident"),
        ("incidents", "getIncidentsByIds"),
        ("network", "getEndpointTags"),
        ("network", "getScanTasksList"),
        ("network", "getIntegrators"),
        ("companies", "getCompanyDetailsByUser"),
    ],
)
async def test_a_write_or_unknown_method_is_refused_before_any_request(
    fast_limits: ConnectorLimits, service: str, method: str
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__startswith=BITDEFENDER_BASE_URL).mock(
            return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": None})
        )
        async with make_bitdefender_client(limits=fast_limits) as client:
            with pytest.raises(ReadOnlyViolation, match="not a read-only method"):
                await client.call(service, method, {"endpointIds": ["x"]})
        assert route.call_count == 0  # no HTTP at all
    assert not BitdefenderClient.is_read_only(service, method)


def test_the_allowlist_holds_reads_only() -> None:
    for service, methods in READ_ONLY_METHODS.items():
        for method in methods:
            assert method.startswith(("get", "find")), (service, method)
            for verb in ("create", "delete", "move", "set", "update", "assign", "add", "remove"):
                assert not method.lower().startswith(verb), (service, method)


@respx.mock
async def test_the_envelope_the_auth_header_and_the_version_path(
    fast_limits: ConnectorLimits,
) -> None:
    route = respx.post(NETWORK).mock(side_effect=answer_network)
    async with make_bitdefender_client(limits=fast_limits) as client:
        await client.call(
            "network", "getNetworkInventoryItems", {"parentId": BITDEFENDER_COMPANY_ID}
        )

    request = route.calls[0].request
    body = json.loads(request.content)
    assert body["jsonrpc"] == "2.0" and body["method"] == "getNetworkInventoryItems"
    assert uuid.UUID(body["id"])  # every call carries its own id
    assert request.headers["Content-Type"] == "application/json"
    expected = base64.b64encode(b"test-api-key:").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"  # key as user, empty password
    assert request.url.path.endswith("/v1.1/jsonrpc/network")  # the method's own version


@pytest.mark.parametrize(
    ("fixture", "error"),
    [
        ("rpc_error_authorization.json", AuthError),
        ("rpc_error_not_found.json", NotFound),
        ("rpc_error_invalid_params.json", ArgumentRejected),
        ("rpc_error_unknown_option.json", ArgumentRejected),
    ],
)
@respx.mock
async def test_a_json_rpc_error_on_http_200_is_mapped(
    fast_limits: ConnectorLimits, fixture: str, error: type[Exception]
) -> None:
    respx.post(NETWORK).mock(
        return_value=httpx.Response(200, json=load_fixture("bitdefender", fixture))
    )
    async with make_bitdefender_client(limits=fast_limits) as client:
        with pytest.raises(error) as raised:
            await client.call("network", "getNetworkInventoryItems", {})
    assert "path" in raised.value.details  # type: ignore[attr-defined]
    if error is ArgumentRejected:
        assert isinstance(raised.value, ValueError)  # the loop hands it back to the model
        assert "correct the value" in str(raised.value)
        if fixture == "rpc_error_invalid_params.json":
            assert "open, closed, in_progress" in str(raised.value)  # the accepted values


@respx.mock
async def test_a_429_honours_retry_after_then_gives_up(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    respx.post(NETWORK).mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json=load_fixture("bitdefender", "rpc_error_rate_limited.json"),
        )
    )
    async with make_bitdefender_client(limits=fast_limits, sleep=sleeper) as client:
        with pytest.raises(RateLimited):
            await client.call("network", "getNetworkInventoryItems", {})
    assert sleeper.delays == [0.05, 0.05]  # Retry-After capped at backoff_max_seconds


# --- sites and scoping --------------------------------------------------------------------------


@respx.mock
async def test_list_organisations_asks_for_customer_companies_only(
    fast_limits: ConnectorLimits,
) -> None:
    route = respx.post(NETWORK_V1).mock(side_effect=answer_network)
    async with make_bitdefender_client(limits=fast_limits) as client:
        organisations = await client.list_organisations()

    assert rpc_params(route.calls[0].request) == {"filters": {"companyType": 1}}
    assert organisations == [
        VendorOrganisation(external_id=BITDEFENDER_COMPANY_ID, name="Acme Dental"),
        VendorOrganisation(external_id="64a0000000000000000000b2", name="Beacon Legal LLP"),
        VendorOrganisation(external_id="64a0000000000000000000b3", name="Former Customer Co"),
    ]


@respx.mock
async def test_a_named_site_is_scoped_by_parent_id_and_checked_again(
    fast_limits: ConnectorLimits,
) -> None:
    network, _, companies = mock_world()
    client = make_bitdefender_client(
        limits=fast_limits,
        sites=[BITDEFENDER_SITE, BITDEFENDER_OTHER_SITE],
        targets=[BITDEFENDER_SITE],
    )
    async with client:
        rows, truncated = await client.list_endpoints()

    assert [rpc_params(c.request).get("parentId") for c in network.calls] == [
        BITDEFENDER_COMPANY_ID
    ]
    assert companies.call_count == 0  # a scoped read never asks who the partner is
    assert [(site.slug, endpoint.name) for site, endpoint in rows] == [
        ("acme-dental", "TL-AUS-WS14"),
        ("acme-dental", "TL-AUS-WS22"),
        ("acme-dental", "TL-AUS-DC01"),
        ("acme-dental", "TL-AUS-MAC03"),
        ("acme-dental", "TL-AUS-WS07"),
        ("acme-dental", "TL-AUS-EXCH"),
    ]
    assert not truncated
    params = rpc_params(network.calls[0].request)
    assert params["filters"]["type"] == {"computers": True, "virtualMachines": True}
    assert params["filters"]["depth"] == {"allItemsRecursively": True}
    assert params["options"]["endpoints"]["returnProductOutdated"] is True


@respx.mock
async def test_the_partner_root_listing_is_classified_row_by_row(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """Sibling kept for the allowlist, inactive dropped quietly, own and foreign dropped."""
    mock_world()
    client = make_bitdefender_client(
        limits=fast_limits,
        sites=[BITDEFENDER_SITE, BITDEFENDER_OTHER_SITE],
        excluded=[BITDEFENDER_INACTIVE_SITE],
    )
    async with client:
        with caplog.at_level("DEBUG"):
            rows, _ = await client.list_endpoints()

    by_site = {}
    for site, endpoint in rows:
        by_site.setdefault(site.slug, []).append(endpoint.name)
    assert by_site == {
        "acme-dental": [
            "TL-AUS-WS14",
            "TL-AUS-WS22",
            "TL-AUS-DC01",
            "TL-AUS-MAC03",
            "TL-AUS-WS07",
            "TL-AUS-EXCH",
        ],
        "beacon-legal": ["OTHER-WS01"],
    }
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "outside the allowlist" in errors[0].getMessage()  # FOREIGN
    assert "1 of the franchisee's own endpoints" in caplog.text  # PARTNER_OWN, quietly
    assert "1 record(s) from inactive sites" in caplog.text
    entry = await client._inventory()
    assert entry.source == "tenant" and len(entry.device_orgs) == 10  # every endpoint, for placing


@respx.mock
async def test_the_inventory_is_cached_and_single_flighted(fast_limits: ConnectorLimits) -> None:
    import asyncio

    network, _, _ = mock_world()
    async with make_bitdefender_client(limits=fast_limits) as client:
        first, second = await asyncio.gather(client.list_endpoints(), client.list_endpoints())
        third = await client.list_endpoints()
    assert network.call_count == 1
    assert len(first[0]) == len(second[0]) == len(third[0]) == 6


@respx.mock
async def test_pages_follow_has_more_records_and_the_cap_is_reported(
    fast_limits: ConnectorLimits,
) -> None:
    def paged(request: httpx.Request) -> httpx.Response:
        page = rpc_params(request)["page"]
        item = {
            "id": f"64e00000000000000000{page:04d}",
            "name": f"WS-{page}",
            "companyId": BITDEFENDER_COMPANY_ID,
            "type": 5,
            "details": {"managedWithBest": True},
        }
        return httpx.Response(
            200,
            json={
                "id": "1",
                "jsonrpc": "2.0",
                "result": {"page": page, "perPage": 1, "hasMoreRecords": True, "items": [item]},
            },
        )

    respx.post(NETWORK).mock(side_effect=paged)
    respx.post(COMPANIES).mock(
        return_value=httpx.Response(
            200, json=load_fixture("bitdefender", "company_details_partner.json")
        )
    )
    async with make_bitdefender_client(limits=fast_limits, page_size=1) as client:
        rows, truncated = await client.list_endpoints()
    assert len(rows) == fast_limits.max_pages and truncated


@respx.mock
async def test_endpoint_details_are_checked_against_the_site_asked_for(
    fast_limits: ConnectorLimits,
) -> None:
    mock_world()
    async with make_bitdefender_client(
        limits=fast_limits, sites=[BITDEFENDER_SITE, BITDEFENDER_OTHER_SITE]
    ) as client:
        detail = await client.get_endpoint_details(BITDEFENDER_SITE, "64e0000000000000000000a1")
        assert detail.agent is not None and detail.agent.product_version == "8.26.9.668"
        with pytest.raises(NotFound):  # the row says Acme; Beacon asked
            await client.get_endpoint_details(BITDEFENDER_OTHER_SITE, "64e0000000000000000000a1")


# --- tools --------------------------------------------------------------------------------------


def test_five_tools_registered_each_saying_what_it_is_not_for() -> None:
    names = {d.name for d in registered_tools()}
    assert {
        "count_endpoints",
        "list_unprotected_endpoints",
        "list_recent_detections",
        "get_endpoint_protection",
        "list_outdated_agents",
    } <= names
    # 18 NinjaOne (2 coverage), 14 Autotask, 5 Bitdefender, 5 ConnectSecure,
    # 5 Microsoft 365, 4 Cove (1 of them the backup coverage gap, which lives
    # with the board's join). The same number is pinned in
    # `test_autotask_reports.py`; both move together or the suite says so.
    assert len(registered_tools()) == 51
    schema = get_tool("count_endpoints").model_facing_schema()
    assert schema["properties"]["group_by"]["enum"] == ["site", "status", "module", "os"]
    assert get_tool("get_endpoint_protection").model_facing_schema()["required"] == ["device_name"]


@respx.mock
async def test_count_endpoints_by_status_and_module(bitdefender: Any) -> None:
    mock_world()
    status = await count_endpoints(CLIENT_ID, group_by="status")
    assert status.total == 6 and status.with_agent == 5
    assert status.counts == {
        "protected": 3,
        "agent outdated": 1,
        "core protection off": 1,
        "no agent": 1,
    }

    mock_licence()  # the pilot's licence: no EDR, encryption or patch management
    modules = await count_endpoints(CLIENT_ID, group_by="module")
    assert modules.counts == {}
    # Only modules the tenant reports, in the documented order then the undocumented ones.
    assert list(modules.module_counts) == [
        "antimalware", "advancedThreatControl", "advancedAntiExploit", "firewall",
        "networkAttackDefense", "contentControl", "deviceControl", "powerUser", "encryption",
        "patchManagement", "applicationControl", "antiTampering", "userControl", "antiphishing",
        "trafficScan", "remoteEnginesScanning", "riskManagement",
    ]  # fmt: skip
    assert "edrSensor" not in modules.module_counts and "exchange" not in modules.module_counts
    assert modules.module_counts["antimalware"].model_dump() == {
        "enabled": 4, "disabled": 1, "not_licensed": 0, "not_reported": 0
    }  # fmt: skip
    # encryption is off on every row, and the licence says why: not licensed, not disabled.
    assert modules.module_counts["encryption"].model_dump() == {
        "enabled": 0, "disabled": 0, "not_licensed": 5, "not_reported": 0
    }  # fmt: skip
    # patchManagement appears on two rows only; the other three did not report it.
    assert modules.module_counts["patchManagement"].model_dump() == {
        "enabled": 0, "disabled": 0, "not_licensed": 5, "not_reported": 0
    }  # fmt: skip
    assert modules.edr_licensed == {"acme-dental": False}

    os = await count_endpoints(CLIENT_ID, group_by="os")
    assert os.counts == {"Windows": 5, "macOS": 1}


@respx.mock
async def test_unprotected_endpoints_worst_first_with_counts_before_the_cap(
    bitdefender: Any,
) -> None:
    mock_world()
    result = await list_unprotected_endpoints(CLIENT_ID)
    assert [(row.name, row.reasons) for row in result.endpoints] == [
        ("TL-AUS-WS07", ["no security agent"]),
        ("TL-AUS-MAC03", ["antimalware disabled"]),
        ("TL-AUS-WS22", ["agent outdated"]),
    ]
    assert result.endpoints_checked == 6 and result.total == 3
    assert result.counts_by_reason == {
        "no security agent": 1,
        "antimalware disabled": 1,
        "agent outdated": 1,
    }
    assert result.counts_by_site == {"Acme Dental": 3}


@respx.mock
async def test_outdated_agents(bitdefender: Any) -> None:
    mock_world()
    result = await list_outdated_agents(CLIENT_ID)
    assert [row.name for row in result.endpoints] == ["TL-AUS-WS22"]
    assert result.endpoints_with_agent == 5 and result.counts_by_site == {"Acme Dental": 1}


@respx.mock
async def test_recent_detections_say_when_edr_is_not_licensed(bitdefender: Any) -> None:
    mock_world()
    mock_licence()  # the live licence: manageEventCorrelator false
    incidents = respx.post(INCIDENTS).mock(
        return_value=httpx.Response(
            200, json=load_fixture("bitdefender", "incidents_list_empty.json")
        )
    )
    result = await list_recent_detections(CLIENT_ID, days=7)
    assert incidents.call_count == 0  # an unlicensed company is not even asked
    assert result.detections == [] and result.total == 0
    assert result.edr_licensed == {"acme-dental": False}
    assert result.sites_without_edr == ["acme-dental"]
    assert result.note and "EDR is not licensed for this site" in result.note
    assert "not a quiet week" in result.note


@respx.mock
async def test_recent_detections_are_windowed_per_company_and_placed(bitdefender: Any) -> None:
    mock_world()
    mock_licence("license_info_edr.json")
    incidents = respx.post(INCIDENTS).mock(
        return_value=httpx.Response(200, json=load_fixture("bitdefender", "incidents_list.json"))
    )
    result = await list_recent_detections(CLIENT_ID, days=7)

    params = rpc_params(incidents.calls[0].request)
    assert params["filters"]["companyId"] == BITDEFENDER_COMPANY_ID
    assert set(params["filters"]) == {"companyId", "startDate", "endDate"}
    assert params["options"] == {"includeChildCompanies": False}
    assert [(d.incident_number, d.endpoint, d.action) for d in result.detections] == [
        (101, "TL-AUS-WS14", "blocked"),
        (103, "TL-AUS-WS22", "partially_blocked"),  # no company on the row: placed by endpoint
        (102, "TL-AUS-MAC03", "reported"),
    ]
    assert result.counts_by_status == {"open": 2, "closed": 1}
    assert result.counts_by_action == {"blocked": 1, "partially_blocked": 1, "reported": 1}
    assert result.detections[0].link and result.detections[0].link.startswith("https://")
    assert result.edr_licensed == {"acme-dental": True} and result.note is None
    with pytest.raises(ValueError, match="between 1 and 90"):
        await list_recent_detections(CLIENT_ID, days=0)


@respx.mock
async def test_get_endpoint_protection_matches_the_hostname_loosely(bitdefender: Any) -> None:
    mock_world()
    mock_licence()
    result = await get_endpoint_protection(CLIENT_ID, "tl-aus-ws14.ACME.local")
    assert result.name == "TL-AUS-WS14" and result.agent_installed
    assert result.agent_version == "8.26.9.668" and result.agent_type == "BEST"
    assert result.state == "online" and result.licence == "active"
    assert (
        "antimalware" in result.modules_enabled and "applicationControl" in result.modules_disabled
    )
    # The three states are told apart: encryption is not licensed, not disabled; so is EDR,
    # which the row never mentions.
    assert (
        "encryption" in result.modules_not_licensed and "edrSensor" in result.modules_not_licensed
    )
    assert "encryption" not in result.modules_disabled and result.edr_licensed is False
    assert result.risk_score == 13.0 and result.risk_impact == "Low"
    assert result.malware_detected_last_24h is False and result.candidates == []
    with pytest.raises(ValueError, match="no GravityZone endpoint named"):
        await get_endpoint_protection(CLIENT_ID, "NO-SUCH-BOX")


@respx.mock
async def test_a_401_is_an_auth_error_and_an_empty_company_is_an_answer(bitdefender: Any) -> None:
    respx.post(url__startswith=BITDEFENDER_BASE_URL).mock(
        return_value=httpx.Response(401, json={"error": "unauthorised"})
    )
    with pytest.raises(AuthError):
        await count_endpoints(CLIENT_ID)


@respx.mock
async def test_an_empty_company_counts_to_zero(bitdefender: Any) -> None:
    respx.post(COMPANIES).mock(
        return_value=httpx.Response(
            200, json=load_fixture("bitdefender", "company_details_partner.json")
        )
    )
    respx.post(NETWORK).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "1",
                "jsonrpc": "2.0",
                "result": {
                    "page": 1,
                    "perPage": 1000,
                    "hasMoreRecords": False,
                    "items": [],
                    "total": 0,
                    "pagesCount": 0,
                },
            },
        )
    )
    result = await count_endpoints(CLIENT_ID, group_by="status")
    assert result.total == 0 and result.counts == {}


# --- the matcher ---------------------------------------------------------------------------------


def test_hostnames_are_matched_within_a_site_case_and_domain_insensitively() -> None:
    assert normalise_hostname(" TL-AUS-WS14.acme.local ") == "tl-aus-ws14"
    assert normalise_hostname(None) == ""
    ninja = [
        ("acme", "TL-AUS-WS14"),
        ("acme", "TL-AUS-DC01.acme.local"),
        ("acme", "RECEPTION"),
        ("beacon", "RECEPTION"),
        ("acme", None),
    ]
    bitdefender = [
        ("acme", "tl-aus-ws14.acme.local"),
        ("acme", "TL-AUS-EXCH"),
        ("beacon", "reception"),
        ("beacon", "BEACON-WS02"),
    ]

    report = match_by_hostname(ninja, bitdefender)

    assert report["acme"].matched == ["tl-aus-ws14"]
    assert report["acme"].left_only == ["reception", "tl-aus-dc01", "(unnamed)"]
    assert report["acme"].right_only == ["tl-aus-exch"]
    assert report["beacon"].matched == ["reception"]  # never paired with Acme's RECEPTION
    assert report["beacon"].counts == {"matched": 1, "left_only": 0, "right_only": 1}


@respx.mock
async def test_the_company_listing_is_judged_against_every_site_not_the_one_targeted(
    caplog: pytest.LogCaptureFixture, fast_limits: ConnectorLimits
) -> None:
    """`getNetworkInventoryItems` for companies takes no `parentId`: the whole tenant
    comes back by construction. Judging it against one targeted site logged a warning
    on every review run saying the response "was not narrowed" to a site nobody had
    asked the vendor to narrow to (2026-09-29)."""
    respx.post(NETWORK).mock(
        return_value=httpx.Response(
            200, json=load_fixture("bitdefender", "network_inventory_companies.json")
        )
    )
    client = make_bitdefender_client(limits=fast_limits)
    client.scope = replace(client.scope, targets=client.scope.allowed[:1])

    with caplog.at_level(logging.WARNING, logger="app.connectors.bitdefender.client"):
        async with client:
            companies = await client.list_companies()

    assert [site.slug for site, _ in companies] == [ref.slug for ref in client.scope.allowed]
    assert "was not narrowed" not in caplog.text

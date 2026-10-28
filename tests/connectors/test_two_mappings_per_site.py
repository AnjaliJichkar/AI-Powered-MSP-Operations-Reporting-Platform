"""A site with two vendor organisations on one connector: fan-out and the guard.

Live on the pilot, customers had a hand-made GravityZone company and an
integration-created one, both holding endpoints. Both are the customer's, so
a site-scoped read covers their union: the connector asks the vendor once per
organisation, tags every row with the one site, and the guard accepts a row
from either company as that site's while still dropping a third company's.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.connectors.base import ConnectorLimits
from app.connectors.bitdefender.models import BdLicense
from app.connectors.bitdefender.tools import list_recent_detections
from app.connectors.errors import ArgumentRejected
from app.connectors.scope import ClientScope, SiteRef
from app.models.enums import ConnectorName
from tests.connectors.conftest import (
    BITDEFENDER_COMPANY_ID,
    BITDEFENDER_OTHER_SITE,
    BITDEFENDER_SITE,
    TEST_CLIENT_ID,
    TEST_CLIENT_SLUG,
    make_bitdefender_client,
)
from tests.connectors.test_bitdefender import (
    INCIDENTS,
    LICENSING,
    NETWORK,
    NETWORK_V1,
    rpc_method,
    rpc_params,
)

CLIENT_ID = str(TEST_CLIENT_ID)

#: The customer's second GravityZone company: the integration-created one.
SECOND_COMPANY_ID = "64a0000000000000000000c9"
SECOND_REF = SiteRef(
    site_id=BITDEFENDER_SITE.site_id,
    slug=BITDEFENDER_SITE.slug,
    name=BITDEFENDER_SITE.name,
    external_id=SECOND_COMPANY_ID,
)
STRANGER_COMPANY_ID = "64a0000000000000000000d1"


def endpoint(item_id: str, name: str, company: str, *, agent: bool = True) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": 5,
        "name": name,
        "companyId": company,
        "details": {"managedWithBest": agent, "isManaged": True},
    }


def inventory(items: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "x",
            "jsonrpc": "2.0",
            "result": {"page": 1, "perPage": 1000, "hasMoreRecords": False, "items": items},
        },
    )


def answer(request: httpx.Request) -> httpx.Response:
    method, params = rpc_method(request), rpc_params(request)
    if method == "getNetworkInventoryItems":
        parent = params.get("parentId")
        if parent == BITDEFENDER_COMPANY_ID:
            return inventory(
                [
                    endpoint("e1", "HAND-WS1", BITDEFENDER_COMPANY_ID),
                    endpoint("e2", "HAND-WS2", BITDEFENDER_COMPANY_ID, agent=False),
                    # The vendor leaking a stranger's row under this parent: dropped.
                    endpoint("e9", "STRANGER", STRANGER_COMPANY_ID),
                ]
            )
        if parent == SECOND_COMPANY_ID:
            return inventory(
                [
                    endpoint("e3", "INTEG-WS1", SECOND_COMPANY_ID),
                    endpoint("e4", "INTEG-WS2", SECOND_COMPANY_ID),
                    endpoint("e5", "INTEG-WS3", SECOND_COMPANY_ID),
                ]
            )
        return inventory([])
    if method == "getLicenseInfo":
        company = params.get("companyId")
        return httpx.Response(
            200,
            json={
                "id": "x",
                "jsonrpc": "2.0",
                "result": {
                    "manageEncryption": company == SECOND_COMPANY_ID,
                    "manageEventCorrelator": False,
                    "managePatchManagement": company == BITDEFENDER_COMPANY_ID,
                    "usedSlots": 2 if company == BITDEFENDER_COMPANY_ID else 3,
                },
            },
        )
    raise AssertionError(f"unexpected method {method}")


def two_company_client(limits: ConnectorLimits) -> Any:
    """Narrowed to the one site: the allowlist has another, so this is a site read."""
    return make_bitdefender_client(
        limits=limits,
        sites=[BITDEFENDER_SITE, SECOND_REF, BITDEFENDER_OTHER_SITE],
        targets=[BITDEFENDER_SITE, SECOND_REF],
    )


@respx.mock
async def test_a_site_scoped_read_fans_out_over_both_companies_and_merges(
    fast_limits: ConnectorLimits,
) -> None:
    network = respx.post(NETWORK).mock(side_effect=answer)
    respx.post(NETWORK_V1).mock(side_effect=answer)
    client = two_company_client(fast_limits)
    assert client.scope.is_single_site  # two refs, one site
    assert client.scope.describe_targets() == "acme-dental"
    async with client:
        rows, truncated = await client.list_endpoints()

    asked = sorted(rpc_params(c.request).get("parentId") for c in network.calls)
    assert asked == sorted([BITDEFENDER_COMPANY_ID, SECOND_COMPANY_ID])
    assert not truncated
    # Five endpoints from two companies, all tagged as the one site; the
    # stranger's row under the first parent is dropped by the guard.
    assert sorted(endpoint.name for _, endpoint in rows) == [
        "HAND-WS1",
        "HAND-WS2",
        "INTEG-WS1",
        "INTEG-WS2",
        "INTEG-WS3",
    ]
    assert {site.site_id for site, _ in rows} == {BITDEFENDER_SITE.site_id}
    assert {site.external_id for site, _ in rows} == {BITDEFENDER_COMPANY_ID, SECOND_COMPANY_ID}


@respx.mock
async def test_the_guard_accepts_either_company_and_refuses_a_third(
    fast_limits: ConnectorLimits,
) -> None:
    respx.post(NETWORK).mock(side_effect=answer)
    respx.post(NETWORK_V1).mock(side_effect=answer)
    client = two_company_client(fast_limits)
    rows = client._scoped(
        [
            endpoint("a", "A", BITDEFENDER_COMPANY_ID),
            endpoint("b", "B", SECOND_COMPANY_ID),
            endpoint("c", "C", STRANGER_COMPANY_ID),
        ],
        [BITDEFENDER_COMPANY_ID, SECOND_COMPANY_ID, STRANGER_COMPANY_ID],
        method="test",
    )
    assert [(site.slug, row["name"]) for site, row in rows] == [
        ("acme-dental", "A"),
        ("acme-dental", "B"),
    ]


@respx.mock
async def test_licences_of_two_companies_are_the_union_for_the_site(
    fast_limits: ConnectorLimits,
) -> None:
    respx.post(NETWORK).mock(side_effect=answer)
    respx.post(NETWORK_V1).mock(side_effect=answer)
    respx.post(LICENSING).mock(side_effect=answer)
    client = two_company_client(fast_limits)
    async with client:
        licences = await client.licences()
    assert list(licences) == ["acme-dental"]
    licence = licences["acme-dental"]
    assert licence.manage_encryption is True  # the second company has it
    assert licence.manage_patch_management is True  # the first has it
    assert licence.edr_licensed is False  # neither has it
    assert licence.used_slots == 5


def test_union_of_licences_keeps_unknown_as_unknown() -> None:
    a = BdLicense.model_validate({"manageEncryption": None, "manageExchange": False})
    b = BdLicense.model_validate({"manageEncryption": None, "manageExchange": None})
    merged = a.union(b)
    assert merged.manage_encryption is None
    assert merged.manage_exchange is False


def test_narrowing_to_a_site_keeps_every_ref_of_that_site() -> None:
    other = SiteRef(site_id=uuid.uuid4(), slug="other", name="Other", external_id="z")
    scope = ClientScope(
        client_id=TEST_CLIENT_ID,
        client_slug=TEST_CLIENT_SLUG,
        connector=ConnectorName.BITDEFENDER,
        allowed=(BITDEFENDER_SITE, SECOND_REF, other),
        targets=(BITDEFENDER_SITE, SECOND_REF, other),
    )
    assert not scope.is_single_site
    assert scope.describe_targets() == "2 sites"
    narrowed = scope.narrowed_to(BITDEFENDER_SITE)
    assert narrowed.targets == (BITDEFENDER_SITE, SECOND_REF)
    assert narrowed.is_single_site
    assert json.dumps(sorted(narrowed.target_external_ids)) == json.dumps(
        sorted([BITDEFENDER_COMPANY_ID, SECOND_COMPANY_ID])
    )


# --- one of the two companies gone at the vendor ------------------------------------------
#
# Live, 2026-09-25: a merged site's hand-made company had been removed at
# GravityZone after the merge; `parentId` was refused for it and the whole
# site-scoped read failed, though the other company answered. A company the
# vendor no longer knows is spared when the site's other company answers,
# and raised as before when nothing else does.


def rpc_error(details: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "x",
            "jsonrpc": "2.0",
            "error": {"code": -32602, "message": "Invalid params", "data": {"details": details}},
        },
    )


def answer_first_company_gone(request: httpx.Request) -> httpx.Response:
    method, params = rpc_method(request), rpc_params(request)
    if method == "getNetworkInventoryItems" and params.get("parentId") == BITDEFENDER_COMPANY_ID:
        return rpc_error("Invalid value for 'parentId' parameter.")
    if method == "getLicenseInfo" and params.get("companyId") == BITDEFENDER_COMPANY_ID:
        return rpc_error("The company does not exist.")
    if method == "getIncidentsList":
        company = params["filters"]["companyId"]
        if company == BITDEFENDER_COMPANY_ID:
            return rpc_error("The company does not exist.")
        return httpx.Response(
            200,
            json={
                "id": "x",
                "jsonrpc": "2.0",
                "result": {
                    "page": 1,
                    "perPage": 100,
                    "hasMoreRecords": False,
                    "items": [
                        {
                            "incidentId": "inc-1",
                            "incidentNumber": 7,
                            "incidentType": "incident",
                            "company": {"id": SECOND_COMPANY_ID, "name": "Acme Dental"},
                            "status": "open",
                            "mainAction": "blocked",
                            "created": "2026-09-24T10:00:00+00:00",
                            "lastUpdated": "2026-09-24T10:00:00+00:00",
                            "endpoint": {"id": "e3", "name": "INTEG-WS1"},
                        }
                    ],
                },
            },
        )
    if method == "getLicenseInfo":
        return httpx.Response(
            200,
            json={
                "id": "x",
                "jsonrpc": "2.0",
                "result": {"manageEventCorrelator": True, "manageEncryption": True, "usedSlots": 3},
            },
        )
    return answer(request)


@respx.mock
async def test_an_endpoint_read_spares_a_company_the_vendor_no_longer_knows(
    fast_limits: ConnectorLimits, caplog: Any
) -> None:
    network = respx.post(NETWORK).mock(side_effect=answer_first_company_gone)
    respx.post(NETWORK_V1).mock(side_effect=answer_first_company_gone)
    client = two_company_client(fast_limits)
    async with client:
        rows, truncated = await client.list_endpoints()
    asked = sorted(rpc_params(c.request).get("parentId") for c in network.calls)
    assert asked == sorted([BITDEFENDER_COMPANY_ID, SECOND_COMPANY_ID])  # both were asked
    assert sorted(endpoint.name for _, endpoint in rows) == ["INTEG-WS1", "INTEG-WS2", "INTEG-WS3"]
    assert not truncated
    spared = [r for r in caplog.records if "no longer answers" in r.getMessage()]
    assert len(spared) == 1 and BITDEFENDER_COMPANY_ID in spared[0].getMessage()


@respx.mock
async def test_a_site_whose_only_company_is_gone_still_fails(fast_limits: ConnectorLimits) -> None:
    respx.post(NETWORK).mock(side_effect=answer_first_company_gone)
    respx.post(NETWORK_V1).mock(side_effect=answer_first_company_gone)
    client = make_bitdefender_client(
        limits=fast_limits,
        sites=[BITDEFENDER_SITE, BITDEFENDER_OTHER_SITE],
        targets=[BITDEFENDER_SITE],  # one company, and it is the gone one
    )
    async with client:
        with pytest.raises(ArgumentRejected, match="parentId"):
            await client.list_endpoints()


@respx.mock
async def test_licences_and_detections_come_from_the_company_that_still_answers(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    respx.post(NETWORK).mock(side_effect=answer_first_company_gone)
    respx.post(NETWORK_V1).mock(side_effect=answer_first_company_gone)
    respx.post(LICENSING).mock(side_effect=answer_first_company_gone)
    incidents = respx.post(INCIDENTS).mock(side_effect=answer_first_company_gone)
    client = two_company_client(fast_limits)
    stub_factory(client)
    async with client:
        licences = await client.licences()
        assert list(licences) == ["acme-dental"]
        assert licences["acme-dental"].edr_licensed is True
        # Only the company that answered, and holds the licence, is asked for incidents.
        assert [s.external_id for s in await client.companies_with_edr()] == [SECOND_COMPANY_ID]
    result = await list_recent_detections(CLIENT_ID, days=7, site_id=str(BITDEFENDER_SITE.site_id))
    assert [rpc_params(c.request)["filters"]["companyId"] for c in incidents.calls] == [
        SECOND_COMPANY_ID
    ]
    assert [d.incident_number for d in result.detections] == [7]
    assert result.sites_covered == ["acme-dental"]  # one site, however many companies
    assert result.edr_licensed == {"acme-dental": True}

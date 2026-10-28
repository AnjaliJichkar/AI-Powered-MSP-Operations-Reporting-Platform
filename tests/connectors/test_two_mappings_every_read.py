"""Every site-scoped read against a site with two vendor organisations.

`tests/connectors/test_two_mappings_per_site.py` proved the Bitdefender
fan-out; these are the other three connectors, each read that narrows to a
site: it asks for both organisations, merges under the one site, reports the
site once in `sites_covered`, and — when the vendor no longer knows one of the
two — reads on from the other rather than failing the site. A site whose only
organisation is gone still fails, as it always did.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.connectors.autotask.tools import get_company
from app.connectors.base import ConnectorLimits
from app.connectors.connectsecure.tools import count_vulnerabilities
from app.connectors.errors import NotFound
from app.connectors.ninjaone.tools import list_devices
from app.connectors.scope import SiteRef
from app.reports.tbr.audit import NullAudit
from app.reports.tbr.collect import Collector
from app.reports.tbr.config import FranchiseeConfig
from app.reports.tbr.model import ReviewPeriod
from tests.connectors.conftest import (
    AUTOTASK_COMPANY_ID,
    AUTOTASK_SITE,
    AUTOTASK_ZONE_URL,
    CONNECTSECURE_BASE_URL,
    CONNECTSECURE_COMPANY_ID,
    CONNECTSECURE_OTHER_SITE,
    CONNECTSECURE_SITE,
    NINJA_BASE_URL,
    RecordingSleep,
    load_fixture,
    make_autotask_client,
    make_connectsecure_client,
    make_ninja_client,
    site_ref,
)
from tests.connectors.test_connectsecure import answer as connectsecure_answer
from tests.connectors.test_connectsecure import mock_world as connectsecure_world
from tests.connectors.test_tenancy import DENTAL, LEGAL, devices_for, devices_url, mock_token

CLIENT_ID = str(uuid.uuid4())


def second_ref(site: SiteRef, external_id: str | int) -> SiteRef:
    """The same site's second organisation on the connector."""
    return SiteRef(
        site_id=site.site_id, slug=site.slug, name=site.name, external_id=str(external_id)
    )


# --- NinjaOne -------------------------------------------------------------------------------

SECOND_ORG = 43
DENTAL_2 = second_ref(DENTAL, SECOND_ORG)
ALERTS_URL = f"{NINJA_BASE_URL}/v2/alerts"
QUERY_URL = f"{NINJA_BASE_URL}/v2/queries/volumes"
ACTIVITIES_URL = f"{NINJA_BASE_URL}/v2/activities"


def locations_url(organization_id: int) -> str:
    return f"{NINJA_BASE_URL}/v2/organization/{organization_id}/locations"


def gone() -> httpx.Response:
    return httpx.Response(404, json={"resultCode": "NOT_FOUND", "errorMessage": "no such org"})


def ninja_two_org_client(fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    return make_ninja_client(
        limits=fast_limits,
        sleep=sleeper,
        sites=[DENTAL, DENTAL_2, LEGAL],
        targets=[DENTAL, DENTAL_2],
    )


@respx.mock
async def test_ninjaone_devices_alerts_queries_locations_and_activities_read_both_organisations(
    stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    mock_token()
    respx.get(devices_url(42)).mock(return_value=httpx.Response(200, json=devices_for(42, [1, 2])))
    respx.get(devices_url(SECOND_ORG)).mock(
        return_value=httpx.Response(200, json=devices_for(SECOND_ORG, [3]))
    )

    def alerts(request: httpx.Request) -> httpx.Response:
        organisation = int(request.url.params["df"].removeprefix("org="))
        template = load_fixture("ninjaone", "alerts.json")[0]
        device_id = 1 if organisation == 42 else 3
        return httpx.Response(
            200,
            json=[
                {
                    **template,
                    "deviceId": device_id,
                    "device": {
                        **template["device"],
                        "id": device_id,
                        "organizationId": organisation,
                    },
                }
            ],
        )

    alert_route = respx.get(ALERTS_URL).mock(side_effect=alerts)

    def volumes(request: httpx.Request) -> httpx.Response:
        organisation = int(request.url.params["df"].removeprefix("org="))
        row = load_fixture("ninjaone", "query_volumes.json")["results"][0]
        device_id = 1 if organisation == 42 else 3
        return httpx.Response(
            200,
            json={
                "cursor": {"name": "c", "offset": 0, "count": 1, "expires": 0},
                "results": [{**row, "deviceId": device_id}],
            },
        )

    query_route = respx.get(QUERY_URL).mock(side_effect=volumes)
    respx.get(locations_url(42)).mock(
        return_value=httpx.Response(200, json=[{"id": 7, "name": "Head Office"}])
    )
    respx.get(locations_url(SECOND_ORG)).mock(
        return_value=httpx.Response(200, json=[{"id": 8, "name": "Warehouse"}])
    )
    activity_route = respx.get(ACTIVITIES_URL).mock(
        return_value=httpx.Response(200, json={"activities": [{"id": 1, "activityTime": 1.0}]})
    )

    client = ninja_two_org_client(fast_limits, sleeper)
    stub_factory(client)
    async with client:
        rows, _ = await client.list_devices()
        assert [(site.external_id, device.id) for site, device in rows] == [
            ("42", 1),
            ("42", 2),
            (str(SECOND_ORG), 3),
        ]
        assert {site.site_id for site, _ in rows} == {DENTAL.site_id}
        placed = await client.list_alerts()
        assert sorted(a.device_id for _, a in placed) == [1, 3]
        assert alert_route.call_count == 2
        volumes_rows, _, _ = await client.query_scoped("volumes")
        assert sorted(r["deviceId"] for r in volumes_rows) == [1, 3]
        assert sorted(
            request.url.params["df"] for request in (c.request for c in query_route.calls)
        ) == ["org=42", f"org={SECOND_ORG}"]
        locations = await client.list_locations()
        assert [(site.external_id, loc.name) for site, loc in locations] == [
            ("42", "Head Office"),
            (str(SECOND_ORG), "Warehouse"),
        ]
        assert await client.activity_log_reaches(dt.datetime(2026, 9, 1, tzinfo=dt.UTC)) is True
        assert activity_route.call_count == 2
    # The tool names the site once, not once per organisation.
    listed = await list_devices(CLIENT_ID, site_id=str(DENTAL.site_id))
    assert listed.sites_covered == ["acme-dental"]
    assert listed.total == 3


@respx.mock
async def test_ninjaone_spares_an_organisation_it_no_longer_knows_when_the_other_answers(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    mock_token()
    respx.get(devices_url(42)).mock(return_value=httpx.Response(200, json=devices_for(42, [1, 2])))
    respx.get(devices_url(SECOND_ORG)).mock(return_value=gone())
    respx.get(ALERTS_URL).mock(
        side_effect=lambda request: (
            gone()
            if request.url.params["df"] == f"org={SECOND_ORG}"
            else httpx.Response(200, json=[])
        )
    )
    respx.get(QUERY_URL).mock(
        side_effect=lambda request: (
            gone()
            if request.url.params["df"] == f"org={SECOND_ORG}"
            else httpx.Response(
                200,
                json={
                    "cursor": {"name": "c", "offset": 0, "count": 0, "expires": 0},
                    "results": [],
                },
            )
        )
    )
    respx.get(locations_url(42)).mock(return_value=httpx.Response(200, json=[]))
    respx.get(locations_url(SECOND_ORG)).mock(return_value=gone())
    async with ninja_two_org_client(fast_limits, sleeper) as client:
        rows, _ = await client.list_devices()
        assert [device.id for _, device in rows] == [1, 2]
        assert await client.list_alerts() == []
        assert (await client.query_scoped("volumes"))[0] == []
        assert await client.list_locations() == []

    # The same site with only the gone organisation: still a failure.
    async with make_ninja_client(
        limits=fast_limits, sleep=sleeper, sites=[DENTAL_2, LEGAL], targets=[DENTAL_2]
    ) as alone:
        with pytest.raises(NotFound):
            await alone.list_devices()


# --- ConnectSecure ------------------------------------------------------------------------

SECOND_COMPANY = "509"
CS_DENTAL_2 = second_ref(CONNECTSECURE_SITE, SECOND_COMPANY)


def connectsecure_two_company_client(fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    return make_connectsecure_client(
        limits=fast_limits,
        sleep=sleeper,
        sites=[CONNECTSECURE_SITE, CS_DENTAL_2, CONNECTSECURE_OTHER_SITE],
        targets=[CONNECTSECURE_SITE, CS_DENTAL_2],
    )


@respx.mock
async def test_connectsecure_asks_both_companies_and_counts_one_site_as_one_site(
    stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    route = connectsecure_world()
    client = connectsecure_two_company_client(fast_limits, sleeper)
    stub_factory(client)
    async with client:
        assets, _ = await client.list_assets()
    conditions = sorted(
        c.request.url.params["condition"]
        for c in route.calls
        if c.request.url.path == "/r/asset/asset_view"
    )
    assert conditions == [f"company_id={CONNECTSECURE_COMPANY_ID}", f"company_id={SECOND_COMPANY}"]
    assert {site.site_id for site, _ in assets} == {CONNECTSECURE_SITE.site_id}

    stats_calls_before = sum(
        1 for c in route.calls if c.request.url.path == "/r/company/company_stats"
    )
    result = await count_vulnerabilities(
        CLIENT_ID, group_by="severity", site_id=str(CONNECTSECURE_SITE.site_id)
    )
    stats_calls_after = sum(
        1 for c in route.calls if c.request.url.path == "/r/company/company_stats"
    )
    # One site with two companies is a one-site question: the findings are
    # walked, not the per-company totals summed as for many sites.
    assert stats_calls_after == stats_calls_before
    assert result.sites_covered == ["acme-dental"]


@respx.mock
async def test_connectsecure_spares_a_company_it_no_longer_knows_when_the_other_answers(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if f"company_id={SECOND_COMPANY}" in (request.url.params.get("condition") or ""):
            return httpx.Response(404, json={"status": False, "message": "not found"})
        return connectsecure_answer(request)

    respx.post(f"{CONNECTSECURE_BASE_URL}/w/authorize").mock(
        return_value=httpx.Response(200, json=load_fixture("connectsecure", "authorize.json"))
    )
    respx.get(url__startswith=CONNECTSECURE_BASE_URL).mock(side_effect=answer)
    async with connectsecure_two_company_client(fast_limits, sleeper) as client:
        assets, _ = await client.list_assets()
    assert len(assets) == 5 and {site.external_id for site, _ in assets} == {
        CONNECTSECURE_COMPANY_ID
    }
    async with make_connectsecure_client(
        limits=fast_limits,
        sleep=sleeper,
        sites=[CS_DENTAL_2, CONNECTSECURE_OTHER_SITE],
        targets=[CS_DENTAL_2],
    ) as alone:
        with pytest.raises(NotFound):
            await alone.list_assets()


# --- Autotask -----------------------------------------------------------------------------

SECOND_AUTOTASK_COMPANY = 78
AT_DENTAL_2 = second_ref(AUTOTASK_SITE, SECOND_AUTOTASK_COMPANY)


def autotask_url(entity: str) -> str:
    return f"{AUTOTASK_ZONE_URL}/V1.0/{entity}/query"


def fields_url(entity: str) -> str:
    return f"{AUTOTASK_ZONE_URL}/V1.0/{entity}/entityInformation/fields"


def company_answer(request: httpx.Request) -> httpx.Response:
    """The Companies query: company 77 as recorded, 78 as the integration-made twin."""
    payload = load_fixture("autotask", "company_detail.json")
    filters = json.loads(request.content.decode())["filter"]
    wanted = next((f["value"] for f in filters if f.get("field") == "id"), None)
    if wanted == SECOND_AUTOTASK_COMPANY:
        twin = {
            **payload["items"][0],
            "id": SECOND_AUTOTASK_COMPANY,
            "companyName": "Acme Dental (integration)",
            "marketSegmentID": 12,  # Healthcare, where 77 is Legal Services
        }
        return httpx.Response(200, json={**payload, "items": [twin]})
    return httpx.Response(200, json=payload)


def mock_autotask_world() -> respx.Route:
    respx.post(autotask_url("Companies")).mock(side_effect=company_answer)
    respx.get(fields_url("Companies")).mock(
        return_value=httpx.Response(
            200, json=load_fixture("autotask", "fields_companies_full.json")
        )
    )
    respx.get(fields_url("Contracts")).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_contracts.json"))
    )
    respx.post(autotask_url("Resources")).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "resources.json"))
    )
    respx.post(autotask_url("CompanyLocations")).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "company_locations.json"))
    )
    return respx.post(autotask_url("Contracts")).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "contracts.json"))
    )


def autotask_two_company_client(fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    legal = site_ref(90, slug="bright-legal", name="Bright Legal")
    return make_autotask_client(
        limits=fast_limits,
        sleep=sleeper,
        config={"base_url": AUTOTASK_ZONE_URL},
        sites=[AUTOTASK_SITE, AT_DENTAL_2, legal],
        targets=[AUTOTASK_SITE, AT_DENTAL_2],
    )


@respx.mock
async def test_the_company_card_reads_both_companies_and_counts_contracts_across_them(
    stub_factory: Any,
    fast_limits: ConnectorLimits,
    sleeper: RecordingSleep,
    autotask_clock: Any,
) -> None:
    contracts = mock_autotask_world()
    stub_factory(autotask_two_company_client(fast_limits, sleeper))

    result = await get_company(CLIENT_ID, site_id=str(AUTOTASK_SITE.site_id))

    assert result.found is True and result.sites_covered == ["acme-dental"]
    assert result.name == "Acme Dental"
    assert result.other_companies == ["Acme Dental (integration)"]
    # Contracts are counted across both companies in one query.
    sent = json.loads(contracts.calls[0].request.content.decode())["filter"]
    assert {
        "op": "in",
        "field": "companyID",
        "value": [AUTOTASK_COMPANY_ID, SECOND_AUTOTASK_COMPANY],
    } in sent
    assert result.active_contracts == 2


@respx.mock
async def test_the_review_names_every_companys_industry(
    stub_factory: Any,
    fast_limits: ConnectorLimits,
    sleeper: RecordingSleep,
    autotask_clock: Any,
) -> None:
    mock_autotask_world()
    stub_factory(autotask_two_company_client(fast_limits, sleeper))
    run = Collector(
        client_id=CLIENT_ID,
        site_id=str(AUTOTASK_SITE.site_id),
        period=ReviewPeriod.ending(2026, 8),
        config=FranchiseeConfig(display_name="Example IT Partners"),
        audit=NullAudit(),
    )
    await run._autotask_company()
    assert run.facts.facts["autotask.company.industry"].value == "Legal Services / Healthcare"

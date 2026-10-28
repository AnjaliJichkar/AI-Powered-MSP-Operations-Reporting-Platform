"""Two-level tenancy: merging across a client's sites, and refusing everything else.

No database and no network. `respx` answers the vendor, and the scope is built
directly, so these test the connector and loop behaviour rather than the
resolution chain — `test_factory_postgres.py` covers that against real rows.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import respx

from app.agent.loop import _run_one_tool
from app.agent.registry import get_tool
from app.connectors.base import ConnectorLimits
from tests.connectors.conftest import (
    NINJA_BASE_URL,
    REQUESTS_PER_LISTING,
    RecordingSleep,
    load_fixture,
    make_ninja_client,
    single_page,
    site_ref,
)

TOKEN_URL = f"{NINJA_BASE_URL}/ws/oauth/token"
ALERTS_URL = f"{NINJA_BASE_URL}/v2/alerts"

#: Two end customers of the same franchisee.
DENTAL = site_ref(42, slug="acme-dental", name="Acme Dental")
LEGAL = site_ref(77, slug="bright-legal", name="Bright Legal")

#: A third organisation that is not this franchisee's at all.
FOREIGN_ORG_ID = 999


TENANT_DEVICES_URL = f"{NINJA_BASE_URL}/v2/devices"


def devices_url(organization_id: int) -> str:
    return f"{NINJA_BASE_URL}/v2/organization/{organization_id}/devices"


def mock_token() -> respx.Route:
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )


def devices_for(organization_id: int, ids: list[int]) -> list[dict[str, Any]]:
    """A device payload for one organisation, built from the recorded shape."""
    template = load_fixture("ninjaone", "devices.json")[0]
    return [
        {
            **template,
            "id": device_id,
            "organizationId": organization_id,
            "systemName": f"PC-{device_id}",
            "displayName": f"PC-{device_id}",
        }
        for device_id in ids
    ]


# --- merging across a client's sites --------------------------------------


@respx.mock
async def test_devices_merge_across_every_site_and_carry_their_site(
    fast_limits: ConnectorLimits,
) -> None:
    """Without a site, a read covers them all and every row says where it came from."""
    mock_token()
    tenant = respx.get(TENANT_DEVICES_URL).mock(
        side_effect=single_page(devices_for(42, [1, 2]) + devices_for(77, [3]))
    )
    dental = respx.get(devices_url(42)).mock(return_value=httpx.Response(200, json=[]))
    legal = respx.get(devices_url(77)).mock(return_value=httpx.Response(200, json=[]))

    async with make_ninja_client(limits=fast_limits, sites=[DENTAL, LEGAL]) as client:
        rows, truncated = await client.list_devices()

    # One tenant-wide request per page (plus the empty page that ends the
    # listing), not one per organisation.
    assert tenant.call_count == REQUESTS_PER_LISTING
    assert dental.call_count == 0
    assert legal.call_count == 0
    assert truncated is False

    assert [(site.slug, device.id) for site, device in rows] == [
        ("acme-dental", 1),
        ("acme-dental", 2),
        ("bright-legal", 3),
    ]


@respx.mock
async def test_naming_a_site_queries_only_that_one(fast_limits: ConnectorLimits) -> None:
    mock_token()
    dental = respx.get(devices_url(42)).mock(
        return_value=httpx.Response(200, json=devices_for(42, [1, 2]))
    )
    legal = respx.get(devices_url(77)).mock(
        return_value=httpx.Response(200, json=devices_for(77, [3]))
    )

    async with make_ninja_client(
        limits=fast_limits, sites=[DENTAL, LEGAL], targets=[LEGAL]
    ) as client:
        rows, _ = await client.list_devices()

    assert dental.call_count == 0
    assert legal.call_count == 1
    assert [(site.slug, device.id) for site, device in rows] == [("bright-legal", 3)]


@respx.mock
async def test_alerts_merge_across_sites_one_call_each(fast_limits: ConnectorLimits) -> None:
    mock_token()

    def responder(request: httpx.Request) -> httpx.Response:
        organisation = request.url.params["df"].removeprefix("org=")
        template = load_fixture("ninjaone", "alerts.json")[0]
        return httpx.Response(
            200,
            json=[
                {
                    **template,
                    "deviceId": int(organisation),
                    "device": {**template["device"], "organizationId": int(organisation)},
                }
            ],
        )

    route = respx.get(ALERTS_URL).mock(side_effect=responder)

    async with make_ninja_client(limits=fast_limits, sites=[DENTAL, LEGAL]) as client:
        rows = await client.list_alerts()

    assert route.call_count == 2
    assert sorted(site.slug for site, _ in rows) == ["acme-dental", "bright-legal"]


# --- what the guard drops -------------------------------------------------


@respx.mock
async def test_another_franchisees_organisation_is_never_merged_in(
    fast_limits: ConnectorLimits,
) -> None:
    """Outside the allowlist entirely: a containment breach, dropped."""
    mock_token()
    respx.get(devices_url(42)).mock(
        return_value=httpx.Response(
            200,
            json=devices_for(42, [1]) + devices_for(FOREIGN_ORG_ID, [500]),
        )
    )

    async with make_ninja_client(
        limits=fast_limits, sites=[DENTAL, LEGAL], targets=[DENTAL]
    ) as client:
        rows, _ = await client.list_devices()

    assert [device.id for _, device in rows] == [1]
    assert all(site.slug in {"acme-dental", "bright-legal"} for site, _ in rows)


@respx.mock
async def test_a_sibling_site_in_a_single_site_read_is_dropped(
    fast_limits: ConnectorLimits,
) -> None:
    """Inside the allowlist but not what we asked for: a filter that did not bite.

    It is still the same franchisee's data, so this is a bug rather than a
    breach — but the answer must reflect the site that was asked about.
    """
    mock_token()
    respx.get(devices_url(77)).mock(
        return_value=httpx.Response(200, json=devices_for(77, [3]) + devices_for(42, [1]))
    )

    async with make_ninja_client(
        limits=fast_limits, sites=[DENTAL, LEGAL], targets=[LEGAL]
    ) as client:
        rows, _ = await client.list_devices()

    assert [(site.slug, device.id) for site, device in rows] == [("bright-legal", 3)]


@respx.mock
async def test_a_record_with_no_organisation_is_dropped(fast_limits: ConnectorLimits) -> None:
    mock_token()
    orphan = devices_for(42, [9])[0] | {"organizationId": None}
    respx.get(TENANT_DEVICES_URL).mock(side_effect=single_page([*devices_for(42, [1]), orphan]))

    async with make_ninja_client(limits=fast_limits, sites=[DENTAL]) as client:
        rows, _ = await client.list_devices()

    assert [device.id for _, device in rows] == [1]


def test_the_allowlist_is_wider_than_the_targets(fast_limits: ConnectorLimits) -> None:
    """The distinction the guard relies on."""
    client = make_ninja_client(limits=fast_limits, sites=[DENTAL, LEGAL], targets=[LEGAL])

    assert client.scope.allowed_external_ids == {"42", "77"}
    assert client.scope.target_external_ids == ["77"]
    assert client.scope.is_single_site is True
    assert client.scope.describe_targets() == "bright-legal"


# --- the loop resolves a slug, and refuses anything outside the client -----


CLIENT_UUID = uuid.uuid4()


async def test_the_loop_resolves_a_site_slug_to_an_id() -> None:
    site_id = uuid.uuid4()
    captured: dict[str, Any] = {}

    async def fake_tool(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"device_count": 0}

    definition = get_tool("list_devices")
    patched = type(definition)(
        name=definition.name,
        description=definition.description,
        connector=definition.connector,
        input_schema=definition.input_schema,
        input_model=definition.input_model,
        func=fake_tool,
    )

    _, is_error, record = await _run_one_tool(
        patched,
        {"site": "acme-dental"},
        CLIENT_UUID,
        timeout_seconds=5.0,
        sites={"acme-dental": site_id},
    )

    assert is_error is False
    assert captured["client_id"] == str(CLIENT_UUID)
    assert captured["site_id"] == str(site_id)
    # The engineer sees the slug, never the id.
    assert record.arguments == {"site": "acme-dental"}


async def test_a_slug_outside_the_client_is_refused_before_any_call() -> None:
    """Hard rule 1: the model cannot reach another franchisee's customer."""
    called = False

    async def fake_tool(**kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    definition = get_tool("list_devices")
    patched = type(definition)(
        name=definition.name,
        description=definition.description,
        connector=definition.connector,
        input_schema=definition.input_schema,
        input_model=definition.input_model,
        func=fake_tool,
    )

    content, is_error, record = await _run_one_tool(
        patched,
        {"site": "dallas-legal"},
        CLIENT_UUID,
        timeout_seconds=5.0,
        sites={"acme-dental": uuid.uuid4()},
    )

    assert called is False
    assert is_error is True
    assert record.outcome == "unknown_site"
    assert "is not a site of this client" in content
    # It says what is available rather than leaving the model to guess.
    assert "acme-dental" in content


@pytest.mark.parametrize(
    "name",
    ["list_devices", "list_devices_not_checked_in", "list_open_alerts"],
)
def test_the_model_sees_a_site_slug_and_never_an_id(name: str) -> None:
    schema = get_tool(name).model_facing_schema()

    assert "site_id" not in schema["properties"]
    assert "client_id" not in schema["properties"]
    assert schema["properties"]["site"]["type"] == "string"
    # Optional: omitting it covers every site.
    assert "site" not in schema.get("required", [])


def test_a_tool_without_a_site_parameter_is_not_given_one() -> None:
    """Only tools that accept `site_id` advertise a `site`."""
    definition = get_tool("list_devices")
    assert definition.accepts_site is True


async def test_sleeper_is_unused_here(sleeper: RecordingSleep) -> None:
    """Guard against these tests silently sleeping on a retry path."""
    assert sleeper.delays == []

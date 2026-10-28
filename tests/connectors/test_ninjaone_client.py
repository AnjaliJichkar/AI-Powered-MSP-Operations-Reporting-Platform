"""NinjaOne client: OAuth, token refresh, scoping and response parsing."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.connectors.base import ConnectorLimits
from app.connectors.errors import AuthError, RateLimited, UpstreamError
from app.connectors.ninjaone.client import REGION_BASE_URLS, NinjaOneClient
from app.connectors.ninjaone.inventory import InventoryCache
from app.connectors.ninjaone.models import NinjaDevice, epoch_to_datetime
from app.connectors.ninjaone.tokens import TOKENS, TokenStore
from app.models.enums import ConnectorName
from tests.connectors.conftest import (
    FROZEN_NOW,
    NINJA_BASE_URL,
    NINJA_ORGANIZATION_ID,
    NINJA_SITE,
    NINJA_TEST_SECRETS,
    REQUESTS_PER_LISTING,
    RecordingSleep,
    load_fixture,
    make_ninja_client,
    make_scope,
    single_page,
    site_ref,
)

TOKEN_URL = f"{NINJA_BASE_URL}/ws/oauth/token"
#: What an unscoped read hits: the tenant-wide listing, classified by org id.
DEVICES_URL = f"{NINJA_BASE_URL}/v2/devices"
#: What a read naming one site hits: the vendor-scoped path.
ORG_DEVICES_URL = f"{NINJA_BASE_URL}/v2/organization/{NINJA_ORGANIZATION_ID}/devices"
ALERTS_URL = f"{NINJA_BASE_URL}/v2/alerts"


def mock_token() -> respx.Route:
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )


# --- authentication -------------------------------------------------------


@respx.mock
async def test_the_token_request_uses_client_credentials(fast_limits: ConnectorLimits) -> None:
    token_route = mock_token()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))

    async with make_ninja_client(limits=fast_limits) as client:
        await client.list_devices()

    body = dict(httpx.QueryParams(token_route.calls[0].request.content.decode()))
    assert body["grant_type"] == "client_credentials"
    assert body["client_id"] == "test-client-id"
    assert body["scope"] == "monitoring"


@respx.mock
async def test_the_token_is_reused_across_calls(fast_limits: ConnectorLimits) -> None:
    token_route = mock_token()
    devices_route = respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))

    async with make_ninja_client(limits=fast_limits, inventory_ttl_seconds=0) as client:
        await client.list_devices()
        await client.list_devices()

    assert token_route.call_count == 1
    assert devices_route.call_count == 2
    assert devices_route.calls[0].request.headers["Authorization"].startswith("Bearer ")


@respx.mock
async def test_an_expired_token_is_refreshed_transparently(
    fast_limits: ConnectorLimits,
) -> None:
    token_route = mock_token()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))

    async with make_ninja_client(limits=fast_limits, inventory_ttl_seconds=0) as client:
        await client.list_devices()
        # Simulate the clock passing the expiry NinjaOne gave us.
        client.tokens.put(
            client._token_key, "expired", dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        )
        await client.list_devices()

    assert token_route.call_count == 2


# --- one token per client, not per connector --------------------------------


@respx.mock
async def test_three_tool_calls_in_one_run_authenticate_once(
    fast_limits: ConnectorLimits,
) -> None:
    """Found live: one question, three connectors, three token requests.

    The factory builds a connector per tool call. They now read one token
    store, keyed by client and credential version, so the run costs one.
    """
    token_route = mock_token()
    devices = respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))
    store = TokenStore()

    for _ in range(3):  # three tool calls, each its own connector, as the loop makes them
        async with make_ninja_client(
            limits=fast_limits,
            token_store=store,
            credential_version="k1:2026-09-18",
            inventory_ttl_seconds=0,
        ) as client:
            await client.list_devices()

    assert token_route.call_count == 1
    sent = {call.request.headers["Authorization"] for call in devices.calls}
    assert len(sent) == 1  # and it is the same token on every call


@respx.mock
async def test_parallel_tool_calls_with_no_token_make_one_token_request(
    fast_limits: ConnectorLimits,
) -> None:
    """Three connectors find the store empty at once; the lock is theirs in common."""
    release = asyncio.Event()
    requests: list[httpx.Request] = []

    async def slow_token(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await release.wait()
        return httpx.Response(200, json=load_fixture("ninjaone", "token.json"))

    respx.post(TOKEN_URL).mock(side_effect=slow_token)
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))
    store = TokenStore()

    async def one_call() -> None:
        async with make_ninja_client(
            limits=fast_limits,
            token_store=store,
            credential_version="k1:2026-09-18",
            inventory_ttl_seconds=0,
        ) as client:
            await client.list_devices()

    tasks = [asyncio.create_task(one_call()) for _ in range(3)]
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*tasks)

    assert len(requests) == 1


@respx.mock
async def test_a_token_is_never_shared_across_clients_or_credential_versions(
    fast_limits: ConnectorLimits,
) -> None:
    token_route = mock_token()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))
    store = TokenStore()

    async def call(**options: Any) -> None:
        async with make_ninja_client(
            limits=fast_limits, token_store=store, inventory_ttl_seconds=0, **options
        ) as client:
            await client.list_devices()

    await call(credential_version="k1:monday")
    await call(credential_version="k1:monday")
    assert token_route.call_count == 1

    await call(credential_version="k1:tuesday")  # the credential was replaced
    assert token_route.call_count == 2
    assert len(store) == 1  # and the old version's token was retired, not kept beside it

    other = dataclasses.replace(
        make_scope(ConnectorName.NINJAONE, [NINJA_SITE]), client_id=uuid.uuid4()
    )
    async with NinjaOneClient(
        scope=other,
        secrets=dict(NINJA_TEST_SECRETS),
        limits=fast_limits,
        token_store=store,
        credential_version="k1:tuesday",
        inventory=InventoryCache(),
        inventory_ttl_seconds=0,
    ) as stranger:
        await stranger.list_devices()
    assert token_route.call_count == 3  # another client, same version string: its own token


@respx.mock
async def test_a_connector_without_a_credential_version_keeps_its_token_to_itself(
    fast_limits: ConnectorLimits,
) -> None:
    """No version means no way to know a stored token is still good: no sharing."""
    token_route = mock_token()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))

    for _ in range(2):
        client = NinjaOneClient(
            scope=make_scope(ConnectorName.NINJAONE, [NINJA_SITE]),
            secrets=dict(NINJA_TEST_SECRETS),
            limits=fast_limits,
            inventory=InventoryCache(),
            inventory_ttl_seconds=0,
        )
        assert client.tokens is not TOKENS
        async with client:
            await client.list_devices()

    assert token_route.call_count == 2


@respx.mock
async def test_a_rejected_shared_token_is_replaced_once_for_everyone(
    fast_limits: ConnectorLimits,
) -> None:
    """A 401 discards the token that was rejected, and the refresh serves the next connector."""
    tokens = iter(["first-token", "second-token"])

    def issue(request: httpx.Request) -> httpx.Response:
        body = {**load_fixture("ninjaone", "token.json"), "access_token": next(tokens)}
        return httpx.Response(200, json=body)

    token_route = respx.post(TOKEN_URL).mock(side_effect=issue)

    def devices(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer first-token":
            return httpx.Response(401, json=load_fixture("ninjaone", "error_401.json"))
        return httpx.Response(200, json=[])

    respx.get(DEVICES_URL).mock(side_effect=devices)
    store = TokenStore()

    for _ in range(2):
        async with make_ninja_client(
            limits=fast_limits,
            token_store=store,
            credential_version="k1",
            inventory_ttl_seconds=0,
        ) as client:
            await client.list_devices()

    assert token_route.call_count == 2  # the first, and one replacement; not one per connector


def test_a_stale_rejection_does_not_discard_a_newer_token() -> None:
    store = TokenStore()
    key = (uuid.uuid4(), "k1", "us", "monitoring")
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    store.put(key, "new", later)

    store.discard(key, "old")  # a connector reporting the token it used, already replaced
    assert store.get(key) == "new"
    store.discard(key, "new")
    assert store.get(key) is None
    assert "new" not in repr(store._tokens)  # a token is never rendered


@respx.mock
async def test_a_rejected_token_is_refreshed_once_then_fails(
    fast_limits: ConnectorLimits,
) -> None:
    token_route = mock_token()
    devices_route = respx.get(DEVICES_URL).mock(
        return_value=httpx.Response(401, json=load_fixture("ninjaone", "error_401.json"))
    )

    async with make_ninja_client(limits=fast_limits) as client:
        with pytest.raises(AuthError):
            await client.list_devices()

    assert token_route.call_count == 2  # initial, then one refresh after the 401
    assert devices_route.call_count == 2


@respx.mock
async def test_bad_credentials_surface_as_an_auth_error(fast_limits: ConnectorLimits) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json=load_fixture("ninjaone", "error_401.json"))
    )

    async with make_ninja_client(limits=fast_limits) as client:
        with pytest.raises(AuthError) as caught:
            await client.list_devices()

    assert caught.value.details["detail"] == "The client credentials are not valid"


@respx.mock
async def test_a_token_response_we_cannot_read_is_an_auth_error(
    fast_limits: ConnectorLimits,
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))

    async with make_ninja_client(limits=fast_limits) as client:
        with pytest.raises(AuthError, match="could not read"):
            await client.list_devices()


async def test_missing_credential_fields_fail_before_any_request(
    fast_limits: ConnectorLimits,
) -> None:
    async with make_ninja_client(limits=fast_limits, secrets={"client_id": "x"}) as client:
        with pytest.raises(AuthError, match="client_secret"):
            await client.list_devices()


# --- configuration --------------------------------------------------------


def test_region_selects_the_base_url(fast_limits: ConnectorLimits) -> None:
    client = make_ninja_client(limits=fast_limits, config={"region": "eu"})
    assert client.base_url == REGION_BASE_URLS["eu"]


def test_an_explicit_base_url_wins(fast_limits: ConnectorLimits) -> None:
    client = make_ninja_client(
        limits=fast_limits, config={"region": "eu", "base_url": "https://x.test/"}
    )
    assert client.base_url == "https://x.test"


def test_the_default_region_is_us(fast_limits: ConnectorLimits) -> None:
    assert make_ninja_client(limits=fast_limits).base_url == REGION_BASE_URLS["us"]


def test_an_unknown_region_fails_loudly(fast_limits: ConnectorLimits) -> None:
    client = make_ninja_client(limits=fast_limits, config={"region": "atlantis"})
    with pytest.raises(AuthError, match="unknown NinjaOne region"):
        _ = client.base_url


def test_a_non_numeric_organisation_mapping_is_rejected(fast_limits: ConnectorLimits) -> None:
    client = make_ninja_client(
        limits=fast_limits, sites=[site_ref("acme-corp", slug="badly-mapped")]
    )
    with pytest.raises(UpstreamError, match="not a number"):
        _ = client.scope.targets[0].as_int("ninjaone")


# --- devices --------------------------------------------------------------


@respx.mock
async def test_devices_are_parsed_from_the_fixture(fast_limits: ConnectorLimits) -> None:
    mock_token()
    respx.get(DEVICES_URL).mock(side_effect=single_page(load_fixture("ninjaone", "devices.json")))

    async with make_ninja_client(limits=fast_limits) as client:
        devices, truncated = await client.list_devices()

    assert truncated is False
    assert [device.id for _, device in devices] == [1001, 1002, 1003, 1004, 1005]

    _, server = devices[0]
    assert server.name == "TL-AUS-DC01"
    assert server.node_class == "WINDOWS_SERVER"
    assert server.offline is False
    assert server.last_contact == FROZEN_NOW - dt.timedelta(minutes=30)


@respx.mock
async def test_an_empty_device_list_is_a_valid_answer(fast_limits: ConnectorLimits) -> None:
    """Hard rule 7: no devices is a fact, not a failure."""
    mock_token()
    respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", "devices_empty.json"))
    )

    async with make_ninja_client(limits=fast_limits) as client:
        devices, truncated = await client.list_devices()

    assert devices == []
    assert truncated is False


@respx.mock
async def test_devices_are_paginated_by_node_id(fast_limits: ConnectorLimits) -> None:
    mock_token()
    pages: dict[str | None, list[dict[str, object]]] = {
        None: load_fixture("ninjaone", "devices_page_1.json"),
        "1002": load_fixture("ninjaone", "devices_page_2.json"),
        "1004": load_fixture("ninjaone", "devices_page_3.json"),
        "1005": [],
    }

    def responder(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        assert request.url.params["pageSize"] == "2"
        return httpx.Response(200, json=pages[after])

    route = respx.get(DEVICES_URL).mock(side_effect=responder)

    async with make_ninja_client(limits=fast_limits, devices_page_size=2) as client:
        devices, truncated = await client.list_devices()

    # The tenant listing pages until an *empty* page: the spec declares no
    # maximum page size, so a short page might be a silent vendor cap rather
    # than the end. One extra request buys a list we know is complete.
    assert route.call_count == 4
    assert [device.id for _, device in devices] == [1001, 1002, 1003, 1004, 1005]
    assert truncated is False


@respx.mock
async def test_a_named_site_uses_the_organisation_path(fast_limits: ConnectorLimits) -> None:
    """Scoped reads keep the vendor doing the scoping; the tenant listing is untouched."""
    mock_token()
    tenant = respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))
    org = respx.get(ORG_DEVICES_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "devices.json"))
    )
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental")
    legal = site_ref(77, slug="bright-legal")

    async with make_ninja_client(
        limits=fast_limits, sites=[dental, legal], targets=[dental]
    ) as client:
        devices, _ = await client.list_devices()

    assert org.call_count == 1
    assert tenant.call_count == 0
    assert len(devices) == 5


# --- the inventory cache --------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@respx.mock
async def test_the_inventory_is_served_from_cache_within_the_ttl(
    fast_limits: ConnectorLimits,
) -> None:
    mock_token()
    route = respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", "devices.json"))
    )
    clock = FakeClock()
    cache = InventoryCache(clock=clock)

    async with make_ninja_client(
        limits=fast_limits, inventory=cache, inventory_ttl_seconds=180
    ) as client:
        first, _ = await client.list_devices()
        clock.now += 100
        second, _ = await client.list_devices()
        assert route.call_count == REQUESTS_PER_LISTING
        assert [d.id for _, d in first] == [d.id for _, d in second]
        assert cache.hits == 1

        clock.now += 100  # past the TTL now
        await client.list_devices()
        assert route.call_count == 2 * REQUESTS_PER_LISTING


@respx.mock
async def test_the_cache_is_shared_across_connector_instances_for_one_client(
    fast_limits: ConnectorLimits,
) -> None:
    """Every tool call builds a fresh connector; the cache must outlive them."""
    mock_token()
    route = respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", "devices.json"))
    )
    cache = InventoryCache()

    for _ in range(3):
        async with make_ninja_client(limits=fast_limits, inventory=cache) as client:
            await client.list_devices()

    assert route.call_count == REQUESTS_PER_LISTING


@respx.mock
async def test_a_credential_change_misses_the_cache(fast_limits: ConnectorLimits) -> None:
    mock_token()
    route = respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", "devices.json"))
    )
    cache = InventoryCache()

    async with make_ninja_client(
        limits=fast_limits, inventory=cache, credential_version="v1:2026-09-01"
    ) as client:
        await client.list_devices()
    async with make_ninja_client(
        limits=fast_limits, inventory=cache, credential_version="v1:2026-09-17"
    ) as client:
        await client.list_devices()

    assert route.call_count == 2 * REQUESTS_PER_LISTING
    assert len(cache) == 1  # the old entry was evicted, not kept beside the new one


@respx.mock
async def test_a_zero_ttl_disables_the_cache(fast_limits: ConnectorLimits) -> None:
    mock_token()
    route = respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", "devices.json"))
    )

    async with make_ninja_client(limits=fast_limits, inventory_ttl_seconds=0) as client:
        await client.list_devices()
        await client.list_devices()

    assert route.call_count == 2 * REQUESTS_PER_LISTING


@respx.mock
async def test_a_cached_whole_client_inventory_serves_a_named_sites_device_list(
    fast_limits: ConnectorLimits,
) -> None:
    """Wide serves narrow: no request at all, and only the named site's devices come back."""
    mock_token()
    respx.get(DEVICES_URL).mock(side_effect=single_page(load_fixture("ninjaone", "devices.json")))
    org = respx.get(ORG_DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))
    cache = InventoryCache()
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental")
    legal = site_ref(77, slug="bright-legal")

    async with make_ninja_client(limits=fast_limits, sites=[dental, legal], inventory=cache) as c:
        everything, _ = await c.list_devices()  # fills the cache
    async with make_ninja_client(
        limits=fast_limits, sites=[dental, legal], targets=[dental], inventory=cache
    ) as c:
        mine, _ = await c.list_devices()

    assert org.call_count == 0
    assert {site.slug for site, _ in mine} == {"acme-dental"}
    assert len(mine) == len(everything)  # the fixture is all one site


@respx.mock
async def test_a_named_site_with_a_cold_cache_lists_that_site_not_the_tenant(
    fast_limits: ConnectorLimits,
) -> None:
    """Only an unscoped call touches the tenant inventory."""
    mock_token()
    tenant = respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))
    org = respx.get(ORG_DEVICES_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "devices.json"))
    )
    cache = InventoryCache()
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental")
    legal = site_ref(77, slug="bright-legal")

    async with make_ninja_client(
        limits=fast_limits, sites=[dental, legal], targets=[dental], inventory=cache
    ) as c:
        first, _ = await c.list_devices()
        again, _ = await c.list_devices()  # within the TTL: the site listing is cached too

    assert tenant.call_count == 0
    assert org.call_count == 1
    assert len(first) == len(again) == 5
    held = cache.get(c.scope.client_id, version=c.credential_version, targets=[dental.site_id])
    assert held is not None
    assert held.source == "sites"


@respx.mock
async def test_concurrent_unscoped_misses_share_one_listing(
    fast_limits: ConnectorLimits,
) -> None:
    """Found live: a model's two parallel tool calls each listed the whole tenant.

    The loop runs parallel tool calls together, each on its own connector, so
    both miss the same cold cache. The first fetches; the second waits for it.
    """
    token = mock_token()
    release = asyncio.Event()
    pages = single_page(load_fixture("ninjaone", "devices.json"))
    seen: list[httpx.Request] = []

    async def slow_listing(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        await release.wait()  # hold the first fetch open until both calls are in flight
        return pages(request)

    respx.get(DEVICES_URL).mock(side_effect=slow_listing)
    cache = InventoryCache()

    async def one_call() -> int:
        async with make_ninja_client(limits=fast_limits, inventory=cache) as client:
            rows, _ = await client.list_devices()
            return len(rows)

    first = asyncio.create_task(one_call())
    second = asyncio.create_task(one_call())
    for _ in range(20):  # let both reach the cache before the vendor answers
        await asyncio.sleep(0)
    release.set()
    counts = await asyncio.gather(first, second)

    assert counts == [5, 5]
    assert len(seen) == REQUESTS_PER_LISTING  # one listing between them, not two
    assert (cache.hits, cache.misses) == (1, 2)  # both missed; the waiter was then served
    assert token.call_count == 1  # the waiter never had to call the vendor at all


@respx.mock
async def test_a_failed_fetch_does_not_wedge_the_waiters(fast_limits: ConnectorLimits) -> None:
    """The lock is released on error, and the next caller fetches for itself."""
    mock_token()
    answers = iter([httpx.Response(401, json=load_fixture("ninjaone", "error_401.json"))] * 2)
    pages = single_page(load_fixture("ninjaone", "devices.json"))

    def flaky(request: httpx.Request) -> httpx.Response:
        return next(answers, None) or pages(request)

    respx.get(DEVICES_URL).mock(side_effect=flaky)
    cache = InventoryCache()

    async with make_ninja_client(limits=fast_limits, inventory=cache) as client:
        with pytest.raises(AuthError):
            await client.list_devices()
    async with make_ninja_client(limits=fast_limits, inventory=cache) as client:
        rows, _ = await client.list_devices()

    assert len(rows) == 5


def _two_site_tenant() -> Any:
    template = load_fixture("ninjaone", "devices.json")[0]
    return respx.get(DEVICES_URL).mock(
        side_effect=single_page(
            [
                {**template, "id": 1, "organizationId": NINJA_ORGANIZATION_ID},
                {**template, "id": 2, "organizationId": 77},
                {**template, "id": 3, "organizationId": 77},
            ]
        )
    )


@respx.mock
async def test_an_unscoped_call_after_a_site_scoped_one_refetches_the_tenant(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """Found live: a named site's inventory was cached for the client as a whole.

    The next unscoped question would have counted one site's devices as the
    franchisee's. The cache key now carries the scope a fetch ran under, so a
    site-scoped entry cannot answer a wider question: the unscoped call
    misses, lists the tenant again, and sees every site.
    """
    mock_token()
    devices = _two_site_tenant()
    template = load_fixture("ninjaone", "devices.json")[0]
    site_listing = respx.get(ORG_DEVICES_URL).mock(
        return_value=httpx.Response(
            200, json=[{**template, "id": 1, "organizationId": NINJA_ORGANIZATION_ID}]
        )
    )
    alert, _ = load_fixture("ninjaone", "alerts_no_device.json")
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=[{**alert, "deviceId": 1}]))
    cache = InventoryCache()
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental")
    legal = site_ref(77, slug="bright-legal")

    with caplog.at_level(logging.WARNING, logger="app.connectors.ninjaone.client"):
        async with make_ninja_client(
            limits=fast_limits, sites=[dental, legal], targets=[dental], inventory=cache
        ) as scoped:
            alerts = await scoped.list_alerts()  # resolves through the site's own listing

    assert [site.slug for site, _ in alerts] == ["acme-dental"]
    assert not caplog.records
    assert site_listing.call_count == 1
    assert devices.call_count == 0  # a named site never lists the tenant
    # What was cached is honestly narrow: the one site, under a key that says so.
    held = cache.get(
        scoped.scope.client_id, version=scoped.credential_version, targets=[dental.site_id]
    )
    assert held is not None
    assert held.coverage == {dental.site_id}
    assert [device.id for _, device in held.rows] == [1]

    async with make_ninja_client(
        limits=fast_limits, sites=[dental, legal], inventory=cache
    ) as unscoped:
        listed, _ = await unscoped.list_devices()

    assert devices.call_count == REQUESTS_PER_LISTING  # listed now: the narrow entry was not reused
    assert sorted(device.id for _, device in listed) == [1, 2, 3]


@respx.mock
async def test_a_whole_client_inventory_serves_a_named_site_narrowed(
    fast_limits: ConnectorLimits,
) -> None:
    """The safe direction: wide serves narrow, and the reader gets only its own site."""
    mock_token()
    devices = _two_site_tenant()
    alert, _ = load_fixture("ninjaone", "alerts_no_device.json")
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=[{**alert, "deviceId": 1}]))
    cache = InventoryCache()
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental")
    legal = site_ref(77, slug="bright-legal")

    async with make_ninja_client(
        limits=fast_limits, sites=[dental, legal], inventory=cache
    ) as unscoped:
        await unscoped.list_devices()
    async with make_ninja_client(
        limits=fast_limits, sites=[dental, legal], targets=[dental], inventory=cache
    ) as scoped:
        alerts = await scoped.list_alerts()
        mine, _ = await scoped.list_all_devices()

    assert devices.call_count == REQUESTS_PER_LISTING  # one listing served both
    assert [site.slug for site, _ in alerts] == ["acme-dental"]
    assert [device.id for _, device in mine] == [1]
    assert len(cache) == 1


def test_the_cache_never_serves_an_entry_that_lacks_a_targeted_site() -> None:
    """The rule itself, without a connector: coverage is a superset test, not an overlap."""
    cache = InventoryCache()
    client_id, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cache.put(
        client_id,
        version="v1",
        targets=[a],
        rows=[],
        device_orgs={},
        truncated=False,
        ttl_seconds=60,
    )

    assert cache.get(client_id, version="v1", targets=[a]) is not None
    assert cache.get(client_id, version="v1", targets=[a, b]) is None
    assert cache.get(client_id, version="v1", targets=[b]) is None
    assert cache.get(uuid.uuid4(), version="v1", targets=[a]) is None

    cache.put(
        client_id,
        version="v1",
        targets=[a, b],
        rows=[],
        device_orgs={},
        truncated=False,
        ttl_seconds=60,
    )
    assert len(cache) == 1  # the wider entry replaced the narrower one
    assert cache.get(client_id, version="v1", targets=[b]) is not None
    cache.invalidate(client_id)
    assert len(cache) == 0


# --- the guards on the tenant path ----------------------------------------


@respx.mock
async def test_an_inactive_site_on_the_tenant_path_is_dropped_quietly(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """A deactivated customer's devices are ours, just not in scope: no breach alarm."""
    mock_token()
    template = load_fixture("ninjaone", "devices.json")[0]
    respx.get(DEVICES_URL).mock(
        side_effect=single_page([template, {**template, "id": 2001, "organizationId": 99}])
    )
    archived = site_ref(99, slug="zz-archived")

    with caplog.at_level(logging.DEBUG, logger="app.connectors.ninjaone.client"):
        async with make_ninja_client(limits=fast_limits, excluded=[archived]) as client:
            devices, _ = await client.list_devices()

    assert [device.id for _, device in devices] == [1001]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("inactive sites" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_an_unknown_organisation_on_the_tenant_path_is_an_error(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """Hard rule 1 on the tenant path: an org we do not know is never merged in."""
    mock_token()
    respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", "devices_foreign_org.json"))
    )

    with caplog.at_level(logging.ERROR, logger="app.connectors.ninjaone.client"):
        async with make_ninja_client(limits=fast_limits) as client:
            devices, _ = await client.list_devices()

    assert [device.id for _, device in devices] == [1001]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "outside the allowlist" in errors[0].getMessage()
    assert "sync_sites" in errors[0].getMessage()


# --- concurrency and rate-limit headers -----------------------------------


@respx.mock
async def test_alert_fan_out_is_bounded_by_the_semaphore(fast_limits: ConnectorLimits) -> None:
    sites = [site_ref(100 + n, slug=f"site-{n}") for n in range(8)]
    mock_token()
    in_flight = 0
    peak = 0

    async def responder(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json=[])

    route = respx.get(ALERTS_URL).mock(side_effect=responder)

    async with make_ninja_client(limits=fast_limits, sites=sites, concurrency=3) as client:
        await client.list_alerts()

    assert route.call_count == 8
    assert peak == 3  # concurrent, but never more than the setting allows


@respx.mock
async def test_rate_limit_headers_are_recorded_and_logged(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    mock_token()
    respx.get(DEVICES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "0"},
        )
    )

    with caplog.at_level(logging.DEBUG, logger="app.connectors.base"):
        async with make_ninja_client(limits=fast_limits) as client:
            await client.list_devices()

    assert client.last_rate_limit == {"x-ratelimit-limit": "100", "x-ratelimit-remaining": "0"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "x-ratelimit-remaining=0" in warnings[0].getMessage()


@respx.mock
async def test_devices_from_another_organisation_are_dropped(
    fast_limits: ConnectorLimits,
) -> None:
    """Defence in depth for hard rule 1: never return another client's device."""
    mock_token()
    respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", "devices_foreign_org.json"))
    )

    async with make_ninja_client(limits=fast_limits) as client:
        devices, _ = await client.list_devices()

    assert [device.id for _, device in devices] == [1001]
    assert all(site.external_id == str(NINJA_ORGANIZATION_ID) for site, _ in devices)


@respx.mock
async def test_rate_limiting_surfaces_after_the_retry_budget(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    mock_token()
    respx.get(DEVICES_URL).mock(
        return_value=httpx.Response(429, json=load_fixture("ninjaone", "error_429.json"))
    )

    async with make_ninja_client(limits=fast_limits, sleep=sleeper) as client:
        with pytest.raises(RateLimited):
            await client.list_devices()

    assert len(sleeper.delays) == fast_limits.max_retries


@respx.mock
async def test_a_server_error_surfaces_as_upstream(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    mock_token()
    respx.get(DEVICES_URL).mock(
        return_value=httpx.Response(500, json=load_fixture("ninjaone", "error_500.json"))
    )

    async with make_ninja_client(limits=fast_limits, sleep=sleeper) as client:
        with pytest.raises(UpstreamError) as caught:
            await client.list_devices()

    assert caught.value.status_code == 500


# --- alerts ---------------------------------------------------------------


@respx.mock
async def test_alerts_are_parsed_and_scoped(fast_limits: ConnectorLimits) -> None:
    mock_token()
    route = respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts.json"))
    )

    async with make_ninja_client(limits=fast_limits) as client:
        alerts = await client.list_alerts()

    assert route.calls[0].request.url.params["df"] == f"org={NINJA_ORGANIZATION_ID}"
    assert [alert.device_id for _, alert in alerts] == [1002, 1003]
    assert alerts[0][1].subject == "Agent offline"
    assert alerts[1][1].severity == "MODERATE"
    assert alerts[0][1].create_time == FROZEN_NOW - dt.timedelta(hours=240)


@respx.mock
async def test_alerts_without_an_embedded_device_are_resolved_through_the_inventory(
    fast_limits: ConnectorLimits,
) -> None:
    """What the live tenant sends: a deviceId and no device object.

    Without resolution every alert was dropped as unclassifiable. The
    inventory's device map places each one, and lends the alert the device
    name the vendor left out.
    """
    mock_token()
    devices = respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", "devices.json"))
    )
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts_no_device.json"))
    )

    async with make_ninja_client(limits=fast_limits) as client:
        rows = await client.list_alerts()

    assert [(site.slug, alert.device_id) for site, alert in rows] == [
        ("acme-dental", 1002),
        ("acme-dental", 1003),
    ]
    assert [alert.device.name for _, alert in rows if alert.device] == [
        "TL-AUS-WS14",
        "TL-AUS-WS22",
    ]
    assert devices.call_count == REQUESTS_PER_LISTING


@respx.mock
async def test_alert_resolution_reads_the_cached_inventory(fast_limits: ConnectorLimits) -> None:
    mock_token()
    devices = respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", "devices.json"))
    )
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts_no_device.json"))
    )
    cache = InventoryCache()

    async with make_ninja_client(limits=fast_limits, inventory=cache) as client:
        await client.list_devices()  # warms the inventory
        rows = await client.list_alerts()

    assert len(rows) == 2
    assert devices.call_count == REQUESTS_PER_LISTING  # no second listing for the alerts


@respx.mock
async def test_alerts_that_embed_their_device_need_no_inventory(
    fast_limits: ConnectorLimits,
) -> None:
    mock_token()
    devices = respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts.json"))
    )

    async with make_ninja_client(limits=fast_limits) as client:
        rows = await client.list_alerts()

    assert len(rows) == 2
    assert devices.call_count == 0


@respx.mock
async def test_an_alert_for_a_device_not_in_the_inventory_is_dropped_with_an_error(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """A device the tenant listing does not know cannot be placed: not a warning."""
    mock_token()
    respx.get(DEVICES_URL).mock(side_effect=single_page(load_fixture("ninjaone", "devices.json")))
    known, _ = load_fixture("ninjaone", "alerts_no_device.json")
    stranger = {**known, "uid": "11111111-1111-4111-8111-000000000099", "deviceId": 9999}
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=[known, stranger]))

    with caplog.at_level(logging.WARNING, logger="app.connectors.ninjaone.client"):
        async with make_ninja_client(limits=fast_limits) as client:
            rows = await client.list_alerts()

    assert [alert.device_id for _, alert in rows] == [1002]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "/v2/alerts" in errors[0].getMessage()
    assert "not in the tenant inventory" in errors[0].getMessage()
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


@respx.mock
async def test_an_alert_on_an_inactive_sites_device_is_dropped_quietly(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    mock_token()
    template = load_fixture("ninjaone", "devices.json")[0]
    respx.get(DEVICES_URL).mock(
        side_effect=single_page([template, {**template, "id": 2001, "organizationId": 99}])
    )
    alert, _ = load_fixture("ninjaone", "alerts_no_device.json")
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=[{**alert, "deviceId": 2001}]))
    archived = site_ref(99, slug="zz-archived")

    with caplog.at_level(logging.WARNING, logger="app.connectors.ninjaone.client"):
        async with make_ninja_client(limits=fast_limits, excluded=[archived]) as client:
            rows = await client.list_alerts()

    assert rows == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@respx.mock
async def test_the_guard_names_the_endpoint_that_produced_an_unclassifiable_row(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """An alert with neither a device id nor a device object is dropped; the log says where from."""
    mock_token()
    alert, _ = load_fixture("ninjaone", "alerts_no_device.json")
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=[{**alert, "deviceId": None}]))

    with caplog.at_level(logging.WARNING, logger="app.connectors.ninjaone.client"):
        async with make_ninja_client(limits=fast_limits) as client:
            rows = await client.list_alerts()

    assert rows == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "GET /v2/alerts" in warnings[0].getMessage()
    assert "1 with no organisation id" in warnings[0].getMessage()


@respx.mock
async def test_alerts_from_another_organisation_are_dropped(
    fast_limits: ConnectorLimits,
) -> None:
    """`/v2/alerts` has no org-scoped path, so the client-side filter is the guarantee."""
    mock_token()
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts_foreign_org.json"))
    )

    async with make_ninja_client(limits=fast_limits) as client:
        alerts = await client.list_alerts()

    assert [alert.device_id for _, alert in alerts] == [1002]


@respx.mock
async def test_no_alerts_is_a_valid_answer(fast_limits: ConnectorLimits) -> None:
    mock_token()
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts_empty.json"))
    )

    async with make_ninja_client(limits=fast_limits) as client:
        assert await client.list_alerts() == []


@respx.mock
async def test_an_alert_payload_of_the_wrong_shape_is_an_upstream_error(
    fast_limits: ConnectorLimits,
) -> None:
    mock_token()
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json={"alerts": []}))

    async with make_ninja_client(limits=fast_limits) as client:
        with pytest.raises(UpstreamError, match="list of alerts"):
            await client.list_alerts()


# --- timestamp handling ---------------------------------------------------


def test_epoch_seconds_become_aware_utc_datetimes() -> None:
    parsed = epoch_to_datetime(FROZEN_NOW.timestamp())
    assert parsed == FROZEN_NOW
    assert parsed is not None
    assert parsed.tzinfo is dt.UTC


@pytest.mark.parametrize("value", [None, 0, -1, 1_700_000_000_000])
def test_implausible_timestamps_become_unknown_rather_than_a_wrong_date(value: float) -> None:
    """A millisecond timestamp read as seconds would claim the year 55000."""
    assert epoch_to_datetime(value) is None


def test_a_device_with_no_last_contact_reports_unknown_not_zero() -> None:
    device = NinjaDevice.model_validate({"id": 1, "organizationId": 42, "lastContact": None})
    assert device.last_contact is None
    assert device.days_since_contact(now=FROZEN_NOW) is None


def test_device_name_falls_back_through_the_fields_ninjaone_provides() -> None:
    assert NinjaDevice.model_validate({"id": 1, "displayName": "D"}).name == "D"
    assert NinjaDevice.model_validate({"id": 1, "dnsName": "d.local"}).name == "d.local"
    # Never invents a name; identifies the device by its id instead.
    assert NinjaDevice.model_validate({"id": 7}).name == "device 7"


def test_unknown_fields_in_a_vendor_response_are_ignored() -> None:
    device = NinjaDevice.model_validate(
        {"id": 1, "organizationId": 42, "somethingNinjaAddedLater": {"nested": True}}
    )
    assert device.id == 1

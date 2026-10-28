"""The NinjaOne `/v2/queries/*` reader: cursor paging, totals, and df narrowing."""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest
import respx

from app.connectors.base import ConnectorLimits
from app.connectors.errors import UpstreamError
from tests.connectors.conftest import (
    NINJA_BASE_URL,
    NINJA_ORGANIZATION_ID,
    load_fixture,
    make_ninja_client,
    site_ref,
)

TOKEN_URL = f"{NINJA_BASE_URL}/ws/oauth/token"
REPORT_URL = f"{NINJA_BASE_URL}/v2/queries/logged-on-users"


def mock_token() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )


def page(rows: list[dict[str, Any]], *, name: str, offset: int, count: int) -> dict[str, Any]:
    return {
        "cursor": {"name": name, "offset": offset, "count": count, "expires": 1789656609.0},
        "results": rows,
    }


@respx.mock
async def test_the_cursor_is_followed_until_the_count_is_reached(
    fast_limits: ConnectorLimits,
) -> None:
    mock_token()
    calls: list[dict[str, str]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        assert params["pageSize"] == "2"
        if "cursor" not in params:
            return httpx.Response(
                200, json=page([{"deviceId": 1}, {"deviceId": 2}], name="c1", offset=0, count=5)
            )
        if params["cursor"] == "c1":
            return httpx.Response(
                200, json=page([{"deviceId": 3}, {"deviceId": 4}], name="c1", offset=2, count=5)
            )
        raise AssertionError(f"unexpected cursor {params['cursor']}")

    route = respx.get(REPORT_URL).mock(side_effect=responder)

    async with make_ninja_client(limits=fast_limits) as client:
        # The third page would be short (offset 4 of 5) — but the vendor's
        # count says 1 row remains, so it is fetched.
        respx.get(REPORT_URL).mock(side_effect=responder)
        rows, truncated, total = await client.query("logged-on-users", page_size=2, max_pages=2)

    assert route.call_count == 2
    assert truncated is True  # the cap of two pages stopped us before row 5
    assert total == 5
    assert [row["deviceId"] for row in rows] == [1, 2, 3, 4]


@respx.mock
async def test_the_walk_stops_when_offset_plus_page_reaches_the_count(
    fast_limits: ConnectorLimits,
) -> None:
    mock_token()

    def responder(request: httpx.Request) -> httpx.Response:
        if "cursor" not in request.url.params:
            return httpx.Response(
                200, json=page([{"deviceId": 1}, {"deviceId": 2}], name="c1", offset=0, count=3)
            )
        return httpx.Response(200, json=page([{"deviceId": 3}], name="c1", offset=2, count=3))

    route = respx.get(REPORT_URL).mock(side_effect=responder)

    async with make_ninja_client(limits=fast_limits) as client:
        rows, truncated, total = await client.query("logged-on-users", page_size=2)

    assert route.call_count == 2
    assert truncated is False
    assert total == 3
    assert len(rows) == 3


@respx.mock
async def test_an_empty_report_is_one_request(fast_limits: ConnectorLimits) -> None:
    mock_token()
    route = respx.get(REPORT_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "query_empty.json"))
    )
    async with make_ninja_client(limits=fast_limits) as client:
        rows, truncated, total = await client.query("logged-on-users")
    assert route.call_count == 1
    assert rows == [] and truncated is False and total == 0


@respx.mock
async def test_query_scoped_fans_out_with_df_per_target(fast_limits: ConnectorLimits) -> None:
    mock_token()
    seen: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["df"])
        # With df the vendor reports no count (api-notes): a short page ends it.
        return httpx.Response(
            200,
            json={
                "cursor": {"name": "x", "offset": 0, "count": None, "expires": 0},
                "results": [{"deviceId": 1}],
            },
        )

    respx.get(REPORT_URL).mock(side_effect=responder)
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental")
    legal = site_ref(77, slug="bright-legal")

    async with make_ninja_client(
        limits=fast_limits, sites=[dental, legal], targets=[dental]
    ) as client:
        rows, truncated, total = await client.query_scoped("logged-on-users")

    assert seen == [f"org={NINJA_ORGANIZATION_ID}"]
    assert len(rows) == 1 and total is None and truncated is False


@respx.mock
async def test_query_scoped_reads_the_tenant_when_no_site_is_named(
    fast_limits: ConnectorLimits,
) -> None:
    mock_token()
    route = respx.get(REPORT_URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("ninjaone", "query_logged_on_users.json")
        )
    )
    limits = dataclasses.replace(fast_limits, max_pages=3)
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental")
    legal = site_ref(77, slug="bright-legal")

    async with make_ninja_client(limits=limits, sites=[dental, legal]) as client:
        rows, _, total = await client.query_scoped("logged-on-users")

    assert route.call_count == 1
    assert "df" not in route.calls[0].request.url.params
    assert total == 3 and len(rows) == 3


@respx.mock
async def test_a_cursor_that_restarts_or_skips_is_an_error_not_a_quiet_read(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """Every page after the first re-sends the same cursor name: the server keeps
    the position. When the server's offset disagrees with ours the read is not
    whole — behind us the rows would repeat, ahead of us rows were skipped — and
    the walk raises rather than returning it; each page is logged at DEBUG."""
    mock_token()
    pages = iter(
        [
            page([{"deviceId": 1}, {"deviceId": 2}], name="c1", offset=0, count=6),
            page([{"deviceId": 1}, {"deviceId": 2}], name="c1", offset=0, count=6),  # restart
        ]
    )
    respx.get(REPORT_URL).mock(side_effect=lambda request: httpx.Response(200, json=next(pages)))
    async with make_ninja_client(limits=fast_limits) as client:
        with caplog.at_level("DEBUG", logger="app.connectors.ninjaone.client"):
            with pytest.raises(UpstreamError, match="cursor restarted"):
                await client.query("logged-on-users", page_size=2)
    logged = [r.getMessage() for r in caplog.records if "page" in r.getMessage()]
    assert any("page 1: cursor=c1 offset=0" in m for m in logged)
    assert any("page 2: cursor=c1 offset=0" in m for m in logged)

    skipping = iter(
        [
            page([{"deviceId": 1}, {"deviceId": 2}], name="c1", offset=0, count=6),
            page([{"deviceId": 5}, {"deviceId": 6}], name="c1", offset=4, count=6),  # a page lost
        ]
    )
    respx.get(REPORT_URL).mock(side_effect=lambda request: httpx.Response(200, json=next(skipping)))
    async with make_ninja_client(limits=fast_limits) as client:
        with pytest.raises(UpstreamError, match="skipped rows"):
            await client.query("logged-on-users", page_size=2)


@respx.mock
async def test_the_same_cursor_name_on_every_later_page_is_the_protocol(
    fast_limits: ConnectorLimits,
) -> None:
    mock_token()
    sent: list[str | None] = []

    def responder(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.params.get("cursor"))
        n = len(sent)
        if n == 1:
            return httpx.Response(
                200, json=page([{"deviceId": 1}, {"deviceId": 2}], name="c1", offset=0, count=5)
            )
        if n == 2:
            return httpx.Response(
                200, json=page([{"deviceId": 3}, {"deviceId": 4}], name="c1", offset=2, count=5)
            )
        return httpx.Response(200, json=page([{"deviceId": 5}], name="c1", offset=4, count=5))

    respx.get(REPORT_URL).mock(side_effect=responder)
    async with make_ninja_client(limits=fast_limits) as client:
        rows, truncated, total = await client.query("logged-on-users", page_size=2)
    assert sent == [None, "c1", "c1"]  # the name repeats; the offsets advance
    assert [r["deviceId"] for r in rows] == [1, 2, 3, 4, 5] and not truncated and total == 5

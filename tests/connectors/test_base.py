"""BaseConnector: retry, backoff, error normalisation and pagination.

Exercised through a throwaway connector so the behaviour is tested once, here,
rather than again in every vendor's suite.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest
import respx

from app.connectors.base import BaseConnector, ConnectorLimits, parse_retry_after
from app.connectors.errors import (
    AuthError,
    NotFound,
    RateLimited,
    UpstreamError,
)
from app.models.enums import ConnectorName
from tests.connectors.conftest import RecordingSleep, make_scope, site_ref

BASE_URL = "https://example.test"

#: These tests exercise the base class, not tenancy: one client, one site.
DEMO_SCOPE = make_scope(ConnectorName.NINJAONE, [site_ref(1, slug="demo-site")])


class DemoConnector(BaseConnector):
    """A connector with a token, used only to drive the base class."""

    name: ClassVar[ConnectorName] = ConnectorName.NINJAONE
    default_base_url: ClassVar[str] = BASE_URL

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.token: str | None = None
        self.authentications = 0

    def _is_authenticated(self) -> bool:
        return self.token is not None

    async def _authenticate(self) -> None:
        self.authentications += 1
        self.token = f"token-{self.authentications}"

    def _invalidate_auth(self) -> None:
        self.token = None

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def build(limits: ConnectorLimits, sleep: RecordingSleep | None = None) -> DemoConnector:
    return DemoConnector(scope=DEMO_SCOPE, secrets={}, limits=limits, sleep=sleep)


# --- success and auth -----------------------------------------------------


@respx.mock
async def test_a_successful_request_authenticates_once(fast_limits: ConnectorLimits) -> None:
    route = respx.get(f"{BASE_URL}/thing").mock(return_value=httpx.Response(200, json={"ok": 1}))

    async with build(fast_limits) as connector:
        assert await connector.get_json("/thing") == {"ok": 1}
        assert await connector.get_json("/thing") == {"ok": 1}

        assert connector.authentications == 1
    assert route.call_count == 2
    assert route.calls[0].request.headers["Authorization"] == "Bearer token-1"


@respx.mock
async def test_a_401_triggers_exactly_one_reauthentication(fast_limits: ConnectorLimits) -> None:
    """An expired token is invisible to callers; a bad one still fails."""
    route = respx.get(f"{BASE_URL}/thing").mock(
        side_effect=[
            httpx.Response(401, json={"error": "expired"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with build(fast_limits) as connector:
        assert await connector.get_json("/thing") == {"ok": True}
        assert connector.authentications == 2

    assert route.call_count == 2
    assert route.calls[1].request.headers["Authorization"] == "Bearer token-2"


@respx.mock
async def test_a_persistent_401_becomes_an_auth_error(fast_limits: ConnectorLimits) -> None:
    route = respx.get(f"{BASE_URL}/thing").mock(
        return_value=httpx.Response(401, json={"error_description": "credentials are not valid"})
    )

    async with build(fast_limits) as connector:
        with pytest.raises(AuthError) as caught:
            await connector.get_json("/thing")

    # One re-auth, then it gives up: bad credentials must not be retried forever.
    assert route.call_count == 2
    assert caught.value.status_code == 401
    assert caught.value.details["detail"] == "credentials are not valid"


# --- retry on 429 and 5xx -------------------------------------------------


@respx.mock
async def test_429_is_retried_then_succeeds(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    route = respx.get(f"{BASE_URL}/thing").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with build(fast_limits, sleeper) as connector:
        assert await connector.get_json("/thing") == {"ok": True}

    assert route.call_count == 3
    assert sleeper.delays == [0.01, 0.02]  # exponential, base 0.01


@respx.mock
async def test_429_beyond_the_retry_budget_raises_rate_limited(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    route = respx.get(f"{BASE_URL}/thing").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0.03"})
    )

    async with build(fast_limits, sleeper) as connector:
        with pytest.raises(RateLimited) as caught:
            await connector.get_json("/thing")

    assert route.call_count == fast_limits.max_retries + 1
    assert caught.value.retry_after == 0.03
    assert caught.value.retryable is True
    assert sleeper.delays == [0.03, 0.03]  # Retry-After wins over the backoff curve


@respx.mock
async def test_retry_after_is_capped_by_the_configured_maximum(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    """A vendor asking us to sleep for an hour must not stall an agent run."""
    respx.get(f"{BASE_URL}/thing").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3600"}),
            httpx.Response(200, json={}),
        ]
    )

    async with build(fast_limits, sleeper) as connector:
        await connector.get_json("/thing")

    assert sleeper.delays == [fast_limits.backoff_max_seconds]


@respx.mock
async def test_500_is_retried_then_raises_upstream_error(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    route = respx.get(f"{BASE_URL}/thing").mock(return_value=httpx.Response(503))

    async with build(fast_limits, sleeper) as connector:
        with pytest.raises(UpstreamError) as caught:
            await connector.get_json("/thing")

    assert route.call_count == fast_limits.max_retries + 1
    assert caught.value.status_code == 503
    assert caught.value.details["attempts"] == 3


@respx.mock
async def test_a_timeout_is_retried_then_normalised(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    route = respx.get(f"{BASE_URL}/thing").mock(side_effect=httpx.ReadTimeout("too slow"))

    async with build(fast_limits, sleeper) as connector:
        with pytest.raises(UpstreamError, match="did not respond"):
            await connector.get_json("/thing")

    assert route.call_count == fast_limits.max_retries + 1


@respx.mock
async def test_a_connection_failure_is_normalised(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    respx.get(f"{BASE_URL}/thing").mock(side_effect=httpx.ConnectError("refused"))

    async with build(fast_limits, sleeper) as connector:
        with pytest.raises(UpstreamError, match="could not be reached"):
            await connector.get_json("/thing")


# --- error mapping --------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthError),
        (403, AuthError),
        (404, NotFound),
        (400, UpstreamError),
        (422, UpstreamError),
    ],
)
async def test_statuses_map_onto_the_error_hierarchy(
    status: int, expected: type[Exception], fast_limits: ConnectorLimits
) -> None:
    respx.get(f"{BASE_URL}/thing").mock(return_value=httpx.Response(status, json={}))

    async with build(fast_limits) as connector:
        with pytest.raises(expected):
            await connector.get_json("/thing")


@respx.mock
async def test_a_non_json_body_is_an_upstream_error(fast_limits: ConnectorLimits) -> None:
    """An HTML error page must not surface as a JSON parse traceback."""
    respx.get(f"{BASE_URL}/thing").mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )

    async with build(fast_limits) as connector:
        with pytest.raises(UpstreamError, match="not JSON"):
            await connector.get_json("/thing")


async def test_errors_serialise_without_leaking(fast_limits: ConnectorLimits) -> None:
    error = RateLimited("slow down", connector="ninjaone", retry_after=12.0)
    payload = error.as_dict()

    assert payload["error_type"] == "RateLimited"
    assert payload["retry_after"] == 12.0
    assert "secret" not in str(payload).lower()


# --- backoff --------------------------------------------------------------


def test_backoff_grows_exponentially_and_is_capped(fast_limits: ConnectorLimits) -> None:
    connector = build(fast_limits)
    delays = [connector.backoff_delay(attempt) for attempt in range(6)]

    assert delays == [0.01, 0.02, 0.04, 0.05, 0.05, 0.05]
    assert max(delays) <= fast_limits.backoff_max_seconds


def test_jitter_keeps_delays_within_half_the_curve() -> None:
    connector = build(ConnectorLimits(backoff_base_seconds=1.0, backoff_max_seconds=10.0))
    delays = [connector.backoff_delay(2) for _ in range(50)]

    assert all(2.0 <= delay <= 4.0 for delay in delays)
    assert len(set(delays)) > 1  # actually jittered


@pytest.mark.parametrize(
    ("header", "expected"), [("5", 5.0), ("0", 0.0), ("garbage", None), (None, None)]
)
def test_retry_after_parsing(header: str | None, expected: float | None) -> None:
    headers = {"Retry-After": header} if header is not None else {}
    assert parse_retry_after(httpx.Response(429, headers=headers)) == expected


def test_retry_after_accepts_an_http_date() -> None:
    response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"})
    parsed = parse_retry_after(response)

    assert parsed is not None
    assert parsed > 0


# --- pagination -----------------------------------------------------------


@respx.mock
async def test_pagination_follows_the_cursor_to_the_end(fast_limits: ConnectorLimits) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        if after is None:
            return httpx.Response(200, json=[{"id": 1}, {"id": 2}])
        if after == "2":
            return httpx.Response(200, json=[{"id": 3}])
        raise AssertionError(f"unexpected cursor {after!r}")

    respx.get(f"{BASE_URL}/items").mock(side_effect=responder)

    async with build(fast_limits) as connector:
        items, truncated = await connector.paginate(
            "/items", next_cursor=lambda batch: batch[-1]["id"], page_size=2
        )

    assert [item["id"] for item in items] == [1, 2, 3]
    assert truncated is False


@respx.mock
async def test_pagination_reports_truncation_at_the_page_cap(
    fast_limits: ConnectorLimits,
) -> None:
    """Hitting the cap must be visible, not a silently short list."""
    counter = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        base = counter["n"] * 10
        return httpx.Response(200, json=[{"id": base}, {"id": base + 1}])

    respx.get(f"{BASE_URL}/items").mock(side_effect=responder)

    async with build(fast_limits) as connector:
        items, truncated = await connector.paginate(
            "/items", next_cursor=lambda batch: batch[-1]["id"], page_size=2
        )

    assert truncated is True
    assert len(items) == fast_limits.max_pages * 2


@respx.mock
async def test_pagination_stops_when_the_vendor_ignores_the_cursor(
    fast_limits: ConnectorLimits,
) -> None:
    """A server that returns the same page forever must not loop us to the cap."""
    route = respx.get(f"{BASE_URL}/items").mock(
        return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}])
    )

    async with build(fast_limits) as connector:
        items, truncated = await connector.paginate(
            "/items", next_cursor=lambda batch: batch[-1]["id"], page_size=2
        )

    assert route.call_count == 2  # second call repeats the cursor, so we stop
    assert truncated is False
    # The resent page is a repeat, not new data: counted once, not twice.
    assert len(items) == 2


@respx.mock
async def test_pagination_rejects_a_non_list_page(fast_limits: ConnectorLimits) -> None:
    respx.get(f"{BASE_URL}/items").mock(return_value=httpx.Response(200, json={"oops": True}))

    async with build(fast_limits) as connector:
        with pytest.raises(UpstreamError, match="where a list was expected"):
            await connector.paginate("/items", next_cursor=lambda batch: None)


@respx.mock
async def test_an_empty_first_page_is_not_an_error(fast_limits: ConnectorLimits) -> None:
    respx.get(f"{BASE_URL}/items").mock(return_value=httpx.Response(200, json=[]))

    async with build(fast_limits) as connector:
        items, truncated = await connector.paginate("/items", next_cursor=lambda batch: None)

    assert items == []
    assert truncated is False


# --- misc -----------------------------------------------------------------


async def test_using_a_connector_outside_its_context_is_a_clear_error(
    fast_limits: ConnectorLimits,
) -> None:
    connector = build(fast_limits)
    with pytest.raises(RuntimeError, match="outside 'async with'"):
        _ = connector.http


def test_repr_never_shows_credentials(fast_limits: ConnectorLimits) -> None:
    connector = DemoConnector(
        scope=DEMO_SCOPE, secrets={"client_secret": "sup3r-s3cret"}, limits=fast_limits
    )
    assert "sup3r-s3cret" not in repr(connector)


def test_a_missing_credential_field_is_an_auth_error(fast_limits: ConnectorLimits) -> None:
    connector = DemoConnector(scope=DEMO_SCOPE, secrets={}, limits=fast_limits)
    with pytest.raises(AuthError, match="client_secret"):
        connector.secret("client_secret")


def test_limits_come_from_settings() -> None:
    limits = ConnectorLimits.from_settings()
    assert limits.timeout_seconds > 0
    assert limits.max_pages >= 1

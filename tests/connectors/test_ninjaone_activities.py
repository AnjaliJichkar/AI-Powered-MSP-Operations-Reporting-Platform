"""NinjaOne's tenant activity log, read for the business review's "alerts resolved" figure.

The fixture carries the live row shape seen on 2026-09-21 (every key, every
type, the vendor's own enum values) with every value invented. What the live
probe showed is in docs/api-notes/ninjaone.md, "Tenant activity log".
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from app.connectors.base import ConnectorLimits
from app.connectors.errors import UpstreamError
from app.reports.tbr.audit import NullAudit
from app.reports.tbr.collect import ALERTS_RESOLVED, Collector
from app.reports.tbr.config import FranchiseeConfig
from app.reports.tbr.model import ReviewPeriod
from tests.connectors.conftest import (
    NINJA_BASE_URL,
    NINJA_ORGANIZATION_ID,
    load_fixture,
    make_ninja_client,
    single_page,
    site_ref,
)

ACTIVITIES_URL = f"{NINJA_BASE_URL}/v2/activities"
DEVICES_URL = f"{NINJA_BASE_URL}/v2/devices"
SITE_DEVICES_URL = f"{NINJA_BASE_URL}/v2/organization/{NINJA_ORGANIZATION_ID}/devices"
AUGUST = ReviewPeriod.ending(2026, 8)
START, END = AUGUST.start, AUGUST.end


def mock_world() -> None:
    respx.post(f"{NINJA_BASE_URL}/ws/oauth/token").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )
    devices = load_fixture("ninjaone", "devices.json")
    respx.get(DEVICES_URL).mock(side_effect=single_page(devices))
    respx.get(SITE_DEVICES_URL).mock(side_effect=single_page(devices))


def reset_row(row_id: int, when: dt.datetime, device_id: int = 1002) -> dict[str, Any]:
    template = load_fixture("ninjaone", "activities_condition_reset.json")["activities"][0]
    return {**template, "id": row_id, "activityTime": when.timestamp(), "deviceId": device_id}


def log(*rows: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"lastActivityId": 1, "activities": list(rows)})


@respx.mock
async def test_the_fixture_is_placed_checked_and_bounded_by_the_caller(
    fast_limits: ConnectorLimits,
) -> None:
    mock_world()
    route = respx.get(ACTIVITIES_URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("ninjaone", "activities_condition_reset.json")
        )
    )
    async with make_ninja_client(limits=fast_limits) as client:
        placed, truncated = await client.list_activities(
            activity_type="condition", status="reset", after=START, before=END
        )

    sent = dict(route.calls[0].request.url.params)
    assert sent["type"] == "CONDITION" and sent["status"] == "RESET"  # the vendor's spelling
    assert sent["after"] == "2026-08-01" and "df" not in sent  # no site named: the whole tenant
    assert not truncated
    # A TRIGGERED row the filter should have removed, and a device nobody knows, are dropped.
    assert [(site.slug, device.id, row.status_code) for site, device, row in placed] == [
        ("acme-dental", 1002, "RESET"),  # 1 September: the caller bounds it, not this read
        ("acme-dental", 1002, "RESET"),
        ("acme-dental", 1005, "RESET"),
        ("acme-dental", 1002, "RESET"),
    ]


@respx.mock
async def test_a_named_site_is_narrowed_with_df(fast_limits: ConnectorLimits) -> None:
    mock_world()
    route = respx.get(ACTIVITIES_URL).mock(return_value=log())
    here = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental", name="Acme Dental")
    there = site_ref(77, slug="other-co", name="Other Co")
    async with make_ninja_client(limits=fast_limits, sites=[here, there], targets=[here]) as c:
        assert await c.list_activities(
            activity_type="condition", status="reset", after=START, before=END
        ) == ([], False)
        assert await c.activity_log_reaches(START) is False

    assert [call.request.url.params["df"] for call in route.calls] == ["org=42", "org=42"]


@respx.mock
async def test_pages_run_by_older_than_until_a_short_page(fast_limits: ConnectorLimits) -> None:
    mock_world()
    day = dt.datetime(2026, 8, 10, 12, tzinfo=dt.UTC)
    first = [reset_row(100 - n, day) for n in range(10)]  # a full page: ids 100 down to 91
    second = [reset_row(90, day), reset_row(89, day)]
    route = respx.get(ACTIVITIES_URL).mock(side_effect=[log(*first), log(*second)])
    async with make_ninja_client(limits=fast_limits) as client:
        placed, truncated = await client.list_activities(
            activity_type="condition", status="reset", after=START, before=END, page_size=10
        )

    assert len(placed) == 12 and not truncated
    assert "olderThan" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["olderThan"] == "91"  # the lowest id seen


@respx.mock
async def test_the_page_cap_is_reported_not_hidden(fast_limits: ConnectorLimits) -> None:
    mock_world()
    day = dt.datetime(2026, 8, 10, 12, tzinfo=dt.UTC)
    pages = iter(range(10_000, 0, -10))

    def full_page(_: httpx.Request) -> httpx.Response:
        top = next(pages)
        return log(*[reset_row(top - n, day) for n in range(10)])

    respx.get(ACTIVITIES_URL).mock(side_effect=full_page)
    async with make_ninja_client(limits=fast_limits) as client:
        _, truncated = await client.list_activities(
            activity_type="condition", status="reset", after=START, before=END, page_size=10
        )

    assert truncated


@respx.mock
async def test_an_unknown_filter_value_is_never_sent(fast_limits: ConnectorLimits) -> None:
    mock_world()
    route = respx.get(ACTIVITIES_URL).mock(return_value=log())
    async with make_ninja_client(limits=fast_limits) as client:
        with pytest.raises(ValueError, match="Nothing was sent"):  # the vendor answers 500
            await client.list_activities(
                activity_type="condition", status="resolved", after=START, before=END
            )
    assert route.call_count == 0


@respx.mock
async def test_a_refusal_is_an_error_and_a_bad_envelope_too(fast_limits: ConnectorLimits) -> None:
    mock_world()
    respx.get(ACTIVITIES_URL).mock(
        side_effect=[
            httpx.Response(500, json=load_fixture("ninjaone", "error_500.json")),
            httpx.Response(200, json={"unexpected": True}),
        ]
    )
    async with make_ninja_client(limits=replace(fast_limits, max_retries=0)) as c:
        with pytest.raises(UpstreamError):
            await c.activity_log_reaches(START)
        with pytest.raises(UpstreamError):
            await c.activity_log_reaches(START)


# --- the collector: a figure only when the log covers the period ------------------------------


def collector() -> Collector:
    return Collector(
        client_id=str(uuid.uuid4()),
        site_id=str(uuid.uuid4()),
        period=AUGUST,
        config=FranchiseeConfig(display_name="Example IT Partners"),
        audit=NullAudit(),
    )


@respx.mock
async def test_alerts_resolved_are_counted_inside_the_period(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    mock_world()
    fixture = load_fixture("ninjaone", "activities_condition_reset.json")

    def answer(request: httpx.Request) -> httpx.Response:
        if "type" not in request.url.params:  # the coverage question: anything before 1 August?
            assert request.url.params["before"] == "2026-08-01"
            return log(reset_row(5, dt.datetime(2026, 7, 30, tzinfo=dt.UTC)))
        return httpx.Response(200, json=fixture)

    respx.get(ACTIVITIES_URL).mock(side_effect=answer)
    stub_factory(make_ninja_client(limits=fast_limits))
    run = collector()

    await run._ninja_alerts_resolved()

    resolved = run.facts.facts["ninjaone.alerts.resolved"]
    assert resolved.value == 3 and resolved.period_bound  # the 1 September row is outside
    assert "CONDITION" in resolved.definition and "RESET" in resolved.definition
    assert run.facts.facts["ninjaone.alerts.resolved_devices"].value == 2
    assert not run.facts.gaps
    assert len(run.audit.rows) == 2  # type: ignore[attr-defined]  # both reads are audited


@respx.mock
async def test_a_log_that_no_longer_covers_the_period_gives_a_gap_not_a_count(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    mock_world()
    route = respx.get(ACTIVITIES_URL).mock(return_value=log())  # nothing before 1 August
    stub_factory(make_ninja_client(limits=fast_limits))
    run = collector()

    await run._ninja_alerts_resolved()

    assert "ninjaone.alerts.resolved" not in run.facts.facts  # not zero: absent
    assert [gap.what for gap in run.facts.gaps] == [ALERTS_RESOLVED]
    assert "no longer covers the whole period" in run.facts.gaps[0].why
    assert route.call_count == 1  # the count was never asked for


@respx.mock
async def test_a_vendor_failure_is_a_gap_through_the_collectors_guard(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    mock_world()
    respx.get(ACTIVITIES_URL).mock(
        return_value=httpx.Response(500, json=load_fixture("ninjaone", "error_500.json"))
    )
    stub_factory(make_ninja_client(limits=replace(fast_limits, max_retries=0)))
    run = collector()

    await run._guarded(ALERTS_RESOLVED, "ninjaone", run._ninja_alerts_resolved)

    assert "ninjaone.alerts.resolved" not in run.facts.facts
    assert run.facts.gaps[0].what == ALERTS_RESOLVED and "500" in run.facts.gaps[0].why

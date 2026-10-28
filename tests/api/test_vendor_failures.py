"""A vendor that times out is not assessed for this run, on every path.

The board, the resolution and the review each run the same join; when a
fetch fails or times out, none of them may turn the silence into a gap: no
device is `absent`, no row is `unmanaged`, no identity link is made, moved or
deactivated, and a lost device whose last-seen could not be read is neither
lost nor alive. Timeouts are simulated at the HTTP layer (`httpx.ReadTimeout`
on the route), so the connectors' retry and error mapping run for real.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.coverage.compute as compute_module
from app.coverage.compute import compute_coverage
from app.devices.agreement import compare
from app.devices.resolve import resolve_devices
from app.models.device import Device, DeviceVendorLink
from app.reports.tbr.security import (
    GROUP_BD_LAST_SEEN_FAILED,
    collect_bitdefender_endpoints,
    derive_coverage,
)
from app.settings import get_settings
from tests.api.test_devices import (
    SITE_ID,
    world,  # noqa: F401  the fixture
)
from tests.connectors.conftest import NINJA_BASE_URL
from tests.connectors.test_bitdefender import NETWORK, NETWORK_V1
from tests.reports.test_tbr_security import (
    collector,
    mock_bitdefender,
    mock_licence,
    mock_ninja,
    two_connectors,  # noqa: F401  the fixture
)

pytestmark = pytest.mark.usefixtures("clean")

TOO_SLOW = httpx.ReadTimeout("too slow")


def forget_caches() -> None:
    """The board's per-client caches emptied: a new day, so a timeout is met, not
    served from yesterday's answer."""
    compute_module._MACS.clear()
    compute_module._SERIALS.clear()


@pytest.fixture(autouse=True)
def cold_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caches empty, and a last-seen kept on a link counts as stale at once, so a
    second day asks the vendor again rather than reading yesterday's date."""
    forget_caches()
    monkeypatch.setenv("BITDEFENDER_LAST_SEEN_REFRESH_HOURS", "0")
    get_settings.cache_clear()


def timeout_bitdefender_listing() -> None:
    """GravityZone's network service (listing and details) times out; the rest answers."""
    mock_bitdefender()
    respx.post(NETWORK).mock(side_effect=TOO_SLOW)
    respx.post(NETWORK_V1).mock(side_effect=TOO_SLOW)


async def _links(
    session_maker: async_sessionmaker[AsyncSession],
) -> list[tuple[str, str, str, bool]]:
    async with session_maker() as session:
        rows = (await session.execute(select(DeviceVendorLink))).scalars().all()
    return sorted((r.connector_name.value, r.external_id, r.matched_by, r.active) for r in rows)


async def _statuses(session_maker: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    async with session_maker() as session:
        rows = (await session.execute(select(Device))).scalars().all()
    return {d.canonical_name: d.status for d in rows}


# --- the board ------------------------------------------------------------------------


@respx.mock
async def test_a_tool_that_times_out_is_not_assessed_on_the_board_never_absent_or_unmanaged(
    world: uuid.UUID,  # noqa: F811
    two_connectors: Any,  # noqa: F811
) -> None:
    mock_ninja()
    mock_licence()
    timeout_bitdefender_listing()
    for through in (False, True):
        report = await compute_coverage(world, exclusions=[], through_identity=through)
        tool = report.tool("bitdefender")
        assert tool.assessed is False and "did not respond" in tool.reason
        assert {d.tools["bitdefender"].state for d in report.devices} == {"not_assessed"}
        assert [u for u in report.unmanaged if u.tool == "bitdefender"] == []
        assert [u for u in report.external if u.tool == "bitdefender"] == []
        assert report.rmm_agent_down == 0


@respx.mock
async def test_a_ninjaone_key_query_that_times_out_leaves_every_tool_not_assessed(
    world: uuid.UUID,  # noqa: F811
    two_connectors: Any,  # noqa: F811
) -> None:
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    respx.get(f"{NINJA_BASE_URL}/v2/queries/network-interfaces").mock(side_effect=TOO_SLOW)
    for through in (False, True):
        report = await compute_coverage(world, exclusions=[], through_identity=through)
        # The population is still listed; nothing is judged against it this run.
        assert len(report.devices) == 5
        tool = report.tool("bitdefender")
        assert tool.assessed is False
        assert tool.reason.startswith("not assessed this run: NinjaOne did not answer")
        assert {s.state for d in report.devices for s in d.tools.values()} == {"not_assessed"}
        assert report.unmanaged == [] and report.external == [] and report.discovered == []


@respx.mock
async def test_a_last_seen_read_that_times_out_leaves_the_lost_device_undecided(
    world: uuid.UUID,  # noqa: F811
    two_connectors: Any,  # noqa: F811
) -> None:
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    # Yesterday the links were written with the vendor answering.
    await resolve_devices(world, actor="tester")
    forget_caches()
    # Today the listing answers (v1.1) and the per-endpoint detail read (v1.0) times out.
    respx.post(NETWORK_V1).mock(side_effect=TOO_SLOW)
    for through in (False, True):
        report = await compute_coverage(world, exclusions=[], through_identity=through)
        assert report.tool("bitdefender").assessed is True
        lost = next(d for d in report.devices if d.device.name == "TL-AUS-MAC03")
        verdict = lost.tools["bitdefender"]
        assert verdict.state == "not_assessed"
        assert "did not answer for the row's last-seen" in verdict.detail
        # The pairing stands and the row is claimed: it is not an unmanaged endpoint.
        assert verdict.matched_by == "exact" and verdict.endpoint_ids
        assert [u.name for u in report.unmanaged if u.tool == "bitdefender"] == ["TL-AUS-EXCH"]
        assert report.rmm_agent_down == 0 and report.rmm_stale == 1
        # Every other device is judged as usual.
        assert {d.tools["bitdefender"].state for d in report.devices if d is not lost} == {
            "covered",
            "stale",
            "absent",
        }


# --- the resolution ---------------------------------------------------------------------


@respx.mock
async def test_the_resolution_leaves_links_untouched_when_a_tool_times_out(
    world: uuid.UUID,  # noqa: F811
    two_connectors: Any,  # noqa: F811
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    first = await resolve_devices(world, actor="tester")
    assert first.tools_read["bitdefender"] == "read" and first.rmm_agent_down == 1
    before_links, before_status = await _links(session_maker), await _statuses(session_maker)

    # Day two: GravityZone does not answer. Nothing of its is made, moved or dropped.
    _ninja, bitdefender = two_connectors
    bitdefender.inventory.invalidate()
    forget_caches()
    respx.post(NETWORK).mock(side_effect=TOO_SLOW)
    respx.post(NETWORK_V1).mock(side_effect=TOO_SLOW)
    second = await resolve_devices(world, actor="tester")
    assert second.tools_read["bitdefender"].startswith("did not answer")
    assert second.devices_created == 0
    assert dict(second.links_created) == {}
    assert second.links_moved == 0 and second.links_deactivated == 0
    assert await _links(session_maker) == before_links
    # A lost device keeps the status it had rather than being re-judged blind.
    assert await _statuses(session_maker) == before_status
    assert second.rmm_agent_down == 1 and second.rmm_stale == 0


@respx.mock
async def test_the_resolution_keeps_a_lost_devices_status_when_the_last_seen_read_times_out(
    world: uuid.UUID,  # noqa: F811
    two_connectors: Any,  # noqa: F811
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    first = await resolve_devices(world, actor="tester")
    assert first.rmm_agent_down == 1
    before_links, before_status = await _links(session_maker), await _statuses(session_maker)
    forget_caches()
    respx.post(NETWORK_V1).mock(side_effect=TOO_SLOW)
    second = await resolve_devices(world, actor="tester")
    assert second.undated == ["bitdefender"]
    assert second.tools_read["bitdefender"] == "read"
    assert dict(second.links_created) == {} and second.links_moved == 0
    assert await _links(session_maker) == before_links
    assert await _statuses(session_maker) == before_status
    # And the board, flag off and on, says the same as each other about it.
    off = await compute_coverage(world, exclusions=[], through_identity=False)
    on = await compute_coverage(world, exclusions=[], through_identity=True)
    assert compare(off, on) == []
    assert [s.as_dict() for s in off.sites] == [s.as_dict() for s in on.sites]


@respx.mock
async def test_the_resolution_runs_no_passes_when_a_ninjaone_key_query_times_out(
    world: uuid.UUID,  # noqa: F811
    two_connectors: Any,  # noqa: F811
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    first = await resolve_devices(world, actor="tester")
    before_links, before_status = await _links(session_maker), await _statuses(session_maker)
    forget_caches()
    respx.get(f"{NINJA_BASE_URL}/v2/queries/computer-systems").mock(side_effect=TOO_SLOW)
    second = await resolve_devices(world, actor="tester")
    assert second.incomplete is not None and "MACs or serials" in second.incomplete
    assert second.tools_read["bitdefender"].startswith("not run:")
    assert dict(second.links_created) == {} and second.links_moved == 0
    assert second.links_deactivated == 0 and second.devices_created == 0
    assert second.links_seen == first.links_created["ninjaone"]  # NinjaOne's own records
    assert await _links(session_maker) == before_links
    assert await _statuses(session_maker) == before_status


# --- the review -----------------------------------------------------------------------


@respx.mock
async def test_the_review_records_a_gap_and_keeps_its_facts_when_the_last_seen_read_times_out(
    last_seen_in_memory: dict[str, Any],
    two_connectors: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COVERAGE_RMM_STALE_DAYS", "365")
    get_settings.cache_clear()
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    respx.post(NETWORK_V1).mock(side_effect=TOO_SLOW)
    run = collector()
    run.site_id = str(SITE_ID)
    await run._ninja_inventory()
    await collect_bitdefender_endpoints(run)
    derive_coverage(run)
    assert run.facts.groups.get(GROUP_BD_LAST_SEEN_FAILED) == ["yes"]
    assert any(g.what == "Bitdefender endpoint last-seen" for g in run.facts.gaps)
    # The endpoints' facts survived the failed read; the lost device is undecided.
    assert run.facts.facts["bitdefender.endpoints.total"].value == 6
    assert run.facts.facts["coverage.matched"].value == 3
    assert run.facts.facts["coverage.ninjaone_only"].value == 1
    assert run.facts.facts["coverage.rmm_agent_down"].value == 0
    assert run.facts.facts["coverage.bitdefender_only"].value == 1


@respx.mock
async def test_the_review_records_a_gap_and_derives_nothing_when_the_listing_times_out(
    last_seen_in_memory: dict[str, Any],
    two_connectors: Any,  # noqa: F811
) -> None:
    mock_ninja()
    mock_licence()
    timeout_bitdefender_listing()
    run = collector()
    await run._ninja_inventory()
    await run._guarded(
        "Bitdefender endpoint protection", "bitdefender", lambda: collect_bitdefender_endpoints(run)
    )
    derive_coverage(run)
    assert any(g.source == "bitdefender" for g in run.facts.gaps)
    assert "coverage.matched" not in run.facts.facts
    assert "coverage.ninjaone_only" not in run.facts.facts

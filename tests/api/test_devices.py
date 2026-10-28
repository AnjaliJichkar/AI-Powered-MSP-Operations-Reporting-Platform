"""The device identity layer against the real database and the recorded vendor answers.

Resolution is idempotent; the board and the review's coverage facts are the
same whether they read the join or the stored links; a link and an unlink
need a reason and leave an audit row; an unlink queues the record again.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.coverage.compute as compute_module
import app.coverage.exclusions as exclusions_module
import app.coverage.snapshots as snapshots_module
import app.devices.lastseen as lastseen_module
import app.devices.lookup as lookup_module
import app.devices.resolve as resolve_module
import app.reports.tbr.collect as collect_module
from app.coverage.compute import compute_coverage
from app.devices.admin import DeviceAdminError, Operator, link, unlink
from app.devices.agreement import compare, latest_agreement, run_agreement
from app.devices.resolve import resolve_devices
from app.models import AuditEventType, Client
from app.models.device import Device, DeviceUnresolved, DeviceVendorLink
from app.models.enums import ConnectorName
from app.models.site import Site
from app.reports.tbr.security import collect_bitdefender_endpoints, derive_coverage
from app.settings import get_settings
from tests.api.conftest import audit_rows
from tests.connectors.conftest import (
    NINJA_BASE_URL,
    TEST_CLIENT_ID,
    TEST_CLIENT_SLUG,
    load_fixture,
    single_page,
)
from tests.connectors.test_bitdefender import NETWORK_V1, answer_network
from tests.reports.test_tbr_security import (
    collector,
    mock_bitdefender,
    mock_licence,
    mock_ninja,
    two_connectors,  # noqa: F401  the fixture
)

pytestmark = pytest.mark.usefixtures("clean")

SITE_ID = uuid.uuid5(TEST_CLIENT_ID, "acme-dental")


@pytest.fixture
async def world(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> uuid.UUID:
    """The client and site the recorded answers belong to, and every module's
    `session_scope` pointed at the test database."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    for module in (
        resolve_module,
        compute_module,
        exclusions_module,
        lookup_module,
        collect_module,
        lastseen_module,
        snapshots_module,
    ):
        monkeypatch.setattr(module, "session_scope", scope)
    monkeypatch.setenv("COVERAGE_RMM_STALE_DAYS", "365")
    get_settings.cache_clear()
    async with session_maker() as session:
        session.add(Client(id=TEST_CLIENT_ID, slug=TEST_CLIENT_SLUG, name="TeamLogic Austin"))
        await session.flush()
        session.add(
            Site(id=SITE_ID, client_id=TEST_CLIENT_ID, slug="acme-dental", name="Acme Dental")
        )
        await session.commit()
    return TEST_CLIENT_ID


async def _links(session_maker: async_sessionmaker[AsyncSession]) -> list[DeviceVendorLink]:
    async with session_maker() as session:
        return list((await session.execute(select(DeviceVendorLink))).scalars().all())


@respx.mock
async def test_resolution_creates_devices_once_and_is_idempotent(
    world: uuid.UUID,
    two_connectors: Any,  # noqa: F811
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    first = await resolve_devices(world, actor="tester")
    # Five NinjaOne devices, each a device with a ninjaone link; five pair with
    # a GravityZone row by exact name (WS07's row has no agent but is the
    # machine). MAC03 has never contacted NinjaOne and takes part in the passes
    # all the same, as it does on the board: its row is a current agent, so the
    # machine is alive with its RMM agent down, not a stale device beside a
    # vendor-only one. TL-AUS-EXCH has no NinjaOne device and becomes one.
    assert first.devices_created == 6
    assert first.links_created["ninjaone"] == 5
    assert first.links_created["exact"] == 5
    assert first.links_created["vendor_only"] == 1
    assert first.rmm_stale == 0 and first.rmm_agent_down == 1
    assert first.unresolved_open == 0
    assert first.tools_read["bitdefender"] == "read"
    assert first.tools_read["connectsecure"].startswith("not configured")

    second = await resolve_devices(world, actor="tester")
    assert second.devices_created == 0
    assert dict(second.links_created) == {}
    assert (first.links_seen, second.links_seen) == (0, 11)  # seen: links that already existed
    assert second.links_moved == 0 and second.links_deactivated == 0
    links = await _links(session_maker)
    assert len(links) == 11 and all(link.active for link in links)
    async with session_maker() as session:
        statuses = sorted(d.status for d in (await session.execute(select(Device))).scalars())
    assert statuses == ["active"] * 5 + ["rmm_agent_down"]
    rows = await audit_rows(session_maker, event_type=AuditEventType.DEVICES_RESOLVED)
    assert len(rows) == 2 and rows[0].payload["devices_created"] == 6


@respx.mock
async def test_a_vendor_only_link_is_claimed_when_the_machine_comes_back(
    world: uuid.UUID,
    two_connectors: Any,  # noqa: F811
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """NinjaOne does not list MAC03 at first, so its GravityZone row is a vendor-only
    device. The day NinjaOne returns it, that device becomes the managed one (the
    NinjaOne record adopts the device of its name) and the exact pass claims the
    link: the same row, now a pairing, nothing deleted and no second device."""
    mock_ninja()
    devices = load_fixture("ninjaone", "devices.json")
    respx.get(f"{NINJA_BASE_URL}/v2/devices").mock(
        side_effect=single_page([row for row in devices if row["id"] != 1004])
    )
    mock_bitdefender()
    mock_licence()
    first = await resolve_devices(world, actor="tester")
    assert first.devices_created == 6 and first.links_created["ninjaone"] == 4
    assert first.links_created["vendor_only"] == 2 and first.rmm_stale == 0

    for row in devices:
        if row["id"] == 1004:
            row["lastContact"] = dt.datetime.now(dt.UTC).timestamp()
    respx.get(f"{NINJA_BASE_URL}/v2/devices").mock(side_effect=single_page(devices))
    ninja, _ = two_connectors
    ninja.inventory.invalidate()
    second = await resolve_devices(world, actor="tester")
    assert second.devices_created == 0
    assert dict(second.links_created) == {"ninjaone": 1}
    assert second.links_moved == 1 and second.rmm_stale == 0
    async with session_maker() as session:
        moved = (
            await session.execute(
                select(DeviceVendorLink).where(
                    DeviceVendorLink.connector_name == ConnectorName.BITDEFENDER,
                    DeviceVendorLink.matched_by == "exact",
                    DeviceVendorLink.note.is_not(None),
                )
            )
        ).scalar_one()
        managed = await session.get(Device, moved.device_id)
        assert managed is not None and managed.status == "active"
        ninja_links = [
            row
            for row in (
                await session.execute(
                    select(DeviceVendorLink).where(DeviceVendorLink.device_id == managed.id)
                )
            ).scalars()
            if row.connector_name == ConnectorName.NINJAONE
        ]
        assert [row.external_id for row in ninja_links] == ["1004"]
        # One device for the machine, not one per vendor, and nothing deleted.
        same_name = [
            d
            for d in (await session.execute(select(Device))).scalars()
            if d.netbios_key == managed.netbios_key
        ]
        assert [d.id for d in same_name] == [managed.id]
        assert len((await session.execute(select(Device))).scalars().all()) == 6
    # And a third run changes nothing again.
    third = await resolve_devices(world, actor="tester")
    assert (third.links_moved, third.devices_created, dict(third.links_created)) == (0, 0, {})


@respx.mock
async def test_the_board_and_the_review_agree_with_the_flag_off_and_on(
    world: uuid.UUID,
    two_connectors: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    await resolve_devices(world, actor="tester")

    def site_counts(report: Any) -> list[dict[str, Any]]:
        return [s.as_dict() for s in report.sites]

    off = await compute_coverage(world, exclusions=[], through_identity=False)
    on = await compute_coverage(world, exclusions=[], through_identity=True)
    assert site_counts(off) == site_counts(on)
    assert compare(off, on) == []
    # The daily step keeps the same answer as a row, and reads it back last-first.
    async with session_maker() as session:
        stored = await run_agreement(session, world, exclusions=[], actor="tester")
        await session.commit()
    async with session_maker() as session:
        latest = await latest_agreement(session, world)
        assert latest is not None and latest.id == stored.id
        assert latest.agree is True and latest.differences == []
        assert latest.sites_compared == 1 and latest.summary["unmanaged"] == [1, 1]
    assert len(off.unmanaged) == len(on.unmanaged) == 1
    # The lost device is alive per Bitdefender on both paths, and its row is claimed.
    assert off.rmm_agent_down == on.rmm_agent_down == 1
    assert {
        d.tools["bitdefender"].state for d in on.devices if d.device.name == "TL-AUS-MAC03"
    } == {"rmm_agent_down"}
    assert sorted(u.name for u in off.unmanaged) == sorted(u.name for u in on.unmanaged)
    # Every pairing on the identity side names its pass, as the join's does.
    assert {
        d.tools["bitdefender"].matched_by
        for d in on.devices
        if d.tools["bitdefender"].state in ("covered", "stale", "absent")
        and d.tools["bitdefender"].matched_by
    } == {"exact"}

    async def review_facts() -> dict[str, Any]:
        run = collector()
        run.client_id = str(world)
        run.site_id = str(SITE_ID)
        await run._ninja_inventory()
        await collect_bitdefender_endpoints(run)
        await run._load_identity()
        derive_coverage(run)
        return {
            key: run.facts.facts[key].value
            for key in ("coverage.matched", "coverage.ninjaone_only", "coverage.bitdefender_only")
        } | {"identity": bool(run.facts.groups.get("identity.loaded"))}

    flag_off = await review_facts()
    monkeypatch.setenv("DEVICE_IDENTITY_ENABLED", "true")
    get_settings.cache_clear()
    flag_on = await review_facts()
    assert flag_off["identity"] is False and flag_on["identity"] is True
    assert {k: v for k, v in flag_off.items() if k != "identity"} == {
        k: v for k, v in flag_on.items() if k != "identity"
    }
    assert flag_on["coverage.matched"] == 3 and flag_on["coverage.ninjaone_only"] == 1


@respx.mock
async def test_link_and_unlink_need_a_reason_and_are_audited(
    world: uuid.UUID,
    two_connectors: Any,  # noqa: F811
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    await resolve_devices(world, actor="tester")
    operator = Operator(actor_id="tester", client_id=world, client_slug=TEST_CLIENT_SLUG)
    async with session_maker() as session:
        row = (
            (
                await session.execute(
                    select(DeviceVendorLink).where(
                        DeviceVendorLink.connector_name == ConnectorName.BITDEFENDER,
                        DeviceVendorLink.matched_by == "exact",
                    )
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        link_id, device_id, external_id = row.id, row.device_id, row.external_id
        with pytest.raises(DeviceAdminError, match="reason is required"):
            await unlink(session, operator, link_id=link_id, reason="   ")
        await unlink(session, operator, link_id=link_id, reason="wrong machine: serial differs")
        await session.commit()
    async with session_maker() as session:
        withdrawn = await session.get(DeviceVendorLink, link_id)
        assert withdrawn is not None and not withdrawn.active
        assert withdrawn.note == "wrong machine: serial differs"
        queued = (
            (
                await session.execute(
                    select(DeviceUnresolved).where(DeviceUnresolved.resolved_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        assert [q.external_id for q in queued] == [external_id]
        assert queued[0].found_by == "unlinked"
        # Linking it back by hand resolves the queue and marks the link manual.
        with pytest.raises(DeviceAdminError, match="reason is required"):
            await link(
                session,
                operator,
                device_id=device_id,
                connector=ConnectorName.BITDEFENDER,
                external_id=external_id,
                reason="",
            )
        relinked = await link(
            session,
            operator,
            device_id=device_id,
            connector=ConnectorName.BITDEFENDER,
            external_id=external_id,
            reason="checked the serial in the console",
        )
        await session.commit()
        assert relinked.id == link_id and relinked.active and relinked.matched_by == "manual"
    async with session_maker() as session:
        assert (
            await session.execute(
                select(DeviceUnresolved).where(DeviceUnresolved.resolved_at.is_(None))
            )
        ).scalars().all() == []
    rows = await audit_rows(session_maker, event_type=AuditEventType.DEVICE_LINKED)
    assert [r.payload["action"] for r in rows] == ["unlinked", "linked"]
    assert rows[0].payload["reason"] == "wrong machine: serial differs"
    # A rerun leaves a manual link alone.
    again = await resolve_devices(world, actor="tester", now=dt.datetime.now(dt.UTC))
    assert again.links_deactivated == 0
    async with session_maker() as session:
        kept = await session.get(DeviceVendorLink, link_id)
        assert kept is not None and kept.matched_by == "manual" and kept.active
        assert (await session.execute(select(Device))).scalars().all()


@respx.mock
async def test_a_last_seen_is_kept_on_the_link_and_not_asked_again_within_the_window(
    world: uuid.UUID,
    two_connectors: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """MAC03 is lost and its GravityZone row has no scan date: the resolution reads
    its lastSeen once, keeps it on the link, and the board that follows reads it
    from there; only a window of zero asks the vendor again."""
    mock_ninja()
    mock_bitdefender()
    mock_licence()
    details = respx.post(NETWORK_V1).mock(side_effect=answer_network)
    first = await resolve_devices(world, actor="tester")
    assert first.rmm_agent_down == 1
    assert first.last_seen_reads["fetched"] == 1 and first.last_seen_reads["from_links"] == 0
    assert details.call_count == 1
    async with session_maker() as session:
        link = (
            await session.execute(
                select(DeviceVendorLink).where(
                    DeviceVendorLink.connector_name == ConnectorName.BITDEFENDER,
                    DeviceVendorLink.external_id == "64e000000000000000000006",
                )
            )
        ).scalar_one()
        assert link.vendor_last_seen_at is not None and link.vendor_checked_at is not None
        others = (
            (
                await session.execute(
                    select(DeviceVendorLink).where(DeviceVendorLink.vendor_checked_at.is_not(None))
                )
            )
            .scalars()
            .all()
        )
        assert [o.external_id for o in others] == ["64e000000000000000000006"]
    # The board, both ways, reads the date from the link: no detail call.
    for through in (False, True):
        report = await compute_coverage(world, exclusions=[], through_identity=through)
        assert report.rmm_agent_down == 1
        assert report.reads["from_links"] == 1 and report.reads["fetched"] == 0
    assert details.call_count == 1
    # A window of zero asks again.
    monkeypatch.setenv("BITDEFENDER_LAST_SEEN_REFRESH_HOURS", "0")
    get_settings.cache_clear()
    report = await compute_coverage(world, exclusions=[], through_identity=False)
    assert report.reads["fetched"] == 1 and details.call_count == 2

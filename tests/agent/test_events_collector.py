"""The daily collector: idempotent on the vendor event id, coverage recorded per window.

Against a real database, because idempotency is a unique constraint and an
upsert. The vendor read is replaced by a reader that returns scripted events.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.events.collector as collector
from app.connectors.errors import AuthError
from app.models import Client, CollectedEvent, CollectionCoverage, ConnectorName, Site, ToolMapping
from app.models.enums import AuditEventType
from tests.agent.conftest import LIVE_URL, audit_rows

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set"),
]

UTC = dt.UTC
NOW = dt.datetime(2026, 9, 22, 6, 0, tzinfo=UTC)


@pytest.fixture
def scoped(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(collector, "session_scope", scope)


async def add_site(
    maker: async_sessionmaker[AsyncSession], client: Client, slug: str, *, mapped: bool = True
) -> Site:
    async with maker() as session:
        site = Site(client_id=client.id, slug=slug, name=slug.title())
        session.add(site)
        await session.flush()
        if mapped:
            session.add(
                ToolMapping(
                    client_id=client.id,
                    site_id=site.id,
                    connector_name=ConnectorName.BITDEFENDER,
                    external_id=f"64a{slug[:3]}00000000000000000b1",
                )
            )
        await session.commit()
        await session.refresh(site)
        return site


def event(
    external_id: str, *, status: str = "open", when: dt.datetime = NOW
) -> collector.VendorEvent:
    return collector.VendorEvent(
        external_id=external_id,
        occurred_at=when - dt.timedelta(hours=3),
        vendor_updated_at=when,
        payload={"status": status, "detection_name": "Trojan.Example", "endpoint_name": "WS-01"},
    )


def scripted(by_site: dict[str, list[collector.VendorEvent] | Exception]) -> Any:
    async def read(client_id: Any, site: Site, since: dt.datetime, until: dt.datetime) -> Any:
        answer = by_site[site.slug]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return read


async def test_a_second_run_updates_rather_than_duplicates_and_records_coverage(
    scoped: None,
    db_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    engine: Any,
) -> None:
    acme = await add_site(session_maker, db_client, "acme-dental")
    await add_site(session_maker, db_client, "beacon-legal")
    await add_site(session_maker, db_client, "unmapped-co", mapped=False)

    first = await collector.collect(
        db_client.id,
        actor="collector",
        now=NOW,
        reader=scripted(
            {
                "acme-dental": [event("inc-1"), event("inc-2")],
                "beacon-legal": AuthError("key refused", connector="bitdefender"),
            }
        ),
    )

    assert [(o.site_slug, o.status, o.seen, o.new) for o in first.outcomes] == [
        ("acme-dental", "collected", 2, 2),
        ("beacon-legal", "failed", 0, 0),
        ("unmapped-co", "skipped", 0, 0),
    ]
    assert "AuthError" in first.failed[0].detail
    assert first.window_start == NOW - dt.timedelta(days=2)  # a day asked for, a day of overlap

    later = NOW + dt.timedelta(days=1)
    second = await collector.collect(
        db_client.id,
        actor="collector",
        now=later,
        reader=scripted(
            {
                "acme-dental": [
                    event("inc-2", status="closed", when=later),
                    event("inc-3", when=later),
                ],
                "beacon-legal": [event("inc-9", when=later)],
            }
        ),
    )

    assert [(o.site_slug, o.status, o.seen, o.new) for o in second.outcomes[:2]] == [
        ("acme-dental", "collected", 2, 1),  # inc-2 seen again: updated, not new
        ("beacon-legal", "collected", 1, 1),
    ]
    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(CollectedEvent)
                    .where(CollectedEvent.client_id == db_client.id)
                    .order_by(CollectedEvent.external_id)
                )
            )
            .scalars()
            .all()
        )
        assert [(r.external_id, r.payload["status"], r.site_id == acme.id) for r in rows] == [
            ("inc-1", "open", True),
            ("inc-2", "closed", True),  # the vendor's change came through
            ("inc-3", "open", True),
            ("inc-9", "open", False),
        ]
        updated = next(r for r in rows if r.external_id == "inc-2")
        assert updated.first_collected_at == NOW and updated.last_collected_at == later
        coverage = (
            (
                await session.execute(
                    select(CollectionCoverage)
                    .where(CollectionCoverage.client_id == db_client.id)
                    .order_by(CollectionCoverage.id)
                )
            )
            .scalars()
            .all()
        )
    assert [(c.status, c.events_seen, c.events_new, c.error is not None) for c in coverage] == [
        ("collected", 2, 2, False),
        ("failed", 0, 0, True),
        ("collected", 2, 1, False),
        ("collected", 1, 1, False),
    ]
    assert {c.collected_by for c in coverage} == {"collector"}
    assert all(c.site_id != acme.id or c.kind == "detection" for c in coverage)

    rows_audit = await audit_rows(engine, db_client.id)
    collected = [r for r in rows_audit if r.event_type is AuditEventType.EVENTS_COLLECTED]
    assert [(r.outcome.value, r.payload["site"]) for r in collected] == [
        ("success", "acme-dental"),
        ("error", "beacon-legal"),
        ("success", "acme-dental"),
        ("success", "beacon-legal"),
    ]


async def test_coverage_tells_a_collected_period_from_an_uncollected_one(
    scoped: None, db_client: Client, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    acme = await add_site(session_maker, db_client, "acme-dental")
    day = dt.timedelta(days=1)
    for offset in (0, 1, 2, 4):  # day 3 was never collected; day 5 failed
        await collector.collect(
            db_client.id, actor="c", now=NOW + day * offset, reader=scripted({"acme-dental": []})
        )
    await collector.collect(
        db_client.id,
        actor="c",
        now=NOW + day * 5,
        reader=scripted({"acme-dental": AuthError("down", connector="bitdefender")}),
    )

    async with session_maker() as session:
        whole, gaps = await collector.coverage_for(
            session,
            client_id=db_client.id,
            site_id=acme.id,
            connector=ConnectorName.BITDEFENDER,
            kind="detection",
            start=NOW - day * 2,
            end=NOW + day * 5,
        )
        covered, no_gaps = await collector.coverage_for(
            session,
            client_id=db_client.id,
            site_id=acme.id,
            connector=ConnectorName.BITDEFENDER,
            kind="detection",
            start=NOW - day,
            end=NOW + day * 2,
        )

    assert covered and no_gaps == []
    # Each run covers [now - 2 days, now]: the day-3 miss leaves no hole (day 4's run reaches
    # back two days) but the failed day-5 run leaves [day 4, day 5) uncovered.
    assert not whole and gaps == [(NOW + day * 4, NOW + day * 5)]


async def test_an_unknown_reader_or_window_is_refused(scoped: None, db_client: Client) -> None:
    with pytest.raises(ValueError, match="no reader"):
        await collector.collect(db_client.id, actor="c", connector=ConnectorName.COVE)
    with pytest.raises(ValueError, match="at least 1"):
        await collector.collect(db_client.id, actor="c", days=0)


async def test_a_site_without_the_licence_is_not_applicable_never_covered_with_zero(
    scoped: None, db_client: Client, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    acme = await add_site(session_maker, db_client, "acme-dental")

    result = await collector.collect(
        db_client.id,
        actor="c",
        now=NOW,
        reader=scripted({"acme-dental": collector.NotApplicable("EDR is not licensed")}),
    )

    outcome = result.outcomes[0]
    assert (outcome.status, outcome.seen, outcome.new) == ("not_applicable", 0, 0)
    assert result.not_applicable == [outcome] and result.failed == []
    assert result.as_dict()["not_applicable"] == 1
    async with session_maker() as session:
        row = (
            await session.execute(
                select(CollectionCoverage).where(CollectionCoverage.site_id == acme.id)
            )
        ).scalar_one()
        assert row.status == "not_applicable" and row.error == "EDR is not licensed"
        covered, gaps = await collector.coverage_for(
            session,
            client_id=db_client.id,
            site_id=acme.id,
            connector=ConnectorName.BITDEFENDER,
            kind="detection",
            start=NOW - dt.timedelta(days=1),
            end=NOW,
        )
    assert not covered and gaps == [(NOW - dt.timedelta(days=1), NOW)]  # not a quiet day

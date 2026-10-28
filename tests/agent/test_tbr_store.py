"""Stored reviews: scoped to their client, superseded rather than deleted, and the next
period's baseline. Against a real database, because the scoping is a query."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Client, Site
from app.reports.tbr import store
from app.reports.tbr.config import FranchiseeConfig, PriceBookEntry, load_config, save_config
from app.reports.tbr.model import ReviewPeriod
from tests.agent.conftest import LIVE_URL
from tests.reports.conftest import make_document, make_facts

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set"),
]


async def _site(maker: async_sessionmaker[AsyncSession], client: Client) -> Site:
    async with maker() as session:
        site = Site(client_id=client.id, slug="acme-legal", name="Acme Legal")
        session.add(site)
        await session.commit()
        await session.refresh(site)
        return site


async def _document(client: Client, site: Site, period: ReviewPeriod, **facts: Any) -> Any:
    document = await make_document(facts=make_facts(**facts))
    document.client_id, document.site_id, document.period = str(client.id), str(site.id), period
    return document


async def test_a_report_is_found_only_through_its_own_client(
    db_client: Client, make_client: Any, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    other = await make_client("another-franchisee", "Another Franchisee")
    site = await _site(session_maker, db_client)
    async with session_maker() as session:
        row = await store.save_new(
            session,
            await _document(db_client, site, ReviewPeriod.ending(2026, 8)),
            created_by="sam",
        )
        await session.commit()
        report_id = row.id

    async with session_maker() as session:
        found, document = await store.load(session, report_id, client_id=db_client.id)
        assert found.id == report_id and document.site_slug == "acme-legal"
        # Another client's id, a made-up id and a malformed id all look the same: not found.
        for wrong_client in (other.id, uuid.uuid4()):
            with pytest.raises(store.ReportNotFound):
                await store.load(session, report_id, client_id=wrong_client)
        with pytest.raises(store.ReportNotFound):
            await store.load(session, "not-a-uuid", client_id=db_client.id)
        assert await store.list_for_client(session, other.id) == []


async def test_regenerating_supersedes_and_last_month_is_the_baseline(
    db_client: Client, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    site = await _site(session_maker, db_client)
    july, august = ReviewPeriod.ending(2026, 7), ReviewPeriod.ending(2026, 8)
    async with session_maker() as session:
        first = await store.save_new(
            session,
            await _document(db_client, site, july, ninjaone__patches__failed=40),
            created_by="sam",
        )
        second = await store.save_new(
            session,
            await _document(db_client, site, july, ninjaone__patches__failed=31),
            created_by="sam",
        )
        await session.commit()
        first_id, second_id = first.id, second.id

    async with session_maker() as session:
        old, _ = await store.load(session, first_id, client_id=db_client.id)
        assert old.status == "superseded"  # kept, not deleted
        listed = await store.list_for_client(session, db_client.id)
        assert [row.id for row in listed] == [second_id]

        baseline = await store.prior_snapshot(session, site.id, august)
        assert baseline is not None
        facts, label = baseline
        assert label == "July 2026"
        assert facts.number("ninjaone.patches.failed") == 31  # the live one, not the superseded
        assert await store.prior_snapshot(session, site.id, july) is None  # nothing before July


async def test_a_franchisees_config_is_its_own_and_defaults_to_no_prices(
    db_client: Client, make_client: Any, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    other = await make_client("another-franchisee", "Another Franchisee")
    async with session_maker() as session:
        default = await load_config(session, db_client.id, client_name=db_client.name)
        assert default.display_name == db_client.name and default.price_book == {}
        await save_config(
            session,
            db_client.id,
            FranchiseeConfig(
                display_name="Example IT",
                review_period_months=3,
                price_book={"device_replacement": PriceBookEntry(cost=1450, per="device")},
            ),
            updated_by="sam",
        )
        await session.commit()

    async with session_maker() as session:
        mine = await load_config(session, db_client.id, client_name=db_client.name)
        assert mine.review_period_months == 3
        assert mine.quote("device_replacement", quantity=2)[1] == "$2,900"
        theirs = await load_config(session, other.id, client_name=other.name)
        assert theirs.price_book == {}  # never another client's prices

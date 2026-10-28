"""`generate_all`: every mapped, active site of one client, past failures, with a summary.

Against a real database, because which sites are drafted is a query: active, this
client's, mapped to NinjaOne or Autotask, and without a review for the period.
`generate` itself is replaced, so no vendor and no model is involved.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.reports.tbr.audit as audit_module
import app.reports.tbr.pipeline as pipeline
from app.connectors.errors import AuthError
from app.models import Client, ConnectorName, Site, ToolMapping
from app.models.enums import AuditEventType
from app.reports.tbr import store
from app.reports.tbr.model import FactSet, Gap
from tests.agent.conftest import LIVE_URL, audit_rows
from tests.reports.conftest import make_document

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set"),
]


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

    for module in (pipeline, audit_module):
        monkeypatch.setattr(module, "session_scope", scope)


async def add_site(
    maker: async_sessionmaker[AsyncSession],
    client: Client,
    slug: str,
    *,
    connectors: tuple[ConnectorName, ...] = (ConnectorName.NINJAONE,),
    active: bool = True,
) -> Site:
    async with maker() as session:
        site = Site(client_id=client.id, slug=slug, name=slug.replace("-", " ").title())
        site.is_active = active
        session.add(site)
        await session.flush()
        for connector in connectors:
            session.add(
                ToolMapping(
                    client_id=client.id,
                    site_id=site.id,
                    connector_name=connector,
                    external_id=f"{slug}-{connector.value}",
                )
            )
        await session.commit()
        await session.refresh(site)
        return site


def fake_generate(
    maker: async_sessionmaker[AsyncSession], calls: list[str], *, failing: dict[str, Exception]
) -> Any:
    """Stands in for `pipeline.generate`: stores a real draft, or raises for the named sites."""

    async def generate(client_ref: Any, site_ref: str, period_text: str, **_: Any) -> Any:
        calls.append(site_ref)
        if site_ref in failing:
            raise failing[site_ref]
        async with maker() as session:
            site = (await session.execute(select(Site).where(Site.slug == site_ref))).scalar_one()
            document = await make_document()
            if site_ref == "empty-co":  # every source failed: gaps, and no vendor fact
                document.facts = FactSet(
                    gaps=[Gap(what="NinjaOne devices", why="HTTP 401: refused", source="ninjaone")]
                )
            document.client_id, document.site_id = str(client_ref), str(site.id)
            document.site_slug = site_ref
            document.period = pipeline.parse_period(period_text, 1)
            row = await store.save_new(session, document, created_by="batch")
            await session.commit()
            return pipeline.Generated(report_id=row.id, document=document)

    return generate


async def test_every_mapped_active_site_is_drafted_past_a_failure_with_a_summary(
    scoped: None,
    db_client: Client,
    make_client: Any,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    engine: Any,
) -> None:
    both = (ConnectorName.NINJAONE, ConnectorName.AUTOTASK)
    await add_site(session_maker, db_client, "alpha-dental", connectors=both)
    await add_site(session_maker, db_client, "bravo-legal", connectors=(ConnectorName.AUTOTASK,))
    await add_site(session_maker, db_client, "charlie-unmapped", connectors=())
    await add_site(session_maker, db_client, "delta-closed", active=False)
    await add_site(session_maker, db_client, "echo-cpa")
    await add_site(session_maker, db_client, "empty-co")
    other = await make_client("another-franchisee", "Another Franchisee")
    await add_site(session_maker, other, "their-customer")  # never this client's to draft
    calls: list[str] = []
    failing = {"bravo-legal": AuthError("credentials refused", connector="autotask")}
    monkeypatch.setattr(pipeline, "generate", fake_generate(session_maker, calls, failing=failing))
    seen: list[str] = []

    batch = await pipeline.generate_all(
        db_client.id, "2026-08", actor="sam", on_site=lambda o: seen.append(o.site_slug)
    )

    by_site = {o.site_slug: o for o in batch.outcomes}
    assert calls == ["alpha-dental", "bravo-legal", "echo-cpa", "empty-co"]  # it went on past bravo
    assert {slug: o.status for slug, o in by_site.items()} == {
        "alpha-dental": "drafted",
        "bravo-legal": "failed",
        "charlie-unmapped": "skipped",
        "echo-cpa": "drafted",
        "empty-co": "failed",
    }  # the inactive site and the other client's site are not in the run at all
    assert seen == list(by_site)  # progress is reported site by site, in name order
    assert "AuthError: [autotask] credentials refused" in by_site["bravo-legal"].detail
    assert by_site["charlie-unmapped"].detail == "no NinjaOne or Autotask mapping"
    assert "every source failed (HTTP 401: refused)" in by_site["empty-co"].detail
    assert "slides on the deck" in by_site["alpha-dental"].detail
    assert uuid.UUID(by_site["alpha-dental"].report_id or "")
    assert (len(batch.drafted), len(batch.failed), len(batch.skipped)) == (2, 2, 1)
    summary = batch.as_dict()
    assert (summary["period"], summary["drafted"], summary["failed"]) == ("August 2026", 2, 2)
    # The failure is on the record (hard rule 4), against this client.
    rows = await audit_rows(engine, db_client.id)
    failed = [r for r in rows if r.event_type is AuditEventType.REPORT_GENERATED]
    assert [(r.outcome.value, r.payload["site"], r.actor_id) for r in failed] == [
        ("error", "bravo-legal", "sam")
    ]


async def test_a_site_with_a_review_is_left_alone_unless_asked(
    scoped: None,
    db_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await add_site(session_maker, db_client, "alpha-dental")
    await add_site(session_maker, db_client, "echo-cpa")
    calls: list[str] = []
    monkeypatch.setattr(pipeline, "generate", fake_generate(session_maker, calls, failing={}))
    first = await pipeline.generate_all(db_client.id, "2026-08", actor="sam")
    assert [o.status for o in first.outcomes] == ["drafted", "drafted"]

    again = await pipeline.generate_all(db_client.id, "2026-08", actor="sam")

    assert [o.status for o in again.outcomes] == ["skipped", "skipped"]
    assert "already has a review for August 2026" in again.outcomes[0].detail
    assert again.outcomes[0].report_id == first.outcomes[0].report_id  # the engineer's review
    assert calls == ["alpha-dental", "echo-cpa"]  # nothing was generated the second time

    another_month = await pipeline.generate_all(db_client.id, "2026-07", actor="sam")
    forced = await pipeline.generate_all(db_client.id, "2026-08", actor="sam", regenerate=True)

    assert [o.status for o in another_month.outcomes] == ["drafted", "drafted"]
    assert [o.status for o in forced.outcomes] == ["drafted", "drafted"]
    assert forced.outcomes[0].report_id != first.outcomes[0].report_id


async def test_an_unknown_client_or_a_bad_period_stops_before_any_site(
    scoped: None, db_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.connectors.factory import UnknownClient

    calls: list[str] = []
    monkeypatch.setattr(pipeline, "generate", fake_generate(None, calls, failing={}))  # type: ignore[arg-type]

    with pytest.raises(UnknownClient):
        await pipeline.generate_all("no-such-client", "2026-08", actor="sam")
    with pytest.raises(ValueError, match="2026-08"):
        await pipeline.generate_all(db_client.id, "August", actor="sam")
    assert calls == []

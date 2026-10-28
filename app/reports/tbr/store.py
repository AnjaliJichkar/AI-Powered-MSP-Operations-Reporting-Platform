"""Storing and loading reviews. Every read is through the client the request is scoped to.

A report id is not a capability. `load` takes the client id as well and treats a
report belonging to another client exactly as it treats one that does not
exist, so an id that leaks cannot be used to read another franchisee's review
(hard rule 1).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import TbrReport
from app.reports.tbr.model import FactSet, ReviewDocument, ReviewPeriod


class ReportNotFound(LookupError):
    """No such report for this client."""


async def save_new(
    session: AsyncSession, document: ReviewDocument, *, created_by: str
) -> TbrReport:
    """Store a freshly generated review, superseding any earlier one for the same period."""
    site_id = uuid.UUID(document.site_id)
    earlier = await session.execute(
        select(TbrReport).where(
            TbrReport.site_id == site_id,
            TbrReport.period_key == document.period.key,
            TbrReport.status != "superseded",
        )
    )
    for row in earlier.scalars():
        row.status = "superseded"
    report = TbrReport(
        client_id=uuid.UUID(document.client_id),
        site_id=site_id,
        period_key=document.period.key,
        period_start=document.period.start,
        period_end=document.period.end,
        status="draft",
        document=document.model_dump(mode="json"),
        created_by=created_by,
    )
    session.add(report)
    await session.flush()
    return report


async def load(
    session: AsyncSession, report_id: uuid.UUID | str, *, client_id: uuid.UUID | str
) -> tuple[TbrReport, ReviewDocument]:
    try:
        wanted, owner = uuid.UUID(str(report_id)), uuid.UUID(str(client_id))
    except ValueError as exc:
        raise ReportNotFound("no such report") from exc
    row = (
        await session.execute(
            select(TbrReport).where(TbrReport.id == wanted, TbrReport.client_id == owner)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ReportNotFound("no such report")
    return row, ReviewDocument.model_validate(row.document)


async def update(session: AsyncSession, row: TbrReport, document: ReviewDocument) -> None:
    document.renders = {}  # every deck written from the earlier text is now stale
    # It is no longer released, so nobody released it. Leaving the name on a
    # review that is back in draft would say somebody signed off words that are
    # not there any more.
    document.released_by = None
    document.released_at = None
    row.document = document.model_dump(mode="json")
    if row.status == "rendered":
        row.status = "draft"  # changed since it was rendered: the deck is stale
    await session.flush()


#: The two outputs of a review. The deck's render keys are the bare mode (`client`,
#: `internal`, `demo`), as they were before the PDF existed; the Monthly Report's
#: are `pdf/<mode>`. Both live in `document.renders` and both are emptied together
#: by `update`, so an edit makes the deck and the PDF stale at once.
Output = Literal["deck", "pdf"]
PDF_PREFIX = "pdf/"


def render_key(output: Output, mode: str) -> str:
    return f"{PDF_PREFIX}{mode}" if output == "pdf" else mode


async def record_render(
    session: AsyncSession,
    row: TbrReport,
    document: ReviewDocument,
    *,
    mode: str,
    path: str,
    output: Output = "deck",
    by: str | None = None,
) -> None:
    """Remember the file written for this output and mode. Only a client deck marks
    the review rendered: the deck is what releases a review, the PDF goes with it.

    `by` is who did it, and it is kept only for that one case: rendering an
    internal deck is a by-product and says nothing about who released anything.
    """
    document.renders[render_key(output, mode)] = path
    if mode == "client" and output == "deck":
        row.status = "rendered"
        row.rendered_path = path
        row.rendered_at = dt.datetime.now(dt.UTC)
        document.released_by = by
        document.released_at = row.rendered_at
    row.document = document.model_dump(mode="json")
    await session.flush()


async def list_for_client(
    session: AsyncSession, client_id: uuid.UUID, *, limit: int = 50
) -> list[TbrReport]:
    rows = await session.execute(
        select(TbrReport)
        .where(TbrReport.client_id == client_id, TbrReport.status != "superseded")
        .order_by(TbrReport.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


async def sites_with_a_review(
    session: AsyncSession, client_id: uuid.UUID, period_key: str
) -> dict[uuid.UUID, uuid.UUID]:
    """Site id -> the id of its current (not superseded) review for this period."""
    rows = await session.execute(
        select(TbrReport.site_id, TbrReport.id).where(
            TbrReport.client_id == client_id,
            TbrReport.period_key == period_key,
            TbrReport.status != "superseded",
        )
    )
    return {site_id: report_id for site_id, report_id in rows.all()}


async def current_for_month(
    session: AsyncSession, client_id: uuid.UUID, site_id: uuid.UUID, month: str
) -> TbrReport | None:
    """The review this customer currently has for a month, whatever its length.

    Matched on the month rather than on the whole `period_key` (`2026-08/1`),
    because a caller asking whether generating would replace something does not
    know, and should not have to know, how many months the franchisee's reviews
    cover: a one-month and a three-month review ending in August both occupy
    August as far as somebody looking at the page is concerned.
    """
    rows = await session.execute(
        select(TbrReport)
        .where(
            TbrReport.client_id == client_id,
            TbrReport.site_id == site_id,
            TbrReport.period_key.startswith(f"{month}/"),
            TbrReport.status != "superseded",
        )
        .order_by(TbrReport.created_at.desc())
        .limit(1)
    )
    return rows.scalars().first()


async def backup_trend(
    session: AsyncSession, site_id: uuid.UUID, *, limit: int = 6
) -> list[tuple[dt.datetime, float]]:
    """What every earlier review measured for backup success, oldest first.

    One point per stored review rather than one per month, and each point
    carries the moment that review was generated: the rate is the 28 days
    Cove was holding then, so two reviews a fortnight apart are two
    overlapping windows and a month name over either would be a claim
    neither supports.

    Superseded reviews are left out — a regenerated review replaces its
    period rather than adding a point — and a review from before Cove was
    connected simply has no rate and contributes nothing.
    """
    rows = (
        (
            await session.execute(
                select(TbrReport)
                .where(TbrReport.site_id == site_id, TbrReport.status != "superseded")
                .order_by(TbrReport.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    found: list[tuple[dt.datetime, float]] = []
    for row in rows:
        document = ReviewDocument.model_validate(row.document)
        fact = document.facts.get("cove.success.rate")
        if fact is None or not isinstance(fact.value, int | float):
            continue
        found.append((fact.as_of, float(fact.value)))
    return sorted(found)


async def prior_snapshot(
    session: AsyncSession, site_id: uuid.UUID, period: ReviewPeriod
) -> tuple[FactSet, str] | None:
    """The previous period's stored facts for this site, and that period's label."""
    before = period.previous()
    row = (
        await session.execute(
            select(TbrReport)
            .where(
                TbrReport.site_id == site_id,
                TbrReport.period_key == before.key,
                TbrReport.status != "superseded",
            )
            .order_by(TbrReport.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    document = ReviewDocument.model_validate(row.document)
    return document.facts, before.label

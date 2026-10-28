"""One coverage snapshot a day per client, and what the board reads back from them.

The page never computes. The daily collector, or a person's Refresh, computes
the board once (`take_snapshot`) and stores it whole — the board itself in
`board` (`app/coverage/serialize.py`), the counts in `summary`, the gaps in
`gaps` — one row per (client, UTC day), replaced when the day is run again.
The page reads the latest row (`latest_board`) with the client's live
exclusions laid over it, and says when it was taken and by whom; `trend` is
the covered share per tool over the last days, `new_gaps` every gap on the
latest board that the one before did not have.

Idempotent: running the day twice replaces the row and changes nothing else,
so a rerun after a failed cron never doubles a day or shifts a trend. Each
snapshot is an audit row (hard rule 4) with its counts, how long the
computation took and what the last-seen read did, which is also where "how
long does it take" is answered without a stopwatch.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.connectors.factory import resolve_client
from app.coverage.compute import CoverageReport, compute_coverage
from app.coverage.core import Exclusion
from app.coverage.exclusions import live_exclusions
from app.coverage.serialize import report_from_json, report_to_json
from app.db.session import session_scope
from app.models.coverage import CoverageSnapshot
from app.models.enums import AuditEventType, AuditOutcome, Interface

logger = logging.getLogger(__name__)

#: How many days of snapshots the page's trend shows.
TREND_DAYS = 30


def summarise(report: CoverageReport) -> dict[str, Any]:
    """The snapshot's summary: per tool for the client, and per site."""
    return {
        "computed_at": report.computed_at.isoformat(),
        "duration_ms": report.duration_ms,
        "managed": len(report.devices),
        "not_computers": report.not_computers,
        "unmanaged": len(report.unmanaged),
        "reads": dict(report.reads),
        "tools": {
            key: {**counts.as_dict(), "assessed": report.tool(key).assessed}
            for key, counts in report.totals().items()
        },
        "sites": [site.as_dict() for site in report.sites],
    }


def gap_rows(report: CoverageReport) -> list[dict[str, Any]]:
    names = {d.device.device_id: d.device.name for d in report.devices}
    return [
        {
            "site": site,
            "device_id": device_id,
            "device": names.get(device_id, ""),
            "tool": tool,
            "state": state,
        }
        for site, device_id, tool, state in sorted(report.gap_keys())
    ]


async def store_snapshot(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    report: CoverageReport,
    actor: str,
    taken_on: dt.date | None = None,
    board: CoverageReport | None = None,
) -> CoverageSnapshot:
    """Write or replace the day's row. Caller owns the transaction.

    `report` is the board as shown (exclusions applied), for the summary and
    the gaps; `board` is the board as computed, without exclusions, which is
    what the page rebuilds from. One report serves as both when no
    exclusions were applied.
    """
    day = taken_on or report.computed_at.date()
    values = {
        "client_id": client_id,
        "taken_on": day,
        "taken_at": report.computed_at,
        "taken_by": actor,
        "duration_ms": report.duration_ms,
        "summary": summarise(report),
        "gaps": gap_rows(report),
        "board": report_to_json(board if board is not None else report),
    }
    statement = insert(CoverageSnapshot).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_coverage_snapshots_client_day",
        set_={k: v for k, v in values.items() if k not in ("client_id", "taken_on")},
    )
    await session.execute(statement)
    row = (
        await session.execute(
            select(CoverageSnapshot).where(
                CoverageSnapshot.client_id == client_id, CoverageSnapshot.taken_on == day
            )
        )
    ).scalar_one()
    return row


@dataclass(slots=True)
class SnapshotResult:
    client_slug: str
    taken_on: dt.date
    duration_ms: int
    managed: int
    unmanaged: int
    tools: dict[str, float | None] = field(default_factory=dict)
    replaced: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.client_slug,
            "taken_on": self.taken_on.isoformat(),
            "duration_ms": self.duration_ms,
            "managed": self.managed,
            "unmanaged": self.unmanaged,
            "covered_percent": self.tools,
            "replaced": self.replaced,
        }


async def take_snapshot(
    client_ref: str | uuid.UUID,
    *,
    actor: str,
    now: dt.datetime | None = None,
    interface: Interface = Interface.SYSTEM,
    actor_user_id: uuid.UUID | None = None,
) -> SnapshotResult:
    """Compute the board and store today's row: the daily collector's step, and
    what a person's Refresh runs in the background.

    The board is computed without exclusions and stored that way; the summary
    and the gaps are taken from it with the live exclusions applied, as the
    page shows it.
    """
    now = now or dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        client = await resolve_client(session, client_ref)
        client_id, client_slug = client.id, client.slug
        existed = (
            await session.execute(
                select(CoverageSnapshot.id).where(
                    CoverageSnapshot.client_id == client_id,
                    CoverageSnapshot.taken_on == now.date(),
                )
            )
        ).scalar_one_or_none()
    computed = await compute_coverage(client_id, now=now, exclusions=[])
    exclusions = await live_exclusions(client_id)
    report = report_from_json(report_to_json(computed), exclusions=exclusions)
    audit = AuditContext.new(
        actor_id=actor,
        interface=interface,
        client_id=client_id,
        client_ref=client_slug,
        actor_user_id=actor_user_id,
    )
    async with session_scope() as session:
        row = await store_snapshot(
            session, client_id=client_id, report=report, actor=actor, board=computed
        )
        await record_event(
            session,
            audit,
            event_type=AuditEventType.COVERAGE_SNAPSHOT,
            outcome=AuditOutcome.SUCCESS,
            duration_ms=report.duration_ms,
            payload={
                "taken_on": row.taken_on.isoformat(),
                "replaced": existed is not None,
                "managed": len(report.devices),
                "unmanaged": len(report.unmanaged),
                "gaps": len(report.gap_keys()),
                "tools": {key: counts.covered_percent for key, counts in report.totals().items()},
                "reads": dict(computed.reads),
            },
        )
    return SnapshotResult(
        client_slug=client_slug,
        taken_on=row.taken_on,
        duration_ms=report.duration_ms,
        managed=len(report.devices),
        unmanaged=len(report.unmanaged),
        tools={key: counts.covered_percent for key, counts in report.totals().items()},
        replaced=existed is not None,
    )


async def latest_snapshot(session: AsyncSession, client_id: uuid.UUID) -> CoverageSnapshot | None:
    """The newest row for the client, whatever day it is from."""
    return (
        await session.execute(
            select(CoverageSnapshot)
            .where(CoverageSnapshot.client_id == client_id)
            .order_by(CoverageSnapshot.taken_on.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def board_from_snapshot(
    row: CoverageSnapshot | None, exclusions: list[Exclusion]
) -> CoverageReport | None:
    """The board the page shows: the latest stored board with today's exclusions
    over it. None when there is no row yet, or the row is from before boards
    were stored (the page then asks for a refresh)."""
    if row is None or row.board is None:
        return None
    return report_from_json(row.board, exclusions=exclusions)


async def recent_snapshots(
    session: AsyncSession, client_id: uuid.UUID, *, days: int = TREND_DAYS
) -> list[CoverageSnapshot]:
    """The last `days` snapshots, oldest first."""
    rows = (
        (
            await session.execute(
                select(CoverageSnapshot)
                .where(CoverageSnapshot.client_id == client_id)
                .order_by(CoverageSnapshot.taken_on.desc())
                .limit(days)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


def trend(snapshots: list[CoverageSnapshot]) -> list[dict[str, Any]]:
    """Per day: covered share per tool, managed and gap counts."""
    return [
        {
            "taken_on": row.taken_on.isoformat(),
            "managed": row.summary.get("managed", 0),
            "unmanaged": row.summary.get("unmanaged", 0),
            "gaps": len(row.gaps),
            "tools": {
                key: values.get("covered_percent")
                for key, values in row.summary.get("tools", {}).items()
            },
        }
        for row in snapshots
    ]


def new_gaps(
    report: CoverageReport, previous: CoverageSnapshot | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Gaps on today's board that the last snapshot did not have, and which day that was.

    With no snapshot yet, every gap is new and the second value says so.
    """
    if previous is None:
        return gap_rows(report), None
    before = {(g["site"], g["device_id"], g["tool"], g["state"]) for g in previous.gaps}
    fresh = [
        gap
        for gap in gap_rows(report)
        if (gap["site"], gap["device_id"], gap["tool"], gap["state"]) not in before
    ]
    return fresh, previous.taken_on.isoformat()

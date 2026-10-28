"""The outcome ledger against a real database, with Autotask faked.

What is tested is what a ledger has to be: idempotent, so the collector can run
twice; honest about a ticket that came back; and measured by the same clock the
queue uses, so a breach is a breach in both places.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.resolved.ledger as ledger_module
import app.tickets.enrich as enrich_module
from app.audit.recorder import AuditContext
from app.connectors.errors import ConnectorError, UpstreamError
from app.connectors.scope import SiteRef
from app.models import AuditLog
from app.models.enums import (
    AuditEventType,
    AuditOutcome,
    ConnectorName,
    Interface,
)
from app.models.outcome import TicketOutcome
from app.models.site import Site
from app.reports.tbr.config import BusinessHours, FranchiseeConfig
from app.resolved.config import (
    ClassificationConfig,
    CostConfig,
    ResolvedConfig,
    ResolverConfig,
    SlaConfig,
    SlaTarget,
    TicketClassRule,
)
from app.resolved.ledger import build_ledger
from tests.api.conftest import Make

pytestmark = pytest.mark.usefixtures("clean")

CHICAGO = dt.timezone(-dt.timedelta(hours=6))
#: A Wednesday at noon, local.
NOW = dt.datetime(2026, 9, 16, 12, tzinfo=CHICAGO)


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=CHICAGO)


def config() -> FranchiseeConfig:
    return FranchiseeConfig(
        display_name="Acme IT",
        business_hours=BusinessHours(timezone="UTC-06:00", start=dt.time(9), end=dt.time(17)),
        resolved=ResolvedConfig(
            sla=SlaConfig(
                targets={
                    "High": SlaTarget(first_response_minutes=60, resolution_minutes=480),
                    "Low": SlaTarget(first_response_minutes=480, resolution_minutes=2880),
                },
                paused_status_labels=["Waiting Customer"],
            ),
            classes=ClassificationConfig(
                rules=[TicketClassRule(ticket_class="L3", priority=["High"])], default="L1"
            ),
            resolvers=ResolverConfig(inputiv=["Sam Okafor"]),
            cost=CostConfig(
                default_hourly=Decimal("60"), by_resource={"Sam Okafor": Decimal("40")}
            ),
            reopened_within_days=7,
        ),
    )


def ticket(ticket_id: int, **over: Any) -> Any:
    fields: dict[str, Any] = {
        "id": ticket_id,
        "ticket_number": f"T{ticket_id}",
        "title": "A ticket",
        "priority": 1,
        "status": 5,
        "queue_id": 10,
        "issue_type": 3,
        "assigned_resource_id": 501,
        "create_date": at(14, 9),
        "first_response_at": at(14, 9, 30),
        "completed_date": at(14, 15),
    }
    fields.update(over)
    return SimpleNamespace(**fields)


class Knobs:
    """What the fake Autotask will answer with, changed between runs."""

    def __init__(self, site: SiteRef) -> None:
        self.site = site
        self.completed: list[Any] = []
        self.by_id: dict[int, Any] = {}
        self.entries: list[Any] = []
        self.calls = 0
        #: One entry per `get_tickets_by_ids` call: which ids the run asked
        #: about again. What is asserted when the question is what we *did not*
        #: ask the vendor.
        self.asked: list[list[int]] = []


@pytest.fixture
def autotask(monkeypatch: pytest.MonkeyPatch) -> Knobs:
    knobs = Knobs(
        SiteRef(site_id=uuid.uuid4(), slug="acme-dental", name="Acme Dental", external_id="1")
    )

    @asynccontextmanager
    async def fake_connector_for(client_id: uuid.UUID, connector: Any, **kwargs: Any) -> Any:
        class FakeAutotask:
            async def list_tickets(self, **kwargs: Any) -> tuple[list[Any], bool]:
                knobs.calls += 1
                return [(knobs.site, t) for t in knobs.completed], False

            async def get_tickets_by_ids(self, ids: list[int]) -> list[Any]:
                knobs.asked.append(list(ids))
                return [(knobs.site, knobs.by_id[i]) for i in ids if i in knobs.by_id]

            async def list_time_entries(
                self, ticket_ids: list[int], **kwargs: Any
            ) -> tuple[list[Any], bool]:
                return [e for e in knobs.entries if e.ticket_id in ticket_ids], False

            async def get_resources(self, ids: list[int]) -> dict[int, Any]:
                return {
                    501: SimpleNamespace(
                        name="Priya Raman", user_name="praman", email="p@acme.test"
                    ),
                    504: SimpleNamespace(
                        name="Sam Okafor", user_name="sokafor", email="s@inputiv.test"
                    ),
                }

            async def label_for(self, entity: str, field: str, value: Any) -> str | None:
                return {
                    "priority": {1: "High", 4: "Low"},
                    "status": {5: "Complete", 7: "Waiting Customer", 8: "In Progress"},
                    "queueID": {10: "Help Desk"},
                    "issueType": {3: "Printer/Scanner/Copier"},
                }[field].get(value)

        yield FakeAutotask()

    monkeypatch.setattr(ledger_module, "connector_for", fake_connector_for)
    return knobs


async def run(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: uuid.UUID,
    *,
    now: dt.datetime = NOW,
) -> Any:
    async with session_maker() as session:
        context = AuditContext.new(
            actor_id="test",
            actor_user_id=None,
            interface=Interface.CLI,
            client_id=client_id,
            client_ref="acme-it",
        )
        result = await build_ledger(
            session,
            context,
            client_id,
            config=config(),
            zone=CHICAGO,
            since=now - dt.timedelta(days=30),
            now=now,
        )
        await session.commit()
        return result


async def rows(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID
) -> list[TicketOutcome]:
    async with session_maker() as session:
        found = await session.execute(
            select(TicketOutcome)
            .where(TicketOutcome.client_id == client_id)
            .order_by(TicketOutcome.ticket_id)
        )
        return list(found.scalars().all())


@pytest.fixture
async def client_id(
    make_client: Make, session_maker: async_sessionmaker[AsyncSession]
) -> uuid.UUID:
    client = await make_client("acme-it", "Acme IT")
    return client.id


async def _site(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, ref: SiteRef
) -> None:
    async with session_maker() as session:
        session.add(Site(id=ref.site_id, client_id=client_id, slug=ref.slug, name=ref.name))
        await session.commit()


# --- the rows ------------------------------------------------------------------------------


async def test_one_row_per_completed_ticket_measured_on_business_hours(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1)]
    result = await run(session_maker, client_id)
    assert result.rows_written == 1

    (row,) = await rows(session_maker, client_id)
    assert row.ticket_number == "T1"
    assert row.ticket_class == "L3"  # High, by the rule
    assert row.resolver == "franchisee"  # 501 is not in the Inputiv list
    assert row.pickup_minutes == 30  # 09:00 to 09:30
    assert row.resolve_minutes == 360  # 09:00 to 15:00
    assert row.first_response_met is True  # 30 against a 60-minute target
    assert row.resolution_met is True  # 360 against 480
    assert row.reopened is False


async def test_a_ticket_ours_closed_is_recorded_as_ours(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1, assigned_resource_id=504)]
    await run(session_maker, client_id)
    (row,) = await rows(session_maker, client_id)
    assert row.resolver == "inputiv"
    assert row.resolver_name == "Sam Okafor"


async def test_a_ticket_nobody_is_on_is_neither_side(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1, assigned_resource_id=None)]
    await run(session_maker, client_id)
    (row,) = await rows(session_maker, client_id)
    assert row.resolver == "unknown"


async def test_a_missed_target_is_recorded_as_missed(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    autotask.completed = [ticket(1, first_response_at=at(14, 11))]  # two hours, target one
    await _site(session_maker, client_id, autotask.site)
    await run(session_maker, client_id)
    (row,) = await rows(session_maker, client_id)
    assert row.first_response_met is False
    assert row.pickup_minutes == 120


async def test_a_priority_with_no_target_is_not_judged_either_way(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    """Null is the answer, not false: a percentage is taken over the judged."""
    autotask.completed = [ticket(1, priority=99)]
    await _site(session_maker, client_id, autotask.site)
    await run(session_maker, client_id)
    (row,) = await rows(session_maker, client_id)
    assert row.first_response_met is None
    assert row.resolution_met is None
    # Still measured, and still classified: only the judgement is withheld.
    assert row.pickup_minutes == 30
    assert row.ticket_class == "L1"


# --- idempotence and reopening -----------------------------------------------------------------


async def test_running_it_twice_changes_nothing(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1), ticket(2)]
    await run(session_maker, client_id)
    first = await rows(session_maker, client_id)
    await run(session_maker, client_id)
    second = await rows(session_maker, client_id)
    assert len(first) == len(second) == 2
    assert [r.id for r in first] == [r.id for r in second]  # updated, never re-inserted
    assert [r.resolve_minutes for r in second] == [360, 360]


async def test_a_ticket_that_comes_back_is_marked_reopened_and_stays_one_row(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1)]
    await run(session_maker, client_id)

    # Two days later it is open again. It is no longer in the completed listing,
    # so it is found by id — which is what the ledger asks for.
    reopened = ticket(1, completed_date=None, status=8)
    autotask.completed = []
    autotask.by_id = {1: reopened}
    later = NOW + dt.timedelta(days=2)
    result = await run(session_maker, client_id, now=later)
    assert result.reopened == 1

    (row,) = await rows(session_maker, client_id)
    assert row.reopened is True
    assert row.reopened_at is not None
    assert row.completed_at is None


async def test_a_reopen_outside_the_window_is_not_counted_as_one(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    """Seven days is the franchisee's window. A ticket raised again a month
    later is a new problem, not a failed outcome."""
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1)]
    await run(session_maker, client_id)

    autotask.completed = []
    autotask.by_id = {1: ticket(1, completed_date=None, status=8)}
    result = await run(session_maker, client_id, now=NOW + dt.timedelta(days=30))
    assert result.reopened == 0
    (row,) = await rows(session_maker, client_id)
    assert row.reopened is False


async def test_a_reopened_ticket_that_closes_again_keeps_its_history(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1)]
    await run(session_maker, client_id)
    autotask.completed = []
    autotask.by_id = {1: ticket(1, completed_date=None, status=8)}
    await run(session_maker, client_id, now=NOW + dt.timedelta(days=1))

    # Closed again the next day.
    autotask.completed = [ticket(1, completed_date=at(16, 16))]
    autotask.by_id = {}
    await run(session_maker, client_id, now=NOW + dt.timedelta(days=2))

    (row,) = await rows(session_maker, client_id)
    assert row.reopened is True  # it did not stay closed the first time
    assert row.completed_at is not None  # and it is closed now


# --- time and money --------------------------------------------------------------------------


async def test_hours_and_cost_come_from_the_time_entries(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1)]
    autotask.entries = [
        SimpleNamespace(id=1, ticket_id=1, resource_id=501, hours_worked=2.0),
        SimpleNamespace(id=2, ticket_id=1, resource_id=504, hours_worked=1.5),
    ]
    await run(session_maker, client_id)
    (row,) = await rows(session_maker, client_id)
    assert float(row.hours_logged or 0) == 3.5
    # Two hours at the franchisee's default 60, one and a half at Sam's 40.
    assert float(row.cost or 0) == 2.0 * 60 + 1.5 * 40


async def test_a_franchisee_with_no_cost_book_gets_hours_and_no_money(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: uuid.UUID,
    autotask: Knobs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cost from a guessed rate would be worse than no cost."""
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1)]
    autotask.entries = [SimpleNamespace(id=1, ticket_id=1, resource_id=501, hours_worked=2.0)]

    bare = config()
    bare.resolved.cost = CostConfig()
    async with session_maker() as session:
        context = AuditContext.new(
            actor_id="test",
            actor_user_id=None,
            interface=Interface.CLI,
            client_id=client_id,
            client_ref="acme-it",
        )
        await build_ledger(
            session,
            context,
            client_id,
            config=bare,
            zone=CHICAGO,
            since=NOW - dt.timedelta(days=30),
            now=NOW,
        )
        await session.commit()

    (row,) = await rows(session_maker, client_id)
    assert float(row.hours_logged or 0) == 2.0
    assert row.cost is None


async def test_another_clients_row_is_never_touched(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: uuid.UUID,
    autotask: Knobs,
    make_client: Make,
) -> None:
    """Hard rule 1, in the one table that is keyed by a vendor's own ticket id:
    two franchisees can hold the same Autotask id and must not collide."""
    other = await make_client("other-it", "Other IT")
    await _site(session_maker, client_id, autotask.site)
    autotask.completed = [ticket(1)]
    await run(session_maker, client_id)
    async with session_maker() as session:
        session.add(
            TicketOutcome(
                client_id=other.id,
                ticket_id="1",
                ticket_class="L1",
                resolver="unknown",
                collected_at=NOW,
            )
        )
        await session.commit()

    await run(session_maker, client_id)
    async with session_maker() as session:
        found = await session.execute(
            select(TicketOutcome).where(TicketOutcome.client_id == other.id)
        )
        (theirs,) = found.scalars().all()
    assert theirs.ticket_class == "L1"  # untouched by our client's run


# --- what a failed run leaves behind, and what it asks for -------------------------------


async def _build(session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID) -> None:
    """One ledger pass in a transaction of the caller's, uncommitted."""
    async with session_maker() as session:
        # Something of our own written first, so the rollback below is provably
        # a rollback: if this row survived, the test would prove nothing.
        session.add(
            TicketOutcome(
                client_id=client_id,
                ticket_id="999",
                ticket_class="L1",
                resolver="unknown",
                collected_at=NOW,
            )
        )
        await session.flush()
        await build_ledger(
            session,
            AuditContext.new(
                actor_id="test",
                actor_user_id=None,
                interface=Interface.CLI,
                client_id=client_id,
                client_ref="acme-it",
            ),
            client_id,
            config=config(),
            zone=CHICAGO,
            since=NOW - dt.timedelta(days=30),
            now=NOW,
        )
        await session.commit()


@pytest.fixture
def refusing_autotask(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vendor that answers the ledger's first read with a 500."""

    @asynccontextmanager
    async def connector(client_id: uuid.UUID, connector_name: Any, **kwargs: Any) -> Any:
        class Refusing:
            async def list_tickets(self, **kwargs: Any) -> tuple[list[Any], bool]:
                raise UpstreamError(
                    "autotask returned 500 on 4 attempts",
                    connector="autotask",
                    status_code=500,
                )

        yield Refusing()

    monkeypatch.setattr(ledger_module, "connector_for", connector)


async def test_a_vendor_failure_is_on_the_record_though_the_run_rolled_back(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: uuid.UUID,
    refusing_autotask: None,
) -> None:
    """Hard rule 4, against the thing that was quietly breaking it.

    The ledger raises when Autotask will not answer, and the caller's
    transaction rolls back — which used to discard the failure row written
    inside it, so a run that failed left nothing behind at all. The row goes on
    its own connection now and is still there afterwards.
    """
    with pytest.raises(ConnectorError):
        await _build(session_maker, client_id)

    assert await rows(session_maker, client_id) == []  # the rollback was real
    async with session_maker() as session:
        found = (
            (await session.execute(select(AuditLog).where(AuditLog.client_id == client_id)))
            .scalars()
            .all()
        )

    assert len(found) == 1
    (row,) = found
    assert (row.event_type, row.outcome) == (AuditEventType.TOOL_CALL, AuditOutcome.ERROR)
    assert (row.tool_name, row.connector_name) == ("ticket_outcomes", ConnectorName.AUTOTASK)
    assert row.error_type == "UpstreamError"
    assert row.error_message is not None and "500" in row.error_message


async def test_an_audit_log_that_cannot_be_written_still_reports_the_vendors_error(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: uuid.UUID,
    refusing_autotask: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording a failure may not become a second, different failure.

    Whatever is wrong with the audit write, what the caller is told is what the
    vendor said — otherwise a database problem would arrive disguised as
    something else entirely.
    """

    @asynccontextmanager
    async def no_connection() -> Any:
        raise RuntimeError("no connection for the audit row")
        yield  # pragma: no cover

    monkeypatch.setattr(enrich_module, "session_scope", no_connection)

    with pytest.raises(ConnectorError, match="500"):
        await _build(session_maker, client_id)


async def test_only_tickets_that_could_still_reopen_are_asked_about_again(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, autotask: Knobs
) -> None:
    """The re-read is the reopen rule's own window, not every row ever written.

    A ticket completed longer ago than `reopened_within_days` cannot be marked
    reopened whatever the vendor says about it, so it is not asked about — and
    the id list stops growing with the table, which is what eventually made the
    vendor refuse the query (api-notes, 2026-10-01).
    """
    await _site(session_maker, client_id, autotask.site)
    # Completed two days before `now`, and completed fifteen days before it.
    autotask.completed = [
        ticket(1, completed_date=at(14, 15)),
        ticket(2, completed_date=at(1, 15)),
    ]
    await run(session_maker, client_id)
    assert autotask.asked == []  # nothing was held when that run started

    # Both are open again at the vendor.
    autotask.completed = []
    autotask.by_id = {
        1: ticket(1, completed_date=None, status=8),
        2: ticket(2, completed_date=None, status=8),
    }
    result = await run(session_maker, client_id)

    assert autotask.asked == [[1]]  # only the one still inside its window
    assert result.reopened == 1
    by_ticket = {row.ticket_id: row for row in await rows(session_maker, client_id)}
    assert by_ticket["1"].reopened is True
    # The other keeps the completion it had: not asked about, so not changed.
    assert by_ticket["2"].reopened is False
    assert by_ticket["2"].completed_at is not None

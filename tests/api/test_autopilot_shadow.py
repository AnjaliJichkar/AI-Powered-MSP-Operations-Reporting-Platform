"""The replay against a real database, with the model faked.

What is tested is what a shadow run has to be: ordered by creation so grouping
cannot see the future, storing labels and ids and **no ticket text**, excluding
the titles the old scrubber damaged from every accuracy figure, and scoring
`unsure` as a decline rather than an error.
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.recorder import AuditContext
from app.autopilot.config import AutopilotConfig
from app.autopilot.shadow.replay import ReplayConfig, replay
from app.autopilot.shadow.sampling import ITERATION, sample_for, ticket_ids
from app.autopilot.shadow.schema import TOOL_NAME
from app.autopilot.shadow.scoring import score_run
from app.models import AuditLog
from app.models.enums import AuditEventType, Interface
from app.models.outcome import TicketOutcome
from app.models.shadow import ShadowBatch, ShadowPrediction, ShadowRun, ShadowSample
from app.models.site import Site
from app.settings import Settings
from tests.api.conftest import Make

pytestmark = pytest.mark.usefixtures("clean")


class _BadRequest(Exception):
    """Shaped like the SDK's `BadRequestError`, which is what a dead account
    raises. Named for the type, because the runner reads the type name."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


_BadRequest.__name__ = "BadRequestError"

NOW = dt.datetime(2026, 9, 30, 12, tzinfo=dt.UTC)
TYPES = ["Application/Software", "Email", "Printer/Scanner/Copier"]


def at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 9, day, hour, tzinfo=dt.UTC)


class FakeModel:
    """One canned answer per call, in order, cycling when it runs out."""

    def __init__(
        self, answers: list[dict[str, Any]], *, usage: tuple[int, int, int] = (100, 20, 0)
    ) -> None:
        self.answers = answers
        self.usage = usage
        self.seen: list[str] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        text = kwargs["messages"][0]["content"]
        self.seen.append(text)
        answer = self.answers[len(self.seen) % len(self.answers) - 1]
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=TOOL_NAME, input=answer)],
            usage=SimpleNamespace(
                input_tokens=self.usage[0],
                output_tokens=self.usage[1],
                cache_read_input_tokens=self.usage[2],
            ),
        )


def answer(**over: Any) -> dict[str, Any]:
    base = {
        "origin": "human",
        "origin_confidence": 0.8,
        "issue_type": "Email",
        "issue_type_confidence": 0.9,
        "urgency": "normal",
        "urgency_confidence": 0.5,
    }
    base.update(over)
    return base


def outcome(ticket_id: int, *, client_id: uuid.UUID, site_id: uuid.UUID, **over: Any) -> Any:
    fields: dict[str, Any] = {
        "client_id": client_id,
        "site_id": site_id,
        "ticket_id": str(ticket_id),
        "ticket_number": f"T{ticket_id}",
        "title": "Outlook will not open",
        "ticket_class": "L1",
        "resolver": "franchisee",
        "resolver_name": "Someone",
        "priority_label": "Medium",
        "status_label": "Complete",
        "queue_label": "Help Desk",
        "issue_type_label": "Email",
        "created_at_vendor": at(10),
        "completed_at": at(11),
        "collected_at": NOW,
    }
    fields.update(over)
    return TicketOutcome(**fields)


async def _world(
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    rows: list[Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    client = await make_client("acme-it", "Acme IT")
    site_id = uuid.uuid4()
    async with session_maker() as session:
        session.add(Site(id=site_id, client_id=client.id, slug="acme-dental", name="Acme Dental"))
        await session.flush()
        for row in rows or []:
            session.add(row)
        await session.commit()
    return client.id, site_id


async def _run(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: uuid.UUID,
    model: FakeModel,
    *,
    config: ReplayConfig | None = None,
    policy: AutopilotConfig | None = None,
    resume: uuid.UUID | None = None,
    on_progress: Any = None,
    model_name: str | None = None,
    batched: bool | None = None,
) -> Any:
    settings = Settings(autopilot_model=model_name) if model_name else Settings()
    async with session_maker() as session:
        result = await replay(
            session,
            AuditContext.new(
                actor_id="test",
                actor_user_id=None,
                interface=Interface.CLI,
                client_id=client_id,
                client_ref="acme-it",
            ),
            client_id,
            model_client=model,
            settings=settings,
            config=config or ReplayConfig(days=90),
            policy=policy,
            resume=resume,
            batched=batched,
            on_progress=on_progress,
            now=NOW,
        )
        await session.commit()
        return result


async def test_a_run_stores_one_prediction_per_ticket_and_no_ticket_text(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The rule the whole table rests on: labels, ids and numbers, never words."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        for number in (1, 2, 3):
            session.add(outcome(number, client_id=client_id, site_id=site_id))
        await session.commit()

    result = await _run(session_maker, client_id, FakeModel([answer()]))
    assert result.tickets_seen == 3
    assert result.by_model == 3
    assert result.errors == 0

    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(ShadowPrediction).where(ShadowPrediction.run_id == result.run_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 3
    stored = " ".join(
        str(value)
        for row in rows
        for value in (row.issue_type, row.origin, row.urgency, row.group_basis, row.error_message)
    )
    assert "Outlook" not in stored  # the title is nowhere in the table


async def test_the_run_records_what_it_ran_as(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """Two runs whose numbers differ are only informative if what changed
    between them is on the record."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        await session.commit()

    result = await _run(session_maker, client_id, FakeModel([answer()]))
    async with session_maker() as session:
        run = await session.get(ShadowRun, result.run_id)
        assert run is not None
        assert run.mode == "replay"
        assert run.prompt_version
        assert run.model
        assert run.config["days"] == 90
        assert run.config["variant"] == "title_site_time"
        assert run.config["group_similarity"] == pytest.approx(0.7)
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.client_id == client_id)))
            .scalars()
            .all()
        )
    kinds = {row.event_type for row in rows}
    assert AuditEventType.AUTOPILOT_RUN_STARTED in kinds
    assert AuditEventType.AUTOPILOT_RUN_FINISHED in kinds


async def test_the_agent_is_shown_only_what_was_known_at_creation(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The outcome fields must not reach the prompt, or the measurement is of
    hindsight. The queue is withheld too in the default variant."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(
            outcome(
                1,
                client_id=client_id,
                site_id=site_id,
                title="Mailbox full",
                issue_type_label="Email",
                priority_label="Critical",
                resolver_name="Priya Raman",
                queue_label="Escalation",
                hours_logged=3.5,
            )
        )
        await session.commit()

    model = FakeModel([answer()])
    await _run(session_maker, client_id, model)
    prompt = model.seen[0]
    assert "Mailbox full" in prompt  # the subject is the input
    for withheld in ("Critical", "Priya", "Escalation", "3.5", "Complete"):
        assert withheld not in prompt


async def test_the_alert_queue_variant_says_so_and_the_default_does_not(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id, queue_label="Alerts"))
        await session.commit()

    default = FakeModel([answer()])
    await _run(session_maker, client_id, default)
    assert "alert queue" not in default.seen[0]

    variant = FakeModel([answer()])
    await _run(
        session_maker,
        client_id,
        variant,
        config=ReplayConfig(days=90, variant="with_alert_queue"),
    )
    assert "alert queue" in variant.seen[0]


async def test_a_damaged_title_is_predicted_on_and_left_out_of_accuracy(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The 30 titles the old scrubber took a word from: predicted, quarantined,
    and absent from every accuracy figure rather than silently counted."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        session.add(
            outcome(
                2,
                client_id=client_id,
                site_id=site_id,
                title="Password [redacted] for a user",
            )
        )
        await session.commit()

    result = await _run(session_maker, client_id, FakeModel([answer()]))
    assert result.quarantined == 1

    async with session_maker() as session:
        card = await score_run(session, result.run_id, client_id=client_id)
    assert card.tickets == 2
    assert card.quarantined == 1
    assert card.issue_type.judged == 1  # only the undamaged one is judged


async def test_grouping_is_backwards_only_and_within_one_site(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client_id, site_id = await _world(session_maker, make_client)
    other_site = uuid.uuid4()
    async with session_maker() as session:
        session.add(Site(id=other_site, client_id=client_id, slug="beta", name="Beta"))
        await session.flush()
        session.add(
            outcome(
                1,
                client_id=client_id,
                site_id=site_id,
                title="Disk space low on SRV-01",
                created_at_vendor=at(10),
            )
        )
        session.add(
            outcome(
                2,
                client_id=client_id,
                site_id=site_id,
                title="Disk space low on SRV-04",
                created_at_vendor=at(11),
            )
        )
        session.add(
            outcome(
                3,
                client_id=client_id,
                site_id=other_site,
                title="Disk space low on SRV-09",
                created_at_vendor=at(12),
            )
        )
        await session.commit()

    result = await _run(session_maker, client_id, FakeModel([answer()]))
    async with session_maker() as session:
        rows = {
            row.ticket_id: row
            for row in (
                await session.execute(
                    select(ShadowPrediction).where(ShadowPrediction.run_id == result.run_id)
                )
            )
            .scalars()
            .all()
        }
    assert rows["1"].group_with is None  # nothing earlier existed
    assert rows["2"].group_with == "1"  # joined the earlier one at its own site
    assert rows["3"].group_with is None  # another customer is another incident
    assert result.groups == 2


async def test_an_invalid_answer_is_stored_as_an_error_not_a_prediction(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        await session.commit()

    result = await _run(session_maker, client_id, FakeModel([answer(issue_type="Not A Real Type")]))
    assert result.errors == 1
    assert result.by_model == 0
    async with session_maker() as session:
        row = (
            await session.execute(
                select(ShadowPrediction).where(ShadowPrediction.run_id == result.run_id)
            )
        ).scalar_one()
    assert row.path == "error"
    assert row.error_type == "InvalidPrediction"
    assert row.issue_type is None  # nothing was salvaged from it


async def test_unsure_is_a_decline_and_not_a_wrong_answer(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """Folding `unsure` into the error rate would push the thresholds the wrong
    way: the point of Level 0 is to find where the agent should stay quiet."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        session.add(outcome(2, client_id=client_id, site_id=site_id, ticket_number="T2"))
        await session.commit()

    result = await _run(
        session_maker, client_id, FakeModel([answer(), answer(issue_type="unsure")])
    )
    async with session_maker() as session:
        card = await score_run(session, result.run_id, client_id=client_id)
    assert card.issue_type.judged == 2
    assert card.issue_type.unsure == 1
    assert card.issue_type.correct == 1
    assert card.issue_type.accuracy == pytest.approx(1.0)  # one answered, one correct


async def test_an_untyped_ticket_is_excluded_but_reported(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """There is nothing to be right about, and what it said is still worth
    knowing: these are the 128 tickets nobody at the tenant typed."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        # A typed ticket as well, or the tenant has no label set at all and
        # `Email` would be refused as out-of-set rather than scored.
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        session.add(outcome(2, client_id=client_id, site_id=site_id, issue_type_label=None))
        await session.commit()

    # A full replay: an untyped ticket is in the window and out of every
    # sample, because there is no class to stratify it under and nothing for
    # its issue type to be scored against.
    result = await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90, sample=None)
    )
    async with session_maker() as session:
        card = await score_run(session, result.run_id, client_id=client_id)
    assert card.issue_type.judged == 1  # the typed one
    assert card.issue_type.excluded == 1  # the untyped one
    assert card.untyped_predictions["Email"] == 1


async def test_a_tenant_with_no_labels_yet_can_only_be_answered_unsure(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The closed set is the tenant's own, so before anybody has typed a ticket
    there is no label to offer and a confident answer is refused rather than
    accepted on the model's word."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id, issue_type_label=None))
        await session.commit()

    # Full, because a tenant with no labels has nothing to draw a sample from:
    # every row of this window is untyped.
    result = await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90, sample=None)
    )
    assert result.errors == 1
    async with session_maker() as session:
        card = await score_run(session, result.run_id, client_id=client_id)
    assert card.untyped_predictions["none"] == 1


async def test_another_clients_run_cannot_be_scored(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client_id, site_id = await _world(session_maker, make_client)
    other = await make_client("beta-msp", "Beta MSP")
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        await session.commit()
    result = await _run(session_maker, client_id, FakeModel([answer()]))
    async with session_maker() as session:
        with pytest.raises(ValueError, match="no shadow run"):
            await score_run(session, result.run_id, client_id=other.id)


# --- mechanics: batched, watchable, resumable ----------------------------------------------


async def test_predictions_are_committed_in_batches_and_progress_is_reported(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """A run that holds one transaction to the end cannot be watched and loses
    everything if it is interrupted. The first full replay did both."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        for number in range(1, 8):
            session.add(outcome(number, client_id=client_id, site_id=site_id))
        await session.commit()

    seen: list[tuple[int, int, int]] = []
    result = await _run(
        session_maker,
        client_id,
        FakeModel([answer()]),
        config=ReplayConfig(days=90, batch=3),
        on_progress=lambda done, total, tokens: seen.append((done, total, tokens)),
    )
    assert result.tickets_seen == 7
    # Seven tickets in batches of three: three reports, the last one short.
    assert [done for done, _, _ in seen] == [3, 6, 7]
    assert {total for _, total, _ in seen} == {7}
    assert seen[-1][2] > 0  # tokens are reported as they are spent


async def test_an_interrupted_run_carries_on_rather_than_starting_over(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """Resume by run id: the rows already committed are the work not to redo."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        for number in range(1, 6):
            session.add(outcome(number, client_id=client_id, site_id=site_id))
        await session.commit()

    # A first run that only gets through two tickets.
    first = await _run(
        session_maker,
        client_id,
        FakeModel([answer()]),
        config=ReplayConfig(days=90, limit=2, batch=2),
    )
    assert first.tickets_seen == 2

    carried = await _run(
        session_maker,
        client_id,
        FakeModel([answer()]),
        config=ReplayConfig(days=90, batch=2),
        resume=first.run_id,
    )
    assert carried.run_id == first.run_id  # the same run, not a new one
    assert carried.resumed_from == 2
    assert carried.tickets_seen == 3  # the three it had not done

    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(ShadowPrediction).where(ShadowPrediction.run_id == first.run_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 5  # one per ticket, none twice
    assert len({row.ticket_id for row in rows}) == 5


async def test_resuming_another_clients_run_is_refused(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client_id, site_id = await _world(session_maker, make_client)
    other = await make_client("beta-msp", "Beta MSP")
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        await session.commit()
    first = await _run(session_maker, client_id, FakeModel([answer()]))

    with pytest.raises(ValueError, match="no shadow run"):
        await _run(session_maker, other.id, FakeModel([answer()]), resume=first.run_id)


async def test_cached_input_is_counted_apart_from_input(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """Cached input is billed at a fraction, and a replay reads the same
    instructions once per ticket: adding the two together overstates."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        await session.commit()

    result = await _run(session_maker, client_id, FakeModel([answer()], usage=(40, 12, 700)))
    assert (result.input_tokens, result.output_tokens) == (40, 12)
    assert result.cache_read_tokens == 700
    async with session_maker() as session:
        run = await session.get(ShadowRun, result.run_id)
        row = (await session.execute(select(ShadowPrediction))).scalar_one()
    assert run is not None
    assert (run.input_tokens, run.cache_read_tokens) == (40, 700)
    assert (row.input_tokens, row.cache_read_tokens) == (40, 700)


# --- the franchisee label policy -----------------------------------------------------------


async def test_the_label_policy_is_applied_after_the_answer_and_both_are_kept(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The raw answer stays on the row, so the policy effect is a measurement
    taken from one run rather than a comparison of two."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        # Both labels have to exist at the tenant, or the one being remapped is
        # refused as out-of-set before any policy is applied.
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        session.add(
            outcome(
                2,
                client_id=client_id,
                site_id=site_id,
                issue_type_label="Printer/Scanner/Copier",
            )
        )
        await session.commit()

    result = await _run(
        session_maker,
        client_id,
        FakeModel([answer(issue_type="Printer/Scanner/Copier")]),
        policy=AutopilotConfig(issue_type_remap={"Printer/Scanner/Copier": "Email"}),
    )
    async with session_maker() as session:
        row = (
            await session.execute(select(ShadowPrediction).where(ShadowPrediction.ticket_id == "1"))
        ).scalar_one()
        run = await session.get(ShadowRun, result.run_id)
    assert row.issue_type == "Email"
    assert row.issue_type_raw == "Printer/Scanner/Copier"
    assert row.issue_type_policy == "remapped"
    assert run is not None
    assert run.config["label_policy"]["issue_type_remap"] == {"Printer/Scanner/Copier": "Email"}


async def test_a_forbidden_label_is_stored_as_a_decline(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(outcome(1, client_id=client_id, site_id=site_id))
        session.add(
            outcome(
                2,
                client_id=client_id,
                site_id=site_id,
                issue_type_label="Printer/Scanner/Copier",
            )
        )
        await session.commit()

    await _run(
        session_maker,
        client_id,
        FakeModel([answer(issue_type="Printer/Scanner/Copier")]),
        policy=AutopilotConfig(forbidden_issue_types=["Printer/Scanner/Copier"]),
    )
    async with session_maker() as session:
        row = (
            await session.execute(select(ShadowPrediction).where(ShadowPrediction.ticket_id == "1"))
        ).scalar_one()
    assert row.issue_type == "unsure"
    assert row.issue_type_raw == "Printer/Scanner/Copier"
    assert row.issue_type_policy == "forbidden"


async def test_a_run_stops_when_the_model_will_not_answer(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """Live, 2026-10-01: an account ran out of credit part-way through a replay
    and the run recorded 1,937 identical 400s, each a call nobody could have
    expected to succeed. Three in a row now end it, with everything predicted
    so far committed and the run saying why it is short."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        for number in range(1, 31):
            session.add(outcome(number, client_id=client_id, site_id=site_id))
        await session.commit()

    class Broke:
        """Answers twice, then refuses the way a dead account refuses."""

        def __init__(self) -> None:
            self.calls = 0
            self.messages = SimpleNamespace(create=self._create)

        async def _create(self, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls > 2:
                raise _BadRequest("Your credit balance is too low")
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", name=TOOL_NAME, input=answer())],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
            )

    model = Broke()
    result = await _run(session_maker, client_id, model, config=ReplayConfig(days=90, batch=5))
    assert result.stopped_early is True
    assert result.stopped_reason == "model_refused"
    # It stopped within a batch or two of the first refusal rather than
    # calling for all thirty tickets.
    assert model.calls < 12
    assert result.errors >= 3

    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(ShadowPrediction).where(ShadowPrediction.run_id == result.run_id)
                )
            )
            .scalars()
            .all()
        )
        run = await session.get(ShadowRun, result.run_id)
    # Everything it did get through is on the record, and the run says why.
    assert len(rows) == result.tickets_seen
    assert run is not None
    assert run.stopped_reason == "model_refused"


async def test_one_refused_ticket_does_not_end_a_replay(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """A single ticket can be refused on its own merits, and that is not a
    reason to abandon a replay of thousands."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        for number in range(1, 11):
            session.add(outcome(number, client_id=client_id, site_id=site_id))
        await session.commit()

    class OneBadTicket:
        def __init__(self) -> None:
            self.calls = 0
            self.messages = SimpleNamespace(create=self._create)

        async def _create(self, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 3:
                raise _BadRequest("that one subject was not acceptable")
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", name=TOOL_NAME, input=answer())],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
            )

    result = await _run(
        session_maker, client_id, OneBadTicket(), config=ReplayConfig(days=90, batch=5)
    )
    assert result.stopped_early is False
    assert result.tickets_seen == 10
    assert result.errors == 1
    assert result.by_model == 9


# --- the iteration sample ---------------------------------------------------


async def _ledger(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: uuid.UUID,
    site_id: uuid.UUID,
    *,
    count: int = 60,
) -> None:
    """Enough tickets, typed and spread, for a sample to be a part of."""
    async with session_maker() as session:
        for number in range(1, count + 1):
            session.add(
                outcome(
                    number,
                    client_id=client_id,
                    site_id=site_id,
                    issue_type_label=TYPES[number % len(TYPES)],
                    created_at_vendor=at(1 + number % 29, hour=9),
                    completed_at=at(30),
                )
            )
        await session.commit()


async def _draw(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, *, size: int = 20
) -> Any:
    """A sample small enough to be a part of the window rather than all of it."""
    async with session_maker() as session:
        return await sample_for(session, client_id, size=size, days=90, now=NOW)


async def _sample_row(session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID) -> Any:
    async with session_maker() as session:
        return (
            await session.execute(select(ShadowSample).where(ShadowSample.client_id == client_id))
        ).scalar_one()


async def _predicted(
    session_maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> set[str]:
    async with session_maker() as session:
        return set(
            (
                await session.execute(
                    select(ShadowPrediction.ticket_id).where(ShadowPrediction.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )


async def test_a_sampled_run_asks_about_the_sample_and_groups_over_the_window(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The money is in the classifying, so that is what the sample narrows.

    Grouping still walks every ticket in creation order: a group is a walk, and
    a walk with gaps in it would form different groups on a sample than on the
    window — which would make the one thing a sample is for, comparing runs,
    the thing it quietly breaks.
    """
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id)
    await _draw(session_maker, client_id, size=20)
    model = FakeModel([answer()])

    result = await _run(
        session_maker,
        client_id,
        model,
        config=ReplayConfig(days=90, sample=ITERATION, batch=10),
    )
    assert result.window == 60
    assert result.sampled == 20
    assert result.tickets_seen == 20
    assert len(model.seen) == 20, "twenty tickets, twenty model calls"

    sample = await _sample_row(session_maker, client_id)
    async with session_maker() as session:
        chosen = await ticket_ids(session, sample.id)
        run = await session.get(ShadowRun, result.run_id)
    assert await _predicted(session_maker, result.run_id) == chosen
    assert run is not None
    assert run.config["sample"] == ITERATION
    assert run.config["sample_id"] == str(sample.id), "which tickets, on the run"


async def test_the_sample_is_drawn_once_and_every_later_run_uses_it(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """Drawn again per run — even from a fixed seed — it would move the moment
    the ledger gained a row, and two runs would stop being comparable without
    anybody having touched anything."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id)
    await _draw(session_maker, client_id, size=20)

    first = await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90)
    )
    sample = await _sample_row(session_maker, client_id)
    async with session_maker() as session:
        drawn = await ticket_ids(session, sample.id)
        # The ledger moves on, as a ledger does.
        for number in range(200, 240):
            session.add(
                outcome(
                    number,
                    client_id=client_id,
                    site_id=site_id,
                    issue_type_label=TYPES[number % len(TYPES)],
                    created_at_vendor=at(2 + number % 27, hour=11),
                    completed_at=at(30),
                )
            )
        await session.commit()

    second = await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90)
    )
    async with session_maker() as session:
        samples = list(
            (await session.execute(select(ShadowSample).where(ShadowSample.client_id == client_id)))
            .scalars()
            .all()
        )
    assert len(samples) == 1, "one sample, however many runs read it"
    assert await _predicted(session_maker, second.run_id) == drawn
    assert second.sampled == first.sampled
    assert second.window == 100, "the window grew; the sample did not"


async def test_a_full_replay_is_the_explicit_one(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """`--full` is `sample=None`: every ticket in the window, no sample drawn."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id)

    result = await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90, sample=None)
    )
    assert result.sampled is None
    assert result.tickets_seen == 60
    async with session_maker() as session:
        samples = list(
            (await session.execute(select(ShadowSample).where(ShadowSample.client_id == client_id)))
            .scalars()
            .all()
        )
    assert samples == [], "a full replay draws nothing"


async def test_a_resumed_sample_run_carries_on_with_the_same_tickets(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The sample is the run's, like its model and its prompt. A resume that
    took today's flags would answer tickets the run was never about."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id)
    await _draw(session_maker, client_id, size=20)
    first = await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90)
    )

    sample = await _sample_row(session_maker, client_id)
    async with session_maker() as session:
        drawn = sorted(await ticket_ids(session, sample.id))
        # Half of the sample lost, as an interrupted run would have left it.
        await session.execute(
            delete(ShadowPrediction).where(
                ShadowPrediction.run_id == first.run_id,
                ShadowPrediction.ticket_id.in_(drawn[:10]),
            )
        )
        await session.commit()

    model = FakeModel([answer()])
    again = await _run(
        session_maker,
        client_id,
        model,
        config=ReplayConfig(days=90, sample=None),
        resume=first.run_id,
    )
    assert again.resumed_from == 10, "ten of the sample's twenty still answered"
    assert again.tickets_seen == 10, "the run's own sample, not the flags'"
    assert len(model.seen) == 10


async def test_the_sample_leaves_out_what_it_could_not_score(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """An untyped ticket has no class to be stratified under and nothing for
    its issue type to be scored against, so it is not in a sample drawn to
    measure accuracy by class. It is still in the window, and a full replay
    still answers it — which is where the "128 tickets nobody typed" figure
    keeps coming from."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id, count=30)
    async with session_maker() as session:
        for number in range(500, 510):
            session.add(
                outcome(
                    number,
                    client_id=client_id,
                    site_id=site_id,
                    issue_type_label=None,
                    created_at_vendor=at(5 + number % 20),
                    completed_at=at(30),
                )
            )
        await session.commit()

    await _draw(session_maker, client_id, size=40)
    sample = await _sample_row(session_maker, client_id)
    async with session_maker() as session:
        chosen = await ticket_ids(session, sample.id)
    assert len(chosen) == 30, "the thirty typed ones"
    assert not chosen & {str(number) for number in range(500, 510)}
    assert sample.drawn_from == 30, "the population it was drawn from, on the row"


# --- reuse ------------------------------------------------------------------


async def test_a_second_run_asks_the_model_nothing_it_has_already_asked(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The same tickets, the same model, the same prompt: no calls at all.

    This is where most of the saving is. Iterating is running the same sample
    again after changing something that is applied *after* the model answers,
    and that used to cost a full set of calls every time.
    """
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id, count=12)

    first = await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90, sample=None)
    )
    assert first.by_model == 12
    assert first.reused == 0

    model = FakeModel([answer(issue_type="Printer/Scanner/Copier")])
    second = await _run(session_maker, client_id, model, config=ReplayConfig(days=90, sample=None))
    assert model.seen == [], "nothing was asked"
    assert second.by_model == 12
    assert second.reused == 12
    assert second.input_tokens == 0
    assert second.output_tokens == 0

    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(ShadowPrediction).where(ShadowPrediction.run_id == second.run_id)
                )
            )
            .scalars()
            .all()
        )
        run = await session.get(ShadowRun, second.run_id)
    assert {row.reused_from_run_id for row in rows} == {first.run_id}
    assert all(row.issue_type == "Email" for row in rows), "yesterday's answer, not today's fake"
    assert run is not None
    assert run.reused == 12


async def test_rescoring_under_a_new_policy_costs_nothing(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """A label policy is applied after the model answers, so a run under a new
    one is yesterday's answers re-derived — and must cost no tokens at all."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id, count=10)
    await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90, sample=None)
    )

    model = FakeModel([answer()])
    remapped = await _run(
        session_maker,
        client_id,
        model,
        config=ReplayConfig(days=90, sample=None),
        policy=AutopilotConfig(issue_type_remap={"Email": "Application/Software"}),
    )
    assert model.seen == []
    assert remapped.reused == 10
    assert remapped.input_tokens == 0

    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(ShadowPrediction).where(ShadowPrediction.run_id == remapped.run_id)
                )
            )
            .scalars()
            .all()
        )
    assert {row.issue_type for row in rows} == {"Application/Software"}, "the policy did apply"
    assert {row.issue_type_raw for row in rows} == {"Email"}, "and the raw answer is still there"
    assert {row.issue_type_policy for row in rows} == {"remapped"}


async def test_the_alert_queue_variant_only_pays_for_the_alert_tickets(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The variant changes the customer line for a ticket raised into an
    alert-shaped queue and for no other, so measuring its worth costs calls for
    those tickets and nothing for the rest."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        for number in range(1, 11):
            session.add(
                outcome(
                    number,
                    client_id=client_id,
                    site_id=site_id,
                    queue_label="Alerts" if number <= 3 else "Help Desk",
                    created_at_vendor=at(1 + number),
                    completed_at=at(30),
                )
            )
        await session.commit()

    await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90, sample=None)
    )
    model = FakeModel([answer()])
    variant = await _run(
        session_maker,
        client_id,
        model,
        config=ReplayConfig(days=90, sample=None, variant="with_alert_queue"),
    )
    assert variant.reused == 7, "the seven whose input did not change"
    assert len(model.seen) == 3, "and the three alert-queue tickets, asked again"
    assert all("alert queue" in seen for seen in model.seen)


async def test_a_different_model_or_prompt_is_a_different_question(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """Reuse is four things matching, and the model is one of them: an answer
    from Sonnet is not an answer from Haiku, whatever the ticket."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id, count=6)
    await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90, sample=None)
    )

    model = FakeModel([answer()])
    other = await _run(
        session_maker,
        client_id,
        model,
        config=ReplayConfig(days=90, sample=None),
        model_name="claude-haiku-4-5-20251001",
    )
    assert other.reused == 0
    assert len(model.seen) == 6

    # And the prompt is the other one. A prompt version is bumped exactly when
    # the question changes, so an answer given under the old one is not an
    # answer to the new one however identical the ticket is.
    async with session_maker() as session:
        await session.execute(
            update(ShadowRun)
            .where(ShadowRun.client_id == client_id)
            .values(prompt_version="shadow-v0")
        )
        await session.commit()
    after = FakeModel([answer()])
    again = await _run(session_maker, client_id, after, config=ReplayConfig(days=90, sample=None))
    assert again.reused == 0
    assert len(after.seen) == 6


async def test_an_error_is_asked_again_rather_than_inherited(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """An invalid answer is a thing to try again, not a thing to pass on."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id, count=4)
    first = await _run(
        session_maker,
        client_id,
        FakeModel([{"origin": "human"}]),
        config=ReplayConfig(days=90, sample=None),
    )
    assert first.errors == 4

    model = FakeModel([answer()])
    again = await _run(session_maker, client_id, model, config=ReplayConfig(days=90, sample=None))
    assert again.reused == 0
    assert len(model.seen) == 4
    assert again.by_model == 4


# --- the batch path ---------------------------------------------------------


class FakeBatches:
    """The Message Batches API, as much of it as the replay touches.

    One batch at a time, held in memory: created in `in_progress`, moved to
    `ended` when a test says so, and answered from the requests it was given so
    a custom id that was never sent cannot come back.
    """

    def __init__(self, answers: dict[str, Any] | None = None, *, fail: set[str] | None = None):
        self.answers = answers or {}
        self.fail = fail or set()
        self.sent: list[dict[str, Any]] = []
        self.status = "in_progress"
        self.created = 0
        self.messages = SimpleNamespace(create=self._refuse, batches=self)

    async def _refuse(self, **kwargs: Any) -> Any:
        raise AssertionError("a batched replay must not send ordinary requests")

    async def create(self, *, requests: list[dict[str, Any]]) -> Any:
        self.created += 1
        self.sent = requests
        self.status = "in_progress"
        return SimpleNamespace(id=f"msgbatch_{self.created}", processing_status=self.status)

    async def retrieve(self, external_id: str) -> Any:
        return SimpleNamespace(id=external_id, processing_status=self.status)

    async def results(self, external_id: str) -> Any:
        rows = []
        for request in self.sent:
            ticket_id = request["custom_id"]
            if ticket_id in self.fail:
                rows.append(
                    SimpleNamespace(
                        custom_id=ticket_id,
                        result=SimpleNamespace(
                            type="errored", error=SimpleNamespace(type="overloaded_error")
                        ),
                    )
                )
                continue
            rows.append(
                SimpleNamespace(
                    custom_id=ticket_id,
                    result=SimpleNamespace(
                        type="succeeded",
                        message=SimpleNamespace(
                            content=[
                                SimpleNamespace(
                                    type="tool_use",
                                    name=TOOL_NAME,
                                    input=self.answers.get(ticket_id, answer()),
                                )
                            ],
                            usage=SimpleNamespace(
                                input_tokens=90,
                                output_tokens=15,
                                cache_read_input_tokens=300,
                            ),
                        ),
                    ),
                )
            )

        async def stream() -> Any:
            for row in rows:
                yield row

        return stream()


async def test_a_batched_replay_submits_and_comes_back_for_the_answers(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The run is not finished and not failed while the batch is pending, and
    the same command collects it. A batch id that lived only in a terminal
    somebody has closed is money spent with nothing to show for it."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id, count=8)
    batches = FakeBatches()

    sent = await _run(
        session_maker,
        client_id,
        batches,
        config=ReplayConfig(days=90, sample=None),
        batched=True,
    )
    assert sent.pending_batch == "msgbatch_1"
    assert sent.pending_requests == 8
    assert sent.tickets_seen == 0, "nothing is answered yet"
    async with session_maker() as session:
        run = await session.get(ShadowRun, sent.run_id)
        rows = (
            (await session.execute(select(ShadowBatch).where(ShadowBatch.run_id == sent.run_id)))
            .scalars()
            .all()
        )
    assert run is not None
    assert run.finished_at is None, "a pending run is not a finished one"
    assert [row.external_id for row in rows] == ["msgbatch_1"]
    assert rows[0].collected_at is None

    # Still cooking: the second run says so and changes nothing.
    waiting = await _run(
        session_maker,
        client_id,
        batches,
        config=ReplayConfig(days=90, sample=None),
        resume=sent.run_id,
        batched=True,
    )
    assert waiting.pending_batch == "msgbatch_1"
    assert waiting.collected == 0
    assert batches.created == 1, "nothing was sent twice"

    batches.status = "ended"
    done = await _run(
        session_maker,
        client_id,
        batches,
        config=ReplayConfig(days=90, sample=None),
        resume=sent.run_id,
        batched=True,
    )
    assert done.collected == 8
    assert done.by_model == 8
    assert done.pending_batch is None
    assert done.output_tokens == 8 * 15
    async with session_maker() as session:
        run = await session.get(ShadowRun, sent.run_id)
        rows = (
            (await session.execute(select(ShadowBatch).where(ShadowBatch.run_id == sent.run_id)))
            .scalars()
            .all()
        )
        predictions = await _predicted(session_maker, sent.run_id)
    assert run is not None
    assert run.finished_at is not None
    assert rows[0].status == "collected"
    assert rows[0].collected_at is not None
    assert len(predictions) == 8


async def test_a_batch_answer_is_read_by_the_same_rules_as_a_request(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """An invalid answer is an error row and a failed request is an error row,
    whichever way the asking was done: a batch is a cheaper way to ask, not a
    different measurement."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id, count=4)
    batches = FakeBatches(answers={"2": {"origin": "sideways"}}, fail={"3"})

    sent = await _run(
        session_maker, client_id, batches, config=ReplayConfig(days=90, sample=None), batched=True
    )
    batches.status = "ended"
    done = await _run(
        session_maker,
        client_id,
        batches,
        config=ReplayConfig(days=90, sample=None),
        resume=sent.run_id,
        batched=True,
    )
    assert done.collected == 4
    assert done.errors == 2
    assert done.by_model == 2
    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(ShadowPrediction).where(ShadowPrediction.run_id == sent.run_id)
                )
            )
            .scalars()
            .all()
        )
    by_ticket = {row.ticket_id: row for row in rows}
    assert by_ticket["2"].error_type == "InvalidPrediction"
    assert by_ticket["3"].error_type is not None
    assert "overloaded_error" in (by_ticket["3"].error_message or "")
    assert by_ticket["1"].issue_type == "Email"


async def test_a_batched_run_still_answers_from_the_rules_and_from_reuse_first(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """Both free passes happen before anything is sent, so a batch carries only
    what nothing cheaper could answer."""
    client_id, site_id = await _world(session_maker, make_client)
    await _ledger(session_maker, client_id, site_id, count=6)
    await _run(
        session_maker, client_id, FakeModel([answer()]), config=ReplayConfig(days=90, sample=None)
    )

    batches = FakeBatches()
    second = await _run(
        session_maker, client_id, batches, config=ReplayConfig(days=90, sample=None), batched=True
    )
    assert batches.created == 0, "every answer was already held"
    assert second.pending_batch is None
    assert second.reused == 6

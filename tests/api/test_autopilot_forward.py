"""The forward shadow end to end, against fixtures and never a vendor.

The vendor reads are the two injected callables `forward_pass` takes, so these
tests supply them and no HTTP is possible. What is proved: the thing is off
until somebody turns it on, a snapshot records which tools were not asked and
why, and an enrichment of another customer's machine is counted rather than
quietly dropped — that last being the condition the Level 1 gate turns on.
"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.recorder import AuditContext
from app.autopilot.shadow.forward import (
    LIVE_CONNECTORS,
    NOT_BUILT,
    DeviceAnswer,
    EnrichmentAnswer,
    NewTicket,
    NotEnabled,
    RequestBudget,
    forward_pass,
)
from app.autopilot.shadow.schema import TOOL_NAME
from app.autopilot.wiring import NOT_SET_UP, tools_not_asked
from app.models.device import Device
from app.models.enums import Interface
from app.models.shadow import ShadowEnrichment, ShadowPrediction, ShadowRun
from app.models.site import Site
from app.settings import Settings
from tests.api.conftest import Make

pytestmark = pytest.mark.usefixtures("clean")

NOW = dt.datetime(2026, 10, 1, 9, tzinfo=dt.UTC)
TYPES = ["Application/Software", "Email", "Server"]


def live_settings(**over: Any) -> Settings:
    """Settings with the live switch on, as a person would set it."""
    return Settings(autopilot_shadow_live=True, **over)


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name=TOOL_NAME,
                    input={
                        "origin": "human",
                        "origin_confidence": 0.7,
                        "issue_type": "Email",
                        "issue_type_confidence": 0.8,
                        "urgency": "normal",
                        "urgency_confidence": 0.5,
                    },
                )
            ],
            usage=SimpleNamespace(input_tokens=90, output_tokens=15, cache_read_input_tokens=0),
        )


def ticket(
    number: int,
    *,
    site_id: uuid.UUID | None,
    title: str = "Outlook will not open",
    queue: str | None = None,
    description: str | None = None,
) -> NewTicket:
    return NewTicket(
        ticket_id=str(number),
        ticket_number=f"T{number}",
        title=title,
        site_id=site_id,
        site_slug="acme-dental",
        created_at=NOW - dt.timedelta(minutes=number),
        contact=None,
        queue_label=queue,
        description=description,
    )


def snapshot(
    *,
    device_id: uuid.UUID | None,
    device_site_id: uuid.UUID | None,
    method: str = "hostname",
    not_assessed: dict[str, str] | None = None,
) -> EnrichmentAnswer:
    return EnrichmentAnswer(
        device=DeviceAnswer(
            device_id=device_id,
            device_site_id=device_site_id,
            method=method,
            confidence="high" if device_id else None,
            candidates=1 if device_id else 0,
        ),
        read_at=NOW,
        facts=[{"id": "device.pending_reboot", "source": "get_device", "value": True}],
        reads=3,
        errors=[],
        not_assessed=not_assessed or {},
    )


async def _site(
    session_maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID, slug: str
) -> uuid.UUID:
    site_id = uuid.uuid4()
    async with session_maker() as session:
        session.add(Site(id=site_id, client_id=client_id, slug=slug, name=slug.title()))
        await session.commit()
    return site_id


async def _machine(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: uuid.UUID,
    site_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    """A real device row: the snapshot's device id is a foreign key, which is
    what makes a wrong-site count auditable afterwards rather than a number
    pointing at nothing."""
    device_id = uuid.uuid4()
    async with session_maker() as session:
        session.add(
            Device(
                id=device_id,
                client_id=client_id,
                site_id=site_id,
                canonical_name=name,
                netbios_key=name.lower()[:15],
                serials=[],
                macs=[],
                node_class="WINDOWS_WORKSTATION",
                status="active",
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        await session.commit()
    return device_id


async def _pass(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: uuid.UUID,
    tickets: list[NewTicket],
    snapshots: dict[str, EnrichmentAnswer],
    *,
    settings: Settings | None = None,
    budget: RequestBudget | None = None,
    model: FakeModel | None = None,
) -> Any:
    async def source(since: dt.datetime) -> list[NewTicket]:
        return tickets

    async def enricher(item: NewTicket) -> EnrichmentAnswer:
        return snapshots[item.ticket_id]

    async with session_maker() as session:
        result = await forward_pass(
            session,
            AuditContext.new(
                actor_id="test",
                actor_user_id=None,
                interface=Interface.CLI,
                client_id=client_id,
                client_ref="acme-it",
            ),
            client_id,
            model_client=model or FakeModel(),
            settings=settings or live_settings(),
            source=source,
            enricher=enricher,
            issue_types=TYPES,
            since=NOW - dt.timedelta(hours=1),
            budget=budget,
            now=NOW,
        )
        await session.commit()
        return result


async def test_it_does_not_run_until_somebody_turns_it_on(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """`AUTOPILOT_SHADOW_LIVE` is false by default, and the default is the point:
    nothing polls a vendor because a branch got merged."""
    client = await make_client("acme-it", "Acme IT")
    assert Settings().autopilot_shadow_live is False
    with pytest.raises(NotEnabled, match="AUTOPILOT_SHADOW_LIVE"):
        await _pass(session_maker, client.id, [], {}, settings=Settings())

    async with session_maker() as session:
        runs = (await session.execute(select(ShadowRun))).scalars().all()
    assert runs == []  # not even a run row: it refused before starting


async def test_a_pass_classifies_and_snapshots_each_new_ticket(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client = await make_client("acme-it", "Acme IT")
    site_id = await _site(session_maker, client.id, "acme-dental")
    device_id = await _machine(session_maker, client.id, site_id, "TL-AUS-WS01")
    tickets = [ticket(1, site_id=site_id), ticket(2, site_id=site_id)]
    result = await _pass(
        session_maker,
        client.id,
        tickets,
        {
            "1": snapshot(device_id=device_id, device_site_id=site_id),
            "2": snapshot(device_id=None, device_site_id=None, method="none"),
        },
    )
    assert result.tickets_seen == 2
    assert result.by_model == 2
    assert result.enriched == 2
    assert result.no_device == 1
    assert result.wrong_site == 0

    async with session_maker() as session:
        predictions = (await session.execute(select(ShadowPrediction))).scalars().all()
        snapshots = (await session.execute(select(ShadowEnrichment))).scalars().all()
        run = (await session.execute(select(ShadowRun))).scalar_one()
    assert len(predictions) == 2
    assert len(snapshots) == 2
    assert run.mode == "forward"
    assert run.config["not_built"] == ["blackpoint"]
    assert "cove" in run.config["live_connectors"]


async def test_the_tools_that_were_not_asked_are_named_on_the_snapshot(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """A tool nobody asked is a gap in the evidence, not a clean bill of health —
    and the one that does not exist yet says so on every row. Cove is built, so
    a customer who has none is carried by the enrichment's own reading instead:
    "not set up for this customer" and "Desk cannot ask" are different claims."""
    client = await make_client("acme-it", "Acme IT")
    site_id = await _site(session_maker, client.id, "acme-dental")
    await _pass(
        session_maker,
        client.id,
        [ticket(1, site_id=site_id)],
        {
            "1": snapshot(
                device_id=await _machine(session_maker, client.id, site_id, "TL-AUS-WS02"),
                device_site_id=site_id,
                not_assessed={"connectsecure": "no active vendor link for this machine"},
            )
        },
    )
    async with session_maker() as session:
        row = (await session.execute(select(ShadowEnrichment))).scalar_one()
    assert row.not_assessed["connectsecure"] == "no active vendor link for this machine"
    assert row.not_assessed["blackpoint"] == "connector not built"


# --- which words a tool that was not asked gets -----------------------------


def test_a_tool_the_customer_does_not_own_says_so_rather_than_blaming_the_machine() -> None:
    """A customer who does not buy backup has no Cove link on any machine.

    Saying "no active vendor link for this machine" about one of their laptops
    is a true sentence about the wrong subject: it reads on the card as a
    finding about that laptop. The claim is about the customer, so that is what
    it says. Before Cove's connector existed the same line read "Cove
    (connector not built)", which was a sentence about us."""
    answers = tools_not_asked(
        unset={"cove"},
        device_found=True,
        held={"ninjaone", "bitdefender", "connectsecure"},
    )
    assert answers == {"cove": NOT_SET_UP}
    assert NOT_SET_UP == "not set up for this customer"


def test_not_set_up_outranks_both_the_device_answers() -> None:
    """A tool nobody could have asked is not a fact about the resolution."""
    assert tools_not_asked(unset={"cove"}, device_found=False, held=set())["cove"] == NOT_SET_UP


def test_cove_is_a_live_connector_and_not_one_of_the_unbuilt_ones() -> None:
    """The regression this pair exists for: the card said "Cove (connector not
    built)" for months after the connector landed, because the tuple it is read
    from was not moved when it did. Blackpoint is the one that is still true."""
    assert "cove" in LIVE_CONNECTORS
    assert "cove" not in NOT_BUILT
    assert NOT_BUILT == ("blackpoint",)


def test_every_tool_that_was_asked_is_left_out_of_the_line() -> None:
    answers = tools_not_asked(
        unset=set(),
        device_found=True,
        held={"ninjaone", "bitdefender", "connectsecure", "cove"},
    )
    assert answers == {}


async def test_an_enrichment_of_another_customers_machine_is_counted(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The gate condition, made countable.

    Both sites are stored, so a wrong-site match is a number afterwards rather
    than something nobody can reconstruct. It is counted and kept, not dropped:
    quietly discarding it would hide the thing a fortnight of running is meant
    to prove.
    """
    client = await make_client("acme-it", "Acme IT")
    theirs = await _site(session_maker, client.id, "acme-dental")
    somebody_elses = await _site(session_maker, client.id, "beacon-legal")
    wrong_machine = await _machine(session_maker, client.id, somebody_elses, "BL-LEGAL-WS01")
    result = await _pass(
        session_maker,
        client.id,
        [ticket(1, site_id=theirs)],
        {"1": snapshot(device_id=wrong_machine, device_site_id=somebody_elses)},
    )
    assert result.wrong_site == 1

    async with session_maker() as session:
        row = (await session.execute(select(ShadowEnrichment))).scalar_one()
    assert row.site_id == theirs
    assert row.device_site_id == somebody_elses  # both, so it can be audited


async def test_a_pass_with_no_request_budget_left_does_nothing_and_says_so(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client = await make_client("acme-it", "Acme IT")
    spent = RequestBudget(per_hour=1)
    assert spent.spend() is True  # used by something else
    result = await _pass(session_maker, client.id, [ticket(1, site_id=None)], {}, budget=spent)
    assert result.budget_exhausted is True
    assert result.tickets_seen == 0
    async with session_maker() as session:
        run = (await session.execute(select(ShadowRun))).scalar_one()
    assert run.stopped_early is True
    assert run.stopped_reason == "request_budget"


async def test_a_rule_answers_a_machine_written_subject_without_the_model(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client = await make_client("acme-it", "Acme IT")
    site_id = await _site(session_maker, client.id, "acme-dental")
    result = await _pass(
        session_maker,
        client.id,
        [ticket(1, site_id=site_id, title="Backup job failed on SRV-01")],
        {
            "1": snapshot(
                device_id=await _machine(session_maker, client.id, site_id, "SRV-01"),
                device_site_id=site_id,
            )
        },
    )
    assert result.by_rule == 1
    assert result.by_model == 0
    async with session_maker() as session:
        row = (await session.execute(select(ShadowPrediction))).scalar_one()
    assert row.path == "rule"
    assert row.rule_name == "backup_job_report"
    assert row.origin == "alert"


async def test_a_snapshot_holds_no_ticket_or_device_text(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client = await make_client("acme-it", "Acme IT")
    site_id = await _site(session_maker, client.id, "acme-dental")
    await _pass(
        session_maker,
        client.id,
        [ticket(1, site_id=site_id, title="TL-AUS-WS14 will not start")],
        {
            "1": snapshot(
                device_id=await _machine(session_maker, client.id, site_id, "TL-AUS-WS14"),
                device_site_id=site_id,
            )
        },
    )
    async with session_maker() as session:
        row = (await session.execute(select(ShadowEnrichment))).scalar_one()
        prediction = (await session.execute(select(ShadowPrediction))).scalar_one()
    written = f"{row.facts}{row.errors}{row.not_assessed}{prediction.issue_type}"
    assert "TL-AUS-WS14" not in written


# --- the two facts that make forward and replay comparable --------------------------------


async def test_the_queue_is_recorded_as_read_at_arrival(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """The one input the forward shadow can record honestly and a replay cannot.

    The ledger reads the queue at completion, by which time it says where the
    ticket was *sent* — which is why the replay withholds it. Minutes after
    arrival it says where the ticket was raised, so the forward shadow keeps it
    and a later comparison can tell the two populations apart.
    """
    client = await make_client("acme-it", "Acme IT")
    site_id = await _site(session_maker, client.id, "acme-dental")
    await _pass(
        session_maker,
        client.id,
        [ticket(1, site_id=site_id, queue="Alerts")],
        {
            "1": snapshot(
                device_id=await _machine(session_maker, client.id, site_id, "TL-AUS-WS20"),
                device_site_id=site_id,
            )
        },
    )
    async with session_maker() as session:
        row = (await session.execute(select(ShadowPrediction))).scalar_one()
    assert row.queue_label_at_arrival == "Alerts"


async def test_the_body_reaches_the_model_and_no_table(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """A ticket's words are not something these tables hold — but whether there
    were any is, because a replay never has a body and comparing the two
    populations without knowing that compares the agent to itself with twice
    the evidence."""
    client = await make_client("acme-it", "Acme IT")
    site_id = await _site(session_maker, client.id, "acme-dental")
    model = FakeModel()
    await _pass(
        session_maker,
        client.id,
        [
            ticket(
                1,
                site_id=site_id,
                description="Outlook crashes on open. Password is Hunter2! Please help.",
            )
        ],
        {
            "1": snapshot(
                device_id=await _machine(session_maker, client.id, site_id, "TL-AUS-WS21"),
                device_site_id=site_id,
            )
        },
        model=model,
    )
    prompt = model.calls[0]["messages"][0]["content"]
    assert "Outlook crashes on open" in prompt  # the body is in flight
    assert "Hunter2" not in prompt  # and scrubbed on the way, like everywhere else

    async with session_maker() as session:
        row = (await session.execute(select(ShadowPrediction))).scalar_one()
    assert row.has_description is True
    stored = f"{row.issue_type}{row.error_message}{row.group_basis}"
    assert "Outlook" not in stored


async def test_a_ticket_with_no_body_says_so_rather_than_guessing(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client = await make_client("acme-it", "Acme IT")
    site_id = await _site(session_maker, client.id, "acme-dental")
    await _pass(
        session_maker,
        client.id,
        [ticket(1, site_id=site_id)],
        {
            "1": snapshot(
                device_id=await _machine(session_maker, client.id, site_id, "TL-AUS-WS22"),
                device_site_id=site_id,
            )
        },
    )
    async with session_maker() as session:
        row = (await session.execute(select(ShadowPrediction))).scalar_one()
    assert row.has_description is False
    assert row.queue_label_at_arrival is None

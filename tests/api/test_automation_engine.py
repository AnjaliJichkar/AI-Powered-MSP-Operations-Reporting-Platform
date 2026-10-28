"""The automation engine's lifecycle and controls, driven on the practice workflow.

Everything here runs the real service and the real worker against the test
database. The practice workflow acts on the engine's own table, so a test can
look at what a run did and did not do, and the worker is called a step at a
time (`run_once`) so each rule is watched at the moment it bites.

No AI and no vendor anywhere in it: see `tests/automation/test_imports.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automation import controls, runner, service
from app.automation.practice import NAME as PRACTICE
from app.automation.states import TRANSITIONS, InvalidTransition, RunState, advance
from app.models import (
    AuditEventType,
    AutomationRun,
    AutomationStep,
    Client,
    OrganisationKind,
    PracticeRecord,
    ReportConfig,
    Role,
    User,
)
from app.models.client import ClientKind
from tests.api.conftest import Make, audit_rows

pytestmark = pytest.mark.usefixtures("clean")

Maker = async_sessionmaker[AsyncSession]
NOW = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC)
APPROVER = "Dana Whitfield"


@dataclass
class World:
    """A demo client with the practice workflow switched on, an approver on its
    list, and two Inputiv engineers: one to start a run and one to approve it."""

    client: Client
    starter: User
    approver: User
    maker: Maker
    clock: list[dt.datetime]

    def now(self) -> dt.datetime:
        return self.clock[0]

    def tick(self, **delta: int) -> None:
        self.clock[0] = self.clock[0] + dt.timedelta(**delta)

    def actor(self, user: User) -> service.Actor:
        return service.Actor(
            user_id=user.id,
            email=user.email,
            client_id=self.client.id,
            client_slug=self.client.slug,
        )

    async def worker(self) -> runner.Outcome:
        return await runner.run_once(self.maker, clock=self.now)

    async def run(self, run_id: uuid.UUID) -> AutomationRun:
        async with self.maker() as session:
            found = await session.get(AutomationRun, run_id)
            assert found is not None
            return found

    async def steps(self, run_id: uuid.UUID) -> dict[str, AutomationStep]:
        async with self.maker() as session:
            rows = (
                (
                    await session.execute(
                        select(AutomationStep)
                        .where(AutomationStep.run_id == run_id)
                        .order_by(AutomationStep.position)
                    )
                )
                .scalars()
                .all()
            )
            return {row.step_key: row for row in rows}

    async def records(self) -> list[PracticeRecord]:
        async with self.maker() as session:
            return list(
                (
                    await session.execute(
                        select(PracticeRecord).where(PracticeRecord.client_id == self.client.id)
                    )
                )
                .scalars()
                .all()
            )

    async def with_service(self, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        async with self.maker() as session:
            try:
                result = await fn(session)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


@pytest.fixture
async def world(session_maker: Maker, make_org: Make, make_user: Make) -> World:
    async with session_maker() as session:
        client = Client(slug="tailwind-practice", name="Tailwind Practice", kind=ClientKind.DEMO)
        session.add(client)
        await session.flush()
        session.add(
            ReportConfig(
                client_id=client.id,
                config={"automation": {"approvers": [{"name": APPROVER, "role": "IT manager"}]}},
                updated_by="test",
            )
        )
        await session.commit()
        await session.refresh(client)
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv")
    starter = await make_user(inputiv, "starter@inputiv.com", Role.INPUTIV_ENGINEER)
    approver = await make_user(inputiv, "approver@inputiv.com", Role.INPUTIV_ENGINEER)
    world = World(client, starter, approver, session_maker, [NOW])
    await switch_on(world)
    return world


async def switch_on(world: World, *, enabled: bool = True) -> None:
    actor = world.actor(world.starter)

    async def go(session: AsyncSession) -> None:
        await controls.set_switch(
            session,
            actor.context(),
            client_id=world.client.id,
            workflow_name=PRACTICE,
            enabled=enabled,
            who=actor.who(),
            now=world.now(),
        )

    await world.with_service(go)


async def halt(world: World, *, halted: bool, reason: str | None = "testing") -> None:
    actor = world.actor(world.starter)

    async def go(session: AsyncSession) -> None:
        await controls.set_kill_switch(
            session,
            actor.context(),
            client_id=world.client.id,
            halted=halted,
            reason=reason,
            who=actor.who(),
            now=world.now(),
        )

    await world.with_service(go)


async def draft(world: World, **inputs: Any) -> uuid.UUID:
    async def go(session: AsyncSession) -> uuid.UUID:
        run = await service.draft(
            session,
            world.actor(world.starter),
            workflow_name=PRACTICE,
            inputs=inputs,
            now=world.now(),
        )
        return run.id

    return await world.with_service(go)


async def dry_run(world: World, run_id: uuid.UUID) -> dict[str, Any]:
    async def go(session: AsyncSession) -> dict[str, Any]:
        run = await service.get_run(session, world.client.id, run_id)
        return await service.dry_run(session, world.actor(world.starter), run, now=world.now())

    return await world.with_service(go)


async def submit(world: World, run_id: uuid.UUID) -> None:
    async def go(session: AsyncSession) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.submit(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(go)


async def approve(
    world: World,
    run_id: uuid.UUID,
    *,
    by: User | None = None,
    authoriser: str = APPROVER,
    at: dt.datetime | None = None,
) -> None:
    async def go(session: AsyncSession) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.approve(
            session,
            world.actor(by or world.approver),
            run,
            authoriser_name=authoriser,
            authoriser_method="phone",
            authoriser_at=at or world.now(),
            now=world.now(),
        )

    await world.with_service(go)


async def approved_run(world: World, **inputs: Any) -> uuid.UUID:
    """Drafted, dry-run, submitted and approved: ready for the worker."""
    run_id = await draft(world, **inputs)
    assert (await dry_run(world, run_id))["ok"]
    await submit(world, run_id)
    await approve(world, run_id)
    return run_id


async def finished(world: World, run_id: uuid.UUID) -> AutomationRun:
    """Run the worker until it has nothing more to do for this run."""
    for _ in range(20):
        outcome = await world.worker()
        if outcome.result in ("idle", "completed", "failed", "paused", "undone", "undo_failed"):
            break
    return await world.run(run_id)


# --- the state machine ----------------------------------------------------------------


def test_every_valid_transition_is_allowed_and_nothing_else() -> None:
    expected = {
        "drafted": {"dry_run_ready", "cancelled"},
        "dry_run_ready": {"awaiting_approval", "drafted", "cancelled"},
        "awaiting_approval": {"approved", "drafted", "cancelled"},
        "approved": {"running", "awaiting_approval", "cancelled"},
        "running": {"completed", "failed"},
        "failed": {"running", "awaiting_approval", "partly_completed", "undone"},
        "completed": {"undone"},
        "partly_completed": {"undone"},
        "cancelled": set(),
        "undone": set(),
    }
    assert {s.value: {t.value for t in targets} for s, targets in TRANSITIONS.items()} == expected
    # Every state is in the table, so nothing can be left with no rule.
    assert set(TRANSITIONS) == set(RunState)


@pytest.mark.parametrize(
    ("current", "wanted"),
    [
        ("drafted", "running"),
        ("drafted", "approved"),
        ("approved", "completed"),
        ("running", "approved"),
        ("completed", "running"),
        ("cancelled", "drafted"),
        ("undone", "completed"),
        ("awaiting_approval", "running"),
    ],
)
def test_an_invalid_transition_is_refused_in_the_one_place(current: str, wanted: str) -> None:
    run = AutomationRun(id=uuid.uuid4(), state=current)
    with pytest.raises(InvalidTransition) as refused:
        advance(run, wanted, now=NOW)
    assert run.state == current, "a refused transition changes nothing"
    assert current in str(refused.value) and wanted in str(refused.value)


def test_advancing_stamps_the_times_that_go_with_the_state() -> None:
    run = AutomationRun(id=uuid.uuid4(), state="approved")
    advance(run, RunState.RUNNING, now=NOW)
    assert run.started_at == NOW and run.finished_at is None
    advance(run, RunState.FAILED, now=NOW)
    assert run.finished_at == NOW
    run.failure = "something"
    advance(run, RunState.RUNNING, now=NOW)  # a resume
    assert run.finished_at is None and run.failure is None
    assert run.started_at == NOW, "the first start is kept"


# --- drafting and the switch ------------------------------------------------------


async def test_the_switch_is_off_by_default_and_off_refuses_a_draft(world: World) -> None:
    await switch_on(world, enabled=False)
    with pytest.raises(service.Refused, match="switched off"):
        await draft(world)
    await switch_on(world, enabled=True)
    run_id = await draft(world)
    assert (await world.run(run_id)).state == "drafted"


async def test_the_practice_workflow_is_refused_on_a_live_client(
    world: World, session_maker: Maker
) -> None:
    async with session_maker() as session:
        live = Client(slug="real-msp", name="Real MSP", kind=ClientKind.LIVE)
        session.add(live)
        await session.commit()
        await session.refresh(live)
    actor = service.Actor(
        user_id=world.starter.id,
        email=world.starter.email,
        client_id=live.id,
        client_slug=live.slug,
    )

    async def go(session: AsyncSession) -> None:
        await controls.set_switch(
            session,
            actor.context(),
            client_id=live.id,
            workflow_name=PRACTICE,
            enabled=True,
            who=actor.who(),
            now=NOW,
        )
        with pytest.raises(service.Refused, match="demo tenant only"):
            await service.draft(session, actor, workflow_name=PRACTICE, inputs={}, now=NOW)

    await world.with_service(go)


async def test_the_dry_run_changes_nothing_and_names_what_cannot_be_undone(world: World) -> None:
    run_id = await draft(world, label="dry")
    checklist = await dry_run(world, run_id)
    assert checklist["ok"] is True
    assert [s["step"] for s in checklist["steps"]] == [
        "create_record",
        "set_status",
        "close_out",
        "write_note",
    ]
    assert checklist["not_reversible"] == ["write_note"]
    assert all(s["plan"]["summary"] for s in checklist["steps"])
    # Nothing was created: a dry run is every precondition and every plan, and no action.
    assert await world.records() == []
    assert (await world.run(run_id)).state == "dry_run_ready"
    # Every step's precondition was audited, as a dry run.
    rows = await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_STEP)
    assert [r.payload["step"] for r in rows if r.payload["action"] == "dry_run"] == [
        "create_record",
        "set_status",
        "close_out",
        "write_note",
    ]


async def test_a_failing_precondition_keeps_the_run_drafted_with_the_reason(
    world: World, session_maker: Maker
) -> None:
    run_id = await draft(world, label="taken")
    # Somebody else's record of the name this run would create.
    async with session_maker() as session:
        session.add(
            PracticeRecord(
                client_id=world.client.id,
                name=f"taken-{str(run_id)[:8]}",
                status="new",
                created_by_run_id=None,
            )
        )
        # created_by_run_id None is "ours from an earlier attempt" by the rule, so
        # give it a stranger's run id instead.
        await session.flush()
        stranger = uuid.uuid4()
        record = (await session.execute(select(PracticeRecord))).scalar_one()
        session.add(
            AutomationRun(
                id=stranger,
                client_id=world.client.id,
                workflow_name=PRACTICE,
                workflow_version=1,
                state="completed",
                inputs={},
                started_by_user_id=world.starter.id,
                started_by="x",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await session.flush()
        record.created_by_run_id = stranger
        await session.commit()
    checklist = await dry_run(world, run_id)
    assert checklist["ok"] is False
    first = checklist["steps"][0]
    assert first["ok"] is False and "another run" in first["reason"]
    assert (await world.run(run_id)).state == "drafted"


# --- approval ------------------------------------------------------------------------


async def test_an_engineer_cannot_approve_their_own_run(world: World) -> None:
    run_id = await draft(world)
    await dry_run(world, run_id)
    await submit(world, run_id)
    with pytest.raises(service.Refused, match="somebody else has to approve"):
        await approve(world, run_id, by=world.starter)
    assert (await world.run(run_id)).state == "awaiting_approval"


async def test_an_authoriser_not_on_the_clients_list_is_refused(world: World) -> None:
    run_id = await draft(world)
    await dry_run(world, run_id)
    await submit(world, run_id)
    with pytest.raises(service.Refused, match="not on this client's approver list"):
        await approve(world, run_id, authoriser="Somebody Else")
    # And the one who is, matched without fuss over case and spacing.
    await approve(world, run_id, authoriser="  dana   WHITFIELD ")
    run = await world.run(run_id)
    assert run.state == "approved"
    assert run.authoriser_name == APPROVER and run.authoriser_method == "phone"
    assert run.approved_by == "approver@inputiv.com" and run.started_by == "starter@inputiv.com"
    row = next(
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["action"] == "approved"
    )
    assert row.payload["authoriser"]["name"] == APPROVER
    assert row.payload["authoriser"]["role"] == "IT manager"
    assert row.payload["approved_by"] == "approver@inputiv.com"
    assert row.actor_user_id == world.approver.id


async def test_the_approval_time_is_the_clients_and_is_bounded_by_the_dry_run_and_now(
    world: World,
) -> None:
    """The client said yes at a time of their own, which may be earlier than
    the moment it is recorded but never later, and never before the dry run
    they said yes to. The audit row keeps both times."""
    run_id = await draft(world)
    await dry_run(world, run_id)
    await submit(world, run_id)
    dry_at = (await world.run(run_id)).dry_run_at
    assert dry_at is not None
    with pytest.raises(service.Refused, match="in the future"):
        await approve(world, run_id, at=world.now() + dt.timedelta(minutes=5))
    with pytest.raises(service.Refused, match="before the dry run"):
        await approve(world, run_id, at=dry_at - dt.timedelta(minutes=1))
    assert (await world.run(run_id)).state == "awaiting_approval"
    # A minute of clock skew is not the future.
    said_yes = world.now() + dt.timedelta(seconds=30)
    await approve(world, run_id, at=said_yes)
    run = await world.run(run_id)
    assert run.state == "approved"
    assert run.authoriser_at == said_yes and run.approved_at == world.now()
    row = next(
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["action"] == "approved"
    )
    assert row.payload["approved_at"] == said_yes.isoformat()
    assert row.payload["recorded_at"] == world.now().isoformat()


async def test_approval_needs_a_dry_run_first_and_the_switch_still_on(world: World) -> None:
    run_id = await draft(world)
    with pytest.raises(service.Refused, match="dry_run_ready"):
        await submit(world, run_id)
    await dry_run(world, run_id)
    await submit(world, run_id)
    await switch_on(world, enabled=False)
    with pytest.raises(service.Refused, match="switched off"):
        await approve(world, run_id)


# --- execution ---------------------------------------------------------------------


async def test_a_run_executes_in_order_and_every_step_is_audited(world: World) -> None:
    run_id = await approved_run(world, label="full")
    run = await finished(world, run_id)
    assert run.state == "completed"
    assert run.undo_deadline_at == NOW + dt.timedelta(hours=24)
    steps = await world.steps(run_id)
    assert [s.state for s in steps.values()] == ["done"] * 4
    assert steps["set_status"].before == {"status": "new"}
    assert steps["set_status"].after["status"] == "offboarded"
    assert steps["close_out"].before == {"status": "offboarded"}
    (record,) = await world.records()
    assert (record.name, record.status) == (f"full-{str(run_id)[:8]}", "closed")
    assert record.note and record.note.startswith("closed by practice run")

    rows = await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_STEP)
    executed = [
        (r.payload["step"], r.payload["action"]) for r in rows if r.payload["action"] != "dry_run"
    ]
    for key in ("create_record", "set_status", "close_out", "write_note"):
        assert (key, "precondition") in executed
        assert (key, "action") in executed
        assert (key, "verify") in executed
    assert executed == sorted(
        executed,
        key=lambda pair: (
            ["create_record", "set_status", "close_out", "write_note"].index(pair[0]) * 10
            + ["precondition", "action", "verify"].index(pair[1])
        ),
    )
    verify = next(
        r for r in rows if r.payload["step"] == "set_status" and r.payload["action"] == "verify"
    )
    assert (
        verify.payload["before"] == {"status": "new"}
        and verify.payload["after"]["status"] == "offboarded"
    )
    assert all(r.actor_id == runner.WORKER for r in rows if r.payload["action"] != "dry_run")
    lifecycle = [
        r.payload["action"]
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
    ]
    assert lifecycle == ["drafted", "dry_run", "submitted", "approved", "started", "completed"]


async def test_execution_stops_at_the_first_failure_and_skips_nothing(world: World) -> None:
    run_id = await approved_run(world, label="brk", fail_at="close_out")
    run = await finished(world, run_id)
    assert run.state == "failed"
    assert run.current_step == "close_out"
    assert "made to fail on attempt 1" in run.failure
    steps = await world.steps(run_id)
    assert [s.state for s in steps.values()] == ["done", "done", "failed", "pending"]
    assert steps["close_out"].attempts == 1
    # The failed step's own change was rolled back: the record is still offboarded.
    (record,) = await world.records()
    assert record.status == "offboarded" and record.note is None
    failed = next(
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_STEP)
        if r.payload["action"] == "failed"
    )
    assert failed.payload["step"] == "close_out" and failed.outcome.value == "error"
    # The phases that did run before the action raised are on the failure row.
    assert [p["phase"] for p in failed.payload["phases"]] == ["precondition", "before"]


async def test_a_failed_run_resumes_from_the_failed_step_and_repeats_nothing(world: World) -> None:
    run_id = await approved_run(world, label="rsm", fail_at="close_out")
    assert (await finished(world, run_id)).state == "failed"

    async def go(session: AsyncSession) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.resume(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(go)
    assert (await world.run(run_id)).state == "running"
    run = await finished(world, run_id)
    assert run.state == "completed"
    steps = await world.steps(run_id)
    assert [s.state for s in steps.values()] == ["done"] * 4
    assert steps["close_out"].attempts == 2, "the second attempt is the one that got through"
    assert steps["create_record"].attempts == 1 and steps["set_status"].attempts == 1
    # Completed steps were not repeated: their actions were audited exactly once.
    actions = [
        r.payload["step"]
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_STEP)
        if r.payload["action"] == "action"
    ]
    assert actions.count("create_record") == 1 and actions.count("set_status") == 1
    assert actions.count("close_out") == 1, "the failed attempt's action row went with its rollback"


def test_a_step_may_depend_only_on_an_earlier_step() -> None:
    """The definition half of the rule, refused when the workflow is written."""
    from app.automation.definitions import InvalidWorkflow, Step, Workflow
    from app.automation.practice import (
        PracticeInputs,
        create_action,
        create_plan,
        create_precondition,
        create_verify,
    )

    def step(key: str, depends_on: str | None = None) -> Step:
        return Step(
            key=key,
            title=key,
            checks="x",
            does="y",
            confirms="z",
            precondition=create_precondition,
            plan=create_plan,
            action=create_action,
            verify=create_verify,
            depends_on=depends_on,
        )

    with pytest.raises(InvalidWorkflow, match="not an earlier step"):
        Workflow(
            name="wrong",
            version=1,
            title="",
            description="",
            inputs=PracticeInputs,
            steps=(step("a", depends_on="b"), step("b")),
            undo_window=dt.timedelta(hours=1),
            allowed_on=lambda kind: None,
        )
    with pytest.raises(InvalidWorkflow, match="used twice"):
        Workflow(
            name="wrong",
            version=1,
            title="",
            description="",
            inputs=PracticeInputs,
            steps=(step("a"), step("a")),
            undo_window=dt.timedelta(hours=1),
            allowed_on=lambda kind: None,
        )


async def test_a_step_whose_dependency_is_not_done_is_refused_at_run_time(
    world: World, session_maker: Maker
) -> None:
    """The engine half. Steps run in order and stop at the first failure, so
    in the ordinary course a dependency is always done by the time its
    dependant is reached; the guard is for the other courses — a resume after
    a partial undo, a hand edit — and this puts the run into one of them."""
    run_id = await approved_run(world, label="dep")
    assert (await finished(world, run_id)).state == "completed"
    # Undo the first three steps' worth of state by hand, as a partial undo
    # leaves them, and put the last step back to pending as if somebody then
    # resumed the run: write_note depends on close_out, which is now undone.
    async with session_maker() as session:
        run = await session.get(AutomationRun, run_id)
        assert run is not None
        rows = await service.steps_of(session, run)
        rows[2].state = "undone"
        rows[3].state = "pending"
        run.state = "running"
        run.finished_at = None
        run.lease_until = None
        await session.commit()
    run = await finished(world, run_id)
    assert run.state == "failed"
    assert "write_note depends on close_out, which is undone, not done" in run.failure
    assert (await world.steps(run_id))["write_note"].state == "failed"


async def test_a_run_survives_a_worker_dying_mid_step(world: World, session_maker: Maker) -> None:
    run_id = await approved_run(world, label="crash")
    # Claim and run the first step by hand, then stop as a dying worker would:
    # the second step marked running, the lease held, nothing more.
    async with session_maker() as session:
        run = await session.get(AutomationRun, run_id)
        assert run is not None
        run.state = "running"
        run.started_at = NOW
        run.lease_until = NOW + runner.LEASE
        rows = await service.steps_of(session, run)
        rows[0].state = "done"
        rows[0].attempts = 1
        rows[1].state = "running"
        rows[1].attempts = 1
        run.current_step = "set_status"
        session.add(
            PracticeRecord(
                client_id=world.client.id,
                name=f"crash-{str(run_id)[:8]}",
                status="new",
                created_by_run_id=run_id,
            )
        )
        await session.commit()
    # While the lease stands, nothing touches it.
    assert (await world.worker()).result == "idle"
    # The lease lapses: the next worker recovers it and carries on from set_status.
    world.tick(minutes=6)
    run = await finished(world, run_id)
    assert run.state == "completed"
    steps = await world.steps(run_id)
    assert steps["set_status"].attempts == 2, "the interrupted attempt counts"
    assert steps["create_record"].attempts == 1, "the completed step was not touched"
    (record,) = await world.records()
    assert record.status == "closed"
    recovered = [
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["action"] == "recovered"
    ]
    assert len(recovered) == 1 and recovered[0].payload["from_step"] == "set_status"


async def test_a_step_run_twice_leaves_the_world_as_once_did(
    world: World, session_maker: Maker
) -> None:
    """Idempotency, keyed by run and step: the same action for the same (run,
    step) twice is one change, not two."""
    run_id = await approved_run(world, label="idem")
    assert (await finished(world, run_id)).state == "completed"
    before = [(r.name, r.status, r.note) for r in await world.records()]
    # Put step one back to pending as a recovery would, with the record already
    # there, and run the whole thing again.
    async with session_maker() as session:
        run = await session.get(AutomationRun, run_id)
        assert run is not None
        run.state = "running"
        run.lease_until = None
        run.finished_at = None
        for row in await service.steps_of(session, run):
            row.state = "pending"
        await session.commit()
    again = await finished(world, run_id)
    assert again.state == "completed", again.failure
    assert [(r.name, r.status, r.note) for r in await world.records()] == before
    assert len(await world.records()) == 1


# --- the kill switch and one-at-a-time -------------------------------------------------


async def test_the_kill_switch_pauses_a_running_run_between_steps(
    world: World, session_maker: Maker
) -> None:
    # A whole practice run is over in one worker pass, so set the scene by
    # hand: running, one step done, the switch thrown before the next.
    run_id = await approved_run(world, label="halt2")
    async with session_maker() as session:
        run = await session.get(AutomationRun, run_id)
        assert run is not None
        run.state = "running"
        run.lease_until = None
        rows = await service.steps_of(session, run)
        rows[0].state = "done"
        rows[0].attempts = 1
        session.add(
            PracticeRecord(
                client_id=world.client.id,
                name=f"halt2-{str(run_id)[:8]}",
                status="new",
                created_by_run_id=run_id,
            )
        )
        await session.commit()
    await halt(world, halted=True, reason="stop everything")
    outcome = await world.worker()
    assert outcome.result == "paused" and outcome.run_id == run_id
    run = await world.run(run_id)
    assert run.state == "running" and run.paused_at is not None
    steps = await world.steps(run_id)
    assert steps["set_status"].state == "pending", "nothing after the switch began"
    # Thrown, it also refuses a new draft.
    with pytest.raises(service.Refused, match="halted"):
        await draft(world)
    # Cleared, the next pass carries on where it stopped.
    await halt(world, halted=False)
    run = await finished(world, run_id)
    assert run.state == "completed" and run.paused_at is None
    actions = [
        r.payload["action"]
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["run_id"] == str(run_id)
    ]
    assert "paused" in actions and "unpaused" in actions
    switch = [
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_CONTROL)
        if r.payload["control"] == "kill_switch"
    ]
    assert [(r.payload["after"], r.payload["reason"]) for r in switch] == [
        (True, "stop everything"),
        (False, None),
    ]


async def test_one_run_at_a_time_per_client(world: World, session_maker: Maker) -> None:
    first = await approved_run(world, label="one")
    second = await approved_run(world, label="two")
    # Hold the first in `running` as a live worker would, and see the second wait.
    async with session_maker() as session:
        run = await session.get(AutomationRun, first)
        assert run is not None
        run.state = "running"
        run.lease_until = NOW + runner.LEASE
        await session.commit()
    assert (await world.worker()).result == "idle"
    assert (await world.run(second)).state == "approved"
    # The rule is the database's: a second running row for the client is refused outright.
    async with session_maker() as session:
        other = await session.get(AutomationRun, second)
        assert other is not None
        assert await runner._claim(session, other, now=NOW) is False
        await session.rollback()
    # The first finishes (its lease lapses and it is recovered), then the second goes.
    world.tick(minutes=6)
    assert (await finished(world, first)).state == "completed"
    assert (await finished(world, second)).state == "completed"


# --- undo --------------------------------------------------------------------------------


async def test_undo_runs_in_reverse_order_and_leaves_what_has_no_undo(world: World) -> None:
    run_id = await approved_run(world, label="back")
    assert (await finished(world, run_id)).state == "completed"

    async def go(session: AsyncSession) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.request_undo(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(go)
    run = await finished(world, run_id)
    assert run.state == "undone" and run.undo_requested_at is None
    steps = await world.steps(run_id)
    assert {k: s.state for k, s in steps.items()} == {
        "create_record": "undone",
        "set_status": "undone",
        "close_out": "undone",
        "write_note": "done",  # no undo: left as it was, and listed
    }
    assert await world.records() == [], "the record the run created is gone again"
    undos = [
        r.payload["step"]
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_STEP)
        if r.payload["action"] == "undo"
    ]
    assert undos == ["close_out", "set_status", "create_record"], "reverse order"
    undone = next(
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["action"] == "undone"
    )
    assert undone.payload["not_reversible"] == ["write_note"]


async def test_undo_is_refused_once_the_window_has_closed(world: World) -> None:
    run_id = await approved_run(world, label="late")
    assert (await finished(world, run_id)).state == "completed"
    world.tick(hours=25)

    async def go(session: AsyncSession) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        with pytest.raises(service.Refused, match="undo window closed"):
            await service.request_undo(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(go)
    assert (await world.run(run_id)).state == "completed"


async def test_undo_of_a_failed_run_puts_back_only_what_was_done(world: World) -> None:
    run_id = await approved_run(world, label="half", fail_at="close_out", fail_times=5)
    assert (await finished(world, run_id)).state == "failed"

    async def go(session: AsyncSession) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.abandon(
            session, world.actor(world.starter), run, reason="giving up", now=world.now()
        )
        assert run.state == "partly_completed"
        await service.request_undo(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(go)
    run = await finished(world, run_id)
    assert run.state == "undone"
    assert await world.records() == []
    steps = await world.steps(run_id)
    assert steps["close_out"].state == "failed" and steps["write_note"].state == "pending"


async def test_a_cancelled_run_never_reaches_the_worker(world: World) -> None:
    run_id = await draft(world)

    async def go(session: AsyncSession) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.cancel(
            session, world.actor(world.starter), run, reason="changed my mind", now=world.now()
        )

    await world.with_service(go)
    assert (await world.worker()).result == "idle"
    assert (await world.run(run_id)).state == "cancelled"
    assert await world.records() == []

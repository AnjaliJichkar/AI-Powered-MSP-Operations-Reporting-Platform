"""What starts a run without a person, and the rules that follow from it:
the four trigger kinds, one draft per ticket, both switches, the trigger
log; an undo that looks first; approvals that expire; a resume that
re-checks; stale drafts cancelled by the worker.

Same harness as the engine tests: the practice workflow on a demo client,
the real service, the real worker, the test database. No vendor is read
anywhere here — the tickets are `Arrival`s built by hand, which is exactly
what the poller hands the evaluator.
"""

# ruff: noqa: F811  (the `world` fixture is imported from the engine tests and then named
# as a parameter, which is how pytest hands it in)
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.automation import service, triggers
from app.automation.definitions import (
    _WORKFLOWS,
    InvalidWorkflow,
    Step,
    TicketRule,
    Trigger,
    Workflow,
    register,
)
from app.automation.practice import NAME as PRACTICE
from app.automation.practice import (
    PracticeInputs,
    create_action,
    create_plan,
    create_precondition,
    create_verify,
)
from app.models import AuditEventType, AutomationRun, AutomationTriggerLog, PracticeRecord
from tests.api.conftest import audit_rows
from tests.api.test_automation_engine import (
    World,
    approve,
    approved_run,
    draft,
    dry_run,
    finished,
    halt,
    submit,
    switch_on,
    world,  # noqa: F401  (the fixture)
)

pytestmark = pytest.mark.usefixtures("clean")

OFFBOARDING = triggers.Arrival(
    ticket_id="9001",
    ticket_number="T20261012.0004",
    title="Leaver: R Patel, last day Friday",
    queue="Help Desk",
    issue_type="User Access and Management",
    sub_issue_type="Offboarding",
    source="Email",
    contact="Priya Raman",
    company="Contoso Legal",
    site_slug="contoso-legal",
    created_at=dt.datetime(2026, 10, 12, 8, 55, tzinfo=dt.UTC),
)
PRINTER = triggers.Arrival(
    ticket_id="9002",
    ticket_number="T20261012.0005",
    title="Printer jammed again",
    queue="Help Desk",
    issue_type="Printer/Scanner/Copier",
    sub_issue_type="Printer",
    source="Phone",
)


async def trigger_on(
    world: World, key: str = "offboarding_ticket", *, enabled: bool = True
) -> None:
    actor = world.actor(world.starter)

    async def go(session: Any) -> None:
        await triggers.set_trigger_switch(
            session,
            actor.context(),
            client_id=world.client.id,
            workflow_name=PRACTICE,
            trigger_key=key,
            enabled=enabled,
            who=actor.who(),
            now=world.now(),
        )

    await world.with_service(go)


async def evaluate(world: World, *arrivals: triggers.Arrival) -> list[triggers.Evaluated]:
    async def go(session: Any) -> list[triggers.Evaluated]:
        return await triggers.evaluate(session, world.client.id, list(arrivals), now=world.now())

    return await world.with_service(go)


async def log_rows(world: World) -> list[AutomationTriggerLog]:
    async with world.maker() as session:
        return await triggers.recent_log(session, world.client.id)


def practice_only(found: list[triggers.Evaluated]) -> list[triggers.Evaluated]:
    return [f for f in found if f.workflow == PRACTICE]


# --- the definition -------------------------------------------------------------------


def _step(key: str) -> Step:
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
    )


def _workflow(**over: Any) -> Workflow:
    given: dict[str, Any] = dict(
        name="t",
        version=1,
        title="T",
        description="",
        inputs=PracticeInputs,
        steps=(_step("a"),),
        undo_window=dt.timedelta(hours=1),
        allowed_on=lambda kind: None,
    )
    given.update(over)
    return Workflow(**given)


def test_a_trigger_must_have_something_to_match_or_a_schedule() -> None:
    with pytest.raises(InvalidWorkflow, match="no conditions"):
        _workflow(triggers=(Trigger(key="t", kind="ticket", title="", rule=TicketRule()),))
    with pytest.raises(InvalidWorkflow, match="must name the source or the queue"):
        _workflow(
            triggers=(
                Trigger(key="a", kind="alert", title="", rule=TicketRule(title_contains="disk")),
            )
        )
    with pytest.raises(InvalidWorkflow, match="an hour or more"):
        _workflow(
            triggers=(Trigger(key="s", kind="schedule", title="", every=dt.timedelta(minutes=5)),)
        )
    with pytest.raises(InvalidWorkflow, match="kinds are"):
        _workflow(triggers=(Trigger(key="x", kind="webhook", title=""),))
    with pytest.raises(InvalidWorkflow, match="used twice"):
        _workflow(
            triggers=(
                Trigger(key="m", kind="manual", title=""),
                Trigger(key="m", kind="manual", title=""),
            )
        )
    with pytest.raises(InvalidWorkflow, match="not an input"):
        _workflow(input_sources={"nope": "ticket.number"})
    with pytest.raises(InvalidWorkflow, match="sources are"):
        _workflow(input_sources={"label": "ticket.moon"})


def test_a_step_needs_its_words() -> None:
    bare = Step(
        key="a",
        title="a",
        precondition=create_precondition,
        plan=create_plan,
        action=create_action,
        verify=create_verify,
    )
    with pytest.raises(InvalidWorkflow, match="needs words"):
        _workflow(steps=(bare,))


def test_matching_is_fixed_conditions_and_names_what_failed() -> None:
    rule = TicketRule(issue_type="user access and management", sub_issue_type="Offboarding")
    assert triggers.match(rule, OFFBOARDING) == (
        True,
        "matched on issue type 'user access and management', sub issue type 'Offboarding'",
    )
    ok, why = triggers.match(rule, PRINTER)
    assert (
        not ok and why == "issue type is 'Printer/Scanner/Copier', not 'user access and management'"
    )
    assert triggers.match(TicketRule(title_contains="LEAVER"), OFFBOARDING)[0]
    assert triggers.match(TicketRule(title_equals="leaver: r patel, last day friday"), OFFBOARDING)[
        0
    ]
    ok, why = triggers.match(TicketRule(title_equals="Leaver"), OFFBOARDING)
    assert not ok and why == "title is not 'Leaver'"
    ok, why = triggers.match(TicketRule(queue="Monitoring Alerts", source="Email"), OFFBOARDING)
    assert not ok and "queue is 'Help Desk'" in why


def test_a_client_overrides_the_rules_values_not_its_shape() -> None:
    rule = TicketRule(issue_type="User Access and Management", sub_issue_type="Offboarding")
    theirs = rule.with_overrides({"sub_issue_type": "Employee Termination", "regex": ".*"})
    assert theirs.conditions() == {
        "issue_type": "User Access and Management",
        "sub_issue_type": "Employee Termination",
    }


# --- manual --------------------------------------------------------------------------


async def test_a_workflow_without_a_manual_trigger_cannot_be_drafted_by_hand(world: World) -> None:
    register(
        _workflow(
            name="rule_only",
            triggers=(
                Trigger(
                    key="r",
                    kind="ticket",
                    title="",
                    rule=TicketRule(title_contains="x"),
                ),
            ),
        )
    )
    try:

        async def go(session: Any) -> None:
            await service.draft(
                session,
                world.actor(world.starter),
                workflow_name="rule_only",
                inputs={},
                now=world.now(),
            )

        with pytest.raises(service.Refused, match="not started by hand"):
            await world.with_service(go)
    finally:
        _WORKFLOWS.pop("rule_only")
    # The practice workflow has one, and drafts.
    run = await world.run(await draft(world))
    assert run.trigger_key is None and run.started_by == world.starter.email


# --- ticket rules ----------------------------------------------------------------------


async def test_a_ticket_rule_drafts_a_run_filled_from_the_ticket(world: World) -> None:
    await trigger_on(world)
    found = practice_only(await evaluate(world, OFFBOARDING, PRINTER))
    assert [(f.ticket and f.ticket.ticket_number, f.outcome) for f in found] == [
        ("T20261012.0004", "drafted"),
        ("T20261012.0005", "no_match"),
    ]
    assert (
        found[1].reason
        == "issue type is 'Printer/Scanner/Copier', not 'User Access and Management'"
    )
    run = await world.run(found[0].run_id or uuid.uuid4())
    assert run.state == "drafted"
    assert run.inputs["label"] == "T20261012.0004", "label comes from the ticket number"
    assert run.ticket_number == "T20261012.0004"
    assert run.started_by_user_id is None and run.started_by == "ticket rule"
    assert run.trigger_key == "offboarding_ticket"
    assert run.trigger_detail == "ticket rule on T20261012.0004"
    drafted = [
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["action"] == "drafted"
    ]
    assert len(drafted) == 1
    assert drafted[0].actor_id == "automation-trigger" and drafted[0].actor_user_id is None
    assert drafted[0].payload["trigger"] == "offboarding_ticket"
    assert drafted[0].payload["ticket_number"] == "T20261012.0004"
    # A triggered draft is a draft and nothing more: the dry run, the
    # submission and the approval are all still ahead of it.
    assert (await dry_run(world, run.id))["ok"]
    await submit(world, run.id)
    await approve(world, run.id)
    assert (await finished(world, run.id)).state == "completed"


async def test_a_ticket_that_matches_twice_gets_one_run(world: World) -> None:
    await trigger_on(world)
    first = practice_only(await evaluate(world, OFFBOARDING))
    second = practice_only(await evaluate(world, OFFBOARDING))
    assert first[0].outcome == "drafted"
    assert second[0].outcome == "skipped"
    assert "a run already exists for this ticket (drafted" in second[0].reason
    assert "one draft per ticket per workflow" in second[0].reason
    async with world.maker() as session:
        runs = (await session.execute(select(AutomationRun))).scalars().all()
    assert len(runs) == 1
    # Even once that run is over: cancelled, it still stands for the ticket.
    run_id = runs[0].id

    async def cancel(session: Any) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.cancel(session, world.actor(world.starter), run, reason=None, now=world.now())

    await world.with_service(cancel)
    third = practice_only(await evaluate(world, OFFBOARDING))
    assert third[0].outcome == "skipped" and "(cancelled" in third[0].reason


async def test_both_switches_must_be_on_for_a_trigger_to_draft(world: World) -> None:
    # The workflow is on (the fixture turns it on); the trigger is not.
    found = practice_only(await evaluate(world, OFFBOARDING))
    assert found[0].matched and found[0].outcome == "skipped"
    assert "ticket rule trigger is switched off" in found[0].reason
    # The trigger on, the workflow off.
    await trigger_on(world)
    await switch_on(world, enabled=False)
    found = practice_only(await evaluate(world, OFFBOARDING))
    assert found[0].outcome == "skipped" and "workflow is switched off" in found[0].reason
    # Both on, but halted.
    await switch_on(world, enabled=True)
    await halt(world, halted=True)
    found = practice_only(await evaluate(world, OFFBOARDING))
    assert found[0].outcome == "skipped" and "automation is halted" in found[0].reason
    # Both on, clear: drafted.
    await halt(world, halted=False)
    found = practice_only(await evaluate(world, OFFBOARDING))
    assert found[0].outcome == "drafted"
    switched = [
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_CONTROL)
        if r.payload["control"] == "trigger_switch"
    ]
    assert len(switched) == 1 and switched[0].payload["trigger"] == "offboarding_ticket"


async def test_the_practice_rule_never_drafts_on_a_live_client(
    world: World, session_maker: Any
) -> None:
    async with session_maker() as session:
        client = await session.get(type(world.client), world.client.id)
        assert client is not None
        client.kind = "live"
        await session.commit()
    await trigger_on(world)
    found = practice_only(await evaluate(world, OFFBOARDING))
    assert found[0].matched and found[0].outcome == "skipped"
    assert "demo tenant only" in found[0].reason


# --- alert rules -----------------------------------------------------------------------


async def test_an_alert_rule_is_a_ticket_rule_on_the_alerts_source(world: World) -> None:
    register(
        _workflow(
            name="disk_alert",
            allowed_on=lambda kind: None,
            triggers=(
                Trigger(
                    key="disk",
                    kind="alert",
                    title="a disk alert",
                    rule=TicketRule(source="Monitoring Alert", title_contains="disk"),
                ),
            ),
        )
    )
    try:
        actor = world.actor(world.starter)

        async def on(session: Any) -> None:
            from app.automation import controls

            await controls.set_switch(
                session,
                actor.context(),
                client_id=world.client.id,
                workflow_name="disk_alert",
                enabled=True,
                who=actor.who(),
                now=world.now(),
            )
            await triggers.set_trigger_switch(
                session,
                actor.context(),
                client_id=world.client.id,
                workflow_name="disk_alert",
                trigger_key="disk",
                enabled=True,
                who=actor.who(),
                now=world.now(),
            )

        await world.with_service(on)
        alert = triggers.Arrival(
            ticket_id="9100",
            ticket_number="T20261012.0100",
            title="[Alert] Disk C: below 10% on CON-FS-01",
            queue="Monitoring Alerts",
            source="Monitoring Alert",
        )
        by_hand = triggers.Arrival(
            ticket_id="9101",
            ticket_number="T20261012.0101",
            title="disk full on my laptop",
            source="Phone",
        )
        found = [f for f in await evaluate(world, alert, by_hand) if f.workflow == "disk_alert"]
        assert [(f.kind, f.outcome) for f in found] == [("alert", "drafted"), ("alert", "no_match")]
        assert found[1].reason == "source is 'Phone', not 'Monitoring Alert'"
        run = await world.run(found[0].run_id or uuid.uuid4())
        assert run.started_by == "alert rule"
        assert run.trigger_detail == "alert rule on T20261012.0100"
    finally:
        _WORKFLOWS.pop("disk_alert")


# --- schedules -------------------------------------------------------------------------


async def test_a_schedule_drafts_one_run_per_interval_from_the_worker(world: World) -> None:
    await trigger_on(world, "daily")
    assert (await world.worker()).result == "idle"
    async with world.maker() as session:
        runs = (await session.execute(select(AutomationRun))).scalars().all()
    assert len(runs) == 1
    run = runs[0]
    assert run.state == "drafted" and run.trigger_key == "daily"
    assert run.started_by == "schedule" and run.trigger_detail == "schedule, a daily practice run"
    assert run.inputs["label"] == "practice", "no ticket: the engineer's input takes its default"
    # Not due again until a day has passed, whatever the worker does meanwhile.
    world.tick(hours=23)
    await world.worker()
    async with world.maker() as session:
        assert len((await session.execute(select(AutomationRun))).scalars().all()) == 1
    world.tick(hours=2)
    await world.worker()
    async with world.maker() as session:
        assert len((await session.execute(select(AutomationRun))).scalars().all()) == 2
    rows = await log_rows(world)
    assert [r.outcome for r in rows if r.kind == "schedule"] == ["drafted", "drafted"]
    # The workflow switched off: due, but skipped, and the log says why.
    await switch_on(world, enabled=False)
    world.tick(hours=25)
    await world.worker()
    async with world.maker() as session:
        assert len((await session.execute(select(AutomationRun))).scalars().all()) == 2
    rows = await log_rows(world)
    assert rows[0].outcome == "skipped" and "workflow is switched off" in rows[0].reason
    # The trigger switched off: not looked at, so nothing drafted and nothing logged.
    await switch_on(world, enabled=True)
    await trigger_on(world, "daily", enabled=False)
    world.tick(hours=25)
    await world.worker()
    async with world.maker() as session:
        assert len((await session.execute(select(AutomationRun))).scalars().all()) == 2
    assert len(await log_rows(world)) == len(rows)


# --- the trigger log -------------------------------------------------------------------


async def test_the_trigger_log_keeps_the_last_evaluations_newest_first(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await trigger_on(world)
    await evaluate(world, PRINTER, OFFBOARDING)
    rows = await log_rows(world)
    practice_rows = [r for r in rows if r.workflow_name == PRACTICE]
    assert [(r.ticket_number, r.matched, r.outcome) for r in practice_rows] == [
        ("T20261012.0004", True, "drafted"),
        ("T20261012.0005", False, "no_match"),
    ]
    assert practice_rows[0].rule == {
        "issue_type": "User Access and Management",
        "sub_issue_type": "Offboarding",
    }
    assert (
        practice_rows[0].run_id is not None and practice_rows[0].trigger_key == "offboarding_ticket"
    )
    # Bounded: past the keep, the oldest go.
    monkeypatch.setattr(triggers, "LOG_KEEP", 3)
    for n in range(5):
        world.tick(minutes=1)
        await evaluate(
            world,
            triggers.Arrival(
                ticket_id=str(n), ticket_number=f"T{n}", title="x", issue_type="Email"
            ),
        )
    async with world.maker() as session:
        kept = (
            (
                await session.execute(
                    select(AutomationTriggerLog).where(
                        AutomationTriggerLog.client_id == world.client.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(kept) == 3
    assert sorted(r.ticket_number or "" for r in kept) == ["T2", "T3", "T4"]


# --- undo looks before it puts anything back ---------------------------------------------


async def test_undo_leaves_alone_what_somebody_changed_since_the_run(world: World) -> None:
    run_id = await approved_run(world, label="touched")
    assert (await finished(world, run_id)).state == "completed"
    # Somebody reopens the record by hand after the run closed it.
    async with world.maker() as session:
        record = (await session.execute(select(PracticeRecord))).scalar_one()
        record.status = "reopened"
        await session.commit()

    async def go(session: Any) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.request_undo(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(go)
    run = await finished(world, run_id)
    assert run.state == "undone"
    steps = await world.steps(run_id)
    assert {k: s.state for k, s in steps.items()} == {
        "create_record": "changed",
        "set_status": "changed",
        "close_out": "changed",
        "write_note": "done",
    }
    assert (
        steps["close_out"].error
        == "changed since the run, left alone: status: 'closed' → 'reopened'"
    )
    records = await world.records()
    assert len(records) == 1 and records[0].status == "reopened", "nothing was overwritten"
    undone = next(
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["action"] == "undone"
    )
    assert undone.payload["changed_since"] == ["create_record", "set_status", "close_out"]
    skipped = [
        r.payload["step"]
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_STEP)
        if r.payload["action"] == "undo_skipped"
    ]
    assert skipped == ["close_out", "set_status", "create_record"]


async def test_undo_still_puts_back_what_is_as_the_run_left_it(world: World) -> None:
    """The other half: a record nobody touched is undone exactly as before."""
    run_id = await approved_run(world, label="untouched")
    assert (await finished(world, run_id)).state == "completed"

    async def go(session: Any) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.request_undo(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(go)
    assert (await finished(world, run_id)).state == "undone"
    assert await world.records() == []


# --- approvals expire --------------------------------------------------------------------


async def test_an_approval_not_started_on_within_the_window_lapses(world: World) -> None:
    run_id = await approved_run(world, label="late")
    run = await world.run(run_id)
    assert run.approval_expires_at == world.now() + dt.timedelta(hours=24)
    await halt(world, halted=True)  # so the worker cannot start it meanwhile
    world.tick(hours=25)
    await halt(world, halted=False)
    assert (await world.worker()).result == "idle", "an expired approval is not started on"
    run = await world.run(run_id)
    assert run.state == "awaiting_approval"
    assert run.authoriser_name is None and run.approved_by is None
    expired = next(
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["action"] == "approval_expired"
    )
    assert expired.payload["previous_approval"]["authoriser"] == "Dana Whitfield"
    assert expired.actor_id == "automation-trigger"
    # Approved again, it runs.
    await approve(world, run_id)
    assert (await finished(world, run_id)).state == "completed"


async def test_a_late_resume_needs_a_fresh_approval(world: World) -> None:
    run_id = await approved_run(world, label="stale-fail", fail_at="close_out")
    assert (await finished(world, run_id)).state == "failed"
    world.tick(hours=25)

    async def resume(session: Any) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.resume(session, world.actor(world.starter), run, now=world.now())

    with pytest.raises(service.Refused, match="fresh approval"):
        await world.with_service(resume)

    async def resubmit(session: Any) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.resubmit(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(resubmit)
    run = await world.run(run_id)
    assert run.state == "awaiting_approval" and run.authoriser_name is None
    assert (await world.steps(run_id))["close_out"].state == "pending"
    resubmitted = next(
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["action"] == "resubmitted"
    )
    assert resubmitted.payload["previous_approval"]["authoriser"] == "Dana Whitfield"
    await approve(world, run_id)
    run = await finished(world, run_id)
    assert run.state == "completed"
    steps = await world.steps(run_id)
    assert steps["close_out"].attempts == 2 and steps["create_record"].attempts == 1


async def test_a_prompt_resume_needs_no_fresh_approval(world: World) -> None:
    run_id = await approved_run(world, label="quick-fail", fail_at="close_out")
    assert (await finished(world, run_id)).state == "failed"
    world.tick(hours=23)

    async def resume(session: Any) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.resume(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(resume)
    assert (await finished(world, run_id)).state == "completed"


# --- a resume re-checks ------------------------------------------------------------------


async def test_a_resume_re_verifies_the_dependency_before_acting(world: World) -> None:
    run_id = await approved_run(world, label="moved", fail_at="close_out")
    assert (await finished(world, run_id)).state == "failed"
    # While it was stopped, somebody put the record back to new: set_status,
    # which close_out depends on, no longer holds.
    async with world.maker() as session:
        record = (await session.execute(select(PracticeRecord))).scalar_one()
        record.status = "new"
        await session.commit()

    async def resume(session: Any) -> None:
        run = await service.get_run(session, world.client.id, run_id)
        await service.resume(session, world.actor(world.starter), run, now=world.now())

    await world.with_service(resume)
    run = await finished(world, run_id)
    assert run.state == "failed"
    assert run.failure == (
        "close_out — recheck: set_status, which close_out depends on, no longer verifies: "
        "status is new, not offboarded. The situation changed since the run stopped; look "
        "before resuming again."
    )
    records = await world.records()
    assert records[0].status == "new", "nothing acted"
    rechecks = [
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_STEP)
        if r.payload["action"] == "failed" and r.payload["phase"] == "recheck"
    ]
    assert len(rechecks) == 1 and rechecks[0].payload["phases"][0]["phase"] == "recheck"


# --- stale drafts ------------------------------------------------------------------------


async def test_drafts_nobody_approved_in_a_week_are_cancelled_by_the_worker(world: World) -> None:
    drafted = await draft(world, label="old-draft")
    waiting = await draft(world, label="old-wait")
    assert (await dry_run(world, waiting))["ok"]
    await submit(world, waiting)
    world.tick(days=6)
    fresh = await draft(world, label="fresh")
    world.tick(days=1, hours=1)
    await world.worker()
    assert (await world.run(drafted)).state == "cancelled"
    assert (await world.run(waiting)).state == "cancelled"
    assert (await world.run(fresh)).state == "drafted", "six days old is not stale"
    run = await world.run(waiting)
    assert run.failure == (
        "cancelled by the worker: awaiting approval for more than 7 days without an approval"
    )
    cancelled = [
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_RUN)
        if r.payload["action"] == "cancelled"
    ]
    assert len(cancelled) == 2
    assert {r.payload["was"] for r in cancelled} == {"drafted", "awaiting_approval"}
    assert all(r.actor_id == "automation-trigger" for r in cancelled)


# --- the poll --------------------------------------------------------------------------


async def test_polling_twice_over_the_same_tickets_drafts_once(world: World) -> None:
    """`scripts/automation.py poll` and anything else that evaluates — two
    pollers, a pass re-run after a crash — produce at most one draft per
    ticket per workflow. The reader is a fake: the poll never reads a vendor
    here, and the real one reads through `app/tickets/poller.py`."""
    await trigger_on(world)
    reads: list[dt.datetime] = []

    async def read(since: dt.datetime) -> list[triggers.Arrival]:
        reads.append(since)
        return [OFFBOARDING, PRINTER]

    first_since = world.now() - dt.timedelta(hours=24)

    async def first(session: Any) -> triggers.Polled:
        return await triggers.poll_once(
            session, world.client.id, read, since=first_since, now=world.now()
        )

    polled = await world.with_service(first)
    assert (polled.tickets, polled.evaluations, polled.matched, polled.drafted) == (2, 2, 1, 1)
    # The poll wrote down when it ran, so the next starts from there.
    async with world.maker() as session:
        assert await triggers.last_poll_at(session, world.client.id) == world.now()
    world.tick(minutes=15)

    async def second(session: Any) -> triggers.Polled:
        since = await triggers.last_poll_at(session, world.client.id)
        assert since is not None
        return await triggers.poll_once(
            session, world.client.id, read, since=since, now=world.now()
        )

    polled = await world.with_service(second)
    assert reads[1] == world.now() - dt.timedelta(minutes=15)
    assert (polled.matched, polled.drafted) == (1, 0), "matched again, drafted nothing"
    async with world.maker() as session:
        runs = (await session.execute(select(AutomationRun))).scalars().all()
    assert len(runs) == 1
    rows = await log_rows(world)
    assert rows[1].outcome == "skipped" and "a run already exists" in rows[1].reason
    polls = [
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_CONTROL)
        if r.payload["control"] == "poll"
    ]
    assert [p.payload["drafted"] for p in polls] == [1, 0]
    assert all(p.actor_id == "automation-trigger" for p in polls)

#!/usr/bin/env python
"""The automation triggers' poll: new tickets, evaluated against the rules.

    python scripts/automation.py poll --client <slug> --once
    python scripts/automation.py notes-backfill --client <slug> [--dry-run] [--limit N]
    python scripts/automation.py validate
    python scripts/automation.py try <workflow> --client <slug> [--ticket <number>]

One polling pass. It reads the tickets that arrived since the last poll
through the same Autotask reader and the same hourly request budget the
forward shadow uses (`app/tickets/poller.py`, `app/tickets/arrivals.py`),
hands them to the automation engine's ticket and alert rules
(`app.automation.triggers.poll_once`), and prints counts. It never calls the
classifier or any model: `tests/automation/test_imports.py` fails the suite
if this file, or anything it imports, reaches the agent, the autopilot or the
Anthropic client.

It is off until a person turns it on: `AUTOMATION_TRIGGERS_LIVE` (false by
default, separate from `AUTOPILOT_SHADOW_LIVE`) must be true, and even then a
rule drafts a run only when the workflow's switch and the trigger's own switch
are both on for that client. A trigger drafts and nothing more.

The same pass runs the note workflows (`app/automation/notes/`): their alert
rules over the same tickets, each match answered with one internal note
built from facts read through the connectors (`app/tickets/alert_tools.py`),
kept in our table and shown on the ticket page; posted to Autotask only when
an engineer presses Post, or at once where the franchisee's approver has
agreed to auto-posting for that workflow and the kill switch is clear. The
monthly chronic devices report is built here too, and the alert tickets'
closures are read for the measurement.

`notes-backfill` is the open alert queue, once: every open ticket, whenever
it arrived, through the same rules. `--dry-run` says which would get a note
and what it would say, and writes nothing; without it, preview notes are
built and **never posted**, whatever the workflow's mode.

`--once` is the only mode there is. Nothing polls on its own; a schedule is
this command on a cron service, shaped like the forward shadow's block in
`render.yaml` (commented out until somebody decides).

`validate` checks every workflow definition (`app/automation/definitions/`)
and prints one line per file: the problem, or OK. `try` runs one workflow's
dry run for one client with real reads and no writes — the preconditions and
plans of a run workflow, or the reads and the note of a note workflow for
one open ticket — and prints what each step would do. It refuses when the
workflow is already switched on for the client: it is for before go-live.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import uuid
from typing import Any

from sqlalchemy import select

import app.automation  # noqa: F401  loads the definitions into the registry
from app.automation import controls, library
from app.automation.definitions import StepContext, workflow
from app.automation.notes import service as notes
from app.automation.notes.definitions import NoteContext, Skip, note_workflows
from app.automation.triggers import Arrival, last_poll_at, match, poll_once
from app.db.session import dispose_engine, get_sessionmaker, session_scope
from app.models.client import Client
from app.settings import get_settings
from app.tickets.alert_tools import AlertTools, writer_for
from app.tickets.arrivals import Backoff, RequestBudget, with_backoff
from app.tickets.poller import arrivals_for, read_new_tickets

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class UnknownClient(ValueError):
    pass


async def _client(slug: str) -> Client:
    async with session_scope() as session:
        try:
            query = select(Client).where(Client.id == uuid.UUID(slug))
        except ValueError:
            query = select(Client).where(Client.slug == slug)
        client = (await session.execute(query)).scalar_one_or_none()
        if client is None:
            raise UnknownClient(f"no client {slug!r}")
        return client


def _refused() -> int:
    print(
        "AUTOMATION_TRIGGERS_LIVE is false, so the automation triggers do not read a vendor.\n"
        "Nothing was called and nothing was written. Turn it on deliberately in settings; it\n"
        "is separate from AUTOPILOT_SHADOW_LIVE, and the per-client switches still apply.",
        file=sys.stderr,
    )
    return EXIT_USAGE


def _say_wait(attempt: int, waited: float, error: Exception) -> None:
    print(
        f"  attempt {attempt} failed ({type(error).__name__}); waiting {waited:.0f}s",
        file=sys.stderr,
    )


async def _poll(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.automation_triggers_live:
        return _refused()
    if not args.once:
        print(
            "Only --once is implemented: nothing here loops, and nothing in the\n"
            "application polls on its own. A schedule is this command on a cron.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        client = await _client(args.client)
    except UnknownClient as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    now = dt.datetime.now(dt.UTC)
    budget = RequestBudget(per_hour=settings.autopilot_poll_requests_per_hour)

    async with session_scope() as session:
        last = await last_poll_at(session, client.id)
    # From the last poll, or from `--since-hours` back when this client has
    # never been polled. Never from the beginning: a rule is for a ticket
    # arriving, not for the whole queue.
    since = last or (now - dt.timedelta(hours=args.since_hours))
    print(f"reading tickets created since {since:%Y-%m-%d %H:%M} UTC")

    async def read(from_when: dt.datetime) -> list[Arrival]:
        # One listing, three picklists cached by the connector, and one
        # contacts read: counted as the forward shadow counts its own.
        if not budget.spend(2):
            raise RuntimeError("no request budget left this hour")
        tickets = await with_backoff(
            lambda: read_new_tickets(client.id, created_since=from_when),
            backoff=Backoff(),
            on_wait=_say_wait,
        )
        async with session_scope() as inner:
            return await arrivals_for(inner, client.id, tickets)

    tools = AlertTools(client.id, client.slug, get_sessionmaker())

    async def closures(ticket_ids: list[str]) -> dict[str, dt.datetime | None]:
        if not budget.spend():
            return {}
        return await tools.closures(ticket_ids)

    try:
        async with session_scope() as session:
            polled = await poll_once(
                session,
                client.id,
                read,
                since=since,
                now=now,
                tools=tools,
                writer_for=writer_for,
                closures=closures,
            )
            await session.commit()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if args.json:
        print(
            json.dumps(
                {
                    "client": client.slug,
                    "since": polled.since.isoformat(),
                    "tickets": polled.tickets,
                    "evaluations": polled.evaluations,
                    "matched": polled.matched,
                    "drafted": polled.drafted,
                    "noted": polled.noted,
                    "posted": polled.posted,
                    "reports": polled.reports,
                    "closures": polled.closures,
                    "autotask_requests": budget.spent,
                },
                indent=2,
            )
        )
        return EXIT_OK
    print(
        f"  {polled.tickets} ticket(s) read, {polled.evaluations} evaluation(s), "
        f"{polled.matched} matched, {polled.drafted} run(s) drafted, {polled.noted} note(s) "
        f"built, {polled.posted} posted, {polled.reports} report(s), {polled.closures} "
        f"closure(s) recorded"
    )
    print(f"  Autotask: {budget.spent} request(s) of {budget.per_hour} this hour")
    print("  the Automations page has the log; every draft still needs a dry run and an approval")
    return EXIT_OK


async def _notes_backfill(args: argparse.Namespace) -> int:
    """The open alert queue, once, through the note workflows' rules.

    Dry run: which open tickets would get a note and what it would say, with
    nothing written — not a note row, not a log row. Without it, preview
    notes are built and kept; nothing is posted, whatever the mode.
    """
    settings = get_settings()
    if not settings.automation_triggers_live:
        return _refused()
    try:
        client = await _client(args.client)
    except UnknownClient as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    now = dt.datetime.now(dt.UTC)
    budget = RequestBudget(per_hour=settings.autopilot_poll_requests_per_hour)
    if not budget.spend(2):
        print("no request budget left this hour", file=sys.stderr)
        return EXIT_FAILED
    try:
        tickets = await with_backoff(
            lambda: read_new_tickets(client.id, created_since=None),
            backoff=Backoff(),
            on_wait=_say_wait,
        )
        async with session_scope() as session:
            arrivals = await arrivals_for(session, client.id, tickets)
    except Exception as exc:
        print(f"Autotask did not answer: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"{len(arrivals)} open ticket(s) at Autotask")
    if args.limit is not None:
        arrivals = arrivals[: args.limit]
    tools = AlertTools(client.id, client.slug, get_sessionmaker())

    if args.dry_run:
        # The rules and the builders run; the rows do not. Ids and numbers on
        # the terminal, and the note's own words, which carry no customer
        # name a tool did not already put in a ticket title.
        would = 0
        async with session_scope() as session:
            from app.automation.notes.service import _Gate  # the same gate the poll uses

            gate = await _Gate.read(session, client.id)
            for definition in note_workflows():
                if definition.kind != "note" or definition.build is None:
                    continue
                rule = gate.rule_for(definition)
                for arrival in arrivals:
                    ok, _ = match(rule, arrival)
                    if not ok:
                        continue
                    skipped = gate.why_not(definition)
                    if skipped is not None:
                        print(f"  {arrival.label}: {definition.title} — skipped: {skipped}")
                        continue
                    existing = await notes.existing_note(
                        session, client.id, definition.name, arrival.ticket_id
                    )
                    if existing is not None:
                        print(f"  {arrival.label}: {definition.title} — already has a note")
                        continue
                    result = await definition.build(
                        NoteContext(
                            ticket=arrival,
                            tools=tools,
                            settings=gate.settings.get(definition.name, {}),
                            now=now,
                        )
                    )
                    if isinstance(result, Skip):
                        print(f"  {arrival.label}: {definition.title} — no note: {result.reason}")
                        continue
                    would += 1
                    print(f"\n  {arrival.label}: {definition.title} would get:")
                    print(f"    Desk: {result.title}")
                    for line in result.body.splitlines():
                        print(f"    {line}")
        print(f"\nwould build {would} note(s); nothing was written")
        return EXIT_OK

    async with session_scope() as session:
        found = await notes.evaluate(
            session, client.id, arrivals, tools=tools, writer_for=None, now=now
        )
        await session.commit()
    built = sum(1 for f in found if f.outcome == "noted")
    print(f"  {len(found)} evaluation(s), {built} preview note(s) built, none posted")
    print(f"  Autotask: {budget.spent} request(s) counted of {budget.per_hour} this hour")
    return EXIT_OK


def _validate(args: argparse.Namespace) -> int:
    """Every definition file, checked; one line each."""
    problems = 0
    for path, error in library.validate_all():
        if error is None:
            print(f"OK   {path.name}")
        else:
            problems += 1
            print(f"FAIL {error}")
    actions = library.action_library.actions()
    print(f"{len(actions)} action(s) in the library, {problems} problem(s)")
    return EXIT_FAILED if problems else EXIT_OK


async def _try(args: argparse.Namespace) -> int:
    """One workflow's dry run for one client: real reads, no writes."""
    try:
        client = await _client(args.client)
    except UnknownClient as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    loaded = library.find(args.workflow)
    if loaded is None:
        names = ", ".join(d.name for d in library.definitions())
        print(
            f"no workflow called {args.workflow!r} (the definitions are: {names})", file=sys.stderr
        )
        return EXIT_USAGE
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        if await controls.is_enabled(session, client.id, loaded.name):
            print(
                f"{loaded.spec.title} is switched on for {client.slug}; try is for before go-live. "
                f"Turn it off on the Automations page first.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        from app.automation.config import load_automation_config

        config = await load_automation_config(session, client.id)
        settings = config.settings_for(loaded.name)
        missing = loaded.missing_settings(settings)
        print(f"{loaded.spec.title} v{loaded.version} ({loaded.hash[:12]}) on {client.slug}")
        if missing:
            print("  needs settings: " + ", ".join(f"{m.key} ({m.type})" for m in missing))

        if loaded.kind == "run":
            return await _try_run(session, client, loaded, settings)
        return await _try_note(session, client, loaded, settings, now, args.ticket)


async def _try_run(session: Any, client: Client, loaded: Any, settings: Any) -> int:
    """The preconditions and plans, as the dry run runs them, on a run that
    is never created: a run id of its own, the inputs' defaults, and a
    rollback at the end."""
    definition = workflow(loaded.name)
    reason = definition.allowed_on(client.kind)
    if reason:
        print(f"  not on this client: {reason}")
        return EXIT_USAGE
    try:
        inputs = definition.inputs.model_validate({}).model_dump(mode="json")
    except Exception as exc:
        print(f"  the inputs have no defaults, so nothing to try with: {exc}", file=sys.stderr)
        return EXIT_USAGE
    run_id = uuid.uuid4()
    all_ok = True
    for position, step in enumerate(definition.steps, start=1):
        ctx = StepContext(
            session=session,
            run_id=run_id,
            client_id=client.id,
            step_key=step.key,
            inputs=inputs,
            settings=settings,
            attempt=1,
            dry_run=True,
        )
        check = await step.precondition(ctx)
        mark = "ok" if check.ok else "would not run"
        print(f"  {position}. {step.title} [{step.action_name or 'code'}] — {mark}")
        if check.ok:
            plan = await step.plan(ctx)
            print(f"     would: {plan.summary}")
            for change in plan.changes:
                print(f"       {change.what}: {change.before!r} -> {change.after!r}")
        else:
            all_ok = False
            print(f"     because: {check.reason}")
        if not step.reversible:
            print("     cannot be undone")
    await session.rollback()
    print("  nothing was written" if all_ok else "  a precondition failed; nothing was written")
    return EXIT_OK


async def _try_note(
    session: Any, client: Client, loaded: Any, settings: Any, now: dt.datetime, number: str | None
) -> int:
    """The reads and the note, for one open ticket, with nothing written."""
    from app.automation.notes.definitions import NoteContext, ReportContext

    tools = AlertTools(client.id, client.slug, get_sessionmaker())
    if loaded.kind == "report":
        every = loaded.note_workflow.trigger.every
        ctx: Any = ReportContext(
            tools=tools, settings=settings, now=now, period_start=now - every, period_end=now
        )
    else:
        if not number:
            print("a note workflow is tried on one ticket: --ticket <number>", file=sys.stderr)
            return EXIT_USAGE
        try:
            tickets = await read_new_tickets(client.id, created_since=None)
            arrivals = await arrivals_for(session, client.id, tickets)
        except Exception as exc:
            print(f"Autotask did not answer: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_FAILED
        arrival = next((a for a in arrivals if a.ticket_number == number), None)
        if arrival is None:
            print(f"ticket {number} is not among the client's open tickets", file=sys.stderr)
            return EXIT_USAGE
        rule = loaded.note_workflow.trigger.rule
        if rule is not None:
            ok, why = match(rule, arrival)
            print(f"  rule: {'matches' if ok else 'does not match'}{'' if ok else f' ({why})'}")
        ctx = NoteContext(ticket=arrival, tools=tools, settings=settings, now=now)
    trace = await library.trace_note(loaded.name, ctx)
    for position, step in enumerate(trace, start=1):
        if not step["ran"]:
            why = "a write; never run here" if step["why"] == "a write" else "condition not met"
            print(f"  {position}. {step['id']} [{step['action']}] — skipped: {why}")
            continue
        print(f"  {position}. {step['id']} [{step['action']}] — ran")
        outputs = step.get("outputs") or {}
        note = outputs.get("note")
        if isinstance(note, dict) and "reason" in note and "title" not in note:
            print(f"     no note: {note['reason']}")
        elif isinstance(note, dict):
            print(f"     Desk: {note.get('title')}")
            for line in str(note.get("body", "")).splitlines():
                print(f"       {line}")
        elif "report" in outputs:
            report = outputs["report"]
            print(
                f"     report: {report.get('tickets', 0)} ticket(s), "
                f"{len(report.get('devices') or [])} chronic device(s)"
            )
        else:
            keys = ", ".join(f"{k}={_short(v)}" for k, v in outputs.items() if k != "read")
            print(f"     {keys}")
    print("  nothing was written")
    return EXIT_OK


def _short(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= 80 else text[:77] + "..."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="automation", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    poll = commands.add_parser("poll", help="one polling pass of the ticket and alert rules")
    poll.add_argument("--client", required=True, help="client slug or id")
    poll.add_argument("--once", action="store_true", help="one pass; the only mode there is")
    poll.add_argument(
        "--since-hours",
        type=float,
        default=24,
        help="how far back to read when this client has never been polled (default 24)",
    )
    poll.add_argument("--json", action="store_true")
    poll.set_defaults(func=_poll)
    backfill = commands.add_parser(
        "notes-backfill", help="the open alert queue, once, through the note workflows"
    )
    backfill.add_argument("--client", required=True, help="client slug or id")
    backfill.add_argument(
        "--dry-run", action="store_true", help="say which tickets would get a note and what"
    )
    backfill.add_argument("--limit", type=int, default=None, help="at most this many tickets")
    backfill.set_defaults(func=_notes_backfill)
    validate = commands.add_parser("validate", help="check every workflow definition")
    validate.set_defaults(func=_validate_async)
    trying = commands.add_parser(
        "try", help="one workflow's dry run on one client: real reads, no writes"
    )
    trying.add_argument("workflow", help="the definition's name")
    trying.add_argument("--client", required=True, help="client slug or id")
    trying.add_argument("--ticket", default=None, help="the open ticket to try a note workflow on")
    trying.set_defaults(func=_try)
    return parser


async def _validate_async(args: argparse.Namespace) -> int:
    return _validate(args)


async def _main(args: argparse.Namespace) -> int:
    try:
        result: int = await args.func(args)
        return result
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())

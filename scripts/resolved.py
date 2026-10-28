"""The per-outcome service from the terminal: the ledger, the baselines, the cost.

    python scripts/resolved.py ledger    --client <slug> [--days 90]
    python scripts/resolved.py baseline  --client <slug> [--days 90]
    python scripts/resolved.py breaches  --client <slug>

`ledger` fills `ticket_outcomes` for the tickets that completed in the window —
the same thing the daily collector does, run by hand for a backfill. It is an
upsert, so running it twice changes nothing and a wider window only re-measures.

`baseline` answers what a franchisee's service looks like: median and 90th
percentile working minutes to pickup and to resolve by class, and cost per
ticket by class. Read from Autotask rather than from the ledger, so a range from
before the ledger existed still answers.

`breaches` is the open queue as the SLA calculation sees it now, grouped by
status label — the before-and-after check when a franchisee's targets change.

`--verbose` logs every Autotask request as it is made: the entity, and the
shape of its filter — the operators, the field names and how many values each
one carries. Never the values: those are ticket and company ids.

**Counts only.** No customer, site, device, contact or engineer name is printed
by any of these (CLAUDE.md, working conventions): they report numbers, class
names, states and status labels, which are the tenant's own picklist words and
not anybody's name.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
import uuid
from collections import Counter
from dataclasses import asdict
from typing import Any, cast

from sqlalchemy import func, select

from app.audit.recorder import AuditContext
from app.connectors.autotask.client import AutotaskClient
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for
from app.db.session import session_scope
from app.models.client import Client
from app.models.enums import ConnectorName, Interface
from app.models.outcome import TicketOutcome
from app.reports.tbr.config import load_config
from app.resolved.baselines import baselines
from app.resolved.clock import franchisee_zone
from app.resolved.ledger import collect_outcomes
from app.resolved.sla import SlaCalculator

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


class UnknownClient(ValueError):
    """No client of that slug or id."""


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


def _context(client: Client, actor: str) -> AuditContext:
    return AuditContext.new(
        actor_id=actor,
        actor_user_id=None,
        interface=Interface.CLI,
        client_id=client.id,
        client_ref=client.slug,
    )


# --- the ledger -------------------------------------------------------------------------


async def _ledger(args: argparse.Namespace) -> int:
    client = await _client(args.client)
    run = await collect_outcomes(client.id, actor=args.user, days=args.days)
    async with session_scope() as session:
        held = (
            await session.execute(
                select(func.count())
                .select_from(TicketOutcome)
                .where(TicketOutcome.client_id == client.id)
            )
        ).scalar_one()
    if args.json:
        print(json.dumps({**asdict(run), "rows_held": held}, indent=2, default=str))
    else:
        print(
            f"ledger: {run.rows_written} row(s) written over {run.window_days} day(s); "
            f"{run.completed_seen} completed, {run.reopened} newly reopened, "
            f"{run.time_entries} time entries, {run.costed} costed; "
            f"{held} row(s) held in all" + ("; a listing hit its page cap" if run.truncated else "")
        )
    return EXIT_OK


# --- the baselines ------------------------------------------------------------------------


async def _baseline(args: argparse.Namespace) -> int:
    client = await _client(args.client)
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        config = await load_config(session, client.id, client_name=client.name)
        zone, zone_name = franchisee_zone(config.business_hours.timezone)
        result = await baselines(
            session,
            _context(client, args.user),
            client.id,
            config=config,
            zone=zone,
            since=now - dt.timedelta(days=args.days),
            until=now,
        )
        await session.commit()
    payload = result.as_json(currency=config.resolved.cost.currency_symbol)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    print(
        f"baseline over {args.days} day(s), days counted in {zone_name}: "
        f"{payload['tickets']} completed ticket(s)"
        + ("; a listing hit its page cap" if payload["truncated"] else "")
    )
    if not payload["has_cost_book"]:
        print("  no cost book is set for this franchisee, so no cost is reported")
    for row in payload["by_class"]:
        pickup, resolve = row["pickup_minutes"], row["resolve_minutes"]
        print(
            f"  {row['class']}: {row['tickets']} ticket(s)"
            f" | pickup median {pickup['median']} p90 {pickup['p90']} min"
            f" (of {pickup['measured']})"
            f" | resolve median {resolve['median']} p90 {resolve['p90']} min"
            f" (of {resolve['measured']})"
            f" | {row['hours_per_ticket']} h/ticket over {row['tickets_with_time']}"
            f" | cost/ticket {row['cost_per_ticket']} over {row['tickets_with_cost']}"
        )
    return EXIT_OK


# --- the open queue, as the clocks see it now -----------------------------------------------


async def _breaches(args: argparse.Namespace) -> int:
    """Open tickets by status label, and what each clock says about them."""
    client = await _client(args.client)
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        config = await load_config(session, client.id, client_name=client.name)
        zone, _ = franchisee_zone(config.business_hours.timezone)
        calculator = SlaCalculator(config.resolved, config.business_hours, zone)

        async with connector_for(client.id, ConnectorName.AUTOTASK) as built:
            connector = cast(AutotaskClient, built)
            rows, truncated = await connector.list_tickets(open_only=True)
            labelled: list[tuple[str | None, str | None, Any]] = []
            for _, ticket in rows:
                labelled.append(
                    (
                        await connector.label_for("Tickets", "status", ticket.status),
                        await connector.label_for("Tickets", "priority", ticket.priority),
                        ticket,
                    )
                )

    # What the old calculation said: any due time Autotask populated, in the past.
    def was_breached(ticket: Any) -> bool:
        clocks = [
            (ticket.due_date_time, True),
            (ticket.first_response_due, ticket.first_response_at is None),
            (ticket.resolved_due, ticket.resolved_at is None),
        ]
        return any(due is not None and live and due < now for due, live in clocks)

    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    states: Counter[str] = Counter()
    for status, priority, ticket in labelled:
        name = status or "no status"
        if was_breached(ticket):
            before[name] += 1
        verdict = calculator.assess(ticket, priority_label=priority, status_label=status, now=now)
        states[verdict.state] += 1
        if verdict.state == "breached":
            after[name] += 1

    payload = {
        "open_tickets": len(rows),
        "truncated": truncated,
        "breached_before_by_status": dict(before.most_common()),
        "breached_before_total": sum(before.values()),
        "breached_after_by_status": dict(after.most_common()),
        "breached_after_total": sum(after.values()),
        "states_after": dict(states.most_common()),
        "targets_configured": config.resolved.configured,
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    print(f"open tickets: {payload['open_tickets']}" + ("; truncated" if truncated else ""))
    print(f"targets configured for this franchisee: {payload['targets_configured']}")
    print(
        f"\nbreached, old rule (any due time Autotask populated, in the past): "
        f"{payload['breached_before_total']}"
    )
    for status, count in payload["breached_before_by_status"].items():
        print(f"  {status}: {count}")
    print(
        f"\nbreached, new rule (the franchisee's targets on their business hours): "
        f"{payload['breached_after_total']}"
    )
    for status, count in payload["breached_after_by_status"].items():
        print(f"  {status}: {count}")
    print(f"\nevery state, new rule: {payload['states_after']}")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["ledger", "baseline", "breaches"])
    parser.add_argument("--client", required=True, help="client slug or UUID")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--user", default="resolved-cli", help="who to attribute the reads to")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log every Autotask request: the entity and the shape of its filter (operators, "
        "field names and how many values each holds), never the values themselves. What to "
        "reach for when the vendor refuses a query and says only which status code it was",
    )
    return parser


def _logging(verbose: bool) -> None:
    """Turn the connector up, and nothing else: an httpx DEBUG log would carry
    request bodies, and a filter's values are in the body."""
    if not verbose:
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("app.connectors").setLevel(logging.DEBUG)


async def _main(args: argparse.Namespace) -> int:
    _logging(args.verbose)
    try:
        if args.command == "ledger":
            return await _ledger(args)
        if args.command == "baseline":
            return await _baseline(args)
        return await _breaches(args)
    except UnknownClient as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except ConnectorError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


def main() -> int:
    return asyncio.run(_main(_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())

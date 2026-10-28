#!/usr/bin/env python
"""The daily collector: pull one kind of vendor event for every mapped site of a client.

    python scripts/collect_events.py --client acme-it
    python scripts/collect_events.py --client acme-it --days 7
    python scripts/collect_events.py --client acme-it --connector bitdefender --json

Reads the window ending now and starting `--days` back (default 1) plus a day of
overlap, site by site, and writes each event into `collected_events` by its
vendor id: run it twice and nothing is duplicated. Every window asked for is
recorded in `collection_coverage`, success or failure, so a report can tell a
quiet day from a day that was never collected. A site that fails does not stop
the run; the exit status is 1 if any did.

After the events it takes the day's coverage snapshot for the client
(`app/coverage/snapshots.py`): the board — which managed devices each tool
covers — once a day, so the Coverage page can show a trend and what is new
since yesterday. One row per day, replaced on a rerun. `--no-coverage` skips
it; `--coverage-only` takes nothing but it. Before the snapshot it runs the
day's device resolution (`app/devices/resolve.py`): the same join, written
down as devices and vendor links, idempotent on a rerun; `--no-devices`
skips it. The resolution writes whether or not `DEVICE_IDENTITY_ENABLED` is
on; the flag decides who reads the result. Last, the agreement
(`app/devices/agreement.py`): the board computed through the join and through
the links, per-site counts compared, the answer kept for the Devices page and
printed here, differences and all.

Schedule it once a day per client (cron, a scheduled task). It reads only
(hard rule 2) and every site's read is an `audit_log` row (hard rule 4).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict
from typing import Any

from sqlalchemy import select

from app.connectors.errors import ConnectorError, ConnectorNotConfigured, UnknownClient
from app.coverage.exclusions import as_record, list_exclusions
from app.coverage.snapshots import SnapshotResult, take_snapshot
from app.db.session import dispose_engine, session_scope
from app.devices.agreement import agreement_view, run_agreement
from app.devices.resolve import ResolutionResult, resolve_devices
from app.events.collector import READERS, collect
from app.models import Client
from app.models.enums import ConnectorName
from app.resolved.ledger import LedgerRun, collect_outcomes

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect vendor events into collected_events, idempotently.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    clients = parser.add_mutually_exclusive_group(required=True)
    clients.add_argument("--client", help="client slug or UUID")
    clients.add_argument(
        "--all-clients",
        action="store_true",
        help="every active client, one after another. A client that fails is reported and "
        "the run carries on; the exit code says whether any did.",
    )
    parser.add_argument(
        "--connector",
        choices=sorted({c.value for c, _ in READERS}),
        default=ConnectorName.BITDEFENDER.value,
    )
    parser.add_argument("--kind", choices=sorted({k for _, k in READERS}), default="detection")
    parser.add_argument("--days", type=int, default=1, help="window length; default 1")
    parser.add_argument(
        "--user",
        default=os.environ.get("USERNAME") or os.environ.get("USER") or "collector",
        help="the handle recorded on the coverage rows and in audit_log",
    )
    parser.add_argument(
        "--no-coverage", action="store_true", help="skip the day's coverage snapshot"
    )
    parser.add_argument(
        "--coverage-only", action="store_true", help="take the coverage snapshot and nothing else"
    )
    parser.add_argument(
        "--no-devices", action="store_true", help="skip the day's device resolution"
    )
    parser.add_argument("--no-outcomes", action="store_true", help="skip the outcome ledger")
    parser.add_argument(
        "--outcome-days",
        type=int,
        default=7,
        help="how far back the outcome ledger re-measures; default 7. The write is an upsert, "
        "so a wider window costs row updates and changes nothing.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


async def _active_clients() -> list[tuple[uuid.UUID, str]]:
    """Every active client, oldest first, as `(id, slug)`.

    Oldest first so the order is the same on every run: a scheduled job that
    reorders its own work makes two mornings' logs impossible to compare.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Client.id, Client.slug)
                .where(Client.is_active.is_(True))
                .order_by(Client.created_at, Client.slug)
            )
        ).all()
    return [(row[0], row[1]) for row in rows]


async def _client_id(reference: str) -> uuid.UUID:
    async with session_scope() as session:
        try:
            parsed: uuid.UUID | None = uuid.UUID(reference)
        except ValueError:
            parsed = None
        query = (
            select(Client).where(Client.id == parsed)
            if parsed is not None
            else select(Client).where(Client.slug == reference)
        )
        client = (await session.execute(query)).scalar_one_or_none()
        if client is None:
            raise SystemExit(f"No client matches {reference!r}.")
        return client.id


async def _devices(client_id: uuid.UUID, args: argparse.Namespace) -> ResolutionResult | None:
    """The day's device resolution. A failure here is reported, never a stack trace."""
    if args.no_devices:
        return None
    try:
        return await resolve_devices(client_id, actor=args.user)
    except ConnectorNotConfigured as exc:
        print(f"device resolution skipped: {exc}", file=sys.stderr)
        return None


def _print_devices(result: ResolutionResult | None) -> None:
    if result is None:
        return
    by_pass = ", ".join(f"{k} {v}" for k, v in sorted(result.links_created.items())) or "none"
    print(
        f"device resolution: {result.devices_created} device(s) created, links created by pass: "
        f"{by_pass}; {result.links_seen} link(s) seen, {result.links_moved} moved, "
        f"{result.links_deactivated} deactivated; "
        f"{result.unresolved_open} unresolved ({result.unresolved_new} new); "
        f"{result.rmm_stale} rmm_stale, {result.retired} retired; {result.duration_ms} ms"
    )


async def _snapshot(client_id: uuid.UUID, args: argparse.Namespace) -> SnapshotResult | None:
    """The day's coverage snapshot. A failure here is reported, never a stack trace."""
    if args.no_coverage:
        return None
    try:
        return await take_snapshot(client_id, actor=args.user)
    except ConnectorNotConfigured as exc:
        print(f"coverage snapshot skipped: {exc}", file=sys.stderr)
        return None


async def _outcomes(client_id: uuid.UUID, args: argparse.Namespace) -> LedgerRun | None:
    """The outcome ledger for the tickets that closed lately.

    Last, because it is the only step that reads the ticket workspaces, and
    those are written by people during the day. A failure is reported, never a
    stack trace: a franchisee with no Autotask credential is a skip, not a
    broken collector run."""
    if args.no_outcomes:
        return None
    try:
        return await collect_outcomes(client_id, actor=args.user, days=args.outcome_days)
    except ConnectorNotConfigured as exc:
        print(f"outcome ledger skipped: {exc}", file=sys.stderr)
        return None
    except ConnectorError as exc:
        print(f"outcome ledger failed: {exc}", file=sys.stderr)
        return None


def _print_outcomes(run: LedgerRun | None) -> None:
    if run is None:
        return
    print(
        f"outcome ledger: {run.rows_written} row(s) written over {run.window_days} day(s), "
        f"{run.completed_seen} completed, {run.reopened} newly reopened, "
        f"{run.time_entries} time entries, {run.costed} costed"
        + (" (a listing hit its page cap)" if run.truncated else "")
    )


async def _agreement(client_id: uuid.UUID, args: argparse.Namespace) -> dict[str, Any] | None:
    """After the resolution and the snapshot: the board both ways, compared and kept.

    Runs whatever the flag says, because the flag decides who reads the links,
    and this is what says whether they may be read at all. A difference is
    stored and printed, never reconciled."""
    if args.no_devices or args.no_coverage:
        return None
    try:
        async with session_scope() as session:
            exclusions = [as_record(row) for row in await list_exclusions(session, client_id)]
            row = await run_agreement(session, client_id, exclusions=exclusions, actor=args.user)
            return agreement_view(row)
    except ConnectorNotConfigured as exc:
        print(f"device agreement skipped: {exc}", file=sys.stderr)
        return None


def _print_agreement(view: dict[str, Any] | None) -> None:
    if view is None:
        return
    if view["agree"]:
        print(f"device agreement: {view['sites_compared']} sites identical, flag off and on")
        return
    print(
        f"device agreement: DIFFER on {len(view['sites_differing'])} of "
        f"{view['sites_compared']} sites ({len(view['differences'])} counts)"
    )
    for entry in view["differences"][:40]:
        print(f"  {entry['site_slug']}.{entry['field']}: off {entry['off']} / on {entry['on']}")


def _print_snapshot(snapshot: SnapshotResult | None) -> None:
    if snapshot is None:
        return
    shares = ", ".join(
        f"{tool} {'n/a' if share is None else f'{share:g}%'}"
        for tool, share in snapshot.tools.items()
    )
    print(
        f"coverage snapshot {snapshot.taken_on}{' (replaced)' if snapshot.replaced else ''}: "
        f"{snapshot.managed} managed, {snapshot.unmanaged} unmanaged; {shares}; "
        f"{snapshot.duration_ms} ms"
    )


async def _main(args: argparse.Namespace) -> int:
    """One client, or every active one.

    The whole day's work for a client is one call of `_one`. A client that
    raises is reported with its slug and the run carries on to the next:
    a franchisee whose vendor is down for an hour must not cost every other
    franchisee their snapshot, their ledger and their day in the trend.
    """
    if not args.all_clients:
        return await _one(await _client_id(args.client), args)

    clients = await _active_clients()
    if not clients:
        print("No active clients.", file=sys.stderr)
        return EXIT_OK
    print(f"{len(clients)} active client(s)")
    failed: list[str] = []
    for client_id, slug in clients:
        print(f"\n=== {slug}")
        try:
            if await _one(client_id, args) != EXIT_OK:
                failed.append(slug)
        except Exception as exc:  # one client's failure is not the run's
            logging.getLogger(__name__).exception("collector failed for client=%s", slug)
            print(f"{slug}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed.append(slug)
    if failed:
        print(f"\n{len(failed)} of {len(clients)} client(s) failed: {', '.join(failed)}")
    return EXIT_FAILED if failed else EXIT_OK


async def _one(client_id: uuid.UUID, args: argparse.Namespace) -> int:
    try:
        if args.coverage_only:
            resolved = await _devices(client_id, args)
            snapshot = await _snapshot(client_id, args)
            agreement = await _agreement(client_id, args)
            outcomes = await _outcomes(client_id, args)
            if args.json:
                print(
                    json.dumps(
                        {
                            "devices": resolved.as_dict() if resolved else None,
                            "coverage": snapshot.as_dict() if snapshot else None,
                            "agreement": agreement,
                            "outcomes": asdict(outcomes) if outcomes else None,
                        },
                        indent=2,
                        default=str,
                    )
                )
            else:
                _print_devices(resolved)
                _print_snapshot(snapshot)
                _print_agreement(agreement)
                _print_outcomes(outcomes)
            return EXIT_OK if snapshot else EXIT_FAILED
        result = await collect(
            client_id,
            connector=ConnectorName(args.connector),
            kind=args.kind,
            actor=args.user,
            days=args.days,
        )
    except (UnknownClient, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    resolved = await _devices(client_id, args)
    snapshot = await _snapshot(client_id, args)
    agreement = await _agreement(client_id, args)
    outcomes = await _outcomes(client_id, args)
    if args.json:
        print(
            json.dumps(
                {
                    **result.as_dict(),
                    "devices": resolved.as_dict() if resolved else None,
                    "coverage": snapshot.as_dict() if snapshot else None,
                    "agreement": agreement,
                    "outcomes": asdict(outcomes) if outcomes else None,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(
            f"{result.connector.value} {result.kind}: "
            f"{result.window_start:%Y-%m-%d %H:%M} to {result.window_end:%Y-%m-%d %H:%M} UTC"
        )
        for outcome in result.outcomes:
            line = f"  {outcome.status:<9} {outcome.site_slug:<40}"
            if outcome.status == "collected":
                line += f" {outcome.seen} seen, {outcome.new} new"
            elif outcome.detail:
                line += f" {outcome.detail}"
            print(line)
        print(
            f"\n{len(result.collected)} collected, {len(result.failed)} failed, "
            f"{len(result.skipped)} skipped; {sum(o.new for o in result.outcomes)} new events."
        )
        _print_devices(resolved)
        _print_snapshot(snapshot)
        _print_agreement(agreement)
    return EXIT_FAILED if result.failed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except SystemExit as error:
        if isinstance(error.code, int):
            raise
        print(str(error), file=sys.stderr)
        return EXIT_USAGE


async def _run(args: argparse.Namespace) -> int:
    try:
        return await _main(args)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(main())

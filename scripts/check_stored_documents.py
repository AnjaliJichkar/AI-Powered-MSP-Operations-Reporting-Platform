#!/usr/bin/env python
"""Load every stored business review through `ReviewDocument` and say which cannot be.

    python scripts/check_stored_documents.py
    python scripts/check_stored_documents.py --client acme-it
    python scripts/check_stored_documents.py --include-superseded

A review is stored as JSON and read back through the Pydantic model; a field
whose type or meaning changes between releases is only found out when a row is
read. Run this after a deploy that touched `app/reports/tbr/model.py`: it reads
every current review (superseded rows too with `--include-superseded`), reports
how many load, and for each that does not prints its id, its client, its status
and the model's error locations, never the document itself. Exit status 1 when
any row fails, so it can gate a rollout.

Nothing is written. Needs database credentials, like every script here.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

from pydantic import ValidationError
from sqlalchemy import select

from app.db.session import dispose_engine, session_scope
from app.models import Client
from app.models.report import TbrReport
from app.reports.tbr.model import ReviewDocument

EXIT_OK, EXIT_FAILURES, EXIT_USAGE = 0, 1, 2


def _error_locations(error: ValidationError) -> list[str]:
    """`field.subfield` per problem, with the message: a shape, never a value."""
    return sorted(
        {
            ".".join(str(part) for part in problem["loc"]) + f": {problem['type']}"
            for problem in error.errors()
        }
    )


async def main(client_ref: str | None, include_superseded: bool) -> int:
    async with session_scope() as session:
        query = select(TbrReport.id, TbrReport.client_id, TbrReport.status, TbrReport.document)
        if client_ref:
            client = (
                await session.execute(select(Client).where(Client.slug == client_ref))
            ).scalar_one_or_none()
            if client is None:
                print(f"no client with slug {client_ref!r}", file=sys.stderr)
                return EXIT_USAGE
            query = query.where(TbrReport.client_id == client.id)
        if not include_superseded:
            query = query.where(TbrReport.status != "superseded")
        rows = (await session.execute(query.order_by(TbrReport.created_at))).all()
        slugs = dict(
            (await session.execute(select(Client.id, Client.slug))).all()  # ids to slugs, once
        )

    loaded = 0
    failures: list[tuple[str, str, str, list[str]]] = []
    by_status: Counter[str] = Counter()
    for report_id, client_id, status, document in rows:
        by_status[status] += 1
        try:
            ReviewDocument.model_validate(document)
            loaded += 1
        except ValidationError as error:
            failures.append(
                (str(report_id), slugs.get(client_id, "?"), status, _error_locations(error))
            )
        except Exception as error:  # a document that is not even a JSON object
            failures.append(
                (str(report_id), slugs.get(client_id, "?"), status, [type(error).__name__])
            )

    statuses = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items())) or "none"
    print(f"stored reviews checked: {len(rows)} ({statuses})")
    print(f"load: {loaded}")
    print(f"fail: {len(failures)}")
    for report_id, slug, status, where in failures:
        print(f"  {report_id}  client={slug}  status={status}")
        for problem in where:
            print(f"    {problem}")
    return EXIT_FAILURES if failures else EXIT_OK


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--client", help="one client's reviews only, by slug")
    parser.add_argument(
        "--include-superseded",
        action="store_true",
        help="check superseded rows too (they are kept as history and read as baselines)",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = _parse(argv)

    async def _go() -> int:
        try:
            return await main(args.client, args.include_superseded)
        finally:
            await dispose_engine()

    return asyncio.run(_go())


if __name__ == "__main__":
    sys.exit(run())

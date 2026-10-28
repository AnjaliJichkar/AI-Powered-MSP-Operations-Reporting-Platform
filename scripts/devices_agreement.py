#!/usr/bin/env python
"""Do the join and the identity layer agree? The board and one review, flag off and on.

    python scripts/devices_agreement.py --client acme-it --site acme-dental

Computes the coverage board twice — the inline join, then the stored links —
and asserts identical per-site counts; then collects one site's review facts
twice the same way and compares the coverage facts (`coverage.matched`,
`coverage.ninjaone_only`, `coverage.bitdefender_only`, `coverage.scan.*`).
A difference is printed, per site and per field, never reconciled: the two
paths are meant to be one join, and a gap between them is a finding.

Run the day's resolution first (`scripts/devices.py resolve`), or the links
will be behind the inventories. Counts and slugs on the terminal; no device
names. Exit status 1 when anything differs.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.factory import resolve_client, resolve_site
from app.coverage.compute import compute_coverage
from app.db.session import dispose_engine, session_scope
from app.devices.agreement import compare, summary_of
from app.models import Client, ToolMapping
from app.models.enums import ConnectorName
from app.reports.tbr.audit import NullAudit
from app.reports.tbr.collect import Collector
from app.reports.tbr.config import load_config
from app.reports.tbr.model import ReviewPeriod
from app.settings import get_settings

COVERAGE_FACTS = (
    "coverage.matched",
    "coverage.ninjaone_only",
    "coverage.bitdefender_only",
    "coverage.scan.matched",
    "coverage.scan.ninjaone_only",
    "coverage.scan.uncovered",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--client", required=True, help="client slug or UUID")
    parser.add_argument("--site", required=True, help="site slug for the review comparison")
    parser.add_argument("--json", action="store_true")
    return parser


async def review_facts(
    client_id: uuid.UUID, site_id: uuid.UUID, *, through_identity: bool, now: dt.datetime
) -> dict[str, Any]:
    async with session_scope() as session:
        client = await resolve_client(session, client_id)
        config = await load_config(session, client_id, client_name=client.name)
        mapped = {
            row[0]
            for row in (
                await session.execute(
                    select(ToolMapping.connector_name).where(ToolMapping.site_id == site_id)
                )
            ).all()
        }
    os.environ["DEVICE_IDENTITY_ENABLED"] = "true" if through_identity else "false"
    get_settings.cache_clear()
    run = Collector(
        client_id=str(client_id),
        site_id=str(site_id),
        period=ReviewPeriod.ending(now.year, now.month),
        config=config,
        audit=NullAudit(),
        now=now,
        bitdefender_mapped=ConnectorName.BITDEFENDER in mapped,
        connectsecure_mapped=ConnectorName.CONNECTSECURE in mapped,
    )
    facts = await run.collect()
    return {key: facts.facts[key].value for key in COVERAGE_FACTS if key in facts.facts}


async def _client(session: AsyncSession, reference: str) -> Client:
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
    return client


async def main(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        client = await _client(session, args.client)
        site = await resolve_site(session, client, args.site)
        client_id, site_id, site_slug = client.id, site.id, site.slug
    now = dt.datetime.now(dt.UTC)
    off = await compute_coverage(client_id, now=now, through_identity=False)
    on = await compute_coverage(client_id, now=now, through_identity=True)
    board_diff = [
        f"{d['site_slug']}.{d['field']}: off {d['off']} / on {d['on']}" for d in compare(off, on)
    ]
    board = {**summary_of(off, on), "differences": board_diff}
    review_off = await review_facts(client_id, site_id, through_identity=False, now=now)
    review_on = await review_facts(client_id, site_id, through_identity=True, now=now)
    review_diff = [
        f"{key}: off {review_off.get(key)} / on {review_on.get(key)}"
        for key in COVERAGE_FACTS
        if review_off.get(key) != review_on.get(key)
    ]
    result = {
        "board": board,
        "review": {
            "site": site_slug,
            "off": review_off,
            "on": review_on,
            "differences": review_diff,
        },
        "agree": not board_diff and not review_diff,
    }
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(
            f"board: {board['sites']} sites; managed off/on {board['managed']}, unmanaged "
            f"{board['unmanaged']}, discovered {board['discovered']}; covered% "
            + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in board["covered_percent"].items())
        )
        verdict = "identical" if not board_diff else f"{len(board_diff)} difference(s)"
        print(f"board per-site counts: {verdict}")
        for line in board_diff[:40]:
            print(f"  {line}")
        print(f"review ({site_slug}): off {review_off}")
        print(f"review ({site_slug}): on  {review_on}")
        print(f"review facts: {'identical' if not review_diff else 'DIFFER'}")
        for line in review_diff:
            print(f"  {line}")
        print("AGREE" if result["agree"] else "DISAGREE")
    return 0 if result["agree"] else 1


async def _run(args: argparse.Namespace) -> int:
    try:
        return await main(args)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(build_parser().parse_args())))

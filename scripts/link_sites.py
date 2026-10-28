#!/usr/bin/env python
"""Correct what sync_sites got wrong about which vendor organisations are one customer.

Name matching is a heuristic. When "Acme Dental" in NinjaOne and "Acme Family
Dentistry" in Autotask are the same customer, sync creates two sites; when a
franchisee's own lab environment slips past the internal-organisation rules, it
creates one it should not use. This is the operator's fix for both.

    python scripts/link_sites.py --client teamlogic-austin \
        --keep acme-dental --merge acme-family-dentistry
    python scripts/link_sites.py --client teamlogic-austin --deactivate teamlogic-lab

A merge moves the merged site's mappings onto the kept site, keeps the kept
site's name and slug, and deactivates the merged site. Nothing is deleted:
audit rows still point where they did, and the next sync leaves the result
alone because every vendor id is still mapped.

When both sites hold an organisation on the same connector — a customer with
two GravityZone companies — the kept site holds both afterwards and its
reads cover their union. Before merging, the script counts each organisation's
endpoints live, so the operator sees what is being combined; it prints
vendor ids and counts, never device names. The kept site remembers the
merged site's names, so a later sync maps them back here.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select

from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.connectsecure.client import ConnectSecureClient
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for, resolve_client
from app.connectors.ninjaone.client import NinjaOneClient
from app.connectors.sync import deactivate_site, merge_sites
from app.db.session import dispose_engine, session_scope
from app.models import Client, ConnectorName, Site, ToolMapping

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ERROR = 3


async def _resolve_client_id(reference: str) -> uuid.UUID:
    # Same lookup as sync_sites.py; scripts are run as files, not as a package,
    # so they cannot import from one another.
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
            known = (
                (await session.execute(select(Client.slug).order_by(Client.slug))).scalars().all()
            )
            available = ", ".join(known) or "none configured"
            raise SystemExit(
                f"No client matches {reference!r}.\nClients in this database: {available}"
            )
        return client.id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="link_sites",
        description="Merge two of a client's sites, or deactivate one.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--client", required=True, help="client slug or UUID")
    parser.add_argument("--keep", metavar="SLUG", help="the site that survives a merge")
    parser.add_argument("--merge", metavar="SLUG", help="the site folded into --keep")
    parser.add_argument("--deactivate", metavar="SLUG", help="take one site out of every read")
    parser.add_argument(
        "--no-count", action="store_true", help="skip the live per-organisation counts"
    )
    return parser


async def live_counts(client_id: uuid.UUID, slugs: list[str]) -> None:
    """Endpoints per vendor organisation of each site, read live; ids and counts only.

    A site-scoped read already covers every organisation the site holds, so
    each organisation is counted from the site's rows by the vendor id the row
    was tagged with.
    """
    async with session_scope() as session:
        sites = {
            site.slug: site
            for site in (
                await session.execute(
                    select(Site).where(Site.client_id == client_id, Site.slug.in_(slugs))
                )
            ).scalars()
        }
        mappings = list(
            (
                await session.execute(
                    select(ToolMapping).where(
                        ToolMapping.site_id.in_([s.id for s in sites.values()])
                    )
                )
            ).scalars()
        )
    by_site: dict[str, list[ToolMapping]] = {}
    for mapping in mappings:
        for slug, site in sites.items():
            if site.id == mapping.site_id:
                by_site.setdefault(slug, []).append(mapping)
    for slug in slugs:
        print(f"  {slug}:")
        for mapping in sorted(by_site.get(slug, []), key=lambda m: m.connector_name.value):
            if mapping.external_id is None:
                continue
            count = await _count(
                client_id, mapping.connector_name, sites[slug].id, mapping.external_id
            )
            print(f"    {mapping.connector_name.value:14} org {mapping.external_id}: {count}")


async def _count(
    client_id: uuid.UUID, connector: ConnectorName, site_id: uuid.UUID, external_id: str
) -> str:
    try:
        async with connector_for(client_id, connector, site_id) as c:
            if isinstance(c, BitdefenderClient):
                rows, _ = await c.list_endpoints()
                mine = [e for site, e in rows if site.external_id == external_id]
                return f"{len(mine)} endpoints, {sum(1 for e in mine if e.has_agent)} with agents"
            if isinstance(c, ConnectSecureClient):
                assets, _ = await c.list_assets()
                mine_cs = [a for site, a in assets if site.external_id == external_id]
                return f"{len(mine_cs)} assets"
            if isinstance(c, NinjaOneClient):
                devices, _ = await c.list_devices()
                return f"{sum(1 for site, _ in devices if site.external_id == external_id)} devices"
            return "not counted"
    except ConnectorError as exc:
        return f"not counted ({type(exc).__name__})"


async def _main(args: argparse.Namespace) -> int:
    merging = args.keep is not None or args.merge is not None
    if merging and args.deactivate:
        raise SystemExit("Pass either --keep/--merge or --deactivate, not both.")
    if merging and not (args.keep and args.merge):
        raise SystemExit("--keep and --merge go together.")
    if not merging and not args.deactivate:
        raise SystemExit("Nothing to do: pass --keep SLUG --merge SLUG, or --deactivate SLUG.")

    client_id = await _resolve_client_id(args.client)
    try:
        async with session_scope() as session:
            client = await resolve_client(session, client_id)
            if merging:
                if not args.no_count:
                    print("live counts before the merge:")
                    await live_counts(client_id, [args.keep, args.merge])
                report = await merge_sites(session, client, keep=args.keep, merge=args.merge)
                moved = ", ".join(report.moved) or "no mappings"
                print(f"merged {report.merged!r} into {report.kept!r}: moved {moved}")
                for connector, external_id in report.combined:
                    print(
                        f"  {report.kept!r} now holds two {connector} organisations; "
                        f"org {external_id} joins the one it had"
                    )
                if report.absorbed:
                    print(f"  {report.kept!r} remembers {len(report.absorbed)} absorbed name(s)")
                print(f"{report.merged!r} is now inactive; {report.kept!r} keeps its name")
            else:
                site = await deactivate_site(session, client, args.deactivate)
                print(f"deactivated {site.slug!r} ({site.name})")
    except ConnectorError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Check the result with: python scripts/sync_sites.py --client {client.slug} --list")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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

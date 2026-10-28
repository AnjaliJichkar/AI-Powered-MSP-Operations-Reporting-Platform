#!/usr/bin/env python
"""Discover a client's end customers from a vendor and record them as sites.

A franchisee's customers are not typed in. This asks the vendor, through that
franchisee's own credentials, which organisations the credentials can see, and
reconciles the answer into `sites` and site-level `tool_mappings`.

    python scripts/sync_sites.py --client teamlogic-austin --connector ninjaone
    python scripts/sync_sites.py --client teamlogic-austin --all --dry-run
    python scripts/sync_sites.py --client teamlogic-austin --connector autotask --list
    python scripts/sync_sites.py --client teamlogic-austin --all --reset   # dev only

Run it after storing credentials, and again whenever the franchisee onboards or
loses a customer. A site the vendor stops listing is marked inactive, never
deleted: audit rows and mappings point at it.

Sync is more than an upsert. A customer a second connector reports under a
slightly different name is matched to the existing site by normalised name;
the franchisee's own internal organisations are recorded inactive; and a
tenant-level mapping that already carries a vendor id is adopted onto the site
rather than duplicated. A vendor id that another client already holds aborts
the sync. `scripts/link_sites.py` corrects what the matching gets wrong.

A new organisation is also compared by its devices: the vendor's own hostnames
for it against the hostnames the client's other tools hold per site. Enough
overlap (`SYNC_OVERLAP_SHARE`) maps it onto that site, "linked by device
overlap"; an organisation named like a site already on this connector, or
overlapping one a little, is created and listed under "possible duplicates".
A site a merge left remembering a name takes an organisation of that name
back. A mapped organisation the vendor stops returning is printed as "mapping
no longer found" and left for a person; the "vendor duplication" lines name
every site holding more than one organisation on the connector, with ids, so
the franchisee can consolidate at the vendor (counts are on the Coverage
board). Hostnames stay in memory: the terminal gets slugs, ids and counts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from sqlalchemy import delete, select

from app.connectors.errors import ConnectorError
from app.connectors.factory import (
    CONNECTOR_CLASSES,
    build_connector,
    list_client_sites,
    resolve_client,
)
from app.connectors.sync import SyncReport, sync_sites
from app.db.session import dispose_engine, session_scope
from app.models import Client
from app.models.enums import ConnectorName
from app.models.site import Site
from app.models.tool_mapping import ToolMapping
from app.settings import get_settings

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONNECTOR_ERROR = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_sites",
        description="Discover a client's sites from a vendor and record them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--client", required=True, help="client slug or UUID")
    parser.add_argument(
        "--connector",
        choices=sorted(name.value for name in CONNECTOR_CLASSES),
        help="which vendor to ask; omit with --all",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="sync every connector this client has credentials for",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )
    parser.add_argument("--list", action="store_true", help="list known sites and exit")
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "DEV ONLY: delete this client's sites and site-level mappings before syncing, "
            "so the result is exactly what the vendors report today. Refused outside a "
            "local environment. Tenant-level rows, which hold configuration, are kept."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="show engine logging")
    return parser


async def _client_connectors(client_id: uuid.UUID) -> list[ConnectorName]:
    """Which connectors this client already has credentials for."""
    from app.models.tool_credential import ToolCredential

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ToolCredential.connector_name).where(
                    ToolCredential.client_id == client_id,
                    ToolCredential.is_active.is_(True),
                )
            )
        ).scalars()
        return [name for name in rows if name in CONNECTOR_CLASSES]


async def _resolve_client_id(reference: str) -> uuid.UUID:
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


async def show_sites(client_id: uuid.UUID) -> int:
    async with session_scope() as session:
        client = await resolve_client(session, client_id)
        sites = await list_client_sites(session, client.id, active_only=False)
        mappings = (
            (
                await session.execute(
                    select(ToolMapping).where(
                        ToolMapping.client_id == client.id,
                        ToolMapping.site_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    by_site: dict[uuid.UUID, list[str]] = {}
    for mapping in mappings:
        if mapping.site_id is not None:
            by_site.setdefault(mapping.site_id, []).append(
                f"{mapping.connector_name.value}:{mapping.external_id}"
            )

    print(f"{client.slug}: {len(sites)} site(s)\n")
    if not sites:
        print("  none yet — run this script without --list to discover them")
        return EXIT_OK
    for site in sites:
        state = "" if site.is_active else "  [INACTIVE]"
        vendors = ", ".join(sorted(by_site.get(site.id, []))) or "no mappings"
        print(f"  {site.slug}{state}")
        print(f"      name:    {site.name}")
        print(f"      vendors: {vendors}")
    return EXIT_OK


async def reset_sites(client_id: uuid.UUID) -> None:
    """Throw away everything sync built for this client, so it can rebuild it.

    Deleting sites cascades to their site-level mappings. Audit rows do not
    reference sites, so nothing else dangles — but a slug that was in use gets
    a fresh UUID, which is why this is a development convenience only.
    """
    settings = get_settings()
    if settings.environment != "local":
        raise SystemExit(
            f"--reset deletes data and is refused in the {settings.environment!r} environment. "
            "Use scripts/link_sites.py to correct individual sites instead."
        )
    async with session_scope() as session:
        client = await resolve_client(session, client_id)
        mappings = await session.execute(
            delete(ToolMapping).where(
                ToolMapping.client_id == client.id, ToolMapping.site_id.is_not(None)
            )
        )
        sites = await session.execute(delete(Site).where(Site.client_id == client.id))
    print(
        f"reset {client.slug}: deleted {sites.rowcount} site(s) and "
        f"{mappings.rowcount} site-level mapping(s)\n"
    )


#: The connectors whose device hostnames say which site a new organisation is.
HOSTNAME_CONNECTORS = (
    ConnectorName.NINJAONE,
    ConnectorName.BITDEFENDER,
    ConnectorName.CONNECTSECURE,
)


async def site_hostnames(client_id: uuid.UUID, exclude: ConnectorName) -> dict[uuid.UUID, set[str]]:
    """Hostnames per site from every other configured connector's tenant read.

    Grouped through each connector's own mappings, so a hostname lands on the
    site its vendor organisation is mapped to. Names stay in memory.
    """
    found: dict[uuid.UUID, set[str]] = {}
    for other in HOSTNAME_CONNECTORS:
        if other is exclude:
            continue
        try:
            async with session_scope() as session:
                connector = await build_connector(session, client_id, other, require_sites=False)
            async with connector:
                by_org = await connector.hostnames_by_organisation()
                for ref in connector.scope.allowed:
                    found.setdefault(ref.site_id, set()).update(by_org.get(ref.external_id, set()))
        except ConnectorError:
            continue
    return found


async def sync_one(client_id: uuid.UUID, connector_name: ConnectorName, dry_run: bool) -> int:
    """Ask one vendor and reconcile. Returns an exit code."""
    print(f"=== {connector_name.value}")
    try:
        async with session_scope() as session:
            # `require_sites=False`: the first sync runs before any site exists.
            connector = await build_connector(
                session, client_id, connector_name, require_sites=False
            )
        async with connector:
            organisations = await connector.list_organisations()
            try:
                by_organisation = await connector.hostnames_by_organisation()
            except ConnectorError as error:
                print(f"  hostnames not read ({type(error).__name__}); linking by name only")
                by_organisation = {}
    except ConnectorError as error:
        print(f"  FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_CONNECTOR_ERROR

    print(f"  {connector_name.value} reports {len(organisations)} organisation(s)")
    by_site = await site_hostnames(client_id, exclude=connector_name) if by_organisation else {}
    if by_organisation:
        print(
            f"  device overlap: {len(by_organisation)} organisation(s) with hostnames, "
            f"{len(by_site)} site(s) to compare against (threshold "
            f"{get_settings().sync_overlap_share:.0%})"
        )

    async with session_scope() as session:
        client = await resolve_client(session, client_id)
        report = await sync_sites(
            session,
            client,
            connector_name,
            organisations,
            hostnames_by_organisation=by_organisation,
            hostnames_by_site=by_site,
        )
        if dry_run:
            # Everything above ran against the session; roll it back untouched.
            await session.rollback()

    _print_report(report, dry_run)
    return EXIT_OK


def _print_report(report: SyncReport, dry_run: bool) -> None:
    prefix = "  would " if dry_run else "  "
    for label, slugs in (
        ("add", report.created),
        ("match to existing site", report.matched),
        ("map by remembered name onto", report.remembered),
        ("link by device overlap to", report.linked_by_overlap),
        ("adopt tenant-level mapping onto", report.adopted),
        ("record as internal (inactive)", report.internal),
        ("reactivate", report.reactivated),
        ("rename", report.renamed),
        ("deactivate", report.deactivated),
    ):
        for slug in slugs:
            print(f"{prefix}{label}: {slug}")
    for entry in report.possible_duplicates:
        print(f"  possible duplicate: {entry}")
    for entry in report.missing:
        print(f"  mapping no longer found (left in place): {entry}")
    if report.duplicated:
        print("  vendor duplication (more than one organisation on this connector):")
        for entry in report.duplicated:
            print(f"    {entry}")
    print(f"  {report.summary()}")
    if dry_run and report.changed:
        print("  (dry run: nothing was written)")


async def _main(args: argparse.Namespace) -> int:
    client_id = await _resolve_client_id(args.client)

    if args.list:
        return await show_sites(client_id)

    if args.all:
        connectors = await _client_connectors(client_id)
        if not connectors:
            raise SystemExit(
                "This client has no active credentials for any implemented connector. "
                "Store some with scripts/manage_client.py first."
            )
    elif args.connector:
        connectors = [ConnectorName(args.connector)]
    else:
        raise SystemExit("Pass --connector NAME or --all.")

    if args.reset:
        if args.dry_run:
            raise SystemExit("--reset writes; it cannot be combined with --dry-run.")
        await reset_sites(client_id)

    worst = EXIT_OK
    for connector_name in connectors:
        code = await sync_one(client_id, connector_name, args.dry_run)
        worst = code if code != EXIT_OK else worst
        print()

    if worst == EXIT_OK and not args.dry_run:
        print("Done. Check the result with --list.")
    return worst


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

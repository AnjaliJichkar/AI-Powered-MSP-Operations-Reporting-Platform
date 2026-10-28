#!/usr/bin/env python
"""The device identity layer, from the terminal: resolve, queue, show, link, unlink.

    python scripts/devices.py resolve    --client acme-it
    python scripts/devices.py unresolved --client acme-it
    python scripts/devices.py show       --client acme-it --device <uuid>
    python scripts/devices.py show       --client acme-it --connector bitdefender --external-id <id>
    python scripts/devices.py link       --client acme-it --device <uuid> --connector bitdefender \\
                                         --external-id <id> --reason "same serial, renamed in March"
    python scripts/devices.py unlink     --client acme-it --link <id> --reason "wrong machine"

The terminal gets counts and ids: device ids, vendor ids, link ids, passes,
statuses and dates. Never a hostname or a person. `link` and `unlink` need a
reason and write a `device_linked` audit row. Nothing here deletes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.connectors.errors import ConnectorError
from app.connectors.factory import resolve_client
from app.db.session import dispose_engine, session_scope
from app.devices.admin import DeviceAdminError, Operator, link, unlink
from app.devices.resolve import resolve_devices
from app.models import Client, ConnectorName
from app.models.device import Device, DeviceUnresolved, DeviceVendorLink

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="The device identity layer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--client", required=True, help="client slug or UUID")
    parser.add_argument(
        "--user",
        default=os.environ.get("USERNAME") or os.environ.get("USER") or "operator",
        help="the handle recorded in audit_log",
    )
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("resolve", help="run the day's resolution now")
    sub.add_parser("unresolved", help="list the queue: records a pass could not place")
    show = sub.add_parser("show", help="one device across every vendor")
    show.add_argument("--device", help="device UUID")
    show.add_argument("--connector", choices=[c.value for c in ConnectorName])
    show.add_argument("--external-id", help="a vendor's id for the machine, with --connector")
    linkp = sub.add_parser("link", help="tie a vendor record to a device by hand")
    linkp.add_argument("--device", required=True)
    linkp.add_argument("--connector", required=True, choices=[c.value for c in ConnectorName])
    linkp.add_argument("--external-id", required=True)
    linkp.add_argument("--reason", required=True)
    unlinkp = sub.add_parser("unlink", help="withdraw a link, with a reason")
    unlinkp.add_argument("--link", required=True, type=int, help="the link id from show")
    unlinkp.add_argument("--reason", required=True)
    return parser


async def _client_id(reference: str) -> tuple[uuid.UUID, str]:
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
        return client.id, client.slug


def _link_row(row: DeviceVendorLink) -> dict[str, Any]:
    return {
        "link_id": row.id,
        "connector": row.connector_name.value,
        "external_id": row.external_id,
        "matched_by": row.matched_by,
        "active": row.active,
        "first_seen": row.first_seen_at.isoformat(),
        "last_seen": row.last_seen_at.isoformat(),
    }


async def cmd_unresolved(client_id: uuid.UUID, as_json: bool) -> int:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(DeviceUnresolved)
                    .where(
                        DeviceUnresolved.client_id == client_id,
                        DeviceUnresolved.resolved_at.is_(None),
                    )
                    .order_by(DeviceUnresolved.first_seen_at)
                )
            )
            .scalars()
            .all()
        )
    entries = [
        {
            "id": row.id,
            "connector": row.connector_name.value,
            "external_id": row.external_id,
            "site_id": str(row.site_id),
            "pass": row.found_by,
            "candidates": [c.get("device_id") for c in row.candidates],
            "first_seen": row.first_seen_at.isoformat(),
        }
        for row in rows
    ]
    if as_json:
        print(json.dumps(entries, indent=2))
        return EXIT_OK
    print(f"{len(entries)} unresolved record(s)")
    by_pass = Counter(e["pass"] for e in entries)
    if by_pass:
        print("  by pass: " + ", ".join(f"{k} {v}" for k, v in sorted(by_pass.items())))
    for entry in entries:
        print(
            f"  #{entry['id']} {entry['connector']} {entry['external_id']} at site "
            f"{entry['site_id']}: {entry['pass']} pass, {len(entry['candidates'])} candidate(s): "
            + ", ".join(entry["candidates"])
        )
    return EXIT_OK


async def cmd_show(client_id: uuid.UUID, args: argparse.Namespace) -> int:
    async with session_scope() as session:
        device: Device | None = None
        if args.device:
            device = (
                await session.execute(
                    select(Device).where(
                        Device.id == uuid.UUID(args.device), Device.client_id == client_id
                    )
                )
            ).scalar_one_or_none()
        elif args.connector and args.external_id:
            found = (
                await session.execute(
                    select(DeviceVendorLink).where(
                        DeviceVendorLink.client_id == client_id,
                        DeviceVendorLink.connector_name == ConnectorName(args.connector),
                        DeviceVendorLink.external_id == args.external_id,
                    )
                )
            ).scalar_one_or_none()
            if found is not None:
                device = await session.get(Device, found.device_id)
        else:
            print("show needs --device, or --connector with --external-id", file=sys.stderr)
            return EXIT_USAGE
        if device is None:
            print("no such device for this client", file=sys.stderr)
            return EXIT_FAILED
        links = (
            (
                await session.execute(
                    select(DeviceVendorLink)
                    .where(DeviceVendorLink.device_id == device.id)
                    .order_by(DeviceVendorLink.connector_name, DeviceVendorLink.id)
                )
            )
            .scalars()
            .all()
        )
    view = {
        "device_id": str(device.id),
        "site_id": str(device.site_id),
        "status": device.status,
        "node_class": device.node_class,
        "netbios_key_length": len(device.netbios_key),
        "serials": len(device.serials),
        "macs": len(device.macs),
        "first_seen": device.first_seen_at.isoformat(),
        "last_seen": device.last_seen_at.isoformat(),
        "links": [_link_row(row) for row in links],
    }
    if args.json:
        print(json.dumps(view, indent=2))
        return EXIT_OK
    print(
        f"device {view['device_id']} at site {view['site_id']}: {view['status']}, "
        f"{view['node_class'] or 'class unknown'}, {view['serials']} serial(s), "
        f"{view['macs']} MAC(s); first seen {view['first_seen'][:10]}, "
        f"last {view['last_seen'][:10]}"
    )
    for row in view["links"]:
        state = "active" if row["active"] else "inactive"
        print(
            f"  link #{row['link_id']} {row['connector']:14} {row['external_id']} "
            f"by {row['matched_by']}, {state}, last seen {row['last_seen'][:10]}"
        )
    return EXIT_OK


async def _main(args: argparse.Namespace) -> int:
    client_id, slug = await _client_id(args.client)
    operator = Operator(actor_id=args.user, client_id=client_id, client_slug=slug)
    if args.command == "resolve":
        try:
            result = await resolve_devices(client_id, actor=args.user)
        except ConnectorError as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_FAILED
        if args.json:
            print(json.dumps(result.as_dict(), indent=2))
        else:
            by_pass = ", ".join(f"{k} {v}" for k, v in sorted(result.links_created.items()))
            print(
                f"{slug}: {result.devices_created} device(s) created; links created by pass: "
                f"{by_pass or 'none'}; {result.links_seen} link(s) seen, {result.links_moved} "
                f"moved, {result.links_deactivated} deactivated; {result.unresolved_open} "
                f"unresolved ({result.unresolved_new} new); {result.rmm_stale} rmm_stale, "
                f"{result.rmm_agent_down} rmm_agent_down, {result.retired} retired; "
                f"tools {result.tools_read}; {result.duration_ms} ms"
            )
        return EXIT_OK
    if args.command == "unresolved":
        return await cmd_unresolved(client_id, args.json)
    if args.command == "show":
        return await cmd_show(client_id, args)
    try:
        async with session_scope() as session:
            await resolve_client(session, client_id)
            if args.command == "link":
                row = await link(
                    session,
                    operator,
                    device_id=uuid.UUID(args.device),
                    connector=ConnectorName(args.connector),
                    external_id=args.external_id,
                    reason=args.reason,
                )
                print(
                    f"linked {row.connector_name.value} {row.external_id} to device "
                    f"{row.device_id} (link #{row.id}, manual)"
                )
            else:
                row = await unlink(session, operator, link_id=args.link, reason=args.reason)
                print(
                    f"unlinked #{row.id}: {row.connector_name.value} {row.external_id} from device "
                    f"{row.device_id}; the record is queued again"
                )
    except DeviceAdminError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


async def _run(args: argparse.Namespace) -> int:
    try:
        return await _main(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_run(args))
    except SystemExit as error:
        if isinstance(error.code, int):
            raise
        print(str(error), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Why the coverage join left each gap: a read-only diagnostic, per site, with a CSV.

    python scripts/coverage_diagnose.py --client acme-it
    python scripts/coverage_diagnose.py --client acme-it --json
    python scripts/coverage_diagnose.py --client acme-it --out var/coverage

For every managed computer a tool does not cover, and every endpoint a tool
holds that NinjaOne does not manage, it says what would have paired them —
the same hostname at another site (a mapping problem, both sites named), a
normalisation the join does not apply (which one), a shared MAC or serial at
the same site (which key) — or nothing. It also splits the unmanaged
endpoints into not assessable, discovered and real, reports NinjaOne
devices the RMM itself has not heard from as `rmm_stale` rather than absent,
says whether serial or last contact tells the duplicate hostnames apart, and
prints the legacy audit's tenant-wide match rate beside the board's.

Nothing here changes the join or any board number (`app/coverage/diagnose.py`).
The terminal gets counts; device names go only to the CSV under `var/`,
which is outside git and holds customer data.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.connectsecure.client import ConnectSecureClient
from app.connectors.errors import ConnectorError, ConnectorNotConfigured
from app.connectors.factory import connector_for, resolve_client
from app.connectors.ninjaone.client import QUERY_PATH, NinjaOneClient
from app.coverage.config import COVERAGE_TOOLS, NINJAONE_LAST_CONTACT_DAYS
from app.coverage.diagnose import Diagnostic, NinjaExtra, mac_key, run_diagnostic
from app.db.session import dispose_engine, session_scope
from app.models.enums import ConnectorName

CSV_FIELDS = [
    "kind",
    "tool",
    "site",
    "site_slug",
    "id",
    "name",
    "node_class",
    "last_seen",
    "unmanaged_class",
    "discovered",
    "category",
    "detail",
    "other_site",
    "matched_name",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Why the coverage join left each gap. Read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--client", required=True, help="client slug or UUID")
    parser.add_argument(
        "--stale-days",
        type=int,
        default=NINJAONE_LAST_CONTACT_DAYS,
        help=(
            "a NinjaOne device unseen for more than this many days is rmm_stale "
            f"(default {NINJAONE_LAST_CONTACT_DAYS})"
        ),
    )
    parser.add_argument("--out", default="var/coverage", help="directory for the CSV")
    parser.add_argument("--json", action="store_true", help="print the counts as JSON")
    parser.add_argument("--verbose", action="store_true")
    return parser


async def ninja_extras(connector: NinjaOneClient) -> dict[int, NinjaExtra]:
    """Serial per device from computer-systems, MACs and DNS host from network-interfaces."""
    raw, _, _ = await connector.query_scoped("computer-systems")
    systems = [row for row in raw if isinstance(row, dict)]
    placed = await connector.place_by_device(
        systems,
        [row.get("deviceId") for row in systems],
        endpoint=QUERY_PATH.format(report="computer-systems"),
    )
    serials: dict[int, str | None] = {}
    for _, device, row in placed:
        serials[device.id] = row.get("serialNumber") or row.get("biosSerialNumber")
    raw, _, _ = await connector.query_scoped("network-interfaces")
    interfaces = [row for row in raw if isinstance(row, dict)]
    placed_ifaces = await connector.place_by_device(
        interfaces,
        [row.get("deviceId") for row in interfaces],
        endpoint=QUERY_PATH.format(report="network-interfaces"),
    )
    macs: dict[int, set[str]] = {}
    dns: dict[int, str] = {}
    for _, device, row in placed_ifaces:
        values = row.get("macAddress")
        for value in values if isinstance(values, list) else [values]:
            key = mac_key(value if isinstance(value, str) else None)
            if key:
                macs.setdefault(device.id, set()).add(key)
        if row.get("dnsHostName") and device.id not in dns:
            dns[device.id] = str(row["dnsHostName"])
    return {
        device_id: NinjaExtra(
            serial=serials.get(device_id),
            macs=frozenset(macs.get(device_id, set())),
            dns_host=dns.get(device_id),
        )
        for device_id in set(serials) | set(macs) | set(dns)
    }


async def gather(client_id: uuid.UUID, *, stale_days: int) -> Diagnostic:
    now = dt.datetime.now(dt.UTC)
    async with connector_for(client_id, ConnectorName.NINJAONE) as ninja:
        assert isinstance(ninja, NinjaOneClient)
        devices, _ = await ninja.list_devices()
        ninja_sites = [site.site_id for site in ninja.scope.targets]
        extras = await ninja_extras(ninja)
    tools: dict[str, tuple[list[Any], list[uuid.UUID]]] = {}
    for tool in COVERAGE_TOOLS:
        if tool.connector is None or tool.reader is None:
            continue
        try:
            async with connector_for(client_id, tool.connector) as connector:
                if isinstance(connector, BitdefenderClient):
                    rows, _ = await connector.list_endpoints()
                elif isinstance(connector, ConnectSecureClient):
                    rows, _ = await connector.list_assets()
                else:
                    continue
                tools[tool.key] = (list(rows), [s.site_id for s in connector.scope.targets])
        except ConnectorNotConfigured as exc:
            print(f"{tool.key}: not configured ({exc})", file=sys.stderr)
        except ConnectorError as exc:
            print(f"{tool.key}: did not answer ({exc})", file=sys.stderr)
    return run_diagnostic(
        ninja=devices,
        extras=extras,
        ninja_sites=ninja_sites,
        tools=tools,
        now=now,
        stale_days=stale_days,
    )


def print_report(diagnostic: Diagnostic) -> None:
    counts = diagnostic.counts()
    print(
        f"managed computers {counts['managed']}, rmm_stale {counts['rmm_stale']} "
        f"(no NinjaOne contact in {diagnostic.stale_days} days), "
        f"not computers {counts['not_computers']}"
    )
    print("\nmatch rates (per cent of managed computers):")
    print("  within-site (the board): hostname at the same site, covered or stale, over computers")
    print("    at sites the tool answered for")
    print("  tenant-wide (legacy audit): 15-character key anywhere in the tool's inventory, any")
    print(
        "    site, over every computer; _active over computers seen in the last "
        f"{diagnostic.stale_days} days"
    )
    for tool in diagnostic.tools:
        rates = counts["match_rates"]
        print(
            f"  {tool:14} within-site {rates['within_site'][tool]}  "
            f"tenant-wide {rates['tenant_wide'][tool]}  "
            f"tenant-wide active {rates['tenant_wide_active'][tool]}"
        )
    print("\nabsent devices by why they did not pair:")
    for tool, by_category in counts["absent_by_category"].items():
        print(f"  {tool:14} " + ", ".join(f"{k} {v}" for k, v in sorted(by_category.items())))
    print(
        "  normalisation rules that would have paired them: "
        + json.dumps(counts["normalisation_rules"])
    )
    print("  secondary keys that would have paired them: " + json.dumps(counts["secondary_keys"]))
    print("\nunmanaged endpoints:")
    for tool, by_class in counts["unmanaged_by_class"].items():
        print(f"  {tool:14} " + ", ".join(f"{k} {v}" for k, v in sorted(by_class.items())))
        real = counts["unmanaged_real_by_category"].get(tool, {})
        if real:
            print(
                f"  {'':14} real, by why: " + ", ".join(f"{k} {v}" for k, v in sorted(real.items()))
            )
    dup = counts["duplicates"]
    print(
        f"\nduplicate hostnames within a site: {dup['groups']} groups; serial tells them apart in "
        f"{dup['serial_distinguishes']}, last contact in {dup['contact_distinguishes']}, "
        f"neither in {dup['neither']}"
    )
    print("\nper site (worst first; absent counts exclude rmm_stale):")
    header = f"  {'site':30} {'managed':>7} {'rmm_stale':>9}"
    for tool in diagnostic.tools:
        header += f" | {tool[:6]} abs mis norm key none | unm n/a disc real"
    print(header)
    for site in diagnostic.sites:
        line = f"  {site.site_name[:30]:30} {site.managed:>7} {site.rmm_stale:>9}"
        for tool in diagnostic.tools:
            a = site.absent.get(tool, {})
            u = site.unmanaged.get(tool, {})
            absent_total = sum(v for k, v in a.items() if k != "rmm_stale")
            line += (
                f" | {'':6} {absent_total:>3} {a.get('site_mismatch', 0):>3} "
                f"{a.get('normalisation', 0):>4} {a.get('secondary_key', 0):>3} "
                f"{a.get('none', 0):>4}"
                f" | {sum(u.get(k, 0) for k in ('not_assessable', 'discovered', 'real')):>3} "
                f"{u.get('not_assessable', 0):>3} {u.get('discovered', 0):>4} {u.get('real', 0):>4}"
            )
        print(line)


def write_csv(diagnostic: Diagnostic, out: Path, slug: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"coverage-diagnostic-{slug}-{diagnostic.computed_at:%Y%m%d-%H%M%S}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in diagnostic.rows:
            writer.writerow(row.as_dict())
    return path


async def _main(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        try:
            client = await resolve_client(session, args.client)
        except ConnectorError:
            from sqlalchemy import select

            from app.models import Client

            found = (
                await session.execute(select(Client).where(Client.slug == args.client))
            ).scalar_one_or_none()
            if found is None:
                print(f"No client matches {args.client!r}.", file=sys.stderr)
                return 2
            client = found
        client_id, slug = client.id, client.slug
    diagnostic = await gather(client_id, stale_days=args.stale_days)
    path = write_csv(diagnostic, Path(args.out), slug)
    if args.json:
        print(json.dumps({**diagnostic.counts(), "csv": str(path)}, indent=2, default=str))
    else:
        print_report(diagnostic)
        print(f"\nCSV with every gap and its diagnosis: {path}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    try:
        return await _main(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""What one franchisee's Cove Data Protection account looks like, in counts.

    python scripts/cove.py profile --client <slug> [--site <slug>]
    python scripts/cove.py profile --client <slug> [--site <slug>] --diagnose
    python scripts/cove.py profile --client <slug> [--site <slug>] --json

Read-only, and it cannot be anything else: `app/connectors/cove/client.py`
refuses every method that is not on its allowlist before a request exists,
and this command only reads and prints. Every run is a `tool_call` audit row
carrying counts and ids.

**It prints counts and never a name.** No customer, device, person or mailbox
reaches the terminal, the log or a scratch file, because a probe's output
outlives the probe (CLAUDE.md, working conventions). Site slugs are ours and
are printed; what a vendor calls a machine is not.

**`--diagnose` asks each call on its own** and prints what Cove said to each:
the method, whether it answered, and the vendor's own error code and message
when it did not. That is the difference between a wrong password, a role that
may not read, and Cove being unavailable — three things an unlucky HTTP error
makes look identical. It also shows the read-only guard refusing the write
methods, which costs nothing because no request is made for them. It prints
no password, no visa and nothing about a customer.

The headline is deliberately two numbers rather than one. Cove counts a
customer's Microsoft 365 tenant as a device, so "5 protected" is four
machines and a tenant, and anything about machines says so.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from app.audit.recorder import AuditContext, record_event
from app.connectors.cove.client import READ_ONLY_METHODS, CoveClient, ReadOnlyViolation
from app.connectors.cove.columns import DATA_SOURCES
from app.connectors.cove.models import CoveDevice
from app.connectors.errors import ConnectorError, ConnectorNotConfigured
from app.connectors.factory import connector_for
from app.db.session import dispose_engine, session_scope
from app.models.client import Client
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface
from app.models.site import Site
from app.models.tool_mapping import ToolMapping

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

ACTOR = "cove-profile"

#: Write methods the guard must refuse. Named here so `--diagnose` can show
#: the refusal happening rather than asserting that it would.
WRITE_METHODS = (
    "AddAccount",
    "ModifyAccount",
    "RemoveAccount",
    "AddPartner",
    "ModifyPartner",
    "RemovePartner",
    "RemoveUser",
)

#: How recent a successful backup has to be before it is nothing to think
#: about. Three days is the report's own "needs attention" line.
FRESH = dt.timedelta(hours=24)
STALE = dt.timedelta(hours=72)


class UnknownThing(RuntimeError):
    pass


async def _client(slug: str) -> Client:
    async with session_scope() as session:
        found = (
            await session.execute(select(Client).where(Client.slug == slug))
        ).scalar_one_or_none()
        if found is None:
            try:
                found = (
                    await session.execute(select(Client).where(Client.id == uuid.UUID(slug)))
                ).scalar_one_or_none()
            except ValueError:
                found = None
        if found is None:
            raise UnknownThing(f"no client called {slug!r}")
        session.expunge(found)
        return found


async def _site_id(client: Client, slug: str | None) -> str | None:
    if not slug:
        return None
    async with session_scope() as session:
        site = (
            await session.execute(
                select(Site).where(Site.client_id == client.id, Site.slug == slug)
            )
        ).scalar_one_or_none()
        if site is None:
            raise UnknownThing(f"{slug!r} is not a site of {client.slug!r}")
        return str(site.id)


async def _mapped_count(client: Client) -> int:
    """How many of this client's sites carry a Cove partner id."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ToolMapping.site_id).where(
                    ToolMapping.client_id == client.id,
                    ToolMapping.connector_name == ConnectorName.COVE,
                    ToolMapping.site_id.is_not(None),
                )
            )
        ).scalars()
        return len({row for row in rows})


def _storage(total_bytes: int) -> str:
    """TB on a summary line, as the monthly report's card has it."""
    if total_bytes >= 10**12:
        return f"{total_bytes / 10**12:.2f} TB"
    return f"{total_bytes / 10**9:.2f} GB"


def _tally(label: str, counts: Counter[str], *, width: int = 20) -> None:
    if not counts:
        return
    said = ", ".join(f"{name} {number}" for name, number in sorted(counts.items()))
    print(f"  {label:<{width}} {said}")


def _freshness(devices: Sequence[CoveDevice], *, now: dt.datetime) -> Counter[str]:
    """How long ago each machine last backed up successfully."""
    counts: Counter[str] = Counter()
    for device in devices:
        when = device.last_successful_at
        if when is None:
            counts["never"] += 1
        elif now - when <= FRESH:
            counts["within 24h"] += 1
        elif now - when <= STALE:
            counts["24-72h"] += 1
        else:
            counts["over 72h"] += 1
    return counts


def _say(devices: Sequence[CoveDevice], *, customers: int, mapped: int, now: dt.datetime) -> None:
    machines = [d for d in devices if d.is_machine]
    tenants = [d for d in devices if d.is_microsoft365]

    print("\nCustomers")
    print(f"  {customers} under our partner, {mapped} mapped to a site of this client")

    print("\nDevices")
    # Cove's own total is the headline, so it reconciles with their console,
    # and it is labelled so it cannot be read as a machine count.
    tenant_words = f" + {len(tenants)} Microsoft 365" if tenants else ""
    print(f"  {len(devices)} protected: {len(machines)} device(s){tenant_words}")
    print(f"  used storage         {_storage(sum(d.used_storage_bytes or 0 for d in devices))}")
    _tally("by OS type", Counter(d.os_type for d in machines))
    _tally("by physicality", Counter(d.physicality or "not reported" for d in machines))
    _tally("retention policy", Counter(d.product or "not reported" for d in devices))
    _tally("retention scheme", Counter(d.retention_units or "not reported" for d in devices))
    _tally("last session", Counter(d.last_session_status or "not reported" for d in machines))
    _tally("last successful", _freshness(machines, now=now))
    sources: Counter[str] = Counter()
    for device in devices:
        sources.update(DATA_SOURCES.get(code, code) for code in device.data_sources)
    _tally("data sources", sources)
    _tally("recovery testing", Counter(d.recovery_testing or "not reported" for d in machines))
    bars = sum(1 for d in devices if d.colour_bar)
    print(f"  28-day history       {bars} of {len(devices)} row(s) carry one")

    if tenants:
        print("\nMicrosoft 365")
        print(f"  protected user mailboxes    {sum(t.user_mailboxes or 0 for t in tenants)}")
        print(f"  protected shared mailboxes  {sum(t.shared_mailboxes or 0 for t in tenants)}")
        print(f"  OneDrive protected users    {sum(t.onedrive_users or 0 for t in tenants)}")
    else:
        print("\nMicrosoft 365")
        print("  no tenant is backed up by Cove for the site(s) read")

    print("\nNothing was written: this connector reads and cannot do anything else.")


def _counts(devices: Sequence[CoveDevice], *, customers: int, mapped: int) -> dict[str, Any]:
    """What the audit row carries: counts and ids, never a name."""
    machines = [d for d in devices if d.is_machine]
    return {
        "customers": customers,
        "customers_mapped": mapped,
        "devices": len(devices),
        "machines": len(machines),
        "microsoft365_tenants": len(devices) - len(machines),
        "used_storage_bytes": sum(d.used_storage_bytes or 0 for d in devices),
        "os_type": dict(Counter(d.os_type for d in machines)),
        "last_session_status": dict(
            Counter(d.last_session_status or "not reported" for d in machines)
        ),
    }


async def _diagnose(connector: CoveClient, *, sites: Sequence[Any]) -> int:
    """Each call on its own, with what Cove said to it.

    Never prints the password, the visa, or anything about a customer: a
    method name, whether it answered, a count, and the vendor's own code and
    message when it refused.
    """
    print("\n  Call, and what Cove said:")
    answered = 0
    asked = 0

    async def ask(label: str, run: Any) -> Any:
        nonlocal answered, asked
        asked += 1
        try:
            result = await run()
        except ConnectorError as exc:
            details = getattr(exc, "details", {}) or {}
            code = details.get("code")
            print(f"    {label:<42} refused")
            print(f"      {type(exc).__name__}" + (f"  code {code}" if code else ""))
            print(f"      {exc}")
            return None
        answered += 1
        return result

    partners = await ask("Login + EnumeratePartners", connector.list_partners)
    if partners is not None:
        print(f"    {'Login':<42} ok   partner {connector.partner_id}")
        print(f"    {'EnumeratePartners':<42} ok   {len(partners)} customer(s)")
        asked += 1
        answered += 1

    # The fleet first, because that is the number somebody came for. A device
    # read at our own partner covers every customer beneath it, so a site
    # with no devices can no longer be mistaken for an empty account.
    fleet = await ask("EnumerateAccountStatistics (whole fleet)", connector.list_fleet)
    if fleet is not None:
        machines = sum(1 for d in fleet if d.is_machine)
        tenants = len(fleet) - machines
        print(
            f"    {'EnumerateAccountStatistics (fleet)':<42} ok   {len(fleet)} row(s): "
            f"{machines} machine(s) + {tenants} Microsoft 365"
        )
        carried = sorted({code for d in fleet for code in d.data_sources})
        readable = ", ".join(f"{code} {DATA_SOURCES.get(code, '?')}" for code in carried)
        print(f"      data sources: {readable or 'none'}")
        bars = [d.colour_bar for d in fleet if d.colour_bar]
        print(
            f"      28-day history: {len(bars)} of {len(fleet)} row(s) carry one"
            + (f", {len(bars[0])} characters" if bars else "")
        )
        placed = Counter(d.partner_id for d in fleet)
        print(f"      spread over {len(placed)} customer(s) of the {len(sites)} mapped")

    # Then one named site, so the per-customer scoping is exercised too.
    for site in sites[:1]:
        devices = await ask(
            f"EnumerateAccountStatistics (site {site.slug})",
            lambda site=site: connector.list_devices(site),  # type: ignore[misc]
        )
        if devices is not None:
            machines = sum(1 for d in devices if d.is_machine)
            label = f"EnumerateAccountStatistics ({site.slug})"
            print(f"    {label:<42} ok   {len(devices)} row(s), {machines} machine(s)")
    if not sites:
        print(f"    {'EnumerateAccountStatistics (one site)':<42} not asked: none mapped")

    print("\n  Write methods, refused before a request is made:")
    for method in WRITE_METHODS:
        try:
            await connector.call(method, {})
        except ReadOnlyViolation:
            print(f"    {method:<42} refused by the connector, no request sent")
        else:  # pragma: no cover - a guard that let one through is a failed run
            print(f"    {method:<42} WAS NOT REFUSED")
            return EXIT_FAILED

    print(f"\n  {answered} of {asked} call(s) answered.")
    print(f"  {len(READ_ONLY_METHODS)} method(s) on the allowlist; everything else is refused.")
    return EXIT_OK if answered == asked else EXIT_FAILED


async def _audit(
    client: Client, payload: dict[str, Any], *, outcome: AuditOutcome, error: str | None
) -> None:
    async with session_scope() as session:
        await record_event(
            session,
            AuditContext.new(
                actor_id=ACTOR,
                interface=Interface.CLI,
                client_id=client.id,
                client_ref=client.slug,
            ),
            event_type=AuditEventType.TOOL_CALL,
            outcome=outcome,
            connector_name=ConnectorName.COVE,
            tool_name="cove_profile",
            payload=payload,
            error_message=error,
        )


async def _profile(args: argparse.Namespace) -> int:
    try:
        client = await _client(args.client)
        site_id = await _site_id(client, args.site)
    except UnknownThing as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE

    now = dt.datetime.now(dt.UTC)
    try:
        async with connector_for(client.id, ConnectorName.COVE, site_id) as built:
            assert isinstance(built, CoveClient)
            header = f"Cove profile for {client.slug}"
            if args.site:
                header += f" / site {args.site}"
            print(header)
            print(f"  endpoint {built.base_url}  read at {now.isoformat(timespec='seconds')}")

            if args.diagnose:
                code = await _diagnose(built, sites=built.scope.targets)
                await _audit(
                    client,
                    {"diagnose": True, "calls": len(built.calls_made)},
                    outcome=AuditOutcome.SUCCESS if code == EXIT_OK else AuditOutcome.FAILURE,
                    error=None,
                )
                return code

            customers = len(await built.list_partners())
            if args.site:
                devices = await built.list_devices(built.scope.targets[0])
            else:
                # One recursive read at our own partner covers every customer,
                # rather than one request each.
                devices = await built.list_fleet()
    except ConnectorNotConfigured as exc:
        print(f"{exc}", file=sys.stderr)
        print(
            "\nCove needs a credential for the client and a partner id on each site: "
            "see docs/cove-connector.md.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    except ConnectorError as exc:
        print(f"Cove did not answer: {type(exc).__name__}: {exc}", file=sys.stderr)
        await _audit(client, {}, outcome=AuditOutcome.FAILURE, error=str(exc))
        return EXIT_FAILED

    mapped = await _mapped_count(client)
    payload = _counts(devices, customers=customers, mapped=mapped)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _say(devices, customers=customers, mapped=mapped, now=now)
    await _audit(client, payload, outcome=AuditOutcome.SUCCESS, error=None)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    profile = commands.add_parser("profile", help="one account's backup estate, in counts")
    profile.add_argument("--client", required=True, help="client slug or id")
    profile.add_argument("--site", help="one end customer's slug; omit for every mapped site")
    profile.add_argument("--json", action="store_true", help="print the counts as JSON")
    profile.add_argument(
        "--diagnose",
        action="store_true",
        help="ask each call on its own and print what Cove said to it",
    )
    profile.set_defaults(func=_profile)
    return parser


async def _main(args: argparse.Namespace) -> int:
    try:
        return int(await args.func(args))
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":  # pragma: no cover - a command
    raise SystemExit(main())

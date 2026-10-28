#!/usr/bin/env python
"""Run one tool against a vendor's live API, for manual connector validation.

A connector is not done until a human has watched it answer from the real
console (CLAUDE.md, "Testing and quality"). This is the thing you run to do
that, and it is the only place in the codebase that talks to a real vendor.

    python scripts/validate_connector.py --list-tools
    python scripts/validate_connector.py --client teamlogic-austin --tool list_devices
    python scripts/validate_connector.py --client teamlogic-austin \
        --tool list_devices_not_checked_in --days 14 --json
    python scripts/validate_connector.py --client teamlogic-austin \
        --tool get_device --arg device_name=TL-AUS-WS14 --site acme-dental
    python scripts/validate_connector.py --client teamlogic-austin \
        --tool count_tickets --arg group_by=assignee
    python scripts/validate_connector.py --client teamlogic-austin \
        --tool list_antivirus_status --arg unhealthy_only=false
    python scripts/validate_connector.py --client teamlogic-austin --check-bitdefender
    python scripts/validate_connector.py --client teamlogic-austin --check-connectsecure
    python scripts/validate_connector.py --client teamlogic-austin \
        --tool list_critical_vulnerabilities --arg known_exploited_only=true
    python scripts/validate_connector.py --client teamlogic-austin \
        --tool count_endpoints --arg group_by=module
    python scripts/validate_connector.py --client teamlogic-austin \
        --tool get_endpoint_protection --arg device_name=TL-AUS-WS14 --site acme-dental

Any tool argument goes through `--arg NAME=VALUE`, coerced by the tool's own
schema (so `unhealthy_only=false` becomes a bool and `days=14` an int); `--days`
and `--site` are shorthands.

It resolves the client exactly as the agent will (server-side, from the
database), runs one read-only tool, and writes the run to `audit_log` with
`interface=cli` so a live validation leaves the same trail as a real request.

`--check-bitdefender` is the pre-flight for a new GravityZone key: it proves
the connector refuses a write method without any HTTP, reads what the key
is allowed to do (`getApiKeyDetails`), and counts the customer companies it
can see. Nothing it prints is customer data: names of enabled APIs, counts,
and the partner company's type.

`--check-connectsecure` does the same for a ConnectSecure key: a scan, a
create, a delete and an unlisted read are refused with no HTTP, then the
key's own user is read and a warning names its role when it is more than a
viewer, and the companies are counted.

Record the outcome in docs/validation-log.md.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
import time
import uuid
from typing import Any

from sqlalchemy import select

import app.connectors  # noqa: F401  registers every tool
from app.agent.registry import ToolArgumentError, get_tool, registered_tools
from app.audit.recorder import AuditContext, record_event
from app.connectors.errors import ConnectorError, UnknownSite
from app.connectors.factory import list_client_sites, resolve_client, resolve_site
from app.db.session import dispose_engine, session_scope
from app.models import Client
from app.models.enums import AuditEventType, AuditOutcome, Interface

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONNECTOR_ERROR = 3
EXIT_UNEXPECTED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_connector",
        description="Run one connector tool against the live vendor API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--client", help="client slug or UUID")
    parser.add_argument("--tool", help="tool name, e.g. list_devices")
    parser.add_argument(
        "--site",
        help=(
            "one end customer, by slug. Omit to run across every active site of "
            "the client and merge the results."
        ),
    )
    parser.add_argument("--days", type=int, help="shorthand for --arg days=N")
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "any other tool argument, repeatable, e.g. --arg ticket_number=T20260915.0042. "
            "Values are coerced to the tool's declared types."
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the raw result as JSON")
    parser.add_argument("--limit", type=int, default=20, help="rows to print (default 20)")
    parser.add_argument("--list-tools", action="store_true", help="list tools and exit")
    parser.add_argument(
        "--check-bitdefender",
        action="store_true",
        help="pre-flight a GravityZone key: read-only refusal, enabled APIs, company count",
    )
    parser.add_argument(
        "--check-connectsecure",
        action="store_true",
        help="pre-flight a ConnectSecure key: read-only refusal, the user's role, company count",
    )
    parser.add_argument(
        "--actor",
        default="cli",
        help="who is running this, recorded in audit_log (default: cli)",
    )
    parser.add_argument("--verbose", action="store_true", help="show HTTP-level logging")
    return parser


def print_tools() -> None:
    print("Registered tools:\n")
    for definition in registered_tools():
        summary = definition.description.strip().splitlines()[0]
        params = (
            ", ".join(sorted(set(definition.input_schema.get("properties", {})) - {"client_id"}))
            or "none beyond the client"
        )
        print(f"  {definition.name}  [{definition.connector.value}]")
        print(f"      {summary}")
        print(f"      parameters: {params}\n")


async def resolve_client_id(reference: str) -> tuple[uuid.UUID, str]:
    """Accept a slug or a UUID and return the real client id, or explain why not."""
    async with session_scope() as session:
        try:
            parsed = uuid.UUID(reference)
        except ValueError:
            parsed = None

        query = (
            select(Client).where(Client.id == parsed)
            if parsed is not None
            else select(Client).where(Client.slug == reference)
        )
        client = (await session.execute(query)).scalar_one_or_none()

        if client is None:
            known = (await session.execute(select(Client.slug).order_by(Client.slug))).scalars()
            available = ", ".join(known) or "none configured"
            raise SystemExit(
                f"No client matches {reference!r}.\nClients in this database: {available}"
            )
        if not client.is_active:
            raise SystemExit(f"Client {client.slug!r} is not active.")
        return client.id, client.slug


async def resolve_site_id(client_id: uuid.UUID, slug: str) -> str:
    """Turn a site slug into an id, within this client only.

    The same rule the agent loop follows: a slug belonging to another client is
    refused exactly as an unknown one is.
    """
    async with session_scope() as session:
        client = await resolve_client(session, client_id)
        try:
            site = await resolve_site(session, client, slug)
        except UnknownSite as exc:
            sites = await list_client_sites(session, client.id)
            available = ", ".join(site.slug for site in sites) or "none on file"
            raise SystemExit(f"{exc}\nSites for {client.slug}: {available}") from exc
        return str(site.id)


def render(result: Any, limit: int) -> None:
    """Print a result readably. Lists are truncated to `limit` rows."""
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    if not isinstance(payload, dict):
        print(json.dumps(payload, indent=2, default=str))
        return

    rows_key = next(
        (key for key in ("devices", "alerts") if isinstance(payload.get(key), list)), None
    )
    rows = payload.pop(rows_key, []) if rows_key else []

    print("Summary")
    for key, value in payload.items():
        print(f"  {key}: {value}")

    if rows_key:
        print(f"\n{rows_key} ({len(rows)} returned, showing up to {limit})")
        for row in rows[:limit]:
            fields = ", ".join(f"{k}={v}" for k, v in row.items() if v is not None)
            print(f"  - {fields}")
        if len(rows) > limit:
            print(f"  ... {len(rows) - limit} more")

    if payload.get("truncated"):
        print(
            "\nNOTE: truncated=True. The page cap stopped the listing early, so this "
            "is a partial result. Raise CONNECTOR_MAX_PAGES to see the rest."
        )


async def run(args: argparse.Namespace) -> int:
    definition = get_tool(args.tool)
    client_id, slug = await resolve_client_id(args.client)

    kwargs: dict[str, Any] = {"client_id": str(client_id)}
    if args.site is not None:
        if not definition.accepts_site:
            raise SystemExit(f"--site does not apply to {args.tool}")
        kwargs["site_id"] = await resolve_site_id(client_id, args.site)
    supplied: dict[str, Any] = {}
    if args.days is not None:
        supplied["days"] = args.days
    for pair in args.arg:
        name, separator, value = pair.partition("=")
        if not separator:
            raise SystemExit(f"--arg expects NAME=VALUE, got {pair!r}")
        supplied[name.strip()] = value

    accepted = definition.input_schema.get("properties", {})
    hidden = {"client_id", "site_id"}
    for name in supplied:
        if name not in accepted or name in hidden:
            usable = ", ".join(sorted(set(accepted) - hidden)) or "none"
            raise SystemExit(f"{args.tool} takes no argument {name!r}. It accepts: {usable}")
    kwargs.update(supplied)

    # Coerce through the tool's own schema, so "14" becomes 14 and a bad value
    # is rejected here with the same message the model would get.
    try:
        kwargs = definition.validate_arguments(kwargs)
    except ToolArgumentError as exc:
        raise SystemExit(str(exc)) from exc

    context = AuditContext.new(
        actor_id=args.actor,
        interface=Interface.CLI,
        client_id=client_id,
        client_ref=slug,
    )

    print(f"client:  {slug} ({client_id})")
    print(f"tool:    {definition.name}({', '.join(f'{k}={v}' for k, v in kwargs.items())})")
    print(f"started: {dt.datetime.now(dt.UTC).isoformat()}\n")

    started = time.perf_counter()
    try:
        result = await definition.func(**kwargs)
    except ConnectorError as error:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        await record(
            context,
            definition.name,
            AuditOutcome.ERROR,
            elapsed_ms,
            error_type=type(error).__name__,
            error_message=str(error),
            payload={"arguments": kwargs, "error": error.as_dict()},
        )
        print(f"FAILED after {elapsed_ms} ms\n  {type(error).__name__}: {error}", file=sys.stderr)
        if error.details:
            print(f"  details: {error.details}", file=sys.stderr)
        return EXIT_CONNECTOR_ERROR

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    await record(
        context,
        definition.name,
        AuditOutcome.SUCCESS,
        elapsed_ms,
        payload={"arguments": kwargs},
    )

    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
    else:
        render(result, args.limit)

    print(f"\nOK in {elapsed_ms} ms. Record this run in docs/validation-log.md.")
    return EXIT_OK


async def record(
    context: AuditContext,
    tool_name: str,
    outcome: AuditOutcome,
    duration_ms: int,
    *,
    error_type: str | None = None,
    error_message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Hard rule 4: a live validation is a tool call and is logged like one."""
    async with session_scope() as session:
        await record_event(
            session,
            context,
            event_type=AuditEventType.TOOL_CALL,
            outcome=outcome,
            tool_name=tool_name,
            duration_ms=duration_ms,
            error_type=error_type,
            error_message=error_message,
            payload=payload,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.list_tools:
        print_tools()
        return EXIT_OK
    if args.client and (args.check_bitdefender or args.check_connectsecure):
        check = _check_bitdefender if args.check_bitdefender else _check_connectsecure
        try:
            return asyncio.run(check(args))
        except SystemExit as error:
            print(str(error), file=sys.stderr)
            return EXIT_USAGE
    if not args.client or not args.tool:
        build_parser().print_usage(sys.stderr)
        print("\n--client and --tool are required (or use --list-tools).", file=sys.stderr)
        return EXIT_USAGE

    try:
        return asyncio.run(_main(args))
    except KeyError as error:
        print(str(error).strip('"'), file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as error:
        print(str(error), file=sys.stderr)
        return EXIT_USAGE


#: The GravityZone APIs the connector calls (licensing for `getLicenseInfo`, which
#: decides a module's not-licensed state per site). A key should be issued with these
#: and no others; the pre-flight names every surplus one.
USED_APIS = frozenset({"companies", "network", "incidents", "reports", "licensing"})


async def _check_bitdefender(args: argparse.Namespace) -> int:
    """The GravityZone pre-flight. Prints keys, counts and enabled APIs only."""
    from app.connectors.bitdefender.client import BitdefenderClient, ReadOnlyViolation
    from app.connectors.factory import build_connector
    from app.models.enums import ConnectorName

    try:
        client_id, client_slug = await resolve_client_id(args.client)
        async with session_scope() as session:
            connector = await build_connector(  # refuses before any HTTP without a key
                session, client_id, ConnectorName.BITDEFENDER, require_sites=False
            )
        assert isinstance(connector, BitdefenderClient)
        async with connector:
            # 1. A write is refused before any request. This is the property the
            #    whole connector rests on, so it is checked on every pre-flight.
            try:
                await connector.call("network", "createScanTask", {"targetIds": []})
            except ReadOnlyViolation:
                print("read-only guard:   createScanTask refused, no request made")
            else:
                print("read-only guard:   FAILED: a write method was not refused", file=sys.stderr)
                return EXIT_UNEXPECTED
            assert connector.calls_made == []
            details = await connector.call("general", "getApiKeyDetails", {})
            enabled = sorted(details.get("enabledApis", [])) if isinstance(details, dict) else []
            print(f"enabled APIs:      {', '.join(enabled) or 'none reported'}")
            missing = sorted(USED_APIS - set(enabled))
            if missing:
                print(f"                   missing for this connector: {', '.join(missing)}")
            surplus = sorted(set(enabled) - USED_APIS - {"general"})
            if surplus:
                print(
                    "WARNING: the key can call APIs this connector never uses: "
                    f"{', '.join(surplus)}. Every one of them is a write surface the "
                    "read-only allowlist guards against in our code only. Ask the "
                    f"franchisee for a key with just {', '.join(sorted(USED_APIS))} "
                    "enabled (api-notes, 'Credentials to request')."
                )
            own = await connector.call("companies", "getCompanyDetails", {})
            kind = {0: "partner", 1: "customer"}.get(own.get("type"), "unknown")
            print(f"API key's company: type={kind} (a partner key is needed to reach customers)")
            organisations = await connector.list_organisations()
            print(f"customer companies visible: {len(organisations)}")
            print(f"requests made:     {len(connector.calls_made)} ({client_slug})")
    except ConnectorError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_CONNECTOR_ERROR
    finally:
        await dispose_engine()
    return EXIT_OK


async def _check_connectsecure(args: argparse.Namespace) -> int:
    """The ConnectSecure pre-flight. Prints the role, counts and request paths only."""
    from app.connectors.connectsecure.client import ConnectSecureClient, ReadOnlyViolation
    from app.connectors.connectsecure.ledger import DAILY_LIMIT
    from app.connectors.connectsecure.models import READ_ONLY_ROLES
    from app.connectors.factory import build_connector
    from app.models.enums import ConnectorName

    try:
        client_id, client_slug = await resolve_client_id(args.client)
        async with session_scope() as session:
            connector = await build_connector(  # refuses before any HTTP without a key
                session, client_id, ConnectorName.CONNECTSECURE, require_sites=False
            )
        assert isinstance(connector, ConnectSecureClient)
        async with connector:
            # 1. A write is refused before any request, and so is a read off the
            #    allowlist. This is the property the connector rests on.
            for method, path in (
                ("POST", "/w/company/scan"),
                ("POST", "/w/company/companies"),
                ("DELETE", "/d/company/companies/1"),
                ("GET", "/r/company/credentials"),
            ):
                try:
                    await connector.request(method, path, json_body={})
                except ReadOnlyViolation:
                    continue
                print(
                    f"read-only guard:   FAILED: {method} {path} was not refused", file=sys.stderr
                )
                return EXIT_UNEXPECTED
            print(
                "read-only guard:   scan, create, delete and an unlisted read refused, "
                "no request made"
            )
            assert connector.calls_made == []
            # 2. Log in, then read the key's own user: its role is the write surface
            #    the allowlist guards against in our code only.
            user = await connector.current_user()
            if user is None:
                print(
                    "API key's user:    not found in /r/user/get_users (unverified shape; "
                    "see api-notes)"
                )
            else:
                roles = sorted(user.role_set) or ["(none reported)"]
                print(f"API key's role:    {', '.join(roles)}")
                if not user.role_set or not user.role_set <= READ_ONLY_ROLES:
                    print(
                        "WARNING: the key's user can do more than read. Every write the vendor "
                        "allows that role is guarded by the read-only allowlist in our code "
                        "only. Ask the franchisee for a user with a viewer role "
                        f"({', '.join(sorted(READ_ONLY_ROLES))}) and the companies this "
                        "integration needs (api-notes, 'Credentials to request')."
                    )
            organisations = await connector.list_organisations()
            print(f"companies visible: {len(organisations)} (assessment and deleted ones left out)")
            print(
                f"requests made:     {len(connector.calls_made)} ({client_slug}): "
                + ", ".join(f"{m} {p}" for m, p in connector.calls_made)
            )
            # The vendor sends no quota headers, so the connector keeps its own count.
            today = connector.requests_today
            print(
                f"requests today:    {today} of {DAILY_LIMIT:,} ({100 * today / DAILY_LIMIT:.2f}%) "
                "in this process; the vendor also limits 300 a minute and 2,000 an hour"
            )
    except ConnectorError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_CONNECTOR_ERROR
    finally:
        await dispose_engine()
    return EXIT_OK


async def _main(args: argparse.Namespace) -> int:
    try:
        return await run(args)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""The Microsoft 365 tenant profile: what one customer's tenant looks like.

    python scripts/m365.py profile --client <slug> [--site <slug>] [--json]
    python scripts/m365.py people  --client <slug> [--site <slug>] [--json]

Read-only, and it writes nothing to the client: Microsoft 365 has no write
path in this engine at all (`app/connectors/microsoft365/client.py` refuses
any method but GET), and this command only reads and prints. Every read is a
`tool_call` audit row, which carries counts and ids and no names.

What it prints, in the order somebody onboarding a customer wants it:

* whether the accounts are cloud-only or synced from an on-premises Active
  Directory, with the count of each;
* the subscriptions, with assigned and free seats;
* MFA coverage over enabled member accounts, and who is missing one;
* groups, Teams and distribution lists, with member counts;
* how many people share each department, job title and office, which is what
  a role matrix is drafted from — what the tenant already says, rather than a
  guess at what it should be;
* anything this tenant could not answer, and why. A tenant without an Entra
  ID P1 licence has no sign-in activity; that is a line saying so, never a
  zero and never an error.

**Each end customer has their own Microsoft 365 tenant**, so the profile is of
one tenant: `--site` names it. A client with exactly one tenant mapped needs
no `--site`.

`people` is the other half: one tenant's accounts joined to that customer's
Autotask contacts by email and to the machines they are the last logged-on
user of, through the device identity layer. It stores nothing either, and it
prints ids rather than names, as `scripts/devices.py` does — the join is what
is being checked, not who is in it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import select

from app.audit.recorder import AuditContext, record_event
from app.connectors.autotask.client import AutotaskClient
from app.connectors.errors import ConnectorError, ConnectorNotConfigured
from app.connectors.factory import connector_for
from app.connectors.microsoft365.client import Microsoft365Client
from app.connectors.microsoft365.reads import Profile, profile_for
from app.connectors.ninjaone.tools import list_logged_on_users
from app.db.session import dispose_engine, session_scope
from app.devices.people import PeopleJoin, join_people, site_devices
from app.models.client import Client
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface
from app.models.site import Site

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 1

ACTOR = "m365-profile"


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


def _bytes(value: int) -> str:
    if value >= 10**9:
        return f"{value / 10**9:.1f} GB"
    return f"{value / 10**6:.0f} MB"


def _print(profile: Profile, *, client: Client) -> None:
    print(f"Microsoft 365 profile for {client.slug}")
    print(
        f"  tenant {profile.tenant_id}"
        + (f"  site {profile.site_slug}" if profile.site_slug else "")
    )
    print(f"  read at {profile.read_at.isoformat(timespec='seconds')}")
    if profile.truncated:
        print("  the user listing stopped at the page cap: every count is a lower bound")

    print("\nAccounts")
    print(
        f"  {profile.users_total} in the directory: {profile.members} member(s), "
        f"{profile.guests} guest(s)"
    )
    print(f"  {profile.enabled} enabled, {profile.disabled} disabled")
    print(f"  {profile.cloud_only} cloud-only, {profile.synced} synced from on-premises AD")

    print("\nSubscriptions")
    if not profile.licences:
        print("  none read")
    for line in profile.licences:
        print(
            f"  {line.name:<22} {line.assigned:>4} assigned  {line.free:>4} free  of {line.total}"
        )

    print("\nMFA")
    if profile.mfa_population:
        share = round(100 * profile.mfa_registered / profile.mfa_population)
        print(
            f"  {profile.mfa_registered} of {profile.mfa_population} enabled member accounts "
            f"registered ({share}%)"
        )
    else:
        print("  no enabled member accounts to judge")
    if profile.mfa_not_people:
        print(
            f"  {profile.mfa_not_people} shared mailbox(es) or room(s) left out: they have no "
            f"interactive sign-in and cannot register one"
        )
    if profile.mfa_missing:
        print(f"  {profile.mfa_missing} with none registered:")
        for name in profile.mfa_missing_names:
            print(f"    {name}")

    print("\nGroups")
    for kind, count in profile.group_counts.items():
        if count:
            print(f"  {count} {kind}(s)")
    for group in profile.groups:
        members = "members unknown" if group.members is None else f"{group.members} member(s)"
        print(f"    {group.name} — {group.kind}, {members}")

    print("\nWho shares what")
    for label, rows in (
        ("department", profile.departments),
        ("job title", profile.job_titles),
        ("office", profile.offices),
    ):
        if not rows:
            print(f"  {label}: none set")
            continue
        print(f"  {label}:")
        for value, count in rows:
            print(f"    {count:>3}  {value}")

    print("\nMailboxes")
    for label, count in profile.mailboxes.items():
        print(f"  {count} {label}")
    for name, size in profile.mailbox_sizes[:5]:
        print(f"    {_bytes(size):>9}  {name}")

    if profile.unavailable:
        print("\nNot available for this tenant")
        for item in profile.unavailable:
            print(f"  {item.line}")
    print("\nNothing was written: this connector reads and cannot do anything else.")


async def _audit(
    client: Client, profile: Profile, *, outcome: AuditOutcome, error: str | None
) -> None:
    """One `tool_call` row with the profile's counts. Counts and ids only —
    no name of a person, a group or a mailbox goes in the record."""
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
            connector_name=ConnectorName.MICROSOFT365,
            tool_name="m365_profile",
            payload=profile.as_dict() if profile is not None else {"error": error},
            error_message=error,
        )


async def _profile(args: argparse.Namespace) -> int:
    try:
        client = await _client(args.client)
        site_id = await _site_id(client, args.site)
    except UnknownThing as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE

    try:
        async with connector_for(client.id, ConnectorName.MICROSOFT365, site_id) as connector:
            assert isinstance(connector, Microsoft365Client)
            profile = await profile_for(connector)
    except ConnectorNotConfigured as exc:
        print(f"{exc}", file=sys.stderr)
        print(
            "\nMicrosoft 365 is per end customer: each tenant consents separately and its tenant "
            "id is the site's mapping. See docs/m365-connector.md.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    except ConnectorError as exc:
        print(f"Microsoft 365 did not answer: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if args.json:
        print(json.dumps(profile.as_dict(), indent=2))
    else:
        _print(profile, client=client)
    await _audit(client, profile, outcome=AuditOutcome.SUCCESS, error=None)
    return EXIT_OK


async def _people(args: argparse.Namespace) -> int:
    """One tenant's accounts joined to the customer's contacts and machines.

    Three reads and a join in memory: the tenant's users, the site's Autotask
    contacts, and NinjaOne's last logged-on user per machine. Nothing is
    written anywhere — there is no people table — and the terminal gets ids.
    """
    try:
        client = await _client(args.client)
        site_id = await _site_id(client, args.site)
    except UnknownThing as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE

    try:
        async with connector_for(client.id, ConnectorName.MICROSOFT365, site_id) as built:
            assert isinstance(built, Microsoft365Client)
            tenant = built.tenant
            site = built.site_for(tenant)
            accounts, truncated, _ = await built.list_users()
        # The other two tools are asked about the same end customer, never the
        # whole client: a tenant's accounts are joined to one customer's
        # contacts and machines or they are joined to the wrong people.
        scope_site = str(site.site_id) if site is not None else site_id
        if scope_site is None:
            print(
                "name the end customer with --site: a tenant's accounts are joined to one "
                "customer's contacts and machines, never to the whole client's",
                file=sys.stderr,
            )
            return EXIT_USAGE
        async with connector_for(client.id, ConnectorName.AUTOTASK, scope_site) as autotask:
            assert isinstance(autotask, AutotaskClient)
            rows, _ = await autotask.list_contacts()
        contacts = [(contact.id, contact.email_address) for _, contact in rows]
        logons = await list_logged_on_users(str(client.id), site_id=scope_site)
    except ConnectorNotConfigured as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_USAGE
    except ConnectorError as exc:
        print(f"a tool did not answer: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    async with session_scope() as session:
        devices = await site_devices(session, client.id, uuid.UUID(scope_site))
    join = join_people(
        accounts=accounts,
        contacts=contacts,
        logged_on=[(row.device, row.user_name) for row in logons.rows],
        devices_by_name=devices,
        site_id=uuid.UUID(scope_site),
        site_slug=site.slug if site is not None else None,
        tenant_id=tenant,
    )

    if args.json:
        print(json.dumps(join.as_dict(), indent=2))
    else:
        print(f"Microsoft 365 people for {client.slug}")
        print(f"  tenant {join.tenant_id}" + (f"  site {join.site_slug}" if join.site_slug else ""))
        if truncated:
            print("  the user listing stopped at the page cap: every count is a lower bound")
        print(f"\n  {len(join.people)} account(s) in the directory")
        print(f"  {join.matched_contacts} matched to an Autotask contact by email")
        print(f"  {join.matched_devices} matched to a machine by last logged-on user")
        print(f"  {join.contacts_unmatched} contact(s) with an address and no account")
        print(f"  {join.logged_on_unmatched} machine(s) whose last user is nobody in the directory")
        print("\n  account                               contact   machines")
        for row in join.people:
            if row.contact_id is None and not row.devices:
                continue
            machines = ", ".join(f"{d.device_id} ({d.matched_by})" for d in row.devices) or "-"
            contact = str(row.contact_id) if row.contact_id is not None else "-"
            print(f"  {row.user_id:<36}  {contact:<8}  {machines}")
        print("\nNothing was written: the join is computed when it is asked for and kept nowhere.")
    await _audit_people(client, join)
    return EXIT_OK


async def _audit_people(client: Client, join: PeopleJoin) -> None:
    """One `tool_call` row with the join's counts. Ids, never addresses."""
    payload = {k: v for k, v in join.as_dict().items() if k != "people"}
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
            outcome=AuditOutcome.SUCCESS,
            connector_name=ConnectorName.MICROSOFT365,
            tool_name="m365_people",
            payload=payload,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m365", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    profile = commands.add_parser("profile", help="one tenant's shape, read only")
    profile.add_argument("--client", required=True, help="client slug or id")
    profile.add_argument(
        "--site",
        default=None,
        help="the end customer whose tenant to read; needed when the client has more than one",
    )
    profile.add_argument("--json", action="store_true", help="the counts as JSON, nothing else")
    profile.set_defaults(func=_profile)

    people = commands.add_parser(
        "people", help="accounts joined to Autotask contacts and NinjaOne machines, read only"
    )
    people.add_argument("--client", required=True, help="client slug or id")
    people.add_argument(
        "--site", default=None, help="the end customer whose tenant, contacts and machines to join"
    )
    people.add_argument("--json", action="store_true", help="the join as JSON, ids only")
    people.set_defaults(func=_people)
    return parser


async def _main(args: argparse.Namespace) -> int:
    try:
        result: int = await args.func(args)
        return result
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())

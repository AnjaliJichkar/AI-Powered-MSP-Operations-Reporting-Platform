#!/usr/bin/env python
"""The demo tenant: create it, reset what it has accumulated, or delete it.

    python scripts/demo_tenant.py create
    python scripts/demo_tenant.py reset
    python scripts/demo_tenant.py delete --yes

`create` registers the demo client (`kind = demo`, so the connector factory
serves it only from the fake connectors in `app/demo/` and it can never hold
a credential), discovers its sites and vendor mappings by running the same
site sync a live client runs — through the fakes — writes its report
settings, creates the demo franchisee organisation, and prints one-time
invite links for a demo administrator, a demo technician and a demo viewer,
one per franchisee role. It is idempotent: run again, it syncs again and
reissues the invites.

`reset` deletes what the tenant has accumulated — reviews, coverage snapshots
and exclusions, device identities and links, collected events, ticket
workspaces and plans, conversations — and syncs the sites again, so the demo
starts from the generated world. Accounts and the audit record stay.

`delete` removes the client and everything that hangs off it, including the
organisation, its accounts and sessions, and the demo-marked audit rows,
which is allowed for this client and no other because nothing in them
concerns a real franchisee. It asks first unless `--yes` is given.

No vendor is called. Everything printed is a count, a slug or a link.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import InviteRefused, create_invite, create_organisation
from app.db.session import dispose_engine, session_scope
from app.demo.connectors import demo_world
from app.demo.scenarios import SCENARIOS, check
from app.demo.vocabulary import FRANCHISEE_NAME, FRANCHISEE_SLUG
from app.models import Client, Organisation, User
from app.models.audit_log import AuditLog
from app.models.client import ClientKind
from app.models.collected_event import CollectedEvent, CollectionCoverage
from app.models.conversation import Conversation, ConversationMessage
from app.models.coverage import CoverageExclusion, CoverageSnapshot
from app.models.device import Device, DeviceAgreement, DeviceUnresolved, DeviceVendorLink
from app.models.enums import ConnectorName, OrganisationKind, Role
from app.models.organisation import Invite, Session
from app.models.report import ReportConfig, TbrReport
from app.models.site import Site
from app.models.ticket import TicketPlan, TicketWorkspace
from app.reports.tbr.config import FranchiseeConfig, PriceBookEntry, load_config, save_config
from app.resolved.config import (
    ClassificationConfig,
    CostConfig,
    ResolvedConfig,
    ResolverConfig,
    SlaConfig,
    SlaTarget,
    TicketClassRule,
)
from app.settings import get_settings

EXIT_OK, EXIT_USAGE = 0, 2
DEMO_ADMIN = "demo-admin@tailwind-it.example"
DEMO_TECHNICIAN = "demo-tech@tailwind-it.example"
DEMO_VIEWER = "demo-viewer@tailwind-it.example"
#: One account per franchisee role, so a demo can show what each of them sees.
DEMO_ACCOUNTS: tuple[tuple[str, Role], ...] = (
    (DEMO_ADMIN, Role.FRANCHISEE_ADMIN),
    (DEMO_TECHNICIAN, Role.FRANCHISEE_TECHNICIAN),
    (DEMO_VIEWER, Role.FRANCHISEE_VIEWER),
)
CONNECTORS = (
    ConnectorName.NINJAONE,
    ConnectorName.AUTOTASK,
    ConnectorName.BITDEFENDER,
    ConnectorName.CONNECTSECURE,
    # Cove discovers its customers like the rest: `EnumeratePartners` returns
    # the tree beneath ours, and only an `EndCustomer` becomes a site. Two
    # demo customers buy no backup and so have no partner, which is what gives
    # the coverage board a customer to say `not assessed` about.
    ConnectorName.COVE,
)


async def _client(session: AsyncSession) -> Client | None:
    return (
        await session.execute(select(Client).where(Client.slug == FRANCHISEE_SLUG))
    ).scalar_one_or_none()


async def _ensure_client(session: AsyncSession) -> Client:
    client = await _client(session)
    if client is None:
        client = Client(
            slug=FRANCHISEE_SLUG,
            name=FRANCHISEE_NAME,
            kind=ClientKind.DEMO,
            notes="The demo tenant: generated data, fake connectors. See app/demo/.",
        )
        session.add(client)
        await session.flush()
        print(f"created demo client {client.slug} ({client.id})")
    elif not client.is_demo:
        raise SystemExit(f"{client.slug} exists and is a live client; refusing to touch it")
    else:
        print(f"demo client {client.slug} exists ({client.id})")
    return client


async def _sync(client: Client) -> None:
    """The same site sync a live client gets, through the fakes."""
    from scripts.sync_sites import sync_one

    for connector in CONNECTORS:
        status = await sync_one(client.id, connector, dry_run=False)
        if status != EXIT_OK:
            raise SystemExit(f"site sync failed for {connector.value}")


async def _map_microsoft365(session: AsyncSession, client: Client) -> int:
    """Map each customer's Entra tenant onto its site.

    Microsoft 365 has no site discovery and cannot have one: a token is for
    one tenant and cannot enumerate the others, so a live client's tenant ids
    are mapped by a person from the consent their customer granted
    (docs/m365-connector.md). The demo does by hand what a person would do.
    """
    from app.models.tool_mapping import ToolMapping

    world = demo_world(FRANCHISEE_SLUG)
    sites = {
        site.slug: site
        for site in (
            await session.execute(select(Site).where(Site.client_id == client.id))
        ).scalars()
    }
    mapped = 0
    for customer in world.customers:
        site = sites.get(customer.key) or next(
            (s for s in sites.values() if s.name == customer.name), None
        )
        if site is None or not customer.m365_tenant_id:
            continue
        existing = (
            await session.execute(
                select(ToolMapping).where(
                    ToolMapping.client_id == client.id,
                    ToolMapping.connector_name == ConnectorName.MICROSOFT365,
                    ToolMapping.site_id == site.id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                ToolMapping(
                    client_id=client.id,
                    site_id=site.id,
                    connector_name=ConnectorName.MICROSOFT365,
                    external_id=customer.m365_tenant_id,
                    external_name=customer.name,
                )
            )
            mapped += 1
        else:
            existing.external_id = customer.m365_tenant_id
    await session.flush()
    return mapped


async def _report_settings(session: AsyncSession, client: Client) -> None:
    config = await load_config(session, client.id, client_name=client.name)
    config.display_name = FRANCHISEE_NAME
    config.presenter = "Alex Morgan"
    config.show_people_names = True  # every name here is fictional
    config.business_hours.timezone = "UTC-06:00"  # a fixed offset needs no tz database
    config.price_book.setdefault(
        "device_replacement", PriceBookEntry(hours=3, cost=1450, per="device")
    )
    config.price_book.setdefault("memory_upgrade", PriceBookEntry(hours=1, cost=180, per="device"))
    config.price_book.setdefault(
        "patch_remediation", PriceBookEntry(label="Patch remediation", cost=900)
    )
    config.resolved = _resolved_settings()
    await save_config(
        session,
        client.id,
        FranchiseeConfig.model_validate(config.model_dump()),
        updated_by="demo_tenant",
    )


def _resolved_settings() -> ResolvedConfig:
    """The per-outcome service, configured the way a real franchisee would.

    Targets in business minutes against this tenant's own priority labels; the
    two statuses that stop the clock named by label, as they must be, because a
    status number means nothing outside one Autotask database; a class mapping
    that sends monitoring alerts to L1 and anything Critical or escalated to L3;
    and two of the five engineers as ours, with a loaded hourly cost each.
    """
    return ResolvedConfig(
        sla=SlaConfig(
            targets={
                "Critical": SlaTarget(first_response_minutes=30, resolution_minutes=240),
                "High": SlaTarget(first_response_minutes=60, resolution_minutes=480),
                "Medium": SlaTarget(first_response_minutes=240, resolution_minutes=1_440),
                "Low": SlaTarget(first_response_minutes=480, resolution_minutes=2_880),
            },
            paused_status_labels=["Waiting Customer", "Waiting Vendor"],
            # A public holiday in the middle of the demo's ninety days, so the
            # clock visibly skips a day somebody can point at.
            holidays=[dt.date(2026, 9, 7)],
        ),
        classes=ClassificationConfig(
            rules=[
                TicketClassRule(ticket_class="L3", priority=["Critical"]),
                TicketClassRule(
                    ticket_class="L3", issue_type=["Cybersecurity", "Network/Internet"]
                ),
                TicketClassRule(ticket_class="L1", queue=["Monitoring Alerts"]),
                TicketClassRule(
                    ticket_class="L1",
                    issue_type=["Email", "User Access and Management", "Printer/Scanner/Copier"],
                ),
                TicketClassRule(ticket_class="L2", priority=["High", "Medium"]),
            ],
            default="L1",
        ),
        resolvers=ResolverConfig(inputiv=["Sam Okafor", "Dana Whitfield"]),
        cost=CostConfig(
            default_hourly=Decimal("62.00"),
            by_resource={
                "Sam Okafor": Decimal("48.00"),
                "Dana Whitfield": Decimal("48.00"),
                "Elena Kowalski": Decimal("71.00"),
            },
        ),
        reopened_within_days=7,
    )


async def _organisation(session: AsyncSession, client: Client) -> Organisation:
    organisation = (
        await session.execute(select(Organisation).where(Organisation.slug == FRANCHISEE_SLUG))
    ).scalar_one_or_none()
    if organisation is None:
        organisation = await create_organisation(
            session,
            kind=OrganisationKind.FRANCHISEE,
            slug=FRANCHISEE_SLUG,
            name=FRANCHISEE_NAME,
            client=client,
        )
        print(f"created organisation {organisation.slug}")
    return organisation


async def _invites(session: AsyncSession, organisation: Organisation) -> list[tuple[str, str, str]]:
    settings = get_settings()
    links = []
    for email, role in DEMO_ACCOUNTS:
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if existing is not None:
            links.append((email, role.value, "already has an account"))
            continue
        try:
            _, token = await create_invite(
                session,
                organisation=organisation,
                email=email,
                role=role,
                settings=settings,
                reissue=True,
            )
        except (InviteRefused, ValueError) as exc:
            links.append((email, role.value, f"not invited: {exc}"))
            continue
        links.append((email, role.value, settings.url_for(f"/invite/{token}")))
    return links


async def cmd_create(args: argparse.Namespace) -> int:
    world = demo_world(FRANCHISEE_SLUG)
    problems = check(world)
    if problems:
        print("the generated world is missing scenarios:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_USAGE
    async with session_scope() as session:
        client = await _ensure_client(session)
        client_id = client.id
    async with session_scope() as session:
        client = await _client(session)
        assert client is not None
        await _report_settings(session, client)
    await _sync(client)
    async with session_scope() as session:
        client = await _client(session)
        assert client is not None
        mapped = await _map_microsoft365(session, client)
        print(f"microsoft365: {mapped} tenant(s) mapped onto sites")
    async with session_scope() as session:
        client = await _client(session)
        assert client is not None
        organisation = await _organisation(session, client)
        links = await _invites(session, organisation)
    print()
    print(f"dataset: {', '.join(f'{k} {v}' for k, v in world.summary().items())}")
    print(f"scenarios present: {len(SCENARIOS)}")
    print()
    print("Invite links (each works once):")
    for email, role, link in links:
        print(f"  {role:<22} {email}")
        print(f"  {'':<22} {link}")
    print(f"\nclient id {client_id}")
    return EXIT_OK


async def _wipe_data(session: AsyncSession, client: Client) -> dict[str, int]:
    """Everything the tenant accumulated in use; not its accounts, not its audit rows."""
    counts: dict[str, int] = {}
    conversation_ids = select(Conversation.id).where(Conversation.client_id == client.id)
    for label, statement in (
        ("ticket plans", delete(TicketPlan).where(TicketPlan.client_id == client.id)),
        (
            "ticket workspaces",
            delete(TicketWorkspace).where(TicketWorkspace.client_id == client.id),
        ),
        (
            "conversation messages",
            delete(ConversationMessage).where(
                ConversationMessage.conversation_id.in_(conversation_ids)
            ),
        ),
        ("reviews", delete(TbrReport).where(TbrReport.client_id == client.id)),
        (
            "coverage snapshots",
            delete(CoverageSnapshot).where(CoverageSnapshot.client_id == client.id),
        ),
        (
            "coverage exclusions",
            delete(CoverageExclusion).where(CoverageExclusion.client_id == client.id),
        ),
        (
            "device agreements",
            delete(DeviceAgreement).where(DeviceAgreement.client_id == client.id),
        ),
        (
            "unresolved records",
            delete(DeviceUnresolved).where(DeviceUnresolved.client_id == client.id),
        ),
        ("device links", delete(DeviceVendorLink).where(DeviceVendorLink.client_id == client.id)),
        ("devices", delete(Device).where(Device.client_id == client.id)),
        (
            "collection coverage",
            delete(CollectionCoverage).where(CollectionCoverage.client_id == client.id),
        ),
        ("collected events", delete(CollectedEvent).where(CollectedEvent.client_id == client.id)),
    ):
        result = await session.execute(statement)
        counts[label] = int(result.rowcount or 0)
    return counts


async def cmd_reset(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        client = await _client(session)
        if client is None:
            print("no demo client; run `create` first", file=sys.stderr)
            return EXIT_USAGE
        if not client.is_demo:
            raise SystemExit(f"{client.slug} is a live client; refusing")
        counts = await _wipe_data(session, client)
    for label, count in counts.items():
        if count:
            print(f"deleted {count} {label}")
    await _sync(client)
    print("reset: the tenant reads the generated world again")
    return EXIT_OK


async def cmd_delete(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        client = await _client(session)
        if client is None:
            print("no demo client to delete")
            return EXIT_OK
        if not client.is_demo:
            raise SystemExit(f"{client.slug} is a live client; refusing")
        audit_rows = (
            await session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.client_id == client.id)
            )
        ).scalar_one()
    if not args.yes:
        question = (
            f"delete the demo client, its organisation, accounts and {audit_rows} demo audit "
            "rows? [y/N] "
        )
        answer = await asyncio.to_thread(input, question)
        if answer.strip().lower() != "y":
            print("nothing deleted")
            return EXIT_OK
    async with session_scope() as session:
        client = await _client(session)
        assert client is not None
        await _wipe_data(session, client)
        # Demo audit rows may go: they are marked demo and concern no franchisee.
        await session.execute(
            delete(AuditLog).where(AuditLog.client_id == client.id, AuditLog.demo.is_(True))
        )
        left = (
            await session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.client_id == client.id)
            )
        ).scalar_one()
        if left:
            raise SystemExit(
                f"{left} audit rows for the demo client are not marked demo; refusing to delete"
            )
        organisation = (
            await session.execute(select(Organisation).where(Organisation.client_id == client.id))
        ).scalar_one_or_none()
        if organisation is not None:
            user_ids = select(User.id).where(User.organisation_id == organisation.id)
            await session.execute(delete(Session).where(Session.user_id.in_(user_ids)))
            await session.execute(delete(Invite).where(Invite.organisation_id == organisation.id))
            await session.execute(delete(User).where(User.organisation_id == organisation.id))
            await session.delete(organisation)
        await session.execute(delete(Conversation).where(Conversation.client_id == client.id))
        await session.execute(delete(ReportConfig).where(ReportConfig.client_id == client.id))
        await session.delete(client)  # sites, mappings and the rest cascade
    print(f"deleted the demo client {FRANCHISEE_SLUG}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="create the demo tenant, or sync it again").set_defaults(
        func=cmd_create
    )
    sub.add_parser("reset", help="delete what it accumulated and sync again").set_defaults(
        func=cmd_reset
    )
    remove = sub.add_parser("delete", help="delete the client, its organisation and accounts")
    remove.add_argument("--yes", action="store_true", help="do not ask")
    remove.set_defaults(func=cmd_delete)
    return parser


async def _main(args: argparse.Namespace) -> int:
    try:
        return int(await args.func(args))
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())

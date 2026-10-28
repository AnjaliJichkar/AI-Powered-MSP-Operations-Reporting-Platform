"""The Account block on a ticket: the contact's Microsoft 365 account.

The enrichment is the real one; only Microsoft 365 is faked, at the tool
boundary, so what is tested is the matching (by email and nothing looser),
the readings the block is made of, the findings they produce, the sentence a
customer with no Microsoft 365 gets instead — not assessed, never a gap — and
that a franchisee sees the block exactly where they already see the readings.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.connectors.factory as factory_module
import app.tickets.enrich as enrich_module
from app.audit.recorder import AuditContext
from app.autopilot.findings import findings_for
from app.connectors.microsoft365.tools import AccountState, NotAvailableRow
from app.models import OrganisationKind, Role
from app.models.enums import ConnectorName, Interface
from app.models.site import Site
from app.models.ticket import TicketWorkspace
from app.models.tool_mapping import ToolMapping
from app.tickets.enrich import enrich
from tests.api.conftest import Make, sign_in

pytestmark = pytest.mark.usefixtures("clean")

NOW = dt.datetime(2026, 10, 13, 9, tzinfo=dt.UTC)
ADDRESS = "sarah.chen@acmedental.example"
TENANT = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
async def world(
    make_client: Make,
    make_org: Make,
    make_user: Make,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """A client whose customer has a Microsoft 365 tenant mapped and no
    credential: the shape a franchisee is in before consent is granted."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    monkeypatch.setattr(factory_module, "session_scope", scope)
    acme = await make_client("acme-it", "Acme IT")
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    acme_org = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=acme)
    async with session_maker() as session:
        dental = Site(client_id=acme.id, slug="acme-dental", name="Acme Dental")
        session.add(dental)
        await session.flush()
        session.add(
            ToolMapping(
                client_id=acme.id,
                site_id=dental.id,
                connector_name=ConnectorName.MICROSOFT365,
                external_id=TENANT,
            )
        )
        workspace = TicketWorkspace(
            client_id=acme.id,
            site_id=dental.id,
            ticket_id="4242",
            ticket_number="T4242",
            title="Cannot sign in to Outlook",
            description="She has been locked out since Monday.",
            contact="Sarah Chen",
            contact_email=ADDRESS,
            status="New",
            ticket_created_at=NOW - dt.timedelta(days=1),
        )
        session.add(workspace)
        await session.commit()
        ids = {"site_id": dental.id, "workspace_id": workspace.id}
    return {
        "acme": acme,
        **ids,
        "staff": await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER),
        "admin": await make_user(acme_org, "boss@acme.test", Role.FRANCHISEE_ADMIN),
        "tech": await make_user(acme_org, "tech@acme.test", Role.FRANCHISEE_TECHNICIAN),
        "viewer": await make_user(acme_org, "viewer@acme.test", Role.FRANCHISEE_VIEWER),
    }


def an_account(**over: Any) -> AccountState:
    fields: dict[str, Any] = {
        "found": True,
        "user": ADDRESS,
        "display_name": "Sarah Chen",
        "enabled": True,
        "account_type": "Member",
        "synced_from_active_directory": False,
        "last_sign_in": (NOW - dt.timedelta(days=2)).isoformat(),
        "days_since_sign_in": 2,
        "mfa_registered": True,
        "mfa_methods": ["authenticator app"],
        "licences": ["Microsoft 365 Business Premium"],
        "groups": ["All staff"],
        "job_title": "Practice Manager",
        "department": "Reception",
        "mailbox_kind": "user",
        "read_at": NOW.isoformat(),
    }
    fields.update(over)
    return AccountState(**fields)


@pytest.fixture
def microsoft365(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Microsoft 365 faked at the tool, and told to be configured. Its knobs:
    the account to answer with, and what it was asked."""
    knobs: dict[str, Any] = {"account": an_account(), "asked": [], "no_access": None}

    async def fake_account(client_id: str, user: str, site_id: str | None = None) -> AccountState:
        knobs["asked"].append((user, site_id))
        answer = knobs["account"]
        assert isinstance(answer, AccountState)
        return answer

    async def fake_why(client_id: Any, connector: Any, site_id: Any = None) -> str | None:
        reason: str | None = knobs["no_access"]
        return reason

    monkeypatch.setattr(enrich_module, "get_microsoft365_account", fake_account)
    monkeypatch.setattr(enrich_module, "why_not_configured", fake_why)
    return knobs


async def read_facts(
    session_maker: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    *,
    contact_email: str | None = ADDRESS,
) -> dict[str, Any]:
    """The enrichment for a ticket with no machine: the Account block alone."""
    context = AuditContext.new(
        actor_id="tests",
        interface=Interface.API,
        client_id=world["acme"].id,
        client_ref="acme-it",
    )
    async with session_maker() as session:
        result = await enrich(
            session,
            context,
            client_id=world["acme"].id,
            device=None,
            links=[],
            contact="Sarah Chen",
            contact_email=contact_email,
            site_id=world["site_id"],
            ticket_number="T4242",
            now=NOW,
        )
        await session.commit()
    return result.as_dict()


def by_id(payload: dict[str, Any]) -> dict[str, Any]:
    return {fact["id"]: fact for fact in payload["facts"]}


def account_errors(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Only Microsoft 365's own failures. This client has no Autotask
    credential either, and the contact's ticket history says so on its own
    line: that is the panel working, not the block under test."""
    return [row for row in payload["errors"] if "microsoft365" in row["source"]]


# --- the readings ---------------------------------------------------------------------


async def test_the_account_is_matched_by_the_contacts_address(
    world: dict[str, Any],
    microsoft365: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """By email and nothing looser: a name matches several people in a
    directory, and the tenant asked is the ticket's own customer."""
    payload = await read_facts(session_maker, world)
    assert microsoft365["asked"] == [(ADDRESS, str(world["site_id"]))]
    facts = by_id(payload)
    assert facts["account.found"]["value"] == ADDRESS
    assert facts["account.found"]["detail"] == "Sarah Chen · Practice Manager · Reception"
    assert facts["account.enabled"]["value"] is True
    assert facts["account.mfa_registered"]["value"] is True
    assert facts["account.mfa_registered"]["detail"] == "authenticator app"
    assert facts["account.sign_in_days"]["value"] == 2
    assert facts["account.sign_in_days"]["detail"] == "last signed in 2026-10-11"
    assert facts["account.licences"]["value"] == ["Microsoft 365 Business Premium"]
    assert all(fact["source"] == "get_microsoft365_account" for fact in facts.values())
    # Nothing exceptional, so the two conditional readings are absent.
    assert "account.mailbox_kind" not in facts and "account.unlicensed_member" not in facts
    assert account_errors(payload) == []


async def test_a_ticket_with_no_address_asks_microsoft_nothing(
    world: dict[str, Any],
    microsoft365: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Autotask holds no address for every contact. No address is no match, and
    a lookup by name is not offered as a substitute."""
    payload = await read_facts(session_maker, world, contact_email=None)
    assert microsoft365["asked"] == []
    assert [fact["id"] for fact in payload["facts"] if fact["id"].startswith("account.")] == []


async def test_an_address_in_no_tenant_is_an_answer_not_an_error(
    world: dict[str, Any],
    microsoft365: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    microsoft365["account"] = AccountState(found=False, user=ADDRESS, read_at=NOW.isoformat())
    payload = await read_facts(session_maker, world)
    facts = by_id(payload)
    assert facts["account.found"]["value"] is False
    assert ADDRESS in (facts["account.found"]["detail"] or "")
    assert account_errors(payload) == []
    assert len(facts) == 1  # nothing is asserted about an account we do not have


async def test_what_the_tenant_cannot_answer_is_said_under_the_reading(
    world: dict[str, Any],
    microsoft365: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant with no premium licence has no sign-in activity and a disabled
    account has no row in the registration report. Both are sentences under
    the reading they belong to — never a zero, and never "no MFA"."""
    microsoft365["account"] = an_account(
        enabled=False,
        mfa_registered=None,
        mfa_methods=[],
        last_sign_in=None,
        days_since_sign_in=None,
        not_available=[
            NotAvailableRow(
                item="last sign-in", reason="this tenant has no Microsoft Entra ID P1 licence"
            ),
            NotAvailableRow(
                item="MFA registration",
                reason="Graph's registration report leaves out disabled accounts",
            ),
        ],
    )
    facts = by_id(await read_facts(session_maker, world))
    assert facts["account.mfa_registered"]["value"] is None
    assert "disabled accounts" in facts["account.mfa_registered"]["detail"]
    assert facts["account.sign_in_days"]["value"] is None
    assert "P1" in facts["account.sign_in_days"]["detail"]
    assert facts["account.enabled"]["value"] is False


async def test_a_shared_mailbox_is_not_an_unlicensed_member(
    world: dict[str, Any],
    microsoft365: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A shared mailbox with no licence is correct, and the reading that says
    which it is appears only because it is the exception."""
    microsoft365["account"] = an_account(
        licences=[], mailbox_kind="shared", unlicensed_member=False
    )
    facts = by_id(await read_facts(session_maker, world))
    assert facts["account.mailbox_kind"]["value"] == "shared"
    assert "account.unlicensed_member" not in facts
    assert facts["account.licences"]["detail"] == "nothing is assigned to this account"


async def test_a_client_with_no_microsoft_365_is_not_assessed(
    world: dict[str, Any],
    microsoft365: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The real reason, from the real factory: this client has a tenant mapped
    and no credential, which is something we were never able to look at rather
    than a failed read. The tool is not called at all."""
    microsoft365.pop("no_access")
    monkey = enrich_module.why_not_configured
    assert monkey is not None  # the fixture replaced it; put the real one back
    import app.connectors.factory as factory

    enrich_module.why_not_configured = factory.why_not_configured  # type: ignore[assignment]
    try:
        payload = await read_facts(session_maker, world)
    finally:
        enrich_module.why_not_configured = monkey  # type: ignore[assignment]
    facts = by_id(payload)
    assert microsoft365["asked"] == []
    assert account_errors(payload) == []
    not_assessed = facts["account.not_assessed"]
    assert not_assessed["value"] is None
    assert "not assessed" in not_assessed["detail"]
    assert "no microsoft365 credentials" in not_assessed["detail"]
    assert not_assessed["tone"] == "neutral"


# --- the findings the readings produce -------------------------------------------------


async def test_the_readings_produce_the_account_findings(
    world: dict[str, Any],
    microsoft365: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The card's findings are read from these facts, so the panel and the card
    cannot say different things about one account."""
    microsoft365["account"] = an_account(
        enabled=True,
        mfa_registered=False,
        mfa_methods=[],
        last_sign_in=(NOW - dt.timedelta(days=41)).isoformat(),
        days_since_sign_in=41,
        licences=[],
        unlicensed_member=True,
    )
    payload = await read_facts(session_maker, world)
    values = {fact["id"]: fact["value"] for fact in payload["facts"]}
    found = findings_for(values, read_at=NOW, now=NOW)
    assert [one.kind for one in found] == [
        "no_mfa_registered",
        "account_dormant",
        "unlicensed_account",
    ]
    assert all(one.source == "Microsoft 365" and one.as_of == NOW.isoformat() for one in found)
    # And the dots the panel draws agree with them.
    tones = {fact["id"]: fact["tone"] for fact in payload["facts"]}
    assert tones["account.mfa_registered"] == "warning"
    assert tones["account.sign_in_days"] == "warning"


async def test_a_disabled_account_is_the_first_thing_said(
    world: dict[str, Any],
    microsoft365: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    microsoft365["account"] = an_account(enabled=False, mfa_registered=None, mfa_methods=[])
    payload = await read_facts(session_maker, world)
    values = {fact["id"]: fact["value"] for fact in payload["facts"]}
    found = findings_for(values, read_at=NOW, now=NOW)
    assert [one.kind for one in found] == ["account_disabled"]
    assert found[0].rank == 1


# --- who sees it ----------------------------------------------------------------------


async def test_a_franchisee_sees_the_block_where_they_see_the_readings(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    microsoft365: dict[str, Any],
) -> None:
    """The Account block is part of the tool readings and is gated exactly as
    they are: the administrator and the technician compute and read it, and the
    viewer, who sees no readings at all, is refused the workspace."""
    workspace_id = world["workspace_id"]
    for email in ("boss@acme.test", "tech@acme.test"):
        session = await sign_in(http, email)
        refreshed = await session.post(f"/tickets/workspaces/{workspace_id}/enrich")
        assert refreshed.status_code == 200, refreshed.text
        facts = {f["id"]: f for f in refreshed.json()["enrichment"]["facts"]}
        assert facts["account.found"]["value"] == ADDRESS
        read = await http.get(f"/tickets/workspaces/{workspace_id}")
        assert read.status_code == 200
        assert "account.found" in {f["id"] for f in read.json()["enrichment"]["facts"]}

    viewer = await sign_in(http, "viewer@acme.test")
    assert (await http.get(f"/tickets/workspaces/{workspace_id}")).status_code == 403
    assert (await viewer.post(f"/tickets/workspaces/{workspace_id}/enrich")).status_code == 403


async def test_another_clients_workspace_is_still_a_404(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    microsoft365: dict[str, Any],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    beta = await make_client("beta-msp", "Beta MSP")
    beta_org = await make_org(OrganisationKind.FRANCHISEE, "beta-people", client=beta)
    await make_user(beta_org, "boss@beta.test", Role.FRANCHISEE_ADMIN)
    other = await sign_in(http, "boss@beta.test")
    url = f"/tickets/workspaces/{world['workspace_id']}"
    assert (await http.get(url)).status_code == 404
    assert (await other.post(f"{url}/enrich")).status_code == 404
    assert (await http.get(f"/tickets/workspaces/{uuid.uuid4()}")).status_code == 404

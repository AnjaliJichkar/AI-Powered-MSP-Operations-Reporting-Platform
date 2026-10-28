"""The ticket workspace against the real application and a real database.

Autotask, NinjaOne and the model are faked at the tool boundary: what is
tested is the workspace itself — who may open one, the device found by
hostname and by contact with its method and confidence, the scrubbing before
storage, a plan whose uncited steps are dropped and whose commands come only
from a cited ticket, versioning with the author, the review and the outcome
with their audit rows, and that another client's workspace is a 404.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.tickets as tickets_api
import app.tickets.plan as plan_module
import app.tickets.workspace as workspace_module
from app.connectors.autotask.tools import (
    TicketDetail,
    TicketMatch,
    TicketSearchResult,
    TicketSummary,
)
from app.connectors.ninjaone.tools import LoggedOnUserList, LoggedOnUserRow
from app.models import AuditEventType, OrganisationKind, Role
from app.models.device import Device, DeviceVendorLink
from app.models.enums import ConnectorName
from app.models.site import Site
from app.models.ticket import TicketPlan, TicketWorkspace
from app.reports.tbr.config import BusinessHours, FranchiseeConfig, save_config
from app.resolved.config import ResolvedConfig, SlaConfig, SlaTarget
from app.tickets.enrich import Enrichment, Fact
from tests.api.conftest import Make, audit_rows, sign_in

pytestmark = pytest.mark.usefixtures("clean")

NOW = dt.datetime(2026, 9, 25, 9, tzinfo=dt.UTC)


def _device(client_id: uuid.UUID, site_id: uuid.UUID, name: str) -> Device:
    return Device(
        id=uuid.uuid4(),
        client_id=client_id,
        site_id=site_id,
        canonical_name=name,
        netbios_key=name.lower()[:15],
        serials=[],
        macs=[],
        node_class="WINDOWS_WORKSTATION",
        status="active",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


@pytest.fixture
async def world(
    make_client: Make,
    make_org: Make,
    make_user: Make,
    session_maker: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    acme = await make_client("acme-it", "Acme IT")
    beta = await make_client("beta-msp", "Beta MSP")
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    acme_org = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=acme)
    beta_org = await make_org(OrganisationKind.FRANCHISEE, "beta-people", client=beta)
    async with session_maker() as session:
        dental = Site(client_id=acme.id, slug="acme-dental", name="Acme Dental")
        legal = Site(client_id=beta.id, slug="beacon-legal", name="Beacon Legal")
        session.add_all([dental, legal])
        await session.flush()
        ws14 = _device(acme.id, dental.id, "TL-AUS-WS14")
        ws22 = _device(acme.id, dental.id, "TL-AUS-WS22")
        session.add_all([ws14, ws22])
        await session.flush()
        session.add_all(
            [
                DeviceVendorLink(
                    client_id=acme.id,
                    device_id=ws14.id,
                    connector_name=ConnectorName.NINJAONE,
                    external_id="101",
                    vendor_name="TL-AUS-WS14",
                    matched_by="ninjaone",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                ),
                DeviceVendorLink(
                    client_id=acme.id,
                    device_id=ws22.id,
                    connector_name=ConnectorName.NINJAONE,
                    external_id="102",
                    vendor_name="TL-AUS-WS22",
                    matched_by="ninjaone",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                ),
            ]
        )
        # Acme's own SLA settings: what the queue's clocks are judged against.
        await save_config(
            session,
            acme.id,
            FranchiseeConfig(
                display_name="Acme IT",
                business_hours=BusinessHours(timezone="UTC"),
                resolved=ResolvedConfig(
                    sla=SlaConfig(
                        default=SlaTarget(first_response_minutes=60, resolution_minutes=480),
                        clock="calendar",
                    )
                ),
            ),
            updated_by="tests",
        )
        await session.commit()
        ids = {"ws14": ws14.id, "ws22": ws22.id, "dental": dental.id}
    return {
        "acme": acme,
        "beta": beta,
        **ids,
        "staff": await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER),
        "acme_admin": await make_user(acme_org, "boss@acme.test", Role.FRANCHISEE_ADMIN),
        "acme_viewer": await make_user(acme_org, "viewer@acme.test", Role.FRANCHISEE_VIEWER),
        "beta_admin": await make_user(beta_org, "boss@beta.test", Role.FRANCHISEE_ADMIN),
    }


# --- the fakes ------------------------------------------------------------------------

TICKETS: dict[str, dict[str, Any]] = {
    "T1": {
        "title": "TL-AUS-WS14 Outlook keeps crashing on start",
        "description": "Outlook closes at once. Her password is Winter2026$ if you need it.",
        "contact": "Sarah O'Connor",
        "open": True,
    },
    "T2": {
        "title": "Printer offline again",
        "description": "Cannot print to the lobby printer since yesterday.",
        "contact": "Sarah O'Connor",
        "open": True,
    },
    "T3": {
        "title": "Outlook crashing on start",
        "description": "Same as before.",
        "contact": None,
        "open": True,
    },
}
RESOLVED = {
    "T0": {
        "title": "Outlook crashing at start after update",
        "description": "Outlook crashed at start for the user.",
        "resolution": "Started Outlook in safe mode: outlook.exe /safe, then disabled the add-in.",
    }
}


def _summary(number: str, *, is_open: bool) -> TicketSummary:
    return TicketSummary(
        site="Acme Dental",
        site_slug="acme-dental",
        ticket_number=number,
        title=TICKETS.get(number, RESOLVED.get(number, {})).get("title"),
        status="New" if is_open else "Complete",
        priority="Medium",
        assigned_to="Dan",
        created_at=NOW - dt.timedelta(days=2),
        age_days=2.0,
        is_open=is_open,
    )


@pytest.fixture
def vendors(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Autotask, NinjaOne and the enrichment replaced with fakes; returns their knobs."""
    knobs: dict[str, Any] = {"closed": set(), "searches": [], "logged_on": []}

    async def fake_get_ticket(
        client_id: str, ticket_number: str, site_id: str | None = None
    ) -> Any:
        source = TICKETS.get(ticket_number) or RESOLVED.get(ticket_number)
        if source is None:
            return TicketDetail(
                client_id=client_id, sites_covered=[], found=False, message="no such ticket"
            )
        is_open = ticket_number in TICKETS and ticket_number not in knobs["closed"]
        return TicketDetail(
            client_id=client_id,
            sites_covered=["acme-dental"],
            found=True,
            ticket=_summary(ticket_number, is_open=is_open),
            description=source.get("description"),
            resolution=source.get("resolution"),
            contact=source.get("contact"),
        )

    async def fake_logged_on(
        client_id: str, user: str | None = None, site_id: str | None = None
    ) -> Any:
        rows = [
            LoggedOnUserRow(
                site="Acme Dental", site_slug="acme-dental", device=device, user_name=name
            )
            for device, name in knobs["logged_on"]
        ]
        return LoggedOnUserList(
            client_id=client_id,
            sites_covered=["acme-dental"],
            rows=rows,
            total=len(rows),
            returned=len(rows),
        )

    async def fake_search(client_id: str, query: str, **kwargs: Any) -> Any:
        knobs["searches"].append(query)
        hits = (
            [
                TicketMatch(
                    site="Acme Dental",
                    site_slug="acme-dental",
                    ticket_number="T0",
                    title=RESOLVED["T0"]["title"],
                    status="Complete",
                    created_at=NOW - dt.timedelta(days=40),
                    completed_at=NOW - dt.timedelta(days=39),
                    matched_in=["title"],
                    snippet="Outlook crashing",
                    score=3,
                )
            ]
            if "outlook" in query.lower()
            else []
        )
        return TicketSearchResult(
            client_id=client_id,
            sites_covered=["acme-dental"],
            query=query,
            searched=["title"],
            order_by="relevance",
            ranking_note="",
            tickets=hits,
            total=len(hits),
            returned=len(hits),
        )

    async def fake_enrich(session: Any, context: Any, **kwargs: Any) -> Enrichment:
        device = kwargs.get("device")
        facts = (
            [Fact("device.needs_reboot", "Pending reboot", True, "get_device")] if device else []
        )
        return Enrichment(as_of=kwargs.get("now") or NOW, facts=facts, reads=1 if device else 0)

    class FakeOpenList:
        def __init__(self, tickets: list[str]) -> None:
            self.tickets = tickets

    @asynccontextmanager
    async def fake_connector_for(client_id: Any, connector: Any, site_id: Any = None) -> Any:
        class FakeAutotask:
            async def list_tickets(self, **kwargs: Any) -> Any:
                if str(client_id) != str(knobs["acme_id"]):
                    return [], False
                site = SimpleNamespace(name="Acme Dental", slug="acme-dental")
                rows = []
                for index, (number, source) in enumerate(TICKETS.items(), start=1):
                    if number in knobs["closed"]:
                        continue
                    rows.append(
                        (
                            site,
                            SimpleNamespace(
                                id=index,
                                ticket_number=number,
                                title=source["title"],
                                priority=1,
                                status=1,
                                assigned_resource_id=7,
                                create_date=NOW - dt.timedelta(days=index),
                                age_days=lambda now, index=index: float(index),
                                due_date_time=(
                                    dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
                                    if number == "T1"
                                    else None
                                ),
                                first_response_due=None,
                                first_response_at=None,
                                resolved_due=None,
                                resolved_at=None,
                                # The SLA calculation measures from created to
                                # completed, so the fake carries both.
                                completed_date=None,
                                queue_id=None,
                                issue_type=None,
                            ),
                        )
                    )
                return rows, False

            async def get_resources(self, ids: list[int]) -> dict[int, Any]:
                return {7: SimpleNamespace(name="Dan")}

            async def label_for(self, entity: str, field: str, value: Any) -> str:
                return {"priority": "High", "status": "New"}[field]

        yield FakeAutotask()

    monkeypatch.setattr(workspace_module, "get_ticket", fake_get_ticket)
    monkeypatch.setattr(workspace_module, "list_logged_on_users", fake_logged_on)
    monkeypatch.setattr(workspace_module, "enrich", fake_enrich)
    monkeypatch.setattr(workspace_module, "connector_for", fake_connector_for)
    monkeypatch.setattr(plan_module, "search_tickets", fake_search)
    monkeypatch.setattr(plan_module, "get_ticket", fake_get_ticket)
    return knobs


def _block(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A model that searches once and writes a plan; the steps are set per test."""
    knobs: dict[str, Any] = {
        "steps": [],
        "query": "Outlook crashes on start safe mode",
        "calls": 0,
        "briefs": [],
    }

    class Messages:
        async def create(self, **kwargs: Any) -> Any:
            knobs["calls"] += 1
            knobs["briefs"].append(kwargs["messages"][0]["content"])
            return SimpleNamespace(
                content=[
                    _block("server_tool_use", name="web_search", input={"query": knobs["query"]}),
                    _block(
                        "web_search_tool_result",
                        content=[
                            SimpleNamespace(
                                url="https://learn.microsoft.com/outlook/safe-mode",
                                title="Start Outlook in safe mode",
                            ),
                            SimpleNamespace(url="https://evil.example/outlook", title="nope"),
                        ],
                    ),
                    _block("tool_use", name="write_plan", input={"steps": knobs["steps"]}),
                ],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
            )

    client = SimpleNamespace(messages=Messages())
    monkeypatch.setattr(tickets_api, "get_model_client", lambda: client)
    return knobs


STEPS = [
    {
        "text": "Start Outlook in safe mode, as the earlier ticket did.",
        "cites": ["ticket:T0"],
        "command": "outlook.exe /safe",
    },
    {
        "text": "Reboot first: a reboot is pending on the machine.",
        "cites": ["fact:device.needs_reboot"],
        "command": "shutdown /r /t 0",
    },
    {
        "text": "Repair the Office installation.",
        "cites": ["url:https://learn.microsoft.com/outlook/safe-mode"],
    },
    {"text": "Reinstall Windows.", "cites": []},
    {"text": "Also try this.", "cites": ["url:https://evil.example/outlook"]},
]


# --- who may, and the list -----------------------------------------------------------


async def test_viewers_are_refused_and_admins_and_staff_see_the_list(
    http: httpx.AsyncClient, world: dict[str, Any], vendors: dict[str, Any]
) -> None:
    vendors["acme_id"] = world["acme"].id
    viewer = await sign_in(http, "viewer@acme.test")
    assert (await http.get("/tickets/list")).status_code == 403
    assert (
        await viewer.post("/tickets/workspaces", json={"ticket_number": "T1"})
    ).status_code == 403
    page = await http.get("/tickets", headers={"Accept": "text/html"})
    assert page.status_code == 200 and 'href="/tickets"' not in page.text

    await sign_in(http, "boss@acme.test")
    listed = await http.get("/tickets/list")
    assert listed.status_code == 200, listed.text
    rows = listed.json()["tickets"]
    assert [t["ticket_number"] for t in rows] == ["T3", "T2", "T1"]  # oldest first
    t1 = rows[-1]
    assert (t1["priority"], t1["assigned_to"], t1["site"]) == ("High", "Dan", "Acme Dental")
    # Days old and never answered, against an hour to answer: breached, and the
    # detail says which clock ran out rather than naming an Autotask field.
    assert t1["sla"]["state"] == "breached"
    assert "no first answered yet" in t1["sla"]["detail"]
    assert t1["sla"]["clocks"]["first_response"]["state"] == "breached"
    # A ticket nobody has opened a workspace for still carries a judged clock:
    # the word comes from the franchisee's targets, not from a stored workspace.
    assert rows[0]["workspace_id"] is None
    assert set(rows[0]["sla"]["clocks"]) == {"first_response", "resolution"}
    page = await http.get("/tickets", headers={"Accept": "text/html"})
    # The nav offers Tickets, and the page is the queue: the tabs that narrow
    # it, the list itself, and the figures behind their own link. A workspace is
    # not a card on this page — it is `/tickets/<number>`, its own page
    # (`tests/api/test_ticket_page.py`).
    assert 'href="/tickets"' in page.text
    assert 'id="queue-tabs"' in page.text  # All, Needs review, Warnings
    assert 'id="tickets"' in page.text  # the rows
    assert 'id="search"' in page.text and 'id="filters-list"' in page.text
    assert 'id="stats-panel"' in page.text  # "Where the work is", over the page
    assert 'id="how"' in page.text  # and the explanation behind one link

    staff = await sign_in(http, "engineer@inputiv.test")
    assert (await http.get("/tickets/list")).status_code == 409  # no client yet
    await staff.post("/auth/switch-client", json={"client": "acme-it"})
    assert (await http.get("/tickets/list")).status_code == 200


# --- opening: the device, the scrubbing --------------------------------------------


async def test_a_workspace_finds_the_device_by_hostname_and_scrubs_the_text(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    vendors: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    vendors["acme_id"] = world["acme"].id
    admin = await sign_in(http, "boss@acme.test")
    opened = await admin.post("/tickets/workspaces", json={"ticket_number": "T1"})
    assert opened.status_code == 201, opened.text
    data = opened.json()
    assert data["device"]["device"] == "TL-AUS-WS14"
    assert (data["device"]["method"], data["device"]["confidence"]) == ("hostname", "high")
    assert data["device"]["candidates"][0]["why"].startswith("'TL-AUS-WS14' in the ticket")
    assert data["device"]["hostnames"] == ["TL-AUS-WS14"]
    # The password never reached the table; the page is told something was removed.
    assert "Winter2026" not in data["ticket"]["description"]
    assert "[redacted]" in data["ticket"]["description"] and data["ticket"]["scrubbed"] == 1
    async with session_maker() as session:
        stored = (await session.execute(select(TicketWorkspace))).scalar_one()
        assert "Winter2026" not in (stored.description or "")
    # The facts panel was computed as of a time, for the device.
    assert (
        data["enrichment"]["as_of"]
        and data["enrichment"]["facts"][0]["id"] == "device.needs_reboot"
    )
    # Opening again refreshes the same workspace rather than making a second.
    again = await admin.post("/tickets/workspaces", json={"ticket_number": "T1"})
    assert again.json()["id"] == data["id"]
    assert (await http.get("/tickets/list")).json()["tickets"][-1]["workspace_id"] == data["id"]
    # An unknown ticket is a 404 with the vendor's sentence.
    assert (
        await admin.post("/tickets/workspaces", json={"ticket_number": "T404"})
    ).status_code == 404


async def test_the_device_by_the_contacts_last_logon_and_the_engineers_correction(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    vendors: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    vendors["acme_id"] = world["acme"].id
    vendors["logged_on"] = [("TL-AUS-WS22", "ACME\\soconnor"), ("TL-AUS-WS14", "jdoe")]
    admin = await sign_in(http, "boss@acme.test")
    data = (await admin.post("/tickets/workspaces", json={"ticket_number": "T2"})).json()
    assert data["device"]["device"] == "TL-AUS-WS22"
    assert (data["device"]["method"], data["device"]["confidence"]) == ("contact", "medium")
    assert "last logged on as ACME\\soconnor" in data["device"]["candidates"][0]["why"]

    # Two machines with her logon: nobody's guess, both listed at low.
    vendors["logged_on"] = [("TL-AUS-WS22", "ACME\\soconnor"), ("TL-AUS-WS14", "sarah.oconnor")]
    data = (await admin.post("/tickets/workspaces", json={"ticket_number": "T2"})).json()
    assert data["device"]["device"] is None and data["device"]["method"] == "none"
    assert sorted(c["name"] for c in data["device"]["candidates"]) == ["TL-AUS-WS14", "TL-AUS-WS22"]
    assert {c["confidence"] for c in data["device"]["candidates"]} == {"low"}

    # The engineer chooses; the choice is recorded, audited, and survives a reopen.
    chosen = await admin.post(
        f"/tickets/workspaces/{data['id']}/device",
        json={"device_id": str(world["ws14"]), "note": "she moved desks last week"},
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["device"]["device"] == "TL-AUS-WS14"
    assert (chosen.json()["device"]["method"], chosen.json()["device"]["confidence"]) == (
        "engineer",
        "high",
    )
    reopened = (await admin.post("/tickets/workspaces", json={"ticket_number": "T2"})).json()
    assert (
        reopened["device"]["device"] == "TL-AUS-WS14" and reopened["device"]["method"] == "engineer"
    )
    rows = await audit_rows(session_maker, event_type=AuditEventType.TICKET_DEVICE_CHOSEN)
    assert len(rows) == 1 and rows[0].actor_user_id == world["acme_admin"].id
    assert rows[0].payload["device_id"] == str(world["ws14"])
    assert rows[0].payload["note"] == "she moved desks last week"
    # Another client's device cannot be chosen.
    refused = await admin.post(
        f"/tickets/workspaces/{data['id']}/device", json={"device_id": str(uuid.uuid4())}
    )
    assert refused.status_code == 404


# --- plans ---------------------------------------------------------------------------


async def test_a_plan_keeps_cited_steps_shows_commands_only_from_sources_and_is_versioned(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    vendors: dict[str, Any],
    model: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    vendors["acme_id"] = world["acme"].id
    model["steps"] = STEPS
    admin = await sign_in(http, "boss@acme.test")
    workspace = (await admin.post("/tickets/workspaces", json={"ticket_number": "T1"})).json()
    made = await admin.post(f"/tickets/workspaces/{workspace['id']}/plans")
    assert made.status_code == 201, made.text
    plan = made.json()
    assert plan["version"] == 1 and plan["author"] == "boss@acme.test"
    assert plan["steps_written"] == 5 and plan["dropped"] == 2
    texts = [s["text"] for s in plan["steps"]]
    assert texts == [
        "Start Outlook in safe mode, as the earlier ticket did.",
        "Reboot first: a reboot is pending on the machine.",
        "Repair the Office installation.",
    ]
    # The command from the resolved ticket is shown and sourced; the invented one is not.
    assert plan["steps"][0]["command"] == "outlook.exe /safe"
    assert plan["steps"][0]["command_source"] == "ticket:T0"
    assert plan["steps"][1]["command"] is None
    assert plan["steps"][1]["execution"] == "manual, not executed"
    assert (
        plan["steps"][2]["citations"][0]["url"] == "https://learn.microsoft.com/outlook/safe-mode"
    )
    assert plan["citations"] == 3 and plan["web_searches"] == [model["query"]]
    assert plan["evidence"]["counts"] == {"resolved_step": 0, "ticket": 1, "fact": 1}
    # The model saw the scrubbed ticket, never the password.
    assert "Winter2026" not in model["briefs"][0] and "[redacted]" in model["briefs"][0]
    assert "ticket:T0" in model["briefs"][0] and "outlook.exe /safe" in model["briefs"][0]
    # The audit trail: the prompt, the search, the answer, the plan.
    kinds = [
        r.event_type
        for r in await audit_rows(session_maker)
        if r.event_type
        in (
            AuditEventType.PROMPT,
            AuditEventType.WEB_SEARCH,
            AuditEventType.MODEL_RESPONSE,
            AuditEventType.TICKET_PLAN_GENERATED,
        )
    ]
    assert kinds == [
        AuditEventType.PROMPT,
        AuditEventType.WEB_SEARCH,
        AuditEventType.MODEL_RESPONSE,
        AuditEventType.TICKET_PLAN_GENERATED,
    ]
    generated = (await audit_rows(session_maker, event_type=AuditEventType.TICKET_PLAN_GENERATED))[
        0
    ]
    assert generated.payload["steps"] == 3 and generated.payload["dropped"] == 2
    assert generated.actor_user_id == world["acme_admin"].id

    # Review: accept, edit (needs text), reject; each audited.
    base = f"/tickets/workspaces/{workspace['id']}/plans/1/steps"
    assert (await admin.post(f"{base}/s1/review", json={"status": "accepted"})).status_code == 200
    assert (await admin.post(f"{base}/s2/review", json={"status": "edited"})).status_code == 422
    edited = await admin.post(
        f"{base}/s2/review", json={"status": "edited", "text": "Reboot after hours."}
    )
    assert edited.status_code == 200 and edited.json()["review"]["text"] == "Reboot after hours."
    assert (await admin.post(f"{base}/s3/review", json={"status": "rejected"})).status_code == 200
    assert (await admin.post(f"{base}/s9/review", json={"status": "accepted"})).status_code == 422
    reviews = await audit_rows(session_maker, event_type=AuditEventType.TICKET_PLAN_REVIEWED)
    assert [r.payload["status"] for r in reviews] == ["accepted", "edited", "rejected"]

    # A second generation is version 2 by whoever asked; version 1 is untouched.
    staff = await sign_in(http, "engineer@inputiv.test")
    await staff.post("/auth/switch-client", json={"client": "acme-it"})
    second = await staff.post(f"/tickets/workspaces/{workspace['id']}/plans")
    assert second.status_code == 201 and second.json()["version"] == 2
    assert second.json()["author"] == "engineer@inputiv.test"
    view = (await http.get(f"/tickets/workspaces/{workspace['id']}")).json()
    assert [p["version"] for p in view["plans"]] == [2, 1]
    assert view["plans"][1]["steps"][0]["review"]["status"] == "accepted"
    assert view["plans"][0]["steps"][0]["review"]["status"] == "proposed"


async def test_a_leaked_query_loses_the_web_and_a_plan_with_nothing_cited_is_empty(
    http: httpx.AsyncClient, world: dict[str, Any], vendors: dict[str, Any], model: dict[str, Any]
) -> None:
    vendors["acme_id"] = world["acme"].id
    model["steps"] = STEPS
    model["query"] = "Outlook crash TL-AUS-WS14 Acme Dental"  # the machine and the site
    admin = await sign_in(http, "boss@acme.test")
    workspace = (await admin.post("/tickets/workspaces", json={"ticket_number": "T1"})).json()
    plan = (await admin.post(f"/tickets/workspaces/{workspace['id']}/plans")).json()
    assert [s["text"] for s in plan["steps"]] == [
        "Start Outlook in safe mode, as the earlier ticket did.",
        "Reboot first: a reboot is pending on the machine.",
    ]
    assert plan["problems"][0].startswith("a web search query named")
    model["steps"] = [{"text": "Reinstall Windows.", "cites": []}]
    empty = (await admin.post(f"/tickets/workspaces/{workspace['id']}/plans")).json()
    assert empty["steps"] == [] and empty["dropped"] == 1 and empty["version"] == 2


# --- outcomes ------------------------------------------------------------------------


async def test_a_closed_ticket_prompts_its_engineer_and_the_outcome_ranks_the_next_plan(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    vendors: dict[str, Any],
    model: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    vendors["acme_id"] = world["acme"].id
    model["steps"] = STEPS
    admin = await sign_in(http, "boss@acme.test")
    workspace = (await admin.post("/tickets/workspaces", json={"ticket_number": "T1"})).json()
    await admin.post(f"/tickets/workspaces/{workspace['id']}/plans")
    assert (await http.get("/tickets/pending")).json() == []

    # The ticket closes in Autotask; polling notices; the author is prompted.
    vendors["closed"].add("T1")
    polled = await admin.post("/tickets/poll")
    assert polled.status_code == 200 and polled.json()["closed"] == [workspace["id"]]
    pending = (await http.get("/tickets/pending")).json()
    assert len(pending) == 1 and pending[0]["ticket_number"] == "T1" and pending[0]["version"] == 1
    assert [s["id"] for s in pending[0]["steps"]] == ["s1", "s2", "s3"]
    # Somebody else is not prompted for a plan they did not write.
    staff = await sign_in(http, "engineer@inputiv.test")
    await staff.post("/auth/switch-client", json={"client": "acme-it"})
    assert (await http.get("/tickets/pending")).json() == []

    admin = await sign_in(http, "boss@acme.test")
    url = f"/tickets/workspaces/{workspace['id']}/plans/1/outcome"
    assert (await admin.post(url, json={"resolved_steps": []})).status_code == 422
    assert (await admin.post(url, json={"resolved_steps": ["s9"]})).status_code == 422
    recorded = await admin.post(url, json={"resolved_steps": ["s1"], "note": "safe mode did it"})
    assert recorded.status_code == 200, recorded.text
    outcome = recorded.json()["outcome"]
    assert outcome["resolved_steps"] == ["s1"] and outcome["by"] == "boss@acme.test"
    assert (await http.get("/tickets/pending")).json() == []
    rows = await audit_rows(session_maker, event_type=AuditEventType.TICKET_OUTCOME_RECORDED)
    assert len(rows) == 1 and rows[0].payload["resolved_steps"] == ["s1"]
    assert rows[0].actor_user_id == world["acme_admin"].id
    async with session_maker() as session:
        stored = (await session.execute(select(TicketPlan))).scalar_one()
        assert stored.outcome["note"] == "safe mode did it"

    # The next similar ticket: the resolving step is evidence, ranked first, and the
    # model is told so. (The fake search returns T1 as a resolved ticket now.)
    async def search_with_t1(client_id: str, query: str, **kwargs: Any) -> Any:
        return TicketSearchResult(
            client_id=client_id,
            sites_covered=["acme-dental"],
            query=query,
            searched=["title"],
            order_by="relevance",
            ranking_note="",
            tickets=[
                TicketMatch(
                    site="Acme Dental",
                    site_slug="acme-dental",
                    ticket_number="T1",
                    title=TICKETS["T1"]["title"],
                    status="Complete",
                    created_at=NOW,
                    completed_at=NOW,
                    matched_in=["title"],
                    snippet="Outlook",
                    score=3,
                )
            ]
            if "outlook" in query.lower()
            else [],
            total=1,
            returned=1,
        )

    plan_module.search_tickets = search_with_t1  # type: ignore[assignment]
    model["steps"] = [
        {"text": "Start Outlook in safe mode; this resolved T1.", "cites": ["resolved:T1:s1"]}
    ]
    other = (await admin.post("/tickets/workspaces", json={"ticket_number": "T3"})).json()
    plan = (await admin.post(f"/tickets/workspaces/{other['id']}/plans")).json()
    assert plan["evidence"]["counts"]["resolved_step"] == 1
    assert plan["steps"][0]["citations"][0]["ref"] == "resolved:T1:s1"
    assert model["briefs"][-1].index("resolved:T1:s1") < model["briefs"][-1].index("ticket:T1")


# --- scope ---------------------------------------------------------------------------


async def test_another_clients_workspace_and_plan_are_out_of_reach(
    http: httpx.AsyncClient, world: dict[str, Any], vendors: dict[str, Any], model: dict[str, Any]
) -> None:
    vendors["acme_id"] = world["acme"].id
    model["steps"] = STEPS
    admin = await sign_in(http, "boss@acme.test")
    workspace = (await admin.post("/tickets/workspaces", json={"ticket_number": "T1"})).json()
    await admin.post(f"/tickets/workspaces/{workspace['id']}/plans")

    beta = await sign_in(http, "boss@beta.test")
    assert (await http.get("/tickets/list")).json()["tickets"] == []
    assert (await http.get(f"/tickets/workspaces/{workspace['id']}")).status_code == 404
    assert (await beta.post(f"/tickets/workspaces/{workspace['id']}/plans")).status_code == 404
    assert (
        await beta.post(
            f"/tickets/workspaces/{workspace['id']}/plans/1/steps/s1/review",
            json={"status": "accepted"},
        )
    ).status_code == 404
    assert (
        await beta.post(
            f"/tickets/workspaces/{workspace['id']}/plans/1/outcome",
            json={"resolved_steps": ["s1"]},
        )
    ).status_code == 404
    assert (
        await beta.post(f"/tickets/workspaces/{workspace['id']}/device", json={"device_id": None})
    ).status_code == 404
    assert (await http.get("/tickets/pending")).json() == []
    # The client never comes from the body.
    assert (
        await beta.post("/tickets/workspaces", json={"ticket_number": "T1", "client_id": "x"})
    ).status_code == 422

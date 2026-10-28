"""The Autotask report tools: counts, SLA breaches, recent tickets, contacts, assets, company.

Fixtures carry the keys the pilot tenant populates (2026-09-17). Contracts and
ConfigurationItems could not be read live — the tenant answers HTTP 500 to
every query on those entities (api-notes) — so those two are Swagger-shaped.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import pytest
import respx

import app.connectors.autotask.tools as autotask_tools
from app.agent.registry import get_tool, tools_for
from app.connectors.autotask.tools import (
    count_tickets,
    get_company,
    list_configuration_items,
    list_contacts,
    list_open_tickets,
    list_recent_tickets,
    list_sla_breaches,
    list_tickets_for_contact,
)
from app.connectors.base import ConnectorLimits
from app.connectors.errors import AuthError, NotFound, RateLimited
from app.models.enums import ConnectorName
from app.reports.tbr.config import BusinessHours
from app.resolved.config import ResolvedConfig, SlaConfig, SlaTarget
from app.resolved.sla import SlaCalculator
from tests.connectors.conftest import (
    AUTOTASK_COMPANY_ID,
    AUTOTASK_ZONE_URL,
    RecordingSleep,
    load_fixture,
    make_autotask_client,
    site_ref,
)

CLIENT_ID = str(uuid.uuid4())


def query_url(entity: str) -> str:
    return f"{AUTOTASK_ZONE_URL}/V1.0/{entity}/query"


def fields_url(entity: str) -> str:
    return f"{AUTOTASK_ZONE_URL}/V1.0/{entity}/entityInformation/fields"


@pytest.fixture
def autotask(stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    return stub_factory(
        make_autotask_client(
            limits=fast_limits, sleep=sleeper, config={"base_url": AUTOTASK_ZONE_URL}
        )
    )


@pytest.fixture(autouse=True)
def _clock(autotask_clock: dt.datetime) -> None:
    """Every tool here measures against the frozen Autotask clock."""


def mock_post(entity: str, fixture: str, status: int = 200) -> respx.Route:
    return respx.post(query_url(entity)).mock(
        return_value=httpx.Response(status, json=load_fixture("autotask", fixture))
    )


def mock_fields(entity: str = "Tickets", fixture: str = "fields_tickets.json") -> respx.Route:
    return respx.get(fields_url(entity)).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", fixture))
    )


def filters_of(route: respx.Route, index: int = 0) -> list[dict[str, Any]]:
    return json.loads(route.calls[index].request.content.decode())["filter"]


def mock_ticket_world() -> None:
    mock_fields()
    mock_post("Resources", "resources.json")


# --- count_tickets --------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    ("group_by", "expected"),
    [
        ("status", {"Complete": 1, "In Progress": 1, "New": 1}),
        ("priority", {"High": 1, "Low": 1, "Medium": 1}),
        ("site", {"Acme Dental": 3}),
        ("assignee", {"Dana Okafor": 3}),
    ],
)
async def test_count_tickets_groups_open_tickets(
    autotask: Any, group_by: str, expected: dict[str, int]
) -> None:
    mock_ticket_world()
    tickets = mock_post("Tickets", "tickets_open.json")

    result = await count_tickets(CLIENT_ID, group_by=group_by)  # type: ignore[arg-type]

    assert result.total == 3
    assert result.counts == expected
    assert result.filtered_by == {"state": "open (not completed)"}
    assert {"op": "notExist", "field": "completedDate"} in filters_of(tickets)
    assert "tickets" not in result.model_dump()  # numbers only


@respx.mock
async def test_count_tickets_with_days_counts_everything_created_in_the_window(
    autotask: Any,
) -> None:
    mock_ticket_world()
    tickets = mock_post("Tickets", "tickets_recent.json")

    result = await count_tickets(CLIENT_ID, days=7)

    applied = filters_of(tickets)
    created = next(entry for entry in applied if entry.get("field") == "createDate")
    assert created["op"] == "gte" and created["value"] == "2026-09-09T12:00:00Z"
    assert not any(entry.get("field") == "completedDate" for entry in applied)
    assert result.total == 3
    assert result.filtered_by["days"] == "7"


async def test_count_tickets_rejects_a_nonsense_window(autotask: Any) -> None:
    with pytest.raises(ValueError, match="days must be at least 1"):
        await count_tickets(CLIENT_ID, days=0)


@respx.mock
async def test_count_tickets_401(autotask: Any) -> None:
    mock_ticket_world()
    mock_post("Tickets", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await count_tickets(CLIENT_ID)


# --- list_sla_breaches ----------------------------------------------------


@pytest.fixture
def sla_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """A franchisee with an hour to answer and a working day to resolve.

    The tool judges against the franchisee's own settings, which live in our
    database; these tests have none by design, so the calculator is handed in
    the way the recorded HTTP is. What is asserted here is the vendor read, the
    row shape and the guards — `tests/resolved/` asserts the clock itself.
    """
    calculator = SlaCalculator(
        ResolvedConfig(
            sla=SlaConfig(
                default=SlaTarget(first_response_minutes=60, resolution_minutes=480),
                clock="calendar",
                paused_status_labels=["waiting customer"],
            )
        ),
        BusinessHours(timezone="UTC"),
        dt.UTC,
    )

    async def fake_calculator_for(*args: Any, **kwargs: Any) -> SlaCalculator:
        return calculator

    monkeypatch.setattr(autotask_tools, "calculator_for", fake_calculator_for)


@respx.mock
async def test_sla_breaches_judge_the_franchisees_own_targets(
    autotask: Any, sla_targets: None
) -> None:
    mock_ticket_world()
    mock_post("Tickets", "tickets_open.json")

    result = await list_sla_breaches(CLIENT_ID)

    assert result.open_tickets_checked == 3
    # Every open ticket in the fixture is older than a working day, so both of
    # its clocks are past a target of an hour and eight hours — except where a
    # first response was recorded in time, which settles that clock as met.
    assert set(result.breach_counts) <= {"first_response", "resolution"}
    assert "due" not in result.breach_counts  # not a clock this tool judges any more
    assert result.breach_counts["resolution"] == 3
    # The one answered in time is not counted as having missed its response.
    assert result.breach_counts.get("first_response", 0) == 2
    assert all(row.overdue_hours >= 0 for row in result.rows)


@respx.mock
async def test_sla_breaches_empty_queue(autotask: Any, sla_targets: None) -> None:
    mock_ticket_world()
    mock_post("Tickets", "tickets_empty.json")
    result = await list_sla_breaches(CLIENT_ID)
    assert result.total == 0 and result.open_tickets_checked == 0


@respx.mock
async def test_sla_breaches_429(autotask: Any, sla_targets: None) -> None:
    mock_ticket_world()
    mock_post("Tickets", "error_429.json", status=429)
    with pytest.raises(RateLimited):
        await list_sla_breaches(CLIENT_ID)


# --- list_recent_tickets --------------------------------------------------


@respx.mock
async def test_recent_tickets_created_this_week_newest_first(autotask: Any) -> None:
    mock_ticket_world()
    tickets = mock_post("Tickets", "tickets_recent.json")

    result = await list_recent_tickets(CLIENT_ID)

    created = next(e for e in filters_of(tickets) if e.get("field") == "createDate")
    assert created == {"op": "gte", "field": "createDate", "value": "2026-09-09T12:00:00Z"}
    assert [t.ticket_number for t in result.tickets] == [
        "T20260915.0101",
        "T20260913.0102",
        "T20260911.0103",
    ]
    assert result.status_counts == {"Complete": 1, "In Progress": 1, "New": 1}
    assert result.tickets[1].is_open is False


@respx.mock
async def test_recent_tickets_closed_filters_on_completion(autotask: Any) -> None:
    mock_ticket_world()
    tickets = mock_post("Tickets", "tickets_recent.json")

    result = await list_recent_tickets(CLIENT_ID, days=3, created_or_closed="closed")

    completed = next(e for e in filters_of(tickets) if e.get("field") == "completedDate")
    assert completed == {"op": "gte", "field": "completedDate", "value": "2026-09-13T12:00:00Z"}
    assert result.created_or_closed == "closed"


@respx.mock
async def test_recent_tickets_401(autotask: Any) -> None:
    mock_ticket_world()
    mock_post("Tickets", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await list_recent_tickets(CLIENT_ID)


# --- assigned_to on list_open_tickets --------------------------------------


@respx.mock
async def test_an_engineers_queue_is_a_filter_on_open_tickets(autotask: Any) -> None:
    mock_ticket_world()
    tickets = mock_post("Tickets", "tickets_open.json")

    result = await list_open_tickets(CLIENT_ID, assigned_to="dana")

    assert {"op": "in", "field": "assignedResourceID", "value": [501]} in filters_of(tickets)
    assert result.filtered_by["assigned_to"] == "Dana Okafor"


@respx.mock
async def test_an_unknown_engineer_lists_who_exists(autotask: Any) -> None:
    mock_ticket_world()
    mock_post("Tickets", "tickets_open.json")
    with pytest.raises(NotFound, match="Dana Okafor, Sam Ellis"):
        await list_open_tickets(CLIENT_ID, assigned_to="nobody")


# --- list_contacts ----------------------------------------------------------


@respx.mock
async def test_contacts_are_listed_with_role_and_email(autotask: Any) -> None:
    contacts = mock_post("Contacts", "contacts_company.json")

    result = await list_contacts(CLIENT_ID)

    assert [c.name for c in result.contacts] == ["Daniel Okafor", "Priya Natarajan", "Sarah Chen"]
    sarah = result.contacts[2]
    assert sarah.email == "sarah.chen@acme.example"
    assert sarah.title == "Office Manager"
    assert sarah.primary_contact is True
    assert sarah.mobile == "512-555-0199"
    # Active contacts only, and isActive is an int on this entity.
    assert {"op": "eq", "field": "isActive", "value": 1} in filters_of(contacts)


@respx.mock
async def test_contacts_name_search_is_pushed_to_the_vendor(autotask: Any) -> None:
    contacts = mock_post("Contacts", "contacts_company.json")
    result = await list_contacts(CLIENT_ID, name="sarah")
    clause = next(e for e in filters_of(contacts) if e.get("op") == "or")
    assert {"op": "contains", "field": "emailAddress", "value": "sarah"} in clause["items"]
    assert result.name_searched == "sarah"


@respx.mock
async def test_contacts_401(autotask: Any) -> None:
    mock_post("Contacts", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await list_contacts(CLIENT_ID)


# --- list_tickets_for_contact ----------------------------------------------


@respx.mock
async def test_tickets_for_contact_follow_the_matched_contacts(autotask: Any) -> None:
    mock_ticket_world()
    mock_post("Contacts", "contacts_company.json")
    tickets = mock_post("Tickets", "tickets_for_contact.json")

    result = await list_tickets_for_contact(CLIENT_ID, "chen")

    assert {"op": "in", "field": "contactID", "value": [601, 602, 603]} in filters_of(tickets)
    assert result.matched_contacts[0] == "Sarah Chen (acme-dental)"
    assert [t.ticket_number for t in result.tickets] == ["T20260915.0202", "T20260901.0201"]


@respx.mock
async def test_tickets_for_an_unknown_contact_say_so_without_a_ticket_query(
    autotask: Any,
) -> None:
    mock_post("Contacts", "contacts_empty.json")
    tickets = mock_post("Tickets", "tickets_open.json")

    result = await list_tickets_for_contact(CLIENT_ID, "nobody")

    assert result.total == 0
    assert "no contact matching 'nobody'" in (result.message or "")
    assert tickets.call_count == 0


@respx.mock
async def test_tickets_for_contact_429(autotask: Any) -> None:
    mock_post("Contacts", "error_429.json", status=429)
    with pytest.raises(RateLimited):
        await list_tickets_for_contact(CLIENT_ID, "chen")


# --- list_configuration_items ----------------------------------------------


@respx.mock
async def test_configuration_items_carry_the_psa_view_of_a_device(autotask: Any) -> None:
    mock_fields("ConfigurationItems", "fields_configuration_items.json")
    mock_post("ConfigurationItems", "configuration_items_company.json")

    result = await list_configuration_items(CLIENT_ID)

    assert [(i.name, i.type) for i in result.items] == [
        ("TL-AUS-DC01", "Server"),
        ("TL-AUS-WS14", "Workstation"),
    ]
    assert result.items[1].operating_system == "Microsoft Windows 11 Pro Edition"
    assert result.items[1].last_user == "ACME\\sarah.chen"
    assert result.items[1].serial_number == "ABC1234"


@respx.mock
async def test_configuration_items_name_search(autotask: Any) -> None:
    mock_fields("ConfigurationItems", "fields_configuration_items.json")
    route = mock_post("ConfigurationItems", "configuration_items_company.json")
    await list_configuration_items(CLIENT_ID, name="ws14")
    clause = next(e for e in filters_of(route) if e.get("op") == "or")
    assert {"op": "contains", "field": "serialNumber", "value": "ws14"} in clause["items"]


@respx.mock
async def test_configuration_items_401(autotask: Any) -> None:
    mock_post("ConfigurationItems", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await list_configuration_items(CLIENT_ID)


# --- get_company ------------------------------------------------------------


def mock_company_world() -> respx.Route:
    mock_post("Companies", "company_detail.json")
    mock_fields("Companies", "fields_companies_full.json")
    mock_fields("Contracts", "fields_contracts.json")
    mock_post("Resources", "resources.json")
    mock_post("CompanyLocations", "company_locations.json")
    return mock_post("Contracts", "contracts.json")


@respx.mock
async def test_the_company_card(autotask: Any) -> None:
    contracts = mock_company_world()

    result = await get_company(CLIENT_ID, site_id=str(uuid.uuid4()))

    assert result.found is True
    assert result.name == "Acme Dental"
    assert result.company_type == "Customer"
    assert result.classification == "Managed Services"
    # The industry is the market segment; `classification` is only a sales grade.
    assert result.industry == "Legal Services"
    assert result.locations == ["Head Office — Austin, TX", "Warehouse — Round Rock, TX"]
    assert result.address == "100 Main St, Suite 200, Austin, TX, 78701"
    assert result.account_manager == "Dana Okafor"
    assert result.active_contracts == 2  # 8002 is inactive
    assert result.contracts_note is None
    assert {"op": "eq", "field": "status", "value": 1} in filters_of(contracts)
    assert {"op": "eq", "field": "companyID", "value": AUTOTASK_COMPANY_ID} in filters_of(contracts)


@respx.mock
async def test_the_company_card_reports_an_unreadable_contract_count_as_unknown(
    autotask: Any,
) -> None:
    """What the pilot tenant does: HTTP 500 on Contracts. Unknown, never zero."""
    mock_post("Companies", "company_detail.json")
    mock_fields("Companies", "fields_companies_full.json")
    mock_fields("Contracts", "fields_contracts.json")
    mock_post("Resources", "resources.json")
    mock_post("CompanyLocations", "error_500.json", status=500)  # refused too: card survives
    mock_post("Contracts", "error_500.json", status=500)

    result = await get_company(CLIENT_ID, site_id=str(uuid.uuid4()))

    assert result.found is True
    assert result.active_contracts is None
    assert "unknown, not zero" in (result.contracts_note or "")


@respx.mock
async def test_the_company_card_needs_one_site(
    stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    dental = site_ref(AUTOTASK_COMPANY_ID, slug="acme-dental", name="Acme Dental")
    legal = site_ref(78, slug="bright-legal", name="Bright Legal")
    stub_factory(
        make_autotask_client(
            limits=fast_limits,
            sleep=sleeper,
            config={"base_url": AUTOTASK_ZONE_URL},
            sites=[dental, legal],
        )
    )
    companies = mock_post("Companies", "company_detail.json")

    result = await get_company(CLIENT_ID)

    assert result.found is False
    assert "Name one end customer" in (result.message or "")
    assert companies.call_count == 0


@respx.mock
async def test_the_company_card_401(autotask: Any) -> None:
    mock_post("Companies", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await get_company(CLIENT_ID, site_id=str(uuid.uuid4()))


# --- registration -----------------------------------------------------------


def test_the_report_tools_are_registered() -> None:
    """The total is asserted once, in test_autotask_search.py."""
    names = {definition.name for definition in tools_for(ConnectorName.AUTOTASK)}
    assert names >= {
        "list_open_tickets",
        "get_ticket",
        "list_tickets_for_device",
        "list_recent_time_entries",
        "count_tickets",
        "list_sla_breaches",
        "list_recent_tickets",
        "list_tickets_for_contact",
        "list_contacts",
        "list_configuration_items",
        "get_company",
    }


def test_the_tool_count_is_monitored_not_capped() -> None:
    """There is no ceiling on the number of tools. What is held is selection:
    `scripts/selection_check.py` on real engineer questions stays at or above 90%.

    This pins the count so that adding or removing a tool is a visible, deliberate
    change: update the number here, run the selection check before and after, and
    record both in `docs/validation-log.md`.
    """
    from app.agent.registry import registered_tools

    # 16 NinjaOne, 14 Autotask, 5 Bitdefender, 5 ConnectSecure, 5 Microsoft 365,
    # 3 Cove, 2 coverage (under NinjaOne) and 1 backup coverage (under Cove)
    assert len(registered_tools()) == 51


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("count_tickets", {"group_by", "days", "site"}),
        ("list_sla_breaches", {"site"}),
        ("list_recent_tickets", {"days", "created_or_closed", "site"}),
        ("list_tickets_for_contact", {"contact_name", "site"}),
        ("list_contacts", {"name", "site"}),
        ("list_configuration_items", {"name", "site"}),
        ("get_company", {"site"}),
    ],
)
def test_each_report_tool_exposes_the_arguments_it_should(name: str, expected: set[str]) -> None:
    schema = get_tool(name).model_facing_schema()
    assert set(schema["properties"]) == expected
    assert "client_id" not in schema["properties"]
    assert len(get_tool(name).description) > 120


def test_count_tickets_offers_only_its_five_groupings() -> None:
    schema = get_tool("count_tickets").model_facing_schema()
    assert schema["properties"]["group_by"]["enum"] == [
        "status",
        "priority",
        "site",
        "assignee",
        "queue",
    ]

"""The four Autotask tools, as the agent loop will call them.

These go through `connector_for`, so they also prove a tool cannot run without
the factory resolving a client first.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.agent.registry import get_tool, tools_for
from app.connectors.autotask.tools import (
    get_ticket,
    list_open_tickets,
    list_recent_time_entries,
    list_tickets_for_device,
)
from app.connectors.base import ConnectorLimits
from app.connectors.errors import AuthError, NotFound, RateLimited
from app.models.enums import ConnectorName
from app.settings import get_settings
from tests.connectors.conftest import (
    AUTOTASK_COMPANY_ID,
    AUTOTASK_ZONE_URL,
    RecordingSleep,
    load_fixture,
    make_autotask_client,
)

CLIENT_ID = str(uuid.uuid4())

TICKETS_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Tickets/query"
NOTES_URL = f"{AUTOTASK_ZONE_URL}/V1.0/TicketNotes/query"
TIME_URL = f"{AUTOTASK_ZONE_URL}/V1.0/TimeEntries/query"
CI_URL = f"{AUTOTASK_ZONE_URL}/V1.0/ConfigurationItems/query"
RESOURCES_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Resources/query"
CONTACTS_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Contacts/query"
FIELDS_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Tickets/entityInformation/fields"


@pytest.fixture
def autotask(stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    """Route `connector_for` to an Autotask client backed by respx.

    `base_url` is pinned so the zone lookup is out of the way; the client tests
    cover zone resolution.
    """
    return stub_factory(
        make_autotask_client(
            limits=fast_limits, sleep=sleeper, config={"base_url": AUTOTASK_ZONE_URL}
        )
    )


def mock_post(url: str, fixture: str, status: int = 200) -> respx.Route:
    return respx.post(url).mock(
        return_value=httpx.Response(status, json=load_fixture("autotask", fixture))
    )


def mock_fields() -> respx.Route:
    return respx.get(FIELDS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_tickets.json"))
    )


def filters_of(route: respx.Route, index: int = 0) -> list[dict[str, Any]]:
    return json.loads(route.calls[index].request.content.decode())["filter"]


# --- list_open_tickets ----------------------------------------------------


@respx.mock
async def test_list_open_tickets_happy_path(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_fields()
    mock_post(TICKETS_URL, "tickets_open.json")
    mock_post(RESOURCES_URL, "resources.json")

    result = await list_open_tickets(CLIENT_ID)

    assert result.client_id == CLIENT_ID
    assert result.sites_covered == ["acme-dental"]
    assert result.ticket_count == 3
    assert result.truncated is False
    # Oldest first: the thing an engineer triaging wants at the top.
    assert [ticket.ticket_number for ticket in result.tickets] == [
        "T20260902.0011",
        "T20260910.0007",
        "T20260914.0003",
    ]

    oldest = result.tickets[0]
    assert oldest.title == "Outlook disconnects every morning"
    # Labels come from the tenant's picklist, not from hardcoded numbers.
    assert oldest.status == "New"
    assert oldest.priority == "High"
    assert oldest.assigned_to == "Dana Okafor"
    assert oldest.age_days == pytest.approx(14.0, abs=0.1)
    assert result.filtered_by == {"state": "open (not completed)"}


@respx.mock
async def test_list_open_tickets_passes_the_client_id_to_the_factory(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    """Hard rule 1: the scope reaches credential resolution."""
    mock_fields()
    mock_post(TICKETS_URL, "tickets_open.json")
    mock_post(RESOURCES_URL, "resources.json")

    await list_open_tickets(CLIENT_ID)

    assert autotask.calls == [(CLIENT_ID, ConnectorName.AUTOTASK)]


@respx.mock
async def test_older_than_days_narrows_the_query_and_is_reported(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    mock_fields()
    tickets = mock_post(TICKETS_URL, "tickets_open.json")
    mock_post(RESOURCES_URL, "resources.json")

    result = await list_open_tickets(CLIENT_ID, older_than_days=10)

    applied = filters_of(tickets)
    created = next(entry for entry in applied if entry.get("field") == "createDate")
    assert created["op"] == "lt"
    assert created["value"] == "2026-09-06T12:00:00Z"
    # A narrowed answer must not read as "everything open".
    assert result.filtered_by["older_than_days"] == "10"


@respx.mock
async def test_a_priority_label_is_resolved_to_this_tenants_number(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    mock_fields()
    tickets = mock_post(TICKETS_URL, "tickets_open.json")
    mock_post(RESOURCES_URL, "resources.json")

    result = await list_open_tickets(CLIENT_ID, priority="High")

    assert {"op": "eq", "field": "priority", "value": 2} in filters_of(tickets)
    assert result.filtered_by["priority"] == "High"


@respx.mock
async def test_an_unknown_priority_says_which_ones_exist(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    mock_fields()
    mock_post(TICKETS_URL, "tickets_open.json")

    with pytest.raises(NotFound, match="Critical"):
        await list_open_tickets(CLIENT_ID, priority="Urgent")


@respx.mock
async def test_a_status_label_is_resolved_to_this_tenants_number(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    mock_fields()
    tickets = mock_post(TICKETS_URL, "tickets_open.json")
    mock_post(RESOURCES_URL, "resources.json")

    result = await list_open_tickets(CLIENT_ID, status="In Progress")

    assert {"op": "eq", "field": "status", "value": 8} in filters_of(tickets)
    assert result.filtered_by["status"] == "In Progress"


@respx.mock
async def test_an_unknown_status_says_which_ones_exist(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    mock_fields()
    mock_post(TICKETS_URL, "tickets_open.json")

    with pytest.raises(NotFound, match="In Progress"):
        await list_open_tickets(CLIENT_ID, status="Waiting Customer")


@respx.mock
async def test_the_row_cap_keeps_the_oldest_and_reports_the_total(
    autotask: Any, autotask_clock: dt.datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capped list says how much it left out; the model must not call it complete."""
    monkeypatch.setenv("TOOL_MAX_ROWS", "2")
    get_settings.cache_clear()
    mock_fields()
    mock_post(TICKETS_URL, "tickets_open.json")
    mock_post(RESOURCES_URL, "resources.json")

    result = await list_open_tickets(CLIENT_ID)

    assert result.total == result.ticket_count == 3
    assert result.returned == len(result.tickets) == 2
    assert result.truncated is True
    assert result.truncation_note is not None
    assert "first 2 of 3 open tickets" in result.truncation_note
    # Oldest first, so the cap drops the newest.
    assert [ticket.ticket_number for ticket in result.tickets] == [
        "T20260902.0011",
        "T20260910.0007",
    ]


@respx.mock
async def test_list_open_tickets_empty(autotask: Any, autotask_clock: dt.datetime) -> None:
    """Hard rule 7: nothing open is an answer, not a failure."""
    mock_fields()
    mock_post(TICKETS_URL, "tickets_empty.json")

    result = await list_open_tickets(CLIENT_ID)

    assert result.ticket_count == 0
    assert result.tickets == []


@respx.mock
async def test_list_open_tickets_401(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_post(TICKETS_URL, "error_401.json", status=401)

    with pytest.raises(AuthError):
        await list_open_tickets(CLIENT_ID)


@respx.mock
async def test_list_open_tickets_429(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_post(TICKETS_URL, "error_429.json", status=429)

    with pytest.raises(RateLimited):
        await list_open_tickets(CLIENT_ID)


async def test_a_negative_age_threshold_is_rejected(autotask: Any) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        await list_open_tickets(CLIENT_ID, older_than_days=-1)


# --- get_ticket -----------------------------------------------------------


@respx.mock
async def test_get_ticket_happy_path(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_fields()
    mock_post(TICKETS_URL, "ticket_single.json")
    mock_post(NOTES_URL, "ticket_notes.json")
    mock_post(RESOURCES_URL, "resources.json")
    mock_post(CONTACTS_URL, "contacts.json")

    result = await get_ticket(CLIENT_ID, "T20260902.0011")

    assert result.found is True
    assert result.ticket is not None
    assert result.ticket.ticket_number == "T20260902.0011"
    assert result.ticket.status == "New"
    assert result.contact == "Priya Raman"
    assert result.note_count == 2
    # Newest note first, with the author resolved to a name.
    assert [note.note_id for note in result.notes] == [7002, 7001]
    assert result.notes[0].author == "Sam Ellis"
    assert result.notes[0].excerpt == "Profile rebuilt, monitoring for a day."


@respx.mock
async def test_get_ticket_queries_by_number_within_the_company(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    mock_fields()
    tickets = mock_post(TICKETS_URL, "ticket_single.json")
    mock_post(NOTES_URL, "ticket_notes_empty.json")
    mock_post(RESOURCES_URL, "resources.json")
    mock_post(CONTACTS_URL, "contacts.json")

    await get_ticket(CLIENT_ID, "T20260902.0011")

    applied = filters_of(tickets)
    assert {"op": "eq", "field": "companyID", "value": AUTOTASK_COMPANY_ID} in applied
    assert {"op": "eq", "field": "ticketNumber", "value": "T20260902.0011"} in applied


@respx.mock
async def test_get_ticket_not_found_says_so(autotask: Any, autotask_clock: dt.datetime) -> None:
    """Hard rule 7: never substitute a different ticket."""
    mock_post(TICKETS_URL, "tickets_empty.json")

    result = await get_ticket(CLIENT_ID, "T19990101.0001")

    assert result.found is False
    assert result.ticket is None
    assert result.notes == []
    assert result.message is not None
    assert "T19990101.0001" in result.message


@respx.mock
async def test_get_ticket_with_no_notes(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_fields()
    mock_post(TICKETS_URL, "ticket_single.json")
    mock_post(NOTES_URL, "ticket_notes_empty.json")
    mock_post(RESOURCES_URL, "resources.json")
    mock_post(CONTACTS_URL, "contacts.json")

    result = await get_ticket(CLIENT_ID, "T20260902.0011")

    assert result.found is True
    assert result.note_count == 0
    assert result.notes == []


@respx.mock
async def test_get_ticket_401(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_post(TICKETS_URL, "error_401.json", status=401)

    with pytest.raises(AuthError):
        await get_ticket(CLIENT_ID, "T20260902.0011")


@respx.mock
async def test_get_ticket_429(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_post(TICKETS_URL, "error_429.json", status=429)

    with pytest.raises(RateLimited):
        await get_ticket(CLIENT_ID, "T20260902.0011")


# --- list_tickets_for_device ----------------------------------------------


@respx.mock
async def test_list_tickets_for_device_happy_path(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    mock_fields()
    mock_post(CI_URL, "configuration_items.json")
    tickets = mock_post(TICKETS_URL, "tickets_for_device.json")
    mock_post(RESOURCES_URL, "resources.json")

    result = await list_tickets_for_device(CLIENT_ID, "WS14")

    assert result.matched_devices == ["TL-AUS-WS14 (acme-dental)"]
    assert result.ticket_count == 2
    # History, so closed tickets count too: newest first.
    assert [ticket.ticket_number for ticket in result.tickets] == [
        "T20260902.0011",
        "T20260801.0022",
    ]
    assert result.tickets[1].is_open is False
    assert {"op": "in", "field": "configurationItemID", "value": [4001]} in filters_of(tickets)


@respx.mock
async def test_list_tickets_for_device_with_no_match_says_so(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    """An unmatched device must not fall back to the client's whole ticket list."""
    mock_post(CI_URL, "configuration_items_empty.json")
    tickets = mock_post(TICKETS_URL, "tickets_open.json")

    result = await list_tickets_for_device(CLIENT_ID, "NO-SUCH-PC")

    assert result.matched_devices == []
    assert result.tickets == []
    assert result.message is not None
    assert "NO-SUCH-PC" in result.message
    assert tickets.call_count == 0


@respx.mock
async def test_list_tickets_for_device_401(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_post(CI_URL, "error_401.json", status=401)

    with pytest.raises(AuthError):
        await list_tickets_for_device(CLIENT_ID, "WS14")


@respx.mock
async def test_list_tickets_for_device_429(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_post(CI_URL, "error_429.json", status=429)

    with pytest.raises(RateLimited):
        await list_tickets_for_device(CLIENT_ID, "WS14")


# --- list_recent_time_entries ---------------------------------------------


@respx.mock
async def test_list_recent_time_entries_happy_path(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    mock_post(TICKETS_URL, "tickets_open.json")
    mock_post(TIME_URL, "time_entries.json")
    mock_post(RESOURCES_URL, "resources.json")

    result = await list_recent_time_entries(CLIENT_ID, days=7)

    assert result.window_days == 7
    assert result.since == autotask_clock - dt.timedelta(days=7)
    assert result.entry_count == 2
    assert result.total_hours == pytest.approx(1.75)
    # Newest first, with the ticket and the technician resolved.
    assert [entry.entry_id for entry in result.entries] == [8001, 8002]
    assert result.entries[0].ticket_number == "T20260902.0011"
    assert result.entries[0].worked_by == "Dana Okafor"
    assert result.entries[0].hours == pytest.approx(1.25)


@respx.mock
async def test_time_entries_are_scoped_through_this_clients_tickets(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    """TimeEntries carry no company id, so the ticket list is the only scope."""
    tickets = mock_post(TICKETS_URL, "tickets_open.json")
    entries = mock_post(TIME_URL, "time_entries.json")
    mock_post(RESOURCES_URL, "resources.json")

    await list_recent_time_entries(CLIENT_ID, days=7)

    assert {"op": "eq", "field": "companyID", "value": AUTOTASK_COMPANY_ID} in filters_of(tickets)
    ticket_filter = next(entry for entry in filters_of(entries) if entry.get("op") == "in")
    assert ticket_filter["field"] == "ticketID"
    assert sorted(ticket_filter["value"]) == [9001, 9002, 9003]


@respx.mock
async def test_list_recent_time_entries_empty(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_post(TICKETS_URL, "tickets_open.json")
    mock_post(TIME_URL, "time_entries_empty.json")

    result = await list_recent_time_entries(CLIENT_ID, days=7)

    assert result.entry_count == 0
    assert result.total_hours == 0
    assert result.entries == []


@respx.mock
async def test_no_tickets_means_no_time_entries_query(
    autotask: Any, autotask_clock: dt.datetime
) -> None:
    mock_post(TICKETS_URL, "tickets_empty.json")
    entries = mock_post(TIME_URL, "time_entries.json")

    result = await list_recent_time_entries(CLIENT_ID, days=7)

    assert result.entry_count == 0
    assert entries.call_count == 0


@respx.mock
async def test_list_recent_time_entries_401(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_post(TICKETS_URL, "error_401.json", status=401)

    with pytest.raises(AuthError):
        await list_recent_time_entries(CLIENT_ID)


@respx.mock
async def test_list_recent_time_entries_429(autotask: Any, autotask_clock: dt.datetime) -> None:
    mock_post(TICKETS_URL, "error_429.json", status=429)

    with pytest.raises(RateLimited):
        await list_recent_time_entries(CLIENT_ID)


async def test_a_nonsense_window_is_rejected(autotask: Any) -> None:
    with pytest.raises(ValueError, match="days must be at least 1"):
        await list_recent_time_entries(CLIENT_ID, days=0)


# --- registration ---------------------------------------------------------


def test_the_core_autotask_tools_are_registered() -> None:
    """The full set, report tools included, is asserted in test_autotask_reports.py."""
    names = {definition.name for definition in tools_for(ConnectorName.AUTOTASK)}
    assert {
        "list_open_tickets",
        "get_ticket",
        "list_tickets_for_device",
        "list_recent_time_entries",
    } <= names


@pytest.mark.parametrize(
    "name",
    ["list_open_tickets", "get_ticket", "list_tickets_for_device", "list_recent_time_entries"],
)
def test_every_tool_requires_a_client_id_it_never_shows_the_model(name: str) -> None:
    definition = get_tool(name)

    assert "client_id" in definition.input_schema["required"]
    # Hard rule 1: the model has no parameter through which to name a client.
    assert "client_id" not in definition.model_facing_schema()["properties"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "list_open_tickets",
            {"older_than_days", "priority", "status", "assigned_to", "queue", "site"},
        ),
        ("get_ticket", {"ticket_number", "site"}),
        ("list_tickets_for_device", {"device_name", "site"}),
        ("list_recent_time_entries", {"days", "site"}),
    ],
)
def test_each_tool_exposes_the_arguments_it_should(name: str, expected: set[str]) -> None:
    assert set(get_tool(name).model_facing_schema()["properties"]) == expected


@pytest.mark.parametrize(
    "name",
    ["list_open_tickets", "get_ticket", "list_tickets_for_device", "list_recent_time_entries"],
)
def test_every_tool_describes_itself_to_the_model(name: str) -> None:
    definition = get_tool(name)
    assert len(definition.description) > 120
    assert definition.to_anthropic_schema()["name"] == name


def test_required_arguments_stay_required_for_the_model() -> None:
    """`ticket_number` and `device_name` have no default: the model must supply them."""
    assert get_tool("get_ticket").model_facing_schema()["required"] == ["ticket_number"]
    assert get_tool("list_tickets_for_device").model_facing_schema()["required"] == ["device_name"]

"""search_tickets, list_service_calls, and queues on the ticket tools.

Tickets and TicketNotes fixtures carry the keys the pilot tenant populates
(2026-09-18). The pilot tenant has never created a service call, so those three
fixtures are Swagger-shaped and unverified live (api-notes).
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
    count_tickets,
    list_open_tickets,
    list_service_calls,
    search_tickets,
)
from app.connectors.base import ConnectorLimits
from app.connectors.errors import AuthError, NotFound, RateLimited
from app.models.enums import ConnectorName
from tests.connectors.conftest import (
    AUTOTASK_ZONE_URL,
    RecordingSleep,
    load_fixture,
    make_autotask_client,
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
    """2026-09-16 12:00 UTC."""


def filters_of(request: httpx.Request) -> list[dict[str, Any]]:
    return json.loads(request.content.decode())["filter"]


def mock_fields(entity: str = "Tickets", fixture: str = "fields_tickets.json") -> None:
    respx.get(fields_url(entity)).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", fixture))
    )


def mock_post(entity: str, fixture: str, status: int = 200) -> respx.Route:
    return respx.post(query_url(entity)).mock(
        return_value=httpx.Response(status, json=load_fixture("autotask", fixture))
    )


def mock_ticket_passes(search: str = "tickets_search.json") -> respx.Route:
    """Tickets/query serves both passes: the text search, and the parents by id."""

    def responder(request: httpx.Request) -> httpx.Response:
        by_id = any(f.get("field") == "id" and f.get("op") == "in" for f in filters_of(request))
        name = "tickets_by_ids.json" if by_id else search
        return httpx.Response(200, json=load_fixture("autotask", name))

    return respx.post(query_url("Tickets")).mock(side_effect=responder)


# --- search_tickets -----------------------------------------------------------


@respx.mock
async def test_search_finds_tickets_by_title_description_and_notes(autotask: Any) -> None:
    mock_fields()
    tickets = mock_ticket_passes()
    notes = mock_post("TicketNotes", "ticket_notes_search.json")

    result = await search_tickets(CLIENT_ID, "printer")

    assert result.searched == ["title", "description", "notes"]
    assert result.total == 4
    # Newest first by default.
    assert [t.ticket_number for t in result.tickets] == [
        "T20260914.0301",
        "T20260820.0302",
        "T20260709.0304",
        "T20260601.0303",
    ]
    by_number = {t.ticket_number: t for t in result.tickets}
    assert by_number["T20260914.0301"].matched_in == ["title", "notes"]
    assert by_number["T20260820.0302"].matched_in == ["description"]
    # Found only through its notes: pass 1 never saw it.
    notes_only = by_number["T20260709.0304"]
    assert notes_only.matched_in == ["notes"] and notes_only.note_matches == 2
    assert "Brother printer driver" in (notes_only.snippet or "")
    assert notes_only.status == "Complete"
    assert notes_only.completed_at == dt.datetime(2026, 7, 10, 16, 0, tzinfo=dt.UTC)
    assert by_number["T20260914.0301"].completed_at is None

    # Pass 1 is company-scoped; the note query cannot be, and is not.
    first_pass = filters_of(tickets.calls[0].request)
    assert any(f.get("field") == "companyID" for f in first_pass)
    text = next(f for f in first_pass if f.get("op") == "or")
    assert {"op": "contains", "field": "description", "value": "printer"} in text["items"]
    assert not any(f.get("field") == "companyID" for f in filters_of(notes.calls[0].request))
    # The parents are then asked for through the company filter.
    by_id = filters_of(tickets.calls[1].request)
    assert any(f.get("field") == "companyID" for f in by_id)
    assert {"op": "in", "field": "id", "value": [9304, 9999]} in by_id


@respx.mock
async def test_a_note_on_another_companys_ticket_is_dropped(autotask: Any) -> None:
    """Hard rule 1: ticket 9999 never came back through the company filter."""
    mock_fields()
    mock_ticket_passes()
    mock_post("TicketNotes", "ticket_notes_search.json")

    result = await search_tickets(CLIENT_ID, "printer")

    assert result.notes_outside_scope == 1
    assert "not this client's" not in json.dumps(result.model_dump(mode="json"))


@respx.mock
async def test_relevance_ranks_title_over_description_over_notes(autotask: Any) -> None:
    mock_fields()
    mock_ticket_passes()
    mock_post("TicketNotes", "ticket_notes_search.json")

    result = await search_tickets(CLIENT_ID, "printer", order_by="relevance")

    assert [(t.ticket_number, t.score) for t in result.tickets] == [
        ("T20260914.0301", 4),  # title 3 + one note
        ("T20260820.0302", 2),  # description; newer than the two below it
        ("T20260709.0304", 2),  # two notes
        ("T20260601.0303", 2),  # description, oldest
    ]
    assert result.order_by == "relevance"
    assert "no sort" in result.ranking_note


@respx.mock
async def test_notes_can_be_left_out_and_days_bounds_both_passes(autotask: Any) -> None:
    mock_fields()
    tickets = mock_ticket_passes()
    notes = mock_post("TicketNotes", "ticket_notes_search.json")

    quick = await search_tickets(CLIENT_ID, "printer", include_notes=False)
    assert notes.call_count == 0
    assert quick.searched == ["title", "description"] and quick.total == 3

    await search_tickets(CLIENT_ID, "  printer  ", days=30)
    created = next(
        f for f in filters_of(tickets.calls[-2].request) if f.get("field") == "createDate"
    )
    assert created == {"op": "gte", "field": "createDate", "value": "2026-08-17T12:00:00Z"}
    note_window = next(
        f for f in filters_of(notes.calls[0].request) if f.get("field") == "createDateTime"
    )
    assert note_window["value"] == "2026-08-17T12:00:00Z"


@respx.mock
async def test_a_search_with_no_hits_is_an_answer(autotask: Any) -> None:
    mock_fields()
    mock_post("Tickets", "tickets_empty.json")
    mock_post("TicketNotes", "ticket_notes_empty.json")
    result = await search_tickets(CLIENT_ID, "quantum flux")
    assert result.total == 0 and result.tickets == []


async def test_a_one_character_query_is_refused(autotask: Any) -> None:
    with pytest.raises(ValueError, match="at least 2 characters"):
        await search_tickets(CLIENT_ID, " a ")


@respx.mock
async def test_search_401(autotask: Any) -> None:
    mock_fields()
    mock_post("Tickets", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await search_tickets(CLIENT_ID, "printer")


@respx.mock
async def test_search_429(autotask: Any) -> None:
    mock_fields()
    mock_post("Tickets", "error_429.json", status=429)
    with pytest.raises(RateLimited):
        await search_tickets(CLIENT_ID, "printer")


# --- queues -------------------------------------------------------------------


@respx.mock
async def test_tickets_are_counted_by_queue_label(autotask: Any) -> None:
    mock_fields()
    mock_post("Tickets", "tickets_open.json")

    result = await count_tickets(CLIENT_ID, group_by="queue")

    assert result.counts == {"Help Desk": 2, "Escalation": 1}
    assert result.total == 3


@respx.mock
async def test_a_queue_is_named_by_label_and_resolved_from_the_picklist(autotask: Any) -> None:
    mock_fields()
    tickets = mock_post("Tickets", "tickets_open.json")
    mock_post("Resources", "resources.json")

    result = await list_open_tickets(CLIENT_ID, queue="help desk")

    assert {"op": "eq", "field": "queueID", "value": 14046773} in filters_of(
        tickets.calls[0].request
    )
    assert result.filtered_by["queue"] == "help desk"
    assert {t.queue for t in result.tickets} == {"Help Desk", "Escalation"}  # labels, not ids


@respx.mock
async def test_an_unknown_queue_lists_the_real_ones(autotask: Any) -> None:
    mock_fields()
    mock_post("Tickets", "tickets_open.json")
    with pytest.raises(NotFound, match="Help Desk"):
        await list_open_tickets(CLIENT_ID, queue="Level 9")


# --- list_service_calls -------------------------------------------------------


def mock_service_call_world(calls: str = "service_calls.json") -> respx.Route:
    mock_fields("ServiceCalls", "fields_service_calls.json")
    mock_post("ServiceCallTickets", "service_call_tickets.json")
    mock_post("ServiceCallTicketResources", "service_call_ticket_resources.json")
    mock_post("Tickets", "tickets_open.json")
    mock_post("Resources", "resources.json")
    return mock_post("ServiceCalls", calls)


@respx.mock
async def test_service_calls_carry_who_and_which_tickets(autotask: Any) -> None:
    calls = mock_service_call_world()

    result = await list_service_calls(CLIENT_ID, days=2)

    window = filters_of(calls.calls[0].request)
    assert {"op": "gte", "field": "startDateTime", "value": "2026-09-14T12:00:00Z"} in window
    assert any(f.get("field") == "companyID" for f in window)
    assert [(row.site_slug, row.status, row.complete) for row in result.rows] == [
        ("acme-dental", "New", False),
        ("acme-dental", "Complete", True),
    ]
    first, second = result.rows
    assert first.resources == ["Dana Okafor"]
    assert first.tickets == ["T20260902.0011 — " + (first.tickets[0].split(" — ", 1)[1])]
    assert second.resources == ["Sam Ellis"]
    assert [t.split(" — ")[0] for t in second.tickets] == ["T20260910.0007", "T20260914.0003"]
    assert first.duration_hours == 2.0


@respx.mock
async def test_no_service_calls_is_an_answer_and_asks_for_nothing_else(autotask: Any) -> None:
    """What the pilot tenant returns: it has never scheduled one."""
    mock_fields("ServiceCalls", "fields_service_calls.json")
    links = mock_post("ServiceCallTickets", "service_call_tickets.json")
    mock_post("ServiceCalls", "service_calls_empty.json")

    result = await list_service_calls(CLIENT_ID)

    assert result.total == 0 and result.rows == []
    assert links.call_count == 0


@respx.mock
async def test_service_calls_401(autotask: Any) -> None:
    mock_post("ServiceCalls", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await list_service_calls(CLIENT_ID)


# --- registration -------------------------------------------------------------


def test_the_fourteen_autotask_tools_are_registered() -> None:
    names = {definition.name for definition in tools_for(ConnectorName.AUTOTASK)}
    assert names >= {"search_tickets", "list_service_calls", "list_projects"}
    assert "list_tickets_by_queue" not in names  # it is a grouping and a filter, not a tool
    assert len(names) == 14


def test_search_offers_both_orderings_and_the_queue_grouping_exists() -> None:
    search = get_tool("search_tickets").model_facing_schema()
    assert search["properties"]["order_by"]["enum"] == ["recency", "relevance"]
    assert search["required"] == ["query"]
    assert set(search["properties"]) == {"query", "days", "include_notes", "order_by", "site"}
    counts = get_tool("count_tickets").model_facing_schema()
    assert "queue" in counts["properties"]["group_by"]["enum"]
    assert "queue" in get_tool("list_open_tickets").model_facing_schema()["properties"]

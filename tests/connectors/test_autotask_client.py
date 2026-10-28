"""Autotask client: zone resolution, header auth, paging, scoping, picklists."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from app.connectors.autotask.client import MAX_IN_CLAUSE_VALUES, AutotaskClient
from app.connectors.base import ConnectorLimits
from app.connectors.errors import (
    AuthError,
    ConnectorNotConfigured,
    NotFound,
    RateLimited,
    UpstreamError,
)
from tests.connectors.conftest import (
    AUTOTASK_COMPANY_ID,
    AUTOTASK_ZONE_LOOKUP_URL,
    AUTOTASK_ZONE_URL,
    RecordingSleep,
    load_fixture,
    make_autotask_client,
    site_ref,
)

TICKETS_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Tickets/query"
NOTES_URL = f"{AUTOTASK_ZONE_URL}/V1.0/TicketNotes/query"
TIME_URL = f"{AUTOTASK_ZONE_URL}/V1.0/TimeEntries/query"
CI_URL = f"{AUTOTASK_ZONE_URL}/V1.0/ConfigurationItems/query"
RESOURCES_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Resources/query"
CONTACTS_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Contacts/query"
FIELDS_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Tickets/entityInformation/fields"
COMPANIES_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Companies/query"
COMPANY_FIELDS_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Companies/entityInformation/fields"


def mock_zone() -> respx.Route:
    return respx.get(AUTOTASK_ZONE_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "zone.json"))
    )


def mock_query(url: str, fixture: str, status: int = 200) -> respx.Route:
    return respx.post(url).mock(
        return_value=httpx.Response(status, json=load_fixture("autotask", fixture))
    )


def sent_body(route: respx.Route, index: int = 0) -> dict[str, Any]:
    return json.loads(route.calls[index].request.content.decode())


def filters_of(route: respx.Route, index: int = 0) -> list[dict[str, Any]]:
    return sent_body(route, index)["filter"]


# --- zone resolution ------------------------------------------------------


@respx.mock
async def test_the_zone_is_resolved_once_and_reused(fast_limits: ConnectorLimits) -> None:
    zone = mock_zone()
    tickets = mock_query(TICKETS_URL, "tickets_open.json")

    async with make_autotask_client(limits=fast_limits) as client:
        await client.list_tickets()
        await client.list_tickets()

        assert client.base_url == AUTOTASK_ZONE_URL

    assert zone.call_count == 1
    assert tickets.call_count == 2


@respx.mock
async def test_the_zone_lookup_carries_the_auth_headers(fast_limits: ConnectorLimits) -> None:
    zone = mock_zone()
    mock_query(TICKETS_URL, "tickets_empty.json")

    async with make_autotask_client(limits=fast_limits) as client:
        await client.list_tickets()

    request = zone.calls[0].request
    assert request.headers["ApiIntegrationCode"] == "test-integration-code"
    assert request.headers["UserName"] == "api-user@example.invalid"
    assert request.headers["Secret"] == "test-secret"
    assert request.url.params["user"] == "api-user@example.invalid"


@respx.mock
async def test_a_configured_base_url_skips_the_zone_lookup(
    fast_limits: ConnectorLimits,
) -> None:
    zone = mock_zone()
    mock_query(TICKETS_URL, "tickets_empty.json")

    async with make_autotask_client(
        limits=fast_limits, config={"base_url": AUTOTASK_ZONE_URL}
    ) as client:
        await client.list_tickets()

    assert zone.call_count == 0


@respx.mock
async def test_an_unresolvable_zone_is_an_auth_error_not_a_server_error(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    """Autotask answers an unknown API user with 500, which must not look retryable."""
    respx.get(AUTOTASK_ZONE_LOOKUP_URL).mock(
        return_value=httpx.Response(500, json=load_fixture("autotask", "zone_error.json"))
    )

    async with make_autotask_client(limits=fast_limits, sleep=sleeper) as client:
        with pytest.raises(AuthError, match="could not determine the zone"):
            await client.list_tickets()


@respx.mock
async def test_a_zone_response_we_cannot_read_is_an_auth_error(
    fast_limits: ConnectorLimits,
) -> None:
    respx.get(AUTOTASK_ZONE_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )

    async with make_autotask_client(limits=fast_limits) as client:
        with pytest.raises(AuthError, match="could not read"):
            await client.list_tickets()


async def test_missing_credentials_fail_before_any_request(
    fast_limits: ConnectorLimits,
) -> None:
    async with make_autotask_client(
        limits=fast_limits, secrets={"username": "u", "secret": "s"}
    ) as client:
        with pytest.raises(AuthError, match="api_integration_code"):
            await client.list_tickets()


def test_a_non_numeric_company_mapping_is_rejected(fast_limits: ConnectorLimits) -> None:
    client = make_autotask_client(
        limits=fast_limits, sites=[site_ref("acme-corp", slug="badly-mapped")]
    )
    with pytest.raises(UpstreamError, match="not a number"):
        _ = client.target_company_ids


def test_base_url_before_resolution_is_a_clear_error(fast_limits: ConnectorLimits) -> None:
    client = make_autotask_client(limits=fast_limits)
    with pytest.raises(UpstreamError, match="zone has not been resolved"):
        _ = client.base_url


# --- auth headers on every call -------------------------------------------


@respx.mock
async def test_every_query_carries_the_three_headers(fast_limits: ConnectorLimits) -> None:
    mock_zone()
    tickets = mock_query(TICKETS_URL, "tickets_open.json")

    async with make_autotask_client(limits=fast_limits) as client:
        await client.list_tickets()

    headers = tickets.calls[0].request.headers
    assert headers["ApiIntegrationCode"] == "test-integration-code"
    assert headers["UserName"] == "api-user@example.invalid"
    assert headers["Secret"] == "test-secret"
    assert headers["Content-Type"] == "application/json"


# --- the query we send ----------------------------------------------------


@respx.mock
async def test_open_tickets_are_filtered_by_company_and_completion(
    fast_limits: ConnectorLimits,
) -> None:
    """Open is `completedDate notExist`, never a status number (api-notes)."""
    mock_zone()
    tickets = mock_query(TICKETS_URL, "tickets_open.json")

    async with make_autotask_client(limits=fast_limits) as client:
        await client.list_tickets(open_only=True)

    body = sent_body(tickets)
    assert body["maxRecords"] == 500
    applied = filters_of(tickets)
    assert {"op": "eq", "field": "companyID", "value": AUTOTASK_COMPANY_ID} in applied
    assert {"op": "notExist", "field": "completedDate"} in applied
    assert not any(entry.get("field") == "status" for entry in applied)


@respx.mock
async def test_a_query_is_sent_as_a_post(fast_limits: ConnectorLimits) -> None:
    """POST avoids the 2048-character limit on GET query strings."""
    mock_zone()
    tickets = mock_query(TICKETS_URL, "tickets_empty.json")

    async with make_autotask_client(limits=fast_limits) as client:
        await client.list_tickets()

    assert tickets.calls[0].request.method == "POST"


@respx.mock
async def test_an_in_filter_is_capped_below_the_or_condition_limit(
    fast_limits: ConnectorLimits,
) -> None:
    """Autotask rejects a query with more than 500 OR conditions."""
    mock_zone()
    entries = mock_query(TIME_URL, "time_entries_empty.json")

    async with make_autotask_client(limits=fast_limits) as client:
        ticket_ids = list(range(1, MAX_IN_CLAUSE_VALUES + 50))
        _, truncated = await client.list_time_entries(
            ticket_ids, worked_since=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
        )

    values = next(f for f in filters_of(entries) if f.get("op") == "in")["value"]
    assert len(values) == MAX_IN_CLAUSE_VALUES
    # Dropping ids means the answer is partial, and must say so.
    assert truncated is True


# --- the condition budget -------------------------------------------------


def test_conditions_are_counted_across_every_clause() -> None:
    """A scalar is one, an `in` is one per value, a group is its items."""
    count = AutotaskClient._conditions(
        [
            {"op": "in", "field": "companyID", "value": [1, 2, 3]},
            {"op": "gte", "field": "completedDate", "value": "2026-07-01T00:00:00Z"},
            {
                "op": "or",
                "items": [
                    {"op": "eq", "field": "title", "value": "x"},
                    {"op": "in", "field": "status", "value": [5, 8]},
                ],
            },
        ]
    )
    assert count == 3 + 1 + (1 + 2)


def test_repeated_values_are_counted_once_because_they_are_sent_once() -> None:
    assert AutotaskClient._conditions([{"op": "in", "field": "id", "value": [7, 7, 7, 8]}]) == 2


def test_a_filter_inside_the_budget_is_still_one_query() -> None:
    filters = [
        {"op": "in", "field": "companyID", "value": list(range(144))},
        {"op": "gte", "field": "completedDate", "value": "2026-07-01T00:00:00Z"},
    ]
    assert AutotaskClient._split_to_budget(filters, 450) == [filters]


def test_the_filter_that_was_refused_live_is_split_and_nothing_is_lost() -> None:
    """`in companyID[144] and in id[400]` — 544 conditions, a 500 from the
    vendor on 2026-10-01. The widest `in` is the one that splits; the company
    scope rides along whole on each part, or a part would be asked of fewer
    customers than the caller meant."""
    companies = list(range(1000, 1144))
    ids = list(range(500, 900))
    parts = AutotaskClient._split_to_budget(
        [
            {"op": "in", "field": "companyID", "value": companies},
            {"op": "in", "field": "id", "value": ids},
        ],
        450,
    )

    assert len(parts) == 2
    assert all(AutotaskClient._conditions(part) <= 450 for part in parts)
    split: list[int] = []
    for part in parts:
        by_field = {clause["field"]: clause["value"] for clause in part}
        assert by_field["companyID"] == companies  # every part, every customer
        split.extend(by_field["id"])
    assert split == ids  # every id asked for exactly once, in order


def test_the_widest_in_is_the_one_that_splits_whichever_clause_it_is() -> None:
    """A franchisee with more customers than the budget is still answerable.

    The company scope is the wide clause here, so it is what splits: each part
    asks about some of the customers and all of the ids, and the union is the
    question the caller asked. Every row is checked against the whole allowlist
    on the way back regardless (`_scoped`), so a part is narrower than the
    client, never wider.
    """
    parts = AutotaskClient._split_to_budget(
        [
            {"op": "in", "field": "companyID", "value": list(range(600))},
            {"op": "in", "field": "id", "value": [1, 2, 3]},
        ],
        450,
    )
    assert len(parts) == 2
    assert all(AutotaskClient._conditions(part) <= 450 for part in parts)
    companies: list[int] = []
    for part in parts:
        by_field = {clause["field"]: clause["value"] for clause in part}
        assert by_field["id"] == [1, 2, 3]
        companies.extend(by_field["companyID"])
    assert companies == list(range(600))


def test_a_budget_the_unsplittable_clauses_already_fill_is_refused_before_sending() -> None:
    """Better our error naming the problem than the vendor's 500 naming nothing.

    Nothing we send looks like this today; the guard is here so that a filter
    built from a list somebody grows later cannot reach the vendor over the
    limit. The loop hands a `ValueError` back to the model as correctable.
    """
    with pytest.raises(ValueError, match="cannot be brought under"):
        AutotaskClient._split_to_budget(
            [
                {
                    "op": "or",
                    "items": [{"op": "eq", "field": "id", "value": n} for n in range(450)],
                },
                {"op": "in", "field": "companyID", "value": [1, 2, 3]},
            ],
            450,
        )


def test_a_single_clause_over_the_budget_with_nothing_to_split_is_refused() -> None:
    with pytest.raises(ValueError, match="no `in` filter to split"):
        AutotaskClient._split_to_budget(
            [{"op": "or", "items": [{"op": "eq", "field": "id", "value": n} for n in range(20)]}],
            5,
        )


@respx.mock
async def test_an_over_budget_query_is_sent_as_several_and_merged(
    fast_limits: ConnectorLimits,
) -> None:
    """End to end through the real query path: two POSTs, one answer.

    The caller asks one question about more tickets than one query may carry,
    and gets the rows from both parts with the truncation flag of neither.
    """
    mock_zone()
    page = load_fixture("autotask", "tickets_open.json")
    queries = respx.post(TICKETS_URL).mock(return_value=httpx.Response(200, json=page))
    budget = replace(fast_limits, max_filter_conditions=250)

    async with make_autotask_client(limits=budget) as client:
        found = await client.get_tickets_by_ids(list(range(9000, 9400)))

    assert queries.call_count == 2
    for index in range(2):
        sent = filters_of(queries, index)
        assert AutotaskClient._conditions(sent) <= 250
    # Both parts' rows come back, and the fixture's rows belong to the scope.
    assert len(found) == 2 * len(page["items"])


# --- paging ---------------------------------------------------------------


@respx.mock
async def test_paging_follows_next_page_url(fast_limits: ConnectorLimits) -> None:
    mock_zone()
    page_one = load_fixture("autotask", "tickets_page_1.json")
    next_url = page_one["pageDetails"]["nextPageUrl"]
    first = respx.post(TICKETS_URL).mock(return_value=httpx.Response(200, json=page_one))
    second = respx.get(next_url).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "tickets_page_2.json"))
    )

    async with make_autotask_client(limits=fast_limits) as client:
        tickets, truncated = await client.list_tickets()

    assert first.call_count == 1
    assert second.call_count == 1
    assert [ticket.id for _, ticket in tickets] == [9001, 9002, 9003]
    assert truncated is False


@respx.mock
async def test_a_post_querys_own_next_page_is_posted_with_the_filter_again(
    fast_limits: ConnectorLimits,
) -> None:
    """The page shape the pilot tenant actually returns.

    A POST query's `nextPageUrl` is `/query/next?paging=…`, which carries the
    paging token in the URL and takes the filter in the body. Live,
    2026-09-30, following it with a GET — correct for the `?search=` URL a GET
    query returns — was answered `405 Method Not Allowed` with `Allow: POST`,
    and the outcome ledger's first live run failed on it. The fixture's own
    next page was a hand-made `?search=` URL, which is why no test caught it.
    """
    mock_zone()
    page_one = load_fixture("autotask", "tickets_page_1_post.json")
    next_url = page_one["pageDetails"]["nextPageUrl"]
    first = respx.post(TICKETS_URL).mock(return_value=httpx.Response(200, json=page_one))
    second = respx.post(next_url).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "tickets_page_2.json"))
    )
    refused = respx.get(next_url).mock(
        return_value=httpx.Response(405, json={}, headers={"Allow": "POST"})
    )

    async with make_autotask_client(limits=fast_limits) as client:
        tickets, truncated = await client.list_tickets()

    assert refused.call_count == 0
    assert second.call_count == 1
    assert first.call_count == 1
    assert [ticket.id for _, ticket in tickets] == [9001, 9002, 9003]
    assert truncated is False
    # The filter is sent again: the paging token alone does not carry it.
    assert json.loads(second.calls[0].request.content)["filter"]


@respx.mock
async def test_paging_reports_truncation_at_the_page_cap(
    fast_limits: ConnectorLimits,
) -> None:
    mock_zone()
    counter = {"n": 0}
    page = load_fixture("autotask", "tickets_page_1.json")

    def responder(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        body = json.loads(json.dumps(page))
        body["pageDetails"]["nextPageUrl"] = f"{AUTOTASK_ZONE_URL}/next/{counter['n']}"
        return httpx.Response(200, json=body)

    respx.post(TICKETS_URL).mock(side_effect=responder)
    respx.get(url__startswith=f"{AUTOTASK_ZONE_URL}/next/").mock(side_effect=responder)

    async with make_autotask_client(limits=fast_limits) as client:
        _, truncated = await client.list_tickets()

    assert truncated is True
    assert counter["n"] == fast_limits.max_pages


@respx.mock
async def test_paging_stops_if_the_vendor_repeats_a_page(
    fast_limits: ConnectorLimits,
) -> None:
    mock_zone()
    page = load_fixture("autotask", "tickets_page_1.json")
    repeated = page["pageDetails"]["nextPageUrl"]
    respx.post(TICKETS_URL).mock(return_value=httpx.Response(200, json=page))
    second = respx.get(repeated).mock(return_value=httpx.Response(200, json=page))

    async with make_autotask_client(limits=fast_limits) as client:
        _, truncated = await client.list_tickets()

    assert second.call_count == 1
    assert truncated is False


@respx.mock
async def test_a_response_without_items_is_an_upstream_error(
    fast_limits: ConnectorLimits,
) -> None:
    mock_zone()
    respx.post(TICKETS_URL).mock(return_value=httpx.Response(200, json={"pageDetails": {}}))

    async with make_autotask_client(limits=fast_limits) as client:
        with pytest.raises(UpstreamError, match="without an items array"):
            await client.list_tickets()


# --- scoping --------------------------------------------------------------


@respx.mock
async def test_tickets_from_another_company_are_dropped(
    fast_limits: ConnectorLimits,
) -> None:
    """Defence in depth for hard rule 1."""
    mock_zone()
    mock_query(TICKETS_URL, "tickets_foreign_company.json")

    async with make_autotask_client(limits=fast_limits) as client:
        tickets, _ = await client.list_tickets()

    assert [ticket.id for _, ticket in tickets] == [9001]
    assert all(site.external_id == str(AUTOTASK_COMPANY_ID) for site, _ in tickets)


@respx.mock
async def test_notes_for_another_companys_ticket_are_dropped(
    fast_limits: ConnectorLimits,
) -> None:
    """TicketNotes carry no company id, so the ticket is the only scope."""
    mock_zone()
    mock_query(TICKETS_URL, "tickets_open.json")
    mock_query(NOTES_URL, "ticket_notes_foreign.json")

    async with make_autotask_client(limits=fast_limits) as client:
        await client.list_tickets()  # establishes which ticket ids are ours
        notes = await client.list_ticket_notes(9001)

    assert notes == []


@respx.mock
async def test_time_entries_for_another_companys_ticket_are_dropped(
    fast_limits: ConnectorLimits,
) -> None:
    mock_zone()
    mock_query(TICKETS_URL, "tickets_open.json")
    mock_query(TIME_URL, "time_entries_foreign.json")

    async with make_autotask_client(limits=fast_limits) as client:
        tickets, _ = await client.list_tickets()
        entries, _ = await client.list_time_entries(
            [ticket.id for _, ticket in tickets],
            worked_since=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        )

    assert entries == []


@respx.mock
async def test_notes_come_back_newest_first(fast_limits: ConnectorLimits) -> None:
    mock_zone()
    mock_query(TICKETS_URL, "tickets_open.json")
    mock_query(NOTES_URL, "ticket_notes.json")

    async with make_autotask_client(limits=fast_limits) as client:
        await client.list_tickets()
        notes = await client.list_ticket_notes(9001)

    assert [note.id for note in notes] == [7002, 7001]


# --- organisations, for sync_sites ---------------------------------------


@respx.mock
async def test_only_active_customer_companies_are_organisations(
    fast_limits: ConnectorLimits,
) -> None:
    """Prospects, vendors, inactive companies and company 0 are not sites."""
    mock_zone()
    respx.get(COMPANY_FIELDS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_companies.json"))
    )
    query = mock_query(COMPANIES_URL, "companies.json")

    async with make_autotask_client(limits=fast_limits) as client:
        organisations = await client.list_organisations()

    # The vendor is asked for exactly that, with "Customer" resolved through
    # this tenant's own picklist rather than assumed to be 1...
    assert filters_of(query) == [
        {"op": "gt", "field": "id", "value": 0},
        {"op": "eq", "field": "isActive", "value": True},
        {"op": "eq", "field": "companyType", "value": 1},
    ]
    # ...and whatever comes back is checked again: the fixture includes
    # company 0, an inactive customer and a prospect, none of which survive.
    assert [(o.external_id, o.name) for o in organisations] == [
        ("42", "Acme Dental"),
        ("43", "TeamLogic IT of Dallas"),
    ]


@respx.mock
async def test_a_tenant_without_a_customer_company_type_cannot_sync(
    fast_limits: ConnectorLimits,
) -> None:
    """Hard rule 7 applied to configuration: no guessing which type means customer."""
    mock_zone()
    respx.get(COMPANY_FIELDS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "fields": [
                    {
                        "name": "companyType",
                        "dataType": "integer",
                        "isPickList": True,
                        "picklistValues": [
                            {"value": "1", "label": "Client", "isActive": True},
                            {"value": "3", "label": "Prospect", "isActive": True},
                        ],
                    }
                ]
            },
        )
    )
    query = mock_query(COMPANIES_URL, "companies.json")

    async with make_autotask_client(limits=fast_limits) as client:
        with pytest.raises(ConnectorNotConfigured, match="Client, Prospect"):
            await client.list_organisations()

    assert query.call_count == 0


# --- picklists ------------------------------------------------------------


@respx.mock
async def test_picklist_labels_come_from_the_tenant(fast_limits: ConnectorLimits) -> None:
    mock_zone()
    fields = respx.get(FIELDS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_tickets.json"))
    )

    async with make_autotask_client(limits=fast_limits) as client:
        assert await client.label_for("Tickets", "status", 8) == "In Progress"
        assert await client.label_for("Tickets", "priority", 2) == "High"
        # Cached per (entity, field): status and priority are one fetch each.
        assert await client.label_for("Tickets", "status", 1) == "New"

    assert fields.call_count == 2


@respx.mock
async def test_null_picklist_values_on_other_fields_do_not_break_a_lookup(
    fast_limits: ConnectorLimits,
) -> None:
    """Autotask sends `picklistValues: null`, not `[]`, on non-picklist fields.

    The companies fixture has that on `isActive`, and a picklist field with a
    null list on `classification`. Neither may stop `companyType` resolving.
    """
    mock_zone()
    respx.get(COMPANY_FIELDS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_companies.json"))
    )

    async with make_autotask_client(limits=fast_limits) as client:
        assert await client.picklist("Companies", "companyType") == {
            1: "Customer",
            2: "Lead",
            3: "Prospect",
            4: "Dead",
            6: "Vendor",
            7: "Partner",
        }
        # The fields carrying null are simply empty, not errors.
        assert await client.picklist("Companies", "isActive") == {}
        assert await client.picklist("Companies", "classification") == {}


@respx.mock
async def test_a_picklist_option_missing_its_value_or_label_is_skipped(
    fast_limits: ConnectorLimits,
) -> None:
    """Hard rule 7: an option we cannot read is left out, never filled in."""
    mock_zone()
    respx.get(FIELDS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "fields": [
                    {
                        "name": "status",
                        "isPickList": True,
                        "picklistValues": [
                            {"value": "1", "label": "New", "isActive": None},
                            {"value": None, "label": "Orphan"},
                            {"value": "5", "label": None},
                            {"value": "x", "label": "Not a number"},
                        ],
                    },
                    {"name": None, "isPickList": None, "picklistValues": None},
                ]
            },
        )
    )

    async with make_autotask_client(limits=fast_limits) as client:
        assert await client.picklist("Tickets", "status") == {1: "New"}


@respx.mock
async def test_an_unmapped_picklist_number_is_reported_as_unknown(
    fast_limits: ConnectorLimits,
) -> None:
    """Hard rule 7: never invent a label for a status we do not recognise."""
    mock_zone()
    respx.get(FIELDS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_tickets.json"))
    )

    async with make_autotask_client(limits=fast_limits) as client:
        assert await client.label_for("Tickets", "status", 99) == "99 (unknown)"
        assert await client.label_for("Tickets", "status", None) is None


@respx.mock
async def test_a_priority_label_resolves_to_this_tenants_number(
    fast_limits: ConnectorLimits,
) -> None:
    mock_zone()
    respx.get(FIELDS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_tickets.json"))
    )

    async with make_autotask_client(limits=fast_limits) as client:
        assert await client.resolve_picklist_value("Tickets", "priority", "High") == 2
        assert await client.resolve_picklist_value("Tickets", "priority", "critical") == 1
        assert await client.resolve_picklist_value("Tickets", "priority", "3") == 3


@respx.mock
async def test_an_unknown_priority_label_lists_the_real_ones(
    fast_limits: ConnectorLimits,
) -> None:
    mock_zone()
    respx.get(FIELDS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_tickets.json"))
    )

    async with make_autotask_client(limits=fast_limits) as client:
        with pytest.raises(NotFound, match="Critical"):
            await client.resolve_picklist_value("Tickets", "priority", "Urgent")


# --- errors ---------------------------------------------------------------


@respx.mock
async def test_a_401_on_a_query_becomes_an_auth_error(fast_limits: ConnectorLimits) -> None:
    mock_zone()
    mock_query(TICKETS_URL, "error_401.json", status=401)

    async with make_autotask_client(limits=fast_limits) as client:
        with pytest.raises(AuthError):
            await client.list_tickets()


@respx.mock
async def test_a_429_is_retried_then_raises_rate_limited(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    mock_zone()
    route = mock_query(TICKETS_URL, "error_429.json", status=429)

    async with make_autotask_client(limits=fast_limits, sleep=sleeper) as client:
        with pytest.raises(RateLimited):
            await client.list_tickets()

    assert route.call_count == fast_limits.max_retries + 1
    assert len(sleeper.delays) == fast_limits.max_retries


@respx.mock
async def test_a_500_is_an_upstream_error(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    mock_zone()
    mock_query(TICKETS_URL, "error_500.json", status=500)

    async with make_autotask_client(limits=fast_limits, sleep=sleeper) as client:
        with pytest.raises(UpstreamError):
            await client.list_tickets()


@respx.mock
async def test_no_tickets_is_a_valid_answer(fast_limits: ConnectorLimits) -> None:
    mock_zone()
    mock_query(TICKETS_URL, "tickets_empty.json")

    async with make_autotask_client(limits=fast_limits) as client:
        tickets, truncated = await client.list_tickets()

    assert tickets == []
    assert truncated is False


# --- lookups --------------------------------------------------------------


@respx.mock
async def test_a_device_search_matches_title_hostname_or_reference(
    fast_limits: ConnectorLimits,
) -> None:
    mock_zone()
    route = mock_query(CI_URL, "configuration_items.json")

    async with make_autotask_client(limits=fast_limits) as client:
        devices = await client.find_configuration_items("WS14")

    assert [device.name for _, device in devices] == ["TL-AUS-WS14"]
    group = next(f for f in filters_of(route) if f.get("op") == "or")
    assert {entry["field"] for entry in group["items"]} == {
        "referenceTitle",
        "rmmDeviceAuditHostname",
        "referenceNumber",
    }


@respx.mock
async def test_resources_are_fetched_by_id_not_listed(fast_limits: ConnectorLimits) -> None:
    """Resources are MSP staff, shared across companies: never company-scoped."""
    mock_zone()
    route = mock_query(RESOURCES_URL, "resources.json")

    async with make_autotask_client(limits=fast_limits) as client:
        resources = await client.get_resources([501, 502, 501])

    assert resources[501].name == "Dana Okafor"
    applied = filters_of(route)
    assert applied == [{"op": "in", "field": "id", "value": [501, 502]}]
    assert not any(entry.get("field") == "companyID" for entry in applied)


@respx.mock
async def test_contacts_are_company_scoped(fast_limits: ConnectorLimits) -> None:
    mock_zone()
    route = mock_query(CONTACTS_URL, "contacts.json")

    async with make_autotask_client(limits=fast_limits) as client:
        contacts = await client.get_contacts([6001])

    assert contacts[6001].name == "Priya Raman"
    assert {"op": "eq", "field": "companyID", "value": AUTOTASK_COMPANY_ID} in filters_of(route)


async def test_empty_id_lists_make_no_request(fast_limits: ConnectorLimits) -> None:
    async with make_autotask_client(
        limits=fast_limits, config={"base_url": AUTOTASK_ZONE_URL}
    ) as client:
        assert await client.get_resources([]) == {}
        assert await client.get_contacts([]) == {}


def test_limits_can_be_tightened_for_tests(fast_limits: ConnectorLimits) -> None:
    tightened = dataclasses.replace(fast_limits, max_pages=2)
    assert tightened.max_pages == 2

"""The numbers above the ticket queue.

Two halves, deliberately apart. `build` is arithmetic over rows and is tested
as arithmetic: hand it two listings and assert every figure, so a counting
mistake is a failure here rather than something somebody notices on a wall.
The endpoint around it is tested for the things endpoints get wrong — who may
call it, whose data comes back, and whether the cache does what it says.

No vendor is called: Autotask is replaced at the connector boundary, the same
way `tests/api/test_tickets.py` replaces it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import app.tickets.statistics as statistics_module
from app.connectors.scope import SiteRef
from app.models import OrganisationKind, Role
from app.reports.tbr.config import BusinessHours
from app.resolved.config import ResolvedConfig, SlaConfig, SlaTarget
from app.resolved.sla import SlaCalculator
from app.tickets.statistics import CARD_FILTERS, build
from tests.api.conftest import Make, sign_in

pytestmark = pytest.mark.usefixtures("clean")

#: A Wednesday, so "this week" has a Monday behind it and the fortnight window
#: crosses a month boundary — both of which have caught off-by-one before.
NOW = dt.datetime(2026, 9, 30, 15, 0, tzinfo=dt.UTC)
ZONE = dt.UTC


def _site(name: str = "Acme Dental", slug: str = "acme-dental") -> SiteRef:
    return SiteRef(site_id=uuid.uuid4(), slug=slug, name=name, external_id="1")


def _ticket(
    ticket_id: int,
    *,
    created: dt.datetime,
    completed: dt.datetime | None = None,
    assignee: int | None = 7,
    priority: int = 1,
    due: dt.datetime | None = None,
    responded: dt.datetime | None = None,
    number: str | None = None,
    title: str = "A ticket",
) -> Any:
    return SimpleNamespace(
        id=ticket_id,
        ticket_number=number or f"T{ticket_id}",
        title=title,
        priority=priority,
        status=1,
        assigned_resource_id=assignee,
        create_date=created,
        completed_date=completed,
        due_date_time=due,
        first_response_due=None,
        first_response_at=responded,
        resolved_due=None,
        resolved_at=None,
    )


def _data(open_rows: list[Any], done_rows: list[Any], **over: Any) -> dict[str, Any]:
    return {
        "open": open_rows,
        "done": done_rows,
        "truncated": False,
        "resources": {7: "Dan", 8: "Priya"},
        "priorities": {1: "High", 2: "Low"},
        **over,
    }


#: Targets for the arithmetic tests: one minute, so a ticket raised before now
#: is breached and one raised at now is not. What is being asserted here is the
#: counting, not the clock — `tests/resolved/` asserts the clock.
def _calculator(**over: Any) -> SlaCalculator:
    config = ResolvedConfig(
        sla=SlaConfig(
            default=SlaTarget(first_response_minutes=1, resolution_minutes=1),
            clock="calendar",
            **over,
        )
    )
    return SlaCalculator(config, BusinessHours(timezone="UTC"), dt.UTC)


def _payload(data: dict[str, Any], calculator: SlaCalculator | None = None) -> dict[str, Any]:
    return build(
        data,
        now=NOW,
        zone=ZONE,
        zone_name="UTC",
        window_days=14,
        calculator=calculator or _calculator(),
    )


def _card(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return next(card for card in payload["cards"] if card["key"] == key)


# --- the arithmetic --------------------------------------------------------------------


#: Two priorities, an hour and eight to resolve against eight hours and three
#: days, on a calendar clock so these fixtures read as wall time.
def _realistic() -> SlaCalculator:
    config = ResolvedConfig(
        sla=SlaConfig(
            targets={
                "High": SlaTarget(first_response_minutes=60, resolution_minutes=480),
                "Low": SlaTarget(first_response_minutes=480, resolution_minutes=4_320),
            },
            clock="calendar",
        )
    )
    return SlaCalculator(config, BusinessHours(timezone="UTC"), dt.UTC)


def test_the_cards_count_what_they_say_they_count() -> None:
    site = _site()
    open_rows = [
        # High, three days old and never answered: both its clocks are over.
        (site, _ticket(1, priority=1, created=NOW - dt.timedelta(days=3))),
        # Low, answered, and its three-day resolution target runs out in an
        # hour: due soon, and not breached.
        (
            site,
            _ticket(
                2,
                priority=2,
                created=NOW - dt.timedelta(days=2, hours=23),
                responded=NOW - dt.timedelta(days=2, hours=22),
            ),
        ),
        # Low, answered yesterday, two days left to resolve: neither.
        (
            site,
            _ticket(
                3,
                priority=2,
                created=NOW - dt.timedelta(days=1),
                responded=NOW - dt.timedelta(hours=23),
            ),
        ),
        # Nobody is on it, and it was raised this morning.
        (
            site,
            _ticket(
                4,
                priority=2,
                created=NOW - dt.timedelta(hours=4),
                responded=NOW - dt.timedelta(hours=3, minutes=30),
                assignee=None,
            ),
        ),
    ]
    done_rows = [
        # Completed on Monday, which is inside this week.
        (
            site,
            _ticket(5, created=NOW - dt.timedelta(days=9), completed=NOW - dt.timedelta(days=2)),
        ),
        # Completed a fortnight ago: inside the chart's window, outside the week.
        (
            site,
            _ticket(6, created=NOW - dt.timedelta(days=13), completed=NOW - dt.timedelta(days=12)),
        ),
    ]
    payload = _payload(_data(open_rows, done_rows), _realistic())

    assert _card(payload, "open")["value"] == 4
    assert _card(payload, "breached")["value"] == 1
    assert _card(payload, "due_soon")["value"] == 1
    assert _card(payload, "unassigned")["value"] == 1
    assert _card(payload, "created_today")["value"] == 1
    assert _card(payload, "resolved_week")["value"] == 1


def test_a_breached_ticket_is_never_also_counted_as_due_soon() -> None:
    """Overdue by an hour is inside four hours of now in both directions; the
    card that says "not yet breached" has to mean it."""
    site = _site()
    rows = [(site, _ticket(1, created=NOW - dt.timedelta(hours=2)))]
    payload = _payload(_data(rows, []), _realistic())
    assert _card(payload, "breached")["value"] == 1
    assert _card(payload, "due_soon")["value"] == 0


def test_the_fortnight_chart_has_a_row_for_every_day_including_the_quiet_ones() -> None:
    site = _site()
    open_rows = [(site, _ticket(1, created=NOW - dt.timedelta(days=1)))]
    done_rows = [
        (site, _ticket(2, created=NOW - dt.timedelta(days=5), completed=NOW - dt.timedelta(days=1)))
    ]
    payload = _payload(_data(open_rows, done_rows))
    days = payload["charts"]["created_vs_resolved"]["days"]
    assert len(days) == 14
    assert days[-1]["day"] == NOW.date().isoformat()
    yesterday = next(d for d in days if d["day"] == (NOW - dt.timedelta(days=1)).date().isoformat())
    assert yesterday == {"day": yesterday["day"], "created": 1, "resolved": 1}
    # A day nothing happened on is a zero, not a missing bar.
    assert all("created" in day and "resolved" in day for day in days)


def test_a_ticket_created_before_the_window_is_not_counted_as_created_in_it() -> None:
    site = _site()
    rows = [(site, _ticket(1, created=NOW - dt.timedelta(days=40)))]
    payload = _payload(_data(rows, []))
    days = payload["charts"]["created_vs_resolved"]["days"]
    assert sum(day["created"] for day in days) == 0
    # It is still open, so it is still on the open card.
    assert _card(payload, "open")["value"] == 1


def test_a_ticket_returned_by_both_listings_is_counted_once() -> None:
    """Autotask should never do this. If it does, a bar must not double."""
    site = _site()
    ticket = _ticket(1, created=NOW - dt.timedelta(days=1))
    payload = _payload(_data([(site, ticket)], [(site, ticket)]))
    days = payload["charts"]["created_vs_resolved"]["days"]
    assert sum(day["created"] for day in days) == 1


def test_the_customer_chart_names_five_and_counts_the_rest_without_drawing_them() -> None:
    """Live, the sixth bar was bigger than the other five together and made the
    chart useless. The rest are counted and stated, never quietly dropped."""
    rows = []
    for index, name in enumerate(["A", "B", "C", "D", "E", "F", "G"]):
        for _ in range(7 - index):
            rows.append((_site(name, name.lower()), _ticket(len(rows) + 1, created=NOW)))
    chart = _payload(_data(rows, []))["charts"]["by_customer"]
    assert [s["label"] for s in chart["series"]] == ["A", "B", "C", "D", "E"]
    assert len(chart["series"]) == 5
    rest = chart["remainder"]
    assert rest["customers"] == 2
    assert rest["count"] == 2 + 1  # F has two, G has one
    assert str(rest["count"]) in rest["note"]
    # Drawn plus counted is still the whole population.
    open_count = _card(_payload(_data(rows, [])), "open")["value"]
    assert sum(s["value"] for s in chart["series"]) + rest["count"] == open_count


def test_with_five_customers_or_fewer_there_is_no_remainder_to_mention() -> None:
    rows = [(_site("A", "a"), _ticket(1, created=NOW)), (_site("B", "b"), _ticket(2, created=NOW))]
    chart = _payload(_data(rows, []))["charts"]["by_customer"]
    assert chart["remainder"]["count"] == 0
    assert chart["remainder"]["note"] == ""


def test_unassigned_is_last_on_the_technician_chart_and_named_as_a_gap() -> None:
    site = _site()
    rows = [
        (site, _ticket(1, created=NOW, assignee=7)),
        (site, _ticket(2, created=NOW, assignee=None)),
        (site, _ticket(3, created=NOW, assignee=None)),
        (site, _ticket(4, created=NOW, assignee=8)),
        (site, _ticket(5, created=NOW, assignee=8)),
    ]
    series = _payload(_data(rows, []))["charts"]["by_technician"]["series"]
    assert [s["label"] for s in series] == ["Priya", "Dan", "Unassigned"]
    assert series[-1]["value"] == 2


def test_a_priority_the_tenant_has_no_label_for_still_gets_a_bar() -> None:
    site = _site()
    rows = [(site, _ticket(1, created=NOW, priority=99))]
    series = _payload(_data(rows, [], priorities={}))["charts"]["by_priority"]["series"]
    assert series == [{"label": "No priority set", "value": 1, "note": ""}]


def test_every_figure_carries_a_written_definition() -> None:
    payload = _payload(_data([], []))
    for card in payload["cards"]:
        assert card["definition"].strip(), card["key"]
    for name, chart in payload["charts"].items():
        assert chart["definition"].strip(), name


def test_every_card_names_a_filter_the_queue_implements() -> None:
    """A card pointing at a filter nothing implements would quietly do nothing,
    and the figure and the rows it opens would be two different populations.
    `CARD_FILTERS` is the set the page agrees to, in `js/tickets.js`."""
    payload = _payload(_data([], []))
    for card in payload["cards"]:
        assert card["filter"] in CARD_FILTERS, card["key"]


def test_the_resolved_list_stops_but_the_count_does_not() -> None:
    site = _site()
    done = [
        (
            site,
            _ticket(
                index,
                created=NOW - dt.timedelta(days=3),
                completed=NOW - dt.timedelta(hours=index),
            ),
        )
        for index in range(1, 8)
    ]
    payload = build(
        _data([], done),
        now=NOW,
        zone=ZONE,
        zone_name="UTC",
        window_days=14,
        calculator=_calculator(),
        max_resolved=3,
    )
    resolved = payload["resolved_this_week"]
    assert resolved["count"] == 7
    assert resolved["listed"] == 3
    assert resolved["truncated"] is True
    assert resolved["truncation_note"]
    # Most recently resolved first, so the list that is kept is the useful end.
    assert [t["ticket_number"] for t in resolved["tickets"]] == ["T1", "T2", "T3"]


def test_a_truncated_listing_is_reported_rather_than_passed_off_as_whole() -> None:
    payload = _payload(_data([], [], truncated=True))
    assert payload["truncated"] is True


def test_the_zone_it_counted_in_is_named() -> None:
    payload = build(
        _data([], []),
        now=NOW,
        zone=ZONE,
        zone_name="America/Chicago",
        window_days=14,
        calculator=_calculator(),
    )
    assert payload["time_zone"] == "America/Chicago"
    assert "America/Chicago" in _card(payload, "created_today")["definition"]


# --- the endpoint -----------------------------------------------------------------------


@pytest.fixture
async def world(make_client: Make, make_org: Make, make_user: Make) -> dict[str, str]:
    """Two clients and one account per role. The values are addresses, because
    that is what signing in takes."""
    acme = await make_client("acme-it", "Acme IT")
    await make_client("other-it", "Other IT")
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    acme_org = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=acme)
    await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER)
    await make_user(acme_org, "boss@acme.test", Role.FRANCHISEE_ADMIN)
    await make_user(acme_org, "tech@acme.test", Role.FRANCHISEE_TECHNICIAN)
    await make_user(acme_org, "viewer@acme.test", Role.FRANCHISEE_VIEWER)
    return {
        "staff": "engineer@inputiv.test",
        "boss": "boss@acme.test",
        "tech": "tech@acme.test",
        "viewer": "viewer@acme.test",
    }


@pytest.fixture
def fake_autotask(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Autotask, replaced at the connector boundary. `reads` counts the calls,
    which is how the cache is proved to be a cache."""
    knobs: dict[str, Any] = {"reads": 0}
    statistics_module.forget()

    @asynccontextmanager
    async def fake_connector_for(client_id: uuid.UUID, connector: Any, **kwargs: Any) -> Any:
        class FakeAutotask:
            async def list_tickets(self, **kwargs: Any) -> tuple[list[Any], bool]:
                knobs["reads"] += 1
                if kwargs.get("open_only"):
                    return [(_site(), _ticket(1, created=NOW))], False
                return [], False

            async def get_resources(self, ids: list[int]) -> dict[int, Any]:
                return {7: SimpleNamespace(name="Dan")}

            async def label_for(self, entity: str, field: str, value: Any) -> str:
                return "High"

        yield FakeAutotask()

    monkeypatch.setattr(statistics_module, "connector_for", fake_connector_for)
    return knobs


@pytest.mark.parametrize("who", ["staff", "boss", "tech"])
async def test_the_roles_that_may_run_a_model_may_read_the_statistics(
    http: httpx.AsyncClient, world: dict[str, str], fake_autotask: dict[str, Any], who: str
) -> None:
    session = await sign_in(http, world[who])
    if who == "staff":
        await session.post("/auth/switch-client", json={"client": "acme-it"})
    response = await http.get("/tickets/statistics")
    assert response.status_code == 200
    assert response.json()["cards"][0]["key"] == "open"


async def test_a_viewer_is_refused(
    http: httpx.AsyncClient, world: dict[str, str], fake_autotask: dict[str, Any]
) -> None:
    """A viewer has no Tickets nav entry and no business making the engine read
    a tenant's queue. The endpoint refuses rather than trusting the nav."""
    await sign_in(http, world["viewer"])
    assert (await http.get("/tickets/statistics")).status_code == 403


async def test_no_session_is_refused(
    http: httpx.AsyncClient, fake_autotask: dict[str, Any]
) -> None:
    assert (await http.get("/tickets/statistics")).status_code == 401


async def test_the_answer_says_when_it_was_worked_out_and_is_kept(
    http: httpx.AsyncClient, world: dict[str, str], fake_autotask: dict[str, Any]
) -> None:
    await sign_in(http, world["boss"])
    first = (await http.get("/tickets/statistics")).json()
    assert first["cached"] is False
    assert first["computed_at"]
    reads = fake_autotask["reads"]
    assert reads == 2  # the open listing and the completed one

    second = (await http.get("/tickets/statistics")).json()
    assert second["cached"] is True
    assert second["computed_at"] == first["computed_at"]
    assert fake_autotask["reads"] == reads  # no vendor was asked again


async def test_the_cache_is_per_client_and_never_shared(
    http: httpx.AsyncClient, world: dict[str, str], fake_autotask: dict[str, Any]
) -> None:
    """Hard rule 1 in the one place a cache could break it: a second client must
    not be served the first one's figures."""
    session = await sign_in(http, world["staff"])
    await session.post("/auth/switch-client", json={"client": "acme-it"})
    await http.get("/tickets/statistics")
    reads = fake_autotask["reads"]

    await session.post("/auth/switch-client", json={"client": "other-it"})
    second = await http.get("/tickets/statistics")
    assert second.status_code == 200
    assert second.json()["cached"] is False
    assert fake_autotask["reads"] > reads


async def test_a_vendor_that_does_not_answer_is_a_502_and_not_a_page_of_zeroes(
    http: httpx.AsyncClient,
    world: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard rule 7: nothing is fabricated. An Autotask that fails is said so,
    never drawn as an empty week."""
    from app.connectors.errors import UpstreamError

    statistics_module.forget()

    @asynccontextmanager
    async def broken(client_id: uuid.UUID, connector: Any, **kwargs: Any) -> Any:
        raise UpstreamError("Autotask returned 500", connector="autotask")
        yield  # pragma: no cover

    monkeypatch.setattr(statistics_module, "connector_for", broken)
    await sign_in(http, world["boss"])
    response = await http.get("/tickets/statistics")
    assert response.status_code == 502
    assert "autotask" in response.json()["detail"].lower()

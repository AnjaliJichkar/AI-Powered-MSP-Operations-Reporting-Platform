"""A ticket's own page, and the Devices side panel's markup contract.

Two things are worth a test here and neither is the JavaScript, which this
repository has no browser to run.

**The routes.** `/tickets/<number>` is a page, and it sits under the same path
prefix as the JSON the page reads. The JSON routers are included first so that
`/tickets/list` and `/tickets/statistics` stay themselves; if that order ever
changes, a ticket page would answer them and the queue would quietly break, so
it is asserted rather than remembered.

**The markup the scripts stand on.** A panel that traps focus, closes on Esc
and hands the keyboard back is only as good as the element it is drawn into:
the dialog role, the label it points at, the scrim and the three controls. A
test cannot press Esc, but it can prove the page still ships the thing Esc is
wired to — which is what silently disappears when somebody tidies a template.
"""

from __future__ import annotations

import httpx
import pytest

from app.models import OrganisationKind, Role
from tests.api.conftest import Make, sign_in

pytestmark = pytest.mark.usefixtures("clean")


@pytest.fixture
async def world(make_client: Make, make_org: Make, make_user: Make) -> dict[str, str]:
    acme = await make_client("acme-it", "Acme IT")
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


# --- the ticket page's route ------------------------------------------------------------


async def test_a_ticket_page_needs_a_session(http: httpx.AsyncClient) -> None:
    response = await http.get(
        "/tickets/T20260111", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/tickets/T20260111"


@pytest.mark.parametrize("who", ["boss", "tech"])
async def test_the_page_renders_and_names_the_ticket(
    http: httpx.AsyncClient, world: dict[str, str], who: str
) -> None:
    """The shell and the ticket number, and nothing else: every figure on the
    page arrives from an endpoint that resolves the session itself."""
    await sign_in(http, world[who])
    response = await http.get("/tickets/T20260111")
    assert response.status_code == 200
    assert "T20260111" in response.text
    assert 'id="ticket-ref"' in response.text
    assert "js/ticket.js" in response.text


async def test_a_viewer_gets_reports_rather_than_a_dead_end(
    http: httpx.AsyncClient, world: dict[str, str]
) -> None:
    """A viewer has no Tickets nav entry and the endpoints behind the page would
    refuse them, so the page is not drawn for them at all."""
    await sign_in(http, world["viewer"])
    response = await http.get("/tickets/T20260111")
    assert response.status_code == 200
    assert 'id="ticket-ref"' not in response.text
    assert "js/reports.js" in response.text


async def test_the_page_escapes_the_reference_it_is_given(
    http: httpx.AsyncClient, world: dict[str, str]
) -> None:
    """The number in the address bar is untrusted text like any other: it
    reaches the page as characters, never as markup."""
    await sign_in(http, world["boss"])
    response = await http.get("/tickets/%3Cimg%20src=x%20onerror=alert(1)%3E")
    assert response.status_code == 200
    assert "<img src=x" not in response.text
    assert "&lt;img" in response.text or "\\u003c" in response.text


async def test_the_breadcrumb_and_the_tabs_are_on_the_page(
    http: httpx.AsyncClient, world: dict[str, str]
) -> None:
    """The shell of the page, which the script fills.

    The folded rows became tabs: an engineer reads what Desk found, with the
    ticket and the machine beside it, and opens the readings, the plan or the
    record when they want one — rather than scrolling past four panels to
    reach the plan. Every target the script writes into has to be here, or it
    writes into nothing and says nothing about it.
    """
    text = (await sign_in(http, world["boss"]) and await http.get("/tickets/T1")).text
    assert 'id="back-to-queue"' in text  # the breadcrumb back to the queue
    assert 'id="step-links"' in text  # previous and next through it
    assert 'id="ws-question"' in text  # the machine question, when it is one
    for panel in ("overview", "readings", "plan", "history"):
        assert f'id="tab-{panel}"' in text
        assert f'id="tab-button-{panel}"' in text
    assert 'id="ticket-body"' in text  # the ticket's own words, in the rail
    assert 'id="enrichment-body"' in text  # what the tools said
    assert 'id="plan-body"' in text  # the plan, versioned
    assert 'id="history-body"' in text  # who did what to the suggestion
    # The readings are also a dialog over the page, which has to be a real one.
    assert 'id="readings-panel"' in text
    assert 'role="dialog"' in text
    assert 'aria-modal="true"' in text


# --- the JSON under the same prefix is not shadowed ---------------------------------------


@pytest.mark.parametrize("path", ["/tickets/list", "/tickets/statistics", "/tickets/pending"])
async def test_the_json_routes_are_still_json(
    http: httpx.AsyncClient, world: dict[str, str], path: str
) -> None:
    """These live under the same prefix as `/tickets/<number>`. The API routers
    are included first, so they answer; a page here would be a queue that broke
    without anything failing."""
    await sign_in(http, world["boss"])
    response = await http.get(path, headers={"Accept": "text/html"})
    # Whatever it answers — data, or a refusal because no vendor is configured —
    # it is never the ticket page's HTML.
    assert "text/html" not in response.headers.get("content-type", "")
    assert 'id="ticket-ref"' not in response.text


# --- the Devices panel's markup -----------------------------------------------------------


async def test_the_devices_page_ships_a_dialog_the_script_can_open(
    http: httpx.AsyncClient, world: dict[str, str]
) -> None:
    await sign_in(http, world["boss"])
    text = (await http.get("/devices")).text
    assert 'id="detail-panel"' in text
    assert 'role="dialog"' in text
    assert 'aria-modal="true"' in text
    # It is labelled by the machine's name, which the script writes into it.
    assert 'aria-labelledby="detail-title"' in text
    assert 'id="detail-title"' in text


async def test_the_panel_has_a_scrim_and_the_three_controls(
    http: httpx.AsyncClient, world: dict[str, str]
) -> None:
    """Close, and the two that step through the list. The scrim is what makes
    "click outside" a target rather than a guess."""
    await sign_in(http, world["boss"])
    text = (await http.get("/devices")).text
    for element in (
        'id="panel-scrim"',
        'id="detail-close"',
        'id="detail-prev"',
        'id="detail-next"',
    ):
        assert element in text, element


async def test_the_panel_starts_closed(http: httpx.AsyncClient, world: dict[str, str]) -> None:
    """Server-rendered hidden, so a slow script never flashes a dialog over a
    list nobody asked to leave."""
    await sign_in(http, world["boss"])
    text = (await http.get("/devices")).text
    panel = text[text.index('id="detail-panel"') : text.index('id="detail-panel"') + 400]
    assert "hidden" in panel


async def test_a_link_to_one_machine_is_served_the_same_page(
    http: httpx.AsyncClient, world: dict[str, str]
) -> None:
    """`?device=<id>` is read by the script, not the route: the page is the
    same page, and the endpoint behind it is what refuses another client's."""
    await sign_in(http, world["boss"])
    plain = await http.get("/devices")
    linked = await http.get("/devices?device=2b3c4d5e-0000-0000-0000-000000000000")
    assert linked.status_code == 200
    assert linked.text == plain.text


# --- the shell every page is drawn in -------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/devices", "/tickets", "/coverage", "/settings"])
async def test_every_page_carries_the_menu_button_and_its_scrim(
    http: httpx.AsyncClient, world: dict[str, str], path: str
) -> None:
    """The nav becomes a drawer under 900px. The button is in the shell, so it
    is on every page or on none."""
    await sign_in(http, world["boss"])
    text = (await http.get(path)).text
    assert 'id="nav-toggle"' in text
    assert 'aria-controls="shell-nav"' in text
    assert 'id="nav-scrim"' in text

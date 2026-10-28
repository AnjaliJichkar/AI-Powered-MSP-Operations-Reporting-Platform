"""The pages: the shell, who sees which nav, and what an unsigned browser gets.

The pages carry no data of their own — every number on one arrives from a JSON
endpoint that resolves the session itself — so what is asserted here is the
shell: that the header names the right person and the right client, that the
client switcher exists only for the people who may switch, and that a browser
with no session is sent to sign in rather than handed an empty page.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.models import OrganisationKind, Role
from tests.api.conftest import Make, sign_in

pytestmark = pytest.mark.usefixtures("clean")


@pytest.fixture
async def world(make_client: Make, make_org: Make, make_user: Make) -> dict[str, Any]:
    acme = await make_client("acme-it", "Acme IT")
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    acme_org = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=acme)
    return {
        "acme": acme,
        "staff": await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER),
        "admin": await make_user(inputiv, "admin@inputiv.test", Role.INPUTIV_ADMIN),
        "boss": await make_user(acme_org, "boss@acme.test", Role.FRANCHISEE_ADMIN),
        "viewer": await make_user(acme_org, "viewer@acme.test", Role.FRANCHISEE_VIEWER),
    }


PAGES = ["/", "/reports", "/sites", "/integrations", "/settings"]


# --- without a session ---------------------------------------------------------


@pytest.mark.parametrize("path", [*PAGES, "/tbr"])
async def test_a_browser_with_no_session_is_sent_to_sign_in(
    http: httpx.AsyncClient, path: str
) -> None:
    """A page, not a 401. The browser has nowhere to show a JSON refusal."""
    response = await http.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_the_redirect_remembers_where_they_were_going(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get("/sites", headers={"Accept": "text/html"}, follow_redirects=False)
    assert response.headers["location"] == "/login?next=/sites"


async def test_an_api_call_still_gets_its_401(http: httpx.AsyncClient) -> None:
    """A redirect would reach a `fetch` as a 200 full of markup."""
    response = await http.get("/auth/me", headers={"Accept": "application/json"})
    assert response.status_code == 401

    # And a page request that is not a GET keeps its status too.
    posted = await http.post("/conversations", json={}, headers={"Accept": "text/html"})
    assert posted.status_code == 401


async def test_the_sign_in_page_is_public(http: httpx.AsyncClient) -> None:
    response = await http.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    # It is the one page with no shell: nobody to name, no nav to offer.
    assert "shell-nav" not in response.text


async def test_the_stylesheet_and_scripts_are_served(http: httpx.AsyncClient) -> None:
    for path in ("/static/app.css", "/static/js/common.js", "/static/js/login.js"):
        response = await http.get(path)
        assert response.status_code == 200, path
        assert response.content


# --- the shell ------------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES)
async def test_every_page_renders_for_staff_on_a_client(
    http: httpx.AsyncClient, world: dict[str, Any], path: str
) -> None:
    staff = await sign_in(http, "engineer@inputiv.test")
    await staff.post("/auth/switch-client", json={"client": "acme-it"})

    response = await http.get(path)
    assert response.status_code == 200, path
    body = response.text
    # One shell, one stylesheet, on every page.
    assert 'class="shell"' in body
    assert "/static/app.css" in body
    assert "engineer@inputiv.test" in body


async def test_the_header_names_the_user_their_role_and_the_client(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    session = await sign_in(http, "boss@acme.test")
    body = (await http.get("/reports")).text

    assert "boss@acme.test" in body
    # The role is shown in words. Nobody should have to read an enum value.
    assert "Administrator" in body and "franchisee_admin" not in body.split("window.DESK")[0]
    assert "Acme IT" in body
    assert session.csrf in body  # handed to the page, not read from a cookie


async def test_the_client_switcher_is_for_inputiv_staff_only(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """A franchisee has one client; a picker would imply there were others."""
    await sign_in(http, "boss@acme.test")
    theirs = (await http.get("/reports")).text
    # Their client is named, and the control that would offer another is not
    # there to press: the block is drawn disabled rather than left out, so the
    # header does not change shape between the two audiences.
    assert "Acme IT" in theirs
    switcher = theirs.split('id="client-switch"')[1].split(">")[0]
    assert "disabled" in switcher

    await sign_in(http, "engineer@inputiv.test")
    ours = (await http.get("/reports")).text
    assert 'id="client-switch"' in ours
    assert "disabled" not in ours.split('id="client-switch"')[1].split(">")[0]


async def test_staff_with_no_client_still_get_a_page(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """The switcher is the point of it: the page must render in order to offer one."""
    await sign_in(http, "engineer@inputiv.test")
    response = await http.get("/sites")
    assert response.status_code == 200
    assert 'id="client-switch"' in response.text
    assert "Choose a client" in response.text
    assert "window.DESK" in response.text


# --- the nav --------------------------------------------------------------------


async def test_a_viewer_is_not_offered_ask(http: httpx.AsyncClient, world: dict[str, Any]) -> None:
    """They cannot use it — `/chat` refuses them — so it is not in the nav, and
    the front page shows them what they can read instead of a dead end."""
    await sign_in(http, "viewer@acme.test")
    body = (await http.get("/reports")).text
    assert ">Ask<" not in body
    assert ">Reports<" in body and ">Sites<" in body and ">Settings<" in body

    landing = await http.get("/")
    assert landing.status_code == 200
    assert "Reports" in landing.text


async def test_everybody_else_is_offered_ask(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    for email in ("engineer@inputiv.test", "boss@acme.test"):
        await sign_in(http, email)
        assert ">Ask<" in (await http.get("/reports")).text, email


async def test_the_current_page_is_marked_in_the_nav(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    await sign_in(http, "boss@acme.test")
    nav = (await http.get("/sites")).text.split('<nav class="nav">')[1].split("</nav>")[0]
    links = [chunk for chunk in nav.split("<a ") if "href=" in chunk]
    marked = [chunk for chunk in links if 'aria-current="page"' in chunk]
    assert len(marked) == 1
    assert 'href="/sites"' in marked[0]


async def test_the_old_tbr_address_still_works(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """A bookmark from before the nav existed should not break."""
    await sign_in(http, "boss@acme.test")
    assert (await http.get("/tbr")).status_code == 200


# --- administration ---------------------------------------------------------------


async def test_the_people_panel_is_for_administrators(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    await sign_in(http, "viewer@acme.test")
    assert "Open invites" not in (await http.get("/settings")).text

    await sign_in(http, "boss@acme.test")
    assert "Open invites" in (await http.get("/settings")).text


async def test_everybody_gets_their_own_password_and_second_factor(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    await sign_in(http, "viewer@acme.test")
    body = (await http.get("/settings")).text
    assert "Change password" in body
    assert "Two-factor authentication" in body


# --- the logo ------------------------------------------------------------------------


async def test_a_client_with_no_logo_stored_is_a_404_and_no_broken_image(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """The template only draws an `<img>` when the server knows there is a file,
    so a client without one gets a header with a name in it and nothing else."""
    await sign_in(http, "boss@acme.test")
    assert (await http.get("/branding/logo")).status_code == 404
    assert "client-logo" not in (await http.get("/reports")).text

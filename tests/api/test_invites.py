"""Invitations, end to end: the link, the page, and every way a link can fail.

A token by itself was never usable by the person holding it. What is asserted
here is the whole path — an administrator creates an invitation and gets a
link, the person opens it and is told who invited them and into what, chooses a
password, and lands signed in — and then each of the four endings that are not
that, because a week-old link is an ordinary thing and not an error.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.service import InviteState, describe_invite
from app.models import AuditEventType, Invite, OrganisationKind, Role, User, UserStatus
from app.settings import Settings
from tests.api.conftest import PASSWORD, Make, audit_rows, sign_in

pytestmark = pytest.mark.usefixtures("clean")

NEW_PASSWORD = "a-long-enough-new-password"


@pytest.fixture
async def world(make_client: Make, make_org: Make, make_user: Make) -> dict[str, Any]:
    client = await make_client("acme-it", "Acme IT")
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    acme = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=client, name="Acme IT")
    return {
        "client": client,
        "acme": acme,
        "admin": await make_user(inputiv, "admin@inputiv.test", Role.INPUTIV_ADMIN),
        "boss": await make_user(acme, "boss@acme.test", Role.FRANCHISEE_ADMIN),
    }


async def make_invite(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    *,
    email: str = "new@acme.test",
    role: str = "franchisee_viewer",
    as_email: str = "boss@acme.test",
    **extra: Any,
) -> dict[str, Any]:
    """One invitation, as an administrator would make it."""
    session = await sign_in(http, as_email)
    response = await session.post("/auth/invites", json={"email": email, "role": role, **extra})
    assert response.status_code == 201, response.text
    return dict(response.json())


async def browser(http: httpx.AsyncClient) -> httpx.AsyncClient:
    """A second, empty browser: the person receiving the link is not the
    administrator who sent it, and must not inherit their session."""
    return httpx.AsyncClient(transport=http._transport, base_url="https://engine.test")


# --- the link -----------------------------------------------------------------


async def test_creating_an_invitation_returns_a_link_not_just_a_token(
    http: httpx.AsyncClient, world: dict[str, Any], settings: Settings
) -> None:
    """A bare token is not something the person receiving it can use."""
    invite = await make_invite(http, world)

    assert invite["token"]
    assert invite["url"] == f"{settings.public_base_url}/invite/{invite['token']}"
    assert invite["url"].startswith("http")
    assert invite["expires_at"]


async def test_the_link_opens_a_page_naming_who_invited_them_and_into_what(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    invite = await make_invite(http, world)

    async with await browser(http) as theirs:
        page = await theirs.get(f"/invite/{invite['token']}")

    assert page.status_code == 200
    body = page.text
    assert "boss@acme.test" in body  # who invited them
    assert "Acme IT" in body  # into what
    assert "Viewer" in body  # as what, in words
    assert "new@acme.test" in body  # which account this becomes
    # The password form, and no shell: there is no session to draw one from.
    assert 'id="password"' in body and 'id="confirm"' in body
    assert "shell-nav" not in body


# --- accepting -----------------------------------------------------------------


async def test_accepting_sets_the_password_signs_them_in_and_lands_them_on_ask(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    invite = await make_invite(http, world)

    async with await browser(http) as theirs:
        accepted = await theirs.post(
            "/auth/invites/accept",
            json={"token": invite["token"], "password": NEW_PASSWORD, "display_name": "New Person"},
        )
        assert accepted.status_code == 201
        assert accepted.json()["email"] == "new@acme.test"
        assert accepted.json()["role"] == Role.FRANCHISEE_VIEWER.value
        # Already signed in: the cookie came back with the response, and the
        # front page is theirs to load.
        assert (await theirs.get("/auth/me")).status_code == 200
        landing = await theirs.get("/")
        assert landing.status_code == 200

    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "new@acme.test"))).scalar_one()
        row = (await db.execute(select(Invite))).scalar_one()
    assert user.status is UserStatus.ACTIVE
    assert user.display_name == "New Person"
    assert user.password_hash and user.password_hash.startswith("$argon2id$")
    assert row.accepted_at is not None and row.accepted_user_id == user.id

    events = await audit_rows(session_maker, event_type=AuditEventType.INVITE_ACCEPTED)
    assert len(events) == 1 and events[0].actor_user_id == user.id


async def test_the_new_password_is_the_one_that_works(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    invite = await make_invite(http, world)
    async with await browser(http) as theirs:
        await theirs.post(
            "/auth/invites/accept", json={"token": invite["token"], "password": NEW_PASSWORD}
        )

    async with await browser(http) as later:
        assert (
            await later.post("/auth/login", json={"email": "new@acme.test", "password": PASSWORD})
        ).status_code == 401
        signed = await later.post(
            "/auth/login", json={"email": "new@acme.test", "password": NEW_PASSWORD}
        )
        assert signed.status_code == 200


async def test_a_password_that_is_too_short_is_refused_and_spends_nothing(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A refused attempt must leave the link usable: otherwise a typo costs
    somebody their invitation."""
    invite = await make_invite(http, world)

    async with await browser(http) as theirs:
        refused = await theirs.post(
            "/auth/invites/accept", json={"token": invite["token"], "password": "short"}
        )
        assert refused.status_code == 422
        assert "12 characters" in refused.json()["detail"]

        # Still open, and still works.
        assert (await theirs.get(f"/invite/{invite['token']}")).status_code == 200
        assert (
            await theirs.post(
                "/auth/invites/accept",
                json={"token": invite["token"], "password": NEW_PASSWORD},
            )
        ).status_code == 201

    async with session_maker() as db:
        assert (await db.execute(select(Invite))).scalar_one().accepted_at is not None


# --- the four endings that are not acceptance ------------------------------------


async def test_an_unknown_token_says_so_on_a_page(http: httpx.AsyncClient) -> None:
    async with await browser(http) as theirs:
        page = await theirs.get("/invite/not-a-real-token-at-all")
        assert page.status_code == 404
        assert "not one we recognise" in page.text
        # A way forward, and no form to fill in.
        assert "/login" in page.text
        assert 'id="password"' not in page.text

        refused = await theirs.post(
            "/auth/invites/accept",
            json={"token": "not-a-real-token-at-all", "password": NEW_PASSWORD},
        )
        assert refused.status_code == 400
        assert "not one we recognise" in refused.json()["detail"]


async def test_an_expired_token_says_so(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    invite = await make_invite(http, world)
    async with session_maker() as db:
        await db.execute(
            update(Invite).values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
        )
        await db.commit()

    async with await browser(http) as theirs:
        page = await theirs.get(f"/invite/{invite['token']}")
        assert page.status_code == 410
        assert "has expired" in page.text
        assert 'id="password"' not in page.text

        refused = await theirs.post(
            "/auth/invites/accept",
            json={"token": invite["token"], "password": NEW_PASSWORD},
        )
        assert refused.status_code == 400
        assert "has expired" in refused.json()["detail"]

    # And nothing was created by the attempt.
    async with session_maker() as db:
        assert (await db.execute(select(User).where(User.email == "new@acme.test"))).first() is None


async def test_a_token_that_has_been_used_says_so(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """The second person to open a forwarded link is told the account exists."""
    invite = await make_invite(http, world)
    async with await browser(http) as first:
        assert (
            await first.post(
                "/auth/invites/accept",
                json={"token": invite["token"], "password": NEW_PASSWORD},
            )
        ).status_code == 201

    async with await browser(http) as second:
        page = await second.get(f"/invite/{invite['token']}")
        assert page.status_code == 410
        assert "already been used" in page.text

        refused = await second.post(
            "/auth/invites/accept",
            json={"token": invite["token"], "password": "another-long-password"},
        )
        assert refused.status_code == 400
        assert "already been used" in refused.json()["detail"]


async def test_a_withdrawn_token_says_so(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    invite = await make_invite(http, world)
    async with session_maker() as db:
        await db.execute(update(Invite).values(revoked_at=dt.datetime.now(dt.UTC)))
        await db.commit()

    async with await browser(http) as theirs:
        page = await theirs.get(f"/invite/{invite['token']}")
        assert page.status_code == 410
        assert "withdrawn" in page.text


async def test_an_address_that_gained_an_account_another_way(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    make_user: Make,
) -> None:
    """The invitation is open, and there is nothing left for it to do."""
    invite = await make_invite(http, world)
    await make_user(world["acme"], "new@acme.test", Role.FRANCHISEE_VIEWER)

    async with await browser(http) as theirs:
        page = await theirs.get(f"/invite/{invite['token']}")
        assert page.status_code == 409
        assert "already an account" in page.text


# --- reissuing --------------------------------------------------------------------


async def test_a_second_invitation_is_refused_unless_it_replaces_the_first(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """Two live tokens for one address is the state nobody wants."""
    first = await make_invite(http, world)

    session = await sign_in(http, "boss@acme.test")
    duplicate = await session.post(
        "/auth/invites", json={"email": "new@acme.test", "role": "franchisee_viewer"}
    )
    assert duplicate.status_code == 409
    assert "already has an invitation" in duplicate.json()["detail"]

    second = await make_invite(http, world, reissue=True)
    assert second["token"] != first["token"]

    async with await browser(http) as theirs:
        # The replaced one is withdrawn, and says so rather than failing oddly.
        stale = await theirs.get(f"/invite/{first['token']}")
        assert stale.status_code == 410 and "withdrawn" in stale.text
        # The new one works.
        assert (await theirs.get(f"/invite/{second['token']}")).status_code == 200
        assert (
            await theirs.post(
                "/auth/invites/accept",
                json={"token": second["token"], "password": NEW_PASSWORD},
            )
        ).status_code == 201


async def test_reissuing_records_what_it_replaced(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await make_invite(http, world)
    await make_invite(http, world, reissue=True)

    invited = await audit_rows(session_maker, event_type=AuditEventType.USER_INVITED)
    assert [row.payload["replaced"] for row in invited] == [0, 1]


# --- the description every caller shares --------------------------------------------


async def test_the_page_and_the_endpoint_never_disagree(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """One function decides what a token is, so a page cannot offer a form for
    a link the endpoint would then refuse."""
    invite = await make_invite(http, world)

    async with session_maker() as db:
        assert (await describe_invite(db, invite["token"])).state is InviteState.OPEN
        assert (await describe_invite(db, "nonsense")).state is InviteState.UNKNOWN

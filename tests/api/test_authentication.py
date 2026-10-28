"""Signing in: the cookie, the throttles, CSRF, expiry and the second factor.

These go through HTTP with the real resolver. Nothing here is overridden but
the database connection, so what is asserted is what a browser would get.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.sessions import CSRF_HEADER
from app.models import AuditEventType, AuditOutcome, OrganisationKind, Role, User, UserStatus
from app.models.organisation import Session as SessionRow
from app.security.totp import code_for, open_secret
from app.settings import Settings
from tests.api.conftest import PASSWORD, Make, audit_rows, sign_in

pytestmark = pytest.mark.usefixtures("clean")


@pytest.fixture
async def account(make_client: Make, make_org: Make, make_user: Make) -> User:
    client = await make_client("acme-it", "Acme IT")
    organisation = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=client)
    return await make_user(organisation, "boss@acme.test", Role.FRANCHISEE_ADMIN)  # type: ignore[no-any-return]


# --- the cookie ---------------------------------------------------------------


async def test_the_session_cookie_is_httponly_secure_and_lax(
    http: httpx.AsyncClient, account: User, settings: Settings
) -> None:
    """Script cannot read it, it does not travel in clear text, and another
    site's form post does not carry it."""
    response = await http.post("/auth/login", json={"email": account.email, "password": PASSWORD})
    assert response.status_code == 200

    header = response.headers["set-cookie"]
    assert header.startswith(f"{settings.session_cookie_name}=")
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=lax" in header.lower().replace("samesite=lax", "SameSite=lax")


async def test_the_cookie_holds_a_token_and_the_database_holds_its_hash(
    http: httpx.AsyncClient,
    account: User,
    settings: Settings,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A leaked table of sessions yields nothing that can be replayed."""
    await sign_in(http, account.email)
    token = http.cookies[settings.session_cookie_name]

    async with session_maker() as session:
        rows = (await session.execute(select(SessionRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].token_hash != token
    assert token not in rows[0].token_hash


async def test_a_forged_cookie_is_not_a_session(
    http: httpx.AsyncClient, account: User, settings: Settings
) -> None:
    await sign_in(http, account.email)
    http.cookies.set(settings.session_cookie_name, "not-a-real-token", domain="engine.test")
    assert (await http.get("/auth/me")).status_code == 401


async def test_signing_out_revokes_the_session_rather_than_deleting_it(
    http: httpx.AsyncClient,
    account: User,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    session = await sign_in(http, account.email)
    assert (await session.post("/auth/logout")).status_code == 204
    assert (await http.get("/auth/me")).status_code == 401

    async with session_maker() as db:
        rows = (await db.execute(select(SessionRow))).scalars().all()
    assert len(rows) == 1 and rows[0].revoked_at is not None

    logouts = await audit_rows(session_maker, event_type=AuditEventType.LOGOUT)
    assert len(logouts) == 1 and logouts[0].actor_user_id == account.id


# --- CSRF ---------------------------------------------------------------------


async def test_a_state_changing_request_without_the_csrf_token_is_refused(
    http: httpx.AsyncClient, account: User
) -> None:
    """The cookie alone is not enough: another site can send it, not read it."""
    session = await sign_in(http, account.email)

    # No header at all: this is what a cross-site form post looks like.
    bare = await http.post("/conversations", json={})
    assert bare.status_code == 403 and CSRF_HEADER in bare.json()["detail"]

    wrong = await http.post("/conversations", json={}, headers={CSRF_HEADER: "a-different-token"})
    assert wrong.status_code == 403

    assert (await session.post("/conversations", json={})).status_code == 201


async def test_a_read_needs_no_csrf_token(http: httpx.AsyncClient, account: User) -> None:
    await sign_in(http, account.email)
    assert (await http.get("/auth/me")).status_code == 200
    assert (await http.get("/conversations")).status_code == 200


async def test_one_sessions_csrf_token_is_not_anothers(
    http: httpx.AsyncClient, account: User, app: Any
) -> None:
    first = await sign_in(http, account.email)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://engine.test"
    ) as second_browser:
        second = await sign_in(second_browser, account.email)
        assert second.csrf != first.csrf
        # The other session's token, with this session's cookie: refused.
        crossed = await second_browser.post(
            "/conversations", json={}, headers={CSRF_HEADER: first.csrf}
        )
        assert crossed.status_code == 403


# --- throttling ----------------------------------------------------------------


async def test_an_account_locks_after_too_many_wrong_passwords(
    http: httpx.AsyncClient,
    account: User,
    settings: Settings,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The lockout is a column, so it survives a restart and follows the account."""
    for _ in range(settings.login_max_failures):
        refused = await http.post(
            "/auth/login", json={"email": account.email, "password": "wrong-password-here"}
        )
        assert refused.status_code == 401
        # Every refusal says the same thing, whatever the cause.
        assert refused.json()["detail"] == "that email address and password do not match an account"

    # Now even the right password is refused, and says why.
    locked = await http.post("/auth/login", json={"email": account.email, "password": PASSWORD})
    assert locked.status_code == 423
    assert "locked" in locked.json()["detail"]

    async with session_maker() as session:
        user = (await session.execute(select(User).where(User.id == account.id))).scalar_one()
        assert user.locked_until is not None

    failures = await audit_rows(session_maker, event_type=AuditEventType.LOGIN)
    assert all(row.outcome is AuditOutcome.DENIED for row in failures)
    # One of them is the attempt that locked it, and the last is the one
    # that was refused because it was already locked.
    assert [row.payload["locked"] for row in failures].count(True) == 1
    assert failures[-1].payload["reason"] == "locked"
    # The password is never in the record, under any key.
    assert not any("wrong-password-here" in str(row.payload) for row in failures)


async def test_the_address_is_rate_limited_whether_or_not_the_account_exists(
    http: httpx.AsyncClient, account: User, settings: Settings
) -> None:
    """A lockout cannot see one caller trying many accounts. This can."""
    for index in range(settings.login_rate_limit_per_minute):
        response = await http.post(
            "/auth/login", json={"email": f"nobody{index}@acme.test", "password": "x" * 20}
        )
        assert response.status_code == 401

    limited = await http.post(
        "/auth/login", json={"email": "nobody@acme.test", "password": "x" * 20}
    )
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


async def test_a_signing_in_that_works_clears_the_failures(
    http: httpx.AsyncClient,
    account: User,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await http.post("/auth/login", json={"email": account.email, "password": "wrong-one-here"})
    await sign_in(http, account.email)

    async with session_maker() as session:
        user = (await session.execute(select(User).where(User.id == account.id))).scalar_one()
    assert user.failed_login_count == 0 and user.last_login_at is not None

    successes = [
        row
        for row in await audit_rows(session_maker, event_type=AuditEventType.LOGIN)
        if row.outcome is AuditOutcome.SUCCESS
    ]
    assert len(successes) == 1 and successes[0].actor_user_id == account.id


async def test_an_invited_or_suspended_account_cannot_sign_in(
    http: httpx.AsyncClient, make_client: Make, make_org: Make, make_user: Make
) -> None:
    client = await make_client("beta-msp")
    organisation = await make_org(OrganisationKind.FRANCHISEE, "beta-people", client=client)
    invited = await make_user(
        organisation, "pending@beta.test", Role.FRANCHISEE_VIEWER, password=None
    )
    suspended = await make_user(
        organisation,
        "gone@beta.test",
        Role.FRANCHISEE_ADMIN,
        status=UserStatus.SUSPENDED,
    )
    assert invited.status is UserStatus.INVITED

    for email in (invited.email, suspended.email, "never-existed@beta.test"):
        response = await http.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert response.status_code == 401
        # The same message for all three: this cannot be used to find accounts.
        assert (
            response.json()["detail"] == "that email address and password do not match an account"
        )


# --- expiry ---------------------------------------------------------------------


async def test_an_idle_session_expires(
    http: httpx.AsyncClient,
    account: User,
    settings: Settings,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await sign_in(http, account.email)
    assert (await http.get("/auth/me")).status_code == 200

    stale = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=settings.session_idle_minutes + 1)
    async with session_maker() as session:
        await session.execute(update(SessionRow).values(last_seen_at=stale))
        await session.commit()

    assert (await http.get("/auth/me")).status_code == 401


async def test_the_absolute_expiry_is_never_extended_by_using_the_session(
    http: httpx.AsyncClient,
    account: User,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A stolen session cannot be kept alive by using it."""
    await sign_in(http, account.email)
    async with session_maker() as session:
        first = (await session.execute(select(SessionRow))).scalar_one().expires_at

    assert (await http.get("/auth/me")).status_code == 200
    async with session_maker() as session:
        assert (await session.execute(select(SessionRow))).scalar_one().expires_at == first

    async with session_maker() as session:
        await session.execute(
            update(SessionRow).values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
        await session.commit()
    assert (await http.get("/auth/me")).status_code == 401


async def test_changing_a_password_ends_every_session(
    http: httpx.AsyncClient, account: User
) -> None:
    session = await sign_in(http, account.email)
    changed = await session.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "an-entirely-new-password"},
    )
    assert changed.status_code == 204
    assert (await http.get("/auth/me")).status_code == 401

    # And the new password is the one that works.
    assert (
        await http.post("/auth/login", json={"email": account.email, "password": PASSWORD})
    ).status_code == 401
    await sign_in(http, account.email, "an-entirely-new-password")


async def test_a_password_that_is_too_short_is_refused(
    http: httpx.AsyncClient, account: User
) -> None:
    session = await sign_in(http, account.email)
    refused = await session.post(
        "/auth/password", json={"current_password": PASSWORD, "new_password": "short"}
    )
    assert refused.status_code == 422 and "12 characters" in refused.json()["detail"]


# --- the second factor ------------------------------------------------------------


async def test_totp_is_optional_and_only_required_once_confirmed(
    http: httpx.AsyncClient,
    account: User,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An abandoned enrolment never locks anybody out."""
    session = await sign_in(http, account.email)
    assert (await http.get("/auth/me")).json()["totp_enabled"] is False

    started = await session.post("/auth/totp/start")
    assert started.status_code == 200
    secret = started.json()["secret"]
    assert started.json()["otpauth_uri"].startswith("otpauth://totp/")

    # Enrolled but not confirmed: signing in still needs only a password.
    async with httpx.AsyncClient(
        transport=http._transport, base_url="https://engine.test"
    ) as other:
        await sign_in(other, account.email)

    wrong = await session.post("/auth/totp/confirm", json={"code": "000000"})
    assert wrong.status_code == 403

    confirmed = await session.post("/auth/totp/confirm", json={"code": code_for(secret)})
    assert confirmed.status_code == 204

    # From here a code is required, and the secret is stored encrypted.
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.id == account.id))).scalar_one()
    assert user.totp_secret is not None and secret not in user.totp_secret
    assert open_secret(user.totp_secret) == secret

    async with httpx.AsyncClient(
        transport=http._transport, base_url="https://engine.test"
    ) as third:
        refused = await third.post(
            "/auth/login", json={"email": account.email, "password": PASSWORD}
        )
        assert refused.status_code == 401
        assert refused.headers["X-Second-Factor"] == "required"

        accepted = await third.post(
            "/auth/login",
            json={"email": account.email, "password": PASSWORD, "totp_code": code_for(secret)},
        )
        assert accepted.status_code == 200
        assert accepted.json()["totp_enabled"] is True


async def test_turning_off_the_second_factor_needs_the_password(
    http: httpx.AsyncClient, account: User
) -> None:
    session = await sign_in(http, account.email)
    secret = (await session.post("/auth/totp/start")).json()["secret"]
    await session.post("/auth/totp/confirm", json={"code": code_for(secret)})

    refused = await session.post("/auth/totp/disable", json={"password": "not-the-password"})
    assert refused.status_code == 403

    done = await session.post("/auth/totp/disable", json={"password": PASSWORD})
    assert done.status_code == 204
    # Disabling ends every session, this one included.
    assert (await http.get("/auth/me")).status_code == 401
    assert (await sign_in(http, account.email)).body["totp_enabled"] is False


# --- invites -----------------------------------------------------------------------


async def test_an_invite_is_one_use_and_signs_the_new_account_in(
    http: httpx.AsyncClient,
    make_client: Make,
    make_org: Make,
    make_user: Make,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    client = await make_client("acme-it", "Acme IT")
    organisation = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=client)
    admin = await make_user(organisation, "boss@acme.test", Role.FRANCHISEE_ADMIN)

    session = await sign_in(http, admin.email)
    created = await session.post(
        "/auth/invites", json={"email": "New.Person@Acme.test", "role": "franchisee_viewer"}
    )
    assert created.status_code == 201
    token = created.json()["token"]
    assert created.json()["email"] == "new.person@acme.test"  # normalised

    async with httpx.AsyncClient(
        transport=http._transport, base_url="https://engine.test"
    ) as browser:
        accepted = await browser.post(
            "/auth/invites/accept",
            json={"token": token, "password": "a-brand-new-password", "display_name": "New"},
        )
        assert accepted.status_code == 201
        assert accepted.json()["email"] == "new.person@acme.test"
        assert accepted.json()["role"] == Role.FRANCHISEE_VIEWER.value
        # Already signed in: the cookie came with the response.
        assert (await browser.get("/auth/me")).status_code == 200

    # The token is spent.
    async with httpx.AsyncClient(
        transport=http._transport, base_url="https://engine.test"
    ) as again:
        reused = await again.post(
            "/auth/invites/accept", json={"token": token, "password": "another-long-password"}
        )
        assert reused.status_code == 400
        assert "already been used" in reused.json()["detail"]

    events = await audit_rows(session_maker, event_type=AuditEventType.INVITE_ACCEPTED)
    assert len(events) == 1


async def test_an_invite_token_is_stored_only_as_a_hash(
    http: httpx.AsyncClient,
    make_client: Make,
    make_org: Make,
    make_user: Make,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    client = await make_client("acme-it", "Acme IT")
    organisation = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=client)
    admin = await make_user(organisation, "boss@acme.test", Role.FRANCHISEE_ADMIN)
    session = await sign_in(http, admin.email)

    token = (
        await session.post(
            "/auth/invites", json={"email": "someone@acme.test", "role": "franchisee_viewer"}
        )
    ).json()["token"]

    from app.models import Invite

    async with session_maker() as db:
        invite = (await db.execute(select(Invite))).scalar_one()
    assert invite.token_hash != token and token not in invite.token_hash


async def test_an_unknown_or_expired_invite_is_refused(http: httpx.AsyncClient) -> None:
    response = await http.post(
        "/auth/invites/accept", json={"token": "x" * 40, "password": "a-long-enough-password"}
    )
    assert response.status_code == 400
    # The sentence the page would have shown. Every ending is covered in
    # tests/api/test_invites.py; this is the endpoint refusing on its own.
    assert "not one we recognise" in response.json()["detail"]

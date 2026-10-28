"""A real database, a real cookie, and real people to sign in as.

These tests do not use `tests/authfixtures.py`. They are the ones that have to
prove the resolver itself: that a cookie is what identifies a caller, that the
session row is what says which client they are on, and that neither can be
talked round by a request body. Everything goes through HTTP.

`base_url` is **https**. A `Secure` cookie is not stored by a client fetching
`http://`, so a suite that used plain http would silently test a session that
was never set — which is exactly the sort of thing this file exists to catch.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.ratelimit import LOGIN_LIMITER
from app.auth.service import create_organisation, create_user
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AuditLog,
    AutomationAlertTicket,
    AutomationKillSwitch,
    AutomationNote,
    AutomationNoteMode,
    AutomationReport,
    AutomationRun,
    AutomationSwitch,
    AutomationTriggerLog,
    AutomationTriggerSwitch,
    BlindCheck,
    Client,
    Conversation,
    CoverageExclusion,
    CoverageSnapshot,
    Invite,
    LabellingAnswer,
    LabellingItem,
    LabellingSet,
    Organisation,
    OrganisationKind,
    PracticeRecord,
    Role,
    Session,
    SuggestionReview,
    TicketPlan,
    TicketSuggestion,
    TicketWorkspace,
    User,
    UserStatus,
)
from app.settings import Settings
from tests.database import require_database

PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: Set to run these tests; `migrated_database` skips them all when it is not.
#: Not a `pytestmark` here — pytest ignores one in a conftest, which is how 172
#: tests came to fail on a bare assertion instead of skipping (tests/database.py).
LIVE_URL = os.environ.get("TEST_DATABASE_URL")

#: Long enough for the policy, and obviously not a real one.
PASSWORD = "correct-horse-battery-staple"
OTHER_PASSWORD = "a-different-long-password"


async def _drop_schema(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """A schema at head, however the last run ended.

    A plain `downgrade base` is not enough on its own here. These tests write
    audit event types that exist only at head, and a run that crashed mid-test
    leaves rows the earlier migrations' checks refuse — so the *next* run fails
    during set-up, which reads as a broken suite rather than a dirty database.
    When the downgrade will not go through, the schema is dropped instead.
    """
    url = require_database(LIVE_URL)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    try:
        command.downgrade(config, "base")
    except Exception:
        asyncio.run(_drop_schema(url))
    command.upgrade(config, "head")
    yield url
    try:
        command.downgrade(config, "base")
    except Exception:
        asyncio.run(_drop_schema(url))


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[Any]:
    built = create_async_engine(migrated_database, poolclass=NullPool)
    yield built
    await built.dispose()


@pytest.fixture
def session_maker(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture(autouse=True)
def _forget_rate_limits() -> Iterator[None]:
    """The limiter is process-wide; one test's attempts are not another's."""
    LOGIN_LIMITER.reset()
    yield
    LOGIN_LIMITER.reset()


@pytest.fixture
async def clean(session_maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Empty every table these tests write, before and after.

    In dependency order: audit rows and sessions point at users, users at
    organisations, organisations at clients. All of those keys are RESTRICT
    where they matter, which is the point of them.
    """

    async def wipe() -> None:
        async with session_maker() as session:
            # Conversations point at users and at clients, both RESTRICT, so
            # they go before either. Their messages cascade from them.
            for table in (
                # A labelling answer points at the account that judged it, and
                # that key is RESTRICT on purpose: nothing done to an account
                # may remove the record of what that person judged. So the
                # answers go before the users here, as the audit rows do.
                # An automation run points at the client and at the accounts
                # that started and approved it, all RESTRICT for the same
                # reason, so runs go first of all; their steps cascade.
                PracticeRecord,
                AutomationAlertTicket,
                AutomationNote,
                AutomationNoteMode,
                AutomationReport,
                AutomationTriggerLog,
                AutomationRun,
                AutomationSwitch,
                AutomationTriggerSwitch,
                AutomationKillSwitch,
                BlindCheck,
                SuggestionReview,
                TicketSuggestion,
                LabellingAnswer,
                LabellingItem,
                LabellingSet,
                TicketPlan,
                TicketWorkspace,
                CoverageExclusion,
                CoverageSnapshot,
                AuditLog,
                Conversation,
                Session,
                Invite,
                User,
                Organisation,
                Client,
            ):
                await session.execute(delete(table))
            await session.commit()

    await wipe()
    yield
    await wipe()


@pytest.fixture
def app(
    settings: Settings,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """The real application, with its sessions pointed at the test database.

    Nothing is overridden but the database: the scope resolver, the cookie and
    the CSRF check are the production ones.
    """

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    import app.api.reports as reports_api
    import app.connectors.factory as factory_module
    import app.reports.tbr.audit as audit_module
    import app.reports.tbr.pipeline as pipeline_module
    import app.tickets.alert_tools as alert_tools_module

    # Every module that opens a session of its own (`session_scope`), so no
    # request reaches the process-wide engine: its pool would hold connections
    # from an earlier test's event loop, and the first use on a new loop fails
    # with "'NoneType' object has no attribute 'send'" on a stale connection.
    for module in (
        reports_api,
        pipeline_module,
        audit_module,
        factory_module,
        alert_tools_module,
    ):
        monkeypatch.setattr(module, "session_scope", scope, raising=False)

    async def dependency() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    application = create_app(settings)
    application.dependency_overrides[get_session] = dependency
    return application


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # https, so the Secure session cookie is actually stored by the client.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://engine.test"
    ) as client:
        yield client


Make = Callable[..., Any]


@pytest.fixture
async def make_client(session_maker: async_sessionmaker[AsyncSession]) -> Make:
    async def _make(slug: str, name: str | None = None) -> Client:
        async with session_maker() as session:
            client = Client(slug=slug, name=name or slug.replace("-", " ").title())
            session.add(client)
            await session.commit()
            await session.refresh(client)
            return client

    return _make


@pytest.fixture
async def make_org(session_maker: async_sessionmaker[AsyncSession]) -> Make:
    async def _make(
        kind: OrganisationKind,
        slug: str,
        *,
        client: Client | None = None,
        name: str | None = None,
    ) -> Organisation:
        async with session_maker() as session:
            organisation = await create_organisation(
                session, kind=kind, slug=slug, name=name or slug.title(), client=client
            )
            await session.commit()
            await session.refresh(organisation)
            return organisation

    return _make


@pytest.fixture
async def make_user(session_maker: async_sessionmaker[AsyncSession]) -> Make:
    async def _make(
        organisation: Organisation,
        email: str,
        role: Role,
        *,
        password: str | None = PASSWORD,
        status: UserStatus | None = None,
    ) -> User:
        """`status=None` lets `create_user` decide: active with a password,
        invited without one, which is what an invite produces."""
        async with session_maker() as session:
            merged = await session.merge(organisation)
            user = await create_user(
                session,
                organisation=merged,
                email=email,
                role=role,
                password=password,
                status=status,
            )
            await session.commit()
            await session.refresh(user)
            user.organisation = merged
            return user

    return _make


class SignedIn:
    """A signed-in browser: the cookie is on the client, the token is here."""

    def __init__(self, http: httpx.AsyncClient, body: dict[str, Any]) -> None:
        self.http = http
        self.body = body
        self.csrf = body["csrf_token"]

    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        """A state-changing request, with the CSRF token this session expects."""
        headers = {**self.headers(), **kwargs.pop("headers", {})}
        return await self.http.post(url, headers=headers, **kwargs)


async def sign_in(
    http: httpx.AsyncClient, email: str, password: str = PASSWORD, **extra: Any
) -> SignedIn:
    response = await http.post("/auth/login", json={"email": email, "password": password, **extra})
    assert response.status_code == 200, response.text
    return SignedIn(http, response.json())


async def audit_rows(
    session_maker: async_sessionmaker[AsyncSession], **where: Any
) -> list[AuditLog]:
    from sqlalchemy import select

    async with session_maker() as session:
        query = select(AuditLog).order_by(AuditLog.id)
        for column, value in where.items():
            query = query.where(getattr(AuditLog, column) == value)
        return list((await session.execute(query)).scalars().all())


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()

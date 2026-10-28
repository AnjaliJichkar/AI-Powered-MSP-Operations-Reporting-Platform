"""Shared fixtures for the agent tests: a real database, a scripted model's tools.

The database is real because the audit trail and the conversation threads are
the things being proven. The tools are throwaway stand-ins for a connector.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent.registry import clear_registry, tool
from app.connectors.errors import NotFound, RateLimited
from app.models import (
    AuditLog,
    Client,
    ConnectorName,
    Conversation,
    Organisation,
    OrganisationKind,
    Role,
    Session,
    User,
    UserStatus,
)
from app.settings import Settings, get_settings
from tests.database import require_database

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_URL = os.environ.get("TEST_DATABASE_URL")

DEMO_TOOL_NAMES = [
    "demo_list",
    "demo_alerts",
    "demo_rate_limited",
    "demo_not_found",
    "demo_slow",
    "demo_picky",
    "demo_unparseable",
]


class DemoResult(BaseModel):
    device_count: int
    devices: list[str]
    truncated: bool = False


#: Every (client_id, kwargs) the recording tools were called with.
CALLS: list[tuple[str, dict[str, Any]]] = []


@pytest.fixture
def demo_tools() -> Iterator[None]:
    """Register tools that stand in for a connector, then remove them."""
    CALLS.clear()

    @tool(ConnectorName.NINJAONE, name="demo_list")
    async def demo_list(client_id: str, days: int = 7) -> DemoResult:
        """List demo devices for a client. Used only by the loop tests."""
        CALLS.append((client_id, {"days": days}))
        return DemoResult(device_count=2, devices=["PC-01", "PC-02"])

    @tool(ConnectorName.NINJAONE, name="demo_alerts")
    async def demo_alerts(client_id: str) -> DemoResult:
        """List demo alerts for a client. Used only by the loop tests."""
        CALLS.append((client_id, {}))
        return DemoResult(device_count=1, devices=["PC-01"])

    @tool(ConnectorName.NINJAONE, name="demo_rate_limited")
    async def demo_rate_limited(client_id: str) -> DemoResult:
        """A tool whose vendor is throttling us. Used only by the loop tests."""
        raise RateLimited(
            "ninjaone rate limit not cleared after 3 attempts",
            connector="ninjaone",
            retry_after=30.0,
        )

    @tool(ConnectorName.NINJAONE, name="demo_not_found")
    async def demo_not_found(client_id: str) -> DemoResult:
        """A tool whose object does not exist. Used only by the loop tests."""
        raise NotFound("no such device", connector="ninjaone", status_code=404)

    @tool(ConnectorName.NINJAONE, name="demo_picky")
    async def demo_picky(client_id: str, severity: str = "critical") -> DemoResult:
        """A tool that refuses a value it does not know. Used only by the loop tests."""
        if severity not in ("critical", "important"):
            raise ValueError(
                f"severity must be one of critical, important; got {severity!r}. "
                "Nothing was sent to NinjaOne."
            )
        return DemoResult(device_count=0, devices=[])

    @tool(ConnectorName.NINJAONE, name="demo_unparseable")
    async def demo_unparseable(client_id: str) -> DemoResult:
        """A tool whose vendor sent a shape we cannot read. Used only by the loop tests."""
        return DemoResult.model_validate({"device_count": "not a number"})

    @tool(ConnectorName.NINJAONE, name="demo_slow")
    async def demo_slow(client_id: str) -> DemoResult:
        """A tool that never returns in time. Used only by the loop tests."""
        await asyncio.sleep(30)
        return DemoResult(device_count=0, devices=[])

    yield

    clear_registry(DEMO_TOOL_NAMES)


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    url = require_database(LIVE_URL)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield url
    command.downgrade(config, "base")


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[Any]:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_maker(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def _delete_client(maker: async_sessionmaker[AsyncSession], client_id: uuid.UUID) -> None:
    """Audit rows first (they RESTRICT), then the client; threads cascade from it."""
    async with maker() as session:
        await session.execute(AuditLog.__table__.delete().where(AuditLog.client_id == client_id))
        await session.execute(Client.__table__.delete().where(Client.id == client_id))
        await session.commit()


@pytest.fixture
async def db_client(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Client]:
    """A committed client, with `session_scope` pointed at the test database.

    The loop opens its own sessions, so it needs the real factory redirected
    rather than a session handed in.
    """
    import app.agent.loop as loop_module
    import app.connectors.factory as factory_module

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(loop_module, "session_scope", scope)
    monkeypatch.setattr(factory_module, "session_scope", scope)

    async with session_maker() as session:
        client = Client(slug="loop-test-client", name="Loop Test Client")
        session.add(client)
        await session.commit()
        await session.refresh(client)

    yield client

    await _delete_client(session_maker, client.id)


@pytest.fixture
async def make_client(
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Callable[[str, str], Awaitable[Client]]]:
    """Create further clients, for tests about what must not cross between them."""
    created: list[uuid.UUID] = []

    async def _make(slug: str, name: str) -> Client:
        async with session_maker() as session:
            client = Client(slug=slug, name=name)
            session.add(client)
            await session.commit()
            await session.refresh(client)
        created.append(client.id)
        return client

    yield _make

    for client_id in created:
        await _delete_client(session_maker, client_id)


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={
            "anthropic_model": "claude-opus-5",
            "max_tool_calls": 4,
            "tool_timeout_seconds": 5.0,
            "agent_timeout_seconds": 30.0,
        }
    )


async def audit_rows(engine: Any, client_id: uuid.UUID) -> list[AuditLog]:
    async with AsyncSession(bind=engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.client_id == client_id).order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )


@pytest.fixture
async def db_user(
    session_maker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[User]:
    """A committed Inputiv engineer, for tests that need a real actor.

    `conversations.owner_user_id` and `audit_log.actor_user_id` are foreign
    keys, so a test that signs in has to sign in as somebody who exists. The
    teardown removes the rows that point at the account before the account,
    since both keys are ON DELETE RESTRICT by design.
    """
    async with session_maker() as session:
        organisation = Organisation(
            kind=OrganisationKind.INPUTIV, slug="inputiv-test", name="Inputiv (test)"
        )
        session.add(organisation)
        await session.flush()
        user = User(
            organisation_id=organisation.id,
            organisation_kind=OrganisationKind.INPUTIV,
            email="engineer-1@inputiv.test",
            role=Role.INPUTIV_ENGINEER,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        await session.refresh(organisation)
    user.organisation = organisation

    yield user

    async with session_maker() as session:
        await session.execute(AuditLog.__table__.delete().where(AuditLog.actor_user_id == user.id))
        await session.execute(
            Conversation.__table__.update()
            .where(Conversation.owner_user_id == user.id)
            .values(owner_user_id=None)
        )
        await session.execute(Session.__table__.delete().where(Session.user_id == user.id))
        await session.execute(User.__table__.delete().where(User.id == user.id))
        await session.execute(
            Organisation.__table__.delete().where(Organisation.id == organisation.id)
        )
        await session.commit()

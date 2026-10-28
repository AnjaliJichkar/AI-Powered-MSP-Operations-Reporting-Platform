"""Shared helpers for connector tests.

Connector tests never touch the network or a database: `respx` answers the HTTP
calls from scrubbed fixtures, and a stub factory stands in for the credential
resolution that `test_factory_postgres.py` covers against a real database.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.connectors.autotask.client import AutotaskClient
from app.connectors.base import BaseConnector, ConnectorLimits
from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.connectsecure.client import ConnectSecureClient
from app.connectors.factory import ConnectorFactory, set_factory
from app.connectors.ninjaone.client import NinjaOneClient
from app.connectors.ninjaone.inventory import InventoryCache
from app.connectors.ninjaone.tokens import TokenStore
from app.connectors.scope import ClientScope, SiteRef
from app.models.enums import ConnectorName
from tests.database import require_database

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Set to run the `postgres`-marked tests; they are skipped otherwise.
LIVE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """A database at `alembic upgrade head`, torn back down after the module."""
    url = require_database(LIVE_URL)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield url
    command.downgrade(config, "base")


@pytest.fixture
async def session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """One transaction per test, rolled back at the end so tests never see each other."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as db_session:
            yield db_session
        await transaction.rollback()
    await engine.dispose()


#: The clock the NinjaOne fixtures were generated against.
FROZEN_NOW = dt.datetime(2026, 9, 7, 12, 0, 0, tzinfo=dt.UTC)

NINJA_BASE_URL = "https://app.ninjarmm.com"
NINJA_ORGANIZATION_ID = 42

#: Not a real credential: the fixtures are answered by respx, not NinjaOne.
NINJA_TEST_SECRETS = {"client_id": "test-client-id", "client_secret": "test-client-secret"}

#: The clock the Autotask fixtures were generated against.
AUTOTASK_FROZEN_NOW = dt.datetime(2026, 9, 16, 12, 0, 0, tzinfo=dt.UTC)

AUTOTASK_ZONE_LOOKUP_URL = "https://webservices.autotask.net/atservicesrest/v1.0/zoneInformation"
AUTOTASK_ZONE_URL = "https://webservices2.autotask.net/ATServicesRest"
AUTOTASK_COMPANY_ID = 77

#: Not a real credential either.
AUTOTASK_TEST_SECRETS = {
    "api_integration_code": "test-integration-code",
    "username": "api-user@example.invalid",
    "secret": "test-secret",
}


def load_fixture(connector: str, name: str) -> Any:
    """Read one scrubbed fixture."""
    path = FIXTURE_ROOT / connector / name
    return json.loads(path.read_text(encoding="utf-8"))


#: Requests one tenant-wide NinjaOne device listing makes when it fits in a
#: page: the page itself, then the empty page that proves it was the last.
REQUESTS_PER_LISTING = 2


def single_page(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    """A cursor-paged endpoint with exactly one page of data.

    The payload answers the first request; any request carrying `after` gets
    an empty page, as the vendor would. A `return_value` mock would resend the
    same page for every cursor, which is a broken vendor, not a small one.
    """

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after"):
            return httpx.Response(200, json=[])
        return httpx.Response(status, json=payload)

    return responder


@pytest.fixture
def fixture() -> Any:
    return load_fixture


@pytest.fixture
def fast_limits() -> ConnectorLimits:
    """Real retry behaviour, no jitter, so delays are assertable."""
    return ConnectorLimits(
        timeout_seconds=5.0,
        connect_timeout_seconds=2.0,
        max_retries=2,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.05,
        page_size=100,
        max_pages=5,
        jitter=False,
    )


class RecordingSleep:
    """Stands in for `asyncio.sleep` and records what the retry loop asked for."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def sleeper() -> RecordingSleep:
    return RecordingSleep()


#: The client every connector test belongs to.
TEST_CLIENT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
TEST_CLIENT_SLUG = "teamlogic-austin"


def site_ref(external_id: str | int, *, slug: str, name: str | None = None) -> SiteRef:
    """One end customer, as the factory would hand it to a connector."""
    return SiteRef(
        site_id=uuid.uuid5(TEST_CLIENT_ID, slug),
        slug=slug,
        name=name or slug.replace("-", " ").title(),
        external_id=str(external_id),
    )


def make_scope(
    connector: ConnectorName,
    allowed: Sequence[SiteRef],
    targets: Sequence[SiteRef] | None = None,
) -> ClientScope:
    """A scope whose allowlist and targets can differ, as in production."""
    return ClientScope(
        client_id=TEST_CLIENT_ID,
        client_slug=TEST_CLIENT_SLUG,
        connector=connector,
        allowed=tuple(allowed),
        targets=tuple(allowed if targets is None else targets),
    )


#: The single site most NinjaOne tests use.
NINJA_SITE = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental", name="Acme Dental")

#: The single site most Autotask tests use.
AUTOTASK_SITE = site_ref(AUTOTASK_COMPANY_ID, slug="acme-dental", name="Acme Dental")


def make_ninja_client(
    *,
    limits: ConnectorLimits,
    sleep: RecordingSleep | None = None,
    secrets: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    sites: Sequence[SiteRef] | None = None,
    targets: Sequence[SiteRef] | None = None,
    excluded: Sequence[SiteRef] = (),
    **options: Any,
) -> NinjaOneClient:
    """Build a NinjaOne client directly, as the factory would.

    `options` are the NinjaOne-specific knobs (`concurrency`, `inventory`,
    `inventory_ttl_seconds`, `devices_page_size`); each test that does not
    pass an `inventory` gets a private cache, so tests never share one; the
    same goes for `token_store`.
    """
    allowed = list(sites) if sites is not None else [NINJA_SITE]
    scope = make_scope(ConnectorName.NINJAONE, allowed, targets)
    if excluded:
        scope = ClientScope(
            client_id=scope.client_id,
            client_slug=scope.client_slug,
            connector=scope.connector,
            allowed=scope.allowed,
            targets=scope.targets,
            excluded=tuple(excluded),
        )
    options.setdefault("inventory", InventoryCache())
    options.setdefault("token_store", TokenStore())
    return NinjaOneClient(
        scope=scope,
        secrets=dict(secrets or NINJA_TEST_SECRETS),
        config=dict(config or {}),
        limits=limits,
        sleep=sleep,
        **options,
    )


def make_autotask_client(
    *,
    limits: ConnectorLimits,
    sleep: RecordingSleep | None = None,
    secrets: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    sites: Sequence[SiteRef] | None = None,
    targets: Sequence[SiteRef] | None = None,
) -> AutotaskClient:
    """Build an Autotask client directly, as the factory would.

    No `base_url` by default, so the zone lookup runs like it does in
    production; pass one to skip it.
    """
    allowed = list(sites) if sites is not None else [AUTOTASK_SITE]
    return AutotaskClient(
        scope=make_scope(ConnectorName.AUTOTASK, allowed, targets),
        secrets=dict(secrets or AUTOTASK_TEST_SECRETS),
        config=dict(config or {}),
        limits=limits,
        sleep=sleep,
    )


class StubFactory(ConnectorFactory):
    """Hands out a pre-built connector instead of reading the database."""

    def __init__(self, connector: BaseConnector) -> None:
        self.connector = connector
        self.calls: list[tuple[str, ConnectorName]] = []
        self.site_calls: list[str | None] = []

    @asynccontextmanager
    async def open(
        self,
        client_id: Any,
        connector_name: ConnectorName,
        site_id: Any = None,
    ) -> AsyncIterator[BaseConnector]:
        self.calls.append((str(client_id), connector_name))
        self.site_calls.append(None if site_id is None else str(site_id))
        async with self.connector:
            yield self.connector


@pytest.fixture
def stub_factory() -> Iterator[Any]:
    """Install a stub factory for the duration of a test."""
    installed: list[StubFactory] = []
    previous = None

    def _install(connector: BaseConnector) -> StubFactory:
        nonlocal previous
        factory = StubFactory(connector)
        previous = set_factory(factory)
        installed.append(factory)
        return factory

    yield _install

    if installed and previous is not None:
        set_factory(previous)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> dt.datetime:
    """Freeze the clock the NinjaOne tools measure staleness against."""
    from app.connectors.ninjaone import tools

    monkeypatch.setattr(tools, "_utcnow", lambda: FROZEN_NOW)
    return FROZEN_NOW


@pytest.fixture
def autotask_clock(monkeypatch: pytest.MonkeyPatch) -> dt.datetime:
    """Freeze the clock the Autotask tools measure ages and windows against."""
    from app.connectors.autotask import tools

    monkeypatch.setattr(tools, "_utcnow", lambda: AUTOTASK_FROZEN_NOW)
    return AUTOTASK_FROZEN_NOW


# --- Bitdefender ------------------------------------------------------------------------

BITDEFENDER_BASE_URL = "https://cloud.gravityzone.bitdefender.com/api"
BITDEFENDER_PARTNER_ID = "64a0000000000000000000aa"
BITDEFENDER_COMPANY_ID = "64a0000000000000000000b1"
BITDEFENDER_OTHER_COMPANY_ID = "64a0000000000000000000b2"
BITDEFENDER_INACTIVE_COMPANY_ID = "64a0000000000000000000b3"

#: Not a real credential either; the fixtures are answered by respx.
BITDEFENDER_TEST_SECRETS = {"api_key": "test-api-key"}

#: The single site most Bitdefender tests use, plus the sibling and the inactive one.
BITDEFENDER_SITE = site_ref(BITDEFENDER_COMPANY_ID, slug="acme-dental", name="Acme Dental")
BITDEFENDER_OTHER_SITE = site_ref(
    BITDEFENDER_OTHER_COMPANY_ID, slug="beacon-legal", name="Beacon Legal LLP"
)
BITDEFENDER_INACTIVE_SITE = site_ref(
    BITDEFENDER_INACTIVE_COMPANY_ID, slug="former-customer", name="Former Customer Co"
)


def make_bitdefender_client(
    *,
    limits: ConnectorLimits,
    sleep: RecordingSleep | None = None,
    secrets: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    sites: Sequence[SiteRef] | None = None,
    targets: Sequence[SiteRef] | None = None,
    excluded: Sequence[SiteRef] = (),
    **options: Any,
) -> BitdefenderClient:
    """Build a Bitdefender client directly, as the factory would. Private cache per test."""
    from app.connectors.inventory import InventoryCache as GenericInventoryCache

    allowed = list(sites) if sites is not None else [BITDEFENDER_SITE]
    scope = make_scope(ConnectorName.BITDEFENDER, allowed, targets)
    if excluded:
        scope = ClientScope(
            client_id=scope.client_id,
            client_slug=scope.client_slug,
            connector=scope.connector,
            allowed=scope.allowed,
            targets=scope.targets,
            excluded=tuple(excluded),
        )
    options.setdefault("inventory", GenericInventoryCache())
    return BitdefenderClient(
        scope=scope,
        secrets=dict(secrets or BITDEFENDER_TEST_SECRETS),
        config=dict(config or {}),
        limits=limits,
        sleep=sleep,
        **options,
    )


# --- ConnectSecure ----------------------------------------------------------------------

CONNECTSECURE_BASE_URL = "https://pod401.myconnectsecure.com"
CONNECTSECURE_COMPANY_ID = "501"
CONNECTSECURE_OTHER_COMPANY_ID = "502"
CONNECTSECURE_INACTIVE_COMPANY_ID = "503"

#: Not a real credential; the fixtures are answered by respx.
CONNECTSECURE_TEST_SECRETS = {"client_id": "test-client-id", "client_secret": "test-client-secret"}
CONNECTSECURE_TEST_CONFIG = {"tenant": "example-msp", "pod": "401"}

CONNECTSECURE_SITE = site_ref(CONNECTSECURE_COMPANY_ID, slug="acme-dental", name="Acme Dental")
CONNECTSECURE_OTHER_SITE = site_ref(
    CONNECTSECURE_OTHER_COMPANY_ID, slug="beacon-legal", name="Beacon Legal LLP"
)
CONNECTSECURE_INACTIVE_SITE = site_ref(
    CONNECTSECURE_INACTIVE_COMPANY_ID, slug="former-customer", name="Former Customer Co"
)


def make_connectsecure_client(
    *,
    limits: ConnectorLimits,
    sleep: RecordingSleep | None = None,
    secrets: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    sites: Sequence[SiteRef] | None = None,
    targets: Sequence[SiteRef] | None = None,
    excluded: Sequence[SiteRef] = (),
    **options: Any,
) -> ConnectSecureClient:
    """Build a ConnectSecure client directly, as the factory would. Private caches per test."""
    from app.connectors.inventory import InventoryCache as GenericInventoryCache

    allowed = list(sites) if sites is not None else [CONNECTSECURE_SITE]
    scope = make_scope(ConnectorName.CONNECTSECURE, allowed, targets)
    if excluded:
        scope = ClientScope(
            client_id=scope.client_id,
            client_slug=scope.client_slug,
            connector=scope.connector,
            allowed=scope.allowed,
            targets=scope.targets,
            excluded=tuple(excluded),
        )
    options.setdefault("inventory", GenericInventoryCache())
    options.setdefault("token_store", TokenStore())
    return ConnectSecureClient(
        scope=scope,
        secrets=dict(secrets or CONNECTSECURE_TEST_SECRETS),
        config=dict(config or CONNECTSECURE_TEST_CONFIG),
        limits=limits,
        sleep=sleep,
        **options,
    )

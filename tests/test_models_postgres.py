"""The hard rules, verified against a real PostgreSQL database.

These exercise the constraints themselves rather than their declarations:
`tests/test_models.py` asserts the schema says the right thing, this asserts
Postgres actually enforces it. Skipped unless ``TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.audit.recorder import AuditContext, record_event
from app.models import AuditLog, Client, ConnectorName, Site, ToolCredential, ToolMapping
from app.models.enums import AuditEventType, AuditOutcome, Interface
from app.security.crypto import CredentialCipher
from tests.database import require_database

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIVE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set"),
]


@pytest.fixture(scope="module")
def migrated_database() -> Iterator[str]:
    """Apply the migration to a scratch database for the whole module."""
    url = require_database(LIVE_URL)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield LIVE_URL
    command.downgrade(config, "base")


@pytest.fixture
async def session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as db_session:
            yield db_session
        await transaction.rollback()
    await engine.dispose()


async def _make_site(
    session: AsyncSession,
    client: Client,
    *,
    slug: str = "acme-dental",
    name: str = "Acme Dental",
) -> Site:
    """One end customer of a franchisee."""
    site = Site(client_id=client.id, slug=slug, name=name)
    session.add(site)
    await session.flush()
    return site


async def _make_client(session: AsyncSession, slug: str = "teamlogic-austin") -> Client:
    client = Client(slug=slug, name=slug.replace("-", " ").title())
    session.add(client)
    await session.flush()
    return client


# --- hard rule 1: per-client scoping --------------------------------------


async def test_two_clients_cannot_claim_the_same_vendor_organisation(
    session: AsyncSession,
) -> None:
    """The unique constraint that stops one franchisee's data reaching another."""
    first = await _make_client(session, "teamlogic-austin")
    second = await _make_client(session, "teamlogic-dallas")
    first_site = await _make_site(session, first)
    second_site = await _make_site(session, second)

    session.add(
        ToolMapping(
            client_id=first.id,
            site_id=first_site.id,
            connector_name=ConnectorName.NINJAONE,
            external_id="org-42",
        )
    )
    await session.flush()

    session.add(
        ToolMapping(
            client_id=second.id,
            site_id=second_site.id,
            connector_name=ConnectorName.NINJAONE,
            external_id="org-42",
        )
    )
    with pytest.raises(IntegrityError, match="uq_tool_mappings_connector_name_external_id"):
        await session.flush()
    await session.rollback()


async def test_a_tenant_level_mapping_carries_configuration_not_a_vendor_id(
    session: AsyncSession,
) -> None:
    """A vendor id names one end customer, so it belongs on a site-level row."""
    client = await _make_client(session)
    session.add(
        ToolMapping(
            client_id=client.id,
            site_id=None,
            connector_name=ConnectorName.NINJAONE,
            extra={"sync": {"internal_prefixes": ["zz"]}},
        )
    )
    await session.flush()  # fine: no vendor id

    session.add(
        ToolMapping(
            client_id=client.id,
            site_id=None,
            connector_name=ConnectorName.AUTOTASK,
            external_id="42",
        )
    )
    with pytest.raises(IntegrityError, match="ck_tool_mappings_tenant_level_has_no_external_id"):
        await session.flush()
    await session.rollback()


async def test_a_site_may_hold_two_organisations_on_one_connector(session: AsyncSession) -> None:
    """A customer with two GravityZone companies holds both; each organisation is
    still one site's (the `(connector, external_id)` rule)."""
    client = await _make_client(session)
    site = await _make_site(session, client)
    for external_id in ("org-1", "org-2"):
        session.add(
            ToolMapping(
                client_id=client.id,
                site_id=site.id,
                connector_name=ConnectorName.NINJAONE,
                external_id=external_id,
            )
        )
    await session.flush()
    other = await _make_site(session, client, slug="other", name="Other")
    session.add(
        ToolMapping(
            client_id=client.id,
            site_id=other.id,
            connector_name=ConnectorName.NINJAONE,
            external_id="org-1",
        )
    )
    with pytest.raises(IntegrityError, match="uq_tool_mappings_connector_name_external_id"):
        await session.flush()
    await session.rollback()


async def test_two_sites_of_one_client_may_share_a_connector(session: AsyncSession) -> None:
    """The pair that used to be unique is now deliberately repeated per site."""
    client = await _make_client(session)
    dental = await _make_site(session, client, slug="acme-dental", name="Acme Dental")
    legal = await _make_site(session, client, slug="bright-legal", name="Bright Legal")

    session.add_all(
        [
            ToolMapping(
                client_id=client.id,
                site_id=dental.id,
                connector_name=ConnectorName.NINJAONE,
                external_id="org-1",
            ),
            ToolMapping(
                client_id=client.id,
                site_id=legal.id,
                connector_name=ConnectorName.NINJAONE,
                external_id="org-2",
            ),
        ]
    )
    await session.flush()  # no error: one franchisee, two customers, one vendor


async def test_only_one_tenant_level_mapping_per_client_and_connector(
    session: AsyncSession,
) -> None:
    """The partial unique index, which the site-level rows are exempt from."""
    client = await _make_client(session)
    for _ in range(2):
        session.add(
            ToolMapping(client_id=client.id, site_id=None, connector_name=ConnectorName.NINJAONE)
        )
    with pytest.raises(IntegrityError, match="uq_tool_mappings_tenant_level"):
        await session.flush()
    await session.rollback()


async def test_a_site_slug_is_unique_only_within_its_client(session: AsyncSession) -> None:
    """Two franchisees may both have an "acme" customer; neither blocks the other."""
    first = await _make_client(session, "teamlogic-austin")
    second = await _make_client(session, "teamlogic-dallas")

    await _make_site(session, first, slug="acme", name="Acme")
    await _make_site(session, second, slug="acme", name="Acme Manufacturing")
    await session.flush()  # no error

    session.add(Site(client_id=first.id, slug="acme", name="Acme Again"))
    with pytest.raises(IntegrityError, match="uq_sites_client_id_slug"):
        await session.flush()
    await session.rollback()


async def test_deleting_a_client_takes_its_sites(session: AsyncSession) -> None:
    client = await _make_client(session)
    await _make_site(session, client)
    await session.flush()

    await session.execute(text("DELETE FROM clients WHERE id = :id"), {"id": client.id})

    assert (await session.execute(text("SELECT count(*) FROM sites"))).scalar_one() == 0


async def test_one_credential_per_client_and_connector(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await _make_client(session)
    for _ in range(2):
        credential = ToolCredential(client_id=client.id, connector_name=ConnectorName.AUTOTASK)
        credential.set_secrets({"api_key": "k"}, cipher=cipher)
        session.add(credential)

    with pytest.raises(IntegrityError, match="uq_tool_credentials_client_id_connector_name"):
        await session.flush()
    await session.rollback()


async def test_an_unknown_connector_cannot_be_stored(session: AsyncSession) -> None:
    """The check constraint keeps typos and product-specific integrations out."""
    client = await _make_client(session)
    with pytest.raises(IntegrityError, match="ck_tool_mappings_connector_name"):
        await session.execute(
            text(
                "INSERT INTO tool_mappings (client_id, connector_name, external_id) "
                "VALUES (:client_id, 'connectwise', 'org-1')"
            ),
            {"client_id": client.id},
        )
    await session.rollback()


@pytest.mark.parametrize("slug", ["Teamlogic-Austin", "team logic", "-leading-dash", "x"])
async def test_malformed_slugs_are_rejected(session: AsyncSession, slug: str) -> None:
    session.add(Client(slug=slug, name="whatever"))
    with pytest.raises(IntegrityError, match="ck_clients_slug_format"):
        await session.flush()
    await session.rollback()


# --- hard rule 3: secrets at rest -----------------------------------------


async def test_credential_secrets_are_ciphertext_in_the_database(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await _make_client(session)
    credential = ToolCredential(client_id=client.id, connector_name=ConnectorName.NINJAONE)
    credential.set_secrets(
        {"client_id": "ninja-app", "client_secret": "sup3r-s3cret"}, cipher=cipher
    )
    session.add(credential)
    await session.flush()

    stored = (
        await session.execute(text("SELECT secret_ciphertext FROM tool_credentials"))
    ).scalar_one()
    assert b"sup3r-s3cret" not in stored
    assert b"client_secret" not in stored

    session.expire_all()
    reloaded = (await session.execute(select(ToolCredential))).scalar_one()
    assert reloaded.get_secrets(cipher=cipher)["client_secret"] == "sup3r-s3cret"


# --- hard rule 4: the audit trail survives --------------------------------


async def test_a_client_with_audit_history_cannot_be_deleted(session: AsyncSession) -> None:
    client = await _make_client(session)
    context = AuditContext.new(
        actor_id="U123", interface=Interface.SLACK, client_id=client.id, client_ref=client.slug
    )
    await record_event(
        session,
        context,
        event_type=AuditEventType.PROMPT,
        outcome=AuditOutcome.SUCCESS,
        payload={"message": "which devices have not checked in?"},
    )

    await session.delete(client)
    with pytest.raises(IntegrityError, match="fk_audit_log_client_id"):
        await session.flush()
    await session.rollback()


async def test_credentials_and_mappings_go_when_a_client_does(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await _make_client(session)
    credential = ToolCredential(client_id=client.id, connector_name=ConnectorName.COVE)
    credential.set_secrets({"api_key": "k"}, cipher=cipher)
    session.add(credential)
    site = await _make_site(session, client)
    session.add(
        ToolMapping(
            client_id=client.id,
            site_id=site.id,
            connector_name=ConnectorName.COVE,
            external_id="c-1",
        )
    )
    await session.flush()

    await session.execute(text("DELETE FROM clients WHERE id = :id"), {"id": client.id})

    assert (await session.execute(text("SELECT count(*) FROM tool_credentials"))).scalar_one() == 0
    assert (await session.execute(text("SELECT count(*) FROM tool_mappings"))).scalar_one() == 0


async def test_a_request_with_no_resolvable_client_is_still_audited(
    session: AsyncSession,
) -> None:
    """A rejected request must leave a trace, including what was asked for."""
    context = AuditContext.new(
        actor_id="U999", interface=Interface.SLACK, client_ref="teamlogic-nowhere"
    )
    await record_event(
        session,
        context,
        event_type=AuditEventType.ERROR,
        outcome=AuditOutcome.DENIED,
        error_type="UnknownClient",
        error_message="no client matches 'teamlogic-nowhere'",
    )
    await session.flush()

    entry = (await session.execute(select(AuditLog))).scalar_one()
    assert entry.client_id is None
    assert entry.client_ref == "teamlogic-nowhere"
    assert entry.outcome == AuditOutcome.DENIED
    assert entry.created_at is not None


async def test_audit_payloads_are_redacted_on_the_way_in(session: AsyncSession) -> None:
    client = await _make_client(session)
    context = AuditContext.new(
        actor_id="U123", interface=Interface.API, client_id=client.id, client_ref=client.slug
    )
    await record_event(
        session,
        context,
        event_type=AuditEventType.TOOL_CALL,
        outcome=AuditOutcome.SUCCESS,
        connector_name=ConnectorName.NINJAONE,
        tool_name="list_devices_not_checked_in",
        payload={"days": 7, "headers": {"Authorization": "Bearer live-token"}},
    )
    await session.flush()

    stored = (await session.execute(text("SELECT payload::text FROM audit_log"))).scalar_one()
    assert "live-token" not in stored
    assert '"days": 7' in stored or '"days":7' in stored


async def test_audit_rows_of_one_run_are_ordered_by_id(session: AsyncSession) -> None:
    client = await _make_client(session)
    context = AuditContext.new(
        actor_id="U123", interface=Interface.SLACK, client_id=client.id, client_ref=client.slug
    )
    for event in (AuditEventType.RUN_STARTED, AuditEventType.PROMPT, AuditEventType.RUN_FINISHED):
        await record_event(session, context, event_type=event, outcome=AuditOutcome.SUCCESS)
    await session.flush()

    rows = (
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.request_id == context.request_id)
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.event_type for row in rows] == [
        AuditEventType.RUN_STARTED,
        AuditEventType.PROMPT,
        AuditEventType.RUN_FINISHED,
    ]


# --- server-side defaults -------------------------------------------------


async def test_server_generates_the_client_id_and_timestamps(session: AsyncSession) -> None:
    client = Client(slug="teamlogic-boise", name="TeamLogic Boise")
    session.add(client)
    await session.flush()
    await session.refresh(client)

    assert isinstance(client.id, uuid.UUID)
    assert client.is_active is True
    assert client.created_at.tzinfo is not None
    assert client.updated_at is not None


async def test_identity_columns_are_generated_by_the_database(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await _make_client(session)
    credential = ToolCredential(client_id=client.id, connector_name=ConnectorName.BITDEFENDER)
    credential.set_secrets({"api_key": "k"}, cipher=cipher)
    session.add(credential)
    await session.flush()

    assert credential.id is not None
    assert credential.config == {}

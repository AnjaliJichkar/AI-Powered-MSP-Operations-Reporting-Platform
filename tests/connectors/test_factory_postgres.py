"""Credential resolution, end to end, against a real database.

`client_id` -> Client -> ToolMapping -> decrypted ToolCredential -> connector.
Everything else stubs this chain out; this is the test that proves it works,
and that it refuses when any link is missing.

Skipped unless `TEST_DATABASE_URL` is set.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorLimits
from app.connectors.errors import ConnectorNotConfigured, UnknownClient
from app.connectors.factory import build_connector, resolve_client
from app.connectors.ninjaone.client import NinjaOneClient
from app.models import Client, ConnectorName, Site, ToolCredential, ToolMapping
from app.security.crypto import CredentialCipher
from tests.connectors.conftest import (
    LIVE_URL,
    NINJA_BASE_URL,
    NINJA_ORGANIZATION_ID,
    NINJA_TEST_SECRETS,
    load_fixture,
    single_page,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set"),
]

TOKEN_URL = f"{NINJA_BASE_URL}/ws/oauth/token"
#: An unscoped call reads the tenant listing.
DEVICES_URL = f"{NINJA_BASE_URL}/v2/devices"


async def seed_client(
    session: AsyncSession,
    cipher: CredentialCipher,
    *,
    slug: str = "teamlogic-austin",
    with_mapping: bool = True,
    with_credential: bool = True,
    credential_active: bool = True,
    client_active: bool = True,
    external_id: str = str(NINJA_ORGANIZATION_ID),
    site_slug: str = "acme-dental",
    site_active: bool = True,
    config: dict[str, object] | None = None,
) -> Client:
    client = Client(slug=slug, name="TeamLogic Austin", is_active=client_active)
    session.add(client)
    await session.flush()

    if with_mapping:
        # A mapping only scopes a read when it belongs to a site, so the seed
        # creates the end customer too.
        site = Site(client_id=client.id, slug=site_slug, name="Acme Dental", is_active=site_active)
        session.add(site)
        await session.flush()
        session.add(
            ToolMapping(
                client_id=client.id,
                site_id=site.id,
                connector_name=ConnectorName.NINJAONE,
                external_id=external_id,
                external_name="Acme Dental",
            )
        )
    if with_credential:
        credential = ToolCredential(
            client_id=client.id,
            connector_name=ConnectorName.NINJAONE,
            config=config or {},
            is_active=credential_active,
        )
        credential.set_secrets(dict(NINJA_TEST_SECRETS), cipher=cipher)
        session.add(credential)
    await session.flush()
    return client


# --- the happy path -------------------------------------------------------


async def test_the_factory_builds_a_connector_from_a_client_id(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await seed_client(session, cipher)

    connector = await build_connector(
        session, str(client.id), ConnectorName.NINJAONE, cipher=cipher
    )

    assert isinstance(connector, NinjaOneClient)
    assert connector.scope.target_external_ids == [str(NINJA_ORGANIZATION_ID)]
    assert connector.scope.allowed_external_ids == {str(NINJA_ORGANIZATION_ID)}
    assert connector.scope.targets[0].slug == "acme-dental"
    # The credential was decrypted on the way through.
    assert connector.secret("client_id") == NINJA_TEST_SECRETS["client_id"]


async def test_per_client_config_reaches_the_connector(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await seed_client(session, cipher, config={"region": "eu"})

    connector = await build_connector(
        session, str(client.id), ConnectorName.NINJAONE, cipher=cipher
    )

    assert connector.base_url == "https://eu.ninjarmm.com"


@respx.mock
async def test_a_connector_resolved_from_the_database_can_make_a_call(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """The whole chain: database row, decryption, OAuth, a scoped read."""
    client = await seed_client(session, cipher)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )
    respx.get(DEVICES_URL).mock(side_effect=single_page(load_fixture("ninjaone", "devices.json")))

    connector = await build_connector(
        session,
        str(client.id),
        ConnectorName.NINJAONE,
        cipher=cipher,
        limits=ConnectorLimits(jitter=False),
    )
    async with connector:
        assert isinstance(connector, NinjaOneClient)
        devices, truncated = await connector.list_devices()

    assert len(devices) == 5
    assert truncated is False


async def test_two_clients_resolve_to_their_own_organisations(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """Hard rule 1: resolution is per client, with no shared state between them."""
    austin = await seed_client(
        session, cipher, slug="teamlogic-austin", external_id="42", site_slug="austin-dental"
    )
    dallas = await seed_client(
        session, cipher, slug="teamlogic-dallas", external_id="77", site_slug="dallas-legal"
    )

    first = await build_connector(session, str(austin.id), ConnectorName.NINJAONE, cipher=cipher)
    second = await build_connector(session, str(dallas.id), ConnectorName.NINJAONE, cipher=cipher)

    assert first.scope.target_external_ids == ["42"]
    assert second.scope.target_external_ids == ["77"]
    # Neither client's allowlist contains the other's organisation.
    assert first.scope.allowed_external_ids.isdisjoint(second.scope.allowed_external_ids)


# --- every way it can refuse ----------------------------------------------


async def test_an_unknown_client_id_is_refused(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    with pytest.raises(UnknownClient, match="no client matches"):
        await build_connector(session, str(uuid.uuid4()), ConnectorName.NINJAONE, cipher=cipher)


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "'; drop table clients; --"])
async def test_a_malformed_client_id_is_refused_before_any_query(
    session: AsyncSession, cipher: CredentialCipher, bad: str
) -> None:
    with pytest.raises(UnknownClient, match="not a valid client id"):
        await build_connector(session, bad, ConnectorName.NINJAONE, cipher=cipher)


async def test_an_inactive_client_is_refused(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await seed_client(session, cipher, client_active=False)

    with pytest.raises(UnknownClient, match="not active"):
        await build_connector(session, str(client.id), ConnectorName.NINJAONE, cipher=cipher)


async def test_a_client_with_no_mapping_is_refused(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await seed_client(session, cipher, with_mapping=False)

    with pytest.raises(ConnectorNotConfigured, match="no ninjaone sites mapped"):
        await build_connector(session, str(client.id), ConnectorName.NINJAONE, cipher=cipher)


async def test_a_client_with_no_credentials_is_refused(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await seed_client(session, cipher, with_credential=False)

    with pytest.raises(ConnectorNotConfigured, match="no ninjaone credentials stored"):
        await build_connector(session, str(client.id), ConnectorName.NINJAONE, cipher=cipher)


async def test_disabled_credentials_are_refused(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await seed_client(session, cipher, credential_active=False)

    with pytest.raises(ConnectorNotConfigured, match="disabled"):
        await build_connector(session, str(client.id), ConnectorName.NINJAONE, cipher=cipher)


async def test_an_unimplemented_connector_is_refused(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """Blackpoint is after the freeze; asking for it now must fail rather than
    half-work. Cove stood here until it was built, and the connector that
    stands here is whichever has no class in `CONNECTOR_CLASSES` yet.

    Bitdefender is implemented but this client holds no key for it: that is a
    different, equally clear refusal, before any HTTP.
    """
    client = await seed_client(session, cipher)

    with pytest.raises(ConnectorNotConfigured, match="no connector is implemented"):
        await build_connector(session, str(client.id), ConnectorName.BLACKPOINT, cipher=cipher)
    with pytest.raises(ConnectorNotConfigured, match="no bitdefender credentials stored"):
        await build_connector(session, str(client.id), ConnectorName.BITDEFENDER, cipher=cipher)


async def test_resolution_errors_never_carry_a_credential(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await seed_client(session, cipher, credential_active=False)

    with pytest.raises(ConnectorNotConfigured) as caught:
        await build_connector(session, str(client.id), ConnectorName.NINJAONE, cipher=cipher)

    rendered = str(caught.value.as_dict())
    assert NINJA_TEST_SECRETS["client_secret"] not in rendered


async def test_resolve_client_returns_the_row(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    client = await seed_client(session, cipher)
    assert (await resolve_client(session, client.id)).slug == "teamlogic-austin"


# --- cross-client site rejection ------------------------------------------


async def test_a_site_of_another_client_is_refused(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """Hard rule 1 at the site level: one franchisee cannot name another's customer."""
    from app.connectors.errors import UnknownSite
    from app.connectors.factory import resolve_site

    austin = await seed_client(
        session, cipher, slug="teamlogic-austin", external_id="42", site_slug="austin-dental"
    )
    await seed_client(
        session, cipher, slug="teamlogic-dallas", external_id="77", site_slug="dallas-legal"
    )

    with pytest.raises(UnknownSite, match="is not a site of"):
        await resolve_site(session, austin, "dallas-legal")


async def test_building_a_connector_for_another_clients_site_is_refused(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """And it is refused before the credential is even loaded."""
    from sqlalchemy import select as _select

    from app.connectors.errors import UnknownSite

    austin = await seed_client(
        session, cipher, slug="teamlogic-austin", external_id="42", site_slug="austin-dental"
    )
    await seed_client(
        session, cipher, slug="teamlogic-dallas", external_id="77", site_slug="dallas-legal"
    )
    foreign_site_id = (
        await session.execute(_select(Site.id).where(Site.slug == "dallas-legal"))
    ).scalar_one()

    with pytest.raises(UnknownSite):
        await build_connector(
            session,
            str(austin.id),
            ConnectorName.NINJAONE,
            site_id=str(foreign_site_id),
            cipher=cipher,
        )


async def test_an_inactive_site_cannot_be_targeted(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """A customer the vendor stopped listing is not silently queried."""
    from app.connectors.errors import UnknownSite
    from app.connectors.factory import resolve_site

    client = await seed_client(session, cipher, site_active=False)

    with pytest.raises(UnknownSite, match="no longer active"):
        await resolve_site(session, client, "acme-dental")


async def test_the_allowlist_only_contains_this_clients_sites(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    from app.connectors.factory import build_client_scope

    austin = await seed_client(
        session, cipher, slug="teamlogic-austin", external_id="42", site_slug="austin-dental"
    )
    await seed_client(
        session, cipher, slug="teamlogic-dallas", external_id="77", site_slug="dallas-legal"
    )

    scope = await build_client_scope(session, austin, ConnectorName.NINJAONE)

    assert scope.allowed_external_ids == {"42"}
    assert [site.slug for site in scope.allowed] == ["austin-dental"]

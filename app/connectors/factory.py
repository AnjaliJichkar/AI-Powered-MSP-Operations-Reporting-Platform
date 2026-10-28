"""The only place a connector is constructed.

Resolution is entirely server-side (hard rule 1):

    client_id -> Client (must exist and be active)
              -> Sites          (the client's active end customers)
              -> ToolMapping    (each site's vendor organisation id)
              -> ToolCredential (decrypted with CredentialCipher)
              -> connector instance carrying a ClientScope

The model supplies only `client_id`, and optionally a site *slug* the loop has
already resolved to a `site_id` within that client. It never sees, chooses or
passes a credential, and a call naming a site outside the current client fails
before any HTTP request.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.autotask.client import AutotaskClient
from app.connectors.base import BaseConnector, ConnectorLimits
from app.connectors.bitdefender.client import BitdefenderClient
from app.connectors.connectsecure.client import ConnectSecureClient
from app.connectors.cove.client import CoveClient
from app.connectors.errors import ConnectorNotConfigured, UnknownClient, UnknownSite
from app.connectors.microsoft365.client import Microsoft365Client
from app.connectors.ninjaone.client import NinjaOneClient
from app.connectors.scope import ClientScope, SiteRef
from app.db.session import session_scope
from app.models.client import Client
from app.models.enums import ConnectorName
from app.models.site import Site
from app.models.tool_credential import ToolCredential
from app.models.tool_mapping import ToolMapping
from app.security.crypto import CredentialCipher, get_cipher

#: Every connector the factory can build for a live client. Adding one means adding it here.
CONNECTOR_CLASSES: dict[ConnectorName, type[BaseConnector]] = {
    ConnectorName.NINJAONE: NinjaOneClient,
    ConnectorName.AUTOTASK: AutotaskClient,
    ConnectorName.BITDEFENDER: BitdefenderClient,
    ConnectorName.CONNECTSECURE: ConnectSecureClient,
    ConnectorName.MICROSOFT365: Microsoft365Client,
    ConnectorName.COVE: CoveClient,
}


class DemoClientRefused(ConnectorNotConfigured):
    """A demo client was asked to do something only a live one can: hold a credential,
    or reach a real vendor."""


def _parse_uuid(value: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def resolve_client(session: AsyncSession, client_id: str | uuid.UUID) -> Client:
    """Load an active client, or fail. Inactive clients are not usable."""
    parsed = _parse_uuid(client_id)
    if parsed is None:
        raise UnknownClient(
            f"{client_id!r} is not a valid client id",
            connector="factory",
            details={"client_ref": str(client_id)},
        )

    client = (await session.execute(select(Client).where(Client.id == parsed))).scalar_one_or_none()

    if client is None:
        raise UnknownClient(
            "no client matches that client_id",
            connector="factory",
            details={"client_ref": str(client_id)},
        )
    if not client.is_active:
        raise UnknownClient(
            f"client {client.slug!r} is not active",
            connector="factory",
            details={"client_ref": client.slug},
        )
    return client


async def list_client_sites(
    session: AsyncSession, client_id: uuid.UUID, *, active_only: bool = True
) -> list[Site]:
    """The client's sites, in name order. This is the allowlist's source."""
    query = select(Site).where(Site.client_id == client_id)
    if active_only:
        query = query.where(Site.is_active.is_(True))
    return list((await session.execute(query.order_by(Site.name))).scalars().all())


async def resolve_site(session: AsyncSession, client: Client, site_ref: str | uuid.UUID) -> Site:
    """Load one of this client's active sites, by id or slug.

    A site belonging to another client is refused exactly as an unknown one is.
    Confirming that a slug exists but belongs to someone else would leak another
    franchisee's customer list.
    """
    parsed = _parse_uuid(site_ref)
    query = select(Site).where(Site.client_id == client.id)
    query = query.where(Site.id == parsed) if parsed else query.where(Site.slug == str(site_ref))

    site = (await session.execute(query)).scalar_one_or_none()
    if site is None:
        raise UnknownSite(
            f"{str(site_ref)!r} is not a site of {client.slug!r}",
            connector="factory",
            details={"client_ref": client.slug, "site_ref": str(site_ref)},
        )
    if not site.is_active:
        raise UnknownSite(
            f"site {site.slug!r} is no longer active for {client.slug!r}",
            connector="factory",
            details={"client_ref": client.slug, "site_ref": site.slug},
        )
    return site


async def build_client_scope(
    session: AsyncSession,
    client: Client,
    connector_name: ConnectorName,
    *,
    site: Site | None = None,
) -> ClientScope:
    """The allowlist of vendor ids for this client, and this call's targets.

    Inactive sites are loaded too, as `excluded`: a tenant-wide listing returns
    their records, and the guard needs to know they are ours to drop them
    quietly rather than report a breach.
    """
    rows = (
        await session.execute(
            select(ToolMapping, Site)
            .join(Site, ToolMapping.site_id == Site.id)
            .where(
                ToolMapping.client_id == client.id,
                ToolMapping.connector_name == connector_name,
            )
            .order_by(Site.name)
        )
    ).all()

    def ref(mapping: ToolMapping, site_row: Site) -> SiteRef:
        assert mapping.external_id is not None
        return SiteRef(
            site_id=site_row.id,
            slug=site_row.slug,
            name=site_row.name,
            external_id=mapping.external_id,
        )

    # The check constraint only forbids a vendor id on tenant-level rows; a
    # site-level row is expected to carry one and cannot scope without it.
    allowed = tuple(
        ref(mapping, site_row)
        for mapping, site_row in rows
        if site_row.is_active and mapping.external_id is not None
    )
    excluded = tuple(
        ref(mapping, site_row)
        for mapping, site_row in rows
        if not site_row.is_active and mapping.external_id is not None
    )

    targets = allowed
    if site is not None:
        # Every organisation the site holds on this connector: a customer with
        # two GravityZone companies is read as the union of both.
        matching = [entry for entry in allowed if entry.site_id == site.id]
        if not matching:
            raise ConnectorNotConfigured(
                f"site {site.slug!r} has no {connector_name.value} organisation mapped; "
                "run sync_sites for this client",
                connector=connector_name.value,
                details={"client_ref": client.slug, "site_ref": site.slug},
            )
        targets = tuple(matching)

    return ClientScope(
        client_id=client.id,
        client_slug=client.slug,
        connector=connector_name,
        allowed=allowed,
        targets=targets,
        excluded=excluded,
    )


async def _load_credential(
    session: AsyncSession, client: Client, connector_name: ConnectorName
) -> ToolCredential:
    credential = (
        await session.execute(
            select(ToolCredential).where(
                ToolCredential.client_id == client.id,
                ToolCredential.connector_name == connector_name,
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise ConnectorNotConfigured(
            f"client {client.slug!r} has no {connector_name.value} credentials stored",
            connector=connector_name.value,
            details={"client_ref": client.slug},
        )
    if not credential.is_active:
        raise ConnectorNotConfigured(
            f"the {connector_name.value} credentials for {client.slug!r} are disabled",
            connector=connector_name.value,
            details={"client_ref": client.slug},
        )
    return credential


async def build_connector(
    session: AsyncSession,
    client_id: str | uuid.UUID,
    connector_name: ConnectorName,
    *,
    site_id: str | uuid.UUID | None = None,
    require_sites: bool = True,
    cipher: CredentialCipher | None = None,
    limits: ConnectorLimits | None = None,
    **connector_kwargs: Any,
) -> BaseConnector:
    """Build a connector for one client, optionally narrowed to one site.

    `require_sites=False` is for `sync_sites`, which runs before any site exists
    and needs only the tenant's credentials.

    The returned instance is not yet open: use it in an `async with`, or go
    through `connector_for`, which handles that.
    """
    client = await resolve_client(session, client_id)
    if client.is_demo:
        # The demo tenant never holds a credential and never reaches a vendor: its
        # connectors are the fakes in `app/demo/`, built from the generated dataset.
        return await _build_demo_connector(
            session, client, connector_name, site_id=site_id, limits=limits, **connector_kwargs
        )

    connector_class = CONNECTOR_CLASSES.get(connector_name)
    if connector_class is None:
        raise ConnectorNotConfigured(
            f"no connector is implemented for {connector_name.value}",
            connector=connector_name.value,
        )

    # The site is resolved before the credential and before any HTTP: a call
    # naming someone else's site must cost nothing (hard rule 1).
    site = await resolve_site(session, client, site_id) if site_id is not None else None
    credential = await _load_credential(session, client, connector_name)
    scope = await build_client_scope(session, client, connector_name, site=site)

    if require_sites and not scope.allowed:
        raise ConnectorNotConfigured(
            f"client {client.slug!r} has no {connector_name.value} sites mapped; run "
            f"`python scripts/sync_sites.py --client {client.slug} "
            f"--connector {connector_name.value}` first",
            connector=connector_name.value,
            details={"client_ref": client.slug},
        )

    secrets: Mapping[str, Any] = credential.get_secrets(cipher=cipher or get_cipher())

    return connector_class(
        scope=scope,
        secrets=secrets,
        config=credential.config,
        limits=limits or ConnectorLimits.from_settings(connector=connector_name.value),
        # Anything cached under the old credential misses once it is replaced.
        credential_version=f"{credential.encryption_key_id}:{credential.updated_at.isoformat()}",
        **connector_kwargs,
    )


async def _build_demo_connector(
    session: AsyncSession,
    client: Client,
    connector_name: ConnectorName,
    *,
    site_id: str | uuid.UUID | None,
    limits: ConnectorLimits | None,
    **connector_kwargs: Any,
) -> BaseConnector:
    """A fake connector for the demo client: same scope rules, no credential, a
    transport that answers from the generated dataset and can reach no host."""
    from app.demo.connectors import DEMO_CONNECTOR_CLASSES, demo_world

    demo_class = DEMO_CONNECTOR_CLASSES.get(connector_name)
    if demo_class is None:
        raise ConnectorNotConfigured(
            f"the demo tenant has no {connector_name.value} connector",
            connector=connector_name.value,
            details={"client_ref": client.slug},
        )
    site = await resolve_site(session, client, site_id) if site_id is not None else None
    scope = await build_client_scope(session, client, connector_name, site=site)
    return demo_class(
        scope=scope,
        world=demo_world(client.slug),
        limits=limits or ConnectorLimits.from_settings(connector=connector_name.value),
        **connector_kwargs,
    )


class ConnectorFactory:
    """Opens connectors against the database. Swap it out only in tests."""

    @asynccontextmanager
    async def open(
        self,
        client_id: str | uuid.UUID,
        connector_name: ConnectorName,
        site_id: str | uuid.UUID | None = None,
    ) -> AsyncIterator[BaseConnector]:
        async with session_scope() as session:
            connector = await build_connector(session, client_id, connector_name, site_id=site_id)
        # The session is closed before any HTTP happens: a slow vendor must not
        # hold a database connection open.
        async with connector:
            yield connector


_factory: ConnectorFactory = ConnectorFactory()


def get_factory() -> ConnectorFactory:
    return _factory


def set_factory(factory: ConnectorFactory) -> ConnectorFactory:
    """Install a different factory and return the previous one.

    Only tests use this. Production code always goes through the database.
    """
    global _factory
    previous = _factory
    _factory = factory
    return previous


@asynccontextmanager
async def connector_for(
    client_id: str | uuid.UUID,
    connector_name: ConnectorName,
    site_id: str | uuid.UUID | None = None,
) -> AsyncIterator[BaseConnector]:
    """Open a connector for a client, optionally one of its sites.

    This is what `tools.py` calls.
    """
    async with get_factory().open(client_id, connector_name, site_id) as connector:
        yield connector


async def why_not_configured(
    client_id: str | uuid.UUID,
    connector_name: ConnectorName,
    site_id: str | uuid.UUID | None = None,
) -> str | None:
    """`None` when a connector can be built for this scope; else why it cannot.

    A client with no credentials for a tool, or a site with no organisation
    mapped on it, is not a failed read: it is something we were never able to
    look at. A reader that cannot tell the two apart reports a gap where there
    is only an absence, which is the coverage board's rule ("a vendor that did
    not answer is not assessed this run, never absent") applied to one panel.

    One database read and no HTTP: the connector is opened and closed without a
    request being made.
    """
    try:
        async with connector_for(client_id, connector_name, site_id):
            return None
    except ConnectorNotConfigured as exc:
        return str(exc)


def site_choices(sites: Sequence[Site]) -> list[tuple[str, str]]:
    """`(slug, name)` pairs for the prompt. Never ids."""
    return [(site.slug, site.name) for site in sites]

"""Where a vendor's console lives, for the "open it there" links on the pages.

The point of these is the refusals. A NinjaOne tenant is on one of several
instances and an Autotask tenant on one of nineteen numbered zones, so a
guessed host is another tenant's sign-in page. Anything we do not recognise as
that vendor's own must produce no link at all, and the page then shows the
vendor id to copy instead.

Nothing here reads a secret: `console_urls` touches `ToolCredential.config`,
the non-secret half, and never `get_secrets()`.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.interfaces.consoles import console_urls
from app.models.enums import ConnectorName
from app.models.tool_credential import ToolCredential
from tests.api.conftest import Make

pytestmark = pytest.mark.usefixtures("clean")


async def _credential(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: object,
    connector: ConnectorName,
    config: dict[str, object],
    *,
    active: bool = True,
) -> None:
    async with session_maker() as session:
        row = ToolCredential(
            client_id=client_id,
            connector_name=connector,
            config=config,
            is_active=active,
        )
        row.set_secrets({"token": "not-read-here"})
        session.add(row)
        await session.commit()


async def test_a_ninjaone_instance_is_taken_from_the_clients_own_base_url(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client = await make_client("eu-franchisee")
    await _credential(
        session_maker,
        client.id,
        ConnectorName.NINJAONE,
        {"base_url": "https://eu.ninjarmm.com/"},
    )
    async with session_maker() as session:
        urls = await console_urls(session, client.id)
    assert urls == {"ninjaone": "https://eu.ninjarmm.com"}


async def test_a_ninjaone_region_with_no_base_url_still_gives_that_instance(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client = await make_client("oceania-franchisee")
    await _credential(session_maker, client.id, ConnectorName.NINJAONE, {"region": "oc"})
    async with session_maker() as session:
        urls = await console_urls(session, client.id)
    assert urls == {"ninjaone": "https://oc.ninjarmm.com"}


async def test_an_autotask_zone_is_renamed_from_the_api_host_to_the_ui_host(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client = await make_client("zone-fifteen")
    await _credential(
        session_maker,
        client.id,
        ConnectorName.AUTOTASK,
        {"base_url": "https://webservices15.autotask.net/ATServicesRest"},
    )
    async with session_maker() as session:
        urls = await console_urls(session, client.id)
    assert urls == {"autotask": "https://ww15.autotask.net"}


@pytest.mark.parametrize(
    ("slug", "connector", "config"),
    [
        # A demo tenant's fake host, a self-hosted proxy, a missing setting, an
        # Autotask host with no zone in it, a zone given as a bare number: none
        # of these is the vendor's console, and a link to them is worse than no
        # link, because it is another tenant's sign-in page.
        (
            "ninja-demo-host",
            ConnectorName.NINJAONE,
            {"base_url": "https://ninjaone.demo.invalid/api"},
        ),
        ("ninja-proxy-host", ConnectorName.NINJAONE, {"base_url": "https://rmm.example.com"}),
        ("ninja-no-setting", ConnectorName.NINJAONE, {}),
        ("at-demo-host", ConnectorName.AUTOTASK, {"base_url": "https://autotask.demo.invalid/api"}),
        ("at-no-zone", ConnectorName.AUTOTASK, {"base_url": "https://webservices.autotask.net"}),
        ("at-bare-region", ConnectorName.AUTOTASK, {"region": "15"}),
    ],
)
async def test_a_host_we_do_not_recognise_gives_no_link(
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    slug: str,
    connector: ConnectorName,
    config: dict[str, object],
) -> None:
    client = await make_client(slug)
    await _credential(session_maker, client.id, connector, config)
    async with session_maker() as session:
        urls = await console_urls(session, client.id)
    assert urls == {}


async def test_vendors_with_no_addressable_console_are_absent(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    """GravityZone and ConnectSecure have no route to one record we can build,
    so they are left out rather than approximated."""
    client = await make_client("every-connector")
    await _credential(
        session_maker,
        client.id,
        ConnectorName.BITDEFENDER,
        {"base_url": "https://cloud.gravityzone.bitdefender.com/api"},
    )
    await _credential(
        session_maker,
        client.id,
        ConnectorName.CONNECTSECURE,
        {"base_url": "https://portal-eu.mycybercns.com"},
    )
    async with session_maker() as session:
        urls = await console_urls(session, client.id)
    assert urls == {}


async def test_an_inactive_credential_gives_no_link(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> None:
    client = await make_client("retired-credential")
    await _credential(
        session_maker,
        client.id,
        ConnectorName.NINJAONE,
        {"base_url": "https://app.ninjarmm.com"},
        active=False,
    )
    async with session_maker() as session:
        urls = await console_urls(session, client.id)
    assert urls == {}


async def test_a_session_with_no_client_reads_nothing(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An Inputiv session that has not switched to a client yet. No query is
    made, because there is no tenant to make it for."""
    async with session_maker() as session:
        assert await console_urls(session, None) == {}

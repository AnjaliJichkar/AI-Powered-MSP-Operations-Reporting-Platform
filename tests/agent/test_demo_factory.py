"""The factory keeps live and demo apart, against a real database.

A demo client is served only by the fake classes, needs no credential, and
its audit rows are marked; a live client is served only by the real classes,
and without a credential it is refused rather than given a fake.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.connectors.factory as factory_module
from app.audit.recorder import AuditContext, is_demo_client, record_event
from app.connectors.autotask.client import AutotaskClient
from app.connectors.errors import ConnectorNotConfigured
from app.connectors.factory import build_connector
from app.connectors.ninjaone.client import NinjaOneClient
from app.demo.connectors import DemoNinjaOneClient, is_demo_connector
from app.demo.vocabulary import CUSTOMERS, FRANCHISEE_NAME
from app.models import AuditLog, Client
from app.models.client import ClientKind
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface
from app.models.site import Site
from app.models.tool_credential import ToolCredential
from app.models.tool_mapping import ToolMapping
from tests.agent.conftest import LIVE_URL

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set"),
]


@pytest.fixture
async def demo_client(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Client]:
    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(factory_module, "session_scope", scope)
    async with session_maker() as session:
        client = Client(slug="demo-under-test", name=FRANCHISEE_NAME, kind=ClientKind.DEMO)
        session.add(client)
        await session.commit()
        await session.refresh(client)
    yield client
    async with session_maker() as session:
        await session.execute(AuditLog.__table__.delete().where(AuditLog.client_id == client.id))
        await session.execute(Client.__table__.delete().where(Client.id == client.id))
        await session.commit()


async def _map(
    session: AsyncSession, client: Client, connector: ConnectorName, slug: str, external: str
) -> None:
    site = (
        await session.execute(select(Site).where(Site.client_id == client.id, Site.slug == slug))
    ).scalar_one_or_none()
    if site is None:
        site = Site(client_id=client.id, slug=slug, name=slug.title())
        session.add(site)
        await session.flush()
    session.add(
        ToolMapping(
            client_id=client.id, site_id=site.id, connector_name=connector, external_id=external
        )
    )


async def test_a_demo_client_is_served_by_the_fakes_with_no_credential(
    demo_client: Client, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    async with session_maker() as session:
        await _map(session, demo_client, ConnectorName.NINJAONE, "contoso", "100")
        await session.commit()
    async with session_maker() as session:
        connector = await build_connector(session, demo_client.id, ConnectorName.NINJAONE)
    assert isinstance(connector, DemoNinjaOneClient) and isinstance(connector, NinjaOneClient)
    assert is_demo_connector(connector)
    async with connector:
        devices, _ = await connector.list_devices()
    assert len(devices) == CUSTOMERS[0][2] + 1  # Contoso's computers and its switch
    assert connector.base_url.endswith(".demo.invalid")


async def test_a_live_client_never_gets_a_fake_and_needs_its_credential(
    db_client: Client, session_maker: async_sessionmaker[AsyncSession], settings: Any
) -> None:
    async with session_maker() as session:
        await _map(session, db_client, ConnectorName.AUTOTASK, "a-site", "77")
        await session.commit()
    async with session_maker() as session:
        with pytest.raises(ConnectorNotConfigured, match="no autotask credentials"):
            await build_connector(session, db_client.id, ConnectorName.AUTOTASK)
    async with session_maker() as session:
        credential = ToolCredential(client_id=db_client.id, connector_name=ConnectorName.AUTOTASK)
        credential.set_secrets({"api_integration_code": "x", "username": "u", "secret": "s"})
        credential.config = {"base_url": "https://example.invalid"}
        session.add(credential)
        await session.commit()
    async with session_maker() as session:
        connector = await build_connector(session, db_client.id, ConnectorName.AUTOTASK)
    assert type(connector) is AutotaskClient and not is_demo_connector(connector)


async def test_set_credentials_refuses_the_demo_client(
    demo_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.manage_client as manage

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    monkeypatch.setattr(manage, "session_scope", scope)
    prompted: list[str] = []
    monkeypatch.setattr(manage, "collect_secrets", lambda *a, **k: prompted.append("asked") or {})
    args = argparse.Namespace(
        connector="ninjaone", client=demo_client.slug, region=None, no_prompt=True, user="t"
    )
    with pytest.raises(SystemExit, match="demo tenant"):
        await manage.cmd_set_credentials(args)
    assert prompted == []  # refused before anyone typed a secret
    async with session_maker() as session:
        held = (
            (
                await session.execute(
                    select(ToolCredential).where(ToolCredential.client_id == demo_client.id)
                )
            )
            .scalars()
            .all()
        )
    assert held == []


async def test_audit_rows_for_the_demo_client_are_marked_and_others_are_not(
    demo_client: Client, db_client: Client, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    async with session_maker() as session:
        assert await is_demo_client(session, demo_client.id) is True
        assert await is_demo_client(session, db_client.id) is False
        assert await is_demo_client(session, uuid.uuid4()) is False
        for client in (demo_client, db_client):
            context = AuditContext.new(
                actor_id="t", interface=Interface.CLI, client_id=client.id, client_ref=client.slug
            )
            await record_event(
                session,
                context,
                event_type=AuditEventType.RUN_STARTED,
                outcome=AuditOutcome.SUCCESS,
            )
        await session.commit()
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(AuditLog.client_id, AuditLog.demo).where(
                    AuditLog.client_id.in_([demo_client.id, db_client.id])
                )
            )
        ).all()
    assert dict(rows) == {demo_client.id: True, db_client.id: False}


async def test_the_whole_product_reads_the_demo_tenant_through_the_fakes(
    demo_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sites synced by the same sync a live client gets, the coverage board computed,
    and a review's facts collected for one customer: all through the fakes, no network,
    and every scenario the board can show, shown."""
    import app.coverage.compute as compute_module
    import app.reports.tbr.audit as audit_module
    from app.coverage.compute import compute_coverage
    from app.reports.tbr.audit import NullAudit
    from app.reports.tbr.collect import Collector
    from app.reports.tbr.config import FranchiseeConfig
    from app.reports.tbr.model import ReviewPeriod

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(compute_module, "session_scope", scope)
    monkeypatch.setattr(audit_module, "session_scope", scope)
    monkeypatch.setenv("COVERAGE_RMM_STALE_DAYS", "14")

    import scripts.sync_sites as sync_script

    monkeypatch.setattr(sync_script, "session_scope", scope)
    for connector_name in (
        ConnectorName.NINJAONE,
        ConnectorName.AUTOTASK,
        ConnectorName.BITDEFENDER,
        ConnectorName.CONNECTSECURE,
    ):
        assert await sync_script.sync_one(demo_client.id, connector_name, dry_run=False) == 0
    async with session_maker() as session:
        sites = (
            (await session.execute(select(Site).where(Site.client_id == demo_client.id)))
            .scalars()
            .all()
        )
    active = [s for s in sites if s.is_active]
    assert sorted(s.slug for s in sites if not s.is_active) == ["tailwind-it-services"]  # internal
    assert len(active) == 8

    board = await compute_coverage(demo_client.id, through_identity=False)
    states = {
        (tool, state.state) for device in board.devices for tool, state in device.tools.items()
    }
    for wanted in (
        ("bitdefender", "covered"),
        ("bitdefender", "absent"),
        ("bitdefender", "unmatched"),
        ("bitdefender", "rmm_stale"),
        ("bitdefender", "rmm_agent_down"),
        ("connectsecure", "stale"),
        ("connectsecure", "not_assessed"),
        ("backup", "not_assessed"),
    ):
        assert wanted in states, wanted
    assert board.not_computers == 8 and len(board.discovered) == 2
    assert sum(site.external for site in board.sites) == 2
    assert any("not yet onboarded" in (u.note or "") for u in board.unmanaged)
    passes = {state.matched_by for device in board.devices for state in device.tools.values()}
    assert {"exact", "mac"} <= {p for p in passes if p}

    contoso = next(s for s in active if s.slug == "contoso")
    run = Collector(
        client_id=str(demo_client.id),
        site_id=str(contoso.id),
        period=ReviewPeriod.ending(2026, 9),
        config=FranchiseeConfig(display_name="Tailwind IT Services", show_people_names=True),
        audit=NullAudit(),
    )
    facts = await run.collect()
    for fact_id in (
        "ninjaone.devices.workstations",
        "autotask.tickets.created",
        "bitdefender.endpoints.total",
        "connectsecure.vulnerabilities.critical",
        "connectsecure.remediations.uninstall",
        "ninjaone.patches.installed",
        "ninjaone.software_patches.installed",
    ):
        assert fact_id in facts.facts, fact_id
    assert facts.tables["ninjaone.lifecycle.workstations"].rows
    assert not [g for g in facts.gaps if "credentials" in g.why.casefold()]


async def test_the_microsoft_365_commands_run_on_the_demo_tenant(
    demo_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`m365.py profile` and `m365.py people`, end to end on the demo tenant.

    The same commands a live client runs, over the fake Graph, the fake
    Autotask and the fake NinjaOne: no network, no credential and nothing
    written to any tenant. What is checked is the whole path — the mapping a
    person makes by hand (Microsoft 365 has no site discovery), the tenant
    chosen by site, the printed profile, and the join of the tenant's accounts
    to that customer's contacts and machines.
    """
    import argparse

    import scripts.demo_tenant as demo_script
    import scripts.m365 as m365
    import scripts.sync_sites as sync_script

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    import app.devices.resolve as resolve_module

    monkeypatch.setattr(sync_script, "session_scope", scope)
    monkeypatch.setattr(m365, "session_scope", scope)
    monkeypatch.setattr(resolve_module, "session_scope", scope)
    for connector_name in (ConnectorName.NINJAONE, ConnectorName.AUTOTASK):
        assert await sync_script.sync_one(demo_client.id, connector_name, dry_run=False) == 0
    # The machine half of the join goes through the identity layer, so the day's
    # resolution runs first: a machine nothing has resolved is not ours to pair.
    resolved = await resolve_module.resolve_devices(demo_client.id, actor="tests")
    assert resolved.devices_created > 0
    async with session_maker() as session:
        client = (
            await session.execute(select(Client).where(Client.id == demo_client.id))
        ).scalar_one()
        mapped = await demo_script._map_microsoft365(session, client)
        await session.commit()
    assert mapped == 8  # one Entra tenant per end customer, by hand

    args = argparse.Namespace(client=demo_client.slug, site="contoso", json=False)
    assert await m365._profile(args) == 0
    printed = capsys.readouterr().out
    assert "Microsoft 365 profile for" in printed
    assert "cloud-only" in printed and "synced from on-premises AD" in printed
    assert "enabled member accounts" in printed  # MFA coverage, with its population
    assert "distribution list(s)" in printed
    assert "Nothing was written" in printed

    # A tenant the client has many of needs naming: answering for all of them
    # would merge two companies' directories into one profile.
    without_a_site = argparse.Namespace(client=demo_client.slug, site=None, json=False)
    assert await m365._profile(without_a_site) == 2  # usage: it says to name the site
    assert "name the site" in capsys.readouterr().err

    assert await m365._people(args) == 0
    joined = capsys.readouterr().out
    assert "matched to an Autotask contact by email" in joined
    assert "matched to a machine by last logged-on user" in joined
    assert "(login name)" in joined or "(display name)" in joined

    # And the join itself, read as JSON: people matched, and no name anywhere.
    import json as json_module

    assert (
        await m365._people(argparse.Namespace(client=demo_client.slug, site="contoso", json=True))
        == 0
    )
    payload = json_module.loads(capsys.readouterr().out)
    assert payload["accounts"] > 0
    assert payload["accounts_with_a_contact"] > 0
    assert payload["accounts_with_a_device"] > 0
    assert "contoso.example" not in json_module.dumps(payload)


async def test_a_tenant_id_is_mapped_onto_a_site_by_hand_and_never_stolen(
    db_client: Client,
    demo_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Microsoft 365 cannot be synced — a Graph token is for one directory and
    cannot enumerate the others — so a person maps each customer's Entra tenant
    id onto its site. The command refuses an id another client holds, exactly
    as the sync does: re-pointing it would move one franchisee's customer into
    another franchisee's view."""
    import scripts.manage_client as manage

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(manage, "session_scope", scope)
    tenant = "0f8fad5b-d9cb-469f-a165-70867728950e"
    async with session_maker() as session:
        for client, slug in ((db_client, "a-customer"), (demo_client, "another-customer")):
            session.add(Site(client_id=client.id, slug=slug, name=slug.title()))
        await session.commit()

    def args(client: Client, site: str) -> argparse.Namespace:
        return argparse.Namespace(
            client=client.slug,
            site=site,
            connector="microsoft365",
            external_id=tenant,
            name=None,
        )

    assert await manage.cmd_map_site(args(db_client, "a-customer")) == 0
    # Idempotent: the same mapping again is not a second row.
    assert await manage.cmd_map_site(args(db_client, "a-customer")) == 0
    with pytest.raises(SystemExit, match="another client"):
        await manage.cmd_map_site(args(demo_client, "another-customer"))
    with pytest.raises(SystemExit, match="not a site"):
        await manage.cmd_map_site(args(db_client, "nobody"))

    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(ToolMapping).where(
                        ToolMapping.connector_name == ConnectorName.MICROSOFT365,
                        ToolMapping.external_id == tenant,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [row.client_id for row in rows] == [db_client.id]

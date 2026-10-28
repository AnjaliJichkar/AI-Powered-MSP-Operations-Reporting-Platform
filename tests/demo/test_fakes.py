"""The fake vendors, through the real clients: same models, same guards, no network.

No database: the scope is built by hand from the world, as the factory would build
it from the synced sites. Every read below is one the product makes.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter

import httpx
import pytest

from app.connectors.bitdefender.client import ReadOnlyViolation as BdReadOnly
from app.connectors.connectsecure.client import ReadOnlyViolation as CsReadOnly
from app.connectors.errors import ArgumentRejected, NotFound
from app.connectors.scope import ClientScope, SiteRef
from app.demo.connectors import (
    DEMO_CONNECTOR_CLASSES,
    DemoAutotaskClient,
    DemoBitdefenderClient,
    DemoConnectSecureClient,
    DemoNinjaOneClient,
    DemoTransportError,
    demo_world,
    is_demo_connector,
)
from app.demo.dataset import World
from app.models.enums import ConnectorName

WORLD = demo_world("test-demo")


def scope_for(name: ConnectorName, world: World = WORLD) -> ClientScope:
    refs = []
    for customer in world.customers:
        externals = {
            ConnectorName.NINJAONE: [str(customer.ninja_org_id)],
            ConnectorName.AUTOTASK: [str(customer.autotask_company_id)],
            ConnectorName.BITDEFENDER: list(customer.bitdefender_company_ids),
            ConnectorName.CONNECTSECURE: (
                [str(customer.connectsecure_company_id)]
                if customer.connectsecure_company_id
                else []
            ),
        }[name]
        for external in externals:
            refs.append(
                SiteRef(
                    site_id=uuid.uuid5(uuid.NAMESPACE_DNS, customer.key),
                    slug=customer.key,
                    name=customer.name,
                    external_id=external,
                )
            )
    return ClientScope(
        client_id=uuid.uuid5(uuid.NAMESPACE_DNS, "demo-client"),
        client_slug="test-demo",
        connector=name,
        allowed=tuple(refs),
        targets=tuple(refs),
        excluded=(),
    )


# --- construction and the wire ------------------------------------------------------------


def test_a_demo_class_is_the_real_client_and_needs_a_world() -> None:
    from app.connectors.ninjaone.client import NinjaOneClient

    assert issubclass(DemoNinjaOneClient, NinjaOneClient)
    with pytest.raises(TypeError, match="World"):
        DemoNinjaOneClient(scope=scope_for(ConnectorName.NINJAONE), world=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        DemoNinjaOneClient(scope=scope_for(ConnectorName.NINJAONE))  # type: ignore[call-arg]
    assert set(DEMO_CONNECTOR_CLASSES) == {
        ConnectorName.NINJAONE,
        ConnectorName.AUTOTASK,
        ConnectorName.BITDEFENDER,
        ConnectorName.CONNECTSECURE,
        ConnectorName.COVE,
        ConnectorName.MICROSOFT365,
    }


async def test_the_transport_never_leaves_the_process() -> None:
    async with DemoNinjaOneClient(scope=scope_for(ConnectorName.NINJAONE), world=WORLD) as ninja:
        assert is_demo_connector(ninja)
        assert ninja.base_url.endswith(".demo.invalid")
        assert isinstance(ninja.http._transport, httpx.MockTransport)
        with pytest.raises(DemoTransportError):
            await ninja.http.get("https://app.ninjarmm.com/v2/devices")
        # A path the fake does not know is the vendor's 404, not a crash.
        with pytest.raises(NotFound):
            await ninja.request("GET", "/v2/nothing-here")


# --- NinjaOne -------------------------------------------------------------------------------


async def test_ninjaone_lists_and_pages_the_world() -> None:
    async with DemoNinjaOneClient(scope=scope_for(ConnectorName.NINJAONE), world=WORLD) as ninja:
        organisations = await ninja.list_organisations()
        assert len(organisations) == 9  # eight customers and the franchisee's own
        devices, truncated = await ninja.list_devices()
        assert len(devices) == len(WORLD.devices) and not truncated
        classes = Counter(device.node_class for _, device in devices)
        assert classes["NMS_SWITCH"] == 8 and classes["WINDOWS_SERVER"] >= 8 and classes["MAC"] >= 5
        # The query envelope pages under the client's own offset checks.
        rows, truncated, total = await ninja.query("computer-systems", page_size=100)
        assert len(rows) == len([d for d in WORLD.devices if d.is_agent_class]) and not truncated
        assert total == len(rows)
        # The waiting list ignores an unknown status by returning nothing; the history returns all.
        empty, _, _ = await ninja.query("os-patches", params={"status": "BOGUS"})
        assert empty == []
        installed, _, _ = await ninja.query("os-patch-installs", params={"status": "INSTALLED"})
        everything, _, _ = await ninja.query("os-patch-installs", params={"status": "BOGUS"})
        assert len(everything) > len(installed) > 0
        locations = await ninja.list_locations()
        assert len(locations) == sum(len(c.locations) for c in WORLD.customers)
        alerts = await ninja.list_alerts()
        assert len(alerts) == sum(1 for a in WORLD.alerts if a.resolved_days is None)


async def test_ninjaone_site_scoped_reads_carry_the_org_filter_and_a_null_count() -> None:
    world = WORLD
    contoso = next(
        ref for ref in scope_for(ConnectorName.NINJAONE).allowed if ref.slug == "contoso"
    )
    scope = scope_for(ConnectorName.NINJAONE)
    scoped = ClientScope(
        client_id=scope.client_id,
        client_slug=scope.client_slug,
        connector=scope.connector,
        allowed=scope.allowed,
        targets=(contoso,),
        excluded=(),
    )
    async with DemoNinjaOneClient(scope=scoped, world=world) as ninja:
        rows, _, _ = await ninja.query_scoped("operating-systems")
        assert len(rows) == len([d for d in world.devices_of("contoso") if d.is_agent_class])
        assert ("GET", "/v2/queries/operating-systems") in ninja.api.router.calls


# --- Autotask -------------------------------------------------------------------------------


async def test_autotask_answers_queries_picklists_and_pages() -> None:
    async with DemoAutotaskClient(scope=scope_for(ConnectorName.AUTOTASK), world=WORLD) as autotask:
        organisations = await autotask.list_organisations()
        assert len(organisations) == 8  # the internal and the prospect are not customers
        statuses = await autotask.picklist("Tickets", "status")
        assert "Complete" in statuses.values()
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
        tickets, truncated = await autotask.list_tickets(open_only=False, created_since=since)
        assert not truncated
        # `created_days` is a float of days and `created_since` is an instant
        # worked out here, while the fake works its own out when it serves the
        # row: a ticket within a few hours of the boundary falls on either
        # side depending on which of the two clocks ran first. The slack is
        # the tickets actually in that band rather than a number that happened
        # to be big enough on the day it was written — it was 2, and a world
        # with three tickets near the thirtieth day failed the suite on a
        # change that had nothing to do with it.
        expected = sum(1 for t in WORLD.tickets if t.created_days <= 30)
        boundary = sum(1 for t in WORLD.tickets if 29.5 <= t.created_days <= 30.5)
        assert abs(len(tickets) - expected) <= boundary
        assert all(ticket.company_id for _, ticket in tickets)
        # More than one page: the client followed nextPageUrl for the whole history.
        everything, _ = await autotask.list_tickets(
            open_only=False, created_since=since - dt.timedelta(days=60)
        )
        assert len(everything) > 500
        followed = [
            str(request.url)
            for request in autotask.api.router.requests
            if "page=1" in str(request.url)
        ]
        assert followed, "the second page was never fetched"


# --- Bitdefender -----------------------------------------------------------------------------


async def test_bitdefender_inventory_licences_details_and_incidents() -> None:
    scope = scope_for(ConnectorName.BITDEFENDER)
    async with DemoBitdefenderClient(scope=scope, world=WORLD) as bitdefender:
        companies = await bitdefender.list_organisations()
        assert len(companies) == 9  # Fabrikam holds two
        endpoints, truncated = await bitdefender.list_endpoints()
        assert not truncated
        held = sum(1 for d in WORLD.devices if d.in_bitdefender and d.is_agent_class)
        assert len(endpoints) >= held
        licences = await bitdefender.licences()
        assert {lic.manage_event_correlator for lic in licences.values()} == {True, False}
        assert not any(lic.manage_encryption for lic in licences.values())
        woodgrove = next(ref for ref in scope.allowed if ref.slug == "woodgrove")
        incidents, _ = await bitdefender.list_incidents(
            since=dt.datetime.now(dt.UTC) - dt.timedelta(days=90),
            until=dt.datetime.now(dt.UTC),
            sites=[woodgrove],
        )
        assert len(incidents) == len(WORLD.detections)
        contoso = next(ref for ref in scope.allowed if ref.slug == "contoso")
        quiet, _ = await bitdefender.list_incidents(
            since=dt.datetime.now(dt.UTC) - dt.timedelta(days=90),
            until=dt.datetime.now(dt.UTC),
            sites=[contoso],
        )
        assert quiet == []  # no EDR licence: an empty page, not an error
        site, first = endpoints[0]
        detail = await bitdefender.get_endpoint_details(site, first.id)
        assert detail.last_seen is not None
        with pytest.raises(BdReadOnly):
            await bitdefender.call("network", "createScanTask", {})
        with pytest.raises((NotFound, ArgumentRejected)):
            await bitdefender.get_endpoint_details(site, "64e000000000000000000000")


# --- ConnectSecure ----------------------------------------------------------------------------


async def test_connectsecure_reads_both_populations_and_refuses_writes() -> None:
    async with DemoConnectSecureClient(
        scope=scope_for(ConnectorName.CONNECTSECURE), world=WORLD
    ) as cs:
        companies = await cs.list_organisations()
        assert len(companies) == 7  # the deleted and the assessment companies are skipped
        assets, _ = await cs.list_assets()
        assert len(assets) >= sum(
            1 for d in WORLD.devices if d.in_connectsecure and d.is_agent_class
        )
        confirmed, _ = await cs.list_vulnerabilities()
        both, _ = await cs.list_vulnerabilities(include_unconfirmed=True)
        assert 0 < len(confirmed) < len(both)
        kev, _ = await cs.list_vulnerabilities(with_known_exploited=True)
        assert any(v.known_exploited for _, v in kev)
        remediations, _ = await cs.list_remediations()
        kinds = Counter(r.kind for _, r in remediations)
        assert kinds["patch"] > 0 and kinds["uninstall"] > 0
        assert any(r.fix[0].isdigit() for _, r in remediations)  # a target version, not a KB
        with pytest.raises(CsReadOnly):
            await cs.request("POST", "/w/company/companies", json_body={})
        with pytest.raises(CsReadOnly):
            await cs.request("GET", "/r/something/else")


async def test_the_real_cove_client_reads_the_fake_and_places_every_row() -> None:
    """Cove over the fake: the visa chain, the recursive read and the guard.

    A read at our own partner covers the tree beneath it, which is why the
    client reads the whole fleet in one call; the rows carry their own partner
    and the client places them. The tenant rows are in that list and are not
    machines, which is the one thing about Cove that every count has to know.
    """
    from app.connectors.cove.client import CoveClient
    from app.demo.connectors import DemoCoveClient

    assert issubclass(DemoCoveClient, CoveClient)
    customers = [c for c in WORLD.customers if c.cove_partner_id is not None]
    refs = tuple(
        SiteRef(
            site_id=uuid.uuid4(),
            slug=c.key,
            name=c.name,
            external_id=str(c.cove_partner_id),
        )
        for c in customers
    )
    scope = ClientScope(
        client_id=uuid.uuid4(),
        client_slug="tailwind-it-demo",
        connector=ConnectorName.COVE,
        allowed=refs,
        targets=refs,
    )
    async with DemoCoveClient(scope=scope, world=WORLD) as cove:
        organisations = await cove.list_organisations()
        # The franchisee's own partner is a Distributor and is not a customer.
        assert len(organisations) == len(customers)

        rows = await cove.list_fleet_with_sites()
        machines = [d for _, d in rows if d.is_machine]
        tenants = [d for _, d in rows if d.is_microsoft365]
        assert len(tenants) == len(customers)
        assert len(machines) > 150
        # One read, not one per customer.
        assert cove.calls_made.count("EnumerateAccountStatistics") == 1

        # The scenarios the demo is for, read back through the real client.
        never_recent = [d for d in machines if d.last_successful_at is None]
        assert not never_recent  # every Cove device here has backed up at some point
        assert any((d.colour_bar or "").count("2") > 5 for d in machines)
        assert any(d.last_session_status == "completed_with_errors" for d in machines)

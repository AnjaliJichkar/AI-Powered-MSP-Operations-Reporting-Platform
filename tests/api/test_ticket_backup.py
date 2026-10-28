"""Backup on a ticket's device panel: what Cove says about this machine.

The enrichment is the real one; only Cove is faked, at the read the panel
makes, so what is tested is the four readings, the word beside them (the
review's own `status_of`, so a machine reads the same in both places), the
sentence a customer with no Cove gets instead — not assessed, never a gap —
the line a machine with no Cove record gets, and that the findings the card
is built from come out of those readings and nothing else.

Every name here is invented.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.connectors.factory as factory_module
import app.tickets.enrich as enrich_module
from app.audit.recorder import AuditContext
from app.autopilot.findings import findings_for, tone_for
from app.connectors.cove.client import CoveClient
from app.connectors.cove.history import History
from app.connectors.cove.models import CoveDevice
from app.models.device import Device, DeviceVendorLink
from app.models.enums import ConnectorName, Interface
from app.models.site import Site
from app.reports.tbr import backup
from app.reports.tbr.config import load_config, save_config
from app.tickets.enrich import enrich
from tests.api.conftest import Make

pytestmark = pytest.mark.usefixtures("clean")

NOW = dt.datetime(2026, 10, 19, 12, tzinfo=dt.UTC)
MACHINE = "fab-srv-01"


def a_cove_device(
    bar: str = "5" * 28,
    *,
    key: str = MACHINE,
    last_success_days: int | None = 0,
    product: str | None = "All-In",
    os_type: str = "server",
) -> CoveDevice:
    return CoveDevice(
        account_id=9001,
        partner_id=4242,
        name=key,
        key=key,
        colour_bar=bar,
        os_type=os_type,
        # Cove sends the last session's own status beside the bar, and they
        # agree about today: character 0 of the bar is that session.
        last_session_status=History.read(bar).days[0],
        product=product,
        account_type="backup_manager",
        created_at=NOW - dt.timedelta(days=400),
        last_successful_at=(
            None if last_success_days is None else NOW - dt.timedelta(days=last_success_days)
        ),
    )


@pytest.fixture
async def world(
    make_client: Make,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """A customer with one machine the identity layer holds a Cove link for."""

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    monkeypatch.setattr(factory_module, "session_scope", scope)
    client = await make_client("fabrikam-it", "Fabrikam IT")
    async with session_maker() as session:
        site = Site(client_id=client.id, slug="fab-legal", name="Fabrikam Legal")
        session.add(site)
        await session.flush()
        device = Device(
            client_id=client.id,
            site_id=site.id,
            canonical_name=MACHINE,
            netbios_key=MACHINE[:15],
            first_seen_at=NOW - dt.timedelta(days=100),
            last_seen_at=NOW,
        )
        session.add(device)
        await session.flush()
        session.add(
            DeviceVendorLink(
                client_id=client.id,
                device_id=device.id,
                connector_name=ConnectorName.COVE,
                external_id="9001",
                vendor_name=MACHINE,
                matched_by="exact",
                first_seen_at=NOW - dt.timedelta(days=100),
                last_seen_at=NOW,
            )
        )
        await session.commit()
        ids = {"site_id": site.id, "device_id": device.id}
    return {"client": client, **ids}


@pytest.fixture
def cove(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Cove faked at the panel's own read, and told to be configured. Its
    knobs: the customer's rows to answer with, and what it was asked for."""
    knobs: dict[str, Any] = {"device": a_cove_device(), "extra": [], "asked": [], "no_access": {}}

    async def fake_read(client_id: str, *, site_id: str | None) -> list[Any]:
        knobs["asked"].append(site_id)
        machine = knobs["device"]
        return ([machine] if machine is not None else []) + list(knobs["extra"])

    async def fake_why(client_id: Any, connector: Any, site_id: Any = None) -> str | None:
        answer: str | None = knobs["no_access"].get(connector)
        return answer

    monkeypatch.setattr(enrich_module, "_cove_devices", fake_read)
    monkeypatch.setattr(enrich_module, "why_not_configured", fake_why)
    return knobs


async def read_facts(
    session_maker: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    *,
    linked: bool = True,
) -> dict[str, Any]:
    context = AuditContext.new(
        actor_id="tests",
        interface=Interface.API,
        client_id=world["client"].id,
        client_ref="fabrikam-it",
    )
    async with session_maker() as session:
        device = await session.get(Device, world["device_id"])
        assert device is not None
        rows = await session.execute(
            select(DeviceVendorLink).where(DeviceVendorLink.device_id == device.id)
        )
        links = list(rows.scalars()) if linked else []
        result = await enrich(
            session,
            context,
            client_id=world["client"].id,
            device=device,
            links=links,
            contact=None,
            site_id=world["site_id"],
            ticket_number="T7001",
            now=NOW,
        )
        await session.commit()
    return result.as_dict()


def by_id(payload: dict[str, Any]) -> dict[str, Any]:
    return {fact["id"]: fact for fact in payload["facts"]}


def values(payload: dict[str, Any]) -> dict[str, Any]:
    return {fact["id"]: fact["value"] for fact in payload["facts"]}


async def _set_scope(
    session_maker: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    scope: str,
    *,
    excluded: list[str] | None = None,
) -> None:
    """This site's backup settings, written where the review reads them."""
    async with session_maker() as session:
        config = await load_config(session, world["client"].id, client_name="Fabrikam IT")
        config.backup.scope["fab-legal"] = scope
        if excluded:
            config.backup.no_backup_needed["fab-legal"] = excluded
        await save_config(session, world["client"].id, config, updated_by="tests")
        await session.commit()


def kinds(payload: dict[str, Any]) -> list[str]:
    return [one.kind for one in findings_for(values(payload), now=NOW)]


# --- the readings ---------------------------------------------------------------------


async def test_a_healthy_machine_reads_as_backed_up(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Four readings, the word from the review's own rule, and the retention
    policy beside it. Asked for the name the link carries, at the ticket's own
    customer."""
    payload = await read_facts(session_maker, world)
    assert cove["asked"] == [str(world["site_id"])]
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "ok"
    assert facts["backup.status"]["detail"] == "All-In retention"
    assert facts["backup.days_since_successful"]["value"] == 0
    assert facts["backup.last_successful"]["value"] == NOW.isoformat()
    assert facts["backup.failures_7d"]["value"] == 0
    assert all(
        facts[fact_id]["source"] == "cove.list_devices"
        for fact_id in facts
        if fact_id.startswith("backup.")
    )
    # Well, and well is the absence of a finding rather than a sentence saying so.
    assert tone_for("backup.status", "ok", now=NOW) == "good"
    assert [kind for kind in kinds(payload) if kind.startswith("backup")] == []


async def test_the_status_is_where_the_machine_stands_now(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A machine that failed a fortnight ago and has backed up every night
    since is backed up: the status is where it stands now, here and in the
    review alike. The month's failures are the failure count beside it."""
    # Succeeded every day for a week; one failed day twenty days back.
    cove["device"] = a_cove_device("5" * 20 + "2" + "5" * 7)
    payload = await read_facts(session_maker, world)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "ok"
    assert facts["backup.failures_7d"]["value"] == 0
    assert [kind for kind in kinds(payload) if kind.startswith("backup")] == []
    # And the review says the same word about the same machine.
    assert backup.status_of(cove["device"], now=NOW) == "ok"


async def test_a_failed_job_is_not_a_machine_nothing_has_backed_up(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A job that ran and broke is a different problem from a machine nothing
    runs on, and the two are never the same sentence."""
    cove["device"] = a_cove_device("2" + "5" * 27, last_success_days=4)
    payload = await read_facts(session_maker, world)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "failed"
    assert facts["backup.failures_7d"]["value"] == 1
    assert "backup_failed" in kinds(payload)


async def test_a_second_failure_in_the_week_says_so(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The question on a ticket is whether somebody has seen this before, which
    is why the count is a week and not the review's 28 days."""
    cove["device"] = a_cove_device("22" + "5" * 26, last_success_days=4)
    payload = await read_facts(session_maker, world)
    assert by_id(payload)["backup.failures_7d"]["value"] == 2
    found = findings_for(values(payload), now=NOW)
    repeat = next(one for one in found if one.kind == "backup_repeat_failures")
    assert repeat.text == "2 backup failures this week"
    assert repeat.chip == "2 failures this week"
    # The two are one finding, not two: a card that said both would be saying
    # the same thing twice.
    assert "backup_failed" not in [one.kind for one in found]


async def test_a_machine_nothing_has_backed_up_counts_the_days(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    cove["device"] = a_cove_device("0" * 28, last_success_days=11)
    payload = await read_facts(session_maker, world)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "not_backed_up"
    assert facts["backup.days_since_successful"]["value"] == 11
    found = findings_for(values(payload), now=NOW)
    none = next(one for one in found if one.kind == "backup_none")
    assert none.text == "No successful backup on this machine"
    assert none.detail == "last succeeded 11 days ago"
    assert none.source == "Cove"


async def test_never_is_not_a_very_large_number_of_days(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A machine Cove has never recorded a success for says that, rather than
    printing a count nobody can check."""
    cove["device"] = a_cove_device("0" * 28, last_success_days=None)
    payload = await read_facts(session_maker, world)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "not_backed_up"
    assert facts["backup.days_since_successful"]["value"] is None
    assert facts["backup.last_successful"]["value"] is None
    none = next(one for one in findings_for(values(payload), now=NOW) if one.kind == "backup_none")
    assert none.chip == "Never backed up"
    assert none.detail == "Cove has never recorded a successful backup"


# --- the two absences, which are not the same thing -----------------------------------


async def test_a_customer_with_no_cove_is_not_assessed(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The coverage board's rule on one panel: a tool nobody asked is a gap in
    the evidence and not a clean bill of health — and not a failed read either,
    which is why it is decided before the read is made."""
    cove["no_access"][ConnectorName.COVE] = "no credential for this client"
    payload = await read_facts(session_maker, world)
    facts = by_id(payload)
    assert facts["backup.not_assessed"]["value"] is None
    assert facts["backup.not_assessed"]["detail"] == (
        "Cove is not set up for this customer, so backup was not assessed "
        "(no credential for this client)"
    )
    assert "backup.status" not in facts
    # Nothing was asked, so nothing is named: "not set up for this customer" is
    # not a reading that rests on a read.
    assert facts["backup.not_assessed"]["rests_on"] == []
    assert cove["asked"] == []
    # Not a failed read: this client has no NinjaOne credential either and the
    # panel says so on its own lines, which is the panel working.
    assert [row for row in payload["errors"] if "cove" in row["source"]] == []
    assert [kind for kind in kinds(payload) if kind.startswith("backup")] == []


async def test_a_machine_with_no_cove_record_is_not_backed_up(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Cove is set up for this customer, a workstation of theirs is backed up,
    and this machine has no record. That is a finding about the machine rather
    than a gap in the evidence."""
    cove["extra"] = [a_cove_device(key="fab-ws-09", os_type="workstation")]
    payload = await read_facts(session_maker, world, linked=False)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "not_backed_up"
    assert facts["backup.status"]["source"] == "identity"
    assert facts["backup.status"]["detail"] == "no Cove device is linked to this machine"
    assert "backup_none" in kinds(payload)
    # Decided by the identity layer and reached by reading Cove, so the panel's
    # "from" line names Cove: a tool that was asked is not left off it.
    assert facts["backup.status"]["rests_on"] == ["cove.list_devices"]


# --- the scope the review applies, applied here -----------------------------------


async def test_a_machine_outside_the_sites_backup_scope_is_not_a_gap(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A workstation at a customer who backs up servers only is not a finding,
    and a red line on every laptop of a customer who never bought workstation
    backup is a page people learn to ignore. Auto, with no workstation backed
    up here, says servers only."""
    payload = await read_facts(session_maker, world, linked=False)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "not_in_scope"
    assert facts["backup.status"]["detail"] == (
        "servers only at this site, as no workstations are backed up in Cove here"
    )
    # Not a finding, and not a dot either: nothing is wrong with this machine.
    assert [kind for kind in kinds(payload) if kind.startswith("backup")] == []
    assert tone_for("backup.status", "not_in_scope", now=NOW) == "neutral"


async def test_the_sites_own_setting_beats_what_cove_happens_to_hold(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The customer who has bought workstation backup and not installed it yet
    is the case auto gets wrong, and the case where the gap matters most."""
    await _set_scope(session_maker, world, "servers_and_workstations")
    payload = await read_facts(session_maker, world, linked=False)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "not_backed_up"
    assert "backup_none" in kinds(payload)


async def test_a_machine_the_site_has_decided_needs_no_backup_is_out_of_scope(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A till, a lab box, a spare laptop: decided once, per site, and the same
    list the review's gap uses."""
    await _set_scope(session_maker, world, "servers_and_workstations", excluded=[MACHINE])
    payload = await read_facts(session_maker, world, linked=False)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "not_in_scope"
    assert facts["backup.status"]["detail"] == (
        "this site has recorded that this machine needs no backup"
    )


async def test_the_customers_own_read_and_never_the_fleet(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fleet read covers the whole allowlist, so a machine of this name at a
    sibling customer would come back from it. The panel is about one site's
    machine, so it is the site's own read that is made."""
    asked: list[str] = []

    class FakeCove(CoveClient):
        def __init__(self) -> None:  # no credentials, no transport, no request
            pass

        async def list_devices_for_targets(self) -> list[tuple[Any, list[CoveDevice]]]:
            asked.append("targets")
            return [(None, [a_cove_device()])]

        async def list_fleet_with_sites(self) -> list[tuple[Any, CoveDevice]]:
            raise AssertionError("the panel must not read the fleet")

    @asynccontextmanager
    async def fake_connector(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield FakeCove()

    monkeypatch.setattr(enrich_module, "connector_for", fake_connector)
    found = await enrich_module._cove_devices(str(uuid.uuid4()), site_id=str(world["site_id"]))
    assert asked == ["targets"]
    assert [d.match_key for d in found] == [MACHINE]


async def _set_class(
    session_maker: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    node_class: str,
    os_value: str | None,
) -> None:
    """What NinjaOne says this machine is, and what the identity layer stored."""
    async with session_maker() as session:
        device = await session.get(Device, world["device_id"])
        assert device is not None
        device.node_class = node_class
        device.os = os_value
        await session.commit()


async def test_a_windows_server_at_a_servers_only_site_is_expected_to_be_backed_up(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A server is in scope at a site that backs up servers, which is the point.

    It reads that way from the **node class**, because `Device.os` is not an OS
    name for a machine NinjaOne found: the org-scoped device listing carries no
    OS string, so the identity layer stores a family — `"Windows"` — and
    `device_role` reads `"windows"` without `"windows server"` as a
    workstation. Every Windows server went through that path as a workstation
    and came out "not in backup scope", which at a servers-only site is the
    opposite of right: the one machine whose missing backup matters most was
    the one the page excused.
    """
    await _set_scope(session_maker, world, "servers")
    await _set_class(session_maker, world, "WINDOWS_SERVER", "Windows")

    payload = await read_facts(session_maker, world, linked=False)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "not_backed_up"
    assert "backup_none" in kinds(payload)


async def test_a_workstation_at_a_servers_only_site_is_still_out_of_scope(
    world: dict[str, Any],
    cove: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The other half of the same decision, so the fix is not "everything is a
    server now": a laptop at a customer who backs up servers is not a gap."""
    await _set_scope(session_maker, world, "servers")
    await _set_class(session_maker, world, "WINDOWS_WORKSTATION", "Windows")

    payload = await read_facts(session_maker, world, linked=False)
    facts = by_id(payload)
    assert facts["backup.status"]["value"] == "not_in_scope"
    assert "servers only at this site" in facts["backup.status"]["detail"]

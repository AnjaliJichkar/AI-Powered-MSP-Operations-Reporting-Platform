"""The coverage endpoints against the real application and a real database.

The vendors are not here: `compute_coverage` is replaced with a board built
from plain records, so what is tested is everything around the join — who
may read and who may exclude, that an exclusion needs a reason at every
layer, the audit row it writes, the snapshot being one row a day, and that a
site of another client is a 404.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.coverage as coverage_api
import app.coverage.exclusions as exclusions_module
import app.coverage.snapshots as snapshots_module
from app.connectors.errors import ConnectorError
from app.coverage.compute import CoverageReport, ToolReport, summarise_sites
from app.coverage.core import (
    Exclusion,
    ManagedDevice,
    ToolEndpoint,
    ToolInventory,
    assess,
)
from app.coverage.serialize import report_from_json, report_to_json
from app.coverage.snapshots import new_gaps, store_snapshot
from app.models import AuditEventType, OrganisationKind, Role
from app.models.coverage import CoverageExclusion, CoverageSnapshot
from app.models.site import Site
from tests.api.conftest import Make, audit_rows, sign_in

pytestmark = pytest.mark.usefixtures("clean")

NOW = dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC)


@pytest.fixture
async def world(
    make_client: Make,
    make_org: Make,
    make_user: Make,
    session_maker: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    acme = await make_client("acme-it", "Acme IT")
    beta = await make_client("beta-msp", "Beta MSP")
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    acme_org = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=acme)
    beta_org = await make_org(OrganisationKind.FRANCHISEE, "beta-people", client=beta)
    async with session_maker() as session:
        dental = Site(client_id=acme.id, slug="acme-dental", name="Acme Dental")
        legal = Site(client_id=beta.id, slug="beacon-legal", name="Beacon Legal")
        session.add_all([dental, legal])
        await session.commit()
        await session.refresh(dental)
        await session.refresh(legal)
    return {
        "acme": acme,
        "beta": beta,
        "dental": dental,
        "legal": legal,
        "staff": await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER),
        "acme_admin": await make_user(acme_org, "boss@acme.test", Role.FRANCHISEE_ADMIN),
        "acme_viewer": await make_user(acme_org, "viewer@acme.test", Role.FRANCHISEE_VIEWER),
        "beta_admin": await make_user(beta_org, "boss@beta.test", Role.FRANCHISEE_ADMIN),
    }


def fake_report(
    site: Site,
    *,
    exclusions: list[Exclusion] | None = None,
    now: dt.datetime = NOW,
    scanner_lost: str | None = None,
) -> CoverageReport:
    """Three computers at one site: one covered, one stale, one with no agent.

    GravityZone also holds GHOST (unmanaged), and RECEPTION twice over with two
    MACs: two machines of one name, which the CSV must print as two lines.
    ConnectSecure holds an external scan target beside its one asset.
    `scanner_lost` drops that device from ConnectSecure's inventory: the
    overnight change a snapshot comparison has to notice.
    """
    devices = [
        ManagedDevice(site.id, site.slug, site.name, "101", "WS-OK", "WINDOWS_WORKSTATION", now),
        ManagedDevice(site.id, site.slug, site.name, "102", "WS-OLD", "WINDOWS_WORKSTATION", now),
        ManagedDevice(site.id, site.slug, site.name, "103", "KIOSK", "WINDOWS_WORKSTATION", now),
    ]
    inventories = {
        "bitdefender": ToolInventory(
            endpoints=[
                ToolEndpoint(site.id, "WS-OK", "covered", "agent current, core protection on"),
                ToolEndpoint(site.id, "WS-OLD", "stale", "agent out of date"),
                ToolEndpoint(site.id, "GHOST", "covered", "agent current, core protection on"),
                ToolEndpoint(
                    site.id,
                    "RECEPTION",
                    "covered",
                    "agent current, core protection on",
                    external_id="bd-r1",
                    macs=frozenset({"aa0000000001"}),
                ),
                ToolEndpoint(
                    site.id,
                    "RECEPTION",
                    "covered",
                    "agent current, core protection on",
                    external_id="bd-r2",
                    macs=frozenset({"aa0000000002"}),
                ),
            ],
            sites=frozenset({site.id}),
        ),
        "connectsecure": ToolInventory(
            endpoints=[
                ToolEndpoint(site.id, name, "covered", "current")
                for name in ("WS-OK",)
                if name != scanner_lost
            ]
            + [
                ToolEndpoint(
                    site.id,
                    "203.0.113.9",
                    "covered",
                    "current",
                    external_id="cs-ext",
                    external="name is an IP address",
                )
            ],
            sites=frozenset({site.id}),
        ),
        "backup": None,
    }
    assessment = assess(
        devices, inventories, exclusions=exclusions or [], now=now, ninjaone_stale_days=14
    )
    tools = [
        ToolReport("bitdefender", "Endpoint protection", True, endpoints=3),
        ToolReport("connectsecure", "Vulnerability scanning", True, endpoints=1),
        ToolReport("backup", "Backup", False, reason="Cove is not connected yet."),
    ]
    return CoverageReport(
        client_id=site.client_id,
        computed_at=now,
        duration_ms=7,
        tools=tools,
        devices=assessment.devices,
        unmanaged=assessment.unmanaged,
        sites=summarise_sites(
            assessment, site_names={site.id: (site.slug, site.name)}, tools=tools
        ),
        exclusions=list(exclusions or []),
        not_computers=2,
        external=assessment.external,
    )


@pytest.fixture
async def board(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Each client has a stored board, as the collector or a refresh would leave it,
    and `compute_coverage` answers from records for a refresh, whichever client asks."""
    sites = {world["acme"].id: world["dental"], world["beta"].id: world["legal"]}
    async with session_maker() as session:
        for client_id, site in sites.items():
            await store_snapshot(
                session, client_id=client_id, report=fake_report(site), actor="cron"
            )
        await session.commit()

    async def compute(client_id: uuid.UUID, *, exclusions: Any = None, now: Any = None) -> Any:
        return fake_report(sites[client_id], exclusions=exclusions, now=now or NOW)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(snapshots_module, "compute_coverage", compute)
    monkeypatch.setattr(snapshots_module, "session_scope", scope)
    monkeypatch.setattr(exclusions_module, "session_scope", scope)
    monkeypatch.setattr(coverage_api.REFRESHES, "tasks", {})
    monkeypatch.setattr(coverage_api.REFRESHES, "status", {})


# --- reading -------------------------------------------------------------------


async def test_the_board_is_the_sites_worst_first_with_states_and_the_reverse(
    http: httpx.AsyncClient, world: dict[str, Any], board: None
) -> None:
    await sign_in(http, "viewer@acme.test")
    response = await http.get("/coverage/board")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["managed"] == 3 and data["unmanaged"] == 3 and data["not_computers"] == 2
    assert data["external"] == 1 and data["rmm_agent_down"] == 0
    assert data["sites"][0]["external"] == 1 and data["sites"][0]["unmanaged"] == 3
    assert [t["key"] for t in data["tools"]] == ["bitdefender", "connectsecure", "backup"]
    bitdefender = data["tools"][0]
    assert (bitdefender["covered"], bitdefender["stale"], bitdefender["absent"]) == (1, 1, 1)
    assert bitdefender["covered_percent"] == 33.3
    assert data["tools"][2]["assessed"] is False and "Cove" in data["tools"][2]["reason"]
    assert data["sites"][0]["site_slug"] == "acme-dental"
    assert data["sites"][0]["tools"]["connectsecure"]["absent"] == 2
    assert data["may_exclude"] is False  # a viewer reads
    # The board is the stored snapshot, and the page is told when and by whom.
    assert data["snapshot"]["taken_by"] == "cron" and data["snapshot"]["has_board"] is True
    assert data["snapshot"]["taken_on"] == "2026-09-23"
    # How old the board is, in words, with the server's own definition beside
    # it. A date alone reads as ordinary: a board three weeks old reported no
    # endpoint protection anywhere on the demo tenant while the Devices page,
    # which reads the resolved links, had the same machines covered.
    assert data["snapshot"]["age_days"] >= 1 and data["snapshot"]["old"] is True
    assert data["snapshot"]["age_words"].endswith("old")
    assert "Refresh recomputes it" in data["snapshot"]["age_definition"]
    assert data["refresh"]["running"] is False
    # One snapshot only: every gap is new, and the page is told why.
    assert data["new_gaps_since"] is None
    assert len(data["new_gaps"]) == 4  # WS-OLD stale, KIOSK absent (bd); WS-OLD, KIOSK absent (cs)

    devices = (await http.get("/coverage/sites/acme-dental")).json()
    assert [d["device"] for d in devices["devices"]] == ["KIOSK", "WS-OLD", "WS-OK"]  # gaps first
    assert devices["devices"][1]["tools"]["bitdefender"]["detail"] == "agent out of date"
    assert devices["unmanaged"][0] == {
        "tool": "bitdefender",
        "device": "GHOST",
        "detail": "agent current, core protection on",
        "last_seen": None,
        "note": None,
        "vendor_ids": [],
        "rows": 1,
    }
    # Two machines of one name are two entries, each with its own vendor id.
    assert [(u["device"], u["vendor_ids"]) for u in devices["unmanaged"][1:]] == [
        ("RECEPTION", ["bd-r1"]),
        ("RECEPTION", ["bd-r2"]),
    ]
    assert [(u["device"], u["note"]) for u in devices["external"]] == [
        ("203.0.113.9", "name is an IP address")
    ]

    csv_text = (await http.get("/coverage/export.csv")).text
    lines = csv_text.splitlines()
    assert lines[0].startswith("site,device,device_id,node_class,ninjaone_state")
    assert lines[0].endswith(",vendor_id,note")
    assert "KIOSK" in csv_text and "GHOST" in csv_text and "unmanaged" in csv_text
    # One line per vendor record, never one per name.
    reception = [line for line in lines if ",RECEPTION," in line]
    assert len(reception) == 2
    assert {line.split(",")[-2] for line in reception} == {"bd-r1", "bd-r2"}
    external_lines = [line for line in lines if ",203.0.113.9," in line]
    assert len(external_lines) == 1 and ",external," in external_lines[0]
    assert external_lines[0].endswith(",cs-ext,name is an IP address")


async def test_another_clients_site_is_a_404_and_the_page_is_in_the_nav(
    http: httpx.AsyncClient, world: dict[str, Any], board: None
) -> None:
    await sign_in(http, "boss@acme.test")
    assert (await http.get("/coverage/sites/beacon-legal")).status_code == 404
    page = await http.get("/coverage", headers={"Accept": "text/html"})
    assert page.status_code == 200 and 'href="/coverage"' in page.text


# --- excluding -----------------------------------------------------------------


async def test_an_exclusion_needs_a_reason_at_every_layer(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    board: None,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    admin = await sign_in(http, "boss@acme.test")
    body = {
        "site_slug": "acme-dental",
        "device_id": "103",
        "device": "KIOSK",
        "tool": "connectsecure",
    }

    # The API model: no reason at all.
    assert (await admin.post("/coverage/exclusions", json=body)).status_code == 422
    # The service: a reason made of whitespace.
    refused = await admin.post("/coverage/exclusions", json={**body, "reason": "   "})
    assert refused.status_code == 422 and "needs a reason" in refused.json()["detail"]
    # The database itself, with the API and the service gone around.
    async with session_maker() as session:
        session.add(
            CoverageExclusion(
                client_id=world["acme"].id,
                site_id=world["dental"].id,
                device_id="103",
                device_name="KIOSK",
                tool="connectsecure",
                reason="  ",
                created_by_user_id=world["acme_admin"].id,
                created_by="boss@acme.test",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
    async with session_maker() as session:
        assert (await session.execute(select(CoverageExclusion))).scalars().all() == []


async def test_an_exclusion_is_recorded_with_the_user_shown_on_the_board_and_audited(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    board: None,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    admin = await sign_in(http, "boss@acme.test")
    made = await admin.post(
        "/coverage/exclusions",
        json={
            "site_slug": "acme-dental",
            "device_id": "103",
            "device": "KIOSK",
            "tool": "connectsecure",
            "reason": "Lobby kiosk: no agent by design",
        },
    )
    assert made.status_code == 201, made.text
    row = made.json()
    assert row["created_by"] == "boss@acme.test" and row["created_at"]

    # The device is still on the board, as excluded, with the reason; not gone.
    devices = (await http.get("/coverage/sites/acme-dental")).json()
    kiosk = next(d for d in devices["devices"] if d["device"] == "KIOSK")
    assert kiosk["tools"]["connectsecure"]["state"] == "excluded"
    assert kiosk["tools"]["connectsecure"]["detail"] == "Lobby kiosk: no agent by design"
    assert kiosk["tools"]["connectsecure"]["exclusion_id"] == row["id"]
    assert kiosk["tools"]["bitdefender"]["state"] == "absent"  # one tool, not all
    listed = (await http.get("/coverage/exclusions")).json()
    assert [e["device"] for e in listed] == ["KIOSK"]

    # A second exclusion of the same device and tool is refused, not doubled.
    again = await admin.post(
        "/coverage/exclusions",
        json={
            "site_slug": "acme-dental",
            "device_id": "103",
            "device": "KIOSK",
            "tool": "connectsecure",
            "reason": "again",
        },
    )
    assert again.status_code == 422 and "already excluded" in again.json()["detail"]

    # The audit row names everything.
    rows = await audit_rows(session_maker, event_type=AuditEventType.COVERAGE_EXCLUDED)
    assert len(rows) == 1
    assert rows[0].actor_user_id == world["acme_admin"].id
    assert rows[0].client_id == world["acme"].id
    assert rows[0].payload["device"] == "KIOSK"
    assert rows[0].payload["tool"] == "connectsecure"
    assert rows[0].payload["reason"] == "Lobby kiosk: no agent by design"

    # Withdrawing keeps the row and writes a second audit row.
    gone = await http.delete(f"/coverage/exclusions/{row['id']}", headers=admin.headers())
    assert gone.status_code == 200 and gone.json()["removed_by"] == "boss@acme.test"
    assert (await http.get("/coverage/exclusions")).json() == []
    kept = (await http.get("/coverage/exclusions?include_removed=true")).json()
    assert len(kept) == 1 and kept[0]["removed_at"]
    rows = await audit_rows(session_maker, event_type=AuditEventType.COVERAGE_EXCLUDED)
    assert [r.payload["action"] for r in rows] == ["excluded", "withdrawn"]


async def test_who_may_exclude(http: httpx.AsyncClient, world: dict[str, Any], board: None) -> None:
    body = {
        "site_slug": "acme-dental",
        "device_id": "102",
        "device": "WS-OLD",
        "tool": "bitdefender",
        "reason": "Being rebuilt this week",
    }
    # A viewer reads the board and may not change it.
    viewer = await sign_in(http, "viewer@acme.test")
    assert (await http.get("/coverage/board")).status_code == 200
    assert (await viewer.post("/coverage/exclusions", json=body)).status_code == 403
    # Another client's admin cannot reach Acme's site at all.
    beta = await sign_in(http, "boss@beta.test")
    assert (await beta.post("/coverage/exclusions", json=body)).status_code == 404
    # Inputiv staff can, on the client they switched to.
    staff = await sign_in(http, "engineer@inputiv.test")
    assert (await staff.post("/coverage/exclusions", json=body)).status_code == 409  # no client
    assert (await staff.post("/auth/switch-client", json={"client": "acme-it"})).status_code == 200
    made = await staff.post("/coverage/exclusions", json=body)
    assert made.status_code == 201, made.text
    assert made.json()["created_by"] == "engineer@inputiv.test"
    # And a franchisee admin can withdraw what staff recorded on their client.
    admin = await sign_in(http, "boss@acme.test")
    assert (
        await http.delete(f"/coverage/exclusions/{made.json()['id']}", headers=admin.headers())
    ).status_code == 200


# --- snapshots -----------------------------------------------------------------


async def test_a_snapshot_is_one_row_a_day_and_says_what_is_new(
    world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    client_id = world["acme"].id
    report = fake_report(world["dental"])
    async with session_maker() as session:
        first = await store_snapshot(session, client_id=client_id, report=report, actor="cron")
        await session.commit()
    # The same day again: replaced, not doubled.
    later = fake_report(world["dental"], now=NOW + dt.timedelta(hours=6))
    async with session_maker() as session:
        second = await store_snapshot(session, client_id=client_id, report=later, actor="cron")
        await session.commit()
        query = select(CoverageSnapshot).where(CoverageSnapshot.client_id == client_id)
        rows = (await session.execute(query)).scalars().all()
    assert len(rows) == 1 and first.id == second.id
    assert rows[0].taken_at == later.computed_at
    assert rows[0].summary["managed"] == 3 and len(rows[0].gaps) == 4

    # Tomorrow, with the kiosk excluded: its two gaps are gone, nothing is new.
    tomorrow = NOW + dt.timedelta(days=1)
    excluded = [
        Exclusion("103", "connectsecure", "kiosk", "boss", NOW),
        Exclusion("103", "bitdefender", "kiosk", "boss", NOW),
    ]
    fresh, since = new_gaps(
        fake_report(world["dental"], exclusions=excluded, now=tomorrow), rows[0]
    )
    assert fresh == [] and since == "2026-09-23"
    # And a device the scanner loses overnight is exactly the one new gap.
    fresh, _ = new_gaps(fake_report(world["dental"], now=tomorrow, scanner_lost="WS-OK"), rows[0])
    assert fresh == [
        {
            "site": "acme-dental",
            "device_id": "101",
            "device": "WS-OK",
            "tool": "connectsecure",
            "state": "absent",
        }
    ]


# --- the stored board and the refresh ---------------------------------------------


async def test_a_client_with_no_snapshot_gets_an_empty_board_and_a_refresh_computes_one(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    board: None,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    # Take the stored board away: the page says so rather than computing.
    async with session_maker() as session:
        await session.execute(
            delete(CoverageSnapshot).where(CoverageSnapshot.client_id == world["acme"].id)
        )
        await session.commit()
    viewer = await sign_in(http, "viewer@acme.test")
    empty = (await http.get("/coverage/board")).json()
    assert empty["snapshot"] is None and empty["sites"] == [] and empty["managed"] == 0
    assert (await http.get("/coverage/sites/acme-dental")).status_code == 404
    assert (await http.get("/coverage/export.csv")).status_code == 404
    # A viewer may not refresh; the status is readable by anyone on the client.
    assert (await viewer.post("/coverage/refresh")).status_code == 403
    assert (await http.get("/coverage/refresh")).json()["running"] is False

    admin = await sign_in(http, "boss@acme.test")
    started = await admin.post("/coverage/refresh")
    assert started.status_code == 202, started.text
    assert started.json()["started"] is True and started.json()["started_by"] == "boss@acme.test"
    # A second request while one runs joins it rather than starting another.
    again = await admin.post("/coverage/refresh")
    assert again.status_code == 202 and again.json()["started"] is False
    await coverage_api.REFRESHES.wait(world["acme"].id)
    status = (await http.get("/coverage/refresh")).json()
    assert status["running"] is False and status["error"] is None
    assert status["duration_ms"] is not None

    data = (await http.get("/coverage/board")).json()
    assert data["snapshot"]["taken_by"] == "boss@acme.test"
    assert data["managed"] == 3 and data["sites"][0]["site_slug"] == "acme-dental"
    rows = await audit_rows(session_maker, event_type=AuditEventType.COVERAGE_SNAPSHOT)
    assert len(rows) == 1
    assert rows[0].actor_user_id == world["acme_admin"].id
    assert rows[0].payload["managed"] == 3
    # Beta's board was never touched: a refresh is per client.
    await sign_in(http, "boss@beta.test")
    assert (await http.get("/coverage/board")).json()["snapshot"]["taken_by"] == "cron"


async def test_a_failed_refresh_is_reported_on_the_page_not_raised(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    board: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(client_id: uuid.UUID, **kwargs: Any) -> Any:
        raise ConnectorError("vendor down", connector="bitdefender")

    monkeypatch.setattr(snapshots_module, "compute_coverage", broken)
    admin = await sign_in(http, "boss@acme.test")
    assert (await admin.post("/coverage/refresh")).status_code == 202
    await coverage_api.REFRESHES.wait(world["acme"].id)
    status = (await http.get("/coverage/refresh")).json()
    assert status["running"] is False and "vendor down" in status["error"]
    # The stored board still serves.
    assert (await http.get("/coverage/board")).json()["snapshot"]["taken_by"] == "cron"


async def test_an_exclusion_made_after_the_snapshot_shows_without_a_refresh(
    http: httpx.AsyncClient, world: dict[str, Any], board: None
) -> None:
    """The board is stored without exclusions and the live ones are laid over it."""
    admin = await sign_in(http, "boss@acme.test")
    made = await admin.post(
        "/coverage/exclusions",
        json={
            "site_slug": "acme-dental",
            "device_id": "102",
            "device": "WS-OLD",
            "tool": "bitdefender",
            "reason": "Being retired",
        },
    )
    assert made.status_code == 201
    data = (await http.get("/coverage/board")).json()
    assert data["snapshot"]["taken_by"] == "cron"  # no recompute happened
    assert data["tools"][0]["excluded"] == 1 and data["tools"][0]["stale"] == 0
    devices = (await http.get("/coverage/sites/acme-dental")).json()
    old = next(d for d in devices["devices"] if d["device"] == "WS-OLD")
    assert old["tools"]["bitdefender"]["state"] == "excluded"
    assert old["tools"]["bitdefender"]["detail"] == "Being retired"
    # Withdrawn: the stored verdict is back, still without a recompute.
    await http.delete(f"/coverage/exclusions/{made.json()['id']}", headers=admin.headers())
    devices = (await http.get("/coverage/sites/acme-dental")).json()
    old = next(d for d in devices["devices"] if d["device"] == "WS-OLD")
    assert old["tools"]["bitdefender"]["state"] == "stale"


def test_the_board_survives_the_round_trip_through_json(world: dict[str, Any]) -> None:
    report = fake_report(world["dental"])
    payload = report_to_json(report)
    back = report_from_json(payload)
    assert [s.as_dict() for s in back.sites] == [s.as_dict() for s in report.sites]
    assert {d.device.device_id: {k: v.state for k, v in d.tools.items()} for d in back.devices} == {
        d.device.device_id: {k: v.state for k, v in d.tools.items()} for d in report.devices
    }
    assert [(u.name, u.external_ids) for u in back.unmanaged] == [
        (u.name, u.external_ids) for u in report.unmanaged
    ]
    assert [u.name for u in back.external] == [u.name for u in report.external]
    assert back.site_names == {s.site_id: (s.slug, s.name) for s in report.sites}


def test_the_boards_age_is_words_a_person_would_say() -> None:
    """Three wordings, because this line is read rather than parsed.

    "taken today" and "19 days old" are the two ends somebody acts on; a bare
    date between them is what let a three-week-old board sit on the page
    looking ordinary while it disagreed with every other page about the fleet.
    """
    from app.api.coverage import _age_words

    assert _age_words(0) == "taken today"
    assert _age_words(1) == "a day old"
    assert _age_words(19) == "19 days old"

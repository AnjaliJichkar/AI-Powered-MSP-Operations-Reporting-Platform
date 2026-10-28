"""The Devices page's endpoints against the real application and a real database.

No vendor is called: the identity tables are seeded by hand and the four
tools the detail view reaches through are replaced with fakes that record
what they were asked. What is tested is everything around them — who may
read, who may see the queue and link, that a device of another client is a
404, that a link needs a reason and writes an audit row naming the user, and
that the link changes what the identity reader says on the next computation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.devices as devices_api
from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.scope import SiteRef
from app.coverage.core import ToolEndpoint, ToolInventory
from app.devices.identity import assess_from_identity, load_identity
from app.models import AuditEventType, OrganisationKind, Role
from app.models.device import Device, DeviceAgreement, DeviceUnresolved, DeviceVendorLink
from app.models.enums import ConnectorName
from app.models.site import Site
from tests.api.conftest import Make, audit_rows, sign_in

pytestmark = pytest.mark.usefixtures("clean")

NOW = dt.datetime(2026, 9, 24, 12, tzinfo=dt.UTC)
EARLIER = NOW - dt.timedelta(days=30)


def _device(client_id: uuid.UUID, site_id: uuid.UUID, name: str, status: str = "active") -> Device:
    return Device(
        id=uuid.uuid4(),
        client_id=client_id,
        site_id=site_id,
        canonical_name=name,
        netbios_key=name.lower()[:15],
        serials=[],
        macs=[],
        node_class="WINDOWS_WORKSTATION",
        os="Windows 11",
        status=status,
        first_seen_at=EARLIER,
        last_seen_at=NOW,
    )


def _link(
    device: Device, connector: ConnectorName, external_id: str, matched_by: str, name: str
) -> DeviceVendorLink:
    return DeviceVendorLink(
        client_id=device.client_id,
        device_id=device.id,
        connector_name=connector,
        external_id=external_id,
        vendor_name=name,
        matched_by=matched_by,
        first_seen_at=EARLIER,
        last_seen_at=NOW,
        active=True,
    )


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
        await session.flush()
        # Acme Dental: a fully paired machine, one NinjaOne has lost, one with
        # no Bitdefender record whose GravityZone row is in the queue, and a
        # machine only Bitdefender knows.
        ws_ok = _device(acme.id, dental.id, "WS-OK")
        ws_old = _device(acme.id, dental.id, "WS-OLD", status="rmm_stale")
        kiosk = _device(acme.id, dental.id, "KIOSK")
        ghost = _device(acme.id, dental.id, "GHOST")
        legal_01 = _device(beta.id, legal.id, "LEGAL-01")
        session.add_all([ws_ok, ws_old, kiosk, ghost, legal_01])
        await session.flush()
        session.add_all(
            [
                _link(ws_ok, ConnectorName.NINJAONE, "101", "ninjaone", "WS-OK"),
                _link(ws_ok, ConnectorName.BITDEFENDER, "bd-1", "exact", "WS-OK"),
                _link(ws_ok, ConnectorName.CONNECTSECURE, "cs-1", "netbios15", "ws-ok.acme.local"),
                _link(ws_old, ConnectorName.NINJAONE, "102", "ninjaone", "WS-OLD"),
                _link(kiosk, ConnectorName.NINJAONE, "103", "ninjaone", "KIOSK"),
                _link(ghost, ConnectorName.BITDEFENDER, "bd-ghost", "vendor_only", "GHOST"),
                _link(legal_01, ConnectorName.NINJAONE, "901", "ninjaone", "LEGAL-01"),
                DeviceUnresolved(
                    client_id=acme.id,
                    site_id=dental.id,
                    connector_name=ConnectorName.BITDEFENDER,
                    external_id="bd-2",
                    vendor_name="KIOSK",
                    found_by="mac",
                    candidates=[
                        {"device_id": str(kiosk.id), "name": "KIOSK"},
                        {"device_id": str(ws_old.id), "name": "WS-OLD"},
                    ],
                    first_seen_at=EARLIER,
                    last_seen_at=NOW,
                ),
                DeviceAgreement(
                    client_id=acme.id,
                    computed_at=NOW,
                    agree=False,
                    sites_compared=1,
                    summary={"sites": 1, "managed": [3, 3], "unmanaged": [1, 2]},
                    differences=[
                        {
                            "site_slug": "acme-dental",
                            "site": "Acme Dental",
                            "field": "unmanaged",
                            "off": 1,
                            "on": 2,
                        }
                    ],
                    actor="collector",
                ),
            ]
        )
        await session.commit()
        ids = {
            "ws_ok": ws_ok.id,
            "ws_old": ws_old.id,
            "kiosk": kiosk.id,
            "ghost": ghost.id,
            "legal_01": legal_01.id,
        }
    return {
        "acme": acme,
        "beta": beta,
        "dental": dental,
        "legal": legal,
        **ids,
        "staff": await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER),
        "acme_admin": await make_user(acme_org, "boss@acme.test", Role.FRANCHISEE_ADMIN),
        "acme_viewer": await make_user(acme_org, "viewer@acme.test", Role.FRANCHISEE_VIEWER),
        "beta_admin": await make_user(beta_org, "boss@beta.test", Role.FRANCHISEE_ADMIN),
    }


class _Answer(BaseModel):
    """What a fake tool returns: enough shape for the page, and what it was asked."""

    asked: str
    site_id: str | None = None
    found: bool = True
    name: str | None = None
    agent_installed: bool = True
    counts_by_severity: dict[str, int] = {}
    vulnerabilities: list[dict[str, Any]] = []
    total: int = 0
    tickets: list[dict[str, Any]] = []


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str | None]]:
    """The four tools replaced with fakes that record (tool, name asked, site)."""
    calls: list[tuple[str, str, str | None]] = []

    def fake(tool: str, *, fail: bool = False) -> Any:
        async def call(client_id: str, device_name: str, *args: Any, **kwargs: Any) -> _Answer:
            site = kwargs.get("site_id") or (args[0] if args else None)
            calls.append((tool, device_name, site))
            if fail:
                raise ValueError(f"no such record for {device_name!r}")
            return _Answer(asked=device_name, site_id=site, name=device_name)

        return call

    monkeypatch.setattr(devices_api, "get_device", fake("get_device"))
    monkeypatch.setattr(devices_api, "get_endpoint_protection", fake("get_endpoint_protection"))
    monkeypatch.setattr(
        devices_api, "get_asset_vulnerabilities", fake("get_asset_vulnerabilities", fail=True)
    )
    monkeypatch.setattr(devices_api, "search_tickets", fake("search_tickets"))
    return calls


# --- reading, and who sees what -----------------------------------------------------


async def test_everyone_reads_the_list_with_its_filters_and_the_csv(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    await sign_in(http, "viewer@acme.test")
    response = await http.get("/devices/list")
    assert response.status_code == 200, response.text
    data = response.json()
    assert [d["device"] for d in data["devices"]] == ["GHOST", "KIOSK", "WS-OK", "WS-OLD"]
    assert data["counts"] == {
        "total": 4,
        "shown": 4,
        "by_status": {"active": 3, "rmm_stale": 1, "rmm_agent_down": 0, "retired": 0},
    }
    assert data["may_link"] is False and data["resolved_at"] is not None
    # The order the page shows them in. Cove joined when backup coverage did.
    assert [v["key"] for v in data["vendors"]] == [
        "ninjaone",
        "bitdefender",
        "connectsecure",
        "cove",
    ]
    ws_ok = next(d for d in data["devices"] if d["device"] == "WS-OK")
    assert ws_ok["vendors"]["connectsecure"] == {
        "present": True,
        "links": 1,
        "last_seen": ws_ok["vendors"]["ninjaone"]["last_seen"],
        "matched_by": ["netbios15"],
        "names": ["ws-ok.acme.local"],
    }
    ghost = next(d for d in data["devices"] if d["device"] == "GHOST")
    assert ghost["vendors"]["ninjaone"]["present"] is False
    assert ghost["vendors"]["bitdefender"]["matched_by"] == ["vendor_only"]

    # The three filters, and a value the filter does not know.
    stale = (await http.get("/devices/list?status=rmm_stale")).json()
    assert [d["device"] for d in stale["devices"]] == ["WS-OLD"] and stale["counts"]["total"] == 4
    missing = (await http.get("/devices/list?missing=bitdefender")).json()
    assert [d["device"] for d in missing["devices"]] == ["KIOSK", "WS-OLD"]
    by_site = (await http.get("/devices/list?site=acme-dental&missing=ninjaone")).json()
    assert [d["device"] for d in by_site["devices"]] == ["GHOST"]
    assert (await http.get("/devices/list?status=lost")).status_code == 422
    assert (await http.get("/devices/list?missing=autotask")).status_code == 422

    csv_text = (await http.get("/devices/export.csv?missing=bitdefender")).text
    lines = csv_text.splitlines()
    assert lines[0].startswith("site,device,device_id,status,node_class,os,first_seen,last_seen")
    assert "ninjaone_present" in lines[0] and "connectsecure_matched_by" in lines[0]
    assert [line.split(",")[1] for line in lines[1:]] == ["KIOSK", "WS-OLD"]
    assert "GHOST" not in csv_text

    page = await http.get("/devices", headers={"Accept": "text/html"})
    assert page.status_code == 200 and 'href="/devices"' in page.text
    assert "queue-card" not in page.text  # a franchisee is not shown the queue


async def test_a_device_is_read_through_the_tools_by_each_vendors_own_name(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    tools: list[tuple[str, str, str | None]],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await sign_in(http, "viewer@acme.test")
    response = await http.get(f"/devices/{world['ws_ok']}")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["device"]["device"] == "WS-OK" and data["may_link"] is False
    assert [(link["connector"], link["matched_by"]) for link in data["links"]] == [
        ("ninjaone", "ninjaone"),
        ("bitdefender", "exact"),
        ("connectsecure", "netbios15"),
    ]
    # Each vendor was asked by the name its own link carries, within the site;
    # Autotask by the canonical name. The scanner's error is the answer shown.
    site = str(world["dental"].id)
    assert tools == [
        ("get_device", "WS-OK", site),
        ("get_endpoint_protection", "WS-OK", site),
        ("get_asset_vulnerabilities", "ws-ok.acme.local", site),
        ("search_tickets", "WS-OK", site),
    ]
    assert data["vendors"]["ninjaone"]["available"] is True
    assert data["vendors"]["bitdefender"]["data"]["asked"] == "WS-OK"
    assert data["vendors"]["connectsecure"]["available"] is False
    assert "no such record" in data["vendors"]["connectsecure"]["error"]
    assert data["vendors"]["autotask"]["available"] is True
    # Every read is a tool_call audit row for this user, on this client.
    rows = await audit_rows(session_maker, event_type=AuditEventType.TOOL_CALL)
    assert [(r.tool_name, r.outcome.value) for r in rows] == [
        ("get_device", "success"),
        ("get_endpoint_protection", "success"),
        ("get_asset_vulnerabilities", "error"),
        ("search_tickets", "success"),
    ]
    assert {r.actor_user_id for r in rows} == {world["acme_viewer"].id}
    assert {r.client_id for r in rows} == {world["acme"].id}

    # A vendor with no active link is not asked at all; a queued record shows.
    tools.clear()
    kiosk = (await http.get(f"/devices/{world['kiosk']}")).json()
    assert kiosk["vendors"]["bitdefender"] == {"available": False, "linked": False}
    assert kiosk["vendors"]["connectsecure"] == {"available": False, "linked": False}
    assert [t[0] for t in tools] == ["get_device", "search_tickets"]
    assert [(q["connector"], q["external_id"], q["found_by"]) for q in kiosk["queued"]] == [
        ("bitdefender", "bd-2", "mac")
    ]


async def test_the_queue_the_agreement_and_the_writes_are_staff_only(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    body = {
        "device_id": str(world["kiosk"]),
        "connector": "bitdefender",
        "external_id": "bd-2",
        "reason": "same MAC, the kiosk was reimaged",
    }
    for email in ("viewer@acme.test", "boss@acme.test"):
        user = await sign_in(http, email)
        assert (await http.get("/devices/unresolved")).status_code == 403
        assert (await http.get("/devices/agreement")).status_code == 403
        assert (await user.post("/devices/links", json=body)).status_code == 403
        assert (await user.post("/devices/links/1/unlink", json={"reason": "x"})).status_code == 403
    # Inputiv staff: nothing without a client, everything once switched.
    staff = await sign_in(http, "engineer@inputiv.test")
    assert (await http.get("/devices/unresolved")).status_code == 409
    assert (await staff.post("/auth/switch-client", json={"client": "acme-it"})).status_code == 200
    queue = (await http.get("/devices/unresolved")).json()
    assert queue["count"] == 1
    entry = queue["entries"][0]
    assert (entry["connector"], entry["external_id"], entry["found_by"]) == (
        "bitdefender",
        "bd-2",
        "mac",
    )
    assert [(c["device"], c["status"], c["known"]) for c in entry["candidates"]] == [
        ("KIOSK", "active", True),
        ("WS-OLD", "rmm_stale", True),
    ]
    agreement = (await http.get("/devices/agreement")).json()
    assert agreement["identity_enabled"] is False
    assert agreement["latest"]["agree"] is False
    assert agreement["latest"]["sites_differing"] == ["acme-dental"]
    assert agreement["latest"]["differences"][0]["field"] == "unmanaged"
    page = await http.get("/devices", headers={"Accept": "text/html"})
    assert "queue-card" in page.text and 'id="agreement"' in page.text
    assert (await http.get("/devices/list")).json()["may_link"] is True


# --- scope ------------------------------------------------------------------------


async def test_another_clients_devices_links_and_queue_are_out_of_reach(
    http: httpx.AsyncClient, world: dict[str, Any], tools: list[tuple[str, str, str | None]]
) -> None:
    await sign_in(http, "boss@beta.test")
    listed = (await http.get("/devices/list")).json()
    assert [d["device"] for d in listed["devices"]] == ["LEGAL-01"]
    assert [s["slug"] for s in listed["sites"]] == ["beacon-legal"]
    assert (await http.get(f"/devices/{world['ws_ok']}")).status_code == 404
    assert tools == []  # no vendor was asked about a device that is not theirs
    assert "WS-OK" not in (await http.get("/devices/export.csv")).text
    # Staff switched to Beta cannot reach Acme's device, link or queue entry either.
    staff = await sign_in(http, "engineer@inputiv.test")
    await staff.post("/auth/switch-client", json={"client": "beta-msp"})
    assert (await http.get(f"/devices/{world['kiosk']}")).status_code == 404
    refused = await staff.post(
        "/devices/links",
        json={
            "device_id": str(world["kiosk"]),
            "connector": "bitdefender",
            "external_id": "bd-2",
            "reason": "trying from the wrong client",
        },
    )
    assert refused.status_code == 404
    assert (await http.get("/devices/unresolved")).json()["count"] == 0
    assert (await staff.post("/devices/links/1/unlink", json={"reason": "x"})).status_code == 404
    assert (await http.get("/devices/agreement")).json()["latest"] is None


# --- linking from the page ------------------------------------------------------------


def _ninja_rows(world: dict[str, Any]) -> list[tuple[SiteRef, NinjaDevice]]:
    site = SiteRef(world["dental"].id, "acme-dental", "Acme Dental", "42")
    return [
        (
            site,
            NinjaDevice.model_validate(
                {
                    "id": ninja_id,
                    "systemName": name,
                    "nodeClass": "WINDOWS_WORKSTATION",
                    "lastContact": NOW.timestamp(),
                }
            ),
        )
        for ninja_id, name in ((101, "WS-OK"), (102, "WS-OLD"), (103, "KIOSK"))
    ]


def _bitdefender(world: dict[str, Any]) -> ToolInventory:
    site_id = world["dental"].id
    return ToolInventory(
        endpoints=[
            ToolEndpoint(site_id, "WS-OK", "covered", "agent current", external_id="bd-1"),
            ToolEndpoint(site_id, "KIOSK-NEW", "covered", "agent current", external_id="bd-2"),
            ToolEndpoint(site_id, "GHOST", "covered", "agent current", external_id="bd-ghost"),
        ],
        sites=frozenset({site_id}),
    )


async def _kiosk_through_identity(
    session_maker: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> tuple[str, str | None, int]:
    """KIOSK's Bitdefender verdict read through the links, and the unmanaged count."""
    async with session_maker() as session:
        view = await load_identity(session, world["acme"].id)
    assessment = assess_from_identity(
        view,
        _ninja_rows(world),
        {"bitdefender": _bitdefender(world)},
        now=NOW,
        ninjaone_stale_days=14,
    )
    kiosk = next(d for d in assessment.devices if d.device.name == "KIOSK")
    state = kiosk.tools["bitdefender"]
    return state.state, state.matched_by, len(assessment.unmanaged)


async def test_a_link_from_the_page_needs_a_reason_is_audited_and_changes_coverage(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    # Before: the record is queued, so the device is unmatched and the row unmanaged.
    assert await _kiosk_through_identity(session_maker, world) == ("unmatched", None, 2)

    staff = await sign_in(http, "engineer@inputiv.test")
    await staff.post("/auth/switch-client", json={"client": "acme-it"})
    body = {
        "device_id": str(world["kiosk"]),
        "connector": "bitdefender",
        "external_id": "bd-2",
        "vendor_name": "KIOSK-NEW",
    }
    assert (await staff.post("/devices/links", json=body)).status_code == 422  # no reason
    blank = await staff.post("/devices/links", json={**body, "reason": "   "})
    assert blank.status_code == 422 and "reason is required" in blank.json()["detail"]
    assert (
        await staff.post("/devices/links", json={**body, "reason": "x", "client_id": "other"})
    ).status_code == 422  # the client comes from the session, never the body
    made = await staff.post(
        "/devices/links", json={**body, "reason": "same MAC; the kiosk was reimaged in June"}
    )
    assert made.status_code == 201, made.text
    link = made.json()
    assert link["matched_by"] == "manual" and link["active"] is True
    assert link["device_id"] == str(world["kiosk"])

    # The queue entry is closed, the device shows the link, and the audit row
    # names the user, the client, the device, the record and the reason.
    assert (await http.get("/devices/unresolved")).json()["count"] == 0
    rows = await audit_rows(session_maker, event_type=AuditEventType.DEVICE_LINKED)
    assert len(rows) == 1
    assert rows[0].actor_user_id == world["staff"].id
    assert rows[0].client_id == world["acme"].id
    assert rows[0].interface.value == "api"
    assert rows[0].payload["action"] == "linked"
    assert rows[0].payload["device_id"] == str(world["kiosk"])
    assert rows[0].payload["external_id"] == "bd-2"
    assert rows[0].payload["reason"] == "same MAC; the kiosk was reimaged in June"

    # After: the next computation through the links pairs the kiosk with its
    # row, by hand, and the row is no longer unmanaged.
    assert await _kiosk_through_identity(session_maker, world) == ("covered", "manual", 1)

    # A record active on another device is refused, not moved.
    taken = await staff.post(
        "/devices/links",
        json={
            "device_id": str(world["ws_old"]),
            "connector": "bitdefender",
            "external_id": "bd-2",
            "reason": "second thoughts",
        },
    )
    assert taken.status_code == 409 and "unlink it there first" in taken.json()["detail"]

    # Unlinking needs a reason too, keeps the row, queues the record again and
    # is audited; the next computation is back to unmatched.
    unlink_url = f"/devices/links/{link['link_id']}/unlink"
    assert (await staff.post(unlink_url, json={})).status_code == 422
    undone = await staff.post(unlink_url, json={"reason": "serial differs after all"})
    assert undone.status_code == 200, undone.text
    assert undone.json()["active"] is False and undone.json()["note"] == "serial differs after all"
    queue = (await http.get("/devices/unresolved")).json()
    assert [(e["external_id"], e["found_by"]) for e in queue["entries"]] == [("bd-2", "unlinked")]
    rows = await audit_rows(session_maker, event_type=AuditEventType.DEVICE_LINKED)
    assert [r.payload["action"] for r in rows] == ["linked", "unlinked"]
    assert await _kiosk_through_identity(session_maker, world) == ("unmatched", None, 2)
    async with session_maker() as session:
        kept = await session.get(DeviceVendorLink, link["link_id"])
        assert kept is not None and kept.matched_by == "manual" and not kept.active

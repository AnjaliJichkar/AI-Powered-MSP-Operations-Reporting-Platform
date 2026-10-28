"""The deck and the board read the same join: the same devices, the same counts.

The review's "no protection agent" and scan-coverage findings used to run
their own hostname join inside the collector. Now they run the board's
(`app/coverage/core.py`) on the same per-row verdicts. This feeds both paths
the same recorded vendor responses and asserts they name the same devices.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
import respx

from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.scope import SiteRef
from app.coverage.compute import compute_coverage
from app.coverage.config import ninjaone_population
from app.coverage.core import NOT_ONBOARDED, ToolEndpoint, ToolInventory, assess, external_target
from app.devices.identity import IdentityView, assess_from_identity
from app.models.device import Device, DeviceVendorLink
from app.models.enums import ConnectorName
from app.reports.tbr.security import (
    GROUP_COVERAGE_NINJA_ONLY,
    collect_bitdefender_endpoints,
    derive_coverage,
)
from app.settings import get_settings
from tests.reports.test_tbr_security import (
    collector,
    mock_bitdefender,
    mock_licence,
    mock_ninja,
    two_connectors,  # noqa: F401  the fixture
)


@respx.mock
async def test_the_review_and_the_board_count_the_same_unprotected_devices(
    two_connectors: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One threshold for both paths: the recorded devices last checked in weeks
    # before the fixed clock, and the review tests widen it the same way.
    monkeypatch.setenv("COVERAGE_RMM_STALE_DAYS", "365")
    get_settings.cache_clear()
    mock_ninja()
    mock_bitdefender()
    mock_licence()

    # The review's path: collect, then derive on one site's name groups.
    run = collector()
    await run._ninja_inventory()
    await collect_bitdefender_endpoints(run)
    derive_coverage(run)
    deck_absent = sorted(run.facts.groups[GROUP_COVERAGE_NINJA_ONLY])
    deck_matched = run.facts.facts["coverage.matched"].value
    deck_bd_only = run.facts.facts["coverage.bitdefender_only"].value

    # The board's path: the same recorded inventories through compute_coverage.
    report = await compute_coverage(uuid.uuid4(), exclusions=[], now=run.now)
    board_absent = sorted(
        d.device.name for d in report.devices if d.tools["bitdefender"].state == "absent"
    )
    board_matched = sum(
        1 for d in report.devices if d.tools["bitdefender"].state in ("covered", "stale")
    )
    board_bd_only = sum(1 for u in report.unmanaged if u.tool == "bitdefender")

    assert deck_absent == board_absent == ["TL-AUS-WS07"]
    assert deck_matched == board_matched == 3
    assert deck_bd_only == board_bd_only == 1
    # The same device is lost to NinjaOne on both (never contacted it), and on
    # both its current GravityZone row pairs with it: alive, RMM agent down,
    # rather than a stale device beside an unmanaged endpoint.
    assert run.facts.groups["ninjaone.rmm_stale"] == ["TL-AUS-MAC03"]
    assert [d.device.name for d in report.devices if d.ninjaone_state == "stale"] == [
        "TL-AUS-MAC03"
    ]
    assert {
        d.tools["bitdefender"].state for d in report.devices if d.device.name == "TL-AUS-MAC03"
    } == {"rmm_agent_down"}
    assert run.facts.facts["coverage.rmm_agent_down"].value == report.rmm_agent_down == 1
    assert report.rmm_stale == 0
    # And every pairing says which pass made it.
    assert {
        d.tools["bitdefender"].matched_by
        for d in report.devices
        if d.tools["bitdefender"].state in ("covered", "stale")
    } == {"exact"}
    # The population is the same rule on both sides: computers only, one of them RMM-stale.
    assert len(report.devices) == run.facts.facts["coverage.matched"].value + len(deck_absent) + 1
    # And a tool that is not configured is not a gap anywhere.
    assert report.tool("connectsecure").assessed is False
    assert {d.tools["connectsecure"].state for d in report.devices} == {"not_assessed"}


# --- the identity reader mirrors the join's new rules, with no database ---------------


def _links_from(
    client_id: uuid.UUID, assessment: Any, ninja_rows: list[Any], now: dt.datetime
) -> IdentityView:
    """What the resolution would write for this assessment: a device per NinjaOne
    row and a link per pairing, `matched_by` the pass. Nothing else."""
    devices: dict[uuid.UUID, Device] = {}
    links: list[DeviceVendorLink] = []
    for site, ninja in ninja_rows:
        device = Device(
            id=uuid.uuid5(client_id, str(ninja.id)),
            client_id=client_id,
            site_id=site.site_id,
            canonical_name=ninja.hostname,
            netbios_key=ninja.hostname.lower()[:15],
            serials=[],
            macs=[],
            status="active",
            first_seen_at=now,
            last_seen_at=now,
        )
        devices[device.id] = device
        links.append(
            DeviceVendorLink(
                client_id=client_id,
                device_id=device.id,
                connector_name=ConnectorName.NINJAONE,
                external_id=str(ninja.id),
                matched_by="ninjaone",
                first_seen_at=now,
                last_seen_at=now,
                active=True,
            )
        )
    for outcome in assessment.devices:
        for tool, verdict in outcome.tools.items():
            if verdict.matched_by is None:
                continue
            for external_id in verdict.endpoint_ids:
                links.append(
                    DeviceVendorLink(
                        client_id=client_id,
                        device_id=uuid.uuid5(client_id, outcome.device.device_id),
                        connector_name=ConnectorName(tool),
                        external_id=external_id,
                        matched_by=verdict.matched_by,
                        first_seen_at=now,
                        last_seen_at=now,
                        active=True,
                    )
                )
    return IdentityView(devices=devices, links=links, unresolved=[])


def test_the_identity_reader_agrees_with_the_join_on_collisions_lost_devices_and_externals() -> (
    None
):
    now = dt.datetime(2026, 9, 28, 12, tzinfo=dt.UTC)
    client_id = uuid.uuid4()
    site = SiteRef(site_id=uuid.uuid4(), slug="ata", name="Austin Tax", external_id="1")
    stale = (now - dt.timedelta(days=40)).timestamp()
    fresh = (now - dt.timedelta(days=1)).timestamp()
    ninja_rows = [
        (
            site,
            NinjaDevice(
                id=1,
                systemName="ATA-WORKSTATIO",
                dnsName="ATA-WORKSTATION1.ata.local",
                nodeClass="WINDOWS_WORKSTATION",
                lastContact=fresh,
            ),
        ),
        (
            site,
            NinjaDevice(
                id=2,
                systemName="ATA-WORKSTATIO",
                dnsName="ATA-WORKSTATION2.ata.local",
                nodeClass="WINDOWS_WORKSTATION",
                lastContact=fresh,
            ),
        ),
        (
            site,
            NinjaDevice(
                id=3,
                systemName="ATA-LOST",
                dnsName="ata-lost.ata.local",
                nodeClass="WINDOWS_WORKSTATION",
                lastContact=stale,
            ),
        ),
        (
            site,
            NinjaDevice(
                id=4, systemName="ATA-GONE", nodeClass="WINDOWS_WORKSTATION", lastContact=stale
            ),
        ),
        (
            site,
            NinjaDevice(
                id=5, systemName="ATA-TWIN", nodeClass="WINDOWS_WORKSTATION", lastContact=fresh
            ),
        ),
        (
            site,
            NinjaDevice(
                id=6, systemName="ATA-TWIN", nodeClass="WINDOWS_WORKSTATION", lastContact=fresh
            ),
        ),
    ]
    macs = {1: frozenset({"aa0000000001"}), 2: frozenset({"aa0000000002"})}
    serials = {3: "SN-LOST"}

    def row(name: str, external_id: str, **extra: Any) -> ToolEndpoint:
        return ToolEndpoint(
            site_id=site.site_id,
            name=name,
            state=extra.pop("state", "covered"),
            detail="current",
            external_id=external_id,
            external=external_target(name),
            **extra,
        )

    inventories = {
        "bitdefender": ToolInventory(
            endpoints=[
                row("ATA-WORKSTATIO", "b1", macs=frozenset({"aa0000000001"})),
                row("ATA-WORKSTATIO", "b2", macs=frozenset({"aa0000000002"})),
                row("ATA-TWIN", "b3"),
                row("DESKTOP-4K2J9QP", "b4"),
            ],
            sites=frozenset({site.site_id}),
        ),
        "connectsecure": ToolInventory(
            endpoints=[
                row("ATA-WORKSTATION1", "c1"),
                row("ATA-WORKSTATION2", "c2"),
                row("SOMETHING-ELSE", "c3", serial="SN-LOST", last_seen=now - dt.timedelta(days=2)),
                row("203.0.113.9", "c4"),
                row("c-73-1-2-3.hsd1.tx.example.net", "c5"),
            ],
            sites=frozenset({site.site_id}),
        ),
    }
    population = ninjaone_population(ninja_rows, macs, serials)
    joined = assess(population, inventories, now=now, ninjaone_stale_days=14)
    view = _links_from(client_id, joined, ninja_rows, now)
    remembered = assess_from_identity(
        view,
        ninja_rows,
        inventories,
        now=now,
        ninjaone_stale_days=14,
        ninja_sites=[site.site_id],
        macs=macs,
        serials=serials,
    )

    def states(result: Any) -> dict[str, dict[str, tuple[str, str | None]]]:
        return {
            d.device.device_id: {t: (v.state, v.matched_by) for t, v in d.tools.items()}
            for d in result.devices
        }

    assert states(joined) == states(remembered)
    assert states(joined)["1"] == {
        "bitdefender": ("covered", "mac"),
        "connectsecure": ("covered", "exact"),
    }
    assert states(joined)["3"] == {
        "bitdefender": ("rmm_stale", None),
        "connectsecure": ("rmm_agent_down", "serial"),
    }
    assert states(joined)["4"] == {
        "bitdefender": ("rmm_stale", None),
        "connectsecure": ("rmm_stale", None),
    }
    # Twins: unmatched where the tool holds their name, absent where it does not.
    assert states(joined)["5"] == {
        "bitdefender": ("unmatched", None),
        "connectsecure": ("absent", None),
    }

    def reverse(result: Any) -> dict[str, list[tuple[str, str, str | None]]]:
        return {
            kind: sorted((u.tool, u.name, u.note) for u in getattr(result, kind))
            for kind in ("unmanaged", "discovered", "external", "not_assessable")
        }

    assert reverse(joined) == reverse(remembered)
    assert reverse(joined)["unmanaged"] == [
        ("bitdefender", "ATA-TWIN", None),
        ("bitdefender", "DESKTOP-4K2J9QP", NOT_ONBOARDED),
    ]
    assert [name for _, name, _ in reverse(joined)["external"]] == [
        "203.0.113.9",
        "c-73-1-2-3.hsd1.tx.example.net",
    ]
    assert compare_counts(joined) == compare_counts(remembered)


def compare_counts(result: Any) -> dict[str, int]:
    return {
        "agent_down": sum(1 for d in result.devices if d.agent_down),
        "unmanaged": len(result.unmanaged),
        "external": len(result.external),
    }

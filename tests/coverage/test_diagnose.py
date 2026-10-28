"""The matching diagnostic: one synthetic case per category, and the two match rates."""

from __future__ import annotations

import datetime as dt
import uuid

from app.connectors.bitdefender.models import BdEndpoint
from app.connectors.connectsecure.models import CsAgent, CsAsset
from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.scope import SiteRef
from app.coverage.diagnose import (
    NinjaExtra,
    legacy_key,
    mac_key,
    run_diagnostic,
    serial_key,
)

NOW = dt.datetime(2026, 9, 24, 12, tzinfo=dt.UTC)
ACME = SiteRef(
    site_id=uuid.uuid5(uuid.NAMESPACE_DNS, "acme"), slug="acme", name="Acme", external_id="1"
)
BEACON = SiteRef(
    site_id=uuid.uuid5(uuid.NAMESPACE_DNS, "beacon"), slug="beacon", name="Beacon", external_id="2"
)
ORPHAN = SiteRef(
    site_id=uuid.uuid5(uuid.NAMESPACE_DNS, "orphan"), slug="orphan", name="Orphan", external_id="3"
)


def ninja(
    device_id: int,
    name: str,
    site: SiteRef = ACME,
    *,
    days_ago: int = 1,
    node: str = "WINDOWS_WORKSTATION",
) -> tuple[SiteRef, NinjaDevice]:
    return site, NinjaDevice(
        id=device_id, systemName=name, nodeClass=node, lastContact=NOW - dt.timedelta(days=days_ago)
    )


def bd(
    endpoint_id: str, name: str, site: SiteRef = ACME, **fields: object
) -> tuple[SiteRef, BdEndpoint]:
    return site, BdEndpoint(id=endpoint_id, name=name, **{"managedWithBest": True, **fields})


def cs(asset_id: str, host: str, site: SiteRef = ACME, **fields: object) -> tuple[SiteRef, CsAsset]:
    agent = fields.pop("agent", CsAgent(id=f"a{asset_id}"))
    return site, CsAsset(id=asset_id, host_name=host, agent=agent, last_scanned=NOW, **fields)


def test_keys() -> None:
    assert legacy_key("SKG-WS-ACCOUNTING-04.corp.local") == "skg-ws-accounti"
    assert mac_key("00:1A:2B:3C:4D:5E") == "001a2b3c4d5e"
    assert mac_key("00-00-00-00-00-00") is None
    assert serial_key(" to be filled by o.e.m. ") is None
    assert serial_key("abc123") == "ABC123"


def test_every_category_from_its_own_case() -> None:
    devices = [
        ninja(1, "PAIRED"),
        ninja(2, "MOVED"),  # in Bitdefender, at Beacon
        ninja(3, "SKG-WS-ACCOUNTI"),  # NetBIOS-truncated in NinjaOne, full in ConnectSecure
        ninja(4, "DASH-NAME"),  # DASH_NAME in Bitdefender
        ninja(5, "RENAMED"),  # only the MAC pairs it
        ninja(6, "BY-SERIAL"),  # only the serial pairs it, in ConnectSecure
        ninja(7, "NOTHING"),
        ninja(8, "GHOST-OLD", days_ago=40),  # the RMM itself has not seen it
        ninja(9, "PRINTER", node="NETWORK_PRINTER"),
    ]
    extras = {
        5: NinjaExtra(macs=frozenset({"001a2bccddee"})),
        6: NinjaExtra(serial="SER-6"),
    }
    bitdefender = [
        bd("b1", "paired.acme.local"),
        bd("b2", "MOVED", BEACON),
        bd("b4", "DASH_NAME"),
        bd("b5", "WAS-RENAMED", macs=["00:1A:2B:CC:DD:EE"]),
        bd("b6", "STRAY-AT-ORPHAN", ORPHAN),  # a site NinjaOne has no mapping for
        bd("b7", "DISCOVERED", isManaged=False, managedWithBest=False),
        bd("b8", "REAL-STRAY"),
    ]
    connectsecure = [
        cs("c1", "PAIRED"),
        cs("c3", "SKG-WS-ACCOUNTING-04"),
        cs("c6", "OTHER-NAME", serial_number="ser-6"),
        cs("c9", "PROBE-1", agent_type="PROBE"),
        cs("c10", "RETIRED", agent=CsAgent(id="r", is_retired=True)),
    ]
    result = run_diagnostic(
        ninja=devices,
        extras=extras,
        ninja_sites=[ACME.site_id, BEACON.site_id],
        tools={
            "bitdefender": (bitdefender, [ACME.site_id, BEACON.site_id, ORPHAN.site_id]),
            "connectsecure": (connectsecure, [ACME.site_id]),
        },
        now=NOW,
    )
    absent = {(r.tool, r.name): r.diagnosis for r in result.rows if r.kind == "absent"}
    assert absent[("bitdefender", "MOVED")].category == "site_mismatch"
    assert absent[("bitdefender", "MOVED")].other_site == "Beacon"
    assert absent[("connectsecure", "SKG-WS-ACCOUNTI")].category == "normalisation"
    assert absent[("connectsecure", "SKG-WS-ACCOUNTI")].detail.startswith("netbios15")
    assert absent[("bitdefender", "DASH-NAME")].detail.startswith("separators")
    assert absent[("bitdefender", "RENAMED")].detail == "mac"
    assert absent[("connectsecure", "BY-SERIAL")].detail == "serial"
    assert absent[("bitdefender", "NOTHING")].category == "none"
    assert absent[("bitdefender", "GHOST-OLD")].category == "rmm_stale"
    assert ("bitdefender", "PRINTER") not in absent  # not in the population
    assert ("bitdefender", "PAIRED") not in absent

    unmanaged = {(r.tool, r.name): r for r in result.rows if r.kind == "unmanaged"}
    assert unmanaged[("bitdefender", "STRAY-AT-ORPHAN")].unmanaged_class == "not_assessable"
    assert unmanaged[("bitdefender", "DISCOVERED")].unmanaged_class == "discovered"
    assert unmanaged[("bitdefender", "REAL-STRAY")].unmanaged_class == "real"
    assert unmanaged[("bitdefender", "REAL-STRAY")].diagnosis.category == "none"
    assert unmanaged[("bitdefender", "MOVED")].diagnosis.category == "site_mismatch"
    assert unmanaged[("connectsecure", "PROBE-1")].discovered == "probe (PROBE)"
    assert unmanaged[("connectsecure", "RETIRED")].unmanaged_class == "discovered"
    # The reverse of a MAC pairing is a MAC pairing too.
    assert unmanaged[("bitdefender", "WAS-RENAMED")].diagnosis.detail == "mac"

    counts = result.counts()
    assert counts["managed"] == 8 and counts["rmm_stale"] == 1 and counts["not_computers"] == 1
    assert counts["absent_by_category"]["bitdefender"] == {
        "site_mismatch": 1,
        "normalisation": 1,
        "secondary_key": 1,
        "none": 3,  # NOTHING, BY-SERIAL (no BD row), SKG (no BD row)
        "rmm_stale": 1,
    }
    assert counts["unmanaged_by_class"]["bitdefender"] == {
        "not_assessable": 1,
        "discovered": 1,
        "real": 4,
    }


def test_the_two_match_rates_have_different_definitions() -> None:
    # Two computers; Bitdefender has one at the right site and one at the wrong site.
    devices = [ninja(1, "A"), ninja(2, "B"), ninja(3, "C", days_ago=30)]
    bitdefender = [bd("b1", "A"), bd("b2", "B", BEACON)]
    result = run_diagnostic(
        ninja=devices,
        extras={},
        ninja_sites=[ACME.site_id, BEACON.site_id],
        tools={"bitdefender": (bitdefender, [ACME.site_id, BEACON.site_id])},
        now=NOW,
    )
    rates = result.rates
    assert rates.within_site["bitdefender"] == 33.3  # A only, of three
    assert rates.tenant_wide["bitdefender"] == 66.7  # A and B, any site
    assert rates.tenant_wide_active["bitdefender"] == 100.0  # C is stale and out of the base


def test_duplicates_are_told_apart_by_serial_or_contact_or_neither() -> None:
    devices = [
        ninja(1, "TWIN", days_ago=1),
        ninja(2, "TWIN", days_ago=1),
        ninja(3, "PAIR", days_ago=1),
        ninja(4, "PAIR", days_ago=60),
        ninja(5, "BLUR", days_ago=1),
        ninja(6, "BLUR", days_ago=2),
    ]
    extras = {1: NinjaExtra(serial="S1"), 2: NinjaExtra(serial="S2")}
    result = run_diagnostic(
        ninja=devices, extras=extras, ninja_sites=[ACME.site_id], tools={}, now=NOW
    )
    by_key = {g.key: g for g in result.duplicates}
    assert by_key["twin"].serial_distinguishes and not by_key["twin"].contact_distinguishes(
        now=NOW, stale_days=14
    )
    assert not by_key["pair"].serial_distinguishes and by_key["pair"].contact_distinguishes(
        now=NOW, stale_days=14
    )
    assert result.counts()["duplicates"] == {
        "groups": 3,
        "serial_distinguishes": 1,
        "contact_distinguishes": 1,
        "neither": 1,
    }

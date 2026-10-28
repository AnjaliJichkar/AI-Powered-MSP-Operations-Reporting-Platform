"""The join and the per-tool verdicts, case by case, with no vendor and no database."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.connectors.bitdefender.models import BdEndpoint
from app.connectors.connectsecure.models import CsAgent, CsAsset
from app.connectors.cove.models import CoveDevice
from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.scope import SiteRef
from app.coverage import config
from app.coverage.config import COVERAGE_TOOLS, STALE_RULES, tool_by_key
from app.coverage.core import (
    NOT_ONBOARDED,
    Exclusion,
    ManagedDevice,
    ToolEndpoint,
    ToolInventory,
    assess,
    external_target,
    is_default_windows_name,
)
from app.models.enums import ConnectorName

NOW = dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC)
ACME = uuid.uuid5(uuid.NAMESPACE_DNS, "acme")
BEACON = uuid.uuid5(uuid.NAMESPACE_DNS, "beacon")


def device(
    name: str, site: uuid.UUID = ACME, *, days_ago: int = 1, device_id: str | None = None
) -> ManagedDevice:
    return ManagedDevice(
        site_id=site,
        site_slug="acme" if site == ACME else "beacon",
        site_name="Acme" if site == ACME else "Beacon",
        device_id=device_id or f"{name.lower()}@{'acme' if site == ACME else 'beacon'}",
        name=name,
        node_class="WINDOWS_WORKSTATION",
        last_contact=NOW - dt.timedelta(days=days_ago),
    )


def endpoint(
    name: str | None, state: str = "covered", site: uuid.UUID = ACME, detail: str = ""
) -> ToolEndpoint:
    return ToolEndpoint(site_id=site, name=name, state=state, detail=detail or state)  # type: ignore[arg-type]


def run(devices: list[ManagedDevice], **tools: ToolInventory | None) -> dict[str, dict[str, str]]:
    result = assess(devices, dict(tools), now=NOW, ninjaone_stale_days=14)
    return {d.device.name: {k: v.state for k, v in d.tools.items()} for d in result.devices}


def inventory(*endpoints: ToolEndpoint, sites: set[uuid.UUID] | None = None) -> ToolInventory:
    return ToolInventory(endpoints=list(endpoints), sites=frozenset(sites or {ACME}))


# --- one state per case ---------------------------------------------------------


def test_each_state_comes_from_exactly_its_case() -> None:
    devices = [
        device("WS-COVERED"),
        device("WS-STALE"),
        device("WS-ABSENT"),
        device("WS-NOAGENT"),
        device("", device_id="nameless"),
        device("WS-TWIN", device_id="twin-a"),
        device("WS-TWIN", device_id="twin-b"),
    ]
    bitdefender = inventory(
        endpoint("ws-covered.acme.local"),  # the domain suffix is not part of the match
        endpoint("WS-STALE", "stale", detail="agent out of date"),
        endpoint("WS-NOAGENT", "absent", detail="known to GravityZone, no agent installed"),
        endpoint("WS-TWIN"),
    )
    states = run(devices, bitdefender=bitdefender, backup=None)

    assert states["WS-COVERED"] == {"bitdefender": "covered", "backup": "not_assessed"}
    assert states["WS-STALE"]["bitdefender"] == "stale"
    assert states["WS-ABSENT"]["bitdefender"] == "absent"
    assert states["WS-NOAGENT"]["bitdefender"] == "absent"  # a row with nothing on it is no row
    assert states[""]["bitdefender"] == "unmatched"
    # Two managed devices with one hostname: neither is matched, whichever the
    # endpoint "really" is, rather than one of them wrongly covered.
    twins = [
        d
        for d in assess(
            devices, {"bitdefender": bitdefender}, now=NOW, ninjaone_stale_days=14
        ).devices
        if d.device.name == "WS-TWIN"
    ]
    assert {t.tools["bitdefender"].state for t in twins} == {"unmatched"}


def test_a_site_the_tool_has_no_company_for_is_not_assessed_not_absent() -> None:
    devices = [device("WS-1", ACME), device("WS-2", BEACON)]
    states = run(devices, connectsecure=inventory(endpoint("WS-1"), sites={ACME}))
    assert states["WS-1"]["connectsecure"] == "covered"
    assert states["WS-2"]["connectsecure"] == "not_assessed"


def test_ninjaone_itself_is_stale_past_the_threshold() -> None:
    result = assess(
        [device("FRESH", days_ago=3), device("OLD", days_ago=15), device("NEVER")],
        {},
        now=NOW,
        ninjaone_stale_days=14,
    )
    by_name = {d.device.name: d for d in result.devices}
    assert by_name["FRESH"].ninjaone_state == "covered"
    assert by_name["OLD"].ninjaone_state == "stale"
    never = ManagedDevice(site_id=ACME, site_slug="acme", site_name="Acme", device_id="n", name="N")
    assert assess([never], {}, now=NOW, ninjaone_stale_days=14).devices[0].ninjaone_state == "stale"


# --- never across sites ---------------------------------------------------------


def test_the_same_hostname_at_two_sites_is_never_paired_across_them() -> None:
    devices = [device("RECEPTION", ACME), device("RECEPTION", BEACON)]
    # Bitdefender has a RECEPTION at Beacon only; Acme's must not borrow it.
    bitdefender = inventory(endpoint("RECEPTION", site=BEACON), sites={ACME, BEACON})
    result = assess(devices, {"bitdefender": bitdefender}, now=NOW, ninjaone_stale_days=14)
    states = {
        (d.device.site_slug, d.device.name): d.tools["bitdefender"].state for d in result.devices
    }
    assert states[("acme", "RECEPTION")] == "absent"
    assert states[("beacon", "RECEPTION")] == "covered"
    assert result.unmanaged == []


def test_an_endpoint_with_no_managed_device_at_its_site_is_unmanaged() -> None:
    devices = [device("WS-1", ACME)]
    bitdefender = inventory(
        endpoint("WS-1"),
        endpoint("GHOST", site=ACME),
        endpoint("WS-1", site=BEACON),  # the right name, the wrong site
        sites={ACME, BEACON},
    )
    result = assess(devices, {"bitdefender": bitdefender}, now=NOW, ninjaone_stale_days=14)
    # GHOST is unmanaged at a site NinjaOne manages; Beacon's WS-1 is at a site
    # NinjaOne has no device for, so there is nothing to compare it with.
    assert [(u.site_id == ACME, u.name) for u in result.unmanaged] == [(True, "GHOST")]
    assert [(u.site_id == BEACON, u.name) for u in result.not_assessable] == [(True, "WS-1")]


# --- exclusions ---------------------------------------------------------------


def test_an_excluded_device_shows_as_excluded_with_its_reason_and_never_disappears() -> None:
    devices = [device("KIOSK"), device("WS-1")]
    exclusions = [
        Exclusion(
            device_id="kiosk@acme",
            tool="connectsecure",
            reason="Kiosk, no agent by design",
            by="boss@acme.test",
            at=NOW,
        )
    ]
    result = assess(
        devices,
        {
            "connectsecure": inventory(endpoint("WS-1")),
            "bitdefender": inventory(endpoint("WS-1"), endpoint("KIOSK")),
        },
        exclusions=exclusions,
        now=NOW,
        ninjaone_stale_days=14,
    )
    kiosk = next(d for d in result.devices if d.device.name == "KIOSK")
    assert kiosk.tools["connectsecure"].state == "excluded"
    assert kiosk.tools["connectsecure"].detail == "Kiosk, no agent by design"
    assert kiosk.tools["bitdefender"].state == "covered"  # excluded from one tool, not all
    assert len(result.devices) == 2
    assert not kiosk.has_gap


# --- the per-tool readers ------------------------------------------------------


def _site(site_id: uuid.UUID = ACME) -> SiteRef:
    return SiteRef(site_id=site_id, slug="acme", name="Acme", external_id="1")


def test_bitdefender_rows_are_judged_on_agent_currency_and_core_modules() -> None:
    rows = [
        (
            _site(),
            BdEndpoint(id="1", name="OK", managedWithBest=True, modules={"antimalware": True}),
        ),
        (_site(), BdEndpoint(id="2", name="OLD", managedWithBest=True, productOutdated=True)),
        (
            _site(),
            BdEndpoint(id="3", name="OFF", managedWithBest=True, modules={"antimalware": False}),
        ),
        (_site(), BdEndpoint(id="4", name="NONE", managedWithBest=False)),
    ]
    judged = {e.name: (e.state, e.detail) for e in config.bitdefender_endpoints(rows)}
    assert judged["OK"] == ("covered", "agent current, core protection on")
    assert judged["OLD"] == ("stale", "agent out of date")
    assert judged["OFF"] == ("stale", "core protection off by policy")
    assert judged["NONE"] == ("absent", "known to GravityZone, no agent installed")


def test_connectsecure_rows_are_judged_by_the_connectors_own_scan_status() -> None:
    fresh = CsAsset(
        id="1", host_name="FRESH", agent=CsAgent(id="a"), last_scanned=NOW - dt.timedelta(days=2)
    )
    old = CsAsset(
        id="2", host_name="OLD", agent=CsAgent(id="b"), last_scanned=NOW - dt.timedelta(days=40)
    )
    never = CsAsset(id="3", host_name="NEVER", agent=CsAgent(id="c"))
    orphan = CsAsset(id="4", host_name="ORPHAN")
    judged = {
        e.name: e.state
        for e in config.connectsecure_endpoints(
            [(_site(), a) for a in (fresh, old, never, orphan)], now=NOW
        )
    }
    assert judged == {"FRESH": "covered", "OLD": "stale", "NEVER": "stale", "ORPHAN": "absent"}


def test_the_population_is_computers_only() -> None:
    rows = [
        (_site(), NinjaDevice(id=1, systemName="WS", nodeClass="WINDOWS_WORKSTATION")),
        (_site(), NinjaDevice(id=2, systemName="SW", nodeClass="NETWORK_SWITCH")),
        (_site(), NinjaDevice(id=3, systemName="VM", nodeClass="VMWARE_VM_GUEST")),
        (_site(), NinjaDevice(id=4, systemName="HOST", nodeClass="VMWARE_VM_HOST")),
    ]
    assert [d.name for d in config.ninjaone_population(rows)] == ["WS", "VM"]


# --- the config is one place ---------------------------------------------------


def test_every_tool_declares_its_gap_and_backup_now_reads_cove() -> None:
    """Backup was `not_assessed` with no reader until Cove existed; wiring it
    in was one reader on this entry and no caller changed, which is what the
    list is for. The entry a later tool fills is the next one added here."""
    keys = [t.key for t in COVERAGE_TOOLS]
    assert keys == ["bitdefender", "connectsecure", "backup"]
    backup = tool_by_key("backup")
    assert backup is not None
    assert backup.assessed and backup.connector is ConnectorName.COVE
    assert backup.gap == "not backed up by Cove"
    # Its reason is about the customer rather than about the connector not
    # existing, and it is a sentence rather than an exception's message: this
    # string is printed on the board, on a tile and in a review. On the demo
    # tenant the exception's own words — "[cove] the demo tenant has no cove
    # connector" — were being shown to a reader.
    assert "Cove customer mapped" in backup.not_assessed_reason
    for tool in COVERAGE_TOOLS:
        assert tool.gap
        # Every tool can say why it was not assessed, in English. Without one
        # the compute falls back to the exception, which names a connector key.
        assert tool.not_assessed_reason and tool.not_assessed_reason[0].isupper()
    # Every stale rule names a tool that exists (or NinjaOne itself) and says what it means.
    for rule in STALE_RULES:
        assert rule.tool == "ninjaone" or tool_by_key(rule.tool) is not None
        assert rule.definition


# --- the passes ------------------------------------------------------------------


def mac_endpoint(name: str, *macs: str, state: str = "covered") -> ToolEndpoint:
    return ToolEndpoint(
        site_id=ACME,
        name=name,
        state=state,
        detail=state,
        macs=frozenset(macs),  # type: ignore[arg-type]
    )


def mac_device(name: str, *macs: str, device_id: str | None = None) -> ManagedDevice:
    return ManagedDevice(
        site_id=ACME,
        site_slug="acme",
        site_name="Acme",
        device_id=device_id or name.lower(),
        name=name,
        node_class="WINDOWS_WORKSTATION",
        last_contact=NOW,
        macs=frozenset(macs),
    )


def test_each_pass_pairs_one_to_one_and_records_itself() -> None:
    devices = [
        mac_device("EXACT-01"),
        mac_device("SKG-WS-ACCOUNTI"),  # NinjaOne's 15-character name
        mac_device("RENAMED-PC", "aabbccddeeff"),
        mac_device("DOCK-A", "111111111111"),  # two devices share a docking-station MAC
        mac_device("DOCK-B", "111111111111"),
        mac_device("AMBIG-15CHARS-X"),  # two endpoints share its first fifteen characters
        mac_device("LOST-ONE"),
    ]
    bitdefender = inventory(
        mac_endpoint("exact-01.acme.local"),
        mac_endpoint("SKG-WS-ACCOUNTING-04"),
        mac_endpoint("OLD-NAME", "aabbccddeeff"),  # keys arrive normalised (config.mac_key)
        mac_endpoint("DOCKED", "111111111111"),
        mac_endpoint("AMBIG-15CHARS-XY"),
        mac_endpoint("AMBIG-15CHARS-XZ"),
    )
    result = assess(devices, {"bitdefender": bitdefender}, now=NOW, ninjaone_stale_days=14)
    by_name = {d.device.name: d.tools["bitdefender"] for d in result.devices}

    assert (by_name["EXACT-01"].state, by_name["EXACT-01"].matched_by) == ("covered", "exact")
    assert by_name["SKG-WS-ACCOUNTI"].matched_by == "netbios15"
    assert by_name["RENAMED-PC"].matched_by == "mac"
    # A MAC two devices share pairs neither: both unmatched, the endpoint named.
    for twin in ("DOCK-A", "DOCK-B"):
        assert by_name[twin].state == "unmatched"
        assert by_name[twin].candidates == ("DOCKED",)
        assert "shared" in by_name[twin].detail
    # Two endpoints behind one fifteen-character key: unmatched, both named.
    assert by_name["AMBIG-15CHARS-X"].state == "unmatched"
    assert by_name["AMBIG-15CHARS-X"].candidates == ("AMBIG-15CHARS-XY", "AMBIG-15CHARS-XZ")
    assert by_name["LOST-ONE"].state == "absent"
    assert by_name["LOST-ONE"].matched_by is None
    # What paired is claimed; what did not is unmanaged, once per identity.
    assert sorted(u.name for u in result.unmanaged) == [
        "AMBIG-15CHARS-XY",
        "AMBIG-15CHARS-XZ",
        "DOCKED",
    ]


def test_rmm_stale_is_its_own_state_and_never_absent() -> None:
    devices = [device("GONE", days_ago=15), device("HERE", days_ago=14)]
    result = assess(devices, {"bitdefender": inventory()}, now=NOW, ninjaone_stale_days=14)
    states = {d.device.name: d.tools["bitdefender"] for d in result.devices}
    assert states["GONE"].state == "rmm_stale"
    assert "14 days" in states["GONE"].detail
    assert states["HERE"].state == "absent"
    # The review passes None: no threshold, no rmm_stale.
    off = assess(devices, {"bitdefender": inventory()}, now=NOW, ninjaone_stale_days=None)
    assert {d.tools["bitdefender"].state for d in off.devices} == {"absent"}


def test_discovered_rows_are_reported_apart_and_probes_never_pair() -> None:
    devices = [device("WS-1"), device("SCANNER")]
    bitdefender = inventory(
        ToolEndpoint(ACME, "WS-1", "absent", "no agent", discovered="isManaged false"),
        ToolEndpoint(ACME, "UNKNOWN-BOX", "absent", "no agent", discovered="isManaged false"),
        ToolEndpoint(ACME, "REAL-STRAY", "covered", "ok"),
    )
    connectsecure = inventory(
        ToolEndpoint(ACME, "SCANNER", "covered", "current", discovered="probe", probe=True),
    )
    result = assess(
        devices,
        {"bitdefender": bitdefender, "connectsecure": connectsecure},
        now=NOW,
        ninjaone_stale_days=14,
    )
    states = {d.device.name: d.tools for d in result.devices}
    # A discovered row still pairs with its device: the device is absent (no agent).
    assert states["WS-1"]["bitdefender"].state == "absent"
    assert states["WS-1"]["bitdefender"].matched_by == "exact"
    # A probe is not the device that hosts it.
    assert states["SCANNER"]["connectsecure"].state == "absent"
    assert [u.name for u in result.unmanaged] == ["REAL-STRAY"]
    assert sorted(u.name for u in result.discovered) == ["SCANNER", "UNKNOWN-BOX"]


# --- the coverage-sheet defects: collisions, lost devices, external targets -------


def full_device(
    name: str,
    *,
    macs: tuple[str, ...] = (),
    serials: tuple[str, ...] = (),
    days_ago: int = 1,
    device_id: str | None = None,
) -> ManagedDevice:
    return ManagedDevice(
        site_id=ACME,
        site_slug="acme",
        site_name="Acme",
        device_id=device_id or name.lower(),
        name=name,
        node_class="WINDOWS_WORKSTATION",
        last_contact=NOW - dt.timedelta(days=days_ago),
        macs=frozenset(macs),
        serials=frozenset(serials),
    )


def full_endpoint(
    name: str,
    *,
    external_id: str,
    macs: tuple[str, ...] = (),
    serial: str | None = None,
    state: str = "covered",
    last_seen: dt.datetime | None = None,
) -> ToolEndpoint:
    return ToolEndpoint(
        site_id=ACME,
        name=name,
        state=state,  # type: ignore[arg-type]
        detail=state,
        external_id=external_id,
        macs=frozenset(macs),
        serial=serial,
        last_seen=last_seen,
        external=external_target(name),
    )


def test_the_hostname_is_the_full_dns_name_where_the_system_name_is_the_truncation() -> None:
    long = NinjaDevice(id=1, systemName="ATA-WORKSTATIO", dnsName="ATA-WORKSTATION-01.ata.local")
    same = NinjaDevice(id=2, systemName="ATA-DC01", dnsName="ata-dc01.ata.local")
    bare = NinjaDevice(id=3, systemName="ATA-LAPTOP-9")
    none = NinjaDevice(id=4, displayName="Front desk")
    assert long.hostname == "ATA-WORKSTATION-01"  # the domain is the join's to strip
    assert same.hostname == "ATA-DC01"  # the console's spelling where the two agree
    assert bare.hostname == "ATA-LAPTOP-9"
    assert none.hostname == "Front desk"
    assert long.name == "ATA-WORKSTATIO"  # the display name the tools show is unchanged
    rows = []
    for d in (long, same, bare):
        d.node_class = "WINDOWS_WORKSTATION"
        rows.append((_site(), d))
    population = config.ninjaone_population(rows, {}, {1: " to be filled by o.e.m. ", 2: "abc123"})
    assert [d.key for d in population] == ["ata-workstation-01", "ata-dc01", "ata-laptop-9"]
    assert [sorted(d.serials) for d in population] == [[], ["ABC123"], []]


def test_names_that_collide_after_fifteen_characters_are_told_apart() -> None:
    # Three machines whose systemName is ATA-WORKSTATIO; their dnsName tells them
    # apart. ConnectSecure knows the full names; GravityZone knows the NetBIOS
    # name three times over, one row per machine, each with its own MAC.
    devices = [
        full_device("ATA-WORKSTATION1", macs=("aa0000000001", "cc0000000001"), device_id="ws1"),
        full_device("ATA-WORKSTATION2", macs=("aa0000000002", "cc0000000001"), device_id="ws2"),
        full_device("ATA-WORKSTATION3", macs=("aa0000000003",), device_id="ws3"),
    ]
    connectsecure = inventory(
        full_endpoint("ATA-WORKSTATION1", external_id="c1"),
        full_endpoint("ATA-WORKSTATION2", external_id="c2"),
        full_endpoint("ATA-WORKSTATION3", external_id="c3"),
    )
    bitdefender = inventory(
        full_endpoint("ATA-WORKSTATIO", external_id="b1", macs=("aa0000000001",)),
        full_endpoint("ATA-WORKSTATIO", external_id="b2", macs=("aa0000000002", "cc0000000001")),
        full_endpoint("ATA-WORKSTATIO", external_id="b3", macs=("aa0000000003",)),
    )
    result = assess(
        devices,
        {"connectsecure": connectsecure, "bitdefender": bitdefender},
        now=NOW,
        ninjaone_stale_days=14,
    )
    by_id = {d.device.device_id: d.tools for d in result.devices}
    for n in (1, 2, 3):
        assert by_id[f"ws{n}"]["connectsecure"].matched_by == "exact"
        assert by_id[f"ws{n}"]["connectsecure"].endpoint_ids == (f"c{n}",)
        # The NetBIOS pass finds one name for three devices; the MAC each row and
        # device share alone decides, and the docking-station MAC two share does not.
        assert by_id[f"ws{n}"]["bitdefender"].state == "covered"
        assert by_id[f"ws{n}"]["bitdefender"].matched_by == "mac"
        assert by_id[f"ws{n}"]["bitdefender"].endpoint_ids == (f"b{n}",)
    assert result.unmanaged == []


def test_twins_nothing_tells_apart_stay_unmatched_naming_each_other() -> None:
    devices = [
        full_device("WS-TWIN", device_id="twin-a"),
        full_device("WS-TWIN", device_id="twin-b"),
        full_device("WS-ONLY", serials=("SN-1",), device_id="only"),
    ]
    bitdefender = inventory(
        full_endpoint("WS-TWIN", external_id="t"),
        full_endpoint("WS-ONLY", external_id="o1", serial="SN-1"),
        full_endpoint("WS-ONLY", external_id="o2", serial="SN-2"),
    )
    result = assess(devices, {"bitdefender": bitdefender}, now=NOW, ninjaone_stale_days=14)
    by_id = {d.device.device_id: d.tools["bitdefender"] for d in result.devices}
    for twin, other in (("twin-a", "twin-b"), ("twin-b", "twin-a")):
        assert by_id[twin].state == "unmatched"
        assert by_id[twin].detail == "two managed devices at this site share the hostname"
        assert by_id[twin].candidates == ("WS-TWIN", f"device {other}")
        assert by_id[twin].endpoint_ids == ("t",)
        assert by_id[twin].found_by == "exact"
    # One device whose name two rows carry, with nothing to contradict it: both
    # rows are its, as they always were (a reinstalled agent leaves one behind).
    assert by_id["only"].matched_by == "exact"
    assert by_id["only"].endpoint_ids == ("o1", "o2")
    # The row the twins share is not unmanaged: it is one of theirs, undecidably.
    assert [u.name for u in result.unmanaged] == ["WS-TWIN"]


def test_a_serial_pairs_what_no_name_or_mac_could() -> None:
    devices = [
        full_device("NEW-NAME", serials=("SN-42",)),
        full_device("SHARED-SN-A", serials=("SN-9",), device_id="sa"),
        full_device("SHARED-SN-B", serials=("SN-9",), device_id="sb"),
        full_device("NOTHING"),
    ]
    connectsecure = inventory(
        full_endpoint("OLD-NAME", external_id="1", serial="SN-42"),
        full_endpoint("WHICHEVER", external_id="2", serial="SN-9"),
    )
    result = assess(devices, {"connectsecure": connectsecure}, now=NOW, ninjaone_stale_days=14)
    by_id = {d.device.device_id: d.tools["connectsecure"] for d in result.devices}
    assert (by_id["new-name"].state, by_id["new-name"].matched_by) == ("covered", "serial")
    # A serial two devices report (a placeholder that slipped through) pairs neither.
    for twin in ("sa", "sb"):
        assert by_id[twin].state == "unmatched"
        assert by_id[twin].found_by == "serial"
        assert "shared" in by_id[twin].detail
    assert by_id["nothing"].state == "absent"
    assert by_id["nothing"].detail == "no endpoint of this hostname, MAC or serial"


def test_a_lost_device_takes_part_and_is_agent_down_when_a_tool_still_hears_it() -> None:
    devices = [
        full_device("ALIVE-BD", days_ago=30),
        full_device("UNDATED-BD", days_ago=30),
        full_device("ALIVE-CS", days_ago=30),
        full_device("DEAD-CS", days_ago=30),
        full_device("GONE", days_ago=30),
        full_device("LIVE", days_ago=1),
    ]
    bitdefender = inventory(
        # An agent with a module off, seen by GravityZone this week (the detail read).
        full_endpoint(
            "ALIVE-BD", external_id="b1", state="stale", last_seen=NOW - dt.timedelta(days=2)
        ),
        # An agent on the list with no date: no evidence of life, whatever the row says.
        full_endpoint("UNDATED-BD", external_id="b0"),
        full_endpoint("LIVE", external_id="b2"),
    )
    connectsecure = inventory(
        full_endpoint("ALIVE-CS", external_id="c1", last_seen=NOW - dt.timedelta(days=3)),
        full_endpoint(
            "DEAD-CS", external_id="c2", state="stale", last_seen=NOW - dt.timedelta(days=40)
        ),
    )
    result = assess(
        devices,
        {"bitdefender": bitdefender, "connectsecure": connectsecure},
        now=NOW,
        ninjaone_stale_days=14,
    )
    by_name = {d.device.name: d for d in result.devices}
    alive_bd = by_name["ALIVE-BD"].tools["bitdefender"]
    assert alive_bd.state == "rmm_agent_down"
    assert alive_bd.detail.startswith("machine alive per bitdefender, NinjaOne not reporting")
    assert alive_bd.matched_by == "exact" and alive_bd.endpoint_ids == ("b1",)
    assert by_name["ALIVE-BD"].tools["connectsecure"].state == "rmm_stale"
    assert by_name["ALIVE-BD"].agent_down
    undated = by_name["UNDATED-BD"].tools["bitdefender"]
    assert undated.state == "rmm_stale" and undated.matched_by == "exact"
    assert "bitdefender holds it as covered" in undated.detail
    alive_cs = by_name["ALIVE-CS"].tools["connectsecure"]
    assert alive_cs.state == "rmm_agent_down"
    assert alive_cs.last_seen == NOW - dt.timedelta(days=3)
    dead = by_name["DEAD-CS"].tools["connectsecure"]
    assert dead.state == "rmm_stale" and dead.matched_by == "exact"
    assert "connectsecure holds it as stale" in dead.detail
    assert not by_name["DEAD-CS"].agent_down
    gone = by_name["GONE"].tools["bitdefender"]
    assert gone.state == "rmm_stale"
    assert "no endpoint of this hostname, MAC or serial" in gone.detail
    assert by_name["LIVE"].tools["bitdefender"].state == "covered"
    # One physical machine, never both stale and unmanaged: every paired row is claimed.
    assert result.unmanaged == []
    # RMM-stale stays out of the tool's gap count; agent-down is not a gap either.
    assert not by_name["ALIVE-BD"].has_gap and not by_name["GONE"].has_gap


def test_external_scan_targets_are_reported_apart_and_default_names_are_noted() -> None:
    assert external_target("203.0.113.7") == "name is an IP address"
    assert external_target("c-73-1-2-3.hsd1.tx.example.net") is not None
    assert external_target("73.1.2.3.static.example.net") is not None
    assert external_target("ip72-200-1-2.dc.example.net") is not None
    assert external_target("ATA-WS-01") is None
    assert external_target("pool-3") is None  # a single label is a workstation
    assert external_target("ata-ws-01.ata.local") is None
    assert external_target("SRV-2019-01.corp.example.com") is None  # digits, no address shape
    # An ISP-sounding word with no address in it is a machine: two such names at
    # the pilot were workstations NinjaOne manages.
    assert external_target("dsl-office.acme.local") is None
    assert external_target("host01.acme.local") is None
    assert is_default_windows_name("DESKTOP-4K2J9QP") and is_default_windows_name("laptop-a1b2c3d")
    assert is_default_windows_name("WIN-ABCDEFGHIJK")
    assert not is_default_windows_name("DESKTOP-FRONT")

    devices = [full_device("WS-1")]
    connectsecure = inventory(
        full_endpoint("WS-1", external_id="1"),
        full_endpoint("203.0.113.7", external_id="2"),
        full_endpoint("c-73-1-2-3.hsd1.tx.example.net", external_id="3"),
        full_endpoint("DESKTOP-4K2J9QP", external_id="4"),
        full_endpoint("REAL-STRAY", external_id="5"),
    )
    result = assess(devices, {"connectsecure": connectsecure}, now=NOW, ninjaone_stale_days=14)
    assert [(u.name, u.note) for u in result.external] == [
        ("203.0.113.7", "name is an IP address"),
        ("c-73-1-2-3.hsd1.tx.example.net", "name is an ISP reverse-DNS name (an address in it)"),
    ]
    assert [(u.name, u.note) for u in result.unmanaged] == [
        ("DESKTOP-4K2J9QP", NOT_ONBOARDED),
        ("REAL-STRAY", None),
    ]


def test_same_named_vendor_rows_that_are_different_machines_are_each_reported() -> None:
    # Three GravityZone rows called RECEPTION with three MACs and no NinjaOne
    # device: three machines, three unmanaged entries, every vendor id carried.
    # Two rows called KIOSK sharing a MAC: one machine, one entry with both ids.
    bitdefender = inventory(
        full_endpoint("RECEPTION", external_id="r1", macs=("aa0000000001",)),
        full_endpoint("RECEPTION", external_id="r2", macs=("aa0000000002",)),
        full_endpoint("RECEPTION", external_id="r3", macs=("aa0000000003",)),
        full_endpoint("KIOSK", external_id="k1", macs=("bb0000000001",)),
        full_endpoint("KIOSK", external_id="k2", macs=("bb0000000001", "bb0000000002")),
    )
    result = assess(
        [full_device("WS-1")], {"bitdefender": bitdefender}, now=NOW, ninjaone_stale_days=14
    )
    assert sorted((u.name, u.external_ids) for u in result.unmanaged) == [
        ("KIOSK", ("k1", "k2")),
        ("RECEPTION", ("r1",)),
        ("RECEPTION", ("r2",)),
        ("RECEPTION", ("r3",)),
    ]


# --- Cove on the board ---------------------------------------------------------


def _cove(
    name: str,
    *,
    last_success_days: int | None = 0,
    microsoft365: bool = False,
) -> tuple[SiteRef, CoveDevice]:
    return (
        _site(),
        CoveDevice(
            account_id=1,
            partner_id=2,
            name=name,
            key=name,
            account_type="microsoft365" if microsoft365 else "backup_manager",
            last_successful_at=(
                None if last_success_days is None else NOW - dt.timedelta(days=last_success_days)
            ),
        ),
    )


def test_a_cove_row_is_judged_on_its_last_successful_backup() -> None:
    found = config.cove_endpoints(
        [
            _cove("fresh_ab12c", last_success_days=0),
            _cove("late_ab12c", last_success_days=9),
            _cove("never_ab12c", last_success_days=None),
        ],
        now=NOW,
    )
    assert [(e.name, e.state) for e in found] == [
        ("fresh", "covered"),
        ("late", "stale"),
        ("never", "absent"),
    ]
    # Never backed up is a gap and says so, not a very old date.
    assert "no successful backup on record" in found[2].detail
    assert found[2].last_seen is None


def test_cove_strips_its_own_suffix_so_the_machine_matches() -> None:
    """`jc-lt-25_pqpsp` is `JC-LT-25` everywhere else. Without this every
    Cove row would be an unmanaged endpoint and every machine a gap."""
    (endpoint,) = config.cove_endpoints([_cove("jc-lt-25_pqpsp")], now=NOW)
    assert endpoint.name == "jc-lt-25"
    assert endpoint.key == "jc-lt-25"


def test_a_microsoft_365_tenant_is_not_an_endpoint_on_this_board() -> None:
    """Cove counts a customer's tenant as a device. It is not a machine, so
    it is neither covered nor an unmanaged endpoint: the board is about
    agents on machines."""
    found = config.cove_endpoints(
        [_cove("contoso.example.invalid", microsoft365=True), _cove("real-ws_ab12c")],
        now=NOW,
    )
    assert [e.name for e in found] == ["real-ws"]


def test_a_tools_key_is_not_its_connector_name() -> None:
    """Backup reads Cove: the board's key and the connector's name are two
    different words. They are the same for the other two, which is how code
    that conflated them worked until Cove arrived — twice, in the identity
    groups and in the link rows."""
    backup = tool_by_key("backup")
    assert backup is not None and backup.connector is not None
    assert backup.key != backup.connector.value
    for tool in COVERAGE_TOOLS:
        if tool.connector is not None:
            # Whatever a tool is called, its connector must be a real one:
            # a link row stores a connector, not a key.
            assert ConnectorName(tool.connector.value) is tool.connector


def test_cove_rows_carry_their_macs_for_the_third_pass() -> None:
    """Live, 2026-10-19: every one of the pilot's machines sends at least one
    MAC and some send eleven, comma-separated. Cove sends no serial at all,
    so the MAC is the only thing beyond the name the join can use."""
    site_ref = _site()
    device = CoveDevice(
        account_id=1,
        partner_id=2,
        name="fab-ws-01_ab12c",
        key="fab-ws-01",
        computer_name="FAB-WS-01",
        macs=["00:1A:2B:DD:EE:01", "00-1a-2b-dd-ee-02"],
        last_successful_at=NOW,
    )
    (endpoint,) = config.cove_endpoints([(site_ref, device)], now=NOW)
    assert len(endpoint.macs) == 2
    assert endpoint.serial is None


@pytest.mark.parametrize(
    ("mac", "why"),
    [
        ("00:00:00:00:00:00", "all zeros is a placeholder, not an address"),
        ("ff:ff:ff:ff:ff:ff", "so is all ones"),
        ("AA:BB:CC:DD:EE:FF", "locally administered: software minted it"),
        ("52:54:00:12:34:56", "QEMU, and locally administered with it"),
        ("00:15:5D:01:02:03", "a Hyper-V virtual NIC"),
        ("00:50:56:01:02:03", "a VMware virtual NIC"),
        ("08:00:27:01:02:03", "VirtualBox"),
        ("not a mac at all", "not twelve hex digits"),
    ],
)
def test_a_mac_that_cannot_identify_a_machine_is_refused(mac: str, why: str) -> None:
    """Pairing two machines on a shared address is worse than leaving both
    unpaired, and a cloned VM carries its template's virtual MAC."""
    assert config.mac_key(mac) is None, why


def test_a_real_burned_in_mac_is_kept() -> None:
    assert config.mac_key("00:1A:2B:CC:DD:EE") == "001a2bccddee"
    assert config.mac_key("00-1a-2b-cc-dd-ee") == "001a2bccddee"


def test_the_computer_name_wins_where_it_differs_from_the_device_name() -> None:
    """Cove's device name is whatever the account was called; its computer
    name is the machine's own. They agree on all but one of the pilot's 44."""
    renamed = CoveDevice(
        account_id=1,
        partner_id=2,
        name="old-name_ab12c",
        key="old-name",
        computer_name="REAL-HOSTNAME",
        last_successful_at=NOW,
    )
    (endpoint,) = config.cove_endpoints([(_site(), renamed)], now=NOW)
    assert endpoint.name == "real-hostname"

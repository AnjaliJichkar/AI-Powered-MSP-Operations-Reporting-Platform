"""The generated world: deterministic, sized as promised, every scenario present, nothing real."""

from __future__ import annotations

import datetime as dt
import re

from app.demo import vocabulary as V
from app.demo.dataset import DAYS, World, at, build_world, epoch
from app.demo.scenarios import SCENARIOS, check, describe

#: Naming shapes of the pilot tenant's machines, which must never appear in the demo.
REAL_TENANT_SHAPES = re.compile(r"\b(TL-AUS|SKG|TLIT|teamlogic)\b", re.IGNORECASE)


def test_the_same_seed_is_the_same_world() -> None:
    first, second = build_world(7), build_world(7)
    assert first.summary() == second.summary()
    assert [d.serial for d in first.devices] == [d.serial for d in second.devices]
    assert [t.title for t in first.tickets] == [t.title for t in second.tickets]
    assert [f.cve for f in first.findings] == [f.cve for f in second.findings]
    assert build_world(8).summary() != first.summary() or [
        d.serial for d in build_world(8).devices
    ] != [d.serial for d in first.devices]


def test_the_world_is_the_size_the_brief_asked_for() -> None:
    world = build_world()
    summary = world.summary()
    assert summary["customers"] == 8
    assert 240 <= summary["devices"] <= 270  # about 250 computers, plus a switch each
    assert summary["tickets"] >= 500 and summary["patch_installs"] >= 2000
    assert summary["alerts"] >= 100 and summary["detections"] >= 10
    assert summary["vulnerability_findings"] >= 400
    # Ninety days of history and no more.
    assert max(t.created_days for t in world.tickets) <= DAYS
    assert max(i.installed_days for i in world.installs) <= DAYS + 1
    assert max(a.created_days for a in world.alerts) <= DAYS


def test_every_scenario_is_present_and_documented() -> None:
    world = build_world()
    assert check(world) == []
    assert len(SCENARIOS) >= 41
    document = describe()
    for scenario in SCENARIOS:
        assert scenario.key in document and scenario.where in document
    # Every scenario key is unique and named by the area it belongs to.
    keys = [s.key for s in SCENARIOS]
    assert len(set(keys)) == len(keys)
    assert all(
        key.split(".")[0]
        in {"coverage", "bitdefender", "ninjaone", "connectsecure", "autotask", "backup"}
        for key in keys
    )


def test_names_are_fictional_and_vocabulary_is_public() -> None:
    world = build_world()
    names = " ".join(
        [c.name for c in world.customers]
        + [d.system_name for d in world.devices]
        + [d.dns_name for d in world.devices]
        + [p.name for c in world.customers for p in c.people]
        + [loc.name for c in world.customers for loc in c.locations]
    )
    assert not REAL_TENANT_SHAPES.search(names)
    assert {c.name for c in world.customers} <= {name for name, _, _ in V.CUSTOMERS}
    assert all(kb.startswith("KB") and kb[2:].isdigit() for kb, _, _ in V.OS_PATCHES)
    assert all(re.fullmatch(r"CVE-\d{4}-\d{4,7}", cve) for cve, _, _, _ in V.CVES)
    assert sum(1 for _, _, _, kev in V.CVES if kev) >= 10
    assert all(f.cve.startswith("CVE-") for f in world.findings)


def test_time_is_relative_to_the_clock_at_serve_time() -> None:
    world = build_world()
    device = next(d for d in world.devices if d.last_contact_days is not None)
    early = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    late = dt.datetime(2031, 6, 1, tzinfo=dt.UTC)
    assert at(device.last_contact_days, early) < early
    assert at(device.last_contact_days, late) - at(device.last_contact_days, early) == late - early  # type: ignore[operator]
    assert epoch(None) is None and at(None) is None
    # Nothing in the world is a wall-clock time: only offsets.
    assert not any(
        isinstance(getattr(d, name), dt.datetime) for d in world.devices[:5] for name in d.__slots__
    )


def test_scenario_devices_are_placed_where_the_document_says() -> None:
    world: World = build_world()
    by_key = {c.key: c for c in world.customers}
    twins = [d for d in world.devices if d.scenario == "netbios_collision"]
    assert {d.customer_key for d in twins} == {"contoso"}
    assert len({d.system_name for d in twins}) == 1 and len({d.dns_name for d in twins}) == 2
    assert len(twins[0].system_name) == 15
    till = next(d for d in world.devices if d.scenario == "windows_10_iot_classed_server")
    assert till.node_class == "WINDOWS_SERVER" and "IoT" in till.os_name
    assert by_key["fabrikam"].bitdefender_company_ids[1].endswith("f8")
    assert by_key["fourth"].av_only and not by_key["fourth"].edr_licensed
    down = next(d for d in world.devices if d.scenario == "rmm_agent_down")
    assert down.last_contact_days > 14 and down.bitdefender_last_seen_days < 1  # type: ignore[operator]

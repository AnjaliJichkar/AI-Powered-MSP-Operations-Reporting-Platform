"""Workstation or server is decided once, by the operating system where it can be.

`hardware.device_role` feeds the deck's device counts and the PDF's lifecycle
tables alike; NinjaOne's node class alone put a Windows 10 IoT till among the
servers. The collector tests run against the recorded NinjaOne fixtures with one
row changed in memory.
"""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest
import respx

from app.connectors.ninjaone.hardware import device_role, role_of_class
from app.reports.tbr.collect import ROLE_DISAGREEMENTS
from app.reports.tbr.monthly import LIFECYCLE_SERVERS, LIFECYCLE_WORKSTATIONS, collect_lifecycle
from tests.connectors.conftest import NINJA_BASE_URL, load_fixture
from tests.reports.test_tbr_security import collector, mock_ninja, two_connectors  # noqa: F401

pytestmark = pytest.mark.usefixtures("two_connectors")


@pytest.mark.parametrize(
    ("os_name", "node_class", "role", "from_os", "disagrees"),
    [
        ("Microsoft Windows Server 2022 Standard", "WINDOWS_SERVER", "server", True, False),
        ("Microsoft Windows 11 Pro Edition", "WINDOWS_WORKSTATION", "workstation", True, False),
        # The case that started this: a till on Windows 10 IoT, classed as a server.
        ("Windows 10 IoT Enterprise LTSC", "WINDOWS_SERVER", "workstation", True, True),
        # And the reverse: Windows Server on a machine NinjaOne classed as a workstation.
        ("Microsoft Windows Server 2019 Standard", "WINDOWS_WORKSTATION", "server", True, True),
        ("macOS 14.6", "MAC_SERVER", "workstation", True, True),
        ("Mac OS X 10.15", "MAC", "workstation", True, False),
        ("Ubuntu 22.04.4 LTS", "LINUX_SERVER", "server", False, False),
        ("Ubuntu 22.04.4 LTS", "LINUX_WORKSTATION", "workstation", False, False),
        (None, "VMWARE_VM_GUEST", "server", False, False),
        (None, "WINDOWS_WORKSTATION", "workstation", False, False),
        (None, "NMS_SWITCH", "other", False, False),
        ("", None, "other", False, False),
    ],
)
def test_the_operating_system_decides_where_it_can_and_the_node_class_elsewhere(
    os_name: str | None, node_class: str | None, role: str, from_os: bool, disagrees: bool
) -> None:
    decided = device_role(os_name, node_class)
    assert decided.role == role
    assert decided.from_os is from_os
    assert decided.disagrees is disagrees
    assert decided.class_role == role_of_class(node_class)
    assert decided.basis.startswith("operating system:" if from_os else "NinjaOne node class:")
    if os_name:
        assert (os_name in decided.basis) is from_os


def _operating_systems_with(device_id: int, name: str) -> dict[str, Any]:
    rows = copy.deepcopy(load_fixture("ninjaone", "query_operating_systems.json"))
    for row in rows["results"]:
        if row["deviceId"] == device_id:
            row["name"] = name
    return rows


def _mock_inventory(operating_systems: dict[str, Any]) -> None:
    """The recorded NinjaOne fixtures, with the operating-systems report replaced."""
    mock_ninja()
    for report in ("computer-systems", "processors"):
        respx.get(f"{NINJA_BASE_URL}/v2/queries/{report}").mock(
            return_value=httpx.Response(
                200, json=load_fixture("ninjaone", f"query_{report.replace('-', '_')}.json")
            )
        )
    respx.get(f"{NINJA_BASE_URL}/v2/queries/operating-systems").mock(
        return_value=httpx.Response(200, json=operating_systems)
    )
    respx.get(f"{NINJA_BASE_URL}/v2/queries/volumes").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "query_volumes.json"))
    )
    respx.get(f"{NINJA_BASE_URL}/v2/queries/logged-on-users").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "query_empty.json"))
    )
    respx.get(url__regex=r".*/v2/organization/\d+/locations").mock(
        return_value=httpx.Response(200, json=[])
    )


async def _facts_from() -> Any:
    run = collector()
    await run._ninja_inventory()
    await run._ninja_disks()
    await collect_lifecycle(run)
    return run


@respx.mock
async def test_the_recorded_fixtures_count_as_they_did() -> None:
    """One Windows Server, three Windows workstations, one Mac with no OS row."""
    _mock_inventory(load_fixture("ninjaone", "query_operating_systems.json"))
    run = await _facts_from()

    assert run.facts.facts["ninjaone.devices.workstations"].value == 4
    assert run.facts.facts["ninjaone.devices.servers_physical"].value == 1
    assert run.facts.facts["ninjaone.devices.servers_virtual"].value == 0
    assert run.facts.tables[ROLE_DISAGREEMENTS].rows == []
    assert [r[0] for r in run.facts.tables[LIFECYCLE_SERVERS].rows] == ["TL-AUS-DC01"]
    assert len(run.facts.tables[LIFECYCLE_WORKSTATIONS].rows) == 4
    # The Mac has no operating-systems row: NinjaOne's class decided, and said so.
    assert run.ninja_roles[1004].from_os is False
    assert run.ninja_roles[1004].basis == "NinjaOne node class: MAC"


@respx.mock
async def test_a_windows_10_iot_device_classed_as_a_server_is_a_workstation() -> None:
    """Device 1001 is `WINDOWS_SERVER` in the recorded inventory; its OS row says
    Windows 10 IoT. It lands in the workstation table, the server table is written
    empty, the counts follow, and the internal list names it."""
    _mock_inventory(_operating_systems_with(1001, "Microsoft Windows 10 IoT Enterprise LTSC"))
    run = await _facts_from()

    assert run.facts.facts["ninjaone.devices.workstations"].value == 5
    assert run.facts.facts["ninjaone.devices.servers_physical"].value == 0
    assert run.facts.facts["ninjaone.devices.servers_virtual"].value == 0
    workstations = run.facts.tables[LIFECYCLE_WORKSTATIONS]
    servers = run.facts.tables[LIFECYCLE_SERVERS]
    assert "TL-AUS-DC01" in [r[0] for r in workstations.rows]
    assert servers.rows == [] and servers.total == 0  # written, and empty
    assert run.ninja_roles[1001].basis == (
        "operating system: Microsoft Windows 10 IoT Enterprise LTSC"
    )
    disagreements = run.facts.tables[ROLE_DISAGREEMENTS]
    assert disagreements.internal_only
    assert disagreements.rows == [
        [
            "TL-AUS-DC01",
            "Microsoft Windows 10 IoT Enterprise LTSC",
            "WINDOWS_SERVER",
            "workstation",
        ]
    ]
    # The lifecycle rows and the deck's counts are the same population.
    assert len(workstations.rows) == run.facts.facts["ninjaone.devices.workstations"].value


@respx.mock
async def test_windows_server_on_a_workstation_class_is_a_server() -> None:
    _mock_inventory(_operating_systems_with(1002, "Microsoft Windows Server 2019 Standard"))
    run = await _facts_from()

    assert run.facts.facts["ninjaone.devices.workstations"].value == 3
    assert run.facts.facts["ninjaone.devices.servers_physical"].value == 2
    assert sorted(r[0] for r in run.facts.tables[LIFECYCLE_SERVERS].rows) == [
        "TL-AUS-DC01",
        "TL-AUS-WS14",
    ]
    assert [r[0] for r in run.facts.tables[ROLE_DISAGREEMENTS].rows] == ["TL-AUS-WS14"]

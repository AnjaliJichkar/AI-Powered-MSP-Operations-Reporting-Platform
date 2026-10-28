"""The reads the business review added: Autotask projects, NinjaOne locations, hardware class.

Fixtures carry the live shape (every key, every type) with every value replaced;
see api-notes for what was observed. Project statuses are the pilot tenant's own
picklist labels, which is configuration rather than customer data.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.agent.registry import get_tool
from app.connectors.autotask.tools import list_projects
from app.connectors.base import ConnectorLimits
from app.connectors.errors import AuthError, UpstreamError
from app.connectors.ninjaone.hardware import (
    classify_machine,
    processor_supported,
    windows11_eligibility,
)
from app.connectors.ninjaone.tools import get_hardware
from tests.connectors.conftest import (
    AUTOTASK_ZONE_URL,
    NINJA_BASE_URL,
    NINJA_ORGANIZATION_ID,
    RecordingSleep,
    load_fixture,
    make_autotask_client,
    make_ninja_client,
    single_page,
)

CLIENT_ID = str(uuid.uuid4())


def at_query(entity: str) -> str:
    return f"{AUTOTASK_ZONE_URL}/V1.0/{entity}/query"


@pytest.fixture
def autotask(stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    return stub_factory(
        make_autotask_client(
            limits=fast_limits, sleep=sleeper, config={"base_url": AUTOTASK_ZONE_URL}
        )
    )


@pytest.fixture(autouse=True)
def _clock(autotask_clock: dt.datetime) -> None:
    """2026-09-16 12:00 UTC."""


def mock_project_world() -> tuple[respx.Route, respx.Route]:
    respx.get(f"{AUTOTASK_ZONE_URL}/V1.0/Projects/entityInformation/fields").mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_projects.json"))
    )
    projects = respx.post(at_query("Projects")).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "projects.json"))
    )
    phases = respx.post(at_query("Phases")).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "phases.json"))
    )
    return projects, phases


# --- list_projects ------------------------------------------------------------------------


@respx.mock
async def test_open_projects_with_phase_progress_and_overdue_first(autotask: Any) -> None:
    projects, _ = mock_project_world()

    result = await list_projects(CLIENT_ID)

    assert [(p.project, p.status, p.overdue) for p in result.projects] == [
        ("Firewall replacement", "New", True),  # past its end date: shown first
        ("Office 365 tenant migration", "In Progress", False),
    ]
    migration = result.projects[1]
    assert migration.phase == "Migration"  # the phase whose dates contain today
    assert migration.percent_complete == 40
    assert result.projects[0].phase is None  # no phases: said so, never invented
    # Internal, template and baseline projects are filtered by label, server-side.
    sent = json.loads(projects.calls[0].request.content)["filter"]
    types = next(f for f in sent if f.get("field") == "projectType")
    assert types == {"op": "in", "field": "projectType", "value": [2, 5]}


@respx.mock
async def test_scheduled_and_completed_are_separate_states(autotask: Any) -> None:
    mock_project_world()

    scheduled = await list_projects(CLIENT_ID, state="scheduled")
    done = await list_projects(CLIENT_ID, state="completed")
    everything = await list_projects(CLIENT_ID, state="all")

    assert [p.project for p in scheduled.projects] == ["Wi-Fi refresh"]
    assert [p.project for p in done.projects] == ["Server room UPS"]
    assert everything.total == 4
    assert everything.status_counts == {"New": 1, "In Progress": 1, "Scheduled": 1, "Complete": 1}


@respx.mock
async def test_a_phase_of_someone_elses_project_is_never_used(autotask: Any) -> None:
    """Phases carry no company: they are only ever read for projects already placed."""
    _, phases = mock_project_world()

    result = await list_projects(CLIENT_ID, state="all")

    asked = json.loads(phases.calls[0].request.content)["filter"][0]
    assert asked["field"] == "projectID" and set(asked["value"]) == {9001, 9002, 9003, 9004}
    assert "Someone else's phase" not in {p.phase for p in result.projects}


@respx.mock
async def test_no_projects_is_an_answer(autotask: Any) -> None:
    respx.get(f"{AUTOTASK_ZONE_URL}/V1.0/Projects/entityInformation/fields").mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "fields_projects.json"))
    )
    respx.post(at_query("Projects")).mock(
        return_value=httpx.Response(200, json=load_fixture("autotask", "tickets_empty.json"))
    )
    phases = respx.post(at_query("Phases")).mock(return_value=httpx.Response(200, json={}))

    result = await list_projects(CLIENT_ID)

    assert result.total == 0 and result.projects == []
    assert phases.call_count == 0  # nothing to ask phases for


@respx.mock
async def test_projects_401(autotask: Any) -> None:
    respx.get(f"{AUTOTASK_ZONE_URL}/V1.0/Projects/entityInformation/fields").mock(
        return_value=httpx.Response(401, json=load_fixture("autotask", "error_401.json"))
    )
    with pytest.raises(AuthError):
        await list_projects(CLIENT_ID)


def test_projects_says_what_it_is_not_for() -> None:
    description = get_tool("list_projects").description
    assert "Not for tickets" in description
    assert get_tool("list_projects").model_facing_schema()["properties"]["state"]["enum"] == [
        "open",
        "scheduled",
        "completed",
        "all",
    ]


# --- NinjaOne: locations, and what a machine is -----------------------------------------------

LOCATIONS_URL = f"{NINJA_BASE_URL}/v2/organization/{NINJA_ORGANIZATION_ID}/locations"


@respx.mock
async def test_locations_come_from_the_organisations_own_path(
    fast_limits: ConnectorLimits,
) -> None:
    respx.post(f"{NINJA_BASE_URL}/ws/oauth/token").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )
    route = respx.get(LOCATIONS_URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("ninjaone", "organization_locations.json")
        )
    )
    async with make_ninja_client(limits=fast_limits) as client:
        placed = await client.list_locations()

    assert [(site.slug, location.name) for site, location in placed] == [
        ("acme-dental", "Main Office"),
        ("acme-dental", "Branch"),
    ]
    assert route.call_count == 1


@respx.mock
async def test_locations_that_are_not_a_list_are_an_error(fast_limits: ConnectorLimits) -> None:
    respx.post(f"{NINJA_BASE_URL}/ws/oauth/token").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )
    respx.get(LOCATIONS_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    async with make_ninja_client(limits=fast_limits) as client:
        with pytest.raises(UpstreamError):
            await client.list_locations()


@pytest.mark.parametrize(
    ("flag", "node_class", "manufacturer", "model", "expected"),
    [
        (False, "WINDOWS_SERVER", "Dell Inc.", "PowerEdge T350", "physical"),
        (True, "WINDOWS_SERVER", "Dell Inc.", "PowerEdge T350", "virtual"),
        (None, "WINDOWS_SERVER", "VMware, Inc.", "VMware Virtual Platform", "virtual"),
        (False, "WINDOWS_SERVER", "Microsoft Corporation", "Virtual Machine", "virtual"),
        (None, "HYPERV_VMM_GUEST", None, None, "virtual"),
        (None, "WINDOWS_SERVER", "HP", "ProLiant DL380", "physical"),
        (None, "WINDOWS_SERVER", None, None, "unknown"),  # nothing reported: never guessed
    ],
)
def test_physical_or_virtual(
    flag: bool | None, node_class: str, manufacturer: str | None, model: str | None, expected: str
) -> None:
    assert (
        classify_machine(
            virtual_flag=flag, node_class=node_class, manufacturer=manufacturer, model=model
        )
        == expected
    )


@pytest.mark.parametrize(
    ("name", "supported"),
    [
        ("Intel(R) Core(TM) i5-6500 CPU @ 3.20GHz", False),
        ("Intel(R) Core(TM) i7-7700 CPU @ 3.60GHz", False),
        ("Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz", True),
        ("11th Gen Intel(R) Core(TM) i7-1185G7 @ 3.00GHz", True),
        ("Intel(R) Core(TM) i9-14900HX", True),
        ("Intel(R) Core(TM) Ultra 7 155U", True),
        ("AMD Ryzen 5 1600 Six-Core Processor", False),
        ("AMD Ryzen 5 2400G with Radeon Vega Graphics", False),
        ("AMD Ryzen 5 PRO 4650U with Radeon Graphics", True),
        ("AMD Ryzen 7 7735HS with Radeon Graphics", True),
        ("Intel(R) Xeon(R) Silver 4210 CPU @ 2.20GHz", None),  # needs the list, not a guess
        ("Intel(R) Celeron(R) N4020 CPU @ 1.10GHz", None),
        (None, None),
    ],
)
def test_processor_generation_is_read_or_left_unknown(
    name: str | None, supported: bool | None
) -> None:
    assert processor_supported(name) is supported


def test_windows11_eligibility_says_what_it_could_not_check() -> None:
    gib = 2**30
    old = windows11_eligibility(
        os_name="Microsoft Windows 10 Pro",
        processor="Intel(R) Core(TM) i5-6500 CPU @ 3.20GHz",
        memory_bytes=16 * gib,
    )
    assert old.status == "ineligible" and "processor" in old.reasons[0]
    assert "TPM" in old.basis  # NinjaOne reports none, and the result says so

    small = windows11_eligibility(
        os_name="Microsoft Windows 10 Pro",
        processor="Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz",
        memory_bytes=3 * gib,
    )
    assert small.status == "ineligible" and "4 GB" in small.reasons[0]

    ready = windows11_eligibility(
        os_name="Microsoft Windows 10 Pro",
        processor="Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz",
        memory_bytes=int(7.8 * gib),
    )
    assert ready.status == "eligible"

    assert (
        windows11_eligibility(
            os_name="Microsoft Windows 10 Pro",
            processor="Intel(R) Xeon(R) E-2224",
            memory_bytes=gib,
        ).status
        == "ineligible"  # memory alone decides it
    )
    unknown = windows11_eligibility(
        os_name="Microsoft Windows 10 Pro", processor="Intel(R) Xeon(R) E-2224", memory_bytes=None
    )
    assert unknown.status == "unknown" and len(unknown.reasons) == 2
    done = windows11_eligibility(
        os_name="Microsoft Windows 11 Pro Edition", processor=None, memory_bytes=None
    )
    assert done.status == "already_windows_11"
    for other in ("Microsoft Windows Server 2022 Standard", "Ubuntu 26.04 LTS"):
        assert (
            windows11_eligibility(os_name=other, processor=None, memory_bytes=None).status
            == "not_windows"
        )


@respx.mock
async def test_get_hardware_joins_three_reports_per_device(
    stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    stub_factory(make_ninja_client(limits=fast_limits, sleep=sleeper))
    respx.post(f"{NINJA_BASE_URL}/ws/oauth/token").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )
    respx.get(f"{NINJA_BASE_URL}/v2/devices").mock(
        side_effect=single_page(load_fixture("ninjaone", "devices.json"))
    )
    operating_systems = load_fixture("ninjaone", "query_operating_systems.json")
    operating_systems["results"][0]["name"] = "Microsoft Windows 10 Pro"  # device 1002
    for report, payload in (
        ("computer-systems", load_fixture("ninjaone", "query_computer_systems.json")),
        ("processors", load_fixture("ninjaone", "query_processors.json")),
        ("operating-systems", operating_systems),
    ):
        respx.get(f"{NINJA_BASE_URL}/v2/queries/{report}").mock(
            return_value=httpx.Response(200, json=payload)
        )

    result = await get_hardware(CLIENT_ID)

    by_device = {row.device: row for row in result.rows}
    assert by_device["TL-AUS-WS07"].kind == "virtual"
    assert by_device["TL-AUS-DC01"].kind == "physical"
    assert by_device["TL-AUS-DC01"].windows11 == "not_windows"  # a server edition
    old = by_device["TL-AUS-WS14"]  # Windows 10 on a sixth-generation Core i5
    assert old.windows11 == "ineligible" and "i5-6500" in old.windows11_reasons[0]
    assert result.kind_counts == {"physical": 3, "virtual": 1}
    assert "TPM" in result.windows11_note

    replace = await get_hardware(CLIENT_ID, windows11="ineligible")
    assert [row.device for row in replace.rows] == ["TL-AUS-WS14"]
    assert replace.filtered_by == {"windows11": "ineligible"}
    virtual = await get_hardware(CLIENT_ID, kind="virtual")
    assert [row.device for row in virtual.rows] == ["TL-AUS-WS07"]

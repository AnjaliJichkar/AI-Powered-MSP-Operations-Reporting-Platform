"""Patch install failures, Windows services, hardware and custom fields.

Fixtures are the live shapes recorded on 2026-09-18; device ids match
`devices.json` so every row resolves through the inventory.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.agent.registry import ToolArgumentError, get_tool, tools_for
from app.connectors.base import ConnectorLimits
from app.connectors.errors import AuthError, RateLimited
from app.connectors.ninjaone.tools import (
    get_hardware,
    list_custom_fields,
    list_patch_installs,
    list_windows_services,
)
from app.models.enums import ConnectorName
from tests.connectors.conftest import (
    NINJA_BASE_URL,
    NINJA_ORGANIZATION_ID,
    RecordingSleep,
    load_fixture,
    make_ninja_client,
    single_page,
    site_ref,
)

CLIENT_ID = str(uuid.uuid4())
SITE_ID = str(uuid.uuid4())
TOKEN_URL = f"{NINJA_BASE_URL}/ws/oauth/token"
DEVICES_URL = f"{NINJA_BASE_URL}/v2/devices"
DEFINITIONS_URL = f"{NINJA_BASE_URL}/v2/device-custom-fields"


def query_url(report: str) -> str:
    return f"{NINJA_BASE_URL}/v2/queries/{report}"


@pytest.fixture
def ninja(stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    """One site in scope, so the scope is single-site as a named site would be."""
    return stub_factory(make_ninja_client(limits=fast_limits, sleep=sleeper))


@pytest.fixture(autouse=True)
def _clock(frozen_clock: dt.datetime) -> None:
    """`days` windows are measured against the frozen NinjaOne clock."""


def mock_tenant() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )
    respx.get(DEVICES_URL).mock(side_effect=single_page(load_fixture("ninjaone", "devices.json")))


def mock_query(report: str, fixture: str = "query_empty.json", status: int = 200) -> respx.Route:
    return respx.get(query_url(report)).mock(
        return_value=httpx.Response(status, json=load_fixture("ninjaone", fixture))
    )


# --- list_patch_installs ------------------------------------------------------


@respx.mock
async def test_failures_are_the_default_newest_first(ninja: Any) -> None:
    mock_tenant()
    route = mock_query("os-patch-installs", "query_os_patch_installs_failed.json")

    result = await list_patch_installs(CLIENT_ID)

    params = route.calls[0].request.url.params
    assert params["status"] == "FAILED"
    assert params["installedAfter"] == "2026-08-31"  # seven days before the frozen clock
    assert result.status == "failed"
    assert result.total == 3
    assert result.status_counts == {"FAILED": 3}
    assert result.devices_affected == 2
    assert [(row.device, row.kb) for row in result.rows] == [
        ("TL-AUS-DC01", "KB5060001"),
        ("TL-AUS-WS14", "KB5060000"),
        ("TL-AUS-WS14", None),  # a driver: the vendor sends an empty kbNumber
    ]
    assert result.rows[0].installed_at == dt.datetime(2026, 9, 7, 3, 0, tzinfo=dt.UTC)


@respx.mock
async def test_what_succeeded_this_week_is_answerable(ninja: Any) -> None:
    mock_tenant()
    payload = load_fixture("ninjaone", "query_os_patch_installs_mixed.json")
    payload["results"] = [row for row in payload["results"] if row["status"] == "INSTALLED"]
    route = respx.get(query_url("os-patch-installs")).mock(
        return_value=httpx.Response(200, json=payload)
    )

    result = await list_patch_installs(CLIENT_ID, status="installed")

    assert route.calls[0].request.url.params["status"] == "INSTALLED"
    assert result.status_counts == {"INSTALLED": 4}
    assert result.devices_affected == 3
    # The per-patch totals are the answer when the rows run to thousands.
    top = result.patches[0]
    assert (top.kb, top.installed, top.failed, top.devices) == ("KB5060000", 2, 0, 2)
    assert result.patches_total == 3
    assert {row.status for row in result.rows} == {"INSTALLED"}


@respx.mock
async def test_all_sends_no_status_and_puts_failures_first(ninja: Any) -> None:
    mock_tenant()
    route = mock_query("os-patch-installs", "query_os_patch_installs_mixed.json")

    result = await list_patch_installs(CLIENT_ID, status="all")

    # The vendor ignores a status it does not know, so "all" is the absence of one.
    assert "status" not in route.calls[0].request.url.params
    assert result.status_counts == {"INSTALLED": 4, "FAILED": 3}
    assert [row.status for row in result.rows[:3]] == ["FAILED"] * 3
    by_kb = {tally.kb: tally for tally in result.patches}
    assert (by_kb["KB5060000"].installed, by_kb["KB5060000"].failed) == (2, 1)
    assert by_kb["KB5060000"].devices == 3
    assert result.patches[0].failed >= result.patches[-1].failed  # most failures first


@respx.mock
async def test_the_status_filter_is_applied_again_on_our_side(ninja: Any) -> None:
    """Live, an unrecognised status is silently ignored and everything comes back."""
    mock_tenant()
    mock_query("os-patch-installs", "query_os_patch_installs_mixed.json")  # unfiltered reply

    failed = await list_patch_installs(CLIENT_ID)
    assert failed.status_counts == {"FAILED": 3}

    succeeded = await list_patch_installs(CLIENT_ID, status="installed")
    assert succeeded.status_counts == {"INSTALLED": 4}


@respx.mock
async def test_an_unknown_install_status_is_refused_not_sent(ninja: Any) -> None:
    """This report *ignores* an unknown status and returns everything.

    A typo would come back as every installation, labelled as whatever was
    asked for. It used to fall through to "all" here for the same reason.
    """
    mock_tenant()
    route = mock_query("os-patch-installs", "query_os_patch_installs_mixed.json")

    with pytest.raises(ValueError, match="failed, installed, all") as refused:
        await list_patch_installs(CLIENT_ID, status="succeeded")  # type: ignore[arg-type]

    assert "Nothing was sent to NinjaOne" in str(refused.value)
    assert route.call_count == 0


def test_only_the_three_statuses_can_be_asked_for() -> None:
    schema = get_tool("list_patch_installs").model_facing_schema()
    assert schema["properties"]["status"]["enum"] == ["failed", "installed", "all"]
    assert schema["properties"]["status"]["default"] == "failed"
    with pytest.raises(ToolArgumentError):
        get_tool("list_patch_installs").validate_arguments({"status": "pending"})


@respx.mock
async def test_no_installations_is_an_answer(ninja: Any) -> None:
    mock_tenant()
    mock_query("os-patch-installs")
    result = await list_patch_installs(CLIENT_ID, days=1)
    assert result.total == 0 and result.devices_affected == 0
    assert result.patches == [] and result.status_counts == {}


async def test_patch_installs_reject_a_nonsense_window(ninja: Any) -> None:
    with pytest.raises(ValueError, match="days must be at least 1"):
        await list_patch_installs(CLIENT_ID, days=0)


@respx.mock
async def test_patch_installs_401(ninja: Any) -> None:
    mock_tenant()
    mock_query("os-patch-installs", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await list_patch_installs(CLIENT_ID)


# --- list_windows_services ----------------------------------------------------


@respx.mock
async def test_stopped_services_put_the_ones_that_should_be_running_first(ninja: Any) -> None:
    mock_tenant()
    route = mock_query("windows-services", "query_windows_services.json")

    result = await list_windows_services(CLIENT_ID, site_id=SITE_ID)

    assert route.calls[0].request.url.params["state"] == "STOPPED"
    assert result.found is True
    assert result.auto_start_not_running == 2
    assert [(row.device, row.service, row.start_type) for row in result.rows] == [
        ("TL-AUS-DC01", "MSSQLSERVER", "AUTO_START_DELAYED"),
        ("TL-AUS-WS14", "Spooler", "AUTO_START"),
        ("TL-AUS-DC01", "RemoteRegistry", "DISABLED"),
        ("TL-AUS-WS14", "wuauserv", "DEMAND_START"),
    ]
    # Compact: the long vendor description is not carried.
    assert "description" not in result.rows[0].model_dump()


@respx.mock
async def test_services_can_be_narrowed_to_a_device_and_a_name(ninja: Any) -> None:
    mock_tenant()
    route = mock_query("windows-services", "query_windows_services.json")

    result = await list_windows_services(
        CLIENT_ID, device_name="tl-aus-ws14", state="any", name="Spooler", site_id=SITE_ID
    )

    params = route.calls[0].request.url.params
    assert params["name"] == "Spooler" and "state" not in params
    assert {row.device for row in result.rows} == {"TL-AUS-WS14"}
    assert result.filtered_by == {"state": "any", "name": "Spooler", "device": "TL-AUS-WS14"}


@respx.mock
async def test_services_refuse_to_read_the_whole_tenant(
    stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental", name="Acme Dental")
    legal = site_ref(77, slug="bright-legal", name="Bright Legal")
    stub_factory(make_ninja_client(limits=fast_limits, sleep=sleeper, sites=[dental, legal]))
    mock_tenant()
    route = mock_query("windows-services", "query_windows_services.json")

    result = await list_windows_services(CLIENT_ID)

    assert result.found is False
    assert "name one end customer" in (result.message or "")
    assert route.call_count == 0


@respx.mock
async def test_services_429(ninja: Any) -> None:
    mock_tenant()
    mock_query("windows-services", "error_429.json", status=429)
    with pytest.raises(RateLimited):
        await list_windows_services(CLIENT_ID, site_id=SITE_ID)


# --- get_hardware ---------------------------------------------------------------


@respx.mock
async def test_hardware_lists_models_and_serials_and_says_age_is_unavailable(ninja: Any) -> None:
    mock_tenant()
    mock_query("computer-systems", "query_computer_systems.json")
    mock_query("processors", "query_processors.json")
    mock_query("operating-systems", "query_operating_systems.json")

    result = await get_hardware(CLIENT_ID)

    assert result.total == 4
    assert result.model_counts == {
        "Dell Inc. OptiPlex 7010": 2,
        "Dell Inc. PowerEdge T350": 1,
        "VMware, Inc. VMware Virtual Platform": 1,
    }
    server = result.rows[0]
    assert (server.device, server.model, server.serial_number) == (
        "TL-AUS-DC01",
        "PowerEdge T350",
        "SRV1234",
    )
    assert server.memory_gb == 68.7
    # Hard rule 7: there is no date to derive an age from, and the tool says so.
    assert result.age_available is False
    assert all(row.age_years is None for row in result.rows)
    assert "cannot be determined" in result.age_note
    # Enrolment is offered, labelled for what it is.
    assert server.enrolled_at == dt.datetime(2023, 1, 1, tzinfo=dt.UTC)
    virtual = [row.device for row in result.rows if row.kind == "virtual"]
    assert virtual == ["TL-AUS-WS07"]


@respx.mock
async def test_an_age_filter_is_refused_rather_than_guessed(ninja: Any) -> None:
    mock_tenant()
    route = mock_query("computer-systems", "query_computer_systems.json")

    result = await get_hardware(CLIENT_ID, older_than_years=4)

    assert result.rows == [] and result.total == 0
    assert "Cannot list hardware older than 4 years" in result.age_note
    assert "lower bound" in result.age_note
    assert route.call_count == 0  # nothing is fetched to then be mis-filtered


@respx.mock
async def test_hardware_401(ninja: Any) -> None:
    mock_tenant()
    mock_query("computer-systems", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await get_hardware(CLIENT_ID)


# --- list_custom_fields -----------------------------------------------------------


def mock_definitions() -> respx.Route:
    return respx.get(DEFINITIONS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "device_custom_fields.json"))
    )


@respx.mock
async def test_without_a_field_the_definitions_are_listed_and_no_device_is_read(
    ninja: Any,
) -> None:
    mock_tenant()
    mock_definitions()
    values = mock_query("custom-fields", "query_custom_fields_asset_tag.json")

    result = await list_custom_fields(CLIENT_ID)

    assert values.call_count == 0
    assert [(d.label, d.type, d.readable) for d in result.definitions] == [
        ("Asset Tag", "TEXT", True),
        ("BitLocker Recovery Key", "TEXT", False),  # plain text, but it is a secret
        ("Decommissioned", "CHECKBOX", True),
        ("Local Admin Password", "TEXT_ENCRYPTED", False),
        ("Primary User", "TEXT", True),
    ]
    assert result.rows == []


@respx.mock
async def test_one_field_is_read_by_name_or_label_and_only_that_field_is_requested(
    ninja: Any,
) -> None:
    mock_tenant()
    mock_definitions()
    values = mock_query("custom-fields", "query_custom_fields_asset_tag.json")

    result = await list_custom_fields(CLIENT_ID, field="asset tag")

    params = values.calls[0].request.url.params
    assert params["fields"] == "assetTag"
    assert "showSecureValues" not in params
    assert result.field == "assetTag"
    assert [(row.device, row.value) for row in result.rows] == [
        ("TL-AUS-DC01", "ACME-0001"),
        ("TL-AUS-WS14", "ACME-0014"),
    ]
    assert result.devices_without_a_value == 2


@respx.mock
@pytest.mark.parametrize("field", ["Local Admin Password", "bitlockerRecoveryKey"])
async def test_a_secret_field_is_refused_and_never_requested(ninja: Any, field: str) -> None:
    mock_tenant()
    mock_definitions()
    values = mock_query("custom-fields", "query_custom_fields_asset_tag.json")

    result = await list_custom_fields(CLIENT_ID, field=field)

    assert values.call_count == 0
    assert result.rows == []
    assert "Desk does not read those" in (result.message or "")


@respx.mock
async def test_an_unknown_field_lists_what_exists(ninja: Any) -> None:
    mock_tenant()
    mock_definitions()
    result = await list_custom_fields(CLIENT_ID, field="warranty end")
    assert "no custom field called 'warranty end'" in (result.message or "")
    assert "Asset Tag" in (result.message or "")


@respx.mock
async def test_custom_fields_429(ninja: Any) -> None:
    mock_tenant()
    respx.get(DEFINITIONS_URL).mock(
        return_value=httpx.Response(429, json=load_fixture("ninjaone", "error_429.json"))
    )
    with pytest.raises(RateLimited):
        await list_custom_fields(CLIENT_ID)


@respx.mock
async def test_a_value_for_a_device_outside_the_inventory_is_dropped_with_an_error(
    ninja: Any, caplog: pytest.LogCaptureFixture
) -> None:
    mock_tenant()
    mock_definitions()
    payload = load_fixture("ninjaone", "query_custom_fields_asset_tag.json")
    payload["results"].append({"deviceId": 9999, "fields": {"assetTag": "GHOST"}})
    respx.get(query_url("custom-fields")).mock(return_value=httpx.Response(200, json=payload))

    with caplog.at_level(logging.ERROR, logger="app.connectors.ninjaone.client"):
        result = await list_custom_fields(CLIENT_ID, field="assetTag")

    assert "GHOST" not in [row.value for row in result.rows]
    assert any("/v2/queries/custom-fields" in r.getMessage() for r in caplog.records)


# --- registration ---------------------------------------------------------------


def test_the_sixteen_ninjaone_tools_are_registered() -> None:
    names = {definition.name for definition in tools_for(ConnectorName.NINJAONE)}
    assert names >= {
        "list_patch_installs",
        "list_windows_services",
        "get_hardware",
        "list_custom_fields",
    }
    assert len(names) == 18  # 16 of its own, plus the two coverage tools registered under it


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("list_patch_installs", {"days", "status", "site"}),
        ("list_windows_services", {"device_name", "state", "name", "site"}),
        ("get_hardware", {"older_than_years", "kind", "windows11", "site"}),
        ("list_custom_fields", {"field", "site"}),
    ],
)
def test_each_tool_exposes_the_arguments_it_should(name: str, expected: set[str]) -> None:
    schema = get_tool(name).model_facing_schema()
    assert set(schema["properties"]) == expected
    assert "client_id" not in schema["properties"]

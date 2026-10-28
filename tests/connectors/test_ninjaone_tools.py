"""The three NinjaOne tools, as the agent loop will call them.

These go through `connector_for`, so they also prove a tool cannot be run
without the factory resolving a client first.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.agent.registry import get_tool, tools_for
from app.connectors.base import ConnectorLimits
from app.connectors.errors import AuthError, RateLimited, UpstreamError
from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.ninjaone.tools import (
    count_devices,
    list_devices,
    list_devices_not_checked_in,
    list_open_alerts,
)
from app.models.enums import ConnectorName
from app.settings import get_settings
from tests.connectors.conftest import (
    FROZEN_NOW,
    NINJA_BASE_URL,
    NINJA_ORGANIZATION_ID,
    RecordingSleep,
    load_fixture,
    make_ninja_client,
    single_page,
    site_ref,
)

CLIENT_ID = str(uuid.uuid4())
TOKEN_URL = f"{NINJA_BASE_URL}/ws/oauth/token"
#: The tools are called without a site here, so they read the tenant listing.
DEVICES_URL = f"{NINJA_BASE_URL}/v2/devices"
ALERTS_URL = f"{NINJA_BASE_URL}/v2/alerts"


@pytest.fixture
def ninja(stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    """Route every `connector_for` call to a NinjaOne client backed by respx."""
    return stub_factory(make_ninja_client(limits=fast_limits, sleep=sleeper))


def mock_token() -> respx.Route:
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )


def mock_devices(fixture_name: str = "devices.json", status: int = 200) -> respx.Route:
    return respx.get(DEVICES_URL).mock(
        side_effect=single_page(load_fixture("ninjaone", fixture_name), status=status)
    )


# --- list_devices ---------------------------------------------------------


@respx.mock
async def test_list_devices_happy_path(ninja: Any) -> None:
    mock_token()
    mock_devices()

    result = await list_devices(CLIENT_ID)

    assert result.client_id == CLIENT_ID
    assert result.sites_covered == ["acme-dental"]
    assert result.device_count == 5
    assert result.truncated is False
    # Sorted by name within the site, so a capped list is deterministic.
    assert [device.name for device in result.devices] == [
        "TL-AUS-DC01",
        "TL-AUS-MAC03",
        "TL-AUS-WS07",
        "TL-AUS-WS14",
        "TL-AUS-WS22",
    ]
    assert result.devices[0].device_class == "WINDOWS_SERVER"


@respx.mock
async def test_list_devices_passes_the_client_id_to_the_factory(ninja: Any) -> None:
    """Hard rule 1: the scope reaches credential resolution, not the vendor call."""
    mock_token()
    mock_devices()

    await list_devices(CLIENT_ID)

    assert ninja.calls == [(CLIENT_ID, ConnectorName.NINJAONE)]


@respx.mock
async def test_list_devices_on_an_empty_organisation(ninja: Any) -> None:
    mock_token()
    mock_devices("devices_empty.json")

    result = await list_devices(CLIENT_ID)

    assert result.device_count == 0
    assert result.devices == []


@respx.mock
async def test_list_devices_reports_a_device_with_no_last_contact(ninja: Any) -> None:
    mock_token()
    mock_devices()

    result = await list_devices(CLIENT_ID)
    mac = next(device for device in result.devices if device.name == "TL-AUS-MAC03")

    assert mac.last_contact is None
    assert mac.last_contact_known is False


@respx.mock
async def test_list_devices_401(ninja: Any) -> None:
    mock_token()
    mock_devices("error_401.json", status=401)

    with pytest.raises(AuthError):
        await list_devices(CLIENT_ID)


@respx.mock
async def test_list_devices_429(ninja: Any) -> None:
    mock_token()
    mock_devices("error_429.json", status=429)

    with pytest.raises(RateLimited):
        await list_devices(CLIENT_ID)


# --- list_devices_not_checked_in ------------------------------------------


@respx.mock
async def test_stale_devices_at_the_default_threshold(
    ninja: Any, frozen_clock: dt.datetime
) -> None:
    mock_token()
    mock_devices()

    result = await list_devices_not_checked_in(CLIENT_ID)

    assert result.threshold_days == 7
    assert result.devices_checked == 5
    assert result.devices_never_seen == 1  # TL-AUS-MAC03 has no lastContact
    assert result.stale_device_count == 2
    # Longest silence first.
    assert [device.name for device in result.devices] == ["TL-AUS-WS22", "TL-AUS-WS14"]
    assert result.devices[0].days_since_contact == pytest.approx(69.17, abs=0.01)
    assert result.devices[1].days_since_contact == pytest.approx(10.08, abs=0.01)
    assert result.checked_at == frozen_clock


@respx.mock
async def test_a_lower_threshold_catches_more_devices(
    ninja: Any, frozen_clock: dt.datetime
) -> None:
    mock_token()
    mock_devices()

    result = await list_devices_not_checked_in(CLIENT_ID, days=3)

    # TL-AUS-WS07 went quiet 4.92 days ago: inside 7, outside 3.
    assert [device.name for device in result.devices] == [
        "TL-AUS-WS22",
        "TL-AUS-WS14",
        "TL-AUS-WS07",
    ]


@respx.mock
async def test_a_high_threshold_can_return_nothing(ninja: Any, frozen_clock: dt.datetime) -> None:
    mock_token()
    mock_devices()

    result = await list_devices_not_checked_in(CLIENT_ID, days=365)

    assert result.stale_device_count == 0
    assert result.devices == []
    # The count of what was examined is still reported, so "none" is meaningful.
    assert result.devices_checked == 5


@respx.mock
async def test_devices_with_no_timestamp_are_never_called_stale(
    ninja: Any, frozen_clock: dt.datetime
) -> None:
    """Hard rule 7: unmeasurable silence is reported as unknown, not as stale."""
    mock_token()
    mock_devices()

    result = await list_devices_not_checked_in(CLIENT_ID, days=1)

    assert "TL-AUS-MAC03" not in [device.name for device in result.devices]
    assert result.devices_never_seen == 1


@respx.mock
async def test_an_empty_organisation_yields_no_stale_devices(
    ninja: Any, frozen_clock: dt.datetime
) -> None:
    mock_token()
    mock_devices("devices_empty.json")

    result = await list_devices_not_checked_in(CLIENT_ID)

    assert result.devices_checked == 0
    assert result.stale_device_count == 0


async def test_a_nonsense_threshold_is_rejected(ninja: Any) -> None:
    with pytest.raises(ValueError, match="days must be at least 1"):
        await list_devices_not_checked_in(CLIENT_ID, days=0)


@respx.mock
async def test_stale_devices_401(ninja: Any) -> None:
    mock_token()
    mock_devices("error_401.json", status=401)

    with pytest.raises(AuthError):
        await list_devices_not_checked_in(CLIENT_ID)


@respx.mock
async def test_stale_devices_429(ninja: Any) -> None:
    mock_token()
    mock_devices("error_429.json", status=429)

    with pytest.raises(RateLimited):
        await list_devices_not_checked_in(CLIENT_ID)


@respx.mock
async def test_truncation_is_reported_rather_than_hidden(
    ninja: Any, frozen_clock: dt.datetime, fast_limits: ConnectorLimits
) -> None:
    """A partial list the model presents as complete would be a fabrication."""
    mock_token()
    page = load_fixture("ninjaone", "devices.json")[:2]
    counter = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        # Always a full page with a fresh cursor, so the page cap is what stops us.
        return httpx.Response(
            200,
            json=[{**device, "id": device["id"] + counter["n"] * 1000} for device in page],
        )

    respx.get(DEVICES_URL).mock(side_effect=responder)

    import dataclasses

    ninja.connector.limits = dataclasses.replace(fast_limits, page_size=2)
    ninja.connector.devices_page_size = 2
    result = await list_devices_not_checked_in(CLIENT_ID)

    assert result.truncated is True


# --- filters and the row cap ----------------------------------------------


@respx.mock
async def test_list_devices_reports_the_os_family_and_compact_fields(ninja: Any) -> None:
    mock_token()
    mock_devices()

    result = await list_devices(CLIENT_ID)

    by_name = {device.name: device for device in result.devices}
    assert by_name["TL-AUS-DC01"].os == "Windows"
    assert by_name["TL-AUS-MAC03"].os == "macOS"
    # Only the fields the prompt needs: no vendor ids in a list row.
    assert set(result.devices[0].model_dump()) == {
        "site",
        "site_slug",
        "name",
        "device_class",
        "os",
        "offline",
        "last_contact",
        "last_contact_known",
    }
    assert result.total == result.returned == result.device_count == 5
    assert result.truncated is False
    assert result.truncation_note is None


@respx.mock
@pytest.mark.parametrize(
    ("node_class", "expected"),
    [
        ("server", ["TL-AUS-DC01"]),
        ("workstation", ["TL-AUS-WS07", "TL-AUS-WS14", "TL-AUS-WS22"]),
        ("mac", ["TL-AUS-MAC03"]),
        ("WINDOWS_SERVER", ["TL-AUS-DC01"]),
        ("printer", []),
    ],
)
async def test_list_devices_filters_by_node_class(
    ninja: Any, node_class: str, expected: list[str]
) -> None:
    mock_token()
    mock_devices()

    result = await list_devices(CLIENT_ID, node_class=node_class)

    assert [device.name for device in result.devices] == expected
    assert result.device_count == len(expected)
    assert result.filtered_by == {"node_class": node_class}


@respx.mock
@pytest.mark.parametrize(
    ("os", "expected_count"), [("windows", 4), ("macos", 1), ("mac", 1), ("linux", 0)]
)
async def test_list_devices_filters_by_os_family(ninja: Any, os: str, expected_count: int) -> None:
    mock_token()
    mock_devices()

    result = await list_devices(CLIENT_ID, os=os)

    assert result.device_count == expected_count
    assert result.filtered_by == {"os": os}


@respx.mock
async def test_the_row_cap_reports_the_total_and_says_it_truncated(
    ninja: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capped list says how much it left out; the model must not call it complete."""
    monkeypatch.setenv("TOOL_MAX_ROWS", "2")
    get_settings.cache_clear()
    mock_token()
    mock_devices()

    result = await list_devices(CLIENT_ID)

    assert result.total == result.device_count == 5
    assert result.returned == len(result.devices) == 2
    assert result.truncated is True
    assert result.truncation_note is not None
    assert "first 2 of 5 devices" in result.truncation_note
    # Sorted by name within the site, so the cap is deterministic.
    assert [device.name for device in result.devices] == ["TL-AUS-DC01", "TL-AUS-MAC03"]


@respx.mock
async def test_the_row_cap_applies_to_stale_devices_and_alerts(
    ninja: Any, frozen_clock: dt.datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOOL_MAX_ROWS", "1")
    get_settings.cache_clear()
    mock_token()
    mock_devices()
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts.json"))
    )

    stale = await list_devices_not_checked_in(CLIENT_ID)
    assert stale.stale_device_count == stale.total == 2
    assert stale.returned == 1 and stale.truncated is True

    alerts = await list_open_alerts(CLIENT_ID)
    assert alerts.alert_count == alerts.total == 2
    assert alerts.returned == 1 and alerts.truncated is True


# --- count_devices --------------------------------------------------------


@respx.mock
async def test_count_devices_by_node_class(ninja: Any) -> None:
    mock_token()
    mock_devices()

    result = await count_devices(CLIENT_ID, group_by="node_class")

    assert result.total == 5
    assert result.counts == {"WINDOWS_WORKSTATION": 3, "MAC": 1, "WINDOWS_SERVER": 1}
    assert result.truncated is False
    # Numbers only: a count result carries no device rows at all.
    assert "devices" not in result.model_dump()


@respx.mock
async def test_count_devices_by_os(ninja: Any) -> None:
    mock_token()
    mock_devices()

    result = await count_devices(CLIENT_ID, group_by="os")

    assert result.counts == {"Windows": 4, "macOS": 1}


@respx.mock
async def test_count_devices_by_site_across_two_sites(
    stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    """One tenant listing, with every device counted under the site its org id maps to."""
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental", name="Acme Dental")
    legal = site_ref(43, slug="bright-legal", name="Bright Legal")
    stub_factory(make_ninja_client(limits=fast_limits, sleep=sleeper, sites=[dental, legal]))
    mock_token()
    fixture = load_fixture("ninjaone", "devices.json")
    legal_devices = [
        {**device, "id": device["id"] + 500, "organizationId": 43} for device in fixture[:2]
    ]
    respx.get(DEVICES_URL).mock(side_effect=single_page(fixture + legal_devices))

    result = await count_devices(CLIENT_ID)

    assert result.group_by == "site"
    assert result.sites_covered == ["acme-dental", "bright-legal"]
    assert result.counts == {"Acme Dental": 5, "Bright Legal": 2}
    assert result.total == 7


@respx.mock
async def test_count_devices_filters_before_grouping(
    stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    """ "How many servers does each customer have" is one call, not a capped device list.

    Found by the selection check: with no class filter the count tool could not
    answer a two-way question, so the model fell back to `list_devices`.
    """
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental", name="Acme Dental")
    legal = site_ref(43, slug="bright-legal", name="Bright Legal")
    stub_factory(make_ninja_client(limits=fast_limits, sleep=sleeper, sites=[dental, legal]))
    mock_token()
    fixture = load_fixture("ninjaone", "devices.json")
    legal_devices = [
        {**device, "id": device["id"] + 500, "organizationId": 43, "nodeClass": "WINDOWS_SERVER"}
        for device in fixture[:2]
    ]
    respx.get(DEVICES_URL).mock(side_effect=single_page(fixture + legal_devices))

    servers = await count_devices(CLIENT_ID, group_by="site", node_class="server")

    assert servers.counts == {"Bright Legal": 2, "Acme Dental": 1}
    assert servers.total == 3
    assert servers.filtered_by == {"node_class": "server"}

    macs = await count_devices(CLIENT_ID, group_by="site", os="macos")
    assert macs.counts == {"Acme Dental": 1}
    assert macs.filtered_by == {"os": "macos"}


@respx.mock
async def test_count_devices_on_an_empty_organisation(ninja: Any) -> None:
    mock_token()
    mock_devices("devices_empty.json")

    result = await count_devices(CLIENT_ID, group_by="os")

    assert result.total == 0
    assert result.counts == {}


@respx.mock
async def test_count_devices_401(ninja: Any) -> None:
    mock_token()
    mock_devices("error_401.json", status=401)

    with pytest.raises(AuthError):
        await count_devices(CLIENT_ID)


def test_count_devices_offers_only_the_three_groupings() -> None:
    schema = get_tool("count_devices").model_facing_schema()
    assert schema["properties"]["group_by"]["enum"] == ["site", "node_class", "os"]
    assert set(schema["properties"]) == {"group_by", "node_class", "os", "site"}


# --- list_open_alerts -----------------------------------------------------


@respx.mock
async def test_list_open_alerts_happy_path(ninja: Any) -> None:
    mock_token()
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts.json"))
    )

    result = await list_open_alerts(CLIENT_ID)

    assert result.alert_count == 2
    assert result.sites_covered == ["acme-dental"]

    # Newest first: the disk alert was raised after the offline one.
    disk, offline = result.alerts
    assert offline.device_name == "TL-AUS-WS14"
    assert offline.subject == "Agent offline"
    assert offline.severity == "MAJOR"
    assert offline.priority == "HIGH"
    assert offline.created_at == FROZEN_NOW - dt.timedelta(hours=240)
    assert disk.condition == "Disk Free Space"
    assert disk.message == "Volume C: is at 96% capacity"


@respx.mock
async def test_no_open_alerts_is_an_answer_not_a_failure(ninja: Any) -> None:
    mock_token()
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts_empty.json"))
    )

    result = await list_open_alerts(CLIENT_ID)

    assert result.alert_count == 0
    assert result.alerts == []


@respx.mock
async def test_alerts_belonging_to_another_client_never_reach_the_model(ninja: Any) -> None:
    mock_token()
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "alerts_foreign_org.json"))
    )

    result = await list_open_alerts(CLIENT_ID)

    assert result.alert_count == 1
    assert "OTHER-MSP-WS01" not in str(result.model_dump())


@respx.mock
async def test_list_open_alerts_401(ninja: Any) -> None:
    mock_token()
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(401, json=load_fixture("ninjaone", "error_401.json"))
    )

    with pytest.raises(AuthError):
        await list_open_alerts(CLIENT_ID)


@respx.mock
async def test_list_open_alerts_429(ninja: Any) -> None:
    mock_token()
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(429, json=load_fixture("ninjaone", "error_429.json"))
    )

    with pytest.raises(RateLimited):
        await list_open_alerts(CLIENT_ID)


@respx.mock
async def test_list_open_alerts_upstream_failure(ninja: Any) -> None:
    mock_token()
    respx.get(ALERTS_URL).mock(
        return_value=httpx.Response(500, json=load_fixture("ninjaone", "error_500.json"))
    )

    with pytest.raises(UpstreamError):
        await list_open_alerts(CLIENT_ID)


# --- registration ---------------------------------------------------------


def test_the_core_ninjaone_tools_are_registered() -> None:
    """The full set, report tools included, is asserted in test_ninjaone_reports.py."""
    names = {definition.name for definition in tools_for(ConnectorName.NINJAONE)}
    assert {
        "list_devices",
        "count_devices",
        "list_devices_not_checked_in",
        "list_open_alerts",
    } <= names


@pytest.mark.parametrize(
    "name", ["list_devices", "count_devices", "list_devices_not_checked_in", "list_open_alerts"]
)
def test_every_tool_requires_a_client_id(name: str) -> None:
    schema = get_tool(name).input_schema
    assert "client_id" in schema["required"]
    assert schema["properties"]["client_id"]["type"] == "string"


def test_the_days_parameter_is_optional_and_typed() -> None:
    schema = get_tool("list_devices_not_checked_in").input_schema
    assert schema["properties"]["days"] == {"default": 7, "title": "Days", "type": "integer"}
    assert "days" not in schema["required"]


@pytest.mark.parametrize(
    "name", ["list_devices", "count_devices", "list_devices_not_checked_in", "list_open_alerts"]
)
def test_every_tool_describes_itself_to_the_model(name: str) -> None:
    definition = get_tool(name)
    assert len(definition.description) > 80
    assert definition.to_anthropic_schema()["name"] == name


def test_a_server_filter_means_what_the_review_means_by_a_server() -> None:
    """Chat's "server" and the review's server count must agree: a virtual server
    is a server. They disagreed until 2026-09-29 (a VM guest was left out of chat's
    answer while the deck counted it), which is two numbers for one question."""
    from app.connectors.ninjaone.hardware import device_role
    from app.connectors.ninjaone.tools import _node_class_matcher

    matches = _node_class_matcher("server")
    classes = [
        "WINDOWS_SERVER",
        "LINUX_SERVER",
        "VMWARE_VM_GUEST",
        "HYPERV_VMM_GUEST",
        "WINDOWS_WORKSTATION",
        "MAC",
        "NMS_SWITCH",
    ]
    for node_class in classes:
        device = NinjaDevice(id=1, nodeClass=node_class)
        by_role = device_role(None, node_class).role == "server"
        assert matches(device) is by_role, node_class
    # The other shorthands are untouched.
    assert _node_class_matcher("workstation")(NinjaDevice(id=1, nodeClass="WINDOWS_WORKSTATION"))
    assert not _node_class_matcher("workstation")(NinjaDevice(id=1, nodeClass="WINDOWS_SERVER"))
    assert _node_class_matcher("switch")(NinjaDevice(id=1, nodeClass="NMS_SWITCH"))

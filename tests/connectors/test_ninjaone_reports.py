"""The NinjaOne report tools: device detail, patches, reboots, disks, AV, software, users, activity.

Every report row names a device and nothing else, so each tool goes through
the inventory to place it (`place_by_device`) before the allowlist guard. The
fixtures are the live shapes recorded on 2026-09-17; device ids match
`devices.json` so the inventory resolves them.
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
    get_device,
    list_antivirus_status,
    list_device_activities,
    list_disk_space,
    list_logged_on_users,
    list_patch_status,
    list_pending_reboot,
    list_software,
)
from app.models.enums import ConnectorName
from app.settings import get_settings
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
TOKEN_URL = f"{NINJA_BASE_URL}/ws/oauth/token"
DEVICES_URL = f"{NINJA_BASE_URL}/v2/devices"


def query_url(report: str) -> str:
    return f"{NINJA_BASE_URL}/v2/queries/{report}"


@pytest.fixture
def ninja(stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    return stub_factory(make_ninja_client(limits=fast_limits, sleep=sleeper))


@pytest.fixture(autouse=True)
def _tenant(frozen_clock: dt.datetime) -> None:
    """Every report tool needs the inventory to place its rows; the clock is frozen."""


def mock_tenant() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "token.json"))
    )
    respx.get(DEVICES_URL).mock(side_effect=single_page(load_fixture("ninjaone", "devices.json")))


def mock_query(report: str, fixture: str = "query_empty.json", status: int = 200) -> respx.Route:
    return respx.get(query_url(report)).mock(
        return_value=httpx.Response(status, json=load_fixture("ninjaone", fixture))
    )


# --- get_device -----------------------------------------------------------


@respx.mock
async def test_get_device_returns_the_detail_the_spec_does_not_list(ninja: Any) -> None:
    mock_tenant()
    detail_route = respx.get(f"{NINJA_BASE_URL}/v2/device/1002").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "device_detail.json"))
    )

    result = await get_device(CLIENT_ID, "tl-aus-ws14")

    assert result.found is True
    assert detail_route.call_count == 1
    assert result.site_slug == "acme-dental"
    assert result.os_name == "Microsoft Windows 11 Pro Edition"
    assert result.os_build == "22631"
    assert result.needs_reboot is True
    assert result.last_logged_in_user == "ACME\\sarah.chen"
    assert result.ip_addresses[0] == "10.20.30.14"
    assert result.serial_number == "ABC1234"
    assert result.memory_gb == 17.2
    assert result.volumes[0].volume == "C:"
    assert result.volumes[0].free_percent == 4.0
    assert result.agent_version is None


@respx.mock
async def test_get_device_with_an_ambiguous_name_lists_the_candidates(ninja: Any) -> None:
    mock_tenant()
    detail = respx.get(url__regex=r".*/v2/device/\d+$").mock(
        return_value=httpx.Response(200, json={})
    )

    result = await get_device(CLIENT_ID, "TL-AUS-WS")

    assert result.found is False
    assert detail.call_count == 0  # no vendor call until the name is unambiguous
    assert sorted(result.candidates) == [
        "TL-AUS-WS07 (acme-dental)",
        "TL-AUS-WS14 (acme-dental)",
        "TL-AUS-WS22 (acme-dental)",
    ]
    assert "3 devices match" in (result.message or "")


@respx.mock
async def test_get_device_with_no_match_says_so(ninja: Any) -> None:
    mock_tenant()

    result = await get_device(CLIENT_ID, "no-such-box")

    assert result.found is False
    assert result.candidates == []
    assert "no device named like" in (result.message or "")


@respx.mock
async def test_get_device_refuses_a_detail_from_another_organisation(ninja: Any) -> None:
    """The detail endpoint is trusted no more than a listing: its org is checked."""
    mock_tenant()
    foreign = {**load_fixture("ninjaone", "device_detail.json"), "organizationId": 999}
    respx.get(f"{NINJA_BASE_URL}/v2/device/1002").mock(
        return_value=httpx.Response(200, json=foreign)
    )

    from app.connectors.errors import UpstreamError

    with pytest.raises(UpstreamError, match="did not resolve to site"):
        await get_device(CLIENT_ID, "TL-AUS-WS14")


@respx.mock
async def test_get_device_401(ninja: Any) -> None:
    mock_tenant()
    respx.get(f"{NINJA_BASE_URL}/v2/device/1002").mock(
        return_value=httpx.Response(401, json=load_fixture("ninjaone", "error_401.json"))
    )
    with pytest.raises(AuthError):
        await get_device(CLIENT_ID, "TL-AUS-WS14")


# --- list_patch_status ----------------------------------------------------


def mock_waiting_list(report: str, fixture: str) -> respx.Route:
    """The report as the live vendor filters it.

    `status`, `type` and `severity` are matched exactly and case-sensitively, one
    value each, and a value the vendor does not know returns **no rows** — all
    as observed on 2026-09-18 (api-notes, "Patch report filters").
    """
    payload = load_fixture("ninjaone", fixture)

    def answer(request: httpx.Request) -> httpx.Response:
        rows = payload["results"]
        for param in ("status", "type", "severity"):
            if param in request.url.params:
                rows = [row for row in rows if row.get(param) == request.url.params[param]]
        return httpx.Response(200, json={**payload, "results": rows})

    return respx.get(query_url(report)).mock(side_effect=answer)


def statuses_sent(route: respx.Route) -> list[str | None]:
    return [call.request.url.params.get("status") for call in route.calls]


@respx.mock
async def test_the_default_is_what_is_pending_not_what_was_rejected(ninja: Any) -> None:
    mock_tenant()
    os_route = mock_waiting_list("os-patches", "query_os_patches.json")
    sw_route = mock_waiting_list("software-patches", "query_software_patches.json")

    result = await list_patch_status(CLIENT_ID)

    # One request per status: the vendor takes a single value (two return no rows).
    assert statuses_sent(os_route) == ["APPROVED", "MANUAL"]
    assert statuses_sent(sw_route) == ["APPROVED", "MANUAL"]
    assert result.status == "pending"
    assert result.kinds_covered == ["os", "software"]
    assert result.total == 9
    assert result.status_counts == {"APPROVED": 8, "MANUAL": 1}
    assert "REJECTED" not in {row.status for row in result.rows}
    # Awaiting approval first, then the most severe.
    assert (result.rows[0].status, result.rows[0].patch) == ("MANUAL", "Realtek - Audio - 6.0.9")
    assert (result.rows[1].severity, result.rows[1].kb) == ("CRITICAL", "KB5060001")


@respx.mock
async def test_the_totals_say_which_patch_is_waiting_on_how_many_machines(ninja: Any) -> None:
    mock_tenant()
    mock_waiting_list("os-patches", "query_os_patches.json")
    mock_waiting_list("software-patches", "query_software_patches.json")

    result = await list_patch_status(CLIENT_ID)

    top = result.patches[0]
    assert (top.kb, top.severity, top.devices, top.sites) == ("KB5060001", "CRITICAL", 1, 1)
    by_patch = {(tally.kind, tally.kb or tally.patch): tally for tally in result.patches}
    assert by_patch[("os", "KB5060000")].devices == 2
    assert by_patch[("software", "Google Chrome 129.0")].devices == 2
    assert result.patches_total == 7
    assert result.severity_counts == {
        "IMPORTANT": 4,
        "RECOMMENDED": 2,
        "CRITICAL": 2,  # one Windows patch, one third-party
        "OPTIONAL": 1,
    }
    assert result.devices_affected == 4
    (site,) = result.site_counts
    assert (site.site_slug, site.patches, site.devices) == ("acme-dental", 9, 4)


@respx.mock
async def test_the_totals_are_computed_before_the_row_cap(
    ninja: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOOL_MAX_ROWS", "2")
    get_settings.cache_clear()
    mock_tenant()
    mock_waiting_list("os-patches", "query_os_patches.json")
    mock_waiting_list("software-patches", "query_software_patches.json")

    try:
        result = await list_patch_status(CLIENT_ID)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    assert (result.returned, result.total, result.truncated) == (2, 9, True)
    assert result.patches_total == 7
    assert sum(result.status_counts.values()) == 9
    assert result.site_counts[0].patches == 9


@respx.mock
async def test_rejected_patches_only_on_request(ninja: Any) -> None:
    mock_tenant()
    os_route = mock_waiting_list("os-patches", "query_os_patches.json")
    sw_route = mock_waiting_list("software-patches", "query_software_patches.json")

    result = await list_patch_status(CLIENT_ID, status="rejected")

    assert statuses_sent(os_route) == ["REJECTED"]
    assert statuses_sent(sw_route) == ["REJECTED"]
    assert result.status_counts == {"REJECTED": 2}
    assert {row.patch for row in result.rows} == {"Intel - Net - 1.2.3.4", "7-Zip 24.08"}


@respx.mock
async def test_all_sends_no_status(ninja: Any) -> None:
    mock_tenant()
    os_route = mock_waiting_list("os-patches", "query_os_patches.json")
    mock_waiting_list("software-patches", "query_software_patches.json")

    result = await list_patch_status(CLIENT_ID, status="all")

    assert statuses_sent(os_route) == [None]
    assert result.total == 11


@respx.mock
async def test_severity_and_category_go_to_the_vendor_in_its_own_spelling(ninja: Any) -> None:
    mock_tenant()
    os_route = mock_waiting_list("os-patches", "query_os_patches.json")
    sw_route = mock_waiting_list("software-patches", "query_software_patches.json")

    result = await list_patch_status(CLIENT_ID, severity="critical", category="security")

    params = os_route.calls[0].request.url.params
    # Upper case exactly: live, `severity=critical` returned no rows at all.
    assert (params["severity"], params["type"], params["status"]) == (
        "CRITICAL",
        "SECURITY_UPDATES",
        "APPROVED",
    )
    assert [row.kb for row in result.rows] == ["KB5060001"]
    assert result.filtered_by == {"severity": "critical", "category": "security"}
    # Severity and category are Windows-patch concepts: third-party is left out, and said so.
    assert sw_route.call_count == 0
    assert result.kinds_covered == ["os"]
    assert "not read" in (result.coverage_note or "")


@respx.mock
async def test_kb_is_filtered_here_because_the_vendor_cannot(ninja: Any) -> None:
    mock_tenant()
    os_route = mock_waiting_list("os-patches", "query_os_patches.json")
    mock_waiting_list("software-patches", "query_software_patches.json")

    result = await list_patch_status(CLIENT_ID, kb="kb 5060000")

    # No KB parameter exists; live, sending one was ignored and everything came back.
    assert not {"kb", "kbNumber"} & set(os_route.calls[0].request.url.params)
    assert result.filtered_by == {"kb": "KB5060000"}
    assert sorted(row.device for row in result.rows) == ["TL-AUS-WS14", "TL-AUS-WS22"]
    assert result.patches[0].devices == 2

    bare = await list_patch_status(CLIENT_ID, kb="5060000")
    assert bare.total == 2


@respx.mock
async def test_a_kb_named_only_in_the_patch_title_still_matches(ninja: Any) -> None:
    mock_tenant()
    payload = load_fixture("ninjaone", "query_os_patches.json")
    for row in payload["results"]:
        row.pop("kbNumber", None)  # about half the live rows carry no kbNumber field
    respx.get(query_url("os-patches")).mock(return_value=httpx.Response(200, json=payload))

    result = await list_patch_status(CLIENT_ID, kb="KB5060001", status="all")

    assert [row.device for row in result.rows] == ["TL-AUS-DC01"]


@pytest.mark.parametrize(
    ("arguments", "accepted"),
    [
        ({"severity": "severe"}, "critical, important, optional"),
        ({"severity": "CRITICAL!"}, "critical, important, optional"),
        ({"category": "SECURITY_UPDATES"}, "security, rollup, feature, driver"),
        ({"status": "failed"}, "pending, rejected, all"),
    ],
)
@respx.mock
async def test_an_unknown_filter_value_is_refused_not_sent(
    ninja: Any, arguments: dict[str, Any], accepted: str
) -> None:
    """This report answers an unknown value with no rows, which reads as "fully patched"."""
    mock_tenant()
    os_route = mock_waiting_list("os-patches", "query_os_patches.json")
    sw_route = mock_waiting_list("software-patches", "query_software_patches.json")

    with pytest.raises(ValueError, match=accepted) as refused:
        await list_patch_status(CLIENT_ID, **arguments)

    assert "Nothing was sent to NinjaOne" in str(refused.value)
    assert os_route.call_count == 0 and sw_route.call_count == 0


async def test_a_kb_that_is_not_a_kb_is_refused(ninja: Any) -> None:
    with pytest.raises(ValueError, match="kb must look like KB5060000"):
        await list_patch_status(CLIENT_ID, kb="the September update")


@respx.mock
async def test_every_filter_is_applied_again_on_our_side(ninja: Any) -> None:
    """A vendor that ignored the filters must not turn them into a lie, nor double the rows."""
    mock_tenant()
    mock_query("os-patches", "query_os_patches.json")  # unfiltered, whatever is asked
    mock_query("software-patches", "query_software_patches.json")

    pending = await list_patch_status(CLIENT_ID)
    assert pending.total == 9  # two requests per report returned the same rows: counted once
    assert "REJECTED" not in pending.status_counts

    critical = await list_patch_status(CLIENT_ID, severity="critical")
    assert [row.kb for row in critical.rows] == ["KB5060001"]


def test_the_model_is_offered_the_known_values_and_no_others() -> None:
    schema = get_tool("list_patch_status").model_facing_schema()
    properties = schema["properties"]
    assert set(properties) == {"status", "severity", "category", "kb", "site"}
    assert properties["status"]["enum"] == ["pending", "rejected", "all"]
    assert properties["status"]["default"] == "pending"
    description = get_tool("list_patch_status").description
    assert "list_patch_installs" in description
    with pytest.raises(ToolArgumentError, match="critical"):
        get_tool("list_patch_status").validate_arguments({"severity": "severe"})


@respx.mock
async def test_patch_status_429(ninja: Any) -> None:
    mock_tenant()
    mock_query("os-patches", "error_429.json", status=429)
    with pytest.raises(RateLimited):
        await list_patch_status(CLIENT_ID)


# --- list_pending_reboot --------------------------------------------------


@respx.mock
async def test_pending_reboot_lists_needs_reboot_devices_longest_up_first(ninja: Any) -> None:
    mock_tenant()
    mock_query("operating-systems", "query_operating_systems.json")

    result = await list_pending_reboot(CLIENT_ID)

    assert result.devices_checked == 3
    assert [row.device for row in result.rows] == ["TL-AUS-WS14", "TL-AUS-WS07"]
    assert result.rows[0].days_since_boot == pytest.approx(14.8, abs=0.1)
    assert result.rows[0].os_name == "Microsoft Windows 11 Pro Edition"


@respx.mock
async def test_pending_reboot_empty(ninja: Any) -> None:
    mock_tenant()
    mock_query("operating-systems")
    result = await list_pending_reboot(CLIENT_ID)
    assert result.total == 0
    assert result.devices_checked == 0


@respx.mock
async def test_pending_reboot_401(ninja: Any) -> None:
    mock_tenant()
    mock_query("operating-systems", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await list_pending_reboot(CLIENT_ID)


# --- list_disk_space ------------------------------------------------------


@respx.mock
async def test_disk_space_reports_fixed_disks_under_the_threshold_fullest_first(
    ninja: Any,
) -> None:
    mock_tenant()
    mock_query("volumes", "query_volumes.json")

    result = await list_disk_space(CLIENT_ID)

    # Removable media and the zero-capacity partition are not counted.
    assert result.volumes_checked == 3
    assert [(row.device, row.volume, row.free_percent) for row in result.rows] == [
        ("TL-AUS-WS14", "C:", 4.0),
        ("TL-AUS-DC01", "D:", 4.5),
    ]
    assert result.rows[0].capacity_gb == 511.0


@respx.mock
async def test_disk_space_threshold_widens_the_net(ninja: Any) -> None:
    mock_tenant()
    mock_query("volumes", "query_volumes.json")
    result = await list_disk_space(CLIENT_ID, below_percent=70)
    assert [row.volume for row in result.rows] == ["C:", "D:", "C:"]


async def test_disk_space_rejects_a_nonsense_threshold(ninja: Any) -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        await list_disk_space(CLIENT_ID, below_percent=0)


@respx.mock
async def test_disk_space_429(ninja: Any) -> None:
    mock_tenant()
    mock_query("volumes", "error_429.json", status=429)
    with pytest.raises(RateLimited):
        await list_disk_space(CLIENT_ID)


# --- list_antivirus_status ------------------------------------------------


@respx.mock
async def test_antivirus_unhealthy_only_by_default(ninja: Any) -> None:
    mock_tenant()
    mock_query("antivirus-status", "query_antivirus_status.json")

    result = await list_antivirus_status(CLIENT_ID)

    assert result.devices_checked == 3
    assert result.product_counts == {
        "Bitdefender Endpoint Security Tools Antimalware": 3,
        "Microsoft Defender Antivirus": 1,
    }
    assert [(row.device, row.product, row.state, row.definitions) for row in result.rows] == [
        ("TL-AUS-DC01", "Microsoft Defender Antivirus", "OFF", "Up-to-Date"),
        ("TL-AUS-WS14", "Bitdefender Endpoint Security Tools Antimalware", "ON", "Out-of-Date"),
    ]
    assert all(row.healthy is False for row in result.rows)


@respx.mock
async def test_antivirus_full_picture(ninja: Any) -> None:
    mock_tenant()
    mock_query("antivirus-status", "query_antivirus_status.json")
    result = await list_antivirus_status(CLIENT_ID, unhealthy_only=False)
    assert result.total == 4
    assert sum(row.healthy for row in result.rows) == 2


@respx.mock
async def test_antivirus_401(ninja: Any) -> None:
    mock_tenant()
    mock_query("antivirus-status", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await list_antivirus_status(CLIENT_ID)


# --- list_software --------------------------------------------------------


@respx.mock
async def test_software_matches_a_partial_name_case_insensitively(ninja: Any) -> None:
    mock_tenant()
    mock_query("software", "query_software.json")

    result = await list_software(CLIENT_ID, "chrome")

    assert result.rows_scanned == 3
    assert [(row.device, row.version) for row in result.rows] == [
        ("TL-AUS-WS07", "127.0.6533.100"),
        ("TL-AUS-WS14", "129.0.6668.59"),
    ]
    assert result.rows[0].publisher == "Google LLC"
    assert result.rows[0].install_date == "2026-07-02"


@respx.mock
async def test_software_no_match_is_an_answer(ninja: Any) -> None:
    mock_tenant()
    mock_query("software", "query_software.json")
    result = await list_software(CLIENT_ID, "teamviewer")
    assert result.total == 0
    assert result.rows_scanned == 3


async def test_software_requires_a_name(ninja: Any) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        await list_software(CLIENT_ID, "   ")


@respx.mock
async def test_software_429(ninja: Any) -> None:
    mock_tenant()
    mock_query("software", "error_429.json", status=429)
    with pytest.raises(RateLimited):
        await list_software(CLIENT_ID, "chrome")


# --- list_logged_on_users -------------------------------------------------


@respx.mock
async def test_logged_on_users_most_recent_first(ninja: Any) -> None:
    mock_tenant()
    mock_query("logged-on-users", "query_logged_on_users.json")

    result = await list_logged_on_users(CLIENT_ID)

    assert [(row.device, row.user_name) for row in result.rows] == [
        ("TL-AUS-WS07", "ACME\\d.okafor"),
        ("TL-AUS-WS14", "ACME\\sarah.chen"),
        ("TL-AUS-DC01", "ACME\\svc-backup"),
    ]
    assert result.rows[0].logon_time == dt.datetime(2026, 9, 7, 9, 56, 40, tzinfo=dt.UTC)


@respx.mock
async def test_logged_on_users_filters_by_user(ninja: Any) -> None:
    mock_tenant()
    mock_query("logged-on-users", "query_logged_on_users.json")
    result = await list_logged_on_users(CLIENT_ID, user="sarah")
    assert [row.device for row in result.rows] == ["TL-AUS-WS14"]
    assert result.user_searched == "sarah"


@respx.mock
async def test_logged_on_users_401(ninja: Any) -> None:
    mock_tenant()
    mock_query("logged-on-users", "error_401.json", status=401)
    with pytest.raises(AuthError):
        await list_logged_on_users(CLIENT_ID)


# --- list_device_activities -----------------------------------------------


@respx.mock
async def test_device_activities_newest_first_within_the_window(ninja: Any) -> None:
    mock_tenant()
    route = respx.get(f"{NINJA_BASE_URL}/v2/device/1002/activities").mock(
        return_value=httpx.Response(200, json=load_fixture("ninjaone", "device_activities.json"))
    )

    result = await list_device_activities(CLIENT_ID, "TL-AUS-WS14", days=7)

    assert result.found is True
    assert result.device == "TL-AUS-WS14"
    assert route.calls[0].request.url.params["startTime"].startswith("2026-08-31")
    assert [row.status for row in result.rows] == [
        "Session terminated",
        "Session started",
        "Software Updated",
        "Completed",
    ]
    assert result.rows[2].message == "Google Chrome updated to 129.0.6668.59"
    assert result.truncated is False


@respx.mock
async def test_device_activities_for_an_unknown_device_says_so(ninja: Any) -> None:
    mock_tenant()
    result = await list_device_activities(CLIENT_ID, "nope")
    assert result.found is False
    assert result.rows == []


@respx.mock
async def test_device_activities_429(ninja: Any) -> None:
    mock_tenant()
    respx.get(f"{NINJA_BASE_URL}/v2/device/1002/activities").mock(
        return_value=httpx.Response(429, json=load_fixture("ninjaone", "error_429.json"))
    )
    with pytest.raises(RateLimited):
        await list_device_activities(CLIENT_ID, "TL-AUS-WS14")


# --- placing rows through the inventory -----------------------------------


@respx.mock
async def test_a_report_row_for_a_device_not_in_the_inventory_is_dropped_with_an_error(
    ninja: Any, caplog: pytest.LogCaptureFixture
) -> None:
    mock_tenant()
    rows = load_fixture("ninjaone", "query_logged_on_users.json")
    rows["results"].append({"userName": "ACME\\ghost", "logonTime": 1788779999.0, "deviceId": 9999})
    respx.get(query_url("logged-on-users")).mock(return_value=httpx.Response(200, json=rows))

    with caplog.at_level(logging.ERROR, logger="app.connectors.ninjaone.client"):
        result = await list_logged_on_users(CLIENT_ID)

    assert result.total == 3
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "/v2/queries/logged-on-users" in errors[0].getMessage()


@respx.mock
async def test_a_named_site_narrows_the_report_with_df(
    stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental", name="Acme Dental")
    legal = site_ref(77, slug="bright-legal", name="Bright Legal")
    stub_factory(
        make_ninja_client(
            limits=fast_limits, sleep=sleeper, sites=[dental, legal], targets=[dental]
        )
    )
    mock_tenant()
    tenant = respx.get(DEVICES_URL)
    site_listing = respx.get(
        f"{NINJA_BASE_URL}/v2/organization/{NINJA_ORGANIZATION_ID}/devices"
    ).mock(return_value=httpx.Response(200, json=load_fixture("ninjaone", "devices.json")))
    route = mock_query("logged-on-users", "query_logged_on_users.json")

    result = await list_logged_on_users(CLIENT_ID, site_id=str(dental.site_id))

    assert route.calls[0].request.url.params["df"] == f"org={NINJA_ORGANIZATION_ID}"
    assert result.sites_covered == ["acme-dental"]
    assert result.total == 3
    # Rows are placed through the site's own device listing; the tenant is never listed.
    assert site_listing.call_count == 1
    assert tenant.call_count == 0


@respx.mock
async def test_a_site_scoped_row_for_a_device_outside_the_site_is_dropped(
    stub_factory: Any,
    fast_limits: ConnectorLimits,
    sleeper: RecordingSleep,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deviceId the site's listing does not hold is out of scope: dropped, with a warning."""
    dental = site_ref(NINJA_ORGANIZATION_ID, slug="acme-dental", name="Acme Dental")
    legal = site_ref(77, slug="bright-legal", name="Bright Legal")
    stub_factory(
        make_ninja_client(
            limits=fast_limits, sleep=sleeper, sites=[dental, legal], targets=[dental]
        )
    )
    mock_tenant()
    devices = load_fixture("ninjaone", "devices.json")
    respx.get(f"{NINJA_BASE_URL}/v2/organization/{NINJA_ORGANIZATION_ID}/devices").mock(
        return_value=httpx.Response(200, json=devices)
    )
    report = load_fixture("ninjaone", "query_logged_on_users.json")
    stray = {**report["results"][0], "deviceId": 987654}
    respx.get(query_url("logged-on-users")).mock(
        return_value=httpx.Response(200, json={**report, "results": [*report["results"], stray]})
    )

    with caplog.at_level(logging.WARNING, logger="app.connectors.ninjaone.client"):
        result = await list_logged_on_users(CLIENT_ID, site_id=str(dental.site_id))

    assert result.total == 3  # the stray row is not shown
    dropped = [r for r in caplog.records if "out of scope for this call" in r.getMessage()]
    assert len(dropped) == 1
    assert dropped[0].levelno == logging.WARNING


# --- registration ---------------------------------------------------------


def test_the_report_tools_are_registered() -> None:
    """The total is asserted once, in test_ninjaone_reports2.py."""
    names = {definition.name for definition in tools_for(ConnectorName.NINJAONE)}
    assert names >= {
        "list_devices",
        "count_devices",
        "list_devices_not_checked_in",
        "list_open_alerts",
        "get_device",
        "list_patch_status",
        "list_pending_reboot",
        "list_disk_space",
        "list_antivirus_status",
        "list_software",
        "list_logged_on_users",
        "list_device_activities",
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("get_device", {"device_name", "site"}),
        ("list_patch_status", {"status", "severity", "category", "kb", "site"}),
        ("list_pending_reboot", {"site"}),
        ("list_disk_space", {"below_percent", "site"}),
        ("list_antivirus_status", {"unhealthy_only", "site"}),
        ("list_software", {"name", "site"}),
        ("list_logged_on_users", {"user", "site"}),
        ("list_device_activities", {"device_name", "days", "site"}),
    ],
)
def test_each_report_tool_exposes_the_arguments_it_should(name: str, expected: set[str]) -> None:
    schema = get_tool(name).model_facing_schema()
    assert set(schema["properties"]) == expected
    assert "client_id" not in schema["properties"]
    assert len(get_tool(name).description) > 120

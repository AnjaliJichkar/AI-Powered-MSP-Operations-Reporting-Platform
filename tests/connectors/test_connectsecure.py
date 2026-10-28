"""ConnectSecure: the read-only allowlist, auth, filters, paging, scoping, the five tools.

Fixtures are **scrubbed live responses** from the pilot tenant (2026-09-22):
every id, hostname, company name, address, serial and MAC replaced, every key,
type and enum kept. A few rows were edited past scrubbing so one fixture holds
states the sample did not (a deprecated agent, an agent that stopped
reporting, an uninstall and a script remediation, a finding with no
remediation row); `docs/api-notes/connectsecure.md` names them.
"""

from __future__ import annotations

import base64
import datetime as dt
import re
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.agent.registry import get_tool, registered_tools
from app.connectors.base import ConnectorLimits
from app.connectors.connectsecure.client import (
    AUTHORIZE_PATH,
    FILTER_FIELDS,
    READ_ONLY_ITEM_PATHS,
    READ_ONLY_PATHS,
    VULNERABILITIES_PATH,
    ConnectSecureClient,
    ReadOnlyViolation,
    build_condition,
)
from app.connectors.connectsecure.ledger import DAILY_LIMIT, RequestLedger
from app.connectors.connectsecure.models import (
    READ_ONLY_ROLES,
    CsRemediation,
    CsVulnerability,
    normalise_product,
    normalise_severity,
    risk_grade,
    version_at_least,
)
from app.connectors.connectsecure.tools import (
    count_vulnerabilities,
    get_asset_vulnerabilities,
    list_critical_vulnerabilities,
    list_scan_coverage,
    list_top_remediations,
)
from app.connectors.errors import AuthError, RateLimited, UpstreamError
from app.connectors.scope import VendorOrganisation
from tests.connectors.conftest import (
    CONNECTSECURE_BASE_URL,
    CONNECTSECURE_COMPANY_ID,
    CONNECTSECURE_INACTIVE_SITE,
    CONNECTSECURE_OTHER_SITE,
    CONNECTSECURE_SITE,
    RecordingSleep,
    load_fixture,
    make_connectsecure_client,
)

CLIENT_ID = str(uuid.uuid4())
BASE = CONNECTSECURE_BASE_URL

BY_PATH = {
    "/r/company/companies": "companies.json",
    "/r/company/agents": "agents.json",
    "/r/asset/asset_view": "assets.json",
    "/r/report_queries/asset_wise_vulnerabilities": "asset_vulnerabilities.json",
    "/r/report_queries/get_remediation": "remediations.json",
    "/r/company/company_stats": "company_stats.json",
    "/r/user/get_users": "users.json",
}


def answer(request: httpx.Request) -> httpx.Response:
    """The tenant as the fixtures describe it, filtered as the vendor filters.

    `company_id=<id>` and `problem_group_id=34` are honoured here, so a test
    sees what a site-scoped read really returns; `total` is the filtered
    total, as the vendor sends it.
    """
    path = request.url.path
    if path not in BY_PATH:
        raise AssertionError(f"unexpected path {path}")
    condition = request.url.params.get("condition") or ""
    if "problem_group_id=34" in condition:
        payload = load_fixture("connectsecure", "asset_vulnerabilities_kev.json")
    else:
        payload = load_fixture("connectsecure", BY_PATH[path])
    rows = payload["data"]
    wanted_match = re.search(r"\b(?:company_)?id=(\d+)", condition)
    if wanted_match:
        wanted = wanted_match.group(1)

        def company_of(row: dict[str, Any]) -> str:
            value = row.get("company_id")
            if value is None and isinstance(row.get("company_ids"), list):
                value = row["company_ids"][0] if row["company_ids"] else None
            if value is None:
                value = row.get("id")
            return str(value)

        rows = [r for r in rows if company_of(r) == wanted]
    skip = int(request.url.params.get("skip", 0))
    limit = int(request.url.params.get("limit", 100))
    return httpx.Response(
        200, json={"status": True, "total": len(rows), "data": rows[skip : skip + limit]}
    )


def mock_world() -> respx.Route:
    respx.post(f"{BASE}{AUTHORIZE_PATH}").mock(
        return_value=httpx.Response(200, json=load_fixture("connectsecure", "authorize.json"))
    )
    return respx.get(url__startswith=BASE).mock(side_effect=answer)


@pytest.fixture
def connectsecure(stub_factory: Any, fast_limits: ConnectorLimits, sleeper: RecordingSleep) -> Any:
    return stub_factory(make_connectsecure_client(limits=fast_limits, sleep=sleeper))


# --- read-only, enforced in code --------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/w/company/companies"),  # create company
        ("POST", "/w/company/scan"),  # trigger a scan
        ("POST", "/w/company/external_scan"),
        ("PATCH", "/w/company/companies"),
        ("DELETE", "/d/company/companies/501"),
        ("DELETE", "/d/asset/assets/1001"),
        ("POST", "/w/company/reset_agents"),
        ("POST", "/report_builder/create_report_job"),
        ("POST", "/r/company/companies"),  # a read path with the wrong verb
        ("GET", "/w/company/companies"),  # a write path with the read verb
        ("GET", "/r/company/credentials"),  # a read the connector has no business making
        ("GET", "/r/company/get_uninstall_secret"),
        ("GET", "/r/asset/asset_view/1/extra"),
        ("GET", "/r/report_queries/asset_wise_vulnerabilitiez"),
    ],
)
async def test_a_write_or_unlisted_request_is_refused_before_any_http(
    fast_limits: ConnectorLimits, method: str, path: str
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.route(url__startswith=BASE).mock(
            return_value=httpx.Response(200, json={"status": True, "data": []})
        )
        async with make_connectsecure_client(limits=fast_limits) as client:
            with pytest.raises(ReadOnlyViolation, match="not a read-only request"):
                await client.request(method, path, json_body={"name": "x"})
        assert route.call_count == 0  # no HTTP at all, not even the login
    assert not ConnectSecureClient.is_read_only(method, path)


def test_the_allowlist_holds_reads_only() -> None:
    assert all(path.startswith("/r/") for path in READ_ONLY_PATHS)
    assert READ_ONLY_ITEM_PATHS <= READ_ONLY_PATHS
    assert ConnectSecureClient.is_read_only("GET", "/r/company/agents/12")
    assert ConnectSecureClient.is_read_only("get", "/r/company/companies")
    assert not ConnectSecureClient.is_read_only("GET", "/r/report_queries/risk_score")
    assert not ConnectSecureClient.is_read_only("POST", AUTHORIZE_PATH)  # login is not a read


# --- the filter builder ------------------------------------------------------------------------


def test_only_fields_seen_honoured_are_sent_and_strings_are_always_quoted() -> None:
    assert build_condition("/r/asset/asset_view", company_id=501) == "company_id=501"
    assert (
        build_condition(VULNERABILITIES_PATH, company_id=501, severity="Critical")
        == "company_id=501 AND severity='Critical'"
    )
    assert build_condition(VULNERABILITIES_PATH, company_id=501, severity=None) == "company_id=501"
    # An unquoted string is ignored by the report views, so a value never goes bare.
    assert "'" in build_condition(VULNERABILITIES_PATH, severity="High")
    assert build_condition(VULNERABILITIES_PATH, severity="it's") == "severity='it''s'"
    with pytest.raises(ValueError, match="not a filter field"):
        build_condition("/r/asset/asset_view", nosuchfield=1)
    with pytest.raises(ValueError, match="not a filter field"):
        build_condition("/r/company/agents", severity="High")  # honoured elsewhere, not here
    assert all("company_id" in fields or "id" in fields for fields in FILTER_FIELDS.values())


@respx.mock
async def test_a_filter_the_vendor_ignored_is_an_error_not_a_result(
    fast_limits: ConnectorLimits,
) -> None:
    """A row that does not satisfy the filter we sent is proof it was ignored."""
    respx.post(f"{BASE}{AUTHORIZE_PATH}").mock(
        return_value=httpx.Response(200, json=load_fixture("connectsecure", "authorize.json"))
    )
    everything = load_fixture("connectsecure", "asset_vulnerabilities.json")["data"]
    for row in everything:  # as if they belonged to another customer
        row["company_ids"] = [999]
    respx.get(f"{BASE}{VULNERABILITIES_PATH}").mock(
        return_value=httpx.Response(
            200, json={"status": True, "total": len(everything), "data": everything}
        )
    )
    async with make_connectsecure_client(limits=fast_limits) as client:
        with pytest.raises(UpstreamError, match="ignored the filter"):
            await client.list_vulnerabilities(sites=[CONNECTSECURE_SITE], with_fixes=False)


@respx.mock
async def test_a_severity_filter_is_applied_again_on_our_side(
    fast_limits: ConnectorLimits,
) -> None:
    """The report views ignore an unquoted or unknown value and return everything."""
    respx.post(f"{BASE}{AUTHORIZE_PATH}").mock(
        return_value=httpx.Response(200, json=load_fixture("connectsecure", "authorize.json"))
    )
    everything = load_fixture("connectsecure", "asset_vulnerabilities.json")["data"]
    route = respx.get(f"{BASE}{VULNERABILITIES_PATH}").mock(
        return_value=httpx.Response(
            200, json={"status": True, "total": len(everything), "data": everything}
        )
    )
    async with make_connectsecure_client(limits=fast_limits) as client:
        with pytest.raises(UpstreamError, match="do not satisfy"):
            await client._filtered(VULNERABILITIES_PATH, company_id=501, severity="Critical")
    assert "severity='Critical'" in route.calls[0].request.url.params["condition"]


# --- auth ------------------------------------------------------------------------------------


@respx.mock
async def test_authorize_sends_the_encoded_tenant_credentials_then_bearer_and_user_id(
    fast_limits: ConnectorLimits,
) -> None:
    login = respx.post(f"{BASE}{AUTHORIZE_PATH}").mock(
        return_value=httpx.Response(200, json=load_fixture("connectsecure", "authorize.json"))
    )
    listing = respx.get(f"{BASE}/r/company/companies").mock(side_effect=answer)
    async with make_connectsecure_client(limits=fast_limits) as client:
        organisations = await client.list_organisations()
        await client.list_organisations()  # the token is reused

    assert login.call_count == 1
    token = login.calls[0].request.headers["Client-Auth-Token"]
    assert base64.b64decode(token).decode() == "example-msp+test-client-id:test-client-secret"
    headers = listing.calls[0].request.headers
    assert headers["Authorization"] == "Bearer test-access-token"
    assert headers["X-USER-ID"] == "u-1001"
    assert str(listing.calls[0].request.url).startswith("https://pod401.myconnectsecure.com/")
    assert [o.external_id for o in organisations] == ["501", "502", "503"]


@respx.mock
async def test_a_401_re_authenticates_once_then_is_an_auth_error(
    fast_limits: ConnectorLimits,
) -> None:
    login = respx.post(f"{BASE}{AUTHORIZE_PATH}").mock(
        return_value=httpx.Response(
            200, json={"data": load_fixture("connectsecure", "authorize.json")}
        )
    )
    respx.get(f"{BASE}/r/company/companies").mock(
        return_value=httpx.Response(401, json=load_fixture("connectsecure", "error_401.json"))
    )
    async with make_connectsecure_client(limits=fast_limits) as client:
        with pytest.raises(AuthError):
            await client.list_organisations()
    assert login.call_count == 2  # the nested `data` shape is read too


async def test_a_missing_pod_or_tenant_is_a_clear_error(fast_limits: ConnectorLimits) -> None:
    with pytest.raises(AuthError, match="pod number"):
        _ = make_connectsecure_client(limits=fast_limits, config={"tenant": "x"}).base_url
    client = make_connectsecure_client(limits=fast_limits, config={"pod": "401"})
    with pytest.raises(AuthError, match="tenant name"):
        client._client_auth_token()


@respx.mock
async def test_a_429_honours_retry_after_then_gives_up(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    respx.post(f"{BASE}{AUTHORIZE_PATH}").mock(
        return_value=httpx.Response(200, json=load_fixture("connectsecure", "authorize.json"))
    )
    respx.get(f"{BASE}/r/company/companies").mock(
        return_value=httpx.Response(
            429, json=load_fixture("connectsecure", "error_429.json"), headers={"Retry-After": "2"}
        )
    )
    async with make_connectsecure_client(limits=fast_limits, sleep=sleeper) as client:
        with pytest.raises(RateLimited):
            await client.list_organisations()
    assert sleeper.delays == [0.05, 0.05]  # Retry-After capped at backoff_max_seconds


# --- the daily request ledger -------------------------------------------------------------------


@respx.mock
async def test_every_request_including_the_login_counts_against_the_daily_limit(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    mock_world()
    ledger = RequestLedger(limit=4)
    async with make_connectsecure_client(limits=fast_limits, ledger=ledger) as client:
        assert client.requests_today == 0
        with caplog.at_level("WARNING", logger="app.connectors.connectsecure.ledger"):
            await client.list_organisations()
        assert client.requests_today == len(client.calls_made)
        assert client.requests_today >= 2  # the login and the listing
        assert ("POST", AUTHORIZE_PATH) in client.calls_made
    # Crossing half of the limit is logged once, naming the client and the share.
    assert any("requests today" in record.getMessage() for record in caplog.records)
    assert DAILY_LIMIT == 30_000  # the vendor's documented limit


def test_the_ledger_counts_per_tenant_per_day_and_warns_once_per_threshold() -> None:
    ledger = RequestLedger(limit=10)
    key = (uuid.uuid4(), "pod401")
    other = (uuid.uuid4(), "pod401")
    day = dt.datetime(2026, 9, 22, 12, tzinfo=dt.UTC)
    for _ in range(8):
        ledger.record(key, client_slug="acme", now=day)
    ledger.record(other, client_slug="beacon", now=day)
    assert ledger.today(key, now=day) == 8
    assert ledger.today(other, now=day) == 1  # tenants never share a count
    assert ledger.remaining(key, now=day) == 2
    # A new day starts over.
    assert ledger.today(key, now=day + dt.timedelta(days=1)) == 0
    ledger.record(key, client_slug="acme", now=day + dt.timedelta(days=1))
    assert ledger.today(key, now=day + dt.timedelta(days=1)) == 1


# --- sites -------------------------------------------------------------------------------------


@respx.mock
async def test_list_organisations_leaves_out_assessment_and_deleted_companies(
    fast_limits: ConnectorLimits,
) -> None:
    mock_world()
    async with make_connectsecure_client(limits=fast_limits) as client:
        organisations = await client.list_organisations()
    assert organisations == [
        VendorOrganisation(external_id="501", name="Acme Dental"),
        VendorOrganisation(external_id="502", name="Other Customer"),
        VendorOrganisation(external_id="503", name="Former Customer"),
    ]


# --- scoping and paging -------------------------------------------------------------------------


@respx.mock
async def test_a_named_site_sends_its_company_in_the_filter_and_is_checked_again(
    fast_limits: ConnectorLimits,
) -> None:
    route = mock_world()
    async with make_connectsecure_client(
        limits=fast_limits,
        sites=[CONNECTSECURE_SITE, CONNECTSECURE_OTHER_SITE],
        targets=[CONNECTSECURE_SITE],
    ) as client:
        assets, truncated = await client.list_assets()
    asset_requests = [c.request for c in route.calls if c.request.url.path == "/r/asset/asset_view"]
    assert asset_requests
    assert f"company_id={CONNECTSECURE_COMPANY_ID}" in asset_requests[0].url.params["condition"]
    assert {site.slug for site, _ in assets} == {"acme-dental"}
    assert sorted(a.host_name for _, a in assets) == [
        "TL-AUS-DC01", "TL-AUS-MAC03", "TL-AUS-WS07", "TL-AUS-WS14", "TL-AUS-WS22"
    ]  # fmt: skip
    assert not truncated


@respx.mock
async def test_the_asset_row_gets_its_scan_time_from_the_agent_it_names(
    fast_limits: ConnectorLimits,
) -> None:
    """A host can have several agent rows, so the join is by `agent_id`, never hostname."""
    mock_world()
    async with make_connectsecure_client(limits=fast_limits) as client:
        assets, _ = await client.list_assets()
    by_host = {a.host_name: a for _, a in assets}
    assert all(a.agent is not None for a in by_host.values())
    assert all(a.agent_id == a.agent.id for a in by_host.values() if a.agent)
    assert all(a.last_scan == a.agent.last_scanned_time for a in by_host.values() if a.agent)
    deprecated = by_host["TL-AUS-WS07"].agent
    assert deprecated is not None and deprecated.is_deprecated is True


@respx.mock
async def test_a_listing_is_read_as_one_page_sized_to_the_total(
    fast_limits: ConnectorLimits,
) -> None:
    """The report views have no stable order, so an offset walk is never used."""
    route = mock_world()
    async with make_connectsecure_client(limits=fast_limits, page_size=2) as client:
        organisations = await client.list_organisations()
    assert [o.external_id for o in organisations] == ["501", "502", "503"]
    requests = [c.request for c in route.calls if c.request.url.path == "/r/company/companies"]
    assert [int(r.url.params["limit"]) for r in requests] == [2, 5]  # then exactly the total
    assert all(int(r.url.params["skip"]) == 0 for r in requests)


@respx.mock
async def test_a_listing_above_the_row_cap_is_reported_truncated(
    fast_limits: ConnectorLimits,
) -> None:
    mock_world()
    async with make_connectsecure_client(limits=fast_limits, page_size=2, max_rows=3) as client:
        rows, truncated = await client.list_agents()
    assert truncated and len(rows) == 2


@respx.mock
async def test_the_tenant_listing_is_classified_row_by_row(fast_limits: ConnectorLimits) -> None:
    mock_world()
    async with make_connectsecure_client(
        limits=fast_limits,
        sites=[CONNECTSECURE_SITE, CONNECTSECURE_OTHER_SITE],
        excluded=[CONNECTSECURE_INACTIVE_SITE],
    ) as client:
        stats = await client.company_stats()
    assert {site.slug for site, _ in stats} == {"acme-dental", "beacon-legal"}
    assert all(s.company_id in ("501", "502") for _, s in stats)  # 503 is inactive, dropped


# --- tools -------------------------------------------------------------------------------------


def test_five_tools_registered_each_saying_what_it_is_not_for() -> None:
    names = {t.name for t in registered_tools() if t.connector.value == "connectsecure"}
    assert names == {
        "count_vulnerabilities",
        "list_critical_vulnerabilities",
        "list_top_remediations",
        "get_asset_vulnerabilities",
        "list_scan_coverage",
    }
    for name in names:
        description = get_tool(name).description
        assert "Not for" in description, name
        assert "client_id" not in description


@respx.mock
async def test_count_vulnerabilities_by_severity_product_and_asset(connectsecure: Any) -> None:
    mock_world()
    result = await count_vulnerabilities(CLIENT_ID, group_by="severity")
    assert result.total == 7 and result.unique_vulnerabilities == 6  # one CVE on two assets
    assert result.counts == {"critical": 3, "high": 2, "medium": 1, "low": 1}
    assert result.known_exploited == sum(
        a["cisa_vul"] for a in load_fixture("connectsecure", "assets.json")["data"]
    )
    assert result.scan_dates.latest is not None
    assert result.source.startswith("the per-asset findings")

    by_product = await count_vulnerabilities(CLIENT_ID, group_by="product")
    assert by_product.counts["Mozilla Firefox (x64 en-US)"] == 2
    by_asset = await count_vulnerabilities(CLIENT_ID, group_by="asset")
    assert sum(by_asset.counts.values()) == 7


@respx.mock
async def test_count_vulnerabilities_across_sites_uses_the_vendor_totals(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    route = mock_world()
    stub_factory(
        make_connectsecure_client(
            limits=fast_limits, sites=[CONNECTSECURE_SITE, CONNECTSECURE_OTHER_SITE]
        )
    )
    result = await count_vulnerabilities(CLIENT_ID, group_by="site")
    assert set(result.counts) == {"Acme Dental", "Beacon Legal LLP"}
    assert result.source.startswith("the vendor's per-company totals")
    assert not any(c.request.url.path.endswith("asset_wise_vulnerabilities") for c in route.calls)


@respx.mock
async def test_critical_vulnerabilities_known_exploited_first_with_the_paired_fix(
    connectsecure: Any,
) -> None:
    mock_world()
    result = await list_critical_vulnerabilities(CLIENT_ID)
    # Three criticals; two are the same CVE on two assets, and that CVE is the KEV one.
    assert result.findings == 3 and result.known_exploited_findings == 2
    first = result.vulnerabilities[0]
    assert first.known_exploited is True and first.devices_affected == 2
    assert first.devices == ["TL-AUS-DC01", "TL-AUS-WS14"]
    assert "Mozilla Firefox" in first.fix and "156.0" in first.fix
    assert result.fix_reported_for >= 2
    # The third critical is on Chrome and has its own fix row.
    assert any("Google Chrome" in row.fix for row in result.vulnerabilities)

    kev_only = await list_critical_vulnerabilities(CLIENT_ID, known_exploited_only=True)
    assert len(kev_only.vulnerabilities) == 1 and kev_only.findings == 2


@respx.mock
async def test_a_finding_with_no_remediation_row_says_fix_not_reported(
    connectsecure: Any,
) -> None:
    mock_world()
    result = await get_asset_vulnerabilities(CLIENT_ID, "TL-AUS-DC01", "acme-dental")
    fixes = {v.vulnerability: v.fix for v in result.vulnerabilities}
    assert "fix not reported" in fixes.values()  # OpenSSL has no remediation row
    assert any("Mozilla Firefox" in fix for fix in fixes.values())
    assert result.fix_reported_for < len(result.vulnerabilities)


@respx.mock
async def test_top_remediations_rank_by_what_they_resolve_and_name_the_kind(
    connectsecure: Any,
) -> None:
    mock_world()
    result = await list_top_remediations(CLIENT_ID)
    kinds = {row.kind for row in result.fixes}
    assert {"patch", "uninstall", "script"} <= kinds
    uninstall = next(row for row in result.fixes if row.kind == "uninstall")
    assert uninstall.instruction.startswith("Uninstall ")
    assert uninstall.target_version is None
    patch = next(row for row in result.fixes if row.kind == "patch")
    assert patch.instruction.startswith("Update ") and patch.target_version
    assert result.counts_by_kind["patch"] >= 1
    assert result.remediations == len(result.fixes)


@respx.mock
async def test_get_asset_vulnerabilities_matches_the_hostname_loosely(connectsecure: Any) -> None:
    mock_world()
    result = await get_asset_vulnerabilities(CLIENT_ID, "tl-aus-ws14.acme.local", "acme-dental")
    assert result.name == "TL-AUS-WS14" and result.matched_on.endswith("TL-AUS-WS14")
    assert result.risk_grade in "ABCDEF"
    assert result.last_scanned is not None
    assert result.counts_by_severity
    with pytest.raises(ValueError, match="no ConnectSecure asset named"):
        await get_asset_vulnerabilities(CLIENT_ID, "NOPE-99", "acme-dental")


@respx.mock
async def test_scan_coverage_lists_the_deprecated_and_silent_agents_first(
    connectsecure: Any,
) -> None:
    mock_world()
    result = await list_scan_coverage(CLIENT_ID)
    assert result.assets == 5
    statuses = {row.name: row.status for row in result.rows}
    assert statuses["TL-AUS-WS07"] == "agent deprecated"
    assert statuses["TL-AUS-MAC03"] == "agent not reporting"
    assert next(iter(result.counts_by_status)) in ("no agent", "agent deprecated")
    assert result.stale_after_days == 30
    assert result.counts_by_site["Acme Dental"] >= 2


@respx.mock
async def test_an_empty_company_counts_to_zero(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    respx.post(f"{BASE}{AUTHORIZE_PATH}").mock(
        return_value=httpx.Response(200, json=load_fixture("connectsecure", "authorize.json"))
    )
    respx.get(url__startswith=BASE).mock(
        return_value=httpx.Response(200, json=load_fixture("connectsecure", "empty.json"))
    )
    stub_factory(make_connectsecure_client(limits=fast_limits))
    result = await count_vulnerabilities(CLIENT_ID)
    assert result.total == 0 and result.counts == {} and result.assets_in_scope == 0
    coverage = await list_scan_coverage(CLIENT_ID)
    assert coverage.assets == 0 and coverage.rows == []


# --- the pre-flight's role check ---------------------------------------------------------------


@respx.mock
async def test_the_current_user_and_its_role_are_read_for_the_pre_flight(
    fast_limits: ConnectorLimits,
) -> None:
    mock_world()
    async with make_connectsecure_client(limits=fast_limits) as client:
        user = await client.current_user()
    assert user is not None and user.id == "u-1001"
    assert user.role_set == {"ADMIN"}  # the live key's role: the pre-flight warns on it
    assert not user.role_set <= READ_ONLY_ROLES
    assert {"VULNERABILITYVIEWER", "ASSETVIEWER"} <= READ_ONLY_ROLES


# --- models ------------------------------------------------------------------------------------


def test_severity_product_and_version_parsing() -> None:
    assert normalise_severity("Critical") == "critical" == normalise_severity("CRITICAL")
    assert normalise_severity(9.3) == "critical" and normalise_severity(4.0) == "medium"
    assert normalise_severity("bogus") is None
    # The product key drops the architecture, locale and version, and nothing else.
    assert normalise_product("Mozilla Firefox (x64 en-US) 156.0") == "mozilla firefox"
    assert normalise_product("Mozilla Firefox") == "mozilla firefox"
    assert normalise_product("Google Chrome") != normalise_product("Google Chrome Beta")
    assert version_at_least("Google Chrome 129.0.6668.59", "129.0")
    assert version_at_least("Firefox 156.0", "156.0")
    assert not version_at_least("Firefox 155.0", "156.0")
    assert not version_at_least("Firefox", "156.0")  # no version, no match
    assert risk_grade(0) == "A" and risk_grade(48) == "C" and risk_grade(95) == "F"
    assert risk_grade(None) is None


def test_the_finding_row_carries_no_fix_and_no_kev_flag_of_its_own() -> None:
    """Live: the fix and the CISA flag are the client's join, never the row's."""
    row = load_fixture("connectsecure", "asset_vulnerabilities.json")["data"][0]
    assert "fix" not in row and "known_exploited" not in row and "problem_group_id" not in row
    parsed = CsVulnerability.model_validate(row)
    assert parsed.fix is None and parsed.known_exploited is None
    assert parsed.problem.startswith("CVE-") and parsed.asset_id and parsed.company_id
    assert parsed.product  # from software_name[0]


def test_a_remediation_reads_as_an_instruction_by_kind() -> None:
    rows = load_fixture("connectsecure", "remediations.json")["data"]
    by_action = {r["remediation_action"]: CsRemediation.model_validate(r) for r in rows}
    assert by_action["Software Patch"].kind == "patch"
    assert by_action["Software Patch"].instruction.startswith("Update ")
    assert by_action["Uninstall"].kind == "uninstall"
    assert by_action["Uninstall"].instruction.startswith("Uninstall ")
    assert by_action["Run remediation script"].kind == "script"
    assert "script" in by_action["Run remediation script"].instruction
    assert by_action["OS Update"].kind == "patch"
    # Live: no fix text on this tenant carries a KB number.
    assert all(not CsRemediation.model_validate(r).kbs for r in rows)


def test_a_remediation_script_may_be_a_string_or_the_vendors_newer_list_of_scripts() -> None:
    row = {"solution_id": "1", "fix": "156.0", "fix_script": "apt-get upgrade"}
    assert CsRemediation.model_validate(row).fix_script == "apt-get upgrade"
    listed = {
        "solution_id": "2",
        "fix": "1.2",
        "fix_script": [
            {"os_type": "windows", "script": "winget upgrade"},
            {"os_type": "linux", "script": "systemctl restart apache2"},
        ],
    }
    folded = CsRemediation.model_validate(listed).fix_script
    assert folded == "[windows] winget upgrade" + chr(10) + "[linux] systemctl restart apache2"
    assert (
        CsRemediation.model_validate({"solution_id": "3", "fix": "x", "fix_script": []}).fix_script
        is None
    )


@respx.mock
async def test_rows_from_companies_sync_leaves_unmapped_drop_at_info_not_error(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """The tenant lists 504 as an assessment and 505 as deleted; sync maps neither.
    Their rows drop at INFO, named by id. A live company nobody synced (502) and a
    company the tenant does not list at all (999) drop at ERROR, named by id."""
    assets = load_fixture("connectsecure", "assets.json")
    extra_rows = [
        {**assets["data"][0], "id": 9001, "company_id": 504, "host_name": "PROSPECT-1"},
        {**assets["data"][0], "id": 9002, "company_id": 504, "host_name": "PROSPECT-2"},
        {**assets["data"][0], "id": 9003, "company_id": 505, "host_name": "GONE-1"},
        {**assets["data"][0], "id": 9004, "company_id": 502, "host_name": "UNSYNCED-1"},
        {**assets["data"][0], "id": 9005, "company_id": 999, "host_name": "STRANGER-1"},
    ]
    respx.get(f"{BASE}/r/asset/asset_view").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": True,
                "total": len(assets["data"]) + len(extra_rows),
                "data": assets["data"] + extra_rows,
            },
        )
    )
    mock_world()  # after the asset route: respx answers with the first route that matches
    async with make_connectsecure_client(limits=fast_limits) as client:
        with caplog.at_level("INFO", logger="app.connectors.connectsecure.client"):
            rows, _ = await client.list_assets()
    assert len(rows) == len(assets["data"])  # only the mapped company's rows are kept
    info = [
        r
        for r in caplog.records
        if r.levelname == "INFO" and "unmapped on purpose" in r.getMessage()
    ]
    assert len(info) == 1
    assert "assessment 2 row(s) from company 504 (2)" in info[0].getMessage()
    assert "deleted 1 row(s) from company 505 (1)" in info[0].getMessage()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "2 record(s)" in message
    assert "company 502 (unsynced, 1)" in message
    assert "company 999 (not in the tenant's company list, 1)" in message
    # No hostname reaches the log.
    assert "PROSPECT" not in caplog.text and "STRANGER" not in caplog.text

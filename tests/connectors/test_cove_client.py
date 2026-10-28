"""The Cove connector against recorded JSON-RPC responses.

Nothing here reaches a network: `respx` answers every call. The fixtures are
built from N-able's documentation rather than from a live account
(docs/api-notes/cove.md), so what these prove is our side of the contract —
the visa chain, the paging, the guard, the scoping and the silence about
secrets — and not that the vendor answers this way. Every name in them is
made up.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import pytest
import respx

from app.connectors.base import ConnectorLimits
from app.connectors.cove.client import (
    JSONAPI_PATH,
    PAGE_SIZE,
    READ_ONLY_METHODS,
    CoveClient,
    ReadOnlyViolation,
)
from app.connectors.cove.models import CoveDevice
from app.connectors.errors import AuthError, UpstreamError
from app.connectors.scope import ClientScope, SiteRef
from app.models.enums import ConnectorName
from tests.connectors.conftest import RecordingSleep, load_fixture

BASE = "https://cove.test.invalid"
URL = f"{BASE}{JSONAPI_PATH}"
CLIENT_ID = uuid.uuid4()

PARTNER = "300100"
OTHER_PARTNER = "300200"
PASSWORD = "a-password-nobody-should-see"


def site(external_id: str = PARTNER, slug: str = "fabrikam") -> SiteRef:
    return SiteRef(site_id=uuid.uuid4(), slug=slug, name=slug.title(), external_id=external_id)


def make_client(
    *,
    limits: ConnectorLimits,
    sites: list[SiteRef] | None = None,
    excluded: tuple[SiteRef, ...] = (),
    sleep: RecordingSleep | None = None,
) -> CoveClient:
    allowed = tuple(sites if sites is not None else [site()])
    scope = ClientScope(
        client_id=CLIENT_ID,
        client_slug="tailwind-it",
        connector=ConnectorName.COVE,
        allowed=allowed,
        targets=allowed,
        excluded=excluded,
    )
    return CoveClient(
        scope=scope,
        secrets={"password": PASSWORD},
        config={"base_url": BASE, "partner": "Tailwind IT", "username": "api@example.invalid"},
        limits=limits,
        sleep=sleep,
    )


def login_answer(visa: str = "visa-first", partner_id: int = 300000) -> dict[str, Any]:
    return {
        "id": "jsonrpc",
        "jsonrpc": "2.0",
        "visa": visa,
        "result": {
            "result": {
                "Id": 77,
                "PartnerId": partner_id,
                "RoleId": 3,
                "EmailAddress": "api@example.invalid",
                "Flags": [],
            }
        },
    }


def rpc_error(message: str, code: int = -32000) -> dict[str, Any]:
    return {"id": "jsonrpc", "jsonrpc": "2.0", "error": {"code": code, "message": message}}


class Conversation:
    """Answers the one endpoint in order, and keeps what was sent."""

    def __init__(self, *answers: dict[str, Any] | httpx.Response) -> None:
        self.answers = list(answers)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        self.sent.append(jsonlib.loads(request.content))
        answer = self.answers.pop(0) if self.answers else {"result": {"result": []}}
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    @property
    def methods(self) -> list[str]:
        return [body.get("method", "") for body in self.sent]


# --- read only ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "AddAccount",
        "ModifyAccount",
        "RemoveAccount",
        "AddPartner",
        "ModifyPartner",
        "RemovePartner",
        "AddUser",
        "ModifyUser",
        "RemoveUser",
        "RegenerateUid",
    ],
)
@respx.mock
async def test_no_write_method_reaches_cove(method: str, fast_limits: ConnectorLimits) -> None:
    """Cove has no read-only scope, so this is ours to enforce. The refusal
    happens before a request exists, which is what makes it a guarantee
    rather than a hope."""
    sent = respx.post(URL)
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(ReadOnlyViolation, match="not a read-only method"):
            await client.call(method, {})
    assert not sent.called


@respx.mock
async def test_a_method_nobody_has_heard_of_is_refused_too(fast_limits: ConnectorLimits) -> None:
    sent = respx.post(URL)
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(ReadOnlyViolation):
            await client.call("DoSomethingNew", {})
    assert not sent.called


def test_the_allowlist_holds_only_methods_this_connector_calls() -> None:
    """An allowlist nobody has exercised is a list of guesses."""
    assert READ_ONLY_METHODS == {"Login", "EnumeratePartners", "EnumerateAccountStatistics"}


# --- the visa chain -----------------------------------------------------------------


@respx.mock
async def test_login_sends_the_three_fields_and_keeps_the_visa(
    fast_limits: ConnectorLimits,
) -> None:
    talk = Conversation(login_answer(), load_fixture("cove", "enumerate_partners.json"))
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        await client.list_partners()

    assert talk.methods == ["Login", "EnumeratePartners"]
    assert talk.sent[0]["params"] == {
        "partner": "Tailwind IT",
        "username": "api@example.invalid",
        "password": PASSWORD,
    }
    # Login carries no visa; everything after it does.
    assert "visa" not in talk.sent[0]
    assert talk.sent[1]["visa"] == "visa-first"


@respx.mock
async def test_the_freshest_visa_is_the_one_that_is_sent(fast_limits: ConnectorLimits) -> None:
    """Every answer carries a new visa, so a session is a chain."""
    talk = Conversation(
        login_answer(),
        load_fixture("cove", "enumerate_partners.json"),  # hands back visa-second
        {"id": "jsonrpc", "jsonrpc": "2.0", "visa": "visa-third", "result": {"result": []}},
    )
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        await client.list_partners()
        await client.call("EnumeratePartners", {})

    assert [body.get("visa") for body in talk.sent] == [None, "visa-first", "visa-second"]


@respx.mock
async def test_a_refused_visa_is_logged_in_again_once(fast_limits: ConnectorLimits) -> None:
    talk = Conversation(
        login_answer(),
        rpc_error("Visa has expired"),
        login_answer("visa-fresh"),
        load_fixture("cove", "enumerate_partners.json"),
    )
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        found = await client.list_partners()

    assert talk.methods == ["Login", "EnumeratePartners", "Login", "EnumeratePartners"]
    assert len(found) == 5


@respx.mock
async def test_a_login_that_does_not_stick_is_a_credential_problem(
    fast_limits: ConnectorLimits,
) -> None:
    """Twice refused is not a session that lapsed; it is one that never
    worked, and saying so stops a retry loop nobody can see."""
    talk = Conversation(
        login_answer(),
        rpc_error("Session is not valid"),
        login_answer("visa-fresh"),
        rpc_error("Session is not valid"),
    )
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        with pytest.raises(AuthError):
            await client.list_partners()

    assert talk.methods.count("Login") == 2


# --- the three ways it can fail -----------------------------------------------------


@respx.mock
async def test_bad_credentials_say_so(fast_limits: ConnectorLimits) -> None:
    respx.post(URL).mock(side_effect=Conversation(rpc_error("Login failed: wrong password")))
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(AuthError, match="rejected the credentials"):
            await client.list_partners()


@respx.mock
async def test_a_role_that_may_not_read_is_not_a_wrong_password(
    fast_limits: ConnectorLimits,
) -> None:
    """The two look alike over HTTP and send somebody to two different
    places: one to the console's roles, the other to a password reset."""
    talk = Conversation(login_answer(), rpc_error("Access denied for this user"))
    respx.post(URL).mock(side_effect=talk)
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(AuthError, match="may not do that"):
            await client.list_partners()


@respx.mock
async def test_cove_being_unavailable_is_upstream_not_auth(
    fast_limits: ConnectorLimits, sleeper: RecordingSleep
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(503, text="maintenance"))
    async with make_client(limits=fast_limits, sleep=sleeper) as client:
        with pytest.raises(UpstreamError):
            await client.list_partners()
    assert sleeper.delays, "a 5xx is retried before it is given up on"


@respx.mock
async def test_an_envelope_that_is_not_one_is_an_upstream_error(
    fast_limits: ConnectorLimits,
) -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json=["not", "an", "envelope"]))
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(UpstreamError, match="JSON-RPC envelope"):
            await client.list_partners()


# --- customers ----------------------------------------------------------------------


@respx.mock
async def test_only_end_customers_become_sites(fast_limits: ConnectorLimits) -> None:
    """A distributor is not somebody's estate, and a partner whose level the
    vendor did not send is left out rather than guessed at."""
    talk = Conversation(login_answer(), load_fixture("cove", "enumerate_partners.json"))
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        organisations = await client.list_organisations()

    assert [o.external_id for o in organisations] == ["300100", "300200", "300400"]
    assert talk.sent[1]["params"]["parentPartnerId"] == 300000
    assert talk.sent[1]["params"]["fetchRecursively"] is True


# --- devices ------------------------------------------------------------------------


@respx.mock
async def test_a_device_row_is_read_into_something_with_names_on_it(
    fast_limits: ConnectorLimits,
) -> None:
    talk = Conversation(login_answer(), load_fixture("cove", "account_statistics.json"))
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        devices = await client.list_devices(site())

    by_id = {d.account_id: d for d in devices}
    workstation = by_id[991001]
    assert workstation.key == "fab-ws-01"
    assert workstation.os_type == "workstation"
    assert workstation.physicality == "Physical"
    assert workstation.used_storage_bytes == 241000000
    assert workstation.data_sources == ["D01", "D02"]
    assert workstation.retention_units == "All-In"
    assert workstation.last_session_status == "completed"
    assert workstation.last_successful_at is not None
    assert workstation.is_machine

    server = by_id[991002]
    assert server.os_type == "server"
    assert server.last_session_status == "completed_with_errors"
    assert server.errors == 3
    assert server.recovery_testing == "recovery_testing"
    # It has never carried a colour bar; that is absent, not empty.
    assert server.colour_bar is None


@respx.mock
async def test_a_microsoft_365_tenant_is_a_row_and_not_a_machine(
    fast_limits: ConnectorLimits,
) -> None:
    """Cove counts the tenant in its device total, with no OS and no
    physicality. Every figure about machines has to exclude it, and the
    mailbox counts live on it."""
    talk = Conversation(login_answer(), load_fixture("cove", "account_statistics.json"))
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        devices = await client.list_devices(site())

    tenant = next(d for d in devices if d.is_microsoft365)
    assert tenant.os_type == "undefined"
    assert tenant.is_machine is False
    assert (tenant.user_mailboxes, tenant.shared_mailboxes, tenant.onedrive_users) == (17, 13, 20)

    assert len(devices) == 3
    assert sum(1 for d in devices if d.is_machine) == 2


@respx.mock
async def test_every_column_we_read_is_asked_for(fast_limits: ConnectorLimits) -> None:
    """A code that is not sent comes back missing, and a code that is sent
    wrong comes back missing too, so the request is worth asserting."""
    talk = Conversation(login_answer(), load_fixture("cove", "account_statistics.json"))
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        await client.list_devices(site())

    asked = talk.sent[1]["params"]["query"]["Columns"]
    for code in ("I1", "I32", "I81", "I14", "I59", "D09F00", "D09F09", "D19F20", "D20F20"):
        assert code in asked
    assert talk.sent[1]["params"]["query"]["PartnerId"] == 300100


@respx.mock
async def test_paging_runs_until_a_page_comes_back_short(fast_limits: ConnectorLimits) -> None:
    """The vendor does not say how many rows there are, so a short page is
    the only end of the list there is."""

    def row(account_id: int) -> dict[str, Any]:
        return {
            "AccountId": account_id,
            "PartnerId": 300100,
            "Settings": [{"I1": f"fab-ws-{account_id}_ab12c"}, {"I59": "1"}],
        }

    full = {"result": {"result": [row(n) for n in range(PAGE_SIZE)]}}
    last = {"result": {"result": [row(90001), row(90002)]}}
    talk = Conversation(login_answer(), full, last)
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        devices = await client.list_devices(site())

    assert len(devices) == PAGE_SIZE + 2
    starts = [body["params"]["query"]["StartRecordNumber"] for body in talk.sent[1:]]
    assert starts == [0, PAGE_SIZE]


# --- the fleet ----------------------------------------------------------------------


@respx.mock
async def test_the_fleet_is_one_recursive_read_at_our_own_partner(
    fast_limits: ConnectorLimits,
) -> None:
    """A device read at a partner covers the tree beneath it (live,
    2026-10-19: 53 rows at the franchisee's partner against 1 at a customer),
    so a fleet question is one request rather than one per customer."""
    talk = Conversation(login_answer(), load_fixture("cove", "account_statistics.json"))
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        devices = await client.list_fleet()

    assert talk.methods == ["Login", "EnumerateAccountStatistics"]
    # Our own partner, from the login, not a site's.
    assert talk.sent[1]["params"]["query"]["PartnerId"] == 300000
    assert len(devices) == 3


@respx.mock
async def test_the_fleet_read_signs_in_first(fast_limits: ConnectorLimits) -> None:
    """It needs our own partner id, which only the login carries. Asked
    before anything else it must sign in, not complain that it does not know
    who we are."""
    talk = Conversation(login_answer(), {"result": {"result": []}})
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        assert await client.list_fleet() == []  # no AuthError
    assert talk.methods[0] == "Login"


@respx.mock
async def test_the_fleet_read_guards_every_row_it_did_not_ask_for(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """A recursive read is a wider net, not a looser one: a customer we hold
    no site for is still dropped with an error, and the franchisee's own
    partner is dropped quietly because we signed in as it."""
    payload = load_fixture("cove", "account_statistics.json")
    payload["result"]["result"][0]["PartnerId"] = 300000  # our own partner
    payload["result"]["result"][1]["PartnerId"] = 777777  # nobody's site
    talk = Conversation(login_answer(), payload)
    respx.post(URL).mock(side_effect=talk)

    with caplog.at_level(logging.INFO):
        async with make_client(limits=fast_limits) as client:
            devices = await client.list_fleet()

    assert [d.account_id for d in devices] == [991003]
    assert "777777" in caplog.text and "outside the allowlist" in caplog.text
    assert "the franchisee's own partner" in caplog.text
    assert "outside the allowlist of client=tailwind-it: [777777]" in caplog.text


# --- an empty customer is not a failure ---------------------------------------------


@respx.mock
async def test_a_customer_with_no_devices_answers_null_and_that_is_not_an_error(
    fast_limits: ConnectorLimits,
) -> None:
    """Live, 2026-10-19: two customers with no devices came back as `null`
    rather than `[]`, and the first draft called that a vendor failure. A
    customer between offboarding one machine and installing the next has no
    devices, which is a fact about them and not a tool that did not answer —
    the one distinction the coverage rules most need kept."""
    talk = Conversation(login_answer(), {"result": {"result": None}})
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        devices = await client.list_devices(site())

    assert devices == []


@respx.mock
async def test_a_partner_with_no_customers_answers_null_too(
    fast_limits: ConnectorLimits,
) -> None:
    talk = Conversation(login_answer(), {"result": {"result": None}})
    respx.post(URL).mock(side_effect=talk)

    async with make_client(limits=fast_limits) as client:
        assert await client.list_partners() == []


@respx.mock
@pytest.mark.parametrize(
    "shape",
    [
        {"result": {"result": {"AccountId": 1}}},  # one object, not a list of them
        {"result": {"result": "fab-ws-01_ab12c"}},  # a string is a Sequence; it is not rows
        {"result": {"result": 7}},
    ],
)
async def test_any_other_shape_still_raises(
    shape: dict[str, Any], fast_limits: ConnectorLimits
) -> None:
    """`None` is the vendor's empty list and nothing else is. A string would
    iterate into characters if the test were `Sequence` rather than `list`."""
    respx.post(URL).mock(side_effect=Conversation(login_answer(), shape))
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(UpstreamError, match="where a list of rows was expected"):
            await client.list_devices(site())


# --- the guard on what comes back ---------------------------------------------------


@respx.mock
async def test_a_row_from_a_partner_we_hold_no_site_for_is_dropped(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    """A filter we did not write is not a guard: every row is checked against
    the client's whole allowlist (hard rule 1)."""
    payload = load_fixture("cove", "account_statistics.json")
    payload["result"]["result"][1]["PartnerId"] = 999999
    talk = Conversation(login_answer(), payload)
    respx.post(URL).mock(side_effect=talk)

    with caplog.at_level(logging.ERROR):
        async with make_client(limits=fast_limits) as client:
            devices = await client.list_devices(site())

    assert [d.account_id for d in devices] == [991001, 991003]
    assert "outside the allowlist" in caplog.text
    assert "999999" in caplog.text


@respx.mock
async def test_a_row_from_an_inactive_site_of_ours_goes_quietly(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    payload = load_fixture("cove", "account_statistics.json")
    payload["result"]["result"][1]["PartnerId"] = int(OTHER_PARTNER)
    talk = Conversation(login_answer(), payload)
    respx.post(URL).mock(side_effect=talk)

    gone = site(OTHER_PARTNER, "contoso")
    with caplog.at_level(logging.ERROR):
        async with make_client(limits=fast_limits, excluded=(gone,)) as client:
            devices = await client.list_devices(site())

    assert [d.account_id for d in devices] == [991001, 991003]
    assert "outside the allowlist" not in caplog.text


# --- what is said out loud ----------------------------------------------------------


@respx.mock
async def test_the_password_and_the_visa_never_reach_a_log(
    fast_limits: ConnectorLimits, caplog: pytest.LogCaptureFixture
) -> None:
    talk = Conversation(
        login_answer(),
        load_fixture("cove", "enumerate_partners.json"),
        load_fixture("cove", "account_statistics.json"),
    )
    respx.post(URL).mock(side_effect=talk)

    with caplog.at_level(logging.DEBUG):
        async with make_client(limits=fast_limits) as client:
            await client.list_partners()
            await client.list_devices(site())

    assert PASSWORD not in caplog.text
    assert "visa-first" not in caplog.text
    assert "visa-second" not in caplog.text
    # Nor a device or customer name: the log carries counts and ids.
    assert "fab-ws-01" not in caplog.text
    assert "Fabrikam Logistics" not in caplog.text


@respx.mock
async def test_a_failure_does_not_put_the_password_in_the_error(
    fast_limits: ConnectorLimits,
) -> None:
    respx.post(URL).mock(side_effect=Conversation(rpc_error("Login failed")))
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(AuthError) as raised:
            await client.list_partners()
    assert PASSWORD not in str(raised.value)
    assert PASSWORD not in repr(raised.value.details)


@respx.mock
async def test_the_client_repr_names_the_client_and_no_secret(
    fast_limits: ConnectorLimits,
) -> None:
    async with make_client(limits=fast_limits) as client:
        drawn = repr(client)
    assert "tailwind-it" in drawn
    assert PASSWORD not in drawn


# --- a row with nothing in it -------------------------------------------------------


def test_a_row_the_vendor_filled_in_badly_is_read_without_raising() -> None:
    """An unrecognised column comes back missing rather than as an error, so
    the model has to survive a row that is mostly absent."""
    device = CoveDevice.from_row({"AccountId": 1, "PartnerId": 2, "Settings": []})
    assert device.name == ""
    assert device.key == ""
    assert device.os_type == "undefined"
    assert device.used_storage_bytes is None
    assert device.last_successful_at is None
    assert device.last_session_status is None

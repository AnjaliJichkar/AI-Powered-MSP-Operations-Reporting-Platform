"""The four backup tools an engineer reaches through Ask.

Nothing here reaches a network: `respx` answers every call and the factory is
stubbed. What is checked is what the model is handed — the word for where a
machine stands, the two failures that are different problems, the tenant that
is not a machine, and the end customer's own scope deciding what counts as a
gap. Every name is made up.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
import respx

from app.connectors.base import ConnectorLimits
from app.connectors.cove.client import JSONAPI_PATH, CoveClient
from app.connectors.cove.tools import (
    count_backups,
    get_device_backup,
    list_backup_failures,
)
from app.connectors.scope import ClientScope, SiteRef
from app.models.enums import ConnectorName
from tests.connectors.test_cove_client import Conversation, login_answer

BASE = "https://cove.test.invalid"
URL = f"{BASE}{JSONAPI_PATH}"
CLIENT_ID = uuid.uuid4()

FABRIKAM = SiteRef(site_id=uuid.uuid4(), slug="fabrikam", name="Fabrikam", external_id="300100")
CONTOSO = SiteRef(site_id=uuid.uuid4(), slug="contoso", name="Contoso", external_id="300200")


def machine(
    account_id: int,
    name: str,
    *,
    partner: str = "300100",
    bar: str = "5" * 28,
    last_success_hours: int | None = 2,
    product: str = "All-In",
    storage: int = 120_000_000_000,
) -> dict[str, Any]:
    """One `EnumerateAccountStatistics` row, in the vendor's own column codes."""
    now = dt.datetime.now(dt.UTC)
    stamp = (
        ""
        if last_success_hours is None
        else str(int((now - dt.timedelta(hours=last_success_hours)).timestamp()))
    )
    return {
        "AccountId": account_id,
        "PartnerId": int(partner),
        "Settings": [
            {"I1": name},
            {"I59": "1"},
            {"I10": product},
            {"I32": "server"},
            {"I14": str(storage)},
            {"D09F08": bar},
            # Across every data source (`D09`): the device's own answer, which
            # is what a status word is made of.
            {"D09F00": bar[0] if bar else "0"},
            {"D09F09": stamp},
        ],
    }


def tenant(account_id: int, name: str, *, partner: str = "300100") -> dict[str, Any]:
    """A customer's Microsoft 365 tenant: a Cove device that is not a machine."""
    return {
        "AccountId": account_id,
        "PartnerId": int(partner),
        "Settings": [{"I1": name}, {"I59": "2"}],
    }


def make_client(*, limits: ConnectorLimits, sites: list[SiteRef] | None = None) -> CoveClient:
    allowed = tuple(sites if sites is not None else [FABRIKAM])
    return CoveClient(
        scope=ClientScope(
            client_id=CLIENT_ID,
            client_slug="tailwind-it",
            connector=ConnectorName.COVE,
            allowed=allowed,
            targets=allowed,
        ),
        secrets={"password": "a-password-nobody-should-see"},
        config={"base_url": BASE, "partner": "Tailwind IT", "username": "api@example.invalid"},
        limits=limits,
    )


def answer(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"result": {"result": rows}}


# --- the failures ---------------------------------------------------------------------


@respx.mock
async def test_the_two_failures_are_different_problems(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    """A job that ran and broke wants a different person from a machine
    nothing has run on for a week, so they are never merged into one word."""
    rows = [
        machine(1, "fab-srv-01"),
        machine(2, "fab-srv-02", bar="2" + "5" * 27, last_success_hours=4),
        machine(3, "fab-srv-03", bar="0" * 28, last_success_hours=24 * 11),
        machine(4, "fab-srv-04", bar="8" + "5" * 27),
        tenant(5, "Fabrikam Microsoft 365"),
    ]
    talk = Conversation(login_answer(partner_id=300000), answer(rows), answer([]))
    respx.post(URL).mock(side_effect=talk)
    stub_factory(make_client(limits=fast_limits))

    result = await list_backup_failures(str(CLIENT_ID))

    assert result.machines == 4
    assert result.microsoft365_tenants == 1
    assert result.by_status == {"not_backed_up": 1, "failed": 1, "warnings": 1, "ok": 1}
    # Worst first, and the one that completed with errors is not listed by default.
    assert [(r.device, r.status) for r in result.rows] == [
        ("fab-srv-03", "not_backed_up"),
        ("fab-srv-02", "failed"),
    ]
    assert result.rows[0].days_since_successful == 11
    assert result.total == 2


@respx.mock
async def test_a_warning_is_offered_and_not_assumed(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    """The job finished and reported errors: the data is kept and somebody
    still has to look, which is neither a failure nor nothing."""
    talk = Conversation(
        login_answer(partner_id=300000),
        answer([machine(4, "fab-srv-04", bar="8" + "5" * 27)]),
        answer([]),
    )
    respx.post(URL).mock(side_effect=talk)
    stub_factory(make_client(limits=fast_limits))

    result = await list_backup_failures(str(CLIENT_ID), include_warnings=True)

    assert [(r.device, r.status) for r in result.rows] == [("fab-srv-04", "warnings")]
    assert result.rows[0].failed_or_missed_days == "0 of 28"


@respx.mock
async def test_the_history_is_reported_beside_a_status_about_now(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    """A machine that failed a fortnight ago and has backed up every night
    since is backed up. The month is a count beside the word, never the word."""
    recovered = machine(1, "fab-srv-01", bar="5" * 20 + "2" + "5" * 7)
    talk = Conversation(login_answer(partner_id=300000), answer([recovered]), answer([]))
    respx.post(URL).mock(side_effect=talk)
    stub_factory(make_client(limits=fast_limits))

    result = await list_backup_failures(str(CLIENT_ID), include_warnings=True)

    assert result.by_status == {"ok": 1}
    assert result.rows == []
    # The month is still there, as a count beside the word.
    assert result.total == 0


# --- counting -------------------------------------------------------------------------


@respx.mock
async def test_a_tenant_is_never_counted_as_a_machine(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    """Cove holds a customer's Microsoft 365 tenant as a device of its own,
    and "5 protected" is not five machines."""
    rows = [
        machine(1, "fab-srv-01"),
        machine(2, "fab-srv-02", product="Documents"),
        tenant(3, "Fabrikam Microsoft 365"),
    ]
    talk = Conversation(login_answer(partner_id=300000), answer(rows), answer([]))
    respx.post(URL).mock(side_effect=talk)
    stub_factory(make_client(limits=fast_limits))

    counts = await count_backups(str(CLIENT_ID), group_by="retention")

    assert counts.machines == 2
    assert counts.microsoft365_tenants == 1
    assert counts.counts == {"All-In": 1, "Documents": 1}


@respx.mock
async def test_counting_by_site_names_the_end_customer(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    rows = [
        machine(1, "fab-srv-01"),
        machine(2, "con-srv-01", partner="300200"),
        machine(3, "con-srv-02", partner="300200"),
    ]
    talk = Conversation(login_answer(partner_id=300000), answer(rows), answer([]))
    respx.post(URL).mock(side_effect=talk)
    stub_factory(make_client(limits=fast_limits, sites=[FABRIKAM, CONTOSO]))

    counts = await count_backups(str(CLIENT_ID), group_by="site")

    assert counts.counts == {"Contoso": 2, "Fabrikam": 1}
    assert counts.sites_covered == ["contoso", "fabrikam"]


# --- one machine ------------------------------------------------------------------------


@respx.mock
async def test_one_machine_is_found_by_the_name_an_engineer_types(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    """Cove's own `_xxxxx` suffix and a domain suffix are both off the name."""
    talk = Conversation(
        login_answer(partner_id=300000),
        answer([machine(1, "fab-srv-01_ab12c"), machine(2, "fab-srv-02")]),
        answer([]),
    )
    respx.post(URL).mock(side_effect=talk)
    stub_factory(make_client(limits=fast_limits))

    found = await get_device_backup(str(CLIENT_ID), "FAB-SRV-01.fabrikam.example")

    assert found.found is True
    assert found.backup is not None
    assert found.backup.device == "fab-srv-01_ab12c"
    assert found.backup.status == "ok"
    assert found.backup.retention == "All-In"
    assert found.backup.used_storage_gb == 120.0
    assert found.candidates == []


@respx.mock
async def test_a_machine_cove_does_not_hold_is_not_a_failing_backup(
    stub_factory: Any, fast_limits: ConnectorLimits
) -> None:
    """The two are different claims, and the answer says which one it is and
    which tool answers the other."""
    talk = Conversation(
        login_answer(partner_id=300000), answer([machine(1, "fab-srv-01")]), answer([])
    )
    respx.post(URL).mock(side_effect=talk)
    stub_factory(make_client(limits=fast_limits))

    missing = await get_device_backup(str(CLIENT_ID), "fab-ws-99")

    assert missing.found is False
    assert missing.backup is None
    assert missing.detail is not None
    assert "list_machines_without_backup" in missing.detail


async def test_a_name_that_is_not_a_hostname_is_a_correctable_error() -> None:
    """The loop hands a tool's ValueError back to the model to correct, so the
    message is written for the model."""
    with pytest.raises(ValueError, match="device_name must be a hostname"):
        await get_device_backup(str(CLIENT_ID), "   ")


# --- what the model is told these tools are not for ---------------------------------------


def test_four_tools_registered_each_saying_what_it_is_not_for() -> None:
    """Overlapping tools stay distinguishable only if each names the other."""
    from app.agent.registry import tools_for

    names = {entry.name for entry in tools_for(ConnectorName.COVE)}
    assert names == {
        "count_backups",
        "get_device_backup",
        "list_backup_failures",
        "list_machines_without_backup",
    }
    for entry in tools_for(ConnectorName.COVE):
        assert "Not for" in entry.description, entry.name

"""The forward shadow's own machinery, with no database and no vendor.

The pieces here are the ones that decide how hard the poller leans on Autotask
and what a snapshot is allowed to remember. Both are worth pinning without a
database in the way.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx
import pytest

from app.autopilot.shadow.forward import (
    LIVE_CONNECTORS,
    NOT_BUILT,
    Backoff,
    RequestBudget,
    keep_value,
    reduce_facts,
)
from app.connectors.base import REFUSAL_BODY_LIMIT, ConnectorLimits
from tests.connectors.conftest import make_autotask_client

# --- the request budget ---------------------------------------------------


def test_the_budget_refuses_past_its_hour() -> None:
    """Our cap, not the vendor's: Autotask refused everything for an hour on
    2026-10-01 and nobody knows what its threshold returns."""
    budget = RequestBudget(per_hour=3)
    assert [budget.spend() for _ in range(4)] == [True, True, True, False]
    assert budget.spent == 3


def test_the_budget_starts_again_in_the_next_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.tickets.arrivals.time.monotonic", lambda: clock["now"])
    budget = RequestBudget(per_hour=1)
    assert budget.spend() is True
    assert budget.spend() is False
    clock["now"] += 3601
    assert budget.spend() is True


def test_a_pass_can_ask_for_several_requests_at_once() -> None:
    budget = RequestBudget(per_hour=5)
    assert budget.spend(4) is True
    assert budget.spend(4) is False  # would be nine
    assert budget.spend(1) is True


# --- backing off ----------------------------------------------------------


def test_backing_off_doubles_and_then_stops_doubling() -> None:
    backoff = Backoff(base_seconds=30.0, ceiling_seconds=240.0)
    assert [backoff.record_failure() for _ in range(6)] == [30, 60, 120, 240, 240, 240]


def test_a_success_clears_the_backoff() -> None:
    backoff = Backoff(base_seconds=10.0)
    backoff.record_failure()
    backoff.record_failure()
    backoff.record_success()
    assert backoff.record_failure() == 10.0


# --- what a snapshot may remember -----------------------------------------


@pytest.mark.parametrize("value", [True, False, 0, 1, 42, 3.5, None])
def test_a_number_or_a_flag_is_evidence_and_is_kept(value: Any) -> None:
    assert keep_value(value) == value


def test_a_time_is_kept_as_a_time() -> None:
    moment = dt.datetime(2026, 9, 16, 12, tzinfo=dt.UTC)
    assert keep_value(moment) == moment.isoformat()
    assert keep_value(dt.date(2026, 9, 16)) == "2026-09-16"


@pytest.mark.parametrize(
    "value",
    [
        "TL-AUS-WS14",  # a hostname
        "priya.raman@customer.test",  # a person
        "Acme Dental",  # a customer
        "1.2.3.4",
    ],
)
def test_a_string_is_a_name_until_proved_otherwise_and_is_dropped(value: str) -> None:
    """The snapshot's whole point is counts, states and read times. A string
    could be any of a machine's name, a person's or a customer's, so none of
    them is kept."""
    kept = keep_value(value)
    assert kept == {"type": "text"}
    assert value not in str(kept)


def test_a_collection_is_kept_as_its_size() -> None:
    assert keep_value([1, 2, 3]) == {"type": "list", "length": 3}
    assert keep_value({"a": 1}) == {"type": "object", "keys": 1}


def test_facts_reduce_to_id_source_and_a_safe_value() -> None:
    class Fact:
        def __init__(self, id: str, value: Any, source: str) -> None:
            self.id = id
            self.value = value
            self.source = source

    reduced = reduce_facts(
        [
            Fact("device.pending_reboot", True, "get_device"),
            Fact("device.last_contact", "TL-AUS-WS14 at 10:00", "get_device"),
            Fact("vulns.critical", 7, "get_asset_vulnerabilities"),
        ]
    )
    assert reduced[0] == {
        "id": "device.pending_reboot",
        "source": "get_device",
        "value": True,
    }
    assert reduced[1]["value"] == {"type": "text"}
    assert reduced[2]["value"] == 7
    assert "TL-AUS-WS14" not in str(reduced)


def test_the_tools_that_do_not_exist_are_named() -> None:
    """Blackpoint is not built, and a snapshot says so: a tool nobody asked is a
    gap in the evidence, not a clean bill of health. Cove moved onto the live
    list when its connector landed, and a customer who has none is carried by
    the enrichment's own `backup.not_assessed` reading — "not set up for this
    customer" is a different claim from "Desk cannot ask"."""
    assert set(NOT_BUILT) == {"blackpoint"}
    assert set(LIVE_CONNECTORS) == {"ninjaone", "bitdefender", "connectsecure", "cove"}
    assert not set(NOT_BUILT) & set(LIVE_CONNECTORS)


# --- what a refusal tells us ----------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_refusal_is_logged_in_full_so_the_threshold_can_be_identified(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    """The open api-notes question: what does an exceeded threshold return?

    It may not be a 429, in which case our retry path never backs off for it.
    The only way to find out is to have the next one on the record whole.
    """
    client = make_autotask_client(limits=ConnectorLimits())
    response = httpx.Response(
        status,
        headers={
            "X-Vendor-Note": "too many requests this hour",
            "Set-Cookie": "session=secret-value",
        },
        text="the tenant has exceeded its hourly threshold",
        request=httpx.Request("POST", "https://example.test/V1.0/Tickets/query"),
    )
    with caplog.at_level(logging.WARNING):
        client.log_refusal(response, "/V1.0/Tickets/query", 0)
    logged = caplog.text
    assert str(status) in logged
    assert "too many requests this hour" in logged  # the header that explains it
    assert "exceeded its hourly threshold" in logged  # and the body
    assert "secret-value" not in logged  # but never a cookie
    assert "Set-Cookie" not in logged


def test_a_long_refusal_body_is_truncated(caplog: pytest.LogCaptureFixture) -> None:
    client = make_autotask_client(limits=ConnectorLimits())
    response = httpx.Response(
        500,
        text="x" * (REFUSAL_BODY_LIMIT * 3),
        request=httpx.Request("POST", "https://example.test/q"),
    )
    with caplog.at_level(logging.WARNING):
        client.log_refusal(response, "/q", 0)
    assert len(caplog.text) < REFUSAL_BODY_LIMIT * 2

"""The last-seen read: who is asked, who is not, and what is kept. No vendor, no database."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

import app.devices.lastseen as lastseen_module
from app.connectors.scope import SiteRef
from app.coverage.core import ManagedDevice, ToolEndpoint, ToolInventory, assess, mark_unchecked
from app.devices.lastseen import LastSeenReads, candidates_for, read_last_seen
from app.settings import get_settings

NOW = dt.datetime(2026, 9, 28, 12, tzinfo=dt.UTC)
SITE = uuid.uuid5(uuid.NAMESPACE_DNS, "acme")
REF = SiteRef(site_id=SITE, slug="acme", name="Acme", external_id="c1")


def lost(name: str, days: int | None) -> ManagedDevice:
    return ManagedDevice(
        site_id=SITE,
        site_slug="acme",
        site_name="Acme",
        device_id=name.lower(),
        name=name,
        node_class="WINDOWS_WORKSTATION",
        last_contact=None if days is None else NOW - dt.timedelta(days=days),
    )


def row(
    name: str, external_id: str, *, state: str = "covered", scanned: int | None = None
) -> ToolEndpoint:
    return ToolEndpoint(
        site_id=SITE,
        name=name,
        state=state,  # type: ignore[arg-type]
        detail=state,
        external_id=external_id,
        last_seen=None if scanned is None else NOW - dt.timedelta(days=scanned),
    )


def test_only_lost_devices_inside_the_window_without_a_recent_scan_are_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVICE_RETIRE_DAYS", "90")
    get_settings.cache_clear()
    devices = [
        lost("FRESH", 3),  # not lost: never a candidate
        lost("SCANNED", 30),  # lost, but scanned 5 days ago: alive without a call
        lost("ASK-ME", 30),  # lost, no scan date: the one to ask
        lost("OLD-SCAN", 30),  # lost, scanned 40 days ago: ask, the scan is too old
        lost("LONG-GONE", 120),  # lost past the retire window: not checked
        lost("NEVER", None),  # never contacted NinjaOne: inside the window, asked
        lost("NO-AGENT", 30),  # lost, its row has no agent: nothing to ask
    ]
    rows = [
        row("FRESH", "1"),
        row("SCANNED", "2", scanned=5),
        row("ASK-ME", "3"),
        row("OLD-SCAN", "4", scanned=40),
        row("LONG-GONE", "5"),
        row("NEVER", "6"),
        row("NO-AGENT", "7", state="absent"),
    ]
    inventory = ToolInventory(endpoints=rows, sites=frozenset({SITE}))
    assessment = assess(devices, {"bitdefender": inventory}, now=NOW, ninjaone_stale_days=14)
    reads = LastSeenReads()
    candidates = candidates_for(
        assessment,
        rows_by_id={r.external_id or "": r for r in rows},
        refs={r.external_id or "": REF for r in rows},
        devices_by_id={d.device_id: d for d in devices},
        now=NOW,
        threshold_days=14,
        reads=reads,
    )
    assert sorted(c.external_id for c in candidates) == ["3", "4", "6"]
    assert reads.lost_devices == 6 and reads.alive_by_scan == 1 and reads.beyond_window == 1
    assert reads.unchecked_devices == ["long-gone"]
    # The device past the window says so on the board.
    mark_unchecked(assessment, "bitdefender", reads.unchecked_devices, 90)
    verdicts = {d.device.device_id: d.tools["bitdefender"] for d in assessment.devices}
    assert verdicts["long-gone"].state == "rmm_stale"
    assert verdicts["long-gone"].detail.endswith(
        "last-seen not checked (lost for more than 90 days)"
    )
    assert verdicts["scanned"].state == "rmm_agent_down"
    assert "not checked" not in verdicts["ask-me"].detail


async def test_a_fresh_date_on_the_link_is_read_back_and_a_stale_one_is_fetched_again(
    monkeypatch: pytest.MonkeyPatch, last_seen_in_memory: dict[str, Any]
) -> None:
    monkeypatch.setenv("BITDEFENDER_LAST_SEEN_REFRESH_HOURS", "12")
    get_settings.cache_clear()
    calls: list[str] = []

    class Detail:
        def __init__(self, when: dt.datetime) -> None:
            self.last_seen = when

    class FakeConnector:
        async def get_endpoint_details(self, ref: SiteRef, endpoint_id: str) -> Detail:
            calls.append(endpoint_id)
            return Detail(NOW - dt.timedelta(days=1))

        async def __aenter__(self) -> FakeConnector:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    def connector_for(client_id: uuid.UUID, connector: Any) -> FakeConnector:
        return FakeConnector()

    monkeypatch.setattr(lastseen_module, "connector_for", connector_for)
    monkeypatch.setattr(lastseen_module, "BitdefenderClient", FakeConnector)
    from types import SimpleNamespace

    # One link checked an hour ago, one checked yesterday, one never.
    last_seen_in_memory["fresh"] = SimpleNamespace(
        external_id="fresh",
        vendor_last_seen_at=NOW - dt.timedelta(days=3),
        vendor_checked_at=NOW - dt.timedelta(hours=1),
    )
    last_seen_in_memory["stale"] = SimpleNamespace(
        external_id="stale",
        vendor_last_seen_at=NOW - dt.timedelta(days=30),
        vendor_checked_at=NOW - dt.timedelta(hours=30),
    )
    candidates = [
        lastseen_module.Candidate(i, REF, i, 20, row(i.upper(), i))
        for i in ("fresh", "stale", "new")
    ]
    reads = LastSeenReads()
    seen = await read_last_seen(uuid.uuid4(), candidates, now=NOW, reads=reads)
    assert sorted(calls) == ["new", "stale"]
    assert seen["fresh"] == NOW - dt.timedelta(days=3)
    assert seen["stale"] == seen["new"] == NOW - dt.timedelta(days=1)
    assert reads.from_links == 1 and reads.fetched == 2 and reads.failed is False
    # What was fetched is kept, with the time it was checked.
    assert last_seen_in_memory["stale"].vendor_checked_at == NOW
    assert last_seen_in_memory["new"].vendor_last_seen_at == NOW - dt.timedelta(days=1)
    # Asked again within the window: nothing is fetched.
    calls.clear()
    again = LastSeenReads()
    await read_last_seen(uuid.uuid4(), candidates, now=NOW, reads=again)
    assert calls == [] and again.from_links == 3 and again.fetched == 0
    # With the window at zero every candidate is fetched every run.
    monkeypatch.setenv("BITDEFENDER_LAST_SEEN_REFRESH_HOURS", "0")
    get_settings.cache_clear()
    third = LastSeenReads()
    await read_last_seen(uuid.uuid4(), candidates, now=NOW + dt.timedelta(seconds=1), reads=third)
    assert sorted(calls) == ["fresh", "new", "stale"] and third.fetched == 3


async def test_detail_calls_run_under_the_configured_concurrency(
    monkeypatch: pytest.MonkeyPatch, last_seen_in_memory: dict[str, Any]
) -> None:
    import asyncio

    monkeypatch.setenv("BITDEFENDER_DETAIL_CONCURRENCY", "2")
    monkeypatch.setenv("BITDEFENDER_LAST_SEEN_REFRESH_HOURS", "0")
    get_settings.cache_clear()
    in_flight = 0
    peak = 0

    class FakeConnector:
        async def get_endpoint_details(self, ref: SiteRef, endpoint_id: str) -> Any:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return type("D", (), {"last_seen": NOW})()

        async def __aenter__(self) -> FakeConnector:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(lastseen_module, "connector_for", lambda *a, **k: FakeConnector())
    monkeypatch.setattr(lastseen_module, "BitdefenderClient", FakeConnector)
    candidates = [
        lastseen_module.Candidate(str(i), REF, str(i), 20, row(f"R{i}", str(i))) for i in range(9)
    ]
    reads = LastSeenReads()
    await read_last_seen(uuid.uuid4(), candidates, now=NOW, reads=reads)
    assert reads.fetched == 9 and peak == 2

"""The budget's headroom and the shared backoff, without a vendor.

Both exist because Autotask refused every query for about an hour on
2026-10-01 and nobody knows what its own threshold returns (api-notes). The
poller and the backfill answer that the same way, which is only true if they
share this code — so what it does is pinned here.
"""

from __future__ import annotations

import pytest

from app.autopilot.shadow.forward import Backoff, RequestBudget, with_backoff


def test_the_budget_says_how_much_headroom_is_left() -> None:
    """The backfill asks before it starts, because a run that stops mid-ticket
    leaves a prediction with no enrichment."""
    budget = RequestBudget(per_hour=10)
    assert budget.remaining == 10
    budget.spend(4)
    assert budget.remaining == 6
    assert budget.spend(6) is True
    assert budget.remaining == 0
    assert budget.spend() is False


async def test_a_read_that_works_is_not_retried() -> None:
    calls: list[int] = []

    async def read() -> str:
        calls.append(1)
        return "answered"

    assert await with_backoff(read, sleep=_no_wait) == "answered"
    assert len(calls) == 1


async def test_a_read_that_fails_is_retried_with_a_doubling_wait() -> None:
    """The wait doubles rather than the request repeating: a vendor refusing
    everything is not a vendor that wants asking again immediately."""
    waits: list[float] = []
    attempts: list[int] = []

    async def read() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("502")
        return "answered"

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    found = await with_backoff(read, backoff=Backoff(base_seconds=30), sleep=sleep)
    assert found == "answered"
    assert waits == [30.0, 60.0]


async def test_the_last_failure_is_raised_rather_than_swallowed() -> None:
    """A backfill that quietly returned no tickets because Autotask was down
    would read as a client with nothing open."""

    async def read() -> str:
        raise RuntimeError("still refusing")

    with pytest.raises(RuntimeError, match="still refusing"):
        await with_backoff(read, attempts=2, sleep=_no_wait)


async def test_the_caller_is_told_about_each_wait() -> None:
    """So a terminal can say it, rather than appearing to hang."""
    said: list[tuple[int, float, str]] = []

    async def read() -> str:
        raise RuntimeError("no")

    def on_wait(attempt: int, waited: float, error: Exception) -> None:
        said.append((attempt, waited, type(error).__name__))

    with pytest.raises(RuntimeError):
        await with_backoff(read, attempts=3, sleep=_no_wait, on_wait=on_wait)
    assert [one[0] for one in said] == [1, 2]
    assert [one[2] for one in said] == ["RuntimeError", "RuntimeError"]


async def _no_wait(seconds: float) -> None:
    del seconds


def test_a_successful_read_resets_the_doubling() -> None:
    """Otherwise an hour of occasional failures ends up waiting a quarter of an
    hour after one that worked."""
    waits = Backoff(base_seconds=10)
    waits.record_failure()
    waits.record_failure()
    waits.record_success()
    assert waits.record_failure() == 10.0


def test_the_wait_has_a_ceiling() -> None:
    waits = Backoff(base_seconds=30, ceiling_seconds=120)
    seen: list[float] = [waits.record_failure() for _ in range(6)]
    assert max(seen) == 120.0

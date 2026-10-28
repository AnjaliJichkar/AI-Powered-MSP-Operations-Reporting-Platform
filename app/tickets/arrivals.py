"""A ticket as a poller found it, and the budget and backoff a poller reads
a vendor under. Shared by the forward shadow and the automation triggers'
poll; imports no connector, so either may import it.

* **A request budget per hour, ours and not the vendor's.** Autotask refused
  every query for about an hour on 2026-10-01 and nobody knows what its own
  threshold returns, so a poller counts its requests and stops rather than
  finding out the hard way (`RequestBudget`).
* **Back off on anything that is not an answer.** Any failure doubles the
  wait, up to a ceiling; the last failure is raised rather than swallowed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class NewTicket:
    """A ticket as the poller found it: what was known when it was created."""

    ticket_id: str
    ticket_number: str | None
    title: str
    site_id: uuid.UUID | None
    site_slug: str | None
    created_at: dt.datetime | None
    contact: str | None
    """Used only to find the machine, exactly as the workspace uses it. It is
    never shown to the classifier, which sees the subject, the customer handle
    and the time."""
    queue_label: str | None = None
    """The queue the ticket was raised into, read **at arrival**. This is the
    one thing the forward shadow knows that a replay cannot: the ledger reads
    the queue at completion, by which time it says where the ticket was sent."""
    description: str | None = None
    """The body, unscrubbed as the vendor gave it. Scrubbed on the way to the
    model and stored nowhere — the prediction records only that there was
    one."""
    issue_type_label: str | None = None
    sub_issue_type_label: str | None = None
    source_label: str | None = None
    """The three labels an automation trigger's rule may read (`app/automation/
    triggers.py`), resolved through the tenant's own picklists. The classifier
    never sees them: issue type is what it is asked to predict."""
    contact_id: int | None = None
    """The ticket's contact, by id, for a trigger that fills an input from the
    contact's name. Not a name: a name is read only for a ticket a rule matched."""


@dataclass
class RequestBudget:
    """Requests an hour, counted by us because the vendor will not say.

    Not a rate limiter in the usual sense: there is no queue and nothing waits
    its turn. It answers one question — may another request be made — and the
    poller stops for the hour when the answer is no.
    """

    per_hour: int
    #: Set on the first spend, not at construction: a budget built at start-up
    #: and first used an hour later should have a whole hour, not none of one.
    _window_started: float | None = None
    _spent: int = 0

    def spend(self, count: int = 1) -> bool:
        """Take `count` requests from the budget, or refuse."""
        now = time.monotonic()
        if self._window_started is None or now - self._window_started >= 3600:
            self._window_started, self._spent = now, 0
        if self._spent + count > self.per_hour:
            return False
        self._spent += count
        return True

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        """How many more requests this hour allows.

        Read by the backfill, which decides **before** it starts how many
        tickets it can afford: a run that stops halfway through because the
        budget ran out mid-ticket leaves a prediction with no enrichment, and
        a half-enriched ticket is worse than one nobody got to.
        """
        if self._window_started is not None and time.monotonic() - self._window_started >= 3600:
            return self.per_hour
        return max(0, self.per_hour - self._spent)


@dataclass
class Backoff:
    """Doubling wait after anything that is not an answer.

    Reset by a success. The ceiling exists so a poller that has been refused
    for an hour is still polling once in a while rather than sleeping for a
    day.
    """

    base_seconds: float = 30.0
    ceiling_seconds: float = 900.0
    failures: int = 0

    def record_failure(self) -> float:
        self.failures += 1
        waited = self.base_seconds * 2 ** (self.failures - 1)
        return float(min(waited, self.ceiling_seconds))

    def record_success(self) -> None:
        self.failures = 0


async def with_backoff(
    call: Callable[[], Awaitable[Any]],
    *,
    backoff: Backoff | None = None,
    attempts: int = 4,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    on_wait: Callable[[int, float, Exception], None] | None = None,
) -> Any:
    """Run a vendor read, doubling the wait after anything that is not an answer.

    The one place polling and the backfill share: both talk to a vendor that
    refused every query for about an hour on 2026-10-01 without saying why
    (api-notes), and neither should answer that by hammering it. `on_wait` is
    told which attempt failed, how long the wait is and what was raised, so the
    terminal can say it rather than appearing to hang.

    The last failure is raised rather than swallowed: a backfill that quietly
    returned no tickets because Autotask was down would look like a client with
    nothing open.
    """
    waits = backoff or Backoff()
    pause = sleep or asyncio.sleep
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            found = await call()
        except Exception as exc:
            last = exc
            if attempt == attempts:
                break
            waited = waits.record_failure()
            if on_wait is not None:
                on_wait(attempt, waited, exc)
            await pause(waited)
            continue
        waits.record_success()
        return found
    assert last is not None
    raise last

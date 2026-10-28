"""Whether a ticket met its targets, computed rather than read off the vendor.

Autotask has SLA fields and on the pilot tenant they cannot be used:
`serviceLevelAgreementHasBeenMet` is populated on **none** of the open tickets,
`firstResponseDueDateTime` on about two thirds and `resolvedDueDateTime` on a
third (docs/api-notes/autotask.md). A breach count drawn from those fields is a
count of the tickets somebody remembered to configure, which is why the old
`sla_state` — which took whatever due times Autotask happened to carry — could
report a franchisee as breaching on a third of their tickets and missing the
rest entirely.

So the targets come from the franchisee's own settings (`app/resolved/config.py`),
the clock runs on their business hours in their own time zone with their
holidays (`app/resolved/clock.py`), and this module compares the two.

**The definitions, in full, because they are what a service is sold against.**

*First response.* From the moment Autotask stamped the ticket created, to the
moment it stamped a first response. Working time only. A ticket with no
response yet is measured to now. Met when the working time used is at or under
the target for that ticket's priority.

*Resolution.* From created to the completed date Autotask stamped. Working time
only. A ticket not completed is measured to now.

*Paused.* A ticket whose **current** status is one the franchisee named as
pausing — waiting on the customer, on a part, on a vendor — has its clock
stopped. Autotask's REST API exposes no status history we read, so we cannot
know how long a ticket *has been* paused, only that it is paused now. A clock
that is running and already over target is a breach whatever happens next; a
clock that is paused is reported as paused and **no breach is asserted from
it**, because the time it has been waiting is time we cannot account for. That
is a deliberate under-count: it is better to say "we cannot tell" than to bill a
franchisee for a breach caused by their customer not answering the phone.

*No target.* A priority the settings do not name, with no default, has no
target. It is reported as having none and is left out of every percentage —
never counted as met, which would flatter, and never as breached, which would
be a number nobody could defend.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.reports.tbr.config import BusinessHours, load_config
from app.resolved.clock import BusinessClock, franchisee_zone
from app.resolved.config import ResolvedConfig

#: What a clock can be. `met` and `breached` are settled; `running` is not yet
#: late; `paused` and `no_target` are the two ways of saying we will not judge.
ClockState = Literal["met", "breached", "running", "paused", "no_target"]

#: The two clocks, in the order a ticket meets them.
FIRST_RESPONSE = "first_response"
RESOLUTION = "resolution"

#: How close to its deadline a running clock has to be to be worth saying so.
DUE_SOON = dt.timedelta(hours=24)


@dataclass(frozen=True)
class Clock:
    """One clock on one ticket."""

    kind: str
    state: ClockState
    target_minutes: int | None
    used_minutes: int | None
    due_at: dt.datetime | None
    detail: str
    settled: bool = False
    """The thing it was waiting for has happened: a response was recorded, or
    the ticket completed. A settled clock is history — it still counts towards
    every percentage, and it no longer decides what the queue's badge says,
    because a first response missed last week is not what to do about a ticket
    that is progressing fine now."""

    @property
    def met(self) -> bool | None:
        """True, False, or None when we will not judge it. The ledger stores
        exactly this, so a percentage can be taken over the ones we judged."""
        if self.state == "met":
            return True
        if self.state == "breached":
            return False
        return None


@dataclass(frozen=True)
class TicketSla:
    """Both clocks, and the one word the queue puts on the row."""

    first_response: Clock
    resolution: Clock
    state: str
    detail: str
    due_at: dt.datetime | None

    def as_json(self) -> dict[str, Any]:
        """The shape the pages read. `state` is what the badge is drawn from."""
        return {
            "state": self.state,
            "detail": self.detail,
            "due_at": self.due_at,
            "clocks": {
                clock.kind: {
                    "state": clock.state,
                    "target_minutes": clock.target_minutes,
                    "used_minutes": clock.used_minutes,
                    "due_at": clock.due_at,
                    "detail": clock.detail,
                }
                for clock in (self.first_response, self.resolution)
            },
        }


async def calculator_for(
    session: AsyncSession, client_id: uuid.UUID, *, client_name: str | None = None
) -> SlaCalculator:
    """This franchisee's calculator, from their own stored settings.

    One function, so the queue, the statistics, the review and the ledger all
    judge a ticket against the same targets on the same clock. A franchisee who
    has set nothing gets a calculator with no targets, which reports every clock
    as having none rather than inventing one.
    """
    config = await load_config(session, client_id, client_name=client_name or "")
    zone, _ = franchisee_zone(config.business_hours.timezone)
    return SlaCalculator(config.resolved, config.business_hours, zone)


def _words(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} h".replace(".0 ", " ")
    return f"{hours / 8:.1f} working days".replace(".0 ", " ")


class SlaCalculator:
    """One franchisee's targets and clock, ready to judge tickets with.

    Built once per request from the franchisee's settings, then asked about each
    ticket. It is given the ticket's **labels**, never its picklist numbers: the
    caller resolves those through the tenant's own field information, because a
    status number means different things in different Autotask databases.
    """

    __slots__ = ("clock", "config")

    def __init__(
        self,
        config: ResolvedConfig,
        hours: BusinessHours,
        zone: dt.tzinfo,
    ) -> None:
        self.config = config
        self.clock = BusinessClock(
            hours,
            zone,
            holidays=frozenset(config.sla.holidays),
            calendar=config.sla.clock == "calendar",
        )

    # --- one clock -------------------------------------------------------------------

    def _clock(
        self,
        kind: str,
        *,
        target_minutes: int | None,
        started: dt.datetime | None,
        stopped: dt.datetime | None,
        paused: bool,
        now: dt.datetime,
        stopped_name: str,
    ) -> Clock:
        if started is None:
            return Clock(
                kind, "no_target", target_minutes, None, None, "Autotask recorded no created time."
            )
        if target_minutes is None:
            used = self.clock.elapsed_minutes(started, stopped or now)
            return Clock(
                kind,
                "no_target",
                None,
                used,
                None,
                "No target is set for this priority, so nothing is judged. "
                f"{_words(used)} of working time have been used.",
            )
        budget = dt.timedelta(minutes=target_minutes)
        due_at = self.clock.deadline(started, budget)
        if stopped is not None:
            used = self.clock.elapsed_minutes(started, stopped)
            met = used <= target_minutes
            return Clock(
                kind,
                "met" if met else "breached",
                target_minutes,
                used,
                due_at,
                f"{_words(used)} of working time against a target of {_words(target_minutes)}; "
                f"{stopped_name} {'inside' if met else 'past'} it.",
                settled=True,
            )
        used = self.clock.elapsed_minutes(started, now)
        if used > target_minutes:
            # Already over. Pausing cannot undo a clock that has run out, so
            # this is a breach whether the ticket is waiting on somebody or not.
            return Clock(
                kind,
                "breached",
                target_minutes,
                used,
                due_at,
                f"{_words(used)} of working time against a target of {_words(target_minutes)}, "
                f"and no {stopped_name} yet.",
            )
        if paused:
            return Clock(
                kind,
                "paused",
                target_minutes,
                used,
                due_at,
                "The clock is stopped: this ticket's status is one that pauses it. How long it "
                "has been waiting is not something Autotask tells us, so nothing is judged.",
            )
        return Clock(
            kind,
            "running",
            target_minutes,
            used,
            due_at,
            f"{_words(used)} of working time used of {_words(target_minutes)}.",
        )

    # --- both clocks, and the word for the row ------------------------------------------

    def assess(
        self,
        ticket: Any,
        *,
        priority_label: str | None,
        status_label: str | None,
        now: dt.datetime,
    ) -> TicketSla:
        target = self.config.sla.target_for(priority_label)
        paused = self.config.sla.pauses(status_label)
        created = ticket.create_date
        first_response = self._clock(
            FIRST_RESPONSE,
            target_minutes=target.first_response_minutes if target else None,
            started=created,
            stopped=ticket.first_response_at,
            paused=paused,
            now=now,
            stopped_name="first answered",
        )
        resolution = self._clock(
            RESOLUTION,
            target_minutes=target.resolution_minutes if target else None,
            started=created,
            stopped=ticket.completed_date,
            paused=paused,
            now=now,
            stopped_name="completed",
        )
        state, detail, due_at = self._headline(first_response, resolution, now=now)
        return TicketSla(first_response, resolution, state, detail, due_at)

    def _headline(
        self, first_response: Clock, resolution: Clock, *, now: dt.datetime
    ) -> tuple[str, str, dt.datetime | None]:
        """One word for the queue: the worse of the two live clocks.

        A clock that has already stopped is history and does not decide what the
        row says now — a ticket answered late but still open is shown by where
        its resolution clock stands, with the missed response in the detail.
        """
        live = [
            clock
            for clock in (first_response, resolution)
            if not clock.settled and clock.state in ("breached", "running", "paused")
        ]
        if any(clock.state == "breached" for clock in live):
            worst = next(clock for clock in live if clock.state == "breached")
            return "breached", worst.detail, worst.due_at
        running = [clock for clock in live if clock.state == "running" and clock.due_at]
        if running:
            soonest = min(running, key=lambda clock: clock.due_at or now)
            due_at = soonest.due_at
            if due_at is not None and due_at - now <= DUE_SOON:
                return "due_soon", soonest.detail, due_at
            return "ok", soonest.detail, due_at
        if any(clock.state == "paused" for clock in live):
            paused = next(clock for clock in live if clock.state == "paused")
            return "paused", paused.detail, paused.due_at
        settled = [clock for clock in (first_response, resolution) if clock.settled]
        if settled:
            missed = [clock for clock in settled if clock.state == "breached"]
            if missed:
                return (
                    "ok",
                    "Both clocks have stopped. " + " ".join(clock.detail for clock in missed),
                    None,
                )
            return "ok", "Both clocks stopped and neither was missed.", None
        return "none", "No target is set for this ticket's priority.", None

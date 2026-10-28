"""Time as a service desk counts it: business hours, in the customer's zone.

Two questions, and everything about an SLA is one of them:

* **how much of the clock has run** between two moments (`elapsed`), and
* **when does it run out** if it starts now (`deadline`).

Both are answered by walking local days and intersecting each one with the
franchisee's open window. That is slower than arithmetic on a total and it is
the only way to be right across a daylight-saving change: four hours before the
clocks go forward and four hours after are not the same four hours, and a
resolution target that quietly moved by an hour twice a year would be a breach
nobody could explain.

A holiday is a closed day whatever the weekday says. A franchisee on a 24/7
agreement sets `clock: calendar` and both questions become subtraction.

Nothing here reads a setting or a database: it is given hours, a zone and a set
of holidays, so a test can ask it about a Friday afternoon in March without a
franchisee existing.
"""

from __future__ import annotations

import datetime as dt

from app.reports.tbr.config import BusinessHours, TimeZoneUnavailable, resolve_zone

#: A deadline is walked forward a day at a time. A target so long, or a week so
#: closed, that it does not land inside this many days is reported as not
#: landing rather than looped over forever.
MAX_DAYS = 400


def franchisee_zone(timezone: str | None) -> tuple[dt.tzinfo, str]:
    """The zone a franchisee's working day is counted in, and its name.

    Their own when they have set one, UTC when they have not — and the name is
    returned with it so every figure drawn from it can say which it used. A
    clock has to be in some zone; what it must not do is pretend it is in the
    customer's when nobody has said what that is. A configured zone this machine
    cannot load (a named zone with no time zone database) falls back the same
    way rather than failing the read.
    """
    if timezone:
        try:
            return resolve_zone(timezone), timezone
        except TimeZoneUnavailable:
            pass
    return dt.UTC, "UTC"


class BusinessClock:
    """One franchisee's working week, as something you can measure with."""

    __slots__ = ("_holidays", "calendar", "hours", "zone")

    def __init__(
        self,
        hours: BusinessHours,
        zone: dt.tzinfo,
        *,
        holidays: frozenset[dt.date] = frozenset(),
        calendar: bool = False,
    ) -> None:
        self.hours = hours
        self.zone = zone
        self._holidays = holidays
        #: True runs the clock continuously: no weekends, no evenings, no
        #: holidays. For a customer whose agreement says so.
        self.calendar = calendar

    # --- the working week ----------------------------------------------------------

    def is_working_day(self, day: dt.date) -> bool:
        if self.calendar:
            return True
        return day.weekday() in self.hours.days and day not in self._holidays

    def window(self, day: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
        """The open window on this local day, or nothing if it is closed."""
        if not self.is_working_day(day):
            return None
        if self.calendar:
            start = dt.datetime.combine(day, dt.time.min, tzinfo=self.zone)
            return start, start + dt.timedelta(days=1)
        opens = dt.datetime.combine(day, self.hours.start, tzinfo=self.zone)
        closes = dt.datetime.combine(day, self.hours.end, tzinfo=self.zone)
        return (opens, closes) if closes > opens else None

    def is_open(self, moment: dt.datetime) -> bool:
        local = moment.astimezone(self.zone)
        window = self.window(local.date())
        return bool(window and window[0] <= local < window[1])

    # --- how much has run -----------------------------------------------------------

    def elapsed(self, start: dt.datetime, end: dt.datetime) -> dt.timedelta:
        """Working time between two moments. Never negative.

        Time outside the window at either end is not counted, so a ticket raised
        at eight in the evening starts its clock the next morning — which is what
        a target of "four business hours" means to the person who agreed it.
        """
        if end <= start:
            return dt.timedelta(0)
        if self.calendar:
            return end - start
        here = start.astimezone(self.zone)
        there = end.astimezone(self.zone)
        total = dt.timedelta(0)
        day = here.date()
        last = there.date()
        while day <= last:
            window = self.window(day)
            if window is not None:
                opens, closes = window
                first = max(opens, here)
                final = min(closes, there)
                if final > first:
                    total += final - first
            day += dt.timedelta(days=1)
        return total

    def elapsed_minutes(self, start: dt.datetime, end: dt.datetime) -> int:
        return int(self.elapsed(start, end).total_seconds() // 60)

    # --- when it runs out -------------------------------------------------------------

    def deadline(self, start: dt.datetime, budget: dt.timedelta) -> dt.datetime | None:
        """When `budget` of working time is used up, starting at `start`.

        `None` when it does not land inside `MAX_DAYS` — a franchisee who has
        set a target of a thousand hours, or a closed week with no open days at
        all, gets no due time rather than a wrong one.
        """
        if budget <= dt.timedelta(0):
            return start
        if self.calendar:
            return start + budget
        here = start.astimezone(self.zone)
        left = budget
        day = here.date()
        for _ in range(MAX_DAYS):
            window = self.window(day)
            if window is not None:
                opens, closes = window
                begin = max(opens, here) if day == here.date() else opens
                if closes > begin:
                    available = closes - begin
                    if available >= left:
                        return begin + left
                    left -= available
            day += dt.timedelta(days=1)
        return None

    def next_open(self, moment: dt.datetime) -> dt.datetime | None:
        """The first moment the clock runs at or after this one."""
        if self.calendar:
            return moment
        here = moment.astimezone(self.zone)
        day = here.date()
        for _ in range(MAX_DAYS):
            window = self.window(day)
            if window is not None:
                opens, closes = window
                if here < closes:
                    return max(opens, here)
            day += dt.timedelta(days=1)
        return None

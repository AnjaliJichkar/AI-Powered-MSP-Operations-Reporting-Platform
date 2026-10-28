"""The business clock: the arithmetic every SLA figure rests on.

Pure functions over instants, so every case here is a question somebody could
ask about a real ticket: raised on Friday evening, answered on Monday; raised
the day before a public holiday; a target longer than a working day. No
database, no vendor, no franchisee.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.reports.tbr.config import BusinessHours
from app.resolved.clock import BusinessClock, franchisee_zone

CHICAGO = dt.timezone(-dt.timedelta(hours=6))
NINE_TO_FIVE = BusinessHours(timezone="UTC-06:00", start=dt.time(9), end=dt.time(17))


def clock(**over: object) -> BusinessClock:
    return BusinessClock(NINE_TO_FIVE, CHICAGO, **over)  # type: ignore[arg-type]


def at(day: str, hour: int, minute: int = 0) -> dt.datetime:
    """A local moment in the franchisee's own zone."""
    year, month, date = (int(part) for part in day.split("-"))
    return dt.datetime(year, month, date, hour, minute, tzinfo=CHICAGO)


# --- elapsed --------------------------------------------------------------------------


def test_time_inside_one_working_day_is_just_the_difference() -> None:
    # Wednesday, 10:00 to 14:30.
    assert clock().elapsed_minutes(at("2026-09-16", 10), at("2026-09-16", 14, 30)) == 270


def test_the_clock_does_not_run_before_the_office_opens() -> None:
    """A ticket raised at seven in the morning starts its clock at nine, which
    is what "four business hours" means to the person who agreed it."""
    assert clock().elapsed_minutes(at("2026-09-16", 7), at("2026-09-16", 10)) == 60


def test_the_clock_does_not_run_after_it_closes() -> None:
    assert clock().elapsed_minutes(at("2026-09-16", 16), at("2026-09-16", 23)) == 60


def test_an_evening_to_the_next_morning_is_almost_nothing() -> None:
    """Raised at 20:00, answered at 09:30 the next day: half an hour of work."""
    assert clock().elapsed_minutes(at("2026-09-16", 20), at("2026-09-17", 9, 30)) == 30


def test_a_weekend_does_not_count() -> None:
    """Friday 16:00 to Monday 10:00 is one hour on Friday and one on Monday."""
    assert clock().elapsed_minutes(at("2026-09-18", 16), at("2026-09-21", 10)) == 120


def test_a_holiday_does_not_count() -> None:
    """The Monday is a holiday, so the same span loses a whole working day."""
    holiday = clock(holidays=frozenset({dt.date(2026, 9, 21)}))
    assert holiday.elapsed_minutes(at("2026-09-18", 16), at("2026-09-22", 10)) == 120


def test_several_whole_days_are_counted_a_day_at_a_time() -> None:
    # Monday 09:00 to Thursday 17:00: four working days of eight hours.
    assert clock().elapsed_minutes(at("2026-09-14", 9), at("2026-09-17", 17)) == 4 * 8 * 60


def test_an_end_before_the_start_is_zero_and_never_negative() -> None:
    assert clock().elapsed_minutes(at("2026-09-16", 14), at("2026-09-16", 10)) == 0


def test_calendar_mode_counts_everything() -> None:
    """A customer on a 24/7 agreement. Friday evening to Monday morning is the
    whole weekend."""
    always = clock(calendar=True)
    assert always.elapsed_minutes(at("2026-09-18", 16), at("2026-09-21", 10)) == 66 * 60


# --- across a daylight-saving change ---------------------------------------------------


def test_a_span_across_the_clocks_going_back_is_counted_in_local_days() -> None:
    """The reason this walks days rather than subtracting totals.

    America/Chicago puts its clocks back on 1 November 2026. Two working days
    either side of it are still two working days of eight hours each, whatever
    the number of elapsed hours says.
    """
    zone, name = franchisee_zone("America/Chicago")
    if name == "UTC":
        pytest.skip("no time zone database on this machine")
    hours = BusinessHours(timezone="America/Chicago", start=dt.time(9), end=dt.time(17))
    walking = BusinessClock(hours, zone)
    start = dt.datetime(2026, 10, 30, 9, tzinfo=zone)  # Friday
    end = dt.datetime(2026, 11, 2, 17, tzinfo=zone)  # Monday
    assert walking.elapsed_minutes(start, end) == 2 * 8 * 60


# --- deadlines --------------------------------------------------------------------------


def test_a_deadline_inside_the_same_day() -> None:
    assert clock().deadline(at("2026-09-16", 10), dt.timedelta(hours=3)) == at("2026-09-16", 13)


def test_a_deadline_rolls_into_the_next_working_day() -> None:
    """Two hours from 16:00 is one hour today and one tomorrow morning."""
    assert clock().deadline(at("2026-09-16", 16), dt.timedelta(hours=2)) == at("2026-09-17", 10)


def test_a_deadline_from_outside_hours_starts_when_the_office_opens() -> None:
    assert clock().deadline(at("2026-09-16", 5), dt.timedelta(hours=1)) == at("2026-09-16", 10)


def test_a_deadline_skips_the_weekend() -> None:
    """Four hours from Friday 15:00: two on Friday, two on Monday."""
    assert clock().deadline(at("2026-09-18", 15), dt.timedelta(hours=4)) == at("2026-09-21", 11)


def test_a_deadline_that_never_lands_is_nothing_rather_than_a_wrong_answer() -> None:
    """A week with no open days at all. Better no due time than one invented."""
    closed = BusinessClock(
        BusinessHours(timezone="UTC-06:00", start=dt.time(9), end=dt.time(17), days=[0]),
        CHICAGO,
        holidays=frozenset({dt.date(2026, 9, 21) + dt.timedelta(days=7 * n) for n in range(80)}),
    )
    assert closed.deadline(at("2026-09-21", 9), dt.timedelta(hours=4000)) is None


def test_the_deadline_and_the_elapsed_clock_agree() -> None:
    """Whatever the deadline says, exactly the target has run at that moment.
    These are two walks over the same week and they must not drift."""
    walking = clock()
    start = at("2026-09-18", 15, 20)  # a Friday afternoon
    for minutes in (30, 90, 240, 600, 3000):
        due = walking.deadline(start, dt.timedelta(minutes=minutes))
        assert due is not None
        assert walking.elapsed_minutes(start, due) == minutes


# --- the working week ------------------------------------------------------------------


def test_a_saturday_is_not_a_working_day() -> None:
    assert clock().is_working_day(dt.date(2026, 9, 19)) is False
    assert clock().is_working_day(dt.date(2026, 9, 18)) is True


def test_is_open_is_the_window_not_the_day() -> None:
    assert clock().is_open(at("2026-09-16", 12)) is True
    assert clock().is_open(at("2026-09-16", 18)) is False


def test_next_open_skips_to_monday_morning() -> None:
    assert clock().next_open(at("2026-09-19", 11)) == at("2026-09-21", 9)


def test_a_franchisee_with_no_zone_gets_utc_and_is_told_so() -> None:
    zone, name = franchisee_zone(None)
    assert zone is dt.UTC
    assert name == "UTC"


def test_a_zone_this_machine_cannot_load_falls_back_rather_than_failing() -> None:
    zone, name = franchisee_zone("Mars/Olympus_Mons")
    assert zone is dt.UTC
    assert name == "UTC"

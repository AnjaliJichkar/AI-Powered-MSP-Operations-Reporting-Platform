"""The 28-day session history Cove sends on every device row.

`D09F08` is one character per day, **newest first**: character 0 is today,
character 27 is 27 days ago. Each character is a session status digit from
the same vocabulary as `F00`, with `0` for a day that had no session at all.

The direction is not documented and was established against the live account
on 19 October 2026 (docs/api-notes/cove.md): a device whose last successful
backup was five days ago sends `0000055555555555555555555555`, and one that
succeeded today sends a string beginning `5`. Reading it the other way round
would put every window at the wrong end of the month and nothing would
error.

This module reads the string and counts the days. **It does not decide what
is good**: whether a day that completed with errors is a success is a
reporting decision and lives with the report, which has to print the rule
beside the number.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: What one character of the bar means. The digits are `F00`'s, plus `0`.
DAY: Final[Mapping[str, str]] = {
    "0": "none",
    "1": "in_process",
    "2": "failed",
    "3": "aborted",
    "5": "completed",
    "6": "interrupted",
    "7": "not_started",
    "8": "completed_with_errors",
    "9": "in_progress_with_faults",
}

#: A day nobody backed up on. Not a failure: nothing ran.
NONE: Final = "none"

#: What the bar is: four weeks, newest first.
WINDOW_DAYS: Final = 28


@dataclass(frozen=True, slots=True)
class History:
    """One device's recent days, newest first.

    `days[0]` is today. An unreadable character keeps its place as
    `unknown`, so the days either side stay on the dates they belong to —
    dropping it would shift every older day by one.
    """

    days: tuple[str, ...]

    @classmethod
    def read(cls, bar: str | None) -> History:
        return cls(tuple(DAY.get(ch, "unknown") for ch in (bar or "")))

    def recent(self, days: int) -> History:
        """The newest `days` days. Fewer if the bar is shorter."""
        return History(self.days[: max(0, days)])

    def count(self, *states: str) -> int:
        return sum(1 for day in self.days if day in states)

    @property
    def length(self) -> int:
        return len(self.days)

    @property
    def missed(self) -> int:
        """Days with no session at all."""
        return self.count(NONE)

    @property
    def with_session(self) -> int:
        """Days something ran, whatever came of it."""
        return self.length - self.missed

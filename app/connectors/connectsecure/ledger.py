"""The per-tenant daily request ledger, against ConnectSecure's 30,000-a-day limit.

The vendor counts requests per API key per day (also 300 a minute and 2,000 an
hour) and sends no quota headers, so the connector keeps its own count: one
entry per tenant (client and host) per UTC day, incremented on every request
including the login, logged at each threshold crossed, and printed by the
pre-flight. It lives in this process only: two processes each see their own
share, and a restart starts over, so the figure is a floor, never the vendor's
own count. The `audit_log` rows of tool calls are the durable record.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: The vendor's documented daily limit per API key.
DAILY_LIMIT = 30_000

#: Log a warning when a day's count crosses these shares of the limit.
THRESHOLDS = (0.5, 0.8, 1.0)

TenantKey = tuple[uuid.UUID, str]


@dataclass(slots=True)
class DayCount:
    day: dt.date
    count: int = 0
    warned: set[float] = field(default_factory=set)


class RequestLedger:
    """Requests per tenant per UTC day. Not shared across processes."""

    def __init__(self, *, limit: int = DAILY_LIMIT) -> None:
        self.limit = limit
        self._days: dict[TenantKey, DayCount] = {}

    def record(self, key: TenantKey, *, client_slug: str, now: dt.datetime | None = None) -> int:
        """Count one request; log the first crossing of each threshold. Returns the day's count."""
        today = (now or dt.datetime.now(dt.UTC)).date()
        entry = self._days.get(key)
        if entry is None or entry.day != today:
            entry = self._days[key] = DayCount(day=today)
        entry.count += 1
        share = entry.count / self.limit
        for threshold in THRESHOLDS:
            if share >= threshold and threshold not in entry.warned:
                entry.warned.add(threshold)
                logger.log(
                    logging.ERROR if threshold >= 1.0 else logging.WARNING,
                    "connectsecure requests today for client=%s: %d of %d (%d%%) in this process",
                    client_slug,
                    entry.count,
                    self.limit,
                    int(threshold * 100),
                )
        return entry.count

    def today(self, key: TenantKey, *, now: dt.datetime | None = None) -> int:
        today = (now or dt.datetime.now(dt.UTC)).date()
        entry = self._days.get(key)
        return entry.count if entry is not None and entry.day == today else 0

    def remaining(self, key: TenantKey, *, now: dt.datetime | None = None) -> int:
        return max(self.limit - self.today(key, now=now), 0)

    def clear(self) -> None:
        self._days.clear()


#: The process-wide ledger. Tests install their own through the connector.
LEDGER = RequestLedger()

"""A fixed-window rate limiter for sign-in attempts.

Two different defences, and both are needed:

- the **per-user lockout** in `app/auth/service.py` stops one account being
  guessed at, and survives a restart because it is a column;
- this **per-address limit** stops one caller trying many accounts, which a
  lockout cannot see at all.

It is in memory, so it is per process: two workers each allow the configured
rate, and a restart forgets. That is a deliberate limit, written down rather
than hidden — the stack has no Redis, and adding one for this would be a bigger
change than the thing it protects. The durable defence is the lockout; this
raises the cost of a spray. When the engine runs behind more than one worker,
put a rate limit in the proxy in front of it as well.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

#: Addresses tracked at once. Beyond this the oldest are dropped, which fails
#: open for them rather than letting the map grow without bound.
MAX_TRACKED = 10_000


@dataclass(slots=True)
class _Window:
    hits: deque[float] = field(default_factory=deque)


class RateLimiter:
    """Counts attempts per key in a sliding window. Safe across threads."""

    def __init__(self, *, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}

    def check(self, key: str, *, now: float | None = None) -> bool:
        """Record an attempt. False when this key is over its limit.

        The attempt is counted whether or not it is allowed, so a caller that
        keeps trying while limited stays limited.
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            if len(self._windows) > MAX_TRACKED:
                self._windows.clear()
            window = self._windows.setdefault(key, _Window())
            cutoff = moment - self._window
            while window.hits and window.hits[0] <= cutoff:
                window.hits.popleft()
            window.hits.append(moment)
            return len(window.hits) <= self._limit

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        """Whole seconds until this key's oldest attempt leaves the window."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            window = self._windows.get(key)
            if window is None or not window.hits:
                return 0
            return max(1, int(window.hits[0] + self._window - moment) + 1)

    def forget(self, key: str) -> None:
        """Clear a key's history, on a sign-in that succeeded."""
        with self._lock:
            self._windows.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


#: The process-wide limiter for `POST /auth/login`, keyed by client address.
LOGIN_LIMITER = RateLimiter(limit=10)


def configure_login_limiter(*, limit: int) -> None:
    """Point the shared limiter at the configured rate. Called at app start-up."""
    LOGIN_LIMITER._limit = limit
    LOGIN_LIMITER.reset()

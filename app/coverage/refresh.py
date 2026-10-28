"""Refreshing the stored board in the background, one computation per client at a time.

The Coverage page reads the latest snapshot and never computes on load. When
a person wants a newer board they press Refresh, which starts
`take_snapshot` as a background task and returns at once; the page polls
`status` until it is done and reloads. Requests while one is running join
it rather than starting another (single-flight per client), because two
computations of the same tenant would read the same vendors twice for the
same answer.

The registry is per process. The daily collector runs in its own process and
is not coordinated with it; a refresh and the collector on the same day both
replace the day's row, and the later one wins, which is the same board.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.coverage.snapshots import take_snapshot
from app.models.enums import Interface

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RefreshStatus:
    """What the page is told about the client's refresh."""

    running: bool = False
    started_at: dt.datetime | None = None
    started_by: str | None = None
    finished_at: dt.datetime | None = None
    error: str | None = None
    duration_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "started_by": self.started_by,
            "finished_at": self.finished_at,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class RefreshRegistry:
    tasks: dict[uuid.UUID, asyncio.Task[None]] = field(default_factory=dict)
    status: dict[uuid.UUID, RefreshStatus] = field(default_factory=dict)

    def status_of(self, client_id: uuid.UUID) -> RefreshStatus:
        return self.status.setdefault(client_id, RefreshStatus())

    def start(
        self,
        client_id: uuid.UUID,
        *,
        actor: str,
        actor_user_id: uuid.UUID | None,
        interface: Interface = Interface.API,
    ) -> tuple[RefreshStatus, bool]:
        """Start a refresh for the client unless one is running; (status, started)."""
        running = self.tasks.get(client_id)
        if running is not None and not running.done():
            return self.status_of(client_id), False
        status = RefreshStatus(running=True, started_at=dt.datetime.now(dt.UTC), started_by=actor)
        self.status[client_id] = status

        async def run() -> None:
            started = asyncio.get_running_loop().time()
            try:
                await take_snapshot(
                    client_id, actor=actor, interface=interface, actor_user_id=actor_user_id
                )
            except Exception as exc:  # the page reads the error; nothing else does
                status.error = f"{type(exc).__name__}: {exc}"
                logger.exception("coverage refresh failed for client=%s", client_id)
            finally:
                status.running = False
                status.finished_at = dt.datetime.now(dt.UTC)
                status.duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)

        self.tasks[client_id] = asyncio.create_task(run(), name=f"coverage-refresh-{client_id}")
        return status, True

    async def wait(self, client_id: uuid.UUID) -> None:
        """Wait for the client's refresh to finish, if one is running (tests)."""
        task = self.tasks.get(client_id)
        if task is not None:
            await task


REFRESHES = RefreshRegistry()

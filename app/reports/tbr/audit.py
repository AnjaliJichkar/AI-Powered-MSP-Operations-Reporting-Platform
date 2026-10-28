"""Audit rows for a report run. Hard rule 4 has no exception for a report.

Every connector read, every model call and every engineer action on a review is
written to `audit_log` under one `request_id` per run, with interface `system`
and the engineer who asked as the actor. Each row is its own short transaction,
as in the agent loop, so a slow vendor never holds a connection open.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.audit.recorder import AuditContext, record_event
from app.db.session import session_scope
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface


class ReportAudit:
    """Writes this run's rows. Tests replace `write` to record instead of storing."""

    def __init__(
        self,
        *,
        actor_id: str,
        client_id: uuid.UUID,
        client_ref: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        self.context = AuditContext.new(
            actor_id=actor_id,
            interface=Interface.SYSTEM,
            actor_user_id=actor_user_id,
            client_id=client_id,
            client_ref=client_ref,
        )

    async def write(self, event_type: AuditEventType, outcome: AuditOutcome, **fields: Any) -> None:
        async with session_scope() as session:
            await record_event(
                session, self.context, event_type=event_type, outcome=outcome, **fields
            )

    async def read[T](
        self,
        name: str,
        connector: ConnectorName,
        call: Callable[[], Awaitable[T]],
        *,
        summarise: Callable[[T], dict[str, Any]] | None = None,
    ) -> T:
        """Run one connector read and record it, success or failure. Re-raises."""
        started = time.perf_counter()
        try:
            result = await call()
        except Exception as exc:
            await self.write(
                AuditEventType.TOOL_RESULT,
                AuditOutcome.ERROR,
                tool_name=f"tbr.{name}",
                connector_name=connector,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error_type=type(exc).__name__,
                error_message=str(exc)[:500],
            )
            raise
        await self.write(
            AuditEventType.TOOL_RESULT,
            AuditOutcome.SUCCESS,
            tool_name=f"tbr.{name}",
            connector_name=connector,
            duration_ms=int((time.perf_counter() - started) * 1000),
            payload=summarise(result) if summarise else {},
        )
        return result


class NullAudit(ReportAudit):
    """For tests and dry runs that must not touch a database."""

    def __init__(self) -> None:
        self.rows: list[tuple[AuditEventType, AuditOutcome, dict[str, Any]]] = []

    async def write(self, event_type: AuditEventType, outcome: AuditOutcome, **fields: Any) -> None:
        self.rows.append((event_type, outcome, fields))

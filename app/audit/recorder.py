"""Writing rows to ``audit_log``.

Every part of the engine that needs to record something goes through
:func:`record_event`, so redaction happens in exactly one place.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.client import Client, ClientKind
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface

REDACTED = "[redacted]"

#: Payload keys whose values never reach the audit table.
SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "refresh_token",
        "secret",
        "secret_ciphertext",
        "token",
    }
)

MAX_STRING_LENGTH = 8_000


def redact(value: Any) -> Any:
    """Strip secret-looking values and truncate long strings, recursively."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in SECRET_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return REDACTED
    if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
        return value[:MAX_STRING_LENGTH] + "…[truncated]"
    return value


@dataclass(frozen=True, slots=True)
class AuditContext:
    """The who/where/what-run of a request, carried through an agent run."""

    request_id: uuid.UUID
    actor_id: str
    """Who acted, as text. Since authentication this is the user's id; rows
    from before it hold the handle an engineer typed, and are not rewritten."""
    interface: Interface
    actor_user_id: uuid.UUID | None = None
    """The authenticated user, when a person signed in to do this. None for a
    scripted or system run, which is why it is nullable rather than required."""
    client_id: uuid.UUID | None = None
    client_ref: str | None = None
    conversation_id: uuid.UUID | None = None
    """The thread the run belongs to, once one has been created or verified."""

    @classmethod
    def new(
        cls,
        *,
        actor_id: str,
        interface: Interface,
        actor_user_id: uuid.UUID | None = None,
        client_id: uuid.UUID | None = None,
        client_ref: str | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> AuditContext:
        return cls(
            request_id=uuid.uuid4(),
            actor_id=actor_id,
            actor_user_id=actor_user_id,
            interface=interface,
            client_id=client_id,
            client_ref=client_ref,
            conversation_id=conversation_id,
        )


_CLIENT_IS_DEMO: dict[uuid.UUID, bool] = {}


async def is_demo_client(session: AsyncSession, client_id: uuid.UUID | None) -> bool:
    """Whether `client_id` is the demo tenant. A client's kind is set when it is
    created and never changes, so the answer is kept per process; an id with no
    row is not kept, in case the row is written later in the same process."""
    if client_id is None:
        return False
    known = _CLIENT_IS_DEMO.get(client_id)
    if known is not None:
        return known
    kind = (
        await session.execute(select(Client.kind).where(Client.id == client_id))
    ).scalar_one_or_none()
    if kind is None:
        return False
    _CLIENT_IS_DEMO[client_id] = kind == ClientKind.DEMO
    return _CLIENT_IS_DEMO[client_id]


def forget_client_kinds() -> None:
    """Tests create and drop clients; the per-process memory must not outlive them."""
    _CLIENT_IS_DEMO.clear()


async def record_event(
    session: AsyncSession,
    context: AuditContext,
    *,
    event_type: AuditEventType,
    outcome: AuditOutcome,
    connector_name: ConnectorName | None = None,
    tool_name: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    duration_ms: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one row. The caller owns the transaction.

    A row for a demo client is marked `demo` here, whatever wrote it, so no caller
    has to remember: the client's kind is read once per process per client."""
    entry = AuditLog(
        request_id=context.request_id,
        actor_id=context.actor_id,
        actor_user_id=context.actor_user_id,
        interface=context.interface,
        client_id=context.client_id,
        client_ref=context.client_ref,
        demo=await is_demo_client(session, context.client_id),
        conversation_id=context.conversation_id,
        event_type=event_type,
        outcome=outcome,
        connector_name=connector_name,
        tool_name=tool_name,
        model=model,
        prompt_version=prompt_version,
        duration_ms=duration_ms,
        error_type=error_type,
        error_message=error_message,
        payload=redact(payload or {}),
    )
    session.add(entry)
    await session.flush()
    return entry

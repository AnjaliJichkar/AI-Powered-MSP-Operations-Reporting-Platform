"""GET /audit: what the engine did for the client this session is on.

The audit trail is not only ours. A franchisee admin may read their own
client's rows, because it is a record of what was done with their customers'
data and hard rule 4 exists as much for them as for us. The scope is the same
as everything else: the session's client, never one named in a query string.

Two things are narrowed for a franchisee:

- **who** is coarsened. An Inputiv engineer's account is shown as "Inputiv
  staff" rather than as a person: which of us ran a report is our staffing, not
  the customer's business, and the precise actor is in the row for us.
- **what** is trimmed to the shape of the event. `payload` holds prompt text,
  tool arguments and result summaries, which can carry another site's names
  when a read spanned sites, so it is not returned at all.

A viewer sees nothing here: reading the record of every question anyone asked
about the estate is an administrator's job.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import ScopeRule
from app.auth.scope import INPUTIV_ROLES, Scope
from app.db.session import get_session
from app.models.audit_log import AuditLog
from app.models.enums import AuditEventType, AuditOutcome, Interface, Role
from app.models.organisation import User

router = APIRouter(tags=["audit"])

#: Inputiv staff, and a franchisee's own administrator. Not a viewer.
AUDIT_READ = ScopeRule(
    label="audit-read",
    roles=INPUTIV_ROLES | frozenset({Role.FRANCHISEE_ADMIN}),
)

#: Harness runs are excluded from what anybody reads, as they are from usage
#: figures (CLAUDE.md, "Tool selection check").
EXCLUDED_INTERFACES = (Interface.EVAL,)

MAX_ROWS = 200

#: What a franchisee is shown instead of one of our people's account ids.
INPUTIV_ACTOR = "Inputiv staff"


class AuditRowView(BaseModel):
    id: int
    created_at: dt.datetime
    event_type: AuditEventType
    outcome: AuditOutcome
    actor: str
    interface: Interface
    tool_name: str | None
    connector: str | None
    duration_ms: int | None
    error_type: str | None
    payload: dict[str, Any] | None
    """Omitted entirely for a franchisee: it can carry text from a read that
    spanned sites, and trimming it field by field would be a guess."""


@router.get("/audit", response_model=list[AuditRowView])
async def read_audit(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(AUDIT_READ)],
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = 50,
    event_type: AuditEventType | None = None,
) -> list[AuditRowView]:
    """Recent audit rows for the session's client, newest first."""
    query = (
        select(AuditLog)
        .where(AuditLog.client_id == scope.require_client())
        .where(AuditLog.interface.not_in(EXCLUDED_INTERFACES))
        .order_by(AuditLog.id.desc())
        .limit(limit)
    )
    if event_type is not None:
        query = query.where(AuditLog.event_type == event_type)
    rows = (await db.execute(query)).scalars().all()

    actors: dict[str, str] = {}
    if not scope.is_inputiv:
        # One lookup for the actors on this page, to decide which are ours.
        ids = {row.actor_user_id for row in rows if row.actor_user_id is not None}
        if ids:
            staff = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
            actors = {
                str(user.id): (INPUTIV_ACTOR if user.role.is_inputiv else user.email)
                for user in staff
            }

    def actor_of(row: AuditLog) -> str:
        if scope.is_inputiv:
            return row.actor_id
        # An account we did not resolve, or a row from before sign-in existed,
        # is one of ours by definition: no franchisee could have written it.
        return actors.get(row.actor_id, INPUTIV_ACTOR)

    return [
        AuditRowView(
            id=row.id,
            created_at=row.created_at,
            event_type=row.event_type,
            outcome=row.outcome,
            actor=actor_of(row),
            interface=row.interface,
            tool_name=row.tool_name,
            connector=row.connector_name.value if row.connector_name else None,
            duration_ms=row.duration_ms,
            error_type=row.error_type,
            payload=row.payload if scope.is_inputiv else None,
        )
        for row in rows
    ]

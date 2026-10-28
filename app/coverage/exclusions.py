"""Excluding a device from a tool: a decision with a reason, a name and a time.

A kiosk that will never have a vulnerability agent is not a gap; it is a
decision, and the board should say whose. So an exclusion is a row with the
reason the person gave, who they were and when, and it turns the device's
state to `excluded` — shown on the board with the reason, never dropped from
it. Withdrawing one is a second write on the same row, so the history stays.

Every write is an audit row (hard rule 4) naming the device, the tool and the
reason. The reason is required here, in the API model and in the database's
own check constraint: three places, because a device that quietly disappears
from a coverage board is the failure this whole module exists to prevent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.coverage.config import tool_by_key
from app.coverage.core import Exclusion
from app.db.session import session_scope
from app.models.coverage import CoverageExclusion
from app.models.enums import AuditEventType, AuditOutcome


class ExclusionError(ValueError):
    """A request that cannot be recorded: no reason, an unknown tool, a repeat."""


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is excluding: the signed-in user, or an operator handle from a script."""

    actor_id: str
    email: str
    user_id: uuid.UUID
    client_id: uuid.UUID
    client_slug: str | None = None

    def audit(self) -> AuditContext:
        from app.models.enums import Interface

        return AuditContext.new(
            actor_id=self.actor_id,
            actor_user_id=self.user_id,
            interface=Interface.API,
            client_id=self.client_id,
            client_ref=self.client_slug,
        )


def as_record(row: CoverageExclusion) -> Exclusion:
    return Exclusion(
        id=row.id,
        device_id=row.device_id,
        tool=row.tool,
        reason=row.reason,
        by=row.created_by,
        at=row.created_at,
    )


async def list_exclusions(
    session: AsyncSession, client_id: uuid.UUID, *, include_removed: bool = False
) -> list[CoverageExclusion]:
    query = select(CoverageExclusion).where(CoverageExclusion.client_id == client_id)
    if not include_removed:
        query = query.where(CoverageExclusion.removed_at.is_(None))
    return list(
        (await session.execute(query.order_by(CoverageExclusion.created_at.desc()))).scalars().all()
    )


async def live_exclusions(client_id: uuid.UUID) -> list[Exclusion]:
    """The exclusions in force, as the join reads them. Opens its own session."""
    async with session_scope() as session:
        return [as_record(row) for row in await list_exclusions(session, client_id)]


async def exclude(
    session: AsyncSession,
    actor: Actor,
    *,
    site_id: uuid.UUID,
    device_id: str,
    device_name: str,
    tool: str,
    reason: str,
    now: dt.datetime | None = None,
) -> CoverageExclusion:
    """Record that `device_id` is not expected to have `tool`. Caller owns the transaction."""
    reason = " ".join(reason.split())
    if not reason:
        raise ExclusionError(
            "an exclusion needs a reason: say why this device should not have the tool"
        )
    if tool_by_key(tool) is None:
        raise ExclusionError(f"{tool!r} is not a coverage tool")
    if not device_id.strip():
        raise ExclusionError("an exclusion names a device")
    existing = (
        await session.execute(
            select(CoverageExclusion).where(
                CoverageExclusion.client_id == actor.client_id,
                CoverageExclusion.device_id == device_id,
                CoverageExclusion.tool == tool,
                CoverageExclusion.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ExclusionError(
            f"{device_name} is already excluded from {tool} ({existing.created_by}, "
            f"{existing.created_at:%d %b %Y}): withdraw that one first"
        )
    now = now or dt.datetime.now(dt.UTC)
    row = CoverageExclusion(
        client_id=actor.client_id,
        site_id=site_id,
        device_id=device_id,
        device_name=device_name,
        tool=tool,
        reason=reason,
        created_by_user_id=actor.user_id,
        created_by=actor.email,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    await record_event(
        session,
        actor.audit(),
        event_type=AuditEventType.COVERAGE_EXCLUDED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "action": "excluded",
            "exclusion_id": row.id,
            "site_id": str(site_id),
            "device_id": device_id,
            "device": device_name,
            "tool": tool,
            "reason": reason,
        },
    )
    return row


async def withdraw(
    session: AsyncSession,
    actor: Actor,
    *,
    exclusion_id: int,
    now: dt.datetime | None = None,
) -> CoverageExclusion:
    """Withdraw one exclusion. The row stays, with who withdrew it and when."""
    row = (
        await session.execute(
            select(CoverageExclusion).where(
                CoverageExclusion.id == exclusion_id,
                CoverageExclusion.client_id == actor.client_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("no such exclusion for this client")
    if row.removed_at is not None:
        raise ExclusionError("that exclusion was already withdrawn")
    row.removed_at = now or dt.datetime.now(dt.UTC)
    row.removed_by_user_id = actor.user_id
    row.removed_by = actor.email
    await record_event(
        session,
        actor.audit(),
        event_type=AuditEventType.COVERAGE_EXCLUDED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "action": "withdrawn",
            "exclusion_id": row.id,
            "site_id": str(row.site_id),
            "device_id": row.device_id,
            "device": row.device_name,
            "tool": row.tool,
            "reason": row.reason,
        },
    )
    return row

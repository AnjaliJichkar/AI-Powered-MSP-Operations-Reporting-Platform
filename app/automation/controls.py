"""The two controls every run passes through: the switch and the stop button.

* **The switch** is per client and per workflow, and it is off until somebody
  turns it on. The absence of a row is off. A workflow that is off cannot be
  drafted for that client and cannot be approved there; a run that was
  approved before the switch went off still runs, because an approval is a
  decision somebody made and the kill switch is the thing for stopping it.
* **The kill switch** is per client. Thrown, no new run starts and a running
  one waits between steps until it is cleared — the step it is on finishes,
  because half a step is worse than a whole one, and nothing after it begins.

Both are rows rather than settings in a JSON blob, so throwing one is a small
atomic write that races with nothing, and the worker reads two rows between
steps rather than parsing a franchisee's whole configuration.

Every change to either is an `automation_control` audit row naming who.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.models.automation import AutomationKillSwitch, AutomationSwitch
from app.models.enums import AuditEventType, AuditOutcome


@dataclass(frozen=True, slots=True)
class Who:
    """The person throwing a switch, as the row and the audit record them."""

    user_id: uuid.UUID | None
    label: str
    """Their address, or a script's handle."""


async def is_enabled(session: AsyncSession, client_id: uuid.UUID, workflow_name: str) -> bool:
    row = await session.get(AutomationSwitch, (client_id, workflow_name))
    return bool(row is not None and row.enabled)


async def is_halted(session: AsyncSession, client_id: uuid.UUID) -> bool:
    row = await session.get(AutomationKillSwitch, client_id)
    return bool(row is not None and row.halted)


async def switches_for(session: AsyncSession, client_id: uuid.UUID) -> dict[str, AutomationSwitch]:
    rows = (
        (
            await session.execute(
                select(AutomationSwitch).where(AutomationSwitch.client_id == client_id)
            )
        )
        .scalars()
        .all()
    )
    return {row.workflow_name: row for row in rows}


async def kill_switch_for(
    session: AsyncSession, client_id: uuid.UUID
) -> AutomationKillSwitch | None:
    return await session.get(AutomationKillSwitch, client_id)


class NeedsSettings(ValueError):
    """A workflow cannot be turned on for a client until the settings its
    definition requires are set for that client. Names them."""

    def __init__(self, workflow_name: str, missing: list[str]) -> None:
        self.workflow_name = workflow_name
        self.missing = missing
        super().__init__(
            f"{workflow_name} needs settings before it can be turned on: {', '.join(missing)}. "
            f"Set them with manage_client.py report-config --automation-file."
        )


async def missing_settings(
    session: AsyncSession, client_id: uuid.UUID, workflow_name: str
) -> list[str]:
    """The required settings this client has not set for the workflow, as
    `key (type)`, from the workflow's definition; nothing for a workflow
    with none or one not from a definition."""
    from app.automation import library
    from app.automation.config import load_automation_config

    config = await load_automation_config(session, client_id)
    given = config.settings_for(workflow_name)
    return [f"{m.key} ({m.type})" for m in library.missing_settings_for(workflow_name, given)]


async def set_switch(
    session: AsyncSession,
    context: AuditContext,
    *,
    client_id: uuid.UUID,
    workflow_name: str,
    enabled: bool,
    who: Who,
    now: dt.datetime,
) -> AutomationSwitch:
    """Turn one workflow on or off for one client. The caller owns the
    transaction. Turning on is refused (`NeedsSettings`) while a setting the
    definition requires is not set for the client."""
    if enabled:
        missing = await missing_settings(session, client_id, workflow_name)
        if missing:
            raise NeedsSettings(workflow_name, missing)
    row = await session.get(AutomationSwitch, (client_id, workflow_name))
    was = bool(row is not None and row.enabled)
    if row is None:
        row = AutomationSwitch(client_id=client_id, workflow_name=workflow_name)
        session.add(row)
    row.enabled = enabled
    row.changed_by_user_id = who.user_id
    row.changed_by = who.label
    row.changed_at = now
    await session.flush()
    await record_event(
        session,
        context,
        event_type=AuditEventType.AUTOMATION_CONTROL,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "control": "switch",
            "workflow": workflow_name,
            "before": was,
            "after": enabled,
            "by": who.label,
        },
    )
    return row


async def set_kill_switch(
    session: AsyncSession,
    context: AuditContext,
    *,
    client_id: uuid.UUID,
    halted: bool,
    reason: str | None,
    who: Who,
    now: dt.datetime,
) -> AutomationKillSwitch:
    """Throw or clear the client's stop button. The caller owns the transaction."""
    row = await session.get(AutomationKillSwitch, client_id)
    was = bool(row is not None and row.halted)
    if row is None:
        row = AutomationKillSwitch(client_id=client_id)
        session.add(row)
    row.halted = halted
    row.reason = reason if halted else None
    row.changed_by_user_id = who.user_id
    row.changed_by = who.label
    row.changed_at = now
    await session.flush()
    await record_event(
        session,
        context,
        event_type=AuditEventType.AUTOMATION_CONTROL,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "control": "kill_switch",
            "before": was,
            "after": halted,
            "reason": reason if halted else None,
            "by": who.label,
        },
    )
    return row

"""One franchisee's automation settings: who may authorise a run, and what each
workflow is told about this client.

Stored with the franchisee's other settings (`FranchiseeConfig.automation`,
one `report_configs` row per client), for the reason the Resolved and
Autopilot settings are: it is one franchisee's settings, and a second place
to keep them is one too many. Set with `manage_client.py report-config`.

Nothing a workflow needs to know about a client is written in code. The
approver list is here; the lists and exclusions a workflow reads — which
groups a leaver is removed from, which mailboxes are never touched — go under
`workflows[<name>]`, validated by that workflow's own settings model when it
is drafted.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import ReportConfig


class Approver(BaseModel):
    """A person at the client who may say yes to a run."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="As they are known at the client.")
    role: str | None = Field(
        default=None, description="Their role there, for the record: 'IT manager'."
    )
    email: str | None = Field(
        default=None,
        description="Where an approval link will go, once there is such a thing. "
        "Nothing sends to it yet.",
    )

    @field_validator("name")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("an approver needs a name")
        return value


class NotesConfig(BaseModel):
    """Who at the client may act on a Desk note (`automation.notes`)."""

    model_config = ConfigDict(extra="forbid")

    franchisee_can_post: bool = Field(
        default=False,
        description="When true, the franchisee's administrators and technicians see the "
        "Desk note on a ticket and may post it to Autotask or mark it not useful, beside "
        "Inputiv staff. Off, the note is Inputiv staff's alone and franchisee users see "
        "no Desk note card at all.",
    )


class AutomationConfig(BaseModel):
    """What this franchisee has decided about automation."""

    model_config = ConfigDict(extra="forbid")

    notes: NotesConfig = Field(
        default_factory=NotesConfig,
        description="Who may act on a Desk note: Inputiv staff always; the franchisee's "
        "own engineers only when `franchisee_can_post` is on.",
    )
    approvers: list[Approver] = Field(
        default_factory=list,
        description="Who at the client may authorise a run. Empty means nobody can, "
        "and no run can be approved.",
    )
    workflows: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-workflow settings, keyed by workflow name: the lists and "
        "exclusions that workflow reads for this client.",
    )

    def approver(self, name: str) -> Approver | None:
        """The approver of that name, matched case-insensitively on the
        collapsed-whitespace name, or None."""
        wanted = " ".join(name.split()).casefold()
        return next((a for a in self.approvers if a.name.casefold() == wanted), None)

    def settings_for(self, workflow_name: str) -> dict[str, Any]:
        return dict(self.workflows.get(workflow_name, {}))


async def load_automation_config(session: AsyncSession, client_id: uuid.UUID) -> AutomationConfig:
    """This client's automation settings, or the empty default.

    Read straight off the `report_configs` row rather than through
    `app.reports.tbr.config.load_config`, on purpose: that module composes the
    autopilot settings too, and importing it would put `app.autopilot` on this
    package's import graph. The no-AI guard would be right to object.
    """
    row = await session.get(ReportConfig, client_id)
    if row is None or not row.config:
        return AutomationConfig()
    return AutomationConfig.model_validate(row.config.get("automation") or {})

"""The automation engine's own tables: runs, their steps, the switches, and the
practice records the built-in workflow acts on.

A run is the record of a workflow being applied to one client: which workflow
and which version of it, what it was given, who started it, who authorised it
and who approved it, and where it got to. A step is one action of that run,
with what the world looked like before it and after it, so the ticket note,
the audit and any undo are exact rather than reconstructed.

Three things about these tables are deliberate and worth saying once:

* **A run's foreign keys are RESTRICT.** A run that changed something at a
  client is a record of that change. Deleting the client, or the person who
  approved it, must be refused rather than quietly take the record with it —
  the same rule every judgement table here follows (`suggestion_reviews`,
  `labelling_answers`).
* **State is a column with a check constraint, and the transitions are not
  here.** The database knows the eight words; `app/automation/states.py` knows
  which may follow which, in one place, and the engine refuses anything else.
  A row cannot be in a state that does not exist, and the order they may be
  moved through is code that is tested.
* **The approval record is shaped for channels that do not exist yet.** An
  emailed or Teams approval link will carry a reference and a payload of its
  own; `authoriser_method` is free text and `authoriser_ref` and
  `approval_payload` are nullable columns waiting for it, so adding a channel
  is a new value for `authoriser_method`, not a migration.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

#: The eight states a run may be in. Which may follow which is
#: `app.automation.states.TRANSITIONS`; this is only the vocabulary the column
#: accepts.
RUN_STATES = (
    "drafted",
    "dry_run_ready",
    "awaiting_approval",
    "approved",
    "running",
    "completed",
    "failed",
    "partly_completed",
    "cancelled",
    "undone",
)

#: What a step may be in. `done` means its verify check passed; a step is
#: never "done" because its action returned. `changed` is a done step an undo
#: found changed since the run and left alone.
STEP_STATES = ("pending", "running", "done", "failed", "undone", "undo_failed", "changed")

#: What a trigger evaluation came to: a run drafted, a note (or report) built,
#: a match skipped with a reason, or no match.
TRIGGER_OUTCOMES = ("drafted", "noted", "skipped", "no_match")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class AutomationRun(Base):
    """One workflow applied to one client, from draft to whatever it ended as."""

    __tablename__ = "automation_runs"
    __table_args__ = (
        CheckConstraint(_in("state", RUN_STATES), name="ck_automation_runs_state"),
        CheckConstraint("workflow_version >= 1", name="ck_automation_runs_version"),
        # One run at a time per client: a second cannot enter `running` while
        # one is there. Enforced by the database rather than by the worker
        # remembering, because two workers would each remember their own.
        Index(
            "uq_automation_runs_one_running_per_client",
            "client_id",
            unique=True,
            postgresql_where=text("state = 'running'"),
        ),
        Index("ix_automation_runs_client_created", "client_id", "created_at"),
        Index("ix_automation_runs_state", "state"),
        # A run was started by a person or drafted by a trigger, never neither.
        CheckConstraint(
            "started_by_user_id IS NOT NULL OR trigger_key IS NOT NULL",
            name="ck_automation_runs_starter",
        ),
        # One draft per ticket per workflow from a trigger: a ticket that
        # matches on two passes, or in two pollers, gets one run.
        Index(
            "uq_automation_runs_one_per_ticket_trigger",
            "client_id",
            "workflow_name",
            "ticket_number",
            unique=True,
            postgresql_where=text("trigger_key IS NOT NULL AND ticket_number IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    workflow_name: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    """The version of the definition this run was drafted against, recorded so a
    run read later is read against the steps it actually had."""
    definition_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The SHA-256 of the canonical form of the definition the workflow was
    compiled from when this run was drafted (`library.canonical`), so a run
    can always be read against what it actually ran. Of the parsed
    definition rather than the file's bytes, so it is the same on a
    developer's machine and in production whatever the checkout did to the
    line endings. Null for a run drafted before workflows were
    definitions."""
    state: Mapped[str] = mapped_column(Text, nullable=False, default="drafted")

    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    """What the workflow was given, after validation against its input schema."""
    ticket_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The ticket this run is about, where there is one: named by the engineer
    who drafted it, or the ticket a rule matched."""

    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    """The engineer who drafted it, or null for a run a trigger drafted."""
    started_by: Mapped[str] = mapped_column(Text, nullable=False)
    """The engineer's address as it was, kept beside the id so the record reads
    without a join and survives a changed address; for a triggered run, the
    trigger's kind in words ("ticket rule")."""
    trigger_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The workflow trigger that drafted it, or null for a person's draft."""
    trigger_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The trigger as a reader sees it: "ticket rule on T20261012.0004"."""

    # --- approval -------------------------------------------------------------
    authoriser_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The client's person who said yes: a name on the client's approver list."""
    authoriser_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    """How they said it. Free text on purpose — `phone`, `email`, `teams`,
    `in_person` today; an approval link later adds a value, not a column."""
    authoriser_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    authoriser_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Where a channel has one — a message id, a link token — the reference
    that proves it. Null for a verbal approval somebody recorded."""
    approval_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """Whatever a later channel needs to keep beside the approval. Nothing
    writes it yet."""
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    approval_expires_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """Approval plus the workflow's approval window. A run not started by then
    goes back to awaiting approval; a failed run not resumed by then needs a
    fresh approval."""

    # --- the dry run ----------------------------------------------------------
    dry_run: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """The checklist the dry run produced: per step, what it would change and
    whether it can be undone. Kept on the run because it is what was approved."""
    dry_run_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    # --- execution ------------------------------------------------------------
    current_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The step the run is on, or stopped at."""
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why it failed, in words, when it did."""
    paused_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """Set while the client's kill switch holds a running run between steps.
    The state stays `running`: the run has not ended, it is waiting."""
    lease_until: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """The worker holding a running run renews this as it goes. A run whose
    lease has lapsed was left by a worker that died, and the next worker takes
    it up from the step it was on."""
    undo_deadline_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """Until when the run may be undone: finished plus the workflow's window."""
    undo_requested_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """An undo is asked for from a page and done by the worker, like a run, so
    the request is a column the worker reads rather than work a web request
    does. Set with who asked; cleared when the undo has run, either way."""
    undo_requested_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    undo_failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), onupdate=dt.datetime.now, nullable=False
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    undone_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    steps: Mapped[list[AutomationStep]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AutomationStep.position",
    )


class AutomationStep(Base):
    """One step of one run: what it saw, what it did, what it found afterwards."""

    __tablename__ = "automation_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "step_key", name="uq_automation_steps_run_step"),
        CheckConstraint(_in("state", STEP_STATES), name="ck_automation_steps_state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    """The step's own words for what it does, copied from the definition so the
    row reads on its own."""
    depends_on: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The step that must be `done` before this one may run."""
    reversible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")

    precondition: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """What the precondition check found, the last time it ran."""
    planned: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """What the dry run said this step would change."""
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """What the verify check found after the action."""
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """The step's inputs as its references resolved, the last time it was
    called: what the action was actually handed."""
    outputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """What the action returned, which later steps' references read."""

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    undone_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    run: Mapped[AutomationRun] = relationship(back_populates="steps")


class AutomationSwitch(Base):
    """Whether one workflow may run for one client. Off until somebody says
    otherwise, and the absence of a row is off."""

    __tablename__ = "automation_switches"

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True
    )
    workflow_name: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    changed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), onupdate=dt.datetime.now, nullable=False
    )


class AutomationKillSwitch(Base):
    """The client's stop button. Thrown, no new run starts and a running one
    waits between steps until it is cleared. The absence of a row is clear."""

    __tablename__ = "automation_kill_switches"

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True
    )
    halted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    changed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), onupdate=dt.datetime.now, nullable=False
    )


class AutomationTriggerSwitch(Base):
    """Whether one trigger of one workflow may draft runs for one client. Off
    until somebody says otherwise, and separate from the workflow's own
    switch: both must be on before a trigger drafts anything."""

    __tablename__ = "automation_trigger_switches"

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True
    )
    workflow_name: Mapped[str] = mapped_column(Text, primary_key=True)
    trigger_key: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    changed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), onupdate=dt.datetime.now, nullable=False
    )


class AutomationTriggerLog(Base):
    """One evaluation of one trigger against one ticket (or one tick of a
    schedule): whether it matched, what it came to, and why. The Automations
    page shows a client's last hundred, so "why did that ticket not start a
    run" is a row somebody can read rather than a guess."""

    __tablename__ = "automation_trigger_log"
    __table_args__ = (
        CheckConstraint(_in("outcome", TRIGGER_OUTCOMES), name="ck_automation_trigger_log_outcome"),
        Index("ix_automation_trigger_log_client_time", "client_id", "evaluated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    workflow_name: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    """Which condition failed, or why a match drafted nothing, in words."""
    rule: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """The conditions as evaluated, after the client's overrides."""
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="SET NULL"), nullable=True
    )
    evaluated_at: Mapped[dt.datetime] = mapped_column(nullable=False)


class PracticeRecord(Base):
    """What the practice workflow acts on: a small table of fake records owned
    by the engine, so the controls can be proven on a run that touches nothing
    real. The demo tenant's only; nothing live ever holds one."""

    __tablename__ = "automation_practice_records"
    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_automation_practice_records_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)


# --- notes: the first real workflows -------------------------------------------------

#: The modes a note workflow may be in for a client. `preview` writes the note
#: to our table and shows it on the ticket page; nothing reaches Autotask until
#: an engineer posts it. `auto` posts as soon as the note is built, and needs
#: the franchisee's approver recorded to switch into.
NOTE_MODES = ("preview", "auto")

#: What a note is in. `built` is waiting on the ticket page; `posted` reached
#: Autotask; `not_useful` was dismissed by an engineer; `post_failed` was
#: asked to post and the writer did not succeed.
NOTE_STATES = ("built", "posted", "not_useful", "post_failed")


class AutomationNote(Base):
    """One Desk note for one ticket from one workflow: the fixed template filled
    with the facts read from the tools, each with the time it was read.

    At most one per workflow per ticket, kept by a unique constraint, so a
    ticket seen on two polls gets one note. The row is the record whether or
    not the note was ever posted: what was built, when, from what, who posted
    it or marked it not useful, and when the ticket closed."""

    __tablename__ = "automation_notes"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "workflow_name", "ticket_id", name="uq_automation_notes_one_per_ticket"
        ),
        CheckConstraint(_in("mode", NOTE_MODES), name="ck_automation_notes_mode"),
        CheckConstraint(_in("state", NOTE_STATES), name="ck_automation_notes_state"),
        Index("ix_automation_notes_client_built", "client_id", "built_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    workflow_name: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The SHA-256 of the canonical form of the definition the note was built
    from (`library.canonical`)."""
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The vendor's id, which is what a note is posted against."""
    ticket_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_created_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    site_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The machine the note is about, by the name the resolution gave it."""

    mode: Mapped[str] = mapped_column(Text, nullable=False)
    """The workflow's mode for this client when the note was built."""
    state: Mapped[str] = mapped_column(Text, nullable=False, default="built")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    """Each fact the template was filled from: id, label, value, source, read_at."""
    read_failures: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    """The reads that did not answer, each named in the note as one line."""

    built_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    posted_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    posted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    posted_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The engineer's address, or `auto` when the mode posted it."""
    autotask_note_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    not_useful_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    not_useful_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    not_useful_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_closed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """Stamped by the poll once Autotask reports the ticket complete."""


class AutomationNoteMode(Base):
    """A note workflow's rollout mode for one client, and who agreed to it.

    Absent is `preview`. Switching to `auto` records the franchisee's
    authoriser the way a run approval does — name, method, time — because it
    is the one approval that covers every note the workflow then posts;
    switching back records nobody and takes effect at once."""

    __tablename__ = "automation_note_modes"
    __table_args__ = (
        CheckConstraint(_in("mode", NOTE_MODES), name="ck_automation_note_modes_mode"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True
    )
    workflow_name: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="preview")
    authoriser_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    authoriser_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    authoriser_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    changed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), onupdate=dt.datetime.now, nullable=False
    )


class AutomationAlertTicket(Base):
    """Every alert ticket a note workflow's rule matched, with when it closed,
    so time-to-close can be compared for tickets with and without a note.
    One row per ticket per client; `note_id` is set when a note was built."""

    __tablename__ = "automation_alert_tickets"
    __table_args__ = (
        Index("ix_automation_alert_tickets_client_created", "client_id", "created_at"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True
    )
    ticket_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ticket_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    checked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    note_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("automation_notes.id", ondelete="SET NULL"), nullable=True
    )


class AutomationReport(Base):
    """A report a scheduled workflow built — the chronic devices report — kept
    whole, as the coverage board is: the page reads the latest row and says
    when it was built."""

    __tablename__ = "automation_reports"
    __table_args__ = (Index("ix_automation_reports_client_built", "client_id", "built_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    workflow_name: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[dt.datetime] = mapped_column(nullable=False)
    period_end: Mapped[dt.datetime] = mapped_column(nullable=False)
    built_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

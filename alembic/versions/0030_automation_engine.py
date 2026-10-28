"""The automation engine's tables: runs, steps, switches, practice records.

Revision ID: 0030
Revises: 0029

No workflow writes to a client's tools yet, and this migration does not change
that: nothing here reaches a vendor. What it adds is the record every future
workflow will leave — which workflow, at which version, given what, started by
whom, authorised by whom and approved by whom, and for every step what the
world looked like before and after — together with the two controls that have
to exist before any workflow does: a per-client, per-workflow switch that is
off until thrown, and a per-client stop button.

The practice table is the engine's own: a handful of fake records the built-in
"practice" workflow creates, changes and deletes, so that the switch, the dry
run, the approval and the audit are all proven on a run that touches nothing
real.

Keys on `automation_runs` are RESTRICT: a run is a record of something done
at a client, and neither the client nor the approving account may be deleted
out from under it. The one-running-per-client rule is a partial unique index,
so two workers cannot each start one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_CONSTRAINT = "ck_audit_log_audit_event_type"
BEFORE = (
    "run_started",
    "prompt",
    "model_response",
    "tool_call",
    "tool_result",
    "run_finished",
    "error",
    "web_search",
    "conversation_started",
    "conversation_archived",
    "context_trimmed",
    "report_generated",
    "report_reviewed",
    "report_rendered",
    "report_config_changed",
    "events_collected",
    "login",
    "logout",
    "client_switched",
    "access_denied",
    "user_invited",
    "invite_accepted",
    "user_changed",
    "coverage_excluded",
    "coverage_snapshot",
    "devices_resolved",
    "device_linked",
    "ticket_device_chosen",
    "ticket_plan_generated",
    "ticket_plan_reviewed",
    "ticket_outcome_recorded",
    "autopilot_run_started",
    "autopilot_run_finished",
    "autopilot_run_failed",
    "autopilot_labelled",
    "autopilot_suggestion_reviewed",
)
#: Three types, the action in the payload: one for the run's own transitions,
#: one for every check, action, verification and undo of a step, one for the
#: switches. Three rather than a dozen so the audit table's constraint grows
#: by what is new in kind, not by every verb.
ADDED = ("automation_run", "automation_step", "automation_control")
AFTER = (*BEFORE, *ADDED)

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
STEP_STATES = ("pending", "running", "done", "failed", "undone", "undo_failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "automation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ticket_number", sa.Text(), nullable=True),
        sa.Column("started_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_by", sa.Text(), nullable=False),
        sa.Column("authoriser_name", sa.Text(), nullable=True),
        sa.Column("authoriser_method", sa.Text(), nullable=True),
        sa.Column("authoriser_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("authoriser_ref", sa.Text(), nullable=True),
        sa.Column("approval_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dry_run", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("dry_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("current_step", sa.Text(), nullable=True),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("undo_deadline_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("undo_requested_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("undo_requested_by", sa.Text(), nullable=True),
        sa.Column("undo_failure", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("undone_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(_in("state", RUN_STATES), name=op.f("ck_automation_runs_state")),
        sa.CheckConstraint("workflow_version >= 1", name=op.f("ck_automation_runs_version")),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_automation_runs_client_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["started_by_user_id"],
            ["users.id"],
            name=op.f("fk_automation_runs_started_by_user_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name=op.f("fk_automation_runs_approved_by_user_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automation_runs")),
    )
    op.create_index(
        "uq_automation_runs_one_running_per_client",
        "automation_runs",
        ["client_id"],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index(
        "ix_automation_runs_client_created",
        "automation_runs",
        ["client_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_automation_runs_state", "automation_runs", ["state"], unique=False)

    op.create_table(
        "automation_steps",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("depends_on", sa.Text(), nullable=True),
        sa.Column("reversible", sa.Boolean(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("precondition", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("planned", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("undone_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(_in("state", STEP_STATES), name=op.f("ck_automation_steps_state")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["automation_runs.id"],
            name=op.f("fk_automation_steps_run_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automation_steps")),
        sa.UniqueConstraint("run_id", "step_key", name=op.f("uq_automation_steps_run_step")),
    )

    op.create_table(
        "automation_switches",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("changed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_automation_switches_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["users.id"],
            name=op.f("fk_automation_switches_changed_by_user_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("client_id", "workflow_name", name=op.f("pk_automation_switches")),
    )

    op.create_table(
        "automation_kill_switches",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("halted", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_automation_kill_switches_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["users.id"],
            name=op.f("fk_automation_kill_switches_changed_by_user_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_automation_kill_switches")),
    )

    op.create_table(
        "automation_practice_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_automation_practice_records_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_run_id"],
            ["automation_runs.id"],
            name=op.f("fk_automation_practice_records_created_by_run_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automation_practice_records")),
        sa.UniqueConstraint("client_id", "name", name=op.f("uq_automation_practice_records_name")),
    )

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", AFTER))


def downgrade() -> None:
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", BEFORE))
    op.drop_table("automation_practice_records")
    op.drop_table("automation_kill_switches")
    op.drop_table("automation_switches")
    op.drop_table("automation_steps")
    op.drop_index("ix_automation_runs_state", table_name="automation_runs")
    op.drop_index("ix_automation_runs_client_created", table_name="automation_runs")
    op.drop_index("uq_automation_runs_one_running_per_client", table_name="automation_runs")
    op.drop_table("automation_runs")

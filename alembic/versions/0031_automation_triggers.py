"""Automation triggers, approval expiry, and the step state an undo leaves alone.

Revision ID: 0031
Revises: 0030

A run may now be drafted by a trigger rather than a person: `started_by_user_id`
becomes nullable and `trigger_key` says which trigger, with a check that one
of the two is set. One draft per ticket per workflow from a trigger is a
partial unique index, so two pollers cannot each draft one.

Approvals expire: `approval_expires_at` is approval plus the workflow's
window, read by the worker before it starts a run.

An undo that finds a step's after-state changed since the run leaves it alone
and marks the step `changed`, a new word in the step-state constraint.

Two tables: the per-client, per-trigger switch (off until thrown, separate
from the workflow's switch), and the trigger log — one row per evaluation of
a trigger against a ticket, matched or not, with the reason.

Nothing here reaches a vendor; no workflow writes to a client's tools.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STEP_STATES_BEFORE = ("pending", "running", "done", "failed", "undone", "undo_failed")
STEP_STATES_AFTER = (*STEP_STATES_BEFORE, "changed")
TRIGGER_OUTCOMES = ("drafted", "skipped", "no_match")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    # --- runs: who or what drafted it, and how long the approval stands -------
    op.alter_column("automation_runs", "started_by_user_id", nullable=True)
    op.add_column("automation_runs", sa.Column("trigger_key", sa.Text(), nullable=True))
    op.add_column("automation_runs", sa.Column("trigger_detail", sa.Text(), nullable=True))
    op.add_column(
        "automation_runs",
        sa.Column("approval_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_automation_runs_starter"),
        "automation_runs",
        "started_by_user_id IS NOT NULL OR trigger_key IS NOT NULL",
    )
    op.create_index(
        "uq_automation_runs_one_per_ticket_trigger",
        "automation_runs",
        ["client_id", "workflow_name", "ticket_number"],
        unique=True,
        postgresql_where=sa.text("trigger_key IS NOT NULL AND ticket_number IS NOT NULL"),
    )

    # --- steps: the state an undo leaves a changed step in ---------------------
    op.drop_constraint(op.f("ck_automation_steps_state"), "automation_steps", type_="check")
    op.create_check_constraint(
        op.f("ck_automation_steps_state"), "automation_steps", _in("state", STEP_STATES_AFTER)
    )

    # --- the trigger switch ------------------------------------------------------
    op.create_table(
        "automation_trigger_switches",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column("trigger_key", sa.Text(), nullable=False),
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
            name=op.f("fk_automation_trigger_switches_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["users.id"],
            name=op.f("fk_automation_trigger_switches_changed_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "client_id", "workflow_name", "trigger_key", name=op.f("pk_automation_trigger_switches")
        ),
    )

    # --- the trigger log ----------------------------------------------------------
    op.create_table(
        "automation_trigger_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column("trigger_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("ticket_id", sa.Text(), nullable=True),
        sa.Column("ticket_number", sa.Text(), nullable=True),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("evaluated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            _in("outcome", TRIGGER_OUTCOMES), name=op.f("ck_automation_trigger_log_outcome")
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_automation_trigger_log_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["automation_runs.id"],
            name=op.f("fk_automation_trigger_log_run_id_automation_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automation_trigger_log")),
    )
    op.create_index(
        "ix_automation_trigger_log_client_time",
        "automation_trigger_log",
        ["client_id", "evaluated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_automation_trigger_log_client_time", table_name="automation_trigger_log")
    op.drop_table("automation_trigger_log")
    op.drop_table("automation_trigger_switches")
    op.execute("UPDATE automation_steps SET state = 'done' WHERE state = 'changed'")
    op.drop_constraint(op.f("ck_automation_steps_state"), "automation_steps", type_="check")
    op.create_check_constraint(
        op.f("ck_automation_steps_state"), "automation_steps", _in("state", STEP_STATES_BEFORE)
    )
    op.drop_index("uq_automation_runs_one_per_ticket_trigger", table_name="automation_runs")
    op.drop_constraint(op.f("ck_automation_runs_starter"), "automation_runs", type_="check")
    op.execute(
        "DELETE FROM automation_steps WHERE run_id IN (SELECT id FROM automation_runs WHERE started_by_user_id IS NULL)"
    )
    op.execute("DELETE FROM automation_runs WHERE started_by_user_id IS NULL")
    op.drop_column("automation_runs", "approval_expires_at")
    op.drop_column("automation_runs", "trigger_detail")
    op.drop_column("automation_runs", "trigger_key")
    op.alter_column("automation_runs", "started_by_user_id", nullable=False)

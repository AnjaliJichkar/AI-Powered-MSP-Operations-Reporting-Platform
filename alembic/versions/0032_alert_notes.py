"""Alert notes: the first real workflows' tables, a note writer's credential, an event type.

Revision ID: 0032
Revises: 0031

The automation engine gains its first write: one internal note added to an
Autotask ticket, built from facts read from the tools. Four tables record it
— the notes themselves (one per workflow per ticket, kept by a unique
constraint), each workflow's rollout mode per client with the franchisee's
approver who agreed to auto-posting, every alert ticket a rule matched with
when it closed (so time-to-close can be compared with and without a note),
and the reports a scheduled workflow builds (the chronic devices report).

`autotask_notes` joins the connector names: a second Autotask API user, held
per client in `tool_credentials` like every other credential, that the note
writer alone uses. Every table that lists the connector names gets the new
one; the two device tables never had `microsoft365`, and keep not having it.

`automation_note` joins the audit event types.

Nothing here writes to a vendor; the writer's one request is made by code
that reads these tables, and no workflow is switched on for any client by a
migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BASE_CONNECTORS = ("ninjaone", "autotask", "bitdefender", "cove", "connectsecure", "blackpoint")
NEW_CONNECTOR = "autotask_notes"
#: Which tables listed `microsoft365` before (0001, 0009) and which did not
#: (0014), as (before, after). The new name goes where the enum has it — before
#: `microsoft365` — so the constraint reads as the model's does.
CONNECTOR_TABLES = {
    "tool_mappings": (
        (*BASE_CONNECTORS, "microsoft365"),
        (*BASE_CONNECTORS, NEW_CONNECTOR, "microsoft365"),
    ),
    "tool_credentials": (
        (*BASE_CONNECTORS, "microsoft365"),
        (*BASE_CONNECTORS, NEW_CONNECTOR, "microsoft365"),
    ),
    "audit_log": (
        (*BASE_CONNECTORS, "microsoft365"),
        (*BASE_CONNECTORS, NEW_CONNECTOR, "microsoft365"),
    ),
    "collected_events": (
        (*BASE_CONNECTORS, "microsoft365"),
        (*BASE_CONNECTORS, NEW_CONNECTOR, "microsoft365"),
    ),
    "collection_coverage": (
        (*BASE_CONNECTORS, "microsoft365"),
        (*BASE_CONNECTORS, NEW_CONNECTOR, "microsoft365"),
    ),
    "device_vendor_links": (BASE_CONNECTORS, (*BASE_CONNECTORS, NEW_CONNECTOR)),
    "device_unresolved": (BASE_CONNECTORS, (*BASE_CONNECTORS, NEW_CONNECTOR)),
}

EVENT_CONSTRAINT = "ck_audit_log_audit_event_type"
EVENTS_BEFORE = (
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
    "automation_run",
    "automation_step",
    "automation_control",
)
EVENTS_AFTER = (*EVENTS_BEFORE, "automation_note")

NOTE_MODES = ("preview", "auto")
TRIGGER_OUTCOMES_BEFORE = ("drafted", "skipped", "no_match")
TRIGGER_OUTCOMES_AFTER = ("drafted", "noted", "skipped", "no_match")
NOTE_STATES = ("built", "posted", "not_useful", "post_failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    # --- the connector names, on every table that lists them -----------------
    for table, (_, after) in CONNECTOR_TABLES.items():
        name = op.f(f"ck_{table}_connector_name")
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _in("connector_name", after))

    # --- the event type ------------------------------------------------------
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", EVENTS_AFTER))

    # --- the trigger log learns the word for a note built ------------------------
    op.drop_constraint(
        op.f("ck_automation_trigger_log_outcome"), "automation_trigger_log", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_automation_trigger_log_outcome"),
        "automation_trigger_log",
        _in("outcome", TRIGGER_OUTCOMES_AFTER),
    )

    # --- the notes -----------------------------------------------------------
    op.create_table(
        "automation_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("ticket_number", sa.Text(), nullable=True),
        sa.Column("ticket_created_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("site_slug", sa.Text(), nullable=True),
        sa.Column("device_name", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("read_failures", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("built_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("posted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("posted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("posted_by", sa.Text(), nullable=True),
        sa.Column("autotask_note_id", sa.Text(), nullable=True),
        sa.Column("post_error", sa.Text(), nullable=True),
        sa.Column("not_useful_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("not_useful_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("not_useful_by", sa.Text(), nullable=True),
        sa.Column("ticket_closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(_in("mode", NOTE_MODES), name=op.f("ck_automation_notes_mode")),
        sa.CheckConstraint(_in("state", NOTE_STATES), name=op.f("ck_automation_notes_state")),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_automation_notes_client_id_clients"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["posted_by_user_id"],
            ["users.id"],
            name=op.f("fk_automation_notes_posted_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["not_useful_by_user_id"],
            ["users.id"],
            name=op.f("fk_automation_notes_not_useful_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automation_notes")),
        sa.UniqueConstraint(
            "client_id",
            "workflow_name",
            "ticket_id",
            name=op.f("uq_automation_notes_one_per_ticket"),
        ),
    )
    op.create_index(
        "ix_automation_notes_client_built", "automation_notes", ["client_id", "built_at"]
    )

    # --- the mode per client per workflow --------------------------------------
    op.create_table(
        "automation_note_modes",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("authoriser_name", sa.Text(), nullable=True),
        sa.Column("authoriser_method", sa.Text(), nullable=True),
        sa.Column("authoriser_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("changed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_in("mode", NOTE_MODES), name=op.f("ck_automation_note_modes_mode")),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_automation_note_modes_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["users.id"],
            name=op.f("fk_automation_note_modes_changed_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "client_id", "workflow_name", name=op.f("pk_automation_note_modes")
        ),
    )

    # --- every alert ticket a rule matched, and when it closed -------------------
    op.create_table(
        "automation_alert_tickets",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("ticket_number", sa.Text(), nullable=True),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("note_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_automation_alert_tickets_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["note_id"],
            ["automation_notes.id"],
            name=op.f("fk_automation_alert_tickets_note_id_automation_notes"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("client_id", "ticket_id", name=op.f("pk_automation_alert_tickets")),
    )
    op.create_index(
        "ix_automation_alert_tickets_client_created",
        "automation_alert_tickets",
        ["client_id", "created_at"],
    )

    # --- reports ---------------------------------------------------------------
    op.create_table(
        "automation_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_name", sa.Text(), nullable=False),
        sa.Column("period_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("period_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("built_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_automation_reports_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automation_reports")),
    )
    op.create_index(
        "ix_automation_reports_client_built", "automation_reports", ["client_id", "built_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_automation_reports_client_built", table_name="automation_reports")
    op.drop_table("automation_reports")
    op.drop_index(
        "ix_automation_alert_tickets_client_created", table_name="automation_alert_tickets"
    )
    op.drop_table("automation_alert_tickets")
    op.drop_table("automation_note_modes")
    op.drop_index("ix_automation_notes_client_built", table_name="automation_notes")
    op.drop_table("automation_notes")
    op.execute("DELETE FROM automation_trigger_log WHERE outcome = 'noted'")
    op.drop_constraint(
        op.f("ck_automation_trigger_log_outcome"), "automation_trigger_log", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_automation_trigger_log_outcome"),
        "automation_trigger_log",
        _in("outcome", TRIGGER_OUTCOMES_BEFORE),
    )
    op.execute("DELETE FROM audit_log WHERE event_type = 'automation_note'")
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(
        op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", EVENTS_BEFORE)
    )
    for table, (before, _) in CONNECTOR_TABLES.items():
        op.execute(f"DELETE FROM {table} WHERE connector_name = '{NEW_CONNECTOR}'")
        name = op.f(f"ck_{table}_connector_name")
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _in("connector_name", before))

"""The ticket workspace: workspaces, versioned plans; four audit event types.

Revision ID: 0016
Revises: 0015

Autotask keeps the ticket; these tables keep what the engine and an engineer
decided about it: the device, the enrichment as of a time, the plans and
their review, and the outcome once the ticket closed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
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
)
ADDED = (
    "ticket_device_chosen",
    "ticket_plan_generated",
    "ticket_plan_reviewed",
    "ticket_outcome_recorded",
)
AFTER = (*BEFORE, *ADDED)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "ticket_workspaces",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("ticket_number", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("priority", sa.Text(), nullable=True),
        sa.Column("assigned_to", sa.Text(), nullable=True),
        sa.Column("ticket_created_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("scrubbed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status_checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("device_method", sa.Text(), nullable=True),
        sa.Column("device_confidence", sa.Text(), nullable=True),
        sa.Column(
            "device_candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("device_chosen_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("device_chosen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("device_note", sa.Text(), nullable=True),
        sa.Column(
            "enrichment",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("enriched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("opened_by_user_id", sa.Uuid(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_ticket_workspaces_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["sites.id"],
            name=op.f("fk_ticket_workspaces_site_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_ticket_workspaces_device_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["device_chosen_by_user_id"],
            ["users.id"],
            name=op.f("fk_ticket_workspaces_device_chosen_by_user_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["opened_by_user_id"],
            ["users.id"],
            name=op.f("fk_ticket_workspaces_opened_by_user_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_workspaces")),
        sa.UniqueConstraint("client_id", "ticket_id", name="uq_ticket_workspaces_ticket"),
    )
    op.create_index(
        "ix_ticket_workspaces_client_open", "ticket_workspaces", ["client_id", "closed_at"]
    )

    op.create_table(
        "ticket_plans",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=True),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("generation_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column(
            "steps",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("dropped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "problems",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_ticket_plans_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["ticket_workspaces.id"],
            name=op.f("fk_ticket_plans_workspace_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name=op.f("fk_ticket_plans_author_user_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_ticket_plans_device_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_plans")),
        sa.UniqueConstraint("workspace_id", "version", name="uq_ticket_plans_version"),
    )
    op.create_index("ix_ticket_plans_client", "ticket_plans", ["client_id"])

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE " + _in("event_type", ADDED))
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", BEFORE))
    op.drop_index("ix_ticket_plans_client", table_name="ticket_plans")
    op.drop_table("ticket_plans")
    op.drop_index("ix_ticket_workspaces_client_open", table_name="ticket_workspaces")
    op.drop_table("ticket_workspaces")

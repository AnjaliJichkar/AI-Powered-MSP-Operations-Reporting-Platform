"""Coverage exclusions and daily snapshots; two audit event types.

Revision ID: 0012
Revises: 0011

`coverage_exclusions`: a person's decision that a managed device is not
expected to have a tool, with a reason the database requires, who and when;
withdrawn by a second write, never deleted. `coverage_snapshots`: the board
once a day per client, one row per day, replaced on a rerun.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
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
)
ADDED = ("coverage_excluded", "coverage_snapshot")
AFTER = (*BEFORE, *ADDED)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "coverage_exclusions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("device_name", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("removed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("removed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("removed_by", sa.Text(), nullable=True),
        sa.CheckConstraint("length(btrim(reason)) > 0", name=op.f("ck_coverage_exclusions_reason")),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_coverage_exclusions_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["sites.id"],
            name=op.f("fk_coverage_exclusions_site_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_coverage_exclusions_created_by_user_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["removed_by_user_id"],
            ["users.id"],
            name=op.f("fk_coverage_exclusions_removed_by_user_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coverage_exclusions")),
    )
    op.create_index(
        "ix_coverage_exclusions_client_live", "coverage_exclusions", ["client_id", "removed_at"]
    )

    op.create_table(
        "coverage_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("taken_on", sa.Date(), nullable=False),
        sa.Column("taken_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("taken_by", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "gaps",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_coverage_snapshots_client_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coverage_snapshots")),
        sa.UniqueConstraint("client_id", "taken_on", name="uq_coverage_snapshots_client_day"),
    )

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE " + _in("event_type", ADDED))
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", BEFORE))
    op.drop_table("coverage_snapshots")
    op.drop_index("ix_coverage_exclusions_client_live", table_name="coverage_exclusions")
    op.drop_table("coverage_exclusions")

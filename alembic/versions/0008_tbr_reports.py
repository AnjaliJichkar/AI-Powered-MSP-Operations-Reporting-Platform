"""Business reviews: franchisee report settings and the stored reviews.

Revision ID: 0008
Revises: 0007

`report_configs` is one row per client: display name, logo, service catalog,
price book and default review period, as JSON (`app/reports/tbr/config.py`).

`tbr_reports` is one row per generated review. `document` is the whole review:
facts with definitions, findings, drafted and engineer-edited sections,
approvals. The previous period's row is the snapshot a later review measures
change against. Rows are superseded, never deleted.

`audit_log.event_type` gains `report_generated`, `report_reviewed` and
`report_rendered`, by the same constraint swap 0005 and 0006 used.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
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
)
AFTER = (*BEFORE, "report_generated", "report_reviewed", "report_rendered")


def _condition(values: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column(
            name,
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        )
        for name in ("created_at", "updated_at")
    ]


def upgrade() -> None:
    op.create_table(
        "report_configs",
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_report_configs_client_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_report_configs")),
    )

    op.create_table(
        "tbr_reports",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("period_key", sa.Text(), nullable=False),
        sa.Column("period_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("period_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("rendered_path", sa.Text(), nullable=True),
        sa.Column("rendered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_tbr_reports_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_tbr_reports_site_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tbr_reports")),
    )
    op.create_index(
        "ix_tbr_reports_site_period",
        "tbr_reports",
        ["site_id", "period_key", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_tbr_reports_client",
        "tbr_reports",
        ["client_id", sa.text("created_at DESC")],
        unique=False,
    )

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _condition(AFTER))


def downgrade() -> None:
    # As in 0005: the audit trail is not rewritten to make a downgrade fit.
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _condition(BEFORE))
    op.drop_index("ix_tbr_reports_client", table_name="tbr_reports")
    op.drop_index("ix_tbr_reports_site_period", table_name="tbr_reports")
    op.drop_table("tbr_reports")
    op.drop_table("report_configs")

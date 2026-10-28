"""The daily collector's tables: collected events and collection coverage.

Revision ID: 0009
Revises: 0008

`collected_events` holds one row per vendor event, unique on
`(connector_name, kind, external_id)` so a collection run is idempotent.
`collection_coverage` records each window a run asked a vendor for and how it
ended, per site, connector and kind.

`audit_log.event_type` gains `events_collected`, by the constraint swap the
earlier revisions used.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
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
)
AFTER = (*BEFORE, "events_collected")


CONNECTOR_NAMES = (
    "ninjaone",
    "autotask",
    "bitdefender",
    "cove",
    "connectsecure",
    "blackpoint",
    "microsoft365",
)


def _enum(values: tuple[str, ...], name: str, length: int = 32) -> sa.Enum:
    """varchar + check constraint, matching ``app.models.types``."""
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=length)


def _condition(values: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "collected_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("connector_name", _enum(CONNECTOR_NAMES, "connector_name"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("vendor_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "first_collected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_collected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_collected_events_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_collected_events_site_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collected_events")),
        sa.UniqueConstraint(
            "connector_name", "kind", "external_id", name="uq_collected_events_vendor_event"
        ),
    )
    op.create_index(
        "ix_collected_events_site_kind_time",
        "collected_events",
        ["site_id", "kind", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_collected_events_client_time",
        "collected_events",
        ["client_id", sa.text("occurred_at DESC")],
    )

    op.create_table(
        "collection_coverage",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("connector_name", _enum(CONNECTOR_NAMES, "connector_name"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("events_seen", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("events_new", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "collected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("collected_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_collection_coverage_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["sites.id"],
            name=op.f("fk_collection_coverage_site_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_coverage")),
        sa.CheckConstraint(
            "status IN ('collected', 'failed')", name=op.f("ck_collection_coverage_status")
        ),
    )
    op.create_index(
        "ix_collection_coverage_site_kind",
        "collection_coverage",
        ["site_id", "connector_name", "kind", sa.text("window_end DESC")],
    )

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _condition(AFTER))


def downgrade() -> None:
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _condition(BEFORE))
    op.drop_index("ix_collection_coverage_site_kind", table_name="collection_coverage")
    op.drop_table("collection_coverage")
    op.drop_index("ix_collected_events_client_time", table_name="collected_events")
    op.drop_index("ix_collected_events_site_kind_time", table_name="collected_events")
    op.drop_table("collected_events")

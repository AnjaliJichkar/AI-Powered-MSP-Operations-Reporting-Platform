"""audit_log feedback: thumbs up/down on an answer.

Revision ID: 0002
Revises: 0001

The record of what happened stays append-only; these columns annotate an answer
with an engineer's verdict on it afterwards. The partial index serves the weekly
prompt review, which only ever looks at rows that were actually rated.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEEDBACK_RATINGS = ("up", "down")


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column(
            "feedback_rating",
            sa.Enum(
                *FEEDBACK_RATINGS,
                name="feedback_rating",
                native_enum=False,
                create_constraint=True,
                length=8,
            ),
            nullable=True,
        ),
    )
    op.add_column("audit_log", sa.Column("feedback_comment", sa.Text(), nullable=True))
    op.add_column("audit_log", sa.Column("feedback_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("audit_log", sa.Column("feedback_by", sa.Text(), nullable=True))
    op.create_index(
        "ix_audit_log_feedback_rating_created_at",
        "audit_log",
        ["feedback_rating", "created_at"],
        postgresql_where=sa.text("feedback_rating IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_feedback_rating_created_at", table_name="audit_log")
    op.drop_column("audit_log", "feedback_by")
    op.drop_column("audit_log", "feedback_at")
    op.drop_column("audit_log", "feedback_comment")
    op.drop_column("audit_log", "feedback_rating")

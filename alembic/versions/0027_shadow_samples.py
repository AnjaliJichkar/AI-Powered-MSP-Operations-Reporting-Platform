"""The fixed set of tickets iteration runs against.

Revision ID: 0027
Revises: 0026

A full replay of the pilot's ninety days is 2,722 tickets and about five
million tokens. Almost none of that buys an answer to the question somebody
asks while iterating — did that change help — which a few hundred tickets drawn
properly answer as well.

So the sample is drawn once and **stored**, rather than recomputed from a seed
per run: the ledger gains rows, a seed over a changed population selects a
different set, and two runs would stop being comparable without anybody having
touched anything. `shadow_sample_tickets` is a list of ids and which period
each came from — no title, no issue type, nothing the ledger already holds.

It is not a measurement. The number that goes in front of anybody still comes
from a full replay on the holdout; every run records which sample it used so
the two are never confused.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shadow_samples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("holdout_days", sa.Integer(), nullable=False),
        sa.Column("drawn_from", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_shadow_samples_client_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_samples")),
        sa.UniqueConstraint("client_id", "name", name="uq_shadow_samples_name"),
    )
    op.create_index(
        "ix_shadow_samples_client", "shadow_samples", ["client_id", "created_at"], unique=False
    )
    op.create_table(
        "shadow_sample_tickets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("sample_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "period IN ('tuning', 'test')",
            name=op.f("ck_shadow_sample_tickets_period_is_tuning_or_test"),
        ),
        sa.ForeignKeyConstraint(
            ["sample_id"],
            ["shadow_samples.id"],
            name=op.f("fk_shadow_sample_tickets_sample_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_sample_tickets")),
        sa.UniqueConstraint("sample_id", "ticket_id", name="uq_shadow_sample_tickets_ticket"),
    )
    op.create_index(
        "ix_shadow_sample_tickets_sample",
        "shadow_sample_tickets",
        ["sample_id", "period"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_shadow_sample_tickets_sample", table_name="shadow_sample_tickets")
    op.drop_table("shadow_sample_tickets")
    op.drop_index("ix_shadow_samples_client", table_name="shadow_samples")
    op.drop_table("shadow_samples")

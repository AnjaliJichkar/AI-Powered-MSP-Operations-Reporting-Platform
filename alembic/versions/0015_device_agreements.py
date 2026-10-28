"""The daily device agreement: the board both ways, per-site counts compared, kept.

Revision ID: 0015
Revises: 0014

One row per run of the daily collector's agreement step. The Devices page
shows Inputiv staff the latest, with the differing sites when there are any.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_agreements",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("agree", sa.Boolean(), nullable=False),
        sa.Column("sites_compared", sa.Integer(), nullable=False),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "differences",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_device_agreements_client_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_agreements")),
    )
    op.create_index(
        "ix_device_agreements_client_at", "device_agreements", ["client_id", "computed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_device_agreements_client_at", table_name="device_agreements")
    op.drop_table("device_agreements")

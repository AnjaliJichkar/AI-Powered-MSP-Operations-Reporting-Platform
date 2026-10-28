"""A replay's requests, sent as a batch and waited for.

Revision ID: 0029
Revises: 0028

A replay is thousands of independent requests nobody is waiting on, which is
what the Message Batches API is for: about half the per-token price, answered
within a day. The wait is the thing that needs a table. A batch id that lived
only in the terminal somebody ran the replay in is money spent with nothing to
show for it, so the row is written at submission and the run is carried on by
running the same command again.

`status` holds the vendor's own word for where the batch is, plus `collected`,
which is ours: it has ended and its answers are written down. A collected batch
is never waited on again.

Behind `AUTOPILOT_BATCH_REPLAY`, and never on a live path — the forward shadow,
the backfill and the ticket card are answering about a ticket an engineer is
looking at now, and hours is not a latency, it is a different product.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shadow_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["shadow_runs.id"],
            name=op.f("fk_shadow_batches_run_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_batches")),
        sa.UniqueConstraint("external_id", name="uq_shadow_batches_external"),
    )
    op.create_index(
        "ix_shadow_batches_run", "shadow_batches", ["run_id", "submitted_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_shadow_batches_run", table_name="shadow_batches")
    op.drop_table("shadow_batches")

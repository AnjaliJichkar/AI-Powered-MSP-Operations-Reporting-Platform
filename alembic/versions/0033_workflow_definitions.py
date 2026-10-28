"""Workflows as data: what a run and a note ran, and what each step was handed.

Revision ID: 0033
Revises: 0032

Workflows are now definitions (`app/automation/definitions/*.yaml`) assembled
from the action library. Three things are recorded that the code could not
record before: the SHA-256 of the definition a run was drafted from and a
note was built from (`definition_hash`, beside the version), and per step the
inputs its references resolved to and the outputs the action returned, which
later steps read and the run page shows. All nullable: a row from before this
has none.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automation_runs", sa.Column("definition_hash", sa.Text(), nullable=True))
    op.add_column("automation_notes", sa.Column("definition_hash", sa.Text(), nullable=True))
    op.add_column(
        "automation_steps",
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "automation_steps",
        sa.Column("outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_steps", "outputs")
    op.drop_column("automation_steps", "inputs")
    op.drop_column("automation_notes", "definition_hash")
    op.drop_column("automation_runs", "definition_hash")

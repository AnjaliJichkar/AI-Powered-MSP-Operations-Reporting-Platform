"""An answer the model has already given, keyed by the question.

Revision ID: 0028
Revises: 0027

Most of what a replay spends is spent asking the same question twice. A policy
chosen on the development period, a threshold moved, a sample redrawn, the
alert-queue variant — none of them change what the model is shown for most
tickets, and all of them used to cost a full set of calls.

`input_hash` is the digest of exactly what was sent: the model, the
instructions, the tenant's label set, the tool schema and the ticket's own
line. A later run that would send an identical request for the same ticket,
under the same model and prompt version, reads the stored answer instead and
records `reused_from_run_id` so where it came from is on the row rather than
in somebody's memory. Nothing is matched loosely: a cheaper reuse that answers
a different question is not a saving, it is a wrong number.

`shadow_runs.reused` is how many of a run's answers came that way, beside
`by_model`, which stays what it always was — how many tickets the model path
answered, whoever did the asking.

Old rows carry no digest and are never reused, which is the right way round:
they were made before anybody was recording what the question was.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("shadow_predictions", sa.Column("input_hash", sa.Text(), nullable=True))
    op.add_column("shadow_predictions", sa.Column("reused_from_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_shadow_predictions_reused_from_run_id"),
        "shadow_predictions",
        "shadow_runs",
        ["reused_from_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_shadow_predictions_reuse",
        "shadow_predictions",
        ["ticket_id", "input_hash"],
        unique=False,
    )
    # The default fills the runs that already exist — none of them reused
    # anything, because there was nothing to reuse from — and is then dropped,
    # so the column the model declares and the column the database has are the
    # same column.
    op.add_column(
        "shadow_runs",
        sa.Column("reused", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.alter_column("shadow_runs", "reused", server_default=None)


def downgrade() -> None:
    op.drop_column("shadow_runs", "reused")
    op.drop_index("ix_shadow_predictions_reuse", table_name="shadow_predictions")
    op.drop_constraint(
        op.f("fk_shadow_predictions_reused_from_run_id"), "shadow_predictions", type_="foreignkey"
    )
    op.drop_column("shadow_predictions", "reused_from_run_id")
    op.drop_column("shadow_predictions", "input_hash")

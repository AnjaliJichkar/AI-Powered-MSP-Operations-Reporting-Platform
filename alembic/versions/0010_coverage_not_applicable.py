"""collection_coverage.status gains `not_applicable`.

Revision ID: 0010
Revises: 0009

A site whose licence cannot produce the kind of event asked for (a company
without EDR asked for detections) is recorded as `not_applicable`, never as
`collected` with zero events: a report must not read it as a quiet period.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_collection_coverage_status"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "collection_coverage", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT),
        "collection_coverage",
        "status IN ('collected', 'failed', 'not_applicable')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM collection_coverage WHERE status = 'not_applicable'")
    op.drop_constraint(op.f(CONSTRAINT), "collection_coverage", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT), "collection_coverage", "status IN ('collected', 'failed')"
    )

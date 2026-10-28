"""The board is stored, and a vendor's last-seen lives on the link.

Revision ID: 0018
Revises: 0017

`coverage_snapshots.board` keeps the whole board a snapshot was made from,
so the Coverage page reads the latest row rather than computing on every
load; rows from before this carry NULL and the page asks for a refresh.

`device_vendor_links.vendor_last_seen_at` is the date a vendor last heard
from the record (GravityZone `lastSeen`, one detail call per row), and
`vendor_checked_at` when we last asked, so the readers ask again only when
the stored value is older than `BITDEFENDER_LAST_SEEN_REFRESH_HOURS`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "coverage_snapshots",
        sa.Column("board", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "device_vendor_links",
        sa.Column("vendor_last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "device_vendor_links",
        sa.Column("vendor_checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("device_vendor_links", "vendor_checked_at")
    op.drop_column("device_vendor_links", "vendor_last_seen_at")
    op.drop_column("coverage_snapshots", "board")

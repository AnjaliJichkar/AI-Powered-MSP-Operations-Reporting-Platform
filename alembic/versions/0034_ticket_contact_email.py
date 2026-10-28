"""The ticket contact's address, so a Microsoft 365 account can be matched to it.

Revision ID: 0034
Revises: 0033

The ticket page matches its contact to a Microsoft 365 user **by email**: a
name matches several people in a directory and an address matches one. Autotask
gives the address with the contact record the workspace already reads, so it is
kept beside the contact's name rather than re-read on every refresh of the
panel. Nullable: a workspace opened before this has none, and a ticket whose
contact has no address in Autotask never gets one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ticket_workspaces", sa.Column("contact_email", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ticket_workspaces", "contact_email")

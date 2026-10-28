"""The demo tenant: `clients.kind` and `audit_log.demo`.

Revision ID: 0020
Revises: 0019

A client is `live` (a franchisee we hold credentials for) or `demo` (the
fictional franchisee `app/demo/` generates, served only by the fake
connectors). Every audit row written for a demo client is marked, so demo
traffic is told apart from a franchisee's in every figure read from the
table. Existing rows are live and not demo.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("kind", sa.String(length=8), nullable=False, server_default=sa.text("'live'")),
    )
    op.create_check_constraint(op.f("ck_clients_kind_known"), "clients", "kind IN ('live', 'demo')")
    op.add_column(
        "audit_log",
        sa.Column("demo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("audit_log", "demo")
    op.drop_constraint(op.f("ck_clients_kind_known"), "clients", type_="check")
    op.drop_column("clients", "kind")

"""The outcome ledger: one row per completed ticket.

Revision ID: 0022
Revises: 0021

Resolved is sold per outcome, so an outcome has to be a row: what it was, who
closed it, how long it took against the franchisee's own targets, what it cost,
and whether it stayed closed. `ticket_outcomes` is that row, written by the
daily collector and keyed `(client_id, ticket_id)` so a rerun changes nothing.

Everything in it is **derived** — it can be rebuilt from Autotask and from our
own workspaces at any time — which is why the client foreign key cascades where
`audit_log`'s restricts: losing a franchisee's measurements loses no record of
anything that happened, only the arithmetic over it.

Two nullable columns carry meaning and are not "missing": `first_response_met`
and `resolution_met` are null where the clock was paused or the priority had no
target, so a percentage is taken over the tickets that were judged rather than
over every ticket. `cost` is null where the franchisee has no cost book, because
a figure from a guessed hourly rate is worse than no figure.

Down again drops the table. Nothing else points at it, and it holds no decision
a person made — the ledger is a reading of Autotask, not a record of intent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticket_outcomes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("ticket_number", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("ticket_class", sa.Text(), nullable=False),
        sa.Column("resolver", sa.Text(), nullable=False),
        sa.Column("resolver_name", sa.Text(), nullable=True),
        sa.Column("priority_label", sa.Text(), nullable=True),
        sa.Column("status_label", sa.Text(), nullable=True),
        sa.Column("queue_label", sa.Text(), nullable=True),
        sa.Column("issue_type_label", sa.Text(), nullable=True),
        sa.Column("created_at_vendor", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("first_response_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("pickup_minutes", sa.Integer(), nullable=True),
        sa.Column("resolve_minutes", sa.Integer(), nullable=True),
        sa.Column("first_response_met", sa.Boolean(), nullable=True),
        sa.Column("resolution_met", sa.Boolean(), nullable=True),
        sa.Column("reopened", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reopened_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("hours_logged", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("cost", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("plan_version", sa.Integer(), nullable=True),
        sa.Column("plan_outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("collected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "resolver IN ('inputiv', 'franchisee', 'unknown')",
            name=op.f("ck_ticket_outcomes_resolver_is_known_side"),
        ),
        sa.CheckConstraint(
            "ticket_class IN ('L1', 'L2', 'L3')",
            name=op.f("ck_ticket_outcomes_class_is_l1_l2_or_l3"),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_ticket_outcomes_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_ticket_outcomes_site_id"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["ticket_workspaces.id"],
            name=op.f("fk_ticket_outcomes_workspace_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_outcomes")),
        # The upsert the collector uses depends on this, and so does the promise
        # that running the collector twice changes nothing.
        sa.UniqueConstraint("client_id", "ticket_id", name="uq_ticket_outcomes_ticket"),
    )
    # The two questions asked of this table: what closed in a date range, and
    # what closed for one end customer.
    op.create_index(
        "ix_ticket_outcomes_completed", "ticket_outcomes", ["client_id", "completed_at"]
    )
    op.create_index("ix_ticket_outcomes_site", "ticket_outcomes", ["client_id", "site_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_outcomes_site", table_name="ticket_outcomes")
    op.drop_index("ix_ticket_outcomes_completed", table_name="ticket_outcomes")
    op.drop_table("ticket_outcomes")

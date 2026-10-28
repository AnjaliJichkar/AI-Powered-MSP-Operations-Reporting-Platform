"""Conversation threads.

Revision ID: 0006
Revises: 0005

`conversations` and `conversation_messages` hold what is replayed into the model
so an engineer does not have to restate a device or ticket every turn.

`audit_log` gains a nullable `conversation_id`. Existing rows stay null. The
foreign key is ON DELETE RESTRICT: threads are archived rather than deleted, and
nothing done to a thread may remove or rewrite an audit row.

Three audit step types are added — `conversation_started`,
`conversation_archived`, `context_trimmed` — by the same drop-and-re-add of the
`event_type` check constraint that 0005 used.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_CONSTRAINT = "ck_audit_log_audit_event_type"

BEFORE = (
    "run_started",
    "prompt",
    "model_response",
    "tool_call",
    "tool_result",
    "run_finished",
    "error",
    "web_search",
)
AFTER = (*BEFORE, "conversation_started", "conversation_archived", "context_trimmed")


def _condition(values: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("engineer_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_conversations_client_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "ix_conversations_engineer_client_updated",
        "conversations",
        ["engineer_id", "client_id", sa.text("updated_at DESC")],
        unique=False,
    )

    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "user",
                "assistant",
                name="message_role",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("content_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_messages_conversation_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_messages")),
        sa.UniqueConstraint(
            "conversation_id", "seq", name=op.f("uq_conversation_messages_conversation_id_seq")
        ),
    )

    op.add_column("audit_log", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_audit_log_conversation_id"),
        "audit_log",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_audit_log_conversation_id", "audit_log", ["conversation_id"], unique=False)

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _condition(AFTER))


def downgrade() -> None:
    # As in 0005: the audit trail is not rewritten to make a downgrade fit. If
    # any of the three new step types has been recorded, restoring the old
    # constraint fails loudly rather than deleting or relabelling audit rows.
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _condition(BEFORE))

    op.drop_index("ix_audit_log_conversation_id", table_name="audit_log")
    op.drop_constraint(op.f("fk_audit_log_conversation_id"), "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "conversation_id")

    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_engineer_client_updated", table_name="conversations")
    op.drop_table("conversations")

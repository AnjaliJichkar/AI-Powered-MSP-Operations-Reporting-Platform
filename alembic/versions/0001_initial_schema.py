"""Initial schema: clients, tool_mappings, tool_credentials, audit_log.

Revision ID: 0001
Revises:
Create Date: 2026-09-07

Requires PostgreSQL 13 or newer, where ``gen_random_uuid()`` is in core.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONNECTOR_NAMES = (
    "ninjaone",
    "autotask",
    "bitdefender",
    "cove",
    "connectsecure",
    "blackpoint",
    "microsoft365",
)
AUDIT_EVENT_TYPES = (
    "run_started",
    "prompt",
    "model_response",
    "tool_call",
    "tool_result",
    "run_finished",
    "error",
)
AUDIT_OUTCOMES = ("success", "error", "denied", "timeout", "capped", "in_progress")
INTERFACES = ("slack", "api", "cli", "system")

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"


def _enum(values: tuple[str, ...], name: str, length: int = 32) -> sa.Enum:
    """varchar + check constraint, matching ``app.models.types``."""
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
    )


def _timestamp(name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_clients"),
        # Bare name: op.create_table applies the metadata naming convention,
        # which expands this to ck_clients_slug_format.
        sa.CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        sa.UniqueConstraint("slug", name="uq_clients_slug"),
    )

    op.create_table(
        "tool_mappings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("connector_name", _enum(CONNECTOR_NAMES, "connector_name"), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("external_name", sa.Text(), nullable=True),
        sa.Column(
            "extra",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_tool_mappings"),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name="fk_tool_mappings_client_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "client_id", "connector_name", name="uq_tool_mappings_client_id_connector_name"
        ),
        # A vendor tenant belongs to exactly one client: the database half of
        # the per-client scoping rule.
        sa.UniqueConstraint(
            "connector_name", "external_id", name="uq_tool_mappings_connector_name_external_id"
        ),
    )

    op.create_table(
        "tool_credentials",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("connector_name", _enum(CONNECTOR_NAMES, "connector_name"), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("encryption_key_id", sa.Text(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_tool_credentials"),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name="fk_tool_credentials_client_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "client_id", "connector_name", name="uq_tool_credentials_client_id_connector_name"
        ),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        _timestamp("created_at"),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", _enum(AUDIT_EVENT_TYPES, "audit_event_type"), nullable=False),
        sa.Column("outcome", _enum(AUDIT_OUTCOMES, "audit_outcome"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("interface", _enum(INTERFACES, "interface", length=16), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("client_ref", sa.Text(), nullable=True),
        sa.Column("connector_name", _enum(CONNECTOR_NAMES, "connector_name"), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        # RESTRICT, not CASCADE: the audit trail outlives the client record.
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name="fk_audit_log_client_id",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_audit_log_request_id_id", "audit_log", ["request_id", "id"])
    op.create_index("ix_audit_log_client_id_created_at", "audit_log", ["client_id", "created_at"])
    op.create_index("ix_audit_log_actor_id_created_at", "audit_log", ["actor_id", "created_at"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_id_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_client_id_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_request_id_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("tool_credentials")
    op.drop_table("tool_mappings")
    op.drop_table("clients")

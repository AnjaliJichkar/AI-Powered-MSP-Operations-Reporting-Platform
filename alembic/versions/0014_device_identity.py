"""The device identity layer: devices, vendor links, the unresolved queue; two audit types.

Revision ID: 0014
Revises: 0013

Behind DEVICE_IDENTITY_ENABLED (default off): the tables exist and the daily
resolution may fill them, and nothing reads them until the flag is on.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
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
    "conversation_started",
    "conversation_archived",
    "context_trimmed",
    "report_generated",
    "report_reviewed",
    "report_rendered",
    "events_collected",
    "login",
    "logout",
    "client_switched",
    "access_denied",
    "user_invited",
    "invite_accepted",
    "user_changed",
    "coverage_excluded",
    "coverage_snapshot",
)
ADDED = ("devices_resolved", "device_linked")
AFTER = (*BEFORE, *ADDED)

CONNECTOR_NAMES = ("ninjaone", "autotask", "bitdefender", "cove", "connectsecure", "blackpoint")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _connector() -> sa.Enum:
    """varchar + check constraint, matching ``app.models.types``."""
    return sa.Enum(
        *CONNECTOR_NAMES,
        name="connector_name",
        native_enum=False,
        create_constraint=True,
        length=32,
    )


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("netbios_key", sa.Text(), nullable=False),
        sa.Column(
            "serials",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "macs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("node_class", sa.Text(), nullable=True),
        sa.Column("os", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
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
            "status IN ('active', 'rmm_stale', 'retired')", name=op.f("ck_devices_status")
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_devices_client_id"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_devices_site_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
    )
    op.create_index("ix_devices_client_site", "devices", ["client_id", "site_id"])
    op.create_index("ix_devices_client_status", "devices", ["client_id", "status"])
    op.create_index("ix_devices_client_netbios", "devices", ["client_id", "netbios_key"])

    op.create_table(
        "device_vendor_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("connector_name", _connector(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("vendor_name", sa.Text(), nullable=True),
        sa.Column("matched_by", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_device_vendor_links_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_device_vendor_links_device_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_vendor_links")),
        sa.UniqueConstraint(
            "client_id", "connector_name", "external_id", name="uq_device_vendor_links_record"
        ),
    )
    op.create_index("ix_device_vendor_links_device", "device_vendor_links", ["device_id"])

    op.create_table(
        "device_unresolved",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("connector_name", _connector(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("vendor_name", sa.Text(), nullable=True),
        sa.Column("found_by", sa.Text(), nullable=False),
        sa.Column(
            "candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_device_unresolved_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["sites.id"],
            name=op.f("fk_device_unresolved_site_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_unresolved")),
        sa.UniqueConstraint(
            "client_id", "connector_name", "external_id", name="uq_device_unresolved_record"
        ),
    )
    op.create_index(
        "ix_device_unresolved_client_open", "device_unresolved", ["client_id", "resolved_at"]
    )

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE " + _in("event_type", ADDED))
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", BEFORE))
    op.drop_index("ix_device_unresolved_client_open", table_name="device_unresolved")
    op.drop_table("device_unresolved")
    op.drop_index("ix_device_vendor_links_device", table_name="device_vendor_links")
    op.drop_table("device_vendor_links")
    for name in ("ix_devices_client_netbios", "ix_devices_client_status", "ix_devices_client_site"):
        op.drop_index(name, table_name="devices")
    op.drop_table("devices")

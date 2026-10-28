"""Sites, and per-site tool mappings.

Revision ID: 0003
Revises: 0002

Tenancy becomes two levels. `clients` stays the franchisee and keeps owning the
credentials; `sites` is new and holds that franchisee's own end customers, which
are what a vendor calls an organisation or a company.

`tool_mappings` gains a nullable `site_id`, which is what tells the two kinds of
row apart:

- `site_id` set — the vendor id for one end customer. This is what scopes a
  data read.
- `site_id` NULL — tenant-level, for the franchisee as a whole. At most one per
  `(client_id, connector_name)`, enforced by a partial unique index because the
  site-level rows deliberately repeat that pair.

**Existing rows become tenant-level**, since they predate sites and have a NULL
`site_id`. They stay valid and keep their vendor id, but they no longer scope
anything, so a client configured before this migration will have no sites and
its tools will say so until `scripts/sync_sites.py` has been run for it. That is
deliberate: inventing a site from an old tenant-level mapping would be guessing
at which end customer it referred to.

The old `uq_tool_mappings_client_id_connector_name` is dropped because a client
now has many mappings per connector, one per site.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name=op.f("ck_sites_slug_format")),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name=op.f("fk_sites_client_id"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sites")),
        # Slugs are what an engineer and the model name a site by, so they need
        # only be unique within the franchisee that owns them.
        sa.UniqueConstraint("client_id", "slug", name=op.f("uq_sites_client_id_slug")),
    )
    op.create_index(
        "ix_sites_client_id_is_active", "sites", ["client_id", "is_active"], unique=False
    )

    op.add_column("tool_mappings", sa.Column("site_id", sa.Uuid(), nullable=True))
    op.drop_constraint(
        op.f("uq_tool_mappings_client_id_connector_name"), "tool_mappings", type_="unique"
    )
    op.create_index(
        "ix_tool_mappings_client_id_connector_name",
        "tool_mappings",
        ["client_id", "connector_name"],
        unique=False,
    )
    op.create_unique_constraint(
        op.f("uq_tool_mappings_site_id_connector_name"),
        "tool_mappings",
        ["site_id", "connector_name"],
    )
    op.create_index(
        "uq_tool_mappings_tenant_level",
        "tool_mappings",
        ["client_id", "connector_name"],
        unique=True,
        postgresql_where=sa.text("site_id IS NULL"),
    )
    op.create_foreign_key(
        op.f("fk_tool_mappings_site_id"),
        "tool_mappings",
        "sites",
        ["site_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # Site-level mappings have no home in the old shape, and more than one would
    # violate the unique constraint being restored. Dropping them is the only
    # coherent reversal: re-running sync_sites rebuilds them.
    op.execute(sa.text("DELETE FROM tool_mappings WHERE site_id IS NOT NULL"))

    op.drop_constraint(op.f("fk_tool_mappings_site_id"), "tool_mappings", type_="foreignkey")
    op.drop_index(
        "uq_tool_mappings_tenant_level",
        table_name="tool_mappings",
        postgresql_where=sa.text("site_id IS NULL"),
    )
    op.drop_constraint(
        op.f("uq_tool_mappings_site_id_connector_name"), "tool_mappings", type_="unique"
    )
    op.drop_index("ix_tool_mappings_client_id_connector_name", table_name="tool_mappings")
    op.create_unique_constraint(
        op.f("uq_tool_mappings_client_id_connector_name"),
        "tool_mappings",
        ["client_id", "connector_name"],
    )
    op.drop_column("tool_mappings", "site_id")
    op.drop_index("ix_sites_client_id_is_active", table_name="sites")
    op.drop_table("sites")

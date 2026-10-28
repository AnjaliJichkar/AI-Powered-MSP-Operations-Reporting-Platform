"""Site matching across connectors; tenant-level mappings carry no vendor id.

Revision ID: 0004
Revises: 0003

`sites.normalized_name` is the key `sync_sites` matches on when a second
connector reports a customer the first one already created. It is derived from
`name` (see `app.models.site.normalise_name`) and backfilled here for any site
that already exists.

`tool_mappings.external_id` becomes nullable, and a check constraint says a
tenant-level row (`site_id` NULL) never carries one: a vendor id always names
one end customer, so it belongs on a site-level row, while the tenant-level row
exists for per-client configuration in `extra`.

The constraint is added NOT VALID on purpose. Rows that predate sites are
tenant-level and still hold the vendor id they were configured with; the next
`sync_sites` run adopts each onto the site it names, at which point the row
satisfies the constraint. Nulling those ids here would throw away the very
information adoption needs, and validating the constraint would refuse the
migration on every database configured before 0003. It is validated in the same
migration when no such rows exist.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# The same function the models use, so the backfill agrees with what
# `sync_sites` will compute for the same names afterwards.
from app.models.site import normalise_name

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("normalized_name", sa.Text(), nullable=True))
    # A first pass any database can run, so the column is never left NULL;
    # the exact key is computed below when there is a connection to do it on.
    op.execute(sa.text("UPDATE sites SET normalized_name = lower(btrim(name))"))
    if not context.is_offline_mode():
        bind = op.get_bind()
        for site_id, name in bind.execute(sa.text("SELECT id, name FROM sites")).all():
            bind.execute(
                sa.text("UPDATE sites SET normalized_name = :key WHERE id = :id"),
                {"key": normalise_name(name), "id": site_id},
            )
    op.alter_column("sites", "normalized_name", existing_type=sa.Text(), nullable=False)
    op.create_index(
        "ix_sites_client_id_normalized_name",
        "sites",
        ["client_id", "normalized_name"],
        unique=False,
    )

    op.alter_column("tool_mappings", "external_id", existing_type=sa.Text(), nullable=True)
    # Raw SQL rather than `op.create_check_constraint(postgresql_not_valid=True)`,
    # which renders the same statement but trips an SAWarning inside Alembic.
    op.execute(
        sa.text(
            "ALTER TABLE tool_mappings "
            "ADD CONSTRAINT ck_tool_mappings_tenant_level_has_no_external_id "
            "CHECK (site_id IS NOT NULL OR external_id IS NULL) NOT VALID"
        )
    )
    # Validate straight away when nothing violates it, which is every database
    # that never had a pre-0003 mapping. Where legacy rows remain, run
    #   ALTER TABLE tool_mappings
    #     VALIDATE CONSTRAINT ck_tool_mappings_tenant_level_has_no_external_id;
    # once sync_sites has adopted them.
    if not context.is_offline_mode():
        violations = op.get_bind().scalar(
            sa.text(
                "SELECT count(*) FROM tool_mappings "
                "WHERE site_id IS NULL AND external_id IS NOT NULL"
            )
        )
        if not violations:
            op.execute(
                sa.text(
                    "ALTER TABLE tool_mappings "
                    "VALIDATE CONSTRAINT ck_tool_mappings_tenant_level_has_no_external_id"
                )
            )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_tool_mappings_tenant_level_has_no_external_id"), "tool_mappings", type_="check"
    )
    # A configuration-only tenant row has nothing to put in a NOT NULL vendor id.
    op.execute(sa.text("DELETE FROM tool_mappings WHERE external_id IS NULL"))
    op.alter_column("tool_mappings", "external_id", existing_type=sa.Text(), nullable=False)

    op.drop_index("ix_sites_client_id_normalized_name", table_name="sites")
    op.drop_column("sites", "normalized_name")

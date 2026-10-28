"""A site may hold more than one vendor organisation per connector; sites remember absorbed names.

Revision ID: 0013
Revises: 0012

Live on the pilot: customers with two GravityZone companies, both holding
endpoints — one made by hand before the NinjaOne integration and one the
integration created with a six-character suffix. Both are the customer's, so
a site may carry both mappings and a site-scoped read covers their union.
`(connector_name, external_id)` stays unique across every client: a vendor
organisation still belongs to exactly one site.

`sites.absorbed_names` is what a merge leaves behind — the merged site's name
and the vendor names of the mappings it brought — so a later sync maps a
company with one of those names onto the kept site instead of recreating it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("uq_tool_mappings_site_id_connector_name"), "tool_mappings", type_="unique"
    )
    op.create_index(
        "ix_tool_mappings_site_id_connector_name",
        "tool_mappings",
        ["site_id", "connector_name"],
    )
    op.add_column(
        "sites",
        sa.Column(
            "absorbed_names",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("sites", "absorbed_names")
    op.drop_index("ix_tool_mappings_site_id_connector_name", table_name="tool_mappings")
    # Fails if any site holds two mappings for one connector: that is the point.
    op.create_unique_constraint(
        op.f("uq_tool_mappings_site_id_connector_name"),
        "tool_mappings",
        ["site_id", "connector_name"],
    )

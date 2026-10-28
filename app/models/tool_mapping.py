"""Cross-tool identity map: our records to a vendor's own identifiers.

A mapping row is one of two kinds, and `site_id` is what tells them apart.

**Site-level** (`site_id` set) is the common one: it holds the vendor's
organisation or company id for one end customer, and it is what every tool call
is scoped by. A site may hold more than one per connector — a customer with a
hand-made GravityZone company and an integration-created one — and a
site-scoped call then covers their union.

**Tenant-level** (`site_id` NULL) belongs to the franchisee as a whole. It
carries per-client configuration for the connector in `extra` — the sync rules
that decide which vendor organisations are internal, for instance — and never a
vendor id or a data-read scope.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ConnectorName
from app.models.mixins import TimestampMixin
from app.models.types import connector_name_type

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.site import Site


class ToolMapping(Base, TimestampMixin):
    """One site's identity inside one connector, or one client's configuration for it.

    ``external_id`` is whatever the vendor calls the end customer: a NinjaOne
    organisation id, an Autotask companyID, a GravityZone company id.
    """

    __tablename__ = "tool_mappings"
    __table_args__ = (
        # A site may hold several organisations on one connector (a customer
        # with two GravityZone companies, both theirs); a site-scoped read
        # covers their union. Since migration 0013.
        Index("ix_tool_mappings_site_id_connector_name", "site_id", "connector_name"),
        # A vendor organisation belongs to exactly one site, across every
        # client. This is the database half of hard rule 1: two franchisees
        # cannot resolve to the same organisation, whether by mistake or not.
        UniqueConstraint("connector_name", "external_id"),
        # At most one tenant-level row per (client, connector). Partial,
        # because the site-level rows deliberately repeat that pair.
        Index(
            "uq_tool_mappings_tenant_level",
            "client_id",
            "connector_name",
            unique=True,
            postgresql_where=text("site_id IS NULL"),
        ),
        Index("ix_tool_mappings_client_id_connector_name", "client_id", "connector_name"),
        # A tenant-level row holds per-client configuration (`extra`), never a
        # vendor id: a vendor id always names one end customer. Added NOT VALID
        # in migration 0004 so rows that predate sites survive until sync_sites
        # adopts them onto a site.
        CheckConstraint(
            "site_id IS NOT NULL OR external_id IS NULL",
            name="tenant_level_has_no_external_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"),
        nullable=True,
    )
    """The end customer this identity belongs to. NULL means tenant-level."""

    connector_name: Mapped[ConnectorName] = mapped_column(connector_name_type, nullable=False)
    external_id: Mapped[str | None] = mapped_column(nullable=True)
    """The vendor's id for the site. NULL on tenant-level rows."""

    external_name: Mapped[str | None] = mapped_column(nullable=True)
    """Name shown in the vendor console, for humans checking a mapping."""

    extra: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    """Non-secret connector-specific identifiers, e.g. a Microsoft tenant id."""

    client: Mapped[Client] = relationship(back_populates="tool_mappings")
    site: Mapped[Site | None] = relationship(back_populates="tool_mappings")

    @property
    def is_tenant_level(self) -> bool:
        return self.site_id is None

    def __repr__(self) -> str:
        level = "tenant" if self.is_tenant_level else f"site={self.site_id!r}"
        return (
            f"<ToolMapping client_id={self.client_id!r} {level} "
            f"connector={self.connector_name!r} external_id={self.external_id!r}>"
        )

"""The client: the franchisee we work for, and the owner of the credentials.

Tenancy is two levels: a `Client` is the MSP, and a `Site` is one of that MSP's
own end customers. Credentials hang off the client; the vendor organisation ids
that scope a data read hang off its sites.

``client_id`` is the scoping key for every tool call (hard rule 1). It is a UUID
because it is passed around outside the database — into tool arguments, Slack
commands and audit records — and must not be guessable or reused.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.site import Site
    from app.models.tool_credential import ToolCredential
    from app.models.tool_mapping import ToolMapping

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"


class ClientKind(StrEnum):
    """Whether a client's connectors reach real vendors or the demo tenant's fakes.

    `live` is a franchisee we hold credentials for. `demo` is the fictional
    franchisee `app/demo/` generates: the factory serves it only from the fake
    connector classes, it can hold no credential, and every audit row written
    for it is marked `demo`. The two never cross (`app/connectors/factory.py`).
    """

    LIVE = "live"
    DEMO = "demo"


class Client(Base, TimestampMixin):
    """An MSP tenant. Credentials and tool identities hang off this row."""

    __tablename__ = "clients"
    __table_args__ = (
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        CheckConstraint("kind IN ('live', 'demo')", name="kind_known"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    slug: Mapped[str] = mapped_column(unique=True, nullable=False)
    """Short lowercase handle typed by engineers in Slack, e.g. ``teamlogic-austin``."""

    name: Mapped[str] = mapped_column(nullable=False)
    """Display name as the engineers know it."""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    """Deactivate rather than delete: audit rows reference this client."""

    kind: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'live'"))
    """`live` or `demo` (`ClientKind`). Decides which connector classes the factory builds."""

    @property
    def is_demo(self) -> bool:
        return self.kind == ClientKind.DEMO

    notes: Mapped[str | None] = mapped_column(nullable=True)

    credentials: Mapped[list[ToolCredential]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tool_mappings: Mapped[list[ToolMapping]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sites: Mapped[list[Site]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Client slug={self.slug!r} id={self.id!r}>"

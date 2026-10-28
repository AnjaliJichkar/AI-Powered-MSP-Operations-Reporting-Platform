"""The site: one end customer of a franchisee.

Tenancy is two levels. A `Client` is the MSP we work for — the franchisee — and
owns the credentials. A `Site` is one of that franchisee's own customers, and is
what a vendor calls an organisation (NinjaOne) or a company (Autotask).

Sites are discovered, not typed in: `sync_sites` lists the tenant's
organisations through the client's own credentials and upserts a row per one.
A site that disappears from the vendor is marked inactive rather than deleted,
because `audit_log` and `tool_mappings` reference it and because a site that
vanishes for a day should not lose its history.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.tool_mapping import ToolMapping

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"

#: Trailing tokens that say what kind of company it is, not which one.
LEGAL_SUFFIX_TOKENS: frozenset[str] = frozenset(
    {
        "llc",
        "inc",
        "incorporated",
        "ltd",
        "limited",
        "pllc",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "lp",
        "llp",
        "pty",
        "gmbh",
    }
)

_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")
_NOT_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise_name(name: str) -> str:
    """The key two vendors' names for one customer are matched on.

    "Acme Dental, LLC" in Autotask and "Acme Dental (Main St)" in NinjaOne both
    become ``acme dental``: trim, drop one trailing parenthetical, casefold,
    turn punctuation into spaces, collapse whitespace, then strip legal
    suffixes off the end. An empty result falls back to the casefolded
    original, so nothing normalises to "" and matches everything else that did.
    """
    text = name.strip()
    text = _TRAILING_PARENTHETICAL.sub("", text)
    text = text.casefold()
    text = _NOT_WORD.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    tokens = text.split(" ") if text else []
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIX_TOKENS:
        tokens.pop()

    normalised = " ".join(tokens)
    return normalised or _WHITESPACE.sub(" ", name.strip().casefold()) or name


class Site(Base, TimestampMixin):
    """One end customer belonging to one client."""

    __tablename__ = "sites"
    __table_args__ = (
        # Slugs are what an engineer and the model name a site by, so they only
        # need to be unique within the franchisee that owns them.
        UniqueConstraint("client_id", "slug"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        Index("ix_sites_client_id_is_active", "client_id", "is_active"),
        # What sync_sites matches on when a second connector reports a customer
        # the first one already created.
        Index("ix_sites_client_id_normalized_name", "client_id", "normalized_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(nullable=False)
    """Short lowercase handle, unique within the client. Derived from the name."""

    name: Mapped[str] = mapped_column(nullable=False)
    """The site's name as the vendor reports it."""

    normalized_name: Mapped[str] = mapped_column(nullable=False)
    """`normalise_name(name)`, kept in step with `name` by the validator below."""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    """False once the vendor stops listing it. Never deleted: history points here."""

    absorbed_names: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """Names a merge folded into this site: `{"name", "connector", "external_id"}`
    per absorbed site name and vendor organisation, so a later sync maps a
    company with one of these names here rather than recreating a site."""

    client: Mapped[Client] = relationship(back_populates="sites")
    tool_mappings: Mapped[list[ToolMapping]] = relationship(
        back_populates="site",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @validates("name")
    def _derive_normalized_name(self, _key: str, value: str) -> str:
        # Runs on every assignment, the constructor included, so no code path
        # can rename a site and leave its matching key stale.
        self.normalized_name = normalise_name(value)
        return value

    def __repr__(self) -> str:
        return f"<Site slug={self.slug!r} client_id={self.client_id!r} active={self.is_active!r}>"

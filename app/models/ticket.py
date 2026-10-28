"""The ticket workspace: what the engine remembers about working one Autotask ticket.

Autotask holds the ticket; nothing here writes to it (hard rule 2). What we
keep is ours: which device the ticket concerns and how that was decided,
the enrichment facts as of a time, the remediation plans an engineer asked
for — versioned, each with its author and every step's citations and review
— and, once the ticket closes, which steps resolved it.

`ticket_workspaces` is one row per (client, Autotask ticket). The ticket's
text is stored **scrubbed** (`app/tickets/scrub.py`): a password or a key
pasted into a ticket never reaches this table or the model.

`ticket_plans` is one row per generation: version 1, 2, 3 of a ticket's plan,
never overwritten. A step is a dict in `steps`: its text, its citations
(a resolved ticket, an enrichment fact, or a documentation URL — a step with
none is not stored), a command only when it appeared in the cited source,
and the engineer's review of it. `outcome` is filled when the ticket closes
and the engineer says which steps resolved it; that is what ranks a step the
next time a similar ticket comes in.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class TicketWorkspace(Base, TimestampMixin):
    __tablename__ = "ticket_workspaces"
    __table_args__ = (
        UniqueConstraint("client_id", "ticket_id", name="uq_ticket_workspaces_ticket"),
        Index("ix_ticket_workspaces_client_open", "client_id", "closed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True
    )
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    """Autotask's numeric id, as text: the stable key."""
    ticket_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Scrubbed before it is stored. Never the raw ticket text."""
    contact: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The contact's address as Autotask holds it, where it holds one. It is
    what the Microsoft 365 account on the page is matched by: a name matches
    several people in a directory and an address matches one. Stored because
    the page needs it on every refresh and the alternative is re-reading the
    ticket for it (CLAUDE.md: names and addresses where a page needs them)."""
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_created_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    scrubbed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """How many likely secrets were removed from the ticket text."""
    status_checked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """When polling first saw Autotask's completion date on the ticket."""

    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    device_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    """How the device was decided: hostname, contact, engineer, or none."""
    device_confidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    """high, medium or low; an engineer's choice is high."""
    device_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """Every candidate the resolution found: device_id, name, method, confidence, why."""
    device_chosen_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    device_chosen_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    device_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    enrichment: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """`{"as_of", "facts": [{"id", "label", "value", "source"}], "errors": [...]}`."""
    enriched_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    opened_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<TicketWorkspace client={self.client_id!r} ticket={self.ticket_number!r}>"


class TicketPlan(Base):
    __tablename__ = "ticket_plans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_ticket_plans_version"),
        Index("ix_ticket_plans_client", "client_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    author: Mapped[str] = mapped_column(Text, nullable=False)
    """The author's email, or a CLI handle, for the screen."""
    generated_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    """The device the plan was written for, as it stood when it was generated."""
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """Ordered. `{"id", "text", "citations": [{"kind", "ref", "label", "url"}],
    "command", "manual", "review": {"status", "text", "by", "at"}}`."""
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """What the model was given and what it searched: tickets, facts, queries, urls."""
    dropped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    """Steps the model wrote that cited nothing, and so were not kept."""
    problems: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    outcome: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """`{"resolved_steps": [...ids], "none": bool, "note", "by", "by_user_id", "at"}`."""

    def __repr__(self) -> str:
        return f"<TicketPlan workspace={self.workspace_id!r} v{self.version}>"

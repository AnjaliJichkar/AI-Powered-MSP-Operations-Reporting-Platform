"""The outcome ledger: one row per completed ticket, and what it cost to close.

Resolved is sold per outcome, so an outcome has to be a row somebody can count,
price and argue with — not a number recomputed from a vendor every time it is
asked for. `ticket_outcomes` is that row, written by the daily collector from
Autotask and from our own ticket workspaces:

* **what it was** — the customer, the ticket, and its class (L1, L2 or L3, by
  the franchisee's own mapping from priority, queue and issue type);
* **who closed it** — Inputiv or the franchisee, by the franchisee's own mapping
  of Autotask resources to us;
* **how long it took** — business minutes to first response and to completion,
  on that franchisee's hours, holidays and time zone;
* **whether it met the targets** — one answer per clock, and `NULL` where we
  will not judge (a paused clock, or a priority with no target), so a percentage
  is taken over the tickets that were judged rather than over everything;
* **whether it stayed closed** — a ticket completed and opened again inside the
  franchisee's reopen window is not an outcome, and says so;
* **what we did about it** — the ticket-workspace plan, if one was written, and
  what its engineer recorded as having actually resolved it.

The row is keyed by `(client_id, ticket_id)` and written with an upsert, so a
rerun of the collector changes nothing and a ticket that closes, reopens and
closes again is one row that keeps its history rather than three.

Scoped to a client like everything else (hard rule 1), and derived: nothing here
is a source of truth, so a row may be rebuilt from Autotask at any time.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin

#: What the ledger records as having closed a ticket. `unknown` is a real
#: answer: a ticket Autotask has no resource on, or a resource the franchisee's
#: mapping does not name, is not quietly counted as either side.
RESOLVER_SIDES: tuple[str, ...] = ("inputiv", "franchisee", "unknown")

#: The classes an outcome is priced at.
OUTCOME_CLASSES: tuple[str, ...] = ("L1", "L2", "L3")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class TicketOutcome(Base, TimestampMixin):
    """One completed ticket, measured."""

    __tablename__ = "ticket_outcomes"
    __table_args__ = (
        # One row per ticket per client. The upsert the collector uses depends
        # on this, and so does the promise that a rerun changes nothing.
        UniqueConstraint("client_id", "ticket_id", name="uq_ticket_outcomes_ticket"),
        CheckConstraint(_in("resolver", RESOLVER_SIDES), name="resolver_is_known_side"),
        CheckConstraint(_in("ticket_class", OUTCOME_CLASSES), name="class_is_l1_l2_or_l3"),
        # The two questions asked of this table: "what closed in this range",
        # and "what closed for this customer".
        Index("ix_ticket_outcomes_completed", "client_id", "completed_at"),
        Index("ix_ticket_outcomes_site", "client_id", "site_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    """The franchisee. CASCADE because this is derived: deleting a client's
    measurements loses nothing that cannot be read from Autotask again."""

    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True
    )
    """The end customer. Null when the ticket's company maps to no site we hold."""

    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    """Autotask's own entity id, as text: it is an identifier, never arithmetic."""

    ticket_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    ticket_class: Mapped[str] = mapped_column(Text, nullable=False)
    """L1, L2 or L3, by the franchisee's own rules at the time it was written."""

    resolver: Mapped[str] = mapped_column(Text, nullable=False)
    resolver_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Autotask's name for the assigned resource, kept so a disputed row can be
    checked. Never used to decide the side: that is the mapping's job."""

    # The labels this row was classified and judged under, kept because they are
    # per-tenant picklists that a franchisee can rename underneath us.
    priority_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_type_label: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at_vendor: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    first_response_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    pickup_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Business minutes from created to first response, on this franchisee's
    hours. Null when Autotask never stamped a first response."""

    resolve_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Business minutes from created to completed."""

    first_response_met: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    resolution_met: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """True, false, or null where the clock was paused or had no target. A
    percentage is taken over the rows that are not null."""

    reopened: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    reopened_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """When we first saw it open again after having recorded it complete."""

    hours_logged: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    """Time entries against this ticket, in hours. Null when none were read."""

    cost: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    """Those hours at each engineer's loaded hourly cost. Null when the
    franchisee has no cost book: a guessed rate is worse than no figure."""

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ticket_workspaces.id", ondelete="SET NULL"), nullable=True
    )
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """What the plan's engineer recorded as having resolved it, verbatim from
    the plan. Null when no plan was written, or none has been marked."""

    collected_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    """When the collector last looked at this ticket."""

    def __repr__(self) -> str:  # no title, no customer: this is a log line
        return (
            f"<TicketOutcome client={self.client_id!r} ticket={self.ticket_id!r} "
            f"class={self.ticket_class!r} resolver={self.resolver!r}>"
        )

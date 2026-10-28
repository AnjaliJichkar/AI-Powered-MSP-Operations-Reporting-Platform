"""Append-only record of everything the agent is asked and everything it does.

Hard rule 4: every prompt, tool call and result is logged with the user, the
client, a timestamp and an outcome.

The record of what happened is append-only: no code rewrites what was asked,
what ran, or what came back. The one exception is the `feedback_*` columns,
which annotate an answer after the fact with an engineer's verdict on it. That
is new information about an existing row, not a revision of it.

One agent run is a set of rows sharing ``request_id``; ``id`` gives their order.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Identity, Index, Integer, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    AuditEventType,
    AuditOutcome,
    ConnectorName,
    FeedbackRating,
    Interface,
)
from app.models.types import (
    audit_event_type,
    audit_outcome_type,
    connector_name_type,
    feedback_rating_type,
    interface_type,
)


class AuditLog(Base):
    """One step of one request."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_request_id_id", "request_id", "id"),
        Index("ix_audit_log_client_id_created_at", "client_id", "created_at"),
        Index("ix_audit_log_actor_id_created_at", "actor_id", "created_at"),
        Index("ix_audit_log_actor_user_id_created_at", "actor_user_id", "created_at"),
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_conversation_id", "conversation_id"),
        # Partial: the weekly prompt review only reads rows that were rated.
        Index(
            "ix_audit_log_feedback_rating_created_at",
            "feedback_rating",
            "created_at",
            postgresql_where=text("feedback_rating IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    request_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    """Groups every row belonging to a single agent run."""

    event_type: Mapped[AuditEventType] = mapped_column(audit_event_type, nullable=False)
    outcome: Mapped[AuditOutcome] = mapped_column(audit_outcome_type, nullable=False)

    actor_id: Mapped[str] = mapped_column(nullable=False)
    """Who asked, as text: the authenticated user's id, or ``system`` /
    ``selection-check`` for something no person started. Rows written before
    authentication existed hold the handle an engineer typed, and keep it."""

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """The authenticated user, once there is one. Null for a scripted or
    system run, and for every row written before sign-in existed: the actor of
    those is whatever `actor_id` already said, which is not rewritten.
    RESTRICT, like the conversation key: suspending or reassigning a user
    cannot remove the record of what they did."""

    interface: Mapped[Interface] = mapped_column(interface_type, nullable=False)

    client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """Null only when the request was rejected before a client could be resolved."""

    client_ref: Mapped[str | None] = mapped_column(nullable=True)

    demo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    """True when the row was written for a demo client (`Client.kind`), so demo traffic
    is told apart from a franchisee's in every figure read from this table."""
    """The client identifier as supplied, kept even when it resolved to nothing."""

    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """The thread this run belonged to. Null for runs from before threads existed.
    RESTRICT, not CASCADE: nothing done to a thread can remove an audit row."""

    connector_name: Mapped[ConnectorName | None] = mapped_column(connector_name_type, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(nullable=True)

    model: Mapped[str | None] = mapped_column(nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(nullable=True)

    payload: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    """Redacted detail: prompt text, tool arguments, result summary."""

    # --- feedback: the only fields written after the row is created ---------

    feedback_rating: Mapped[FeedbackRating | None] = mapped_column(
        feedback_rating_type, nullable=True
    )
    """Set when an engineer rates the answer this row records."""

    feedback_comment: Mapped[str | None] = mapped_column(nullable=True)
    feedback_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    feedback_by: Mapped[str | None] = mapped_column(nullable=True)
    """Who rated it, which need not be who asked."""

    def __repr__(self) -> str:
        return (
            f"<AuditLog request_id={self.request_id!r} event={self.event_type!r} "
            f"outcome={self.outcome!r}>"
        )

"""Conversation threads: what an engineer and the agent have said about one client.

A thread belongs to exactly one client and one engineer. It is replayed into the
model on every turn, so an engineer who named a device once does not have to
name it again.

Two things a thread is not:

- **Not the audit trail.** `audit_log` records what happened and is append-only;
  these tables hold what is replayed. Archiving a thread never touches an audit
  row, and an audit row references a thread without cascading from it.
- **Not editable.** Messages are written once, in order, and never changed.
  Trimming for the context budget happens to the copy that is replayed, not to
  what is stored. Threads are archived, never deleted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import MessageRole
from app.models.mixins import TimestampMixin
from app.models.types import message_role_type

if TYPE_CHECKING:
    from app.models.client import Client


class Conversation(Base, TimestampMixin):
    """One thread between one engineer and the agent, about one client."""

    __tablename__ = "conversations"
    __table_args__ = (
        # The sidebar's query: this engineer's threads for this client, newest first.
        Index(
            "ix_conversations_engineer_client_updated",
            "engineer_id",
            "client_id",
            text("updated_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    """The one client this thread is about. A request forced to any other client
    may not read or continue it (hard rule 1)."""

    engineer_id: Mapped[str] = mapped_column(nullable=False)
    """Who owns the thread, as text: the same value `audit_log.actor_id` holds,
    which since authentication is the user's id. Threads started before that
    hold the handle their engineer typed, and keep it."""

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """The authenticated owner. Null on threads from before sign-in existed, so
    `check_access` falls back to `engineer_id` for those and compares this
    otherwise: a thread is never opened by someone it does not belong to
    because the old column happened to match."""

    title: Mapped[str | None] = mapped_column(nullable=True)
    """The first question, truncated. Null until there is one."""

    archived_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """Set when the engineer archives the thread. Archived, never deleted."""

    client: Mapped[Client] = relationship()
    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ConversationMessage.seq",
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def __repr__(self) -> str:
        return (
            f"<Conversation id={self.id!r} client_id={self.client_id!r} "
            f"engineer_id={self.engineer_id!r}>"
        )


class ConversationMessage(Base):
    """One message of a thread, exactly as it is replayed to the model."""

    __tablename__ = "conversation_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "seq"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    """Position in the thread, from 1. Replay order."""

    role: Mapped[MessageRole] = mapped_column(message_role_type, nullable=False)
    content_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    """The Anthropic content block list, verbatim: text, tool_use, tool_result,
    server_tool_use, web_search_tool_result. A user turn holds the engineer's
    question only; the client context is added when the thread is assembled."""

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return (
            f"<ConversationMessage conversation_id={self.conversation_id!r} "
            f"seq={self.seq!r} role={self.role!r}>"
        )

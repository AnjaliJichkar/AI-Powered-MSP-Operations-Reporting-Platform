"""Conversation threads over HTTP: create, list, read, archive.

A thread belongs to one client and one account. Both are resolved from the
session, so neither the client nor the owner is something a caller can assert:
the routes below take no `user_id` and no `client_id` at all.

The ownership check is now an identity check. A thread started since sign-in
existed carries `owner_user_id` and that is what is compared; one started
before carries only the handle its engineer typed, and cannot be opened through
these routes at all (`check_access` refuses it rather than guessing). Sharing a
thread between people is still Phase 2.

Nothing here deletes anything. A thread is archived; its messages and every
audit row stay.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.conversations import (
    ConversationClientMismatch,
    ConversationError,
    ConversationForbidden,
    ConversationNotFound,
    archive_conversation,
    check_access,
    get_conversation,
    list_conversations,
    start_conversation,
    stored_messages,
    text_of,
)
from app.auth.dependencies import CAN_CHAT
from app.auth.scope import Scope
from app.connectors.factory import resolve_client
from app.db.session import get_session
from app.models.conversation import Conversation
from app.models.enums import Interface

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversations"])


class ConversationView(BaseModel):
    id: str
    title: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    archived_at: dt.datetime | None
    turns: int = 0
    """How many questions have been asked in this thread. The rail prints it
    beside the date, so a thread with one question and one with forty are told
    apart without opening either."""

    @classmethod
    def of(cls, conversation: Conversation, turns: int = 0) -> ConversationView:
        return cls(
            id=str(conversation.id),
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            archived_at=conversation.archived_at,
            turns=turns,
        )


class MessageView(BaseModel):
    seq: int
    role: str
    text: str
    tools: list[str]
    content: list[dict[str, Any]]
    at: dt.datetime | None = None
    """When this message was written. A replayed turn used to carry no time at
    all, so the page could say who answered and never when."""


class EmptyRequest(BaseModel):
    """A body with nothing in it, and nothing allowed in it.

    Archiving and creating used to carry `user_id`. They carry nothing now, and
    a caller that still sends a field is told rather than ignored.
    """

    model_config = ConfigDict(extra="forbid")


def http_error(error: ConversationError) -> HTTPException:
    """One mapping, shared with `/chat`, from a thread refusal to a status code."""
    if isinstance(error, ConversationNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, ConversationClientMismatch | ConversationForbidden):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))


@router.post("/conversations", response_model=ConversationView, status_code=201)
async def create_conversation(
    request: EmptyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> ConversationView:
    """Start an empty thread for the session's client. `/chat` does this itself."""
    client = await resolve_client(session, scope.require_client())
    conversation = await start_conversation(
        session,
        client_id=client.id,
        client_ref=client.slug,
        engineer_id=scope.actor_id,
        interface=Interface.API,
        owner_user_id=scope.user_id,
    )
    await session.commit()
    await session.refresh(conversation)
    return ConversationView.of(conversation)


@router.get("/conversations", response_model=list[ConversationView])
async def get_conversations(
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> list[ConversationView]:
    """The caller's own open threads for the session's client, newest first."""
    threads = await list_conversations(
        session, client_id=scope.require_client(), engineer_id=scope.actor_id
    )
    return [ConversationView.of(thread, turns) for thread, turns in threads]


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageView])
async def get_messages(
    conversation_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> list[MessageView]:
    """A thread's messages in order, for the account that owns it.

    The client is checked as well as the owner: another client's thread is
    refused even to an Inputiv engineer who has not switched to that client.
    """
    try:
        conversation = await get_conversation(session, conversation_id)
        check_access(conversation, client_id=scope.require_client(), owner_user_id=scope.user_id)
    except ConversationError as exc:
        raise http_error(exc) from exc
    rows = await stored_messages(session, conversation.id)
    return [
        MessageView(
            seq=index,
            role=row.role.value,
            text=text_of(row.content_json),
            tools=[
                str(block.get("name"))
                for block in row.content_json
                if block.get("type") in ("tool_use", "server_tool_use")
            ],
            content=row.content_json,
            at=row.created_at,
        )
        for index, row in enumerate(rows, start=1)
    ]


@router.post("/conversations/{conversation_id}/archive", response_model=ConversationView)
async def archive(
    conversation_id: str,
    request: EmptyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_CHAT)],
) -> ConversationView:
    """Archive a thread. Idempotent. Deletes nothing — least of all an audit row."""
    try:
        conversation = await get_conversation(session, conversation_id)
        check_access(conversation, client_id=scope.require_client(), owner_user_id=scope.user_id)
    except ConversationError as exc:
        raise http_error(exc) from exc
    client = await resolve_client(session, conversation.client_id)
    await archive_conversation(
        session,
        conversation,
        actor_id=scope.actor_id,
        client_ref=client.slug,
        interface=Interface.API,
    )
    await session.commit()
    await session.refresh(conversation)
    return ConversationView.of(conversation)

"""Conversation threads: loading, guarding, trimming, assembling and persisting.

`app/agent/loop.py` drives the model; this module is everything about the
thread around it. Keeping it separate keeps two things testable without a
model: the guard that a thread belongs to the client a request is forced to,
and the trimming that keeps a long thread inside the context budget.

What is stored and what is replayed are deliberately different:

- A **user turn is stored as the engineer's question only**. The client context
  (the site list) is added once, to the first message, when the thread is
  assembled — not repeated in every historical turn, and part of the prefix the
  prompt cache can reuse.
- **Stored rows are never modified.** Trimming works on the copy being replayed.
- A turn is **repaired before it is stored** (`finalise_turn`), so a later
  replay can never contain a `tool_use` without its `tool_result`.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.models.conversation import Conversation, ConversationMessage
from app.models.enums import AuditEventType, AuditOutcome, Interface, MessageRole

logger = logging.getLogger(__name__)


#: A rough characters-per-token figure for JSON-ish content. The budget is a
#: guard against runaway context, not an invoice, so an estimate is enough; the
#: API's own count is what gets billed and recorded.
CHARS_PER_TOKEN = 4

#: Block types the Messages API lets a cache breakpoint sit on.
CACHEABLE_BLOCK_TYPES = frozenset(
    {"text", "tool_use", "tool_result", "server_tool_use", "web_search_tool_result"}
)

Message = dict[str, Any]
"""`{"role": ..., "content": [...]}`, as the Messages API takes it."""


class ConversationError(Exception):
    """A thread could not be used for this request."""


class ConversationNotFound(ConversationError):
    pass


class ConversationClientMismatch(ConversationError):
    """The thread belongs to a different client than the request is forced to.

    Tenant isolation, not a soft check: replaying client A's thread into a
    request scoped to client B would put A's data in front of B's tools.
    """


class ConversationForbidden(ConversationError):
    """The thread belongs to a different engineer. Sharing is Phase 2."""


class ConversationArchived(ConversationError):
    """The thread was archived and can be read but not continued."""


# --- content blocks ---------------------------------------------------------


def dump_blocks(content: Any) -> list[dict[str, Any]]:
    """A message's content as plain JSON blocks, verbatim.

    SDK response blocks are dumped without their null fields, which is the
    shape the API accepts back as input. A bare string becomes one text block.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks: list[dict[str, Any]] = []
    for block in content or []:
        if hasattr(block, "model_dump"):
            blocks.append(block.model_dump(mode="json", exclude_none=True))
        elif isinstance(block, dict):
            blocks.append(copy.deepcopy(block))
    return blocks


def text_of(content: list[dict[str, Any]]) -> str:
    """The visible text of a stored message, for titles and the webview."""
    return "\n".join(
        str(block.get("text", "")) for block in content if block.get("type") == "text"
    ).strip()


def title_from(message: str) -> str:
    """A thread title: the first question, on one line.

    Whole, not cut short. It used to end at eighty characters and an ellipsis,
    which made the rail the one place in the product that truncated something —
    and worse, truncated it in the database, where no page could get the rest of
    it back. The rail shortens it to two lines in CSS and carries the whole
    question in its tooltip.
    """
    return " ".join(message.split())


def _has(content: list[dict[str, Any]], block_type: str) -> bool:
    return any(block.get("type") == block_type for block in content)


# --- loading and guarding ---------------------------------------------------


def _parse(conversation_id: str | uuid.UUID) -> uuid.UUID:
    if isinstance(conversation_id, uuid.UUID):
        return conversation_id
    try:
        return uuid.UUID(str(conversation_id))
    except (ValueError, AttributeError, TypeError):
        raise ConversationNotFound(f"{conversation_id!r} is not a conversation id") from None


async def get_conversation(session: AsyncSession, conversation_id: str | uuid.UUID) -> Conversation:
    conversation = (
        await session.execute(
            select(Conversation).where(Conversation.id == _parse(conversation_id))
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise ConversationNotFound("no conversation with that id")
    return conversation


def check_access(
    conversation: Conversation,
    *,
    client_id: uuid.UUID | None = None,
    engineer_id: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    for_writing: bool = False,
) -> None:
    """Refuse a thread that is not this client's, not this user's, or closed.

    The client check comes first and is the hard one (hard rule 1).

    The owner check is now an identity check rather than a courtesy. A
    thread started since sign-in existed carries `owner_user_id`, and that
    is what is compared; one started before carries only the handle its
    engineer typed, and falls back to comparing that. The two are never
    mixed: a request that knows its user id may not open a legacy thread by
    happening to match its handle, and vice versa.
    """
    if client_id is not None and conversation.client_id != client_id:
        raise ConversationClientMismatch(
            "this conversation belongs to a different client than the one this request "
            "is scoped to; start a new conversation for this client"
        )
    if owner_user_id is not None:
        if conversation.owner_user_id is None:
            raise ConversationForbidden(
                "this conversation predates sign-in and has no owner account; start a new one"
            )
        if conversation.owner_user_id != owner_user_id:
            raise ConversationForbidden("this conversation belongs to someone else")
    elif engineer_id is not None and conversation.engineer_id != engineer_id:
        raise ConversationForbidden("this conversation belongs to a different engineer")
    if for_writing and conversation.is_archived:
        raise ConversationArchived("this conversation is archived; start a new one")


async def stored_messages(
    session: AsyncSession, conversation_id: uuid.UUID
) -> list[ConversationMessage]:
    """The rows themselves, in replay order, for a caller that needs more than
    the model does — when each was written, say."""
    return list(
        (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.seq)
            )
        )
        .scalars()
        .all()
    )


async def load_history(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    """Every stored message of a thread, in replay order.

    Exactly what the model is replayed and nothing else: the shape here is the
    Anthropic one, and a field added for a page would be a field sent to a
    vendor. A page that wants more reads `stored_messages`.
    """
    rows = await stored_messages(session, conversation_id)
    return [{"role": row.role.value, "content": copy.deepcopy(row.content_json)} for row in rows]


async def list_conversations(
    session: AsyncSession, *, client_id: uuid.UUID, engineer_id: str, limit: int = 50
) -> list[tuple[Conversation, int]]:
    """This engineer's open threads for this client, most recently used first,
    each with how many questions have been asked in it.

    The count is a correlated subquery rather than a load of the messages: fifty
    threads of a hundred turns each is a lot of content block JSON to read in
    order to count rows.

    A turn is a question somebody asked, which is **not** the same as a user
    message. A tool result is sent to the model as a user message too, so
    counting the role alone said nine questions about a thread with five: the
    count is user messages carrying a text block, which is the engineer's own
    words and the only thing in a thread anybody typed.
    """
    turns = (
        select(func.count())
        .select_from(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == Conversation.id,
            ConversationMessage.role == MessageRole.USER,
            ConversationMessage.content_json.contains([{"type": "text"}]),
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(Conversation, turns)
        .where(
            Conversation.engineer_id == engineer_id,
            Conversation.client_id == client_id,
            Conversation.archived_at.is_(None),
        )
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )
    return [(conversation, int(count)) for conversation, count in rows.all()]


# --- creating and archiving (both audited) ----------------------------------


async def start_conversation(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    client_ref: str,
    engineer_id: str,
    interface: Interface,
    owner_user_id: uuid.UUID | None = None,
    title: str | None = None,
    request_id: uuid.UUID | None = None,
) -> Conversation:
    """Create a thread and record `conversation_started`. The caller commits."""
    conversation = Conversation(
        client_id=client_id,
        engineer_id=engineer_id,
        owner_user_id=owner_user_id,
        title=title,
    )
    session.add(conversation)
    await session.flush()
    await record_event(
        session,
        AuditContext(
            request_id=request_id or uuid.uuid4(),
            actor_id=engineer_id,
            interface=interface,
            actor_user_id=owner_user_id,
            client_id=client_id,
            client_ref=client_ref,
            conversation_id=conversation.id,
        ),
        event_type=AuditEventType.CONVERSATION_STARTED,
        outcome=AuditOutcome.SUCCESS,
        payload={"conversation_id": str(conversation.id), "title": title},
    )
    return conversation


async def archive_conversation(
    session: AsyncSession,
    conversation: Conversation,
    *,
    actor_id: str,
    client_ref: str,
    interface: Interface,
) -> bool:
    """Archive a thread and record it. Returns False if it already was.

    Nothing is deleted: not the thread, not its messages, not one audit row.
    """
    if conversation.is_archived:
        return False
    conversation.archived_at = dt.datetime.now(dt.UTC)
    await session.flush()
    await record_event(
        session,
        AuditContext(
            request_id=uuid.uuid4(),
            actor_id=actor_id,
            interface=interface,
            client_id=conversation.client_id,
            client_ref=client_ref,
            conversation_id=conversation.id,
        ),
        event_type=AuditEventType.CONVERSATION_ARCHIVED,
        outcome=AuditOutcome.SUCCESS,
        payload={"conversation_id": str(conversation.id)},
    )
    return True


# --- trimming -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrimReport:
    tokens_before: int
    tokens_after: int
    turns_total: int
    results_elided: int = 0
    turns_dropped: int = 0

    @property
    def trimmed(self) -> bool:
        return bool(self.results_elided or self.turns_dropped)

    def as_payload(self) -> dict[str, int]:
        return {
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "turns_total": self.turns_total,
            "results_elided": self.results_elided,
            "turns_dropped": self.turns_dropped,
        }


def estimate_tokens(messages: list[Message]) -> int:
    """Roughly how many input tokens these messages cost. See `CHARS_PER_TOKEN`."""
    return sum(len(json.dumps(message["content"], default=str)) for message in messages) // (
        CHARS_PER_TOKEN
    )


def split_turns(messages: list[Message]) -> list[list[Message]]:
    """Group messages into turns: a question and everything up to the next one.

    A user message carrying text opens a turn; a user message carrying only
    `tool_result` blocks belongs to the turn that asked for the tools. Dropping
    a whole turn therefore never separates a `tool_use` from its result.
    """
    turns: list[list[Message]] = []
    for message in messages:
        opens = message["role"] == "user" and not _has(message["content"], "tool_result")
        if opens or not turns:
            turns.append([])
        turns[-1].append(message)
    return turns


def _body_length(block: dict[str, Any]) -> int:
    body = block.get("content")
    return len(body) if isinstance(body, str) else len(json.dumps(body, default=str))


def trim_history(
    messages: list[Message], *, budget: int, keep_recent_turns: int
) -> tuple[list[Message], TrimReport]:
    """Fit a thread's history inside the context budget. Never edits what is stored.

    Oldest first, and the newest `keep_recent_turns` turns are never touched:

    1. Replace `tool_result` bodies with `[result elided, N chars]`, keeping the
       `tool_use` block, so the model still sees what was asked and that it was
       answered. Tool results are the bulk of a thread and the least useful part
       of an old turn.
    2. If that is not enough, drop whole oldest turns.
    """
    working = copy.deepcopy(messages)
    before = estimate_tokens(working)
    turns = split_turns(working)
    total_turns = len(turns)
    if before <= budget or total_turns <= keep_recent_turns:
        return working, TrimReport(before, before, total_turns)

    protected_from = max(total_turns - keep_recent_turns, 0)
    current = before
    elided = 0

    for turn in turns[:protected_from]:
        for message in turn:
            if message["role"] != "user":
                continue
            for block in message["content"]:
                if block.get("type") != "tool_result":
                    continue
                body = block.get("content")
                if isinstance(body, str) and body.startswith("[result elided"):
                    continue
                length = _body_length(block)
                stub = f"[result elided, {length} chars]"
                block["content"] = stub
                elided += 1
                current -= max(length - len(stub), 0) // CHARS_PER_TOKEN
                if current <= budget:
                    break
            if current <= budget:
                break
        if current <= budget:
            break

    dropped = 0
    current = estimate_tokens([message for turn in turns for message in turn])
    while current > budget and len(turns) > keep_recent_turns:
        current -= estimate_tokens(turns.pop(0))
        dropped += 1

    kept = [message for turn in turns for message in turn]
    return kept, TrimReport(before, estimate_tokens(kept), total_turns, elided, dropped)


# --- assembling what the model sees -----------------------------------------


def _mark_cache_breakpoint(message: Message) -> None:
    """Put a cache breakpoint on the last block of `message` that can carry one."""
    for block in reversed(message["content"]):
        if block.get("type") in CACHEABLE_BLOCK_TYPES:
            block["cache_control"] = {"type": "ephemeral"}
            return


def assemble_messages(history: list[Message], *, client_context: str, question: str) -> list[Any]:
    """The `messages` for this turn: the thread so far, then the new question.

    With no history this is the single message a standalone request always was.
    With history, the client context leads the first message, a cache breakpoint
    sits on the last historical message — everything up to it is identical to
    what the previous turn sent, so it is read from cache rather than billed in
    full — and the new question follows on its own.
    """
    if not history:
        return [{"role": "user", "content": f"{client_context}\n\nQuestion: {question}"}]

    messages = copy.deepcopy(history)
    context_block = {"type": "text", "text": client_context}
    if messages[0]["role"] == "user":
        messages[0]["content"] = [context_block, *messages[0]["content"]]
    else:  # pragma: no cover - a stored thread always opens with a question
        messages.insert(0, {"role": "user", "content": [context_block]})

    _mark_cache_breakpoint(messages[-1])
    messages.append({"role": "user", "content": [{"type": "text", "text": question}]})
    return messages


# --- persisting a turn --------------------------------------------------------


@dataclass(slots=True)
class TurnMessage:
    """One message the loop produced this turn, with the usage that paid for it."""

    role: str
    content: list[dict[str, Any]]
    input_tokens: int | None = None
    output_tokens: int | None = None


def finalise_turn(question: str, transcript: list[TurnMessage], answer: str) -> list[TurnMessage]:
    """What to store for one turn, repaired so that it always replays.

    - The user turn is the question alone.
    - Assistant messages that follow one another (a paused server-tool turn and
      its resumption) are merged, which is what the API does with them anyway.
    - A trailing assistant message whose `tool_use` was never answered — the run
      hit a cap or the clock first — is dropped: replaying it would be rejected.
    - If what remains does not end in assistant text, the answer the engineer
      was actually shown is stored as the closing message, so the thread reads
      the way it happened and the next turn has something to follow.
    """
    merged: list[TurnMessage] = []
    for entry in transcript:
        previous = merged[-1] if merged else None
        if previous is not None and previous.role == entry.role == "assistant":
            previous.content = [*previous.content, *copy.deepcopy(entry.content)]
            previous.input_tokens = (previous.input_tokens or 0) + (entry.input_tokens or 0)
            previous.output_tokens = (previous.output_tokens or 0) + (entry.output_tokens or 0)
        else:
            merged.append(
                TurnMessage(
                    entry.role,
                    copy.deepcopy(entry.content),
                    entry.input_tokens,
                    entry.output_tokens,
                )
            )

    while merged and merged[-1].role == "assistant" and _has(merged[-1].content, "tool_use"):
        merged.pop()

    closing = {"type": "text", "text": answer}
    if merged and merged[-1].role == "assistant":
        if not text_of(merged[-1].content):
            merged[-1].content.append(closing)
    else:
        merged.append(TurnMessage("assistant", [closing]))

    return [TurnMessage("user", [{"type": "text", "text": question}]), *merged]


async def append_turn(
    session: AsyncSession, conversation: Conversation, messages: list[TurnMessage]
) -> int:
    """Append a finalised turn to the thread. Returns how many rows were written."""
    last_seq = (
        await session.execute(
            select(func.coalesce(func.max(ConversationMessage.seq), 0)).where(
                ConversationMessage.conversation_id == conversation.id
            )
        )
    ).scalar_one()
    for offset, message in enumerate(messages, start=1):
        session.add(
            ConversationMessage(
                conversation_id=conversation.id,
                seq=last_seq + offset,
                role=MessageRole(message.role),
                content_json=message.content,
                input_tokens=message.input_tokens,
                output_tokens=message.output_tokens,
            )
        )
    if conversation.title is None and messages:
        conversation.title = title_from(text_of(messages[0].content)) or None
    # `onupdate` only fires when a column changes; the sidebar sorts on this.
    conversation.updated_at = dt.datetime.now(dt.UTC)
    await session.flush()
    return len(messages)

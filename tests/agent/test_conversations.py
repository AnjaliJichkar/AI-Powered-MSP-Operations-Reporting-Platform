"""Conversation threads, against a real database and a scripted model.

What is proven here: a turn is stored verbatim and replayed identically; a
thread cannot cross clients or engineers; a long thread is trimmed for the
model without being edited in storage; and archiving a thread leaves every
audit row where it was.
"""

from __future__ import annotations

import copy
import itertools
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.chat as chat_module
from app.agent.conversations import (
    ConversationArchived,
    ConversationClientMismatch,
    ConversationForbidden,
    ConversationNotFound,
    TurnMessage,
    append_turn,
    dump_blocks,
    start_conversation,
)
from app.agent.loop import STATUS_CAPPED, STATUS_OK, AgentResult, run_agent
from app.db.session import get_session
from app.main import create_app
from app.models import AuditLog, Client, Conversation, ConversationMessage, User
from app.models.enums import AuditEventType, AuditOutcome, Interface
from app.settings import Settings
from tests.agent.conftest import CALLS, LIVE_URL, audit_rows
from tests.agent.fake_model import FakeAnthropic, calls, says, searches, searches_then_calls
from tests.authfixtures import make_scope, sign_in

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set"),
    pytest.mark.usefixtures("demo_tools"),
]

USER = "engineer-1"
KB = ("https://ninjaone.example/kb/agent-offline", "Why a NinjaOne agent shows offline")

Maker = async_sessionmaker[AsyncSession]


async def stored(maker: Maker, conversation_id: uuid.UUID | None) -> list[ConversationMessage]:
    assert conversation_id is not None
    async with maker() as session:
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


def replayed_blocks(request: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """The history a request carried, minus what assembly adds around it.

    Assembly prepends the client context to the first message and puts a cache
    breakpoint on the last historical block; the new question is the final
    message. Everything else must be the stored blocks, untouched.
    """
    history = copy.deepcopy(request["messages"][:-1])
    history[0]["content"] = history[0]["content"][1:]
    for message in history:
        for block in message["content"]:
            block.pop("cache_control", None)
    return [(message["role"], message["content"]) for message in history]


def unanswered_tool_uses(rows: list[ConversationMessage]) -> set[str]:
    used = {b["id"] for r in rows for b in r.content_json if b.get("type") == "tool_use"}
    answered = {
        b["tool_use_id"] for r in rows for b in r.content_json if b.get("type") == "tool_result"
    }
    return used - answered


async def ask(
    client: Client, model: FakeAnthropic, settings: Settings, message: str, **kwargs: Any
) -> AgentResult:
    return await run_agent(
        kwargs.pop("user", USER),
        client.id,
        message,
        Interface.API,
        model_client=model,
        settings=settings,
        **kwargs,
    )


async def ask_as(
    user: User,
    client: Client,
    model: FakeAnthropic,
    settings: Settings,
    message: str,
    **kwargs: Any,
) -> AgentResult:
    """`ask`, as a signed-in account rather than a bare handle.

    The HTTP routes own threads by `owner_user_id`, so a thread the API is
    then expected to open must have been created with one. `user` matches
    what the routes put in `actor_id`, which is what the listing filters on.
    """
    return await ask(
        client,
        model,
        settings,
        message,
        user=str(user.id),
        actor_user_id=user.id,
        **kwargs,
    )


# --- replay round-trip ----------------------------------------------------


async def test_a_turn_with_a_tool_call_and_a_web_search_replays_identically(
    db_client: Client, settings: Settings, session_maker: Maker
) -> None:
    searched_and_called = searches_then_calls("ninjaone agent offline", [KB], "demo_list", days=3)
    answered = says("Two devices: PC-01 and PC-02.")
    first = await ask(
        db_client, FakeAnthropic(searched_and_called, answered), settings, "what about PC-01?"
    )
    assert first.status == STATUS_OK
    assert first.conversation_id is not None

    rows = await stored(session_maker, first.conversation_id)
    assert [(row.seq, row.role.value) for row in rows] == [
        (1, "user"),
        (2, "assistant"),
        (3, "user"),
        (4, "assistant"),
    ]
    # The user turn is the question alone: no client context, no site list.
    assert rows[0].content_json == [{"type": "text", "text": "what about PC-01?"}]
    # The assistant's blocks are verbatim: search, its result, and the tool call.
    assert rows[1].content_json == dump_blocks(searched_and_called.content)
    assert [block["type"] for block in rows[1].content_json] == [
        "server_tool_use",
        "web_search_tool_result",
        "tool_use",
    ]
    assert rows[2].content_json[0]["type"] == "tool_result"
    assert rows[3].content_json == dump_blocks(answered.content)
    assert (rows[1].input_tokens, rows[1].output_tokens) == (100, 50)
    assert unanswered_tool_uses(rows) == set()

    # Second turn: the thread is replayed ahead of the new question.
    model = FakeAnthropic(says("It last checked in on Monday."))
    second = await ask(
        db_client,
        model,
        settings,
        "and when did it last check in?",
        conversation_id=first.conversation_id,
    )
    assert second.conversation_id == first.conversation_id

    request = model.messages.requests[0]
    assert replayed_blocks(request) == [(row.role.value, row.content_json) for row in rows]
    assert request["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "and when did it last check in?"}],
    }
    # The client is named once, at the head of the thread, and never by id.
    context = request["messages"][0]["content"][0]["text"]
    assert "Loop Test Client" in context and str(db_client.id) not in context
    # Prompt caching: the system prompt, and the last message before the new turn.
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    assert len(await stored(session_maker, first.conversation_id)) == 6


async def test_a_new_thread_is_started_and_audited_when_none_is_given(
    db_client: Client, settings: Settings, engine: Any, session_maker: Maker
) -> None:
    question = "which devices are offline at  " + "x" * 120
    result = await ask(db_client, FakeAnthropic(says("None.")), settings, question)

    rows = await audit_rows(engine, db_client.id)
    started = [row for row in rows if row.event_type is AuditEventType.CONVERSATION_STARTED]
    assert len(started) == 1
    assert started[0].conversation_id == result.conversation_id
    assert started[0].request_id == result.request_id
    # Every row of the run carries the thread, the run_finished row included.
    assert {row.conversation_id for row in rows} == {result.conversation_id}
    assert rows[-1].event_type is AuditEventType.RUN_FINISHED
    assert rows[-1].payload["conversation_id"] == str(result.conversation_id)

    async with session_maker() as session:
        thread = await session.get(Conversation, result.conversation_id)
    assert thread is not None
    assert thread.client_id == db_client.id and thread.engineer_id == USER
    # The title is the question, whole and on one line. It used to stop at
    # eighty characters and an ellipsis, in the database, where no page could
    # get the rest of it back; the rail shortens it in CSS instead and carries
    # the whole of it in the button's tooltip.
    assert thread.title == " ".join(question.split())
    assert len(thread.title) > 80
    assert "…" not in thread.title

    other = await ask(db_client, FakeAnthropic(says("None.")), settings, "a separate question")
    assert other.conversation_id != result.conversation_id


# --- isolation ------------------------------------------------------------


async def test_a_thread_from_another_client_is_refused_and_nothing_is_written(
    db_client: Client,
    make_client: Callable[[str, str], Awaitable[Client]],
    settings: Settings,
    engine: Any,
    session_maker: Maker,
) -> None:
    """Hard rule 1. Client A's thread must never be replayed into client B's tools."""
    client_b = await make_client("other-franchisee", "Other Franchisee")
    first = await ask(db_client, FakeAnthropic(says("Two devices.")), settings, "how many?")
    before = await stored(session_maker, first.conversation_id)

    model = FakeAnthropic(says("never reached"))
    with pytest.raises(ConversationClientMismatch, match="different client"):
        await ask(
            client_b, model, settings, "and for this one?", conversation_id=first.conversation_id
        )

    assert model.call_count == 0  # refused before any model call
    assert CALLS == []
    after = await stored(session_maker, first.conversation_id)
    assert [row.id for row in after] == [row.id for row in before]  # no messages written

    # The refusal is on client B's audit trail, and B got no thread of its own.
    rows = await audit_rows(engine, client_b.id)
    assert [row.event_type for row in rows] == [AuditEventType.ERROR]
    assert rows[0].outcome is AuditOutcome.DENIED
    assert rows[0].error_type == "ConversationClientMismatch"
    async with session_maker() as session:
        threads_for_b = (
            await session.execute(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.client_id == client_b.id)
            )
        ).scalar_one()
    assert threads_for_b == 0


async def test_another_engineers_thread_is_refused(
    db_client: Client, settings: Settings, session_maker: Maker
) -> None:
    first = await ask(db_client, FakeAnthropic(says("ok")), settings, "hello")
    model = FakeAnthropic(says("never reached"))

    with pytest.raises(ConversationForbidden):
        await ask(
            db_client,
            model,
            settings,
            "mine now",
            user="engineer-2",
            conversation_id=first.conversation_id,
        )

    assert model.call_count == 0
    assert len(await stored(session_maker, first.conversation_id)) == 2


async def test_an_unknown_thread_is_refused(db_client: Client, settings: Settings) -> None:
    with pytest.raises(ConversationNotFound):
        await ask(db_client, FakeAnthropic(), settings, "hi", conversation_id=str(uuid.uuid4()))
    with pytest.raises(ConversationNotFound):
        await ask(db_client, FakeAnthropic(), settings, "hi", conversation_id="not-a-uuid")


# --- trimming in the loop -------------------------------------------------


async def seed_thread(maker: Maker, client: Client, turns: int, result_chars: int) -> uuid.UUID:
    """A long thread written straight to storage: `turns` tool round trips."""
    async with maker() as session:
        thread = await start_conversation(
            session,
            client_id=client.id,
            client_ref=client.slug,
            engineer_id=USER,
            interface=Interface.API,
        )
        for n in range(1, turns + 1):
            await append_turn(
                session,
                thread,
                [
                    TurnMessage("user", [{"type": "text", "text": f"question {n}"}]),
                    TurnMessage(
                        "assistant",
                        [
                            {
                                "type": "tool_use",
                                "id": f"toolu_{n}",
                                "name": "demo_list",
                                "input": {},
                            }
                        ],
                    ),
                    TurnMessage(
                        "user",
                        [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"toolu_{n}",
                                "content": "x" * result_chars,
                            }
                        ],
                    ),
                    TurnMessage("assistant", [{"type": "text", "text": f"answer {n}"}]),
                ],
            )
        await session.commit()
        return thread.id


async def test_a_long_thread_is_trimmed_for_the_model_but_not_in_storage(
    db_client: Client, settings: Settings, engine: Any, session_maker: Maker
) -> None:
    thread_id = await seed_thread(session_maker, db_client, turns=10, result_chars=48_000)
    model = FakeAnthropic(says("Still two devices."))

    await ask(db_client, model, settings, "and now?", conversation_id=thread_id)

    rows = await audit_rows(engine, db_client.id)
    trimmed = [row for row in rows if row.event_type is AuditEventType.CONTEXT_TRIMMED]
    assert len(trimmed) == 1
    payload = trimmed[0].payload
    assert payload["results_elided"] == 4 and payload["turns_dropped"] == 0
    assert payload["tokens_before"] > payload["budget"] >= payload["tokens_after"]
    assert payload["budget"] == 80_000

    sent = model.messages.requests[0]["messages"]
    bodies = [
        block["content"]
        for message in sent
        for block in (message["content"] if isinstance(message["content"], list) else [])
        if block.get("type") == "tool_result"
    ]
    assert bodies[:4] == ["[result elided, 48000 chars]"] * 4
    assert all(body == "x" * 48_000 for body in bodies[4:])  # the newest turns are intact
    assert len(bodies) == 10  # every tool_use kept its (possibly stubbed) result

    # Storage is untouched: the elision happened to the replayed copy only.
    kept = await stored(session_maker, thread_id)
    stored_bodies = [
        block["content"]
        for row in kept
        for block in row.content_json
        if block.get("type") == "tool_result"
    ]
    assert stored_bodies == ["x" * 48_000] * 10
    assert len(kept) == 42  # 40 seeded + this turn's question and answer


async def test_a_short_thread_is_not_trimmed(
    db_client: Client, settings: Settings, engine: Any, session_maker: Maker
) -> None:
    thread_id = await seed_thread(session_maker, db_client, turns=3, result_chars=200)
    await ask(db_client, FakeAnthropic(says("ok")), settings, "again?", conversation_id=thread_id)
    events = [row.event_type for row in await audit_rows(engine, db_client.id)]
    assert AuditEventType.CONTEXT_TRIMMED not in events


# --- turns that end badly still replay ------------------------------------


async def test_a_capped_turn_is_stored_without_a_dangling_tool_use(
    db_client: Client, settings: Settings, session_maker: Maker
) -> None:
    capped = settings.model_copy(update={"max_tool_calls": 1})
    model = FakeAnthropic(calls("demo_list"), calls("demo_alerts"), says("never reached"))

    result = await ask(db_client, model, capped, "keep going")
    assert result.status == STATUS_CAPPED

    rows = await stored(session_maker, result.conversation_id)
    assert unanswered_tool_uses(rows) == set()
    assert rows[-1].role.value == "assistant"
    assert "1 tool calls" in rows[-1].content_json[-1]["text"]  # what the engineer was shown

    # And the thread can be continued: roles alternate, every call is answered.
    follow_up = FakeAnthropic(says("Picking up where I stopped."))
    await ask(db_client, follow_up, settings, "go on", conversation_id=result.conversation_id)
    roles = [message["role"] for message in follow_up.messages.requests[0]["messages"]]
    assert all(a != b for a, b in itertools.pairwise(roles))


async def test_a_paused_search_turn_works_with_history_present(
    db_client: Client, settings: Settings, session_maker: Maker
) -> None:
    first = await ask(db_client, FakeAnthropic(says("PC-01 is offline.")), settings, "PC-01?")

    model = FakeAnthropic(
        searches("ninjaone agent offline causes", [KB], stop_reason="pause_turn"),
        says(
            "Per NinjaOne's KB, usually the agent service (https://ninjaone.example/kb/agent-offline)."
        ),
    )
    second = await ask(
        db_client, model, settings, "why would that be?", conversation_id=first.conversation_id
    )

    assert second.status == STATUS_OK and second.web_searches == 1
    assert model.call_count == 2
    # The resumed request carries the history, the question, then the paused turn.
    resumed = model.messages.requests[1]["messages"]
    assert [m["role"] for m in resumed] == ["user", "assistant", "user", "assistant"]

    rows = await stored(session_maker, first.conversation_id)
    # Paused and resumed halves are one assistant message, as the API treats them.
    assert [row.role.value for row in rows] == ["user", "assistant", "user", "assistant"]
    assert [block["type"] for block in rows[3].content_json] == [
        "server_tool_use",
        "web_search_tool_result",
        "text",
    ]


# --- archiving ------------------------------------------------------------


@pytest.fixture
def api(settings: Settings, session_maker: Maker, db_client: Client, db_user: User) -> FastAPI:
    application = create_app(settings)

    async def dependency() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    application.dependency_overrides[get_session] = dependency
    # Signed in as the committed engineer, on the committed client. That a
    # cookie really proves this is asserted in tests/api/test_authentication.py.
    sign_in(
        application,
        make_scope(
            user_id=db_user.id,
            email=db_user.email,
            client_id=db_client.id,
            client_slug=db_client.slug,
        ),
    )
    return application


@pytest.fixture
async def http(api: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://engine.test"
    ) as client:
        yield client


async def test_archiving_a_thread_deletes_no_audit_rows(
    db_client: Client,
    db_user: User,
    settings: Settings,
    engine: Any,
    session_maker: Maker,
    http: httpx.AsyncClient,
) -> None:
    result = await ask_as(
        db_user,
        db_client,
        FakeAnthropic(calls("demo_list"), says("Two.")),
        settings,
        "how many?",
    )
    before = await audit_rows(engine, db_client.id)

    response = await http.post(f"/conversations/{result.conversation_id}/archive", json={})
    assert response.status_code == 200
    assert response.json()["archived_at"] is not None

    after = await audit_rows(engine, db_client.id)
    # Every row that was there still is, unchanged, plus the archive itself.
    assert [row.id for row in after[: len(before)]] == [row.id for row in before]
    assert [row.payload for row in after[: len(before)]] == [row.payload for row in before]
    assert len(after) == len(before) + 1
    assert after[-1].event_type is AuditEventType.CONVERSATION_ARCHIVED
    assert after[-1].conversation_id == result.conversation_id
    # The messages are still there too: archived, not deleted.
    assert len(await stored(session_maker, result.conversation_id)) == 4

    # Archiving twice is harmless and writes nothing more.
    again = await http.post(f"/conversations/{result.conversation_id}/archive", json={})
    assert again.status_code == 200
    assert len(await audit_rows(engine, db_client.id)) == len(after)

    # An archived thread can be read but not continued, by its owner either.
    with pytest.raises(ConversationArchived):
        await ask_as(
            db_user,
            db_client,
            FakeAnthropic(),
            settings,
            "more?",
            conversation_id=result.conversation_id,
        )


async def test_the_database_refuses_to_delete_a_thread_that_has_audit_rows(
    db_client: Client, settings: Settings, session_maker: Maker
) -> None:
    """The foreign key is RESTRICT: there is no path from a thread to a lost audit row."""
    from sqlalchemy.exc import IntegrityError

    result = await ask(db_client, FakeAnthropic(says("ok")), settings, "hello")
    async with session_maker() as session:
        with pytest.raises(IntegrityError, match="fk_audit_log_conversation_id"):
            await session.execute(
                Conversation.__table__.delete().where(Conversation.id == result.conversation_id)
            )
        await session.rollback()
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.conversation_id == result.conversation_id)
            )
        ).scalar_one()
    assert remaining > 0


# --- the HTTP surface -----------------------------------------------------


async def test_threads_are_created_listed_newest_first_and_hidden_once_archived(
    db_client: Client,
    db_user: User,
    settings: Settings,
    http: httpx.AsyncClient,
    session_maker: Maker,
) -> None:
    created = await http.post("/conversations", json={})
    assert created.status_code == 201
    empty_thread = created.json()["id"]

    # A run with a tool call, so the turn count has to tell the engineer's
    # question from the tool result that is also stored as a user message.
    older = await ask_as(
        db_user, db_client, FakeAnthropic(calls("demo_list"), says("a")), settings, "first question"
    )
    newer = await ask_as(db_user, db_client, FakeAnthropic(says("b")), settings, "second question")
    # Another engineer's thread, which must not appear in this listing.
    await ask(db_client, FakeAnthropic(says("c")), settings, "not mine", user="engineer-2")

    listing = await http.get("/conversations")
    assert listing.status_code == 200
    assert [t["id"] for t in listing.json()] == [
        str(newer.conversation_id),
        str(older.conversation_id),
        empty_thread,
    ]
    assert listing.json()[0]["title"] == "second question"

    # How many questions have been asked in each, so the rail can tell a thread
    # with one from a thread with forty without opening either. A turn is a
    # question somebody asked: the older thread holds four messages, two of them
    # user messages, and only one of those is a question — the other is the
    # tool result, which the Anthropic format also sends as a user message.
    by_id = {thread["id"]: thread for thread in listing.json()}
    assert by_id[str(newer.conversation_id)]["turns"] == 1
    assert len(await stored(session_maker, older.conversation_id)) == 4
    assert by_id[str(older.conversation_id)]["turns"] == 1
    assert by_id[empty_thread]["turns"] == 0

    await http.post(f"/conversations/{newer.conversation_id}/archive", json={})
    listing = await http.get("/conversations")
    assert str(newer.conversation_id) not in [t["id"] for t in listing.json()]


async def test_messages_are_readable_by_their_owner_only(
    db_client: Client, db_user: User, settings: Settings, http: httpx.AsyncClient
) -> None:
    result = await ask_as(
        db_user,
        db_client,
        FakeAnthropic(calls("demo_list"), says("Two devices.")),
        settings,
        "how many?",
    )
    url = f"/conversations/{result.conversation_id}/messages"

    mine = await http.get(url)
    assert mine.status_code == 200
    body = mine.json()
    assert [(m["seq"], m["role"], m["text"]) for m in body] == [
        (1, "user", "how many?"),
        (2, "assistant", ""),
        (3, "user", ""),
        (4, "assistant", "Two devices."),
    ]
    assert body[1]["tools"] == ["demo_list"]
    # Every message says when it was written, so a thread reopened a week later
    # can say when each answer was given rather than only that Desk gave it.
    assert all(message["at"] for message in body)
    assert [message["at"] for message in body] == sorted(message["at"] for message in body)

    assert (await http.get(f"/conversations/{uuid.uuid4()}/messages")).status_code == 404

    # Somebody else's thread: the owner is an account, so this is now an
    # identity check rather than a comparison of typed handles.
    theirs = await ask(db_client, FakeAnthropic(says("x")), settings, "mine", user="engineer-2")
    assert (await http.get(f"/conversations/{theirs.conversation_id}/messages")).status_code == 403
    other = await http.post(f"/conversations/{theirs.conversation_id}/archive", json={})
    assert other.status_code == 403


async def test_chat_passes_the_thread_through_and_returns_its_id(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = uuid.uuid4()
    seen: dict[str, Any] = {}

    async def fake_run_agent(**kwargs: Any) -> AgentResult:
        seen.update(kwargs)
        return AgentResult(
            status="ok",
            text="ok",
            request_id=uuid.uuid4(),
            client_id=uuid.uuid4(),
            client_slug="teamlogic-austin",
            conversation_id=thread_id,
        )

    monkeypatch.setattr(chat_module, "run_agent", fake_run_agent)
    payload = {"message": "hi"}

    started = await http.post("/chat", json=payload)
    assert started.status_code == 200
    assert seen["conversation_id"] is None
    assert started.json()["conversation_id"] == str(thread_id)

    await http.post("/chat", json={**payload, "conversation_id": str(thread_id)})
    assert seen["conversation_id"] == str(thread_id)


async def test_chat_refuses_a_thread_from_another_client_with_403(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuses(**kwargs: Any) -> AgentResult:
        raise ConversationClientMismatch("this conversation belongs to a different client")

    monkeypatch.setattr(chat_module, "run_agent", refuses)
    response = await http.post(
        "/chat",
        json={"message": "hi", "conversation_id": str(uuid.uuid4())},
    )
    assert response.status_code == 403
    assert "different client" in response.json()["detail"]

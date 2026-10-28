"""POST /chat, POST /chat/stream and POST /feedback over HTTP.

`run_agent` is patched out for the routing tests: the loop itself is covered in
`test_loop.py` against a real database, and these need to assert what the
endpoint does with its result, including the paths that never reach the loop.

The caller is signed in with `tests/authfixtures.py`, which supplies a scope
rather than a cookie: who a session belongs to is proved in
`tests/api/test_authentication.py`, against a real database. What matters here
is that the route takes its client and its actor from the scope and not from
the body, and that a body which still names either is refused.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import app.api.chat as chat_module
from app.agent.conversations import ConversationForbidden
from app.agent.loop import AgentResult, ModelNotConfigured, ToolCallRecord
from app.connectors.errors import UnknownClient
from app.main import create_app
from app.settings import Settings
from tests.authfixtures import make_scope, sign_in

CLIENT_ID = uuid.uuid4()
SLUG = "teamlogic-austin"


def sample_result(**overrides: Any) -> AgentResult:
    result = AgentResult(
        status="ok",
        text="**PC-01** has not checked in for 9 days.",
        request_id=uuid.uuid4(),
        client_id=CLIENT_ID,
        client_slug="teamlogic-austin",
        audit_log_id=4321,
        tool_calls=[
            ToolCallRecord(
                tool_name="list_devices_not_checked_in",
                connector="ninjaone",
                arguments={"days": 7},
                outcome="success",
                duration_ms=412,
                result_summary="stale device count: 1",
            )
        ],
        model="claude-opus-5",
        input_tokens=1200,
        output_tokens=180,
        model_calls=2,
        duration_ms=3100,
    )
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    application = create_app(settings)
    # An Inputiv engineer, already switched to this client. Role-specific
    # behaviour is asserted in tests/api/test_authorization.py.
    sign_in(application, make_scope(client_id=CLIENT_ID, client_slug=SLUG))
    return application


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://engine.test"
    ) as client:
        yield client


# --- POST /chat -----------------------------------------------------------


async def test_chat_returns_the_agent_result(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_run_agent(**kwargs: Any) -> AgentResult:
        seen.update(kwargs)
        return sample_result()

    monkeypatch.setattr(chat_module, "run_agent", fake_run_agent)

    response = await http.post(
        "/chat",
        json={"message": "what is stale?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["audit_log_id"] == 4321
    assert body["client_slug"] == "teamlogic-austin"
    assert body["tool_calls"][0]["tool_name"] == "list_devices_not_checked_in"
    assert body["input_tokens"] == 1200

    # The actor and the client come from the session, not from the body.
    assert seen["client_id"] == CLIENT_ID
    assert seen["user_id"] == str(seen["actor_user_id"])
    assert seen["message"] == "what is stale?"


async def test_an_unresolvable_client_is_a_400(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run_agent(**kwargs: Any) -> AgentResult:
        raise UnknownClient("no client matches that client_id", connector="factory")

    monkeypatch.setattr(chat_module, "run_agent", fake_run_agent)

    response = await http.post(
        "/chat",
        json={"message": "hello"},
    )

    assert response.status_code == 400
    assert "no client matches" in response.json()["detail"]


async def test_a_capped_run_is_still_a_200_with_its_status(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap is an answer about what happened, not a transport failure."""

    async def fake_run_agent(**kwargs: Any) -> AgentResult:
        return sample_result(status="capped", text="I stopped after 8 tool calls.")

    monkeypatch.setattr(chat_module, "run_agent", fake_run_agent)

    response = await http.post(
        "/chat",
        json={"message": "everything"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "capped"


@pytest.mark.parametrize(
    "payload",
    [
        # Nothing at all.
        {},
        # An empty question.
        {"message": ""},
        # A question longer than the cap.
        {"message": "x" * 9000},
        # The two fields that used to choose a tenant and an actor. They are
        # refused rather than ignored, so a caller still sending one is told.
        {"message": "hi", "client_id": "x"},
        {"message": "hi", "user_id": "p"},
    ],
)
async def test_incomplete_requests_are_rejected(
    http: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    assert (await http.post("/chat", json=payload)).status_code == 422


# --- POST /chat/stream -------------------------------------------------------


def parse_events(body: str) -> list[tuple[str, Any]]:
    """Server-sent events as (name, data); comment lines are kept as (":", text)."""
    events: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            events.append((":", block[1:].strip()))
            continue
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def scripted_run(*, fail_with: Exception | None = None, pause: float = 0.0) -> Any:
    """A stand-in for `run_agent` that reports progress the way the loop does."""

    async def fake_run_agent(**kwargs: Any) -> AgentResult:
        async def nobody_listening(kind: str, data: dict[str, Any]) -> None:
            return None

        emit = kwargs.get("on_event") or nobody_listening  # POST /chat passes none
        await emit("started", {"request_id": "r-1", "conversation_id": "c-1"})
        if fail_with is not None:
            raise fail_with
        await emit("model_call", {"index": 1})
        call = {"id": "toolu_1", "tool_name": "list_devices_not_checked_in"}
        await emit("tool_start", {**call, "connector": "ninjaone", "arguments": {"days": 7}})
        if pause:
            await asyncio.sleep(pause)
        await emit("tool_end", {**call, "outcome": "success", "duration_ms": 412})
        await emit("model_call", {"index": 2})
        for piece in ("**PC-01** has not ", "checked in for 9 days."):
            await emit("text", {"text": piece})
        return sample_result()

    return fake_run_agent


STREAM_BODY = {"message": "what is stale?"}


async def test_the_stream_reports_tools_then_text_then_the_full_result(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_module, "run_agent", scripted_run())

    response = await http.post("/chat/stream", json=STREAM_BODY)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    events = parse_events(response.text)
    assert [name for name, _ in events] == [
        "started",
        "model_call",
        "tool_start",
        "tool_end",
        "model_call",
        "text",
        "text",
        "done",
    ]
    assert events[2][1]["arguments"] == {"days": 7}
    assert events[3][1]["id"] == events[2][1]["id"]
    streamed = "".join(data["text"] for name, data in events if name == "text")
    done = events[-1][1]
    assert streamed == done["text"]

    # `done` is exactly what POST /chat returns for the same run.
    plain = await http.post("/chat", json=STREAM_BODY)
    assert done == plain.json() | {"request_id": done["request_id"]}


async def test_a_refusal_before_the_run_keeps_its_status_code(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run_agent(**kwargs: Any) -> AgentResult:
        raise UnknownClient("no client matches that client_id", connector="factory")

    monkeypatch.setattr(chat_module, "run_agent", fake_run_agent)

    response = await http.post("/chat/stream", json=STREAM_BODY)

    assert response.status_code == 400
    assert "no client matches" in response.json()["detail"]


async def test_a_thread_that_is_not_yours_is_a_403_not_an_event(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run_agent(**kwargs: Any) -> AgentResult:
        raise ConversationForbidden("this conversation belongs to another client")

    monkeypatch.setattr(chat_module, "run_agent", fake_run_agent)

    response = await http.post("/chat/stream", json=STREAM_BODY | {"conversation_id": "x"})

    assert response.status_code == 403


async def test_a_failure_after_the_run_started_arrives_as_an_error_event(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        chat_module,
        "run_agent",
        scripted_run(fail_with=ModelNotConfigured("ANTHROPIC_API_KEY is not set")),
    )

    response = await http.post("/chat/stream", json=STREAM_BODY)

    events = parse_events(response.text)
    assert [name for name, _ in events] == ["started", "error"]
    assert events[-1][1] == {"status": 503, "detail": "ANTHROPIC_API_KEY is not set"}


async def test_an_unexpected_failure_says_nothing_about_its_cause(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        chat_module, "run_agent", scripted_run(fail_with=RuntimeError("secret detail"))
    )

    response = await http.post("/chat/stream", json=STREAM_BODY)

    assert "secret detail" not in response.text
    assert parse_events(response.text)[-1][1]["status"] == 500


async def test_a_quiet_stream_is_kept_alive(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_module, "STREAM_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(chat_module, "run_agent", scripted_run(pause=0.08))

    response = await http.post("/chat/stream", json=STREAM_BODY)

    events = parse_events(response.text)
    assert (":", "keep-alive") in events
    assert events[-1][0] == "done"  # and the comment lines disturbed nothing


async def test_a_listener_that_leaves_does_not_cancel_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run is audited and saved to the thread whether or not anyone is watching."""
    finished = asyncio.Event()
    inner = scripted_run(pause=0.05)

    async def fake_run_agent(**kwargs: Any) -> AgentResult:
        result = await inner(**kwargs)
        finished.set()
        return result

    monkeypatch.setattr(chat_module, "run_agent", fake_run_agent)

    response = await chat_module.chat_stream(
        chat_module.ChatRequest(**STREAM_BODY),
        make_scope(client_id=CLIENT_ID, client_slug=SLUG),
    )
    body = response.body_iterator
    first = await anext(aiter(body))
    assert str(first).startswith("event: started")
    await body.aclose()  # type: ignore[attr-defined]  # the browser tab is closed

    await asyncio.wait_for(finished.wait(), timeout=2)


# --- POST /feedback -------------------------------------------------------


async def test_feedback_records_a_rating(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake_record(audit_log_id: int, rating: str, **kwargs: Any) -> bool:
        seen.update({"audit_log_id": audit_log_id, "rating": rating, **kwargs})
        return True

    monkeypatch.setattr(chat_module, "record_feedback", fake_record)

    response = await http.post(
        "/feedback",
        json={"audit_log_id": 4321, "rating": "down", "comment": "wrong"},
    )

    assert response.status_code == 200
    assert response.json() == {"recorded": True, "audit_log_id": 4321}
    # Who rated it is the signed-in account, not a name the body supplied.
    assert seen["audit_log_id"] == 4321
    assert seen["rating"] == "down"
    assert seen["comment"] == "wrong"
    assert uuid.UUID(seen["rated_by"])


async def test_feedback_on_an_unknown_row_is_a_404(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_record(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(chat_module, "record_feedback", fake_record)

    response = await http.post("/feedback", json={"audit_log_id": 999, "rating": "up"})

    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"audit_log_id": 1, "rating": "sideways"},
        {"audit_log_id": 0, "rating": "up"},
        {"rating": "up"},
    ],
)
async def test_malformed_feedback_is_rejected(
    http: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    assert (await http.post("/feedback", json=payload)).status_code == 422

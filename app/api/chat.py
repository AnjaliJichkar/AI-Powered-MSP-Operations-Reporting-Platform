"""POST /chat, POST /chat/stream, POST /feedback.

Who is asking and which client they are asking about are both resolved from the
session (`app/auth/dependencies.py`), never from the body. The request carries
the question and nothing that names a tenant: `extra=forbid` means a caller
still sending `client_id` or `user_id` is told so rather than quietly ignored.

The client list moved to `GET /auth/clients`, which shows a franchisee user
exactly their own client and Inputiv staff the ones they may switch to.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agent.conversations import ConversationError
from app.agent.loop import AgentResult, ModelNotConfigured, record_feedback, run_agent
from app.api.conversations import http_error
from app.auth.dependencies import CAN_CHAT
from app.auth.scope import Scope
from app.connectors.errors import UnknownClient
from app.models.enums import Interface

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    """One question. Who is asking, and about whom, are not in here.

    Unknown fields are refused rather than ignored, so a caller that still
    sends `client_id` or `user_id` gets a 422 naming the field instead of
    believing it chose a tenant.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(
        default=None,
        description=(
            "The thread to continue. Omit to start one; its id comes back in the response. "
            "It must belong to the signed-in user and to the client this session is on."
        ),
    )


class ToolCallView(BaseModel):
    tool_name: str
    connector: str
    arguments: dict[str, Any]
    outcome: str
    duration_ms: int
    error_type: str | None = None
    error_message: str | None = None
    result_summary: str | None = None


class ChatResponse(BaseModel):
    status: Literal["ok", "capped", "truncated", "timeout", "refused", "error"]
    text: str
    request_id: str
    client_id: str
    client_slug: str
    audit_log_id: int | None
    tool_calls: list[ToolCallView]
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    model_calls: int
    duration_ms: int
    conversation_id: str | None = None

    @classmethod
    def from_result(cls, result: AgentResult) -> ChatResponse:
        return cls.model_validate(result.as_dict())


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_log_id: int = Field(gt=0)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackResponse(BaseModel):
    recorded: bool
    audit_log_id: int


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, scope: Annotated[Scope, Depends(CAN_CHAT)]) -> ChatResponse:
    """Answer one question about the client this session is on.

    The client comes from the session: a franchisee user's own, or the one
    Inputiv staff last switched to. A viewer never reaches here, because
    running a model against the customer's data is not a read.
    """
    try:
        result = await run_agent(
            user_id=scope.actor_id,
            client_id=scope.require_client(),
            message=request.message,
            interface=Interface.API,
            conversation_id=request.conversation_id,
            actor_user_id=scope.user_id,
        )
    except UnknownClient as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ConversationError as exc:
        # A thread from another client or another engineer is refused outright
        # (403), never quietly swapped for a fresh one.
        raise http_error(exc) from exc
    except ModelNotConfigured as exc:
        # A deployment fault, not a bad question: say so plainly rather than
        # letting it surface as an unexplained 500.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return ChatResponse.from_result(result)


# --- the same question, with progress ------------------------------------------

#: Seconds of silence before a comment line is sent, so a proxy or browser does
#: not drop a connection that is merely waiting on a slow vendor.
STREAM_HEARTBEAT_SECONDS = 15.0

#: Runs in flight. A task nobody holds can be garbage-collected mid-run.
_RUNS: set[asyncio.Task[None]] = set()


def sse(event: str, data: dict[str, Any]) -> str:
    """One server-sent event. JSON never contains a raw newline, so one `data:` line."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest, scope: Annotated[Scope, Depends(CAN_CHAT)]
) -> StreamingResponse:
    """`POST /chat` as server-sent events, for an interface that renders progressively.

    Same request body, same run (`run_agent`), same audit rows. Events, each
    with a JSON body:

    - `started`: `request_id`, `conversation_id`, `model`, `prompt_version`
    - `model_call`: `index` — the model is being asked (again, after tools)
    - `text`: `text` — a piece of the model's text, as it is written
    - `tool_start`: `id`, `tool_name`, `connector`, `arguments`
    - `tool_end`: the same `id` plus the `ToolCallView` fields
    - `web_search`: `query`
    - `done`: the complete `ChatResponse`, exactly what `POST /chat` returns.
      Its `text` is the answer of record; streamed text is a preview of it.
    - `error`: `status`, `detail` — the run could not complete

    Anything that would be refused before a model call — an unknown client, a
    thread that is not this client's or this engineer's — is still a plain
    HTTP 400/403/404, not an event: the response does not start until the run
    has. If the listener goes away the run is **not** cancelled. It finishes,
    is audited and is saved to the thread, as if nobody had been watching.
    """
    client_id = scope.require_client()
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def sink(kind: str, data: dict[str, Any]) -> None:
        await queue.put((kind, data))

    async def run() -> None:
        try:
            result = await run_agent(
                user_id=scope.actor_id,
                client_id=client_id,
                message=request.message,
                interface=Interface.API,
                conversation_id=request.conversation_id,
                actor_user_id=scope.user_id,
                on_event=sink,
            )
            await queue.put(("done", ChatResponse.from_result(result).model_dump()))
        except UnknownClient as exc:
            await queue.put(("error", {"status": 400, "detail": str(exc)}))
        except ConversationError as exc:
            refusal = http_error(exc)
            await queue.put(("error", {"status": refusal.status_code, "detail": refusal.detail}))
        except ModelNotConfigured as exc:
            await queue.put(("error", {"status": 503, "detail": str(exc)}))
        except Exception:
            logger.exception("streamed run failed")
            await queue.put(
                ("error", {"status": 500, "detail": "The engine failed to complete this run."})
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    _RUNS.add(task)
    task.add_done_callback(_RUNS.discard)

    # Hold the response until the run has either started or been refused, so a
    # refusal keeps its status code instead of arriving inside a 200.
    first = await queue.get()
    if first is not None and first[0] == "error" and first[1]["status"] < 500:
        raise HTTPException(status_code=first[1]["status"], detail=first[1]["detail"])

    async def events() -> AsyncIterator[str]:
        item = first
        while item is not None:
            yield sse(*item)
            if item[0] in ("done", "error"):
                return
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), STREAM_HEARTBEAT_SECONDS)
                    break
                except TimeoutError:
                    yield ": keep-alive\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx would otherwise hold the events back
        },
    )


@router.post("/feedback", response_model=FeedbackResponse)
async def feedback(
    request: FeedbackRequest, scope: Annotated[Scope, Depends(CAN_CHAT)]
) -> FeedbackResponse:
    """Record the signed-in user's verdict on one answer."""
    recorded = await record_feedback(
        request.audit_log_id,
        request.rating,
        rated_by=scope.actor_id,
        comment=request.comment,
    )
    if not recorded:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no audit_log row with id {request.audit_log_id}",
        )
    return FeedbackResponse(recorded=True, audit_log_id=request.audit_log_id)

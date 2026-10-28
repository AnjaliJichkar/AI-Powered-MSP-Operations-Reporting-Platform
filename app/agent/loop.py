"""The Claude tool-call loop.

`run_agent` takes a question, resolves the client, lets the model call read-only
tools until it can answer, and returns the answer with the audit trail id.

Three invariants hold no matter what the model does:

1. **The client is chosen by the caller, never the model.** `client_id` is
   stripped from every tool schema before the model sees it and injected
   server-side on every call. Even a model that hallucinates the parameter
   cannot reach another client's data.
2. **Every run is bounded.** `MAX_TOOL_CALLS`, `TOOL_TIMEOUT_SECONDS` and
   `AGENT_TIMEOUT_SECONDS` come from settings and are enforced here.
3. **Every step is audited.** Prompt, each model call, each tool call and
   result, and the final answer, with timing and token counts.

A connector failure is not a run failure: the normalised error goes back to the
model as a tool result so it can tell the engineer what broke (hard rule 7).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import (
    Message,
    MessageParam,
    OutputConfigParam,
    ServerToolUseBlock,
    TextBlock,
    TextBlockParam,
    ToolParam,
    ToolUseBlock,
    WebSearchToolResultBlock,
    WebSearchToolResultError,
)
from pydantic import ValidationError

from app.agent.conversations import (
    ConversationError,
    TurnMessage,
    append_turn,
    assemble_messages,
    check_access,
    dump_blocks,
    finalise_turn,
    get_conversation,
    load_history,
    start_conversation,
    title_from,
    trim_history,
)
from app.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION, build_client_context
from app.agent.registry import (
    SITE_ID_PARAMETER,
    SITE_PARAMETER,
    ToolArgumentError,
    ToolDefinition,
    get_tool,
    registered_tools,
)
from app.audit.recorder import AuditContext, record_event
from app.connectors.errors import ConnectorError, UnknownClient
from app.connectors.factory import list_client_sites, resolve_client, site_choices
from app.db.session import session_scope
from app.models.enums import AuditEventType, AuditOutcome, Interface
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Where a caller hears about a run while it is still going: `(kind, data)`.
#: Kinds: `started`, `model_call`, `text`, `tool_start`, `tool_end`, `web_search`.
#: This is progress for a person watching, not a record: the audit log is
#: written exactly as it is without a sink, and nothing here feeds back into
#: the run. See `app/api/chat.py` for the server-sent-events endpoint.
EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _emit(sink: EventSink | None, kind: str, data: dict[str, Any]) -> None:
    """Tell the listener, if there is one. A listener that fails never fails the run."""
    if sink is None:
        return
    try:
        await sink(kind, data)
    except Exception:
        logger.exception("progress listener failed on %r; the run continues", kind)


#: How the run ended.
STATUS_OK = "ok"
STATUS_CAPPED = "capped"
STATUS_TRUNCATED = "truncated"
STATUS_TIMEOUT = "timeout"
STATUS_REFUSED = "refused"
STATUS_ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call, as it will be shown to the engineer."""

    tool_name: str
    connector: str
    arguments: dict[str, Any]
    outcome: str
    duration_ms: int
    error_type: str | None = None
    error_message: str | None = None
    result_summary: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "connector": self.connector,
            "arguments": self.arguments,
            "outcome": self.outcome,
            "duration_ms": self.duration_ms,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "result_summary": self.result_summary,
        }


@dataclass(slots=True)
class AgentResult:
    """What one run produced."""

    status: str
    text: str
    request_id: uuid.UUID
    client_id: uuid.UUID
    client_slug: str
    audit_log_id: int | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    model: str = ""
    prompt_version: str = SYSTEM_PROMPT_VERSION
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    """Input read from the prompt cache. Not included in `input_tokens`: the API
    reports them apart, so what the model read is the sum of the two and
    `cache_write_tokens`."""
    cache_write_tokens: int = 0
    model_calls: int = 0
    duration_ms: int = 0
    stop_reason: str | None = None
    """Why the last model call stopped, as the API reported it."""
    web_searches: int = 0
    """How many web searches the model made. Each one is its own audit row."""
    conversation_id: uuid.UUID | None = None
    """The thread this turn was appended to. Send it back to continue the thread."""
    first_tools: list[str] = field(default_factory=list)
    """The tools the model asked for in its first response that asked for any, in
    order, `web_search` included — whether or not they then ran. This is the
    model's tool *selection*, which is what `scripts/selection_check.py` scores."""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "text": self.text,
            "request_id": str(self.request_id),
            "client_id": str(self.client_id),
            "client_slug": self.client_slug,
            "audit_log_id": self.audit_log_id,
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "model": self.model,
            "prompt_version": self.prompt_version,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "model_calls": self.model_calls,
            "duration_ms": self.duration_ms,
            "stop_reason": self.stop_reason,
            "web_searches": self.web_searches,
            "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            "first_tools": list(self.first_tools),
        }


class ModelNotConfigured(RuntimeError):
    """No usable Anthropic credential, so no question can be answered."""


@lru_cache(maxsize=1)
def get_model_client() -> AsyncAnthropic:
    """The Anthropic client. Cached per process; tests pass their own."""
    settings = get_settings()
    key = settings.anthropic_api_key
    # An empty value counts as unset: `ANTHROPIC_API_KEY=` in a .env parses to
    # an empty SecretStr, which would otherwise fail later as a puzzling 401.
    if key is None or not key.get_secret_value().strip():
        raise ModelNotConfigured(
            "ANTHROPIC_API_KEY is not set, so the agent cannot call the model."
        )
    return AsyncAnthropic(
        api_key=key.get_secret_value(),
        max_retries=settings.anthropic_max_retries,
    )


# --- audit helpers --------------------------------------------------------


async def _record(
    context: AuditContext,
    *,
    event_type: AuditEventType,
    outcome: AuditOutcome,
    **fields: Any,
) -> int:
    """Write one audit row in its own transaction and return its id.

    A short transaction per event, rather than one held open for the whole run:
    a model call can take a minute and must not hold a database connection.
    """
    async with session_scope() as session:
        entry = await record_event(
            session, context, event_type=event_type, outcome=outcome, **fields
        )
        await session.flush()
        return entry.id


def _summarise_result(payload: Any) -> str:
    """A one-line description of a tool result, for the audit row and the UI."""
    if isinstance(payload, dict):
        extra = " (truncated)" if payload.get("truncated") else ""
        if "returned" in payload and "total" in payload:
            return f"{payload['returned']} of {payload['total']} rows{extra}"
        if "counts" in payload and "total" in payload:
            groups = len(payload["counts"]) if isinstance(payload["counts"], dict) else 0
            return f"total: {payload['total']} across {groups} group(s){extra}"
        for key in ("device_count", "alert_count", "stale_device_count", "ticket_count"):
            if key in payload:
                return f"{key.replace('_', ' ')}: {payload[key]}{extra}"
        return ", ".join(sorted(payload)[:6])
    return type(payload).__name__


def _serialise(result: Any) -> tuple[str, Any]:
    """Render a tool's return value as JSON for the model."""
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
    else:
        payload = result
    return json.dumps(payload, default=str), payload


# --- web search -----------------------------------------------------------

#: The server tool's name, as the API reports it on a `server_tool_use` block.
WEB_SEARCH_TOOL_NAME = "web_search"

#: How many times in a row the API may pause a long server-tool turn before we
#: stop resuming it. The overall agent timeout still applies.
MAX_PAUSED_TURNS = 5


def web_search_tool(settings: Settings) -> dict[str, Any] | None:
    """The server-side web search tool definition, or None when it is switched off.

    Anthropic runs the search; nothing here fetches a URL. The limits are
    settings (hard rule 6): how many searches one question may make, and
    optionally which domains results may come from.
    """
    if not settings.web_search_enabled:
        return None
    definition: dict[str, Any] = {
        "type": "web_search_20250305",
        "name": WEB_SEARCH_TOOL_NAME,
        "max_uses": settings.web_search_max_uses,
    }
    domains = settings.web_search_domains
    if domains:
        definition["allowed_domains"] = domains
    return definition


async def _record_web_searches(
    response: Message, context: AuditContext, result: AgentResult, *, model: str
) -> list[str]:
    """Audit every search in this response: the query, and the URLs returned.

    The search ran on Anthropic's side, so this is the only place the engine
    sees it. One row per search (hard rule 4): what the model asked the web,
    and exactly which pages it was handed, so an answer citing the web can be
    checked against what it actually read. The page text itself is encrypted
    in the API response and is not stored.
    """
    results_by_use: dict[str, WebSearchToolResultBlock] = {
        block.tool_use_id: block
        for block in response.content
        if isinstance(block, WebSearchToolResultBlock)
    }
    queries: list[str] = []
    for block in response.content:
        if not isinstance(block, ServerToolUseBlock) or block.name != WEB_SEARCH_TOOL_NAME:
            continue
        query = str(block.input.get("query") or "")
        queries.append(query)
        result.web_searches += 1

        outcome = AuditOutcome.SUCCESS
        error_type: str | None = None
        urls: list[dict[str, Any]] = []
        found = results_by_use.get(block.id)
        if found is None:
            outcome = AuditOutcome.ERROR
            error_type = "no_result_block"
        elif isinstance(found.content, WebSearchToolResultError):
            outcome = AuditOutcome.ERROR
            error_type = str(found.content.error_code)
        else:
            urls = [
                {"url": item.url, "title": item.title, "page_age": item.page_age}
                for item in found.content
            ]

        await _record(
            context,
            event_type=AuditEventType.WEB_SEARCH,
            outcome=outcome,
            tool_name=WEB_SEARCH_TOOL_NAME,
            model=model,
            prompt_version=SYSTEM_PROMPT_VERSION,
            error_type=error_type,
            error_message=(
                f"web search failed: {error_type}" if outcome is AuditOutcome.ERROR else None
            ),
            payload={"query": query, "result_count": len(urls), "urls": urls},
        )
    return queries


# --- tool execution -------------------------------------------------------


async def _run_one_tool(
    definition: ToolDefinition,
    raw_arguments: dict[str, Any],
    client_id: uuid.UUID,
    *,
    timeout_seconds: float,
    sites: Mapping[str, uuid.UUID] | None = None,
) -> tuple[str, bool, ToolCallRecord]:
    """Execute one tool call. Returns `(content_for_model, is_error, record)`.

    Never raises for an expected failure: the model is told what went wrong so
    it can pass that on to the engineer.
    """
    # Hard rule 1. The model is not shown `client_id`, but if it invents one we
    # drop it rather than trust it, and always substitute the resolved client.
    model_supplied = raw_arguments.pop("client_id", None)
    if model_supplied is not None:
        logger.warning(
            "model supplied client_id=%r for tool %s; overriding with the resolved client",
            model_supplied,
            definition.name,
        )
    # Nor is it shown `site_id`. It names a site by the slug it read in the
    # prompt; we resolve that within this client, and only this client.
    raw_arguments.pop(SITE_ID_PARAMETER, None)
    site_slug = raw_arguments.pop(SITE_PARAMETER, None)
    arguments = {**raw_arguments, "client_id": str(client_id)}

    known_sites = dict(sites or {})
    if site_slug is not None:
        resolved = known_sites.get(str(site_slug).strip().lower())
        if resolved is None:
            # A slug the model invented, or one belonging to another client.
            # Refused here, so the factory never sees it and nothing is called.
            available = ", ".join(sorted(known_sites)) or "none on file"
            message = (
                f"{str(site_slug)!r} is not a site of this client. Its sites are: "
                f"{available}. Use one of those, or omit the site to cover them all."
            )
            return (
                message,
                True,
                ToolCallRecord(
                    tool_name=definition.name,
                    connector=definition.connector.value,
                    arguments={SITE_PARAMETER: str(site_slug)},
                    outcome="unknown_site",
                    duration_ms=0,
                    error_type="UnknownSite",
                    error_message=message,
                ),
            )
        arguments[SITE_ID_PARAMETER] = str(resolved)

    started = time.perf_counter()

    def _elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    def _visible(args: dict[str, Any]) -> dict[str, Any]:
        # What the engineer sees: no client id, and no filter that was left unset.
        shown = {
            key: value for key, value in args.items() if key != "client_id" and value is not None
        }
        if SITE_ID_PARAMETER in shown:
            shown.pop(SITE_ID_PARAMETER)
            if site_slug is not None:
                shown[SITE_PARAMETER] = str(site_slug)
        return shown

    try:
        validated = definition.validate_arguments(arguments)
    except ToolArgumentError as exc:
        record = ToolCallRecord(
            tool_name=definition.name,
            connector=definition.connector.value,
            arguments=_visible(raw_arguments),
            outcome="invalid_arguments",
            duration_ms=_elapsed(),
            error_type="ToolArgumentError",
            error_message=str(exc),
        )
        return str(exc), True, record

    try:
        async with asyncio.timeout(timeout_seconds):
            result = await definition.func(**validated)
    except TimeoutError:
        message = (
            f"{definition.name} did not return within {timeout_seconds:g}s and was "
            "abandoned. Tell the user the check timed out; do not guess the answer."
        )
        record = ToolCallRecord(
            tool_name=definition.name,
            connector=definition.connector.value,
            arguments=_visible(validated),
            outcome="timeout",
            duration_ms=_elapsed(),
            error_type="TimeoutError",
            error_message=message,
        )
        return message, True, record
    except ConnectorError as exc:
        # Normalised and handed back so the model can explain it (hard rule 7).
        message = json.dumps({"error": exc.as_dict()}, default=str)
        record = ToolCallRecord(
            tool_name=definition.name,
            connector=definition.connector.value,
            arguments=_visible(validated),
            outcome="error",
            duration_ms=_elapsed(),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return message, True, record
    except Exception as exc:  # a bug in one tool must not lose the whole run
        # A tool refusing its own arguments ("severity must be one of ...", "days
        # must be at least 1") is not a bug: the message is written for the model,
        # so it goes back as a correctable error, like a schema failure does. A
        # pydantic ValidationError is also a ValueError, but there it is the
        # *vendor's response* that failed to parse, which the model cannot fix.
        if isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
            message = f"invalid arguments for {definition.name}: {exc}"
            record = ToolCallRecord(
                tool_name=definition.name,
                connector=definition.connector.value,
                arguments=_visible(validated),
                outcome="invalid_arguments",
                duration_ms=_elapsed(),
                error_type="ValueError",
                error_message=str(exc),
            )
            return message, True, record
        logger.exception("tool %s raised an unexpected error", definition.name)
        message = (
            f"{definition.name} failed unexpectedly ({type(exc).__name__}). Tell the "
            "user the check could not be completed."
        )
        record = ToolCallRecord(
            tool_name=definition.name,
            connector=definition.connector.value,
            arguments=_visible(validated),
            outcome="error",
            duration_ms=_elapsed(),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return message, True, record

    content, payload = _serialise(result)
    record = ToolCallRecord(
        tool_name=definition.name,
        connector=definition.connector.value,
        arguments=_visible(validated),
        outcome="success",
        duration_ms=_elapsed(),
        result_summary=_summarise_result(payload),
    )
    return content, False, record


# --- the loop -------------------------------------------------------------


def _text_of(message: Message) -> str:
    """Only the visible answer. Thinking blocks are never shown to a user."""
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    ).strip()


def _requested_tools(message: Message) -> list[str]:
    """Every tool this response asked for, ours and server-side, in order."""
    return [
        str(getattr(block, "name", ""))
        for block in message.content
        if getattr(block, "type", None) in ("tool_use", "server_tool_use")
    ]


def _tool_uses(message: Message) -> list[ToolUseBlock]:
    return [block for block in message.content if isinstance(block, ToolUseBlock)]


async def run_agent(
    user_id: str,
    client_id: str | uuid.UUID,
    message: str,
    interface: Interface = Interface.API,
    *,
    conversation_id: str | uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    model_client: AsyncAnthropic | None = None,
    settings: Settings | None = None,
    select_only: bool = False,
    on_event: EventSink | None = None,
) -> AgentResult:
    """Answer one question about one client, as one turn of a conversation thread.

    Without `conversation_id` a new thread is started for the client. With one,
    the thread's history is replayed to the model ahead of the question — and
    the thread must belong to the client this request is scoped to and to the
    engineer asking, or the call fails before any model call and nothing is
    written to the thread.

    `actor_user_id` is the authenticated account behind `user_id`, when there is
    one. The HTTP entry points always pass it and pass the user's id as
    `user_id` with it; the CLI and the selection harness pass neither, so their
    rows carry the operator handle and no account, exactly as they did before.
    A thread is owned by the account when one is given and by the handle when
    not, and the two are never compared with each other.

    `select_only` is for the tool-selection harness: the run stops as soon as the
    model has said which tools it wants, and **no tool is executed** — no vendor
    is called. Everything up to that point is the real thing (same prompt, same
    tool list, same client context) and is audited as usual.

    `on_event` receives progress while the run is going — each tool call as it
    starts and finishes, and the model's text as it arrives (the model call is
    streamed when a listener is given). It changes nothing about the run: same
    tools, same caps, same audit rows, same result.

    Raises `UnknownClient` if `client_id` does not resolve to an active client,
    and a `ConversationError` if the thread cannot be used. Every other failure
    is reported in the returned `AgentResult`.
    """
    settings = settings or get_settings()
    context = AuditContext.new(
        actor_id=user_id,
        interface=interface,
        actor_user_id=actor_user_id,
        client_ref=str(client_id),
    )
    run_started = time.perf_counter()

    # Resolve the client first. A question we cannot scope is never sent to the
    # model, and the refusal is still audited.
    try:
        async with session_scope() as session:
            client = await resolve_client(session, client_id)
            client_uuid, client_slug, client_name = client.id, client.slug, client.name
            client_sites = await list_client_sites(session, client.id)
    except UnknownClient as exc:
        await _record(
            context,
            event_type=AuditEventType.ERROR,
            outcome=AuditOutcome.DENIED,
            error_type=type(exc).__name__,
            error_message=str(exc),
            payload={"message": message},
        )
        raise

    # Names and slugs reach the prompt; ids stay here (hard rule 1).
    site_pairs = site_choices(client_sites)
    site_ids = {site.slug: site.id for site in client_sites}

    context = AuditContext(
        request_id=context.request_id,
        actor_id=user_id,
        interface=interface,
        actor_user_id=actor_user_id,
        client_id=client_uuid,
        client_ref=client_slug,
    )

    # The thread. Checked against the *resolved* client, which the caller chose
    # and the model never sees: a thread from client A cannot be replayed into a
    # request scoped to client B. This is hard rule 1, so it raises rather than
    # falling back to a fresh thread, and it happens before any model call.
    try:
        async with session_scope() as session:
            if conversation_id is None:
                thread = await start_conversation(
                    session,
                    client_id=client_uuid,
                    client_ref=client_slug,
                    engineer_id=user_id,
                    interface=interface,
                    owner_user_id=actor_user_id,
                    title=title_from(message),
                    request_id=context.request_id,
                )
                history: list[dict[str, Any]] = []
            else:
                thread = await get_conversation(session, conversation_id)
                check_access(
                    thread,
                    client_id=client_uuid,
                    engineer_id=None if actor_user_id else user_id,
                    owner_user_id=actor_user_id,
                    for_writing=True,
                )
                history = await load_history(session, thread.id)
            thread_id = thread.id
    except ConversationError as exc:
        await _record(
            context,
            event_type=AuditEventType.ERROR,
            outcome=AuditOutcome.DENIED,
            error_type=type(exc).__name__,
            error_message=str(exc),
            payload={"message": message, "conversation_ref": str(conversation_id)},
        )
        raise

    context = dataclasses.replace(context, conversation_id=thread_id)
    result = AgentResult(
        status=STATUS_OK,
        text="",
        request_id=context.request_id,
        client_id=client_uuid,
        client_slug=client_slug,
        model=settings.anthropic_model,
        conversation_id=thread_id,
    )

    await _emit(
        on_event,
        "started",
        {
            "request_id": str(context.request_id),
            "conversation_id": str(thread_id),
            "client_slug": client_slug,
            "model": settings.anthropic_model,
            "prompt_version": SYSTEM_PROMPT_VERSION,
        },
    )

    await _record(
        context,
        event_type=AuditEventType.PROMPT,
        outcome=AuditOutcome.SUCCESS,
        model=settings.anthropic_model,
        prompt_version=SYSTEM_PROMPT_VERSION,
        payload={"message": message, "interface": interface.value},
    )

    tools = [definition.to_anthropic_schema() for definition in registered_tools()]
    # Server-side and optional: the tenant tools above are scoped to this
    # client, the web is not, and the prompt says which is for what.
    search_tool = web_search_tool(settings)
    if search_tool is not None:
        tools.append(search_tool)
    # A long thread is cut to the context budget before the model sees it. Only
    # the replayed copy is trimmed; what is stored is never edited.
    history, trim = trim_history(
        history,
        budget=settings.conversation_context_budget,
        keep_recent_turns=settings.conversation_keep_recent_turns,
    )
    if trim.trimmed:
        await _record(
            context,
            event_type=AuditEventType.CONTEXT_TRIMMED,
            outcome=AuditOutcome.SUCCESS,
            payload={**trim.as_payload(), "budget": settings.conversation_context_budget},
        )

    # The system prompt is not part of a thread: whatever version is current
    # applies to every turn, old and new.
    conversation = assemble_messages(
        history,
        client_context=build_client_context(client_slug, client_name, site_pairs),
        question=message,
    )
    # What this turn adds, for the thread. Kept apart from `conversation`, which
    # also holds the replayed history and SDK objects the API hands back.
    transcript: list[TurnMessage] = []
    client_api = model_client or get_model_client()

    try:
        async with asyncio.timeout(settings.agent_timeout_seconds):
            await _converse(
                client_api,
                conversation,
                tools,
                context=context,
                settings=settings,
                result=result,
                client_uuid=client_uuid,
                sites=site_ids,
                transcript=transcript,
                select_only=select_only,
                on_event=on_event,
            )
    except TimeoutError:
        result.status = STATUS_TIMEOUT
        result.text = _partial_answer(
            result,
            f"I ran out of time after {settings.agent_timeout_seconds:g} seconds and "
            "stopped before I could finish.",
        )
    except anthropic.APIStatusError as exc:
        logger.exception("model call failed")
        result.status = STATUS_ERROR
        result.text = (
            f"I could not reach the model ({type(exc).__name__}, HTTP {exc.status_code}), "
            "so I have no answer for you. Nothing was changed."
        )
    except anthropic.APIConnectionError as exc:
        logger.exception("model connection failed")
        result.status = STATUS_ERROR
        result.text = (
            f"I could not reach the model ({type(exc).__name__}), so I have no answer "
            "for you. Nothing was changed."
        )

    result.duration_ms = int((time.perf_counter() - run_started) * 1000)

    # Persist the turn however it ended: the engineer saw the question and an
    # answer, so the thread holds both. `finalise_turn` repairs a turn that
    # stopped mid-flight, so a later replay never carries a `tool_use` without
    # its `tool_result`. A failure here must not cost the engineer an answer
    # that was already paid for, so it is logged rather than raised.
    try:
        async with session_scope() as session:
            thread = await get_conversation(session, thread_id)
            await append_turn(session, thread, finalise_turn(message, transcript, result.text))
    except Exception:
        logger.exception("could not persist this turn to conversation %s", thread_id)

    outcome = {
        STATUS_OK: AuditOutcome.SUCCESS,
        STATUS_CAPPED: AuditOutcome.CAPPED,
        STATUS_TRUNCATED: AuditOutcome.CAPPED,
        STATUS_TIMEOUT: AuditOutcome.TIMEOUT,
        STATUS_REFUSED: AuditOutcome.DENIED,
        STATUS_ERROR: AuditOutcome.ERROR,
    }[result.status]

    result.audit_log_id = await _record(
        context,
        event_type=AuditEventType.RUN_FINISHED,
        outcome=outcome,
        model=result.model,
        prompt_version=SYSTEM_PROMPT_VERSION,
        duration_ms=result.duration_ms,
        payload={
            "question": message,
            "answer": result.text,
            "status": result.status,
            "tool_calls": [call.as_dict() for call in result.tool_calls],
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "model_calls": result.model_calls,
            "stop_reason": result.stop_reason,
            "web_searches": result.web_searches,
            "conversation_id": str(thread_id),
            "first_tools": list(result.first_tools),
            "select_only": select_only,
        },
    )
    return result


def _partial_answer(result: AgentResult, reason: str) -> str:
    """What to say when a cap stopped the run.

    Reports only what actually happened. The model is not asked to summarise
    from here, because a model asked to wrap up under pressure is exactly where
    invented detail comes from.
    """
    lines = [reason]
    if result.text:
        lines.append("\nWhat I had so far:\n" + result.text)
    if result.tool_calls:
        lines.append("\nChecks completed before I stopped:")
        for call in result.tool_calls:
            detail = call.result_summary or call.error_message or call.outcome
            lines.append(f"- `{call.tool_name}` — {detail}")
    lines.append("\nAsk a narrower question and I should be able to finish.")
    return "\n".join(lines)


async def _converse(
    client_api: AsyncAnthropic,
    conversation: list[Any],
    tools: list[dict[str, Any]],
    *,
    context: AuditContext,
    settings: Settings,
    result: AgentResult,
    client_uuid: uuid.UUID,
    sites: Mapping[str, uuid.UUID],
    transcript: list[TurnMessage],
    select_only: bool = False,
    on_event: EventSink | None = None,
) -> None:
    """Drive the model until it answers or a cap stops it.

    `conversation` already holds the thread's history and the new question.
    Every message this turn adds to it is also noted in `transcript`, as plain
    JSON with its token usage, which is what gets stored.
    """
    paused_turns = 0
    while True:
        call_started = time.perf_counter()
        # Stable prefix, cached: the tool list and this prompt do not vary per
        # request, so across a working day only the questions are billed in full.
        system: list[TextBlockParam] = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        output_config: OutputConfigParam = {"effort": settings.anthropic_effort}
        await _emit(on_event, "model_call", {"index": result.model_calls + 1})
        if on_event is None:
            response = await client_api.messages.create(
                model=settings.anthropic_model,
                max_tokens=settings.anthropic_max_tokens,
                system=system,
                output_config=output_config,
                messages=cast(list[MessageParam], conversation),
                tools=cast(list[ToolParam], tools),
            )
        else:
            # The same request, streamed, so text reaches the listener as it is
            # written. What the loop acts on is still the assembled message.
            async with client_api.messages.stream(
                model=settings.anthropic_model,
                max_tokens=settings.anthropic_max_tokens,
                system=system,
                output_config=output_config,
                messages=cast(list[MessageParam], conversation),
                tools=cast(list[ToolParam], tools),
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        await _emit(on_event, "text", {"text": event.text})
                response = await stream.get_final_message()
        model_ms = int((time.perf_counter() - call_started) * 1000)
        result.model_calls += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            result.input_tokens += getattr(usage, "input_tokens", 0) or 0
            result.output_tokens += getattr(usage, "output_tokens", 0) or 0
            result.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            result.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

        tool_uses = _tool_uses(response)
        result.stop_reason = response.stop_reason
        if not result.first_tools:
            result.first_tools = _requested_tools(response)
        # Searches first, so the trail reads in the order things happened: the
        # model searched, then said what it said.
        search_queries = await _record_web_searches(
            response, context, result, model=settings.anthropic_model
        )
        for query in search_queries:
            await _emit(on_event, "web_search", {"query": query})
        await _record(
            context,
            event_type=AuditEventType.MODEL_RESPONSE,
            outcome=AuditOutcome.SUCCESS,
            model=settings.anthropic_model,
            prompt_version=SYSTEM_PROMPT_VERSION,
            duration_ms=model_ms,
            payload={
                "stop_reason": response.stop_reason,
                "text": _text_of(response),
                "tool_uses": [block.name for block in tool_uses],
                "web_searches": search_queries,
                "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
                "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
            },
        )

        # Guard before reading content: a refusal carries no usable answer.
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            result.status = STATUS_REFUSED
            result.text = (
                "The model declined to answer this question"
                + (f" ({details.category})" if details and details.category else "")
                + ". Rephrase it, or check it manually in the console."
            )
            return

        # The output limit cut the response short. Whatever text exists is real
        # and goes to the user with a note; "no answer" would be a lie about a
        # partial one, and continuing would feed the model a broken turn.
        if response.stop_reason == "max_tokens":
            result.status = STATUS_TRUNCATED
            produced = _text_of(response)
            limit = settings.anthropic_max_tokens
            if produced:
                result.text = (
                    f"{produced}\n\n_(This answer was cut off: the model reached its output "
                    f"limit of {limit} tokens. Everything above is what it produced; ask a "
                    "narrower question for the rest.)_"
                )
            else:
                result.text = (
                    f"The answer was cut off: the model reached its output limit of {limit} "
                    "tokens before producing any text. This is a truncated answer, not a "
                    "missing one — ask a narrower question."
                )
            return

        # Thinking blocks, tool_use blocks and web search blocks must go back
        # unchanged. Search results are data the model reads; nothing in them
        # is parsed or acted on here — only a `tool_use` block the model itself
        # emits ever runs a tool.
        conversation.append({"role": "assistant", "content": response.content})
        transcript.append(
            TurnMessage(
                "assistant",
                dump_blocks(response.content),
                getattr(usage, "input_tokens", None) if usage else None,
                getattr(usage, "output_tokens", None) if usage else None,
            )
        )

        # A long server-tool turn can be paused by the API. Resume it by
        # sending the conversation back as it stands; there is nothing to run.
        if response.stop_reason == "pause_turn" and paused_turns < MAX_PAUSED_TURNS:
            paused_turns += 1
            continue

        if response.stop_reason != "tool_use" or not tool_uses:
            result.text = _text_of(response) or (
                "The model returned no answer text. Nothing was changed; try asking again."
            )
            result.status = STATUS_OK
            return

        if select_only:
            # The harness wants the choice, not the answer: stop before any vendor
            # is called. `finalise_turn` drops the unanswered `tool_use` blocks.
            result.status = STATUS_OK
            result.text = (
                "Selection check: stopped before running "
                + ", ".join(f"`{name}`" for name in result.first_tools)
                + ". No tool was executed."
            )
            return

        remaining = settings.max_tool_calls - len(result.tool_calls)
        if remaining <= 0:
            result.status = STATUS_CAPPED
            result.text = _partial_answer(
                result,
                f"I stopped after {settings.max_tool_calls} tool calls, which is the "
                "limit for one question.",
            )
            return

        # The model may ask for several tools at once. Run them together and
        # return every result in one user message: splitting them teaches the
        # model to stop batching.
        accepted = tool_uses[:remaining]
        refused = tool_uses[remaining:]

        outcomes = await asyncio.gather(
            *(
                _execute_block(
                    block,
                    client_uuid,
                    settings=settings,
                    context=context,
                    sites=sites,
                    on_event=on_event,
                )
                for block in accepted
            )
        )

        blocks: list[dict[str, Any]] = []
        for block, (content, is_error, record) in zip(accepted, outcomes, strict=True):
            if record is not None:
                result.tool_calls.append(record)
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )
        for block in refused:
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": (
                        f"Not run: this question has reached its limit of "
                        f"{settings.max_tool_calls} tool calls."
                    ),
                    "is_error": True,
                }
            )
        conversation.append({"role": "user", "content": blocks})
        transcript.append(TurnMessage("user", dump_blocks(blocks)))

        if refused:
            result.status = STATUS_CAPPED
            result.text = _partial_answer(
                result,
                f"I stopped after {settings.max_tool_calls} tool calls, which is the "
                "limit for one question.",
            )
            return


async def _execute_block(
    block: Any,
    client_uuid: uuid.UUID,
    *,
    settings: Settings,
    context: AuditContext,
    sites: Mapping[str, uuid.UUID],
    on_event: EventSink | None = None,
) -> tuple[str, bool, ToolCallRecord | None]:
    """Resolve one `tool_use` block and run it, auditing the result."""
    raw_arguments = dict(block.input or {})
    # What a person watching may see: what the model asked for, less any id it
    # invented. The ids the engine injects are never in here to begin with.
    shown = {
        key: value
        for key, value in raw_arguments.items()
        if key not in ("client_id", SITE_ID_PARAMETER)
    }
    try:
        definition = get_tool(block.name)
    except KeyError as exc:
        message = str(exc).strip('"')
        await _emit(
            on_event,
            "tool_end",
            {
                "id": block.id,
                "tool_name": block.name,
                "connector": "",
                "arguments": shown,
                "outcome": "error",
                "duration_ms": 0,
                "error_type": "UnknownTool",
                "error_message": message,
                "result_summary": None,
            },
        )
        await _record(
            context,
            event_type=AuditEventType.TOOL_CALL,
            outcome=AuditOutcome.ERROR,
            tool_name=block.name,
            error_type="UnknownTool",
            error_message=message,
            payload={"arguments": raw_arguments},
        )
        return message, True, None

    await _record(
        context,
        event_type=AuditEventType.TOOL_CALL,
        outcome=AuditOutcome.IN_PROGRESS,
        tool_name=definition.name,
        connector_name=definition.connector,
        payload={"arguments": {k: v for k, v in raw_arguments.items() if k != "client_id"}},
    )

    await _emit(
        on_event,
        "tool_start",
        {
            "id": block.id,
            "tool_name": definition.name,
            "connector": definition.connector.value,
            "arguments": shown,
        },
    )

    content, is_error, record = await _run_one_tool(
        definition,
        raw_arguments,
        client_uuid,
        timeout_seconds=settings.tool_timeout_seconds,
        sites=sites,
    )
    await _emit(on_event, "tool_end", {"id": block.id, **record.as_dict()})

    await _record(
        context,
        event_type=AuditEventType.TOOL_RESULT,
        outcome={
            "success": AuditOutcome.SUCCESS,
            "timeout": AuditOutcome.TIMEOUT,
        }.get(record.outcome, AuditOutcome.ERROR),
        tool_name=record.tool_name,
        connector_name=definition.connector,
        duration_ms=record.duration_ms,
        error_type=record.error_type,
        error_message=record.error_message,
        payload={
            "arguments": record.arguments,
            "result_summary": record.result_summary,
        },
    )
    return content, is_error, record


async def record_feedback(
    audit_log_id: int,
    rating: str,
    *,
    rated_by: str,
    comment: str | None = None,
) -> bool:
    """Attach an engineer's verdict to one answer. False if the row is unknown."""
    from sqlalchemy import select

    from app.models.audit_log import AuditLog
    from app.models.enums import FeedbackRating

    async with session_scope() as session:
        entry = (
            await session.execute(select(AuditLog).where(AuditLog.id == audit_log_id))
        ).scalar_one_or_none()
        if entry is None:
            return False
        entry.feedback_rating = FeedbackRating(rating)
        entry.feedback_comment = comment
        entry.feedback_by = rated_by
        entry.feedback_at = dt.datetime.now(dt.UTC)
        await session.flush()
        return True

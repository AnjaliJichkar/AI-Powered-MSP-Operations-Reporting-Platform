"""A stand-in for `AsyncAnthropic` that returns scripted responses.

The responses are **real SDK objects** - `Message`, `TextBlock`, `ToolUseBlock`,
`Usage` - not look-alikes. The loop reads them exactly as it reads a live
response, so a change in the SDK's shape breaks these tests rather than
production.

Every request is recorded, so tests can assert what the model was actually
sent: the tool schemas, the system prompt, and the tool results fed back.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from typing import Any

from anthropic.lib.streaming import TextEvent
from anthropic.types import (
    Message,
    RefusalStopDetails,
    ServerToolUseBlock,
    TextBlock,
    ToolUseBlock,
    Usage,
    WebSearchResultBlock,
    WebSearchToolResultBlock,
    WebSearchToolResultError,
)

_ids = itertools.count(1)


def _message(
    content: list[Any],
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
    stop_details: RefusalStopDetails | None = None,
) -> Message:
    return Message(
        id=f"msg_{next(_ids):04d}",
        content=content,
        model="claude-opus-5",
        role="assistant",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        stop_details=stop_details,
        type="message",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def says(text: str, **usage: int) -> Message:
    """A final answer."""
    return _message([TextBlock(type="text", text=text)], **usage)


def calls(tool_name: str, **arguments: Any) -> Message:
    """A turn that asks for one tool."""
    return calls_many((tool_name, dict(arguments)))


def calls_many(*requests: tuple[str, dict[str, Any]]) -> Message:
    """A turn that asks for several tools at once (parallel tool use)."""
    return _message(
        [
            ToolUseBlock(
                type="tool_use",
                id=f"toolu_{next(_ids):04d}",
                name=name,
                input=arguments,
            )
            for name, arguments in requests
        ],
        stop_reason="tool_use",
    )


def thinks_then_calls(thought: str, tool_name: str, **arguments: Any) -> Message:
    """A turn with visible text alongside a tool call."""
    return _message(
        [
            TextBlock(type="text", text=thought),
            ToolUseBlock(
                type="tool_use", id=f"toolu_{next(_ids):04d}", name=tool_name, input=arguments
            ),
        ],
        stop_reason="tool_use",
    )


def _search_blocks(
    query: str, results: list[tuple[str, str]] | None, *, error_code: str | None = None
) -> list[Any]:
    """The pair of blocks a server-side web search leaves in a response."""
    use_id = f"srvtoolu_{next(_ids):04d}"
    use = ServerToolUseBlock(
        type="server_tool_use", id=use_id, name="web_search", input={"query": query}
    )
    content: Any
    if error_code is not None:
        content = WebSearchToolResultError(
            type="web_search_tool_result_error",
            error_code=error_code,  # type: ignore[arg-type]
        )
    else:
        content = [
            WebSearchResultBlock(
                type="web_search_result",
                url=url,
                title=title,
                encrypted_content="ZW5jcnlwdGVk",
                page_age="September 1, 2026",
            )
            for url, title in (results or [])
        ]
    found = WebSearchToolResultBlock(
        type="web_search_tool_result", tool_use_id=use_id, content=content
    )
    return [use, found]


def searches(
    query: str,
    results: list[tuple[str, str]] | None = None,
    *,
    then_says: str = "",
    error_code: str | None = None,
    stop_reason: str = "end_turn",
) -> Message:
    """A turn in which the model searched the web, as the API returns it.

    The search ran on Anthropic's side, so the response carries both the
    `server_tool_use` block and its result, and usually the answer text too.
    `results` is `(url, title)` pairs.
    """
    content = _search_blocks(query, results, error_code=error_code)
    if then_says:
        content.append(TextBlock(type="text", text=then_says))
    return _message(content, stop_reason=stop_reason)


def searches_then_calls(
    query: str, results: list[tuple[str, str]], tool_name: str, **arguments: Any
) -> Message:
    """A turn that searched and also asked for one of our tools."""
    content = _search_blocks(query, results)
    content.append(
        ToolUseBlock(type="tool_use", id=f"toolu_{next(_ids):04d}", name=tool_name, input=arguments)
    )
    return _message(content, stop_reason="tool_use")


def truncates(text: str = "") -> Message:
    """A turn the output limit cut short, with whatever text got out."""
    content: list[Any] = [TextBlock(type="text", text=text)] if text else []
    return _message(content, stop_reason="max_tokens", output_tokens=16000)


def refuses(category: str = "cyber") -> Message:
    return _message(
        [],
        stop_reason="refusal",
        stop_details=RefusalStopDetails(type="refusal", category=category),
    )


#: How many characters of text each streamed piece carries.
STREAM_PIECE = 12


class FakeStream:
    """What `messages.stream(...)` returns: text pieces, then the assembled message.

    The pieces are real SDK `TextEvent`s cut from the scripted message's own
    text, so what a listener is sent always adds up to what the loop acts on.
    """

    def __init__(self, message: Message) -> None:
        self._message = message

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[TextEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[TextEvent]:
        for block in self._message.content:
            text = getattr(block, "text", None) if block.type == "text" else None
            if not text:
                continue
            for start in range(0, len(text), STREAM_PIECE):
                yield TextEvent(
                    type="text",
                    text=text[start : start + STREAM_PIECE],
                    snapshot=text[: start + STREAM_PIECE],
                )

    async def get_final_message(self) -> Message:
        return self._message


class FakeMessages:
    def __init__(self, script: list[Message]) -> None:
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.streamed = 0

    async def create(self, **kwargs: Any) -> Message:
        # Snapshot `messages`: the loop appends to the same list after this
        # returns, so storing the reference would make every recorded request
        # show the conversation's final state instead of what was sent.
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self._script:
            # A test that under-scripts would otherwise loop forever.
            raise AssertionError(
                f"the loop made {len(self.requests)} model calls but the script "
                "ran out of responses"
            )
        return self._script.pop(0)

    def stream(self, **kwargs: Any) -> FakeStream:
        """The streamed form of `create`: same script, same request record."""
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        self.streamed += 1
        if not self._script:
            raise AssertionError(
                f"the loop made {len(self.requests)} model calls but the script "
                "ran out of responses"
            )
        return FakeStream(self._script.pop(0))

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def tool_results_sent(self) -> list[dict[str, Any]]:
        """Every distinct `tool_result` block the loop fed back to the model.

        The Messages API is stateless, so the loop resends the whole
        conversation each turn and a given result appears in several requests.
        Deduplicated by `tool_use_id` so a test counting results counts calls.
        """
        seen: dict[str, dict[str, Any]] = {}
        for request in self.requests:
            for message in request["messages"]:
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        seen.setdefault(block["tool_use_id"], block)
        return list(seen.values())


class FakeAnthropic:
    """Quacks like `AsyncAnthropic` for the two attributes the loop touches."""

    def __init__(self, *script: Message) -> None:
        self.messages = FakeMessages(list(script))

    @property
    def call_count(self) -> int:
        return self.messages.call_count

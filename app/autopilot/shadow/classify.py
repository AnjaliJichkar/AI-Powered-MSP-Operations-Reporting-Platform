"""One ticket, one model call, one validated answer or one recorded error.

The call is forced to the `classify_ticket` tool (`tool_choice`), so a text
answer is not among the outcomes, and the system prompt is marked for the API's
prompt cache, so a replay of thousands of tickets pays for the instructions
once instead of per ticket.

**The ticket is data.** It arrives inside a delimited block in the user
message, labelled as the thing to classify, and the prompt says plainly that a
subject asking for different behaviour is itself a ticket to classify. The
enforcement is not the wording though: it is that the answer must validate
against a closed schema built from *this tenant's* labels
(`schema.parse_prediction`), so there is no reachable output in which a subject
line changes which fields come back or which values are allowed. The worst a
hostile subject can do is get itself classified oddly.

This module takes the Anthropic client as an argument and never builds one, so
a test passes a fake and nothing here reaches the network by default.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from app.autopilot.shadow.prompts import CLASSIFY_SYSTEM_PROMPT
from app.autopilot.shadow.schema import (
    TOOL_NAME,
    InvalidPrediction,
    Prediction,
    classify_tool,
    parse_prediction,
)

#: The answer is six short fields. A generous ceiling is still small, and a
#: model that wants more than this has misunderstood the task.
MAX_TOKENS = 300


@dataclass(frozen=True, slots=True)
class Classification:
    """What one call produced: an answer or an error, never both."""

    prediction: Prediction | None
    error_type: str | None
    error_message: str | None
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    """Input served from the cache, **not** part of `input_tokens`. Billed at a
    fraction of it, and a replay reads the same instructions once per ticket,
    so adding the two together overstates the cost several-fold."""
    input_hash: str = ""
    """What was asked, as a digest. Stored on the prediction so a later run can
    tell that it would send the same request and read the answer instead."""

    @property
    def ok(self) -> bool:
        return self.prediction is not None


def _tool_input(response: Any) -> Any:
    """The forced tool's input, or `None` if the model did not call it."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == TOOL_NAME:
            return getattr(block, "input", None)
    return None


def _ticket_block(
    *,
    title: str,
    site: str,
    created_at: dt.datetime | None,
    description: str | None = None,
) -> str:
    """What the model is shown: the known-at-creation facts, delimited.

    The creation time is given with its weekday because that is the part that
    carries signal — an alert at three on a Sunday morning reads differently
    from a request at nine on a Monday — and because a bare timestamp invites
    arithmetic nobody needs.

    The body appears only when there is one. The forward shadow has it and a
    replay never does, so a prediction records *whether* it was there
    (`has_description`) and the two populations are compared knowing which saw
    more.
    """
    when = "unknown"
    if created_at is not None:
        when = f"{created_at:%Y-%m-%d %H:%M} ({created_at:%A})"
    body = f"\nbody: {description.strip()}" if description and description.strip() else ""
    return f"<ticket>\ncustomer: {site}\ncreated: {when}\nsubject: {title}{body}\n</ticket>"


def request_params(
    *,
    model: str,
    title: str,
    site: str,
    created_at: dt.datetime | None,
    issue_types: list[str],
    description: str | None = None,
) -> dict[str, Any]:
    """Exactly what is sent for one ticket, built in one place.

    One place because two things read it: the call, and the digest a later run
    compares against to decide whether it would be asking the same question.
    A request built twice is a digest that drifts from what was actually sent,
    which would show up as a cache that silently stops hitting — or worse, one
    that hits when the question has changed.
    """
    listing = "\n".join(f"- {label}" for label in issue_types)
    message = (
        "Classify the ticket below. This customer's issue types are:\n"
        f"{listing}\n\n"
        f"{_ticket_block(title=title, site=site, created_at=created_at, description=description)}"
    )
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [
            {
                "type": "text",
                "text": CLASSIFY_SYSTEM_PROMPT,
                # The instructions are identical on every ticket of a run;
                # cached, a replay pays for them once.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": message}],
        "tools": [classify_tool(issue_types)],
        "tool_choice": {"type": "tool", "name": TOOL_NAME},
    }


def fingerprint(params: Mapping[str, Any]) -> str:
    """The digest of one request: everything that goes over the wire.

    The model, the instructions, the tenant's label set, the tool schema and
    the ticket's own line are all in it, so two runs share an answer only when
    they would have asked an identical question. Nothing is left out to make
    more things match — a cheaper reuse that answers a different question is
    not a saving, it is a wrong number.
    """
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def read_message(
    response: Any, *, issue_types: list[str], digest: str = "", latency_ms: int = 0
) -> Classification:
    """One model message as a classification, however it got here.

    A message from a batch result is the same object as a message from a
    request, so it is read by the same function: a second parser would be a
    second set of rules about what counts as a valid answer, and the point of
    the batch path is that it measures the same thing more cheaply.
    """
    usage = getattr(response, "usage", None)
    input_tokens = (getattr(usage, "input_tokens", 0) or 0) if usage else 0
    cache_read = (getattr(usage, "cache_read_input_tokens", 0) or 0) if usage else 0
    output_tokens = (getattr(usage, "output_tokens", 0) or 0) if usage else 0
    try:
        prediction = parse_prediction(_tool_input(response), issue_types)
    except InvalidPrediction as exc:
        return Classification(
            prediction=None,
            error_type="InvalidPrediction",
            error_message=str(exc)[:300],
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            input_hash=digest,
        )
    return Classification(
        prediction=prediction,
        error_type=None,
        error_message=None,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        input_hash=digest,
    )


def failure(error: Exception | str, *, digest: str = "", latency_ms: int = 0) -> Classification:
    """A call that did not produce a message, as a row rather than a raise."""
    return Classification(
        prediction=None,
        error_type=type(error).__name__ if isinstance(error, Exception) else error,
        error_message=(str(error) if isinstance(error, Exception) else error)[:300],
        latency_ms=latency_ms,
        input_tokens=0,
        output_tokens=0,
        input_hash=digest,
    )


async def send(
    model_client: Any, params: dict[str, Any], *, issue_types: list[str]
) -> Classification:
    """One request, its answer parsed, or a recorded error. Never raises."""
    digest = fingerprint(params)
    started = time.monotonic()
    try:
        response = await cast(Any, model_client).messages.create(**params)
    except Exception as exc:
        return failure(exc, digest=digest, latency_ms=int((time.monotonic() - started) * 1000))
    return read_message(
        response,
        issue_types=issue_types,
        digest=digest,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


async def classify(
    model_client: Any,
    *,
    model: str,
    title: str,
    site: str,
    created_at: dt.datetime | None,
    issue_types: list[str],
    description: str | None = None,
) -> Classification:
    """Classify one ticket. Never raises for a bad answer: it returns one.

    `description` is the scrubbed body. It is passed in flight and stored
    nowhere — not here, not on the prediction — because a ticket's words are
    not something these tables hold.
    """
    return await send(
        model_client,
        request_params(
            model=model,
            title=title,
            site=site,
            created_at=created_at,
            issue_types=issue_types,
            description=description,
        ),
        issue_types=issue_types,
    )

"""Collects the tool definitions every connector exposes.

Importing `app.connectors` registers every tool. The agent loop (step 3) reads
`registered_tools()` to build the tool list it sends to the model; this module
knows nothing about the model or the loop.

The decorator enforces hard rule 1 at import time: a tool without a required
`client_id: str` parameter cannot be registered, so it can never reach the
model in the first place.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel, ValidationError, create_model

from app.models.enums import ConnectorName
from app.settings import get_settings

P = ParamSpec("P")
R = TypeVar("R")

CLIENT_ID_PARAMETER = "client_id"

#: The real parameter a tool takes to narrow itself to one end customer.
SITE_ID_PARAMETER = "site_id"

#: What the model is shown instead: a slug it can read off the system prompt.
#: The loop resolves it to a `site_id` within the current client.
SITE_PARAMETER = "site"


class ToolRegistrationError(RuntimeError):
    """A tool function does not meet the contract in CLAUDE.md."""


class ToolArgumentError(ValueError):
    """The model sent arguments that do not fit the tool's signature."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One callable tool, plus the schema the model is shown."""

    name: str
    description: str
    connector: ConnectorName
    input_schema: dict[str, Any]
    input_model: type[BaseModel]
    func: Callable[..., Awaitable[Any]]
    max_rows: int | None = None
    """Rows this tool may return in one result; None means `TOOL_MAX_ROWS`."""

    @property
    def row_cap(self) -> int:
        """The cap in force: the tool's own, else the configured default."""
        return self.max_rows if self.max_rows is not None else get_settings().tool_max_rows

    @property
    def accepts_site(self) -> bool:
        """True when this tool can be narrowed to one end customer."""
        return SITE_ID_PARAMETER in self.input_schema.get("properties", {})

    def model_facing_schema(self) -> dict[str, Any]:
        """`input_schema` with the ids removed and a site *slug* offered instead.

        The engine resolves both scopes server-side, so the model is never shown
        `client_id` or `site_id`. It cannot pass an id, cannot pass the wrong
        one, and cannot express a call against another client at all. What it
        can do is name a site by the slug listed in the system prompt; the loop
        resolves that within the current client and rejects anything outside it.
        """
        schema = deepcopy(self.input_schema)
        properties = schema.get("properties", {})
        had_site = properties.pop(SITE_ID_PARAMETER, None) is not None
        properties.pop(CLIENT_ID_PARAMETER, None)

        required = schema.get("required")
        if isinstance(required, list):
            for hidden in (CLIENT_ID_PARAMETER, SITE_ID_PARAMETER):
                if hidden in required:
                    required.remove(hidden)

        if had_site:
            properties[SITE_PARAMETER] = {
                "type": "string",
                "description": (
                    "Optional. The slug of one end customer, taken from the site list in "
                    "the system prompt. Omit it to cover every site of this client at "
                    "once, in which case each row is tagged with the site it came from."
                ),
            }
        return schema

    def to_anthropic_schema(self) -> dict[str, Any]:
        """The tool as the Anthropic tool-use API expects it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.model_facing_schema(),
        }

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Coerce and check the model's arguments against the signature.

        Raises `ToolArgumentError` with a message written to be handed back to
        the model, so it can correct itself rather than the run failing.
        """
        try:
            validated = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
                for error in exc.errors()
            )
            raise ToolArgumentError(f"invalid arguments for {self.name}: {problems}") from exc
        return validated.model_dump()


_REGISTRY: dict[str, ToolDefinition] = {}


def _build_input_model(func: Callable[..., Any]) -> tuple[type[BaseModel], dict[str, Any]]:
    """Derive a validation model and a JSON Schema from the function's signature."""
    signature = inspect.signature(func)
    try:
        hints = inspect.get_annotations(func, eval_str=True)
    except (NameError, TypeError) as exc:  # pragma: no cover - defensive
        raise ToolRegistrationError(
            f"tool {func.__name__!r} has annotations that cannot be resolved: {exc}"
        ) from exc

    fields: dict[str, Any] = {}
    for parameter_name, parameter in signature.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise ToolRegistrationError(
                f"tool {func.__name__!r} must not take *args or **kwargs: the model "
                "can only send named, typed arguments"
            )
        if parameter_name not in hints:
            raise ToolRegistrationError(
                f"tool {func.__name__!r} parameter {parameter_name!r} has no type annotation"
            )
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter_name] = (hints[parameter_name], default)

    model: type[BaseModel] = create_model(f"{func.__name__}_input", **fields)
    schema = model.model_json_schema()
    schema.pop("title", None)
    return model, schema


def _validate_client_scope(func: Callable[..., Any], hints: dict[str, Any]) -> None:
    """Hard rule 1: every tool is scoped to a client, and the scope is required."""
    signature = inspect.signature(func)
    parameter = signature.parameters.get(CLIENT_ID_PARAMETER)
    if parameter is None:
        raise ToolRegistrationError(
            f"tool {func.__name__!r} has no {CLIENT_ID_PARAMETER!r} parameter; "
            "every tool call must be scoped to a client"
        )
    if parameter.default is not inspect.Parameter.empty:
        raise ToolRegistrationError(
            f"tool {func.__name__!r} gives {CLIENT_ID_PARAMETER!r} a default; "
            "a request without a client must fail, not fall back"
        )
    if hints.get(CLIENT_ID_PARAMETER) is not str:
        raise ToolRegistrationError(
            f"tool {func.__name__!r} must annotate {CLIENT_ID_PARAMETER!r} as str"
        )


def _validate_site_scope(func: Callable[..., Any]) -> None:
    """A site is always optional: omitting it means every site of the client."""
    parameter = inspect.signature(func).parameters.get(SITE_ID_PARAMETER)
    if parameter is None:
        return
    if parameter.default is inspect.Parameter.empty:
        raise ToolRegistrationError(
            f"tool {func.__name__!r} makes {SITE_ID_PARAMETER!r} required; it must "
            "default to None, because a question without a site covers them all"
        )


def tool(
    connector: ConnectorName, *, name: str | None = None, max_rows: int | None = None
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Register an async connector function as a tool the model can call.

    The docstring becomes the description the model reads, so write it for the
    model: say what the tool returns and when to reach for it.

    `max_rows` caps how many rows a list tool hands back in one result; it
    defaults to the `TOOL_MAX_ROWS` setting. A tool applies it with
    `limit_rows` and reports the total, so a capped list is never mistaken
    for the whole.
    """
    if max_rows is not None and max_rows < 1:
        raise ToolRegistrationError(f"max_rows must be at least 1, got {max_rows}")

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        if not inspect.iscoroutinefunction(func):
            raise ToolRegistrationError(f"tool {func.__name__!r} must be an async function")

        description = inspect.getdoc(func)
        if not description:
            raise ToolRegistrationError(
                f"tool {func.__name__!r} has no docstring; the model has nothing to read"
            )

        hints = inspect.get_annotations(func, eval_str=True)
        _validate_client_scope(func, hints)
        _validate_site_scope(func)

        tool_name = name or func.__name__
        if tool_name in _REGISTRY and _REGISTRY[tool_name].func is not func:
            raise ToolRegistrationError(
                f"a different tool named {tool_name!r} is already registered"
            )

        input_model, input_schema = _build_input_model(func)
        _REGISTRY[tool_name] = ToolDefinition(
            name=tool_name,
            description=description,
            connector=connector,
            input_schema=input_schema,
            input_model=input_model,
            func=func,
            max_rows=max_rows,
        )
        return func

    return decorator


@dataclass(frozen=True, slots=True)
class RowWindow[T]:
    """The rows a tool may return, and what it must say about the rest."""

    rows: list[T]
    total: int
    """How many rows there were before the cap."""

    @property
    def truncated(self) -> bool:
        return self.total > len(self.rows)


def limit_rows[T](tool_name: str, rows: Sequence[T]) -> RowWindow[T]:
    """Apply a tool's row cap. Sort first: the cap keeps the front of the list."""
    cap = get_tool(tool_name).row_cap
    return RowWindow(rows=list(rows[:cap]), total=len(rows))


def registered_tools() -> list[ToolDefinition]:
    """Every registered tool, in a stable order."""
    return [_REGISTRY[key] for key in sorted(_REGISTRY)]


def get_tool(name: str) -> ToolDefinition:
    """Look up one tool by the name the model used."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise KeyError(f"no tool named {name!r}; registered tools: {known}") from None


def tools_for(connector: ConnectorName) -> list[ToolDefinition]:
    """Every tool belonging to one connector."""
    return [entry for entry in registered_tools() if entry.connector is connector]


def clear_registry(names: Iterable[str] | None = None) -> None:
    """Drop registrations. For tests that register throwaway tools."""
    if names is None:
        _REGISTRY.clear()
        return
    for entry in names:
        _REGISTRY.pop(entry, None)

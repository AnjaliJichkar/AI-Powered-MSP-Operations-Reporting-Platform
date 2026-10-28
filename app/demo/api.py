"""The shared shape of the fake vendors: an in-memory HTTP API behind `httpx.MockTransport`.

Each vendor module builds a `Router` with the paths its real client sends and
answers from the `World`. The real connector clients run against them
unchanged, so every allowlist guard, filter re-check, pagination rule and
error mapping is exercised as it is in production. A request the router does
not know gets a 404 with the vendor's own error shape, never a stack trace,
so a new client call is found the same way a wrong path is found live.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.demo.dataset import World

Handler = Callable[["Call"], httpx.Response]


@dataclass(slots=True)
class Call:
    """One request, unpacked: what a handler reads."""

    request: httpx.Request
    world: World
    now: dt.datetime
    path_params: dict[str, str]
    params: dict[str, str]
    body: Any

    def param(self, name: str, default: str | None = None) -> str | None:
        return self.params.get(name, default)

    def int_param(self, name: str, default: int) -> int:
        value = self.params.get(name)
        try:
            return int(value) if value is not None else default
        except ValueError:
            return default


def json_response(
    body: Any, status: int = 200, headers: Mapping[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=body, headers=dict(headers or {}))


@dataclass(slots=True)
class Router:
    """Method + path pattern -> handler. Path patterns use `{name}` placeholders."""

    vendor: str
    error_body: Callable[[int, str], Any]
    routes: list[tuple[str, re.Pattern[str], Handler]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    """Every (method, path) answered, for tests that assert what a client sent."""
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        """Register one path. Everything but a `{name}` placeholder is matched
        literally, escaped: a vendor path can carry regex metacharacters —
        Microsoft Graph's `/members/$count` and
        `getMailboxUsageDetail(period='D30')` both do — and an unescaped `$`
        or `(` matches nothing, or the wrong thing, without saying so."""
        parts = re.split(r"\{(\w+)\}", pattern)
        regex = "^"
        for index, part in enumerate(parts):
            regex += f"(?P<{part}>[^/]+)" if index % 2 else re.escape(part)
        self.routes.append((method.upper(), re.compile(regex + "$"), handler))

    def handle(self, request: httpx.Request, world: World, now: dt.datetime) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        self.requests.append(request)
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content.decode("utf-8", errors="replace")
        for method, regex, handler in self.routes:
            match = regex.match(path)
            if match and method == request.method.upper():
                call = Call(
                    request=request,
                    world=world,
                    now=now,
                    path_params=match.groupdict(),
                    params=dict(request.url.params),
                    body=body,
                )
                return handler(call)
        return json_response(self.error_body(404, f"no route for {request.method} {path}"), 404)


def paged(
    rows: list[Any], after: int | None, page_size: int, key: Callable[[Any], int]
) -> list[Any]:
    """NinjaOne's list paging: rows with an id past `after`, in id order, `page_size` at most."""
    ordered = sorted(rows, key=key)
    if after is not None:
        ordered = [row for row in ordered if key(row) > after]
    return ordered[:page_size]


def parse_date(value: str | None) -> dt.datetime | None:
    """`YYYY-MM-DD` or ISO-8601, as the clients send them; None when absent or unreadable."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        if len(text) == 10:
            return dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)
        parsed = dt.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except ValueError:
        return None

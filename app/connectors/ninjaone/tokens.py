"""NinjaOne access tokens, shared between connectors in one process.

The factory builds a connector per tool call, and a connector used to hold its
own token, so one question that made three tool calls authenticated three times
(seen live on 2026-09-18). A client-credentials token is good for an hour and
belongs to the franchisee's API application, not to a connector instance, so it
is held here instead and every connector for the same client reads it.

What keeps that safe:

- **The key is the client and its credential version**, plus the host and the
  OAuth scope the token was issued for. A token is never visible to another
  client, and replacing a client's credentials changes the version, so the old
  token is simply never looked up again.
- **No version, no sharing.** A connector built without a credential version
  cannot know whether the credentials behind a stored token are still the
  current ones, so it gets a private store (see `NinjaOneClient.__init__`).
- **Tokens live in memory only.** Nothing here is logged, persisted or put in
  an error message. The stored credential remains the id/secret pair.
- **One refresh at a time.** `lock(key)` is shared by every connector with that
  key, so three connectors that find no token make one request, and a 401 that
  several hit at once leads to one re-authentication.
- **A rejected token is discarded only by value.** A connector that was handed
  a 401 drops the token *it* used; if another connector has already refreshed,
  the newer token stays.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from dataclasses import dataclass

#: (client id, credential version, host or region, OAuth scope)
TokenKey = tuple[uuid.UUID, str | None, str, str]


@dataclass(frozen=True, slots=True)
class _HeldToken:
    value: str
    expires_at: dt.datetime

    def __repr__(self) -> str:  # never render the token
        return f"<token expires_at={self.expires_at.isoformat()}>"


class TokenStore:
    """Access tokens by client and credential version. Not shared across processes."""

    def __init__(self) -> None:
        self._tokens: dict[TokenKey, _HeldToken] = {}
        self._locks: dict[TokenKey, asyncio.Lock] = {}

    def get(self, key: TokenKey) -> str | None:
        """The live token for this key, or None. An expired one is dropped."""
        held = self._tokens.get(key)
        if held is None:
            return None
        if dt.datetime.now(dt.UTC) >= held.expires_at:
            del self._tokens[key]
            return None
        return held.value

    def put(self, key: TokenKey, token: str, expires_at: dt.datetime) -> None:
        # One live token per client: a new credential version retires the old one's.
        for other in [k for k in self._tokens if k[0] == key[0] and k[1] != key[1]]:
            del self._tokens[other]
        self._tokens[key] = _HeldToken(token, expires_at)

    def discard(self, key: TokenKey, token: str | None) -> None:
        """Drop the stored token if it is still `token` — the one the vendor rejected."""
        held = self._tokens.get(key)
        if held is not None and (token is None or held.value == token):
            del self._tokens[key]

    def lock(self, key: TokenKey) -> asyncio.Lock:
        """Held while authenticating, by whichever connector gets there first."""
        return self._locks.setdefault(key, asyncio.Lock())

    def clear(self) -> None:
        self._tokens.clear()

    def __len__(self) -> int:
        return len(self._tokens)


#: The process-wide store. Tests install their own through the connector.
TOKENS = TokenStore()

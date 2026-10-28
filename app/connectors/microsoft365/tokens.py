"""Microsoft Graph access tokens, shared between connectors in one process.

The same store the NinjaOne connector has, for the same reason — the factory
builds a connector per tool call and a client-credentials token is good for an
hour — with one difference that matters: a Graph token belongs to **one
tenant**, so the key carries the tenant id. A client that reads three of its
customers' tenants holds three tokens, and none of them can be used against
another tenant even by mistake.

What keeps it safe is unchanged: the key is the client and its credential
version as well as the tenant and the scope; a connector with no credential
version gets a private store; tokens live in memory only and are never logged
or persisted; one refresh at a time per key; and a rejected token is discarded
only by value, so a connector that was handed a 401 does not throw away a
newer token another connector just fetched.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from dataclasses import dataclass

#: (client id, credential version, Entra tenant id, OAuth scope)
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

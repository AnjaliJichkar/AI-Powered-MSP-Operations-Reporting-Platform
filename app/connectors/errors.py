"""Normalised connector errors.

Every connector raises these, never a vendor-specific or `httpx` exception. The
agent loop turns them into an honest answer rather than a guess (hard rule 7),
so each one carries enough detail to say what actually happened.
"""

from __future__ import annotations

from typing import Any, ClassVar


class ConnectorError(Exception):
    """Base class for everything a connector can fail with."""

    #: Whether retrying the same call could plausibly succeed.
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        connector: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.connector = connector
        self.status_code = status_code
        self.details: dict[str, Any] = details or {}

    def as_dict(self) -> dict[str, Any]:
        """Audit-safe summary. Never includes credentials or response bodies."""
        return {
            "error_type": type(self).__name__,
            "connector": self.connector,
            "status_code": self.status_code,
            "message": self.message,
            "details": self.details,
        }

    def __str__(self) -> str:
        if self.status_code is None:
            return f"[{self.connector}] {self.message}"
        return f"[{self.connector}] {self.message} (HTTP {self.status_code})"


class AuthError(ConnectorError):
    """Credentials were rejected, expired, or lack the required scope."""


class RateLimited(ConnectorError):
    """The vendor is throttling us. Retried up to the configured limit first."""

    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str,
        *,
        connector: str,
        status_code: int | None = 429,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, connector=connector, status_code=status_code, details=details)
        self.retry_after = retry_after

    def as_dict(self) -> dict[str, Any]:
        return {**super().as_dict(), "retry_after": self.retry_after}


class NotFound(ConnectorError):
    """The requested object does not exist upstream."""


class UpstreamError(ConnectorError):
    """The vendor failed, timed out, or answered in a shape we cannot read."""

    retryable: ClassVar[bool] = True


# --- resolution failures, raised by the factory before any HTTP call ---------


class UnknownClient(ConnectorError):
    """No active client matches the supplied ``client_id`` (hard rule 1)."""


class ArgumentRejected(ConnectorError, ValueError):
    """The vendor refused a parameter value and said what it accepts.

    A `ValueError` as well, so the agent loop hands the message back to the
    model as a correctable argument error rather than a vendor failure. The
    message carries the vendor's own list of accepted values when it gave one.
    """


class ConnectorNotConfigured(ConnectorError):
    """The client exists but this connector has no mapping or no credentials."""


class SyncConflict(ConnectorError):
    """`sync_sites` found a vendor organisation already claimed by another client.

    Aborting is the only safe answer: silently re-pointing the mapping would
    move one franchisee's customer into another franchisee's view.
    """


class UnknownSite(ConnectorError):
    """The named site does not exist, is inactive, or belongs to another client.

    Deliberately one error for all three: telling a caller that a slug exists
    but belongs to someone else would leak another franchisee's customer list.
    """


__all__ = [
    "AuthError",
    "ConnectorError",
    "ConnectorNotConfigured",
    "NotFound",
    "RateLimited",
    "SyncConflict",
    "UnknownClient",
    "UnknownSite",
    "UpstreamError",
]

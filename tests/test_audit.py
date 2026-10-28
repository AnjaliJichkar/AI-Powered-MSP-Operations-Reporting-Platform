"""Audit payloads must never carry a secret into the database."""

from __future__ import annotations

import uuid

from app.audit.recorder import MAX_STRING_LENGTH, REDACTED, AuditContext, redact
from app.models.enums import Interface


def test_secret_keys_are_stripped() -> None:
    payload = {
        "client_id": "abc",
        "api_key": "sk-live-123",
        "password": "hunter2",
        "authorization": "Bearer token",
    }
    result = redact(payload)

    assert result["client_id"] == "abc"
    assert result["api_key"] == REDACTED
    assert result["password"] == REDACTED
    assert result["authorization"] == REDACTED


def test_redaction_is_recursive_and_case_insensitive() -> None:
    payload = {
        "request": {"headers": {"Authorization": "Bearer x"}, "path": "/devices"},
        "results": [{"name": "PC-01", "Refresh_Token": "r"}],
    }
    result = redact(payload)

    assert result["request"]["headers"]["Authorization"] == REDACTED
    assert result["request"]["path"] == "/devices"
    assert result["results"][0]["name"] == "PC-01"
    assert result["results"][0]["Refresh_Token"] == REDACTED


def test_bytes_are_never_stored() -> None:
    assert redact({"secret_ciphertext": b"\x01\x02"}) == {"secret_ciphertext": REDACTED}
    assert redact({"blob": b"\x01\x02"}) == {"blob": REDACTED}


def test_long_strings_are_truncated() -> None:
    long_value = "x" * (MAX_STRING_LENGTH + 500)
    result = redact({"answer": long_value})

    assert result["answer"].endswith("[truncated]")
    assert len(result["answer"]) < len(long_value)


def test_short_values_pass_through_untouched() -> None:
    payload = {"days": 30, "ok": True, "name": None, "tags": ["a", "b"]}
    assert redact(payload) == payload


def test_audit_context_generates_a_request_id() -> None:
    context = AuditContext.new(actor_id="U123", interface=Interface.SLACK)

    assert isinstance(context.request_id, uuid.UUID)
    assert context.client_id is None
    assert context.actor_id == "U123"


def test_audit_context_carries_an_unresolved_client_reference() -> None:
    """A request rejected for a bad client is still auditable."""
    context = AuditContext.new(
        actor_id="U123", interface=Interface.SLACK, client_ref="teamlogic-nowhere"
    )
    assert context.client_id is None
    assert context.client_ref == "teamlogic-nowhere"

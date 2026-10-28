"""Reusable column types."""

from __future__ import annotations

from sqlalchemy import Enum as SAEnum

from app.models.enums import (
    AuditEventType,
    AuditOutcome,
    ConnectorName,
    FeedbackRating,
    IdentityProvider,
    Interface,
    MessageRole,
    OrganisationKind,
    Role,
    UserStatus,
)


def _text_enum(enum_cls: type, name: str, length: int = 32) -> SAEnum:
    """Store an enum as varchar + check constraint rather than a Postgres enum type.

    Native enum types need an ``ALTER TYPE`` and cannot drop a value; a check
    constraint is one ordinary migration.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


connector_name_type = _text_enum(ConnectorName, "connector_name")
interface_type = _text_enum(Interface, "interface", length=16)
audit_event_type = _text_enum(AuditEventType, "audit_event_type")
audit_outcome_type = _text_enum(AuditOutcome, "audit_outcome")
feedback_rating_type = _text_enum(FeedbackRating, "feedback_rating", length=8)
message_role_type = _text_enum(MessageRole, "message_role", length=16)
organisation_kind_type = _text_enum(OrganisationKind, "organisation_kind", length=16)
role_type = _text_enum(Role, "user_role", length=24)
user_status_type = _text_enum(UserStatus, "user_status", length=16)
identity_provider_type = _text_enum(IdentityProvider, "identity_provider", length=16)

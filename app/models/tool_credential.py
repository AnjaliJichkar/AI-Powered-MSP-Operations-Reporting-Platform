"""Encrypted per-client connector credentials.

The model never sees these values. ``app/connectors/factory.py`` resolves a row
from ``(client_id, connector_name)`` and hands the decrypted secrets to a
connector constructor; nothing else reads them (hard rules 1 and 3).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Identity,
    LargeBinary,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ConnectorName
from app.models.mixins import TimestampMixin
from app.models.types import connector_name_type
from app.security.crypto import CredentialCipher, get_cipher

if TYPE_CHECKING:
    from app.models.client import Client


class ToolCredential(Base, TimestampMixin):
    """One credential set for one ``(client, connector)`` pair."""

    __tablename__ = "tool_credentials"
    __table_args__ = (UniqueConstraint("client_id", "connector_name"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    connector_name: Mapped[ConnectorName] = mapped_column(connector_name_type, nullable=False)

    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    """Fernet token over a JSON object of secret fields."""

    encryption_key_id: Mapped[str] = mapped_column(nullable=False)
    """Which configured key produced ``secret_ciphertext``. Enables rotation."""

    config: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    """Non-secret connector settings: base URL, region, instance, scopes."""

    expires_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """When the stored access token stops working, if the vendor tells us."""

    rotated_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    client: Mapped[Client] = relationship(back_populates="credentials")

    # --- secret access ---------------------------------------------------

    def set_secrets(
        self, secrets: dict[str, Any], *, cipher: CredentialCipher | None = None
    ) -> None:
        """Encrypt and store the secret fields."""
        cipher = cipher or get_cipher()
        self.secret_ciphertext, self.encryption_key_id = cipher.encrypt(secrets)

    def get_secrets(self, *, cipher: CredentialCipher | None = None) -> dict[str, Any]:
        """Decrypt the secret fields. Callers must not log the result."""
        cipher = cipher or get_cipher()
        return cipher.decrypt(self.secret_ciphertext, self.encryption_key_id)

    def is_expired(self, *, now: dt.datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or dt.datetime.now(dt.UTC))

    def __repr__(self) -> str:
        # Deliberately excludes the ciphertext and every secret field.
        return (
            f"<ToolCredential client_id={self.client_id!r} "
            f"connector={self.connector_name!r} active={self.is_active!r}>"
        )

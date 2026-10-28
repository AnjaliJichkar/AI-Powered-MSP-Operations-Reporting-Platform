"""Symmetric encryption for connector credentials.

`ToolCredential` rows store a Fernet token, never plaintext. The key id used to
produce a token is stored alongside it so keys can be rotated without a bulk
re-encryption: retired keys stay in ``CREDENTIAL_ENCRYPTION_KEYS`` and keep
decrypting old rows while only the active key encrypts new ones.

Keys come from the environment. Nothing in this module reads or writes a file.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.settings import get_settings


class DecryptionError(RuntimeError):
    """A credential blob could not be decrypted with the referenced key."""


class CredentialCipher:
    """Encrypts and decrypts the JSON secret blob of a connector credential."""

    def __init__(self, keys: dict[str, str], active_key_id: str) -> None:
        if not keys:
            raise ValueError(
                "No credential encryption keys configured. Set "
                "CREDENTIAL_ENCRYPTION_KEYS before storing or reading credentials."
            )
        if active_key_id not in keys:
            raise ValueError(f"Active key id {active_key_id!r} is not in the key map")
        self._active_key_id = active_key_id
        self._fernets: dict[str, Fernet] = {
            key_id: Fernet(key.encode("utf-8")) for key_id, key in keys.items()
        }

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def encrypt(self, secrets: dict[str, Any]) -> tuple[bytes, str]:
        """Return ``(ciphertext, key_id)`` for a mapping of secret fields."""
        payload = json.dumps(secrets, separators=(",", ":"), sort_keys=True).encode("utf-8")
        token = self._fernets[self._active_key_id].encrypt(payload)
        return token, self._active_key_id

    def decrypt(self, ciphertext: bytes, key_id: str) -> dict[str, Any]:
        """Return the secret mapping for a token produced with ``key_id``."""
        fernet = self._fernets.get(key_id)
        if fernet is None:
            raise DecryptionError(
                f"Credential was encrypted with key id {key_id!r}, which is not "
                "configured. Add it to CREDENTIAL_ENCRYPTION_KEYS."
            )
        try:
            payload = fernet.decrypt(ciphertext)
        except InvalidToken as exc:  # pragma: no cover - defensive
            raise DecryptionError(f"Credential could not be decrypted with key {key_id!r}") from exc
        decoded: dict[str, Any] = json.loads(payload)
        return decoded

    def needs_rotation(self, key_id: str) -> bool:
        """True when a row is still encrypted under a retired key."""
        return key_id != self._active_key_id


@lru_cache(maxsize=1)
def get_cipher() -> CredentialCipher:
    """Cipher built from settings. Call ``get_cipher.cache_clear()`` in tests."""
    settings = get_settings()
    keys = {
        key_id: secret.get_secret_value()
        for key_id, secret in settings.credential_encryption_keys.items()
    }
    return CredentialCipher(keys, settings.credential_encryption_active_key_id)


def generate_key() -> str:
    """Generate a new Fernet key. Used by operators, never at runtime."""
    return Fernet.generate_key().decode("utf-8")

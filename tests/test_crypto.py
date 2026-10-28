"""Credential encryption, including key rotation."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.security.crypto import CredentialCipher, DecryptionError, generate_key, get_cipher

SECRETS = {"client_id": "abc", "client_secret": "s3cr3t", "refresh_token": "r3fr3sh"}


def test_roundtrip(cipher: CredentialCipher) -> None:
    ciphertext, key_id = cipher.encrypt(SECRETS)
    assert cipher.decrypt(ciphertext, key_id) == SECRETS


def test_ciphertext_does_not_contain_the_plaintext(cipher: CredentialCipher) -> None:
    ciphertext, _ = cipher.encrypt(SECRETS)
    assert b"s3cr3t" not in ciphertext
    assert b"client_secret" not in ciphertext


def test_encryption_uses_the_active_key(test_keys: dict[str, str]) -> None:
    cipher = CredentialCipher(test_keys, "v2")
    _, key_id = cipher.encrypt(SECRETS)
    assert key_id == "v2"


def test_retired_keys_still_decrypt_after_rotation(test_keys: dict[str, str]) -> None:
    """A rotation must not require re-encrypting every row before it takes effect."""
    old = CredentialCipher(test_keys, "v1")
    ciphertext, key_id = old.encrypt(SECRETS)

    rotated = CredentialCipher(test_keys, "v2")
    assert rotated.decrypt(ciphertext, key_id) == SECRETS
    assert rotated.needs_rotation(key_id) is True

    _, new_key_id = rotated.encrypt(SECRETS)
    assert new_key_id == "v2"
    assert rotated.needs_rotation(new_key_id) is False


def test_unknown_key_id_is_a_clear_error(cipher: CredentialCipher) -> None:
    ciphertext, _ = cipher.encrypt(SECRETS)
    with pytest.raises(DecryptionError, match=r"not.*configured"):
        cipher.decrypt(ciphertext, "v99")


def test_ciphertext_from_another_key_does_not_decrypt(test_keys: dict[str, str]) -> None:
    stranger = CredentialCipher({"v1": Fernet.generate_key().decode()}, "v1")
    ciphertext, _ = stranger.encrypt(SECRETS)

    cipher = CredentialCipher(test_keys, "v1")
    with pytest.raises(DecryptionError):
        cipher.decrypt(ciphertext, "v1")


def test_empty_key_map_is_refused(test_keys: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="No credential encryption keys"):
        CredentialCipher({}, "v1")
    with pytest.raises(ValueError, match="not in the key map"):
        CredentialCipher(test_keys, "v9")


def test_get_cipher_uses_settings() -> None:
    assert get_cipher().active_key_id == "v2"


def test_generate_key_produces_a_usable_key() -> None:
    cipher = CredentialCipher({"v1": generate_key()}, "v1")
    ciphertext, key_id = cipher.encrypt({"a": 1})
    assert cipher.decrypt(ciphertext, key_id) == {"a": 1}

"""Password hashing and one-time codes, on their own.

These are the two pieces of cryptography written for authentication, so they
are tested apart from the HTTP that uses them. Neither is invented here:
argon2id comes from a library, and the code generator is RFC 6238, which the
last test checks against the vectors in the RFC itself.
"""

from __future__ import annotations

import base64
import time

import pytest

from app.security.passwords import (
    MIN_PASSWORD_LENGTH,
    WeakPassword,
    check_password_policy,
    constant_time_equals,
    hash_password,
    verify_password,
)
from app.security.totp import (
    DIGITS,
    STEP_SECONDS,
    _code_at,
    code_for,
    new_secret,
    open_secret,
    provisioning_uri,
    seal_secret,
    verify_code,
)
from app.settings import Settings, get_settings

GOOD = "a-long-enough-password"


def test_a_hash_is_argon2id_and_salted() -> None:
    first = hash_password(GOOD)
    second = hash_password(GOOD)

    assert first.startswith("$argon2id$")
    # Different salts, so the same password never hashes to the same string.
    assert first != second
    assert GOOD not in first


def test_the_right_password_verifies_and_a_wrong_one_does_not() -> None:
    stored = hash_password(GOOD)
    assert verify_password(stored, GOOD).ok is True
    assert verify_password(stored, GOOD + "!").ok is False
    assert verify_password(stored, "").ok is False


def test_a_user_with_no_password_is_refused_rather_than_erroring() -> None:
    """Invited, or governed by an external provider: no hash, no exception."""
    result = verify_password(None, GOOD)
    assert result.ok is False and result.needs_rehash is False


def test_a_corrupt_hash_is_a_refusal_not_a_crash() -> None:
    assert verify_password("not-a-hash", GOOD).ok is False


def test_a_hash_made_with_weaker_settings_asks_to_be_upgraded() -> None:
    """Raising the cost later does not invalidate anything already stored."""
    weak = get_settings().model_copy(update={"password_hash_time_cost": 1})
    stored = hash_password(GOOD, settings=weak)

    weak_check = verify_password(stored, GOOD, settings=weak)
    assert weak_check.ok is True and weak_check.needs_rehash is False

    strong = get_settings().model_copy(update={"password_hash_time_cost": 3})
    strong_check = verify_password(stored, GOOD, settings=strong)
    assert strong_check.ok is True and strong_check.needs_rehash is True


@pytest.mark.parametrize("password", ["", "   ", "short", "x" * (MIN_PASSWORD_LENGTH - 1)])
def test_a_password_that_is_too_short_is_refused(password: str) -> None:
    with pytest.raises(WeakPassword):
        check_password_policy(password)


def test_a_very_long_password_is_refused_rather_than_hashed() -> None:
    """A body cannot be used to make the server do unbounded work."""
    with pytest.raises(WeakPassword, match="at most"):
        check_password_policy("x" * 2000)


def test_length_is_the_only_rule() -> None:
    """No complexity rules: they push people towards predictable substitutions."""
    check_password_policy("aaaaaaaaaaaaaaaaaaaa")
    check_password_policy("correct horse battery staple")


def test_constant_time_equals_compares_tokens() -> None:
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", "abd") is False
    assert constant_time_equals("abc", "abcd") is False


# --- one-time codes -------------------------------------------------------------


def test_a_secret_is_base32_and_long_enough() -> None:
    secret = new_secret()
    assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    # 160 bits in base32, unpadded.
    assert len(secret) == 32
    assert new_secret() != secret


def test_a_code_is_six_digits_and_verifies() -> None:
    secret = new_secret()
    code = code_for(secret)
    assert len(code) == DIGITS and code.isdigit()
    assert verify_code(secret, code) is True


def test_a_code_from_another_secret_does_not_verify() -> None:
    assert verify_code(new_secret(), code_for(new_secret())) is False


def test_one_step_of_drift_is_allowed_and_two_is_not() -> None:
    """A phone's clock is not the server's, and ninety seconds is the allowance."""
    secret = new_secret()
    now = time.time()

    assert verify_code(secret, code_for(secret, at=now - STEP_SECONDS), at=now) is True
    assert verify_code(secret, code_for(secret, at=now + STEP_SECONDS), at=now) is True
    assert verify_code(secret, code_for(secret, at=now - 3 * STEP_SECONDS), at=now) is False
    assert verify_code(secret, code_for(secret, at=now + 3 * STEP_SECONDS), at=now) is False


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "  "])
def test_a_code_of_the_wrong_shape_is_refused(code: str) -> None:
    assert verify_code(new_secret(), code) is False


def test_spaces_in_a_typed_code_are_ignored() -> None:
    """Authenticator apps print `123 456`, and people copy what they see."""
    secret = new_secret()
    code = code_for(secret)
    assert verify_code(secret, f"{code[:3]} {code[3:]}") is True


def test_the_generator_matches_the_rfc_6238_vectors() -> None:
    """RFC 6238 appendix B, the SHA-1 rows. The algorithm is not ours to choose.

    The RFC's key is the ASCII "12345678901234567890"; the vectors are given
    as times, which are counters once divided by the step.
    """
    secret = base64.b32encode(b"12345678901234567890").decode("ascii").rstrip("=")
    for unix_time, expected in (
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
        (20000000000, "353130"),
    ):
        assert _code_at(secret, unix_time // STEP_SECONDS) == expected


def test_a_provisioning_uri_carries_the_secret_and_the_issuer() -> None:
    uri = provisioning_uri("ABCDEF", email="someone@example.com", issuer="Inputiv")
    assert uri.startswith("otpauth://totp/Inputiv%3Asomeone%40example.com?")
    assert "secret=ABCDEF" in uri
    assert "issuer=Inputiv" in uri
    assert f"period={STEP_SECONDS}" in uri


def test_a_stored_secret_is_encrypted_and_reads_back(settings: Settings) -> None:
    secret = new_secret()
    sealed = seal_secret(secret)

    assert secret not in sealed
    # `<key_id>:<token>`, so a key rotation reads an old row as it does a credential.
    key_id, _, token = sealed.partition(":")
    assert key_id == settings.credential_encryption_active_key_id and token
    assert open_secret(sealed) == secret

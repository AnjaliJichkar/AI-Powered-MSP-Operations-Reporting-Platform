"""Password hashing, with argon2id.

One algorithm, one place. Everything that checks a password goes through
:func:`verify_password`, and nothing else in the engine touches a plaintext one
beyond the request that carried it.

**Why argon2id and not a general-purpose hash.** A password is the one secret a
person chooses, which means it is guessable; the defence is to make each guess
expensive in both time and memory. argon2id is the memory-hard construction
OWASP and RFC 9106 name for this, and it is a different tool from
`app/security/crypto.py`: that encrypts credentials we must read back, this
hashes a value we must never read back.

**Cost is configuration, not a constant** (the same reasoning as hard rule 6).
A hash carries the parameters it was made with, so raising them later does not
invalidate anything: `verify_password` reports when a stored hash was made with
weaker settings, and the caller writes back the upgraded one on the next
successful sign-in.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from app.settings import Settings, get_settings

#: A password shorter than this is refused at every entry point. Length is the
#: only property worth insisting on: complexity rules push people towards
#: predictable substitutions and a second place to write the result down.
MIN_PASSWORD_LENGTH = 12

#: argon2 hashes the whole input, so a very long password is not a problem for
#: the algorithm. The cap is here so a request body cannot be used to make the
#: server do unbounded work.
MAX_PASSWORD_LENGTH = 1024


class WeakPassword(ValueError):
    """The password does not meet the policy. The message is shown to the user."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What checking a password found."""

    ok: bool
    needs_rehash: bool = False
    """True when the stored hash is valid but was made with weaker parameters
    than the ones configured now. The caller re-hashes and stores it."""


@lru_cache(maxsize=4)
def _hasher(time_cost: int, memory_cost: int, parallelism: int) -> PasswordHasher:
    return PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


def hasher(settings: Settings | None = None) -> PasswordHasher:
    settings = settings or get_settings()
    return _hasher(
        settings.password_hash_time_cost,
        settings.password_hash_memory_kib,
        settings.password_hash_parallelism,
    )


def check_password_policy(password: str) -> None:
    """Raise `WeakPassword` if this password may not be used. Returns nothing."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"a password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPassword(f"a password may be at most {MAX_PASSWORD_LENGTH} characters")
    if not password.strip():
        raise WeakPassword("a password may not be only whitespace")


def hash_password(password: str, *, settings: Settings | None = None) -> str:
    """Hash a password after checking the policy. The result carries its parameters."""
    check_password_policy(password)
    return hasher(settings).hash(password)


def verify_password(
    stored_hash: str | None, password: str, *, settings: Settings | None = None
) -> VerificationResult:
    """Check a password against a stored hash.

    A user with no password — invited and not yet accepted, or governed by an
    external identity provider — still costs a full verification against a
    throwaway hash, so the time this takes does not say which case it was.
    """
    engine = hasher(settings)
    if stored_hash is None:
        # Equal work for a user that cannot sign in, so an attacker enumerating
        # addresses learns nothing from how long the answer took. The mismatch
        # is the point of the call and is swallowed, not raised: this function
        # answers with a result, never with an exception.
        try:
            engine.verify(_absent_hash(engine), "not the password")
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass
        return VerificationResult(ok=False)
    try:
        engine.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return VerificationResult(ok=False)
    return VerificationResult(ok=True, needs_rehash=engine.check_needs_rehash(stored_hash))


@lru_cache(maxsize=4)
def _absent_hash(engine: PasswordHasher) -> str:
    """A hash of a random value, for the no-password path. Never matches anything."""
    return engine.hash(secrets.token_urlsafe(32))


def constant_time_equals(left: str, right: str) -> bool:
    """Compare two tokens without leaking their common prefix through timing."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))

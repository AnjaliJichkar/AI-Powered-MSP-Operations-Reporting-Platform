"""Time-based one-time passwords (RFC 6238), and where the secret lives.

A second factor is optional per user: it is required at sign-in only once the
user has proved a code from the secret they enrolled, so an abandoned enrolment
never locks anybody out.

**No new dependency.** RFC 6238 is HMAC over a counter and RFC 4226's dynamic
truncation of the result; both are in the standard library, and the whole of it
is the forty lines below. The stack list in CLAUDE.md is short on purpose, and
a library here would add one for an algorithm that is fully specified and has
no room for interpretation.

**The secret is encrypted at rest**, with the same Fernet cipher that protects
connector credentials (`app/security/crypto.py`, hard rule 3). It is stored as
`<key_id>:<token>` so a key rotation reads old rows exactly as it does for a
credential. It is returned by exactly one endpoint, once, at enrolment, and
never again.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from app.security.crypto import CredentialCipher, get_cipher

#: Seconds per step, and the digits shown. The values every authenticator app
#: assumes; changing either means every enrolled device stops working.
STEP_SECONDS = 30
DIGITS = 6

#: Steps either side of now that are accepted, for clock drift between the
#: server and the phone. One step is the usual allowance: it accepts a code for
#: at most ninety seconds, and a wider window is a longer replay opportunity.
DRIFT_STEPS = 1

#: 160 bits, which is the HMAC-SHA1 block the RFC specifies and what
#: authenticator apps expect from a base32 secret.
SECRET_BYTES = 20


def new_secret() -> str:
    """A fresh base32 secret, unpadded, as an authenticator app expects it."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _code_at(secret: str, counter: int) -> str:
    """RFC 4226 dynamic truncation of HMAC-SHA1(secret, counter)."""
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**DIGITS)).zfill(DIGITS)


def code_for(secret: str, *, at: float | None = None) -> str:
    """The code that is valid right now. For tests and for enrolment checks."""
    moment = time.time() if at is None else at
    return _code_at(secret, int(moment // STEP_SECONDS))


def verify_code(secret: str, code: str, *, at: float | None = None) -> bool:
    """True if `code` is valid for `secret` now, allowing for clock drift.

    Every candidate is compared in constant time, and every step in the window
    is compared even after a match, so the answer takes the same time whichever
    step it was.
    """
    cleaned = "".join(character for character in code if character.isdigit())
    if len(cleaned) != DIGITS:
        return False
    moment = time.time() if at is None else at
    step = int(moment // STEP_SECONDS)
    matched = False
    for offset in range(-DRIFT_STEPS, DRIFT_STEPS + 1):
        candidate = _code_at(secret, step + offset)
        matched |= hmac.compare_digest(candidate, cleaned)
    return matched


def provisioning_uri(secret: str, *, email: str, issuer: str) -> str:
    """The `otpauth://` URI an authenticator app scans as a QR code."""
    label = quote(f"{issuer}:{email}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


# --- storage -----------------------------------------------------------------


def seal_secret(secret: str, *, cipher: CredentialCipher | None = None) -> str:
    """Encrypt a TOTP secret for the `users.totp_secret` column."""
    engine = cipher or get_cipher()
    token, key_id = engine.encrypt({"totp_secret": secret})
    return f"{key_id}:{token.decode('ascii')}"


def open_secret(stored: str, *, cipher: CredentialCipher | None = None) -> str:
    """Decrypt what `seal_secret` wrote. Raises `DecryptionError` on a retired key."""
    engine = cipher or get_cipher()
    key_id, _, token = stored.partition(":")
    return str(engine.decrypt(token.encode("ascii"), key_id)["totp_secret"])

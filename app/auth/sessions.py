"""Server-side sessions, and the cookie that points at one.

The cookie holds a random token and nothing else: no identity, no role, no
client, no expiry the client could edit. Everything about the session is a row,
so revoking one is a write and takes effect on the next request rather than
whenever a signed token happens to expire.

The cookie is `HttpOnly` (script cannot read it), `Secure` (it does not travel
in clear text) and `SameSite=Lax` (another site's form post does not carry it,
while an ordinary link from an email still works).

Two expiries, both checked on every request:

- **absolute** (`expires_at`) is set at sign-in and never extended, so a stolen
  session cannot be kept alive indefinitely by using it;
- **idle** (`last_seen_at` plus the idle window) closes a session left alone.

`last_seen_at` is only written when it has moved by more than
`TOUCH_INTERVAL_SECONDS`, so an active session is not a write per request.

CSRF is a synchroniser token. The session row holds one; the page gets it from
`GET /auth/me` and sends it back in `X-CSRF-Token` on anything that changes
state. It is held in the clear because it must be handed back on every page
load, and it is useless without the session cookie, which script cannot read.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

from fastapi import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organisation import Session, User
from app.settings import Settings

#: Bytes of entropy in the cookie token and the CSRF token.
TOKEN_BYTES = 32

#: How stale `last_seen_at` may get before a request writes it.
TOUCH_INTERVAL_SECONDS = 60.0

#: The header a state-changing request carries its CSRF token in.
CSRF_HEADER = "X-CSRF-Token"

#: Methods that may change state, and so must carry a CSRF token.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hash(token: str) -> str:
    """SHA-256 of a session token. The database never holds the token itself.

    A plain hash and not argon2: this value is 256 bits of our own randomness,
    not a password, so there is nothing to guess and no reason to pay for a
    slow comparison on every request.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(
    db: AsyncSession,
    user: User,
    *,
    settings: Settings,
    ip_address: str | None = None,
    user_agent: str | None = None,
    acting_client_id: uuid.UUID | None = None,
    now: dt.datetime | None = None,
) -> tuple[Session, str]:
    """Open a session. Returns the row and the token that goes in the cookie."""
    moment = now or dt.datetime.now(dt.UTC)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    row = Session(
        user_id=user.id,
        token_hash=_hash(token),
        csrf_token=secrets.token_urlsafe(TOKEN_BYTES),
        created_at=moment,
        last_seen_at=moment,
        expires_at=moment + dt.timedelta(hours=settings.session_absolute_hours),
        acting_client_id=acting_client_id,
        ip_address=ip_address,
        # Kept only to tell one session from another in a list; truncated so a
        # long header cannot be used to write a lot of rows quickly.
        user_agent=(user_agent or None) and (user_agent or "")[:300],
    )
    db.add(row)
    await db.flush()
    return row, token


async def load_session(
    db: AsyncSession, token: str | None, *, settings: Settings, now: dt.datetime | None = None
) -> Session | None:
    """The live session this token names, or None. Expiry is checked here."""
    if not token:
        return None
    moment = now or dt.datetime.now(dt.UTC)
    row = (
        await db.execute(select(Session).where(Session.token_hash == _hash(token)))
    ).scalar_one_or_none()
    if row is None:
        return None
    if not row.is_live(moment, idle_seconds=settings.session_idle_minutes * 60):
        return None
    return row


async def touch(db: AsyncSession, row: Session, *, now: dt.datetime | None = None) -> None:
    """Move the idle clock, but only when it has actually drifted."""
    moment = now or dt.datetime.now(dt.UTC)
    if (moment - row.last_seen_at).total_seconds() < TOUCH_INTERVAL_SECONDS:
        return
    row.last_seen_at = moment
    await db.execute(update(Session).where(Session.id == row.id).values(last_seen_at=moment))
    await db.commit()


async def revoke_session(db: AsyncSession, row: Session, *, now: dt.datetime | None = None) -> None:
    """Close one session. The row stays: a sign-out is part of the record."""
    moment = now or dt.datetime.now(dt.UTC)
    row.revoked_at = moment
    await db.execute(update(Session).where(Session.id == row.id).values(revoked_at=moment))


async def revoke_all_for_user(
    db: AsyncSession, user_id: uuid.UUID, *, now: dt.datetime | None = None
) -> int:
    """Close every live session a user holds. Used when a password changes, a
    second factor is turned off, or an account is suspended: any of those means
    whoever held an old session should be made to prove themselves again."""
    moment = now or dt.datetime.now(dt.UTC)
    # Returning the ids rather than reading `rowcount`: the async result type
    # does not expose one, and the count is what the audit row reports.
    revoked = (
        (
            await db.execute(
                update(Session)
                .where(Session.user_id == user_id, Session.revoked_at.is_(None))
                .values(revoked_at=moment)
                .returning(Session.id)
            )
        )
        .scalars()
        .all()
    )
    return len(revoked)


# --- the cookie --------------------------------------------------------------


def set_session_cookie(response: Response, token: str, *, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_absolute_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, *, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )

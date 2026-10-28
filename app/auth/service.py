"""Signing in, inviting, and changing an account.

Everything here writes an `audit_log` row (hard rule 4). A sign-in that fails
is as much a record as one that succeeds, and neither ever carries the password
or the token: the payload says which account and why it was refused, and the
redactor in `app/audit/recorder.py` is the second line of defence.

The two throttles are deliberately different in kind. The **lockout** is a
column on the user, so it survives a restart and follows the account wherever
it is attacked from. The **rate limit** is per client address and in memory, so
it sees a spray across many accounts that no per-account counter could. Neither
alone is enough.

Refusals are deliberately uninformative to the caller and precise in the log. A
sign-in that names an address we have never seen takes the same work and gives
the same message as one with the wrong password, so the endpoint cannot be used
to find out who has an account.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.models.client import Client
from app.models.enums import (
    AuditEventType,
    AuditOutcome,
    IdentityProvider,
    Interface,
    OrganisationKind,
    Role,
    UserStatus,
)
from app.models.organisation import ROLES_BY_KIND, Invite, Organisation, User
from app.security.passwords import hash_password, verify_password
from app.security.totp import open_secret, verify_code
from app.settings import Settings

#: What every refused sign-in says, whatever the reason. The reason is in the
#: audit row, where it belongs.
GENERIC_LOGIN_FAILURE = "that email address and password do not match an account"


class LoginRefused(Exception):
    """A sign-in that will not proceed. `reason` is for the log, not the caller."""

    def __init__(self, reason: str, *, message: str = GENERIC_LOGIN_FAILURE) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class AccountLocked(LoginRefused):
    """Too many failures. Says so, because the user needs to know to wait."""


class SecondFactorRequired(LoginRefused):
    """The password was right and the code was missing or wrong."""


class InviteRefused(Exception):
    """An invite link that cannot be used, and why. Shown to the caller."""


class InviteState(StrEnum):
    """What a token turned out to be.

    A page and an endpoint both have to answer this, and they have to answer
    it the same way, so it is decided once and the two only differ in how they
    say it. Every value but `open` is an ordinary end for a link somebody was
    sent, not an error: a stack trace is never the right answer to a link that
    is a day old.
    """

    OPEN = "open"
    UNKNOWN = "unknown"
    """No invite has this token. A typo, a truncated link, or one already
    replaced by a reissue."""
    EXPIRED = "expired"
    ACCEPTED = "accepted"
    """Already used. The account exists, so the next step is to sign in."""
    REVOKED = "revoked"
    """Withdrawn, or replaced by a reissue to the same address."""
    HAS_ACCOUNT = "has_account"
    """The invite is open but the address has an account some other way."""


#: What each state says to the person holding the link. Plain sentences: they
#: did nothing wrong, and the next step is the useful half.
INVITE_MESSAGES: dict[InviteState, str] = {
    InviteState.UNKNOWN: (
        "This invitation link is not one we recognise. It may have been cut short by an "
        "email client, or replaced by a newer invitation. Ask whoever invited you to send "
        "another."
    ),
    InviteState.EXPIRED: (
        "This invitation has expired. Ask whoever invited you to send another; it only "
        "takes them a moment."
    ),
    InviteState.ACCEPTED: (
        "This invitation has already been used, which usually means the account is ready. "
        "Sign in with the password you chose."
    ),
    InviteState.REVOKED: (
        "This invitation has been withdrawn, which usually means a newer one was sent to "
        "the same address. Check for a later email, or ask for another."
    ),
    InviteState.HAS_ACCOUNT: (
        "There is already an account for this address, so there is nothing to set up. "
        "Sign in, or use the password reset if you cannot."
    ),
}


@dataclass(frozen=True, slots=True)
class InviteLookup:
    """A token, resolved. `invite` is None only when nothing matched."""

    state: InviteState
    invite: Invite | None = None

    @property
    def usable(self) -> bool:
        return self.state is InviteState.OPEN

    @property
    def message(self) -> str:
        return INVITE_MESSAGES.get(self.state, "")


async def describe_invite(
    db: AsyncSession, token: str, *, now: dt.datetime | None = None
) -> InviteLookup:
    """What this token is, without spending it.

    Used by the page that shows somebody who invited them, and by `accept`
    before it writes anything, so the two can never disagree about whether a
    link is usable.
    """
    moment = now or dt.datetime.now(dt.UTC)
    invite = (
        await db.execute(select(Invite).where(Invite.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if invite is None:
        return InviteLookup(InviteState.UNKNOWN)
    if invite.accepted_at is not None:
        return InviteLookup(InviteState.ACCEPTED, invite)
    if invite.revoked_at is not None:
        return InviteLookup(InviteState.REVOKED, invite)
    if invite.expires_at <= moment:
        return InviteLookup(InviteState.EXPIRED, invite)
    if await get_user_by_email(db, invite.email) is not None:
        return InviteLookup(InviteState.HAS_ACCOUNT, invite)
    return InviteLookup(InviteState.OPEN, invite)


async def open_invites_for(
    db: AsyncSession, email: str, *, now: dt.datetime | None = None
) -> list[Invite]:
    """Every invite for an address that could still be accepted."""
    moment = now or dt.datetime.now(dt.UTC)
    rows = (
        (await db.execute(select(Invite).where(Invite.email == normalise_email(email))))
        .scalars()
        .all()
    )
    return [row for row in rows if row.is_open(moment)]


async def revoke_invites_for(
    db: AsyncSession, email: str, *, now: dt.datetime | None = None
) -> int:
    """Withdraw every open invite for an address. Returns how many.

    Revoked rather than deleted, like everything else here: the record that
    somebody was invited and the invitation was withdrawn is worth keeping.
    """
    moment = now or dt.datetime.now(dt.UTC)
    outstanding = await open_invites_for(db, email, now=moment)
    for invite in outstanding:
        invite.revoked_at = moment
    return len(outstanding)


def normalise_email(email: str) -> str:
    """Lower-cased and stripped. The database refuses anything else."""
    return email.strip().lower()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == normalise_email(email)))
    ).scalar_one_or_none()


# --- signing in --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    """What the caller sent. The password is never stored or logged."""

    email: str
    password: str
    totp_code: str | None = None


async def authenticate(
    db: AsyncSession,
    attempt: LoginAttempt,
    *,
    settings: Settings,
    now: dt.datetime | None = None,
) -> User:
    """Check an account's password and second factor, or raise `LoginRefused`.

    The work is the same whether or not the address exists: `verify_password`
    hashes against a throwaway when there is no stored hash, so the time taken
    does not say whether an account is there.
    """
    moment = now or dt.datetime.now(dt.UTC)
    email = normalise_email(attempt.email)
    user = await get_user_by_email(db, email)

    if user is None:
        verify_password(None, attempt.password, settings=settings)
        raise LoginRefused("no such account")

    if user.locked_until is not None and user.locked_until > moment:
        raise AccountLocked(
            "locked",
            message=(
                "this account is locked after too many failed attempts; "
                f"try again after {user.locked_until:%H:%M UTC}"
            ),
        )

    if user.identity_provider is not IdentityProvider.LOCAL:
        # The provider owns this account's credentials; we hold no password to
        # check and must not pretend otherwise.
        verify_password(None, attempt.password, settings=settings)
        raise LoginRefused(f"identity provider is {user.identity_provider.value}")

    result = verify_password(user.password_hash, attempt.password, settings=settings)
    if not result.ok:
        raise LoginRefused("wrong password")

    # Only now, once the password is known to be right, does anything about the
    # account's state or its second factor change the answer. Checking status
    # first would let a wrong password still reveal that an account is
    # suspended.
    if user.status is not UserStatus.ACTIVE:
        raise LoginRefused(f"status is {user.status.value}")
    if not user.organisation.is_active:
        raise LoginRefused("organisation is inactive")

    if user.totp_enabled:
        if not attempt.totp_code:
            raise SecondFactorRequired(
                "second factor missing",
                message="this account needs a code from your authenticator app",
            )
        if not verify_code(
            open_secret(str(user.totp_secret)), attempt.totp_code, at=moment.timestamp()
        ):
            raise SecondFactorRequired(
                "second factor wrong", message="that code is not right; try the next one"
            )

    if result.needs_rehash:
        # The stored hash predates the current cost settings. Upgrade it now,
        # while the plaintext is in hand and known to be correct.
        user.password_hash = hash_password(attempt.password, settings=settings)
        user.password_changed_at = moment
    return user


async def record_failure(
    db: AsyncSession,
    *,
    email: str,
    reason: str,
    user: User | None,
    settings: Settings,
    interface: Interface = Interface.API,
    ip_address: str | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Count a failure against the account and write the audit row."""
    moment = now or dt.datetime.now(dt.UTC)
    locked = False
    if user is not None:
        user.failed_login_count += 1
        if user.failed_login_count >= settings.login_max_failures:
            user.locked_until = moment + dt.timedelta(minutes=settings.login_lockout_minutes)
            user.failed_login_count = 0
            locked = True
    await record_event(
        db,
        AuditContext.new(
            actor_id=str(user.id) if user else "anonymous",
            actor_user_id=user.id if user else None,
            interface=interface,
        ),
        event_type=AuditEventType.LOGIN,
        outcome=AuditOutcome.DENIED,
        payload={
            "email": normalise_email(email),
            "reason": reason,
            "ip": ip_address,
            "locked": locked,
        },
    )


async def record_success(
    db: AsyncSession,
    user: User,
    *,
    session_id: uuid.UUID,
    interface: Interface = Interface.API,
    ip_address: str | None = None,
    now: dt.datetime | None = None,
) -> None:
    moment = now or dt.datetime.now(dt.UTC)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = moment
    await record_event(
        db,
        AuditContext.new(actor_id=str(user.id), actor_user_id=user.id, interface=interface),
        event_type=AuditEventType.LOGIN,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "email": user.email,
            "role": user.role.value,
            "organisation": user.organisation.slug,
            "session_id": str(session_id),
            "ip": ip_address,
            "second_factor": user.totp_enabled,
        },
    )


# --- organisations and users --------------------------------------------------


async def create_organisation(
    db: AsyncSession,
    *,
    kind: OrganisationKind,
    slug: str,
    name: str,
    client: Client | None = None,
) -> Organisation:
    """Create the Inputiv organisation, or one franchisee's."""
    if kind is OrganisationKind.FRANCHISEE and client is None:
        raise ValueError("a franchisee organisation must name the client it belongs to")
    if kind is OrganisationKind.INPUTIV and client is not None:
        raise ValueError("the Inputiv organisation does not belong to a client")
    organisation = Organisation(
        kind=kind, slug=slug, name=name, client_id=client.id if client else None
    )
    db.add(organisation)
    await db.flush()
    return organisation


async def create_user(
    db: AsyncSession,
    *,
    organisation: Organisation,
    email: str,
    role: Role,
    password: str | None = None,
    display_name: str | None = None,
    status: UserStatus | None = None,
) -> User:
    """Add a user to an organisation. The role must belong to its kind."""
    if role not in ROLES_BY_KIND[organisation.kind]:
        raise ValueError(
            f"{role.value!r} is not a role of a {organisation.kind.value} organisation; "
            f"expected one of {sorted(r.value for r in ROLES_BY_KIND[organisation.kind])}"
        )
    now = dt.datetime.now(dt.UTC)
    user = User(
        organisation_id=organisation.id,
        organisation_kind=organisation.kind,
        email=normalise_email(email),
        display_name=display_name,
        role=role,
        status=status or (UserStatus.ACTIVE if password else UserStatus.INVITED),
        identity_provider=organisation.identity_provider,
        password_hash=hash_password(password) if password else None,
        password_changed_at=now if password else None,
    )
    db.add(user)
    await db.flush()
    user.organisation = organisation
    return user


async def count_users(db: AsyncSession, organisation_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.organisation_id == organisation_id)
            )
        ).scalar_one()
    )


# --- invites ------------------------------------------------------------------


async def create_invite(
    db: AsyncSession,
    *,
    organisation: Organisation,
    email: str,
    role: Role,
    settings: Settings,
    created_by: User | None = None,
    reissue: bool = False,
    now: dt.datetime | None = None,
) -> tuple[Invite, str]:
    """Make a one-time invite. Returns the row and the token, which is shown once.

    The token is not recoverable afterwards: only its hash is stored, so a
    database that leaks yields no usable link. That is also why a lost invite
    is *reissued* rather than looked up.

    An address with an invite already outstanding is refused unless `reissue`
    is set, in which case the outstanding ones are withdrawn first. Two live
    tokens for one address is the state nobody wants: whoever holds the older
    email uses it, the second person's link then fails because the account
    exists, and the trail says two people were invited once each.
    """
    if role not in ROLES_BY_KIND[organisation.kind]:
        raise ValueError(
            f"{role.value!r} is not a role of a {organisation.kind.value} organisation"
        )
    moment = now or dt.datetime.now(dt.UTC)
    address = normalise_email(email)
    if await get_user_by_email(db, address) is not None:
        raise InviteRefused(f"{address} already has an account")
    outstanding = await open_invites_for(db, address, now=moment)
    if outstanding and not reissue:
        raise InviteRefused(
            f"{address} already has an invitation, open until "
            f"{outstanding[0].expires_at:%Y-%m-%d %H:%M UTC}. Reissue it to replace that one."
        )
    replaced = await revoke_invites_for(db, address, now=moment) if reissue else 0
    token = secrets.token_urlsafe(32)
    invite = Invite(
        organisation_id=organisation.id,
        email=address,
        role=role,
        token_hash=hash_token(token),
        expires_at=moment + dt.timedelta(hours=settings.invite_expiry_hours),
        created_by_user_id=created_by.id if created_by else None,
    )
    db.add(invite)
    await db.flush()
    await record_event(
        db,
        AuditContext.new(
            actor_id=str(created_by.id) if created_by else "system",
            actor_user_id=created_by.id if created_by else None,
            interface=Interface.API if created_by else Interface.CLI,
        ),
        event_type=AuditEventType.USER_INVITED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "invite_id": str(invite.id),
            "email": address,
            "role": role.value,
            "organisation": organisation.slug,
            "expires_at": invite.expires_at.isoformat(),
            "replaced": replaced,
        },
    )
    return invite, token


async def accept_invite(
    db: AsyncSession,
    *,
    token: str,
    password: str,
    display_name: str | None = None,
    now: dt.datetime | None = None,
) -> User:
    """Turn an open invite into an active account. The token is spent either way."""
    moment = now or dt.datetime.now(dt.UTC)
    found = await describe_invite(db, token, now=moment)
    if not found.usable or found.invite is None:
        # The same sentence the page would have shown, so somebody who got
        # past it by racing a second tab is told the same thing.
        raise InviteRefused(found.message)
    invite = found.invite

    user = await create_user(
        db,
        organisation=invite.organisation,
        email=invite.email,
        role=invite.role,
        password=password,
        display_name=display_name,
        status=UserStatus.ACTIVE,
    )
    invite.accepted_at = moment
    invite.accepted_user_id = user.id
    await record_event(
        db,
        AuditContext.new(actor_id=str(user.id), actor_user_id=user.id, interface=Interface.API),
        event_type=AuditEventType.INVITE_ACCEPTED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "invite_id": str(invite.id),
            "email": user.email,
            "role": user.role.value,
            "organisation": invite.organisation.slug,
        },
    )
    return user


# --- changing an account -------------------------------------------------------


async def record_user_change(
    db: AsyncSession,
    *,
    actor: User | None,
    subject: User,
    change: str,
    detail: dict[str, object] | None = None,
) -> None:
    """One audit row for anything done to an account, by whoever did it."""
    await record_event(
        db,
        AuditContext.new(
            actor_id=str(actor.id) if actor else "system",
            actor_user_id=actor.id if actor else None,
            interface=Interface.API if actor else Interface.CLI,
        ),
        event_type=AuditEventType.USER_CHANGED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "user_id": str(subject.id),
            "email": subject.email,
            "change": change,
            **(detail or {}),
        },
    )

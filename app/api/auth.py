"""Signing in, the current session, switching client, second factors, invites.

Nothing here takes a `client_id` or a `user_id` from the caller. Identity comes
from the session cookie and the tenant from the session row, which is the whole
point of the change: before it, a request said who it was and was believed.

Every response about the caller carries the CSRF token for their session, so a
page that has just loaded can send it on the next thing it does.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.auth.dependencies import (
    INPUTIV_ADMIN,
    PUBLIC,
    SELECT_CLIENT,
    SIGNED_IN,
    ScopeRule,
)
from app.auth.ratelimit import LOGIN_LIMITER
from app.auth.scope import Scope
from app.auth.service import (
    AccountLocked,
    InviteRefused,
    LoginAttempt,
    LoginRefused,
    SecondFactorRequired,
    accept_invite,
    authenticate,
    create_invite,
    record_failure,
    record_user_change,
)
from app.auth.service import (
    record_success as write_login_success,
)
from app.auth.sessions import (
    clear_session_cookie,
    create_session,
    revoke_all_for_user,
    revoke_session,
    set_session_cookie,
)
from app.db.session import get_session
from app.models.client import Client
from app.models.enums import (
    AuditEventType,
    AuditOutcome,
    Interface,
    OrganisationKind,
    Role,
    UserStatus,
)
from app.models.organisation import Invite, Organisation, User
from app.security.passwords import WeakPassword, hash_password, verify_password
from app.security.totp import new_secret, open_secret, provisioning_uri, seal_secret, verify_code
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: Who may create invites: Inputiv admins anywhere, and a franchisee admin
#: within their own organisation only (enforced below, not just here).
INVITE_ADMIN = ScopeRule(
    label="invite-admin",
    roles=frozenset({Role.INPUTIV_ADMIN, Role.FRANCHISEE_ADMIN}),
    needs_client=False,
)


class AuthRequest(BaseModel):
    """Base for everything posted here: unknown fields are refused, not ignored.

    That is what retires `client_id` and `user_id` from request bodies. A caller
    that still sends one gets a 422 naming it, rather than having it silently
    dropped and believing it took effect.
    """

    model_config = ConfigDict(extra="forbid")


class EmailRequest(AuthRequest):
    """A request carrying an address. Normalised here, and checked again by the
    `email = lower(email)` and `position('@' in email) > 1` constraints on the
    table: shape is not a security property, so there is no third-party
    validator here for it."""

    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _normalise(cls, value: str) -> str:
        cleaned = value.strip().lower()
        local, _, domain = cleaned.partition("@")
        if not local or not domain or "." not in domain or " " in cleaned:
            raise ValueError("that does not look like an email address")
        return cleaned


class LoginRequest(EmailRequest):
    password: str = Field(min_length=1, max_length=1024)
    totp_code: str | None = Field(default=None, max_length=16)


class SwitchClientRequest(AuthRequest):
    client: str = Field(min_length=1, max_length=64, description="The client's slug.")


class PasswordChangeRequest(AuthRequest):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class TotpConfirmRequest(AuthRequest):
    code: str = Field(min_length=6, max_length=16)


class TotpDisableRequest(AuthRequest):
    password: str = Field(min_length=1, max_length=1024)


class InviteCreateRequest(EmailRequest):
    role: Role
    organisation: str | None = Field(
        default=None, description="Organisation slug. Inputiv admins only; defaults to your own."
    )
    reissue: bool = Field(
        default=False,
        description=(
            "Replace an invitation this address already has. Without it, a second "
            "invitation is refused: two live tokens for one address means whoever holds "
            "the older email uses it and the newer one then fails."
        ),
    )


class InviteAcceptRequest(AuthRequest):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str | None = Field(default=None, max_length=200)


class OrganisationView(BaseModel):
    slug: str
    name: str
    kind: OrganisationKind


class ClientView(BaseModel):
    """A client as the browser sees it: slug and name, never an id.

    The engine resolves the slug itself, as it does for the model (CLAUDE.md,
    "Tenancy"). Nothing outside the database needs the UUID.
    """

    slug: str
    name: str


class MeView(BaseModel):
    email: str
    display_name: str | None
    role: Role
    organisation: OrganisationView
    client: ClientView | None
    csrf_token: str
    totp_enabled: bool
    second_factor_required: bool = False
    """True for a staff account that must enrol before it can reach anything of a
    client's (`TOTP_REQUIRED_FOR_INPUTIV`): the page sends them to Settings."""
    may_switch_client: bool
    session_expires_at: dt.datetime


def _me(scope: Scope) -> MeView:
    view: ClientView | None = None
    if scope.client_id is not None and scope.client_slug is not None:
        view = ClientView(slug=scope.client_slug, name=scope.client_name or scope.client_slug)
    return MeView(
        email=scope.user.email,
        display_name=scope.user.display_name,
        role=scope.role,
        organisation=OrganisationView(
            slug=scope.organisation.slug,
            name=scope.organisation.name,
            kind=scope.organisation.kind,
        ),
        client=view,
        csrf_token=scope.session.csrf_token,
        totp_enabled=scope.user.totp_enabled,
        second_factor_required=(
            get_settings().totp_required_for_inputiv
            and scope.role.is_inputiv
            and not scope.user.totp_enabled
        ),
        may_switch_client=scope.may_switch_client,
        session_expires_at=scope.session.expires_at,
    )


def _client_address(request: Request) -> str | None:
    return request.client.host if request.client else None


# --- signing in and out --------------------------------------------------------


@router.post("/login", response_model=MeView)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(PUBLIC)],
) -> MeView:
    """Sign in. Sets the session cookie and returns the session's CSRF token.

    Every refusal says the same thing, whatever the cause, so this cannot be
    used to find out which addresses have accounts. The audit row says which.
    """
    settings: Settings = get_settings()
    address = _client_address(request) or "unknown"
    if not LOGIN_LIMITER.check(address):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many sign-in attempts; wait a moment and try again",
            headers={"Retry-After": str(LOGIN_LIMITER.retry_after(address))},
        )

    attempt = LoginAttempt(
        email=payload.email, password=payload.password, totp_code=payload.totp_code
    )
    try:
        user = await authenticate(db, attempt, settings=settings)
    except LoginRefused as refusal:
        existing = (
            await db.execute(select(User).where(User.email == payload.email))
        ).scalar_one_or_none()
        await record_failure(
            db,
            email=payload.email,
            reason=refusal.reason,
            user=existing,
            settings=settings,
            ip_address=address,
        )
        await db.commit()
        code = (
            status.HTTP_401_UNAUTHORIZED
            if not isinstance(refusal, AccountLocked)
            else status.HTTP_423_LOCKED
        )
        if isinstance(refusal, SecondFactorRequired):
            # The password was right, so saying a code is needed gives nothing
            # away that the enrolment did not already tell this user.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=refusal.message,
                headers={"X-Second-Factor": "required"},
            ) from refusal
        raise HTTPException(status_code=code, detail=refusal.message) from refusal

    session_row, token = await create_session(
        db,
        user,
        settings=settings,
        ip_address=address,
        user_agent=request.headers.get("user-agent"),
    )
    await write_login_success(db, user, session_id=session_row.id, ip_address=address)
    await db.commit()
    LOGIN_LIMITER.forget(address)
    set_session_cookie(response, token, settings=settings)
    scope = Scope(
        user=user,
        organisation=user.organisation,
        session=session_row,
        client_id=user.organisation.client_id,
        client_slug=user.organisation.client.slug if user.organisation.client else None,
        client_name=user.organisation.client.name if user.organisation.client else None,
        site_id=user.site_id,
    )
    return _me(scope)


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> Response:
    """End this session. The row stays, revoked, as part of the record."""
    await revoke_session(db, scope.session)
    await record_event(
        db,
        AuditContext.new(
            actor_id=scope.actor_id,
            actor_user_id=scope.user_id,
            interface=Interface.API,
            client_id=scope.client_id,
            client_ref=scope.client_slug,
        ),
        event_type=AuditEventType.LOGOUT,
        outcome=AuditOutcome.SUCCESS,
        payload={"session_id": str(scope.session.id)},
    )
    await db.commit()
    clear_session_cookie(response, settings=get_settings())
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=MeView)
async def me(scope: Annotated[Scope, Depends(SIGNED_IN)]) -> MeView:
    """Who this session is, which client it is pointed at, and its CSRF token."""
    return _me(scope)


# --- which client staff are working on ------------------------------------------


@router.get("/clients", response_model=list[ClientView])
async def switchable_clients(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> list[ClientView]:
    """The clients this session may switch to.

    For a franchisee user that is exactly their own, always: the list is not a
    way to discover that other franchisees exist.
    """
    if not scope.may_switch_client:
        client = scope.organisation.client
        return [ClientView(slug=client.slug, name=client.name)] if client else []
    rows = (
        (await db.execute(select(Client).where(Client.is_active.is_(True)).order_by(Client.name)))
        .scalars()
        .all()
    )
    return [ClientView(slug=row.slug, name=row.name) for row in rows]


@router.post("/switch-client", response_model=MeView)
async def switch_client(
    payload: SwitchClientRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SELECT_CLIENT)],
) -> MeView:
    """Point this session at one franchisee. Inputiv staff only, and audited.

    This is the only way an Inputiv session acquires a client, which is what
    makes "which tenant is this request about" a decision somebody made and a
    row somebody can read, rather than a field in a body.
    """
    target = (
        await db.execute(select(Client).where(Client.slug == payload.client.strip().lower()))
    ).scalar_one_or_none()
    if target is None or not target.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no active client with that slug"
        )

    previous = scope.session.acting_client_id
    scope.session.acting_client_id = target.id
    await record_event(
        db,
        AuditContext.new(
            actor_id=scope.actor_id,
            actor_user_id=scope.user_id,
            interface=Interface.API,
            client_id=target.id,
            client_ref=target.slug,
        ),
        event_type=AuditEventType.CLIENT_SWITCHED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "session_id": str(scope.session.id),
            "from_client_id": str(previous) if previous else None,
            "to_client_id": str(target.id),
            "to_client_slug": target.slug,
        },
    )
    await db.commit()
    return _me(
        Scope(
            user=scope.user,
            organisation=scope.organisation,
            session=scope.session,
            client_id=target.id,
            client_slug=target.slug,
            client_name=target.name,
            site_id=scope.site_id,
        )
    )


# --- the caller's own credentials -------------------------------------------------


@router.post("/password", status_code=204)
async def change_password(
    payload: PasswordChangeRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> Response:
    """Change your own password. Every other session of yours is ended."""
    settings = get_settings()
    if not verify_password(
        scope.user.password_hash, payload.current_password, settings=settings
    ).ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="that is not your password"
        )
    try:
        scope.user.password_hash = hash_password(payload.new_password, settings=settings)
    except WeakPassword as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    scope.user.password_changed_at = dt.datetime.now(dt.UTC)
    ended = await revoke_all_for_user(db, scope.user_id)
    # This session included: changing a password signs you in again everywhere.
    await record_user_change(
        db,
        actor=scope.user,
        subject=scope.user,
        change="password",
        detail={"sessions_ended": ended},
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class TotpStartView(BaseModel):
    secret: str = Field(description="Shown once. Type it into an authenticator app.")
    otpauth_uri: str


@router.post("/totp/start", response_model=TotpStartView)
async def totp_start(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> TotpStartView:
    """Begin enrolling a second factor. Not in force until confirmed.

    The secret is returned exactly once, here. It is stored encrypted and no
    endpoint gives it back, so an abandoned enrolment is started again rather
    than recovered.
    """
    if scope.user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a second factor is already enabled; disable it before enrolling another",
        )
    settings = get_settings()
    secret = new_secret()
    scope.user.totp_secret = seal_secret(secret)
    scope.user.totp_confirmed_at = None
    await db.commit()
    return TotpStartView(
        secret=secret,
        otpauth_uri=provisioning_uri(secret, email=scope.user.email, issuer=settings.totp_issuer),
    )


@router.post("/totp/confirm", status_code=204)
async def totp_confirm(
    payload: TotpConfirmRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> Response:
    """Prove a code from the enrolled secret. Only now is the factor required."""
    if scope.user.totp_secret is None:
        raise HTTPException(status_code=409, detail="start an enrolment first")
    if not verify_code(open_secret(scope.user.totp_secret), payload.code):
        raise HTTPException(status_code=403, detail="that code is not right; try the next one")
    scope.user.totp_confirmed_at = dt.datetime.now(dt.UTC)
    await record_user_change(db, actor=scope.user, subject=scope.user, change="totp_enabled")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/totp/disable", status_code=204)
async def totp_disable(
    payload: TotpDisableRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(SIGNED_IN)],
) -> Response:
    """Turn off your second factor. Needs your password, and ends your sessions."""
    if not verify_password(scope.user.password_hash, payload.password, settings=get_settings()).ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="that is not your password"
        )
    scope.user.totp_secret = None
    scope.user.totp_confirmed_at = None
    ended = await revoke_all_for_user(db, scope.user_id)
    await record_user_change(
        db,
        actor=scope.user,
        subject=scope.user,
        change="totp_disabled",
        detail={"sessions_ended": ended},
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- invites ---------------------------------------------------------------------


class InviteView(BaseModel):
    id: str
    email: str
    role: Role
    organisation: str
    expires_at: dt.datetime
    accepted_at: dt.datetime | None
    revoked_at: dt.datetime | None


class InviteCreatedView(InviteView):
    token: str = Field(description="Shown once. Send it to the person by a separate channel.")
    url: str = Field(
        description=(
            "The link to send. A bare token is not usable by the person receiving it; "
            "this is the page that accepts it."
        )
    )


@router.post("/invites", response_model=InviteCreatedView, status_code=201)
async def invite(
    payload: InviteCreateRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INVITE_ADMIN)],
) -> InviteCreatedView:
    """Invite one address into one organisation.

    A franchisee admin may only invite into their own organisation, and only to
    a franchisee role: naming another organisation, or an Inputiv role, is a
    403 rather than a quiet substitution.
    """
    settings = get_settings()
    organisation = scope.organisation
    if payload.organisation and payload.organisation != scope.organisation.slug:
        if scope.role is not Role.INPUTIV_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="you may only invite people into your own organisation",
            )
        found = (
            await db.execute(select(Organisation).where(Organisation.slug == payload.organisation))
        ).scalar_one_or_none()
        if found is None:
            raise HTTPException(status_code=404, detail="no organisation with that slug")
        organisation = found
    if scope.role is not Role.INPUTIV_ADMIN and payload.role.is_inputiv:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="you may not grant an Inputiv role"
        )

    try:
        row, token = await create_invite(
            db,
            organisation=organisation,
            email=payload.email,
            role=payload.role,
            settings=settings,
            created_by=scope.user,
            reissue=payload.reissue,
        )
    except (InviteRefused, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return InviteCreatedView(
        id=str(row.id),
        email=row.email,
        role=row.role,
        organisation=organisation.slug,
        expires_at=row.expires_at,
        accepted_at=None,
        revoked_at=None,
        token=token,
        url=settings.url_for(f"/invite/{token}"),
    )


@router.get("/invites", response_model=list[InviteView])
async def list_invites(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INVITE_ADMIN)],
) -> list[InviteView]:
    """Open invites. A franchisee admin sees only their own organisation's."""
    query = select(Invite).order_by(Invite.created_at.desc()).limit(200)
    if scope.role is not Role.INPUTIV_ADMIN:
        query = query.where(Invite.organisation_id == scope.organisation.id)
    rows = (await db.execute(query)).scalars().all()
    return [
        InviteView(
            id=str(row.id),
            email=row.email,
            role=row.role,
            organisation=row.organisation.slug,
            expires_at=row.expires_at,
            accepted_at=row.accepted_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


@router.post("/invites/accept", response_model=MeView, status_code=201)
async def accept(
    payload: InviteAcceptRequest,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(PUBLIC)],
) -> MeView:
    """Turn an invite token into an account, and sign in with it."""
    settings = get_settings()
    try:
        user = await accept_invite(
            db,
            token=payload.token,
            password=payload.password,
            display_name=payload.display_name,
        )
    except WeakPassword as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InviteRefused as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    session_row, token = await create_session(
        db,
        user,
        settings=settings,
        ip_address=_client_address(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    set_session_cookie(response, token, settings=settings)
    return _me(
        Scope(
            user=user,
            organisation=user.organisation,
            session=session_row,
            client_id=user.organisation.client_id,
            client_slug=user.organisation.client.slug if user.organisation.client else None,
            client_name=user.organisation.client.name if user.organisation.client else None,
            site_id=user.site_id,
        )
    )


# --- users ------------------------------------------------------------------------


class UserView(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: Role
    status: UserStatus
    organisation: str
    totp_enabled: bool
    last_login_at: dt.datetime | None


class UserStatusRequest(AuthRequest):
    status: Literal[UserStatus.ACTIVE, UserStatus.SUSPENDED]


@router.get("/users", response_model=list[UserView])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INVITE_ADMIN)],
) -> list[UserView]:
    """Accounts. A franchisee admin sees only their own organisation's."""
    query = select(User).order_by(User.email).limit(500)
    if scope.role is not Role.INPUTIV_ADMIN:
        query = query.where(User.organisation_id == scope.organisation.id)
    rows = (await db.execute(query)).scalars().all()
    return [
        UserView(
            id=str(row.id),
            email=row.email,
            display_name=row.display_name,
            role=row.role,
            status=row.status,
            organisation=row.organisation.slug,
            totp_enabled=row.totp_enabled,
            last_login_at=row.last_login_at,
        )
        for row in rows
    ]


@router.post("/users/{user_id}/status", response_model=UserView)
async def set_user_status(
    user_id: str,
    payload: UserStatusRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ADMIN)],
) -> UserView:
    """Suspend or reinstate an account. Suspending ends its sessions at once."""
    try:
        target_id = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="no such user") from exc
    target = (await db.execute(select(User).where(User.id == target_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="no such user")
    if target.id == scope.user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="you cannot suspend your own account"
        )
    target.status = payload.status
    ended = 0
    if payload.status is UserStatus.SUSPENDED:
        ended = await revoke_all_for_user(db, target.id)
    await record_user_change(
        db,
        actor=scope.user,
        subject=target,
        change=f"status:{payload.status.value}",
        detail={"sessions_ended": ended},
    )
    await db.commit()
    return UserView(
        id=str(target.id),
        email=target.email,
        display_name=target.display_name,
        role=target.role,
        status=target.status,
        organisation=target.organisation.slug,
        totp_enabled=target.totp_enabled,
        last_login_at=target.last_login_at,
    )

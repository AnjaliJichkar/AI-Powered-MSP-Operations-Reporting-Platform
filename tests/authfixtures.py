"""Signing a test in, without a database.

Most API tests are about what a route does once it knows who is calling, not
about how it found out. They install a `Scope` directly in place of the real
resolver, which keeps them fast and keeps the cookie, the session row and the
CSRF token in the tests that are actually about those
(`tests/api/test_authentication.py`, against a real database).

The override is not a blanket "allow everything": it applies each route's own
`ScopeRule`, so a test that signs in as a viewer still gets the 403 a viewer
would get. The only thing it skips is proving the identity.

Every rule is collected from the application itself rather than from a list
kept here, so a rule added later is covered without anybody remembering to add
it — the same reason `undeclared_routes` walks the app.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import FastAPI, HTTPException, status

from app.auth.dependencies import ScopeRule, _rules_of, api_routes
from app.auth.scope import Scope
from app.models.enums import IdentityProvider, OrganisationKind, Role, UserStatus
from app.models.organisation import Organisation, Session, User

DEFAULT_CLIENT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
DEFAULT_CLIENT_SLUG = "teamlogic-austin"


def make_scope(
    *,
    role: Role = Role.INPUTIV_ENGINEER,
    client_id: uuid.UUID | None = DEFAULT_CLIENT_ID,
    client_slug: str | None = DEFAULT_CLIENT_SLUG,
    client_name: str | None = "TeamLogic IT of Austin",
    email: str = "engineer@inputiv.com",
    user_id: uuid.UUID | None = None,
    organisation_slug: str | None = None,
    csrf_token: str = "test-csrf-token",
) -> Scope:
    """A `Scope` as the resolver would have built it, with unattached rows."""
    kind = role.organisation_kind
    organisation = Organisation(
        id=uuid.uuid4(),
        kind=kind,
        slug=organisation_slug or ("inputiv" if kind is OrganisationKind.INPUTIV else "acme-it"),
        name="Inputiv" if kind is OrganisationKind.INPUTIV else "Acme IT",
        client_id=None if kind is OrganisationKind.INPUTIV else client_id,
        is_active=True,
        identity_provider=IdentityProvider.LOCAL,
        identity_provider_config={},
    )
    user = User(
        id=user_id or uuid.uuid4(),
        organisation_id=organisation.id,
        organisation_kind=kind,
        email=email,
        display_name=None,
        role=role,
        status=UserStatus.ACTIVE,
        identity_provider=IdentityProvider.LOCAL,
        failed_login_count=0,
    )
    user.organisation = organisation
    now = dt.datetime.now(dt.UTC)
    session = Session(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash="not-a-real-token",
        csrf_token=csrf_token,
        created_at=now,
        last_seen_at=now,
        expires_at=now + dt.timedelta(hours=12),
        acting_client_id=client_id if kind is OrganisationKind.INPUTIV else None,
    )
    session.user = user
    return Scope(
        user=user,
        organisation=organisation,
        session=session,
        client_id=client_id,
        client_slug=client_slug,
        client_name=client_name,
        site_id=None,
    )


def rules_of(app: FastAPI) -> set[ScopeRule]:
    """Every scope rule the application's routes declare."""
    return {rule for route in api_routes(app) for rule in _rules_of(route)}


def _override(rule: ScopeRule, scope: Scope):  # type: ignore[no-untyped-def]
    """The rule's own decision, made against a scope that is simply given."""

    async def resolve() -> Scope | None:
        if rule.public:
            return None
        if scope.role not in rule.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{scope.role.value} may not use this",
            )
        if rule.needs_client and scope.client_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="no client is selected for this session",
            )
        return scope

    return resolve


def sign_in(app: FastAPI, scope: Scope | None = None, **fields: object) -> Scope:
    """Make every route believe this scope is the caller. Returns it."""
    resolved = scope or make_scope(**fields)  # type: ignore[arg-type]
    for rule in rules_of(app):
        app.dependency_overrides[rule] = _override(rule, resolved)
    return resolved


def sign_out(app: FastAPI) -> None:
    """Remove the overrides, so the real resolver answers again."""
    for rule in rules_of(app):
        app.dependency_overrides.pop(rule, None)

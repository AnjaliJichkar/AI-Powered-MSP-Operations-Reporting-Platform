"""What one authenticated request is allowed to touch.

A `Scope` is the answer to "who is this, and which tenant are they working on",
resolved once per request from the session cookie and never from the request
body. It is the authorization counterpart of `ClientScope` in
`app/connectors/scope.py`: that one decides which vendor records may be
returned, this one decides which client the request may ask about at all.

The scope is **(organisation, optional site)**. The site half is carried
everywhere and used nowhere yet: no user is pinned to a site today. It is
modelled now because retrofitting a second axis after routes and tests assume
one is the kind of change that gets done incompletely.

How the client is decided, and it is only ever one of two ways:

- a **franchisee** user's client is their organisation's, and cannot be changed
  by anything they send;
- an **Inputiv** user's client is whichever one their session is pointed at,
  set only by `POST /auth/switch-client`, which writes an audit row every time.

Neither reads `client_id` from a query string or a body. That is the whole
point: before this, any caller could name any client.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models.enums import OrganisationKind, Role
from app.models.organisation import Organisation, Session, User

#: Everyone who works at Inputiv.
INPUTIV_ROLES: frozenset[Role] = frozenset({Role.INPUTIV_ADMIN, Role.INPUTIV_ENGINEER})
#: Everyone who works at a franchisee.
FRANCHISEE_ROLES: frozenset[Role] = frozenset(
    {Role.FRANCHISEE_ADMIN, Role.FRANCHISEE_TECHNICIAN, Role.FRANCHISEE_VIEWER}
)
#: Every role there is. A rule that names this still names it explicitly.
EVERY_ROLE: frozenset[Role] = INPUTIV_ROLES | FRANCHISEE_ROLES

#: Roles that may make the engine call a model on the customer's data, and with it
#: the work a model call belongs to: Ask, the ticket workspace, refreshing the
#: coverage board and excluding a device from a tool. A viewer reads what has
#: already been produced and starts nothing.
CAN_RUN_MODEL: frozenset[Role] = INPUTIV_ROLES | {
    Role.FRANCHISEE_ADMIN,
    Role.FRANCHISEE_TECHNICIAN,
}

#: Roles that see a review before it has been released: the drafting screen,
#: the fact-id trace, held findings, internal render modes.
CAN_SEE_DRAFTS: frozenset[Role] = INPUTIV_ROLES

#: Roles that may make a review of their own customers: generate it, settle the
#: findings that were held, render the client copy and release it.
#:
#: A franchisee's administrator is on this list and the other two franchisee
#: roles are not. The administrator is the person who presents the review to
#: their own customer, so the decision of when that customer sees it is theirs;
#: a technician fixes machines and a viewer reads what has been released.
#:
#: **It is not the same list as `CAN_SEE_DRAFTS`, and the difference is the
#: point.** Being allowed to make a review is not being allowed to see how it
#: was made: the fact-id trace, the held findings' workings and the internal
#: render mode stay with `CAN_SEE_DRAFTS`, which is Inputiv alone. What a
#: franchisee's administrator acts on is the review as the customer will read
#: it, plus the findings they are being asked to settle.
CAN_MAKE_REVIEWS: frozenset[Role] = INPUTIV_ROLES | {Role.FRANCHISEE_ADMIN}


class AuthError(Exception):
    """Base for anything that stops a request at the door."""


class NotAuthenticated(AuthError):
    """No usable session. The answer is 401 and a hint to sign in again."""


class AccessDenied(AuthError):
    """Authenticated, and not allowed. The answer is 403 and an audit row."""


class NoClientSelected(AuthError):
    """Inputiv staff asked a client-scoped question without switching to one."""


@dataclass(frozen=True, slots=True)
class Scope:
    """One authenticated request's identity and tenant."""

    user: User
    organisation: Organisation
    session: Session
    client_id: uuid.UUID | None
    client_slug: str | None
    client_name: str | None
    """The client's display name. Carried rather than looked up again: for
    Inputiv staff the client is not their organisation's, so there is no
    relationship to read it from, and printing the slug instead reads as a
    missing name rather than as a deliberate one."""
    site_id: uuid.UUID | None
    """The one site this user is restricted to. Always None today."""

    @property
    def role(self) -> Role:
        return self.user.role

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def actor_id(self) -> str:
        """What goes in `audit_log.actor_id`: the user's id, not a typed handle."""
        return str(self.user.id)

    @property
    def is_inputiv(self) -> bool:
        return self.organisation.kind is OrganisationKind.INPUTIV

    @property
    def may_switch_client(self) -> bool:
        return self.is_inputiv

    @property
    def sees_drafts(self) -> bool:
        """Whether unreleased reviews, internal render modes and the fact-id
        trace are visible. Franchisee users see a review only once it has been
        released to them, and never the workings behind it."""
        return self.role in CAN_SEE_DRAFTS

    def require_client(self) -> uuid.UUID:
        """The client this request is about, or a refusal naming what to do."""
        if self.client_id is None:
            raise NoClientSelected(
                "no client is selected for this session; "
                "POST /auth/switch-client with the client's slug first"
            )
        return self.client_id

    def owns_client(self, client_id: uuid.UUID) -> bool:
        """Whether this scope may act on a given client. Used by anything that
        resolves a client by another route (a report's stored client_id, say)."""
        return self.client_id is not None and self.client_id == client_id

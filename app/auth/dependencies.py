"""The scope resolver: one dependency, and every route declares against it.

There is exactly one way to find out who is calling and what they may touch,
and it is `ScopeRule`. A route says what it needs:

    @router.post("/chat")
    async def chat(request: ChatRequest, scope: Scope = Depends(CAN_CHAT)) -> ...:

and gets back a resolved `Scope` or no response at all. A route that needs no
identity still says so, with `Depends(PUBLIC)`, because **the check is that
every route declared something**, not that every route is protected.
`undeclared_routes` enumerates the application and returns the ones that
declared nothing; `create_app` refuses to build with any, and a test asserts
the same. Forgetting is therefore not a quiet hole: it is a server that does
not start.

What a rule does, in order, and it stops at the first refusal:

1. resolves the session from the cookie — never from a body, a query string or
   a header the caller controls;
2. refuses a user or organisation that is not active, as if the session had
   expired, because from the caller's side it has;
3. checks the CSRF token on anything that changes state;
4. checks the role against the ones the route named;
5. resolves the client: a franchisee user's own, or the one an Inputiv user's
   session has been switched to.

`client_id` is therefore an output of this function, never an input to it.
Request bodies do not carry one, and the models forbid unknown fields so that a
caller who sends one gets a 422 rather than having it quietly ignored.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.auth.scope import (
    CAN_MAKE_REVIEWS as CAN_MAKE_REVIEWS_ROLES,
)
from app.auth.scope import (
    CAN_RUN_MODEL,
    EVERY_ROLE,
    INPUTIV_ROLES,
    Scope,
)
from app.auth.sessions import CSRF_HEADER, UNSAFE_METHODS, load_session, touch
from app.db.session import get_session
from app.models.enums import AuditEventType, AuditOutcome, Interface, Role, UserStatus
from app.security.passwords import constant_time_equals
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _unauthenticated(settings: Settings) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="sign in to continue",
        headers={"X-Login-Path": settings.login_path},
    )


class ScopeRule:
    """One route's declaration of who may call it, and what it is scoped to."""

    def __init__(
        self,
        *,
        label: str,
        roles: frozenset[Role] = frozenset(),
        needs_client: bool = True,
        public: bool = False,
        second_factor_exempt: bool = False,
    ) -> None:
        if public and roles:
            raise ValueError("a public rule names no roles")
        if not public and not roles:
            raise ValueError(
                f"rule {label!r} names no roles: a rule that allows everyone must say "
                "roles=EVERY_ROLE explicitly, so that allowing everyone is a decision"
            )
        self.label = label
        self.roles = roles
        self.needs_client = needs_client and not public
        self.public = public
        self.second_factor_exempt = second_factor_exempt
        """Reachable by a staff account that has not yet enrolled a second factor
        when `TOTP_REQUIRED_FOR_INPUTIV` is on: the routes enrolment itself needs."""

    def __repr__(self) -> str:
        return f"<ScopeRule {self.label}>"

    async def __call__(
        self,
        request: Request,
        db: Annotated[AsyncSession, Depends(get_session)],
    ) -> Scope | None:
        settings = get_settings()
        if self.public:
            return None

        now = dt.datetime.now(dt.UTC)
        token = request.cookies.get(settings.session_cookie_name)
        session_row = await load_session(db, token, settings=settings, now=now)
        if session_row is None:
            raise _unauthenticated(settings)

        user = session_row.user
        organisation = user.organisation
        if user.status is not UserStatus.ACTIVE or not organisation.is_active:
            # Suspended between requests. Indistinguishable from an expired
            # session on purpose: the caller is told to sign in, and the audit
            # trail already holds why they cannot.
            raise _unauthenticated(settings)

        if request.method in UNSAFE_METHODS:
            supplied = request.headers.get(CSRF_HEADER, "")
            if not supplied or not constant_time_equals(supplied, session_row.csrf_token):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"missing or wrong {CSRF_HEADER}; read it from GET /auth/me and "
                        "send it on anything that changes state"
                    ),
                )

        if user.role not in self.roles:
            await self._deny(
                db,
                request,
                user_id=user.id,
                reason="role",
                detail={"role": user.role.value, "allowed": sorted(r.value for r in self.roles)},
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{user.role.value} may not use this",
            )

        if (
            settings.totp_required_for_inputiv
            and user.role.is_inputiv
            and not user.totp_enabled
            and not self.second_factor_exempt
        ):
            # Staff without a confirmed second factor reach their own account
            # and nothing of a client's, until they have enrolled.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Inputiv staff must enrol a second factor before using this: "
                    "open Settings and add an authenticator app"
                ),
                headers={"X-Second-Factor": "enrol"},
            )

        client_id = (
            session_row.acting_client_id
            if organisation.client_id is None
            else organisation.client_id
        )
        client_slug: str | None = None
        client_name: str | None = None
        if client_id is not None:
            if organisation.client is not None and organisation.client.id == client_id:
                client_slug = organisation.client.slug
                client_name = organisation.client.name
            else:
                from app.connectors.errors import UnknownClient
                from app.connectors.factory import resolve_client  # local: avoids a cycle

                try:
                    found = await resolve_client(db, client_id)
                    client_slug, client_name = found.slug, found.name
                except UnknownClient:
                    # The client was deactivated while the session pointed at
                    # it. Treat it as no selection rather than as an error on
                    # an unrelated route.
                    client_id = None

        if self.needs_client and client_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "no client is selected for this session; "
                    "POST /auth/switch-client with the client's slug first"
                ),
            )

        await touch(db, session_row, now=now)
        scope = Scope(
            user=user,
            organisation=organisation,
            session=session_row,
            client_id=client_id,
            client_slug=client_slug,
            client_name=client_name,
            site_id=user.site_id,
        )
        # Kept for anything that needs the actor outside the signature, such as
        # an exception handler turning a refusal into an audited row.
        request.state.scope = scope
        return scope

    async def _deny(
        self,
        db: AsyncSession,
        request: Request,
        *,
        user_id: Any,
        reason: str,
        detail: dict[str, Any],
    ) -> None:
        """Record a refusal. A denial is a security event and is not silent."""
        await record_event(
            db,
            AuditContext.new(actor_id=str(user_id), actor_user_id=user_id, interface=Interface.API),
            event_type=AuditEventType.ACCESS_DENIED,
            outcome=AuditOutcome.DENIED,
            payload={
                "route": request.url.path,
                "method": request.method,
                "rule": self.label,
                "reason": reason,
                **detail,
            },
        )
        await db.commit()


# --- the rules every route picks from ----------------------------------------
#
# Named rather than built inline, so the set of answers to "who may do this" is
# a short list that can be read in one place and audited as a whole.

PUBLIC = ScopeRule(label="public", public=True)
"""No identity: health, sign-in, accepting an invite, the login page."""

SIGNED_IN = ScopeRule(
    label="signed-in", roles=EVERY_ROLE, needs_client=False, second_factor_exempt=True
)
"""Any active account, with no client needed: who am I, sign out, my second factor,
the page shells. The one rule a staff account reaches before it has enrolled a
second factor, because enrolment lives behind it."""

SELECT_CLIENT = ScopeRule(label="select-client", roles=INPUTIV_ROLES, needs_client=False)
"""Inputiv staff choosing which franchisee to work on."""

INPUTIV_ANY = ScopeRule(label="inputiv-any", roles=INPUTIV_ROLES)
"""Inputiv staff, on the client they have switched to."""

INPUTIV_ADMIN = ScopeRule(
    label="inputiv-admin", roles=frozenset({Role.INPUTIV_ADMIN}), needs_client=False
)
"""Account administration: organisations, users, invites."""

CAN_CHAT = ScopeRule(label="can-chat", roles=CAN_RUN_MODEL)
"""Anything that runs a model against the client's data. Not a viewer."""

CLIENT_READ = ScopeRule(label="client-read", roles=EVERY_ROLE)
"""Reading within the scoped client: reviews, sites, the audit trail. What each
role actually sees inside that is narrowed again by the route."""

CAN_MANAGE_COVERAGE = ScopeRule(label="can-manage-coverage", roles=CAN_RUN_MODEL)
"""Excluding a device from a coverage tool, or withdrawing that: a franchisee's
administrator and Inputiv staff. A viewer reads the board and changes nothing."""

CAN_MAKE_REVIEWS = ScopeRule(label="can-make-reviews", roles=CAN_MAKE_REVIEWS_ROLES)
"""Generating a review, settling its held findings, rendering the client copy and
releasing it: Inputiv staff and a franchisee's own administrator, on their own
customers. **The rule is the door and not the whole lock** — inside it the route
narrows again by role, because a franchisee's administrator may do four of these
things and an engineer may do all of them (`app/api/reports.py`)."""


def _rules_of(route: APIRoute) -> list[ScopeRule]:
    """Every `ScopeRule` in a route's dependency tree, however it was declared."""
    found: list[ScopeRule] = []

    def walk(dependant: Any) -> None:
        call = getattr(dependant, "call", None)
        if isinstance(call, ScopeRule):
            found.append(call)
        for child in getattr(dependant, "dependencies", ()):
            walk(child)

    walk(route.dependant)
    return found


def api_routes(app: FastAPI) -> list[APIRoute]:
    """Every `APIRoute` the application serves, however deeply nested.

    Written as a recursive walk rather than a read of `app.routes`, because an
    included router is not flattened into that list on every FastAPI version:
    on this one it appears as a wrapper holding its own `routes`. A check that
    iterated the top level would find three documentation endpoints, report no
    problems, and be worth nothing — which is the failure mode this whole
    function exists to prevent, so it is worth the recursion.
    """
    found: list[APIRoute] = []
    seen: set[int] = set()

    def children(node: Any) -> Iterable[Any]:
        # The attribute that holds the real routes differs by FastAPI version
        # and by wrapper: `_IncludedRouter` (0.141) keeps them behind
        # `original_router`, a `Mount` behind `app`, a router behind `routes`.
        # Trying each is cheap and survives the next rename.
        for attribute in ("routes", "original_router", "router", "app"):
            value = getattr(node, attribute, None)
            if value is None or isinstance(value, str):
                continue
            if isinstance(value, list | tuple):
                yield from value
            elif hasattr(value, "routes"):
                yield value

    def walk(nodes: Iterable[Any]) -> None:
        for node in nodes:
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, APIRoute):
                found.append(node)
                continue
            walk(children(node))

    walk(app.routes)
    return found


def undeclared_routes(app: FastAPI) -> list[str]:
    """Routes with no scope declaration. Empty is the only acceptable answer.

    A route is listed when it declares no rule at all, or more than one: two
    rules on one route means the stricter might be the one that is not reached,
    and which that is depends on dependency ordering nobody should have to
    reason about.
    """
    problems: list[str] = []
    routes = api_routes(app)
    if not routes:
        # An empty answer here would pass every caller silently.
        return ["no API routes were found to check, which cannot be right"]
    for route in routes:
        rules = _rules_of(route)
        methods = ",".join(sorted(route.methods or {"GET"}))
        if not rules:
            problems.append(f"{methods} {route.path} declares no scope")
        elif len(rules) > 1:
            names = ", ".join(rule.label for rule in rules)
            problems.append(f"{methods} {route.path} declares {len(rules)} scopes: {names}")
    return problems


def duplicate_routes(app: FastAPI) -> list[str]:
    """Paths claimed twice for the same method. Empty is the only right answer.

    Two routes on one method and path resolve by registration order, so which
    answers depends on the order the routers happened to be included. That is
    not something anybody should have to know, and the loser disappears without
    an error anywhere: a page and its own JSON endpoint both called `/sites`
    put the page beyond reach and said nothing.
    """
    seen: dict[tuple[str, str], int] = {}
    for route in api_routes(app):
        for method in route.methods or {"GET"}:
            key = (method, route.path)
            seen[key] = seen.get(key, 0) + 1
    return [
        f"{method} {path} is declared {count} times"
        for (method, path), count in seen.items()
        if count > 1
    ]


def assert_scopes_declared(app: FastAPI) -> None:
    """Refuse to build an application with an undeclared or a duplicated route."""
    problems = undeclared_routes(app)
    if problems:
        raise RuntimeError(
            "every route must declare a scope with Depends(<ScopeRule>), including "
            "Depends(PUBLIC) for one that needs no identity. Missing:\n  " + "\n  ".join(problems)
        )
    clashes = duplicate_routes(app)
    if clashes:
        raise RuntimeError(
            "two routes cannot claim the same method and path; which one answers would "
            "depend on the order the routers were included:\n  " + "\n  ".join(clashes)
        )


def roles_named(rules: Iterable[ScopeRule]) -> set[str]:
    """Every role any of these rules allows. For documentation and tests."""
    return {role.value for rule in rules for role in rule.roles}

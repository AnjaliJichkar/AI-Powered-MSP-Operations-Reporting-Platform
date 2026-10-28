"""Every route declares a scope, and the check that says so is not vacuous.

This is the deny-by-default guarantee. A route added without a declaration is
not a route that quietly runs for anybody: `create_app` refuses to build, and
this names it.

The second test is the one that matters most. An enumeration that walks the
wrong attribute finds nothing, reports no problems and is worth nothing — which
is what happened on the first attempt here, because this FastAPI version keeps
an included router's routes behind a wrapper rather than in `app.routes`. So
the count is asserted too, and a route with a deliberately missing declaration
is added to a throwaway application to prove the check fails when it should.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI

from app.auth.dependencies import (
    PUBLIC,
    ScopeRule,
    _rules_of,
    api_routes,
    assert_scopes_declared,
    duplicate_routes,
    undeclared_routes,
)
from app.main import create_app
from app.settings import Settings

#: Below this, the walk has clearly found a fraction of the application and the
#: "no problems" answer means nothing. Not a pin on the exact number: adding a
#: route should not fail this test, forgetting to declare one should.
MINIMUM_ROUTES = 25


def test_every_route_declares_a_scope(settings: Settings) -> None:
    app = create_app(settings)
    assert undeclared_routes(app) == []


def test_the_check_actually_reaches_every_route(settings: Settings) -> None:
    """A route the walk cannot see is a route nobody checked."""
    app = create_app(settings)
    paths = {route.path for route in api_routes(app)}

    assert len(paths) >= MINIMUM_ROUTES, sorted(paths)
    # One from each router, including the ones that are not in the schema.
    for path in (
        "/health",
        "/auth/login",
        "/chat",
        "/conversations",
        "/reports/tbr",
        "/audit",
        "/",
    ):
        assert path in paths, sorted(paths)


def test_a_route_without_a_declaration_is_reported() -> None:
    """The failure mode this exists for, reproduced."""
    app = FastAPI()
    router = APIRouter()

    @router.get("/forgotten")
    async def forgotten() -> dict[str, str]:
        return {}

    @router.get("/declared")
    async def declared(_: None = Depends(PUBLIC)) -> dict[str, str]:
        return {}

    app.include_router(router)

    problems = undeclared_routes(app)
    assert problems == ["GET /forgotten declares no scope"]
    with pytest.raises(RuntimeError, match="/forgotten"):
        assert_scopes_declared(app)


def test_two_declarations_on_one_route_are_reported() -> None:
    """Which of two rules wins depends on ordering nobody should reason about."""
    app = FastAPI()
    router = APIRouter()
    other = ScopeRule(label="other", public=True)

    @router.get("/confused", dependencies=[Depends(other)])
    async def confused(_: None = Depends(PUBLIC)) -> dict[str, str]:
        return {}

    app.include_router(router)
    problems = undeclared_routes(app)
    assert len(problems) == 1 and "declares 2 scopes" in problems[0]


def test_an_application_with_no_routes_is_not_a_pass() -> None:
    """An empty answer from an empty walk would pass every caller silently."""
    assert undeclared_routes(FastAPI()) != []


def test_a_rule_that_allows_everyone_has_to_say_so() -> None:
    """`roles=()` would read as "no restriction" and mean "nobody"."""
    with pytest.raises(ValueError, match="names no roles"):
        ScopeRule(label="oops")


def test_no_two_routes_claim_the_same_method_and_path(settings: Settings) -> None:
    """Whichever was included first would answer, and the other would vanish."""
    assert duplicate_routes(create_app(settings)) == []


def test_a_path_claimed_twice_is_reported() -> None:
    """The failure this exists for: a page and its own JSON endpoint both
    called `/sites`, which put the page beyond reach and said nothing."""
    app = FastAPI()
    data = APIRouter()
    pages = APIRouter()

    @data.get("/sites")
    async def sites_json(_: None = Depends(PUBLIC)) -> dict[str, str]:
        return {}

    @pages.get("/sites")
    async def sites_page(_: None = Depends(PUBLIC)) -> dict[str, str]:
        return {}

    app.include_router(data)
    app.include_router(pages)

    assert duplicate_routes(app) == ["GET /sites is declared 2 times"]
    with pytest.raises(RuntimeError, match="same method and path"):
        assert_scopes_declared(app)


# --- which roles each rule lets in -------------------------------------------------------
#
# The enumeration above proves every route declared *something*. This proves the
# something is what we think: a table of rule -> roles, asserted against the live
# application. Adding a role is then a deliberate edit here, not a set that quietly
# widens because it was written as a union somewhere else.

RULE_ROLES: dict[str, set[str]] = {
    "public": set(),
    "signed-in": {
        "inputiv_admin",
        "inputiv_engineer",
        "franchisee_admin",
        "franchisee_technician",
        "franchisee_viewer",
    },
    "client-read": {
        "inputiv_admin",
        "inputiv_engineer",
        "franchisee_admin",
        "franchisee_technician",
        "franchisee_viewer",
    },
    "can-chat": {
        "inputiv_admin",
        "inputiv_engineer",
        "franchisee_admin",
        "franchisee_technician",
    },
    "can-manage-coverage": {
        "inputiv_admin",
        "inputiv_engineer",
        "franchisee_admin",
        "franchisee_technician",
    },
    "audit-read": {"inputiv_admin", "inputiv_engineer", "franchisee_admin"},
    # Making a review of your own customers: generating it, settling the
    # findings held for somebody who knows the estate, approving the Monthly
    # Report and releasing it. Not a technician and not a viewer, and the rule
    # is only the door: the route narrows it again by role.
    "can-make-reviews": {"inputiv_admin", "inputiv_engineer", "franchisee_admin"},
    "invite-admin": {"inputiv_admin", "franchisee_admin"},
    "inputiv-any": {"inputiv_admin", "inputiv_engineer"},
    "select-client": {"inputiv_admin", "inputiv_engineer"},
    "inputiv-admin": {"inputiv_admin"},
}


def _rules_by_label(app: FastAPI) -> dict[str, ScopeRule]:
    found: dict[str, ScopeRule] = {}
    for route in api_routes(app):
        for rule in _rules_of(route):
            found[rule.label] = rule
    return found


def test_each_rule_lets_in_exactly_the_roles_the_table_says(settings: Settings) -> None:
    rules = _rules_by_label(create_app(settings))
    assert set(rules) == set(RULE_ROLES), "a rule was added or renamed; update the table"
    for label, rule in sorted(rules.items()):
        assert {role.value for role in rule.roles} == RULE_ROLES[label], label


def test_the_technician_reaches_the_work_and_not_the_account(settings: Settings) -> None:
    """One role's whole surface, by route rather than by rule: what a franchisee
    technician can and cannot reach, so widening it is a visible change."""
    app = create_app(settings)
    rules = {
        (method, route.path): rule.label
        for route in api_routes(app)
        for rule in _rules_of(route)
        for method in sorted(route.methods or {"GET"})
        if method != "HEAD"
    }
    allowed = {
        route
        for route, label in rules.items()
        if "franchisee_technician" in RULE_ROLES[label] or label == "public"
    }

    # Ask, the device pages, the coverage board with refresh and exclusions, tickets.
    for route in (
        ("POST", "/chat"),
        ("POST", "/chat/stream"),
        ("GET", "/conversations"),
        ("GET", "/devices/list"),
        ("GET", "/devices/{device_id}"),
        ("GET", "/devices/export.csv"),
        ("GET", "/coverage/board"),
        ("GET", "/coverage/sites/{slug}"),
        ("POST", "/coverage/refresh"),
        ("POST", "/coverage/exclusions"),
        ("DELETE", "/coverage/exclusions/{exclusion_id}"),
        ("GET", "/tickets/list"),
        ("POST", "/tickets/workspaces"),
        ("POST", "/tickets/workspaces/{workspace_id}/plans"),
        ("GET", "/reports/tbr"),
        ("GET", "/reports/tbr/{report_id}"),
        ("GET", "/reports/tbr/{report_id}/download"),
    ):
        assert route in allowed, route

    # No accounts, no approving or releasing a review, no Inputiv screens, no audit.
    for route in (
        ("POST", "/auth/invites"),
        ("GET", "/auth/invites"),
        ("GET", "/auth/users"),
        ("POST", "/auth/users/{user_id}/status"),
        ("POST", "/auth/switch-client"),
        ("POST", "/reports/tbr"),
        ("POST", "/reports/tbr/{report_id}/review"),
        ("POST", "/reports/tbr/{report_id}/render"),
        ("GET", "/devices/unresolved"),
        ("POST", "/devices/links"),
        ("GET", "/devices/agreement"),
        ("GET", "/audit"),
    ):
        assert route not in allowed, route

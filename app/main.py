"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

# `from app import connectors`, never `import app.connectors`: the latter binds
# the name `app` (the package) in this module, so `uvicorn app.main:app` would
# resolve to the package and fail with "'module' object is not callable".
from app import __version__
from app import connectors as _connectors  # noqa: F401  registers every tool
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router
from app.api.coverage import router as coverage_router
from app.api.devices import router as devices_router
from app.api.health import router as health_router
from app.api.integrations import router as integrations_router
from app.api.reports import router as reports_router
from app.api.sites import router as sites_router
from app.api.tickets import router as tickets_router
from app.auth.dependencies import assert_scopes_declared
from app.auth.ratelimit import configure_login_limiter
from app.automation.api import router as automation_router
from app.autopilot.api import router as autopilot_router
from app.db.session import dispose_engine
from app.interfaces.web import STATIC_DIR, assert_icons_named
from app.interfaces.web import router as web_router
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down. The engine connects lazily on first use."""
    settings: Settings = get_settings()
    logger.info(
        "starting %s version=%s environment=%s",
        settings.app_name,
        __version__,
        settings.environment,
    )
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("shutdown complete")


async def redirect_unauthenticated(request: Request, exc: Exception) -> Response:
    """A browser asking for a page with no session is sent to sign in.

    Only a page: a `GET` whose `Accept` names HTML. Everything else keeps the
    401 it was given, because an API caller needs the status and a redirect to
    a login form would arrive at a `fetch` as a confusing 200 full of markup.

    Where they were going is carried in `next`, so signing in returns them to
    it rather than to the front page.
    """
    assert isinstance(exc, FastAPIHTTPException)
    wants_page = request.method == "GET" and "text/html" in request.headers.get("accept", "")
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and wants_page:
        target = get_settings().login_path
        destination = request.url.path
        if destination and destination != target:
            target = f"{target}?next={quote(destination, safe='/')}"
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    return await http_exception_handler(request, exc)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Tests call this with an explicit settings object."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="Inputiv Engine",
        version=__version__,
        description="Shared backend for Desk, Resolved and Memory.",
        debug=settings.debug,
        lifespan=lifespan,
        # Docs stay off in production: the tool surface is internal.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings
    configure_login_limiter(limit=settings.login_rate_limit_per_minute)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(audit_router)
    app.include_router(chat_router)
    app.include_router(conversations_router)
    app.include_router(reports_router)
    app.include_router(sites_router)
    app.include_router(integrations_router)
    app.include_router(coverage_router)
    app.include_router(devices_router)
    app.include_router(tickets_router)
    # Before the web router, for the reason `/tickets/list` is: a page route
    # and its own JSON endpoints share a prefix, and whichever router is
    # included first answers.
    app.include_router(autopilot_router)
    app.include_router(automation_router)
    app.include_router(web_router)
    # The stylesheet and the page scripts. A Mount, not a route: it serves
    # files with no tenant data in them, and it is deliberately outside the
    # scope declaration check, which is about routes that answer with data.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.add_exception_handler(FastAPIHTTPException, redirect_unauthenticated)
    # Deny by default, checked at build rather than trusted to review: a
    # route that declares no scope is a hole, so the server does not start
    # with one. `tests/api/test_scope_declarations.py` asserts the same
    # thing, and names the offender.
    assert_scopes_declared(app)
    # The same idea one layer up: the nav is a list in Python and the icons
    # are markup in a template, and a rename that moves one and not the other
    # used to draw the same glyph on every row with no error anywhere.
    assert_icons_named()
    return app


def __getattr__(name: str) -> Any:
    """Build the application on first access to ``app``, not at import.

    Both of these work, and neither reads settings until the server starts:

        uvicorn app.main:app
        uvicorn app.main:create_app --factory --reload

    Importing this module still has no side effects, which is what keeps
    Alembic and the tests from needing a configured environment. PEP 562.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

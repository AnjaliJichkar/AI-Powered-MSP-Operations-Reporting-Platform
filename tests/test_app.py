"""Application factory and the health endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app import __version__
from app.db.session import get_session
from app.main import create_app
from app.settings import Settings, get_settings
from tests.authfixtures import sign_in


class _FakeSession:
    """Minimal stand-in for AsyncSession: only ``execute`` is exercised."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def execute(self, *_: Any, **__: Any) -> None:
        if self._error is not None:
            raise self._error


def _override_session(app: FastAPI, session: _FakeSession) -> None:
    async def _dependency() -> AsyncIterator[_FakeSession]:
        yield session

    app.dependency_overrides[get_session] = _dependency


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine.test")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


async def test_health_reports_version_and_environment(app: FastAPI) -> None:
    async with await _client(app) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "environment": "local"}


async def test_readiness_is_ok_when_the_database_answers(app: FastAPI) -> None:
    _override_session(app, _FakeSession())

    async with await _client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_readiness_degrades_instead_of_raising(app: FastAPI) -> None:
    _override_session(app, _FakeSession(error=OSError("connection refused")))

    async with await _client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unavailable"
    assert body["detail"] == "OSError"


async def test_docs_are_available_outside_production(app: FastAPI) -> None:
    async with await _client(app) as client:
        assert (await client.get("/openapi.json")).status_code == 200


async def test_docs_are_disabled_in_production(settings: Settings) -> None:
    production = settings.model_copy(update={"environment": "prod"})
    app = create_app(production)

    async with await _client(app) as client:
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404


def test_create_app_falls_back_to_environment_settings() -> None:
    app = create_app()
    assert app.state.settings == get_settings()


def test_uvicorn_can_load_the_app_by_attribute() -> None:
    """`uvicorn app.main:app` must find a FastAPI instance, not the package.

    Regression: `import app.connectors` inside app/main.py bound the name `app`
    to the top-level package, so uvicorn loaded a module and every request died
    with "'module' object is not callable".
    """
    import app.main as main_module

    loaded = main_module.app
    assert isinstance(loaded, FastAPI)
    assert callable(loaded)


def test_an_unknown_module_attribute_still_raises() -> None:
    """The lazy `app` must not turn every typo into a silent app instance."""
    import app.main as main_module

    with pytest.raises(AttributeError, match="no attribute 'nonsense'"):
        _ = main_module.nonsense


async def test_chat_reports_a_missing_api_key_as_unavailable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment fault should say what is wrong, not return a bare 500."""
    import app.api.chat as chat_module
    from app.agent.loop import ModelNotConfigured

    async def unconfigured(**_: object) -> None:
        raise ModelNotConfigured("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(chat_module, "run_agent", unconfigured)

    application = create_app(settings)
    sign_in(application)
    async with await _client(application) as client:
        response = await client.post("/chat", json={"message": "hi"})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]

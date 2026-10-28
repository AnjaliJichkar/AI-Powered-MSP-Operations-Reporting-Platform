"""Shared test configuration.

Environment variables are set before ``app.settings`` is imported anywhere, so
no test depends on a developer's local ``.env``. Keys are generated per run:
no secret, real or fake, is committed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet

#: The database this run owns.
#:
#: **Every run gets its own**, named after the process. Two suites sharing one
#: database do not fail honestly: the second one's schema set-up drops the
#: first one's tables mid-test, and what comes back is dozens of errors about
#: relations that do not exist — which reads as a broken suite and is a broken
#: *arrangement*. It cost three separate false diagnoses before it was worth
#: fixing. `TEST_DATABASE_PER_RUN=0` opts out, for a CI runner that already
#: gives each job its own database.
_BASE_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://inputiv:inputiv@localhost:5432/inputiv_test"
)


def _per_run(url: str) -> str:
    if os.environ.get("TEST_DATABASE_PER_RUN", "1") == "0":
        return url
    base, _, name = url.rpartition("/")
    database, sep, query = name.partition("?")
    return f"{base}/{database}_r{os.getpid()}{sep}{query}"


#: The developer's own database, as it was before this file replaced it.
#: Captured so the guard can refuse to drop it however it is named.
_APP_DATABASE_URL = os.environ.get("DATABASE_URL")

TEST_DATABASE_URL = _per_run(_BASE_DATABASE_URL)
os.environ["TEST_DATABASE_URL"] = TEST_DATABASE_URL

import tests.database as _database  # noqa: E402
from tests.database import Disposable  # noqa: E402

_database.WHERE = Disposable(base_url=_BASE_DATABASE_URL, app_url=_APP_DATABASE_URL)

_TEST_KEY_V1 = Fernet.generate_key().decode()
_TEST_KEY_V2 = Fernet.generate_key().decode()

os.environ.update(
    {
        "ENVIRONMENT": "local",
        "DEBUG": "false",
        "LOG_LEVEL": "WARNING",
        "DATABASE_URL": TEST_DATABASE_URL,
        "CREDENTIAL_ENCRYPTION_KEYS": json.dumps({"v1": _TEST_KEY_V1, "v2": _TEST_KEY_V2}),
        "CREDENTIAL_ENCRYPTION_ACTIVE_KEY_ID": "v2",
        "ANTHROPIC_MODEL": "claude-sonnet-5",
        "MAX_TOOL_CALLS": "8",
        "TOOL_TIMEOUT_SECONDS": "30",
        "AGENT_TIMEOUT_SECONDS": "120",
    }
)

from app.audit.recorder import forget_client_kinds  # noqa: E402
from app.security.crypto import CredentialCipher, get_cipher  # noqa: E402
from app.settings import Settings, get_settings  # noqa: E402

# Tests read only the environment set above, never a developer's .env. Without
# this the suite's results depend on local config: pydantic-settings *merges*
# dict fields such as CREDENTIAL_ENCRYPTION_KEYS across sources, so a key in
# .env would silently join the ones a test sets.
Settings.model_config["env_file"] = None


@pytest.fixture(scope="session", autouse=True)
def _own_database() -> Iterator[None]:
    """Drop this run's database when the run ends, whatever happened in it."""
    yield
    from tests.database import drop_run_database

    drop_run_database(TEST_DATABASE_URL)


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    """Settings and cipher are cached per process; reset around every test."""
    get_settings.cache_clear()
    get_cipher.cache_clear()
    forget_client_kinds()
    yield
    get_settings.cache_clear()
    get_cipher.cache_clear()
    forget_client_kinds()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def cipher() -> CredentialCipher:
    return get_cipher()


@pytest.fixture
def test_keys() -> dict[str, str]:
    return {"v1": _TEST_KEY_V1, "v2": _TEST_KEY_V2}


@pytest.fixture
def last_seen_in_memory(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`app.devices.lastseen` keeps GravityZone's last-seen on the device links,
    which needs the database; a test that has none takes this and the store is a
    dict keyed by vendor id (values carry `vendor_last_seen_at`, `vendor_checked_at`)."""
    import datetime as dt
    from types import SimpleNamespace

    import app.devices.lastseen as lastseen_module

    store: dict[str, Any] = {}

    async def load(client_id: Any, ids: Any) -> dict[str, Any]:
        return {i: store[i] for i in ids if i in store}

    async def save(links: Any, fetched: Any, now: dt.datetime) -> None:
        for external_id, when in fetched.items():
            store[external_id] = SimpleNamespace(
                external_id=external_id, vendor_last_seen_at=when, vendor_checked_at=now
            )

    monkeypatch.setattr(lastseen_module, "_load_links", load)
    monkeypatch.setattr(lastseen_module, "_store_links", save)
    return store

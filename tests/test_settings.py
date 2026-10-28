"""Settings are read from the environment and validated there, not at use time."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.settings import Settings, get_settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "credential_encryption_keys": {},
        "credential_encryption_active_key_id": "v1",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _production(**overrides: object) -> Settings:
    """A configuration production accepts: https link, a database off the machine,
    staff second factor required. Each test below takes one of them away."""
    base: dict[str, object] = {
        "environment": "prod",
        "public_base_url": "https://desk.example.com",
        "database_url": "postgresql://u:p@dpg-abc-a/db",
        "totp_required_for_inputiv": True,
    }
    base.update(overrides)
    return _settings(**base)


def test_bare_postgres_url_is_pointed_at_asyncpg() -> None:
    settings = _settings(database_url="postgresql://u:p@localhost:5432/db")
    assert settings.database_url == "postgresql+asyncpg://u:p@localhost:5432/db"


def test_asyncpg_url_is_left_alone() -> None:
    url = "postgresql+asyncpg://u:p@host:5432/db"
    assert _settings(database_url=url).database_url == url


@pytest.mark.parametrize("url", ["mysql://u:p@h/db", "sqlite+aiosqlite:///./x.db", "redis://h"])
def test_non_postgres_url_is_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="postgresql"):
        _settings(database_url=url)


def test_log_level_is_normalised_and_validated() -> None:
    assert _settings(log_level="info").log_level == "INFO"
    with pytest.raises(ValidationError, match="log_level"):
        _settings(log_level="chatty")


def test_active_encryption_key_must_be_present_in_the_key_map() -> None:
    with pytest.raises(ValidationError, match="credential_encryption_active_key_id"):
        _settings(
            credential_encryption_keys={"v1": "k1"},
            credential_encryption_active_key_id="v9",
        )


def test_agent_caps_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard rule 6: caps are configuration, never constants in code."""
    monkeypatch.setenv("MAX_TOOL_CALLS", "3")
    monkeypatch.setenv("TOOL_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("AGENT_TIMEOUT_SECONDS", "42")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.max_tool_calls == 3
    assert settings.tool_timeout_seconds == 5.5
    assert settings.agent_timeout_seconds == 42


@pytest.mark.parametrize(("field", "value"), [("max_tool_calls", 0), ("tool_timeout_seconds", 0)])
def test_caps_must_be_positive(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_encryption_keys_parse_from_a_json_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", json.dumps({"a": "k1", "b": "k2"}))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_ACTIVE_KEY_ID", "b")
    get_settings.cache_clear()

    settings = get_settings()
    assert set(settings.credential_encryption_keys) == {"a", "b"}
    assert settings.credential_encryption_active_key_id == "b"


def test_secrets_do_not_appear_in_the_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    get_settings.cache_clear()

    assert "sk-ant-not-a-real-key" not in repr(get_settings())


def test_the_suite_does_not_read_a_developers_env_file() -> None:
    """A local .env must never change what the tests see.

    pydantic-settings merges dict fields across sources, so without this
    isolation a key in someone's .env would join the ones a test sets and the
    suite would pass or fail depending on the machine.
    """
    assert Settings.model_config["env_file"] is None


def test_is_production_flag() -> None:
    # A production configuration has to be a valid one: outside `local` the
    # base URL must be https, because an invite link carries a one-time token.
    assert _production().is_production is True
    assert _settings(environment="local").is_production is False


def test_a_link_to_a_deployment_has_to_be_reachable_and_encrypted() -> None:
    """A default of `http://localhost:8000` is right for a laptop and wrong
    everywhere else, so every real deployment has to say where it answers."""
    local = _settings(environment="local")
    assert local.url_for("/invite/abc") == "http://localhost:8000/invite/abc"

    # A trailing slash would double up on every link built from it.
    trimmed = _settings(public_base_url="https://desk.example.com/")
    assert trimmed.url_for("invite/abc") == "https://desk.example.com/invite/abc"

    with pytest.raises(ValidationError, match="http:// or https://"):
        _settings(public_base_url="desk.example.com")

    with pytest.raises(ValidationError, match="must be https outside"):
        _settings(environment="prod")


def test_renders_postgres_scheme_is_pointed_at_asyncpg() -> None:
    settings = _settings(database_url="postgres://u:p@dpg-abc-a/db")
    assert settings.database_url == "postgresql+asyncpg://u:p@dpg-abc-a/db"


@pytest.mark.parametrize(
    ("override", "problem"),
    [
        ({"debug": True}, "DEBUG must be false"),
        ({"database_echo": True}, "DATABASE_ECHO must be false"),
        ({"totp_required_for_inputiv": False}, "TOTP_REQUIRED_FOR_INPUTIV must be true"),
        ({"public_base_url": "https://localhost:8000"}, "PUBLIC_BASE_URL points at the local"),
        (
            {"database_url": "postgresql://u:p@127.0.0.1:5432/db"},
            "DATABASE_URL points at the local",
        ),
    ],
)
def test_laptop_options_are_refused_outside_local(
    override: dict[str, object], problem: str
) -> None:
    """Debug output, SQL echo, a skippable second factor and a local address are
    refused at start-up in every environment but a developer's machine."""
    with pytest.raises(ValidationError, match=problem):
        _production(**override)
    # The same options are fine on the laptop.
    _settings(environment="local", **override)


def test_production_accepts_a_configuration_with_none_of_them() -> None:
    settings = _production()
    assert settings.session_cookie_secure is True
    assert settings.totp_required_for_inputiv is True
    assert settings.public_base_url.startswith("https://")


def test_a_connector_may_have_its_own_timeout_and_the_others_take_the_shared_one() -> None:
    from app.connectors.base import ConnectorLimits

    settings = _settings(connector_timeout_seconds=30.0, bitdefender_timeout_seconds=120.0)
    assert settings.connector_timeout_for("bitdefender") == 120.0
    assert settings.connector_timeout_for("ninjaone") == 30.0
    assert settings.connector_timeout_for("connectsecure") == 30.0
    assert ConnectorLimits.from_settings(settings, connector="bitdefender").timeout_seconds == 120.0
    assert ConnectorLimits.from_settings(settings, connector="autotask").timeout_seconds == 30.0
    assert ConnectorLimits.from_settings(settings).timeout_seconds == 30.0
    # The default: GravityZone alone gets two minutes.
    assert _settings().connector_timeout_for("bitdefender") == 120.0
    assert _settings().connector_timeout_for("ninjaone") == 30.0
    with pytest.raises(ValidationError):
        _settings(bitdefender_timeout_seconds=0)

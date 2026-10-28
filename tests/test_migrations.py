"""The migration must produce exactly the schema the models describe.

The first test needs no database: Alembic renders the migration offline and the
result is compared with the DDL SQLAlchemy generates from ``Base.metadata``.
The second runs the real thing and is skipped unless ``TEST_DATABASE_URL`` is
set (see the postgres service in ``docker-compose.yml``).
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  registers the tables
from app.db.base import Base
from tests.database import require_database
from tests.ddl_utils import normalise, parse_indexes, parse_tables

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFLINE_URL = "postgresql+asyncpg://user:pass@localhost:5432/offline"
LIVE_URL = os.environ.get("TEST_DATABASE_URL")


def _alembic_config(url: str, output_buffer: io.StringIO | None = None) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=output_buffer)
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="module")
def rendered_sql() -> str:
    """DDL produced by ``alembic upgrade head --sql``, without a database."""
    buffer = io.StringIO()
    command.upgrade(_alembic_config(OFFLINE_URL, buffer), "head", sql=True)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def expected_sql() -> str:
    """DDL for the same schema, generated straight from the models."""
    dialect = postgresql.dialect()
    statements = []
    for table in Base.metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)) + "\n);")
        statements.extend(
            str(CreateIndex(index).compile(dialect=dialect)) + ";" for index in table.indexes
        )
    return "\n".join(statements)


def test_migration_creates_every_table_the_models_declare(
    rendered_sql: str, expected_sql: str
) -> None:
    assert set(parse_tables(rendered_sql)) == set(parse_tables(expected_sql))


@pytest.mark.parametrize(
    "table_name",
    [
        "clients",
        "sites",
        "tool_mappings",
        "tool_credentials",
        "audit_log",
        "conversations",
        "conversation_messages",
    ],
)
def test_migration_matches_the_models_column_for_column(
    table_name: str, rendered_sql: str, expected_sql: str
) -> None:
    """Catches drift between a model change and its migration."""
    actual = parse_tables(rendered_sql)[table_name]
    expected = parse_tables(expected_sql)[table_name]

    missing = expected - actual
    extra = actual - expected
    assert not missing, f"{table_name}: migration is missing {sorted(missing)}"
    assert not extra, f"{table_name}: migration adds {sorted(extra)}"


def test_migration_creates_every_index(rendered_sql: str, expected_sql: str) -> None:
    assert parse_indexes(rendered_sql) == parse_indexes(expected_sql)


def test_downgrade_renders() -> None:
    """A downgrade that does not render is a downgrade nobody can run."""
    buffer = io.StringIO()
    command.downgrade(_alembic_config(OFFLINE_URL, buffer), "0001:base", sql=True)
    sql = normalise(buffer.getvalue()).upper()

    for table in ("AUDIT_LOG", "TOOL_CREDENTIALS", "TOOL_MAPPINGS", "CLIENTS"):
        assert f"DROP TABLE {table}" in sql


@pytest.mark.postgres
@pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set")
def test_upgrade_and_downgrade_against_a_real_database() -> None:
    """Applies the migration for real, then asserts the models match the result."""
    config = _alembic_config(require_database(LIVE_URL))
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    # `alembic check` fails if autogenerate would produce any operation, which
    # is the definitive no-drift assertion.
    command.check(config)
    command.downgrade(config, "base")

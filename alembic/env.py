"""Alembic environment.

Runs the async engine from :mod:`app.db.session` so migrations use exactly the
same URL and driver as the application. Offline mode (``--sql``) renders DDL
without a database, which is how the migrations are checked in CI.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  registers every table on Base.metadata
from app.db.base import Base
from app.settings import get_settings

config = context.config

if config.config_file_name is not None:
    # fileConfig's default is disable_existing_loggers=True, which would mute
    # every `app.*` logger already created in this process — including the
    # scoping-guard errors — whenever a migration runs in-process (tests,
    # scripts). Configure alembic's own logging without silencing ours.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """``-x db_url=...`` wins, then an explicit config url, then settings."""
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return override
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Render DDL to stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect with the async engine and run migrations."""
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

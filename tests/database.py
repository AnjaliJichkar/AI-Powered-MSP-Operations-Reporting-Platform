"""One place that says what the database tests need, and how to give it to them.

A fixture that reaches a real PostgreSQL has to do something when nobody has
pointed it at one. `assert LIVE_URL is not None` was doing it, and the message
that reaches the developer is `assert None is not None` — 172 of them, in the
set-up phase, which reads as a broken suite rather than a missing variable.

So the skip lives here, with the variable named and a value that works.

**A `pytestmark` in a conftest does not do this.** pytest reads `pytestmark`
from test modules and classes only, so a `skipif` written in a conftest is
collected by nothing and silently guards nothing (`pytest tests/api -m postgres`
deselected all 182 of them). Marking every module is the alternative and is one
forgotten line away from the same hole; a fixture cannot be forgotten, because
the test that needs the database is the test that asks for it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

#: Databases this run has already made sure of, so the check is one query per
#: process rather than one per fixture.
_READY: set[str] = set()

#: The only name shape this suite will create or drop: the base name, `_r`,
#: and the process id. Anchored on purpose — an unanchored pattern matches
#: `inputiv_test_r1_backup_of_production` and `drop database` does not ask
#: twice.
PER_RUN_NAME = re.compile(r"^inputiv_test_r\d+$")

#: The only base this suite will build a per-run name from.
BASE_NAME = "inputiv_test"

#: One empty file per live run, named for its database. A run writes it when it
#: creates the database and removes it when it drops it.
#:
#: The sweep has to tell a database a crashed run left behind from one a run
#: happening *now* is using, and "no rows in `pg_stat_activity`" does not: a
#: suite between two test modules holds no connection for a moment, and a
#: second suite sweeping at that moment dropped the first one's database out
#: from under it — the collision the per-run database exists to prevent,
#: arriving from the other end. A file on disk is held for the whole run and
#: does not blink.
LIVE_RUNS = Path(tempfile.gettempdir()) / "inputiv-test-runs"

#: How long a marker is believed. A suite takes minutes; past this the process
#: that wrote it is gone and did not clean up, so its database is rubbish.
LIVE_FOR = dt.timedelta(hours=2)


class UnsafeDatabase(RuntimeError):
    """A name this suite refuses to create or drop. Nothing was touched."""


def _marker(database: str) -> Path:
    return LIVE_RUNS / f"{database}.live"


def mark_live(database: str) -> None:
    """Say this run is using that database, for as long as it is."""
    try:
        LIVE_RUNS.mkdir(parents=True, exist_ok=True)
        _marker(database).write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - a marker we cannot write is not fatal
        logger.warning("could not mark %s as live: %s", database, exc)


def mark_finished(database: str) -> None:
    """The run is over; the database may be swept if dropping it failed."""
    try:
        _marker(database).unlink(missing_ok=True)
    except OSError:  # pragma: no cover - the next sweep reads it as old instead
        pass


def is_live(database: str, *, now: dt.datetime | None = None) -> bool:
    """Whether a run is using this database at the moment.

    A marker older than `LIVE_FOR` is read as abandoned rather than live: the
    alternative is a crashed run's database left on the machine for ever.
    """
    try:
        written = dt.datetime.fromtimestamp(_marker(database).stat().st_mtime, dt.UTC)
    except OSError:
        return False
    return (now or dt.datetime.now(dt.UTC)) - written < LIVE_FOR


@dataclass(frozen=True)
class Disposable:
    """Where the suite may act, decided once and checked at every use."""

    base_url: str
    """What the developer pointed `TEST_DATABASE_URL` at."""
    app_url: str | None = None
    """The `DATABASE_URL` that was set before the suite replaced it: the
    developer's own database, which must never be the one we drop."""


#: Set by `tests/conftest.py` before anything connects.
WHERE = Disposable(base_url="")


def database_name(url: str) -> str:
    """The database a URL names, without its query string."""
    return url.rpartition("/")[2].partition("?")[0]


def guard_disposable(url: str) -> str:
    """The name of a database this suite may create and drop, or raise.

    Three checks, all of which must hold. Any one of them failing stops the
    run and drops nothing: a database this suite deletes is one somebody
    could be using, and the cost of being wrong once is not recoverable.
    """
    name = database_name(url)
    base = database_name(WHERE.base_url)
    if base != BASE_NAME:
        raise UnsafeDatabase(
            f"TEST_DATABASE_URL names the database {base!r}, and the per-run database is "
            f"only built from {BASE_NAME!r}. Point TEST_DATABASE_URL at {BASE_NAME}, or set "
            "TEST_DATABASE_PER_RUN=0 to use the database you named and run one suite at a "
            "time."
        )
    if not PER_RUN_NAME.fullmatch(name):
        raise UnsafeDatabase(
            f"{name!r} is not a per-run test database ({PER_RUN_NAME.pattern}). This suite "
            "creates and drops nothing else."
        )
    if WHERE.app_url and database_name(WHERE.app_url) == name:
        raise UnsafeDatabase(
            f"{name!r} is also what DATABASE_URL names, so it is a database somebody is "
            "using. Nothing was created or dropped."
        )
    return name


#: The URL to hand the variable, and what docker-compose.yml serves.
EXAMPLE_URL = "postgresql+asyncpg://inputiv:inputiv@localhost:5432/inputiv_test"


def require_database(url: str | None) -> str:
    """The database URL these tests run against, or skip saying how to set it.

    Returns the URL so a caller can use it directly, which is also the type
    narrowing the old assertions were there for.
    """
    if url is None:
        pytest.skip(
            "TEST_DATABASE_URL is not set, so this test cannot reach a database. "
            "Start PostgreSQL with `docker compose up -d postgres`, then run with "
            f"TEST_DATABASE_URL={EXAMPLE_URL}"
        )
    if url not in _READY:
        asyncio.run(_create_if_missing(url))
        _READY.add(url)
    return url


def _parts(url: str) -> tuple[str, str]:
    """A URL's maintenance address and its database name.

    `postgres` is the database every server has and nobody's tests use, which
    is where a `CREATE DATABASE` has to be issued from.
    """
    base, _, name = url.rpartition("/")
    database = name.partition("?")[0]
    dsn = f"{base}/postgres".replace("postgresql+asyncpg://", "postgresql://")
    return dsn, database


async def _create_if_missing(url: str) -> None:
    """Make this run's database, and sweep away any a killed run left behind.

    A run that is interrupted cannot drop its own database, so the sweep is
    how they stop accumulating: any `*_r<pid>` database with nobody connected
    to it is from a run that has ended.
    """
    import asyncpg

    database = guard_disposable(url)
    dsn, _ = _parts(url)
    connection = await asyncpg.connect(dsn)
    try:
        exists = await connection.fetchval("select 1 from pg_database where datname = $1", database)
        if not exists:
            await connection.execute(f'create database "{database}"')
        mark_live(database)
        base = database.rpartition("_r")[0]
        if base:
            stale = await connection.fetch(
                """
                select d.datname from pg_database d
                where d.datname like $1 and d.datname <> $2
                  and not exists (
                      select 1 from pg_stat_activity a where a.datname = d.datname
                  )
                """,
                f"{base}_r%",
                database,
            )
            for row in stale:
                # The sweep goes through the same gate as everything else: a
                # pattern in SQL is not a guarantee, and this one is.
                if not PER_RUN_NAME.fullmatch(row["datname"]):
                    continue
                # And a run happening now is left alone whatever its
                # connections are doing between two test modules.
                if is_live(row["datname"]):
                    continue
                await connection.execute(f'drop database if exists "{row["datname"]}"')
                mark_finished(row["datname"])
                logger.info("dropped the database a finished run left behind: %s", row["datname"])
    finally:
        await connection.close()


def drop_run_database(url: str | None) -> None:
    """This run's database, gone. Never anybody else's."""
    if not url:
        return
    try:
        guard_disposable(url)
    except UnsafeDatabase as exc:
        logger.warning("not dropping anything: %s", exc)
        return
    # The marker goes first: a drop that fails leaves a database the next
    # sweep should be free to take, and a marker nobody will ever remove is
    # how one survives for ever.
    mark_finished(database_name(url))
    try:
        asyncio.run(_drop(url))
    except Exception as exc:
        logger.warning("could not drop this run's database: %s", exc)
    _READY.discard(url)


async def _drop(url: str) -> None:
    import asyncpg

    database = guard_disposable(url)
    dsn, _ = _parts(url)
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where datname = $1",
            database,
        )
        await connection.execute(f'drop database if exists "{database}"')
    finally:
        await connection.close()

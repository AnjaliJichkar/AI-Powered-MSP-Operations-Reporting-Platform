"""What this suite is allowed to create and drop.

The suite makes a database per run and deletes it afterwards, which means
code in this repository issues `drop database`. These are the checks that
stand between that statement and somebody's real data, so they are tested
one refusal at a time — and a guard nobody has watched fail is a guard
nobody knows works.

No database is reached here: every case is the decision, not the act.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tests import database
from tests.database import Disposable, UnsafeDatabase, guard_disposable

PER_RUN = "postgresql+asyncpg://u:p@localhost:5432/inputiv_test_r4242"
BASE = "postgresql+asyncpg://u:p@localhost:5432/inputiv_test"


@pytest.fixture
def allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database, "WHERE", Disposable(base_url=BASE, app_url=None))


def test_a_per_run_database_is_allowed(allowed: None) -> None:
    assert guard_disposable(PER_RUN) == "inputiv_test_r4242"


def test_the_base_must_be_the_one_name_we_build_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pointed at anything else, the suite stops rather than inventing a
    per-run name beside a database it knows nothing about."""
    monkeypatch.setattr(
        database,
        "WHERE",
        Disposable(base_url="postgresql+asyncpg://u:p@localhost:5432/production"),
    )
    with pytest.raises(UnsafeDatabase) as raised:
        guard_disposable("postgresql+asyncpg://u:p@localhost:5432/production_r1")
    assert "production" in str(raised.value)
    assert "TEST_DATABASE_PER_RUN=0" in str(raised.value)


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("inputiv_test", "the base itself is not a per-run database"),
        ("inputiv_test_r", "no process id"),
        ("inputiv_test_rabc", "not a number"),
        ("inputiv_test_r1_backup_of_production", "the trap an unanchored pattern falls into"),
        ("prefix_inputiv_test_r1", "anchored at the front too"),
        ("postgres", "the maintenance database"),
        ("inputiv", "the developer's own"),
    ],
)
def test_only_an_exact_per_run_name_may_be_touched(name: str, why: str, allowed: None) -> None:
    with pytest.raises(UnsafeDatabase):
        guard_disposable(f"postgresql+asyncpg://u:p@localhost:5432/{name}")


def test_a_database_someone_is_using_is_refused_even_if_it_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt and braces: a name of the right shape that is also what
    DATABASE_URL points at is somebody's database, whatever it is called."""
    monkeypatch.setattr(
        database,
        "WHERE",
        Disposable(base_url=BASE, app_url=PER_RUN),
    )
    with pytest.raises(UnsafeDatabase, match="somebody is using"):
        guard_disposable(PER_RUN)


def test_a_refused_name_drops_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal has to stop the act, not just describe it."""
    monkeypatch.setattr(database, "WHERE", Disposable(base_url=BASE))
    called: list[str] = []
    monkeypatch.setattr(database.asyncio, "run", lambda *a, **k: called.append("ran"))
    database.drop_run_database("postgresql+asyncpg://u:p@localhost:5432/inputiv")
    assert called == []


def test_the_query_string_is_not_part_of_the_name(allowed: None) -> None:
    assert guard_disposable(f"{PER_RUN}?sslmode=require") == "inputiv_test_r4242"


# --- a run that is happening now ------------------------------------------------


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The live-run markers, somewhere this test owns."""
    monkeypatch.setattr(database, "LIVE_RUNS", tmp_path / "runs")
    return tmp_path / "runs"


def test_a_run_in_progress_is_not_swept(runs: Path) -> None:
    """`pg_stat_activity` was the whole test, and a suite between two modules
    holds no connection for a moment. A second suite sweeping at that moment
    dropped the first one's database out from under it, which is the collision
    the per-run database exists to prevent, arriving from the other end."""
    database.mark_live("inputiv_test_r4242")
    assert database.is_live("inputiv_test_r4242") is True


def test_a_database_nobody_claimed_is_swept(runs: Path) -> None:
    assert database.is_live("inputiv_test_r4242") is False


def test_a_run_that_crashed_does_not_hold_its_database_for_ever(runs: Path) -> None:
    """A marker outlives the process that wrote it, so it is believed for a
    while and not indefinitely: the alternative is a dead run's database left
    on the machine until somebody notices."""
    database.mark_live("inputiv_test_r4242")
    later = dt.datetime.now(dt.UTC) + database.LIVE_FOR + dt.timedelta(minutes=1)
    assert database.is_live("inputiv_test_r4242", now=later) is False


def test_a_finished_run_releases_its_database(runs: Path) -> None:
    database.mark_live("inputiv_test_r4242")
    database.mark_finished("inputiv_test_r4242")
    assert database.is_live("inputiv_test_r4242") is False
    # And saying it twice is not an error: the drop path says it before trying.
    database.mark_finished("inputiv_test_r4242")

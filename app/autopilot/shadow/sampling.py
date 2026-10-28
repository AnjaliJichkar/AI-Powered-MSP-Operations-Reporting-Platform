"""The fixed set of tickets iteration runs against.

A full replay of this tenant's ninety days is 2,722 tickets and about five
million tokens, and almost none of that buys an answer to the question you
actually ask while iterating — *did that change help?* A few hundred tickets
drawn properly answer it as well, and the honest number still comes from a full
replay on the holdout at the end.

Three rules make the sample worth having:

* **Drawn once and stored.** Not recomputed from a seed per run: the ledger
  gains rows, and a seed over a changed population selects a different set, so
  two runs would stop being comparable without anybody having touched anything.
  `sample_for` creates it on the first ask and reads it for ever after.
* **Both periods, in the proportion they have.** The window splits where
  scoring splits it — the last `holdout_days` measured back from the newest
  ticket — and the sample is drawn from each side, so a threshold chosen on the
  sample's tuning half can be checked on its test half the way the real thing
  is.
* **Stratified by issue type.** The rare labels are where the agent is wrong
  and a uniform draw of 600 from 2,722 would take one or two of each; the
  classes are allocated proportionally, with at least one of every label that
  exists in that period, so a change that breaks a small class shows up.

No ticket text is read or stored here. A sample is a list of ids.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outcome import TicketOutcome
from app.models.shadow import ShadowSample, ShadowSampleTicket

#: The one sample there is today. A name rather than an enum: a second sample
#: for a second purpose is a row.
ITERATION = "iteration"

#: What `shadow sample` draws when nobody says otherwise. Six hundred of this
#: tenant's 2,722 is a fifth of the ledger for a twentieth of the token bill,
#: and leaves every issue type with enough rows to move a percentage.
DEFAULT_SIZE = 600

#: Fixed, and fixed in code rather than passed in, because a seed somebody can
#: change per run is a seed somebody changes until the number looks better.
SEED = 20261002


@dataclass(frozen=True, slots=True)
class Drawn:
    """What a draw produced, in counts."""

    sample_id: uuid.UUID
    created: bool
    """False when the stored sample was read rather than drawn again."""
    tuning: int = 0
    test: int = 0
    drawn_from: int = 0
    by_issue_type: dict[str, int] | None = None

    @property
    def size(self) -> int:
        return self.tuning + self.test


@dataclass(frozen=True, slots=True)
class Candidate:
    """One ledger row as a draw sees it: an id, when it arrived, its class."""

    ticket_id: str
    created_at: dt.datetime
    issue_type: str


async def _candidates(
    session: AsyncSession, client_id: uuid.UUID, *, days: int, now: dt.datetime
) -> list[Candidate]:
    """The window's typed, dated tickets — the population a replay can score.

    A row with no issue type cannot be scored against anything, and a row with
    no creation time cannot be placed in a period; neither belongs in a sample
    drawn to measure accuracy by class.
    """
    rows = (
        (
            await session.execute(
                select(TicketOutcome).where(
                    TicketOutcome.client_id == client_id,
                    TicketOutcome.completed_at >= now - dt.timedelta(days=days),
                    TicketOutcome.created_at_vendor.is_not(None),
                    TicketOutcome.issue_type_label.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidate(
            ticket_id=row.ticket_id,
            created_at=row.created_at_vendor,
            issue_type=row.issue_type_label,
        )
        for row in rows
        if row.created_at_vendor is not None and row.issue_type_label
    ]


def _allocate(groups: dict[str, list[Candidate]], want: int) -> dict[str, int]:
    """How many of each issue type to take, proportionally and never none.

    Largest-remainder, so the parts add to the whole rather than to whatever
    rounding leaves. Every label present gets at least one — the rare classes
    are the interesting ones — and no label gets more than it has.
    """
    total = sum(len(rows) for rows in groups.values())
    if not total or want <= 0:
        return {}
    exact = {name: want * len(rows) / total for name, rows in groups.items()}
    taken = {name: min(len(groups[name]), max(1, int(share))) for name, share in exact.items()}
    # Then hand out what rounding left, to the labels with most owing, until the
    # sample is full or every label is exhausted.
    order = sorted(
        exact, key=lambda name: (exact[name] - int(exact[name]), exact[name]), reverse=True
    )
    while sum(taken.values()) < want:
        moved = False
        for name in order:
            if sum(taken.values()) >= want:
                break
            if taken[name] < len(groups[name]):
                taken[name] += 1
                moved = True
        if not moved:
            break
    # And take back the over-allocation a minimum of one can cause, from the
    # largest classes first, where one ticket makes the least difference.
    for name in sorted(taken, key=lambda name: len(groups[name]), reverse=True):
        while sum(taken.values()) > want and taken[name] > 1:
            taken[name] -= 1
    return taken


def draw(
    rows: list[Candidate], *, size: int, holdout_days: int, seed: int
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """The chosen ticket ids with their period, and the per-class counts.

    Deterministic given the same rows: the ids are sorted before anything is
    shuffled, so a database that returns them in a different order draws the
    same sample.
    """
    if not rows:
        return [], {}
    newest = max(row.created_at for row in rows)
    boundary = newest - dt.timedelta(days=holdout_days)
    periods = {
        "tuning": [row for row in rows if row.created_at <= boundary],
        "test": [row for row in rows if row.created_at > boundary],
    }
    chosen: list[tuple[str, str]] = []
    by_class: dict[str, int] = {}
    for period, population in periods.items():
        if not population:
            continue
        # Each period keeps its share of the sample, so the split of the sample
        # is the split of the ledger rather than an even one nobody asked for.
        want = round(size * len(population) / len(rows))
        groups: dict[str, list[Candidate]] = {}
        for row in population:
            groups.setdefault(row.issue_type, []).append(row)
        for name in groups:
            groups[name].sort(key=lambda row: row.ticket_id)
        rng = random.Random(f"{seed}:{period}")  # noqa: S311  seeded for reproducibility
        for name, count in sorted(_allocate(groups, want).items()):
            for row in rng.sample(groups[name], count):
                chosen.append((row.ticket_id, period))
                by_class[name] = by_class.get(name, 0) + 1
    chosen.sort()
    return chosen, by_class


async def sample_for(
    session: AsyncSession,
    client_id: uuid.UUID,
    *,
    name: str = ITERATION,
    size: int = DEFAULT_SIZE,
    days: int = 90,
    holdout_days: int = 30,
    seed: int = SEED,
    created_by: str = "autopilot-cli",
    now: dt.datetime | None = None,
    redraw: bool = False,
) -> Drawn:
    """The stored sample, drawn on the first ask.

    `redraw` replaces it, which is a deliberate act: every run that has used the
    old one was measuring something slightly different, so the runs either side
    of a redraw are not comparable and the sample's id on each is how anybody
    notices.
    """
    now = now or dt.datetime.now(dt.UTC)
    stored = (
        await session.execute(
            select(ShadowSample).where(
                ShadowSample.client_id == client_id, ShadowSample.name == name
            )
        )
    ).scalar_one_or_none()
    if stored is not None and not redraw:
        counts = (
            await session.execute(
                select(ShadowSampleTicket.period).where(ShadowSampleTicket.sample_id == stored.id)
            )
        ).scalars()
        periods = list(counts)
        return Drawn(
            sample_id=stored.id,
            created=False,
            tuning=sum(1 for one in periods if one == "tuning"),
            test=sum(1 for one in periods if one == "test"),
            drawn_from=stored.drawn_from,
        )
    if stored is not None:
        await session.delete(stored)
        await session.flush()

    rows = await _candidates(session, client_id, days=days, now=now)
    chosen, by_class = draw(rows, size=size, holdout_days=holdout_days, seed=seed)
    row = ShadowSample(
        id=uuid.uuid4(),
        client_id=client_id,
        name=name,
        seed=seed,
        size=size,
        days=days,
        holdout_days=holdout_days,
        drawn_from=len(rows),
        created_at=now,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    for ticket_id, period in chosen:
        session.add(ShadowSampleTicket(sample_id=row.id, ticket_id=ticket_id, period=period))
    await session.commit()
    return Drawn(
        sample_id=row.id,
        created=True,
        tuning=sum(1 for _, period in chosen if period == "tuning"),
        test=sum(1 for _, period in chosen if period == "test"),
        drawn_from=len(rows),
        by_issue_type=by_class,
    )


async def ticket_ids(session: AsyncSession, sample_id: uuid.UUID) -> set[str]:
    """The sample's tickets, for a run to restrict itself to."""
    rows = (
        await session.execute(
            select(ShadowSampleTicket.ticket_id).where(ShadowSampleTicket.sample_id == sample_id)
        )
    ).scalars()
    return set(rows)

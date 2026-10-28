"""The draw: both periods, every class, the same answer twice.

Nothing here touches a database. `draw` is a pure function over the rows a
ledger read returns, which is what makes "the same tickets every time" a thing
that can be asserted rather than hoped for.
"""

from __future__ import annotations

import datetime as dt

from app.autopilot.shadow.sampling import Candidate, draw

NEWEST = dt.datetime(2026, 9, 30, 12, tzinfo=dt.UTC)

#: A ledger shaped like the pilot's: a few common classes and a long tail, more
#: of it in the older two thirds of the window than in the last thirty days.
SHAPE = {"Email": 400, "User Access and Management": 300, "Printer": 120, "Telephony": 9}


def ledger() -> list[Candidate]:
    rows: list[Candidate] = []
    number = 0
    for issue_type, count in SHAPE.items():
        for index in range(count):
            number += 1
            # Two thirds into the tuning stretch, a third into the test one.
            days_ago = 80 - (index % 60) if index % 3 else 25 - (index % 20)
            rows.append(
                Candidate(
                    ticket_id=f"{10_000 + number}",
                    created_at=NEWEST - dt.timedelta(days=days_ago),
                    issue_type=issue_type,
                )
            )
    return rows


def test_the_draw_takes_both_periods_in_the_proportion_they_have() -> None:
    """A sample with no test half cannot check a threshold chosen on the
    tuning half, which is the one thing the split is for."""
    rows = ledger()
    chosen, _ = draw(rows, size=200, holdout_days=30, seed=1)
    periods = [period for _, period in chosen]
    assert len(chosen) == 200
    # Measured back from the newest ticket, as scoring measures it — not from
    # today, which is what makes "the last thirty days" mean of the data.
    boundary = max(row.created_at for row in rows) - dt.timedelta(days=30)
    actual_test = sum(1 for row in rows if row.created_at > boundary) / len(rows)
    share = periods.count("test") / len(periods)
    assert abs(share - actual_test) < 0.05, "the sample's split is the ledger's split"


def test_every_issue_type_survives_the_draw() -> None:
    """The rare classes are where the agent is wrong. A uniform draw of 200
    from 829 would take two of the nine Telephony tickets and might take none;
    the stratification is what keeps a class that breaks visible."""
    _, by_class = draw(ledger(), size=200, holdout_days=30, seed=1)
    assert set(by_class) == set(SHAPE)
    assert by_class["Email"] > by_class["Printer"] > by_class["Telephony"]
    assert by_class["Telephony"] >= 2, "one per period, at least"


def test_the_same_ledger_draws_the_same_sample_whatever_order_it_arrives_in() -> None:
    rows = ledger()
    first, _ = draw(rows, size=120, holdout_days=30, seed=7)
    second, _ = draw(list(reversed(rows)), size=120, holdout_days=30, seed=7)
    assert first == second
    assert len(set(ticket for ticket, _ in first)) == len(first), "no ticket twice"


def test_a_different_seed_draws_a_different_sample() -> None:
    """Proof the determinism above is the seed's doing rather than the draw
    having no randomness in it at all."""
    rows = ledger()
    first, _ = draw(rows, size=120, holdout_days=30, seed=7)
    other, _ = draw(rows, size=120, holdout_days=30, seed=8)
    assert first != other


def test_asking_for_more_than_there_is_takes_what_there_is() -> None:
    rows = ledger()
    chosen, _ = draw(rows, size=5_000, holdout_days=30, seed=1)
    assert len(chosen) == len(rows)


def test_an_empty_ledger_draws_nothing_rather_than_raising() -> None:
    assert draw([], size=600, holdout_days=30, seed=1) == ([], {})

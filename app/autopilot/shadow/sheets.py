"""The two things a person has to look at, written to `var/`.

Origin and grouping have no ground truth in any table we hold, so the only
honest way to score them is for somebody to read some tickets. Two exports:

* a **labelling sheet** of 200 tickets, stratified by issue type and site so it
  is not 200 tickets from one customer, with an empty column to write `alert`
  or `human` in; and
* **30 groups** the run made, for a spot-check of whether they are one thing.

These files carry ticket titles, which is why they go to `var/` — git-ignored,
never committed, and outside everything the counts-and-ids convention governs.
They are working papers for a person, not stored data: nothing reads them back
except `score_origin`, which reads only the ids and the column.

The sheets are CSV because that is what opens in a spreadsheet without being
asked twice.
"""

from __future__ import annotations

import csv
import pathlib
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outcome import TicketOutcome
from app.models.shadow import ShadowPrediction
from app.tickets.scrub import scrub

#: Written in the sheet's own column, for a person to replace.
UNLABELLED = ""
#: What a labeller may write. Anything else is reported rather than guessed at.
LABELS = ("alert", "human", "unsure")


@dataclass(frozen=True, slots=True)
class SheetWritten:
    path: pathlib.Path
    rows: int
    sites: int
    issue_types: int


def _stratified(rows: list[TicketOutcome], *, wanted: int, seed: int) -> list[TicketOutcome]:
    """A sample spread over issue type and site rather than over the list.

    Round-robin across (issue type, site) buckets: every bucket gives up its
    first ticket before any bucket gives a second. A proportional sample would
    be four fifths one customer's, which is the shape of this franchisee's
    volume and the wrong shape for a labelling exercise.
    """
    buckets: dict[tuple[str, Any], list[TicketOutcome]] = defaultdict(list)
    for row in rows:
        buckets[(row.issue_type_label or "(not set)", row.site_id)].append(row)
    # Seeded on purpose: the same sample every time, so two people
    # labelling the same export are labelling the same tickets. Nothing
    # here is a secret.
    shuffler = random.Random(seed)  # noqa: S311
    for bucket in buckets.values():
        shuffler.shuffle(bucket)
    # Shuffled, not sorted. Sorted, the first pass walks the buckets
    # alphabetically and a 200-row sheet of 433 buckets never reaches the issue
    # types late in the alphabet — measured: 8 of 15 types. Shuffled with the
    # seed, the sample spreads over every type and is still the same sample
    # every time.
    order = sorted(buckets, key=lambda key: (str(key[0]), str(key[1])))
    shuffler.shuffle(order)
    picked: list[TicketOutcome] = []
    depth = 0
    while len(picked) < wanted:
        added = False
        for key in order:
            bucket = buckets[key]
            if depth < len(bucket):
                picked.append(bucket[depth])
                added = True
                if len(picked) == wanted:
                    break
        if not added:
            break  # every bucket exhausted; the sample is as big as the data
        depth += 1
    return picked


async def write_origin_sheet(
    session: AsyncSession,
    client_id: uuid.UUID,
    *,
    path: pathlib.Path,
    wanted: int = 200,
    seed: int = 20261001,
) -> SheetWritten:
    """The labelling sheet. One row per ticket, one empty column to fill in."""
    rows = (
        (await session.execute(select(TicketOutcome).where(TicketOutcome.client_id == client_id)))
        .scalars()
        .all()
    )
    sample = _stratified(list(rows), wanted=wanted, seed=seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticket_id", "issue_type", "site_id", "title", "origin"])
        for row in sample:
            writer.writerow(
                [
                    row.ticket_id,
                    row.issue_type_label or "(not set)",
                    str(row.site_id) if row.site_id else "",
                    scrub(row.title or "").text,
                    UNLABELLED,
                ]
            )
    return SheetWritten(
        path=path,
        rows=len(sample),
        sites=len({row.site_id for row in sample}),
        issue_types=len({row.issue_type_label or "(not set)" for row in sample}),
    )


async def write_group_sheet(
    session: AsyncSession,
    client_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    path: pathlib.Path,
    wanted: int = 30,
    seed: int = 20261001,
) -> SheetWritten:
    """Thirty of the run's groups, each as its leader and the tickets that
    joined it, for a person to say whether they are one thing."""
    predictions = (
        (
            await session.execute(
                select(ShadowPrediction)
                .where(ShadowPrediction.run_id == run_id)
                .order_by(ShadowPrediction.created_at_vendor)
            )
        )
        .scalars()
        .all()
    )
    titles = dict(
        (
            await session.execute(
                select(TicketOutcome.ticket_id, TicketOutcome.title).where(
                    TicketOutcome.client_id == client_id
                )
            )
        ).all()
    )
    members: dict[str, list[ShadowPrediction]] = defaultdict(list)
    for prediction in predictions:
        if prediction.group_with:
            members[prediction.group_with].append(prediction)
    leaders = sorted(members)
    random.Random(seed).shuffle(leaders)  # noqa: S311  seeded for reproducibility
    chosen = leaders[:wanted]

    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["group", "role", "ticket_id", "score", "title", "one_thing (yes/no)"])
        for number, leader in enumerate(chosen, start=1):
            writer.writerow(
                [number, "leader", leader, "", scrub(titles.get(leader) or "").text, UNLABELLED]
            )
            written += 1
            for member in members[leader]:
                writer.writerow(
                    [
                        number,
                        "joined",
                        member.ticket_id,
                        float(member.group_score) if member.group_score is not None else "",
                        scrub(titles.get(member.ticket_id) or "").text,
                        "",
                    ]
                )
                written += 1
    return SheetWritten(path=path, rows=written, sites=0, issue_types=len(chosen))


@dataclass
class OriginScore:
    """What the filled-in sheet says, against what the agent said."""

    labelled: int = 0
    unlabelled: int = 0
    unreadable: int = 0
    """Rows whose label was not one of `LABELS`. Reported, never interpreted."""
    missing_prediction: int = 0
    agree: int = 0
    #: Precision and recall for `alert`, which is the gate's metric.
    predicted_alert: int = 0
    predicted_alert_correct: int = 0
    actual_alert: int = 0
    actual_alert_found: int = 0
    confusion: dict[str, Any] = None  # type: ignore[assignment]

    @property
    def alert_precision(self) -> float | None:
        return self.predicted_alert_correct / self.predicted_alert if self.predicted_alert else None

    @property
    def alert_recall(self) -> float | None:
        return self.actual_alert_found / self.actual_alert if self.actual_alert else None


@dataclass(frozen=True, slots=True)
class SheetRead:
    """A filled-in sheet, as rows of (ticket id, what the person wrote).

    Reading is file IO and therefore synchronous: the caller hands it to a
    thread, so nothing async blocks on a disk.
    """

    labels: list[tuple[str, str]]
    blank: int
    unreadable: int


def read_labels(sheet: pathlib.Path) -> SheetRead:
    """The sheet's ticket ids and labels. Blank and unreadable rows counted.

    A label that is not one of `LABELS` is counted and dropped: the sheet is a
    person's working file, and a typo in it is not something to interpret.
    """
    labels: list[tuple[str, str]] = []
    blank = unreadable = 0
    with sheet.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            written = (row.get("origin") or "").strip().lower()
            ticket = (row.get("ticket_id") or "").strip()
            if not written:
                blank += 1
            elif written not in LABELS:
                unreadable += 1
            else:
                labels.append((ticket, written))
    return SheetRead(labels=labels, blank=blank, unreadable=unreadable)


async def score_origin(
    session: AsyncSession, run_id: uuid.UUID, *, sheet: SheetRead
) -> OriginScore:
    """Score `origin` against a labelled sheet."""
    predictions = {
        row.ticket_id: row
        for row in (
            await session.execute(select(ShadowPrediction).where(ShadowPrediction.run_id == run_id))
        )
        .scalars()
        .all()
    }
    score = OriginScore(unlabelled=sheet.blank, unreadable=sheet.unreadable)
    pairs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ticket, label in sheet.labels:
        prediction = predictions.get(ticket)
        if prediction is None or prediction.origin is None:
            score.missing_prediction += 1
            continue
        score.labelled += 1
        pairs[prediction.origin][label] += 1
        if prediction.origin == label:
            score.agree += 1
        if prediction.origin == "alert":
            score.predicted_alert += 1
            if label == "alert":
                score.predicted_alert_correct += 1
        if label == "alert":
            score.actual_alert += 1
            if prediction.origin == "alert":
                score.actual_alert_found += 1
    score.confusion = {predicted: dict(actuals) for predicted, actuals in sorted(pairs.items())}
    return score

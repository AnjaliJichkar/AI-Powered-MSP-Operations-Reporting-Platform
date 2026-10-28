"""Putting tickets in front of a person, and writing down what they say.

Origin and grouping are the two things Level 0 cannot score from any table: the
vendor's own `source` field would answer the first and the ledger does not keep
it. So somebody reads some tickets. This module is the whole of that — the
sample, what a labeller is shown, what is stored, and how a score reads it
back.

**The labeller judges blind.** `item_view` returns the scrubbed subject, the
customer, when the ticket arrived and the queue if we know it, and **nothing
else**. Not the agent's prediction, not its confidence, not the issue type or
priority or assignee the ledger holds. A person shown the agent's answer is
not labelling the ticket, they are agreeing with the agent, and the resulting
number would measure nothing. `tests/api/test_autopilot_labelling.py` inspects
the response keys for exactly this.

**Answers are appended.** A change of mind is a new row; an undo is a row
saying `withdrawn`. `latest_answers` takes the newest row per labeller per
item, so the score is the current opinion and the history is still there.

**Demo mode pseudonymises the customer**, the same rule the review's demo
render follows: on a demo client a site reads as `Site A`, consistently within
a response, so these pages can be shown to a room without naming anybody.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client, ClientKind
from app.models.labelling import (
    GROUP_ANSWERS,
    LABELLING_KINDS,
    NOT_AN_ANSWER,
    ORIGIN_ANSWERS,
    LabellingAnswer,
    LabellingItem,
    LabellingSet,
)
from app.models.outcome import TicketOutcome
from app.models.shadow import ShadowPrediction, ShadowRun
from app.models.site import Site
from app.reports.tbr.demo import _letters
from app.tickets.scrub import scrub

#: How many tickets an origin sample holds, and how many groups a group sample
#: does. The brief's numbers, and the same ones the CSV export used.
ORIGIN_SIZE = 200
GROUP_SIZE = 30
#: Fixed, so two people labelling "the sample" label the same tickets and a
#: rebuilt set is the set it was.
SEED = 20261001


class UnknownSet(ValueError):
    """No labelling set of that id for this client."""


class NotLabellable(ValueError):
    """The answer is not one this kind of set accepts."""


def answers_for(kind: str) -> tuple[str, ...]:
    if kind == "origin":
        return ORIGIN_ANSWERS
    if kind == "groups":
        return GROUP_ANSWERS
    raise NotLabellable(f"{kind!r} is not a labelling kind")


@dataclass
class Progress:
    """How far a set has got, per labeller and overall."""

    total: int = 0
    answered: int = 0
    """Items with a current answer from anybody that is not a skip or a
    withdrawal."""
    skipped: int = 0
    mine: int = 0
    """Items the person asking has a current answer for."""
    double_check: int = 0
    double_check_done: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "answered": self.answered,
            "skipped": self.skipped,
            "mine": self.mine,
            "double_check": self.double_check,
            "double_check_done": self.double_check_done,
        }


@dataclass
class Pseudonymiser:
    """Site names as `Site A`, consistently, for a demo client.

    One instance per response, so the same customer is the same letter all the
    way down a page — the rule the review's demo render follows, for the same
    reason: a page shown to a room should not name somebody else's customer.
    """

    on: bool = False
    _seen: dict[uuid.UUID, str] = field(default_factory=dict)

    def site(self, site_id: uuid.UUID | None, name: str | None) -> str:
        if site_id is None:
            return name or "no customer"
        if not self.on:
            return name or f"site {str(site_id)[:8]}"
        if site_id not in self._seen:
            self._seen[site_id] = f"Site {_letters(len(self._seen))}"
        return self._seen[site_id]


async def pseudonymiser_for(session: AsyncSession, client_id: uuid.UUID) -> Pseudonymiser:
    client = await session.get(Client, client_id)
    return Pseudonymiser(on=bool(client and client.kind == ClientKind.DEMO))


# --- building a set -------------------------------------------------------


def _stratified(rows: list[TicketOutcome], *, wanted: int, seed: int) -> list[TicketOutcome]:
    """A sample spread over issue type and site rather than over the list.

    Round-robin across (issue type, site) buckets in a shuffled order. Sorted
    instead, the first pass walks them alphabetically and a 200-row sample of
    433 buckets never reaches the issue types late in the alphabet — measured:
    8 of 15. The same sampler the CSV export uses, so a set and a sheet are the
    same sample.
    """
    buckets: dict[tuple[str, Any], list[TicketOutcome]] = defaultdict(list)
    for row in rows:
        buckets[(row.issue_type_label or "(not set)", row.site_id)].append(row)
    shuffler = random.Random(seed)  # noqa: S311  seeded for reproducibility
    for bucket in buckets.values():
        shuffler.shuffle(bucket)
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
            break
        depth += 1
    return picked


async def build_set(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    run_id: uuid.UUID,
    kind: str,
    created_by: str,
    size: int | None = None,
    seed: int = SEED,
    double_check: int = 0,
    now: dt.datetime | None = None,
) -> LabellingSet:
    """The sample for one run, or the one that already exists.

    Idempotent per (run, kind): a second call returns the set rather than a
    second sample of the same question. Two opinions of one sample is what the
    double-check is for.
    """
    if kind not in LABELLING_KINDS:
        raise NotLabellable(f"{kind!r} is not a labelling kind")
    existing = (
        await session.execute(
            select(LabellingSet).where(LabellingSet.run_id == run_id, LabellingSet.kind == kind)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    run = await session.get(ShadowRun, run_id)
    if run is None or run.client_id != client_id:
        raise UnknownSet(f"no shadow run {run_id} for this client")

    subjects: list[str] = []
    if kind == "origin":
        rows = (
            (
                await session.execute(
                    select(TicketOutcome).where(TicketOutcome.client_id == client_id)
                )
            )
            .scalars()
            .all()
        )
        wanted = size or ORIGIN_SIZE
        subjects = [row.ticket_id for row in _stratified(list(rows), wanted=wanted, seed=seed)]
    else:
        # A group is named by the ticket that opened it, and only groups that
        # somebody actually joined are worth judging: a group of one is the
        # agent declining to group, which is not what the spot-check asks.
        joined = (
            (
                await session.execute(
                    select(ShadowPrediction.group_with)
                    .where(
                        ShadowPrediction.run_id == run_id,
                        ShadowPrediction.group_with.is_not(None),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        leaders = sorted(leader for leader in joined if leader)
        random.Random(seed).shuffle(leaders)  # noqa: S311  seeded
        subjects = leaders[: (size or GROUP_SIZE)]

    built = LabellingSet(
        id=uuid.uuid4(),
        client_id=client_id,
        run_id=run_id,
        kind=kind,
        size=len(subjects),
        seed=seed,
        double_check=min(double_check, len(subjects)),
        created_at=now or dt.datetime.now(dt.UTC),
        created_by=created_by,
    )
    session.add(built)
    await session.flush()
    for position, subject in enumerate(subjects):
        session.add(
            LabellingItem(
                set_id=built.id,
                position=position,
                subject_id=subject,
                double_check=position < built.double_check,
            )
        )
    await session.flush()
    return built


# --- what a labeller sees -------------------------------------------------


async def _tickets(
    session: AsyncSession, client_id: uuid.UUID, ids: Sequence[str]
) -> dict[str, TicketOutcome]:
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(TicketOutcome).where(
                    TicketOutcome.client_id == client_id, TicketOutcome.ticket_id.in_(list(ids))
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.ticket_id: row for row in rows}


async def _site_names(session: AsyncSession, client_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = (
        await session.execute(select(Site.id, Site.name).where(Site.client_id == client_id))
    ).all()
    return {site_id: name for site_id, name in rows}


def _ticket_view(
    row: TicketOutcome, names: dict[uuid.UUID, str], masks: Pseudonymiser
) -> dict[str, Any]:
    """One ticket as a labeller may see it, and no more than that.

    The four facts a person needs to judge where a ticket came from. Every
    other column on the row is either the agent's business or the answer.
    """
    return {
        "ticket_id": row.ticket_id,
        "ticket_number": row.ticket_number,
        "title": scrub(row.title or "").text,
        "site": masks.site(row.site_id, names.get(row.site_id) if row.site_id else None),
        "created_at": row.created_at_vendor.isoformat() if row.created_at_vendor else None,
        "queue": row.queue_label,
    }


async def item_view(
    session: AsyncSession,
    labelling_set: LabellingSet,
    item: LabellingItem,
    *,
    masks: Pseudonymiser,
    names: dict[uuid.UUID, str] | None = None,
) -> dict[str, Any]:
    """The item to judge, blind: the ticket, or the group's tickets in order."""
    names = names if names is not None else await _site_names(session, labelling_set.client_id)
    if labelling_set.kind == "origin":
        found = await _tickets(session, labelling_set.client_id, [item.subject_id])
        row = found.get(item.subject_id)
        ticket = (
            _ticket_view(row, names, masks)
            if row is not None
            else {"ticket_id": item.subject_id, "title": None}
        )
        return {
            "item_id": item.id,
            "position": item.position,
            "kind": "origin",
            "double_check": item.double_check,
            "ticket": ticket,
        }

    members = (
        (
            await session.execute(
                select(ShadowPrediction.ticket_id).where(
                    ShadowPrediction.run_id == labelling_set.run_id,
                    ShadowPrediction.group_with == item.subject_id,
                )
            )
        )
        .scalars()
        .all()
    )
    wanted = [item.subject_id, *members]
    found = await _tickets(session, labelling_set.client_id, wanted)
    tickets = [_ticket_view(found[key], names, masks) for key in wanted if key in found]
    tickets.sort(key=lambda view: view.get("created_at") or "")
    return {
        "item_id": item.id,
        "position": item.position,
        "kind": "groups",
        "double_check": item.double_check,
        "leader_id": item.subject_id,
        "tickets": tickets,
    }


# --- answers --------------------------------------------------------------


async def latest_answers(
    session: AsyncSession, set_id: uuid.UUID
) -> dict[int, dict[str, LabellingAnswer]]:
    """item id -> labeller -> their current answer.

    The newest row per labeller per item. Everything older is history, which is
    the point of appending rather than updating.
    """
    rows = (
        (
            await session.execute(
                select(LabellingAnswer)
                .join(LabellingItem, LabellingItem.id == LabellingAnswer.item_id)
                .where(LabellingItem.set_id == set_id)
                .order_by(LabellingAnswer.created_at, LabellingAnswer.id)
            )
        )
        .scalars()
        .all()
    )
    current: dict[int, dict[str, LabellingAnswer]] = defaultdict(dict)
    for row in rows:
        current[row.item_id][row.labeller_ref] = row
    return current


async def record_answer(
    session: AsyncSession,
    labelling_set: LabellingSet,
    item: LabellingItem,
    *,
    answer: str,
    labeller_ref: str,
    labeller_user_id: uuid.UUID | None,
    note: str | None = None,
    excluded: list[str] | None = None,
    source: str = "page",
    now: dt.datetime | None = None,
) -> LabellingAnswer:
    """One answer, appended. Never an update."""
    allowed = answers_for(labelling_set.kind)
    if answer not in allowed:
        raise NotLabellable(
            f"{answer!r} is not one of {', '.join(allowed)} for a {labelling_set.kind} set"
        )
    row = LabellingAnswer(
        item_id=item.id,
        labeller_user_id=labeller_user_id,
        labeller_ref=labeller_ref,
        answer=answer,
        # A person typing a note is as likely to paste a password as a person
        # typing a ticket, so the same scrubber runs on it.
        note=(scrub(note).text[:300] if note else None),
        excluded=list(excluded) if excluded else None,
        source=source,
        created_at=now or dt.datetime.now(dt.UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def undo_last(
    session: AsyncSession,
    labelling_set: LabellingSet,
    *,
    labeller_ref: str,
    labeller_user_id: uuid.UUID | None,
    now: dt.datetime | None = None,
) -> LabellingAnswer | None:
    """Withdraw this labeller's most recent answer in this set.

    A row rather than a deletion: the item reads as unanswered again and what
    happened is still on the record.
    """
    newest = (
        await session.execute(
            select(LabellingAnswer)
            .join(LabellingItem, LabellingItem.id == LabellingAnswer.item_id)
            .where(
                LabellingItem.set_id == labelling_set.id,
                LabellingAnswer.labeller_ref == labeller_ref,
            )
            .order_by(desc(LabellingAnswer.created_at), desc(LabellingAnswer.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if newest is None or newest.answer == "withdrawn":
        return None
    item = await session.get(LabellingItem, newest.item_id)
    if item is None:
        return None
    return await record_answer(
        session,
        labelling_set,
        item,
        answer="withdrawn",
        labeller_ref=labeller_ref,
        labeller_user_id=labeller_user_id,
        now=now,
    )


async def progress_of(
    session: AsyncSession, labelling_set: LabellingSet, *, labeller_ref: str
) -> Progress:
    items = (
        (
            await session.execute(
                select(LabellingItem)
                .where(LabellingItem.set_id == labelling_set.id)
                .order_by(LabellingItem.position)
            )
        )
        .scalars()
        .all()
    )
    current = await latest_answers(session, labelling_set.id)
    progress = Progress(total=len(items))
    for item in items:
        answers = current.get(item.id, {})
        real = [row for row in answers.values() if row.counts]
        if real:
            progress.answered += 1
        elif any(row.answer == "skipped" for row in answers.values()):
            progress.skipped += 1
        mine = answers.get(labeller_ref)
        if mine is not None and mine.counts:
            progress.mine += 1
        if item.double_check:
            progress.double_check += 1
            if len({row.labeller_ref for row in real}) >= 2:
                progress.double_check_done += 1
    return progress


async def item_at(
    session: AsyncSession, labelling_set: LabellingSet, position: int
) -> LabellingItem | None:
    """Exactly the item at that position, answered or not.

    `next_item` walks past what the labeller has already judged, which is what
    labelling two hundred tickets wants. Going **back** to look at one of them
    again is the other thing a person does, and relabelling is allowed, so the
    page needs a way to ask for one by position rather than by "the next one".
    """
    return (
        await session.execute(
            select(LabellingItem).where(
                LabellingItem.set_id == labelling_set.id,
                LabellingItem.position == position,
            )
        )
    ).scalar_one_or_none()


async def next_item(
    session: AsyncSession,
    labelling_set: LabellingSet,
    *,
    labeller_ref: str,
    after: int | None = None,
) -> LabellingItem | None:
    """The next item this labeller has not answered, from `after` onwards.

    A double-check item they have not answered counts as unanswered for them
    even when somebody else has: that is what makes a second opinion possible.
    """
    items = (
        (
            await session.execute(
                select(LabellingItem)
                .where(LabellingItem.set_id == labelling_set.id)
                .order_by(LabellingItem.position)
            )
        )
        .scalars()
        .all()
    )
    current = await latest_answers(session, labelling_set.id)
    start = 0 if after is None else after + 1
    for item in items:
        if item.position < start:
            continue
        mine = current.get(item.id, {}).get(labeller_ref)
        if mine is not None and mine.answer not in ("withdrawn",):
            continue
        if item.double_check or not current.get(item.id):
            return item
        # Somebody else has answered a single-label item: nothing to add.
        others = [row for row in current.get(item.id, {}).values() if row.counts]
        if not others:
            return item
    return None


# --- what a score reads back ----------------------------------------------


@dataclass
class OriginLabels:
    """The current origin labels of a run, for scoring."""

    by_ticket: dict[str, str] = field(default_factory=dict)
    labelled: int = 0
    skipped: int = 0
    unlabelled: int = 0
    disagreed: int = 0
    """Double-checked items whose two labellers do not agree. Left out of the
    scored set: a label two people cannot agree on is not ground truth."""
    agreement_pairs: int = 0
    agreement_same: int = 0

    @property
    def agreement(self) -> float | None:
        return self.agreement_same / self.agreement_pairs if self.agreement_pairs else None


async def origin_labels(
    session: AsyncSession, *, client_id: uuid.UUID, run_id: uuid.UUID
) -> OriginLabels:
    """The run's origin labels as they stand, with the double-check agreement."""
    found = OriginLabels()
    labelling_set = (
        await session.execute(
            select(LabellingSet).where(
                LabellingSet.run_id == run_id,
                LabellingSet.kind == "origin",
                LabellingSet.client_id == client_id,
            )
        )
    ).scalar_one_or_none()
    if labelling_set is None:
        return found
    items = (
        (
            await session.execute(
                select(LabellingItem).where(LabellingItem.set_id == labelling_set.id)
            )
        )
        .scalars()
        .all()
    )
    current = await latest_answers(session, labelling_set.id)
    for item in items:
        answers = current.get(item.id, {})
        real = [row for row in answers.values() if row.counts]
        if not real:
            if any(row.answer == "skipped" for row in answers.values()):
                found.skipped += 1
            else:
                found.unlabelled += 1
            continue
        chosen = {row.answer for row in real}
        if len(real) >= 2:
            found.agreement_pairs += 1
            found.agreement_same += 1 if len(chosen) == 1 else 0
        if len(chosen) > 1:
            # Two people, two answers. Not ground truth, and not quietly
            # resolved by taking the newer or the senior one.
            found.disagreed += 1
            continue
        found.by_ticket[item.subject_id] = next(iter(chosen))
        found.labelled += 1
    return found


@dataclass
class GroupJudgements:
    """What people said about a run's groups."""

    judged: int = 0
    same: int = 0
    partly: int = 0
    not_same: int = 0
    skipped: int = 0
    unjudged: int = 0
    excluded_tickets: int = 0
    """Tickets a labeller marked as not belonging to the group they were in."""

    @property
    def correct_share(self) -> float | None:
        """The spot-check figure: the share judged to be one thing.

        `partly` is not correct. A group a person had to carve up is a group
        the agent got wrong, and counting it as a pass would make the gate's
        90% unreachable in the only direction that matters.
        """
        return self.same / self.judged if self.judged else None

    def as_json(self) -> dict[str, Any]:
        return {
            "judged": self.judged,
            "same": self.same,
            "partly": self.partly,
            "not_same": self.not_same,
            "skipped": self.skipped,
            "unjudged": self.unjudged,
            "excluded_tickets": self.excluded_tickets,
            "correct_share": self.correct_share,
        }


async def group_judgements(
    session: AsyncSession, *, client_id: uuid.UUID, run_id: uuid.UUID
) -> GroupJudgements:
    found = GroupJudgements()
    labelling_set = (
        await session.execute(
            select(LabellingSet).where(
                LabellingSet.run_id == run_id,
                LabellingSet.kind == "groups",
                LabellingSet.client_id == client_id,
            )
        )
    ).scalar_one_or_none()
    if labelling_set is None:
        return found
    items = (
        (
            await session.execute(
                select(LabellingItem).where(LabellingItem.set_id == labelling_set.id)
            )
        )
        .scalars()
        .all()
    )
    current = await latest_answers(session, labelling_set.id)
    for item in items:
        answers = current.get(item.id, {})
        real = [row for row in answers.values() if row.counts]
        if not real:
            if any(row.answer == "skipped" for row in answers.values()):
                found.skipped += 1
            else:
                found.unjudged += 1
            continue
        newest = max(real, key=lambda row: (row.created_at, row.id))
        found.judged += 1
        if newest.answer == "same":
            found.same += 1
        elif newest.answer == "partly":
            found.partly += 1
            found.excluded_tickets += len(newest.excluded or [])
        elif newest.answer == "not_same":
            found.not_same += 1
    return found


#: Re-exported for the API layer, which has no business importing a model's
#: vocabulary module for one tuple.
__all__ = [
    "GROUP_SIZE",
    "NOT_AN_ANSWER",
    "ORIGIN_SIZE",
    "SEED",
    "GroupJudgements",
    "NotLabellable",
    "OriginLabels",
    "Progress",
    "Pseudonymiser",
    "UnknownSet",
    "answers_for",
    "build_set",
    "group_judgements",
    "item_at",
    "item_view",
    "latest_answers",
    "next_item",
    "origin_labels",
    "progress_of",
    "pseudonymiser_for",
    "record_answer",
    "undo_last",
]

"""/autopilot/labelling/*: a person judging tickets, one at a time.

The page at `/autopilot/labelling` is the HTML; everything it knows comes from
here. Inputiv staff only (`INPUTIV_ANY`): a franchisee gets 403 on every route,
because this is a measurement of our agent and not of their estate.

**What a labeller is shown is the point of the design.** `GET /task` returns
the scrubbed subject, the customer, when the ticket arrived and the queue if we
know it — and no prediction, no confidence, no issue type, priority or
assignee. A person shown the agent's answer is agreeing with the agent rather
than labelling the ticket, and the number that comes out of that measures
nothing. The test for this inspects the response's keys rather than its values,
because a field added later would otherwise leak in unnoticed.

Every write is audited (`autopilot_labelled`): the set, the item and the answer,
never the note. A label is ground truth the gate is read against, so who said
what and when is part of the record.
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.auth.dependencies import INPUTIV_ANY
from app.auth.scope import Scope
from app.autopilot.labelling import (
    GROUP_SIZE,
    ORIGIN_SIZE,
    NotLabellable,
    build_set,
    item_at,
    item_view,
    latest_answers,
    next_item,
    origin_labels,
    progress_of,
    pseudonymiser_for,
    record_answer,
    undo_last,
)
from app.autopilot.shadow.replay import issue_types_for
from app.autopilot.suggestions import record_blind
from app.db.session import get_session
from app.models.enums import AuditEventType, AuditOutcome, Interface
from app.models.labelling import LabellingAnswer, LabellingItem, LabellingSet
from app.models.shadow import ShadowRun
from app.settings import get_settings

router = APIRouter(prefix="/autopilot/labelling", tags=["autopilot"])

#: Where the CSV exports live, for the one-off import of sheets filled in
#: before these pages existed. Git-ignored; see `app/autopilot/shadow/sheets.py`.
VAR = pathlib.Path("var") / "autopilot"


# --- what comes back ------------------------------------------------------


class SetSummary(BaseModel):
    """One labelling set, as the tab strip reads it."""

    model_config = ConfigDict(extra="forbid")

    set_id: uuid.UUID
    run_id: uuid.UUID
    kind: str
    size: int
    double_check: int
    created_at: dt.datetime
    created_by: str
    progress: dict[str, int]


class TaskView(BaseModel):
    """The item to judge, and how far the set has got.

    `item` carries only what a labeller may see. There is deliberately no
    field here for a prediction: the shape itself refuses one.
    """

    model_config = ConfigDict(extra="forbid")

    set_id: uuid.UUID
    kind: str
    issue_types: list[str] = Field(default_factory=list)
    """This franchisee's own labels, for the second question. Not a prediction:
    a person cannot answer "what type is it?" without the list of types."""
    answers: list[str]
    """What this kind of set accepts, so the page's buttons come from the
    server rather than being repeated in JavaScript."""
    item: dict[str, Any] | None
    progress: dict[str, int]
    finished: bool


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    answer: str
    issue_type: str | None = None
    """The second question, on an origin item: what type is it? Stored as a
    blind check rather than beside the origin answer, because that is the table
    the gate reads and because it is the same answer an engineer gives to the
    same question on a live ticket."""
    note: str | None = Field(default=None, max_length=300)
    excluded: list[str] = Field(default_factory=list)
    """For `partly`: the tickets in the group that do not belong."""


class UndoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set_id: uuid.UUID


class BuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    kind: str
    size: int | None = None


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    note: str | None
    excluded: list[str]
    labeller: str
    source: str
    at: dt.datetime


class ImportReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: int
    rows: int
    imported: int
    already: int
    unmatched: int
    unreadable: int


# --- the routes -----------------------------------------------------------


async def _set_for(db: AsyncSession, scope: Scope, set_id: uuid.UUID) -> LabellingSet:
    """One set of this client's, or a 404. The client comes from the session."""
    found = await db.get(LabellingSet, set_id)
    if found is None or found.client_id != scope.client_id:
        raise HTTPException(status_code=404, detail="No labelling set of that id.")
    return found


async def _item_of(db: AsyncSession, labelling_set: LabellingSet, item_id: int) -> LabellingItem:
    item = await db.get(LabellingItem, item_id)
    if item is None or item.set_id != labelling_set.id:
        raise HTTPException(status_code=404, detail="No item of that id in this set.")
    return item


def _context(scope: Scope) -> AuditContext:
    return AuditContext.new(
        actor_id=scope.user.email,
        actor_user_id=scope.user.id,
        interface=Interface.API,
        client_id=scope.client_id,
        client_ref=scope.client_slug,
    )


async def _summary(
    db: AsyncSession, labelling_set: LabellingSet, *, labeller_ref: str
) -> SetSummary:
    progress = await progress_of(db, labelling_set, labeller_ref=labeller_ref)
    return SetSummary(
        set_id=labelling_set.id,
        run_id=labelling_set.run_id,
        kind=labelling_set.kind,
        size=labelling_set.size,
        double_check=labelling_set.double_check,
        created_at=labelling_set.created_at,
        created_by=labelling_set.created_by,
        progress=progress.as_json(),
    )


@router.get("/sets", response_model=list[SetSummary])
async def list_sets(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> list[SetSummary]:
    """Every labelling set this client has, newest first."""
    rows = (
        (
            await db.execute(
                select(LabellingSet)
                .where(LabellingSet.client_id == scope.client_id)
                .order_by(LabellingSet.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await _summary(db, row, labeller_ref=scope.user.email) for row in rows]


@router.post("/sets", response_model=SetSummary)
async def create_set(
    payload: BuildIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> SetSummary:
    """The sample for one run and kind, built once and returned thereafter.

    Not a run and not a vendor call: it draws a sample from rows we already
    hold. Idempotent per (run, kind), so pressing the button twice is the same
    sample rather than a second opinion of the same question — which is what
    the double-check is for.
    """
    settings = get_settings()
    assert scope.client_id is not None  # the resolver guarantees it
    try:
        built = await build_set(
            db,
            client_id=scope.client_id,
            run_id=payload.run_id,
            kind=payload.kind,
            created_by=scope.user.email,
            size=payload.size or (ORIGIN_SIZE if payload.kind == "origin" else GROUP_SIZE),
            double_check=(settings.autopilot_double_label_count if payload.kind == "origin" else 0),
        )
    except NotLabellable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await record_event(
        db,
        _context(scope),
        event_type=AuditEventType.AUTOPILOT_LABELLED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "action": "set_built",
            "set_id": str(built.id),
            "run_id": str(built.run_id),
            "kind": built.kind,
            "size": built.size,
            "double_check": built.double_check,
        },
    )
    # `get_session` does not commit: a write route says so itself, as every
    # other write route in the application does.
    await db.commit()
    return await _summary(db, built, labeller_ref=scope.user.email)


async def _view(
    db: AsyncSession,
    scope: Scope,
    labelling_set: LabellingSet,
    *,
    after: int | None = None,
    at: int | None = None,
) -> TaskView:
    """The next item for this labeller, with what the page needs to draw it.

    `at` asks for one item by position whatever its answers, which is how the
    page goes back to something already judged; `after` asks for the next one
    this labeller has not judged, from there onwards.
    """
    from app.autopilot.labelling import answers_for  # local: one name, one use

    item = (
        await item_at(db, labelling_set, at)
        if at is not None
        else await next_item(db, labelling_set, labeller_ref=scope.user.email, after=after)
    )
    assert labelling_set.client_id is not None
    masks = await pseudonymiser_for(db, labelling_set.client_id)
    progress = await progress_of(db, labelling_set, labeller_ref=scope.user.email)
    return TaskView(
        set_id=labelling_set.id,
        kind=labelling_set.kind,
        issue_types=(
            await issue_types_for(db, labelling_set.client_id)
            if labelling_set.kind == "origin"
            else []
        ),
        answers=[
            one
            for one in answers_for(labelling_set.kind)
            # `withdrawn` is what undo writes, not something to offer a button
            # for: a labeller withdraws their last answer, not an arbitrary one.
            if one != "withdrawn"
        ],
        item=(await item_view(db, labelling_set, item, masks=masks) if item is not None else None),
        progress=progress.as_json(),
        finished=item is None,
    )


@router.get("/task", response_model=TaskView)
async def task(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    kind: Annotated[str, Query(pattern="^(origin|groups)$")] = "origin",
    run: Annotated[uuid.UUID | None, Query(description="a particular run's sample")] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
    at: Annotated[int | None, Query(ge=0)] = None,
) -> TaskView:
    """The next thing to judge, blind, with the set's progress.

    Blind is the whole design: what comes back is the subject, the customer,
    the arrival time and the queue. No prediction, no confidence, and none of
    the ledger's outcome columns — a person shown the agent's answer is
    agreeing with it rather than judging the ticket.

    `run` is optional and the page does not send it: somebody working through a
    sample is doing one job, not choosing between runs, and a picker at the top
    of it is a decision nobody asked for. Without one, the newest sample of
    that kind is the one in front of them.
    """
    query = select(LabellingSet).where(
        LabellingSet.client_id == scope.client_id, LabellingSet.kind == kind
    )
    if run is not None:
        query = query.where(LabellingSet.run_id == run)
    labelling_set = (
        await db.execute(query.order_by(LabellingSet.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    if labelling_set is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {kind} sample has been drawn yet.",
        )
    return await _view(db, scope, labelling_set, after=after, at=at)


@router.post("/answer", response_model=TaskView)
async def answer(
    payload: AnswerIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> TaskView:
    """Record one judgement and hand back the next item.

    An append, never an update: a labeller who answers the same item twice
    leaves two rows and the newer one scores.
    """
    item = await db.get(LabellingItem, payload.item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="No item of that id.")
    labelling_set = await _set_for(db, scope, item.set_id)
    try:
        stored = await record_answer(
            db,
            labelling_set,
            item,
            answer=payload.answer,
            labeller_ref=scope.user.email,
            labeller_user_id=scope.user.id,
            note=payload.note,
            excluded=payload.excluded,
        )
    except NotLabellable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.issue_type and labelling_set.kind == "origin":
        # The same question the quick check asks on a live ticket, so it lands
        # in the same table and the gate reads one population rather than two.
        await record_blind(
            db,
            client_id=labelling_set.client_id,
            ticket_id=item.subject_id,
            suggestion=None,
            answers={"issue_type": payload.issue_type},
            reviewer_ref=scope.user.email,
            reviewer_user_id=scope.user.id,
        )
    await record_event(
        db,
        _context(scope),
        event_type=AuditEventType.AUTOPILOT_LABELLED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "action": "answered",
            "set_id": str(labelling_set.id),
            "kind": labelling_set.kind,
            "item_id": item.id,
            "subject_id": item.subject_id,
            "answer": stored.answer,
            "issue_type": payload.issue_type,
            # The count and not the ids of what a labeller excluded: the ids
            # are on the row, and an audit payload is a summary.
            "excluded": len(payload.excluded),
            # Never the note. It is a person's words about a customer's ticket.
            "note": bool(payload.note),
        },
    )
    await db.commit()
    return await _view(db, scope, labelling_set, after=item.position)


@router.post("/undo", response_model=TaskView)
async def undo(
    payload: UndoIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> TaskView:
    """Withdraw this labeller's last answer in the set, and go back to it.

    A row saying `withdrawn` rather than a deletion, so the item reads as
    unanswered again and what happened is still on the record.
    """
    labelling_set = await _set_for(db, scope, payload.set_id)
    withdrawn = await undo_last(
        db,
        labelling_set,
        labeller_ref=scope.user.email,
        labeller_user_id=scope.user.id,
    )
    if withdrawn is None:
        # Nothing of this labeller's to take back. Not an error: pressing undo
        # on an untouched set is a reasonable thing to do by accident.
        return await _view(db, scope, labelling_set)
    item = await db.get(LabellingItem, withdrawn.item_id)
    await record_event(
        db,
        _context(scope),
        event_type=AuditEventType.AUTOPILOT_LABELLED,
        outcome=AuditOutcome.SUCCESS,
        payload={
            "action": "withdrawn",
            "set_id": str(labelling_set.id),
            "item_id": withdrawn.item_id,
            "subject_id": item.subject_id if item else None,
        },
    )
    # Back to the item just withdrawn, which is the one the labeller means to
    # look at again.
    await db.commit()
    return await _view(db, scope, labelling_set, after=(item.position - 1) if item else None)


@router.get("/history", response_model=list[HistoryEntry])
async def history(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    item: Annotated[int, Query(description="the item whose answers to list")],
) -> list[HistoryEntry]:
    """Every answer ever given for one item, oldest first.

    The reason answers append rather than update: a label somebody disputes can
    be looked into, including the withdrawals.
    """
    found = await db.get(LabellingItem, item)
    if found is None:
        raise HTTPException(status_code=404, detail="No item of that id.")
    await _set_for(db, scope, found.set_id)  # the client check, by its 404
    rows = (
        (
            await db.execute(
                select(LabellingAnswer)
                .where(LabellingAnswer.item_id == item)
                .order_by(LabellingAnswer.created_at, LabellingAnswer.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        HistoryEntry(
            answer=row.answer,
            note=row.note,
            excluded=list(row.excluded or []),
            labeller=row.labeller_ref,
            source=row.source,
            at=row.created_at,
        )
        for row in rows
    ]


@router.get("/agreement")
async def agreement(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    run: Annotated[uuid.UUID, Query(description="the run whose labels to compare")],
) -> dict[str, Any]:
    """How often two labellers of the same ticket said the same thing.

    The only measure we have of how hard the question is. A pair who disagree
    make that item **not** ground truth, and it is left out of the score rather
    than settled by taking the newer or the senior answer.
    """
    assert scope.client_id is not None
    labels = await origin_labels(db, client_id=scope.client_id, run_id=run)
    return {
        "labelled": labels.labelled,
        "skipped": labels.skipped,
        "unlabelled": labels.unlabelled,
        "disagreed": labels.disagreed,
        "pairs": labels.agreement_pairs,
        "agreed": labels.agreement_same,
        "agreement": labels.agreement,
        "definition": (
            "Of the double-checked tickets two people have both labelled, the share "
            "they labelled the same way. A disagreement is left out of the scored "
            "labels: a label two people cannot agree on is not ground truth."
        ),
    }


def _read_sheet(path: pathlib.Path) -> list[tuple[str, str, str | None]]:
    """A filled-in export as (ticket id, answer, note). Blank rows dropped.

    Both sheets are read by the same shape: the origin sheet answers in its
    `origin` column and the group sheet in `one_thing (yes/no)`, which a person
    fills with yes, no or partly.
    """
    found: list[tuple[str, str, str | None]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            ticket = (row.get("ticket_id") or "").strip()
            written = (row.get("origin") or row.get("one_thing (yes/no)") or "").strip().lower()
            if not ticket or not written:
                continue
            mapped = {
                "alert": "alert",
                "human": "human",
                "unsure": "unsure",
                "yes": "same",
                "no": "not_same",
                "partly": "partly",
            }.get(written)
            if mapped is None:
                found.append((ticket, "", None))  # counted as unreadable
                continue
            found.append((ticket, mapped, (row.get("note") or "").strip() or None))
    return found


@router.post("/import", response_model=ImportReport)
async def import_sheets(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    run: Annotated[uuid.UUID, Query(description="the run the sheets belong to")],
) -> ImportReport:
    """Read the filled-in CSV exports from `var/` into the tables, once.

    For the sheets filled in before these pages existed. Idempotent: a row
    already imported is counted and skipped, so running it twice does not
    double anybody's answers. A ticket the sample does not hold is reported
    rather than invented — the sheet and the set are the same sample, and a
    mismatch means they are not.
    """
    assert scope.client_id is not None
    run_row = await db.get(ShadowRun, run)
    if run_row is None or run_row.client_id != scope.client_id:
        raise HTTPException(status_code=404, detail="No shadow run of that id.")

    report = ImportReport(files=0, rows=0, imported=0, already=0, unmatched=0, unreadable=0)
    sets = (
        (
            await db.execute(
                select(LabellingSet).where(
                    LabellingSet.client_id == scope.client_id, LabellingSet.run_id == run
                )
            )
        )
        .scalars()
        .all()
    )
    if not sets:
        raise HTTPException(
            status_code=422, detail="Build the samples for this run before importing a sheet."
        )

    for labelling_set in sets:
        items = {
            item.subject_id: item
            for item in (
                (
                    await db.execute(
                        select(LabellingItem).where(LabellingItem.set_id == labelling_set.id)
                    )
                )
                .scalars()
                .all()
            )
        }
        current = await latest_answers(db, labelling_set.id)
        pattern = "*origin-labels*.csv" if labelling_set.kind == "origin" else "*groups-*.csv"
        for path in sorted(VAR.glob(pattern)):
            report.files += 1
            for ticket, written, note in _read_sheet(path):
                report.rows += 1
                if not written:
                    report.unreadable += 1
                    continue
                item = items.get(ticket)
                if item is None:
                    report.unmatched += 1
                    continue
                if any(row.source == "sheet" for row in current.get(item.id, {}).values()):
                    report.already += 1
                    continue
                await record_answer(
                    db,
                    labelling_set,
                    item,
                    answer=written,
                    labeller_ref=f"sheet:{path.name}",
                    # No account: a spreadsheet filled in by hand has no user
                    # id, and inventing the importer's would be a lie about who
                    # judged it.
                    labeller_user_id=None,
                    note=note,
                    source="sheet",
                )
                report.imported += 1

    await record_event(
        db,
        _context(scope),
        event_type=AuditEventType.AUTOPILOT_LABELLED,
        outcome=AuditOutcome.SUCCESS,
        payload={"action": "imported", "run_id": str(run), **report.model_dump()},
    )
    await db.commit()
    return report

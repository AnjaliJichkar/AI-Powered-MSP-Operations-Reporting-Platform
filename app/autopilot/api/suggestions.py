"""/autopilot/suggestions/*: the agent's answer, where the engineer works.

Three routes an engineer touches and one the queue reads. Everything about
them follows from two rules:

**A franchisee never receives a suggestion field.** Not a hidden one, not an
empty one: these routes are `INPUTIV_ANY` and refuse them, and the Tickets page
asks for none of this unless the session is staff, so a franchisee's queue is
byte for byte what it was before this existed.

**The blind check must not be able to leak.** While a ticket's blind answers
are missing, `GET /ticket/{id}` returns the *questions* and no suggestion at
all — there is no field in the payload for one to hide in — and the suggestion
arrives only on the response to the answer, or on a later read once the answer
is stored. A card that fetched both and hid one in the markup would be one
"view source" away from being worthless.

Writing "correct" is a row in our database. The note for Autotask is text the
page puts on a clipboard; pasting it is a person's keystroke, which is what
keeps Phase 1 read-only to every vendor.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.auth.dependencies import INPUTIV_ANY
from app.auth.scope import Scope
from app.autopilot.findings import (
    MAX_SHOWN,
    TOOLS,
    TicketFacts,
    findings_for,
    next_step_for,
)
from app.autopilot.shadow.replay import issue_types_for
from app.autopilot.suggestions import (
    CLASSIFICATION_REVIEW_FIELDS,
    ORIGIN_VALUES,
    URGENCY_VALUES,
    blind_answers_for,
    latest_for_ref,
    latest_for_tickets,
    latest_review_per_field,
    not_assessed_line,
    note_text,
    record_blind,
    record_review,
    review_state,
    reviews_of,
    shown_fields,
    suggestion_of,
)
from app.db.session import get_session
from app.models.client import Client
from app.models.device import Device
from app.models.enums import AuditEventType, AuditOutcome, Interface
from app.models.shadow import ShadowEnrichment, ShadowPrediction
from app.models.suggestion import (
    ALL_VERDICTS,
    BLIND_FIELDS,
    BLIND_ORIGIN_ANSWERS,
    REVIEW_VERDICTS,
    USEFULNESS_VERDICTS,
    TicketSuggestion,
    is_review_field,
)
from app.settings import get_settings

router = APIRouter(prefix="/autopilot/suggestions", tags=["autopilot"])

#: What the card says when the forward shadow is off on a live client. The
#: switch is `AUTOPILOT_SHADOW_LIVE` and it is turned on by a person editing
#: settings, never from a page.
OFF_DETAIL = (
    "Suggestions are off. The forward shadow is not polling for new tickets, so "
    "nothing has been suggested on this client's live tickets. The switch is "
    "AUTOPILOT_SHADOW_LIVE and a person turns it on in settings."
)


# --- what comes back --------------------------------------------------------


class Chip(BaseModel):
    """One ticket's row in the queue's Desk column.

    The findings and not the classification: Autotask's own list already shows
    a type beside every ticket, and a second one of ours in the same row is
    noise. What it does not show is that the machine has been offline for ten
    days, which is what decides who picks the ticket up.
    """

    model_config = ConfigDict(extra="forbid")

    findings: list[dict[str, Any]] = Field(default_factory=list)
    """The top two, each `{kind, tone, chip}`. Two, because that is what a row
    holds before it wraps, and a wrapped row is one nobody scans."""
    more: int = 0
    """How many did not fit, drawn as "+N" and opened on the ticket's page."""
    warning: bool = False
    """Whether any finding is a warning: what the third filter pill counts."""
    reviewed: bool = False
    blind_pending: bool = False
    """A ticket that will ask before it tells. The queue does not say so — the
    row looks like any other — but what still needs review includes it."""


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    field_name: str = Field(alias="field")
    verdict: str
    corrected_value: str | None = None


class BlindIn(BaseModel):
    """The two questions, answered before anything is revealed."""

    model_config = ConfigDict(extra="forbid")

    origin: str
    issue_type: str


# --- helpers ----------------------------------------------------------------


def _context(scope: Scope) -> AuditContext:
    return AuditContext.new(
        actor_id=str(scope.user_id),
        actor_user_id=scope.user_id,
        interface=Interface.API,
        client_id=scope.client_id,
        client_ref=scope.client_slug,
    )


async def _is_demo(db: AsyncSession, client_id: uuid.UUID) -> bool:
    client = await db.get(Client, client_id)
    return bool(client and client.is_demo)


async def _suggestions_on(db: AsyncSession, client_id: uuid.UUID) -> bool:
    """Whether this client's suggestions are being made at all.

    Live clients need the forward shadow switched on, because that is what
    produces a suggestion for a live ticket. The demo tenant's suggestions come
    from a simulated pass over its own fixtures and are always available, which
    is what makes the page reviewable before the switch is thrown.
    """
    if get_settings().autopilot_shadow_live:
        return True
    return await _is_demo(db, client_id)


async def _enrichment_of(db: AsyncSession, suggestion: TicketSuggestion) -> ShadowEnrichment | None:
    if suggestion.enrichment_id is None:
        return None
    return await db.get(ShadowEnrichment, suggestion.enrichment_id)


async def _repeat_time(
    db: AsyncSession, suggestion: TicketSuggestion
) -> tuple[dt.datetime | None, str | None]:
    """When the ticket this one repeats arrived, and the number a person reads.

    Looked up rather than copied onto the row, because the earlier ticket is
    usually from an earlier pass: the pass that made this suggestion never saw
    it, and a number the page can find is better than a column that is null
    most of the time.
    """
    if not suggestion.group_with:
        return None, None
    number = suggestion.group_with_number
    if number is None:
        number = (
            await db.execute(
                select(TicketSuggestion.ticket_number).where(
                    TicketSuggestion.client_id == suggestion.client_id,
                    TicketSuggestion.ticket_id == suggestion.group_with,
                )
            )
        ).scalar_one_or_none()
    when = (
        await db.execute(
            select(ShadowPrediction.created_at_vendor).where(
                ShadowPrediction.run_id == suggestion.run_id,
                ShadowPrediction.ticket_id == suggestion.group_with,
            )
        )
    ).scalar_one_or_none()
    return when, number or suggestion.group_with


async def _enrichments_of(
    db: AsyncSession, suggestions: list[TicketSuggestion]
) -> dict[int, ShadowEnrichment]:
    """Every snapshot those suggestions point at, in one read.

    The queue draws its chips from the same findings the card does, so it needs
    the snapshots; one query for all of them rather than one per row, because a
    queue of 143 tickets is 143 round trips otherwise.
    """
    ids = [one.enrichment_id for one in suggestions if one.enrichment_id]
    if not ids:
        return {}
    rows = (
        (await db.execute(select(ShadowEnrichment).where(ShadowEnrichment.id.in_(ids))))
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


async def _predictions_of(
    db: AsyncSession, suggestions: list[TicketSuggestion]
) -> dict[int, ShadowPrediction]:
    """And the predictions beside them, for when each ticket arrived."""
    ids = [one.prediction_id for one in suggestions if one.prediction_id]
    if not ids:
        return {}
    rows = (
        (await db.execute(select(ShadowPrediction).where(ShadowPrediction.id.in_(ids))))
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}


# --- the routes -------------------------------------------------------------


@router.get("/queue")
async def queue(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    tickets: Annotated[str, Query(description="comma-separated ticket ids the queue is showing")],
) -> dict[str, Any]:
    """A chip per ticket, and how many still need a review.

    Takes the ids the queue is actually showing rather than returning every
    suggestion this client has: the queue is the vendor's open tickets and this
    table is every ticket ever suggested on, so the intersection is the only
    thing worth sending.
    """
    client_id = scope.require_client()
    if not await _suggestions_on(db, client_id):
        return {"live": False, "detail": OFF_DETAIL, "suggestions": {}, "needs_review": 0}
    wanted = [one.strip() for one in tickets.split(",") if one.strip()]
    found = await latest_for_tickets(db, client_id=client_id, ticket_ids=wanted)
    reviews = await reviews_of(db, [row.id for row in found.values()])
    # One read for every snapshot and prediction the rows point at, rather than
    # one per row: a queue of 143 tickets is 286 round trips otherwise.
    snapshots = await _enrichments_of(db, list(found.values()))
    predictions = await _predictions_of(db, list(found.values()))
    now = dt.datetime.now(dt.UTC)

    chips: dict[str, dict[str, Any]] = {}
    needs_review = 0
    warnings = 0
    for ticket_id, suggestion in found.items():
        enrichment = snapshots.get(suggestion.enrichment_id or -1)
        prediction = predictions.get(suggestion.prediction_id or -1)
        shown = shown_fields(suggestion)
        ranked = findings_for(
            {
                str(fact["id"]): fact.get("value")
                for fact in (enrichment.facts if enrichment else [])
                if fact.get("id")
            },
            ticket=TicketFacts(
                created_at=prediction.created_at_vendor if prediction else None,
                origin=suggestion.origin,
                repeat_of=suggestion.group_with if "repeat" in shown else None,
                repeat_of_number=suggestion.group_with_number,
            ),
            now=now,
        )
        if not ranked and not shown:
            # Nothing found and nothing confident. No chip, and nobody is
            # waiting on it: there is nothing to review.
            continue
        # Finished, rather than touched: the classification lines answered, or
        # the card's own thumbs given. The same function the card's
        # "(N to review)" counts with, so the two cannot drift.
        answered = review_state(
            suggestion,
            latest_review_per_field(reviews.get(suggestion.id, [])),
            has_machine=enrichment is not None and enrichment.device_id is not None,
        ).finished
        blind_done = (
            bool(await blind_answers_for(db, client_id=client_id, ticket_id=ticket_id))
            or not suggestion.blind_check
        )
        has_warning = any(one.tone == "warning" for one in ranked)
        chips[ticket_id] = Chip(
            findings=[{"kind": one.kind, "tone": one.tone, "chip": one.chip} for one in ranked[:2]],
            more=max(0, len(ranked) - 2),
            warning=has_warning,
            reviewed=answered,
            blind_pending=not blind_done,
        ).model_dump()
        if not answered:
            needs_review += 1
        if has_warning:
            warnings += 1
    return {
        "live": True,
        "suggestions": chips,
        "needs_review": needs_review,
        "warnings": warnings,
    }


#: What each classification line is called on the card, in the words the
#: mockup uses. "Raised by" rather than "origin": a field name is not a
#: sentence, and this card is read in two seconds.
CLASSIFICATION_WORDS = {
    "issue_type": "Issue type",
    "origin": "Raised by",
    "urgency": "Urgency",
    "repeat": "Repeat of",
}

#: What a classification value reads as. Only origin needs translating; a type
#: is the franchisee's own label and an urgency is already a word.
ORIGIN_READS = {"alert": "A tool (alert)", "human": "A person", "unsure": "Could not tell"}


def _review_json(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "verdict": row.verdict,
        "corrected_value": row.corrected_value,
        "by": row.reviewer_ref,
        "at": row.created_at.isoformat(),
    }


async def _machine(db: AsyncSession, enrichment: ShadowEnrichment | None) -> dict[str, Any]:
    """Which machine the ticket is about, as the workspace decided it.

    The same resolution the ticket page's own panel shows, read from the
    snapshot rather than worked out again: two answers to "which machine" on
    one page is the thing this card exists to avoid.
    """
    if enrichment is None or enrichment.device_id is None:
        return {
            "device": None,
            "confidence": None,
            "method": enrichment.device_method if enrichment else "none",
            "candidates": enrichment.candidates if enrichment else 0,
        }
    device = await db.get(Device, enrichment.device_id)
    return {
        "device": device.canonical_name if device else None,
        "device_id": str(enrichment.device_id),
        "confidence": enrichment.device_confidence,
        "method": enrichment.device_method,
        "candidates": enrichment.candidates,
    }


async def _card(
    db: AsyncSession,
    scope: Scope,
    suggestion: TicketSuggestion,
    *,
    blind_pending: bool = False,
) -> dict[str, Any]:
    """The whole card: the estate first, the classification last.

    `blind_pending` hides **only** the classification section and swaps in the
    two questions. The findings stay, because they are what the card is for and
    because none of them is an answer to what is being asked — the one that
    could be, "this alert is two weeks old", is phrased without saying who
    raised it until the engineer has.
    """
    client_id = scope.require_client()
    enrichment = await _enrichment_of(db, suggestion)
    prediction = (
        await db.get(ShadowPrediction, suggestion.prediction_id)
        if suggestion.prediction_id
        else None
    )
    repeat_at, repeat_number = await _repeat_time(db, suggestion)
    if repeat_number and not suggestion.group_with_number:
        suggestion.group_with_number = repeat_number
    now = dt.datetime.now(dt.UTC)

    shown = shown_fields(suggestion)
    facts: dict[str, Any] = {
        str(fact["id"]): fact.get("value")
        for fact in (enrichment.facts if enrichment else [])
        if fact.get("id")
    }
    found = findings_for(
        facts,
        ticket=TicketFacts(
            created_at=prediction.created_at_vendor if prediction else None,
            # The agent's own reading of who raised it, which is what phrases a
            # stale ticket — and is withheld while that is the question.
            origin=None if blind_pending else suggestion.origin,
            repeat_of=suggestion.group_with if "repeat" in shown else None,
            repeat_of_number=suggestion.group_with_number,
            repeat_at=repeat_at,
        ),
        read_at=enrichment.read_at if enrichment else None,
        now=now,
    )

    reviews = (await reviews_of(db, [suggestion.id])).get(suggestion.id, [])
    current = latest_review_per_field(reviews)
    labels = await issue_types_for(db, client_id)

    findings = [
        {**one.as_json(), "review": _review_json(current.get(f"finding:{one.kind}"))}
        for one in found
    ]
    step = next_step_for(found)
    machine = await _machine(db, enrichment)
    machine["review"] = _review_json(current.get("machine"))

    classification: list[dict[str, Any]] = []
    if not blind_pending:
        for name in ("issue_type", "origin", "urgency", "repeat"):
            if name not in shown:
                continue
            value = suggestion.group_with_number if name == "repeat" else getattr(suggestion, name)
            classification.append(
                {
                    "field": name,
                    "label": CLASSIFICATION_WORDS[name],
                    "value": value,
                    "reads": ORIGIN_READS.get(value or "", value) if name == "origin" else value,
                    "ticket_id": suggestion.group_with if name == "repeat" else None,
                    "review": _review_json(current.get(name)),
                    "choices": {
                        "issue_type": labels,
                        "origin": list(ORIGIN_VALUES),
                        "urgency": list(URGENCY_VALUES),
                        "repeat": [],
                    }[name],
                }
            )

    # What is still waiting for somebody: the classification lines, which are
    # the ones a ticket is held in "Needs review" for. The findings' and the
    # next step's thumbs are answerable and counted and are not counted here.
    state = review_state(suggestion, current, has_machine=machine["device"] is not None)

    note = note_text(
        suggestion,
        machine=machine,
        findings=found,
        next_step=step.text if step else None,
        classification=classification,
        ticket_number=suggestion.ticket_number,
    )
    last = reviews[-1] if reviews else None
    return {
        "live": True,
        "ticket_id": suggestion.ticket_id,
        "suggestion_id": str(suggestion.id),
        "seen": True,
        "quiet": not (findings or classification or machine["device"]),
        "to_review": state.waiting,
        # How many lines this card asks for at all, so the header can tell
        # "everything has been confirmed" from "there was nothing to confirm",
        # and whether the ticket is off the queue's "Needs review" filter.
        "review_required": len(state.required),
        "reviewed": state.finished,
        "threshold": float(suggestion.display_threshold),
        "made_at": suggestion.created_at.isoformat(),
        "checked": {
            # How many tools were actually asked, which is not how many exist:
            # a tool with no link for this machine was not asked and says so
            # on its own line rather than looking like a tool that found
            # nothing.
            # Vendors, not us: the identity layer is where a fact about which
            # machine it is comes from, and counting Desk as a tool it checked
            # would flatter the number.
            "tools": len(
                {
                    TOOLS.get(str(fact.get("source")))
                    for fact in (enrichment.facts if enrichment else [])
                    if fact.get("source") and TOOLS.get(str(fact.get("source")), "Desk") != "Desk"
                }
            ),
            "as_of": enrichment.read_at.isoformat() if enrichment and enrichment.read_at else None,
        },
        "machine": machine,
        "findings": findings,
        "max_shown": MAX_SHOWN,
        "next_step": (
            {
                "text": step.text,
                "because": step.because,
                "review": _review_json(current.get("next_step")),
            }
            if step is not None
            else None
        ),
        "classification": classification,
        "not_assessed": not_assessed_line(enrichment),
        "note": note,
        "card_review": _review_json(current.get("card")),
        "last_actioned": (
            {"by": last.reviewer_ref, "at": last.created_at.isoformat()} if last else None
        ),
        "history": [
            {
                "field": row.field,
                "verdict": row.verdict,
                "corrected_value": row.corrected_value,
                "by": row.reviewer_ref,
                "at": row.created_at.isoformat(),
            }
            for row in reviews
        ],
    }


@router.get("/ticket/{ref}")
async def card(
    ref: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """One ticket's card: the questions first where it is a blind check.

    While a blind check is unanswered the payload holds the questions and no
    suggestion — not a hidden one, not an empty one. There is nowhere in this
    response for the agent's answer to sit, which is what makes the answer that
    comes back worth scoring.
    """
    client_id = scope.require_client()
    if not await _suggestions_on(db, client_id):
        return {"live": False, "detail": OFF_DETAIL, "ticket_id": ref}
    suggestion = await latest_for_ref(db, client_id=client_id, ref=ref)
    if suggestion is None:
        # Never suggested on, which is not the same as suggested on and quiet:
        # one is a ticket the agent has not seen, the other a ticket it saw and
        # had nothing confident to say about. The card says which.
        return {
            "live": True,
            "ticket_id": ref,
            "suggestion_id": None,
            "seen": False,
            "quiet": True,
        }
    if suggestion.blind_check:
        answered = await blind_answers_for(db, client_id=client_id, ticket_id=suggestion.ticket_id)
        if not all(name in answered for name in BLIND_FIELDS):
            # The card, minus the classification, plus the two questions. The
            # findings are what the card is for and none of them answers what
            # is being asked.
            card = await _card(db, scope, suggestion, blind_pending=True)
            return {
                **card,
                "blind": {
                    "pending": True,
                    # The engineer is not told why this ticket asks first. It
                    # says what it wants, not what it is for.
                    "title": "Quick check before suggestions",
                    "questions": [
                        {
                            "field": "origin",
                            "text": "Was this raised by a tool or a person?",
                            "choices": [
                                {"value": "alert", "label": "Tool"},
                                {"value": "human", "label": "Person"},
                                {"value": "unsure", "label": "Can't tell"},
                            ],
                        },
                        {
                            "field": "issue_type",
                            "text": "What type is it?",
                            "choices": [
                                {"value": label, "label": label}
                                for label in await issue_types_for(db, client_id)
                            ]
                            + [{"value": "unsure", "label": "Not sure"}],
                        },
                    ],
                },
            }
    return await _card(db, scope, suggestion)


async def _audit(db: AsyncSession, scope: Scope, *, action: str, payload: dict[str, Any]) -> None:
    await record_event(
        db,
        _context(scope),
        event_type=AuditEventType.AUTOPILOT_SUGGESTION_REVIEWED,
        outcome=AuditOutcome.SUCCESS,
        payload={"action": action, **payload},
    )


async def _write_review(
    db: AsyncSession,
    scope: Scope,
    suggestion: TicketSuggestion,
    *,
    field_name: str,
    verdict: str,
    corrected_value: str | None,
) -> None:
    await record_review(
        db,
        suggestion,
        field_name=field_name,
        verdict=verdict,
        corrected_value=corrected_value,
        reviewer_ref=scope.user.email,
        reviewer_user_id=scope.user.id,
    )


@router.post("/{suggestion_id}/review")
async def review(
    suggestion_id: uuid.UUID,
    payload: ReviewIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """One field's verdict, appended.

    A second verdict on the same field is a new row and the latest one counts,
    so changing your mind is an ordinary thing to do and the record says it
    happened.
    """
    client_id = scope.require_client()
    if not is_review_field(payload.field_name):
        raise HTTPException(status_code=422, detail="Not something a verdict is stored against.")
    if payload.verdict not in ALL_VERDICTS:
        raise HTTPException(
            status_code=422,
            detail="Not a verdict: correct, changed or wrong for a classification; "
            "useful or not_useful for a finding, a next step or the card.",
        )
    # The two vocabularies do not mix. "Was this correct?" about "machine
    # offline for 10 days" gets "yes, and I did not need telling", and
    # "was this useful?" about an issue type is not the question the gate's
    # readiness rows count.
    classification = payload.field_name in CLASSIFICATION_REVIEW_FIELDS
    if classification and payload.verdict not in REVIEW_VERDICTS:
        raise HTTPException(
            status_code=422, detail="A classification is correct, changed or wrong."
        )
    if not classification and payload.verdict not in USEFULNESS_VERDICTS:
        raise HTTPException(
            status_code=422, detail="A finding, a next step or the card is useful or not useful."
        )
    try:
        suggestion = await suggestion_of(db, client_id=client_id, suggestion_id=suggestion_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    corrected = payload.corrected_value
    if payload.verdict == "changed":
        if not corrected:
            raise HTTPException(
                status_code=422,
                detail="Changed to what? A correction with no value teaches the next run nothing.",
            )
        allowed = {
            "issue_type": await issue_types_for(db, client_id),
            "origin": list(ORIGIN_VALUES),
            "urgency": list(URGENCY_VALUES),
            "repeat": [],
        }[payload.field_name]
        if allowed and corrected not in allowed:
            raise HTTPException(
                status_code=422, detail="That value is not one this franchisee uses."
            )
    await _write_review(
        db,
        scope,
        suggestion,
        field_name=payload.field_name,
        verdict=payload.verdict,
        corrected_value=corrected,
    )
    await _audit(
        db,
        scope,
        action="reviewed",
        payload={
            "suggestion_id": str(suggestion.id),
            "ticket_id": suggestion.ticket_id,
            "field": payload.field_name,
            "verdict": payload.verdict,
            # The corrected label, which is a franchisee's own vocabulary and
            # not a person's words: safe to keep, and the point of the row.
            "corrected_value": corrected if payload.verdict == "changed" else None,
        },
    )
    await db.commit()
    return await _card(db, scope, suggestion)


@router.post("/{suggestion_id}/all-correct")
async def all_correct(
    suggestion_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """Every unanswered classification line accepted, in one click.

    The common case is that the agent got all four lines and the machine right,
    and making that five clicks is how a review step becomes a thing people
    learn to skip.

    Two things it does not do. The fields are decided here from what the card
    showed rather than taken from the request, so it can never approve
    something the engineer was never shown; and a line somebody has already
    answered is left alone, so a correction is never quietly overwritten with
    "correct" by somebody pressing this afterwards.
    """
    client_id = scope.require_client()
    try:
        suggestion = await suggestion_of(db, client_id=client_id, suggestion_id=suggestion_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    machine = await _machine(db, await _enrichment_of(db, suggestion))
    state = review_state(
        suggestion,
        latest_review_per_field((await reviews_of(db, [suggestion.id])).get(suggestion.id, [])),
        has_machine=machine["device"] is not None,
    )
    for name in state.unanswered:
        await _write_review(
            db, scope, suggestion, field_name=name, verdict="correct", corrected_value=None
        )
    await _audit(
        db,
        scope,
        action="all_correct",
        payload={
            "suggestion_id": str(suggestion.id),
            "ticket_id": suggestion.ticket_id,
            "fields": list(state.unanswered),
        },
    )
    await db.commit()
    return await _card(db, scope, suggestion)


@router.post("/{suggestion_id}/blind")
async def blind(
    suggestion_id: uuid.UUID,
    payload: BlindIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """The blind answers, and then the card.

    The answers are stored before the suggestion is built into the response, so
    there is no path through this that reveals anything first.
    """
    client_id = scope.require_client()
    try:
        suggestion = await suggestion_of(db, client_id=client_id, suggestion_id=suggestion_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    labels = await issue_types_for(db, client_id)
    if payload.origin not in BLIND_ORIGIN_ANSWERS:
        raise HTTPException(status_code=422, detail="Answer tool, person or can't tell.")
    if payload.issue_type != "unsure" and payload.issue_type not in labels:
        raise HTTPException(status_code=422, detail="That type is not one this franchisee uses.")
    await record_blind(
        db,
        client_id=client_id,
        ticket_id=suggestion.ticket_id,
        suggestion=suggestion,
        answers={"origin": payload.origin, "issue_type": payload.issue_type},
        reviewer_ref=scope.user.email,
        reviewer_user_id=scope.user.id,
    )
    await _audit(
        db,
        scope,
        action="blind_answered",
        payload={
            "suggestion_id": str(suggestion.id),
            "ticket_id": suggestion.ticket_id,
            # The answers themselves: they are the measurement, and they are
            # labels rather than anybody's words.
            "origin": payload.origin,
            "issue_type": payload.issue_type,
        },
    )
    await db.commit()
    return await _card(db, scope, suggestion)

"""Suggestions on a live ticket: making them, showing them, judging them.

This is the review that scales. The labelling sets put past tickets in front of
somebody with nothing else to do with them; this puts the agent's answer in
front of the engineer who is working the ticket anyway. The cost of that is
that approval is not accuracy — an engineer who has read "Looks like User
Access and Management" is agreeing with it — so the module keeps two things
apart on purpose:

* **Shown and approved.** Every field whose confidence clears
  `AUTOPILOT_DISPLAY_THRESHOLD` is offered with three verdicts. Those verdicts
  are reported as a readiness figure and never scored as accuracy.
* **Asked blind.** A share of suggested tickets ask the two questions with the
  suggestions hidden, and those answers are what the gate reads. Which tickets
  those are is decided when the suggestion is made and written down, so the
  card cannot change its mind on a refresh, and the engineer is never told
  which they are, because being told changes the answer.

Below the threshold nothing is shown at all. An agent that is not sure stays
quiet, and the card says so rather than offering a guess with a caveat nobody
reads.

**Nothing here writes to a vendor.** "Correct" is a row in our database. The
note for Autotask is text on a clipboard; pasting it is a person's keystroke,
which is what keeps Phase 1 read-only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import vendor_label
from app.models.shadow import ShadowEnrichment, ShadowPrediction
from app.models.suggestion import (
    BLIND_FIELDS,
    FINDING_PREFIX,
    REVIEW_VERDICTS,
    SUGGESTION_FIELDS,
    USEFULNESS_VERDICTS,
    BlindCheck,
    SuggestionReview,
    TicketSuggestion,
    is_review_field,
)

#: Autotask's ticket note body is 32,000 characters. The note this builds is a
#: handful of lines and nowhere near it; the cap is enforced anyway because a
#: paste that is silently truncated at the vendor is a note that lies.
NOTE_LIMIT = 32_000

#: The fields the classification tally counts: the agent's reading of the
#: ticket, which an engineer accepts, changes or rejects. `machine` is one of
#: them — which machine a ticket is about is a claim that is right or wrong,
#: not a line that is useful or not — and everything else on the card
#: (findings, the next step, the card itself) belongs to the other tally.
CLASSIFICATION_REVIEW_FIELDS = (*SUGGESTION_FIELDS, "machine")

#: What `urgency` may be changed to, and what `origin` may be changed to. Issue
#: types come from the tenant's own labels and are never a constant here.
URGENCY_VALUES = ("low", "normal", "high")
ORIGIN_VALUES = ("alert", "human")


logger = logging.getLogger(__name__)


class UnknownSuggestion(ValueError):
    """No suggestion of that id for this client."""


# --- which tickets ask first ------------------------------------------------


def is_blind(client_id: uuid.UUID, ticket_id: str, rate: float) -> bool:
    """Whether this ticket asks the engineer before it tells them.

    Deterministic on the ticket rather than random at the moment of asking, so
    the same ticket is always the same kind of card: a coin flipped per page
    load would show the questions, then the answers on a refresh, and the
    blind answer would be worth nothing. Hashed rather than taken modulo some
    id, because Autotask ticket ids run in sequence and every fifth one is not
    a random fifth of anything.
    """
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = hashlib.sha256(f"{client_id}:{ticket_id}".encode()).digest()
    # The first four bytes as a fraction of their range: uniform, stable, and
    # independent of how the vendor numbers its tickets.
    draw = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return draw < rate


# --- making one -------------------------------------------------------------


async def record_suggestion(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    prediction: ShadowPrediction,
    enrichment: ShadowEnrichment | None,
    display_threshold: float,
    blind_rate: float,
    ticket_number: str | None = None,
    group_with_number: str | None = None,
    now: dt.datetime | None = None,
) -> TicketSuggestion:
    """Write what the agent said about one ticket, as it was said.

    Takes the prediction rather than re-deriving anything: the suggestion an
    engineer sees and the prediction the gate scores must be the same answer,
    or the page and the scorecard are measuring two different agents.
    """
    moment = now or dt.datetime.now(dt.UTC)
    row = TicketSuggestion(
        id=uuid.uuid4(),
        client_id=client_id,
        run_id=prediction.run_id,
        prediction_id=prediction.id,
        enrichment_id=enrichment.id if enrichment is not None else None,
        ticket_id=prediction.ticket_id,
        ticket_number=ticket_number,
        site_id=prediction.site_id,
        issue_type=prediction.issue_type,
        issue_type_confidence=prediction.issue_type_confidence,
        origin=prediction.origin,
        origin_confidence=prediction.origin_confidence,
        urgency=prediction.urgency,
        urgency_confidence=prediction.urgency_confidence,
        group_with=prediction.group_with,
        group_with_number=group_with_number,
        group_score=prediction.group_score,
        display_threshold=display_threshold,
        blind_check=is_blind(client_id, prediction.ticket_id, blind_rate),
        created_at=moment,
    )
    session.add(row)
    await session.flush()
    return row


# --- reading them back ------------------------------------------------------


async def latest_for_tickets(
    session: AsyncSession, *, client_id: uuid.UUID, ticket_ids: list[str]
) -> dict[str, TicketSuggestion]:
    """The newest suggestion per ticket, for the tickets asked about."""
    if not ticket_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(TicketSuggestion)
                .where(
                    TicketSuggestion.client_id == client_id,
                    TicketSuggestion.ticket_id.in_(ticket_ids),
                )
                .order_by(TicketSuggestion.created_at)
            )
        )
        .scalars()
        .all()
    )
    # Ordered oldest first, so the last write per ticket wins: a second pass
    # over a ticket is a new reading, not a duplicate.
    return {row.ticket_id: row for row in rows}


async def latest_for_ticket(
    session: AsyncSession, *, client_id: uuid.UUID, ticket_id: str
) -> TicketSuggestion | None:
    found = await latest_for_tickets(session, client_id=client_id, ticket_ids=[ticket_id])
    return found.get(ticket_id)


async def latest_for_ref(
    session: AsyncSession, *, client_id: uuid.UUID, ref: str
) -> TicketSuggestion | None:
    """One ticket's newest suggestion, by the vendor's id **or** its number.

    Both, because the two halves of the product hold different ones: every
    autopilot table is keyed by the vendor's id, and a ticket workspace is
    opened by the number an engineer reads and stores that. Scoped to the
    client either way, and a number is matched only when no id does, so the
    lookup cannot be talked into crossing between the two.
    """
    by_id = await latest_for_ticket(session, client_id=client_id, ticket_id=ref)
    if by_id is not None:
        return by_id
    rows = (
        (
            await session.execute(
                select(TicketSuggestion)
                .where(
                    TicketSuggestion.client_id == client_id,
                    TicketSuggestion.ticket_number == ref,
                )
                .order_by(TicketSuggestion.created_at)
            )
        )
        .scalars()
        .all()
    )
    return rows[-1] if rows else None


async def suggestion_of(
    session: AsyncSession, *, client_id: uuid.UUID, suggestion_id: uuid.UUID
) -> TicketSuggestion:
    row = await session.get(TicketSuggestion, suggestion_id)
    if row is None or row.client_id != client_id:
        raise UnknownSuggestion("No suggestion of that id.")
    return row


async def reviews_of(
    session: AsyncSession, suggestion_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[SuggestionReview]]:
    """Every review of those suggestions, oldest first. Nothing is collapsed
    here: the history is what makes a changed verdict readable."""
    if not suggestion_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(SuggestionReview)
                .where(SuggestionReview.suggestion_id.in_(suggestion_ids))
                .order_by(SuggestionReview.created_at, SuggestionReview.id)
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, list[SuggestionReview]] = {}
    for row in rows:
        out.setdefault(row.suggestion_id, []).append(row)
    return out


def latest_review_per_field(reviews: list[SuggestionReview]) -> dict[str, SuggestionReview]:
    """The verdict that counts per field: the last one written."""
    current: dict[str, SuggestionReview] = {}
    for review in reviews:
        current[review.field] = review
    return current


async def blind_answers_for(
    session: AsyncSession, *, client_id: uuid.UUID, ticket_id: str
) -> dict[str, BlindCheck]:
    """The latest blind answer per field for one ticket."""
    rows = (
        (
            await session.execute(
                select(BlindCheck)
                .where(BlindCheck.client_id == client_id, BlindCheck.ticket_id == ticket_id)
                .order_by(BlindCheck.created_at, BlindCheck.id)
            )
        )
        .scalars()
        .all()
    )
    return {row.field: row for row in rows}


# --- what may be shown ------------------------------------------------------


def shown_fields(suggestion: TicketSuggestion) -> dict[str, float | None]:
    """The fields whose confidence clears the suggestion's own threshold.

    The threshold stored on the row, not today's setting: a suggestion made
    under one bar is not retroactively promoted by somebody lowering it.
    `repeat` has no confidence of its own — the grouping decision is
    deterministic — so it is shown whenever there is one.
    """
    floor = float(suggestion.display_threshold)
    out: dict[str, float | None] = {}
    for name in ("issue_type", "origin", "urgency"):
        value = getattr(suggestion, name)
        confidence = getattr(suggestion, f"{name}_confidence")
        if value is None or value == "unsure":
            continue
        if confidence is None or float(confidence) < floor:
            continue
        out[name] = float(confidence)
    if suggestion.group_with:
        out["repeat"] = float(suggestion.group_score) if suggestion.group_score else None
    return out


def has_anything_to_show(suggestion: TicketSuggestion) -> bool:
    return bool(shown_fields(suggestion))


@dataclass(frozen=True)
class ReviewState:
    """What is still waiting on one card, and whether anybody still need look.

    Two readers and one definition, because the queue's "Needs review" count
    and the card's own "(N to review)" are the same sentence said twice and a
    reader who finds them disagreeing has to assume one of them is wrong.

    **Only the classification lines are required**: which machine it is, the
    issue type, who raised it, the urgency and what it repeats. A finding's
    verdict and the next step's are worth having — they are what the results
    page reads to decide which findings earn their place — but they answer a
    different question. "Was this line worth the space" is feedback on us, and
    a ticket nobody felt strongly about the wording of is still a ticket
    somebody has finished reviewing.

    The card's own thumbs finish it too. Somebody who read the whole card and
    said what they made of it has reviewed it, and asking them to then accept
    four lines one at a time is how a review step becomes a thing people learn
    to skip.
    """

    required: tuple[str, ...]
    """The lines that must be answered, in the order the card draws them."""
    unanswered: tuple[str, ...]
    """Those of them with no verdict yet: what "All correct" would accept."""
    card_answered: bool

    @property
    def waiting(self) -> int:
        """The header's "(N to review)"."""
        return len(self.unanswered)

    @property
    def finished(self) -> bool:
        """Whether the ticket is off the queue's "Needs review" filter."""
        return self.card_answered or not self.unanswered


def review_state(
    suggestion: TicketSuggestion,
    current: Mapping[str, SuggestionReview],
    *,
    has_machine: bool,
) -> ReviewState:
    """What `suggestion` still needs, given the verdicts it already has.

    `has_machine` rather than the enrichment, because the two callers know it
    differently: the card has resolved the device to a name, the queue has only
    the snapshot's id and deliberately reads no device rows for a queue of a
    hundred tickets. Nothing deletes a device, so a snapshot with an id has a
    machine on its card.
    """
    shown = shown_fields(suggestion)
    required = (
        *(("machine",) if has_machine else ()),
        *(name for name in SUGGESTION_FIELDS if name in shown),
    )
    return ReviewState(
        required=required,
        unanswered=tuple(name for name in required if name not in current),
        card_answered="card" in current,
    )


# --- the words --------------------------------------------------------------


def not_assessed_line(enrichment: ShadowEnrichment | None) -> str | None:
    """One line naming the tools nobody asked, or nothing.

    A tool that was not asked is a gap in the evidence rather than a clean bill
    of health, which is the same rule the coverage board and the review follow.
    It is deliberately **not** a finding: a finding is something that changes
    how the ticket is handled, and "nobody asked Cove" changes nothing except
    how much the rest of the card is worth.
    """
    if enrichment is None or not enrichment.not_assessed:
        return None
    # Grouped by reason rather than one bracket per tool: five tools skipped
    # for the same reason is one fact, and reading it five times is how a line
    # meant to be informative becomes a line nobody finishes.
    by_reason: dict[str, list[str]] = {}
    # The vendor's name, not the connector's key. `connectsecure` on a card
    # an engineer reads is an internal spelling, and this line is the one
    # place the card would otherwise print one.
    for name in sorted(enrichment.not_assessed):
        by_reason.setdefault(str(enrichment.not_assessed[name]), []).append(vendor_label(name))
    parts = [f"{', '.join(names)} ({reason})" for reason, names in sorted(by_reason.items())]
    return "Not asked: " + "; ".join(parts) + "."


def note_text(
    suggestion: TicketSuggestion,
    *,
    machine: dict[str, Any] | None = None,
    findings: list[Any] | None = None,
    next_step: str | None = None,
    classification: list[dict[str, Any]] | None = None,
    ticket_number: str | None = None,
) -> str:
    """The plain-text note an engineer pastes into Autotask themselves.

    The card's own order — the machine, what the tools found, what to do, and
    the classification last — because the note is read by whoever opens the
    ticket next and they need the estate before they need a label.

    Capped at the vendor's own limit: a note silently cut off at the far end is
    a note that lies about what we found. It says what it is, so whoever reads
    it in the ticket knows it came from Desk and that nobody wrote it there.
    """
    head = ticket_number or suggestion.ticket_number or suggestion.ticket_id
    body = [f"{head} — Desk suggestion (not written by Desk; pasted by an engineer)"]
    if machine and machine.get("device"):
        body.append(f"Machine: {machine['device']}")
    for one in findings or []:
        detail = f" ({one.detail})" if getattr(one, "detail", None) else ""
        body.append(f"- {one.text}{detail} [{one.source}]")
    if next_step:
        body.append("")
        body.append(f"Suggested next step: {next_step}")
    if classification:
        body.append("")
        for line in classification:
            body.append(f"{line['label']}: {line.get('reads') or line.get('value') or ''}")
    body.append("")
    body.append("Suggestions only. Nothing here was applied to the ticket automatically.")
    return "\n".join(body)[:NOTE_LIMIT]


# --- writing a judgement ----------------------------------------------------


async def record_review(
    session: AsyncSession,
    suggestion: TicketSuggestion,
    *,
    field_name: str,
    verdict: str,
    corrected_value: str | None,
    reviewer_ref: str,
    reviewer_user_id: uuid.UUID | None,
    now: dt.datetime | None = None,
) -> SuggestionReview:
    """One verdict, appended. A second verdict on the same field is a new row."""
    if not is_review_field(field_name):
        raise ValueError(f"{field_name!r} is not something a verdict may be stored against")
    row = SuggestionReview(
        suggestion_id=suggestion.id,
        field=field_name,
        verdict=verdict,
        corrected_value=corrected_value if verdict == "changed" else None,
        reviewer_user_id=reviewer_user_id,
        reviewer_ref=reviewer_ref,
        created_at=now or dt.datetime.now(dt.UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def record_blind(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    ticket_id: str,
    suggestion: TicketSuggestion | None,
    answers: dict[str, str],
    reviewer_ref: str,
    reviewer_user_id: uuid.UUID | None,
    now: dt.datetime | None = None,
) -> list[BlindCheck]:
    """The blind answers, appended, before anything is revealed."""
    moment = now or dt.datetime.now(dt.UTC)
    written: list[BlindCheck] = []
    for name in BLIND_FIELDS:
        if name not in answers:
            continue
        row = BlindCheck(
            client_id=client_id,
            suggestion_id=suggestion.id if suggestion is not None else None,
            ticket_id=ticket_id,
            field=name,
            answer=answers[name],
            reviewer_user_id=reviewer_user_id,
            reviewer_ref=reviewer_ref,
            created_at=moment,
            revealed=True,
        )
        session.add(row)
        written.append(row)
    await session.flush()
    return written


# --- what the results page counts -------------------------------------------


@dataclass
class FindingTally:
    """How often one line of the card was shown, and what people made of it.

    A finding, the suggested next step or the card as a whole: the three things
    an engineer answers "useful" or "not useful" about. Kept apart from the
    classification tally because the question is different — a finding is not
    right or wrong, it is worth the space or it is not.

    `shown` is recomputed from the snapshots rather than stored. The findings
    are a pure function of rows we keep, and the thresholds in them only ever
    make a finding *more* true as time passes — a machine last seen ten days
    ago does not become recently seen — so a recount is what was shown, give or
    take a ticket that has since crossed a line.
    """

    shown: int = 0
    useful: int = 0
    not_useful: int = 0
    unrecognised: int = 0
    """Verdicts this tally has no column for, counted and shown rather than
    dropped — the same rule the classification tally follows."""

    @property
    def reviewed(self) -> int:
        return self.useful + self.not_useful

    @property
    def share_useful(self) -> float | None:
        return self.useful / self.reviewed if self.reviewed else None

    def add(self, verdict: str, *, field_name: str) -> None:
        if verdict in USEFULNESS_VERDICTS:
            setattr(self, verdict, getattr(self, verdict) + 1)
            return
        self.unrecognised += 1
        logger.warning(
            "suggestion review on %r carries verdict %r, which the findings tally "
            "has no column for; counted as unrecognised",
            field_name,
            verdict,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "shown": self.shown,
            "useful": self.useful,
            "not_useful": self.not_useful,
            "unrecognised": self.unrecognised,
            "reviewed": self.reviewed,
            "share_useful": self.share_useful,
        }


async def finding_review_counts(
    session: AsyncSession, *, client_id: uuid.UUID, now: dt.datetime | None = None
) -> dict[str, FindingTally]:
    """Per finding kind, plus the next step and the card: shown and useful.

    This is what decides which findings to keep, reword or drop. A kind shown
    four hundred times and marked useful twice is a line on four hundred cards
    that nobody wanted.

    It counts **only** the fields it owns — `finding:<kind>`, `next_step` and
    `card` — and the classification tally counts only its own. Neither is ever
    handed the other's rows, which is the bug this split exists to make
    impossible rather than to handle.
    """
    from app.autopilot.findings import TicketFacts, findings_for, next_step_for  # one use

    suggestions = (
        (
            await session.execute(
                select(TicketSuggestion).where(TicketSuggestion.client_id == client_id)
            )
        )
        .scalars()
        .all()
    )
    if not suggestions:
        return {}
    snapshots = {
        row.id: row
        for row in (
            (
                await session.execute(
                    select(ShadowEnrichment).where(
                        ShadowEnrichment.id.in_(
                            [one.enrichment_id for one in suggestions if one.enrichment_id]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    predictions = {
        row.id: row
        for row in (
            (
                await session.execute(
                    select(ShadowPrediction).where(
                        ShadowPrediction.id.in_(
                            [one.prediction_id for one in suggestions if one.prediction_id]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    tallies: dict[str, FindingTally] = {}
    moment = now or dt.datetime.now(dt.UTC)
    for suggestion in suggestions:
        enrichment = snapshots.get(suggestion.enrichment_id or -1)
        prediction = predictions.get(suggestion.prediction_id or -1)
        found = findings_for(
            {
                str(fact["id"]): fact.get("value")
                for fact in (enrichment.facts if enrichment else [])
                if fact.get("id")
            },
            ticket=TicketFacts(
                created_at=prediction.created_at_vendor if prediction else None,
                origin=suggestion.origin,
                repeat_of=suggestion.group_with,
                repeat_of_number=suggestion.group_with_number,
            ),
            now=moment,
        )
        for one in found:
            tallies.setdefault(one.kind, FindingTally()).shown += 1
        if next_step_for(found) is not None:
            tallies.setdefault("next_step", FindingTally()).shown += 1
        if found or has_anything_to_show(suggestion):
            # The card was drawn with something on it, which is what a thumb on
            # the footer is a verdict about.
            tallies.setdefault("card", FindingTally()).shown += 1

    reviews = await reviews_of(session, [one.id for one in suggestions])
    for rows in reviews.values():
        for name, review in latest_review_per_field(rows).items():
            if name.startswith(FINDING_PREFIX):
                kind = name.removeprefix(FINDING_PREFIX)
            elif name in ("next_step", "card"):
                kind = name
            else:
                # A classification verdict. Not this tally's row.
                continue
            tallies.setdefault(kind, FindingTally()).add(review.verdict, field_name=name)
    return tallies


@dataclass
class ReviewTally:
    """How often a classification was approved, corrected or rejected.

    Approval, which is **not** accuracy and is labelled as such everywhere it
    is drawn. It answers a different question: would an engineer have been
    helped, or interrupted.
    """

    correct: int = 0
    changed: int = 0
    wrong: int = 0
    unrecognised: int = 0
    """Verdicts this tally has no column for. Counted and shown rather than
    dropped: a row the page cannot read is a thing somebody should know about,
    and silently skipping it is how a figure quietly stops adding up."""

    @property
    def reviewed(self) -> int:
        return self.correct + self.changed + self.wrong

    @property
    def share_correct(self) -> float | None:
        return self.correct / self.reviewed if self.reviewed else None

    def add(self, verdict: str, *, field_name: str) -> None:
        """Count one verdict, or count that it could not be counted."""
        if verdict in REVIEW_VERDICTS:
            setattr(self, verdict, getattr(self, verdict) + 1)
            return
        self.unrecognised += 1
        logger.warning(
            "suggestion review on %r carries verdict %r, which the classification "
            "tally has no column for; counted as unrecognised",
            field_name,
            verdict,
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "correct": self.correct,
            "changed": self.changed,
            "wrong": self.wrong,
            "unrecognised": self.unrecognised,
            "reviewed": self.reviewed,
            "share_correct": self.share_correct,
        }


@dataclass
class LiveReviewCounts:
    """The live review section of the results page, in one object."""

    suggested: int = 0
    shown: int = 0
    reviewed_tickets: int = 0
    blind_tickets: int = 0
    by_field: dict[str, ReviewTally] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "suggested": self.suggested,
            "shown": self.shown,
            "reviewed_tickets": self.reviewed_tickets,
            "blind_tickets": self.blind_tickets,
            "by_field": {name: tally.as_json() for name, tally in sorted(self.by_field.items())},
        }


async def live_review_counts(session: AsyncSession, *, client_id: uuid.UUID) -> LiveReviewCounts:
    """Every suggestion this client has, and what engineers made of them."""
    suggestions = (
        (
            await session.execute(
                select(TicketSuggestion).where(TicketSuggestion.client_id == client_id)
            )
        )
        .scalars()
        .all()
    )
    counts = LiveReviewCounts(suggested=len(suggestions))
    counts.shown = sum(1 for row in suggestions if has_anything_to_show(row))
    reviews = await reviews_of(session, [row.id for row in suggestions])
    for rows in reviews.values():
        if not rows:
            continue
        counts.reviewed_tickets += 1
        for name, review in latest_review_per_field(rows).items():
            # Classification fields only. A finding's verdict is a different
            # question with a different vocabulary, counted by
            # `finding_review_counts`; handing it to this tally is what made
            # the results page raise.
            if name not in CLASSIFICATION_REVIEW_FIELDS:
                continue
            counts.by_field.setdefault(name, ReviewTally()).add(review.verdict, field_name=name)
    blind = (
        (
            await session.execute(
                select(BlindCheck.ticket_id).where(BlindCheck.client_id == client_id).distinct()
            )
        )
        .scalars()
        .all()
    )
    counts.blind_tickets = len(set(blind))
    return counts


__all__ = [
    "NOTE_LIMIT",
    "ORIGIN_VALUES",
    "URGENCY_VALUES",
    "BlindCheck",
    "FindingTally",
    "LiveReviewCounts",
    "ReviewTally",
    "SuggestionReview",
    "TicketSuggestion",
    "UnknownSuggestion",
    "blind_answers_for",
    "finding_review_counts",
    "has_anything_to_show",
    "is_blind",
    "latest_for_ref",
    "latest_for_ticket",
    "latest_for_tickets",
    "latest_review_per_field",
    "live_review_counts",
    "not_assessed_line",
    "note_text",
    "record_blind",
    "record_review",
    "record_suggestion",
    "reviews_of",
    "shown_fields",
    "suggestion_of",
]

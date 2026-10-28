"""What the agent suggested on a live ticket, and what an engineer made of it.

The labelling tables put a sample of *past* tickets in front of a person who
had nothing else to do with them. These three put the agent's answer in front
of the engineer who is working the ticket anyway, which is the only review that
scales — and which is also the one that cannot measure accuracy, because an
engineer who has read the agent's answer is agreeing with it rather than
judging the ticket. That is why there are two kinds of row here and not one:

* `suggestion_reviews` is approval. It says how often an engineer, having seen
  the suggestion, thought it right. It is reported and **never gated**.
* `blind_checks` is measurement. On a share of tickets the card asks the two
  questions *before* revealing anything, and those answers are what the gate
  scores. The engineer is not told which tickets those are, because knowing
  changes the answer.

Both are **append only**, like every other judgement in this product: a change
of mind is a new row and the latest one scores, so a review that moved is a
fact about the review rather than something that quietly replaced it.

**No ticket text here either.** A suggestion is labels, confidences and ids;
what an engineer reads comes from the ticket itself at display time. The
display threshold in force is stored on every row, so a figure read months
later knows which bar the suggestions it counted had to clear.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: The classification fields an engineer is offered a verdict on. `repeat` is
#: the grouping decision; it has a verdict like the rest because "this is not
#: the same issue" is exactly the correction the grouping threshold needs.
SUGGESTION_FIELDS = ("issue_type", "origin", "urgency", "repeat")

#: And the rest of the card: which machine it is, the suggested next step, and
#: the card as a whole. A finding's verdict is stored as `finding:<kind>`,
#: which is a prefix rather than a list because the kinds are a vocabulary that
#: grows (`app/autopilot/findings.py`) and a new finding should not need a
#: migration.
CARD_FIELDS = ("machine", "next_step", "card")
FINDING_PREFIX = "finding:"

#: What a reviewer can say about a classification. `changed` carries the value
#: they say it should have been, which is worth more than a bare "wrong": a
#: wrong answer nobody corrected teaches the next run nothing.
REVIEW_VERDICTS = ("correct", "changed", "wrong")
#: And about a finding, a next step or the card. A finding is not right or
#: wrong the way a type is — it is either worth the space it took or it is
#: not, and "was this correct?" about "machine offline for 10 days" gets the
#: answer "yes, and I did not need telling".
USEFULNESS_VERDICTS = ("useful", "not_useful")
ALL_VERDICTS = (*REVIEW_VERDICTS, *USEFULNESS_VERDICTS)


def is_review_field(name: str) -> bool:
    """Whether `name` is something a verdict may be stored against.

    The same rule as the database's check constraint (0026), in one function so
    the API refuses what the constraint would refuse rather than finding out at
    the write.
    """
    return name in SUGGESTION_FIELDS or name in CARD_FIELDS or name.startswith(FINDING_PREFIX)


#: What the blind check asks, before anything is revealed. Only these two:
#: urgency has no ground truth worth asking for at arrival, and the repeat
#: question needs the other ticket on screen, which would reveal the grouping.
BLIND_FIELDS = ("origin", "issue_type")

#: What a person may answer blind. `unsure` is an answer — a subject line that
#: does not say is a fact about the subject line — and is out of the accuracy
#: denominator exactly as the agent's own `unsure` is.
BLIND_ORIGIN_ANSWERS = ("alert", "human", "unsure")


class TicketSuggestion(Base):
    """One ticket, as the agent read it when it arrived."""

    __tablename__ = "ticket_suggestions"
    __table_args__ = (
        # One suggestion per ticket per pass. A later pass over the same ticket
        # is a new row and the newest is what the page shows, so a changed
        # model or prompt leaves both readings on the record.
        UniqueConstraint("run_id", "ticket_id", name="uq_ticket_suggestions_run_ticket"),
        Index("ix_ticket_suggestions_client", "client_id", "created_at"),
        Index("ix_ticket_suggestions_ticket", "client_id", "ticket_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_runs.id", ondelete="CASCADE"), nullable=False
    )
    prediction_id: Mapped[int | None] = mapped_column(
        ForeignKey("shadow_predictions.id", ondelete="SET NULL"), nullable=True
    )
    enrichment_id: Mapped[int | None] = mapped_column(
        ForeignKey("shadow_enrichments.id", ondelete="SET NULL"), nullable=True
    )
    """The snapshot the "what we found" lines were read from. A pointer, not a
    copy: the facts live on the snapshot and are not duplicated here."""

    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The vendor's id, as every other autopilot table holds it."""
    ticket_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The number a person reads, kept only so a repeat can be named in words
    without a second vendor read."""
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True
    )

    issue_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_type_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    urgency: Mapped[str | None] = mapped_column(Text, nullable=True)
    urgency_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    group_with: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The earlier ticket this one was grouped with, if any."""
    group_with_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    display_threshold: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    """`AUTOPILOT_DISPLAY_THRESHOLD` as it stood when this was made. Below it a
    field is not shown at all: an agent that is not sure stays quiet."""
    blind_check: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Decided once, here, and not re-rolled on every page load: the card would
    otherwise flip between asking and telling when somebody refreshed."""

    created_at: Mapped[dt.datetime] = mapped_column(nullable=False)

    def __repr__(self) -> str:  # ids and labels only
        return f"<TicketSuggestion ticket={self.ticket_id} type={self.issue_type}>"


class SuggestionReview(Base):
    """One engineer's verdict on one field of one suggestion. Appended."""

    __tablename__ = "suggestion_reviews"
    __table_args__ = (
        CheckConstraint(
            "field IN ('" + "', '".join((*SUGGESTION_FIELDS, *CARD_FIELDS)) + "')"
            " OR field LIKE 'finding:%'",
            name="field_is_a_known_one",
        ),
        CheckConstraint(
            "verdict IN ('" + "', '".join(ALL_VERDICTS) + "')",
            name="verdict_is_a_known_one",
        ),
        Index("ix_suggestion_reviews_suggestion", "suggestion_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    suggestion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ticket_suggestions.id", ondelete="CASCADE"), nullable=False
    )
    field: Mapped[str] = mapped_column(Text, nullable=False)
    """A classification field, one of the card's own (`machine`, `next_step`,
    `card`), or `finding:<kind>`."""
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    """`correct` / `changed` / `wrong` for a classification; `useful` /
    `not_useful` for everything else."""
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    """What it should have been, on `changed`. One of the tenant's own labels
    for a type, one of the fixed sets otherwise."""

    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    """RESTRICT like every other actor column: nothing done to an account may
    remove the record of what that person judged."""
    reviewer_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(nullable=False)

    def __repr__(self) -> str:
        return f"<SuggestionReview {self.field}={self.verdict}>"


class BlindCheck(Base):
    """What an engineer said before they were shown anything."""

    __tablename__ = "blind_checks"
    __table_args__ = (
        CheckConstraint(
            "field IN ('" + "', '".join(BLIND_FIELDS) + "')",
            name="field_is_a_known_one",
        ),
        Index("ix_blind_checks_client", "client_id", "created_at"),
        Index("ix_blind_checks_ticket", "client_id", "ticket_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ticket_suggestions.id", ondelete="SET NULL"), nullable=True
    )
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    """`alert`, `human` or `unsure` for origin; one of the tenant's labels or
    `unsure` for a type. Stored as the word, never a picklist number."""

    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewer_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    revealed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """Whether the suggestions were shown afterwards. True in the one flow
    there is; the column exists so a later flow that does not reveal them is
    told apart rather than assumed."""

    def __repr__(self) -> str:
        return f"<BlindCheck {self.field}={self.answer}>"

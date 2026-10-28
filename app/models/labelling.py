"""Where a person's judgement is written down.

Two of the four things Level 0 measures have no ground truth in any table we
hold: where a ticket came from, and whether a group is one thing. The vendor's
own `source` field would answer the first and the ledger does not keep it, so
the only honest answer is that somebody reads some tickets and says. These
tables are where that lands.

**A labelling set is tied to a run**, because a group only exists as a run's
opinion: the 30 groups a person judges are the groups *that run* made, and the
same tickets under a different threshold would be different groups. The origin
sample is tied to a run too, for one reason — so a score names the run it
scored.

**Answers are appended, never edited.** A labeller who changes their mind adds
a row; an undo adds a row saying `withdrawn`. The latest row per labeller is
what scores, and the history is what makes a disputed label checkable. That is
the same rule the audit log follows, for the same reason.

**No ticket text lives here.** An item is a ticket id and a position; the words
a labeller reads come from the ledger at display time, scrubbed, and go no
further. A note a labeller writes is scrubbed before it is stored, because a
person typing a note is as likely to paste a password as a person typing a
ticket.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: What a set asks. `origin` is one ticket at a time; `groups` is one of a
#: run's groups at a time, with every ticket in it.
LABELLING_KINDS = ("origin", "groups")

#: What a labeller may say about a ticket's origin. `unsure` is an answer — a
#: subject line that does not say is a fact about the subject line — and
#: `skipped` and `withdrawn` are the two ways of not answering.
ORIGIN_ANSWERS = ("alert", "human", "unsure", "skipped", "withdrawn")
#: And about a group. `partly` carries the tickets that do not belong.
GROUP_ANSWERS = ("same", "not_same", "partly", "skipped", "withdrawn")
#: Neither answer scores: one is a deferral, the other a correction.
NOT_AN_ANSWER = ("skipped", "withdrawn")


class LabellingSet(Base):
    """One sample of one run, put in front of people."""

    __tablename__ = "labelling_sets"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('" + "', '".join(LABELLING_KINDS) + "')",
            name="kind_is_origin_or_groups",
        ),
        UniqueConstraint("run_id", "kind", name="uq_labelling_sets_run_kind"),
        Index("ix_labelling_sets_client", "client_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_runs.id", ondelete="CASCADE"), nullable=False
    )
    """The run whose sample this is. One set per run per kind: a second sample
    of the same run would be a second opinion of the same question, and the
    agreement between two labellers is the better way to ask that."""

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    """The sampler's seed, so the same set can be rebuilt and two people
    labelling "the sample" are labelling the same tickets."""
    double_check: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """How many of the items are put to a second labeller as well, from the
    front of the set. Agreement between the two is the only measure we have of
    how hard the question is."""

    created_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:  # ids and counts
        return f"<LabellingSet id={self.id} kind={self.kind!r} size={self.size} run={self.run_id}>"


class LabellingItem(Base):
    """One thing to judge: a ticket, or a group named by its earliest ticket."""

    __tablename__ = "labelling_items"
    __table_args__ = (
        UniqueConstraint("set_id", "subject_id", name="uq_labelling_items_subject"),
        Index("ix_labelling_items_set", "set_id", "position"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("labelling_sets.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    """Where it sits in the set, so "the first twenty" means the same thing to
    everybody and a labeller can be sent back to where they stopped."""

    subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The ticket's own id for an origin item; for a group item, the id of the
    ticket that opened the group. The members are the run's own opinion and are
    read back from `shadow_predictions`, never copied here."""

    double_check: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<LabellingItem set={self.set_id} position={self.position}>"


class LabellingAnswer(Base):
    """What one person said about one item, once.

    Appended, never updated. The latest row per labeller per item is what
    scores; the rest is how a label that somebody disputes can be looked into.
    """

    __tablename__ = "labelling_answers"
    __table_args__ = (
        CheckConstraint(
            "answer IN ('" + "', '".join(sorted(set(ORIGIN_ANSWERS) | set(GROUP_ANSWERS))) + "')",
            name="answer_is_a_known_one",
        ),
        Index("ix_labelling_answers_item", "item_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("labelling_items.id", ondelete="CASCADE"), nullable=False
    )
    labeller_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    """Who said it. RESTRICT, like every other actor column: nothing done to an
    account may remove the record of what that person judged. Null only for an
    import of a sheet filled in before the pages existed, which says so in
    `source`."""
    labeller_ref: Mapped[str] = mapped_column(Text, nullable=False)
    """The actor as text, for a row whose account is gone or was never one."""

    answer: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    """One line from the labeller, scrubbed on the way in."""
    excluded: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    """For `partly`: the ids of the tickets in the group that do not belong."""

    source: Mapped[str] = mapped_column(Text, nullable=False, default="page")
    """`page` or `sheet`: whether a person pressed a key or a spreadsheet was
    imported. A score that mixes the two should be able to say so."""

    created_at: Mapped[dt.datetime] = mapped_column(nullable=False)

    @property
    def counts(self) -> bool:
        """Is this an answer, or one of the two ways of not answering?"""
        return self.answer not in NOT_AN_ANSWER

    def as_json(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "note": self.note,
            "excluded": list(self.excluded or []),
            "labeller": self.labeller_ref,
            "source": self.source,
            "at": self.created_at.isoformat(),
        }

    def __repr__(self) -> str:  # never the note: a person's words
        return f"<LabellingAnswer item={self.item_id} answer={self.answer!r}>"

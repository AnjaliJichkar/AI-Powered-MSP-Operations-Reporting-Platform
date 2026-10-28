"""What the agent would have said, and what actually happened.

Level 0 of Autopilot measures the agent against the record before it is
allowed to write anything: a run replays tickets in creation order, predicts
the few things a triage decision rests on, and stores the prediction beside the
id of the ticket it was about. Scoring joins that to `ticket_outcomes`, which
holds what the ticket turned out to be.

**A prediction is a measurement, not a document.** Two rules follow:

* **No ticket text, ever.** Not the title, not an excerpt, not a "reason" that
  quotes one. A row is ids, labels from a fixed set, numbers and timing. The
  reason a prediction can be reviewed at all is that the ticket id is on it and
  the ledger holds the rest; copying the text here would put a second copy of
  customer wording in a table nobody remembers to scrub.
* **A run is reproducible or it is not a comparison.** `shadow_runs` carries
  the commit, the model, the prompt version and the config, so two runs whose
  numbers differ can be told apart by what changed between them.
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
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin

#: What a run was doing. `replay` reads stored ledger rows and calls no vendor;
#: `forward` is the live shadow on newly created tickets (Part B).
SHADOW_MODES = ("replay", "forward")
#: How a prediction was arrived at. A rule is deterministic and free; the model
#: is neither, and the two are scored apart so the cheap path's worth is known.
PREDICTION_PATHS = ("rule", "model", "error")
#: Where a ticket came from. `unsure` is a real answer and is never scored as a
#: wrong guess: an agent that says it does not know is the point of Level 0.
ORIGINS = ("alert", "human", "unsure")
URGENCIES = ("low", "normal", "high")


class ShadowRun(Base, TimestampMixin):
    """One replay or forward pass, with everything needed to compare it."""

    __tablename__ = "shadow_runs"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('" + "', '".join(SHADOW_MODES) + "')",
            name="mode_is_replay_or_forward",
        ),
        Index("ix_shadow_runs_client", "client_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    """CASCADE: a shadow measurement is derived and can be run again."""

    mode: Mapped[str] = mapped_column(Text, nullable=False)

    # --- what it ran as, so two runs are comparable -------------------------
    code_commit: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The git commit the runner was at, or null outside a checkout."""
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The knobs that change an answer: the window, the input variant, the
    grouping window and threshold, the caps. Never a credential."""

    # --- what it did --------------------------------------------------------
    started_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    tickets_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    by_rule: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    by_model: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Predictions the model returned that did not validate. Recorded, never
    repaired: a guess dressed as an answer is the failure this phase exists to
    catch."""
    groups: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    reused: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Answers taken from an earlier run rather than asked for again, because
    this run would have sent an identical request. Counted beside `by_model`,
    which stays what it was: how many tickets the model path answered, whoever
    did the asking."""

    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    """Input served from the prompt cache, **not** included in `input_tokens`.
    Counted apart because it is billed at a fraction of the input rate: a
    replay of thousands of tickets reads the same instructions every time, and
    adding the two together would overstate the cost several-fold."""
    cost: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    """Priced from the per-million-token settings. Null when nobody has set
    them, which is reported as unpriced rather than as free."""

    stopped_early: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stopped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """`token_cap`, `cost_cap`, `limit`, or an error's type. Null when it ran
    to the end of the window."""

    def __repr__(self) -> str:  # ids and counts: never a customer
        return (
            f"<ShadowRun id={self.id} mode={self.mode!r} tickets={self.tickets_seen} "
            f"rule={self.by_rule} model={self.by_model} errors={self.errors}>"
        )


class ShadowPrediction(Base):
    """What the agent would have said about one ticket.

    Keyed `(run_id, ticket_id)`: a run says one thing per ticket, and a rerun
    is a new run rather than an edit, so a prediction is never rewritten.
    """

    __tablename__ = "shadow_predictions"
    __table_args__ = (
        UniqueConstraint("run_id", "ticket_id", name="uq_shadow_predictions_ticket"),
        CheckConstraint(
            "path IN ('" + "', '".join(PREDICTION_PATHS) + "')",
            name="path_is_rule_model_or_error",
        ),
        CheckConstraint(
            "origin IS NULL OR origin IN ('" + "', '".join(ORIGINS) + "')",
            name="origin_is_alert_human_or_unsure",
        ),
        CheckConstraint(
            "urgency IS NULL OR urgency IN ('" + "', '".join(URGENCIES) + "')",
            name="urgency_is_low_normal_or_high",
        ),
        Index("ix_shadow_predictions_run", "run_id"),
        Index("ix_shadow_predictions_reuse", "ticket_id", "input_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_runs.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    """Autotask's own id, as text, as the ledger holds it. The join to what
    actually happened, and the only way back to the ticket's words."""

    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True
    )
    created_at_vendor: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """Kept so a run can be read in creation order without rejoining the
    ledger, and so grouping can be checked for looking only backwards."""

    path: Mapped[str] = mapped_column(Text, nullable=False)

    # --- the prediction -----------------------------------------------------
    origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    issue_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    """One of the tenant's own issue-type labels, or `unsure`, **after** the
    franchisee's label policy. A label the tenant does not have is an error,
    not a prediction."""
    issue_type_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    """What the model actually said, before the policy. Kept so the policy's
    effect is a measurement — accuracy before and after, on the same run —
    rather than something taken on trust."""
    issue_type_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    """`kept`, `remapped` or `forbidden`: what the franchisee's label policy did
    to the raw answer."""
    issue_type_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    urgency: Mapped[str | None] = mapped_column(Text, nullable=True)
    urgency_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    group_with: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The earlier ticket's id this one would have been grouped with. Null for
    a ticket that opens its own group."""
    group_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The rule that grouped it, named — never the words it matched on."""
    group_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    # --- what it cost -------------------------------------------------------
    rule_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Which rule answered, for a `rule` path. Null on the model path."""
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Not part of `input_tokens`: cached input is billed at a fraction."""
    cost: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)

    input_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The digest of exactly what was sent for this ticket: the model, the
    instructions, the tenant's label set, the tool schema and the ticket's own
    line. A later run that would send an identical request reads this answer
    instead of asking for it again. Null on rows written before anybody was
    recording the question, which are never reused for that reason."""
    reused_from_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shadow_runs.id", ondelete="SET NULL"), nullable=True
    )
    """Where the answer came from, when it was not asked for here. On the row
    rather than in somebody's memory: a run of reused answers is a real
    measurement of today's policy over yesterday's model calls, and anybody
    reading it should be able to see that is what it is."""

    error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why a prediction did not validate, in our words. Never the model's
    output and never the ticket's text: both could carry a customer's wording."""

    title_quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """The stored title lost a word to the scrubber's old over-redaction, so
    this row is predicted on but left out of every accuracy figure."""

    queue_label_at_arrival: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The queue the ticket was raised into, read when it arrived. The forward
    shadow can record this honestly; a replay cannot, because the ledger reads
    the queue at completion and by then it says where the ticket was *sent*.
    Null on a replay row for that reason, and that is the difference between
    the two populations rather than a gap in one."""
    has_description: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Whether the classifier was given a body as well as a subject. The
    forward shadow has one and a replay never does (the ledger does not store
    it), so comparing the two populations without this would be comparing the
    agent to itself with twice the evidence."""

    def __repr__(self) -> str:  # ids and labels only
        return (
            f"<ShadowPrediction run={self.run_id} ticket={self.ticket_id!r} "
            f"path={self.path!r} origin={self.origin!r} issue_type={self.issue_type!r}>"
        )


class ShadowEnrichment(Base):
    """What the tools said about one new ticket's machine, as counts and states.

    The forward shadow (Part B) runs the same classifier as the replay and then
    the ticket workspace's own enrichment — not a second one — and keeps the
    answer here so a fortnight of shadow running can be reviewed without
    reading a vendor again.

    **A snapshot is evidence about a machine, not a copy of it.** Each fact is
    kept as its id, the tool that produced it, and its value only where that
    value is a number, a flag or a time; anything else is recorded as having
    been text and its content dropped (`facts`). A hostname is a device name,
    and a device name is not something this table needs to hold.

    `device_method` and `device_confidence` are the workspace's own — how sure
    we are which machine the ticket is about — because an enrichment of the
    wrong machine is the failure the Level 1 gate counts, and it cannot be
    counted later if the basis was not written down at the time.
    """

    __tablename__ = "shadow_enrichments"
    __table_args__ = (
        UniqueConstraint("run_id", "ticket_id", name="uq_shadow_enrichments_ticket"),
        Index("ix_shadow_enrichments_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_runs.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)

    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True
    )
    """The site the *ticket* belongs to. Compared against the device's site to
    count wrong-site enrichments, which is what the gate asks about."""

    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    device_site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True
    )
    """The site the device belongs to. Equal to `site_id` or the enrichment was
    of a machine at another customer, which is never acceptable."""
    device_method: Mapped[str] = mapped_column(Text, nullable=False)
    """How the machine was found: `hostname`, `netbios`, `vendor_name`,
    `contact`, `engineer`, or `none`."""
    device_confidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """How many machines it could have been. More than one and nobody chose."""

    read_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    """When the tools were asked. A snapshot is as-of, like every device fact."""

    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    """`[{id, source, value}]`, value present only when it is a number, a flag
    or a time."""
    fact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    """`[{source, error_type}]` — which tool failed and with what kind of error,
    never the message, which could quote a record."""
    not_assessed: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """connector -> why it was not asked: no link, no credential, or not built.
    A tool that was not asked is a gap in the snapshot and says so, rather than
    looking like a tool that found nothing."""

    def __repr__(self) -> str:  # ids and counts
        return (
            f"<ShadowEnrichment run={self.run_id} ticket={self.ticket_id!r} "
            f"device={self.device_id} method={self.device_method!r} facts={self.fact_count}>"
        )


class ShadowBatch(Base):
    """One set of a run's requests, sent to the Message Batches API.

    A replay is thousands of independent requests nobody is waiting on, which
    is what that API is for: about half the price, answered within a day. The
    row exists so the wait is survivable — a batch id that lived only in a
    terminal somebody has closed is money spent with nothing to show for it, so
    it is committed at submission and the run is carried on by running the same
    command again.
    """

    __tablename__ = "shadow_batches"
    __table_args__ = (
        UniqueConstraint("external_id", name="uq_shadow_batches_external"),
        Index("ix_shadow_batches_run", "run_id", "submitted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_runs.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The vendor's batch id. What a person needs to ask about it, and what
    makes a second submission of the same work findable rather than silent."""
    status: Mapped[str] = mapped_column(Text, nullable=False)
    """The vendor's word for where it is, plus `collected`, which is ours: the
    batch has ended and its answers are written down."""
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    submitted_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    collected_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # ids and counts
        return (
            f"<ShadowBatch run={self.run_id} external={self.external_id!r} "
            f"status={self.status!r} requests={self.requests}>"
        )


#: The periods a sampled ticket can be in: the stretch where a threshold or a
#: policy may be chosen, and the last thirty days, which are scored and
#: otherwise left alone. The same split `scoring.split` makes, drawn once and
#: written down so a sample cannot drift under a run.
SAMPLE_PERIODS = ("tuning", "test")


class ShadowSample(Base):
    """A fixed set of tickets every iteration run uses.

    Iterating on a prompt by replaying 2,722 tickets costs five million tokens a
    go, and most of that buys nothing: the question "did this change help" is
    answered as well by a few hundred tickets drawn properly as by all of them.
    So the sample is drawn **once**, stratified by issue type across both
    periods, and stored — not recomputed per run from a seed, because a ledger
    that gains a row would quietly change which tickets a seed selects and two
    runs would stop being comparable without anybody touching anything.

    It is not a measurement. A number that goes in front of anybody still comes
    from a full replay on the holdout; this is the thing you iterate against on
    the way there, and every run says which sample it used.
    """

    __tablename__ = "shadow_samples"
    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_shadow_samples_name"),
        Index("ix_shadow_samples_client", "client_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    """`iteration` today. A name rather than a kind: a second sample for a
    second purpose is a row, not a migration."""

    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    """How many were asked for. What was drawn is the row count, which can be
    smaller where a period has fewer tickets than its share."""
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    holdout_days: Mapped[int] = mapped_column(Integer, nullable=False)
    """The window it was drawn from and where the split fell, both measured
    back from the newest ticket, exactly as scoring measures them."""
    drawn_from: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """How many tickets the window held when it was drawn. The denominator of
    "a sixth of the ledger", and a hint that the window has moved on."""

    created_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:  # ids and counts
        return (
            f"<ShadowSample id={self.id} name={self.name!r} size={self.size} "
            f"days={self.days} seed={self.seed}>"
        )


class ShadowSampleTicket(Base):
    """One ticket in a sample, and which period it was drawn from."""

    __tablename__ = "shadow_sample_tickets"
    __table_args__ = (
        UniqueConstraint("sample_id", "ticket_id", name="uq_shadow_sample_tickets_ticket"),
        CheckConstraint(
            "period IN ('" + "', '".join(SAMPLE_PERIODS) + "')",
            name="period_is_tuning_or_test",
        ),
        Index("ix_shadow_sample_tickets_sample", "sample_id", "period"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sample_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_samples.id", ondelete="CASCADE"), nullable=False
    )
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The ledger's id. No title and no issue type: the ledger holds both, and a
    sample is a list of which tickets, not a copy of them."""
    period: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<ShadowSampleTicket sample={self.sample_id} ticket={self.ticket_id!r}>"

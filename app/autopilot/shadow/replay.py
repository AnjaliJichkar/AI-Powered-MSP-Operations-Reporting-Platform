"""Replaying the ledger: what the agent would have said, ticket by ticket.

Reads `ticket_outcomes` and calls no vendor at all. That is not a detail of
this implementation, it is the point of it — the whole phase runs on rows we
already hold, so measuring the agent costs the franchisee's Autotask tenant
nothing and cannot be confused with doing something to it.

**What the agent is shown, and nothing else** (`_inputs`): the scrubbed
subject, the customer, and when the ticket arrived. Withheld: the final issue
type, priority, status, assignee, resolver, every duration and every outcome.
Those are the answers. The one input with a question mark over it is the
queue — the ledger reads it at *completion*, and `Escalation` and `Triage` are
places a ticket is sent rather than raised, so including it would let what
happened leak into what was known. It is off in the default variant and
available as `with_alert_queue` so its worth can be measured rather than
assumed (see `InputVariant`).

**Order matters in one direction.** Tickets are walked in creation order and
grouping may only look back, so a run cannot group a ticket with one that had
not happened yet. The grouping pass is therefore sequential; the classifying
pass is not, because each ticket's classification is independent of every
other's.

**The issue-type set comes from the ledger's own distinct labels.** In the
forward shadow it comes from the tenant's picklist, which is where it belongs;
during the Autotask cooling period the observed labels are the only source
available. Knowing *which labels exist* is tenant configuration and tells the
model nothing about which ticket is which — unlike the per-ticket label, which
is withheld.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.autopilot.config import AutopilotConfig
from app.autopilot.shadow import batching
from app.autopilot.shadow.classify import (
    Classification,
    fingerprint,
    request_params,
    send,
)
from app.autopilot.shadow.cost import Budget
from app.autopilot.shadow.grouping import Grouper
from app.autopilot.shadow.prompts import PROMPT_VERSION
from app.autopilot.shadow.rules import RuleAnswer, apply_rules, rules_for
from app.autopilot.shadow.sampling import ITERATION, sample_for, ticket_ids
from app.autopilot.shadow.schema import UNSURE
from app.models.enums import AuditEventType, AuditOutcome
from app.models.outcome import TicketOutcome
from app.models.shadow import ShadowPrediction, ShadowRun
from app.settings import Settings
from app.tickets.scrub import scrub

#: The two input sets a run can be scored under. The default is the defensible
#: one; the variant exists so the queue's worth is a measurement.
InputVariant = Literal["title_site_time", "with_alert_queue"]

#: Called after each committed batch with (done, total, tokens). The CLI
#: prints it; a test counts the calls.
ProgressReport = Callable[[int, int, int], None]

#: Queue labels that read as where a ticket was *raised* rather than where it
#: was sent. Used only by the `with_alert_queue` variant, and only as a
#: boolean: the label itself never reaches the model.
ALERT_QUEUES = frozenset({"alerts", "monitoring alert", "system maintenance"})

#: Errors that will not fix themselves part-way through a run: the credential
#: is wrong, the account cannot be billed, the request is not acceptable. Live,
#: 2026-10-01: an account ran out of credit mid-replay and the run recorded
#: 1,937 identical 400s before finishing, each one a call nobody could have
#: expected to succeed. A few in a row now stop the run instead.
FATAL_MODEL_ERRORS = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "BadRequestError",
        "NotFoundError",
    }
)
#: How many in a row before a run gives up. More than one, because a single
#: ticket can be refused on its own merits — a subject that is somehow
#: unacceptable — and that should not end a replay of thousands.
FATAL_RUN_LENGTH = 3

#: A stored title whose word after a secret keyword was taken by the scrubber's
#: old over-redaction. Predicted on, and left out of every accuracy figure.
_DAMAGED_TITLE = re.compile(
    r"\b(?:pass(?:word|phrase|wd|code)|pwd|api[ _-]?key|access[ _-]?key|client[ _-]?secret|"
    r"licen[cs]e[ _-]?key|activation[ _-]?key|recovery[ _-]?key|bitlocker(?: recovery)?"
    r"(?: key)?)\s*(?:is|was|-)?\s+\[redacted\]",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """Everything that changes an answer, and therefore everything stored on
    the run so two runs can be told apart."""

    days: int = 90
    limit: int | None = None
    variant: InputVariant = "title_site_time"
    sample: str | None = ITERATION
    """The stored sample to replay, or `None` for the whole window.

    The default is the sample and the whole window is the explicit act, because
    the cost of the two differs by a factor of twenty and the one people reach
    for a dozen times a day should be the cheap one. The run records which it
    was, so a number from a sample is never read as a number from the ledger."""
    group_window_days: int = 30
    group_similarity: float = 0.7
    batch: int = 100
    """Predictions committed at a time. A run that holds one transaction to the
    end cannot be watched from another connection and loses everything if it is
    interrupted."""

    def as_json(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "limit": self.limit,
            "variant": self.variant,
            "sample": self.sample,
            "group_window_days": self.group_window_days,
            "group_similarity": self.group_similarity,
            "batch": self.batch,
        }


@dataclass
class ReplayResult:
    """Counts only, as everything in this phase is."""

    run_id: uuid.UUID
    tickets_seen: int = 0
    by_rule: int = 0
    by_model: int = 0
    reused: int = 0
    """Of `by_model`, how many were read from an earlier run rather than asked
    for again. `by_model - reused` is what this run actually spent."""
    errors: int = 0
    groups: int = 0
    quarantined: int = 0
    window: int = 0
    """How many tickets the window held. The denominator a sample is a part
    of, and the whole of a full replay."""
    sampled: int | None = None
    """How many of the window's tickets the sample asked about. `None` on a
    full replay, which is the difference between "600 tickets" and "600 of
    2,722"."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float | None = None
    duration_ms: int = 0
    stopped_early: bool = False
    stopped_reason: str | None = None
    rule_names: dict[str, int] = field(default_factory=dict)
    resumed_from: int = 0
    """Tickets this run already had a prediction for when it started again."""
    pending_batch: str | None = None
    """The batch this run is waiting on, when it is waiting on one. A run with
    a pending batch is not finished and not failed: its answers arrive within
    hours and the same command collects them."""
    pending_requests: int = 0
    collected: int = 0
    """Answers written down from a batch that had ended by the time somebody
    ran the command again."""


@dataclass(frozen=True, slots=True)
class _Ticket:
    """One ledger row reduced to what a replay may see, plus its ids."""

    ticket_id: str
    site_id: uuid.UUID | None
    site_handle: str
    created_at: dt.datetime | None
    title: str
    quarantined: bool
    alert_queue: bool


def code_commit() -> str | None:
    """The commit the runner is at, so a run is reproducible. `None` outside a
    checkout, which is a fact about the run rather than an error."""
    try:
        found = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout.strip() or None if found.returncode == 0 else None


def _inputs(row: TicketOutcome) -> _Ticket:
    """One row as the agent may see it: the scrubbed subject, the customer and
    the arrival time. Everything else on the row is an answer or an outcome.

    The title is passed through the *current* scrubber on the way out, as every
    other path does. For stored text that is all but a no-op — it cannot put
    back a word the old rule took — which is exactly why the rows it took one
    from are marked and excluded from accuracy.
    """
    stored = row.title or ""
    return _Ticket(
        ticket_id=row.ticket_id,
        site_id=row.site_id,
        # A short, stable handle rather than the customer's name: the model
        # needs to tell one customer from another, not know who they are.
        site_handle=f"site-{str(row.site_id)[:8]}" if row.site_id else "site-unknown",
        created_at=row.created_at_vendor,
        title=scrub(stored).text,
        quarantined=bool(_DAMAGED_TITLE.search(stored)),
        alert_queue=(row.queue_label or "").strip().lower() in ALERT_QUEUES,
    )


async def _load(
    session: AsyncSession, client_id: uuid.UUID, *, since: dt.datetime, limit: int | None
) -> list[_Ticket]:
    """The window's tickets, in creation order, as inputs.

    Ordered by creation and not by completion: the replay is of triage, and
    triage happens when a ticket arrives. A row with no creation time cannot be
    placed in that order and is left out rather than guessed at.
    """
    rows = (
        (
            await session.execute(
                select(TicketOutcome)
                .where(
                    TicketOutcome.client_id == client_id,
                    TicketOutcome.completed_at >= since,
                    TicketOutcome.created_at_vendor.is_not(None),
                )
                .order_by(TicketOutcome.created_at_vendor)
            )
        )
        .scalars()
        .all()
    )
    tickets = [_inputs(row) for row in rows]
    return tickets[:limit] if limit is not None else tickets


async def issue_types_for(session: AsyncSession, client_id: uuid.UUID) -> list[str]:
    """The tenant's own issue-type labels, as the ledger has observed them."""
    labels = (
        (
            await session.execute(
                select(TicketOutcome.issue_type_label)
                .where(
                    TicketOutcome.client_id == client_id,
                    TicketOutcome.issue_type_label.is_not(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    return sorted(label for label in labels if label)


def _row(
    run_id: uuid.UUID,
    ticket: _Ticket,
    *,
    path: str,
    grouped: Any,
    policy: AutopilotConfig,
    answer: RuleAnswer | None = None,
    prediction: Any = None,
    rule_name: str | None = None,
    latency_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cost: float | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    input_hash: str | None = None,
    reused_from_run_id: uuid.UUID | None = None,
) -> ShadowPrediction:
    """One stored prediction. Labels, ids, numbers — no title, ever.

    The franchisee's label policy is applied **here**, after the answer, and
    both the raw label and the policy's verdict are stored: that is what makes
    accuracy before and after the policy two numbers from one run rather than
    two runs.
    """
    source = answer or prediction
    raw = getattr(source, "issue_type", None)
    final, verdict = policy.apply(raw)
    return ShadowPrediction(
        run_id=run_id,
        ticket_id=ticket.ticket_id,
        site_id=ticket.site_id,
        created_at_vendor=ticket.created_at,
        path=path,
        origin=getattr(source, "origin", None),
        origin_confidence=getattr(source, "origin_confidence", None),
        issue_type=final,
        issue_type_raw=raw,
        issue_type_policy=verdict if raw is not None else None,
        issue_type_confidence=getattr(source, "issue_type_confidence", None),
        urgency=getattr(source, "urgency", None),
        urgency_confidence=getattr(source, "urgency_confidence", None),
        group_with=grouped.ticket_id if grouped else None,
        group_basis=grouped.basis if grouped else None,
        group_score=grouped.score if grouped else None,
        rule_name=rule_name,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cost=cost,
        error_type=error_type,
        error_message=error_message,
        input_hash=input_hash,
        reused_from_run_id=reused_from_run_id,
        title_quarantined=ticket.quarantined,
    )


async def reusable(
    session: AsyncSession,
    client_id: uuid.UUID,
    wanted: dict[str, str],
    *,
    model: str,
    prompt_version: str,
) -> dict[str, ShadowPrediction]:
    """The answers this client already holds to exactly these questions.

    `wanted` is ticket id to the digest of the request this run would send.
    A row qualifies only if every one of four things matches — the ticket, the
    digest, the model and the prompt version — and only if the model actually
    answered: an error is a thing to try again, not a thing to inherit.

    The newest matching row wins, which matters when the same question has been
    asked under two prompts that happen to share a version: the later answer is
    the one somebody would have been looking at.
    """
    if not wanted:
        return {}
    rows = (
        (
            await session.execute(
                select(ShadowPrediction)
                .join(ShadowRun, ShadowRun.id == ShadowPrediction.run_id)
                .where(
                    ShadowRun.client_id == client_id,
                    ShadowRun.model == model,
                    ShadowRun.prompt_version == prompt_version,
                    ShadowPrediction.path == "model",
                    ShadowPrediction.ticket_id.in_(wanted),
                    ShadowPrediction.input_hash.in_(set(wanted.values())),
                )
                .order_by(ShadowPrediction.id)
            )
        )
        .scalars()
        .all()
    )
    found: dict[str, ShadowPrediction] = {}
    for row in rows:
        if wanted.get(row.ticket_id) == row.input_hash:
            found[row.ticket_id] = row
    return found


async def already_predicted(session: AsyncSession, run_id: uuid.UUID) -> set[str]:
    """The tickets this run has already answered.

    What makes a run resumable: a replay that was interrupted has rows
    committed up to its last batch, and the ids of those rows are the work not
    to do again.
    """
    rows = (
        await session.execute(
            select(ShadowPrediction.ticket_id).where(ShadowPrediction.run_id == run_id)
        )
    ).scalars()
    return set(rows)


async def replay(
    session: AsyncSession,
    context: AuditContext,
    client_id: uuid.UUID,
    *,
    model_client: Any,
    settings: Settings,
    config: ReplayConfig | None = None,
    policy: AutopilotConfig | None = None,
    resume: uuid.UUID | None = None,
    model: str | None = None,
    reuse: bool = True,
    batched: bool | None = None,
    on_progress: ProgressReport | None = None,
    now: dt.datetime | None = None,
) -> ReplayResult:
    """Replay one client's window and store what the agent would have said.

    Commits every `config.batch` tickets and reports progress as it goes, so a
    long run can be watched from another connection and an interrupted one
    resumed (`resume=<run id>`) rather than started over. The first full replay
    held one transaction for nine minutes, which had neither property.

    `policy` is the franchisee's label settings, applied after each answer and
    copied onto the run's config: a run whose policy is unknown cannot be
    compared with one whose is.

    `model` overrides `AUTOPILOT_MODEL` for this run and is stored on it, so
    "the same sample on a cheaper model" is one flag and two comparable rows.

    `batched` sends what is left through the Message Batches API — about half
    the price, answered within hours — and returns with the run unfinished and
    its batch id on a row. Running the same command again collects it. Off
    unless `AUTOPILOT_BATCH_REPLAY` says otherwise, and never on a live path.

    `reuse` reads an answer this client already holds whenever the request this
    run would send is identical — same ticket, same digest, same model, same
    prompt version. That is what makes rescoring under a new policy or a moved
    threshold cost nothing: both are applied after the model has answered, so
    neither is a reason to ask again. `reuse=False` is for proving the two
    paths agree.
    """
    config = config or ReplayConfig(
        group_window_days=settings.autopilot_group_window_days,
        group_similarity=settings.autopilot_group_similarity,
    )
    policy = policy or AutopilotConfig()
    now = now or dt.datetime.now(dt.UTC)
    # A per-run choice, defaulting to the setting: comparing two models is two
    # runs over the same sample, and neither should need an environment edit.
    model = model or settings.autopilot_model or settings.anthropic_model
    batched = settings.autopilot_batch_replay if batched is None else batched
    budget = Budget.from_settings(settings)
    started = time.monotonic()

    tickets = await _load(
        session, client_id, since=now - dt.timedelta(days=config.days), limit=config.limit
    )
    issue_types = await issue_types_for(session, client_id)
    rules = rules_for(issue_types)

    # The sample is drawn on the first ask and read every time after. Drawn
    # before the run row is written so a run that cannot have one does not
    # leave a row behind saying it tried.
    sampled: set[str] | None = None
    sample_id: uuid.UUID | None = None
    if config.sample is not None:
        drawn = await sample_for(session, client_id, name=config.sample, days=config.days, now=now)
        sample_id = drawn.sample_id
        sampled = await ticket_ids(session, drawn.sample_id)

    # --- the run: a new one, or the one being carried on --------------------
    done: set[str] = set()
    if resume is not None:
        run = await session.get(ShadowRun, resume)
        if run is None or run.client_id != client_id:
            raise ValueError(f"no shadow run {resume} for this client to resume")
        done = await already_predicted(session, resume)
        # The model and the prompt are the run's, not today's: resuming a run
        # under a different model would make one row set two measurements. Its
        # sample is the run's as well, for the same reason — a resumed run that
        # took today's default would answer tickets the run was never about.
        model = run.model
        stored_sample = run.config.get("sample_id")
        sample_id = uuid.UUID(stored_sample) if stored_sample else None
        sampled = await ticket_ids(session, sample_id) if sample_id else None
    else:
        run = ShadowRun(
            id=uuid.uuid4(),
            client_id=client_id,
            mode="replay",
            code_commit=code_commit(),
            model=model,
            prompt_version=PROMPT_VERSION,
            config={
                **config.as_json(),
                "issue_types": len(issue_types),
                "label_policy": policy.as_json(),
                "sample_id": str(sample_id) if sample_id else None,
            },
            started_at=now,
        )
        session.add(run)
        await session.commit()
        await record_event(
            session,
            context,
            event_type=AuditEventType.AUTOPILOT_RUN_STARTED,
            outcome=AuditOutcome.IN_PROGRESS,
            model=model,
            prompt_version=PROMPT_VERSION,
            payload={
                "run_id": str(run.id),
                "mode": "replay",
                "tickets": len(tickets),
                "sample_id": str(sample_id) if sample_id else None,
                **config.as_json(),
                "label_policy": policy.as_json(),
            },
        )
        await session.commit()

    result = ReplayResult(run_id=run.id, resumed_from=len(done))

    # --- grouping, sequential, backwards only, over every ticket -------------
    # Every ticket, including the ones a resumed run will skip: grouping is a
    # walk in creation order, and skipping the walk's early steps would give a
    # resumed run different groups from an uninterrupted one.
    grouper = Grouper(
        window=dt.timedelta(days=config.group_window_days), similarity=config.group_similarity
    )
    grouped_with: dict[str, Any] = {}
    for ticket in tickets:
        if ticket.created_at is None:
            continue
        grouped_with[ticket.ticket_id] = grouper.consider(
            ticket_id=ticket.ticket_id,
            site_id=ticket.site_id,
            created_at=ticket.created_at,
            title=ticket.title,
        )

    # Grouping walked every ticket above, because a group is a walk in creation
    # order and a sample's gaps would change it. Only the classifying is
    # narrowed: that is where the money is.
    asking = [ticket for ticket in tickets if sampled is None or ticket.ticket_id in sampled]
    result.window = len(tickets)
    result.sampled = len(asking) if sampled is not None else None
    outstanding = [ticket for ticket in asking if ticket.ticket_id not in done]
    semaphore = asyncio.Semaphore(settings.autopilot_concurrency)

    def _params(ticket: _Ticket) -> dict[str, Any]:
        """What this run would send for one ticket.

        The variant changes the customer line and only for a ticket raised into
        an alert-shaped queue, so the digest of every other ticket is the same
        under both variants — which is why running the variant costs model
        calls for the alert tickets and nothing for the rest.
        """
        return request_params(
            model=model,
            title=ticket.title,
            site=(
                f"{ticket.site_handle} (raised into an alert queue)"
                if config.variant == "with_alert_queue" and ticket.alert_queue
                else ticket.site_handle
            ),
            created_at=ticket.created_at,
            issue_types=issue_types,
        )

    async def one(ticket: _Ticket) -> tuple[_Ticket, Any]:
        async with semaphore:
            return ticket, await send(model_client, _params(ticket), issue_types=issue_types)

    async def save() -> None:
        """One batch on the record, and the run's counters with it."""
        run.tickets_seen = result.tickets_seen + result.resumed_from
        run.by_rule = result.by_rule
        run.by_model = result.by_model
        run.reused = result.reused
        run.errors = result.errors
        run.input_tokens = result.input_tokens
        run.output_tokens = result.output_tokens
        run.cache_read_tokens = result.cache_read_tokens
        await session.commit()

    def rules_pass(batch: list[_Ticket]) -> list[_Ticket]:
        """Write what the rules answer, and hand back what they declined.

        Rules first and free; only what they decline costs a model call. Each
        ticket is put to the rules once and the answer kept, because asking
        twice is how the cheap path stops being cheap.
        """
        declined: list[_Ticket] = []
        for ticket in batch:
            if ticket.quarantined:
                result.quarantined += 1
            answer = apply_rules(ticket.title, rules)
            if answer is None:
                declined.append(ticket)
                continue
            result.tickets_seen += 1
            result.by_rule += 1
            result.rule_names[answer.rule_name] = result.rule_names.get(answer.rule_name, 0) + 1
            session.add(
                _row(
                    run.id,
                    ticket,
                    path="rule",
                    grouped=grouped_with.get(ticket.ticket_id),
                    policy=policy,
                    answer=answer,
                    rule_name=answer.rule_name,
                    latency_ms=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cost=0.0 if budget.priced else None,
                )
            )
        return declined

    async def reuse_pass(asked: list[_Ticket]) -> list[_Ticket]:
        """Write the answers this client already holds; hand back the rest.

        A policy change, a redrawn sample or a second variant sends the model
        nothing it has already been asked.
        """
        if not reuse or not asked:
            return asked
        known = await reusable(
            session,
            client_id,
            {ticket.ticket_id: fingerprint(_params(ticket)) for ticket in asked},
            model=model,
            prompt_version=PROMPT_VERSION,
        )
        for ticket in asked:
            stored = known.get(ticket.ticket_id)
            if stored is None:
                continue
            result.tickets_seen += 1
            result.by_model += 1
            result.reused += 1
            session.add(
                _row(
                    run.id,
                    ticket,
                    path="model",
                    grouped=grouped_with.get(ticket.ticket_id),
                    # Today's policy over yesterday's answer: the raw label is
                    # what the model said and the policy is applied here, which
                    # is what makes rescoring under a new policy free.
                    policy=policy,
                    prediction=SimpleNamespace(
                        origin=stored.origin,
                        origin_confidence=stored.origin_confidence,
                        issue_type=stored.issue_type_raw,
                        issue_type_confidence=stored.issue_type_confidence,
                        urgency=stored.urgency,
                        urgency_confidence=stored.urgency_confidence,
                    ),
                    latency_ms=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cost=0.0 if budget.priced else None,
                    input_hash=stored.input_hash,
                    reused_from_run_id=stored.run_id,
                )
            )
        return [ticket for ticket in asked if ticket.ticket_id not in known]

    def write_answer(ticket: _Ticket, found: Classification) -> bool:
        """One model answer on the record. True when it was an answer.

        The same row whether it came back in milliseconds from a request or
        overnight from a batch: a batch is a cheaper way to ask, not a
        different measurement.
        """
        result.tickets_seen += 1
        result.input_tokens += found.input_tokens
        result.output_tokens += found.output_tokens
        result.cache_read_tokens += found.cache_read_tokens
        if found.ok:
            result.by_model += 1
        else:
            result.errors += 1
        session.add(
            _row(
                run.id,
                ticket,
                path="model" if found.ok else "error",
                grouped=grouped_with.get(ticket.ticket_id),
                policy=policy,
                prediction=found.prediction,
                latency_ms=found.latency_ms,
                input_tokens=found.input_tokens,
                output_tokens=found.output_tokens,
                cache_read_tokens=found.cache_read_tokens,
                cost=budget.price(
                    input_tokens=found.input_tokens,
                    output_tokens=found.output_tokens,
                    cache_read_tokens=found.cache_read_tokens,
                ),
                error_type=found.error_type,
                error_message=found.error_message,
                input_hash=found.input_hash,
            )
        )
        return found.ok

    # --- a batch that was already sent, collected if it has ended -----------
    waiting = await batching.pending_for(session, run.id) if batched else None
    if waiting is not None:
        waiting = await batching.refresh(session, model_client, waiting)
        if not waiting.ended:
            # Nothing to do but say where it is. The run stays unfinished and
            # the same command collects it later.
            result.pending_batch = waiting.external_id
            result.pending_requests = waiting.requests
            result.duration_ms = int((time.monotonic() - started) * 1000)
            await save()
            return result
        by_id = {ticket.ticket_id: ticket for ticket in outstanding}
        answered = await batching.results(
            model_client,
            waiting,
            issue_types=issue_types,
            digests={ticket.ticket_id: fingerprint(_params(ticket)) for ticket in outstanding},
        )
        for ticket_id, found in answered:
            asked_about = by_id.get(ticket_id)
            if asked_about is None:
                # A result for a ticket this run is no longer asking about —
                # the window moved, or the sample was redrawn. Counted nowhere
                # rather than written against a ticket nobody asked for.
                continue
            write_answer(asked_about, found)
            result.collected += 1
        await save()
        await batching.collected(session, waiting)
        done |= {ticket_id for ticket_id, _ in answered}
        outstanding = [one for one in outstanding if one.ticket_id not in done]

    # --- and the rest: a batch to send, or requests to make -----------------
    if batched:
        left = await reuse_pass(rules_pass(outstanding))
        await save()
        if left:
            waiting = await batching.submit(
                session,
                model_client,
                run.id,
                {ticket.ticket_id: _params(ticket) for ticket in left},
            )
            result.pending_batch = waiting.external_id
            result.pending_requests = waiting.requests
            await record_event(
                session,
                context,
                event_type=AuditEventType.AUTOPILOT_RUN_STARTED,
                outcome=AuditOutcome.IN_PROGRESS,
                model=model,
                prompt_version=PROMPT_VERSION,
                payload={
                    "run_id": str(run.id),
                    "mode": "replay",
                    "batch": waiting.external_id,
                    "requests": waiting.requests,
                },
            )
            result.duration_ms = int((time.monotonic() - started) * 1000)
            await save()
            return result
        outstanding = []

    stop: str | None = None
    consecutive_fatal = 0
    for start in range(0, len(outstanding), config.batch):
        batch = outstanding[start : start + config.batch]
        asked = await reuse_pass(rules_pass(batch))
        for group in range(0, len(asked), settings.autopilot_concurrency):
            slice_of = asked[group : group + settings.autopilot_concurrency]
            for ticket, found in await asyncio.gather(*(one(item) for item in slice_of)):
                if write_answer(ticket, found):
                    consecutive_fatal = 0
                elif found.error_type in FATAL_MODEL_ERRORS:
                    consecutive_fatal += 1
                else:
                    consecutive_fatal = 0

        await save()
        if consecutive_fatal >= FATAL_RUN_LENGTH:
            # The model is not going to start answering: a wrong credential, an
            # account that cannot be billed, a request it will keep refusing.
            # Everything predicted so far is committed and the run says why it
            # is short, which is worth more than two thousand identical errors.
            stop = "model_refused"
            break
        if on_progress is not None:
            on_progress(
                result.tickets_seen + result.resumed_from,
                len(asking),
                result.input_tokens + result.output_tokens + result.cache_read_tokens,
            )
        hit = budget.exceeded(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
        )
        if hit is not None:
            # Stop cleanly, with this batch already committed: everything
            # predicted is on the record and the run says why it is short.
            stop = hit
            break

    result.groups = sum(1 for value in grouped_with.values() if value is None)
    result.cost = budget.price(
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
    )
    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.stopped_early = stop is not None
    result.stopped_reason = stop

    run.finished_at = dt.datetime.now(dt.UTC)
    run.duration_ms = result.duration_ms
    run.groups = result.groups
    run.cost = result.cost
    run.stopped_early = result.stopped_early
    run.stopped_reason = result.stopped_reason
    await save()
    await record_event(
        session,
        context,
        event_type=AuditEventType.AUTOPILOT_RUN_FINISHED,
        outcome=AuditOutcome.SUCCESS,
        model=model,
        prompt_version=PROMPT_VERSION,
        duration_ms=result.duration_ms,
        payload={
            "run_id": str(run.id),
            "tickets_seen": result.tickets_seen,
            "resumed_from": result.resumed_from,
            "by_rule": result.by_rule,
            "by_model": result.by_model,
            "reused": result.reused,
            "errors": result.errors,
            "groups": result.groups,
            "quarantined": result.quarantined,
            "window": result.window,
            "sampled": result.sampled,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "cost": result.cost,
            "unpriced": not budget.priced,
            "stopped_reason": result.stopped_reason,
            "collected_from_batch": result.collected,
            "rules": result.rule_names,
        },
    )
    await session.commit()
    return result


#: Re-exported so a caller does not reach past the package for the one name it
#: needs when a prediction declines to answer.
__all__ = [
    "ALERT_QUEUES",
    "UNSURE",
    "InputVariant",
    "ReplayConfig",
    "ReplayResult",
    "already_predicted",
    "code_commit",
    "issue_types_for",
    "replay",
]

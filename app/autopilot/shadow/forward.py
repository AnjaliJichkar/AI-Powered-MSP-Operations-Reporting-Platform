"""The forward shadow: the same classifier, on tickets as they arrive.

Built now and **off** until a person turns it on (`AUTOPILOT_SHADOW_LIVE`,
false by default). What it adds over the replay is that it reads a vendor, and
everything here is shaped by not trusting that:

* **A request budget per hour, ours and not the vendor's.** Autotask refused
  every query for about an hour on 2026-10-01 and nobody knows what its own
  threshold returns, so the poller counts its requests and stops rather than
  finding out the hard way (`RequestBudget`).
* **Back off on anything that is not an answer.** Any non-200 doubles the wait,
  up to a ceiling, and the response is handed to a logger that records the
  status, the headers and a bounded body — which is how the open api-notes
  question gets answered when it next happens.
* **The ticket, the budget and the backoff live in `app/tickets/arrivals.py`**,
  shared with the automation triggers' poll, which reads the same tickets
  under the same budget and calls no model.
* **Nothing is written to a vendor.** This module calls two injected
  callables — "what is new" and "enrich this ticket" — and writes rows to our
  own tables. It imports no connector: `app/autopilot/wiring.py` builds the
  real readers out of the ticket workspace's own code, which is why there is no
  second enrichment path to drift from the first.

**The snapshot keeps evidence, not records.** A fact's value is stored only
where it is a number, a flag or a time; anything else is noted as having been
text and dropped. A hostname is a device's name, and the point of the snapshot
is counts, states and when they were read.

The site of the ticket and the site of the device are both stored, because
"zero wrong-site enrichments" is a gate condition and it cannot be counted
afterwards unless both were written down at the time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.autopilot.config import AutopilotConfig
from app.autopilot.shadow.classify import classify
from app.autopilot.shadow.cost import Budget
from app.autopilot.shadow.grouping import Grouper
from app.autopilot.shadow.prompts import PROMPT_VERSION
from app.autopilot.shadow.rules import apply_rules, rules_for
from app.autopilot.suggestions import record_suggestion
from app.models.enums import AuditEventType, AuditOutcome
from app.models.shadow import ShadowEnrichment, ShadowPrediction, ShadowRun
from app.settings import Settings
from app.tickets.arrivals import Backoff, NewTicket, RequestBudget, with_backoff
from app.tickets.scrub import scrub

#: The connectors the forward shadow can actually ask. Blackpoint is not built,
#: and a snapshot says so rather than leaving it out silently: a tool nobody
#: asked is a gap in the evidence, not a clean bill of health. Cove moved onto
#: the first list when its connector landed; a customer who has no Cove is
#: carried by the enrichment's own `backup.not_assessed` reading, which is the
#: per-customer answer and not a claim about what Desk can do.
LIVE_CONNECTORS = ("ninjaone", "bitdefender", "connectsecure", "cove")
NOT_BUILT = ("blackpoint",)

#: Values a snapshot keeps as they are. Everything else is recorded as text and
#: its content dropped.
KEPT_TYPES = (bool, int, float)


class NotEnabled(RuntimeError):
    """`AUTOPILOT_SHADOW_LIVE` is false, so the forward shadow does not run."""


@dataclass(frozen=True, slots=True)
class DeviceAnswer:
    """Which machine the ticket is about, and how sure we are.

    Straight from the workspace's own resolution (`app/tickets/resolve.py`), so
    the forward shadow and the ticket page cannot disagree about a machine.
    """

    device_id: uuid.UUID | None
    device_site_id: uuid.UUID | None
    method: str
    confidence: str | None
    candidates: int


@dataclass(frozen=True, slots=True)
class EnrichmentAnswer:
    """What the tools said, already reduced to what a snapshot may hold."""

    device: DeviceAnswer
    read_at: dt.datetime
    facts: list[dict[str, Any]]
    reads: int
    errors: list[dict[str, Any]]
    not_assessed: dict[str, str]


class TicketSource(Protocol):
    """ "What is new since this moment", injected so this module imports no
    connector."""

    async def __call__(self, since: dt.datetime) -> list[NewTicket]: ...


class Enricher(Protocol):
    """ "What do the tools say about this ticket's machine", injected likewise."""

    async def __call__(self, ticket: NewTicket) -> EnrichmentAnswer: ...


def keep_value(value: Any) -> Any:
    """A fact's value if a snapshot may hold it, or a marker if not.

    Numbers, flags and times are evidence. A string is a name until proved
    otherwise — a hostname, a logged-in user, a signature version — so it is
    recorded as having been text and its content dropped.
    """
    if value is None or isinstance(value, KEPT_TYPES):
        return value
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return {"type": "list", "length": len(value)}
    if isinstance(value, dict):
        return {"type": "object", "keys": len(value)}
    return {"type": "text"}


def reduce_facts(facts: list[Any]) -> list[dict[str, Any]]:
    """The workspace's facts as a snapshot may store them: id, tool, value."""
    reduced: list[dict[str, Any]] = []
    for fact in facts:
        reduced.append(
            {
                "id": getattr(fact, "id", None),
                "source": getattr(fact, "source", None),
                "value": keep_value(getattr(fact, "value", None)),
            }
        )
    return reduced


@dataclass
class ForwardResult:
    """One pass of the forward shadow. Counts only, as ever."""

    run_id: uuid.UUID
    tickets_seen: int = 0
    by_rule: int = 0
    by_model: int = 0
    errors: int = 0
    cache_read_tokens: int = 0
    enriched: int = 0
    wrong_site: int = 0
    """Enrichments whose device belongs to a different customer than the
    ticket. The gate asks for zero of these, and this is the count."""
    no_device: int = 0
    suggested: int = 0
    """Suggestions written for engineers to review. One per ticket seen; how
    many of them are *shown* is decided per field by the display threshold."""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    budget_exhausted: bool = False


async def forward_pass(
    session: AsyncSession,
    context: AuditContext,
    client_id: uuid.UUID,
    *,
    model_client: Any,
    settings: Settings,
    source: TicketSource,
    enricher: Enricher,
    issue_types: list[str],
    since: dt.datetime,
    policy: AutopilotConfig | None = None,
    budget: RequestBudget | None = None,
    now: dt.datetime | None = None,
) -> ForwardResult:
    """Classify and enrich the tickets that arrived since `since`.

    Refuses to run unless `AUTOPILOT_SHADOW_LIVE` is true: the switch is a
    person's decision and not a default.
    """
    if not settings.autopilot_shadow_live:
        raise NotEnabled(
            "AUTOPILOT_SHADOW_LIVE is false, so the forward shadow does not poll. "
            "Turn it on deliberately; the replay needs no vendor and is unaffected."
        )
    now = now or dt.datetime.now(dt.UTC)
    policy = policy or AutopilotConfig()
    model = settings.autopilot_model or settings.anthropic_model
    spend = budget or RequestBudget(per_hour=settings.autopilot_poll_requests_per_hour)
    money = Budget.from_settings(settings)

    run = ShadowRun(
        id=uuid.uuid4(),
        client_id=client_id,
        mode="forward",
        model=model,
        prompt_version=PROMPT_VERSION,
        config={
            "since": since.isoformat(),
            "requests_per_hour": spend.per_hour,
            "live_connectors": list(LIVE_CONNECTORS),
            "not_built": list(NOT_BUILT),
            "label_policy": policy.as_json(),
        },
        started_at=now,
    )
    session.add(run)
    await session.flush()
    await record_event(
        session,
        context,
        event_type=AuditEventType.AUTOPILOT_RUN_STARTED,
        outcome=AuditOutcome.IN_PROGRESS,
        model=model,
        prompt_version=PROMPT_VERSION,
        payload={"run_id": str(run.id), "mode": "forward", "since": since.isoformat()},
    )
    result = ForwardResult(run_id=run.id)

    if not spend.spend():
        # Out of budget before asking: a pass that does nothing, and says so.
        result.budget_exhausted = True
        tickets: list[NewTicket] = []
    else:
        tickets = await source(since)

    rules = rules_for(issue_types)
    grouper = Grouper(
        window=dt.timedelta(days=settings.autopilot_group_window_days),
        similarity=settings.autopilot_group_similarity,
    )
    semaphore = asyncio.Semaphore(settings.autopilot_concurrency)

    #: ticket id -> the number a person reads, for the tickets this pass has
    #: already walked. A repeat of something from an earlier pass is named by
    #: the page instead, which can look it up.
    numbers: dict[str, str] = {}

    for ticket in sorted(tickets, key=lambda item: item.created_at or now):
        result.tickets_seen += 1
        title = scrub(ticket.title).text
        grouped = (
            grouper.consider(
                ticket_id=ticket.ticket_id,
                site_id=ticket.site_id,
                created_at=ticket.created_at,
                title=title,
            )
            if ticket.created_at is not None
            else None
        )

        # The body goes to the model and no further: scrubbed in flight,
        # stored nowhere, and only its presence recorded.
        body = scrub(ticket.description).text if ticket.description else None
        answer = apply_rules(title, rules)
        prediction: Any = None
        path = "rule"
        latency = tokens_in = tokens_out = tokens_cached = 0
        error_type = error_message = None
        if answer is None:
            async with semaphore:
                found = await classify(
                    model_client,
                    model=model,
                    title=title,
                    site=f"site-{str(ticket.site_id)[:8]}" if ticket.site_id else "site-unknown",
                    created_at=ticket.created_at,
                    issue_types=issue_types,
                    description=body,
                )
            latency, tokens_in, tokens_out, tokens_cached = (
                found.latency_ms,
                found.input_tokens,
                found.output_tokens,
                found.cache_read_tokens,
            )
            result.input_tokens += tokens_in
            result.output_tokens += tokens_out
            result.cache_read_tokens += tokens_cached
            if found.ok:
                prediction, path = found.prediction, "model"
                result.by_model += 1
            else:
                path = "error"
                error_type, error_message = found.error_type, found.error_message
                result.errors += 1
        else:
            result.by_rule += 1

        source_of = answer or prediction
        raw = getattr(source_of, "issue_type", None)
        final, verdict = policy.apply(raw)
        predicted = ShadowPrediction(
            run_id=run.id,
            ticket_id=ticket.ticket_id,
            site_id=ticket.site_id,
            created_at_vendor=ticket.created_at,
            path=path,
            origin=getattr(source_of, "origin", None),
            origin_confidence=getattr(source_of, "origin_confidence", None),
            issue_type=final,
            issue_type_raw=raw,
            issue_type_policy=verdict if raw is not None else None,
            issue_type_confidence=getattr(source_of, "issue_type_confidence", None),
            urgency=getattr(source_of, "urgency", None),
            urgency_confidence=getattr(source_of, "urgency_confidence", None),
            group_with=grouped.ticket_id if grouped else None,
            group_basis=grouped.basis if grouped else None,
            group_score=grouped.score if grouped else None,
            rule_name=answer.rule_name if answer else None,
            latency_ms=latency,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cache_read_tokens=tokens_cached,
            cost=money.price(input_tokens=tokens_in, output_tokens=tokens_out),
            error_type=error_type,
            error_message=error_message,
            title_quarantined=False,
            queue_label_at_arrival=ticket.queue_label,
            has_description=bool(body),
        )
        session.add(predicted)

        # --- enrichment, through the workspace's own code ------------------
        snapshot = await enricher(ticket)
        result.enriched += 1
        if snapshot.device.device_id is None:
            result.no_device += 1
        elif (
            snapshot.device.device_site_id is not None
            and ticket.site_id is not None
            and snapshot.device.device_site_id != ticket.site_id
        ):
            # The failure the gate counts. Recorded loudly rather than fixed
            # here: a wrong-site match is a matching bug, and quietly dropping
            # it would hide the thing the fortnight is meant to prove.
            result.wrong_site += 1
        snapshot_row = ShadowEnrichment(
            run_id=run.id,
            ticket_id=ticket.ticket_id,
            site_id=ticket.site_id,
            device_id=snapshot.device.device_id,
            device_site_id=snapshot.device.device_site_id,
            device_method=snapshot.device.method,
            device_confidence=snapshot.device.confidence,
            candidates=snapshot.device.candidates,
            read_at=snapshot.read_at,
            facts=snapshot.facts,
            fact_count=len(snapshot.facts),
            reads=snapshot.reads,
            errors=snapshot.errors,
            not_assessed={
                **snapshot.not_assessed,
                **{name: "connector not built" for name in NOT_BUILT},
            },
        )
        session.add(snapshot_row)

        # --- what the engineer will be offered ----------------------------
        # The suggestion is written from the prediction rather than from a
        # second reading of the ticket: the answer an engineer approves and
        # the answer the gate scores have to be the same answer, or the page
        # and the scorecard are measuring two different agents.
        await session.flush()
        await record_suggestion(
            session,
            client_id=client_id,
            prediction=predicted,
            enrichment=snapshot_row,
            display_threshold=settings.autopilot_display_threshold,
            blind_rate=settings.autopilot_blind_rate,
            ticket_number=ticket.ticket_number,
            group_with_number=numbers.get(grouped.ticket_id) if grouped else None,
            now=now,
        )
        result.suggested += 1
        if ticket.ticket_number:
            numbers[ticket.ticket_id] = ticket.ticket_number

    result.cost = money.price(
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
    )
    run.finished_at = dt.datetime.now(dt.UTC)
    run.tickets_seen = result.tickets_seen
    run.by_rule = result.by_rule
    run.by_model = result.by_model
    run.errors = result.errors
    run.groups = sum(1 for _ in range(0))  # forward passes do not count groups per pass
    run.input_tokens = result.input_tokens
    run.output_tokens = result.output_tokens
    run.cache_read_tokens = result.cache_read_tokens
    run.cost = result.cost
    run.stopped_early = result.budget_exhausted
    run.stopped_reason = "request_budget" if result.budget_exhausted else None
    await record_event(
        session,
        context,
        event_type=AuditEventType.AUTOPILOT_RUN_FINISHED,
        outcome=AuditOutcome.SUCCESS,
        model=model,
        prompt_version=PROMPT_VERSION,
        payload={
            "run_id": str(run.id),
            "mode": "forward",
            "tickets_seen": result.tickets_seen,
            "by_rule": result.by_rule,
            "by_model": result.by_model,
            "errors": result.errors,
            "enriched": result.enriched,
            "no_device": result.no_device,
            "suggested": result.suggested,
            "wrong_site": result.wrong_site,
            "requests_spent": spend.spent,
            "budget_exhausted": result.budget_exhausted,
        },
    )
    return result


__all__ = [
    "LIVE_CONNECTORS",
    "NOT_BUILT",
    "Backoff",
    "DeviceAnswer",
    "Enricher",
    "EnrichmentAnswer",
    "ForwardResult",
    "NewTicket",
    "NotEnabled",
    "RequestBudget",
    "TicketSource",
    "forward_pass",
    "keep_value",
    "reduce_facts",
    "with_backoff",
]

"""The same replay, sent as a batch: about half the price, hours not minutes.

A replay is thousands of independent requests that nobody is waiting on, which
is exactly what the Message Batches API is for — the same model, the same
prompt, the same tool, at about half the per-token price, answered within a
day. Nothing about the answers changes: a batch result holds the same message
object a request would have returned, and it is read by the same function
(`classify.read_message`), because a second parser would be a second set of
rules about what counts as a valid answer.

Three rules this path keeps:

* **A submitted batch is on the record before it is waited for.** The run row
  and a `shadow_batches` row are committed at submission, so a replay that is
  interrupted, or simply left for the afternoon, is carried on by running the
  same command again rather than by starting over. A batch id in a terminal
  somebody has closed is money spent and nothing to show for it.
* **Live paths never use it.** The forward shadow, the backfill and the ticket
  page are answering about a ticket an engineer is looking at now; hours is not
  a latency, it is a different product. They send ordinary requests, and this
  module is reachable only from the replay.
* **It is behind a setting** (`AUTOPILOT_BATCH_REPLAY`), off by default,
  because "the answers arrive tomorrow" is a surprise to anybody who has not
  chosen it.

The client is passed in, as it is everywhere else here, so a test hands this a
fake and nothing reaches the network.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.autopilot.shadow.classify import Classification, failure, fingerprint, read_message
from app.models.shadow import ShadowBatch, ShadowRun

#: What a batch can be, in the vendor's words plus ours. `collected` is ours:
#: the vendor's batch has ended and we have written its answers down.
BATCH_STATES = ("in_progress", "canceling", "ended", "collected", "failed")

#: The vendor's cap on one batch. Well above a full replay of the pilot's
#: ninety days, so a run is one batch; the chunking is here anyway because a
#: tenant ten times the size is a tenant, not a special case.
MAX_REQUESTS = 10_000


@dataclass(frozen=True, slots=True)
class Pending:
    """A batch that has been sent and not yet written down."""

    batch_id: uuid.UUID
    external_id: str
    requests: int
    status: str

    @property
    def ended(self) -> bool:
        return self.status == "ended"


def _requests(params_by_ticket: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The vendor's request list: our ticket id as the custom id.

    The ticket id and nothing else, because the answers come back keyed by it
    and a custom id that needed a lookup table would be a lookup table to lose.
    """
    return [
        {"custom_id": ticket_id, "params": params}
        for ticket_id, params in sorted(params_by_ticket.items())
    ]


async def submit(
    session: AsyncSession,
    model_client: Any,
    run_id: uuid.UUID,
    params_by_ticket: dict[str, dict[str, Any]],
    *,
    now: dt.datetime | None = None,
) -> Pending:
    """Send one batch and write down that it was sent, in that order.

    In that order on purpose: a row written first and a send that then fails
    would have us waiting for a batch nobody has. The other way round, the
    worst case is a batch that cost money and has no row — which the vendor's
    own listing can still find, and which the next submission does not
    duplicate because the tickets have no predictions yet.
    """
    now = now or dt.datetime.now(dt.UTC)
    requests = _requests(params_by_ticket)
    if len(requests) > MAX_REQUESTS:
        raise ValueError(
            f"{len(requests)} requests is more than one batch takes ({MAX_REQUESTS}); "
            "replay a sample, or raise the cap deliberately"
        )
    sent = await cast(Any, model_client).messages.batches.create(requests=requests)
    row = ShadowBatch(
        id=uuid.uuid4(),
        run_id=run_id,
        external_id=str(getattr(sent, "id", "")),
        status=str(getattr(sent, "processing_status", "in_progress")),
        requests=len(requests),
        submitted_at=now,
    )
    session.add(row)
    await session.commit()
    return Pending(
        batch_id=row.id,
        external_id=row.external_id,
        requests=row.requests,
        status=row.status,
    )


async def pending_for(session: AsyncSession, run_id: uuid.UUID) -> Pending | None:
    """The batch this run is waiting on, if it is waiting on one."""
    row = (
        (
            await session.execute(
                select(ShadowBatch)
                .where(ShadowBatch.run_id == run_id, ShadowBatch.collected_at.is_(None))
                .order_by(ShadowBatch.submitted_at)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    return Pending(
        batch_id=row.id,
        external_id=row.external_id,
        requests=row.requests,
        status=row.status,
    )


async def run_waiting_on_a_batch(session: AsyncSession, client_id: uuid.UUID) -> uuid.UUID | None:
    """The newest run of this client with a batch nobody has collected.

    What makes "run the command again" the whole instruction: a replay that
    finds one carries it on rather than starting a second run and sending the
    same questions a second time.
    """
    row = (
        (
            await session.execute(
                select(ShadowBatch)
                .join(ShadowRun, ShadowRun.id == ShadowBatch.run_id)
                .where(ShadowRun.client_id == client_id, ShadowBatch.collected_at.is_(None))
                .order_by(ShadowBatch.submitted_at.desc())
            )
        )
        .scalars()
        .first()
    )
    return row.run_id if row is not None else None


async def refresh(session: AsyncSession, model_client: Any, pending: Pending) -> Pending:
    """Ask the vendor where the batch is, and write the answer on the row."""
    found = await cast(Any, model_client).messages.batches.retrieve(pending.external_id)
    status = str(getattr(found, "processing_status", pending.status))
    row = await session.get(ShadowBatch, pending.batch_id)
    if row is not None and row.status != status:
        row.status = status
        await session.commit()
    return Pending(
        batch_id=pending.batch_id,
        external_id=pending.external_id,
        requests=pending.requests,
        status=status,
    )


async def collected(
    session: AsyncSession, pending: Pending, *, now: dt.datetime | None = None
) -> None:
    """Mark a batch written down, so the next run does not wait on it again."""
    row = await session.get(ShadowBatch, pending.batch_id)
    if row is not None:
        row.collected_at = now or dt.datetime.now(dt.UTC)
        row.status = "collected"
        await session.commit()


def read_entry(entry: Any, *, issue_types: list[str], digest: str = "") -> Classification:
    """One result line as a classification.

    A batch answers four ways and three of them are not a message: errored,
    canceled, expired. Each becomes a recorded error with the vendor's own word
    for it, which is the same thing a failed request becomes — the run reports
    what happened rather than quietly having fewer rows.
    """
    result = getattr(entry, "result", None)
    kind = str(getattr(result, "type", "") or "")
    if kind == "succeeded":
        message = getattr(result, "message", None)
        if message is None:
            return failure("BatchResultEmpty", digest=digest)
        return read_message(message, issue_types=issue_types, digest=digest)
    error = getattr(result, "error", None)
    detail = getattr(error, "type", None) or getattr(error, "message", None) or kind or "unknown"
    return failure(f"Batch{kind.title() or 'Unknown'}: {detail}", digest=digest)


async def results(
    model_client: Any, pending: Pending, *, issue_types: list[str], digests: dict[str, str]
) -> list[tuple[str, Classification]]:
    """Every answer in the batch, as (ticket id, classification).

    Read whole rather than streamed into the database: a replay of a few
    thousand short answers is a few hundred kilobytes, and a half-written
    collection is the one state this path has no way to resume from.
    """
    out: list[tuple[str, Classification]] = []
    stream = await cast(Any, model_client).messages.batches.results(pending.external_id)
    async for entry in stream:
        ticket_id = str(getattr(entry, "custom_id", "") or "")
        if not ticket_id:
            continue
        answer = read_entry(entry, issue_types=issue_types, digest=digests.get(ticket_id, ""))
        out.append((ticket_id, answer))
    return out


def digest_of(params_by_ticket: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The digests of what was sent, so reuse works on batch answers too."""
    return {ticket_id: fingerprint(params) for ticket_id, params in params_by_ticket.items()}


__all__: Sequence[str] = [
    "BATCH_STATES",
    "MAX_REQUESTS",
    "Pending",
    "collected",
    "digest_of",
    "pending_for",
    "read_entry",
    "refresh",
    "results",
    "run_waiting_on_a_batch",
    "submit",
]

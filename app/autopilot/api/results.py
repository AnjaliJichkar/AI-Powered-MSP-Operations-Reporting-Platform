"""/autopilot/results/*: what the shadow runs say. Read only, and provably so.

The page at `/autopilot/results` shows a run against the gate. Nothing here
changes anything: there is no POST, PUT, PATCH or DELETE in this module, the
forward-shadow switch is reported as a **status** rather than offered as a
toggle, and `tests/api/test_autopilot_results.py` asserts the router's method
list so that stays true. A page that can start a run is a page somebody starts
a run from by accident.

Inputiv staff only (`INPUTIV_ANY`), like the labelling page: this is a
measurement of our agent.

**Every number carries its definition**, in the payload beside it, the same
rule the ticket statistics follow — a definition written on the page and a
figure computed on the server drift apart, and the server is the thing that
knows.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import INPUTIV_ANY
from app.auth.scope import Scope
from app.autopilot.config import AutopilotConfig
from app.autopilot.gate import blind_answers, build_gate, forward_state
from app.autopilot.labelling import group_judgements, origin_labels
from app.autopilot.shadow.scoring import (
    ThresholdRule,
    joined_rows,
    lift,
    projected_monthly,
    score_rows,
    split,
)
from app.autopilot.suggestions import finding_review_counts, live_review_counts
from app.db.session import get_session
from app.models.labelling import LabellingSet
from app.models.shadow import ShadowRun
from app.models.suggestion import TicketSuggestion
from app.reports.tbr.config import load_config
from app.settings import get_settings

router = APIRouter(prefix="/autopilot/results", tags=["autopilot"])

#: What every figure on the page means, in one place, handed to the page with
#: the figures. The page shows these on hover; nothing is left for a reader to
#: infer from a colour or a column heading.
DEFINITIONS: dict[str, str] = {
    "accuracy": "Of the tickets the agent answered, the share it got right. Tickets it "
    "declined (`unsure`) are not in the denominator: a decline is not a wrong answer.",
    "coverage": "Of the tickets that could be judged, the share the agent answered at all. "
    "Reported beside every accuracy, because a threshold trades one for the other.",
    "judged": "Tickets with both a prediction and a stored outcome to compare it to. "
    "Excludes tickets the tenant left untyped, and the titles the old scrubber damaged.",
    "development": "The first stretch of the window, where a label policy or a threshold "
    "may be chosen. Split on creation time, measured back from the newest ticket.",
    "holdout": "The last stretch of the window, scored and otherwise left alone. A "
    "threshold chosen by looking at these tickets would be a memory, not a measurement.",
    "threshold": "The confidence below which the agent is treated as having declined. "
    "Chosen on the development period by the rule, then read on the holdout.",
    "before_policy": "What the model said before the franchisee's label policy was applied. "
    "The raw answer is stored on every row, so the policy's worth is one run's two numbers.",
    "rule_path": "Tickets a deterministic rule answered without calling the model, where a "
    "machine's wording is unmistakable. Scored apart so the cheap path has to earn its place.",
    "fresh_input": "Input tokens read for the first time, billed at the full input rate.",
    "cached_input": "Input served from the prompt cache, billed at a fraction. A replay reads "
    "the same instructions once per ticket, so adding the two together overstates the cost.",
    "proxy": "Not a score. A stand-in for ground truth we do not have, named as one wherever "
    "it appears: the labelled sample is the measurement.",
    "wrong_site": "An enrichment whose device belongs to a different customer than the ticket. "
    "The gate asks for none of these, and both sites are stored so it can be counted.",
    "not_assessed": "A tool that was not asked, and why. A tool nobody asked is a gap in the "
    "evidence rather than a clean bill of health.",
}


# --- what comes back ------------------------------------------------------


class RunRow(BaseModel):
    """One run, as the list reads it."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    mode: str
    variant: str | None
    model: str
    prompt_version: str
    code_commit: str | None
    started_at: dt.datetime
    finished_at: dt.datetime | None
    duration_ms: int | None
    status: str
    """`running`, `finished`, or `stopped` — derived, so the page does not have
    to work out what a null `finished_at` means."""
    stopped_reason: str | None
    tickets: int
    by_rule: int
    by_model: int
    errors: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost: float | None


# --- the routes -----------------------------------------------------------


async def _run_of(db: AsyncSession, scope: Scope, run_id: uuid.UUID) -> ShadowRun:
    """One run of this client's, or a 404 — never another client's."""
    found = await db.get(ShadowRun, run_id)
    if found is None or found.client_id != scope.client_id:
        raise HTTPException(status_code=404, detail="No shadow run of that id.")
    return found


def _status(run: ShadowRun) -> str:
    if run.stopped_early:
        return "stopped"
    return "finished" if run.finished_at is not None else "running"


def _row(run: ShadowRun) -> RunRow:
    config = run.config or {}
    return RunRow(
        run_id=run.id,
        mode=run.mode,
        variant=config.get("variant"),
        model=run.model,
        prompt_version=run.prompt_version,
        code_commit=run.code_commit,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        status=_status(run),
        stopped_reason=run.stopped_reason,
        tickets=run.tickets_seen,
        by_rule=run.by_rule,
        by_model=run.by_model,
        errors=run.errors,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_tokens=run.cache_read_tokens,
        cost=float(run.cost) if run.cost is not None else None,
    )


@router.get("/runs", response_model=list[RunRow])
async def runs(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunRow]:
    """This client's runs, newest first."""
    rows = (
        (
            await db.execute(
                select(ShadowRun)
                .where(ShadowRun.client_id == scope.client_id)
                .order_by(desc(ShadowRun.started_at))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_row(run) for run in rows]


async def _policy(db: AsyncSession, scope: Scope, *, apply_policy: bool) -> AutopilotConfig | None:
    """The franchisee's label policy, when the caller wants it applied."""
    if not apply_policy or scope.client_id is None:
        return None
    config = await load_config(db, scope.client_id, client_name=scope.client_name or "")
    return config.autopilot


@router.get("/runs/{run_id}")
async def run_detail(
    run_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    holdout_days: Annotated[int, Query(ge=1, le=365)] = 30,
    apply_policy: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """One run against the gate, scored on both periods.

    Both periods, always: the threshold is chosen on one and read on the other,
    and a page that showed only the holdout would hide the choosing. The gate
    panel says which period gave which number.
    """
    run = await _run_of(db, scope, run_id)
    assert scope.client_id is not None
    settings = get_settings()
    policy = await _policy(db, scope, apply_policy=apply_policy)
    rows = await joined_rows(db, run_id, client_id=scope.client_id, policy=policy)

    rule = ThresholdRule.from_settings(settings)
    # The threshold comes from a **replay**, whichever run the page is showing.
    # The gate is a property of the client rather than of one run: the rule
    # needs a tuning period, which only a replay over past tickets has, while
    # the accuracy it is then read at comes from blind answers about live
    # tickets. Selecting a forward pass must not make the gate unreadable.
    tuning_rows = rows
    if run.mode != "replay":
        latest_replay = (
            await db.execute(
                select(ShadowRun)
                .where(
                    ShadowRun.client_id == scope.client_id,
                    ShadowRun.mode == "replay",
                    ShadowRun.finished_at.is_not(None),
                )
                .order_by(desc(ShadowRun.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_replay is not None:
            tuning_rows = await joined_rows(
                db, latest_replay.id, client_id=scope.client_id, policy=policy
            )
    # Past-ticket judgements belong to the sample, not to the run on screen:
    # a sample is drawn from one replay and the gate is a property of the
    # client, so selecting a different run must not empty them.
    sampled_run = (
        await db.execute(
            select(LabellingSet.run_id)
            .where(LabellingSet.client_id == scope.client_id, LabellingSet.kind == "origin")
            .order_by(LabellingSet.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or run_id
    grouped_run = (
        await db.execute(
            select(LabellingSet.run_id)
            .where(LabellingSet.client_id == scope.client_id, LabellingSet.kind == "groups")
            .order_by(LabellingSet.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none() or run_id
    labels = await origin_labels(db, client_id=scope.client_id, run_id=sampled_run)
    groups = await group_judgements(db, client_id=scope.client_id, run_id=grouped_run)
    forward = await forward_state(
        db, client_id=scope.client_id, live=settings.autopilot_shadow_live
    )
    # What people said blind, and what the agent said on live tickets: the two
    # the gate's accuracy criteria are read against. Both span runs — a blind
    # check is about a ticket, not about a replay — so they are read per client
    # rather than per run.
    said = await blind_answers(db, client_id=scope.client_id)
    suggested = (
        (
            await db.execute(
                select(TicketSuggestion).where(TicketSuggestion.client_id == scope.client_id)
            )
        )
        .scalars()
        .all()
    )
    reviews = await live_review_counts(db, client_id=scope.client_id)
    # Which findings earn their place on the card. Recomputed from the
    # snapshots rather than stored: they are a pure function of rows we keep.
    findings = await finding_review_counts(db, client_id=scope.client_id)
    gate = build_gate(
        tuning_rows,
        rule=rule,
        holdout_days=holdout_days,
        labels=labels,
        groups=groups,
        forward=forward,
        blind=said,
        suggestions=list(suggested),
        reviews=reviews,
    )

    periods: dict[str, Any] = {}
    for period in ("development", "holdout"):
        card = score_rows(
            split(rows, period=period, holdout_days=holdout_days),
            run_id,
            period=period,
        )
        card.input_tokens = run.input_tokens
        card.output_tokens = run.output_tokens
        card.cache_read_tokens = run.cache_read_tokens
        card.cost = float(run.cost) if run.cost is not None else None
        card.scored_under_policy = policy is not None
        periods[period] = card.as_json()

    days = int((run.config or {}).get("days", 90))
    whole = score_rows(rows, run_id, period="all")
    whole.cost = float(run.cost) if run.cost is not None else None
    return {
        "run": _row(run).model_dump(),
        "config": run.config or {},
        "gate": gate.as_json(),
        "periods": periods,
        "origin_labels": {
            "labelled": labels.labelled,
            "skipped": labels.skipped,
            "unlabelled": labels.unlabelled,
            "disagreed": labels.disagreed,
            "agreement": labels.agreement,
            "pairs": labels.agreement_pairs,
        },
        "groups_judged": groups.as_json(),
        # Live tickets: what was suggested, what engineers made of it, and —
        # separately, because they are different measurements — what people
        # said blind. Both are drawn, so the gap between approval and accuracy
        # is on the page rather than in somebody's head.
        "live_reviews": reviews.as_json(),
        "findings": {kind: tally.as_json() for kind, tally in sorted(findings.items())},
        "blind": {
            "tickets": said.answered,
            "disagreed": said.disagreed,
            "origin": len(said.answers_of("origin")),
            "issue_type": len(said.answers_of("issue_type")),
        },
        "forward": forward.as_json(),
        "cost": {
            "fresh_input": run.input_tokens,
            "cached_input": run.cache_read_tokens,
            "output": run.output_tokens,
            "total": float(run.cost) if run.cost is not None else None,
            "unpriced": run.cost is None,
            "per_ticket": (
                float(run.cost) / run.tickets_seen
                if run.cost is not None and run.tickets_seen
                else None
            ),
            "projected_monthly_one": projected_monthly(whole, days=days, tickets=run.tickets_seen),
            "projected_monthly_twenty": projected_monthly(
                whole, days=days, tickets=run.tickets_seen, franchisees=20
            ),
        },
        "definitions": DEFINITIONS,
    }


@router.get("/compare")
async def compare(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
    baseline: Annotated[uuid.UUID, Query(description="the run to compare against")],
    variant: Annotated[uuid.UUID, Query(description="the run to compare")],
    period: Annotated[str, Query(pattern="^(all|development|holdout)$")] = "holdout",
    holdout_days: Annotated[int, Query(ge=1, le=365)] = 30,
    apply_policy: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Two runs on the same period, field by field.

    The difference is reported as accuracy **and** coverage, because an extra
    input that only makes the agent bolder shows up in the second and would
    read as an improvement if only the first were shown.
    """
    first = await _run_of(db, scope, baseline)
    second = await _run_of(db, scope, variant)
    assert scope.client_id is not None
    policy = await _policy(db, scope, apply_policy=apply_policy)

    cards = []
    for run in (first, second):
        rows = await joined_rows(db, run.id, client_id=scope.client_id, policy=policy)
        card = score_rows(
            split(rows, period=period, holdout_days=holdout_days),  # type: ignore[arg-type]
            run.id,
            period=period,  # type: ignore[arg-type]
        )
        cards.append(card)

    return {
        "period": period,
        "baseline": {**_row(first).model_dump(), "scorecard": cards[0].as_json()},
        "variant": {**_row(second).model_dump(), "scorecard": cards[1].as_json()},
        "lift": lift(cards[0], cards[1]),
        # A comparison of two runs that differ in more than one thing is worth
        # less, so the page is told what differs rather than being left to
        # guess from two config blocks.
        "differs": sorted(
            key
            for key in set(first.config or {}) | set(second.config or {})
            if (first.config or {}).get(key) != (second.config or {}).get(key)
        )
        + (["model"] if first.model != second.model else [])
        + (["prompt_version"] if first.prompt_version != second.prompt_version else []),
        "definitions": DEFINITIONS,
    }


@router.get("/forward")
async def forward(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """The forward shadow's switch status and what it has seen.

    A **status**, not a toggle: the switch is `AUTOPILOT_SHADOW_LIVE` and it is
    turned on by a person editing settings, not by a button on a page. Off and
    with nothing run, the page shows an empty state saying so rather than four
    zeroes that read like a result.
    """
    assert scope.client_id is not None
    settings = get_settings()
    state = await forward_state(db, client_id=scope.client_id, live=settings.autopilot_shadow_live)
    return {
        **state.as_json(),
        "switch": {
            "setting": "AUTOPILOT_SHADOW_LIVE",
            "live": settings.autopilot_shadow_live,
            "detail": (
                "The forward shadow is polling Autotask for new tickets."
                if settings.autopilot_shadow_live
                else "Nothing is being polled; the replay needs no vendor and is "
                "unaffected. Turned on by a person in settings, never from this page."
            ),
            "requests_per_hour": settings.autopilot_poll_requests_per_hour,
        },
        "empty": state.runs == 0,
        "definitions": DEFINITIONS,
    }

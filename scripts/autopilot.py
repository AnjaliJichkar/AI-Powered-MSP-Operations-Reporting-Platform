"""Autopilot from the terminal. Level 0: it predicts and is scored, nothing else.

    python scripts/autopilot.py shadow replay  --client <slug> [--days 90] [--limit 50]
    python scripts/autopilot.py shadow score   --client <slug> [--run <uuid>]
    python scripts/autopilot.py shadow sheets  --client <slug> [--run <uuid>]
    python scripts/autopilot.py shadow score-origin --client <slug> --sheet <path>

`replay` reads stored ledger rows and calls **no vendor**: the whole of Part A
runs on `ticket_outcomes`, so measuring the agent costs the franchisee's
Autotask tenant nothing. It does call the model, which is what `--limit` is
for — try fifty before two thousand seven hundred.

`score` joins a run's predictions to what actually happened. Arithmetic over
two tables, so it is free and can be re-run whenever the question changes.

`sheets` writes the two things a person has to read — 200 tickets to label
`alert` or `human`, and 30 groups to spot-check — into `var/`, which is
git-ignored. Those files carry ticket titles; nothing else here prints one.

**Counts, ids and labels only.** No customer, site, device or contact name
reaches the terminal (CLAUDE.md, working conventions); a site is printed as the
first characters of its id, as the replay shows it to the model.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import pathlib
import sys
import time
import uuid
from typing import Any

from sqlalchemy import desc, func, select

from app.agent.loop import get_model_client
from app.audit.recorder import AuditContext, record_event
from app.autopilot.labelling import group_judgements, origin_labels
from app.autopilot.shadow.batching import run_waiting_on_a_batch
from app.autopilot.shadow.forward import (
    LIVE_CONNECTORS,
    Backoff,
    NewTicket,
    NotEnabled,
    RequestBudget,
    forward_pass,
    with_backoff,
)
from app.autopilot.shadow.replay import ReplayConfig, issue_types_for, replay
from app.autopilot.shadow.sampling import DEFAULT_SIZE, ITERATION, sample_for
from app.autopilot.shadow.scoring import lift, projected_monthly, score_run
from app.autopilot.shadow.sheets import (
    SheetRead,
    read_labels,
    score_origin,
    write_group_sheet,
    write_origin_sheet,
)
from app.autopilot.suggestions import has_anything_to_show
from app.autopilot.wiring import autotask_open_tickets, autotask_source, workspace_enricher
from app.db.session import session_scope
from app.models.client import Client
from app.models.enums import AuditEventType, AuditOutcome, Interface
from app.models.shadow import ShadowEnrichment, ShadowRun
from app.models.suggestion import TicketSuggestion
from app.reports.tbr.config import load_config
from app.settings import get_settings

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

#: Where the sheets go. Git-ignored, because they hold ticket titles.
VAR = pathlib.Path("var") / "autopilot"


class UnknownClient(ValueError):
    """No client of that slug or id."""


async def _client(slug: str) -> Client:
    async with session_scope() as session:
        try:
            query = select(Client).where(Client.id == uuid.UUID(slug))
        except ValueError:
            query = select(Client).where(Client.slug == slug)
        client = (await session.execute(query)).scalar_one_or_none()
        if client is None:
            raise UnknownClient(f"no client {slug!r}")
        return client


def _context(client: Client, actor: str) -> AuditContext:
    return AuditContext.new(
        actor_id=actor,
        actor_user_id=None,
        interface=Interface.CLI,
        client_id=client.id,
        client_ref=client.slug,
    )


async def _latest_run(session: Any, client_id: uuid.UUID) -> ShadowRun | None:
    return (
        await session.execute(
            select(ShadowRun)
            .where(ShadowRun.client_id == client_id)
            .order_by(desc(ShadowRun.started_at))
            .limit(1)
        )
    ).scalar_one_or_none()


# --- replay -------------------------------------------------------------------------------


def _progress(total_hint: int) -> Any:
    """Print where the run is after each committed batch.

    A nine-minute run with nothing on the terminal is indistinguishable from a
    hung one, and the rows are not visible from another connection until they
    are committed — so the run says so itself.
    """
    started = time.monotonic()

    def report(done: int, total: int, tokens: int) -> None:
        elapsed = time.monotonic() - started
        rate = done / elapsed if elapsed > 0 else 0.0
        left = (total - done) / rate if rate > 0 else 0.0
        print(
            f"  {done}/{total} ticket(s), {tokens} token(s), {elapsed:.0f}s elapsed"
            f"{f', ~{left:.0f}s left' if done < total else ''}",
            flush=True,
        )

    return report


async def _replay(args: argparse.Namespace) -> int:
    client = await _client(args.client)
    settings = get_settings()
    config = ReplayConfig(
        days=args.days,
        limit=args.limit,
        variant=args.variant,
        # The sample by default, the window when somebody asks for it. A run
        # that resumes another takes that run's own choice, which is on its
        # config rather than on today's flags.
        sample=None if args.full else args.sample,
        group_window_days=settings.autopilot_group_window_days,
        group_similarity=settings.autopilot_group_similarity,
        batch=args.batch,
    )
    context = _context(client, args.user)
    # A batch somebody sent this morning is collected by running the same
    # command again: the replay finds the run that is waiting rather than
    # starting a second one and asking everything twice.
    resume = uuid.UUID(args.resume) if args.resume else None
    if resume is None and (settings.autopilot_batch_replay or args.batched):
        async with session_scope() as session:
            resume = await run_waiting_on_a_batch(session, client.id)
        if resume is not None:
            print(f"carrying on run {resume}, which has a batch out")
    # The franchisee's own label policy, from their settings. A run records it,
    # so a number taken under one policy is never compared with one taken
    # under another by accident.
    async with session_scope() as session:
        franchisee = await load_config(session, client.id, client_name=client.name)
    policy = franchisee.autopilot
    if policy.configured:
        print(
            f"label policy: {len(policy.forbidden_issue_types)} forbidden, "
            f"{len(policy.issue_type_remap)} remapped"
        )
    try:
        model_client = get_model_client()
    except Exception as exc:  # no key, no run: say so rather than half-running
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    try:
        async with session_scope() as session:
            result = await replay(
                session,
                context,
                client.id,
                model_client=model_client,
                settings=settings,
                config=config,
                policy=policy,
                resume=resume,
                model=args.model,
                batched=True if args.batched else None,
                reuse=not args.no_reuse,
                on_progress=None if args.json else _progress(0),
            )
    except Exception as exc:
        # The failure goes on the record in its own transaction, as every
        # failed read does, so a run that rolled back still happened.
        async with session_scope() as session:
            await record_event(
                session,
                context,
                event_type=AuditEventType.AUTOPILOT_RUN_FAILED,
                outcome=AuditOutcome.ERROR,
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
                payload={"mode": "replay", **config.as_json()},
            )
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    payload = {
        "run_id": str(result.run_id),
        "tickets_seen": result.tickets_seen,
        "by_rule": result.by_rule,
        "by_model": result.by_model,
        "reused": result.reused,
        "fresh": result.by_model - result.reused,
        "output_tokens_per_fresh_call": (
            result.output_tokens / (result.by_model - result.reused)
            if result.by_model - result.reused
            else None
        ),
        "errors": result.errors,
        "groups": result.groups,
        "quarantined": result.quarantined,
        "sampled": result.sampled,
        "window": result.window,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "resumed_from": result.resumed_from,
        "pending_batch": result.pending_batch,
        "pending_requests": result.pending_requests,
        "collected_from_batch": result.collected,
        "cost": result.cost,
        "duration_ms": result.duration_ms,
        "stopped_reason": result.stopped_reason,
        "rules": result.rule_names,
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    if result.pending_batch:
        print(f"run {result.run_id}")
        print(
            f"  batch {result.pending_batch}: {result.pending_requests} request(s) out. "
            "Run the same command again to collect them; nothing is lost in between."
        )
        if result.by_rule or result.reused:
            print(
                f"  already answered without it: {result.by_rule} by rule, {result.reused} reused"
            )
        return EXIT_OK
    print(f"run {result.run_id}")
    if result.collected:
        print(f"  collected {result.collected} answer(s) from a batch")
    if result.sampled is not None:
        print(f"  sample: {result.sampled} ticket(s) of the window's {payload['window']}")
    if result.resumed_from:
        print(f"  resumed: {result.resumed_from} ticket(s) were already answered")
    print(
        f"  {result.tickets_seen} ticket(s): {result.by_rule} by rule, "
        f"{result.by_model} by model, {result.errors} invalid"
    )
    print(
        f"  model answers: {result.by_model - result.reused} fresh, {result.reused} reused "
        f"from earlier runs (same ticket, same request, same model and prompt)"
    )
    print(f"  rules that fired: {result.rule_names or 'none'}")
    # Over the window, not over the sample: grouping walks every ticket in
    # creation order whatever is being classified, so counting it against the
    # tickets this run answered gave a sampled run a negative number.
    walked = result.window or result.tickets_seen
    print(
        f"  grouping over the window's {walked} ticket(s): {result.groups} group(s) opened, "
        f"{walked - result.groups} joined an earlier one"
    )
    print(f"  {result.quarantined} title(s) quarantined (old over-redaction)")
    cost = "unpriced" if result.cost is None else f"${result.cost:.4f}"
    fresh = result.by_model - result.reused
    print(
        f"  {result.input_tokens} input + {result.output_tokens} output + "
        f"{result.cache_read_tokens} cached token(s); cost {cost}"
    )
    if fresh:
        # Per fresh call, which is the unit that compares across runs of
        # different sizes — and the one a prompt change moves.
        print(
            f"  per fresh call: {result.input_tokens / fresh:.0f} input, "
            f"{result.output_tokens / fresh:.0f} output, "
            f"{result.cache_read_tokens / fresh:.0f} cached"
        )
    if result.cost is None:
        print(
            "  unpriced: AUTOPILOT_INPUT_COST_PER_MTOK and "
            "AUTOPILOT_OUTPUT_COST_PER_MTOK are unset, so this reports tokens "
            "rather than a cost nobody agreed to"
        )
    print(
        f"  {result.duration_ms / 1000:.1f}s"
        + (f"; stopped early: {result.stopped_reason}" if result.stopped_early else "")
    )
    return EXIT_OK


async def _runs(args: argparse.Namespace) -> int:
    """Every run's token bill, newest first.

    What a saving is measured against: the last full replay is on this list,
    and so is the sample run somebody did this morning. Tokens per ticket is
    the column that compares the two, since the runs are deliberately different
    sizes.
    """
    client = await _client(args.client)
    async with session_scope() as session:
        runs = list(
            (
                await session.execute(
                    select(ShadowRun)
                    .where(ShadowRun.client_id == client.id)
                    .order_by(desc(ShadowRun.started_at))
                    .limit(args.limit)
                )
            )
            .scalars()
            .all()
        )
    rows = []
    for run in runs:
        asked = max(run.by_model - run.reused, 0)
        tokens = run.input_tokens + run.output_tokens + run.cache_read_tokens
        rows.append(
            {
                "run_id": str(run.id),
                "started_at": run.started_at.isoformat(),
                "mode": run.mode,
                "model": run.model,
                "prompt_version": run.prompt_version,
                "over": run.config.get("sample") or "full",
                "tickets": run.tickets_seen,
                "by_rule": run.by_rule,
                "fresh": asked,
                "reused": run.reused,
                "input_tokens": run.input_tokens,
                "cache_read_tokens": run.cache_read_tokens,
                "output_tokens": run.output_tokens,
                "tokens": tokens,
                "tokens_per_ticket": tokens / run.tickets_seen if run.tickets_seen else None,
                "cost": float(run.cost) if run.cost is not None else None,
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return EXIT_OK
    if not rows:
        print("no runs for this client")
        return EXIT_OK
    print(
        f"{'started':<17}{'mode':<8}{'over':<10}{'tickets':>8}{'fresh':>7}{'reused':>8}"
        f"{'input':>10}{'cached':>10}{'output':>9}{'tok/tkt':>9}  cost"
    )
    for row in rows:
        cost = "unpriced" if row["cost"] is None else f"${row['cost']:.4f}"
        per = "-" if row["tokens_per_ticket"] is None else f"{row['tokens_per_ticket']:.0f}"
        print(
            f"{row['started_at'][:16]:<17}{row['mode']:<8}{str(row['over'])[:9]:<10}"
            f"{row['tickets']:>8}{row['fresh']:>7}{row['reused']:>8}"
            f"{row['input_tokens']:>10}{row['cache_read_tokens']:>10}"
            f"{row['output_tokens']:>9}{per:>9}  {cost}"
        )
        print(f"    {row['run_id']}  {row['model']} {row['prompt_version']}")
    return EXIT_OK


async def _sample(args: argparse.Namespace) -> int:
    """Draw the iteration sample, or say what the stored one holds."""
    client = await _client(args.client)
    async with session_scope() as session:
        drawn = await sample_for(
            session,
            client.id,
            name=args.name,
            size=args.size,
            days=args.days,
            holdout_days=args.holdout_days,
            created_by=args.user,
            redraw=args.redraw,
        )
    payload = {
        "sample_id": str(drawn.sample_id),
        "created": drawn.created,
        "tickets": drawn.size,
        "tuning": drawn.tuning,
        "test": drawn.test,
        "drawn_from": drawn.drawn_from,
        "by_issue_type": drawn.by_issue_type,
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    print(f"sample {drawn.sample_id} ({'drawn' if drawn.created else 'already drawn'})")
    print(
        f"  {drawn.size} ticket(s) of {drawn.drawn_from} in the window: "
        f"{drawn.tuning} tuning, {drawn.test} test"
    )
    if drawn.by_issue_type:
        for name, count in sorted(drawn.by_issue_type.items(), key=lambda one: -one[1]):
            print(f"    {count:>4}  {name}")
    return EXIT_OK


# --- score --------------------------------------------------------------------------------


def _line(name: str, score: Any) -> str:
    accuracy = "n/a" if score.accuracy is None else f"{100 * score.accuracy:.1f}%"
    coverage = "n/a" if score.coverage is None else f"{100 * score.coverage:.1f}%"
    return (
        f"  {name:16} judged {score.judged:>5} | answered {score.answered:>5} "
        f"| correct {score.correct:>5} | accuracy {accuracy:>7} | coverage {coverage:>7}"
    )


def _print_field(name: str, score: Any) -> None:
    print(_line(name, score))


def _report(card: Any, *, days: int, run: Any, judged: Any = None) -> None:
    """One scorecard, as a person reads it."""
    print(
        f"run {card.run_id} [{card.period}]: {card.tickets} prediction(s), "
        f"{card.errors} invalid, {card.quarantined} quarantined"
        + (" (scored under a label policy applied now)" if card.scored_under_policy else "")
    )
    if card.policy_counts:
        print(f"label policy applied: {dict(card.policy_counts.most_common())}")

    print("\naccuracy and coverage")
    _print_field("issue_type", card.issue_type)
    _print_field("  before policy", card.issue_type_raw)
    _print_field("urgency*", card.urgency)
    print(
        "  * urgency is scored against the FINAL priority, which can have moved since "
        "creation: approximate, and reported rather than gated."
    )

    print("\nthresholds: accuracy and coverage at each floor")
    for floor, score in sorted(card.thresholds.items()):
        _print_field(f">= {floor}", score)
    print("  the same, before the franchisee's label policy:")
    for floor, score in sorted(card.thresholds_raw.items()):
        _print_field(f">= {floor}", score)

    print("\nissue_type by ground-truth label (after policy | before)")
    for name, score in sorted(card.by_issue_type.items(), key=lambda kv: -kv[1].judged):
        before = card.by_issue_type_raw.get(name)
        after_pct = "n/a" if score.accuracy is None else f"{100 * score.accuracy:.1f}%"
        before_pct = (
            "n/a" if before is None or before.accuracy is None else f"{100 * before.accuracy:.1f}%"
        )
        print(
            f"  {name[:34]:34} judged {score.judged:>5} | after {after_pct:>7} "
            f"| before {before_pct:>7}"
        )

    print("\naccuracy by title length (words)")
    for name in ("<=3", "4-6", "7+"):
        score = card.by_title_length.get(name)
        if score is not None:
            _print_field(name, score)

    print("\ncalibration by confidence band")
    for band, score in sorted(card.calibration.items()):
        _print_field(band, score)

    print("\npath")
    for name, score in sorted(card.by_path.items()):
        _print_field(name, score)
    print(f"  rules that fired (whole run): {dict(card.rule_names.most_common())}")

    if card.untyped_predictions:
        print(
            f"\nthe tenant left untyped (excluded from accuracy): "
            f"{sum(card.untyped_predictions.values())}, predicted as "
            f"{dict(card.untyped_predictions.most_common(6))}"
        )

    print("\norigin: no ground truth yet, two PROXIES, neither is a score")
    print(f"  predicted: {dict(card.origin_counts.most_common())}")
    if card.proxy_unassigned:
        print(
            f"  PROXY nobody assigned: {card.proxy_unassigned}, called alert "
            f"{card.proxy_unassigned_alert} "
            f"({100 * card.proxy_unassigned_alert / card.proxy_unassigned:.1f}%)"
        )
    if card.proxy_assigned:
        print(
            f"  PROXY somebody assigned: {card.proxy_assigned}, called human "
            f"{card.proxy_assigned_human} "
            f"({100 * card.proxy_assigned_human / card.proxy_assigned:.1f}%)"
        )
    if card.proxy_alert_queue:
        print(
            f"  PROXY alert-shaped queue: {card.proxy_alert_queue}, called alert "
            f"{card.proxy_alert_queue_alert} "
            f"({100 * card.proxy_alert_queue_alert / card.proxy_alert_queue:.1f}%)"
        )
    if card.proxy_other_queue:
        print(
            f"  PROXY any other queue: {card.proxy_other_queue}, called human "
            f"{card.proxy_other_queue_human} "
            f"({100 * card.proxy_other_queue_human / card.proxy_other_queue:.1f}%)"
        )

    print("\ngrouping")
    print(
        f"  {card.tickets} ticket(s) in, {card.group_leaders} group(s) out, "
        f"{card.grouped} joined an earlier ticket"
    )
    if card.alert_origin:
        print(
            f"  alert-origin tickets grouped: {card.alert_origin_grouped} of "
            f"{card.alert_origin} "
            f"({100 * card.alert_origin_grouped / card.alert_origin:.1f}%)"
        )
    # The spot-check, from the labelling tables rather than a sheet: the page
    # writes them and the gate reads them, so this reads them too.
    if judged is not None and judged.judged:
        share = judged.correct_share or 0.0
        print(
            f"  spot-check: {judged.same} of {judged.judged} judged group(s) are one "
            f"thing ({100 * share:.1f}%); {judged.partly} partly and {judged.not_same} "
            f"not, both counted as wrong; {judged.unjudged} not judged yet"
        )
    elif judged is not None:
        print("  spot-check: no groups judged yet (/autopilot/labelling, groups tab)")

    print("\ncost and latency (the whole run, not the scored period)")
    tokens = (
        f"{card.input_tokens} input + {card.output_tokens} output + {card.cache_read_tokens} cached"
    )
    if card.cost is None:
        print(
            f"  unpriced: {tokens} token(s). Set AUTOPILOT_INPUT_COST_PER_MTOK, "
            "AUTOPILOT_OUTPUT_COST_PER_MTOK and AUTOPILOT_CACHE_READ_COST_PER_MTOK."
        )
    else:
        whole = run.tickets_seen or card.tickets
        print(f"  {tokens} token(s); ${card.cost:.4f} over {whole} ticket(s)")
        print(f"  ${card.cost / whole:.6f} per ticket")
        one = projected_monthly(card, days=days, tickets=whole)
        twenty = projected_monthly(card, days=days, tickets=whole, franchisees=20)
        if one is not None and twenty is not None:
            print(f"  projected monthly: ${one:.2f} for this franchisee, ${twenty:.2f} for 20")
    if card.latency_per_model_call_ms:
        print(
            f"  {card.latency_per_model_call_ms:.0f} ms per model call ({card.model_calls} call(s))"
        )

    if card.confident_errors:
        print("\nthe ten most confident disagreements (ids only)")
        for row in card.confident_errors:
            print(
                f"  ticket {row['ticket_id']} at {row['confidence']:.2f}: said "
                f"{row['predicted']!r}, filed as {row['actual']!r} ({row['path']})"
            )


async def _score(args: argparse.Namespace) -> int:
    client = await _client(args.client)
    async with session_scope() as session:
        run_id = uuid.UUID(args.run) if args.run else None
        if run_id is None:
            latest = await _latest_run(session, client.id)
            if latest is None:
                print("no shadow run for this client yet", file=sys.stderr)
                return EXIT_USAGE
            run_id = latest.id
        # The franchisee's saved label policy, re-derived from the raw answers
        # rather than re-asked: that is what makes a policy chosen on the
        # development period scoreable on the holdout for nothing.
        policy = None
        if args.apply_policy:
            franchisee = await load_config(session, client.id, client_name=client.name)
            policy = franchisee.autopilot
        card = await score_run(
            session,
            run_id,
            client_id=client.id,
            period=args.period,
            holdout_days=args.holdout_days,
            policy=policy,
        )
        judged = await group_judgements(session, client_id=client.id, run_id=run_id)
        run = await session.get(ShadowRun, run_id)
        days = int((run.config or {}).get("days", 90)) if run else 90
        against: Any = None
        if args.variant_of:
            # The variant is scored on the same period as the headline, and the
            # lift is per field: an input that only makes the agent bolder
            # shows up as coverage, not accuracy.
            baseline = await score_run(
                session,
                uuid.UUID(args.variant_of),
                client_id=client.id,
                period=args.period,
                holdout_days=args.holdout_days,
                policy=policy,
            )
            against = lift(baseline, card)

    if args.json:
        payload = card.as_json()
        if against is not None:
            payload["lift_over"] = {"run_id": args.variant_of, **against}
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK

    _report(card, days=days, run=run, judged=judged)
    if against is not None:
        print(f"\nlift over run {args.variant_of} on the same period")
        for field_name in ("issue_type", "urgency"):
            numbers = against[field_name]
            accuracy = numbers["accuracy_lift"]
            coverage = numbers["coverage_lift"]
            print(
                f"  {field_name:12} accuracy "
                f"{'n/a' if accuracy is None else f'{100 * accuracy:+.1f} points'}"
                f" | coverage "
                f"{'n/a' if coverage is None else f'{100 * coverage:+.1f} points'}"
                f" | answered {numbers['baseline_answered']} -> {numbers['variant_answered']}"
            )
        print(
            f"  origin called alert: {against['origin_alert_called']['baseline']} -> "
            f"{against['origin_alert_called']['variant']}; unsure "
            f"{against['origin_unsure']['baseline']} -> {against['origin_unsure']['variant']}"
        )
    return EXIT_OK


# --- the forward shadow: a backfill, and one pass by hand ----------------------------------

#: What one ticket costs the request budget on a live pass: the enrichment asks
#: each connector that holds the machine. An over-estimate on purpose — a run
#: that stops halfway through a ticket leaves a prediction with no enrichment,
#: and a half-enriched ticket is worse than one nobody got to.
READS_PER_TICKET = len(LIVE_CONNECTORS)


def _refused_live() -> int:
    """`AUTOPILOT_SHADOW_LIVE` is false, said in words rather than by failing."""
    print(
        "AUTOPILOT_SHADOW_LIVE is false, so the forward shadow does not read a vendor.\n"
        "Nothing was called and nothing was written. Turn it on deliberately in settings;\n"
        "the replay needs no vendor and is unaffected.",
        file=sys.stderr,
    )
    return EXIT_USAGE


def _say_wait(attempt: int, waited: float, error: Exception) -> None:
    """A wait the terminal can see, so a backed-off run does not look hung."""
    print(
        f"  attempt {attempt} failed ({type(error).__name__}); waiting {waited:.0f}s",
        file=sys.stderr,
    )


async def _already_suggested(client_id: uuid.UUID) -> set[str]:
    """Ticket ids this client already holds a suggestion for.

    What makes a backfill resumable: run it again and it carries on from where
    it stopped, because a ticket that has been through the agent is not sent
    through it a second time. `--redo` is how you ask for the opposite.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(TicketSuggestion.ticket_id).where(TicketSuggestion.client_id == client_id)
            )
        ).scalars()
        return set(rows)


async def _shown_above_threshold(run_id: uuid.UUID) -> int:
    """Suggestions from this run an engineer will actually be offered."""
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(TicketSuggestion).where(TicketSuggestion.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
    return sum(1 for row in rows if has_anything_to_show(row))


async def _tool_reads(run_id: uuid.UUID) -> int:
    """Vendor reads the enrichment made, from the snapshots it wrote."""
    async with session_scope() as session:
        total = (
            await session.execute(
                select(func.coalesce(func.sum(ShadowEnrichment.reads), 0)).where(
                    ShadowEnrichment.run_id == run_id
                )
            )
        ).scalar_one()
    return int(total)


async def _report_pass(args: argparse.Namespace, result: Any, budget: RequestBudget) -> None:
    """What a live pass did, in the same shape for both commands."""
    shown = await _shown_above_threshold(result.run_id)
    reads = await _tool_reads(result.run_id)
    if args.json:
        print(
            json.dumps(
                {
                    "run_id": str(result.run_id),
                    "tickets": result.tickets_seen,
                    "suggested": result.suggested,
                    "shown": shown,
                    "by_rule": result.by_rule,
                    "by_model": result.by_model,
                    "errors": result.errors,
                    "enriched": result.enriched,
                    "no_device": result.no_device,
                    "wrong_site": result.wrong_site,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cache_read_tokens": result.cache_read_tokens,
                    "cost": result.cost,
                    "autotask_requests": budget.spent,
                    "tool_reads": reads,
                    "budget_exhausted": result.budget_exhausted,
                },
                indent=2,
            )
        )
        return
    print(f"run {result.run_id}")
    print(
        f"  {result.tickets_seen} ticket(s) processed: {result.by_rule} by rule, "
        f"{result.by_model} by model, {result.errors} invalid"
    )
    print(
        f"  {result.suggested} suggestion(s) written, {shown} of them above the "
        f"display threshold and shown to an engineer"
    )
    print(
        f"  {result.enriched} enrichment(s): {result.no_device} resolved no machine, "
        f"{result.wrong_site} on another customer's machine"
    )
    tokens = (
        f"{result.input_tokens} input + {result.output_tokens} output + "
        f"{result.cache_read_tokens} cached"
    )
    cost = f"${result.cost:.2f}" if result.cost is not None else "not priced"
    print(f"  model: {tokens} token(s), {cost}")
    print(f"  Autotask: {budget.spent} request(s) of {budget.per_hour} this hour")
    print(f"  tools: {reads} read(s) across every connector the enrichment asked")
    if result.budget_exhausted:
        print("  stopped: the hourly request budget ran out. Run it again next hour.")


async def _live_pass(
    args: argparse.Namespace,
    client: Client,
    *,
    tickets: list[NewTicket] | None,
    since: dt.datetime,
    now: dt.datetime,
    budget: RequestBudget,
) -> int:
    """One forward pass, with the tickets either handed in or read by the source.

    `tickets` is the backfill's filtered list; `None` means "ask the source",
    which is what a polling pass does.
    """
    settings = get_settings()
    context = _context(client, args.user)
    try:
        model_client = get_model_client()
    except Exception as exc:  # no key, no run: say so rather than half-running
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    async with session_scope() as session:
        issue_types = await issue_types_for(session, client.id)
        franchisee = await load_config(session, client.id, client_name=client.name)

    async def handed_in(_: dt.datetime) -> list[NewTicket]:
        return list(tickets or [])

    try:
        async with session_scope() as session:
            source = (
                handed_in if tickets is not None else autotask_source(session, context, client.id)
            )
            result = await forward_pass(
                session,
                context,
                client.id,
                model_client=model_client,
                settings=settings,
                source=source,
                enricher=workspace_enricher(session, context, client.id),
                issue_types=issue_types,
                since=since,
                policy=franchisee.autopilot,
                budget=budget,
                now=now,
            )
    except NotEnabled:
        return _refused_live()
    except Exception as exc:
        # On the record in its own transaction, as every failed read is.
        async with session_scope() as session:
            await record_event(
                session,
                context,
                event_type=AuditEventType.AUTOPILOT_RUN_FAILED,
                outcome=AuditOutcome.ERROR,
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
                payload={"mode": "forward", "command": args.func.__name__.strip("_")},
            )
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    await _report_pass(args, result, budget)
    return EXIT_OK


async def _backfill(args: argparse.Namespace) -> int:
    """The open queue, once, through the same pipeline that polling uses.

    A backfill is not catching up on what a poller missed: it is the work
    sitting in the queue today, whenever it arrived, because that is what an
    engineer opens next and the point of a suggestion is to be there when they
    do.
    """
    settings = get_settings()
    if not settings.autopilot_shadow_live:
        return _refused_live()
    client = await _client(args.client)
    context = _context(client, args.user)
    now = dt.datetime.now(dt.UTC)
    budget = RequestBudget(per_hour=settings.autopilot_poll_requests_per_hour)

    if not budget.spend():
        print("no request budget left this hour", file=sys.stderr)
        return EXIT_FAILED
    async with session_scope() as session:
        read = autotask_open_tickets(session, context, client.id)
        try:
            found: list[NewTicket] = await with_backoff(
                lambda: read(now), backoff=Backoff(), on_wait=_say_wait
            )
        except Exception as exc:
            print(f"Autotask did not answer: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_FAILED
    print(f"{len(found)} open ticket(s) at Autotask")

    # --- which of them -------------------------------------------------------
    asked = {one.strip() for one in (args.ticket or []) if one.strip()}
    if asked:
        held = {one.ticket_number for one in found} | {one.ticket_id for one in found}
        missing = sorted(asked - held)
        if missing:
            # Named back, because a number that is not open is usually a typo
            # or a ticket somebody has already closed, and silence about it
            # reads as "done".
            print(f"  not open, so not processed: {', '.join(missing)}", file=sys.stderr)
        found = [one for one in found if one.ticket_number in asked or one.ticket_id in asked]
    done: set[str] = set() if args.redo else await _already_suggested(client.id)
    carried = [one for one in found if one.ticket_id in done]
    if carried:
        print(f"  {len(carried)} already suggested on, so skipped (--redo to repeat them)")
    found = [one for one in found if one.ticket_id not in done]
    if args.limit is not None:
        found = found[: args.limit]

    # --- what the budget allows ----------------------------------------------
    # One request is kept back for the pass's own listing call, which it makes
    # whether or not the tickets were handed to it.
    affordable = max(0, (budget.remaining - 1) // READS_PER_TICKET)
    if len(found) > affordable:
        print(
            f"  the hourly budget allows {affordable} more ticket(s) at "
            f"{READS_PER_TICKET} read(s) each; the rest wait for the next hour"
        )
        found = found[:affordable]

    if not found:
        print("nothing to process")
        return EXIT_OK

    if args.dry_run:
        print(f"\nwould process {len(found)} ticket(s):")
        for one in found:
            # Ids and numbers only. A ticket's words are not something a
            # terminal needs (CLAUDE.md, working conventions).
            print(f"  {one.ticket_number or one.ticket_id}")
        print(
            f"\nAutotask requests: 1 listing already used, 1 more for the pass's own "
            f"listing, and up to {len(found) * READS_PER_TICKET} for the enrichment "
            f"({READS_PER_TICKET} per ticket, one per connector that holds the machine) — "
            f"at most {1 + 1 + len(found) * READS_PER_TICKET} of {budget.per_hour} this hour."
        )
        print("no model call was made.")
        return EXIT_OK

    return await _live_pass(
        args, client, tickets=found, since=now - dt.timedelta(days=365), now=now, budget=budget
    )


async def _forward(args: argparse.Namespace) -> int:
    """One polling pass, by hand.

    There is no poller process: nothing in the application calls this on a
    schedule, and `--once` is the only mode there is. A schedule is a cron
    service running this command, like the daily collector's (`render.yaml`).
    """
    settings = get_settings()
    if not settings.autopilot_shadow_live:
        return _refused_live()
    if not args.once:
        print(
            "Only --once is implemented: nothing here loops, and nothing in the\n"
            "application polls on its own. A schedule is this command on a cron.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    client = await _client(args.client)
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        last = (
            await session.execute(
                select(ShadowRun.started_at)
                .where(ShadowRun.client_id == client.id, ShadowRun.mode == "forward")
                .order_by(desc(ShadowRun.started_at))
                .limit(1)
            )
        ).scalar_one_or_none()
    # From the last pass, or from `--since-hours` back when there has never
    # been one. Never from "the beginning": that is what the backfill is for,
    # and it is the command that counts the cost of it first.
    since = last or (now - dt.timedelta(hours=args.since_hours))
    print(f"reading tickets created since {since:%Y-%m-%d %H:%M} UTC")
    budget = RequestBudget(per_hour=settings.autopilot_poll_requests_per_hour)
    return await _live_pass(args, client, tickets=None, since=since, now=now, budget=budget)


# --- the sheets ---------------------------------------------------------------------------


async def _sheets(args: argparse.Namespace) -> int:
    client = await _client(args.client)
    async with session_scope() as session:
        origin = await write_origin_sheet(
            session, client.id, path=VAR / f"{client.slug}-origin-labels.csv", wanted=args.labels
        )
        print(
            f"origin sheet: {origin.rows} row(s) over {origin.sites} site(s) and "
            f"{origin.issue_types} issue type(s) -> {origin.path}"
        )
        run_id: uuid.UUID | None = uuid.UUID(args.run) if args.run else None
        if run_id is None:
            run = await _latest_run(session, client.id)
            run_id = run.id if run else None
        if run_id is None:
            print("no shadow run yet, so no groups to spot-check", file=sys.stderr)
            return EXIT_OK
        groups = await write_group_sheet(
            session,
            client.id,
            run_id,
            path=VAR / f"{client.slug}-groups-{str(run_id)[:8]}.csv",
            wanted=args.groups,
        )
        print(f"group sheet: {groups.issue_types} group(s), {groups.rows} row(s) -> {groups.path}")
    print("\nBoth files hold ticket titles and live under var/, which is git-ignored.")
    print("Fill in the origin sheet's `origin` column with alert, human or unsure, then:")
    print(
        f"  python scripts/autopilot.py shadow score-origin --client {client.slug} "
        f"--sheet {origin.path}"
    )
    return EXIT_OK


async def _score_origin(args: argparse.Namespace) -> int:
    """Score `origin` against what people judged.

    The labelling tables are the source now: the page writes them, the gate
    reads them, and a second copy of the same judgements in a spreadsheet would
    be a second answer to the same question. `--sheet` stays for the sheets
    filled in before the page existed, and says which source it used either
    way, because a figure from 88 labels and a figure from a stale CSV are not
    the same figure.
    """
    client = await _client(args.client)
    sheet: SheetRead | None = None
    if args.sheet:
        try:
            sheet = await asyncio.to_thread(read_labels, pathlib.Path(args.sheet))
        except OSError as exc:
            print(f"could not read {args.sheet}: {type(exc).__name__}", file=sys.stderr)
            return EXIT_USAGE
    async with session_scope() as session:
        if args.run:
            run_id = uuid.UUID(args.run)
        else:
            run = await _latest_run(session, client.id)
            if run is None:
                print("no shadow run for this client yet", file=sys.stderr)
                return EXIT_USAGE
            run_id = run.id
        if sheet is None:
            stored = await origin_labels(session, client_id=client.id, run_id=run_id)
            if not stored.by_ticket:
                print(
                    "no labels stored for this run yet. Label it on "
                    "/autopilot/labelling, or pass --sheet for an older filled-in sheet.",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            # A ticket two labellers disagreed about is not ground truth and is
            # not in `by_ticket`; it is counted so the source line can say so.
            sheet = SheetRead(
                labels=sorted(stored.by_ticket.items()),
                blank=stored.unlabelled + stored.skipped,
                unreadable=stored.disagreed,
            )
            print(
                f"source: the labelling tables — {stored.labelled} labelled, "
                f"{stored.skipped} skipped, {stored.unlabelled} not reached, "
                f"{stored.disagreed} left out because two labellers disagreed"
            )
        else:
            print(f"source: the sheet at {args.sheet}")
        score = await score_origin(session, run_id, sheet=sheet)
    if args.json:
        print(
            json.dumps(
                {
                    "labelled": score.labelled,
                    "unlabelled": score.unlabelled,
                    "unreadable": score.unreadable,
                    "missing_prediction": score.missing_prediction,
                    "agree": score.agree,
                    "alert_precision": score.alert_precision,
                    "alert_recall": score.alert_recall,
                    "confusion": score.confusion,
                },
                indent=2,
            )
        )
        return EXIT_OK
    print(
        f"{score.labelled} labelled row(s) scored "
        f"({score.unlabelled} blank, {score.unreadable} unreadable, "
        f"{score.missing_prediction} with no prediction)"
    )
    if score.labelled:
        print(
            f"  agreement: {score.agree} of {score.labelled} "
            f"({100 * score.agree / score.labelled:.1f}%)"
        )
    precision = score.alert_precision
    recall = score.alert_recall
    print(
        f"  alert precision: {'n/a' if precision is None else f'{100 * precision:.1f}%'} "
        f"(the gate asks for 95%)"
    )
    print(f"  alert recall:    {'n/a' if recall is None else f'{100 * recall:.1f}%'}")
    print(f"  predicted -> labelled: {score.confusion}")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="group", required=True)
    shadow = sub.add_parser("shadow", help="Level 0: predict and be scored").add_subparsers(
        dest="command", required=True
    )

    common = {"--client": "client slug or UUID"}

    replay_parser = shadow.add_parser("replay", help="replay stored tickets through the agent")
    replay_parser.add_argument("--client", required=True, help=common["--client"])
    replay_parser.add_argument("--days", type=int, default=90)
    replay_parser.add_argument(
        "--limit", type=int, default=None, help="stop after this many tickets; try 50 first"
    )
    replay_parser.add_argument(
        "--batch",
        type=int,
        default=100,
        help="predictions committed at a time (default 100). A run that holds one "
        "transaction to the end cannot be watched and loses everything if interrupted",
    )
    replay_parser.add_argument(
        "--resume",
        help="carry on an existing run by its UUID, answering only the tickets it "
        "has no prediction for",
    )
    replay_parser.add_argument(
        "--variant",
        choices=["title_site_time", "with_alert_queue"],
        default="title_site_time",
        help="what the agent is shown. The default withholds the queue, whose stored value "
        "is read at completion and so may carry what happened; the variant adds a "
        "raised-into-an-alert-queue flag so its worth can be measured",
    )
    replay_parser.add_argument(
        "--batched",
        action="store_true",
        help="send this run through the Message Batches API: about half the price, "
        "answered within hours. The run is left open with its batch id on a row; "
        "run the same command again to collect it. Also AUTOPILOT_BATCH_REPLAY",
    )
    replay_parser.add_argument(
        "--model",
        default=None,
        help="which model to run as (default: AUTOPILOT_MODEL, else ANTHROPIC_MODEL). "
        "Stored on the run, and never reused across models: an answer from one is "
        "not an answer from another",
    )
    replay_parser.add_argument(
        "--no-reuse",
        action="store_true",
        help="ask the model again even where this client already holds the answer to "
        "an identical request. For proving the two paths agree, and for nothing else",
    )
    replay_parser.add_argument(
        "--sample",
        default=ITERATION,
        help="the stored sample to replay (default: the iteration sample, drawn once "
        "and reused). A fifth of the tickets for a twentieth of the tokens",
    )
    replay_parser.add_argument(
        "--full",
        action="store_true",
        help="replay the whole window instead of a sample. The honest number comes "
        "from this; the sample is what you iterate against on the way to it",
    )
    replay_parser.set_defaults(func=_replay)

    sample_parser = shadow.add_parser(
        "sample", help="draw or show the fixed set of tickets iteration runs against"
    )
    sample_parser.add_argument("--client", required=True, help=common["--client"])
    sample_parser.add_argument("--name", default=ITERATION)
    sample_parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    sample_parser.add_argument("--days", type=int, default=90)
    sample_parser.add_argument(
        "--holdout-days",
        type=int,
        default=30,
        help="where the tuning and test halves split, measured back from the newest "
        "ticket, exactly as scoring measures it (default 30)",
    )
    sample_parser.add_argument(
        "--redraw",
        action="store_true",
        help="replace the stored sample. Runs either side of a redraw measured "
        "different tickets and are not comparable, so this is deliberate",
    )
    sample_parser.set_defaults(func=_sample)

    runs_parser = shadow.add_parser("runs", help="every run's token bill, newest first")
    runs_parser.add_argument("--client", required=True, help=common["--client"])
    runs_parser.add_argument("--limit", type=int, default=15)
    runs_parser.set_defaults(func=_runs)

    score_parser = shadow.add_parser("score", help="score a run against what happened")
    score_parser.add_argument("--client", required=True, help=common["--client"])
    score_parser.add_argument("--run", help="run UUID; the latest run by default")
    score_parser.add_argument(
        "--period",
        choices=["all", "development", "holdout"],
        default="holdout",
        help="which stretch of the window to score. `development` is where a label "
        "policy or a threshold may be chosen; `holdout` is what a gate is read from, "
        "and is the default so a number is not quoted off the tickets it was tuned on",
    )
    score_parser.add_argument(
        "--holdout-days",
        type=int,
        default=30,
        help="how many days at the end of the window are the holdout (default 30)",
    )
    score_parser.add_argument(
        "--apply-policy",
        action="store_true",
        help="score under the franchisee's saved label policy, re-deriving each answer "
        "from the raw label the model gave. The stored rows are untouched: this is how a "
        "policy chosen on the development period is scored on the holdout without asking "
        "the model anything again",
    )
    score_parser.add_argument(
        "--variant-of",
        help="score this run as a variant of that one: the lift per field, on the same period",
    )
    score_parser.set_defaults(func=_score)

    sheets_parser = shadow.add_parser("sheets", help="export the two sheets a person reads")
    sheets_parser.add_argument("--client", required=True, help=common["--client"])
    sheets_parser.add_argument("--run", help="run UUID; the latest run by default")
    sheets_parser.add_argument("--labels", type=int, default=200)
    sheets_parser.add_argument("--groups", type=int, default=30)
    sheets_parser.set_defaults(func=_sheets)

    origin_parser = shadow.add_parser(
        "score-origin", help="score origin against what people judged"
    )
    origin_parser.add_argument("--client", required=True, help=common["--client"])
    origin_parser.add_argument(
        "--sheet",
        default=None,
        help="a filled-in sheet to score instead of the stored labels (the fallback "
        "for sheets filled in before the labelling page existed)",
    )
    origin_parser.add_argument("--run", help="run UUID; the latest run by default")
    origin_parser.set_defaults(func=_score_origin)

    backfill_parser = shadow.add_parser("backfill", help="suggest on every open ticket, once")
    backfill_parser.add_argument("--client", required=True)
    backfill_parser.add_argument(
        "--limit", type=int, default=None, help="only the first N open tickets"
    )
    backfill_parser.add_argument(
        "--ticket",
        action="append",
        default=None,
        help="one ticket number, repeatable; only these are processed",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="say which tickets and how many vendor requests, and call no model",
    )
    backfill_parser.add_argument(
        "--redo",
        action="store_true",
        help="process tickets that already have a suggestion (by default they are skipped, "
        "which is what makes this resumable)",
    )
    backfill_parser.set_defaults(func=_backfill)

    forward_parser = shadow.add_parser("forward", help="one polling pass, by hand")
    forward_parser.add_argument("--client", required=True)
    forward_parser.add_argument(
        "--once", action="store_true", help="the only mode: nothing here loops"
    )
    forward_parser.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="how far back to read when this client has never had a forward pass",
    )
    forward_parser.set_defaults(func=_forward)

    for one in (
        replay_parser,
        sample_parser,
        runs_parser,
        score_parser,
        sheets_parser,
        origin_parser,
        backfill_parser,
        forward_parser,
    ):
        one.add_argument("--user", default="autopilot-cli", help="who to attribute the run to")
        one.add_argument("--json", action="store_true")
    return parser


async def _main(args: argparse.Namespace) -> int:
    try:
        result: int = await args.func(args)
    except UnknownClient as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    return result


def main() -> int:
    return asyncio.run(_main(_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())

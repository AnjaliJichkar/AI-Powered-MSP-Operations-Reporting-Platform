#!/usr/bin/env python
"""Does the model reach for the right tool first?

    python scripts/selection_check.py --client teamlogic-austin
    python scripts/selection_check.py --client teamlogic-austin --site acme-dental
    python scripts/selection_check.py --client teamlogic-austin --json > selection.json
    python scripts/selection_check.py --client teamlogic-austin --execute --limit 3

Reads questions and the tool each should lead to from
`tests/selection/questions.yaml`, runs each through the real agent
(`run_agent`: the current system prompt, the full tool list, the client's
site list) and reports the first tool the model asked for, whether it matched,
and the latency and token counts per question. It ends with one line:

    N of M correct at T tools

which is the number to watch as tools are added. Exit status is 0 when at
least nine in ten were right, 3 otherwise, 2 for a usage error.

What it does and does not touch:

- **No tool is executed and no vendor is called** by default. The run stops as
  soon as the model has named its tools (`run_agent(select_only=True)`), so the
  latency is the time to choose, and running it fifty times costs the vendors
  nothing. `--execute` runs each question to a full answer instead — real
  vendor reads, real answer latency — for when that is what you want to see.
- **It calls the model**, so it costs tokens and needs `ANTHROPIC_API_KEY`.
- **It is audited, and marked.** Hard rule 4 has no exception for a harness:
  every prompt and model response lands in `audit_log`. These rows carry
  `interface = 'eval'` and the actor `selection-check`, which no real entry
  point can produce, so normal traffic is `WHERE interface <> 'eval'`.
- **Safe to repeat.** Each question is its own short thread, owned by the
  harness actor (never shown to an engineer) and archived as soon as it is
  scored. Nothing is deleted, nothing is reused between runs, and one question
  failing does not stop the rest.

Web search stays as production has it. If the model searches the web before
it checks the client's own tools, that is a selection failure worth seeing, and
it is reported as `web_search` being called first. A search is server-side, so
it has already run by the time the check stops; it reads nothing of the
client's, and its query is audited like any other.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from app.agent.conversations import archive_conversation, get_conversation
from app.agent.loop import ModelNotConfigured, run_agent
from app.agent.prompts import SYSTEM_PROMPT_VERSION
from app.agent.registry import registered_tools
from app.agent.selection import (
    QuestionFileError,
    SelectionOutcome,
    SelectionQuestion,
    parse_questions,
    score,
    summarise,
)
from app.connectors.errors import UnknownClient
from app.db.session import dispose_engine, session_scope
from app.models import Client, Site
from app.models.enums import ConnectorName, Interface
from app.settings import get_settings

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BELOW_THRESHOLD = 3

ACTOR = "selection-check"
DEFAULT_QUESTIONS = (
    Path(__file__).resolve().parent.parent / "tests" / "selection" / "questions.yaml"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="selection_check",
        description="Score which tool the model calls first for a file of questions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--client", required=True, help="client slug or UUID")
    parser.add_argument(
        "--questions",
        type=Path,
        default=DEFAULT_QUESTIONS,
        help="YAML file of questions (default: tests/selection/questions.yaml)",
    )
    parser.add_argument(
        "--site",
        help="site slug to substitute for {site} in questions that need one; "
        "without it those questions are skipped",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run every question to a full answer: real vendor reads, slower, "
        "same scoring. Default stops before any tool runs.",
    )
    parser.add_argument("--limit", type=int, help="only the first N questions")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    parser.add_argument("--verbose", action="store_true", help="show engine logging")
    return parser


async def _resolve(reference: str) -> tuple[uuid.UUID, str, set[str]]:
    """Client id, slug and its active site slugs; lists the clients when none matches."""
    async with session_scope() as session:
        try:
            parsed: uuid.UUID | None = uuid.UUID(reference)
        except ValueError:
            parsed = None
        query = (
            select(Client).where(Client.id == parsed)
            if parsed is not None
            else select(Client).where(Client.slug == reference)
        )
        client = (await session.execute(query)).scalar_one_or_none()
        if client is None:
            known = (
                (await session.execute(select(Client.slug).order_by(Client.slug))).scalars().all()
            )
            raise SystemExit(
                f"No client matches {reference!r}.\n"
                f"Clients in this database: {', '.join(known) or 'none configured'}"
            )
        slugs = (
            (
                await session.execute(
                    select(Site.slug).where(Site.client_id == client.id, Site.is_active.is_(True))
                )
            )
            .scalars()
            .all()
        )
        return client.id, client.slug, set(slugs)


def load_questions(path: Path) -> list[SelectionQuestion]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"No question file at {path}") from None
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path} is not valid YAML: {exc}") from None
    try:
        return parse_questions(data, known_tools=[tool.name for tool in registered_tools()])
    except QuestionFileError as exc:
        raise SystemExit(f"{path}: {exc}") from None


async def _archive(thread_id: uuid.UUID | None, client_slug: str) -> None:
    """Close the harness's thread. Archived, not deleted; failure here is not a result."""
    if thread_id is None:
        return
    try:
        async with session_scope() as session:
            thread = await get_conversation(session, thread_id)
            await archive_conversation(
                session, thread, actor_id=ACTOR, client_ref=client_slug, interface=Interface.EVAL
            )
    except Exception:
        logging.getLogger(__name__).exception("could not archive harness thread %s", thread_id)


async def ask(
    question: SelectionQuestion,
    *,
    client_id: uuid.UUID,
    client_slug: str,
    site: str | None,
    execute: bool,
    settings: Any,
) -> SelectionOutcome:
    outcome = SelectionOutcome(question=question.text_for(site), expect=question.expect)
    if question.needs_site and not site:
        outcome.skipped = "needs a site: pass --site"
        return outcome
    try:
        result = await run_agent(
            user_id=ACTOR,
            client_id=client_id,
            message=outcome.question,
            interface=Interface.EVAL,
            settings=settings,
            select_only=not execute,
        )
    except ModelNotConfigured:
        raise
    except Exception as exc:  # one bad question must not cost the other results
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome

    first, matched = score(question, result.first_tools)
    outcome.first_tool = first
    outcome.also_requested = list(result.first_tools[1:])
    outcome.matched = matched
    outcome.status = result.status
    outcome.duration_ms = result.duration_ms
    outcome.input_tokens = result.input_tokens
    outcome.cache_read_tokens = result.cache_read_tokens
    outcome.cache_write_tokens = result.cache_write_tokens
    outcome.output_tokens = result.output_tokens
    outcome.audit_log_id = result.audit_log_id
    if result.status == "error":
        outcome.error = result.text
    await _archive(result.conversation_id, client_slug)
    return outcome


def _render(outcomes: list[SelectionOutcome]) -> None:
    for number, outcome in enumerate(outcomes, start=1):
        if outcome.skipped:
            print(f"{number:>3}. SKIP  {outcome.question}\n       {outcome.skipped}")
            continue
        mark = "ok  " if outcome.matched else "MISS"
        print(f"{number:>3}. {mark}  {outcome.question}")
        if outcome.error:
            print(f"       error: {outcome.error}")
            continue
        called = outcome.first_tool or "(answered without calling a tool)"
        extra = f"  (+ {', '.join(outcome.also_requested)})" if outcome.also_requested else ""
        print(f"       called first: {called}{extra}")
        if not outcome.matched:
            print(f"       expected:     {' or '.join(outcome.expect)}")
        print(
            f"       {outcome.duration_ms / 1000:.1f}s   read {outcome.tokens_read} tokens "
            f"({outcome.input_tokens} new, {outcome.cache_read_tokens} cached, "
            f"{outcome.cache_write_tokens} cache-write)   wrote {outcome.output_tokens}"
            f"   audit_log {outcome.audit_log_id}"
        )


async def _main(args: argparse.Namespace) -> int:
    questions = load_questions(args.questions)
    if args.limit is not None:
        questions = questions[: max(args.limit, 0)]
    tools = registered_tools()
    settings = get_settings()

    try:
        client_id, client_slug, site_slugs = await _resolve(args.client)
        if args.site and args.site not in site_slugs:
            raise SystemExit(f"{args.site!r} is not an active site of {client_slug}.")
        outcomes: list[SelectionOutcome] = []
        for question in questions:  # one at a time: comparable latencies, gentle on rate limits
            outcomes.append(
                await ask(
                    question,
                    client_id=client_id,
                    client_slug=client_slug,
                    site=args.site,
                    execute=args.execute,
                    settings=settings,
                )
            )
    except (UnknownClient, ModelNotConfigured) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    finally:
        await dispose_engine()

    summary = summarise(outcomes, tool_count=len(tools))
    per_connector = {
        connector.value: sum(1 for tool in tools if tool.connector is connector)
        for connector in ConnectorName
        if any(tool.connector is connector for tool in tools)
    }
    if args.json:
        print(
            json.dumps(
                {
                    "client": client_slug,
                    "model": settings.anthropic_model,
                    "prompt_version": SYSTEM_PROMPT_VERSION,
                    "mode": "execute" if args.execute else "select_only",
                    "tool_count": summary.tool_count,
                    "tools_per_connector": per_connector,
                    "correct": summary.correct,
                    "asked": summary.asked,
                    "skipped": summary.skipped,
                    "errors": summary.errors,
                    "passed": summary.passed,
                    "summary": summary.line(),
                    "results": [outcome.as_dict() for outcome in outcomes],
                },
                indent=2,
            )
        )
    else:
        print(
            f"client={client_slug}  model={settings.anthropic_model}  "
            f"prompt={SYSTEM_PROMPT_VERSION}  "
            f"mode={'execute' if args.execute else 'select-only (no tool is run)'}\n"
        )
        _render(outcomes)
        breakdown = ", ".join(f"{name} {count}" for name, count in per_connector.items())
        notes = [f"{summary.skipped} skipped"] if summary.skipped else []
        notes += [f"{summary.errors} errored"] if summary.errors else []
        print(f"\n{summary.line()} ({breakdown})" + (f"; {', '.join(notes)}" if notes else ""))
    return EXIT_OK if summary.passed else EXIT_BELOW_THRESHOLD


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_main(args))
    except SystemExit as error:
        if isinstance(error.code, int):
            raise
        print(str(error), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())

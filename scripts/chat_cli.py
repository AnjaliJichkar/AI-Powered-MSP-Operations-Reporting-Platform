#!/usr/bin/env python
"""Ask Desk one question from the terminal.

    python scripts/chat_cli.py --client teamlogic-austin "what hasn't checked in for 2 weeks?"
    python scripts/chat_cli.py --client teamlogic-austin --json "what is alerting?"

Runs the same `run_agent` the web UI and Slack will use, so what you see here is
what an engineer will get. The run is written to `audit_log` with
`interface=cli`, and the row id is printed so you can rate it or read it back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Any

from sqlalchemy import select

from app.agent.loop import STATUS_OK, run_agent
from app.connectors.errors import UnknownClient
from app.db.session import dispose_engine, session_scope
from app.models import Client
from app.models.enums import Interface

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_OK = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chat_cli",
        description="Ask Desk one question about one client.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("question", nargs="*", help="the question, in plain English")
    parser.add_argument("--client", required=True, help="client slug or UUID")
    parser.add_argument(
        "--user",
        default=os.environ.get("USERNAME") or os.environ.get("USER") or "cli",
        help="your handle, recorded in audit_log (default: your OS username)",
    )
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--quiet", action="store_true", help="print only the answer text")
    parser.add_argument("--verbose", action="store_true", help="show engine logging")
    return parser


async def _resolve(reference: str) -> uuid.UUID:
    """Accept a slug or a UUID; list what exists when neither matches."""
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
            available = ", ".join(known) or "none configured"
            raise SystemExit(
                f"No client matches {reference!r}.\nClients in this database: {available}"
            )
        return client.id


def _render(result: Any, quiet: bool) -> None:
    if quiet:
        print(result.text)
        return

    print(f"\n{'=' * 72}")
    print(result.text)
    print("=" * 72)

    if result.tool_calls:
        print("\nTool calls:")
        for call in result.tool_calls:
            arguments = ", ".join(f"{k}={v}" for k, v in call.arguments.items())
            detail = call.result_summary or call.error_message or ""
            print(f"  [{call.outcome}] {call.tool_name}({arguments})  {call.duration_ms}ms")
            if detail:
                print(f"      {detail}")
    else:
        print("\nNo tools were called.")

    print(
        f"\nclient={result.client_slug}  status={result.status}  model={result.model}  "
        f"prompt={result.prompt_version}"
    )
    print(
        f"{result.model_calls} model call(s), {result.input_tokens} in / "
        f"{result.output_tokens} out tokens, {result.duration_ms / 1000:.1f}s"
    )
    print(f"audit_log id: {result.audit_log_id}")


async def _main(args: argparse.Namespace) -> int:
    question = " ".join(args.question).strip()
    if not question:
        raise SystemExit('Ask a question, e.g.: chat_cli.py --client acme "what is alerting?"')

    try:
        client_id = await _resolve(args.client)
        result = await run_agent(
            user_id=args.user,
            client_id=client_id,
            message=question,
            interface=Interface.CLI,
        )
    except UnknownClient as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    finally:
        await dispose_engine()

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))
    else:
        _render(result, args.quiet)

    return EXIT_OK if result.status == STATUS_OK else EXIT_NOT_OK


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
    raise SystemExit(main())

#!/usr/bin/env python
"""The automation worker: executes approved runs, in the background.

    python scripts/automation_worker.py --once
    python scripts/automation_worker.py --loop --every 5

Nothing in the web service runs a step. A page drafts, dry-runs, submits and
approves; this process picks up what is approved and does it, one run at a
time per client, one step per transaction, and lets go when the run completes,
a step fails, or the client's kill switch holds it between steps.

It survives a restart. A run a dead worker left mid-step is taken up again
from that step by the next worker — which is why a step's action has to be
idempotent for one (run, step); see `app/automation/definitions.py`.

`--once` does one piece of work and exits, which is what a test or a cron
service wants. `--loop` keeps going, sleeping `--every` seconds when there is
nothing to do, which is what a long-lived service wants. Either way every
action it takes is an `audit_log` row written as `automation-worker`.

No workflow here writes to a client's tool yet.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.automation import practice  # noqa: F401  registers the built-in workflow
from app.automation.runner import run_once
from app.db.session import dispose_engine, get_sessionmaker

EXIT_OK, EXIT_USAGE = 0, 2

log = logging.getLogger("automation_worker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="automation_worker", description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="do one piece of work and exit")
    mode.add_argument("--loop", action="store_true", help="keep going until stopped")
    parser.add_argument(
        "--every",
        type=float,
        default=5.0,
        help="seconds to wait when there is nothing to do (with --loop; default 5)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


async def _main(args: argparse.Namespace) -> int:
    maker = get_sessionmaker()
    try:
        if args.once:
            outcome = await run_once(maker)
            print(f"{outcome.result}" + (f" {outcome.run_id}" if outcome.run_id else ""))
            return EXIT_OK
        log.info("automation worker started; waiting %.1fs when idle", args.every)
        while True:
            outcome = await run_once(maker)
            if outcome.result == "idle":
                await asyncio.sleep(args.every)
            else:
                log.info("run %s: %s", outcome.run_id, outcome.result)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

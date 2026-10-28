#!/usr/bin/env python
"""Generate, review and render a Client Business Review (TBR).

    python scripts/tbr.py generate --client acme-it --site acme-dental --period 2026-08
    python scripts/tbr.py status   --client acme-it --report <id>
    python scripts/tbr.py show     --client acme-it --report <id> --section cybersecurity
    python scripts/tbr.py approve  --client acme-it --report <id> --section trends
    python scripts/tbr.py add      --client acme-it --report <id> --section accomplishments \\
                                   --text "Moved the finance share to SharePoint"
    python scripts/tbr.py confirm  --client acme-it --report <id> --finding antivirus.unprotected
    python scripts/tbr.py dismiss  --client acme-it --report <id> --finding antivirus.unprotected
    python scripts/tbr.py scores   --client acme-it --report <id> --nps 72 --csat 96 \\
                                   --comment "Always quick to respond"
    python scripts/tbr.py notes    --client acme-it --report <id> --section cybersecurity \\
                                   --text "Open with the patching result"
    python scripts/tbr.py contact-names --client acme-it --report <id> --show
    python scripts/tbr.py generate-all  --client acme-it --period 2026-08
    python scripts/tbr.py render   --client acme-it --report <id>
    python scripts/tbr.py render   --client acme-it --report <id> --mode internal
    python scripts/tbr.py render   --client acme-it --report <id> --mode demo
    python scripts/tbr.py render   --client acme-it --report <id> --pdf
    python scripts/tbr.py approve  --client acme-it --report <id> --section monthly_report

`generate` reads NinjaOne and Autotask for one site, computes the facts and
findings, drafts each section (one model call per section; without
ANTHROPIC_API_KEY the sections use their code-written wording) and stores the
review as a draft. Nothing is rendered until you ask.

`render` fills the template and runs the render gate. If any red text or sample
string from the template would survive, no file is written and the problems are
listed (exit status 3). `--mode` decides who the deck is for:

- `client` (default): what the customer receives. Speaker notes are empty, or
  hold only what you wrote with `notes`. No fact ids.
- `internal`: the same slides with the fact-id trace in the notes.
- `demo`: every client, site, device, contact and engineer name replaced by a
  consistent pseudonym, every number kept, "Sample data" on every slide.

The trace is on the stored review whatever you render: `show --section` prints it.

`render --pdf` writes the Monthly Report instead: the same review's facts as a
landscape PDF (Ticketing, Monitoring, Backup), no model-written text, the same
three modes and the same gate rules. A client PDF needs the review's
`monthly_report` section approved first (`approve --section monthly_report`); an
internal or demo PDF renders before that, stamped as a draft. Any edit to the
review makes the deck and the PDF stale together.

The tickets-by-contact chart prints the customer's people as Contact A, Contact B
unless you turn names on for that review with `contact-names --show` (`--hide`
turns them off again). A demo deck has its own pseudonyms either way.

`generate-all` drafts a review for every active site of the client that is mapped
to NinjaOne or Autotask, one after another. A site that fails does not stop the
run. A site that already has a review for the period is skipped, so nobody's review
work is replaced by a batch; `--regenerate` drafts it again anyway (the earlier
review is superseded, not deleted). It ends with a summary of drafted, failed and
skipped sites, and exits 1 if any site failed.

Every command is scoped to the client given, and a report id that belongs to
another client is reported as not found. Everything is written to `audit_log`
with your handle (`--user`, default your OS user name).
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

import app.connectors.autotask.tools
import app.connectors.ninjaone.tools  # noqa: F401 - registers the connector layer
from app.connectors.errors import ConnectorError, UnknownClient
from app.db.session import dispose_engine, session_scope
from app.models import Client
from app.models.enums import AuditEventType, AuditOutcome
from app.reports.tbr import pipeline, review, store
from app.reports.tbr.audit import ReportAudit
from app.reports.tbr.gate import RenderGateError
from app.reports.tbr.model import ReviewDocument

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_GATE = 0, 1, 2, 3

STATUS_WORDS = {
    "filled": "FILLED",
    "awaiting_engineer": "AWAITING ENGINEER",
    "dropped": "DROPPED",
    "deferred": "DEFERRED",
    "static": "STATIC",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbr",
        description="Generate, review and render a Client Business Review.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("USERNAME") or os.environ.get("USER") or "cli",
        help="your handle, recorded in audit_log and on anything you approve",
    )
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(name: str, help_text: str, *, report: bool = True) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--client", required=True, help="client slug or UUID")
        if report:
            sub.add_argument("--report", required=True, help="report id")
        return sub

    generate = common("generate", "collect, draft and store a review", report=False)
    generate.add_argument("--site", required=True, help="site slug")
    generate.add_argument("--period", required=True, help="last month of the period, e.g. 2026-08")
    generate.add_argument("--months", type=int, help="period length; default from the config")
    generate.add_argument("--json", action="store_true")

    common("status", "one line per slide")
    show = common("show", "a section's drafted content and where it came from")
    show.add_argument("--section", help="section key; omit for facts, findings and gaps")
    common("list", "this client's reviews", report=False)

    for name, help_text in (("approve", "approve a section that needs it"),):
        sub = common(name, help_text)
        sub.add_argument("--section", required=True)
    add = common("add", "add an engineer-written item to a section")
    add.add_argument("--section", required=True)
    add.add_argument("--text", required=True)
    remove = common("remove", "remove one item from a section")
    remove.add_argument("--section", required=True)
    remove.add_argument("--index", type=int, required=True)
    for name, help_text in (
        ("confirm", "confirm a single-source gap so it is asserted"),
        ("dismiss", "dismiss a finding"),
    ):
        sub = common(name, help_text)
        sub.add_argument("--finding", required=True)
    industry = common("industry", "name the industry and research its trends (web)")
    industry.add_argument("--name", required=True, help='e.g. "Commercial real estate"')
    scores = common("scores", "enter CX survey scores and comments")
    scores.add_argument("--nps")
    scores.add_argument("--csat")
    scores.add_argument("--pqual")
    scores.add_argument("--comment", action="append", default=[])
    batch = common("generate-all", "draft a review for every mapped, active site", report=False)
    batch.add_argument("--period", required=True, help="last month of the period, e.g. 2026-08")
    batch.add_argument("--months", type=int, help="period length; default from the config")
    batch.add_argument(
        "--regenerate",
        action="store_true",
        help="also draft sites that already have a review for the period (it is superseded)",
    )
    batch.add_argument("--json", action="store_true")
    names = common("contact-names", "show or hide people's names on the contact chart")
    shown = names.add_mutually_exclusive_group(required=True)
    shown.add_argument("--show", action="store_true", help="print real names")
    shown.add_argument("--hide", action="store_true", help="Contact A, B... (the default)")
    notes = common("notes", "write a slide's speaker notes; empty --text clears them")
    notes.add_argument("--section", required=True)
    notes.add_argument("--text", default="")
    render = common("render", "fill the template; refuses if any placeholder remains")
    render.add_argument(
        "--mode",
        choices=("client", "internal", "demo"),
        default="client",
        help="client: no trace in the notes (default); internal: fact-id trace; "
        "demo: pseudonyms and a Sample data stamp",
    )
    render.add_argument("--output", help="path of the .pptx (or, with --pdf, the .pdf) to write")
    render.add_argument(
        "--pdf", action="store_true", help="write the Monthly Report PDF instead of the deck"
    )
    return parser


async def _client_id(reference: str) -> uuid.UUID:
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
            raise SystemExit(f"No client matches {reference!r}.")
        return client.id


def print_status(document: ReviewDocument) -> None:
    print(
        f"\n{document.site_name} — {document.period.label} "
        f"(generated {document.generated_at:%Y-%m-%d %H:%M} UTC by {document.generated_by})"
    )
    print(f"{'#':>2}  {'STATUS':<18} {'DECK':<4} {'TEXT':<8} SLIDE")
    for row in review.status_report(document):
        print(
            f"{row['slide']:>2}  {STATUS_WORDS[str(row['status'])]:<18} "
            f"{row['on_deck']:<4} {row['narrative']:<8} {row['title']}"
        )
        print(f"      {row['why']}")
        if row["waiting_for"]:
            print(f"      -> {row['waiting_for']}")
        if row["later_connector"] and row["status"] in ("deferred", "dropped"):
            print(f"      later: {row['later_connector']}")
    waiting = pipeline.unresolved(document.findings)
    if waiting:
        print("\nFindings held for confirmation (one tool only):")
        for finding in waiting:
            print(f"  {finding.id}: {finding.headline}")
    if document.facts.gaps:
        print("\nData that could not be read:")
        for gap in document.facts.gaps:
            print(f"  {gap.what}: {gap.why[:140]}")


def print_section(document: ReviewDocument, key: str | None) -> None:
    if key is None:
        print(f"\nFACTS ({len(document.facts.facts)})")
        for fact in document.facts.facts.values():
            scope = "period" if fact.period_bound else "as of generation"
            print(f"  {fact.id} = {fact.display}   [{fact.source}, {scope}]")
        print(
            f"\nTABLES: {', '.join(f'{t.id} ({t.total})' for t in document.facts.tables.values())}"
        )
        print(f"CHARTS: {', '.join(document.facts.charts)}")
        print(f"\nFINDINGS ({len(document.findings)})")
        for finding in document.findings:
            held = "  HELD: one tool only" if not finding.assertable else ""
            print(
                f"  [{finding.severity:<8}] {finding.id} ({finding.section}): "
                f"{finding.headline}{held}"
            )
        return
    section = document.section(key)
    print(f"\nSlide {section.slide}: {section.title} — {section.status} ({section.narrative})")
    print(f"  {section.reason}")
    for index, item in enumerate(section.all_items()):
        lead = f"{item.lead} " if item.lead else ""
        print(f"  [{index}] ({item.author}) {lead}{item.text}")
        print(f"        cites: {', '.join(item.cites)}" + (f"   {item.url}" if item.url else ""))
    for problem in section.problems:
        print(f"  ! {problem}")


async def _mutate(args: argparse.Namespace, client_id: uuid.UUID) -> ReviewDocument:
    """Apply one review action, store it and audit it."""
    async with session_scope() as session:
        _, document = await store.load(session, args.report, client_id=client_id)
    rebuild = False
    detail: dict[str, Any] = {"action": args.command}
    if args.command == "approve":
        review.approve(document, args.section, by=args.user)
        detail["section"] = args.section
    elif args.command == "add":
        review.add_item(document, args.section, args.text, by=args.user)
        detail.update(section=args.section, text=args.text)
    elif args.command == "remove":
        review.remove_item(document, args.section, args.index, by=args.user)
        detail.update(section=args.section, index=args.index)
    elif args.command == "confirm":
        review.confirm_finding(document, args.finding, by=args.user)
        rebuild, detail["finding"] = True, args.finding
    elif args.command == "dismiss":
        review.dismiss_finding(document, args.finding, by=args.user)
        rebuild, detail["finding"] = True, args.finding
    elif args.command == "contact-names":
        review.set_contact_names(document, bool(args.show), by=args.user)
        detail["show"] = bool(args.show)
    elif args.command == "notes":
        review.set_talk_track(document, args.section, args.text, by=args.user)
        detail.update(section=args.section, text=args.text)
    elif args.command == "industry":
        document = await pipeline.research_industry(document, args.name, actor=args.user)
        detail["industry"] = args.name
    elif args.command == "scores":
        review.set_scores(
            document,
            nps=args.nps,
            csat=args.csat,
            pqual=args.pqual,
            comments=args.comment,
            by=args.user,
        )
        detail["scores"] = {"nps": args.nps, "csat": args.csat, "pqual": args.pqual}
    if rebuild:
        document = await pipeline.rebuild(document, actor=args.user)
    async with session_scope() as session:
        row, _ = await store.load(session, args.report, client_id=client_id)
        await store.update(session, row, document)
    await ReportAudit(
        actor_id=args.user, client_id=client_id, client_ref=document.client_slug
    ).write(
        AuditEventType.REPORT_REVIEWED,
        AuditOutcome.SUCCESS,
        payload={"report_id": str(args.report), **detail},
    )
    return document


async def _generate_all(args: argparse.Namespace, client_id: uuid.UUID) -> int:
    def progress(outcome: pipeline.SiteOutcome) -> None:
        if not args.json:
            print(f"  {outcome.status:<8} {outcome.site_slug:<40} {outcome.detail}", flush=True)

    if not args.json:
        print(f"Drafting reviews for the period ending {args.period}:")
    batch = await pipeline.generate_all(
        client_id,
        args.period,
        actor=args.user,
        months=args.months,
        regenerate=args.regenerate,
        on_site=progress,
    )
    if args.json:
        print(json.dumps(batch.as_dict(), indent=2))
    else:
        print(
            f"\n{len(batch.drafted)} drafted, {len(batch.failed)} failed, "
            f"{len(batch.skipped)} skipped, of {len(batch.outcomes)} active sites."
        )
        for title, group in (("Failed", batch.failed), ("Skipped", batch.skipped)):
            if group:
                print(f"\n{title}:")
                for outcome in group:
                    print(f"  {outcome.site_slug}: {outcome.detail}")
        if batch.drafted:
            print("\nDrafted (report id, slides on the deck, waiting on an engineer):")
            for outcome in batch.drafted:
                print(f"  {outcome.report_id}  {outcome.site_slug}: {outcome.detail}")
    return EXIT_FAILED if batch.failed else EXIT_OK


async def _main(args: argparse.Namespace) -> int:
    try:
        client_id = await _client_id(args.client)
        if args.command == "generate":
            made = await pipeline.generate(
                client_id, args.site, args.period, actor=args.user, months=args.months
            )
            if args.json:
                print(
                    json.dumps(
                        {
                            "report_id": str(made.report_id),
                            "slides": review.status_report(made.document),
                        },
                        indent=2,
                    )
                )
            else:
                print(f"report {made.report_id}")
                print_status(made.document)
            return EXIT_OK
        if args.command == "generate-all":
            return await _generate_all(args, client_id)
        if args.command == "list":
            async with session_scope() as session:
                for row in await store.list_for_client(session, client_id):
                    print(
                        f"{row.id}  {row.period_key:<10} {row.status:<9} "
                        f"{row.document.get('site_slug')}  {row.created_at:%Y-%m-%d %H:%M}"
                    )
            return EXIT_OK
        if args.command in ("status", "show"):
            async with session_scope() as session:
                _, document = await store.load(session, args.report, client_id=client_id)
            if args.command == "status":
                print_status(document)
            else:
                print_section(document, args.section)
            return EXIT_OK
        if args.command == "render" and args.pdf:
            from pathlib import Path

            try:
                pdf = await pipeline.render_monthly_report(
                    args.report,
                    client_ref=client_id,
                    actor=args.user,
                    mode=args.mode,
                    output=Path(args.output) if args.output else None,
                )
            except RenderGateError as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_GATE
            print(f"wrote {pdf.path} ({pdf.mode} Monthly Report, {pdf.pages} pages)")
            print(f"sections: {', '.join(pdf.sections) or 'none'}")
            for what, why in pdf.omitted:
                print(f"omitted:  {what}: {why}")
            print(f"render gate: passed ({pdf.texts_checked} strings checked)")
            return EXIT_OK
        if args.command == "render":
            from pathlib import Path

            try:
                result = await pipeline.render_report(
                    args.report,
                    client_ref=client_id,
                    actor=args.user,
                    mode=args.mode,
                    output=Path(args.output) if args.output else None,
                )
            except RenderGateError as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_GATE
            print(f"wrote {result.path} ({result.mode} deck)")
            print(f"slides on the deck: {result.kept}")
            print(f"slides dropped:     {result.dropped}")
            print(
                f"render gate: passed ({result.gate.slides_checked} slides, "
                f"{result.gate.runs_checked} paragraphs checked)"
            )
            return EXIT_OK
        document = await _mutate(args, client_id)
        print_status(document)
        return EXIT_OK
    except (store.ReportNotFound, review.ReviewError, UnknownClient, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except ConnectorError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        await dispose_engine()


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

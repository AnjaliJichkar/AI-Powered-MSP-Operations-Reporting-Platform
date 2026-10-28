#!/usr/bin/env python
"""The ticket workspace from the terminal: list, open, plan, poll.

    python scripts/tickets.py list --client acme-it
    python scripts/tickets.py open --client acme-it --ticket T20260915.0042
    python scripts/tickets.py plan --client acme-it --ticket T20260915.0042
    python scripts/tickets.py poll --client acme-it

Read-only to every vendor; writes only our own tables. The terminal gets
ticket numbers, device ids, methods, confidences, counts and timings — never
a hostname, a contact or a step's text (a step can name a machine). The page
is where a person reads the plan. Every vendor read, prompt, search and
generation is an audit row, attributed to the operator handle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.agent.loop import ModelNotConfigured, get_model_client
from app.audit.recorder import AuditContext
from app.connectors.errors import ConnectorError
from app.db.session import dispose_engine, session_scope
from app.models import Client
from app.models.device import Device
from app.models.enums import Interface
from app.models.site import Site
from app.tickets import workspace as service
from app.tickets.plan import PlanUnavailable, generate_plan

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="The ticket workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--client", required=True, help="client slug or UUID")
    parser.add_argument(
        "--user",
        default=os.environ.get("USERNAME") or os.environ.get("USER") or "operator",
        help="the handle recorded in audit_log",
    )
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="the open tickets: number, site slug, priority, age, SLA")
    opener = sub.add_parser("open", help="open (or refresh) a ticket's workspace")
    opener.add_argument("--ticket", required=True, help="the Autotask ticket number")
    planner = sub.add_parser("plan", help="open the workspace if needed and generate a plan")
    planner.add_argument("--ticket", required=True)
    sub.add_parser("poll", help="stamp workspaces whose ticket has closed")
    return parser


async def _client(reference: str) -> Client:
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
        return client


def _context(actor: str, client: Client) -> AuditContext:
    return AuditContext.new(
        actor_id=actor, interface=Interface.CLI, client_id=client.id, client_ref=client.slug
    )


async def cmd_list(client: Client, actor: str, as_json: bool) -> int:
    async with session_scope() as session:
        data = await service.list_open(session, _context(actor, client), client.id)
    rows = [
        {
            "ticket_number": t["ticket_number"],
            "site_slug": t["site_slug"],
            "priority": t["priority"],
            "age_days": t["age_days"],
            "sla": t["sla"]["state"],
            "workspace": bool(t["workspace_id"]),
            "plans": t["plans"],
        }
        for t in data["tickets"]
    ]
    if as_json:
        print(json.dumps(rows, indent=2, default=str))
        return EXIT_OK
    print(f"{len(rows)} open ticket(s); SLA {dict(Counter(r['sla'] for r in rows))}")
    for r in rows:
        state = ("workspace" if r["workspace"] else "") + (
            f" ({r['plans']} plans)" if r["plans"] else ""
        )
        print(
            f"  {r['ticket_number']:<18} {r['site_slug']:<36} {r['priority']!s:<10} "
            f"{r['age_days']:>6} d  {r['sla']:<9} {state}"
        )
    return EXIT_OK


async def cmd_open(client: Client, actor: str, ticket: str, as_json: bool) -> dict[str, Any]:
    async with session_scope() as session:
        context = _context(actor, client)
        opened = await service.open_workspace(
            session, context, client_id=client.id, ticket_number=ticket, opened_by_user_id=None
        )
        enrichment = await service.refresh_enrichment(session, context, opened.workspace)
        w = opened.workspace
        out = {
            "workspace_id": str(w.id),
            "ticket_number": w.ticket_number,
            "site_id": str(w.site_id) if w.site_id else None,
            "created": opened.created,
            "scrubbed": w.scrubbed,
            "hostnames_seen": len(opened.resolution.hostnames),
            "contact": bool(w.contact),
            "device_id": str(w.device_id) if w.device_id else None,
            "method": w.device_method,
            "confidence": w.device_confidence,
            "candidates": [
                {"method": c["method"], "confidence": c["confidence"]} for c in w.device_candidates
            ],
            "facts": len(enrichment.facts),
            "enrichment_reads": enrichment.reads,
            "enrichment_errors": [e["source"] for e in enrichment.errors],
        }
    if as_json:
        print(json.dumps(out, indent=2))
    else:
        freshness = "new" if out["created"] else "refreshed"
        contact = "yes" if out["contact"] else "no"
        candidates = ", ".join(f"{c['method']}/{c['confidence']}" for c in out["candidates"])
        print(
            f"{out['ticket_number']}: workspace {out['workspace_id']} ({freshness}); "
            f"{out['scrubbed']} secret(s) scrubbed; {out['hostnames_seen']} hostname-like "
            f"token(s); contact {contact}"
        )
        print(
            f"  device {out['device_id'] or 'none'} by {out['method']} "
            f"({out['confidence'] or 'n/a'}); candidates: {candidates or 'none'}"
        )
        print(
            f"  enrichment: {out['facts']} fact(s) from {out['enrichment_reads']} read(s); "
            f"errors: {out['enrichment_errors'] or 'none'}"
        )
    return out


async def cmd_plan(client: Client, actor: str, ticket: str, as_json: bool) -> int:
    opened = await cmd_open(client, actor, ticket, as_json=False)
    try:
        model_client = get_model_client()
    except ModelNotConfigured:
        model_client = None
    async with session_scope() as session:
        context = _context(actor, client)
        workspace = (
            await session.execute(
                select(service.TicketWorkspace).where(
                    service.TicketWorkspace.id == uuid.UUID(opened["workspace_id"])
                )
            )
        ).scalar_one()
        device = await session.get(Device, workspace.device_id) if workspace.device_id else None
        site = await session.get(Site, workspace.site_id) if workspace.site_id else None
        try:
            generation = await generate_plan(
                session,
                context,
                workspace,
                author=actor,
                author_user_id=None,
                device_name=device.canonical_name if device else None,
                site_name=site.name if site else None,
                client_name=client.name,
                model_client=model_client,
            )
        except PlanUnavailable as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return EXIT_FAILED
        plan = generation.plan
        kinds = Counter(c["kind"] for s in plan.steps for c in s["citations"])
        summary = {
            "ticket_number": workspace.ticket_number,
            "version": plan.version,
            "steps_written": generation.steps_written,
            "steps_kept": len(plan.steps),
            "dropped": plan.dropped,
            "citations": generation.citations,
            "citations_by_kind": dict(kinds),
            "commands_shown": sum(1 for s in plan.steps if s.get("command")),
            "evidence": {
                kind: sum(1 for e in plan.evidence["items"] if e["kind"] == kind)
                for kind in ("resolved_step", "ticket", "fact")
            },
            "web_searches": len(generation.queries),
            "urls_returned": len(plan.evidence.get("urls", [])),
            "problems": plan.problems,
            "generation_ms": plan.generation_ms,
        }
    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"  plan v{summary['version']}: {summary['steps_kept']} step(s) kept of "
            f"{summary['steps_written']} written ({summary['dropped']} dropped); "
            f"{summary['citations']} citation(s) {summary['citations_by_kind']}; "
            f"{summary['commands_shown']} command(s) shown; evidence {summary['evidence']}; "
            f"{summary['web_searches']} web search(es), {summary['urls_returned']} url(s); "
            f"{summary['generation_ms']} ms"
        )
        for problem in summary["problems"]:
            print(f"    problem: {problem}")
    return EXIT_OK


async def cmd_poll(client: Client, actor: str) -> int:
    async with session_scope() as session:
        closed = await service.poll_statuses(session, _context(actor, client), client.id)
    print(f"{len(closed)} workspace(s) closed since the last check")
    return EXIT_OK


async def _main(args: argparse.Namespace) -> int:
    client = await _client(args.client)
    try:
        if args.command == "list":
            return await cmd_list(client, args.user, args.json)
        if args.command == "open":
            await cmd_open(client, args.user, args.ticket, args.json)
            return EXIT_OK
        if args.command == "plan":
            return await cmd_plan(client, args.user, args.ticket, args.json)
        return await cmd_poll(client, args.user)
    except (ConnectorError, service.WorkspaceError) as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


async def _run(args: argparse.Namespace) -> int:
    try:
        return await _main(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_run(args))
    except SystemExit as error:
        if isinstance(error.code, int):
            raise
        print(str(error), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())

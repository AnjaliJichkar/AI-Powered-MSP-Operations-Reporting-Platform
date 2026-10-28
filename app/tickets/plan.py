"""A remediation plan: ordered steps, each resting on evidence a person can open.

The model is given three kinds of evidence and may cite nothing else:

- `ticket:<number>` — a resolved Autotask ticket `search_tickets` found for
  the same words, with its scrubbed description and resolution;
- `fact:<id>` — a line of the enrichment panel (`enrich.py`), as of its time;
- `url:<address>` — a page its own web searches returned, on the ticket
  allowlist (`TICKET_WEB_SEARCH_ALLOWED_DOMAINS`), and never a query that
  named the client, the site, the contact or a machine: the queries are
  checked afterwards and a leak voids every web citation.

Steps from earlier plans that an engineer marked as having resolved a similar
ticket come first (`resolved:<number>:<step>`), which is how an outcome ranks
the next suggestion. A step the model writes without a citation we hold is
dropped and counted, never shown. A command appears only when it is in a
cited ticket's text word for word; otherwise the step is words and is marked
"manual, not executed" — a web page's text is not something we hold, so a
command from the web is never shown as a command.

Every generation is a new plan version with its author; nothing is
overwritten. The prompt, each search, the model's answer and the plan itself
are audit rows (hard rule 4). Ticket text reaches the model scrubbed only.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, cast
from urllib.parse import urlparse

from anthropic import AsyncAnthropic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.connectors.autotask.tools import get_ticket, search_tickets
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName
from app.models.ticket import TicketPlan, TicketWorkspace
from app.settings import Settings, get_settings
from app.tickets.enrich import audited_read
from app.tickets.scrub import scrub

PROMPT_VERSION = "ticket-plan-v1"
WEB_SEARCH_TOOL_NAME = "web_search"
MAX_STEPS = 10
MAX_STEP_CHARS = 600
STOPWORDS = frozenset(
    "the a an and or of to in on for with from at by is are was were be been not no "
    "cannot can't wont won't does doesn't did didn't has have had this that these those "
    "user users please help issue problem error unable working work need needs new when "
    "after before still again keeps keep getting get gets since today yesterday computer "
    "laptop machine pc desktop ticket request".split()
)

SYSTEM_PROMPT = """\
You write a remediation plan for an engineer at a managed IT provider who is \
working one support ticket. You are given the ticket (title, description, \
contact role), what the monitoring and security tools currently say about the \
machine it concerns (facts, each with an id), resolved past tickets that \
matched the same words (each with an id, and its description and resolution), \
and, first, steps that engineers marked as having resolved similar tickets.

Rules, and each is checked in code:
- Write at most {max_steps} ordered steps, each under {max_chars} characters, \
in plain words an engineer can follow. First what to check, then what to change.
- Every step must cite at least one piece of evidence by its id: a resolved \
ticket (ticket:...), a fact (fact:...), a resolved step (resolved:...), or a \
documentation page you found with web search (url:...). A step with no citation \
is discarded before anyone sees it, so do not write one.
- You may use web search for vendor documentation about the product, error or \
symptom. Never put the customer's name, a site, a person, a hostname, an IP \
address, a serial number or anything else from the ticket into a query: search \
for the product and the error, nothing more. A query that names any of them \
discards every web citation in the plan.
- Include a command or script only when it appears word for word in a cited \
resolved ticket's text; put it in the step's `command` field and quote the \
ticket in `quote`. Never invent a command, and never take one from a web page: \
describe the action in words instead. Steps without a command are marked \
"manual, not executed" by code.
- Do not restate the facts; use them as reasons. Say when a fact rules \
something out.
- Answer only by calling write_plan.
"""

WRITE_PLAN_TOOL: dict[str, Any] = {
    "name": "write_plan",
    "description": "Return the ordered remediation steps, each with its citations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "cites": {"type": "array", "items": {"type": "string"}},
                        "command": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                    "required": ["text", "cites"],
                },
            }
        },
        "required": ["steps"],
    },
}


class PlanUnavailable(Exception):
    """The plan could not be generated: no model, or the model failed."""


# --- evidence -------------------------------------------------------------------------


@dataclass(slots=True)
class Evidence:
    """One thing a step may cite, and the text a command must be found in."""

    id: str
    kind: str
    label: str
    text: str = ""
    url: str | None = None
    rank: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "url": self.url}


@dataclass(slots=True)
class Gathered:
    evidence: list[Evidence] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    never_in_a_query: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, Evidence]:
        return {e.id: e for e in self.evidence}


def search_terms(title: str | None, description: str | None) -> list[str]:
    """What to search past tickets for: the title whole, then its two rarest words."""
    terms: list[str] = []
    clean_title = " ".join((title or "").split())
    if len(clean_title) >= 6:
        terms.append(clean_title[:120])
    words = re.findall(r"[A-Za-z][A-Za-z0-9._-]{4,}", f"{title or ''} {description or ''}")
    distinct: list[str] = []
    for word in sorted(words, key=len, reverse=True):
        key = word.lower().strip(".-_")
        if key in STOPWORDS or key in {t.lower() for t in distinct}:
            continue
        distinct.append(word)
        if len(distinct) == 2:
            break
    terms.extend(distinct)
    return terms


async def gather(
    session: AsyncSession,
    context: AuditContext,
    workspace: TicketWorkspace,
    *,
    device_name: str | None,
    site_name: str | None,
    client_name: str | None,
    settings: Settings,
) -> Gathered:
    """Facts from the stored enrichment, resolved tickets from search, prior outcomes."""
    out = Gathered()
    client = str(workspace.client_id)
    out.never_in_a_query = [
        name
        for name in (
            client_name,
            site_name,
            device_name,
            workspace.contact,
            *workspace.enrichment.get("hostnames", []),
        )
        if name and len(name) > 3
    ]

    # Resolved tickets for the same words, whole client: history is client-wide.
    hits: dict[str, Any] = {}
    for term in search_terms(workspace.title, workspace.description):
        found, _ = await audited_read(
            session,
            context,
            connector=ConnectorName.AUTOTASK,
            tool="search_tickets",
            call=partial(search_tickets, client, term, order_by="relevance"),
            payload={"purpose": "plan evidence"},
        )
        if found is None:
            continue
        for match in found.tickets:
            if not match.ticket_number or match.ticket_number == workspace.ticket_number:
                continue
            if match.completed_at is None and not (match.status or "").lower().startswith(
                ("complete", "closed", "resolved")
            ):
                continue
            if match.ticket_number not in hits or match.score > hits[match.ticket_number].score:
                hits[match.ticket_number] = match
        if len(hits) >= settings.ticket_similar_tickets:
            break
    ranked = sorted(
        hits.values(),
        key=lambda m: (-m.score, m.created_at or dt.datetime.min.replace(tzinfo=dt.UTC)),
    )
    ranked = ranked[: settings.ticket_similar_tickets]

    # Steps that resolved one of those tickets, from an earlier plan's outcome.
    numbers = [m.ticket_number for m in ranked]
    if numbers:
        rows = (
            await session.execute(
                select(TicketPlan, TicketWorkspace)
                .join(TicketWorkspace, TicketWorkspace.id == TicketPlan.workspace_id)
                .where(
                    TicketPlan.client_id == workspace.client_id,
                    TicketWorkspace.ticket_number.in_(numbers),
                )
            )
        ).all()
        for plan, other in rows:
            resolved = set(plan.outcome.get("resolved_steps") or [])
            for step in plan.steps:
                if step.get("id") in resolved:
                    out.evidence.append(
                        Evidence(
                            id=f"resolved:{other.ticket_number}:{step['id']}",
                            kind="resolved_step",
                            label=f"a step an engineer marked as resolving {other.ticket_number}",
                            text=str(step.get("review", {}).get("text") or step.get("text", "")),
                            rank=0,
                        )
                    )

    for order, match in enumerate(ranked, start=1):
        detail, _ = await audited_read(
            session,
            context,
            connector=ConnectorName.AUTOTASK,
            tool="get_ticket",
            call=partial(get_ticket, client, match.ticket_number),
            payload={"purpose": "plan evidence"},
        )
        description = resolution = ""
        notes: list[str] = []
        if detail is not None and getattr(detail, "found", False):
            d: Any = detail
            description = scrub(d.description).text
            resolution = scrub(d.resolution).text
            notes = [scrub(n.excerpt).text for n in d.notes if n.excerpt]
        text = "\n".join(part for part in (description, resolution, *notes) if part)
        out.evidence.append(
            Evidence(
                id=f"ticket:{match.ticket_number}",
                kind="ticket",
                label=f"{match.ticket_number}: {scrub(match.title).text}",
                text=text or scrub(match.snippet).text,
                rank=order,
            )
        )

    for fact in workspace.enrichment.get("facts", []):
        out.evidence.append(
            Evidence(
                id=f"fact:{fact['id']}",
                kind="fact",
                label=f"{fact['label']}: {json.dumps(fact.get('value'), default=str)}"
                + (f" ({fact['detail']})" if fact.get("detail") else ""),
                rank=100,
            )
        )
    out.evidence.sort(key=lambda e: e.rank)
    return out


# --- the model ------------------------------------------------------------------------


def ticket_search_tool(settings: Settings) -> dict[str, Any] | None:
    if not settings.web_search_enabled:
        return None
    return {
        "type": "web_search_20250305",
        "name": WEB_SEARCH_TOOL_NAME,
        "max_uses": settings.ticket_web_search_max_uses,
        "allowed_domains": settings.ticket_web_search_domains,
    }


def url_allowed(url: str, settings: Settings) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return bool(host) and any(
        host == domain or host.endswith("." + domain)
        for domain in settings.ticket_web_search_domains
    )


def url_key(url: str) -> str:
    """A URL as the model tends to retype it: no scheme, no www, no query, no slash."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/").lower()
    return f"{host}{path}"


def _searched(response: Any) -> tuple[list[str], dict[str, str]]:
    """Queries the model ran and every URL (with title) those searches returned."""
    queries: list[str] = []
    urls: dict[str, str] = {}
    for block in response.content:
        kind = getattr(block, "type", None)
        if kind == "server_tool_use" and getattr(block, "name", "") == WEB_SEARCH_TOOL_NAME:
            query = (getattr(block, "input", None) or {}).get("query")
            if query:
                queries.append(str(query))
        elif kind == "web_search_tool_result":
            content = getattr(block, "content", None)
            for result in content if isinstance(content, list) else []:
                url = getattr(result, "url", None)
                if url:
                    urls[str(url)] = str(getattr(result, "title", "") or "")
    return queries, urls


def _plan_input(response: Any) -> dict[str, Any] | None:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "write_plan":
            return dict(block.input or {})
    return None


def brief_for(workspace: TicketWorkspace, gathered: Gathered, *, device_name: str | None) -> str:
    """What the model is shown: the scrubbed ticket, the device, and the evidence by id."""
    lines = [
        f"Ticket title: {workspace.title or ''}",
        f"Priority: {workspace.priority or 'unknown'}",
        f"Contact: {'named' if workspace.contact else 'none'}",
        f"Machine: {device_name or 'not identified'}",
        "",
        "Description:",
        (workspace.description or "").strip() or "(none)",
        "",
        "Evidence you may cite, by id:",
    ]
    for item in gathered.evidence:
        lines.append(f"- {item.id} [{item.kind}] {item.label}")
        if item.kind in ("ticket", "resolved_step") and item.text:
            body = item.text if len(item.text) <= 2500 else item.text[:2500] + " …"
            lines.append("  text: " + body.replace("\n", "\n  "))
    return "\n".join(lines)


# --- validation ----------------------------------------------------------------------


_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.S)


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


def _command_in(command: str, sources: Sequence[Evidence]) -> str | None:
    """The id of the cited ticket whose text holds the command word for word, if any."""
    wanted = _normalise(command)
    if len(wanted) < 3:
        return None
    for source in sources:
        if source.kind in ("ticket", "resolved_step") and wanted in _normalise(source.text):
            return source.id
    return None


def _strip_unsourced_code(text: str, sources: Sequence[Evidence]) -> str:
    def keep_or_omit(match: re.Match[str]) -> str:
        span = match.group(0).strip("`").strip()
        if _command_in(span, sources):
            return match.group(0)
        return "[command omitted: not in a cited source]"

    return _CODE.sub(keep_or_omit, text)


def validate_steps(
    raw_steps: Sequence[dict[str, Any]],
    evidence: dict[str, Evidence],
    *,
    allowed_urls: dict[str, str],
    web_voided: bool,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Keep the steps that cite evidence we hold; strip what no source backs.

    Returns the steps, how many were dropped, and the problems worth showing.
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    problems: list[str] = []
    for index, raw in enumerate(list(raw_steps)[:MAX_STEPS], start=1):
        text = " ".join(str(raw.get("text", "")).split())
        if not text:
            dropped += 1
            continue
        citations: list[dict[str, Any]] = []
        cited_sources: list[Evidence] = []
        for cite in raw.get("cites") or []:
            ref = str(cite).strip()
            if ref in evidence:
                item = evidence[ref]
                citations.append(
                    {"kind": item.kind, "ref": ref, "label": item.label, "url": item.url}
                )
                cited_sources.append(item)
            elif ref.startswith("url:") or ref.startswith("http"):
                cited = ref[4:] if ref.startswith("url:") else ref
                # The page as the search returned it, however the model retyped it.
                url = next((u for u in allowed_urls if url_key(u) == url_key(cited)), None)
                if web_voided:
                    problems.append(
                        f"step {index}: web citation discarded, a search named the client"
                    )
                elif url is not None:
                    citations.append(
                        {
                            "kind": "url",
                            "ref": f"url:{url}",
                            "label": allowed_urls[url] or url,
                            "url": url,
                        }
                    )
                else:
                    problems.append(f"step {index}: cited a page the searches did not return")
        # One citation per distinct ref.
        unique: dict[str, dict[str, Any]] = {}
        for citation in citations:
            unique.setdefault(citation["ref"], citation)
        citations = list(unique.values())
        if not citations:
            dropped += 1
            continue
        command = str(raw.get("command") or "").strip() or None
        command_source = _command_in(command, cited_sources) if command else None
        if command and command_source is None:
            problems.append(f"step {index}: a command not found in any cited ticket was removed")
            command = None

        # Code the model put in the prose is held to the same rule.
        text = _strip_unsourced_code(text, cited_sources)[:MAX_STEP_CHARS]
        kept.append(
            {
                "id": f"s{len(kept) + 1}",
                "order": len(kept) + 1,
                "text": text,
                "citations": citations,
                "command": command,
                "command_source": command_source,
                "execution": (
                    f"command from {command_source}, not executed"
                    if command
                    else "manual, not executed"
                ),
                "review": {"status": "proposed", "text": None, "by": None, "at": None},
            }
        )
    return kept, dropped, problems


# --- generation ----------------------------------------------------------------------


@dataclass(slots=True)
class Generation:
    plan: TicketPlan
    steps_written: int
    citations: int
    queries: list[str]


async def next_version(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    current = (
        await session.execute(
            select(func.max(TicketPlan.version)).where(TicketPlan.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    return int(current or 0) + 1


async def generate_plan(
    session: AsyncSession,
    context: AuditContext,
    workspace: TicketWorkspace,
    *,
    author: str,
    author_user_id: uuid.UUID | None,
    device_name: str | None,
    site_name: str | None,
    client_name: str | None,
    model_client: AsyncAnthropic | None,
    settings: Settings | None = None,
    now: dt.datetime | None = None,
) -> Generation:
    """Gather the evidence, ask the model, keep what cites, store a new version."""
    settings = settings or get_settings()
    now = now or dt.datetime.now(dt.UTC)
    if model_client is None:
        raise PlanUnavailable(
            "no model is configured (ANTHROPIC_API_KEY), so no plan can be written"
        )
    started = time.perf_counter()
    gathered = await gather(
        session,
        context,
        workspace,
        device_name=device_name,
        site_name=site_name,
        client_name=client_name,
        settings=settings,
    )
    brief = brief_for(workspace, gathered, device_name=device_name)
    await record_event(
        session,
        context,
        event_type=AuditEventType.PROMPT,
        outcome=AuditOutcome.SUCCESS,
        model=settings.anthropic_model,
        prompt_version=PROMPT_VERSION,
        payload={
            "purpose": "ticket plan",
            "ticket_number": workspace.ticket_number,
            "evidence": {
                kind: sum(1 for e in gathered.evidence if e.kind == kind)
                for kind in ("resolved_step", "ticket", "fact")
            },
            "brief_chars": len(brief),
        },
    )
    system = SYSTEM_PROMPT.format(max_steps=MAX_STEPS, max_chars=MAX_STEP_CHARS)
    tools: list[dict[str, Any]] = [WRITE_PLAN_TOOL]
    search = ticket_search_tool(settings)
    if search is not None:
        tools.insert(0, search)
    messages: list[dict[str, Any]] = [{"role": "user", "content": brief}]
    queries: list[str] = []
    urls: dict[str, str] = {}
    payload: dict[str, Any] | None = None
    usage: Any = None
    try:
        for attempt in range(6):
            response = await cast(Any, model_client).messages.create(
                model=settings.anthropic_model,
                max_tokens=settings.ticket_plan_max_tokens,
                system=system,
                messages=messages,
                tools=tools,
            )
            usage = getattr(response, "usage", None)
            found_queries, found_urls = _searched(response)
            queries.extend(found_queries)
            urls.update(found_urls)
            payload = _plan_input(response)
            if payload is not None and payload.get("steps"):
                break
            messages = [*messages, {"role": "assistant", "content": response.content}]
            if response.stop_reason == "pause_turn":
                continue
            if attempt >= 1:
                break
            messages.append(
                {
                    "role": "user",
                    "content": "Call write_plan now with the steps and their citations.",
                }
            )
        if payload is None or not payload.get("steps"):
            # Text instead of the tool, or an empty plan: one last turn with the
            # plan tool required and no more searching, on what it already has.
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "Write the plan now from the evidence above, citing ticket:, fact: "
                        "and resolved: ids and the url: pages your searches already returned. "
                        "No more searches."
                    ),
                },
            ]
            response = await cast(Any, model_client).messages.create(
                model=settings.anthropic_model,
                max_tokens=settings.ticket_plan_max_tokens,
                system=system,
                messages=messages,
                tools=[WRITE_PLAN_TOOL],
                tool_choice={"type": "tool", "name": "write_plan"},
            )
            usage = getattr(response, "usage", None)
            payload = _plan_input(response)
    except Exception as exc:
        await record_event(
            session,
            context,
            event_type=AuditEventType.MODEL_RESPONSE,
            outcome=AuditOutcome.ERROR,
            model=settings.anthropic_model,
            prompt_version=PROMPT_VERSION,
            error_type=type(exc).__name__,
            error_message=str(exc)[:300],
            payload={"purpose": "ticket plan", "ticket_number": workspace.ticket_number},
        )
        raise PlanUnavailable(f"the model call failed: {type(exc).__name__}: {exc}"[:300]) from exc

    for query in queries:
        await record_event(
            session,
            context,
            event_type=AuditEventType.WEB_SEARCH,
            outcome=AuditOutcome.SUCCESS,
            model=settings.anthropic_model,
            payload={"purpose": "ticket plan", "query": query, "urls": sorted(urls)[:20]},
        )
    leaked = [
        query
        for query in queries
        for name in gathered.never_in_a_query
        if name.casefold() in query.casefold()
    ]
    allowed_urls = {url: title for url, title in urls.items() if url_allowed(url, settings)}
    raw_steps = list((payload or {}).get("steps") or [])
    steps, dropped, problems = validate_steps(
        raw_steps, gathered.by_id(), allowed_urls=allowed_urls, web_voided=bool(leaked)
    )
    if leaked:
        problems.insert(
            0,
            "a web search query named the client, a site, a person or a machine: "
            "every web citation was discarded",
        )
    if payload is None:
        problems.insert(0, "the model did not return a plan")
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    await record_event(
        session,
        context,
        event_type=AuditEventType.MODEL_RESPONSE,
        outcome=AuditOutcome.SUCCESS if steps else AuditOutcome.ERROR,
        model=settings.anthropic_model,
        prompt_version=PROMPT_VERSION,
        duration_ms=elapsed_ms,
        payload={
            "purpose": "ticket plan",
            "ticket_number": workspace.ticket_number,
            "steps_written": len(raw_steps),
            "steps_kept": len(steps),
            "dropped": dropped,
            "problems": problems,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        },
    )
    version = await next_version(session, workspace.id)
    plan = TicketPlan(
        client_id=workspace.client_id,
        workspace_id=workspace.id,
        version=version,
        author_user_id=author_user_id,
        author=author,
        generated_at=now,
        model=settings.anthropic_model,
        prompt_version=PROMPT_VERSION,
        generation_ms=elapsed_ms,
        device_id=workspace.device_id,
        steps=steps,
        evidence={
            "items": [e.as_dict() for e in gathered.evidence],
            "queries": queries,
            "urls": sorted(allowed_urls),
            "enrichment_as_of": workspace.enrichment.get("as_of"),
        },
        dropped=dropped,
        problems=problems,
        outcome={},
    )
    session.add(plan)
    await session.flush()
    citations = sum(len(step["citations"]) for step in steps)
    await record_event(
        session,
        context,
        event_type=AuditEventType.TICKET_PLAN_GENERATED,
        outcome=AuditOutcome.SUCCESS if steps else AuditOutcome.ERROR,
        model=settings.anthropic_model,
        prompt_version=PROMPT_VERSION,
        duration_ms=elapsed_ms,
        payload={
            "workspace_id": str(workspace.id),
            "ticket_number": workspace.ticket_number,
            "version": version,
            "steps": len(steps),
            "dropped": dropped,
            "citations": citations,
            "web_searches": len(queries),
            "problems": problems,
        },
    )
    return Generation(plan=plan, steps_written=len(raw_steps), citations=citations, queries=queries)

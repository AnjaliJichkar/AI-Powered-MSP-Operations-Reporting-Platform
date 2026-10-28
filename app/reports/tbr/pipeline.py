"""The run: facts, findings, sections, stored as a review an engineer can then work on.

`generate` is the only way a review comes into being, and `render_report` the
only way one becomes a deck. Both are scoped to a client resolved server-side
and a site that must be that client's, exactly as a tool call is (hard rule 1),
and both leave audit rows (hard rule 4).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from anthropic import AsyncAnthropic
from sqlalchemy import select

from app.agent.loop import ModelNotConfigured, get_model_client
from app.connectors.factory import list_client_sites, resolve_client, resolve_site
from app.db.session import session_scope
from app.models import ToolMapping
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName
from app.reports.tbr import review, store
from app.reports.tbr.audit import ReportAudit
from app.reports.tbr.collect import Collector
from app.reports.tbr.config import FranchiseeConfig, load_config
from app.reports.tbr.findings import derive_findings
from app.reports.tbr.gate import RenderGateError
from app.reports.tbr.model import Finding, ReviewDocument, ReviewPeriod, Section
from app.reports.tbr.narrative import research_trends
from app.reports.tbr.pdf import PdfResult, default_pdf_output, render_pdf
from app.reports.tbr.render import RenderMode, RenderResult, default_output, render
from app.reports.tbr.sections import Context, build_sections
from app.settings import Settings, get_settings


@dataclass(slots=True)
class Generated:
    report_id: uuid.UUID
    document: ReviewDocument


def parse_period(text: str, months: int) -> ReviewPeriod:
    """`2026-08` -> the review period ending that month."""
    try:
        year, month = (int(part) for part in text.split("-", 1))
    except ValueError as exc:
        raise ValueError("period must look like 2026-08") from exc
    return ReviewPeriod.ending(year, month, months)


def _model(settings: Settings, model_client: AsyncAnthropic | None) -> AsyncAnthropic | None:
    if model_client is not None:
        return model_client
    try:
        return get_model_client()
    except ModelNotConfigured:
        return None  # sections fall back to their code-written wording, and say so


def _carry_over(fresh: list[Section], old: list[Section]) -> list[Section]:
    """Keep what the engineer did when sections are rebuilt after a finding changes."""
    previous = {section.key: section for section in old}
    for section in fresh:
        before = previous.get(section.key)
        if before is None:
            continue
        if section.key == "trends":
            fresh[fresh.index(section)] = before  # never re-research behind an approval
            continue
        section.engineer_items = before.engineer_items
        if before.data and section.key == "cx":
            section.data = before.data
        if before.engineer_items or (section.key == "cx" and before.data):
            section.status = "filled"
            section.reason = before.reason
            section.waiting_for = None
    return fresh


async def _has_mapping(session: Any, site_id: uuid.UUID, connector: ConnectorName) -> bool:
    row = await session.execute(
        select(ToolMapping.id).where(
            ToolMapping.site_id == site_id, ToolMapping.connector_name == connector
        )
    )
    return row.first() is not None


async def generate(
    client_ref: str | uuid.UUID,
    site_ref: str,
    period_text: str,
    *,
    actor: str,
    months: int | None = None,
    model_client: AsyncAnthropic | None = None,
    settings: Settings | None = None,
    now: dt.datetime | None = None,
) -> Generated:
    settings = settings or get_settings()
    now = now or dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        client = await resolve_client(session, client_ref)
        site = await resolve_site(session, client, site_ref)  # refuses another client's site
        config = await load_config(session, client.id, client_name=client.name)
        period = parse_period(period_text, months or config.review_period_months)
        prior = await store.prior_snapshot(session, site.id, period)
        bitdefender_mapped = await _has_mapping(session, site.id, ConnectorName.BITDEFENDER)
        connectsecure_mapped = await _has_mapping(session, site.id, ConnectorName.CONNECTSECURE)
        cove_mapped = await _has_mapping(session, site.id, ConnectorName.COVE)
        backup_trend = await store.backup_trend(session, site.id)
        previous = prior[0].get("cove.coverage.scope") if prior else None
        client_id, client_slug, client_name = client.id, client.slug, client.name
        site_id, site_slug, site_name = site.id, site.slug, site.name

    audit = ReportAudit(actor_id=actor, client_id=client_id, client_ref=client_slug)
    facts = await Collector(
        client_id=str(client_id),
        site_id=str(site_id),
        site_slug=site.slug,
        period=period,
        config=config,
        audit=audit,
        now=now,
        bitdefender_mapped=bitdefender_mapped,
        connectsecure_mapped=connectsecure_mapped,
        cove_mapped=cove_mapped,
        backup_trend=backup_trend,
        previous_backup_scope=str(previous.value) if previous else None,
    ).collect()
    prior_facts, prior_label = prior if prior else (None, None)
    findings = derive_findings(
        facts, prior=prior_facts, prior_label=prior_label, config=config, period=period
    )
    client_api = _model(settings, model_client)
    sections = await build_sections(
        Context(
            facts=facts,
            findings=findings,
            config=config,
            period=period,
            site_name=site_name,
            client_name=client_name,
            has_prior=prior_facts is not None,
            prior=prior_facts,
            prior_label=prior_label,
            model_client=client_api,
            settings=settings,
            audit=audit,
        )
    )
    document = ReviewDocument(
        client_id=str(client_id),
        client_slug=client_slug,
        client_name=client_name,
        site_id=str(site_id),
        site_slug=site_slug,
        site_name=site_name,
        period=period,
        generated_at=now,
        generated_by=actor,
        model=settings.anthropic_model if client_api is not None else None,
        franchisee=config.model_dump(mode="json"),
        facts=facts,
        prior_facts=prior_facts,
        prior_period_label=prior_label,
        findings=findings,
        sections=sections,
    )
    async with session_scope() as session:
        row = await store.save_new(session, document, created_by=actor)
        report_id = row.id
    await audit.write(
        AuditEventType.REPORT_GENERATED,
        AuditOutcome.SUCCESS,
        payload={
            "report_id": str(report_id),
            "site": site_slug,
            "period": period.key,
            "facts": len(facts.facts),
            "findings": [f.id for f in findings],
            "gaps": [gap.model_dump() for gap in facts.gaps],
            "slides": {str(r["slide"]): r["status"] for r in review.status_report(document)},
        },
    )
    return Generated(report_id=report_id, document=document)


#: A review reads these two. A site mapped to neither has nothing to review.
REVIEW_CONNECTORS = (ConnectorName.NINJAONE, ConnectorName.AUTOTASK)


@dataclass(slots=True)
class SiteOutcome:
    site_slug: str
    site_name: str
    status: Literal["drafted", "failed", "skipped"]
    detail: str
    report_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "site": self.site_slug,
            "name": self.site_name,
            "status": self.status,
            "detail": self.detail,
            "report_id": self.report_id,
        }


@dataclass(slots=True)
class BatchResult:
    period: str
    outcomes: list[SiteOutcome] = field(default_factory=list)

    def _with(self, status: str) -> list[SiteOutcome]:
        return [outcome for outcome in self.outcomes if outcome.status == status]

    @property
    def drafted(self) -> list[SiteOutcome]:
        return self._with("drafted")

    @property
    def failed(self) -> list[SiteOutcome]:
        return self._with("failed")

    @property
    def skipped(self) -> list[SiteOutcome]:
        return self._with("skipped")

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "drafted": len(self.drafted),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "sites": [outcome.as_dict() for outcome in self.outcomes],
        }


def _drafted_detail(document: ReviewDocument) -> str:
    slides = review.status_report(document)
    on_deck = sum(1 for row in slides if row["on_deck"] == "yes")
    waiting = sum(1 for row in slides if row["status"] == "awaiting_engineer")
    gaps = len(document.facts.gaps)
    return f"{on_deck} slides on the deck, {waiting} waiting on an engineer" + (
        f", {gaps} source(s) unavailable" if gaps else ""
    )


async def generate_all(
    client_ref: str | uuid.UUID,
    period_text: str,
    *,
    actor: str,
    months: int | None = None,
    regenerate: bool = False,
    model_client: AsyncAnthropic | None = None,
    settings: Settings | None = None,
    on_site: Callable[[SiteOutcome], None] | None = None,
) -> BatchResult:
    """Draft a review for every active site of one client that a review can read.

    Every site gets an outcome, in name order:

    - **skipped** — no NinjaOne or Autotask mapping, so there is nothing to read;
      or it already has a review for the period and `regenerate` is off. A batch
      does not replace a review an engineer may have worked on unless asked to.
    - **failed** — generating raised, or every source failed and the draft holds no
      vendor fact. The run goes on to the next site either way.
    - **drafted** — a draft is stored, exactly as `generate` stores one.

    Sites run one after another: a batch is not urgent, and the vendors' rate
    limits are shared with the engineers using the agent. Each site is scoped
    and audited by `generate` itself; nothing here reads a vendor.
    """
    async with session_scope() as session:
        client = await resolve_client(session, client_ref)
        config = await load_config(session, client.id, client_name=client.name)
        period = parse_period(period_text, months or config.review_period_months)
        sites = [(s.id, s.slug, s.name) for s in await list_client_sites(session, client.id)]
        mapped = {
            site_id
            for (site_id,) in (
                await session.execute(
                    select(ToolMapping.site_id).where(
                        ToolMapping.client_id == client.id,
                        ToolMapping.site_id.is_not(None),
                        ToolMapping.connector_name.in_(REVIEW_CONNECTORS),
                    )
                )
            ).all()
        }
        existing = await store.sites_with_a_review(session, client.id, period.key)
        client_id, client_slug = client.id, client.slug

    audit = ReportAudit(actor_id=actor, client_id=client_id, client_ref=client_slug)
    batch = BatchResult(period=period.label)
    for site_id, slug, name in sites:
        if site_id not in mapped:
            outcome = SiteOutcome(slug, name, "skipped", "no NinjaOne or Autotask mapping")
        elif site_id in existing and not regenerate:
            outcome = SiteOutcome(
                slug,
                name,
                "skipped",
                f"already has a review for {period.label} (--regenerate drafts it again)",
                report_id=str(existing[site_id]),
            )
        else:
            try:
                made = await generate(
                    client_id,
                    slug,
                    period_text,
                    actor=actor,
                    months=months,
                    model_client=model_client,
                    settings=settings,
                )
            except Exception as exc:  # one site must not stop the rest
                reason = f"{type(exc).__name__}: {exc}"[:300]
                outcome = SiteOutcome(slug, name, "failed", reason)
                await audit.write(
                    AuditEventType.REPORT_GENERATED,
                    AuditOutcome.ERROR,
                    error_type=type(exc).__name__,
                    error_message=reason,
                    payload={"site": slug, "period": period.key, "batch": True},
                )
            else:
                facts = made.document.facts
                from_vendors = [f for f in facts.facts.values() if f.source != "config"]
                if from_vendors:
                    outcome = SiteOutcome(
                        slug,
                        name,
                        "drafted",
                        _drafted_detail(made.document),
                        report_id=str(made.report_id),
                    )
                else:
                    why = facts.gaps[0].why if facts.gaps else "no source returned anything"
                    outcome = SiteOutcome(
                        slug,
                        name,
                        "failed",
                        f"every source failed ({why[:160]}); the stored draft is empty",
                        report_id=str(made.report_id),
                    )
        batch.outcomes.append(outcome)
        if on_site is not None:
            on_site(outcome)
    return batch


async def rebuild(
    document: ReviewDocument,
    *,
    actor: str,
    model_client: AsyncAnthropic | None = None,
    settings: Settings | None = None,
) -> ReviewDocument:
    """Re-draft the sections after the findings changed (a gap confirmed or dismissed).

    The facts are not collected again: a review is of the facts it was
    generated from, and re-reading the vendors mid-review would move them.
    """
    settings = settings or get_settings()
    config = FranchiseeConfig.model_validate(document.franchisee)
    audit = ReportAudit(
        actor_id=actor, client_id=uuid.UUID(document.client_id), client_ref=document.client_slug
    )
    fresh = await build_sections(
        Context(
            facts=document.facts,
            findings=document.findings,
            config=config,
            period=document.period,
            site_name=document.site_name,
            client_name=document.client_name,
            has_prior=document.prior_facts is not None,
            prior=document.prior_facts,
            prior_label=document.prior_period_label,
            model_client=_model(settings, model_client),
            settings=settings,
            audit=audit,
        )
    )
    document.sections = _carry_over(fresh, document.sections)
    return document


async def research_industry(
    document: ReviewDocument,
    industry: str,
    *,
    actor: str,
    model_client: AsyncAnthropic | None = None,
    settings: Settings | None = None,
) -> ReviewDocument:
    """The engineer names the industry; the trends are researched and await their approval.

    Only the industry string goes to the model. The result replaces the
    section's draft and clears any approval: what was approved is gone.
    """
    cleaned = " ".join(industry.split())
    if not 3 <= len(cleaned) <= 80:
        raise review.ReviewError("name the industry in a few words, e.g. 'Commercial real estate'")
    for name in (document.site_name, document.client_name):
        if name and name.casefold() in cleaned.casefold():
            raise review.ReviewError(
                "name the industry, not the customer: this text is sent to a web search"
            )
    settings = settings or get_settings()
    audit = ReportAudit(
        actor_id=actor, client_id=uuid.UUID(document.client_id), client_ref=document.client_slug
    )
    draft = await research_trends(
        cleaned,
        never_in_a_query=[document.site_name, document.client_name],
        model_client=_model(settings, model_client),
        settings=settings,
        audit=audit,
    )
    section = document.section("trends")
    section.items = draft.items
    section.problems = draft.problems
    section.narrative = "model" if draft.items else "none"
    section.status = "awaiting_engineer"
    section.approved_by = section.approved_at = None
    section.data = {
        "industry": cleaned,
        "industry_from": f"engineer:{actor}",
        "searches": " | ".join(draft.searches),
    }
    section.reason = (
        f"Researched for the {cleaned} industry (named by {actor}); every trend carries its "
        "source. Web research is never rendered without an engineer's approval."
        if draft.items
        else f"Research for the {cleaned} industry produced nothing usable."
    )
    section.waiting_for = (
        "Read each trend against its source, edit or remove, then approve."
        if draft.items
        else "Write the trends by hand, or try a more specific industry."
    )
    return document


def unresolved(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.needs_confirmation and f.confirmed_by is None]


async def render_report(
    report_id: uuid.UUID | str,
    *,
    client_ref: str | uuid.UUID,
    actor: str,
    mode: RenderMode = "client",
    settings: Settings | None = None,
    output: Path | None = None,
) -> RenderResult:
    """Render a stored review. `mode` decides who the deck is for (`render.RenderMode`).

    Only a client render marks the review as rendered: an internal or demo deck is a
    by-product, and the review's state should not say it has been delivered.
    """
    settings = settings or get_settings()
    async with session_scope() as session:
        client = await resolve_client(session, client_ref)
        _, document = await store.load(session, report_id, client_id=client.id)
        client_id, client_slug = client.id, client.slug
    audit = ReportAudit(actor_id=actor, client_id=client_id, client_ref=client_slug)
    now = dt.datetime.now(dt.UTC)
    target = output or default_output(document, Path(settings.reports_dir), now=now, mode=mode)
    try:
        result = render(document, target, mode=mode)
    except RenderGateError as exc:
        await audit.write(
            AuditEventType.REPORT_RENDERED,
            AuditOutcome.DENIED,
            error_type="RenderGateError",
            error_message=f"{len(exc.problems)} placeholder problem(s)",
            payload={"report_id": str(report_id), "mode": mode, "problems": exc.problems[:50]},
        )
        raise
    async with session_scope() as session:
        row, stored = await store.load(session, report_id, client_id=client_id)
        await store.record_render(session, row, stored, mode=mode, path=str(result.path), by=actor)
    await audit.write(
        AuditEventType.REPORT_RENDERED,
        AuditOutcome.SUCCESS,
        payload={
            "report_id": str(report_id),
            "mode": mode,
            "path": str(result.path),
            "kept": result.kept,
            "dropped": result.dropped,
            "slides_checked": result.gate.slides_checked,
        },
    )
    return result


async def render_monthly_report(
    report_id: uuid.UUID | str,
    *,
    client_ref: str | uuid.UUID,
    actor: str,
    mode: RenderMode = "client",
    settings: Settings | None = None,
    output: Path | None = None,
) -> PdfResult:
    """Render the Monthly Report PDF for a stored review: the deck's second output.

    Same record, same modes, same gate rules (`pdf.render_pdf`). A client PDF needs
    the review's `monthly_report` section approved; the render is remembered under
    `pdf/<mode>` and goes stale with the deck on any change. It never marks the
    review rendered: the client deck is what releases a review.
    """
    settings = settings or get_settings()
    async with session_scope() as session:
        client = await resolve_client(session, client_ref)
        _, document = await store.load(session, report_id, client_id=client.id)
        client_id, client_slug = client.id, client.slug
    audit = ReportAudit(actor_id=actor, client_id=client_id, client_ref=client_slug)
    now = dt.datetime.now(dt.UTC)
    target = output or default_pdf_output(document, Path(settings.reports_dir), now=now, mode=mode)
    try:
        result = render_pdf(document, target, mode=mode, now=now)
    except RenderGateError as exc:
        await audit.write(
            AuditEventType.REPORT_RENDERED,
            AuditOutcome.DENIED,
            error_type="RenderGateError",
            error_message=f"{len(exc.problems)} problem(s)",
            payload={
                "report_id": str(report_id),
                "output": "pdf",
                "mode": mode,
                "problems": exc.problems[:50],
            },
        )
        raise
    async with session_scope() as session:
        row, stored = await store.load(session, report_id, client_id=client_id)
        await store.record_render(
            session, row, stored, mode=mode, path=str(result.path), output="pdf"
        )
    await audit.write(
        AuditEventType.REPORT_RENDERED,
        AuditOutcome.SUCCESS,
        payload={
            "report_id": str(report_id),
            "output": "pdf",
            "mode": mode,
            "path": str(result.path),
            "pages": result.pages,
            "sections": result.sections,
            "omitted": [f"{what}: {why}" for what, why in result.omitted],
            "texts_checked": result.texts_checked,
        },
    )
    return result

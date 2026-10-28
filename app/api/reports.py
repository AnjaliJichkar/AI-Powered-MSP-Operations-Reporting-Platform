"""The business-review (TBR) API: generate, review, render, download.

Every route is scoped to the session's client, and a report is only ever found
through it: another client's report id is a 404, not a 403, so an id cannot be
used even to learn that a report exists (hard rule 1).

**Two audiences, and they see different things.** Inputiv staff see a review
while it is being made: drafts, held findings, the fact-id trace behind every
sentence, and the internal and demo render modes. A franchisee sees a review
only once it has been *released* — approved and rendered in client mode — and
then only what the customer's own deck says. The workings are ours.

**A franchisee's administrator makes reviews of their own customers, and still
does not see the workings.** They generate one, settle the findings that were
held, approve the Monthly Report, render the client deck and PDF, and release
it. Those are decisions about their own estate and their own customer, and the
person who presents the review is the person who decides when it goes out.
Everything behind the words stays ours: the fact-id trace, an unreleased
review of a customer they did not generate, the internal and demo renders, the
drafting of sentences, and the Industry Trends approval, which puts
web-research output on a deck and is the one section an engineer still signs.
That split is `CAN_MAKE_REVIEWS` (who may act) against `Scope.sees_drafts`
(who may look), and they are deliberately not the same list.

**Every route is a server-side check.** The page hides what a role may not do,
and that is a courtesy: `ACTIONS_FOR_FRANCHISEE` and the checks beside it are
what refuse it. A franchisee's own client comes from their organisation and
never from a request, so another organisation's customer is not a thing they
can ask for at all — it is a 404 before any of this is reached.

`released` is not a fourth status: it is "this review has a client-mode deck
rendered from exactly its current content". Rendering in client mode is the act
of releasing, and any later change to a review empties `renders` (see
`app/reports/tbr/review.py`), so a released review cannot drift from the deck
that was released. A draft, a superseded row, or a review whose only decks are
internal or demo, is not visible to a franchisee at all.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import CAN_MAKE_REVIEWS, CLIENT_READ
from app.auth.scope import CAN_MAKE_REVIEWS as CAN_MAKE_REVIEWS_ROLES
from app.auth.scope import Scope
from app.connectors.errors import ConnectorError, UnknownClient, UnknownSite
from app.connectors.factory import list_client_sites, resolve_client, resolve_site
from app.db.session import session_scope
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, vendor_label
from app.reports.tbr import pipeline, review, store
from app.reports.tbr.audit import ReportAudit
from app.reports.tbr.config import load_config
from app.reports.tbr.gate import RenderGateError
from app.reports.tbr.limits import GenerationLimitReached, check_generation_allowed
from app.reports.tbr.model import ReviewDocument
from app.reports.tbr.pdf import MONTHLY_SECTION
from app.reports.tbr.render import RenderMode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reports/tbr", tags=["reports"])

#: The only render mode a franchisee may ask for, and the one that defines a
#: released review. `internal` carries the fact-id trace and `demo` carries
#: pseudonyms of other people's data; neither is theirs to see.
CLIENT_MODE: RenderMode = "client"


class ReportRequest(BaseModel):
    """Base for report bodies. No `client_id`, no `user_id`, no extra fields."""

    model_config = ConfigDict(extra="forbid")


class GenerateRequest(ReportRequest):
    site: str = Field(min_length=1, description="Site slug, one of this client's.")
    period: str = Field(pattern=r"^\d{4}-\d{2}$", description="Last month of the period.")
    months: int | None = Field(default=None, ge=1, le=24)


class ReviewRequest(ReportRequest):
    action: Literal[
        "approve",
        "add",
        "edit",
        "remove",
        "confirm",
        "dismiss",
        "scores",
        "industry",
        "notes",
        "contact_names",
    ]
    section: str | None = None
    finding: str | None = None
    index: int | None = None
    text: str | None = Field(default=None, max_length=2000)
    nps: str | None = None
    csat: str | None = None
    pqual: str | None = None
    comments: list[str] = Field(default_factory=list, max_length=3)
    show: bool | None = Field(
        default=None,
        description=(
            "For `contact_names`: true names people on this review, false hides them, "
            "omitted clears the override so the franchisee's `show_people_names` applies."
        ),
    )


class RenderRequest(ReportRequest):
    mode: RenderMode = CLIENT_MODE
    """`client` (default): no trace in the notes. `internal`: the fact-id trace.
    `demo`: pseudonyms and a "Sample data" stamp. See `render.RenderMode`."""
    output: store.Output = "deck"
    """`deck` (default): the PowerPoint review. `pdf`: the Monthly Report, rendered
    from the same stored facts under the same mode and gate."""


#: The review actions a franchisee's own administrator may take.
#:
#: `confirm` and `dismiss` settle a finding that was *held*: a claim that
#: something is absent, which one tool saw and a second could not corroborate.
#: Settling it is a judgement about their own estate — they know whether that
#: server really has no backup — and it is the judgement the held state exists
#: to ask for.
#:
#: `approve` is on the list for exactly one section and the route checks which
#: (`FRANCHISEE_APPROVABLE`): the Monthly Report's approval is the gate on a
#: client PDF, so without it they could release a deck and never a PDF of a
#: review they made themselves.
#:
#: Everything else is drafting, and drafting is ours: `add`, `edit` and
#: `remove` write the sentences a customer reads, `scores` and `notes` put an
#: engineer's words on a slide, `contact_names` decides whether a customer's
#: own people are named in a document that gets passed around, and `industry`
#: runs web research whose output no franchisee signs off.
FRANCHISEE_REVIEW_ACTIONS: frozenset[str] = frozenset({"confirm", "dismiss", "approve"})

#: The only section a franchisee's administrator may approve. Industry Trends
#: is the other section that requires approval and is deliberately absent:
#: a trend is web-research output on a customer's deck and an engineer signs it.
FRANCHISEE_APPROVABLE: frozenset[str] = frozenset({MONTHLY_SECTION})


def _refuse_review_action(action: str, section: str | None) -> HTTPException | None:
    """Why a franchisee's administrator may not take this action, or `None`.

    Returned rather than raised so the caller decides the shape; the message is
    what the person reads, so it says what they *can* do rather than only what
    they cannot.
    """
    if action not in FRANCHISEE_REVIEW_ACTIONS:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Inputiv writes the review. You can confirm or dismiss the findings "
                "you are asked about, approve the Monthly Report, and release it."
            ),
        )
    if action == "approve" and section not in FRANCHISEE_APPROVABLE:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inputiv approves this section. You can approve the Monthly Report.",
        )
    return None


def is_released(row_status: str, document: ReviewDocument) -> bool:
    """Whether this review has been released to the customer it is about."""
    return row_status == "rendered" and CLIENT_MODE in document.renders


CLIENT_PDF = store.render_key("pdf", CLIENT_MODE)


def _released_renders(document: ReviewDocument) -> list[str]:
    """What a franchisee may download: the client deck, and the client PDF when one
    has been rendered from the same content."""
    return [key for key in (CLIENT_MODE, CLIENT_PDF) if key in document.renders]


def _internal_view(report_id: Any, row_status: str, document: ReviewDocument) -> dict[str, Any]:
    """Everything: the workings, for the people who make the review."""
    return {
        "report_id": str(report_id),
        "status": row_status,
        "released": is_released(row_status, document),
        "site": document.site_name,
        "site_slug": document.site_slug,
        "period": document.period.label,
        "generated_at": document.generated_at,
        "generated_by": document.generated_by,
        "released_by": document.released_by,
        "released_at": document.released_at,
        "slides": review.status_report(document),
        "sections": [section.model_dump(mode="json") for section in document.sections],
        "findings": [
            {**finding.model_dump(mode="json"), "assertable": finding.assertable}
            for finding in document.findings
        ],
        "facts": [fact.model_dump(mode="json") for fact in document.facts.facts.values()],
        "gaps": [gap.model_dump(mode="json") for gap in document.facts.gaps],
        # Which decks exist for the review as it stands. Modes only: never a server path.
        "renders": sorted(document.renders),
        # The override (None: none), who set it, the franchisee default and what applies.
        "show_contact_names": document.show_contact_names,
        "show_contact_names_by": document.show_contact_names_by,
        "people_names_default": document.people_names_default,
        "people_names_shown": document.people_names_shown,
    }


def _released_view(report_id: Any, row_status: str, document: ReviewDocument) -> dict[str, Any]:
    """What the customer's own people see: the deck's words, and nothing behind them.

    Deliberately not the internal view with fields removed one by one. It is
    built from the parts a released review is *made of*, so a field added to
    the document later does not leak by default: it has to be added here.

    Left out on purpose: every fact (an internal-only one is not shown to a
    customer at all, and the rest are already in the sentences), the fact-id
    trace on each item, findings that are held for an engineer, the collection
    gaps, and any render mode but `client`.
    """
    return {
        "report_id": str(report_id),
        "status": row_status,
        "released": True,
        "site": document.site_name,
        "site_slug": document.site_slug,
        "period": document.period.label,
        "sections": [
            {
                "key": section.key,
                "slide": section.slide,
                "title": section.title,
                "items": [
                    {"role": item.role, "lead": item.lead, "text": item.text}
                    for item in section.items
                ],
            }
            for section in document.sections
            if section.status == "filled"
        ],
        "renders": _released_renders(document),
    }


#: Every connector, by the name a reader calls it. Built once and looked up,
#: rather than `ConnectorName(key)` per source: a lookup says "this is a
#: connector and here is its name" and cannot raise on a key that is not one,
#: which is the shape `tests/coverage/test_naming.py` exists to keep out.
VENDOR_NAMES: dict[str, str] = {name.value: vendor_label(name.value) for name in ConnectorName}


def _finding_tools(finding: Any) -> list[str]:
    """The tools behind a finding, as a reader's words.

    A finding's `sources` also carries `derived`, which is how we worked
    something out rather than somewhere it came from; on a card it reads as a
    second vendor. Only real connectors survive, and each becomes the name the
    vendor is called — never the key. The page already draws it this way for
    Inputiv staff; here the filtering is done on the server, because a key a
    franchisee may not see should not be sent to their browser at all.
    """
    return [VENDOR_NAMES[key] for key in finding.sources if key in VENDOR_NAMES]


def _admin_view(
    report_id: Any, row_status: str, document: ReviewDocument, *, scope: Scope
) -> dict[str, Any]:
    """What a franchisee's own administrator works on: their review, not its workings.

    Built from the parts rather than by removing fields from the internal view,
    for the reason `_released_view` is: a field added to the document later
    does not leak by default, it has to be added here.

    They get what they are being asked to act on — which slides are filled,
    which are waiting, and the findings held for somebody to settle — and none
    of what proves it. Left out on purpose: every fact and its definition, the
    fact-id trace under each sentence and each finding, the collection gaps,
    the raw source keys, whether a sentence was written by the model or fell
    back to code, why a model draft was rejected, and any render mode but the
    client one.
    """
    return {
        "report_id": str(report_id),
        "status": row_status,
        "released": is_released(row_status, document),
        "site": document.site_name,
        "site_slug": document.site_slug,
        "period": document.period.label,
        "generated_at": document.generated_at,
        "released_at": document.released_at,
        # Whether this is one of theirs, which is what decides whether they may
        # regenerate it. A user id is not sent: who at Inputiv drafted a review
        # is our business, and "Inputiv" is the whole of the answer they need.
        "mine": document.generated_by == scope.actor_id,
        "slides": [
            {
                "slide": row["slide"],
                "title": row["title"],
                "status": row["status"],
                "on_deck": row["on_deck"],
                "why": row["why"],
                "waiting_for": row["waiting_for"],
            }
            for row in review.status_report(document)
        ],
        "sections": [
            {
                "key": section.key,
                "slide": section.slide,
                "title": section.title,
                "status": section.status,
                "approved_by": bool(section.approved_by),
                "requires_approval": section.requires_approval,
                "items": [
                    {"role": item.role, "lead": item.lead, "text": item.text}
                    for item in section.all_items()
                ],
            }
            for section in document.sections
        ],
        # Only the ones they are being asked about. A finding that is already
        # asserted needs nothing from them, and one that was dismissed is a
        # decision somebody has made.
        "held_findings": [
            {
                "id": finding.id,
                "section": finding.section,
                "severity": finding.severity,
                "headline": finding.headline,
                "tools": _finding_tools(finding),
                "confirmed": finding.confirmed_by is not None,
            }
            for finding in document.findings
            if finding.needs_confirmation
        ],
        "renders": _released_renders(document),
        "monthly_section": MONTHLY_SECTION,
    }


def _view(
    report_id: Any, row_status: str, document: ReviewDocument, *, scope: Scope
) -> dict[str, Any]:
    """The review as this session may read it.

    Three audiences, not two. Inputiv staff see the workings; a franchisee's
    administrator sees the review they are making and none of the workings; a
    technician or a viewer sees a released review as the customer reads it.
    """
    if scope.sees_drafts:
        return _internal_view(report_id, row_status, document)
    if scope.role in CAN_MAKE_REVIEWS_ROLES:
        return _admin_view(report_id, row_status, document, scope=scope)
    return _released_view(report_id, row_status, document)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such report")


def _required(value: Any, name: str) -> Any:
    if value is None or value == "":
        raise HTTPException(status_code=422, detail=f"`{name}` is required for this action")
    return value


async def _check_may_generate(scope: Scope, site_slug: str, month: str) -> None:
    """A franchisee's allowance for this customer, and whose review this is.

    Two refusals, both before any connector is read and any model is called,
    because the point of a limit is not to spend the money and then decline to
    show the result.
    """
    if scope.sees_drafts:
        # Inputiv staff: no allowance to spend, and nothing of ours to protect
        # from us. `limits.py` says why.
        return
    async with session_scope() as session:
        client = await resolve_client(session, scope.require_client())
        site = await resolve_site(session, client, site_slug)
        config = await load_config(session, client.id, client_name=client.name)
        await check_generation_allowed(
            session,
            client_id=client.id,
            site_id=site.id,
            site_name=site.name,
            allowed=config.self_service.generations_per_site_per_month,
        )
        existing = await store.current_for_month(session, client.id, site.id, month)
        existing_by = existing.created_by if existing is not None else None
    if existing_by is not None and existing_by != scope.actor_id:
        # Generating over a period that already has a review supersedes it.
        # Superseding somebody else's work is not a self-service act: the
        # engineer who drafted it may be mid-review, and the row they were
        # working on would quietly become the previous period's snapshot.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This period already has a review that Inputiv is working on. "
                "Ask them to regenerate it."
            ),
        )


@router.post("", status_code=201)
async def generate(
    request: GenerateRequest, scope: Annotated[Scope, Depends(CAN_MAKE_REVIEWS)]
) -> dict[str, Any]:
    """Collect facts, derive findings, draft every section and store a draft.

    Inputiv staff and a franchisee's own administrator. Generating reads every
    connector for the customer and costs model calls, so a franchisee's own
    generations are counted against a per-customer monthly allowance
    (`app/reports/tbr/limits.py`), and one that would supersede a review they
    did not make is refused. The result is not shown to the customer until it
    has been reviewed and released, whoever generated it.
    """
    try:
        await _check_may_generate(scope, request.site, request.period)
    except GenerationLimitReached as exc:
        # 429 and not 403: they are allowed to do this, they have done it
        # enough times this month, and the message says when they may again.
        raise HTTPException(status_code=429, detail=exc.message) from exc
    except (UnknownClient, UnknownSite) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        made = await pipeline.generate(
            str(scope.require_client()),
            request.site,
            request.period,
            actor=scope.actor_id,
            months=request.months,
        )
    except (UnknownClient, UnknownSite) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ConnectorError as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    return _view(made.report_id, "draft", made.document, scope=scope)


def _may_open(row_status: str, document: ReviewDocument, *, scope: Scope) -> bool:
    """Whether this session may open this review at all.

    Inputiv staff always may. A franchisee's administrator may, because they
    make reviews of their own customers and a review they cannot open is one
    they cannot finish — their own *and* ours, since settling a held finding is
    the thing we ask them for. Everybody else sees a review once it has been
    released, and before that is told nothing, not even that a draft exists.
    """
    if scope.sees_drafts or scope.role in CAN_MAKE_REVIEWS_ROLES:
        return True
    return is_released(row_status, document)


@router.get("")
async def list_reports(scope: Annotated[Scope, Depends(CLIENT_READ)]) -> list[dict[str, Any]]:
    """Reviews for this client.

    Inputiv staff get every one with who generated and who released it. A
    franchisee's administrator gets every one of their own customers', drafts
    included, because those are reviews they are working on. A technician or a
    viewer gets the released ones.
    """
    async with session_scope() as session:
        client = await resolve_client(session, scope.require_client())
        rows = await store.list_for_client(session, client.id)
        listed: list[dict[str, Any]] = []
        for row in rows:
            renders = (row.document.get("renders") or {}) if isinstance(row.document, dict) else {}
            released = row.status == "rendered" and CLIENT_MODE in renders
            may_make = scope.role in CAN_MAKE_REVIEWS_ROLES
            if not released and not may_make:
                continue
            entry: dict[str, Any] = {
                "report_id": str(row.id),
                "site_slug": row.document.get("site_slug"),
                "site": row.document.get("site_name"),
                "period": (row.document.get("period") or {}).get("label"),
                "status": row.status,
                "released": released,
            }
            if scope.sees_drafts:
                entry["created_by"] = row.created_by
                entry["created_at"] = row.created_at
                entry["released_by"] = row.document.get("released_by")
                entry["released_at"] = row.document.get("released_at")
            elif may_make:
                # Theirs or ours, and nothing finer: a user id of one of our
                # engineers is not something a customer's MSP is handed.
                entry["mine"] = row.created_by == scope.actor_id
            listed.append(entry)
        return listed


@router.get("/sites")
async def list_sites(scope: Annotated[Scope, Depends(CLIENT_READ)]) -> list[dict[str, str]]:
    """This client's active sites, for the picker. Names and slugs only, never ids."""
    async with session_scope() as session:
        client = await resolve_client(session, scope.require_client())
        sites = await list_client_sites(session, client.id)
        return [{"slug": site.slug, "name": site.name} for site in sites]


@router.get("/{report_id}")
async def get_report(
    report_id: str, scope: Annotated[Scope, Depends(CLIENT_READ)]
) -> dict[str, Any]:
    """One review. A 404 for one this session may not open — never a 403, because
    whether an unreleased draft exists is not something a customer is told."""
    async with session_scope() as session:
        client = await resolve_client(session, scope.require_client())
        try:
            row, document = await store.load(session, report_id, client_id=client.id)
        except store.ReportNotFound as exc:
            raise _not_found() from exc
        if not _may_open(row.status, document, scope=scope):
            # 404 and not 403: whether an unreleased draft exists is not
            # something the customer is told, any more than another client's
            # report id is.
            raise _not_found()
        return _view(row.id, row.status, document, scope=scope)


@router.post("/{report_id}/review")
async def review_report(
    report_id: str, request: ReviewRequest, scope: Annotated[Scope, Depends(CAN_MAKE_REVIEWS)]
) -> dict[str, Any]:
    """One action on a review. Returns the review as it now stands.

    Most of this screen is ours: drafting the sentences a customer reads,
    putting an engineer's words on a slide, and deciding whether the customer's
    own people are named are not decisions the customer makes.

    A franchisee's own administrator may do three of them —
    `FRANCHISEE_REVIEW_ACTIONS` — and the refusal is here rather than on the
    page: settling a held finding is a judgement about their own estate, and
    approving the Monthly Report is the gate on the PDF they are entitled to
    release.
    """
    if not scope.sees_drafts:
        refusal = _refuse_review_action(request.action, request.section)
        if refusal is not None:
            raise refusal
    client_uuid = scope.require_client()
    try:
        async with session_scope() as session:
            client = await resolve_client(session, client_uuid)
            _, document = await store.load(session, report_id, client_id=client.id)
            client_id, client_slug = client.id, client.slug
    except UnknownClient as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except store.ReportNotFound as exc:
        raise _not_found() from exc

    by = scope.actor_id
    rebuild = False
    try:
        if request.action == "approve":
            review.approve(document, _required(request.section, "section"), by=by)
        elif request.action == "add":
            review.add_item(
                document,
                _required(request.section, "section"),
                _required(request.text, "text"),
                by=by,
            )
        elif request.action == "edit":
            review.edit_item(
                document,
                _required(request.section, "section"),
                _required(request.index, "index"),
                _required(request.text, "text"),
                by=by,
            )
        elif request.action == "remove":
            review.remove_item(
                document,
                _required(request.section, "section"),
                _required(request.index, "index"),
                by=by,
            )
        elif request.action == "confirm":
            review.confirm_finding(document, _required(request.finding, "finding"), by=by)
            rebuild = True
        elif request.action == "dismiss":
            review.dismiss_finding(document, _required(request.finding, "finding"), by=by)
            rebuild = True
        elif request.action == "scores":
            review.set_scores(
                document,
                nps=request.nps,
                csat=request.csat,
                pqual=request.pqual,
                comments=request.comments,
                by=by,
            )
        elif request.action == "contact_names":
            review.set_contact_names(document, request.show, by=by)
        elif request.action == "notes":
            review.set_talk_track(
                document, _required(request.section, "section"), request.text or "", by=by
            )
        elif request.action == "industry":
            document = await pipeline.research_industry(
                document, _required(request.text, "text"), actor=by
            )
        if rebuild:
            document = await pipeline.rebuild(document, actor=by)
    except review.ReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async with session_scope() as session:
        row, _ = await store.load(session, report_id, client_id=client_id)
        await store.update(session, row, document)
        row_status = row.status
    await ReportAudit(
        actor_id=by,
        client_id=client_id,
        client_ref=client_slug,
        actor_user_id=scope.user_id,
    ).write(
        AuditEventType.REPORT_REVIEWED,
        AuditOutcome.SUCCESS,
        payload={"report_id": report_id, **request.model_dump(exclude_none=True)},
    )
    return _view(report_id, row_status, document, scope=scope)


@router.post("/{report_id}/render")
async def render_report(
    report_id: str, request: RenderRequest, scope: Annotated[Scope, Depends(CAN_MAKE_REVIEWS)]
) -> dict[str, Any]:
    """Render the deck, or with `output: pdf` the Monthly Report. 409 with the list
    of problems if the render gate refuses.

    Rendering the deck in client mode is the act of releasing a review, and a
    franchisee's own administrator may do it: the person who presents the
    review to the customer decides when the customer has it.

    **They may ask for the client mode and no other.** `internal` carries the
    fact-id trace and `demo` carries pseudonyms of somebody else's estate, and
    neither is theirs — which is the same refusal `download` already makes, in
    the same words, because a mode they cannot read is not one they can write.
    """
    if not scope.sees_drafts and request.mode != CLIENT_MODE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the client deck and PDF are available to you",
        )
    try:
        if request.output == "pdf":
            pdf = await pipeline.render_monthly_report(
                report_id,
                client_ref=str(scope.require_client()),
                actor=scope.actor_id,
                mode=request.mode,
            )
            return {
                "report_id": report_id,
                "output": "pdf",
                "mode": pdf.mode,
                "file": pdf.path.name,
                "pages": pdf.pages,
                "sections": pdf.sections,
                "omitted": [{"what": what, "why": why} for what, why in pdf.omitted],
                "gate": {"passed": True, "texts_checked": pdf.texts_checked},
            }
        result = await pipeline.render_report(
            report_id,
            client_ref=str(scope.require_client()),
            actor=scope.actor_id,
            mode=request.mode,
        )
    except UnknownClient as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except store.ReportNotFound as exc:
        raise _not_found() from exc
    except RenderGateError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "the render gate refused the file", "problems": exc.problems[:50]},
        ) from exc
    return {
        "report_id": report_id,
        "output": "deck",
        "mode": result.mode,
        "file": result.path.name,
        "kept": result.kept,
        "dropped": result.dropped,
        "gate": {
            "passed": True,
            "slides_checked": result.gate.slides_checked,
            "paragraphs_checked": result.gate.runs_checked,
        },
    }


@router.get("/{report_id}/download")
async def download(
    report_id: str,
    scope: Annotated[Scope, Depends(CLIENT_READ)],
    mode: RenderMode = CLIENT_MODE,
    output: store.Output = "deck",
) -> FileResponse:
    """The rendered deck, or with `output=pdf` the Monthly Report. A franchisee may
    only ever ask for the client one of either."""
    if not scope.sees_drafts and mode != CLIENT_MODE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the client deck and PDF are available to you",
        )
    key = store.render_key(output, mode)
    what = "Monthly Report PDF" if output == "pdf" else "deck"
    async with session_scope() as session:
        client = await resolve_client(session, scope.require_client())
        try:
            row, document = await store.load(session, report_id, client_id=client.id)
        except store.ReportNotFound as exc:
            raise _not_found() from exc
        if not _may_open(row.status, document, scope=scope):
            raise _not_found()
        # `renders` is emptied whenever the review changes, so an entry is never stale.
        # A deck rendered before modes existed is not served: its notes hold the trace.
        recorded = document.renders.get(key)
        path = Path(recorded) if recorded else None
    if path is None or not path.is_file():
        raise HTTPException(
            status_code=409,
            detail=f"no {mode} {what} has been rendered since this review last changed",
        )
    return FileResponse(
        path,
        filename=path.name,
        media_type=(
            "application/pdf"
            if output == "pdf"
            else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
    )

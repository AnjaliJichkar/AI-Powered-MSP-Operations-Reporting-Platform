"""/coverage/*: the board, a site's devices, the CSV, and exclusions.

The board's JSON is `/coverage/board`, not `/coverage`: that path is the
page, and `create_app` refuses two routes on one path rather than let
registration order decide which answers.

Reads never compute. They serve the latest stored snapshot
(`snapshots.latest_snapshot`, the board the daily collector or a Refresh
wrote) with the client's live exclusions laid over it, say when it was taken
and by whom, and add what only the database knows: the trend and which of
the latest board's gaps the one before did not have. `POST /coverage/refresh`
computes a new board in the background, one at a time per client
(`refresh.REFRESHES`), and `GET /coverage/refresh` is its status, which the
page polls. Every read is `CLIENT_READ`, so a viewer sees the board; the
writes — excluding a device from a tool, withdrawing that, and a refresh —
are `CAN_MANAGE_COVERAGE`, a franchisee's administrator and Inputiv staff.
The client comes from the session and nowhere else (hard rule 1); a site is
only ever looked up within it, and another client's slug is a 404.

The CSV is a download of the whole device list — one row per device, then
one row per vendor record no device claimed (unmanaged, external) — for the
engineer who wants it in a spreadsheet. It carries device names, as the page
does; it is the same data with a different content type. Same-named vendor
records are never collapsed: a machine the board reports once as three rows
of one name is three lines here, each with its vendor id.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CAN_MANAGE_COVERAGE, CLIENT_READ
from app.auth.scope import Scope
from app.connectors.errors import ConnectorError
from app.connectors.factory import resolve_client, resolve_site
from app.coverage.compute import CoverageReport
from app.coverage.config import COVERAGE_TOOLS, STALE_RULES
from app.coverage.core import STATES, Unmanaged
from app.coverage.duplication import duplication_rows
from app.coverage.exclusions import (
    Actor,
    ExclusionError,
    as_record,
    exclude,
    list_exclusions,
    withdraw,
)
from app.coverage.refresh import REFRESHES
from app.coverage.snapshots import (
    board_from_snapshot,
    latest_snapshot,
    new_gaps,
    recent_snapshots,
    trend,
)
from app.db.session import get_session
from app.models.coverage import CoverageExclusion, CoverageSnapshot
from app.models.enums import ConnectorName, Interface
from app.models.site import Site
from app.models.tool_mapping import ToolMapping
from app.settings import get_settings

router = APIRouter(prefix="/coverage", tags=["coverage"])


def _actor(scope: Scope) -> Actor:
    return Actor(
        actor_id=scope.actor_id,
        email=scope.user.email,
        user_id=scope.user_id,
        client_id=scope.require_client(),
        client_slug=scope.client_slug,
    )


async def _stored(
    db: AsyncSession, scope: Scope
) -> tuple[CoverageReport | None, CoverageSnapshot | None]:
    """The latest stored board with the live exclusions over it, and its row."""
    client_id = scope.require_client()
    exclusions = [as_record(row) for row in await list_exclusions(db, client_id)]
    row = await latest_snapshot(db, client_id)
    return board_from_snapshot(row, exclusions), row


async def _board(db: AsyncSession, scope: Scope) -> CoverageReport:
    """The stored board, or 404 when none has been computed yet."""
    report, _ = await _stored(db, scope)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail="No coverage board has been computed for this client yet; refresh it.",
        )
    return report


def _snapshot_view(row: CoverageSnapshot | None, now: dt.datetime) -> dict[str, Any] | None:
    """The stored board's provenance, with how old it is decided here.

    The age is a figure the page prints, so its verdict and its definition
    come with it rather than being a rule written into a script — the same
    reason every ticket statistic carries its own definition. A board the
    collector has not rewritten is not wrong, it is *old*, and the difference
    matters: one three weeks old disagreed with the Devices page about every
    machine on the demo tenant while printing a date that read as ordinary.
    """
    if row is None:
        return None
    age_seconds = int((now - row.taken_at).total_seconds())
    age_days = age_seconds // 86_400
    limit = get_settings().coverage_board_stale_days
    return {
        "taken_on": row.taken_on.isoformat(),
        "taken_at": row.taken_at,
        "taken_by": row.taken_by,
        "age_seconds": age_seconds,
        "age_days": age_days,
        "age_words": _age_words(age_days),
        "old": age_days >= limit,
        "age_definition": (
            f"The board is read from the last stored snapshot. The daily collector "
            f"writes one a day, so one {limit} days old or more means it has not run; "
            f"Refresh recomputes it from the tools now."
        ),
        "duration_ms": row.duration_ms,
        "has_board": row.board is not None,
        "reads": row.summary.get("reads", {}),
    }


async def _directory_sites(db: AsyncSession, client_id: uuid.UUID) -> list[str]:
    """The slugs of the sites that have a Microsoft 365 tenant mapped.

    One query, live rather than from the snapshot, because a tenant mapped
    this morning is a true thing to say about a board taken last night: the
    mapping is not a measurement.
    """
    rows = (
        await db.execute(
            select(Site.slug)
            .join(ToolMapping, ToolMapping.site_id == Site.id)
            .where(
                ToolMapping.client_id == client_id,
                ToolMapping.connector_name == ConnectorName.MICROSOFT365,
            )
        )
    ).scalars()
    return sorted(set(rows))


def _age_words(days: int) -> str:
    """How old the board is, in the words somebody would say."""
    if days <= 0:
        return "taken today"
    if days == 1:
        return "a day old"
    return f"{days} days old"


def _iso(when: dt.datetime | None) -> str:
    return when.isoformat() if when else ""


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "key": tool.key,
            "label": tool.label,
            "assessed": tool.assessed,
            "gap": tool.gap,
            "rules": [
                {"key": r.key, "label": r.label, "days": r.days, "definition": r.definition}
                for r in STALE_RULES
                if r.tool == tool.key
            ],
        }
        for tool in COVERAGE_TOOLS
    ]


def _exclusion_view(row: CoverageExclusion) -> dict[str, Any]:
    return {
        "id": row.id,
        "site_id": str(row.site_id),
        "device_id": row.device_id,
        "device": row.device_name,
        "tool": row.tool,
        "reason": row.reason,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "removed_at": row.removed_at,
        "removed_by": row.removed_by,
    }


@router.get("/board")
async def board(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
) -> dict[str, Any]:
    """The per-site matrix, worst first, from the latest stored board, with the trend,
    what is new since the snapshot before it, and the refresh's status."""
    client_id = scope.require_client()
    now = dt.datetime.now(dt.UTC)
    report, row = await _stored(db, scope)
    refresh = REFRESHES.status_of(client_id).as_dict()
    if report is None:
        return {
            "snapshot": _snapshot_view(row, now),
            "refresh": refresh,
            "computed_at": None,
            "managed": 0,
            "not_computers": 0,
            "rmm_stale": 0,
            "rmm_agent_down": 0,
            "rmm_stale_days": 0,
            "unmanaged": 0,
            "discovered": 0,
            "external": 0,
            "not_assessable": 0,
            "population_truncated": False,
            "duplication": [],
            "tools": [{**entry, "reason": "", "truncated": False} for entry in _tools()],
            "states": list(STATES),
            "sites": [],
            "new_gaps": [],
            "new_gaps_since": None,
            "trend": [],
            "may_exclude": scope.role in CAN_MANAGE_COVERAGE.roles,
        }
    snapshots = await recent_snapshots(db, report.client_id)
    previous = [s for s in snapshots if row is None or s.id != row.id]
    fresh, since = new_gaps(report, previous[-1] if previous else None)
    return {
        "snapshot": _snapshot_view(row, now),
        "refresh": refresh,
        "computed_at": report.computed_at,
        "duration_ms": report.duration_ms,
        "managed": len(report.devices),
        "not_computers": report.not_computers,
        "rmm_stale": report.rmm_stale,
        "rmm_agent_down": report.rmm_agent_down,
        "rmm_stale_days": report.rmm_stale_days,
        "unmanaged": len(report.unmanaged),
        "discovered": len(report.discovered),
        "external": len(report.external),
        "not_assessable": len(report.not_assessable),
        "population_truncated": report.population_truncated,
        "duplication": await duplication_rows(db, report),
        "tools": [
            {
                **entry,
                **report.totals()[entry["key"]].as_dict(),
                "reason": report.tool(entry["key"]).reason,
                "truncated": report.tool(entry["key"]).truncated,
                "assessed": report.tool(entry["key"]).assessed,
            }
            for entry in _tools()
        ],
        "states": list(STATES),
        "sites": [site.as_dict() for site in report.sites],
        # Which customers have a Microsoft 365 tenant mapped. **Not a coverage
        # figure**: this board is agents on machines, and an account is not an
        # agent, so Microsoft 365 is not in `COVERAGE_TOOLS` and never will be
        # by default. The page shows it as a column that says connected or
        # not, with its own words, because "does this customer have a tenant we
        # can read" is a question people ask of this page and the answer is one
        # we hold. Anything more about a tenant is the Microsoft 365 reads, not
        # this join.
        "directory_sites": await _directory_sites(db, client_id),
        "new_gaps": fresh,
        "new_gaps_since": since,
        "trend": trend(snapshots),
        "may_exclude": scope.role in CAN_MANAGE_COVERAGE.roles,
    }


@router.get("/sites/{slug}")
async def site_devices(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
) -> dict[str, Any]:
    """One site's devices, with each tool's state and last-seen, and its unmanaged endpoints."""
    client = await resolve_client(db, scope.require_client())
    try:
        site = await resolve_site(db, client, slug)
    except ConnectorError as exc:
        raise HTTPException(status_code=404, detail="no such site for this client") from exc
    report = await _board(db, scope)
    row = await latest_snapshot(db, client.id)
    summary = report.site(site.slug)
    live = {(e.device_id, e.tool): e for e in report.exclusions}
    devices = [
        {
            "device_id": d.device.device_id,
            "device": d.device.name,
            "node_class": d.device.node_class,
            "ninjaone": {
                "state": d.ninjaone_state,
                "detail": d.ninjaone_detail,
                "last_seen": d.device.last_contact,
            },
            "tools": {
                key: {
                    "state": state.state,
                    "detail": state.detail,
                    "last_seen": state.last_seen,
                    "matched_by": state.matched_by,
                    "candidates": list(state.candidates),
                    "found_by": state.found_by,
                    "exclusion_id": (
                        live[(d.device.device_id, key)].id
                        if (d.device.device_id, key) in live
                        else None
                    ),
                }
                for key, state in d.tools.items()
            },
            "has_gap": d.has_gap,
            "agent_down": d.agent_down,
        }
        for d in sorted(
            report.devices_at(site.slug), key=lambda d: (not d.has_gap, d.device.name.lower())
        )
    ]
    return {
        "site_slug": site.slug,
        "site": site.name,
        "computed_at": report.computed_at,
        "snapshot": _snapshot_view(row, dt.datetime.now(dt.UTC)),
        "summary": summary.as_dict() if summary else None,
        "tools": _tools(),
        "devices": devices,
        "rmm_stale_days": report.rmm_stale_days,
        "unmanaged": [
            _unmanaged_view(u)
            for u in sorted(report.unmanaged_at(site.id), key=lambda u: (u.tool, u.name.lower()))
        ],
        "discovered": [
            _unmanaged_view(u)
            for u in sorted(report.discovered_at(site.id), key=lambda u: (u.tool, u.name.lower()))
        ],
        "external": [
            _unmanaged_view(u)
            for u in sorted(report.external_at(site.id), key=lambda u: (u.tool, u.name.lower()))
        ],
        "may_exclude": scope.role in CAN_MANAGE_COVERAGE.roles,
    }


def _unmanaged_view(u: Unmanaged) -> dict[str, Any]:
    return {
        "tool": u.tool,
        "device": u.name,
        "detail": u.detail,
        "last_seen": u.last_seen,
        "note": u.note,
        "vendor_ids": list(u.external_ids),
        "rows": max(1, len(u.external_ids)),
    }


@router.get("/export.csv")
async def export_csv(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
    site: Annotated[str | None, Query()] = None,
) -> Response:
    """Every device and tool as rows, then every unclaimed vendor record, one line
    each, for a spreadsheet. `site` narrows to one."""
    report = await _board(db, scope)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["site", "device", "device_id", "node_class", "ninjaone_state", "ninjaone_last_contact"]
        + [f"{tool.key}_state" for tool in COVERAGE_TOOLS]
        + [f"{tool.key}_detail" for tool in COVERAGE_TOOLS]
        + [f"{tool.key}_last_seen" for tool in COVERAGE_TOOLS]
        + [f"{tool.key}_matched_by" for tool in COVERAGE_TOOLS]
        + [f"{tool.key}_candidates" for tool in COVERAGE_TOOLS]
        + ["vendor_id", "note"]
    )
    for d in report.devices:
        if site and d.device.site_slug != site:
            continue
        writer.writerow(
            [
                d.device.site_name,
                d.device.name,
                d.device.device_id,
                d.device.node_class or "",
                d.ninjaone_state,
                _iso(d.device.last_contact),
            ]
            + [d.tools[t.key].state for t in COVERAGE_TOOLS]
            + [d.tools[t.key].detail for t in COVERAGE_TOOLS]
            + [_iso(d.tools[t.key].last_seen) for t in COVERAGE_TOOLS]
            + [d.tools[t.key].matched_by or "" for t in COVERAGE_TOOLS]
            + ["; ".join(d.tools[t.key].candidates) for t in COVERAGE_TOOLS]
            + [
                "",
                "RMM agent down: "
                + "; ".join(
                    sorted(
                        t.key for t in COVERAGE_TOOLS if d.tools[t.key].state == "rmm_agent_down"
                    )
                )
                if d.agent_down
                else "",
            ]
        )
    names: dict[Any, str] = {s.site_id: s.name for s in report.sites}
    slugs: dict[Any, str] = {s.site_id: s.slug for s in report.sites}
    for kind, entries in (("unmanaged", report.unmanaged), ("external", report.external)):
        for u in entries:
            if site and slugs.get(u.site_id) != site:
                continue
            # One line per vendor record: same-named rows are never collapsed.
            for vendor_id in u.external_ids or ("",):
                writer.writerow(
                    [names.get(u.site_id, str(u.site_id)), u.name, "", kind, "", ""]
                    + [(kind if t.key == u.tool else "") for t in COVERAGE_TOOLS]
                    + [(u.detail if t.key == u.tool else "") for t in COVERAGE_TOOLS]
                    + [(_iso(u.last_seen) if t.key == u.tool else "") for t in COVERAGE_TOOLS]
                    + ["" for _ in COVERAGE_TOOLS]
                    + ["" for _ in COVERAGE_TOOLS]
                    + [vendor_id, u.note or ""]
                )
    filename = f"coverage-{scope.client_slug or 'client'}{'-' + site if site else ''}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/duplication.csv")
async def export_duplication(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
) -> Response:
    """Every site with more than one vendor organisation on one tool, one row per
    organisation, for the franchisee to consolidate on the vendor side."""
    report = await _board(db, scope)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["site", "site_slug", "tool", "organisation", "external_id", "endpoints", "with_agent"]
    )
    for entry in await duplication_rows(db, report):
        for organisation in entry["organisations"]:
            writer.writerow(
                [
                    entry["site"],
                    entry["site_slug"],
                    entry["tool"],
                    organisation["name"],
                    organisation["external_id"],
                    organisation["endpoints"],
                    organisation["with_agent"],
                ]
            )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="vendor-duplication-{scope.client_slug or "client"}.csv"'
            )
        },
    )


@router.post("/refresh", status_code=202)
async def start_refresh(
    scope: Annotated[Scope, Depends(CAN_MANAGE_COVERAGE)],
) -> dict[str, Any]:
    """Compute a new board in the background and store it as today's snapshot.

    One computation per client at a time: a second request while one runs
    joins it. The page polls `GET /coverage/refresh` and reloads when done.
    """
    status, started = REFRESHES.start(
        scope.require_client(),
        actor=scope.user.email,
        actor_user_id=scope.user_id,
        interface=Interface.API,
    )
    return {**status.as_dict(), "started": started}


@router.get("/refresh")
async def refresh_status(
    scope: Annotated[Scope, Depends(CLIENT_READ)],
) -> dict[str, Any]:
    """Whether a refresh is running for this client, and how the last one ended."""
    return REFRESHES.status_of(scope.require_client()).as_dict()


@router.get("/exclusions")
async def exclusions(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
    include_removed: Annotated[bool, Query()] = False,
) -> list[dict[str, Any]]:
    """Every exclusion in force for this client — who, when and why — for review."""
    rows = await list_exclusions(db, scope.require_client(), include_removed=include_removed)
    return [_exclusion_view(row) for row in rows]


class ExclusionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_slug: str
    device_id: str = Field(description="NinjaOne's device id, from the site's device list.")
    device: str = Field(description="The hostname, for the record and the audit row.")
    tool: str
    reason: str = Field(
        min_length=1, description="Why this device is not expected to have the tool."
    )


@router.post("/exclusions", status_code=201)
async def add_exclusion(
    body: ExclusionRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_MANAGE_COVERAGE)],
) -> dict[str, Any]:
    """Exclude one device from one tool, with a reason. Recorded with the user and the time."""
    client = await resolve_client(db, scope.require_client())
    try:
        site = await resolve_site(db, client, body.site_slug)
    except ConnectorError as exc:
        raise HTTPException(status_code=404, detail="no such site for this client") from exc
    try:
        row = await exclude(
            db,
            _actor(scope),
            site_id=site.id,
            device_id=body.device_id,
            device_name=body.device,
            tool=body.tool,
            reason=body.reason,
        )
    except ExclusionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return _exclusion_view(row)


@router.delete("/exclusions/{exclusion_id}")
async def remove_exclusion(
    exclusion_id: int,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CAN_MANAGE_COVERAGE)],
) -> dict[str, Any]:
    """Withdraw an exclusion. The row stays, marked withdrawn by whom and when."""
    try:
        row = await withdraw(db, _actor(scope), exclusion_id=exclusion_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExclusionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return _exclusion_view(row)

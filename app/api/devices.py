"""/devices/*: the identity layer on a page — every machine across every vendor.

The page's JSON is `/devices/list`, not `/devices`: that path is the page.
Reads come from the tables the daily resolution writes (`app/devices/`):
which vendors hold a record of each device, when each last returned it, and
by which pass the record was paired. Nothing here runs the join; the list is
the links as of the last resolution, and says when that was.

One device's detail reaches out through the tools an engineer would call —
`get_device`, `get_endpoint_protection`, `get_asset_vulnerabilities`,
`search_tickets` — each given the name the vendor's own link carries, so the
machine is found under whatever that vendor calls it. No other vendor call is
made, and each one is a `tool_call` audit row (hard rule 4). A vendor that
errors is shown as an error, never as a blank (hard rule 7).

Who sees what: every role reads the list and a device (`CLIENT_READ`); the
unresolved queue, the agreement and the two writes — linking a record to a
device and unlinking one, both with a reason — are Inputiv staff only
(`INPUTIV_ANY`), because a link changes the coverage numbers a franchisee
reads. The client comes from the session (hard rule 1); a device of another
client is a 404, as is a link, and a franchisee's request for the queue is a
403 that is audited like any refusal.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.auth.dependencies import CLIENT_READ, INPUTIV_ANY
from app.auth.scope import Scope
from app.connectors.autotask.tools import search_tickets
from app.connectors.bitdefender.tools import get_endpoint_protection
from app.connectors.connectsecure.tools import get_asset_vulnerabilities
from app.connectors.errors import ConnectorError
from app.connectors.ninjaone.tools import get_device
from app.coverage.core import NOT_ONBOARDED, is_default_windows_name
from app.db.session import get_session
from app.devices.admin import (
    DeviceAdminError,
    DeviceNotFound,
    Operator,
    RecordTaken,
    link,
    unlink,
)
from app.devices.agreement import agreement_view, latest_agreement
from app.models.device import DEVICE_STATUSES, Device, DeviceUnresolved, DeviceVendorLink
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface
from app.models.site import Site
from app.models.tool_mapping import ToolMapping
from app.settings import get_settings

router = APIRouter(prefix="/devices", tags=["devices"])

#: The vendors that hold device records, in the order the page shows them.
#: Autotask holds tickets, not devices, and appears only on the detail view.
VENDORS: tuple[ConnectorName, ...] = (
    ConnectorName.NINJAONE,
    ConnectorName.BITDEFENDER,
    ConnectorName.CONNECTSECURE,
    ConnectorName.COVE,
)
VENDOR_LABELS = {
    ConnectorName.NINJAONE: "NinjaOne",
    ConnectorName.BITDEFENDER: "Bitdefender",
    ConnectorName.CONNECTSECURE: "ConnectSecure",
    ConnectorName.COVE: "Cove",
}
TICKET_DAYS = 90
FINDINGS_SHOWN = 10


def _operator(scope: Scope) -> Operator:
    return Operator(
        actor_id=scope.actor_id,
        client_id=scope.require_client(),
        client_slug=scope.client_slug,
        actor_user_id=scope.user_id,
        interface=Interface.API,
    )


def _link_view(row: DeviceVendorLink) -> dict[str, Any]:
    return {
        "link_id": row.id,
        "device_id": str(row.device_id),
        "connector": row.connector_name.value,
        "external_id": row.external_id,
        "vendor_name": row.vendor_name,
        "matched_by": row.matched_by,
        "active": row.active,
        "first_seen": row.first_seen_at,
        "last_seen": row.last_seen_at,
        "note": row.note,
    }


async def _sites(db: AsyncSession, client_id: uuid.UUID) -> dict[uuid.UUID, Site]:
    rows = (await db.execute(select(Site).where(Site.client_id == client_id))).scalars().all()
    return {site.id: site for site in rows}


async def _links_by_device(
    db: AsyncSession, client_id: uuid.UUID, *, active_only: bool = True
) -> dict[uuid.UUID, list[DeviceVendorLink]]:
    query = select(DeviceVendorLink).where(DeviceVendorLink.client_id == client_id)
    if active_only:
        query = query.where(DeviceVendorLink.active.is_(True))
    grouped: dict[uuid.UUID, list[DeviceVendorLink]] = defaultdict(list)
    for row in (await db.execute(query.order_by(DeviceVendorLink.id))).scalars():
        grouped[row.device_id].append(row)
    return grouped


def _vendor_presence(links: list[DeviceVendorLink]) -> dict[str, dict[str, Any]]:
    """Per vendor: how many active records, when the latest was returned, and
    how the first was paired. Absent vendors are listed too, as not present."""
    presence: dict[str, dict[str, Any]] = {}
    for vendor in VENDORS:
        mine = [row for row in links if row.connector_name is vendor]
        presence[vendor.value] = {
            "present": bool(mine),
            "links": len(mine),
            "last_seen": max((row.last_seen_at for row in mine), default=None),
            "matched_by": sorted({row.matched_by for row in mine}),
            "names": sorted({row.vendor_name for row in mine if row.vendor_name}),
        }
    return presence


async def _mapped_tools(db: AsyncSession, client_id: uuid.UUID) -> dict[str, list[str]]:
    """Per site slug, the tools that customer has an organisation mapped on.

    The coverage board's rule, on this page: a tool with no company for a site
    is `not_assessed` there, never `absent`. Without it every machine on a
    customer with no Cove reads "Missing" under Backup, which is a gap asserted
    about a product they do not buy — and two pages then disagree about the
    same fleet, which is worse than either answer.

    One query for the whole client rather than a connector opened per site and
    tool: this is a list endpoint, and the mapping is what the board checks.
    """
    rows = (
        await db.execute(
            select(Site.slug, ToolMapping.connector_name)
            .join(ToolMapping, ToolMapping.site_id == Site.id)
            .where(ToolMapping.client_id == client_id, ToolMapping.site_id.is_not(None))
        )
    ).all()
    mapped: dict[str, set[str]] = defaultdict(set)
    for slug, connector in rows:
        mapped[slug].add(connector.value)
    return {slug: sorted(tools) for slug, tools in mapped.items()}


def _device_row(device: Device, site: Site | None, links: list[DeviceVendorLink]) -> dict[str, Any]:
    return {
        "device_id": str(device.id),
        "site_slug": site.slug if site else str(device.site_id),
        "site": site.name if site else str(device.site_id),
        "device": device.canonical_name,
        "status": device.status,
        "node_class": device.node_class,
        "os": device.os,
        "first_seen": device.first_seen_at,
        "last_seen": device.last_seen_at,
        "vendors": _vendor_presence(links),
        # A machine only other vendors hold, still called what Windows called it at
        # install: most likely not yet onboarded to NinjaOne.
        "note": NOT_ONBOARDED
        if not any(link.active and link.connector_name is ConnectorName.NINJAONE for link in links)
        and is_default_windows_name(device.canonical_name)
        else None,
    }


class ListFilters:
    """The three filters, validated once and applied the same way to the list and the CSV."""

    def __init__(
        self,
        site: Annotated[str | None, Query(description="a site slug")] = None,
        status: Annotated[
            str | None, Query(description="active, rmm_stale, rmm_agent_down or retired")
        ] = None,
        missing: Annotated[
            str | None, Query(description="a vendor the device has no active record in")
        ] = None,
    ) -> None:
        if status is not None and status not in DEVICE_STATUSES:
            raise HTTPException(
                status_code=422, detail=f"status must be one of {', '.join(DEVICE_STATUSES)}"
            )
        if missing is not None and missing not in {v.value for v in VENDORS}:
            raise HTTPException(
                status_code=422,
                detail=f"missing must be one of {', '.join(v.value for v in VENDORS)}",
            )
        self.site = site
        self.status = status
        self.missing = missing

    def keep(self, row: dict[str, Any]) -> bool:
        if self.site and row["site_slug"] != self.site:
            return False
        if self.status and row["status"] != self.status:
            return False
        return not (self.missing and row["vendors"][self.missing]["present"])


async def _rows(
    db: AsyncSession, client_id: uuid.UUID
) -> tuple[list[dict[str, Any]], dict[uuid.UUID, Site]]:
    sites = await _sites(db, client_id)
    links = await _links_by_device(db, client_id)
    devices = (
        (await db.execute(select(Device).where(Device.client_id == client_id))).scalars().all()
    )
    rows = [_device_row(d, sites.get(d.site_id), links.get(d.id, [])) for d in devices]
    rows.sort(key=lambda r: (r["site"].lower(), r["device"].lower()))
    return rows, sites


@router.get("/list")
async def list_devices(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
    filters: Annotated[ListFilters, Depends()],
) -> dict[str, Any]:
    """Every device the resolution knows, per site, with each vendor's presence and last seen."""
    client_id = scope.require_client()
    rows, sites = await _rows(db, client_id)
    resolved_at = (
        await db.execute(
            select(func.max(DeviceVendorLink.last_seen_at)).where(
                DeviceVendorLink.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    kept = [row for row in rows if filters.keep(row)]
    mapped = await _mapped_tools(db, client_id)
    by_status = {
        status: sum(1 for r in rows if r["status"] == status) for status in DEVICE_STATUSES
    }
    return {
        "resolved_at": resolved_at,
        "identity_enabled": get_settings().device_identity_enabled,
        "vendors": [{"key": v.value, "label": VENDOR_LABELS[v]} for v in VENDORS],
        # Which tools a customer actually has, so a column can say "not
        # assessed" instead of "Missing" where there is nothing to miss.
        "tools_by_site": mapped,
        "statuses": list(DEVICE_STATUSES),
        "sites": sorted(
            (
                {"slug": site.slug, "name": site.name, "active": site.is_active}
                for site in sites.values()
                if any(r["site_slug"] == site.slug for r in rows)
            ),
            key=lambda s: s["name"].lower(),
        ),
        "counts": {"total": len(rows), "shown": len(kept), "by_status": by_status},
        "filters": {"site": filters.site, "status": filters.status, "missing": filters.missing},
        "devices": kept,
        "may_link": scope.role in INPUTIV_ANY.roles,
    }


@router.get("/export.csv")
async def export_csv(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
    filters: Annotated[ListFilters, Depends()],
) -> Response:
    """The list as rows, under the same filters, for a spreadsheet."""
    rows, _ = await _rows(db, scope.require_client())

    def iso(when: dt.datetime | None) -> str:
        return when.isoformat() if when else ""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["site", "device", "device_id", "status", "node_class", "os", "first_seen", "last_seen"]
        + [f"{v.value}_present" for v in VENDORS]
        + [f"{v.value}_records" for v in VENDORS]
        + [f"{v.value}_last_seen" for v in VENDORS]
        + [f"{v.value}_matched_by" for v in VENDORS]
        + [f"{v.value}_names" for v in VENDORS]
    )
    for row in rows:
        if not filters.keep(row):
            continue
        vendors = row["vendors"]
        writer.writerow(
            [
                row["site"],
                row["device"],
                row["device_id"],
                row["status"],
                row["node_class"] or "",
                row["os"] or "",
                iso(row["first_seen"]),
                iso(row["last_seen"]),
            ]
            + ["yes" if vendors[v.value]["present"] else "no" for v in VENDORS]
            + [vendors[v.value]["links"] for v in VENDORS]
            + [iso(vendors[v.value]["last_seen"]) for v in VENDORS]
            + ["; ".join(vendors[v.value]["matched_by"]) for v in VENDORS]
            + ["; ".join(vendors[v.value]["names"]) for v in VENDORS]
        )
    parts = [scope.client_slug or "client"]
    parts += [value for value in (filters.site, filters.status, filters.missing) if value]
    filename = "devices-" + "-".join(parts) + ".csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- the queue and the agreement: Inputiv staff -------------------------------------


@router.get("/unresolved")
async def unresolved(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """The records no pass could place without guessing, each with its candidate devices."""
    client_id = scope.require_client()
    sites = await _sites(db, client_id)
    rows = (
        (
            await db.execute(
                select(DeviceUnresolved)
                .where(
                    DeviceUnresolved.client_id == client_id,
                    DeviceUnresolved.resolved_at.is_(None),
                )
                .order_by(DeviceUnresolved.first_seen_at, DeviceUnresolved.id)
            )
        )
        .scalars()
        .all()
    )
    candidate_ids: set[uuid.UUID] = set()
    for row in rows:
        for candidate in row.candidates:
            try:
                candidate_ids.add(uuid.UUID(str(candidate.get("device_id"))))
            except ValueError:
                continue
    devices: dict[uuid.UUID, Device] = {}
    if candidate_ids:
        found = (
            await db.execute(
                select(Device).where(Device.client_id == client_id, Device.id.in_(candidate_ids))
            )
        ).scalars()
        devices = {d.id: d for d in found}
    entries = []
    for row in rows:
        site = sites.get(row.site_id)
        candidates = []
        for candidate in row.candidates:
            try:
                device_id = uuid.UUID(str(candidate.get("device_id")))
            except ValueError:
                continue
            device = devices.get(device_id)
            candidates.append(
                {
                    "device_id": str(device_id),
                    "device": device.canonical_name if device else candidate.get("name"),
                    "status": device.status if device else None,
                    "known": device is not None,
                }
            )
        entries.append(
            {
                "id": row.id,
                "site_slug": site.slug if site else str(row.site_id),
                "site": site.name if site else str(row.site_id),
                "connector": row.connector_name.value,
                "external_id": row.external_id,
                "vendor_name": row.vendor_name,
                "found_by": row.found_by,
                "first_seen": row.first_seen_at,
                "last_seen": row.last_seen_at,
                "candidates": candidates,
            }
        )
    return {"count": len(entries), "entries": entries}


@router.get("/agreement")
async def agreement(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """The latest daily comparison of the board through the join and through the links."""
    row = await latest_agreement(db, scope.require_client())
    return {
        "identity_enabled": get_settings().device_identity_enabled,
        "latest": agreement_view(row),
    }


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: uuid.UUID
    connector: ConnectorName
    external_id: str = Field(min_length=1, description="The vendor's id for the record.")
    vendor_name: str | None = Field(default=None, description="The name the vendor shows.")
    reason: str = Field(min_length=1, description="Why this record is that device's.")


class UnlinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, description="Why the record is not that device's.")


def _admin_error(exc: DeviceAdminError) -> HTTPException:
    if isinstance(exc, DeviceNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RecordTaken):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.post("/links", status_code=201)
async def add_link(
    body: LinkRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """Tie a vendor record to a device by hand, with a reason. Audited; the queue entry closes."""
    try:
        row = await link(
            db,
            _operator(scope),
            device_id=body.device_id,
            connector=body.connector,
            external_id=body.external_id,
            reason=body.reason,
            vendor_name=body.vendor_name,
        )
    except DeviceAdminError as exc:
        raise _admin_error(exc) from exc
    await db.commit()
    return _link_view(row)


@router.post("/links/{link_id}/unlink")
async def remove_link(
    link_id: int,
    body: UnlinkRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(INPUTIV_ANY)],
) -> dict[str, Any]:
    """Withdraw a link, with a reason. The row stays inactive; the record is queued again."""
    try:
        row = await unlink(db, _operator(scope), link_id=link_id, reason=body.reason)
    except DeviceAdminError as exc:
        raise _admin_error(exc) from exc
    await db.commit()
    return _link_view(row)


# --- one device across every vendor -------------------------------------------------

ToolCall = Callable[[], Awaitable[BaseModel]]


async def _read(
    db: AsyncSession, scope: Scope, *, connector: ConnectorName, tool: str, call: ToolCall
) -> dict[str, Any]:
    """One tool read for the page, audited as the tool call it is. An error is the answer."""
    started = time.monotonic()
    context = AuditContext.new(
        actor_id=scope.actor_id,
        actor_user_id=scope.user_id,
        interface=Interface.API,
        client_id=scope.client_id,
        client_ref=scope.client_slug,
    )
    try:
        result = await call()
    except (ConnectorError, ValueError) as exc:
        await record_event(
            db,
            context,
            event_type=AuditEventType.TOOL_CALL,
            outcome=AuditOutcome.ERROR,
            connector_name=connector,
            tool_name=tool,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_type=type(exc).__name__,
            error_message=str(exc),
            payload={"source": "devices page"},
        )
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    await record_event(
        db,
        context,
        event_type=AuditEventType.TOOL_CALL,
        outcome=AuditOutcome.SUCCESS,
        connector_name=connector,
        tool_name=tool,
        duration_ms=int((time.monotonic() - started) * 1000),
        payload={"source": "devices page"},
    )
    return {"available": True, "data": result.model_dump(mode="json")}


def _first_active(
    links: Sequence[DeviceVendorLink], vendor: ConnectorName
) -> DeviceVendorLink | None:
    return next((r for r in links if r.connector_name is vendor and r.active), None)


def _findings_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """The confirmed findings in brief: counts, the worst few, the scan date."""
    if not payload.get("available"):
        return payload
    data = payload["data"]
    return {
        "available": True,
        "data": {
            "name": data.get("name"),
            "matched_on": data.get("matched_on"),
            "population": data.get("population"),
            "last_scanned": data.get("last_scanned"),
            "days_since_scan": data.get("days_since_scan"),
            "risk_score": data.get("risk_score"),
            "risk_grade": data.get("risk_grade"),
            "counts_by_severity": data.get("counts_by_severity", {}),
            "known_exploited": data.get("known_exploited"),
            "fix_reported_for": data.get("fix_reported_for"),
            "total": data.get("total"),
            "candidates": data.get("candidates", []),
            "vulnerabilities": (data.get("vulnerabilities") or [])[:FINDINGS_SHOWN],
        },
    }


@router.get("/{device_id}")
async def device_detail(
    device_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
) -> dict[str, Any]:
    """One machine: its links with how each was made, and what every vendor says about it."""
    client_id = scope.require_client()
    device = (
        await db.execute(
            select(Device).where(Device.id == device_id, Device.client_id == client_id)
        )
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="no such device for this client")
    site = await db.get(Site, device.site_id)
    order = {vendor: index for index, vendor in enumerate(VENDORS)}
    links = sorted(
        (await db.execute(select(DeviceVendorLink).where(DeviceVendorLink.device_id == device.id)))
        .scalars()
        .all(),
        key=lambda row: (order.get(row.connector_name, len(order)), row.id),
    )
    queued = (
        (
            await db.execute(
                select(DeviceUnresolved).where(
                    DeviceUnresolved.client_id == client_id,
                    DeviceUnresolved.resolved_at.is_(None),
                    DeviceUnresolved.candidates.contains([{"device_id": str(device.id)}]),
                )
            )
        )
        .scalars()
        .all()
    )
    client = str(client_id)
    site_ref = str(device.site_id)
    not_linked = {"available": False, "linked": False}
    vendors: dict[str, Any] = {}
    # Each vendor is asked by the name its own link carries, so the machine is
    # found under whatever that vendor calls it; a vendor with no active link
    # is not asked at all. These are the tools an engineer would call, and
    # nothing beyond them.
    ninja = _first_active(links, ConnectorName.NINJAONE)
    if ninja is None:
        vendors["ninjaone"] = not_linked
    else:
        ninja_name = ninja.vendor_name or device.canonical_name
        vendors["ninjaone"] = await _read(
            db,
            scope,
            connector=ConnectorName.NINJAONE,
            tool="get_device",
            call=lambda: get_device(client, ninja_name, site_ref),
        )
    protection = _first_active(links, ConnectorName.BITDEFENDER)
    if protection is None:
        vendors["bitdefender"] = not_linked
    else:
        protection_name = protection.vendor_name or device.canonical_name
        vendors["bitdefender"] = await _read(
            db,
            scope,
            connector=ConnectorName.BITDEFENDER,
            tool="get_endpoint_protection",
            call=lambda: get_endpoint_protection(client, protection_name, site_ref),
        )
    scanner = _first_active(links, ConnectorName.CONNECTSECURE)
    if scanner is None:
        vendors["connectsecure"] = not_linked
    else:
        scanner_name = scanner.vendor_name or device.canonical_name
        vendors["connectsecure"] = _findings_summary(
            await _read(
                db,
                scope,
                connector=ConnectorName.CONNECTSECURE,
                tool="get_asset_vulnerabilities",
                call=lambda: get_asset_vulnerabilities(client, scanner_name, site_id=site_ref),
            )
        )
    vendors["autotask"] = await _read(
        db,
        scope,
        connector=ConnectorName.AUTOTASK,
        tool="search_tickets",
        call=lambda: search_tickets(
            client, device.canonical_name, days=TICKET_DAYS, site_id=site_ref
        ),
    )
    await db.commit()  # the tool_call rows
    return {
        "device": {
            **_device_row(device, site, [r for r in links if r.active]),
            "netbios_key": device.netbios_key,
            "serials": device.serials,
            "macs": device.macs,
        },
        "links": [_link_view(row) for row in links],
        "queued": [
            {
                "id": row.id,
                "connector": row.connector_name.value,
                "external_id": row.external_id,
                "vendor_name": row.vendor_name,
                "found_by": row.found_by,
            }
            for row in queued
        ],
        "vendors": vendors,
        "ticket_days": TICKET_DAYS,
        "may_link": scope.role in INPUTIV_ANY.roles,
    }

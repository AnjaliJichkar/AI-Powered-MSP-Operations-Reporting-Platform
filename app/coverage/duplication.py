"""Vendor duplication: sites with more than one organisation on one connector.

A customer with two GravityZone companies is one site holding both mappings
and read as their union (`ToolMapping`, since migration 0013). That keeps
the board right, but the duplication itself is the franchisee's to clean up
on the vendor side, so it is reported: per site and tool, each organisation
with its name, vendor id, row count and how many rows carry an agent. The
counts come from the board's own reads (`CoverageReport.organisations`); the
names come from the mappings.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.coverage.compute import CoverageReport
from app.models.enums import ConnectorName
from app.models.tool_mapping import ToolMapping


async def mapping_names(session: AsyncSession, client_id: uuid.UUID) -> dict[tuple[str, str], str]:
    """`(connector, external_id) -> the vendor's name` for every site-level mapping."""
    rows = (
        (
            await session.execute(
                select(ToolMapping).where(
                    ToolMapping.client_id == client_id, ToolMapping.site_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        (
            row.connector_name.value
            if isinstance(row.connector_name, ConnectorName)
            else str(row.connector_name),
            str(row.external_id),
        ): row.external_name or ""
        for row in rows
        if row.external_id is not None
    }


async def duplication_rows(session: AsyncSession, report: CoverageReport) -> list[dict[str, Any]]:
    """One entry per (site, tool) with more than one organisation, largest first."""
    names = await mapping_names(session, report.client_id)
    site_names = {s.site_id: (s.slug, s.name) for s in report.sites}
    entries: list[dict[str, Any]] = []
    for (tool, site_id), organisations in report.duplicated().items():
        slug, name = site_names.get(site_id, (str(site_id), str(site_id)))
        entries.append(
            {
                "site": name,
                "site_slug": slug,
                "tool": tool,
                "organisations": [
                    {
                        "external_id": o.external_id,
                        "name": names.get((tool, o.external_id), ""),
                        "endpoints": o.endpoints,
                        "with_agent": o.with_agent,
                    }
                    for o in sorted(organisations, key=lambda o: -o.endpoints)
                ],
            }
        )
    entries.sort(key=lambda e: (-sum(o["endpoints"] for o in e["organisations"]), e["site"]))
    return entries

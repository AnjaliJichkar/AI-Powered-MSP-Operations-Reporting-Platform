"""GET /sites: the client's end customers, and which vendors know each one.

Sites are discovered by `sync_sites` and never typed in, so this is a read and
there is nothing to create here. It exists because the Sites page needs one
listing with the vendor mappings on it: `GET /reports/tbr/sites` answers the
report picker's narrower question (active sites, name and slug) and is left
alone rather than widened into two jobs.

The path is `/sites/list` and not `/sites`, which belongs to the page. Nesting
the data under the feature is what `/reports/tbr` already does; a route table
where one path means a page to a browser and JSON to a script resolves by
registration order, which is not something anybody should have to know.

Scoped like everything else: the client comes from the session, and a site is
only ever listed through it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CLIENT_READ
from app.auth.scope import Scope
from app.db.session import get_session
from app.models.enums import ConnectorName
from app.models.site import Site
from app.models.tool_mapping import ToolMapping

router = APIRouter(tags=["sites"])


class SiteView(BaseModel):
    slug: str
    name: str
    is_active: bool
    connectors: list[str] = Field(
        description="Which vendors hold an organisation for this site, in name order."
    )


class SiteList(BaseModel):
    total: int
    active: int
    returned: int
    sites: list[SiteView]
    connectors: dict[str, int] = Field(
        description="Connector -> how many active sites it covers. For the summary line."
    )


@router.get("/sites/list", response_model=SiteList)
async def list_sites(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
    include_inactive: Annotated[bool, Query()] = False,
) -> SiteList:
    """This client's sites. Inactive ones are left out unless asked for.

    A site is deactivated rather than deleted when a vendor stops listing it,
    so the inactive ones are history worth being able to see; they are just
    not what anybody means by "our customers" on an ordinary day.
    """
    client_id = scope.require_client()
    rows = (
        (await db.execute(select(Site).where(Site.client_id == client_id).order_by(Site.name)))
        .scalars()
        .all()
    )
    mappings = (
        (
            await db.execute(
                select(ToolMapping).where(
                    ToolMapping.client_id == client_id, ToolMapping.site_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )

    by_site: dict[object, list[str]] = {}
    for mapping in mappings:
        by_site.setdefault(mapping.site_id, []).append(
            mapping.connector_name.value
            if isinstance(mapping.connector_name, ConnectorName)
            else str(mapping.connector_name)
        )

    coverage: dict[str, int] = {}
    listed: list[SiteView] = []
    for row in rows:
        connectors = sorted(by_site.get(row.id, []))
        if row.is_active:
            for connector in connectors:
                coverage[connector] = coverage.get(connector, 0) + 1
        if row.is_active or include_inactive:
            listed.append(
                SiteView(
                    slug=row.slug,
                    name=row.name,
                    is_active=row.is_active,
                    connectors=connectors,
                )
            )

    return SiteList(
        total=len(rows),
        active=sum(1 for row in rows if row.is_active),
        returned=len(listed),
        sites=listed,
        connectors=dict(sorted(coverage.items())),
    )

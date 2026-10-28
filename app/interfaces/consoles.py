"""The address of each vendor's own console, for a link out to a record.

The Devices and Tickets pages offer "open this machine in NinjaOne" and "open
this ticket in Autotask". The id in the link is one we already hold — a
`DeviceVendorLink.external_id`, a ticket id — so the only thing missing is
where that vendor's console lives, and that is **not guessable**: a NinjaOne
tenant is on one of several instances (`app`, `eu`, `oc`, `ca`), an Autotask
tenant on one of nineteen numbered zones, and a link to the wrong one takes an
engineer to somebody else's sign-in page. So the address is derived from the
client's own stored connector settings and nothing else, and where it cannot
be derived no link is drawn and the page shows the id to copy instead.

`ToolCredential.config` is the non-secret half of a credential — "base URL,
region, instance, scopes", by its own docstring — and is the only thing read
here. `get_secrets()` is never called; nothing in this module can reach a key.

Two vendors have a console route we can build:

* **NinjaOne** serves its API and its console from one host, so the API base
  URL is the console's: `https://app.ninjarmm.com/#/deviceDashboard/<id>/overview`.
* **Autotask** splits them by name and shares the zone number:
  `webservices15.autotask.net` for the API, `ww15.autotask.net` for the UI,
  and a ticket is `/Mvc/ServiceDesk/TicketDetail.mvc?ticketId=<id>`.

Two do not, and are deliberately absent rather than approximated:
GravityZone's console has no address for one endpoint that survives a session,
and ConnectSecure's is per-tenant and not derivable from the API host. Both
show the vendor id to copy instead.

The routes themselves are recorded in `docs/api-notes/<vendor>.md` and have not
been checked against a live console; if one moves, it moves in one place here
and in `CONSOLE_ROUTES` in `app/static/js/states.js`.
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ConnectorName
from app.models.tool_credential import ToolCredential

#: NinjaOne's console is its API host. Only hosts we recognise as NinjaOne's
#: are used, so a self-hosted proxy or a fake host in a demo tenant yields
#: nothing rather than a link into the void.
_NINJAONE_HOST = re.compile(r"^(?:[a-z0-9-]+\.)?ninjarmm\.com$")

#: Autotask's API host names its zone; the UI host is the same zone, renamed.
_AUTOTASK_API_HOST = re.compile(r"^webservices(\d+)\.autotask\.net$")


def _host(config: dict[str, object]) -> str:
    """The host of a stored base URL, lower-cased, or "" when there is none."""
    raw = config.get("base_url")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    return (urlsplit(raw.strip()).hostname or "").lower()


def _ninjaone_console(config: dict[str, object]) -> str | None:
    host = _host(config)
    if host and _NINJAONE_HOST.match(host):
        return f"https://{host}"
    # A region with no base URL: NinjaOne names its instances the same way its
    # API does, so `eu` is `eu.ninjarmm.com` and the default instance is `app`.
    region = str(config.get("region") or "").strip().lower()
    if region and re.fullmatch(r"[a-z0-9-]+", region):
        return f"https://{region}.ninjarmm.com"
    return None


def _autotask_console(config: dict[str, object]) -> str | None:
    host = _host(config)
    match = _AUTOTASK_API_HOST.match(host)
    if match:
        return f"https://ww{match.group(1)}.autotask.net"
    return None


#: One builder per vendor whose console we can address. A vendor absent from
#: this map has no link on any page, by design.
_BUILDERS = {
    ConnectorName.NINJAONE: _ninjaone_console,
    ConnectorName.AUTOTASK: _autotask_console,
}


async def console_urls(db: AsyncSession, client_id: uuid.UUID | None) -> dict[str, str]:
    """Each vendor console we can address for this client, by connector name.

    A read of the non-secret `config` column only. A client with no credential,
    an inactive one, or a host we do not recognise contributes nothing, and the
    page then draws no link for that vendor.
    """
    if client_id is None:
        return {}
    rows = (
        (
            await db.execute(
                select(ToolCredential).where(
                    ToolCredential.client_id == client_id,
                    ToolCredential.is_active.is_(True),
                    ToolCredential.connector_name.in_(list(_BUILDERS)),
                )
            )
        )
        .scalars()
        .all()
    )
    urls: dict[str, str] = {}
    for row in rows:
        builder = _BUILDERS.get(row.connector_name)
        if builder is None:
            continue
        url = builder(row.config or {})
        if url:
            urls[row.connector_name.value] = url
    return urls

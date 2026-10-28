"""What a device lookup tool asks the identity layer: the vendor's ids for a name.

Flag off, this answers nothing and the tool matches names inline as it
always did. Flag on, a name is resolved to devices (`find_devices`) and their
active links on the vendor, so `get_endpoint_protection("TL-AUS-WS14")` finds
the endpoint GravityZone calls `OLD-NAME` when a MAC paired the two. The tool
contract does not change: the same arguments, the same answer shape, only the
set of records the name can reach.
"""

from __future__ import annotations

import uuid

from app.db.session import session_scope
from app.models.enums import ConnectorName
from app.settings import get_settings


async def linked_ids(
    client_id: str | uuid.UUID,
    connector: ConnectorName,
    device_name: str,
    site_id: str | uuid.UUID | None = None,
) -> set[str]:
    """The vendor ids linked to the device(s) called `device_name`, or an empty set
    when the identity layer is off or knows nothing of the name."""
    if not get_settings().device_identity_enabled:
        return set()
    try:
        client = uuid.UUID(str(client_id))
        site = uuid.UUID(str(site_id)) if site_id is not None else None
    except ValueError:
        return set()
    # Imported here: the tools import this module, and the identity reader
    # imports the coverage config, which imports the tools.
    from app.devices.identity import linked_records

    async with session_scope() as session:
        return set(await linked_records(session, client, connector, device_name, site_id=site))

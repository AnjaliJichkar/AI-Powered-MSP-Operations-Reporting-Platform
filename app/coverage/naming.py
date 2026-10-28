"""The one place a coverage board key becomes a connector, and back.

A board entry is named for **what it measures** and a vendor record is keyed by
**which connector wrote it**. For GravityZone and ConnectSecure the two words
are the same; for Backup they are not — the entry is `backup` and the connector
is `cove` — and the confusion has now been written five separate times:

* the device resolution keyed links by the board key, so every Cove record
  looked new on every run;
* the identity reader compared a link's connector against the board key in two
  places, found nothing, and reported 0% backup coverage where Cove held a
  device for every machine;
* the unresolved queue built `ConnectorName(tool_key)`, which raises
  `ValueError` on `"backup"` — a crash waiting for the first ambiguous Cove
  record;
* and the Devices page asserted a gap on a tool a customer does not own,
  because the two namespaces had no one owner.

So neither word is ever derived at a call site again. `connector_of` and
`board_key_of` are the only translations, `tests/coverage/test_naming.py`
fails if a module writes one of the old shapes by hand, and anything new that
needs the mapping imports it from here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from app.coverage.config import COVERAGE_TOOLS
from app.models.enums import ConnectorName

#: Board key -> the connector whose rows that entry reads.
CONNECTOR_OF: Final[Mapping[str, ConnectorName]] = {
    tool.key: tool.connector for tool in COVERAGE_TOOLS if tool.connector is not None
}

#: The reverse. A connector no board entry reads is simply absent.
BOARD_KEY_OF: Final[Mapping[ConnectorName, str]] = {
    connector: key for key, connector in CONNECTOR_OF.items()
}


def connector_of(key: str) -> ConnectorName | None:
    """The connector behind a board entry, or `None` for an entry with no reader.

    `None` rather than a guess: a board entry can exist before its connector
    does (Backup did, for months), and a caller that gets a connector name back
    for one would go looking for links nothing has written.
    """
    return CONNECTOR_OF.get(key)


def connector_name_of(key: str) -> str:
    """The same, as the string a `DeviceVendorLink` stores.

    Falls back to the key itself, which is right for the two entries whose
    names already match their connector and harmless for any other: a lookup
    on a name nothing wrote finds nothing, rather than raising in the middle
    of a resolution.
    """
    connector = connector_of(key)
    return connector.value if connector is not None else key


def board_key_of(connector: ConnectorName) -> str | None:
    """The board entry that reads this connector, or `None` if none does.

    NinjaOne is the obvious `None`: it is the population this board is about
    rather than one of the tools measured against it.
    """
    return BOARD_KEY_OF.get(connector)

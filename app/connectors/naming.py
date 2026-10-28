"""Which of a vendor's organisations are the franchisee's own.

`SyncRules` decides which vendor organisations are the franchisee's own internal
bookkeeping — a "zz Archived" org in NinjaOne, or a TeamLogic location entered
as a company in Autotask — and should be recorded but left inactive. The
defaults below are a starting point; each client can override them in the
`extra["sync"]` of its tenant-level `ToolMapping` for that connector, so a
franchisee with an unusual naming convention needs a config change, not a
deploy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import ConnectorName
from app.models.site import normalise_name

__all__ = [
    "DEFAULT_SYNC_RULES",
    "EXTRA_KEY",
    "INTEGRATION_SUFFIX",
    "SyncRules",
    "normalise_name",
    "rules_for",
    "vendor_match_key",
]

#: GravityZone appends a six-character lowercase alphanumeric tag in parentheses
#: to companies its RMM integration creates: "Acme Dental (vnghix)" beside a
#: hand-made "Acme Dental". It is not part of the customer's name and is
#: stripped before matching (api-notes/bitdefender.md).
INTEGRATION_SUFFIX = re.compile(r"\s*\([a-z0-9]{6}\)\s*$")


def vendor_match_key(connector: ConnectorName, name: str) -> str:
    """The key a vendor organisation's name is matched on: `normalise_name`, after
    the connector's own decorations are removed."""
    if connector is ConnectorName.BITDEFENDER:
        name = INTEGRATION_SUFFIX.sub("", name)
    return normalise_name(name)


@dataclass(frozen=True)
class SyncRules:
    """Which vendor organisations are the franchisee's own, not a customer's."""

    internal_name_patterns: tuple[str, ...] = ()
    """Regexes matched, case-insensitively, against the raw vendor name."""

    internal_prefixes: tuple[str, ...] = ()
    """Casefolded prefixes of the raw vendor name, e.g. ``zz``."""

    match_tenant_name: bool = True
    """Treat an organisation named like the client itself as internal."""

    compiled: tuple[re.Pattern[str], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "compiled",
            tuple(re.compile(pattern, re.IGNORECASE) for pattern in self.internal_name_patterns),
        )

    def is_internal(self, organisation_name: str, *, client_name: str) -> bool:
        """True when this organisation should be recorded but left inactive."""
        raw = organisation_name.strip()
        folded = raw.casefold()
        if any(folded.startswith(prefix.casefold()) for prefix in self.internal_prefixes):
            return True
        if any(pattern.search(raw) for pattern in self.compiled):
            return True
        if self.match_tenant_name and normalise_name(raw) == normalise_name(client_name):
            return True
        return False


#: What applies when a client has not said otherwise.
DEFAULT_SYNC_RULES: dict[ConnectorName, SyncRules] = {
    # NinjaOne tenants conventionally park archived or template orgs under "zz".
    ConnectorName.NINJAONE: SyncRules(internal_prefixes=("zz",)),
    # A TeamLogic franchisee's Autotask lists its own and sibling locations as
    # companies, and its GravityZone and ConnectSecure tenants hold a company
    # for the franchisee itself. None is a customer. Live on 2026-09-24 a
    # Bitdefender sync would otherwise have reactivated the franchisee's own
    # site once its company was merged onto it.
    ConnectorName.AUTOTASK: SyncRules(internal_name_patterns=(r"\bteamlogic\b",)),
    ConnectorName.BITDEFENDER: SyncRules(internal_name_patterns=(r"\bteamlogic\b",)),
    ConnectorName.CONNECTSECURE: SyncRules(internal_name_patterns=(r"\bteamlogic\b",)),
}

EXTRA_KEY = "sync"


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def rules_for(connector: ConnectorName, extra: Mapping[str, Any] | None = None) -> SyncRules:
    """The defaults for a connector, overridden by a client's ``extra["sync"]``.

    Each key present in the override replaces the default for that key; a key
    that is absent keeps the default. So ``{"internal_prefixes": []}`` switches
    the prefix rule off, while ``{}`` changes nothing.
    """
    base = DEFAULT_SYNC_RULES.get(connector, SyncRules())
    override = (extra or {}).get(EXTRA_KEY)
    if not isinstance(override, Mapping):
        return base
    return SyncRules(
        internal_name_patterns=(
            _strings(override["internal_name_patterns"])
            if "internal_name_patterns" in override
            else base.internal_name_patterns
        ),
        internal_prefixes=(
            _strings(override["internal_prefixes"])
            if "internal_prefixes" in override
            else base.internal_prefixes
        ),
        match_tenant_name=bool(override.get("match_tenant_name", base.match_tenant_name)),
    )

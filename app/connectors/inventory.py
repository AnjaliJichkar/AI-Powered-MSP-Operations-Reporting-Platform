"""An in-process cache of a franchisee's device inventory, per connector.

Written for NinjaOne and shared with every connector that lists devices per
customer (Bitdefender endpoints, later others). `D` is the vendor's device
record and `K` the vendor's device id; the rules below hold for all of them.

`count_devices` and a filtered `list_devices` both need every device the
tenant has, and an engineer asks a few such questions in a row. One tenant-wide
listing every `NINJAONE_INVENTORY_TTL_SECONDS` serves all of them.

The cache is per process and deliberately simple. An entry is keyed by the
client **and the set of sites the call that fetched it targeted**, and it
serves a later call only when it covers every site that call targets:

- an entry fetched for the whole client serves any call for that client (the
  reader narrows it to its own targets);
- an entry fetched for one named site serves that site again, and never a
  wider question. An unscoped call after a site-scoped one misses and lists
  the tenant again.

That rule exists because of a bug found live on 2026-09-18: a site-scoped
fetch was stored under the client alone, so the next unscoped question would
have read one site as the whole franchisee. The connector no longer builds a
narrow inventory by accident, and this key means that if one is ever built
again it still cannot be served wide.

Concurrent misses are single-flighted. When the model asks for two tools at once
the loop runs them together, each with its own connector, and both miss the
same cold cache; without a guard each lists the tenant (seen live on
2026-09-18: one question, two full listings). `flight(client_id)` is a
per-client lock: the first caller fetches, the rest wait, re-check the cache and
are served the entry the first one stored. It is per client rather than per
scope so that a named-site call waiting behind an unscoped one is served by the
whole-client entry instead of fetching its own.

An entry is also tied to the credential version the factory hands the
connector, so replacing a client's credentials makes its entries miss rather
than serve devices read with the old ones. It holds already-scoped rows, so
nothing a guard dropped is ever served from here.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from app.connectors.scope import SiteRef

#: The sites a fetch targeted, by our own site id.
Coverage = frozenset[uuid.UUID]


@dataclass(frozen=True, slots=True)
class InventoryEntry[D, K]:
    rows: list[tuple[SiteRef, D]]
    """The devices of the covered sites, already classified and tagged."""

    device_orgs: dict[K, K]
    """`deviceId -> organizationId` (NinjaOne) or `endpointId -> companyId`
    (Bitdefender) for *every* device the tenant returned, inactive sites
    included. Alerts carry only a `deviceId`, and this is how one is
    classified: an inactive site's device resolves to an org the guard drops
    quietly, while an id missing here is unknown to the tenant."""

    truncated: bool
    version: str | None
    expires_at: float
    """On the monotonic clock: wall-clock changes cannot revive or kill an entry."""

    coverage: Coverage = frozenset()
    """The sites whose devices `rows` holds: the targets of the call that fetched it."""

    source: Literal["tenant", "sites"] = "tenant"
    """Where it was listed from. `tenant`: `/v2/devices`, so `device_orgs` knows
    every device the tenant has and an id missing from it is unknown to the
    vendor. `sites`: the covered sites' own listings, so `device_orgs` knows
    those sites only and an id missing from it is simply out of scope."""


class InventoryCache[D, K]:
    """Device inventories per client and scope, with a TTL. Not shared across processes."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._entries: dict[tuple[uuid.UUID, Coverage], InventoryEntry[D, K]] = {}
        self._flights: dict[uuid.UUID, asyncio.Lock] = {}
        self._clock = clock
        self.hits = 0
        self.misses = 0

    def get(
        self,
        client_id: uuid.UUID,
        *,
        version: str | None,
        targets: Iterable[uuid.UUID],
        recheck: bool = False,
    ) -> InventoryEntry[D, K] | None:
        """A live entry for this client that covers every one of `targets`, or None.

        Coverage is a superset test, never an overlap test: an entry that lacks
        even one targeted site is a miss, so a narrow fetch cannot answer a
        wider question. `recheck` is the second look a caller takes once it
        holds the flight lock: a miss then is the same miss, counted once.
        """
        wanted = frozenset(targets)
        found: InventoryEntry[D, K] | None = None
        for key in [key for key in self._entries if key[0] == client_id]:
            entry = self._entries[key]
            if entry.version != version or self._clock() >= entry.expires_at:
                # Stale, or read with credentials that have since been replaced.
                del self._entries[key]
                continue
            if found is None and wanted <= entry.coverage:
                found = entry
        if found is None:
            if not recheck:
                self.misses += 1
            return None
        self.hits += 1
        return found

    def flight(self, client_id: uuid.UUID) -> asyncio.Lock:
        """The lock a caller holds while it fetches this client's inventory.

        Take it after a miss, then look again before fetching: whoever held it
        first has usually stored what you need.
        """
        return self._flights.setdefault(client_id, asyncio.Lock())

    def put(
        self,
        client_id: uuid.UUID,
        *,
        version: str | None,
        targets: Iterable[uuid.UUID],
        rows: list[tuple[SiteRef, D]],
        device_orgs: dict[K, K],
        truncated: bool,
        ttl_seconds: float,
        source: Literal["tenant", "sites"] = "tenant",
    ) -> InventoryEntry[D, K]:
        """Build the entry, and keep it only when the TTL says to.

        `rows` is cut down to the targeted sites here, not by the caller, so
        an entry can never hold less than its key claims or more than it says.
        """
        coverage = frozenset(targets)
        entry: InventoryEntry[D, K] = InventoryEntry(
            rows=[row for row in rows if row[0].site_id in coverage],
            device_orgs=dict(device_orgs),
            truncated=truncated,
            version=version,
            expires_at=self._clock() + ttl_seconds,
            coverage=coverage,
            source=source,
        )
        if ttl_seconds > 0:
            # A wider entry makes the client's narrower ones redundant.
            for key in [k for k in self._entries if k[0] == client_id and k[1] <= coverage]:
                del self._entries[key]
            self._entries[(client_id, coverage)] = entry
        return entry

    def invalidate(self, client_id: uuid.UUID | None = None) -> None:
        """Drop one client's entries, or every entry."""
        if client_id is None:
            self._entries.clear()
            return
        for key in [key for key in self._entries if key[0] == client_id]:
            del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)

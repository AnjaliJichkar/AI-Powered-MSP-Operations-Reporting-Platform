"""NinjaOne's device inventory cache: the shared cache, typed for NinjaOne devices."""

from __future__ import annotations

from app.connectors.inventory import InventoryCache as _InventoryCache
from app.connectors.inventory import InventoryEntry as _InventoryEntry
from app.connectors.ninjaone.models import NinjaDevice

InventoryEntry = _InventoryEntry[NinjaDevice, int]
InventoryCache = _InventoryCache[NinjaDevice, int]

#: The process-wide cache. Tests install their own through the connector.
INVENTORY: InventoryCache = _InventoryCache()

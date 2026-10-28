"""NinjaOne RMM connector.

Auth is OAuth 2.0 client credentials against `POST /ws/oauth/token`. The access
token lives in memory, in a store shared by every connector built for the same
client and credential version in this process (`tokens.py`), so a run of tool
calls authenticates once. It is refreshed when it expires or when NinjaOne
rejects it; the stored credential is the client id/secret pair, never a token.

Scoping is two levels. The credential belongs to the franchisee; each of its end
customers is a NinjaOne *organisation*, held as a `Site`. Device reads take one
of two paths:

- **No site named** (the call covers the whole allowlist): one tenant-wide
  listing through `GET /v2/devices`, paged, with every device classified by its
  `organizationId` against the allowlist. The inventory is cached per client
  for a short TTL (`app/connectors/ninjaone/inventory.py`), so a run of "how
  many" questions costs one listing rather than one per question.
- **A site named**: `GET /v2/organization/{id}/devices`, which the vendor
  scopes for us. A subset of several sites (not something a tool can express
  today) fans out concurrently under a semaphore.

Alerts have no org-scoped path, so `list_alerts` is one call per organisation
narrowed by `df`, run concurrently under the same semaphore. A live alert
carries a `deviceId` and no organisation (the spec's embedded `device` object
is optional and this tenant omits it), so alerts are classified by resolving
the device through the tenant inventory's `deviceId -> organizationId` map
before the allowlist guard sees them.

`_scoped` checks against the client's whole allowlist, not just the organisation
being queried: a sibling site in a response is a filter bug worth a warning; an
inactive site of this client is dropped quietly (expected on the tenant path);
an organisation we know nothing about is logged as an error and never merged
in. See `app/connectors/scope.py` and docs/api-notes/ninjaone.md.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from typing import Any, ClassVar, Literal

from app.connectors.base import BaseConnector
from app.connectors.errors import AuthError, UpstreamError
from app.connectors.matching import normalise_hostname
from app.connectors.ninjaone.inventory import INVENTORY, InventoryCache, InventoryEntry
from app.connectors.ninjaone.models import (
    NinjaAccessToken,
    NinjaActivity,
    NinjaAlert,
    NinjaAlertDevice,
    NinjaCustomFieldDefinition,
    NinjaDevice,
    NinjaDeviceDetail,
    NinjaLocation,
)
from app.connectors.ninjaone.tokens import TOKENS, TokenKey, TokenStore
from app.connectors.scope import SiteRef, VendorOrganisation
from app.models.enums import ConnectorName
from app.settings import get_settings

logger = logging.getLogger(__name__)

TOKEN_PATH = "/ws/oauth/token"  # noqa: S105 - a URL path, not a secret
ORGANIZATIONS_PATH = "/v2/organizations"
DEVICES_PATH = "/v2/devices"
ORGANIZATION_DEVICES_PATH = "/v2/organization/{organization_id}/devices"
DEVICE_PATH = "/v2/device/{device_id}"
DEVICE_ACTIVITIES_PATH = "/v2/device/{device_id}/activities"
ALERTS_PATH = "/v2/alerts"
ACTIVITIES_PATH = "/v2/activities"

#: The activity log's filter values we have seen honoured (api-notes, 2026-09-21).
#: An unknown `status` is an HTTP 500, so nothing outside these maps is sent.
ACTIVITY_TYPES = {"condition": "CONDITION"}
ACTIVITY_STATUSES = {"reset": "RESET", "triggered": "TRIGGERED"}
#: The vendor returns at least this many rows whatever smaller `pageSize` is asked for.
ACTIVITY_MIN_PAGE = 10
QUERY_PATH = "/v2/queries/{report}"
CUSTOM_FIELD_DEFINITIONS_PATH = "/v2/device-custom-fields"
ORGANIZATION_LOCATIONS_PATH = "/v2/organization/{organization_id}/locations"

#: NinjaOne runs one instance per region; a client's row says which.
REGION_BASE_URLS = {
    "us": "https://app.ninjarmm.com",
    "eu": "https://eu.ninjarmm.com",
    "ca": "https://ca.ninjarmm.com",
    "oc": "https://oc.ninjarmm.com",
}

#: Refresh this many seconds before the token actually expires.
TOKEN_EXPIRY_MARGIN_SECONDS = 60


def _last_id(batch: Sequence[Any]) -> Any | None:
    """The cursor NinjaOne's list endpoints page by: the last row's id."""
    if not batch:
        return None
    last = batch[-1]
    return last.get("id") if isinstance(last, dict) else None


class NinjaOneClient(BaseConnector):
    """Read-only access to one franchisee's NinjaOne organisations."""

    name: ClassVar[ConnectorName] = ConnectorName.NINJAONE
    default_base_url: ClassVar[str] = REGION_BASE_URLS["us"]
    default_scope: ClassVar[str] = "monitoring"

    def __init__(
        self,
        *,
        concurrency: int | None = None,
        inventory_ttl_seconds: float | None = None,
        devices_page_size: int | None = None,
        inventory: InventoryCache | None = None,
        token_store: TokenStore | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._token: str | None = None
        """The token this instance last sent, so a 401 discards that one and no other."""
        # Shared only when the factory said which credential version this is:
        # without one there is no telling whether a stored token still matches
        # the credentials, so the connector keeps its token to itself.
        if token_store is not None:
            self.tokens = token_store
        else:
            self.tokens = TOKENS if self.credential_version is not None else TokenStore()
        self._token_key: TokenKey = (
            self.scope.client_id,
            self.credential_version,
            str(self.config.get("base_url") or self.config.get("region") or "").lower(),
            str(self.config.get("scope") or self.default_scope),
        )
        # One refresh at a time across every connector holding this key, not
        # just within this instance.
        self._auth_lock = self.tokens.lock(self._token_key)
        # Hard rule 6: the fan-out width, cache TTL and page size are settings.
        settings = get_settings()
        self.concurrency = concurrency if concurrency is not None else settings.ninjaone_concurrency
        self.inventory_ttl_seconds = (
            inventory_ttl_seconds
            if inventory_ttl_seconds is not None
            else settings.ninjaone_inventory_ttl_seconds
        )
        self.devices_page_size = (
            devices_page_size
            if devices_page_size is not None
            else settings.ninjaone_devices_page_size
        )
        self.query_page_size = settings.ninjaone_query_page_size
        self.inventory = inventory if inventory is not None else INVENTORY
        self._semaphore: asyncio.Semaphore | None = None

    # --- configuration --------------------------------------------------

    @property
    def base_url(self) -> str:
        """`base_url` in the credential config wins; otherwise `region`; else US."""
        configured = self.config.get("base_url")
        if configured:
            return str(configured).rstrip("/")
        region = str(self.config.get("region", "")).lower()
        if region:
            if region not in REGION_BASE_URLS:
                raise AuthError(
                    f"unknown NinjaOne region {region!r}; expected one of "
                    f"{sorted(REGION_BASE_URLS)}",
                    connector=self.name.value,
                )
            return REGION_BASE_URLS[region]
        return self.default_base_url

    # --- auth -----------------------------------------------------------

    def _is_authenticated(self) -> bool:
        # Read through to the store every time: another connector may have
        # refreshed the token, or discarded the one we last used.
        self._token = self.tokens.get(self._token_key)
        return self._token is not None

    def _invalidate_auth(self) -> None:
        # Only the token the vendor just rejected. If another connector has
        # already replaced it, theirs stands and we pick it up on the retry.
        self.tokens.discard(self._token_key, self._token)
        self._token = None

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def _authenticate(self) -> None:
        """Exchange the client credentials for an access token."""
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.secret("client_id"),
            "client_secret": self.secret("client_secret"),
            "scope": str(self.config.get("scope") or self.default_scope),
        }
        response = await self.request(
            "POST",
            TOKEN_PATH,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            authenticated=False,
        )
        try:
            token = NinjaAccessToken.model_validate(self._decode(response, TOKEN_PATH))
        except UpstreamError:
            raise
        except Exception as exc:
            raise AuthError(
                "NinjaOne returned a token response we could not read",
                connector=self.name.value,
                status_code=response.status_code,
            ) from exc

        self._token = token.access_token
        self.tokens.put(
            self._token_key,
            token.access_token,
            dt.datetime.now(dt.UTC)
            + dt.timedelta(seconds=max(token.expires_in - TOKEN_EXPIRY_MARGIN_SECONDS, 1)),
        )
        logger.debug(
            "ninjaone token acquired client=%s expires_in=%s",
            self.scope.client_slug,
            token.expires_in,
        )

    # --- scoping guard --------------------------------------------------

    def _allowed_by_org_id(self) -> dict[int, SiteRef]:
        return {site.as_int(self.name.value): site for site in self.scope.allowed}

    def _excluded_org_ids(self) -> set[int]:
        return {site.as_int(self.name.value) for site in self.scope.excluded}

    def _scoped(
        self,
        items: list[Any],
        organization_ids: list[int | None],
        *,
        endpoint: str,
        targets: Sequence[SiteRef] | None = None,
    ) -> list[tuple[SiteRef, Any]]:
        """Keep the records this call asked for, each tagged with its site.

        Defence in depth for hard rule 1, and the allowlist is what makes the
        failures distinguishable. A record from one of *this client's other*
        active sites means a vendor-side filter did not bite: dropped, logged as
        a warning. A record from one of its inactive sites is expected on the
        tenant-wide path: dropped quietly. A record from an organisation we do
        not know at all is either another franchisee's or a customer nobody has
        synced yet: dropped, logged as an error. A row with no organisation id
        is dropped too — we do not guess that it is ours.

        `endpoint` names the path that produced the rows, so a drop is
        diagnosable from the log line alone. `targets` overrides which sites
        count as asked-for; the tenant inventory passes the whole allowlist,
        because it is a tenant-wide read whoever is asking.
        """
        allowed = self._allowed_by_org_id()
        excluded = self._excluded_org_ids()
        wanted = self.scope.targets if targets is None else targets
        targeted = {site.external_id for site in wanted}
        kept: list[tuple[SiteRef, Any]] = []
        outside = 0
        sibling = 0
        inactive = 0
        unknown = 0
        for item, organization_id in zip(items, organization_ids, strict=True):
            if organization_id is None:
                unknown += 1
                continue
            site = allowed.get(organization_id)
            if site is None:
                if organization_id in excluded:
                    inactive += 1
                else:
                    outside += 1
                continue
            if site.external_id not in targeted:
                sibling += 1
                continue
            kept.append((site, item))

        if outside:
            # Not one of this client's sites, active or not. Either another
            # franchisee's data reached us, or a customer nobody has synced.
            # Never merged in either way, and worth shouting about.
            logger.error(
                "ninjaone GET %s returned %d record(s) from organisations outside the "
                "allowlist of client=%s; dropped. Another franchisee's organisation, or a "
                "customer not yet synced (run sync_sites)",
                endpoint,
                outside,
                self.scope.client_slug,
            )
        if sibling or unknown:
            # Inside the client but not what we asked for: a filter that did not
            # bite. Dropped, and worth a look.
            logger.warning(
                "ninjaone GET %s response was not narrowed to %s for client=%s: dropped %d "
                "from the client's other sites and %d with no organisation id",
                endpoint,
                self.scope.describe_targets(),
                self.scope.client_slug,
                sibling,
                unknown,
            )
        if inactive:
            logger.debug(
                "ninjaone GET %s: dropped %d record(s) from inactive sites of client=%s",
                endpoint,
                inactive,
                self.scope.client_slug,
            )
        return kept

    # --- fan-out --------------------------------------------------------

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Bounds how many per-organisation requests run at once."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        return self._semaphore

    # --- reads ----------------------------------------------------------

    async def list_organisations(self) -> list[VendorOrganisation]:
        """Every organisation these credentials can see, for `sync_sites`."""
        raw, _ = await self.paginate(ORGANIZATIONS_PATH, next_cursor=_last_id)
        organisations: list[VendorOrganisation] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            organisations.append(
                VendorOrganisation(
                    external_id=str(item["id"]),
                    name=str(item.get("name") or f"organisation {item['id']}"),
                )
            )
        return organisations

    async def _inventory(self, *, use_cache: bool = True) -> InventoryEntry:
        """The devices this call can place rows against: cached within the TTL, else listed.

        What is listed depends on the scope, and only an unscoped call touches
        the tenant inventory:

        - **no site named**: `GET /v2/devices`, the whole tenant (`_list_tenant`);
        - **a site named**: that site's own `GET /v2/organization/{id}/devices`
          (`_list_sites`). A one-site question has no business paging a thousand
          devices to place the forty it asked about.

        A cached entry serves the call if it covers every targeted site, so a
        whole-client entry already in the cache serves a named site for free.
        Concurrent misses are single-flighted per client: the loop runs a
        model's parallel tool calls together, each on its own connector, and
        without this both would list.
        """
        client_id = self.scope.client_id
        targets = [site.site_id for site in self.scope.targets]
        if not use_cache:
            return await self._list_inventory(targets, ttl_seconds=0)

        entry = self.inventory.get(client_id, version=self.credential_version, targets=targets)
        if entry is not None:
            logger.debug("ninjaone inventory cache hit client=%s", self.scope.client_slug)
            return entry
        async with self.inventory.flight(client_id):
            # Whoever held the lock before us has usually stored what we need.
            entry = self.inventory.get(
                client_id, version=self.credential_version, targets=targets, recheck=True
            )
            if entry is not None:
                logger.debug(
                    "ninjaone inventory served by a concurrent fetch client=%s",
                    self.scope.client_slug,
                )
                return entry
            return await self._list_inventory(targets, ttl_seconds=self.inventory_ttl_seconds)

    async def hostnames_by_organisation(self) -> dict[str, set[str]]:
        """`GET /v2/devices` tenant-wide, grouped by organisation. Sync only."""
        raw, _ = await self.paginate(
            DEVICES_PATH,
            next_cursor=_last_id,
            page_size=self.devices_page_size,
            stop_on_short_page=False,
        )
        found: dict[str, set[str]] = {}
        for item in raw:
            device = NinjaDevice.model_validate(item)
            if device.organization_id is not None and device.name:
                found.setdefault(str(device.organization_id), set()).add(
                    normalise_hostname(device.name)
                )
        return found

    async def _list_inventory(
        self, targets: list[uuid.UUID], *, ttl_seconds: float
    ) -> InventoryEntry:
        if self.scope.covers_every_site:
            rows, device_orgs, truncated = await self._list_tenant()
            source: Literal["tenant", "sites"] = "tenant"
        else:
            rows, device_orgs, truncated = await self._list_sites()
            source = "sites"
        return self.inventory.put(
            self.scope.client_id,
            version=self.credential_version,
            targets=targets,
            rows=rows,
            device_orgs=device_orgs,
            truncated=truncated,
            ttl_seconds=ttl_seconds,
            source=source,
        )

    async def _list_tenant(
        self,
    ) -> tuple[list[tuple[SiteRef, NinjaDevice]], dict[int, int], bool]:
        """`GET /v2/devices`: every device the tenant has, classified row by row.

        Paged with the configured page size until an empty page (the spec
        declares no maximum page size, so a silent vendor cap must not be
        mistaken for the end of the list). Every device is checked against the
        allowlist by `organizationId`: a device from an inactive site is
        dropped quietly, one from an unknown organisation is dropped with an
        error. The map returned covers *every* device, which is what lets an
        alert on an inactive site's device be told from one nobody knows.
        """
        raw, truncated = await self.paginate(
            DEVICES_PATH,
            next_cursor=_last_id,
            page_size=self.devices_page_size,
            stop_on_short_page=False,
        )
        devices = [NinjaDevice.model_validate(item) for item in raw]

        # A device with no organisation at all is not something the tenant
        # listing has been seen to return (api-notes); if it ever is, it is a
        # fact about the tenant rather than a scoping failure, so it is noted
        # at INFO and left out of the guard, which would otherwise warn.
        assigned = [device for device in devices if device.organization_id is not None]
        unassigned = len(devices) - len(assigned)
        if unassigned:
            logger.info(
                "ninjaone GET %s returned %d device(s) with no organizationId for client=%s; "
                "not attributable to any site, left out",
                DEVICES_PATH,
                unassigned,
                self.scope.client_slug,
            )

        # The whole tenant, so classified against the full allowlist: a sibling
        # site's device here is expected, not a filter failure.
        rows = self._scoped(
            assigned,
            [device.organization_id for device in assigned],
            endpoint=DEVICES_PATH,
            targets=self.scope.allowed,
        )
        device_orgs = {
            device.id: device.organization_id
            for device in assigned
            if device.organization_id is not None
        }
        logger.debug(
            "ninjaone tenant inventory listed client=%s devices=%d kept=%d truncated=%s",
            self.scope.client_slug,
            len(devices),
            len(rows),
            truncated,
        )
        return rows, device_orgs, truncated

    async def _list_sites(
        self,
    ) -> tuple[list[tuple[SiteRef, NinjaDevice]], dict[int, int], bool]:
        """The targeted sites' own listings: `GET /v2/organization/{id}/devices` each.

        The vendor does the scoping, and the guard still checks every row. The
        map returned knows these sites' devices and nothing else, so a
        `deviceId` missing from it is out of scope for this call by
        definition.
        """

        async def one_site(site: SiteRef) -> tuple[list[tuple[SiteRef, NinjaDevice]], bool]:
            path = ORGANIZATION_DEVICES_PATH.format(organization_id=site.as_int(self.name.value))
            raw, page_truncated = await self.paginate(path, next_cursor=_last_id)
            devices = [NinjaDevice.model_validate(item) for item in raw]
            scoped = self._scoped(
                devices, [device.organization_id for device in devices], endpoint=path
            )
            return scoped, page_truncated

        rows: list[tuple[SiteRef, NinjaDevice]] = []
        truncated = False
        answered, _ = await self._fan_out_refs(self.scope.targets, one_site)
        for _site, (site_rows, page_truncated) in answered:
            rows.extend(site_rows)
            truncated = truncated or page_truncated
        device_orgs = {device.id: site.as_int(self.name.value) for site, device in rows}
        logger.debug(
            "ninjaone site inventory listed client=%s sites=%s devices=%d truncated=%s",
            self.scope.client_slug,
            self.scope.describe_targets(),
            len(rows),
            truncated,
        )
        return rows, device_orgs, truncated

    async def list_all_devices(
        self, *, use_cache: bool = True
    ) -> tuple[list[tuple[SiteRef, NinjaDevice]], bool]:
        """Every device this call targets, from the inventory. See `_inventory`."""
        entry = await self._inventory(use_cache=use_cache)
        # An entry may cover more than this call targets; the call gets its own sites.
        targeted = {site.site_id for site in self.scope.targets}
        return [row for row in entry.rows if row[0].site_id in targeted], entry.truncated

    async def device_organizations(self) -> dict[int, int]:
        """`deviceId -> organizationId` for the devices this call can place."""
        return dict((await self._inventory()).device_orgs)

    async def list_devices(self) -> tuple[list[tuple[SiteRef, NinjaDevice]], bool]:
        """Devices across every site this call targets, tagged with their site.

        Unscoped calls take the tenant-wide listing (one request per page, not
        per organisation). A named site goes through its own path-scoped
        endpoint, which is what makes the vendor do the scoping for us. Both
        come through the inventory, so a device list and a report about the
        same site within the TTL cost one listing between them.
        """
        return await self.list_all_devices()

    async def list_locations(self) -> list[tuple[SiteRef, NinjaLocation]]:
        """Each targeted organisation's locations, tagged with its site.

        The path names the organisation, so the vendor does the scoping; a
        location row carries no organisation id of its own to check again.
        """

        async def one_site(site: SiteRef) -> list[NinjaLocation]:
            path = ORGANIZATION_LOCATIONS_PATH.format(organization_id=site.as_int(self.name.value))
            raw = await self.get_json(path)
            if not isinstance(raw, list):
                raise UpstreamError(
                    f"NinjaOne returned {type(raw).__name__} where a list of locations "
                    "was expected",
                    connector=self.name.value,
                    details={"path": path, "site": site.slug},
                )
            return [NinjaLocation.model_validate(item) for item in raw]

        answered, _ = await self._fan_out_refs(self.scope.targets, one_site)
        return [(site, location) for site, locations in answered for location in locations]

    async def list_alerts(self) -> list[tuple[SiteRef, NinjaAlert]]:
        """Active alerts across every site this call targets, tagged with theirs.

        `/v2/alerts` has no organisation-scoped path and takes no pagination
        parameters, so this is one call per organisation narrowed by `df`, run
        concurrently under the semaphore. The `df` syntax is undocumented, so
        `_scoped` is what actually guarantees the result (see api-notes).

        A live alert names its device by `deviceId` only; the embedded `device`
        object the spec allows is not sent. So any alert without an organisation
        is resolved through the tenant inventory's device map before the guard.
        An alert whose device the inventory does not know cannot be placed and
        is dropped with an error: it is not ours to show, and something is off.
        """

        async def one_site(site: SiteRef) -> list[NinjaAlert]:
            raw = await self.get_json(
                ALERTS_PATH, params={"df": f"org={site.as_int(self.name.value)}"}
            )
            if not isinstance(raw, list):
                raise UpstreamError(
                    f"NinjaOne returned {type(raw).__name__} where a list of alerts was expected",
                    connector=self.name.value,
                    details={"path": ALERTS_PATH, "site": site.slug},
                )
            return [NinjaAlert.model_validate(item) for item in raw]

        answered, _ = await self._fan_out_refs(self.scope.targets, one_site)

        # The inventory is only fetched when an alert actually needs it, so a
        # tenant that does embed the device costs no extra request.
        needs_lookup = any(
            alert.organization_id is None and alert.device_id is not None
            for _site, alerts in answered
            for alert in alerts
        )
        lookup = await self._inventory() if needs_lookup else None
        known_devices = (
            {device.id: (site, device) for site, device in lookup.rows} if lookup else {}
        )

        collected: list[tuple[SiteRef, NinjaAlert]] = []
        for site, alerts in answered:
            resolved: list[NinjaAlert] = []
            organization_ids: list[int | None] = []
            unresolved = 0
            for alert in alerts:
                organization_id = alert.organization_id
                if organization_id is None and alert.device_id is not None and lookup:
                    organization_id = lookup.device_orgs.get(alert.device_id)
                    if organization_id is None:
                        unresolved += 1
                        continue
                    placed = known_devices.get(alert.device_id)
                    if placed is not None:
                        # Give the alert the device summary the vendor left out,
                        # so the tool can name the machine.
                        _, device = placed
                        alert = alert.model_copy(
                            update={
                                "device": NinjaAlertDevice(
                                    id=device.id,
                                    organizationId=organization_id,
                                    systemName=device.system_name,
                                    displayName=device.display_name,
                                )
                            }
                        )
                resolved.append(alert)
                organization_ids.append(organization_id)

            if unresolved and lookup is not None:
                self._log_unplaced(lookup, ALERTS_PATH, unresolved, what="alert(s)", site=site)
            collected.extend(self._scoped(resolved, organization_ids, endpoint=ALERTS_PATH))
        return collected

    # --- device-keyed reports (/v2/queries/*) ------------------------------

    async def query(
        self,
        report: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool, int | None]:
        """Read one `/v2/queries/{report}` report, following its cursor.

        The envelope is `{cursor: {name, offset, count, expires}, results}`;
        the next page is asked for with `cursor=<name>`. Returns
        `(rows, truncated, count)` where `count` is the vendor's total for the
        query (None when a `df` filter is present — the vendor then reports
        no total, api-notes) and `truncated` means the page cap stopped us.
        """
        path = QUERY_PATH.format(report=report)
        size = page_size or self.query_page_size
        cap = max_pages or self.limits.max_pages
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        total: int | None = None
        expected_offset = 0

        for page_number in range(1, cap + 1):
            page_params = dict(params or {})
            page_params["pageSize"] = size
            if cursor is not None:
                page_params["cursor"] = cursor
            raw = await self.get_json(path, params=page_params)
            if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
                raise UpstreamError(
                    f"NinjaOne returned {type(raw).__name__} where a query envelope was expected",
                    connector=self.name.value,
                    details={"path": path},
                )
            batch = raw["results"]
            raw_cursor = raw.get("cursor")
            envelope: dict[str, Any] = raw_cursor if isinstance(raw_cursor, dict) else {}
            count = envelope.get("count")
            if isinstance(count, int):
                total = count
            raw_offset = envelope.get("offset")
            offset: int = raw_offset if isinstance(raw_offset, int) else expected_offset
            name = envelope.get("name")
            # The cursor is a name the server keeps the position of, and every page
            # after the first re-sends the same name: that is the protocol, not a
            # repeat. What must not happen is the server's position disagreeing
            # with ours — behind it (the cursor restarted or expired: the rows
            # would repeat), ahead of it (a retry after a failed response advanced
            # it: rows would be skipped). Either is an incomplete read, never a
            # quiet one.
            logger.debug(
                "ninjaone query %s page %d: cursor=%s offset=%s count=%s rows=%d expires=%s",
                report,
                page_number,
                name,
                raw_offset,
                count,
                len(batch),
                envelope.get("expires"),
            )
            if page_number > 1 and isinstance(raw_offset, int) and raw_offset != expected_offset:
                what = "restarted" if raw_offset < expected_offset else "skipped rows"
                raise UpstreamError(
                    f"NinjaOne's {report} cursor {what}: page {page_number} came back at "
                    f"offset {raw_offset}, expected {expected_offset}",
                    connector=self.name.value,
                    details={"path": path, "offset": raw_offset, "expected": expected_offset},
                )
            rows.extend(item for item in batch if isinstance(item, dict))
            expected_offset = offset + len(batch)

            if not batch or not isinstance(name, str) or len(batch) < size:
                return rows, False, total
            if total is not None and offset + len(batch) >= total:
                return rows, False, total
            cursor = name

        return rows, True, total

    async def query_scoped(
        self, report: str, *, params: dict[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], bool, int | None]:
        """A report across this call's targets: tenant-wide, or `df=org=N` per site.

        With no site named the whole tenant is read in one cursor walk and
        rows are classified afterwards. With a site named the vendor narrows
        each call with `df=org={id}` (verified to bite, api-notes), and the
        calls run under the semaphore. Rows are not yet classified: see
        `place_by_device`.
        """
        if self.scope.covers_every_site:
            return await self.query(report, params=params)

        async def one_site(site: SiteRef) -> tuple[list[dict[str, Any]], bool, int | None]:
            narrowed = {**(params or {}), "df": f"org={site.as_int(self.name.value)}"}
            return await self.query(report, params=narrowed)

        rows: list[dict[str, Any]] = []
        truncated = False
        answered, _ = await self._fan_out_refs(self.scope.targets, one_site)
        for _site, (site_rows, site_truncated, _) in answered:
            rows.extend(site_rows)
            truncated = truncated or site_truncated
        return rows, truncated, None

    def _log_unplaced(
        self,
        entry: InventoryEntry,
        endpoint: str,
        count: int,
        *,
        what: str,
        site: SiteRef | None = None,
    ) -> None:
        """Say that rows were dropped because their device could not be placed.

        Against the tenant inventory an unknown `deviceId` is an **error**: the
        vendor returned a row for a device it does not list. Against a site's
        own listing it only means the device is not that site's, so the row is
        out of scope for the call — dropped, and a **warning**, because the
        report was narrowed with `df=org=N` and should not have sent it.
        """
        where = f" site={site.slug}" if site is not None else ""
        if entry.source == "tenant":
            logger.error(
                "ninjaone GET %s for client=%s%s: %d %s reference devices that are not in "
                "the tenant inventory; dropped",
                endpoint,
                self.scope.client_slug,
                where,
                count,
                what,
            )
        else:
            logger.warning(
                "ninjaone GET %s for client=%s%s: %d %s reference devices that are not in "
                "the device listing of %s; out of scope for this call, dropped",
                endpoint,
                self.scope.client_slug,
                where,
                count,
                what,
                self.scope.describe_targets(),
            )

    async def place_by_device[T](
        self, rows: list[T], device_ids: list[int | None], *, endpoint: str
    ) -> list[tuple[SiteRef, NinjaDevice, T]]:
        """Classify device-keyed rows through the inventory, then the allowlist guard.

        Every `/v2/queries/*` row names a device and nothing else. The
        inventory maps the device to its organisation; the guard then applies
        exactly as it does to a device listing. A device id the inventory does
        not know is dropped (see `_log_unplaced` for what that means under each
        scope). Each kept row comes back with the device it belongs to, for
        naming.
        """
        entry = await self._inventory()
        known = {device.id: (site, device) for site, device in entry.rows}
        resolved: list[T] = []
        organization_ids: list[int | None] = []
        unresolved = 0
        for row, device_id in zip(rows, device_ids, strict=True):
            if device_id is None:
                resolved.append(row)
                organization_ids.append(None)
                continue
            organization_id = entry.device_orgs.get(device_id)
            if organization_id is None:
                unresolved += 1
                continue
            resolved.append(row)
            organization_ids.append(organization_id)
        if unresolved:
            self._log_unplaced(entry, endpoint, unresolved, what="row(s)")
        placed: list[tuple[SiteRef, NinjaDevice, T]] = []
        for site, row in self._scoped(resolved, organization_ids, endpoint=endpoint):
            device_id = self._device_id_of(row)
            pair = known.get(device_id) if device_id is not None else None
            if pair is None:  # pragma: no cover - a kept row's device is in scope by construction
                continue
            placed.append((site, pair[1], row))
        return placed

    @staticmethod
    def _device_id_of(row: Any) -> int | None:
        value = row.get("deviceId") if isinstance(row, dict) else getattr(row, "device_id", None)
        return value if isinstance(value, int) else None

    async def custom_field_definitions(self) -> list[NinjaCustomFieldDefinition]:
        """The device custom fields defined on this tenant.

        Tenant configuration, not client data: there is nothing to scope. The
        values are another matter — see `list_custom_fields`.
        """
        raw = await self.get_json(CUSTOM_FIELD_DEFINITIONS_PATH)
        if not isinstance(raw, list):
            raise UpstreamError(
                "NinjaOne returned no list of custom field definitions",
                connector=self.name.value,
                details={"path": CUSTOM_FIELD_DEFINITIONS_PATH},
            )
        return [
            NinjaCustomFieldDefinition.model_validate(item)
            for item in raw
            if isinstance(item, dict) and item.get("name")
        ]

    # --- one device -----------------------------------------------------

    async def find_devices(self, name: str) -> list[tuple[SiteRef, NinjaDevice]]:
        """Devices in scope whose name matches, exact (casefold) first, else substring."""
        rows, _ = await self.list_devices()
        wanted = name.strip().casefold()
        exact = [(site, device) for site, device in rows if device.name.casefold() == wanted]
        if exact:
            return exact
        return [(site, device) for site, device in rows if wanted in device.name.casefold()]

    async def get_device(self, site: SiteRef, device_id: int) -> NinjaDeviceDetail:
        """`GET /v2/device/{id}`, checked against the allowlist before it is returned."""
        raw = await self.get_json(DEVICE_PATH.format(device_id=device_id))
        if not isinstance(raw, dict):
            raise UpstreamError(
                f"NinjaOne returned {type(raw).__name__} where a device was expected",
                connector=self.name.value,
                details={"path": DEVICE_PATH.format(device_id=device_id)},
            )
        detail = NinjaDeviceDetail.model_validate(raw)
        kept = self._scoped(
            [detail], [detail.organization_id], endpoint=DEVICE_PATH.format(device_id=device_id)
        )
        if not kept or kept[0][0].site_id != site.site_id:
            raise UpstreamError(
                f"NinjaOne device {device_id} did not resolve to site {site.slug!r}; refused",
                connector=self.name.value,
                details={"device_id": device_id, "site": site.slug},
            )
        return detail

    async def device_activities(
        self, device_id: int, *, since: dt.datetime, page_size: int
    ) -> tuple[list[NinjaActivity], bool]:
        """One page of a device's activity log, newest first, from `since` onwards.

        The endpoint pages by activity id (`olderThan`); one page of
        `page_size` is what a tool shows, and `truncated` says whether the
        page was full — there may be more.
        """
        raw = await self.get_json(
            DEVICE_ACTIVITIES_PATH.format(device_id=device_id),
            params={"pageSize": page_size, "startTime": since.isoformat()},
        )
        activities = raw.get("activities") if isinstance(raw, dict) else None
        if not isinstance(activities, list):
            raise UpstreamError(
                "NinjaOne returned no activities list for the device",
                connector=self.name.value,
                details={"path": DEVICE_ACTIVITIES_PATH.format(device_id=device_id)},
            )
        parsed = [
            NinjaActivity.model_validate(item) for item in activities if isinstance(item, dict)
        ]
        return parsed, len(parsed) >= page_size

    def _activity_params(self, site: SiteRef | None, params: dict[str, Any]) -> dict[str, Any]:
        if site is None:
            return params
        return {**params, "df": f"org={site.as_int(self.name.value)}"}

    async def _activities_page(self, params: dict[str, Any]) -> list[NinjaActivity]:
        raw = await self.get_json(ACTIVITIES_PATH, params=params)
        activities = raw.get("activities") if isinstance(raw, dict) else None
        if not isinstance(activities, list):
            raise UpstreamError(
                "NinjaOne returned no activities list",
                connector=self.name.value,
                details={"path": ACTIVITIES_PATH},
            )
        return [NinjaActivity.model_validate(item) for item in activities if isinstance(item, dict)]

    async def activity_log_reaches(self, moment: dt.datetime) -> bool:
        """Does the activity log hold anything from before `moment`, for every target?

        NinjaOne keeps the log for a limited time (on the pilot tenant, about
        seventeen days of it on 2026-09-21), and says nothing when a window
        falls off the end: it returns fewer rows, or none. A count over a period
        is only a count if the log still reaches back past the period's start,
        so a caller asks this first and reports a gap when the answer is no.
        """
        window = {"pageSize": ACTIVITY_MIN_PAGE, "before": f"{moment:%Y-%m-%d}"}
        if self.scope.covers_every_site:
            return bool(await self._activities_page(self._activity_params(None, window)))

        async def reaches(site: SiteRef) -> bool:
            return bool(await self._activities_page(self._activity_params(site, window)))

        # Every organisation the vendor still answers for must reach back; one
        # it no longer knows (a site's other organisation answered) is spared.
        answered, _ = await self._fan_out_refs(self.scope.targets, reaches)
        return all(result for _site, result in answered)

    async def list_activities(
        self,
        *,
        activity_type: str,
        status: str,
        after: dt.datetime,
        before: dt.datetime,
        page_size: int = 1000,
    ) -> tuple[list[tuple[SiteRef, NinjaDevice, NinjaActivity]], bool]:
        """The tenant activity log (`GET /v2/activities`) for one type and status in a window.

        `after` and `before` are day-granular on the vendor's side; the caller
        bounds the rows again. Pages run newest first by `olderThan=<lowest id>`
        until a short page. Rows name a device and nothing else, so they are
        placed through the inventory and the allowlist guard like any query
        row. Returns `(rows, truncated)`; truncated means the page cap stopped
        the walk and the rows are not the whole window.
        """
        sent_type = ACTIVITY_TYPES.get(activity_type.strip().lower())
        sent_status = ACTIVITY_STATUSES.get(status.strip().lower())
        if sent_type is None or sent_status is None:
            raise ValueError(
                f"activity_type must be one of {', '.join(ACTIVITY_TYPES)} and status one of "
                f"{', '.join(ACTIVITY_STATUSES)}; got {activity_type!r}, {status!r}. "
                "Nothing was sent to NinjaOne."
            )
        size = max(page_size, ACTIVITY_MIN_PAGE)
        base: dict[str, Any] = {
            "type": sent_type,
            "status": sent_status,
            "after": f"{after:%Y-%m-%d}",
            "before": f"{before + dt.timedelta(days=1):%Y-%m-%d}",
            "pageSize": size,
        }

        async def walk(site: SiteRef | None) -> tuple[list[NinjaActivity], bool]:
            rows: list[NinjaActivity] = []
            older_than: int | None = None
            for _ in range(self.limits.max_pages):
                params = self._activity_params(site, dict(base))
                if older_than is not None:
                    params["olderThan"] = older_than
                batch = await self._activities_page(params)
                rows.extend(batch)
                ids = [row.id for row in batch if row.id is not None]
                if len(batch) < size or not ids:
                    return rows, False
                older_than = min(ids)
            return rows, True

        if self.scope.covers_every_site:
            walked = [await walk(None)]
        else:
            answered, _ = await self._fan_out_refs(self.scope.targets, walk)
            walked = [result for _site, result in answered]
        found = [row for rows, _ in walked for row in rows]
        truncated = any(cut for _, cut in walked)
        # The vendor filters were asked for; they are checked, not trusted.
        kept = [
            row
            for row in found
            if (row.activity_type or "").upper() == sent_type
            and (row.status_code or "").upper() == sent_status
        ]
        placed = await self.place_by_device(
            kept, [row.device_id for row in kept], endpoint=ACTIVITIES_PATH
        )
        return placed, truncated

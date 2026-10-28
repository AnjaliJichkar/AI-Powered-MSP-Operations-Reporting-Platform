"""Bitdefender GravityZone connector (Control Center public API, JSON-RPC 2.0).

**Read-only is enforced here, not by the credential.** A GravityZone API key
cannot be scoped to reads: any key that can list endpoints can also start a
scan, move a machine or change a policy. So this client holds an explicit
allowlist of read-only method names per service (`READ_ONLY_METHODS`), checks
every call against it before a request is built, and raises
`ReadOnlyViolation` for anything else. Nothing outside the list can reach the
vendor through this class, whatever a caller asks for (hard rule 2).

Auth is HTTP Basic with the API key as the user name and an empty password
(docs/api-notes/bitdefender.md). Every call is `POST {base}/v1.x/jsonrpc/{service}`
with a JSON-RPC envelope; errors come back as HTTP 200 with an `error` member,
which `call` maps onto the normal hierarchy.

Scoping is the same two levels as NinjaOne. The API key belongs to the
franchisee's *partner* company; each end customer is a GravityZone company
under it, held as a `Site` whose mapping carries the company id. Endpoint reads
take one of two paths:

- **no site named**: one recursive `getNetworkInventoryItems` walk from the
  partner root, every row classified by its `companyId` against the allowlist
  and cached per client (`app/connectors/inventory.py`);
- **a site named**: the same method with `parentId` = that company, which the
  vendor scopes; several sites fan out under a semaphore.

A record that names an endpoint but no company (an incident whose company is
missing) is placed through the inventory's `endpointId -> companyId` map
before the guard, exactly as NinjaOne's query rows are. `_scoped` checks every
row against the client's whole allowlist and drops the franchisee's own
endpoints, an inactive site's, and anything unknown, each at its own log level.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Literal

from app.connectors.base import BaseConnector
from app.connectors.bitdefender.models import (
    ENDPOINT_ITEM_TYPES,
    ITEM_COMPANY,
    BdCompany,
    BdEndpoint,
    BdEndpointDetail,
    BdIncident,
    BdLicense,
)
from app.connectors.errors import (
    ArgumentRejected,
    AuthError,
    ConnectorError,
    NotFound,
    UpstreamError,
)
from app.connectors.inventory import InventoryCache, InventoryEntry
from app.connectors.matching import normalise_hostname
from app.connectors.scope import SiteRef, VendorOrganisation
from app.models.enums import ConnectorName
from app.settings import get_settings

logger = logging.getLogger(__name__)

#: GravityZone runs one console per cloud instance; a client's row says which.
REGION_BASE_URLS = {
    "us": "https://cloud.gravityzone.bitdefender.com/api",
    "eu": "https://cloudgz.gravityzone.bitdefender.com/api",
    "ap": "https://cloudap.gravityzone.bitdefender.com/api",
}

#: Every method this connector may call, by service. Anything not here is
#: refused before a request exists. All are reads: they list, get or find.
READ_ONLY_METHODS: Mapping[str, frozenset[str]] = {
    "general": frozenset({"getApiKeyDetails"}),
    "companies": frozenset(
        {"getCompanyDetails", "findCompaniesByName", "getCustomFieldsDefinitions"}
    ),
    "network": frozenset(
        {
            "getCompaniesList",
            "getCompanyFoldersList",
            "getRootContainers",
            "getEndpointsList",
            "getManagedEndpointDetails",
            "getNetworkInventoryItems",
            "getCustomGroupsList",
            "getTaskStatus",
        }
    ),
    "incidents": frozenset({"getIncidentsList", "getBlocklistItems"}),
    "licensing": frozenset({"getLicenseInfo", "getMonthlyUsage"}),
    "reports": frozenset({"getReportsList"}),
    "policies": frozenset({"getPoliciesList", "getPolicyDetails"}),
}

#: Methods that live on a later API version than 1.0 (docs: Network 1.1, Incidents 1.2).
METHOD_VERSIONS: Mapping[tuple[str, str], str] = {
    ("network", "getEndpointsList"): "1.1",
    ("network", "getNetworkInventoryItems"): "1.1",
    ("incidents", "getIncidentsList"): "1.2",
    ("incidents", "getBlocklistItems"): "1.2",
}

#: JSON-RPC error codes the vendor documents.
RPC_AUTHORIZATION = -32001
RPC_NOT_FOUND = -32002
RPC_TOO_MANY = -32003
RPC_INVALID_PARAMS = -32602

#: Live, an unknown object id is reported as invalid params, not as -32002, with
#: one of these phrases in the detail (api-notes, 2026-09-22).
NOT_FOUND_PHRASES = ("does not exist", "not found", "do not have full access to")


CUSTOMER_COMPANY = 1


class ReadOnlyViolation(PermissionError):
    """A caller asked this connector for a method that is not a read. No request was made."""


class BitdefenderClient(BaseConnector):
    """Read-only access to one franchisee's GravityZone companies."""

    name: ClassVar[ConnectorName] = ConnectorName.BITDEFENDER
    default_base_url: ClassVar[str] = REGION_BASE_URLS["us"]

    def __init__(
        self,
        *,
        concurrency: int | None = None,
        inventory_ttl_seconds: float | None = None,
        page_size: int | None = None,
        inventory: InventoryCache[BdEndpoint, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        settings = get_settings()
        # Hard rule 6: fan-out width, cache TTL and page size are settings.
        self.concurrency = (
            concurrency if concurrency is not None else settings.bitdefender_concurrency
        )
        self.inventory_ttl_seconds = (
            inventory_ttl_seconds
            if inventory_ttl_seconds is not None
            else settings.bitdefender_inventory_ttl_seconds
        )
        self.page_size = page_size if page_size is not None else settings.bitdefender_page_size
        self.inventory: InventoryCache[BdEndpoint, str] = (
            inventory if inventory is not None else INVENTORY
        )
        self._semaphore: asyncio.Semaphore | None = None
        self._own_company_id: str | None = None
        self._licences: dict[str, BdLicense] = {}
        """Per company id, for this instance: a licence changes monthly, not per call."""
        self.calls_made: list[tuple[str, str]] = []
        """`(service, method)` of every request sent, for tests and validation runs."""

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
                    f"unknown GravityZone region {region!r}; expected one of "
                    f"{sorted(REGION_BASE_URLS)}",
                    connector=self.name.value,
                )
            return REGION_BASE_URLS[region]
        return self.default_base_url

    # --- auth: static Basic header, nothing to refresh --------------------

    def _auth_headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.secret('api_key')}:".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    # --- the one way out ------------------------------------------------

    @staticmethod
    def is_read_only(service: str, method: str) -> bool:
        return method in READ_ONLY_METHODS.get(service, frozenset())

    async def call(self, service: str, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """One JSON-RPC call. Refuses anything outside `READ_ONLY_METHODS` before any HTTP."""
        if not self.is_read_only(service, method):
            raise ReadOnlyViolation(
                f"bitdefender {service}.{method} is not a read-only method; this connector "
                "makes no request for it (hard rule 2)"
            )
        version = METHOD_VERSIONS.get((service, method), "1.0")
        path = f"/v{version}/jsonrpc/{service}"
        envelope = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": dict(params or {}),
        }
        self.calls_made.append((service, method))
        response = await self.request(
            "POST", path, json_body=envelope, headers={"Content-Type": "application/json"}
        )
        body = self._decode(response, path)
        if not isinstance(body, dict):
            raise UpstreamError(
                f"GravityZone returned {type(body).__name__} where a JSON-RPC envelope was "
                "expected",
                connector=self.name.value,
                details={"path": path, "method": method},
            )
        error = body.get("error")
        if error:
            raise self._rpc_error(error, method=method, path=path)
        return body.get("result")

    def _rpc_error(self, error: Any, *, method: str, path: str) -> ConnectorError:
        code = error.get("code") if isinstance(error, dict) else None
        message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
        data = error.get("data") if isinstance(error, dict) else None
        detail = str(data.get("details", ""))[:200] if isinstance(data, dict) else ""
        details = {"path": path, "method": method, "code": code, "detail": detail or message}
        if code == RPC_AUTHORIZATION:
            return AuthError(
                f"GravityZone refused {method}: {detail or message}",
                connector=self.name.value,
                details=details,
            )
        lowered = detail.casefold()
        if code == RPC_NOT_FOUND or (
            code == RPC_INVALID_PARAMS and any(p in lowered for p in NOT_FOUND_PHRASES)
        ):
            return NotFound(
                f"GravityZone has no such object for {method}: {detail or message}",
                connector=self.name.value,
                details=details,
            )
        if code == RPC_INVALID_PARAMS:
            # The vendor names what it accepts ("should be between 10 and 10000",
            # "a combination of these strings: open, closed, ..."): hand that to
            # the model as something it can correct.
            return ArgumentRejected(
                f"GravityZone rejected an argument to {method}: {detail or message}. "
                "Nothing was changed; correct the value and call again.",
                connector=self.name.value,
                details=details,
            )
        return UpstreamError(
            f"GravityZone {method} failed: {message} ({detail})"
            if detail
            else f"GravityZone {method} failed: {message}",
            connector=self.name.value,
            details=details,
        )

    # --- scoping guard --------------------------------------------------

    def _allowed_by_company(self) -> dict[str, SiteRef]:
        return {site.external_id: site for site in self.scope.allowed}

    def _excluded_companies(self) -> set[str]:
        return {site.external_id for site in self.scope.excluded}

    def _scoped(
        self,
        items: list[Any],
        company_ids: list[str | None],
        *,
        method: str,
        targets: Sequence[SiteRef] | None = None,
    ) -> list[tuple[SiteRef, Any]]:
        """Keep the records this call asked for, each tagged with its site.

        The same defence in depth as NinjaOne's guard: a sibling site's row
        means a vendor filter did not bite (warning); an inactive site's row is
        expected on the partner-root path (quiet); the franchisee's own
        endpoints, under the partner company itself, are not a customer's
        (info); a company we do not know is another franchisee's or a customer
        nobody has synced (error, never merged in). A row with no company id is
        dropped too.
        """
        allowed = self._allowed_by_company()
        excluded = self._excluded_companies()
        wanted = self.scope.targets if targets is None else targets
        targeted = {site.external_id for site in wanted}
        kept: list[tuple[SiteRef, Any]] = []
        outside = sibling = inactive = unknown = own = 0
        for item, company_id in zip(items, company_ids, strict=True):
            if not company_id:
                unknown += 1
                continue
            site = allowed.get(company_id)
            if site is None:
                if company_id in excluded:
                    inactive += 1
                elif company_id == self._own_company_id:
                    own += 1
                else:
                    outside += 1
                continue
            if site.external_id not in targeted:
                sibling += 1
                continue
            kept.append((site, item))
        if outside:
            logger.error(
                "bitdefender %s returned %d record(s) from companies outside the allowlist of "
                "client=%s; dropped. Another franchisee's company, or a customer not yet "
                "synced (run sync_sites)",
                method,
                outside,
                self.scope.client_slug,
            )
        if sibling or unknown:
            logger.warning(
                "bitdefender %s response was not narrowed to %s for client=%s: dropped %d from "
                "the client's other sites and %d with no company id",
                method,
                self.scope.describe_targets(),
                self.scope.client_slug,
                sibling,
                unknown,
            )
        if inactive or own:
            logger.debug(
                "bitdefender %s: dropped %d record(s) from inactive sites and %d of the "
                "franchisee's own endpoints for client=%s",
                method,
                inactive,
                own,
                self.scope.client_slug,
            )
        return kept

    # --- fan-out --------------------------------------------------------

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        return self._semaphore

    # --- paging ---------------------------------------------------------

    async def _pages(
        self, service: str, method: str, params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Walk a `{page, perPage, hasMoreRecords, items}` result until it ends.

        `pagesCount` and `total` appear on page 1 only (docs), and a silently
        capped page would look like a short one, so the walk stops on
        `hasMoreRecords` being false, on an empty page, or at the page cap,
        which is reported as `truncated`.
        """
        items: list[dict[str, Any]] = []
        for page in range(1, self.limits.max_pages + 1):
            result = await self.call(
                service, method, {**params, "page": page, "perPage": self.page_size}
            )
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise UpstreamError(
                    f"GravityZone {method} returned no items list",
                    connector=self.name.value,
                    details={"method": method, "page": page},
                )
            batch = [item for item in result["items"] if isinstance(item, dict)]
            items.extend(batch)
            more = result.get("hasMoreRecords")
            if more is None:  # older shape: pagesCount on page 1
                pages = result.get("pagesCount")
                more = isinstance(pages, int) and page < pages
            if not batch or not more:
                return items, False
        return items, True

    # --- reads ----------------------------------------------------------

    async def own_company_id(self) -> str | None:
        """The partner company the API key belongs to, so its own endpoints are recognised."""
        if self._own_company_id is None:
            result = await self.call("companies", "getCompanyDetails", {})
            if isinstance(result, dict) and result.get("id"):
                self._own_company_id = str(result["id"])
        return self._own_company_id

    async def list_organisations(self) -> list[VendorOrganisation]:
        """Every *customer* company under the partner, for `sync_sites`.

        `getCompaniesList` with `companyType: 1` (customers). Partner
        companies nested under this one are not customers and are left out;
        their own customers are not reached (api-notes, unverified item 2).
        """
        result = await self.call("network", "getCompaniesList", {"filters": {"companyType": 1}})
        if not isinstance(result, list):
            raise UpstreamError(
                "GravityZone getCompaniesList returned no list",
                connector=self.name.value,
                details={"method": "getCompaniesList"},
            )
        organisations: list[VendorOrganisation] = []
        for item in result:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            organisations.append(
                VendorOrganisation(
                    external_id=str(item["id"]),
                    name=str(item.get("name") or f"company {item['id']}"),
                )
            )
        return organisations

    async def licence(self, site: SiteRef) -> BdLicense:
        """`getLicenseInfo` for one company: which modules it may have at all."""
        cached = self._licences.get(site.external_id)
        if cached is not None:
            return cached
        result = await self.call("licensing", "getLicenseInfo", {"companyId": site.external_id})
        licence = BdLicense.model_validate(result if isinstance(result, dict) else {})
        self._licences[site.external_id] = licence
        return licence

    async def licences(self) -> dict[str, BdLicense]:
        """Licences for every targeted company, by site slug, under the semaphore.

        A site with two companies gets the union of their licences
        (`BdLicense.union`): a module either company is licensed for is one
        the customer has bought.
        """
        answers, _ = await self._fan_out_refs(self.scope.targets, self.licence)
        merged: dict[str, BdLicense] = {}
        for site, licence in answers:
            merged[site.slug] = merged[site.slug].union(licence) if site.slug in merged else licence
        return merged

    async def companies_with_edr(self) -> list[SiteRef]:
        """The targeted companies whose own licence does not rule EDR out.

        A site's union licence (`licences`) says whether detections can exist
        for the customer at all; this says which of its companies to ask, since
        an unlicensed company has nothing to list and a company the vendor no
        longer knows answers nothing. Reads the licences if they are not held.
        """
        await self.licences()
        return [
            site
            for site in self.scope.targets
            if site.external_id in self._licences
            and self._licences[site.external_id].edr_licensed is not False
        ]

    async def list_companies(self, *, include_risk: bool = True) -> list[tuple[SiteRef, BdCompany]]:
        """The targeted companies as inventory items, with their risk scores."""
        params: dict[str, Any] = {
            "filters": {"type": {"companies": True}, "depth": {"allItemsRecursively": True}},
            "options": {"companies": {"includeRiskScore": include_risk}},
        }
        items, _ = await self._pages("network", "getNetworkInventoryItems", params)
        companies: list[BdCompany] = []
        for item in items:
            if item.get("type") != ITEM_COMPANY or not item.get("id"):
                continue
            raw_details = item.get("details")
            details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
            companies.append(BdCompany.model_validate({**details, **item}))
        # The company listing is tenant-wide by construction: `getNetworkInventoryItems`
        # takes no parentId here, so every one of the client's companies comes back and
        # the rows for its other sites are expected, not a filter that failed. Judged
        # against the whole allowlist, as the tenant-wide endpoint read is; the caller
        # picks its site out of the result.
        return self._scoped(
            companies,
            [company.id for company in companies],
            method="getNetworkInventoryItems",
            targets=self.scope.allowed,
        )

    async def hostnames_by_organisation(self) -> dict[str, set[str]]:
        """Every company's endpoint hostnames from one tenant listing. Sync only."""
        await self.own_company_id()
        items, _ = await self._pages(
            "network", "getNetworkInventoryItems", self._endpoint_params(None)
        )
        found: dict[str, set[str]] = {}
        for item in items:
            if item.get("type") not in ENDPOINT_ITEM_TYPES or not item.get("id"):
                continue
            endpoint = BdEndpoint.from_item(item)
            if endpoint.company_id and endpoint.name:
                found.setdefault(endpoint.company_id, set()).add(normalise_hostname(endpoint.name))
        return found

    async def _inventory(self, *, use_cache: bool = True) -> InventoryEntry[BdEndpoint, str]:
        client_id = self.scope.client_id
        targets = [site.site_id for site in self.scope.targets]
        if not use_cache:
            return await self._list_inventory(targets, ttl_seconds=0)
        entry = self.inventory.get(client_id, version=self.credential_version, targets=targets)
        if entry is not None:
            return entry
        async with self.inventory.flight(client_id):
            entry = self.inventory.get(
                client_id, version=self.credential_version, targets=targets, recheck=True
            )
            if entry is not None:
                return entry
            return await self._list_inventory(targets, ttl_seconds=self.inventory_ttl_seconds)

    def _endpoint_params(self, parent_id: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "filters": {
                "type": {"computers": True, "virtualMachines": True},
                "depth": {"allItemsRecursively": True},
            },
            # includeScanLogs: the date of the last successful scan per row, the one
            # activity date the list gives in bulk (api-notes, "Last seen in bulk").
            "options": {
                "endpoints": {
                    "returnProductOutdated": True,
                    "includeRiskScore": True,
                    "includeScanLogs": True,
                }
            },
        }
        if parent_id is not None:
            params["parentId"] = parent_id
        return params

    async def _list_inventory(
        self, targets: list[uuid.UUID], *, ttl_seconds: float
    ) -> InventoryEntry[BdEndpoint, str]:
        source: Literal["tenant", "sites"]
        if self.scope.covers_every_site:
            await self.own_company_id()
            items, truncated = await self._pages(
                "network", "getNetworkInventoryItems", self._endpoint_params(None)
            )
            endpoints = [
                BdEndpoint.from_item(item)
                for item in items
                if item.get("type") in ENDPOINT_ITEM_TYPES and item.get("id")
            ]
            rows = self._scoped(
                endpoints,
                [endpoint.company_id for endpoint in endpoints],
                method="getNetworkInventoryItems",
                targets=self.scope.allowed,
            )
            endpoint_companies = {e.id: e.company_id for e in endpoints if e.company_id}
            source = "tenant"
        else:

            async def one_site(site: SiteRef) -> tuple[list[tuple[SiteRef, BdEndpoint]], bool]:
                items, cut = await self._pages(
                    "network", "getNetworkInventoryItems", self._endpoint_params(site.external_id)
                )
                endpoints = [
                    BdEndpoint.from_item(item)
                    for item in items
                    if item.get("type") in ENDPOINT_ITEM_TYPES and item.get("id")
                ]
                # The vendor scoped by parentId; a row that still carries no
                # company id is this company's by construction.
                for endpoint in endpoints:
                    if endpoint.company_id is None:
                        endpoint.company_id = site.external_id
                return (
                    self._scoped(
                        endpoints,
                        [e.company_id for e in endpoints],
                        method="getNetworkInventoryItems",
                    ),
                    cut,
                )

            rows = []
            truncated = False
            answers, _ = await self._fan_out_refs(self.scope.targets, one_site)
            for _site, (site_rows, cut) in answers:
                rows.extend(site_rows)
                truncated = truncated or cut
            endpoint_companies = {e.id: site.external_id for site, e in rows}
            source = "sites"
        logger.debug(
            "bitdefender inventory listed client=%s source=%s endpoints=%d truncated=%s",
            self.scope.client_slug,
            source,
            len(rows),
            truncated,
        )
        return self.inventory.put(
            self.scope.client_id,
            version=self.credential_version,
            targets=targets,
            rows=rows,
            device_orgs=endpoint_companies,
            truncated=truncated,
            ttl_seconds=ttl_seconds,
            source=source,
        )

    async def list_endpoints(
        self, *, use_cache: bool = True
    ) -> tuple[list[tuple[SiteRef, BdEndpoint]], bool]:
        """Every endpoint this call targets, tagged with its site, from the inventory."""
        entry = await self._inventory(use_cache=use_cache)
        targeted = {site.site_id for site in self.scope.targets}
        return [row for row in entry.rows if row[0].site_id in targeted], entry.truncated

    async def place_by_endpoint[T](
        self, rows: list[T], endpoint_ids: list[str | None], *, method: str
    ) -> list[tuple[SiteRef, BdEndpoint, T]]:
        """Classify rows that name an endpoint and no company, through the inventory."""
        entry = await self._inventory()
        known = {endpoint.id: (site, endpoint) for site, endpoint in entry.rows}
        resolved: list[T] = []
        company_ids: list[str | None] = []
        unplaced = 0
        for row, endpoint_id in zip(rows, endpoint_ids, strict=True):
            company = entry.device_orgs.get(endpoint_id) if endpoint_id else None
            if company is None:
                unplaced += 1
                continue
            resolved.append(row)
            company_ids.append(company)
        if unplaced:
            logger.log(
                logging.ERROR if entry.source == "tenant" else logging.WARNING,
                "bitdefender %s: %d row(s) name endpoints the inventory does not hold for "
                "client=%s; dropped",
                method,
                unplaced,
                self.scope.client_slug,
            )
        placed: list[tuple[SiteRef, BdEndpoint, T]] = []
        for site, row in self._scoped(resolved, company_ids, method=method):
            endpoint_id = self._endpoint_id_of(row)
            pair = known.get(endpoint_id) if endpoint_id else None
            if pair is not None:
                placed.append((site, pair[1], row))
        return placed

    @staticmethod
    def _endpoint_id_of(row: Any) -> str | None:
        if isinstance(row, BdIncident):
            return row.endpoint_id
        value = (
            row.get("endpointId") if isinstance(row, dict) else getattr(row, "endpoint_id", None)
        )
        return str(value) if value else None

    async def get_endpoint_details(self, site: SiteRef, endpoint_id: str) -> BdEndpointDetail:
        """`getManagedEndpointDetails`, its company checked against the site asked for."""
        result = await self.call(
            "network", "getManagedEndpointDetails", {"endpointId": endpoint_id}
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise NotFound(
                "GravityZone returned no endpoint details",
                connector=self.name.value,
                details={"endpoint_id": endpoint_id, "site": site.slug},
            )
        detail = BdEndpointDetail.model_validate(result)
        company_id = detail.company_id or site.external_id
        kept = self._scoped(
            [detail], [company_id], method="getManagedEndpointDetails", targets=[site]
        )
        if not kept:
            raise NotFound(
                f"endpoint {endpoint_id} is not one of {site.slug}'s",
                connector=self.name.value,
                details={"endpoint_id": endpoint_id, "site": site.slug},
            )
        return detail

    async def list_incidents(
        self,
        *,
        since: dt.datetime,
        until: dt.datetime,
        sites: Sequence[SiteRef] | None = None,
    ) -> tuple[list[tuple[SiteRef, BdIncident]], bool]:
        """EDR/XDR incidents updated in the window, per targeted company.

        `getIncidentsList` (v1.2) filters by `lastUpdated` between `startDate`
        and `endDate`, one company at a time (`companyId`). Rows carry the
        company they belong to; one that does not is placed by its endpoint.
        """
        window = {
            "startDate": since.astimezone(dt.UTC).isoformat(timespec="seconds"),
            "endDate": until.astimezone(dt.UTC).isoformat(timespec="seconds"),
        }

        async def one_site(site: SiteRef) -> tuple[list[BdIncident], bool]:
            items, cut = await self._pages(
                "incidents",
                "getIncidentsList",
                {
                    "filters": {"companyId": site.external_id, **window},
                    "options": {"includeChildCompanies": False},
                },
            )
            return [
                BdIncident.model_validate(item) for item in items if item.get("incidentId")
            ], cut

        targets = list(self.scope.targets if sites is None else sites)
        collected: list[tuple[SiteRef, BdIncident]] = []
        truncated = False
        answers, _ = await self._fan_out_refs(targets, one_site)
        for site, (incidents, cut) in answers:
            truncated = truncated or cut
            with_company = [i for i in incidents if i.company_id]
            without = [i for i in incidents if not i.company_id]
            collected.extend(
                self._scoped(
                    with_company,
                    [i.company_id for i in with_company],
                    method="getIncidentsList",
                    targets=[site],
                )
            )
            if without:
                placed = await self.place_by_endpoint(
                    without, [i.endpoint_id for i in without], method="getIncidentsList"
                )
                collected.extend((placed_site, row) for placed_site, _, row in placed)
        return collected, truncated


#: The process-wide inventory cache for this connector.
INVENTORY: InventoryCache[BdEndpoint, str] = InventoryCache()

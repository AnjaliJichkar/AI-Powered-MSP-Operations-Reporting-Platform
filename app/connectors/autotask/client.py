"""Autotask PSA connector.

Three things differ from NinjaOne and drive the shape of this file.

**Auth is static headers, not a token.** `ApiIntegrationCode`, `UserName` and
`Secret` go on every request; there is nothing to refresh.

**The host is per tenant.** Every Autotask database lives in a numbered zone,
and calling the wrong host fails. `_authenticate` resolves it once through the
zone-neutral endpoint and caches it for the life of the connector, reusing the
base class's auth lock so concurrent calls resolve it once.

**Paging is `nextPageUrl`, not a cursor parameter.** A query returns at most 500
records with a `pageDetails.nextPageUrl` to follow, so `BaseConnector.paginate`
(which appends a cursor parameter) does not apply; `_query_all` follows the
vendor's URLs instead and reports truncation when the page cap stops it.

Scoping: `ToolMapping.external_id` is the Autotask company id. Every query
filters on it and every returned record is checked again in `_scoped`.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, ClassVar

from app.connectors.autotask.models import (
    AutotaskCompany,
    AutotaskCompanyLocation,
    AutotaskConfigurationItem,
    AutotaskContact,
    AutotaskContract,
    AutotaskPhase,
    AutotaskProject,
    AutotaskResource,
    AutotaskServiceCall,
    AutotaskServiceCallTicket,
    AutotaskServiceCallTicketResource,
    AutotaskTicket,
    AutotaskTicketNote,
    AutotaskTimeEntry,
    FieldInformation,
    PageDetails,
    ZoneInformation,
)
from app.connectors.base import BaseConnector
from app.connectors.errors import (
    AuthError,
    ConnectorError,
    ConnectorNotConfigured,
    NotFound,
    UpstreamError,
)
from app.connectors.scope import SiteRef, VendorOrganisation
from app.models.enums import ConnectorName

logger = logging.getLogger(__name__)

#: Zone-neutral host. The only URL that is the same for every tenant.
ZONE_LOOKUP_URL = "https://webservices.autotask.net/atservicesrest/v1.0/zoneInformation"

QUERY_PATH = "/V1.0/{entity}/query"

#: What a POST query's own `nextPageUrl` points at. It takes the paging token
#: in the URL and the filter in the body, and refuses a GET.
NEXT_PAGE_PATH = "/query/next"
FIELDS_PATH = "/V1.0/{entity}/entityInformation/fields"

#: Autotask returns at most 500 records per query, whatever we ask for.
MAX_RECORDS_PER_PAGE = 500

#: And rejects a query carrying more than 500 OR conditions, which is what an
#: `in` filter compiles to. Kept below the limit with room to spare.
#:
#: This is the cap on one caller's own `in` list, and a caller over it reports
#: its answer truncated. It is **not** what keeps a query legal: a filter is
#: several clauses and the limit is on their total, which only `_query_all`
#: knows (see `_split_to_budget` and `autotask_max_filter_conditions`).
MAX_IN_CLAUSE_VALUES = 400


def _hashable(value: Any) -> Any:
    """A filter value as something a set can hold.

    Every `in` value we send is an int or a string, but counting must not be
    the thing that raises if one day it is not.
    """
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def _iso(moment: dt.datetime) -> str:
    """Render a datetime the way Autotask filters expect (see api-notes)."""
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AutotaskClient(BaseConnector):
    """Read-only access to one Autotask company."""

    name: ClassVar[ConnectorName] = ConnectorName.AUTOTASK
    default_base_url: ClassVar[str] = ""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._zone_url: str | None = None
        self._picklists: dict[tuple[str, str], dict[int, str]] = {}

    # --- configuration --------------------------------------------------

    @property
    def base_url(self) -> str:
        """The tenant's API host: configured, or resolved from the zone lookup."""
        configured = self.config.get("base_url")
        if configured:
            return str(configured).rstrip("/")
        if self._zone_url:
            return self._zone_url
        raise UpstreamError(
            "the Autotask zone has not been resolved yet",
            connector=self.name.value,
        )

    @property
    def max_conditions(self) -> int:
        """The condition budget for one query, from settings via the limits."""
        return self.limits.max_filter_conditions

    @property
    def target_company_ids(self) -> list[int]:
        """The Autotask companies this call covers, from the mapped sites."""
        return [site.as_int(self.name.value) for site in self.scope.targets]

    def _allowed_by_company_id(self) -> dict[int, SiteRef]:
        return {site.as_int(self.name.value): site for site in self.scope.allowed}

    # --- auth and zone resolution ---------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Sent on every request. Autotask has no token exchange."""
        return {
            "ApiIntegrationCode": self.secret("api_integration_code"),
            "UserName": self.secret("username"),
            "Secret": self.secret("secret"),
            "Content-Type": "application/json",
        }

    def _is_authenticated(self) -> bool:
        """Nothing expires; we only need the zone before the first call."""
        return bool(self.config.get("base_url")) or self._zone_url is not None

    def _invalidate_auth(self) -> None:
        # A 401 is bad credentials, not a stale zone, but re-resolving is cheap
        # and covers a tenant that has been migrated between zones.
        self._zone_url = None

    async def _authenticate(self) -> None:
        """Resolve which Autotask host serves this tenant.

        Uses an absolute URL and supplies the headers itself, so it does not
        depend on `base_url` — which is what it is about to establish.
        """
        # Read the credentials before the request, not inside the try: a
        # missing field is a credential problem and must be reported as one,
        # not disguised as a zone lookup failure.
        headers = self._auth_headers()
        username = self.secret("username")
        try:
            response = await self.request(
                "GET",
                ZONE_LOOKUP_URL,
                params={"user": username},
                headers=headers,
                authenticated=False,
            )
        except AuthError:
            # Credentials the vendor actively rejected. Already the clearer
            # message of the two; do not rewrite it as a zone problem.
            raise
        except ConnectorError as exc:
            # An unknown user is answered with 500 and an `errors` body, which
            # would otherwise surface as a retryable upstream failure.
            raise AuthError(
                "Autotask could not determine the zone for this API user; check "
                "the user name and that the account is an API-only user",
                connector=self.name.value,
                status_code=exc.status_code,
                details=exc.details,
            ) from exc

        try:
            zone = ZoneInformation.model_validate(self._decode(response, ZONE_LOOKUP_URL))
        except UpstreamError:
            raise
        except Exception as exc:
            raise AuthError(
                "Autotask returned a zone response we could not read",
                connector=self.name.value,
                status_code=response.status_code,
            ) from exc

        self._zone_url = zone.url.rstrip("/")
        logger.debug("autotask zone resolved zone=%s url=%s", zone.zone_name, self._zone_url)

    # --- scoping guard --------------------------------------------------

    def _scoped(self, items: list[Any], company_ids: list[int | None]) -> list[tuple[SiteRef, Any]]:
        """Keep the records this call asked for, each tagged with its site.

        Defence in depth for hard rule 1, and the allowlist is what makes two
        very different failures distinguishable. A record from one of *this
        client's other* sites means a vendor-side filter did not bite: dropped,
        logged as a warning. A record from outside the allowlist means another
        franchisee's data reached us: dropped, logged as an error. A row with no
        company id is dropped too — we do not guess that it is ours.
        """
        allowed = self._allowed_by_company_id()
        targeted = {site.external_id for site in self.scope.targets}
        kept: list[tuple[SiteRef, Any]] = []
        outside = 0
        sibling = 0
        unknown = 0
        for item, company_id in zip(items, company_ids, strict=True):
            site = allowed.get(company_id) if company_id is not None else None
            if site is None:
                if company_id is None:
                    unknown += 1
                else:
                    outside += 1
                continue
            if site.external_id not in targeted:
                sibling += 1
                continue
            kept.append((site, item))

        if outside:
            # Outside the allowlist entirely: another franchisee's data reached
            # us. Never merged in, and worth shouting about.
            logger.error(
                "autotask returned %d record(s) from companies outside the allowlist "
                "of client=%s; dropped",
                outside,
                self.scope.client_slug,
            )
        if sibling or unknown:
            # Inside the client but not what we asked for: a filter that did not
            # bite. Dropped, and worth a look.
            logger.warning(
                "autotask response was not narrowed to %s for client=%s: dropped %d from "
                "the client's other sites and %d with no company id",
                self.scope.describe_targets(),
                self.scope.client_slug,
                sibling,
                unknown,
            )
        return kept

    def _scoped_by_ticket(self, items: list[Any], ticket_ids: list[int | None]) -> list[Any]:
        """Scope records that carry no company id through their ticket.

        TicketNotes and TimeEntries have no `companyID`, so the only way to
        know a row belongs to this client is that its ticket does.
        """
        allowed = self._known_ticket_ids
        kept: list[Any] = []
        dropped = 0
        for item, ticket_id in zip(items, ticket_ids, strict=True):
            if ticket_id is not None and ticket_id in allowed:
                kept.append(item)
            else:
                dropped += 1
        if dropped:
            logger.warning(
                "autotask returned %d records whose ticket does not belong to client=%s",
                dropped,
                self.scope.client_slug,
            )
        return kept

    @property
    def _known_ticket_ids(self) -> set[int]:
        return getattr(self, "_ticket_id_scope", set())

    def _remember_ticket_ids(self, ticket_ids: set[int]) -> None:
        self._ticket_id_scope: set[int] = ticket_ids

    # --- querying -------------------------------------------------------

    @staticmethod
    def _conditions(filters: list[dict[str, Any]]) -> int:
        """How many OR conditions the vendor will expand this filter into.

        A scalar clause is one, an `in` clause is one per value, and a nested
        group is the sum of its items. Counted on de-duplicated values, because
        that is what we send.
        """
        total = 0
        for clause in filters:
            inner = clause.get("items")
            if isinstance(inner, list):
                total += AutotaskClient._conditions(inner)
                continue
            value = clause.get("value")
            total += len(set(map(_hashable, value))) if isinstance(value, list) else 1
        return total

    @classmethod
    def _split_to_budget(
        cls, filters: list[dict[str, Any]], budget: int
    ) -> list[list[dict[str, Any]]]:
        """One filter in, as many queries as it takes to stay under the budget.

        The largest `in` clause is the one split, because splitting two would
        multiply out into a query per pair. Everything else rides along
        unchanged on each part, so each part is the same question over fewer
        values and the answers concatenate.

        Raises `ValueError` — which the agent loop hands back to the model as a
        correctable error — when the clauses that cannot be split already fill
        the budget: a 144-company scope with a 400-value `in` is splittable, a
        600-company scope is a question nobody can ask of this API, and saying
        so beats a 500 from the vendor.
        """
        if cls._conditions(filters) <= budget:
            return [filters]

        widest, values = -1, 0
        for index, clause in enumerate(filters):
            value = clause.get("value")
            if isinstance(value, list) and len(value) > values:
                widest, values = index, len(value)
        if widest < 0:
            raise ValueError(
                f"an Autotask query of {cls._conditions(filters)} conditions is over the "
                f"{budget} this connector allows and holds no `in` filter to split: "
                f"{cls._filter_shape(filters)}"
            )

        others = cls._conditions([c for i, c in enumerate(filters) if i != widest])
        room = budget - others
        if room < 1:
            raise ValueError(
                f"an Autotask query cannot be brought under {budget} conditions: the clauses "
                f"beside its widest `in` filter already carry {others}. Narrow the scope — "
                f"ask one site at a time — rather than asking for everything at once "
                f"({cls._filter_shape(filters)})"
            )

        wanted = list(dict.fromkeys(filters[widest]["value"]))
        parts: list[list[dict[str, Any]]] = []
        for start in range(0, len(wanted), room):
            chunk = wanted[start : start + room]
            part = [dict(clause) for clause in filters]
            part[widest] = {**filters[widest], "value": chunk}
            parts.append(part)
        return parts

    @staticmethod
    def _filter_shape(filters: list[dict[str, Any]]) -> str:
        """A filter described by its operators, fields and sizes — never values.

        `in companyID[58] and gte completedDate and in id[400]` is enough to see
        why a query was refused. The values behind it are ticket and company
        ids, which a log is not the place for.
        """

        def one(clause: dict[str, Any]) -> str:
            op = str(clause.get("op", "?"))
            if "items" in clause:
                inner = clause.get("items")
                parts = [one(item) for item in inner] if isinstance(inner, list) else []
                return f"{op}({', '.join(parts)})"
            field = str(clause.get("field", "?"))
            value = clause.get("value")
            if isinstance(value, list):
                return f"{op} {field}[{len(value)}]"
            if value is None and "value" not in clause:
                return f"{op} {field}"
            return f"{op} {field}[1]"

        return " and ".join(one(clause) for clause in filters) or "no filter"

    async def _query_all(
        self,
        entity: str,
        filters: list[dict[str, Any]],
        *,
        max_pages: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run the query, in as many parts as the condition budget needs.

        Every query in this connector comes through here, which is the only
        place the whole filter is known and therefore the only place its
        conditions can be counted (`_split_to_budget`). A filter inside the
        budget is one query, as it always was.
        """
        parts = self._split_to_budget(filters, self.max_conditions)
        if len(parts) > 1:
            logger.info(
                "autotask %s: %s is over the %d-condition budget; asked as %d queries",
                entity,
                self._filter_shape(filters),
                self.max_conditions,
                len(parts),
            )
        items: list[dict[str, Any]] = []
        truncated = False
        for part in parts:
            found, capped = await self._query_one(entity, part, max_pages=max_pages)
            items.extend(found)
            truncated = truncated or capped
        return items, truncated

    async def _query_one(
        self,
        entity: str,
        filters: list[dict[str, Any]],
        *,
        max_pages: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run one query and follow `nextPageUrl` to the end.

        Returns `(items, truncated)`. Every page is a POST: the first so that a
        long `in` filter cannot breach the 2048-character GET limit, and the
        rest because the `nextPageUrl` Autotask hands back for a POST query is
        `/query/next?paging=…`, which answers a GET with **405** and
        `Allow: POST` (live, 2026-09-30; api-notes). The paging token is in that
        URL and the filter goes in the body again, so the body is sent every
        time. A `?search=` URL — what a GET-initiated query returns, which we
        do not send — is still followed with a GET.
        """
        await self._ensure_authenticated()
        cap = max_pages or self.limits.max_pages
        body = {"filter": filters, "maxRecords": MAX_RECORDS_PER_PAGE}
        url: str | None = f"{self.base_url}{QUERY_PATH.format(entity=entity)}"
        # Which query, and the shape of its filter. A vendor error names
        # neither, and the values are ids we do not put in a log.
        logger.debug("autotask query %s: %s", entity, self._filter_shape(filters))

        items: list[dict[str, Any]] = []
        seen_urls: set[str] = set()

        for page in range(cap):
            if url is None:
                return items, False
            if page == 0 or NEXT_PAGE_PATH in url:
                response = await self.request("POST", url, json_body=body)
                payload = self._decode(response, url)
            else:
                payload = await self.get_json(url)

            if not isinstance(payload, dict) or "items" not in payload:
                raise UpstreamError(
                    f"Autotask returned a {entity} response without an items array",
                    connector=self.name.value,
                    details={"entity": entity},
                )
            batch = payload["items"]
            if not isinstance(batch, list):
                raise UpstreamError(
                    f"Autotask returned {type(batch).__name__} where a list of "
                    f"{entity} was expected",
                    connector=self.name.value,
                    details={"entity": entity},
                )
            items.extend(batch)

            details = PageDetails.model_validate(payload.get("pageDetails") or {})
            url = details.next_page_url
            if not url:
                return items, False
            if url in seen_urls:
                # The vendor is handing back a page we have already read.
                logger.warning("autotask repeated a page url for %s; stopping", entity)
                return items, False
            seen_urls.add(url)

        return items, True

    def _company_filter(self) -> dict[str, Any]:
        """One filter covering every targeted company.

        Autotask supports `in`, so a multi-site read is still a single query —
        unlike NinjaOne, whose device endpoint is path-scoped per organisation.
        """
        companies = self.target_company_ids
        if len(companies) == 1:
            return {"op": "eq", "field": "companyID", "value": companies[0]}
        return {"op": "in", "field": "companyID", "value": companies[:MAX_IN_CLAUSE_VALUES]}

    # --- picklists ------------------------------------------------------

    async def picklist(self, entity: str, field: str) -> dict[int, str]:
        """Value-to-label map for one per-tenant picklist, cached per instance.

        Autotask status and priority numbers mean different things in different
        databases, so a label can only come from the tenant (api-notes).
        """
        key = (entity, field)
        if key in self._picklists:
            return self._picklists[key]

        await self._ensure_authenticated()
        url = f"{self.base_url}{FIELDS_PATH.format(entity=entity)}"
        payload = await self.get_json(url)
        fields = (payload or {}).get("fields") if isinstance(payload, dict) else None
        mapping: dict[int, str] = {}
        for raw in fields or []:
            information = FieldInformation.model_validate(raw)
            if (information.name or "").lower() != field.lower() or not information.is_pick_list:
                continue
            # `options` reads a null `picklistValues` as empty; an option with no
            # label or a non-numeric value is skipped rather than invented.
            for option in information.options:
                if option.value is None or option.label is None:
                    continue
                try:
                    mapping[int(option.value)] = option.label
                except ValueError:
                    continue
            break

        self._picklists[key] = mapping
        return mapping

    async def label_for(self, entity: str, field: str, value: int | None) -> str | None:
        """A picklist label, or an honest 'unknown' rather than an invented one."""
        if value is None:
            return None
        try:
            mapping = await self.picklist(entity, field)
        except ConnectorError:
            # A tenant that will not tell us its picklist still gets a number.
            return f"{value} (label unavailable)"
        label = mapping.get(value)
        return label if label else f"{value} (unknown)"

    async def resolve_picklist_value(self, entity: str, field: str, wanted: str) -> int:
        """Turn 'High' (or '3') into the number this tenant uses."""
        if wanted.strip().isdigit():
            return int(wanted.strip())
        mapping = await self.picklist(entity, field)
        for value, label in mapping.items():
            if label.strip().casefold() == wanted.strip().casefold():
                return value
        known = ", ".join(sorted(mapping.values())) or "none reported"
        raise NotFound(
            f"{wanted!r} is not a {field} this Autotask database uses; it has: {known}",
            connector=self.name.value,
        )

    # --- reads ----------------------------------------------------------

    async def list_tickets(
        self,
        *,
        open_only: bool = True,
        created_before: dt.datetime | None = None,
        active_since: dt.datetime | None = None,
        created_since: dt.datetime | None = None,
        completed_since: dt.datetime | None = None,
        completed_before: dt.datetime | None = None,
        priority: int | None = None,
        status: int | None = None,
        queue_id: int | None = None,
        text: str | None = None,
        assigned_resource_ids: list[int] | None = None,
        contact_ids: list[int] | None = None,
        configuration_item_ids: list[int] | None = None,
    ) -> tuple[list[tuple[SiteRef, AutotaskTicket]], bool]:
        """Tickets across the targeted sites, each tagged with the site it belongs to.

        `priority` and `status` are this tenant's picklist numbers, resolved by
        the caller from labels; they are never hardcoded here. The id lists
        (`assigned_resource_ids`, `contact_ids`, `configuration_item_ids`) are
        resolved by the caller too; an empty list means "nothing can match"
        and returns nothing without a request.
        """
        filters: list[dict[str, Any]] = [self._company_filter()]
        if open_only:
            # Not a status number: statuses are per-tenant (api-notes).
            filters.append({"op": "notExist", "field": "completedDate"})
        if created_before is not None:
            filters.append({"op": "lt", "field": "createDate", "value": _iso(created_before)})
        if created_since is not None:
            filters.append({"op": "gte", "field": "createDate", "value": _iso(created_since)})
        if completed_since is not None:
            filters.append({"op": "gte", "field": "completedDate", "value": _iso(completed_since)})
        if completed_before is not None:
            filters.append({"op": "lt", "field": "completedDate", "value": _iso(completed_before)})
        if active_since is not None:
            filters.append({"op": "gte", "field": "lastActivityDate", "value": _iso(active_since)})
        if priority is not None:
            filters.append({"op": "eq", "field": "priority", "value": priority})
        if status is not None:
            filters.append({"op": "eq", "field": "status", "value": status})
        if queue_id is not None:
            filters.append({"op": "eq", "field": "queueID", "value": queue_id})
        if text:
            # `contains` is case-insensitive (verified live, api-notes).
            filters.append(self._name_filter(text, ("title", "description")))
        for field, ids in (
            ("assignedResourceID", assigned_resource_ids),
            ("contactID", contact_ids),
        ):
            if ids is not None:
                if not ids:
                    return [], False
                filters.append({"op": "in", "field": field, "value": ids[:MAX_IN_CLAUSE_VALUES]})
        if configuration_item_ids is not None:
            if not configuration_item_ids:
                return [], False
            filters.append(
                {
                    "op": "in",
                    "field": "configurationItemID",
                    "value": configuration_item_ids[:MAX_IN_CLAUSE_VALUES],
                }
            )

        raw, truncated = await self._query_all("Tickets", filters)
        tickets = [AutotaskTicket.model_validate(item) for item in raw]
        scoped = self._scoped(tickets, [ticket.company_id for ticket in tickets])
        self._remember_ticket_ids({ticket.id for _, ticket in scoped})
        return scoped, truncated

    async def get_tickets_by_ids(
        self, ticket_ids: list[int]
    ) -> list[tuple[SiteRef, AutotaskTicket]]:
        """Tickets by id, through the company filter: only this client's come back.

        This is how a record with no company of its own (a note) is placed: ask
        for its ticket *with* the company filter, and a ticket that is not this
        client's simply is not returned.
        """
        wanted = list(dict.fromkeys(ticket_ids))
        found: list[tuple[SiteRef, AutotaskTicket]] = []
        for start in range(0, len(wanted), MAX_IN_CLAUSE_VALUES):
            chunk = wanted[start : start + MAX_IN_CLAUSE_VALUES]
            raw, _ = await self._query_all(
                "Tickets", [self._company_filter(), {"op": "in", "field": "id", "value": chunk}]
            )
            tickets = [AutotaskTicket.model_validate(item) for item in raw]
            found.extend(self._scoped(tickets, [ticket.company_id for ticket in tickets]))
        return found

    async def search_ticket_notes(
        self, text: str, *, created_since: dt.datetime | None = None
    ) -> tuple[list[AutotaskTicketNote], bool]:
        """Notes whose title or body contains `text`. NOT scoped: the caller must place them.

        `TicketNotes` has no company field and cannot be joined to `Tickets` in
        one query (api-notes), so this search is tenant-wide. Every note comes
        back unplaced; `search_tickets` resolves each one's ticket through the
        company filter and drops the rest.
        """
        filters: list[dict[str, Any]] = [self._name_filter(text, ("title", "description"))]
        if created_since is not None:
            filters.append({"op": "gte", "field": "createDateTime", "value": _iso(created_since)})
        raw, truncated = await self._query_all("TicketNotes", filters)
        return [AutotaskTicketNote.model_validate(item) for item in raw], truncated

    async def list_service_calls(
        self, *, starting_since: dt.datetime
    ) -> tuple[list[tuple[SiteRef, AutotaskServiceCall]], bool]:
        """Service calls for the targeted sites that start on or after a moment."""
        filters: list[dict[str, Any]] = [
            self._company_filter(),
            {"op": "gte", "field": "startDateTime", "value": _iso(starting_since)},
        ]
        raw, truncated = await self._query_all("ServiceCalls", filters)
        calls = [AutotaskServiceCall.model_validate(item) for item in raw]
        return self._scoped(calls, [call.company_id for call in calls]), truncated

    async def service_call_links(
        self, service_call_ids: list[int]
    ) -> tuple[list[AutotaskServiceCallTicket], list[AutotaskServiceCallTicketResource]]:
        """The tickets on these service calls, and who is assigned to each.

        Neither join entity has a company field. They are reached only through
        service calls already scoped to this client, and the tickets they name
        are fetched through the company filter before anything is shown.
        """
        wanted = list(dict.fromkeys(service_call_ids))[:MAX_IN_CLAUSE_VALUES]
        if not wanted:
            return [], []
        raw, _ = await self._query_all(
            "ServiceCallTickets", [{"op": "in", "field": "serviceCallID", "value": wanted}]
        )
        tickets = [
            link
            for link in (AutotaskServiceCallTicket.model_validate(item) for item in raw)
            if link.service_call_id in set(wanted)
        ]
        link_ids = [link.id for link in tickets][:MAX_IN_CLAUSE_VALUES]
        if not link_ids:
            return tickets, []
        raw, _ = await self._query_all(
            "ServiceCallTicketResources",
            [{"op": "in", "field": "serviceCallTicketID", "value": link_ids}],
        )
        resources = [
            link
            for link in (AutotaskServiceCallTicketResource.model_validate(item) for item in raw)
            if link.service_call_ticket_id in set(link_ids)
        ]
        return tickets, resources

    async def get_ticket_by_number(
        self, ticket_number: str
    ) -> tuple[SiteRef, AutotaskTicket] | None:
        """One ticket by the number an engineer reads off the console."""
        filters = [
            self._company_filter(),
            {"op": "eq", "field": "ticketNumber", "value": ticket_number},
        ]
        raw, _ = await self._query_all("Tickets", filters, max_pages=1)
        tickets = [AutotaskTicket.model_validate(item) for item in raw]
        scoped = self._scoped(tickets, [ticket.company_id for ticket in tickets])
        self._remember_ticket_ids({ticket.id for _, ticket in scoped})
        return scoped[0] if scoped else None

    async def list_ticket_notes(self, ticket_id: int) -> list[AutotaskTicketNote]:
        """Notes on one ticket, scoped through the ticket, newest first."""
        filters = [{"op": "eq", "field": "ticketID", "value": ticket_id}]
        raw, _ = await self._query_all("TicketNotes", filters)
        notes = [AutotaskTicketNote.model_validate(item) for item in raw]
        scoped = self._scoped_by_ticket(notes, [note.ticket_id for note in notes])
        return sorted(
            scoped,
            key=lambda note: note.create_date_time or dt.datetime.min.replace(tzinfo=dt.UTC),
            reverse=True,
        )

    async def find_configuration_items(
        self, device_name: str
    ) -> list[tuple[SiteRef, AutotaskConfigurationItem]]:
        """Devices whose title, RMM hostname or reference number matches."""
        filters = [
            self._company_filter(),
            {
                "op": "or",
                "items": [
                    {"op": "contains", "field": "referenceTitle", "value": device_name},
                    {"op": "contains", "field": "rmmDeviceAuditHostname", "value": device_name},
                    {"op": "eq", "field": "referenceNumber", "value": device_name},
                ],
            },
        ]
        raw, _ = await self._query_all("ConfigurationItems", filters)
        items = [AutotaskConfigurationItem.model_validate(item) for item in raw]
        return self._scoped(items, [item.company_id for item in items])

    async def list_time_entries(
        self, ticket_ids: list[int], *, worked_since: dt.datetime
    ) -> tuple[list[AutotaskTimeEntry], bool]:
        """Time logged against the given tickets since a date.

        TimeEntries carry no company id, so the ticket list is the scope: the
        caller must pass ids that are already known to be this client's.

        A caller with more tickets than one `in` filter takes batches them and
        says so: the answer is reported truncated rather than quietly short.
        """
        if not ticket_ids:
            return [], False
        capped = ticket_ids[:MAX_IN_CLAUSE_VALUES]
        filters: list[dict[str, Any]] = [
            {"op": "in", "field": "ticketID", "value": capped},
            {"op": "gte", "field": "dateWorked", "value": _iso(worked_since)},
        ]
        raw, truncated = await self._query_all("TimeEntries", filters)
        entries = [AutotaskTimeEntry.model_validate(item) for item in raw]
        scoped = self._scoped_by_ticket(entries, [entry.ticket_id for entry in entries])
        return scoped, truncated or len(capped) < len(ticket_ids)

    async def list_organisations(self) -> list[VendorOrganisation]:
        """Active customer companies, for `sync_sites`.

        Deliberately unscoped: this is what discovers the sites in the first
        place, so there is no allowlist to check against yet.

        Only companies whose `companyType` resolves to this tenant's "Customer"
        label are returned — prospects, vendors and partners are not sites.
        The label is resolved through the picklist rather than assumed to be
        number 1, because picklists are per tenant (api-notes). Company 0 is
        the franchisee's own placeholder in every Autotask database and is
        skipped outright.
        """
        customer = await self._customer_company_type()
        filters: list[dict[str, Any]] = [
            {"op": "gt", "field": "id", "value": 0},
            {"op": "eq", "field": "isActive", "value": True},
            {"op": "eq", "field": "companyType", "value": customer},
        ]
        raw, _ = await self._query_all("Companies", filters)
        organisations: list[VendorOrganisation] = []
        for item in raw:
            company_id = item.get("id")
            if not company_id:
                continue
            # The vendor-side filter is checked again here: a company that is
            # not an active customer must not become a site, whatever came back.
            if item.get("isActive") is not True or item.get("companyType") != customer:
                continue
            organisations.append(
                VendorOrganisation(
                    external_id=str(company_id),
                    name=str(item.get("companyName") or f"company {company_id}"),
                )
            )
        return organisations

    async def _customer_company_type(self) -> int:
        """The number this tenant uses for a customer company."""
        mapping = await self.picklist("Companies", "companyType")
        for value, label in mapping.items():
            if label.strip().casefold() == "customer":
                return value
        known = ", ".join(sorted(mapping.values())) or "none reported"
        raise ConnectorNotConfigured(
            "this Autotask database has no company type labelled 'Customer', so its "
            f"customers cannot be told apart from prospects and vendors. It has: {known}",
            connector=self.name.value,
        )

    async def get_resources(self, resource_ids: list[int]) -> dict[int, AutotaskResource]:
        """Names for assigned technicians. Resources are MSP staff, not client data."""
        wanted = list(dict.fromkeys(resource_ids))
        if not wanted:
            return {}
        filters = [{"op": "in", "field": "id", "value": wanted[:MAX_IN_CLAUSE_VALUES]}]
        raw, _ = await self._query_all("Resources", filters)
        resources = [AutotaskResource.model_validate(item) for item in raw]
        return {resource.id: resource for resource in resources}

    async def list_active_resources(self) -> list[AutotaskResource]:
        """Every active resource: the MSP's staff, for matching an engineer by name."""
        raw, _ = await self._query_all(
            "Resources", [{"op": "eq", "field": "isActive", "value": True}]
        )
        return [AutotaskResource.model_validate(item) for item in raw]

    async def find_resources(self, name: str) -> list[AutotaskResource]:
        """Active resources whose name or login contains `name`, case-insensitively."""
        return [
            resource for resource in await self.list_active_resources() if resource.matches(name)
        ]

    def _name_filter(self, name: str, fields: tuple[str, ...]) -> dict[str, Any]:
        return {
            "op": "or",
            "items": [{"op": "contains", "field": field, "value": name} for field in fields],
        }

    async def list_contacts(
        self, *, name: str | None = None, active_only: bool = True
    ) -> tuple[list[tuple[SiteRef, AutotaskContact]], bool]:
        """Contacts across the targeted sites, optionally matching a name or email."""
        filters: list[dict[str, Any]] = [self._company_filter()]
        if active_only:
            # `isActive` is an int32 on ContactModel, not a boolean (api-notes).
            filters.append({"op": "eq", "field": "isActive", "value": 1})
        if name:
            filters.append(self._name_filter(name, ("firstName", "lastName", "emailAddress")))
        raw, truncated = await self._query_all("Contacts", filters)
        contacts = [AutotaskContact.model_validate(item) for item in raw]
        return self._scoped(contacts, [contact.company_id for contact in contacts]), truncated

    async def list_configuration_items(
        self, *, name: str | None = None, active_only: bool = True
    ) -> tuple[list[tuple[SiteRef, AutotaskConfigurationItem]], bool]:
        """Configuration items (assets) across the targeted sites, optionally by name."""
        filters: list[dict[str, Any]] = [self._company_filter()]
        if active_only:
            filters.append({"op": "eq", "field": "isActive", "value": True})
        if name:
            filters.append(
                self._name_filter(
                    name,
                    ("referenceTitle", "rmmDeviceAuditHostname", "referenceNumber", "serialNumber"),
                )
            )
        raw, truncated = await self._query_all("ConfigurationItems", filters)
        items = [AutotaskConfigurationItem.model_validate(item) for item in raw]
        return self._scoped(items, [item.company_id for item in items]), truncated

    async def get_company(self, site: SiteRef) -> AutotaskCompany | None:
        """One site's company record, checked against the allowlist like any other row."""
        filters = [{"op": "eq", "field": "id", "value": site.as_int(self.name.value)}]
        raw, _ = await self._query_all("Companies", filters, max_pages=1)
        companies = [AutotaskCompany.model_validate(item) for item in raw]
        scoped = self._scoped(companies, [company.id for company in companies])
        for placed_site, placed in scoped:
            if placed_site.site_id == site.site_id:
                company: AutotaskCompany = placed
                return company
        return None

    async def get_site_companies(self, site_id: uuid.UUID) -> list[tuple[SiteRef, AutotaskCompany]]:
        """Every Autotask company one site holds, in mapping order.

        A site may hold two companies (migration 0013). `get_company(ref)` is
        one company; this is the site, so a card or a report reads all of them
        rather than whichever mapping happened to come first. A company the
        vendor no longer returns is left out; the caller sees the ones it does.
        """
        found: list[tuple[SiteRef, AutotaskCompany]] = []
        for ref in self.scope.refs_for_site(site_id):
            company = await self.get_company(ref)
            if company is not None:
                found.append((ref, company))
        return found

    async def count_site_active_contracts(self, site_id: uuid.UUID) -> int:
        """Active contracts across every company the site holds, in one query."""
        refs = self.scope.refs_for_site(site_id)
        if not refs:
            return 0
        active = await self.resolve_picklist_value("Contracts", "status", "Active")
        companies = [ref.as_int(self.name.value) for ref in refs]
        company_filter: dict[str, Any] = (
            {"op": "eq", "field": "companyID", "value": companies[0]}
            if len(companies) == 1
            else {"op": "in", "field": "companyID", "value": companies[:MAX_IN_CLAUSE_VALUES]}
        )
        raw, _ = await self._query_all(
            "Contracts", [company_filter, {"op": "eq", "field": "status", "value": active}]
        )
        contracts = [
            contract
            for contract in (AutotaskContract.model_validate(item) for item in raw)
            if contract.status == active
        ]
        return len(self._scoped(contracts, [contract.company_id for contract in contracts]))

    async def list_company_locations(self) -> list[tuple[SiteRef, AutotaskCompanyLocation]]:
        """The targeted companies' active locations, each tagged with its site."""
        raw, _ = await self._query_all(
            "CompanyLocations",
            [self._company_filter(), {"op": "eq", "field": "isActive", "value": True}],
        )
        locations = [AutotaskCompanyLocation.model_validate(item) for item in raw]
        return self._scoped(locations, [location.company_id for location in locations])

    async def list_projects(
        self, *, statuses: list[int] | None = None, project_types: list[int] | None = None
    ) -> tuple[list[tuple[SiteRef, AutotaskProject]], bool]:
        """Projects of the targeted companies, each tagged with its site.

        `statuses` and `project_types` are this tenant's picklist numbers,
        resolved by the caller from labels. An empty list means nothing can
        match and returns nothing without a request.
        """
        filters: list[dict[str, Any]] = [self._company_filter()]
        for field, values in (("status", statuses), ("projectType", project_types)):
            if values is not None:
                if not values:
                    return [], False
                filters.append({"op": "in", "field": field, "value": values})
        raw, truncated = await self._query_all("Projects", filters)
        projects = [AutotaskProject.model_validate(item) for item in raw]
        return self._scoped(projects, [project.company_id for project in projects]), truncated

    async def list_phases(self, project_ids: list[int]) -> list[AutotaskPhase]:
        """Phases of the given projects.

        A phase has no company field, so it is only ever asked for by the ids of
        projects that already came back through the company filter, and any
        phase naming another project is dropped.
        """
        wanted = list(dict.fromkeys(project_ids))[:MAX_IN_CLAUSE_VALUES]
        if not wanted:
            return []
        raw, _ = await self._query_all(
            "Phases", [{"op": "in", "field": "projectID", "value": wanted}]
        )
        phases = [AutotaskPhase.model_validate(item) for item in raw]
        return [phase for phase in phases if phase.project_id in set(wanted)]

    async def count_active_contracts(self, site: SiteRef) -> int:
        """How many contracts with the tenant's "Active" status the company has.

        Raises `UpstreamError` when the tenant refuses the entity (the pilot
        franchisee's API user gets HTTP 500 on every Contracts query,
        api-notes); the caller reports that rather than a number.
        """
        active = await self.resolve_picklist_value("Contracts", "status", "Active")
        filters = [
            {"op": "eq", "field": "companyID", "value": site.as_int(self.name.value)},
            {"op": "eq", "field": "status", "value": active},
        ]
        raw, _ = await self._query_all("Contracts", filters)
        # The status filter is checked again here: an inactive contract that
        # came back anyway must not be counted as active.
        contracts = [
            contract
            for contract in (AutotaskContract.model_validate(item) for item in raw)
            if contract.status == active
        ]
        scoped = self._scoped(contracts, [contract.company_id for contract in contracts])
        return len(scoped)

    async def get_contacts(self, contact_ids: list[int]) -> dict[int, AutotaskContact]:
        """Contacts for this company, by id. Scoped like any other client record."""
        wanted = list(dict.fromkeys(contact_ids))
        if not wanted:
            return {}
        filters = [
            self._company_filter(),
            {"op": "in", "field": "id", "value": wanted[:MAX_IN_CLAUSE_VALUES]},
        ]
        raw, _ = await self._query_all("Contacts", filters)
        contacts = [AutotaskContact.model_validate(item) for item in raw]
        scoped = self._scoped(contacts, [contact.company_id for contact in contacts])
        return {contact.id: contact for _, contact in scoped}

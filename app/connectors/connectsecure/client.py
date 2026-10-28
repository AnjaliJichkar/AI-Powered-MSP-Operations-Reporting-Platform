"""ConnectSecure v4 connector (vulnerability scanning), read-only by allowlist.

**Read-only is enforced here, not by the credential.** A ConnectSecure API key
belongs to a user, and a user's role decides what it may do; the ADMIN role
the vendor hands out by default can create companies, start scans and delete
assets with the same key that lists them. So this client sends exactly two
kinds of request: the authentication call (`POST /w/authorize`) and `GET`s
to paths on `READ_ONLY_PATHS`. `request()` refuses anything else with
`ReadOnlyViolation` before a request is built (hard rule 2), whatever a caller
asks for. The pre-flight (`validate_connector.py --check-connectsecure`) also
reads the key's own user and warns when its role is more than a viewer.

The vendor's URL convention makes the allowlist legible: `/r/...` reads,
`/w/...` writes, `/d/...` deletes. Only `/r/` paths are listed, and the list
is exact paths, never a prefix, so a new read is a deliberate addition.

Auth (docs/api-notes/connectsecure.md): `Client-Auth-Token: base64(utf8(
"<tenant>+<client_id>:<client_secret>"))` on `/w/authorize` returns an
`access_token` (good for at most two hours) and a `user_id`; every later call
carries `Authorization: Bearer <token>` and `X-USER-ID: <user_id>`. The pair is
held per client and credential version in a process-wide store, as NinjaOne's
tokens are, so a question that makes three tool calls authenticates once.

Filters (live, 2026-09-22): the grammar is `field=value` everywhere, strings
quoted; an unknown field or value on `/r/company/*` and `/r/asset/*` returns
**zero rows silently**, on `/r/report_queries/*` an unknown field or an
unquoted string is **ignored and every row returned**. So a condition is only
ever built by `build_condition` from `FILTER_FIELDS` (a fixed map of fields
seen honoured, strings always quoted), every filter is applied again on our
side, and a filtered read whose `total` equals the unfiltered total of the
same query is an error, not a result.

Paging (live): `skip` is a record offset, but the report views have no stable
order across reads, so an offset walk drops and duplicates rows. Every listing
is therefore read as **one page sized to the vendor's `total`** (a `limit` of
10,000 was honoured), up to `CONNECTSECURE_MAX_ROWS`; beyond that the read is
`truncated`, never silently partial. The vulnerability and remediation views
are read per site for that reason: a site is thousands of rows, the tenant is
hundreds of thousands.

Scoping is the same two levels as every connector. The key sees the
franchisee's whole tenant; each end customer is a ConnectSecure *company*
(an integer id) held as a `Site`. A site-scoped read sends `company_id=<id>`
and `_scoped` checks every row's company against the client's full allowlist
again; an unscoped read of a small listing classifies the tenant row by row.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar, Literal

import httpx

from app.connectors.base import BaseConnector
from app.connectors.connectsecure.ledger import LEDGER, RequestLedger
from app.connectors.connectsecure.models import (
    PROBLEM_GROUP_CISA_KEV,
    CsAgent,
    CsAsset,
    CsCompany,
    CsCompanyStats,
    CsRemediation,
    CsUser,
    CsVulnerability,
)
from app.connectors.errors import AuthError, UpstreamError
from app.connectors.inventory import InventoryCache, InventoryEntry
from app.connectors.matching import normalise_hostname
from app.connectors.ninjaone.tokens import TokenKey, TokenStore
from app.connectors.scope import SiteRef, VendorOrganisation
from app.models.enums import ConnectorName
from app.settings import get_settings

logger = logging.getLogger(__name__)

AUTHORIZE_PATH = "/w/authorize"

COMPANIES_PATH = "/r/company/companies"
#: Per (client, credential version): the tenant's companies sync leaves unmapped,
#: with the reason, kept for the inventory's TTL (`_load_known_excluded`).
_KNOWN_EXCLUDED: dict[tuple[uuid.UUID, str | None], tuple[float, dict[str, str]]] = {}
COMPANY_STATS_PATH = "/r/company/company_stats"
AGENTS_PATH = "/r/company/agents"
ASSETS_PATH = "/r/asset/asset_view"
ASSET_STATS_PATH = "/r/asset/asset_stats"
VULNERABILITIES_PATH = "/r/report_queries/asset_wise_vulnerabilities"
VULNERABILITY_DETAILS_PATH = "/r/report_queries/vulnerabilities_details"
REMEDIATIONS_PATH = "/r/report_queries/get_remediation"
USERS_PATH = "/r/user/get_users"

#: Every path this connector may GET. Exact paths; a `{id}` suffix is allowed
#: only for the paths in `READ_ONLY_ITEM_PATHS`. Everything is under `/r/`,
#: the vendor's read prefix, and every one is a listing or a lookup. Each was
#: called live on 2026-09-22 and its row shape recorded (api-notes).
READ_ONLY_PATHS: frozenset[str] = frozenset(
    {
        COMPANIES_PATH,
        COMPANY_STATS_PATH,
        AGENTS_PATH,
        ASSETS_PATH,
        ASSET_STATS_PATH,
        VULNERABILITIES_PATH,
        VULNERABILITY_DETAILS_PATH,
        REMEDIATIONS_PATH,
        USERS_PATH,
    }
)
READ_ONLY_ITEM_PATHS: frozenset[str] = frozenset({COMPANIES_PATH, AGENTS_PATH, ASSETS_PATH})

#: The filter fields the vendor was seen to honour, per path, with their type.
#: A field not here is never sent (`build_condition` refuses it), because an
#: unknown field either empties or widens the result without an error.
FILTER_FIELDS: Mapping[str, Mapping[str, type]] = {
    COMPANIES_PATH: {"id": int},
    COMPANY_STATS_PATH: {"company_id": int},
    AGENTS_PATH: {"company_id": int},
    ASSETS_PATH: {"company_id": int, "platform": str, "cisa_vul": int},
    ASSET_STATS_PATH: {"company_id": int},
    VULNERABILITIES_PATH: {
        "company_id": int,
        "severity": str,
        "problem_group_id": int,
        "unconfirmed": bool,
    },
    VULNERABILITY_DETAILS_PATH: {"company_id": int, "problem_group_id": int},
    REMEDIATIONS_PATH: {"company_id": int, "unconfirmed": bool},
}

#: The vendor reports every finding twice over: *confirmed*, where the scanner
#: proved the vulnerable build is present, and *unconfirmed*, where it inferred
#: one and has not verified it. `unconfirmed=false` narrows both the findings
#: and the remediation view to the confirmed population, and it reconciles
#: exactly with the per-asset counters (`cisa_vul` and friends are confirmed).
#: Live, 2026-09-22: 3,869 confirmed of 8,278 findings on the pilot site.
CONFIRMED_ONLY = {"unconfirmed": False}

#: Access tokens last at most two hours (vendor); refresh a little early.
TOKEN_LIFETIME_SECONDS = 2 * 60 * 60
TOKEN_EXPIRY_MARGIN_SECONDS = 300

#: The pod hosts: `https://pod<N>.myconnectsecure.com` for the current
#: generation, `https://pod<N>.connectsecure.com` for older tenants.
POD_HOST = "https://pod{pod}.myconnectsecure.com"


class ReadOnlyViolation(PermissionError):
    """A caller asked this connector for something that is not a read. No request was made."""


#: How a filter field is read back off a row, when the row spells it differently.
_ROW_ALIASES: Mapping[str, tuple[str, ...]] = {
    "company_id": ("company_id", "company_ids"),
}

#: Filters the rows carry no field for, so they cannot be checked again on our
#: side: `unconfirmed` and `problem_group_id` narrow the query, and the row
#: shows neither. Their effect is verified by the counts reconciling with the
#: per-asset counters (api-notes), not row by row.
_UNCHECKABLE: frozenset[str] = frozenset({"unconfirmed", "problem_group_id"})


def _row_matches(row: Mapping[str, Any], fields: Mapping[str, Any]) -> bool:
    """Every filter we sent, applied again to one row. An absent field cannot be checked."""
    for name, wanted in fields.items():
        if wanted is None or name in _UNCHECKABLE:
            continue
        found: Any = None
        for alias in _ROW_ALIASES.get(name, (name,)):
            if alias in row:
                found = row[alias]
                break
        else:
            continue  # the row does not carry it: nothing to check against
        if isinstance(found, list):
            if not any(str(item) == str(wanted) for item in found):
                return False
        elif str(found).casefold() != str(wanted).casefold():
            return False
    return True


def build_condition(path: str, **fields: Any) -> str:
    """`company_id=42` and `platform='windows'` joined with AND, for one path.

    Only fields in `FILTER_FIELDS[path]`; integers bare, strings always quoted
    (an unquoted string is ignored by the report views), a quote inside a
    value doubled. A `ValueError` names the accepted fields.
    """
    allowed = FILTER_FIELDS.get(path)
    if allowed is None:
        raise ValueError(f"{path} takes no condition")
    parts: list[str] = []
    for name, value in fields.items():
        if value is None:
            continue
        kind = allowed.get(name)
        if kind is None:
            raise ValueError(
                f"{name!r} is not a filter field of {path}; accepted: {', '.join(sorted(allowed))}"
            )
        if kind is bool:
            # A bare true/false: the vendor ignores a quoted boolean.
            parts.append(f"{name}={'true' if value else 'false'}")
        elif kind is int:
            parts.append(f"{name}={int(value)}")
        else:
            text = str(value).replace("'", "''")
            parts.append(f"{name}='{text}'")
    return " AND ".join(parts)


class ConnectSecureClient(BaseConnector):
    """Read-only access to one franchisee's ConnectSecure tenant."""

    name: ClassVar[ConnectorName] = ConnectorName.CONNECTSECURE
    default_base_url: ClassVar[str] = ""

    def __init__(
        self,
        *,
        concurrency: int | None = None,
        inventory_ttl_seconds: float | None = None,
        page_size: int | None = None,
        max_rows: int | None = None,
        inventory: InventoryCache[CsAsset, str] | None = None,
        token_store: TokenStore | None = None,
        ledger: RequestLedger | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        settings = get_settings()
        # Hard rule 6: fan-out width, cache TTL and page size are settings.
        self.concurrency = (
            concurrency if concurrency is not None else settings.connectsecure_concurrency
        )
        self.inventory_ttl_seconds = (
            inventory_ttl_seconds
            if inventory_ttl_seconds is not None
            else settings.connectsecure_inventory_ttl_seconds
        )
        self.page_size = page_size if page_size is not None else settings.connectsecure_page_size
        self.max_rows = max_rows if max_rows is not None else settings.connectsecure_max_rows
        self.inventory: InventoryCache[CsAsset, str] = (
            inventory if inventory is not None else INVENTORY
        )
        self.ledger = ledger if ledger is not None else LEDGER
        self._semaphore: asyncio.Semaphore | None = None
        self._session: str | None = None
        """`"<user_id> <access_token>"` this instance last sent; a 401 discards that one."""
        if token_store is not None:
            self.sessions = token_store
        else:
            self.sessions = SESSIONS if self.credential_version is not None else TokenStore()
        host = str(self.config.get("base_url") or self.config.get("pod") or "").lower()
        self._token_key: TokenKey = (
            self.scope.client_id,
            self.credential_version,
            host,
            str(self.config.get("tenant") or ""),
        )
        self._ledger_key = (self.scope.client_id, host)
        self._auth_lock = self.sessions.lock(self._token_key)
        self._totals: dict[str, int] = {}
        """The unfiltered `total` per path, for the filter-ignored check."""
        self._company_count: int | None = None
        self._known_excluded: dict[str, str] | None = None
        """Company id -> why sync leaves it unmapped (`deleted`, `assessment`), or
        `unsynced` for a live company no site holds; loaded on the first row the
        guard cannot place (`_load_known_excluded`)."""
        self.calls_made: list[tuple[str, str]] = []
        """`(method, path)` of every request sent, for tests and validation runs."""

    # --- configuration --------------------------------------------------

    @property
    def base_url(self) -> str:
        """`base_url` in the credential config wins; otherwise the pod number."""
        configured = self.config.get("base_url")
        if configured:
            return str(configured).rstrip("/")
        pod = str(self.config.get("pod") or "").strip().lower().removeprefix("pod")
        if not pod:
            raise AuthError(
                "the ConnectSecure credential needs a pod number or a base_url; the console "
                "shows the pod under its Info button",
                connector=self.name.value,
            )
        return POD_HOST.format(pod=pod)

    # --- the one way out ------------------------------------------------

    @staticmethod
    def is_read_only(method: str, path: str) -> bool:
        """True for a GET of an allowlisted path (or one item under an item path)."""
        if method.upper() != "GET":
            return False
        if path in READ_ONLY_PATHS:
            return True
        head, _, tail = path.rpartition("/")
        return head in READ_ONLY_ITEM_PATHS and bool(tail) and "/" not in tail

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
        follow_redirects: bool | None = None,
    ) -> httpx.Response:
        """Every request goes through here, and only reads (plus the login) get past."""
        is_login = method.upper() == "POST" and path == AUTHORIZE_PATH and not authenticated
        if not is_login and not self.is_read_only(method, path):
            raise ReadOnlyViolation(
                f"connectsecure {method.upper()} {path} is not a read-only request; this "
                "connector makes no request for it (hard rule 2)"
            )
        self.calls_made.append((method.upper(), path))
        self.ledger.record(self._ledger_key, client_slug=self.scope.client_slug)
        return await super().request(
            method,
            path,
            params=params,
            data=data,
            json_body=json_body,
            headers=headers,
            authenticated=authenticated,
            follow_redirects=follow_redirects,
        )

    @property
    def requests_today(self) -> int:
        """This process's count of requests to this tenant today (the ledger)."""
        return self.ledger.today(self._ledger_key)

    # --- auth: one token and user id per client -----------------------------

    def _is_authenticated(self) -> bool:
        self._session = self.sessions.get(self._token_key)
        return self._session is not None

    def _invalidate_auth(self) -> None:
        self.sessions.discard(self._token_key, self._session)
        self._session = None

    def _auth_headers(self) -> dict[str, str]:
        if self._session is None:
            return {}
        user_id, _, token = self._session.partition(" ")
        return {"Authorization": f"Bearer {token}", "X-USER-ID": user_id}

    def _client_auth_token(self) -> str:
        tenant = str(self.config.get("tenant") or "").strip()
        if not tenant:
            raise AuthError(
                "the ConnectSecure credential needs the tenant name (the portal login "
                "organisation)",
                connector=self.name.value,
            )
        raw = f"{tenant}+{self.secret('client_id')}:{self.secret('client_secret')}"
        # UTF-8, as the vendor insists: another encoding is a 502 from the gateway.
        return base64.b64encode(raw.encode("utf-8")).decode("ascii")

    async def _authenticate(self) -> None:
        response = await self.request(
            "POST",
            AUTHORIZE_PATH,
            headers={"Client-Auth-Token": self._client_auth_token(), "Accept": "application/json"},
            authenticated=False,
        )
        body = self._decode(response, AUTHORIZE_PATH)
        # Live: the token and user id are at the top level. An integrator's
        # wrapper reads them under `data`; accepted too.
        payload = body.get("data") if isinstance(body, dict) and "data" in body else body
        token = payload.get("access_token") if isinstance(payload, dict) else None
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        if not token or not user_id:
            raise AuthError(
                "ConnectSecure returned an authorize response without access_token and user_id",
                connector=self.name.value,
                status_code=response.status_code,
            )
        self._session = f"{user_id} {token}"
        self.sessions.put(
            self._token_key,
            self._session,
            dt.datetime.now(dt.UTC)
            + dt.timedelta(seconds=TOKEN_LIFETIME_SECONDS - TOKEN_EXPIRY_MARGIN_SECONDS),
        )
        logger.debug("connectsecure token acquired client=%s", self.scope.client_slug)

    @property
    def user_id(self) -> str | None:
        """The API key's user, from the authorize response. None before login."""
        if self._session is None:
            return None
        return self._session.partition(" ")[0]

    # --- one page sized to the total --------------------------------------

    async def _rows(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], bool, int]:
        """Read a listing as one page: `(rows, truncated, total)`.

        The first request asks for `page_size` rows and reads the vendor's
        `total`. If more exist and the total is within `max_rows`, one more
        request asks for exactly that many. Beyond `max_rows` the read is
        reported truncated. Never an offset walk (module docstring).
        """
        base = {k: v for k, v in dict(params or {}).items() if k not in ("skip", "limit")}
        body = await self.get_json(path, params={**base, "skip": 0, "limit": self.page_size})
        rows, total = self._data_list(body, path)
        if total is None or total <= len(rows):
            return rows, False, len(rows) if total is None else total
        if total > self.max_rows:
            logger.warning(
                "connectsecure %s holds %d rows for client=%s, above the %d-row cap; truncated",
                path,
                total,
                self.scope.client_slug,
                self.max_rows,
            )
            return rows, True, total
        body = await self.get_json(path, params={**base, "skip": 0, "limit": total})
        rows, again = self._data_list(body, path)
        return rows, (again or total) > len(rows), again or total

    def _data_list(self, body: Any, path: str) -> tuple[list[dict[str, Any]], int | None]:
        data = body.get("data") if isinstance(body, dict) else body
        if isinstance(data, dict):  # a single-object answer
            data = [data]
        if not isinstance(data, list):
            raise UpstreamError(
                f"ConnectSecure {path} returned no data list",
                connector=self.name.value,
                details={"path": path},
            )
        total = body.get("total") if isinstance(body, dict) else None
        return [row for row in data if isinstance(row, dict)], (
            int(total) if isinstance(total, int | float) else None
        )

    async def _filtered(self, path: str, **fields: Any) -> tuple[list[dict[str, Any]], bool]:
        """A filtered listing, with every filter applied again on our side.

        The report views return every row for a condition they do not
        understand, and the company and asset resources return nothing (live),
        so the response is checked rather than trusted: a row that does not
        satisfy the filter we sent is proof the vendor ignored it, and the
        whole read is refused rather than quietly narrowed. (An equal filtered
        and unfiltered total is only a hint — a customer that owns every asset
        of a small tenant produces one legitimately — so the row check, which
        is exact, is what raises.)
        """
        condition = build_condition(path, **fields)
        rows, truncated, total = await self._rows(path, {"condition": condition})
        ignored = [row for row in rows if not _row_matches(row, fields)]
        if ignored:
            raise UpstreamError(
                f"ConnectSecure ignored the filter on {path}: {len(ignored)} of {len(rows)} rows "
                f"do not satisfy {condition!r} (tenant total {total}). Nothing from it is "
                "trusted.",
                connector=self.name.value,
                details={"path": path, "condition": condition, "unmatched": len(ignored)},
            )
        return rows, truncated

    # --- scoping guard --------------------------------------------------

    def _allowed_by_company(self) -> dict[str, SiteRef]:
        return {site.external_id: site for site in self.scope.allowed}

    def _outside(self, company_ids: Sequence[str | None]) -> set[str]:
        """The company ids in a response that are neither a site's nor an inactive site's."""
        allowed = self._allowed_by_company()
        excluded = {site.external_id for site in self.scope.excluded}
        return {
            str(c) for c in company_ids if c and str(c) not in allowed and str(c) not in excluded
        }

    async def _load_known_excluded(self) -> dict[str, str]:
        """The tenant's companies `sync_sites` leaves unmapped on purpose, by id:
        `deleted`, `assessment` (a prospect scanned once, not a customer), and
        `unsynced` for a live company nobody has synced yet. Read once per client
        for the inventory's TTL; a tenant-wide listing that returns rows from an
        unmapped company is classified against it rather than shouted about."""
        if self._known_excluded is not None:
            return self._known_excluded
        key = (self.scope.client_id, self.credential_version)
        cached = _KNOWN_EXCLUDED.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            self._known_excluded = cached[1]
            return cached[1]
        raw, _, _ = await self._rows(COMPANIES_PATH)
        known: dict[str, str] = {}
        for row in raw:
            if not row.get("id"):
                continue
            company = CsCompany.model_validate(row)
            if company.is_deleted:
                known[company.id] = "deleted"
            elif company.is_assessment:
                known[company.id] = "assessment"
            else:
                known[company.id] = "unsynced"
        if self.credential_version is not None and self.inventory_ttl_seconds > 0:
            _KNOWN_EXCLUDED[key] = (now + self.inventory_ttl_seconds, known)
        self._known_excluded = known
        return known

    def _scoped(
        self,
        items: list[Any],
        company_ids: list[str | None],
        *,
        path: str,
        targets: Sequence[SiteRef] | None = None,
    ) -> list[tuple[SiteRef, Any]]:
        """Keep the records this call asked for, each tagged with its site.

        A sibling site's row means the vendor filter did not bite (warning);
        an inactive site's row is expected on a tenant listing (quiet); a
        company we hold no site for is another franchisee's or one nobody
        synced (error, never merged in); a row with no company id is dropped.
        """
        allowed = self._allowed_by_company()
        excluded = {site.external_id for site in self.scope.excluded}
        wanted = self.scope.targets if targets is None else targets
        targeted = {site.external_id for site in wanted}
        kept: list[tuple[SiteRef, Any]] = []
        outside = sibling = inactive = unknown = 0
        outside_ids: Counter[str] = Counter()
        for item, company_id in zip(items, company_ids, strict=True):
            if not company_id:
                unknown += 1
                continue
            site = allowed.get(str(company_id))
            if site is None:
                if str(company_id) in excluded:
                    inactive += 1
                else:
                    outside += 1
                    outside_ids[str(company_id)] += 1
                continue
            if site.external_id not in targeted:
                sibling += 1
                continue
            kept.append((site, item))
        if outside:
            self._report_outside(path, outside_ids)
        if sibling or unknown:
            logger.warning(
                "connectsecure %s response was not narrowed to %s for client=%s: dropped %d "
                "from the client's other sites and %d with no company id",
                path,
                self.scope.describe_targets(),
                self.scope.client_slug,
                sibling,
                unknown,
            )
        if inactive:
            logger.debug(
                "connectsecure %s: dropped %d record(s) from inactive sites for client=%s",
                path,
                inactive,
                self.scope.client_slug,
            )
        return kept

    # --- fan-out --------------------------------------------------------

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        return self._semaphore

    def _report_outside(self, path: str, outside_ids: Counter[str]) -> None:
        """Rows from companies no site holds: the ones sync leaves unmapped on
        purpose (deleted, assessment) at INFO with their ids; a live company
        nobody synced, or one this tenant does not list at all, at ERROR."""
        known = self._known_excluded or {}
        deliberate: dict[str, list[tuple[str, int]]] = {}
        loud: list[tuple[str, str, int]] = []
        for company_id, count in sorted(outside_ids.items()):
            reason = known.get(company_id)
            if reason in ("deleted", "assessment"):
                deliberate.setdefault(reason, []).append((company_id, count))
            else:
                loud.append((company_id, reason or "not in the tenant's company list", count))
        if deliberate:
            logger.info(
                "connectsecure %s returned rows from companies sync leaves unmapped on purpose "
                "for client=%s; dropped: %s",
                path,
                self.scope.client_slug,
                "; ".join(
                    f"{reason} {sum(n for _, n in ids)} row(s) from company "
                    + ", ".join(f"{cid} ({n})" for cid, n in ids)
                    for reason, ids in deliberate.items()
                ),
            )
        if loud:
            logger.error(
                "connectsecure %s returned %d record(s) from companies outside the allowlist "
                "of client=%s; dropped. Another franchisee's company, or a customer not yet "
                "synced (run sync_sites): %s",
                path,
                sum(n for _, _, n in loud),
                self.scope.client_slug,
                ", ".join(f"company {cid} ({why}, {n})" for cid, why, n in loud),
            )

    async def _read_scoped[T](
        self,
        path: str,
        build: Callable[[dict[str, Any]], T | None],
        company_of: Callable[[T], str | None],
        *,
        sites: Sequence[SiteRef] | None = None,
        per_site: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[list[tuple[SiteRef, T]], bool]:
        """One listing, every row checked against the allowlist.

        Tenant-wide (every active site targeted, `per_site` false): one read,
        classified row by row. Otherwise one filtered read per targeted site,
        under the semaphore, the site filter re-applied by `_scoped`; a row
        the vendor did not stamp with a company is this company's by
        construction. `extra` are further `build_condition` fields, applied
        again on our side by the caller.
        """
        targets = list(self.scope.targets if sites is None else sites)
        if self.scope.covers_every_site and sites is None and not per_site and not extra:
            raw, truncated, _ = await self._rows(path)
            built = [b for b in (build(row) for row in raw) if b is not None]
            ids = [company_of(b) for b in built]
            if self._outside(ids):
                # Rows from companies no site holds: learn which the tenant lists
                # as deleted or assessment before deciding how loudly to drop them.
                await self._load_known_excluded()
            return (
                self._scoped(built, ids, path=path, targets=self.scope.allowed),
                truncated,
            )

        async def one_site(site: SiteRef) -> tuple[list[tuple[SiteRef, T]], bool]:
            raw, cut = await self._filtered(
                path, company_id=int(site.external_id), **dict(extra or {})
            )
            built = [b for b in (build(row) for row in raw) if b is not None]
            ids: list[str | None] = [company_of(b) or site.external_id for b in built]
            return self._scoped(built, ids, path=path, targets=[site]), cut

        rows: list[tuple[SiteRef, T]] = []
        truncated = False
        answered, _ = await self._fan_out_refs(targets, one_site)
        for _site, (site_rows, cut) in answered:
            rows.extend(site_rows)
            truncated = truncated or cut
        return rows, truncated

    # --- reads ----------------------------------------------------------

    async def list_organisations(self) -> list[VendorOrganisation]:
        """Every managed company in the tenant, for `sync_sites`.

        Deleted companies are left out, and so are assessment companies: an
        assessment is a prospect being scanned once, not a customer under
        management, as Autotask's prospects are left out of its sync.
        """
        raw, truncated, total = await self._rows(COMPANIES_PATH)
        self._company_count = total
        if truncated:
            logger.warning("connectsecure company listing hit the row cap; some are missing")
        organisations: list[VendorOrganisation] = []
        for row in raw:
            if not row.get("id"):
                continue
            company = CsCompany.model_validate(row)
            if company.is_deleted or company.is_assessment:
                continue
            organisations.append(
                VendorOrganisation(
                    external_id=company.id, name=company.name or f"company {company.id}"
                )
            )
        return organisations

    async def current_user(self) -> CsUser | None:
        """The API key's own user, for the pre-flight's role check."""
        await self._ensure_authenticated()
        raw, _, _ = await self._rows(USERS_PATH)
        users = [CsUser.model_validate(row) for row in raw]
        mine = [u for u in users if u.id and u.id == self.user_id]
        return mine[0] if mine else None

    async def list_agents(
        self, *, sites: Sequence[SiteRef] | None = None
    ) -> tuple[list[tuple[SiteRef, CsAgent]], bool]:
        """Installed agents, per targeted company, including deprecated ones."""
        return await self._read_scoped(
            AGENTS_PATH,
            lambda row: CsAgent.model_validate(row) if row.get("id") else None,
            lambda agent: agent.company_id,
            sites=sites,
        )

    async def _inventory(self, *, use_cache: bool = True) -> InventoryEntry[CsAsset, str]:
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

    async def hostnames_by_organisation(self) -> dict[str, set[str]]:
        """Every company's asset hostnames from one tenant read of the asset view. Sync only."""
        raw, _, _ = await self._rows(ASSETS_PATH)
        found: dict[str, set[str]] = {}
        for row in raw:
            asset = CsAsset.model_validate(row)
            if asset.company_id and asset.host_name:
                found.setdefault(asset.company_id, set()).add(normalise_hostname(asset.host_name))
        return found

    async def _list_inventory(
        self, targets: list[uuid.UUID], *, ttl_seconds: float
    ) -> InventoryEntry[CsAsset, str]:
        """Assets with their agent joined by `agent_id`, which is where the scan time lives.

        A host can have several agent rows (a redeployed agent leaves a
        deprecated one behind: 165 agents for 94 assets on the pilot site), so
        the join is by the id the asset names, never by hostname.
        """
        source: Literal["tenant", "sites"] = "tenant" if self.scope.covers_every_site else "sites"
        rows, truncated = await self._read_scoped(
            ASSETS_PATH,
            lambda row: CsAsset.model_validate(row) if row.get("id") else None,
            lambda asset: asset.company_id,
        )
        agents, agents_cut = await self.list_agents()
        by_id = {agent.id: agent for _, agent in agents}
        for _, asset in rows:
            agent = by_id.get(asset.agent_id) if asset.agent_id else None
            if agent is not None:
                asset.agent = agent
                asset.last_scanned = agent.last_scanned_time
        logger.debug(
            "connectsecure assets listed client=%s source=%s assets=%d agents=%d truncated=%s",
            self.scope.client_slug,
            source,
            len(rows),
            len(agents),
            truncated or agents_cut,
        )
        return self.inventory.put(
            self.scope.client_id,
            version=self.credential_version,
            targets=targets,
            rows=rows,
            device_orgs={asset.id: site.external_id for site, asset in rows},
            truncated=truncated or agents_cut,
            ttl_seconds=ttl_seconds,
            source=source,
        )

    async def list_assets(
        self, *, use_cache: bool = True
    ) -> tuple[list[tuple[SiteRef, CsAsset]], bool]:
        """Every asset this call targets, tagged with its site, agent joined, from the inventory."""
        entry = await self._inventory(use_cache=use_cache)
        targeted = {site.site_id for site in self.scope.targets}
        return [row for row in entry.rows if row[0].site_id in targeted], entry.truncated

    async def list_remediations(
        self, *, sites: Sequence[SiteRef] | None = None, include_unconfirmed: bool = False
    ) -> tuple[list[tuple[SiteRef, CsRemediation]], bool]:
        """Every (asset, fix) row in scope, read per site. Confirmed findings only by default."""
        rows, truncated = await self._read_scoped(
            REMEDIATIONS_PATH,
            lambda row: CsRemediation.model_validate(row) if row.get("solution_id") else None,
            lambda r: r.company_id,
            sites=sites,
            per_site=True,
            extra=None if include_unconfirmed else CONFIRMED_ONLY,
        )
        return [(site, r) for site, r in rows if r.fix or r.kind != "patch"], truncated

    async def list_vulnerabilities(
        self,
        *,
        sites: Sequence[SiteRef] | None = None,
        with_known_exploited: bool = False,
        with_fixes: bool = True,
        include_unconfirmed: bool = False,
    ) -> tuple[list[tuple[SiteRef, CsVulnerability]], bool]:
        """Every (asset, vulnerability) pair in scope, read per site.

        **Confirmed findings only by default** (`CONFIRMED_ONLY`): the vendor
        also reports findings it inferred and has not verified, and mixing the
        two makes a count that no other figure agrees with.
        `include_unconfirmed` returns both populations.

        `with_known_exploited` adds one filtered read per site (problem group
        34) and flags the pairs it returns; a count of known-exploited findings
        is cheaper from the assets' `cisa_vul`, which is the confirmed count.
        `with_fixes` joins each pair to the remediation row of the same asset
        and normalised product, read in the same population; a pair with no
        such row keeps `fix` None, which the tools print as "fix not reported".
        Never by hostname.
        """
        population = None if include_unconfirmed else CONFIRMED_ONLY
        rows, truncated = await self._read_scoped(
            VULNERABILITIES_PATH,
            lambda row: CsVulnerability.model_validate(row) if row.get("problem_name") else None,
            lambda v: v.company_id,
            sites=sites,
            per_site=True,
            extra=population,
        )
        if with_known_exploited and rows:
            kev, kev_cut = await self._read_scoped(
                VULNERABILITIES_PATH,
                lambda row: (
                    CsVulnerability.model_validate(row) if row.get("problem_name") else None
                ),
                lambda v: v.company_id,
                sites=sites,
                per_site=True,
                extra={"problem_group_id": int(PROBLEM_GROUP_CISA_KEV), **(population or {})},
            )
            truncated = truncated or kev_cut
            flagged = {(v.asset_id, v.problem) for _, v in kev}
            for _, v in rows:
                v.known_exploited = (v.asset_id, v.problem) in flagged
        if with_fixes and rows:
            fixes, fixes_cut = await self.list_remediations(
                sites=sites, include_unconfirmed=include_unconfirmed
            )
            truncated = truncated or fixes_cut
            attach_fixes([v for _, v in rows], [r for _, r in fixes])
        return rows, truncated

    async def company_stats(
        self, *, sites: Sequence[SiteRef] | None = None
    ) -> list[tuple[SiteRef, CsCompanyStats]]:
        """The vendor's own per-company totals: assets, findings by severity, the risk score."""
        rows, _ = await self._read_scoped(
            COMPANY_STATS_PATH,
            lambda row: CsCompanyStats.model_validate(row),
            lambda s: s.company_id,
            sites=sites,
        )
        return rows


def attach_fixes(vulnerabilities: list[CsVulnerability], fixes: list[CsRemediation]) -> int:
    """Join each finding to the fix on the same asset for the same normalised product.

    Returns how many findings got one. A finding with no fix row on its asset
    for its product keeps `fix` None: "fix not reported", never a guess.
    """
    by_key: dict[tuple[str | None, str], CsRemediation] = {}
    for remediation in fixes:
        key = (remediation.asset_id, remediation.product_key)
        if key[1] and key not in by_key:
            by_key[key] = remediation
    matched = 0
    for v in vulnerabilities:
        found = by_key.get((v.asset_id, v.product_key)) if v.product_key else None
        if found is None:
            continue
        v.fix = found.instruction
        v.fix_kind = found.kind
        matched += 1
    return matched


#: The process-wide asset inventory cache and session store for this connector.
INVENTORY: InventoryCache[CsAsset, str] = InventoryCache()
SESSIONS = TokenStore()

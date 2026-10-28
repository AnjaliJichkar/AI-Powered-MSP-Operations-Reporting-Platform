"""Shared connector machinery: auth, retry, pagination, error normalisation.

A connector is constructed by `app.connectors.factory` with the credentials and
the vendor-side tenant id already resolved from a `client_id`. It never reads
the environment, and it never decides which client it is talking about.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, ClassVar, Self

import httpx

from app.connectors.errors import (
    ArgumentRejected,
    AuthError,
    ConnectorError,
    ConnectorNotConfigured,
    NotFound,
    RateLimited,
    UpstreamError,
)
from app.connectors.scope import ClientScope, SiteRef, VendorOrganisation
from app.models.enums import ConnectorName
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

#: Header prefix vendors use for quota information, when they send any.
RATE_LIMIT_HEADER_PREFIX = "x-ratelimit"
#: How much of a refusal's body goes in the log. Enough to read a vendor's
#: own explanation, short enough that nothing large lands in a log file.
REFUSAL_BODY_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class ConnectorLimits:
    """Timeouts, retry budget and pagination caps.

    Hard rule 6: these are configuration. Nothing here is a literal in a
    connector, so a misbehaving vendor is a settings change, not a deploy.
    """

    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 20.0
    page_size: int = 100
    max_pages: int = 20
    jitter: bool = True
    #: The most conditions one query's filter may carry, where a vendor counts
    #: them and refuses the excess. Autotask does both (api-notes); nothing
    #: else reads this yet.
    max_filter_conditions: int = 450

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, connector: str | None = None
    ) -> ConnectorLimits:
        """The limits from settings; `connector` picks up that vendor's own timeout."""
        settings = settings or get_settings()
        return cls(
            timeout_seconds=settings.connector_timeout_for(connector)
            if connector
            else settings.connector_timeout_seconds,
            connect_timeout_seconds=settings.connector_connect_timeout_seconds,
            max_retries=settings.connector_max_retries,
            backoff_base_seconds=settings.connector_backoff_base_seconds,
            backoff_max_seconds=settings.connector_backoff_max_seconds,
            page_size=settings.connector_page_size,
            max_pages=settings.connector_max_pages,
            max_filter_conditions=settings.autotask_max_filter_conditions,
        )


def parse_retry_after(response: httpx.Response) -> float | None:
    """Read a ``Retry-After`` header, in either of its two legal forms."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    import datetime as dt

    delta = (retry_at - dt.datetime.now(dt.UTC)).total_seconds()
    return max(delta, 0.0)


class BaseConnector:
    """Async HTTP client for one vendor, scoped to one client and its sites.

    Subclasses provide `default_base_url`, the auth hooks, and their own typed
    methods. They do not implement retry, backoff or error mapping.

    `self.scope` carries both the client's full allowlist of vendor ids and the
    subset this call targets; see `app/connectors/scope.py`.
    """

    name: ClassVar[ConnectorName]
    default_base_url: ClassVar[str] = ""

    def __init__(
        self,
        *,
        scope: ClientScope,
        secrets: Mapping[str, Any],
        config: Mapping[str, Any] | None = None,
        limits: ConnectorLimits | None = None,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        credential_version: str | None = None,
    ) -> None:
        self.scope = scope
        self.config: Mapping[str, Any] = config or {}
        self.limits = limits or ConnectorLimits()
        self.credential_version = credential_version
        """Changes whenever the stored credential does; cache keys include it."""
        self.last_rate_limit: dict[str, str] = {}
        """The most recent `X-RateLimit-*` headers seen, if the vendor sends any."""
        self._secrets = dict(secrets)
        self._client = http_client
        self._owns_client = http_client is None
        self._sleep = sleep or asyncio.sleep
        self._auth_lock = asyncio.Lock()

    # --- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> Self:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.limits.timeout_seconds,
                    connect=self.limits.connect_timeout_seconds,
                ),
                follow_redirects=True,
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def base_url(self) -> str:
        """Per-client override wins; otherwise the connector's default host."""
        configured = self.config.get("base_url") or self.default_base_url
        return str(configured).rstrip("/")

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                f"{type(self).__name__} used outside 'async with'; the HTTP client is not open"
            )
        return self._client

    def secret(self, key: str) -> str:
        """Read one decrypted credential field, with a clear error if missing."""
        value = self._secrets.get(key)
        if value is None or value == "":
            raise AuthError(
                f"credential field {key!r} is missing for this client",
                connector=self.name.value,
            )
        return str(value)

    def __repr__(self) -> str:
        # Never renders credentials.
        return (
            f"<{type(self).__name__} client={self.scope.client_slug!r} "
            f"targets={self.scope.describe_targets()}>"
        )

    async def hostnames_by_organisation(self) -> dict[str, set[str]]:
        """Every organisation's device hostnames, by vendor id, from one tenant read.

        For `sync_sites` only, which runs before a new organisation has a site
        and needs its hostnames to see whether they overlap an existing site's.
        Names stay in memory and are never printed. A connector that cannot
        list its tenant returns nothing, and the sync links by name alone.
        """
        return {}

    async def list_organisations(self) -> list[VendorOrganisation]:
        """Every organisation this tenant's credentials can see.

        `sync_sites` turns these into `Site` rows. A connector that cannot
        enumerate its tenants has no automatic site discovery and must say so
        rather than silently returning nothing.
        """
        raise ConnectorNotConfigured(
            f"{self.name.value} cannot list organisations, so its sites cannot be synced",
            connector=self.name.value,
        )

    # --- fan-out over a site's organisations -----------------------------

    async def _fan_out_refs[T](
        self, sites: Sequence[SiteRef], call: Callable[[SiteRef], Awaitable[T]]
    ) -> tuple[list[tuple[SiteRef, T]], list[tuple[SiteRef, ConnectorError]]]:
        """Run `call` for every ref concurrently — at most `concurrency` at a time
        when the connector has a semaphore — and return every answer in `sites`
        order, with the refs a site could do without.

        A site may hold several vendor organisations (migration 0013). When the
        vendor no longer knows one of them — `NotFound`, or an `ArgumentRejected`
        on the id itself — and another organisation of the same site answered,
        the read goes on without it and returns it in the second list, so the
        caller can say so. When a site has no other answer, or the failure is
        anything else (auth, a rate limit, an upstream error), it is raised as
        it always was: a partial read is never passed off as a whole one.
        """
        semaphore = getattr(self, "semaphore", None)

        async def bounded(site: SiteRef) -> T | ConnectorError:
            try:
                if semaphore is None:
                    return await call(site)
                async with semaphore:
                    return await call(site)
            except (NotFound, ArgumentRejected) as exc:
                return exc

        results = list(await asyncio.gather(*(bounded(site) for site in sites)))
        answered_sites = {
            site.site_id
            for site, result in zip(sites, results, strict=True)
            if not isinstance(result, ConnectorError)
        }
        answers: list[tuple[SiteRef, T]] = []
        spared: list[tuple[SiteRef, ConnectorError]] = []
        for site, result in zip(sites, results, strict=True):
            if isinstance(result, ConnectorError):
                if site.site_id not in answered_sites:
                    raise result
                logger.warning(
                    "%s: site %s holds organisation %s the vendor no longer answers for; "
                    "read from its other organisation(s): %s",
                    self.name.value,
                    site.slug,
                    site.external_id,
                    type(result).__name__,
                )
                spared.append((site, result))
            else:
                answers.append((site, result))
        return answers, spared

    # --- auth hooks (overridden by connectors that need a token) ---------

    def _is_authenticated(self) -> bool:
        """True when a usable token is held. Static-credential connectors: always."""
        return True

    async def _authenticate(self) -> None:
        """Obtain a token. Default: nothing to do."""
        return None

    def _invalidate_auth(self) -> None:
        """Drop a cached token after the vendor rejects it."""
        return None

    def _auth_headers(self) -> dict[str, str]:
        """Headers added to every authenticated request."""
        return {}

    async def _ensure_authenticated(self) -> None:
        if self._is_authenticated():
            return
        async with self._auth_lock:
            # Another task may have authenticated while we waited.
            if self._is_authenticated():
                return
            await self._authenticate()

    # --- requests -------------------------------------------------------

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
        """Send one request, retrying 429 and 5xx with exponential backoff.

        A 401 triggers exactly one re-authentication before it becomes an
        `AuthError`, so an expired token is invisible to callers but bad
        credentials fail fast.

        `follow_redirects=False` hands the redirect back instead of following
        it. One endpoint family needs that: Microsoft Graph's usage reports
        answer `302` with a pre-authenticated download URL, which must be
        fetched *without* our `Authorization` header, so the caller follows it
        itself (`app/connectors/microsoft365/client.py`).
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        attempt = 0
        reauthenticated = False

        while True:
            request_headers = dict(headers or {})
            if authenticated:
                await self._ensure_authenticated()
                request_headers.update(self._auth_headers())

            try:
                extra: dict[str, Any] = (
                    {} if follow_redirects is None else {"follow_redirects": follow_redirects}
                )
                response = await self.http.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    data=dict(data) if data else None,
                    json=json_body,
                    headers=request_headers,
                    **extra,
                )
            except httpx.TimeoutException as exc:
                if attempt >= self.limits.max_retries:
                    raise UpstreamError(
                        f"{self.name.value} did not respond within "
                        f"{self.limits.timeout_seconds}s after {attempt + 1} attempts",
                        connector=self.name.value,
                        details={"path": path, "attempts": attempt + 1},
                    ) from exc
                await self._backoff(attempt)
                attempt += 1
                continue
            except httpx.HTTPError as exc:
                if attempt >= self.limits.max_retries:
                    raise UpstreamError(
                        f"{self.name.value} could not be reached: {type(exc).__name__}",
                        connector=self.name.value,
                        details={"path": path, "attempts": attempt + 1},
                    ) from exc
                await self._backoff(attempt)
                attempt += 1
                continue

            status = response.status_code
            self._note_rate_limit(response, path)

            if status == 401 and authenticated and not reauthenticated:
                reauthenticated = True
                self._invalidate_auth()
                continue

            if status == 429:
                retry_after = parse_retry_after(response)
                self.log_refusal(response, path, attempt)
                if attempt >= self.limits.max_retries:
                    raise RateLimited(
                        f"{self.name.value} rate limit not cleared after {attempt + 1} attempts",
                        connector=self.name.value,
                        retry_after=retry_after,
                        details={"path": path, "attempts": attempt + 1},
                    )
                await self._backoff(attempt, retry_after=retry_after)
                attempt += 1
                continue

            if 500 <= status < 600:
                self.log_refusal(response, path, attempt)
                if attempt >= self.limits.max_retries:
                    raise UpstreamError(
                        f"{self.name.value} returned {status} on {attempt + 1} attempts",
                        connector=self.name.value,
                        status_code=status,
                        details={"path": path, "attempts": attempt + 1},
                    )
                await self._backoff(attempt)
                attempt += 1
                continue

            if status >= 400:
                raise self._error_for(response, path)

            return response

    def log_refusal(self, response: httpx.Response, path: str, attempt: int) -> None:
        """Everything a refusal said, so a vendor's threshold can be identified.

        Autotask refused every query for about an hour on 2026-10-01 with a
        plain 500 and no explanation, and the api-notes question of what an
        exceeded request threshold actually returns is still open — it may not
        be a 429, in which case our retry path never backs off for it. The only
        way to answer that is to have the next one on the record in full.

        **Response headers only, never the request's**: ours carry the
        credential. `set-cookie` is dropped for the same reason. The body is
        truncated, and logged only for a refusal — a 429 or a 5xx — where it is
        a vendor's apology rather than a customer's data.
        """
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"set-cookie", "authorization"}
        }
        try:
            body = response.text[:REFUSAL_BODY_LIMIT]
        except Exception:  # a body that will not decode is still a fact
            body = "<undecodable>"
        logger.warning(
            "%s refused a request: status=%s path=%s attempt=%d client=%s headers=%r body=%r",
            self.name.value,
            response.status_code,
            path,
            attempt + 1,
            self.scope.client_slug,
            headers,
            body,
        )

    def _note_rate_limit(self, response: httpx.Response, path: str) -> None:
        """Record and log any quota headers, so real limits can be learned live.

        Nothing depends on these: retry reads `Retry-After` on a 429. But a
        vendor that does send them tells us how close we run to the limit long
        before we hit it, which is worth a log line.
        """
        headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower().startswith(RATE_LIMIT_HEADER_PREFIX)
        }
        if not headers:
            return
        self.last_rate_limit = headers
        remaining = headers.get(f"{RATE_LIMIT_HEADER_PREFIX}-remaining")
        exhausted = remaining is not None and remaining.strip() in {"0", "0.0"}
        logger.log(
            logging.WARNING if exhausted else logging.DEBUG,
            "%s rate limit client=%s path=%s %s",
            self.name.value,
            self.scope.client_slug,
            path,
            " ".join(f"{key}={value}" for key, value in sorted(headers.items())),
        )

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        """GET and decode JSON, normalising a non-JSON body into an error."""
        response = await self.request("GET", path, params=params, authenticated=authenticated)
        return self._decode(response, path)

    def _decode(self, response: httpx.Response, path: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(
                f"{self.name.value} returned a body that is not JSON",
                connector=self.name.value,
                status_code=response.status_code,
                details={"path": path, "content_type": response.headers.get("content-type")},
            ) from exc

    def _error_for(self, response: httpx.Response, path: str) -> ConnectorError:
        """Map a failing status onto the normalised hierarchy."""
        status = response.status_code
        detail = self._error_detail(response)
        common: dict[str, Any] = {"path": path}
        if detail:
            common["detail"] = detail

        if status in (401, 403):
            return AuthError(
                f"{self.name.value} rejected our credentials",
                connector=self.name.value,
                status_code=status,
                details=common,
            )
        if status == 404:
            return NotFound(
                f"{self.name.value} has no object at {path}",
                connector=self.name.value,
                status_code=status,
                details=common,
            )
        return UpstreamError(
            f"{self.name.value} returned {status}",
            connector=self.name.value,
            status_code=status,
            details=common,
        )

    def _error_detail(self, response: httpx.Response) -> str | None:
        """A short, non-sensitive description of a vendor error body."""
        try:
            body = response.json()
        except ValueError:
            text = response.text.strip()
            return text[:200] or None
        if isinstance(body, dict):
            for key in ("error_description", "resultCode", "message", "error", "detail"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value[:200]
        return None

    async def _backoff(self, attempt: int, *, retry_after: float | None = None) -> None:
        await self._sleep(self.backoff_delay(attempt, retry_after=retry_after))

    def backoff_delay(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Exponential backoff, capped, honouring ``Retry-After`` when given."""
        if retry_after is not None:
            return min(retry_after, self.limits.backoff_max_seconds)
        delay = min(
            self.limits.backoff_base_seconds * (2.0**attempt),
            self.limits.backoff_max_seconds,
        )
        if self.limits.jitter:
            # Jitter spreads retries across concurrent runs. Not cryptographic.
            delay = random.uniform(delay / 2, delay)  # noqa: S311
        return delay

    # --- pagination -----------------------------------------------------

    async def paginate(
        self,
        path: str,
        *,
        next_cursor: Callable[[Sequence[Any]], Any | None],
        params: Mapping[str, Any] | None = None,
        cursor_param: str = "after",
        page_size_param: str = "pageSize",
        page_size: int | None = None,
        max_pages: int | None = None,
        stop_on_short_page: bool = True,
    ) -> tuple[list[Any], bool]:
        """Follow a cursor until the vendor runs out of pages.

        Returns ``(items, truncated)``. ``truncated`` is True when the page cap
        stopped us early, so callers can say so instead of implying the list is
        complete (hard rule 7).

        ``stop_on_short_page=False`` keeps going until a page comes back empty.
        It costs one more request and is for page sizes the vendor might cap
        silently: a capped page looks short, and stopping on it would report a
        one-page slice as the whole.
        """
        size = page_size or self.limits.page_size
        cap = max_pages or self.limits.max_pages
        items: list[Any] = []
        cursor: Any | None = None
        seen: set[Any] = set()

        for _ in range(cap):
            page_params = dict(params or {})
            page_params[page_size_param] = size
            if cursor is not None:
                page_params[cursor_param] = cursor

            batch = await self.get_json(path, params=page_params)
            if not isinstance(batch, list):
                raise UpstreamError(
                    f"{self.name.value} returned {type(batch).__name__} where a list was expected",
                    connector=self.name.value,
                    details={"path": path},
                )
            if not batch:
                return items, False

            cursor = next_cursor(batch)
            if cursor is not None and cursor in seen:
                # The vendor ignored our cursor and resent a page. Do not count
                # it twice, and stop rather than loop.
                return items, False
            items.extend(batch)

            if cursor is None or (stop_on_short_page and len(batch) < size):
                return items, False
            seen.add(cursor)

        return items, True

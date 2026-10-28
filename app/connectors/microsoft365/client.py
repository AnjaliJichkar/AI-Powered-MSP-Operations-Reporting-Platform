"""Microsoft 365 connector: Microsoft Graph v1.0, application permissions, read only.

**Read-only is enforced here as well as by the permissions granted.** Every
application permission we ask a tenant to consent to ends in `.Read.All`, so a
token this connector holds cannot write — but a permission list is something a
person maintains in a portal, and hard rule 2 is ours to keep. So this client
refuses any method other than `GET` on the Graph host before a request is
built (`ReadOnlyViolation`), and the one place a read-only `POST` could ever
be allowed is `READ_ONLY_POSTS`, which is empty: Graph's read-only POSTs
(`/$batch`, `/directoryObjects/getByIds`, `/users/{id}/getMemberGroups`) are
deliberately not used, so the rule is the simplest one there is. The token
request itself is a `POST` to the login host, which is not Graph and is
matched by host rather than exempted by path.

**Two levels of scope, as everywhere else.** The credential is the client's:
our multi-tenant application's id and its certificate or secret. The *tenant*
is the `ToolMapping` — a site-level mapping whose `external_id` is that end
customer's Entra tenant id (a GUID), or the tenant-level mapping's
`extra["tenant_id"]` for the franchisee's own tenant. A token belongs to one
tenant, so this client holds one per tenant it reads
(`app/connectors/microsoft365/tokens.py` keys them by client, credential
version, tenant and scope) and reads one tenant at a time: `for_tenant` says
which, and every read asserts it is inside one.

**What a tenant cannot answer is said, not guessed.** `signInActivity` needs
an Entra ID P1 or P2 licence; a tenant without one answers the same query with
a specific error, which `NotLicensed` carries up so a page can print *not
available for this tenant* rather than a zero or a stack trace (hard rule 7).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import httpx

from app.connectors.base import BaseConnector
from app.connectors.errors import (
    AuthError,
    ConnectorError,
    ConnectorNotConfigured,
    NotFound,
    UpstreamError,
)
from app.connectors.microsoft365.models import (
    GraphGroup,
    GraphUser,
    MailboxUsage,
    RegistrationDetail,
    SubscribedSku,
)
from app.connectors.microsoft365.tokens import TOKENS, TokenStore
from app.connectors.scope import SiteRef, VendorOrganisation
from app.models.enums import ConnectorName

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
LOGIN_BASE_URL = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

#: The read-only `POST` endpoints this connector may use. Empty on purpose:
#: Graph has a few (`/$batch`, `/directoryObjects/getByIds`,
#: `/users/{id}/getMemberGroups`) and none of them is needed for what we read,
#: so the guard is "GET, and nothing else". Adding one here is a decision, and
#: `tests/connectors/test_microsoft365_readonly.py` fails on an entry that is
#: not one of those three.
READ_ONLY_POSTS: frozenset[str] = frozenset()

#: Graph's own words when a tenant has no Entra ID P1/P2 and asks for a
#: property or report that needs one. Matched case-insensitively against the
#: error code and message; a tenant that says any of these is reported as not
#: licensed for that item rather than as a failure.
PREMIUM_PHRASES = (
    "neither tenant is b2c or tenant doesn't have premium license",
    "tenant doesn't have premium license",
    "does not have a premium license",
    "requires an azure ad premium",
    "requires a microsoft entra id p1",
    "aadpremiumrequired",
)

#: What `$select` asks for on a user. Everything here is a documented v1.0
#: property; `signInActivity` is the one that needs a premium licence and is
#: asked for separately so the rest of the read survives without it.
USER_FIELDS = (
    "id",
    "displayName",
    "userPrincipalName",
    "mail",
    "accountEnabled",
    "createdDateTime",
    "userType",
    "onPremisesSyncEnabled",
    "department",
    "jobTitle",
    "officeLocation",
    "assignedLicenses",
)

GROUP_FIELDS = (
    "id",
    "displayName",
    "mail",
    "mailEnabled",
    "securityEnabled",
    "groupTypes",
    "resourceProvisioningOptions",
    "visibility",
    "createdDateTime",
)

#: Graph's own page cap for `/users`. With `signInActivity` selected the cap
#: is 500 rather than 999 — "Requests with $top set higher than 500 return
#: pages with up to 500 users" — and that endpoint is separately throttled at
#: about ten requests a minute, so a large tenant's sign-in read is the slow
#: part of a profile and is asked for once (learn.microsoft.com, user-list).
USERS_PAGE = 999
USERS_PAGE_WITH_SIGN_IN = 500

#: How long before a token's stated expiry we stop using it, so a read that
#: starts just under the wire does not finish just over it.
TOKEN_SKEW = dt.timedelta(minutes=5)


class ReadOnlyViolation(ConnectorError):
    """A caller asked this connector to send something other than a read.
    Raised before any HTTP request exists (hard rule 2)."""


class NotLicensed(ConnectorError):
    """The tenant cannot answer this because of its own licensing, not because
    anything failed. Reported as *not available for this tenant*: never an
    error on a page, never a zero."""


class NotAvailable(ConnectorError):
    """This tenant cannot answer this item, and that is a fact about the
    tenant rather than a failure of the read: it has no premium licence, the
    permission was not consented to, or its own report settings conceal the
    answer. Every caller reports it in words and carries on."""


class NoTenant(ConnectorNotConfigured):
    """No Entra tenant id is mapped for this client or site, so there is
    nothing to read. Different from a missing credential: the application may
    be registered and consented and still have no tenant mapped here."""


class Microsoft365Client(BaseConnector):
    """Graph v1.0 for one client, one tenant at a time."""

    name: ClassVar[ConnectorName] = ConnectorName.MICROSOFT365
    default_base_url: ClassVar[str] = GRAPH_BASE_URL

    def __init__(self, *args: Any, tokens: TokenStore | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # A connector with no credential version cannot know whether the
        # credentials behind a stored token are still the current ones, so it
        # keeps its tokens to itself (the NinjaOne rule, for the same reason).
        self.tokens = (
            tokens if tokens is not None else (TOKENS if self.credential_version else TokenStore())
        )
        self._tenant: str | None = None
        self._token: str | None = None
        self._semaphore = asyncio.Semaphore(4)

    # --- which tenant ---------------------------------------------------

    @property
    def login_url(self) -> str:
        return str(self.config.get("login_url") or LOGIN_BASE_URL).rstrip("/")

    @property
    def tenant(self) -> str:
        """The tenant this read is for. Set by `for_tenant`, or the one tenant
        the client has when it has exactly one."""
        if self._tenant is not None:
            return self._tenant
        found = self.tenant_ids()
        if len(found) == 1:
            return found[0]
        if not found:
            raise NoTenant(
                f"no Microsoft 365 tenant is mapped for {self.scope.client_slug!r}. A tenant id "
                f"is the site's mapping (sync it, or set it with manage_client.py map-site).",
                connector=self.name.value,
            )
        raise NoTenant(
            f"{self.scope.client_slug!r} has {len(found)} Microsoft 365 tenants mapped; name the "
            f"site to say which one to read.",
            connector=self.name.value,
        )

    def tenant_ids(self) -> list[str]:
        """Every tenant this call targets, in target order, without repeats."""
        found = [site.external_id for site in self.scope.targets if site.external_id]
        if not found:
            configured = self.config.get("tenant_id")
            if configured:
                found = [str(configured)]
        return list(dict.fromkeys(found))

    def for_tenant(self, tenant: str) -> Microsoft365Client:
        """This connector pointed at one tenant. Returns self: a read is one
        tenant at a time, and `read_each_tenant` walks them in order rather
        than concurrently, because a token belongs to a tenant."""
        self._tenant = tenant
        self._token = None
        return self

    def site_for(self, tenant: str) -> SiteRef | None:
        return next((s for s in self.scope.allowed if s.external_id == tenant), None)

    async def read_each_tenant[T](self, call: Any) -> list[tuple[SiteRef | None, T]]:
        """Run one read for every tenant this call targets, one at a time.

        Serial on purpose: each tenant needs its own token, and a client with
        many customer tenants would otherwise open as many token requests at
        once against one login endpoint.
        """
        found: list[tuple[SiteRef | None, T]] = []
        for tenant in self.tenant_ids():
            self.for_tenant(tenant)
            found.append((self.site_for(tenant), await call()))
        return found

    # --- the read-only guard ---------------------------------------------

    def _guard(self, method: str, url: str) -> None:
        if not url.startswith(self.base_url):
            return  # the login host, matched below
        if method.upper() == "GET":
            return
        if method.upper() == "POST" and self._path_of(url) in READ_ONLY_POSTS:
            return
        raise ReadOnlyViolation(
            f"{self.name.value} is read-only: {method.upper()} {self._path_of(url)} is not a read. "
            f"Every permission this connector asks for ends in .Read.All, and this client sends "
            f"nothing but GET.",
            connector=self.name.value,
            details={"method": method.upper(), "path": self._path_of(url)},
        )

    def _path_of(self, url: str) -> str:
        return url[len(self.base_url) :].split("?")[0] if url.startswith(self.base_url) else url

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        self._guard(method, url)
        if url.startswith(self.login_url) and method.upper() != "POST":
            raise ReadOnlyViolation(
                f"{self.name.value}: only the token request goes to the login host",
                connector=self.name.value,
            )
        return await super().request(method, url, **kwargs)

    # --- auth -------------------------------------------------------------

    @property
    def _token_key(self) -> tuple[uuid.UUID, str | None, str, str]:
        return (self.scope.client_id, self.credential_version, self.tenant, GRAPH_SCOPE)

    def _is_authenticated(self) -> bool:
        self._token = self.tokens.get(self._token_key)
        return self._token is not None

    def _invalidate_auth(self) -> None:
        self.tokens.discard(self._token_key, self._token)
        self._token = None

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def _authenticate(self) -> None:
        """One app-only token for this tenant, by certificate where we have
        one and by secret otherwise."""
        tenant = self.tenant
        key = self._token_key
        async with self.tokens.lock(key):
            if self.tokens.get(key) is not None:
                self._token = self.tokens.get(key)
                return
            payload = {
                "grant_type": "client_credentials",
                "client_id": self.secret("client_id"),
                "scope": GRAPH_SCOPE,
            }
            payload.update(self._credential_proof(tenant))
            path = f"{self.login_url}/{tenant}/oauth2/v2.0/token"
            response = await super().request(
                "POST",
                path,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                authenticated=False,
            )
            body = self._decode(response, path)
            token = body.get("access_token") if isinstance(body, dict) else None
            if not token:
                raise AuthError(
                    f"{self.name.value} returned no access token for this tenant",
                    connector=self.name.value,
                )
            expires = int(body.get("expires_in") or 3600)
            expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=expires) - TOKEN_SKEW
            self.tokens.put(key, str(token), expires_at)
            self._token = str(token)

    def _credential_proof(self, tenant: str) -> dict[str, str]:
        """A certificate assertion where the credential carries a private key,
        a client secret otherwise. A certificate is preferred: it is not a
        bearer value, so a copy of our configuration is not a copy of our
        access (docs/m365-connector.md)."""
        private_key = self._secrets.get("certificate_private_key")
        if private_key:
            assertion = build_client_assertion(
                tenant=tenant,
                client_id=self.secret("client_id"),
                private_key_pem=str(private_key),
                certificate_pem=str(self._secrets.get("certificate_pem") or ""),
                login_url=self.login_url,
            )
            return {
                "client_assertion_type": ("urn:ietf:params:oauth:client-assertion-type:jwt-bearer"),
                "client_assertion": assertion,
            }
        return {"client_secret": self.secret("client_secret")}

    # --- errors -----------------------------------------------------------

    def _error_detail(self, response: httpx.Response) -> str | None:
        """Graph nests its explanation one level down (`{"error": {"code",
        "message"}}`), which the shared reader does not look into: without
        this every Graph refusal reads as a bare status, and the one that says
        a tenant has no premium licence would be indistinguishable from a
        credential that was rejected."""
        try:
            body = response.json()
        except ValueError:
            return super()._error_detail(response)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                parts = [str(error.get(key)) for key in ("code", "message") if error.get(key)]
                if parts:
                    return " ".join(parts)[:200]
        return super()._error_detail(response)

    def _error_for(self, response: httpx.Response, path: str) -> ConnectorError:
        """A refusal a tenant's own licensing explains is `NotLicensed`, which
        a caller reports as not available rather than as a failure."""
        detail = (self._error_detail(response) or "").casefold()
        if any(phrase in detail for phrase in PREMIUM_PHRASES):
            return NotLicensed(
                "this tenant does not have the Microsoft Entra ID licence this needs",
                connector=self.name.value,
                status_code=response.status_code,
                details={"path": self._path_of(path)},
            )
        return super()._error_for(response, path)

    # --- paging -----------------------------------------------------------

    async def _pages(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Follow `@odata.nextLink` to the page cap.

        Graph pages by an opaque absolute URL rather than a cursor we build, so
        `BaseConnector.paginate` does not fit: the next page is whatever the
        last one said it was, and the parameters are already in it.
        """
        items: list[dict[str, Any]] = []
        url: str | None = path
        send: Mapping[str, Any] | None = params
        for _ in range(self.limits.max_pages):
            assert url is not None
            response = await self.request("GET", url, params=send, headers=headers)
            body = self._decode(response, url)
            if not isinstance(body, dict):
                raise UpstreamError(
                    f"{self.name.value} returned {type(body).__name__}, not an object",
                    connector=self.name.value,
                    details={"path": self._path_of(url)},
                )
            batch = body.get("value")
            if isinstance(batch, list):
                items.extend(row for row in batch if isinstance(row, dict))
            url = body.get("@odata.nextLink")
            send = None  # the next link carries its own query
            if not url:
                return items, False
        return items, True

    # --- reads ------------------------------------------------------------

    async def list_users(
        self, *, with_sign_in: bool = True
    ) -> tuple[list[GraphUser], bool, str | None]:
        """Every user in the tenant.

        Returns the users, whether the page cap truncated the list, and the
        reason sign-in activity is missing when it is. `signInActivity` needs
        an Entra ID P1 licence, so it is asked for and, when the tenant refuses
        it for that reason, asked for again without — the rest of a user's
        state is worth having on a tenant that has no premium licence.
        """
        fields = list(USER_FIELDS)
        if with_sign_in:
            try:
                rows, truncated = await self._pages(
                    "/users",
                    params={
                        "$select": ",".join([*fields, "signInActivity"]),
                        "$top": USERS_PAGE_WITH_SIGN_IN,
                    },
                )
                return [GraphUser.model_validate(row) for row in rows], truncated, None
            except (NotLicensed, AuthError) as exc:
                # No premium licence, or `AuditLog.Read.All` was not consented
                # to. Either way the rest of a user's state is worth having,
                # and what is missing is said rather than shown as never.
                logger.info(
                    "%s: reading users without sign-in activity (%s)",
                    self.name.value,
                    type(exc).__name__,
                )
                reason = _why(exc)
        else:
            reason = "not read"
        rows, truncated = await self._pages(
            "/users", params={"$select": ",".join(fields), "$top": USERS_PAGE}
        )
        return [GraphUser.model_validate(row) for row in rows], truncated, reason

    async def get_user(self, address: str) -> GraphUser | None:
        """One user by address, or None. Matched on the user principal name
        first and then on `mail`, which is what a ticket's contact carries."""
        safe = address.replace("'", "''")
        where = f"userPrincipalName eq '{safe}' or mail eq '{safe}'"
        try:
            rows, _ = await self._pages(
                "/users",
                params={
                    "$select": ",".join([*USER_FIELDS, "signInActivity"]),
                    "$filter": where,
                    "$top": 2,
                },
            )
        except (NotLicensed, AuthError):
            # A tenant with no premium licence refuses the whole query when
            # `signInActivity` is selected, so the same read runs without it
            # and the account's state is still answered.
            rows, _ = await self._pages(
                "/users",
                params={"$select": ",".join(USER_FIELDS), "$filter": where, "$top": 2},
            )
        if not rows:
            return None
        return GraphUser.model_validate(rows[0])

    async def get_manager(self, user_id: str) -> GraphUser | None:
        """A user's manager, None where none is set, or `NotAvailable`.

        Graph's reference for `/users/{id}/manager` says application access is
        "Not supported", while the description of the `User.Read.All`
        application permission says it reads "the full set of profile
        properties, reports, and managers of all users". The two contradict
        each other, so this asks and reports what it is told: a 404 is a user
        with no manager, a refusal is `NotAvailable` and the page says the
        manager could not be read for this tenant (docs/m365-connector.md).
        """
        try:
            body = await self.get_json(
                f"/users/{user_id}/manager",
                params={"$select": "id,displayName,mail,userPrincipalName"},
            )
        except NotFound:
            return None
        except AuthError as exc:
            raise NotAvailable(
                "this tenant did not allow the manager to be read with application permissions",
                connector=self.name.value,
            ) from exc
        return GraphUser.model_validate(body) if isinstance(body, dict) else None

    async def list_subscribed_skus(self) -> list[SubscribedSku]:
        """The tenant's subscriptions. Not paged by Graph: one object list."""
        body = await self.get_json("/subscribedSkus")
        rows = body.get("value") if isinstance(body, dict) else None
        return [SubscribedSku.model_validate(row) for row in rows or []]

    async def list_registration_details(self) -> tuple[list[RegistrationDetail], bool]:
        """The authentication methods registration report: who has registered
        a second factor. The only way to see this tenant-wide with
        application permissions."""
        rows, truncated = await self._pages(
            "/reports/authenticationMethods/userRegistrationDetails", params={"$top": 999}
        )
        return [RegistrationDetail.model_validate(row) for row in rows], truncated

    async def list_groups(self, *, with_counts: bool = True) -> tuple[list[GraphGroup], bool]:
        """Every group, and how many members each has.

        The count is its own documented read — `/groups/{id}/members/$count`
        with `ConsistencyLevel: eventual`, which answers a bare number rather
        than JSON — because Graph has no way to carry a member count on a
        group listing. One small request per group, bounded by the page cap;
        `with_counts=False` skips them where the caller only needs the groups.
        """
        rows, truncated = await self._pages(
            "/groups", params={"$select": ",".join(GROUP_FIELDS), "$top": USERS_PAGE}
        )
        groups = [GraphGroup.model_validate(row) for row in rows]
        if with_counts:
            for group in groups:
                group.member_count = await self.member_count(group.id)
        return groups, truncated

    async def member_count(self, group_id: str) -> int | None:
        """How many members one group has, or None when the tenant would not
        say. `$count` answers `text/plain`, not JSON."""
        try:
            response = await self.request(
                "GET",
                f"/groups/{group_id}/members/$count",
                headers={"ConsistencyLevel": "eventual"},
            )
        except ConnectorError:
            return None
        try:
            return int(response.text.strip())
        except ValueError:
            return None

    async def list_group_members(self, group_id: str) -> tuple[list[GraphUser], bool]:
        """The users in one group. Nested groups and devices are not users and
        are left out rather than counted as people."""
        rows, truncated = await self._pages(
            f"/groups/{group_id}/members/microsoft.graph.user",
            params={"$select": ",".join(USER_FIELDS), "$top": 999},
        )
        return [GraphUser.model_validate(row) for row in rows], truncated

    async def list_user_groups(self, user_id: str) -> tuple[list[GraphGroup], bool]:
        """Every group a user is in, including through nesting."""
        rows, truncated = await self._pages(
            f"/users/{user_id}/transitiveMemberOf/microsoft.graph.group",
            params={"$select": ",".join(GROUP_FIELDS), "$top": 999},
        )
        return [GraphGroup.model_validate(row) for row in rows], truncated

    async def mailbox_usage(self, *, period: str = "D30") -> list[MailboxUsage]:
        """`getMailboxUsageDetail`, as rows.

        Graph answers a `302` whose `Location` is a pre-authenticated download
        URL that is valid for a few minutes and "doesn't require an
        Authorization header". So the redirect is followed here rather than by
        the HTTP client: the second hop goes to a storage host and must not
        carry our token.

        A tenant whose report settings conceal names returns rows whose
        identity is an opaque string; those rows say so (`MailboxUsage.
        concealed`) rather than being joined to the wrong person.
        """
        path = f"/reports/getMailboxUsageDetail(period='{period}')"
        response = await self.request("GET", path, follow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            if not location:
                raise UpstreamError(
                    f"{self.name.value} redirected the usage report to nowhere",
                    connector=self.name.value,
                    details={"path": path},
                )
            # No Authorization header: the URL carries its own authorisation,
            # and sending ours to a storage host would be sending it off Graph.
            response = await self.http.get(location)
            response.raise_for_status()
        return parse_mailbox_usage(response.text)

    async def mailbox_purpose(self, user_id: str) -> str | None:
        """What kind of mailbox this is: `user`, `shared`, `room`, `equipment`,
        `linked` or `others`. The one app-only way to tell a shared mailbox
        from a person's, and it is per user — there is no tenant-wide filter —
        so a caller reads it for the accounts it cares about rather than for
        every one. Needs `MailboxSettings.Read`, which a tenant may not have
        consented to: a refusal is `NotAvailable`, not an error.
        """
        try:
            body = await self.get_json(f"/users/{user_id}/mailboxSettings/userPurpose")
        except NotFound:
            return None
        except AuthError as exc:
            raise NotAvailable(
                "MailboxSettings.Read was not granted for this tenant, so a shared mailbox "
                "cannot be told from a person's",
                connector=self.name.value,
            ) from exc
        if isinstance(body, dict):
            value = body.get("value") or body.get("userPurpose")
            return str(value) if value else None
        return None

    async def report_settings(self) -> bool | None:
        """Whether this tenant conceals the names in its usage reports. True
        means every mailbox row is pseudonymous and cannot be joined to a
        user; None means the setting could not be read."""
        try:
            body = await self.get_json("/admin/reportSettings")
        except ConnectorError:
            return None
        if isinstance(body, dict) and isinstance(body.get("displayConcealedNames"), bool):
            return bool(body["displayConcealedNames"])
        return None

    # --- sites --------------------------------------------------------------

    async def list_organisations(self) -> list[VendorOrganisation]:
        """A Graph token is for one tenant and cannot enumerate the others, so
        Microsoft 365 has no site discovery: a tenant id is mapped onto a site
        by a person, from the consent the client's admin granted."""
        raise ConnectorNotConfigured(
            "Microsoft 365 tenants cannot be listed from a tenant's own token: each one is "
            "consented separately, so its tenant id is mapped onto the site by hand "
            "(docs/m365-connector.md).",
            connector=self.name.value,
        )


def parse_mailbox_usage(csv_text: str) -> list[MailboxUsage]:
    """The usage report's CSV, as rows. Column names rather than positions:
    Microsoft has moved them, and a report read by position would quietly
    report the wrong number."""
    import csv
    import io

    rows: list[MailboxUsage] = []
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
    for raw in reader:
        row = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
        rows.append(
            MailboxUsage(
                user_principal_name=row.get("User Principal Name") or None,
                display_name=row.get("Display Name") or None,
                storage_used_bytes=_as_int(row.get("Storage Used (Byte)")),
                item_count=_as_int(row.get("Item Count")),
                deleted=(row.get("Is Deleted") or "").casefold() == "true",
                last_activity=_as_date(row.get("Last Activity Date")),
                issue_warning_quota_bytes=_as_int(row.get("Issue Warning Quota (Byte)")),
            )
        )
    return rows


def _why(exc: Exception) -> str:
    """Why an item is missing, in words a page prints."""
    if isinstance(exc, NotLicensed):
        return "this tenant has no Microsoft Entra ID P1 or P2 licence"
    return "this tenant did not allow it to be read (AuditLog.Read.All)"


def _as_int(value: str | None) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def build_client_assertion(
    *, tenant: str, client_id: str, private_key_pem: str, certificate_pem: str, login_url: str
) -> str:
    """The signed JWT a certificate credential sends instead of a secret.

    RS256 over the header and claims Microsoft documents: `x5t#S256` is the
    certificate's SHA-256 thumbprint, base64url, which is how the tenant finds
    which of the application's registered certificates signed this.
    """
    import json

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.x509 import load_pem_x509_certificate

    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthError(
            "the Microsoft 365 certificate credential is not an RSA private key",
            connector=ConnectorName.MICROSOFT365.value,
        )
    if not certificate_pem.strip():
        raise AuthError(
            "a Microsoft 365 certificate credential needs the certificate beside the private "
            "key: its thumbprint is what tells the tenant which key signed the assertion",
            connector=ConnectorName.MICROSOFT365.value,
        )
    certificate = load_pem_x509_certificate(certificate_pem.encode("utf-8"))
    thumbprint = _b64url(certificate.fingerprint(hashes.SHA256()))
    now = dt.datetime.now(dt.UTC)
    header = {"alg": "RS256", "typ": "JWT", "x5t#S256": thumbprint}
    claims = {
        "aud": f"{login_url}/{tenant}/oauth2/v2.0/token",
        "iss": client_id,
        "sub": client_id,
        "jti": str(uuid.uuid4()),
        "nbf": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=10)).timestamp()),
    }
    signing_input = b".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8")).encode("ascii")
        for part in (header, claims)
    )
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def tenant_ids_of(sites: Sequence[SiteRef]) -> list[str]:
    return list(dict.fromkeys(site.external_id for site in sites if site.external_id))


__all__ = [
    "GRAPH_BASE_URL",
    "GRAPH_SCOPE",
    "LOGIN_BASE_URL",
    "READ_ONLY_POSTS",
    "USERS_PAGE",
    "USERS_PAGE_WITH_SIGN_IN",
    "USER_FIELDS",
    "Microsoft365Client",
    "NoTenant",
    "NotAvailable",
    "NotLicensed",
    "ReadOnlyViolation",
    "build_client_assertion",
    "parse_mailbox_usage",
]

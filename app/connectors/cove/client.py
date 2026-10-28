"""N-able Cove Data Protection connector (Backup.Management JSON-RPC API).

**Read-only is enforced here, not by the credential.** Cove has no read-only
API scope: the same session that lists devices can remove them, and the
methods sit side by side on one endpoint with one visa. A console role can be
made read-only, but a role is somebody's setting rather than a guarantee we
hold. So this client carries an allowlist of method names
(`READ_ONLY_METHODS`), checks every call against it before a request exists,
and raises `ReadOnlyViolation` for anything else — the same answer as
GravityZone and ConnectSecure (hard rule 2).

Everything is `POST https://api.backup.management/jsonapi`; the method is a
field in the body, not a path. Authentication is a **visa**, not a header:
`Login` returns one at the top level of the response, it lasts about fifteen
minutes, and **every response carries a fresh one**, so a session is a chain
that only breaks after a quiet quarter of an hour. We therefore read the visa
out of every answer and re-login once when one is refused.

Scoping is the usual two levels. The credential belongs to the franchisee's
own partner; each end customer is a partner beneath it, held as a `Site`
whose mapping carries the partner id. A device read names one partner, and
every row that comes back is checked against the client's whole allowlist
anyway (`_scoped`), because a filter we did not write is not a guard.

**A row is not always a machine.** Cove holds a customer's Microsoft 365
tenant as a device, with no OS and no physicality, and counts it in its own
device total. `CoveDevice.is_machine` is the distinction, and callers that
count machines use it rather than the row count.

Nothing here has been run against a live account. `docs/api-notes/cove.md`
lists what is documentation and what is still to be established, and
`scripts/cove.py profile --diagnose` is how it gets established.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final

from app.connectors.base import BaseConnector
from app.connectors.cove import columns as col
from app.connectors.cove.models import CoveDevice, CovePartner
from app.connectors.errors import (
    AuthError,
    ConnectorError,
    NotFound,
    UpstreamError,
)
from app.connectors.scope import SiteRef, VendorOrganisation
from app.models.enums import ConnectorName

logger = logging.getLogger(__name__)

#: Every method this connector may call. All of them read. Anything else —
#: `AddAccount`, `ModifyAccount`, `RemoveAccount`, `AddPartner`,
#: `ModifyPartner`, `RemovePartner`, the user methods, `RegenerateUid` — is
#: refused before a request is built, whatever a caller asks for.
#:
#: The list holds only the methods this connector actually calls. A read whose
#: parameter shape we have not established is a candidate in the api-notes,
#: not an entry here: an allowlist nobody has exercised is a list of guesses.
READ_ONLY_METHODS: Final[frozenset[str]] = frozenset(
    {
        "Login",
        "EnumeratePartners",
        "EnumerateAccountStatistics",
    }
)

#: The one path. The method is in the body.
JSONAPI_PATH: Final = "/jsonapi"

#: Documented at fifteen minutes. We treat a visa as spent a little early,
#: because the cost of being wrong is a failed read and the cost of being
#: early is one extra login.
VISA_LIFETIME = dt.timedelta(minutes=15)
VISA_REFRESH_AFTER = dt.timedelta(minutes=12)

#: Devices per page. The vendor's own recipe asks for 5,000 at a time; we ask
#: for less and page, because a page that times out is a read that failed and
#: a page that is short is the only signal that the list has ended (the
#: documentation does not say the total row count comes back anywhere).
PAGE_SIZE: Final = 500

#: A guard against a vendor that never reports the end of a list.
MAX_PAGES: Final = 200

#: Sites read at once when no site is named. Bounded for two reasons: a
#: franchisee may have sixty customers and nobody wants sixty device
#: listings in flight, and every answer carries a fresh visa that the
#: concurrent callers would otherwise race to store. Any visa in the chain
#: is valid, so the race is harmless, but a bounded one is easier to reason
#: about than a harmless one.
SITE_CONCURRENCY: Final = 4

#: The only two selection modes the vendor accepts (live, 2026-10-19:
#: `Undefined` and `Count` are refused as "Unknown selection mode: 0" and
#: ": 3"). `Merged` is one row per device; `PerInstallation` splits a
#: reinstalled agent into a row per installation, which would double-count a
#: machine — on the pilot it is 55 rows against Merged's 53.
SELECTION_MERGED: Final = "Merged"

#: The level of the partner tree an end customer sits at.
END_CUSTOMER: Final = "EndCustomer"

#: `EnumeratePartners` `fields`: 0 Name, 1 Level, 5 State. We ask for the
#: three we read and nothing else.
PARTNER_FIELDS: Final[tuple[int, ...]] = (0, 1, 5)

#: What a refused visa looks like. Unverified: the vendor documents neither a
#: code nor a message for it, so this is matched broadly on purpose and is the
#: first thing the live run narrows (docs/api-notes/cove.md).
SESSION_EXPIRED_PHRASES: Final[tuple[str, ...]] = (
    "visa",
    "session",
    "expired",
    "not logged in",
    "authentication",
)

#: Phrases that mean the credential itself is wrong rather than insufficient.
BAD_CREDENTIAL_PHRASES: Final[tuple[str, ...]] = (
    "password",
    "username",
    "credential",
    "unknown partner",
    "login failed",
)

#: Phrases that mean this user may not do that: a role problem, not a secret.
NO_PERMISSION_PHRASES: Final[tuple[str, ...]] = (
    "permission",
    "not allowed",
    "forbidden",
    "access denied",
    "insufficient",
)


class ReadOnlyViolation(PermissionError):
    """A caller asked for a method that is not a read. No request was made."""


class CoveClient(BaseConnector):
    """Read-only access to one franchisee's Cove account."""

    name: ClassVar[ConnectorName] = ConnectorName.COVE
    default_base_url: ClassVar[str] = "https://api.backup.management"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._visa: str | None = None
        self._visa_at: dt.datetime | None = None
        self._partner_id: int | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self.calls_made: list[str] = []
        """Method names called, in order. Counts and ids for the audit trail;
        never a parameter, because parameters carry names."""

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """What `_fan_out_refs` reads to bound a fan-out over a client's sites."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(SITE_CONCURRENCY)
        return self._semaphore

    # --- the guard ------------------------------------------------------

    @staticmethod
    def is_read_only(method: str) -> bool:
        return method in READ_ONLY_METHODS

    def _setting(self, key: str) -> str:
        """One non-secret config field, with a clear error when it is missing.

        The partner name and the API user's address are identifiers, not
        secrets: they are stored in the clear beside the credential and typed
        at an ordinary prompt. Only the password is hidden.
        """
        value = self.config.get(key)
        if value is None or str(value).strip() == "":
            raise AuthError(
                f"cove needs {key!r} in this client's credential config; set it with "
                f"manage_client.py set-credentials --connector cove",
                connector=self.name.value,
            )
        return str(value)

    # --- the visa -------------------------------------------------------

    @property
    def partner_id(self) -> int | None:
        """Our own partner id, known only once we have logged in."""
        return self._partner_id

    def _is_authenticated(self) -> bool:
        if self._visa is None or self._visa_at is None:
            return False
        return dt.datetime.now(dt.UTC) - self._visa_at < VISA_REFRESH_AFTER

    def _invalidate_auth(self) -> None:
        self._visa = None
        self._visa_at = None

    async def _authenticate(self) -> None:
        """`Login`, the one call that carries no visa.

        The password reaches the vendor and nothing else: it is never logged,
        never put in an error, and never returned by this class.
        """
        body = await self._send(
            "Login",
            {
                "partner": self._setting("partner"),
                "username": self._setting("username"),
                "password": self.secret("password"),
            },
        )
        result = body.get("result")
        inner = result.get("result") if isinstance(result, Mapping) else None
        if isinstance(inner, Mapping):
            partner = col.as_int(str(inner.get("PartnerId") or ""))
            if partner is not None:
                self._partner_id = partner
        if self._visa is None:
            raise AuthError(
                "Cove accepted the login and returned no visa, so nothing else can be asked of it",
                connector=self.name.value,
            )
        logger.info(
            "cove: signed in for client=%s partner=%s", self.scope.client_slug, self._partner_id
        )

    # --- one call -------------------------------------------------------

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """One JSON-RPC call, with the visa chain kept up.

        Refuses anything outside `READ_ONLY_METHODS` before any HTTP. A call
        refused for a stale visa is logged in again once and retried; a second
        refusal is an `AuthError`, because a login that does not stick is a
        credential problem and not a session one.
        """
        if not self.is_read_only(method):
            raise ReadOnlyViolation(
                f"cove {method} is not a read-only method; this connector makes no request "
                "for it (hard rule 2)"
            )
        await self._ensure_authenticated()
        try:
            body = await self._send(method, params)
        except AuthError as exc:
            if not self._looks_expired(exc):
                raise
            logger.info("cove: visa refused for %s; signing in again", method)
            self._invalidate_auth()
            await self._ensure_authenticated()
            body = await self._send(method, params)
        result = body.get("result")
        if isinstance(result, Mapping) and "result" in result:
            return result["result"]
        return result

    async def _send(self, method: str, params: Mapping[str, Any] | None) -> Mapping[str, Any]:
        """The envelope, the request, the fresh visa, and the error mapping."""
        envelope: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": "jsonrpc",
            "method": method,
            "params": dict(params or {}),
        }
        if self._visa is not None:
            envelope["visa"] = self._visa
        self.calls_made.append(method)
        response = await self.request(
            "POST",
            JSONAPI_PATH,
            json_body=envelope,
            headers={"Content-Type": "application/json"},
            authenticated=False,
        )
        body = self._decode(response, JSONAPI_PATH)
        if not isinstance(body, Mapping):
            raise UpstreamError(
                f"Cove returned {type(body).__name__} where a JSON-RPC envelope was expected",
                connector=self.name.value,
                details={"method": method},
            )
        visa = body.get("visa")
        if isinstance(visa, str) and visa:
            self._visa = visa
            self._visa_at = dt.datetime.now(dt.UTC)
        error = body.get("error")
        if error:
            raise self._rpc_error(error, method=method)
        return body

    def _rpc_error(self, error: Any, *, method: str) -> ConnectorError:
        """Map Cove's error member onto the normal hierarchy.

        The vendor documents neither its codes nor its messages, so this reads
        the message as well as the code and errs towards `UpstreamError` — the
        one that says "we do not know", rather than one that tells somebody to
        go and change a password. Every branch here is a candidate for
        narrowing once the live account has refused us in each way
        (docs/api-notes/cove.md).
        """
        code = error.get("code") if isinstance(error, Mapping) else None
        message = str(error.get("message", "")) if isinstance(error, Mapping) else str(error)
        details = {"method": method, "code": code, "message": message[:200]}
        lowered = message.casefold()
        if any(phrase in lowered for phrase in NO_PERMISSION_PHRASES):
            return AuthError(
                f"Cove refused {method}: this API user may not do that ({message}). "
                "That is a role, not a password.",
                connector=self.name.value,
                details=details,
            )
        if any(phrase in lowered for phrase in BAD_CREDENTIAL_PHRASES):
            return AuthError(
                f"Cove rejected the credentials for {method}: {message}",
                connector=self.name.value,
                details=details,
            )
        if any(phrase in lowered for phrase in SESSION_EXPIRED_PHRASES):
            return AuthError(
                f"Cove refused {method} for the session: {message}",
                connector=self.name.value,
                details=details,
            )
        if "not found" in lowered or "does not exist" in lowered:
            return NotFound(
                f"Cove has no such object for {method}: {message}",
                connector=self.name.value,
                details=details,
            )
        return UpstreamError(
            f"Cove {method} failed: {message}",
            connector=self.name.value,
            details=details,
        )

    @staticmethod
    def _looks_expired(exc: AuthError) -> bool:
        lowered = str(exc).casefold()
        return any(phrase in lowered for phrase in SESSION_EXPIRED_PHRASES)

    # --- what the vendor calls an empty list ----------------------------

    def _rows(self, result: Any, *, method: str, where: str) -> list[Mapping[str, Any]]:
        """The rows of a list-returning call.

        **Cove answers an empty list with `null`, not `[]`** (live, 2026-10-19:
        two customers with no devices). So `None` is no rows, which is a fact
        about the customer and not a failure — a customer between offboarding
        one machine and installing the next would otherwise read as a vendor
        that did not answer, and those are the two things the coverage rules
        most need to keep apart.

        Any *other* shape still raises. A string is a `Sequence` and iterating
        one yields characters, so the test is `list` and nothing looser.
        """
        if result is None:
            return []
        if not isinstance(result, list):
            raise UpstreamError(
                f"Cove answered {method} with {type(result).__name__} where a list of rows "
                "was expected",
                connector=self.name.value,
                details={"method": method, "where": where},
            )
        return [row for row in result if isinstance(row, Mapping)]

    # --- customers ------------------------------------------------------

    async def list_partners(self) -> list[CovePartner]:
        """Every customer beneath ours, as a flat list."""
        await self._ensure_authenticated()
        if self._partner_id is None:
            raise AuthError(
                "Cove did not say which partner we are, so its customers cannot be listed",
                connector=self.name.value,
            )
        rows = await self.call(
            "EnumeratePartners",
            {
                "parentPartnerId": self._partner_id,
                "fetchRecursively": True,
                "fields": list(PARTNER_FIELDS),
            },
        )
        found = [
            CovePartner.model_validate(row)
            for row in self._rows(rows, method="EnumeratePartners", where="customers")
        ]
        logger.info("cove: %d customer(s) under partner %s", len(found), self._partner_id)
        return found

    async def list_organisations(self) -> list[VendorOrganisation]:
        """The end customers, for `sync_sites`.

        Only `EndCustomer` partners become sites: the levels above are
        distributors and the franchisee itself, which are not somebody's
        estate. A partner whose level the vendor did not send is left out
        rather than guessed at, and the count of those is logged.
        """
        partners = await self.list_partners()
        customers = [p for p in partners if p.level == END_CUSTOMER]
        unnamed_level = sum(1 for p in partners if p.level is None)
        if unnamed_level:
            logger.warning(
                "cove: %d partner(s) carry no level and are not synced as sites", unnamed_level
            )
        return [VendorOrganisation(external_id=str(p.id), name=p.name) for p in customers]

    # --- devices --------------------------------------------------------

    def _columns(self) -> list[str]:
        """Every column a device row needs, built from the declared codes."""
        base = list(col.COLUMNS.values())
        totals = [
            col.stat(col.TOTAL, field)
            for field in (
                "last_session_status",
                "last_session_errors",
                "colour_bar_28_days",
                "last_successful_at",
                "last_session_at",
            )
        ]
        m365 = [
            col.stat(col.M365_EXCHANGE, "user_mailboxes"),
            col.stat(col.M365_EXCHANGE, "shared_mailboxes"),
            col.stat(col.M365_ONEDRIVE, "user_mailboxes"),
        ]
        return [*base, *totals, *m365]

    async def _pages(self, partner_id: int, *, where: str) -> list[Mapping[str, Any]]:
        """Every row for one partner, paged until a page comes back short.

        Short is the only end of the list there is: the vendor does not say
        how many rows match, and an empty list arrives as `null`.
        """
        rows: list[Mapping[str, Any]] = []
        start = 0
        for page in range(MAX_PAGES):
            batch = await self.call(
                "EnumerateAccountStatistics",
                {
                    "query": {
                        "PartnerId": partner_id,
                        "SelectionMode": SELECTION_MERGED,
                        "StartRecordNumber": start,
                        "RecordsCount": PAGE_SIZE,
                        "Columns": self._columns(),
                    }
                },
            )
            found = self._rows(batch, method="EnumerateAccountStatistics", where=where)
            rows.extend(found)
            if len(found) < PAGE_SIZE:
                break
            start += PAGE_SIZE
            if page == MAX_PAGES - 1:
                logger.error(
                    "cove: the device list for %s did not end after %d pages; returning "
                    "what was read",
                    where,
                    MAX_PAGES,
                )
        return rows

    async def list_devices(self, site: SiteRef) -> list[CoveDevice]:
        """Every device Cove holds for one end customer, with its statistics.

        One partner, which is the only scoping the vendor offers. Every row
        is checked against the client's allowlist before it is returned.
        """
        rows = await self._pages(site.as_int(self.name.value), where=f"site {site.slug}")
        devices = [CoveDevice.from_row(row) for row in rows]
        kept = self._scoped(devices, where=f"site {site.slug}")
        logger.info(
            "cove: site=%s devices=%d machines=%d microsoft365=%d",
            site.slug,
            len(kept),
            sum(1 for d in kept if d.is_machine),
            sum(1 for d in kept if d.is_microsoft365),
        )
        return kept

    def _scoped(self, devices: Sequence[CoveDevice], *, where: str) -> list[CoveDevice]:
        """Drop any row that is not this client's, whatever we asked for.

        A record from one of the client's inactive sites goes quietly; one
        from a partner we hold no site for is an error and can never be
        merged in. This is what makes the recursive fleet read safe: it is a
        wider net, not a looser one. Ids only, never a device or customer
        name.
        """
        allowed = self.scope.allowed_ints(self.name.value)
        excluded = {ref.as_int(self.name.value) for ref in self.scope.excluded}
        kept: list[CoveDevice] = []
        dropped_inactive = 0
        dropped_our_own = 0
        unknown: list[int] = []
        for device in devices:
            if device.partner_id in allowed:
                kept.append(device)
            elif device.partner_id in excluded:
                dropped_inactive += 1
            elif device.partner_id == self._partner_id:
                # The franchisee's own partner holds a few machines of its
                # own. A recursive read returns them and no site maps them,
                # which is not a containment problem: we signed in as that
                # partner, so it is the one id we can be sure is ours.
                dropped_our_own += 1
            else:
                unknown.append(device.partner_id)
        if dropped_our_own:
            logger.info(
                "cove: dropped %d row(s) held by the franchisee's own partner (%s) reading %s; "
                "they belong to no end customer",
                dropped_our_own,
                self._partner_id,
                where,
            )
        if dropped_inactive:
            logger.info(
                "cove: dropped %d row(s) from this client's inactive site(s) reading %s",
                dropped_inactive,
                where,
            )
        if unknown:
            logger.error(
                "cove: EnumerateAccountStatistics returned %d row(s) from partner(s) outside "
                "the allowlist of client=%s: %s; dropped. Another franchisee's customer, or "
                "one not yet synced (run sync_sites)",
                len(unknown),
                self.scope.client_slug,
                sorted(set(unknown)),
            )
        return kept

    async def list_fleet(self) -> list[CoveDevice]:
        """Every device under our own partner, in one read.

        **A device read at a partner covers the whole tree beneath it** (live,
        2026-10-19: 53 rows at the franchisee's own partner, against one row
        at a single customer). So a fleet-wide question is one request rather
        than one per customer — 32 calls saved on the pilot — and the rows are
        placed by their own `PartnerId` afterwards.

        Every row still goes through the same guard: a customer we hold no
        site for is dropped with an error and never merged in, exactly as a
        site-scoped read's rows are. A recursive read is a wider net, not a
        looser one.
        """
        # Our own partner id arrives with the login, so the fleet read cannot
        # be the first thing that happens without one.
        await self._ensure_authenticated()
        rows = await self._pages(self._partner_id_or_raise(), where="fleet")
        devices = [CoveDevice.from_row(row) for row in rows]
        kept = self._scoped(devices, where="fleet")
        logger.info(
            "cove: fleet devices=%d machines=%d microsoft365=%d",
            len(kept),
            sum(1 for d in kept if d.is_machine),
            sum(1 for d in kept if d.is_microsoft365),
        )
        return kept

    async def list_fleet_with_sites(self) -> list[tuple[SiteRef, CoveDevice]]:
        """The fleet, each row tagged with the site it belongs to.

        The same one read; the rows carry their own `PartnerId`, so placing
        them is a lookup rather than another request. What the device
        identity layer needs, which works in (site, record) pairs like every
        other connector's listing.
        """
        by_partner = {ref.as_int(self.name.value): ref for ref in self.scope.allowed}
        return [
            (by_partner[device.partner_id], device)
            for device in await self.list_fleet()
            if device.partner_id in by_partner
        ]

    def _partner_id_or_raise(self) -> int:
        if self._partner_id is None:
            raise AuthError(
                "Cove did not say which partner we are, so the fleet cannot be read",
                connector=self.name.value,
            )
        return self._partner_id

    async def list_devices_for_targets(self) -> list[tuple[SiteRef, list[CoveDevice]]]:
        """Every targeted site's devices, a site at a time.

        One partner per call because that is the only scoping the vendor
        offers; a site holding more than one Cove partner fans out and the
        answers merge, which is what `_fan_out_refs` is for.
        """
        answers, _spared = await self._fan_out_refs(self.scope.targets, self.list_devices)
        return [(ref, devices) for ref, devices in answers]

"""Microsoft Graph, faked: the reads `Microsoft365Client` sends, answered from the world.

The real client runs against this unchanged, so its read-only guard, its token
handling, its `@odata.nextLink` paging, its premium-licence fallback and its
error mapping are all exercised exactly as they are against a tenant.

Two hosts, because Graph has two: the login host answers the client-credentials
token request for one tenant, and the Graph host answers the reads with that
tenant's own accounts. A token for one customer's tenant cannot read another's
— the fake checks it, which is the containment rule the real tenants enforce
and the one worth proving we respect.

What is deliberately faithful to Graph rather than convenient:

* `signInActivity` is refused with Microsoft's own wording on the one demo
  tenant without a premium licence, and refused for the *whole query*, which
  is what makes the client's fallback worth having;
* the registration report leaves out disabled accounts, as Graph's does;
* `$count` on a navigation property answers `text/plain`, not JSON;
* the mailbox usage report answers `302` to a URL that carries no token;
* a page is 500 rows when `signInActivity` is selected and 999 otherwise.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from app.demo.api import Call, Router, json_response
from app.demo.dataset import Customer, M365Account, M365Group, World, at

DEMO_BASE_URL = "https://graph.microsoft365.demo.invalid/v1.0"
DEMO_LOGIN_URL = "https://login.microsoft365.demo.invalid"
DOWNLOAD_HOST = "https://reports.microsoft365.demo.invalid"

#: What Graph says when a tenant has no Entra ID P1/P2 and sign-in activity is
#: asked for. The client matches on this, so it is the real wording.
PREMIUM_MESSAGE = "Neither tenant is B2C or tenant doesn't have premium license"

#: Every demo tenant consents to the same set, except one that withholds
#: `MailboxSettings.Read` so the page that says "not available" has a reason
#: to say it.
WITHHELD_MAILBOX_SETTINGS = 2


def _error(status: int, message: str) -> Any:
    code = {
        400: "BadRequest",
        401: "InvalidAuthenticationToken",
        403: "Authorization_RequestDenied",
        404: "ResourceNotFound",
        429: "TooManyRequests",
    }.get(status, "UnknownError")
    return {"error": {"code": code, "message": message}}


def _stamp(days: float | None, now: dt.datetime) -> str | None:
    moment = at(days, now)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


class Microsoft365Api:
    """One fake Graph over every demo customer's tenant."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.router = Router("microsoft365", _error)
        r = self.router
        r.add("POST", "/{tenant}/oauth2/v2.0/token", self.token)
        r.add("GET", "/v1.0/users", self.users)
        r.add("GET", "/v1.0/users/{user_id}", self.user)
        r.add("GET", "/v1.0/users/{user_id}/manager", self.manager)
        r.add(
            "GET",
            "/v1.0/users/{user_id}/transitiveMemberOf/microsoft.graph.group",
            self.user_groups,
        )
        r.add("GET", "/v1.0/users/{user_id}/mailboxSettings/userPurpose", self.mailbox_purpose)
        r.add("GET", "/v1.0/subscribedSkus", self.skus)
        r.add(
            "GET",
            "/v1.0/reports/authenticationMethods/userRegistrationDetails",
            self.registration,
        )
        r.add("GET", "/v1.0/groups", self.groups)
        r.add("GET", "/v1.0/groups/{group_id}/members/$count", self.member_count)
        r.add("GET", "/v1.0/groups/{group_id}/members/microsoft.graph.user", self.members)
        r.add("GET", "/v1.0/admin/reportSettings", self.report_settings)
        # The period is an OData function parameter in the path — literal
        # parentheses and quotes, which a pattern would read as a group — so
        # it is matched as a placeholder and read from there.
        r.add("GET", "/v1.0/reports/getMailboxUsageDetail{period}", self.usage)
        r.add("GET", "/report.csv", self.usage_csv)

    # --- plumbing ---------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        return self.router.handle(request, self.world, dt.datetime.now(dt.UTC))

    def customer_by_tenant(self, tenant: str) -> Customer | None:
        return next((c for c in self.world.customers if c.m365_tenant_id == tenant), None)

    def _tenant_of(self, call: Call) -> tuple[Customer | None, httpx.Response | None]:
        """Which tenant this token is for.

        A demo token is `demo-token-<tenant>`: readable on purpose, and
        checked, so a read with one customer's token can never answer from
        another customer's directory.
        """
        header = call.request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None, json_response(_error(401, "no bearer token"), 401)
        tenant = header.removeprefix("Bearer demo-token-")
        customer = self.customer_by_tenant(tenant)
        if customer is None:
            return None, json_response(_error(401, "the token is not for a tenant here"), 401)
        return customer, None

    def token(self, call: Call) -> httpx.Response:
        tenant = call.path_params["tenant"]
        if self.customer_by_tenant(tenant) is None:
            return json_response(
                {
                    "error": "invalid_request",
                    "error_description": f"AADSTS90002: Tenant {tenant} not found",
                },
                400,
            )
        body = dict(httpx.QueryParams(call.request.content.decode()))
        if not body.get("client_id"):
            return json_response({"error": "invalid_client"}, 401)
        has_secret = bool(body.get("client_secret"))
        has_assertion = body.get("client_assertion_type", "").endswith("jwt-bearer") and bool(
            body.get("client_assertion")
        )
        if not (has_secret or has_assertion):
            return json_response(
                {"error": "invalid_client", "error_description": "no credential"}, 401
            )
        return json_response(
            {
                "token_type": "Bearer",
                "expires_in": 3599,
                "access_token": f"demo-token-{tenant}",
            }
        )

    # --- users ------------------------------------------------------------

    def _user_json(self, account: M365Account, now: dt.datetime, *, sign_in: bool) -> Any:
        row: dict[str, Any] = {
            "id": self._id(account),
            "displayName": account.display_name,
            "userPrincipalName": account.upn,
            "mail": account.upn if account.mailbox_purpose != "others" else None,
            "accountEnabled": account.enabled,
            "createdDateTime": _stamp(account.created_days, now),
            "userType": account.user_type,
            "department": account.department,
            "jobTitle": account.job_title,
            "officeLocation": account.office,
            "assignedLicenses": [
                {"skuId": sku, "disabledPlans": []} for sku in account.licence_skus
            ],
        }
        # Graph sets `onPremisesSyncEnabled` only on synced objects; absent is
        # cloud-only, which is what the client reads it as.
        if account.synced:
            row["onPremisesSyncEnabled"] = True
        if sign_in:
            row["signInActivity"] = {
                "lastSignInDateTime": _stamp(account.last_sign_in_days, now),
                "lastNonInteractiveSignInDateTime": _stamp(account.last_sign_in_days, now),
            }
        return row

    def _id(self, account: M365Account) -> str:
        """An opaque id, as Graph's own are.

        A real directory object id is a GUID that says which account without
        saying who, which is exactly why ids are what the engine stores and
        prints (CLAUDE.md, working conventions). A fake that put the address in
        the id would let a caller leak one by printing the other, and the test
        that checks for it would pass.
        """
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"m365-user:{account.upn}"))

    def users(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        wants_sign_in = "signInActivity" in (call.param("$select") or "")
        if wants_sign_in and not customer.m365_premium:
            return json_response(_error(403, PREMIUM_MESSAGE), 403)
        rows = list(customer.m365_accounts)
        where = call.param("$filter") or ""
        if where:
            wanted = {
                part.split("eq", 1)[1].strip().strip("'").casefold()
                for part in where.split(" or ")
                if "eq" in part
            }
            rows = [a for a in rows if a.upn.casefold() in wanted]
        # Graph's own page caps: 500 with sign-in activity selected, 999 without.
        cap = 500 if wants_sign_in else 999
        size = min(call.int_param("$top", cap), cap)
        start = call.int_param("$skiptoken", 0)
        page = rows[start : start + size]
        body: dict[str, Any] = {
            "value": [self._user_json(a, call.now, sign_in=wants_sign_in) for a in page]
        }
        if start + size < len(rows):
            query = {k: v for k, v in call.params.items() if k != "$skiptoken"}
            query["$skiptoken"] = str(start + size)
            body["@odata.nextLink"] = f"{DEMO_BASE_URL}/users?{urlencode(query)}"
        return json_response(body)

    def _account(self, customer: Customer, user_id: str) -> M365Account | None:
        """By object id or by address, as Graph takes either in `/users/{id}`."""
        wanted = user_id.casefold()
        return next(
            (
                a
                for a in customer.m365_accounts
                if a.upn.casefold() == wanted or self._id(a) == wanted
            ),
            None,
        )

    def user(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        account = self._account(customer, call.path_params["user_id"])
        if account is None:
            return json_response(_error(404, "no user"), 404)
        return json_response(self._user_json(account, call.now, sign_in=customer.m365_premium))

    def manager(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        account = self._account(customer, call.path_params["user_id"])
        if account is None or not account.manager_upn:
            return json_response(_error(404, "no manager"), 404)
        manager = next(
            (a for a in customer.m365_accounts if a.upn == account.manager_upn),
            None,
        )
        if manager is None:
            return json_response(_error(404, "no manager"), 404)
        return json_response(self._user_json(manager, call.now, sign_in=False))

    def mailbox_purpose(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        if self.world.customers.index(customer) == WITHHELD_MAILBOX_SETTINGS:
            return json_response(_error(403, "MailboxSettings.Read was not consented"), 403)
        account = self._account(customer, call.path_params["user_id"])
        if account is None:
            return json_response(_error(404, "no user"), 404)
        return json_response({"@odata.context": "userPurpose", "value": account.mailbox_purpose})

    # --- licences, MFA, groups --------------------------------------------

    def skus(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        from app.demo.dataset import DEMO_SKUS

        assigned: dict[str, int] = {}
        for account in customer.m365_accounts:
            for sku in account.licence_skus:
                assigned[sku] = assigned.get(sku, 0) + 1
        rows = []
        for sku_id, part, _ in DEMO_SKUS:
            used = assigned.get(sku_id, 0)
            if not used and part == "VISIOCLIENT":
                continue
            bought = used + (3 if part == "ENTERPRISEPACK" else 1)
            rows.append(
                {
                    "id": f"{customer.m365_tenant_id}_{sku_id}",
                    "skuId": sku_id,
                    "skuPartNumber": part,
                    "capabilityStatus": "Enabled",
                    "consumedUnits": used,
                    "prepaidUnits": {"enabled": bought, "suspended": 0, "warning": 0},
                }
            )
        return json_response({"value": rows})

    def registration(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        rows = []
        for account in customer.m365_accounts:
            # Graph's report leaves out disabled accounts entirely.
            if not account.enabled:
                continue
            strong = [m for m in account.mfa_methods if m != "mobilePhone"]
            rows.append(
                {
                    "id": self._id(account),
                    "userPrincipalName": account.upn,
                    "userDisplayName": account.display_name,
                    "isAdmin": False,
                    "isMfaRegistered": bool(account.mfa_methods),
                    "isMfaCapable": bool(strong),
                    "userType": account.user_type.lower(),
                    "methodsRegistered": list(account.mfa_methods),
                }
            )
        return json_response({"value": rows})

    def _group_json(self, group: M365Group, now: dt.datetime) -> Any:
        return {
            "id": group.id,
            "displayName": group.name,
            "mail": group.mail,
            "mailEnabled": group.mail is not None,
            "securityEnabled": group.security,
            "groupTypes": ["Unified"] if group.unified else [],
            "resourceProvisioningOptions": ["Team"] if group.team else [],
            "visibility": "Private",
            "createdDateTime": _stamp(group.created_days, now),
        }

    def groups(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        return json_response(
            {"value": [self._group_json(g, call.now) for g in customer.m365_groups]}
        )

    def _group(self, customer: Customer, group_id: str) -> M365Group | None:
        return next((g for g in customer.m365_groups if g.id == group_id), None)

    def member_count(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        if call.request.headers.get("consistencylevel") != "eventual":
            return json_response(
                _error(400, "$count requires the ConsistencyLevel: eventual header"), 400
            )
        group = self._group(customer, call.path_params["group_id"])
        if group is None:
            return json_response(_error(404, "no group"), 404)
        # `$count` is a bare number, not JSON.
        return httpx.Response(
            200, text=str(len(group.member_upns)), headers={"Content-Type": "text/plain"}
        )

    def members(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        group = self._group(customer, call.path_params["group_id"])
        if group is None:
            return json_response(_error(404, "no group"), 404)
        rows = [a for a in customer.m365_accounts if a.upn in group.member_upns]
        return json_response({"value": [self._user_json(a, call.now, sign_in=False) for a in rows]})

    def user_groups(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        account = self._account(customer, call.path_params["user_id"])
        if account is None:
            return json_response(_error(404, "no user"), 404)
        rows = [g for g in customer.m365_groups if g.id in account.group_ids]
        return json_response({"value": [self._group_json(g, call.now) for g in rows]})

    # --- reports ------------------------------------------------------------

    def report_settings(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        # One tenant conceals the names in its reports, as most do by default.
        concealed = self.world.customers.index(customer) == 1
        return json_response(
            {"@odata.context": "reportSettings", "displayConcealedNames": concealed}
        )

    def usage(self, call: Call) -> httpx.Response:
        customer, refusal = self._tenant_of(call)
        if refusal is not None or customer is None:
            return refusal or json_response(_error(401, "no tenant"), 401)
        period = call.path_params.get("period", "")
        if "D7" not in period and "D30" not in period and "D90" not in period:
            return json_response(_error(400, f"period {period} is not one of D7, D30, D90"), 400)
        # Graph answers a redirect to a URL that needs no Authorization header.
        return httpx.Response(
            302, headers={"Location": f"{DOWNLOAD_HOST}/report.csv?t={customer.m365_tenant_id}"}
        )

    def usage_csv(self, call: Call) -> httpx.Response:
        tenant = call.param("t") or ""
        customer = self.customer_by_tenant(tenant)
        if customer is None:
            return json_response(_error(404, "no report"), 404)
        concealed = self.world.customers.index(customer) == 1
        lines = [
            "Report Refresh Date,User Principal Name,Display Name,Is Deleted,Deleted Date,"
            "Created Date,Last Activity Date,Item Count,Storage Used (Byte),"
            "Issue Warning Quota (Byte),Has Archive,Report Period"
        ]
        day = call.now.date().isoformat()
        for n, account in enumerate(customer.m365_accounts):
            if account.mailbox_bytes is None:
                continue
            # A tenant with concealed reporting replaces the identity with an
            # opaque string, which cannot be joined to anybody.
            name = f"{abs(hash(account.upn)) % 10**12:012X}" if concealed else account.upn
            shown = name if concealed else account.display_name
            lines.append(
                f"{day},{name},{shown},False,,"
                f"{(at(account.created_days, call.now) or call.now).date().isoformat()},"
                f"{day},{1000 + n * 37},{account.mailbox_bytes},107374182400,False,30"
            )
        return httpx.Response(
            200, text="\n".join(lines) + "\n", headers={"Content-Type": "application/octet-stream"}
        )

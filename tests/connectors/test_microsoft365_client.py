"""The Microsoft 365 connector against recorded Graph responses.

Nothing here reaches a network: `respx` answers every call. What is proven is
the behaviour a live tenant will exercise and nobody wants to discover there —
paging by `@odata.nextLink`, a 429 with `Retry-After`, a tenant with no
premium licence, a tenant that consented to less than we asked for, and the
one thing that is ours to keep rather than Microsoft's: that this client sends
nothing but reads.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

import httpx
import pytest
import respx

from app.connectors.base import ConnectorLimits
from app.connectors.errors import AuthError, RateLimited
from app.connectors.microsoft365.client import (
    GRAPH_BASE_URL,
    GRAPH_SCOPE,
    READ_ONLY_POSTS,
    USERS_PAGE_WITH_SIGN_IN,
    Microsoft365Client,
    NotAvailable,
    NoTenant,
    NotLicensed,
    ReadOnlyViolation,
    parse_mailbox_usage,
)
from app.connectors.microsoft365.models import (
    GROUP_DISTRIBUTION,
    GROUP_MAIL_SECURITY,
    GROUP_MICROSOFT365,
    GROUP_SECURITY,
    GROUP_TEAM,
)
from app.connectors.microsoft365.tokens import TokenStore
from app.connectors.scope import ClientScope, SiteRef
from app.models.enums import ConnectorName
from tests.connectors.conftest import RecordingSleep, load_fixture

LOGIN = "https://login.test.invalid"
TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_TENANT = "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee"
TOKEN_URL = f"{LOGIN}/{TENANT}/oauth2/v2.0/token"
CLIENT_ID = uuid.uuid4()


def site(tenant: str, slug: str = "contoso") -> SiteRef:
    return SiteRef(site_id=uuid.uuid4(), slug=slug, name=slug.title(), external_id=tenant)


def make_client(
    *,
    limits: ConnectorLimits,
    sites: list[SiteRef] | None = None,
    sleep: RecordingSleep | None = None,
    secrets: dict[str, str] | None = None,
) -> Microsoft365Client:
    allowed = tuple(sites if sites is not None else [site(TENANT)])
    scope = ClientScope(
        client_id=CLIENT_ID,
        client_slug="tailwind-it",
        connector=ConnectorName.MICROSOFT365,
        allowed=allowed,
        targets=allowed,
    )
    return Microsoft365Client(
        scope=scope,
        secrets=secrets or {"client_id": "app-id", "client_secret": "app-secret"},
        config={"login_url": LOGIN},
        limits=limits,
        sleep=sleep,
        tokens=TokenStore(),
    )


def a_token(times: int = 1) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "t0ken", "expires_in": 3600})
    )


# --- read only ----------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE", "HEAD"])
@respx.mock
async def test_nothing_but_a_read_reaches_graph(method: str, fast_limits: ConnectorLimits) -> None:
    sent = respx.route(host="graph.microsoft.com")
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(ReadOnlyViolation, match="read-only"):
            await client.request(method, "/users")
    assert sent.call_count == 0, "the guard runs before a request exists"


def test_the_read_only_post_list_is_empty_and_could_only_hold_a_read() -> None:
    """Graph has read-only POSTs; none is used, so the rule is GET and
    nothing else. An entry here must be one of the three that are reads."""
    assert READ_ONLY_POSTS == frozenset()
    allowed_if_ever = {"/$batch", "/directoryObjects/getByIds", "/users/{id}/getMemberGroups"}
    assert READ_ONLY_POSTS <= allowed_if_ever


@respx.mock
async def test_only_the_token_request_goes_to_the_login_host(
    fast_limits: ConnectorLimits,
) -> None:
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(ReadOnlyViolation, match="only the token request"):
            await client.request("GET", f"{LOGIN}/{TENANT}/oauth2/v2.0/token")


# --- auth ---------------------------------------------------------------------------


@respx.mock
async def test_a_token_is_fetched_per_tenant_and_reused(fast_limits: ConnectorLimits) -> None:
    first = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "one", "expires_in": 3600})
    )
    second = respx.post(f"{LOGIN}/{OTHER_TENANT}/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "two", "expires_in": 3600})
    )
    users = respx.get(f"{GRAPH_BASE_URL}/users").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    sites = [site(TENANT, "contoso"), site(OTHER_TENANT, "fabrikam")]
    async with make_client(limits=fast_limits, sites=sites) as client:
        found = await client.read_each_tenant(lambda: client.list_users())
    assert [s.slug for s, _ in found] == ["contoso", "fabrikam"]
    assert first.call_count == 1 and second.call_count == 1
    # Each tenant's own token on its own reads, and never the other's.
    sent = [call.request.headers["Authorization"] for call in users.calls]
    assert sent == ["Bearer one", "Bearer two"]
    body = dict(httpx.QueryParams(first.calls[0].request.content.decode()))
    assert body["grant_type"] == "client_credentials" and body["scope"] == GRAPH_SCOPE
    assert body["client_secret"] == "app-secret"


@respx.mock
async def test_a_certificate_credential_sends_an_assertion_and_no_secret(
    fast_limits: ConnectorLimits,
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509 import CertificateBuilder, Name, NameAttribute, random_serial_number
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = Name([NameAttribute(NameOID.COMMON_NAME, "desk-test")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .sign(
            key, __import__("cryptography.hazmat.primitives.hashes", fromlist=["SHA256"]).SHA256()
        )
    )
    secrets = {
        "client_id": "app-id",
        "certificate_private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "certificate_pem": certificate.public_bytes(serialization.Encoding.PEM).decode(),
    }
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "cert", "expires_in": 3600})
    )
    respx.get(f"{GRAPH_BASE_URL}/users").mock(return_value=httpx.Response(200, json={"value": []}))
    async with make_client(limits=fast_limits, secrets=secrets) as client:
        await client.list_users()
    body = dict(httpx.QueryParams(token.calls[0].request.content.decode()))
    assert body["client_assertion_type"] == (
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    )
    assert "client_secret" not in body
    header, claims, signature = body["client_assertion"].split(".")
    import base64
    import json

    def unpad(part: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

    assert unpad(header)["alg"] == "RS256" and unpad(header)["x5t#S256"]
    payload = unpad(claims)
    assert payload["aud"] == TOKEN_URL
    assert payload["iss"] == payload["sub"] == "app-id"
    assert payload["exp"] > payload["nbf"] and signature


@respx.mock
async def test_a_certificate_without_its_public_half_is_refused(
    fast_limits: ConnectorLimits,
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    secrets = {
        "client_id": "app-id",
        "certificate_private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    }
    async with make_client(limits=fast_limits, secrets=secrets) as client:
        with pytest.raises(AuthError, match="needs the certificate"):
            await client.list_subscribed_skus()


@respx.mock
async def test_no_tenant_mapped_is_said_plainly(fast_limits: ConnectorLimits) -> None:
    async with make_client(limits=fast_limits, sites=[]) as client:
        with pytest.raises(NoTenant, match="no Microsoft 365 tenant is mapped"):
            await client.list_subscribed_skus()
    two = [site(TENANT), site(OTHER_TENANT, "fabrikam")]
    async with make_client(limits=fast_limits, sites=two) as client:
        with pytest.raises(NoTenant, match="name the site"):
            await client.list_subscribed_skus()


# --- paging and throttling ------------------------------------------------------------


@respx.mock
async def test_users_are_read_across_pages(fast_limits: ConnectorLimits) -> None:
    a_token()
    page1 = load_fixture("microsoft365", "users_page1.json")
    page2 = load_fixture("microsoft365", "users_page2.json")
    respx.get(f"{GRAPH_BASE_URL}/users", params__contains={"$skiptoken": "PAGE2"}).mock(
        return_value=httpx.Response(200, json=page2)
    )
    first = respx.get(f"{GRAPH_BASE_URL}/users").mock(return_value=httpx.Response(200, json=page1))
    async with make_client(limits=fast_limits) as client:
        users, truncated, missing = await client.list_users()
    assert [u.display_name for u in users] == ["Dana Whitfield", "Marcus Bell", "Priya Raman"]
    assert truncated is False and missing is None
    # Graph caps a page at 500 when sign-in activity is selected.
    assert first.calls[0].request.url.params["$top"] == str(USERS_PAGE_WITH_SIGN_IN)
    assert "signInActivity" in first.calls[0].request.url.params["$select"]
    dana, marcus, priya = users
    assert dana.account_enabled and dana.is_member and not dana.synced
    assert dana.last_sign_in == dt.datetime(2026, 10, 12, 6, 41, tzinfo=dt.UTC)
    assert marcus.synced and marcus.account_enabled is False and marcus.licence_skus == []
    assert priya.is_member is False and priya.last_sign_in is None


@respx.mock
async def test_the_page_cap_is_reported_rather_than_hidden(fast_limits: ConnectorLimits) -> None:
    a_token()
    forever = {
        "@odata.nextLink": f"{GRAPH_BASE_URL}/users?$skiptoken=ALWAYS",
        "value": [{"id": "x", "displayName": "X"}],
    }
    respx.get(f"{GRAPH_BASE_URL}/users").mock(return_value=httpx.Response(200, json=forever))
    async with make_client(limits=fast_limits) as client:
        users, truncated, _ = await client.list_users()
    assert truncated is True and len(users) == fast_limits.max_pages


@respx.mock
async def test_a_429_waits_the_retry_after_and_then_succeeds(
    fast_limits: ConnectorLimits,
) -> None:
    a_token()
    sleep = RecordingSleep()
    answers = [
        httpx.Response(
            429, headers={"Retry-After": "3"}, json={"error": {"code": "TooManyRequests"}}
        ),
        httpx.Response(200, json=load_fixture("microsoft365", "subscribed_skus.json")),
    ]
    respx.get(f"{GRAPH_BASE_URL}/subscribedSkus").mock(side_effect=answers)
    # A backoff ceiling above the header's value, so what is waited is the
    # header rather than our own curve. (The ceiling still caps it: a vendor
    # that asks for an hour does not get one.)
    patient = dataclasses.replace(fast_limits, backoff_max_seconds=30.0)
    async with make_client(limits=patient, sleep=sleep) as client:
        skus = await client.list_subscribed_skus()
    assert [s.name for s in skus] == ["ENTERPRISEPACK", "FLOW_FREE", "VISIOCLIENT"]
    assert sleep.delays == [3.0], "Retry-After is honoured, not the backoff curve"
    assert skus[2].free_units == 0, "a tenant over its count has no free seats, never a negative"
    assert skus[0].free_units == 7 and skus[0].enabled_units == 25


@respx.mock
async def test_a_429_that_never_clears_is_raised(fast_limits: ConnectorLimits) -> None:
    a_token()
    sleep = RecordingSleep()
    respx.get(f"{GRAPH_BASE_URL}/subscribedSkus").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )
    async with make_client(limits=fast_limits, sleep=sleep) as client:
        with pytest.raises(RateLimited):
            await client.list_subscribed_skus()
    assert len(sleep.delays) == fast_limits.max_retries


# --- a tenant without a premium licence ------------------------------------------------


PREMIUM_REFUSAL = {
    "error": {
        "code": "Authentication_RequestFromUnsupportedUserRole",
        "message": "Neither tenant is B2C or tenant doesn't have premium license",
    }
}


@respx.mock
async def test_a_tenant_without_a_premium_licence_still_gets_its_users(
    fast_limits: ConnectorLimits,
) -> None:
    """The sign-in read is refused for the tenant's own licensing. The rest of
    every user is read, and what is missing is said in words — never a zero."""
    a_token()
    without = {
        "value": [
            row | {"signInActivity": None}
            for row in load_fixture("microsoft365", "users_page1.json")["value"]
        ]
    }

    def answer(request: httpx.Request) -> httpx.Response:
        if "signInActivity" in request.url.params.get("$select", ""):
            return httpx.Response(403, json=PREMIUM_REFUSAL)
        return httpx.Response(200, json=without)

    respx.get(f"{GRAPH_BASE_URL}/users").mock(side_effect=answer)
    async with make_client(limits=fast_limits) as client:
        users, _, missing = await client.list_users()
    assert len(users) == 2 and all(u.last_sign_in is None for u in users)
    assert missing == "this tenant has no Microsoft Entra ID P1 or P2 licence"


@respx.mock
async def test_the_premium_refusal_is_its_own_error_not_an_auth_failure(
    fast_limits: ConnectorLimits,
) -> None:
    a_token()
    respx.get(f"{GRAPH_BASE_URL}/reports/authenticationMethods/userRegistrationDetails").mock(
        return_value=httpx.Response(403, json=PREMIUM_REFUSAL)
    )
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(NotLicensed, match="Entra ID licence"):
            await client.list_registration_details()


@respx.mock
async def test_a_permission_that_was_not_granted_reads_as_not_available(
    fast_limits: ConnectorLimits,
) -> None:
    """A tenant that consented to less than we asked for refuses with 403.
    For an item that is not the point of the connector that is reported as
    not available, with the permission named."""
    a_token()
    respx.get(f"{GRAPH_BASE_URL}/users/u1/mailboxSettings/userPurpose").mock(
        return_value=httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}})
    )
    respx.get(f"{GRAPH_BASE_URL}/users/u1/manager").mock(
        return_value=httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}})
    )
    async with make_client(limits=fast_limits) as client:
        with pytest.raises(NotAvailable, match=r"MailboxSettings\.Read"):
            await client.mailbox_purpose("u1")
        with pytest.raises(NotAvailable, match="manager"):
            await client.get_manager("u1")


# --- the reads ---------------------------------------------------------------------------


@respx.mock
async def test_mfa_registration_says_who_registered_what(fast_limits: ConnectorLimits) -> None:
    a_token()
    respx.get(f"{GRAPH_BASE_URL}/reports/authenticationMethods/userRegistrationDetails").mock(
        return_value=httpx.Response(
            200, json=load_fixture("microsoft365", "registration_details.json")
        )
    )
    async with make_client(limits=fast_limits) as client:
        rows, truncated = await client.list_registration_details()
    assert truncated is False
    dana, marcus, priya = rows
    assert dana.registered and dana.strong_methods == ["microsoftAuthenticatorPush"]
    assert not marcus.registered and marcus.methods_registered == []
    # Registered something, but nothing that makes the account MFA-capable.
    assert priya.is_mfa_registered and not priya.registered and priya.strong_methods == []


@respx.mock
async def test_groups_are_sorted_into_their_five_kinds_with_member_counts(
    fast_limits: ConnectorLimits,
) -> None:
    a_token()
    respx.get(f"{GRAPH_BASE_URL}/groups").mock(
        return_value=httpx.Response(200, json=load_fixture("microsoft365", "groups.json"))
    )
    counts = {"g1": 12, "g2": 24, "g3": 9, "g4": 4, "g5": 6}

    def count(request: httpx.Request) -> httpx.Response:
        key = str(request.url).split("/groups/")[1][:2]
        return httpx.Response(200, text=str(counts[key]), headers={"Content-Type": "text/plain"})

    counted = respx.get(url__regex=rf"{GRAPH_BASE_URL}/groups/.+/members/\$count").mock(
        side_effect=count
    )
    async with make_client(limits=fast_limits) as client:
        groups, _ = await client.list_groups()
    assert [g.kind for g in groups] == [
        GROUP_TEAM,
        GROUP_DISTRIBUTION,
        GROUP_SECURITY,
        GROUP_MAIL_SECURITY,
        GROUP_MICROSOFT365,
    ]
    assert [g.member_count for g in groups] == [12, 24, 9, 4, 6]
    # The count is its own documented read and needs the eventual header.
    assert counted.calls[0].request.headers["ConsistencyLevel"] == "eventual"


@respx.mock
async def test_a_member_count_the_tenant_will_not_give_is_none_not_zero(
    fast_limits: ConnectorLimits,
) -> None:
    a_token()
    respx.get(f"{GRAPH_BASE_URL}/groups").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "g1", "displayName": "X", "securityEnabled": True}]}
        )
    )
    respx.get(url__regex=rf"{GRAPH_BASE_URL}/groups/.+/members/\$count").mock(
        return_value=httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}})
    )
    async with make_client(limits=fast_limits) as client:
        groups, _ = await client.list_groups()
    assert groups[0].member_count is None


@respx.mock
async def test_one_user_is_found_by_either_address(fast_limits: ConnectorLimits) -> None:
    a_token()
    page = load_fixture("microsoft365", "users_page1.json")
    found = respx.get(f"{GRAPH_BASE_URL}/users").mock(
        return_value=httpx.Response(200, json={"value": page["value"][:1]})
    )
    async with make_client(limits=fast_limits) as client:
        user = await client.get_user("Dana.Whitfield@Contoso.example")
    assert user is not None and user.id == "11111111-1111-1111-1111-111111111111"
    assert "dana.whitfield@contoso.example" in user.addresses
    sent = found.calls[0].request.url.params["$filter"]
    assert "userPrincipalName eq" in sent and "mail eq" in sent


@respx.mock
async def test_a_user_who_is_not_in_the_tenant_is_none(fast_limits: ConnectorLimits) -> None:
    a_token()
    respx.get(f"{GRAPH_BASE_URL}/users").mock(return_value=httpx.Response(200, json={"value": []}))
    async with make_client(limits=fast_limits) as client:
        assert await client.get_user("nobody@contoso.example") is None


@respx.mock
async def test_the_mailbox_report_is_followed_without_our_token(
    fast_limits: ConnectorLimits,
) -> None:
    """Graph answers the report with a redirect to a pre-authenticated URL.
    Our bearer token must not follow it onto a storage host."""
    a_token()
    csv = (
        "Report Refresh Date,User Principal Name,Display Name,Is Deleted,Deleted Date,"
        "Created Date,Last Activity Date,Item Count,Storage Used (Byte),"
        "Issue Warning Quota (Byte)\n"
        "2026-10-13,dana.whitfield@contoso.example,Dana Whitfield,False,,2024-02-11,"
        "2026-10-12,8421,9663676416,98784247808\n"
        "2026-10-13,5C7B2A,5C7B2A,False,,2021-06-02,2026-09-01,120,104857600,98784247808\n"
    )
    respx.get(url__startswith=f"{GRAPH_BASE_URL}/reports/getMailboxUsageDetail").mock(
        return_value=httpx.Response(302, headers={"Location": "https://storage.test.invalid/r.csv"})
    )
    download = respx.get("https://storage.test.invalid/r.csv").mock(
        return_value=httpx.Response(200, text=csv)
    )
    async with make_client(limits=fast_limits) as client:
        rows = await client.mailbox_usage()
    assert "authorization" not in {k.lower() for k in download.calls[0].request.headers}
    assert len(rows) == 2
    assert rows[0].storage_used_bytes == 9663676416 and not rows[0].concealed
    assert rows[1].concealed, "a concealed row cannot be joined to a person"


def test_the_usage_csv_is_read_by_column_name() -> None:
    """Microsoft has moved these columns; a report read by position would
    quietly report the wrong number."""
    csv = (
        "Display Name,User Principal Name,Item Count,Storage Used (Byte),Is Deleted,"
        "Last Activity Date\n"
        "Dana,dana@contoso.example,10,2048,False,2026-10-01\n"
    )
    (row,) = parse_mailbox_usage(csv)
    assert row.user_principal_name == "dana@contoso.example"
    assert row.storage_used_bytes == 2048 and row.item_count == 10
    assert row.last_activity == dt.date(2026, 10, 1) and row.deleted is False


@respx.mock
async def test_a_tenant_that_conceals_report_names_says_so(fast_limits: ConnectorLimits) -> None:
    a_token()
    respx.get(f"{GRAPH_BASE_URL}/admin/reportSettings").mock(
        return_value=httpx.Response(200, json={"displayConcealedNames": True})
    )
    async with make_client(limits=fast_limits) as client:
        assert await client.report_settings() is True


@respx.mock
async def test_microsoft365_has_no_site_discovery(fast_limits: ConnectorLimits) -> None:
    """A tenant's token cannot enumerate other tenants, so a tenant id is
    mapped onto a site by a person rather than discovered."""
    from app.connectors.errors import ConnectorNotConfigured

    async with make_client(limits=fast_limits) as client:
        with pytest.raises(ConnectorNotConfigured, match="consented separately"):
            await client.list_organisations()

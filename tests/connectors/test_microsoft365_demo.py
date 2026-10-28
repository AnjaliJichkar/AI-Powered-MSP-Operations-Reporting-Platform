"""The connector against the demo tenant: the profile, the account, the tools.

Everything here runs the real `Microsoft365Client` over the fake Graph, with
no network and no database beyond the demo world itself. It is where the
reads are proven end to end — the paging, the per-tenant tokens, the premium
fallback, the permission a tenant withheld — on data that is a whole tenant
rather than a fixture of three rows.
"""

from __future__ import annotations

import uuid

import pytest

from app.connectors.microsoft365.client import Microsoft365Client, ReadOnlyViolation
from app.connectors.microsoft365.reads import account_for, profile_for
from app.connectors.scope import ClientScope, SiteRef
from app.demo.connectors import DemoMicrosoft365Client, demo_world
from app.demo.dataset import Customer, World
from app.models.enums import ConnectorName

SLUG = "tailwind-it-demo"


@pytest.fixture(scope="module")
def world() -> World:
    return demo_world(SLUG)


def client_for(world: World, customer: Customer) -> DemoMicrosoft365Client:
    site = SiteRef(
        site_id=uuid.uuid4(),
        slug=customer.key,
        name=customer.name,
        external_id=customer.m365_tenant_id,
    )
    scope = ClientScope(
        client_id=uuid.uuid4(),
        client_slug=SLUG,
        connector=ConnectorName.MICROSOFT365,
        allowed=(site,),
        targets=(site,),
    )
    return DemoMicrosoft365Client(scope=scope, world=world)


def premium(world: World) -> Customer:
    return next(c for c in world.customers if c.m365_premium)


def without_premium(world: World) -> Customer:
    return next(c for c in world.customers if not c.m365_premium)


# --- the profile ------------------------------------------------------------------


async def test_the_profile_counts_what_the_tenant_is(world: World) -> None:
    customer = premium(world)
    async with client_for(world, customer) as connector:
        profile = await profile_for(connector)

    accounts = customer.m365_accounts
    assert profile.tenant_id == customer.m365_tenant_id
    assert profile.users_total == len(accounts)
    assert profile.guests == sum(1 for a in accounts if a.user_type == "Guest")
    assert profile.disabled == sum(1 for a in accounts if not a.enabled)
    assert profile.synced == sum(1 for a in accounts if a.synced)
    assert profile.cloud_only + profile.synced == profile.users_total
    assert profile.enabled + profile.disabled == profile.users_total
    assert profile.unavailable == [], "a tenant with every permission is missing nothing"
    # Counts and ids are what may be stored; no name is in the stored shape.
    stored = profile.as_dict()
    text = repr(stored)
    for account in accounts:
        assert account.display_name not in text
        assert account.upn not in text


async def test_the_profile_separates_cloud_only_from_synced(world: World) -> None:
    """Both kinds exist in the demo, so the line that says which is which has
    something to say on at least one tenant."""
    totals = []
    for customer in world.customers[:3]:
        async with client_for(world, customer) as connector:
            profile = await profile_for(connector)
        totals.append((profile.cloud_only, profile.synced))
    assert any(synced for _, synced in totals), "no tenant is synced from on-premises AD"
    assert any(not synced for _, synced in totals), "every tenant is synced"


async def test_licences_say_assigned_and_free(world: World) -> None:
    async with client_for(world, premium(world)) as connector:
        profile = await profile_for(connector)
    assert profile.licences
    for line in profile.licences:
        assert line.free == max(line.total - line.assigned, 0)
        assert line.assigned >= 0 and line.total >= line.assigned


async def test_mfa_is_counted_over_people_not_shared_mailboxes(world: World) -> None:
    """A shared mailbox and a room are enabled member accounts in Entra and
    cannot register a second factor. Counting them as missing one would make
    the gap read larger than it is."""
    customer = premium(world)
    async with client_for(world, customer) as connector:
        profile = await profile_for(connector)
    shared = [a for a in customer.m365_accounts if a.mailbox_purpose in ("shared", "room")]
    assert shared, "the demo has no shared mailboxes to leave out"
    assert profile.mfa_not_people == len(shared)
    for account in shared:
        assert account.upn not in profile.mfa_missing_names
    # The people who genuinely have none are listed.
    people_without = {
        a.upn
        for a in customer.m365_accounts
        if a.person_id is not None and a.enabled and not a.mfa_methods
    }
    assert people_without <= set(profile.mfa_missing_names)
    assert profile.mfa_registered + profile.mfa_missing == profile.mfa_population


async def test_a_phone_alone_is_registered_but_not_capable(world: World) -> None:
    """Graph reports a phone-only account as registered and not MFA-capable.
    Capable is what the tenant's own screen shows, so it is what we count."""
    customer = premium(world)
    phone_only = [
        a
        for a in customer.m365_accounts
        if a.person_id is not None and a.enabled and a.mfa_methods == ["mobilePhone"]
    ]
    if not phone_only:
        pytest.skip("this tenant has nobody registered with a phone alone")
    async with client_for(world, customer) as connector:
        profile = await profile_for(connector)
    for account in phone_only:
        assert account.upn in profile.mfa_missing_names


async def test_groups_are_counted_by_kind_with_members(world: World) -> None:
    async with client_for(world, premium(world)) as connector:
        profile = await profile_for(connector)
    assert profile.group_counts["Team"] >= 1
    assert profile.group_counts["distribution list"] >= 1
    assert profile.group_counts["security group"] >= 1
    assert all(g.members is not None and g.members > 0 for g in profile.groups)


async def test_departments_titles_and_offices_are_tallied(world: World) -> None:
    """What a role matrix is drafted from: what the tenant already says."""
    async with client_for(world, premium(world)) as connector:
        profile = await profile_for(connector)
    for rows in (profile.departments, profile.job_titles, profile.offices):
        assert rows, "nothing to draft a role matrix from"
        assert rows == sorted(rows, key=lambda item: (-item[1], item[0]))
        assert all(count > 0 and value.strip() for value, count in rows)


async def test_a_tenant_without_a_premium_licence_says_so_and_still_profiles(
    world: World,
) -> None:
    customer = without_premium(world)
    async with client_for(world, customer) as connector:
        profile = await profile_for(connector)
    assert profile.users_total == len(customer.m365_accounts)
    assert profile.licences and profile.groups
    reasons = " ".join(u.line for u in profile.unavailable)
    assert "last sign-in" in reasons and "P1 or P2" in reasons
    assert profile.mfa_population > 0, "MFA is readable without a premium licence"


async def test_a_tenant_that_conceals_its_reports_joins_no_mailbox_to_a_person(
    world: World,
) -> None:
    concealed = world.customers[1]
    async with client_for(world, concealed) as connector:
        profile = await profile_for(connector)
    assert profile.mailboxes["with a size"] == 0
    assert profile.mailboxes["name concealed"] > 0
    assert any("conceals the names" in u.reason for u in profile.unavailable)
    assert profile.mailbox_sizes == []


async def test_a_permission_the_tenant_withheld_is_named_not_swallowed(world: World) -> None:
    """One demo tenant did not consent to MailboxSettings.Read. The profile
    says so, and says what it costs: shared mailboxes are then counted as
    missing MFA."""
    withheld = world.customers[2]
    async with client_for(world, withheld) as connector:
        profile = await profile_for(connector)
    reasons = " ".join(u.line for u in profile.unavailable)
    assert "MailboxSettings.Read" in reasons
    assert "may read larger than it is" in reasons
    assert profile.mfa_not_people == 0


# --- one account --------------------------------------------------------------------


async def test_an_account_is_read_by_either_address(world: World) -> None:
    customer = premium(world)
    person = next(a for a in customer.m365_accounts if a.person_id is not None and a.enabled)
    async with client_for(world, customer) as connector:
        account = await account_for(connector, person.upn.upper())
    assert account.found and account.address == person.upn
    assert account.display_name == person.display_name
    assert account.enabled is True
    assert account.licences and account.groups
    assert account.mfa_registered is not None
    assert account.last_sign_in is not None and account.days_since_sign_in is not None


async def test_a_disabled_account_says_its_mfa_cannot_be_read(world: World) -> None:
    """Graph's registration report leaves out disabled accounts, so their
    second factor is unknown rather than absent."""
    customer = premium(world)
    disabled = next(a for a in customer.m365_accounts if not a.enabled)
    async with client_for(world, customer) as connector:
        account = await account_for(connector, disabled.upn)
    assert account.found and account.enabled is False
    assert account.mfa_registered is None
    assert any("disabled accounts" in u.reason for u in account.unavailable)


async def test_a_shared_mailbox_is_not_an_unlicensed_member(world: World) -> None:
    customer = premium(world)
    shared = next(a for a in customer.m365_accounts if a.mailbox_purpose == "shared")
    async with client_for(world, customer) as connector:
        account = await account_for(connector, shared.upn)
    assert account.found and account.mailbox_kind == "shared"
    assert account.licences == []
    assert not account.unlicensed_member, "a shared mailbox with no licence is correct"


async def test_an_address_that_is_not_in_the_tenant_is_an_answer(world: World) -> None:
    async with client_for(world, premium(world)) as connector:
        account = await account_for(connector, "nobody@example.invalid")
    assert account.found is False and account.unavailable == []


# --- scope and the guard ---------------------------------------------------------------


async def test_each_tenant_is_read_with_its_own_token(world: World) -> None:
    """A Graph token is for one directory. The client holds one per tenant and
    sends each tenant's own: if it ever sent the first tenant's token to the
    second, two customers' people would end up in one list — which the fake
    refuses, as a real tenant would."""
    first, second = world.customers[0], world.customers[1]
    site_of = lambda c: SiteRef(  # noqa: E731
        site_id=uuid.uuid4(), slug=c.key, name=c.name, external_id=c.m365_tenant_id
    )
    sites = (site_of(first), site_of(second))
    scope = ClientScope(
        client_id=uuid.uuid4(),
        client_slug=SLUG,
        connector=ConnectorName.MICROSOFT365,
        allowed=sites,
        targets=sites,
    )
    async with DemoMicrosoft365Client(scope=scope, world=world) as connector:
        # The fake is shared across this module, so only this call's requests
        # are looked at.
        already = len(connector.api.router.requests)
        found = await connector.read_each_tenant(connector.list_subscribed_skus)
        sent = [
            (r.url.path, r.headers.get("authorization"))
            for r in connector.api.router.requests[already:]
            if r.url.path.endswith("/subscribedSkus")
        ]
    assert [s.slug for s, _ in found] == [first.key, second.key]
    assert sent == [
        ("/v1.0/subscribedSkus", f"Bearer demo-token-{first.m365_tenant_id}"),
        ("/v1.0/subscribedSkus", f"Bearer demo-token-{second.m365_tenant_id}"),
    ]


def test_a_token_minted_for_one_tenant_is_refused_by_another(world: World) -> None:
    """The fake enforces what a real tenant does, so the rule above is proven
    rather than assumed: a token for one directory does not open another."""
    import httpx

    from app.demo.microsoft365_api import DEMO_BASE_URL, Microsoft365Api

    api = Microsoft365Api(world)
    first, second = world.customers[0], world.customers[1]
    for tenant, expected in (
        (first.m365_tenant_id, 200),
        (second.m365_tenant_id, 200),
        ("00000000-0000-0000-0000-000000000000", 401),
    ):
        response = api.handle(
            httpx.Request(
                "GET",
                f"{DEMO_BASE_URL}/subscribedSkus",
                headers={"Authorization": f"Bearer demo-token-{tenant}"},
            )
        )
        assert response.status_code == expected, tenant

    # And each tenant's token answers from its own directory, never the other's.
    def skus(tenant: str) -> set[str]:
        body = api.handle(
            httpx.Request(
                "GET",
                f"{DEMO_BASE_URL}/subscribedSkus",
                headers={"Authorization": f"Bearer demo-token-{tenant}"},
            )
        ).json()
        return {row["id"] for row in body["value"]}

    assert skus(first.m365_tenant_id).isdisjoint(skus(second.m365_tenant_id))


async def test_the_demo_client_is_the_real_client_and_still_read_only(world: World) -> None:
    async with client_for(world, premium(world)) as connector:
        assert isinstance(connector, Microsoft365Client)
        with pytest.raises(ReadOnlyViolation):
            await connector.request("PATCH", "/users/u-1")
        with pytest.raises(ReadOnlyViolation):
            await connector.request("DELETE", "/groups/g-1")

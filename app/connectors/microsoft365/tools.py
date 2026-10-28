"""The Microsoft 365 tools the model can call.

Each docstring is what the model reads when it chooses a tool. Every one is
read-only (hard rule 2, enforced by the client's method guard), takes
`client_id` (hard rule 1) and an optional `site_id` the loop resolves from a
slug, and reports `truncated` honestly (hard rule 7).

Microsoft 365 is scoped by **site**, not by client: each end customer has
their own Entra tenant, consented separately, and its tenant id is that site's
mapping. So every tool here takes a site, and a client with more than one
tenant mapped is asked which — answering "all of them" would merge two
companies' directories into one list.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agent.registry import tool
from app.connectors.factory import connector_for
from app.connectors.microsoft365.client import Microsoft365Client
from app.connectors.microsoft365.reads import account_for, profile_for
from app.connectors.results import RowCapped, capped
from app.models.enums import ConnectorName


class NotAvailableRow(BaseModel):
    item: str = Field(description="What could not be read for this tenant.")
    reason: str = Field(description="Why. Repeat it; it is not a zero and not a failure.")


class UserWithoutMfa(BaseModel):
    user: str = Field(description="The account's sign-in address.")
    enabled: bool = Field(description="Whether the account can sign in.")
    licensed: bool = Field(description="Whether it has any licence assigned.")
    last_sign_in: str | None = Field(
        default=None, description="When it last signed in, or null where that cannot be read."
    )


class UsersWithoutMfaList(RowCapped):
    tenant: str = Field(description="The Entra tenant id these accounts are in.")
    site: str | None = Field(default=None, description="The end customer, where one is mapped.")
    population: int = Field(
        description="Enabled member accounts, which is what the share is out of. Guests and "
        "disabled accounts are not counted: a disabled account has no row in Microsoft's "
        "registration report at all."
    )
    registered: int = Field(description="How many of the population have registered a method.")
    users: list[UserWithoutMfa]
    not_available: list[NotAvailableRow] = Field(default_factory=list)


@tool(ConnectorName.MICROSOFT365)
async def list_users_without_mfa(client_id: str, site_id: str | None = None) -> UsersWithoutMfaList:
    """List Microsoft 365 accounts with no multi-factor authentication registered.

    Use this for "who has no MFA", "which accounts are not protected by MFA",
    "what is our MFA coverage in Microsoft 365", "who still needs to set up
    the authenticator". Counted over **enabled member accounts**: a guest is
    somebody else's identity and a disabled account is not in Microsoft's
    registration report at all, so neither is counted — say so if asked why a
    number looks small.

    Pass `site` to name the end customer whose tenant to read; each customer
    has their own Microsoft 365 tenant, so a site is usually required. If
    `not_available` has entries, read them out: they are items this tenant's
    licensing or consent would not give, and they are not zeros.

    Not for Entra sign-in logs or conditional access policies, which this
    connector does not read. Not for a single person's account: use
    `get_microsoft365_account`.
    """
    async with connector_for(client_id, ConnectorName.MICROSOFT365, site_id) as connector:
        assert isinstance(connector, Microsoft365Client)
        profile = await profile_for(connector)
    rows = [
        UserWithoutMfa(user=name, enabled=True, licensed=True, last_sign_in=None)
        for name in profile.mfa_missing_names
    ]
    kept, counts = capped("list_users_without_mfa", rows, noun="accounts")
    return UsersWithoutMfaList(
        tenant=profile.tenant_id,
        site=profile.site_slug,
        population=profile.mfa_population,
        registered=profile.mfa_registered,
        users=kept,
        not_available=[NotAvailableRow(**u.as_dict()) for u in profile.unavailable],
        **counts,
    )


class AccountState(BaseModel):
    found: bool = Field(description="False when no account in this tenant has that address.")
    user: str | None = None
    display_name: str | None = None
    enabled: bool | None = Field(default=None, description="Whether the account can sign in.")
    account_type: str | None = Field(default=None, description="Member or Guest.")
    synced_from_active_directory: bool | None = Field(
        default=None,
        description="True when the account is synced from an on-premises Active Directory, so it "
        "is managed there and not in the cloud; false when it is cloud-only.",
    )
    created: str | None = None
    last_sign_in: str | None = Field(
        default=None, description="Null where the tenant has no licence to report it."
    )
    days_since_sign_in: int | None = None
    mfa_registered: bool | None = Field(
        default=None, description="Null where it could not be read, which is not 'no'."
    )
    mfa_methods: list[str] = Field(default_factory=list)
    licences: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    department: str | None = None
    job_title: str | None = None
    office_location: str | None = None
    manager: str | None = None
    mailbox_kind: str | None = Field(
        default=None, description="user, shared, room, equipment, or null where not readable."
    )
    unlicensed_member: bool = Field(
        default=False,
        description="True for an enabled account for a person with no licence assigned at all. "
        "A guest with none is ordinary and a shared mailbox with none is correct, so neither is "
        "reported this way.",
    )
    not_available: list[NotAvailableRow] = Field(default_factory=list)
    read_at: str | None = None


@tool(ConnectorName.MICROSOFT365)
async def get_microsoft365_account(
    client_id: str, user: str, site_id: str | None = None
) -> AccountState:
    """One person's Microsoft 365 account: enabled, MFA, licences, groups, last sign-in.

    Use this for "is this person's account enabled", "does X have MFA", "what
    licences does X have", "when did X last sign in", "what groups is X in",
    "is X synced from Active Directory". `user` is the person's email address
    or sign-in name. Pass `site` to name the end customer whose tenant to look
    in.

    `found: false` means no account in that tenant has that address — which is
    an answer, not an error. A null `mfa_registered` or `last_sign_in` means it
    could not be read for this tenant, and `not_available` says why; null is
    never "no".

    Not for the person's devices (`get_device`) or their tickets
    (`search_tickets`). Not for changing anything: this connector only reads.
    """
    async with connector_for(client_id, ConnectorName.MICROSOFT365, site_id) as connector:
        assert isinstance(connector, Microsoft365Client)
        account = await account_for(connector, user)
    return AccountState(
        found=account.found,
        user=account.address,
        display_name=account.display_name,
        enabled=account.enabled,
        account_type=account.user_type,
        synced_from_active_directory=account.synced,
        created=_iso(account.created),
        last_sign_in=_iso(account.last_sign_in),
        days_since_sign_in=account.days_since_sign_in,
        mfa_registered=account.mfa_registered,
        mfa_methods=account.mfa_methods,
        licences=account.licences,
        groups=account.groups,
        department=account.department,
        job_title=account.job_title,
        office_location=account.office_location,
        manager=account.manager,
        mailbox_kind=account.mailbox_kind,
        unlicensed_member=account.unlicensed_member,
        not_available=[NotAvailableRow(**u.as_dict()) for u in account.unavailable],
        read_at=_iso(account.read_at),
    )


class LicenceRow(BaseModel):
    licence: str = Field(description="Microsoft's name for the subscription.")
    assigned: int = Field(description="Seats assigned to accounts.")
    total: int = Field(description="Seats bought.")
    free: int = Field(description="Seats bought and not assigned. Never negative.")


class LicenceSummary(BaseModel):
    tenant: str
    site: str | None = None
    licences: list[LicenceRow]
    assigned_total: int = Field(description="Every assigned seat, across subscriptions.")
    free_total: int = Field(description="Every free seat, across subscriptions.")
    not_available: list[NotAvailableRow] = Field(default_factory=list)


@tool(ConnectorName.MICROSOFT365)
async def microsoft365_licence_summary(
    client_id: str, site_id: str | None = None
) -> LicenceSummary:
    """Microsoft 365 subscriptions with how many seats are assigned and how many are free.

    Use this for "what licences does this customer have", "how many spare
    Microsoft 365 licences are there", "are we over-licensed", "what is
    assigned". Pass `site` to name the end customer.

    Seats bought are `total` and seats in use are `assigned`; `free` is the
    difference and is never negative, because a tenant in its grace period can
    be over its count. Names are Microsoft's own part numbers (ENTERPRISEPACK
    is Office 365 E3); do not invent friendlier names.

    Not for who has which licence — that is `get_microsoft365_account` for one
    person. Not for cost: this connector reads no pricing.
    """
    async with connector_for(client_id, ConnectorName.MICROSOFT365, site_id) as connector:
        assert isinstance(connector, Microsoft365Client)
        profile = await profile_for(connector)
    rows = [
        LicenceRow(licence=line.name, assigned=line.assigned, total=line.total, free=line.free)
        for line in profile.licences
    ]
    return LicenceSummary(
        tenant=profile.tenant_id,
        site=profile.site_slug,
        licences=rows,
        assigned_total=sum(r.assigned for r in rows),
        free_total=sum(r.free for r in rows),
        not_available=[NotAvailableRow(**u.as_dict()) for u in profile.unavailable],
    )


class GroupMember(BaseModel):
    user: str = Field(description="The member's sign-in address.")
    display_name: str | None = None
    enabled: bool | None = None


class GroupMembers(RowCapped):
    group: str
    kind: str = Field(description="Team, Microsoft 365 group, distribution list or security group.")
    members: list[GroupMember]


@tool(ConnectorName.MICROSOFT365)
async def list_microsoft365_group_members(
    client_id: str, group: str, site_id: str | None = None
) -> GroupMembers:
    """List the people in one Microsoft 365 group, Team or distribution list.

    Use this for "who is in the finance team", "who is on the all-staff
    distribution list", "who is in that security group". `group` is the
    group's name as it is in Microsoft 365. Pass `site` to name the end
    customer.

    Only people are listed: a nested group or a device in the group is not a
    person and is left out rather than counted as one.

    Not for a person's own groups — that is `list_microsoft365_user_groups`.
    Not for Autotask contacts or NinjaOne device groups.
    """
    async with connector_for(client_id, ConnectorName.MICROSOFT365, site_id) as connector:
        assert isinstance(connector, Microsoft365Client)
        groups, _ = await connector.list_groups(with_counts=False)
        wanted = group.strip().casefold()
        found = next(
            (g for g in groups if (g.display_name or "").strip().casefold() == wanted),
            None,
        ) or next(
            (g for g in groups if wanted in (g.display_name or "").strip().casefold()),
            None,
        )
        if found is None:
            return GroupMembers(group=group, kind="not found", members=[], total=0, returned=0)
        members, page_truncated = await connector.list_group_members(found.id)
    rows = [
        GroupMember(
            user=(m.mail or m.user_principal_name or m.id),
            display_name=m.display_name,
            enabled=m.account_enabled,
        )
        for m in members
    ]
    kept, counts = capped(
        "list_microsoft365_group_members", rows, noun="members", page_truncated=page_truncated
    )
    return GroupMembers(group=found.display_name or group, kind=found.kind, members=kept, **counts)


class UserGroup(BaseModel):
    group: str
    kind: str


class UserGroups(RowCapped):
    user: str
    groups: list[UserGroup]


@tool(ConnectorName.MICROSOFT365)
async def list_microsoft365_user_groups(
    client_id: str, user: str, site_id: str | None = None
) -> UserGroups:
    """List the Microsoft 365 groups, Teams and distribution lists one person is in.

    Use this for "what groups is X in", "what Teams does X belong to", "is X
    on the all-staff list", "what access does X have through groups". `user` is
    the person's email address or sign-in name. Pass `site` to name the end
    customer. Nested memberships are included, so a group somebody is in
    through another group is listed.

    Not for the members of a group — that is
    `list_microsoft365_group_members`. Not for Entra roles or licences;
    licences are on `get_microsoft365_account`.
    """
    async with connector_for(client_id, ConnectorName.MICROSOFT365, site_id) as connector:
        assert isinstance(connector, Microsoft365Client)
        found = await connector.get_user(user)
        if found is None:
            return UserGroups(user=user, groups=[], total=0, returned=0)
        groups, page_truncated = await connector.list_user_groups(found.id)
    rows = [UserGroup(group=g.display_name or g.id, kind=g.kind) for g in groups]
    kept, counts = capped(
        "list_microsoft365_user_groups", rows, noun="groups", page_truncated=page_truncated
    )
    return UserGroups(user=found.mail or found.user_principal_name or user, groups=kept, **counts)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "get_microsoft365_account",
    "list_microsoft365_group_members",
    "list_microsoft365_user_groups",
    "list_users_without_mfa",
    "microsoft365_licence_summary",
]

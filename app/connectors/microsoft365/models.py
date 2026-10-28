"""The Microsoft Graph responses we actually read, and nothing more.

Only the properties the features in `docs/m365-connector.md` need are
modelled; Graph returns a great deal more and none of it is parsed, stored or
logged. Every model is tolerant of a missing property, because what Graph
returns depends on the permissions granted and the tenant's licensing: a
tenant without Entra ID P1 returns no `signInActivity` at all, and that is
reported as *not available for this tenant*, never as a zero or an error.

**Names and addresses live here and go no further.** A user's display name,
user principal name and mail are on `GraphUser` because a page and an Ask
answer need them; nothing that is stored or logged carries them (CLAUDE.md,
working conventions).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: `userType` as Entra reports it.
MEMBER = "Member"
GUEST = "Guest"

#: The `methodsRegistered` values the registration report uses, mapped to
#: words a person reads. Graph's list is open: anything not here is reported
#: by its own name rather than dropped, so a new method is visible the day
#: Microsoft adds it.
METHOD_WORDS: dict[str, str] = {
    "microsoftAuthenticatorPush": "Microsoft Authenticator",
    "softwareOneTimePasscode": "authenticator app code",
    "mobilePhone": "text message",
    "alternateMobilePhone": "text message (alternate)",
    "officePhone": "phone call",
    "email": "email",
    "fido2SecurityKey": "security key",
    "windowsHelloForBusiness": "Windows Hello for Business",
    "passKeyDeviceBound": "passkey",
    "passKeyDeviceBoundAuthenticator": "passkey (authenticator)",
    "passKeyDeviceBoundWindowsHello": "passkey (Windows Hello)",
    "temporaryAccessPass": "temporary access pass",
    "macOsSecureEnclaveKey": "device key",
    "hardwareOneTimePasscode": "hardware token",
}

#: The methods that are a strong second factor. A password is not one, and
#: neither is an email or an SMS code on their own — but what the tenant
#: counts as MFA is the tenant's policy, so `isMfaRegistered` from the report
#: is what we report, and this list only says which *kinds* were registered.
STRONG_METHODS = frozenset(
    {
        "microsoftAuthenticatorPush",
        "softwareOneTimePasscode",
        "fido2SecurityKey",
        "windowsHelloForBusiness",
        "passKeyDeviceBound",
        "passKeyDeviceBoundAuthenticator",
        "passKeyDeviceBoundWindowsHello",
        "hardwareOneTimePasscode",
        "macOsSecureEnclaveKey",
    }
)


def method_words(methods: list[str]) -> list[str]:
    """Registered methods in words, in the order Graph listed them."""
    return [METHOD_WORDS.get(method, method) for method in methods]


class GraphModel(BaseModel):
    """Ignores every property we did not ask for, so a new Graph field is not
    a parse error on a Tuesday."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AssignedLicence(GraphModel):
    sku_id: str | None = Field(default=None, alias="skuId")


class SignInActivity(GraphModel):
    last_sign_in: dt.datetime | None = Field(default=None, alias="lastSignInDateTime")
    last_non_interactive_sign_in: dt.datetime | None = Field(
        default=None, alias="lastNonInteractiveSignInDateTime"
    )

    @property
    def most_recent(self) -> dt.datetime | None:
        """The later of the two, because a sign-in is a sign-in: a mailbox
        client refreshing a token is non-interactive and still says the
        account is in use."""
        both = [t for t in (self.last_sign_in, self.last_non_interactive_sign_in) if t is not None]
        return max(both) if both else None


class GraphUser(GraphModel):
    id: str
    display_name: str | None = Field(default=None, alias="displayName")
    user_principal_name: str | None = Field(default=None, alias="userPrincipalName")
    mail: str | None = None
    account_enabled: bool | None = Field(default=None, alias="accountEnabled")
    created: dt.datetime | None = Field(default=None, alias="createdDateTime")
    user_type: str | None = Field(default=None, alias="userType")
    on_premises_sync_enabled: bool | None = Field(default=None, alias="onPremisesSyncEnabled")
    department: str | None = None
    job_title: str | None = Field(default=None, alias="jobTitle")
    office_location: str | None = Field(default=None, alias="officeLocation")
    assigned_licences: list[AssignedLicence] = Field(default_factory=list, alias="assignedLicenses")
    sign_in_activity: SignInActivity | None = Field(default=None, alias="signInActivity")

    @property
    def addresses(self) -> list[str]:
        """Every address this user is known by, lower case: what a contact is
        matched against."""
        found = [a.lower() for a in (self.mail, self.user_principal_name) if a]
        return list(dict.fromkeys(found))

    @property
    def is_member(self) -> bool:
        """A member rather than a guest. `userType` is absent on some
        directory-synced accounts; absent is treated as a member, which is
        what Entra's own console shows."""
        return (self.user_type or MEMBER).casefold() == MEMBER.casefold()

    @property
    def synced(self) -> bool:
        """Synced from on-premises Active Directory. Absent means cloud-only:
        Graph sets the property only on synced objects."""
        return bool(self.on_premises_sync_enabled)

    @property
    def licence_skus(self) -> list[str]:
        return [a.sku_id for a in self.assigned_licences if a.sku_id]

    @property
    def last_sign_in(self) -> dt.datetime | None:
        return self.sign_in_activity.most_recent if self.sign_in_activity else None


class SubscribedSku(GraphModel):
    """One subscription the tenant holds. `prepaidUnits.enabled` is what was
    bought and `consumedUnits` what is assigned; free seats are the difference
    and never go below zero, because a tenant in its grace period can be over
    its count."""

    sku_id: str = Field(alias="skuId")
    sku_part_number: str | None = Field(default=None, alias="skuPartNumber")
    consumed_units: int = Field(default=0, alias="consumedUnits")
    prepaid_units: dict[str, Any] = Field(default_factory=dict, alias="prepaidUnits")
    capability_status: str | None = Field(default=None, alias="capabilityStatus")

    @property
    def enabled_units(self) -> int:
        value = self.prepaid_units.get("enabled")
        return int(value) if isinstance(value, int) else 0

    @property
    def warning_units(self) -> int:
        value = self.prepaid_units.get("warning")
        return int(value) if isinstance(value, int) else 0

    @property
    def free_units(self) -> int:
        return max(self.enabled_units - self.consumed_units, 0)

    @property
    def name(self) -> str:
        """What a person calls it. Graph's part number is the stable name; the
        friendly product names are not in the API and are not invented here."""
        return self.sku_part_number or self.sku_id


class RegistrationDetail(GraphModel):
    """One row of the authentication methods registration report. The report
    is the only app-only way to see MFA registration across a tenant."""

    id: str
    user_principal_name: str | None = Field(default=None, alias="userPrincipalName")
    user_display_name: str | None = Field(default=None, alias="userDisplayName")
    is_mfa_registered: bool | None = Field(default=None, alias="isMfaRegistered")
    is_mfa_capable: bool | None = Field(default=None, alias="isMfaCapable")
    is_admin: bool | None = Field(default=None, alias="isAdmin")
    methods_registered: list[str] = Field(default_factory=list, alias="methodsRegistered")
    user_type: str | None = Field(default=None, alias="userType")

    @property
    def registered(self) -> bool:
        """Whether this account has a second factor registered. `isMfaCapable`
        is the stricter of the two — registered *and* usable for MFA — and is
        what a tenant's own report screen shows; `isMfaRegistered` is the
        fallback for a tenant that does not return it."""
        if self.is_mfa_capable is not None:
            return bool(self.is_mfa_capable)
        return bool(self.is_mfa_registered)

    @property
    def strong_methods(self) -> list[str]:
        return [m for m in self.methods_registered if m in STRONG_METHODS]


#: `groupTypes` carries this for a Microsoft 365 group; a security group's
#: list is empty.
UNIFIED = "Unified"
#: `resourceProvisioningOptions` carries this when a Microsoft 365 group has
#: been given a team.
TEAM = "Team"

GROUP_SECURITY = "security group"
GROUP_MICROSOFT365 = "Microsoft 365 group"
GROUP_TEAM = "Team"
GROUP_DISTRIBUTION = "distribution list"
GROUP_MAIL_SECURITY = "mail-enabled security group"
GROUP_KINDS = (
    GROUP_TEAM,
    GROUP_MICROSOFT365,
    GROUP_DISTRIBUTION,
    GROUP_MAIL_SECURITY,
    GROUP_SECURITY,
)


class GraphGroup(GraphModel):
    id: str
    display_name: str | None = Field(default=None, alias="displayName")
    mail: str | None = None
    mail_enabled: bool | None = Field(default=None, alias="mailEnabled")
    security_enabled: bool | None = Field(default=None, alias="securityEnabled")
    group_types: list[str] = Field(default_factory=list, alias="groupTypes")
    resource_provisioning_options: list[str] = Field(
        default_factory=list, alias="resourceProvisioningOptions"
    )
    member_count: int | None = Field(default=None, alias="members@odata.count")
    visibility: str | None = None
    created: dt.datetime | None = Field(default=None, alias="createdDateTime")

    @property
    def kind(self) -> str:
        """Which of the five kinds this is, by Graph's own flags.

        A team is a Microsoft 365 group with a team provisioned on it, so it is
        tested first; a mail-enabled security group is both, and is its own
        kind because a distribution list is not a security principal and the
        two are managed differently.
        """
        unified = UNIFIED in self.group_types
        if unified and TEAM in self.resource_provisioning_options:
            return GROUP_TEAM
        if unified:
            return GROUP_MICROSOFT365
        if self.mail_enabled and self.security_enabled:
            return GROUP_MAIL_SECURITY
        if self.mail_enabled:
            return GROUP_DISTRIBUTION
        return GROUP_SECURITY


class MailboxUsage(GraphModel):
    """One row of `getMailboxUsageDetail`. The report is CSV; this is what we
    keep of each row. The display name and address are concealed by Graph
    itself when the tenant has the anonymised-reports setting on, in which case
    a row cannot be joined to a user and says so."""

    user_principal_name: str | None = None
    display_name: str | None = None
    storage_used_bytes: int | None = None
    item_count: int | None = None
    deleted: bool | None = None
    last_activity: dt.date | None = None
    issue_warning_quota_bytes: int | None = None

    @property
    def concealed(self) -> bool:
        """Whether this row's identity was withheld by the tenant's own report
        setting. Graph replaces the address with an opaque string, so a row
        with no `@` in it cannot be joined to a user."""
        return not (self.user_principal_name and "@" in self.user_principal_name)


__all__ = [
    "GROUP_DISTRIBUTION",
    "GROUP_KINDS",
    "GROUP_MAIL_SECURITY",
    "GROUP_MICROSOFT365",
    "GROUP_SECURITY",
    "GROUP_TEAM",
    "GUEST",
    "MEMBER",
    "METHOD_WORDS",
    "STRONG_METHODS",
    "AssignedLicence",
    "GraphGroup",
    "GraphUser",
    "MailboxUsage",
    "RegistrationDetail",
    "SignInActivity",
    "SubscribedSku",
    "method_words",
]

"""What the connector is asked for, assembled once: an account, and a tenant.

The profile command, the Ask tools and the ticket page all want the same two
things — one person's account state, and a whole tenant's shape — so they are
built here and each caller renders them. Three code paths asking Graph three
slightly different questions is how two screens come to disagree about how
many people have no MFA.

**Counts and ids are what a caller stores; names are what a page prints.**
Every structure here carries both, and the rule is on the caller: the profile
prints counts, the ticket page prints one person's own details, and nothing
writes a name to a log (CLAUDE.md, working conventions).

**An item a tenant cannot answer is a sentence, not a gap.** Each result
carries `unavailable`: the items that could not be read and why, in words a
page prints. A tenant with no premium licence has no sign-in activity, a
tenant that did not consent to `MailboxSettings.Read` has no mailbox kinds,
and neither is an error or a zero.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.connectors.microsoft365.client import Microsoft365Client, NotAvailable, NotLicensed
from app.connectors.microsoft365.models import (
    GROUP_KINDS,
    GraphGroup,
    GraphUser,
    MailboxUsage,
    RegistrationDetail,
    SubscribedSku,
    method_words,
)

#: A member account that has not signed in for this long is stale enough to
#: be worth a line on a ticket. The same number the ticket findings use.
STALE_SIGN_IN_DAYS = 30

#: How many accounts the profile will read a mailbox kind for. Only the ones
#: that look like they have no second factor are read — that is where the
#: distinction changes a number — and the read is one request each, so it is
#: bounded rather than run over a whole directory.
MAILBOX_KIND_READS = 60


@dataclass(slots=True)
class Unavailable:
    """One item this tenant cannot answer, and why."""

    item: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"item": self.item, "reason": self.reason}

    @property
    def line(self) -> str:
        return f"{self.item}: {self.reason}"


@dataclass(slots=True)
class Account:
    """One person's Microsoft 365 account, as a page prints it."""

    found: bool
    user_id: str | None = None
    display_name: str | None = None
    address: str | None = None
    enabled: bool | None = None
    user_type: str | None = None
    synced: bool | None = None
    created: dt.datetime | None = None
    last_sign_in: dt.datetime | None = None
    department: str | None = None
    job_title: str | None = None
    office_location: str | None = None
    manager: str | None = None
    licences: list[str] = field(default_factory=list)
    mfa_registered: bool | None = None
    mfa_methods: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    mailbox_kind: str | None = None
    unavailable: list[Unavailable] = field(default_factory=list)
    read_at: dt.datetime | None = None

    @property
    def days_since_sign_in(self) -> int | None:
        if self.last_sign_in is None or self.read_at is None:
            return None
        return max((self.read_at - self.last_sign_in).days, 0)

    @property
    def unlicensed_member(self) -> bool:
        """An enabled member account with no licence at all. A guest with none
        is ordinary; a shared mailbox with none is correct."""
        return bool(
            self.found
            and self.enabled
            and (self.user_type or "Member").casefold() == "member"
            and not self.licences
            and self.mailbox_kind in (None, "user")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "address": self.address,
            "enabled": self.enabled,
            "user_type": self.user_type,
            "synced": self.synced,
            "created": _iso(self.created),
            "last_sign_in": _iso(self.last_sign_in),
            "days_since_sign_in": self.days_since_sign_in,
            "department": self.department,
            "job_title": self.job_title,
            "office_location": self.office_location,
            "manager": self.manager,
            "licences": list(self.licences),
            "mfa_registered": self.mfa_registered,
            "mfa_methods": list(self.mfa_methods),
            "groups": list(self.groups),
            "mailbox_kind": self.mailbox_kind,
            "unavailable": [u.as_dict() for u in self.unavailable],
            "read_at": _iso(self.read_at),
        }


@dataclass(slots=True)
class LicenceLine:
    name: str
    sku_id: str
    assigned: int
    total: int
    free: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sku_id": self.sku_id,
            "assigned": self.assigned,
            "total": self.total,
            "free": self.free,
        }


@dataclass(slots=True)
class GroupLine:
    name: str
    kind: str
    members: int | None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "members": self.members}


@dataclass(slots=True)
class Profile:
    """One tenant's shape, in counts. Names appear only where a line is about
    one thing a person reads — a licence's name, a group's name — never in a
    stored figure."""

    tenant_id: str
    site_slug: str | None
    read_at: dt.datetime
    users_total: int = 0
    members: int = 0
    guests: int = 0
    enabled: int = 0
    disabled: int = 0
    synced: int = 0
    cloud_only: int = 0
    licences: list[LicenceLine] = field(default_factory=list)
    mfa_registered: int = 0
    mfa_missing: int = 0
    mfa_population: int = 0
    mfa_missing_names: list[str] = field(default_factory=list)
    mfa_not_people: int = 0
    """Accounts left out of the MFA figures because they are not people: a
    shared mailbox or a room has no interactive sign-in and cannot register a
    second factor, so counting it as missing one would inflate the gap."""
    groups: list[GroupLine] = field(default_factory=list)
    group_counts: dict[str, int] = field(default_factory=dict)
    departments: list[tuple[str, int]] = field(default_factory=list)
    job_titles: list[tuple[str, int]] = field(default_factory=list)
    offices: list[tuple[str, int]] = field(default_factory=list)
    mailboxes: dict[str, int] = field(default_factory=dict)
    mailbox_sizes: list[tuple[str, int]] = field(default_factory=list)
    unavailable: list[Unavailable] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Counts and ids only: what a caller may store."""
        return {
            "tenant_id": self.tenant_id,
            "site_slug": self.site_slug,
            "read_at": self.read_at.isoformat(),
            "users": {
                "total": self.users_total,
                "members": self.members,
                "guests": self.guests,
                "enabled": self.enabled,
                "disabled": self.disabled,
                "synced": self.synced,
                "cloud_only": self.cloud_only,
            },
            "licences": [licence.as_dict() for licence in self.licences],
            "mfa": {
                "registered": self.mfa_registered,
                "missing": self.mfa_missing,
                "population": self.mfa_population,
                "not_people": self.mfa_not_people,
            },
            "groups": self.group_counts,
            "departments": len(self.departments),
            "job_titles": len(self.job_titles),
            "offices": len(self.offices),
            "mailboxes": dict(self.mailboxes),
            "unavailable": [u.as_dict() for u in self.unavailable],
            "truncated": self.truncated,
        }


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


#: The refusals that are facts about a tenant rather than failures of a read.
TENANT_LIMITS = (NotAvailable, NotLicensed)


async def account_for(
    connector: Microsoft365Client, address: str, *, now: dt.datetime | None = None
) -> Account:
    """One person's account, by the address a ticket's contact carries.

    Every read but the user is optional: a tenant that cannot answer one of
    them gets a line saying so and the rest of the account. An account that is
    not in the tenant is `found=False`, which is a fact about the contact and
    not a failure.
    """
    now = now or dt.datetime.now(dt.UTC)
    missing: list[Unavailable] = []
    user = await connector.get_user(address)
    if user is None:
        return Account(found=False, address=address.lower(), read_at=now)

    names: dict[str, str] = {}
    try:
        names = {sku.sku_id: sku.name for sku in await connector.list_subscribed_skus()}
    except TENANT_LIMITS as exc:
        missing.append(Unavailable("licence names", str(exc)))

    registration: RegistrationDetail | None = None
    try:
        registration = await _registration_for(connector, user)
    except TENANT_LIMITS as exc:
        missing.append(Unavailable("MFA registration", str(exc)))

    groups: list[GraphGroup] = []
    try:
        groups, _ = await connector.list_user_groups(user.id)
    except TENANT_LIMITS as exc:
        missing.append(Unavailable("groups", str(exc)))

    manager: GraphUser | None = None
    try:
        manager = await connector.get_manager(user.id)
    except TENANT_LIMITS as exc:
        missing.append(Unavailable("manager", str(exc)))

    purpose: str | None = None
    try:
        purpose = await connector.mailbox_purpose(user.id)
    except TENANT_LIMITS as exc:
        missing.append(Unavailable("mailbox kind", str(exc)))

    if user.sign_in_activity is None:
        missing.append(
            Unavailable(
                "last sign-in",
                "this tenant has no Microsoft Entra ID P1 or P2 licence, or did not grant "
                "AuditLog.Read.All",
            )
        )
    if user.account_enabled is False and registration is None:
        missing.append(
            Unavailable(
                "MFA registration",
                "Graph's registration report leaves out disabled accounts, so this one's "
                "second factor cannot be read",
            )
        )

    return Account(
        found=True,
        user_id=user.id,
        display_name=user.display_name,
        address=(user.mail or user.user_principal_name or address).lower(),
        enabled=user.account_enabled,
        user_type=user.user_type,
        synced=user.synced,
        created=user.created,
        last_sign_in=user.last_sign_in,
        department=user.department,
        job_title=user.job_title,
        office_location=user.office_location,
        manager=manager.display_name if manager is not None else None,
        licences=[names.get(sku, sku) for sku in user.licence_skus],
        mfa_registered=registration.registered if registration is not None else None,
        mfa_methods=method_words(registration.methods_registered) if registration else [],
        groups=[g.display_name or g.id for g in groups],
        mailbox_kind=purpose,
        unavailable=missing,
        read_at=now,
    )


async def _registration_for(
    connector: Microsoft365Client, user: GraphUser
) -> RegistrationDetail | None:
    """This user's row of the registration report.

    The report is tenant-wide and has no per-user form with application
    permissions; it is read and filtered here. It also omits disabled
    accounts, so a disabled user has no row and their MFA state is reported
    as unknown rather than as "none registered".
    """
    rows, _ = await connector.list_registration_details()
    wanted = {a for a in user.addresses}
    for row in rows:
        if row.id == user.id or (row.user_principal_name or "").lower() in wanted:
            return row
    return None


async def profile_for(connector: Microsoft365Client, *, now: dt.datetime | None = None) -> Profile:
    """One tenant's shape: who is in it, what it is licensed for, who has no
    second factor, what its groups are, and what roles exist by department,
    job title and office."""
    now = now or dt.datetime.now(dt.UTC)
    missing: list[Unavailable] = []
    tenant = connector.tenant
    site = connector.site_for(tenant)
    profile = Profile(tenant_id=tenant, site_slug=site.slug if site else None, read_at=now)

    users, truncated, sign_in_missing = await connector.list_users()
    profile.truncated = truncated
    if sign_in_missing:
        missing.append(Unavailable("last sign-in", sign_in_missing))

    profile.users_total = len(users)
    for user in users:
        if user.is_member:
            profile.members += 1
        else:
            profile.guests += 1
        if user.account_enabled is False:
            profile.disabled += 1
        else:
            profile.enabled += 1
        if user.synced:
            profile.synced += 1
        else:
            profile.cloud_only += 1

    try:
        profile.licences = _licence_lines(await connector.list_subscribed_skus())
    except TENANT_LIMITS as exc:
        missing.append(Unavailable("licences", str(exc)))

    rows: list[RegistrationDetail] = []
    try:
        rows, _ = await connector.list_registration_details()
    except TENANT_LIMITS as exc:
        missing.append(Unavailable("MFA registration", str(exc)))
    registered, without, population = _mfa_coverage(users, rows)
    # A shared mailbox and a room are enabled member accounts in Entra and
    # cannot register a second factor. They are only told apart by their
    # mailbox kind, which is one read each, so only the accounts that look
    # like they have none are checked: that is where it changes a number.
    not_people: list[GraphUser] = []
    if without:
        checked = 0
        for user in list(without):
            if checked >= MAILBOX_KIND_READS:
                break
            try:
                purpose = await connector.mailbox_purpose(user.id)
            except TENANT_LIMITS as exc:
                missing.append(
                    Unavailable(
                        "which accounts are shared mailboxes",
                        f"{exc}; a shared mailbox is counted as missing MFA, so the gap may "
                        f"read larger than it is",
                    )
                )
                break
            checked += 1
            if purpose is not None and purpose != "user":
                not_people.append(user)
        for user in not_people:
            without.remove(user)
    profile.mfa_registered = registered
    profile.mfa_missing = len(without)
    profile.mfa_missing_names = sorted(
        u.user_principal_name or u.display_name or u.id for u in without
    )
    profile.mfa_population = population - len(not_people)
    profile.mfa_not_people = len(not_people)

    found: list[GraphGroup] = []
    try:
        found, _ = await connector.list_groups()
    except TENANT_LIMITS as exc:
        missing.append(Unavailable("groups", str(exc)))
    profile.groups = [
        GroupLine(name=g.display_name or g.id, kind=g.kind, members=g.member_count) for g in found
    ]
    profile.group_counts = {
        kind: sum(1 for g in profile.groups if g.kind == kind) for kind in GROUP_KINDS
    }

    profile.departments = _tally(user.department for user in users)
    profile.job_titles = _tally(user.job_title for user in users)
    profile.offices = _tally(user.office_location for user in users)

    concealed = await connector.report_settings()
    usage: list[MailboxUsage] = []
    try:
        usage = await connector.mailbox_usage()
    except TENANT_LIMITS as exc:
        missing.append(Unavailable("mailbox sizes", str(exc)))
    profile.mailbox_sizes, profile.mailboxes = _mailboxes(usage, concealed, missing)
    profile.unavailable = missing
    return profile


def _licence_lines(skus: list[SubscribedSku]) -> list[LicenceLine]:
    """Subscriptions, the ones somebody pays for first, then by name."""
    lines = [
        LicenceLine(
            name=sku.name,
            sku_id=sku.sku_id,
            assigned=sku.consumed_units,
            total=sku.enabled_units,
            free=sku.free_units,
        )
        for sku in skus
    ]
    return sorted(lines, key=lambda line: (-line.total, line.name))


def _mfa_coverage(
    users: list[GraphUser], rows: list[RegistrationDetail]
) -> tuple[int, list[GraphUser], int]:
    """MFA over the population that can be judged: enabled member accounts.

    A disabled account has no row in Graph's report at all, and a guest is
    somebody else's identity — counting either would make the figure look
    better or worse than the thing anybody can act on. The accounts that are
    not people are taken out by the caller, which has to read a mailbox kind
    to know.
    """
    by_address = {(row.user_principal_name or "").lower(): row for row in rows if row}
    by_id = {row.id: row for row in rows if row}
    registered = 0
    without: list[GraphUser] = []
    population = 0
    for user in users:
        if not user.is_member or user.account_enabled is False:
            continue
        population += 1
        row = by_id.get(user.id) or next(
            (by_address[a] for a in user.addresses if a in by_address), None
        )
        if row is not None and row.registered:
            registered += 1
        else:
            without.append(user)
    return registered, without, population


def _tally(values: Iterable[str | None]) -> list[tuple[str, int]]:
    """How many people share each value, most first; blanks left out.

    This is what a role matrix is drafted from: not a guess at what the roles
    should be, but what the tenant already says they are.
    """
    counts = Counter(str(v).strip() for v in values if v and str(v).strip())
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _mailboxes(
    usage: list[MailboxUsage], concealed: bool | None, missing: list[Unavailable]
) -> tuple[list[tuple[str, int]], dict[str, int]]:
    """Mailbox sizes, biggest first, and the counts beside them.

    A tenant that conceals the names in its reports gives rows that cannot be
    joined to anybody; those are counted and said, never guessed onto a user.
    """
    live = [row for row in usage if not row.deleted]
    named = [row for row in live if not row.concealed]
    hidden = len(live) - len(named)
    if hidden:
        why = (
            "this tenant conceals the names in its usage reports, so a mailbox cannot be "
            "joined to a person"
            if concealed
            else "some rows of the usage report carry no address"
        )
        missing.append(Unavailable(f"{hidden} mailbox size(s)", why))
    sizes = sorted(
        (
            (row.user_principal_name or "", row.storage_used_bytes or 0)
            for row in named
            if row.storage_used_bytes
        ),
        key=lambda item: -item[1],
    )
    return sizes, {"with a size": len(named), "name concealed": hidden}


def mailbox_kinds(purposes: list[str | None]) -> dict[str, int]:
    """How many of each kind of mailbox, for a caller that read them."""
    return dict(Counter(p or "unknown" for p in purposes))


__all__ = [
    "STALE_SIGN_IN_DAYS",
    "Account",
    "GroupLine",
    "LicenceLine",
    "Profile",
    "Unavailable",
    "account_for",
    "mailbox_kinds",
    "profile_for",
]

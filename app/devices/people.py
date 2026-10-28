"""People across the tools: one Microsoft 365 account, its contact, its machines.

The device layer beside this one answers "which machine is this", by pairing
vendor records within a site. This answers the same question about a person,
and it is deliberately the lighter of the two: **nothing is stored and nothing
is written**. A join is computed when somebody asks for it, printed, and
forgotten. There is no `people` table, no link row, no queue and no manual
correction, because none of those is needed yet and all of them would have to
be kept true afterwards.

Two joins, each by one thing and nothing looser:

- **a Microsoft 365 account to an Autotask contact, by email address.** Exact,
  case-insensitive, against every address the account carries (its sign-in
  name and its mail address). A name would match several people in a
  directory, and two people called Chris Allen at one customer is how a
  password reset gets done on the wrong account.
- **a Microsoft 365 account to the machines it is the last logged-on user of**,
  through the identity layer's devices for that site. The login is compared on
  its local part (`someone@contoso.example` against `CONTOSO\\someone`), which
  is `login_key`, and failing that the display name's usual login shapes —
  the same two rules the ticket page already resolves a device by, so the two
  cannot come to different answers about one person.

**Within one site only**, like every other join here: each end customer has
their own Entra tenant, and a tenant's accounts are never matched against
another customer's contacts or machines.

Microsoft 365 is **not** on the coverage board, on purpose. The board is about
agents on machines; an account is not an agent, and a tool with no reader on
that board reads as `not_assessed` rather than as a gap.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.matching import normalise_hostname
from app.models.device import Device
from app.tickets.resolve import login_key, login_shapes

#: How an account was joined to a machine. Printed beside every pairing: a
#: match on a login name is a different claim from a match on a display name.
BY_LOGIN = "login name"
BY_DISPLAY_NAME = "display name"


@dataclass(slots=True)
class DeviceMatch:
    device_id: uuid.UUID
    matched_by: str

    def as_dict(self) -> dict[str, Any]:
        return {"device_id": str(self.device_id), "matched_by": self.matched_by}


@dataclass(slots=True)
class PersonRow:
    """One Microsoft 365 account and what it was joined to.

    Ids, not names: this is what a terminal prints and what a caller may keep,
    and `scripts/devices.py` sets the rule — device ids, vendor ids and passes,
    never a hostname (CLAUDE.md, working conventions).
    """

    user_id: str
    enabled: bool | None
    contact_id: int | None = None
    devices: list[DeviceMatch] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "enabled": self.enabled,
            "contact_id": self.contact_id,
            "devices": [d.as_dict() for d in self.devices],
        }


@dataclass(slots=True)
class PeopleJoin:
    """The whole join for one tenant, in counts and ids."""

    site_id: uuid.UUID | None
    site_slug: str | None
    tenant_id: str
    people: list[PersonRow] = field(default_factory=list)
    contacts_total: int = 0
    contacts_unmatched: int = 0
    """Autotask contacts at this customer with no Microsoft 365 account on that
    address. An ex-employee's contact record, or a shared address nobody signs
    in with — a count and never a finding."""
    logged_on_unmatched: int = 0
    """Machines whose last logged-on user is nobody in the directory: a local
    account, a technician's own login, or a service account."""

    @property
    def matched_contacts(self) -> int:
        return sum(1 for row in self.people if row.contact_id is not None)

    @property
    def matched_devices(self) -> int:
        return sum(1 for row in self.people if row.devices)

    def as_dict(self) -> dict[str, Any]:
        return {
            "site_id": str(self.site_id) if self.site_id else None,
            "site_slug": self.site_slug,
            "tenant_id": self.tenant_id,
            "accounts": len(self.people),
            "accounts_with_a_contact": self.matched_contacts,
            "accounts_with_a_device": self.matched_devices,
            "contacts": self.contacts_total,
            "contacts_without_an_account": self.contacts_unmatched,
            "machines_whose_user_is_nobody": self.logged_on_unmatched,
            "people": [row.as_dict() for row in self.people],
        }


async def site_devices(
    session: AsyncSession, client_id: uuid.UUID, site_id: uuid.UUID | None
) -> dict[str, Device]:
    """The identity layer's live devices for a site, by normalised name.

    The same population the ticket page resolves against, so a machine this
    join reaches is a machine that page would reach.
    """
    query = select(Device).where(Device.client_id == client_id, Device.status != "retired")
    if site_id is not None:
        query = query.where(Device.site_id == site_id)
    return {
        normalise_hostname(device.canonical_name): device
        for device in (await session.execute(query)).scalars()
    }


def join_people(
    *,
    accounts: list[Any],
    contacts: list[tuple[int, str | None]],
    logged_on: list[tuple[str, str | None]],
    devices_by_name: dict[str, Device],
    site_id: uuid.UUID | None = None,
    site_slug: str | None = None,
    tenant_id: str = "",
) -> PeopleJoin:
    """Join one tenant's accounts to its customer's contacts and machines.

    Pure: given the same rows it returns the same join, and it reads no vendor
    and no database. `accounts` are `GraphUser`s, `contacts` are
    `(Autotask contact id, email)`, `logged_on` are `(device name, user)` rows
    from NinjaOne for the same site.
    """
    join = PeopleJoin(site_id=site_id, site_slug=site_slug, tenant_id=tenant_id)

    by_address: dict[str, int] = {}
    for contact_id, email in contacts:
        address = (email or "").strip().lower()
        if address:
            by_address.setdefault(address, contact_id)
    join.contacts_total = len(contacts)

    claimed_contacts: set[int] = set()
    claimed_logins: set[int] = set()
    for account in accounts:
        row = PersonRow(user_id=account.id, enabled=account.account_enabled)
        for address in account.addresses:
            found = by_address.get(address)
            if found is not None:
                row.contact_id = found
                claimed_contacts.add(found)
                break

        keys = {login_key(address) for address in account.addresses}
        keys.discard("")
        shapes = login_shapes(account.display_name or "")
        seen: set[uuid.UUID] = set()
        for index, (device_name, user) in enumerate(logged_on):
            key = login_key(user)
            if not key:
                continue
            if key in keys:
                how = BY_LOGIN
            elif key in shapes or key.replace(".", " ") in shapes:
                how = BY_DISPLAY_NAME
            else:
                continue
            claimed_logins.add(index)
            device = devices_by_name.get(normalise_hostname(device_name))
            if device is None or device.id in seen:
                # A machine NinjaOne reports that the identity layer has not
                # resolved yet is not a device of ours, so it is not a pairing.
                continue
            seen.add(device.id)
            row.devices.append(DeviceMatch(device_id=device.id, matched_by=how))
        join.people.append(row)

    join.contacts_unmatched = sum(
        1
        for contact_id, email in contacts
        if (email or "").strip() and contact_id not in claimed_contacts
    )
    join.logged_on_unmatched = sum(
        1
        for index, (_, user) in enumerate(logged_on)
        if login_key(user) and index not in claimed_logins
    )
    return join


__all__ = [
    "BY_DISPLAY_NAME",
    "BY_LOGIN",
    "DeviceMatch",
    "PeopleJoin",
    "PersonRow",
    "join_people",
    "site_devices",
]

"""The Cove responses we actually read.

Only what the backup section of a report and a ticket's device context need;
the API returns a great deal more and modelling it would be modelling the
vendor rather than the question.

A statistics row arrives as `AccountId`, `PartnerId`, `Flags` and a list of
single-key objects keyed by column code. `CoveDevice.from_row` is the one
place that is turned into something with names on it, so a code appears in
this package exactly twice: once in `columns.py` where it is declared, and
once here where it is read.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from app.connectors.cove import columns as col


class CovePartner(BaseModel):
    """One customer under ours. `EnumeratePartners` returns a flat list and
    the tree is in `parent_id`."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: int = Field(alias="Id")
    name: str = Field(alias="Name")
    parent_id: int | None = Field(default=None, alias="ParentId")
    level: str | None = Field(default=None, alias="Level")
    """`EndCustomer`, `SubDistributor`, … — which level of the tree this is."""
    state: str | None = Field(default=None, alias="State")
    """`Registered`, `InTrial`, … — a trial customer is not a lapsed one."""


class CoveDevice(BaseModel):
    """One row of `EnumerateAccountStatistics`.

    A row is not always a machine: Cove holds a customer's Microsoft 365
    tenant as a device of its own, with no OS and no physicality.
    `is_microsoft365` is how the two are told apart, and anything counting
    machines must say which population it counted.
    """

    model_config = ConfigDict(extra="forbid")

    account_id: int
    partner_id: int
    name: str
    """Cove's own name, suffix and all, as the console shows it."""
    key: str
    """The machine's name as other tools know it: lower case, suffix removed."""
    computer_name: str | None = None
    macs: list[str] = Field(default_factory=list)
    """Every interface Cove reports, for the join's MAC pass."""
    account_type: str = "unknown"
    os_type: str = "undefined"
    os_version: str | None = None
    physicality: str | None = None
    used_storage_bytes: int | None = None
    data_sources: list[str] = Field(default_factory=list)
    product: str | None = None
    """What the console calls the retention policy — `All-In`, `Documents`,
    `90D-52W-36M-7Y`. This is what a report's retention distribution counts
    (live, 2026-10-19), not `retention_units`."""
    retention_units: str | None = None
    """The retention scheme behind the product: `Enhanced` or `Classic(Product)`."""
    sku: str | None = None
    """The billing article (`ServerPro`, `Office365`). Not a retention policy."""
    profile: str | None = None
    recovery_testing: str | None = None
    created_at: dt.datetime | None = None

    # Across every data source: the device's own answer.
    last_session_status: str | None = None
    last_session_at: dt.datetime | None = None
    last_successful_at: dt.datetime | None = None
    errors: int | None = None
    colour_bar: str | None = None
    """28 characters, one per day. The encoding is not documented and is the
    first thing the live run has to establish (docs/api-notes/cove.md)."""

    # Microsoft 365, where this row is a tenant.
    user_mailboxes: int | None = None
    shared_mailboxes: int | None = None
    onedrive_users: int | None = None

    @property
    def match_key(self) -> str:
        """The name to pair this row on.

        Cove's *computer name* is the machine's own hostname; its *device
        name* is whatever the account was called, with Cove's suffix on the
        end. They agree on all but one of the pilot's 44 machines, and where
        they differ the computer name is the one the other tools know.
        """
        return col.device_key(self.computer_name or self.name)

    @property
    def is_microsoft365(self) -> bool:
        return self.account_type == "microsoft365"

    @property
    def is_machine(self) -> bool:
        """A row that stands for a computer. The complement of a tenant, said
        this way round because every count about machines reads it."""
        return not self.is_microsoft365

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Self:
        """One statistics row, folded and converted. Nothing here raises: a
        value the vendor left out, or sent in a shape we cannot read, is
        `None`, which is a different claim from a zero."""
        settings = col.fold(row.get("Settings"))

        def text(name: str) -> str | None:
            return settings.get(col.COLUMNS[name]) or None

        def number(name: str) -> int | None:
            return col.as_int(settings.get(col.COLUMNS[name]))

        def total(field: str) -> str | None:
            return settings.get(col.stat(col.TOTAL, field)) or None

        account_type = col.ACCOUNT_TYPE.get(number("account_type") or 0, "unknown")
        os_type = col.OS_TYPE.get(number("os_type") or 0, "undefined")
        testing = number("recovery_testing")
        name = text("device_name") or text("computer_name") or ""
        return cls(
            account_id=int(row.get("AccountId", 0)),
            partner_id=int(row.get("PartnerId", 0)),
            name=name,
            key=col.device_key(name),
            computer_name=text("computer_name"),
            macs=col.macs(text("macs")),
            account_type=account_type,
            os_type=os_type,
            os_version=text("os_version"),
            physicality=text("physicality"),
            used_storage_bytes=number("used_storage"),
            data_sources=col.data_sources(text("data_sources")),
            product=text("product"),
            retention_units=text("retention_units"),
            sku=text("sku"),
            profile=text("profile"),
            recovery_testing=None if testing is None else col.RECOVERY_TESTING.get(testing),
            created_at=col.as_time(settings.get(col.COLUMNS["created_at"])),
            last_session_status=col.SESSION_STATUS.get(
                col.as_int(total("last_session_status")) or -1
            ),
            last_session_at=col.as_time(total("last_session_at")),
            last_successful_at=col.as_time(total("last_successful_at")),
            errors=col.as_int(total("last_session_errors")),
            colour_bar=total("colour_bar_28_days"),
            user_mailboxes=col.as_int(settings.get(col.stat(col.M365_EXCHANGE, "user_mailboxes"))),
            shared_mailboxes=col.as_int(
                settings.get(col.stat(col.M365_EXCHANGE, "shared_mailboxes"))
            ),
            onedrive_users=col.as_int(settings.get(col.stat(col.M365_ONEDRIVE, "user_mailboxes"))),
        )

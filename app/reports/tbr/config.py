"""A franchisee's report settings: who they are, what they sell, what they charge.

One per client, stored in `report_configs`, set with
`scripts/manage_client.py report-config`. A franchisee with no row gets
`FranchiseeConfig.default_for(...)`: its own name, the template's service list,
and an **empty price book**.

The price book is the only place a price or an hour estimate can come from
(slide map rule 4). A recommendation whose type has no entry, or whose entry
has no figure, prints "Quote pending". Nothing in the pipeline estimates,
interpolates or remembers a price, and the model never sees one to repeat.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.config import AutomationConfig
from app.autopilot.config import AutopilotConfig
from app.models.report import ReportConfig
from app.reports.tbr.backup_config import BackupConfig
from app.reports.tbr.limits import SelfServiceConfig
from app.reports.tbr.model import ReviewDocument
from app.resolved.config import ResolvedConfig

QUOTE_PENDING = "Quote pending"


class ServiceCatalogItem(BaseModel):
    key: str = Field(description="Stable key a finding rule points at, e.g. `mdr`.")
    name: str = Field(description="As it is printed on the Recommended Services slide.")
    category: str | None = Field(
        default=None,
        description="Heading it is listed under, e.g. Recommended Additional Cybersecurity.",
    )


class PriceBookEntry(BaseModel):
    """What one kind of recommendation costs at this franchisee. Either figure may be absent."""

    label: str | None = None
    hours: Decimal | None = Field(default=None, ge=0)
    cost: Decimal | None = Field(default=None, ge=0)
    per: str | None = Field(default=None, description='Unit, e.g. "device". Absent for a flat fee.')


#: The service list on the template's Recommended Services slide, with its typo
#: ("Cyber Attach Detection") corrected. A franchisee replaces this with its own.
DEFAULT_SERVICE_CATALOG: tuple[ServiceCatalogItem, ...] = (
    ServiceCatalogItem(key="cloud_computing", name="Cloud Computing Technology"),
    ServiceCatalogItem(key="cybersecurity_essentials", name="Cybersecurity Essentials"),
    ServiceCatalogItem(
        key="mdr",
        name="Endpoint + Cloud Protection (MDR) – Device and Cloud Security Monitoring",
        category="Recommended Additional Cybersecurity",
    ),
    ServiceCatalogItem(
        key="sase_dlp",
        name=(
            "Secure Access Service Edge + Network Data Loss Prevention – Secure Login and "
            "Permission Management"
        ),
        category="Recommended Additional Cybersecurity",
    ),
    ServiceCatalogItem(
        key="mdm",
        name="Mobile Device Management (MDM) – Secure Access on Mobile Devices",
        category="Recommended Additional Cybersecurity",
    ),
    ServiceCatalogItem(
        key="pen_test",
        name="Penetration Testing – Security Testing",
        category="Recommended Additional Cybersecurity",
    ),
    ServiceCatalogItem(
        key="siem_soc",
        name="SIEM / SOC and Threat Hunting – Cyber Attack Detection",
        category="Recommended Additional Cybersecurity",
    ),
    ServiceCatalogItem(
        key="full_disk_encryption",
        name="Full Disk Encryption – Device Data Protection",
        category="Recommended Additional Cybersecurity",
    ),
    ServiceCatalogItem(key="password_manager", name="Password Management", category="Other"),
    ServiceCatalogItem(
        key="patch_remediation", name="Patch Remediation and Compliance", category="Other"
    ),
    ServiceCatalogItem(
        key="vulnerability_management",
        name="Vulnerability Management – Continuous Scanning and Remediation",
        category="Recommended Additional Cybersecurity",
    ),
    ServiceCatalogItem(
        key="hardware_lifecycle", name="Hardware Lifecycle Planning", category="Other"
    ),
)


DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
FIXED_OFFSET = re.compile(r"^UTC(?:([+-])(\d{1,2})(?::?(\d{2}))?)?$", re.IGNORECASE)


class TimeZoneUnavailable(ValueError):
    """The configured time zone cannot be used on this machine; the message says why."""


def resolve_zone(name: str) -> dt.tzinfo:
    """`America/Chicago`, or a fixed offset written `UTC-06:00`.

    A named zone follows daylight saving and is what a franchisee should set. It
    needs the IANA database: present on Linux, and on Windows only if the
    `tzdata` package is installed. A fixed offset needs nothing, and is wrong for
    half the year anywhere that changes its clocks.
    """
    fixed = FIXED_OFFSET.match(name.strip())
    if fixed:
        sign, hours, minutes = fixed.groups()
        delta = dt.timedelta(hours=int(hours or 0), minutes=int(minutes or 0))
        return dt.timezone(-delta if sign == "-" else delta)
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimeZoneUnavailable(
            f"time zone {name!r} could not be loaded (unknown name, or no time zone database "
            "on this machine: install `tzdata`, or use a fixed offset such as UTC-06:00)"
        ) from exc


class BusinessHours(BaseModel):
    """When the customer's offices are open, for counting work done outside those hours.

    There is no default time zone: a guess would put a number on a slide that
    is an hour or a continent out. Until one is set the figure is left off.
    """

    timezone: str | None = Field(
        default=None, description="IANA name (America/Chicago) or a fixed offset (UTC-06:00)."
    )
    start: dt.time = dt.time(8, 0)
    end: dt.time = dt.time(17, 0)
    days: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4], description="Open days; Monday is 0."
    )

    @field_validator("days")
    @classmethod
    def _real_days(cls, value: list[int]) -> list[int]:
        if not value or any(day not in range(7) for day in value):
            raise ValueError("days are numbers from 0 (Monday) to 6 (Sunday), at least one")
        return sorted(set(value))

    def contains(self, moment: dt.datetime, zone: dt.tzinfo) -> bool:
        """Is `moment` inside business hours? The start is inside, the end is not."""
        local = moment.astimezone(zone)
        return local.weekday() in self.days and self.start <= local.time() < self.end

    def describe(self) -> str:
        """As printed: `Monday to Friday, 08:00 to 17:00`."""
        days = self.days
        if len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
            which = f"{DAY_NAMES[days[0]]} to {DAY_NAMES[days[-1]]}"
        else:
            which = ", ".join(DAY_NAMES[day] for day in days)
        return f"{which}, {self.start:%H:%M} to {self.end:%H:%M}"


class FranchiseeConfig(BaseModel):
    display_name: str = Field(description='As printed after "Presented by:".')
    logo_path: str | None = Field(
        default=None,
        description="An image file for the title slide. Without one the client's name is "
        "printed as text.",
    )
    presenter: str | None = Field(default=None, description="A person, if one should be named.")
    service_catalog: list[ServiceCatalogItem] = Field(
        default_factory=lambda: list(DEFAULT_SERVICE_CATALOG)
    )
    price_book: dict[str, PriceBookEntry] = Field(
        default_factory=dict,
        description="Keyed by recommendation type or service key. Empty means every figure "
        "prints as Quote pending.",
    )
    currency_symbol: str = "$"
    review_period_months: int = Field(default=1, ge=1, le=24)
    low_memory_gb: float = Field(
        default=8, description="A workstation below this is recommended for a memory upgrade."
    )
    low_disk_percent: float = Field(default=10, gt=0, lt=100)
    sla_target_percent: float = Field(default=90, gt=0, le=100)
    business_hours: BusinessHours = Field(default_factory=BusinessHours)
    #: The per-outcome service's settings: SLA targets and the statuses that
    #: pause them, how a ticket becomes an L1, L2 or L3, which Autotask
    #: resources are Inputiv's, and what an engineer's hour costs. Here rather
    #: than in a table of its own because the clock it runs on is
    #: `business_hours` above, and two places to set a franchisee's working week
    #: is one place too many (`app/resolved/config.py`).
    resolved: ResolvedConfig = Field(default_factory=ResolvedConfig)
    #: What the triage agent may say about this franchisee's tickets: the
    #: issue-type labels it must not predict, and the ones this franchisee
    #: files as something else. Here for the same reason as `resolved`: it is
    #: one franchisee's settings, and a second place to keep them is one too
    #: many (`app/autopilot/config.py`).
    autopilot: AutopilotConfig = Field(default_factory=AutopilotConfig)
    #: Who at this franchisee's clients may authorise an automation run, and
    #: what each workflow is told about this client. Here for the same reason
    #: again: nothing a workflow needs to know about a client is written in
    #: code, and it is one franchisee's settings (`app/automation/config.py`).
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    #: Which machines are not expected to have a backup, per site. Here for
    #: the same reason as the rest: it is one franchisee's settings, and a
    #: coverage gap nobody can act on is one nobody reads
    #: (`app/reports/tbr/backup.py`).
    backup: BackupConfig = Field(default_factory=BackupConfig)
    #: What this franchisee's own administrator may do on the Reports page
    #: without asking us, and how often. Here for the same reason as the rest:
    #: it is one franchisee's settings. `limits.py` says why generating is the
    #: thing with a number on it.
    self_service: SelfServiceConfig = Field(default_factory=SelfServiceConfig)
    show_people_names: bool = Field(
        default=False,
        description=(
            "Print people's names on client and internal renders of the deck and the PDF: "
            "the tickets-by-contact chart, the lifecycle tables' last logged-in user, and a "
            "location named after a person. Off, they print as Contact A, User A and a "
            "withheld location. A review may override this (`review.set_contact_names`); a "
            "demo render pseudonymises everyone whatever this says."
        ),
    )

    @classmethod
    def default_for(cls, client_name: str) -> FranchiseeConfig:
        return cls(display_name=client_name)

    def service(self, key: str | None) -> ServiceCatalogItem | None:
        return next((item for item in self.service_catalog if item.key == key), None)

    def logo(self) -> Path | None:
        """The logo file, only if it is there to be read."""
        if not self.logo_path:
            return None
        path = Path(self.logo_path)
        return path if path.is_file() else None

    # --- the price book: the only source of a price or an hour figure ---------------

    def money(self, amount: Decimal) -> str:
        whole = amount == amount.to_integral_value()
        return (
            f"{self.currency_symbol}{amount:,.0f}"
            if whole
            else (f"{self.currency_symbol}{amount:,.2f}")
        )

    def quote(self, key: str | None, *, quantity: int = 1) -> tuple[str, str]:
        """`(hours, cost)` as printed for `quantity` of this recommendation type.

        Each is "Quote pending" unless the price book holds that figure. A
        per-unit entry is multiplied by `quantity`; a flat entry is not.
        """
        entry = self.price_book.get(key or "")
        if entry is None:
            return QUOTE_PENDING, QUOTE_PENDING
        factor = Decimal(max(quantity, 1)) if entry.per else Decimal(1)
        hours = QUOTE_PENDING
        if entry.hours is not None:
            total = (entry.hours * factor).normalize()
            hours = f"{total:f} hour" + ("" if total == 1 else "s")
        cost = QUOTE_PENDING if entry.cost is None else self.money(entry.cost * factor)
        return hours, cost


async def load_config(
    session: AsyncSession, client_id: uuid.UUID, *, client_name: str
) -> FranchiseeConfig:
    """This client's config, or the default. Never another client's."""
    row = await session.get(ReportConfig, client_id)
    if row is None or not row.config:
        return FranchiseeConfig.default_for(client_name)
    data: dict[str, Any] = {"display_name": client_name, **row.config}
    return FranchiseeConfig.model_validate(data)


async def save_config(
    session: AsyncSession, client_id: uuid.UUID, config: FranchiseeConfig, *, updated_by: str
) -> None:
    row = await session.get(ReportConfig, client_id)
    payload = config.model_dump(mode="json")
    if row is None:
        session.add(ReportConfig(client_id=client_id, config=payload, updated_by=updated_by))
    else:
        row.config = payload
        row.updated_by = updated_by
    await session.flush()


async def set_people_names(
    session: AsyncSession, client_id: uuid.UUID, *, client_name: str, show: bool, by: str
) -> tuple[bool, list[str]]:
    """Turn the franchisee's `show_people_names` on or off.

    The setting is copied onto every current review's config snapshot, and each of
    those reviews is marked changed (`store.update`): its deck and PDF are stale and
    a released one is unreleased until rendered again, because what a render names
    depends on this. Returns whether anything changed and the ids of the reviews
    touched; the caller writes the audit row once the transaction is committed.
    """
    from app.reports.tbr import store  # store imports the model only; no cycle

    config = await load_config(session, client_id, client_name=client_name)
    if config.show_people_names == show:
        return False, []
    config.show_people_names = show
    await save_config(session, client_id, config, updated_by=by)
    touched: list[str] = []
    for row in await store.list_for_client(session, client_id, limit=10_000):
        document = ReviewDocument.model_validate(row.document)
        document.franchisee["show_people_names"] = show
        await store.update(session, row, document)
        touched.append(str(row.id))
    return True, touched

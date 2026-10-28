"""Pydantic models for the parts of the Autotask REST API we actually use.

Deliberately partial: `TicketModel` has 76 fields in the vendor's Swagger and
`ConfigurationItemModel` has 113. Extra keys are ignored so an Autotask release
cannot break us.

Autotask returns datetimes as ISO 8601 strings, not epoch numbers, so Pydantic
parses them directly. Values that arrive without a timezone are treated as UTC:
see `_as_utc`. Status and priority are integers whose meaning is configured per
tenant — see docs/api-notes/autotask.md.

Every field except `id` is optional. The vendor's Swagger declares no property
required and no property nullable, and in practice Autotask sends `null` for
anything unset — including `picklistValues` on a field that is not a picklist,
where `[]` would have been the natural answer. A model that insisted on a
value would fail on the whole response because of one unset field.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _as_utc(value: Any) -> Any:
    """Attach UTC to a naive datetime rather than leaving it ambiguous."""
    if isinstance(value, dt.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


class AutotaskModel(BaseModel):
    """Shared configuration: ignore unknown fields, accept the vendor's casing."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ZoneInformation(AutotaskModel):
    """Which Autotask host serves this tenant."""

    zone_name: str | None = Field(default=None, alias="zoneName")
    url: str
    web_url: str | None = Field(default=None, alias="webUrl")
    ci: int | None = None


class PageDetails(AutotaskModel):
    """The paging half of every query response."""

    count: int | None = None
    request_count: int | None = Field(default=None, alias="requestCount")
    prev_page_url: str | None = Field(default=None, alias="prevPageUrl")
    next_page_url: str | None = Field(default=None, alias="nextPageUrl")


class PicklistValue(AutotaskModel):
    """One option of a per-tenant picklist. An option missing either half is unusable."""

    value: str | None = None
    label: str | None = None
    is_active: bool | None = Field(default=None, alias="isActive")


class FieldInformation(AutotaskModel):
    """One field as described by `entityInformation/fields`.

    `picklistValues` is `null`, not `[]`, on a field that is not a picklist.
    """

    name: str | None = None
    data_type: str | None = Field(default=None, alias="dataType")
    is_pick_list: bool | None = Field(default=None, alias="isPickList")
    picklist_values: list[PicklistValue] | None = Field(default=None, alias="picklistValues")

    @property
    def options(self) -> list[PicklistValue]:
        """The picklist's options, with `null` read as none."""
        return self.picklist_values or []


class AutotaskCompany(AutotaskModel):
    """A company: an end customer. The address and owner fields feed `get_company`."""

    id: int
    company_name: str | None = Field(default=None, alias="companyName")
    company_number: str | None = Field(default=None, alias="companyNumber")
    company_type: int | None = Field(default=None, alias="companyType")
    classification: int | None = None
    """A sales grade on the pilot tenant ("Managed Services", "A (hot)"), not an industry."""
    market_segment_id: int | None = Field(default=None, alias="marketSegmentID")
    """The industry ("Legal Services", "Healthcare"): what a business review wants."""
    is_active: bool | None = Field(default=None, alias="isActive")
    phone: str | None = None
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = Field(default=None, alias="postalCode")
    web_address: str | None = Field(default=None, alias="webAddress")
    owner_resource_id: int | None = Field(default=None, alias="ownerResourceID")
    parent_company_id: int | None = Field(default=None, alias="parentCompanyID")
    create_date: dt.datetime | None = Field(default=None, alias="createDate")
    last_activity_date: dt.datetime | None = Field(default=None, alias="lastActivityDate")

    _utc = field_validator("create_date", "last_activity_date", mode="before")(_as_utc)


class AutotaskCompanyLocation(AutotaskModel):
    """One of a company's premises. Live shape, 2026-09-21."""

    id: int
    company_id: int | None = Field(default=None, alias="companyID")
    name: str | None = None
    city: str | None = None
    state: str | None = None
    is_active: bool | None = Field(default=None, alias="isActive")
    is_primary: bool | None = Field(default=None, alias="isPrimary")


class AutotaskProject(AutotaskModel):
    """A project. Live shape, 2026-09-21; money fields are deliberately not modelled."""

    id: int
    project_name: str | None = Field(default=None, alias="projectName")
    project_number: str | None = Field(default=None, alias="projectNumber")
    company_id: int | None = Field(default=None, alias="companyID")
    status: int | None = None
    project_type: int | None = Field(default=None, alias="projectType")
    description: str | None = None
    completed_percentage: int | None = Field(default=None, alias="completedPercentage")
    start_date_time: dt.datetime | None = Field(default=None, alias="startDateTime")
    end_date_time: dt.datetime | None = Field(default=None, alias="endDateTime")
    completed_date_time: dt.datetime | None = Field(default=None, alias="completedDateTime")
    estimated_time: float | None = Field(default=None, alias="estimatedTime")
    actual_hours: float | None = Field(default=None, alias="actualHours")
    project_lead_resource_id: int | None = Field(default=None, alias="projectLeadResourceID")

    _utc = field_validator(
        "start_date_time", "end_date_time", "completed_date_time", mode="before"
    )(_as_utc)


class AutotaskPhase(AutotaskModel):
    """A phase of a project. It has no company: it is placed through its project."""

    id: int
    project_id: int | None = Field(default=None, alias="projectID")
    title: str | None = None
    phase_number: str | None = Field(default=None, alias="phaseNumber")
    start_date: dt.datetime | None = Field(default=None, alias="startDate")
    due_date: dt.datetime | None = Field(default=None, alias="dueDate")

    _utc = field_validator("start_date", "due_date", mode="before")(_as_utc)


class AutotaskContract(AutotaskModel):
    """A contract. Shape from the Swagger only: the pilot tenant's API user cannot
    query Contracts (HTTP 500 on every filter, api-notes), so this is unverified live."""

    id: int
    company_id: int | None = Field(default=None, alias="companyID")
    contract_name: str | None = Field(default=None, alias="contractName")
    status: int | None = None
    contract_type: int | None = Field(default=None, alias="contractType")
    start_date: dt.datetime | None = Field(default=None, alias="startDate")
    end_date: dt.datetime | None = Field(default=None, alias="endDate")

    _utc = field_validator("start_date", "end_date", mode="before")(_as_utc)


class AutotaskTicket(AutotaskModel):
    """A PSA ticket. `status` and `priority` are per-tenant picklist numbers."""

    id: int
    ticket_number: str | None = Field(default=None, alias="ticketNumber")
    title: str | None = None
    description: str | None = None
    status: int | None = None
    priority: int | None = None
    company_id: int | None = Field(default=None, alias="companyID")
    configuration_item_id: int | None = Field(default=None, alias="configurationItemID")
    contact_id: int | None = Field(default=None, alias="contactID")
    assigned_resource_id: int | None = Field(default=None, alias="assignedResourceID")
    queue_id: int | None = Field(default=None, alias="queueID")
    # Picklists, per tenant like every other: what kind of request it was and
    # where it came from (a person, or monitoring).
    issue_type: int | None = Field(default=None, alias="issueType")
    sub_issue_type: int | None = Field(default=None, alias="subIssueType")
    source: int | None = None
    create_date: dt.datetime | None = Field(default=None, alias="createDate")
    last_activity_date: dt.datetime | None = Field(default=None, alias="lastActivityDate")
    due_date_time: dt.datetime | None = Field(default=None, alias="dueDateTime")
    completed_date: dt.datetime | None = Field(default=None, alias="completedDate")
    resolution: str | None = None
    # SLA clocks. Live: dueDateTime and firstResponseDateTime on every open
    # ticket, firstResponseDueDateTime on most, resolvedDueDateTime on a third.
    first_response_due: dt.datetime | None = Field(default=None, alias="firstResponseDueDateTime")
    first_response_at: dt.datetime | None = Field(default=None, alias="firstResponseDateTime")
    resolved_due: dt.datetime | None = Field(default=None, alias="resolvedDueDateTime")
    resolved_at: dt.datetime | None = Field(default=None, alias="resolvedDateTime")
    sla_id: int | None = Field(default=None, alias="serviceLevelAgreementID")

    _utc = field_validator(
        "create_date",
        "last_activity_date",
        "due_date_time",
        "completed_date",
        "first_response_due",
        "first_response_at",
        "resolved_due",
        "resolved_at",
        mode="before",
    )(_as_utc)

    @property
    def is_open(self) -> bool:
        """Open means Autotask has not stamped a completion date on it."""
        return self.completed_date is None

    def age_days(self, *, now: dt.datetime | None = None) -> float | None:
        if self.create_date is None:
            return None
        return ((now or dt.datetime.now(dt.UTC)) - self.create_date).total_seconds() / 86_400


class AutotaskTicketNote(AutotaskModel):
    id: int
    ticket_id: int | None = Field(default=None, alias="ticketID")
    title: str | None = None
    description: str | None = None
    note_type: int | None = Field(default=None, alias="noteType")
    publish: int | None = None
    create_date_time: dt.datetime | None = Field(default=None, alias="createDateTime")
    creator_resource_id: int | None = Field(default=None, alias="creatorResourceID")

    _utc = field_validator("create_date_time", mode="before")(_as_utc)


class AutotaskServiceCall(AutotaskModel):
    """A scheduled visit or block of work. Shape from the Swagger only: the pilot
    tenant has never created one, so there is no live row to check it against."""

    id: int
    company_id: int | None = Field(default=None, alias="companyID")
    description: str | None = None
    start_date_time: dt.datetime | None = Field(default=None, alias="startDateTime")
    end_date_time: dt.datetime | None = Field(default=None, alias="endDateTime")
    duration: float | None = None
    status: int | None = None
    is_complete: int | bool | None = Field(default=None, alias="isComplete")

    _utc = field_validator("start_date_time", "end_date_time", mode="before")(_as_utc)


class AutotaskServiceCallTicket(AutotaskModel):
    """Joins a service call to a ticket."""

    id: int
    service_call_id: int | None = Field(default=None, alias="serviceCallID")
    ticket_id: int | None = Field(default=None, alias="ticketID")


class AutotaskServiceCallTicketResource(AutotaskModel):
    """Joins a service call's ticket to the resource doing the work."""

    id: int
    service_call_ticket_id: int | None = Field(default=None, alias="serviceCallTicketID")
    resource_id: int | None = Field(default=None, alias="resourceID")


class AutotaskContact(AutotaskModel):
    id: int
    company_id: int | None = Field(default=None, alias="companyID")
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    email_address: str | None = Field(default=None, alias="emailAddress")
    phone: str | None = None
    mobile_phone: str | None = Field(default=None, alias="mobilePhone")
    title: str | None = None
    primary_contact: bool | None = Field(default=None, alias="primaryContact")
    billing_contact: bool | None = Field(default=None, alias="billingContact")
    is_active: int | bool | None = Field(default=None, alias="isActive")
    last_activity_date: dt.datetime | None = Field(default=None, alias="lastActivityDate")

    _utc = field_validator("last_activity_date", mode="before")(_as_utc)

    @property
    def name(self) -> str | None:
        parts = [part for part in (self.first_name, self.last_name) if part]
        return " ".join(parts) if parts else None


class AutotaskConfigurationItem(AutotaskModel):
    """A device or asset. Autotask calls these configuration items."""

    id: int
    company_id: int | None = Field(default=None, alias="companyID")
    reference_title: str | None = Field(default=None, alias="referenceTitle")
    reference_number: str | None = Field(default=None, alias="referenceNumber")
    serial_number: str | None = Field(default=None, alias="serialNumber")
    rmm_hostname: str | None = Field(default=None, alias="rmmDeviceAuditHostname")
    rmm_last_user: str | None = Field(default=None, alias="rmmDeviceAuditLastUser")
    rmm_operating_system: str | None = Field(default=None, alias="rmmDeviceAuditOperatingSystem")
    rmm_ip_address: str | None = Field(default=None, alias="rmmDeviceAuditIPAddress")
    configuration_item_type: int | None = Field(default=None, alias="configurationItemType")
    install_date: dt.datetime | None = Field(default=None, alias="installDate")
    warranty_expiration_date: dt.datetime | None = Field(
        default=None, alias="warrantyExpirationDate"
    )
    is_active: bool | None = Field(default=None, alias="isActive")

    _utc = field_validator("install_date", "warranty_expiration_date", mode="before")(_as_utc)

    @property
    def name(self) -> str:
        """The best name Autotask gave us, never invented."""
        for candidate in (self.reference_title, self.rmm_hostname, self.reference_number):
            if candidate:
                return candidate
        return f"configuration item {self.id}"


class AutotaskResource(AutotaskModel):
    """An MSP staff member. Not client data — shared across every company."""

    id: int
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    email: str | None = None
    user_name: str | None = Field(default=None, alias="userName")
    title: str | None = None
    is_active: bool | None = Field(default=None, alias="isActive")

    @property
    def name(self) -> str:
        parts = [part for part in (self.first_name, self.last_name) if part]
        return " ".join(parts) if parts else f"resource {self.id}"

    def matches(self, wanted: str) -> bool:
        """Case-insensitive match on the full name, either name, or the login."""
        needle = wanted.strip().casefold()
        if not needle:
            return False
        haystacks = (self.name, self.first_name or "", self.last_name or "", self.user_name or "")
        return any(needle in value.casefold() for value in haystacks)


class AutotaskTimeEntry(AutotaskModel):
    """Logged work. Carries no company id — scoped through its ticket."""

    id: int
    ticket_id: int | None = Field(default=None, alias="ticketID")
    resource_id: int | None = Field(default=None, alias="resourceID")
    date_worked: dt.datetime | None = Field(default=None, alias="dateWorked")
    hours_worked: float | None = Field(default=None, alias="hoursWorked")
    summary_notes: str | None = Field(default=None, alias="summaryNotes")
    start_date_time: dt.datetime | None = Field(default=None, alias="startDateTime")

    _utc = field_validator("date_worked", "start_date_time", mode="before")(_as_utc)

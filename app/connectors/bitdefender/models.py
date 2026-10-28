"""Pydantic models for the GravityZone responses we read. Not the whole API.

Every field except an id is optional: the documentation declares nothing
required, and a partner tenant's rows arrive with whatever the endpoint has
reported. See docs/api-notes/bitdefender.md.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Modules named in the documentation, in the order the tools list them when
#: present, followed by the ones seen live that the documentation does not name.
#: **The set a tenant reports differs from the documentation** (live, 2026-09-22:
#: sixteen keys, seven undocumented, eight documented ones absent), so nothing
#: iterates this tuple as the truth: the module list is read from the rows
#: (`modules_seen`). It only orders what is seen.
DOCUMENTED_MODULES: tuple[str, ...] = (
    "antimalware",
    "advancedThreatControl",
    "advancedAntiExploit",
    "hyperDetect",
    "firewall",
    "networkAttackDefense",
    "contentControl",
    "deviceControl",
    "powerUser",
    "encryption",
    "edrSensor",
    "patchManagement",
    "sandboxAnalyzer",
    "integrityMonitoring",
    "exchange",
    "containerProtection",
    "relay",
    # seen live on the pilot tenant, not in the documentation
    "applicationControl",
    "antiTampering",
    "userControl",
    "antiphishing",
    "trafficScan",
    "remoteEnginesScanning",
    "riskManagement",
)

#: `getLicenseInfo` flag -> the module it licenses. With the flag false the module is
#: `not_licensed` on every endpoint of the company, whatever the row says or omits.
LICENSED_MODULES: dict[str, str] = {
    "encryption": "manageEncryption",
    "edrSensor": "manageEventCorrelator",
    "patchManagement": "managePatchManagement",
    "exchange": "manageExchange",
    "containerProtection": "manageContainerProtection",
    "hyperDetect": "manageHyperDetect",
    "sandboxAnalyzer": "manageSandboxAnalyzer",
    "integrityMonitoring": "manageIntegrityMonitoring",
}

ModuleStatus = Literal["enabled", "disabled", "not_licensed", "not_reported"]


def modules_seen(endpoints: Iterable[BdEndpoint]) -> list[str]:
    """Every module key any endpoint reports: documented order first, then the rest."""
    seen: set[str] = set()
    for endpoint in endpoints:
        seen.update(endpoint.modules or {})
    return [m for m in DOCUMENTED_MODULES if m in seen] + sorted(seen - set(DOCUMENTED_MODULES))


def module_status(endpoint: BdEndpoint, module: str, licence: BdLicense | None) -> ModuleStatus:
    """Three states beside enabled, and they are never conflated.

    `not_licensed`: the company's licence says the module cannot be on, so the
    row's value (false, or absent) is the licence, not a policy choice.
    `disabled`: licensed, and off by policy. `not_reported`: the row has no
    value and the licence does not say either way.
    """
    flag = LICENSED_MODULES.get(module)
    licensed = licence.flag(flag) if licence is not None and flag else None
    if licensed is False:
        return "not_licensed"
    value = endpoint.module_enabled(module)
    if value is True:
        return "enabled"
    if value is False:
        return "disabled"
    return "not_reported"


#: Modules that protect a workload the endpoint may simply not have. Their
#: being off is never a finding on its own (Exchange protection on a laptop).
WORKLOAD_MODULES: frozenset[str] = frozenset({"exchange", "containerProtection", "relay"})

#: The modules whose absence means "core protection off".
CORE_MODULES: tuple[str, ...] = ("antimalware", "advancedThreatControl")

#: `getNetworkInventoryItems` item types.
ITEM_COMPANY = 1
ITEM_COMPUTER = 5
ITEM_VIRTUAL_MACHINE = 6
ITEM_EC2 = 7
ENDPOINT_ITEM_TYPES = frozenset({ITEM_COMPUTER, ITEM_VIRTUAL_MACHINE, ITEM_EC2})

MACHINE_TYPES = {0: "other", 1: "computer", 2: "virtual machine", 3: "EC2 instance"}
AGENT_TYPES = {1: "Endpoint Security", 2: "Bitdefender Tools", 3: "BEST"}
LICENSE_STATES = {
    0: "pending authentication",
    1: "active",
    2: "expired",
    6: "no license or not applicable",
}
POWER_STATES = {0: "unknown", 1: "online", 2: "offline", 3: "suspended"}


def _iso(value: Any) -> dt.datetime | None:
    """GravityZone writes ISO-8601 with or without an offset; naive means UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    text = str(value).strip().replace(" ", "")  # one documented sample has spaces inside
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _percent(value: Any) -> float | None:
    """`"81%"` -> 81.0. The risk score arrives as a string with a percent sign."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().rstrip("%").strip()
    try:
        return float(text)
    except ValueError:
        return None


class BdRiskScore(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    value: float | None = None
    impact: str | None = None
    misconfigurations: float | None = None
    app_vulnerabilities: float | None = Field(default=None, alias="appVulnerabilities")
    human_risks: float | None = Field(default=None, alias="humanRisks")
    industry_modifier: float | None = Field(default=None, alias="industryModifier")

    _percents = field_validator(
        "value",
        "misconfigurations",
        "app_vulnerabilities",
        "human_risks",
        "industry_modifier",
        mode="before",
    )(_percent)


class BdPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str | None = None
    applied: bool | None = None


class BdLastScan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    date: dt.datetime | None = None

    _date = field_validator("date", mode="before")(_iso)


class BdCompany(BaseModel):
    """A company as `getCompaniesList` or `getNetworkInventoryItems` (type 1) reports it."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str | None = None
    type: int | None = Field(default=None, description="0 partner, 1 customer.")
    is_suspended: bool | None = Field(default=None, alias="isSuspended")
    can_be_managed_by_above: bool | None = Field(default=None, alias="canBeManagedByAbove")
    created_at: dt.datetime | None = Field(default=None, alias="createdAt")
    risk_score: BdRiskScore | None = Field(default=None, alias="riskScore")
    industry: int | None = None
    country: str | None = None

    _created = field_validator("created_at", mode="before")(_iso)


class BdEndpoint(BaseModel):
    """One endpoint row of `getNetworkInventoryItems`, `details` flattened in.

    `company_id` is what the guard classifies by. `modules` is the vendor's
    map of module name to enabled flag; a module the vendor did not report is
    absent, which is not the same as disabled.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str | None = None
    company_id: str | None = Field(default=None, alias="companyId")
    parent_id: str | None = Field(default=None, alias="parentId")
    type: int | None = None
    label: str | None = None
    fqdn: str | None = None
    group_id: str | None = Field(default=None, alias="groupId")
    is_managed: bool | None = Field(default=None, alias="isManaged")
    machine_type: int | None = Field(default=None, alias="machineType")
    operating_system_version: str | None = Field(default=None, alias="operatingSystemVersion")
    first_seen: dt.datetime | None = Field(default=None, alias="firstSeen")
    is_isolated: bool | None = Field(default=None, alias="isIsolated")
    ip: str | None = None
    macs: list[str] | None = None
    ssid: str | None = None
    managed_with_best: bool | None = Field(default=None, alias="managedWithBest")
    is_container_host: bool | None = Field(default=None, alias="isContainerHost")
    managed_exchange_server: bool | None = Field(default=None, alias="managedExchangeServer")
    managed_relay: bool | None = Field(default=None, alias="managedRelay")
    security_server: bool | None = Field(default=None, alias="securityServer")
    policy: BdPolicy | None = None
    modules: dict[str, bool] | None = None
    product_outdated: bool | None = Field(default=None, alias="productOutdated")
    last_successful_scan: BdLastScan | None = Field(default=None, alias="lastSuccessfulScan")
    risk_score: BdRiskScore | None = Field(default=None, alias="riskScore")

    _first_seen = field_validator("first_seen", mode="before")(_iso)

    @field_validator("modules", mode="before")
    @classmethod
    def _bool_modules(cls, value: Any) -> dict[str, bool] | None:
        if not isinstance(value, dict):
            return None
        return {str(k): bool(v) for k, v in value.items() if v is not None}

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> BdEndpoint:
        """A network inventory item, its `details` member merged into the row."""
        details = item.get("details")
        merged = {**(details if isinstance(details, dict) else {}), **item}
        return cls.model_validate(merged)

    @property
    def has_agent(self) -> bool:
        return bool(self.managed_with_best)

    @property
    def os_family(self) -> str | None:
        text = (self.operating_system_version or "").lower()
        if not text:
            return None
        if "windows" in text:
            return "Windows"
        if "mac" in text or "os x" in text or "darwin" in text:
            return "macOS"
        if any(word in text for word in ("linux", "ubuntu", "debian", "centos", "red hat", "rhel")):
            return "Linux"
        return "Other"

    def module_enabled(self, module: str) -> bool | None:
        """True, False, or None when the vendor did not report the module."""
        if self.modules is None:
            return None
        return self.modules.get(module)


class BdLicense(BaseModel):
    """`getLicenseInfo` for one company: the flags that decide which modules can exist."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    subscription_type: int | None = Field(default=None, alias="subscriptionType")
    assigned_protection_model: str | None = Field(default=None, alias="assignedProtectionModel")
    expiry_date: dt.datetime | None = Field(default=None, alias="expiryDate")
    used_slots: int | None = Field(default=None, alias="usedSlots")
    manage_encryption: bool | None = Field(default=None, alias="manageEncryption")
    manage_event_correlator: bool | None = Field(default=None, alias="manageEventCorrelator")
    manage_patch_management: bool | None = Field(default=None, alias="managePatchManagement")
    manage_exchange: bool | None = Field(default=None, alias="manageExchange")
    manage_container_protection: bool | None = Field(
        default=None, alias="manageContainerProtection"
    )
    manage_hyper_detect: bool | None = Field(default=None, alias="manageHyperDetect")
    manage_sandbox_analyzer: bool | None = Field(default=None, alias="manageSandboxAnalyzer")
    manage_integrity_monitoring: bool | None = Field(
        default=None, alias="manageIntegrityMonitoring"
    )

    _expiry = field_validator("expiry_date", mode="before")(_iso)

    @property
    def edr_licensed(self) -> bool | None:
        """`manageEventCorrelator`: whether incidents can exist for this company at all."""
        return self.manage_event_correlator

    def union(self, other: BdLicense) -> BdLicense:
        """The licence of a site with two companies: a module either company may have,
        the customer may have. A flag is True if either says so, False if either says
        so and neither says True, None when neither said."""
        merged: dict[str, object] = {}
        for field_name in type(self).model_fields:
            mine, theirs = getattr(self, field_name), getattr(other, field_name)
            if field_name.startswith("manage_"):
                merged[field_name] = (
                    True if True in (mine, theirs) else False if False in (mine, theirs) else None
                )
            elif field_name == "used_slots":
                merged[field_name] = (
                    (mine or 0) + (theirs or 0) if (mine, theirs) != (None, None) else None
                )
            else:
                merged[field_name] = mine if mine is not None else theirs
        return BdLicense.model_validate(merged)

    def flag(self, name: str) -> bool | None:
        """A `manage*` flag by its vendor name, or None when the licence did not say."""
        for field_name, info in type(self).model_fields.items():
            if info.alias == name:
                value = getattr(self, field_name)
                return bool(value) if value is not None else None
        return None


class BdAgent(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    engine_version: str | None = Field(default=None, alias="engineVersion")
    primary_engine: int | None = Field(default=None, alias="primaryEngine")
    last_update: dt.datetime | None = Field(default=None, alias="lastUpdate")
    licensed: int | None = None
    product_outdated: bool | None = Field(default=None, alias="productOutdated")
    product_update_disabled: bool | None = Field(default=None, alias="productUpdateDisabled")
    product_version: str | None = Field(default=None, alias="productVersion")
    signature_outdated: bool | None = Field(default=None, alias="signatureOutdated")
    signature_update_disabled: bool | None = Field(default=None, alias="signatureUpdateDisabled")
    type: int | None = None

    _updated = field_validator("last_update", mode="before")(_iso)


class BdMalwareStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    detection: bool | None = None
    infected: bool | None = None


class BdEndpointDetail(BaseModel):
    """`getManagedEndpointDetails`: what one machine's protection looks like now."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str | None = None
    company_id: str | None = Field(default=None, alias="companyId")
    operating_system: str | None = Field(default=None, alias="operatingSystem")
    state: int | None = None
    ip: str | None = None
    first_seen: dt.datetime | None = Field(default=None, alias="firstSeen")
    last_seen: dt.datetime | None = Field(default=None, alias="lastSeen")
    is_isolated: bool | None = Field(default=None, alias="isIsolated")
    machine_type: int | None = Field(default=None, alias="machineType")
    agent: BdAgent | None = None
    malware_status: BdMalwareStatus | None = Field(default=None, alias="malwareStatus")
    policy: BdPolicy | None = None
    modules: dict[str, bool] | None = None
    label: str | None = None
    managed_with_best: bool | None = Field(default=None, alias="managedWithBest")
    managed_exchange_server: bool | None = Field(default=None, alias="managedExchangeServer")
    managed_relay: bool | None = Field(default=None, alias="managedRelay")
    security_server: bool | None = Field(default=None, alias="securityServer")
    risk_score: BdRiskScore | None = Field(default=None, alias="riskScore")
    last_successful_scan: BdLastScan | None = Field(default=None, alias="lastSuccessfulScan")

    _first_seen = field_validator("first_seen", mode="before")(_iso)
    _last_seen = field_validator("last_seen", mode="before")(_iso)

    @field_validator("modules", mode="before")
    @classmethod
    def _bool_modules(cls, value: Any) -> dict[str, bool] | None:
        if not isinstance(value, dict):
            return None
        return {str(k): bool(v) for k, v in value.items() if v is not None}


class BdIncidentCompany(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str | None = None


class BdIncidentDetails(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    detection_name: str | None = Field(default=None, alias="detectionName")
    computer_id: str | None = Field(default=None, alias="computerId")
    computer_name: str | None = Field(default=None, alias="computerName")
    computer_fqdn: str | None = Field(default=None, alias="computerFqdn")
    computer_ip: str | None = Field(default=None, alias="computerIp")


class BdIncident(BaseModel):
    """One row of `getIncidentsList` (v1.2): an EDR/XDR detection."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    incident_id: str = Field(alias="incidentId")
    incident_number: int | None = Field(default=None, alias="incidentNumber")
    incident_type: str | None = Field(default=None, alias="incidentType")
    company: BdIncidentCompany | None = None
    status: str | None = None
    main_action: str | None = Field(default=None, alias="mainAction")
    created: dt.datetime | None = None
    last_updated: dt.datetime | None = Field(default=None, alias="lastUpdated")
    severity_score: int | None = Field(default=None, alias="severityScore")
    priority: str | None = None
    attack_types: list[str] | None = Field(default=None, alias="attackTypes")
    incident_link: str | None = Field(default=None, alias="incidentLink")
    details: BdIncidentDetails | None = None

    _created = field_validator("created", mode="before")(_iso)
    _updated = field_validator("last_updated", mode="before")(_iso)

    @property
    def company_id(self) -> str | None:
        return self.company.id if self.company else None

    @property
    def endpoint_id(self) -> str | None:
        return self.details.computer_id if self.details else None

    @property
    def endpoint_name(self) -> str | None:
        return self.details.computer_name if self.details else None

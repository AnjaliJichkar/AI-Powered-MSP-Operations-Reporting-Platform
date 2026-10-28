"""Pydantic models for the parts of the NinjaOne API we actually use.

Deliberately partial: the vendor's device object has two dozen fields and we
read six. Extra keys are ignored so a NinjaOne release cannot break us.

Timestamps (`lastContact`, `createTime`) come back as Unix epoch *seconds* in a
JSON number, not ISO strings — see docs/api-notes/ninjaone.md.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# A value larger than this is not plausible as epoch seconds (it is year 5138),
# and almost certainly means the vendor switched to milliseconds.
_MAX_PLAUSIBLE_EPOCH_SECONDS = 100_000_000_000


def epoch_to_datetime(value: float | None) -> dt.datetime | None:
    """Convert a NinjaOne epoch-seconds number to an aware UTC datetime."""
    if value is None:
        return None
    if value <= 0 or value > _MAX_PLAUSIBLE_EPOCH_SECONDS:
        # Refuse to invent a date. The caller reports the field as unknown.
        return None
    return dt.datetime.fromtimestamp(value, tz=dt.UTC)


class NinjaDevice(BaseModel):
    """A device (NinjaOne calls it a node) as returned by the devices endpoints."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    organization_id: int | None = Field(default=None, alias="organizationId")
    system_name: str | None = Field(default=None, alias="systemName")
    display_name: str | None = Field(default=None, alias="displayName")
    dns_name: str | None = Field(default=None, alias="dnsName")
    node_class: str | None = Field(default=None, alias="nodeClass")
    location_id: int | None = Field(default=None, alias="locationId")
    offline: bool | None = None
    last_contact: dt.datetime | None = Field(default=None, alias="lastContact")
    created: dt.datetime | None = None
    """When NinjaOne first enrolled the device. Not a purchase date: a device can
    be years old when it is enrolled, so this is only a lower bound on its age."""

    @field_validator("last_contact", "created", mode="before")
    @classmethod
    def _parse_last_contact(cls, value: Any) -> Any:
        if isinstance(value, int | float):
            return epoch_to_datetime(float(value))
        return value

    @property
    def name(self) -> str:
        """The best human-readable name NinjaOne gave us, never invented."""
        for candidate in (self.system_name, self.display_name, self.dns_name):
            if candidate:
                return candidate
        return f"device {self.id}"

    @property
    def hostname(self) -> str:
        """The name the coverage join keys on: the host part of the full `dnsName`
        where NinjaOne has one, else `systemName`, else the display name, never
        invented.

        `systemName` is the 15-character NetBIOS name, so two machines whose
        names differ only after the fifteenth character share it (live: 77 of
        1,024 computers on the pilot carry a longer `dnsName`); `dnsName` is the
        full name, with a domain the join strips. Where the two agree, the
        spelling is `systemName`'s, the one an engineer sees in the console.
        """
        label = (self.dns_name or "").strip().split(".", 1)[0]
        system = (self.system_name or "").strip()
        if label and system and label.lower() == system.lower():
            return system
        for candidate in (label, system, (self.display_name or "").strip()):
            if candidate:
                return candidate
        return f"device {self.id}"

    @property
    def os_family(self) -> str | None:
        """The operating-system family, read off `nodeClass`.

        The org-scoped devices endpoint carries no OS string, only the node
        class, so this is Windows/macOS/Linux/... rather than a build number.
        None when NinjaOne gave no class; "Other" for a class we do not map.
        """
        return os_family_of(self.node_class)

    def days_since_contact(self, *, now: dt.datetime | None = None) -> float | None:
        if self.last_contact is None:
            return None
        reference = now or dt.datetime.now(dt.UTC)
        return (reference - self.last_contact).total_seconds() / 86_400


#: `nodeClass` prefixes -> OS family. Order matters: first match wins.
_OS_FAMILIES: tuple[tuple[str, str], ...] = (
    ("WINDOWS", "Windows"),
    ("MAC", "macOS"),
    ("APPLE_IOS", "iOS"),
    ("LINUX", "Linux"),
    ("ANDROID", "Android"),
    ("VMWARE", "VMware"),
    ("HYPERV", "Hyper-V"),
    ("NMS", "Network device"),
)


def os_family_of(node_class: str | None) -> str | None:
    if not node_class:
        return None
    upper = node_class.upper()
    for prefix, family in _OS_FAMILIES:
        if upper.startswith(prefix):
            return family
    return "Other"


class NinjaAlertDevice(BaseModel):
    """The device summary embedded in an alert. Carries the org id we scope on."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int | None = None
    organization_id: int | None = Field(default=None, alias="organizationId")
    system_name: str | None = Field(default=None, alias="systemName")
    display_name: str | None = Field(default=None, alias="displayName")

    @property
    def name(self) -> str | None:
        return self.system_name or self.display_name


class NinjaAlert(BaseModel):
    """An active alert (NinjaOne calls it a triggered condition)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    uid: str | None = None
    device_id: int | None = Field(default=None, alias="deviceId")
    message: str | None = None
    subject: str | None = None
    severity: str | None = None
    priority: str | None = None
    source_type: str | None = Field(default=None, alias="sourceType")
    condition_name: str | None = Field(default=None, alias="conditionName")
    create_time: dt.datetime | None = Field(default=None, alias="createTime")
    device: NinjaAlertDevice | None = None

    @field_validator("create_time", mode="before")
    @classmethod
    def _parse_create_time(cls, value: Any) -> Any:
        if isinstance(value, int | float):
            return epoch_to_datetime(float(value))
        return value

    @property
    def organization_id(self) -> int | None:
        return self.device.organization_id if self.device else None


def _epoch_field(value: Any) -> Any:
    if isinstance(value, int | float):
        return epoch_to_datetime(float(value))
    return value


class NinjaDeviceOS(BaseModel):
    """The `os` block of a device detail, or one `operating-systems` query row."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    manufacturer: str | None = None
    name: str | None = None
    architecture: str | None = None
    build_number: str | None = Field(default=None, alias="buildNumber")
    release_id: str | None = Field(default=None, alias="releaseId")
    last_boot_time: dt.datetime | None = Field(default=None, alias="lastBootTime")
    needs_reboot: bool | None = Field(default=None, alias="needsReboot")

    _boot = field_validator("last_boot_time", mode="before")(_epoch_field)


class NinjaDeviceSystem(BaseModel):
    """The `system` block of a device detail: the hardware."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = Field(default=None, alias="serialNumber")
    bios_serial_number: str | None = Field(default=None, alias="biosSerialNumber")
    domain: str | None = None
    domain_role: str | None = Field(default=None, alias="domainRole")
    number_of_processors: int | None = Field(default=None, alias="numberOfProcessors")
    total_physical_memory: int | None = Field(default=None, alias="totalPhysicalMemory")
    virtual_machine: bool | None = Field(default=None, alias="virtualMachine")
    chassis_type: str | None = Field(default=None, alias="chassisType")


class NinjaVolume(BaseModel):
    """One disk volume, from a device detail or the `volumes` query."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = None
    drive_letter: str | None = Field(default=None, alias="driveLetter")
    label: str | None = None
    device_type: str | None = Field(default=None, alias="deviceType")
    file_system: str | None = Field(default=None, alias="fileSystem")
    capacity: int | None = None
    free_space: int | None = Field(default=None, alias="freeSpace")
    device_id: int | None = Field(default=None, alias="deviceId")

    @property
    def free_percent(self) -> float | None:
        if not self.capacity or self.free_space is None:
            return None
        return round(self.free_space / self.capacity * 100, 1)


class NinjaDeviceDetail(NinjaDevice):
    """`GET /v2/device/{id}`: the base device plus the blocks the spec omits.

    `os`, `system`, `memory`, `processors`, `volumes` and `lastLoggedInUser`
    are not in the spec's schema for this endpoint but are sent live
    (api-notes). There is no agent-version field anywhere in the response.
    """

    ip_addresses: list[str] = Field(default_factory=list, alias="ipAddresses")
    mac_addresses: list[str] = Field(default_factory=list, alias="macAddresses")
    public_ip: str | None = Field(default=None, alias="publicIP")
    approval_status: str | None = Field(default=None, alias="approvalStatus")
    device_type: str | None = Field(default=None, alias="deviceType")
    last_logged_in_user: str | None = Field(default=None, alias="lastLoggedInUser")
    os: NinjaDeviceOS | None = None
    system: NinjaDeviceSystem | None = None
    volumes: list[NinjaVolume] = Field(default_factory=list)


class NinjaOSPatch(BaseModel):
    """One row of `/v2/queries/os-patches` (or a device's own os-patches)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = None
    name: str | None = None
    kb_number: str | None = Field(default=None, alias="kbNumber")
    severity: str | None = None
    status: str | None = None
    type: str | None = None
    device_id: int | None = Field(default=None, alias="deviceId")


class NinjaSoftwarePatch(BaseModel):
    """One row of `/v2/queries/software-patches`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = None
    title: str | None = None
    impact: str | None = None
    status: str | None = None
    type: str | None = None
    device_id: int | None = Field(default=None, alias="deviceId")


class NinjaAntivirusStatus(BaseModel):
    """One row of `/v2/queries/antivirus-status`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    product_name: str | None = Field(default=None, alias="productName")
    product_state: str | None = Field(default=None, alias="productState")
    definition_status: str | None = Field(default=None, alias="definitionStatus")
    version: str | None = None
    device_id: int | None = Field(default=None, alias="deviceId")

    @property
    def healthy(self) -> bool:
        """On, with current definitions. Anything else needs a look."""
        state = (self.product_state or "").upper()
        definitions = (self.definition_status or "").replace("-", "").replace(" ", "").upper()
        return state == "ON" and definitions == "UPTODATE"


class NinjaSoftware(BaseModel):
    """One row of `/v2/queries/software`: one installed application on one device."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = None
    publisher: str | None = None
    version: str | None = None
    install_date: str | None = Field(default=None, alias="installDate")
    """A date string, not an epoch, unlike every other timestamp (api-notes)."""
    device_id: int | None = Field(default=None, alias="deviceId")


class NinjaLoggedOnUser(BaseModel):
    """One row of `/v2/queries/logged-on-users`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user_name: str | None = Field(default=None, alias="userName")
    logon_time: dt.datetime | None = Field(default=None, alias="logonTime")
    device_id: int | None = Field(default=None, alias="deviceId")

    _logon = field_validator("logon_time", mode="before")(_epoch_field)


class NinjaOperatingSystemRow(NinjaDeviceOS):
    """One row of `/v2/queries/operating-systems`: an OS block plus the device."""

    device_id: int | None = Field(default=None, alias="deviceId")


class NinjaActivity(BaseModel):
    """One entry of a device's activity log."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int | None = None
    activity_time: dt.datetime | None = Field(default=None, alias="activityTime")
    device_id: int | None = Field(default=None, alias="deviceId")
    activity_type: str | None = Field(default=None, alias="activityType")
    status_code: str | None = Field(default=None, alias="statusCode")
    status: str | None = None
    message: str | None = None
    type: str | None = None

    _time = field_validator("activity_time", mode="before")(_epoch_field)


class NinjaPatchInstall(BaseModel):
    """One row of `/v2/queries/os-patch-installs`: an installation attempt and how it ended."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = None
    name: str | None = None
    kb_number: str | None = Field(default=None, alias="kbNumber")
    severity: str | None = None
    status: str | None = None
    type: str | None = None
    installed_at: dt.datetime | None = Field(default=None, alias="installedAt")
    """When the attempt happened. For a FAILED row, when it failed."""
    device_id: int | None = Field(default=None, alias="deviceId")

    _at = field_validator("installed_at", mode="before")(_epoch_field)


class NinjaSoftwarePatchInstall(BaseModel):
    """One row of `/v2/queries/software-patch-installs`: a third-party patch attempt.

    Live shape (2026-09-29): `id, productIdentifier, title, impact, status, type,
    installedAt, deviceId, timestamp`; status `INSTALLED` or `FAILED`, type
    `PATCH` or `INSTALLER`, impact `recommended` or `unknown`. `title` is the
    product, not a KB.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str | None = None
    product_identifier: str | None = Field(default=None, alias="productIdentifier")
    title: str | None = None
    impact: str | None = None
    status: str | None = None
    type: str | None = None
    installed_at: dt.datetime | None = Field(default=None, alias="installedAt")
    device_id: int | None = Field(default=None, alias="deviceId")

    _at = field_validator("installed_at", mode="before")(_epoch_field)


class NinjaWindowsService(BaseModel):
    """One row of `/v2/queries/windows-services`."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = None
    display_name: str | None = Field(default=None, alias="displayName")
    start_type: str | None = Field(default=None, alias="startType")
    user_name: str | None = Field(default=None, alias="userName")
    state: str | None = None
    device_id: int | None = Field(default=None, alias="deviceId")

    @property
    def should_be_running(self) -> bool:
        """Set to start automatically. A stopped one of these is the interesting kind."""
        return (self.start_type or "").upper().startswith("AUTO_START")


class NinjaComputerSystem(NinjaDeviceSystem):
    """One row of `/v2/queries/computer-systems`: the `system` block plus the device.

    There is no purchase, BIOS or warranty date on this report (api-notes), so
    nothing here can say how old a machine is.
    """

    device_id: int | None = Field(default=None, alias="deviceId")


class NinjaProcessor(BaseModel):
    """One row of `/v2/queries/processors`. Live shape, 2026-09-21."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = None
    architecture: str | None = None
    num_cores: int | None = Field(default=None, alias="numCores")
    device_id: int | None = Field(default=None, alias="deviceId")


class NinjaLocation(BaseModel):
    """One entry of `/v2/organization/{id}/locations`: a customer's premises."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    name: str | None = None
    address: str | None = None
    description: str | None = None


class NinjaCustomFieldDefinition(BaseModel):
    """One entry of `/v2/device-custom-fields`: a field defined on this tenant."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    label: str | None = None
    description: str | None = None
    type: str | None = None
    scope: str | None = None
    active: bool | None = None
    system: bool | None = None

    @property
    def is_encrypted(self) -> bool:
        return "ENCRYPT" in (self.type or "").upper() or "SECURE" in (self.type or "").upper()


class NinjaAccessToken(BaseModel):
    """The OAuth token response. `access_token` is never logged or persisted."""

    model_config = ConfigDict(extra="ignore")

    access_token: str
    expires_in: int = 3600
    token_type: str = "bearer"  # noqa: S105 - the OAuth token *type*, not a token
    scope: str | None = None

    def __repr__(self) -> str:
        return f"<NinjaAccessToken expires_in={self.expires_in} scope={self.scope!r}>"

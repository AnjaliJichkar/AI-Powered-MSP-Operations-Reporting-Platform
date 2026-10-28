"""The demo world: one fictional franchisee, eight customers, generated from a seed.

Everything is a plain dataclass with vendor-neutral fields; the `*_api.py`
modules shape them into each vendor's JSON. Time is stored as **days ago**
(floats) and resolved against the clock when a response is built, so the
world is always the last ninety days.

The generator is deterministic: `build_world(seed)` returns the same world
every time for the same seed, on every machine. The scenarios in
`scenarios.py` are placed on purpose, by name, not left to chance, and
`scenarios.check(world)` proves each one is present.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from app.demo import vocabulary as V

DAYS = 90
"""How far back the ticket, patch, alert and detection history runs."""

NodeClass = Literal[
    "WINDOWS_WORKSTATION", "WINDOWS_SERVER", "MAC", "LINUX_SERVER", "VMWARE_VM_GUEST", "NMS_SWITCH"
]


@dataclass(slots=True)
class Person:
    id: int
    first: str
    last: str
    customer_key: str

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"

    @property
    def login(self) -> str:
        return f"{self.first[0]}{self.last}".lower()

    @property
    def email(self) -> str:
        return f"{self.first}.{self.last}@{self.customer_key}.example".lower()


@dataclass(slots=True)
class Location:
    id: int
    name: str
    address: str


@dataclass(slots=True)
class Volume:
    letter: str
    capacity: int
    free: int
    bitlocker: str | None
    """`Encrypted`, `Decrypted`, or None when the device reports no BitLocker state."""


@dataclass(slots=True)
class Device:
    id: int
    customer_key: str
    system_name: str
    """What NinjaOne calls `systemName`: the NetBIOS name, 15 characters at most."""
    dns_name: str
    node_class: NodeClass
    os_name: str
    manufacturer: str
    model: str
    processor: str
    memory_gb: int
    serial: str
    macs: list[str]
    location_id: int
    last_contact_days: float | None
    """None: NinjaOne has never heard from it since enrolment."""
    created_days: float
    volumes: list[Volume] = field(default_factory=list)
    last_user: Person | None = None
    virtual: bool = False
    pending_reboot: bool = False
    in_bitdefender: bool = True
    """Whether GravityZone holds a row for it (False: the "no protection agent" gap)."""
    in_cove: bool = False
    """Whether Cove holds a device for it. Off by default: backup is sold per
    machine rather than rolled out to the fleet, so most workstations have no
    Cove device and that is not a finding about them."""
    cove_last_success_days: float | None = None
    """Days since the last backup that completed. None where Cove holds a
    device that has never had one, which is 0% and not "no failures"."""
    cove_bad_days: int = 0
    """How many of the last 28 days failed or had no session at all. The
    colour bar the fake draws from this is what the review's rate counts."""
    cove_errors: int = 0
    """Files the last session could not read. A session that completed with
    errors is a success for the rate and still something somebody looks at."""
    bitdefender_last_seen_days: float | None = None
    """GravityZone's own last-seen; None when it follows NinjaOne's last contact."""
    bitdefender_company_index: int = 0
    """Which of the customer's GravityZone companies holds it (Fabrikam has two)."""
    in_connectsecure: bool = True
    vendor_identifiers: bool = True
    """Whether the tools' rows carry the MAC and serial. False for the twins nothing can tell
    apart: the name pass is ambiguous and no key decides, so both stay `unmatched`."""
    connectsecure_scan_days: float | None = None
    """Days since the last vulnerability scan; None when it follows NinjaOne's contact."""
    scenario: str = ""
    """The scenario this device was placed for, for the scenario check. Empty: ordinary."""

    @property
    def hostname(self) -> str:
        return self.dns_name.split(".")[0]

    @property
    def is_server(self) -> bool:
        return "SERVER" in self.node_class or "VM_GUEST" in self.node_class

    @property
    def is_agent_class(self) -> bool:
        return self.node_class != "NMS_SWITCH"


@dataclass(slots=True)
class VendorOnlyEndpoint:
    """A machine a tool holds with no NinjaOne device: `unmanaged`, `discovered` or
    `external` on the coverage board, depending on what it is."""

    customer_key: str
    name: str
    tool: Literal["bitdefender", "connectsecure"]
    kind: Literal["unmanaged", "default_name", "external", "discovered"]
    mac: str
    last_seen_days: float


@dataclass(slots=True)
class Ticket:
    id: int
    customer_key: str
    number: str
    title: str
    issue_type: str
    sub_issue_type: str
    contact: Person | None
    """None: raised by monitoring or automation."""
    created_days: float
    completed_days: float | None
    first_response_days: float | None
    first_response_due_days: float
    resolution_due_days: float
    priority: str
    status: str
    device: Device | None
    description: str
    resolution: str
    scenario: str = ""


@dataclass(slots=True)
class PatchInstall:
    id: int
    device_id: int
    kind: Literal["os", "software"]
    identifier: str
    """The KB number for an OS patch; the product identifier for third-party software."""
    title: str
    severity: str
    status: Literal["INSTALLED", "FAILED"]
    installed_days: float


@dataclass(slots=True)
class PendingPatch:
    id: int
    device_id: int
    kind: Literal["os", "software"]
    identifier: str
    title: str
    severity: str
    status: Literal["APPROVED", "REJECTED", "MANUAL"]
    version: str = ""


@dataclass(slots=True)
class Alert:
    id: int
    device_id: int
    message: str
    severity: str
    created_days: float
    resolved_days: float | None


@dataclass(slots=True)
class Detection:
    id: str
    customer_key: str
    device_id: int
    threat: str
    action: str
    detected_days: float


@dataclass(slots=True)
class Finding:
    """One ConnectSecure vulnerability on one asset."""

    device_id: int
    cve: str
    product: str
    severity: str
    kev: bool
    confirmed: bool
    fix: str
    """The remediation text: a target version, or `Uninstall`."""
    remediation_type: Literal["Update", "Uninstall"]


@dataclass(slots=True)
class M365Account:
    """One account in a customer's Microsoft 365 tenant.

    A person's account, a shared mailbox or a room: a shared mailbox has no
    person behind it, is never licensed and has no second factor, which is
    correct rather than a finding.
    """

    upn: str
    display_name: str
    person_id: int | None
    enabled: bool
    synced: bool
    """Synced from an on-premises Active Directory rather than cloud-only."""
    user_type: str
    mfa_methods: list[str]
    """Empty means no second factor is registered."""
    licence_skus: list[str]
    created_days: float
    last_sign_in_days: float | None
    """None: never signed in, or no sign-in to report."""
    department: str | None
    job_title: str | None
    office: str | None
    mailbox_purpose: str
    """`user`, `shared` or `room`, as Graph's `userPurpose` reports it."""
    mailbox_bytes: int | None
    group_ids: list[str] = field(default_factory=list)
    manager_upn: str | None = None


@dataclass(slots=True)
class M365Group:
    id: str
    name: str
    mail: str | None
    unified: bool
    """A Microsoft 365 group rather than a security group."""
    team: bool
    security: bool
    member_upns: list[str] = field(default_factory=list)
    created_days: float = 400.0


@dataclass(slots=True)
class Customer:
    key: str
    name: str
    industry: str
    ninja_org_id: int
    autotask_company_id: int
    bitdefender_company_ids: list[str]
    """Zero, one or two GravityZone companies (an AV-only customer still has one)."""
    connectsecure_company_id: int | None
    locations: list[Location]
    people: list[Person]
    m365_tenant_id: str = ""
    """The customer's own Entra tenant. Each one is consented separately, so
    it is the site's mapping rather than anything the franchisee holds."""
    m365_accounts: list[M365Account] = field(default_factory=list)
    m365_groups: list[M365Group] = field(default_factory=list)
    m365_premium: bool = True
    """Whether the tenant has an Entra ID P1/P2 licence. Without one its
    sign-in activity cannot be read at all, which the fake refuses exactly as
    Graph does."""
    cove_partner_id: int | None = None
    """The customer's own partner beneath the franchisee's at Cove. None for a
    customer who does not buy backup, which is a real shape: the board says
    `not assessed` for them rather than counting every machine as a gap."""
    edr_licensed: bool = False
    av_only: bool = False
    """GravityZone licence manages antimalware only: no patch, encryption or EDR modules."""
    scenario: str = ""


@dataclass(slots=True)
class World:
    seed: int
    franchisee: str
    customers: list[Customer]
    internal_organisation: str
    devices: list[Device]
    vendor_only: list[VendorOnlyEndpoint]
    tickets: list[Ticket]
    installs: list[PatchInstall]
    pending: list[PendingPatch]
    alerts: list[Alert]
    detections: list[Detection]
    findings: list[Finding]
    notes: list[dict[str, Any]] = field(default_factory=list)
    """Ticket notes the fake Autotask accepted, in the order they were posted. The
    only thing in the world that a connector wrote rather than read."""

    def customer(self, key: str) -> Customer:
        return next(c for c in self.customers if c.key == key)

    def devices_of(self, key: str) -> list[Device]:
        return [d for d in self.devices if d.customer_key == key]

    def device(self, device_id: int) -> Device:
        return next(d for d in self.devices if d.id == device_id)

    def summary(self) -> dict[str, int]:
        """The dataset's size, for the scenario document and the terminal."""
        return {
            "customers": len(self.customers),
            "devices": len(self.devices),
            "vendor_only_endpoints": len(self.vendor_only),
            "tickets": len(self.tickets),
            "patch_installs": len(self.installs),
            "pending_patches": len(self.pending),
            "alerts": len(self.alerts),
            "detections": len(self.detections),
            "vulnerability_findings": len(self.findings),
            "people": sum(len(c.people) for c in self.customers),
            "microsoft365_accounts": sum(len(c.m365_accounts) for c in self.customers),
            "microsoft365_groups": sum(len(c.m365_groups) for c in self.customers),
        }


# --- time -------------------------------------------------------------------------------------


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def at(days_ago: float | None, now: dt.datetime | None = None) -> dt.datetime | None:
    """The moment `days_ago` days before `now`; None stays None."""
    if days_ago is None:
        return None
    return (now or now_utc()) - dt.timedelta(days=days_ago)


def epoch(days_ago: float | None, now: dt.datetime | None = None) -> float | None:
    moment = at(days_ago, now)
    return None if moment is None else moment.timestamp()


def iso(days_ago: float | None, now: dt.datetime | None = None) -> str | None:
    moment = at(days_ago, now)
    return None if moment is None else moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the generator ------------------------------------------------------------------------------


def _key(name: str) -> str:
    return name.split()[0].lower()


def _prefix(name: str) -> str:
    word = name.split()[0].upper()
    return {"ADVENTURE": "AWC", "FOURTH": "FCF", "NORTHWIND": "NWT"}.get(word, word[:3])


def _mac(rng: random.Random) -> str:
    return "00:1B:" + ":".join(f"{rng.randrange(256):02X}" for _ in range(4))


def _serial(rng: random.Random, manufacturer: str) -> str:
    if manufacturer == "Apple":
        return "".join(rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", k=10))
    if manufacturer == "Dell Inc.":
        return "".join(rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", k=7))
    return "".join(rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", k=10))


def _people(rng: random.Random, key: str, count: int, start: int) -> list[Person]:
    seen: set[tuple[str, str]] = set()
    people: list[Person] = []
    while len(people) < count:
        pair = (rng.choice(V.GIVEN_NAMES), rng.choice(V.SURNAMES))
        if pair in seen:
            continue
        seen.add(pair)
        people.append(Person(start + len(people), pair[0], pair[1], key))
    return people


#: The subscriptions the demo tenants hold, by Microsoft's own part numbers.
#: The ids are the real published SKU ids, which are public and are the same
#: in every tenant; nothing here came from one.
DEMO_SKUS: tuple[tuple[str, str, int], ...] = (
    ("6fd2c87f-b296-42f0-b197-1e91e994b900", "ENTERPRISEPACK", 0),
    ("05e9a617-0261-4cee-bb44-138d3ef5d965", "SPE_E3", 0),
    ("4b585984-651b-448a-9e53-3b10f069cf7f", "DESKLESSPACK", 0),
    ("c5928f49-12ba-48f7-ada3-0d743a3601d5", "VISIOCLIENT", 0),
)

#: What a demo account registers as a second factor. The first is strong, the
#: second is a phone, which Graph reports as registered but not MFA-capable.
DEMO_METHODS = ("microsoftAuthenticatorPush", "mobilePhone")


def _m365(
    rng: random.Random, customer_key: str, name: str, people: list[Person], index: int
) -> tuple[str, list[M365Account], list[M365Group]]:
    """One customer's Microsoft 365 tenant, built from the people who already
    exist, so a ticket's contact and a device's last logged-on user are the
    same person here (`docs/demo-tenant.md`).

    What it is built to contain, because each is something a page or a finding
    has to be seen handling: mostly cloud-only accounts and a few synced from
    an on-premises directory; a few people with no second factor; one disabled
    account that still has a licence; a guest; an account nobody has signed in
    to for months; two shared mailboxes and a room, which are not people and
    must not be counted as unlicensed; a Team, a Microsoft 365 group, a
    distribution list and a security group.
    """
    tenant = f"{index:08x}-d3m0-4a5e-9c1b-{index:012x}"
    departments = ["Finance", "Operations", "Sales", "Support"]
    titles = ["Manager", "Coordinator", "Technician", "Director"]
    offices = ["Head Office", "Branch"]
    accounts: list[M365Account] = []
    # Every third tenant is synced from an on-premises directory; the rest are
    # cloud-only, which is what most small customers are.
    synced_tenant = index % 3 == 0
    for n, person in enumerate(people):
        enabled = True
        licences = [DEMO_SKUS[0][0]] if n % 4 != 3 else [DEMO_SKUS[2][0]]
        last_seen: float | None = round(rng.uniform(0.1, 6.0), 2)
        methods = list(DEMO_METHODS[:1])
        if n % 5 == 2:
            methods = []  # no second factor at all
        elif n % 7 == 4:
            methods = [DEMO_METHODS[1]]  # a phone only: registered, not capable
        if n == 1:
            enabled = False  # one disabled account, licence still assigned
        if n == 3:
            last_seen = round(rng.uniform(45.0, 120.0), 2)  # nobody has signed in for months
        accounts.append(
            M365Account(
                upn=person.email,
                display_name=person.name,
                person_id=person.id,
                enabled=enabled,
                synced=synced_tenant and n % 2 == 0,
                user_type="Member",
                mfa_methods=methods,
                licence_skus=licences,
                created_days=round(rng.uniform(200.0, 1500.0), 1),
                last_sign_in_days=last_seen,
                department=departments[n % len(departments)],
                job_title=titles[n % len(titles)],
                office=offices[n % len(offices)],
                mailbox_purpose="user",
                mailbox_bytes=int(rng.uniform(0.4, 28.0) * 10**9),
                manager_upn=people[0].email if n else None,
            )
        )
    # A guest: somebody else's identity, in this tenant to share a document.
    accounts.append(
        M365Account(
            upn=f"external.partner_{customer_key}#EXT#@{customer_key}.example".lower(),
            display_name="External Partner",
            person_id=None,
            enabled=True,
            synced=False,
            user_type="Guest",
            mfa_methods=[],
            licence_skus=[],
            created_days=90.0,
            last_sign_in_days=20.0,
            department=None,
            job_title=None,
            office=None,
            mailbox_purpose="others",
            mailbox_bytes=None,
        )
    )
    # Shared mailboxes and a room: no licence, no second factor, no person.
    for label, purpose in (("info", "shared"), ("accounts", "shared"), ("boardroom", "room")):
        accounts.append(
            M365Account(
                upn=f"{label}@{customer_key}.example".lower(),
                display_name=f"{name} {label.title()}",
                person_id=None,
                enabled=True,
                synced=False,
                user_type="Member",
                mfa_methods=[],
                licence_skus=[],
                created_days=800.0,
                last_sign_in_days=None,
                department=None,
                job_title=None,
                office=offices[0],
                mailbox_purpose=purpose,
                mailbox_bytes=int(rng.uniform(1.0, 9.0) * 10**9),
            )
        )
    everyone = [a.upn for a in accounts if a.person_id is not None]
    first_half = everyone[: max(1, len(everyone) // 2)]
    groups = [
        M365Group(
            id=f"{customer_key}-team-finance",
            name="Finance team",
            mail=f"finance@{customer_key}.example".lower(),
            unified=True,
            team=True,
            security=False,
            member_upns=first_half,
        ),
        M365Group(
            id=f"{customer_key}-grp-projects",
            name="Projects",
            mail=f"projects@{customer_key}.example".lower(),
            unified=True,
            team=False,
            security=False,
            member_upns=everyone[1:4],
        ),
        M365Group(
            id=f"{customer_key}-dl-allstaff",
            name="All staff",
            mail=f"allstaff@{customer_key}.example".lower(),
            unified=False,
            team=False,
            security=False,
            member_upns=everyone,
        ),
        M365Group(
            id=f"{customer_key}-sec-vpn",
            name="VPN users",
            mail=None,
            unified=False,
            team=False,
            security=True,
            member_upns=everyone[:3],
        ),
    ]
    for group in groups:
        for account in accounts:
            if account.upn in group.member_upns:
                account.group_ids.append(group.id)
    return tenant, accounts, groups


def _volumes(
    rng: random.Random, *, server: bool, free_share: float | None, bitlocker: str | None
) -> list[Volume]:
    if free_share is None:
        return []
    capacity = (
        rng.choice([256, 512, 1024]) * 10**9 if not server else rng.choice([1, 2, 4]) * 10**12
    )
    volumes = [Volume("C:", capacity, int(capacity * free_share), bitlocker)]
    if server:
        data = rng.choice([2, 4, 8]) * 10**12
        volumes.append(Volume("D:", data, int(data * rng.uniform(0.15, 0.6)), bitlocker))
    return volumes


#: The two customers who do not buy backup. Named rather than drawn, so the
#: board always has a customer it can say `not assessed` about.
COVE_NOT_SOLD: Final[frozenset[str]] = frozenset({"fourth", "tailspin"})

#: The machines a protection or backup agent belongs on. Mirrors
#: `AGENT_NODE_CLASSES` in `app/coverage/core.py`, which is what the board
#: counts: a switch with no Cove device is not a gap.
AGENT_CLASSES: Final[frozenset[str]] = frozenset(
    {"WINDOWS_SERVER", "WINDOWS_WORKSTATION", "VMWARE_VM_GUEST", "MAC"}
)


def build_world(seed: int = 20260929) -> World:
    rng = random.Random(seed)  # noqa: S311 - a deterministic world, not a secret
    customers: list[Customer] = []
    devices: list[Device] = []
    vendor_only: list[VendorOnlyEndpoint] = []
    person_id = 1000
    location_id = 500
    device_id = 10_000

    for index, (name, industry, size) in enumerate(V.CUSTOMERS):
        key = _key(name)
        prefix = _prefix(name)
        location_count = 1 if size < 20 else 2 if size < 45 else 3
        locations = []
        for n in range(location_count):
            location_id += 1
            label = ["Head Office", "Branch", "Warehouse"][n]
            street = rng.choice(V.STREETS)
            city = rng.choice(V.CITIES)
            locations.append(
                Location(
                    location_id,
                    f"{name} - {label}",
                    f"{rng.randrange(10, 999)} {street}, {city}, TX",
                )
            )
        people = _people(rng, key, max(6, size // 3), person_id)
        person_id += len(people)
        tenant, accounts, groups = _m365(rng, key, name, people, index)
        customer = Customer(
            key=key,
            name=name,
            industry=industry,
            ninja_org_id=100 + index,
            autotask_company_id=2000 + index,
            bitdefender_company_ids=[f"6{index:02d}0f1e2d3c4b5a6978{index:04x}"],
            connectsecure_company_id=30_000 + index,
            locations=locations,
            people=people,
            m365_tenant_id=tenant,
            m365_accounts=accounts,
            m365_groups=groups,
            # One tenant without an Entra premium licence, so the page that
            # says "not available for this tenant" has somewhere to say it.
            m365_premium=index != 1,
        )
        customers.append(customer)

        device_id += 1
        devices.append(
            Device(
                id=device_id,
                customer_key=key,
                system_name=f"{prefix}-SW01",
                dns_name=f"{prefix}-SW01.{key}.local".lower(),
                node_class="NMS_SWITCH",
                os_name="",
                manufacturer="Ubiquiti Inc.",
                model="USW-24-PoE",
                processor="",
                memory_gb=0,
                serial=_serial(rng, "Ubiquiti"),
                macs=[_mac(rng)],
                location_id=locations[0].id,
                last_contact_days=rng.uniform(0, 0.2),
                created_days=rng.uniform(300, 900),
                in_bitdefender=False,
                in_connectsecure=False,
            )
        )
        servers = max(1, size // 15)
        for n in range(size):
            device_id += 1
            is_server = n < servers
            if is_server:
                manufacturer, model, processor, memory = rng.choice(V.SERVER_MODELS)
                role = ["DC01", "FS01", "SQL01", "APP01"][n % 4]
                system_name = f"{prefix}-{role}"
                node_class: NodeClass = "WINDOWS_SERVER"
                os_name = rng.choice(V.OS_SERVER)
                virtual = n % 3 == 1
                if virtual:
                    manufacturer, model = "VMware, Inc.", "VMware Virtual Platform"
                    node_class = "VMWARE_VM_GUEST"
                last_user = None
                free = rng.uniform(0.08, 0.5)
                bitlocker = None
            else:
                manufacturer, model, processor, memory = rng.choice(V.WORKSTATION_MODELS)
                kind = (
                    "LT"
                    if "Latitude" in model or "Book" in model or "Pad" in model or "Mac" in model
                    else "WS"
                )
                system_name = f"{prefix}-{kind}-{n - servers + 1:03d}"
                if manufacturer == "Apple":
                    node_class, os_name = "MAC", rng.choice(V.OS_MAC)
                else:
                    node_class = "WINDOWS_WORKSTATION"
                    os_name = (
                        "Microsoft Windows 10 Pro Edition"
                        if ("i5-6500" in processor or "i5-8265U" in processor)
                        else rng.choice(V.OS_WORKSTATION[:2])
                    )
                virtual = False
                last_user = rng.choice(people)
                free = rng.choice(
                    [rng.uniform(0.45, 0.8), rng.uniform(0.22, 0.4), rng.uniform(0.05, 0.19)]
                )
                bitlocker = (
                    None
                    if node_class == "MAC"
                    else ("Encrypted" if rng.random() < 0.86 else "Decrypted")
                )
            device = Device(
                id=device_id,
                customer_key=key,
                system_name=system_name[:15],
                dns_name=f"{system_name}.{key}.local".lower(),
                node_class=node_class,
                os_name=os_name,
                manufacturer=manufacturer,
                model=model,
                processor=processor,
                memory_gb=memory,
                serial=_serial(rng, manufacturer),
                macs=[_mac(rng)],
                location_id=rng.choice(locations).id,
                last_contact_days=rng.choice(
                    [rng.uniform(0, 0.12), rng.uniform(0, 0.12), rng.uniform(0.2, 1.4)]
                ),
                created_days=rng.uniform(60, 1400),
                volumes=_volumes(rng, server=is_server, free_share=free, bitlocker=bitlocker),
                last_user=last_user,
                virtual=virtual,
                pending_reboot=rng.random() < 0.08,
            )
            devices.append(device)

    _place_scenarios(rng, customers, devices, vendor_only)
    tickets = _tickets(rng, customers, devices)
    installs, pending = _patches(rng, devices)
    alerts = _alerts(rng, devices)
    detections = _detections(rng, customers, devices)
    findings = _findings(rng, customers, devices)
    return World(
        seed=seed,
        franchisee=V.FRANCHISEE_NAME,
        customers=customers,
        internal_organisation=V.INTERNAL_ORGANISATION,
        devices=devices,
        vendor_only=vendor_only,
        tickets=tickets,
        installs=installs,
        pending=pending,
        alerts=alerts,
        detections=detections,
        findings=findings,
    )


def _place_scenarios(
    rng: random.Random,
    customers: list[Customer],
    devices: list[Device],
    vendor_only: list[VendorOnlyEndpoint],
) -> None:
    """Every scenario in `scenarios.SCENARIOS`, placed on a named customer."""
    by_key = {c.key: c for c in customers}
    contoso = by_key["contoso"]
    fabrikam = by_key["fabrikam"]
    northwind = by_key["northwind"]
    adventure = by_key["adventure"]
    woodgrove = by_key["woodgrove"]
    tailspin = by_key["tailspin"]
    litware = by_key["litware"]
    fourth = by_key["fourth"]

    def workstations(key: str) -> list[Device]:
        return [
            d
            for d in devices
            if d.customer_key == key and d.node_class == "WINDOWS_WORKSTATION" and not d.scenario
        ]

    # Fabrikam holds two GravityZone companies: a hand-made one and the integration's.
    fabrikam.bitdefender_company_ids.append("6f1a2b3c4d5e6f708192a3b4c5d6e7f8")
    fabrikam.scenario = "two_gravityzone_companies"
    for n, device in enumerate(devices):
        if device.customer_key == "fabrikam":
            device.bitdefender_company_index = n % 2

    # Woodgrove is the one customer whose licence includes EDR, so detections exist there.
    woodgrove.edr_licensed = True
    woodgrove.scenario = "edr_licensed"

    # Fourth Coffee: antivirus only, no vulnerability scanner.
    fourth.av_only = True
    fourth.connectsecure_company_id = None
    fourth.scenario = "av_only"
    for device in devices:
        if device.customer_key == "fourth":
            device.in_connectsecure = False

    # Contoso: a NetBIOS collision. Two accounting machines whose names differ only
    # after the fifteenth character; systemName is the truncation, dnsName is whole.
    twins = workstations("contoso")[:2]
    for n, device in enumerate(twins, start=1):
        device.dns_name = f"contoso-accounting-{n:02d}.contoso.local"
        device.system_name = "CONTOSO-ACCOUNT"
        device.scenario = "netbios_collision"

    # Contoso: a device NinjaOne has lost, and the tools have too (rmm_stale)...
    lost = workstations("contoso")[2]
    lost.last_contact_days = 31.0
    lost.bitdefender_last_seen_days = 33.0
    lost.connectsecure_scan_days = 34.0
    lost.scenario = "rmm_stale"
    # ...and one NinjaOne has lost while Bitdefender still hears from it (rmm_agent_down).
    down = workstations("contoso")[3]
    down.last_contact_days = 21.0
    down.bitdefender_last_seen_days = 0.5
    down.connectsecure_scan_days = 1.0
    down.scenario = "rmm_agent_down"
    # ...and one NinjaOne has never heard from since enrolment.
    never = workstations("contoso")[4]
    never.last_contact_days = None
    never.bitdefender_last_seen_days = 40.0
    never.in_connectsecure = False
    never.scenario = "never_contacted"

    # Northwind: computers with no protection agent and none scanned (the two-tool gaps).
    unprotected = workstations("northwind")[:3]  # taken before any is marked
    for device in unprotected:
        device.in_bitdefender = False
        device.scenario = "no_protection_agent"
    # One of them has also gone dark: no protection agent and no check-in for over a
    # week. The worst square on the board, and the answer to "what is unprotected and
    # has not been seen".
    dark = unprotected[0]
    dark.last_contact_days = 9.5
    dark.connectsecure_scan_days = 11.0
    dark.scenario = "unprotected_and_dark"
    for device in workstations("northwind")[3:5]:
        device.in_connectsecure = False
        device.scenario = "not_scanned"
    stale_scan = workstations("northwind")[5]
    stale_scan.connectsecure_scan_days = 45.0
    stale_scan.scenario = "stale_scan"

    # Tailspin: two reimaged spare laptops enrolled under one name, whose vendor rows carry
    # no MAC and no serial: the name pass is ambiguous and nothing decides, so `unmatched`.
    spares = workstations("tailspin")[:2]
    for device in spares:
        device.system_name, device.dns_name = "TST-SPARE", "tst-spare.tailspin.local"
        device.vendor_identifiers = False
        device.scenario = "unmatched_twins"

    # --- backup, as a franchisee actually sells it ---------------------------------
    #
    # Six of the eight customers buy it; two do not, so the board has somewhere
    # to say `not assessed` for a whole customer rather than counting every one
    # of their machines as a gap.
    #
    # Where it is sold it is on every machine, which is what the coverage
    # board's Backup column asks about: a managed computer with no Cove device
    # is the gap it is for, so a world where most machines never had one would
    # paint the column red and say nothing. The handful of real gaps are
    # placed below, by name. Servers back up nightly and keep everything;
    # workstations keep documents and miss a night more often.
    for index, customer in enumerate(customers):
        if customer.key in COVE_NOT_SOLD:
            continue
        customer.cove_partner_id = 70_000 + index
        for device in devices:
            if device.customer_key != customer.key or device.node_class not in AGENT_CLASSES:
                continue
            device.in_cove = True
            server = device.node_class in ("WINDOWS_SERVER", "VMWARE_VM_GUEST")
            device.cove_last_success_days = (
                rng.uniform(0.2, 0.9) if server else rng.uniform(0.3, 1.6)
            )
            device.cove_bad_days = rng.choice([0, 0, 0, 1] if server else [0, 0, 1, 3])

    # One server whose last good backup was eleven days ago: the agent is there,
    # it runs, and nothing it has written since is worth restoring.
    behind = next(d for d in devices if d.customer_key == "contoso" and d.in_cove)
    behind.cove_last_success_days = 11.4
    behind.cove_bad_days = 11
    behind.scenario = "backup_behind"

    # One that has not backed up at all in two months and nobody has said so:
    # the machine may have been retired, which is a question rather than a fact,
    # and the page has to let somebody ask it.
    maybe_retired = next(
        d for d in devices if d.customer_key == "woodgrove" and d.in_cove and d is not behind
    )
    maybe_retired.cove_last_success_days = 63.0
    maybe_retired.cove_bad_days = 28
    maybe_retired.scenario = "backup_possibly_retired"

    # One that completed last night and could not read a handful of files. A
    # success for the rate, and still something somebody looks at.
    with_errors = next(d for d in devices if d.customer_key == "fabrikam" and d.in_cove)
    with_errors.cove_errors = 7
    with_errors.scenario = "backup_completed_with_errors"

    # And two machines Cove has no device for at a customer who buys backup:
    # the coverage gap the Backup column is for.
    for device in [
        d
        for d in devices
        if d.customer_key == "fabrikam"
        and d.node_class in ("WINDOWS_SERVER", "VMWARE_VM_GUEST")
        and d is not with_errors
    ][:2]:
        device.in_cove = False
        device.cove_last_success_days = None
        device.cove_bad_days = 0
        device.scenario = "no_backup_device"

    # Adventure Works: a till on Windows 10 IoT that NinjaOne classes as a server.
    till = workstations("adventure")[0]
    till.system_name, till.dns_name = "AWC-POS-01", "awc-pos-01.adventure.local"
    till.node_class = "WINDOWS_SERVER"
    till.os_name = V.OS_IOT
    till.last_user = None
    till.scenario = "windows_10_iot_classed_server"

    # Machines the tools hold that NinjaOne does not manage, per kind.
    vendor_only.extend(
        [
            VendorOnlyEndpoint(
                "fabrikam", "FAB-RECEPTION-KIOSK", "bitdefender", "unmanaged", _mac(rng), 0.3
            ),
            VendorOnlyEndpoint(
                "fabrikam", "FAB-RECEPTION-KIOSK", "connectsecure", "unmanaged", _mac(rng), 2.0
            ),
            VendorOnlyEndpoint(
                "litware", "DESKTOP-7K3P2QX", "bitdefender", "default_name", _mac(rng), 0.2
            ),
            VendorOnlyEndpoint(
                "litware", "LAPTOP-9Q2M4RT", "connectsecure", "default_name", _mac(rng), 1.5
            ),
            VendorOnlyEndpoint("northwind", "203.0.113.10", "connectsecure", "external", "", 1.0),
            VendorOnlyEndpoint("northwind", "198.51.100.7", "connectsecure", "external", "", 1.0),
            VendorOnlyEndpoint(
                "tailspin", "TST-PRINTER-01", "bitdefender", "discovered", _mac(rng), 3.0
            ),
            VendorOnlyEndpoint(
                "tailspin", "TST-PROBE-01", "connectsecure", "discovered", _mac(rng), 1.0
            ),
        ]
    )
    litware.scenario = "default_windows_names"
    tailspin.scenario = "discovered_endpoints"
    northwind.scenario = "external_targets"

    # Encryption: no licence manages it anywhere; BitLocker covers most devices already
    # (set per device above at 86%), so the gap is the unencrypted minority.
    contoso.scenario = contoso.scenario or "coverage_states"
    adventure.scenario = adventure.scenario or "windows_10_iot"


def _tickets(rng: random.Random, customers: list[Customer], devices: list[Device]) -> list[Ticket]:
    tickets: list[Ticket] = []
    number = 20_260_001
    ticket_id = 70_000
    for n, customer in enumerate(customers):
        size = len([d for d in devices if d.customer_key == customer.key])
        count = int(size * 2.2)  # about two tickets per device a quarter
        workstations = [
            d
            for d in devices
            if d.customer_key == customer.key and not d.is_server and d.is_agent_class
        ]
        # A repeat issue: the same printer, the same person, most weeks.
        repeater = rng.choice(customer.people)
        for week in range(0, DAYS, 7):
            if rng.random() < 0.7:
                tickets.append(
                    _ticket(
                        rng,
                        customer,
                        ticket_id,
                        number,
                        "Printer offline in reception",
                        "Printer/Scanner/Copier",
                        "Printer",
                        repeater,
                        week + rng.uniform(0, 6),
                        None,
                        "repeat_issue",
                    )
                )
                ticket_id, number = ticket_id + 1, number + 1
        for _ in range(count):
            title, issue, sub = rng.choice(V.TICKET_KINDS)
            contact = rng.choice(customer.people)
            created = rng.uniform(0, DAYS)
            device = rng.choice(workstations) if workstations and rng.random() < 0.6 else None
            tickets.append(
                _ticket(
                    rng,
                    customer,
                    ticket_id,
                    number,
                    title,
                    issue,
                    sub,
                    contact,
                    created,
                    device,
                    "",
                )
            )
            ticket_id, number = ticket_id + 1, number + 1
        # A few tickets open now and already past their due time: the SLA breaches
        # a presenter asks about. Priority decides the clock, so these are created
        # comfortably past the longest one.
        for _ in range(2):
            title, issue, sub = rng.choice(V.TICKET_KINDS)
            tickets.append(
                _ticket(
                    rng,
                    customer,
                    ticket_id,
                    number,
                    title,
                    issue,
                    sub,
                    rng.choice(customer.people),
                    rng.uniform(0.4, 2.2),
                    rng.choice(workstations) if workstations else None,
                    "sla_breach_open",
                )
            )
            ticket_id, number = ticket_id + 1, number + 1

        # Automated tickets: monitoring raised them, not a person.
        for _ in range(max(3, size // 6)):
            title = rng.choice(V.AUTOMATED_TICKETS)
            created = rng.uniform(0, DAYS)
            device = rng.choice(workstations) if workstations else None
            tickets.append(
                _ticket(
                    rng,
                    customer,
                    ticket_id,
                    number,
                    title,
                    "Monitoring Alert",
                    "Automated",
                    None,
                    created,
                    device,
                    "automated_contact",
                )
            )
            ticket_id, number = ticket_id + 1, number + 1
        # Recent, open alerts in the shapes the alert-note workflows answer, one
        # per customer from the list in turn, raised in the last day and a half.
        # The device is the same for the whole customer so the repeat-alert
        # workflow has a second ticket of a kind to find.
        if workstations:
            # The last workstation, not the first: the first few of a customer
            # are where the coverage scenarios (twins, a NetBIOS collision) are
            # placed, and a machine the resolution cannot name gets no note.
            device = workstations[-1]
            title, body = V.RECENT_ALERTS[n % len(V.RECENT_ALERTS)]
            machine = device.system_name
            recent = _ticket(
                rng,
                customer,
                ticket_id,
                number,
                title.format(machine=machine),
                "Monitoring Alert",
                "Automated",
                None,
                rng.uniform(0.05, 1.5),
                device,
                "recent_alert",
            )
            recent.description = body.format(who=V.AUTOMATION_CONTACT, machine=machine)
            recent.completed_days, recent.status = None, "New"
            tickets.append(recent)
            ticket_id, number = ticket_id + 1, number + 1
            # And the same kind again, a few days earlier and already closed,
            # for the repeat-alert note.
            earlier = _ticket(
                rng,
                customer,
                ticket_id,
                number,
                title.format(machine=machine),
                "Monitoring Alert",
                "Automated",
                None,
                rng.uniform(2.0, 5.0),
                device,
                "recent_alert_repeat",
            )
            earlier.description = body.format(who=V.AUTOMATION_CONTACT, machine=machine)
            tickets.append(earlier)
            ticket_id, number = ticket_id + 1, number + 1
            # One customer's machine is chronic: the same alert twice more in
            # the month, so the chronic devices report has a row to show.
            if n == 0:
                for days_ago in (9.0, 17.0):
                    again = _ticket(
                        rng,
                        customer,
                        ticket_id,
                        number,
                        title.format(machine=machine),
                        "Monitoring Alert",
                        "Automated",
                        None,
                        days_ago,
                        device,
                        "chronic_device",
                    )
                    again.description = body.format(who=V.AUTOMATION_CONTACT, machine=machine)
                    tickets.append(again)
                    ticket_id, number = ticket_id + 1, number + 1
        # Some tickets carry no issue type at all.
        for ticket in rng.sample(
            [t for t in tickets if t.customer_key == customer.key and t.contact],
            k=max(1, count // 12),
        ):
            ticket.issue_type, ticket.sub_issue_type = "", ""
    tickets.sort(key=lambda t: -t.created_days)
    return tickets


def _resolution(title: str, machine: str, who: str) -> str:
    """What was wrong and what fixed it. The ticket workspace cites these, so a
    resolution names a root cause and the steps, never just "resolved"."""
    found = V.RESOLUTIONS.get(title)
    if found is None:
        return f"Resolved{machine}. Confirmed with {who}."
    cause, fix = found
    return f"Root cause: {cause}. Fix: {fix}{machine}. Confirmed with {who}."


def _ticket(
    rng: random.Random,
    customer: Customer,
    ticket_id: int,
    number: int,
    title: str,
    issue: str,
    sub: str,
    contact: Person | None,
    created: float,
    device: Device | None,
    scenario: str,
) -> Ticket:
    priority = rng.choices(["Low", "Medium", "High", "Critical"], weights=[3, 6, 2, 1])[0]
    first_due = {"Critical": 1 / 24, "High": 2 / 24, "Medium": 4 / 24, "Low": 8 / 24}[priority]
    resolve_due = {"Critical": 4 / 24, "High": 1, "Medium": 2, "Low": 5}[priority]
    breach = rng.random() < 0.12  # about one in eight tickets misses an SLA
    first_response = created - (first_due * (1.8 if breach else rng.uniform(0.1, 0.9)))
    took = resolve_due * (rng.uniform(1.2, 2.5) if breach else rng.uniform(0.2, 0.9))
    completed: float | None = created - took
    status = "Complete"
    if completed is not None and completed < 0:
        completed, status = (
            None,
            rng.choice(["New", "In Progress", "Waiting Customer", "Waiting Vendor", "Escalated"]),
        )
    if created < 2 and rng.random() < 0.5:
        completed, status = None, "In Progress"
    if scenario == "sla_breach_open":
        # Still open and already past its due time, by hours rather than weeks: a
        # Critical ticket three weeks past a four-hour clock is not believable, and
        # a ticket younger than its own clock is not breaching at all. The age is
        # set from the clock, not the other way round.
        completed, status = None, rng.choice(["In Progress", "Escalated"])
        priority = rng.choice(["Medium", "High"])
        first_due = {"High": 2 / 24, "Medium": 4 / 24}[priority]
        resolve_due = {"High": 1.0, "Medium": 2.0}[priority]
        created = resolve_due + rng.uniform(0.15, 1.1)  # hours to a day past due
        first_response = -1.0 if rng.random() < 0.4 else created - first_due * 2.5
    who = contact.name if contact else V.AUTOMATION_CONTACT
    machine = f" on {device.system_name}" if device else ""
    return Ticket(
        id=ticket_id,
        customer_key=customer.key,
        number=f"T{number}",
        title=title,
        issue_type=issue,
        sub_issue_type=sub,
        contact=contact,
        created_days=created,
        completed_days=completed,
        first_response_days=None if (status == "New" or first_response < 0) else first_response,
        first_response_due_days=created - first_due,
        resolution_due_days=created - resolve_due,
        priority=priority,
        status=status,
        device=device,
        description=f"{who} reports: {title.lower()}{machine}. Please investigate.",
        resolution=_resolution(title, machine, who) if completed is not None else "",
        scenario=scenario if scenario else ("sla_breach" if breach else ""),
    )


def _patches(
    rng: random.Random, devices: list[Device]
) -> tuple[list[PatchInstall], list[PendingPatch]]:
    installs: list[PatchInstall] = []
    pending: list[PendingPatch] = []
    install_id = 400_000
    pending_id = 500_000
    for device in devices:
        if not device.is_agent_class or device.node_class == "MAC":
            continue
        windows_server = "Server" in device.os_name
        candidates = [
            p
            for p in V.OS_PATCHES
            if ("server operating system" in p[1]) == windows_server
            or p[0] in ("KB2267602", "KB4052623")
        ]
        for month in range(3):
            for kb, title, severity in rng.sample(candidates, k=min(3, len(candidates))):
                install_id += 1
                failed = rng.random() < 0.04
                installs.append(
                    PatchInstall(
                        install_id,
                        device.id,
                        "os",
                        kb,
                        title,
                        severity,
                        "FAILED" if failed else "INSTALLED",
                        month * 30 + rng.uniform(1, 20),
                    )
                )
        if not windows_server:
            for product, title, version in rng.sample(V.SOFTWARE_PATCHES, k=4):
                install_id += 1
                failed = rng.random() < 0.03
                installs.append(
                    PatchInstall(
                        install_id,
                        device.id,
                        "software",
                        product,
                        f"{title} {version}",
                        "recommended",
                        "FAILED" if failed else "INSTALLED",
                        rng.uniform(0, DAYS),
                    )
                )
        # The waiting list: a few approved patches not yet installed, some critical.
        if rng.random() < 0.35:
            kb, title, severity = rng.choice(candidates[:4])
            pending_id += 1
            pending.append(
                PendingPatch(pending_id, device.id, "os", kb, title, severity, "APPROVED")
            )
        if rng.random() < 0.25 and not windows_server:
            product, title, version = rng.choice(V.SOFTWARE_PATCHES)
            pending_id += 1
            pending.append(
                PendingPatch(
                    pending_id,
                    device.id,
                    "software",
                    product,
                    title,
                    "recommended",
                    "APPROVED",
                    version,
                )
            )
    return installs, pending


def _alerts(rng: random.Random, devices: list[Device]) -> list[Alert]:
    alerts: list[Alert] = []
    alert_id = 900_000
    by_customer: dict[str, list[Device]] = {}
    for device in devices:
        if device.is_agent_class:
            by_customer.setdefault(device.customer_key, []).append(device)
    # Every customer has something open today: "anything wrong at X today" is a
    # question a presenter asks first, and an empty answer is a bad demo. Northwind
    # gets the worst of it, because that is where the coverage gaps are.
    for key, machines in by_customer.items():
        today = 4 if key == "northwind" else 2
        for n in range(today):
            alert_id += 1
            message, severity = V.ALERTS[n % len(V.ALERTS)]
            alerts.append(
                Alert(
                    alert_id,
                    rng.choice(machines).id,
                    message,
                    severity,
                    rng.uniform(0.05, 0.9),
                    None,
                )
            )
    for device in devices:
        if not device.is_agent_class:
            continue
        for _ in range(rng.choices([0, 1, 2, 3], weights=[5, 3, 1.5, 0.5])[0]):
            alert_id += 1
            message, severity = rng.choice(V.ALERTS)
            created = rng.uniform(1, DAYS)
            resolved = None if rng.random() < 0.15 else created - rng.uniform(0.05, 3)
            alerts.append(
                Alert(
                    alert_id,
                    device.id,
                    message,
                    severity,
                    created,
                    None if resolved is not None and resolved < 0 else resolved,
                )
            )
    return alerts


def _detections(
    rng: random.Random, customers: list[Customer], devices: list[Device]
) -> list[Detection]:
    detections: list[Detection] = []
    for customer in customers:
        if not customer.edr_licensed:
            continue
        machines = [d for d in devices if d.customer_key == customer.key and d.in_bitdefender]
        for n in range(14):
            threat, action = rng.choice(V.DETECTIONS)
            device = rng.choice(machines)
            detections.append(
                Detection(
                    f"inc-{customer.key}-{n:03d}",
                    customer.key,
                    device.id,
                    threat,
                    action,
                    rng.uniform(0, DAYS),
                )
            )
    return detections


def _findings(
    rng: random.Random, customers: list[Customer], devices: list[Device]
) -> list[Finding]:
    findings: list[Finding] = []
    kev = [c for c in V.CVES if c[3]]
    other = [c for c in V.CVES if not c[3]]
    software_versions = {name: (bad, good) for name, _, bad, good in V.SOFTWARE}
    for customer in customers:
        if customer.connectsecure_company_id is None:
            continue
        machines = [
            d
            for d in devices
            if d.customer_key == customer.key and d.in_connectsecure and d.node_class != "MAC"
        ]
        for device in machines:
            # Every scanned machine carries a few findings; a third of them a known-exploited one.
            picks = rng.sample(other, k=rng.randrange(1, 5))
            if rng.random() < 0.3:
                picks.append(rng.choice(kev))
            for cve, product, severity, is_kev in picks:
                confirmed = rng.random() < 0.55
                if product in software_versions:
                    fix = f"Update {product} to {software_versions[product][1]} or later"
                    kind: Literal["Update", "Uninstall"] = "Update"
                else:
                    fix = f"Install the Windows security update that addresses {cve}"
                    kind = "Update"
                findings.append(
                    Finding(device.id, cve, product, severity, is_kev, confirmed, fix, kind)
                )
            if rng.random() < 0.12:
                name, _ = rng.choice(V.SOFTWARE_TO_REMOVE)
                findings.append(
                    Finding(
                        device.id,
                        f"CVE-2013-{rng.randrange(1000, 5999)}",
                        name,
                        "HIGH",
                        False,
                        True,
                        f"Uninstall {name}",
                        "Uninstall",
                    )
                )
    return findings


def stable_uuid(seed: int, name: str) -> uuid.UUID:
    """A UUID that is the same for the same world and name: vendor ids that must look like ids."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"demo:{seed}:{name}")

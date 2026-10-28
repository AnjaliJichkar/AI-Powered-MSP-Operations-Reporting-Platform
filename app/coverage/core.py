"""The join: managed devices against each tool's endpoints, within a site.

Pure functions over plain records, so the same code answers the board, the
chat tools and the business review, and a test can feed all three the same
devices and read the same counts back.

A device's state for a tool is one of:

- `covered`: the tool has the device's endpoint at the same site, and it is
  current by the tool's own rules (`config.STALE_RULES`);
- `stale`: the tool has the endpoint and it is not current;
- `absent`: the tool answered for the site and has no endpoint for the
  device, or has one with no agent on it;
- `unmatched`: no pass could pair it without guessing — NinjaOne gives it no
  hostname, or a pass found more than one candidate and no key told them
  apart; the candidates are listed;
- `rmm_stale`: NinjaOne itself has not heard from the device for more than
  the configured number of days, and this tool has not heard from it lately
  either. Its own line on the board, never counted as absent;
- `rmm_agent_down`: NinjaOne has not heard from the device, but this tool's
  row for the same machine is alive: the machine is alive per Bitdefender or
  ConnectSecure and NinjaOne is not reporting. A finding about the RMM agent,
  reported rather than excluded; the tool's row is never `unmanaged` as well,
  so one physical machine never appears as both stale and unmanaged;
- `excluded`: somebody recorded that this device is not expected to have this
  tool, with a reason; it is shown, never dropped;
- `not_assessed`: the tool is not connected for this client, has no company
  for this site, or does not exist yet (Backup until Cove).

**Four passes**, each one-to-one and within one site, and each pairing
records which pass made it (`ToolState.matched_by`):

1. the exact normalised hostname (`app/connectors/matching.py`). NinjaOne's
   hostname is its full `dnsName` where it has one, falling back to
   `systemName`: `systemName` is the 15-character NetBIOS name, and machines
   whose names differ only after the fifteenth character collide on it;
2. the NetBIOS key, the first fifteen characters, for the tool that holds
   the truncated name where NinjaOne has the full one, or the reverse;
3. a MAC address the two rows share: Bitdefender's `macs` and ConnectSecure's
   `mac` against NinjaOne's network interfaces (renamed machines);
4. a serial number the two rows share: ConnectSecure's `serial_number`
   against NinjaOne's `computer-systems` query.

When a key pass is ambiguous — two managed devices share the key, or the key
names more than one machine in the tool — the disambiguating keys are tried
among the candidates before anything is declared unmatched: the full DNS
name is the exact key itself, then a MAC, then a serial, each counted only
when no rival device shares it. A device or endpoint that nothing tells
apart stays unmatched with the candidates named, never paired with the
nearest.

A **machine** in a tool's inventory is the rows that share one exact key at
one site, unless their MACs or serials say they are different machines (a
reinstalled agent leaves an old row behind: one machine; three machines a
scanner knows by the same truncated name: three). Machines, not rows, are
what the passes pair and what the reverse reports; the split only ever
matters when there are several devices to tell apart — one device whose
key names several machines and nothing to contradict it takes them all, as
it always did.

The reverse — a machine in a tool with no managed device at the site — is
`unmanaged` only at a site NinjaOne manages; at any other site it is
`not_assessable`. A row that is discovered rather than installed
(Bitdefender `isManaged: false`, ConnectSecure probes and deprecated agents)
is `discovered`, reported apart from unmanaged. A ConnectSecure row whose
name is an IP address or an ISP's reverse-DNS name is an **external** scan
target, reported apart and never an unmanaged endpoint. An unmanaged row
with a default Windows name (`DESKTOP-XXXXXXX`) is noted as likely not yet
onboarded. Matching never crosses a site: a `RECEPTION` at one customer is
never paired with one at another.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from app.connectors.matching import normalise_hostname

State = Literal[
    "covered",
    "stale",
    "absent",
    "unmatched",
    "rmm_stale",
    "rmm_agent_down",
    "excluded",
    "not_assessed",
    "not_in_scope",
]
#: A site as the join keys it: our site id on the board, the review's site
#: slug inside the collector. Never a name, and never compared across two.
SiteKey = uuid.UUID | str
STATES: tuple[State, ...] = (
    "covered",
    "stale",
    "absent",
    "unmatched",
    "rmm_stale",
    "rmm_agent_down",
    "excluded",
    "not_assessed",
    # A machine this site does not buy this tool for. Not a gap and not a
    # failure to assess: the question was asked and the answer is that nobody
    # expects the tool to be there. Only Backup produces it today, from the
    # site's own backup scope (`app/coverage/scope.py`), because backup is the
    # one thing on this board that is sold per machine rather than rolled out
    # to the fleet.
    "not_in_scope",
)
#: The passes, in the order they run.
PASSES = ("exact", "netbios15", "mac", "serial")
#: The keys that tell the candidates of an ambiguous key pass apart, in order.
DISAMBIGUATORS = ("mac", "serial")
GAP_STATES: tuple[State, ...] = ("absent", "stale", "unmatched")
#: The passes that pair by a key two machines can share; a pairing a twin
#: needs comes from one of the others, or from a person.
NAME_PASSES = ("exact", "netbios15")
#: What a tool's own row can say about a machine it holds.
EndpointState = Literal["covered", "stale", "absent"]
#: What `rmm_agent_down` means, in the words the page and the log use.
RMM_AGENT_DOWN = "machine alive per {tool}, NinjaOne not reporting"
#: The note on an unmanaged row whose name Windows gave it at install.
NOT_ONBOARDED = "likely not yet onboarded (default Windows name)"

#: Only these count as machines a protection agent belongs on. Network gear,
#: phones and hypervisor hosts have no endpoint agent, so a NinjaOne switch
#: without a Bitdefender endpoint is not a gap.
AGENT_NODE_CLASSES = ("WINDOWS_", "MAC", "LINUX_", "VMWARE_VM_GUEST", "HYPERV_VMM_GUEST")

#: A name Windows gives a machine at install and nobody has changed: `DESKTOP-`
#: or `LAPTOP-` and seven characters, or the older `WIN-` and eleven.
_DEFAULT_WINDOWS_NAME = re.compile(r"^(?:desktop|laptop)-[a-z0-9]{7}$|^win-[a-z0-9]{11}$")
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
#: Four octets separated by dots or dashes inside a DNS name: the shape of an
#: ISP's reverse-DNS name (`c-73-1-2-3.hsd1.tx.example.net`,
#: `73.1.2.3.static.example.net`). The address is the test, not the words
#: around it: a rule on pool labels (`dsl-`, `pool-`, `host`) flagged two of
#: the pilot's own workstations, both managed by NinjaOne (validation log,
#: 2026-09-28), and was dropped.
_EMBEDDED_IPV4 = re.compile(r"(?<![0-9])\d{1,3}(?:[-.]\d{1,3}){3}(?![0-9])")


def is_agent_class(node_class: str | None) -> bool:
    return bool(node_class) and any(str(node_class).startswith(p) for p in AGENT_NODE_CLASSES)


def is_default_windows_name(name: str | None) -> bool:
    """`DESKTOP-4K2J9QP`, `LAPTOP-A1B2C3D`, `WIN-ABCDEFGHIJK`: the name Windows chose."""
    return bool(_DEFAULT_WINDOWS_NAME.match(normalise_hostname(name)))


def external_target(name: str | None) -> str | None:
    """Why a scanner's row is an external target rather than an endpoint, or None.

    An IP address for a name is a host the scanner reached by address and
    could not name; a dotted name with four octets in it is the reverse-DNS
    name an ISP gave a public address. Either is the outside of the
    customer's network as the scanner sees it, not a machine an agent belongs
    on. Anything else is a machine, whatever its name looks like: a
    single-label name is never external, and nor is a name with an ISP-sounding
    word and no address in it.
    """
    if not name:
        return None
    cleaned = name.strip().lower()
    if _IPV4.match(cleaned):
        return "name is an IP address"
    if "." in cleaned and _EMBEDDED_IPV4.search(cleaned):
        return "name is an ISP reverse-DNS name (an address in it)"
    return None


@dataclass(frozen=True, slots=True)
class ManagedDevice:
    """One NinjaOne computer: the unit of the population."""

    site_id: SiteKey
    site_slug: str
    site_name: str
    device_id: str
    name: str
    """NinjaOne's hostname: the full `dnsName` where it has one, else `systemName`."""
    node_class: str | None = None
    last_contact: dt.datetime | None = None
    macs: frozenset[str] = frozenset()
    """Normalised MACs of the device's interfaces, for the third pass."""
    serials: frozenset[str] = frozenset()
    """Normalised serial numbers (`config.serial_key`), for the fourth pass."""

    @property
    def key(self) -> str:
        return normalise_hostname(self.name)

    @property
    def key15(self) -> str:
        return self.key[:15]

    def keys(self, which: str) -> frozenset[str]:
        return self.macs if which == "mac" else self.serials


@dataclass(frozen=True, slots=True)
class ToolEndpoint:
    """One row of a tool's inventory, already judged current or not."""

    site_id: SiteKey
    name: str | None
    state: EndpointState
    """`absent` for a row the tool holds with nothing on it (no agent)."""
    detail: str
    last_seen: dt.datetime | None = None
    external_id: str | None = None
    macs: frozenset[str] = frozenset()
    discovered: str | None = None
    """Why this row is discovered rather than installed, when it is: it is then
    never `unmanaged`, and a probe is never paired with anything."""
    probe: bool = False
    serial: str | None = None
    """The normalised serial number the row carries, for the fourth pass."""
    external: str | None = None
    """Why this row is an external scan target (`external_target`), when it is:
    it then takes no part in the passes and is reported apart."""

    @property
    def key(self) -> str:
        return normalise_hostname(self.name)

    @property
    def key15(self) -> str:
        return self.key[:15]

    @property
    def pairable(self) -> bool:
        """Named, not a probe, and not an external target: a row a pass may use."""
        return bool(self.key) and not self.probe and self.external is None


@dataclass(frozen=True, slots=True)
class Exclusion:
    """A recorded decision that a device is not expected to have a tool."""

    device_id: str
    tool: str
    reason: str
    by: str
    at: dt.datetime
    id: int | None = None


@dataclass(frozen=True, slots=True)
class ToolState:
    state: State
    detail: str
    last_seen: dt.datetime | None = None
    matched_by: str | None = None
    """Which pass paired the device with its endpoint: exact, netbios15, mac or serial."""
    candidates: tuple[str, ...] = ()
    """For an unmatched device: the endpoint names a pass could not choose between."""
    endpoint_ids: tuple[str, ...] = ()
    """The vendor ids of the rows the pairing covers (one machine can be several rows),
    or of the candidates when unmatched. What the identity layer writes links from."""
    found_by: str | None = None
    """For an unmatched device: the pass whose candidates these are."""


@dataclass(slots=True)
class DeviceCoverage:
    device: ManagedDevice
    tools: dict[str, ToolState] = field(default_factory=dict)
    ninjaone_state: Literal["covered", "stale"] = "covered"
    ninjaone_detail: str = ""

    @property
    def has_gap(self) -> bool:
        return any(state.state in GAP_STATES for state in self.tools.values())

    @property
    def agent_down(self) -> bool:
        """NinjaOne has lost the device and at least one tool still hears from it."""
        return any(state.state == "rmm_agent_down" for state in self.tools.values())


@dataclass(frozen=True, slots=True)
class Unmanaged:
    """A machine a tool holds for a site with no managed device of that name."""

    site_id: SiteKey
    tool: str
    name: str
    detail: str
    last_seen: dt.datetime | None = None
    external_ids: tuple[str, ...] = ()
    """Every vendor row of the machine: a CSV writes one line per row, never fewer."""
    note: str | None = None
    """`NOT_ONBOARDED` for a default Windows name; the reason for an external target."""


@dataclass(frozen=True, slots=True)
class ToolInventory:
    """What one tool answered for one client: endpoints, and which sites it answered for."""

    endpoints: list[ToolEndpoint]
    sites: frozenset[SiteKey]
    """The sites the tool has a company for and answered. A device at any other
    site is `not_assessed` for this tool, never `absent`."""
    truncated: bool = False


@dataclass(slots=True)
class Assessment:
    devices: list[DeviceCoverage]
    unmanaged: list[Unmanaged]
    """Real endpoints with no managed device, at sites NinjaOne manages."""
    discovered: list[Unmanaged] = field(default_factory=list)
    """Rows a tool discovered rather than installed on, unpaired."""
    not_assessable: list[Unmanaged] = field(default_factory=list)
    """Endpoints at sites NinjaOne has no mapping for: nothing to compare with."""
    external: list[Unmanaged] = field(default_factory=list)
    """Scanner rows that are external targets: an IP or an ISP's name, never endpoints."""

    def counts(self, tool: str, *, site_id: SiteKey | None = None) -> Counter[str]:
        return Counter(
            d.tools[tool].state
            for d in self.devices
            if tool in d.tools and (site_id is None or d.device.site_id == site_id)
        )


def ninjaone_state(
    device: ManagedDevice, *, now: dt.datetime, stale_days: int
) -> tuple[Literal["covered", "stale"], str]:
    """NinjaOne's own verdict on a device it manages: has it been seen lately?"""
    if device.last_contact is None:
        return "stale", "no contact recorded"
    age = (now - device.last_contact).days
    if age > stale_days:
        return "stale", f"last contact {age} days ago"
    return "covered", f"last contact {age} day{'s' if age != 1 else ''} ago"


def endpoint_alive(endpoint: ToolEndpoint, *, now: dt.datetime, days: int) -> bool:
    """Has the tool heard from this row within `days`?

    Only a date says so: ConnectSecure's last scan, or the `lastSeen` the
    readers fetch from `getManagedEndpointDetails` for the GravityZone rows
    paired with lost devices (the endpoint list itself carries no date, and an
    agent installed is no evidence — live, 91 lost devices at the pilot kept a
    GravityZone row, 43 of them unseen by NinjaOne for over ninety days). A
    row with no date, or no agent, is never alive.
    """
    if endpoint.state == "absent" or endpoint.last_seen is None:
        return False
    return (now - endpoint.last_seen).days <= days


def lost_pairings(assessment: Assessment, tool: str) -> dict[str, list[str]]:
    """The rows of `tool` paired with devices NinjaOne has lost, by device id.

    What a reader fetches a last-seen for before deciding `rmm_agent_down`:
    the join is run once to find them, the dates are added to the rows, and
    it is run again. Pure and cheap, so running it twice costs nothing.
    """
    found: dict[str, list[str]] = {}
    for outcome in assessment.devices:
        verdict = outcome.tools.get(tool)
        if verdict is None or verdict.matched_by is None:
            continue
        if verdict.state in ("rmm_stale", "rmm_agent_down") and verdict.endpoint_ids:
            found[outcome.device.device_id] = list(verdict.endpoint_ids)
    return found


def mark_unchecked(assessment: Assessment, tool: str, device_ids: Iterable[str], days: int) -> None:
    """Say on a lost device's verdict that its last-seen was not read: the device
    has been lost for longer than `days`, so the vendor is not asked about it."""
    wanted = set(device_ids)
    for outcome in assessment.devices:
        if outcome.device.device_id not in wanted:
            continue
        verdict = outcome.tools.get(tool)
        if verdict is None or verdict.state != "rmm_stale" or verdict.matched_by is None:
            continue
        outcome.tools[tool] = replace(
            verdict,
            detail=f"{verdict.detail}; last-seen not checked (lost for more than {days} days)",
        )


def with_last_seen(
    inventory: ToolInventory, seen: Mapping[str, dt.datetime | None]
) -> ToolInventory:
    """The same inventory with `last_seen` set on the rows `seen` names (by vendor id)."""
    dated = {key: value for key, value in seen.items() if value is not None}
    if not dated:
        return inventory
    return ToolInventory(
        endpoints=[
            replace(e, last_seen=dated[e.external_id])
            if e.external_id is not None and e.external_id in dated
            else e
            for e in inventory.endpoints
        ],
        sites=inventory.sites,
        truncated=inventory.truncated,
    )


def lost_state(
    verdict: ToolState,
    rows: Sequence[ToolEndpoint],
    *,
    tool: str,
    now: dt.datetime,
    stale_days: int,
    dated: bool = True,
) -> ToolState:
    """A device NinjaOne has lost, given what a pass made of it for `tool`.

    Paired with a row the tool still hears from: `rmm_agent_down`. Paired with
    a dead row, or with nothing, or left ambiguous: `rmm_stale`, keeping what
    the pass found so a person can see it. `dated` False says the read that
    puts dates on this tool's rows failed this run: a paired device is then
    `not_assessed`, neither lost nor alive, and its rows stay claimed. The one
    place every reader decides this, so the board, the identity layer and the
    review cannot differ on it.
    """
    lost = f"no NinjaOne contact in {stale_days} days"
    if verdict.matched_by is not None:
        if not dated:
            return ToolState(
                "not_assessed",
                f"{lost}; {tool} did not answer for the row's last-seen this run, so alive "
                "or lost is not decided",
                verdict.last_seen,
                matched_by=verdict.matched_by,
                endpoint_ids=verdict.endpoint_ids,
            )
        if any(endpoint_alive(r, now=now, days=stale_days) for r in rows):
            return ToolState(
                "rmm_agent_down",
                f"{RMM_AGENT_DOWN.format(tool=tool)} ({lost})",
                verdict.last_seen,
                matched_by=verdict.matched_by,
                endpoint_ids=verdict.endpoint_ids,
            )
        return ToolState(
            "rmm_stale",
            f"{lost}; {tool} holds it as {verdict.detail}",
            verdict.last_seen,
            matched_by=verdict.matched_by,
            endpoint_ids=verdict.endpoint_ids,
        )
    return ToolState(
        "rmm_stale",
        f"{lost}; {verdict.detail}",
        candidates=verdict.candidates,
        endpoint_ids=verdict.endpoint_ids,
        found_by=verdict.found_by,
    )


def _best(found: Sequence[ToolEndpoint]) -> ToolEndpoint:
    """Several rows for one machine (a reinstalled agent leaves the old row
    behind): the best state is the machine's state."""
    return min(found, key=lambda e: ("covered", "stale", "absent").index(e.state))


def unmanaged_entry(tool: str, rows: Sequence[ToolEndpoint]) -> Unmanaged:
    """One machine's rows as the reverse reports them."""
    best = _best(rows)
    note = None
    if best.external:
        note = best.external
    elif is_default_windows_name(best.name):
        note = NOT_ONBOARDED
    return Unmanaged(
        site_id=best.site_id,
        tool=tool,
        name=best.name or "(unnamed)",
        detail=best.detail,
        last_seen=max((r.last_seen for r in rows if r.last_seen), default=None),
        external_ids=tuple(r.external_id for r in rows if r.external_id),
        note=note,
    )


MachineKey = tuple[SiteKey, str, int]
"""One machine as a tool knows it: its site, its exact key, and which of the
machines of that key it is (0 when the key names one machine; negative for a
row with no key or an external target, one machine each)."""


def group_machines(endpoints: Iterable[ToolEndpoint]) -> dict[MachineKey, list[ToolEndpoint]]:
    """Rows into machines: one exact key at one site is one machine, unless the
    rows' MACs or serials say otherwise.

    Within a key, rows that share a MAC or a serial are one machine, and a row
    with neither joins the only machine there is; when the rows split into
    more than one machine, a row with no key of its own stands alone rather
    than being guessed onto one. Rows with no key and external targets are one
    machine each, so the reverse reports every row.
    """
    by_key: dict[tuple[SiteKey, str], list[ToolEndpoint]] = defaultdict(list)
    loose: list[ToolEndpoint] = []
    for endpoint in endpoints:
        if endpoint.key and not endpoint.external:
            by_key[(endpoint.site_id, endpoint.key)].append(endpoint)
        else:
            loose.append(endpoint)
    machines: dict[MachineKey, list[ToolEndpoint]] = {}
    for (site_id, key), rows in by_key.items():
        parts = _split_by_keys(rows) if len(rows) > 1 else [rows]
        if len(parts) == 1:
            machines[(site_id, key, 0)] = parts[0]
        else:
            for index, part in enumerate(parts, start=1):
                machines[(site_id, key, index)] = part
    for index, endpoint in enumerate(loose, start=1):
        machines[(endpoint.site_id, endpoint.key, -index)] = [endpoint]
    return machines


def _split_by_keys(rows: list[ToolEndpoint]) -> list[list[ToolEndpoint]]:
    """Rows sharing one hostname, partitioned by the MACs and serials they carry."""
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owners: dict[str, int] = {}
    keyed: list[int] = []
    for index, row in enumerate(rows):
        keys = {f"mac:{m}" for m in row.macs}
        if row.serial:
            keys.add(f"serial:{row.serial}")
        if not keys:
            continue
        keyed.append(index)
        for key in keys:
            if key in owners:
                parent[find(index)] = find(owners[key])
            else:
                owners[key] = index
    groups: dict[int, list[ToolEndpoint]] = {}
    for index in keyed:
        groups.setdefault(find(index), []).append(rows[index])
    parts = list(groups.values())
    unkeyed = [row for index, row in enumerate(rows) if index not in keyed]
    if len(parts) <= 1:
        return [(parts[0] if parts else []) + unkeyed]
    return parts + [[row] for row in unkeyed]


@dataclass(slots=True)
class _Machine:
    key: MachineKey
    rows: list[ToolEndpoint]

    @property
    def site_id(self) -> SiteKey:
        return self.key[0]

    @property
    def key15(self) -> str:
        return self.key[1][:15]

    def keys(self, which: str) -> frozenset[str]:
        if which == "mac":
            return frozenset(m for r in self.rows for m in r.macs)
        return frozenset(r.serial for r in self.rows if r.serial)

    @property
    def name(self) -> str:
        return _best(self.rows).name or ""


@dataclass(slots=True)
class _Matcher:
    """The bookkeeping one tool's passes share: what pairs, and what is claimed."""

    tool: str
    machines: dict[MachineKey, _Machine]
    results: dict[str, DeviceCoverage]
    claimed: set[MachineKey] = field(default_factory=set)
    claimed_rows: set[int] = field(default_factory=set)
    """`id()` of every row a pairing covers: what the reverse leaves out."""
    paired: dict[str, list[ToolEndpoint]] = field(default_factory=dict)
    """Device id to the rows it paired with, for the RMM-stale decision."""

    def pair(self, device: ManagedDevice, keys: Sequence[MachineKey], how: str) -> None:
        rows = [r for k in keys for r in self.machines[k].rows]
        best = _best(rows)
        self.claimed.update(keys)
        self.claimed_rows.update(id(r) for r in rows)
        self.paired[device.device_id] = rows
        self.results[device.device_id].tools[self.tool] = ToolState(
            best.state,
            best.detail,
            best.last_seen,
            matched_by=how,
            endpoint_ids=tuple(r.external_id for r in rows if r.external_id),
        )

    def unmatched(
        self,
        device: ManagedDevice,
        detail: str,
        keys: Iterable[MachineKey],
        found_by: str,
        *,
        twins: Sequence[ManagedDevice] = (),
    ) -> None:
        keys = list(keys)
        self.results[device.device_id].tools[self.tool] = ToolState(
            "unmatched",
            detail,
            candidates=tuple(sorted(self.machines[k].name for k in keys))
            + tuple(f"device {t.device_id}" for t in sorted(twins, key=lambda d: d.device_id)),
            endpoint_ids=tuple(
                sorted(r.external_id for k in keys for r in self.machines[k].rows if r.external_id)
            ),
            found_by=found_by,
        )

    def open(self) -> list[_Machine]:
        return [m for k, m in self.machines.items() if k not in self.claimed]

    def decide(
        self,
        device: ManagedDevice,
        candidates: Sequence[MachineKey],
        rivals: Sequence[ManagedDevice],
        *,
        keys: Sequence[str] = DISAMBIGUATORS,
    ) -> tuple[list[MachineKey], str | None]:
        """Among the candidates a key pass found, the ones a stronger key singles out.

        Tried in `keys` order: the machines sharing that key with the device,
        leaving out any whose shared value a rival (another device the same
        pass found the same candidates for) carries too — a MAC three laptops
        report is a docking station or a VPN adapter, not an identity. Several
        machines may remain: they are the device's several rows. Returns the
        machines and the key that chose them, or ([], None).
        """
        for which in keys:
            mine = device.keys(which)
            if not mine:
                continue
            chosen: list[MachineKey] = []
            for key in candidates:
                shared = self.machines[key].keys(which) & mine
                if shared and not any(rival.keys(which) & shared for rival in rivals):
                    chosen.append(key)
            if chosen:
                return chosen, which
        return [], None


def assess(
    devices: Sequence[ManagedDevice],
    inventories: dict[str, ToolInventory | None],
    *,
    exclusions: Iterable[Exclusion] = (),
    now: dt.datetime,
    ninjaone_stale_days: int | None,
    ninja_sites: Iterable[SiteKey] | None = None,
    undated_tools: Iterable[str] = (),
) -> Assessment:
    """Every managed device's state for every tool, and every tool's unpaired endpoints.

    `inventories` is keyed by tool; None means the tool is not assessed at all
    for this client. `ninjaone_stale_days` None switches the `rmm_stale`
    dimension off (the review has no threshold of its own). `ninja_sites` is
    the population's sites; None means every site the devices name.
    `undated_tools` names the tools whose last-seen read failed this run: a
    lost device paired in one of them is `not_assessed` rather than judged.
    """
    undated = set(undated_tools)
    excluded = {(e.device_id, e.tool): e for e in exclusions}
    population_sites = set(ninja_sites) if ninja_sites is not None else {d.site_id for d in devices}
    results: dict[str, DeviceCoverage] = {}
    for device in devices:
        state, detail = ninjaone_state(device, now=now, stale_days=ninjaone_stale_days or 0)
        results[device.device_id] = DeviceCoverage(
            device=device, ninjaone_state=state, ninjaone_detail=detail
        )
    rmm_stale: set[str] = set()
    if ninjaone_stale_days is not None:
        rmm_stale = {
            d.device_id
            for d in devices
            if d.last_contact is None or (now - d.last_contact).days > ninjaone_stale_days
        }

    unmanaged: list[Unmanaged] = []
    discovered: list[Unmanaged] = []
    not_assessable: list[Unmanaged] = []
    external: list[Unmanaged] = []

    for tool, inventory in inventories.items():
        # --- what needs no pass -------------------------------------------------
        pending: list[ManagedDevice] = []
        for device in devices:
            result = results[device.device_id]
            exclusion = excluded.get((device.device_id, tool))
            if exclusion is not None:
                result.tools[tool] = ToolState("excluded", exclusion.reason)
            elif inventory is None:
                result.tools[tool] = ToolState("not_assessed", "tool not assessed")
            elif device.site_id not in inventory.sites:
                result.tools[tool] = ToolState("not_assessed", "tool has no company for this site")
            elif not device.key:
                if device.device_id in rmm_stale:
                    result.tools[tool] = ToolState(
                        "rmm_stale", f"no NinjaOne contact in {ninjaone_stale_days} days"
                    )
                else:
                    result.tools[tool] = ToolState(
                        "unmatched", "NinjaOne gives the device no hostname"
                    )
            else:
                pending.append(device)
        if inventory is None:
            continue

        # The passes see the rows that can pair; an RMM-stale device takes part
        # like any other, and what it paired with decides its state afterwards.
        machines = {
            key: _Machine(key, rows)
            for key, rows in group_machines(e for e in inventory.endpoints if e.pairable).items()
        }
        matcher = _Matcher(tool, machines, results)
        _run_passes(matcher, pending)
        for device in pending:
            if device.device_id in rmm_stale:
                results[device.device_id].tools[tool] = lost_state(
                    results[device.device_id].tools[tool],
                    matcher.paired.get(device.device_id, []),
                    tool=tool,
                    now=now,
                    stale_days=ninjaone_stale_days or 0,
                    dated=tool not in undated,
                )

        # --- the reverse -----------------------------------------------------------
        for rows in group_machines(inventory.endpoints).values():
            if any(id(r) in matcher.claimed_rows for r in rows):
                continue
            entry = unmanaged_entry(tool, rows)
            best = _best(rows)
            if best.external:
                external.append(entry)
            elif best.site_id not in population_sites:
                not_assessable.append(entry)
            elif best.discovered or best.probe:
                discovered.append(entry)
            else:
                unmanaged.append(entry)

    return Assessment(
        devices=list(results.values()),
        unmanaged=unmanaged,
        discovered=discovered,
        not_assessable=not_assessable,
        external=external,
    )


def _run_passes(matcher: _Matcher, pending: Sequence[ManagedDevice]) -> None:
    """The four passes for one tool, over the devices that need one."""
    pair, unmatched, decide = matcher.pair, matcher.unmatched, matcher.decide

    # --- pass 1: the exact key ---------------------------------------------
    # Every device sharing the key and every machine of the key are judged
    # together: one device and one machine pair; one device and several
    # machines takes the ones a stronger key singles out, or all of them when
    # nothing contradicts it; twins are told apart by a stronger key or left
    # unmatched naming each other.
    machines_by_key: dict[tuple[SiteKey, str], list[MachineKey]] = defaultdict(list)
    for key in matcher.machines:
        machines_by_key[(key[0], key[1])].append(key)
    devices_by_key: dict[tuple[SiteKey, str], list[ManagedDevice]] = defaultdict(list)
    for device in pending:
        devices_by_key[(device.site_id, device.key)].append(device)
    after_exact: list[ManagedDevice] = []
    for identity, group in devices_by_key.items():
        candidates = machines_by_key.get(identity, [])
        if not candidates:
            after_exact.extend(group)
        elif len(group) == 1:
            pair(group[0], candidates, "exact")
        else:
            taken: set[MachineKey] = set()
            for device in group:
                rivals = [d for d in group if d is not device]
                left = [c for c in candidates if c not in taken]
                chosen, how = decide(device, left, rivals)
                if chosen and how is not None:
                    taken.update(chosen)
                    pair(device, chosen, how)
                elif left:
                    unmatched(
                        device,
                        "two managed devices at this site share the hostname",
                        left,
                        "exact",
                        twins=rivals,
                    )
                else:
                    after_exact.append(device)

    # --- pass 2: the NetBIOS key, one device to one machine -------------------
    machines_by_15: dict[tuple[SiteKey, str], list[MachineKey]] = defaultdict(list)
    for machine in matcher.open():
        machines_by_15[(machine.site_id, machine.key15)].append(machine.key)
    devices_by_15: dict[tuple[SiteKey, str], list[ManagedDevice]] = defaultdict(list)
    for device in after_exact:
        devices_by_15[(device.site_id, device.key15)].append(device)
    after_netbios: list[ManagedDevice] = []
    for key15, group in devices_by_15.items():
        candidates = machines_by_15.get(key15, [])
        if not candidates:
            after_netbios.extend(group)
        elif len(group) == 1 and len(candidates) == 1:
            pair(group[0], candidates, "netbios15")
        else:
            taken = set()
            for device in group:
                rivals = [d for d in group if d is not device]
                left = [c for c in candidates if c not in taken]
                chosen, how = decide(device, left, rivals)
                if chosen and how is not None:
                    taken.update(chosen)
                    pair(device, chosen, how)
                elif len(left) > 1:
                    unmatched(
                        device,
                        "the first fifteen characters match more than one endpoint",
                        left,
                        "netbios15",
                    )
                elif left:
                    unmatched(
                        device,
                        "the first fifteen characters match an endpoint and another managed device",
                        left,
                        "netbios15",
                        twins=rivals,
                    )
                else:
                    after_netbios.append(device)

    # --- pass 3: a shared MAC; pass 4: a shared serial -------------------------
    # A value another pending device also carries is no identity (a docking
    # station, a VPN adapter, a placeholder serial) and is left out; a device
    # is paired on the values that are its alone.
    remaining = after_netbios
    for which, label in (("mac", "a MAC address"), ("serial", "a serial number")):
        machines_by_value: dict[tuple[SiteKey, str], set[MachineKey]] = defaultdict(set)
        for machine in matcher.open():
            for value in machine.keys(which):
                machines_by_value[(machine.site_id, value)].add(machine.key)
        devices_by_value: Counter[tuple[SiteKey, str]] = Counter(
            (d.site_id, v) for d in remaining for v in d.keys(which)
        )
        still: list[ManagedDevice] = []
        for device in remaining:
            own: set[MachineKey] = set()
            shared: set[MachineKey] = set()
            for value in device.keys(which):
                found = machines_by_value.get((device.site_id, value), set())
                if devices_by_value[(device.site_id, value)] > 1:
                    shared |= found
                else:
                    own |= found
            if len(own) == 1:
                pair(device, sorted(own), which)
            elif own:
                # Several machines on the device's own MACs: a serial may still choose.
                later = DISAMBIGUATORS[DISAMBIGUATORS.index(which) + 1 :]
                chosen, how = decide(device, sorted(own), [], keys=later)
                if how is not None and len(chosen) == 1:
                    pair(device, chosen, how)
                else:
                    unmatched(device, f"{label} matches more than one endpoint", sorted(own), which)
            elif shared:
                unmatched(
                    device, f"{label} is shared with another managed device", sorted(shared), which
                )
            else:
                still.append(device)
        remaining = still
    for device in remaining:
        matcher.results[device.device_id].tools[matcher.tool] = ToolState(
            "absent", "no endpoint of this hostname, MAC or serial"
        )

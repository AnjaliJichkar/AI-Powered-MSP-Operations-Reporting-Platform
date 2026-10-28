"""Why a device and an endpoint did not pair: a diagnostic beside the join, not in it.

The board's join (`core.assess`) matches on one thing — the normalised
hostname, within one site — and refuses to guess. This module asks, for every
gap the join left, what *would* have paired it, so the counts can decide
which of the earlier audit's looser rules are worth adopting:

- **site mismatch**: the exact hostname is in the other inventory at a
  different site. That is a mapping problem, and both sites are named.
- **normalisation**: the names match after a rule the join does not apply —
  an alternate name field (Bitdefender's `fqdn` or `label`, NinjaOne's
  `dnsName`, `displayName` or the interface's `dnsHostName`, ConnectSecure's
  `name`), the legacy audit's 15-character NetBIOS truncation, dashes,
  underscores and spaces treated alike, whitespace inside the name, or all of
  those together. The first rule that pairs it is named.
- **secondary key**: a MAC address or a serial number the two rows share at
  the same site. Bitdefender rows carry MACs; ConnectSecure assets carry a
  MAC and a serial; NinjaOne's come from the `network-interfaces` and
  `computer-systems` queries. Which key paired it is named.
- **none**: nothing above pairs it.

Two more dimensions the board does not have yet:

- a NinjaOne device with no contact for more than `NINJAONE_LAST_CONTACT_DAYS`
  is **rmm_stale** — reported on its own, never as `absent`, because a
  machine the RMM has not heard from cannot be expected in anything else;
- an unmanaged endpoint is split three ways: at a site with **no NinjaOne
  mapping** (not assessable), **discovered** rather than an endpoint
  (Bitdefender `isManaged: false`, ConnectSecure probes and deprecated or
  retired agents), and the **real** remainder, which gets the diagnosis above.

Everything here is read-only and changes no board number. The legacy audit's
tenant-wide match rate is computed beside the board's within-site one so both
are visible with their definitions.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.connectors.bitdefender.models import BdEndpoint
from app.connectors.connectsecure.models import CsAsset
from app.connectors.matching import normalise_hostname
from app.connectors.ninjaone.models import NinjaDevice
from app.connectors.scope import SiteRef
from app.coverage.config import (
    NINJAONE_LAST_CONTACT_DAYS,
    PROBE_AGENT_TYPES,
    bitdefender_endpoints,
    mac_key,
    serial_key,
)
from app.coverage.core import is_agent_class

Category = Literal["site_mismatch", "normalisation", "secondary_key", "none", "rmm_stale"]
UnmanagedClass = Literal["not_assessable", "discovered", "real"]

#: The order the normalisations are tried, and their names in the output.
NORMALISATIONS = ("alt_field", "netbios15", "separators", "whitespace", "combined")

# --- keys ------------------------------------------------------------------------


def legacy_key(name: str | None) -> str:
    """The earlier audit's key: lower, whitespace stripped, first label, 15 characters."""
    return normalise_hostname(name)[:15]


def _no_separators(key: str) -> str:
    return re.sub(r"[-_ ]", "", key)


def _no_whitespace(key: str) -> str:
    return re.sub(r"\s+", "", key)


Variants = dict[str, dict[str, str]]
"""rule -> normalised key -> the label of the field it came from."""


def variants(names: Iterable[tuple[str, str | None]]) -> Variants:
    """Every normalised form of a set of `(field label, raw name)` pairs, by rule.

    `exact` is the join's own key of the first (primary) field; `alt_field` the
    join's key of every other field; the rest apply their rule to every field.
    `combined` applies all of them at once. Each key remembers the field it
    came from, so a pairing can say which field would have made it.
    """
    raw = [(label, n) for label, n in names if n]
    out: Variants = {rule: {} for rule in ("exact", *NORMALISATIONS)}
    for index, (label, name) in enumerate(raw):
        key = normalise_hostname(name)
        if not key:
            continue
        out["exact" if index == 0 else "alt_field"].setdefault(key, label)
        out["netbios15"].setdefault(key[:15], label)
        out["separators"].setdefault(_no_separators(key), label)
        out["whitespace"].setdefault(_no_whitespace(key), label)
        out["combined"].setdefault(_no_separators(_no_whitespace(key))[:15], label)
    return out


# --- the records the diagnostic works on ------------------------------------------


@dataclass(frozen=True, slots=True)
class NinjaExtra:
    """What the device list does not carry: from `computer-systems` and `network-interfaces`."""

    serial: str | None = None
    macs: frozenset[str] = frozenset()
    dns_host: str | None = None


@dataclass(slots=True)
class Record:
    """One row of one inventory, with every key the diagnostic can match on."""

    tool: str
    site_id: uuid.UUID
    site_slug: str
    site_name: str
    id: str
    name: str
    legacy_name: str | None = None
    """The name the earlier audit keyed on: NinjaOne `systemName` or `dnsName`,
    Bitdefender `fqdn` or `name`, ConnectSecure `host_name` or `name`."""
    node_class: str | None = None
    names: Variants = field(default_factory=dict)
    macs: frozenset[str] = frozenset()
    serial: str | None = None
    last_seen: dt.datetime | None = None
    discovered: str | None = None
    """Why this endpoint is discovered rather than installed, when it is."""

    @property
    def key(self) -> str:
        return next(iter(self.names.get("exact", ())), "")


def ninja_records(
    rows: Sequence[tuple[SiteRef, NinjaDevice]], extras: Mapping[int, NinjaExtra]
) -> tuple[list[Record], list[Record]]:
    """The population as records, and the NinjaOne devices that are not computers."""
    computers: list[Record] = []
    others: list[Record] = []
    for site, device in rows:
        extra = extras.get(device.id, NinjaExtra())
        record = Record(
            tool="ninjaone",
            site_id=site.site_id,
            site_slug=site.slug,
            site_name=site.name,
            id=str(device.id),
            name=device.name,
            legacy_name=device.system_name or device.dns_name,
            node_class=device.node_class,
            names=variants(
                [
                    ("name", device.name),
                    ("systemName", device.system_name),
                    ("dnsName", device.dns_name),
                    ("displayName", device.display_name),
                    ("interface.dnsHostName", extra.dns_host),
                ]
            ),
            macs=extra.macs,
            serial=serial_key(extra.serial),
            last_seen=device.last_contact,
        )
        (computers if is_agent_class(device.node_class) else others).append(record)
    return computers, others


def bitdefender_records(rows: Sequence[tuple[SiteRef, BdEndpoint]]) -> list[Record]:
    judged = {e.external_id: e for e in bitdefender_endpoints(rows)}
    out: list[Record] = []
    for site, endpoint in rows:
        verdict = judged.get(endpoint.id)
        discovered = None
        if endpoint.is_managed is False:
            discovered = "isManaged false"
        elif not endpoint.has_agent:
            discovered = "no agent (managedWithBest false)"
        out.append(
            Record(
                tool="bitdefender",
                site_id=site.site_id,
                site_slug=site.slug,
                site_name=site.name,
                id=endpoint.id,
                name=endpoint.name or "",
                legacy_name=endpoint.fqdn or endpoint.name,
                names=variants(
                    [("name", endpoint.name), ("fqdn", endpoint.fqdn), ("label", endpoint.label)]
                ),
                macs=frozenset(k for k in (mac_key(m) for m in endpoint.macs or []) if k),
                last_seen=verdict.last_seen if verdict else None,
                discovered=discovered,
            )
        )
    return out


def connectsecure_records(rows: Sequence[tuple[SiteRef, CsAsset]]) -> list[Record]:
    out: list[Record] = []
    for site, asset in rows:
        agent = asset.agent
        discovered = None
        agent_type = (asset.agent_type or (agent.agent_type if agent else None) or "").upper()
        if agent_type in PROBE_AGENT_TYPES or (asset.asset_type or "").upper() in ("PROBE",):
            discovered = f"probe ({agent_type or asset.asset_type})"
        elif agent is None:
            discovered = "no agent joined"
        elif agent.is_deprecated or agent.is_retired or agent.agent_deleted:
            discovered = "agent deprecated, retired or deleted"
        elif asset.is_deprecated:
            discovered = "asset deprecated"
        mac = mac_key(asset.mac)
        out.append(
            Record(
                tool="connectsecure",
                site_id=site.site_id,
                site_slug=site.slug,
                site_name=site.name,
                id=asset.id,
                name=asset.host_name or "",
                legacy_name=asset.host_name or asset.name,
                names=variants([("host_name", asset.host_name), ("name", asset.name)]),
                macs=frozenset({mac}) if mac else frozenset(),
                serial=serial_key(asset.serial_number),
                last_seen=asset.last_scan,
                discovered=discovered,
            )
        )
    return out


# --- the diagnosis ------------------------------------------------------------------


@dataclass(slots=True)
class Diagnosis:
    category: Category
    detail: str
    """Which site, which rule, which key: the evidence."""
    other_site: str | None = None
    matched_name: str | None = None


@dataclass(slots=True)
class GapRow:
    kind: Literal["absent", "unmanaged"]
    tool: str
    site_slug: str
    site_name: str
    id: str
    name: str
    node_class: str | None
    last_seen: dt.datetime | None
    diagnosis: Diagnosis
    unmanaged_class: UnmanagedClass | None = None
    discovered: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tool": self.tool,
            "site": self.site_name,
            "site_slug": self.site_slug,
            "id": self.id,
            "name": self.name,
            "node_class": self.node_class or "",
            "last_seen": self.last_seen.isoformat() if self.last_seen else "",
            "unmanaged_class": self.unmanaged_class or "",
            "discovered": self.discovered or "",
            "category": self.diagnosis.category,
            "detail": self.diagnosis.detail,
            "other_site": self.diagnosis.other_site or "",
            "matched_name": self.diagnosis.matched_name or "",
        }


class Index:
    """One inventory's records by every key the diagnostic can look up."""

    def __init__(self, records: Iterable[Record]) -> None:
        self.by_rule: dict[str, dict[tuple[uuid.UUID, str], list[tuple[Record, str]]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        self.exact_anywhere: dict[str, list[Record]] = defaultdict(list)
        self.by_mac: dict[tuple[uuid.UUID, str], list[Record]] = defaultdict(list)
        self.by_serial: dict[tuple[uuid.UUID, str], list[Record]] = defaultdict(list)
        for record in records:
            for rule, keys in record.names.items():
                for key, label in keys.items():
                    self.by_rule[rule][(record.site_id, key)].append((record, label))
            if record.key:
                self.exact_anywhere[record.key].append(record)
            for mac in record.macs:
                self.by_mac[(record.site_id, mac)].append(record)
            if record.serial:
                self.by_serial[(record.site_id, record.serial)].append(record)


def diagnose(record: Record, other: Index) -> Diagnosis:
    """Why `record` did not pair with anything in `other` at its site."""
    # 1. The exact key at another site: a mapping problem.
    elsewhere = [r for r in other.exact_anywhere.get(record.key, []) if r.site_id != record.site_id]
    if record.key and elsewhere:
        sites = sorted({r.site_name for r in elsewhere})
        return Diagnosis(
            "site_mismatch",
            f"exact hostname is in {elsewhere[0].tool} at {', '.join(sites)}",
            other_site=", ".join(sites),
            matched_name=elsewhere[0].name,
        )
    # 2. A normalisation the join does not apply, at the same site. Each side's
    # variant under the rule is compared with the other side's under the same
    # rule, and the alternate-field rule also against the other side's exact key.
    for rule in NORMALISATIONS:
        mine = dict(record.names.get(rule, {}))
        if rule != "alt_field":
            mine.update(record.names.get("exact", {}))
        theirs = other.by_rule.get(rule, {})
        exact = other.by_rule.get("exact", {})
        for key, my_label in mine.items():
            hits = theirs.get((record.site_id, key), []) + (
                exact.get((record.site_id, key), []) if rule == "alt_field" else []
            )
            if hits:
                hit, their_label = hits[0]
                return Diagnosis(
                    "normalisation",
                    f"{rule}: {record.tool}.{my_label} ~ {hit.tool}.{their_label}",
                    matched_name=hit.name,
                )
    # 3. A secondary key at the same site.
    for mac in sorted(record.macs):
        by_mac = other.by_mac.get((record.site_id, mac), [])
        if by_mac:
            return Diagnosis("secondary_key", "mac", matched_name=by_mac[0].name)
    if record.serial:
        by_serial = other.by_serial.get((record.site_id, record.serial), [])
        if by_serial:
            return Diagnosis("secondary_key", "serial", matched_name=by_serial[0].name)
    return Diagnosis("none", "no name, MAC or serial pairs it")


# --- the whole report ---------------------------------------------------------------


@dataclass(slots=True)
class DuplicateGroup:
    site_slug: str
    site_name: str
    key: str
    devices: list[Record]

    @property
    def serial_distinguishes(self) -> bool:
        serials = [d.serial for d in self.devices]
        return all(serials) and len(set(serials)) == len(serials)

    def contact_distinguishes(self, *, now: dt.datetime, stale_days: int) -> bool:
        """Exactly one of them has been seen within the threshold."""
        live = [
            d
            for d in self.devices
            if d.last_seen is not None and (now - d.last_seen).days < stale_days
        ]
        return len(live) == 1


@dataclass(slots=True)
class SiteDiagnostic:
    site_slug: str
    site_name: str
    managed: int = 0
    rmm_stale: int = 0
    absent: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    """Per tool: category -> count, for the absent (and not rmm-stale) devices."""
    unmanaged: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    """Per tool: unmanaged class -> count, and for the real ones category -> count."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "site": self.site_name,
            "site_slug": self.site_slug,
            "managed": self.managed,
            "rmm_stale": self.rmm_stale,
            "absent": {tool: dict(c) for tool, c in self.absent.items()},
            "unmanaged": {tool: dict(c) for tool, c in self.unmanaged.items()},
        }


@dataclass(slots=True)
class MatchRates:
    """Two definitions of "matched", side by side."""

    within_site: dict[str, float | None]
    """The board's: managed computers with an endpoint of the same normalised hostname at
    the same site (covered or stale), as a share of the computers at sites the tool
    answered for. Excluded and not-assessed devices are outside the denominator."""
    tenant_wide: dict[str, float | None]
    """The legacy audit's: computers whose 15-character key appears anywhere in the
    tool's inventory, any site, as a share of every managed computer. Bitdefender is
    keyed on `fqdn` or `name`, NinjaOne on `systemName` or `dnsName`, as the audit did."""
    tenant_wide_active: dict[str, float | None]
    """The legacy audit's `_active` variant: the same, over computers with a contact in
    the last `NINJAONE_LAST_CONTACT_DAYS` days only."""


@dataclass(slots=True)
class Diagnostic:
    computed_at: dt.datetime
    stale_days: int
    managed: int
    rmm_stale: int
    not_computers: int
    rows: list[GapRow]
    sites: list[SiteDiagnostic]
    duplicates: list[DuplicateGroup]
    rates: MatchRates
    tools: list[str]

    def counts(self) -> dict[str, Any]:
        """The category counts, per tool, for the report."""
        absent: dict[str, Counter[str]] = defaultdict(Counter)
        unmanaged_class: dict[str, Counter[str]] = defaultdict(Counter)
        unmanaged_real: dict[str, Counter[str]] = defaultdict(Counter)
        norm_rules: dict[str, Counter[str]] = defaultdict(Counter)
        keys: dict[str, Counter[str]] = defaultdict(Counter)
        for row in self.rows:
            if row.kind == "absent":
                absent[row.tool][row.diagnosis.category] += 1
            else:
                unmanaged_class[row.tool][row.unmanaged_class or "real"] += 1
                if row.unmanaged_class == "real":
                    unmanaged_real[row.tool][row.diagnosis.category] += 1
            if row.diagnosis.category == "normalisation":
                norm_rules[row.tool][row.diagnosis.detail] += 1
            if row.diagnosis.category == "secondary_key":
                keys[row.tool][row.diagnosis.detail] += 1
        return {
            "managed": self.managed,
            "rmm_stale": self.rmm_stale,
            "not_computers": self.not_computers,
            "absent_by_category": {t: dict(c) for t, c in absent.items()},
            "unmanaged_by_class": {t: dict(c) for t, c in unmanaged_class.items()},
            "unmanaged_real_by_category": {t: dict(c) for t, c in unmanaged_real.items()},
            "normalisation_rules": {t: dict(c) for t, c in norm_rules.items()},
            "secondary_keys": {t: dict(c) for t, c in keys.items()},
            "duplicates": {
                "groups": len(self.duplicates),
                "serial_distinguishes": sum(1 for g in self.duplicates if g.serial_distinguishes),
                "contact_distinguishes": sum(
                    1
                    for g in self.duplicates
                    if g.contact_distinguishes(now=self.computed_at, stale_days=self.stale_days)
                ),
                "neither": sum(
                    1
                    for g in self.duplicates
                    if not g.serial_distinguishes
                    and not g.contact_distinguishes(
                        now=self.computed_at, stale_days=self.stale_days
                    )
                ),
            },
            "match_rates": {
                "within_site": self.rates.within_site,
                "tenant_wide": self.rates.tenant_wide,
                "tenant_wide_active": self.rates.tenant_wide_active,
            },
        }


def run_diagnostic(
    *,
    ninja: Sequence[tuple[SiteRef, NinjaDevice]],
    extras: Mapping[int, NinjaExtra],
    ninja_sites: Iterable[uuid.UUID],
    tools: Mapping[str, tuple[Sequence[Any], Iterable[uuid.UUID]]],
    now: dt.datetime,
    stale_days: int = NINJAONE_LAST_CONTACT_DAYS,
) -> Diagnostic:
    """Classify every gap. `tools` maps a tool key to (its rows, the sites it answered for).

    Pure: nothing is read here. The caller (`scripts/coverage_diagnose.py`)
    gathers the rows through the connectors and hands them in.
    """
    computers, others = ninja_records(ninja, extras)
    ninja_site_ids = set(ninja_sites)
    ninja_index = Index(computers)
    rows: list[GapRow] = []
    sites: dict[uuid.UUID, SiteDiagnostic] = {}

    def site_of(record: Record) -> SiteDiagnostic:
        return sites.setdefault(
            record.site_id, SiteDiagnostic(site_slug=record.site_slug, site_name=record.site_name)
        )

    stale: set[str] = set()
    for device in computers:
        summary = site_of(device)
        summary.managed += 1
        if device.last_seen is None or (now - device.last_seen).days > stale_days:
            stale.add(device.id)
            summary.rmm_stale += 1

    # The join's own within-site pairing, recomputed here on the join's key so
    # the diagnostic never has to be told which devices were absent.
    within: dict[str, set[str]] = {}
    tenant: dict[str, set[str]] = {}
    tenant_keys: dict[str, set[str]] = {}
    tool_records: dict[str, list[Record]] = {}
    tool_sites: dict[str, set[uuid.UUID]] = {}
    for tool, (tool_rows, answered) in tools.items():
        records = (
            bitdefender_records(tool_rows)
            if tool == "bitdefender"
            else connectsecure_records(tool_rows)
            if tool == "connectsecure"
            else []
        )
        tool_records[tool] = records
        tool_sites[tool] = set(answered)
        index = Index(records)
        paired_keys = {(r.site_id, r.key) for r in records if r.key}
        tenant_keys[tool] = {
            legacy_key(r.legacy_name) for r in records if legacy_key(r.legacy_name)
        }
        paired_devices: set[str] = set()
        for device in computers:
            if device.site_id not in tool_sites[tool]:
                continue
            if device.key and (device.site_id, device.key) in paired_keys:
                paired_devices.add(device.id)
                continue
            summary = site_of(device)
            if device.id in stale:
                diagnosis = Diagnosis("rmm_stale", f"no NinjaOne contact in {stale_days} days")
            else:
                diagnosis = diagnose(device, index)
            summary.absent[tool][diagnosis.category] += 1
            rows.append(
                GapRow(
                    kind="absent",
                    tool=tool,
                    site_slug=device.site_slug,
                    site_name=device.site_name,
                    id=device.id,
                    name=device.name,
                    node_class=device.node_class,
                    last_seen=device.last_seen,
                    diagnosis=diagnosis,
                )
            )
        within[tool] = paired_devices
        # The reverse: endpoints with no computer of that key at their site.
        computer_keys = {(c.site_id, c.key) for c in computers if c.key}
        seen: set[tuple[uuid.UUID, str]] = set()
        for record in records:
            if record.key and (record.site_id, record.key) in computer_keys:
                continue
            if record.key and (record.site_id, record.key) in seen:
                continue
            seen.add((record.site_id, record.key))
            summary = site_of(record)
            if record.site_id not in ninja_site_ids:
                klass: UnmanagedClass = "not_assessable"
                diagnosis = Diagnosis("none", "site has no NinjaOne mapping")
            elif record.discovered:
                klass = "discovered"
                diagnosis = Diagnosis("none", record.discovered)
            else:
                klass = "real"
                diagnosis = diagnose(record, ninja_index)
            summary.unmanaged[tool][klass] += 1
            if klass == "real":
                summary.unmanaged[tool][f"real:{diagnosis.category}"] += 1
            rows.append(
                GapRow(
                    kind="unmanaged",
                    tool=tool,
                    site_slug=record.site_slug,
                    site_name=record.site_name,
                    id=record.id,
                    name=record.name,
                    node_class=None,
                    last_seen=record.last_seen,
                    diagnosis=diagnosis,
                    unmanaged_class=klass,
                    discovered=record.discovered,
                )
            )
        tenant[tool] = {c.id for c in computers if legacy_key(c.legacy_name) in tenant_keys[tool]}

    # Duplicate hostnames within a site.
    groups: dict[tuple[uuid.UUID, str], list[Record]] = defaultdict(list)
    for device in computers:
        if device.key:
            groups[(device.site_id, device.key)].append(device)
    duplicates = [
        DuplicateGroup(
            site_slug=members[0].site_slug,
            site_name=members[0].site_name,
            key=key,
            devices=members,
        )
        for (_, key), members in sorted(
            groups.items(), key=lambda kv: (kv[1][0].site_slug, kv[0][1])
        )
        if len(members) > 1
    ]

    def share(numerator: int, denominator: int) -> float | None:
        return round(100 * numerator / denominator, 1) if denominator else None

    active = [c for c in computers if c.id not in stale]
    rates = MatchRates(
        within_site={
            tool: share(
                len(within[tool]),
                sum(1 for c in computers if c.site_id in tool_sites[tool]),
            )
            for tool in tools
        },
        tenant_wide={tool: share(len(tenant[tool]), len(computers)) for tool in tools},
        tenant_wide_active={
            tool: share(sum(1 for c in active if c.id in tenant[tool]), len(active))
            for tool in tools
        },
    )
    ordered = sorted(
        sites.values(),
        key=lambda s: (
            -sum(
                n
                for tool_counts in s.absent.values()
                for category, n in tool_counts.items()
                if category != "rmm_stale"
            ),
            s.site_name,
        ),
    )
    return Diagnostic(
        computed_at=now,
        stale_days=stale_days,
        managed=len(computers),
        rmm_stale=len(stale),
        not_computers=len(others),
        rows=rows,
        sites=ordered,
        duplicates=duplicates,
        rates=rates,
        tools=list(tools),
    )

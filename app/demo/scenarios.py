"""What the demo world must contain, by name, and the check that it does.

Each scenario is something a page, a review or a tool must show a prospect:
the coverage states, the two-tool gaps, the licence edge cases, the ticket
patterns. They are placed on purpose in `dataset._place_scenarios`, not left
to the random draw, and `check(world)` returns the ones it cannot find, so a
change to the generator that loses one fails `scripts/demo_tenant.py create`
and the tests before it reaches a demo.

`SCENARIOS` is the document: `scripts/demo_tenant.py create` prints its size,
and docs/demo-tenant.md is generated from it (`describe()`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.demo import vocabulary as V
from app.demo.dataset import World


@dataclass(frozen=True, slots=True)
class Scenario:
    key: str
    where: str
    """The customer (or customers) it is placed on."""
    shows: str
    """What it demonstrates, in a sentence."""
    present: Callable[[World], bool]


def _devices(world: World, scenario: str) -> int:
    return sum(1 for d in world.devices if d.scenario == scenario)


_RESOLVED_TITLES = frozenset(V.RESOLUTIONS)


def _vendor_only(world: World, kind: str) -> int:
    return sum(1 for e in world.vendor_only if e.kind == kind)


SCENARIOS: tuple[Scenario, ...] = (
    # --- coverage states -------------------------------------------------------------------
    Scenario(
        "coverage.covered",
        "every customer",
        "Most managed computers are covered: a current GravityZone row and a recent scan.",
        lambda w: (
            sum(1 for d in w.devices if not d.scenario and d.in_bitdefender and d.in_connectsecure)
            > 150
        ),
    ),
    Scenario(
        "coverage.netbios_collision",
        "Contoso Ltd",
        "Two accounting machines whose names differ only after the fifteenth character share "
        "one NetBIOS `systemName`; `dnsName` is whole, and MAC and serial tell them apart.",
        lambda w: _devices(w, "netbios_collision") == 2,
    ),
    Scenario(
        "coverage.unmatched",
        "Tailspin Toys",
        "Two reimaged spare laptops enrolled under one name whose vendor rows carry no MAC and no "
        "serial: the name pass is ambiguous, nothing decides, both stay unmatched with each other "
        "named.",
        lambda w: _devices(w, "unmatched_twins") == 2,
    ),
    Scenario(
        "coverage.not_computers",
        "every customer",
        "A switch per customer: network gear NinjaOne lists, counted and left out.",
        lambda w: sum(1 for d in w.devices if d.node_class == "NMS_SWITCH") == 8,
    ),
    Scenario(
        "coverage.rmm_stale",
        "Contoso Ltd",
        "A device NinjaOne has not heard from in 31 days, and neither has any other tool.",
        lambda w: _devices(w, "rmm_stale") == 1,
    ),
    Scenario(
        "coverage.rmm_agent_down",
        "Contoso Ltd",
        "A device NinjaOne lost 21 days ago that GravityZone heard from this morning: the "
        "machine is alive, the RMM agent is not.",
        lambda w: _devices(w, "rmm_agent_down") == 1,
    ),
    Scenario(
        "coverage.never_contacted",
        "Contoso Ltd",
        "A device enrolled in NinjaOne that has never checked in.",
        lambda w: _devices(w, "never_contacted") == 1,
    ),
    Scenario(
        "coverage.absent_no_agent",
        "Northwind Traders",
        "Managed computers with no GravityZone row at all: the 'no protection agent' gap.",
        lambda w: (
            _devices(w, "no_protection_agent") == 2 and _devices(w, "unprotected_and_dark") == 1
        ),
    ),
    Scenario(
        "coverage.unprotected_and_dark",
        "Northwind Traders",
        "One machine with no protection agent that has also not checked in for over a week: "
        "unprotected and unseen, the worst square on the board.",
        lambda w: any(
            d.scenario == "unprotected_and_dark"
            and not d.in_bitdefender
            and (d.last_contact_days or 999) > 7
            for d in w.devices
        ),
    ),
    Scenario(
        "autotask.open_sla_breaches",
        "every customer",
        "Two tickets per customer open now and already past their due time: what "
        "'which tickets are breaching SLA' shows today.",
        lambda w: sum(1 for t in w.tickets if t.scenario == "sla_breach_open") == 16,
    ),
    Scenario(
        "ninjaone.alerts_today",
        "every customer",
        "Open alerts raised in the last day at every customer, four at Northwind: "
        "'anything wrong at X today' is never an empty answer.",
        lambda w: all(
            sum(
                1
                for a in w.alerts
                if a.resolved_days is None
                and a.created_days < 1
                and w.device(a.device_id).customer_key == c.key
            )
            >= 2
            for c in w.customers
        ),
    ),
    Scenario(
        "autotask.resolution_notes",
        "every customer",
        "Every resolved ticket says what was wrong and what fixed it, so the ticket "
        "workspace can cite a similar one.",
        lambda w: all(
            "Root cause:" in t.resolution
            for t in w.tickets
            if t.completed_days is not None and t.title in _RESOLVED_TITLES
        ),
    ),
    Scenario(
        "coverage.not_scanned",
        "Northwind Traders",
        "Managed computers ConnectSecure has never scanned, and one not scanned in 45 days.",
        lambda w: _devices(w, "not_scanned") == 2 and _devices(w, "stale_scan") == 1,
    ),
    Scenario(
        "coverage.unmanaged",
        "Fabrikam Inc",
        "A real machine both tools hold that NinjaOne does not manage (a reception kiosk).",
        lambda w: _vendor_only(w, "unmanaged") == 2,
    ),
    Scenario(
        "coverage.default_windows_names",
        "Litware Inc",
        "Endpoints named DESKTOP-… and LAPTOP-…: real machines, likely not yet onboarded.",
        lambda w: _vendor_only(w, "default_name") == 2,
    ),
    Scenario(
        "coverage.external",
        "Northwind Traders",
        "ConnectSecure scan targets named by an IP address and by a public host name: external, "
        "never an unmanaged endpoint.",
        lambda w: _vendor_only(w, "external") == 2,
    ),
    Scenario(
        "coverage.discovered",
        "Tailspin Toys",
        "A GravityZone row that is not managed and a ConnectSecure probe: discovered, "
        "reported apart.",
        lambda w: _vendor_only(w, "discovered") == 2,
    ),
    # --- vendor edge cases ------------------------------------------------------------------
    Scenario(
        "bitdefender.two_companies",
        "Fabrikam Inc",
        "One customer holding two GravityZone companies (a hand-made one and the integration's, "
        "with its six-character suffix); the site is read as their union.",
        lambda w: (
            len(w.customer("fabrikam").bitdefender_company_ids) == 2
            and {d.bitdefender_company_index for d in w.devices_of("fabrikam")} == {0, 1}
        ),
    ),
    Scenario(
        "bitdefender.av_only",
        "Fourth Coffee",
        "An antivirus-only customer: a licence that manages antimalware alone, no "
        "vulnerability scanner.",
        lambda w: (
            w.customer("fourth").av_only and w.customer("fourth").connectsecure_company_id is None
        ),
    ),
    Scenario(
        "bitdefender.encryption_not_licensed",
        "every customer",
        "No licence manages encryption, while BitLocker already covers most Windows devices: "
        "the gap is the unencrypted minority, never the whole fleet.",
        lambda w: (
            0.75
            < sum(1 for d in w.devices for v in d.volumes if v.bitlocker == "Encrypted")
            / max(1, sum(1 for d in w.devices for v in d.volumes if v.bitlocker is not None))
            < 0.95
        ),
    ),
    Scenario(
        "bitdefender.edr_not_licensed",
        "seven of eight customers",
        "EDR is not licensed, so no detection can exist and the review says so rather than "
        "'quiet'.",
        lambda w: sum(1 for c in w.customers if not c.edr_licensed) == 7,
    ),
    Scenario(
        "bitdefender.edr_detections",
        "Woodgrove Bank",
        "The one customer with EDR licensed, and ninety days of detections on it.",
        lambda w: w.customer("woodgrove").edr_licensed and len(w.detections) >= 10,
    ),
    Scenario(
        "ninjaone.windows_10_iot_classed_server",
        "Adventure Works Cycles",
        "A till on Windows 10 IoT Enterprise that NinjaOne classes WINDOWS_SERVER: counted as a "
        "workstation by its operating system, and listed on the internal render.",
        lambda w: _devices(w, "windows_10_iot_classed_server") == 1,
    ),
    # --- vulnerabilities -----------------------------------------------------------------------
    Scenario(
        "connectsecure.confirmed_and_unconfirmed_criticals",
        "every scanned customer",
        "Critical findings in both populations, confirmed and suspected, never added together.",
        lambda w: (
            sum(1 for f in w.findings if f.severity == "CRITICAL" and f.confirmed) > 20
            and sum(1 for f in w.findings if f.severity == "CRITICAL" and not f.confirmed) > 20
        ),
    ),
    Scenario(
        "connectsecure.known_exploited",
        "every scanned customer",
        "Findings on CISA's Known Exploited Vulnerabilities catalogue, confirmed on some machines.",
        lambda w: sum(1 for f in w.findings if f.kev and f.confirmed) > 10,
    ),
    Scenario(
        "connectsecure.version_remediations",
        "every scanned customer",
        "Fixes that are a target version (Update Google Chrome to 139.0.7258.128), joined to "
        "NinjaOne's pending software patches.",
        lambda w: (
            sum(1 for f in w.findings if f.remediation_type == "Update" and " to " in f.fix) > 50
        ),
    ),
    Scenario(
        "connectsecure.uninstall_remediations",
        "every scanned customer",
        "Software to remove (uTorrent, CCleaner, an old Java): `Uninstall` remediations, "
        "their own finding.",
        lambda w: sum(1 for f in w.findings if f.remediation_type == "Uninstall") >= 5,
    ),
    # --- tickets ---------------------------------------------------------------------------
    Scenario(
        "autotask.sla_breaches",
        "every customer",
        "About one ticket in eight misses its first-response or resolution due time.",
        lambda w: (
            0.06 < sum(1 for t in w.tickets if t.scenario == "sla_breach") / len(w.tickets) < 0.2
        ),
    ),
    Scenario(
        "autotask.automated_contacts",
        "every customer",
        "Tickets raised by monitoring (source 'Monitoring Alert', no contact): counted apart "
        "from people.",
        lambda w: sum(1 for t in w.tickets if t.contact is None) >= 24,
    ),
    Scenario(
        "autotask.repeat_issue",
        "every customer",
        "The same printer, the same person, most weeks: a repeat issue a plan can cite "
        "resolved tickets for.",
        lambda w: sum(1 for t in w.tickets if t.scenario == "repeat_issue") >= 40,
    ),
    Scenario(
        "autotask.issue_type_unset",
        "every customer",
        "Some tickets carry no issue type, so the review's 'largely unset' rule has "
        "something to measure.",
        lambda w: sum(1 for t in w.tickets if t.contact and not t.issue_type) >= 20,
    ),
    # --- patching -----------------------------------------------------------------------------
    Scenario(
        "ninjaone.patch_install_failures",
        "every customer",
        "OS and third-party patch installs that failed, on a few devices each month.",
        lambda w: (
            sum(1 for i in w.installs if i.status == "FAILED" and i.kind == "os") >= 20
            and sum(1 for i in w.installs if i.status == "FAILED" and i.kind == "software") >= 5
        ),
    ),
    Scenario(
        "ninjaone.pending_critical_patches",
        "every customer",
        "Approved patches waiting to install, some critical, so the patch backlog has a number.",
        lambda w: sum(1 for p in w.pending if p.kind == "os" and p.severity == "CRITICAL") >= 10,
    ),
    Scenario(
        "ninjaone.windows_10_ineligible",
        "several customers",
        "Windows 10 machines on processors Windows 11 does not support (6th and 8th generation "
        "Core): the replacement list.",
        lambda w: (
            sum(
                1
                for d in w.devices
                if "Windows 10 Pro" in d.os_name
                and ("i5-6500" in d.processor or "i5-8265U" in d.processor)
            )
            >= 5
        ),
    ),
    # --- backup ------------------------------------------------------------------------
    Scenario(
        "backup.sold_to_a_customer",
        "six of eight customers",
        "Where backup is sold it is on every machine, which is what the board's Backup column "
        "asks about: a managed computer with no Cove device is the gap it is for.",
        lambda w: sum(1 for d in w.devices if d.in_cove) > 150,
    ),
    Scenario(
        "backup.not_sold",
        "Tailspin Toys, Fourth Coffee",
        "Two customers buy no backup at all, so the board has somewhere to say `not assessed` "
        "for a whole customer rather than counting every one of their machines as a gap.",
        lambda w: sum(1 for c in w.customers if c.cove_partner_id is None) == 2,
    ),
    Scenario(
        "backup.microsoft365_is_a_device",
        "every customer who buys backup",
        "Cove holds each customer's Microsoft 365 tenant as a device row, told apart only by "
        "`I59`, so every count about machines has to exclude it rather than assume the list "
        "is machines.",
        lambda w: sum(1 for c in w.customers if c.cove_partner_id is not None) == 6,
    ),
    Scenario(
        "backup.behind",
        "Contoso Ltd",
        "A server whose last good backup was eleven days ago: the agent is there, it runs, and "
        "nothing it has written since is worth restoring.",
        lambda w: _devices(w, "backup_behind") == 1,
    ),
    Scenario(
        "backup.possibly_retired",
        "Woodgrove Bank",
        "A device nothing has run on for two months and nobody has said so. Whether it was "
        "retired is a question rather than a fact, and the page has to let somebody ask it.",
        lambda w: _devices(w, "backup_possibly_retired") == 1,
    ),
    Scenario(
        "backup.completed_with_errors",
        "Fabrikam Inc",
        "A session that completed and could not read a handful of files: a success for the "
        "rate, and still something somebody looks at.",
        lambda w: _devices(w, "backup_completed_with_errors") == 1,
    ),
    Scenario(
        "backup.no_cove_device",
        "Fabrikam Inc",
        "Two servers at a customer who buys backup that Cove holds no device for: the coverage "
        "gap the Backup column is for.",
        lambda w: _devices(w, "no_backup_device") == 2,
    ),
)


def check(world: World) -> list[str]:
    """The scenarios the world does not hold. Empty when it holds every one."""
    return [f"{s.key} ({s.where})" for s in SCENARIOS if not s.present(world)]


def describe() -> str:
    """The scenario list as Markdown, for docs/demo-tenant.md."""
    lines = ["| Scenario | Where | What it shows |", "|---|---|---|"]
    for s in SCENARIOS:
        lines.append(f"| `{s.key}` | {s.where} | {s.shows} |")
    return "\n".join(lines)

"""Findings: what the facts add up to. Decided by rules here, only phrased by the model.

A rule reads facts and, if its condition holds, returns a `Finding` with a
severity, the ids of the facts it rests on, and code-written wording that is
used as it stands if no model is available. The model is given a finding to
phrase; it is never asked whether something is a problem.

**Measurements and gaps are different claims.** "Nine critical patches are
waiting on six devices" is a measurement: one tool counted it, and it is
asserted. "These machines have no antivirus" is a claim that something is
*absent*, and an agent that fails to see a product is not proof the product is
missing. A `gap` is asserted only when two tools support it; with one, it is
marked `needs_confirmation`, shown to the engineer with its evidence, and left
off the deck unless they confirm it (slide map, slide 14).

Comparisons with the last review use the previous period's stored facts and
nothing else. With no prior snapshot there are no improvements and no
regressions, and the sentences that would have stated them are not written.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from app.connectors.matching import normalise_hostname
from app.reports.tbr.config import FranchiseeConfig
from app.reports.tbr.model import (
    SEVERITY_ORDER,
    Fact,
    FactSet,
    Finding,
    ReviewPeriod,
    Severity,
    Source,
)
from app.reports.tbr.security import (
    GROUP_BD_CORE_OFF,
    GROUP_BD_ENDPOINTS,
    GROUP_BD_MODULE_OFF,
    GROUP_BD_MODULE_ON,
    GROUP_BD_MODULE_UNLICENSED,
    GROUP_BD_NO_AGENT,
    GROUP_BITLOCKER_ENCRYPTED,
    GROUP_BITLOCKER_UNENCRYPTED,
    GROUP_COVERAGE_MATCHED,
    GROUP_COVERAGE_NINJA_ONLY,
    GROUP_NINJA_PATCHING,
    PATCH_COVERAGE_DAYS,
)
from app.reports.tbr.vulnerabilities import (
    GROUP_CS_AWAITING_INSTALL,
    GROUP_CS_KNOWN_EXPLOITED,
    GROUP_CS_UNINSTALL,
)

#: Facts worth comparing between reviews: id -> True when a lower value is better.
COMPARABLE: dict[str, bool] = {
    "ninjaone.patches.failed": True,
    "ninjaone.patches.critical_pending": True,
    "ninjaone.antivirus.devices_unprotected": True,
    "ninjaone.disks.low": True,
    "ninjaone.windows11.ineligible": True,
    "ninjaone.windows11.eligible": True,
    "autotask.tickets.open": True,
    "autotask.tickets.password": True,
    "autotask.sla.resolution_met_percent": False,
    "autotask.sla.first_response_met_percent": False,
    "ninjaone.patches.success_percent": False,
    "bitdefender.endpoints.protected_percent": False,
    "bitdefender.company.risk_score": True,
    "coverage.ninjaone_only": True,
    "bitdefender.incidents.reported": True,
    # Vulnerabilities are state: the stored facts are the monthly snapshot the
    # next review's trend compares against.
    "connectsecure.vulnerabilities.total": True,
    "connectsecure.vulnerabilities.critical": True,
    "connectsecure.vulnerabilities.known_exploited": True,
    "connectsecure.vulnerabilities.critical_assets": True,
    "connectsecure.company.risk_score": True,
    "coverage.scan.uncovered": True,
}

#: The tools a finding may rest on. Two of them make a gap corroborated.
TOOLS: tuple[str, ...] = ("ninjaone", "autotask", "bitdefender", "connectsecure")

#: A change smaller than this, relative to the earlier value, is noise.
MEANINGFUL_CHANGE = 0.10


def quarter_for(severity: Severity, period: ReviewPeriod) -> str:
    """The quarter a recommendation is proposed for: sooner the more serious it is."""
    offset = {"critical": 1, "high": 1, "medium": 2, "low": 3}[severity]
    last = period.end - dt.timedelta(days=1)  # the period's final day
    # Counted from the quarter the period ends in: a review of August proposes
    # its urgent work for Q4, not for the few weeks of Q3 that are left.
    index = (last.year * 4 + (last.month - 1) // 3) + offset
    return f"Q{index % 4 + 1} {index // 4}"


class Rules:
    def __init__(
        self,
        facts: FactSet,
        *,
        prior: FactSet | None,
        prior_label: str | None,
        config: FranchiseeConfig,
        period: ReviewPeriod,
    ) -> None:
        self.facts = facts
        self.prior = prior
        self.prior_label = prior_label
        self.config = config
        self.period = period
        self.found: list[Finding] = []
        self._display: dict[str, str] = {}

    # --- helpers ------------------------------------------------------------------

    def n(self, fact_id: str) -> float:
        return self.facts.number(fact_id) or 0.0

    def has(self, *fact_ids: str) -> bool:
        return all(self.facts.get(fact_id) is not None for fact_id in fact_ids)

    def group(self, key: str) -> set[str]:
        """A device-name group, by normalised hostname, so two tools' spellings meet."""
        found: set[str] = set()
        for name in self.facts.groups.get(key, []):
            if name:
                key_name = normalise_hostname(name)
                found.add(key_name)
                self._display.setdefault(key_name, name)  # the spelling a slide prints
        return found

    def display(self, names: list[str]) -> list[str]:
        """Normalised hostnames back to the spelling a tool reported, for a slide."""
        return [self._display.get(name, name) for name in names]

    def add(
        self,
        finding_id: str,
        *,
        section: str,
        kind: str,
        severity: Severity,
        headline: str,
        detail: str,
        fact_ids: list[str],
        recommendation: str | None = None,
        service_key: str | None = None,
        recommendation_type: str | None = None,
    ) -> None:
        present = [fact_id for fact_id in fact_ids if fact_id in self.facts.ids()]
        sources: list[Source] = sorted(
            {self.facts.facts[i].source for i in present if i in self.facts.facts}
            | {self.facts.tables[i].source for i in present if i in self.facts.tables}
        )
        tools = [source for source in sources if source in TOOLS]
        corroborated = len(tools) >= 2
        self.found.append(
            Finding(
                id=finding_id,
                section=section,
                kind=kind,  # type: ignore[arg-type]
                severity=severity,
                headline=headline,
                detail=detail,
                recommendation=recommendation,
                fact_ids=present,
                sources=sources,
                corroborated=corroborated,
                needs_confirmation=kind == "gap" and not corroborated,
                service_key=service_key,
                recommendation_type=recommendation_type,
            )
        )

    # --- rules ----------------------------------------------------------------------

    def run(self) -> list[Finding]:
        rules: list[Callable[[], None]] = [
            self.failed_patches,
            self.critical_patches,
            self.unprotected_devices,
            self.no_protection_agent,
            self.unpatched_by_either_tool,
            self.unencrypted_by_either_tool,
            self.edr_sensor_off,
            self.known_exploited_vulnerabilities,
            self.top_remediations,
            self.critical_vulnerabilities,
            self.software_to_remove,
            self.fix_awaiting_install,
            self.vulnerability_scan_coverage,
            self.windows10,
            self.low_memory,
            self.low_disks,
            self.passwords,
            self.sla,
            self.ticket_concentration,
            self.frequent_contact,
            self.automation_noise,
            self.overdue_projects,
            self.location_mismatch,
            self.changes_since_last_review,
        ]
        for rule in rules:
            rule()
        self.reconcile_antivirus_with_bitdefender()
        self.found.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.id))
        return self.found

    def failed_patches(self) -> None:
        if self.n("ninjaone.patches.failed") <= 0:
            return
        devices = self.n("ninjaone.patches.failed_devices")
        success = self.facts.number("ninjaone.patches.success_percent")
        serious = devices >= 5 or (success is not None and success < 90)
        self.add(
            "patch.failed_installs",
            section="cybersecurity",
            kind="measurement",
            severity="high" if serious else "medium",
            headline="Windows patch installations are failing",
            detail=(
                "{{ninjaone.patches.failed}} Windows patch installations failed on "
                "{{ninjaone.patches.failed_devices}} devices during the review period."
            ),
            recommendation=(
                "Investigate the failing devices, clear the cause, and re-run the installations "
                "so they return to the patch baseline."
            ),
            fact_ids=[
                "ninjaone.patches.failed",
                "ninjaone.patches.failed_devices",
                "ninjaone.patches.success_percent",
                "ninjaone.patches.installed",
            ],
            service_key="patch_remediation",
            recommendation_type="patch_remediation",
        )

    def critical_patches(self) -> None:
        if self.n("ninjaone.patches.critical_pending") <= 0:
            return
        devices = self.n("ninjaone.patches.critical_pending_devices")
        self.add(
            "patch.critical_pending",
            section="cybersecurity",
            kind="measurement",
            severity="critical" if devices >= 10 else "high",
            headline="Critical Windows patches are approved but not installed",
            detail=(
                "{{ninjaone.patches.critical_pending}} critical Windows patches are approved "
                "and still waiting to install across "
                "{{ninjaone.patches.critical_pending_devices}} devices."
            ),
            recommendation=(
                "Schedule a maintenance window to install the outstanding critical patches and "
                "confirm each device reboots."
            ),
            fact_ids=[
                "ninjaone.patches.critical_pending",
                "ninjaone.patches.critical_pending_devices",
            ],
            service_key="patch_remediation",
            recommendation_type="patch_remediation",
        )

    def unprotected_devices(self) -> None:
        if self.n("ninjaone.antivirus.devices_unprotected") <= 0:
            return
        self.add(
            "antivirus.unprotected",
            section="cybersecurity",
            kind="gap",  # an absence: one agent not seeing a product does not prove it is missing
            severity="high",
            headline="Some devices show no healthy antivirus product",
            detail=(
                "{{ninjaone.antivirus.devices_unprotected}} of "
                "{{ninjaone.antivirus.devices_reporting}} devices report no antivirus product "
                "that is both switched on and up to date."
            ),
            recommendation=(
                "Confirm endpoint protection is installed, enabled and updating on each of "
                "these devices."
            ),
            fact_ids=[
                "ninjaone.antivirus.devices_unprotected",
                "ninjaone.antivirus.devices_reporting",
                "ninjaone.antivirus.unprotected.devices",
            ],
            service_key="cybersecurity_essentials",
        )

    # --- cross-tool security rules: each needs evidence from two tools -----------------

    def no_protection_agent(self) -> None:
        """A NinjaOne computer with no Bitdefender endpoint of the same hostname."""
        if not self.has("coverage.ninjaone_only", "bitdefender.endpoints.total"):
            return
        if self.n("coverage.ninjaone_only") <= 0:
            return
        self.add(
            "security.no_protection_agent",
            section="cybersecurity",
            kind="gap",
            severity="high" if self.n("coverage.ninjaone_only") >= 5 else "medium",
            headline="Some computers have no endpoint protection agent",
            detail=(
                "{{coverage.ninjaone_only}} of {{ninjaone.devices.total}} computers that "
                "NinjaOne manages have no Bitdefender endpoint, so no protection agent is "
                "reporting on them."
            ),
            recommendation=(
                "Deploy the Bitdefender agent to each of these devices, or retire the ones "
                "that should no longer be managed."
            ),
            # Both tools are in the evidence: the derived count rests on NinjaOne's
            # inventory and Bitdefender's endpoint list, and both facts are cited.
            fact_ids=[
                "coverage.ninjaone_only",
                "coverage.ninjaone_only.devices",
                "ninjaone.devices.total",
                "bitdefender.endpoints.total",
            ],
            service_key="cybersecurity_essentials",
        )

    def _module_rule(
        self,
        *,
        module: str,
        state: str,
        devices: list[str],
        finding_id: str,
        headline: str,
        detail: str,
        recommendation: str,
        service_key: str,
        fact_ids: list[str],
        severity: Severity,
        held: bool = False,
        recommendation_type: str | None = None,
    ) -> None:
        """One finding for one module state, worded for that state and nowhere else.

        `disabled` (licensed, off by policy) is a Cybersecurity finding. `not_licensed`
        is a Recommended Services finding: the customer cannot switch it on, only buy
        it. Both rest on the same cross-tool evidence and are never merged.
        """
        if not devices:
            return
        label = "disabled" if state == "disabled" else "not licensed"
        why = (
            "(licensed for the company, switched off by policy)"
            if state == "disabled"
            else "(the company's GravityZone licence does not include it)"
        )
        count_id = f"{finding_id}.{state}"
        self._derived_count(
            count_id,
            f"Devices where the Bitdefender {module} module is {label} and nothing else "
            "covers them",
            len(devices),
            f"Devices with a Bitdefender agent whose {module} module is {label} {why}, {detail}",
            names=devices,
        )
        self.add(
            count_id,
            section="cybersecurity" if state == "disabled" else "services",
            kind="gap",
            severity=severity,
            headline=f"{headline} ({label})",
            detail=(
                f"{{{{{count_id}}}}} devices have Bitdefender's {module} module {label}, {detail}"
            ),
            recommendation=recommendation,
            fact_ids=[count_id, f"{count_id}.devices", *fact_ids],
            service_key=service_key,
            recommendation_type=recommendation_type,
        )
        if held:
            self.found[-1].needs_confirmation = True

    def unpatched_by_either_tool(self) -> None:
        """Bitdefender patch management off or unlicensed AND NinjaOne is not patching the device.

        A missing Bitdefender patch module is not a gap on its own: most
        franchisees patch through NinjaOne. It is a gap only on a device neither
        tool patches. Disabled is a Cybersecurity finding; not licensed is a
        Recommended Services one.
        """
        if GROUP_NINJA_PATCHING not in self.facts.groups:
            return
        ninja_patched = self.group(GROUP_NINJA_PATCHING)
        for state, group in (
            ("disabled", GROUP_BD_MODULE_OFF),
            ("not_licensed", GROUP_BD_MODULE_UNLICENSED),
        ):
            module_off = self.group(group.format(module="patchManagement"))
            neither = sorted(module_off - ninja_patched)
            self._module_rule(
                module="patchManagement",
                state=state,
                devices=neither,
                finding_id="security.unpatched_devices",
                headline="Some devices are not being patched by either tool",
                detail=f"and NinjaOne recorded no patch installation for them in the last "
                f"{PATCH_COVERAGE_DAYS} days and has none waiting on its patch list.",
                recommendation=(
                    "Bring these devices under the NinjaOne patch policy"
                    + (
                        ", or enable Bitdefender Patch Management for them."
                        if state == "disabled"
                        else ", or add Bitdefender Patch Management to the customer's licence."
                    )
                ),
                service_key="patch_remediation",
                recommendation_type="patch_remediation",
                fact_ids=[
                    f"bitdefender.modules.patchManagement.{state}",
                    "ninjaone.patching.devices_managed",
                ],
                severity="high" if len(neither) >= 5 else "medium",
            )

    def unencrypted_by_either_tool(self) -> None:
        """Neither BitLocker nor Bitdefender encryption protects the device.

        Judged only on devices both tools know: NinjaOne says whether BitLocker
        has every local disk encrypted, Bitdefender says whether its encryption
        module is on, off, or not in the licence. A device one tool cannot see
        is not asserted either way. Disabled is a Cybersecurity finding; not
        licensed is a Recommended Services one, for the devices BitLocker
        leaves unencrypted.
        """
        if not self.has("ninjaone.bitlocker.devices_reporting", "bitdefender.endpoints.total"):
            return
        both = self.group(GROUP_COVERAGE_MATCHED)
        bitlocker_on = self.group(GROUP_BITLOCKER_ENCRYPTED)
        bitlocker_off = self.group(GROUP_BITLOCKER_UNENCRYPTED)
        bd_on = self.group(GROUP_BD_MODULE_ON.format(module="encryption"))
        for state, group in (
            ("disabled", GROUP_BD_MODULE_OFF),
            ("not_licensed", GROUP_BD_MODULE_UNLICENSED),
        ):
            bd_state = self.group(group.format(module="encryption"))
            unprotected = sorted((both & bitlocker_off & bd_state) - bitlocker_on - bd_on)
            self._module_rule(
                module="encryption",
                state=state,
                devices=unprotected,
                finding_id="security.unencrypted_devices",
                headline="Some devices have no disk encryption",
                detail="and NinjaOne reports a local disk BitLocker has not encrypted.",
                recommendation=(
                    "Enable BitLocker through the NinjaOne policy"
                    + (
                        ", or Bitdefender Full Disk Encryption, on each of these devices, and "
                        "escrow the recovery keys."
                        if state == "disabled"
                        else ", or add Bitdefender Full Disk Encryption to the customer's "
                        "licence, for each of these devices, and escrow the recovery keys."
                    )
                ),
                service_key=(
                    "cybersecurity_essentials" if state == "disabled" else "full_disk_encryption"
                ),
                fact_ids=[
                    "ninjaone.bitlocker.devices_unencrypted",
                    f"bitdefender.modules.encryption.{state}",
                ],
                severity="high",
            )

    def edr_sensor_off(self) -> None:
        """The EDR sensor is disabled or unlicensed on devices with an agent: held.

        It may be off by design (a licence without EDR, a server excluded on
        purpose), and until Blackpoint is connected no second tool can say. So
        both states are held findings, whatever else corroborates them:
        disabled under Cybersecurity, not licensed under Recommended Services.
        """
        for state, group in (
            ("disabled", GROUP_BD_MODULE_OFF),
            ("not_licensed", GROUP_BD_MODULE_UNLICENSED),
        ):
            off = sorted(self.group(group.format(module="edrSensor")))
            self._module_rule(
                module="edrSensor",
                state=state,
                devices=off,
                finding_id="security.edr_sensor_off",
                headline="The EDR sensor is off on some endpoints",
                detail="so no incident telemetry is collected from them.",
                recommendation=(
                    "Confirm whether EDR is intended for these endpoints; if it is, enable the "
                    "sensor in their policy."
                    if state == "disabled"
                    else "Confirm whether the customer should have EDR; if so, propose adding "
                    "it to the licence, or Blackpoint's managed detection."
                ),
                service_key="mdr",
                fact_ids=[f"bitdefender.modules.edrSensor.{state}"],
                severity="medium",
                held=True,
            )

    # --- vulnerabilities (ConnectSecure) ------------------------------------------

    def known_exploited_vulnerabilities(self) -> None:
        """Findings on CISA's Known Exploited Vulnerabilities list: asserted, critical.

        A measurement from one tool, ranked above everything else on slide 14:
        these are being exploited in the wild now. Findings whose fix is already
        a NinjaOne patch awaiting install are reported separately, not here.
        """
        if self.n("connectsecure.vulnerabilities.known_exploited") <= 0:
            return
        waiting = self.group(GROUP_CS_AWAITING_INSTALL)
        devices = sorted(self.group(GROUP_CS_KNOWN_EXPLOITED) - waiting)
        if not devices:
            return
        self._derived_count(
            "security.vulnerabilities.known_exploited_devices",
            "Devices with a known-exploited vulnerability and no fix under way",
            len(devices),
            "Devices carrying a CISA known-exploited vulnerability whose fix is not already an "
            "approved or manual NinjaOne patch waiting on that device.",
            names=devices,
        )
        self.add(
            "security.vulnerabilities.known_exploited",
            section="cybersecurity",
            kind="measurement",
            severity="critical",
            headline="Vulnerabilities attackers are exploiting now are present",
            detail=(
                "{{connectsecure.vulnerabilities.known_exploited_unique}} vulnerabilities on "
                "CISA's Known Exploited Vulnerabilities list are present on "
                "{{security.vulnerabilities.known_exploited_devices}} devices, as of the scan "
                "on {{connectsecure.scan.latest}}."
            ),
            recommendation=(
                "Apply the fixes for these first, ahead of everything else on the list; they "
                "are the ones with evidence of active exploitation."
            ),
            fact_ids=[
                "connectsecure.vulnerabilities.known_exploited",
                "connectsecure.vulnerabilities.known_exploited_unique",
                "security.vulnerabilities.known_exploited_devices",
                "security.vulnerabilities.known_exploited_devices.devices",
                "connectsecure.vulnerabilities.known_exploited.list",
                "connectsecure.scan.latest",
            ],
            service_key="vulnerability_management",
            recommendation_type="patch_remediation",
        )

    def top_remediations(self) -> None:
        """A few fixes resolve most critical findings: the actionable headline."""
        if not self.has(
            "connectsecure.remediations.top_critical_percent",
            "connectsecure.remediations.top_count",
        ):
            return
        if self.n("connectsecure.vulnerabilities.critical_open") <= 0:
            return
        share = self.n("connectsecure.remediations.top_critical_percent")
        # The share ranks the finding; it never reaches the sentence. Its
        # denominator is the critical findings the recommended updates address,
        # which is not a number on the deck, so "100%" read as all of them.
        self.add(
            "security.vulnerabilities.top_fixes",
            section="cybersecurity",
            kind="measurement",
            severity="high" if share >= 50 else "medium",
            headline="A few updates would resolve most of the critical findings",
            # "118 of the 118" is true and reads like a mistake, so the whole
            # case says "all" instead. Both forms print the total either way.
            detail=(
                "{{connectsecure.remediations.top_count}} updates resolve "
                + (
                    "all {{connectsecure.vulnerabilities.critical_open}} open critical findings"
                    if self.n("connectsecure.remediations.top_critical_resolved")
                    >= self.n("connectsecure.vulnerabilities.critical_open")
                    else "{{connectsecure.remediations.top_critical_resolved}} of the "
                    "{{connectsecure.vulnerabilities.critical_open}} open critical findings"
                )
                + ", across {{connectsecure.vulnerabilities.critical_assets}} devices."
            ),
            recommendation=(
                "Schedule those updates first: they close the most critical findings for the "
                "least work. The full ranking is in the fixes table."
            ),
            fact_ids=[
                "connectsecure.remediations.top_count",
                "connectsecure.remediations.top_critical_resolved",
                "connectsecure.remediations.top",
                "connectsecure.vulnerabilities.critical_assets",
                "connectsecure.vulnerabilities.critical_open",
            ],
            service_key="vulnerability_management",
            recommendation_type="patch_remediation",
        )

    def critical_vulnerabilities(self) -> None:
        """Critical findings with no fix under way: a measurement, ranked by devices."""
        if not self.has("connectsecure.vulnerabilities.critical_open"):
            return
        open_count = self.n("connectsecure.vulnerabilities.critical_open")
        if open_count <= 0:
            return
        devices = self.n("connectsecure.vulnerabilities.critical_assets")
        self.add(
            "security.vulnerabilities.critical",
            section="cybersecurity",
            kind="measurement",
            severity="high" if devices >= 5 or open_count >= 10 else "medium",
            headline="Critical vulnerabilities are open on managed devices",
            detail=(
                "{{connectsecure.vulnerabilities.critical_open}} critical findings are open on "
                "{{connectsecure.vulnerabilities.critical_assets}} devices, as of the scan on "
                "{{connectsecure.scan.latest}}, with no update queued in NinjaOne."
            ),
            recommendation=(
                "Work through the fixes in the order the fixes table ranks them, and rescan to "
                "confirm each closes."
            ),
            fact_ids=[
                "connectsecure.vulnerabilities.critical_open",
                "connectsecure.vulnerabilities.critical",
                "connectsecure.vulnerabilities.unique",
                "connectsecure.vulnerabilities.critical_assets",
                "connectsecure.scan.latest",
            ],
            service_key="vulnerability_management",
            recommendation_type="patch_remediation",
        )

    def software_to_remove(self) -> None:
        """Products ConnectSecure says to uninstall: a different kind of work from an update."""
        if self.n("connectsecure.remediations.uninstall") <= 0:
            return
        devices = sorted(self.group(GROUP_CS_UNINSTALL))
        self.add(
            "security.vulnerabilities.software_to_remove",
            section="cybersecurity",
            kind="measurement",
            severity="medium" if len(devices) >= 5 else "low",
            headline="Some software should be removed rather than updated",
            detail=(
                "ConnectSecure recommends removing "
                "{{connectsecure.remediations.uninstall}} product(s) outright: they are out of "
                "support or should not be installed, so no update closes their findings."
            ),
            recommendation=(
                "Confirm nobody needs these products, then uninstall them; if one is needed, "
                "record why so the finding can be suppressed at the scanner."
            ),
            fact_ids=[
                "connectsecure.remediations.uninstall",
                "connectsecure.remediations.uninstall.list",
            ],
            service_key="vulnerability_management",
        )

    def fix_awaiting_install(self) -> None:
        """Fixes NinjaOne already has queued: reported as such, not as a gap."""
        if self.n("connectsecure.remediations.awaiting_install") <= 0:
            return
        self.add(
            "security.vulnerabilities.fix_awaiting_install",
            section="cybersecurity",
            kind="measurement",
            severity="low",
            headline="Some fixes are already queued for installation",
            detail=(
                "{{connectsecure.remediations.awaiting_install}} of the updates ConnectSecure "
                "recommends are already approved in NinjaOne for the same device and simply "
                "not installed yet: work under way, not a new gap."
            ),
            recommendation="Let the next patch window run, then rescan to confirm they close.",
            # The match rate beside it is internal: it says how well the two tools
            # reconcile, which is an engineering question, not a customer one.
            fact_ids=[
                "connectsecure.remediations.awaiting_install",
                "ninjaone.software_patches.queued_devices",
            ],
        )

    def vulnerability_scan_coverage(self) -> None:
        """NinjaOne computers the scanner does not cover: no asset, or not scanned in 30 days.

        A gap with two tools behind it: NinjaOne's inventory and ConnectSecure's
        asset list and scan dates, joined by hostname within the site.
        """
        if not self.has("coverage.scan.uncovered", "connectsecure.assets.total"):
            return
        uncovered = self.n("coverage.scan.uncovered")
        if uncovered <= 0:
            return
        self.add(
            "security.vulnerability_scan_coverage",
            section="cybersecurity",
            kind="gap",
            severity="high" if uncovered >= 5 else "medium",
            headline="Some managed computers are not being vulnerability-scanned",
            detail=(
                "{{coverage.scan.uncovered}} of {{ninjaone.devices.total}} computers NinjaOne "
                "manages have no ConnectSecure asset or have not been scanned in the last "
                "thirty days, so their vulnerabilities are unknown."
            ),
            recommendation=(
                "Deploy the ConnectSecure agent to the computers it has never seen, and check "
                "the agents that have stopped reporting."
            ),
            fact_ids=[
                "coverage.scan.uncovered",
                "coverage.scan.uncovered.devices",
                "coverage.scan.ninjaone_only",
                "ninjaone.devices.total",
                "connectsecure.assets.total",
                "connectsecure.assets.stale",
            ],
            service_key="vulnerability_management",
        )

    def reconcile_antivirus_with_bitdefender(self) -> None:
        """Confirm or dismiss NinjaOne's held antivirus finding from Bitdefender's view.

        NinjaOne alone cannot prove a product is missing. Bitdefender can: an
        endpoint with no agent, or with core protection off, confirms the gap;
        every named device protected in Bitdefender dismisses it. Devices
        Bitdefender does not know leave the finding held for the engineer.
        """
        finding = next((f for f in self.found if f.id == "antivirus.unprotected"), None)
        if finding is None or GROUP_BD_ENDPOINTS not in self.facts.groups:
            return
        table = self.facts.tables.get("ninjaone.antivirus.unprotected.devices")
        if table is None:
            return
        named = {normalise_hostname(row[0]) for row in table.rows if row}
        known = self.group(GROUP_BD_ENDPOINTS)
        unprotected_in_bd = self.group(GROUP_BD_NO_AGENT) | self.group(GROUP_BD_CORE_OFF)
        # A doubted device with no Bitdefender endpoint at all (the hostname join found
        # none) is the second tool agreeing: nothing of Bitdefender's is on it.
        absent_from_bd = named & self.group(GROUP_COVERAGE_NINJA_ONLY)
        if (named & unprotected_in_bd) or absent_from_bd:
            finding.corroborated = True
            finding.confirmed_by = "bitdefender"
            finding.sources = sorted({*finding.sources, "bitdefender"})
            finding.fact_ids = [
                *finding.fact_ids,
                "bitdefender.endpoints.no_agent",
                "bitdefender.endpoints.core_off",
            ]
            if absent_from_bd:
                # Its own fact: the site-wide NinjaOne-only count is not the count of
                # doubted devices, and a sentence must not cite one for the other.
                self._derived_count(
                    "antivirus.unprotected.no_endpoint",
                    "Devices NinjaOne doubts that have no Bitdefender endpoint",
                    len(absent_from_bd),
                    "Of the devices NinjaOne reports without a healthy antivirus product, those "
                    "the hostname join found no Bitdefender endpoint for at all.",
                    names=sorted(absent_from_bd),
                )
                finding.fact_ids.append("antivirus.unprotected.no_endpoint")
                finding.detail += (
                    " Bitdefender confirms it: {{antivirus.unprotected.no_endpoint}} of them "
                    "have no Bitdefender endpoint at all."
                )
            else:
                finding.detail += (
                    " Bitdefender confirms it: at least one of them has no working "
                    "Bitdefender agent."
                )
        elif named and named <= known:
            # Every device NinjaOne doubts is protected in Bitdefender: the doubt is dismissed.
            self.found.remove(finding)
            self.add(
                "internal.antivirus_dismissed",
                section="internal",
                kind="pattern",
                severity="low",
                headline="NinjaOne's antivirus doubt was dismissed by Bitdefender",
                detail=(
                    "NinjaOne reported {{ninjaone.antivirus.devices_unprotected}} devices with no "
                    "healthy antivirus product; every one of them has a current Bitdefender "
                    "agent with core protection on."
                ),
                recommendation="Check why NinjaOne's antivirus report disagrees with the agent.",
                fact_ids=[
                    "ninjaone.antivirus.devices_unprotected",
                    "bitdefender.endpoints.with_agent",
                ],
            )

    def _derived_count(
        self, fact_id: str, label: str, value: int, definition: str, *, names: list[str]
    ) -> None:
        """A cross-tool count the rule computed, recorded as a fact so the sentence can cite it."""
        as_of = next(iter(self.facts.facts.values())).as_of
        self.facts.add(
            Fact(
                id=fact_id,
                label=label,
                value=value,
                display=f"{value:,}",
                definition=definition,
                source="derived",
                as_of=as_of,
            )
        )
        from app.reports.tbr.model import FactTable

        self.facts.tables[f"{fact_id}.devices"] = FactTable(
            id=f"{fact_id}.devices",
            label=label,
            definition=f"See {fact_id}.",
            source="derived",
            columns=["Device"],
            rows=[[name] for name in self.display(names)],
            total=len(names),
            as_of=as_of,
        )

    # --- hardware: which of these is a service, and which is a job --------------------
    #
    # Hardware Lifecycle Planning is the service for devices that are old or out
    # of support, and the only finding that says so today is a Windows 10 device
    # that cannot run Windows 11. Device age is not collected yet, so nothing
    # else can claim it. A full disk or a short memory module is a job for the
    # engineer with a line in the price book, and no service in the catalogue
    # sells storage or maintenance, so those findings back no service at all:
    # they stay on slide 13 and off slide 18, rather than pointing at the
    # nearest name.

    def windows10(self) -> None:
        if self.n("ninjaone.windows11.ineligible") > 0:
            self.add(
                "hardware.windows10_replace",
                section="replacement",
                kind="measurement",
                severity="high",
                headline="Windows 10 devices that cannot move to Windows 11",
                detail=(
                    "{{ninjaone.windows11.ineligible}} devices are still on Windows 10 and do "
                    "not meet the processor or memory requirement for Windows 11."
                ),
                recommendation="Plan replacement of these devices.",
                fact_ids=[
                    "ninjaone.windows11.ineligible",
                    "ninjaone.windows11.ineligible.devices",
                ],
                service_key="hardware_lifecycle",
                recommendation_type="device_replacement",
            )
        if self.n("ninjaone.windows11.eligible") > 0:
            self.add(
                "hardware.windows10_upgrade",
                section="upgrade",
                kind="measurement",
                severity="medium",
                headline="Windows 10 devices ready for Windows 11",
                detail=(
                    "{{ninjaone.windows11.eligible}} devices are on Windows 10 and meet the "
                    "processor and memory requirement for Windows 11."
                ),
                recommendation="Schedule an in-place upgrade to Windows 11.",
                fact_ids=["ninjaone.windows11.eligible", "ninjaone.windows11.eligible.devices"],
                # An in-place upgrade is work on slide 13 with a price-book line, not
                # a service: the hardware is staying.
                recommendation_type="windows11_upgrade",
            )

    def low_memory(self) -> None:
        if self.n("ninjaone.hardware.low_memory") <= 0:
            return
        self.add(
            "hardware.low_memory",
            section="upgrade",
            kind="measurement",
            severity="low",
            headline="Workstations short of memory",
            detail=(
                "{{ninjaone.hardware.low_memory}} workstations have less memory installed than "
                "the recommended minimum."
            ),
            recommendation="Add memory where the model allows it, otherwise plan replacement.",
            fact_ids=["ninjaone.hardware.low_memory", "ninjaone.hardware.low_memory.devices"],
            recommendation_type="memory_upgrade",
        )

    def low_disks(self) -> None:
        if self.n("ninjaone.disks.low") <= 0:
            return
        self.add(
            "hardware.low_disk",
            section="upgrade",
            kind="measurement",
            severity="medium",
            headline="Disks running out of space",
            detail="{{ninjaone.disks.low}} fixed disks are below the free-space threshold.",
            recommendation="Clear space or fit a larger drive before the disks fill.",
            fact_ids=["ninjaone.disks.low", "ninjaone.disks.low.volumes"],
            recommendation_type="storage_upgrade",
        )

    def passwords(self) -> None:
        count = self.n("autotask.tickets.password")
        people = self.n("autotask.tickets.from_people")
        if count < 5 or not people or count / people < 0.10:
            return
        self.add(
            "tickets.passwords",
            section="dashboard",
            kind="pattern",
            severity="medium",
            headline="Password and lockout requests are a steady share of the helpdesk load",
            detail=(
                "{{autotask.tickets.password}} of the {{autotask.tickets.from_people}} tickets "
                "people raised in the period were password resets or account lockouts."
            ),
            recommendation=(
                "A business password manager with self-service reset would remove most of "
                "these requests."
            ),
            fact_ids=["autotask.tickets.password", "autotask.tickets.from_people"],
            service_key="password_manager",
            recommendation_type="password_manager",
        )

    def sla(self) -> None:
        target = self.config.sla_target_percent
        for fact_id, finding_id, what in (
            ("autotask.sla.resolution_met_percent", "sla.resolution", "resolution"),
            ("autotask.sla.first_response_met_percent", "sla.first_response", "first-response"),
        ):
            value = self.facts.number(fact_id)
            if value is None or value >= target:
                continue
            measured = fact_id.replace("_met_percent", "_measured")
            self.add(
                finding_id,
                section="dashboard",
                kind="measurement",
                severity="high" if value < target - 15 else "medium",
                headline=f"The {what} service level was below target",
                detail=(
                    f"The {what} service level was met on {{{{{fact_id}}}}} of "
                    f"{{{{{measured}}}}} measured tickets, below the target."
                ),
                recommendation="Review the tickets that missed and agree what would prevent it.",
                fact_ids=[fact_id, measured],
            )

    def ticket_concentration(self) -> None:
        share = self.facts.number("autotask.tickets.top_issue_type_share")
        if share is None or share < 40 or self.n("autotask.tickets.created") < 10:
            return
        self.add(
            "tickets.concentration",
            section="dashboard",
            kind="pattern",
            severity="low",
            headline="One kind of request dominates the helpdesk",
            detail=(
                "{{autotask.tickets.top_issue_type}} accounted for "
                "{{autotask.tickets.top_issue_type_share}} of the "
                "{{autotask.tickets.created}} tickets created in the period."
            ),
            recommendation="Look for a common cause that a change or training would remove.",
            fact_ids=[
                "autotask.tickets.top_issue_type",
                "autotask.tickets.top_issue_type_share",
                "autotask.tickets.created",
                "autotask.chart.issue_type",
            ],
        )

    def frequent_contact(self) -> None:
        share = self.facts.number("autotask.tickets.top_contact_share")
        if share is None or share < 30 or self.n("autotask.tickets.from_people") < 10:
            return
        self.add(
            "tickets.frequent_contact",
            section="dashboard",
            kind="pattern",
            severity="low",
            headline="One person raises a large share of the requests",
            detail=(
                "A single contact raised {{autotask.tickets.top_contact_share}} of the tickets "
                "people opened in the period."
            ),
            recommendation=(
                "Check whether it points to a device, application or training need for that person."
            ),
            fact_ids=["autotask.tickets.top_contact_share", "autotask.chart.top_contacts"],
        )

    def automation_noise(self) -> None:
        automated, created = (
            self.n("autotask.tickets.automated"),
            self.n("autotask.tickets.created"),
        )
        if created < 10 or automated / created < 0.5:
            return
        self.add(
            "tickets.automation",
            section="dashboard",
            kind="pattern",
            severity="low",
            headline="Most tickets are raised by monitoring, not by people",
            detail=(
                "{{autotask.tickets.automated}} of the {{autotask.tickets.created}} tickets "
                "created in the period came from monitoring or automation."
            ),
            recommendation="Tune alert thresholds so the queue reflects work that needs a person.",
            fact_ids=[
                "autotask.tickets.automated",
                "autotask.tickets.created",
                "autotask.chart.top_contacts",
            ],
        )

    def overdue_projects(self) -> None:
        if self.n("autotask.projects.overdue") <= 0:
            return
        self.add(
            "projects.overdue",
            section="projects",
            kind="measurement",
            severity="medium",
            headline="Projects running past their planned end date",
            detail=(
                "The project in progress is past its planned end date."
                if self.n("autotask.projects.current") == 1
                else "{{autotask.projects.overdue}} of the {{autotask.projects.current}} "
                "projects in progress are past their planned end date."
            ),
            recommendation="Agree a revised date for each and what is holding it.",
            fact_ids=["autotask.projects.overdue", "autotask.projects.current"],
        )

    def location_mismatch(self) -> None:
        if not self.has("ninjaone.locations", "autotask.locations"):
            return
        if self.n("ninjaone.locations") == self.n("autotask.locations"):
            return
        self.add(
            "internal.location_mismatch",
            section="internal",  # housekeeping for the engineer; never rendered
            kind="pattern",
            severity="low",
            headline="NinjaOne and Autotask disagree on the number of locations",
            detail=(
                "NinjaOne lists {{ninjaone.locations}} locations and Autotask lists "
                "{{autotask.locations}}."
            ),
            recommendation="Reconcile the two so the review states the right figure.",
            fact_ids=["ninjaone.locations", "autotask.locations"],
        )

    def changes_since_last_review(self) -> None:
        if self.prior is None:
            return
        for fact_id, lower_is_better in COMPARABLE.items():
            now, before = self.facts.get(fact_id), self.prior.get(fact_id)
            if now is None or before is None:
                continue
            current, earlier = self.facts.number(fact_id), self.prior.number(fact_id)
            if current is None or earlier is None or current == earlier:
                continue
            if earlier and abs(current - earlier) / abs(earlier) < MEANINGFUL_CHANGE:
                continue
            better = (current < earlier) == lower_is_better
            delta = self._delta_fact(now, before)
            self.add(
                f"change.{fact_id}",
                section="accomplishments" if better else _section_of(fact_id),
                kind="improvement" if better else "regression",
                severity="low" if better else "medium",
                headline=f"{now.label}: {'improved' if better else 'worse'} since the last review",
                detail=f"{now.label} went {{{{{delta.id}}}}}.",
                fact_ids=[delta.id, fact_id],
            )

    def _delta_fact(self, now: Fact, before: Fact) -> Fact:
        """The comparison as a fact of its own, so the sentence about it cites one."""
        return self.facts.add(
            Fact(
                id=f"delta.{now.id}",
                label=f"{now.label}: change since {self.prior_label or 'the last review'}",
                value=f"{before.display} -> {now.display}",
                display=f"from {before.display} to {now.display}",
                definition=(
                    f"{now.id} in {self.prior_label or 'the previous review'} against this "
                    "review. Both values come from stored facts, not from memory."
                ),
                source="derived",
                as_of=now.as_of,
                period_bound=now.period_bound,
            )
        )


def _section_of(fact_id: str) -> str:
    if fact_id.startswith("autotask."):
        return "dashboard"
    if "windows11" in fact_id or "disks" in fact_id:
        return "upgrade"
    return "cybersecurity"


def derive_findings(
    facts: FactSet,
    *,
    prior: FactSet | None,
    prior_label: str | None,
    config: FranchiseeConfig,
    period: ReviewPeriod,
) -> list[Finding]:
    return Rules(facts, prior=prior, prior_label=prior_label, config=config, period=period).run()


def overall_opinion(findings: list[Finding], *, now: dt.datetime | None = None) -> str:
    """The one-phrase verdict on slide 5, chosen by code from the findings' severity."""
    serious = [f for f in findings if f.assertable and f.section != "internal"]
    worst = min((SEVERITY_ORDER[f.severity] for f in serious), default=9)
    if worst == 0:
        return "effective overall, with items that need prompt attention"
    if worst == 1:
        return "effective, with a small number of items to address"
    if worst == 2:
        return "effective and stable, with minor items to tidy up"
    return "effective and stable"

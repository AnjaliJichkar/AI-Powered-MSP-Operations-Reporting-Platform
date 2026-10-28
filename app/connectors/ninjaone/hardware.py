"""What a machine is, from what NinjaOne reports about it.

Two classifications the hardware tool and the business-review report share:

- **physical or virtual**, from the `virtualMachine` flag NinjaOne sets and,
  when the flag is absent, from the node class and the system manufacturer and
  model a hypervisor presents;
- **Windows 11 hardware eligibility** for a machine still on Windows 10, from
  the processor model and installed memory.

What this cannot see, and says so: NinjaOne's API reports **no TPM, Secure Boot
or UEFI state** (the spec does not contain the words; checked 2026-09-21). An
"eligible" result therefore means "the processor generation and memory pass";
it is never a guarantee, and every result carries that caveat in `basis`.
Processor support follows Microsoft's published lists by generation — Intel
8th generation and later, AMD Zen+ and later — not model by model, so a CPU
this cannot read is `unknown`, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

MachineKind = Literal["physical", "virtual", "unknown"]
Windows11Status = Literal["already_windows_11", "eligible", "ineligible", "unknown", "not_windows"]

#: Node classes that are virtual by definition.
VIRTUAL_NODE_CLASSES = frozenset({"VMWARE_VM_GUEST", "HYPERV_VMM_GUEST"})

#: What a guest sees as its manufacturer or model under each hypervisor.
VIRTUAL_HINTS = (
    "vmware",
    "virtual machine",  # Hyper-V: manufacturer Microsoft Corporation, model "Virtual Machine"
    "virtualbox",
    "innotek",
    "qemu",
    "kvm",
    "xen",
    "parallels",
    "nutanix",
    "amazon ec2",
    "google compute engine",
    "openstack",
    "bochs",
)

#: Windows 11 needs 4 GB. Reported memory is what the OS can use, a little under
#: what is fitted, so the test is against 3.5 GiB.
MIN_MEMORY_BYTES = int(3.5 * 2**30)

BASIS = (
    "Judged from the processor generation and installed memory. NinjaOne does not report "
    "TPM, Secure Boot or UEFI state, so those are unverified."
)


def classify_machine(
    *,
    virtual_flag: bool | None,
    node_class: str | None,
    manufacturer: str | None,
    model: str | None,
) -> MachineKind:
    """Physical, virtual, or unknown when NinjaOne reported nothing to tell by."""
    if (node_class or "").upper() in VIRTUAL_NODE_CLASSES:
        return "virtual"
    described = f"{manufacturer or ''} {model or ''}".casefold()
    if any(hint in described for hint in VIRTUAL_HINTS):
        return "virtual"
    if virtual_flag is True:
        return "virtual"
    if virtual_flag is False:
        return "physical"
    return "physical" if described.strip() else "unknown"


_INTEL_GEN = re.compile(r"(\d{1,2})(?:st|nd|rd|th) gen", re.IGNORECASE)
_INTEL_CORE = re.compile(r"\bi[3579]-(\d{4,5})[a-z0-9]*", re.IGNORECASE)
_INTEL_CORE_SERIES = re.compile(r"core\(tm\) [3579] \d{3}[a-z]*", re.IGNORECASE)
_AMD_RYZEN = re.compile(r"ryzen(?: \w+)? (?:pro )?(\d)(\d{3})([a-z]*)", re.IGNORECASE)


def processor_supported(name: str | None) -> bool | None:
    """True or False when the generation is readable from the name, else None."""
    if not name:
        return None
    text = " ".join(name.split())
    lowered = text.casefold()
    if "core(tm) ultra" in lowered or "core ultra" in lowered or "ryzen ai" in lowered:
        return True
    if _INTEL_CORE_SERIES.search(text):
        return True
    generation = _INTEL_GEN.search(text)
    if generation:
        return int(generation.group(1)) >= 8
    core = _INTEL_CORE.search(text)
    if core:
        digits = core.group(1)
        return int(digits[:2] if len(digits) == 5 else digits[0]) >= 8
    ryzen = _AMD_RYZEN.search(text)
    if ryzen:
        series, suffix = int(ryzen.group(1)), ryzen.group(3).casefold()
        if series == 2 and suffix.startswith("g"):
            return False  # 2200G / 2400G are first-generation Zen despite the number
        return series >= 2
    if any(old in lowered for old in ("core(tm)2", "core 2 ", "amd fx", "amd a1", "amd a8")):
        return False
    return None  # Xeon, EPYC, Celeron, Pentium, ARM: needs the model list, not a guess


@dataclass(frozen=True, slots=True)
class Eligibility:
    status: Windows11Status
    reasons: list[str] = field(default_factory=list)
    basis: str = BASIS


def windows11_eligibility(
    *, os_name: str | None, processor: str | None, memory_bytes: int | None
) -> Eligibility:
    """Can this machine run Windows 11, as far as NinjaOne's data can say."""
    system = (os_name or "").casefold()
    if "windows" not in system:
        return Eligibility("not_windows" if system else "unknown", [], BASIS)
    if "server" in system:
        return Eligibility("not_windows", ["a Windows Server edition"], BASIS)
    if "windows 11" in system:
        return Eligibility("already_windows_11", [], BASIS)

    reasons: list[str] = []
    supported = processor_supported(processor)
    if supported is False:
        reasons.append(f"processor is older than Windows 11 supports ({processor})")
    enough = None if not memory_bytes else memory_bytes >= MIN_MEMORY_BYTES
    if enough is False:
        reasons.append("less than 4 GB of memory")
    if reasons:
        return Eligibility("ineligible", reasons, BASIS)
    if supported is None or enough is None:
        missing = [
            text
            for text, unknown in (
                ("processor generation could not be read", supported is None),
                ("installed memory was not reported", enough is None),
            )
            if unknown
        ]
        return Eligibility("unknown", missing, BASIS)
    return Eligibility("eligible", [], BASIS)


# --- estimated age, from the processor's launch year -------------------------------

#: The year each processor family shipped, by the generation its name carries.
#: Intel Core by generation number (8th generation launched in 2017), Core Ultra by
#: series, AMD Ryzen by the thousands digit of the model number, Intel Xeon
#: Scalable by the second digit of the model (41xx Skylake-SP 2017, 42xx Cascade
#: Lake 2019, 43xx Ice Lake 2021, 44xx Sapphire Rapids 2023) and Xeon E by its series
#: (21xx 2018, 22xx 2019, 23xx 2021). A machine's estimated age is the review
#: year less this year: the year the processor family was current, which is
#: when a machine built around it was bought, give or take a model year.
#: Anything the name does not place is left blank, never guessed.
INTEL_CORE_YEARS: dict[int, int] = {
    2: 2011, 3: 2012, 4: 2013, 5: 2014, 6: 2015, 7: 2016, 8: 2017, 9: 2018, 10: 2019,
    11: 2020, 12: 2021, 13: 2022, 14: 2023,
}  # fmt: skip
CORE_ULTRA_YEARS: dict[int, int] = {1: 2023, 2: 2024, 3: 2025}
RYZEN_YEARS: dict[int, int] = {
    1: 2017, 2: 2018, 3: 2019, 4: 2020, 5: 2021, 6: 2022, 7: 2023, 8: 2024, 9: 2024,
}  # fmt: skip
XEON_SCALABLE_YEARS: dict[int, int] = {1: 2017, 2: 2019, 3: 2021, 4: 2023, 5: 2023, 6: 2024}
XEON_E_YEARS: dict[int, int] = {21: 2018, 22: 2019, 23: 2021, 24: 2024}
AGE_METHOD = (
    "Estimated age is the review year less the launch year of the processor family "
    "NinjaOne reports (Intel Core by generation, Core Ultra by series, AMD Ryzen by "
    "model series, Intel Xeon Scalable and Xeon E by series); blank when the "
    "processor's generation cannot be read from its name. It is the age of the "
    "design, not a purchase date."
)

_CORE_ULTRA = re.compile(r"core\(tm\)? ?ultra [3579] (\d)\d{2}", re.IGNORECASE)
# Xeon Scalable model numbers: the tier digit, then the generation digit (4114:
# Silver, first generation; 4210: second; 4314: third; 4410: fourth).
_XEON_SCALABLE = re.compile(
    r"xeon\(r\)? (?:platinum|gold|silver|bronze) \d(\d)\d{2}", re.IGNORECASE
)
_XEON_E = re.compile(r"xeon\(r\)? e-(\d{2})\d{2}", re.IGNORECASE)


def processor_launch_year(name: str | None) -> int | None:
    """The year the processor family in `name` launched, or None when it cannot be read."""
    if not name:
        return None
    text = " ".join(name.split())
    ultra = _CORE_ULTRA.search(text)
    if ultra:
        return CORE_ULTRA_YEARS.get(int(ultra.group(1)))
    generation = _INTEL_GEN.search(text)
    if generation:
        return INTEL_CORE_YEARS.get(int(generation.group(1)))
    core = _INTEL_CORE.search(text)
    if core:
        digits = core.group(1)
        return INTEL_CORE_YEARS.get(int(digits[:2] if len(digits) == 5 else digits[0]))
    ryzen = _AMD_RYZEN.search(text)
    if ryzen:
        return RYZEN_YEARS.get(int(ryzen.group(1)))
    scalable = _XEON_SCALABLE.search(text)
    if scalable:
        return XEON_SCALABLE_YEARS.get(int(scalable.group(1)))
    xeon_e = _XEON_E.search(text)
    if xeon_e:
        return XEON_E_YEARS.get(int(xeon_e.group(1)))
    return None


def estimated_age_years(name: str | None, *, year: int) -> int | None:
    """`year` less the processor family's launch year; None when unknown or negative."""
    launched = processor_launch_year(name)
    if launched is None or launched > year:
        return None
    return year - launched


# --- workstation or server: the operating system decides where it can ------------------

DeviceRoleName = Literal["workstation", "server", "other"]

#: NinjaOne node classes for a workstation, and the markers of a server or VM guest.
WORKSTATION_CLASSES = frozenset(
    {"WINDOWS_WORKSTATION", "MAC", "LINUX_WORKSTATION", "MAC_WORKSTATION"}
)
SERVER_CLASS_MARKERS = ("SERVER", "VM_GUEST", "VMM_GUEST")


@dataclass(frozen=True, slots=True)
class DeviceRole:
    role: DeviceRoleName
    basis: str
    """How the role was decided, in words a page can print."""
    from_os: bool
    """True when the operating system decided; False when NinjaOne's node class did."""
    class_role: DeviceRoleName
    """What the node class alone would have said, for the disagreement list."""

    @property
    def disagrees(self) -> bool:
        return self.from_os and self.class_role != self.role


def role_of_class(node_class: str | None) -> DeviceRoleName:
    upper = (node_class or "").upper()
    if upper in WORKSTATION_CLASSES:
        return "workstation"
    if any(marker in upper for marker in SERVER_CLASS_MARKERS):
        return "server"
    return "other"


def device_role(os_name: str | None, node_class: str | None) -> DeviceRole:
    """Workstation or server, decided once for the deck's counts and the PDF's tables.

    Windows is a server only when the OS name says `Windows Server`; every other
    Windows edition is a workstation whatever NinjaOne's node class says (live: a
    Windows 10 IoT till classed `WINDOWS_SERVER`). macOS is a workstation. Linux,
    an unknown OS and anything else fall back to the node class, which is the
    only signal there.
    """
    by_class = role_of_class(node_class)
    lowered = " ".join((os_name or "").split()).casefold()
    if "windows" in lowered:
        role: DeviceRoleName = "server" if "windows server" in lowered else "workstation"
        return DeviceRole(role, f"operating system: {os_name}", True, by_class)
    if any(mark in lowered for mark in ("macos", "mac os", "os x")):
        return DeviceRole("workstation", f"operating system: {os_name}", True, by_class)
    shown = node_class or "not reported"
    return DeviceRole(by_class, f"NinjaOne node class: {shown}", False, by_class)

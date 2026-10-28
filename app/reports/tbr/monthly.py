"""The facts the Monthly Report PDF adds to a review, and the shape of the ones it waits for.

The PDF (`app/reports/tbr/pdf.py`) renders from the stored review's facts,
tables and charts and writes no sentence of its own. Most of what it prints
the deck's collectors already record — tickets by day, the SLA shares, who
raised tickets, issue types, OS patch installs. What only the PDF needs is
collected here, by the same collector, in the same run, audited the same way:

- **lifecycle tables**: one row per workstation and per server with what
  NinjaOne reports about the machine — who logged on last, make, model, OS,
  memory, processor, location, storage used, total and free with the free
  share banded, and an estimated age from the processor family's launch
  year (`hardware.processor_launch_year`; blank when it cannot be read, and
  the method footnoted);
- **third-party patch installs** for the period, from NinjaOne's
  `software-patch-installs` history (read live for the first time on
  2026-09-29: `id, productIdentifier, title, impact, status, type,
  installedAt, deviceId, timestamp`; status `INSTALLED` or `FAILED`), the
  same shape as the OS history's facts;
- **failed-device detail** for both histories: the device with the most
  failed installs and the list.

`BACKUP_FACTS` names the facts a Cove connector will record; until it
exists the PDF's Backup section is "not yet assessed" and prints the shape
it is waiting for, and backup coverage against NinjaOne's workstations is
recorded as a gap, never as zero.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

from app.connectors.factory import connector_for
from app.connectors.ninjaone.client import QUERY_PATH, NinjaOneClient
from app.connectors.ninjaone.hardware import AGE_METHOD, device_role, estimated_age_years
from app.connectors.ninjaone.models import NinjaLoggedOnUser, NinjaSoftwarePatchInstall
from app.models.enums import ConnectorName
from app.reports.tbr.model import FactTable, Gap

if TYPE_CHECKING:
    from app.reports.tbr.collect import Collector

# --- ids the PDF reads -------------------------------------------------------------------

LIFECYCLE_WORKSTATIONS = "ninjaone.lifecycle.workstations"
LIFECYCLE_SERVERS = "ninjaone.lifecycle.servers"
LIFECYCLE_COLUMNS = [
    "Device",
    "Last logged-in user",
    "Make",
    "Model",
    "OS",
    "RAM",
    "CPU",
    "Location",
    "Storage used",
    "Storage total",
    "Storage free",
    "Free %",
    "Est. age (years)",
]
SOFTWARE_INSTALLED = "ninjaone.software_patches.installed"
SOFTWARE_FAILED = "ninjaone.software_patches.failed"
SOFTWARE_ATTEMPTED = "ninjaone.software_patches.attempted"
SOFTWARE_SUCCESS = "ninjaone.software_patches.success_percent"
SOFTWARE_FAILED_DEVICES = "ninjaone.software_patches.failed_devices"
SOFTWARE_TOP_FAILED = "ninjaone.software_patches.top_failed_device"
SOFTWARE_FAILED_LIST = "ninjaone.software_patches.failed.devices"
OS_TOP_FAILED = "ninjaone.patches.top_failed_device"
OS_FAILED_LIST = "ninjaone.patches.failed.devices"

#: Storage bands, by the share of the volume that is free.
HEALTHY_FREE_PERCENT = 40.0
LOW_FREE_PERCENT = 20.0
NOT_REPORTED = "not reported"

#: What a Cove connector will record, and what the PDF's Backup section shows
#: as "not yet assessed" until it does. Fact ids only; nothing here is a value.
BACKUP_FACTS: dict[str, str] = {
    "cove.devices.total": "Devices backed up",
    "cove.storage.used": "Used backup storage",
    "cove.mailboxes.user": "Protected user mailboxes",
    "cove.mailboxes.shared": "Protected shared mailboxes",
    "cove.onedrive.users": "OneDrive protected users",
    "cove.retention.policies": "Retention policies",
}
BACKUP_TABLE = "cove.devices.list"
BACKUP_COVERAGE = "coverage.backup.machines_percent"
"""Machines rather than workstations: a server without a backup is the
more serious half of this, and leaving servers out of the share would
flatter it. Renamed before anything stored it."""
BACKUP_COVERAGE_GAP = "Backup coverage against NinjaOne workstations"


def free_band(free_percent: float | None) -> str:
    """`healthy`, `low`, `critical`, or `unknown` when the volume reported no size."""
    if free_percent is None:
        return "unknown"
    if free_percent >= HEALTHY_FREE_PERCENT:
        return "healthy"
    if free_percent >= LOW_FREE_PERCENT:
        return "low"
    return "critical"


def _gb(value: int | None) -> str:
    return NOT_REPORTED if value is None else f"{value / 10**9:.1f} GB"


def _memory(value: int | None) -> str:
    return NOT_REPORTED if not value else f"{value / 2**30:.1f} GB"


# --- lifecycle -------------------------------------------------------------------------------


async def collect_lifecycle(run: Collector) -> None:
    """One row per workstation and per server, from what the run already read of
    NinjaOne (devices, hardware profiles, volumes, locations) plus who logged on
    last (`logged-on-users`). Machines the hardware report knows nothing about
    still get a row, with `not reported` where NinjaOne said nothing."""
    profiles = {p.device.id: p for p in run.ninja_profiles}
    if not run.ninja_devices:
        return
    async with connector_for(run.client_id, ConnectorName.NINJAONE, run.site_id) as c:
        assert isinstance(c, NinjaOneClient)

        async def read() -> list[tuple[Any, Any, NinjaLoggedOnUser]]:
            raw, _, _ = await c.query_scoped("logged-on-users")
            rows = [NinjaLoggedOnUser.model_validate(item) for item in raw]
            return await c.place_by_device(
                rows,
                [row.device_id for row in rows],
                endpoint=QUERY_PATH.format(report="logged-on-users"),
            )

        placed = await run.audit.read(
            "ninjaone.logged_on_users",
            ConnectorName.NINJAONE,
            read,
            summarise=lambda r: {"rows": len(r)},
        )
    latest_user: dict[int, NinjaLoggedOnUser] = {}
    for _, device, logon in placed:
        current = latest_user.get(device.id)
        if current is None or (
            logon.logon_time is not None
            and (current.logon_time is None or logon.logon_time > current.logon_time)
        ):
            latest_user[device.id] = logon
    run.facts.names["users"] = sorted(
        {u.user_name for u in latest_user.values() if u.user_name}
        | set(run.facts.names.get("users", []))
    )
    # A location is a name the franchisee typed: often the customer's name, an
    # office, sometimes a person. A demo render replaces it like any other name.
    run.facts.names["locations"] = sorted(
        {name for name in run.ninja_locations.values() if name and name != NOT_REPORTED}
        | set(run.facts.names.get("locations", []))
    )

    storage: dict[int, tuple[int, int]] = {}
    for _, device, volume in run.ninja_volumes:
        if (volume.device_type or "") != "Local Disk" or not volume.capacity:
            continue
        capacity, available = storage.get(device.id, (0, 0))
        storage[device.id] = (capacity + volume.capacity, available + (volume.free_space or 0))

    workstations: list[list[str]] = []
    servers: list[list[str]] = []
    unknown_age = 0
    year = (run.period.end - dt.timedelta(days=1)).year
    for _, device in run.ninja_devices:
        profile = profiles.get(device.id)
        # The same decision the deck's counts rest on (`hardware.device_role`),
        # taken from the inventory step; recomputed only if that step did not run.
        role = run.ninja_roles.get(device.id) or device_role(
            profile.os_name if profile else None, device.node_class
        )
        if role.role == "other":
            continue
        system = profile.system if profile else None
        processor = profile.processor if profile else None
        total_free = storage.get(device.id)
        used = total = free = share = NOT_REPORTED
        if total_free is not None:
            total_bytes, free_bytes = total_free
            used, total, free = _gb(total_bytes - free_bytes), _gb(total_bytes), _gb(free_bytes)
            share = f"{100 * free_bytes / total_bytes:.1f}%"
        age = estimated_age_years(processor, year=year)
        if age is None:
            unknown_age += 1
        logged = latest_user.get(device.id)
        row: list[str] = [
            device.name,
            (logged.user_name if logged and logged.user_name else NOT_REPORTED),
            (system.manufacturer if system and system.manufacturer else NOT_REPORTED),
            (system.model if system and system.model else NOT_REPORTED),
            (profile.os_name if profile and profile.os_name else NOT_REPORTED),
            _memory(system.total_physical_memory if system else None),
            processor or NOT_REPORTED,
            run.ninja_locations.get(device.location_id or -1, NOT_REPORTED),
            used,
            total,
            free,
            share,
            "" if age is None else str(age),
        ]
        (workstations if role.role == "workstation" else servers).append(row)
    for table_id, label, rows, what in (
        (LIFECYCLE_WORKSTATIONS, "Workstation lifecycle", workstations, "workstation"),
        (LIFECYCLE_SERVERS, "Server lifecycle", servers, "server"),
    ):
        # An empty table is written too: the PDF says "No servers at this site"
        # from it, rather than guessing from a missing table.
        run.facts.tables[table_id] = FactTable(
            id=table_id,
            label=label,
            definition=(
                f"Every NinjaOne {what} at the site with what NinjaOne reports about it; "
                "workstation or server is decided by the operating system where it can be "
                "(Windows is a server only as Windows Server, macOS is a workstation) and by "
                "NinjaOne's node class otherwise. "
                "Storage is the sum of the machine's Local Disk volumes; a machine with no "
                f"volume reported reads '{NOT_REPORTED}', never zero. Free share bands: "
                f"healthy {HEALTHY_FREE_PERCENT:g}% or more free, low {LOW_FREE_PERCENT:g} to "
                f"{HEALTHY_FREE_PERCENT:g}%, critical under {LOW_FREE_PERCENT:g}%. {AGE_METHOD}"
            ),
            source="ninjaone",
            columns=list(LIFECYCLE_COLUMNS),
            rows=sorted(rows, key=lambda r: r[0].lower()),
            total=len(rows),
            as_of=run.now,
        )
    run.fact(
        "ninjaone.lifecycle.age_unknown",
        "Machines whose estimated age could not be read",
        unknown_age,
        "Workstations and servers whose processor name gives no generation the age mapping "
        "knows; their age column is blank.",
        "ninjaone",
    )


# --- third-party patch installs -------------------------------------------------------------


async def collect_software_patch_installs(run: Collector) -> None:
    """NinjaOne's third-party (software) patch install history for the period, judged
    like the OS history: installed, failed, attempted, success rate, the device
    with the most failures, and the failed-device list."""
    params = {
        "installedAfter": f"{run.period.start:%Y-%m-%d}",
        "installedBefore": f"{run.period.end:%Y-%m-%d}",
    }
    async with connector_for(run.client_id, ConnectorName.NINJAONE, run.site_id) as c:
        assert isinstance(c, NinjaOneClient)

        async def read() -> list[tuple[Any, Any, NinjaSoftwarePatchInstall]]:
            raw, _, _ = await c.query_scoped("software-patch-installs", params=params)
            rows = [NinjaSoftwarePatchInstall.model_validate(item) for item in raw]
            return await c.place_by_device(
                rows,
                [row.device_id for row in rows],
                endpoint=QUERY_PATH.format(report="software-patch-installs"),
            )

        placed = await run.audit.read(
            "ninjaone.software_patch_installs",
            ConnectorName.NINJAONE,
            read,
            summarise=lambda r: {"rows": len(r)},
        )
    in_period = [
        (device, row)
        for _, device, row in placed
        if row.installed_at is None or run.period.start <= row.installed_at < run.period.end
    ]
    installed = [x for x in in_period if (x[1].status or "").upper() == "INSTALLED"]
    failed = [x for x in in_period if (x[1].status or "").upper() == "FAILED"]
    run.fact(
        SOFTWARE_INSTALLED,
        "Third-party patches installed in the period",
        len(installed),
        "Rows of NinjaOne's software patch installation history (software-patch-installs) "
        "with status INSTALLED and an install time inside the review period.",
        "ninjaone",
        period_bound=True,
    )
    run.fact(
        SOFTWARE_FAILED,
        "Third-party patch installations that failed in the period",
        len(failed),
        "Rows of the same history with status FAILED inside the review period. One patch "
        "failing on one device twice is two rows.",
        "ninjaone",
        period_bound=True,
    )
    record_failed_devices(
        run,
        failed,
        devices_fact=SOFTWARE_FAILED_DEVICES,
        top_fact=SOFTWARE_TOP_FAILED,
        table_id=SOFTWARE_FAILED_LIST,
        what="third-party patch",
    )
    attempts = len(installed) + len(failed)
    if attempts:
        share = round(100 * len(installed) / attempts, 1)
        run.fact(
            SOFTWARE_ATTEMPTED,
            "Third-party patch installations attempted in the period",
            attempts,
            "Installed plus failed rows in the period: what the success rate is out of.",
            "ninjaone",
            period_bound=True,
        )
        run.fact(
            SOFTWARE_SUCCESS,
            "Third-party patch installation success rate in the period",
            share,
            "Installed rows as a percentage of installed plus failed rows in the period.",
            "ninjaone",
            display=f"{share:g}%",
            period_bound=True,
            denominator=SOFTWARE_ATTEMPTED,
        )


def record_failed_devices(
    run: Collector,
    failed: list[tuple[Any, Any]],
    *,
    devices_fact: str,
    top_fact: str,
    table_id: str,
    what: str,
) -> None:
    """The distinct devices behind a failed-install count, the one with the most
    failures, and the list: shared by the OS and the third-party histories."""
    per_device: Counter[str] = Counter()
    ids: dict[str, int] = {}
    for device, _ in failed:
        per_device[device.name] += 1
        ids[device.name] = device.id
    run.fact(
        devices_fact,
        f"Devices with at least one failed {what} installation in the period",
        len(per_device),
        f"Distinct devices behind the failed {what} installations.",
        "ninjaone",
        period_bound=True,
    )
    if not per_device:
        return
    top_name, top_count = per_device.most_common(1)[0]
    run.fact(
        top_fact,
        f"Device with the most failed {what} installations",
        top_name,
        f"The device with the most failed {what} installation rows in the period ({top_count}).",
        "ninjaone",
        period_bound=True,
    )
    run.facts.tables[table_id] = FactTable(
        id=table_id,
        label=f"Devices with failed {what} installations",
        definition=f"Every device with a failed {what} installation in the period, with the "
        "number of failed rows.",
        source="ninjaone",
        columns=["Device", "Failed installations"],
        rows=[[name, str(count)] for name, count in per_device.most_common()],
        total=len(per_device),
        as_of=run.now,
    )


# --- backup: the shape, and the coverage gap ----------------------------------------------


def record_backup_gap(run: Collector) -> None:
    """Cove is not connected: backup coverage against NinjaOne's workstations is a
    recorded gap, so the PDF says 'not yet assessed' and never prints a zero."""
    if any(g.what == BACKUP_COVERAGE_GAP for g in run.facts.gaps):
        return
    run.facts.gaps.append(
        Gap(
            what=BACKUP_COVERAGE_GAP,
            why="Backup facts come from N-able Cove, which is not connected yet; the share of "
            "NinjaOne workstations with a backup is not assessed.",
            source="config",
        )
    )


def failed_by_device(failed: list[tuple[Any, Any]]) -> dict[str, int]:
    """Failures per device name, for callers that hold the rows (tests)."""
    counts: defaultdict[str, int] = defaultdict(int)
    for device, _ in failed:
        counts[device.name] += 1
    return dict(counts)

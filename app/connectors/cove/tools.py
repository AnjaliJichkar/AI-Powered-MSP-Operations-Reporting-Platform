"""The Cove backup tools the model can call.

Each docstring is what the model reads when it chooses a tool. Every tool is
read-only (hard rule 2, enforced by the client's method allowlist), takes
`client_id` (hard rule 1) and an optional `site_id` the loop resolves from a
slug, and reports `truncated` honestly (hard rule 7). Results are compact:
counts are taken over every row before the row cap, so "how many" never
depends on the rows that were kept.

Two things decide every answer here and both are the review's own rules
(`app.reports.tbr.backup`), so a machine reads the same in Slack as it does
on a deck and on a ticket:

* **The status word is where a machine stands now** — what the last session
  did, and how long since one succeeded. It is not a window: a machine that
  failed a fortnight ago and has backed up every night since is backed up.
  The window is reported beside it, as a count of failed or missed days.
* **Cove counts a customer's Microsoft 365 tenant as a device.** Every count
  about machines leaves the tenant out and says so, because "5 protected" and
  "5 machines protected" are different claims about a customer with four.

The fleet read is one request. A device read at a partner covers the whole
tree beneath it (api-notes/cove.md), so an unscoped question is a single call
rather than one per end customer, and the rows are placed by their own
partner id afterwards.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.registry import tool
from app.connectors.cove.client import CoveClient
from app.connectors.cove.history import WINDOW_DAYS
from app.connectors.cove.models import CoveDevice
from app.connectors.factory import connector_for
from app.connectors.matching import normalise_hostname
from app.connectors.results import RowCapped, SiteTagged, capped
from app.connectors.scope import SiteRef
from app.models.enums import ConnectorName

BackupGroupBy = Literal["status", "site", "retention"]

#: The words a status may take, in the order somebody would work through them.
STATUSES = ("not_backed_up", "failed", "warnings", "ok")


class BackupRow(SiteTagged):
    """One Cove machine and where its backup stands."""

    device: str = Field(description="The machine's name, as Cove holds it.")
    status: str = Field(
        description="ok, warnings (completed with errors), failed, or not_backed_up "
        "(nothing has succeeded for a week or more). Where it stands now."
    )
    last_successful: dt.datetime | None = Field(
        default=None, description="Empty where Cove has never recorded a successful backup."
    )
    days_since_successful: int | None = Field(
        default=None, description="Empty for the same reason; 'never' is not a large number."
    )
    last_session: str | None = Field(default=None, description="What the last session did.")
    failed_or_missed_days: str = Field(
        description="How many of the window's days failed or ran nothing, of the days that "
        "could be judged. The history behind a status that is only about now."
    )
    retention: str | None = Field(
        default=None, description="Cove's retention policy: All-In, Documents, 90D-52W-36M-7Y."
    )
    used_storage_gb: float | None = None


class BackupFailures(RowCapped):
    client_id: str
    sites_covered: list[str]
    machines: int = Field(description="Cove machines in scope. Microsoft 365 tenants excluded.")
    microsoft365_tenants: int = Field(
        description="Cove rows that are a customer's Microsoft 365 tenant, not a machine."
    )
    by_status: dict[str, int] = Field(description="Every machine by status, before the cap.")
    by_site: dict[str, int] = Field(description="Matching machines by end customer.")
    window_days: int
    rows: list[BackupRow]


class BackupCounts(BaseModel):
    client_id: str
    sites_covered: list[str]
    group_by: BackupGroupBy
    machines: int
    microsoft365_tenants: int
    counts: dict[str, int]
    window_days: int
    statuses: list[str] = Field(description="Every status a machine can be in, for the wording.")


class DeviceBackup(BaseModel):
    client_id: str
    device_name: str = Field(description="The name that was asked about.")
    found: bool
    detail: str | None = Field(
        default=None, description="Why there is no answer, when there is none."
    )
    backup: BackupRow | None = None
    candidates: list[str] = Field(
        default_factory=list,
        description="Other Cove machines of that name, when more than one matched.",
    )


def _covered(connector: CoveClient) -> list[str]:
    return sorted(connector.scope.target_slugs)


def _gb(value: int | None) -> float | None:
    return None if value is None else round(value / 1_000_000_000, 1)


def _row(site: SiteRef, device: CoveDevice, *, now: dt.datetime) -> BackupRow:
    from app.reports.tbr.backup import days_since_success, rate_for, status_of

    rate = rate_for(device, now=now, window=WINDOW_DAYS)
    return BackupRow(
        site=site.name,
        site_slug=site.slug,
        device=device.name,
        status=status_of(device, now=now),
        last_successful=device.last_successful_at,
        days_since_successful=days_since_success(device, now=now),
        last_session=device.last_session_status,
        failed_or_missed_days=(
            "not measured" if rate.eligible <= 0 else f"{rate.bad} of {rate.eligible}"
        ),
        retention=device.product,
        used_storage_gb=_gb(device.used_storage_bytes),
    )


async def _fleet(
    client_id: str, site_id: str | None
) -> tuple[list[tuple[SiteRef, CoveDevice]], list[str]]:
    """Every Cove row in scope, with the sites it covers.

    One request where no site is named, because a partner read covers the tree
    beneath it; the site's own read where one is.
    """
    async with connector_for(client_id, ConnectorName.COVE, site_id) as connector:
        assert isinstance(connector, CoveClient)
        covered = _covered(connector)
        if site_id is None:
            return await connector.list_fleet_with_sites(), covered
        return [
            (ref, device)
            for ref, devices in await connector.list_devices_for_targets()
            for device in devices
        ], covered


@tool(ConnectorName.COVE)
async def list_backup_failures(
    client_id: str, site_id: str | None = None, include_warnings: bool = False
) -> BackupFailures:
    """List the machines whose backup is failing or has stopped, worst first.

    THE tool for "which customers have failed backups", "what backups are
    broken", "is anything not backing up", "show me the backup failures at
    Beacon". Each row is one machine with its end customer, the word for where
    it stands now, when it last succeeded and how many of the last 28 days
    failed or ran nothing. Counts by status and by end customer cover every
    machine before the row cap.

    Two statuses are listed: `failed` (a job ran and broke, or nothing has
    succeeded for three days) and `not_backed_up` (nothing has succeeded for
    a week or more, which is a different problem and wants different people).
    `include_warnings` adds `warnings` — the job finished and reported errors,
    so what it wrote may not be everything. A customer's Microsoft 365 tenant
    is a Cove device too; it is counted separately and never as a machine.

    Not for machines Cove does not hold at all: that is a coverage gap, and
    `list_machines_without_backup` is the tool. Not for one named machine
    (`get_device_backup`) or for "how many" (`count_backups`).
    """

    now = dt.datetime.now(dt.UTC)
    rows, covered = await _fleet(client_id, site_id)
    machines = [(site, d) for site, d in rows if d.is_machine]
    tenants = len(rows) - len(machines)
    assessed = [(site, _row(site, d, now=now)) for site, d in machines]
    by_status = Counter(row.status for _, row in assessed)
    wanted = {"failed", "not_backed_up"} | ({"warnings"} if include_warnings else set())
    matching = [(site, row) for site, row in assessed if row.status in wanted]
    order = {name: i for i, name in enumerate(STATUSES)}
    matching.sort(key=lambda pair: (order.get(pair[1].status, 9), pair[1].site, pair[1].device))
    kept, counts = capped("list_backup_failures", [row for _, row in matching], noun="machines")
    return BackupFailures(
        client_id=str(client_id),
        sites_covered=covered,
        machines=len(machines),
        microsoft365_tenants=tenants,
        by_status={name: by_status[name] for name in STATUSES if by_status[name]},
        by_site=dict(sorted(Counter(row.site for _, row in matching).items())),
        window_days=WINDOW_DAYS,
        rows=kept,
        **counts,
    )


@tool(ConnectorName.COVE)
async def count_backups(
    client_id: str, group_by: BackupGroupBy = "status", site_id: str | None = None
) -> BackupCounts:
    """Count the machines Cove backs up, by status, end customer or retention policy.

    Use this for "how many machines are backed up", "how many backups are
    failing", "what retention is everyone on", "how many does each customer
    have". It is one small answer whatever the fleet; reach for
    `list_backup_failures` only once somebody wants the names.

    `group_by`: "status" (ok, warnings, failed, not_backed_up — where each
    machine stands now), "site" (per end customer) or "retention" (Cove's
    policy: All-In, Documents, 90D-52W-36M-7Y). A customer's Microsoft 365
    tenant is counted on its own line and never as a machine.

    Not for naming machines (`list_backup_failures`), for one machine
    (`get_device_backup`), or for machines Cove has no device for at all
    (`list_machines_without_backup`).
    """

    now = dt.datetime.now(dt.UTC)
    rows, covered = await _fleet(client_id, site_id)
    machines = [(site, d) for site, d in rows if d.is_machine]

    def label(site: SiteRef, device: CoveDevice) -> str:
        if group_by == "site":
            return site.name
        if group_by == "retention":
            return device.product or "not reported"
        return _row(site, device, now=now).status

    tally = Counter(label(site, d) for site, d in machines)
    counts = (
        {name: tally[name] for name in STATUSES if tally[name]}
        if group_by == "status"
        else dict(sorted(tally.items(), key=lambda item: (-item[1], item[0])))
    )
    return BackupCounts(
        client_id=str(client_id),
        sites_covered=covered,
        group_by=group_by,
        machines=len(machines),
        microsoft365_tenants=len(rows) - len(machines),
        counts=counts,
        window_days=WINDOW_DAYS,
        statuses=list(STATUSES),
    )


@tool(ConnectorName.COVE)
async def get_device_backup(
    client_id: str, device_name: str, site_id: str | None = None
) -> DeviceBackup:
    """Show one machine's backup: where it stands now, when it last succeeded, what is kept.

    Use this when an engineer names a machine and asks about its backup: "is
    TL-AUS-SRV01 backed up", "when did that server last back up", "what
    retention is the file server on". The name is matched on the hostname,
    ignoring case, Cove's own `_xxxxx` suffix and any domain suffix, within
    the client's end customers (or one if given). If several machines share
    the name the first is shown and the rest are in `candidates`; ask with
    `site` to pick one.

    `found` is false when Cove holds no machine of that name — which is not
    the same as a machine with no backup, because Cove may simply never have
    been asked to back it up. `list_machines_without_backup` is the tool for
    that question.

    Not for a list (`list_backup_failures`), a count (`count_backups`), or
    NinjaOne's view of the same machine (`get_device`).
    """
    wanted = normalise_hostname(device_name)
    if not wanted:
        raise ValueError("device_name must be a hostname, e.g. TL-AUS-SRV01")
    now = dt.datetime.now(dt.UTC)
    rows, _covered_slugs = await _fleet(client_id, site_id)
    hits = [
        (site, device)
        for site, device in rows
        if device.is_machine
        and (device.match_key == wanted or normalise_hostname(device.name) == wanted)
    ]
    if not hits:
        return DeviceBackup(
            client_id=str(client_id),
            device_name=device_name,
            found=False,
            detail=f"Cove holds no machine named {device_name!r} for this client. That is not "
            "the same as a machine whose backup is failing: use list_machines_without_backup "
            "to see whether it is a machine Cove was never asked to back up.",
        )
    site, device = hits[0]
    return DeviceBackup(
        client_id=str(client_id),
        device_name=device_name,
        found=True,
        backup=_row(site, device, now=now),
        candidates=[f"{d.name} ({s.slug})" for s, d in hits[1:]],
    )

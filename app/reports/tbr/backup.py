"""The Backup section's derivations: what the 28 days of history come to.

The connector reads Cove's bar and says what happened on each day
(`app/connectors/cove/history.py`). **This module decides what counts as
good**, which is a reporting decision and not a vendor one, and the rule it
applies is printed beside every number it produces:

- a day that **completed** is a success;
- a day that **completed with errors** is also a success, counted and listed
  apart, because somebody has to look at it and it is not a lost backup;
- a day that **failed, was interrupted or was aborted** is a failure;
- a day with **no session at all** is a missed day, not a quiet pass. A
  device nothing ever ran on scores 0%, never "no failures";
- **today's session, if it is still running, is left out of both halves.**
  The day is not over, and counting it against the device would mark a
  machine down for backing up at the moment somebody asked. A session still
  running on an *earlier* day is a missed day: it never ended, so there is
  nothing to restore from.

The denominator is the days the device could have backed up: the window, or
its whole life if it is younger than the window. A machine installed
yesterday is not at 3% because it missed the 27 days before it existed.

**The window is as-of, not the review period.** Cove sends 28 days ending
today and no way to ask for a calendar month, so these facts are the state
at generation (`period_bound=False`) and their definitions say so. The
month-by-month trend is built from the stored facts of previous reviews,
which is how every other comparison in a review is made.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NamedTuple

from app.connectors.cove.history import WINDOW_DAYS, History
from app.connectors.cove.models import CoveDevice
from app.models.enums import ConnectorName
from app.reports.tbr.model import ChartData, ChartSeries, FactSet, FactTable, Gap

if TYPE_CHECKING:  # pragma: no cover - the collector imports this module
    from app.reports.tbr.collect import Collector

#: What each day counts as. Named here because the report prints the rule.
SUCCESS_DAYS: Final = ("completed",)
WARNING_DAYS: Final = ("completed_with_errors",)
FAILURE_DAYS: Final = ("failed", "interrupted", "aborted")
#: A day nothing finished on. An *earlier* day still running is one of these:
#: a session that never ended is not a backup somebody can restore from.
MISSED_DAYS: Final = ("none", "not_started", "in_process", "in_progress_with_faults")

#: A day whose character we could not read. Judged neither way and left out
#: of the denominator: we do not know what happened, and guessing in either
#: direction would be inventing a measurement.
UNREADABLE: Final = "unknown"

#: Today's session, still going. Left out of both halves of the rate: the
#: day is not over, and counting it against the device would mark a machine
#: down for backing up at the moment somebody asked.
RUNNING: Final = "in_process"

#: A backup older than this is the first thing an engineer should see.
STALE_HOURS: Final = 72

#: Past this, the device is not "late", it is not being backed up.
ABANDONED_DAYS: Final = 7

#: The one sentence that has to travel with every rate.
RULE: Final = (
    "A day counts as a success when the backup completed, including one that "
    "completed with errors; a day that failed, was interrupted or was aborted, "
    "and a day with no session at all, both count against it. The share is of "
    "the days the device could have backed up, so a device younger than the "
    "window is measured over its own life. A session still running today is "
    "left out of both halves, because the day is not over."
)


@dataclass(frozen=True, slots=True)
class Rate:
    """One device's four weeks, and what they come to."""

    successes: int
    warnings: int
    failures: int
    missed: int
    eligible: int
    """Days the device could have backed up and that we can judge: the
    window or its whole life, less today if a backup is running and less any
    day whose character we could not read."""
    running_now: bool = False
    """A session was in progress today, so today is in neither half."""
    unreadable: int = 0

    @property
    def good(self) -> int:
        """Days a backup was taken, warnings included."""
        return self.successes + self.warnings

    @property
    def bad(self) -> int:
        return self.failures + self.missed

    @property
    def share(self) -> float | None:
        """The success rate, or `None` where there was no day to judge.

        `None` is not zero: a device Cove has never sent a day for cannot be
        scored, and a report that printed 0% for it would be asserting a
        failure nobody measured. A device with days, none of them good, *is*
        zero.
        """
        if self.eligible <= 0:
            return None
        return self.good / self.eligible


def eligible_days(device: CoveDevice, *, now: dt.datetime, window: int = WINDOW_DAYS) -> int:
    """How many of the window's days this device could have backed up on."""
    if device.created_at is None:
        return window
    lived = (now - device.created_at).days + 1
    return max(0, min(window, lived))


def rate_for(device: CoveDevice, *, now: dt.datetime, window: int = WINDOW_DAYS) -> Rate:
    """One device's rate over the window, clamped to its own life."""
    lived = eligible_days(device, now=now, window=window)
    days = History.read(device.colour_bar).recent(lived).days
    running_now = bool(days) and days[0] == RUNNING
    judged = days[1:] if running_now else days
    unreadable = sum(1 for day in judged if day == UNREADABLE)
    return Rate(
        successes=sum(1 for day in judged if day in SUCCESS_DAYS),
        warnings=sum(1 for day in judged if day in WARNING_DAYS),
        failures=sum(1 for day in judged if day in FAILURE_DAYS),
        missed=sum(1 for day in judged if day in MISSED_DAYS),
        eligible=max(0, lived - (1 if running_now else 0) - unreadable),
        running_now=running_now,
        unreadable=unreadable,
    )


def window_dates(*, now: dt.datetime, days: int = WINDOW_DAYS) -> tuple[dt.date, dt.date]:
    """The first and last day the window covers, inclusive.

    Printed beside every rate, because "last 28 days" is not a period a
    reader can check and two dates are.
    """
    last = now.date()
    return last - dt.timedelta(days=max(0, days - 1)), last


def window_label(*, now: dt.datetime, days: int = WINDOW_DAYS) -> str:
    """`last 28 days (22 Sep - 19 Oct)`, for the heading above a rate."""
    first, last = window_dates(now=now, days=days)
    return f"last {days} days ({_day(first)} - {_day(last)})"


def _day(when: dt.date) -> str:
    """`22 Sep`, without a leading zero and without platform-specific formats."""
    return f"{when.day} {when:%b}"


def fleet_rate(rates: Sequence[Rate]) -> Rate:
    """Every device's days added together.

    Days rather than devices: a site of one server backing up daily and one
    laptop that never does is at 50%, and averaging per-device percentages
    would say the same thing only by accident. Adding days is the figure
    that survives a site gaining a device mid-month.
    """
    return Rate(
        successes=sum(r.successes for r in rates),
        warnings=sum(r.warnings for r in rates),
        failures=sum(r.failures for r in rates),
        missed=sum(r.missed for r in rates),
        eligible=sum(r.eligible for r in rates),
        running_now=any(r.running_now for r in rates),
        unreadable=sum(r.unreadable for r in rates),
    )


@dataclass(frozen=True, slots=True)
class Attention:
    """One device worth looking at, and why."""

    device: CoveDevice
    rate: Rate
    days_since_success: int | None
    """`None` where Cove has never recorded a successful backup."""

    @property
    def never(self) -> bool:
        return self.days_since_success is None


def days_since_success(device: CoveDevice, *, now: dt.datetime) -> int | None:
    if device.last_successful_at is None:
        return None
    return max(0, (now - device.last_successful_at).days)


def stale(
    devices: Sequence[CoveDevice], *, now: dt.datetime, hours: int = STALE_HOURS
) -> list[Attention]:
    """Devices with no successful backup inside `hours`, worst first.

    A device that has never backed up sorts above one that backed up a year
    ago: "never" is not a very large number of days, and ordering it as one
    would bury it under whichever machine happened to be oldest.
    """
    late: list[Attention] = []
    cutoff = dt.timedelta(hours=hours)
    for device in devices:
        since = days_since_success(device, now=now)
        if device.last_successful_at is None or now - device.last_successful_at > cutoff:
            late.append(
                Attention(device=device, rate=rate_for(device, now=now), days_since_success=since)
            )
    return sorted(late, key=lambda a: (not a.never, -(a.days_since_success or 0)))


def worst_days(
    devices: Sequence[CoveDevice], *, now: dt.datetime, limit: int = 5
) -> list[Attention]:
    """Devices with the most failed and missed days, worst first.

    Separate from `stale` on purpose: a device that backs up most nights and
    fails the rest never appears there, and is exactly the one somebody
    should fix before it becomes the other list.
    """
    scored = [
        Attention(
            device=device,
            rate=rate_for(device, now=now),
            days_since_success=days_since_success(device, now=now),
        )
        for device in devices
    ]
    with_trouble = [a for a in scored if a.rate.bad > 0]
    with_trouble.sort(key=lambda a: (-a.rate.bad, a.device.key))
    return with_trouble[:limit]


#: The four words in the reader's language, for the one place that draws a
#: document rather than a page. `app/static/js/states.js` holds the page's
#: copy of them, with the reason beside each; a key must never reach either
#: output as itself.
STATUS_WORDS: Final[Mapping[str, str]] = {
    "ok": "Backed up",
    "warnings": "Completed with errors",
    "failed": "Backup failed",
    "not_backed_up": "No backup",
}


def status_of(device: CoveDevice, *, now: dt.datetime) -> str:
    """The one word beside a device: where it stands **now**.

    Two things decide it and neither is a window: what the last session did,
    and how long it has been since one succeeded. A status is a state, so a
    machine that failed a fortnight ago and has backed up every night since
    is backed up — the month's failures are history, and history has a
    column of its own (`cove.devices.list`, "Failed or missed days") rather
    than a word that says the wrong thing about today.

    `not_backed_up` is deliberately not "failed": a machine nothing has run
    on for a week is a different problem from one whose backup ran and
    broke, and the two want different people.
    """
    since = days_since_success(device, now=now)
    if since is None or since >= ABANDONED_DAYS:
        return "not_backed_up"
    if device.last_session_status in FAILURE_DAYS or since * 24 >= STALE_HOURS:
        return "failed"
    if device.last_session_status in WARNING_DAYS:
        return "warnings"
    return "ok"


# --- collecting it ------------------------------------------------------------------

#: Name groups the coverage join reads back, the same shape as Bitdefender's.
GROUP_COVE_DEVICES: Final = "cove.devices"
GROUP_COVE_STALE: Final = "cove.coverage.stale"
GROUP_COVE_ABSENT: Final = "cove.coverage.absent"
GROUP_COVE_LAST_SEEN: Final = "cove.last_seen.{name}"

#: What a missing Cove read is called, wherever it is recorded.
COVE_GAP: Final = "Backup devices and storage from N-able Cove"

#: A Cove machine no RMM knows about, with no successful backup for this
#: long, is most likely a machine that was decommissioned and never removed
#: from Cove. It is reported as that rather than as a protection failure,
#: and it is kept out of the success rate: a retired machine not backing up
#: is not a fleet that is failing.
RETIRED_DAYS: Final = 30

#: Network monitors, not machines anybody backs up. Excluded from the
#: coverage gap by default, because a list with a permanent tail of things
#: nobody will ever act on is a list nobody reads.
NOT_A_MACHINE: Final = ("domotz",)


def is_not_a_machine(name: str) -> bool:
    """Whether a name is a network appliance rather than a computer."""
    lowered = name.casefold()
    return any(marker in lowered for marker in NOT_A_MACHINE)


@dataclass(frozen=True, slots=True)
class Placed:
    """One Cove machine, and the name the RMM knows it by if it knows it."""

    device: CoveDevice
    ninja_name: str | None = None

    @property
    def name(self) -> str:
        """What to print: the RMM's name where there is one, else Cove's.

        One page, one vocabulary. A machine called `BAY-VSRV19-D1`
        everywhere else should not appear as `bay-vsrv19-d1` here because
        Cove lower-cases it.
        """
        return self.ninja_name or self.device.key

    def retired(self, *, now: dt.datetime) -> bool:
        """No RMM knows it and it has not backed up for a month."""
        if self.ninja_name is not None:
            return False
        since = days_since_success(self.device, now=now)
        return since is None or since >= RETIRED_DAYS


def _no_rate_because(*, machines: int, held: int) -> str:
    """Which of the three reasons there is no success rate.

    They are different facts about the customer and a report that said the
    same words for all three would be telling two of them something untrue.
    """
    if held == 0:
        return "Cove holds no machine for this customer, only a Microsoft 365 tenant."
    if machines == 0:
        return (
            "Every machine Cove holds here is reported as possibly retired, so there is "
            "nothing left to measure a success rate over."
        )
    return (
        "No machine here has a day of history to judge, so a success rate would be a share "
        "of nothing."
    )


def _percent(value: float) -> str:
    """`0%`, `100%`, `99.6%`: no decimal unless it carries something."""
    rounded = round(value, 1)
    return f"{rounded:.0f}%" if rounded == int(rounded) else f"{rounded:.1f}%"


def _storage(total: int) -> str:
    """TB on a summary card, as the sample report has it."""
    if total >= 10**12:
        return f"{total / 10**12:.2f} TB"
    return f"{total / 10**9:.1f} GB"


def _group(facts: FactSet, key: str, names: set[str]) -> None:
    facts.groups[key] = sorted(name for name in names if name)


def _date(when: dt.datetime | None) -> str:
    return "never" if when is None else _day(when.date())


def headline(
    machines: Sequence[CoveDevice],
    tenants: Sequence[CoveDevice],
    *,
    retired: int = 0,
) -> str:
    """What is protected, in words rather than a bare count.

    The kind is Cove's own classification of its own rows. Mixed kinds are
    "computers", because a sentence that says "servers" when a third of them
    are laptops is worse than the general word.
    """
    kinds = {d.os_type for d in machines}
    retired_words = f"{retired} possibly retired" if retired else ""
    if not machines and not tenants:
        # Saying "protected" of a machine nobody has backed up for a month
        # would be the page's one outright false claim.
        return (
            f"Nothing is currently backed up; {retired_words}"
            if retired
            else ("Nothing is backed up by Cove")
        )
    if not machines:
        if retired:
            return f"Microsoft 365 protected; {retired_words}"
        return "Microsoft 365 protected"
    noun = (
        "server"
        if kinds == {"server"}
        else "workstation"
        if kinds == {"workstation"}
        else "computer"
    )
    plural = noun if len(machines) == 1 else f"{noun}s"
    said = (
        f"{len(machines)} {plural} + Microsoft 365 protected"
        if tenants
        else f"{len(machines)} {plural} protected"
    )
    return f"{said}; {retired_words}" if retired else said


async def collect_cove(run: Collector) -> None:
    """Every Cove figure that does not depend on the RMM, and the groups the
    join reads back. What does depend on it is `derive_backup`."""
    # Imported here rather than at module scope: this module is part of one
    # franchisee's settings, and the coverage package reaches the connectors,
    # which reach the settings again.
    from app.connectors.cove.client import CoveClient
    from app.connectors.factory import connector_for
    from app.coverage.config import cove_endpoints

    async with connector_for(run.client_id, ConnectorName.COVE, run.site_id) as c:
        assert isinstance(c, CoveClient)
        answers = await run.audit.read(
            "cove.devices",
            ConnectorName.COVE,
            c.list_devices_for_targets,
            summarise=lambda r: {"sites": len(r), "devices": sum(len(d) for _, d in r)},
        )
    devices = [device for _ref, found in answers for device in found]
    run.cove_devices = devices
    machines = [d for d in devices if d.is_machine]
    tenants = [d for d in devices if d.is_microsoft365]

    run.fact(
        "cove.devices.total",
        "Protected",
        len(devices),
        "Everything Cove protects for this customer. A Microsoft 365 tenant is counted as a "
        "device by Cove and is named apart here, because it is not a machine.",
        "cove",
        display=headline(machines, tenants),
    )
    run.fact(
        "cove.devices.machines",
        "Machines backed up",
        len(machines),
        "Cove devices that are computers: every row whose account type is not Microsoft 365.",
        "cove",
    )
    run.fact(
        "cove.devices.tenants",
        "Microsoft 365 tenants backed up",
        len(tenants),
        "Cove rows that are a Microsoft 365 tenant rather than a machine.",
        "cove",
    )
    run.fact(
        "cove.storage.used",
        "Used storage",
        sum(d.used_storage_bytes or 0 for d in devices),
        "Storage Cove holds for this customer, across every device and tenant, as of the read.",
        "cove",
        display=_storage(sum(d.used_storage_bytes or 0 for d in devices)),
    )
    for fact_id, label, value in (
        ("cove.mailboxes.user", "User mailboxes", sum(t.user_mailboxes or 0 for t in tenants)),
        (
            "cove.mailboxes.shared",
            "Shared mailboxes",
            sum(t.shared_mailboxes or 0 for t in tenants),
        ),
        ("cove.onedrive.users", "OneDrive users", sum(t.onedrive_users or 0 for t in tenants)),
    ):
        run.fact(
            fact_id,
            label,
            value,
            "From the last session of the Microsoft 365 tenant Cove backs up for this customer.",
            "cove",
        )

    _retention(run, devices)

    judged = cove_endpoints([(ref, d) for ref, found in answers for d in found], now=run.now)
    _group(run.facts, GROUP_COVE_DEVICES, {e.name or "" for e in judged})
    _group(run.facts, GROUP_COVE_STALE, {e.name or "" for e in judged if e.state == "stale"})
    _group(run.facts, GROUP_COVE_ABSENT, {e.name or "" for e in judged if e.state == "absent"})
    for endpoint in judged:
        if endpoint.name and endpoint.last_seen is not None:
            run.facts.groups[GROUP_COVE_LAST_SEEN.format(name=endpoint.name)] = [
                endpoint.last_seen.isoformat()
            ]


def _retention(run: Collector, devices: Sequence[CoveDevice]) -> None:
    """How many devices sit on each retention policy.

    From `I10` (Product), which is what the console calls the policy and what
    the sample report's own distribution counts. Not `I39`, which is the
    coarser scheme behind it: two values across the whole pilot fleet.
    """
    tally = Counter(d.product or "not reported" for d in devices)
    run.fact(
        "cove.retention.policies",
        "Retention policies",
        len(tally),
        "Distinct retention policies across this customer's devices.",
        "cove",
    )
    if not tally:
        return
    run.facts.tables["cove.retention"] = FactTable(
        id="cove.retention",
        label="Retention policies",
        definition="Devices on each retention policy, from Cove's product on each row.",
        source="cove",
        columns=["Retention policy", "Devices"],
        rows=[[name, str(count)] for name, count in tally.most_common()],
        total=len(tally),
        as_of=run.now,
    )


# --- what the RMM has to answer first -------------------------------------------------


def derive_backup(run: Collector) -> None:
    """Everything that needs to know whether the RMM has heard of a machine.

    The success rate, the two attention lists, the device table and the
    coverage gap all turn on one question — does a NinjaOne device pair with
    this Cove machine — so they are derived together, after the join, from
    one answer.
    """
    from app.reports.tbr.security import GROUP_NINJA_COMPUTERS, assess_review_site

    devices = list(getattr(run, "cove_devices", []))
    if run.facts.get("cove.devices.total") is None:
        return
    machines = [d for d in devices if d.is_machine]
    tenants = [d for d in devices if d.is_microsoft365]

    by_cove_name: dict[str, str] = {}
    gaps: list[str] = []
    assessed = GROUP_NINJA_COMPUTERS in run.facts.groups
    if assessed:
        result = assess_review_site(
            run,
            tool="backup",
            tool_names=run.facts.groups.get(GROUP_COVE_DEVICES, []),
            stale_names=run.facts.groups.get(GROUP_COVE_STALE, []),
            absent_names=run.facts.groups.get(GROUP_COVE_ABSENT, []),
            last_seen_group=GROUP_COVE_LAST_SEEN,
        )
        for found in result.devices:
            state = found.tools.get("backup")
            if state is None:
                continue
            if state.state in ("covered", "stale"):
                for endpoint_name in state.endpoint_ids:
                    by_cove_name[endpoint_name] = found.device.name
            elif state.state in ("absent", "unmatched"):
                gaps.append(found.device.name)

    placed = [Placed(device=d, ninja_name=by_cove_name.get(d.key)) for d in machines]
    retired = [p for p in placed if assessed and p.retired(now=run.now)] if assessed else []
    live = [p for p in placed if p not in retired]

    run.fact(
        "cove.devices.total",
        "Protected",
        len(devices),
        "Everything Cove protects for this customer. A Microsoft 365 tenant is counted as a "
        "device by Cove and is named apart here, because it is not a machine. A machine "
        "reported as possibly retired is not counted as protected.",
        "cove",
        display=headline([p.device for p in live], tenants, retired=len(retired)),
    )
    _success(run, [p.device for p in live], held=len(machines))
    _attention(run, live)
    _retired(run, retired)
    _inventory(run, live, tenants)
    if assessed:
        _gap(
            run,
            gaps=gaps,
            covered=len(placed) - len(retired),
            machines=[p.device for p in placed],
        )


def _success(run: Collector, machines: Sequence[CoveDevice], *, held: int = 0) -> None:
    """The success rate, with the days it is a share of as its own fact."""
    whole = fleet_rate([rate_for(d, now=run.now) for d in machines])
    window = window_label(now=run.now)
    run.fact(
        "cove.success.days",
        "Backup days measured",
        whole.eligible,
        f"Days across every machine that could have been backed up in the {window}.",
        "cove",
    )
    run.fact(
        "cove.success.good_days",
        "Days a backup was taken",
        whole.good,
        f"Days in the {window} on which a backup completed, including with errors.",
        "cove",
        denominator="cove.success.days",
    )
    run.fact(
        "cove.success.warning_days",
        "Days a backup completed with errors",
        whole.warnings,
        f"Days in the {window} on which the backup finished and reported errors.",
        "cove",
    )
    run.fact(
        "cove.success.unreadable_days",
        "Days that could not be read",
        whole.unreadable,
        "Days whose entry in Cove's history was a character we do not recognise. In neither "
        "half of the rate: we do not know what happened, and counting them either way would "
        "invent a measurement.",
        "cove",
    )
    if whole.share is None:
        run.facts.gaps.append(
            Gap(
                what=COVE_GAP,
                why=_no_rate_because(machines=len(machines), held=held),
                source="cove",
            )
        )
        return
    run.fact(
        "cove.success.rate",
        window[0].upper() + window[1:],
        round(whole.share * 100, 1),
        f"{RULE} Measured over the {window}, which is what Cove keeps: the state when the "
        "review was generated rather than the review period. Machines reported as possibly "
        "retired are left out.",
        "cove",
        # Whole numbers print plainly; anything else keeps its decimal.
        # Rounding 99.6% to "100%" would say a fleet is perfect when a backup
        # failed, which is the one direction this figure must never err in.
        display=_percent(whole.share * 100),
        denominator="cove.success.days",
    )
    _trend(run, now_rate=whole.share * 100)


def _trend(run: Collector, *, now_rate: float) -> None:
    """Backup success at each review, oldest first.

    Labelled by review date and never by month name: each point is the 28
    days that review was holding, so two reviews a fortnight apart are two
    overlapping windows. Fewer than two points is not a trend.
    """
    points = [*run.backup_trend, (run.now, now_rate)]
    if len(points) < 2:
        return
    run.facts.charts["cove.success.trend"] = ChartData(
        id="cove.success.trend",
        title="Backup success at each review",
        kind="line",
        categories=[_day(when.date()) for when, _ in points],
        series=[ChartSeries(name="Success rate", values=[round(rate, 1) for _, rate in points])],
        definition="The success rate each review measured, at the date it was generated.",
        source="cove",
        number_format="0.0",
    )


def _attention(run: Collector, live: Sequence[Placed]) -> None:
    """Two lists, in the order somebody should work them."""
    machines = [p.device for p in live]
    names = {p.device.key: p.name for p in live}
    late = stale(machines, now=run.now)
    run.fact(
        "cove.stale.count",
        f"Machines with no successful backup in {STALE_HOURS} hours",
        len(late),
        f"Machines Cove holds whose last successful backup is more than {STALE_HOURS} hours "
        "old, or that have never had one. Machines reported as possibly retired are left out.",
        "cove",
    )
    if late:
        run.facts.tables["cove.attention"] = FactTable(
            id="cove.attention",
            label="Needs attention",
            definition="No successful backup in 72 hours, longest first.",
            source="cove",
            columns=["Device", "Last successful backup", "Days ago"],
            rows=[
                [
                    names.get(a.device.key, a.device.key),
                    _date(a.device.last_successful_at),
                    "never" if a.never else str(a.days_since_success),
                ]
                for a in late
            ],
            total=len(late),
            as_of=run.now,
        )
    worst = worst_days(machines, now=run.now)
    if worst:
        run.facts.tables["cove.worst_days"] = FactTable(
            id="cove.worst_days",
            label="Most failed or missed days",
            definition="Machines with the most days a backup failed or none ran.",
            source="cove",
            columns=["Device", "Failed days", "Missed days"],
            rows=[
                [names.get(a.device.key, a.device.key), str(a.rate.failures), str(a.rate.missed)]
                for a in worst
            ],
            total=len(worst),
            as_of=run.now,
        )


def _retired(run: Collector, retired: Sequence[Placed]) -> None:
    """Cove machines no RMM knows, with no backup for a month.

    Most likely decommissioned and never removed from Cove. Reported as that
    rather than as a protection failure, because the action is to confirm and
    remove rather than to fix a backup — and left out of the rate, since a
    retired machine not backing up is not a fleet that is failing.
    """
    run.fact(
        "cove.retired.count",
        "Possibly retired",
        len(retired),
        f"Cove machines with no NinjaOne device and no successful backup for {RETIRED_DAYS} "
        "days or more.",
        "cove",
    )
    if not retired:
        return
    run.facts.tables["cove.retired"] = FactTable(
        id="cove.retired",
        label="Possibly retired: confirm and remove from Cove",
        definition=f"Cove holds these and no NinjaOne device matches them, and none has "
        f"backed up for {RETIRED_DAYS} days or more. They are left out of the success rate "
        "and out of the list above.",
        source="cove",
        columns=["Device", "Last successful backup", "Used storage"],
        rows=[
            [
                p.name,
                _date(p.device.last_successful_at),
                _storage(p.device.used_storage_bytes or 0),
            ]
            for p in sorted(
                retired, key=lambda p: (p.device.last_successful_at is not None, p.name)
            )
        ],
        total=len(retired),
        as_of=run.now,
    )


def _inventory(run: Collector, live: Sequence[Placed], tenants: Sequence[CoveDevice]) -> None:
    """The device table. A tenant is labelled as one and never as a machine."""
    rows: list[list[str]] = [
        [
            p.name,
            p.device.os_type.replace("_", " "),
            p.device.physicality or "not reported",
            _storage(p.device.used_storage_bytes or 0),
            _date(p.device.last_successful_at),
            STATUS_WORDS[status_of(p.device, now=run.now)],
            _bad_days(rate_for(p.device, now=run.now)),
        ]
        for p in sorted(live, key=lambda p: p.name.casefold())
    ]
    rows += [
        [
            t.key,
            "Microsoft 365 tenant",
            "-",
            _storage(t.used_storage_bytes or 0),
            _date(t.last_successful_at),
            "tenant",
            "-",
        ]
        for t in tenants
    ]
    run.facts.tables["cove.devices.list"] = FactTable(
        id="cove.devices.list",
        label="Devices",
        definition="Every row Cove holds for this customer, with a Microsoft 365 tenant "
        "labelled as one.",
        source="cove",
        columns=[
            "Device",
            "OS type",
            "Physical or virtual",
            "Used storage",
            "Last successful",
            "Status",
            f"Failed or missed days ({WINDOW_DAYS} days)",
        ],
        rows=rows,
        total=len(rows),
        as_of=run.now,
    )


def _bad_days(rate: Rate) -> str:
    """The window's history beside a status that is only about today.

    Status says where a machine stands now; this says what the month did, so
    a machine that is backed up today and failed four nights of the last
    twenty-eight is not silently a clean row.
    """
    if rate.eligible <= 0:
        return "not measured"
    return f"{rate.bad} of {rate.eligible}"


class BackupScope(NamedTuple):
    """What counts as a backup gap at one site, and how sure we are.

    `mixed` is the case nobody should read past: `auto` landed on servers
    only, and yet somebody *is* backing up a workstation or two here. Either
    the customer buys backup for a handful of machines on purpose — in which
    case the setting should say so — or a rollout stalled and the laptops
    nobody got to are findings this board is now hiding. The rule cannot tell
    those apart from the rows, and neither can a sentence; a person can, in a
    minute, if they are asked.

    It is a flag for **us**: the board and the Ask tools report it, and no
    client render ever does. "Our rule is unsure about your estate" is a
    sentence about our rule.
    """

    scope: str
    why: str
    mixed: bool = False


def effective_scope(
    configured: str,
    *,
    machines: Sequence[CoveDevice],
    workstations: int | None = None,
) -> BackupScope:
    """Which machines count as a gap here, and the one line that says why.

    `auto` reads it from what Cove already holds, and **asks how many rather
    than whether**. It used to say that any backed-up workstation meant the
    site bought workstation backup, which on the pilot turned one laptop at a
    107-machine customer into 106 gaps reported every month: their Backup
    coverage read 0.9%, and the figure said more about the rule than about
    them. A site is read as buying workstation backup when Cove holds one for
    at least `BACKUP_AUTO_WORKSTATION_SHARE` of its workstations; below that
    the machines Cove does hold are exceptions, and an exception is not a
    policy.

    `workstations` is how many the site has, which is what makes a share
    possible. Where a caller cannot know it the old test applies and the
    sentence says so — better a rule a reader can see than a share of an
    unknown.

    An explicit setting always wins, because the customer who has bought
    workstation backup and not installed it yet is the case `auto` gets wrong,
    and that is the case where the gap list matters most. It is also the way
    back: a site mid-rollout is set `servers_and_workstations` by hand.
    """
    # A setting is somebody's decision, so it is never mixed: they said.
    if configured == "servers":
        return BackupScope("servers", "Servers only, by this site's setting.")
    if configured == "servers_and_workstations":
        return BackupScope(
            "servers_and_workstations",
            "Servers and workstations, by this site's setting.",
        )
    backed_up = sum(1 for d in machines if d.os_type == "workstation")
    if not backed_up:
        return BackupScope("servers", "Servers only, as no workstations are backed up here.")
    if workstations is None:
        return BackupScope(
            "servers_and_workstations",
            "Servers and workstations, as workstations are backed up here.",
        )
    from app.settings import get_settings

    share = get_settings().backup_auto_workstation_share
    if backed_up >= share * workstations:
        return BackupScope(
            "servers_and_workstations",
            f"Servers and workstations, as {backed_up} of this site's "
            f"{workstations} workstations are backed up.",
        )
    # Servers only, and somebody is still backing up a workstation here. The
    # rule has guessed, and the guess is hiding the other workstations from
    # the gap list, so it says so rather than reading as a decided thing.
    return BackupScope(
        "servers",
        f"Servers only: {backed_up} of this site's {workstations} workstations are "
        f"backed up, which reads as an exception rather than a policy.",
        mixed=True,
    )


def _gap(
    run: Collector, *, gaps: Sequence[str], covered: int, machines: Sequence[CoveDevice]
) -> None:
    """NinjaOne machines with no Cove device, under this site's own settings.

    Servers first, because a server without a backup is a different
    conversation. Never stated as a certainty: the match rests on a name
    rule, so this is what Cove appears not to hold.
    """
    from app.connectors.cove.columns import device_key
    from app.reports.tbr.monthly import BACKUP_COVERAGE

    settings = run.config.backup
    excluded = settings.excluded_for(run.site_slug)
    # Every workstation this site has, which is what `auto` asks about: how
    # many of them are backed up, not whether any is. `ninja_roles` is the one
    # decision the deck's counts and the lifecycle tables already read, so the
    # scope and the tables cannot disagree about what a machine is.
    workstations = sum(
        1
        for _ref, device in run.ninja_devices
        if run.ninja_roles.get(device.id) and run.ninja_roles[device.id].role != "server"
    )
    decision = effective_scope(
        settings.scope_for(run.site_slug), machines=machines, workstations=workstations
    )
    scope, why = decision.scope, decision.why
    servers_only = scope == "servers"
    run.fact(
        "cove.coverage.scope",
        "Machines counted as a backup gap",
        scope,
        "Which machines are expected to have a backup here. Set per site; 'auto' reads it "
        "from what Cove already holds.",
        "config",
        display=why,
    )
    if decision.mixed:
        # Internal only, and deliberately: "our rule is unsure about your
        # estate" is a sentence about our rule. A client deck and a client PDF
        # never carry it; the review screen and the board do, so somebody can
        # settle it with one setting.
        run.fact(
            "cove.coverage.scope_mixed",
            "Backup scope needs confirming",
            True,
            "Some workstations here are backed up and most are not, so `auto` has guessed "
            "servers only and is leaving the rest off the gap list. Set this site's backup "
            "scope explicitly to settle it.",
            "config",
            display="Confirm this site's backup scope",
            internal_only=True,
        )

    kept: list[tuple[str, str]] = []
    left_out = 0
    for name in gaps:
        kind = "server" if _is_server(run, name) else "workstation"
        if is_not_a_machine(name) or device_key(name) in excluded:
            left_out += 1
            continue
        if servers_only and kind != "server":
            left_out += 1
            continue
        kept.append((name, kind))

    judged = covered + len(kept)
    run.fact(
        "cove.coverage.machines",
        "Machines measured for backup coverage",
        judged,
        "NinjaOne machines Cove could be expected to hold, under this site's backup settings.",
        "derived",
    )
    if judged:
        share = covered / judged * 100
        run.fact(
            BACKUP_COVERAGE,
            "Machines with a Cove device",
            round(share, 1),
            "NinjaOne machines paired with a Cove device within the site. The pairing is a "
            "name rule rather than a certainty.",
            "derived",
            display=_percent(share),
            denominator="cove.coverage.machines",
        )
    run.fact(
        "cove.coverage.reviewed",
        "Coverage list reviewed",
        "yes" if settings.reviewed_for(run.site_slug) else "no",
        "Whether somebody has been through this site's backup coverage list. Until they have, "
        "the customer's copy says the review is pending rather than printing a list nobody has "
        "checked.",
        "config",
        internal_only=True,
    )
    _scope_changed(run, scope=scope, why=why)
    if not kept:
        return
    note = "NinjaOne machines with no Cove device paired to them, servers first."
    if left_out:
        note += f" {left_out} left out by this site's backup settings."
    run.facts.tables["cove.not_backed_up"] = FactTable(
        id="cove.not_backed_up",
        label="Not backed up by Cove",
        definition=note,
        source="derived",
        columns=["Machine", "Kind"],
        rows=[
            [name, kind]
            for name, kind in sorted(kept, key=lambda row: (row[1] != "server", row[0].casefold()))
        ],
        total=len(kept),
        as_of=run.now,
    )


def _scope_changed(run: Collector, *, scope: str, why: str) -> None:
    """Say so when this review counted a different set than the last one did.

    `auto` is a reading of what Cove holds, so it can change between two
    reviews without anybody touching a setting — a site whose only backed-up
    laptop was retired stops counting laptops, and its coverage percentage
    jumps for a reason that is not an improvement. **Internal only**: it is
    about how we measured, which is ours to explain and not the customer's
    to puzzle over.
    """
    before = run.previous_backup_scope
    if not before or before == scope:
        return
    words = {
        "servers": "servers only",
        "servers_and_workstations": "servers and workstations",
    }
    run.fact(
        "cove.coverage.scope_changed",
        "Backup coverage scope changed since the last review",
        scope,
        "What this review counted as a backup gap, and what the last one did.",
        "derived",
        display=(
            f"The last review counted {words.get(before, before)}; this one counts "
            f"{words.get(scope, scope)}. {why} A coverage figure that moved between the two "
            "reviews may have moved for this reason rather than for anything that happened "
            "to the estate."
        ),
        internal_only=True,
    )


def _is_server(run: Collector, hostname: str) -> bool:
    """Whether NinjaOne's own role decision calls this machine a server.

    The one decision (`hardware.device_role`) the deck's counts and the
    lifecycle tables already read, so this list cannot call a machine a
    server that the rest of the report calls a workstation.
    """
    for _ref, device in run.ninja_devices:
        if getattr(device, "hostname", None) == hostname:
            role = run.ninja_roles.get(device.id)
            return bool(role and role.role == "server")
    return False

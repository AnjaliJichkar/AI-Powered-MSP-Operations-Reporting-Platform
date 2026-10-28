"""The Backup section's maths: what 28 days of history come to.

No network and no database. Every device here is invented; what is being
checked is the rule, which has to be defensible in front of a customer who
disagrees with the number.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from app.connectors.cove.history import History
from app.connectors.cove.models import CoveDevice
from app.reports.tbr import backup
from app.reports.tbr.backup_config import BackupConfig

NOW = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.UTC)


def device(
    bar: str | None = None,
    *,
    key: str = "fab-ws-01",
    last_success_days: int | None = 0,
    created_days: int = 400,
    microsoft365: bool = False,
    last_session: str | None = None,
) -> CoveDevice:
    # Cove sends both the bar and the last session's own status, and they agree
    # about today: character 0 of the bar *is* the last session. So one is
    # derived from the other here rather than invented, because a device whose
    # two fields disagree is not one the vendor produces.
    return CoveDevice(
        account_id=1,
        partner_id=2,
        name=key,
        key=key,
        colour_bar=bar,
        last_session_status=last_session or (History.read(bar).days[0] if bar else None),
        account_type="microsoft365" if microsoft365 else "backup_manager",
        created_at=NOW - dt.timedelta(days=created_days),
        last_successful_at=(
            None if last_success_days is None else NOW - dt.timedelta(days=last_success_days)
        ),
    )


# --- the bar is newest first --------------------------------------------------------


def test_the_bar_is_read_newest_first() -> None:
    """Established live, 2026-10-19: a device whose last success was five
    days ago sends five zeros at the *front*. Read the other way round every
    window lands on the wrong days and nothing errors."""
    days = History.read("0000055555555555555555555555")
    assert days.days[0] == "none"  # today
    assert days.days[4] == "none"  # four days ago
    assert days.days[5] == "completed"  # five days ago: the last success
    assert days.length == 28


def test_an_unreadable_character_keeps_its_place() -> None:
    """Dropping it would shift every older day onto the wrong date."""
    days = History.read("5X5")
    assert days.days == ("completed", "unknown", "completed")


# --- the rate -----------------------------------------------------------------------


def test_a_device_that_backs_up_every_day_is_whole() -> None:
    rate = backup.rate_for(device("5" * 28), now=NOW)
    assert (rate.successes, rate.warnings, rate.failures, rate.missed) == (28, 0, 0, 0)
    assert rate.share == 1.0


def test_completing_with_errors_is_a_success_and_is_counted_apart() -> None:
    """It is not a lost backup, and somebody still has to look at it."""
    rate = backup.rate_for(device("8" * 7 + "5" * 21), now=NOW)
    assert rate.successes == 21
    assert rate.warnings == 7
    assert rate.good == 28
    assert rate.share == 1.0


def test_failed_interrupted_and_aborted_all_count_against() -> None:
    rate = backup.rate_for(device("2" * 3 + "6" * 2 + "3" * 1 + "5" * 22), now=NOW)
    assert rate.failures == 6
    assert rate.share == pytest.approx(22 / 28)


def test_a_day_with_no_session_is_missed_and_not_a_quiet_pass() -> None:
    rate = backup.rate_for(device("0" * 14 + "5" * 14), now=NOW)
    assert rate.missed == 14
    assert rate.share == pytest.approx(0.5)


def test_a_device_with_no_sessions_at_all_is_zero_not_no_failures() -> None:
    """The brief's rule, and the one a report gets wrong by omission: an
    all-quiet device has no failures, which must never read as healthy."""
    rate = backup.rate_for(device("0" * 28, last_success_days=None), now=NOW)
    assert rate.failures == 0
    assert rate.missed == 28
    assert rate.share == 0.0


def test_a_device_younger_than_the_window_is_measured_over_its_own_life() -> None:
    """Installed three days ago and backed up every night since: 100%, not
    11%. The 25 days before it existed are not days it missed."""
    rate = backup.rate_for(device("555" + "0" * 25, created_days=2), now=NOW)
    assert rate.eligible == 3
    assert rate.share == 1.0


def test_a_device_cove_has_sent_no_days_for_cannot_be_scored() -> None:
    """`None` is not zero: nobody measured it, so nothing is asserted."""
    rate = backup.rate_for(device(None, created_days=0), now=NOW)
    assert rate.eligible == 1
    assert backup.rate_for(device(None), now=NOW).share == 0.0
    # A device with no eligible day at all is unscoreable rather than failing.
    newborn = backup.rate_for(device("5", created_days=-1), now=NOW)
    assert newborn.eligible == 0
    assert newborn.share is None


def test_a_fleet_rate_adds_days_rather_than_averaging_percentages() -> None:
    """One server backing up daily beside one laptop that never does is 50%.
    Averaging the two percentages gives the same answer here and the wrong
    one as soon as the two have different numbers of days."""
    daily = backup.rate_for(device("5" * 28), now=NOW)
    never = backup.rate_for(device("0" * 28, last_success_days=None), now=NOW)
    young = backup.rate_for(device("5" * 4 + "0" * 24, created_days=3), now=NOW)
    whole = backup.fleet_rate([daily, never, young])
    assert whole.eligible == 28 + 28 + 4
    assert whole.good == 28 + 0 + 4
    assert whole.share == pytest.approx(32 / 60)


# --- a session still running --------------------------------------------------------


def test_a_backup_running_right_now_counts_neither_way() -> None:
    """The day is not over. Counting it against the device would mark a
    machine down for backing up at the moment somebody asked."""
    rate = backup.rate_for(device("1" + "5" * 27), now=NOW)
    assert rate.running_now is True
    assert rate.eligible == 27  # today is out of the denominator
    assert rate.successes == 27
    assert rate.missed == 0
    assert rate.share == 1.0


def test_a_session_still_running_on_an_earlier_day_is_a_missed_day() -> None:
    """It never ended, so there is nothing to restore from. Only *today* is
    unfinished business."""
    rate = backup.rate_for(device("5" + "1" + "5" * 26), now=NOW)
    assert rate.running_now is False
    assert rate.eligible == 28
    assert rate.missed == 1
    assert rate.share == pytest.approx(27 / 28)


def test_a_running_day_does_not_rescue_a_device_that_never_backs_up() -> None:
    rate = backup.rate_for(device("1" + "0" * 27, last_success_days=None), now=NOW)
    assert rate.eligible == 27
    assert rate.missed == 27
    assert rate.share == 0.0


def test_a_character_nobody_can_read_is_judged_neither_way() -> None:
    """Guessing in either direction would be inventing a measurement."""
    rate = backup.rate_for(device("X" * 4 + "5" * 24), now=NOW)
    assert rate.unreadable == 4
    assert rate.eligible == 24
    assert rate.share == 1.0


# --- the window a reader can check --------------------------------------------------


def test_the_window_is_printed_as_two_dates() -> None:
    """ "Last 28 days" is not a period anybody can check; two dates are."""
    first, last = backup.window_dates(now=NOW)
    assert last == dt.date(2026, 10, 19)
    assert first == dt.date(2026, 9, 22)
    assert backup.window_label(now=NOW) == "last 28 days (22 Sep - 19 Oct)"


# --- what to look at first ----------------------------------------------------------


def test_stale_is_worst_first_and_never_sorts_above_a_number() -> None:
    """'Never' is not a very large number of days. Ordered as one it ends up
    behind whichever machine happens to be oldest."""
    week = device("0" * 28, key="a-week", last_success_days=7)
    year = device("0" * 28, key="a-year", last_success_days=365)
    none = device("0" * 28, key="never", last_success_days=None)
    fresh = device("5" * 28, key="fresh", last_success_days=0)
    found = backup.stale([week, year, none, fresh], now=NOW)
    assert [a.device.key for a in found] == ["never", "a-year", "a-week"]
    assert found[0].never is True
    assert found[1].days_since_success == 365


def test_a_device_inside_the_window_is_not_stale() -> None:
    assert backup.stale([device("5" * 28, last_success_days=2)], now=NOW) == []


def test_worst_days_finds_the_device_that_fails_without_going_stale() -> None:
    """It backs up most nights, so it never reaches the stale list, and it
    is the one to fix before it does."""
    flaky = device("5252525252" + "5" * 18, key="flaky", last_success_days=0)
    steady = device("5" * 28, key="steady", last_success_days=0)
    found = backup.worst_days([flaky, steady], now=NOW)
    assert [a.device.key for a in found] == ["flaky"]
    assert found[0].rate.bad == 5


def test_a_device_with_nothing_wrong_is_not_on_either_list() -> None:
    clean = [device("5" * 28, last_success_days=0)]
    assert backup.stale(clean, now=NOW) == []
    assert backup.worst_days(clean, now=NOW) == []


# --- the word beside a device -------------------------------------------------------


@pytest.mark.parametrize(
    ("bar", "since", "expected"),
    [
        ("5" * 28, 0, "ok"),
        ("8" + "5" * 27, 0, "warnings"),
        ("2" + "5" * 27, 0, "failed"),
        ("0" * 4 + "5" * 24, 4, "failed"),  # nothing has succeeded for four days
        ("0" * 28, 9, "not_backed_up"),
        ("0" * 28, None, "not_backed_up"),
    ],
)
def test_the_status_word(bar: str, since: int | None, expected: str) -> None:
    assert backup.status_of(device(bar, last_success_days=since), now=NOW) == expected


def test_the_status_is_where_a_machine_stands_now() -> None:
    """A machine that failed a fortnight ago and has backed up every night
    since is backed up. The month's failures are history, and history is a
    column of its own on the device table rather than a word that says the
    wrong thing about today."""
    recovered = device("5" * 20 + "2" + "5" * 7, last_success_days=0)
    assert backup.rate_for(recovered, now=NOW).failures == 1
    assert backup.status_of(recovered, now=NOW) == "ok"


def test_not_backed_up_is_not_the_same_claim_as_failed() -> None:
    """One machine's backup ran and broke; nothing has run on the other for
    over a week. They want different people."""
    broke = device("2" + "5" * 27, last_success_days=0)
    quiet = device("0" * 28, last_success_days=30)
    assert backup.status_of(broke, now=NOW) == "failed"
    assert backup.status_of(quiet, now=NOW) == "not_backed_up"


# --- which machines count as a gap --------------------------------------------------


def workstation(key: str = "a-laptop") -> CoveDevice:
    return CoveDevice(
        account_id=1,
        partner_id=2,
        name=key,
        key=key,
        os_type="workstation",
        account_type="backup_manager",
    )


def server(key: str = "a-server") -> CoveDevice:
    return CoveDevice(
        account_id=2,
        partner_id=2,
        name=key,
        key=key,
        os_type="server",
        account_type="backup_manager",
    )


def test_auto_asks_how_many_workstations_are_backed_up_not_whether_any_is() -> None:
    """One backed-up laptop is an exception; half of them is a policy.

    The rule used to be "any", and on the pilot one laptop at a 107-machine
    customer turned the other 106 into gaps reported every month: their Backup
    coverage read 0.9%, which said more about the rule than about them.
    """
    held = [server(), workstation()]
    # One of two: a policy.
    scope, why, mixed = backup.effective_scope("auto", machines=held, workstations=2)
    assert scope == "servers_and_workstations"
    assert why == "Servers and workstations, as 1 of this site's 2 workstations are backed up."
    assert mixed is False
    # One of forty: an exception, and the other thirty-nine are not gaps —
    # and `mixed`, because the rule has guessed and the guess is what took
    # those thirty-nine off the gap list. Somebody should settle it.
    scope, why, mixed = backup.effective_scope("auto", machines=held, workstations=40)
    assert scope == "servers"
    assert "reads as an exception rather than a policy" in why
    assert "1 of this site's 40 workstations" in why
    assert mixed is True


def test_auto_without_a_denominator_keeps_the_older_rule_and_says_so() -> None:
    """A caller that cannot count the site's workstations still gets an answer.

    Better a rule a reader can see in the sentence than a share of an unknown.
    """
    scope, why, mixed = backup.effective_scope("auto", machines=[server(), workstation()])
    assert scope == "servers_and_workstations"
    assert why == "Servers and workstations, as workstations are backed up here."
    # Nothing to confirm either: the rule did not narrow anything here.
    assert mixed is False


def test_auto_counts_servers_only_where_cove_holds_no_workstation() -> None:
    """Otherwise every laptop at the site is a finding nobody asked for,
    every month, which is how a list stops being read."""
    scope, why, mixed = backup.effective_scope("auto", machines=[server(), server("another")])
    assert scope == "servers"
    assert why == "Servers only, as no workstations are backed up here."
    # Nothing to confirm: nobody is backing up a workstation here at all.
    assert mixed is False


def test_auto_on_a_site_with_no_cove_machines_at_all_is_servers_only() -> None:
    scope, why, mixed = backup.effective_scope("auto", machines=[])
    assert scope == "servers"
    assert "no workstations are backed up" in why
    assert mixed is False


@pytest.mark.parametrize(
    ("setting", "expected"),
    [("servers", "servers"), ("servers_and_workstations", "servers_and_workstations")],
)
def test_an_explicit_setting_beats_what_cove_happens_to_hold(setting: str, expected: str) -> None:
    """The customer who has bought workstation backup and not installed it
    yet is exactly the case auto gets wrong, and the case where the gap list
    matters most. Both directions are overridable."""
    machines = [server(), workstation()] if setting == "servers" else [server()]
    scope, why, mixed = backup.effective_scope(setting, machines=machines)
    assert scope == expected
    assert why.endswith("by this site's setting.")
    # A setting is somebody's decision, so there is never anything to confirm.
    assert mixed is False


def test_the_setting_reads_back_per_site_and_defaults_to_auto() -> None:
    config = BackupConfig(scope={"bay-associates": "servers"})
    assert config.scope_for("bay-associates") == "servers"
    assert config.scope_for("somebody-else") == "auto"
    # A value nobody recognises is auto rather than a crash or a silent "all".
    assert BackupConfig(scope={"x": "everything"}).scope_for("x") == "auto"


# --- the headline may not call a retired machine protected --------------------------


def test_a_site_whose_only_machine_is_retired_does_not_claim_it_is_protected() -> None:
    said = backup.headline([], [], retired=1)
    assert said == "Nothing is currently backed up; 1 possibly retired"


def test_the_headline_counts_what_is_live_and_names_what_is_not() -> None:
    assert backup.headline([server()], [], retired=2) == "1 server protected; 2 possibly retired"
    assert backup.headline([server(), server("b")], []) == "2 servers protected"
    assert backup.headline([server(), workstation()], []) == "2 computers protected"


def test_a_tenant_only_site_says_so() -> None:
    tenant = CoveDevice(account_id=3, partner_id=2, name="t", key="t", account_type="microsoft365")
    assert backup.headline([], [tenant]) == "Microsoft 365 protected"
    assert backup.headline([server()], [tenant]) == "1 server + Microsoft 365 protected"


# --- when auto changes its mind -----------------------------------------------------


class _Run:
    """Just enough Collector for the scope note: what it remembers, and the
    facts it is told to record."""

    def __init__(self, previous: str | None) -> None:
        self.previous_backup_scope = previous
        self.recorded: dict[str, Any] = {}

    def fact(self, fact_id: str, label: str, value: Any, definition: str, source: str, **kw: Any):
        self.recorded[fact_id] = {"value": value, "display": kw.get("display"), **kw}
        return None


def test_nothing_is_said_when_the_rule_did_not_change() -> None:
    run = _Run("servers")
    backup._scope_changed(run, scope="servers", why="Servers only, by this site's setting.")
    assert run.recorded == {}


def test_nothing_is_said_on_a_site_with_no_previous_review() -> None:
    run = _Run(None)
    backup._scope_changed(run, scope="servers", why="Servers only, as none are backed up here.")
    assert run.recorded == {}


def test_a_changed_rule_is_said_once_and_only_to_us() -> None:
    """`auto` can change its mind between two reviews without anybody
    touching a setting, and a coverage figure that moved for that reason is
    not a figure that moved."""
    run = _Run("servers")
    backup._scope_changed(
        run,
        scope="servers_and_workstations",
        why="Servers and workstations, as workstations are backed up here.",
    )
    said = run.recorded["cove.coverage.scope_changed"]
    assert said["internal_only"] is True
    assert "last review counted servers only" in said["display"]
    assert "this one counts servers and workstations" in said["display"]
    assert "may have moved for this reason" in said["display"]


class _Role:
    """`ninja_roles`' answer: what a machine is, and what decided it."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.basis = "node class"


class _Device:
    def __init__(self, device_id: int) -> None:
        self.id = device_id


class _ScopeRun(_Run):
    """`_Run` with the two things `_gap` reads beyond the facts it records."""

    def __init__(self, workstations: int) -> None:
        super().__init__(None)
        self.config = SimpleNamespace(backup=BackupConfig())
        self.site_slug = "a-customer"
        self.ninja_devices = [(None, _Device(n)) for n in range(workstations)]
        self.ninja_roles = {n: _Role("workstation") for n in range(workstations)}
        self.tables: dict[str, Any] = {}

    def table(self, *args: Any, **kw: Any) -> None:
        return None


def test_a_guessed_scope_is_recorded_for_us_and_never_for_a_customer() -> None:
    """ "Our rule is unsure about your estate" is a sentence about our rule.

    A site where somebody backs up one or two workstations and not the rest
    is one `auto` has guessed at, and the guess is what takes the remaining
    laptops off the gap list. Somebody should settle it with one setting — so
    the board and the Ask tools say so, and the fact that carries it is
    `internal_only`, which is what keeps it out of a client deck and a client
    PDF by construction rather than by remembering.
    """
    run = _ScopeRun(workstations=40)
    backup._gap(run, gaps=[], covered=1, machines=[server(), workstation()])

    said = run.recorded["cove.coverage.scope_mixed"]
    assert said["internal_only"] is True
    assert said["display"] == "Confirm this site's backup scope"
    # And the scope it qualifies says which way the rule went, with both counts.
    scope = run.recorded["cove.coverage.scope"]
    assert scope["value"] == "servers"
    assert "1 of this site's 40 workstations" in scope["display"]

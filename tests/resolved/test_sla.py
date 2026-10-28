"""The settings, and the five things a clock can be.

What is asserted here is the two refusals as much as the arithmetic: a paused
clock and a priority with no target are reported and never judged, because a
breach we cannot defend is worse than no breach count at all.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.reports.tbr.config import BusinessHours
from app.resolved.baselines import percentile
from app.resolved.config import (
    ClassificationConfig,
    CostConfig,
    ResolvedConfig,
    ResolverConfig,
    SlaConfig,
    SlaTarget,
    TicketClassRule,
)
from app.resolved.sla import SlaCalculator

CHICAGO = dt.timezone(-dt.timedelta(hours=6))
HOURS = BusinessHours(timezone="UTC-06:00", start=dt.time(9), end=dt.time(17))


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    """A local moment in September 2026. The 14th is a Monday."""
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=CHICAGO)


def ticket(**over: Any) -> Any:
    fields: dict[str, Any] = {
        "create_date": at(14, 9),
        "first_response_at": None,
        "completed_date": None,
    }
    fields.update(over)
    return SimpleNamespace(**fields)


def calculator(**over: Any) -> SlaCalculator:
    sla = SlaConfig(
        targets={
            "High": SlaTarget(first_response_minutes=60, resolution_minutes=480),
            "Low": SlaTarget(first_response_minutes=480),
        },
        paused_status_labels=["Waiting Customer"],
        **over,
    )
    return SlaCalculator(ResolvedConfig(sla=sla), HOURS, CHICAGO)


# --- the settings -------------------------------------------------------------------------


def test_a_target_is_found_by_label_whatever_its_case() -> None:
    config = SlaConfig(targets={"High": SlaTarget(first_response_minutes=60)})
    assert config.target_for("high") is not None
    assert config.target_for("  HIGH ") is not None
    assert config.target_for("Critical") is None


def test_a_priority_with_no_entry_falls_back_to_the_default() -> None:
    config = SlaConfig(
        targets={"High": SlaTarget(first_response_minutes=60)},
        default=SlaTarget(first_response_minutes=999),
    )
    found = config.target_for("Anything")
    assert found is not None
    assert found.first_response_minutes == 999


def test_pausing_statuses_are_matched_by_label() -> None:
    config = SlaConfig(paused_status_labels=["Waiting Customer", "Waiting Vendor"])
    assert config.pauses("waiting customer") is True
    assert config.pauses("Waiting Vendor") is True
    assert config.pauses("In Progress") is False
    assert config.pauses(None) is False


def test_the_first_matching_class_rule_wins() -> None:
    rules = ClassificationConfig(
        rules=[
            TicketClassRule(ticket_class="L3", priority=["Critical"]),
            TicketClassRule(ticket_class="L1", queue=["Monitoring Alerts"]),
        ],
        default="L2",
    )
    # Critical matches the first rule even though the queue matches the second.
    assert rules.classify(priority="Critical", queue="Monitoring Alerts", issue_type=None) == "L3"
    assert rules.classify(priority="Low", queue="Monitoring Alerts", issue_type=None) == "L1"
    assert rules.classify(priority="Low", queue="Help Desk", issue_type=None) == "L2"


def test_a_rule_naming_two_fields_needs_both() -> None:
    rules = ClassificationConfig(
        rules=[TicketClassRule(ticket_class="L3", priority=["High"], queue=["Escalation"])],
        default="L1",
    )
    assert rules.classify(priority="High", queue="Escalation", issue_type=None) == "L3"
    assert rules.classify(priority="High", queue="Help Desk", issue_type=None) == "L1"


def test_a_resolver_is_ours_by_any_of_the_names_a_tenant_knows_them_by() -> None:
    config = ResolverConfig(inputiv=["Sam Okafor", "dwhitfield@inputiv.test"])
    assert config.side("Sam Okafor", "sokafor", "sam@x.test") == "inputiv"
    assert config.side("Dana Whitfield", "dwhit", "dwhitfield@inputiv.test") == "inputiv"
    assert config.side("Priya Raman", "praman", "priya@x.test") == "franchisee"


def test_a_ticket_with_nobody_on_it_is_not_quietly_given_to_a_side() -> None:
    assert ResolverConfig(inputiv=["Sam Okafor"]).side(None, None) == "unknown"


def test_an_engineer_with_no_rate_falls_back_and_then_to_nothing() -> None:
    book = CostConfig(default_hourly=Decimal("60"), by_resource={"Sam Okafor": Decimal("48")})
    assert book.hourly_for("Sam Okafor") == Decimal("48")
    assert book.hourly_for("Priya Raman") == Decimal("60")
    assert CostConfig().hourly_for("Anybody") is None


# --- the clocks --------------------------------------------------------------------------


def test_a_ticket_answered_inside_its_target_is_met() -> None:
    verdict = calculator().assess(
        ticket(first_response_at=at(14, 9, 45)),
        priority_label="High",
        status_label="In Progress",
        now=at(14, 12),
    )
    assert verdict.first_response.state == "met"
    assert verdict.first_response.used_minutes == 45
    assert verdict.first_response.met is True


def test_a_ticket_answered_late_is_breached() -> None:
    verdict = calculator().assess(
        ticket(first_response_at=at(14, 11)),
        priority_label="High",
        status_label="In Progress",
        now=at(14, 12),
    )
    assert verdict.first_response.state == "breached"
    assert verdict.first_response.used_minutes == 120
    assert verdict.first_response.met is False


def test_an_unanswered_ticket_inside_its_target_is_still_running() -> None:
    verdict = calculator().assess(
        ticket(), priority_label="High", status_label="In Progress", now=at(14, 9, 30)
    )
    assert verdict.first_response.state == "running"
    assert verdict.first_response.met is None
    assert verdict.state in ("ok", "due_soon")


def test_overnight_does_not_breach_a_one_hour_target() -> None:
    """Raised at 16:30, still unanswered at 09:15 the next morning: forty-five
    working minutes, not seventeen hours."""
    verdict = calculator().assess(
        ticket(create_date=at(14, 16, 30)),
        priority_label="High",
        status_label="In Progress",
        now=at(15, 9, 15),
    )
    assert verdict.first_response.used_minutes == 45
    assert verdict.first_response.state == "running"


def test_a_paused_ticket_is_not_judged() -> None:
    """Waiting on the customer for two working days against a one-hour target,
    but still under it when it stopped. We cannot say it breached."""
    verdict = calculator().assess(
        ticket(create_date=at(14, 16, 45)),
        priority_label="High",
        status_label="Waiting Customer",
        now=at(15, 9, 30),
    )
    assert verdict.first_response.state == "paused"
    assert verdict.first_response.met is None
    assert verdict.state == "paused"


def test_pausing_cannot_undo_a_clock_that_has_already_run_out() -> None:
    """The distinction that keeps the count honest in the other direction: a
    target used up before the ticket was parked is a breach that happened."""
    verdict = calculator().assess(
        ticket(create_date=at(14, 9)),
        priority_label="High",
        status_label="Waiting Customer",
        now=at(14, 15),
    )
    assert verdict.first_response.state == "breached"
    assert verdict.state == "breached"


def test_a_priority_with_no_target_is_never_met_and_never_breached() -> None:
    verdict = calculator().assess(
        ticket(create_date=at(14, 9)),
        priority_label="Critical",
        status_label="In Progress",
        now=at(21, 9),
    )
    assert verdict.first_response.state == "no_target"
    assert verdict.first_response.met is None
    assert verdict.resolution.met is None
    assert verdict.state == "none"


def test_a_target_set_for_one_clock_and_not_the_other() -> None:
    """Low has a first-response target and no resolution target. One is judged
    and the other is not, rather than both or neither."""
    verdict = calculator().assess(
        ticket(first_response_at=at(14, 10), create_date=at(14, 9)),
        priority_label="Low",
        status_label="In Progress",
        now=at(21, 9),
    )
    assert verdict.first_response.state == "met"
    assert verdict.resolution.state == "no_target"


def test_a_completed_ticket_is_judged_on_when_it_completed() -> None:
    verdict = calculator().assess(
        ticket(first_response_at=at(14, 9, 30), completed_date=at(15, 12)),
        priority_label="High",
        status_label="Complete",
        now=at(21, 9),
    )
    # 09:00 Monday to 12:00 Tuesday is eight plus three working hours: 660.
    assert verdict.resolution.used_minutes == 660
    assert verdict.resolution.state == "breached"  # against a 480-minute target
    assert verdict.first_response.state == "met"


def test_the_headline_is_the_worse_of_the_live_clocks() -> None:
    """Answered late but still open and inside its resolution target: the row
    says where it stands now, and the missed response is in the detail."""
    verdict = calculator().assess(
        ticket(first_response_at=at(14, 11)),
        priority_label="High",
        status_label="In Progress",
        now=at(14, 12),
    )
    assert verdict.first_response.state == "breached"
    assert verdict.first_response.settled is True
    assert verdict.resolution.state == "running"
    # The badge follows the clock that is still running — here the resolution
    # target, due tomorrow lunchtime, so within a day of now.
    assert verdict.state == "due_soon"
    # And the missed response is still a miss for every percentage taken later.
    assert verdict.first_response.met is False


def test_the_json_shape_the_pages_read() -> None:
    payload = (
        calculator()
        .assess(ticket(), priority_label="High", status_label="In Progress", now=at(14, 10))
        .as_json()
    )
    assert set(payload) == {"state", "detail", "due_at", "clocks"}
    assert set(payload["clocks"]) == {"first_response", "resolution"}
    assert payload["detail"]


# --- percentiles ----------------------------------------------------------------------------


def test_a_percentile_names_a_real_value() -> None:
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(values, 0.5) == 50
    assert percentile(values, 0.9) == 90
    assert percentile(values, 1.0) == 100


def test_a_percentile_of_nothing_is_nothing() -> None:
    assert percentile([], 0.5) is None


def test_a_percentile_of_one_ticket_is_that_ticket() -> None:
    assert percentile([42], 0.9) == 42

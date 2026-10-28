"""The cheap paths: the rules that answer without a model, and the grouping.

Both are deterministic, so these are the tests that pin the behaviour the
replay's numbers rest on — above all that grouping cannot look forwards, which
is the one way a replay could flatter itself.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.autopilot.shadow.cost import Budget
from app.autopilot.shadow.grouping import Grouper, normalise
from app.autopilot.shadow.rules import apply_rules, rules_for

TYPES = ["Application/Software", "Backup/Restore", "Cybersecurity", "Email", "Server"]
RULES = rules_for(TYPES)
NOW = dt.datetime(2026, 9, 16, 12, tzinfo=dt.UTC)


def at(day: int, hour: int = 9) -> dt.datetime:
    return dt.datetime(2026, 9, day, hour, tzinfo=dt.UTC)


# --- the rules ------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "rule"),
    [
        ("Backup job failed on SRV-01", "backup_job_report"),
        ("Veeam backup failure overnight", "backup_job_report"),
        ("Backup alert: repository full", "backup_job_report"),
        ("Alert: disk space low on C:", "monitoring_threshold"),
        ("CPU threshold exceeded on SRV-02", "monitoring_threshold"),
        ("Service stopped: print spooler", "monitoring_threshold"),
        ("Host unreachable", "monitoring_threshold"),
        ("Threat detected and quarantined", "security_automation"),
        ("Patch installation failed", "security_automation"),
    ],
)
def test_a_machine_wrote_this_one(title: str, rule: str) -> None:
    answer = apply_rules(title, RULES)
    assert answer is not None
    assert answer.rule_name == rule
    assert answer.origin == "alert"


@pytest.mark.parametrize(
    "title",
    [
        "Outlook will not open",
        "Password reset for a user",
        "Can we add a new starter to the shared mailbox",
        "Printer jam again",
        "Laptop very slow since yesterday",
        "please call me about the invoice",
        "Need a backup of my documents before the upgrade",  # a person, about backup
        "disk space question",  # a question, not a threshold
    ],
)
def test_a_person_wrote_this_one_so_the_model_decides(title: str) -> None:
    """The bar for a rule is near-certainty. Anything a person might plausibly
    have typed goes to the model, because a rule's mistakes arrive with the
    authority of a certainty and never reach the model that might catch them."""
    assert apply_rules(title, RULES) is None


def test_a_rule_asserts_a_type_only_where_the_tenant_has_the_label() -> None:
    """Every picklist is per tenant, so a rule that hard-coded
    `Backup/Restore` would be quietly wrong at the next franchisee."""
    without = rules_for(["Email", "Server"])
    answer = apply_rules("Backup job failed on SRV-01", without)
    assert answer is not None
    assert answer.origin == "alert"  # still sure a machine wrote it
    assert answer.issue_type == "unsure"  # and no opinion on the category
    assert answer.issue_type_confidence == 0.0


def test_the_more_specific_rule_claims_a_line_both_could() -> None:
    """A backup job's failure notice also says 'failed'."""
    answer = apply_rules("Backup job failed: service stopped", RULES)
    assert answer is not None
    assert answer.rule_name == "backup_job_report"


# --- grouping -------------------------------------------------------------


def test_the_same_alert_twice_at_one_site_is_one_group() -> None:
    grouper = Grouper(window=dt.timedelta(days=30), similarity=0.7)
    assert (
        grouper.consider(
            ticket_id="1", site_id="a", created_at=at(1), title="Disk space low on SRV-01"
        )
        is None
    )
    second = grouper.consider(
        ticket_id="2", site_id="a", created_at=at(2), title="Disk space low on SRV-04"
    )
    assert second is not None
    assert second.ticket_id == "1"
    assert second.basis == "same_site_similar_title"
    assert second.score >= 0.7


def test_the_same_alert_at_two_sites_is_two_groups() -> None:
    """Two customers running the same monitoring tool are not one incident —
    the same rule the coverage join follows for hostnames."""
    grouper = Grouper(window=dt.timedelta(days=30), similarity=0.7)
    grouper.consider(ticket_id="1", site_id="a", created_at=at(1), title="Disk space low")
    assert (
        grouper.consider(ticket_id="2", site_id="b", created_at=at(1), title="Disk space low")
        is None
    )


def test_grouping_never_looks_forwards() -> None:
    """The one way a replay could flatter itself.

    Tickets arrive in creation order and each is grouped against what came
    before it, so the first of a pair opens the group and the second joins it —
    never the other way round.
    """
    grouper = Grouper(window=dt.timedelta(days=30), similarity=0.7)
    first = grouper.consider(
        ticket_id="1", site_id="a", created_at=at(1), title="Exchange mail flow stopped"
    )
    second = grouper.consider(
        ticket_id="2", site_id="a", created_at=at(2), title="Exchange mail flow stopped"
    )
    assert first is None  # nothing earlier existed
    assert second is not None and second.ticket_id == "1"


def test_outside_the_window_is_a_new_group() -> None:
    grouper = Grouper(window=dt.timedelta(days=7), similarity=0.7)
    grouper.consider(ticket_id="1", site_id="a", created_at=at(1), title="Printer offline")
    assert (
        grouper.consider(ticket_id="2", site_id="a", created_at=at(20), title="Printer offline")
        is None
    )


def test_different_problems_at_one_site_stay_apart() -> None:
    grouper = Grouper(window=dt.timedelta(days=30), similarity=0.7)
    grouper.consider(ticket_id="1", site_id="a", created_at=at(1), title="Printer jam")
    assert (
        grouper.consider(
            ticket_id="2", site_id="a", created_at=at(1, 11), title="Outlook will not open"
        )
        is None
    )


def test_two_hostnames_of_one_problem_group_at_any_threshold() -> None:
    """`SRV-01` and `SRV-04` normalise to the same line, so the pair scores 1.0
    and no threshold separates them. That is the normalisation earning its
    place: one fault across two servers is one incident."""
    pair = ("Server SRV-01 not responding", "Server SRV-02 not responding")
    strict = Grouper(window=dt.timedelta(days=30), similarity=0.99)
    strict.consider(ticket_id="1", site_id="a", created_at=at(1), title=pair[0])
    joined = strict.consider(ticket_id="2", site_id="a", created_at=at(1, 10), title=pair[1])
    assert joined is not None
    assert joined.score == 1.0


def test_the_threshold_is_what_decides_one_ticket_or_eleven() -> None:
    """Loosen it and more joins; the knob is on the run's config for exactly
    this reason, so a number that moves can be explained.

    This pair scores 0.776 — the same words about two different places — which
    is the judgement call the threshold exists to make.
    """
    pair = ("Printer offline in accounts", "Printer offline in reception")
    strict = Grouper(window=dt.timedelta(days=30), similarity=0.9)
    loose = Grouper(window=dt.timedelta(days=30), similarity=0.7)
    for grouper in (strict, loose):
        grouper.consider(ticket_id="1", site_id="a", created_at=at(1), title=pair[0])
    assert strict.consider(ticket_id="2", site_id="a", created_at=at(1, 10), title=pair[1]) is None
    assert loose.consider(ticket_id="2", site_id="a", created_at=at(1, 10), title=pair[1])


def test_normalising_drops_the_digits_that_make_one_problem_look_like_many() -> None:
    first, tokens = normalise("Disk space low on SRV-01 at 95%")
    second, _ = normalise("Disk space low on SRV-04 at 92%")
    assert first == second
    assert "disk" in tokens and "space" in tokens
    assert all(not token.isdigit() for token in tokens)


# --- the budget -----------------------------------------------------------


def test_an_unpriced_run_reports_no_cost_rather_than_zero() -> None:
    """A cost from a rate nobody set is worse than no cost, and worse in the
    specific way that it looks like an answer."""
    budget = Budget(max_tokens=1000, max_usd=None, input_per_mtok=None, output_per_mtok=None)
    assert budget.priced is False
    assert budget.price(input_tokens=500_000, output_tokens=100_000) is None


def test_the_token_cap_bites_whether_or_not_anybody_priced_it() -> None:
    budget = Budget(max_tokens=1000, max_usd=None, input_per_mtok=None, output_per_mtok=None)
    assert budget.exceeded(input_tokens=400, output_tokens=400) is None
    assert budget.exceeded(input_tokens=900, output_tokens=200) == "token_cap"


def test_the_cost_cap_bites_once_prices_are_set() -> None:
    budget = Budget(max_tokens=10_000_000, max_usd=1.0, input_per_mtok=3.0, output_per_mtok=15.0)
    assert budget.price(input_tokens=1_000_000, output_tokens=0) == pytest.approx(3.0)
    # 300,000 input at $3 is $0.90, and 10,000 output at $15 is $0.15: $1.05.
    assert budget.exceeded(input_tokens=300_000, output_tokens=10_000) == "cost_cap"
    # A tenth of that is nowhere near it.
    assert budget.exceeded(input_tokens=30_000, output_tokens=1_000) is None

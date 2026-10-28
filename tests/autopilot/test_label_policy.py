"""One franchisee's label policy: what the agent may say about their tickets.

The policy exists because of a measured fault. The pilot's picklist offers
`Customer Onboarding Offboarding`; they filed nine tickets under it in ninety
days and file that work as `User Access and Management`. Offered the label, the
agent reached for it 219 times — 18% of every wrong answer, and six of the
eight most confident ones (validation log, 2026-10-01).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.autopilot.config import AutopilotConfig
from app.reports.tbr.config import FranchiseeConfig

ONBOARDING = "Customer Onboarding Offboarding"
USER_ACCESS = "User Access and Management"


def test_no_policy_is_the_default_and_changes_nothing() -> None:
    """A replay with no policy is the honest baseline, so nothing is assumed."""
    policy = AutopilotConfig()
    assert policy.configured is False
    assert policy.apply("Email") == ("Email", "kept")
    assert policy.apply(None) == (None, "kept")
    assert policy.apply("unsure") == ("unsure", "kept")


def test_a_remap_says_what_this_franchisee_files_it_as() -> None:
    policy = AutopilotConfig(issue_type_remap={ONBOARDING: USER_ACCESS})
    assert policy.apply(ONBOARDING) == (USER_ACCESS, "remapped")
    assert policy.apply("Email") == ("Email", "kept")


def test_a_remap_matches_whatever_case_the_model_used() -> None:
    policy = AutopilotConfig(issue_type_remap={ONBOARDING: USER_ACCESS})
    assert policy.apply(ONBOARDING.lower())[0] == USER_ACCESS
    assert policy.apply(ONBOARDING.upper())[1] == "remapped"


def test_a_forbidden_label_becomes_a_decline_not_a_guess() -> None:
    """`unsure` is a decline and is scored as one: the agent is being told it
    may not answer this, not told to answer differently."""
    policy = AutopilotConfig(forbidden_issue_types=[ONBOARDING])
    assert policy.apply(ONBOARDING) == ("unsure", "forbidden")
    assert policy.apply("Email") == ("Email", "kept")


def test_remap_runs_before_the_forbidden_check() -> None:
    """`A -> B` with `A` forbidden means "we file A as B", not "we decline A".

    The other order would make the pair useless: every remapped answer would
    turn into a decline on the way out.
    """
    policy = AutopilotConfig(
        forbidden_issue_types=[ONBOARDING], issue_type_remap={ONBOARDING: USER_ACCESS}
    )
    assert policy.apply(ONBOARDING) == (USER_ACCESS, "remapped")


def test_a_remap_pointing_at_a_forbidden_label_is_refused() -> None:
    """It would quietly turn a good answer into a decline, so it is not a
    configuration anybody gets to save."""
    with pytest.raises(ValidationError, match="forbidden label"):
        AutopilotConfig(
            forbidden_issue_types=[USER_ACCESS], issue_type_remap={ONBOARDING: USER_ACCESS}
        )


def test_the_policy_rides_on_the_franchisees_own_settings() -> None:
    """One franchisee's settings, in the row that already holds them."""
    config = FranchiseeConfig(
        display_name="Acme IT",
        autopilot=AutopilotConfig(issue_type_remap={ONBOARDING: USER_ACCESS}),
    )
    restored = FranchiseeConfig.model_validate(config.model_dump(mode="json"))
    assert restored.autopilot.apply(ONBOARDING) == (USER_ACCESS, "remapped")
    assert restored.autopilot.as_json()["issue_type_remap"] == {ONBOARDING: USER_ACCESS}


def test_blank_entries_are_dropped_rather_than_matched() -> None:
    policy = AutopilotConfig(forbidden_issue_types=["", "   ", ONBOARDING])
    assert policy.forbidden_issue_types == [ONBOARDING]
    assert policy.apply("")[0] == ""  # an empty answer is not forbidden by a blank

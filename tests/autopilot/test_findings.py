"""Which findings a snapshot produces, in which order, and what to do about it.

The order is the design: the card shows four and the queue shows one, so a
finding that ranks wrongly is a finding nobody sees. Everything here is pure —
a dict of facts in, sentences out — which is also what lets the results page
count how often a kind was shown.
"""

from __future__ import annotations

import datetime as dt

from app.autopilot.findings import (
    MAX_SHOWN,
    OFFLINE_DAYS,
    Finding,
    TicketFacts,
    findings_for,
    next_step_for,
    tone_for,
)

NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.UTC)


def facts(**over: object) -> dict[str, object]:
    return dict(over)


def kinds(found: list[Finding]) -> list[str]:
    return [one.kind for one in found]


# --- the ranking --------------------------------------------------------------


def test_the_machine_being_absent_outranks_everything_else() -> None:
    """Nothing remote works on a machine that is not there, so it is said first
    however many other things are true."""
    found = findings_for(
        facts(
            **{
                "device.needs_reboot": True,
                "alerts.open": 3,
                "device.last_contact": (NOW - dt.timedelta(days=10)).isoformat(),
                "findings.critical": 7,
            }
        ),
        now=NOW,
    )
    assert kinds(found)[0] == "device_offline"
    assert found[0].text == "Machine offline for 10 days"
    assert found[0].tone == "warning"
    assert found[0].source == "NinjaOne"


def test_the_order_is_the_six_groups_in_turn() -> None:
    found = findings_for(
        facts(
            **{
                "device.last_contact": (NOW - dt.timedelta(days=10)).isoformat(),
                "alerts.open": 3,
                "tickets.device": 2,
                "device.needs_reboot": True,
            }
        ),
        ticket=TicketFacts(created_at=NOW - dt.timedelta(days=14), origin="alert"),
        now=NOW,
    )
    assert kinds(found) == [
        "device_offline",
        "alert_stale",
        "open_alerts",
        "other_tickets_device",
        "pending_reboot",
    ]
    assert [one.rank for one in found] == [1, 2, 4, 5, 6]


def test_a_machine_seen_yesterday_is_not_a_finding() -> None:
    """Three days, because a laptop that was off overnight is not a story."""
    found = findings_for(
        facts(**{"device.last_contact": (NOW - dt.timedelta(days=1)).isoformat()}), now=NOW
    )
    assert found == []


def test_a_fresh_ticket_is_not_stale() -> None:
    found = findings_for(
        facts(), ticket=TicketFacts(created_at=NOW - dt.timedelta(days=2)), now=NOW
    )
    assert found == []


# --- the sentences ------------------------------------------------------------


def test_an_alert_older_than_the_outage_says_so() -> None:
    """The comparison an engineer would make themselves: an alert raised before
    the machine went quiet may be about the thing that made it go quiet."""
    found = findings_for(
        facts(**{"device.last_contact": (NOW - dt.timedelta(days=10)).isoformat()}),
        ticket=TicketFacts(created_at=NOW - dt.timedelta(days=14), origin="alert"),
        now=NOW,
    )
    stale = next(one for one in found if one.kind == "alert_stale")
    assert stale.text == "Alert is 2 weeks old, raised before the machine went offline"
    # The line beneath a finding says what the sentence has not: when, here.
    assert stale.detail == "raised 18 Sep"


def test_a_ticket_a_person_raised_is_called_a_ticket() -> None:
    found = findings_for(
        facts(), ticket=TicketFacts(created_at=NOW - dt.timedelta(days=30), origin="human"), now=NOW
    )
    assert kinds(found) == ["ticket_stale"]
    assert found[0].text == "Ticket is 4 weeks old"


def test_out_of_date_signatures_say_they_are_expected_while_offline() -> None:
    """Without the qualifier an engineer chases a thing that cannot be true: a
    machine that is not there has not updated anything."""
    offline = findings_for(
        facts(
            **{
                "device.last_contact": (NOW - dt.timedelta(days=10)).isoformat(),
                "protection.signatures_outdated": True,
            }
        ),
        now=NOW,
    )
    assert next(one for one in offline if one.kind == "signatures_outdated").detail == (
        "expected while offline"
    )
    alone = findings_for(facts(**{"protection.signatures_outdated": True}), now=NOW)
    assert alone[0].detail is None


def test_a_repeat_links_the_earlier_ticket() -> None:
    found = findings_for(
        facts(),
        ticket=TicketFacts(
            repeat_of="70001",
            repeat_of_number="T20260814.0031",
            repeat_at=NOW - dt.timedelta(days=3),
        ),
        now=NOW,
    )
    repeat = found[0]
    assert repeat.kind == "repeat_machine"
    assert repeat.tone == "repeat"
    assert repeat.detail == "3 days ago"
    assert repeat.tickets == [{"id": "70001", "number": "T20260814.0031"}]


def test_every_finding_carries_a_tool_and_the_time_it_was_read() -> None:
    """A fact with no source is a claim, and an engineer who cannot tell where
    it came from has to go and look anyway."""
    read_at = NOW - dt.timedelta(minutes=5)
    found = findings_for(
        facts(**{"alerts.open": 2, "findings.critical": 1, "device.needs_reboot": True}),
        read_at=read_at,
        now=NOW,
    )
    assert {one.source for one in found} == {"NinjaOne", "ConnectSecure"}
    assert {one.as_of for one in found} == {read_at.isoformat()}


def test_the_queue_chip_is_short_enough_for_a_row() -> None:
    found = findings_for(
        facts(**{"device.last_contact": (NOW - dt.timedelta(days=10)).isoformat()}), now=NOW
    )
    assert found[0].chip == "Offline 10 days"
    assert len(found[0].chip) < 24


def test_a_count_of_zero_or_a_flag_that_is_false_says_nothing() -> None:
    found = findings_for(
        facts(**{"alerts.open": 0, "device.needs_reboot": False, "patches.failed": 0}), now=NOW
    )
    assert found == []


def test_a_missing_protection_agent_is_a_finding_because_it_is_missing() -> None:
    found = findings_for(facts(**{"protection.agent": False}), now=NOW)
    assert kinds(found) == ["no_protection_agent"]


def test_a_list_valued_fact_is_counted_from_its_length() -> None:
    """The snapshot keeps a list as `{type, length}`: the values were names.

    And the count reads as English — "1 volume", "2 volumes". A card that says
    "1 alerts" looks generated, and an engineer trusts it accordingly.
    """
    found = findings_for(facts(**{"device.disks_low": {"type": "list", "length": 2}}), now=NOW)
    assert found[0].text == "2 volumes under 10% free"


def test_more_than_four_findings_still_all_come_back_ranked() -> None:
    """The card shows `MAX_SHOWN` and offers the rest; the cut is the page's
    decision and not this module's, so nothing is lost here."""
    found = findings_for(
        facts(
            **{
                "device.last_contact": (NOW - dt.timedelta(days=10)).isoformat(),
                "alerts.open": 4,
                "findings.known_exploited": 2,
                "findings.critical": 9,
                "device.needs_reboot": True,
                "patches.failed": 3,
            }
        ),
        ticket=TicketFacts(created_at=NOW - dt.timedelta(days=20), origin="alert"),
        now=NOW,
    )
    assert len(found) > MAX_SHOWN
    assert [one.rank for one in found] == sorted(one.rank for one in found)


# --- the next step ------------------------------------------------------------


def test_an_offline_machine_asks_for_the_user_before_a_remote_cleanup() -> None:
    found = findings_for(
        facts(**{"device.last_contact": (NOW - dt.timedelta(days=10)).isoformat()}), now=NOW
    )
    step = next_step_for(found)
    assert step is not None
    assert step.text == "Contact the user to bring the machine online before any remote cleanup."
    assert step.because == "device_offline"


def test_a_stale_alert_asks_whether_it_still_applies() -> None:
    found = findings_for(
        facts(), ticket=TicketFacts(created_at=NOW - dt.timedelta(days=9), origin="alert"), now=NOW
    )
    step = next_step_for(found)
    assert step is not None
    assert step.text == "Check the alert still applies before acting."


def test_a_repeat_asks_how_the_earlier_ones_were_closed() -> None:
    found = findings_for(facts(**{"tickets.contact": 2}), now=NOW)
    step = next_step_for(found)
    assert step is not None
    assert step.text == "Look at how the earlier tickets were closed."


def test_nothing_to_say_shows_no_next_step_at_all() -> None:
    """A section that appears on every ticket saying something bland is a
    section people learn to skip, and then they skip it when it matters."""
    assert next_step_for([]) is None
    assert next_step_for(findings_for(facts(**{"device.needs_reboot": True}), now=NOW)) is None


def test_counts_read_as_english_in_the_singular() -> None:
    found = findings_for(facts(**{"alerts.open": 1, "patches.failed": 1}), now=NOW)
    assert [one.text for one in found] == [
        "1 open alert on this machine",
        "1 patch install failed recently",
    ]


def test_yesterday_is_one_day_and_not_one_days() -> None:
    found = findings_for(
        facts(),
        ticket=TicketFacts(repeat_of="70001", repeat_at=NOW - dt.timedelta(days=1)),
        now=NOW,
    )
    assert found[0].detail == "1 day ago"


# --- the dot beside a fact --------------------------------------------------


def test_a_facts_tone_and_its_finding_say_the_same_thing() -> None:
    """The ticket page draws facts in its rail and findings in its column, and
    the two are about the same machine. A dot that said one thing where the
    sentence beside it said another would be worse than no dot: the reader
    would have to work out which to believe.

    So the tone is read from the findings' own conditions, and this is the
    assertion that keeps them together — a fact whose tone is not neutral
    produces its finding, and a fact that produces no finding is neutral.
    """
    cases = [
        # (fact id, value, tone, the finding it agrees with)
        ("device.last_contact", (NOW - dt.timedelta(days=10)).isoformat(), "bad", "device_offline"),
        ("device.offline", True, "bad", "device_offline"),
        ("protection.signatures_outdated", True, "warning", "signatures_outdated"),
        ("device.needs_reboot", True, "warning", "pending_reboot"),
        ("protection.agent", False, "bad", "no_protection_agent"),
        ("account.enabled", False, "bad", "account_disabled"),
        ("account.mfa_registered", False, "warning", "no_mfa_registered"),
        ("account.sign_in_days", 60, "warning", "account_dormant"),
        ("account.unlicensed_member", True, "warning", "unlicensed_account"),
        ("backup.status", "failed", "warning", "backup_failed"),
        ("backup.status", "not_backed_up", "warning", "backup_none"),
    ]
    for fact_id, value, tone, kind in cases:
        assert tone_for(fact_id, value, now=NOW) == tone, fact_id
        assert kind in kinds(findings_for(facts(**{fact_id: value}), now=NOW)), fact_id


def test_a_fact_no_finding_is_built_from_is_neutral() -> None:
    """Neutral is the honest answer where the rules have nothing to say. A page
    that coloured "Windows 11 Pro" would be inventing a verdict."""
    quiet = [
        ("device.os", "Windows 11 Pro"),
        ("device.last_user", "CONTOSO\\cfoster"),
        ("findings.risk_grade", "A"),
        ("device.last_contact", (NOW - dt.timedelta(hours=2)).isoformat()),
        ("device.needs_reboot", False),
        # Some days completed with errors. The report calls that a warning and
        # no rule here asserts it, so the word carries it and the dot does not.
        ("backup.status", "warnings"),
        # Evidence under the status rather than a claim of its own: a count
        # with no status beside it is not a finding about anything.
        ("backup.failures_7d", 3),
        ("backup.days_since_successful", 11),
    ]
    for fact_id, value in quiet:
        assert tone_for(fact_id, value, now=NOW) == "neutral", fact_id
        assert findings_for(facts(**{fact_id: value}), now=NOW) == [], fact_id


def test_the_good_tone_is_the_absence_of_the_finding() -> None:
    """An agent installed and its signatures current is the one thing the rail
    says is *well*, and it is well precisely because no finding is produced."""
    for fact_id, value in (
        ("protection.agent", True),
        ("protection.signatures_outdated", False),
        ("device.offline", False),
        ("backup.status", "ok"),
    ):
        assert tone_for(fact_id, value, now=NOW) == "good", fact_id
        assert findings_for(facts(**{fact_id: value}), now=NOW) == [], fact_id


def test_the_tone_uses_the_findings_own_threshold() -> None:
    """Three days is `OFFLINE_DAYS`, in one place. A machine seen two days ago
    is neither a finding nor a red dot."""
    recent = (NOW - dt.timedelta(days=OFFLINE_DAYS - 1)).isoformat()
    older = (NOW - dt.timedelta(days=OFFLINE_DAYS)).isoformat()
    assert tone_for("device.last_contact", recent, now=NOW) == "neutral"
    assert tone_for("device.last_contact", older, now=NOW) == "bad"
    assert kinds(findings_for(facts(**{"device.last_contact": recent}), now=NOW)) == []
    assert kinds(findings_for(facts(**{"device.last_contact": older}), now=NOW)) == [
        "device_offline"
    ]


# --- the account, beside the machine ------------------------------------------


def test_a_disabled_account_is_said_beside_the_machine_being_absent() -> None:
    """The identity half of rank 1: nothing the person signs into will work.
    The machine is still said first when both are true."""
    found = findings_for(
        facts(
            **{
                "device.last_contact": (NOW - dt.timedelta(days=10)).isoformat(),
                "account.enabled": False,
            }
        ),
        now=NOW,
    )
    assert kinds(found)[:2] == ["device_offline", "account_disabled"]
    disabled = found[1]
    assert disabled.rank == 1 and disabled.source == "Microsoft 365"
    assert disabled.text == "The Microsoft 365 account is disabled"
    assert disabled.chip == "Account disabled"


def test_the_four_account_findings_rank_where_they_belong() -> None:
    found = findings_for(
        facts(
            **{
                "account.enabled": True,
                "account.mfa_registered": False,
                "account.sign_in_days": 41,
                "account.unlicensed_member": True,
            }
        ),
        now=NOW,
    )
    assert kinds(found) == ["no_mfa_registered", "account_dormant", "unlicensed_account"]
    assert [one.rank for one in found] == [4, 6, 6]
    assert found[1].text == "No Microsoft 365 sign-in for 5 weeks"
    assert found[2].text == "The Microsoft 365 account has no licence"


def test_mfa_that_could_not_be_read_is_not_a_finding() -> None:
    """`None` is not "no": a disabled account has no row in Microsoft's
    registration report, and a tenant may not have consented to it being read.
    A card that said "no MFA" for either would be making it up."""
    assert findings_for(facts(**{"account.mfa_registered": None}), now=NOW) == []
    assert tone_for("account.mfa_registered", None, now=NOW) == "neutral"


def test_a_disabled_account_is_not_also_called_dormant() -> None:
    """Two findings about one thing read as two problems. The disabled account
    is the one that changes the handling."""
    found = findings_for(facts(**{"account.enabled": False, "account.sign_in_days": 200}), now=NOW)
    assert kinds(found) == ["account_disabled"]


def test_the_sign_in_threshold_is_the_connectors_own() -> None:
    """Thirty days in one place: the connector reads it and this module ranks
    it, and a ticket page that disagreed with an Ask answer about the same
    account would be the bug this pins shut."""
    from app.autopilot.findings import SIGN_IN_STALE_DAYS
    from app.connectors.microsoft365.reads import STALE_SIGN_IN_DAYS

    assert SIGN_IN_STALE_DAYS == STALE_SIGN_IN_DAYS
    under = SIGN_IN_STALE_DAYS - 1
    assert findings_for(facts(**{"account.sign_in_days": under}), now=NOW) == []
    assert tone_for("account.sign_in_days", under, now=NOW) == "neutral"
    assert tone_for("account.sign_in_days", SIGN_IN_STALE_DAYS, now=NOW) == "warning"


def test_an_account_in_good_order_produces_nothing() -> None:
    for fact_id, value in (
        ("account.enabled", True),
        ("account.mfa_registered", True),
    ):
        assert tone_for(fact_id, value, now=NOW) == "good", fact_id
        assert findings_for(facts(**{fact_id: value}), now=NOW) == [], fact_id

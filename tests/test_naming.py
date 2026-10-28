"""Name normalisation and the per-client internal-organisation rules. No database."""

from __future__ import annotations

from typing import Any

import pytest

from app.connectors.naming import DEFAULT_SYNC_RULES, SyncRules, rules_for, vendor_match_key
from app.models.enums import ConnectorName
from app.models.site import Site, normalise_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Dental", "acme dental"),
        ("  Acme   Dental  ", "acme dental"),
        ("ACME DENTAL", "acme dental"),
        ("Acme Dental, LLC", "acme dental"),
        ("Acme Dental Inc.", "acme dental"),
        ("Acme Dental (Main St)", "acme dental"),
        ("Acme Dental, LLC (Main St)", "acme dental"),
        ("Smith & Jones PLLC", "smith jones"),
        ("Bright Legal Ltd. Co.", "bright legal"),
        ("O'Brien's Pub", "o brien s pub"),
        # A parenthetical in the middle is part of the name, not a suffix.
        ("Acme (East) Dental", "acme east dental"),
    ],
)
def test_names_that_mean_the_same_customer_normalise_equal(raw: str, expected: str) -> None:
    assert normalise_name(raw) == expected


@pytest.mark.parametrize("raw", ["LLC", "Inc", "(x)", "!!!", "   "])
def test_a_name_that_is_all_suffix_or_punctuation_never_normalises_to_nothing(raw: str) -> None:
    """Nothing may collapse to "" and match everything else that did."""
    assert normalise_name(raw) != ""


def test_a_site_keeps_its_normalised_name_in_step_with_its_name() -> None:
    site = Site(slug="acme-dental", name="Acme Dental, LLC")
    assert site.normalized_name == "acme dental"

    site.name = "Acme Family Dental (North)"
    assert site.normalized_name == "acme family dental"


# --- rules ------------------------------------------------------------------


def test_the_defaults_recognise_each_vendors_conventions() -> None:
    ninja = DEFAULT_SYNC_RULES[ConnectorName.NINJAONE]
    assert ninja.is_internal("zz Archived", client_name="TeamLogic IT of Austin")
    assert ninja.is_internal("ZZ-Templates", client_name="TeamLogic IT of Austin")
    assert not ninja.is_internal("Buzz Coffee", client_name="TeamLogic IT of Austin")

    autotask = DEFAULT_SYNC_RULES[ConnectorName.AUTOTASK]
    assert autotask.is_internal("TeamLogic IT of Dallas", client_name="TeamLogic IT of Austin")
    assert not autotask.is_internal("Teamlogical Solutions", client_name="TeamLogic IT of Austin")


def test_an_organisation_named_like_the_client_is_internal() -> None:
    rules = SyncRules()
    assert rules.is_internal("TeamLogic IT of Austin, LLC", client_name="TeamLogic IT of Austin")
    assert not SyncRules(match_tenant_name=False).is_internal(
        "TeamLogic IT of Austin", client_name="TeamLogic IT of Austin"
    )


def test_a_client_override_replaces_only_the_keys_it_names() -> None:
    extra: dict[str, Any] = {"sync": {"internal_prefixes": ["TL-"]}}
    rules = rules_for(ConnectorName.AUTOTASK, extra)

    assert rules.internal_prefixes == ("TL-",)
    # Untouched keys keep the connector's defaults.
    assert rules.internal_name_patterns == (r"\bteamlogic\b",)
    assert rules.match_tenant_name is True


def test_an_override_can_switch_a_default_rule_off() -> None:
    rules = rules_for(ConnectorName.NINJAONE, {"sync": {"internal_prefixes": []}})
    assert rules.internal_prefixes == ()
    assert not rules.is_internal("zz Archived", client_name="whoever")


@pytest.mark.parametrize("extra", [None, {}, {"sync": None}, {"sync": "zz"}, {"other": {}}])
def test_a_missing_or_malformed_override_means_the_defaults(extra: dict[str, Any] | None) -> None:
    assert rules_for(ConnectorName.NINJAONE, extra) == DEFAULT_SYNC_RULES[ConnectorName.NINJAONE]


def test_a_single_string_is_accepted_where_a_list_is_expected() -> None:
    rules = rules_for(ConnectorName.NINJAONE, {"sync": {"internal_prefixes": "lab-"}})
    assert rules.internal_prefixes == ("lab-",)


def test_an_unknown_connector_has_no_rules_beyond_the_tenant_name() -> None:
    rules = rules_for(ConnectorName.COVE)
    assert rules == SyncRules()


def test_the_franchisees_own_company_is_internal_on_every_vendor_that_lists_one() -> None:
    """Autotask, GravityZone and ConnectSecure each hold a company for the franchisee
    itself; a sync must not wake its site up (live, 2026-09-24)."""
    for connector in (
        ConnectorName.AUTOTASK,
        ConnectorName.BITDEFENDER,
        ConnectorName.CONNECTSECURE,
    ):
        rules = rules_for(connector)
        assert rules.is_internal("TeamLogic IT (2zi2x6)", client_name="TeamLogic IT of Austin")
        assert not rules.is_internal("Acme Dental (2zi2x6)", client_name="TeamLogic IT of Austin")


def test_the_gravityzone_integration_suffix_is_stripped_for_matching_only() -> None:
    assert vendor_match_key(ConnectorName.BITDEFENDER, "Insurance Council of Texas (vnghix)") == (
        vendor_match_key(ConnectorName.BITDEFENDER, "Insurance Council of Texas")
    )
    # Five uppercase characters is a hand-made tag, not the integration's; the
    # generic one-trailing-parenthetical rule still folds it.
    assert (
        vendor_match_key(ConnectorName.BITDEFENDER, "Sobering Center (AAAJM)") == "sobering center"
    )
    # Six lowercase characters that are part of a name and not in parentheses stay.
    assert vendor_match_key(ConnectorName.BITDEFENDER, "Abc123 Holdings") == "abc123 holdings"

"""Cove's column vocabulary: the codes, and reading a row back.

No network and no client here — this is the part that decides what a value
means, and most of the ways it can go wrong are quiet ones. The vendor
ignores a column code it does not recognise rather than refusing it, sends
every value as a string, and uses zero for "never".
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.connectors.cove import columns as col

# --- the codes we send --------------------------------------------------------------


def test_a_statistics_code_is_built_and_never_typed() -> None:
    """Zero-padding is mandatory and a wrong code fails silently, so the only
    way to make one is from the declared parts."""
    assert col.stat(col.TOTAL, "last_successful_at") == "D09F09"
    assert col.stat(col.M365_EXCHANGE, "user_mailboxes") == "D19F20"
    assert col.stat(col.M365_ONEDRIVE, "user_mailboxes") == "D20F20"


def test_an_unknown_field_is_refused_here_rather_than_by_the_vendor() -> None:
    with pytest.raises(ValueError, match="not a Cove statistics field"):
        col.stat(col.TOTAL, "last_good_backup")


def test_every_declared_code_is_the_shape_the_vendor_requires() -> None:
    """`D1F3` is not a worse `D01F03`; it is a value that never arrives."""
    for code in col.COLUMNS.values():
        assert code.startswith("I") and code[1:].isdigit(), code
    for code in col.FIELDS.values():
        assert len(code) == 3 and code.startswith("F") and code[1:].isdigit(), code
    for source in col.DATA_SOURCES:
        assert len(source) == 3 and source.startswith("D") and source[1:].isdigit(), source


# --- reading a row ------------------------------------------------------------------


def test_settings_fold_into_something_with_names_on_it() -> None:
    folded = col.fold([{"I14": "23630480"}, {"I1": "fab-ws-01_ab12c"}])
    assert folded == {"I14": "23630480", "I1": "fab-ws-01_ab12c"}


def test_a_malformed_entry_does_not_cost_the_rest_of_the_row() -> None:
    assert col.fold([{"I14": "1"}, "nonsense", None, {"I1": None}]) == {"I14": "1"}
    assert col.fold(None) == {}


def test_a_number_that_is_not_one_is_absent_rather_than_zero() -> None:
    assert col.as_int("41") == 41
    assert col.as_int("") is None
    assert col.as_int("n/a") is None
    assert col.as_int(None) is None


def test_zero_is_never_a_time() -> None:
    """Cove uses zero for 'never', and a backup dated 1 January 1970 on a
    report is worse than one that says never."""
    assert col.as_time("0") is None
    assert col.as_time(None) is None
    # The vendor's own worked example: 1536906195 is 14 September 2018, which
    # its documentation prints as 07:23:15 in the author's own time zone.
    assert col.as_time("1536906195") == dt.datetime(2018, 9, 14, 6, 23, 15, tzinfo=dt.UTC)


def test_data_sources_split_into_fixed_blocks() -> None:
    assert col.data_sources("D01D02") == ["D01", "D02"]
    assert col.data_sources("D19D20D05") == ["D19", "D20", "D05"]
    assert col.data_sources("") == []
    # A trailing fragment is dropped rather than guessed at.
    assert col.data_sources("D01D0") == ["D01"]
    # A code we have never seen is still a data source.
    assert col.data_sources("D99") == ["D99"]


# --- the name a machine is known by elsewhere ---------------------------------------


@pytest.mark.parametrize(
    ("cove_name", "expected"),
    [
        ("jc-lt-25_pqpsp", "jc-lt-25"),
        ("JC-LT-25_PQPSP", "jc-lt-25"),
        ("fab-ws-01_ab12c", "fab-ws-01"),
        ("  fab-ws-01_ab12c  ", "fab-ws-01"),
        # No suffix at all: the name is the name.
        ("fab-ws-01", "fab-ws-01"),
        ("FAB-WS-01", "fab-ws-01"),
        # A tenant row is not a machine name and is left alone.
        ("fabrikam.example.invalid", "fabrikam.example.invalid"),
        ("", ""),
    ],
)
def test_the_suffix_comes_off_and_nothing_else_does(cove_name: str, expected: str) -> None:
    assert col.device_key(cove_name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "backup_srv",  # three characters, not five
        "fab-ws-01_abc",  # four
        "fab-ws-01_abcdef",  # six
        "fab-ws-01_ab-12",  # not alphanumeric
        "fab_ws_01",  # an underscore in the middle of a name
    ],
)
def test_a_name_that_only_looks_like_it_has_a_suffix_keeps_it(name: str) -> None:
    assert col.device_key(name) == name.casefold()


def test_two_machines_that_differ_before_the_suffix_stay_two_machines() -> None:
    """The rule must not collapse neighbours. JC-LT-25 and JC-LT-26 share
    everything but the digit somebody needs."""
    assert col.device_key("jc-lt-25_pqpsp") != col.device_key("jc-lt-26_pqpsp")
    assert col.device_key("fab-ws-01_ab12c") != col.device_key("fab-ws-010_ab12c")


def test_the_same_machine_under_two_suffixes_is_one_key() -> None:
    """A reinstalled agent gets a new suffix; it is the same machine."""
    assert col.device_key("fab-ws-01_ab12c") == col.device_key("fab-ws-01_zz9yx")

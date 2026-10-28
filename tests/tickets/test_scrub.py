"""Likely secrets leave ticket text; hostnames, users, IPs and error codes stay."""

from __future__ import annotations

import pytest

from app.tickets.scrub import REDACTED, scrub, scrub_all


@pytest.mark.parametrize(
    ("text", "expected", "count"),
    [
        ("Password: Hunter2!", f"Password: {REDACTED}", 1),
        (
            "the user's password is Winter2026$ and it fails",
            f"the user's password is {REDACTED} and it fails",
            1,
        ),
        ("pwd=abc123; Server=sql01;Database=erp", f"pwd={REDACTED}; Server=sql01;Database=erp", 1),
        (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dGVzdHNpZ25hdHVyZQ",
            f"Authorization: Bearer {REDACTED}",
            1,
        ),
        ("api key: sk-ant-abcdefghijklmnopqrstuvwxyz0123456789", f"api key: {REDACTED}", 1),
        (
            "AWS key AKIAIOSFODNN7EXAMPLE was in the script",
            f"AWS key {REDACTED} was in the script",
            1,
        ),
        (
            "BitLocker recovery key 123456-654321-112233-445566-778899-001122-334455-667788",
            f"BitLocker recovery key {REDACTED}",
            1,
        ),
        ("PIN: 4821 for the door", f"PIN: {REDACTED} for the door", 1),
    ],
)
def test_secrets_are_replaced_and_the_words_around_them_kept(
    text: str, expected: str, count: int
) -> None:
    result = scrub(text)
    assert result.text == expected
    assert result.count == count


def test_a_private_key_block_goes_whole() -> None:
    text = (
        "attached:\n-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\nthanks"
    )
    assert scrub(text).text == f"attached:\n{REDACTED}\nthanks"


def test_what_an_engineer_needs_is_untouched() -> None:
    text = (
        "TL-AUS-WS14 (10.0.4.22) cannot pin the app to the taskbar; error 0x80070005 "
        "after KB5060000; user jdoe@acme.local; ticket T20260915.0042; device id "
        "6a04a766-cb6a-2196-c8f7-13ec00000000; a token gesture of goodwill"
    )
    assert scrub(text) == scrub(text)  # deterministic
    assert scrub(text).count == 0
    assert scrub(text).text == text


def test_a_long_key_looking_run_is_removed_but_a_long_word_is_not() -> None:
    assert scrub("key material Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg== here").text.startswith(
        "key material [redacted]"
    )
    assert scrub("Pneumonoultramicroscopicsilicovolcanoconiosis is long").count == 0


def test_scrub_all_counts_across_texts_and_none_is_empty() -> None:
    texts, count = scrub_all("password: a1b2c3d4", None, "token=abcd1234efgh")
    assert texts == [f"password: {REDACTED}", "", f"token={REDACTED}"]
    assert count == 2


# --- the next word of a sentence is not a secret ---------------------------------------


#: Titles shaped like the pilot's own. Taking the word after `password` cost us
#: the commonest helpdesk category: 26 of 2,724 stored titles read
#: `Password [redacted] ...` and resets, expiries and changes could not be told
#: apart (validation log, 2026-10-01).
@pytest.mark.parametrize(
    "title",
    [
        "Password reset for a user",
        "Password reset.",
        "Password reset request",
        "password expired",
        "Password change request",
        "password reset-request",
        "User password is reset",
        "password is expired",
        "password not working",
        "cannot remember password",
        "Bitlocker recovery key not available",
        "License key missing",
        "MFA token not working",
        "Account locked out after password change",
        "api key rotation scheduled",
    ],
)
def test_an_ordinary_word_after_a_secret_keyword_is_kept(title: str) -> None:
    result = scrub(title)
    assert result.text == title
    assert result.count == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A punctuation separator is somebody writing a value down: anything goes.
        ("password: Welcome", f"password: {REDACTED}"),
        ("pwd=letmein", f"pwd={REDACTED}"),
        ("api key -> abcdef", f"api key -> {REDACTED}"),
        # Without one, the token has to look like a secret.
        ("password P@ssw0rd", f"password {REDACTED}"),
        ("New password is Tr0ub4dor&3", f"New password is {REDACTED}"),
        (
            "Password reset - temp password Welcome123!",
            f"Password reset - temp password {REDACTED}",
        ),
        ("BitLocker key 123456-654321-112233", f"BitLocker key {REDACTED}"),
    ],
)
def test_a_value_that_looks_like_one_still_goes(text: str, expected: str) -> None:
    result = scrub(text)
    assert result.text == expected
    assert result.count == 1


def test_the_trade_is_written_down_rather_than_hidden() -> None:
    """What a plain-word password costs, stated exactly.

    `password Welcome` is kept, because nothing distinguishes it from
    `password reset`. A separator or a digit catches it. So does a symbol
    inside the word — but **not** one at the end: sentence punctuation is
    stripped before the test, which is what keeps `Password reset.` readable,
    and `password Welcome!` is the same shape. The 26 real titles that rule
    protects are worth more than the one it lets through.
    """
    assert scrub("password Welcome").count == 0
    assert scrub("password Welcome!").count == 0  # could be either; reads as a sentence
    assert scrub("password: Welcome").count == 1
    assert scrub("password Welcome1").count == 1
    assert scrub("password Wel!come").count == 1

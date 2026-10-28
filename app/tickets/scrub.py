"""Likely secrets out of ticket text, before it is stored or shown to a model.

People paste passwords into tickets. The workspace stores a ticket's text
and hands it to the model, so both get the scrubbed copy and nothing else
ever holds the raw one. The rules are patterns, applied in order, each
replacing the secret with `[redacted]` and leaving the words around it so
the ticket still reads: `password: Hunter2!` becomes `password: [redacted]`.

What is caught: a value after a password, passphrase, PIN, secret, token,
API key, client secret or connection-string keyword; `Bearer` and `Basic`
authorisation values; PEM private-key blocks; the well-known key shapes
(AWS access keys, GitHub and Slack tokens, Anthropic and OpenAI keys, JWTs);
and any long run of key-looking characters (32 or more of base64 or hex) that
is not a plain word. What is not caught, on purpose: hostnames, usernames,
IPs, ticket numbers, error codes — the things an engineer needs.

**And not the next word of a sentence.** `password: Hunter2!` is somebody
writing a value down; `Password reset for a user` is what the ticket is about,
and taking the word after `password` there cost us the commonest category on
the helpdesk — 26 of the pilot's 2,724 titles, with resets, expiries and
changes made indistinguishable (validation log, 2026-10-01). A punctuation
separator (`:`, `=`, `->`) means a value follows and anything goes; with `is`,
`was`, `-` or nothing, only a token that looks like a secret does — one that is
not an ordinary word and carries a digit or a symbol. The cost of that line is
a plain-word password written without punctuation (`password Welcome`), which
is kept, because nothing distinguishes it from `password reset`.

`scrub` returns the text and how many replacements were made; the count is
stored on the workspace so a person can see that something was removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REDACTED = "[redacted]"

#: The words that mean nothing but a secret: `password`, `passphrase`, `api
#: key`, `client secret`, `BitLocker recovery key`.
_SENSITIVE_WORD = (
    r"\b(?:pass(?:word|phrase|wd|code)|pwd|api[ _-]?key|access[ _-]?key|"
    r"client[ _-]?secret|auth(?:orization)?[ _-]?(?:key|token)|license[ _-]?key|"
    r"activation[ _-]?key|recovery[ _-]?key|bitlocker(?: recovery)?(?: key)?)"
)
#: `password: x`, `pwd=x`, `api key -> x`. Somebody writing a value down: the
#: punctuation is the intent, so whatever follows goes, word or not.
_KEYWORD_WITH_SEPARATOR = re.compile(
    rf"(?P<lead>{_SENSITIVE_WORD}\s*(?:=|:|->)\s*)"
    r"(?P<value>(?!\[redacted\])[^\s=:>-]\S{2,})",
    re.IGNORECASE,
)
#: `password is x`, `password - x`, `password x`. Ordinary English, where the
#: next word is as likely to be `reset` as a secret, so only a token that
#: *looks* like one is taken (`_looks_like_a_secret`).
_KEYWORD_LOOSE = re.compile(
    rf"(?P<lead>{_SENSITIVE_WORD}\s*(?:is|was|-)?\s+)"
    r"(?P<value>(?!\[redacted\])[^\s=:>-]\S{2,})",
    re.IGNORECASE,
)
#: Sentence punctuation around a word, which is not part of it.
_EDGE_PUNCTUATION = ".,;:!?)(][}{'\"`"
#: Characters that appear inside ordinary words and do not make one a secret.
_WORD_INNARDS = str.maketrans({"-": "", "'": "", "/": "", "\u2019": ""})


def _looks_like_a_secret(value: str) -> bool:
    """Is this a secret somebody pasted, or the next word of a sentence?

    A secret is not an ordinary word: it carries a digit or a symbol. Hyphens,
    apostrophes and slashes do not count, or `password reset-request` would
    read as one. Sentence punctuation is stripped first, or `password reset.`
    would.
    """
    core = value.strip(_EDGE_PUNCTUATION)
    if len(core) < 3:
        return False
    if core.translate(_WORD_INNARDS).isalpha():
        return False
    return any(character.isdigit() or not character.isalnum() for character in core)


#: `PIN: 1234`, `token=abc`, `secret is x`, `pw: x`. Words with ordinary uses
#: too ("pin the app", "a token gesture"): only with a separator.
_WEAK_KEYWORD = re.compile(
    r"(?P<lead>\b(?:pin|pw|token|secret|key)\s*(?:is|=|:|->)\s*)"
    r"(?P<value>(?!\[redacted\])[^\s=:>-]\S{3,})",
    re.IGNORECASE,
)
#: `Authorization: Bearer eyJ...`, `Basic dXNlcjpwYXNz`.
_AUTH_HEADER = re.compile(r"\b(?P<lead>(?:bearer|basic)\s+)(?P<value>[A-Za-z0-9+/=_\-.]{8,})", re.I)
#: `Password=...;` inside a connection string, whatever the surrounding text.
_CONNECTION_STRING = re.compile(r"(?P<lead>\b(?:password|pwd)\s*=\s*)(?P<value>[^;\s]{1,})", re.I)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
)
_KNOWN_SHAPES = re.compile(
    r"\b(?:"
    r"AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|ghp_[A-Za-z0-9]{36,}"  # GitHub
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"  # Slack
    r"|sk-ant-[A-Za-z0-9_\-]{20,}"  # Anthropic
    r"|sk-[A-Za-z0-9]{20,}"  # OpenAI and friends
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"  # JWT
    r")"
)
#: A long run of key-looking characters. Excludes plain hex GUIDs with dashes
#: (device ids, correlation ids) by requiring no dash, and excludes words by
#: requiring at least one digit.
_LONG_TOKEN = re.compile(
    r"(?<![A-Za-z0-9+/=])(?=[A-Za-z0-9+/=]*\d)[A-Za-z0-9+/=]{32,}(?![A-Za-z0-9+/=])"
)


@dataclass(frozen=True, slots=True)
class Scrubbed:
    text: str
    count: int


def scrub(text: str | None) -> Scrubbed:
    """The text with likely secrets replaced, and how many were."""
    if not text:
        return Scrubbed("", 0)
    count = 0

    def keep_lead(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return match.group("lead") + REDACTED

    def whole(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return REDACTED

    def keep_lead_if_secret(match: re.Match[str]) -> str:
        """Redact only what looks like a secret; leave a sentence alone."""
        nonlocal count
        if not _looks_like_a_secret(match.group("value")):
            return match.group(0)
        count += 1
        return match.group("lead") + REDACTED

    out = _PRIVATE_KEY.sub(whole, text)
    out = _AUTH_HEADER.sub(keep_lead, out)
    out = _CONNECTION_STRING.sub(keep_lead, out)
    out = _KEYWORD_WITH_SEPARATOR.sub(keep_lead, out)
    out = _KEYWORD_LOOSE.sub(keep_lead_if_secret, out)
    out = _WEAK_KEYWORD.sub(keep_lead, out)
    out = _KNOWN_SHAPES.sub(whole, out)
    out = _LONG_TOKEN.sub(whole, out)
    return Scrubbed(out, count)


def scrub_all(*texts: str | None) -> tuple[list[str], int]:
    """Several texts at once, with the one count the workspace stores."""
    results = [scrub(text) for text in texts]
    return [result.text for result in results], sum(result.count for result in results)

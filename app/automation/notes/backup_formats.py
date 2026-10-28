"""Backup notices, parsed by fixed rules per sender format.

There is no backup connector yet, so the backup-notice workflow reads the
ticket's title and body and nothing else. Two formats are known — Veeam
Backup & Replication's job report emails and N-able Cove's backup status
emails — each recognised by a marker only that sender writes, and each parsed
by a handful of exact prefixes. A notice in any other format is `None`: no
note is written for a format nobody has read, because a guess about a backup
is worse than silence.

Nothing here is a regex over free text looking for "the error": every value
comes from a line that starts with a known label.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackupNotice:
    sender: str
    """`veeam` or `n-able`."""
    outcome: str
    """`success`, `warning` or `failed`."""
    job: str | None
    error: str | None


def _lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").replace("\r", "").split("\n") if line.strip()]


def _after(lines: list[str], *labels: str) -> str | None:
    """The rest of the first line that starts with one of the labels."""
    for line in lines:
        for label in labels:
            if line.casefold().startswith(label.casefold()):
                value = line[len(label) :].strip(" :\t-")
                if value:
                    return value
    return None


def _outcome(word: str | None) -> str | None:
    if word is None:
        return None
    folded = word.casefold()
    if folded.startswith(("success", "completed", "ok")):
        return "success"
    if folded.startswith(("warn",)):
        return "warning"
    if folded.startswith(("fail", "error", "unsuccessful")):
        return "failed"
    return None


def parse_veeam(title: str, body: str | None) -> BackupNotice | None:
    """Veeam Backup & Replication job reports.

    Recognised by `Veeam` in the title or body. The subject is
    `[Success] <job>` / `[Warning] <job>` / `[Failed] <job>` and the body
    carries `Job: <name>` or `Backup job: <name>`, `Status: <word>` and
    `Error: <text>` lines.
    """
    text = f"{title}\n{body or ''}"
    if "veeam" not in text.casefold():
        return None
    lines = _lines(text)
    outcome = None
    job = None
    stripped = title.strip()
    if stripped.startswith("[") and "]" in stripped:
        word, rest = stripped[1:].split("]", 1)
        outcome = _outcome(word)
        job = rest.strip(" :-") or None
    outcome = outcome or _outcome(_after(lines, "Status", "Result"))
    job = _after(lines, "Backup job", "Job name", "Job") or job
    error = _after(lines, "Error", "Details", "Reason")
    if outcome is None:
        return None
    return BackupNotice(sender="veeam", outcome=outcome, job=job, error=error)


def parse_nable(title: str, body: str | None) -> BackupNotice | None:
    """N-able Cove (Backup Manager) status emails.

    Recognised by `N-able` or `Cove` or `Backup Manager` in the title or
    body; the body carries `Backup status: <word>` (or `Status:`), `Device:
    <name>` and `Error: <text>` lines, and the subject often reads
    `Backup <word> on <device>`.
    """
    text = f"{title}\n{body or ''}"
    folded = text.casefold()
    if not any(mark in folded for mark in ("n-able", "cove", "backup manager")):
        return None
    lines = _lines(text)
    outcome = _outcome(_after(lines, "Backup status", "Status", "Session status"))
    if outcome is None:
        words = title.casefold().split()
        for n, word in enumerate(words):
            if word == "backup" and n + 1 < len(words):
                outcome = _outcome(words[n + 1])
                break
    job = _after(lines, "Device", "Device name", "Backup set", "Job")
    error = _after(lines, "Error", "Error message", "Reason")
    if outcome is None:
        return None
    return BackupNotice(sender="n-able", outcome=outcome, job=job, error=error)


PARSERS = (parse_veeam, parse_nable)


def parse_backup_notice(title: str, body: str | None) -> BackupNotice | None:
    """The first parser that recognises the sender, or None."""
    for parser in PARSERS:
        found = parser(title, body)
        if found is not None:
            return found
    return None

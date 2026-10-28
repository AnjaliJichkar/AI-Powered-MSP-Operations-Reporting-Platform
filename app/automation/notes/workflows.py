"""The words the alert-note workflows share: the alert source, the phrase
lists their rules match on, the Blackpoint line.

The workflows themselves are definitions (`app/automation/definitions/
<name>.yaml`) assembled from the read actions and the note actions
(`app/automation/actions/`); the wording of every note is in
`app/automation/actions/notes.py`. The phrases here are the demo tenant's
defaults and the definitions' rules repeat them; a client's own come from its
settings (`automation.workflows.<name>.triggers.<key>`).
"""

from __future__ import annotations

from app.automation.notes.definitions import settings_tuple

ALERT_SOURCE = "Monitoring Alert"
DOWN_PHRASES = (
    "offline",
    "device down",
    "server down",
    "not responding",
    "unreachable",
    "no contact",
)
DISK_PHRASES = ("disk space", "disk free", "low disk", "free space", "volume full")
#: Not "virus": "Antivirus definitions out of date" is a hygiene alert, not a
#: threat, and a phrase list is read as substrings.
THREAT_PHRASES = (
    "malware",
    "threat",
    "quarantin",
    "ransomware",
    "infect",
    "blackpoint",
    "mdr",
)
MDR_PHRASES = ("blackpoint", "mdr")
#: A Veeam job report's subject is "[Failed] <job name>", which need not say
#: "backup"; the bracketed words and the senders' names are in the list, and
#: the parser decides what the notice is — an unknown format gets no note.
BACKUP_PHRASES = ("backup", "veeam", "n-able", "cove", "[success]", "[warning]", "[failed]")

MDR_LINE = "MDR alert: review required, do not close without investigation."


__all__ = [
    "ALERT_SOURCE",
    "BACKUP_PHRASES",
    "DISK_PHRASES",
    "DOWN_PHRASES",
    "MDR_LINE",
    "MDR_PHRASES",
    "THREAT_PHRASES",
    "settings_tuple",
]

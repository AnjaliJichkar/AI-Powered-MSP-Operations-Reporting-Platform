"""The cheap path: subject lines a rule is near-certain about.

A 2,700-ticket replay through a model costs money and twenty minutes; a regular
expression costs neither. So the rules run first and the model only sees what
they decline — and because the two paths are scored apart, the rules have to
earn their place rather than be assumed to help.

**Near-certain is the bar, not likely.** A rule here answers only where the
wording is a machine's: a monitoring check's name, a backup job's result, a
disk threshold. Anything a person might plausibly have typed goes to the model.
A rule that is right 80% of the time is worse than no rule, because its
mistakes arrive with the authority of a certainty and never reach the model
that might have caught them.

Every rule names itself on the prediction (`rule_name`), so a rule that turns
out to be wrong can be found and removed with the evidence in hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.autopilot.shadow.schema import UNSURE, Origin, Urgency


@dataclass(frozen=True, slots=True)
class RuleAnswer:
    """What a rule concluded. Confidences are the rule's own, and deliberately
    short of 1.0: a rule is near-certain, not certain."""

    rule_name: str
    origin: Origin
    origin_confidence: float
    issue_type: str
    issue_type_confidence: float
    urgency: Urgency
    urgency_confidence: float


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    origin: Origin
    #: The tenant's own issue-type label this rule asserts, or `None` to leave
    #: the type unanswered. A rule may be sure a line is machine-generated and
    #: have no business saying which category the tenant files it under.
    issue_type: str | None
    urgency: Urgency
    origin_confidence: float = 0.95
    issue_type_confidence: float = 0.8
    urgency_confidence: float = 0.6


#: Backup software reporting on itself. The vendor names and the words `job`,
#: `backup failed`, `replication` in a subject line are not how a person asks
#: for help; they are how Veeam, Datto and Cove write to a mailbox.
_BACKUP = re.compile(
    r"(?:\bbackup\s+(?:job\s+)?(?:failed|failure|error|warning|success|completed|report)\b"
    r"|\b(?:veeam|datto|cove|shadowprotect|acronis)\b[^.]{0,40}\b(?:job|backup|failed|warning)\b"
    r"|\breplication\s+(?:failed|job)\b"
    r"|\bbackup\s+(?:alert|notification)\b)",
    re.IGNORECASE,
)
#: Monitoring saying a threshold was crossed or a check changed state. The
#: shape is a subject with a state word and no addressee.
_MONITORING = re.compile(
    r"(?:\balert[: ]|\bmonitoring\b[^.]{0,30}\b(?:alert|triggered|threshold)\b"
    r"|\b(?:cpu|memory|ram|disk|volume|drive)\b[^.]{0,20}"
    r"\b(?:threshold|over|above|high|critical|exceeded|at \d{1,3}%)"
    r"|\bdisk\s+space\s+(?:low|critical|warning)\b"
    r"|\b(?:service|agent|heartbeat|check)\b[^.]{0,20}"
    r"\b(?:down|stopped|offline|unreachable|not responding|failed)\b"
    r"|\b(?:ping|host)\s+(?:down|unreachable)\b"
    r"|\bsnmp\b|\bnagios\b|\bzabbix\b|\bprtg\b)",
    re.IGNORECASE,
)
#: Patch and antivirus automation reporting in.
_SECURITY_AUTOMATION = re.compile(
    r"(?:\b(?:threat|malware|virus|ransomware)\s+(?:detected|found|blocked|quarantined)\b"
    r"|\bquarantine[d]?\s+(?:a\s+)?(?:file|threat|item)\b"
    r"|\bpatch\s+(?:install|installation|deployment)\s+(?:failed|failure)\b"
    r"|\bdefinitions?\s+(?:out of date|outdated)\b)",
    re.IGNORECASE,
)


def rules_for(issue_types: list[str]) -> list[Rule]:
    """The rules this tenant's own issue-type labels support.

    A rule asserts a type only when the tenant actually has that label; where
    it does not, the rule still answers `origin` and leaves the type to the
    model. That is why this takes the label list rather than hard-coding
    names: `Backup/Restore` is this tenant's word for it and need not be the
    next one's (api-notes: every picklist is per tenant).
    """

    def label(*wanted: str) -> str | None:
        for name in wanted:
            if name in issue_types:
                return name
        return None

    return [
        Rule(
            name="backup_job_report",
            pattern=_BACKUP,
            origin="alert",
            issue_type=label("Backup/Restore"),
            urgency="normal",
        ),
        Rule(
            name="monitoring_threshold",
            pattern=_MONITORING,
            origin="alert",
            # Monitoring fires about servers, workstations and the network
            # alike, so the category is the model's problem, not the rule's.
            issue_type=None,
            urgency="normal",
        ),
        Rule(
            name="security_automation",
            pattern=_SECURITY_AUTOMATION,
            origin="alert",
            issue_type=label("Cybersecurity"),
            urgency="high",
        ),
    ]


def apply_rules(title: str, rules: list[Rule]) -> RuleAnswer | None:
    """The first rule that matches, or nothing for the model to answer.

    First match wins and the order in `rules_for` is the order of specificity:
    a backup job's failure notice also mentions a failure, and the backup rule
    should be the one that claims it.
    """
    for rule in rules:
        if not rule.pattern.search(title):
            continue
        return RuleAnswer(
            rule_name=rule.name,
            origin=rule.origin,
            origin_confidence=rule.origin_confidence,
            issue_type=rule.issue_type or UNSURE,
            issue_type_confidence=rule.issue_type_confidence if rule.issue_type else 0.0,
            urgency=rule.urgency,
            urgency_confidence=rule.urgency_confidence,
        )
    return None

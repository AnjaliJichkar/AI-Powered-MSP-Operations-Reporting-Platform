"""What the tools say that changes how a ticket is handled, ranked.

Autotask's own Kaseya Intelligence already suggests an issue type, a priority,
a queue and an assignee from inside Autotask. Repeating that is worth little.
What it cannot see is the estate: that the machine has been offline for ten
days, that the alert is older than the outage, that this is the third backup
failure this week, that the same user raised the same thing on Tuesday. So the
card leads with those, and the classification goes at the bottom.

**A finding is ranked by how much it changes the handling**, not by how
interesting it is:

1. the machine is not there — nothing remote will work;
2. the ticket or alert is stale — it may not still apply;
3. backup has failed — the thing the customer notices last and minds most;
4. security: open alerts, known-exploited and critical findings;
5. a repeat — somebody has seen this before and how they closed it matters;
6. the ordinary housekeeping: a reboot, a failed patch, a full disk.

Each one carries the tool that said so and when it was read, because a fact
with no source is a claim, and an engineer who cannot tell where it came from
has to go and look anyway.

**Everything here is derived from rows we already hold** — the enrichment
snapshot the forward pass wrote, the prediction beside it, and the ledger —
so this module reads no vendor and calls no model. A sentence is written in
code, in the words an engineer would use, and never by the model: a finding
that cannot be phrased in a line is a finding nobody reads.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

#: How many findings the card shows before "Show N more". Four, because the
#: card is a glance and the fifth thing is never the reason somebody opens it.
MAX_SHOWN = 4

#: Days offline before the machine being absent is the first thing to say.
OFFLINE_DAYS = 3
#: Days before a ticket or its alert is old enough to doubt.
STALE_DAYS = 7
#: How far back a repeat counts. The same window the grouping uses.
REPEAT_DAYS = 30
#: Days without a Microsoft 365 sign-in before the account is worth a line.
#: The same number the connector uses (`microsoft365.reads.STALE_SIGN_IN_DAYS`),
#: kept here as its own constant so this module imports no connector;
#: `tests/autopilot/test_findings.py` pins the two together.
SIGN_IN_STALE_DAYS = 30

#: The tool each enrichment source belongs to, in the words on the card. A
#: reader has to be able to go and look, and `get_endpoint_protection` is not
#: somewhere anybody can look.
#: Named once, because a finding's source and the readings' tool name are the
#: same claim about where a sentence came from.
COVE = "Cove"

TOOLS: dict[str, str] = {
    "get_device": "NinjaOne",
    "list_open_alerts": "NinjaOne",
    "list_patch_installs": "NinjaOne",
    "get_endpoint_protection": "Bitdefender",
    "get_asset_vulnerabilities": "ConnectSecure",
    "search_tickets": "Autotask",
    "get_microsoft365_account": "Microsoft 365",
    "cove.list_devices": COVE,
    "identity": "Desk",
}

#: Which glyph a finding carries. Colour is never the only signal, so the tone
#: decides a mark as well as a tint (`states.js`).
WARNING = "warning"
INFO = "info"
REPEAT = "repeat"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing worth saying about a ticket, and where it came from."""

    kind: str
    """A stable id. It is what a verdict is stored against
    (`suggestion_reviews.field = "finding:<kind>"`), so renaming one loses the
    record of what people thought of it — rename the wording instead."""
    rank: int
    tone: str
    text: str
    """The bold sentence, as an engineer would say it. No field names."""
    chip: str
    """The queue's version: three or four words, because a queue row holds a
    chip and not a sentence."""
    detail: str | None = None
    """What follows the middle dot: the qualifier that stops the sentence
    being read too widely ("expected while offline")."""
    source: str = "Desk"
    as_of: str | None = None
    tickets: list[dict[str, str]] = field(default_factory=list)
    """Earlier tickets this finding points at, as `{id, number}` for linking."""

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rank": self.rank,
            "tone": self.tone,
            "text": self.text,
            "chip": self.chip,
            "detail": self.detail,
            "source": self.source,
            "as_of": self.as_of,
            "tickets": self.tickets,
        }


@dataclass(frozen=True, slots=True)
class TicketFacts:
    """What the card knows about the ticket itself, beside the snapshot.

    Passed in rather than read here, because this module is pure: given the
    same rows it writes the same findings, which is what makes the results
    page's "how often was this shown" worth reading.
    """

    created_at: dt.datetime | None = None
    origin: str | None = None
    """`alert` or `human`, as the agent read it. Decides whether a stale ticket
    is phrased as an alert or as a ticket."""
    repeat_of: str | None = None
    repeat_of_number: str | None = None
    repeat_at: dt.datetime | None = None


def _value(facts: dict[str, Any], fact_id: str) -> Any:
    return facts.get(fact_id)


def _count(value: Any) -> int | None:
    """A count from a snapshot value, which may be a number or a list marker."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict) and value.get("type") == "list":
        length = value.get("length")
        return int(length) if isinstance(length, int) else None
    return None


def _when(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        found = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=dt.UTC)


def _days(since: dt.datetime | None, *, now: dt.datetime) -> int | None:
    if since is None:
        return None
    return max(0, (now - since).days)


def _plural(count: int, word: str, many: str | None = None) -> str:
    """ "1 alert", "3 alerts". A card that says "1 alerts" is a card that looks
    generated, and an engineer trusts it accordingly."""
    return f"{count} {word}" if count == 1 else f"{count} {many or word + 's'}"


def _age_words(days: int) -> str:
    """ "1 day", "10 days", "2 weeks", "3 months" — the unit a person would use."""
    if days == 1:
        return "1 day"
    if days < 14:
        return f"{days} days"
    if days < 60:
        return f"{days // 7} weeks"
    return f"{days // 30} months"


def _on(value: Any) -> bool:
    return value is True


#: What a dot beside one fact is, where the page shows facts rather than
#: findings — the ticket page's "Machine at a glance". The conditions are the
#: findings' own, read one fact at a time instead of over the whole snapshot,
#: and the thresholds are the same constants: a machine not seen for
#: `OFFLINE_DAYS` is the `device_offline` finding's condition, outdated
#: signatures and a pending reboot are `signatures_outdated` and
#: `pending_reboot`. Nothing here decides anything the findings do not; a fact
#: no finding is built from is `neutral`, which is the honest answer rather
#: than a guess dressed as a verdict.
BAD, WARN, GOOD, NEUTRAL = "bad", "warning", "good", "neutral"


def tone_for(fact_id: str, value: Any, *, now: dt.datetime | None = None) -> str:
    """The dot a fact is drawn with: `bad`, `warning`, `good` or `neutral`.

    `tests/autopilot/test_findings.py` pins this against `findings_for` for the
    same fact, so a rule that moves in one moves in the other or the suite
    says so.
    """
    moment = now or dt.datetime.now(dt.UTC)
    if fact_id == "device.offline":
        return BAD if _on(value) else GOOD
    if fact_id == "device.last_contact":
        days = _days(_when(value), now=moment)
        return BAD if days is not None and days >= OFFLINE_DAYS else NEUTRAL
    if fact_id == "protection.last_seen":
        days = _days(_when(value), now=moment)
        return BAD if days is not None and days >= OFFLINE_DAYS else NEUTRAL
    if fact_id in ("protection.signatures_outdated", "protection.agent_outdated"):
        return WARN if _on(value) else GOOD
    if fact_id == "device.needs_reboot":
        return WARN if _on(value) else NEUTRAL
    if fact_id == "protection.agent":
        # Installed is good and absent is the `no_protection_agent` finding.
        return GOOD if value is True else BAD if value is False else NEUTRAL
    if fact_id == "backup.status":
        # The report's own words, and the two that are findings are the two
        # that get a dot. "warnings" is neither: no rule asserts it, so a tint
        # here would be a verdict nobody made. The days and the failure count
        # are the evidence under these and stay neutral for the same reason.
        return {"failed": WARN, "not_backed_up": WARN, "ok": GOOD}.get(str(value), NEUTRAL)
    if fact_id == "protection.still_infected":
        return BAD if _on(value) else NEUTRAL
    if fact_id == "account.enabled":
        # Disabled is the `account_disabled` finding's condition, and it is the
        # identity version of a machine that is not there.
        return GOOD if value is True else BAD if value is False else NEUTRAL
    if fact_id == "account.mfa_registered":
        # None is "could not be read", which is not "none registered".
        return GOOD if value is True else WARN if value is False else NEUTRAL
    if fact_id == "account.sign_in_days":
        days = _count(value)
        return WARN if days is not None and days >= SIGN_IN_STALE_DAYS else NEUTRAL
    if fact_id == "account.unlicensed_member":
        return WARN if _on(value) else NEUTRAL
    return NEUTRAL


def findings_for(
    facts: dict[str, Any],
    *,
    ticket: TicketFacts | None = None,
    read_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> list[Finding]:
    """Every finding the snapshot supports, best first.

    `facts` is the enrichment snapshot's own `{id: value}`. Ranked rather than
    filtered: the card shows the first four and offers the rest, and the queue
    shows the first one or two, so the order is the whole design.
    """
    moment = now or dt.datetime.now(dt.UTC)
    said = ticket or TicketFacts()
    stamp = read_at.isoformat() if read_at else None
    found: list[Finding] = []

    # --- 1. the machine is not there ----------------------------------------
    last_seen = _when(_value(facts, "device.last_contact"))
    offline_days = _days(last_seen, now=moment)
    offline = (offline_days is not None and offline_days >= OFFLINE_DAYS) or _on(
        _value(facts, "device.offline")
    )
    if offline:
        # A duration only where there is a date behind it. "Offline for some
        # time" is the kind of sentence that makes a reader stop trusting the
        # rest of the card.
        words = _age_words(offline_days) if offline_days is not None else None
        found.append(
            Finding(
                kind="device_offline",
                rank=1,
                tone=WARNING,
                text=f"Machine offline for {words}" if words else "The machine is offline",
                chip=f"Offline {words}" if words else "Offline",
                # The date, or nothing: "NinjaOne has it down as offline" said
                # the tool's name back to a line that already names the tool
                # and the sentence back to the sentence above it.
                detail=(f"last check-in {last_seen:%d %b}" if last_seen is not None else None),
                source=TOOLS["get_device"],
                as_of=stamp,
            )
        )

    # --- 1b. the account is not there either ---------------------------------
    # The identity half of the same rule: nothing the person signs into will
    # work until somebody enables the account, so it belongs beside the machine
    # being absent rather than among the security lines. A machine that is not
    # there is still said first.
    if _value(facts, "account.enabled") is False:
        found.append(
            Finding(
                kind="account_disabled",
                rank=1,
                tone=WARNING,
                text="The Microsoft 365 account is disabled",
                chip="Account disabled",
                detail="nothing they sign into will work until it is enabled",
                source=TOOLS["get_microsoft365_account"],
                as_of=stamp,
            )
        )

    # --- 2. the ticket or its alert has been sitting -------------------------
    age = _days(said.created_at, now=moment)
    if age is not None and age > STALE_DAYS:
        raised_by_tool = said.origin == "alert"
        # The comparison an engineer would make themselves: an alert raised
        # before the machine went quiet may be about the machine and may be
        # about the thing that made it go quiet.
        # The qualifier belongs in the sentence, which is where it changes how
        # the ticket is read; the line beneath it is for when it was raised.
        before_it_went = offline and offline_days is not None and age > offline_days
        words = f"{'Alert' if raised_by_tool else 'Ticket'} is {_age_words(age)} old"
        found.append(
            Finding(
                kind="alert_stale" if raised_by_tool else "ticket_stale",
                rank=2,
                tone=WARNING,
                text=f"{words}, raised before the machine went offline"
                if before_it_went
                else words,
                chip=f"{_age_words(age)} old",
                detail=(f"raised {said.created_at:%d %b}" if said.created_at else None),
                source=TOOLS["search_tickets"],
                as_of=stamp,
            )
        )

    # --- 3. backup ------------------------------------------------------------
    # One signal, `backup.status`, which is the word the report's own rules
    # decide (`app.reports.tbr.backup.status_of`), so a machine reads the same
    # on a ticket as it does in a review. A customer with no Cove carries
    # `backup.not_assessed` and none of this fires: a tool nobody asked is a
    # gap in the evidence, not a clean bill of health.
    status = _value(facts, "backup.status")
    weekly = _count(_value(facts, "backup.failures_7d"))
    if status == "not_backed_up":
        since = _count(_value(facts, "backup.days_since_successful"))
        found.append(
            Finding(
                kind="backup_none",
                rank=3,
                tone=WARNING,
                # "Nothing has backed up" and "a job broke" are different
                # problems that want different people, so they are never
                # worded as the same thing.
                text="No successful backup on this machine",
                chip=f"No backup for {_age_words(since)}" if since else "Never backed up",
                detail=(
                    f"last succeeded {_plural(since, 'day')} ago"
                    if since
                    else "Cove has never recorded a successful backup"
                ),
                source=COVE,
                as_of=stamp,
            )
        )
    elif status == "failed":
        repeat = weekly is not None and weekly > 1
        found.append(
            Finding(
                kind="backup_repeat_failures" if repeat else "backup_failed",
                rank=3,
                tone=WARNING,
                text=(
                    f"{_plural(weekly or 0, 'backup failure')} this week"
                    if repeat
                    else "The last backup job failed"
                ),
                chip=f"{weekly} failures this week" if repeat else "Backup failed",
                source=COVE,
                as_of=stamp,
            )
        )

    # --- 4. security ----------------------------------------------------------
    if _on(_value(facts, "protection.still_infected")):
        found.append(
            Finding(
                kind="still_infected",
                rank=4,
                tone=WARNING,
                text="The machine is still infected",
                chip="Still infected",
                source=TOOLS["get_endpoint_protection"],
                as_of=stamp,
            )
        )
    elif _on(_value(facts, "protection.malware_24h")):
        found.append(
            Finding(
                kind="malware_detected",
                rank=4,
                tone=WARNING,
                text="Malware detected in the last 24 hours",
                chip="Malware detected",
                source=TOOLS["get_endpoint_protection"],
                as_of=stamp,
            )
        )
    alerts = _count(_value(facts, "alerts.open"))
    if alerts:
        found.append(
            Finding(
                kind="open_alerts",
                rank=4,
                tone=WARNING if alerts > 2 else INFO,
                text=f"{_plural(alerts, 'open alert')} on this machine",
                chip=_plural(alerts, "open alert"),
                source=TOOLS["list_open_alerts"],
                as_of=stamp,
            )
        )
    known_exploited = _count(_value(facts, "findings.known_exploited"))
    if known_exploited:
        found.append(
            Finding(
                kind="known_exploited",
                rank=4,
                tone=WARNING,
                text=f"{_plural(known_exploited, 'known-exploited vulnerability')} found",
                chip=f"{known_exploited} known-exploited",
                detail="CISA's list of what is being exploited now",
                source=TOOLS["get_asset_vulnerabilities"],
                as_of=stamp,
            )
        )
    critical = _count(_value(facts, "findings.critical"))
    if critical:
        found.append(
            Finding(
                kind="critical_findings",
                rank=4,
                tone=INFO,
                text=f"{_plural(critical, 'confirmed critical vulnerability')} found",
                chip=f"{critical} critical",
                source=TOOLS["get_asset_vulnerabilities"],
                as_of=stamp,
            )
        )
    if _value(facts, "protection.agent") is False:
        found.append(
            Finding(
                kind="no_protection_agent",
                rank=4,
                tone=WARNING,
                text="No protection agent on this machine",
                chip="No protection agent",
                source=TOOLS["identity"],
                as_of=stamp,
            )
        )
    if _on(_value(facts, "protection.signatures_outdated")):
        found.append(
            Finding(
                kind="signatures_outdated",
                rank=4,
                tone=INFO,
                text="Antivirus signatures out of date",
                chip="Signatures out of date",
                # The qualifier that stops somebody chasing it: a machine that
                # is not there cannot have updated anything.
                detail="expected while offline" if offline else None,
                source=TOOLS["get_endpoint_protection"],
                as_of=stamp,
            )
        )

    # A second factor nobody has registered is a security finding about the
    # person rather than the machine. `None` is not "no": a disabled account has
    # no row in Microsoft's registration report at all, and a tenant may not
    # have consented to the report being read.
    if _value(facts, "account.mfa_registered") is False:
        found.append(
            Finding(
                kind="no_mfa_registered",
                rank=4,
                tone=WARNING,
                text="No MFA registered on the Microsoft 365 account",
                chip="No MFA",
                source=TOOLS["get_microsoft365_account"],
                as_of=stamp,
            )
        )

    # --- 5. somebody has seen this before -------------------------------------
    if said.repeat_of:
        ago = _days(said.repeat_at, now=moment)
        found.append(
            Finding(
                kind="repeat_machine",
                rank=5,
                tone=REPEAT,
                text="The same thing came in before on this machine",
                chip="Repeat",
                detail=f"{_age_words(ago)} ago" if ago is not None else None,
                source=TOOLS["search_tickets"],
                as_of=stamp,
                tickets=[
                    {
                        "id": said.repeat_of,
                        "number": said.repeat_of_number or said.repeat_of,
                    }
                ],
            )
        )
    on_device = _count(_value(facts, "tickets.device"))
    if on_device:
        found.append(
            Finding(
                kind="other_tickets_device",
                rank=5,
                tone=REPEAT,
                text=f"{_plural(on_device, 'other ticket')} for this machine in 90 days",
                chip=f"{on_device} more on this machine",
                source=TOOLS["search_tickets"],
                as_of=stamp,
            )
        )
    from_contact = _count(_value(facts, "tickets.contact"))
    if from_contact:
        found.append(
            Finding(
                kind="other_tickets_contact",
                rank=5,
                tone=REPEAT,
                text=f"{_plural(from_contact, 'other ticket')} from this person in 90 days",
                chip="Same user",
                source=TOOLS["search_tickets"],
                as_of=stamp,
            )
        )

    # --- 6. housekeeping ------------------------------------------------------
    if _on(_value(facts, "device.needs_reboot")):
        found.append(
            Finding(
                kind="pending_reboot",
                rank=6,
                tone=INFO,
                text="The machine is waiting for a reboot",
                chip="Pending reboot",
                source=TOOLS["get_device"],
                as_of=stamp,
            )
        )
    failed = _count(_value(facts, "patches.failed"))
    if failed:
        found.append(
            Finding(
                kind="patch_failures",
                rank=6,
                tone=INFO,
                text=f"{_plural(failed, 'patch install')} failed recently",
                chip=f"{_plural(failed, 'patch')} failed",
                source=TOOLS["list_patch_installs"],
                as_of=stamp,
            )
        )
    # The account's housekeeping, which is where it changes the handling least:
    # worth knowing while somebody is in the ticket, never the reason to open it.
    dormant = _count(_value(facts, "account.sign_in_days"))
    if (
        dormant is not None
        and dormant >= SIGN_IN_STALE_DAYS
        # A disabled account has its own line above; saying both reads as two
        # findings about one thing.
        and _value(facts, "account.enabled") is not False
    ):
        found.append(
            Finding(
                kind="account_dormant",
                rank=6,
                tone=INFO,
                text=f"No Microsoft 365 sign-in for {_age_words(dormant)}",
                chip=f"No sign-in {_age_words(dormant)}",
                source=TOOLS["get_microsoft365_account"],
                as_of=stamp,
            )
        )
    if _on(_value(facts, "account.unlicensed_member")):
        found.append(
            Finding(
                kind="unlicensed_account",
                rank=6,
                tone=INFO,
                text="The Microsoft 365 account has no licence",
                chip="No licence",
                detail="an enabled account for a person, with nothing assigned",
                source=TOOLS["get_microsoft365_account"],
                as_of=stamp,
            )
        )
    low = _count(_value(facts, "device.disks_low"))
    if low:
        found.append(
            Finding(
                kind="disk_low",
                rank=6,
                tone=INFO,
                text=f"{_plural(low, 'volume')} under 10% free",
                chip="Disk nearly full",
                source=TOOLS["get_device"],
                as_of=stamp,
            )
        )

    # Stable within a rank: the order above is the order they were written, and
    # two runs over the same snapshot must put them in the same order or the
    # queue's chip changes for no reason.
    return sorted(found, key=lambda one: one.rank)


# --- what to do about it -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class NextStep:
    """One sentence, decided by rules from the findings and nothing else."""

    text: str
    because: str
    """Which finding produced it, for the review row and for anybody asking
    why the card said that."""


#: The rules, best first. **Not the model**: a next step the model wrote would
#: be a suggestion nobody can check, and the whole card is built on being able
#: to say where every line came from. First match wins; no match shows no
#: section at all, because a next step nobody needs is noise on every ticket.
def next_step_for(found: list[Finding]) -> NextStep | None:
    kinds = {one.kind for one in found}
    if "device_offline" in kinds:
        return NextStep(
            text="Contact the user to bring the machine online before any remote cleanup.",
            because="device_offline",
        )
    if kinds & {"alert_stale", "ticket_stale"}:
        return NextStep(
            text="Check the alert still applies before acting.",
            because="alert_stale" if "alert_stale" in kinds else "ticket_stale",
        )
    if kinds & {"repeat_machine", "other_tickets_device", "other_tickets_contact"}:
        return NextStep(
            text="Look at how the earlier tickets were closed.",
            because=next(
                one
                for one in ("repeat_machine", "other_tickets_device", "other_tickets_contact")
                if one in kinds
            ),
        )
    if kinds & {"still_infected", "malware_detected"}:
        return NextStep(
            text="Isolate the machine before anything else.",
            because="still_infected" if "still_infected" in kinds else "malware_detected",
        )
    if kinds & {"backup_failed", "backup_repeat_failures"}:
        return NextStep(
            text="Check the backup job's own error before re-running it.",
            because="backup_repeat_failures"
            if "backup_repeat_failures" in kinds
            else "backup_failed",
        )
    return None


__all__ = [
    "INFO",
    "MAX_SHOWN",
    "OFFLINE_DAYS",
    "REPEAT",
    "REPEAT_DAYS",
    "SIGN_IN_STALE_DAYS",
    "STALE_DAYS",
    "TOOLS",
    "WARNING",
    "Finding",
    "NextStep",
    "TicketFacts",
    "findings_for",
    "next_step_for",
    "tone_for",
]

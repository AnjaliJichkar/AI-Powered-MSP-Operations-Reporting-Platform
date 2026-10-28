"""The enrichment panel: what every tool knows about the ticket's device, as of now.

Computed in code from the tools an engineer would call, never by a model:
`get_device` (state, last contact, pending reboot), `list_open_alerts`,
`get_endpoint_protection`, `get_asset_vulnerabilities` (confirmed critical
and known-exploited counts), `list_patch_installs` (failures in the window),
`list_tickets_for_device` and `list_tickets_for_contact`. Each vendor is
asked by the name its own identity link carries. Every read is a `tool_call`
audit row; a tool that errors is an entry in `errors`, never a blank fact.

The contact gets a block of their own: the Microsoft 365 account behind the
address Autotask holds for them (`get_microsoft365_account`) — enabled, a
second factor, when they last signed in, what is licensed. Matched by email
and nothing looser: a name matches several people in a directory. A client
with no Microsoft 365 is **not assessed** rather than an error, which is the
coverage board's rule applied to one panel.

The result is a list of facts, each with a stable id, a label, a value and
its source, stamped with an `as_of` time and stored on the workspace so a
plan can cite `fact:<id>` and the page can say how old the panel is.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.autopilot.findings import tone_for
from app.connectors.autotask.tools import (
    list_tickets_for_contact,
    list_tickets_for_device,
    search_tickets,
)
from app.connectors.bitdefender.tools import get_endpoint_protection
from app.connectors.connectsecure.tools import get_asset_vulnerabilities
from app.connectors.errors import ConnectorError
from app.connectors.factory import connector_for, why_not_configured
from app.connectors.matching import normalise_hostname
from app.connectors.microsoft365.tools import get_microsoft365_account
from app.connectors.ninjaone.tools import get_device, list_open_alerts, list_patch_installs
from app.db.session import session_scope
from app.models.client import Client
from app.models.device import Device, DeviceVendorLink
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName
from app.models.site import Site
from app.reports.tbr.backup import days_since_success, rate_for, status_of
from app.settings import get_settings

logger = logging.getLogger(__name__)

ToolCall = Callable[[], Awaitable[Any]]
SHOWN = 5

#: What a backup fact says it came from, in one place so the panel and a
#: plan citing `fact:backup.status` name the same read.
COVE_SOURCE = "cove.list_devices"

#: The window the panel's failure count covers. Deliberately not the review's
#: 28 days: a ticket is about this week, and the status beside it is about now.
BACKUP_RECENT_DAYS = 7


@dataclass(slots=True)
class Fact:
    id: str
    label: str
    value: Any
    source: str
    detail: str | None = None
    rests_on: tuple[str, ...] = ()
    """Tools this reading depends on besides the one that decided it.

    `source` is what a plan cites and stays one thing. But "no Cove device is
    linked to this machine" is decided by the identity layer *and* rests on
    having read Cove, and a panel whose "from" line left Cove out would be
    naming where the sentence was written rather than what it took to write
    it. A reader checking whether a tool was asked needs the second answer.
    """

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "value": self.value,
            "source": self.source,
            "rests_on": list(self.rests_on),
            "detail": self.detail,
            # The dot a page draws beside this fact. Computed by the rules that
            # rank the findings (`app.autopilot.findings.tone_for`) rather than
            # by the page, so "offline" means the same thing wherever it is
            # drawn, and never by colour alone: every reader of this writes the
            # word beside the dot.
            "tone": tone_for(self.id, self.value),
        }


@dataclass(slots=True)
class Enrichment:
    as_of: dt.datetime
    facts: list[Fact] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    reads: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "facts": [f.as_dict() for f in self.facts],
            "errors": self.errors,
            "reads": self.reads,
        }

    def fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fact_id), None)


async def _record_failure(
    context: AuditContext,
    *,
    connector: ConnectorName,
    tool: str,
    duration_ms: int,
    exc: Exception,
    payload: dict[str, Any],
) -> None:
    """One `tool_call` error row, in its own transaction.

    Audit is not the thing that gets to fail a run, and it is not the thing
    that gets to hide one either: if the row cannot be written — the database
    is where the trouble is, say — the vendor's error is still what the caller
    is told, and the failure to record it is logged rather than raised.
    """
    try:
        async with session_scope() as audit_session:
            await record_event(
                audit_session,
                context,
                event_type=AuditEventType.TOOL_CALL,
                outcome=AuditOutcome.ERROR,
                connector_name=connector,
                tool_name=tool,
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
                error_message=str(exc)[:300],
                payload=payload,
            )
    except Exception:
        logger.exception(
            "could not record the failed %s read of %s on the audit log", connector.value, tool
        )


async def audited_read(
    session: AsyncSession,
    context: AuditContext,
    *,
    connector: ConnectorName,
    tool: str,
    call: ToolCall,
    payload: dict[str, Any] | None = None,
) -> tuple[Any | None, str | None]:
    """One tool read on the workspace's behalf, audited as the tool call it is."""
    started = time.monotonic()
    try:
        result = await call()
    except (ConnectorError, ValueError) as exc:
        # **On a connection of its own, and committed.** A failed read is often
        # the last thing that happens in its caller's transaction: the ledger
        # raises on it and `session_scope` rolls back, which took the failure
        # row with it and left the run unrecorded — hard rule 4 says the record
        # is what happened, and a vendor refusing us is exactly what somebody
        # needs to find later. The caller's session is untouched, so this says
        # nothing about whatever that transaction goes on to do.
        await _record_failure(
            context,
            connector=connector,
            tool=tool,
            duration_ms=int((time.monotonic() - started) * 1000),
            exc=exc,
            payload={"source": "ticket workspace", **(payload or {})},
        )
        return None, f"{type(exc).__name__}: {exc}"
    await record_event(
        session,
        context,
        event_type=AuditEventType.TOOL_CALL,
        outcome=AuditOutcome.SUCCESS,
        connector_name=connector,
        tool_name=tool,
        duration_ms=int((time.monotonic() - started) * 1000),
        payload={"source": "ticket workspace", **(payload or {})},
    )
    return result, None


def _vendor_name(links: Sequence[DeviceVendorLink], connector: ConnectorName) -> str | None:
    row = next((r for r in links if r.connector_name is connector and r.active), None)
    return row.vendor_name if row and row.vendor_name else None


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, dt.datetime) else value


def _ticket_rows(tickets: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "ticket_number": t.ticket_number,
            "title": t.title,
            "status": t.status,
            "created_at": _iso(t.created_at),
            "is_open": (
                t.is_open if hasattr(t, "is_open") else getattr(t, "completed_at", None) is None
            ),
        }
        for t in tickets[:SHOWN]
    ]


#: The tool the Account block is read with. One name in one place: the page
#: prints it and the findings cite it.
ACCOUNT_SOURCE = "get_microsoft365_account"


def _reason(state: Any, item: str) -> str | None:
    """Why this tenant could not answer one item, in its own words.

    A premium-licence item and a permission nobody consented to are facts about
    the tenant, not failures: the account carries them and the page prints them
    under the reading they belong to.
    """
    for row in getattr(state, "not_available", []):
        if row.item == item:
            return str(row.reason)
    return None


def _account_facts(state: Any, *, email: str) -> list[Fact]:
    """The Account block: who the contact is in Microsoft 365, and its state.

    Short on purpose — enabled, a second factor, how long since a sign-in, what
    is licensed — because this is a block under the machine's readings and not
    a directory browser. Two readings appear only when they are the exception:
    a mailbox that is not a person's, and a member account with nothing
    assigned.
    """
    if not state.found:
        return [
            Fact(
                "account.found",
                "Microsoft 365 account",
                False,
                ACCOUNT_SOURCE,
                detail=f"no account in this customer's tenant has the address {email}",
            )
        ]
    about = " · ".join(
        part for part in (state.display_name, state.job_title, state.department) if part
    )
    days = state.days_since_sign_in
    signed_in = None
    if state.last_sign_in:
        signed_in = f"last signed in {state.last_sign_in[:10]}"
    facts = [
        Fact("account.found", "Microsoft 365 account", state.user, ACCOUNT_SOURCE, detail=about),
        Fact(
            "account.enabled",
            "Account enabled",
            state.enabled,
            ACCOUNT_SOURCE,
            detail="the account cannot sign in" if state.enabled is False else None,
        ),
        Fact(
            "account.mfa_registered",
            "MFA registered",
            state.mfa_registered,
            ACCOUNT_SOURCE,
            detail=", ".join(state.mfa_methods) or _reason(state, "MFA registration"),
        ),
        # A count of days rather than a timestamp, because that is the thing a
        # finding is made of and because a snapshot keeps a number and drops a
        # string. The date itself is the line beneath it, so nothing is lost.
        Fact(
            "account.sign_in_days",
            "Days since last sign-in",
            days,
            ACCOUNT_SOURCE,
            detail=signed_in or _reason(state, "last sign-in") or "no sign-in recorded",
        ),
        Fact(
            "account.licences",
            "Microsoft 365 licences",
            list(state.licences),
            ACCOUNT_SOURCE,
            detail=None if state.licences else "nothing is assigned to this account",
        ),
    ]
    if state.mailbox_kind and state.mailbox_kind != "user":
        facts.append(
            Fact(
                "account.mailbox_kind",
                "Mailbox",
                state.mailbox_kind,
                ACCOUNT_SOURCE,
                detail="not a person's mailbox, so it registers no second factor and needs no "
                "licence of its own",
            )
        )
    # The rule for what counts as unlicensed is the connector's own
    # (`Account.unlicensed_member`), so the card, the panel and an answer in Ask
    # cannot come to three views of one account.
    if state.unlicensed_member:
        facts.append(
            Fact(
                "account.unlicensed_member",
                "Unlicensed member account",
                True,
                ACCOUNT_SOURCE,
                detail="an enabled account for a person, with no licence assigned",
            )
        )
    return facts


async def enrich(
    session: AsyncSession,
    context: AuditContext,
    *,
    client_id: uuid.UUID,
    device: Device | None,
    links: Sequence[DeviceVendorLink],
    contact: str | None,
    site_id: uuid.UUID | None,
    ticket_number: str | None,
    contact_email: str | None = None,
    now: dt.datetime | None = None,
) -> Enrichment:
    """Every fact the tools can give about the device (and the contact), as of now."""
    now = now or dt.datetime.now(dt.UTC)
    settings = get_settings()
    out = Enrichment(as_of=now)
    client = str(client_id)
    site_ref = str(site_id) if site_id else None
    days = settings.ticket_recent_days

    async def read(
        connector: ConnectorName, tool: str, call: ToolCall, **payload: Any
    ) -> Any | None:
        out.reads += 1
        result, error = await audited_read(
            session, context, connector=connector, tool=tool, call=call, payload=payload
        )
        if error:
            out.errors.append({"source": tool, "error": error})
        return result

    if device is not None:
        name = device.canonical_name
        key = normalise_hostname(name)
        ninja_name = _vendor_name(links, ConnectorName.NINJAONE)

        # A machine only another vendor knows is not asked of NinjaOne: that it
        # is not managed is the fact.
        detail = (
            await read(
                ConnectorName.NINJAONE,
                "get_device",
                lambda: get_device(client, ninja_name or name, site_ref),
            )
            if ninja_name
            else None
        )
        out.facts.append(
            Fact(
                "device.managed",
                "Managed in NinjaOne",
                bool(ninja_name),
                "identity",
                detail=None if ninja_name else "no NinjaOne record is linked to this machine",
            )
        )
        if detail is not None and getattr(detail, "found", False):
            d: Any = detail
            out.facts += [
                Fact("device.offline", "Offline in NinjaOne", d.offline, "get_device"),
                Fact(
                    "device.last_contact",
                    "Last NinjaOne contact",
                    _iso(d.last_contact),
                    "get_device",
                ),
                Fact(
                    "device.os",
                    "Operating system",
                    " ".join(str(p) for p in (d.os_name, d.os_build) if p) or None,
                    "get_device",
                ),
                Fact("device.last_boot", "Last boot", _iso(d.last_boot), "get_device"),
                Fact("device.needs_reboot", "Pending reboot", d.needs_reboot, "get_device"),
                Fact(
                    "device.last_user", "Last logged-on user", d.last_logged_in_user, "get_device"
                ),
                Fact(
                    "device.disks_low",
                    "Volumes under 10% free",
                    [
                        f"{v.volume} {v.free_percent:g}%"
                        for v in d.volumes
                        if v.free_percent is not None and v.free_percent < 10
                    ],
                    "get_device",
                ),
            ]
        elif detail is not None:
            out.errors.append(
                {"source": "get_device", "error": str(getattr(detail, "message", ""))}
            )

        alerts = await read(
            ConnectorName.NINJAONE,
            "list_open_alerts",
            lambda: list_open_alerts(client, site_ref),
        )
        if alerts is not None:
            mine = [a for a in alerts.alerts if normalise_hostname(a.device_name) == key]
            out.facts.append(
                Fact(
                    "alerts.open",
                    "Open NinjaOne alerts on this device",
                    len(mine),
                    "list_open_alerts",
                    detail="; ".join(
                        f"{a.severity or ''} {a.subject or a.message or a.condition or ''}".strip()
                        for a in mine[:SHOWN]
                    )
                    or None,
                )
            )

        bd_name = _vendor_name(links, ConnectorName.BITDEFENDER)
        if bd_name:
            protection = await read(
                ConnectorName.BITDEFENDER,
                "get_endpoint_protection",
                lambda: get_endpoint_protection(client, bd_name, site_ref),
            )
            if protection is not None:
                p: Any = protection
                out.facts += [
                    Fact(
                        "protection.agent",
                        "Bitdefender agent installed",
                        p.agent_installed,
                        "get_endpoint_protection",
                    ),
                    Fact(
                        "protection.agent_outdated",
                        "Bitdefender agent outdated",
                        p.agent_outdated,
                        "get_endpoint_protection",
                    ),
                    Fact(
                        "protection.signatures_outdated",
                        "Bitdefender signatures outdated",
                        p.signatures_outdated,
                        "get_endpoint_protection",
                    ),
                    Fact(
                        "protection.last_seen",
                        "Bitdefender last seen",
                        _iso(p.last_seen),
                        "get_endpoint_protection",
                    ),
                    Fact(
                        "protection.modules_disabled",
                        "Bitdefender modules off by policy",
                        p.modules_disabled,
                        "get_endpoint_protection",
                    ),
                    Fact(
                        "protection.malware_24h",
                        "Malware detected in 24 hours",
                        p.malware_detected_last_24h,
                        "get_endpoint_protection",
                    ),
                    Fact(
                        "protection.still_infected",
                        "Still infected",
                        p.still_infected,
                        "get_endpoint_protection",
                    ),
                    Fact(
                        "protection.isolated",
                        "Isolated by Bitdefender",
                        p.isolated,
                        "get_endpoint_protection",
                    ),
                ]
        else:
            out.facts.append(
                Fact(
                    "protection.agent",
                    "Bitdefender agent installed",
                    False,
                    "identity",
                    detail="no Bitdefender record is linked to this machine",
                )
            )

        # Backup, under the coverage board's rule: a customer with no Cove is
        # not assessed, which is decided before the read so an absence is
        # never reported as a failed one.
        no_backup = await why_not_configured(client_id, ConnectorName.COVE, site_id)
        if no_backup is not None:
            out.facts.append(
                Fact(
                    "backup.not_assessed",
                    "Backup",
                    None,
                    "identity",
                    detail=f"Cove is not set up for this customer, so backup was not "
                    f"assessed ({no_backup})",
                )
            )
        else:
            # One read of the customer's own Cove rows: the machine comes out
            # of it by the name its link carries, and the rest of it decides
            # the site's scope where that is `auto`.
            cove_rows = await read(
                ConnectorName.COVE,
                COVE_SOURCE,
                lambda: _cove_devices(client, site_id=site_ref),
            )
            cove_name = _vendor_name(links, ConnectorName.COVE)
            # Found through the identity layer's link and never by a second
            # name join of our own: the coverage gap has one join, and a panel
            # that matched on the name here could disagree with the board.
            machine = (
                next((d for d in cove_rows if d.is_machine and d.match_key == cove_name), None)
                if cove_rows is not None and cove_name
                else None
            )
            if machine is not None:
                m: Any = machine
                out.facts += [
                    Fact(
                        "backup.status",
                        "Backup",
                        status_of(m, now=now),
                        COVE_SOURCE,
                        detail=f"{m.product} retention" if m.product else None,
                    ),
                    # The days are what the finding rests on and the date is
                    # the evidence under it: a panel that printed only a
                    # timestamp would make every reader do the subtraction.
                    Fact(
                        "backup.days_since_successful",
                        "Days since a successful backup",
                        days_since_success(m, now=now),
                        COVE_SOURCE,
                    ),
                    Fact(
                        "backup.last_successful",
                        "Last successful backup",
                        _iso(m.last_successful_at),
                        COVE_SOURCE,
                    ),
                    # A week rather than the review's 28 days: on a ticket the
                    # question is whether this is the third failure somebody
                    # has looked at, not what the month came to.
                    Fact(
                        "backup.failures_7d",
                        "Failed backups in 7 days",
                        rate_for(m, now=now, window=BACKUP_RECENT_DAYS).failures,
                        COVE_SOURCE,
                    ),
                ]
            elif cove_rows is not None:
                # Expected to have a backup, or not? The site's own setting,
                # and the same `effective_scope` the review applies — a
                # workstation at a site that backs up servers only is not a
                # finding, and calling it one puts a red line on every laptop
                # of a customer who never bought it.
                expected, why = await _backup_expected(
                    session,
                    device,
                    machines=[d for d in cove_rows if d.is_machine],
                    client_id=client_id,
                    site_id=site_id,
                )
                # One fact id either way, because both are the same question
                # answered: a machine out of scope is assessed and expected to
                # have nothing, which is not the same as not assessed at all.
                out.facts.append(
                    Fact(
                        "backup.status",
                        "Backup",
                        "not_backed_up" if expected else "not_in_scope",
                        "identity",
                        detail=("no Cove device is linked to this machine" if expected else why),
                        # Decided here, but only because Cove answered: the
                        # customer's rows are what say this machine is not among
                        # them, and an `auto` scope is read from them too.
                        rests_on=(COVE_SOURCE,),
                    )
                )

        cs_name = _vendor_name(links, ConnectorName.CONNECTSECURE)
        if cs_name:
            findings = await read(
                ConnectorName.CONNECTSECURE,
                "get_asset_vulnerabilities",
                lambda: get_asset_vulnerabilities(client, cs_name, site_id=site_ref),
            )
            if findings is not None:
                f: Any = findings
                counts = f.counts_by_severity or {}
                out.facts += [
                    Fact(
                        "findings.critical",
                        "Confirmed critical findings",
                        counts.get("critical", 0),
                        "get_asset_vulnerabilities",
                        detail=f.population,
                    ),
                    Fact(
                        "findings.known_exploited",
                        "Confirmed known-exploited findings",
                        f.known_exploited,
                        "get_asset_vulnerabilities",
                    ),
                    Fact(
                        "findings.last_scanned",
                        "Last vulnerability scan",
                        _iso(f.last_scanned),
                        "get_asset_vulnerabilities",
                    ),
                    Fact(
                        "findings.risk_grade",
                        "ConnectSecure risk grade",
                        f.risk_grade,
                        "get_asset_vulnerabilities",
                    ),
                    Fact(
                        "findings.worst",
                        "Worst confirmed findings",
                        [
                            f"{v.severity or ''} {v.vulnerability} "
                            f"({v.product or '?'}): {v.fix}".strip()
                            for v in f.vulnerabilities[:SHOWN]
                        ],
                        "get_asset_vulnerabilities",
                    ),
                ]
        else:
            out.facts.append(
                Fact(
                    "findings.critical",
                    "Confirmed critical findings",
                    None,
                    "identity",
                    detail="no ConnectSecure record is linked to this machine",
                )
            )

        patches = await read(
            ConnectorName.NINJAONE,
            "list_patch_installs",
            lambda: list_patch_installs(client, days=days, status="failed", site_id=site_ref),
            days=days,
        )
        if patches is not None:
            failed = [r for r in patches.rows if normalise_hostname(r.device) == key]
            out.facts.append(
                Fact(
                    "patches.failed",
                    f"Failed patch installs in {days} days",
                    len(failed),
                    "list_patch_installs",
                    detail="; ".join(
                        f"{r.patch}{f' ({r.kb})' if r.kb else ''}" for r in failed[:SHOWN]
                    )
                    or None,
                )
            )

        history = await read(
            ConnectorName.AUTOTASK,
            "list_tickets_for_device",
            lambda: list_tickets_for_device(client, name, site_ref),
        )
        rows: list[Any] = []
        source = "list_tickets_for_device"
        if history is not None and getattr(history, "matched_devices", []):
            rows = list(history.tickets)
        else:
            # Configuration items are not always readable, or the machine is not
            # one: the text search by name is the tool's own fallback.
            found = await read(
                ConnectorName.AUTOTASK,
                "search_tickets",
                lambda: search_tickets(client, name, days=365, site_id=site_ref),
            )
            source = "search_tickets"
            if found is not None:
                rows = list(found.tickets)
        rows = [t for t in rows if t.ticket_number != ticket_number]
        out.facts.append(
            Fact(
                "tickets.device",
                "Other tickets for this device",
                len(rows),
                source,
                detail="; ".join(
                    f"{t.ticket_number} {t.title or ''} [{t.status or ''}]" for t in rows[:SHOWN]
                )
                or None,
            )
        )
        out.facts[-1].value = {"count": len(rows), "recent": _ticket_rows(rows)}

    if contact_email:
        address = contact_email
        # Decided before the read, because "we have no Microsoft 365 for this
        # customer" is not a failed read and must not be reported as one.
        no_access = await why_not_configured(client_id, ConnectorName.MICROSOFT365, site_id)
        if no_access is not None:
            out.facts.append(
                Fact(
                    "account.not_assessed",
                    "Microsoft 365 account",
                    None,
                    "identity",
                    detail="Microsoft 365 is not set up for this customer, so the account was "
                    f"not assessed ({no_access})",
                )
            )
        else:
            state = await read(
                ConnectorName.MICROSOFT365,
                ACCOUNT_SOURCE,
                lambda: get_microsoft365_account(client, address, site_ref),
            )
            if state is not None:
                out.facts += _account_facts(state, email=address)

    if contact:
        theirs = await read(
            ConnectorName.AUTOTASK,
            "list_tickets_for_contact",
            lambda: list_tickets_for_contact(client, contact, site_ref),
        )
        if theirs is not None:
            rows = [t for t in theirs.tickets if t.ticket_number != ticket_number]
            out.facts.append(
                Fact(
                    "tickets.contact",
                    "Other tickets from this contact",
                    {"count": len(rows), "recent": _ticket_rows(rows)},
                    "list_tickets_for_contact",
                    detail="; ".join(
                        f"{t.ticket_number} {t.title or ''} [{t.status or ''}]"
                        for t in rows[:SHOWN]
                    )
                    or None,
                )
            )
    return out


async def _cove_devices(client_id: str, *, site_id: str | None) -> list[Any]:
    """Every row Cove holds for this customer.

    Cove has no read for a single device, so the machine on the panel comes
    out of the customer's listing — the same read the review makes — and the
    rest of the listing is what decides an `auto` backup scope.

    The customer's own read and not the fleet one: a fleet read covers the
    whole allowlist, so a machine of this name at a *sibling* site would come
    back from it, and a panel is about one site's machine. A customer whose
    Cove account holds nothing returns nothing rather than raising: that they
    have no devices is a fact about them.
    """
    from app.connectors.cove.client import CoveClient

    out: list[Any] = []
    async with connector_for(client_id, ConnectorName.COVE, site_id) as connector:
        assert isinstance(connector, CoveClient)
        for _, devices in await connector.list_devices_for_targets():
            out += devices
    return out


async def _backup_expected(
    session: AsyncSession,
    device: Device,
    *,
    machines: Sequence[Any],
    client_id: uuid.UUID,
    site_id: uuid.UUID | None,
) -> tuple[bool, str]:
    """Whether this machine is one the site expects to be backed up, and why.

    The franchisee's own settings and `effective_scope`, which is the rule the
    review's gap list uses, so the panel and the deck cannot come to two views
    of one laptop. A machine the site has already decided needs no backup is
    out of scope by name, as it is there.
    """
    from sqlalchemy import select

    from app.connectors.cove.columns import device_key
    from app.connectors.ninjaone.hardware import device_role
    from app.reports.tbr.backup import effective_scope, is_not_a_machine
    from app.reports.tbr.config import load_config

    site = await session.get(Site, site_id) if site_id else None
    client = await session.get(Client, client_id)
    if site is None or client is None:  # pragma: no cover - a workspace always has both
        return True, ""
    settings = (await load_config(session, client_id, client_name=client.name)).backup
    name = device.canonical_name
    if is_not_a_machine(name):
        return False, "a network appliance rather than a computer"
    if device_key(name) in settings.excluded_for(site.slug):
        return False, "this site has recorded that this machine needs no backup"
    configured = settings.scope_for(site.slug)
    # How many workstations this customer has, from the identity layer: the
    # denominator `auto` asks about. One query, and the same question the
    # board and the review ask, so a laptop cannot read as out of scope here
    # and as a gap there.
    rows = (
        await session.execute(
            select(Device.node_class).where(
                Device.client_id == client_id,
                Device.site_id == site.id,
                Device.status != "retired",
            )
        )
    ).scalars()
    workstations = sum(1 for node_class in rows if device_role(None, node_class).role != "server")
    decision = effective_scope(configured, machines=machines, workstations=workstations)
    scope, why = decision.scope, decision.why
    if scope == "servers" and device_role(None, device.node_class).role != "server":
        # Said in a sentence rather than by reshaping the review's own line:
        # which rule applied is the thing a reader needs, and "servers only,
        # as no workstations are backed up here" twice over reads as a stutter.
        return False, (
            "servers only at this site, by its backup setting"
            if configured == "servers"
            else "servers only at this site, as no workstations are backed up in Cove here"
        )
    return True, why

"""The note actions: fixed wording filled from reads, one block per note.

Each takes the ticket, the device match and the reads its workflow made —
never a tool: the reads were made by the read actions before it, or were
skipped because the match was not confident — and returns one `note`: a
`Draft` (title, body, the facts it was filled from, the reads that failed)
or a `Skip` with the reason no note is written. The wording is in this file
and nowhere else; `tests/api/test_automation_notes.py` pins every note word
for word, so a change here is a change to what lands on a client's ticket.

A read that is missing (its step was skipped) or that failed is handled
exactly as the note's definition of its core fact says: the core fact
missing is a skip; any other is one line in the note saying which tool did
not answer.

`notes.threat_gate` is the one decision block: a Blackpoint (MDR) alert goes
on to the detections without Bitdefender's endpoint state, because its core
is the instruction to investigate; any other threat alert does not.
"""

from __future__ import annotations

from typing import Any

from app.automation.actions.base import (
    Action,
    ActionContext,
    Input,
    Output,
    register_action,
)
from app.automation.definitions import Facts
from app.automation.notes.backup_formats import parse_backup_notice
from app.automation.notes.definitions import (
    DeviceMatch,
    Draft,
    Fact,
    Read,
    ReadFailure,
    Skip,
    about,
    contains_any,
    minutes_between,
    names_another_device,
    normalised_kind,
    when,
)
from app.automation.notes.workflows import DISK_PHRASES, MDR_LINE, MDR_PHRASES
from app.automation.triggers import Arrival

NOT_CONFIDENT = "the ticket's machine could not be matched confidently"

TICKET = Input("ticket", "The ticket an alert raised.")
DEVICE = Input("device", "The machine, from devices.resolve.", required=False)
NOTE = {"note": Output("note", "The note to add, or the reason none is written.")}


def _failure(read: Read[Any], what: str) -> ReadFailure:
    return ReadFailure(
        source=read.source, what=what, reason=read.error or "no answer", read_at=read.read_at
    )


def _fact(fact_id: str, label: str, value: Any, read: Read[Any]) -> Fact:
    return Fact(id=fact_id, label=label, value=value, source=read.source, read_at=read.read_at)


def _device(ctx: ActionContext) -> DeviceMatch | None:
    device = ctx.inputs.get("device")
    return device if isinstance(device, DeviceMatch) else None


def _read(ctx: ActionContext, name: str) -> Read[Any] | None:
    read = ctx.inputs.get(name)
    return read if isinstance(read, Read) else None


def _ticket(ctx: ActionContext) -> Arrival:
    ticket = ctx.inputs["ticket"]
    assert isinstance(ticket, Arrival)
    return ticket


# --- a. device back online --------------------------------------------------------------


async def device_online(ctx: ActionContext) -> Facts:
    """Core fact: the device's state in NinjaOne. Other open alerts are a line
    that says so when they could not be read."""
    ticket = _ticket(ctx)
    device = _device(ctx)
    if device is None or not device.confident:
        return {"note": Skip(NOT_CONFIDENT)}
    state = _read(ctx, "state")
    if state is None or not state.ok or state.value is None:
        source = state.source if state is not None else "NinjaOne"
        error = state.error if state is not None else "not read"
        return {"note": Skip(f"{source} did not answer for the device state ({error})")}
    facts: list[Fact] = [
        _fact("device.online", "Online", state.value.online, state),
        _fact("device.last_contact", "Last contact", state.value.last_contact, state),
        _fact("device.last_boot", "Last boot", state.value.last_boot, state),
    ]
    failures: list[ReadFailure] = []
    alerts = _read(ctx, "alerts")
    if alerts is not None and alerts.ok and alerts.value is not None:
        facts.append(_fact("device.open_alerts", "Other open alerts", len(alerts.value), alerts))
    else:
        failures.append(
            _failure(
                alerts or Read.failed(state.source, "not read", at=state.read_at),
                "Other open alerts",
            )
        )

    raised = ticket.created_at
    if state.value.online:
        down = minutes_between(raised, state.value.last_contact)
        lines = [
            f"Device {device.name} is back online: last contact {when(state.value.last_contact)}"
            + (f" (alert raised {when(raised)}, down {about(down)})." if raised else "."),
            f"Last boot {when(state.value.last_boot)}.",
        ]
        if alerts is not None and alerts.ok and alerts.value is not None:
            if alerts.value:
                subjects = "; ".join(a.subject for a in alerts.value[:5])
                lines.append(f"{len(alerts.value)} other open alert(s) on this device: {subjects}.")
            else:
                lines.append("No other open alerts.")
        title = f"{device.name} back online"
    else:
        since = minutes_between(state.value.last_contact, ctx.now)
        lines = [
            f"Device {device.name} is still offline: last contact {when(state.value.last_contact)}"
            + (f" ({about(since)} ago)." if since is not None else "."),
            f"Last boot {when(state.value.last_boot)}.",
        ]
        if alerts is not None and alerts.ok and alerts.value:
            lines.append(f"{len(alerts.value)} other open alert(s) on this device.")
        title = f"{device.name} still offline"
    lines.extend(f.line for f in failures)
    lines.append(f"Read by Desk at {when(state.read_at)}.")
    return {
        "note": Draft(
            title=title,
            body="\n".join(lines),
            facts=tuple(facts),
            failures=tuple(failures),
            device=device,
        )
    }


DEVICE_ONLINE = register_action(
    Action(
        name="notes.device_online",
        version=1,
        title="The note: back online, or still offline",
        connectors=("none",),
        mode="read",
        inputs={
            "ticket": TICKET,
            "device": DEVICE,
            "state": Input("read", "NinjaOne's device state.", required=False),
            "alerts": Input("read", "NinjaOne's open alerts on the device.", required=False),
        },
        outputs=NOTE,
        run=device_online,
    )
)


# --- b. repeat alert on the same device ---------------------------------------------------


async def repeat_alert(ctx: ActionContext) -> Facts:
    """Core fact: the device's alert tickets in the last seven days. The kind
    is the normalised title with the device's name taken out; a ticket of the
    same kind on the same device is a repeat, and a ticket that names a
    machine whose name is this one's with other digits is another machine's.
    One other at least, or no note."""
    ticket = _ticket(ctx)
    device = _device(ctx)
    if device is None or not device.confident:
        return {"note": Skip(NOT_CONFIDENT)}
    tickets = _read(ctx, "tickets")
    if tickets is None or not tickets.ok or tickets.value is None:
        source = tickets.source if tickets is not None else "Autotask"
        error = tickets.error if tickets is not None else "not read"
        return {"note": Skip(f"{source} did not answer for the device's tickets ({error})")}
    kind = normalised_kind(ticket.title, device.name)
    same = [
        t
        for t in tickets.value
        if t.ticket_id != ticket.ticket_id
        and normalised_kind(t.title, device.name) == kind
        and not names_another_device(t.title, device.name)
    ]
    if not same:
        return {"note": Skip("no other ticket of this kind on this device in 7 days")}
    same.sort(key=lambda t: t.created_at or ctx.now)
    listed = ", ".join(f"{t.number} ({t.status or 'status unknown'})" for t in same)
    open_ones = [t for t in same if t.is_open]
    lines = [
        f"Same alert on {device.name} {len(same) + 1} times in 7 days: {listed}, and this ticket."
    ]
    if open_ones:
        lines.append(f"Possible duplicate of {open_ones[0].number} (open).")
    lines.append(f"Read by Desk at {when(tickets.read_at)}.")
    facts = (
        _fact("repeat.count", "Same alert in 7 days", len(same) + 1, tickets),
        _fact("repeat.tickets", "Tickets", [t.number for t in same], tickets),
        _fact("repeat.open", "Open duplicate", open_ones[0].number if open_ones else None, tickets),
    )
    return {
        "note": Draft(
            title=f"Repeat alert on {device.name}",
            body="\n".join(lines),
            facts=facts,
            device=device,
        )
    }


REPEAT_ALERT = register_action(
    Action(
        name="notes.repeat_alert",
        version=1,
        title="The note: the same alert on this machine before",
        connectors=("none",),
        mode="read",
        inputs={
            "ticket": TICKET,
            "device": DEVICE,
            "tickets": Input("read", "Autotask's tickets for the device in the window.", False),
        },
        outputs=NOTE,
        run=repeat_alert,
    )
)


# --- c. disk space -----------------------------------------------------------------------


def _gb(value: float | None) -> str:
    return "unknown" if value is None else f"{value:g} GB"


async def disk_space(ctx: ActionContext) -> Facts:
    """Core fact: the device's volumes in NinjaOne. The online state and the
    30-day alert count are lines that say so when they could not be read."""
    device = _device(ctx)
    if device is None or not device.confident:
        return {"note": Skip(NOT_CONFIDENT)}
    volumes = _read(ctx, "volumes")
    if volumes is None or not volumes.ok or volumes.value is None:
        source = volumes.source if volumes is not None else "NinjaOne"
        error = volumes.error if volumes is not None else "not read"
        return {"note": Skip(f"{source} did not answer for the volumes ({error})")}
    facts: list[Fact] = []
    failures: list[ReadFailure] = []
    lines: list[str] = []
    for volume in volumes.value:
        pct = f" ({volume.free_percent:g}%)" if volume.free_percent is not None else ""
        name = volume.name.rstrip(":")
        lines.append(f"{name}: {_gb(volume.free_gb)} free of {_gb(volume.capacity_gb)}{pct}.")
        facts.append(
            _fact(
                f"volume.{volume.name.rstrip(':').lower()}",
                f"Volume {volume.name}",
                {
                    "free_gb": volume.free_gb,
                    "capacity_gb": volume.capacity_gb,
                    "free_percent": volume.free_percent,
                },
                volumes,
            )
        )
    if not volumes.value:
        lines.append("NinjaOne reports no volumes for this device.")
    state = _read(ctx, "state")
    if state is not None and state.ok and state.value is not None:
        word = "online" if state.value.online else "offline"
        lines.append(f"Device is {word} (last contact {when(state.value.last_contact)}).")
        facts.append(_fact("device.online", "Online", state.value.online, state))
    else:
        failures.append(
            _failure(
                state or Read.failed("NinjaOne", "not read", at=volumes.read_at), "Online state"
            )
        )
    history = _read(ctx, "history")
    if history is not None and history.ok and history.value is not None:
        disk = [t for t in history.value if contains_any(t.title, DISK_PHRASES)]
        facts.append(_fact("disk.alerts_30d", "Disk alerts in 30 days", len(disk), history))
        if len(disk) > 2:
            lines.append(f"{len(disk)} disk alerts on this device in 30 days.")
    else:
        failures.append(
            _failure(
                history or Read.failed("Autotask", "not read", at=volumes.read_at),
                "Disk alerts in 30 days",
            )
        )
    lines.extend(f.line for f in failures)
    lines.append(f"Read by Desk at {when(volumes.read_at)}.")
    return {
        "note": Draft(
            title=f"Disk space on {device.name}",
            body="\n".join(lines),
            facts=tuple(facts),
            failures=tuple(failures),
            device=device,
        )
    }


DISK_SPACE = register_action(
    Action(
        name="notes.disk_space",
        version=1,
        title="The note: free space per volume, the state, the month's alerts",
        connectors=("none",),
        mode="read",
        inputs={
            "ticket": TICKET,
            "device": DEVICE,
            "volumes": Input("read", "NinjaOne's volumes.", required=False),
            "state": Input("read", "NinjaOne's device state.", required=False),
            "history": Input("read", "Autotask's tickets for the device, 30 days.", False),
        },
        outputs=NOTE,
        run=disk_space,
    )
)


# --- d. antivirus / EDR threat --------------------------------------------------------------


def _is_mdr(ticket: Arrival) -> bool:
    return contains_any(f"{ticket.title} {ticket.description or ''}", MDR_PHRASES) is not None


async def threat_gate(ctx: ActionContext) -> Facts:
    """Whether to read the detections: when Bitdefender's endpoint state came
    back, or when the alert is Blackpoint's (MDR), whose core is the
    instruction to investigate rather than the endpoint state."""
    protection = _read(ctx, "protection")
    mdr = _is_mdr(_ticket(ctx))
    answered = protection is not None and protection.ok and protection.value is not None
    return {"mdr": mdr, "read_detections": bool(answered or mdr)}


THREAT_GATE = register_action(
    Action(
        name="notes.threat_gate",
        version=1,
        title="Whether a threat note goes on without Bitdefender's endpoint state",
        connectors=("none",),
        mode="read",
        inputs={
            "ticket": TICKET,
            "protection": Input("read", "Bitdefender's endpoint state.", required=False),
        },
        outputs={
            "mdr": Output("boolean", "Whether the alert is Blackpoint's (MDR)."),
            "read_detections": Output("boolean", "Whether to read the detections."),
        },
        run=threat_gate,
    )
)


async def threat(ctx: ActionContext) -> Facts:
    """Core fact: the endpoint's protection in Bitdefender — except for a
    Blackpoint (MDR) alert, whose core is the instruction to investigate and
    which says in one line when Bitdefender did not answer."""
    ticket = _ticket(ctx)
    mdr = _is_mdr(ticket)
    device = _device(ctx)
    if device is None or not device.confident:
        if mdr:
            body = "\n".join(
                [
                    "The ticket's machine could not be matched confidently, so Bitdefender "
                    "was not read.",
                    MDR_LINE,
                ]
            )
            return {"note": Draft(title="MDR alert", body=body)}
        return {"note": Skip(NOT_CONFIDENT)}
    protection = _read(ctx, "protection")
    if protection is None:
        protection = Read.failed("Bitdefender", "not read", at=ctx.now)
    facts: list[Fact] = []
    failures: list[ReadFailure] = []
    lines: list[str] = []
    if protection.ok and protection.value is not None:
        p = protection.value
        yes_no = {True: "yes", False: "no", None: "not reported"}
        lines.append(
            f"Bitdefender: {device.name} is {p.state or 'state not reported'}"
            f" (last seen {when(p.last_seen)}); malware in the last 24 h: "
            f"{yes_no[p.malware_last_24h]};"
            f" still infected: {yes_no[p.still_infected]}; isolated: {yes_no[p.isolated]}."
        )
        facts.extend(
            [
                _fact("endpoint.state", "Endpoint state", p.state, protection),
                _fact("endpoint.malware_24h", "Malware in 24 h", p.malware_last_24h, protection),
                _fact("endpoint.still_infected", "Still infected", p.still_infected, protection),
                _fact("endpoint.isolated", "Isolated", p.isolated, protection),
            ]
        )
    else:
        if not mdr:
            return {
                "note": Skip(
                    f"{protection.source} did not answer for the endpoint ({protection.error})"
                )
            }
        failures.append(_failure(protection, "Bitdefender endpoint"))
    detections = _read(ctx, "detections")
    if detections is None:
        detections = Read.failed("Bitdefender", "not read", at=ctx.now)
    if detections.ok and detections.value is not None:
        facts.append(
            _fact("detections.24h", "Detections in 24 h", len(detections.value), detections)
        )
        if detections.value:
            found = "; ".join(
                f"{d.name} — {d.action or 'action not reported'} ({when(d.created_at)})"
                for d in detections.value[:5]
            )
            lines.append(f"What Bitdefender found and did in the last 24 h: {found}.")
            if len(detections.value) > 1:
                lines.append(
                    f"Re-detected: {len(detections.value)} detections in 24 h on this device."
                )
        else:
            lines.append("No Bitdefender detections on this device in the last 24 h.")
    else:
        failures.append(_failure(detections, "Detections in 24 h"))
    lines.extend(f.line for f in failures)
    if mdr:
        lines.append(MDR_LINE)
    read_at = protection.read_at if protection.ok else detections.read_at
    lines.append(f"Read by Desk at {when(read_at)}.")
    title = f"{'MDR alert' if mdr else 'Threat'} on {device.name}"
    return {
        "note": Draft(
            title=title,
            body="\n".join(lines),
            facts=tuple(facts),
            failures=tuple(failures),
            device=device,
        )
    }


THREAT = register_action(
    Action(
        name="notes.threat",
        version=1,
        title="The note: the endpoint's state and what Bitdefender found",
        connectors=("none",),
        mode="read",
        inputs={
            "ticket": TICKET,
            "device": DEVICE,
            "protection": Input("read", "Bitdefender's endpoint state.", required=False),
            "detections": Input("read", "Bitdefender's detections, 24 h.", required=False),
        },
        outputs=NOTE,
        run=threat,
    )
)


# --- e. backup notices ------------------------------------------------------------------------


async def backup_notice(ctx: ActionContext) -> Facts:
    """Reads nothing from a tool: the title and body, parsed by fixed rules
    per sender. An unknown format is a skip."""
    ticket = _ticket(ctx)
    notice = parse_backup_notice(ticket.title, ticket.description)
    if notice is None:
        return {"note": Skip("the notice is not in a format we parse (Veeam, N-able)")}
    read = Read.of("the ticket", notice, at=ctx.now)
    facts = (
        _fact("backup.sender", "Sender format", notice.sender, read),
        _fact("backup.outcome", "Outcome", notice.outcome, read),
        _fact("backup.job", "Job", notice.job, read),
        _fact("backup.error", "Error", notice.error, read),
    )
    if notice.outcome == "success":
        return {
            "note": Draft(
                title="Backup success notice",
                body="Backup success notice: no action needed.",
                facts=facts,
            )
        }
    word = "failed" if notice.outcome == "failed" else "finished with a warning"
    lines = [f"Backup {word}: job {notice.job or 'not named in the notice'}."]
    lines.append(f"Error: {notice.error}." if notice.error else "Error: none given in the notice.")
    lines.append(f"Parsed from the {notice.sender} notice by Desk at {when(ctx.now)}.")
    return {"note": Draft(title=f"Backup {word}", body="\n".join(lines), facts=facts)}


BACKUP_NOTICE = register_action(
    Action(
        name="notes.backup_notice",
        version=1,
        title="The note: success, or the failed job and its error",
        connectors=("none",),
        mode="read",
        inputs={"ticket": TICKET},
        outputs=NOTE,
        run=backup_notice,
    )
)


__all__ = [
    "BACKUP_NOTICE",
    "DEVICE_ONLINE",
    "DISK_SPACE",
    "NOT_CONFIDENT",
    "REPEAT_ALERT",
    "THREAT",
    "THREAT_GATE",
]

"""The report actions: a page in Desk built from tool reads, nothing written
anywhere else.

`reports.chronic_devices` reads the alert tickets of the period and resolves
each one's machine through the same device resolution the notes use, so it
names two connectors: Autotask and our own identity layer. A ticket whose
machine is not matched confidently is counted as unmatched, never guessed
onto one. The loop over tickets is inside the action, where loops live: a
definition names the action once.
"""

from __future__ import annotations

from typing import Any

from app.automation.actions.base import Action, ActionContext, Output, register_action
from app.automation.definitions import Facts
from app.automation.notes.definitions import TicketFact, normalised_kind
from app.automation.triggers import Arrival


def _arrival_from(ticket: TicketFact) -> Arrival:
    return Arrival(
        ticket_id=ticket.ticket_id,
        ticket_number=ticket.number,
        title=ticket.title,
        description=ticket.description,
        site_slug=ticket.site_slug,
        created_at=ticket.created_at,
    )


async def chronic_devices(ctx: ActionContext) -> Facts:
    """Devices with three or more alert tickets in the period, by alert kind."""
    if ctx.period is None:
        raise RuntimeError(f"{ctx.step_id}: a report action runs on a schedule with a period")
    period_start, period_end = ctx.period
    tools = ctx.need_tools()
    days = max(1, int((period_end - period_start).total_seconds() // 86400))
    tickets = await tools.alert_tickets(days=days)
    if not tickets.ok or tickets.value is None:
        return {
            "report": {
                "read_at": tickets.read_at.isoformat(),
                "error": f"{tickets.source} did not answer ({tickets.error})",
                "devices": [],
                "tickets": 0,
                "unmatched": 0,
            }
        }
    by_device: dict[str, dict[str, Any]] = {}
    unmatched = 0
    for ticket in tickets.value:
        device = await tools.resolve_device(_arrival_from(ticket))
        if device is None or not device.confident:
            unmatched += 1
            continue
        entry = by_device.setdefault(
            device.device_id,
            {"device": device.name, "site": device.site_slug, "kinds": {}, "total": 0},
        )
        kind = normalised_kind(ticket.title, device.name)
        kinds = entry["kinds"].setdefault(kind, {"title": ticket.title, "tickets": []})
        kinds["tickets"].append(
            {
                "number": ticket.number,
                "ticket_id": ticket.ticket_id,
                "status": ticket.status,
                "is_open": ticket.is_open,
                "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
            }
        )
        entry["total"] += 1
    chronic = sorted(
        (e for e in by_device.values() if e["total"] >= 3), key=lambda e: (-e["total"], e["device"])
    )
    for entry in chronic:
        entry["kinds"] = sorted(
            ({"kind": k, **v, "count": len(v["tickets"])} for k, v in entry["kinds"].items()),
            key=lambda k: -k["count"],
        )
    return {
        "report": {
            "read_at": tickets.read_at.isoformat(),
            "error": None,
            "tickets": len(tickets.value),
            "unmatched": unmatched,
            "threshold": 3,
            "devices": chronic,
        }
    }


CHRONIC_DEVICES = register_action(
    Action(
        name="reports.chronic_devices",
        version=1,
        title="Autotask: alert tickets in the period, matched to devices",
        connectors=("autotask", "devices"),
        mode="read",
        inputs={},
        outputs={"report": Output("facts", "The report: devices, kinds, tickets.")},
        run=chronic_devices,
    )
)

__all__ = ["CHRONIC_DEVICES"]

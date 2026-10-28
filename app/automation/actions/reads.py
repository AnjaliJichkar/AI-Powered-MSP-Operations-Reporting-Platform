"""The read actions: one tool read each, through the `Tools` protocol.

Each returns the read as it came back — a `Read` with its time, or the
reason it failed — under `read`, and `ok` beside it so a definition can gate
a later step on it (`when: {steps.state.ok} equals true`). Nothing here
decides what a failed read means; that is the note that is built from it,
which says so in one line or writes nothing, as its workflow's definition
of the core fact says.

`devices.resolve` is the identity layer, not a vendor: the ticket's machine,
with how sure the resolution is. A note is never built on a match that is
not confident, and every workflow says so by gating its reads on
`{steps.device.confident}`.
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
from app.automation.notes.definitions import DeviceMatch

TICKET = Input("ticket", "The ticket an alert raised, as the poll saw it.")
DEVICE = Input("device", "The machine, from devices.resolve.")
READ_OUTPUTS = {
    "read": Output("read", "The read: its value and time, or why it failed."),
    "ok": Output("boolean", "Whether the tool answered."),
}


def _device(ctx: ActionContext) -> DeviceMatch:
    device = ctx.inputs.get("device")
    if not isinstance(device, DeviceMatch):
        raise RuntimeError(f"{ctx.step_id}: no device to read; gate this step on the match")
    return device


def _reading(read: Any) -> Facts:
    return {"read": read, "ok": bool(read.ok)}


async def resolve_device(ctx: ActionContext) -> Facts:
    ticket = ctx.inputs["ticket"]
    device = await ctx.need_tools().resolve_device(ticket)
    return {
        "device": device,
        "confident": bool(device is not None and device.confident),
        "name": device.name if device is not None else None,
    }


RESOLVE_DEVICE = register_action(
    Action(
        name="devices.resolve",
        version=1,
        title="Which machine the ticket is about",
        connectors=("devices",),
        mode="read",
        inputs={"ticket": TICKET},
        outputs={
            "device": Output("device", "The match, or nothing."),
            "confident": Output("boolean", "Whether the match is high or medium confidence."),
            "name": Output("string", "The device's name, when matched."),
        },
        run=resolve_device,
        idempotency=("ticket",),
    )
)


async def read_device(ctx: ActionContext) -> Facts:
    return _reading(await ctx.need_tools().device_state(_device(ctx)))


READ_DEVICE = register_action(
    Action(
        name="ninjaone.read_device",
        version=1,
        title="NinjaOne: device status, last contact, last boot",
        connectors=("ninjaone",),
        mode="read",
        inputs={"device": DEVICE},
        outputs=READ_OUTPUTS,
        run=read_device,
        idempotency=("device",),
    )
)


async def read_open_alerts(ctx: ActionContext) -> Facts:
    return _reading(await ctx.need_tools().open_alerts(_device(ctx)))


READ_OPEN_ALERTS = register_action(
    Action(
        name="ninjaone.read_open_alerts",
        version=1,
        title="NinjaOne: open alerts on the device",
        connectors=("ninjaone",),
        mode="read",
        inputs={"device": DEVICE},
        outputs=READ_OUTPUTS,
        run=read_open_alerts,
        idempotency=("device",),
    )
)


async def read_volumes(ctx: ActionContext) -> Facts:
    return _reading(await ctx.need_tools().volumes(_device(ctx)))


READ_VOLUMES = register_action(
    Action(
        name="ninjaone.read_volumes",
        version=1,
        title="NinjaOne: volumes, free space, size",
        connectors=("ninjaone",),
        mode="read",
        inputs={"device": DEVICE},
        outputs=READ_OUTPUTS,
        run=read_volumes,
        idempotency=("device",),
    )
)


async def find_related_tickets(ctx: ActionContext) -> Facts:
    days = int(ctx.inputs["days"])
    return _reading(await ctx.need_tools().device_tickets(_device(ctx), days=days))


FIND_RELATED_TICKETS = register_action(
    Action(
        name="autotask.find_related_tickets",
        version=1,
        title="Autotask: the device's tickets",
        connectors=("autotask",),
        mode="read",
        inputs={"device": DEVICE, "days": Input("integer", "How many days back to look.")},
        outputs=READ_OUTPUTS,
        run=find_related_tickets,
        idempotency=("device", "days"),
    )
)


async def read_protection(ctx: ActionContext) -> Facts:
    return _reading(await ctx.need_tools().endpoint_protection(_device(ctx)))


READ_PROTECTION = register_action(
    Action(
        name="bitdefender.read_protection",
        version=1,
        title="Bitdefender: endpoint status, last seen, infection, isolation",
        connectors=("bitdefender",),
        mode="read",
        inputs={"device": DEVICE},
        outputs=READ_OUTPUTS,
        run=read_protection,
        idempotency=("device",),
    )
)


async def read_threats(ctx: ActionContext) -> Facts:
    hours = int(ctx.inputs["hours"])
    return _reading(await ctx.need_tools().detections(_device(ctx), hours=hours))


READ_THREATS = register_action(
    Action(
        name="bitdefender.read_threats",
        version=1,
        title="Bitdefender: detections",
        connectors=("bitdefender",),
        mode="read",
        inputs={"device": DEVICE, "hours": Input("integer", "How many hours back to look.")},
        outputs=READ_OUTPUTS,
        run=read_threats,
        idempotency=("device", "hours"),
    )
)


__all__ = [
    "FIND_RELATED_TICKETS",
    "READ_DEVICE",
    "READ_OPEN_ALERTS",
    "READ_PROTECTION",
    "READ_THREATS",
    "READ_VOLUMES",
    "RESOLVE_DEVICE",
]

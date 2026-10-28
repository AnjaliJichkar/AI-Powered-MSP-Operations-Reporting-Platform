"""What an action is: one small, tested block a workflow is assembled from.

A workflow used to be code: a Python file with four functions per step. It is
now data (`app/automation/definitions/*.yaml`) assembled from actions, and an
action is the unit that is written, reviewed and tested once and used by many
workflows: "read the device's state from NinjaOne", "find the device's
tickets in Autotask", "add an internal note to the ticket".

An action declares, and the registry refuses one that does not:

* a **name** (`connector.verb_noun`) and a **version**;
* a plain-language **title**, the words the workflow page prints for the step;
* typed **inputs** and **outputs** (`TYPES`), which is what lets a definition
  be checked before it is loaded: a step that hands a string where a device
  is wanted is refused at load, not at run time;
* the **connectors** it uses (`CONNECTORS`), and `mode`: `read` or `write`;
* for a write, the exact **write scope**, which must be on `WRITE_SCOPES` — the
  allow-list of everything this engine may do to any system. Today that is
  one line: add an internal note to an Autotask ticket. The practice table is
  the engine's own and is listed so the practice workflow can exist;
* the contract functions: `precondition` (read only), `plan` (what would
  change, in words), `before` (the state about to change), `run` (the one
  thing that does anything; **idempotent** for one (run, step)), `verify`
  (read only; a write is done when it passes, never because it returned) and
  `undo`, or none, in which case the action cannot be undone and every page
  says so;
* its **idempotency key**: the inputs that, with the run and the step,
  identify one act, so a second attempt of the same step is the same act.

Nothing here reaches a vendor. A read goes through the `Tools` protocol the
caller hands in (`app/tickets/alert_tools.py` live, a fake in the tests) and
a write through the `NoteWriter` the caller hands in; which connector each
`Tools` method stands for is written down in `TOOL_CONNECTORS`, and a test
runs every action against a recording fake to prove it reaches only the
connectors it declares.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.definitions import Check, Facts, Plan
from app.automation.notes.definitions import (
    DeviceMatch,
    Draft,
    Read,
    Skip,
    Tools,
)
from app.automation.triggers import Arrival

# --- the vocabulary ------------------------------------------------------------------

#: The types an input, an output or a setting may have. A definition names
#: one of these and the loader checks every value and every reference against
#: them. `ticket` is the arrival a trigger saw; `device` is the identity
#: layer's match (or nothing); `read` is one tool read with its time, or why
#: it failed; `note` is what a note workflow builds (a draft or a skip);
#: `facts` is plain JSON.
TYPES = (
    "string",
    "integer",
    "number",
    "boolean",
    "list[string]",
    "ticket",
    "device",
    "read",
    "note",
    "facts",
)

#: Which systems an action may name. `devices` is our own identity layer,
#: `practice` the engine's own table, `none` a pure computation.
CONNECTORS = ("autotask", "ninjaone", "bitdefender", "devices", "practice", "none")

#: Every write this engine may make to any system, in words. A write action
#: whose scope is not on this list is refused by the registry; widening the
#: list is a decision made in a commit that says so.
WRITE_SCOPES = frozenset(
    {
        "add internal ticket note",
        "practice record on the engine's own table",
    }
)

#: Which connector each `Tools` read stands for. The isolation test maps the
#: calls an action made through these names.
TOOL_CONNECTORS: dict[str, str] = {
    "resolve_device": "devices",
    "device_state": "ninjaone",
    "open_alerts": "ninjaone",
    "volumes": "ninjaone",
    "device_tickets": "autotask",
    "alert_tickets": "autotask",
    "endpoint_protection": "bitdefender",
    "detections": "bitdefender",
}


def fits(value: Any, type_name: str) -> bool:
    """Whether a value is of a declared type. `None` fits nothing; an
    optional input is declared `required: false` and checked only when set."""
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "list[string]":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if type_name == "ticket":
        return isinstance(value, Arrival)
    if type_name == "device":
        return isinstance(value, DeviceMatch)
    if type_name == "read":
        return isinstance(value, Read)
    if type_name == "note":
        return isinstance(value, Draft | Skip)
    if type_name == "facts":
        return isinstance(value, dict)
    return False


# --- the contract ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Input:
    type: str
    description: str
    required: bool = True

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise InvalidAction(f"input type {self.type!r} is not one of {', '.join(TYPES)}")


@dataclass(frozen=True, slots=True)
class Output:
    type: str
    description: str

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise InvalidAction(f"output type {self.type!r} is not one of {', '.join(TYPES)}")


class NoteWriter(Protocol):
    """The one write client: adds an internal note and nothing else."""

    async def add_internal_note(self, ticket_id: str | int, *, title: str, body: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class ActionContext:
    """What an action's functions are handed.

    The inputs are already resolved: every `{inputs.x}`, `{steps.id.out}`,
    `{settings.x}` and `{ticket.x}` in the definition replaced with its value
    and checked against the action's types. A run-kind step also has the
    session (its own transaction), the run and the attempt; a note-kind step
    has the tools and the ticket; a report step has the period. An action
    uses what its kind is given and nothing else.
    """

    inputs: Mapping[str, Any]
    client_id: uuid.UUID
    step_id: str
    now: dt.datetime
    settings: Mapping[str, Any] = field(default_factory=dict)
    session: AsyncSession | None = None
    run_id: uuid.UUID | None = None
    attempt: int = 1
    dry_run: bool = False
    before: Facts | None = None
    outputs: Facts | None = None
    """What `run` returned, handed to `verify` and `undo` after it."""
    tools: Tools | None = None
    writer: NoteWriter | None = None
    ticket: Arrival | None = None
    period: tuple[dt.datetime, dt.datetime] | None = None

    def need_tools(self) -> Tools:
        if self.tools is None:
            raise RuntimeError(f"{self.step_id}: this action reads a tool and none was given")
        return self.tools

    def need_writer(self) -> NoteWriter:
        if self.writer is None:
            raise RuntimeError(f"{self.step_id}: this action writes and no writer was given")
        return self.writer

    def need_session(self) -> AsyncSession:
        if self.session is None:
            raise RuntimeError(f"{self.step_id}: this action needs a session and none was given")
        return self.session


RunFn = Callable[[ActionContext], Awaitable[Facts]]
CheckFn = Callable[[ActionContext], Awaitable[Check]]
PlanFn = Callable[[ActionContext], Awaitable[Plan]]


class InvalidAction(ValueError):
    """An action the registry will not take: a write with no verify, a write
    scope off the allow-list, a connector or a type it does not know."""


@dataclass(frozen=True, slots=True)
class Action:
    name: str
    version: int
    title: str
    connectors: tuple[str, ...]
    mode: str
    """`read` or `write`."""
    inputs: Mapping[str, Input]
    outputs: Mapping[str, Output]
    run: RunFn
    """The action itself. For a read, the read; for a write, the one thing
    that changes anything, idempotent for one (run, step)."""
    idempotency: tuple[str, ...] = ()
    """The input names that, with the run and the step, identify one act."""
    write_scope: str | None = None
    precondition: CheckFn | None = None
    plan: PlanFn | None = None
    before: RunFn | None = None
    verify: CheckFn | None = None
    undo: RunFn | None = None
    checks: str = ""
    """What the precondition looks for, in words."""
    does: str = ""
    """What the action does, in words."""
    confirms: str = ""
    """How verify knows it worked, in words."""
    undoes: str | None = None
    """What the undo puts back, when there is one."""

    def __post_init__(self) -> None:
        if "." not in self.name or not self.name.replace(".", "_").replace("_", "").isalnum():
            raise InvalidAction(f"{self.name!r}: an action is named connector.verb_noun")
        if self.version < 1:
            raise InvalidAction(f"{self.name}: version must be 1 or more")
        if not self.title.strip():
            raise InvalidAction(f"{self.name}: needs a title in words")
        if self.mode not in ("read", "write"):
            raise InvalidAction(f"{self.name}: mode is read or write, not {self.mode!r}")
        if not self.connectors:
            raise InvalidAction(f"{self.name}: names no connector; a pure action names 'none'")
        for connector in self.connectors:
            if connector not in CONNECTORS:
                raise InvalidAction(
                    f"{self.name}: connector {connector!r} is not one of {', '.join(CONNECTORS)}"
                )
        if self.mode == "write":
            if self.write_scope not in WRITE_SCOPES:
                raise InvalidAction(
                    f"{self.name}: write scope {self.write_scope!r} is not on the allow-list "
                    f"({', '.join(sorted(WRITE_SCOPES))}). A new write is a decision, made by "
                    f"adding it there in a commit that says so."
                )
            if self.verify is None:
                raise InvalidAction(
                    f"{self.name}: a write action needs a verify; a write is done when the "
                    f"verify passes, never because the action returned"
                )
            if not (self.checks and self.does and self.confirms):
                raise InvalidAction(
                    f"{self.name}: a write action needs words for what it checks, does and "
                    f"confirms; the workflow page prints them for the authoriser"
                )
            if self.undo is not None and not self.undoes:
                raise InvalidAction(f"{self.name}: has an undo and no words for what it puts back")
        elif self.write_scope is not None:
            raise InvalidAction(f"{self.name}: a read action has no write scope")
        for key in self.idempotency:
            if key not in self.inputs:
                raise InvalidAction(
                    f"{self.name}: idempotency names {key!r}, which is not an input"
                )

    @property
    def reversible(self) -> bool:
        return self.undo is not None

    @property
    def writes(self) -> bool:
        return self.mode == "write"

    def idempotency_key(
        self, run_id: uuid.UUID | None, step_id: str, inputs: Mapping[str, Any]
    ) -> str:
        """The one act this is, as a string: run, step, and the named inputs."""
        parts = [str(run_id or "-"), step_id]
        parts.extend(f"{key}={inputs.get(key)!r}" for key in self.idempotency)
        return " / ".join(parts)


# --- the registry ----------------------------------------------------------------------

_ACTIONS: dict[str, Action] = {}


def register_action(action: Action) -> Action:
    if action.name in _ACTIONS:
        raise InvalidAction(f"{action.name}: registered twice")
    _ACTIONS[action.name] = action
    return action


def action(name: str) -> Action:
    try:
        return _ACTIONS[name]
    except KeyError:
        raise KeyError(f"no action called {name!r}") from None


def actions() -> tuple[Action, ...]:
    return tuple(_ACTIONS[name] for name in sorted(_ACTIONS))


__all__ = [
    "CONNECTORS",
    "TOOL_CONNECTORS",
    "TYPES",
    "WRITE_SCOPES",
    "Action",
    "ActionContext",
    "Input",
    "InvalidAction",
    "NoteWriter",
    "Output",
    "action",
    "actions",
    "fits",
    "register_action",
]

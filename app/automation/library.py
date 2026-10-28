"""Workflows as data: the loader, the validator and the compiler.

A workflow is one YAML file in `app/automation/definitions/`, reviewed like
code, naming the actions it is assembled from (`app/automation/actions/`).
This module reads those files, refuses anything wrong with them in one line
that names the file, the step and the problem, and compiles each into the
shape the engine already runs: a `Workflow` (steps with the four-function
contract, executed by `service` and `runner` inside a run) or a
`NoteWorkflow` (built from tool reads by the notes service). The engine did
not change; what changed is where a workflow comes from.

**No code in a definition.** A value is a literal (a string, a number, a
boolean, a list of strings) or one reference, and nothing else:

    {inputs.x}          a run's input
    {steps.<id>.<out>}  an output of an earlier step
    {settings.x}        a setting the definition declares and the client sets
    {ticket.x}          a field of the ticket that started a note workflow

A string that looks like a reference and is not one is refused, so there is
no way to write a template. A condition (`when`) is one reference and one
operator — `equals`, `not_equals`, `in`, `exists`, `greater_than`,
`less_than` — or a list of them, all of which must hold. There are no loops:
an action that needs a list takes it as one input.

**Validation is the point.** Every reference is checked against the type
of what it names — the action's declared inputs and outputs, the
definition's inputs and settings, the ticket's fields — so a step that hands
a string where a device is wanted, a reference to a step that has not run
yet, a dependency on a later step, a setting nobody declared or an action
nobody wrote is refused before anything is registered. `validate_all` is
run by the test suite and by `scripts/automation.py validate`.

**A run knows what it ran.** Each definition carries a SHA-256 of its
*canonical form* — the parsed definition, serialised one way (`canonical`) —
and a run records the definition's name, version and that hash. Hashing the
form rather than the file's bytes is deliberate: the same definition is not
the same bytes on two machines (a checkout with `core.autocrlf` writes CRLF
on Windows and LF on Linux; an editor adds or strips a final newline, a byte
order mark or trailing spaces), and a run's record of what it ran must not
move because somebody opened the file in a different editor.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from sqlalchemy import select

from app.automation import actions as action_library
from app.automation.actions.base import Action, ActionContext, fits
from app.automation.definitions import (
    INPUT_SOURCES,
    TRIGGER_KINDS,
    Check,
    Facts,
    FactsFn,
    InvalidWorkflow,
    Plan,
    Step,
    StepContext,
    TicketRule,
    Trigger,
    Workflow,
    register,
)
from app.automation.notes.definitions import (
    Draft,
    InvalidNoteWorkflow,
    NoteContext,
    NoteWorkflow,
    ReportContext,
    Skip,
    register_note_workflow,
)
from app.automation.triggers import Arrival
from app.models.automation import AutomationRun, AutomationStep
from app.models.client import ClientKind

DEFINITIONS_DIR = Path(__file__).parent / "definitions"

KINDS = ("run", "note", "report")
ALLOWED_ON = ("any", "demo", "live")
OPERATORS = ("equals", "not_equals", "in", "exists", "greater_than", "less_than")
NAMESPACES = ("inputs", "steps", "settings", "ticket")

#: The ticket's fields a note workflow may reference, with their types.
TICKET_FIELDS: dict[str, str] = {
    "self": "ticket",
    "id": "string",
    "number": "string",
    "title": "string",
    "description": "string",
    "queue": "string",
    "issue_type": "string",
    "sub_issue_type": "string",
    "source": "string",
    "contact": "string",
    "company": "string",
    "site": "string",
}

#: Which setting and input types map to which Python types for the run
#: input schema and for checking a client's settings.
SCALAR_TYPES = ("string", "integer", "number", "boolean", "list[string]")

REFERENCE = re.compile(
    r"^\{(inputs|steps|settings|ticket)\.([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\.([A-Za-z_][A-Za-z0-9_]*))?\}$"
)
DURATION = re.compile(r"^(\d+)\s*(minute|minutes|hour|hours|day|days)$")
IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


class DefinitionError(ValueError):
    """One line: the file, the step and what is wrong."""

    def __init__(self, file: str, step: str | None, problem: str) -> None:
        self.file = file
        self.step = step
        self.problem = problem
        where = f"{file}: {step}: " if step else f"{file}: "
        super().__init__(where + problem)


# --- the schema of a file ---------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputDef(_Strict):
    type: str
    description: str = ""
    required: bool = False
    default: Any = None
    source: str = "engineer"
    min: float | None = None
    max: float | None = None
    min_length: int | None = None
    max_length: int | None = None


class SettingDef(_Strict):
    type: str
    description: str = ""
    required: bool = False
    default: Any = None


class RuleDef(_Strict):
    queue: str | None = None
    issue_type: str | None = None
    sub_issue_type: str | None = None
    source: str | None = None
    title_equals: str | None = None
    title_contains: str | None = None
    title_contains_any: list[str] | None = None


class TriggerDef(_Strict):
    key: str
    kind: str
    title: str
    rule: RuleDef | None = None
    every: str | None = None


class ConditionDef(_Strict):
    value: Any
    equals: Any = None
    not_equals: Any = None
    in_: list[Any] | None = Field(default=None, alias="in")
    exists: bool | None = None
    greater_than: float | None = None
    less_than: float | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def operator(self) -> tuple[str, Any]:
        given = [
            (name, value)
            for name, value in (
                ("equals", self.equals),
                ("not_equals", self.not_equals),
                ("in", self.in_),
                ("exists", self.exists),
                ("greater_than", self.greater_than),
                ("less_than", self.less_than),
            )
            if value is not None
        ]
        if len(given) != 1:
            raise ValueError(f"a condition has exactly one operator ({', '.join(OPERATORS)})")
        return given[0]


class StepDef(_Strict):
    id: str
    action: str
    title: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: str | None = None
    when: list[ConditionDef] | ConditionDef | None = None

    def conditions(self) -> list[ConditionDef]:
        if self.when is None:
            return []
        return self.when if isinstance(self.when, list) else [self.when]


class ApprovalDef(_Strict):
    window: str = "24 hours"


class DefinitionDef(_Strict):
    name: str
    title: str
    version: int
    description: str
    kind: str
    allowed_on: str = "any"
    inputs: dict[str, InputDef] = Field(default_factory=dict)
    settings: dict[str, SettingDef] = Field(default_factory=dict)
    triggers: list[TriggerDef] = Field(default_factory=list)
    steps: list[StepDef] = Field(default_factory=list)
    approval: ApprovalDef = Field(default_factory=ApprovalDef)
    undo_window: str = "24 hours"
    note_mode: str | None = None
    writes: str | None = None


# --- what a loaded definition is ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    type: str
    description: str
    required: bool
    default: Any = None


@dataclass(frozen=True, slots=True)
class Definition:
    """One file, parsed, checked and compiled."""

    file: str
    name: str
    kind: str
    version: int
    hash: str
    spec: DefinitionDef
    settings: tuple[SettingSpec, ...]
    steps: tuple[StepDef, ...]
    workflow: Workflow | None = None
    note_workflow: NoteWorkflow | None = None

    def missing_settings(self, given: Mapping[str, Any]) -> tuple[SettingSpec, ...]:
        """The required settings a client has not set, or set to the wrong type."""
        return tuple(
            s
            for s in self.settings
            if s.required and (s.key not in given or not fits(given[s.key], s.type))
        )

    def wrong_settings(self, given: Mapping[str, Any]) -> list[str]:
        """What is wrong with a client's settings for this workflow, in words:
        a declared setting of the wrong type, or a key nobody declared.
        `triggers` is the per-trigger rule overrides and is always allowed."""
        declared = {s.key: s for s in self.settings}
        problems: list[str] = []
        for key, value in given.items():
            if key == "triggers":
                continue
            spec = declared.get(key)
            if spec is None:
                raise_key = ", ".join(sorted(declared)) or "none"
                problems.append(f"{key}: not a setting of {self.name} (it declares: {raise_key})")
            elif value is not None and not fits(value, spec.type):
                problems.append(f"{key}: expected {spec.type}, got {type(value).__name__}")
        return problems


_DEFINITIONS: dict[str, Definition] = {}
_TRACERS: dict[str, Any] = {}


async def trace_note(name: str, ctx: NoteContext | ReportContext) -> list[dict[str, Any]]:
    """Run a note or report definition's steps and say what each did; no write."""
    tracer = _TRACERS.get(name)
    if tracer is None:
        raise KeyError(f"no note or report definition called {name!r}")
    found: list[dict[str, Any]] = await tracer(ctx)
    return found


def definition(name: str) -> Definition:
    try:
        return _DEFINITIONS[name]
    except KeyError:
        raise KeyError(f"no definition called {name!r}") from None


def definitions() -> tuple[Definition, ...]:
    return tuple(_DEFINITIONS[name] for name in sorted(_DEFINITIONS))


def find(name: str) -> Definition | None:
    return _DEFINITIONS.get(name)


# --- references and conditions -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reference:
    namespace: str
    name: str
    output: str | None = None

    @property
    def text(self) -> str:
        tail = f".{self.output}" if self.output else ""
        return f"{{{self.namespace}.{self.name}{tail}}}"


def parse_reference(value: Any) -> Reference | None:
    """The reference a value is, or None when it is a literal. A string that
    has a brace in it and is not exactly one reference is refused: that is
    the whole of the no-templates rule."""
    if not isinstance(value, str):
        return None
    found = REFERENCE.match(value)
    if found:
        namespace, name, output = found.groups()
        if (namespace == "steps") != (output is not None):
            raise ValueError(
                f"{value!r}: a step reference is {{steps.<id>.<output>}}; the others take one name"
            )
        return Reference(namespace, name, output)
    if "{" in value or "}" in value:
        raise ValueError(
            f"{value!r} is not a reference. A value is a literal or exactly one of "
            f"{{inputs.x}}, {{steps.<id>.<output>}}, {{settings.x}}, {{ticket.x}}; "
            f"there are no templates."
        )
    return None


def literal_type(value: Any) -> str | None:
    """The declared type a literal is, or None when it is not a literal we take."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "list[string]"
    return None


def _duration(text: str, *, file: str, what: str) -> dt.timedelta:
    found = DURATION.match(str(text).strip())
    if not found:
        raise DefinitionError(
            file, None, f"{what}: {text!r} is not a duration like '24 hours' or '30 days'"
        )
    amount, unit = int(found.group(1)), found.group(2).rstrip("s")
    return dt.timedelta(**{f"{unit}s": amount})


@dataclass(frozen=True, slots=True)
class Scope:
    """What references resolve against while a workflow runs."""

    inputs: Mapping[str, Any]
    settings: Mapping[str, Any]
    steps: Mapping[str, Facts | None]
    ticket: Arrival | None = None
    ticket_number: str | None = None

    def resolve(self, ref: Reference) -> Any:
        if ref.namespace == "inputs":
            return self.inputs.get(ref.name)
        if ref.namespace == "settings":
            return self.settings.get(ref.name)
        if ref.namespace == "steps":
            outputs = self.steps.get(ref.name)
            return None if outputs is None else outputs.get(ref.output or "")
        if ref.namespace == "ticket":
            return self.ticket_field(ref.name)
        raise ValueError(ref.text)

    def ticket_field(self, name: str) -> Any:
        if self.ticket is None:
            return self.ticket_number if name == "number" else None
        t = self.ticket
        return {
            "self": t,
            "id": t.ticket_id,
            "number": t.ticket_number,
            "title": t.title,
            "description": t.description,
            "queue": t.queue,
            "issue_type": t.issue_type,
            "sub_issue_type": t.sub_issue_type,
            "source": t.source,
            "contact": t.contact,
            "company": t.company,
            "site": t.site_slug,
        }.get(name)


def resolve_value(value: Any, scope: Scope) -> Any:
    ref = parse_reference(value)
    if ref is not None:
        return scope.resolve(ref)
    if isinstance(value, list):
        return [resolve_value(v, scope) for v in value]
    return value


def resolve_inputs(step: StepDef, scope: Scope) -> dict[str, Any]:
    return {name: resolve_value(value, scope) for name, value in step.inputs.items()}


def condition_holds(condition: ConditionDef, scope: Scope) -> bool:
    operator, operand = condition.operator()
    actual = resolve_value(condition.value, scope)
    if operator == "exists":
        return (actual is not None) == bool(operand)
    if operator == "equals":
        return bool(actual == operand)
    if operator == "not_equals":
        return bool(actual != operand)
    if operator == "in":
        return actual in (operand or [])
    if actual is None or isinstance(actual, bool) or not isinstance(actual, int | float):
        return False
    if operator == "greater_than":
        return bool(actual > operand)
    return bool(actual < operand)


def conditions_hold(step: StepDef, scope: Scope) -> bool:
    return all(condition_holds(c, scope) for c in step.conditions())


# --- validation ---------------------------------------------------------------------------


def _check_identifier(file: str, step: str | None, what: str, value: str) -> None:
    if not IDENT.match(value):
        raise DefinitionError(
            file, step, f"{what} {value!r} must be lower-case letters, digits and underscores"
        )


def _type_of_reference(
    ref: Reference,
    spec: DefinitionDef,
    earlier: dict[str, Action],
    *,
    file: str,
    step: str,
) -> str:
    """The declared type of what a reference names, or a refusal."""
    if ref.namespace == "inputs":
        found = spec.inputs.get(ref.name)
        if found is None:
            raise DefinitionError(
                file, step, f"{ref.text} names an input the definition does not declare"
            )
        return found.type
    if ref.namespace == "settings":
        setting = spec.settings.get(ref.name)
        if setting is None:
            raise DefinitionError(
                file, step, f"{ref.text} names a setting the definition does not declare"
            )
        return setting.type
    if ref.namespace == "ticket":
        if spec.kind == "run" and ref.name != "number":
            raise DefinitionError(
                file,
                step,
                f"{ref.text}: a run has only the ticket's number; take the other fields as "
                f"inputs with a ticket source",
            )
        kind = TICKET_FIELDS.get(ref.name)
        if kind is None:
            raise DefinitionError(
                file, step, f"{ref.text}: the ticket's fields are {', '.join(TICKET_FIELDS)}"
            )
        return kind
    if ref.name == step:
        raise DefinitionError(file, step, f"{ref.text} refers to the step itself")
    action = earlier.get(ref.name)
    if action is None:
        raise DefinitionError(
            file,
            step,
            f"{ref.text} refers to a step that has not run yet (or does not exist); a step may "
            f"use only the outputs of steps before it",
        )
    output = action.outputs.get(ref.output or "")
    if output is None:
        raise DefinitionError(
            file,
            step,
            f"{ref.text}: {action.name} has no output {ref.output!r} (it has: "
            f"{', '.join(action.outputs) or 'none'})",
        )
    return output.type


def _check_value(
    value: Any,
    wanted: str,
    spec: DefinitionDef,
    earlier: dict[str, Action],
    *,
    file: str,
    step: str,
    what: str,
) -> None:
    try:
        ref = parse_reference(value)
    except ValueError as exc:
        raise DefinitionError(file, step, f"{what}: {exc}") from None
    if ref is not None:
        given = _type_of_reference(ref, spec, earlier, file=file, step=step)
        if given != wanted:
            raise DefinitionError(file, step, f"{what} wants a {wanted}, not {ref.text} ({given})")
        return
    if isinstance(value, list):
        if wanted != "list[string]":
            raise DefinitionError(file, step, f"{what} wants a {wanted}; a list was given")
        for item in value:
            _check_value(item, "string", spec, earlier, file=file, step=step, what=what)
        return
    given_type = literal_type(value)
    if given_type is None:
        raise DefinitionError(
            file, step, f"{what}: {value!r} is not a literal a definition may hold"
        )
    if given_type != wanted and not (wanted == "number" and given_type == "integer"):
        raise DefinitionError(file, step, f"{what} wants a {wanted}, not {value!r} ({given_type})")


def _check_steps(spec: DefinitionDef, *, file: str) -> None:
    if not spec.steps:
        raise DefinitionError(file, None, "a workflow needs at least one step")
    earlier: dict[str, Action] = {}
    seen: set[str] = set()
    for step in spec.steps:
        _check_identifier(file, step.id, "step id", step.id)
        if step.id in seen:
            raise DefinitionError(file, step.id, "step id is used twice")
        try:
            action = action_library.action(step.action)
        except KeyError:
            known = ", ".join(a.name for a in action_library.actions())
            raise DefinitionError(
                file, step.id, f"unknown action {step.action!r} (the library has: {known})"
            ) from None
        if action.writes and action.verify is None:
            raise DefinitionError(file, step.id, f"{action.name} writes and has no verify")
        for name in step.inputs:
            if name not in action.inputs:
                raise DefinitionError(
                    file,
                    step.id,
                    f"{action.name} has no input {name!r} (it takes: "
                    f"{', '.join(action.inputs) or 'nothing'})",
                )
        for name, wanted in action.inputs.items():
            if name not in step.inputs:
                if wanted.required:
                    raise DefinitionError(file, step.id, f"{action.name} needs input {name!r}")
                continue
            _check_value(
                step.inputs[name],
                wanted.type,
                spec,
                earlier,
                file=file,
                step=step.id,
                what=f"input {name!r}",
            )
        if step.depends_on is not None:
            if step.depends_on == step.id:
                raise DefinitionError(file, step.id, "depends on itself")
            if step.depends_on not in earlier:
                raise DefinitionError(
                    file,
                    step.id,
                    f"depends on {step.depends_on!r}, which is not an earlier step (a dependency "
                    f"on a later step is a cycle)",
                )
        for condition in step.conditions():
            try:
                operator, operand = condition.operator()
            except ValueError as exc:
                raise DefinitionError(file, step.id, f"when: {exc}") from None
            try:
                ref = parse_reference(condition.value)
            except ValueError as exc:
                raise DefinitionError(file, step.id, f"when: {exc}") from None
            if ref is None:
                raise DefinitionError(
                    file, step.id, "when: the value compared is a reference, not a literal"
                )
            kind = _type_of_reference(ref, spec, earlier, file=file, step=step.id)
            if operator in ("greater_than", "less_than") and kind not in ("integer", "number"):
                raise DefinitionError(
                    file, step.id, f"when: {operator} compares numbers; {ref.text} is a {kind}"
                )
            if operator in ("equals", "not_equals") and literal_type(operand) is None:
                raise DefinitionError(file, step.id, f"when: {operator} takes a literal")
        seen.add(step.id)
        earlier[step.id] = action
    # A cycle through depends_on cannot form when every dependency is earlier,
    # but the walk is cheap and the rule is stated, so it is checked outright.
    by_id = {s.id: s for s in spec.steps}
    for step in spec.steps:
        trail: list[str] = []
        at: str | None = step.id
        while at is not None:
            if at in trail:
                raise DefinitionError(
                    file, step.id, f"dependency cycle: {' -> '.join([*trail, at])}"
                )
            trail.append(at)
            at = by_id[at].depends_on if at in by_id else None


def _check_kind(spec: DefinitionDef, *, file: str) -> None:
    if spec.kind not in KINDS:
        raise DefinitionError(file, None, f"kind {spec.kind!r} is not one of {', '.join(KINDS)}")
    if spec.allowed_on not in ALLOWED_ON:
        raise DefinitionError(
            file, None, f"allowed_on {spec.allowed_on!r} is not one of {', '.join(ALLOWED_ON)}"
        )
    writes = [s for s in spec.steps if action_library.action(s.action).writes]
    if spec.kind == "note":
        if len(spec.triggers) != 1 or spec.triggers[0].kind != "alert":
            raise DefinitionError(file, None, "a note workflow is started by one alert trigger")
        if len(writes) != 1 or writes[0] is not spec.steps[-1]:
            raise DefinitionError(
                file, None, "a note workflow ends with its one write step, autotask.add_note"
            )
        last = spec.steps[-1]
        if last.action != "autotask.add_note":
            raise DefinitionError(file, last.id, "a note workflow's write is autotask.add_note")
        ref = parse_reference(last.inputs.get("note"))
        if ref is None or ref.namespace != "steps":
            raise DefinitionError(
                file, last.id, "the note comes from an earlier step: note: {steps.<id>.note}"
            )
        if spec.inputs:
            raise DefinitionError(
                file, None, "a note workflow takes no inputs; it reads the ticket"
            )
    elif spec.kind == "report":
        if len(spec.triggers) != 1 or spec.triggers[0].kind != "schedule":
            raise DefinitionError(file, None, "a report workflow is started by one schedule")
        if writes:
            raise DefinitionError(file, writes[0].id, "a report writes nothing anywhere")
        if "report" not in action_library.action(spec.steps[-1].action).outputs:
            raise DefinitionError(
                file, spec.steps[-1].id, "a report workflow's last step outputs the report"
            )
    else:
        if not spec.triggers:
            raise DefinitionError(file, None, "a run workflow declares what starts it")
        for name, given in spec.inputs.items():
            _check_identifier(file, None, "input", name)
            if given.type not in SCALAR_TYPES:
                raise DefinitionError(
                    file,
                    None,
                    f"input {name!r}: type {given.type!r} is not one of {', '.join(SCALAR_TYPES)}",
                )
            if given.source not in INPUT_SOURCES:
                raise DefinitionError(
                    file,
                    None,
                    f"input {name!r}: source {given.source!r} is not one of "
                    f"{', '.join(INPUT_SOURCES)}",
                )
            if given.default is not None and not fits(given.default, given.type):
                raise DefinitionError(
                    file, None, f"input {name!r}: default {given.default!r} is not a {given.type}"
                )
    if spec.note_mode is not None and spec.kind != "note":
        raise DefinitionError(file, None, "note_mode is for a note workflow")
    for key, setting in spec.settings.items():
        _check_identifier(file, None, "setting", key)
        if setting.type not in SCALAR_TYPES:
            raise DefinitionError(
                file,
                None,
                f"setting {key!r}: type {setting.type!r} is not one of {', '.join(SCALAR_TYPES)}",
            )
        if setting.default is not None and not fits(setting.default, setting.type):
            raise DefinitionError(
                file, None, f"setting {key!r}: default {setting.default!r} is not a {setting.type}"
            )
        if setting.required and setting.default is not None:
            raise DefinitionError(file, None, f"setting {key!r}: a required setting has no default")


def _triggers(spec: DefinitionDef, *, file: str) -> tuple[Trigger, ...]:
    found: list[Trigger] = []
    for t in spec.triggers:
        if t.kind not in TRIGGER_KINDS:
            raise DefinitionError(
                file,
                None,
                f"trigger {t.key!r}: kind {t.kind!r} is not one of {', '.join(TRIGGER_KINDS)}",
            )
        rule = None
        if t.rule is not None:
            values = t.rule.model_dump()
            if values.get("title_contains_any") is not None:
                values["title_contains_any"] = tuple(values["title_contains_any"])
            rule = TicketRule(**values)
        every = _duration(t.every, file=file, what=f"trigger {t.key!r}: every") if t.every else None
        found.append(Trigger(key=t.key, kind=t.kind, title=t.title, rule=rule, every=every))
    return tuple(found)


# --- compiling -----------------------------------------------------------------------------


def _input_model(spec: DefinitionDef) -> type[BaseModel]:
    """The run input schema the engine validates against, from the declared
    inputs: the same pydantic rules a hand-written model would carry."""
    python = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "list[string]": list[str],
    }
    fields: dict[str, Any] = {}
    for name, given in spec.inputs.items():
        annotation: Any = python[given.type]
        constraints: dict[str, Any] = {"description": given.description or None}
        if given.min is not None:
            constraints["ge"] = given.min
        if given.max is not None:
            constraints["le"] = given.max
        if given.min_length is not None:
            constraints["min_length"] = given.min_length
        if given.max_length is not None:
            constraints["max_length"] = given.max_length
        if given.required:
            fields[name] = (annotation, Field(**constraints))
        elif given.default is None:
            fields[name] = (annotation | None, Field(default=None, **constraints))
        else:
            fields[name] = (annotation, Field(default=given.default, **constraints))
    model: type[BaseModel] = create_model(
        f"{spec.name.title().replace('_', '')}Inputs",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return model


def _allowed_on(spec: DefinitionDef) -> Any:
    if spec.allowed_on == "demo":

        def demo_only(kind: str) -> str | None:
            if kind == ClientKind.DEMO:
                return None
            return (
                f"The {spec.title} workflow runs on the demo tenant only. It exists to prove "
                f"the controls, and a live client is not where that is done."
            )

        return demo_only
    if spec.allowed_on == "live":
        return lambda kind: (
            None
            if kind == ClientKind.LIVE
            else f"The {spec.title} workflow runs on live clients only."
        )
    return lambda kind: None


def jsonable(value: Any) -> Any:
    """A resolved value as it is stored on the step row and shown on the run page."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _check_resolved(action: Action, step_id: str, resolved: Mapping[str, Any]) -> None:
    """At run time, what the references came to against the action's types.
    A missing optional input is fine; a required one missing, or a value of
    the wrong type, is not — it means a step before this one was skipped by
    its condition, or a reference named the wrong thing."""
    for name, wanted in action.inputs.items():
        value = resolved.get(name)
        if value is None:
            if wanted.required:
                raise RuntimeError(f"{step_id}: input {name!r} has no value")
            continue
        if not fits(value, wanted.type):
            raise RuntimeError(
                f"{step_id}: input {name!r} is a {type(value).__name__}, not a {wanted.type}"
            )


async def _run_scope(ctx: StepContext) -> tuple[Scope, AutomationStep | None]:
    """What a run-kind step's references resolve against: the run's inputs,
    the client's settings and the outputs the earlier steps recorded."""
    rows = (
        (
            await ctx.session.execute(
                select(AutomationStep)
                .where(AutomationStep.run_id == ctx.run_id)
                .order_by(AutomationStep.position)
            )
        )
        .scalars()
        .all()
    )
    outputs: dict[str, Facts | None] = {r.step_key: r.outputs for r in rows}
    mine = next((r for r in rows if r.step_key == ctx.step_key), None)
    ticket_number = (
        await ctx.session.execute(
            select(AutomationRun.ticket_number).where(AutomationRun.id == ctx.run_id)
        )
    ).scalar_one_or_none()
    scope = Scope(
        inputs=ctx.inputs, settings=ctx.settings, steps=outputs, ticket_number=ticket_number
    )
    return scope, mine


def _compile_run(spec: DefinitionDef, *, file: str, digest: str) -> Workflow:
    steps: list[Step] = []
    for step_def in spec.steps:
        action = action_library.action(step_def.action)
        steps.append(_compile_step(step_def, action))
    return Workflow(
        name=spec.name,
        version=spec.version,
        title=spec.title,
        description=spec.description,
        inputs=_input_model(spec),
        steps=tuple(steps),
        undo_window=_duration(spec.undo_window, file=file, what="undo_window"),
        allowed_on=_allowed_on(spec),
        triggers=_triggers(spec, file=file),
        input_sources={
            name: given.source for name, given in spec.inputs.items() if given.source != "engineer"
        },
        approval_window=_duration(spec.approval.window, file=file, what="approval.window"),
        settings_used=tuple((s.key, s.description) for s in _settings(spec)),
        definition_hash=digest,
    )


def _compile_step(step_def: StepDef, action: Action) -> Step:
    """A run-kind step: the action's contract behind the engine's `Step`,
    with the references resolved against the run each time it is called and
    the resolved inputs and the outputs written on the step row."""

    async def context(ctx: StepContext) -> tuple[ActionContext, AutomationStep | None]:
        scope, row = await _run_scope(ctx)
        resolved = resolve_inputs(step_def, scope)
        _check_resolved(action, step_def.id, resolved)
        if row is not None:
            row.inputs = jsonable(resolved)
        actx = ActionContext(
            inputs=resolved,
            client_id=ctx.client_id,
            step_id=ctx.step_key,
            now=dt.datetime.now(dt.UTC),
            settings=ctx.settings,
            session=ctx.session,
            run_id=ctx.run_id,
            attempt=ctx.attempt,
            dry_run=ctx.dry_run,
            before=ctx.before,
            outputs=row.outputs if row is not None else None,
        )
        return actx, row

    async def precondition(ctx: StepContext) -> Check:
        actx, _ = await context(ctx)
        if action.precondition is None:
            return Check.passed()
        return await action.precondition(actx)

    async def plan(ctx: StepContext) -> Plan:
        actx, _ = await context(ctx)
        if action.plan is None:
            return Plan(f"{action.title}.")
        return await action.plan(actx)

    async def run(ctx: StepContext) -> Facts:
        actx, row = await context(ctx)
        result = await action.run(actx)
        if row is not None:
            row.outputs = jsonable(result)
        return result

    async def verify(ctx: StepContext) -> Check:
        actx, _ = await context(ctx)
        if action.verify is None:
            return Check.passed()
        return await action.verify(actx)

    before: FactsFn | None = None
    if action.before is not None:
        before_fn = action.before

        async def before_wrapped(ctx: StepContext) -> Facts:
            actx, _ = await context(ctx)
            return await before_fn(actx)

        before = before_wrapped

    undo: FactsFn | None = None
    if action.undo is not None:
        undo_fn = action.undo

        async def undo_wrapped(ctx: StepContext) -> Facts:
            actx, _ = await context(ctx)
            return await undo_fn(actx)

        undo = undo_wrapped

    return Step(
        key=step_def.id,
        title=step_def.title or action.title,
        precondition=precondition,
        plan=plan,
        action=run,
        verify=verify,
        before=before,
        undo=undo,
        checks=action.checks or f"nothing; {action.title.lower()} is a read",
        does=action.does or action.title.lower(),
        confirms=action.confirms or "the read came back",
        undoes=action.undoes,
        depends_on=step_def.depends_on,
        action_name=action.name,
    )


def _settings(spec: DefinitionDef) -> tuple[SettingSpec, ...]:
    return tuple(
        SettingSpec(
            key=k, type=s.type, description=s.description, required=s.required, default=s.default
        )
        for k, s in spec.settings.items()
    )


def _compile_note(spec: DefinitionDef, *, file: str, digest: str) -> NoteWorkflow:
    triggers = _triggers(spec, file=file)
    steps = tuple(spec.steps)
    acts = {s.id: action_library.action(s.action) for s in steps}
    note_ref = parse_reference(steps[-1].inputs.get("note")) if spec.kind == "note" else None

    async def run_steps(
        *,
        tools: Any,
        now: dt.datetime,
        client_id: uuid.UUID,
        settings: Mapping[str, Any],
        ticket: Arrival | None,
        period: tuple[dt.datetime, dt.datetime] | None,
        trace: list[dict[str, Any]] | None = None,
    ) -> dict[str, Facts | None]:
        outputs: dict[str, Facts | None] = {}
        for step in steps:
            action = acts[step.id]
            scope = Scope(inputs={}, settings=settings, steps=outputs, ticket=ticket)
            if action.writes:
                # Never run here: a note is posted by the notes service, as
                # the workflow's mode says, through this same action.
                outputs[step.id] = None
                if trace is not None:
                    trace.append(
                        {"id": step.id, "action": action.name, "ran": False, "why": "a write"}
                    )
                continue
            if not conditions_hold(step, scope):
                outputs[step.id] = None
                if trace is not None:
                    trace.append(
                        {"id": step.id, "action": action.name, "ran": False, "why": "condition"}
                    )
                continue
            resolved = resolve_inputs(step, scope)
            _check_resolved(action, step.id, resolved)
            outputs[step.id] = await action.run(
                ActionContext(
                    inputs=resolved,
                    client_id=client_id,
                    step_id=step.id,
                    now=now,
                    settings=settings,
                    tools=tools,
                    ticket=ticket,
                    period=period,
                )
            )
            if trace is not None:
                trace.append(
                    {
                        "id": step.id,
                        "action": action.name,
                        "ran": True,
                        "inputs": jsonable(resolved),
                        "outputs": jsonable(outputs[step.id]),
                    }
                )
        return outputs

    async def build(ctx: NoteContext) -> Draft | Skip:
        outputs = await run_steps(
            tools=ctx.tools,
            now=ctx.now,
            client_id=uuid.UUID(int=0),
            settings=ctx.settings,
            ticket=ctx.ticket,
            period=None,
        )
        assert note_ref is not None
        note = Scope(inputs={}, settings=ctx.settings, steps=outputs, ticket=ctx.ticket).resolve(
            note_ref
        )
        if isinstance(note, Draft | Skip):
            return note
        return Skip(f"{note_ref.text} produced no note")

    async def report(ctx: ReportContext) -> dict[str, Any]:
        outputs = await run_steps(
            tools=ctx.tools,
            now=ctx.now,
            client_id=uuid.UUID(int=0),
            settings=ctx.settings,
            ticket=None,
            period=(ctx.period_start, ctx.period_end),
        )
        last = outputs.get(steps[-1].id) or {}
        found = last.get("report")
        return dict(found) if isinstance(found, dict) else {"error": "no report", "devices": []}

    reads = tuple(
        step.title or acts[step.id].title
        for step in steps
        if not acts[step.id].writes and acts[step.id].connectors != ("none",)
    ) or tuple(step.title or acts[step.id].title for step in steps if not acts[step.id].writes)

    async def trace_steps(ctx: NoteContext | ReportContext) -> list[dict[str, Any]]:
        """What each step did, for `scripts/automation.py try`: the steps run as
        `build` or `report` runs them, with nothing written."""
        found: list[dict[str, Any]] = []
        if isinstance(ctx, NoteContext):
            await run_steps(
                tools=ctx.tools,
                now=ctx.now,
                client_id=uuid.UUID(int=0),
                settings=ctx.settings,
                ticket=ctx.ticket,
                period=None,
                trace=found,
            )
        else:
            await run_steps(
                tools=ctx.tools,
                now=ctx.now,
                client_id=uuid.UUID(int=0),
                settings=ctx.settings,
                ticket=None,
                period=(ctx.period_start, ctx.period_end),
                trace=found,
            )
        return found

    _TRACERS[spec.name] = trace_steps
    is_report = spec.kind == "report"
    return NoteWorkflow(
        name=spec.name,
        version=spec.version,
        title=spec.title,
        description=spec.description,
        trigger=triggers[0],
        reads=reads,
        template=spec.writes or ("A page in Desk." if is_report else "One internal note."),
        build=None if is_report else build,
        report=report if is_report else None,
        settings_used=tuple((s.key, s.description) for s in _settings(spec)),
        writes="nothing; a page in Desk" if is_report else "one internal note on the ticket",
        definition_hash=digest,
        step_views=tuple(
            {
                "id": s.id,
                "action": acts[s.id].name,
                "title": s.title or acts[s.id].title,
                "writes": acts[s.id].writes,
                "connectors": list(acts[s.id].connectors),
            }
            for s in steps
        ),
    )


# --- loading --------------------------------------------------------------------------------


def canonical(spec: DefinitionDef) -> str:
    """One definition as one string, the same on every machine.

    The parsed definition rather than the file: every mapping key in sorted
    order, no whitespace of its own, UTF-8. Two files that parse to the same
    definition — CRLF or LF, with or without a final newline, a byte order
    mark, trailing spaces, comments, or their keys in another order — give
    the same string, and a file whose *meaning* changes (a step, an input, a
    trigger, a word of the description) gives a different one.

    `exclude_defaults` keeps it to what the file actually says, so adding an
    optional field to the schema later does not move the hash of every
    definition already recorded against a run.
    """
    return json.dumps(
        spec.model_dump(mode="json", by_alias=True, exclude_defaults=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _digest(spec: DefinitionDef) -> str:
    return hashlib.sha256(canonical(spec).encode("utf-8")).hexdigest()


def parse(path: Path) -> tuple[DefinitionDef, str]:
    """A file as its schema, and the hash of its canonical form. Refuses
    anything off the schema.

    Decoded `utf-8-sig`, so a file an editor left a byte order mark on is
    read as the file it is rather than one whose first key has an invisible
    character in it.
    """
    file = path.name
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw.decode("utf-8-sig"))
    except yaml.YAMLError as exc:
        raise DefinitionError(file, None, f"not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise DefinitionError(file, None, "the file is not a mapping of keys to values")
    try:
        spec = DefinitionDef.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "the file"
        raise DefinitionError(file, None, f"{where}: {first['msg']}") from None
    if spec.name != path.stem:
        raise DefinitionError(
            file, None, f"name {spec.name!r} must match the file name {path.stem!r}"
        )
    _check_identifier(file, None, "name", spec.name)
    if spec.version < 1:
        raise DefinitionError(file, None, "version must be 1 or more")
    if not spec.description.strip() or "\n" in spec.description.strip():
        raise DefinitionError(file, None, "description is one sentence")
    return spec, _digest(spec)


def check(path: Path) -> Definition:
    """Parse, validate and compile one file without registering it."""
    spec, digest = parse(path)
    file = path.name
    _check_steps(spec, file=file)
    _check_kind(spec, file=file)
    try:
        if spec.kind == "run":
            workflow = _compile_run(spec, file=file, digest=digest)
            note = None
        else:
            workflow = None
            note = _compile_note(spec, file=file, digest=digest)
    except (InvalidWorkflow, InvalidNoteWorkflow) as exc:
        raise DefinitionError(file, None, str(exc)) from None
    return Definition(
        file=file,
        name=spec.name,
        kind=spec.kind,
        version=spec.version,
        hash=digest,
        spec=spec,
        settings=_settings(spec),
        steps=tuple(spec.steps),
        workflow=workflow,
        note_workflow=note,
    )


def validate_all(directory: Path = DEFINITIONS_DIR) -> list[tuple[Path, DefinitionError | None]]:
    """Every file in the directory, with the problem it has or None."""
    found: list[tuple[Path, DefinitionError | None]] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            check(path)
            found.append((path, None))
        except DefinitionError as exc:
            found.append((path, exc))
    return found


def load(path: Path) -> Definition:
    """Check one file and register its workflow with the engine."""
    loaded = check(path)
    if loaded.workflow is not None:
        register(loaded.workflow)
    if loaded.note_workflow is not None:
        register_note_workflow(loaded.note_workflow)
    _DEFINITIONS[loaded.name] = loaded
    return loaded


def load_all(directory: Path = DEFINITIONS_DIR) -> tuple[Definition, ...]:
    """Every definition in the directory, registered. The first bad file
    stops the load: an engine with half its workflows is not an engine."""
    for path in sorted(directory.glob("*.yaml")):
        load(path)
    return definitions()


def missing_settings_for(name: str, given: Mapping[str, Any]) -> tuple[SettingSpec, ...]:
    """The required settings a client has not set for a workflow, by name;
    nothing for a workflow that is not from a definition."""
    found = _DEFINITIONS.get(name)
    return found.missing_settings(given) if found is not None else ()


__all__ = [
    "DEFINITIONS_DIR",
    "Definition",
    "DefinitionError",
    "Reference",
    "Scope",
    "SettingSpec",
    "canonical",
    "check",
    "condition_holds",
    "definition",
    "definitions",
    "find",
    "jsonable",
    "load",
    "load_all",
    "missing_settings_for",
    "parse",
    "parse_reference",
    "resolve_inputs",
    "trace_note",
    "validate_all",
]

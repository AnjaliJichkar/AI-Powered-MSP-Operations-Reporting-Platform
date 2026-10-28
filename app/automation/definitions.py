"""What a workflow is, and what a step is.

A workflow is a name, a version, an input schema and an ordered list of steps.
A step is four functions and a flag:

* **precondition** — read only. Looks at the world and says whether the step
  may run, and what it saw. Run at dry-run time and again just before the
  action, because the world may have moved in between. At dry-run time no step
  has run, so a precondition that looks for an earlier step's work will not
  find it: it checks `ctx.dry_run` and passes on the strength of the
  dependency, saying so, rather than failing a dry run nothing could pass.
* **plan** — read only. Says exactly what the action would change, in words a
  person approves: the dry run is this, for every step, with nothing changed.
* **before** — read only. Captures the state the action is about to change,
  immediately before it changes it. This is what the ticket note, the audit
  and the undo are exact against. A step that changes nothing it could
  restore may leave it out.
* **action** — the one thing in the contract that changes anything. It must be
  **idempotent for one (run, step)**: a worker that dies between the action
  and the record of it will run the action again on recovery, and the second
  run must leave the world as the first did. Create-if-absent, set-to-value,
  never increment.
* **verify** — read only. Confirms the world now looks the way the action was
  meant to leave it. A step is `done` when verify passes, never because the
  action returned.
* **undo** — optional. Puts back what `before` captured. A step without one is
  declared *not reversible* and the dry run says so before anybody approves.

A step may name one earlier step it depends on; the engine refuses to run it
unless that step is `done`. Steps are run in order and the engine stops at the
first failure, so in practice the dependency is always satisfied — the
declaration is there so the rule survives a resume, a hand edit or a later
workflow that reorders.

Every step also says, in words, what it checks, what it does, how it confirms
the result and what its undo puts back. The workflow page prints those words;
they are the description a client's authoriser reads, so they are required.

A workflow declares what starts it (`triggers`): by hand from the Automations
page, a fixed rule on a ticket's fields, the same rule on a ticket an alert
raised, or a schedule. A rule is conditions on labels — queue, issue type,
sub-issue type, source, a title that equals or contains a phrase — evaluated
in code and never by a model. A trigger only ever drafts a run; every run is
still dry-run, submitted and approved by two people before it executes.

Nothing in this module touches a database, a vendor or a model. It is shapes
and the registry of built-in workflows.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

Facts = dict[str, Any]
"""What a check saw, as plain JSON: strings, numbers, booleans, lists, dicts.
Written to the step row and the audit as given, so a step should put only what
somebody reading the record needs — never a secret, never a whole vendor
response."""


@dataclass(frozen=True, slots=True)
class Check:
    """What a precondition or a verify check found."""

    ok: bool
    facts: Facts = field(default_factory=dict)
    reason: str | None = None
    """Why it is not ok, in words, when it is not."""

    @classmethod
    def passed(cls, **facts: Any) -> Check:
        return cls(ok=True, facts=facts)

    @classmethod
    def failed(cls, reason: str, **facts: Any) -> Check:
        return cls(ok=False, facts=facts, reason=reason)


@dataclass(frozen=True, slots=True)
class Change:
    """One thing a step would change, as the dry run lists it."""

    what: str
    """What is changed: a record, a field, a membership."""
    before: Any = None
    after: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"what": self.what, "before": self.before, "after": self.after}


@dataclass(frozen=True, slots=True)
class Plan:
    """What a step would do, in the words the approver reads."""

    summary: str
    changes: tuple[Change, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "changes": [c.as_dict() for c in self.changes]}


@dataclass(frozen=True, slots=True)
class StepContext:
    """What a step's functions are handed. The session is the step's own
    transaction: a step's action and the record of it commit together, and a
    step that raises leaves nothing of itself behind."""

    session: AsyncSession
    run_id: uuid.UUID
    client_id: uuid.UUID
    step_key: str
    inputs: Mapping[str, Any]
    """The run's inputs, validated against the workflow's schema at draft."""
    settings: Mapping[str, Any]
    """This client's settings for this workflow (`AutomationConfig.workflows`):
    lists, exclusions, anything the franchisee decides rather than the code."""
    attempt: int
    """Which attempt at this step this is. 1 the first time; higher on a resume
    or a recovery, which is how a step can tell it is being run again."""
    before: Facts | None = None
    """What `before` captured, handed to `action`, `verify` and `undo`."""
    dry_run: bool = False
    """True while the dry run is checking, which is before any step has run.

    A precondition that looks for what an earlier step produces — the record
    step one creates, the group step two empties — will not find it yet. Such a
    step says so in its own code: when `dry_run` is set and the thing is
    absent, it passes on the strength of its dependency and says that is what
    it did (`Check.passed(pending="create_record")`). At execution the same
    precondition runs with `dry_run` false and the dependency done, and the
    thing had better be there."""


CheckFn = Callable[[StepContext], Awaitable[Check]]
PlanFn = Callable[[StepContext], Awaitable[Plan]]
FactsFn = Callable[[StepContext], Awaitable[Facts]]


@dataclass(frozen=True, slots=True)
class Step:
    key: str
    title: str
    precondition: CheckFn
    plan: PlanFn
    action: FactsFn
    verify: CheckFn
    checks: str = ""
    """What the precondition looks for, in words: "the record exists and
    belongs to this run"."""
    does: str = ""
    """What the action changes: "sets the record's status to offboarded"."""
    confirms: str = ""
    """How verify knows it worked: "reads the record back and finds the status"."""
    undoes: str | None = None
    """What the undo puts back, when there is one. A step with an undo function
    and no words for it is refused, like a step with no words at all."""
    before: FactsFn | None = None
    undo: FactsFn | None = None
    depends_on: str | None = None
    action_name: str | None = None
    """The library action this step is, when the workflow came from a
    definition (`app/automation/library.py`); None for a step written in code."""

    @property
    def reversible(self) -> bool:
        return self.undo is not None


# --- what starts a workflow --------------------------------------------------------

#: The kinds of trigger. `manual` is a person on the Automations page (or a
#: ticket page action); `ticket` and `alert` are the same fixed rule on a
#: ticket's fields, told apart because an alert-raised ticket is read and
#: described differently; `schedule` is the worker drafting one every so often.
TRIGGER_KINDS = ("manual", "ticket", "alert", "schedule")

#: Where a run's input may come from when a trigger drafts it. A field not
#: named here is typed by the engineer and takes its default on a triggered
#: draft. The ticket fields are the arrival's: nothing is looked up for them.
INPUT_SOURCES: dict[str, str] = {
    "engineer": "typed by the engineer",
    "ticket.number": "the ticket's number",
    "ticket.title": "the ticket's title",
    "ticket.contact": "the ticket's contact",
    "ticket.company": "the ticket's company",
    "ticket.site": "the site the ticket belongs to",
}


@dataclass(frozen=True, slots=True)
class TicketRule:
    """Fixed conditions on a ticket's fields. Every condition given must hold;
    labels are compared case-insensitively on collapsed whitespace, a title
    pattern is exact or a substring, and there is no regex anywhere."""

    queue: str | None = None
    issue_type: str | None = None
    sub_issue_type: str | None = None
    source: str | None = None
    title_equals: str | None = None
    title_contains: str | None = None
    title_contains_any: tuple[str, ...] | None = None
    """Any one of these phrases in the title. Still a fixed list of substrings,
    not a pattern: an alert family has several wordings and one rule."""

    FIELDS = (
        "queue",
        "issue_type",
        "sub_issue_type",
        "source",
        "title_equals",
        "title_contains",
        "title_contains_any",
    )

    def conditions(self) -> dict[str, Any]:
        """The conditions that are set, in a fixed order."""
        found: dict[str, Any] = {}
        for name in self.FIELDS:
            value = getattr(self, name)
            if value is not None:
                found[name] = list(value) if isinstance(value, tuple) else value
        return found

    def with_overrides(self, overrides: Mapping[str, Any]) -> TicketRule:
        """This rule with a client's own values for some conditions. A tenant's
        labels are its own, so the values are a setting; the shape is not."""
        values: dict[str, Any] = {name: getattr(self, name) for name in self.FIELDS}
        for name, value in overrides.items():
            if name not in self.FIELDS:
                continue
            if value is None:
                values[name] = None
            elif name == "title_contains_any":
                given = value if isinstance(value, list | tuple) else [value]
                values[name] = tuple(str(v) for v in given if str(v).strip()) or None
            else:
                values[name] = str(value)
        return TicketRule(**values)


@dataclass(frozen=True, slots=True)
class Trigger:
    key: str
    kind: str
    title: str
    """What this trigger is for, in a few words: "an offboarding ticket"."""
    rule: TicketRule | None = None
    """For `ticket` and `alert`: the conditions."""
    every: dt.timedelta | None = None
    """For `schedule`: how often a run is drafted."""


class InvalidWorkflow(ValueError):
    """A definition that cannot be registered: a dependency that is not an
    earlier step, a duplicate key, a version below one, a step with no words,
    a trigger with nothing to match."""


@dataclass(frozen=True, slots=True)
class Workflow:
    name: str
    version: int
    title: str
    description: str
    inputs: type[BaseModel]
    """The input schema. A run's inputs are validated against it at draft and
    stored as they validated; a workflow with nothing to be told uses an empty
    model."""
    steps: tuple[Step, ...]
    undo_window: dt.timedelta
    """How long after a run finishes it may still be undone. Past it the undo
    is refused: a change that has stood for a week has been built on."""
    allowed_on: Callable[[str], str | None]
    """Given the client's kind (`live` or `demo`), the reason this workflow
    may not run there, or None when it may. The practice workflow is demo-only
    and says so here, where the engine checks before anything is drafted."""
    triggers: tuple[Trigger, ...] = ()
    """What starts it. Nothing without a `manual` trigger can be drafted by
    hand; nothing without a rule or a schedule is ever drafted on its own."""
    input_sources: Mapping[str, str] = field(default_factory=dict)
    """Per input field, where a triggered draft takes it from (`INPUT_SOURCES`).
    Unnamed fields are the engineer's and take their defaults."""
    approval_window: dt.timedelta = dt.timedelta(hours=24)
    """How long an approval stands. A run not started within it goes back to
    awaiting approval; a failed run resumed later than this after it stopped
    needs a fresh approval, recorded the same way."""
    settings_used: tuple[tuple[str, str], ...] = ()
    """The client settings this workflow reads (`AutomationConfig.workflows`),
    as (key, what for), so the workflow page can say what it depends on."""
    definition_hash: str = ""
    """The SHA-256 of the canonical form of the definition this workflow was
    compiled from (`library.canonical`), recorded on every run; empty for a
    workflow written in code."""

    def __post_init__(self) -> None:
        if self.version < 1:
            raise InvalidWorkflow(f"{self.name}: version must be 1 or more")
        if not self.steps:
            raise InvalidWorkflow(f"{self.name}: a workflow needs at least one step")
        seen: list[str] = []
        for step in self.steps:
            if step.key in seen:
                raise InvalidWorkflow(f"{self.name}: step key {step.key!r} is used twice")
            if step.depends_on is not None and step.depends_on not in seen:
                raise InvalidWorkflow(
                    f"{self.name}: step {step.key!r} depends on {step.depends_on!r}, which is "
                    f"not an earlier step. A step may depend only on one that runs before it."
                )
            seen.append(step.key)
        for step in self.steps:
            if not (step.checks and step.does and step.confirms):
                raise InvalidWorkflow(
                    f"{self.name}: step {step.key!r} needs words for what it checks, does and "
                    f"confirms; the workflow page prints them and the authoriser reads them."
                )
            if step.undo is not None and not step.undoes:
                raise InvalidWorkflow(
                    f"{self.name}: step {step.key!r} has an undo and no words for what it puts back"
                )
        keys: list[str] = []
        for trigger in self.triggers:
            if trigger.key in keys:
                raise InvalidWorkflow(f"{self.name}: trigger key {trigger.key!r} is used twice")
            keys.append(trigger.key)
            if trigger.kind not in TRIGGER_KINDS:
                raise InvalidWorkflow(
                    f"{self.name}: trigger {trigger.key!r} is of kind {trigger.kind!r}; "
                    f"the kinds are {', '.join(TRIGGER_KINDS)}"
                )
            if trigger.kind in ("ticket", "alert"):
                if trigger.rule is None or not trigger.rule.conditions():
                    raise InvalidWorkflow(
                        f"{self.name}: trigger {trigger.key!r} is a {trigger.kind} rule with no "
                        f"conditions, which would match every ticket"
                    )
                if trigger.kind == "alert" and not (trigger.rule.source or trigger.rule.queue):
                    raise InvalidWorkflow(
                        f"{self.name}: alert trigger {trigger.key!r} must name the source or "
                        f"the queue the alerts arrive by; that is what makes it an alert rule"
                    )
            elif trigger.kind == "schedule":
                if trigger.every is None or trigger.every < dt.timedelta(hours=1):
                    raise InvalidWorkflow(
                        f"{self.name}: schedule {trigger.key!r} needs an interval of an hour "
                        f"or more"
                    )
            elif trigger.rule is not None or trigger.every is not None:
                raise InvalidWorkflow(
                    f"{self.name}: manual trigger {trigger.key!r} takes no rule and no interval"
                )
        fields = set(self.inputs.model_fields)
        for name, source in self.input_sources.items():
            if name not in fields:
                raise InvalidWorkflow(
                    f"{self.name}: input_sources names {name!r}, which is not an input"
                )
            if source not in INPUT_SOURCES:
                raise InvalidWorkflow(
                    f"{self.name}: input {name!r} comes from {source!r}; the sources are "
                    f"{', '.join(INPUT_SOURCES)}"
                )

    def step(self, key: str) -> Step:
        for step in self.steps:
            if step.key == key:
                return step
        raise KeyError(key)

    def trigger(self, key: str) -> Trigger:
        for trigger in self.triggers:
            if trigger.key == key:
                return trigger
        raise KeyError(key)

    @property
    def manual(self) -> bool:
        """Whether a person may draft it from a page."""
        return any(t.kind == "manual" for t in self.triggers)

    def source_of(self, input_name: str) -> str:
        return self.input_sources.get(input_name, "engineer")

    @property
    def not_reversible(self) -> tuple[str, ...]:
        """The steps that cannot be undone. Listed on the dry run, before approval."""
        return tuple(step.key for step in self.steps if not step.reversible)


# --- the registry ----------------------------------------------------------------

_WORKFLOWS: dict[str, Workflow] = {}


def register(workflow: Workflow) -> Workflow:
    """Make a workflow available by name. One version per name is live at a
    time; a run records the version it was drafted against, so a run read
    after an upgrade is still read against the steps it had."""
    _WORKFLOWS[workflow.name] = workflow
    return workflow


def workflow(name: str) -> Workflow:
    try:
        return _WORKFLOWS[name]
    except KeyError:
        raise KeyError(f"no workflow called {name!r}") from None


def workflows() -> tuple[Workflow, ...]:
    return tuple(_WORKFLOWS[name] for name in sorted(_WORKFLOWS))


class UnknownWorkflow(KeyError):
    pass

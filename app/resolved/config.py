"""What a franchisee's service looks like, in settings rather than in code.

Resolved is priced and measured per outcome, so three things have to be
answerable per franchisee and none of them can be a constant:

* **What is the target?** First response and resolution, by priority, against
  business hours in the customer's own time zone, with holidays, and with the
  statuses that stop the clock.
* **What kind of ticket was it?** L1, L2 or L3, from the priority, the queue and
  the issue type.
* **Who resolved it, and what did it cost?** Inputiv or the franchisee, and a
  loaded hourly cost per engineer.

**Everything here is stored as a label, never as a picklist number.** Autotask's
status, priority, queue and issue-type numbers mean different things in
different databases (docs/api-notes/autotask.md), so a config that said
`status = 7` would be quietly wrong on the next tenant. The numbers are resolved
to labels through the tenant's own `entityInformation/fields` at read time, and
matching happens on the label, case-insensitively.

Stored with the rest of a franchisee's settings in `report_configs.config` —
one row per client, whose name is historical: it is the franchisee's settings,
and the business hours the SLA clock runs on are the ones the business review
already uses, so the two cannot drift.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: The three classes an outcome is priced at. Not configurable: they are the
#: shape of the service, and a fourth would be a product decision rather than a
#: setting. Which tickets fall into each one *is* configurable.
TICKET_CLASSES: tuple[str, ...] = ("L1", "L2", "L3")

TicketClass = Literal["L1", "L2", "L3"]


def _folded(values: list[str]) -> list[str]:
    return [value.strip().lower() for value in values if value and value.strip()]


class SlaTarget(BaseModel):
    """How long each clock may run, in minutes of the configured clock.

    Either may be absent, and an absent target is not a target of zero: the
    clock is reported as having none and no breach is ever asserted from it.
    """

    first_response_minutes: int | None = Field(default=None, gt=0)
    resolution_minutes: int | None = Field(default=None, gt=0)


class SlaConfig(BaseModel):
    """The clocks, and what stops them."""

    #: Keyed by the tenant's own priority label, matched case-insensitively.
    #: A priority with no entry falls back to `default`.
    targets: dict[str, SlaTarget] = Field(default_factory=dict)
    default: SlaTarget | None = Field(
        default=None,
        description="Used for a priority the targets do not name. Absent means such a ticket "
        "has no target, which is reported rather than treated as met.",
    )
    #: Status labels that stop the clock while a ticket sits in them — waiting
    #: on the customer, on a part, on a vendor. Matched case-insensitively
    #: against the tenant's own status labels.
    paused_status_labels: list[str] = Field(default_factory=list)
    #: Days the clock does not run at all, whatever the business hours say.
    holidays: list[dt.date] = Field(default_factory=list)
    clock: Literal["business_hours", "calendar"] = Field(
        default="business_hours",
        description="`business_hours` runs the clock only inside the franchisee's own hours; "
        "`calendar` runs it continuously, for a customer on a 24/7 agreement.",
    )

    @field_validator("paused_status_labels")
    @classmethod
    def _fold(cls, value: list[str]) -> list[str]:
        return _folded(value)

    def target_for(self, priority_label: str | None) -> SlaTarget | None:
        """The target for this priority, or the default, or nothing."""
        if priority_label:
            wanted = priority_label.strip().lower()
            for label, target in self.targets.items():
                if label.strip().lower() == wanted:
                    return target
        return self.default

    def pauses(self, status_label: str | None) -> bool:
        if not status_label:
            return False
        return status_label.strip().lower() in self.paused_status_labels


class TicketClassRule(BaseModel):
    """One rule: a ticket matching every field named here is this class.

    An empty list matches anything, so a rule naming only priorities matches on
    priority alone. Rules are tried in order and the first match wins, which is
    what makes "Critical is always L3" expressible as a rule above the rest.
    """

    ticket_class: TicketClass
    priority: list[str] = Field(default_factory=list)
    queue: list[str] = Field(default_factory=list)
    issue_type: list[str] = Field(default_factory=list)

    @field_validator("priority", "queue", "issue_type")
    @classmethod
    def _fold(cls, value: list[str]) -> list[str]:
        return _folded(value)

    def matches(self, *, priority: str | None, queue: str | None, issue_type: str | None) -> bool:
        for wanted, have in (
            (self.priority, priority),
            (self.queue, queue),
            (self.issue_type, issue_type),
        ):
            if not wanted:
                continue
            if not have or have.strip().lower() not in wanted:
                return False
        return True


class ClassificationConfig(BaseModel):
    """How a ticket becomes an L1, an L2 or an L3."""

    rules: list[TicketClassRule] = Field(default_factory=list)
    default: TicketClass = Field(
        default="L1", description="What a ticket no rule matches is counted as."
    )

    def classify(
        self, *, priority: str | None, queue: str | None, issue_type: str | None
    ) -> TicketClass:
        for rule in self.rules:
            if rule.matches(priority=priority, queue=queue, issue_type=issue_type):
                return rule.ticket_class
        return self.default


class ResolverConfig(BaseModel):
    """Which Autotask resources are ours.

    Named by whatever identifies them at that tenant — full name, user name or
    email — matched case-insensitively against all three, because a franchisee
    knows their engineers by one of the three and not always the same one.
    Everybody else is the franchisee's own.
    """

    inputiv: list[str] = Field(default_factory=list)

    @field_validator("inputiv")
    @classmethod
    def _fold(cls, value: list[str]) -> list[str]:
        return _folded(value)

    def side(self, *identifiers: str | None) -> Literal["inputiv", "franchisee", "unknown"]:
        known = [i.strip().lower() for i in identifiers if i and i.strip()]
        if not known:
            return "unknown"
        return "inputiv" if any(i in self.inputiv for i in known) else "franchisee"


class CostConfig(BaseModel):
    """Loaded hourly cost per engineer: salary, tax, benefits, tooling, over
    productive hours. Never a rate card — this is what an hour costs us, not
    what it is sold for."""

    default_hourly: Decimal | None = Field(default=None, ge=0)
    #: Keyed like `ResolverConfig.inputiv`: name, user name or email.
    by_resource: dict[str, Decimal] = Field(default_factory=dict)
    currency_symbol: str = "$"

    def hourly_for(self, *identifiers: str | None) -> Decimal | None:
        """This engineer's loaded cost, or the default, or nothing.

        Nothing is a real answer: a cost per ticket computed from a guessed rate
        is worse than no cost per ticket, so a franchisee with no cost book gets
        counts and hours and no money.
        """
        known = [i.strip().lower() for i in identifiers if i and i.strip()]
        for label, amount in self.by_resource.items():
            if label.strip().lower() in known:
                return amount
        return self.default_hourly


class ResolvedConfig(BaseModel):
    """Everything the per-outcome service needs to know about one franchisee."""

    sla: SlaConfig = Field(default_factory=SlaConfig)
    classes: ClassificationConfig = Field(default_factory=ClassificationConfig)
    resolvers: ResolverConfig = Field(default_factory=ResolverConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    reopened_within_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="A ticket completed and then opened again inside this many days is counted "
        "as reopened, which is the signal that an outcome was not one.",
    )

    @property
    def configured(self) -> bool:
        """Has anybody set this up? An unset franchisee gets counts and no
        targets, rather than targets somebody invented."""
        return bool(self.sla.targets or self.sla.default)

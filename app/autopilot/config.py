"""What the agent may say about one franchisee's tickets.

A tenant's issue-type picklist is not always the taxonomy its people actually
use. The pilot's has `Customer Onboarding Offboarding`; in ninety days they
filed nine tickets under it and put that work under `User Access and
Management` instead. The agent, offered the label, reached for it 219 times —
18% of every wrong answer, and six of the eight most confident ones. The model
was reading the subject correctly and answering a question the franchisee does
not file by.

So the policy is a setting, per franchisee, and it is applied **in code after
the model answers**:

* `forbidden_issue_types` — labels the agent may not predict. One that comes
  back anyway becomes `unsure`, which is a decline and is scored as one.
* `issue_type_remap` — labels that mean something else here. `Customer
  Onboarding Offboarding -> User Access and Management` says "when you see
  this, we file it as that".

**After the answer, not before, and both kept.** Narrowing the offered set
would hide what the model actually said; applying the policy afterwards leaves
the raw answer on the row (`issue_type_raw`) so the policy's effect is a
measurement rather than an assumption, and so a label setting chosen on one
period can be scored on another.

Stored with the franchisee's other settings (`report_configs.config`, as
`autopilot`), and the whole block is copied onto every run's config, because a
run whose label policy is unknown cannot be compared to one whose is.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

#: What happened to a predicted label. On the row so a score can be taken
#: before or after the policy.
Policy = Literal["kept", "remapped", "forbidden"]


class AutopilotConfig(BaseModel):
    """One franchisee's label policy. Empty by default: no policy is a policy
    somebody has to choose, and a replay with none is the honest baseline."""

    forbidden_issue_types: list[str] = Field(
        default_factory=list,
        description="Issue-type labels the classifier may not predict for this "
        "franchisee. A forbidden label that comes back becomes `unsure`.",
    )
    issue_type_remap: dict[str, str] = Field(
        default_factory=dict,
        description="Issue-type labels this franchisee files as something else: "
        "the label the model said, to the label they use.",
    )

    @field_validator("forbidden_issue_types")
    @classmethod
    def _no_blanks(cls, value: list[str]) -> list[str]:
        return [label.strip() for label in value if label and label.strip()]

    @model_validator(mode="after")
    def _a_remap_may_not_point_at_a_forbidden_label(self) -> AutopilotConfig:
        """Or the policy would quietly turn a good answer into a decline."""
        forbidden = {label.casefold() for label in self.forbidden_issue_types}
        pointing = {
            target for target in self.issue_type_remap.values() if target.casefold() in forbidden
        }
        if pointing:
            raise ValueError(
                f"{len(pointing)} remap(s) point at a forbidden label, which would turn "
                "an answer into a decline. Remove one or the other."
            )
        return self

    @property
    def configured(self) -> bool:
        return bool(self.forbidden_issue_types or self.issue_type_remap)

    def apply(self, predicted: str | None) -> tuple[str | None, Policy]:
        """The label this franchisee would file, and what the policy did.

        Remap first, then the forbidden check on the result: a label that is
        remapped is being *translated*, and translating it before asking
        whether it is allowed is the order that makes
        `A -> B, forbid A` mean "we file A as B" rather than "we decline A".
        """
        if predicted is None:
            return None, "kept"
        final = predicted
        policy: Policy = "kept"
        for source, target in self.issue_type_remap.items():
            if source.casefold() == predicted.casefold():
                final, policy = target, "remapped"
                break
        if any(label.casefold() == final.casefold() for label in self.forbidden_issue_types):
            return "unsure", "forbidden"
        return final, policy

    def as_json(self) -> dict[str, Any]:
        """For a run's config, so two runs' policies can be told apart."""
        return {
            "forbidden_issue_types": list(self.forbidden_issue_types),
            "issue_type_remap": dict(self.issue_type_remap),
        }

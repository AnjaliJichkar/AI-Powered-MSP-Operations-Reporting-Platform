"""The shape a prediction must have, and the refusal when it does not.

Two things are enforced here and nowhere else:

* **The values are a closed set.** `origin` and `urgency` are literals;
  `issue_type` must be one of the tenant's own labels or `unsure`, checked
  against the list the run was built with rather than against anything the
  model offers. A label this tenant does not have is not a near miss to be
  tidied up — it is an invalid answer.
* **An invalid answer is recorded, never repaired.** `parse_prediction` either
  returns a prediction or raises `InvalidPrediction`, and the runner stores
  that as an error row. Nothing here fills in a missing field, rounds a
  confidence into range, or maps an unknown label onto the closest known one.
  A guess dressed as an answer is precisely what Level 0 exists to catch, and
  code that quietly fixes the model's output is code that hides it.

`UNSURE` is a first-class answer, not a failure: scoring counts it apart from
both right and wrong, because an agent that declines is the behaviour the
confidence thresholds are being chosen to produce.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

#: What the model says when the subject line does not tell it.
UNSURE = "unsure"

Origin = Literal["alert", "human", "unsure"]
Urgency = Literal["low", "normal", "high"]

#: The name the model must call. Forced with `tool_choice`, so a text answer is
#: not one of the outcomes.
TOOL_NAME = "classify_ticket"


class Prediction(BaseModel):
    """One ticket's triage, as the model returned it.

    `model_config` forbids extra keys: a model that invents a field is telling
    us something about the prompt, and swallowing it would lose that.
    """

    model_config = {"extra": "forbid"}

    origin: Origin
    origin_confidence: float = Field(ge=0.0, le=1.0)
    issue_type: str
    issue_type_confidence: float = Field(ge=0.0, le=1.0)
    urgency: Urgency
    urgency_confidence: float = Field(ge=0.0, le=1.0)


class InvalidPrediction(ValueError):
    """The model's answer did not fit the schema, or named a label the tenant
    does not have. Recorded as an error; never corrected."""


def classify_tool(issue_types: list[str]) -> dict[str, Any]:
    """The tool definition, with this tenant's own issue types enumerated.

    The labels go in the schema as an `enum` rather than only in the prompt, so
    an out-of-set answer is a schema violation the API itself discourages —
    and `parse_prediction` checks it again on the way back, because a vendor's
    schema enforcement is not something to rely on for correctness.
    """
    return {
        "name": TOOL_NAME,
        "description": (
            "Record the triage of one ticket from its subject line. Call exactly once, "
            "with no other text: these six values are the whole answer."
        ),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "origin": {
                    "type": "string",
                    "enum": ["alert", "human", UNSURE],
                    "description": "alert for machine-generated, human for a person, "
                    "unsure when the subject does not say.",
                },
                "origin_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "issue_type": {
                    "type": "string",
                    "enum": [*issue_types, UNSURE],
                    "description": "One of this customer's own issue types, spelled as "
                    "listed, or unsure.",
                },
                "issue_type_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                "urgency_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": [
                "origin",
                "origin_confidence",
                "issue_type",
                "issue_type_confidence",
                "urgency",
                "urgency_confidence",
            ],
        },
    }


def parse_prediction(payload: Any, issue_types: list[str]) -> Prediction:
    """The model's tool input as a `Prediction`, or `InvalidPrediction`.

    The issue type is matched against the tenant's labels **exactly**: no
    case-folding, no stripping, no nearest match. The prompt gives the model
    the spelling to use; a different spelling is a wrong answer we want to
    see, not a formatting detail to absorb.
    """
    if not isinstance(payload, dict):
        raise InvalidPrediction(f"the model returned {type(payload).__name__}, not an object")
    try:
        prediction = Prediction.model_validate(payload)
    except ValidationError as exc:
        # The error's own text, not the model's output: a message quoting the
        # answer could carry the ticket's wording into the row.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['type']}"
            for error in exc.errors()[:4]
        )
        raise InvalidPrediction(f"the answer did not validate ({problems})") from exc
    if prediction.issue_type != UNSURE and prediction.issue_type not in issue_types:
        raise InvalidPrediction("the answer named an issue type this tenant does not have")
    return prediction

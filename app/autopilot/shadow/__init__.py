"""Shadow mode: what the agent would have said, measured against what happened.

Nothing in this package reaches a vendor. It imports no connector factory and
no vendor client, so a replay cannot call Autotask by accident — the forward
shadow's live reads are injected from outside (`app/autopilot/wiring.py`), and
`tests/autopilot/test_imports.py` fails if that ever stops being true.
"""

from app.autopilot.shadow.cost import Budget
from app.autopilot.shadow.grouping import Grouped, Grouper, normalise
from app.autopilot.shadow.prompts import CLASSIFY_SYSTEM_PROMPT, PROMPT_VERSION
from app.autopilot.shadow.replay import (
    InputVariant,
    ReplayConfig,
    ReplayResult,
    issue_types_for,
    replay,
)
from app.autopilot.shadow.rules import RuleAnswer, apply_rules, rules_for
from app.autopilot.shadow.schema import (
    UNSURE,
    InvalidPrediction,
    Prediction,
    classify_tool,
    parse_prediction,
)

__all__ = [
    "CLASSIFY_SYSTEM_PROMPT",
    "PROMPT_VERSION",
    "UNSURE",
    "Budget",
    "Grouped",
    "Grouper",
    "InputVariant",
    "InvalidPrediction",
    "Prediction",
    "ReplayConfig",
    "ReplayResult",
    "RuleAnswer",
    "apply_rules",
    "classify_tool",
    "issue_types_for",
    "normalise",
    "parse_prediction",
    "replay",
    "rules_for",
]

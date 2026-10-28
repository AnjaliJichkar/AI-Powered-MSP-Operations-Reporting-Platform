"""The action library: every block a workflow definition may name.

Importing the package registers them all (`base.actions()` lists them). A
new action is a module here and a line below; a new write is also a line on
`base.WRITE_SCOPES`, which is the decision.
"""

from app.automation.actions import notes as _notes  # noqa: F401
from app.automation.actions import practice as _practice  # noqa: F401
from app.automation.actions import reads as _reads  # noqa: F401
from app.automation.actions import reports as _reports  # noqa: F401
from app.automation.actions import write as _write  # noqa: F401
from app.automation.actions.base import (
    CONNECTORS,
    TOOL_CONNECTORS,
    TYPES,
    WRITE_SCOPES,
    Action,
    ActionContext,
    Input,
    InvalidAction,
    Output,
    action,
    actions,
    fits,
    register_action,
)

__all__ = [
    "CONNECTORS",
    "TOOL_CONNECTORS",
    "TYPES",
    "WRITE_SCOPES",
    "Action",
    "ActionContext",
    "Input",
    "InvalidAction",
    "Output",
    "action",
    "actions",
    "fits",
    "register_action",
]

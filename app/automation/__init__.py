"""The automation engine: fixed, versioned workflows run under controls.

No AI anywhere in this package. A workflow is a definition — one YAML file in
`definitions/`, assembled from the action library in `actions/` — compiled
into steps, each with a read-only precondition, an action, a verify check
and, where it is possible, an undo. Nothing here imports the agent, the
autopilot or the Anthropic client, and `tests/automation/test_imports.py`
checks the live import graph to keep it so — the same guard the shadow
package has against vendors.

The only write any workflow can make is adding an internal note to an
Autotask ticket, through the engine's own write client. Every write an
action may make is on `actions.base.WRITE_SCOPES`, and that list is the
decision.
"""

# The definitions register themselves when the library loads them. Loaded
# here so that anything which imports the engine finds every workflow in the
# registry, and so the no-AI guard's import probe covers the loader and the
# actions.
from app.automation import library as _library
from app.automation import notes as _notes  # noqa: F401
from app.automation import practice as _practice  # noqa: F401

_library.load_all()

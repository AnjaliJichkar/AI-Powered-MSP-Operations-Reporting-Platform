"""The note workflows: alert tickets answered with one internal note built
from facts read from the tools. See `definitions.py` for the contract and
`docs/automation-notes.md` for the rollout. No model anywhere in here; the
package is under the same import guard as the rest of the engine.

Importing the package registers the built-in workflows.
"""

from app.automation.notes import workflows as _workflows  # noqa: F401

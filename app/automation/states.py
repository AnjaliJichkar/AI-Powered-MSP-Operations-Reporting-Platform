"""A run's states, and the one place that says which may follow which.

    drafted ─► dry_run_ready ─► awaiting_approval ─► approved ─► running ─► completed
       │             │                 │                 │           │
       │             └─► drafted       └─► drafted       │           └─► failed ─► running
       │                                                 │                  │
       └─────────────── cancelled ◄──────────────────────┘                  ├─► partly_completed
                                                                            └─► undone
    completed ─► undone      partly_completed ─► undone

    and two ways back to awaiting_approval, both about the approval's age:
    approved ─► awaiting_approval   when the run did not start inside the window
    failed   ─► awaiting_approval   when a resume comes later than the window

The words:

* **drafted** — the run exists with its inputs. Nothing has been checked.
* **dry_run_ready** — every precondition passed and the checklist of what
  each step would change is on the run. A dry run with a failing precondition
  leaves the run drafted, with the failure shown, so it can be fixed and
  re-run.
* **awaiting_approval** — the engineer has put the checklist in front of the
  client. Going back to drafted is withdrawing it.
* **approved** — the client's authoriser said yes and an engineer other than
  the one who started it recorded that. The worker may take it, for as long
  as the workflow's approval window says; past it the run goes back to
  awaiting approval, and that approval is on the record as expired.
* **running** — the worker has it. The kill switch holds it here between
  steps; it does not change the state.
* **completed** — every step is `done`. Undo is open until the deadline.
* **failed** — stopped at the first step that failed. Resumable from that
  step once whatever failed is fixed; completed steps are not repeated. A
  resume later than the approval window after it stopped needs a fresh
  approval first: the run goes back to awaiting approval and is approved the
  same way again.
* **partly_completed** — a failed run somebody gave up on, with some steps
  done. Undo is open for those.
* **cancelled** — stopped before any step ran. Terminal.
* **undone** — the undo of every reversible completed step ran, in reverse.
  Terminal.

Nothing else moves a run. `advance` is the only writer of `state`, it refuses
anything not in the table, and the test of the table is the test of the
lifecycle.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from app.models.automation import AutomationRun


class RunState(StrEnum):
    DRAFTED = "drafted"
    DRY_RUN_READY = "dry_run_ready"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTLY_COMPLETED = "partly_completed"
    CANCELLED = "cancelled"
    UNDONE = "undone"


TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.DRAFTED: frozenset({RunState.DRY_RUN_READY, RunState.CANCELLED}),
    RunState.DRY_RUN_READY: frozenset(
        {RunState.AWAITING_APPROVAL, RunState.DRAFTED, RunState.CANCELLED}
    ),
    RunState.AWAITING_APPROVAL: frozenset(
        {RunState.APPROVED, RunState.DRAFTED, RunState.CANCELLED}
    ),
    RunState.APPROVED: frozenset(
        {RunState.RUNNING, RunState.AWAITING_APPROVAL, RunState.CANCELLED}
    ),
    RunState.RUNNING: frozenset({RunState.COMPLETED, RunState.FAILED}),
    RunState.FAILED: frozenset(
        {
            RunState.RUNNING,
            RunState.AWAITING_APPROVAL,
            RunState.PARTLY_COMPLETED,
            RunState.UNDONE,
        }
    ),
    RunState.COMPLETED: frozenset({RunState.UNDONE}),
    RunState.PARTLY_COMPLETED: frozenset({RunState.UNDONE}),
    RunState.CANCELLED: frozenset(),
    RunState.UNDONE: frozenset(),
}

#: The states an undo may be asked for from. Not `running`: a run in flight is
#: stopped by the kill switch, and undone once it has stopped.
UNDOABLE = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.PARTLY_COMPLETED})

#: The states a run has finished in, one way or another.
TERMINAL = frozenset({RunState.CANCELLED, RunState.UNDONE})

#: The words a page prints for each, and the tone of the dot beside them.
WORDS: dict[RunState, tuple[str, str]] = {
    RunState.DRAFTED: ("Drafted", "neutral"),
    RunState.DRY_RUN_READY: ("Dry run ready", "neutral"),
    RunState.AWAITING_APPROVAL: ("Awaiting approval", "warn"),
    RunState.APPROVED: ("Approved", "accent"),
    RunState.RUNNING: ("Running", "accent"),
    RunState.COMPLETED: ("Completed", "good"),
    RunState.FAILED: ("Failed", "bad"),
    RunState.PARTLY_COMPLETED: ("Partly completed", "warn"),
    RunState.CANCELLED: ("Cancelled", "neutral"),
    RunState.UNDONE: ("Undone", "neutral"),
}


class InvalidTransition(ValueError):
    def __init__(self, run_id: object, current: RunState, wanted: RunState) -> None:
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "nothing"
        super().__init__(
            f"run {run_id} is {current.value} and cannot become {wanted.value}; "
            f"from {current.value} it can become: {allowed}"
        )
        self.current = current
        self.wanted = wanted


def may_advance(current: RunState | str, wanted: RunState | str) -> bool:
    return RunState(wanted) in TRANSITIONS[RunState(current)]


def advance(run: AutomationRun, wanted: RunState | str, *, now: dt.datetime) -> None:
    """Move a run to its next state, or refuse. The only writer of `state`."""
    current = RunState(run.state)
    target = RunState(wanted)
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(run.id, current, target)
    run.state = target.value
    run.updated_at = now
    if target is RunState.RUNNING:
        if run.started_at is None:
            run.started_at = now
        # A resume: the run is no longer finished, and the failure it is
        # resuming from is the step row's to remember, not the run's.
        run.finished_at = None
        run.failure = None
        # The undo window is counted from when the run last stopped; a run
        # that is going again has not stopped yet.
        run.undo_deadline_at = None
    if target in (
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.PARTLY_COMPLETED,
        RunState.CANCELLED,
    ):
        run.finished_at = now
    if target is RunState.UNDONE:
        run.undone_at = now

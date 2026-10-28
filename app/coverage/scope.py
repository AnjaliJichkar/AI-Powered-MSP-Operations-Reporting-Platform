"""Which machines a site actually buys backup for, applied to the board.

Every other tool on the coverage board is rolled out to a fleet: a managed
computer either has a protection agent or it is a gap. Backup is not sold that
way. A customer who backs up their servers and not their laptops has no gap on
those laptops, and a board that counts them as gaps tells them the same wrong
thing every month until they stop reading it. Live, before this: the pilot's
Backup column read **5.4%**, which is the share of their fleet that happens to
be servers.

The rule is not invented here. `effective_scope` in `app/reports/tbr/backup.py`
is the one the review's gap list and the ticket panel already apply — an
explicit per-site setting, or `auto`, which reads the answer from what Cove
already holds. This module is that rule applied to the join's verdicts, so the
board, the review, the ticket panel and Ask all say the same thing about the
same laptop.

What it changes and what it does not:

* a machine outside its site's scope with **no** Cove device is `not_in_scope`
  rather than `absent` or `unmatched`: the question was asked and the answer is
  that nobody expects a backup there. It is out of the percentage and counted
  on its own line.
* a machine outside the scope that Cove **does** hold keeps its verdict. Being
  backed up is the end of the argument about whether it should be.
* `excluded`, `not_assessed`, `rmm_stale` and `rmm_agent_down` are untouched:
  each says something this does not contradict.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.coverage.core import Assessment, ToolState

if TYPE_CHECKING:  # pragma: no cover - a type, never a runtime import
    # `app.connectors.cove.models` reaches `app.connectors.__init__`, which
    # imports the coverage tools, which import `app.coverage.core` — and this
    # module is below that, so importing it for real is a circular import that
    # surfaces as a partially initialised `core` on an unrelated module
    # (CLAUDE.md, "Working conventions"). Nothing here touches a CoveDevice at
    # runtime; it is only the shape of what the caller passes.
    from app.connectors.cove.models import CoveDevice

logger = logging.getLogger(__name__)

#: The board entry this applies to. One tool, named rather than inferred: the
#: next tool sold per machine is a decision somebody makes, not a default.
BACKUP = "backup"


@dataclass(frozen=True, slots=True)
class SiteScope:
    """One site's backup scope, and the sentence that says which rule applied."""

    scope: str
    why: str
    mixed: bool = False
    """`auto` guessed servers only while somebody is backing up a workstation
    here. Reported so a person can settle it with one setting; never rendered
    for a customer, because it is a statement about our rule."""

    @property
    def servers_only(self) -> bool:
        return self.scope == "servers"


def site_scopes(
    *,
    settings: object,
    machines_by_site: Mapping[uuid.UUID, Sequence[CoveDevice]],
    slugs: Mapping[uuid.UUID, str],
    workstations: Mapping[uuid.UUID, int] | None = None,
) -> dict[uuid.UUID, SiteScope]:
    """Each site's effective backup scope, by the review's own rule.

    `settings` is the franchisee's `BackupConfig`. It is typed loosely on
    purpose: importing it here would pull `app.reports` into the coverage
    package, and the one function needed is imported inside this call for the
    same reason `BackupConfig` itself lives apart from the connectors
    (CLAUDE.md, "Working conventions").
    """
    from app.reports.tbr.backup import effective_scope

    scopes: dict[uuid.UUID, SiteScope] = {}
    for site_id, slug in slugs.items():
        configured = settings.scope_for(slug)  # type: ignore[attr-defined]
        decision = effective_scope(
            configured,
            machines=list(machines_by_site.get(site_id, ())),
            # How many workstations the site has, so `auto` can ask how many
            # are backed up rather than whether any is. Without it one laptop
            # at a 107-machine customer makes the other 106 gaps.
            workstations=(workstations or {}).get(site_id),
        )
        scopes[site_id] = SiteScope(decision.scope, decision.why, decision.mixed)
    return scopes


def apply_backup_scope(
    assessment: Assessment,
    *,
    scopes: Mapping[uuid.UUID, SiteScope],
    is_server: Mapping[str, bool],
) -> int:
    """Flip out-of-scope backup gaps to `not_in_scope`. Returns how many moved.

    `is_server` is keyed by device id, decided once by the caller from the same
    `device_role` rule the review and the ticket panel use, so three readers
    cannot disagree about what a machine is.
    """
    moved = 0
    for entry in assessment.devices:
        state = entry.tools.get(BACKUP)
        if state is None or state.state not in ("absent", "unmatched"):
            continue
        site_id = entry.device.site_id
        scope = scopes.get(site_id) if isinstance(site_id, uuid.UUID) else None
        if scope is None or not scope.servers_only:
            continue
        if is_server.get(entry.device.device_id, False):
            continue
        entry.tools[BACKUP] = ToolState("not_in_scope", scope.why)
        moved += 1
    if moved:
        logger.info("coverage: %d machine(s) are outside their site's backup scope", moved)
    return moved

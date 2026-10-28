"""One franchisee's backup settings.

Its own module, and deliberately light: `FranchiseeConfig` imports it, and
anything that reaches `app.connectors` from there is a circular import —
`app/connectors/__init__.py` pulls in the Autotask tools, which read the
settings. So the one connector function this needs is imported inside the
method that uses it.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, Field

#: What a site's backup scope may be set to.
SCOPES: Final = ("auto", "servers", "servers_and_workstations")


class BackupConfig(BaseModel):
    """What this franchisee has decided about backup coverage.

    `no_backup_needed` is the list of machines that are not expected to have
    a backup, per site: a till, a lab box, a spare laptop. Without it the
    coverage gap grows a permanent tail of machines everybody has already
    decided about, and a list nobody can act on is one nobody reads.
    """

    no_backup_needed: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Site slug -> machine names that are not expected to be backed up. "
        "Matched the way the gap is matched: lower case, with Cove's own suffix removed.",
    )

    coverage_reviewed: dict[str, bool] = Field(
        default_factory=dict,
        description="Site slug -> whether somebody has been through that site's backup "
        "coverage list. Until they have, the customer's copy says the review is pending "
        "rather than printing a list nobody has checked; the coverage board and Ask show it "
        "in full either way, because those are ours.",
    )
    scope: dict[str, str] = Field(
        default_factory=dict,
        description="Site slug -> 'auto' (the default), 'servers' or "
        "'servers_and_workstations'. Which machines are expected to have a backup is a fact "
        "about what the customer bought, and it differs site by site: some back up servers "
        "only, some back up everything. 'auto' reads it from what Cove already holds there; "
        "an explicit setting overrides it, because a customer who has bought workstation "
        "backup and not installed it yet is exactly the case auto gets wrong, and the case "
        "where the gap list matters most.",
    )

    def reviewed_for(self, site_slug: str) -> bool:
        return bool(self.coverage_reviewed.get(site_slug, False))

    def scope_for(self, site_slug: str) -> str:
        """What this site is set to. `auto` until somebody decides otherwise."""
        wanted = self.scope.get(site_slug, "auto")
        return wanted if wanted in SCOPES else "auto"

    def excluded_for(self, site_slug: str) -> set[str]:
        """The machines this site has decided need no backup, as match keys."""
        from app.connectors.cove.columns import device_key

        return {device_key(name) for name in self.no_backup_needed.get(site_slug, [])}

"""The board counts a backup gap only where the customer buys backup.

Every other tool here is rolled out to a fleet: a managed computer either has
a protection agent or it is a gap. Backup is sold per machine, and a customer
who backs up their servers and not their laptops has no gap on those laptops.
A board that counted them told the pilot their Backup coverage was **5.4%**,
which was the share of their fleet that happened to be servers.

The rule is `effective_scope`, the one the review's gap list and the ticket
panel already apply, so the four readers cannot come to two views of one
laptop. These tests are the servers-only site it was written for.
"""

from __future__ import annotations

import datetime as dt
import uuid

# `app.connectors` first, and deliberately: `app.coverage.core` imports
# `app.connectors.matching`, whose package imports the coverage tools, which
# import `app.coverage.config`, which imports `core` — so a test whose first
# application import is `app.coverage.*` starts that cycle from the middle and
# gets a partially initialised module. Every other test in this package loads a
# connector model first for the same reason.
from app.connectors.cove.models import CoveDevice
from app.coverage.core import (
    Assessment,
    DeviceCoverage,
    ManagedDevice,
    ToolState,
)
from app.coverage.scope import SiteScope, apply_backup_scope, site_scopes

NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)
SITE = uuid.uuid5(uuid.NAMESPACE_DNS, "servers-only")
OTHER = uuid.uuid5(uuid.NAMESPACE_DNS, "everything")


def _device(name: str, site: uuid.UUID = SITE) -> ManagedDevice:
    return ManagedDevice(
        site_id=site,
        site_slug="servers-only" if site == SITE else "everything",
        site_name="Servers Only Ltd" if site == SITE else "Everything Ltd",
        device_id=name.lower(),
        name=name,
        node_class="WINDOWS_WORKSTATION",
        last_contact=NOW - dt.timedelta(hours=2),
    )


def _assessment(*entries: tuple[str, str, uuid.UUID]) -> Assessment:
    """One device per entry, with the backup verdict the join reached."""
    return Assessment(
        devices=[
            DeviceCoverage(device=_device(name, site), tools={"backup": ToolState(state, state)})  # type: ignore[arg-type]
            for name, state, site in entries
        ],
        unmanaged=[],
    )


def _states(assessment: Assessment) -> dict[str, str]:
    return {d.device.name: d.tools["backup"].state for d in assessment.devices}


class _Settings:
    """The franchisee's backup settings, as `BackupConfig` answers them."""

    def __init__(self, **by_slug: str) -> None:
        self._by_slug = by_slug

    def scope_for(self, slug: str) -> str:
        return self._by_slug.get(slug, "auto")


def test_a_workstation_at_a_servers_only_site_is_not_a_gap() -> None:
    assessment = _assessment(
        ("SRV-DC01", "absent", SITE),
        ("WS-ACCOUNTS", "absent", SITE),
        ("WS-RECEPTION", "unmatched", SITE),
    )
    moved = apply_backup_scope(
        assessment,
        scopes={SITE: SiteScope("servers", "Servers only, by this site's setting.")},
        is_server={"srv-dc01": True, "ws-accounts": False, "ws-reception": False},
    )
    assert moved == 2
    assert _states(assessment) == {
        # The server is still a gap: this site does buy backup for it.
        "SRV-DC01": "absent",
        # The two workstations are a decision, not a failure.
        "WS-ACCOUNTS": "not_in_scope",
        "WS-RECEPTION": "not_in_scope",
    }
    # And the reason is the rule that applied, which is what a reader needs.
    reasons = {d.device.name: d.tools["backup"].detail for d in assessment.devices}
    assert reasons["WS-ACCOUNTS"] == "Servers only, by this site's setting."


def test_a_machine_cove_already_holds_keeps_its_verdict() -> None:
    """Being backed up ends the argument about whether it should be.

    A workstation Cove holds is `covered` or `stale`, and neither becomes
    `not_in_scope`: somebody is paying for that backup, and a stale one is
    the finding the column exists for.
    """
    assessment = _assessment(("WS-SALES", "covered", SITE), ("WS-DESIGN", "stale", SITE))
    moved = apply_backup_scope(
        assessment,
        scopes={SITE: SiteScope("servers", "Servers only, by this site's setting.")},
        is_server={"ws-sales": False, "ws-design": False},
    )
    assert moved == 0
    assert _states(assessment) == {"WS-SALES": "covered", "WS-DESIGN": "stale"}


def test_a_site_that_backs_up_everything_keeps_every_gap() -> None:
    assessment = _assessment(("WS-ONE", "absent", OTHER), ("WS-TWO", "absent", OTHER))
    moved = apply_backup_scope(
        assessment,
        scopes={
            OTHER: SiteScope("servers_and_workstations", "Servers and workstations."),
        },
        is_server={"ws-one": False, "ws-two": False},
    )
    assert moved == 0
    assert set(_states(assessment).values()) == {"absent"}


def test_a_state_this_does_not_speak_for_is_left_alone() -> None:
    """`excluded`, `not_assessed` and the RMM's own states each say something.

    None of them is "this machine has no backup", so none of them is replaced:
    a device somebody excluded with a reason keeps the reason, and a tool that
    did not answer is still a tool that did not answer.
    """
    assessment = _assessment(
        ("WS-GONE", "rmm_stale", SITE),
        ("WS-EXCLUDED", "excluded", SITE),
        ("WS-UNREAD", "not_assessed", SITE),
    )
    moved = apply_backup_scope(
        assessment,
        scopes={SITE: SiteScope("servers", "Servers only, by this site's setting.")},
        is_server=dict.fromkeys(["ws-gone", "ws-excluded", "ws-unread"], False),
    )
    assert moved == 0
    assert _states(assessment) == {
        "WS-GONE": "rmm_stale",
        "WS-EXCLUDED": "excluded",
        "WS-UNREAD": "not_assessed",
    }


def test_auto_reads_the_scope_from_what_cove_already_holds() -> None:
    """The review's own `auto`: a site with a backed-up workstation buys them.

    Read through `site_scopes`, so the board gets the rule rather than a copy
    of it; a site with only servers in Cove is not told every month that its
    laptops have no backup.
    """

    def cove(name: str, os_type: str) -> CoveDevice:
        return CoveDevice(
            account_id=1,
            partner_id=1,
            name=name,
            key=name.lower(),
            os_type=os_type,
            account_type="backup_manager",
        )

    scopes = site_scopes(
        settings=_Settings(),
        machines_by_site={
            SITE: [cove("SRV-DC01", "server")],
            OTHER: [cove("SRV-DC01", "server"), cove("WS-SALES", "workstation")],
        },
        slugs={SITE: "servers-only", OTHER: "everything"},
    )
    assert scopes[SITE].servers_only
    assert "no workstations are backed up here" in scopes[SITE].why
    assert not scopes[OTHER].servers_only


def test_an_explicit_setting_beats_what_cove_holds() -> None:
    """The customer who bought workstation backup and has not installed it.

    `auto` gets that one wrong, and it is the case where the gap list matters
    most, so an explicit setting wins — here and in the review alike.
    """
    scopes = site_scopes(
        settings=_Settings(**{"servers-only": "servers_and_workstations"}),
        machines_by_site={SITE: []},
        slugs={SITE: "servers-only"},
    )
    assert not scopes[SITE].servers_only
    assert "by this site's setting" in scopes[SITE].why

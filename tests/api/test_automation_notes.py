"""The note workflows, built from fixtures and never from a tool or a model.

A fake `Tools` answers every read from values written here, including the
two things a live tool does that a fixture must also do: fail to answer, and
match a device without confidence. The writer is a fake that records what it
was asked to post. The service, the switches, the kill switch, the audit
rows and the trigger log are the real ones against the test database.
"""

# ruff: noqa: F811  (the `world` fixture is imported from the engine tests)
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select

from app.automation import controls, service, triggers
from app.automation.notes import service as notes
from app.automation.notes.backup_formats import parse_backup_notice
from app.automation.notes.definitions import (
    AlertFact,
    DetectionFact,
    DeviceMatch,
    DeviceState,
    Draft,
    NoteContext,
    ProtectionFact,
    Read,
    Skip,
    TicketFact,
    VolumeFact,
    normalised_kind,
    note_workflow,
    note_workflows,
)
from app.automation.notes.workflows import MDR_LINE
from app.models import AuditEventType, AutomationAlertTicket, AutomationNote
from tests.api.conftest import audit_rows
from tests.api.test_automation_engine import World, world  # noqa: F401

pytestmark = pytest.mark.usefixtures("clean")

NOW = dt.datetime(2026, 10, 13, 9, 30, tzinfo=dt.UTC)
SRC = "Monitoring Alert"
DEVICE = DeviceMatch(
    device_id="d1",
    name="CON-FS-01",
    site_slug="contoso-legal",
    confidence="high",
    method="hostname",
)
LOW = DeviceMatch(
    device_id="d2", name="CON-LT-07", site_slug="contoso-legal", confidence="low", method="contact"
)


def arrival(
    ticket_id: str, title: str, *, description: str | None = None, source: str = SRC
) -> triggers.Arrival:
    return triggers.Arrival(
        ticket_id=ticket_id,
        ticket_number=f"T{ticket_id}",
        title=title,
        source=source,
        description=description
        or f"NinjaOne Monitoring reports: {title.lower()} on CON-FS-01. Please investigate.",
        site_slug="contoso-legal",
        created_at=NOW - dt.timedelta(minutes=50),
    )


@dataclass
class FakeTools:
    """Every read answered from here. `down` names reads that do not answer."""

    device: DeviceMatch | None = DEVICE
    down: set[str] = field(default_factory=set)
    state: DeviceState = field(
        default_factory=lambda: DeviceState(
            online=True,
            last_contact=NOW - dt.timedelta(minutes=7),
            last_boot=NOW - dt.timedelta(minutes=9),
        )
    )
    alerts: list[AlertFact] = field(default_factory=list)
    volumes_: list[VolumeFact] = field(
        default_factory=lambda: [
            VolumeFact("C:", 237.0, 4.2, 1.8),
            VolumeFact("D:", 500.0, 120.0, 24.0),
        ]
    )
    tickets: list[TicketFact] = field(default_factory=list)
    protection: ProtectionFact = field(
        default_factory=lambda: ProtectionFact(
            state="online",
            last_seen=NOW - dt.timedelta(minutes=3),
            malware_last_24h=True,
            still_infected=False,
            isolated=False,
        )
    )
    detections_: list[DetectionFact] = field(
        default_factory=lambda: [
            DetectionFact(
                "Trojan.GenericKD.71234567", "quarantined", "closed", NOW - dt.timedelta(minutes=20)
            )
        ]
    )
    all_alerts: list[TicketFact] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)

    def _read(self, name: str, value: Any, source: str) -> Read[Any]:
        self.reads.append(name)
        if name in self.down:
            return Read.failed(source, "timed out after 30s", at=NOW)
        return Read.of(source, value, at=NOW)

    async def resolve_device(self, ticket: triggers.Arrival) -> DeviceMatch | None:
        self.reads.append("resolve")
        return self.device

    async def device_state(self, device: DeviceMatch) -> Read[DeviceState]:
        return self._read("state", self.state, "NinjaOne")

    async def open_alerts(self, device: DeviceMatch) -> Read[list[AlertFact]]:
        return self._read("alerts", self.alerts, "NinjaOne")

    async def volumes(self, device: DeviceMatch) -> Read[list[VolumeFact]]:
        return self._read("volumes", self.volumes_, "NinjaOne")

    async def device_tickets(self, device: DeviceMatch, *, days: int) -> Read[list[TicketFact]]:
        return self._read("tickets", self.tickets, "Autotask")

    async def endpoint_protection(self, device: DeviceMatch) -> Read[ProtectionFact]:
        return self._read("protection", self.protection, "Bitdefender")

    async def detections(self, device: DeviceMatch, *, hours: int) -> Read[list[DetectionFact]]:
        return self._read("detections", self.detections_, "Bitdefender")

    async def alert_tickets(self, *, days: int) -> Read[list[TicketFact]]:
        return self._read("all_alerts", self.all_alerts, "Autotask")


@dataclass
class FakeWriter:
    posted: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False

    async def add_internal_note(self, ticket_id: str | int, *, title: str, body: str) -> Any:
        if self.fail:
            raise RuntimeError("Autotask said no")
        self.posted.append({"ticket_id": str(ticket_id), "title": title, "body": body})
        return type(
            "Posted", (), {"note_id": str(9000 + len(self.posted)), "publish": 1, "note_type": 3}
        )()


async def build(name: str, ticket: triggers.Arrival, tools: FakeTools) -> Any:
    definition = note_workflow(name)
    assert definition.build is not None
    return await definition.build(NoteContext(ticket=ticket, tools=tools, settings={}, now=NOW))


# --- a. device back online -------------------------------------------------------------------


async def test_device_online_note_from_fixtures() -> None:
    draft = await build(
        "device_online", arrival("1", "Device offline for more than 24 hours"), FakeTools()
    )
    assert draft.title == "CON-FS-01 back online"
    assert draft.body.splitlines() == [
        "Device CON-FS-01 is back online: last contact 13 Oct 2026 09:23 UTC "
        "(alert raised 13 Oct 2026 08:40 UTC, down about 43 min).",
        "Last boot 13 Oct 2026 09:21 UTC.",
        "No other open alerts.",
        "Read by Desk at 13 Oct 2026 09:30 UTC.",
    ]
    assert {f["id"] for f in [x.as_dict() for x in draft.facts]} >= {
        "device.online",
        "device.last_contact",
        "device.last_boot",
        "device.open_alerts",
    }
    assert all(f.read_at == NOW for f in draft.facts), "every fact carries when it was read"


async def test_device_still_offline_says_so() -> None:
    tools = FakeTools(
        state=DeviceState(online=False, last_contact=NOW - dt.timedelta(hours=3), last_boot=None)
    )
    draft = await build("device_online", arrival("1", "Server down: CON-FS-01"), tools)
    assert draft.title == "CON-FS-01 still offline"
    assert draft.body.startswith(
        "Device CON-FS-01 is still offline: last contact 13 Oct 2026 06:30 UTC (about 3 h ago)."
    )
    assert "Last boot unknown." in draft.body


async def test_a_read_that_is_not_core_becomes_one_line_and_a_missing_core_fact_no_note() -> None:
    tools = FakeTools(down={"alerts"})
    draft = await build("device_online", arrival("1", "Device offline"), tools)
    assert "Other open alerts: NinjaOne did not answer (timed out after 30s)." in draft.body
    assert len(draft.failures) == 1 and draft.failures[0].source == "NinjaOne"
    skipped = await build(
        "device_online", arrival("1", "Device offline"), FakeTools(down={"state"})
    )
    assert isinstance(skipped, Skip) and "did not answer for the device state" in skipped.reason


async def test_a_low_confidence_device_match_writes_no_note() -> None:
    for name in ("device_online", "repeat_alert", "disk_space", "threat"):
        skipped = await build(
            name, arrival("1", "Device offline / disk space / malware"), FakeTools(device=LOW)
        )
        assert isinstance(skipped, Skip), name
        assert "could not be matched confidently" in skipped.reason
    skipped = await build("device_online", arrival("1", "Device offline"), FakeTools(device=None))
    assert isinstance(skipped, Skip)


# --- b. repeat alert -------------------------------------------------------------------------


async def test_repeat_alert_counts_the_same_kind_and_names_the_open_duplicate() -> None:
    tickets = [
        TicketFact(
            "90", "T90", "Disk free space below 10%", "Complete", False, NOW - dt.timedelta(days=5)
        ),
        TicketFact(
            "91", "T91", "Disk free space below 5%", "In Progress", True, NOW - dt.timedelta(days=1)
        ),
        TicketFact(
            "92",
            "T92",
            "Device offline for 24 hours",
            "Complete",
            False,
            NOW - dt.timedelta(days=2),
        ),
        TicketFact("1", "T1", "Disk free space below 10%", "New", True, NOW),
    ]
    draft = await build(
        "repeat_alert", arrival("1", "Disk free space below 10%"), FakeTools(tickets=tickets)
    )
    assert draft.body.splitlines()[:2] == [
        "Same alert on CON-FS-01 3 times in 7 days: T90 (Complete), T91 (In Progress), "
        "and this ticket.",
        "Possible duplicate of T91 (open).",
    ]
    assert normalised_kind("Disk free space below 10%") == normalised_kind(
        "Disk free space below 5%"
    )
    alone = await build("repeat_alert", arrival("1", "Printer offline"), FakeTools(tickets=tickets))
    assert isinstance(alone, Skip) and "no other ticket of this kind" in alone.reason


async def test_repeat_alert_takes_the_device_name_out_before_folding_digits() -> None:
    """Two machines whose names differ only in digits, CON-LT-043 and
    CON-LT-044. The digits fold would make `Device offline: CON-LT-043` and
    `Device offline: CON-LT-044` one kind, so the name comes out first and a
    ticket naming the other machine is never a repeat of this one — while the
    same machine written as its FQDN is."""
    this = DeviceMatch(
        device_id="d43",
        name="CON-LT-043",
        site_slug="contoso-legal",
        confidence="high",
        method="hostname",
    )
    assert normalised_kind("Device offline: CON-LT-043", this.name) == normalised_kind(
        "Device offline: con-lt-043.tailwind.local", this.name
    )
    assert normalised_kind("Device offline: CON-LT-043", "CON-LT-043") == "device offline"
    # Without the name the two fold to one kind; that is the trap.
    assert normalised_kind("Device offline: CON-LT-043") == normalised_kind(
        "Device offline: CON-LT-044"
    )
    tickets = [
        # The neighbouring machine, returned by a text search that matched its
        # description ("see also CON-LT-043"): not a repeat.
        TicketFact(
            "80", "T80", "Device offline: CON-LT-044", "New", True, NOW - dt.timedelta(days=1)
        ),
        # The same machine by its qualified name, three days ago: a repeat.
        TicketFact(
            "81",
            "T81",
            "Device offline: con-lt-043.tailwind.local",
            "Complete",
            False,
            NOW - dt.timedelta(days=3),
        ),
        TicketFact("1", "T1", "Device offline: CON-LT-043", "New", True, NOW),
    ]
    draft = await build(
        "repeat_alert",
        arrival("1", "Device offline: CON-LT-043"),
        FakeTools(device=this, tickets=tickets),
    )
    assert isinstance(draft, Draft)
    assert draft.body.splitlines()[0] == (
        "Same alert on CON-LT-043 2 times in 7 days: T81 (Complete), and this ticket."
    )
    assert "T80" not in draft.body and "Possible duplicate" not in draft.body
    # Only the neighbour's ticket in the window: no note at all.
    only_neighbour = await build(
        "repeat_alert",
        arrival("1", "Device offline: CON-LT-043"),
        FakeTools(device=this, tickets=[tickets[0], tickets[2]]),
    )
    assert isinstance(only_neighbour, Skip)


# --- the exact text, pinned ---------------------------------------------------------------------

#: What each workflow posts, word for word, from the fixtures above. The page
#: shows a template of this wording (`NoteWorkflow.template`); that string is
#: presentation and may be reworded, this may not: a change here is a change
#: to what lands on a client's ticket and must be meant.
EXACT_NOTES: dict[str, tuple[str, str]] = {
    "device_online": (
        "CON-FS-01 back online",
        "Device CON-FS-01 is back online: last contact 13 Oct 2026 09:23 UTC (alert raised "
        "13 Oct 2026 08:40 UTC, down about 43 min).\n"
        "Last boot 13 Oct 2026 09:21 UTC.\n"
        "No other open alerts.\n"
        "Read by Desk at 13 Oct 2026 09:30 UTC.",
    ),
    "repeat_alert": (
        "Repeat alert on CON-FS-01",
        "Same alert on CON-FS-01 3 times in 7 days: T90 (Complete), T91 (In Progress), and "
        "this ticket.\n"
        "Possible duplicate of T91 (open).\n"
        "Read by Desk at 13 Oct 2026 09:30 UTC.",
    ),
    "disk_space": (
        "Disk space on CON-FS-01",
        "C: 4.2 GB free of 237 GB (1.8%).\n"
        "D: 120 GB free of 500 GB (24%).\n"
        "Device is online (last contact 13 Oct 2026 09:23 UTC).\n"
        "Read by Desk at 13 Oct 2026 09:30 UTC.",
    ),
    "threat": (
        "Threat on CON-FS-01",
        "Bitdefender: CON-FS-01 is online (last seen 13 Oct 2026 09:27 UTC); malware in the "
        "last 24 h: yes; still infected: no; isolated: no.\n"
        "What Bitdefender found and did in the last 24 h: Trojan.GenericKD.71234567 — "
        "quarantined (13 Oct 2026 09:10 UTC).\n"
        "Read by Desk at 13 Oct 2026 09:30 UTC.",
    ),
    "backup_notice": (
        "Backup failed",
        "Backup failed: job Daily-CON-FS-01.\nError: Disk full.\n"
        "Parsed from the veeam notice by Desk at 13 Oct 2026 09:30 UTC.",
    ),
}


def exact_cases() -> dict[str, tuple[triggers.Arrival, FakeTools]]:
    repeat = [
        TicketFact(
            "90", "T90", "Disk free space below 10%", "Complete", False, NOW - dt.timedelta(days=5)
        ),
        TicketFact(
            "91", "T91", "Disk free space below 5%", "In Progress", True, NOW - dt.timedelta(days=1)
        ),
        TicketFact("1", "T1", "Disk free space below 10%", "New", True, NOW),
    ]
    veeam = (
        "Veeam Backup & Replication\nBackup job: Daily-CON-FS-01\nStatus: Failed\nError: Disk full"
    )
    return {
        "device_online": (arrival("1", "Device offline for more than 24 hours"), FakeTools()),
        "repeat_alert": (arrival("1", "Disk free space below 10%"), FakeTools(tickets=repeat)),
        "disk_space": (arrival("1", "Disk space low on C: (below 10%)"), FakeTools()),
        "threat": (arrival("1", "Malware detected on CON-FS-01"), FakeTools()),
        "backup_notice": (arrival("1", "[Failed] Daily-CON-FS-01", description=veeam), FakeTools()),
    }


async def test_every_note_workflow_builds_exactly_the_pinned_text() -> None:
    """Every note workflow, from fixtures, against its text word for word.
    Covers every workflow that writes a note, so a new one must be pinned."""
    writing = {w.name for w in note_workflows() if w.kind == "note"}
    assert writing == set(EXACT_NOTES) == set(exact_cases())
    for name, (ticket, tools) in exact_cases().items():
        draft = await build(name, ticket, tools)
        assert isinstance(draft, Draft), name
        assert (draft.title, draft.body) == EXACT_NOTES[name], name


# --- c. disk space ------------------------------------------------------------------------------


async def test_disk_space_lists_volumes_online_state_and_the_30_day_count_above_two() -> None:
    history = [
        TicketFact(str(n), f"T{n}", "Disk space low on C: (below 10%)", "Complete", False, NOW)
        for n in range(4)
    ]
    draft = await build(
        "disk_space", arrival("1", "Disk space low on C: (below 10%)"), FakeTools(tickets=history)
    )
    assert draft.body.splitlines() == [
        "C: 4.2 GB free of 237 GB (1.8%).",
        "D: 120 GB free of 500 GB (24%).",
        "Device is online (last contact 13 Oct 2026 09:23 UTC).",
        "4 disk alerts on this device in 30 days.",
        "Read by Desk at 13 Oct 2026 09:30 UTC.",
    ]
    quiet = await build(
        "disk_space", arrival("1", "Disk space low"), FakeTools(tickets=history[:2])
    )
    assert "disk alerts on this device" not in quiet.body, "two or fewer is not worth a line"
    skipped = await build("disk_space", arrival("1", "Disk space low"), FakeTools(down={"volumes"}))
    assert isinstance(skipped, Skip)


# --- d. threat --------------------------------------------------------------------------------


async def test_threat_note_says_what_bitdefender_found_and_did() -> None:
    draft = await build("threat", arrival("1", "Malware detected on CON-FS-01"), FakeTools())
    lines = draft.body.splitlines()
    assert lines[0] == (
        "Bitdefender: CON-FS-01 is online (last seen 13 Oct 2026 09:27 UTC); malware in the last "
        "24 h: yes; still infected: no; isolated: no."
    )
    assert lines[1] == (
        "What Bitdefender found and did in the last 24 h: Trojan.GenericKD.71234567 — quarantined "
        "(13 Oct 2026 09:10 UTC)."
    )
    assert MDR_LINE not in draft.body
    skipped = await build(
        "threat", arrival("1", "Malware detected"), FakeTools(down={"protection"})
    )
    assert isinstance(skipped, Skip), "Bitdefender is the core fact for a Bitdefender alert"


async def test_a_blackpoint_alert_always_carries_the_mdr_line() -> None:
    bp = arrival("1", "Blackpoint MDR: suspicious PowerShell on CON-FS-01")
    draft = await build("threat", bp, FakeTools())
    assert draft.body.splitlines()[-2] == MDR_LINE
    # Even when Bitdefender did not answer, and even when the machine is not matched.
    draft = await build("threat", bp, FakeTools(down={"protection", "detections"}))
    assert "Bitdefender endpoint: Bitdefender did not answer" in draft.body
    assert draft.body.splitlines()[-2] == MDR_LINE
    draft = await build("threat", bp, FakeTools(device=LOW))
    assert draft.body.splitlines()[-1] == MDR_LINE


# --- e. backup notices ----------------------------------------------------------------------------


VEEAM_FAIL = """Veeam Backup & Replication
Backup job: Daily-FS01
Status: Failed
Error: Failed to connect to repository REPO01 (The network path was not found)
"""
NABLE_OK = """N-able Backup Manager
Device: CON-FS-01
Backup status: Completed
"""


def test_backup_formats_are_parsed_by_fixed_rules_and_unknown_formats_are_not() -> None:
    veeam = parse_backup_notice("[Failed] Daily-FS01", VEEAM_FAIL)
    assert veeam is not None and (veeam.sender, veeam.outcome, veeam.job) == (
        "veeam",
        "failed",
        "Daily-FS01",
    )
    assert veeam.error == "Failed to connect to repository REPO01 (The network path was not found)"
    nable = parse_backup_notice("Backup completed on CON-FS-01", NABLE_OK)
    assert nable is not None and (nable.sender, nable.outcome, nable.job) == (
        "n-able",
        "success",
        "CON-FS-01",
    )
    assert (
        parse_backup_notice("Backup job failed", "Acme Backup Pro: job 7 failed with code 3")
        is None
    )


async def test_backup_notes_say_no_action_or_the_job_and_error() -> None:
    ok = await build(
        "backup_notice",
        arrival("1", "Backup completed on CON-FS-01", description=NABLE_OK),
        FakeTools(),
    )
    assert ok.body == "Backup success notice: no action needed."
    failed = await build(
        "backup_notice", arrival("2", "[Failed] Daily-FS01", description=VEEAM_FAIL), FakeTools()
    )
    assert failed.body.splitlines()[:2] == [
        "Backup failed: job Daily-FS01.",
        "Error: Failed to connect to repository REPO01 (The network path was not found).",
    ]
    unknown = await build(
        "backup_notice",
        arrival("3", "Backup job failed", description="Acme Backup Pro: job 7 failed"),
        FakeTools(),
    )
    assert isinstance(unknown, Skip) and "not in a format we parse" in unknown.reason
    assert FakeTools().reads == [], "the backup workflow reads no tool"


# --- the service: one note per ticket, modes, posting, the kill switch ---------------------------


async def switches_on(world: World, name: str, trigger_key: str) -> None:
    actor = world.actor(world.starter)

    async def go(session: Any) -> None:
        await controls.set_switch(
            session,
            actor.context(),
            client_id=world.client.id,
            workflow_name=name,
            enabled=True,
            who=actor.who(),
            now=world.now(),
        )
        await triggers.set_trigger_switch(
            session,
            actor.context(),
            client_id=world.client.id,
            workflow_name=name,
            trigger_key=trigger_key,
            enabled=True,
            who=actor.who(),
            now=world.now(),
        )

    await world.with_service(go)


async def evaluate(
    world: World, tools: FakeTools, writer: FakeWriter | None, *arrivals: triggers.Arrival
) -> list[triggers.Evaluated]:
    async def writer_for(client_id: uuid.UUID) -> Any:
        assert writer is not None
        return writer

    async def go(session: Any) -> list[triggers.Evaluated]:
        return await notes.evaluate(
            session,
            world.client.id,
            list(arrivals),
            tools=tools,
            writer_for=writer_for if writer is not None else None,
            now=world.now(),
        )

    return await world.with_service(go)


async def all_notes(world: World) -> list[AutomationNote]:
    async with world.maker() as session:
        return list(
            (await session.execute(select(AutomationNote).order_by(AutomationNote.built_at)))
            .scalars()
            .all()
        )


async def test_a_matched_alert_builds_one_preview_note_and_nothing_leaves_the_app(
    world: World,
) -> None:
    await switches_on(world, "device_online", "down_alert")
    writer = FakeWriter()
    found = await evaluate(
        world, FakeTools(), writer, arrival("1", "Device offline for more than 24 hours")
    )
    mine = [f for f in found if f.workflow == "device_online"]
    assert mine[0].outcome == "noted" and "built a Desk note (preview)" in mine[0].reason
    rows = await all_notes(world)
    assert len(rows) == 1
    note = rows[0]
    assert (
        note.state == "built"
        and note.mode == "preview"
        and note.title == "Desk: CON-FS-01 back online"
    )
    assert note.device_name == "CON-FS-01" and note.ticket_number == "T1"
    assert note.facts[0]["read_at"] == NOW.isoformat()
    assert writer.posted == [], "preview never posts"
    built = [r for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_NOTE)]
    assert [r.payload["action"] for r in built] == ["built"]
    # Seen again on the next poll: one note per workflow per ticket.
    again = await evaluate(
        world, FakeTools(), writer, arrival("1", "Device offline for more than 24 hours")
    )
    mine = [f for f in again if f.workflow == "device_online"]
    assert mine[0].outcome == "skipped" and "a note already exists" in mine[0].reason
    assert len(await all_notes(world)) == 1
    # The alert ticket is remembered for the measurement, pointing at the note.
    async with world.maker() as session:
        remembered = (await session.execute(select(AutomationAlertTicket))).scalars().all()
    assert len(remembered) == 1 and remembered[0].note_id == note.id


async def test_both_switches_gate_a_note_and_the_log_says_which(world: World) -> None:
    found = await evaluate(world, FakeTools(), None, arrival("1", "Device offline"))
    mine = [f for f in found if f.workflow == "device_online"]
    assert (
        mine[0].matched
        and mine[0].outcome == "skipped"
        and "workflow is switched off" in mine[0].reason
    )
    assert await all_notes(world) == []


async def test_auto_post_needs_a_recorded_approver_and_then_posts_at_once(world: World) -> None:
    await switches_on(world, "device_online", "down_alert")
    actor = world.actor(world.starter)

    async def no_approver(session: Any) -> None:
        await notes.set_mode(
            session, actor, workflow_name="device_online", mode="auto", now=world.now()
        )

    with pytest.raises(service.Refused, match="approver recorded"):
        await world.with_service(no_approver)

    async def wrong_approver(session: Any) -> None:
        await notes.set_mode(
            session,
            actor,
            workflow_name="device_online",
            mode="auto",
            authoriser_name="Nobody Here",
            authoriser_method="email",
            now=world.now(),
        )

    with pytest.raises(service.Refused, match="not on this client's approver list"):
        await world.with_service(wrong_approver)

    async def right(session: Any) -> None:
        row = await notes.set_mode(
            session,
            actor,
            workflow_name="device_online",
            mode="auto",
            authoriser_name="dana whitfield",
            authoriser_method="Teams",
            now=world.now(),
        )
        assert row.authoriser_name == "Dana Whitfield" and row.authoriser_method == "teams"

    await world.with_service(right)
    writer = FakeWriter()
    found = await evaluate(world, FakeTools(), writer, arrival("1", "Device offline"))
    mine = [f for f in found if f.workflow == "device_online"]
    assert mine[0].outcome == "noted" and mine[0].reason.endswith(
        "built a Desk note (auto); posted"
    )
    note = (await all_notes(world))[0]
    assert note.state == "posted" and note.posted_by == "auto" and note.autotask_note_id == "9001"
    assert writer.posted[0]["title"].startswith("Desk: ")
    actions = [
        r.payload["action"]
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_NOTE)
    ]
    assert actions == ["built", "posted"]
    modes = [
        r
        for r in await audit_rows(world.maker, event_type=AuditEventType.AUTOMATION_CONTROL)
        if r.payload["control"] == "note_mode"
    ]
    assert modes[-1].payload["authoriser"]["name"] == "Dana Whitfield"

    # Back to preview: immediate, nobody recorded.
    async def back(session: Any) -> None:
        row = await notes.set_mode(
            session, actor, workflow_name="device_online", mode="preview", now=world.now()
        )
        assert row.authoriser_name is None

    await world.with_service(back)


async def test_the_kill_switch_stops_all_posting(world: World) -> None:
    await switches_on(world, "device_online", "down_alert")
    actor = world.actor(world.starter)

    async def auto(session: Any) -> None:
        await notes.set_mode(
            session,
            actor,
            workflow_name="device_online",
            mode="auto",
            authoriser_name="Dana Whitfield",
            authoriser_method="phone",
            now=world.now(),
        )
        await controls.set_kill_switch(
            session,
            actor.context(),
            client_id=world.client.id,
            halted=True,
            reason="freeze",
            who=actor.who(),
            now=world.now(),
        )

    await world.with_service(auto)
    writer = FakeWriter()
    found = await evaluate(world, FakeTools(), writer, arrival("1", "Device offline"))
    mine = [f for f in found if f.workflow == "device_online"]
    assert mine[0].outcome == "noted" and "not posted: automation is halted" in mine[0].reason
    note = (await all_notes(world))[0]
    assert note.state == "built" and writer.posted == []

    # An engineer's Post is refused too, until it is cleared.
    async def by_hand(session: Any) -> None:
        got = await notes.get_note(session, world.client.id, note.id)
        await notes.post(
            session,
            world.client,
            got,
            writer=writer,
            by=world.actor(world.approver),
            now=world.now(),
        )

    with pytest.raises(service.Refused, match="halted"):
        await world.with_service(by_hand)
    assert writer.posted == []


async def test_an_engineer_posts_a_preview_note_and_a_failed_post_is_kept(world: World) -> None:
    await switches_on(world, "disk_space", "disk_alert")
    await evaluate(world, FakeTools(), None, arrival("5", "Disk space low on C: (below 10%)"))
    note = (await all_notes(world))[0]
    bad = FakeWriter(fail=True)

    async def fails(session: Any) -> None:
        got = await notes.get_note(session, world.client.id, note.id)
        await notes.post(
            session, world.client, got, writer=bad, by=world.actor(world.approver), now=world.now()
        )

    await world.with_service(fails)
    kept = (await all_notes(world))[0]
    assert kept.state == "post_failed" and "Autotask said no" in (kept.post_error or "")
    good = FakeWriter()

    async def works(session: Any) -> None:
        got = await notes.get_note(session, world.client.id, note.id)
        await notes.post(
            session, world.client, got, writer=good, by=world.actor(world.approver), now=world.now()
        )

    await world.with_service(works)
    posted = (await all_notes(world))[0]
    assert posted.state == "posted" and posted.posted_by == world.approver.email
    # What the writer was handed is the pinned text, under the Desk: title.
    title, body = EXACT_NOTES["disk_space"]
    assert good.posted[0] == {"ticket_id": "5", "title": notes.TITLE_PREFIX + title, "body": body}

    async def twice(session: Any) -> None:
        got = await notes.get_note(session, world.client.id, note.id)
        await notes.post(
            session, world.client, got, writer=good, by=world.actor(world.approver), now=world.now()
        )

    with pytest.raises(service.Refused, match="posted and cannot be posted"):
        await world.with_service(twice)


async def test_not_useful_and_the_measurement_report(world: World) -> None:
    await switches_on(world, "device_online", "down_alert")
    await evaluate(
        world, FakeTools(), None, arrival("1", "Device offline"), arrival("2", "Server down")
    )
    first, _second = await all_notes(world)

    async def dismiss(session: Any) -> None:
        got = await notes.get_note(session, world.client.id, first.id)
        await notes.mark_not_useful(
            session, world.client, got, by=world.actor(world.approver), now=world.now()
        )

    await world.with_service(dismiss)

    async def closures(ids: list[str]) -> dict[str, dt.datetime | None]:
        return {"1": NOW + dt.timedelta(hours=2), "2": None}

    async def measure(session: Any) -> int:
        return await notes.measure_closures(session, world.client.id, closures, now=world.now())

    assert await world.with_service(measure) == 1

    async def read(session: Any) -> dict[str, Any]:
        return await notes.report(session, world.client.id, now=world.now())

    found = await world.with_service(read)
    row = next(r for r in found["by_workflow"] if r["workflow"] == "device_online")
    assert (row["built"], row["not_useful"], row["share_not_useful"]) == (2, 1, 0.5)
    assert row["median_hours_to_close_with_note"] == 2.8, "50 min before, 2 h after, rounded"
    assert row["closed_with_note"] == 1 and row["alert_tickets_with_note"] == 2
    assert found["weeks"][0]["workflows"]["device_online"]["built"] == 2
    assert "median_hours_to_close" in found["definitions"]
    note = next(n for n in await all_notes(world) if n.id == first.id)
    assert note.state == "not_useful" and note.ticket_closed_at == NOW + dt.timedelta(hours=2)


async def test_the_chronic_report_lists_devices_with_three_or_more_alerts(world: World) -> None:
    await switches_on(world, "chronic_devices", "monthly")
    tools = FakeTools(
        all_alerts=[
            TicketFact(
                str(n),
                f"T{n}",
                "Disk free space below 10%",
                "Complete",
                False,
                NOW - dt.timedelta(days=n),
                description="on CON-FS-01",
            )
            for n in range(1, 4)
        ]
        + [TicketFact("9", "T9", "Device offline", "New", True, NOW, description="on CON-FS-01")]
    )

    async def go(session: Any) -> list[Any]:
        return await notes.build_due_reports(session, world.client.id, tools=tools, now=world.now())

    built = await world.with_service(go)
    assert len(built) == 1
    body = built[0].body
    assert body["devices"][0]["device"] == "CON-FS-01" and body["devices"][0]["total"] == 4
    assert body["devices"][0]["kinds"][0]["count"] == 3
    assert body["tickets"] == 4 and body["unmatched"] == 0
    # Not due again until the schedule says so.
    assert await world.with_service(go) == []

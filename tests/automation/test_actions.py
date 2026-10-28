"""The action library: every block tested on its own, against fixtures.

Three things are proven here that no workflow test can: that the registry
refuses an action that breaks the contract (a write with no verify, a write
scope off the allow-list, a connector it does not know); that each action
does what it says from fixtures alone, with no workflow around it; and that
no action reaches a connector other than the ones it declares, measured by
running every one against a recording fake and mapping the calls it made.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.automation import actions as library
from app.automation.actions.base import (
    TOOL_CONNECTORS,
    WRITE_SCOPES,
    Action,
    ActionContext,
    Input,
    InvalidAction,
    fits,
)
from app.automation.definitions import Check
from app.automation.notes.definitions import (
    DeviceState,
    Draft,
    Read,
    Skip,
    TicketFact,
)
from tests.api.test_automation_notes import DEVICE, LOW, NOW, FakeTools, FakeWriter, arrival

CLIENT = uuid.UUID(int=7)


def ctx(inputs: dict[str, Any], **extra: Any) -> ActionContext:
    return ActionContext(inputs=inputs, client_id=CLIENT, step_id="step", now=NOW, **extra)


async def noop(_: ActionContext) -> dict[str, Any]:
    return {}


async def passes(_: ActionContext) -> Check:
    return Check.passed()


# --- the registry's rules --------------------------------------------------------------


def test_every_registered_action_keeps_the_contract() -> None:
    found = library.actions()
    assert len(found) >= 19
    for action in found:
        assert action.version >= 1 and action.title.strip()
        assert action.mode in ("read", "write")
        for connector in action.connectors:
            assert connector in library.CONNECTORS
        if action.writes:
            assert action.verify is not None, f"{action.name} writes and has no verify"
            assert action.write_scope in WRITE_SCOPES, f"{action.name}: {action.write_scope}"
        else:
            assert action.write_scope is None
        for key in action.idempotency:
            assert key in action.inputs


def test_the_only_write_to_a_client_system_is_the_internal_note() -> None:
    """The allow-list is two lines: the note, and the engine's own table."""
    assert WRITE_SCOPES == {"add internal ticket note", "practice record on the engine's own table"}
    client_writes = [
        a for a in library.actions() if a.writes and a.write_scope == "add internal ticket note"
    ]
    assert [a.name for a in client_writes] == ["autotask.add_note"]
    assert all(
        a.connectors == ("practice",)
        for a in library.actions()
        if a.writes and a.name != "autotask.add_note"
    )


def test_a_write_action_without_a_verify_is_refused() -> None:
    with pytest.raises(InvalidAction, match="needs a verify"):
        Action(
            name="autotask.add_tag",
            version=1,
            title="Tag",
            connectors=("autotask",),
            mode="write",
            write_scope="add internal ticket note",
            inputs={},
            outputs={},
            run=noop,
            checks="x",
            does="y",
            confirms="z",
        )


def test_a_write_scope_off_the_allow_list_is_refused() -> None:
    with pytest.raises(InvalidAction, match="not on the allow-list"):
        Action(
            name="autotask.close_ticket",
            version=1,
            title="Close",
            connectors=("autotask",),
            mode="write",
            write_scope="close ticket",
            inputs={},
            outputs={},
            run=noop,
            verify=passes,
            checks="x",
            does="y",
            confirms="z",
        )


def test_an_unknown_connector_type_or_mode_is_refused() -> None:
    with pytest.raises(InvalidAction, match="connector"):
        Action(
            name="x.y",
            version=1,
            title="t",
            connectors=("github",),
            mode="read",
            inputs={},
            outputs={},
            run=noop,
        )
    with pytest.raises(InvalidAction, match="type"):
        Input("regex", "no")
    with pytest.raises(InvalidAction, match="mode"):
        Action(
            name="x.y",
            version=1,
            title="t",
            connectors=("none",),
            mode="execute",
            inputs={},
            outputs={},
            run=noop,
        )
    with pytest.raises(InvalidAction, match="registered twice"):
        library.register_action(library.action("devices.resolve"))


def test_types_fit_what_they_say() -> None:
    assert fits("a", "string") and not fits(1, "string")
    assert fits(1, "integer") and not fits(True, "integer") and not fits(1.5, "integer")
    assert fits(1.5, "number") and fits(2, "number")
    assert fits(["a"], "list[string]") and not fits(["a", 1], "list[string]")
    assert fits(DEVICE, "device") and fits(arrival("1", "x"), "ticket")
    assert fits(Read.of("t", 1, at=NOW), "read") and fits(Skip("no"), "note")
    assert not fits(None, "string")


# --- the read actions, from fixtures ----------------------------------------------------


async def test_devices_resolve_says_how_sure_it_is() -> None:
    sure = await library.action("devices.resolve").run(
        ctx({"ticket": arrival("1", "x")}, tools=FakeTools())
    )
    assert sure == {"device": DEVICE, "confident": True, "name": "CON-FS-01"}
    unsure = await library.action("devices.resolve").run(
        ctx({"ticket": arrival("1", "x")}, tools=FakeTools(device=LOW))
    )
    assert unsure["confident"] is False and unsure["device"] is LOW
    none = await library.action("devices.resolve").run(
        ctx({"ticket": arrival("1", "x")}, tools=FakeTools(device=None))
    )
    assert none == {"device": None, "confident": False, "name": None}


@pytest.mark.parametrize(
    ("name", "inputs", "tool", "down"),
    [
        ("ninjaone.read_device", {}, "state", "state"),
        ("ninjaone.read_open_alerts", {}, "alerts", "alerts"),
        ("ninjaone.read_volumes", {}, "volumes", "volumes"),
        ("autotask.find_related_tickets", {"days": 7}, "tickets", "tickets"),
        ("bitdefender.read_protection", {}, "protection", "protection"),
        ("bitdefender.read_threats", {"hours": 24}, "detections", "detections"),
    ],
)
async def test_each_read_returns_the_read_and_whether_it_answered(
    name: str, inputs: dict[str, Any], tool: str, down: str
) -> None:
    action = library.action(name)
    tools = FakeTools()
    got = await action.run(ctx({"device": DEVICE, **inputs}, tools=tools))
    assert got["ok"] is True and isinstance(got["read"], Read) and got["read"].read_at == NOW
    assert tools.reads == [tool]
    failed = await action.run(ctx({"device": DEVICE, **inputs}, tools=FakeTools(down={down})))
    assert failed["ok"] is False and failed["read"].error == "timed out after 30s"
    with pytest.raises(RuntimeError, match="no device"):
        await action.run(ctx({"device": None, **inputs}, tools=FakeTools()))
    with pytest.raises(RuntimeError, match="none was given"):
        await action.run(ctx({"device": DEVICE, **inputs}))


# --- the note actions, from fixtures -----------------------------------------------------


def state_read(**changes: Any) -> Read[DeviceState]:
    tools = FakeTools(**changes)
    return Read.of("NinjaOne", tools.state, at=NOW)


async def test_notes_device_online_from_reads() -> None:
    action = library.action("notes.device_online")
    alerts: Read[list[Any]] = Read.of("NinjaOne", [], at=NOW)
    got = await action.run(
        ctx(
            {
                "ticket": arrival("1", "Device offline"),
                "device": DEVICE,
                "state": state_read(),
                "alerts": alerts,
            }
        )
    )
    note = got["note"]
    assert isinstance(note, Draft) and note.title == "CON-FS-01 back online"
    assert note.body.splitlines()[2] == "No other open alerts."
    # Not confident: no note. The core read missing: no note. A failed other
    # read: one line.
    unsure = await action.run(ctx({"ticket": arrival("1", "x"), "device": LOW}))
    assert isinstance(unsure["note"], Skip) and "confidently" in unsure["note"].reason
    missing = await action.run(ctx({"ticket": arrival("1", "x"), "device": DEVICE}))
    assert isinstance(missing["note"], Skip) and "did not answer" in missing["note"].reason
    failed_alerts: Read[list[Any]] = Read.failed("NinjaOne", "timed out", at=NOW)
    lined = await action.run(
        ctx(
            {
                "ticket": arrival("1", "x"),
                "device": DEVICE,
                "state": state_read(),
                "alerts": failed_alerts,
            }
        )
    )
    assert "Other open alerts: NinjaOne did not answer (timed out)." in lined["note"].body


async def test_notes_repeat_alert_from_reads() -> None:
    action = library.action("notes.repeat_alert")
    tickets: Read[list[TicketFact]] = Read.of(
        "Autotask",
        [
            TicketFact(
                "9", "T9", "Disk free space below 5%", "New", True, NOW - dt.timedelta(days=1)
            ),
            TicketFact("1", "T1", "Disk free space below 10%", "New", True, NOW),
        ],
        at=NOW,
    )
    got = await action.run(
        ctx(
            {
                "ticket": arrival("1", "Disk free space below 10%"),
                "device": DEVICE,
                "tickets": tickets,
            }
        )
    )
    assert got["note"].body.startswith(
        "Same alert on CON-FS-01 2 times in 7 days: T9 (New), and this ticket."
    )
    alone = await action.run(
        ctx({"ticket": arrival("1", "Printer offline"), "device": DEVICE, "tickets": tickets})
    )
    assert isinstance(alone["note"], Skip)


async def test_notes_disk_space_from_reads() -> None:
    action = library.action("notes.disk_space")
    tools = FakeTools()
    volumes: Read[Any] = Read.of("NinjaOne", tools.volumes_, at=NOW)
    got = await action.run(ctx({"ticket": arrival("1", "x"), "device": DEVICE, "volumes": volumes}))
    body = got["note"].body.splitlines()
    assert body[0] == "C: 4.2 GB free of 237 GB (1.8%)."
    # The state and the history were not read: one line each says so.
    assert any(line.startswith("Online state: NinjaOne did not answer") for line in body)
    assert any(line.startswith("Disk alerts in 30 days: Autotask did not answer") for line in body)


async def test_notes_threat_gate_and_threat_from_reads() -> None:
    gate = library.action("notes.threat_gate")
    protection: Read[Any] = Read.failed("Bitdefender", "timed out", at=NOW)
    plain = await gate.run(
        ctx({"ticket": arrival("1", "Malware detected"), "protection": protection})
    )
    assert plain == {"mdr": False, "read_detections": False}
    mdr = await gate.run(
        ctx({"ticket": arrival("1", "Blackpoint MDR alert"), "protection": protection})
    )
    assert mdr == {"mdr": True, "read_detections": True}
    threat = library.action("notes.threat")
    skipped = await threat.run(
        ctx(
            {"ticket": arrival("1", "Malware detected"), "device": DEVICE, "protection": protection}
        )
    )
    assert isinstance(skipped["note"], Skip)
    noted = await threat.run(
        ctx(
            {
                "ticket": arrival("1", "Blackpoint MDR alert"),
                "device": DEVICE,
                "protection": protection,
            }
        )
    )
    assert noted["note"].body.splitlines()[-2] == (
        "MDR alert: review required, do not close without investigation."
    )


async def test_notes_backup_notice_parses_the_ticket_alone() -> None:
    action = library.action("notes.backup_notice")
    veeam = "Veeam Backup & Replication\nBackup job: Daily\nStatus: Failed\nError: Disk full"
    got = await action.run(ctx({"ticket": arrival("1", "[Failed] Daily", description=veeam)}))
    assert got["note"].title == "Backup failed"
    unknown = await action.run(ctx({"ticket": arrival("1", "Backup thing", description="??")}))
    assert isinstance(unknown["note"], Skip)


# --- the report action and the write action -----------------------------------------------


async def test_reports_chronic_devices_counts_from_the_alert_tickets() -> None:
    action = library.action("reports.chronic_devices")
    tickets = [
        TicketFact(str(n), f"T{n}", "Device offline", "Complete", False, NOW - dt.timedelta(days=n))
        for n in range(1, 5)
    ]
    tools = FakeTools(all_alerts=tickets)
    got = await action.run(ctx({}, tools=tools, period=(NOW - dt.timedelta(days=30), NOW)))
    report = got["report"]
    assert report["tickets"] == 4 and report["devices"][0]["device"] == "CON-FS-01"
    assert report["devices"][0]["total"] == 4
    with pytest.raises(RuntimeError, match="period"):
        await action.run(ctx({}, tools=tools))


async def test_autotask_add_note_posts_under_the_desk_title_and_verifies_the_id() -> None:
    action = library.action("autotask.add_note")
    writer = FakeWriter()
    draft = Draft(title="Disk space on X", body="C: full.")
    ok = await action.precondition(ctx({"ticket_id": "5", "note": draft}))
    assert ok.ok
    skip = await action.precondition(ctx({"ticket_id": "5", "note": Skip("no")}))
    assert not skip.ok and skip.facts.get("skipped")
    plan = await action.plan(ctx({"ticket_id": "5", "note": draft}))
    assert "Disk space on X" in plan.summary
    outputs = await action.run(ctx({"ticket_id": "5", "note": draft}, writer=writer))
    assert writer.posted == [
        {"ticket_id": "5", "title": "Desk: Disk space on X", "body": "C: full."}
    ]
    verified = await action.verify(ctx({"ticket_id": "5", "note": draft}, outputs=outputs))
    assert verified.ok and verified.facts["note_id"] == "9001"
    unverified = await action.verify(ctx({"ticket_id": "5", "note": draft}, outputs={}))
    assert not unverified.ok
    assert not action.reversible
    with pytest.raises(RuntimeError, match="no writer"):
        await action.run(ctx({"ticket_id": "5", "note": draft}))


# --- isolation: an action reaches only the connectors it declares -------------------------


@dataclass
class RecordingTools(FakeTools):
    """A fake that records which `Tools` method each action called."""

    called: list[str] = field(default_factory=list)

    def __getattribute__(self, name: str) -> Any:
        if name in TOOL_CONNECTORS:
            object.__getattribute__(self, "called").append(name)
        return object.__getattribute__(self, name)


#: The inputs each action is run with in the isolation test. The practice
#: actions act on a database session and are run in the engine tests.
ISOLATION_INPUTS: dict[str, dict[str, Any]] = {
    "devices.resolve": {"ticket": arrival("1", "x")},
    "ninjaone.read_device": {"device": DEVICE},
    "ninjaone.read_open_alerts": {"device": DEVICE},
    "ninjaone.read_volumes": {"device": DEVICE},
    "autotask.find_related_tickets": {"device": DEVICE, "days": 7},
    "bitdefender.read_protection": {"device": DEVICE},
    "bitdefender.read_threats": {"device": DEVICE, "hours": 24},
    "notes.device_online": {"ticket": arrival("1", "x"), "device": DEVICE},
    "notes.repeat_alert": {"ticket": arrival("1", "x"), "device": DEVICE},
    "notes.disk_space": {"ticket": arrival("1", "x"), "device": DEVICE},
    "notes.threat_gate": {"ticket": arrival("1", "x")},
    "notes.threat": {"ticket": arrival("1", "x"), "device": DEVICE},
    "notes.backup_notice": {"ticket": arrival("1", "x")},
    "reports.chronic_devices": {},
    "autotask.add_note": {"ticket_id": "1", "note": Draft(title="t", body="b")},
}


async def test_no_action_reaches_a_connector_it_does_not_declare() -> None:
    covered = set()
    for action in library.actions():
        if action.connectors == ("practice",):
            continue
        covered.add(action.name)
        assert action.name in ISOLATION_INPUTS, f"{action.name} has no isolation fixture"
        tools = RecordingTools(all_alerts=[TicketFact("1", "T1", "x", "New", True, NOW)])
        await action.run(
            ctx(
                ISOLATION_INPUTS[action.name],
                tools=tools,
                writer=FakeWriter(),
                period=(NOW - dt.timedelta(days=30), NOW),
            )
        )
        reached = {TOOL_CONNECTORS[name] for name in tools.called}
        if action.name == "autotask.add_note":
            reached.add("autotask")  # the writer is Autotask's
        assert reached <= set(action.connectors), (
            f"{action.name} declares {action.connectors} and reached {sorted(reached)}"
        )
        if action.connectors == ("none",):
            assert not tools.called, f"{action.name} is pure and called {tools.called}"
    assert covered == set(ISOLATION_INPUTS)


def test_every_tools_method_is_mapped_to_a_connector() -> None:
    from app.automation.notes.definitions import Tools

    methods = {name for name in vars(Tools) if not name.startswith("_")}
    assert methods == set(TOOL_CONNECTORS)
    assert set(TOOL_CONNECTORS.values()) <= set(library.CONNECTORS)

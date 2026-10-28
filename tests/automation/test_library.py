"""Workflows as data: the loader refuses what it should, in one line.

Every refusal the definition rules name is produced here from a file written
for the purpose, and the message names the file, the step and the problem.
The shipped definitions are validated the way `scripts/automation.py
validate` validates them, so the suite fails before a bad file does.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
import subprocess
import sys
from typing import Any

import pytest
import yaml

import app.automation  # noqa: F401  loads the definitions
from app.automation import library
from app.automation.library import (
    DEFINITIONS_DIR,
    DefinitionError,
    Scope,
    check,
    condition_holds,
    parse_reference,
    validate_all,
)
from app.automation.notes.definitions import NoteContext, Skip
from tests.api.test_automation_notes import LOW, NOW, FakeTools, arrival

ROOT = pathlib.Path(__file__).resolve().parents[2]

PRACTICE = {
    "name": "sample",
    "title": "Sample",
    "version": 1,
    "kind": "run",
    "description": "A sample run workflow.",
    "allowed_on": "demo",
    "inputs": {"label": {"type": "string", "default": "x"}},
    "settings": {},
    "triggers": [{"key": "by_hand", "kind": "manual", "title": "a person"}],
    "steps": [
        {"id": "first", "action": "practice.create_record", "inputs": {"label": "{inputs.label}"}},
        {
            "id": "second",
            "action": "practice.set_status",
            "depends_on": "first",
            "inputs": {"label": "{inputs.label}"},
        },
    ],
}

NOTE = {
    "name": "sample",
    "title": "Sample note",
    "version": 1,
    "kind": "note",
    "description": "A sample note workflow.",
    "triggers": [
        {
            "key": "alert",
            "kind": "alert",
            "title": "an alert",
            "rule": {"source": "Monitoring Alert", "title_contains": "offline"},
        }
    ],
    "steps": [
        {"id": "device", "action": "devices.resolve", "inputs": {"ticket": "{ticket.self}"}},
        {
            "id": "state",
            "action": "ninjaone.read_device",
            "inputs": {"device": "{steps.device.device}"},
            "when": {"value": "{steps.device.confident}", "equals": True},
        },
        {
            "id": "compose",
            "action": "notes.device_online",
            "inputs": {
                "ticket": "{ticket.self}",
                "device": "{steps.device.device}",
                "state": "{steps.state.read}",
            },
        },
        {
            "id": "post",
            "action": "autotask.add_note",
            "inputs": {"ticket_id": "{ticket.id}", "note": "{steps.compose.note}"},
        },
    ],
}


def write(tmp_path: pathlib.Path, data: dict[str, Any], name: str = "sample") -> pathlib.Path:
    path = tmp_path / f"{name}.yaml"
    path.write_bytes(yaml.safe_dump(data, sort_keys=False).encode("utf-8"))
    return path


def refused(tmp_path: pathlib.Path, data: dict[str, Any], match: str) -> None:
    with pytest.raises(DefinitionError, match=match) as found:
        check(write(tmp_path, data))
    message = str(found.value)
    assert message.startswith("sample.yaml: ") and "\n" not in message


# --- the shipped definitions ------------------------------------------------------------


def test_every_shipped_definition_validates() -> None:
    found = validate_all()
    names = {path.stem for path, _ in found}
    assert names >= {
        "practice",
        "device_online",
        "repeat_alert",
        "disk_space",
        "threat",
        "backup_notice",
        "chronic_devices",
    }
    assert [str(e) for _, e in found if e is not None] == []


def test_the_validate_command_runs_clean() -> None:
    found = subprocess.run(  # noqa: S603  our own interpreter, a fixed script
        [sys.executable, str(ROOT / "scripts" / "automation.py"), "validate"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=ROOT,
    )
    assert found.returncode == 0, found.stdout + found.stderr
    assert "0 problem(s)" in found.stdout and "FAIL" not in found.stdout


def test_every_definition_is_registered_with_its_hash() -> None:
    for loaded in library.definitions():
        assert len(loaded.hash) == 64
        compiled = loaded.workflow or loaded.note_workflow
        assert compiled is not None and compiled.definition_hash == loaded.hash
        assert compiled.name == loaded.name and compiled.version == loaded.version


#: The same definition, written the ways a checkout and an editor write it.
#: A `core.autocrlf` checkout gives CRLF on Windows and LF on Linux; the rest
#: an editor leaves behind. None of it is what the workflow does.
def _as_bytes(lf: bytes) -> dict[str, bytes]:
    return {
        "LF": lf,
        "CRLF": lf.replace(b"\n", b"\r\n"),
        "lone CR": lf.replace(b"\n", b"\r"),
        "no final newline": lf.rstrip(b"\n"),
        "byte order mark": b"\xef\xbb\xbf" + lf,
        "trailing spaces": b"\n".join(
            line + b"  " if line.strip() else line for line in lf.split(b"\n")
        ),
    }


def test_the_same_definition_hashes_the_same_whatever_the_bytes(tmp_path: pathlib.Path) -> None:
    """A definition's hash goes on every run drafted from it, and the engine
    runs on Linux while it is written on Windows. The same definition must
    hash the same on both, so the hash is of the parsed definition and not of
    the file: CRLF and LF first, and every other difference a checkout or an
    editor makes after them."""
    path = write(tmp_path, PRACTICE)
    lf = path.read_bytes()
    assert b"\r" not in lf
    hashes: dict[str, str] = {}
    for how, data in _as_bytes(lf).items():
        path.write_bytes(data)
        hashes[how] = check(path).hash
    assert hashes["CRLF"] == hashes["LF"], "a Windows checkout must hash as a Linux one does"
    assert len(set(hashes.values())) == 1, hashes
    assert len(hashes["LF"]) == 64


def test_the_hash_is_of_the_meaning_and_moves_when_that_does(tmp_path: pathlib.Path) -> None:
    """Formatting is not meaning: comments, key order and indentation do not
    move the hash. Anything the engine reads does."""
    first = check(write(tmp_path, PRACTICE)).hash
    assert check(write(tmp_path, PRACTICE)).hash == first
    # The same definition, re-written: a comment, the top-level keys in
    # another order, and the steps' keys in another order.
    reordered = {
        "version": PRACTICE["version"],
        "kind": PRACTICE["kind"],
        "name": PRACTICE["name"],
        "description": PRACTICE["description"],
        "title": PRACTICE["title"],
        "allowed_on": PRACTICE["allowed_on"],
        "triggers": PRACTICE["triggers"],
        "inputs": PRACTICE["inputs"],
        "settings": PRACTICE["settings"],
        "steps": [
            {"inputs": step["inputs"], "action": step["action"], "id": step["id"]}
            | ({"depends_on": step["depends_on"]} if "depends_on" in step else {})
            for step in PRACTICE["steps"]
        ],
    }
    path = tmp_path / "sample.yaml"
    body = yaml.safe_dump(reordered, sort_keys=False)
    path.write_bytes(("# a comment the engine never reads\n" + body).encode("utf-8"))
    assert check(path).hash == first
    # And what the engine does read moves it: a word of the description, the
    # version, a step's inputs, the order of the steps.
    for changed in (
        dict(PRACTICE, description="A sample run workflow, reworded."),
        dict(PRACTICE, version=2),
        dict(PRACTICE, steps=[dict(PRACTICE["steps"][0], inputs={"label": "fixed"})]),
        dict(
            PRACTICE,
            steps=[
                {"id": "second", "action": "practice.set_status", "inputs": {"label": "x"}},
                {"id": "first", "action": "practice.create_record", "inputs": {"label": "x"}},
            ],
        ),
    ):
        assert check(write(tmp_path, changed)).hash != first, changed


def test_the_canonical_form_is_what_the_hash_is_of(tmp_path: pathlib.Path) -> None:
    """One string per definition, and it says what the file says."""
    loaded = check(write(tmp_path, PRACTICE))
    form = library.canonical(loaded.spec)
    assert form.startswith("{") and "\n" not in form and " " not in form.split('"description"')[0]
    assert '"name":"sample"' in form and '"action":"practice.create_record"' in form
    assert hashlib.sha256(form.encode("utf-8")).hexdigest() == loaded.hash
    # Only what the file says: a field nobody set is not in it.
    assert '"note_mode"' not in form


# --- what a definition may not be ---------------------------------------------------------


def test_an_unknown_action_is_refused(tmp_path: pathlib.Path) -> None:
    bad = dict(PRACTICE, steps=[{"id": "first", "action": "autotask.close_ticket"}])
    refused(tmp_path, bad, "first: unknown action 'autotask.close_ticket'")


def test_a_missing_or_mistyped_input_is_refused(tmp_path: pathlib.Path) -> None:
    missing = dict(PRACTICE, steps=[{"id": "first", "action": "practice.create_record"}])
    refused(tmp_path, missing, "first: practice.create_record needs input 'label'")
    mistyped = dict(
        PRACTICE,
        steps=[{"id": "first", "action": "practice.create_record", "inputs": {"label": 7}}],
    )
    refused(tmp_path, mistyped, "first: input 'label' wants a string, not 7")
    unknown = dict(
        PRACTICE,
        steps=[
            {
                "id": "first",
                "action": "practice.create_record",
                "inputs": {"label": "x", "colour": "red"},
            }
        ],
    )
    refused(tmp_path, unknown, "first: practice.create_record has no input 'colour'")
    wrong_ref = dict(NOTE)
    wrong_ref["steps"] = [
        {"id": "device", "action": "devices.resolve", "inputs": {"ticket": "{ticket.title}"}},
        *NOTE["steps"][1:],
    ]
    refused(tmp_path, wrong_ref, "device: input 'ticket' wants a ticket, not {ticket.title}")


def test_a_reference_to_a_step_that_has_not_run_yet_is_refused(tmp_path: pathlib.Path) -> None:
    forward = dict(
        PRACTICE,
        steps=[
            {
                "id": "first",
                "action": "practice.create_record",
                "inputs": {"label": "{steps.second.status}"},
            },
            {"id": "second", "action": "practice.set_status", "inputs": {"label": "x"}},
        ],
    )
    refused(tmp_path, forward, "first: {steps.second.status} refers to a step that has not run yet")
    no_output = dict(
        PRACTICE,
        steps=[
            {"id": "first", "action": "practice.create_record", "inputs": {"label": "x"}},
            {
                "id": "second",
                "action": "practice.set_status",
                "inputs": {"label": "{steps.first.colour}"},
            },
        ],
    )
    refused(
        tmp_path, no_output, "second: {steps.first.colour}: practice.create_record has no output"
    )


def test_a_dependency_on_a_later_step_or_itself_is_refused(tmp_path: pathlib.Path) -> None:
    later = dict(
        PRACTICE,
        steps=[
            {
                "id": "first",
                "action": "practice.create_record",
                "inputs": {"label": "x"},
                "depends_on": "second",
            },
            {"id": "second", "action": "practice.set_status", "inputs": {"label": "x"}},
        ],
    )
    refused(tmp_path, later, "first: depends on 'second', which is not an earlier step")
    itself = dict(
        PRACTICE,
        steps=[
            {
                "id": "first",
                "action": "practice.create_record",
                "inputs": {"label": "x"},
                "depends_on": "first",
            }
        ],
    )
    refused(tmp_path, itself, "first: depends on itself")


def test_an_undeclared_setting_or_input_is_refused(tmp_path: pathlib.Path) -> None:
    setting = dict(
        PRACTICE,
        steps=[
            {
                "id": "first",
                "action": "practice.create_record",
                "inputs": {"label": "{settings.prefix}"},
            }
        ],
    )
    refused(tmp_path, setting, "first: {settings.prefix} names a setting the definition does not")
    an_input = dict(
        PRACTICE,
        steps=[
            {
                "id": "first",
                "action": "practice.create_record",
                "inputs": {"label": "{inputs.nope}"},
            }
        ],
    )
    refused(tmp_path, an_input, "first: {inputs.nope} names an input the definition does not")


def test_anything_outside_the_syntax_is_refused(tmp_path: pathlib.Path) -> None:
    template = dict(
        PRACTICE,
        steps=[
            {
                "id": "first",
                "action": "practice.create_record",
                "inputs": {"label": "run-{inputs.label}"},
            }
        ],
    )
    refused(tmp_path, template, "there are no templates")
    expression = dict(
        PRACTICE,
        steps=[
            {
                "id": "first",
                "action": "practice.create_record",
                "inputs": {"label": "{inputs.label | upper}"},
            }
        ],
    )
    refused(tmp_path, expression, "is not a reference")
    bad_operator = dict(NOTE)
    bad_operator["steps"] = [
        NOTE["steps"][0],
        dict(NOTE["steps"][1], when={"value": "{steps.device.confident}", "matches": ".*"}),
        *NOTE["steps"][2:],
    ]
    refused(tmp_path, bad_operator, "when")
    two_operators = dict(NOTE)
    two_operators["steps"] = [
        NOTE["steps"][0],
        dict(
            NOTE["steps"][1],
            when={"value": "{steps.device.confident}", "equals": True, "exists": True},
        ),
        *NOTE["steps"][2:],
    ]
    refused(tmp_path, two_operators, "exactly one operator")
    literal_compared = dict(NOTE)
    literal_compared["steps"] = [
        NOTE["steps"][0],
        dict(NOTE["steps"][1], when={"value": True, "equals": True}),
        *NOTE["steps"][2:],
    ]
    refused(tmp_path, literal_compared, "the value compared is a reference")
    unknown_key = dict(PRACTICE, script="print(1)")
    refused(tmp_path, unknown_key, "script")
    refused(tmp_path, dict(PRACTICE, kind="agent"), "kind 'agent' is not one of")
    refused(tmp_path, dict(PRACTICE, name="other"), "must match the file name")
    refused(tmp_path, dict(PRACTICE, undo_window="soon"), "not a duration")


def test_a_note_workflow_ends_with_its_one_write(tmp_path: pathlib.Path) -> None:
    no_write = dict(NOTE, steps=NOTE["steps"][:-1])
    refused(tmp_path, no_write, "ends with its one write step")
    two_triggers = dict(NOTE, triggers=[*NOTE["triggers"], *NOTE["triggers"]])
    refused(tmp_path, two_triggers, "one alert trigger")
    with_inputs = dict(NOTE, inputs={"x": {"type": "string"}})
    refused(tmp_path, with_inputs, "takes no inputs")


def test_a_required_setting_gates_the_workflow(tmp_path: pathlib.Path) -> None:
    data = dict(
        PRACTICE,
        settings={
            "team": {"type": "string", "required": True, "description": "The team to notify."},
            "days": {"type": "integer", "default": 7},
        },
    )
    loaded = check(write(tmp_path, data))
    assert [m.key for m in loaded.missing_settings({})] == ["team"]
    assert loaded.missing_settings({"team": "Service desk"}) == ()
    assert [m.key for m in loaded.missing_settings({"team": 7})] == ["team"]
    assert loaded.wrong_settings({"team": 7, "colour": "red", "triggers": {}}) == [
        "team: expected string, got int",
        "colour: not a setting of sample (it declares: days, team)",
    ]
    refused(
        tmp_path,
        dict(PRACTICE, settings={"team": {"type": "string", "required": True, "default": "x"}}),
        "a required setting has no default",
    )
    refused(tmp_path, dict(PRACTICE, settings={"team": {"type": "regex"}}), "type 'regex'")


# --- the syntax, resolved -----------------------------------------------------------------


def test_references_resolve_and_conditions_hold() -> None:
    ticket = arrival("5", "Device offline")
    scope = Scope(
        inputs={"label": "x"},
        settings={"days": 3},
        steps={"device": {"confident": True, "count": 2}, "skipped": None},
        ticket=ticket,
    )
    assert scope.resolve(parse_reference("{inputs.label}")) == "x"
    assert scope.resolve(parse_reference("{settings.days}")) == 3
    assert scope.resolve(parse_reference("{steps.device.confident}")) is True
    assert scope.resolve(parse_reference("{steps.skipped.anything}")) is None
    assert scope.resolve(parse_reference("{ticket.self}")) is ticket
    assert scope.resolve(parse_reference("{ticket.number}")) == "T5"
    assert parse_reference("plain words") is None
    assert parse_reference(7) is None
    from app.automation.library import ConditionDef

    assert condition_holds(ConditionDef(value="{steps.device.confident}", equals=True), scope)
    assert not condition_holds(
        ConditionDef(value="{steps.device.confident}", not_equals=True), scope
    )
    assert condition_holds(
        ConditionDef.model_validate({"value": "{settings.days}", "in": [1, 3]}), scope
    )
    assert condition_holds(ConditionDef(value="{steps.skipped.x}", exists=False), scope)
    assert condition_holds(ConditionDef(value="{steps.device.count}", greater_than=1), scope)
    assert not condition_holds(ConditionDef(value="{steps.device.count}", less_than=1), scope)
    assert not condition_holds(ConditionDef(value="{steps.skipped.x}", greater_than=1), scope)


async def test_a_compiled_note_workflow_runs_its_steps_in_order(tmp_path: pathlib.Path) -> None:
    """The sample note definition, compiled and run against the fake tools:
    the reads happen in the order written, a gated step is skipped when its
    condition fails, and the write step never runs here."""
    loaded = check(write(tmp_path, NOTE))
    compiled = loaded.note_workflow
    assert compiled is not None and compiled.build is not None
    tools = FakeTools()
    note = await compiled.build(
        NoteContext(ticket=arrival("1", "Device offline"), tools=tools, settings={}, now=NOW)
    )
    assert tools.reads == ["resolve", "state"]
    assert not isinstance(note, Skip) and note.title == "CON-FS-01 back online"
    assert [s["title"] for s in compiled.step_views] == [
        "Which machine the ticket is about",
        "NinjaOne: device status, last contact, last boot",
        "The note: back online, or still offline",
        "Autotask: add an internal note to the ticket",
    ]
    assert compiled.reads == (
        "Which machine the ticket is about",
        "NinjaOne: device status, last contact, last boot",
    )
    # Not confident: the gated read is skipped and the note is a skip.
    unsure = FakeTools(device=LOW)
    skipped = await compiled.build(
        NoteContext(ticket=arrival("1", "Device offline"), tools=unsure, settings={}, now=NOW)
    )
    assert unsure.reads == ["resolve"] and isinstance(skipped, Skip)


def test_the_definitions_directory_is_where_it_says() -> None:
    assert DEFINITIONS_DIR == ROOT / "app" / "automation" / "definitions"
    practice = library.definition("practice")
    assert practice.kind == "run" and practice.workflow is not None
    assert practice.workflow.undo_window == dt.timedelta(hours=24)
    assert practice.workflow.triggers[2].every == dt.timedelta(days=1)

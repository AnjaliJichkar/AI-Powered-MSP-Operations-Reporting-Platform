"""Workflows as data, against the real application: what a run records of
the definition it ran, what each step was handed and returned, and the
settings a definition requires before a client may turn it on.

No vendor and no model anywhere: the practice workflow acts on the engine's
own table, and the gated sample workflow is a file written here and loaded
for the test alone.
"""

# ruff: noqa: F811  (the `world` fixture is imported from the pages tests)
from __future__ import annotations

import pathlib
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.automation import library, runner
from app.automation.definitions import _WORKFLOWS
from app.models import ReportConfig
from tests.api.test_automation_pages import _staff, world  # noqa: F401

pytestmark = pytest.mark.usefixtures("clean")

GATED = {
    "name": "gated_sample",
    "title": "Gated sample",
    "version": 1,
    "kind": "run",
    "description": "A run workflow that needs a setting before it is turned on.",
    "allowed_on": "demo",
    "inputs": {"label": {"type": "string", "default": "gated"}},
    "settings": {
        "team": {"type": "string", "required": True, "description": "The team to tell."},
        "days": {"type": "integer", "default": 7, "description": "How far back to look."},
    },
    "triggers": [{"key": "by_hand", "kind": "manual", "title": "a person"}],
    "steps": [
        {"id": "first", "action": "practice.create_record", "inputs": {"label": "{inputs.label}"}}
    ],
}


async def test_a_run_records_the_definition_and_each_step_its_values(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    signed = await _staff(http)
    assert (
        await signed.post("/automation/switches/practice", json={"enabled": True})
    ).status_code == 200
    drafted = await signed.post(
        "/automation/runs", json={"workflow": "practice", "inputs": {"label": "lib"}}
    )
    assert drafted.status_code == 201, drafted.text
    run_id = drafted.json()["id"]
    practice = library.definition("practice")
    # The run says what it ran: name, version and the file's hash.
    page = (await http.get(f"/automation/runs/{run_id}")).json()
    assert page["run"]["definition_hash"] == practice.hash
    assert page["run"]["version"] == practice.version
    # The dry run resolved every step's references and wrote them on the row.
    assert (await signed.post(f"/automation/runs/{run_id}/dry-run")).status_code == 200
    page = (await http.get(f"/automation/runs/{run_id}")).json()
    first = page["steps"][0]
    assert first["inputs"] == {"label": "lib", "fail_at": None, "fail_times": 1}
    assert first["outputs"] is None
    # After it runs, each step's outputs are there too.
    assert (await signed.post(f"/automation/runs/{run_id}/submit")).status_code == 200
    second = await _staff(http, "second@inputiv.test")
    approved = await second.post(
        f"/automation/runs/{run_id}/approve",
        json={"authoriser_name": "Dana Whitfield", "authoriser_method": "phone"},
    )
    assert approved.status_code == 200, approved.text
    for _ in range(8):
        outcome = await runner.run_once(session_maker)
        if outcome.result in ("completed", "idle"):
            break
    page = (await http.get(f"/automation/runs/{run_id}")).json()
    assert page["run"]["state"]["key"] == "completed"
    outputs = [s["outputs"] for s in page["steps"]]
    assert outputs[0]["created"] is True and outputs[0]["name"].startswith("lib-")
    assert outputs[1]["status"] == "offboarded" and outputs[2]["status"] == "closed"
    assert outputs[3]["note"].startswith("closed by practice run")


async def test_the_workflow_page_renders_from_the_definition(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    await _staff(http)
    page = (await http.get("/automation/workflows/practice")).json()
    definition = page["definition"]
    assert definition["file"] == "practice.yaml" and len(definition["hash"]) == 64
    assert [s["action"] for s in definition["steps"]] == [
        "practice.create_record",
        "practice.set_status",
        "practice.close_out",
        "practice.write_note",
    ]
    assert definition["steps"][1]["depends_on"] == "create_record"
    assert definition["steps"][0]["inputs"]["label"] == "{inputs.label}"
    assert [s["action"] for s in page["steps"]] == [s["action"] for s in definition["steps"]]
    assert page["workflow"]["definition_hash"] == definition["hash"]
    # A note workflow's page lists its steps by action title, from the file.
    note = (await http.get("/automation/workflows/device_online")).json()
    assert [s["action"] for s in note["workflow"]["steps"]] == [
        "devices.resolve",
        "ninjaone.read_device",
        "ninjaone.read_open_alerts",
        "notes.device_online",
        "autotask.add_note",
    ]
    assert note["definition"]["steps"][1]["when"] == [
        {"value": "{steps.device.confident}", "equals": True}
    ]
    assert note["workflow"]["steps"][-1]["writes"] is True


async def test_a_workflow_cannot_be_turned_on_until_its_settings_are_set(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "gated_sample.yaml"
    path.write_bytes(yaml.safe_dump(GATED, sort_keys=False).encode("utf-8"))
    loaded = library.load(path)
    try:
        signed = await _staff(http)
        # The Automations page says what is missing, and the switch refuses.
        overview = (await http.get("/automation/overview")).json()
        row = next(w for w in overview["workflows"] if w["name"] == "gated_sample")
        assert [m["key"] for m in row["missing_settings"]] == ["team"]
        assert row["missing_settings"][0]["type"] == "string"
        refused = await signed.post("/automation/switches/gated_sample", json={"enabled": True})
        assert refused.status_code == 409 and "team (string)" in refused.json()["detail"]
        # The workflow page lists every declared setting, set or not.
        page = (await http.get("/automation/workflows/gated_sample")).json()
        assert [(s["key"], s["required"], s["set"]) for s in page["settings_used"]] == [
            ("team", True, False),
            ("days", False, False),
        ]
        assert [m["key"] for m in page["missing_settings"]] == ["team"]
        # Set it the way manage_client.py does, and the switch goes on.
        async with session_maker() as session:
            config = await session.get(ReportConfig, world["client"].id)
            assert config is not None
            given = dict(config.config)
            automation = dict(given.get("automation") or {})
            automation["workflows"] = {"gated_sample": {"team": "Service desk"}}
            given["automation"] = automation
            config.config = given
            await session.commit()
        assert loaded.wrong_settings({"team": "Service desk"}) == []
        assert loaded.wrong_settings({"team": 5}) == ["team: expected string, got int"]
        switched = await signed.post("/automation/switches/gated_sample", json={"enabled": True})
        assert switched.status_code == 200, switched.text
        overview = (await http.get("/automation/overview")).json()
        row = next(w for w in overview["workflows"] if w["name"] == "gated_sample")
        assert row["missing_settings"] == [] and row["enabled"] is True
    finally:
        _WORKFLOWS.pop("gated_sample", None)
        library._DEFINITIONS.pop("gated_sample", None)

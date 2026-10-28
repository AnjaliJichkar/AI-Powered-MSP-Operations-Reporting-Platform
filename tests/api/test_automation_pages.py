"""The Automations pages and their JSON, against the real application.

What is tested is who may see any of it and what the pages must never do: a
franchisee is refused every page and every endpoint; the workflow page's
router holds no state-changing route at all, asserted on the application's
own route list; the pages render for staff; and the JSON a page draws from
carries what the page says it does — the trigger sentences, the switch
states, the "drafted by" of a triggered run, the trigger log.

No vendor and no model anywhere: the tickets a rule looks at are `Arrival`s
built by hand, as the poller builds them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.dependencies import api_routes
from app.automation import triggers
from app.models import Client, OrganisationKind, ReportConfig, Role
from app.models.client import ClientKind
from tests.api.conftest import Make, sign_in

pytestmark = pytest.mark.usefixtures("clean")

NOW = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
async def world(
    session_maker: async_sessionmaker[AsyncSession], make_org: Make, make_user: Make
) -> dict[str, Any]:
    async with session_maker() as session:
        client = Client(slug="tailwind-it", name="Tailwind IT", kind=ClientKind.DEMO)
        session.add(client)
        await session.flush()
        session.add(
            ReportConfig(
                client_id=client.id,
                config={"automation": {"approvers": [{"name": "Dana Whitfield"}]}},
                updated_by="test",
            )
        )
        await session.commit()
        await session.refresh(client)
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    theirs = await make_org(OrganisationKind.FRANCHISEE, "tailwind-people", client=client)
    await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER)
    await make_user(inputiv, "second@inputiv.test", Role.INPUTIV_ENGINEER)
    await make_user(theirs, "boss@tailwind.test", Role.FRANCHISEE_ADMIN)
    await make_user(theirs, "tech@tailwind.test", Role.FRANCHISEE_TECHNICIAN)
    await make_user(theirs, "viewer@tailwind.test", Role.FRANCHISEE_VIEWER)
    return {"client": client}


async def _staff(http: httpx.AsyncClient, who: str = "engineer@inputiv.test") -> Any:
    signed = await sign_in(http, who)
    switched = await signed.post("/auth/switch-client", json={"client": "tailwind-it"})
    assert switched.status_code == 200, switched.text
    return signed


def _automation_routes(app: FastAPI) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for route in api_routes(app):
        if not (route.path.startswith("/automation") or route.path.startswith("/automations")):
            continue
        for method in sorted((route.methods or {"GET"}) - {"HEAD", "OPTIONS"}):
            found.append((method, route.path))
    return found


# --- who may see any of it --------------------------------------------------------------


@pytest.mark.parametrize(
    "who", ["boss@tailwind.test", "tech@tailwind.test", "viewer@tailwind.test"]
)
async def test_a_franchisee_is_refused_every_automation_page_and_endpoint(
    http: httpx.AsyncClient, app: FastAPI, world: dict[str, Any], who: str
) -> None:
    """403 on all of it, page and JSON alike, for every franchisee role, and
    no nav entry. The engine is ours; nothing here is theirs to see — the
    Desk note on a ticket included, until the client's
    `automation.notes.franchisee_can_post` setting says otherwise."""
    signed = await sign_in(http, who)
    routes = _automation_routes(app)
    assert len(routes) >= 18, routes
    for method, path in routes:
        url = _url(path)
        response = await http.request(
            method, url, headers=signed.headers(), json={} if method != "GET" else None
        )
        assert response.status_code == 403, f"{method} {url} -> {response.status_code}"
    assert ">Automations<" not in (await http.get("/reports")).text


NOTE_ROUTES = {
    "/automation/notes/for-ticket/{ticket_id}",
    "/automation/notes/{note_id}/post",
    "/automation/notes/{note_id}/not-useful",
}


def _url(path: str) -> str:
    return (
        path.replace("{run_id}", str(uuid.uuid4()))
        .replace("{workflow_name}", "practice")
        .replace("{trigger_key}", "daily")
        .replace("{note_id}", str(uuid.uuid4()))
        .replace("{ticket_id}", "70345")
    )


async def _let_franchisee_post(
    session_maker: async_sessionmaker[AsyncSession], client: Client, on: bool
) -> None:
    async with session_maker() as session:
        row = await session.get(ReportConfig, client.id)
        assert row is not None
        config = dict(row.config)
        automation = dict(config.get("automation") or {})
        automation["notes"] = {"franchisee_can_post": on}
        config["automation"] = automation
        row.config = config
        await session.commit()


@pytest.mark.parametrize(
    "who", ["boss@tailwind.test", "tech@tailwind.test", "viewer@tailwind.test"]
)
async def test_franchisee_can_post_opens_the_note_routes_to_their_engineers_only(
    http: httpx.AsyncClient,
    app: FastAPI,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
    who: str,
) -> None:
    """With `automation.notes.franchisee_can_post` on, the franchisee's
    administrators and technicians reach the three note routes and nothing
    else; a viewer is still refused everything."""
    await _let_franchisee_post(session_maker, world["client"], True)
    signed = await sign_in(http, who)
    for method, path in _automation_routes(app):
        url = _url(path)
        response = await http.request(
            method, url, headers=signed.headers(), json={} if method != "GET" else None
        )
        if path in NOTE_ROUTES and who != "viewer@tailwind.test":
            assert response.status_code in (200, 404), f"{method} {url} -> {response.status_code}"
        else:
            assert response.status_code == 403, f"{method} {url} -> {response.status_code}"


def test_the_workflow_page_is_read_only_by_construction(app: FastAPI) -> None:
    """No POST, PUT, PATCH or DELETE under `/automation/workflows` or the
    page's own path. The switches live under `/automation/switches`, on the
    Automations page, where the record of throwing one is beside the runs."""
    writes = [
        (method, path)
        for method, path in _automation_routes(app)
        if (path.startswith("/automation/workflows") or path.startswith("/automations/workflows"))
        and method != "GET"
    ]
    assert writes == []
    assert ("GET", "/automation/workflows/{workflow_name}") in _automation_routes(app)
    assert ("GET", "/automations/workflows/{workflow_name}") in _automation_routes(app)


# --- the pages render -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/automations", "/automations/workflows/practice", "/automations?tab=triggers"]
)
async def test_the_pages_render_for_staff(
    http: httpx.AsyncClient, world: dict[str, Any], path: str
) -> None:
    await _staff(http)
    response = await http.get(path)
    assert response.status_code == 200, path
    assert 'class="shell"' in response.text
    assert ">Automations<" in response.text


async def test_the_workflow_page_is_a_page_and_not_a_run_id(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """`/automations/workflows/<name>` is declared before `/automations/<run_id>`,
    so the word is never read as a run id and 422'd."""
    await _staff(http)
    response = await http.get("/automations/workflows/practice")
    assert response.status_code == 200
    assert "automation_workflow.js" in response.text
    assert '"practice"' in response.text


# --- the JSON the pages draw from ---------------------------------------------------------


async def test_the_workflow_view_says_what_starts_it_and_what_each_step_does(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    signed = await _staff(http)
    response = await http.get("/automation/workflows/practice", headers=signed.headers())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["workflow"]["name"] == "practice" and data["workflow"]["manual"] is True
    sentences = {t["key"]: t["sentence"] for t in data["triggers"]}
    assert sentences["offboarding_ticket"] == (
        "When a ticket arrives for Tailwind IT with issue type User Access and Management "
        "and with sub-issue type Offboarding."
    )
    assert sentences["daily"].startswith("On a schedule, every day, for Tailwind IT")
    assert all(t["enabled"] is False for t in data["triggers"]), "off until somebody turns one on"
    label = next(i for i in data["inputs"] if i["name"] == "label")
    assert label["source"] == "ticket.number" and label["source_words"] == "the ticket's number"
    assert next(i for i in data["inputs"] if i["name"] == "fail_at")["source"] == "engineer"
    close_out = next(s for s in data["steps"] if s["key"] == "close_out")
    assert close_out["checks"] and close_out["does"] and close_out["confirms"]
    assert close_out["undoes"] and close_out["reversible"] is True
    assert close_out["depends_on"] == "set_status" and close_out["depends_on_title"]
    note = next(s for s in data["steps"] if s["key"] == "write_note")
    assert note["reversible"] is False and note["undoes"] is None
    assert data["approval"]["window_hours"] == 24 and data["approval"]["undo_window_hours"] == 24
    assert data["approval"]["stale_after_days"] == 7
    assert data["approval"]["approvers"][0]["name"] == "Dana Whitfield"
    assert data["recent_runs"] == []


async def test_the_overview_counts_switches_and_a_triggered_run_says_who_drafted_it(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    signed = await _staff(http)
    # Both switches from the page's own endpoints.
    on = await signed.post("/automation/switches/practice", json={"enabled": True})
    assert on.status_code == 200, on.text
    on = await signed.post(
        "/automation/switches/practice/triggers/offboarding_ticket", json={"enabled": True}
    )
    assert on.status_code == 200 and on.json()["enabled"] is True, on.text
    unknown = await signed.post(
        "/automation/switches/practice/triggers/nope", json={"enabled": True}
    )
    assert unknown.status_code == 404
    # A ticket arrives, as the poller would hand it in.
    client = world["client"]
    async with session_maker() as session:
        found = await triggers.evaluate(
            session,
            client.id,
            [
                triggers.Arrival(
                    ticket_id="1",
                    ticket_number="T20261013.0001",
                    title="Leaver",
                    issue_type="User Access and Management",
                    sub_issue_type="Offboarding",
                ),
                triggers.Arrival(ticket_id="2", ticket_number="T20261013.0002", title="Printer"),
            ],
            now=NOW,
        )
        await session.commit()
    assert [f.outcome for f in found if f.workflow == "practice"] == ["drafted", "no_match"]

    overview = await http.get("/automation/overview")
    assert overview.status_code == 200, overview.text
    data = overview.json()
    practice = next(w for w in data["workflows"] if w["name"] == "practice")
    assert practice["enabled"] is True
    assert {t["key"]: t["enabled"] for t in practice["triggers"]} == {
        "by_hand": False,
        "offboarding_ticket": True,
        "daily": False,
    }
    run = data["runs"][0]
    assert run["started_how"] == "trigger"
    assert run["started_by"] == "ticket rule"
    assert run["trigger_detail"] == "ticket rule on T20261013.0001"
    assert run["started_by_user_id"] is None and run["ticket_number"] == "T20261013.0001"
    log = data["trigger_log"]
    assert [(r["ticket_number"], r["outcome"]) for r in log if r["workflow"] == "practice"] == [
        ("T20261013.0002", "no_match"),
        ("T20261013.0001", "drafted"),
    ]
    assert log[-1]["run_id"] == run["id"] and log[-1]["rule"] == {
        "issue_type": "User Access and Management",
        "sub_issue_type": "Offboarding",
    }
    # The same rows from the log's own endpoint, newest first.
    own = await http.get("/automation/triggers/log")
    assert own.status_code == 200 and own.json()["shown"] == 100
    assert [r["id"] for r in own.json()["rows"]] == [r["id"] for r in log]

    # The run page's JSON: an engineer may approve a triggered run (nobody
    # started it), and the expiry is on it once they do.
    page = await http.get(f"/automation/runs/{run['id']}")
    assert page.status_code == 200
    assert page.json()["run"]["started_how"] == "trigger"
    dry = await signed.post(f"/automation/runs/{run['id']}/dry-run")
    assert dry.status_code == 200 and dry.json()["run"]["state"]["key"] == "dry_run_ready"
    submitted = await signed.post(f"/automation/runs/{run['id']}/submit")
    assert submitted.json()["can"]["approve"] is True
    approved = await signed.post(
        f"/automation/runs/{run['id']}/approve",
        json={"authoriser_name": "Dana Whitfield", "authoriser_method": "email"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["approval_expires_at"] is not None


async def test_a_failed_run_past_the_window_offers_a_fresh_approval_not_a_resume(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from app.automation import runner, service

    signed = await _staff(http)
    assert (
        await signed.post("/automation/switches/practice", json={"enabled": True})
    ).status_code == 200
    drafted = await signed.post(
        "/automation/runs", json={"workflow": "practice", "inputs": {"fail_at": "close_out"}}
    )
    assert drafted.status_code == 201, drafted.text
    run_id = drafted.json()["id"]
    await signed.post(f"/automation/runs/{run_id}/dry-run")
    await signed.post(f"/automation/runs/{run_id}/submit")
    second = await _staff(http, "second@inputiv.test")
    approved = await second.post(
        f"/automation/runs/{run_id}/approve",
        json={"authoriser_name": "Dana Whitfield", "authoriser_method": "phone"},
    )
    assert approved.status_code == 200, approved.text
    clock = [dt.datetime.now(dt.UTC)]
    outcome = await runner.run_once(session_maker, clock=lambda: clock[0])
    assert outcome.result == "failed"
    before = await http.get(f"/automation/runs/{run_id}")
    assert before.json()["can"]["resume"] is True and before.json()["can"]["resubmit"] is False
    assert before.json()["run"]["resume_by"] is not None
    # Move the failure back in time past the window.
    async with session_maker() as session:
        run = await service.get_run(session, world["client"].id, uuid.UUID(run_id))
        assert run.finished_at is not None
        run.finished_at = run.finished_at - dt.timedelta(hours=25)
        await session.commit()
    after = await http.get(f"/automation/runs/{run_id}")
    assert after.json()["can"]["resume"] is False and after.json()["can"]["resubmit"] is True
    refused = await second.post(f"/automation/runs/{run_id}/resume")
    assert refused.status_code == 409 and "fresh approval" in refused.json()["detail"]
    resubmitted = await second.post(f"/automation/runs/{run_id}/resubmit")
    assert resubmitted.status_code == 200, resubmitted.text
    assert resubmitted.json()["run"]["state"]["key"] == "awaiting_approval"
    assert resubmitted.json()["run"]["authoriser"] is None


# --- start a workflow from a ticket ----------------------------------------------------


async def test_the_ticket_page_offers_start_a_workflow_to_staff_only(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """The group is left out of the markup for a franchisee, not hidden by
    script, and the endpoint behind it refuses them anyway (the refusal test
    above walks `/automation/startable` with the rest)."""
    for who in ("boss@tailwind.test", "tech@tailwind.test"):
        await sign_in(http, who)
        page = await http.get("/tickets/T20261013.0001")
        assert page.status_code == 200
        assert 'id="start-workflow"' not in page.text, who
    await _staff(http)
    page = await http.get("/tickets/T20261013.0001")
    assert page.status_code == 200
    assert 'id="start-workflow"' in page.text and "Start a workflow" in page.text


async def test_startable_lists_what_is_on_and_a_ticket_draft_fills_its_inputs(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    signed = await _staff(http)
    # Off: nothing to start.
    nothing = await http.get("/automation/startable")
    assert nothing.status_code == 200 and nothing.json() == {"workflows": [], "halted": False}
    assert (
        await signed.post("/automation/switches/practice", json={"enabled": True})
    ).status_code == 200
    listed = await http.get("/automation/startable")
    assert [w["name"] for w in listed.json()["workflows"]] == ["practice"]
    assert listed.json()["workflows"][0]["from_ticket"] == ["ticket.number"]
    # From the ticket: the number is attached and fills the label; the
    # engineer's inputs take their defaults; the starter is the engineer.
    drafted = await signed.post(
        "/automation/runs",
        json={
            "workflow": "practice",
            "ticket": {
                "number": "T20261013.0007",
                "title": "Leaver: R Patel",
                "contact": "Priya Raman",
                "company": "Contoso Legal",
                "site": "contoso-legal",
            },
        },
    )
    assert drafted.status_code == 201, drafted.text
    run = drafted.json()
    assert run["ticket_number"] == "T20261013.0007"
    assert run["inputs"] == {"label": "T20261013.0007", "fail_at": None, "fail_times": 1}
    assert run["started_how"] == "engineer" and run["started_by"] == "engineer@inputiv.test"
    # An explicit input stands over the ticket's.
    again = await signed.post(
        "/automation/runs",
        json={
            "workflow": "practice",
            "inputs": {"label": "by-hand"},
            "ticket": {"number": "T20261013.0008"},
        },
    )
    assert again.status_code == 201 and again.json()["inputs"]["label"] == "by-hand"
    # Halted, the list says so rather than offering what would be refused.
    assert (await signed.post("/automation/kill-switch", json={"halted": True})).status_code == 200
    assert (await http.get("/automation/startable")).json()["halted"] is True


# --- the Desk notes ------------------------------------------------------------------------


async def test_a_note_on_a_ticket_its_mode_and_the_notes_view(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    from app.automation.notes import service as notes
    from tests.api.test_automation_notes import FakeTools, arrival

    signed = await _staff(http)
    client = world["client"]
    on = await signed.post("/automation/switches/device_online", json={"enabled": True})
    assert on.status_code == 200
    on = await signed.post(
        "/automation/switches/device_online/triggers/down_alert", json={"enabled": True}
    )
    assert on.status_code == 200
    async with session_maker() as session:
        found = await notes.evaluate(
            session,
            client.id,
            [arrival("70345", "Device offline for more than 24 hours")],
            tools=FakeTools(),
            writer_for=None,
            now=dt.datetime.now(dt.UTC),
        )
        await session.commit()
    assert [f.outcome for f in found if f.workflow == "device_online"] == ["noted"]

    # The ticket page's JSON: the note, its mode, and what may be done with it.
    page = await http.get("/automation/notes/for-ticket/70345")
    assert page.status_code == 200, page.text
    note = page.json()["notes"][0]
    assert note["state"] == "built" and note["mode"] == "preview"
    assert note["title"] == "Desk: CON-FS-01 back online" and note["can"]["post"] is True
    assert note["facts"][0]["read_at"] and page.json()["approval_rules"]["preview"]
    # By number as well as by id, which is what the page holds.
    by_number = await http.get("/automation/notes/for-ticket/T70345")
    assert by_number.json()["notes"][0]["id"] == note["id"]

    # A franchisee technician sees no note and may do nothing with it until
    # the client's setting lets them; then it is theirs to dismiss.
    tech = await sign_in(http, "tech@tailwind.test")
    hidden = await http.get("/automation/notes/for-ticket/70345", headers=tech.headers())
    assert hidden.status_code == 403
    assert (await tech.post(f"/automation/notes/{note['id']}/not-useful")).status_code == 403
    await _let_franchisee_post(session_maker, client, True)
    assert (await http.get("/automation/notes/for-ticket/70345", headers=tech.headers())).json()[
        "notes"
    ][0]["id"] == note["id"]
    dismissed = await tech.post(f"/automation/notes/{note['id']}/not-useful")
    assert dismissed.status_code == 200 and dismissed.json()["state"] == "not_useful"
    assert dismissed.json()["not_useful_by"] == "tech@tailwind.test"
    again = await tech.post(f"/automation/notes/{note['id']}/post")
    assert again.status_code == 409
    await _let_franchisee_post(session_maker, client, False)
    hidden = await http.get("/automation/notes/for-ticket/70345", headers=tech.headers())
    assert hidden.status_code == 403

    # The mode: auto needs the approver; the overview and the workflow page show it.
    signed = await _staff(http)
    refused = await signed.post("/automation/note-modes/device_online", json={"mode": "auto"})
    assert refused.status_code == 409 and "approver recorded" in refused.json()["detail"]
    switched = await signed.post(
        "/automation/note-modes/device_online",
        json={"mode": "auto", "authoriser_name": "Dana Whitfield", "authoriser_method": "email"},
    )
    assert switched.status_code == 200 and switched.json()["mode"] == "auto"
    assert switched.json()["authoriser"]["name"] == "Dana Whitfield"
    overview = (await http.get("/automation/overview")).json()
    row = next(w for w in overview["workflows"] if w["name"] == "device_online")
    assert row["kind"] == "note" and row["mode"]["mode"] == "auto" and row["enabled"] is True
    assert row["triggers"][0]["sentence"].startswith(
        "When an alert raises a ticket for Tailwind IT"
    )
    wf = (await http.get("/automation/workflows/device_online")).json()
    assert wf["workflow"]["kind"] == "note" and wf["mode"]["word"] == "Auto-post"
    assert "approved once" in wf["approval"]["rule"] and wf["reads"] and wf["template"]
    assert wf["recent_notes"][0]["id"] == note["id"]
    back = await signed.post("/automation/note-modes/device_online", json={"mode": "preview"})
    assert back.status_code == 200 and back.json()["authoriser"] is None
    # A report workflow has no mode.
    none = await signed.post("/automation/note-modes/chronic_devices", json={"mode": "auto"})
    assert none.status_code == 409

    # The Notes view.
    report = (await http.get("/automation/notes/report")).json()
    row = next(r for r in report["by_workflow"] if r["workflow"] == "device_online")
    assert (row["built"], row["not_useful"], row["share_not_useful"]) == (1, 1, 1.0)
    recent = (await http.get("/automation/notes/recent")).json()["notes"]
    assert recent[0]["id"] == note["id"]
    # The chronic report page's JSON, with nothing built yet.
    chronic = await http.get("/automation/reports/chronic_devices")
    assert chronic.status_code == 200 and chronic.json()["report"] is None
    assert (await http.get("/automation/reports/device_online")).status_code == 404
    # And the pages render.
    for path in ("/automations/workflows/device_online", "/automations/reports/chronic_devices"):
        assert (await http.get(path)).status_code == 200, path


async def test_the_demo_writer_posts_to_the_fake_autotask_with_the_internal_value(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """The one write, end to end on the demo tenant: the page's Post, the
    engine's writer built for a demo client, the fake Autotask accepting the
    note and keeping it on the world with the internal publish value."""
    from app.automation.notes import service as notes
    from app.demo.connectors import _api, demo_world
    from app.models.enums import ConnectorName
    from tests.api.test_automation_notes import FakeTools, arrival

    client = world["client"]
    # The world the fake Autotask answers from (one per connector name, slug
    # and seed), which is where a posted note lands.
    world_ = _api(ConnectorName.AUTOTASK_NOTES, client.slug, demo_world(client.slug).seed).world
    ticket = world_.tickets[0]
    signed = await _staff(http)
    on = await signed.post("/automation/switches/disk_space", json={"enabled": True})
    assert on.status_code == 200
    on = await signed.post(
        "/automation/switches/disk_space/triggers/disk_alert", json={"enabled": True}
    )
    assert on.status_code == 200
    async with session_maker() as session:
        await notes.evaluate(
            session,
            client.id,
            [arrival(str(ticket.id), "Disk space low on C: (below 10%)")],
            tools=FakeTools(),
            writer_for=None,
            now=dt.datetime.now(dt.UTC),
        )
        await session.commit()
    note = (await http.get(f"/automation/notes/for-ticket/{ticket.id}")).json()["notes"][0]
    before = len(world_.notes)
    posted = await signed.post(f"/automation/notes/{note['id']}/post")
    assert posted.status_code == 200, posted.text
    assert posted.json()["state"] == "posted", posted.json()["post_error"]
    assert posted.json()["posted_by"] == "engineer@inputiv.test"
    assert len(world_.notes) == before + 1
    landed = world_.notes[-1]
    # The value is read from the fake tenant's own field information: the
    # one labelled Internal Project Team, which the fake refuses to be without.
    from app.demo.autotask_api import FIELDS

    publish = FIELDS["TicketNotes"]["publish"]
    internal = next(v for v, label in publish.items() if label == "Internal Project Team")
    assert landed["ticketID"] == ticket.id and landed["publish"] == internal
    assert FIELDS["TicketNotes"]["publish"][landed["publish"]] == "Internal Project Team"
    assert "Client" not in FIELDS["TicketNotes"]["noteType"][landed["noteType"]]
    assert landed["title"].startswith("Desk: Disk space on")
    assert posted.json()["autotask_note_id"] == str(landed["id"])

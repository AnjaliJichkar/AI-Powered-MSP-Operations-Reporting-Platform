"""The two Autopilot pages, against the real application and a real database.

What is tested is what the pages are for and what they must never do: that a
franchisee is refused every page and every endpoint; that the labelling task
carries the ticket and **nothing** of the agent's answer or the ledger's
outcome, asserted by inspecting the response's own keys rather than by reading
the template; that an answer is appended and an undo is a withdrawal, both
kept; that a sheet filled in before the page existed imports once; that the
scoring reads those tables; and that the results router holds no state-changing
route at all.

No vendor and no model: a run is made by the shadow test's own fake, which
answers from a canned list.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.dependencies import api_routes
from app.autopilot.gate import build_gate, forward_state
from app.autopilot.labelling import group_judgements, origin_labels
from app.autopilot.shadow.scoring import ThresholdRule, joined_rows
from app.models import AuditEventType, OrganisationKind, Role
from app.models.labelling import LabellingAnswer, LabellingItem, LabellingSet
from app.models.shadow import ShadowRun
from app.settings import Settings
from tests.api.conftest import Make, audit_rows, sign_in
from tests.api.test_autopilot_shadow import FakeModel, _run, _world, answer, at, outcome

pytestmark = pytest.mark.usefixtures("clean")

#: Every field a labeller must not be shown. The agent's answer and the three
#: columns of the ledger that *are* the answer: showing any of them turns a
#: judgement into an agreement.
FORBIDDEN_KEYS = {
    "origin",
    "origin_confidence",
    "issue_type",
    "issue_type_raw",
    "issue_type_label",
    "issue_type_confidence",
    "urgency",
    "urgency_confidence",
    "confidence",
    "prediction",
    "predicted",
    "priority",
    "priority_label",
    "assignee",
    "resolver",
    "resolver_name",
    "group_with",
    "path",
}


def _keys(payload: Any) -> set[str]:
    """Every key anywhere in a payload, however deep."""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(payload, list):
        for one in payload:
            found |= _keys(one)
    return found


async def _world_with_a_run(
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    *,
    tickets: int = 6,
) -> tuple[uuid.UUID, uuid.UUID]:
    """One client, one site, a few completed tickets and a run over them."""
    client_id, site_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        for number in range(1, tickets + 1):
            session.add(
                outcome(
                    number,
                    client_id=client_id,
                    site_id=site_id,
                    title=f"Outlook will not open for user {number}",
                    created_at_vendor=at(10 + number % 5),
                )
            )
        await session.commit()
    result = await _run(
        session_maker,
        client_id,
        # Every ticket called `alert`, so the labelled sample has something for
        # the origin criterion to be precise or imprecise about whichever
        # tickets the stratified draw picks.
        FakeModel(
            [
                answer(origin="alert"),
                answer(origin="alert", issue_type="Printer/Scanner/Copier"),
            ]
        ),
    )
    return client_id, result.run_id


async def _any_client(session: AsyncSession) -> Any:
    from app.models import Client

    return (await session.execute(select(Client).limit(1))).scalar_one()


async def _people(make_org: Make, make_user: Make, client: Any) -> None:
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    theirs = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=client)
    await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER)
    await make_user(theirs, "boss@acme.test", Role.FRANCHISEE_ADMIN)
    await make_user(theirs, "tech@acme.test", Role.FRANCHISEE_TECHNICIAN)
    await make_user(theirs, "viewer@acme.test", Role.FRANCHISEE_VIEWER)


async def _staff(http: httpx.AsyncClient) -> Any:
    signed = await sign_in(http, "engineer@inputiv.test")
    switched = await signed.post("/auth/switch-client", json={"client": "acme-it"})
    assert switched.status_code == 200, switched.text
    return signed


async def _build(signed: Any, run_id: uuid.UUID, kind: str) -> dict[str, Any]:
    made = await signed.post(
        "/autopilot/labelling/sets", json={"run_id": str(run_id), "kind": kind, "size": 4}
    )
    assert made.status_code == 200, made.text
    return dict(made.json())


# --- who may see any of it --------------------------------------------------


def _autopilot_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every method and path the feature exposes, from the application itself.

    Enumerated rather than listed, so a route added later is covered by this
    test without anybody remembering to add it.
    """
    found: list[tuple[str, str]] = []
    # `api_routes` is the application's own enumeration — the one the scope
    # declaration check uses — because this FastAPI keeps included routers
    # nested rather than flattening them onto `app.routes`.
    for route in api_routes(app):
        if not route.path.startswith("/autopilot"):
            continue
        for method in sorted((route.methods or {"GET"}) - {"HEAD", "OPTIONS"}):
            found.append((method, route.path))
    return found


@pytest.mark.parametrize("who", ["boss@acme.test", "tech@acme.test", "viewer@acme.test"])
async def test_a_franchisee_is_refused_every_autopilot_page_and_endpoint(
    http: httpx.AsyncClient,
    app: FastAPI,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
    who: str,
) -> None:
    """403 on all of it, page and JSON alike, for every franchisee role.

    These pages are a measurement of our own agent. A franchisee is not shown a
    friendlier alternative page the way a viewer is shown Reports: there is
    nothing here that is theirs.
    """
    _, run_id = await _world_with_a_run(session_maker, make_client)
    async with session_maker() as session:
        client = await _any_client(session)
    await _people(make_org, make_user, client)
    signed = await sign_in(http, who)

    routes = _autopilot_routes(app)
    assert len(routes) >= 10, routes
    for method, path in routes:
        # Every placeholder filled with something well-formed, so each route is
        # refused on the role rather than on a path that would not parse: a 422
        # for a bad uuid would pass a "not 200" test and prove nothing.
        url = (
            path.replace("{run_id}", str(run_id))
            .replace("{suggestion_id}", str(uuid.uuid4()))
            .replace("{ticket_id}", "70345")
            .replace("{ref}", "70345")
        )
        if "{" not in path and path.endswith("task"):
            url += "?run=" + str(run_id)
        if path.endswith("/queue"):
            url += "?tickets=70345"
        response = await http.request(
            method, url, headers=signed.headers(), json={} if method != "GET" else None
        )
        assert response.status_code == 403, f"{method} {url} -> {response.status_code}"


# --- blind by construction --------------------------------------------------


async def test_the_task_endpoint_carries_no_prediction_or_outcome_field(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """The response's own keys are the test, not the template's wording.

    A labeller shown the agent's answer is agreeing with it. The shape of
    `TaskView` is what refuses one, and this asserts the shape rather than
    trusting the page not to draw a field it was sent.
    """
    _, run_id = await _world_with_a_run(session_maker, make_client)
    async with session_maker() as session:
        client = await _any_client(session)
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    await _build(signed, run_id, "origin")
    await _build(signed, run_id, "groups")

    for kind in ("origin", "groups"):
        found = await http.get(f"/autopilot/labelling/task?run={run_id}&kind={kind}")
        assert found.status_code == 200, found.text
        payload = found.json()
        assert payload["item"] is not None
        leaked = _keys(payload["item"]) & FORBIDDEN_KEYS
        assert not leaked, f"{kind} task leaked {sorted(leaked)}"
        # And what it *does* carry, so the test fails if the four facts shrink
        # to something nobody can judge from.
        ticket = payload["item"]["ticket"] if kind == "origin" else payload["item"]["tickets"][0]
        assert set(ticket) == {
            "ticket_id",
            "ticket_number",
            "title",
            "site",
            "created_at",
            "queue",
        }


# --- the record -------------------------------------------------------------


async def test_an_answer_is_appended_an_undo_withdraws_and_both_are_kept(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Three answers on one item: one, its withdrawal, and a relabel.

    Nothing is updated and nothing is deleted. The latest answer scores and the
    history is how a disputed label is looked into.
    """
    _, run_id = await _world_with_a_run(session_maker, make_client)
    async with session_maker() as session:
        client = await _any_client(session)
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    built = await _build(signed, run_id, "origin")

    first = (await http.get(f"/autopilot/labelling/task?run={run_id}")).json()
    item_id = first["item"]["item_id"]

    answered = await signed.post(
        "/autopilot/labelling/answer",
        json={"item_id": item_id, "answer": "alert", "note": "raised by the monitor"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["progress"]["mine"] == 1

    undone = await signed.post("/autopilot/labelling/undo", json={"set_id": built["set_id"]})
    assert undone.status_code == 200, undone.text
    # Back to the item it withdrew, and it counts as unanswered again.
    assert undone.json()["item"]["item_id"] == item_id
    assert undone.json()["progress"]["mine"] == 0

    again = await signed.post(
        "/autopilot/labelling/answer", json={"item_id": item_id, "answer": "human"}
    )
    assert again.status_code == 200, again.text

    history = (await http.get(f"/autopilot/labelling/history?item={item_id}")).json()
    assert [entry["answer"] for entry in history] == ["alert", "withdrawn", "human"]
    assert [entry["labeller"] for entry in history] == ["engineer@inputiv.test"] * 3

    async with session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(LabellingAnswer).where(LabellingAnswer.item_id == item_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 3
    assert [row.labeller_user_id is not None for row in rows] == [True, True, True]

    # Every write is on the record, and the note itself never is.
    events = await audit_rows(session_maker, event_type=AuditEventType.AUTOPILOT_LABELLED)
    actions = [row.payload.get("action") for row in events if row.payload]
    assert actions == ["set_built", "answered", "withdrawn", "answered"]
    answered_rows = [row for row in events if (row.payload or {}).get("action") == "answered"]
    assert answered_rows[0].payload["note"] is True
    assert "raised by the monitor" not in str(answered_rows[0].payload)


# --- the sheets filled in before the page existed ---------------------------


async def test_a_filled_in_sheet_imports_once(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import is idempotent: a second run adds nothing and says so."""
    import app.autopilot.api.labelling as labelling_api

    _, run_id = await _world_with_a_run(session_maker, make_client)
    async with session_maker() as session:
        client = await _any_client(session)
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    built = await _build(signed, run_id, "origin")

    async with session_maker() as session:
        items = (
            (
                await session.execute(
                    select(LabellingItem)
                    .where(LabellingItem.set_id == uuid.UUID(built["set_id"]))
                    .order_by(LabellingItem.position)
                )
            )
            .scalars()
            .all()
        )
    sheet = tmp_path / "acme-it-origin-labels.csv"
    lines = ["ticket_id,title,origin"]
    for item in items[:3]:
        lines.append(f"{item.subject_id},a title nobody reads,alert")
    # A row for a ticket that is not in the sample, and a blank one.
    lines.append("999999,not in the sample,human")
    lines.append(f"{items[3].subject_id},another,")
    sheet.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(labelling_api, "VAR", tmp_path)

    first = await signed.post(f"/autopilot/labelling/import?run={run_id}")
    assert first.status_code == 200, first.text
    assert first.json()["imported"] == 3
    assert first.json()["unmatched"] == 1

    second = await signed.post(f"/autopilot/labelling/import?run={run_id}")
    assert second.status_code == 200, second.text
    assert second.json()["imported"] == 0
    assert second.json()["already"] == 3

    async with session_maker() as session:
        stored = (await session.execute(select(LabellingAnswer))).scalars().all()
    assert len(stored) == 3
    assert {row.source for row in stored} == {"sheet"}
    # Imported rows have no account behind them: inventing one would be a lie
    # about who judged the ticket.
    assert {row.labeller_user_id for row in stored} == {None}


# --- the scoring reads the tables -------------------------------------------


async def test_the_gate_reads_the_stored_labels_rather_than_a_sheet(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Label through the page, and the gate's origin criterion moves."""
    client_id, run_id = await _world_with_a_run(session_maker, make_client)
    async with session_maker() as session:
        client = await _any_client(session)
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    await _build(signed, run_id, "origin")
    await _build(signed, run_id, "groups")

    async def read_gate() -> dict[str, Any]:
        settings = Settings()
        async with session_maker() as session:
            rows = await joined_rows(session, run_id, client_id=client_id)
            labels = await origin_labels(session, client_id=client_id, run_id=run_id)
            groups = await group_judgements(session, client_id=client_id, run_id=run_id)
            forward = await forward_state(session, client_id=client_id, live=False)
        gate = build_gate(
            rows,
            rule=ThresholdRule.from_settings(settings),
            holdout_days=30,
            labels=labels,
            groups=groups,
            forward=forward,
        )
        return {one.key: one for one in gate.criteria}

    before = await read_gate()
    assert before["origin_precision"].verdict == "not_measured"
    # The reason says what is missing, in the words of what people have done:
    # nothing labelled on past tickets and nothing answered blind on live ones.
    assert "0 past-ticket label(s) and 0 blind check(s)" in before["origin_precision"].detail
    assert before["grouping"].verdict == "not_measured"

    # Answer every origin item the way the agent's own answer would read, so
    # the criterion has something to be measured against.
    while True:
        view = (await http.get(f"/autopilot/labelling/task?run={run_id}")).json()
        if view["finished"]:
            break
        await signed.post(
            "/autopilot/labelling/answer",
            json={"item_id": view["item"]["item_id"], "answer": "alert"},
        )
    while True:
        view = (await http.get(f"/autopilot/labelling/task?run={run_id}&kind=groups")).json()
        if view["finished"]:
            break
        await signed.post(
            "/autopilot/labelling/answer",
            json={"item_id": view["item"]["item_id"], "answer": "same"},
        )

    after = await read_gate()
    assert after["origin_precision"].verdict in {"pass", "fail"}
    assert after["origin_precision"].value is not None
    assert after["grouping"].verdict == "pass"
    assert after["grouping"].value == "100.0%"


# --- read only, provably ----------------------------------------------------


def test_nothing_under_the_results_page_changes_state(app: FastAPI) -> None:
    """No POST, PUT, PATCH or DELETE under `/autopilot/results`, ever.

    A page somebody can start a run from is a page somebody starts a run from
    by accident. The forward-shadow switch is reported as a status and turned
    on by a person editing settings.
    """
    methods = {
        (method, path)
        for method, path in _autopilot_routes(app)
        if path.startswith("/autopilot/results")
    }
    assert methods, "the results routes disappeared"
    assert all(method == "GET" for method, _ in methods), sorted(methods)


async def test_a_set_is_built_once_per_run_and_kind(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Pressing the button twice is the same sample, not a second opinion."""
    _, run_id = await _world_with_a_run(session_maker, make_client)
    async with session_maker() as session:
        client = await _any_client(session)
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    first = await _build(signed, run_id, "origin")
    second = await _build(signed, run_id, "origin")
    assert first["set_id"] == second["set_id"]
    async with session_maker() as session:
        sets = (await session.execute(select(LabellingSet))).scalars().all()
    assert len(sets) == 1


async def test_the_run_detail_is_this_clients_run_only(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Another client's run id is a 404, like every other scoped lookup."""
    _, run_id = await _world_with_a_run(session_maker, make_client)
    other = await make_client("beta-msp", "Beta MSP")
    async with session_maker() as session:
        session.add(
            ShadowRun(
                id=uuid.uuid4(),
                client_id=other.id,
                mode="replay",
                model="claude-sonnet-5",
                prompt_version="shadow-v1",
                config={},
                started_at=dt.datetime(2026, 9, 30, tzinfo=dt.UTC),
            )
        )
        await session.commit()
        theirs = (
            (await session.execute(select(ShadowRun).where(ShadowRun.client_id == other.id)))
            .scalars()
            .one()
        )
        client = await _any_client(session)
    await _people(make_org, make_user, client)
    await _staff(http)
    assert (await http.get(f"/autopilot/results/runs/{run_id}")).status_code == 200
    assert (await http.get(f"/autopilot/results/runs/{theirs.id}")).status_code == 404

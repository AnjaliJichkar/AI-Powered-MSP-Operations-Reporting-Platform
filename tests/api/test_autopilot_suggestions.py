"""Suggestions on a ticket, through HTTP, against a real database.

What is tested is the two things the feature would be worthless without: that a
franchisee receives no suggestion field from anywhere, and that a blind check
cannot be read before it is answered. Then the ordinary ones — below the
threshold nothing is offered, judgements are appended rather than replaced, and
the note an engineer pastes fits the vendor's limit.

No vendor and no model: the rows are written by hand, which is also the only
way to pin a confidence either side of the threshold.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.autopilot.suggestions import NOTE_LIMIT, not_assessed_line
from app.models import AuditEventType, OrganisationKind, Role
from app.models.device import Device
from app.models.outcome import TicketOutcome
from app.models.shadow import ShadowEnrichment, ShadowPrediction, ShadowRun
from app.models.site import Site
from app.models.suggestion import BlindCheck, SuggestionReview, TicketSuggestion
from app.settings import get_settings
from tests.api.conftest import Make, audit_rows, sign_in

pytestmark = pytest.mark.usefixtures("clean")


@pytest.fixture
def shadow_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The forward shadow switched on, which is what makes suggestions exist.

    Off — the default, and what production is until somebody decides otherwise
    — a live client's page says so and offers nothing, which is its own test
    below. The demo tenant does not need this: its suggestions come from a
    simulated pass and are there to be reviewed before the switch is thrown.
    """
    monkeypatch.setenv("AUTOPILOT_SHADOW_LIVE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


#: This test's own "now", not a date somebody typed.
#:
#: These tests go through the HTTP endpoint, which reads the real clock — the
#: findings rules take an injectable `now` and the route does not pass one — so
#: every fixture date here is measured against whenever the suite is run. Fixed
#: at 2 October 2026, the ticket built two hours before it was two hours old on
#: the day it was written and ten days old a fortnight later, which carried it
#: over the seven-day `STALE_DAYS` boundary and added an `alert_stale` finding
#: the assertions did not expect. The test passed for nine days and then failed
#: for a reason that had nothing to do with the code.
#:
#: Read once, at import, so every offset below is a fixed distance from one
#: moment rather than from a clock that moves during the run.
NOW = dt.datetime.now(dt.UTC)
TYPES = ("Email", "Printer/Scanner/Copier", "User Access and Management")

#: Anything that would tell an engineer what the agent thinks before they have
#: said what they think. The blind check is worth exactly as much as this list
#: being absent from the payload.
PREDICTION_KEYS = {
    "issue_type",
    "origin",
    "urgency",
    "confidence",
    "issue_type_confidence",
    "origin_confidence",
    "urgency_confidence",
    "lines",
    "facts",
    "note",
    "group_with",
    "repeat",
}


def _line(card: dict[str, Any], field: str) -> dict[str, Any]:
    """One classification line of the card, by its field."""
    return next(one for one in card["classification"] if one["field"] == field)


def _keys(payload: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(payload, list):
        for one in payload:
            found |= _keys(one)
    return found


async def _world(
    session_maker: async_sessionmaker[AsyncSession], make_client: Make
) -> tuple[Any, uuid.UUID, uuid.UUID]:
    """A client, a site, a few typed tickets and a forward run over them."""
    client = await make_client("acme-it", "Acme IT")
    site_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with session_maker() as session:
        session.add(Site(id=site_id, client_id=client.id, slug="acme-dental", name="Acme Dental"))
        # Flushed before the rows that point at it: left to one batch, the
        # ledger rows go first and the key is not there yet.
        await session.flush()
        for number, label in enumerate(TYPES, start=1):
            session.add(
                TicketOutcome(
                    client_id=client.id,
                    site_id=site_id,
                    ticket_id=str(900 + number),
                    ticket_number=f"T9{number:04d}",
                    title="Outlook will not open",
                    ticket_class="L1",
                    resolver="franchisee",
                    priority_label="Medium",
                    status_label="Complete",
                    queue_label="Help Desk",
                    issue_type_label=label,
                    created_at_vendor=NOW - dt.timedelta(days=5),
                    completed_at=NOW - dt.timedelta(days=4),
                    collected_at=NOW,
                )
            )
        session.add(
            ShadowRun(
                id=run_id,
                client_id=client.id,
                mode="forward",
                model="claude-sonnet-5",
                prompt_version="shadow-v1",
                config={},
                started_at=NOW,
            )
        )
        await session.commit()
    return client, site_id, run_id


async def _suggestion(
    session_maker: async_sessionmaker[AsyncSession],
    client: Any,
    site_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    ticket_id: str = "70345",
    blind: bool = False,
    issue_type: str = "User Access and Management",
    issue_type_confidence: float = 0.91,
    facts: list[dict[str, Any]] | None = None,
    group_with: str | None = None,
    machine: bool = False,
) -> uuid.UUID:
    suggestion_id = uuid.uuid4()
    device_id: uuid.UUID | None = None
    if machine:
        device_id = uuid.uuid4()
        async with session_maker() as session:
            session.add(
                Device(
                    id=device_id,
                    client_id=client.id,
                    site_id=site_id,
                    canonical_name="ACME-WS-014",
                    netbios_key="acme-ws-014",
                    first_seen_at=NOW - dt.timedelta(days=90),
                    last_seen_at=NOW,
                )
            )
            await session.commit()
    async with session_maker() as session:
        prediction = ShadowPrediction(
            run_id=run_id,
            ticket_id=ticket_id,
            site_id=site_id,
            created_at_vendor=NOW - dt.timedelta(hours=2),
            path="model",
            origin="alert",
            origin_confidence=0.95,
            issue_type=issue_type,
            issue_type_confidence=issue_type_confidence,
            urgency="high",
            urgency_confidence=0.84,
            group_with=group_with,
            group_score=0.86 if group_with else None,
        )
        enrichment = ShadowEnrichment(
            run_id=run_id,
            ticket_id=ticket_id,
            site_id=site_id,
            device_id=device_id,
            device_confidence="high" if device_id else None,
            device_method="hostname",
            read_at=NOW - dt.timedelta(hours=2),
            facts=facts if facts is not None else [{"id": "alerts.open", "value": 2}],
            fact_count=1,
            reads=3,
            errors=[],
            not_assessed={"blackpoint": "connector not built"},
        )
        session.add_all([prediction, enrichment])
        await session.flush()
        session.add(
            TicketSuggestion(
                id=suggestion_id,
                client_id=client.id,
                run_id=run_id,
                prediction_id=prediction.id,
                enrichment_id=enrichment.id,
                ticket_id=ticket_id,
                ticket_number=f"T2026{ticket_id}",
                site_id=site_id,
                issue_type=issue_type,
                issue_type_confidence=issue_type_confidence,
                origin="alert",
                origin_confidence=0.95,
                urgency="high",
                urgency_confidence=0.84,
                group_with=group_with,
                group_score=0.86 if group_with else None,
                display_threshold=0.80,
                blind_check=blind,
                created_at=NOW,
            )
        )
        await session.commit()
    return suggestion_id


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


# --- who may see any of it --------------------------------------------------


@pytest.mark.parametrize("who", ["boss@acme.test", "tech@acme.test", "viewer@acme.test"])
async def test_a_franchisee_receives_no_suggestion_field_from_any_route(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
    who: str,
) -> None:
    """403 on every one of them, for every franchisee role.

    Not an empty payload and not a filtered one: the route refuses. A page that
    returned `{"suggestions": {}}` to a franchisee would be one role check away
    from returning the real thing.
    """
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(session_maker, client, site_id, run_id)
    await _people(make_org, make_user, client)
    signed = await sign_in(http, who)

    reads = [
        "/autopilot/suggestions/queue?tickets=70345",
        "/autopilot/suggestions/ticket/70345",
    ]
    for url in reads:
        response = await http.get(url)
        assert response.status_code == 403, f"{url} -> {response.status_code}"
    writes = [
        (
            f"/autopilot/suggestions/{suggestion_id}/review",
            {"field": "issue_type", "verdict": "correct"},
        ),
        (f"/autopilot/suggestions/{suggestion_id}/all-correct", {}),
        (
            f"/autopilot/suggestions/{suggestion_id}/blind",
            {"origin": "alert", "issue_type": "Email"},
        ),
    ]
    for url, body in writes:
        response = await signed.post(url, json=body)
        assert response.status_code == 403, f"{url} -> {response.status_code}"


async def test_the_ticket_queue_itself_carries_no_suggestion_field(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/tickets/list` is what a franchisee reads, and it is untouched.

    The chips ride on a staff-only endpoint of their own rather than on the
    queue's own payload, so there is no field here to leak and no role check
    inside this endpoint to get wrong.
    """
    import app.api.tickets as tickets_api

    client, site_id, run_id = await _world(session_maker, make_client)
    await _suggestion(session_maker, client, site_id, run_id)
    await _people(make_org, make_user, client)

    async def fake_list(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "tickets": [{"ticket_id": "70345", "ticket_number": "T202670345", "title": "A ticket"}],
            "truncated": False,
        }

    # Through monkeypatch, so the stub goes when this test does: left in place
    # it would hand every later test in the process a one-ticket Autotask.
    monkeypatch.setattr(tickets_api.service, "list_open", fake_list)
    await sign_in(http, "boss@acme.test")
    found = await http.get("/tickets/list")
    assert found.status_code == 200, found.text
    assert not (_keys(found.json()) & {"suggestion", "suggestions", "lines", "blind"})


# --- the blind check --------------------------------------------------------


async def test_a_blind_check_returns_the_questions_and_nothing_else(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Nothing of the agent's answer is in the payload before it is answered.

    Asserted on the response's own keys, not on what the page draws: a card
    that fetched the suggestion and hid it would be one "view source" away from
    being worthless.
    """
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(session_maker, client, site_id, run_id, blind=True)
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    found = await http.get("/autopilot/suggestions/ticket/70345")
    assert found.status_code == 200, found.text
    payload = found.json()
    assert payload["blind"]["pending"] is True
    assert payload["blind"]["title"] == "Quick check before suggestions"
    # The classification is the only section that goes. The findings are what
    # the card is for and none of them answers what is being asked.
    assert payload["classification"] == []
    assert "findings" in payload
    # Everything but the questions themselves: the type dropdown has to list
    # this franchisee's labels, or there is nothing to answer with.
    written = json.dumps({key: value for key, value in payload.items() if key != "blind"})
    assert "User Access and Management" not in written, "the suggested type leaked"
    assert "A tool (alert)" not in written, "the suggested origin leaked"
    assert "Alert is" not in written, "a finding phrased from the origin leaked it"
    # The type question offers this franchisee's own labels, which is not a
    # prediction: an engineer has to be able to answer it.
    choices = payload["blind"]["questions"][1]["choices"]
    assert {one["value"] for one in choices} == {*TYPES, "unsure"}

    answered = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/blind",
        json={"origin": "human", "issue_type": "Email"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["classification"], "the classification is revealed once it is answered"

    # And it stays revealed on the next read, so a refresh is not a second ask.
    again = await http.get("/autopilot/suggestions/ticket/70345")
    assert "blind" not in again.json()
    assert again.json()["classification"]

    async with session_maker() as session:
        stored = (await session.execute(select(BlindCheck))).scalars().all()
    assert sorted((row.field, row.answer) for row in stored) == [
        ("issue_type", "Email"),
        ("origin", "human"),
    ]
    assert all(row.reviewer_user_id is not None for row in stored)
    assert all(row.revealed for row in stored)


async def test_a_blind_answer_outside_the_franchisees_labels_is_refused(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(session_maker, client, site_id, run_id, blind=True)
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    refused = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/blind",
        json={"origin": "alert", "issue_type": "Something They Do Not Use"},
    )
    assert refused.status_code == 422


# --- the threshold ----------------------------------------------------------


async def test_a_suggestion_under_the_threshold_is_never_returned(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Quiet, with the card saying so — and no chip in the queue at all."""
    client, site_id, run_id = await _world(session_maker, make_client)
    await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        ticket_id="70999",
        issue_type_confidence=0.41,
    )
    await _people(make_org, make_user, client)
    await _staff(http)

    card = (await http.get("/autopilot/suggestions/ticket/70999")).json()
    assert [line["field"] for line in card["classification"]] == ["origin", "urgency"]
    assert card["quiet"] is False  # other fields did clear it
    assert "User Access" not in card["note"]

    # And the queue's column is the findings, not the classification: Autotask
    # already shows a type beside every row. The display threshold is about
    # what the agent claims, and a finding is not a claim of the agent's — so
    # a ticket it was too unsure to type still shows what the tools said.
    queue = (await http.get("/autopilot/suggestions/queue?tickets=70999")).json()
    assert [one["kind"] for one in queue["suggestions"]["70999"]["findings"]] == ["open_alerts"]


async def test_a_ticket_the_agent_is_quiet_about_has_no_chip_and_needs_no_review(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    client, _, run_id = await _world(session_maker, make_client)
    async with session_maker() as session:
        session.add(
            TicketSuggestion(
                id=uuid.uuid4(),
                client_id=client.id,
                run_id=run_id,
                ticket_id="70111",
                ticket_number="T70111",
                issue_type="unsure",
                issue_type_confidence=0.3,
                origin="unsure",
                origin_confidence=0.4,
                urgency=None,
                display_threshold=0.80,
                blind_check=False,
                created_at=NOW,
            )
        )
        await session.commit()
    await _people(make_org, make_user, client)
    await _staff(http)

    queue = (await http.get("/autopilot/suggestions/queue?tickets=70111")).json()
    assert queue["suggestions"] == {}
    assert queue["needs_review"] == 0
    card = (await http.get("/autopilot/suggestions/ticket/70111")).json()
    assert card["quiet"] is True
    assert card["classification"] == []
    assert card["findings"] == []
    assert card["next_step"] is None


# --- judgements -------------------------------------------------------------


async def test_a_review_is_appended_and_the_latest_one_counts(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(session_maker, client, site_id, run_id)
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    first = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "issue_type", "verdict": "wrong"},
    )
    assert first.status_code == 200, first.text
    assert _line(first.json(), "issue_type")["review"]["verdict"] == "wrong"

    second = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "issue_type", "verdict": "changed", "corrected_value": "Email"},
    )
    assert second.status_code == 200, second.text
    current = _line(second.json(), "issue_type")["review"]
    assert current["verdict"] == "changed"
    assert current["corrected_value"] == "Email"
    assert current["by"] == "engineer@inputiv.test"
    assert len(second.json()["history"]) == 2

    async with session_maker() as session:
        rows = (await session.execute(select(SuggestionReview))).scalars().all()
    assert len(rows) == 2, "a change of mind is a new row, not an edit"
    assert all(row.reviewer_user_id is not None for row in rows)

    events = await audit_rows(
        session_maker, event_type=AuditEventType.AUTOPILOT_SUGGESTION_REVIEWED
    )
    assert [row.payload["action"] for row in events] == ["reviewed", "reviewed"]


async def test_changing_a_field_to_a_value_the_franchisee_does_not_use_is_refused(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """A correction filed under a label they do not have is not a correction."""
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(session_maker, client, site_id, run_id)
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    refused = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "issue_type", "verdict": "changed", "corrected_value": "Nope"},
    )
    assert refused.status_code == 422
    bare = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "issue_type", "verdict": "changed"},
    )
    assert bare.status_code == 422


async def test_all_correct_marks_what_was_shown_and_only_that(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """The fields come from the suggestion, not from the request, so one click
    cannot approve something the engineer was never shown."""
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(
        session_maker, client, site_id, run_id, issue_type_confidence=0.55
    )
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    done = await signed.post(f"/autopilot/suggestions/{suggestion_id}/all-correct", json={})
    assert done.status_code == 200, done.text
    assert sorted(line["field"] for line in done.json()["classification"] if line["review"]) == [
        "origin",
        "urgency",
    ]

    events = await audit_rows(
        session_maker, event_type=AuditEventType.AUTOPILOT_SUGGESTION_REVIEWED
    )
    assert events[-1].payload["fields"] == ["origin", "urgency"]


async def test_the_note_fits_the_vendors_limit_and_says_nothing_was_written(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    client, site_id, run_id = await _world(session_maker, make_client)
    await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        facts=[
            {"id": "alerts.open", "source": "list_open_alerts", "value": 3},
            {"id": "device.needs_reboot", "source": "get_device", "value": True},
        ],
    )
    await _people(make_org, make_user, client)
    await _staff(http)
    card = (await http.get("/autopilot/suggestions/ticket/70345")).json()
    note = card["note"]
    assert len(note) <= NOTE_LIMIT
    assert "3 open alerts on this machine [NinjaOne]" in note
    assert "Issue type: User Access and Management" in note
    assert note.index("open alerts") < note.index("Issue type:")
    assert "Nothing here was applied to the ticket automatically." in note


async def test_another_clients_suggestion_is_a_404(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(session_maker, client, site_id, run_id)
    other = await make_client("beta-msp", "Beta MSP")
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    await signed.post("/auth/switch-client", json={"client": other.slug})
    refused = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "origin", "verdict": "correct"},
    )
    assert refused.status_code == 404


async def test_with_the_switch_off_a_live_client_is_told_so_and_offered_nothing(
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """The default, and what production is until a person decides otherwise.

    Not an empty card that looks like a quiet agent: a sentence naming the
    setting, so the reader knows the difference between "nothing to say" and
    "nothing is running".
    """
    client, site_id, run_id = await _world(session_maker, make_client)
    await _suggestion(session_maker, client, site_id, run_id)
    await _people(make_org, make_user, client)
    await _staff(http)

    card = (await http.get("/autopilot/suggestions/ticket/70345")).json()
    assert card["live"] is False
    assert "AUTOPILOT_SHADOW_LIVE" in card["detail"]
    assert not (_keys(card) & PREDICTION_KEYS)

    queue = (await http.get("/autopilot/suggestions/queue?tickets=70345")).json()
    assert queue == {"live": False, "detail": card["detail"], "suggestions": {}, "needs_review": 0}


# --- the redesigned card ----------------------------------------------------


async def test_the_card_shows_four_findings_and_offers_the_rest(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """The cut is the page's: the payload carries them all, ranked, and says
    where the fold is. Four, because the fifth thing is never why somebody
    opened the ticket."""
    client, site_id, run_id = await _world(session_maker, make_client)
    await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        facts=[
            {
                "id": "device.last_contact",
                "source": "get_device",
                "value": (NOW - dt.timedelta(days=10)).isoformat(),
            },
            {"id": "alerts.open", "source": "list_open_alerts", "value": 4},
            {"id": "findings.known_exploited", "source": "get_asset_vulnerabilities", "value": 2},
            {"id": "findings.critical", "source": "get_asset_vulnerabilities", "value": 9},
            {"id": "device.needs_reboot", "source": "get_device", "value": True},
            {"id": "patches.failed", "source": "list_patch_installs", "value": 3},
        ],
    )
    await _people(make_org, make_user, client)
    await _staff(http)

    card = (await http.get("/autopilot/suggestions/ticket/70345")).json()
    kinds = [one["kind"] for one in card["findings"]]
    assert kinds[0] == "device_offline", "the machine being absent leads"
    assert len(kinds) > card["max_shown"] == 4
    assert [one["rank"] for one in card["findings"]] == sorted(
        one["rank"] for one in card["findings"]
    )
    assert card["next_step"]["because"] == "device_offline"


async def test_a_finding_is_useful_or_not_and_a_classification_is_not(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Two vocabularies, kept apart. "Was this correct?" about "machine offline
    for 10 days" gets "yes, and I did not need telling"."""
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        facts=[{"id": "device.needs_reboot", "source": "get_device", "value": True}],
    )
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    done = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "finding:pending_reboot", "verdict": "useful"},
    )
    assert done.status_code == 200, done.text
    finding = next(one for one in done.json()["findings"] if one["kind"] == "pending_reboot")
    assert finding["review"]["verdict"] == "useful"

    refused = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "finding:pending_reboot", "verdict": "correct"},
    )
    assert refused.status_code == 422
    also = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "issue_type", "verdict": "useful"},
    )
    assert also.status_code == 422

    async with session_maker() as session:
        rows = (await session.execute(select(SuggestionReview))).scalars().all()
    assert [(row.field, row.verdict) for row in rows] == [("finding:pending_reboot", "useful")]


async def test_the_card_and_the_next_step_can_be_judged_too(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        facts=[
            {
                "id": "device.last_contact",
                "source": "get_device",
                "value": (NOW - dt.timedelta(days=10)).isoformat(),
            }
        ],
    )
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    for field in ("next_step", "card"):
        done = await signed.post(
            f"/autopilot/suggestions/{suggestion_id}/review",
            json={"field": field, "verdict": "not_useful"},
        )
        assert done.status_code == 200, done.text
    card = (await http.get("/autopilot/suggestions/ticket/70345")).json()
    assert card["next_step"]["review"]["verdict"] == "not_useful"
    assert card["card_review"]["verdict"] == "not_useful"
    assert card["last_actioned"]["by"] == "engineer@inputiv.test"


async def test_changing_a_classification_keeps_the_old_value_beside_the_new(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """The card draws "Low Normal, changed" from this: what it said, what it
    should have said, and that somebody said so."""
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(session_maker, client, site_id, run_id)
    await _people(make_org, make_user, client)
    signed = await _staff(http)
    done = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "urgency", "verdict": "changed", "corrected_value": "normal"},
    )
    assert done.status_code == 200, done.text
    line = _line(done.json(), "urgency")
    assert line["value"] == "high", "the agent's own answer is still there to strike through"
    assert line["review"]["corrected_value"] == "normal"


async def test_the_queue_sends_the_top_findings_and_counts_the_warnings(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Two chips at most, then "+N" — and the third filter pill is the count of
    rows carrying a warning."""
    client, site_id, run_id = await _world(session_maker, make_client)
    await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        ticket_id="70345",
        facts=[
            {
                "id": "device.last_contact",
                "source": "get_device",
                "value": (NOW - dt.timedelta(days=10)).isoformat(),
            },
            {"id": "alerts.open", "source": "list_open_alerts", "value": 4},
            {"id": "device.needs_reboot", "source": "get_device", "value": True},
        ],
    )
    await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        ticket_id="70777",
        facts=[{"id": "device.needs_reboot", "source": "get_device", "value": True}],
    )
    await _people(make_org, make_user, client)
    await _staff(http)

    queue = (await http.get("/autopilot/suggestions/queue?tickets=70345,70777")).json()
    loud = queue["suggestions"]["70345"]
    assert [one["kind"] for one in loud["findings"]] == ["device_offline", "open_alerts"]
    assert loud["more"] == 1
    assert loud["warning"] is True
    quiet = queue["suggestions"]["70777"]
    assert [one["kind"] for one in quiet["findings"]] == ["pending_reboot"]
    assert quiet["warning"] is False
    assert queue["warnings"] == 1
    assert queue["needs_review"] == 2


# --- the two tallies, which do not mix ---------------------------------------


async def test_the_results_page_survives_a_client_with_mixed_reviews(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """A classification verdict, a finding verdict and a card thumb on one
    client, and the results page still renders.

    It did not: every review went into the classification tally, which has
    `correct`, `changed` and `wrong` on it and nowhere to put `useful`. The
    page 500ed with an AttributeError the moment anybody pressed Useful.
    """
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        facts=[{"id": "device.needs_reboot", "source": "get_device", "value": True}],
    )
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    for body in (
        {"field": "issue_type", "verdict": "correct"},
        {"field": "urgency", "verdict": "changed", "corrected_value": "normal"},
        {"field": "finding:pending_reboot", "verdict": "useful"},
        {"field": "next_step", "verdict": "not_useful"},
        {"field": "card", "verdict": "useful"},
    ):
        done = await signed.post(f"/autopilot/suggestions/{suggestion_id}/review", json=body)
        assert done.status_code == 200, done.text

    page = await http.get(f"/autopilot/results/runs/{run_id}")
    assert page.status_code == 200, page.text
    payload = page.json()

    # The classification tally holds classification fields and nothing else.
    by_field = payload["live_reviews"]["by_field"]
    assert sorted(by_field) == ["issue_type", "urgency"]
    assert by_field["issue_type"]["correct"] == 1
    assert by_field["urgency"]["changed"] == 1

    # And the findings tally holds the rest, counted its own way.
    findings = payload["findings"]
    assert findings["pending_reboot"]["useful"] == 1
    assert findings["pending_reboot"]["shown"] == 1
    assert findings["next_step"]["not_useful"] == 1
    assert findings["card"]["useful"] == 1


# --- what "reviewed" means -------------------------------------------------


async def test_a_ticket_waits_on_its_classification_and_not_on_its_findings(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """The required lines are the agent's reading of the ticket.

    A finding's thumbs says whether that line was worth the space, which is
    feedback on us and not a thing the ticket waits on; the classification is
    what somebody has to agree with or correct. So a card with every finding
    judged and a type unanswered is still in Needs review, and the reverse is
    not.
    """
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        machine=True,
        facts=[
            {"id": "device.needs_reboot", "source": "get_device", "value": True},
            {"id": "alerts.open", "source": "list_open_alerts", "value": 2},
        ],
    )
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    first = (await http.get("/autopilot/suggestions/queue?tickets=70345")).json()
    assert first["needs_review"] == 1
    assert first["suggestions"]["70345"]["reviewed"] is False

    for body in (
        {"field": "finding:pending_reboot", "verdict": "useful"},
        {"field": "finding:open_alerts", "verdict": "not_useful"},
    ):
        done = await signed.post(f"/autopilot/suggestions/{suggestion_id}/review", json=body)
        assert done.status_code == 200, done.text
    both = (await http.get("/autopilot/suggestions/queue?tickets=70345")).json()
    assert both["needs_review"] == 1, "a judged finding is not a reviewed ticket"
    assert both["suggestions"]["70345"]["reviewed"] is False

    for body in (
        {"field": "machine", "verdict": "correct"},
        {"field": "issue_type", "verdict": "correct"},
        {"field": "origin", "verdict": "correct"},
        {"field": "urgency", "verdict": "changed", "corrected_value": "normal"},
    ):
        done = await signed.post(f"/autopilot/suggestions/{suggestion_id}/review", json=body)
        assert done.status_code == 200, done.text

    # Changed, not correct: a corrected line is an answered line.
    finished = (await http.get("/autopilot/suggestions/queue?tickets=70345")).json()
    assert finished["needs_review"] == 0
    assert finished["suggestions"]["70345"]["reviewed"] is True


async def test_the_cards_own_thumbs_finishes_the_review(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Somebody who read the card and said what they made of it has reviewed
    it. Asking them to then accept four lines one at a time is how a review
    step becomes a thing people learn to skip."""
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(session_maker, client, site_id, run_id, machine=True)
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    done = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "card", "verdict": "not_useful"},
    )
    assert done.status_code == 200, done.text
    queue = (await http.get("/autopilot/suggestions/queue?tickets=70345")).json()
    assert queue["needs_review"] == 0
    assert queue["suggestions"]["70345"]["reviewed"] is True
    # The lines are still unanswered, and the header still says so: finishing
    # with a card is not the same as having confirmed every line on it.
    assert done.json()["to_review"] == 4


async def test_the_header_count_is_the_required_lines_only(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """ "(N to review)" is the number of ✓ Accept buttons somebody can still
    see: the machine and the four lines of ticket information, never the
    findings or the next step."""
    client, site_id, run_id = await _world(session_maker, make_client)
    await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        machine=True,
        group_with="901",
        facts=[
            {
                "id": "device.last_contact",
                "source": "get_device",
                "value": (NOW - dt.timedelta(days=10)).isoformat(),
            },
            {"id": "alerts.open", "source": "list_open_alerts", "value": 4},
            {"id": "device.needs_reboot", "source": "get_device", "value": True},
        ],
    )
    await _people(make_org, make_user, client)
    await _staff(http)

    card = (await http.get("/autopilot/suggestions/ticket/70345")).json()
    assert len(card["findings"]) >= 2, "there is something unanswered to leave out"
    assert card["next_step"] is not None
    # machine, issue type, raised by, urgency, repeat of.
    assert card["to_review"] == 5
    assert card["review_required"] == 5
    assert card["reviewed"] is False


async def test_a_card_with_nothing_to_confirm_is_not_waiting_on_anybody(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Findings and no confident classification: there is nothing for anybody
    to accept, so the ticket is not held in Needs review and the header says
    "nothing to confirm" rather than "all reviewed"."""
    client, site_id, run_id = await _world(session_maker, make_client)
    await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        issue_type_confidence=0.41,
        facts=[{"id": "device.needs_reboot", "source": "get_device", "value": True}],
    )
    await _people(make_org, make_user, client)
    await _staff(http)

    card = (await http.get("/autopilot/suggestions/ticket/70345")).json()
    assert [one["field"] for one in card["classification"]] == ["origin", "urgency"]
    queue = (await http.get("/autopilot/suggestions/queue?tickets=70345")).json()
    assert queue["needs_review"] == 1, "origin and urgency are above the threshold"

    # And with nothing above it at all.
    await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        ticket_id="70777",
        issue_type_confidence=0.41,
        facts=[{"id": "device.needs_reboot", "source": "get_device", "value": True}],
    )
    async with session_maker() as session:
        row = (
            await session.execute(
                select(TicketSuggestion).where(TicketSuggestion.ticket_id == "70777")
            )
        ).scalar_one()
        row.origin_confidence = 0.4
        row.urgency_confidence = 0.4
        await session.commit()

    quiet = (await http.get("/autopilot/suggestions/ticket/70777")).json()
    assert quiet["classification"] == []
    assert quiet["review_required"] == 0
    assert quiet["to_review"] == 0
    assert quiet["reviewed"] is True
    assert quiet["findings"], "the findings are still there to read"
    both = (await http.get("/autopilot/suggestions/queue?tickets=70345,70777")).json()
    assert both["suggestions"]["70777"]["reviewed"] is True
    assert both["needs_review"] == 1


# --- all correct ------------------------------------------------------------


async def test_all_correct_accepts_every_line_that_is_still_unanswered(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """One click for the common case, and nothing else touched.

    A line somebody has already corrected is left as it was — overwriting a
    correction with "correct" would lose the only thing on the card worth more
    than agreement — and a finding is not swept up in it either.
    """
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(
        session_maker,
        client,
        site_id,
        run_id,
        machine=True,
        facts=[{"id": "device.needs_reboot", "source": "get_device", "value": True}],
    )
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    changed = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "urgency", "verdict": "changed", "corrected_value": "normal"},
    )
    assert changed.status_code == 200, changed.text

    done = await signed.post(f"/autopilot/suggestions/{suggestion_id}/all-correct", json={})
    assert done.status_code == 200, done.text
    card = done.json()
    assert card["to_review"] == 0
    assert card["reviewed"] is True
    assert _line(card, "urgency")["review"]["verdict"] == "changed", "a correction stands"
    assert _line(card, "issue_type")["review"]["verdict"] == "correct"
    assert card["machine"]["review"]["verdict"] == "correct"
    assert card["findings"][0]["review"] is None, "a finding is not swept up in it"

    async with session_maker() as session:
        rows = (
            (await session.execute(select(SuggestionReview).order_by(SuggestionReview.created_at)))
            .scalars()
            .all()
        )
    assert [(row.field, row.verdict) for row in rows] == [
        ("urgency", "changed"),
        ("machine", "correct"),
        ("issue_type", "correct"),
        ("origin", "correct"),
    ]
    logged = await audit_rows(
        session_maker, event_type=AuditEventType.AUTOPILOT_SUGGESTION_REVIEWED
    )
    assert [row.payload["fields"] for row in logged if row.payload["action"] == "all_correct"] == [
        ["machine", "issue_type", "origin"]
    ]


async def test_all_correct_cannot_accept_what_was_never_shown(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """The fields come from the suggestion, not the request: a type below the
    threshold was never offered and cannot be approved."""
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(
        session_maker, client, site_id, run_id, issue_type_confidence=0.42
    )
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    done = await signed.post(f"/autopilot/suggestions/{suggestion_id}/all-correct", json={})
    assert done.status_code == 200, done.text
    async with session_maker() as session:
        rows = (await session.execute(select(SuggestionReview))).scalars().all()
    assert sorted(row.field for row in rows) == ["origin", "urgency"]


async def test_the_machine_is_accepted_or_rejected_rather_than_useful(
    shadow_on: None,
    http: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    make_client: Make,
    make_org: Make,
    make_user: Make,
) -> None:
    """Which machine a ticket is about is a claim that is right or wrong, so it
    takes the classification's vocabulary and is counted in its tally."""
    client, site_id, run_id = await _world(session_maker, make_client)
    suggestion_id = await _suggestion(session_maker, client, site_id, run_id)
    await _people(make_org, make_user, client)
    signed = await _staff(http)

    done = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "machine", "verdict": "correct"},
    )
    assert done.status_code == 200, done.text
    refused = await signed.post(
        f"/autopilot/suggestions/{suggestion_id}/review",
        json={"field": "machine", "verdict": "useful"},
    )
    assert refused.status_code == 422

    page = (await http.get(f"/autopilot/results/runs/{run_id}")).json()
    assert page["live_reviews"]["by_field"]["machine"]["correct"] == 1
    assert "machine" not in page["findings"]


# --- the "Not asked" line ---------------------------------------------------


def test_the_not_asked_line_names_the_vendor_and_groups_one_reason_once() -> None:
    """The line an engineer reads under the card, in the words it uses.

    A tool the customer does not own and a tool with nothing to say about this
    machine are two different claims, so they are two clauses; two tools with
    the same reason share one. The vendor's name is printed, never the
    connector's key — `connectsecure` on a card is an internal spelling."""
    line = not_assessed_line(
        ShadowEnrichment(
            not_assessed={
                "cove": "not set up for this customer",
                "connectsecure": "no active vendor link for this machine",
                "bitdefender": "no active vendor link for this machine",
            }
        )
    )
    assert line == (
        "Not asked: Bitdefender, ConnectSecure (no active vendor link for this machine); "
        "Cove (not set up for this customer)."
    )


def test_a_customer_without_cove_is_never_told_the_connector_is_not_built() -> None:
    """Cove's connector has existed since the Backup column landed. The card
    saying otherwise was a sentence about us where the fact is about them."""
    line = not_assessed_line(
        ShadowEnrichment(not_assessed={"cove": "not set up for this customer"})
    )
    assert line == "Not asked: Cove (not set up for this customer)."
    assert "connector not built" not in line


def test_nothing_unasked_is_no_line_at_all() -> None:
    """A bland line on every card is one people learn to skip."""
    assert not_assessed_line(ShadowEnrichment(not_assessed={})) is None
    assert not_assessed_line(None) is None

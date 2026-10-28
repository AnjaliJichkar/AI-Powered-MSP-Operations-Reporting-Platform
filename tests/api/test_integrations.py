"""Integrations: which tools a client is on, and when each last answered.

The page reports the record rather than checking anything, so what matters
here is that it reports the record of the *right* client: a connector another
franchisee is set up on is not one of this franchisee's integrations, and a
read somebody else's session made is not evidence about this one's tools.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.integrations import (
    ERROR_WORDS,
    LISTED,
    PURPOSE,
    READS,
    RECENT_PER_TOOL,
    ROLE,
    SCOPE,
    SETUP,
    WINDOW_DAYS,
)
from app.models import OrganisationKind, Role
from app.models.audit_log import AuditLog
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface
from tests.api.conftest import Make, sign_in

pytestmark = pytest.mark.usefixtures("clean")


@pytest.fixture
async def world(make_client: Make, make_org: Make, make_user: Make) -> dict[str, Any]:
    acme = await make_client("acme-it", "Acme IT")
    other = await make_client("other-it", "Other IT")
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    theirs = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=acme, name="Acme")
    return {
        "acme": acme,
        "other": other,
        "staff": await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER),
        "admin": await make_user(theirs, "admin@acme.test", Role.FRANCHISEE_ADMIN),
    }


async def _read(
    session_maker: async_sessionmaker[AsyncSession],
    client_id: Any,
    connector: ConnectorName,
    outcome: AuditOutcome,
    *,
    days_ago: int = 1,
    error_type: str | None = None,
    interface: Interface = Interface.API,
) -> None:
    """One audit row, as a vendor read would have written it."""
    import uuid

    async with session_maker() as session:
        session.add(
            AuditLog(
                request_id=uuid.uuid4(),
                event_type=AuditEventType.TOOL_CALL,
                outcome=outcome,
                actor_id="engineer@inputiv.test",
                interface=interface,
                client_id=client_id,
                connector_name=connector,
                tool_name="list_devices",
                error_type=error_type,
                created_at=dt.datetime.now(dt.UTC).replace(tzinfo=None)
                - dt.timedelta(days=days_ago),
            )
        )
        await session.commit()


async def _sign_in_on_acme(http: httpx.AsyncClient) -> None:
    staff = await sign_in(http, "engineer@inputiv.test")
    await staff.post("/auth/switch-client", json={"client": "acme-it"})


async def test_every_connector_is_listed_whether_or_not_the_client_has_it(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """ "Not set up" is an answer. A list that leaves out what you do not have
    cannot be read as a list of what there is."""
    await _sign_in_on_acme(http)
    body = (await http.get("/integrations/list")).json()
    assert [row["key"] for row in body["integrations"]] == [c.value for c in LISTED]
    assert body["window_days"] == WINDOW_DAYS


async def test_a_successful_read_is_reported_as_the_last_answer(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _read(session_maker, world["acme"].id, ConnectorName.NINJAONE, AuditOutcome.SUCCESS)
    await _sign_in_on_acme(http)
    rows = {r["key"]: r for r in (await http.get("/integrations/list")).json()["integrations"]}
    assert rows["ninjaone"]["last_ok"] is not None
    assert rows["ninjaone"]["last_failed"] is None


async def test_a_failure_is_reported_with_its_kind(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _read(
        session_maker,
        world["acme"].id,
        ConnectorName.BITDEFENDER,
        AuditOutcome.ERROR,
        error_type="RateLimited",
    )
    await _sign_in_on_acme(http)
    rows = {r["key"]: r for r in (await http.get("/integrations/list")).json()["integrations"]}
    assert rows["bitdefender"]["last_failed"] is not None
    assert rows["bitdefender"]["last_error"] == "RateLimited"


async def test_another_clients_reads_are_not_this_clients_evidence(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Hard rule 1 on a page that only reads the record."""
    await _read(session_maker, world["other"].id, ConnectorName.NINJAONE, AuditOutcome.SUCCESS)
    await _sign_in_on_acme(http)
    rows = {r["key"]: r for r in (await http.get("/integrations/list")).json()["integrations"]}
    assert rows["ninjaone"]["last_ok"] is None


async def test_a_read_older_than_the_window_is_not_an_answer(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A success in August is not evidence that something works today."""
    await _read(
        session_maker,
        world["acme"].id,
        ConnectorName.COVE,
        AuditOutcome.SUCCESS,
        days_ago=WINDOW_DAYS + 5,
    )
    await _sign_in_on_acme(http)
    rows = {r["key"]: r for r in (await http.get("/integrations/list")).json()["integrations"]}
    assert rows["cove"]["last_ok"] is None


async def test_the_selection_checks_own_runs_are_not_counted(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """`interface = 'eval'` is the harness, which no real entry point can send;
    every usage figure in the product leaves it out and so does this one."""
    await _read(
        session_maker,
        world["acme"].id,
        ConnectorName.AUTOTASK,
        AuditOutcome.SUCCESS,
        interface=Interface.EVAL,
    )
    await _sign_in_on_acme(http)
    rows = {r["key"]: r for r in (await http.get("/integrations/list")).json()["integrations"]}
    assert rows["autotask"]["last_ok"] is None


async def test_a_session_with_no_client_is_refused_rather_than_answered(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    await sign_in(http, "engineer@inputiv.test")
    assert (await http.get("/integrations/list")).status_code == 409


async def test_every_listed_connector_has_the_words_its_card_needs() -> None:
    """A card with no sentence, no chips or no scope is a card that says the
    tool exists and nothing else. A connector added to `LISTED` without them
    fails here rather than drawing a blank."""
    for connector in LISTED:
        assert ROLE[connector]
        assert PURPOSE[connector].endswith(".")
        assert READS[connector], connector
        assert SCOPE[connector].endswith(".")
        how, command, docs = SETUP[connector]
        assert how.endswith(".")
        assert command and docs.startswith("docs/")


async def test_a_failure_is_reported_in_words_with_no_class_name_in_them(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """`UpstreamError` on a page is a stack trace with the stack taken off."""
    await _read(
        session_maker,
        world["acme"].id,
        ConnectorName.AUTOTASK,
        AuditOutcome.ERROR,
        error_type="UpstreamError",
    )
    await _sign_in_on_acme(http)
    rows = {r["key"]: r for r in (await http.get("/integrations/list")).json()["integrations"]}
    words = rows["autotask"]["last_error_words"]
    assert words == "Autotask did not answer."
    assert not any(name in words for name in ERROR_WORDS)


async def test_an_unknown_failure_still_gets_a_sentence(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A class nobody has mapped yet is a sentence about the read, never the
    class: a page that falls back to the raw name has no fallback at all."""
    await _read(
        session_maker,
        world["acme"].id,
        ConnectorName.COVE,
        AuditOutcome.ERROR,
        error_type="SomethingNobodyMapped",
    )
    await _sign_in_on_acme(http)
    rows = {r["key"]: r for r in (await http.get("/integrations/list")).json()["integrations"]}
    assert rows["cove"]["last_error_words"] == "The read of Cove did not work."
    assert "SomethingNobodyMapped" not in rows["cove"]["last_error_words"]


async def test_the_class_name_goes_to_staff_and_not_to_the_franchisee(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """The name is what an engineer reading the log wants. It is not withheld
    out of secrecy — it is withheld because it is not an answer to anybody who
    cannot act on it, and because the sentence beside it already is one."""
    await _read(
        session_maker,
        world["acme"].id,
        ConnectorName.BITDEFENDER,
        AuditOutcome.ERROR,
        error_type="ArgumentRejected",
    )

    await _sign_in_on_acme(http)
    staff = (await http.get("/integrations/list")).json()
    row = {r["key"]: r for r in staff["integrations"]}["bitdefender"]
    assert staff["staff"] is True
    assert row["last_error"] == "ArgumentRejected"
    assert row["recent"][0]["error"] == "ArgumentRejected"
    assert row["setup"]["command"]

    await sign_in(http, "admin@acme.test")
    body = (await http.get("/integrations/list")).json()
    their_row = {r["key"]: r for r in body["integrations"]}["bitdefender"]
    assert body["staff"] is False
    assert their_row["last_error"] is None
    assert their_row["recent"][0]["error"] is None
    assert their_row["recent"][0]["words"] == "Bitdefender rejected a request."
    # A command they cannot run is noise on their screen.
    assert their_row["setup"]["command"] is None
    assert their_row["setup"]["how"]


async def test_the_drawers_read_list_is_capped_and_newest_first(
    http: httpx.AsyncClient, world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    for day in range(RECENT_PER_TOOL + 4):
        await _read(
            session_maker,
            world["acme"].id,
            ConnectorName.NINJAONE,
            AuditOutcome.SUCCESS,
            days_ago=day + 1,
        )
    await _sign_in_on_acme(http)
    rows = {r["key"]: r for r in (await http.get("/integrations/list")).json()["integrations"]}
    recent = rows["ninjaone"]["recent"]
    assert len(recent) == RECENT_PER_TOOL
    assert recent == sorted(recent, key=lambda r: r["at"], reverse=True)
    assert all(r["ok"] and r["words"] is None for r in recent)

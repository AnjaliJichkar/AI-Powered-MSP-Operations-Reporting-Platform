"""Who may touch what, over HTTP, with real cookies.

The question each of these asks is the one that had no answer before: a caller
who is definitely signed in, and definitely not entitled to this, is refused —
and cannot get round it by naming a different client, because there is nowhere
left to name one.

Two clients exist throughout, each with its own organisation and people, so
"another client's" is a real other tenant and not a fabricated id.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_session
from app.main import create_app
from app.models import AuditEventType, AuditOutcome, OrganisationKind, Role, UserStatus
from app.security.totp import code_for
from app.settings import get_settings
from tests.api.conftest import Make, audit_rows, sign_in

pytestmark = pytest.mark.usefixtures("clean")


@pytest.fixture
async def world(make_client: Make, make_org: Make, make_user: Make) -> dict[str, Any]:
    """Two franchisees with their own people, and Inputiv with ours."""
    acme = await make_client("acme-it", "Acme IT")
    beta = await make_client("beta-msp", "Beta MSP")

    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    acme_org = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=acme)
    beta_org = await make_org(OrganisationKind.FRANCHISEE, "beta-people", client=beta)

    return {
        "acme": acme,
        "beta": beta,
        "staff": await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER),
        "admin": await make_user(inputiv, "admin@inputiv.test", Role.INPUTIV_ADMIN),
        "acme_admin": await make_user(acme_org, "boss@acme.test", Role.FRANCHISEE_ADMIN),
        "acme_tech": await make_user(acme_org, "tech@acme.test", Role.FRANCHISEE_TECHNICIAN),
        "acme_viewer": await make_user(acme_org, "viewer@acme.test", Role.FRANCHISEE_VIEWER),
        "beta_admin": await make_user(beta_org, "boss@beta.test", Role.FRANCHISEE_ADMIN),
    }


# --- a franchisee cannot reach another client, by any route -------------------


async def test_a_franchisee_only_ever_sees_their_own_client(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """Their client is their organisation's, and nothing they send changes it."""
    session = await sign_in(http, "boss@acme.test")

    me = (await http.get("/auth/me")).json()
    assert me["client"]["slug"] == "acme-it"
    assert me["may_switch_client"] is False

    # The switcher shows exactly one client: it is not a way to learn that
    # another franchisee exists.
    clients = (await http.get("/auth/clients")).json()
    assert clients == [{"slug": "acme-it", "name": "Acme IT"}]

    # And switching is refused outright, whatever slug is named.
    refused = await session.post("/auth/switch-client", json={"client": "beta-msp"})
    assert refused.status_code == 403


async def test_a_franchisee_cannot_name_another_client_in_a_body(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """The field is gone, so sending it is an error rather than a no-op.

    This is the difference that matters. A body field that is ignored leaves
    the caller believing they chose something; a 422 tells them they did not.
    """
    session = await sign_in(http, "boss@acme.test")
    beta_id = str(world["beta"].id)

    for url, body in (
        ("/chat", {"message": "hello", "client_id": beta_id}),
        ("/chat", {"message": "hello", "user_id": "someone-else"}),
        ("/chat/stream", {"message": "hello", "client_id": beta_id}),
        ("/conversations", {"client_id": beta_id}),
        ("/feedback", {"audit_log_id": 1, "rating": "up", "user_id": "someone-else"}),
    ):
        response = await session.post(url, json=body)
        assert response.status_code == 422, (url, response.status_code, response.text)
        assert "extra" in response.text.lower() or "forbidden" in response.text.lower()

    # Generating a review is a route this role *may* call, on their own
    # customers, so the body is validated and the extra field is refused in
    # the same words as the rest. There is no path by which naming a client
    # works: it is not a field, so it is not a choice.
    named = await session.post(
        "/reports/tbr", json={"site": "x", "period": "2026-08", "client_id": beta_id}
    )
    assert named.status_code == 422, named.text
    assert "extra" in named.text.lower() or "forbidden" in named.text.lower()

    # On a route this role may not call at all it is a 403 first: whether the
    # body was well formed is not something an outsider finds out. Switching
    # client is the clearest case, because it is the one route whose whole
    # purpose is naming a client and no franchisee may call it.
    blocked = await session.post("/auth/switch-client", json={"client": "beta-it", "x": 1})
    assert blocked.status_code == 403


async def test_a_franchisee_gets_403_or_404_on_everything_of_another_clients(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Chat, threads, reports, sites and the audit trail, from the other side."""
    # Beta's admin creates a thread of their own, to have something real to
    # point Acme's admin at.
    beta = await sign_in(http, "boss@beta.test")
    made = await beta.post("/conversations", json={})
    assert made.status_code == 201
    beta_thread = made.json()["id"]
    assert (await http.get("/auth/me")).json()["client"]["slug"] == "beta-msp"

    # Now Acme's admin, in the same browser.
    acme = await sign_in(http, "boss@acme.test")
    assert (await http.get("/auth/me")).json()["client"]["slug"] == "acme-it"

    # Beta's thread: theirs, and not reachable by id.
    assert (await http.get(f"/conversations/{beta_thread}/messages")).status_code == 403
    assert (await acme.post(f"/conversations/{beta_thread}/archive", json={})).status_code == 403

    # Their own listing shows nothing of Beta's.
    assert (await http.get("/conversations")).json() == []

    # Reports and sites answer for Acme only, and Acme has none.
    assert (await http.get("/reports/tbr")).json() == []
    assert (await http.get("/reports/tbr/sites")).json() == []

    # The audit trail is scoped the same way: Beta's rows are not in it.
    rows = await http.get("/audit")
    assert rows.status_code == 200
    beta_rows = await audit_rows(session_maker, client_id=world["beta"].id)
    assert beta_rows, "beta should have written audit rows"
    assert all(row["id"] not in {r.id for r in beta_rows} for row in rows.json())


async def test_a_viewer_cannot_open_chat_or_the_audit_trail(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """A viewer reads what has been produced; it does not start a model run."""
    viewer = await sign_in(http, "viewer@acme.test")

    assert (await viewer.post("/chat", json={"message": "what is stale?"})).status_code == 403
    assert (await viewer.post("/chat/stream", json={"message": "hi"})).status_code == 403
    assert (await viewer.post("/conversations", json={})).status_code == 403
    assert (await http.get("/conversations")).status_code == 403
    assert (
        await viewer.post("/feedback", json={"audit_log_id": 1, "rating": "up"})
    ).status_code == 403

    # Reading the record of every question anyone asked is an admin's job.
    assert (await http.get("/audit")).status_code == 403

    # What a viewer may do: see their own client, and the released reviews.
    assert (await http.get("/auth/me")).json()["client"]["slug"] == "acme-it"
    assert (await http.get("/reports/tbr")).status_code == 200


async def test_a_franchisee_admin_cannot_draft_or_reach_an_internal_copy(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """What stays ours once an administrator may make reviews of their own customers.

    They generate, settle the findings they are asked about, approve the
    Monthly Report, render the client copy and release it. **Writing the
    review is still ours**, and so is every copy that shows how it was
    written: `internal` carries the fact-id trace and `demo` carries
    pseudonyms of somebody else's estate.
    """
    admin = await sign_in(http, "boss@acme.test")
    report_id = "00000000-0000-4000-8000-000000000000"

    # Drafting: the sentences a customer reads are not written by the customer.
    for body in (
        {"action": "add", "section": "trends", "text": "something"},
        {"action": "edit", "section": "trends", "index": 0, "text": "something"},
        {"action": "remove", "section": "trends", "index": 0},
        {"action": "scores", "nps": "9"},
        {"action": "notes", "section": "trends", "text": "a talk track"},
        {"action": "contact_names", "show": True},
        {"action": "industry", "text": "legal services"},
    ):
        refused = await admin.post(f"/reports/tbr/{report_id}/review", json=body)
        assert refused.status_code == 403, (body["action"], refused.text)

    # Approving: the Monthly Report is theirs, Industry Trends is not. A trend
    # is web-research output on a customer's deck and an engineer signs it.
    trends = await admin.post(
        f"/reports/tbr/{report_id}/review", json={"action": "approve", "section": "trends"}
    )
    assert trends.status_code == 403
    assert "Monthly Report" in trends.json()["detail"]

    # Rendering: the client copy and no other, refused before the report is
    # even looked up, which is why a report id that exists is not needed here.
    for mode in ("internal", "demo"):
        asked = await admin.post(f"/reports/tbr/{report_id}/render", json={"mode": mode})
        assert asked.status_code == 403, mode

    # Downloading is held to the same rule, deck and Monthly Report alike.
    internal = await http.get(f"/reports/tbr/{report_id}/download", params={"mode": "internal"})
    assert internal.status_code == 403
    pdf = await http.get(
        f"/reports/tbr/{report_id}/download", params={"mode": "demo", "output": "pdf"}
    )
    assert pdf.status_code == 403


async def test_a_technician_and_a_viewer_cannot_make_a_review(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """Making a review is the administrator's, not every franchisee account's.

    A technician fixes machines and a viewer reads what has been released;
    neither generates one, settles a finding on one, or decides when a
    customer sees it. All three are refused at the door, so none of them
    costs a connector read or a model call to refuse.
    """
    report_id = "00000000-0000-4000-8000-000000000000"
    for email in ("tech@acme.test", "viewer@acme.test"):
        session = await sign_in(http, email)
        made = await session.post("/reports/tbr", json={"site": "a-site", "period": "2026-08"})
        assert made.status_code == 403, (email, made.text)
        settled = await session.post(
            f"/reports/tbr/{report_id}/review", json={"action": "confirm", "finding": "x"}
        )
        assert settled.status_code == 403, email
        released = await session.post(f"/reports/tbr/{report_id}/render", json={})
        assert released.status_code == 403, email
        # And they still read what has been released to them.
        assert (await http.get("/reports/tbr")).status_code == 200


# --- Inputiv staff, and the switch that is a decision -------------------------


async def test_staff_have_no_client_until_they_switch_to_one(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """A session that has not chosen a tenant cannot act on one."""
    staff = await sign_in(http, "engineer@inputiv.test")

    me = (await http.get("/auth/me")).json()
    assert me["client"] is None and me["may_switch_client"] is True

    # 409, not 403: they are allowed to do this, they just have not said where.
    asked = await staff.post("/chat", json={"message": "what is stale?"})
    assert asked.status_code == 409
    assert "switch-client" in asked.json()["detail"]

    switched = await staff.post("/auth/switch-client", json={"client": "acme-it"})
    assert switched.status_code == 200
    assert switched.json()["client"] == {"slug": "acme-it", "name": "Acme IT"}
    assert (await http.get("/reports/tbr")).status_code == 200


async def test_a_staff_client_switch_is_logged(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Which tenant we were working on, when, and who decided: a row, not a guess."""
    staff = await sign_in(http, "engineer@inputiv.test")
    assert (await staff.post("/auth/switch-client", json={"client": "acme-it"})).status_code == 200
    assert (await staff.post("/auth/switch-client", json={"client": "beta-msp"})).status_code == 200

    switches = await audit_rows(session_maker, event_type=AuditEventType.CLIENT_SWITCHED)
    assert len(switches) == 2

    first, second = switches
    assert first.outcome is AuditOutcome.SUCCESS
    assert first.actor_user_id == world["staff"].id
    assert first.actor_id == str(world["staff"].id)
    assert first.client_id == world["acme"].id
    assert first.payload["to_client_slug"] == "acme-it"
    assert first.payload["from_client_id"] is None

    # The second says where it came from, so a trail can be followed.
    assert second.payload["from_client_id"] == str(world["acme"].id)
    assert second.payload["to_client_slug"] == "beta-msp"
    assert second.client_id == world["beta"].id


async def test_switching_to_a_client_that_is_not_there_changes_nothing(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    staff = await sign_in(http, "engineer@inputiv.test")
    await staff.post("/auth/switch-client", json={"client": "acme-it"})

    missing = await staff.post("/auth/switch-client", json={"client": "no-such-client"})
    assert missing.status_code == 404
    assert (await http.get("/auth/me")).json()["client"]["slug"] == "acme-it"


async def test_an_engineer_is_not_an_administrator(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """Inputiv staff see every client; only an admin makes accounts."""
    staff = await sign_in(http, "engineer@inputiv.test")
    slugs = [row["slug"] for row in (await http.get("/auth/clients")).json()]
    assert slugs == ["acme-it", "beta-msp"]

    assert (await http.get("/auth/users")).status_code == 403
    invited = await staff.post(
        "/auth/invites", json={"email": "new@inputiv.test", "role": "inputiv_engineer"}
    )
    assert invited.status_code == 403


async def test_a_franchisee_admin_invites_only_into_their_own_organisation(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    admin = await sign_in(http, "boss@acme.test")

    ours = await admin.post(
        "/auth/invites", json={"email": "colleague@acme.test", "role": "franchisee_viewer"}
    )
    assert ours.status_code == 201
    assert ours.json()["organisation"] == "acme-people"
    assert ours.json()["token"]

    theirs = await admin.post(
        "/auth/invites",
        json={
            "email": "someone@beta.test",
            "role": "franchisee_viewer",
            "organisation": "beta-people",
        },
    )
    assert theirs.status_code == 403

    escalation = await admin.post(
        "/auth/invites", json={"email": "sneaky@acme.test", "role": "inputiv_admin"}
    )
    assert escalation.status_code == 403

    # And the listing shows their own organisation's invites only.
    listed = (await http.get("/auth/invites")).json()
    assert [row["email"] for row in listed] == ["colleague@acme.test"]


async def test_suspending_an_account_ends_its_sessions_at_once(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """A suspended user's open browser stops working on its next request."""
    victim = await sign_in(http, "viewer@acme.test")
    assert (await http.get("/auth/me")).status_code == 200

    async with httpx.AsyncClient(
        transport=http._transport, base_url="https://engine.test"
    ) as second:
        admin = await sign_in(second, "admin@inputiv.test")
        done = await admin.post(
            f"/auth/users/{world['acme_viewer'].id}/status", json={"status": "suspended"}
        )
        assert done.status_code == 200
        assert done.json()["status"] == UserStatus.SUSPENDED.value

    # The victim's cookie is still in their jar, and is worth nothing.
    assert (await http.get("/auth/me")).status_code == 401
    assert (await victim.post("/chat", json={"message": "hi"})).status_code == 401


# --- staff must hold a second factor when the deployment says so ------------------


async def test_staff_without_a_second_factor_reach_only_their_own_account_when_required(
    monkeypatch: pytest.MonkeyPatch,
    make_client: Make,
    make_org: Make,
    make_user: Make,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """`TOTP_REQUIRED_FOR_INPUTIV`: a staff account that has not enrolled signs in
    and sees itself, and is refused everything of a client's until it has; a
    franchisee's account is untouched."""
    monkeypatch.setenv("TOTP_REQUIRED_FOR_INPUTIV", "true")
    get_settings.cache_clear()

    acme = await make_client("acme-it", "Acme IT")
    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    acme_org = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=acme)
    await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER)
    await make_user(acme_org, "boss@acme.test", Role.FRANCHISEE_ADMIN)

    async def dependency() -> Any:
        async with session_maker() as session:
            yield session

    application = create_app(get_settings())
    application.dependency_overrides[get_session] = dependency
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="https://engine.test"
    ) as http:
        staff = await sign_in(http, "engineer@inputiv.test")
        me = (await http.get("/auth/me")).json()
        assert me["totp_enabled"] is False and me["second_factor_required"] is True
        # Their own account: reachable. A client's anything: refused, and told why.
        assert (await http.get("/auth/clients")).status_code == 200
        refused = await staff.post("/auth/switch-client", json={"client": "acme-it"})
        assert refused.status_code == 403
        assert refused.headers["X-Second-Factor"] == "enrol"
        assert "enrol a second factor" in refused.json()["detail"]
        assert (await http.get("/sites/list")).status_code == 403
        page = await http.get("/settings", headers={"Accept": "text/html"})
        assert page.status_code == 200  # the page where enrolment happens
        # Enrolment lifts it.
        secret = (await staff.post("/auth/totp/start")).json()["secret"]
        assert (
            await staff.post("/auth/totp/confirm", json={"code": code_for(secret)})
        ).status_code == 204
        assert (await http.get("/auth/me")).json()["second_factor_required"] is False
        assert (
            await staff.post("/auth/switch-client", json={"client": "acme-it"})
        ).status_code == 200
        assert (await http.get("/sites/list")).status_code == 200
        # A franchisee administrator without a second factor is not held to it.
        await sign_in(http, "boss@acme.test")
        assert (await http.get("/sites/list")).status_code == 200
        assert (await http.get("/auth/me")).json()["second_factor_required"] is False


# --- the technician: the work, not the account -------------------------------


async def test_a_technician_may_ask_and_work_tickets(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """Ask and the ticket workspace are the technician's job, as they are the
    administrator's. Both run a model, which is why a viewer has neither."""
    tech = await sign_in(http, "tech@acme.test")

    assert (await http.get("/auth/me")).json()["client"]["slug"] == "acme-it"
    assert (await http.get("/auth/me")).json()["role"] == "franchisee_technician"

    # A thread of their own, and the listing behind it.
    made = await tech.post("/conversations", json={})
    assert made.status_code == 201
    assert (await http.get("/conversations")).status_code == 200
    assert (await http.get(f"/conversations/{made.json()['id']}/messages")).status_code == 200

    # The ticket workspace: the listing answers, and a workspace for a ticket
    # this tenant does not have is a 404 from the connector, never a 403.
    listed = await http.get("/tickets/list")
    assert listed.status_code in (200, 502)  # no Autotask credentials in this world
    opened = await tech.post("/tickets/workspaces", json={"ticket_number": "T404"})
    assert opened.status_code != 403


async def test_a_technician_may_read_devices_and_run_the_coverage_board(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """The board, refreshing it, and excluding a device with a reason: the
    coverage numbers are the technician's work. The identity queue is not."""
    tech = await sign_in(http, "tech@acme.test")

    for path in ("/devices/list", "/devices/export.csv", "/coverage/board", "/coverage/exclusions"):
        assert (await http.get(path)).status_code == 200, path

    assert (await tech.post("/coverage/refresh", json={})).status_code in (202, 409)
    assert (await http.get("/coverage/refresh")).status_code == 200

    # An exclusion needs a reason: refused for the reason, not for the role.
    without = await tech.post(
        "/coverage/exclusions",
        json={"site": "a-site", "device_id": "1", "device_name": "X", "tool": "bitdefender"},
    )
    assert without.status_code == 422

    # Linking a vendor record to a device changes numbers a franchisee reads,
    # so it stays Inputiv's, for the technician as for the administrator.
    assert (await http.get("/devices/unresolved")).status_code == 403
    assert (await http.get("/devices/agreement")).status_code == 403
    assert (
        await tech.post("/devices/links", json={"device_id": "x", "connector": "ninjaone"})
    ).status_code == 403


async def test_a_technician_reads_reviews_exactly_as_a_viewer_does(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """Released reviews and nothing behind them: no generating, no approving,
    no rendering, and no internal or demo deck."""
    tech = await sign_in(http, "tech@acme.test")
    report_id = "00000000-0000-4000-8000-000000000000"

    assert (await http.get("/reports/tbr")).status_code == 200
    assert (await http.get("/reports/tbr/sites")).status_code == 200

    assert (
        await tech.post("/reports/tbr", json={"site": "a-site", "period": "2026-08"})
    ).status_code == 403
    assert (
        await tech.post(
            f"/reports/tbr/{report_id}/review", json={"action": "approve", "section": "trends"}
        )
    ).status_code == 403
    assert (await tech.post(f"/reports/tbr/{report_id}/render", json={})).status_code == 403
    internal = await http.get(f"/reports/tbr/{report_id}/download", params={"mode": "internal"})
    assert internal.status_code == 403


async def test_a_technician_has_no_account_administration_and_no_other_client(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """No users, no invites, no audit trail, and no way to name another client."""
    tech = await sign_in(http, "tech@acme.test")

    assert (await http.get("/auth/users")).status_code == 403
    assert (await http.get("/auth/invites")).status_code == 403
    assert (
        await tech.post(
            "/auth/invites", json={"email": "someone@acme.test", "role": "franchisee_viewer"}
        )
    ).status_code == 403
    assert (await http.get("/audit")).status_code == 403
    assert (
        await tech.post(
            "/auth/users/00000000-0000-4000-8000-000000000000/status", json={"status": "suspended"}
        )
    ).status_code == 403

    # Their client is their organisation's, and switching is not theirs to do.
    assert (await http.get("/auth/me")).json()["may_switch_client"] is False
    assert (await tech.post("/auth/switch-client", json={"client": "beta-msp"})).status_code == 403
    assert (await http.get("/auth/me")).json()["client"]["slug"] == "acme-it"


async def test_a_franchisee_admin_may_invite_a_technician(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """The role is grantable by the people who run the franchisee's account, and
    an Inputiv role still is not."""
    admin = await sign_in(http, "boss@acme.test")

    invited = await admin.post(
        "/auth/invites", json={"email": "newtech@acme.test", "role": "franchisee_technician"}
    )
    assert invited.status_code == 201
    assert invited.json()["role"] == "franchisee_technician"

    refused = await admin.post(
        "/auth/invites", json={"email": "nope@acme.test", "role": "inputiv_engineer"}
    )
    assert refused.status_code == 403

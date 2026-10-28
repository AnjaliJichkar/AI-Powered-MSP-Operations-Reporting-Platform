"""A franchisee's administrator making a review of their own customer.

The rights are new; the boundaries around them are the point of this file.
They generate a review, settle the findings that were held for somebody who
knows the estate, approve the Monthly Report and release it — on **their own
customers**, within an allowance, and without ever seeing how any of it was
worked out.

What is proved here, in the order the refusals matter:

- another organisation's customer is not reachable, and naming one gets
  nothing back about it;
- the internal copy and its workings are not theirs, released or not;
- the month's allowance is counted and refused in words, and the refusal
  happens before a connector is read or a model is called;
- a technician and a viewer make nothing at all;
- every one of these acts is a row in the audit log with the account on it.

Over HTTP with real cookies, as `test_authorization.py` is, because an
authorisation rule tested through the function it guards is a rule nobody has
tested.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditEventType, Client, OrganisationKind, Role, Site
from app.models.report import TbrReport
from app.reports.tbr import pipeline, store
from app.reports.tbr.config import load_config, save_config
from app.reports.tbr.limits import (
    DEFAULT_GENERATIONS_PER_SITE_PER_MONTH,
    generations_this_month,
    month_bounds,
)
from app.reports.tbr.pdf import MONTHLY_SECTION
from tests.api.conftest import Make, audit_rows, sign_in
from tests.reports.conftest import make_document

pytestmark = pytest.mark.usefixtures("clean")

PERIOD = "2026-08"


@pytest.fixture
async def world(
    make_client: Make,
    make_org: Make,
    make_user: Make,
    session_maker: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Two franchisees, each with a customer of their own and people of their own."""
    acme = await make_client("acme-it", "Acme IT")
    beta = await make_client("beta-msp", "Beta MSP")

    inputiv = await make_org(OrganisationKind.INPUTIV, "inputiv", name="Inputiv")
    acme_org = await make_org(OrganisationKind.FRANCHISEE, "acme-people", client=acme)
    beta_org = await make_org(OrganisationKind.FRANCHISEE, "beta-people", client=beta)

    async with session_maker() as session:
        acme_site = Site(client_id=acme.id, slug="acme-legal", name="Acme Legal")
        beta_site = Site(client_id=beta.id, slug="beta-dental", name="Beta Dental")
        session.add_all([acme_site, beta_site])
        await session.commit()
        acme_site_id, beta_site_id = acme_site.id, beta_site.id

    return {
        "acme": acme,
        "beta": beta,
        "acme_site_id": acme_site_id,
        "beta_site_id": beta_site_id,
        "staff": await make_user(inputiv, "engineer@inputiv.test", Role.INPUTIV_ENGINEER),
        "acme_admin": await make_user(acme_org, "boss@acme.test", Role.FRANCHISEE_ADMIN),
        "acme_tech": await make_user(acme_org, "tech@acme.test", Role.FRANCHISEE_TECHNICIAN),
        "acme_viewer": await make_user(acme_org, "viewer@acme.test", Role.FRANCHISEE_VIEWER),
        "beta_admin": await make_user(beta_org, "boss@beta.test", Role.FRANCHISEE_ADMIN),
    }


async def _fill_allowance(
    maker: async_sessionmaker[AsyncSession],
    *,
    client: Client,
    site_id: uuid.UUID,
    created_by: str,
    count: int,
    month: str = PERIOD,
) -> None:
    """`count` reviews of this customer, as this account, generated this month.

    Written as rows rather than generated, because generating one reads every
    connector and calls the model: the thing under test is the counter, and a
    test that paid for ten reviews to prove an eleventh is refused would be a
    test nobody runs.
    """
    start = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    async with maker() as session:
        for index in range(count):
            session.add(
                TbrReport(
                    client_id=client.id,
                    site_id=site_id,
                    period_key=f"{month}/1",
                    period_start=start,
                    period_end=start + dt.timedelta(days=31),
                    status="superseded" if index else "draft",
                    document={},
                    created_by=created_by,
                    created_at=dt.datetime.now(dt.UTC),
                )
            )
        await session.commit()


# --- another organisation's customer -----------------------------------------


async def test_another_organisations_customer_is_not_reachable_at_all(
    http: httpx.AsyncClient, world: dict[str, Any]
) -> None:
    """The client comes from their organisation, so there is nowhere to name one.

    Beta's customer is a real site of a real other franchisee, not an invented
    slug: the refusal has to be that it is not *theirs*, not that it does not
    exist. Acme's administrator gets the same answer either way, which is the
    point — it tells them nothing about Beta.
    """
    acme = await sign_in(http, "boss@acme.test")

    theirs = await acme.post("/reports/tbr", json={"site": "beta-dental", "period": PERIOD})
    invented = await acme.post("/reports/tbr", json={"site": "no-such-site", "period": PERIOD})
    assert theirs.status_code == invented.status_code == 400

    # The same sentence either way, with their own slug echoed back and
    # nothing said about Beta. A real customer of somebody else's and a slug
    # nobody has are indistinguishable from here, which is what stops this
    # being a way to find out who else we work for.
    for refused, asked in ((theirs, "beta-dental"), (invented, "no-such-site")):
        assert refused.json()["detail"] == f"[factory] {asked!r} is not a site of 'acme-it'"

    # The customer picker is their own customers and nobody else's.
    sites = (await http.get("/reports/tbr/sites")).json()
    assert [site["slug"] for site in sites] == ["acme-legal"]


async def test_another_organisations_review_is_a_404_not_a_403(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An id of Beta's tells Acme's administrator nothing, including that it exists."""
    await _fill_allowance(
        session_maker,
        client=world["beta"],
        site_id=world["beta_site_id"],
        created_by=str(world["beta_admin"].id),
        count=1,
    )
    async with session_maker() as session:
        row = (await session.execute(select(TbrReport))).scalar_one()
        beta_report = str(row.id)

    acme = await sign_in(http, "boss@acme.test")
    assert (await http.get(f"/reports/tbr/{beta_report}")).status_code == 404
    assert (await http.get("/reports/tbr")).json() == []
    settled = await acme.post(
        f"/reports/tbr/{beta_report}/review", json={"action": "confirm", "finding": "x"}
    )
    assert settled.status_code == 404
    assert (await acme.post(f"/reports/tbr/{beta_report}/render", json={})).status_code == 404


# --- the month's allowance ----------------------------------------------------


async def test_the_allowance_is_counted_and_refused_in_words(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Over the limit is a 429 whose message is the answer, not a 403.

    429 because they are allowed to do this and have done it enough times this
    month; the message names the customer, both numbers and the date it starts
    again, because a limit nobody can plan around is a door with no handle.
    """
    await _fill_allowance(
        session_maker,
        client=world["acme"],
        site_id=world["acme_site_id"],
        created_by=str(world["acme_admin"].id),
        count=DEFAULT_GENERATIONS_PER_SITE_PER_MONTH,
    )
    acme = await sign_in(http, "boss@acme.test")
    refused = await acme.post("/reports/tbr", json={"site": "acme-legal", "period": PERIOD})

    assert refused.status_code == 429, refused.text
    detail = refused.json()["detail"]
    assert "Acme Legal" in detail
    assert f"{DEFAULT_GENERATIONS_PER_SITE_PER_MONTH} of " in detail
    assert "starts again on" in detail
    # Nothing internal leaks into a sentence a customer's MSP reads.
    assert "tbr" not in detail.lower() and "site_id" not in detail


async def test_the_allowance_is_per_customer_and_not_per_franchisee(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A franchisee with forty customers is not the same spender as one with four.

    Having used up one customer's month says nothing about another's, so the
    second customer's generation is refused for the ordinary reason — there is
    no such customer here — and never for the allowance.
    """
    await _fill_allowance(
        session_maker,
        client=world["acme"],
        site_id=world["acme_site_id"],
        created_by=str(world["acme_admin"].id),
        count=DEFAULT_GENERATIONS_PER_SITE_PER_MONTH,
    )
    async with session_maker() as session:
        second = Site(client_id=world["acme"].id, slug="acme-dental", name="Acme Dental")
        session.add(second)
        await session.commit()
        second_id = second.id

    async with session_maker() as session:
        used_first = await generations_this_month(
            session, client_id=world["acme"].id, site_id=world["acme_site_id"]
        )
        used_second = await generations_this_month(
            session, client_id=world["acme"].id, site_id=second_id
        )
    assert used_first == DEFAULT_GENERATIONS_PER_SITE_PER_MONTH
    assert used_second == 0

    acme = await sign_in(http, "boss@acme.test")
    other = await acme.post("/reports/tbr", json={"site": "acme-dental", "period": PERIOD})
    assert other.status_code != 429


async def test_inputiv_generations_neither_count_nor_are_counted(
    world: dict[str, Any], session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """The allowance is a budget for what a franchisee does without asking us.

    An engineer drafting a review on the same customer does not spend it, so a
    franchisee cannot be locked out by work they did not do — which would be a
    limit they could not reason about.
    """
    await _fill_allowance(
        session_maker,
        client=world["acme"],
        site_id=world["acme_site_id"],
        created_by=str(world["staff"].id),
        count=DEFAULT_GENERATIONS_PER_SITE_PER_MONTH + 5,
    )
    async with session_maker() as session:
        used = await generations_this_month(
            session, client_id=world["acme"].id, site_id=world["acme_site_id"]
        )
    assert used == 0


async def test_the_allowance_is_a_setting_and_zero_switches_generating_off(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Not a constant, and the message at zero says something different.

    "You have generated 0 of 0" is arithmetic. A franchisee who has been put
    back to reading reviews we make for them is told that instead.
    """
    async with session_maker() as session:
        config = await load_config(session, world["acme"].id, client_name="Acme IT")
        config.self_service.generations_per_site_per_month = 0
        await save_config(session, world["acme"].id, config, updated_by="test")
        await session.commit()

    acme = await sign_in(http, "boss@acme.test")
    refused = await acme.post("/reports/tbr", json={"site": "acme-legal", "period": PERIOD})
    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert "switched off" in detail and "Acme Legal" in detail
    assert "0 of 0" not in detail


def test_the_month_a_generation_falls_in_is_the_one_it_happened_in() -> None:
    """Not the review's own period: regenerating August four times in October
    is four generations in October, which is the month somebody is billed in."""
    start, resets = month_bounds(dt.datetime(2026, 10, 17, 23, 30, tzinfo=dt.UTC))
    assert start == dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    assert resets == dt.date(2026, 11, 1)
    # December rolls the year rather than reaching month 13.
    _, new_year = month_bounds(dt.datetime(2026, 12, 31, tzinfo=dt.UTC))
    assert new_year == dt.date(2027, 1, 1)


# --- what is written down -----------------------------------------------------


async def _review_of_acme(
    maker: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    *,
    created_by: str,
) -> str:
    """One stored review of Acme's customer, with a finding held for somebody.

    The held one is made by hand rather than hoped for: which rules hold a
    finding depends on the facts, and a test of what an administrator is asked
    to settle should not also be a test of whether anything was asked.
    """
    document = await make_document()
    document.client_id = str(world["acme"].id)
    document.client_slug = "acme-it"
    document.site_id = str(world["acme_site_id"])
    document.site_slug = "acme-legal"
    document.site_name = "Acme Legal"
    held = document.findings[0]
    held.needs_confirmation = True
    held.confirmed_by = None
    async with maker() as session:
        row = await store.save_new(session, document, created_by=created_by)
        await session.commit()
        return str(row.id)


async def test_an_administrator_is_shown_the_review_and_none_of_its_workings(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The third shape: what they are asked to act on, and nothing that proves it.

    Built from the parts rather than by removing fields, so this asserts the
    absences by name: a field added to the document later cannot arrive here
    by default.
    """
    report_id = await _review_of_acme(session_maker, world, created_by=str(world["staff"].id))
    await sign_in(http, "boss@acme.test")

    view = (await http.get(f"/reports/tbr/{report_id}")).json()

    # The workings, by name. Each one is a thing the internal copy has.
    for ours in ("facts", "gaps", "findings", "generated_by", "people_names_default"):
        assert ours not in view, ours
    for section in view["sections"]:
        for ours in ("fact_ids", "finding_ids", "problems", "narrative", "engineer_items"):
            assert ours not in section, ours
    # Nor any copy but the customer's.
    assert all(key in ("client", "pdf/client") for key in view["renders"])

    # What they are asked about: the finding, the tools behind it as words,
    # and never a source key or a fact id.
    held = view["held_findings"]
    assert held and all(not finding.get("fact_ids") for finding in held)
    assert all(tool[:1].isupper() for finding in held for tool in finding["tools"])

    # Theirs or ours, and nothing finer.
    assert view["mine"] is False
    assert "released_by" not in view


async def test_an_administrator_settles_a_finding_and_it_is_on_the_record(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirming is theirs, and who did it is a row with the account on it.

    Re-drafting after a finding moves is a model call, which is stubbed: what
    is under test is who may settle it and what is written down, not what the
    model then writes.
    """
    report_id = await _review_of_acme(session_maker, world, created_by=str(world["staff"].id))

    async def unchanged(document: Any, *, actor: str) -> Any:
        return document

    monkeypatch.setattr(pipeline, "rebuild", unchanged)

    acme = await sign_in(http, "boss@acme.test")
    finding_id = (await http.get(f"/reports/tbr/{report_id}")).json()["held_findings"][0]["id"]

    settled = await acme.post(
        f"/reports/tbr/{report_id}/review",
        json={"action": "confirm", "finding": finding_id},
    )
    assert settled.status_code == 200, settled.text

    rows = [
        row
        for row in await audit_rows(session_maker)
        if row.event_type is AuditEventType.REPORT_REVIEWED
    ]
    assert len(rows) == 1
    assert rows[0].actor_user_id == world["acme_admin"].id
    assert rows[0].actor_id == str(world["acme_admin"].id)
    assert rows[0].payload["action"] == "confirm"
    assert rows[0].client_id == world["acme"].id


async def test_the_monthly_report_is_theirs_to_approve_and_industry_trends_is_not(
    http: httpx.AsyncClient,
    world: dict[str, Any],
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The one approval they have, and the one they do not.

    Without the Monthly Report's they could release a deck and never a PDF of
    a review they made themselves. Industry Trends is web-research output on a
    customer's deck and an engineer still signs it.
    """
    report_id = await _review_of_acme(session_maker, world, created_by=str(world["staff"].id))
    acme = await sign_in(http, "boss@acme.test")

    approved = await acme.post(
        f"/reports/tbr/{report_id}/review",
        json={"action": "approve", "section": MONTHLY_SECTION},
    )
    assert approved.status_code == 200, approved.text

    refused = await acme.post(
        f"/reports/tbr/{report_id}/review", json={"action": "approve", "section": "trends"}
    )
    assert refused.status_code == 403
    assert "Monthly Report" in refused.json()["detail"]

"""The business-review API over HTTP, against a real database and the real template."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.reports as reports_api
import app.reports.tbr.audit as audit_module
import app.reports.tbr.pipeline as pipeline_module
from app.main import create_app
from app.models import AuditLog, Client, Site, User
from app.models.enums import AuditEventType
from app.models.report import TbrReport
from app.reports.tbr import store
from app.reports.tbr.model import Item
from app.settings import Settings
from tests.agent.conftest import LIVE_URL, audit_rows
from tests.authfixtures import make_scope, sign_in
from tests.reports.conftest import make_document

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set"),
]


@pytest.fixture
def api(
    db_client: Client,
    db_user: User,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: Settings,
) -> FastAPI:
    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    for module in (reports_api, pipeline_module, audit_module):
        monkeypatch.setattr(module, "session_scope", scope)
    monkeypatch.setattr(
        pipeline_module,
        "get_settings",
        lambda: settings.model_copy(update={"reports_dir": str(tmp_path)}),
    )
    application = create_app(settings)
    # An Inputiv engineer, switched to this client. What a franchisee sees
    # instead is tests/api/test_authorization.py.
    sign_in(
        application,
        make_scope(
            user_id=db_user.id,
            email=db_user.email,
            client_id=db_client.id,
            client_slug=db_client.slug,
        ),
    )
    return application


@pytest.fixture
async def http(api: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://engine.test"
    ) as client:
        yield client


async def _stored(maker: async_sessionmaker[AsyncSession], client: Client) -> str:
    async with maker() as session:
        site = Site(client_id=client.id, slug="acme-legal", name="Acme Legal")
        session.add(site)
        await session.flush()
        document = await make_document()
        document.client_id, document.site_id = str(client.id), str(site.id)
        document.client_slug = client.slug
        row = await store.save_new(session, document, created_by="sam")
        await session.commit()
        return str(row.id)


async def test_another_clients_report_is_not_found(
    api: FastAPI,
    http: httpx.AsyncClient,
    db_client: Client,
    db_user: User,
    make_client: Any,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A report is found through the session's client, and no other."""
    other = await make_client("another-franchisee", "Another Franchisee")
    report_id = await _stored(session_maker, db_client)

    mine = await http.get(f"/reports/tbr/{report_id}")
    assert mine.status_code == 200 and mine.json()["site"] == "Acme Legal"
    assert len(mine.json()["slides"]) == 24  # 23 slides and the Monthly Report

    # The same engineer, switched to the other franchisee. The body cannot
    # name a client any more, so this is the only way to ask.
    sign_in(
        api,
        make_scope(
            user_id=db_user.id,
            email=db_user.email,
            client_id=other.id,
            client_slug=other.slug,
        ),
    )
    theirs = await http.get(f"/reports/tbr/{report_id}")
    assert theirs.status_code == 404  # not 403: the id must not confirm the report exists
    assert (await http.get("/reports/tbr")).json() == []
    body = {"action": "approve", "section": "trends"}
    assert (await http.post(f"/reports/tbr/{report_id}/review", json=body)).status_code == 404
    assert (await http.post(f"/reports/tbr/{report_id}/render", json={})).status_code == 404


async def test_review_then_render_then_download(
    http: httpx.AsyncClient,
    db_client: Client,
    db_user: User,
    session_maker: async_sessionmaker[AsyncSession],
    engine: Any,
) -> None:
    report_id = await _stored(session_maker, db_client)

    early = await http.get(f"/reports/tbr/{report_id}/download")
    assert early.status_code == 409  # nothing rendered yet

    refused = await http.post(
        f"/reports/tbr/{report_id}/review",
        json={"action": "add", "section": "accomplishments", "text": "Insert wins here"},
    )
    assert refused.status_code == 409 and "placeholder" in refused.json()["detail"]

    added = await http.post(
        f"/reports/tbr/{report_id}/review",
        json={
            "action": "scores",
            "nps": "72",
            "csat": "96",
            "comments": ["Fast and friendly"],
        },
    )
    assert added.status_code == 200
    cx = next(s for s in added.json()["slides"] if s["slide"] == 9)
    assert (cx["status"], cx["on_deck"]) == ("filled", "yes")

    rendered = await http.post(f"/reports/tbr/{report_id}/render", json={})
    assert rendered.status_code == 200
    assert rendered.json()["gate"]["passed"] is True and 9 in rendered.json()["kept"]

    deck = await http.get(f"/reports/tbr/{report_id}/download")
    assert deck.status_code == 200 and deck.content[:2] == b"PK"  # a real .pptx

    rows = await audit_rows(engine, db_client.id)
    kinds = [row.event_type for row in rows]
    assert AuditEventType.REPORT_REVIEWED in kinds and AuditEventType.REPORT_RENDERED in kinds
    reviewed = next(r for r in rows if r.event_type is AuditEventType.REPORT_REVIEWED)
    # The actor is the signed-in account, not a handle the body supplied.
    assert reviewed.actor_id == str(db_user.id)
    assert reviewed.actor_user_id == db_user.id
    assert reviewed.payload["action"] == "scores"


async def test_a_gate_refusal_is_a_409_that_lists_the_problems(
    http: httpx.AsyncClient,
    db_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    engine: Any,
) -> None:
    report_id = await _stored(session_maker, db_client)
    async with session_maker() as session:
        row, document = await store.load(session, report_id, client_id=db_client.id)
        document.section("accomplishments").engineer_items.append(
            Item(text="Replaced XX laptops", cites=["engineer:x"], author="engineer")
        )
        await store.update(session, row, document)
        await session.commit()

    response = await http.post(f"/reports/tbr/{report_id}/render", json={})

    assert response.status_code == 409
    assert any("'XX'" in problem for problem in response.json()["detail"]["problems"])
    rows: list[AuditLog] = await audit_rows(engine, db_client.id)
    denied = next(r for r in rows if r.event_type is AuditEventType.REPORT_RENDERED)
    assert denied.outcome.value == "denied"  # the refusal is on the record too


async def test_render_modes_notes_and_what_the_review_keeps(
    http: httpx.AsyncClient,
    db_client: Client,
    db_user: User,
    session_maker: async_sessionmaker[AsyncSession],
    engine: Any,
    tmp_path: Path,
) -> None:
    from pptx import Presentation

    from app.reports.tbr import gate

    report_id = await _stored(session_maker, db_client)
    base = f"/reports/tbr/{report_id}"

    noted = await http.post(
        f"{base}/review",
        json={"action": "notes", "section": "cybersecurity", "text": "Open with patching."},
    )
    assert noted.status_code == 200
    section = next(s for s in noted.json()["sections"] if s["key"] == "cybersecurity")
    assert section["talk_track"] == "Open with patching."
    assert section["talk_track_by"] == str(db_user.id)

    # An internal or demo deck is a by-product: the review is not marked as delivered.
    internal = await http.post(f"{base}/render", json={"mode": "internal"})
    assert internal.status_code == 200 and internal.json()["mode"] == "internal"
    assert internal.json()["file"].endswith("-internal.pptx")
    view = (await http.get(base)).json()
    assert view["renders"] == ["internal"] and view["status"] == "draft"
    no_client = await http.get(f"{base}/download")
    assert no_client.status_code == 409 and "no client deck" in no_client.json()["detail"]

    demo = await http.post(f"{base}/render", json={"mode": "demo"})
    assert demo.status_code == 200 and demo.json()["file"].startswith("sample-client-tbr-")
    client = await http.post(f"{base}/render", json={})  # the default
    assert client.status_code == 200 and client.json()["mode"] == "client"

    # The trace is on the review and in the view whichever deck was written.
    view = (await http.get(base)).json()
    assert view["renders"] == ["client", "demo", "internal"] and view["status"] == "rendered"
    cyber = next(s for s in view["sections"] if s["key"] == "cybersecurity")
    assert all(item["cites"] for item in cyber["items"])
    assert any(f["id"] == "ninjaone.patches.failed" and f["definition"] for f in view["facts"])
    assert str(tmp_path) not in str(view)  # modes only: never a server path

    notes = {}
    for mode in ("client", "internal", "demo"):
        got = await http.get(f"{base}/download", params={"mode": mode})
        assert got.status_code == 200
        target = tmp_path / f"got-{mode}.pptx"
        target.write_bytes(got.content)
        notes[mode] = [gate.notes_text(s) for s in Presentation(str(target)).slides]
    assert [n for n in notes["client"] if n] == ["Open with patching."]
    assert [n for n in notes["demo"] if n] == ["Open with patching."]
    assert any("ninjaone.patches.failed" in n for n in notes["internal"])
    assert not any("ninjaone." in n for n in notes["client"] + notes["demo"])

    rows = await audit_rows(engine, db_client.id)
    modes = [r.payload["mode"] for r in rows if r.event_type is AuditEventType.REPORT_RENDERED]
    assert modes == ["internal", "demo", "client"]

    # Any change to the review makes every deck stale.
    await http.post(f"{base}/review", json={"action": "notes", "section": "cybersecurity"})
    view = (await http.get(base)).json()
    assert view["renders"] == [] and view["status"] == "draft"
    stale = await http.get(f"{base}/download")
    assert stale.status_code == 409
    bad = await http.post(f"{base}/render", json={"mode": "customer"})
    assert bad.status_code == 422


async def test_contact_names_are_off_until_an_engineer_turns_them_on(
    http: httpx.AsyncClient,
    db_client: Client,
    db_user: User,
    session_maker: async_sessionmaker[AsyncSession],
    engine: Any,
) -> None:
    report_id = await _stored(session_maker, db_client)
    base = f"/reports/tbr/{report_id}"

    view = (await http.get(base)).json()
    assert (view["show_contact_names"], view["show_contact_names_by"]) == (None, None)
    assert (view["people_names_default"], view["people_names_shown"]) == (False, False)
    takeaways = next(s for s in view["sections"] if s["key"] == "takeaways")
    assert [row["label"] for row in takeaways["scorecard"]][:2] == ["Service", "Devices"]
    assert takeaways["scorecard"][0]["metrics"][0]["rule"]  # the rule travels with the status

    on = await http.post(f"{base}/review", json={"action": "contact_names", "show": True})
    assert on.status_code == 200
    assert on.json()["show_contact_names"] is True and on.json()["people_names_shown"] is True
    # Recorded against the account that turned them on, not a typed handle.
    assert on.json()["show_contact_names_by"] == str(db_user.id)
    # Without `show`, the override is cleared and the franchisee default applies again.
    cleared = await http.post(f"{base}/review", json={"action": "contact_names"})
    assert cleared.status_code == 200
    assert (cleared.json()["show_contact_names"], cleared.json()["people_names_shown"]) == (
        None,
        False,
    )

    rows = await audit_rows(engine, db_client.id)
    reviewed = [r for r in rows if r.event_type is AuditEventType.REPORT_REVIEWED]
    assert reviewed[-2].payload["action"] == "contact_names" and reviewed[-2].payload["show"]
    assert reviewed[-1].payload["action"] == "contact_names" and "show" not in reviewed[-1].payload


async def test_the_monthly_report_pdf_is_rendered_downloaded_and_goes_stale_with_the_deck(
    http: httpx.AsyncClient,
    db_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    engine: Any,
) -> None:
    """The second output over the same routes: `output: pdf` on render, `output=pdf`
    on download, its own approval step, and the same staleness as the deck."""
    from pypdf import PdfReader

    report_id = await _stored(session_maker, db_client)
    base = f"/reports/tbr/{report_id}"

    # The approval step is a section of the review, off the deck.
    view = (await http.get(base)).json()
    monthly = next(s for s in view["sections"] if s["key"] == "monthly_report")
    assert monthly["slide"] == 0 and monthly["requires_approval"]
    assert next(s for s in view["slides"] if s["slide"] == 0)["on_deck"] == "no"

    # A client PDF is refused until then; an internal one is not.
    refused = await http.post(f"{base}/render", json={"output": "pdf"})
    assert refused.status_code == 409
    assert any("not been approved" in p for p in refused.json()["detail"]["problems"])
    internal = await http.post(f"{base}/render", json={"output": "pdf", "mode": "internal"})
    assert internal.status_code == 200
    body = internal.json()
    assert body["output"] == "pdf" and body["mode"] == "internal" and body["pages"] >= 5
    assert body["sections"] == ["Ticketing", "Monitoring", "Backup"]
    assert body["file"].endswith("-internal.pdf") and body["gate"]["passed"] is True
    assert (await http.get(base)).json()["renders"] == ["pdf/internal"]

    approved = await http.post(
        f"{base}/review", json={"action": "approve", "section": "monthly_report"}
    )
    assert approved.status_code == 200
    # The approval is an edit like any other: the internal PDF is stale.
    assert (await http.get(base)).json()["renders"] == []

    rendered = await http.post(f"{base}/render", json={"output": "pdf"})
    assert rendered.status_code == 200 and rendered.json()["mode"] == "client"
    view = (await http.get(base)).json()
    assert view["renders"] == ["pdf/client"] and view["status"] == "draft"  # the deck releases

    got = await http.get(f"{base}/download", params={"output": "pdf"})
    assert got.status_code == 200 and got.headers["content-type"] == "application/pdf"
    assert got.content[:5] == b"%PDF-"
    reader = PdfReader(__import__("io").BytesIO(got.content))
    assert len(reader.pages) == rendered.json()["pages"]
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "Monthly Report" in text and "Ticketing" in text and "Draft" not in text

    # No deck yet: the deck download still says so, in its own words.
    no_deck = await http.get(f"{base}/download")
    assert no_deck.status_code == 409 and "no client deck" in no_deck.json()["detail"]
    deck = await http.post(f"{base}/render", json={})
    assert deck.status_code == 200 and deck.json()["output"] == "deck"
    view = (await http.get(base)).json()
    assert view["renders"] == ["client", "pdf/client"] and view["status"] == "rendered"

    rows = await audit_rows(engine, db_client.id)
    rendered_rows = [r for r in rows if r.event_type is AuditEventType.REPORT_RENDERED]
    assert [(r.payload.get("output", "deck"), r.outcome.value) for r in rendered_rows] == [
        ("pdf", "denied"),
        ("pdf", "success"),
        ("pdf", "success"),
        ("deck", "success"),
    ]

    # One edit, and both outputs are stale together.
    await http.post(f"{base}/review", json={"action": "notes", "section": "cybersecurity"})
    view = (await http.get(base)).json()
    assert view["renders"] == [] and view["status"] == "draft"
    assert (await http.get(f"{base}/download", params={"output": "pdf"})).status_code == 409
    assert (await http.get(f"{base}/download")).status_code == 409
    bad = await http.post(f"{base}/render", json={"output": "docx"})
    assert bad.status_code == 422


async def test_changing_the_franchisee_people_setting_makes_every_render_stale(
    http: httpx.AsyncClient,
    db_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """`set_people_names` (what `manage_client.py report-config --show-people-names`
    calls) writes the setting, copies it onto every current review's snapshot and
    marks each changed, so a released deck and PDF are stale until rendered again."""
    from app.reports.tbr.config import load_config, set_people_names

    report_id = await _stored(session_maker, db_client)
    base = f"/reports/tbr/{report_id}"
    assert (await http.post(f"{base}/render", json={})).status_code == 200
    view = (await http.get(base)).json()
    assert view["status"] == "rendered" and view["people_names_default"] is False

    async with session_maker() as session:
        changed, touched = await set_people_names(
            session, db_client.id, client_name=db_client.name, show=True, by="ops"
        )
        await session.commit()
    assert changed is True and touched == [report_id]

    view = (await http.get(base)).json()
    assert view["renders"] == [] and view["status"] == "draft"  # stale, and unreleased
    assert view["people_names_default"] is True and view["people_names_shown"] is True
    async with session_maker() as session:
        config = await load_config(session, db_client.id, client_name=db_client.name)
        assert config.show_people_names is True
        # The same value again changes nothing and touches no review.
        assert await set_people_names(
            session, db_client.id, client_name=db_client.name, show=True, by="ops"
        ) == (False, [])


@pytest.mark.parametrize("stored", [True, False, None, "missing"])
async def test_a_review_stores_and_loads_with_the_override_in_every_shape(
    http: httpx.AsyncClient,
    db_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    stored: Any,
) -> None:
    """The override is written as true, false or null, and older rows lack the field:
    every one of them loads through `store.load` and answers `GET /reports/tbr/{id}`."""
    report_id = await _stored(session_maker, db_client)
    async with session_maker() as session:
        row, _ = await store.load(session, report_id, client_id=db_client.id)
        document = dict(row.document)
        if stored == "missing":
            document.pop("show_contact_names", None)
        else:
            document["show_contact_names"] = stored
        document["show_contact_names_by"] = "sam" if stored is not None else None
        row.document = document  # written raw, as an older or newer process would leave it
        await session.commit()

    async with session_maker() as session:
        _, loaded = await store.load(session, report_id, client_id=db_client.id)
    expected = None if stored in (None, "missing") else stored
    assert loaded.show_contact_names is expected

    view = await http.get(f"/reports/tbr/{report_id}")
    assert view.status_code == 200, view.text
    assert view.json()["show_contact_names"] is expected
    assert view.json()["people_names_shown"] is (expected if expected is not None else False)


async def test_the_stored_document_check_names_a_row_that_no_longer_loads(
    db_client: Client,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`scripts/check_stored_documents.py`: counts, failing ids and error locations,
    never a document's contents."""
    import importlib

    checker = importlib.import_module("scripts.check_stored_documents")
    good = await _stored(session_maker, db_client)
    async with session_maker() as session:  # a second review, stored raw and broken
        site_id = (await store.load(session, good, client_id=db_client.id))[0].site_id
        document = await make_document()
        document.client_id, document.site_id = str(db_client.id), str(site_id)
        broken = TbrReport(
            client_id=db_client.id,
            site_id=site_id,
            period_key="2026-07/1",
            period_start=document.period.start,
            period_end=document.period.end,
            status="draft",
            document={**document.model_dump(mode="json"), "show_contact_names": "yes please"},
            created_by="sam",
        )
        session.add(broken)
        await session.commit()
        bad = str(broken.id)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    monkeypatch.setattr(checker, "session_scope", scope)
    status = await checker.main(db_client.slug, include_superseded=True)
    out = capsys.readouterr().out

    assert status == checker.EXIT_FAILURES
    assert "load: 1" in out and "fail: 1" in out
    assert bad in out and good not in out
    assert "show_contact_names: bool_parsing" in out
    assert "yes please" not in out and "Acme Legal" not in out  # ids and shapes only

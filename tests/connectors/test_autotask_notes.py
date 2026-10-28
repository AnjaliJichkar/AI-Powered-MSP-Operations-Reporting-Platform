"""The Autotask note writer: one write, internal only, nothing else reachable.

Every assertion here is on the request the writer sent or refused to send,
against a mocked tenant, never a real one. The demo round trip goes through
the fake Autotask the demo connectors use.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from app.connectors.autotask.notes import (
    BODY_MAX,
    INTERNAL_PUBLISH_LABEL,
    NOTE_BODY_FIELDS,
    TITLE_MAX,
    TITLE_PREFIX,
    AutotaskNoteWriter,
    NoteRefused,
    WriteNotAllowed,
)
from app.connectors.base import ConnectorLimits
from app.connectors.errors import AuthError, UpstreamError
from app.connectors.scope import ClientScope
from app.models.enums import ConnectorName
from tests.connectors.conftest import (
    AUTOTASK_SITE,
    AUTOTASK_TEST_SECRETS,
    AUTOTASK_ZONE_URL,
    load_fixture,
    make_scope,
)

NOTES_URL = f"{AUTOTASK_ZONE_URL}/V1.0/Tickets/70345/Notes"
NOTE_FIELDS_URL = f"{AUTOTASK_ZONE_URL}/V1.0/TicketNotes/entityInformation/fields"

PUBLISH_LIVE = {1: "All Autotask Users", 2: "Internal Project Team", 4: "Internal & Co-Managed"}
NOTE_TYPES_LIVE = {
    1: "Task Summary",
    2: "Task Detail",
    3: "Task Notes",
    18: "Client Portal Note",
    99: "RMM Note",
}


def internal_only_value(publish: dict[int, str]) -> int:
    """The internal-only value as the field information gives it: the one
    whose label is `Internal Project Team`. The test's own reading, so the
    writer's is checked against the picklist and not against a number."""
    assert INTERNAL_PUBLISH_LABEL == "Internal Project Team"
    return next(v for v, label in publish.items() if label == INTERNAL_PUBLISH_LABEL)


def _fields(publish: dict[int, str], note_types: dict[int, str]) -> dict[str, Any]:
    def picklist(values: dict[int, str]) -> list[dict[str, Any]]:
        return [{"value": str(v), "label": label, "isActive": True} for v, label in values.items()]

    return {
        "fields": [
            {"name": "publish", "isPickList": True, "picklistValues": picklist(publish)},
            {"name": "noteType", "isPickList": True, "picklistValues": picklist(note_types)},
            {"name": "title", "isPickList": False, "length": 250},
        ]
    }


def writer(limits: ConnectorLimits) -> AutotaskNoteWriter:
    scope: ClientScope = make_scope(ConnectorName.AUTOTASK, [AUTOTASK_SITE], None)
    return AutotaskNoteWriter(
        scope=scope,
        secrets=dict(AUTOTASK_TEST_SECRETS),
        config={"base_url": AUTOTASK_ZONE_URL},
        limits=limits,
    )


def mock_fields(
    publish: dict[int, str] = PUBLISH_LIVE, note_types: dict[int, str] = NOTE_TYPES_LIVE
):
    return respx.get(NOTE_FIELDS_URL).mock(
        return_value=httpx.Response(200, json=_fields(publish, note_types))
    )


# --- internal only, on the request itself ----------------------------------------------


@respx.mock
async def test_the_request_always_carries_the_internal_publish_value(
    fast_limits: ConnectorLimits,
) -> None:
    mock_fields()
    posted = respx.post(NOTES_URL).mock(return_value=httpx.Response(200, json={"itemId": 9001}))
    async with writer(fast_limits) as w:
        result = await w.add_internal_note(
            70345, title=f"{TITLE_PREFIX}Device back online", body="Came back at 09:12."
        )
    internal = internal_only_value(PUBLISH_LIVE)
    assert result.note_id == "9001" and result.publish == internal and result.note_type == 3
    body = json.loads(posted.calls[0].request.content)
    assert body == {
        "ticketID": 70345,
        "title": "Desk: Device back online",
        "description": "Came back at 09:12.",
        "noteType": 3,
        "publish": internal,
    }
    # The value is the one the field information labels internal-only, and
    # nothing else: not "All Autotask Users" (1), which the client portal
    # can show, and not "Internal & Co-Managed" (4).
    assert body["publish"] == 2
    assert body["publish"] not in {
        v for v, label in PUBLISH_LIVE.items() if label != INTERNAL_PUBLISH_LABEL
    }
    assert set(body) == NOTE_BODY_FIELDS


@respx.mock
async def test_the_writer_never_sends_any_other_publish_value(
    fast_limits: ConnectorLimits,
) -> None:
    """Whatever the tenant's picklist holds, the only publish value that ever
    leaves the writer is the internal-only one; every other value the tenant
    offers is proven absent from the request, and a tenant whose only options
    are wider than internal gets no request at all."""
    cases: list[dict[int, str]] = [
        PUBLISH_LIVE,
        {1: "All Autotask Users", 2: "Internal Project Team"},
        {3: "Internal Project Team", 9: "Client Portal", 11: "All"},
    ]
    for publish in cases:
        respx.reset()
        mock_fields(publish=publish)
        posted = respx.post(NOTES_URL).mock(return_value=httpx.Response(200, json={"itemId": 1}))
        async with writer(fast_limits) as w:
            await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="y")
        sent = json.loads(posted.calls[0].request.content)["publish"]
        assert sent == internal_only_value(publish), publish
        for value, label in publish.items():
            if label != INTERNAL_PUBLISH_LABEL:
                assert sent != value, (publish, label)
    # The picklist spelt differently is still matched on the label, never the number.
    respx.reset()
    mock_fields(publish={5: " internal project team "})
    posted = respx.post(NOTES_URL).mock(return_value=httpx.Response(200, json={"itemId": 1}))
    async with writer(fast_limits) as w:
        await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="y")
    assert json.loads(posted.calls[0].request.content)["publish"] == 5


@respx.mock
async def test_a_tenant_whose_picklist_renumbers_the_internal_value_is_followed(
    fast_limits: ConnectorLimits,
) -> None:
    """The number is never assumed. A tenant with the internal label at 7 gets 7."""
    mock_fields(publish={1: "All Autotask Users", 7: "Internal Project Team", 8: "Co-Managed"})
    posted = respx.post(NOTES_URL).mock(return_value=httpx.Response(200, json={"itemId": 1}))
    async with writer(fast_limits) as w:
        await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="y")
    assert json.loads(posted.calls[0].request.content)["publish"] == 7


@respx.mock
async def test_a_tenant_with_no_internal_only_value_gets_no_note(
    fast_limits: ConnectorLimits,
) -> None:
    """A tenant offering only audiences wider than internal gets no note: not
    `All Autotask Users`, not `Internal & Co-Managed`, nothing guessed."""
    mock_fields(publish={1: "All Autotask Users", 4: "Internal & Co-Managed"})
    posted = respx.post(NOTES_URL)
    async with writer(fast_limits) as w:
        with pytest.raises(NoteRefused, match="no 'Internal Project Team' value"):
            await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="y")
    assert posted.call_count == 0


@respx.mock
async def test_a_client_facing_note_type_is_never_chosen(fast_limits: ConnectorLimits) -> None:
    """`Client Portal Note` (18 live) is what the portal shows; even a tenant
    that has renamed its task-note types does not get a client type instead."""
    mock_fields(note_types={18: "Client Portal Note", 2: "Task Detail"})
    posted = respx.post(NOTES_URL).mock(return_value=httpx.Response(200, json={"itemId": 1}))
    async with writer(fast_limits) as w:
        await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="y")
    assert json.loads(posted.calls[0].request.content)["noteType"] == 2
    respx.reset()
    mock_fields(note_types={18: "Client Portal Note", 20: "Task Notes (Client)"})
    posted = respx.post(NOTES_URL)
    async with writer(fast_limits) as w:
        with pytest.raises(NoteRefused, match="noteType picklist has none of"):
            await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="y")
    assert posted.call_count == 0


# --- nothing else is reachable ----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/V1.0/Tickets"),
        ("PUT", "/V1.0/Tickets"),
        ("DELETE", "/V1.0/Tickets/70345/Notes/1"),
        ("POST", "/V1.0/Tickets"),
        ("POST", "/V1.0/TicketNotes"),
        ("POST", "/V1.0/Tickets/70345/Attachments"),
        ("POST", "/V1.0/Companies/1/Notes"),
        ("PATCH", "/V1.0/Tickets/70345/Notes"),
    ],
)
@respx.mock
async def test_any_other_write_is_refused_before_any_request(
    fast_limits: ConnectorLimits, method: str, path: str
) -> None:
    anything = respx.route().mock(return_value=httpx.Response(200, json={}))
    async with writer(fast_limits) as w:
        with pytest.raises(WriteNotAllowed):
            await w.request(method, path, json_body={"status": 5})
    assert anything.call_count == 0


def test_the_allowlist_is_exactly_reads_and_the_note() -> None:
    allowed = AutotaskNoteWriter._permitted
    assert allowed("GET", "/V1.0/TicketNotes/entityInformation/fields")
    assert allowed("POST", "/V1.0/Tickets/query")
    assert allowed("POST", "/V1.0/Tickets/70345/Notes")
    assert allowed("POST", f"{AUTOTASK_ZONE_URL}/V1.0/Tickets/70345/Notes")
    assert allowed("POST", "/ATServicesRest/V1.0/Tickets/70345/Notes")
    assert not allowed("POST", "/V1.0/Tickets/70345/Notes/1")
    assert not allowed("POST", "/V1.0/Tickets")
    assert not allowed("PATCH", "/V1.0/Tickets/70345/Notes")
    assert not allowed("DELETE", "/V1.0/Tickets/70345/Notes")


# --- the title and the limits -------------------------------------------------------------


@respx.mock
async def test_the_title_starts_with_desk_and_nothing_is_truncated(
    fast_limits: ConnectorLimits,
) -> None:
    mock_fields()
    posted = respx.post(NOTES_URL)
    async with writer(fast_limits) as w:
        with pytest.raises(NoteRefused, match="starts with 'Desk: '"):
            await w.add_internal_note(70345, title="Device back online", body="x")
        with pytest.raises(NoteRefused, match=f"allows {TITLE_MAX}"):
            await w.add_internal_note(70345, title=TITLE_PREFIX + "x" * TITLE_MAX, body="x")
        with pytest.raises(NoteRefused, match=f"allows {BODY_MAX}"):
            await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="x" * (BODY_MAX + 1))
        with pytest.raises(NoteRefused, match="needs a body"):
            await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="   ")
    assert posted.call_count == 0


@respx.mock
async def test_a_refused_post_is_an_upstream_error_not_a_silent_success(
    fast_limits: ConnectorLimits,
) -> None:
    mock_fields()
    # A 403 is the API user lacking the note permission: the base client says
    # so as an AuthError. A 400 is the request being wrong, which is ours.
    respx.post(NOTES_URL).mock(
        side_effect=[
            httpx.Response(403, json={"errors": ["no"]}),
            httpx.Response(400, json={"errors": ["bad"]}),
        ]
    )
    async with writer(fast_limits) as w:
        with pytest.raises(AuthError):
            await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="y")
        with pytest.raises(UpstreamError, match="HTTP 400"):
            await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="y")


@respx.mock
async def test_the_writer_retries_as_the_reader_does(fast_limits: ConnectorLimits) -> None:
    """Same budget and backoff: a 429 is retried, then the note lands once."""
    mock_fields()
    posted = respx.post(NOTES_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"itemId": 5}),
        ]
    )
    async with writer(fast_limits) as w:
        result = await w.add_internal_note(70345, title=f"{TITLE_PREFIX}x", body="y")
    assert result.note_id == "5" and posted.call_count == 2


def test_the_fixture_zone_is_what_the_reader_tests_use() -> None:
    """A guard on the test itself: the zone fixture is shared, so a change to
    it reaches the reader tests and these together."""
    assert load_fixture("autotask", "zone.json")["url"].rstrip("/") == AUTOTASK_ZONE_URL

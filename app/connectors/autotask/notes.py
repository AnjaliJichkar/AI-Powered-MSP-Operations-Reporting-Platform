"""The Autotask note writer: the one write path, and the only thing it can do.

`AutotaskNoteWriter` adds an **internal** note to a ticket and nothing else.
It is the automation engine's, built from a second Autotask API user held per
client under `autotask_notes` (never the read credential), and it is not a
connector the factory builds or a source of tools: `build_note_writer` is the
one way to get one, and the import guard names who may import this module.

What makes it safe, each enforced in code and tested:

* **An allowlist of requests, checked before any HTTP** (`_permitted`): the
  zone lookup, `GET`s (entity information, the picklists), `POST …/query`
  (reads), and `POST /V1.0/Tickets/{id}/Notes`. Any other method or path
  raises `WriteNotAllowed` with no request made — a PATCH, a DELETE, a POST to
  any other entity. There is no second write to allow later; a later write is
  a later class.
* **Internal only, by the tenant's own label, read from field information.**
  Autotask's `publish` picklist is per tenant (live, 2026-10-13: `1 All
  Autotask Users`, `2 Internal Project Team`, `4 Internal & Co-Managed`).
  `All Autotask Users` can be shown on the Client Access Portal, and
  `Internal & Co-Managed` reaches the client's own co-managed technicians,
  so the writer sends only the value whose label is `Internal Project Team`
  — the one option whose audience is internal staff alone — and refuses to
  post when the tenant has no such label rather than guess a number or fall
  back to a wider audience. `noteType` is resolved the same way, to the first
  of `Task Notes`, `Task Detail`, `Task Summary` the tenant has; a type that
  names the client or the portal (`Client Portal Note`) is never chosen. Both
  are read from `TicketNotes/entityInformation/fields` on the tenant being
  written to, never hard-coded, and `tests/connectors/test_autotask_notes.py`
  asserts the sent value against the field information and fails on any other.
* **Recognisable and bounded.** The title starts `Desk:` so engineers can
  filter the notes, and the title and body are refused past Autotask's own
  limits (250 and 32,000 characters) rather than truncated: a cut-off note is
  a note that says something other than what was built.
* **The same budget and backoff as the reader.** It shares `BaseConnector`'s
  retry on 429 and 5xx and is called under the poll's `RequestBudget`.

Nothing here changes a ticket's status, priority, assignment or any field;
the note endpoint cannot, and no other endpoint is reachable.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.autotask.client import AutotaskClient
from app.connectors.base import ConnectorLimits
from app.connectors.errors import ConnectorNotConfigured, UpstreamError
from app.connectors.factory import _load_credential, build_client_scope, resolve_client
from app.models.enums import ConnectorName
from app.security.crypto import get_cipher

#: What a Desk note's title starts with, always.
TITLE_PREFIX = "Desk: "
#: Autotask's own limits on a ticket note (entityInformation/fields, live 2026-10-13).
TITLE_MAX = 250
BODY_MAX = 32_000
#: The one publish label whose audience is internal staff alone. Not `All
#: Autotask Users`, which the Client Access Portal can show, and not `Internal
#: & Co-Managed`, which the client's co-managed technicians see.
INTERNAL_PUBLISH_LABEL = "Internal Project Team"
#: Labels that are never sent, whatever a tenant calls them: anything that
#: names the client, the portal, co-managed users, or "all".
NEVER_PUBLISH_WORDS = ("client", "portal", "co-managed", "comanaged", "all autotask", "customer")
#: The note types a manual internal note may carry, in order of preference.
NOTE_TYPE_LABELS = ("Task Notes", "Task Detail", "Task Summary")
#: Note types never chosen: the ones a client can see or that come from them.
NEVER_NOTE_TYPE_WORDS = ("client", "portal", "customer")

_NOTES_PATH = re.compile(r"^/V1\.0/Tickets/(?P<ticket_id>\d+)/Notes$", re.IGNORECASE)


class WriteNotAllowed(PermissionError):
    """The writer was asked for a request that is not adding a ticket note. No
    request was made."""


class NoteRefused(ValueError):
    """The note cannot be posted as given: a title without the prefix, a body
    over the limit, or a tenant whose picklists have no internal-only value."""


@dataclass(frozen=True, slots=True)
class PostedNote:
    """What Autotask said back: the note's id and the values it was sent with."""

    note_id: str
    ticket_id: str
    publish: int
    note_type: int


class AutotaskNoteWriter(AutotaskClient):
    """Adds one internal note to one ticket. Every other write raises."""

    name: ClassVar[ConnectorName] = ConnectorName.AUTOTASK_NOTES

    # --- the allowlist --------------------------------------------------------------

    @staticmethod
    def _permitted(method: str, path: str) -> bool:
        """The requests this class may make, decided on method and path alone.

        `path` may be absolute (the zone lookup, a page URL) or relative; the
        test is on the part after the host.
        """
        where = httpx.URL(path).path if path.startswith("http") else path
        upper = method.upper()
        if upper == "GET":
            return True
        if upper == "POST":
            lowered = where.lower()
            if lowered.endswith("/query"):
                return True
            # The zone lookup is a GET; the only other POST is the note.
            return bool(_NOTES_PATH.match(_strip_prefix(where)))
        return False

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        authenticated: bool = True,
        follow_redirects: bool | None = None,
    ) -> httpx.Response:
        if not self._permitted(method, path):
            raise WriteNotAllowed(
                f"the note writer may add a ticket note and nothing else; {method.upper()} "
                f"{path} was refused before any request was made"
            )
        return await super().request(
            method,
            path,
            params=params,
            data=data,
            json_body=json_body,
            headers=headers,
            authenticated=authenticated,
            follow_redirects=follow_redirects,
        )

    # --- the picklists that make a note internal -----------------------------------

    async def internal_publish_value(self) -> int:
        """The tenant's value for `Internal Project Team`, read from its field
        information, or refuse. Never a value whose label names the client,
        the portal, co-managed users or "all", whatever number it has."""
        mapping = await self.picklist("TicketNotes", "publish")
        for value, label in mapping.items():
            folded = label.strip().casefold()
            if folded == INTERNAL_PUBLISH_LABEL.casefold() and not any(
                word in folded for word in NEVER_PUBLISH_WORDS
            ):
                return value
        raise NoteRefused(
            f"this tenant's TicketNotes.publish picklist has no {INTERNAL_PUBLISH_LABEL!r} "
            f"value (it has {sorted(mapping.values())}); that is the one option whose "
            f"audience is internal staff alone, a note cannot be posted without it, and no "
            f"number is guessed and no wider audience is used instead"
        )

    async def note_type_value(self) -> int:
        """The first of `NOTE_TYPE_LABELS` the tenant has; never a type that
        names the client or the portal."""
        mapping = await self.picklist("TicketNotes", "noteType")
        by_label = {
            label.strip().casefold(): value
            for value, label in mapping.items()
            if not any(word in label.casefold() for word in NEVER_NOTE_TYPE_WORDS)
        }
        for wanted in NOTE_TYPE_LABELS:
            found = by_label.get(wanted.casefold())
            if found is not None:
                return found
        raise NoteRefused(
            f"this tenant's TicketNotes.noteType picklist has none of {NOTE_TYPE_LABELS}"
        )

    # --- the one write ----------------------------------------------------------------

    async def add_internal_note(self, ticket_id: str | int, *, title: str, body: str) -> PostedNote:
        """Add one internal note to one ticket. Refuses a title without the
        prefix or anything over Autotask's limits; never truncates."""
        if not title.startswith(TITLE_PREFIX):
            raise NoteRefused(f"a Desk note's title starts with {TITLE_PREFIX!r}: {title!r}")
        if len(title) > TITLE_MAX:
            raise NoteRefused(f"the title is {len(title)} characters; Autotask allows {TITLE_MAX}")
        if not body.strip():
            raise NoteRefused("a note needs a body")
        if len(body) > BODY_MAX:
            raise NoteRefused(f"the body is {len(body)} characters; Autotask allows {BODY_MAX}")
        ticket = int(str(ticket_id))
        publish = await self.internal_publish_value()
        note_type = await self.note_type_value()
        # Exactly these five fields. Autotask's TicketNotes entity has no
        # field that sends or suppresses a notification: whether a note added
        # through the API notifies anybody is the tenant's own workflow rules
        # and notification defaults (docs/automation-notes.md), so nothing
        # here can ask for a notification by accident.
        response = await self.request(
            "POST",
            f"/V1.0/Tickets/{ticket}/Notes",
            json_body=note_body(ticket, title, body, note_type=note_type, publish=publish),
        )
        if response.status_code >= 400:
            raise UpstreamError(
                f"Autotask refused the note with HTTP {response.status_code}",
                connector=self.name.value,
                details={"status": response.status_code, "body": response.text[:300]},
            )
        payload = response.json() if response.content else {}
        note_id = payload.get("itemId") if isinstance(payload, dict) else None
        return PostedNote(
            note_id=str(note_id) if note_id is not None else "",
            ticket_id=str(ticket),
            publish=publish,
            note_type=note_type,
        )


def note_body(
    ticket: int, title: str, body: str, *, note_type: int, publish: int
) -> dict[str, Any]:
    """The request body, and the only fields it ever carries."""
    return {
        "ticketID": ticket,
        "title": title,
        "description": body,
        "noteType": note_type,
        "publish": publish,
    }


NOTE_BODY_FIELDS = frozenset({"ticketID", "title", "description", "noteType", "publish"})


def _strip_prefix(path: str) -> str:
    """`/ATServicesRest/V1.0/...` and `/V1.0/...` are the same path."""
    lowered = path.lower()
    marker = "/v1.0/"
    at = lowered.find(marker)
    return "/V1.0/" + path[at + len(marker) :] if at >= 0 else path


async def build_note_writer(
    session: AsyncSession, client_id: str | uuid.UUID
) -> AutotaskNoteWriter:
    """The writer for one client, from its `autotask_notes` credential.

    Not the factory: the factory builds read connectors for tools and refuses
    this name, so nothing that asks the factory for a connector can be handed
    a writer. The demo tenant gets the fake, over the same fake Autotask the
    read connector uses, which records the note in the world.
    """
    client = await resolve_client(session, client_id)
    if client.is_demo:
        from app.demo.connectors import DemoAutotaskNoteWriter, demo_world

        scope = await build_client_scope(session, client, ConnectorName.AUTOTASK)
        return DemoAutotaskNoteWriter(scope=scope, world=demo_world(client.slug))
    credential = await _load_credential(session, client, ConnectorName.AUTOTASK_NOTES)
    scope = await build_client_scope(session, client, ConnectorName.AUTOTASK)
    if not scope.allowed:
        raise ConnectorNotConfigured(
            f"client {client.slug!r} has no autotask sites mapped; a note cannot be scoped",
            connector=ConnectorName.AUTOTASK_NOTES.value,
            details={"client_ref": client.slug},
        )
    return AutotaskNoteWriter(
        scope=scope,
        secrets=credential.get_secrets(cipher=get_cipher()),
        config=credential.config,
        limits=ConnectorLimits.from_settings(connector=ConnectorName.AUTOTASK.value),
        credential_version=f"{credential.encryption_key_id}:{credential.updated_at.isoformat()}",
    )


__all__ = [
    "BODY_MAX",
    "INTERNAL_PUBLISH_LABEL",
    "NEVER_NOTE_TYPE_WORDS",
    "NEVER_PUBLISH_WORDS",
    "NOTE_BODY_FIELDS",
    "NOTE_TYPE_LABELS",
    "TITLE_MAX",
    "TITLE_PREFIX",
    "AutotaskNoteWriter",
    "NoteRefused",
    "PostedNote",
    "WriteNotAllowed",
    "build_note_writer",
    "note_body",
]

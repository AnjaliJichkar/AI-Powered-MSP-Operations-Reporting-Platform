"""Which machine a ticket is about: hostnames in the text, the contact, and no guessing."""

from __future__ import annotations

import datetime as dt
import uuid

from app.models.device import Device
from app.tickets.resolve import (
    Candidate,
    by_contact,
    decide,
    hostnames_in,
    login_key,
    login_shapes,
)

SITE = uuid.uuid4()
NOW = dt.datetime(2026, 9, 25, tzinfo=dt.UTC)


def _device(name: str) -> Device:
    return Device(
        id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        site_id=SITE,
        canonical_name=name,
        netbios_key=name.lower()[:15],
        serials=[],
        macs=[],
        status="active",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def test_hostname_looking_tokens_are_found_and_words_are_not() -> None:
    text = (
        "TL-AUS-WS14 keeps losing Wi-Fi after the Windows update; also srv01.acme.local "
        "and DESKTOP-ABC123. Error 0x80070005. Please help, e-mail me. COVID-19 policy."
    )
    assert hostnames_in("Printer offline", text) == [
        "TL-AUS-WS14",
        "srv01.acme.local",
        "DESKTOP-ABC123",
    ]
    assert hostnames_in("Outlook keeps crashing for everyone") == []


def test_login_shapes_cover_the_usual_conventions() -> None:
    shapes = login_shapes("Sarah O'Connor")
    assert {"sarah.oconnor", "soconnor", "sarahoconnor", "sarah_oconnor", "oconnors"} <= shapes
    assert login_shapes("Bo") == set()  # too short to mean anything
    assert login_key("ACME\\soconnor") == "soconnor"
    assert login_key("sarah.oconnor@acme.local") == "sarah.oconnor"


def test_contact_matches_one_machine_at_medium_and_several_at_low() -> None:
    ws14, ws22, dc = _device("TL-AUS-WS14"), _device("TL-AUS-WS22"), _device("TL-AUS-DC01")
    devices = {"tl-aus-ws14": ws14, "tl-aus-ws22": ws22, "tl-aus-dc01": dc}
    rows = [("TL-AUS-WS14", "ACME\\soconnor"), ("TL-AUS-WS22", "jdoe"), ("TL-AUS-DC01", None)]
    one = by_contact("Sarah O'Connor", rows, devices)
    assert [(c.name, c.confidence, c.method) for c in one] == [("TL-AUS-WS14", "medium", "contact")]
    rows[1] = ("TL-AUS-WS22", "sarah.oconnor@acme.local")
    several = by_contact("Sarah O'Connor", rows, devices)
    assert sorted(c.name for c in several) == ["TL-AUS-WS14", "TL-AUS-WS22"]
    assert {c.confidence for c in several} == {"low"}
    assert by_contact(None, rows, devices) == []


def test_decide_picks_only_a_clear_winner() -> None:
    ids = [str(uuid.uuid4()) for _ in range(4)]
    high = Candidate(ids[0], "WS-A", str(SITE), "hostname", "high", "named")
    medium = Candidate(ids[1], "WS-B", str(SITE), "contact", "medium", "logon")
    low = Candidate(ids[2], "WS-C", str(SITE), "contact", "low", "one of two")
    chosen = decide([medium, high, low], ["WS-A"])
    assert str(chosen.device_id) == ids[0]
    assert (chosen.method, chosen.confidence) == ("hostname", "high")
    assert [c.name for c in chosen.candidates] == ["WS-A", "WS-B", "WS-C"]
    # A clear medium is chosen too, at medium.
    alone = decide([medium, low], [])
    assert str(alone.device_id) == ids[1] and alone.confidence == "medium"
    # Two at the top: nobody's guess.
    other_high = Candidate(ids[3], "WS-D", str(SITE), "hostname", "high", "named too")
    tie = decide([high, other_high], ["WS-A", "WS-D"])
    assert tie.device_id is None and tie.method == "none" and len(tie.candidates) == 2
    # Only low candidates: the engineer decides.
    assert decide([low], []).device_id is None
    assert decide([], []).method == "none"

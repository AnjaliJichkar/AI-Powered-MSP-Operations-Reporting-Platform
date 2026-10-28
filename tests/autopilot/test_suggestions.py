"""What a suggestion shows, what it hides, and what it says, without a database.

The display threshold and the blind draw are both decisions that have to be the
same every time a page is opened, so both are pinned here against rows built by
hand. The wording is pinned too: a card an engineer reads in two seconds is a
card whose sentences are part of the contract.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.autopilot.findings import Finding
from app.autopilot.suggestions import (
    NOTE_LIMIT,
    is_blind,
    note_text,
    shown_fields,
)
from app.models.shadow import ShadowEnrichment
from app.models.suggestion import TicketSuggestion

CLIENT = uuid.UUID("11111111-1111-4111-8111-111111111111")
NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.UTC)


def suggestion(**over: object) -> TicketSuggestion:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "client_id": CLIENT,
        "run_id": uuid.uuid4(),
        "ticket_id": "70345",
        "ticket_number": "T20261002.0007",
        "issue_type": "User Access and Management",
        "issue_type_confidence": 0.91,
        "origin": "alert",
        "origin_confidence": 0.95,
        "urgency": "high",
        "urgency_confidence": 0.84,
        "group_with": None,
        "group_with_number": None,
        "group_score": None,
        "display_threshold": 0.80,
        "blind_check": False,
        "created_at": NOW,
    }
    fields.update(over)
    return TicketSuggestion(**fields)


def enrichment(facts: list[dict[str, object]], **over: object) -> ShadowEnrichment:
    fields: dict[str, object] = {
        "run_id": uuid.uuid4(),
        "ticket_id": "70345",
        "device_method": "hostname",
        "read_at": NOW - dt.timedelta(minutes=5),
        "facts": facts,
        "fact_count": len(facts),
        "reads": 3,
        "errors": [],
        "not_assessed": {},
    }
    fields.update(over)
    return ShadowEnrichment(**fields)


# --- the threshold ----------------------------------------------------------


def test_a_field_under_the_threshold_is_not_shown_at_all() -> None:
    """Not shown with a caveat: not shown. An agent that is not sure is quiet."""
    shown = shown_fields(suggestion(issue_type_confidence=0.62))
    assert "issue_type" not in shown
    assert set(shown) == {"origin", "urgency"}


def test_the_threshold_on_the_row_is_the_one_that_decides() -> None:
    """A suggestion made under one bar is not promoted by somebody lowering it.

    The setting can move; what was shown to an engineer at the time cannot, and
    a figure counted later has to be able to say which bar applied.
    """
    strict = suggestion(display_threshold=0.95)
    assert set(shown_fields(strict)) == {"origin"}


def test_an_unsure_answer_is_never_shown_however_confident() -> None:
    """`unsure` is a decline. "Looks like unsure" is not a suggestion."""
    assert "issue_type" not in shown_fields(
        suggestion(issue_type="unsure", issue_type_confidence=0.99)
    )


def test_a_repeat_is_shown_whenever_there_is_one() -> None:
    """Grouping is deterministic and has no confidence to clear."""
    shown = shown_fields(suggestion(group_with="70001", group_score=0.84))
    assert "repeat" in shown


# --- which tickets ask first ------------------------------------------------


def test_the_blind_draw_is_the_same_every_time_it_is_asked() -> None:
    """A coin flipped per page load would show the questions, then the answers
    on a refresh, and the blind answer would be worth nothing."""
    answers = {is_blind(CLIENT, "70345", 0.2) for _ in range(50)}
    assert len(answers) == 1


def test_the_blind_draw_lands_near_the_rate_over_many_tickets() -> None:
    """Not a uniformity proof — a sanity check that 1 in 5 is about 1 in 5 and
    not every ticket or none, which a bad hash would give."""
    drawn = sum(1 for number in range(2000) if is_blind(CLIENT, str(number), 0.2))
    assert 300 < drawn < 500


def test_a_rate_of_zero_asks_nobody_and_one_asks_everybody() -> None:
    assert not is_blind(CLIENT, "70345", 0.0)
    assert is_blind(CLIENT, "70345", 1.0)


def test_two_clients_do_not_draw_the_same_tickets() -> None:
    """The hash is over the client as well, so one franchisee's blind set is
    not another's — the same ticket numbering would otherwise line them up."""
    other = uuid.UUID("22222222-2222-4222-8222-222222222222")
    mine = [is_blind(CLIENT, str(n), 0.2) for n in range(200)]
    theirs = [is_blind(other, str(n), 0.2) for n in range(200)]
    assert mine != theirs


# --- the note ---------------------------------------------------------------


def finding(**over: object) -> Finding:
    fields: dict[str, object] = {
        "kind": "device_offline",
        "rank": 1,
        "tone": "warning",
        "text": "Machine offline for 10 days",
        "chip": "Offline 10 days",
        "detail": "last check-in 21 Sep",
        "source": "NinjaOne",
    }
    fields.update(over)
    return Finding(**fields)  # type: ignore[arg-type]


def test_the_note_is_the_cards_own_order_and_says_what_it_is() -> None:
    """The machine, what was found, what to do, then the classification — the
    order the card reads in, because whoever opens the ticket next needs the
    estate before they need a label."""
    note = note_text(
        suggestion(),
        machine={"device": "WS-LT-07"},
        findings=[finding(), finding(kind="pending_reboot", text="Pending reboot", detail=None)],
        next_step="Contact the user to bring the machine online before any remote cleanup.",
        classification=[
            {"label": "Issue type", "reads": "User Access and Management"},
            {"label": "Raised by", "reads": "A tool (alert)"},
        ],
    )
    lines = note.splitlines()
    assert lines[0].startswith("T20261002.0007 — Desk suggestion")
    assert lines[1] == "Machine: WS-LT-07"
    assert lines[2] == "- Machine offline for 10 days (last check-in 21 Sep) [NinjaOne]"
    assert lines[3] == "- Pending reboot [NinjaOne]"
    assert "Suggested next step: Contact the user" in note
    assert note.index("Machine offline") < note.index("Issue type:")
    assert "Issue type: User Access and Management" in note
    assert "Nothing here was applied to the ticket automatically." in note
    assert len(note) <= NOTE_LIMIT


def test_a_note_with_nothing_to_say_is_still_honest() -> None:
    note = note_text(suggestion(), machine={"device": None}, findings=[], classification=[])
    assert "Machine:" not in note
    assert "Suggestions only." in note


def test_a_very_long_note_is_cut_to_the_limit_rather_than_sent_whole() -> None:
    """A paste silently truncated at the vendor is a note that lies about what
    we found."""
    note = note_text(
        suggestion(),
        findings=[finding(text="x" * 40_000)],
    )
    assert len(note) == NOTE_LIMIT

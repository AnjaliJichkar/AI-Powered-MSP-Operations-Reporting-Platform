"""Trimming, assembling and finalising a thread. Pure functions: no database, no model."""

from __future__ import annotations

import copy
from typing import Any

from app.agent.conversations import (
    TurnMessage,
    assemble_messages,
    estimate_tokens,
    finalise_turn,
    split_turns,
    title_from,
    trim_history,
)

Message = dict[str, Any]


def turn(number: int, *, result_chars: int = 0, answer_chars: int = 40) -> list[Message]:
    """One turn: a question, and — when `result_chars` is set — a tool round trip."""
    messages: list[Message] = [
        {"role": "user", "content": [{"type": "text", "text": f"question {number}"}]}
    ]
    if result_chars:
        messages += [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_{number}",
                        "name": "list_devices",
                        "input": {},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"toolu_{number}",
                        "content": "x" * result_chars,
                    }
                ],
            },
        ]
    messages.append(
        {"role": "assistant", "content": [{"type": "text", "text": "a" * answer_chars}]}
    )
    return messages


def history(turns: int, **kwargs: int) -> list[Message]:
    return [message for number in range(1, turns + 1) for message in turn(number, **kwargs)]


def result_bodies(messages: list[Message]) -> list[str]:
    return [
        block["content"]
        for message in messages
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]


def tool_use_ids(messages: list[Message]) -> set[str]:
    return {
        block["id"]
        for message in messages
        for block in message["content"]
        if block.get("type") == "tool_use"
    }


def answered_ids(messages: list[Message]) -> set[str]:
    return {
        block["tool_use_id"]
        for message in messages
        for block in message["content"]
        if block.get("type") == "tool_result"
    }


# --- turns ----------------------------------------------------------------


def test_a_tool_result_message_belongs_to_the_turn_that_asked_for_it() -> None:
    turns = split_turns(history(3, result_chars=10))
    assert [len(t) for t in turns] == [4, 4, 4]
    assert all(t[0]["content"][0]["text"].startswith("question") for t in turns)


# --- trimming -------------------------------------------------------------


def test_a_120k_token_history_loses_its_oldest_tool_results_first() -> None:
    """Ten turns of ~12k tokens each. Budget 80k, newest four protected."""
    messages = history(10, result_chars=48_000)
    assert 118_000 < estimate_tokens(messages) < 125_000

    trimmed, report = trim_history(messages, budget=80_000, keep_recent_turns=4)

    assert report.trimmed
    assert report.turns_total == 10
    assert report.turns_dropped == 0  # stubbing was enough
    assert report.results_elided == 4  # 120k -> 72k: the first four, oldest first
    assert report.tokens_before > 80_000 >= report.tokens_after

    bodies = result_bodies(trimmed)
    assert bodies[:4] == ["[result elided, 48000 chars]"] * 4
    assert all(body == "x" * 48_000 for body in bodies[4:])
    # The tool_use block stays: the model still sees what was asked and answered.
    assert tool_use_ids(trimmed) == {f"toolu_{n}" for n in range(1, 11)}
    assert tool_use_ids(trimmed) == answered_ids(trimmed)


def test_the_newest_four_turns_are_never_touched_even_when_over_budget() -> None:
    messages = history(6, result_chars=48_000)

    trimmed, report = trim_history(messages, budget=10_000, keep_recent_turns=4)

    # Both old turns were stubbed, then dropped; the last four are intact.
    assert report.results_elided == 2
    assert report.turns_dropped == 2
    assert trimmed == history(6, result_chars=48_000)[8:]
    assert estimate_tokens(trimmed) > 10_000  # still over: protection wins over the budget


def test_whole_oldest_turns_are_dropped_when_stubbing_is_not_enough() -> None:
    """Long answers, no tool results: nothing to stub, so old turns go."""
    messages = history(10, answer_chars=40_000)

    trimmed, report = trim_history(messages, budget=50_000, keep_recent_turns=4)

    assert report.results_elided == 0
    assert report.turns_dropped == 6  # 10 turns of ~10k; five would still be over
    assert len(split_turns(trimmed)) == 4
    # What is left still opens with a question and never orphans a result.
    assert trimmed[0]["role"] == "user"
    assert trimmed[0]["content"][0]["text"] == "question 7"
    assert answered_ids(trimmed) <= tool_use_ids(trimmed)


def test_a_history_inside_the_budget_is_left_alone() -> None:
    messages = history(10, result_chars=100)
    trimmed, report = trim_history(messages, budget=80_000, keep_recent_turns=4)
    assert trimmed == messages
    assert not report.trimmed
    assert report.tokens_before == report.tokens_after


def test_trimming_never_edits_what_it_was_given() -> None:
    """The stored thread is what is passed in; only the replayed copy changes."""
    messages = history(10, result_chars=48_000)
    original = copy.deepcopy(messages)
    trim_history(messages, budget=80_000, keep_recent_turns=4)
    assert messages == original


# --- assembling -----------------------------------------------------------


def test_with_no_history_the_request_is_the_single_message_it_always_was() -> None:
    assembled = assemble_messages([], client_context="CONTEXT", question="how many?")
    assert assembled == [{"role": "user", "content": "CONTEXT\n\nQuestion: how many?"}]


def test_history_is_replayed_with_the_context_first_and_a_cache_breakpoint_last() -> None:
    past = history(2, result_chars=10)
    original = copy.deepcopy(past)

    assembled = assemble_messages(past, client_context="CONTEXT", question="and now?")

    assert assembled[0]["content"][0] == {"type": "text", "text": "CONTEXT"}
    assert assembled[0]["content"][1] == {"type": "text", "text": "question 1"}
    assert assembled[-1] == {"role": "user", "content": [{"type": "text", "text": "and now?"}]}
    # The breakpoint sits on the last historical message, before the new turn.
    assert assembled[-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    marked = [
        block
        for message in assembled
        for block in (message["content"] if isinstance(message["content"], list) else [])
        if "cache_control" in block
    ]
    assert len(marked) == 1
    assert past == original  # the stored history is not modified by assembling it


# --- finalising a turn ----------------------------------------------------


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def use(tool_id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": "demo_list", "input": {}}


def answered(tool_id: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "{}"}


def test_a_complete_turn_is_stored_as_it_happened() -> None:
    transcript = [
        TurnMessage("assistant", [use("t1")], 100, 20),
        TurnMessage("user", [answered("t1")]),
        TurnMessage("assistant", [text("Two devices.")], 150, 30),
    ]
    stored = finalise_turn("how many?", transcript, "Two devices.")

    assert [(m.role, m.content) for m in stored] == [
        ("user", [text("how many?")]),
        ("assistant", [use("t1")]),
        ("user", [answered("t1")]),
        ("assistant", [text("Two devices.")]),
    ]
    assert (stored[1].input_tokens, stored[1].output_tokens) == (100, 20)


def test_an_unanswered_tool_use_is_never_stored() -> None:
    """The cap or the clock stopped the run before the result came back."""
    transcript = [
        TurnMessage("assistant", [use("t1")]),
        TurnMessage("user", [answered("t1")]),
        TurnMessage("assistant", [text("checking alerts"), use("t2")]),
    ]
    stored = finalise_turn("q", transcript, "I stopped after 1 tool calls.")

    ids_used = tool_use_ids([{"content": m.content} for m in stored])
    ids_answered = answered_ids([{"content": m.content} for m in stored])
    assert ids_used == ids_answered == {"t1"}
    # The engineer was shown the partial answer, so that is how the turn closes.
    assert stored[-1].role == "assistant"
    assert stored[-1].content == [text("I stopped after 1 tool calls.")]


def test_a_paused_turn_and_its_resumption_are_stored_as_one_assistant_message() -> None:
    search = {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "q"}}
    found = {"type": "web_search_tool_result", "tool_use_id": "s1", "content": []}
    transcript = [
        TurnMessage("assistant", [search, found], 10, 5),
        TurnMessage("assistant", [text("Per the vendor…")], 20, 7),
    ]
    stored = finalise_turn("q", transcript, "Per the vendor…")

    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[1].content == [search, found, text("Per the vendor…")]
    assert (stored[1].input_tokens, stored[1].output_tokens) == (30, 12)


def test_a_turn_the_model_never_answered_still_closes_with_what_was_shown() -> None:
    """A refusal, an API error, a max_tokens stop: nothing was appended by the loop."""
    stored = finalise_turn("q", [], "The model declined to answer this question.")
    assert [(m.role, m.content) for m in stored] == [
        ("user", [text("q")]),
        ("assistant", [text("The model declined to answer this question.")]),
    ]


def test_a_title_is_the_question_on_one_line_and_is_never_cut_short() -> None:
    """A thread's title is what the engineer typed, flattened to one line.

    It used to stop at eighty characters and an ellipsis — in the database,
    which is the one place a shortening cannot be undone, since no page could
    then get the rest of the question back. The rail shortens it to two lines in
    CSS and carries the whole of it in the button's tooltip.
    """
    assert title_from("  which   devices\nare offline? ") == "which devices are offline?"
    long = title_from("x" * 200)
    assert long == "x" * 200
    assert "…" not in long

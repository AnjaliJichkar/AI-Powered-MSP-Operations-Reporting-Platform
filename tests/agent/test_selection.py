"""The tool-selection check's own logic, and the committed question file.

No model and no database: the harness itself (`scripts/selection_check.py`)
calls the real model and is run by hand. What is tested here is that a question
file cannot silently rot, and that the score means what it says.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import app.connectors.autotask.tools
import app.connectors.ninjaone.tools  # noqa: F401 - registers the tools
from app.agent.registry import registered_tools
from app.agent.selection import (
    QuestionFileError,
    SelectionOutcome,
    SelectionQuestion,
    parse_questions,
    score,
    summarise,
)

QUESTIONS = Path(__file__).resolve().parents[1] / "selection" / "questions.yaml"
KNOWN = ["list_devices", "count_devices", "search_tickets"]


def test_the_committed_question_file_names_only_registered_tools() -> None:
    """Rename or remove a tool and this fails, rather than the model being blamed for a miss."""
    data = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))

    questions = parse_questions(data, known_tools=[tool.name for tool in registered_tools()])

    assert len(questions) >= 15
    assert len({question.question for question in questions}) == len(questions)


def test_expect_may_be_one_tool_or_several() -> None:
    questions = parse_questions(
        [
            {"question": "how many servers?", "expect": "count_devices"},
            {"question": "seen this?", "expect": ["search_tickets", "list_devices"], "note": "x"},
        ],
        known_tools=KNOWN,
    )

    assert questions[0].expect == ("count_devices",)
    assert questions[1].expect == ("search_tickets", "list_devices")
    assert questions[1].note == "x"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "non-empty list"),
        ({"question": "x"}, "non-empty list"),
        (["just a string"], "expected `question:`"),
        ([{"question": "", "expect": "count_devices"}], "non-empty string"),
        ([{"question": "x"}], "`expect` must be"),
        ([{"question": "x", "expect": []}], "`expect` must be"),
        ([{"question": "x", "expect": "list_patch_failures"}], "not a registered tool"),
        ([{"question": "x", "expect": "count_devices", "expected": "y"}], "unknown key"),
    ],
)
def test_a_malformed_question_file_is_refused(data: object, message: str) -> None:
    with pytest.raises(QuestionFileError, match=message):
        parse_questions(data, known_tools=KNOWN)


def test_only_the_first_tool_counts() -> None:
    question = SelectionQuestion("how many servers?", ("count_devices",))

    assert score(question, ["count_devices", "list_devices"]) == ("count_devices", True)
    assert score(question, ["list_devices", "count_devices"]) == ("list_devices", False)
    assert score(question, ["web_search"]) == ("web_search", False)
    assert score(question, []) == (None, False)  # answered without looking anything up


def test_a_site_question_is_filled_in_or_flagged() -> None:
    question = SelectionQuestion("which services are stopped at {site}?", ("list_devices",))

    assert question.needs_site
    assert question.text_for("acme-dental") == "which services are stopped at acme-dental?"
    assert not SelectionQuestion("what is alerting?", ("list_devices",)).needs_site


def test_the_summary_line_and_threshold() -> None:
    def outcome(matched: bool, **extra: object) -> SelectionOutcome:
        return SelectionOutcome(question="q", expect=("t",), matched=matched, **extra)  # type: ignore[arg-type]

    nine = [outcome(True)] * 9 + [outcome(False)]
    summary = summarise(nine, tool_count=29)
    assert summary.line() == "9 of 10 correct at 29 tools"
    assert summary.passed

    eight = [outcome(True)] * 8 + [outcome(False), outcome(False, error="APIStatusError")]
    summary = summarise([*eight, outcome(False, skipped="needs a site")], tool_count=29)
    assert summary.line() == "8 of 10 correct at 29 tools"  # skipped is not asked
    assert (summary.skipped, summary.errors) == (1, 1)  # an error is a miss, not a skip
    assert not summary.passed

    assert not summarise([], tool_count=29).passed  # nothing asked proves nothing

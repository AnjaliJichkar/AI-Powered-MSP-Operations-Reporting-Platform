"""Scoring for the tool-selection check (`scripts/selection_check.py`).

The question being measured: given an engineer's question, does the model
reach for the right tool *first*? With tools that sound alike, that is the
failure that grows as tools are added, and it is invisible in unit tests. This
module holds the parts that need no database and no model: reading the question
file, scoring one run, and the summary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Anthropic's server-side web search, as the model names it in a `server_tool_use`
#: block. Not in our registry, and a valid expectation all the same.
WEB_SEARCH_TOOL_NAME = "web_search"

SITE_PLACEHOLDER = "{site}"

#: The share of questions that must pick the right tool first. Below this the
#: tool list, not the model, is what needs work (CLAUDE.md, "Agent loop").
PASS_RATIO = 0.9


class QuestionFileError(ValueError):
    """The question file is not in the shape the harness reads."""


@dataclass(frozen=True, slots=True)
class SelectionQuestion:
    question: str
    expect: tuple[str, ...]
    note: str | None = None

    @property
    def needs_site(self) -> bool:
        return SITE_PLACEHOLDER in self.question

    def text_for(self, site: str | None) -> str:
        return self.question.replace(SITE_PLACEHOLDER, site) if site else self.question


@dataclass(slots=True)
class SelectionOutcome:
    """What one question produced."""

    question: str
    expect: tuple[str, ...]
    first_tool: str | None = None
    """The first tool the model asked for; None if it answered without one."""
    also_requested: list[str] = field(default_factory=list)
    """Any further tools requested in that same first response."""
    matched: bool = False
    status: str = ""
    duration_ms: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    audit_log_id: int | None = None
    error: str | None = None
    skipped: str | None = None
    """Why the question was not asked, e.g. it needs a site and none was given."""

    @property
    def tokens_read(self) -> int:
        """Everything the model read: fresh input, cache reads and cache writes."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "expect": list(self.expect),
            "first_tool": self.first_tool,
            "also_requested": list(self.also_requested),
            "matched": self.matched,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "audit_log_id": self.audit_log_id,
            "error": self.error,
            "skipped": self.skipped,
        }


def parse_questions(data: Any, *, known_tools: Iterable[str]) -> list[SelectionQuestion]:
    """Validate the parsed question file.

    An expected tool that is not registered is an error, not a miss: a renamed
    or removed tool would otherwise turn into a question the model can never
    get right, and the score would quietly blame the model for it. The one name
    that is not in the registry and is still valid is `web_search`: it is
    Anthropic's server-side tool, the loop offers it, and `first_tools` records
    it like any other, so a question whose right first move is the world outside
    the tenant can be written down (`WEB_SEARCH_TOOL_NAME`).
    """
    known = set(known_tools) | {WEB_SEARCH_TOOL_NAME}
    if not isinstance(data, list) or not data:
        raise QuestionFileError("the question file must be a non-empty list of entries")

    questions: list[SelectionQuestion] = []
    for index, entry in enumerate(data, start=1):
        if not isinstance(entry, Mapping):
            raise QuestionFileError(f"entry {index}: expected `question:` and `expect:` keys")
        unknown_keys = set(entry) - {"question", "expect", "note"}
        if unknown_keys:
            raise QuestionFileError(f"entry {index}: unknown key(s) {sorted(unknown_keys)}")
        text = entry.get("question")
        if not isinstance(text, str) or not text.strip():
            raise QuestionFileError(f"entry {index}: `question` must be a non-empty string")
        raw = entry.get("expect")
        names = [raw] if isinstance(raw, str) else raw
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(name, str) and name for name in names)
        ):
            raise QuestionFileError(
                f"entry {index}: `expect` must be a tool name or a list of tool names"
            )
        missing = [name for name in names if name not in known]
        if missing:
            raise QuestionFileError(
                f"entry {index} ({text.strip()!r}): expects {missing}, which is not a "
                "registered tool. Was it renamed or removed?"
            )
        note = entry.get("note")
        questions.append(
            SelectionQuestion(
                question=text.strip(),
                expect=tuple(names),
                note=str(note).strip() if note else None,
            )
        )
    return questions


def score(question: SelectionQuestion, first_tools: Sequence[str]) -> tuple[str | None, bool]:
    """The first tool asked for, and whether it is an expected one.

    Only the first counts. A model that asks for three tools at once and has
    the right one third has still reached for the wrong thing first.
    """
    first = first_tools[0] if first_tools else None
    return first, first in question.expect


@dataclass(frozen=True, slots=True)
class SelectionSummary:
    correct: int
    asked: int
    skipped: int
    errors: int
    tool_count: int

    @property
    def ratio(self) -> float:
        return self.correct / self.asked if self.asked else 0.0

    @property
    def passed(self) -> bool:
        return self.asked > 0 and self.ratio >= PASS_RATIO

    def line(self) -> str:
        """The line the brief asks for: N of M correct at T tools."""
        return f"{self.correct} of {self.asked} correct at {self.tool_count} tools"


def summarise(outcomes: Sequence[SelectionOutcome], *, tool_count: int) -> SelectionSummary:
    """Skipped questions are left out of M; a run that errored counts as a miss."""
    asked = [outcome for outcome in outcomes if outcome.skipped is None]
    return SelectionSummary(
        correct=sum(1 for outcome in asked if outcome.matched),
        asked=len(asked),
        skipped=len(outcomes) - len(asked),
        errors=sum(1 for outcome in asked if outcome.error),
        tool_count=tool_count,
    )

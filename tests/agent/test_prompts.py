"""The system prompt, against the tool registry it is supposed to steer.

A tool the prompt never names is a tool the model reaches for by luck. That is
not theory: the five Microsoft 365 tools were registered, offered and
documented, and four of the five selection questions about them were answered
with no tool call at all, because the prompt's "which tool answers which
question" map stopped at ConnectSecure (validation log, 2026-10-14).
"""

from __future__ import annotations

import app.connectors  # noqa: F401 - imports every connector, which registers its tools
from app.agent.prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from app.agent.registry import registered_tools

#: Tools the map deliberately does not name. The coverage pair is answered by
#: the prompt in prose instead ("compare `list_devices` with `count_endpoints`"),
#: which predates them; adding them to the map is a change to how coverage
#: questions are answered and needs its own selection questions and run, rather
#: than being slipped in by a test. Everything else must be named.
NOT_IN_THE_MAP = {"count_coverage", "list_coverage_gaps"}


def test_every_tool_is_named_in_the_prompt() -> None:
    names = {tool.name for tool in registered_tools()}
    missing = sorted(name for name in names - NOT_IN_THE_MAP if name not in SYSTEM_PROMPT)

    assert missing == [], (
        f"these tools are offered to the model and named nowhere in the prompt: {missing}. "
        "Add them to the question map, or to NOT_IN_THE_MAP with the reason."
    )


def test_the_exemptions_are_real_tools() -> None:
    """A stale exemption would hide the next connector that lands unmentioned."""
    names = {tool.name for tool in registered_tools()}

    assert NOT_IN_THE_MAP <= names


def test_the_prompt_says_a_name_it_does_not_know_is_looked_up() -> None:
    """The rule that stopped the model deciding, from an email domain, that a
    person does not exist. It holds a list of customers and of nothing else."""
    # Read with the wrapping taken out: the prompt is authored as continued
    # lines, so a sentence in the file is not one run of characters in it.
    text = " ".join(SYSTEM_PROMPT.split())
    assert "a reason to look, not a reason to refuse" in text
    assert "that list is complete" in text
    assert "Look up whatever address the engineer gives you" in text


def test_the_version_is_bumped_with_the_text() -> None:
    """Not a test of the text, a test of the discipline: a prompt an audit row
    names must be the prompt that produced it."""
    assert SYSTEM_PROMPT_VERSION == "v16"

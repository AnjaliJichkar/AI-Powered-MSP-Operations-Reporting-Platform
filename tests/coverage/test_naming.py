"""A board key is not a connector name, and the code may not pretend it is.

A board entry is named for what it measures; a vendor record is keyed by the
connector that wrote it. For GravityZone and ConnectSecure the two words are
the same, which is why nothing enforced the difference for a year — and when
Backup arrived, reading Cove, the same confusion was written five times: links
keyed by the board key so every Cove record looked new, two comparisons in the
identity reader that silently matched nothing and reported 0% coverage where
Cove held a device for every machine, a `ConnectorName("backup")` that raises,
and a Devices column asserting a gap on a tool a customer does not own.

Two guards, because each catches what the other cannot:

* the **behavioural** one proves the hazard is real and the helper handles it,
  so the mapping cannot quietly lose an entry;
* the **source** one fails on the shapes the five bugs were written in, so the
  sixth is caught in review rather than on a board somebody is reading.
"""

from __future__ import annotations

import pathlib
import re

from app.connectors.cove.models import CoveDevice  # noqa: F401  (import order: see below)
from app.coverage.config import COVERAGE_TOOLS
from app.coverage.naming import (
    BOARD_KEY_OF,
    CONNECTOR_OF,
    board_key_of,
    connector_name_of,
    connector_of,
)
from app.models.enums import ConnectorName

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
APP = ROOT / "app"

#: The one module allowed to hold the mapping. Everything else asks it.
OWNER = APP / "coverage" / "naming.py"


def test_the_hazard_is_real_and_the_helper_covers_it() -> None:
    """At least one board key is not a connector name, and `ConnectorName` says so.

    If this ever stops being true the helper is still right, but the reason
    for it has gone — and a test that passes because the problem disappeared
    should say so rather than quietly agree.
    """
    differing = [t for t in COVERAGE_TOOLS if t.connector and t.key != t.connector.value]
    assert differing, "no board entry differs from its connector; this guard has no subject"
    for tool in differing:
        # The shape that raised: `ConnectorName("backup")`.
        with __import__("pytest").raises(ValueError):
            ConnectorName(tool.key)
        # And what to write instead.
        assert connector_of(tool.key) is tool.connector
        assert connector_name_of(tool.key) == tool.connector.value
        assert board_key_of(tool.connector) == tool.key


def test_every_entry_with_a_connector_translates_both_ways() -> None:
    for tool in COVERAGE_TOOLS:
        if tool.connector is None:
            # An entry with no reader yet: `None`, not a guess. A caller given
            # a connector name for one would look for links nothing wrote.
            assert connector_of(tool.key) is None
            continue
        assert CONNECTOR_OF[tool.key] is tool.connector
        assert BOARD_KEY_OF[tool.connector] == tool.key
    # NinjaOne is the population this board is about, not a tool measured
    # against it, so nothing maps to it.
    assert board_key_of(ConnectorName.NINJAONE) is None


#: The shapes the five bugs were written in. Each is a way of turning one
#: namespace into the other by hand.
FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"ConnectorName\(\s*(?:tool_key|tool\.key|key|tool)\s*\)"),
        "ConnectorName(<a board key>) raises on 'backup'; use connector_of()",
    ),
    (
        re.compile(r"connector_name\.value\s*==\s*(?:tool|tool_key|key)\b"),
        "comparing a link's connector against a board key matches nothing for "
        "Backup; translate with connector_name_of() first",
    ),
    (
        re.compile(r"connector_name\s*==\s*(?:tool\.key|tool_key)\b"),
        "same, with the enum: a board key is not a ConnectorName",
    ),
    (
        re.compile(r"\{\s*tool\.key:\s*tool\.connector"),
        "the mapping belongs in app/coverage/naming.py, not a second copy",
    ),
)


def test_no_module_turns_a_board_key_into_a_connector_by_hand() -> None:
    offences: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if path == OWNER:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue  # a comment may name the shape it is warning about
            for pattern, why in FORBIDDEN:
                if pattern.search(line):
                    offences.append(f"{path.relative_to(ROOT)}:{number}: {why}")
    assert not offences, "\n".join(offences)

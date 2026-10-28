"""Every page scrolls to its last element, in a real browser.

This is the one test in the suite that needs something the project does not
depend on: a browser, and a running server with a session to sign in with. Both
are named by environment variables and the test skips without them, so `pytest`
is unchanged for everybody who has not set them up and CI is unaffected.

    SCROLL_CHECK_BASE_URL=http://127.0.0.1:8099 \\
    SCROLL_CHECK_EMAIL=... SCROLL_CHECK_PASSWORD=... \\
    SCROLL_CHECK_CLIENT=<slug> \\
    python -m pytest tests/browser -q

What it is for: most pages scroll the window, and Ask does not — its frame is
exactly the viewport, the conversation scrolls inside its own column and the
question box sits on the bottom edge of the screen. A flex or grid child that
forgets `min-height: 0` stops overflowing and grows past its frame instead,
taking its last paragraph off the bottom of the screen with no scrollbar and no
error. Nothing in a unit test can see it; only a browser with a real layout can.

Verified to fail, not only to pass. With the Ask frame left unbounded — the
shell on `min-height` with the window taking the scrollbar, which is what every
other page wants and what Ask had — the conversation check reports *the frame
scrolls instead of the conversation* and *the question box leaves the screen*,
measured at 1440x900 with a twelve-turn conversation 2,508px tall in a 900px
window and the composer 2,750px down the document.
"""

from __future__ import annotations

import os

import pytest
from scripts.check_page_scrolling import PAGES, WIDTHS, AskResult, NoBrowser, Result, run_all

BASE = os.environ.get("SCROLL_CHECK_BASE_URL")
EMAIL = os.environ.get("SCROLL_CHECK_EMAIL")
PASSWORD = os.environ.get("SCROLL_CHECK_PASSWORD")
CLIENT = os.environ.get("SCROLL_CHECK_CLIENT")

pytestmark = pytest.mark.skipif(
    not (BASE and EMAIL and PASSWORD),
    reason=(
        "needs a running server and a browser: set SCROLL_CHECK_BASE_URL, "
        "SCROLL_CHECK_EMAIL and SCROLL_CHECK_PASSWORD (SCROLL_CHECK_CLIENT too "
        "for a staff account, and BROWSE_BIN if the automation command is not "
        "in the usual place)"
    ),
)


@pytest.fixture(scope="module")
def checked() -> tuple[list[Result], list[AskResult]]:
    """One pass over every page at every width. Module-scoped because it drives
    a browser: forty-odd page loads is enough to do once."""
    assert BASE and EMAIL and PASSWORD
    try:
        return run_all(BASE, EMAIL, PASSWORD, CLIENT)
    except NoBrowser as exc:
        pytest.skip(str(exc))


def test_every_page_and_width_was_checked(
    checked: tuple[list[Result], list[AskResult]],
) -> None:
    results, asks = checked
    assert len(results) == len(PAGES) * len(WIDTHS)
    assert len(asks) == len(WIDTHS)


def test_the_last_element_of_every_page_can_be_scrolled_into_view(
    checked: tuple[list[Result], list[AskResult]],
) -> None:
    """The failure this exists for: a page whose bottom cannot be reached."""
    results, _ = checked
    unreachable = [f"{r.path} at {r.width}px" for r in results if not r.reachable]
    assert not unreachable, "these pages cannot be scrolled to their end: " + ", ".join(unreachable)


def test_nothing_is_clipped_instead_of_scrolling(
    checked: tuple[list[Result], list[AskResult]],
) -> None:
    """A box whose content is taller than it is, with the overflow hidden rather
    than scrolled or spilled. That content is unreachable by any means."""
    results, _ = checked
    clipped = [f"{r.path} at {r.width}px: {', '.join(r.clipped)}" for r in results if r.clipped]
    assert not clipped, "content is cut off rather than scrolling: " + "; ".join(clipped)


def test_a_long_conversation_can_be_read_from_both_ends(
    checked: tuple[list[Result], list[AskResult]],
) -> None:
    """Ask is the one page read in both directions, so both ends are checked.

    A conversation whose first answer cannot be scrolled back to is an answer
    that was never given; and the question box has to stay on the screen while
    either end is being read, which is the whole reason this page's frame is
    the viewport rather than the document."""
    _, asks = checked
    faults = [f"at {a.width}px: {'; '.join(a.faults)}" for a in asks if not a.ok]
    assert not faults, "the conversation cannot be read: " + ", ".join(faults)

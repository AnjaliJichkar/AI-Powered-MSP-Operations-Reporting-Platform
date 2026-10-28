"""The shell works, in a real browser: the switcher, the nav, the width.

The second test in the suite that needs something the project does not depend
on — a browser and a running server with an account to sign in with — so it
skips without them and `pytest` is unchanged for everybody else:

    SHELL_CHECK_BASE_URL=http://127.0.0.1:8099 \\
    SHELL_CHECK_EMAIL=... SHELL_CHECK_PASSWORD=... \\
    python -m pytest tests/browser -q

The account must be Inputiv staff, because a franchisee has one client and no
switcher at all.

What it is for: the Aurora shell landed with three faults that nothing in this
suite could see, because every one of them was a page that rendered without an
error. The switcher's button was wired to markup that no longer existed, so
pressing it did nothing — no menu, no request, no message in the console. The
nav drew one glyph on every row, because the icon macro fell back to a default
when a name it did not have was asked for. And the coverage table squeezed its
columns to the box instead of scrolling, so at 1280 a column header read
`ConnectSeci` with no scrollbar to say there was more.

Verified to fail, not only to pass: with `#client-menu` taken back out of
`_base.html` this reports `EMPTY the switcher`; with `.mobile-menu
{ display: none }` removed it reports the drawer button on all eighteen page
and width pairs; with `table-layout: fixed` put back on the coverage matrix it
reports the squeezed cells at 1280; against a server running Python older than
its own templates it reports one drawing over nine nav rows; and before the
Settings and toolbar alignment was fixed it reported those rows by name.
"""

from __future__ import annotations

import os

import pytest
from scripts.check_shell import PAGES, WIDTHS, NoBrowser, ShellResult, run_all

BASE = os.environ.get("SHELL_CHECK_BASE_URL")
EMAIL = os.environ.get("SHELL_CHECK_EMAIL")
PASSWORD = os.environ.get("SHELL_CHECK_PASSWORD")

pytestmark = pytest.mark.skipif(
    not (BASE and EMAIL and PASSWORD),
    reason=(
        "needs a running server and a browser: set SHELL_CHECK_BASE_URL, "
        "SHELL_CHECK_EMAIL and SHELL_CHECK_PASSWORD (and BROWSE_BIN if the "
        "automation command is not in the usual place)"
    ),
)


@pytest.fixture(scope="module")
def shell() -> tuple[list[ShellResult], str]:
    """One pass over the shell. Module-scoped because it drives a browser."""
    assert BASE and EMAIL and PASSWORD
    try:
        return run_all(BASE, EMAIL, PASSWORD)
    except NoBrowser as exc:
        pytest.skip(str(exc))


def test_every_page_and_width_was_checked(shell: tuple[list[ShellResult], str]) -> None:
    results, _ = shell
    assert len(results) == len(PAGES) * len(WIDTHS)


def test_choosing_a_client_sticks_through_a_reload(shell: tuple[list[ShellResult], str]) -> None:
    """The switcher opens, offers clients, and the choice survives a reload.

    All three, because they fail separately and look the same from outside: a
    button that opens nothing, a menu with nothing in it, and a request that
    goes and does not take."""
    _, switched = shell
    assert switched.startswith(("ok", "skip")), switched


def test_every_nav_entry_draws_its_own_icon(shell: tuple[list[ShellResult], str]) -> None:
    """Nine rows, nine drawings. A collapsed nav is nothing but its icons, so
    two rows that look alike are two rows nobody can tell apart."""
    results, _ = shell
    for result in results:
        drawn = [icon["d"] for icon in result.icons]
        assert drawn, f"{result.path} at {result.width}px drew no nav at all"
        assert all(drawn), f"{result.path} at {result.width}px has a nav row with no icon"
        assert len(set(drawn)) == len(drawn), (
            f"{result.path} at {result.width}px drew {len(set(drawn))} icons "
            f"over {len(drawn)} nav rows"
        )


def test_the_drawer_button_is_only_on_screen_with_the_drawer(
    shell: tuple[list[ShellResult], str],
) -> None:
    results, _ = shell
    offenders = [f"{r.path} at {r.width}px" for r in results if r.hamburger != "none"]
    assert not offenders, "the drawer button is drawn beside a nav already on screen: " + ", ".join(
        offenders
    )


def test_nothing_is_clipped_sideways(shell: tuple[list[ShellResult], str]) -> None:
    results, _ = shell
    offenders = [f"{r.path} at {r.width}px: {'; '.join(r.clipped)}" for r in results if r.clipped]
    assert not offenders, "\n".join(offenders)

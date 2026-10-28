"""Every nav entry names a drawing we have, and no two name the same one.

The nav is a list in Python (`app/interfaces/web.py`) and the icons are markup
in a template (`app/templates/_icons.html`). Nothing joined the two until a
rename moved one and not the other, and then seven of the nine rows drew the
same sparkle: the macro fell back to a default for a name it did not hold, so
there was no error on the page, in the log or in this suite. The fallback is
gone and `assert_icons_named` refuses to build the application with a
mismatch; this is that guard, run where a rename is made rather than where it
is deployed.

It also covers a different accident: two entries pointing at one drawing. A
narrowed nav is nothing but its icons, so that is two rows nobody can tell
apart — no worse-looking than one row with the wrong icon, and harder to spot.
"""

from __future__ import annotations

import re

import pytest

from app.interfaces import web


def test_the_icon_file_yields_its_names() -> None:
    """The reader is not vacuous: it finds the drawings the file plainly has."""
    names = web.icon_names()
    assert {"spark", "chart", "shield", "settings", "menu"} <= names
    assert len(names) > 20


def test_every_nav_entry_names_a_drawing_we_have() -> None:
    known = web.icon_names()
    missing = [f"{item.key} wants {item.icon!r}" for item in web.NAV if item.icon not in known]
    assert not missing, "; ".join(missing)


def test_no_two_nav_entries_share_a_drawing() -> None:
    drawn = [item.icon for item in web.NAV]
    assert len(set(drawn)) == len(drawn), (
        f"repeated: {sorted({n for n in drawn if drawn.count(n) > 1})}"
    )


def test_the_guard_refuses_a_name_the_icon_file_does_not_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proved to fail, not only to pass: this is the shape the rename had."""
    broken = web.NavItem("ask", "Ask", "/", "no-such-drawing", web.EVERYONE)
    monkeypatch.setattr(web, "NAV", (broken, *web.NAV[1:]))
    with pytest.raises(RuntimeError, match="no-such-drawing"):
        web.assert_icons_named()


def test_the_guard_refuses_two_entries_sharing_one_drawing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = web.NAV[1]
    twin = web.NavItem(second.key, second.label, second.href, web.NAV[0].icon, second.roles)
    monkeypatch.setattr(web, "NAV", (web.NAV[0], twin, *web.NAV[2:]))
    with pytest.raises(RuntimeError, match="may not share a drawing"):
        web.assert_icons_named()


def test_the_icon_macro_falls_back_to_a_mark_nobody_would_choose() -> None:
    """The other half of the fix, and the second version of it.

    It fell back to the sparkle, which printed one real icon nine times. Then
    it fell back to nothing, which at 18px is an invisible square and was
    reported as a nav with no icons at all. `missing` is a crossed box: not a
    drawing anybody chose, and not one anybody overlooks."""
    macro = (web.TEMPLATE_DIR / "_icons.html").read_text(encoding="utf-8")
    assert "PATHS['spark'], true" not in macro
    assert re.search(r"PATHS\[name\] \| default\(PATHS\['missing'\], true\)", macro)
    assert web.MISSING_ICON in web.icon_names()


def test_no_nav_entry_may_name_the_missing_mark() -> None:
    """It is what a mistake looks like, so naming it on purpose is a mistake."""
    broken = web.NavItem("ask", "Ask", "/", web.MISSING_ICON, web.EVERYONE)
    assert web.MISSING_ICON not in (item.icon for item in web.NAV)
    assert broken.icon not in web.icon_names() - {web.MISSING_ICON}

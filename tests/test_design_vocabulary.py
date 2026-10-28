"""The names this product draws with, and the ones it used to.

Three vocabularies were collapsed into one — a status is a `.state`, a button is
a `.btn` carrying one of three variants, and 13px text is `.small` — and the
whole point of collapsing them is that there is exactly one way to ask for each.
A name that comes back is not a visual bug anybody would notice: it is a second
way to do the thing, and the next page picks whichever one it copied from.

So the deleted names are checked here rather than left to review. The check is
deliberately narrow — a class attribute in a template or a script, a selector in
the stylesheet, a custom property — because the words themselves are ordinary
English and "a tiny difference" in a comment is not a regression.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"
SCRIPTS = ROOT / "app" / "static" / "js"
STYLESHEET = ROOT / "app" / "static" / "app.css"

#: Class names that were deleted outright, and what to write instead.
GONE: dict[str, str] = {
    "chip": "a status is `.state` — write one with `stateWord` in states.js",
    "button": "a button is `.btn`; `.button` on an anchor is gone",
    "quiet": "`.action` for a text action, `.btn secondary` for a button",
    "danger": "`.btn destructive`",
    "tiny": "`.small` — both were 13px",
}

#: Variants that mean nothing on their own: they name a kind of `.btn`.
VARIANTS = ("primary", "secondary", "destructive")

#: Custom properties collapsed into the three sizes.
GONE_TOKENS = ("--text-xs", "--text-sm", "--text-base", "--text-lg", "--text-xl", "--text-2xl")

#: Names that merely contain a deleted one and are their own thing.
KEPT = ("filter-chip", "chip-row", "chip-line", "mark-large", "nav-toggle")

CLASS_ATTRIBUTE = re.compile(r'class="([^"]*)"')


def _files() -> list[pathlib.Path]:
    return sorted(TEMPLATES.glob("*.html")) + sorted(SCRIPTS.glob("*.js"))


def _attributes(text: str) -> list[tuple[int, set[str]]]:
    """Every class attribute in the file, as the set of words written in it.

    A page builds these by interpolation, so the attribute is read as its words
    rather than parsed: `class="btn small ${x ? "" : "destructive"}"` yields
    btn, small and destructive, which is exactly what should be checked.
    """
    found: list[tuple[int, set[str]]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for attribute in CLASS_ATTRIBUTE.findall(line):
            words = {w for w in re.split(r"[\s\"'`${}?:|&()+,.\[\]]+", attribute) if w}
            if words:
                found.append((number, words))
    return found


def _without_comments(css: str) -> str:
    """The stylesheet with its comments blanked and its line numbers kept."""
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), css, flags=re.S)


def test_no_deleted_class_name_is_written_in_a_template_or_a_script() -> None:
    offences: list[str] = []
    for path in _files():
        for number, words in _attributes(path.read_text(encoding="utf-8")):
            where = f"{path.relative_to(ROOT)}:{number}"
            for word in sorted(words):
                if any(keep == word for keep in KEPT):
                    continue
                if word in GONE:
                    offences.append(f"{where}: {word!r} — {GONE[word]}")
                elif word in VARIANTS and "btn" not in words:
                    offences.append(f"{where}: {word!r} names a kind of `.btn`, so write both")
    assert not offences, "deleted class names are back:\n" + "\n".join(offences)


def test_no_deleted_class_name_is_styled_in_the_stylesheet() -> None:
    """The other half: a rule for a name nothing writes is a name waiting to be
    written again."""
    offences: list[str] = []
    for number, line in enumerate(_without_comments(STYLESHEET.read_text("utf-8")).splitlines(), 1):
        selector = line.split("{")[0]
        if any(keep in selector for keep in KEPT):
            continue
        for name, instead in GONE.items():
            if re.search(rf"\.{re.escape(name)}(?![\w-])", selector):
                offences.append(f"app/static/app.css:{number}: .{name} — {instead}")
    assert not offences, "deleted class names are styled again:\n" + "\n".join(offences)


def test_there_are_three_type_sizes_and_three_names_for_them() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    for token in GONE_TOKENS:
        assert f"{token}:" not in css, f"{token} is declared again; the sizes are title/body/meta"
        assert f"var({token})" not in css, f"{token} is used again"
    for token in ("--text-title", "--text-body", "--text-meta"):
        assert f"{token}:" in css, f"{token} should be declared"


def test_the_check_would_catch_a_name_coming_back() -> None:
    """The three assertions above pass on an empty search as happily as on a
    clean one, so this proves each is looking at something."""
    markup = '<span class="chip bad">late</span>\n<button class="quiet">x</button>'
    words = dict(_attributes(markup))
    assert words[1] == {"chip", "bad"}
    assert words[2] == {"quiet"}
    # A variant on its own is caught; the same variant beside `btn` is not.
    assert _attributes('<a class="secondary">')[0][1] == {"secondary"}
    assert "btn" in _attributes('<a class="btn secondary">')[0][1]
    # The stylesheet's comments are blanked rather than dropped, so a selector
    # on line N is still reported as line N.
    blanked = _without_comments("a {}\n/* .chip\n   .quiet */\nb {}")
    assert blanked.splitlines()[3] == "b {}"
    assert ".chip" not in blanked
    assert len(_files()) > 10

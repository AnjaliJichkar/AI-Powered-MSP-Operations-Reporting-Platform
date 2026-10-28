#!/usr/bin/env python
"""The shell, in a real browser: the client switcher, the nav, and the width.

Three things a unit test cannot see, each of which broke at once when the
Aurora shell landed and none of which raised anything anywhere:

* **The client switcher.** Its button is wired by `common.js` to a menu in the
  markup. When the markup was rewritten, the script still looked for the old
  `<select>`, so the control did nothing at all — no menu, no request, no
  console error. The only way to know is to press it and reload.
* **The nav's icons.** They are markup in a template, named by a list in
  Python. A rename that moves one and not the other (including a server
  process that kept the old Python in memory while Jinja reloaded the new
  template) drew the same glyph on every row. `assert_icons_named` refuses to
  build an application with that mismatch, and this is the other end: what the
  browser actually drew.
* **The width.** A box with `overflow: hidden` and more content than room
  clips it in silence; so does a table whose columns are squeezed to fit.
  Both read as a considerate layout and both lose a column.

It needs a browser and a running server with an account to sign in with, like
`check_page_scrolling.py` beside it, and skips rather than failing without
them:

    SHELL_CHECK_BASE_URL=http://127.0.0.1:8099 \\
    SHELL_CHECK_EMAIL=... SHELL_CHECK_PASSWORD=... \\
    python scripts/check_shell.py

The account must be Inputiv staff: a franchisee has one client and no
switcher, which is the point of the control and not a case to test around.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from scripts.check_page_scrolling import NoBrowser, _browser, _run, sign_in

#: The widths a laptop actually has. 1280 is where the coverage matrix was
#: clipped and 1440 is where it was not, so one of each.
WIDTHS: tuple[int, ...] = (1440, 1280)
HEIGHT = 900

#: Every page in the nav. The shell is the same on all of them, and a rule
#: that only fits on the page somebody looked at is not a rule.
PAGES: tuple[str, ...] = (
    "/",
    "/reports",
    "/sites",
    "/coverage",
    "/devices",
    "/tickets",
    "/integrations",
    "/autopilot/results",
    "/automations",
    "/settings",
)

#: What the nav drew, and whether any of it can be seen.
#:
#: `d` is the whole of an icon here — there is no sprite and no font — so nine
#: different non-empty `d`s is nine different drawings. That alone was not
#: enough: an icon can be present, distinct and still invisible, which is how
#: a nav with nine empty 18px squares was reported as having no icons at all.
#: So the box is measured, the ink is read off the path as the browser
#: resolved it, and the ancestors are walked for a `display`, a `visibility`
#: or an `opacity` that hides it. A `stroke` of `none`, a fully transparent
#: one, or a zero width is no ink: an SVG path with no fill and no stroke
#: draws nothing at all.
NAV_ICONS = r"""
(() => {
  const clear = (c) => c === 'none' || c === 'transparent'
    || /rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*0\s*\)/.test(c);
  const hidden = (el) => {
    for (let node = el; node && node !== document.documentElement; node = node.parentElement) {
      const cs = getComputedStyle(node);
      if (cs.display === 'none') return 'display: none';
      if (cs.visibility === 'hidden') return 'visibility: hidden';
      if (parseFloat(cs.opacity) === 0) return 'opacity: 0';
    }
    return '';
  };
  const rows = Array.from(document.querySelectorAll('.nav a'));
  return JSON.stringify(rows.map((a) => {
    const svg = a.querySelector('svg');
    const path = svg ? svg.querySelector('path') : null;
    if (!svg || !path) return { label: a.textContent.trim(), d: '', why: 'no svg in the markup' };
    const box = svg.getBoundingClientRect();
    const ink = getComputedStyle(path);
    const why = [];
    const concealed = hidden(svg);
    if (concealed) why.push(concealed);
    if (!box.width || !box.height) why.push(`drawn ${box.width}x${box.height}`);
    const d = path.getAttribute('d') || '';
    if (!d.trim()) why.push('an empty path');
    const painted = (!clear(ink.fill)) ||
      (!clear(ink.stroke) && parseFloat(ink.strokeWidth) > 0);
    if (!painted) why.push(`no ink: fill ${ink.fill}, stroke ${ink.stroke} at ${ink.strokeWidth}`);
    return { label: a.textContent.trim(), d, why: why.join('; ') };
  }));
})()
"""

#: Anything clipped sideways, and whether the window itself scrolls. Text that
#: is only there for a screen reader is clipped on purpose (`.sr-only` is
#: `clip: rect(0 0 0 0)`), so it is the one thing left out.
CLIPPED = """
(() => {
  const bad = [];
  if (document.documentElement.scrollWidth > window.innerWidth + 1) {
    bad.push('the window scrolls sideways at ' + document.documentElement.scrollWidth);
  }
  document.querySelectorAll('.main *, .topbar *').forEach((el) => {
    const name = String(el.className);
    if (/sr-only|visually-hidden/.test(name)) return;
    if (!el.clientWidth) return;
    if (el.scrollWidth <= el.clientWidth + 1) return;
    const tag = el.tagName.toLowerCase();
    /* A cell is in whatever its own text and the column let it be. With
       `table-layout: fixed` the columns are squeezed to the box and the words
       are clipped by the cell, which no ancestor's `overflow` reports: the
       wrapper never scrolls, because by its own measure nothing overflowed. */
    if (tag !== 'th' && tag !== 'td' && getComputedStyle(el).overflowX !== 'hidden') return;
    bad.push(tag + '.' + name.split(' ')[0]
             + ' holds ' + el.scrollWidth + ' in ' + el.clientWidth);
  });
  /* One squeezed column is every row of the table; the first few say which
     column and the count says how much of the page it is. */
  const shown = bad.slice(0, 4);
  if (bad.length > shown.length) shown.push('and ' + (bad.length - shown.length) + ' more');
  return JSON.stringify(shown);
})()
"""

#: Rows of controls whose controls are not one row.
#:
#: A filter strip, a toolbar and a form row all put an input, a select and a
#: button side by side, and all three have to be the same height and sit on
#: the same bottom edge or the row reads as three things that happen to be
#: near each other. The Settings invite row had a 36px input, a 39px select
#: and a 36px button centred on the whole field, which put the button up
#: beside the labels. Hidden controls are left out: a `hidden` block measures
#: zero and is not a row anybody is looking at.
CONTROL_ROWS = """
(() => {
  const bad = [];
  document.querySelectorAll('.field-row, .filter-bar, .toolbar').forEach((row) => {
    const parts = Array.from(row.querySelectorAll('input, select, button, .btn'))
      .filter((el) => el.offsetParent !== null)
      /* A tick box is 13px wherever it is drawn, and so is every other one
         beside it: what has to line up in this strip is the boxes somebody
         types or chooses in. Its *label* takes the row's height, which is
         what puts the tick on the same line as the selects. */
      .filter((el) => el.type !== 'checkbox' && el.type !== 'radio')
      .map((el) => el.getBoundingClientRect())
      .filter((box) => box.height > 0)
      .sort((a, b) => a.top - b.top);
    /* A row that wraps is several rows on the screen, and only the controls
       actually beside each other have to line up. Boxes that overlap
       vertically are one line. */
    const lines = [];
    parts.forEach((box) => {
      const line = lines.find((l) => l.some((b) => b.top < box.bottom && box.top < b.bottom));
      if (line) line.push(box); else lines.push([box]);
    });
    const name = (row.id || row.className).toString().split(' ')[0];
    lines.forEach((line) => {
      if (line.length < 2) return;
      const heights = new Set(line.map((b) => Math.round(b.height)));
      const bottoms = new Set(line.map((b) => Math.round(b.bottom)));
      if (heights.size > 1) bad.push(name + ': heights ' + [...heights].join(', '));
      else if (bottoms.size > 1) bad.push(name + ': bottom edges ' + [...bottoms].join(', '));
    });
  });
  return JSON.stringify(bad.slice(0, 4));
})()
"""

#: Whether the hamburger is on screen. It opens the drawer, and above 900px
#: there is no drawer: the nav is already there. It was drawn beside a
#: full-width sidebar at 1440, which is a button that does nothing.
HAMBURGER = """
(() => {
  const button = document.getElementById('nav-toggle');
  return JSON.stringify(button ? getComputedStyle(button).display : 'missing');
})()
"""


@dataclass(frozen=True)
class ShellResult:
    path: str
    width: int
    clipped: list[str]
    rows: list[str]
    hamburger: str
    icons: list[dict[str, str]]

    @property
    def ok(self) -> bool:
        return (
            not self.clipped
            and not self.rows
            and self.hamburger == "none"
            and self.icons_distinct
            and not self.icons_invisible
        )

    @property
    def icons_distinct(self) -> bool:
        drawn = [icon["d"] for icon in self.icons]
        return bool(drawn) and all(drawn) and len(set(drawn)) == len(drawn)

    @property
    def icons_invisible(self) -> list[str]:
        """Rows whose icon is in the markup and cannot be seen on the screen."""
        return [f"{icon['label']}: {icon['why']}" for icon in self.icons if icon.get("why")]


def _js(browser: str, expression: str) -> object:
    return json.loads(_run(browser, "js", expression.strip()))


def check_page(browser: str, base: str, path: str, width: int) -> ShellResult:
    _run(browser, "viewport", f"{width}x{HEIGHT}")
    _run(browser, "goto", f"{base}{path}")
    _run(browser, "js", "new Promise(r => setTimeout(r, 2500))")
    return ShellResult(
        path=path,
        width=width,
        clipped=list(_js(browser, CLIPPED)),  # type: ignore[arg-type]
        rows=list(_js(browser, CONTROL_ROWS)),  # type: ignore[arg-type]
        hamburger=str(_js(browser, HAMBURGER)),
        icons=list(_js(browser, NAV_ICONS)),  # type: ignore[arg-type]
    )


def switch_client(browser: str, base: str) -> str:
    """Open the switcher, choose a client that is not the current one, reload.

    Returns one line saying what happened, beginning `ok` or not. Sticking is
    the whole test: a switcher that opens, sends the request and comes back on
    the client it started on is the same bug, to whoever is using it, as one
    that never opened at all.

    **An empty menu is a failure, not a skip.** That distinction is the bug
    this exists for: with the menu missing from the markup the control was
    silent in exactly the way a session with one client would be, and a check
    that cannot tell those apart would have passed on the broken shell.
    """
    _run(browser, "viewport", f"{WIDTHS[0]}x{HEIGHT}")
    _run(browser, "goto", f"{base}/coverage")
    _run(browser, "js", "new Promise(r => setTimeout(r, 2000))")

    offered = _js(browser, "JSON.stringify(window.DESK.maySwitchClient)")
    if not offered:
        return "skip  the switcher: this account is a franchisee and has no switcher"

    _run(browser, "click", "#client-switch")
    _run(browser, "js", "new Promise(r => setTimeout(r, 1500))")
    open_now = _js(
        browser, "JSON.stringify(!(document.getElementById('client-menu') || {}).hidden)"
    )
    if not open_now:
        return "DEAD  the switcher: pressing it opened nothing"
    current = _js(browser, "JSON.stringify((window.DESK.client || {}).slug || null)")
    slugs = _js(
        browser,
        "JSON.stringify(Array.from(document.querySelectorAll('#client-menu [data-slug]'))"
        ".map(b => b.dataset.slug))",
    )
    assert isinstance(slugs, list)
    if not slugs:
        return "EMPTY the switcher: it opened and offered no clients at all"
    wanted = next((slug for slug in slugs if slug != current), None)
    if wanted is None:
        return "skip  the switcher: this deployment has one client to choose between"

    _run(browser, "click", f"#client-menu [data-slug='{wanted}']")
    _run(browser, "js", "new Promise(r => setTimeout(r, 2500))")
    _run(browser, "reload")
    _run(browser, "js", "new Promise(r => setTimeout(r, 2000))")
    after = _js(browser, "JSON.stringify((window.DESK.client || {}).slug || null)")
    if after == wanted:
        return "ok    the switcher: chose a client and it survived a reload"
    return f"STUCK the switcher: chose one client and the session came back on {after!r}"


def run_all(base: str, email: str, password: str) -> tuple[list[ShellResult], str]:
    browser = _browser()
    sign_in(browser, base, email, password)
    switched = switch_client(browser, base)
    results = [check_page(browser, base, path, width) for width in WIDTHS for path in PAGES]
    return results, switched


def main() -> int:
    base = os.environ.get("SHELL_CHECK_BASE_URL")
    email = os.environ.get("SHELL_CHECK_EMAIL")
    password = os.environ.get("SHELL_CHECK_PASSWORD")
    if not (base and email and password):
        print("set SHELL_CHECK_BASE_URL, SHELL_CHECK_EMAIL and SHELL_CHECK_PASSWORD")
        return 2
    try:
        results, switched = run_all(base, email, password)
    except NoBrowser as exc:
        print(exc)
        return 2

    print(switched)

    for result in results:
        mark = "ok  " if result.ok else "BAD "
        notes = []
        if result.hamburger != "none":
            notes.append(f"the drawer button is {result.hamburger} with the nav already on screen")
        if not result.icons_distinct:
            notes.append(
                f"{len({i['d'] for i in result.icons})} drawings over {len(result.icons)} nav rows"
            )
        notes.extend(result.rows)
        notes.extend(result.icons_invisible[:3])
        notes.extend(result.clipped)
        print(
            f"{mark} {result.width:>5}px {result.path:<20}"
            + ("  " + "; ".join(notes) if notes else "")
        )

    bad = [r for r in results if not r.ok]
    return 1 if bad or not switched.startswith(("ok", "skip")) else 0


if __name__ == "__main__":
    raise SystemExit(main())

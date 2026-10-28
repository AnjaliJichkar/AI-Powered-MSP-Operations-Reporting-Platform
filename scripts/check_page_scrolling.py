"""Every page, filled with more content than fits, and asked to scroll to the end.

Two arrangements, and the check knows which page is which.

Most pages scroll the window: the list is the page and the page is as long as
the list. Ask does not — its frame is exactly the viewport, the conversation
scrolls inside its own column and the question box sits on the bottom edge of
the screen, so somebody reading an answer can type the next question without
going anywhere.

Both are fragile in the same silent way. A flex or grid child that forgets
`min-height: 0` stops overflowing, grows past its frame instead, and takes its
content off the bottom of the screen with no scrollbar anywhere. Nothing
errors. The page just loses its last paragraph, and somebody finds out in a
meeting — which is how Ask came to hold a 2,508px conversation in a 900px
window with the question box 2,750px down the document.

So this loads each page, puts more into it than fits, and asks:

  1. can the last element be brought into view at all?
  2. on Ask, can the **first** message be brought back as well, with the
     question box still on screen and the window itself still not scrolling?
  3. is any box's content taller than the box, with the overflow hidden rather
     than scrolled or spilled?

The second question is its own because a conversation is the one thing here
read in both directions: an answer that cannot be scrolled back to is an answer
that was never given.

**It needs a browser, and this project has none in its dependencies.** It drives
whatever automation command is on the machine, named by `BROWSE_BIN`, which must
accept `viewport WxH`, `goto <url>`, `js <expression>` and print the
expression's value. Without one the check says so and does nothing, and
`tests/browser/test_page_scrolling.py` skips for the same reason — a browser is
not going to appear in CI without somebody deciding to put it there.

    SCROLL_CHECK_BASE_URL=http://127.0.0.1:8099 \\
    SCROLL_CHECK_EMAIL=... SCROLL_CHECK_PASSWORD=... \\
    python scripts/check_page_scrolling.py

Prints a row per page and width and exits non-zero if anything is unreachable.
No name of a customer, site or device is printed: it reports element tags,
classes and pixel counts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

#: Page, and what to fill it with. The selector is the page's own content area;
#: filling the shell instead would test the shell and not the page. `#main` is
#: the page area inside the frame, which every page has and only Ask opts out
#: of with `flush`.
PAGES: tuple[tuple[str, str], ...] = (
    ("/", "#feed"),
    ("/reports", "#review"),
    ("/integrations", "#main"),
    ("/sites", "#main"),
    ("/coverage", "#main"),
    ("/devices", "#main"),
    ("/tickets", "#main"),
    ("/autopilot/results", "#main"),
    ("/automations", "#main"),
    ("/settings", "#main"),
)

#: The page whose frame is the viewport, and so the page with both ends to
#: reach. Its own check runs as well as the one above, not instead of it.
ASK_PATH = "/"

WIDTHS: tuple[int, ...] = (1440, 1280, 1100, 820)
HEIGHT = 900

DEFAULT_BROWSER = os.path.expanduser("~/.claude/skills/gstack/browse/dist/browse")

#: Put forty tall blocks into the page's own content area, then measure. Written
#: as one expression because that is all the driver is asked to evaluate.
FILL_AND_MEASURE = """
(() => {
  const host = document.querySelector(%(selector)s);
  if (!host) return JSON.stringify({error: "no content area"});
  for (let i = 0; i < 40; i += 1) {
    const block = document.createElement("div");
    block.className = "card";
    block.style.minHeight = "80px";
    block.textContent = "Filler block " + i + " — this stands in for a long answer, "
      + "a long review or a long list, so the page is asked to scroll.";
    host.appendChild(block);
  }
  const clipped = [];
  for (const el of document.querySelectorAll("body *")) {
    const style = getComputedStyle(el);
    if (style.display === "none" || el.hidden) continue;
    if (el.classList.contains("sr-only") || el.clientHeight < 6) continue;
    const overflowY = style.overflowY;
    const scrolls = overflowY === "auto" || overflowY === "scroll";
    const swallows = overflowY === "hidden" || overflowY === "clip";
    if (el.scrollHeight > el.clientHeight + 2 && !scrolls && swallows) {
      clipped.push(el.tagName.toLowerCase() + "." + (el.className || "").toString().slice(0, 30));
    }
  }
  const last = host.lastElementChild;
  last.scrollIntoView({block: "end"});
  const box = last.getBoundingClientRect();
  return JSON.stringify({
    reachable: box.bottom <= window.innerHeight + 2 && box.top < window.innerHeight,
    bottom: Math.round(box.bottom),
    viewport: window.innerHeight,
    clipped: clipped,
  });
})()
"""

#: A long conversation, drawn the way `ask.js` draws one, then read from both
#: ends. The question box is checked at each end rather than once: it is held
#: on screen by the frame being bounded, which is the thing that broke.
ASK_CONVERSATION = """
(() => {
  const feed = document.getElementById("feed");
  const main = document.querySelector(".ask-main");
  const composer = document.querySelector(".composer");
  if (!feed || !main || !composer) return JSON.stringify({error: "no conversation"});
  delete main.dataset.empty;
  feed.innerHTML = "";
  for (let i = 1; i <= 14; i += 1) {
    const turn = document.createElement("div");
    turn.className = "turn";
    turn.innerHTML = '<div class="question"></div><div class="answer"></div>'
      + '<div class="meta"><span>Desk</span><span>just now</span></div>';
    turn.querySelector(".question").textContent = "Question " + i + " about the estate";
    turn.querySelector(".answer").textContent = "Answer " + i + " — long enough that a "
      + "dozen of these do not fit on a screen, so the conversation has to be scrolled "
      + "to be read, in both directions, which is what this is here to prove.";
    feed.appendChild(turn);
  }
  const first = feed.firstElementChild;
  const last = feed.lastElementChild;
  const onScreen = (el) => {
    const box = el.getBoundingClientRect();
    return box.top < window.innerHeight && box.bottom > 0;
  };
  const composerHeld = () => {
    const box = composer.getBoundingClientRect();
    return box.bottom <= window.innerHeight + 2 && box.top >= 0;
  };
  const windowScrolls = () =>
    document.documentElement.scrollHeight > window.innerHeight + 2;

  last.scrollIntoView({block: "end"});
  const lastSeen = onScreen(last);
  const heldAtEnd = composerHeld();
  const scrolledAtEnd = windowScrolls();

  first.scrollIntoView({block: "start"});
  const firstSeen = onScreen(first);
  const heldAtStart = composerHeld();

  return JSON.stringify({
    overflowed: feed.scrollHeight > feed.clientHeight + 2,
    scrolls: ["auto", "scroll"].includes(getComputedStyle(feed).overflowY),
    first: firstSeen,
    last: lastSeen,
    composer: heldAtEnd && heldAtStart,
    frameStill: !scrolledAtEnd,
    viewport: window.innerHeight,
    feed: Math.round(feed.clientHeight) + " of " + Math.round(feed.scrollHeight),
  });
})()
"""


@dataclass(frozen=True)
class Result:
    path: str
    width: int
    reachable: bool
    bottom: int
    viewport: int
    clipped: list[str]

    @property
    def ok(self) -> bool:
        return self.reachable and not self.clipped


@dataclass(frozen=True)
class AskResult:
    """Ask read from both ends, with the frame and the question box checked.

    Every field is a thing that has to hold at once: a feed that scrolls but
    cannot reach its first message is as unreadable as one that does not
    scroll at all."""

    width: int
    feed: str
    viewport: int
    faults: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.faults


class NoBrowser(RuntimeError):
    """No automation command on this machine, so there is nothing to drive."""


def _browser() -> str:
    named = os.environ.get("BROWSE_BIN") or DEFAULT_BROWSER
    found = shutil.which(named) or (named if os.path.exists(named) else None)
    if not found:
        raise NoBrowser(
            f"no browser automation command at {named!r}. Set BROWSE_BIN to one that accepts "
            "`viewport WxH`, `goto <url>` and `js <expression>`."
        )
    return found


def _run(browser: str, *args: str) -> str:
    # The command is the operator's own `BROWSE_BIN`, the arguments are the
    # constants above and a URL the operator gave: nothing here comes from a
    # request, a vendor or a model, and no shell is involved.
    done = subprocess.run(  # noqa: S603
        [browser, *args], capture_output=True, text=True, timeout=120
    )
    if done.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip()


def sign_in(browser: str, base: str, email: str, password: str) -> None:
    """Sign in, unless the browser already is.

    A browser that still holds a session is sent away from `/login`, so there is
    no form to fill; that is not a failure, it is the common case when this is
    run twice."""
    _run(browser, "goto", f"{base}/login")
    already = _run(browser, "js", '!document.getElementById("signin")')
    if already.strip() == "true":
        return
    _run(browser, "js", f'document.getElementById("email").value = {json.dumps(email)}')
    _run(browser, "js", f'document.getElementById("password").value = {json.dumps(password)}')
    _run(browser, "js", 'document.getElementById("signin").requestSubmit()')
    _run(browser, "js", "new Promise(r => setTimeout(r, 2500))")


#: Switch onto a client, because most pages have nothing to draw without one
#: and some refuse the request outright. Asked of the API rather than clicked,
#: so this does not depend on the switcher's markup.
CHOOSE_CLIENT = """
fetch("/auth/me")
  .then((r) => r.json())
  .then((me) =>
    me.csrf_token
      ? fetch("/auth/switch-client", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-CSRF-Token": me.csrf_token},
          body: JSON.stringify({client: %(slug)s}),
        }).then((done) => (done.ok ? "ok" : "switch refused with " + done.status))
      : "not signed in"
  )
"""


def choose_client(browser: str, base: str, slug: str) -> None:
    """Put the session on one client. Inputiv staff start on none."""
    _run(browser, "goto", f"{base}/settings")
    answer = _run(browser, "js", CHOOSE_CLIENT % {"slug": json.dumps(slug)}).strip()
    if answer != "ok":
        raise RuntimeError(f"could not switch to {slug!r}: {answer}")


def _open(browser: str, base: str, path: str, width: int) -> None:
    _run(browser, "viewport", f"{width}x{HEIGHT}")
    _run(browser, "goto", f"{base}{path}")
    _run(browser, "js", "new Promise(r => setTimeout(r, 2500))")


def check(browser: str, base: str, path: str, selector: str, width: int) -> Result:
    _open(browser, base, path, width)
    raw = _run(browser, "js", FILL_AND_MEASURE % {"selector": json.dumps(selector)})
    data = json.loads(raw)
    if "error" in data:
        raise RuntimeError(f"{path}: {data['error']}")
    return Result(
        path=path,
        width=width,
        reachable=bool(data["reachable"]),
        bottom=int(data["bottom"]),
        viewport=int(data["viewport"]),
        clipped=list(data["clipped"]),
    )


#: What each answer has to say, and what to call it when it does not.
ASK_FAULTS: tuple[tuple[str, str], ...] = (
    ("overflowed", "the conversation fits, so nothing was tested"),
    ("scrolls", "the conversation is not a scrolling box"),
    ("first", "the first message cannot be reached"),
    ("last", "the last message cannot be reached"),
    ("composer", "the question box leaves the screen"),
    ("frameStill", "the frame scrolls instead of the conversation"),
)


def check_ask(browser: str, base: str, width: int) -> AskResult:
    """A long conversation, read from both ends."""
    _open(browser, base, ASK_PATH, width)
    data = json.loads(_run(browser, "js", ASK_CONVERSATION))
    if "error" in data:
        raise RuntimeError(f"{ASK_PATH}: {data['error']}")
    return AskResult(
        width=width,
        feed=str(data["feed"]),
        viewport=int(data["viewport"]),
        faults=[words for key, words in ASK_FAULTS if not data[key]],
    )


def run_all(
    base: str, email: str, password: str, client: str | None
) -> tuple[list[Result], list[AskResult]]:
    browser = _browser()
    sign_in(browser, base, email, password)
    if client:
        choose_client(browser, base, client)
    results, asks = [], []
    for width in WIDTHS:
        for path, selector in PAGES:
            results.append(check(browser, base, path, selector, width))
        asks.append(check_ask(browser, base, width))
    return results, asks


def main() -> int:
    base = os.environ.get("SCROLL_CHECK_BASE_URL")
    email = os.environ.get("SCROLL_CHECK_EMAIL")
    password = os.environ.get("SCROLL_CHECK_PASSWORD")
    # Inputiv staff sign in on no client, and a page with no client to draw is
    # not the page this is checking.
    client = os.environ.get("SCROLL_CHECK_CLIENT")
    if not (base and email and password):
        print("set SCROLL_CHECK_BASE_URL, SCROLL_CHECK_EMAIL and SCROLL_CHECK_PASSWORD")
        print("and SCROLL_CHECK_CLIENT for a staff account, which starts on no client")
        return 2
    try:
        results, asks = run_all(base, email, password, client)
    except NoBrowser as exc:
        print(exc)
        return 2
    failed = [r for r in results if not r.ok]
    for r in results:
        mark = "ok  " if r.ok else "CUT "
        note = f" clipped: {', '.join(r.clipped)}" if r.clipped else ""
        print(
            f"{mark} {r.width:>5}px {r.path:<20} last element at {r.bottom} of {r.viewport}{note}"
        )
    print()
    for a in asks:
        mark = "ok  " if a.ok else "CUT "
        note = f" — {'; '.join(a.faults)}" if a.faults else ""
        print(f"{mark} {a.width:>5}px {'conversation':<20} feed {a.feed} in {a.viewport}{note}")
    bad = len(failed) + sum(1 for a in asks if not a.ok)
    total = len(results) + len(asks)
    print(f"\n{total - bad} of {total} checks pass")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

/* What every page needs: talking to the API, and the header.
 *
 * The pages are thin on purpose. Nothing here decides what anybody may see —
 * every endpoint resolves the session itself — so this is the mechanics of
 * asking: send the CSRF token on anything that changes state, and when the
 * answer is a 401, go to the sign-in page instead of rendering an empty one.
 */

const DESK = window.DESK || {};

/* --- talking to the API ---------------------------------------------------- */

/** Headers for a request that changes state. A GET must not send these. */
function unsafeHeaders(extra) {
  return Object.assign(
    { "Content-Type": "application/json", "X-CSRF-Token": DESK.csrfToken },
    extra || {}
  );
}

/** True if the response is usable. A 401 means the session went, so leave. */
function signedIn(response) {
  if (response.status === 401) {
    window.location.href = DESK.loginPath || "/login";
    return false;
  }
  return true;
}

/** The server's own message for a failure, or a fallback naming the status.
 *
 * FastAPI puts a string in `detail` for most refusals and an object for the
 * render gate, which lists what it refused. Both are shown as written: the
 * endpoint is the thing that knows why, and paraphrasing it here would mean
 * two descriptions of the same refusal drifting apart. */
async function errorDetail(response) {
  try {
    const body = await response.json();
    const detail = body.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      return [detail.message, ...(detail.problems || [])].filter(Boolean).join("\n");
    }
  } catch (error) {
    /* Not JSON: a proxy error page, or nothing at all. */
  }
  return `The request failed (${response.status}).`;
}

/** One request. Throws `ApiError` with the server's message on a refusal. */
async function api(method, url, body) {
  const options = { method, headers: {} };
  if (method !== "GET" && method !== "HEAD") {
    options.headers = unsafeHeaders();
    options.body = JSON.stringify(body || {});
  }
  const response = await fetch(url, options);
  if (!signedIn(response)) throw new ApiError("Signed out.", 401);
  if (!response.ok) throw new ApiError(await errorDetail(response), response.status);
  if (response.status === 204) return null;
  return response.json();
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

/* --- text -------------------------------------------------------------------- */

/** Text as text. Anything from a vendor, a customer or a model is untrusted:
 *  a device called `<img src=x onerror=...>` renders as characters.
 *
 *  Quotes are escaped as well as angle brackets, which the obvious
 *  `textContent` round-trip does not do. That round-trip is safe between tags
 *  and a hole inside an attribute: `value="${escapeHtml(name)}"` with a name
 *  containing a double quote closes the attribute and starts another. Both
 *  uses are all over these pages, so the one function covers both rather than
 *  leaving a second one to remember. */
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeHtml(text) {
  if (text === null || text === undefined) return "";
  return String(text).replace(/[&<>"']/g, (character) => HTML_ESCAPES[character]);
}

function setText(id, text) {
  const node = document.getElementById(id);
  if (node) node.textContent = text;
}

/* --- time ------------------------------------------------------------------
 *
 * One way to write a time, and one rule for which way.
 *
 * While something is recent, "how long ago" is the question somebody is
 * actually asking, and they work it out from a date every time — getting it
 * wrong on a Monday. Past a week the relative form stops meaning anything:
 * "eleven weeks ago" is not something anybody can line up against a vendor's
 * console. So it is relative for a week and a date after that, with the year
 * only when it is not this one, and the exact instant is always the tooltip, so
 * nothing is lost by shortening it.
 *
 * Five names used to do this — relativeTime, humanAge, humanWhen, two different
 * `when`s and an onDay — and between them they wrote "17 h ago", "17 hours
 * ago", "2 Oct 2026, 19:51" and "06/10/2026, 16:17:40" for the same instant.
 */
const WEEK_SECONDS = 7 * 24 * 60 * 60;

/**
 * The words alone, for somewhere that cannot take markup: an option, a title
 * attribute, a chart label.
 *
 * @param {string} iso
 * @param {object} [options]
 * @param {boolean} [options.time]   add the time of day, where it matters.
 * @param {string} [options.absent]  what to say when there is no time at all.
 */
function timeWords(iso, options) {
  const absent = (options && options.absent) || "never";
  if (!iso) return absent;
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return absent;
  const seconds = (Date.now() - then.getTime()) / 1000;
  /* Only the past is written as "ago". A time in the future — an invitation's
     expiry — is a date, because "in three days" is not what somebody checking
     one wants to read. */
  if (seconds >= 0 && seconds < WEEK_SECONDS) {
    if (seconds < 120) return "just now";
    if (seconds < 3600) {
      const minutes = Math.floor(seconds / 60);
      return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
    }
    if (seconds < 86400) {
      const hours = Math.floor(seconds / 3600);
      return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    }
    const days = Math.floor(seconds / 86400);
    return days === 1 ? "yesterday" : `${days} days ago`;
  }
  const thisYear = then.getFullYear() === new Date().getFullYear();
  const day = then.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(thisYear ? {} : { year: "numeric" }),
  });
  if (!(options && options.time)) return day;
  return `${day}, ${then.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
}

/** The same, with the exact instant one hover away. Everything that renders
 *  markup uses this; nothing writes a date into a page without it. */
function when(iso, options) {
  const absent = (options && options.absent) || "never";
  if (!iso) return `<span class="muted">${escapeHtml(absent)}</span>`;
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return `<span class="muted">${escapeHtml(absent)}</span>`;
  return (
    `<time datetime="${escapeHtml(then.toISOString())}"` +
    ` title="${escapeHtml(then.toLocaleString())}">${escapeHtml(timeWords(iso, options))}</time>`
  );
}

/** A duration a person reads at a glance, not a number they parse. */
function duration(ms) {
  if (ms === null || ms === undefined) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/* --- shared header ------------------------------------------------------------- */

/** Fill the client switcher's menu, and switch when one is chosen.
 *
 * Only Inputiv staff have one: a franchisee's client comes from their
 * organisation and the server renders it as text. Switching is a server
 * action that writes an audit row, so the page reloads from the new client
 * rather than trying to patch what is already on screen — a stale panel from
 * the previous client is exactly the mistake this is meant to prevent.
 *
 * **The menu is wired before the clients are fetched.** This used to be one
 * `await` earlier, which meant a button that did nothing at all until a
 * request came back, and nothing whatsoever if it never did — a control with
 * no behaviour and no error is indistinguishable from a broken one. */
function wireClientSwitcher() {
  const button = document.getElementById("client-switch");
  const menu = document.getElementById("client-menu");
  if (!button || !menu || !DESK.maySwitchClient) return;

  const current = DESK.client ? DESK.client.slug : "";
  menu.innerHTML = '<p class="empty">Loading…</p>';

  menu.addEventListener("click", async (event) => {
    const item = event.target.closest("[data-slug]");
    if (!item) return;
    const slug = item.dataset.slug;
    if (!slug || slug === current) return;
    try {
      await api("POST", "/auth/switch-client", { client: slug });
      window.location.reload();
    } catch (error) {
      /* The header has nowhere to put a message, and the page was about to
         reload, so this is told rather than shown. */
      await tell("That client could not be switched to", error.message);
    }
  });

  wireMenu("client-switch", "client-menu");

  api("GET", "/auth/clients")
    .then((clients) => {
      menu.innerHTML = clients.length
        ? clients
            .map(
              (client) =>
                `<button type="button" class="menu-item" role="menuitem"` +
                ` data-slug="${escapeHtml(client.slug)}"` +
                `${client.slug === current ? ' aria-current="true"' : ""}>` +
                `<span>${escapeHtml(client.name)}</span>` +
                `<span class="trailing">${escapeHtml(client.slug)}</span></button>`,
            )
            .join("")
        : '<p class="empty">No franchisees are set up yet.</p>';
    })
    .catch(() => {
      menu.innerHTML = '<p class="empty">The client list could not be read.</p>';
    });
}

/** The nav as a drawer, under 900px.
 *
 * The same nav, moved — not a second copy — so a role that has no Ask still
 * has no Ask here. Opening it moves focus into it and closing it puts focus
 * back on the button, because a control that opens something and then leaves
 * the keyboard behind is a control only a mouse can use. It closes on Esc, on
 * the scrim, on following a link, and when the window is widened past the
 * breakpoint, where the drawer stops existing and a stuck-open state would
 * leave a scrim over a page nobody can dismiss. */
function wireNav() {
  const toggle = document.getElementById("nav-toggle");
  const nav = document.getElementById("shell-nav");
  const scrim = document.getElementById("nav-scrim");
  if (!toggle || !nav || !scrim) return;

  function setOpen(open) {
    nav.dataset.open = open ? "true" : "false";
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    scrim.hidden = !open;
    if (open) {
      const first = nav.querySelector("a, button");
      if (first) first.focus();
    }
  }

  toggle.addEventListener("click", () => setOpen(nav.dataset.open !== "true"));
  scrim.addEventListener("click", () => {
    setOpen(false);
    toggle.focus();
  });
  nav.addEventListener("click", (event) => {
    if (event.target.closest("a")) setOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && nav.dataset.open === "true") {
      setOpen(false);
      toggle.focus();
    }
  });
  window.matchMedia("(min-width: 901px)").addEventListener("change", (event) => {
    if (event.matches) setOpen(false);
  });
  setOpen(false);
}

/* --- what this browser remembers ---------------------------------------------
 *
 * Two per-viewer conveniences, and nothing else: which theme somebody reads in
 * and whether they keep the nav narrow. Both live in this browser only. They
 * never reach the server, they are not part of anybody's account, and a
 * browser that refuses site data simply gets the defaults — so every read and
 * write is wrapped, and the shell renders either way.
 *
 * `_base.html` reads both inline in the head, before the stylesheet paints,
 * which is why the keys are spelled there too. Changing one means changing
 * both. */
function remember(key, value) {
  try {
    localStorage.setItem("desk-" + key, value);
  } catch (error) {
    /* Private window, or site data blocked. The choice holds for this page. */
  }
}

/** Light or dark. The button says which one pressing it *gives* you, because a
 *  control labelled with the state it is already in is a control nobody can
 *  read. `aria-pressed` carries the current state for a screen reader. */
function wireTheme() {
  const button = document.getElementById("theme-toggle");
  const icons = Array.from(document.querySelectorAll("[data-theme-icon]"));
  const label = document.getElementById("theme-label");
  if (!button || !icons.length || !label) return;

  const system = window.matchMedia("(prefers-color-scheme: dark)");

  function current() {
    const chosen = document.documentElement.dataset.theme;
    if (chosen === "dark" || chosen === "light") return chosen;
    return system.matches ? "dark" : "light";
  }

  function draw() {
    const dark = current() === "dark";
    button.setAttribute("aria-pressed", dark ? "true" : "false");
    /* In the dark theme the control offers the light one, so it shows a sun.
       Both drawings are in the markup; this shows one of them. */
    icons.forEach((node) => {
      node.hidden = node.dataset.themeIcon !== (dark ? "light" : "dark");
    });
    label.textContent = dark ? "Use the light theme" : "Use the dark theme";
  }

  button.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    remember("theme", next);
    draw();
  });

  /* Somebody who has never chosen follows their system. Once they choose, the
     choice stands: the stored value is what `data-theme` holds, and the media
     query no longer decides. */
  system.addEventListener("change", () => {
    if (!document.documentElement.dataset.theme) draw();
  });
  draw();
}

/** The nav, narrowed to its glyphs. Only offered where the nav is always on
 *  screen: under 900px it is a drawer, and narrowing a drawer is nothing. */
function wireNavCollapse() {
  const button = document.getElementById("nav-collapse");
  if (!button) return;

  function draw() {
    const collapsed = document.documentElement.dataset.nav === "collapsed";
    const words = collapsed ? "Expand sections" : "Collapse sections";
    button.setAttribute("aria-expanded", collapsed ? "false" : "true");
    button.setAttribute("title", words);
    button.querySelector(".sr-only").textContent = words;
  }

  button.addEventListener("click", () => {
    const collapsed = document.documentElement.dataset.nav === "collapsed";
    if (collapsed) delete document.documentElement.dataset.nav;
    else document.documentElement.dataset.nav = "collapsed";
    remember("nav", collapsed ? "expanded" : "collapsed");
    draw();
  });
  draw();
}

/* --- a menu hung off a header control ------------------------------------------
 *
 * Opened by its button, placed against that button's own rectangle, closed by
 * Esc, by a click anywhere else, and by following one of its own items. Focus
 * goes into it on open and back to the button on close, so the keyboard never
 * ends up somewhere it cannot see. */
function wireMenu(buttonId, menuId) {
  const button = document.getElementById(buttonId);
  const menu = document.getElementById(menuId);
  if (!button || !menu) return;

  function place() {
    const at = button.getBoundingClientRect();
    menu.style.visibility = "hidden";
    menu.hidden = false;
    const box = menu.getBoundingClientRect();
    /* Right-aligned to the button, and nudged back on screen if that would
       hang it off the edge on a narrow window. */
    const left = Math.max(8, Math.min(at.right - box.width, window.innerWidth - box.width - 8));
    menu.style.left = left + "px";
    menu.style.top = at.bottom + 6 + "px";
    menu.style.visibility = "";
  }

  function setOpen(open) {
    if (open) place();
    else menu.hidden = true;
    button.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      const first = menu.querySelector("a, button");
      if (first) first.focus();
    }
  }

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(menu.hidden);
  });
  menu.addEventListener("click", (event) => {
    if (event.target.closest("a, button")) setOpen(false);
  });
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !menu.contains(event.target)) setOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      setOpen(false);
      button.focus();
    }
  });
  window.addEventListener("resize", () => {
    if (!menu.hidden) place();
  });
}

/** One of the four words a cell, a tile or a legend entry can say about a
 *  tool and a machine.
 *
 *  A tick for covered, a word for the three that are not: colour is never
 *  the only signal, and a reader who cannot tell green from amber still
 *  reads "Needs attention".
 *
 *  Here rather than on one page, because the Coverage board and the Devices
 *  table answer the same question about the same machines — does this tool
 *  hold a current record of it — and two drawings of one answer is two
 *  products. `compact` keeps the word for a screen reader and drops it from
 *  the screen, for a column too narrow to carry it. */
const STATE_GLYPH = {
  ok: '<path d="m5 9 3 3 5-6"/><circle cx="9" cy="9" r="8"/>',
  partial: '<path d="M9 5v5"/><path d="M9 13h.01"/><circle cx="9" cy="9" r="8"/>',
  missing: '<path d="m6 6 6 6M12 6l-6 6"/><circle cx="9" cy="9" r="8"/>',
  unknown: '<path d="M5 9h8"/><circle cx="9" cy="9" r="8"/>',
};

function coverageState(state, label, compact = false) {
  const glyph = STATE_GLYPH[state] || STATE_GLYPH.unknown;
  const hidden = compact ? ' class="sr-only"' : "";
  return (
    `<span class="coverage-state ${state}${compact ? " compact" : ""}" title="${escapeHtml(label)}">` +
    `<svg viewBox="0 0 18 18" aria-hidden="true">${glyph}</svg>` +
    `<span${hidden}>${escapeHtml(label)}</span></span>`
  );
}

/** A vendor's mark, as markup, from the one map the server handed over.
 *
 *  `DESK.vendorMarks` is `VENDOR_MARKS` in `app/interfaces/web.py`, the same
 *  list `_logos.html` draws from, so a column header written by Jinja and a
 *  tile written by a script cannot show one vendor two ways. A vendor the map
 *  does not hold draws nothing and reads as its name, which every caller
 *  prints beside the mark anyway. */
function vendorMark(key) {
  const mark = (DESK.vendorMarks || {})[key];
  if (!mark) return "";
  if (mark.src) {
    return `<img class="brand-mark" src="${escapeHtml(mark.src)}" alt="" width="22" height="22">`;
  }
  return (
    `<span class="brand-mark mono" style="--tint: ${escapeHtml(mark.tint)}"` +
    ` aria-hidden="true">${escapeHtml(mark.letter)}</span>`
  );
}

/** A person, as their initials on a coloured tile.
 *
 *  Two things it is not: a photograph, which we do not hold, and a decoration.
 *  A column of names reads as a column of text; the same column with a mark
 *  beside each name is a column somebody can find their own row in without
 *  reading it. The colour is a hash of the name, so one person is the same
 *  colour everywhere in the product and two people beside each other are
 *  almost never the same — it carries no meaning of its own and nothing is
 *  ever said with it alone.
 *
 *  The name is always printed beside it: initials are not an identifier. */
function initialsOf(name) {
  const words = String(name || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!words.length) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/** A hue from the name. Deterministic, and the same on every page. */
function avatarHue(name) {
  let hash = 0;
  const text = String(name || "");
  for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) % 360;
  return hash;
}

/** The tile alone, for a cell that prints the name itself. */
function personAvatar(name) {
  return (
    `<span class="person-avatar" style="--avatar-h: ${avatarHue(name)}" aria-hidden="true">` +
    `${escapeHtml(initialsOf(name))}</span>`
  );
}

/** The tile and the name, which is how it is used nearly everywhere. */
function person(name, fallback) {
  const clean = String(name || "").trim();
  if (!clean) return `<span class="muted">${escapeHtml(fallback || "nobody recorded")}</span>`;
  return `<span class="person">${personAvatar(clean)}<span>${escapeHtml(clean)}</span></span>`;
}

/** A vendor's name as the one map spells it. */
function vendorName(key) {
  const mark = (DESK.vendorMarks || {})[key];
  return mark ? mark.name : key;
}

/** One of the shell's own icons, as markup, for a list a script draws.
 *
 *  Empty when the template is not on the page, which is a row with no icon
 *  rather than a row that fails to draw. */
function iconMarkup(id) {
  const template = document.getElementById(id);
  return template ? template.innerHTML : "";
}

/* --- the command palette ---------------------------------------------------------
 *
 * Everywhere this session may go, in one list. It holds two kinds of thing and
 * invents neither: the pages this role is shown in the nav (`DESK.pages`, built
 * server-side from the same list the nav is drawn from), and — only for the
 * staff who may switch — the clients the header switcher already offers.
 *
 * Clients are loaded once, on the first open, from the same endpoint the
 * switcher uses. A session that may not switch never asks. */
let paletteClients = null;

async function loadPaletteClients() {
  if (paletteClients || !DESK.maySwitchClient) return;
  try {
    const clients = await api("GET", "/auth/clients");
    paletteClients = clients.map((client) => ({
      label: client.name,
      group: "Client",
      slug: client.slug,
    }));
  } catch (error) {
    /* The palette still lists pages; a failed client read is not worth a
       dialog over a control somebody opened to navigate. */
    paletteClients = [];
  }
}

function wirePalette() {
  const open = document.getElementById("palette-open");
  const panel = document.getElementById("palette");
  const input = document.getElementById("palette-input");
  const results = document.getElementById("palette-results");
  if (!open || !panel || !input || !results) return;

  let items = [];
  let active = 0;
  let opener = null;
  /* The two drawings a row can carry, taken from the markup the shell wrote
     rather than from a copy of the paths here: every icon in the product is
     in `_icons.html` and this clones one. */
  const pageIcon = iconMarkup("icon-page");
  const clientIcon = iconMarkup("icon-client");

  function matches(query) {
    const pages = (DESK.pages || []).map((page) => ({
      label: page.label,
      group: page.group || "Page",
      href: page.href,
    }));
    const all = pages.concat(paletteClients || []);
    const wanted = query.trim().toLowerCase();
    if (!wanted) return all;
    return all.filter((item) =>
      (item.label + " " + item.group).toLowerCase().includes(wanted),
    );
  }

  function draw(query) {
    items = matches(query);
    active = 0;
    if (!items.length) {
      results.innerHTML = '<p class="empty">Nothing by that name.</p>';
      return;
    }
    results.innerHTML = items
      .map(
        (item, index) =>
          `<button type="button" class="menu-item" role="option" data-index="${index}"` +
          ` aria-selected="${index === 0 ? "true" : "false"}">` +
          `<span class="glyph">${item.slug ? clientIcon : pageIcon}</span>` +
          `<span>${escapeHtml(item.label)}</span>` +
          `<span class="trailing">${escapeHtml(item.group)}</span></button>`,
      )
      .join("");
  }

  function highlight(next) {
    const buttons = results.querySelectorAll("[role='option']");
    if (!buttons.length) return;
    active = (next + buttons.length) % buttons.length;
    buttons.forEach((button, index) =>
      button.setAttribute("aria-selected", index === active ? "true" : "false"),
    );
    buttons[active].scrollIntoView({ block: "nearest" });
  }

  async function choose(index) {
    const item = items[index];
    if (!item) return;
    setOpen(false);
    if (item.href) {
      window.location.href = item.href;
      return;
    }
    /* Switching is a server action that writes an audit row, so the page
       reloads from the new client rather than patching what is on screen. */
    try {
      await api("POST", "/auth/switch-client", { client: item.slug });
      window.location.reload();
    } catch (error) {
      await tell("That client could not be switched to", error.message);
    }
  }

  function setOpen(isOpen) {
    panel.hidden = !isOpen;
    if (isOpen) {
      opener = document.activeElement;
      input.value = "";
      draw("");
      input.focus();
      loadPaletteClients().then(() => {
        if (!panel.hidden) draw(input.value);
      });
    } else if (opener && opener.isConnected) {
      opener.focus();
    }
  }

  open.addEventListener("click", () => setOpen(true));
  input.addEventListener("input", () => draw(input.value));
  results.addEventListener("click", (event) => {
    const button = event.target.closest("[data-index]");
    if (button) choose(Number(button.dataset.index));
  });
  panel.addEventListener("click", (event) => {
    if (event.target === panel) setOpen(false);
  });
  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
    else if (event.key === "ArrowDown") {
      event.preventDefault();
      highlight(active + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlight(active - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(active);
    }
  });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      setOpen(panel.hidden);
    }
  });
}

function wireSignOut() {
  const button = document.getElementById("sign-out");
  if (!button) return;
  button.addEventListener("click", async () => {
    try {
      await fetch("/auth/logout", { method: "POST", headers: unsafeHeaders() });
    } catch (error) {
      /* The session may already be gone; the next step is the same either way. */
    }
    window.location.href = DESK.loginPath || "/login";
  });
}

/** Shown when an Inputiv session has not chosen a client yet. Every
 *  client-scoped endpoint answers 409 until one is chosen, so a page says so
 *  once rather than letting each panel fail on its own. */
function needsClient() {
  return DESK.maySwitchClient && !DESK.client;
}

function chooseClientNotice() {
  return nothingHere(
    "Your session is not pointed at a franchisee yet, and every figure on this page is one client's.",
    { text: "Pick one in the header. The choice is recorded.", href: "#client-switch" },
  );
}

/* --- nothing here, and what did not work ----------------------------------- */

/** Whether the person reading this is one of ours.
 *
 *  Decides who is shown a failure's raw detail: a vendor's error body or a
 *  stack of ours is something an Inputiv engineer can act on and a franchisee
 *  can only be alarmed by. Never a permission — every endpoint resolves the
 *  session itself — only who the words are written for. */
function isStaff() {
  return String(DESK.role || "").startsWith("inputiv_");
}

/**
 * Nothing here: a sentence, and the next thing somebody would do.
 *
 * One shape for every empty list on every page, inside a table cell or out, so
 * "nothing yet" reads the same way wherever it appears. The next action is
 * optional because inventing one is worse than leaving it out: a list that is
 * empty because the estate is healthy has nothing for anybody to do.
 *
 * @param {string} sentence  what is missing, as a sentence.
 * @param {object} [next]    {text, href} for a link, {text, command} for the
 *                           command that fills it, or {text} for a line that
 *                           says where the thing comes from when nobody
 *                           reading this page is the one who does it.
 */
function nothingHere(sentence, next) {
  let after = "";
  if (next && next.href) {
    after = `<a href="${escapeHtml(next.href)}">${escapeHtml(next.text)}</a>`;
  } else if (next && next.command) {
    after = `${escapeHtml(next.text || "Run")} <code>${escapeHtml(next.command)}</code>`;
  } else if (next && next.text) {
    after = escapeHtml(next.text);
  }
  return `<div class="empty">${escapeHtml(sentence)}${
    after ? `<span class="next">${after}</span>` : ""
  }</div>`;
}

/** The same, as a row that spans a table. */
function nothingRow(columns, sentence, next) {
  return `<tr><td colspan="${columns}">${nothingHere(sentence, next)}</td></tr>`;
}

/** What to do about a failure, by what kind it was. */
const ADVICE = {
  401: "Your session has ended. Sign in again.",
  403: "Your account is not allowed to do that.",
  404: "It is not there any more.",
  409: "Choose a client in the header first.",
  429: "Too much at once. Try again in a moment.",
};

/** What to do about it, when the caller has not said.
 *
 *  A status is a coarse thing to write a sentence from — a 403 is "you may not"
 *  on one route and "that password is wrong" on another — so a caller that
 *  knows better passes its own, and this is the fallback for the ones that
 *  cannot know. */
function advice(error) {
  const status = error && error.status;
  if (ADVICE[status]) return ADVICE[status];
  if (status >= 500) {
    return "The engine failed on it. Try again; if it keeps happening, send us the detail.";
  }
  if (!status) return "The engine could not be reached. Check your connection and try again.";
  return "Try again in a moment.";
}

/**
 * A failure, in words somebody can act on.
 *
 * The server's own message is never the message. It is written for whoever
 * reads the log — "404: Not Found", "Invalid value for 'parentId'" — and a
 * reader who gets that instead of a sentence has been told that something broke
 * and nothing else. So the page says what did not work and what to do, and the
 * raw detail goes behind "Details" for the people who can act on it.
 *
 * @param {string} what     what did not work: "Couldn't load the device list."
 * @param {Error} error     the failure, for its status and its detail.
 * @param {string} [next]   what to do about it, where the caller knows better
 *                          than the status does.
 */
function problem(what, error, next) {
  const detail = error && error.message ? String(error.message) : "";
  const more =
    detail && isStaff()
      ? '<details class="detail-box"><summary>Details</summary><p></p></details>'
      : "";
  const box = document.createElement("div");
  box.innerHTML =
    `<strong>${escapeHtml(what)}</strong> ` +
    `<span class="advice">${escapeHtml(next || advice(error))}</span>${more}`;
  if (more) box.querySelector("details p").textContent = detail;
  return box.innerHTML;
}

/* --- asking --------------------------------------------------------------- */

/* One dialog, for every question this product asks.
 *
 * It replaced `window.prompt`, `window.confirm` and `alert`, which between them
 * had three problems. They ignore the design system, so the one moment somebody
 * is being asked to do something irreversible looked like nothing else on the
 * screen. They open at the top of the browser window rather than near the thing
 * they are about. And they cannot say which of the two answers is the
 * destructive one, which is the whole point of stopping to ask.
 *
 * The element is built here rather than put in every template: a page should
 * not have to carry markup to be able to ask a question.
 */

/**
 * Ask a question and wait for the answer.
 *
 * @param {object} options
 * @param {string} options.title    the question, as a question.
 * @param {string} [options.body]   what pressing the confirm button will do.
 * @param {object} [options.reason] a field to fill in: {label, placeholder,
 *                                  value, required, missing}. `missing` is what
 *                                  to say when a required one is left empty.
 * @param {string} [options.confirm] the confirm button's words. Name the act —
 *                                  "Suspend", "Withdraw" — never "OK".
 * @param {string} [options.cancel] the cancel button's words.
 * @param {"primary"|"destructive"} [options.tone] which the confirm button is.
 * @returns {Promise<string|true|null>} null if cancelled; the reason if there
 *          was a field; true otherwise.
 */
function ask(options) {
  return new Promise((resolve) => {
    const cameFrom = document.activeElement;
    const scrim = document.createElement("div");
    scrim.className = "scrim";
    const panel = document.createElement("div");
    panel.className = "dialog";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");

    const titleId = "dialog-title";
    const field = options.reason;
    panel.innerHTML =
      `<h2 id="${titleId}"></h2>` +
      (options.body ? "<p></p>" : "") +
      (field
        ? '<label class="field"><span></span>' +
          '<textarea id="dialog-reason" rows="3"></textarea></label>'
        : "") +
      '<p class="says" id="dialog-says" hidden></p>' +
      '<div class="actions">' +
      '<button type="button" class="btn secondary" id="dialog-cancel"></button>' +
      '<button type="button" class="btn" id="dialog-confirm"></button>' +
      "</div>";
    panel.setAttribute("aria-labelledby", titleId);
    panel.querySelector("h2").textContent = options.title;
    if (options.body) panel.querySelector("p").textContent = options.body;
    if (field) {
      const input = panel.querySelector("#dialog-reason");
      panel.querySelector(".field > span").textContent = field.label || "Why?";
      input.placeholder = field.placeholder || "";
      input.value = field.value || "";
    }
    const confirm = panel.querySelector("#dialog-confirm");
    confirm.textContent = options.confirm || "Confirm";
    confirm.classList.add(options.tone === "destructive" ? "destructive" : "primary");
    const cancel = panel.querySelector("#dialog-cancel");
    cancel.textContent = options.cancel || "Cancel";
    /* A dialog with nothing to decide — a failure somebody has to be told about
       — has one way out, and it is not called Cancel. */
    if (options.only) {
      cancel.hidden = true;
      confirm.classList.remove("destructive", "primary");
      confirm.classList.add("secondary");
    }

    document.body.append(scrim, panel);

    function close(answer) {
      document.removeEventListener("keydown", onKey, true);
      scrim.remove();
      panel.remove();
      if (cameFrom && cameFrom.focus) cameFrom.focus();
      resolve(answer);
    }

    function take() {
      if (!field) {
        close(true);
        return;
      }
      const value = panel.querySelector("#dialog-reason").value;
      if (field.required && !value.trim()) {
        const says = panel.querySelector("#dialog-says");
        says.textContent = field.missing || "This needs a reason.";
        says.hidden = false;
        panel.querySelector("#dialog-reason").focus();
        return;
      }
      close(value);
    }

    /* Focus goes into the dialog and stays there while it is open: a modal
       somebody can tab out of is a modal in appearance only. */
    function onKey(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        close(null);
        return;
      }
      if (event.key !== "Tab") return;
      const stops = panel.querySelectorAll("button:not([hidden]), textarea, input, a[href]");
      if (!stops.length) return;
      const first = stops[0];
      const last = stops[stops.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!panel.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    }

    confirm.addEventListener("click", take);
    cancel.addEventListener("click", () => close(null));
    scrim.addEventListener("click", () => close(null));
    document.addEventListener("keydown", onKey, true);

    (field ? panel.querySelector("#dialog-reason") : confirm).focus();
  });
}

/* --- what starts a workflow, in a few words ----------------------------------
 *
 * The server's trigger sentence is the full rule ("When an alert raises a
 * ticket for X from source Monitoring Alert and whose title contains one of
 * ..."). A list needs the gist: "When a disk space alert arrives". Built from
 * the trigger's kind and its short title; the full rule stays behind Details
 * on the workflow page. */
function triggerWords(trigger) {
  if (trigger.kind === "manual") return "by hand";
  if (trigger.kind === "schedule") {
    const every = /every ([^,.]+)/i.exec(trigger.sentence || "");
    return every ? `every ${every[1].trim()}` : "on a schedule";
  }
  return `when ${trigger.title} arrives`;
}

/** One sentence for a workflow's triggers: "When an offboarding ticket
 *  arrives, every day, or by hand." A trigger that is off says so. */
function startsWhen(triggers) {
  const parts = (triggers || []).map((t) => triggerWords(t) + (t.kind !== "manual" && !t.enabled ? " (off)" : ""));
  if (!parts.length) return "Never.";
  const joined = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")}, or ${parts[parts.length - 1]}`;
  return joined.charAt(0).toUpperCase() + joined.slice(1) + ".";
}

/** Something somebody has to be told, with nothing to decide. One way out.
 *
 *  For a failure that has no region of the page to land in — the client
 *  switcher in the header, an action in a rail. Where a page has somewhere to
 *  put it, the message belongs there instead, nearer what failed. */
function tell(title, body) {
  return ask({ title, body, confirm: "Close", only: true });
}

document.addEventListener("DOMContentLoaded", () => {
  wireNav();
  wireNavCollapse();
  wireTheme();
  wireMenu("user-menu-open", "user-menu");
  wirePalette();
  wireSignOut();
  wireClientSwitcher();
});

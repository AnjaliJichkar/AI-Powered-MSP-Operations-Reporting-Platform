/* The ticket queue, and the shape of the week above it.
 *
 * Two endpoints, both read-only. `GET /tickets/list` is the queue, computed
 * from Autotask on every load; `GET /tickets/statistics` is the cards and the
 * charts, two listings the server keeps for a few minutes with every figure's
 * definition beside it. This page adds no data to either; what it adds is an
 * order, a vocabulary and a way in.
 *
 *   - The queue is sorted by how soon a clock runs out, not by age, and each
 *     ticket carries a badge saying which: breached, due within four hours,
 *     due today, in time, or no clock at all.
 *   - The raw SLA string ("first_response overdue by 187.5 h") is rewritten as
 *     a sentence and kept as the tooltip, so nothing is lost.
 *   - **A card and a chip are the same question.** Every card above is the
 *     filter it describes, and the chips are those same filters, so a figure
 *     and the rows it opens can never be two different populations.
 *
 * What the reader is looking at is in the address bar — the chips and the
 * search — so the breadcrumb on a ticket's own page can bring them back to
 * exactly this queue rather than to a queue.
 *
 * A row opens the ticket at `/tickets/<number>`: its own page, its own address,
 * its own back button. Nothing here writes to a vendor.
 */

const summary = document.getElementById("summary");
const errorBox = document.getElementById("error");
const pendingBox = document.getElementById("pending");
const listSub = document.getElementById("list-sub");
const filtersList = document.getElementById("filters-list");
const filtersButton = document.getElementById("filters-button");
const queueTabs = document.getElementById("queue-tabs");
const searchBox = document.getElementById("search");
const ticketList = document.getElementById("tickets");
const statsWhen = document.getElementById("stats-when");
const statsError = document.getElementById("stats-error");
const statCards = document.getElementById("stat-cards");
/* The same cards, above the queue. One renderer writes both: a second copy of
   `cardButton` is how a figure and the rows it opens drift apart. */
const queueCards = document.getElementById("queue-cards");
const chartGrid = document.getElementById("charts");
const definitionsBody = document.getElementById("definitions-body");

let tickets = [];
let stats = null;

/** What the agent suggested, keyed by ticket id — Inputiv staff only, from an
 *  endpoint of its own. A franchisee's page never asks for this and would be
 *  refused if it did, so their queue is exactly what it was. */
let suggestions = {};
let suggestionsLive = false;
let suggestionWarnings = 0;

const IS_STAFF = String(DESK.role || "").startsWith("inputiv");

/** The signed-in person's name, for "Mine". Autotask's assignee is a person's
 *  name and never an email address, so an account with no name recorded has
 *  nothing to compare and the chip says so rather than showing an empty queue. */
const MY_NAME = (DESK.name || "").trim().toLowerCase();

const FOUR_HOURS_MS = 4 * 60 * 60 * 1000;

function fail(error, what) {
  errorBox.innerHTML = problem(what || "Something on the ticket queue did not work.", error);
  errorBox.hidden = false;
}

/* --- urgency: the one order the queue is in ---------------------------------------- */

/** Which clock Autotask set, in words. `sla.detail` starts with its name. */
const CLOCK_NAMES = {
  due: "the ticket's due date",
  first_response: "a first reply",
  resolution: "a resolution",
};

/** The badges, worst first. `rank` is the sort key and `test` decides which
 *  badge a ticket wears, so the order on screen and the badge on the row can
 *  never disagree: they are read from the same list. */
const URGENCY = [
  {
    key: "breached",
    rank: 0,
    tone: "bad",
    mark: "✕",
    label: "Breached",
    reason: "A clock on this ticket has already run out.",
  },
  {
    key: "four_hours",
    rank: 1,
    tone: "bad",
    mark: "!",
    label: "Due within 4 hours",
    reason: "A clock on this ticket runs out in the next four hours.",
  },
  {
    key: "today",
    rank: 2,
    tone: "warn",
    mark: "!",
    label: "Due today",
    reason: "A clock on this ticket runs out before the end of today.",
  },
  {
    key: "later",
    rank: 3,
    tone: "good",
    mark: "✓",
    label: "In time",
    reason: "The next clock on this ticket runs out after today.",
  },
  {
    key: "paused",
    rank: 4,
    tone: "neutral",
    mark: "⏸",
    label: "Clock stopped",
    reason:
      "Waiting on the customer, a part or a vendor. The clock is stopped and nothing is judged.",
  },
  {
    key: "none",
    rank: 5,
    tone: "neutral",
    mark: "–",
    label: "No target",
    reason: "This franchisee has set no target for this ticket's priority.",
  },
];

const URGENCY_BY_KEY = Object.fromEntries(URGENCY.map((u) => [u.key, u]));

function endOfToday() {
  const end = new Date();
  end.setHours(23, 59, 59, 999);
  return end.getTime();
}

function urgency(ticket) {
  const sla = ticket.sla || {};
  if (sla.state === "breached") return URGENCY_BY_KEY.breached;
  if (sla.state === "paused") return URGENCY_BY_KEY.paused;
  if (!sla.due_at) return URGENCY_BY_KEY.none;
  const due = new Date(sla.due_at).getTime();
  if (due - Date.now() <= FOUR_HOURS_MS) return URGENCY_BY_KEY.four_hours;
  if (due <= endOfToday()) return URGENCY_BY_KEY.today;
  return URGENCY_BY_KEY.later;
}

function urgencyBadge(ticket) {
  const u = urgency(ticket);
  return toneChip(u.tone, u.label, u.reason);
}

/** The SLA line as a sentence. The API's own wording is kept as the tooltip,
 *  so the exact figure it computed is one hover away and nothing is lost. */
function clockSentence(ticket) {
  const sla = ticket.sla || {};
  /* The server writes a sentence for every state, from the franchisee's own
     targets, so there is nothing to rewrite here when it is not a countdown. */
  if (sla.state === "paused" || sla.state === "none") return sla.detail || "";
  if (!sla.due_at) return sla.detail || "No target is set for this ticket's priority.";
  const kind = String(sla.detail || "").split(" ")[0];
  const name = CLOCK_NAMES[kind] || "a clock";
  const sentence = `${name} is ${humanDue(sla.due_at)}`;
  return sentence.charAt(0).toUpperCase() + sentence.slice(1) + ".";
}

/** Worst first; among equals, the soonest clock, then the oldest ticket. */
function byUrgency(a, b) {
  const rank = urgency(a).rank - urgency(b).rank;
  if (rank) return rank;
  const dueA = a.sla && a.sla.due_at ? new Date(a.sla.due_at).getTime() : Infinity;
  const dueB = b.sla && b.sla.due_at ? new Date(b.sla.due_at).getTime() : Infinity;
  if (dueA !== dueB) return dueA - dueB;
  return (b.age_days || 0) - (a.age_days || 0);
}

/* --- the filters, which are also the cards --------------------------------------------
 *
 * One list. A card above and a chip below with the same key are the same
 * question asked twice, so the figure and the rows it opens are the same
 * population by construction rather than by two people remembering to agree.
 */

/** The instant the server counted "today" from, so the chip agrees with the
 *  card even when the reader is in a different country from the customer. */
function todayStart() {
  if (stats && stats.boundaries && stats.boundaries.today) {
    return new Date(stats.boundaries.today).getTime();
  }
  const local = new Date();
  local.setHours(0, 0, 0, 0);
  return local.getTime();
}

const TICKET_FILTERS = [
  {
    key: "mine",
    label: "Mine",
    reason: "Assigned to you, by the name on your account.",
    available: () => Boolean(MY_NAME),
    test: (t) => Boolean(MY_NAME) && String(t.assigned_to || "").trim().toLowerCase() === MY_NAME,
  },
  {
    key: "breached",
    label: "Breached",
    reason: "A clock on the ticket has already run out.",
    available: () => true,
    test: (t) => urgency(t).key === "breached",
  },
  {
    key: "four_hours",
    label: "Due within 4 hours",
    reason: "Not breached yet, and a clock runs out in the next four hours.",
    available: () => true,
    test: (t) => urgency(t).key === "four_hours",
  },
  {
    key: "unassigned",
    label: "Unassigned",
    reason: "Autotask has nobody down as working on it.",
    available: () => true,
    test: (t) => !String(t.assigned_to || "").trim(),
  },
  {
    key: "paused",
    label: "Clock stopped",
    reason: "Waiting on the customer, a part or a vendor: the clock is stopped and nothing judged.",
    available: () => true,
    test: (t) => urgency(t).key === "paused",
  },
  {
    key: "has_warnings",
    label: "⚠ Has warnings",
    reason:
      "Desk found something that changes how the ticket is handled: a machine that is not " +
      "there, an alert older than the thing it is about, a failed backup, a security finding.",
    available: () => IS_STAFF && suggestionsLive,
    test: (t) => Boolean((suggestions[t.ticket_id] || {}).warning),
  },
  {
    key: "needs_review",
    label: "Needs review",
    reason:
      "Suggested by the agent and not yet marked correct, changed or wrong. " +
      "Newest first, because a suggestion is worth most while the ticket is still open.",
    available: () => IS_STAFF && suggestionsLive,
    test: (t) => {
      const found = suggestions[t.ticket_id];
      return Boolean(found) && !found.reviewed;
    },
  },
  {
    key: "created_today",
    label: "Created today",
    reason: "Raised since midnight, in the customer's own time zone.",
    available: () => true,
    test: (t) => Boolean(t.created_at) && new Date(t.created_at).getTime() >= todayStart(),
  },
];

/* The tabs: everything, and the two the agent's work adds. They are filters
   like the rest — the same keys, the same tests — drawn as tabs because they
   are the three views somebody actually moves between, and a tab says "you are
   looking at this" where a chip says "this is on". */
const TABS = [
  { key: "", label: "All" },
  { key: "needs_review", label: "Needs review" },
  { key: "has_warnings", label: "Warnings" },
];

function renderTabs(state) {
  const usable = TABS.filter(
    (tab) => !tab.key || TICKET_FILTERS.find((f) => f.key === tab.key).available()
  );
  queueTabs.innerHTML = usable
    .map((tab) => {
      const filter = tab.key && TICKET_FILTERS.find((f) => f.key === tab.key);
      const count = filter ? tickets.filter(filter.test).length : tickets.length;
      const on = tab.key ? state.chips.has(tab.key) : !state.chips.size;
      return `<button type="button" role="tab" data-tab="${escapeHtml(tab.key)}"
        aria-selected="${on}"${filter ? ` title="${escapeHtml(filter.reason)}"` : ""}>${escapeHtml(
          tab.label
        )}<span class="count">${count}</span></button>`;
    })
    .join("");
  for (const button of queueTabs.querySelectorAll("[data-tab]")) {
    button.addEventListener("click", () => {
      const next = urlState();
      /* A tab is a view rather than a toggle: choosing one puts the queue in
         that view and leaves the Filters menu's own choices alone. */
      for (const tab of TABS) if (tab.key) next.chips.delete(tab.key);
      if (button.dataset.tab) next.chips.add(button.dataset.tab);
      writeUrl(next);
      drawList();
    });
  }
}

/* --- the address bar is the page's state ------------------------------------------- */

function urlState() {
  const params = new URLSearchParams(window.location.search);
  return {
    chips: new Set((params.get("chips") || "").split(",").filter(Boolean)),
    view: params.get("view") || "",
  };
}

/** The query string this state is, so a ticket's page can hand it back. */
function queryFor(state) {
  const params = new URLSearchParams();
  const chips = [...state.chips].join(",");
  if (chips) params.set("chips", chips);
  if (state.view) params.set("view", state.view);
  return params.toString();
}

function writeUrl(state) {
  const query = queryFor(state);
  history.replaceState(history.state, "", query ? `?${query}` : window.location.pathname);
}

/* --- the statistics ----------------------------------------------------------------- */

/** Whether this page implements the filter a card names. A card pointing at a
 *  filter that does not exist would quietly do nothing, which is the one way a
 *  figure and the rows it opens could become two different populations; it is
 *  drawn as unpressable and says so instead. The server names the same set in
 *  `app/tickets/statistics.py`, and a test holds the two together. */
function cardIsUsable(card) {
  if (!card.filter) return false;
  if (card.filter === "none" || card.filter === "resolved_week") return true;
  return TICKET_FILTERS.some((f) => f.key === card.filter);
}

function cardButton(card, state) {
  if (!cardIsUsable(card)) {
    return `<div class="kpi stat ${escapeHtml(card.tone)}" title="${escapeHtml(card.definition)}">
      <span class="meta muted">${escapeHtml(card.label)}</span>
      <span class="value">${card.value}</span>
      <small class="muted">Does not narrow the queue</small>
    </div>`;
  }
  const pressed =
    card.filter === "none"
      ? !state.chips.size && !state.view
      : card.filter === "resolved_week"
        ? state.view === "resolved_week"
        : state.chips.has(card.filter);
  /* The definition is the `title` — the hover the brief asks for — and it is
     also written out in full under "What each of these numbers counts", which
     is what a projector, a phone and a screen reader get instead of a hover.
     It is not printed in the card: six paragraphs of definition is a wall, and
     the figure is what somebody is looking at. */
  return `<button type="button" class="kpi stat ${escapeHtml(card.tone)}" data-filter="${escapeHtml(card.filter || "")}"
      aria-pressed="${pressed}" title="${escapeHtml(card.definition)}">
      <span class="meta muted">${escapeHtml(card.label)}</span>
      <span class="value">${card.value}</span>
    </button>`;
}

function chartBlock(chart, body, className = "") {
  /* A chart's remainder — the customers outside the top five — is printed under
     it rather than drawn, so the bars that are there stay comparable and the
     ones that are not are still counted. */
  const rest =
    chart.remainder && chart.remainder.note
      ? `<p class="small muted" style="margin-top: var(--space-2)">${escapeHtml(chart.remainder.note)}</p>`
      : "";
  return `<div class="chart ${className}">
    <h3>${escapeHtml(chart.title)}</h3>
    <p class="definition">${escapeHtml(chart.definition)}</p>
    ${body}${rest}
  </div>`;
}

function renderStats(state) {
  if (!stats) return;
  const openCard = stats.cards.find((c) => c.key === "open");
  const markup = stats.cards.map((card) => cardButton(card, state)).join("");
  /* The same markup in both places: above the queue, where the figures are the
     first thing a reader meets, and inside the panel beside the charts they
     belong with. Rendering them twice from one function is what keeps a card
     and the rows it opens the same population. */
  for (const grid of [queueCards, statCards]) {
    if (!grid) continue;
    grid.innerHTML = markup;
    for (const button of grid.querySelectorAll("button[data-filter]")) {
      button.addEventListener("click", () => applyCard(button.dataset.filter));
    }
  }
  const charts = stats.charts;
  chartGrid.innerHTML = [
    chartBlock(charts.by_priority, barList(charts.by_priority.series, { total: openCard.value })),
    chartBlock(charts.by_customer, barList(charts.by_customer.series, { total: openCard.value })),
    chartBlock(charts.by_technician, barList(charts.by_technician.series, { total: openCard.value })),
    chartBlock(charts.created_vs_resolved, dayColumns(charts.created_vs_resolved.days), "wide"),
  ].join("");
  definitionsBody.innerHTML = `<dl class="facts">${[
    ...stats.cards.map((c) => [c.label, c.definition]),
    ...Object.values(charts).map((c) => [c.title, c.definition]),
  ]
    .map(([term, definition]) => `<dt>${escapeHtml(term)}</dt><dd>${escapeHtml(definition)}</dd>`)
    .join("")}</dl>`;
  const minutes = Math.round((stats.ttl_seconds || 0) / 60);
  statsWhen.textContent = `Worked out ${timeWords(stats.computed_at)}${
    stats.cached ? ", from the copy kept on the server" : ""
  } · kept for ${plural(minutes || 1, "minute")} · days counted in ${stats.time_zone}${
    stats.truncated ? " · Autotask's listing stopped at its page cap, so these are lower bounds" : ""
  }.`;
  statsWhen.title = new Date(stats.computed_at).toLocaleString();
}

function applyCard(filterKey) {
  const state = urlState();
  if (filterKey === "none") {
    state.chips = new Set();
    state.view = "";
  } else if (filterKey === "resolved_week") {
    state.view = state.view === "resolved_week" ? "" : "resolved_week";
    state.chips = new Set();
  } else {
    state.view = "";
    if (state.chips.has(filterKey)) state.chips.delete(filterKey);
    else state.chips.add(filterKey);
  }
  writeUrl(state);
  drawList();
}

async function loadStats() {
  statsError.hidden = true;
  try {
    stats = await api("GET", "/tickets/statistics");
    renderStats(urlState());
    drawList(); /* "Created today" can only be filtered once the boundary is known. */
  } catch (error) {
    stats = null;
    statCards.innerHTML = "";
    if (queueCards) queueCards.innerHTML = "";
    chartGrid.innerHTML = "";
    statsWhen.textContent = "";
    statsError.textContent = `The statistics could not be worked out: ${error.message}`;
    statsError.hidden = false;
  }
}

/* --- the chips ---------------------------------------------------------------------- */

/* The rest of the filters, behind one button. The tabs' two keys are not here:
   a filter in two places is a filter somebody turns on twice and off once. */
function renderFilters(state) {
  const rest = TICKET_FILTERS.filter((f) => !TABS.some((tab) => tab.key === f.key));
  const on = rest.filter((f) => state.chips.has(f.key)).length;
  filtersButton.textContent = on ? `Filters ▾ ${on}` : "Filters ▾";
  filtersList.innerHTML = rest
    .map((f) => {
      const usable = f.available();
      const count = usable ? tickets.filter(f.test).length : 0;
      return `<button type="button" data-chip="${escapeHtml(f.key)}" aria-pressed="${state.chips.has(
        f.key
      )}"${usable ? "" : " disabled"} title="${escapeHtml(f.reason)}">
        <span class="tick" aria-hidden="true">${state.chips.has(f.key) ? "✓" : ""}</span>
        ${escapeHtml(f.label)}<span class="count">${usable ? count : "—"}</span></button>`;
    })
    .join("");
  for (const button of filtersList.querySelectorAll("button[data-chip]")) {
    button.addEventListener("click", () => {
      const next = urlState();
      if (next.chips.has(button.dataset.chip)) next.chips.delete(button.dataset.chip);
      else next.chips.add(button.dataset.chip);
      writeUrl(next);
      drawList();
    });
  }
}

function ticketHref(ticketNumber, state) {
  const query = queryFor(state);
  return `/tickets/${encodeURIComponent(ticketNumber)}${query ? `?${query}` : ""}`;
}

/** What Desk found, small enough for a queue row: the top-ranked finding, a
 *  second one if there is room, then "+N". The classification is deliberately
 *  absent — Autotask's own list already shows a type beside every ticket, and
 *  a second one of ours in the same row is noise. */
function deskFound(t) {
  const found = suggestions[t.ticket_id];
  if (!found) return '<span class="muted">No confident suggestion</span>';
  if (!(found.findings || []).length) return '<span class="muted">Nothing to flag</span>';
  const chips = found.findings
    .map(
      (one) =>
        `<span class="state"><span class="dot ${
          one.tone === "warning" ? "warn" : "neutral"
        }"></span><span class="word">${escapeHtml(one.chip)}</span></span>`
    )
    .join("");
  const more = found.more ? `<span class="muted">+${found.more} more</span>` : "";
  return `<span class="desk-found">${chips}${more}</span>`;
}

/** One ticket, in two lines: what it is, and the facts about it joined by the
 *  middle dot — number, customer, age, who has it, and where its clock stands.
 *  Then what Desk found, then the one state the row is in.
 *
 *  The clock is in the meta line rather than a column of its own because the
 *  queue is *ordered* by it: the first row is the worst one, and saying so in
 *  six columns was six columns saying it. */
function ticketRow(t, state) {
  const href = ticketHref(t.ticket_number || "", state);
  const u = urgency(t);
  /* Where the row's own state is the clock, the meta line does not say it
     again: one fact, in one place, is the whole point of the line. */
  const desk = IS_STAFF && suggestionsLive ? suggestions[t.ticket_id] : null;
  const meta = [
    t.ticket_number || "",
    t.site,
    t.created_at ? timeWords(t.created_at) : null,
    desk && u.key !== "none" ? u.label.toLowerCase() : null,
  ]
    .filter(Boolean)
    .map((part) => escapeHtml(String(part)))
    .join(" · ");
  return `<a class="list-row ticket-row" href="${escapeHtml(href)}" title="${escapeHtml(
    clockSentence(t)
  )}">
    <span>
      <span class="what">${escapeHtml(t.title || "")}</span>
      <span class="meta">${meta}</span>
    </span>
    <span class="who">${person(t.assigned_to, "unassigned")}</span>
    <span class="found">${IS_STAFF && suggestionsLive ? deskFound(t) : ""}</span>
    <span class="state-cell">${rowState(desk, u)}</span>
    <span class="go" aria-hidden="true">${iconMarkup("icon-chevron")}</span>
  </a>`;
}

/** The one state a row is in. For staff with suggestions running that is what
 *  the agent's work is waiting on, which is what the queue is worked from;
 *  otherwise it is the clock, which is what the queue is ordered by. */
function rowState(found, u) {
  if (found) {
    if (found.blind_pending) return stateWord("neutral", "Quick check");
    if (!found.reviewed) return stateWord("accent", "Needs review");
    return stateWord("good", "Reviewed");
  }
  return stateWord(DOT_FOR[u.tone] || "neutral", u.label);
}

/** A clock's tone as one of the four dots. `states.js` names the tone; this is
 *  which dot it is drawn with. */
const DOT_FOR = { bad: "bad", warn: "warn", good: "good", neutral: "neutral", accent: "accent" };

function resolvedRow(t) {
  const href = `/tickets/${encodeURIComponent(t.ticket_number || "")}`;
  return `<tr class="clickable" data-href="${escapeHtml(href)}">
    <td class="nowrap"><a class="mono" href="${escapeHtml(href)}">${escapeHtml(t.ticket_number || "")}</a></td>
    <td><div class="measure"><strong>${escapeHtml(t.title || "")}</strong></div></td>
    <td>${escapeHtml(t.site || "")}</td>
    <td>${person(t.assigned_to)}</td>
    <td class="nowrap">${when(t.resolved_at)}</td>
  </tr>`;
}

/* A row is an `<a>`, so a middle click, a new tab and the keyboard all behave
   without a line of script. What used to wire a click handler onto a `<tr>` is
   the resolved list's job alone now. */
function wireRows() {
  for (const row of ticketList.querySelectorAll("tr[data-href]")) {
    row.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      window.location.href = row.dataset.href;
    });
  }
}

function drawResolved() {
  const resolved = (stats && stats.resolved_this_week) || { tickets: [], count: 0 };
  queueTabs.hidden = true;
  if (!resolved.tickets.length) {
    ticketList.innerHTML = nothingHere("No ticket has been stamped completed since Monday.");
  } else {
    ticketList.innerHTML = `<div class="scroller"><table>${RESOLVED_HEAD}<tbody>${resolved.tickets
      .map(resolvedRow)
      .join("")}</tbody></table></div>`;
    wireRows();
  }
  listSub.hidden = false;
  listSub.textContent = `${plural(resolved.count, "ticket")} completed since Monday. These are closed, so they are not in the open queue; press Open above to go back to it.${
    resolved.truncated ? ` ${resolved.truncation_note}` : ""
  }`;
}

/** What the search box looks at: the words on the row. The title, the number,
 *  the customer and whoever has it — the four things somebody types when they
 *  are looking for a ticket they have already seen. It narrows what is already
 *  loaded and asks nothing of any vendor. */
function matches(t, wanted) {
  return [t.title, t.ticket_number, t.site, t.assigned_to]
    .filter(Boolean)
    .some((part) => String(part).toLowerCase().includes(wanted));
}

function drawList() {
  const state = urlState();
  if (stats) renderStats(state);
  if (state.view === "resolved_week") {
    drawResolved();
    return;
  }
  queueTabs.hidden = false;
  renderTabs(state);
  renderFilters(state);
  const chosen = TICKET_FILTERS.filter((f) => state.chips.has(f.key) && f.available());
  const wanted = searchBox.value.trim().toLowerCase();
  const shown = tickets.filter(
    (t) => chosen.every((f) => f.test(t)) && (!wanted || matches(t, wanted))
  );
  /* Needs review is the one filter that is not about urgency: a suggestion is
     worth most while the ticket is new, so those come newest first. Every
     other view keeps the queue's own order, worst clock first. */
  shown.sort(
    state.chips.has("needs_review")
      ? (a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0)
      : byUrgency
  );
  if (!shown.length) {
    ticketList.innerHTML = `<p class="muted">${
      tickets.length ? "No open ticket matches what you are asking for." : "No open tickets."
    }</p>`;
  } else {
    ticketList.innerHTML = shown.map((t) => ticketRow(t, state)).join("");
  }
  const parts = [];
  if (shown.length !== tickets.length) {
    parts.push(`${shown.length} of ${plural(tickets.length, "open ticket")} shown.`);
  }
  /* The "created today" card counts tickets raised today whether they are still
     open or not; this queue holds only the open ones. Saying so is better than
     letting a card of six open a list of four. */
  if (state.chips.has("created_today") && stats) {
    const raised = stats.cards.find((c) => c.key === "created_today");
    if (raised && raised.value !== shown.length) {
      parts.push(
        `${raised.value} were raised today; ${shown.length} of them ${shown.length === 1 ? "is" : "are"} still open.`,
      );
    }
  }
  listSub.textContent = parts.join(" ");
  listSub.hidden = !parts.length;
}

/* --- loading -------------------------------------------------------------------------- */

function renderSummary(data) {
  const counts = {};
  for (const t of data.tickets) {
    const key = urgency(t).key;
    counts[key] = (counts[key] || 0) + 1;
  }
  /* The figures somebody scans for, then when Autotask was last asked — one
     line of facts, as spans, so the stylesheet draws the separators and a
     missing one leaves no stray dot. **Not the client**: the header says whose
     queue this is on every page, and a page that repeats its own frame is a
     page with a line of nothing at the top. */
  const parts = [
    `${data.tickets.length} open`,
    counts.breached ? `${counts.breached} breached` : null,
    counts.four_hours ? `${counts.four_hours} due within four hours` : null,
    counts.today ? `${counts.today} due today` : null,
    `read from Autotask ${timeWords(data.checked_at)}`,
  ];
  summary.innerHTML = parts
    .filter(Boolean)
    .map((part) => `<span>${escapeHtml(String(part))}</span>`)
    .join("");
}

async function load() {
  if (needsClient()) {
    ticketList.innerHTML = chooseClientNotice();
    document.getElementById("stats-open").hidden = true;
    return;
  }
  errorBox.hidden = true;
  /* The statistics are a separate endpoint and a separate failure: the queue is
     still worth having when they cannot be worked out, and vice versa. */
  loadStats();
  try {
    const data = await api("GET", "/tickets/list");
    tickets = data.tickets;
    renderSummary(data);
    drawList();
    if (data.truncated) {
      listSub.textContent = "Autotask's listing stopped at its page cap: not every open ticket is here.";
    }
    await loadPending();
    await loadSuggestions();
  } catch (error) {
    ticketList.innerHTML = "";
    fail(error);
  }
}

/* --- what the agent made of the open tickets ------------------------------------------ */

/** The chips, from a staff-only endpoint keyed by the ids this queue is
 *  showing. Drawn after the queue rather than with it: the tickets are a vendor
 *  read and these are a database read, and a page that waited for both to show
 *  either would be slower for no reason. A failure here leaves the queue
 *  exactly as it was. */
async function loadSuggestions() {
  if (!IS_STAFF) return;
  const ids = tickets.map((t) => t.ticket_id).filter(Boolean);
  if (!ids.length) return;
  try {
    const found = await api(
      "GET",
      `/autopilot/suggestions/queue?tickets=${encodeURIComponent(ids.join(","))}`
    );
    suggestionsLive = Boolean(found.live);
    suggestions = found.suggestions || {};
    suggestionWarnings = found.warnings || 0;
    drawList();
  } catch (error) {
    /* The queue stands on its own. Nothing is drawn about suggestions and
       nothing is said about them: this is our own measurement, and a reader
       working a ticket does not need a second error about it. */
    suggestionsLive = false;
    suggestions = {};
    suggestionWarnings = 0;
  }
}

/* --- tickets that closed while we had a plan on them ---------------------------------- */

async function loadPending() {
  const rows = await api("GET", "/tickets/pending");
  if (!rows.length) {
    pendingBox.hidden = true;
    return;
  }
  pendingBox.innerHTML = `<strong>${plural(rows.length, "ticket")} with your plan ${rows.length === 1 ? "has" : "have"} closed.</strong> Which steps resolved ${
    rows.length === 1 ? "it" : "them"
  }? The answer ranks the next plan.
    ${rows
      .map(
        (r) => `<form class="stack-tight" data-outcome="${escapeHtml(r.workspace_id)}" data-version="${r.version}" style="margin-top: var(--space-3)">
        <div><span class="mono">${escapeHtml(r.ticket_number || "")}</span> <strong>${escapeHtml(r.title || "")}</strong> · closed ${escapeHtml(timeWords(r.closed_at))} · plan v${r.version}</div>
        ${r.steps.map((s) => `<label class="inline"><input type="checkbox" name="step" value="${escapeHtml(s.id)}"> ${escapeHtml(s.text)}</label>`).join("")}
        <label class="inline"><input type="checkbox" name="none"> None of these resolved it</label>
        <label class="field"><span>Note</span><input type="text" name="note" placeholder="what actually fixed it, if not a step"></label>
        <div><button type="submit" class="small">Record what resolved it</button></div>
      </form>`,
      )
      .join("")}`;
  pendingBox.hidden = false;
  for (const form of pendingBox.querySelectorAll("form[data-outcome]")) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const steps = [...form.querySelectorAll('input[name="step"]:checked')].map((i) => i.value);
      const none = form.querySelector('input[name="none"]').checked;
      const note = form.querySelector('input[name="note"]').value;
      try {
        await api("POST", `/tickets/workspaces/${form.dataset.outcome}/plans/${form.dataset.version}/outcome`, {
          resolved_steps: steps,
          none,
          note: note || null,
        });
        await loadPending();
      } catch (error) {
        fail(error);
      }
    });
  }
}

document.getElementById("poll").addEventListener("click", async () => {
  errorBox.hidden = true;
  try {
    const result = await api("POST", "/tickets/poll");
    await loadPending();
    if (!result.closed.length) {
      await tell(
        "Nothing has closed",
        "No ticket with a plan has closed since the last check. Autotask was asked about every open workspace.",
      );
    }
  } catch (error) {
    fail(error);
  }
});

window.addEventListener("popstate", () => drawList());

load();

/* --- the controls above the queue ---------------------------------------------------- */

/* The filters, behind one button: opened, closed on Escape and on a click
   outside it, and left open while somebody turns two of them on. */
function closeFilters() {
  filtersList.hidden = true;
  filtersButton.setAttribute("aria-expanded", "false");
}

filtersButton.addEventListener("click", (event) => {
  event.stopPropagation();
  const open = filtersList.hidden;
  filtersList.hidden = !open;
  filtersButton.setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", (event) => {
  if (!filtersList.hidden && !filtersList.contains(event.target) && event.target !== filtersButton) {
    closeFilters();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeFilters();
});

/* The search narrows what is already on the page as it is typed. It is not in
   the address bar: a half-typed word is not a view somebody wants to share or
   to come back to. */
searchBox.addEventListener("input", () => drawList());

/* "Where the work is": the figures, the charts and their definitions, over the
   page rather than above the queue somebody came for. A real dialog, like the
   ticket page's readings. */
const statsPanel = document.getElementById("stats-panel");
const statsScrim = document.getElementById("stats-scrim");
const statsCloseButton = document.getElementById("stats-close");
let openedStatsFrom = null;

function showStats(open) {
  statsPanel.hidden = !open;
  statsScrim.hidden = !open;
  if (open) {
    openedStatsFrom = document.activeElement;
    statsCloseButton.focus();
  } else if (openedStatsFrom) {
    openedStatsFrom.focus();
    openedStatsFrom = null;
  }
}

/* And the one action that is not a filter, in its own menu beside them. */
const moreButton = document.getElementById("more-button");
const moreList = document.getElementById("more-list");

moreButton.addEventListener("click", (event) => {
  event.stopPropagation();
  const open = moreList.hidden;
  moreList.hidden = !open;
  moreButton.setAttribute("aria-expanded", String(open));
});
moreList.addEventListener("click", () => {
  moreList.hidden = true;
  moreButton.setAttribute("aria-expanded", "false");
});
document.addEventListener("click", (event) => {
  if (!moreList.hidden && !moreList.contains(event.target) && event.target !== moreButton) {
    moreList.hidden = true;
    moreButton.setAttribute("aria-expanded", "false");
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    moreList.hidden = true;
    moreButton.setAttribute("aria-expanded", "false");
  }
});

document.getElementById("stats-open").addEventListener("click", () => showStats(true));
statsCloseButton.addEventListener("click", () => showStats(false));
statsScrim.addEventListener("click", () => showStats(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !statsPanel.hidden) showStats(false);
});

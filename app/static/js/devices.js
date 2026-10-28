/* Devices: every machine across every tool, in the words a person would use.
 *
 * The list is what the daily resolution wrote (`GET /devices/list`): one row
 * per machine, which tools hold a record of it, when each last returned one.
 * This page adds no data to that and asks no vendor anything extra; what it
 * adds is a reading of it.
 *
 *   - One overall mark per machine, worked out here from the machine's RMM
 *     status and which tools hold it (`deviceHealth` in states.js). Nothing
 *     stores a verdict, so the rule is printed under the table rather than
 *     left implicit.
 *   - Every internal state name is replaced by a label and a one-line reason
 *     from states.js, which is the same vocabulary Coverage and Tickets use.
 *   - Colour is never the only signal: every mark carries a glyph and a word.
 *
 * The customer filter is a query parameter, so the CSV can still download
 * exactly that; the chips and the search narrow the screen only, and the note
 * under them says which of the two the CSV holds.
 *
 * A row opens the machine (`GET /devices/<id>`): the verdict and its reasons,
 * then protection, vulnerabilities, patching and tickets, with vendor ids and
 * pairing methods closed away in Technical details. Inputiv staff also see the
 * records nobody can place and the day's agreement; a link or an unlink needs
 * a reason, is recorded, and changes coverage numbers, so those cards are not
 * drawn for anyone else and the endpoints refuse them anyway.
 *
 * Nothing is truncated. Names, reasons and findings print whole; tables scroll.
 */

const summary = document.getElementById("summary");
const errorBox = document.getElementById("error");
const agreementBox = document.getElementById("agreement");
const strip = document.getElementById("strip");
const listSub = document.getElementById("list-sub");
const chipRow = document.getElementById("chips");
const chipDefinitions = document.getElementById("chip-definitions-body");
const legend = document.getElementById("legend");
const searchBox = document.getElementById("search");
const devicesTable = document.getElementById("devices");
const exportLink = document.getElementById("export");
const filterSite = document.getElementById("filter-site");
const detailPanel = document.getElementById("detail-panel");
const panelScrim = document.getElementById("panel-scrim");
const prevButton = document.getElementById("detail-prev");
const nextButton = document.getElementById("detail-next");
const detailTitle = document.getElementById("detail-title");
const detailSub = document.getElementById("detail-sub");
const detailVerdict = document.getElementById("detail-verdict");
const detailSections = document.getElementById("detail-sections");
const technicalBody = document.getElementById("technical-body");
const queueRows = document.getElementById("queue-rows");
const queueSub = document.getElementById("queue-sub");

let mayLink = false;
let vendors = [];
/** Per site slug, the tools that customer has mapped. See `toolMark`. */
let toolsBySite = {};
let loaded = [];
/* The machine ids in the order the list draws them, which is grouped by
   customer and so is not the order `loaded` is in. ↑ and ↓ in the panel walk
   this, because what a reader means by "the next one" is the next one on the
   screen behind the panel. */
let shownOrder = [];
/* Which machine the panel holds, and which row to put the keyboard back on. */
let panelDevice = null;
let openerDevice = null;

/* The three chips that name a tool, and the query parameter each is the
   same question as. Used only to decide whether the CSV can be the rows on
   screen rather than the whole customer. */
const CHIP_AS_QUERY = {
  "no-protection": "bitdefender",
  "not-scanned": "connectsecure",
  "not-in-rmm": "ninjaone",
};

function fail(error, what) {
  errorBox.innerHTML = problem(what || "Something on the device list did not work.", error);
  errorBox.hidden = false;
}

function value(v) {
  if (v === null || v === undefined || v === "") return '<span class="muted">not reported</span>';
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.length ? escapeHtml(v.join(", ")) : '<span class="muted">none</span>';
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) return when(v);
  return escapeHtml(String(v));
}

/** "Windows server", or "Windows server · Windows 11 Pro" — but never
 *  "Windows server · Windows", which is what printing both fields gives when
 *  the operating system adds nothing the kind did not already say. */
function describeMachine(row) {
  const kind = machineKind(row.node_class);
  const os = (row.os || "").trim();
  if (!os) return kind;
  if (!kind) return os;
  return kind.toLowerCase().includes(os.toLowerCase()) ? kind : `${kind} · ${os}`;
}

/** A list of Bitdefender module keys, in words. The keys are kept as the
 *  tooltip: they are what an engineer would search the vendor's console for. */
function modules(list) {
  if (!list || !list.length) return '<span class="muted">none</span>';
  return `<span title="${escapeHtml(list.join(", "))}">${escapeHtml(list.map(fromCamel).join(", "))}</span>`;
}

function matchWords(list) {
  const names = (list || []).map((p) => stateLabel("match", p));
  return names.length ? names.join(" and ") : "nothing recorded";
}

/* --- what the page is currently showing ------------------------------------------
 *
 * All of it is in the address bar, so a link carries what the reader is looking
 * at: the customer, the chips, the search and the open machine. The filters are
 * written with `replaceState`, because narrowing a list is not a place anybody
 * wants to press Back to; opening a machine is written with `pushState`, so the
 * back button closes the panel, which is what Back means when something is open
 * over something else.
 */

function urlState() {
  const params = new URLSearchParams(window.location.search);
  return {
    site: params.get("site") || "",
    chips: new Set((params.get("chips") || "").split(",").filter(Boolean)),
    search: params.get("q") || "",
    device: params.get("device") || "",
  };
}

function urlFor(next) {
  const params = new URLSearchParams();
  if (next.site) params.set("site", next.site);
  const chips = [...next.chips].join(",");
  if (chips) params.set("chips", chips);
  if (next.search) params.set("q", next.search);
  if (next.device) params.set("device", next.device);
  const query = params.toString();
  return query ? `${window.location.pathname}?${query}` : window.location.pathname;
}

function writeUrl(next, { push = false } = {}) {
  const url = urlFor(next);
  if (push) history.pushState({ panel: true }, "", url);
  else history.replaceState(history.state, "", url);
}

function matchesSearch(row, needle) {
  if (!needle) return true;
  const hay = [row.device, row.site, row.os, row.node_class, row.note]
    .concat(Object.values(row.vendors).flatMap((v) => v.names || []))
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return hay.includes(needle.toLowerCase());
}

function narrow(rows, state) {
  const tests = DEVICE_FILTERS.filter((f) => state.chips.has(f.key)).map((f) => f.test);
  return rows.filter((row) => tests.every((test) => test(row)) && matchesSearch(row, state.search));
}

/* --- the summary strip ------------------------------------------------------------ */

function renderStrip(rows) {
  const counts = { healthy: 0, attention: 0, action: 0, retired: 0 };
  for (const row of rows) counts[deviceHealth(row).key] += 1;
  const tiles = [
    {
      tone: "",
      figure: rows.length,
      name: "Machines",
      caption: counts.retired ? `${counts.retired} retired` : "every machine we know of",
      note: "Every machine the last daily resolution knew of, at every customer.",
    },
    ...["healthy", "attention", "action"].map((key) => ({
      tone: { healthy: "good", attention: "warn", action: "bad" }[key],
      figure: counts[key],
      name: stateLabel("health", key),
      /* The caption is a line and names what the figure is a share of; the
         whole rule is its tooltip. A four-column wall of sentences above the
         list people came for is a wall nobody reads, and the rule is on the
         page under "How this page decides". */
      caption: `of ${rows.length} machines`,
      note: stateReason("health", key),
    })),
  ];
  /* The number and what it counts. The sentence under each used to be printed
     in full, which made a four-column wall of text above the list people came
     for; it is the figure's title now, where the results page keeps its own
     definitions. */
  strip.innerHTML = tiles
    .map(
      (t) => `<section class="kpi">
        <div class="meta muted">${escapeHtml(t.name)}</div>
        <div class="value ${escapeHtml(t.tone)}">${t.figure}</div>
        <small class="muted" title="${escapeHtml(t.note)}">${escapeHtml(t.caption)}</small>
      </section>`,
    )
    .join("");
}

/* --- the chips -------------------------------------------------------------------- */

/** What each chip counted, in full.
 *
 * The Coverage board answers the same questions over its own population and as
 * of its own snapshot, so the two pages can differ even when neither is wrong.
 * Rather than leave a reader to discover that by finding two numbers, every
 * chip states the population it counted and this block says where the rest of
 * the difference comes from. */
function renderChipDefinitions() {
  if (!chipDefinitions) return;
  chipDefinitions.innerHTML =
    `<dl class="facts">${DEVICE_FILTERS.map(
      (f) => `<dt>${escapeHtml(f.label)}</dt><dd>${escapeHtml(f.counts || f.reason)}</dd>`,
    ).join("")}</dl>
    <p class="small" style="margin-top: var(--space-3)">
      <strong>Against the Coverage board.</strong> No protection and Not scanned count the same
      machines the board calls <em>Not installed</em> for that tool: computers the RMM is still
      reporting on, with network gear and machines the RMM has lost left out of both. Two things
      can still make the numbers differ, and neither is either page being wrong.
    </p>
    <ul class="small">
      <li><strong>When each was worked out.</strong> This list is as of the last daily resolution,
      shown at the top of the page; the board is as of its last snapshot. A machine a tool dropped
      between the two is present here and absent there.</li>
      <li><strong>What each holds.</strong> This page knows every machine any tool has ever
      returned; the board's population is the computers NinjaOne returned on the run it was taken.
      A machine only another tool holds is on the Not in RMM chip here and an unmanaged endpoint
      there.</li>
    </ul>
    <p class="small">The two are compared in full every day, and Inputiv staff see the result at
    the top of this page.</p>`;
}

function renderChips(rows, state) {
  renderChipDefinitions();
  chipRow.innerHTML =
    DEVICE_FILTERS.map((f) => {
      const count = rows.filter(f.test).length;
      const pressed = state.chips.has(f.key);
      const title = [f.reason, f.counts].filter(Boolean).join(" ");
      return `<button type="button" class="filter-chip" data-chip="${escapeHtml(f.key)}" aria-pressed="${pressed}" title="${escapeHtml(title)}">${escapeHtml(f.label)} <span class="count">${count}</span></button>`;
    }).join("") +
    (state.chips.size || state.search
      ? '<button type="button" class="action" id="chips-clear">Clear</button>'
      : "");
  for (const button of chipRow.querySelectorAll("button[data-chip]")) {
    button.addEventListener("click", () => {
      const next = urlState();
      const key = button.dataset.chip;
      if (next.chips.has(key)) next.chips.delete(key);
      else next.chips.add(key);
      writeUrl(next);
      draw();
    });
  }
  const clear = document.getElementById("chips-clear");
  if (clear) {
    clear.addEventListener("click", () => {
      const next = urlState();
      next.chips = new Set();
      next.search = "";
      searchBox.value = "";
      writeUrl(next);
      draw();
    });
  }
}

/* --- the list --------------------------------------------------------------------- */

function toolMark(vendor, row) {
  const presence = row.vendors[vendor.key];
  const role = VENDOR_ROLE[vendor.key] || "";
  /* A tool this customer does not have is not a gap. The coverage board says
     `not assessed` where a site has no organisation on a tool, and this page
     has to say the same thing or the two disagree about the same fleet: on a
     customer with no Cove, every machine would otherwise read Missing under
     Backup, which asserts a gap in a product they do not buy. */
  const has = toolsBySite[row.site_slug];
  if (has && !has.includes(vendor.key)) {
    const sentence = `${vendor.label} (${role}) is not set up for this customer, so there is nothing to find. The Coverage board reads this the same way.`;
    return `<span title="${escapeHtml(sentence)}">${coverageState("unknown", "Not assessed", true)}</span>`;
  }
  /* An agent tool that does not belong on this kind of machine is a dash, not
     a red cross: a switch with no protection agent is not a gap, on this page
     or on the coverage board. NinjaOne is the exception — it monitors network
     gear too, so its absence is always worth saying. */
  const expected = vendor.key === "ninjaone" || isAgentClass(row.node_class);
  /* The same glyph-and-word the Coverage board draws, from the same function:
     the two pages answer one question about one machine, and two drawings of
     one answer read as two products. A tick carries its word for a screen
     reader and not on the screen — the column is a yes or a no, and the
     header above it says which tool. */
  let state;
  let label;
  let sentence;
  if (presence.present) {
    state = "ok";
    label = "Present";
    sentence = `${vendor.label} (${role}) holds ${presence.links > 1 ? `${presence.links} records of` : "a record of"} this machine, paired by ${matchWords(presence.matched_by)}, last returned ${timeWords(presence.last_seen)}.`;
  } else if (expected) {
    state = "missing";
    label = "Missing";
    sentence = `${vendor.label} (${role}) holds no record of this machine.`;
  } else {
    state = "unknown";
    label = "Not expected";
    sentence = `${vendor.label} (${role}) holds no record of this machine, and none is expected: it is ${machineKind(row.node_class).toLowerCase() || "not a computer"} rather than a computer.`;
  }
  /* Compact for the two states that are not a finding — a tick and a dash,
     with the word kept for a screen reader and in the tooltip. Spelling both
     out put `Not expected` in four columns on every row and pushed the table
     past the page; `Missing` keeps its word, because it is the one thing in
     the column somebody has to act on. */
  return `<span title="${escapeHtml(sentence)}">${coverageState(state, label, state !== "missing")}</span>`;
}

function deviceRow(row) {
  const health = deviceHealth(row);
  const detail = describeMachine(row);
  /* The reason is carried, not printed. It was printed on every machine that
     was not healthy, which is three lines of the same sentence down two
     thirds of the page and a table whose rows are 100px tall. It is the
     chip's tooltip and is read out with the chip, it is the first thing in
     the panel the row opens, and the rule behind it is under "How this page
     decides" — three places, none of them a wall. */
  const why = health.reasons.join(" ");
  /* A row opens a dialog, so it is reachable and operable from the keyboard as
     a control would be. `aria-haspopup` says what pressing it does. */
  return `<tr class="clickable" tabindex="0" aria-haspopup="dialog" data-device="${escapeHtml(row.device_id)}">
    <td>
      <strong>${escapeHtml(row.device)}</strong>
      ${detail ? `<div class="small muted">${escapeHtml(detail)}</div>` : ""}
      ${row.note ? `<div class="small" style="margin-top: var(--space-1)">${toneChip("warn", row.note)}</div>` : ""}
    </td>
    <td class="nowrap" title="${escapeHtml(why)}">${stateChip("health", health.key)}<span class="sr-only">${escapeHtml(why)}</span></td>
    ${vendors.map((v) => `<td class="tool-cell">${toolMark(v, row)}</td>`).join("")}
    <td class="nowrap">${when(row.last_seen)}</td>
    <td class="chevron" aria-hidden="true">›</td>
  </tr>`;
}

function siteGroup(site, rows) {
  const counts = { healthy: 0, attention: 0, action: 0, retired: 0 };
  for (const row of rows) counts[deviceHealth(row).key] += 1;
  const parts = [plural(rows.length, "machine")];
  if (counts.action) parts.push(agrees(counts.action, "needs action", "need action"));
  if (counts.attention) parts.push(agrees(counts.attention, "needs attention", "need attention"));
  if (counts.retired) parts.push(`${counts.retired} retired`);
  return `<tbody class="group">
    <tr class="group-head"><th colspan="${vendors.length + 4}">${escapeHtml(site)}<span class="counts">${escapeHtml(parts.join(" · "))}</span></th></tr>
    ${rows.map(deviceRow).join("")}
  </tbody>`;
}

/** The head, with one column per tool named after the tool.
 *
 *  It used to be one "Tools" column holding four marks, each prefixed with the
 *  vendor's initial - which is a key a reader has to learn, and two of ours
 *  begin with C. The name in the header says it once per table instead of once
 *  per cell, and the role under it says what that tool is for. Built from the
 *  vendor list the API sent, so a tool added there appears here. */
function devicesHead() {
  /* The mark and the name on one line, as the Coverage board's columns are.
     The role under it was a second line of words in a 90px column, which the
     browser broke mid-word — `NINJAONE MONITORIN G AND PATCHING` over four
     lines, four times across, above every page of this table. It is the
     column's tooltip now, and is read out beside the name. */
  const tools = vendors
    .map((v) => {
      const role = VENDOR_ROLE[v.key] || "";
      return (
        `<th class="tool-col" scope="col" title="${escapeHtml(`${v.label}: ${role}`)}">` +
        `<span class="tool-column">${vendorMark(v.key)}${escapeHtml(v.label)}` +
        `<span class="sr-only"> — ${escapeHtml(role)}</span></span></th>`
      );
    })
    .join("");
  return `<thead><tr>
    <th scope="col">Machine</th>
    <th scope="col">Health</th>
    ${tools}
    <th class="nowrap" scope="col">Last seen by any tool</th>
    <th scope="col"><span class="sr-only">Open</span></th>
  </tr></thead>`;
}

/** The whole table, head and all: the rows are grouped into one tbody per
 *  customer, so the group header scrolls sideways with the columns it labels. */
function renderList(rows, total) {
  legend.innerHTML =
    "<strong>✓</strong> the tool holds a record of the machine · " +
    "<strong>Missing</strong> it holds none · " +
    "<strong>n/a</strong> none is expected, because the machine is not a computer · " +
    "<strong>not assessed</strong> the customer has no organisation on that tool. " +
    "Hover a mark for the whole sentence.";
  if (!rows.length) {
    devicesTable.innerHTML = `${devicesHead()}<tbody><tr><td colspan="${vendors.length + 4}" class="muted">${
      total
        ? "No machine matches what you are asking for. Clear a chip or the search."
        : "No resolution has run for this customer yet: the daily collector writes the machines (scripts/devices.py resolve)."
    }</td></tr></tbody>`;
    return;
  }
  const bySite = new Map();
  for (const row of rows) {
    if (!bySite.has(row.site)) bySite.set(row.site, []);
    bySite.get(row.site).push(row);
  }
  devicesTable.innerHTML =
    devicesHead() + [...bySite.entries()].map(([site, group]) => siteGroup(site, group)).join("");
  shownOrder = [...devicesTable.querySelectorAll("tr[data-device]")].map((r) => r.dataset.device);
  for (const row of devicesTable.querySelectorAll("tr[data-device]")) {
    row.addEventListener("click", () => openDevice(row.dataset.device, "push"));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openDevice(row.dataset.device, "push");
      }
    });
  }
  markOpenRow();
}

/** The row the panel is showing, marked on the list behind it. */
function markOpenRow() {
  for (const row of devicesTable.querySelectorAll("tr[data-device]")) {
    if (row.dataset.device === panelDevice) row.setAttribute("aria-current", "true");
    else row.removeAttribute("aria-current");
  }
}

/* --- what the CSV will hold -------------------------------------------------------- */

function exportPlan(state) {
  const params = new URLSearchParams();
  if (state.site) params.set("site", state.site);
  const chips = [...state.chips];
  let sameAsScreen = !state.search;
  if (chips.length === 1 && CHIP_AS_QUERY[chips[0]]) params.set("missing", CHIP_AS_QUERY[chips[0]]);
  else if (chips.length) sameAsScreen = false;
  const query = params.toString();
  return {
    href: `/devices/export.csv${query ? `?${query}` : ""}`,
    note: sameAsScreen
      ? "The CSV holds exactly the rows on screen."
      : "The CSV holds every machine at this customer: the chips and the search narrow the screen only.",
  };
}

/* --- drawing what is currently asked for -------------------------------------------- */

/* What the last draw was for. Back and Forward only redraw the table when the
   filters actually differ: closing the panel changes only which machine is in
   the address bar, and rebuilding the rows there would destroy the very row
   the keyboard is being handed back to. */
let drawnFor = null;

function filterKey(state) {
  return [state.site, [...state.chips].sort().join(","), state.search].join("|");
}

function draw() {
  const state = urlState();
  drawnFor = filterKey(state);
  filterSite.value = state.site;
  if (searchBox.value !== state.search) searchBox.value = state.search;
  const shown = narrow(loaded, state);
  renderStrip(loaded);
  renderChips(loaded, state);
  renderList(shown, loaded.length);
  /* The list was redrawn under the panel, so where ↑ and ↓ can go may have
     changed with it. */
  if (!detailPanel.hidden) renderMoveButtons();
  const plan = exportPlan(state);
  exportLink.href = plan.href;
  /* Only when it is news. Unfiltered, the count is already the first thing in
     the subtitle at the top of the page, and printing it again above the table
     is a line that says nothing twice. */
  listSub.textContent =
    shown.length === loaded.length
      ? ""
      : `${shown.length} of ${plural(loaded.length, "machine")} shown.`;
  /* What the CSV holds is a property of the button, so it is on the button. */
  exportLink.title = plan.note;
}

function renderSites(data, state) {
  filterSite.innerHTML =
    '<option value="">Every customer</option>' +
    data.sites
      .map((s) => `<option value="${escapeHtml(s.slug)}">${escapeHtml(s.name)}${s.active ? "" : " (no longer active)"}</option>`)
      .join("");
  filterSite.value = state.site;
}

async function load() {
  if (needsClient()) {
    devicesTable.innerHTML = `<tbody><tr><td>${chooseClientNotice()}</td></tr></tbody>`;
    if (queueRows) queueRows.innerHTML = "";
    return;
  }
  errorBox.hidden = true;
  const state = urlState();
  try {
    const query = state.site ? `?site=${encodeURIComponent(state.site)}` : "";
    const data = await api("GET", `/devices/list${query}`);
    vendors = data.vendors;
    toolsBySite = data.tools_by_site || {};
    mayLink = data.may_link;
    loaded = data.devices;
    renderSites(data, state);
    summary.textContent = `${plural(data.counts.total, "machine")} across every customer, as the tools stood ${
      data.resolved_at ? timeWords(data.resolved_at) : "— no resolution has run yet"
    }.`;
    draw();
    if (agreementBox) await loadAgreement();
    if (queueRows) await loadQueue();
    if (state.device) await openDevice(state.device, "none");
  } catch (error) {
    devicesTable.innerHTML = "";
    summary.textContent = "";
    fail(error);
  }
}

filterSite.addEventListener("change", () => {
  const next = urlState();
  next.site = filterSite.value;
  writeUrl(next);
  load();
});

let searchTimer = null;
searchBox.addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    const next = urlState();
    next.search = searchBox.value.trim();
    writeUrl(next);
    draw();
  }, 150);
});

/* --- one machine, in the panel --------------------------------------------------------
 *
 * The panel is a modal dialog over the list: the list keeps its filters and its
 * scroll position, and gets them back untouched when the panel closes. Three
 * things make it a dialog rather than a box that happens to be on top —
 * focus is trapped in it, Esc and the scrim close it, and the keyboard goes
 * back to the row it was opened from.
 *
 * `how` says what to do to the address bar: "push" for a row somebody clicked
 * (so Back closes the panel), "replace" for stepping between machines with ↑
 * and ↓ (so twenty steps are not twenty things to press Back through), and
 * "none" when the URL already says this, which is a fresh load of a link or
 * the browser going back to one.
 */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

function panelFocusable() {
  return [...detailPanel.querySelectorAll(FOCUSABLE)].filter(
    (element) => !element.hidden && element.offsetParent !== null,
  );
}

async function openDevice(deviceId, how = "push") {
  errorBox.hidden = true;
  const first = detailPanel.hidden;
  if (first) openerDevice = panelDevice || deviceId;
  panelDevice = deviceId;
  detailPanel.hidden = false;
  panelScrim.hidden = false;
  detailTitle.textContent = "Loading…";
  detailSub.textContent = "";
  detailVerdict.innerHTML = "";
  technicalBody.innerHTML = "";
  detailSections.innerHTML = '<div class="muted small">Asking each tool what it knows about this machine…</div>';
  detailPanel.querySelector(".panel-body").scrollTop = 0;
  markOpenRow();
  renderMoveButtons();
  if (how !== "none") {
    const next = urlState();
    next.device = deviceId;
    writeUrl(next, { push: how === "push" });
  }
  if (first) detailPanel.focus();
  try {
    const data = await api("GET", `/devices/${encodeURIComponent(deviceId)}`);
    /* A slow answer for a machine the reader has already moved on from is not
       the answer to what is on screen; it is dropped rather than drawn over. */
    if (panelDevice !== deviceId) return;
    renderDetail(data);
  } catch (error) {
    if (panelDevice !== deviceId) return;
    detailTitle.textContent = "";
    detailSections.innerHTML = "";
    fail(error);
  }
}

function closePanel({ fromHistory = false } = {}) {
  if (detailPanel.hidden) return;
  detailPanel.hidden = true;
  panelScrim.hidden = true;
  const returnTo = panelDevice || openerDevice;
  panelDevice = null;
  openerDevice = null;
  markOpenRow();
  const row = returnTo ? devicesTable.querySelector(`tr[data-device="${CSS.escape(returnTo)}"]`) : null;
  if (row) row.focus();
  if (fromHistory) return;
  /* Back, when we put an entry there; otherwise just drop the machine from the
     address bar, which is the case for a link opened straight at one. */
  if (history.state && history.state.panel) history.back();
  else {
    const next = urlState();
    next.device = "";
    writeUrl(next);
  }
}

function neighbour(delta) {
  const index = shownOrder.indexOf(panelDevice);
  if (index < 0) return null;
  return shownOrder[index + delta] || null;
}

function renderMoveButtons() {
  prevButton.disabled = !neighbour(-1);
  nextButton.disabled = !neighbour(1);
}

function moveBy(delta) {
  const next = neighbour(delta);
  if (next) openDevice(next, "replace");
}

prevButton.addEventListener("click", () => moveBy(-1));
nextButton.addEventListener("click", () => moveBy(1));
panelScrim.addEventListener("click", () => closePanel());

document.addEventListener("keydown", (event) => {
  if (detailPanel.hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closePanel();
    return;
  }
  if (event.key === "Tab") {
    const items = panelFocusable();
    if (!items.length) return;
    const firstItem = items[0];
    const lastItem = items[items.length - 1];
    const inside = detailPanel.contains(document.activeElement);
    if (event.shiftKey && (!inside || document.activeElement === firstItem)) {
      event.preventDefault();
      lastItem.focus();
    } else if (!event.shiftKey && (!inside || document.activeElement === lastItem)) {
      event.preventDefault();
      firstItem.focus();
    }
    return;
  }
  if (event.key === "ArrowUp" || event.key === "ArrowDown") {
    /* Not while somebody is typing in something: a text box owns its arrows. */
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    event.preventDefault();
    moveBy(event.key === "ArrowDown" ? 1 : -1);
  }
});

function verdict(row) {
  const health = deviceHealth(row);
  const state = stateOf("health", health.key);
  return `<div class="verdict ${state.tone}">
    <span class="mark-large" aria-hidden="true">${state.mark}</span>
    <div class="body">
      <div class="headline">${escapeHtml(state.label)}</div>
      <ul>${health.reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>
    </div>
  </div>`;
}

function section(title, note, body) {
  return `<div class="section">
    <header><h3>${escapeHtml(title)}</h3>${note ? `<span class="sub">${escapeHtml(note)}</span>` : ""}</header>
    ${body}
  </div>`;
}

/** What a tool's block says when it was not asked, or could not answer. */
function vendorState(part, vendorKey, absent) {
  if (part.linked === false) return `<div class="notice small">${escapeHtml(absent)}</div>`;
  if (!part.available) {
    return `<div class="notice bad small"><strong>${escapeHtml(VENDOR_NAMES[vendorKey])} did not answer.</strong> ${escapeHtml(part.error || "no reason given")}. That is not the same as nothing being wrong.</div>`;
  }
  return null;
}

function dl(pairs) {
  return `<dl class="facts">${pairs.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

function protectionSection(data) {
  const part = data.vendors.bitdefender;
  const stopped = vendorState(
    part,
    "bitdefender",
    "Bitdefender holds no record of this machine, so it was not asked. As far as we can tell, nothing is protecting it.",
  );
  if (stopped) return section("Protection", "Bitdefender", `<div class="panel">${stopped}</div>`);
  const x = part.data;
  const agent = x.agent_installed
    ? `installed${x.agent_version ? `, version ${escapeHtml(x.agent_version)}` : ""}${x.agent_outdated ? " — and out of date" : ""}`
    : toneChip("bad", "no agent on the machine");
  const body = dl([
    ["Protection agent", agent],
    ["Virus definitions", `${value(x.signatures_updated)}${x.signatures_outdated ? " — out of date" : ""}`],
    ["Malware found in the last day", value(x.malware_detected_last_24h)],
    ["Still infected", value(x.still_infected)],
    ["Cut off from the network by Bitdefender", value(x.isolated)],
    ["Last successful scan", value(x.last_successful_scan)],
    ["Last seen by Bitdefender", value(x.last_seen)],
    ["Protection switched on", modules(x.modules_enabled)],
    ["Switched off by policy", modules(x.modules_disabled)],
    ["Not covered by the licence", modules(x.modules_not_licensed)],
    ["Advanced detection (EDR) licensed", value(x.edr_licensed)],
    ["Risk, as Bitdefender scores it", x.risk_score === null || x.risk_score === undefined ? value(null) : `${x.risk_score} out of 100 (higher is worse)${x.risk_impact ? `, ${escapeHtml(x.risk_impact)}` : ""}`],
    ["Policy", `${value(x.policy)}${x.policy_applied === false ? " — not applied to the machine" : ""}`],
    ["Licence", value(x.licence)],
    ["State Bitdefender reports", value(x.state)],
    ["Operating system as Bitdefender sees it", value(x.os)],
    ["Name in Bitdefender", value(x.name)],
    ["Other records that also matched", value(x.candidates)],
  ]);
  return section("Protection", "Bitdefender", `<div class="panel">${body}</div>`);
}

function vulnerabilitySection(data) {
  const part = data.vendors.connectsecure;
  const stopped = vendorState(
    part,
    "connectsecure",
    "ConnectSecure holds no record of this machine, so it was not asked. It has never been scanned for vulnerabilities as far as we can tell.",
  );
  if (stopped) return section("Vulnerabilities", "ConnectSecure", `<div class="panel">${stopped}</div>`);
  const x = part.data;
  const counts = x.counts_by_severity || {};
  const severities = ["critical", "high", "medium", "low"]
    .map((s) => `${counts[s] ?? 0} ${s}`)
    .join(", ");
  const rows = (x.vulnerabilities || [])
    .map(
      (v) => `<tr>
        <td>${escapeHtml(v.severity || "")}${v.known_exploited ? ` ${toneChip("bad", "exploited in the wild")}` : ""}</td>
        <td>${escapeHtml(v.vulnerability || "")}</td>
        <td>${escapeHtml(v.product || v.title || "")}</td>
        <td>${escapeHtml(v.fix || "fix not reported")}</td>
      </tr>`,
    )
    .join("");
  const body =
    dl([
      ["Open findings", `${value(x.total)}${x.total ? ` — ${escapeHtml(severities)}` : ""}`],
      ["Known to be exploited in the wild", value(x.known_exploited)],
      ["Findings with a fix reported", value(x.fix_reported_for)],
      ["Last scanned", `${value(x.last_scanned)}${x.days_since_scan !== null && x.days_since_scan !== undefined ? ` (${plural(x.days_since_scan, "day")} ago)` : ""}`],
      ["Risk, as ConnectSecure scores it", x.risk_score === null || x.risk_score === undefined ? value(null) : `${x.risk_score} out of 100 (higher is worse), grade ${escapeHtml(x.risk_grade || "?")}`],
      ["Which findings were counted", x.population === "confirmed" ? "confirmed only — findings the scanner proved are on the machine, not ones it inferred from a version number" : value(x.population)],
      ["Name in ConnectSecure", value(x.name)],
      ["Other records that also matched", value(x.candidates)],
    ]) +
    (rows
      ? `<div class="scroller" style="margin-top: var(--space-3)"><table><thead><tr><th>Severity</th><th>Finding</th><th>Software</th><th>Fix</th></tr></thead><tbody>${rows}</tbody></table></div>
         <div class="small muted">The worst ${x.vulnerabilities.length} of ${x.total}. Ask for the rest on Ask.</div>`
      : "");
  return section("Vulnerabilities", "ConnectSecure", `<div class="panel">${body}</div>`);
}

function patchSection(data) {
  const ninja = data.vendors.ninjaone;
  const protection = data.vendors.bitdefender;
  const scanner = data.vendors.connectsecure;
  const pairs = [];
  if (ninja.available) {
    const x = ninja.data;
    pairs.push(["Waiting for a restart", value(x.needs_reboot)]);
    pairs.push(["Last restarted", value(x.last_boot)]);
    pairs.push(["Operating system", value([x.os_name, x.os_build, x.os_release, x.architecture].filter(Boolean).join(" "))]);
    pairs.push([
      "Approval status in NinjaOne",
      x.approval_status ? escapeHtml(fromCamel(x.approval_status)) : value(null),
    ]);
  }
  if (protection.available) {
    const modules = protection.data;
    const on = (modules.modules_enabled || []).some((m) => /patch/i.test(m));
    const off = (modules.modules_disabled || []).some((m) => /patch/i.test(m));
    const unlicensed = (modules.modules_not_licensed || []).some((m) => /patch/i.test(m));
    pairs.push([
      "Bitdefender patch management",
      on ? "switched on" : off ? "switched off by policy" : unlicensed ? "not covered by the licence" : '<span class="muted">not reported</span>',
    ]);
  }
  if (scanner.available) {
    pairs.push(["Findings with a fix available", value(scanner.data.fix_reported_for)]);
  }
  const note =
    '<p class="small muted" style="margin-top: var(--space-3)">The list of individual patches waiting on this machine is not read here — it is a fleet-wide report rather than a per-machine one. Ask for it on Ask ("what is waiting to be patched on this machine?"), which calls the same tool.</p>';
  const body = pairs.length
    ? dl(pairs) + note
    : nothingHere("No tool that reports patching holds a record of this machine.");
  return section("Patching and updates", "NinjaOne, Bitdefender and ConnectSecure", `<div class="panel">${body}</div>`);
}

function ticketSection(data) {
  const part = data.vendors.autotask;
  if (!part.available) {
    return section(
      "Tickets",
      `Autotask, last ${plural(data.ticket_days, "day")}`,
      `<div class="panel"><div class="notice bad small"><strong>Autotask did not answer.</strong> ${escapeHtml(part.error || "no reason given")}.</div></div>`,
    );
  }
  const x = part.data;
  if (!x.tickets || !x.tickets.length) {
    return section(
      "Tickets",
      `Autotask, last ${plural(data.ticket_days, "day")}`,
      `<div class="panel"><div class="muted small">No ticket mentions this machine in that window. Searched: ${escapeHtml((x.searched || ["title", "description", "notes"]).join(", "))}.</div></div>`,
    );
  }
  const body = `<div class="scroller"><table>
    <thead><tr><th>Ticket</th><th>Title</th><th>Status</th><th>Raised</th><th>Mentioned in</th></tr></thead>
    <tbody>${x.tickets
      .map(
        (t) => `<tr>
          <td class="mono">${ticketLink(t)}</td>
          <td>${escapeHtml(t.title || "")}${t.snippet ? `<div class="small muted">${escapeHtml(t.snippet)}</div>` : ""}</td>
          <td>${escapeHtml(t.status || "")}</td>
          <td class="nowrap">${when(t.created_at)}</td>
          <td>${escapeHtml((t.matched_in || []).join(", "))}</td>
        </tr>`,
      )
      .join("")}</tbody></table></div>
    ${x.truncated ? `<div class="small muted">${escapeHtml(x.truncation_note || "More tickets than are shown here.")}</div>` : ""}`;
  return section("Tickets", `Autotask, last ${plural(data.ticket_days, "day")}`, `<div class="panel">${body}</div>`);
}

function ticketLink(ticket) {
  const url = consoleUrl("autotask", ticket.ticket_id);
  const number = escapeHtml(ticket.ticket_number || "");
  return url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${number}</a>` : number;
}

function machineSection(data) {
  const part = data.vendors.ninjaone;
  const stopped = vendorState(
    part,
    "ninjaone",
    "NinjaOne holds no record of this machine, so it was not asked. Nobody is monitoring or patching it through the RMM.",
  );
  if (stopped) return section("The machine itself", "NinjaOne", `<div class="panel">${stopped}</div>`);
  const x = part.data;
  if (x.found === false) {
    return section(
      "The machine itself",
      "NinjaOne",
      `<div class="panel"><div class="notice small">${escapeHtml(x.message || "NinjaOne did not find it under that name")}${
        x.candidates && x.candidates.length ? `<div class="small muted">Names that did match: ${escapeHtml(x.candidates.join(", "))}</div>` : ""
      }</div></div>`,
    );
  }
  const body = dl([
    ["Reporting to NinjaOne", x.offline === null || x.offline === undefined ? value(null) : x.offline ? "no, it is offline" : "yes"],
    ["Last heard from", value(x.last_contact)],
    ["Last person signed in", value(x.last_logged_in_user)],
    ["Disks", (x.volumes || []).length
      ? escapeHtml(x.volumes.map((v) => `${v.volume}: ${v.free_gb ?? "?"} GB free of ${v.capacity_gb ?? "?"} GB`).join("; "))
      : value(null)],
    ["Memory", x.memory_gb === null || x.memory_gb === undefined ? value(null) : `${x.memory_gb} GB`],
    ["Hardware", value([x.manufacturer, x.model].filter(Boolean).join(" "))],
    ["Serial number", value(x.serial_number)],
    ["A virtual machine", value(x.virtual_machine)],
    ["Kind of machine", x.node_class ? escapeHtml(machineKind(x.node_class)) : value(null)],
    ["Domain", value(x.domain)],
    ["Addresses", value(x.ip_addresses)],
    ["Address the internet sees", value(x.public_ip)],
    ["Network cards", value(x.mac_addresses)],
    ["Customer", value(x.site)],
    ["Name in NinjaOne", value(x.name)],
  ]);
  return section("The machine itself", "NinjaOne", `<div class="panel">${body}</div>`);
}

function consoleCell(connector, externalId) {
  const url = consoleUrl(connector, externalId);
  const id = `<span class="mono small">${escapeHtml(externalId)}</span>`;
  const copy = `<button class="action" type="button" data-copy="${escapeHtml(externalId)}">copy</button>`;
  const open = url
    ? ` <a href="${escapeHtml(url)}" target="_blank" rel="noopener">open in ${escapeHtml(VENDOR_NAMES[connector] || connector)}</a>`
    : "";
  return `${id} ${copy}${open}`;
}

function renderTechnical(data) {
  const d = data.device;
  const linkRows = data.links.length
    ? data.links
        .map(
          (l) => `<tr${l.active ? "" : ' class="muted"'}>
            <td>${escapeHtml(VENDOR_NAMES[l.connector] || l.connector)}</td>
            <td><strong>${escapeHtml(l.vendor_name || "(no name)")}</strong></td>
            <td>${consoleCell(l.connector, l.external_id)}</td>
            <td>${stateChip("match", l.matched_by)}${l.note ? `<div class="small muted">${escapeHtml(l.note)}</div>` : ""}</td>
            <td class="nowrap">${when(l.last_seen)}</td>
            <td>${l.active ? "in use" : "withdrawn"}</td>
            <td>${mayLink && l.active ? `<button class="action" type="button" data-unlink="${l.link_id}">this is not the same machine</button>` : ""}</td>
          </tr>`,
        )
        .join("")
    : nothingRow(7, "No vendor record is tied to this machine yet.", {
        text: "The daily resolution ties them;",
        command: "python scripts/devices.py resolve --client <slug>",
      });
  const queuedRows = data.queued
    .map(
      (q) => `<tr>
        <td>${escapeHtml(VENDOR_NAMES[q.connector] || q.connector)}</td>
        <td><strong>${escapeHtml(q.vendor_name || "(no name)")}</strong></td>
        <td>${consoleCell(q.connector, q.external_id)}</td>
        <td>${toneChip("warn", "waiting for a person")}<div class="small muted">The ${escapeHtml(stateLabel("match", q.found_by))} pass found more than one machine, so nothing was paired.</div></td>
        <td><span class="muted">—</span></td>
        <td>not placed</td>
        <td>${mayLink ? `<button class="action" type="button" data-link-connector="${escapeHtml(q.connector)}" data-link-external="${escapeHtml(q.external_id)}" data-link-name="${escapeHtml(q.vendor_name || "")}" data-link-device="${escapeHtml(d.device_id)}">it is this machine</button>` : ""}</td>
      </tr>`,
    )
    .join("");
  technicalBody.innerHTML = `
    <div class="scroller"><table>
      <thead><tr><th>Tool</th><th>Name there</th><th>Its id</th><th>Paired by</th><th>Last returned</th><th>State</th><th></th></tr></thead>
      <tbody>${linkRows}${queuedRows}</tbody>
    </table></div>
    <dl class="facts" style="margin-top: var(--space-4)">
      <dt>Our id for this machine</dt><dd><span class="mono small">${escapeHtml(d.device_id)}</span></dd>
      <dt>Short (NetBIOS) name</dt><dd>${value(d.netbios_key)}</dd>
      <dt>Serial numbers</dt><dd>${value(d.serials)}</dd>
      <dt>Network cards</dt><dd>${value(d.macs)}</dd>
      <dt>First seen</dt><dd>${when(d.first_seen)}</dd>
      <dt>Status recorded by the daily resolution</dt><dd>${stateChip("device", d.status)}</dd>
    </dl>`;
  for (const button of technicalBody.querySelectorAll("button[data-unlink]")) {
    button.addEventListener("click", () => unlinkRecord(Number(button.dataset.unlink), d.device_id));
  }
  for (const button of technicalBody.querySelectorAll("button[data-link-connector]")) {
    button.addEventListener("click", () => linkRecord(button.dataset, d.device_id));
  }
  for (const button of technicalBody.querySelectorAll("button[data-copy]")) {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copy);
        button.textContent = "copied";
        window.setTimeout(() => (button.textContent = "copy"), 1500);
      } catch (error) {
        fail(new Error("The browser refused the clipboard; the id is beside the button to select."));
      }
    });
  }
}

function renderDetail(data) {
  const d = data.device;
  detailTitle.textContent = d.device;
  detailSub.textContent = [
    d.site,
    describeMachine(d) || "kind of machine unknown",
    `first seen ${timeWords(d.first_seen)}`,
    `last seen by a tool ${timeWords(d.last_seen)}`,
  ]
    .filter(Boolean)
    .join(" · ");
  detailVerdict.innerHTML = verdict(d);
  detailSections.innerHTML = [
    protectionSection(data),
    vulnerabilitySection(data),
    patchSection(data),
    ticketSection(data),
    machineSection(data),
  ].join("");
  renderTechnical(data);
}

document.getElementById("detail-close").addEventListener("click", () => closePanel());

/* --- records nobody can place: Inputiv staff ----------------------------------------- */

async function loadQueue() {
  const data = await api("GET", "/devices/unresolved");
  queueSub.textContent = `${plural(data.count, "tool record")} that a pass found more than one machine for, so nothing was paired. Each link or unlink needs a reason and is recorded with your name.`;
  queueRows.innerHTML = data.entries.length
    ? data.entries
        .map(
          (e) => `<tr>
          <td>${escapeHtml(e.site)}</td>
          <td>${escapeHtml(VENDOR_NAMES[e.connector] || e.connector)}</td>
          <td><strong>${escapeHtml(e.vendor_name || "(no name)")}</strong><div class="mono small muted">${escapeHtml(e.external_id)}</div></td>
          <td>${escapeHtml(stateLabel("match", e.found_by))}</td>
          <td>${e.candidates
            .map(
              (c) => `<div class="inline"><a href="?device=${escapeHtml(c.device_id)}" data-open="${escapeHtml(c.device_id)}">${escapeHtml(c.device || c.device_id)}</a>${c.status ? stateChip("device", c.status) : ""}${
                mayLink && c.known
                  ? `<button class="action" type="button" data-link-connector="${escapeHtml(e.connector)}" data-link-external="${escapeHtml(e.external_id)}" data-link-name="${escapeHtml(e.vendor_name || "")}" data-link-device="${escapeHtml(c.device_id)}">it is this one</button>`
                  : ""
              }</div>`,
            )
            .join("")}</td>
          <td class="nowrap">${when(e.first_seen)}</td>
        </tr>`,
        )
        .join("")
    : nothingRow(
        6,
        "Nothing is waiting: every record a pass could not place has been tied to a machine, or has gone.",
      );
  for (const anchor of queueRows.querySelectorAll("a[data-open]")) {
    anchor.addEventListener("click", (event) => {
      event.preventDefault();
      openDevice(anchor.dataset.open);
    });
  }
  for (const button of queueRows.querySelectorAll("button[data-link-connector]")) {
    button.addEventListener("click", () => linkRecord(button.dataset, null));
  }
}

async function linkRecord(dataset, openAfter) {
  const reason = await ask({
    title: `Why is this ${VENDOR_NAMES[dataset.linkConnector] || dataset.linkConnector} record this machine?`,
    body: `${dataset.linkName || dataset.linkExternal} is tied to this machine by hand, which the daily job then leaves alone. Your name goes on the record, and the coverage numbers change.`,
    reason: {
      label: "Reason",
      required: true,
      missing: "Tying a record to a machine needs a reason: the daily job will not question it again.",
    },
    confirm: "Tie it to this machine",
  });
  if (reason === null) return;
  try {
    await api("POST", "/devices/links", {
      device_id: dataset.linkDevice,
      connector: dataset.linkConnector,
      external_id: dataset.linkExternal,
      vendor_name: dataset.linkName || null,
      reason,
    });
    await load();
    await openDevice(openAfter || dataset.linkDevice);
  } catch (error) {
    fail(error);
  }
}

async function unlinkRecord(linkId, deviceId) {
  const reason = await ask({
    title: "Why is this record not this machine?",
    body: "The link stays on record as withdrawn, the record goes back to waiting for somebody, and the coverage numbers change.",
    reason: {
      label: "Reason",
      required: true,
      missing: "Withdrawing a link needs a reason: the link is kept, not deleted.",
    },
    confirm: "Withdraw the link",
    tone: "destructive",
  });
  if (reason === null) return;
  try {
    await api("POST", `/devices/links/${linkId}/unlink`, { reason });
    await load();
    await openDevice(deviceId);
  } catch (error) {
    fail(error);
  }
}

/* --- the agreement: Inputiv staff ------------------------------------------------------ */

async function loadAgreement() {
  const data = await api("GET", "/devices/agreement");
  const latest = data.latest;
  const flag = data.identity_enabled
    ? "The identity flag is on: the board and the tools read the stored links."
    : "The identity flag is off: the board and the tools run the join live; the links are written and compared only.";
  if (!latest) {
    agreementBox.className = "notice";
    agreementBox.innerHTML = `<strong>No comparison has run yet.</strong> The daily collector compares the board computed live against the board read from the stored links after each resolution. ${escapeHtml(flag)}`;
    agreementBox.hidden = false;
    return;
  }
  const shares = Object.entries(latest.summary.covered_percent || {})
    .map(([k, v]) => `${k} ${v[0] === null ? "n/a" : v[0] + "%"} / ${v[1] === null ? "n/a" : v[1] + "%"}`)
    .join(" · ");
  if (latest.agree) {
    agreementBox.className = "notice good";
    agreementBox.innerHTML = `<strong>The live join and the stored links agree</strong> as of ${escapeHtml(timeWords(latest.computed_at))}: ${latest.sites_compared} customers identical, ${latest.summary.managed[0]} managed, ${latest.summary.unmanaged[0]} unmanaged; covered off / on: ${escapeHtml(shares)}. ${escapeHtml(flag)}`;
  } else {
    agreementBox.className = "notice bad";
    agreementBox.innerHTML = `<strong>The live join and the stored links disagree</strong> as of ${escapeHtml(timeWords(latest.computed_at))}, on ${latest.sites_differing.length} of ${latest.sites_compared} customers (${latest.differences.length} counts). Nothing reconciles this automatically: somebody decides. ${escapeHtml(flag)}
      <ul>${latest.differences
        .map((x) => `<li>${escapeHtml(x.site)}: ${escapeHtml(x.field)} is ${escapeHtml(String(x.off))} through the live join and ${escapeHtml(String(x.on))} through the links</li>`)
        .join("")}</ul>`;
  }
  agreementBox.hidden = false;
}

/* The address bar is the page's whole state, so Back and Forward move through
   it: a different customer means a new fetch, a different machine means the
   panel shows that one, and no machine at all means the panel closes. */
window.addEventListener("popstate", () => {
  const state = urlState();
  if (filterSite.value !== state.site) {
    load();
    return;
  }
  if (filterKey(state) !== drawnFor) draw();
  if (!state.device) closePanel({ fromHistory: true });
  else if (state.device !== panelDevice) openDevice(state.device, "none");
});

load();


/* --- the filters, behind one button ---------------------------------------------- */

const filtersButton = document.getElementById("filters-button");
const filtersList = document.getElementById("filters-list");

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
  /* Escape closes the menu, but not while the machine panel is open: that
     dialog has its own Escape and it is the one in front. */
  if (event.key === "Escape" && document.getElementById("detail-panel").hidden) closeFilters();
});

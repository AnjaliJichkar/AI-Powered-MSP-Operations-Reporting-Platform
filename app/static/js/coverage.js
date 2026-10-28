/* Coverage: which managed computers each tool covers, site by site.
 *
 * The board is the latest stored snapshot (`GET /coverage/board`), with the
 * client's live exclusions laid over it; the page never asks a vendor. Refresh
 * (`POST /coverage/refresh`, administrators and Inputiv staff) computes a new
 * board in the background, one at a time per client, and the page polls
 * `GET /coverage/refresh` until it is done and then reloads. Nothing here
 * decides a state. A site row opens its device list; an administrator or
 * Inputiv staff can exclude a device from a tool with a reason, and the
 * device then shows as excluded rather than disappearing. A viewer sees
 * everything and changes nothing: the buttons are not drawn for them, and
 * the endpoints would refuse them.
 *
 * Nothing is truncated. Device names and reasons print whole; the tables
 * scroll (see app.css on why).
 */

const errorBox = document.getElementById("error");
const newGapsBox = document.getElementById("new-gaps");
const matrixHead = document.getElementById("matrix-head");
const matrixRows = document.getElementById("matrix-rows");
const matrixSub = document.getElementById("matrix-sub");
const siteCard = document.getElementById("site-card");
const siteTitle = document.getElementById("site-title");
const siteSub = document.getElementById("site-sub");
const siteExport = document.getElementById("site-export");
const devicesHead = document.getElementById("devices-head");
const devicesRows = document.getElementById("devices-rows");
const tiles = document.getElementById("tiles");
/** What the board left out this run — devices that are not computers, a
 *  listing that hit its cap — kept for the disclosure under the tiles. */
let boardNotes = [];
const unmanagedTitle = document.getElementById("unmanaged-title");
const unmanagedBox = document.getElementById("unmanaged-box");
const unmanagedRows = document.getElementById("unmanaged-rows");
const discoveredTitle = document.getElementById("discovered-title");
const discoveredBox = document.getElementById("discovered-box");
const discoveredRows = document.getElementById("discovered-rows");
const externalTitle = document.getElementById("external-title");
const externalBox = document.getElementById("external-box");
const externalRows = document.getElementById("external-rows");
const duplicationRows = document.getElementById("duplication-rows");
const exclusionRows = document.getElementById("exclusion-rows");
const rulesList = document.getElementById("rules");
const trendCard = document.getElementById("trend-card");
const trendHead = document.getElementById("trend-head");
const trendRows = document.getElementById("trend-rows");
const snapshotWhen = document.getElementById("snapshot-when");
const refreshButton = document.getElementById("refresh");
const refreshStatus = document.getElementById("refresh-status");
const gapsPanel = document.getElementById("gaps-panel");
const gapsScrim = document.getElementById("gaps-scrim");
const gapsBody = document.getElementById("gaps-body");

let board = null;
let mayExclude = false;
let openSiteSlug = null;
let pollTimer = null;

/* Every state this page prints is named in `js/states.js`, which the Devices
   page and the Tickets queue read too: one vocabulary across the product, so
   the board and a device page cannot call the same thing two things. */
function chip(state, title) {
  return stateChip("coverage", state, title);
}

function percent(value) {
  if (value === null || value === undefined) return '<span class="muted">n/a</span>';
  const tone = value >= 95 ? "good" : value >= 80 ? "warn" : "bad";
  return stateWord(tone, `${value}%`);
}

function fail(error, what) {
  errorBox.innerHTML = problem(what || "Something on the coverage board did not work.", error);
  errorBox.hidden = false;
}

/* --- the board ---------------------------------------------------------------- */

/** One site's figure for one tool.
 *
 *  A tool can be assessed for the client and still have nothing to say about
 *  one customer: a site with no company on it has no population, and
 *  `covered_percent` is null. That is "not assessed", the same words the
 *  Devices page uses for the same situation — printed as `n/a` it reads as a
 *  figure that could not be worked out rather than a tool the customer does
 *  not have. */
function toolCell(tool, site) {
  const state = site.tools[tool.key];
  /* A tool can be assessed for the client and have nothing to say about one
     customer: no company there means no population. That is "not assessed",
     the same words the Devices page uses — a percentage of nothing reads as a
     figure that could not be worked out rather than a tool they do not have. */
  if (!tool.assessed || !state || state.assessable === 0) {
    return coverageState("unknown", "Not assessed");
  }
  const share = state.covered_percent;
  if (share === 100) return coverageState("ok", "Covered", true);
  /* Not a tick and not a cross: a share, because "68%" is the thing somebody
     acts on and "partial" is not. The colour follows the state, and the
     number beside it is what carries the meaning. */
  if (share === 0) return coverageState("missing", "Missing");
  return coverageState("partial", `${share}%`);
}

/** The chevron a Review button carries. One path, from the shell's own set. */
const icons = {
  chevron:
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>',
};

/** Which customers the toolbar is showing. */
let drawn = [];

function keep(site) {
  const query = (document.getElementById("site-search")?.value || "").trim().toLowerCase();
  const mode = document.getElementById("site-filter")?.value || "all";
  if (query && !`${site.site} ${site.site_slug}`.toLowerCase().includes(query)) return false;
  if (mode === "gaps") return site.gaps > 0;
  if (mode === "complete") return site.gaps === 0;
  return true;
}

function renderMatrix(data) {
  const tools = data.tools;
  /* One column per tool, each headed by that tool's own mark and name. The
     five that used to follow them — absent, stale, unmanaged, discovered,
     external — are the same machines counted a second way, and twelve columns
     ran off the right of a 1440px screen. They are in the site's own panel,
     where somebody is looking at one customer rather than comparing eight. */
  matrixHead.innerHTML = `<tr>
    <th scope="col">Customer</th>
    <th scope="col"><span class="tool-column">${vendorMark('ninjaone')}NinjaOne</span></th>
    ${tools
      .map(
        (t) =>
          `<th scope="col"><span class="tool-column">` +
          `${vendorMark(t.key === 'backup' ? 'cove' : t.key)}` +
          `${escapeHtml(COVERAGE_TOOL_VENDOR[t.key] || t.label)}</span></th>`,
      )
      .join("")}
    <th scope="col"><span class="tool-column">${vendorMark('microsoft365')}Microsoft 365</span></th>
    <th scope="col">Review</th>
  </tr>`;
  if (!data.snapshot || !data.snapshot.has_board) {
    matrixRows.innerHTML = nothingRow(
      data.tools.length + 4,
      "No board has been computed for this client yet.",
      mayExclude ? null : { text: "An administrator or Inputiv staff can press Refresh.", href: "#" },
    );
    return;
  }
  if (!data.sites.length) {
    matrixRows.innerHTML = nothingRow(
      data.tools.length + 4,
      "NinjaOne returned no managed computers for this client.",
      { text: "Check the mapping with", command: "python scripts/sync_sites.py --client <slug>" },
    );
    return;
  }
  const directory = new Set(data.directory_sites || []);
  drawn = data.sites.filter((site) => keep(site));
  matrixRows.innerHTML = drawn
    .map((site) => {
      /* The RMM's own column first: a machine it has lost is why every other
         tool's answer about that machine is worth less. */
      const lost = site.rmm_stale + site.rmm_agent_down;
      const ninja = !site.managed
        ? coverageState("unknown", "Not assessed")
        : lost
          ? coverageState("partial", `${lost} lost`)
          : coverageState("ok", "Covered", true);
      /* Where `auto` guessed this site's backup scope and somebody is still
         backing up a workstation here, the gap list is shorter than it might
         be. Said on the row, beside the number the guess shortened. */
      const mixed = site.backup_scope_mixed
        ? '<span class="badge amber" title="Some workstations here are backed up and most are not, so the scope was guessed. One setting settles it.">Confirm backup scope</span>'
        : "";
      const gaps = site.gaps;
      return `<tr class="clickable" data-site="${escapeHtml(site.site_slug)}" tabindex="0" aria-haspopup="dialog">
        <td><div class="site-name"><strong>${escapeHtml(site.site)}</strong></div>
          <span class="meta mono">${escapeHtml(site.site_slug)}</span>${mixed}</td>
        <td>${ninja}</td>
        ${tools.map((t) => `<td>${toolCell(t, site)}</td>`).join("")}
        <td>${directory.has(site.site_slug) ? coverageState("ok", "Connected", true) : coverageState("missing", "Not connected")}</td>
        <td>${
          gaps
            ? `<button type="button" class="review-link" data-site="${escapeHtml(site.site_slug)}">${gaps} gap${gaps === 1 ? "" : "s"} ${icons.chevron}</button>`
            : '<span class="muted">—</span>'
        }</td>
      </tr>`;
    })
    .join("");
  const count = document.getElementById("matrix-count");
  if (count) {
    count.textContent =
      drawn.length === data.sites.length
        ? `${plural(data.sites.length, "customer")}`
        : `${drawn.length} of ${plural(data.sites.length, "customer")}`;
  }
  const hint = document.getElementById("matrix-hint");
  if (hint) hint.textContent = "Open a customer for its machines";
  for (const control of ["site-search", "site-filter"]) {
    const node = document.getElementById(control);
    /* Once: the board is redrawn on every filter, and a listener added each
       time would narrow the list once per keystroke ever typed. */
    if (node && !node.dataset.wired) {
      node.dataset.wired = "true";
      node.addEventListener("input", () => renderMatrix(data));
      node.addEventListener("change", () => renderMatrix(data));
    }
  }
  for (const button of matrixRows.querySelectorAll("button.review-link")) {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openSite(button.dataset.site);
    });
  }
  for (const row of matrixRows.querySelectorAll("tr[data-site]")) {
    row.style.cursor = "pointer";
    row.addEventListener("click", () => openSite(row.dataset.site));
  }
}

/* A subtitle is a row of spans and the middle dots between them are drawn by
   the stylesheet, so a fact that is missing leaves no stray dot behind. Text
   nodes loose in it become flex items of their own and gap twice; everything
   written into one is therefore a span. */
function subtitle(element, parts) {
  element.innerHTML = parts.filter(Boolean).join("");
}

/** One line: what the board counts, and when it was last worked out.
 *
 *  It was two — a sentence of counts, then a row of four facts about the
 *  snapshot — which is a paragraph of metadata above a page of metadata. The
 *  three things somebody reads at a glance stay (`254 computers · 8 sites ·
 *  updated an hour ago`); who ran it and how long it took are true and are
 *  nobody's first question, so they are the line's tooltip.
 *
 *  **How old it is keeps its dot and its word.** Past the server's own
 *  threshold the freshness is a state like any other, with the server's
 *  definition behind it: a date on its own reads as ordinary to anybody who
 *  does not know the collector's cadence, which is how a three-week-old board
 *  sat here disagreeing with the Devices page about every machine. */
function renderSubtitle(data) {
  const s = data.snapshot;
  if (!s) {
    subtitle(snapshotWhen, ["<span>No board has been computed for this client yet.</span>"]);
    return;
  }
  const counts = [
    `<span>${data.managed} computers</span>`,
    `<span>${data.sites.length} sites</span>`,
  ];
  if (!s.has_board) {
    subtitle(snapshotWhen, [
      ...counts,
      `<span>from before boards were stored: refresh to see it</span>`,
    ]);
    return;
  }
  /* Everything that is true and is not the headline. `taken_by` is whoever
     ran it, which for the daily collector is a job and for a Refresh is a
     person's address. */
  const behind = [
    `Taken ${new Date(s.taken_at).toLocaleString()}`,
    `by ${s.taken_by}`,
    `computed in ${duration(s.duration_ms)}`,
  ].join(" · ");
  const fresh = s.old
    ? `<span class="state warn" title="${escapeHtml(s.age_definition || behind)}">` +
      `<span class="dot warn" aria-hidden="true"></span>updated ${escapeHtml(s.age_words)}</span>`
    : `<span title="${escapeHtml(behind)}">updated ${escapeHtml(timeWords(s.taken_at))}</span>`;
  subtitle(snapshotWhen, [...counts, fresh]);
}

function renderSnapshot(data) {
  renderSubtitle(data);
  refreshButton.hidden = !mayExclude;
  renderRefresh(data.refresh);
}

function renderRefresh(status) {
  if (!status) return;
  if (status.running) {
    setText(
      "refresh-word",
      `Refreshing… started ${timeWords(status.started_at)} by ${status.started_by}`,
    );
    document.getElementById("refresh-dot").className = "dot warn";
    refreshStatus.hidden = false;
    refreshButton.disabled = true;
    if (!pollTimer) pollTimer = setInterval(pollRefresh, 3000);
  } else {
    refreshButton.disabled = false;
    if (status.error) {
      setText("refresh-word", `Last refresh failed: ${status.error}`);
      document.getElementById("refresh-dot").className = "dot bad";
      refreshStatus.hidden = false;
    } else {
      refreshStatus.hidden = true;
    }
  }
}

async function pollRefresh() {
  try {
    const status = await api("GET", "/coverage/refresh");
    if (!status.running) {
      clearInterval(pollTimer);
      pollTimer = null;
      renderRefresh(status);
      await load();
      if (openSiteSlug) await openSite(openSiteSlug);
    } else {
      renderRefresh(status);
    }
  } catch (error) {
    clearInterval(pollTimer);
    pollTimer = null;
    fail(error);
  }
}

async function startRefresh() {
  try {
    const status = await api("POST", "/coverage/refresh");
    renderRefresh(status);
  } catch (error) {
    fail(error);
  }
}

/** One card per tool: the mark, the share, the bar and what is left.
 *
 *  Every figure is the board's own. `partial` is the tool's `stale` — a
 *  machine it holds and has not heard from lately — and `missing` is a gap;
 *  the two are counted apart because one is a machine to look at and the
 *  other is a machine to install something on. A tool with nothing to assess
 *  says so rather than drawing an empty bar. */
function renderTiles(data) {
  if (!tiles) return;
  const cards = [];
  const reporting = data.managed - data.rmm_stale - data.rmm_agent_down;
  cards.push({
    key: "ninjaone",
    label: "NinjaOne",
    covered: reporting,
    partial: data.rmm_stale + data.rmm_agent_down,
    missing: 0,
    total: data.managed,
    assessed: data.managed > 0,
    unit: "computers",
    definition:
      `The computers NinjaOne has heard from in the last ${data.rmm_stale_days} days. ` +
      "One it has lost is reported on its own line and is never counted as missing from " +
      "another tool: a machine nobody has heard from may be in a cupboard.",
  });
  for (const tool of data.tools) {
    cards.push({
      key: tool.key === "backup" ? "cove" : tool.key,
      label: COVERAGE_TOOL_VENDOR[tool.key] || tool.label,
      covered: tool.covered,
      partial: tool.stale,
      missing: tool.absent + tool.unmatched,
      total: tool.assessable,
      assessed: tool.assessed && tool.assessable > 0,
      unit: "computers",
      not_in_scope: tool.not_in_scope,
      definition: tool.assessed
        ? `The computers that should carry an agent where ${COVERAGE_TOOL_VENDOR[tool.key] || tool.label} holds a current record of the machine.` +
          (tool.not_in_scope
            ? ` ${tool.not_in_scope} machine(s) are left out: this customer does not back them up, and backup is sold per machine rather than rolled out to a fleet.`
            : "")
        : tool.reason || "No customer of this client has this tool mapped.",
    });
  }
  const directory = (data.directory_sites || []).length;
  cards.push({
    key: "microsoft365",
    label: "Microsoft 365",
    covered: directory,
    partial: 0,
    missing: data.sites.length - directory,
    total: data.sites.length,
    assessed: data.sites.length > 0,
    unit: "customers",
    definition:
      "Customers whose Microsoft 365 tenant is mapped, so Desk can read their directory. " +
      "This counts accounts rather than agents on machines, so it is not part of the board's coverage.",
  });

  /* Where `auto` guessed a site's backup scope and somebody is still backing
     up a workstation there, the gap list is shorter than it might be. It
     belongs with the rule it qualifies. */
  const mixed = data.sites.filter((s) => s.backup_scope_mixed).length;
  const backup = cards.find((c) => c.key === "cove");
  if (mixed && backup) {
    backup.definition +=
      ` ${mixed} customer(s) have some workstations backed up and most not, so the scope was ` +
      "guessed and their remaining workstations are off the gap list: each is marked on the " +
      "table below, and one setting settles it.";
  }

  tiles.innerHTML = cards
    .map((c) => {
      const pct = (n) => (c.total ? (n / c.total) * 100 : 0);
      const bar = c.assessed
        ? `<div class="coverage-bar" role="img" aria-label="${c.covered} covered, ${c.partial} needing attention, ${c.missing} missing">` +
          `<i style="width:${pct(c.covered)}%"></i>` +
          `<i class="partial" style="width:${pct(c.partial)}%"></i>` +
          `<i class="missing" style="width:${pct(c.missing)}%"></i></div>` +
          `<div class="coverage-breakdown"><span>${c.partial} need attention</span>` +
          `<span class="${c.missing ? "red" : ""}">${c.missing} missing</span></div>`
        : '<div class="coverage-breakdown"><span>Not connected</span></div>';
      const figure = c.assessed
        ? `${c.covered}<span>/ ${c.total} ${escapeHtml(c.unit)}</span>`
        : '<span style="margin:0">—</span>';
      return (
        `<section class="coverage-summary-card"><div class="row">${vendorMark(c.key)}` +
        `<h3>${escapeHtml(c.label)}</h3></div>` +
        `<div class="coverage-number">${figure}</div>${bar}</section>`
      );
    })
    .join("");

  const how = document.getElementById("tiles-how");
  if (how) {
    const caveats = boardNotes.length
      ? `<p class="meta">${escapeHtml(boardNotes.join(" "))}</p>`
      : "";
    how.innerHTML =
      "<summary>How these are counted</summary>" +
      `<dl class="facts">${cards
        .map((c) => `<dt>${escapeHtml(c.label)}</dt><dd>${escapeHtml(c.definition)}</dd>`)
        .join("")}</dl>` +
      caveats;
    how.hidden = false;
  }

  const legend = document.getElementById("legend");
  if (legend) {
    legend.innerHTML =
      coverageState("ok", "Covered") +
      coverageState("partial", "Needs attention") +
      coverageState("missing", "Missing");
  }
}

function renderSummary(data) {
  if (!data.snapshot || !data.snapshot.has_board) {
    matrixSub.textContent = "";
    return;
  }
  /* What the board left out, and why. These are caveats about the population
     rather than facts about the fleet, and above the table they were a
     paragraph between the tiles and the rows that answer the same questions
     the tiles already did. They go under "How these are counted" with the
     definitions; the ones that are not there are not news. */
  const notes = [];
  if (data.not_assessable) notes.push(`${data.not_assessable} endpoints sit at sites NinjaOne has no mapping for and are not assessed.`);
  if (data.not_computers) notes.push(`${data.not_computers} NinjaOne devices are not computers and are not counted.`);
  if (data.population_truncated) notes.push("NinjaOne's listing stopped at the page cap: every count is a lower bound.");
  for (const t of data.tools) {
    if (t.truncated) notes.push(`${t.label}'s listing stopped at the page cap.`);
  }
  boardNotes = notes;
  if (matrixSub) matrixSub.textContent = "";
  /* After the caveats are worked out, because the disclosure under the tiles
     carries both them and the definitions. */
  renderTiles(data);
}

/** What is new since the last snapshot: the count, and the names behind it.
 *
 *  Twenty device names and "and 470 more" sat between the reader and the board
 *  on every load, and the twenty were the first twenty of nothing in
 *  particular. The count is what somebody reads; the names are what they open
 *  when the count is a surprise, and now all of them are there. */
function renderNewGaps(data) {
  if (!data.new_gaps.length) {
    newGapsBox.hidden = true;
    return;
  }
  const since = data.new_gaps_since
    ? `since the snapshot of ${data.new_gaps_since}`
    : "no snapshot has been taken before this one, so every gap is new";
  setText("new-gaps-open", `${data.new_gaps.length} new gap${data.new_gaps.length === 1 ? "" : "s"} →`);
  setText("new-gaps-since", since);
  /* A gap carries the site's slug, which is what the stored boards are
     compared by. The board beside it names the customer, so the panel does
     too, and falls back to the slug for a site the board no longer holds. */
  const names = new Map(data.sites.map((site) => [site.site_slug, site.site]));
  gapsBody.innerHTML = `<div class="list">${data.new_gaps
    .map(
      (g) => `<div class="list-row">
        <span>
          <span class="what">${escapeHtml(g.device)}</span>
          <span class="meta">${escapeHtml(names.get(g.site) || g.site)} · ${escapeHtml(VENDOR_NAMES[g.tool] || g.tool)}: ${escapeHtml(stateLabel("coverage", g.state).toLowerCase())}</span>
        </span>
      </div>`,
    )
    .join("")}</div>`;
  newGapsBox.hidden = false;
}

function renderRules(data) {
  /* The eight per-tool states are printed from the one shared vocabulary, so
     this list and every chip on the page cannot describe a state differently.
     What follows them is not a state: it is how the join itself works. */
  const items = Object.keys(STATES.coverage).map((key) => {
    const state = stateOf("coverage", key);
    return `<li><strong>${escapeHtml(state.label)}</strong>: ${escapeHtml(state.reason)}</li>`;
  });
  items.push(
    `<li><strong>How long is "recently"</strong>: NinjaOne must have heard from the machine within ${data.rmm_stale_days} days (COVERAGE_RMM_STALE_DAYS). Each tool's own threshold is listed below.</li>`,
    "<li><strong>Unmanaged</strong>: a real endpoint in a tool with no managed NinjaOne computer at a site NinjaOne manages. One with a default Windows name (DESKTOP-XXXXXXX) is noted as likely not yet onboarded.</li>",
    "<li><strong>Discovered</strong>: a row a tool discovered rather than installed on (Bitdefender isManaged false; ConnectSecure probes and deprecated agents), reported apart from unmanaged.</li>",
    "<li><strong>External</strong>: a ConnectSecure row whose name is an IP address or an ISP's reverse-DNS name: an external scan target, reported apart and never an unmanaged endpoint.</li>",
    "<li><strong>Four passes</strong>, each one-to-one within the site: the exact hostname (NinjaOne's full DNS name, else its 15-character system name), then the NetBIOS 15-character name, then a shared MAC address, then a shared serial number. When a name pass is ambiguous, a MAC or serial tells the candidates apart before anything is declared unmatched. Each pairing says which pass made it. Nothing is matched across sites.</li>",
  );
  for (const t of data.tools) {
    for (const r of t.rules) items.push(`<li><strong>${escapeHtml(r.label)}</strong>: ${escapeHtml(r.definition)}</li>`);
  }
  rulesList.innerHTML = items.join("");
}

function renderTrend(data) {
  if (!data.trend.length) {
    trendCard.hidden = true;
    return;
  }
  trendHead.innerHTML = `<tr><th>Day</th><th>Managed</th>${data.tools.map((t) => `<th>${escapeHtml(t.label)}</th>`).join("")}<th>Gaps</th><th>Unmanaged</th></tr>`;
  trendRows.innerHTML = data.trend
    .map((day) => `<tr>
      <td class="mono">${escapeHtml(day.taken_on)}</td>
      <td>${day.managed}</td>
      ${data.tools.map((t) => `<td>${day.tools[t.key] === null || day.tools[t.key] === undefined ? '<span class="muted">n/a</span>' : day.tools[t.key] + "%"}</td>`).join("")}
      <td>${day.gaps}</td>
      <td>${day.unmanaged}</td>
    </tr>`)
    .join("");
  trendCard.hidden = false;
}

async function load() {
  if (needsClient()) {
    matrixRows.innerHTML = `<tr><td colspan="13">${chooseClientNotice()}</td></tr>`;
    exclusionRows.innerHTML = "";
    return;
  }
  errorBox.hidden = true;
  try {
    board = await api("GET", "/coverage/board");
    mayExclude = board.may_exclude;
    renderSnapshot(board);
    renderSummary(board);
    renderMatrix(board);
    renderNewGaps(board);
    renderRules(board);
    renderTrend(board);
    renderDuplication(board);
    await loadExclusions();
  } catch (error) {
    matrixRows.innerHTML = "";
    fail(error);
  }
}

/* --- one site ------------------------------------------------------------------ */

async function openSite(slug) {
  openSiteSlug = slug;
  errorBox.hidden = true;
  siteCard.hidden = false;
  siteTitle.textContent = "Loading…";
  devicesRows.innerHTML = "";
  siteExport.href = `/coverage/export.csv?site=${encodeURIComponent(slug)}`;
  try {
    const data = await api("GET", `/coverage/sites/${encodeURIComponent(slug)}`);
    renderSite(data);
    siteCard.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    siteTitle.textContent = "";
    fail(error);
  }
}

function excludeButton(device, toolKey, state) {
  if (!mayExclude) return "";
  if (state.state === "excluded" && state.exclusion_id) {
    return ` <button class="action" type="button" data-withdraw="${state.exclusion_id}">withdraw</button>`;
  }
  if (state.state === "not_assessed") return "";
  return ` <button class="action" type="button" data-exclude="${escapeHtml(toolKey)}" data-device="${escapeHtml(device.device_id)}" data-name="${escapeHtml(device.device)}">exclude</button>`;
}

function renderSite(data) {
  siteTitle.textContent = data.site;
  const s = data.summary;
  subtitle(
    siteSub,
    s
      ? [
          `<span>${s.managed} managed</span>`,
          `<span>${s.rmm_stale} RMM stale</span>`,
          `<span>${s.rmm_agent_down} RMM agent down</span>`,
          ...data.tools.map(
            (t) =>
              `<span>${escapeHtml(t.label)} ${t.assessed ? (s.tools[t.key].covered_percent === null ? "n/a" : s.tools[t.key].covered_percent + "%") : "not assessed"}</span>`,
          ),
          `<span>${s.unmanaged} unmanaged</span>`,
          `<span>${s.external} external</span>`,
        ]
      : [],
  );
  devicesHead.innerHTML = `<tr>
    <th>Device</th>
    <th class="nowrap">NinjaOne</th>
    ${data.tools.map((t) => `<th class="nowrap">${escapeHtml(t.label)}</th>`).join("")}
  </tr>`;
  devicesRows.innerHTML = data.devices.length
    ? data.devices
        .map((d) => `<tr>
          <td><strong>${escapeHtml(d.device)}</strong><div class="small muted">${escapeHtml(machineKind(d.node_class))}</div></td>
          <td>${chip(d.ninjaone.state, d.ninjaone.detail)}<div class="small muted">${when(d.ninjaone.last_seen)}</div></td>
          ${data.tools
            .map((t) => {
              const state = d.tools[t.key];
              const how = state.matched_by && state.matched_by !== "exact" ? ` · paired by ${stateLabel("match", state.matched_by)}` : "";
              const candidates = state.candidates && state.candidates.length ? ` · candidates: ${state.candidates.map(escapeHtml).join(", ")}` : "";
              return `<td>${chip(state.state, state.detail)}${excludeButton(d, t.key, state)}<div class="small muted">${escapeHtml(state.detail)}${how}${candidates}${state.last_seen ? " · " : ""}${state.last_seen ? when(state.last_seen) : ""}</div></td>`;
            })
            .join("")}
        </tr>`)
        .join("")
    : nothingRow(5, "NinjaOne manages no computers at this site.");
  for (const button of devicesRows.querySelectorAll("button[data-exclude]")) {
    button.addEventListener("click", () => excludeDevice(data.site_slug, button.dataset));
  }
  for (const button of devicesRows.querySelectorAll("button[data-withdraw]")) {
    button.addEventListener("click", () => withdrawExclusion(Number(button.dataset.withdraw), data.site_slug));
  }
  if (data.unmanaged.length) {
    unmanagedTitle.textContent = `${data.unmanaged.length} endpoint${data.unmanaged.length === 1 ? "" : "s"} known to a tool with no managed computer of that name, MAC or serial at this site`;
    unmanagedRows.innerHTML = data.unmanaged
      .map((u) => `<tr><td>${escapeHtml(u.tool)}</td><td><strong>${escapeHtml(u.device)}</strong>${u.rows > 1 ? `<div class="small muted">${u.rows} vendor rows</div>` : ""}</td><td>${escapeHtml(u.detail)}</td><td>${when(u.last_seen)}</td><td>${u.note ? escapeHtml(u.note) : '<span class="muted">—</span>'}</td></tr>`)
      .join("");
    unmanagedTitle.hidden = false;
    unmanagedBox.hidden = false;
  } else {
    unmanagedTitle.hidden = true;
    unmanagedBox.hidden = true;
  }
  if (data.external && data.external.length) {
    externalTitle.textContent = `${data.external.length} external scan target${data.external.length === 1 ? "" : "s"} (an IP address or an ISP's name): the outside of the network as the scanner sees it, not endpoints`;
    externalRows.innerHTML = data.external
      .map((u) => `<tr><td>${escapeHtml(u.tool)}</td><td><strong>${escapeHtml(u.device)}</strong></td><td>${escapeHtml(u.detail)}</td><td>${when(u.last_seen)}</td><td>${escapeHtml(u.note || "")}</td></tr>`)
      .join("");
    externalTitle.hidden = false;
    externalBox.hidden = false;
  } else {
    externalTitle.hidden = true;
    externalBox.hidden = true;
  }
  if (data.discovered.length) {
    discoveredTitle.textContent = `${data.discovered.length} discovered row${data.discovered.length === 1 ? "" : "s"} (not endpoints anybody could manage): Bitdefender isManaged false, ConnectSecure probes and deprecated agents`;
    discoveredRows.innerHTML = data.discovered
      .map((u) => `<tr><td>${escapeHtml(u.tool)}</td><td><strong>${escapeHtml(u.device)}</strong></td><td>${escapeHtml(u.detail)}</td><td>${when(u.last_seen)}</td></tr>`)
      .join("");
    discoveredTitle.hidden = false;
    discoveredBox.hidden = false;
  } else {
    discoveredTitle.hidden = true;
    discoveredBox.hidden = true;
  }
}

/* --- vendor duplication ------------------------------------------------------- */

function renderDuplication(data) {
  if (!data.duplication.length) {
    duplicationRows.innerHTML = nothingRow(
      6,
      "No site holds more than one organisation on any tool, which is how it should be.",
    );
    return;
  }
  duplicationRows.innerHTML = data.duplication
    .map((entry) =>
      entry.organisations
        .map(
          (o, index) => `<tr>
          ${index === 0 ? `<td rowspan="${entry.organisations.length}"><strong>${escapeHtml(entry.site)}</strong></td><td rowspan="${entry.organisations.length}">${escapeHtml(entry.tool)}</td>` : ""}
          <td>${escapeHtml(o.name || "(unnamed)")}</td>
          <td class="mono small muted">${escapeHtml(o.external_id)}</td>
          <td>${o.endpoints}</td>
          <td>${o.with_agent}</td>
        </tr>`,
        )
        .join(""),
    )
    .join("");
}

/* --- exclusions ---------------------------------------------------------------- */

async function excludeDevice(siteSlug, dataset) {
  const reason = await ask({
    title: `Why is ${dataset.name} not expected to have ${VENDOR_NAMES[dataset.exclude] || dataset.exclude}?`,
    body: "It stays on the board as not expected, with this reason and your name against it. Nothing is deleted and it can be withdrawn.",
    reason: {
      label: "Reason",
      required: true,
      missing: "An exclusion needs a reason: it is the whole of the record.",
    },
    confirm: "Exclude",
  });
  if (reason === null) return;
  try {
    await api("POST", "/coverage/exclusions", {
      site_slug: siteSlug,
      device_id: dataset.device,
      device: dataset.name,
      tool: dataset.exclude,
      reason,
    });
    await load();
    await openSite(siteSlug);
  } catch (error) {
    fail(error);
  }
}

async function withdrawExclusion(id, siteSlug) {
  const sure = await ask({
    title: "Withdraw this exclusion?",
    body: "The machine goes back to being assessed by that tool. The record of the exclusion is kept.",
    confirm: "Withdraw",
    tone: "destructive",
  });
  if (sure === null) return;
  try {
    await api("DELETE", `/coverage/exclusions/${id}`);
    await load();
    if (siteSlug) await openSite(siteSlug);
  } catch (error) {
    fail(error);
  }
}

async function loadExclusions() {
  const rows = await api("GET", "/coverage/exclusions");
  exclusionRows.innerHTML = rows.length
    ? rows
        .map((e) => `<tr>
          <td><strong>${escapeHtml(e.device)}</strong></td>
          <td>${escapeHtml(e.tool)}</td>
          <td>${escapeHtml(e.reason)}</td>
          <td>${escapeHtml(e.created_by)}</td>
          <td>${when(e.created_at)}</td>
          <td>${mayExclude ? `<button class="action" type="button" data-withdraw="${e.id}">withdraw</button>` : ""}</td>
        </tr>`)
        .join("")
    : nothingRow(6, "No machine has been recorded as not expected to have a tool.");
  for (const button of exclusionRows.querySelectorAll("button[data-withdraw]")) {
    button.addEventListener("click", () => withdrawExclusion(Number(button.dataset.withdraw), null));
  }
}

document.getElementById("site-close").addEventListener("click", () => {
  siteCard.hidden = true;
  openSiteSlug = null;
});
refreshButton.addEventListener("click", startRefresh);

/* The gap list over the page, like every other panel on the product: focus
   moves into it, Escape and the scrim close it, and the keyboard goes back to
   the link it was opened from. */
let openedGapsFrom = null;

function showGaps(open) {
  gapsPanel.hidden = !open;
  gapsScrim.hidden = !open;
  if (open) {
    openedGapsFrom = document.activeElement;
    document.getElementById("gaps-close").focus();
  } else if (openedGapsFrom) {
    openedGapsFrom.focus();
    openedGapsFrom = null;
  }
}

document.getElementById("new-gaps-open").addEventListener("click", () => showGaps(true));
document.getElementById("gaps-close").addEventListener("click", () => showGaps(false));
gapsScrim.addEventListener("click", () => showGaps(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !gapsPanel.hidden) showGaps(false);
});

/* The page's own menu: the two CSVs. Same behaviour as every other menu — it
   closes on Escape, on a click outside, and on following one of its links. */
const moreButton = document.getElementById("more-button");
const moreList = document.getElementById("more-list");

function showMore(open) {
  moreList.hidden = !open;
  moreButton.setAttribute("aria-expanded", open ? "true" : "false");
}

moreButton.addEventListener("click", () => showMore(moreList.hidden));
for (const link of moreList.querySelectorAll("a")) {
  link.addEventListener("click", () => showMore(false));
}
document.addEventListener("click", (event) => {
  if (!moreList.hidden && !event.target.closest(".menu")) showMore(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !moreList.hidden) {
    showMore(false);
    moreButton.focus();
  }
});

load();

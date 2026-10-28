/* Business reviews: making one, and reading one.
 *
 * The endpoint answers in three shapes and this page has three render paths to
 * match. Inputiv staff get the review with its workings — every slide's state,
 * the findings held for a second tool, the fact-id trace under each sentence,
 * and the three decks. A franchisee's own administrator gets the review they
 * are making of their own customer: which slides are filled, the findings they
 * are being asked to settle, and the one deck that is theirs — and none of the
 * workings. Everybody else gets a released review, as sections of plain
 * sentences.
 *
 * They are separate functions reading separate fields rather than one function
 * with the workings switched off. A released review has no `facts` and no
 * `cites`, and the path that draws it never asks for them, so reaching for a
 * field that is not there cannot happen quietly.
 *
 * The server refuses every action this page does not draw, and refuses it by
 * role and not by button: generating over the month's allowance, approving a
 * section that is not the Monthly Report, asking for a mode that is not the
 * client one, and anything belonging to another organisation's customer.
 * Hiding a control is courtesy; `app/api/reports.py` is the control.
 */

const $ = (id) => document.getElementById(id);

/* Which shape this session will be answered in.
   - STAFF make reviews and see how they were made.
   - ADMIN make reviews of their own customers and see none of that.
   - everybody else reads what has been released to them. */
const STAFF = String(DESK.role || "").startsWith("inputiv_");
const ADMIN = String(DESK.role || "") === "franchisee_admin";
const MAKER = STAFF || ADMIN;

/* The review on screen, in whichever shape this audience was given. */
let report = null;
let messageTimer = null;

const STAFF_LEDE = "One review a customer, a period at a time.";

const CLIENT_LEDE = "The reviews your Inputiv team has released to you.";

const ADMIN_LEDE = "One review a customer, a period at a time. Release it when it is right.";

/** What an administrator at a franchisee can rely on, in their words rather
 *  than ours. Behind the one link, where every other page keeps its
 *  definitions. It does not repeat the workings: those are not theirs, and a
 *  summary of them would read as a promise this page cannot show the proof of. */
const ADMIN_HOW = [
  [
    "Where the numbers come from",
    "Every count, date and price on a slide is computed from your tools and carries its own " +
      "definition. A tool that could not be read is recorded as a gap, never as a zero.",
  ],
  [
    "What you are asked to settle",
    "A finding only one tool supports is held until somebody who knows the estate says " +
      "whether it is real. Confirm it and it goes on the deck; dismiss it and it does not.",
  ],
  [
    "What releasing does",
    "It renders the customer's own copy from exactly what is on screen and makes it " +
      "downloadable. Change anything afterwards and the copy is stale until you render again.",
  ],
];

/** How a review is made, and what a reader can rely on. It was four lines
 *  across the top of this page: a method in front of the work, read once and
 *  skipped every day after. It is behind the one link now, where the
 *  definitions are on every other page. */
const HOW = [
  [
    "Where the numbers come from",
    "Every count, percentage, date and price on a slide is computed in code and carries its " +
      "own definition. A source that fails is recorded as a gap, never as a zero.",
  ],
  [
    "What the model writes",
    "Sentences, from that section's facts alone. It may not type a digit: a value is a token " +
      "code substitutes, and every rendered sentence carries the facts it rests on.",
  ],
  [
    "What is checked before a file is written",
    "Nothing that needs approval renders without it, and rendering refuses if any template " +
      "placeholder, sample figure or withheld name would survive.",
  ],
];

/* A slide's state, and a scorecard area's, as a chip. Neither is decided here:
   both arrive decided, and this only picks the colour they are shown in. */
const SLIDE_CHIP = {
  filled: "good",
  awaiting_engineer: "warn",
  dropped: "neutral",
  deferred: "accent",
  static: "neutral",
};

/** The same states in the words a franchisee reads.
 *
 *  `awaiting_engineer` is ours — it names a role inside Inputiv and a step in
 *  our process — and a customer's MSP reading it on their own review learns
 *  nothing from it. Each of these says what is true of the slide instead. */
const ADMIN_SLIDE_WORD = {
  awaiting_engineer: "not written yet",
  dropped: "not on the deck",
  deferred: "not yet available",
};

const SCORE_CHIP = {
  good: "good",
  attention: "warn",
  action: "bad",
  not_assessed: "neutral",
  no_data: "neutral",
};

const TREND_ARROW = { up: " ↑", down: " ↓", steady: " →" };

/* Sections whose content is fixed or supplied elsewhere: an engineer adds
   nothing to them by hand. */
const NO_ADDING = ["title", "agenda", "dashboard", "budget", "cx"];
const FROZEN = ["static", "deferred"];

/* --- text and safety -------------------------------------------------------- */

/** Text inside a double-quoted attribute.
 *
 * `escapeHtml` is enough between tags but not here: it leaves a quote alone,
 * and a customer whose name holds one would end the attribute and start
 * writing markup. Everything that reaches an attribute goes through this. */
/** A URL only if it is one we will follow. A source URL is recorded from a web
 *  search, so it is no more trusted than any other text: anything but http(s)
 *  is printed as characters rather than turned into a link. */
function safeUrl(url) {
  const value = String(url || "");
  return /^https?:\/\//i.test(value) ? value : null;
}

/** The message strip. `api` folds the render gate's list of problems into the
 *  message as lines, so the first line is the sentence and the rest are the
 *  refusals, one per bullet. */
/** A failure, as a sentence somebody can act on, with the server's own words
 *  behind "Details" for the people who can do something with them. */
function sayProblem(what, error) {
  const box = $("message");
  box.innerHTML = problem(what, error);
  box.className = "notice bad";
  box.hidden = false;
}

function say(text, bad) {
  const box = $("message");
  const lines = String(text).split("\n").filter((line) => line.trim() !== "");
  const [first, ...rest] = lines.length ? lines : [""];
  box.className = bad ? "notice bad" : "notice";
  box.innerHTML =
    escapeHtml(first) +
    (rest.length
      ? "<ul>" + rest.map((line) => `<li>${escapeHtml(line)}</li>`).join("") + "</ul>"
      : "");
  box.hidden = false;
  window.clearTimeout(messageTimer);
  /* A refusal stays until something replaces it; a confirmation does not need
     to sit on the screen for the rest of the meeting. */
  if (!bad) messageTimer = window.setTimeout(() => { box.hidden = true; }, 6000);
}

/* --- the toolbars ----------------------------------------------------------- */

function staffControls() {
  $("controls").innerHTML = `
    <div class="toolbar">
      <label class="field"><span>Site</span>
        <select id="site"><option value="">Loading…</option></select></label>
      <label class="field"><span>Period ending</span>
        <input id="period" type="month"></label>
      <button class="btn primary" id="generate">Generate</button>
    </div>
    <div class="toolbar">
      <label class="field"><span>Existing reviews</span>
        <select id="reports"><option value="">—</option></select></label>
      <span class="spacer"></span>
      <label class="field"><span>Output for</span>
        <select id="mode">
          <option value="client">Client copy</option>
          <option value="internal">Internal copy (with sources)</option>
          <option value="demo">Demo copy (pseudonyms)</option>
        </select></label>
      <button class="btn secondary" id="render" disabled>Render deck</button>
      <a class="btn secondary" id="download" hidden>Download</a>
      <button class="btn secondary" id="render-pdf" disabled>Render PDF</button>
      <a class="btn secondary" id="download-pdf" hidden>Download PDF</a>
    </div>`;

  $("generate").addEventListener("click", generate);
  $("render").addEventListener("click", () => renderOutput("deck"));
  $("render-pdf").addEventListener("click", () => renderOutput("pdf"));
  $("mode").addEventListener("change", showDownload);
  $("reports").addEventListener("change", openSelected);
  listenForActions();
}

/** Every review action is a button carrying what it does, so one listener
 *  serves the whole screen however many times it is redrawn. */
function listenForActions() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-act]");
    if (button) act(button);
  });
}

/** What a franchisee's own administrator is given: the same two rows as an
 *  engineer, with the mode picker gone. Their copy is the customer's copy and
 *  there is no other one for them to choose, so a picker with one entry in it
 *  would be a control that asks a question with one answer.
 *
 *  "Render and release" is one button because it is one act: rendering the
 *  client deck *is* releasing the review. Two buttons would imply a step
 *  between them that does not exist. */
function adminControls() {
  $("controls").innerHTML = `
    <div class="toolbar">
      <label class="field"><span>Customer</span>
        <select id="site"><option value="">Loading…</option></select></label>
      <label class="field"><span>Period ending</span>
        <input id="period" type="month"></label>
      <button class="btn primary" id="generate">Generate</button>
    </div>
    <div class="toolbar">
      <label class="field"><span>Reviews</span>
        <select id="reports"><option value="">—</option></select></label>
      <span class="spacer"></span>
      <button class="btn secondary" id="render" disabled>Render and release</button>
      <a class="btn secondary" id="download" hidden>Download</a>
      <button class="btn secondary" id="render-pdf" disabled>Render the Monthly Report</button>
      <a class="btn secondary" id="download-pdf" hidden>Download the Monthly Report</a>
    </div>`;

  $("generate").addEventListener("click", generate);
  $("render").addEventListener("click", () => renderOutput("deck"));
  $("render-pdf").addEventListener("click", () => renderOutput("pdf"));
  $("reports").addEventListener("change", openSelected);
  listenForActions();
}

function releasedControls() {
  $("controls").innerHTML = `
    <div class="toolbar">
      <label class="field"><span>Review</span>
        <select id="reports"><option value="">—</option></select></label>
      <span class="spacer"></span>
      <a class="btn secondary" id="download" hidden>Download the deck</a>
      <a class="btn secondary" id="download-pdf" hidden>Download the Monthly Report</a>
    </div>`;
  $("reports").addEventListener("change", openSelected);
}

/* --- loading ---------------------------------------------------------------- */

async function loadSites() {
  const select = $("site");
  const sites = await api("GET", "/reports/tbr/sites");
  select.innerHTML = "";
  select.appendChild(new Option(sites.length ? "Choose…" : "No sites yet", ""));
  /* `new Option` sets the text as text: a customer's name never becomes markup. */
  for (const site of sites) select.appendChild(new Option(site.name, site.slug));
}

function reviewLabel(row) {
  const parts = [row.site, row.period];
  if (STAFF) {
    parts.push(row.released ? "released" : row.status);
    if (row.created_by) {
      const when = row.created_at ? `, ${timeWords(row.created_at)}` : "";
      parts.push(`by ${row.created_by}${when}`);
    }
  } else if (ADMIN) {
    /* Released, or not yet — and theirs or ours, which is what decides
       whether they may generate over it. Never a user id: who at Inputiv
       drafted a review is not something the server sends them. */
    parts.push(row.released ? "released" : "not released yet");
    parts.push(row.mine ? "yours" : "by Inputiv");
  }
  return parts.filter(Boolean).join(" · ");
}

async function loadReports() {
  const select = $("reports");
  const rows = await api("GET", "/reports/tbr");
  select.innerHTML = "";
  select.appendChild(new Option(rows.length ? "Choose…" : "None yet", ""));
  for (const row of rows) select.appendChild(new Option(reviewLabel(row), row.report_id));
  if (report) select.value = report.report_id;
  renderTiles(rows);
  if (!rows.length && !report) {
    $("review").innerHTML = STAFF
      ? `<div class="empty"><strong>No reviews for this client yet.</strong>
         Pick a site and the month the period ends in, then generate one.</div>`
      : ADMIN
        ? `<div class="empty"><strong>No reviews yet.</strong>
           Pick a customer and the month the period ends in, then generate one.</div>`
        : `<div class="empty"><strong>No business reviews yet.</strong>
           One appears here when your Inputiv team has finished it and released it to you.</div>`;
  }
}

/** Figures about this client's reviews, counted from the list itself.
 *
 *  A technician or a viewer sees one — how many have been released to them —
 *  because the rest are about work they are not shown: a draft they cannot
 *  open is not a figure about their estate. An administrator sees all three,
 *  because every row in the list is one of theirs. */
function renderTiles(rows) {
  const grid = $("tiles");
  if (!grid) return;
  const released = rows.filter((r) => r.released).length;
  const tiles = MAKER
    ? [
        { n: String(rows.length), k: "Reviews", why: STAFF ? "For this client" : "Your customers" },
        { n: String(released), k: "Released", why: `of ${rows.length}` },
        {
          n: String(rows.length - released),
          k: "Not released",
          why: rows.length - released ? "Still being made" : "None",
        },
      ]
    : [{ n: String(released), k: "Reviews", why: "Released to you" }];
  grid.innerHTML = tiles
    .map(
      (t) =>
        `<section class="kpi"><span class="meta muted">${escapeHtml(t.k)}</span>` +
        `<span class="value">${escapeHtml(t.n)}</span>` +
        `<small class="muted">${escapeHtml(t.why)}</small></section>`,
    )
    .join("");
  const how = $("tiles-how");
  if (!how || !MAKER) return;
  how.innerHTML =
    "<summary>How a review is made</summary>" +
    `<dl class="facts">${(STAFF ? HOW : ADMIN_HOW).map(
      ([term, definition]) => `<dt>${escapeHtml(term)}</dt><dd>${escapeHtml(definition)}</dd>`,
    ).join("")}</dl>`;
  how.hidden = false;
}

/** Draw the review in whichever shape this session was answered in. One place,
 *  so an action that returns the updated review never has to know which. */
function show(data) {
  if (STAFF) showStaff(data);
  else if (ADMIN) showAdmin(data);
  else showReleased(data);
}

async function openSelected() {
  const id = $("reports").value;
  if (!id) return;
  try {
    show(await api("GET", `/reports/tbr/${encodeURIComponent(id)}`));
  } catch (error) {
    sayProblem("Couldn't open that review.", error);
  }
}

/* --- what a franchisee sees -------------------------------------------------- */

/* Reads `sections`, `items` (role, lead, text), `site`, `period` and `renders`,
   and nothing else. A released review carries no facts, no cites, no findings
   and no gaps, so none of them is named here. */

function releasedItemHtml(item) {
  const lead = item.lead ? `<span class="lead">${escapeHtml(item.lead)} </span>` : "";
  return `<div class="item"><div class="text">${lead}${escapeHtml(item.text)}</div></div>`;
}

function releasedSectionHtml(section) {
  const items = (section.items || []).map(releasedItemHtml).join("");
  return `<div class="slide">
    <header>
      <span class="number">${escapeHtml(section.slide)}</span>
      <span class="title">${escapeHtml(section.title)}</span>
    </header>
    <div class="body">${items || '<p class="small muted">Nothing on this slide.</p>'}</div>
  </div>`;
}

function showReleased(data) {
  report = data;
  showDownload();
  const sections = data.sections || [];
  $("review").innerHTML =
    `<div class="page-head">
      <h2>${escapeHtml(data.site)} — ${escapeHtml(data.period)}</h2>
      ${stateWord("good", "released")}
    </div>` +
    (sections.length
      ? `<div>${sections.map(releasedSectionHtml).join("")}</div>`
      : `<div class="empty"><strong>Nothing to show.</strong>
         This review has no slides with content on them.</div>`);
}

/* --- what a franchisee's own administrator sees -------------------------------- */

/** A finding only one tool reports, put to the person who knows the estate.
 *
 *  The sentence names the tools and nothing else. `sources` never reaches this
 *  browser: the server sends tool names already, because a key a reader may
 *  not see is not one to send and then not draw. */
function adminHeldHtml(finding) {
  const tools = finding.tools || [];
  const only =
    tools.length === 1
      ? `Only ${escapeHtml(tools[0])} reports this.`
      : tools.length
        ? `Only ${escapeHtml(tools.join(" and "))} report this.`
        : "Only one tool reports this.";
  return `<div class="card">
    <header>
      <h3>${escapeHtml(finding.headline)}</h3>
      ${stateWord("warn", "needs your answer")}
    </header>
    <p class="small muted">${only} Confirm it if it is real, dismiss it if it is not.</p>
    <div class="row">
      <button class="btn secondary" data-act="confirm" data-finding="${escapeHtml(finding.id)}">Confirm</button>
      <button class="btn secondary" data-act="dismiss" data-finding="${escapeHtml(finding.id)}">Dismiss</button>
    </div>
  </div>`;
}

/** One slide, as it will read. The same drawing as a released review, with the
 *  slides that are not filled kept and said to be not filled: this reader is
 *  making the review and an empty slide is a thing they may want to know about,
 *  where a reader of a released one is only being told the result. */
function adminSectionHtml(section) {
  const items = (section.items || []).map(releasedItemHtml).join("");
  /* Only where it is the exception: a slide that is filled says nothing, and a
     slide that is not says what it is. A chip on every row is a column of
     chips nobody reads. */
  const word = ADMIN_SLIDE_WORD[section.status];
  const chip = word ? stateWord(SLIDE_CHIP[section.status] || "neutral", word) : "";
  return `<div class="slide">
    <header>
      <span class="number">${escapeHtml(section.slide)}</span>
      <span class="title">${escapeHtml(section.title)}</span>
      ${chip}
    </header>
    <div class="body">${items || '<p class="small muted">Nothing on this slide.</p>'}</div>
  </div>`;
}

/** The Monthly Report's approval, where it is still waiting.
 *
 *  It is the one thing on this screen an administrator approves, and the
 *  sentence says what approving does rather than naming the step: "approve the
 *  monthly_report section" is our word for it, not theirs. */
function adminApprovalHtml(data) {
  const section = (data.sections || []).find((s) => s.key === data.monthly_section);
  if (!section || !section.requires_approval || section.approved_by) return "";
  if (section.status !== "filled") return "";
  return `<div class="card">
    <header>
      <h3>Monthly Report</h3>
      ${stateWord("warn", "not approved")}
    </header>
    <p class="small muted">The customer's PDF is rendered from the same figures as the deck.
      It will not render until you have approved it.</p>
    <div class="row">
      <button class="btn secondary" data-act="approve"
              data-section="${escapeHtml(section.key)}">Approve the Monthly Report</button>
    </div>
  </div>`;
}

function showAdmin(data) {
  report = data;
  showDownload();
  /* There is a review on screen, so there is something to render. The same
     two lines the staff path has, for the same reason. */
  $("render").disabled = false;
  $("render-pdf").disabled = false;
  const held = (data.held_findings || []).filter((f) => !f.confirmed);
  const approval = adminApprovalHtml(data);
  const sections = (data.sections || []).filter((s) => s.status !== "static");
  const released = data.released;
  $("review").innerHTML =
    `<div class="page-head">
      <div>
        <h2>${escapeHtml(data.site)} — ${escapeHtml(data.period)}</h2>
        <p class="meta">
          <span>${data.mine ? "Generated by you" : "Generated by Inputiv"}</span>
          <span>${escapeHtml(timeWords(data.generated_at))}</span>
          ${released ? `<span>released ${escapeHtml(timeWords(data.released_at))}</span>` : ""}
        </p>
      </div>
      ${released ? stateWord("good", "released") : stateWord("neutral", "not released yet")}
    </div>` +
    (held.length
      ? `<h3 class="section-label">${escapeHtml(plural(held.length, "finding"))} to settle</h3>` +
        held.map(adminHeldHtml).join("")
      : "") +
    approval +
    `<h3 class="section-label">What the customer will see</h3>` +
    (sections.length
      ? `<div>${sections.map(adminSectionHtml).join("")}</div>`
      : `<div class="empty"><strong>Nothing to show.</strong>
         This review has no slides with content on them.</div>`);
}

/* --- what the people making the review see ------------------------------------ */

function itemHtml(section, item, index) {
  const lead = item.lead ? `<span class="lead">${escapeHtml(item.lead)} </span>` : "";
  /* A table row prints its cells; everything else prints the sentence that
     will be rendered, values already substituted. */
  const body =
    item.cells && item.cells.length
      ? item.cells.map(escapeHtml).join(" · ")
      : escapeHtml(item.text);
  const url = safeUrl(item.url);
  const trace = url
    ? `<a class="cites" href="${escapeHtml(url)}" target="_blank" rel="noreferrer noopener">${escapeHtml(url)}</a>`
    : `<span class="cites">${escapeHtml((item.cites || []).join(", ") || item.url || "")}</span>`;
  const editable = !FROZEN.includes(section.status);
  const buttons = editable
    ? `<button class="action" data-act="edit" data-section="${escapeHtml(section.key)}" data-index="${index}">Edit</button>
       <button class="action" data-act="remove" data-section="${escapeHtml(section.key)}" data-index="${index}">Remove</button>`
    : "";
  return `<div class="item">
    ${stateWord("neutral", item.author)}
    <div class="text">${lead}${body}${trace}</div>
    ${buttons}
  </div>`;
}

function scorecardHtml(rows) {
  const chips = rows
    .map(
      (row) =>
        stateWord(
          SCORE_CHIP[row.status] || "neutral",
          `${row.label}: ${row.status_label}${TREND_ARROW[row.trend] || ""}`,
          row.reason,
        )
    )
    .join(" ");
  const workings = rows
    .filter((row) => row.metrics && row.metrics.length)
    .map(
      (row) =>
        `${escapeHtml(row.label)}: ` +
        row.metrics
          .map(
            (metric) =>
              `${escapeHtml(metric.label)} ${escapeHtml(metric.display)} is ` +
              `${escapeHtml(metric.status)} (${escapeHtml(metric.rule)}) ` +
              `[${escapeHtml(metric.fact_id)}` +
              `${metric.per_fact_id ? " per " + escapeHtml(metric.per_fact_id) : ""}]`
          )
          .join("; ")
    )
    .join(". ");
  return `<div class="inline">${chips}</div>
    <p class="small muted">Computed by the rules in scorecard.toml, never drafted. ${workings}</p>`;
}

/** The controls under a section: the ones only it has, then adding and notes. */
function sectionToolsHtml(section) {
  const key = escapeHtml(section.key);
  let html = "";

  if (section.key === "trends") {
    html += `<div class="row">
      <input class="grow" type="text" id="industry" placeholder="Industry, e.g. Commercial real estate"
             value="${escapeHtml((section.data || {}).industry || "")}">
      <button class="btn secondary" data-act="industry">Research trends</button>
    </div>`;
  }

  if (section.key === "dashboard" && report) {
    const on = report.people_names_shown;
    const overridden = report.show_contact_names !== null && report.show_contact_names !== undefined;
    const standing = overridden
      ? `${on ? "names on" : "names off"} for this review, set by ${escapeHtml(report.show_contact_names_by || "")}`
      : `franchisee default: names ${report.people_names_default ? "on" : "off"}`;
    html += `<div class="row">
      <label class="inline"><input type="checkbox" id="contact-names" ${on ? "checked" : ""}>
        Show people's names (contact chart, lifecycle users, person-named locations) on client and internal renders</label>
      <button class="btn secondary" data-act="contact_names">Save</button>
      ${overridden ? '<button class="btn secondary" data-act="contact_names_default">Use the franchisee default</button>' : ""}
      <span class="small muted">${standing}${on ? "" : ": Contact A, User A, locations withheld"}. Demo renders pseudonymise everyone.</span>
    </div>`;
  }

  if (section.key === "takeaways" && section.scorecard && section.scorecard.length) {
    html += scorecardHtml(section.scorecard);
  }

  if (section.key === "cx") {
    const data = section.data || {};
    html += `<div class="row">
      <input id="nps" size="6" type="text" placeholder="NPS" value="${escapeHtml(data.nps || "")}">
      <input id="csat" size="6" type="text" placeholder="CSAT" value="${escapeHtml(data.csat || "")}">
      <input id="pqual" size="6" type="text" placeholder="PQUAL" value="${escapeHtml(data.pqual || "")}">
    </div>
    <div class="row">
      <input class="grow" type="text" id="c1" placeholder="Survey comment">
      <input class="grow" type="text" id="c2" placeholder="Survey comment">
      <input class="grow" type="text" id="c3" placeholder="Survey comment">
      <button class="btn secondary" data-act="scores">Save survey results</button>
    </div>`;
  }

  if (!FROZEN.includes(section.status) && !NO_ADDING.includes(section.key)) {
    html += `<div class="row">
      <input class="grow" type="text" id="add-${key}" placeholder="Add an item">
      <button class="btn secondary" data-act="add" data-section="${key}">Add</button>
    </div>`;
  }

  if (!FROZEN.includes(section.status)) {
    /* Speaker notes are the only thing a client deck's notes may hold, so they
       are written here by the engineer who will present the slide. */
    html += `<div class="row">
      <textarea class="grow" id="notes-${key}" rows="2" maxlength="2000"
        placeholder="Speaker notes for this slide (yours only; optional)">${escapeHtml(section.talk_track || "")}</textarea>
      <button class="btn secondary" data-act="notes" data-section="${key}">Save notes</button>
    </div>`;
  }

  return html;
}

function sectionHtml(section, slide) {
  /* A section always has a line in the status report, but the screen should not
     go blank if one is ever missing: what the section itself says stands in. */
  const state = slide || {
    slide: section.slide,
    status: section.status,
    on_deck: "no",
    why: section.reason || "",
    later_connector: section.later_connector || "",
  };
  const items = [...(section.items || []), ...(section.engineer_items || [])];
  const onDeck = state.on_deck === "yes";
  /* Slide 0 is the Monthly Report PDF's approval step, not a slide. */
  const isPdf = Number(state.slide) === 0;

  const approve =
    section.requires_approval && items.length
      ? section.approved_by
        ? `<span class="small muted">approved by ${escapeHtml(section.approved_by)}</span>`
        : `<button class="btn secondary" data-act="approve" data-section="${escapeHtml(section.key)}">Approve</button>`
      : "";

  const later = state.later_connector
    ? ` Later: ${escapeHtml(state.later_connector)}.`
    : "";

  return `<div class="slide">
    <header>
      <span class="number">${isPdf ? "PDF" : escapeHtml(state.slide)}</span>
      <span class="title">${escapeHtml(section.title)}</span>
      ${stateWord(SLIDE_CHIP[state.status] || "neutral", String(state.status).replace("_", " "))}
      ${stateWord(
        onDeck || isPdf ? "accent" : "neutral",
        isPdf ? "the Monthly Report" : onDeck ? "on the deck" : "not on the deck",
      )}
      <span class="small muted">text: ${escapeHtml(section.narrative)}</span>
      ${approve}
    </header>
    <div class="body">
      <p class="small muted">${escapeHtml(state.why)}${later}</p>
      ${section.waiting_for ? `<div class="notice">→ ${escapeHtml(section.waiting_for)}</div>` : ""}
      ${items.map((item, index) => itemHtml(section, item, index)).join("")}
      ${
        section.problems && section.problems.length
          ? `<div class="notice bad">Model draft rejected: ${section.problems
              .map(escapeHtml)
              .join("; ")}</div>`
          : ""
      }
      ${sectionToolsHtml(section)}
    </div>
  </div>`;
}

/** A finding one tool supports, waiting for a person to check a second.
 *
 *  **The sentence names tools, never slugs or fact ids.** `bitdefender`,
 *  `security.edr_sensor_off.not_licensed` and the word "derived" are how the
 *  review is built, not what it found; printed on the card they read as the
 *  finding and they are not. They are still here, whole, behind Details —
 *  this is an engineer's screen and the trace is how the answer is proved —
 *  and Details is drawn for Inputiv staff alone, which is who this path is
 *  rendered for at all. */
function heldHtml(finding) {
  /* Only the sources that are tools a reader has heard of. A review's
     `sources` also carries `derived`, which is how we worked something out
     and not somewhere it came from; on the card it reads as a second vendor.
     It is in Details with the rest. */
  const tools = (finding.sources || [])
    .filter((key) => (DESK.vendorMarks || {})[key])
    .map((key) => vendorName(key));
  const only =
    tools.length === 1
      ? `Only ${escapeHtml(tools[0])} reports this.`
      : tools.length
        ? `Only ${escapeHtml(tools.join(" and "))} report this.`
        : "Only one tool reports this.";
  const ids = (finding.fact_ids || []).join(", ");
  return `<div class="card">
    <header>
      <h3>Held for confirmation: ${escapeHtml(finding.headline)}</h3>
      ${stateWord("warn", "needs a second tool")}
    </header>
    <p class="small muted">${only} Check a second tool before confirming.</p>
    ${
      ids || tools.length
        ? `<details class="detail-box small"><summary>Details</summary>
            <dl class="facts">
              ${tools.length ? `<dt>Reported by</dt><dd>${escapeHtml((finding.sources || []).join(", "))}</dd>` : ""}
              ${ids ? `<dt>Facts</dt><dd class="mono">${escapeHtml(ids)}</dd>` : ""}
            </dl></details>`
        : ""
    }
    <div class="row">
      <button class="btn secondary" data-act="confirm" data-finding="${escapeHtml(finding.id)}">Confirm</button>
      <button class="btn secondary" data-act="dismiss" data-finding="${escapeHtml(finding.id)}">Dismiss</button>
    </div>
  </div>`;
}

/** Every fact behind the review, whole. It scrolls rather than truncating: half
 *  a definition on a wall is worse than a scrollbar. */
function factsHtml(facts) {
  const rows = facts
    .map(
      (fact) => `<tr>
        <td class="mono">${escapeHtml(fact.id)}</td>
        <td class="num nowrap">${escapeHtml(fact.display)}</td>
        <td>${escapeHtml(fact.definition)}</td>
        <td>${escapeHtml(fact.source)}${fact.period_bound ? "" : " (now)"}</td>
      </tr>`
    )
    .join("");
  return `<div class="scroller"><table>
    <thead><tr><th>Fact</th><th>Value</th><th>Definition</th><th>Source</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function showStaff(data) {
  report = data;
  $("render").disabled = false;
  $("render-pdf").disabled = false;
  showDownload();

  const slides = {};
  for (const slide of data.slides || []) slides[slide.slide] = slide;
  const held = (data.findings || []).filter((finding) => !finding.assertable);
  const facts = data.facts || [];
  const made = [
    data.generated_by ? `by ${escapeHtml(data.generated_by)}` : "",
    data.generated_at ? escapeHtml(timeWords(data.generated_at)) : "",
  ].filter(Boolean);

  $("review").innerHTML = `
    <div class="page-head">
      <h2>${escapeHtml(data.site)} — ${escapeHtml(data.period)}</h2>
      ${stateWord(data.released ? "good" : "neutral", data.released ? "released" : data.status)}
      ${made.length ? `<span class="sub">Generated ${made.join(" ")}</span>` : ""}
    </div>
    ${held.length ? `<div class="stack">${held.map(heldHtml).join("")}</div>` : ""}
    <div>${(data.sections || [])
      .map((section) => sectionHtml(section, slides[section.slide]))
      .join("")}</div>
    <details class="detail-box">
      <summary>Facts and definitions (${facts.length})</summary>
      <div>${factsHtml(facts)}</div>
    </details>`;
}

/* --- acting on a review -------------------------------------------------------- */

/** The text of one item as it stands, for the edit prompt. The index counts the
 *  drafted items then the engineer's own, which is what the endpoint means by
 *  it too. */
function itemText(key, index) {
  const section = (report.sections || []).find((candidate) => candidate.key === key);
  if (!section) return "";
  const item = [...(section.items || []), ...(section.engineer_items || [])][index];
  return item ? item.text : "";
}

async function act(button) {
  if (!report) return;
  const action = button.dataset.act;
  const key = button.dataset.section;
  const body = { action };
  if (key) body.section = key;
  if (button.dataset.finding) body.finding = button.dataset.finding;
  if (button.dataset.index !== undefined) body.index = Number(button.dataset.index);

  if (action === "add") body.text = $("add-" + key).value;
  if (action === "edit") {
    const edited = await ask({
      title: "Edit this item",
      body: "The original wording is kept beside the new one, with your name.",
      reason: {
        label: "How it should read",
        value: itemText(key, Number(button.dataset.index)),
        required: true,
        missing: "An item cannot be emptied here. Remove it instead.",
      },
      confirm: "Save",
    });
    if (edited === null) return;
    body.text = edited;
  }
  if (action === "industry") body.text = $("industry").value;
  if (action === "contact_names") body.show = $("contact-names").checked;
  if (action === "contact_names_default") body.action = "contact_names";  // no `show`: clears it
  if (action === "notes") body.text = $("notes-" + key).value;
  if (action === "scores") {
    Object.assign(body, {
      nps: $("nps").value || null,
      csat: $("csat").value || null,
      pqual: $("pqual").value || null,
      comments: [$("c1").value, $("c2").value, $("c3").value].filter(Boolean),
    });
  }

  button.disabled = true;
  /* Confirming, dismissing and researching an industry all re-draft sections,
     which is a model call and not instant. */
  if (["confirm", "dismiss", "industry"].includes(action)) {
    say("Working… sections are being re-drafted.");
  }
  try {
    show(await api("POST", `/reports/tbr/${encodeURIComponent(report.report_id)}/review`, body));
    say("Saved.");
  } catch (error) {
    sayProblem("That did not work.", error);
    button.disabled = false;
  }
}

async function generate() {
  if (!$("site").value || !$("period").value) {
    say("Pick a site and a period.", true);
    return;
  }
  $("generate").disabled = true;
  say("Generating: reading the connectors, then drafting each section. About a minute.");
  try {
    show(await api("POST", "/reports/tbr", { site: $("site").value, period: $("period").value }));
    await loadReports();
    say("Draft ready for review.");
  } catch (error) {
    /* Two refusals whose own message is the answer, so it is printed instead
       of being folded behind "Details" with a sentence about trying again:
       the month's allowance (429) says when it starts again, and a review
       Inputiv is working on (403) says who to ask. Everything else gets the
       usual treatment, because a server's message is usually written for a
       log and not for a reader. */
    if (error.status === 429 || error.status === 403) say(error.message, true);
    else sayProblem("The review could not be generated.", error);
  }
  $("generate").disabled = false;
}

/** Render the deck or the Monthly Report PDF for the review on screen, in the
 *  mode chosen. `renders` holds a mode for a deck and `pdf/<mode>` for a PDF. */
async function renderOutput(output) {
  if (!report) return;
  const button = $(output === "pdf" ? "render-pdf" : "render");
  button.disabled = true;
  try {
    const result = await api(
      "POST",
      `/reports/tbr/${encodeURIComponent(report.report_id)}/render`,
      /* An administrator has no mode to choose: their copy is the customer's
         copy, and the server refuses any other from them anyway. */
      { mode: STAFF ? $("mode").value : "client", output }
    );
    if (output === "pdf") {
      const omitted = (result.omitted || []).map((o) => `${o.what} (${o.why})`);
      say(
        `Rendered ${result.file} (${result.mode} Monthly Report): ${result.pages} pages, ` +
        `${result.sections.join(", ")}` +
        (omitted.length ? `; left out: ${omitted.join("; ")}` : "") +
        ", render gate passed."
      );
    } else {
      say(
        STAFF
          ? `Rendered ${result.file} (${result.mode} deck): ${result.kept.length} slides, render gate passed.`
          : `Released: ${result.kept.length} slides, ready to download.`
      );
    }
    /* The review on screen has not changed, only which outputs exist for it. */
    report.renders = [...new Set([...(report.renders || []), renderKey(output, result.mode)])];
    showDownload();
    await loadReports();
  } catch (error) {
    say("Not rendered: " + error.message, true);
  }
  button.disabled = false;
}

function renderKey(output, mode) {
  return output === "pdf" ? `pdf/${mode}` : mode;
}

/** The download links, for whichever deck and PDF the audience may have. They
 *  are GETs and carry no token: the session cookie is what authorises them. */
function showDownload() {
  const mode = STAFF ? $("mode").value : "client";
  for (const output of ["deck", "pdf"]) {
    const link = $(output === "pdf" ? "download-pdf" : "download");
    if (!link) continue;
    const ready = Boolean(report) && (report.renders || []).includes(renderKey(output, mode));
    link.hidden = !ready;
    const what = output === "pdf" ? "Monthly Report" : "deck";
    link.textContent = STAFF ? `Download the ${mode} ${what}` : `Download the ${what}`;
    if (ready) {
      link.href = `/reports/tbr/${encodeURIComponent(report.report_id)}/download` +
        `?mode=${encodeURIComponent(mode)}&output=${output}`;
    }
  }
}

/* --- start ---------------------------------------------------------------------- */

async function start() {
  $("lede").textContent = STAFF ? STAFF_LEDE : ADMIN ? ADMIN_LEDE : CLIENT_LEDE;
  if (needsClient()) {
    $("review").innerHTML = chooseClientNotice();
    return;
  }
  if (STAFF) staffControls();
  else if (ADMIN) adminControls();
  else releasedControls();
  try {
    if (MAKER) await loadSites();
    await loadReports();
  } catch (error) {
    sayProblem("The review could not be rendered.", error);
  }
}

document.addEventListener("DOMContentLoaded", start);

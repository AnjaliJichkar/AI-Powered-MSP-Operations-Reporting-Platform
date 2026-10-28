/* One ticket, on its own page.
 *
 * The queue is `/tickets`; this is `/tickets/<number>`, where the number is the
 * one Autotask prints and an engineer reads. Its own address means its own back
 * button, a link somebody can paste into a chat message, and a browser tab per
 * ticket for anybody who works that way.
 *
 * Two reads on load, and they are independent: the workspace itself, which
 * opens or refreshes it (reading the ticket, scrubbing it, finding the machine
 * and computing the facts panel), and the queue, which is here only so that
 * Previous and Next step through the same filtered list the reader came from.
 * A failure of the second never costs them the first.
 *
 * The queue's filters ride in this page's own query string, so the breadcrumb
 * goes back to *that* queue rather than to an unfiltered one.
 *
 * Layout is two panes: the ticket and the plan on the left, which is what an
 * engineer works from; the machine and what the tools say on the right, which
 * is what they check it against. A plan's steps are a checklist — accept, edit
 * or reject each one in place — and every citation is a chip that opens. The
 * one primary action is copying the plan as an Autotask note, which is a
 * clipboard write and nothing more. Nothing here writes to a vendor and nothing
 * is executed.
 *
 * Nothing is truncated: descriptions, steps and citations print whole.
 */

const TICKET_REF = JSON.parse(document.getElementById("ticket-ref").textContent);

const errorBox = document.getElementById("error");
const crumbBack = document.getElementById("back-to-queue");
const crumbHere = document.getElementById("crumb-here");
const stepLinks = document.getElementById("step-links");
const wsTitle = document.getElementById("ws-title");
const wsSub = document.getElementById("ws-sub");
const wsQuestion = document.getElementById("ws-question");
const wsScrubbed = document.getElementById("ws-scrubbed");
const ticketBody = document.getElementById("ticket-body");
const deviceBody = document.getElementById("device-body");
const enrichSub = document.getElementById("enrich-sub");
const enrichmentBody = document.getElementById("enrichment-body");
const planSub = document.getElementById("plan-sub");
const planBody = document.getElementById("plan-body");
const generateButton = document.getElementById("plan-generate");
const copyButton = document.getElementById("plan-copy");
const railMachine = document.getElementById("rail-machine");
const machineGlance = document.getElementById("machine-glance");
const readingsOpen = document.getElementById("readings-open");
const readingsPanel = document.getElementById("readings-panel");
const readingsScrim = document.getElementById("readings-scrim");
const readingsBody = document.getElementById("readings-panel-body");
const historyBody = document.getElementById("history-body");

let current = null;
let stageTimer = null;

/** The four hours the queue's own badge uses, so "next" here and the order
 *  there cannot mean two different things. */
const FOUR_HOURS_MS = 4 * 60 * 60 * 1000;

/** Which vendor a reading came from, in the words a person uses for it. Read
 *  by the readings tab and by the findings, so both name a tool the same way. */
const TOOL_NAMES = {
  get_device: "NinjaOne",
  list_open_alerts: "NinjaOne",
  list_patch_installs: "NinjaOne",
  get_endpoint_protection: "Bitdefender",
  get_asset_vulnerabilities: "ConnectSecure",
  search_tickets: "Autotask",
  list_tickets_for_device: "Autotask",
  list_tickets_for_contact: "Autotask",
  get_microsoft365_account: "Microsoft 365",
  "cove.list_devices": "Cove",
  identity: "Desk",
};

/** The readings about the person rather than the machine. They are drawn as
 *  their own short block under the machine's readings, because "is the account
 *  enabled" and "is the disk full" are two different questions and a single
 *  list of eighteen rows makes the reader find that out for themselves. */
const ACCOUNT_PREFIX = "account.";
const isAccount = (fact) => String(fact.id || "").startsWith(ACCOUNT_PREFIX);

function fail(error, what) {
  errorBox.innerHTML = problem(what || "Something on this ticket did not work.", error);
  errorBox.hidden = false;
}

function value(v) {
  if (v === null || v === undefined || v === "") return '<span class="muted">not reported</span>';
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.length ? escapeHtml(v.join("; ")) : '<span class="muted">none</span>';
  if (typeof v === "object") {
    if ("count" in v) {
      const recent = (v.recent || []).map((t) => `${t.ticket_number} ${t.title || ""} [${t.status || ""}]`);
      return `${v.count}${recent.length ? `<div class="small muted">${escapeHtml(recent.join("; "))}</div>` : ""}`;
    }
    return escapeHtml(JSON.stringify(v));
  }
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) return when(v);
  return escapeHtml(String(v));
}

/* --- the breadcrumb, and stepping through the queue ------------------------------------
 *
 * The filters the reader left the queue with are in this page's query string.
 * They go back into the breadcrumb's link untouched, and they decide which
 * tickets Previous and Next walk: "the next one" means the next one in the list
 * they were looking at, not the next one in Autotask.
 */

/** The same filters the queue draws chips for, by key, so the walk here and the
 *  list there are one population. This page draws no chips; it only has to
 *  agree with the ones that did. */
const QUEUE_FILTERS = {
  mine: (t) => {
    const me = (DESK.name || "").trim().toLowerCase();
    return Boolean(me) && String(t.assigned_to || "").trim().toLowerCase() === me;
  },
  breached: (t) => Boolean(t.sla) && t.sla.state === "breached",
  four_hours: (t) =>
    Boolean(t.sla) &&
    t.sla.state !== "breached" &&
    Boolean(t.sla.due_at) &&
    new Date(t.sla.due_at).getTime() - Date.now() <= FOUR_HOURS_MS,
  unassigned: (t) => !String(t.assigned_to || "").trim(),
  created_today: (t) => {
    const midnight = new Date();
    midnight.setHours(0, 0, 0, 0);
    return Boolean(t.created_at) && new Date(t.created_at).getTime() >= midnight.getTime();
  },
};

/** The queue's own order, in the one form this page needs: breached first, then
 *  due within four hours, then due today, then the rest, then no clock at all. */
function queueRank(t) {
  const sla = t.sla || {};
  if (sla.state === "breached") return 0;
  if (!sla.due_at) return 4;
  const due = new Date(sla.due_at).getTime();
  if (due - Date.now() <= FOUR_HOURS_MS) return 1;
  const end = new Date();
  end.setHours(23, 59, 59, 999);
  return due <= end.getTime() ? 2 : 3;
}

function queueQuery() {
  const params = new URLSearchParams(window.location.search);
  const kept = new URLSearchParams();
  const chips = params.get("chips");
  if (chips) kept.set("chips", chips);
  const view = params.get("view");
  if (view) kept.set("view", view);
  return kept.toString();
}

function renderCrumbs() {
  const query = queueQuery();
  crumbBack.href = query ? `/tickets?${query}` : "/tickets";
  /* "Tickets / T2026…", and the queue's own filters still ride in the href, so
     this is back to *that* queue rather than to the default one. */
  crumbBack.textContent = "Tickets";
  crumbBack.title = query ? "The queue, as you left it" : "The queue";
}

/** Previous and next through the queue the reader came from. Drawn once the
 *  queue has been read, and replaced by a sentence when it could not be. */
async function renderSteps() {
  try {
    const data = await api("GET", "/tickets/list");
    const tests = (new URLSearchParams(window.location.search).get("chips") || "")
      .split(",")
      .filter(Boolean)
      .map((key) => QUEUE_FILTERS[key])
      .filter(Boolean);
    const shown = data.tickets
      .filter((t) => tests.every((test) => test(t)))
      .sort((a, b) => queueRank(a) - queueRank(b) || (b.age_days || 0) - (a.age_days || 0));
    const index = shown.findIndex((t) => t.ticket_number === TICKET_REF);
    if (index < 0) {
      stepLinks.innerHTML =
        '<span class="muted">Not in the queue you came from: it may have closed, or a filter leaves it out.</span>';
      return;
    }
    const query = queueQuery();
    const suffix = query ? `?${query}` : "";
    const before = shown[index - 1];
    const after = shown[index + 1];
    stepLinks.innerHTML = [
      before
        ? `<a href="/tickets/${encodeURIComponent(before.ticket_number)}${suffix}" title="${escapeHtml(before.title || "")}">‹ Previous</a>`
        : '<span class="muted">‹ Previous</span>',
      `<span class="muted">${index + 1} of ${shown.length}</span>`,
      after
        ? `<a href="/tickets/${encodeURIComponent(after.ticket_number)}${suffix}" title="${escapeHtml(after.title || "")}">Next ›</a>`
        : '<span class="muted">Next ›</span>',
    ].join("");
  } catch (error) {
    stepLinks.innerHTML =
      '<span class="muted">The queue could not be read, so there is nothing to step through.</span>';
  }
}

/* --- the workspace --------------------------------------------------------------------- */

async function openTicket() {
  errorBox.hidden = true;
  try {
    render(await api("POST", "/tickets/workspaces", { ticket_number: TICKET_REF }));
  } catch (error) {
    wsSub.textContent = "";
    ticketBody.innerHTML = "";
    fail(error);
  }
}

async function showWorkspace(id) {
  try {
    render(await api("GET", `/tickets/workspaces/${encodeURIComponent(id)}`));
  } catch (error) {
    fail(error);
  }
}

function render(data) {
  current = data;
  generateButton.disabled = false;
  const t = data.ticket;
  loadSuggestions(t.ticket_id);
  loadDeskNotes(t.ticket_id);
  document.title = `${t.ticket_number || ""} ${t.title || ""} · Desk`;
  /* The title is what the ticket is about. Its number is in the breadcrumb,
     where a reader looks for an identifier rather than at the headline. */
  wsTitle.textContent = t.title || t.ticket_number || TICKET_REF;
  crumbHere.textContent = t.ticket_number || TICKET_REF;
  /* One line of facts, as spans: the stylesheet draws the separators, so a
     fact we do not have leaves no stray dot behind it. */
  wsSub.innerHTML = [
    escapeHtml(t.site || "customer unknown"),
    t.priority ? `${escapeHtml(t.priority)} priority` : null,
    escapeHtml(t.status),
    escapeHtml(t.assigned_to || "unassigned"),
    /* Markup rather than escaped text, so the exact instant is on hover here
       as it is everywhere else. The rest of the parts are escaped. */
    t.created_at ? `raised ${when(t.created_at)}` : null,
    t.closed_at ? "closed" : null,
  ]
    .filter(Boolean)
    .map((part) => `<span>${part}</span>`)
    .join("");
  if (t.scrubbed) {
    wsScrubbed.textContent = `${plural(t.scrubbed, "likely secret")} ${
      t.scrubbed === 1 ? "was" : "were"
    } removed from this ticket's text before it was stored or shown to the model.`;
    wsScrubbed.hidden = false;
  } else {
    wsScrubbed.hidden = true;
  }
  /* The ticket's own words, with who raised it and when under them. The facts
     that used to be a three-row list are the subtitle's job.

     In the rail for a staff session, where the main column is what Desk found;
     in the main column for everybody else, who has no Desk block and would
     otherwise be reading a page of white space beside a rail. */
  const words = `<p class="ticket-words">${escapeHtml(t.description || "(no description)")}</p>
    <p class="small muted">${[t.contact ? escapeHtml(t.contact) : "No contact recorded", when(t.created_at)]
      .filter(Boolean)
      .join(" · ")}</p>`;
  const ticketMain = document.getElementById("ticket-main");
  if (IS_STAFF) {
    ticketBody.innerHTML = words;
    ticketMain.hidden = true;
  } else {
    document.getElementById("ticket-main-body").innerHTML = words;
    ticketMain.hidden = false;
    ticketBody.closest("section").hidden = true;
  }
  renderQuestion(data);
  renderDevice(data);
  renderEnrichment(data);
  renderPlans(data);
}

/* --- the question at the top ------------------------------------------------------------
 *
 * Which machine a ticket is about decides every fact in the right-hand pane and
 * every citation in the plan. When it is not a confident match, that is not a
 * detail in a panel: it is the first thing on the page, and it is a question.
 */

function candidateTable(d) {
  if (!d.candidates.length) return "";
  return `<div class="scroller"><table>
    <thead><tr><th>Machine</th><th>Found by</th><th>How sure</th><th>Why</th><th></th></tr></thead>
    <tbody>${d.candidates
      .map(
        (c) => `<tr>
          <td><strong>${escapeHtml(c.name)}</strong></td>
          <td>${escapeHtml(stateLabel("resolution", c.method))}</td>
          <td>${stateChip("confidence", c.confidence)}</td>
          <td>${escapeHtml(c.why)}</td>
          <td>${c.device_id !== d.device_id ? `<button class="small" type="button" data-choose="${escapeHtml(c.device_id)}">this one</button>` : "chosen"}</td>
        </tr>`,
      )
      .join("")}</tbody></table></div>`;
}

function renderQuestion(data) {
  const d = data.device;
  if (d.device && d.confidence === "high") {
    wsQuestion.hidden = true;
    return;
  }
  const heading = d.device
    ? `Is this ticket about ${escapeHtml(d.device)}?`
    : "Which machine is this ticket about?";
  const why = d.device
    ? `It was matched by ${escapeHtml(stateLabel("resolution", d.method))}, which is ${escapeHtml(stateLabel("confidence", d.confidence).toLowerCase())} — ${escapeHtml(stateReason("confidence", d.confidence))}`
    : d.method === "engineer"
      ? "An engineer recorded that this ticket is about no machine."
      : `Nothing identified one.${d.hostnames && d.hostnames.length ? ` Words in the ticket that look like machine names: ${escapeHtml(d.hostnames.join(", "))}.` : ""}`;
  wsQuestion.innerHTML = `<h3>${heading}</h3>
    <p class="small">${why} Until this is settled, everything on the right and every citation in the plan is about that machine and no other. Your answer is recorded with your name and stands.</p>
    ${candidateTable(d)}
    <div class="inline" style="margin-top: var(--space-3)">
      <button type="button" class="btn secondary" data-question="other">Choose another machine…</button>
      <button class="btn secondary" type="button" data-question="none">It is not about a machine</button>
      ${d.device ? `<button class="btn secondary" type="button" data-question="confirm" data-device="${escapeHtml(d.device_id)}">Yes, that is the machine</button>` : ""}
    </div>`;
  wsQuestion.hidden = false;
  for (const button of wsQuestion.querySelectorAll("button[data-choose]")) {
    button.addEventListener("click", () => chooseDevice(button.dataset.choose));
  }
  for (const button of wsQuestion.querySelectorAll("button[data-question]")) {
    button.addEventListener("click", () => {
      if (button.dataset.question === "other") chooseFromList();
      else if (button.dataset.question === "none") chooseDevice(null);
      else chooseDevice(button.dataset.device);
    });
  }
}

function renderDevice(data) {
  const d = data.device;
  /* The chooser, which is opened from the machine line rather than standing
     open under it. Its contents are the two ways of correcting the machine,
     and nothing else: the line above already says which machine and how sure. */
  deviceBody.innerHTML = `<p class="small muted">${
    d.device
      ? `Found by ${escapeHtml(stateLabel("resolution", d.method))}.${d.note ? ` ${escapeHtml(d.note)}` : ""}`
      : d.method === "engineer"
        ? "An engineer recorded that this ticket is about no machine."
        : `No machine stood out.${
            d.hostnames && d.hostnames.length
              ? ` Words that look like machine names: ${escapeHtml(d.hostnames.join(", "))}.`
              : ""
          }`
  }</p>
    <div class="actions">
      <button class="action" type="button" id="choose-other">Choose another machine…</button>
      <button class="action" type="button" id="choose-none">Not about a machine</button>
    </div>`;
  document.getElementById("choose-other").addEventListener("click", chooseFromList);
  document.getElementById("choose-none").addEventListener("click", () => chooseDevice(null));
}

/** The rail's "Machine at a glance": which machine, then the first few things
 *  the tools said about it, then the way to all of them.
 *
 *  The dot is the fact's own `tone`, computed in the payload by the rules that
 *  rank the findings, so "offline" means here what it means on the card. The
 *  word is beside it either way: the dot repeats the meaning, it does not
 *  carry it. */
function renderGlance(data) {
  const d = data.device;
  const facts = (data.enrichment && data.enrichment.facts) || [];
  if (!d.device && !facts.length) {
    railMachine.hidden = true;
    return;
  }
  railMachine.hidden = false;
  const lines = facts
    .filter((f) => !isAccount(f))
    .slice(0, 3)
    .map(
      (f) =>
        `<p class="state"><span class="dot ${escapeHtml(f.tone || "neutral")}"></span><span class="word">${escapeHtml(
          f.label
        )} · ${value(f.value)}</span></p>`
    )
    .join("");
  machineGlance.innerHTML =
    (d.device
      ? `<p class="glance-name"><strong>${escapeHtml(d.device)}</strong>${
          d.confidence ? ` ${stateChip("confidence", d.confidence)}` : ""
        }</p>`
      : '<p class="muted">No machine identified.</p>') + lines;
  readingsOpen.hidden = !facts.length;
  readingsOpen.textContent = `All ${facts.length} readings →`;
}

async function chooseFromList() {
  try {
    const devices = await api("GET", `/tickets/workspaces/${current.id}/devices`);
    if (!devices.length) {
      fail(new Error("We know of no machine at this customer yet; run the device resolution first."));
      return;
    }
    const picked = await ask({
      title: "Which machine is this ticket about?",
      body: `Type the name exactly. ${plural(devices.length, "machine")} at this customer, for example: ${devices
        .slice(0, 5)
        .map((d) => d.device)
        .join(", ")}`,
      reason: {
        label: "Machine name",
        required: true,
        missing: "Type the name of one of this customer's machines.",
      },
      confirm: "Choose it",
    });
    if (picked === null) return;
    const match = devices.find((d) => d.device.toLowerCase() === picked.trim().toLowerCase());
    if (!match) {
      fail(new Error(`No machine at this customer is called ${picked}.`));
      return;
    }
    await chooseDevice(match.device_id);
  } catch (error) {
    fail(error);
  }
}

async function chooseDevice(deviceId) {
  const note = await ask({
    title: deviceId ? "Why this machine?" : "Why is this ticket about no machine?",
    body: "Optional. Whatever you write goes on the record with your name, and the choice stands if the ticket is reopened.",
    reason: { label: "Note", placeholder: "Leave empty to record the choice alone" },
    confirm: "Record it",
  });
  if (note === null) return;
  try {
    render(await api("POST", `/tickets/workspaces/${current.id}/device`, { device_id: deviceId, note: note || null }));
    await refreshEnrichment();
  } catch (error) {
    fail(error);
  }
}

/* --- what the tools say ------------------------------------------------------------------ */

/** The readings whose value is one of our own state names rather than a
 *  number or a date. Drawn as a dot and a word from `states.js`, so a machine
 *  reads the same here as it does on the board and in a review — and so no
 *  panel ever prints an internal key like `not_backed_up` at a person. */
const STATE_FACTS = { "backup.status": "backup" };

/** Readings grouped by the tool that answered, in the order they arrived.
 *
 *  Thirty-four label-and-value rows in one list is a wall: the page an
 *  engineer spends the day in asks them to find "Backup" among the
 *  Bitdefender module names. Each tool's answers sit under its own mark and
 *  name, which is the heading the Coverage board and the Devices table use
 *  for the same vendors. The order the server sent is kept — it is the order
 *  the readings were taken in, and sorting by vendor would put the machine's
 *  own state somewhere other than first. */
function factGroups(rows) {
  /* Grouped by the **vendor**, not by the tool call. `get_device` and
     `list_open_alerts` are two reads of NinjaOne, and grouping on the source
     put NinjaOne on the page twice with a rule between them. */
  const order = [];
  const byVendor = new Map();
  for (const fact of rows) {
    const source = fact.source || "identity";
    /* `TOOL_NAMES` is keyed by the tool call that answered; the vendor map is
       keyed by the connector. A source in neither reads as itself. */
    const name = TOOL_NAMES[source] || vendorName(source);
    if (!byVendor.has(name)) {
      byVendor.set(name, []);
      order.push(name);
    }
    byVendor.get(name).push(fact);
  }
  return order
    .map((name) => {
      const key = Object.keys(DESK.vendorMarks || {}).find(
        (k) => vendorName(k) === name,
      );
      return (
        `<section class="reading-group"><h3>${key ? vendorMark(key) : ""}` +
        `${escapeHtml(name)}</h3>` +
        `<dl class="facts">${factRows(byVendor.get(name))}</dl></section>`
      );
    })
    .join("");
}

/** Readings as label and value, with the fact's id and tool in the title: one
 *  renderer for the machine's and the contact's. */
function factRows(rows) {
  return rows
    .map(
      (f) => `<dt title="${escapeHtml(f.id)} · from ${escapeHtml(f.source)}">${escapeHtml(f.label)}</dt><dd>${
        STATE_FACTS[f.id] && f.value ? stateChip(STATE_FACTS[f.id], String(f.value)) : value(f.value)
      }${
        f.detail ? `<div class="small muted">${escapeHtml(f.detail)}</div>` : ""
      }</dd>`,
    )
    .join("");
}

/** The contact's Microsoft 365 account, under the machine's readings: enabled,
 *  a second factor, how long since a sign-in, what is licensed.
 *
 *  Three states and they are told apart on purpose: no block at all when the
 *  ticket has no contact address to match on, one sentence when the customer
 *  has no Microsoft 365 for us to read — not assessed, which is not a gap —
 *  and the readings themselves with the tool and the time they were read. */
function accountBlock(e) {
  const rows = (e.facts || []).filter(isAccount);
  if (!rows.length) return "";
  const notAssessed = rows.find((f) => f.id === "account.not_assessed");
  /* Held off the machine's readings by space, as every section here is. */
  const open = '<section class="section" style="margin-top: var(--space-6)"><h3 class="section-title">Account</h3>';
  if (notAssessed) {
    return `${open}<p class="small muted">${escapeHtml(notAssessed.detail || "Not assessed.")}</p></section>`;
  }
  return `${open}<p class="small muted">Microsoft 365 · read ${when(e.as_of)}</p><dl class="facts">${factRows(rows)}</dl></section>`;
}

function renderEnrichment(data) {
  const e = data.enrichment;
  const count = (e.facts || []).length;
  setText("readings-count", count ? String(count) : "");
  renderGlance(data);
  if (!e.as_of) {
    enrichSub.innerHTML = "";
    enrichmentBody.innerHTML = '<p class="muted">Not computed yet.</p>';
    readingsBody.innerHTML = enrichmentBody.innerHTML;
    return;
  }
  /* The summary an engineer decides from: how much was read and by whom. The
     mockup's line, and the reason the row is worth opening. */
  /* Every tool a reading rests on, not only the one that decided it: a
     machine with no Cove record is the identity layer's answer and still took
     a Cove read to reach, and a line that left Cove out would say it was never
     asked. */
  const tools = [
    ...new Set(
      (e.facts || [])
        .flatMap((fact) => [fact.source, ...(fact.rests_on || [])])
        .map((source) => TOOL_NAMES[source])
        .filter(Boolean),
    ),
  ];
  enrichSub.innerHTML = [
    escapeHtml(plural(count, "reading")),
    tools.length ? `from ${escapeHtml(tools.join(", "))}` : null,
    `as of ${when(e.as_of)}`,
  ]
    .filter(Boolean)
    .map((part) => `<span>${part}</span>`)
    .join("");
  const facts = factGroups((e.facts || []).filter((f) => !isAccount(f)));
  const errors = (e.errors || []).map((x) => `<li>${escapeHtml(x.source)}: ${escapeHtml(x.error)}</li>`).join("");
  enrichmentBody.innerHTML = `${
    facts
      ? facts
      : '<p class="muted">No facts: no machine is identified, so only the contact\'s tickets could be looked up.</p>'
  }${accountBlock(e)}${
    errors
      ? `<div class="notice bad" style="margin-top: var(--space-5)"><strong>Tools that did not answer:</strong><ul>${errors}</ul><p class="small muted">That is not the same as nothing being wrong.</p></div>`
      : ""
  }`;
  /* The same readings, in the panel the rail's link opens: one renderer, so the
     tab and the panel cannot come to say different things. */
  readingsBody.innerHTML = enrichmentBody.innerHTML;
}

async function refreshEnrichment() {
  enrichSub.innerHTML = "<span>refreshing…</span>";
  try {
    render(await api("POST", `/tickets/workspaces/${current.id}/enrich`));
  } catch (error) {
    fail(error);
  }
}

document.getElementById("enrich-refresh").addEventListener("click", refreshEnrichment);

/* --- the plan ------------------------------------------------------------------------------ */

const CITATION_KINDS = {
  ticket: "A ticket for this customer that mentions the same thing.",
  resolved: "A ticket we resolved before, and what resolved it.",
  fact: "A fact in the panel beside this plan, computed in code from the tools.",
  url: "A vendor documentation page found by web search on the allowed list. We do not hold the page, so nothing from it is ever shown as a command.",
};

function citation(c) {
  const body = [
    escapeHtml(CITATION_KINDS[c.kind] || "Evidence we hold."),
    `<div class="small mono muted" style="margin-top: var(--space-1)">${escapeHtml(c.ref)}</div>`,
    c.kind === "url" && c.url
      ? `<div style="margin-top: var(--space-1)"><a href="${escapeHtml(c.url)}" target="_blank" rel="noopener">${escapeHtml(c.url)}</a></div>`
      : "",
  ].join("");
  return `<details class="cite"><summary>${escapeHtml(c.label || c.ref)}</summary><div class="body">${body}</div></details>`;
}

function stepText(s) {
  if (s.review.status === "edited") {
    return `<s class="muted">${escapeHtml(s.text)}</s><br><strong>${escapeHtml(s.review.text)}</strong>`;
  }
  if (s.review.status === "rejected") return `<s>${escapeHtml(s.text)}</s>`;
  return escapeHtml(s.text);
}

function planStep(s, plan, editable) {
  return `<li class="${escapeHtml(s.review.status)}"><div>
    <div class="step-text">${stepText(s)}</div>
    ${
      s.command
        ? `<pre>${escapeHtml(s.command)}</pre><div class="small muted">Taken word for word from ${escapeHtml(s.command_source)}. Nothing here runs it.</div>`
        : ""
    }
    <div class="step-meta">
      ${stateChip("review", s.review.status)}
      ${toneChip(s.command ? "accent" : "neutral", s.execution)}
      ${s.citations.map(citation).join("")}
    </div>
    ${
      s.review.by
        ? `<div class="small muted">${escapeHtml(stateLabel("review", s.review.status))} by ${escapeHtml(s.review.by)} ${escapeHtml(timeWords(s.review.at))}</div>`
        : ""
    }
    ${
      editable
        ? `<div class="inline" style="margin-top: var(--space-2)">
            <button class="action" type="button" data-review="accepted" data-step="${escapeHtml(s.id)}" data-version="${plan.version}">accept</button>
            <button class="action" type="button" data-review="edited" data-step="${escapeHtml(s.id)}" data-version="${plan.version}">edit</button>
            <button class="action" type="button" data-review="rejected" data-step="${escapeHtml(s.id)}" data-version="${plan.version}">reject</button>
          </div>`
        : ""
    }
  </div></li>`;
}

function planProvenance(p) {
  const bits = [
    `Version ${p.version}, by ${p.author}, ${timeWords(p.generated_at)}`,
    `${plural(p.steps.length, "step")} kept`,
    `${plural(p.citations, "citation")}`,
  ];
  if (p.dropped) bits.push(`${plural(p.dropped, "step")} dropped for citing nothing we hold`);
  bits.push(
    `read ${plural(p.evidence.counts.ticket, "resolved ticket")}, ${plural(p.evidence.counts.fact, "fact")} and ${plural(p.evidence.counts.resolved_step, "step")} that resolved a similar ticket`,
  );
  bits.push(`${plural(p.evidence.queries.length, "web search", "web searches")}`);
  bits.push(`took ${duration(p.generation_ms)}`);
  return bits.join(" · ");
}

function planBlock(p, data, editable) {
  const outcome =
    p.outcome && p.outcome.at
      ? `<div class="notice good small">What resolved it, recorded by ${escapeHtml(p.outcome.by)} ${escapeHtml(timeWords(p.outcome.at))}: ${
          p.outcome.none ? "no step here resolved it" : `steps ${escapeHtml((p.outcome.resolved_steps || []).join(", "))}`
        }${p.outcome.note ? ` · ${escapeHtml(p.outcome.note)}` : ""}</div>`
      : "";
  const problems = p.problems.length
    ? `<div class="notice small"><strong>Worth knowing about this plan:</strong><ul>${p.problems.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul></div>`
    : "";
  const steps = p.steps.length
    ? `<ol class="checklist">${p.steps.map((s) => planStep(s, p, editable)).join("")}</ol>`
    : '<div class="notice small">No step survived: everything the model wrote cited something we do not hold, so none of it is shown.</div>';
  return `<div class="small muted">${escapeHtml(planProvenance(p))}</div>
    ${outcome}${problems}${steps}`;
}

function renderPlans(data) {
  const plans = data.plans;
  /* With one version the provenance line below already says which; with more,
     the header says that what follows is the latest of them. */
  planSub.textContent = !plans.length
    ? "No plan yet."
    : plans.length === 1
      ? `${plural(plans[0].steps.length, "step")}.`
      : `${plural(plans[0].steps.length, "step")}, the latest of ${plural(
          plans.length,
          "version"
        )}.`;
  setText("plan-count", plans.length ? String(plans[0].steps.length) : "");
  copyButton.hidden = !plans.length || !plans[0].steps.length;
  generateButton.textContent = plans.length ? "Generate a new one" : "Generate one";
  if (!plans.length) {
    planBody.innerHTML = nothingHere(
      "No plan has been generated for this ticket yet. Every step will cite its evidence, and a step that cites nothing we hold is dropped before anybody sees it.",
      { text: "Generate one once the machine and the readings beside it look right." },
    );
    return;
  }
  const editable = !data.ticket.closed_at;
  const older = plans.slice(1);
  planBody.innerHTML =
    planBlock(plans[0], data, editable) +
    (older.length
      ? `<details class="detail-box" style="margin-top: var(--space-4)">
           <summary>${plural(older.length, "earlier version")} — kept, never overwritten</summary>
           <div>${older.map((p) => planBlock(p, data, false)).join('<hr style="border: none; border-top: 1px solid var(--line); margin: var(--space-4) 0">')}</div>
         </details>`
      : "");
  for (const button of planBody.querySelectorAll("button[data-review]")) {
    button.addEventListener("click", () => reviewStep(Number(button.dataset.version), button.dataset.step, button.dataset.review));
  }
}

async function reviewStep(version, stepId, status) {
  let text = null;
  if (status === "edited") {
    text = await ask({
      title: "How should this step read?",
      body: "The original wording is kept beside yours, with your name and the time.",
      reason: {
        label: "The step as it should read",
        required: true,
        missing: "An edited step needs its new wording.",
      },
      confirm: "Save the edit",
    });
    if (text === null) return;
  }
  try {
    await api("POST", `/tickets/workspaces/${current.id}/plans/${version}/steps/${encodeURIComponent(stepId)}/review`, {
      status,
      text,
    });
    await showWorkspace(current.id);
  } catch (error) {
    fail(error);
  }
}

/* --- copying the plan as a note -----------------------------------------------------------
 *
 * The one primary action on this page, and it writes nothing anywhere: it puts
 * text on the clipboard. Pasting it into Autotask is a person's keystroke, which
 * is what keeps Phase 1 read-only to every vendor.
 */

function noteText(data) {
  const plan = data.plans[0];
  const t = data.ticket;
  const lines = [
    `${t.ticket_number || ""} — plan from Desk`,
    `Version ${plan.version}, generated ${timeWords(plan.generated_at, { time: true })} by ${plan.author}.`,
    "Every step cites evidence we hold. Nothing below has been executed.",
    "",
  ];
  let number = 0;
  for (const s of plan.steps) {
    if (s.review.status === "rejected") continue;
    number += 1;
    lines.push(`${number}. ${s.review.status === "edited" ? s.review.text : s.text}`);
    if (s.citations.length) lines.push(`   Cites: ${s.citations.map((c) => c.label || c.ref).join("; ")}`);
    if (s.command) {
      lines.push(`   Command, from ${s.command_source}, not executed:`);
      lines.push(`   ${s.command}`);
    }
    lines.push("");
  }
  const rejected = plan.steps.filter((s) => s.review.status === "rejected").length;
  if (rejected) lines.push(`${plural(rejected, "step")} rejected by an engineer and left out of this note.`);
  return lines.join("\n");
}

copyButton.addEventListener("click", async () => {
  if (!current || !current.plans.length) return;
  try {
    await navigator.clipboard.writeText(noteText(current));
    copyButton.textContent = "Copied — paste it into Autotask";
    window.setTimeout(() => (copyButton.textContent = "Copy as Autotask note"), 2500);
  } catch (error) {
    fail(new Error("The browser refused the clipboard. Select the steps above and copy them by hand."));
  }
});

/* --- while a plan is being written ----------------------------------------------------------
 *
 * Generating a plan is one request that reports nothing until it lands, and it
 * takes the better part of a minute: the evidence reads, then the model's web
 * searches, then the writing. So the page names the stages, in order, and marks
 * where a run of typical length would be by now — which is an estimate and says
 * so, rather than a progress bar pretending to measure something.
 */

const PLAN_STAGES = [
  { after: 0, text: "Reading the ticket and the facts beside it" },
  { after: 8, text: "Searching the tickets we have resolved for the same words" },
  { after: 25, text: "Searching vendor documentation on the allowed list" },
  { after: 55, text: "Writing the plan, step by step" },
  { after: 100, text: "Checking every step cites something we hold, and dropping the ones that do not" },
];

function renderStages(startedAt) {
  const elapsed = Math.round((Date.now() - startedAt) / 1000);
  let now = 0;
  PLAN_STAGES.forEach((stage, index) => {
    if (elapsed >= stage.after) now = index;
  });
  planBody.innerHTML = `<div class="small">Writing a plan. This usually takes between 45 seconds and two minutes; most of it is the model's web searches.</div>
    <ol class="stages">${PLAN_STAGES.map((stage, index) => {
      const cls = index < now ? "done" : index === now ? "now" : "";
      const mark = index < now ? "✓" : index === now ? "▸" : "·";
      return `<li class="${cls}"><span class="mark" aria-hidden="true">${mark}</span>${escapeHtml(stage.text)}</li>`;
    }).join("")}</ol>
    <div class="small muted" style="margin-top: var(--space-3)">${elapsed} seconds so far. The stages are what the run does, in order; which one it is in is an estimate from how long it has been, because the generation is one request and says nothing until it finishes.</div>`;
}

generateButton.addEventListener("click", async () => {
  if (!current) return;
  generateButton.disabled = true;
  copyButton.hidden = true;
  planSub.textContent = "writing…";
  const startedAt = Date.now();
  renderStages(startedAt);
  stageTimer = window.setInterval(() => renderStages(startedAt), 1000);
  try {
    await api("POST", `/tickets/workspaces/${current.id}/plans`);
    await showWorkspace(current.id);
  } catch (error) {
    fail(error);
    planBody.innerHTML = "";
  } finally {
    window.clearInterval(stageTimer);
    stageTimer = null;
    generateButton.disabled = false;
  }
});


/* --- the tabs, the menu and the panel -----------------------------------------------------
 *
 * One page shows one thing. The readings, the plan and the record of who did
 * what are here, a tab each, rather than stacked under the thing an engineer
 * came to read — and the readings are also a panel over the page, for the
 * reader who wants them without leaving what they were looking at.
 */

const TABS = ["overview", "readings", "plan", "history"];

function showTab(name) {
  const wanted = TABS.includes(name) ? name : "overview";
  for (const tab of TABS) {
    const panel = document.getElementById(`tab-${tab}`);
    const button = document.getElementById(`tab-button-${tab}`);
    if (panel) panel.hidden = tab !== wanted;
    if (button) button.setAttribute("aria-selected", String(tab === wanted));
  }
  /* Which tab you are on is in the address bar, so a link to this ticket's plan
     is a link to this ticket's plan, and Back steps out of it. `replaceState`
     rather than `push`, because nobody wants to press Back through four tabs to
     leave the page. */
  const url = new URL(window.location.href);
  if (wanted === "overview") url.searchParams.delete("tab");
  else url.searchParams.set("tab", wanted);
  window.history.replaceState({}, "", url);
}

for (const button of document.querySelectorAll("#ticket-tabs [data-tab]")) {
  button.addEventListener("click", () => showTab(button.dataset.tab));
}

/* The secondary actions, behind one button. Closes on Escape, on a click
   outside it and on choosing something, like every other menu anybody has
   used. */
const moreButton = document.getElementById("more-button");
const moreList = document.getElementById("more-list");

function closeMore() {
  moreList.hidden = true;
  moreButton.setAttribute("aria-expanded", "false");
}

moreButton.addEventListener("click", (event) => {
  event.stopPropagation();
  const open = moreList.hidden;
  moreList.hidden = !open;
  moreButton.setAttribute("aria-expanded", String(open));
  if (open) loadStartable();
});
moreList.addEventListener("click", (event) => {
  // The workflow items act on their own; the rest of the menu closes on a click.
  if (!event.target.closest("#start-workflow")) closeMore();
});

/* --- start a workflow from this ticket ------------------------------------------------------ */

/* Staff only (the template leaves the group out for anybody else, and the
   endpoints refuse them). The list is the workflows that are on for this
   client and may be started by hand, read when the menu opens so a switch
   thrown a minute ago shows. Choosing one drafts a run — with this ticket
   attached and its inputs filled from the ticket where the workflow says
   how — and opens the run page. Nothing executes: the dry run, the
   submission and the approval are all still ahead of it. */
const startWorkflow = document.getElementById("start-workflow");
let startableLoaded = false;

async function loadStartable() {
  if (!startWorkflow || startableLoaded) return;
  const items = document.getElementById("start-workflow-items");
  items.innerHTML = '<span class="menu-note">loading…</span>';
  try {
    const data = await api("GET", "/automation/startable");
    startableLoaded = true;
    if (data.halted) {
      items.innerHTML = '<span class="menu-note">Automation is halted for this client.</span>';
      return;
    }
    if (!data.workflows.length) {
      items.innerHTML = '<span class="menu-note">None is on for this client. Turn one on from Automations.</span>';
      return;
    }
    items.innerHTML = data.workflows
      .map(
        (w) =>
          `<button type="button" role="menuitem" data-workflow="${escapeHtml(w.name)}" title="${escapeHtml(w.description)}">${escapeHtml(w.title)}</button>`,
      )
      .join("");
    for (const button of items.querySelectorAll("button[data-workflow]")) {
      button.addEventListener("click", () => {
        closeMore();
        startFromTicket(data.workflows.find((w) => w.name === button.dataset.workflow));
      });
    }
  } catch (error) {
    items.innerHTML = '<span class="menu-note">Could not read the workflows.</span>';
    startableLoaded = false;
    fail(error);
  }
}

async function startFromTicket(workflow) {
  if (!current || !workflow) return;
  const ticket = current.ticket;
  const filled = workflow.from_ticket.length
    ? ` Its ${workflow.from_ticket.map((s) => s.replace("ticket.", "")).join(", ")} come from this ticket.`
    : "";
  const sure = await ask({
    title: `Start ${workflow.title} for ticket ${ticket.ticket_number}?`,
    body: `It drafts a run with this ticket attached and opens it.${filled} Nothing runs until the dry run has passed, the client's authoriser has said yes and a second engineer has recorded that.`,
    confirm: "Draft the run",
  });
  if (sure === null) return;
  try {
    const run = await api("POST", "/automation/runs", {
      workflow: workflow.name,
      ticket: {
        number: ticket.ticket_number,
        title: ticket.title || null,
        contact: ticket.contact || null,
        company: ticket.site || null,
        site: ticket.site_slug || null,
      },
    });
    window.location.href = `/automations/${run.id}`;
  } catch (error) {
    fail(error);
  }
}
document.addEventListener("click", (event) => {
  if (!moreList.hidden && !moreList.contains(event.target) && event.target !== moreButton) closeMore();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeMore();
});

/* The readings as a dialog over the page: focus goes into it, Esc and the scrim
   close it, and the keyboard goes back to the link that opened it. */
const readingsClose = document.getElementById("readings-close");
let openedReadingsFrom = null;

function showReadings(open) {
  readingsPanel.hidden = !open;
  readingsScrim.hidden = !open;
  if (open) {
    openedReadingsFrom = document.activeElement;
    readingsClose.focus();
  } else if (openedReadingsFrom) {
    openedReadingsFrom.focus();
    openedReadingsFrom = null;
  }
}

readingsOpen.addEventListener("click", () => showReadings(true));
readingsClose.addEventListener("click", () => showReadings(false));
readingsScrim.addEventListener("click", () => showReadings(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !readingsPanel.hidden) showReadings(false);
});

/** Who did what to this ticket's suggestion, newest last. The tab is empty for
 *  a franchisee, who has no suggestion to have reviewed. */
function renderHistory(data) {
  const rows = (data && data.history) || [];
  if (!rows.length) {
    historyBody.innerHTML = nothingHere("Nothing on this ticket has been reviewed yet.");
    return;
  }
  historyBody.innerHTML = `<div class="list">${rows
    .map(
      (row) => `<div class="list-row">
        <div>
          <div class="what">${escapeHtml(FIELD_WORDS[row.field] || row.field)} — ${escapeHtml(
            VERDICT_WORDS[row.verdict] || row.verdict
          )}${row.corrected_value ? ` to ${escapeHtml(row.corrected_value)}` : ""}</div>
          <div class="meta">${escapeHtml(row.by || "somebody")} · ${when(row.at)}</div>
        </div>
      </div>`
    )
    .join("")}</div>`;
}

/** The words a field and a verdict are written in, for the history. The card
 *  itself labels them where they are answered; this is the one place they are
 *  read back as a list. */
const FIELD_WORDS = {
  issue_type: "Issue type",
  origin: "Raised by",
  urgency: "Urgency",
  repeat: "Repeat of",
  machine: "Which machine",
  next_step: "The suggested next step",
  card: "The card overall",
};

const VERDICT_WORDS = {
  correct: "marked correct",
  changed: "changed",
  wrong: "marked wrong",
  useful: "marked useful",
  not_useful: "marked not useful",
};

/* --- what Desk found on this ticket -----------------------------------------------------
 *
 * Inputiv staff only; the page asks for nothing unless the session is staff.
 *
 * The card leads with the estate — the machine, what the tools found, what to
 * do about it — because Autotask's own intelligence already suggests a type, a
 * priority and a queue from inside Autotask, and repeating that is worth
 * little. The classification sits at the bottom where it can be confirmed in
 * two clicks.
 *
 * Every row is answerable and every answer is a row in our database. Nothing
 * here writes to Autotask: Copy puts text on the clipboard and a person pastes
 * it.
 */

const IS_STAFF = String(DESK.role || "").startsWith("inputiv");

const suggestionCard = document.getElementById("suggestions");
const suggestionCount = document.getElementById("suggestion-count");
const suggestionChecked = document.getElementById("suggestion-checked");
const suggestionCopy = document.getElementById("suggestion-copy");
const suggestionFoot = document.getElementById("suggestion-foot");
const suggestionEmpty = document.getElementById("suggestion-empty");
const machineRow = document.getElementById("machine-row");
const allCorrectHint = document.getElementById("all-correct-hint");
const findingsSection = document.getElementById("findings-section");
const findingsBox = document.getElementById("findings");
const findingsMore = document.getElementById("findings-more");
const notAssessedLine = document.getElementById("not-assessed");
const nextStepSection = document.getElementById("next-step-section");
const nextStepBox = document.getElementById("next-step");
const classificationSection = document.getElementById("classification-section");
const allCorrect = document.getElementById("all-correct");
const classificationBox = document.getElementById("classification");
const blindSection = document.getElementById("blind-section");
const blindQuestions = document.getElementById("blind-questions");
const cardVerdictWords = document.getElementById("card-verdict");

let suggestion = null;
let showAllFindings = false;

/* The card does not fold any more: there is no card. What was behind the
   chevron is the page, and what used to be stacked under it is a tab. */

function verdictButtons(field, review, words, kind = "usefulness") {
  const yes = kind === "usefulness" ? "useful" : "correct";
  const no = kind === "usefulness" ? "not_useful" : "wrong";
  if (review) {
    const good = review.verdict === yes;
    const known = review.verdict === yes || review.verdict === no;
    /* A verdict this page does not know is shown as itself rather than being
       drawn as the opposite of the one it knows. */
    const text = known ? (good ? words[0] : words[1]) : review.verdict;
    return `<span class="answered ${good ? "yes" : known ? "no" : "changed"}">${
      good ? "✓" : "✕"
    } Marked ${escapeHtml(text.toLowerCase())}</span>`;
  }
  return `<span class="actions">
    <button type="button" class="action" data-verdict="${escapeHtml(yes)}" data-field="${escapeHtml(
      field
    )}">${escapeHtml(words[0])}</button>
    <button type="button" class="action" data-verdict="${escapeHtml(
      no
    )}" data-field="${escapeHtml(field)}">${escapeHtml(words[1])}</button>
  </span>`;
}

/** One finding: a dot, a sentence, and the tool and date in a muted line
 *  under it. The qualifier that makes the sentence true — "expected while
 *  offline" — stays in that line, because it is the thing that stops somebody
 *  chasing a fact that cannot be true. */
function findingRow(one) {
  const links = (one.tickets || [])
    .map(
      (ticket) =>
        `<a href="/tickets/${encodeURIComponent(ticket.number)}">${escapeHtml(ticket.number)}</a>`
    )
    .join(", ");
  /* The tool, and one thing the sentence has not already said: when it was
     read, or the qualifier that makes it make sense, or the tickets it is
     about. Never the source again and never the sentence again — "NinjaOne ·
     NinjaOne has it down as offline" is two thirds of a line wasted. */
  const beneath = [escapeHtml(one.source), links || (one.detail ? escapeHtml(one.detail) : null)]
    .filter(Boolean)
    .join(" · ");
  return `<div class="finding" data-finding="${escapeHtml(one.kind)}">
    <span class="dot ${TONE_DOTS[one.tone] || "neutral"}"></span>
    <div class="what">
      <div>${escapeHtml(one.text)}</div>
      <div class="meta">${beneath}</div>
    </div>
    ${verdictButtons(`finding:${one.kind}`, one.review, ["Useful", "Not useful"])}
  </div>`;
}

/** A finding's tone, as one of the four dots. `warning` is the only one that
 *  earns a colour; everything else is the neutral dot and its own words. */
const TONE_DOTS = { warning: "warn", repeat: "neutral", info: "neutral" };

function drawFindings(data) {
  const all = data.findings || [];
  findingsSection.hidden = !all.length;
  const limit = showAllFindings ? all.length : data.max_shown || 4;
  findingsBox.innerHTML = all
    .slice(0, limit)
    .map(findingRow)
    .join("");
  const rest = all.length - limit;
  findingsMore.hidden = rest <= 0 && !showAllFindings;
  findingsMore.textContent = showAllFindings ? "Show fewer" : `Show ${rest} more`;
  notAssessedLine.textContent = data.not_assessed || "";
}

/** "About WS-LT-07 · named in the ticket · confident · Right · Change".
 *
 *  One line rather than a section: which machine, how it was found, how sure,
 *  and the two things an engineer can say about it. Both are the verdicts they
 *  always were — Change records that the machine is wrong and opens the
 *  chooser, which is what Reject did. */
function drawMachine(data) {
  const machine = data.machine || {};
  machineRow.hidden = !machine.device;
  if (!machine.device) return;
  const said = machine.review;
  const answered = said
    ? `<span class="answered ${said.verdict === "correct" ? "yes" : "no"}">${
        said.verdict === "correct" ? "✓ Accepted" : "✕ Not this machine"
      }</span>`
    : `<span class="actions">
        <button type="button" class="action" data-verdict="correct" data-field="machine">Right</button>
        <button type="button" class="action" data-verdict="wrong" data-field="machine">Change</button>
      </span>`;
  machineRow.innerHTML = `About <strong>${escapeHtml(machine.device)}</strong>
    <span class="muted">· ${escapeHtml(stateLabel("resolution", machine.method))}${
      machine.confidence ? ` · ${escapeHtml(stateLabel("confidence", machine.confidence))}` : ""
    } · </span>${answered}`;
}

function drawNextStep(data) {
  const step = data.next_step;
  nextStepSection.hidden = !step;
  if (!step) return;
  nextStepBox.innerHTML = `<div class="step">
    <strong class="what">${escapeHtml(step.text)}</strong>
    ${verdictButtons("next_step", step.review, ["Useful", "Not useful"])}
  </div>`;
}

/** One classification line: the value, its state, and what to do about it. */
/** How each field reads inside the sentence. The field's own value goes in
 *  the slot, so "A Workstation ticket, raised by a tool, normal urgency, a
 *  repeat of T20260814.0031." is one sentence an engineer reads rather than
 *  four rows they parse. */
const SENTENCE = {
  issue_type: (value) => [`A `, value, ` ticket`],
  origin: (value) => [`raised by `, value],
  urgency: (value) => [``, value, ` urgency`],
  repeat: (value) => [`a repeat of `, value],
};

/** One part of the sentence: the words around it, and the value itself with a
 *  dashed underline that opens the chooser. A changed value keeps what the
 *  agent said beside what it should have been, struck through, because the
 *  correction is the thing worth more than the agreement. */
function sentencePart(line) {
  const said = escapeHtml(line.reads || line.value || "");
  const review = line.review;
  let inner = said;
  if (review && review.verdict === "changed") {
    inner = `<span class="was">${said}</span>${escapeHtml(review.corrected_value || "")}`;
  } else if (review && review.verdict === "wrong") {
    inner = `<span class="was">${said}</span>`;
  }
  const value = `<button type="button" class="editable" data-reject="${escapeHtml(
    line.field
  )}" title="Click to change it">${inner}</button>`;
  const shape = SENTENCE[line.field] || ((v) => [`${line.label}: `, v]);
  const [before, , after] = [...shape(""), "", ""];
  /* "(changed)" is worth saying because the sentence now reads differently
     from what the agent said. "Confirmed" changes nothing about the sentence,
     so it is not written on it. */
  const note =
    review && review.verdict === "changed"
      ? ' <span class="note">(changed)</span>'
      : review && review.verdict === "wrong"
        ? ' <span class="note">(wrong)</span>'
        : "";
  return `${escapeHtml(before)}${value}${escapeHtml(after || "")}${note}`;
}

function drawClassification(data) {
  const lines = data.classification || [];
  classificationSection.hidden = !lines.length;
  const parts = lines.map(sentencePart);
  if (parts.length) {
    /* Whichever field comes first starts the sentence, so the capital belongs
       to the sentence rather than to `issue_type`. */
    parts[0] = parts[0].replace(/^([a-z])/, (letter) => letter.toUpperCase());
  }
  classificationBox.innerHTML = parts.length ? `${parts.join(", ")}.` : "";
  /* Offered only while something is still waiting, and gone once nothing is:
     a button that does nothing is a button people press to find out. */
  allCorrect.hidden = !data.to_review;
  allCorrectHint.hidden = !data.to_review;
}

function drawBlind(data) {
  const blind = data.blind;
  blindSection.hidden = !blind;
  if (!blind) return;
  document.getElementById("blind-title").textContent = blind.title;
  blindQuestions.innerHTML = `${blind.questions
    .map((question) => {
      const choices =
        question.choices.length <= 4
          ? `<div class="answer-row">${question.choices
              .map(
                (choice) =>
                  `<button type="button" class="btn secondary" data-blind="${escapeHtml(
                    question.field
                  )}" data-value="${escapeHtml(choice.value)}">${escapeHtml(choice.label)}</button>`
              )
              .join("")}</div>`
          : `<label class="field">
              <select aria-label="${escapeHtml(question.text)}"
                      data-blind-select="${escapeHtml(question.field)}">
                <option value="">Choose one</option>
                ${question.choices
                  .map(
                    (c) =>
                      `<option value="${escapeHtml(c.value)}">${escapeHtml(c.label)}</option>`
                  )
                  .join("")}
              </select></label>`;
      return `<div class="blind-question"><strong>${escapeHtml(
        question.text
      )}</strong>${choices}</div>`;
    })
    .join("")}
    <div class="filter-bar" style="margin-top: var(--space-3)">
      <button type="button" class="btn primary" id="blind-send" disabled>Answer and see the rest</button>
    </div>`;

  const picked = {};
  const send = document.getElementById("blind-send");
  const ready = () => {
    send.disabled = !(picked.origin && picked.issue_type);
  };
  for (const button of blindQuestions.querySelectorAll("button[data-blind]")) {
    button.addEventListener("click", () => {
      picked[button.dataset.blind] = button.dataset.value;
      for (const other of blindQuestions.querySelectorAll(
        `button[data-blind="${CSS.escape(button.dataset.blind)}"]`
      )) {
        other.setAttribute("aria-pressed", String(other === button));
      }
      ready();
    });
  }
  for (const select of blindQuestions.querySelectorAll("select[data-blind-select]")) {
    select.addEventListener("change", () => {
      picked[select.dataset.blindSelect] = select.value || undefined;
      ready();
    });
  }
  send.addEventListener("click", async () => {
    send.disabled = true;
    try {
      draw(
        await api("POST", `/autopilot/suggestions/${data.suggestion_id}/blind`, {
          origin: picked.origin,
          issue_type: picked.issue_type,
        })
      );
    } catch (error) {
      fail(error);
      send.disabled = false;
    }
  });
}

/* --- drawing the whole card -------------------------------------------------------------- */

function draw(data) {
  suggestion = data;
  suggestionCard.hidden = false;
  suggestionEmpty.hidden = true;
  suggestionFoot.hidden = false;
  suggestionCopy.hidden = false;

  if (data.live === false) {
    drawEmpty(data.detail);
    return;
  }
  if (data.quiet && !(data.findings || []).length) {
    drawEmpty(
      data.seen === false
        ? "No suggestions for this ticket yet."
        : "Nothing found and no confident suggestion for this ticket."
    );
    return;
  }

  /* The required lines only — the machine and the ticket information. A
     finding's thumbs are worth having and are not what this counts, so the
     number here is the number of ✓ Accept buttons somebody can still see. */
  suggestionCount.textContent = data.to_review
    ? `${data.to_review} to review`
    : data.review_required
      ? "all reviewed"
      : "nothing to confirm";
  const checked = data.checked || {};
  suggestionChecked.innerHTML = checked.tools
    ? `${escapeHtml(plural(checked.tools, "tool"))} checked ${escapeHtml(
        timeWords(checked.as_of)
      )} · <button type="button" class="action" id="suggestion-refresh">Refresh</button>`
    : "";
  const refresh = document.getElementById("suggestion-refresh");
  if (refresh) refresh.addEventListener("click", () => loadSuggestions(TICKET_REF));

  renderHistory(data);
  drawMachine(data);
  drawFindings(data);
  drawNextStep(data);
  drawClassification(data);
  drawBlind(data);
  drawFooter(data);
  wireCard();
}

function drawEmpty(message) {
  suggestionCount.textContent = "";
  suggestionChecked.textContent = "";
  suggestionCopy.hidden = true;
  suggestionFoot.hidden = true;
  for (const section of [findingsSection, nextStepSection, classificationSection, blindSection]) {
    section.hidden = true;
  }
  suggestionEmpty.hidden = false;
  suggestionEmpty.innerHTML = `<p class="muted">${escapeHtml(message)}</p>`;
}

function drawFooter(data) {
  const review = data.card_review;
  cardVerdictWords.textContent = review
    ? `${review.verdict === "useful" ? "Marked useful" : "Marked not useful"} by ${review.by}`
    : "Useful overall?";
  document.getElementById("card-up").setAttribute("aria-pressed", String(review?.verdict === "useful"));
  document
    .getElementById("card-down")
    .setAttribute("aria-pressed", String(review?.verdict === "not_useful"));
  const actioned = data.last_actioned;
  document.getElementById("suggestion-actioned").textContent = actioned
    ? `Last actioned by ${actioned.by}, ${timeWords(actioned.at, {
        time: true,
      })} · Nothing here is written to Autotask`
    : "Nothing here is written to Autotask";
}

/* --- answering ---------------------------------------------------------------------------- */

async function send(field, verdict, correctedValue) {
  if (!suggestion || !suggestion.suggestion_id) return;
  try {
    draw(
      await api("POST", `/autopilot/suggestions/${suggestion.suggestion_id}/review`, {
        field,
        verdict,
        corrected_value: correctedValue,
      })
    );
  } catch (error) {
    fail(error);
  }
}

function wireCard() {
  for (const button of suggestionCard.querySelectorAll("button[data-verdict]")) {
    button.addEventListener("click", () => send(button.dataset.field, button.dataset.verdict, null));
  }
  for (const button of suggestionCard.querySelectorAll("button[data-accept]")) {
    button.addEventListener("click", () => send(button.dataset.accept, "correct", null));
  }
  for (const button of suggestionCard.querySelectorAll("button[data-reject]")) {
    button.addEventListener("click", () => openChange(button.dataset.reject));
  }
  for (const button of suggestionCard.querySelectorAll('button[data-field="machine"]')) {
    if (button.dataset.verdict !== "wrong") continue;
    button.addEventListener("click", () => openChange("machine"), { once: true });
  }
}

/** Reject on a classification opens what it should have been, in place. The
 *  values are this franchisee's own labels: a correction filed under a label
 *  they do not have is not a correction. Reject on the machine opens the
 *  chooser the ticket page already has. */
function openChange(field) {
  if (field === "machine") {
    const chooser = document.getElementById("device-body");
    chooser.hidden = false;
    chooser.scrollIntoView({ block: "nearest" });
    return;
  }
  const line = classificationBox.querySelector(`[data-reject="${CSS.escape(field)}"]`);
  const choices = (suggestion.classification.find((one) => one.field === field) || {}).choices || [];
  if (!line || classificationBox.querySelector(".change-box")) return;
  if (!choices.length) {
    /* Nothing to change a repeat *to*: saying it is not the same issue is the
       whole correction. */
    send(field, "wrong", null);
    return;
  }
  const box = document.createElement("span");
  box.className = "change-box";
  box.innerHTML = `<select aria-label="What it should have been">${choices
    .map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`)
    .join("")}</select>
    <button type="button" class="btn secondary small" data-save>Save</button>
    <button type="button" class="action" data-cancel>Cancel</button>`;
  line.after(box);
  box.querySelector("select").focus();
  box
    .querySelector("[data-save]")
    .addEventListener("click", () => send(field, "changed", box.querySelector("select").value));
  box.querySelector("[data-cancel]").addEventListener("click", () => box.remove());
}

/* --- loading ------------------------------------------------------------------------------ */

/* --- the Desk note ------------------------------------------------------------------------ */

const deskNotes = document.getElementById("desk-notes");

const NOTE_WORDS = {
  built: ["neutral", "Not posted"],
  posted: ["good", "Posted"],
  not_useful: ["warn", "Not useful"],
  post_failed: ["bad", "Post failed"],
};

/* One card per note: a header line, the note as it will appear in Autotask,
   the two buttons, one line of metadata, and the sources behind a closed
   disclosure. */
function drawDeskNotes(data) {
  const notes = data.notes || [];
  deskNotes.hidden = !notes.length;
  if (!notes.length) return;
  deskNotes.innerHTML = notes
    .map((n) => {
      const [tone, word] = NOTE_WORDS[n.state] || ["neutral", n.state];
      const sources = (n.facts || [])
        .map((f) => `<span>${escapeHtml(f.label)} · ${escapeHtml(valueWords(f.value))} <span class="muted">· ${escapeHtml(f.source)}, ${when(f.read_at, { time: true })}</span></span>`)
        .join("");
      const failures = (n.read_failures || [])
        .map((f) => `<span class="bad-text">${escapeHtml(f.what)} · ${escapeHtml(f.source)} did not answer</span>`)
        .join("");
      // `when()` returns markup; the words around it are escaped one by one.
      const meta = [
        escapeHtml(n.workflow_title),
        when(n.built_at),
        n.posted_by ? `posted by ${escapeHtml(n.posted_by)}` : null,
        n.not_useful_by ? `${escapeHtml(n.not_useful_by)}` : null,
        n.post_error ? escapeHtml(n.post_error) : null,
      ].filter(Boolean);
      const actions = n.can.post && !data.halted
        ? `<div class="row note-actions">
            <button type="button" class="btn secondary" data-note-post="${escapeHtml(n.id)}">Post to Autotask</button>
            <button type="button" class="btn secondary" data-note-dismiss="${escapeHtml(n.id)}">Not useful</button>
          </div>`
        : n.can.post && data.halted
          ? `<p class="small muted">Paused</p>`
          : "";
      return `<div class="desk-note">
        <h2 class="section-title">Desk note<span class="aside">${stateWord(tone, word)}</span></h2>
        <pre class="note-body"><strong>${escapeHtml(n.title)}</strong>
${escapeHtml(n.body)}</pre>
        ${actions}
        <p class="small muted">${meta.join(" · ")}</p>
        <details class="small">
          <summary>Sources</summary>
          <div class="facts">${sources}${failures}</div>
        </details>
      </div>`;
    })
    .join("");
  for (const button of deskNotes.querySelectorAll("button[data-note-post]")) {
    button.addEventListener("click", () => actOnNote(button.dataset.notePost, "post"));
  }
  for (const button of deskNotes.querySelectorAll("button[data-note-dismiss]")) {
    button.addEventListener("click", () => actOnNote(button.dataset.noteDismiss, "not-useful"));
  }
}

function valueWords(value) {
  if (value === null || value === undefined) return "not reported";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.map(valueWords).join(", ");
  if (typeof value === "object") {
    return Object.entries(value)
      .map(([k, v]) => `${k.replace(/_/g, " ")} ${valueWords(v)}`)
      .join(", ");
  }
  return String(value);
}

async function actOnNote(noteId, action) {
  if (action === "post") {
    const sure = await ask({
      title: "Post this note to Autotask?",
      body: "Added as an internal note the client cannot see.",
      confirm: "Post",
    });
    if (sure === null) return;
  }
  try {
    await api("POST", `/automation/notes/${encodeURIComponent(noteId)}/${action}`, {});
    await loadDeskNotes(current.ticket.ticket_id);
  } catch (error) {
    fail(error);
  }
}

async function loadDeskNotes(ticketId) {
  if (!ticketId) return;
  try {
    drawDeskNotes(await api("GET", `/automation/notes/for-ticket/${encodeURIComponent(ticketId)}`));
  } catch (error) {
    /* The note is a help, not the ticket: a failure to read it stays off the
       page. A 403 is the ordinary answer for a franchisee user while the
       client's automation.notes.franchisee_can_post setting is off. */
    deskNotes.hidden = true;
  }
}

async function loadSuggestions(ticketId) {
  if (!IS_STAFF || !ticketId) return;
  try {
    draw(await api("GET", `/autopilot/suggestions/ticket/${encodeURIComponent(ticketId)}`));
  } catch (error) {
    /* Our own measurement, not the engineer's problem: the card stays away
       rather than putting an error above the ticket they came to read. */
    suggestionCard.hidden = true;
  }
}

findingsMore.addEventListener("click", () => {
  showAllFindings = !showAllFindings;
  drawFindings(suggestion);
  wireCard();
});
allCorrect.addEventListener("click", async () => {
  if (!suggestion || !suggestion.suggestion_id) return;
  allCorrect.disabled = true;
  try {
    draw(await api("POST", `/autopilot/suggestions/${suggestion.suggestion_id}/all-correct`, {}));
  } catch (error) {
    fail(error);
  } finally {
    allCorrect.disabled = false;
  }
});
document.getElementById("card-up").addEventListener("click", () => send("card", "useful", null));
document.getElementById("card-down").addEventListener("click", () => send("card", "not_useful", null));

suggestionCopy.addEventListener("click", async () => {
  if (!suggestion || !suggestion.note) return;
  try {
    await navigator.clipboard.writeText(suggestion.note);
    suggestionCopy.textContent = "Copied — paste it into the ticket";
    window.setTimeout(() => {
      suggestionCopy.textContent = "Copy to Autotask";
    }, 4000);
  } catch (error) {
    fail(new Error("The clipboard refused the note. Select the card's text and copy it by hand."));
  }
});

renderCrumbs();
showTab(new URLSearchParams(window.location.search).get("tab") || "overview");
openTicket();
renderSteps();

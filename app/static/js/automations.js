/* Automations: the workflows for this client with their switches and modes,
 * the runs, the notes.
 *
 * Reads one endpoint and draws it. The only writes are the switches and the
 * modes, which are small and audited, and drafting a practice run, which
 * creates a drafted run and goes to its page. Nothing here runs a step.
 */

const summary = document.getElementById("summary");
const errorBox = document.getElementById("error");
const haltedBox = document.getElementById("halted");
const workflowsList = document.getElementById("workflows");
const runsList = document.getElementById("runs");
const triggerLog = document.getElementById("trigger-log");
const notesByWorkflow = document.getElementById("notes-by-workflow");
const notesByWeek = document.getElementById("notes-by-week");
const startButton = document.getElementById("start");
const haltButton = document.getElementById("halt");
const clearHaltButton = document.getElementById("clear-halt");
const moreButton = document.getElementById("more-button");
const moreList = document.getElementById("more-list");

let overview = null;

function fail(error, what) {
  errorBox.innerHTML = problem(what || "Something on the Automations page did not work.", error);
  errorBox.hidden = false;
}

/* --- words --------------------------------------------------------------------- */

/* The one word per workflow row. Off is off; a note workflow that is on says
   which way its notes go; anything else that is on is on. */
function workflowState(w) {
  if (!w.allowed_here) return ["neutral", "Not here"];
  if ((w.missing_settings || []).length) return ["warn", "Needs settings"];
  if (!w.enabled) return ["neutral", "Off"];
  if (w.kind === "note") return w.mode.mode === "auto" ? ["warn", "Post automatically"] : ["good", "Review first"];
  return ["good", "On"];
}

/* --- drawing ------------------------------------------------------------------ */

function drawSummary(data) {
  const allowed = data.workflows.filter((w) => w.allowed_here);
  const on = allowed.filter((w) => w.enabled).length;
  /* Not the client: the header prints whose automations these are on
     every page of the product. */
  const parts = [`${on} of ${allowed.length} on`];
  if (data.kill_switch.halted) parts.push("Paused");
  summary.innerHTML = parts.map((p) => `<span>${escapeHtml(p)}</span>`).join("");
  haltedBox.hidden = !data.kill_switch.halted;
  if (data.kill_switch.halted) {
    haltedBox.innerHTML =
      `<strong>Paused by ${escapeHtml(data.kill_switch.by || "somebody")}</strong>` +
      (data.kill_switch.reason ? ` <span class="advice">${escapeHtml(data.kill_switch.reason)}</span>` : "");
  }
  haltButton.hidden = data.kill_switch.halted;
  clearHaltButton.hidden = !data.kill_switch.halted;
}

function drawWorkflows(data) {
  workflowsList.innerHTML = data.workflows
    .map((w) => {
      const [tone, word] = workflowState(w);
      // A note workflow's mode is a value somebody can change, so the word
      // is the control: dashed underline, click to switch.
      const needs = (w.missing_settings || []).length > 0;
      // "Needs settings" is a link to the list of exactly which ones, on the
      // workflow page behind Details.
      const state = needs
        ? `<a class="state-link" href="/automations/workflows/${encodeURIComponent(w.name)}#details" title="${escapeHtml(w.missing_settings.map((m) => m.key).join(", "))}">${stateWord(tone, word)}</a>`
        : w.kind === "note" && w.allowed_here && w.enabled
          ? `<button type="button" class="editable" data-mode-switch="${escapeHtml(w.name)}" data-to="${w.mode.mode === "auto" ? "preview" : "auto"}">${stateWord(tone, word)}</button>`
          : stateWord(tone, word);
      const control = w.allowed_here && !(needs && !w.enabled)
        ? `<button type="button" class="btn secondary" data-switch="${escapeHtml(w.name)}" data-to="${w.enabled ? "false" : "true"}">${w.enabled ? "Turn off" : "Turn on"}</button>`
        : "";
      return `<div class="list-row workflow-row">
        <span>
          <a class="what" href="/automations/workflows/${encodeURIComponent(w.name)}">${escapeHtml(w.title)}</a>
          <span class="meta">${escapeHtml(startsWhen(w.triggers))}</span>
        </span>
        <span class="state-cell">${state}</span>
        <span class="row-action">${control}</span>
      </div>`;
    })
    .join("");
  for (const button of workflowsList.querySelectorAll("button[data-switch]")) {
    button.addEventListener("click", () => setSwitch(button.dataset.switch, button.dataset.to === "true"));
  }
  for (const button of workflowsList.querySelectorAll("button[data-mode-switch]")) {
    button.addEventListener("click", () => setMode(button.dataset.modeSwitch, button.dataset.to));
  }
  const practice = data.workflows.find((w) => w.name === "practice");
  startButton.hidden = !(practice && practice.allowed_here && practice.enabled && !data.kill_switch.halted);
}

function startedLine(run) {
  return run.started_how === "trigger"
    ? `drafted by ${run.trigger_detail || run.started_by}`
    : `started by ${run.started_by}`;
}

function drawRuns(data) {
  setText("runs-count", data.runs.length ? String(data.runs.length) : "");
  if (!data.runs.length) {
    runsList.innerHTML = nothingHere("No runs yet.");
    return;
  }
  runsList.innerHTML = data.runs
    .map((run) => {
      const facts = [
        startedLine(run),
        run.approved_by ? `approved by ${run.approved_by}` : null,
        run.ticket_number ? `ticket ${run.ticket_number}` : null,
      ].filter(Boolean);
      const title = run.inputs && run.inputs.label
        ? `${run.workflow_title} — ${run.inputs.label}`
        : run.workflow_title;
      return `<a class="list-row run-row" href="/automations/${escapeHtml(run.id)}">
        <span>
          <span class="what">${escapeHtml(title)}</span>
          <span class="meta">${facts.map(escapeHtml).join(" · ")} · ${when(run.updated_at)}</span>
        </span>
        <span class="state-cell">${stateWord(run.state.tone, run.state.word, run.failure || "")}</span>
      </a>`;
    })
    .join("");
}

const OUTCOME_WORDS = {
  drafted: ["good", "Drafted"],
  noted: ["good", "Noted"],
  skipped: ["warn", "Skipped"],
  no_match: ["neutral", "No match"],
};

function drawTriggerLog(data) {
  const rows = data.trigger_log || [];
  setText("triggers-count", rows.length ? String(rows.length) : "");
  if (!rows.length) {
    triggerLog.innerHTML = nothingHere("Nothing evaluated yet.");
    return;
  }
  triggerLog.innerHTML = rows
    .map((row) => {
      const [tone, word] = OUTCOME_WORDS[row.outcome] || ["neutral", row.outcome];
      const what = row.ticket_number || row.ticket_id
        ? `${row.ticket_number || row.ticket_id} · ${row.workflow_title}`
        : row.workflow_title;
      const body = row.run_id
        ? `<a class="what" href="/automations/${escapeHtml(row.run_id)}">${escapeHtml(what)}</a>`
        : `<span class="what">${escapeHtml(what)}</span>`;
      return `<div class="list-row run-row">
        <span>
          ${body}
          <span class="meta">${escapeHtml(row.reason)} · ${when(row.evaluated_at, { time: true })}</span>
        </span>
        <span class="state-cell">${stateWord(tone, word)}</span>
      </div>`;
    })
    .join("");
}

/* "2.1 h → 1.4 h": the median hours to close without a note, then with one.
   Either side missing is not enough data. */
function fasterWords(w) {
  const without = w.median_hours_to_close_without_note;
  const withNote = w.median_hours_to_close_with_note;
  if (without === null || without === undefined || withNote === null || withNote === undefined) {
    return "Not enough data";
  }
  return `${without} h → ${withNote} h`;
}

function weekCell(c) {
  if (!c || !c.built) return "—";
  const parts = [`${c.built} built`];
  const posted = (c.posted_by_engineer || 0) + (c.posted_auto || 0);
  if (posted) parts.push(`${posted} posted`);
  if (c.not_useful) parts.push(`${c.not_useful} not useful`);
  return parts.join(", ");
}

async function drawNotes() {
  let report;
  try {
    report = await api("GET", "/automation/notes/report");
  } catch (error) {
    fail(error, "Couldn't load the notes.");
    return;
  }
  const total = report.by_workflow.reduce((n, w) => n + w.built, 0);
  setText("notes-count", total ? String(total) : "");
  notesByWorkflow.innerHTML =
    "<thead><tr><th>Workflow</th><th>Notes</th><th>Posted</th><th>Not useful</th><th>Faster to close?</th></tr></thead>" +
    "<tbody>" +
    report.by_workflow
      .map(
        (w) => `<tr>
          <td><a href="/automations/workflows/${encodeURIComponent(w.workflow)}">${escapeHtml(w.title)}</a></td>
          <td>${w.built}</td>
          <td>${w.posted_by_engineer + w.posted_auto}</td>
          <td>${w.not_useful}</td>
          <td>${escapeHtml(fasterWords(w))}</td>
        </tr>`,
      )
      .join("") +
    "</tbody>";
  const names = report.by_workflow.map((w) => w.workflow);
  notesByWeek.innerHTML = report.weeks.length
    ? `<thead><tr><th>Week of</th>${names.map((n) => `<th>${escapeHtml(report.by_workflow.find((w) => w.workflow === n).title)}</th>`).join("")}</tr></thead>` +
      "<tbody>" +
      report.weeks
        .map((week) => `<tr><td>${escapeHtml(week.week)}</td>${names.map((n) => `<td>${escapeHtml(weekCell(week.workflows[n]))}</td>`).join("")}</tr>`)
        .join("") +
      "</tbody>"
    : `<tbody>${nothingRow(1, "No notes yet.")}</tbody>`;
}

function draw(data) {
  overview = data;
  drawSummary(data);
  drawWorkflows(data);
  drawRuns(data);
  drawTriggerLog(data);
  drawNotes();
}

/* --- the tabs ------------------------------------------------------------------- */

const TABS = ["workflows", "runs", "notes"];

function showTab(name) {
  const wanted = TABS.includes(name) ? name : "workflows";
  for (const tab of TABS) {
    document.getElementById(`tab-${tab}`).hidden = tab !== wanted;
    document.getElementById(`tab-button-${tab}`).setAttribute("aria-selected", String(tab === wanted));
  }
  const url = new URL(window.location.href);
  if (wanted === "workflows") url.searchParams.delete("tab");
  else url.searchParams.set("tab", wanted);
  window.history.replaceState({}, "", url);
}

for (const button of document.querySelectorAll("#automation-tabs [data-tab]")) {
  button.addEventListener("click", () => showTab(button.dataset.tab));
}
showTab(new URL(window.location.href).searchParams.get("tab"));

for (const button of document.querySelectorAll("#notes-tabs [data-notes-tab]")) {
  button.addEventListener("click", () => {
    const wanted = button.dataset.notesTab;
    for (const other of document.querySelectorAll("#notes-tabs [data-notes-tab]")) {
      other.setAttribute("aria-selected", String(other === button));
    }
    document.getElementById("notes-by-workflow-panel").hidden = wanted !== "workflow";
    document.getElementById("notes-by-week-panel").hidden = wanted !== "week";
  });
}

/* --- actions ------------------------------------------------------------------- */

async function load() {
  if (needsClient()) {
    workflowsList.innerHTML = chooseClientNotice();
    summary.innerHTML = "";
    return;
  }
  errorBox.hidden = true;
  try {
    draw(await api("GET", "/automation/overview"));
  } catch (error) {
    fail(error, "Couldn't load the automations for this client.");
  }
}

async function setSwitch(workflowName, enabled) {
  const w = overview.workflows.find((x) => x.name === workflowName);
  if (enabled) {
    const sure = await ask({ title: `Turn on ${w.title}?`, confirm: "Turn on" });
    if (sure === null) return;
  }
  try {
    await api("POST", `/automation/switches/${encodeURIComponent(workflowName)}`, { enabled });
    await load();
  } catch (error) {
    fail(error, `Couldn't change the ${w.title} switch.`);
  }
}

/* Posting automatically records the franchisee's approver the way a run
   approval does: who, and how they agreed. The approver must be on the
   client's list, which the server checks. */
async function setMode(workflowName, mode) {
  const w = overview.workflows.find((x) => x.name === workflowName);
  let body = { mode };
  if (mode === "auto") {
    const who = await ask({
      title: `Post automatically: ${w.title}?`,
      body: "Needs the client's approver on record.",
      reason: { label: "Approver, how agreed", placeholder: "Dana Whitfield, email", required: true, missing: "Name the approver." },
      confirm: "Post automatically",
      tone: "destructive",
    });
    if (who === null) return;
    const [name, method] = String(who).split(",").map((s) => s.trim());
    body = { mode, authoriser_name: name || null, authoriser_method: method || "recorded on the Automations page" };
  } else {
    const sure = await ask({ title: `Review first: ${w.title}?`, confirm: "Review first" });
    if (sure === null) return;
  }
  try {
    await api("POST", `/automation/note-modes/${encodeURIComponent(workflowName)}`, body);
    await load();
  } catch (error) {
    fail(error, `Couldn't change the ${w.title} mode.`);
  }
}

async function setHalted(halted) {
  let reason = null;
  if (halted) {
    reason = await ask({
      title: `Pause all automation for ${overview.client.name}?`,
      reason: { label: "Why", placeholder: "Optional" },
      confirm: "Pause all",
      tone: "destructive",
    });
    if (reason === null) return;
  }
  try {
    await api("POST", "/automation/kill-switch", { halted, reason: reason ? reason.trim() || null : null });
    await load();
  } catch (error) {
    fail(error, "Couldn't pause or resume.");
  }
}

async function startPractice() {
  const label = await ask({
    title: "Start a practice run?",
    reason: { label: "Label", value: "practice" },
    confirm: "Start",
  });
  if (label === null) return;
  try {
    const run = await api("POST", "/automation/runs", {
      workflow: "practice",
      inputs: { label: (label || "practice").trim() || "practice" },
    });
    window.location.href = `/automations/${run.id}`;
  } catch (error) {
    fail(error, "Couldn't start a practice run.");
  }
}

/* --- wiring ------------------------------------------------------------------- */

function showMore(open) {
  moreList.hidden = !open;
  moreButton.setAttribute("aria-expanded", open ? "true" : "false");
}
moreButton.addEventListener("click", () => showMore(moreList.hidden));
document.addEventListener("click", (event) => {
  if (!moreList.hidden && !event.target.closest(".menu")) showMore(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !moreList.hidden) {
    showMore(false);
    moreButton.focus();
  }
});
haltButton.addEventListener("click", () => { showMore(false); setHalted(true); });
clearHaltButton.addEventListener("click", () => { showMore(false); setHalted(false); });
startButton.addEventListener("click", startPractice);

load();

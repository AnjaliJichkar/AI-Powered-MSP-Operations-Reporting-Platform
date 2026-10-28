/* One automation run.
 *
 * Reads `/automation/runs/<id>` and draws it; every button posts to one of the
 * service's actions and redraws from what came back. While the worker has the
 * run — approved, running, or an undo requested — the page polls, so the
 * progress list moves without anything executing in the browser.
 */

const RUN_ID = DESK.runId;
const errorBox = document.getElementById("error");
const noticeBox = document.getElementById("notice");
const titleEl = document.getElementById("title");
const subtitle = document.getElementById("subtitle");
const primary = document.getElementById("primary");
const more = document.getElementById("more");
const moreButton = document.getElementById("more-button");
const moreList = document.getElementById("more-list");
const planSection = document.getElementById("plan-section");
const planList = document.getElementById("plan");
const approvedSection = document.getElementById("approved-section");
const approvedPlan = document.getElementById("approved-plan");
const progressSection = document.getElementById("progress-section");
const progressList = document.getElementById("progress");
const approveSection = document.getElementById("approve-section");
const approveForm = document.getElementById("approve-form");

let view = null;
let pollTimer = null;

function fail(error, what) {
  errorBox.innerHTML = problem(what || "Something on this run did not work.", error);
  errorBox.hidden = false;
}

/* --- words ------------------------------------------------------------------- */

const STEP_WORDS = {
  pending: ["neutral", "Waiting"],
  running: ["accent", "Running"],
  done: ["good", "Done"],
  failed: ["bad", "Failed"],
  undone: ["neutral", "Undone"],
  undo_failed: ["bad", "Undo failed"],
  changed: ["warn", "Left alone"],
};

const STARTED = ["running", "completed", "failed", "partly_completed", "undone"];

function stepState(row) {
  const [tone, word] = STEP_WORDS[row.state] || ["neutral", row.state];
  return stateWord(tone, word, row.state === "changed" ? "" : row.error || "");
}

function value(v) {
  if (v === null || v === undefined) return "nothing";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/* An input's key in words: "fail_times" reads "Fail times". */
function words(key) {
  const t = String(key).replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/* --- drawing ------------------------------------------------------------------- */

function drawTop(data) {
  const run = data.run;
  const label = run.inputs && run.inputs.label ? ` — ${run.inputs.label}` : "";
  titleEl.textContent = `${run.workflow_title}${label}`;
  setText("crumb-title", `${run.workflow_title}${label}`);
  document.title = `${run.workflow_title} · Automations · Desk`;
  const started = run.started_how === "trigger" ? run.trigger_detail || run.started_by : run.started_by;
  const parts = [
    stateWord(run.state.tone, run.state.word),
    `<span>${escapeHtml(started)}</span>`,
    `<span>${when(run.created_at)}</span>`,
    run.ticket_number ? `<span>${escapeHtml(run.ticket_number)}</span>` : "",
  ].filter(Boolean);
  subtitle.innerHTML = parts.map((p) => (p.startsWith("<span") ? p : `<span>${p}</span>`)).join("");

  noticeBox.hidden = true;
  if (run.paused_at) {
    noticeBox.className = "notice";
    noticeBox.innerHTML = `<strong>Paused</strong> <span class="advice">since ${timeWords(run.paused_at)}.</span>`;
    noticeBox.hidden = false;
  } else if (run.failure && run.state.key === "failed") {
    const failed = data.steps.find((s) => s.key === run.current_step);
    const where = failed ? `step ${failed.position}` : run.current_step;
    const why = failed && failed.error ? failed.error : run.failure;
    noticeBox.className = "notice bad";
    noticeBox.innerHTML = `<strong>Stopped at ${escapeHtml(where)}.</strong> <span class="advice">${escapeHtml(why.replace(/\.?$/, "."))}</span>`;
    noticeBox.hidden = false;
  } else if (run.undo_failure) {
    noticeBox.className = "notice bad";
    noticeBox.innerHTML = `<strong>Undo stopped.</strong> <span class="advice">${escapeHtml(run.undo_failure)}</span>`;
    noticeBox.hidden = false;
  }
}

function drawActions(data) {
  const can = data.can;
  const run = data.run;
  const choices = [
    can.dry_run && run.state.key === "drafted" && ["dry_run", "Run dry run"],
    can.submit && ["submit", "Submit"],
    can.approve && ["approve", "Approve"],
    can.resume && ["resume", "Resume"],
    can.resubmit && ["resubmit", "Fresh approval"],
    can.undo && ["undo", "Undo"],
  ].filter(Boolean);
  const [mainKey, mainWord] = choices[0] || [null, null];
  primary.hidden = !mainKey;
  if (mainKey) {
    primary.textContent = mainWord;
    primary.dataset.action = mainKey;
  }
  const rest = [
    can.dry_run && run.state.key !== "drafted" && ["dry_run", "Rerun dry run"],
    can.withdraw && ["withdraw", "Withdraw"],
    can.abandon && ["abandon", "Abandon"],
    can.cancel && ["cancel", "Cancel run"],
  ].filter(Boolean);
  more.hidden = !rest.length;
  moreList.innerHTML = rest
    .map(([key, word]) => `<button type="button" role="menuitem" data-action="${key}">${escapeHtml(word)}</button>`)
    .join("");
  for (const button of moreList.querySelectorAll("button")) {
    button.addEventListener("click", () => {
      showMore(false);
      act(button.dataset.action);
    });
  }
  // The approve form stands where the primary would be: who, how, Approve.
  approveSection.hidden = !can.approve;
  if (can.approve) {
    primary.hidden = true;
    const select = document.getElementById("authoriser");
    select.innerHTML = data.approvers.length
      ? data.approvers.map((a) => `<option value="${escapeHtml(a.name)}">${escapeHtml(a.name)}${a.role ? ` — ${escapeHtml(a.role)}` : ""}</option>`).join("")
      : '<option value="">No approvers listed</option>';
  }
}

/* One line per step: what it will do, "Can't be undone" only where true,
   and the before/after values behind the row's disclosure. */
function valueLines(label, values) {
  const entries = Object.entries(values || {}).filter(([, v]) => v !== null && v !== undefined);
  if (!entries.length) return "";
  return `<div class="meta">${escapeHtml(label)} · ${entries.map(([k, v]) => `${escapeHtml(words(k))} ${escapeHtml(value(v))}`).join(" · ")}</div>`;
}

function planRows(dry, rows) {
  const byKey = {};
  for (const r of rows || []) byKey[r.key] = r;
  return dry.steps
    .map((step, index) => {
      const row = byKey[step.step] || {};
      const changes = (step.plan && step.plan.changes) || [];
      const mark = step.ok
        ? step.reversible ? "" : stateWord("warn", "Can't be undone")
        : stateWord("bad", "Won't run", step.reason || "");
      const line = step.ok ? step.plan.summary : step.reason || "The check did not pass.";
      const inner = changes
        .map((c) => `<div class="meta">${escapeHtml(words(c.what))} · ${escapeHtml(value(c.before))} → ${escapeHtml(value(c.after))}</div>`)
        .join("") + valueLines("Inputs", row.inputs) + valueLines("Outputs", row.outputs);
      const detail = inner ? `<details class="small"><summary>Values</summary>${inner}</details>` : "";
      return `<div class="list-row plan-row light">
        <span>
          <span class="what">${index + 1}. ${escapeHtml(line)}</span>
          ${detail}
        </span>
        <span class="state-cell">${mark}</span>
      </div>`;
    })
    .join("");
}

function drawPlan(data) {
  const run = data.run;
  const dry = run.dry_run;
  const started = STARTED.includes(run.state.key);
  planSection.hidden = started;
  approvedSection.hidden = !started;
  if (!dry) {
    planList.innerHTML = nothingHere("No dry run yet.");
    approvedPlan.innerHTML = "";
    return;
  }
  const rows = planRows(dry, data.steps);
  planList.innerHTML = rows;
  approvedPlan.innerHTML = rows;
}

function drawProgress(data) {
  const run = data.run;
  const started = STARTED.includes(run.state.key);
  progressSection.hidden = !started;
  if (!started) return;
  const done = data.steps.filter((s) => s.state === "done").length;
  const undone = data.steps.filter((s) => s.state === "undone").length;
  setText("progress-sub", run.state.key === "undone" ? `${undone} of ${data.steps.length} undone` : `${done} of ${data.steps.length} done`);
  progressList.innerHTML = data.steps
    .map((row) => {
      const facts = [];
      if (row.finished_at) facts.push(timeWords(row.finished_at, { time: true }));
      if (row.undone_at) facts.push(`undone ${timeWords(row.undone_at, { time: true })}`);
      else if (run.state.key === "undone" && row.state === "done" && !row.reversible) facts.push("can't be undone");
      if (row.error) facts.push(row.error);
      return `<div class="list-row step-row">
        <span>
          <span class="what">${row.position}. ${escapeHtml(row.title)}</span>
          ${facts.length ? `<span class="meta">${facts.map(escapeHtml).join(" · ")}</span>` : ""}
        </span>
        <span class="state-cell">${stepState(row)}</span>
      </div>`;
    })
    .join("");
}

function drawRail(data) {
  const run = data.run;
  const inputs = Object.entries(run.inputs || {}).filter(([, v]) => v !== null && v !== undefined);
  document.getElementById("inputs").innerHTML = inputs.length
    ? inputs.map(([k, v]) => `<div>${escapeHtml(words(k))} · <strong>${escapeHtml(value(v))}</strong></div>`).join("")
    : '<span class="muted">None</span>';

  const approval = document.getElementById("approval");
  if (run.authoriser) {
    let standing = "";
    if (run.state.key === "approved" && run.approval_expires_at) {
      standing = `<div class="muted">Start by ${when(run.approval_expires_at, { time: true })}</div>`;
    } else if (run.state.key === "failed" && run.resume_by) {
      const late = new Date(run.resume_by) < new Date();
      standing = late
        ? `<div class="muted">Lapsed ${when(run.resume_by, { time: true })}</div>`
        : `<div class="muted">Resume by ${when(run.resume_by, { time: true })}</div>`;
    }
    approval.innerHTML =
      `<div>${escapeHtml(run.authoriser.name)} · ${escapeHtml(run.authoriser.method)} · ${when(run.authoriser.at)}</div>` +
      `<div class="muted">Recorded by ${escapeHtml(run.approved_by)}</div>` +
      standing;
  } else if (run.state.key === "awaiting_approval") {
    approval.innerHTML = '<span class="muted">Waiting</span>';
  } else {
    approval.innerHTML = '<span class="muted">Not yet</span>';
  }

  const undo = document.getElementById("undo");
  if (run.state.key === "undone") {
    undo.innerHTML = `<div>Done ${when(run.undone_at)}</div>`;
  } else if (run.undo_requested_at) {
    undo.innerHTML = `<div>In progress</div>`;
  } else if (run.undo_deadline_at) {
    const open = new Date(run.undo_deadline_at) > new Date();
    undo.innerHTML = open
      ? `<div>Until ${when(run.undo_deadline_at, { time: true })}</div>`
      : `<div class="muted">Closed ${when(run.undo_deadline_at, { time: true })}</div>`;
  } else {
    undo.innerHTML = `<span class="muted">Not yet</span>`;
  }
}

function draw(data) {
  view = data;
  drawTop(data);
  drawActions(data);
  drawPlan(data);
  drawProgress(data);
  drawRail(data);
  schedulePoll(data);
}

/* --- polling while the worker has it -------------------------------------------- */

function schedulePoll(data) {
  clearTimeout(pollTimer);
  const busy = ["approved", "running"].includes(data.run.state.key) || data.run.undo_requested_at;
  if (busy) pollTimer = setTimeout(load, 2000);
}

/* --- actions ------------------------------------------------------------------- */

async function load() {
  try {
    draw(await api("GET", `/automation/runs/${encodeURIComponent(RUN_ID)}`));
  } catch (error) {
    fail(error, "Couldn't load this run.");
  }
}

const CONFIRM = {
  submit: { title: "Submit for approval?", confirm: "Submit" },
  undo: {
    title: "Undo this run?",
    body: "Steps that can't be undone stay done.",
    confirm: "Undo",
    tone: "destructive",
  },
  cancel: {
    title: "Cancel this run?",
    reason: { label: "Why", placeholder: "Optional" },
    confirm: "Cancel run",
    tone: "destructive",
  },
  resubmit: { title: "Ask for fresh approval?", confirm: "Ask" },
  abandon: {
    title: "Abandon this run?",
    body: "What it did stays done.",
    reason: { label: "Why", placeholder: "Optional" },
    confirm: "Abandon",
    tone: "destructive",
  },
};

async function act(action) {
  errorBox.hidden = true;
  let body = {};
  if (CONFIRM[action]) {
    const answer = await ask(CONFIRM[action]);
    if (answer === null) return;
    if (typeof answer === "string") body = { reason: answer.trim() || null };
  }
  const path = { dry_run: "dry-run" }[action] || action;
  try {
    draw(await api("POST", `/automation/runs/${encodeURIComponent(RUN_ID)}/${path}`, body));
  } catch (error) {
    fail(error, `Couldn't ${action.replace("_", " ")} this run.`);
  }
}

/* The time the client said yes. It is now unless Change time reveals the
   field; a future time, or one before the dry run, is refused here and
   again by the server. */
const whenField = document.getElementById("when-field");
const whenInput = document.getElementById("authorised-at");
const approveSays = document.getElementById("approve-says");

function localInputValue(date) {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

document.getElementById("change-time").addEventListener("click", (event) => {
  whenField.hidden = false;
  event.target.hidden = true;
  if (!whenInput.value) whenInput.value = localInputValue(new Date());
  whenInput.max = localInputValue(new Date());
  if (view && view.run.dry_run_at) whenInput.min = localInputValue(new Date(view.run.dry_run_at));
  whenInput.focus();
});

function approvalTime() {
  if (whenField.hidden || !whenInput.value) return null;
  const at = new Date(whenInput.value);
  if (Number.isNaN(at.getTime())) return "That is not a time.";
  if (at.getTime() > Date.now() + 60000) return "The approval time is in the future.";
  if (view && view.run.dry_run_at && at < new Date(view.run.dry_run_at)) {
    return "The approval time is before the dry run.";
  }
  return at.toISOString();
}

approveForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  approveSays.hidden = true;
  const name = document.getElementById("authoriser").value;
  const method = document.getElementById("method").value;
  const at = approvalTime();
  if (at && !at.endsWith("Z")) {
    approveSays.textContent = at;
    approveSays.hidden = false;
    whenInput.focus();
    return;
  }
  try {
    draw(
      await api("POST", `/automation/runs/${encodeURIComponent(RUN_ID)}/approve`, {
        authoriser_name: name,
        authoriser_method: method,
        authoriser_at: at,
      }),
    );
  } catch (error) {
    fail(error, "The approval was not recorded.");
  }
});

primary.addEventListener("click", () => act(primary.dataset.action));

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

load();

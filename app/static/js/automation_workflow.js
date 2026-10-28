/* One workflow, as this client has it.
 *
 * Reads `/automation/workflows/<name>` and draws it: what starts it, what it
 * checks, what it writes, its recent notes or runs. The full trigger rules
 * with their switches, the approval rule, the approvers and the settings are
 * behind Details. The workflow's own switch and its mode are on the
 * Automations page.
 */

const NAME = DESK.workflowName;
const errorBox = document.getElementById("error");
const noticeBox = document.getElementById("notice");

let view = null;

function fail(error, what) {
  errorBox.innerHTML = problem(what || "Something on this page did not work.", error);
  errorBox.hidden = false;
}

function hours(h) {
  if (h % 24 === 0) return plural(h / 24, "day");
  return plural(h, "hour");
}

function isNoteKind(data) {
  return data.workflow.kind === "note" || data.workflow.kind === "report";
}

function modeWord(data) {
  const w = data.workflow;
  if (!w.allowed_here) return ["neutral", "Not here"];
  if (!w.enabled) return ["neutral", "Off"];
  if (w.kind === "note" && data.mode) {
    return data.mode.mode === "auto" ? ["warn", "Post automatically"] : ["good", "Review first"];
  }
  return ["good", "On"];
}

const NOTE_WORDS = {
  built: ["neutral", "Not posted"],
  posted: ["good", "Posted"],
  not_useful: ["warn", "Not useful"],
  post_failed: ["bad", "Post failed"],
};

function row(what, state) {
  return `<div class="list-row plan-row light">
    <span><span class="what">${escapeHtml(what)}</span></span>
    <span class="state-cell">${state || ""}</span>
  </div>`;
}

/* --- drawing ------------------------------------------------------------------- */

function drawTop(data) {
  const w = data.workflow;
  document.getElementById("title").textContent = w.title;
  setText("crumb-title", w.title);
  document.title = `${w.title} · Automations · Desk`;
  const [tone, word] = modeWord(data);
  document.getElementById("subtitle").innerHTML =
    `<span>${stateWord(tone, word)}</span><span>version ${w.version}</span>`;
  const link = document.getElementById("runs-link");
  if (w.kind === "note") {
    link.href = "/automations?tab=notes";
    link.textContent = "All notes";
  } else if (w.kind === "report") {
    link.href = `/automations/reports/${encodeURIComponent(w.name)}`;
    link.textContent = "Open report";
  } else {
    link.href = "/automations?tab=runs";
  }
  if (!w.allowed_here) {
    noticeBox.innerHTML = `<strong>Not on this client.</strong> <span class="advice">${escapeHtml(w.not_allowed_because)}</span>`;
    noticeBox.hidden = false;
  }
}

function drawSections(data) {
  const w = data.workflow;
  setText("starts", startsWhen(data.triggers));
  const checks = document.getElementById("checks");
  const writes = document.getElementById("writes");
  if (isNoteKind(data)) {
    checks.innerHTML = data.reads.map((r) => row(r)).join("");
    writes.innerHTML = w.kind === "note"
      ? `<blockquote class="template">${escapeHtml(data.template)}</blockquote>`
      : row(w.writes.charAt(0).toUpperCase() + w.writes.slice(1));
  } else {
    checks.innerHTML = data.steps.map((s) => row(sentence(s.checks))).join("");
    writes.innerHTML = data.steps
      .map((s) => row(s.title, s.reversible ? "" : stateWord("warn", "Can't be undone")))
      .join("");
  }
}

function sentence(text) {
  const t = String(text || "").trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1).replace(/\.?$/, ".") : "";
}

function drawRecent(data) {
  const w = data.workflow;
  const list = document.getElementById("recent");
  if (w.kind === "report") {
    setText("recent-title", "Report");
    list.innerHTML = `<div class="list-row plan-row"><span><a class="what" href="/automations/reports/${encodeURIComponent(w.name)}">Open report</a></span></div>`;
    return;
  }
  if (w.kind === "note") {
    setText("recent-title", "Recent notes");
    const notes = (data.recent_notes || []).slice(0, 5);
    list.innerHTML = notes.length
      ? notes.map((n) => {
          const [tone, word] = NOTE_WORDS[n.state] || ["neutral", n.state];
          return `<a class="list-row run-row" href="/tickets/${encodeURIComponent(n.ticket_number || n.ticket_id)}">
            <span><span class="what">${escapeHtml(n.ticket_number || n.ticket_id)} · ${escapeHtml(n.title)}</span>
            <span class="meta">${when(n.built_at)}${n.posted_by ? ` · ${escapeHtml(n.posted_by)}` : ""}</span></span>
            <span class="state-cell">${stateWord(tone, word)}</span>
          </a>`;
        }).join("")
      : nothingHere("No notes yet.");
    return;
  }
  setText("recent-title", "Recent runs");
  const runs = (data.recent_runs || []).slice(0, 5);
  list.innerHTML = runs.length
    ? runs.map((run) => {
        const title = run.inputs && run.inputs.label ? `${run.workflow_title} — ${run.inputs.label}` : run.workflow_title;
        return `<a class="list-row run-row" href="/automations/${escapeHtml(run.id)}">
          <span><span class="what">${escapeHtml(title)}</span>
          <span class="meta">${escapeHtml(run.started_how === "trigger" ? run.trigger_detail : run.started_by)} · ${when(run.updated_at)}</span></span>
          <span class="state-cell">${stateWord(run.state.tone, run.state.word)}</span>
        </a>`;
      }).join("")
    : nothingHere("No runs yet.");
}

/* Details: the full trigger rules with their switches, the approval rule,
   the approvers, the settings, the windows, the version. */
function drawDetails(data) {
  const w = data.workflow;
  const a = data.approval;
  const blocks = [];
  blocks.push(
    `<p class="section-label">Triggers</p>` +
    `<div class="list">${data.triggers.map((t) => {
      const control = t.kind === "manual" || !w.allowed_here
        ? ""
        : `<button type="button" class="btn secondary" data-trigger-switch="${escapeHtml(t.key)}" data-to="${t.enabled ? "false" : "true"}">${t.enabled ? "Turn off" : "Turn on"}</button>`;
      const state = t.kind !== "manual" && !t.enabled ? stateWord("neutral", "Off") : "";
      return `<div class="list-row trigger-row">
        <span><span class="what">${escapeHtml(t.sentence)}</span></span>
        <span class="state-cell">${state}</span>
        <span class="row-action">${control}</span>
      </div>`;
    }).join("")}</div>`,
  );
  const approvers = a.approvers.length ? a.approvers.map((p) => p.name + (p.role ? ` (${p.role})` : "")).join(", ") : "nobody yet";
  blocks.push(`<p class="section-label">Approval</p><div>${escapeHtml(a.rule)}</div><div class="muted">Approvers: ${escapeHtml(approvers)}.</div>`);
  if (!isNoteKind(data)) {
    blocks.push(
      `<p class="section-label">Windows</p>` +
      `<div>Approval · ${escapeHtml(hours(a.window_hours))}</div>` +
      `<div>Undo · ${escapeHtml(hours(a.undo_window_hours))}</div>` +
      `<div>Stale · ${plural(a.stale_after_days, "day")}</div>`,
    );
  }
  const settings = data.settings_used || [];
  const missing = new Set((data.missing_settings || []).map((m) => m.key));
  blocks.push(
    `<p class="section-label">Client settings</p>` +
    (settings.length
      ? settings.map((s) => `<div>${escapeHtml(s.key)} <span class="muted">· ${escapeHtml(s.type || "")}${s.required ? " · required" : ""}${s.what_for ? ` · ${escapeHtml(s.what_for)}` : ""}</span> ${missing.has(s.key) ? stateWord("warn", "Not set") : s.set ? stateWord("good", "Set") : `<span class="muted">· not set</span>`}</div>`).join("")
      : `<div class="muted">Approvers only.</div>`),
  );
  // The definition: the file the workflow is, each step's action and what it
  // is handed, as reviewed. References are shown as written.
  const def = data.definition;
  if (def) {
    blocks.push(
      `<p class="section-label">Definition</p>` +
      `<div>${escapeHtml(def.file)} <span class="muted">· ${escapeHtml(def.hash.slice(0, 12))}</span></div>` +
      def.steps.map((s) => {
        const given = Object.entries(s.inputs || {}).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ");
        const when = (s.when || []).map((c) => Object.entries(c).filter(([k]) => k !== "value").map(([k, v]) => `${c.value} ${k} ${JSON.stringify(v)}`).join(" ")).join("; ");
        return `<div class="meta">${escapeHtml(s.id)} · ${escapeHtml(s.action)}${given ? ` <span class="muted">(${escapeHtml(given)})</span>` : ""}${when ? ` <span class="muted">when ${escapeHtml(when)}</span>` : ""}${s.depends_on ? ` <span class="muted">after ${escapeHtml(s.depends_on)}</span>` : ""}</div>`;
      }).join(""),
    );
  }
  blocks.push(`<p class="section-label">Version</p><div>Version ${w.version}</div>`);
  const body = document.getElementById("details-body");
  body.innerHTML = blocks.join("");
  for (const button of body.querySelectorAll("button[data-trigger-switch]")) {
    button.addEventListener("click", () => setTriggerSwitch(button.dataset.triggerSwitch, button.dataset.to === "true"));
  }
}

function drawHow(data) {
  const w = data.workflow;
  const lines = w.kind === "note"
    ? [
        "The note is fixed wording filled with tool readings.",
        "Review first: an engineer posts each note from the ticket.",
        "Post automatically: posted as built, once the client agrees.",
        "One note per ticket. Nothing else on the ticket changes.",
      ]
    : w.kind === "report"
      ? ["Built on the schedule from alert tickets.", "A page in Desk. Nothing is written to Autotask."]
      : [
          "Steps are fixed code, run in order.",
          "A dry run shows what would change first.",
          "The client's approver says yes; a second engineer records it.",
          "Undo is open for a while after it finishes.",
        ];
  document.getElementById("how-lines").innerHTML = lines.map((l) => `<li>${escapeHtml(l)}</li>`).join("");
}

/* --- actions ------------------------------------------------------------------- */

async function setTriggerSwitch(triggerKey, enabled) {
  const t = view.triggers.find((x) => x.key === triggerKey);
  if (enabled) {
    const sure = await ask({ title: "Turn on this trigger?", body: t.sentence, confirm: "Turn on" });
    if (sure === null) return;
  }
  try {
    await api(
      "POST",
      `/automation/switches/${encodeURIComponent(NAME)}/triggers/${encodeURIComponent(triggerKey)}`,
      { enabled },
    );
    await load(true);
  } catch (error) {
    fail(error, "Couldn't change the trigger switch.");
  }
}

async function load(keepDetailsOpen) {
  if (needsClient()) {
    document.getElementById("checks").innerHTML = chooseClientNotice();
    return;
  }
  try {
    const data = await api("GET", `/automation/workflows/${encodeURIComponent(NAME)}`);
    view = data;
    drawTop(data);
    drawSections(data);
    drawRecent(data);
    drawDetails(data);
    drawHow(data);
    if (keepDetailsOpen) document.getElementById("details").open = true;
  } catch (error) {
    fail(error, "Couldn't load this workflow.");
  }
}

load();

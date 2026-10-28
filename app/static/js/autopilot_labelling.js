/* Past tickets: the quick check's two questions, asked about a sample.
 *
 * The same shape as the card on a live ticket, on purpose — one ticket, the
 * same two questions, the same words on the buttons — because the two answers
 * are scored as one population, and a person who has done both should not have
 * had to learn two interfaces.
 *
 * What is deliberately absent: a run picker, a queue of what is left, a
 * double-check banner, and any explanation beyond one "How this works". A
 * labeller told their ticket is being double-checked answers it differently,
 * and a person doing two hundred of these needs a rhythm rather than a page to
 * read.
 *
 * Keyboard: A / H / U answer the first question, 1-9 pick a type, 0 is "not
 * sure", S skips and Z undoes. Answering moves on by itself.
 */

const LABELLING = {
  kind: "origin",
  item: null,
  setId: null,
  answers: [],
  issueTypes: [],
  /** The first question's answer, held until the second is given: one ticket is
   *  one judgement, and a half-answered ticket is not one. */
  picked: null,
  excluded: new Set(),
  busy: false,
};

const ORIGIN_KEYS = { a: "alert", h: "human", u: "unsure" };
const GROUP_KEYS = { a: "same", h: "not_same", u: "partly" };

const BUTTON_WORDS = {
  alert: "Tool",
  human: "Person",
  unsure: "Can't tell",
  same: "Same",
  not_same: "Different",
  partly: "Partly",
};

function keysFor() {
  return LABELLING.kind === "origin" ? ORIGIN_KEYS : GROUP_KEYS;
}

function showError(error, what) {
  const box = document.getElementById("error");
  if (!error) {
    box.hidden = true;
    return;
  }
  box.innerHTML = problem(what || "Something in the labelling job did not work.", error);
  box.hidden = false;
}

/* --- what is being judged ---------------------------------------------------- */

/** The ticket, blind: the title, then one line of facts about it.
 *
 *  The same four things as before — the customer, when it arrived, the queue it
 *  landed in and its number — written as a sentence rather than as four
 *  labelled pairs, with the separators drawn by the stylesheet. The queue keeps
 *  the word "queue" after it because a queue name on its own does not say what
 *  it is; the rest name themselves. */
function ticketCard(ticket) {
  const facts = [
    ticket.site ? escapeHtml(ticket.site) : "",
    when(ticket.created_at, { time: true }),
    ticket.queue ? `${escapeHtml(ticket.queue)} queue` : "",
    ticket.ticket_number ? `#${escapeHtml(ticket.ticket_number)}` : "",
  ]
    .filter(Boolean)
    .map((fact) => `<span>${fact}</span>`)
    .join("");
  const title = ticket.title
    ? escapeHtml(ticket.title)
    : '<span class="muted">This ticket\'s title is not in the ledger any more.</span>';
  return `<div class="subject-card">
    <div class="title">${title}</div>
    <div class="facts">${facts}${autotaskLink(ticket.ticket_id)}</div>
  </div>`;
}

/** The link out to the vendor, as the last fact on the line.
 *
 *  Built from an id we hold and the address this client's own credentials
 *  carry; opening it is the reader's click in their own browser and no call of
 *  ours. A client whose console address we cannot build gets no element at all
 *  rather than a hidden one — the separators between facts are drawn by the
 *  stylesheet, and a hidden last child would leave a dot hanging after the
 *  ticket number. */
function autotaskLink(ticketId) {
  const url = consoleUrl("autotask", ticketId);
  if (!url) return "";
  return `<a class="action" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open in Autotask</a>`;
}

function groupCard(item) {
  const rows = (item.tickets || [])
    .map(
      (ticket, index) => `<div class="group-ticket">
        <span class="ord">${index + 1}.</span>
        <span class="what">
          <span>${escapeHtml(ticket.title || "(no title in the ledger)")}</span>
          <div class="small muted">${escapeHtml(ticket.site || "customer not recorded")}</div>
        </span>
        <span class="when">${when(ticket.created_at, { time: true })}</span>
        <label title="Tick the tickets that are not the same issue as the rest. Used when you answer Partly.">
          <input type="checkbox" class="exclude" value="${escapeHtml(ticket.ticket_id)}">
          <span>not this one</span>
        </label>
      </div>`
    )
    .join("");
  return `<div class="subject-card">
    <div class="title">${(item.tickets || []).length} tickets the agent joined into one group</div>
    <div class="facts"><span>in the order they arrived</span>${autotaskLink(item.leader_id)}</div>
    <div>${rows}</div>
  </div>`;
}

/* --- the questions ------------------------------------------------------------ */

function originQuestions() {
  const keys = keysFor();
  const buttons = LABELLING.answers
    .filter((answer) => answer !== "skipped")
    .map((answer) => {
      const key = Object.entries(keys).find(([, value]) => value === answer);
      const pressed = LABELLING.picked === answer;
      return `<button type="button" class="btn secondary" data-answer="${escapeHtml(answer)}"
        aria-pressed="${pressed}">${escapeHtml(BUTTON_WORDS[answer] || answer)}${
          key ? `<kbd>${key[0].toUpperCase()}</kbd>` : ""
        }</button>`;
    })
    .join("");
  const types = LABELLING.issueTypes
    .map(
      (label, index) =>
        `<button type="button" class="btn secondary small" data-type="${escapeHtml(label)}">${escapeHtml(
          label
        )}${index < 9 ? `<kbd>${index + 1}</kbd>` : ""}</button>`
    )
    .join("");
  return `<div class="blind-question">
      <strong>Was this raised by a tool or a person?</strong>
      <div class="answer-row">${buttons}</div>
    </div>
    <div class="blind-question" id="type-question" ${LABELLING.picked ? "" : "hidden"}>
      <strong>What type is it?</strong>
      <div class="type-row">${types}
        <button type="button" class="action" data-type="unsure">Not sure<kbd>0</kbd></button>
      </div>
    </div>`;
}

function groupQuestions() {
  const keys = keysFor();
  const buttons = LABELLING.answers
    .filter((answer) => answer !== "skipped")
    .map((answer) => {
      const key = Object.entries(keys).find(([, value]) => value === answer);
      return `<button type="button" class="btn secondary" data-answer="${escapeHtml(
        answer
      )}">${escapeHtml(BUTTON_WORDS[answer] || answer)}${
        key ? `<kbd>${key[0].toUpperCase()}</kbd>` : ""
      }</button>`;
    })
    .join("");
  return `<div class="blind-question">
    <strong>Are these the same issue?</strong>
    <div class="answer-row">${buttons}</div>
  </div>`;
}

function drawQuestions() {
  const box = document.getElementById("questions");
  box.innerHTML = LABELLING.kind === "origin" ? originQuestions() : groupQuestions();
  for (const button of box.querySelectorAll("button[data-answer]")) {
    button.addEventListener("click", () => pickAnswer(button.dataset.answer));
  }
  for (const button of box.querySelectorAll("button[data-type]")) {
    button.addEventListener("click", () => send(LABELLING.picked, button.dataset.type));
  }
}

/** The first question, held. A group has only one question, so that answer
 *  goes straight away. */
function pickAnswer(answer) {
  if (LABELLING.kind !== "origin") {
    send(answer, null);
    return;
  }
  LABELLING.picked = answer;
  drawQuestions();
}

/* --- drawing ------------------------------------------------------------------ */

/** How far the set has got: one line, under the title.
 *
 *  A bar said the same thing less precisely and took a row of the screen to do
 *  it, on a page whose whole argument is that there is one ticket on it. */
function drawProgress(progress) {
  const parts = [`${progress.mine} of ${progress.total} done`];
  if (progress.skipped) parts.push(`${progress.skipped} skipped`);
  setText("summary", parts.join(" · "));
}

function drawTask(view) {
  LABELLING.setId = view.set_id;
  LABELLING.item = view.item;
  LABELLING.answers = view.answers || [];
  LABELLING.issueTypes = view.issue_types || [];
  LABELLING.picked = null;
  LABELLING.excluded = new Set();
  drawProgress(view.progress);
  document.getElementById("empty").hidden = true;
  document.getElementById("task").hidden = view.finished;
  document.getElementById("finished").hidden = !view.finished;
  setText("task-title", LABELLING.kind === "origin" ? "Past tickets" : "Past groups");

  if (view.finished) {
    setText(
      "finished-note",
      `${view.progress.answered} of ${view.progress.total} answered; ${view.progress.skipped} skipped.`
    );
    return;
  }

  const item = view.item;
  document.getElementById("subject").innerHTML =
    item.kind === "origin" ? ticketCard(item.ticket) : groupCard(item);
  drawQuestions();
  document.querySelectorAll("#subject .exclude").forEach((box) => {
    box.addEventListener("change", () => {
      if (box.checked) LABELLING.excluded.add(box.value);
      else LABELLING.excluded.delete(box.value);
    });
  });
}

/* --- reading and writing ------------------------------------------------------ */

async function loadTask() {
  showError(null);
  try {
    drawTask(await api("GET", `/autopilot/labelling/task?kind=${LABELLING.kind}`));
  } catch (error) {
    if (error.status === 404) {
      showEmpty(error.message);
      return;
    }
    showError(error);
  }
}

function showEmpty(message) {
  document.getElementById("task").hidden = true;
  document.getElementById("finished").hidden = true;
  document.getElementById("empty").hidden = false;
  setText("empty-note", message);
  setText("summary", "");
}

async function send(answer, issueType) {
  if (!LABELLING.item || LABELLING.busy || !answer) return;
  LABELLING.busy = true;
  showError(null);
  try {
    drawTask(
      await api("POST", "/autopilot/labelling/answer", {
        item_id: LABELLING.item.item_id,
        answer,
        issue_type: issueType,
        excluded: answer === "partly" ? [...LABELLING.excluded] : [],
      })
    );
  } catch (error) {
    showError(error);
  } finally {
    LABELLING.busy = false;
  }
}

async function undoLast() {
  if (!LABELLING.setId || LABELLING.busy) return;
  LABELLING.busy = true;
  showError(null);
  try {
    drawTask(await api("POST", "/autopilot/labelling/undo", { set_id: LABELLING.setId }));
  } catch (error) {
    showError(error);
  } finally {
    LABELLING.busy = false;
  }
}

/* --- the keyboard ------------------------------------------------------------- */

function onKey(event) {
  const typing =
    event.target && (event.target.tagName === "INPUT" || event.target.tagName === "TEXTAREA");
  if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key.toLowerCase();
  const keys = keysFor();
  if (keys[key]) {
    event.preventDefault();
    pickAnswer(keys[key]);
  } else if (key === "s") {
    event.preventDefault();
    send("skipped", null);
  } else if (key === "z") {
    event.preventDefault();
    undoLast();
  } else if (LABELLING.picked && /^[0-9]$/.test(key)) {
    event.preventDefault();
    const label = key === "0" ? "unsure" : LABELLING.issueTypes[Number(key) - 1];
    if (label) send(LABELLING.picked, label);
  }
}

/* --- wiring ------------------------------------------------------------------- */

function selectKind(kind) {
  LABELLING.kind = kind;
  document.getElementById("tab-origin").setAttribute("aria-selected", String(kind === "origin"));
  document.getElementById("tab-groups").setAttribute("aria-selected", String(kind === "groups"));
  document.getElementById("task-card").setAttribute("aria-labelledby", `tab-${kind}`);
  loadTask();
}

function start() {
  if (needsClient()) {
    document.getElementById("task-card").innerHTML = chooseClientNotice();
    return;
  }
  document.getElementById("tab-origin").addEventListener("click", () => selectKind("origin"));
  document.getElementById("tab-groups").addEventListener("click", () => selectKind("groups"));
  document.getElementById("skip").addEventListener("click", () => send("skipped", null));
  document.getElementById("undo").addEventListener("click", undoLast);
  document.addEventListener("keydown", onKey);
  setText("summary", "Judge each one blind. A / H / U, then the type. S skips, Z undoes.");
  selectKind("origin");
}

start();

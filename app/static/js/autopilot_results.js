/* The results page: what the shadow runs say, against the gate.
 *
 * Reads four GET endpoints and draws them. There is no write on this page at
 * all — not disabled, not hidden: absent, because the router behind it has no
 * state-changing route to call. The forward shadow's switch is drawn as a
 * status naming the setting, never as a control.
 *
 * Every figure is drawn through `defined()`, which hangs the server's own
 * definition off it. The page never writes a definition of its own: the
 * server computed the number and is the thing that knows what it counted.
 */

const RESULTS = {
  /** The run on screen. */
  run: null,
  /** The run to compare it with, or null. */
  against: null,
  /** The definitions the last payload carried, keyed as the server keys them. */
  definitions: {},
  /** Whether the forward shadow is polling, for the subtitle. Null until the
      status has been read, so the line says nothing rather than "off". */
  forwardLive: null,
  /** The run on screen, for the subtitle's second fact. */
  current: null,
};

function showError(error, what) {
  const box = document.getElementById("error");
  if (!error) {
    box.hidden = true;
    return;
  }
  box.innerHTML = problem(what || "Something on this page did not work.", error);
  box.hidden = false;
}

/** A figure with the server's definition behind it, on hover and in its title
 *  attribute, so it is reachable without a pointer. */
function defined(key, text) {
  const definition = RESULTS.definitions[key];
  if (!definition) return escapeHtml(text);
  return `<span class="defined" title="${escapeHtml(definition)}">${escapeHtml(text)}</span>`;
}

function percent(value, digits) {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits === undefined ? 1 : digits)}%`;
}

function money(value) {
  if (value === null || value === undefined) return "not priced";
  return `$${value.toFixed(2)}`;
}

function number(value) {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString();
}

/** A figure tile: the number large, the word under it, the definition behind
 *  the word. */
function figure(value, label, key) {
  return `<div class="figure"><div class="n">${escapeHtml(value)}</div>
    <div class="k">${key ? defined(key, label) : escapeHtml(label)}</div></div>`;
}

/* --- the run list ----------------------------------------------------------- */

/** The words a reader knows, with the name in the code on hover. A page that
 *  prints `title_site_time` is a page only its author can read. */
const MODE_WORDS = { replay: "Past tickets", forward: "Live tickets" };
const VARIANT_WORDS = {
  title_site_time: "Title only",
  with_alert_queue: "Title + alert queue",
};

/** Why a run stopped, in words. A reader deciding whether to trust a figure
 *  needs to know the run was cut short and why, and `token_cap` is not a
 *  reason anybody can act on. */
const STOPPED_WORDS = {
  token_cap: "stopped: it reached the token cap",
  cost_cap: "stopped: it reached the cost cap",
  model_refused: "stopped: the model refused several tickets in a row",
  request_budget: "stopped: it reached the hourly request budget",
};

/** A run's state, as a tone and a word. The glyph that used to sit between them
 *  is the dot's job now. */
const RUN_STATE = {
  running: ["neutral", "running"],
  finished: ["good", "finished"],
  stopped: ["bad", "stopped"],
};

async function loadRuns() {
  const runs = await api("GET", "/autopilot/results/runs?limit=50");
  const options = runs
    .map((run) => {
      const variant = run.variant ? ` · ${run.variant}` : "";
      return `<option value="${escapeHtml(run.run_id)}">${escapeHtml(
        `${timeWords(run.started_at, { time: true })} · ${run.tickets} tickets${variant}`
      )}</option>`;
    })
    .join("");
  document.getElementById("run").innerHTML =
    options || `<option value="">No runs on this client yet</option>`;
  document.getElementById("against").innerHTML =
    `<option value="">Just this run</option>` + options;

  const body = document.querySelector("#runs tbody");
  if (!runs.length) {
    body.innerHTML = nothingRow(10, "No shadow run has been made for this client yet.", {
      text: "This page only reads runs; one is started with",
      command: "python scripts/autopilot.py shadow replay --client <slug>",
    });
    setText("runs-sub", "");
    return runs;
  }
  body.innerHTML = runs
    .map((run) => {
      const [tone, word] = RUN_STATE[run.status] || RUN_STATE.running;
      const why = run.stopped_reason
        ? STOPPED_WORDS[run.stopped_reason] || `stopped: ${run.stopped_reason}`
        : "";
      const title = why ? why.charAt(0).toUpperCase() + why.slice(1) + "." : "";
      return `<tr>
        <td class="nowrap">${when(run.started_at, { time: true })}</td>
        <td title="${escapeHtml(run.mode)}">${escapeHtml(MODE_WORDS[run.mode] || run.mode)}</td>
        <td title="${escapeHtml(run.variant || "")}">${escapeHtml(
          VARIANT_WORDS[run.variant] || run.variant || "—"
        )}</td>
        <td class="mono small">${escapeHtml(run.model)}</td>
        <td class="mono small">${escapeHtml(run.prompt_version)}</td>
        <td class="num">${number(run.tickets)}</td>
        <td class="num">${number(run.by_rule)}</td>
        <td class="num">${number(run.errors)}</td>
        <td>${toneChip(tone, word, title)}${
          why ? `<div class="small bad-text">${escapeHtml(why)}</div>` : ""
        }</td>
        <td class="num">${escapeHtml(money(run.cost))}</td>
      </tr>`;
    })
    .join("");
  setText("runs-sub", `${runs.length} runs, newest first.`);
  return runs;
}


/* --- the answer, before any of the working ------------------------------------ */

/** "Ready for Level 1" or "Not ready yet", what is stopping it, and the next
 *  thing somebody would do about it. The server decides all three: a page that
 *  worked out its own verdict would be a second opinion to keep in step. */
function drawHeadline(gate) {
  const card = document.getElementById("headline");
  const headline = gate.headline || {};
  card.hidden = false;
  card.classList.toggle("ready", Boolean(headline.ready));
  /* The sentence is the server's, counts and all: a page that worked out its
     own verdict would be a second opinion to keep in step. */
  setText("headline-sentence", headline.sentence || "");
  const next = document.getElementById("headline-next");
  next.hidden = !headline.next_step;
  next.textContent = headline.next_step ? `Next: ${headline.next_step}` : "";
}

/* --- live reviews ------------------------------------------------------------- */

/** A finding kind in words. The same sentences the card uses, shortened: a
 *  results page that prints `other_tickets_contact` is one only its author can
 *  read. */
const FINDING_WORDS = {
  device_offline: "Machine offline",
  alert_stale: "Alert older than a week",
  ticket_stale: "Ticket older than a week",
  backup_failed: "Backup failed",
  backup_repeat_failures: "Repeated backup failures",
  still_infected: "Still infected",
  malware_detected: "Malware detected",
  open_alerts: "Open alerts",
  known_exploited: "Known-exploited vulnerabilities",
  critical_findings: "Critical vulnerabilities",
  no_protection_agent: "No protection agent",
  signatures_outdated: "Signatures out of date",
  repeat_machine: "Repeat on this machine",
  other_tickets_device: "Other tickets for this machine",
  other_tickets_contact: "Other tickets from this person",
  pending_reboot: "Pending reboot",
  patch_failures: "Patch failures",
  disk_low: "Disk nearly full",
};

const FIELD_WORDS = {
  issue_type: "Issue type",
  origin: "Where it came from",
  urgency: "Urgency",
  repeat: "Repeat",
  machine: "Which machine",
};

/* The two rows of the Findings table that are not a finding: the step the card
   suggests, and the card itself. Both are answered the same way, so they are
   counted in the same table rather than a third one. */
const FINDING_EXTRA_WORDS = {
  next_step: "The suggested next step",
  card: "The card as a whole",
};

/** A line naming what could not be counted, or nothing.
 *
 *  A verdict neither tally has a column for is counted as `unrecognised`
 *  instead of being dropped, because a figure that quietly stops adding up is
 *  worse than one that says a row was not understood. */
function unrecognisedNote(tallies, words) {
  const odd = tallies.filter(([, tally]) => (tally.unrecognised || 0) > 0);
  if (!odd.length) return "";
  const named = odd
    .map(([name, tally]) => `${words[name] || name}: ${number(tally.unrecognised)}`)
    .join(", ");
  return `<p class="small muted">⚠ Not counted, because the verdict stored was not one this table reads — ${escapeHtml(
    named
  )}. The rows are in the database, and the server logged a warning naming each.</p>`;
}

/** Two measurements side by side, and the page says which is which. Approval
 *  answers "was the engineer helped"; the blind checks answer "was the agent
 *  right", and only the second is read by the gate. */
function drawLiveReviews(data) {
  const live = data.live_reviews || { by_field: {} };
  const blind = data.blind || {};
  setText(
    "live-sub",
    `${number(live.suggested)} suggested · ${number(live.shown)} shown · ${number(
      live.reviewed_tickets
    )} reviewed · ${number(live.blind_tickets)} asked blind`
  );
  const rows = Object.entries(live.by_field || {})
    .map(([name, tally]) => {
      return `<tr>
        <td>${escapeHtml(FIELD_WORDS[name] || name)}</td>
        <td class="num">${number(tally.correct)}</td>
        <td class="num">${number(tally.changed)}</td>
        <td class="num">${number(tally.wrong)}</td>
        <td class="num">${tally.share_correct === null ? "—" : percent(tally.share_correct)}</td>
      </tr>`;
    })
    .join("");
  const approval = rows
    ? `<div class="scroller"><table>
        <thead><tr>
          <th>Field</th><th class="num">Correct</th><th class="num">Changed</th>
          <th class="num">Wrong</th><th class="num">Marked correct</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table></div>${unrecognisedNote(Object.entries(live.by_field || {}), FIELD_WORDS)}`
    : nothingHere("No engineer has answered a card on a live ticket yet.", {
        text: "Approval is not accuracy; the gate reads the blind checks.",
      });

  const gateRows = (data.gate.criteria || []).filter(
    (one) => one.key === "issue_type_accuracy" || one.key === "origin_precision"
  );
  const measured = gateRows
    .map(
      (one) => `<div class="figure">
        <div class="n">${escapeHtml(one.value || "—")}</div>
        <div class="k">${escapeHtml(one.label)} ${stateChip("gate", one.verdict)}</div>
      </div>`
    )
    .join("");

  /* The one way in to the past-ticket job: a line that says how much of it is
     left. It is not in the nav, because it is a job you take on when a gate
     row is waiting on it rather than a page to visit. */
  const past = data.origin_labels || {};
  const total = (past.labelled || 0) + (past.unlabelled || 0) + (past.skipped || 0);
  /* Shown only while there is labelling left to do: a link that says "200 of
     200" is a link nobody needs to see again. */
  const labelling =
    total && (past.labelled || 0) < total
      ? `<a href="/autopilot/labelling">Finish labelling past tickets (${number(
          past.labelled || 0
        )} of ${number(total)})</a> — this is what the gate's accuracy is read against.`
      : total
        ? ""
        : `<a href="/autopilot/labelling">Label past tickets</a> — none drawn yet, and the gate's accuracy is read against them.`;

  /* Which findings earn their place. A kind shown four hundred times and
     marked useful twice is a line on four hundred cards nobody wanted; this
     is the table that says so. */
  const findings = Object.entries(data.findings || {})
    .sort((a, b) => b[1].shown - a[1].shown)
    .map(
      ([kind, tally]) => `<tr>
        <td>${escapeHtml(FINDING_WORDS[kind] || FINDING_EXTRA_WORDS[kind] || kind)}</td>
        <td class="num">${number(tally.shown)}</td>
        <td class="num">${number(tally.reviewed)}</td>
        <td class="num">${tally.share_useful === null ? "—" : percent(tally.share_useful)}</td>
      </tr>`
    )
    .join("");
  const findingsTable = findings
    ? `<div class="scroller"><table>
        <thead><tr>
          <th>Finding</th><th class="num">Shown</th><th class="num">Reviewed</th>
          <th class="num">Marked useful</th>
        </tr></thead><tbody>${findings}</tbody></table></div>${unrecognisedNote(
        Object.entries(data.findings || {}),
        { ...FINDING_WORDS, ...FINDING_EXTRA_WORDS }
      )}`
    : nothingHere("The agent has found nothing on a live ticket yet.");

  document.getElementById("live-body").innerHTML = `
    <div class="section">
      <h2 class="section-title">What engineers made of the suggestions<span class="aside">approval, never gated</span></h2>
      <p class="small muted">The engineer had read the suggestion before they judged it, so this says whether they were helped — not whether the agent was right.</p>
      ${approval}
    </div>
    <div class="section">
      <h2 class="section-title">What people said before they were shown anything<span class="aside">this is what the gate reads</span></h2>
      <p class="small muted">The quick check on ${number(
        blind.tickets
      )} ticket(s), plus the past-ticket sample.${
        blind.disagreed
          ? ` ${number(blind.disagreed)} answer(s) left out because two people disagreed.`
          : ""
      }</p>
      <div class="figure-grid">${measured}</div>
    </div>`;

  /* The findings have a tab of their own, and the labelling line belongs on
     the main view while the sample is unfinished: it is the next thing to do. */
  document.getElementById("findings-body").innerHTML = findingsTable;
  const line = document.getElementById("labelling-line");
  line.hidden = !labelling;
  line.innerHTML = labelling;
}

/* --- the gate --------------------------------------------------------------- */

/** Which dot a verdict is drawn with. The word beside it is `states.js`'s own
 *  and is what carries the meaning. */
const GATE_DOTS = { pass: "good", fail: "bad", not_measured: "neutral" };

function drawGate(gate) {
  /* Four rows: what the criterion is, where it stands against its target, and
     the one word for how it is doing. */
  document.getElementById("gate").innerHTML = gate.criteria
    .map(
      (one) => `<div class="list-row gate-row">
        <span class="dot ${GATE_DOTS[one.verdict] || "neutral"}"></span>
        <span>
          <span class="what">${escapeHtml(one.label)}</span>
          <span class="meta">${escapeHtml(one.value || "no figure yet")} against ${escapeHtml(
            one.target
          )} · ${escapeHtml(one.detail)}</span>
        </span>
        <span class="verdict-word">${stateChip("gate", one.verdict)}</span>
      </div>`
    )
    .join("");

  /* The readiness rows are not the gate, and they are drawn where the
     approvals they count are — under Live reviews, with no verdict beside
     them, because they have no bar to clear and a verdict would read as one. */
  document.getElementById("readiness-body").innerHTML = (gate.readiness || []).length
    ? `<div class="list">${gate.readiness
        .map(
          (one) => `<div class="list-row">
            <span>
              <span class="what">${escapeHtml(one.label)}</span>
              <span class="meta">${escapeHtml(one.detail)}</span>
            </span>
            <span class="meta">${escapeHtml(one.value || "—")}</span>
          </div>`
        )
        .join("")}</div>`
    : nothingHere("Nothing has been reviewed on a live ticket yet.");
}

/** The one comparison the measurement rests on, in a line: what engineers
 *  approve, and what people answered before they were shown anything. Only the
 *  second is read by the gate, and the distance between them is why the two
 *  are kept in different tables everywhere else. */
function drawGap(data) {
  const line = document.getElementById("gap-line");
  const live = data.live_reviews || { by_field: {} };
  const tallies = Object.values(live.by_field || {});
  const reviewed = tallies.reduce((total, one) => total + (one.reviewed || 0), 0);
  const correct = tallies.reduce((total, one) => total + (one.correct || 0), 0);
  const blind = (data.gate.criteria || []).find((one) => one.key === "issue_type_accuracy");
  const parts = [];
  if (reviewed) {
    parts.push(
      `<span class="state"><span class="dot neutral"></span><span class="word">Engineers approve ${percent(
        correct / reviewed
      )}</span></span>`
    );
  }
  if (blind) {
    /* Drawn even with no figure behind it yet: half of this comparison is the
       half that matters, and leaving it out would make an approval rate look
       like the measurement. */
    parts.push(
      `<span class="state"><span class="dot ${
        GATE_DOTS[blind.verdict] || "neutral"
      }"></span><span class="word">blind checks ${
        blind.value ? `agree ${escapeHtml(blind.value)}` : "have no figure yet"
      }</span></span>`
    );
  }
  line.hidden = !parts.length;
  line.innerHTML = parts.join("");
  line.title =
    "Approval is not accuracy: an engineer had read the suggestion before they judged it. " +
    "Only the blind answer is read by the gate.";
}

/* --- accuracy and coverage by threshold ------------------------------------- */

function thresholdTable(thresholds, chosen) {
  const rows = Object.entries(thresholds)
    .map(([floor, score]) => {
      const marked = chosen !== null && Math.abs(Number(floor) - chosen) < 1e-9;
      return `<tr${marked ? ' class="marked"' : ""}>
        <td class="nowrap">${escapeHtml(Number(floor).toFixed(2))}${
          marked ? " ▲" : ""
        }</td>
        <td class="num">${percent(score.accuracy)}</td>
        <td class="num">${percent(score.coverage)}</td>
        <td class="num">${number(score.answered)}</td>
        <td class="num">${number(score.correct)}</td>
        <td class="num">${number(score.judged)}</td>
      </tr>`;
    })
    .join("");
  return `<div class="scroller"><table>
    <thead><tr>
      <th>Threshold</th><th class="num">${defined("accuracy", "Accuracy")}</th>
      <th class="num">${defined("coverage", "Coverage")}</th>
      <th class="num">Answered</th><th class="num">Correct</th>
      <th class="num">${defined("judged", "Judgeable")}</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function drawThresholds(periods, chosen) {
  const blocks = ["development", "holdout"].map((name) => {
    const card = periods[name];
    const points = Object.entries(card.thresholds).map(([floor, score]) => ({
      label: Number(floor).toFixed(2),
      first: score.accuracy,
      second: score.coverage,
    }));
    const mark = chosen === null ? null : chosen.toFixed(2);
    return `<section class="section">
      <h3 title="${escapeHtml(
        name === "development" ? "development period" : "holdout"
      )}">${defined(name, name === "development" ? "Tuning period" : "Test period")}</h3>
      <p class="small muted">${escapeHtml(RESULTS.definitions[name] || "")}</p>
      <p class="small">${defined("judged", "Judgeable tickets")}: <strong>${number(
        card.issue_type.judged
      )}</strong> · answered <strong>${number(card.issue_type.answered)}</strong> · overall
        ${defined("accuracy", "accuracy")} <strong>${percent(card.issue_type.accuracy)}</strong>
        at ${defined("coverage", "coverage")} <strong>${percent(card.issue_type.coverage)}</strong>.
        ${
          card.issue_type_before_policy.accuracy !== card.issue_type.accuracy
            ? `Before the franchisee's label policy: <strong>${percent(
                card.issue_type_before_policy.accuracy
              )}</strong> <span class="defined" title="${escapeHtml(
                RESULTS.definitions.before_policy || ""
              )}">(raw answer)</span>.`
            : ""
        }</p>
      ${percentColumns(points, { marked: name === "holdout" ? mark : mark })}
      ${thresholdTable(card.thresholds, chosen)}
    </section>`;
  });
  document.getElementById("thresholds").innerHTML = blocks.join("");
}

/* --- by issue type, by title length, and the confusion matrix ---------------- */

function scoreRows(group, headKey) {
  const rows = Object.entries(group)
    .sort((a, b) => b[1].judged - a[1].judged)
    .map(
      ([name, score]) => `<tr>
        <td>${escapeHtml(name)}</td>
        <td class="num">${percent(score.accuracy)}</td>
        <td class="num">${percent(score.coverage)}</td>
        <td class="num">${number(score.answered)}</td>
        <td class="num">${number(score.correct)}</td>
        <td class="num">${number(score.judged)}</td>
      </tr>`
    )
    .join("");
  return `<div class="scroller"><table>
    <thead><tr>
      <th>${escapeHtml(headKey)}</th><th class="num">${defined("accuracy", "Accuracy")}</th>
      <th class="num">${defined("coverage", "Coverage")}</th>
      <th class="num">Answered</th><th class="num">Correct</th>
      <th class="num">${defined("judged", "Judgeable")}</th>
    </tr></thead><tbody>${rows}</tbody></table></div>`;
}

/** The confusion matrix, as the pairs it actually holds rather than a square of
 *  mostly zeroes: fifteen issue types would be 225 cells, almost all empty. */
function confusionTable(confusion) {
  const pairs = [];
  Object.entries(confusion).forEach(([predicted, actuals]) => {
    Object.entries(actuals).forEach(([actual, count]) => {
      if (predicted !== actual) pairs.push({ predicted, actual, count });
    });
  });
  pairs.sort((a, b) => b.count - a.count);
  if (!pairs.length) return '<p class="small muted">No wrong answers to lay out.</p>';
  const rows = pairs
    .map(
      (pair) => `<tr>
        <td>${escapeHtml(pair.predicted)}</td>
        <td>${escapeHtml(pair.actual)}</td>
        <td class="num">${number(pair.count)}</td>
      </tr>`
    )
    .join("");
  return `<div class="scroller"><table>
    <thead><tr><th>The agent said</th><th>The ledger says</th><th class="num">Times</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function drawBreakdown(card) {
  setText(
    "breakdown-sub",
    "On the holdout, at every confidence — not at the chosen threshold — so a type the agent " +
      "is quietly bad at is visible rather than hidden behind a decline."
  );
  document.getElementById("breakdown").innerHTML = `
    <section class="section">
      <h3>By issue type</h3>
      ${scoreRows(card.by_issue_type, "Issue type")}
    </section>
    <section class="section">
      <h3>By how long the subject line is</h3>
      <p class="small muted">Words in the scrubbed title. A three-word title is most of what a person gets too.</p>
      ${scoreRows(card.by_title_length, "Words in the title")}
    </section>
    <section class="section">
      <h3>Which path answered</h3>
      <p class="small muted">A rule where the wording is unmistakable, the model for the rest.</p>
      <p class="small muted">${escapeHtml(RESULTS.definitions.rule_path || "")}</p>
      ${scoreRows(card.by_path, "Path")}
    </section>
    <section class="section">
      <h3>Where it went wrong</h3>
      ${confusionTable(card.confusion)}
    </section>`;
}

/* --- origin, grouping, cost, errors ----------------------------------------- */

function drawOrigin(labels, card) {
  const proxyOne = card.origin.PROXY_no_assignee;
  const proxyTwo = card.origin.PROXY_alert_queue;
  const sharePair = (part, whole) => (whole ? percent(part / whole) : "—");
  document.getElementById("origin").innerHTML = `
    <section class="section">
      <h3>Against the labelled sample</h3>
      ${
        labels.labelled
          ? `<div class="figure-grid">
              ${figure(number(labels.labelled), "tickets labelled")}
              ${figure(
                labels.agreement === null ? "—" : percent(labels.agreement),
                "two labellers agreed"
              )}
              ${figure(number(labels.disagreed), "left out: disagreed")}
              ${figure(number(labels.unlabelled), "not labelled yet")}
            </div>`
          : `<p class="small muted"><strong>Nothing labelled yet.</strong> The gate's origin criterion reads
             "labelling 0 of 200" until somebody works through the sample on the
             <a href="/autopilot/labelling">labelling page</a>. The two figures below are proxies and are not the measurement.</p>`
      }
    </section>
    <section class="section">
      <h3>${defined("proxy", "Proxy: tickets nobody was assigned to")}</h3>
      <p class="small muted">Not a score. An unassigned completed ticket is more likely to have come from a tool than from a person, which is a tendency and not a fact about any one ticket.</p>
      <div class="figure-grid">
        ${figure(sharePair(proxyOne.called_alert, proxyOne.unassigned), "unassigned, called alert")}
        ${figure(sharePair(proxyOne.called_human, proxyOne.assigned), "assigned, called human")}
        ${figure(number(proxyOne.unassigned), "unassigned tickets")}
        ${figure(number(proxyOne.assigned), "assigned tickets")}
      </div>
    </section>
    <section class="section">
      <h3>${defined("proxy", "Proxy: the franchisee's alert queue")}</h3>
      <p class="small muted">Not a score either. A ticket in the queue the franchisee points their monitoring at is usually machine-raised; the queue is a convention, not a guarantee.</p>
      <div class="figure-grid">
        ${figure(sharePair(proxyTwo.called_alert, proxyTwo.alert_queue), "alert queue, called alert")}
        ${figure(sharePair(proxyTwo.called_human, proxyTwo.other_queue), "other queue, called human")}
        ${figure(number(proxyTwo.alert_queue), "tickets in the alert queue")}
        ${figure(number(proxyTwo.other_queue), "tickets elsewhere")}
      </div>
    </section>`;
}

function drawGrouping(judged, card) {
  const grouping = card.grouping;
  document.getElementById("grouping").innerHTML = `
    <div class="figure-grid">
      ${figure(number(grouping.groups), "groups the run made")}
      ${figure(number(grouping.joined), "tickets joined to one")}
      ${figure(
        judged.correct_share === null ? "—" : percent(judged.correct_share),
        "of judged groups are one issue"
      )}
      ${figure(`${judged.judged} of ${judged.judged + judged.unjudged}`, "groups spot-checked")}
    </div>
    <p class="small" style="margin-top: var(--space-3)">
      ${judged.judged
        ? `<strong>${judged.same}</strong> one issue · <strong>${judged.partly}</strong> partly, which counts as wrong ·
           <strong>${judged.not_same}</strong> not the same · <strong>${judged.excluded_tickets}</strong> tickets named as not belonging.`
        : `<strong>No groups judged yet.</strong> The gate's grouping criterion is not measured until the
           <a href="/autopilot/labelling">groups sample</a> has answers. Counting a group nobody checked as correct
           would make the 90% bar unreachable in the only direction that matters.`}
    </p>`;
}

function drawCost(cost, run) {
  document.getElementById("cost").innerHTML = `
    <div class="figure-grid">
      ${figure(number(cost.fresh_input), "fresh input tokens", "fresh_input")}
      ${figure(number(cost.cached_input), "cached input tokens", "cached_input")}
      ${figure(number(cost.output), "output tokens")}
      ${figure(money(cost.total), cost.unpriced ? "no rates set" : "this run")}
    </div>
    <p class="small" style="margin-top: var(--space-3)">
      ${
        cost.unpriced
          ? `<strong>Unpriced.</strong> No per-token rates are set for this deployment, so the token counts stand on their own
             and nothing is multiplied by a guess.`
          : `${escapeHtml(money(cost.per_ticket))} a ticket over ${number(run.tickets)} tickets.
             Projected at this rate: <strong>${escapeHtml(
               money(cost.projected_monthly_one)
             )}</strong> a month for one franchisee,
             <strong>${escapeHtml(money(cost.projected_monthly_twenty))}</strong> for twenty.`
      }
    </p>
    <p class="small muted">${escapeHtml(RESULTS.definitions.cached_input || "")}</p>`;
}

function drawErrors(card) {
  const errors = card.confident_errors || [];
  if (!errors.length) {
    document.getElementById("errors").innerHTML =
      '<p class="small muted">No wrong answers above the confidence this list reads from.</p>';
    return;
  }
  document.getElementById("errors").innerHTML = `<div class="scroller"><table>
    <thead><tr><th>Ticket</th><th>Confidence</th><th>The agent said</th><th>The ledger says</th></tr></thead>
    <tbody>${errors
      .map(
        (one) => `<tr>
          <td class="mono">${escapeHtml(one.ticket_id)}</td>
          <td class="num">${escapeHtml(one.confidence === null ? "—" : one.confidence.toFixed(2))}</td>
          <td>${escapeHtml(one.predicted || "—")}</td>
          <td>${escapeHtml(one.actual || "—")}</td>
        </tr>`
      )
      .join("")}</tbody></table></div>
    <p class="small muted" style="margin-top: var(--space-3)">Ids, not titles: the point of the list is which answers to read, and a title here would be ticket text on a page that does not need it.</p>`;
}

/* --- the forward shadow ----------------------------------------------------- */

async function loadForward() {
  const state = await api("GET", "/autopilot/results/forward");
  RESULTS.definitions = { ...RESULTS.definitions, ...state.definitions };
  RESULTS.forwardLive = Boolean(state.switch.live);
  drawSubtitle(RESULTS.current);
  document.getElementById("forward-switch").innerHTML = state.switch.live
    ? toneChip("good", "on", state.switch.detail)
    : toneChip("neutral", "off", state.switch.detail);
  const days = Object.entries(state.per_day).sort();
  const switchNote = `<p class="small">
      <strong>${escapeHtml(state.switch.setting)}</strong> is
      ${state.switch.live ? "on" : "off"}. ${escapeHtml(state.switch.detail)}
      It is a setting, not a button: this page reports it and cannot change it.
    </p>`;
  if (state.empty) {
    document.getElementById("forward-body").innerHTML = `${switchNote}
      <p class="small muted"><strong>Nothing to show yet.</strong> The forward shadow has not run, so there are no
      tickets, no snapshots and no wrong-site count — rather than four zeroes, which would read as a clean result
      from a fortnight of watching.</p>`;
    return;
  }
  document.getElementById("forward-body").innerHTML = `${switchNote}
    <div class="figure-grid">
      ${figure(number(state.tickets), "tickets seen")}
      ${figure(`${state.days_running}`, "days running")}
      ${figure(number(state.snapshots), "enrichment snapshots")}
      ${figure(number(state.wrong_site), "wrong site", "wrong_site")}
    </div>
    <p class="small" style="margin-top: var(--space-3)">First ${escapeHtml(
      timeWords(state.first_at)
    )}, latest ${escapeHtml(timeWords(state.last_at))}. ${number(state.no_device)} snapshots resolved no machine.</p>
    ${
      state.wrong_site_tickets.length
        ? `<p class="small"><strong>Tickets whose enrichment reached another customer's machine:</strong>
           <span class="mono">${escapeHtml(state.wrong_site_tickets.join(", "))}</span>.
           The gate asks for none of these.</p>`
        : `<p class="small">${toneChip(
            "good",
            "no wrong-site enrichment",
            "Every snapshot's device belonged to the ticket's own customer."
          )}</p>`
    }
    ${
      Object.keys(state.not_assessed).length
        ? `<p class="small">${defined("not_assessed", "Tools not assessed")}:
           ${escapeHtml(
             Object.entries(state.not_assessed)
               .map(([reason, count]) => `${reason} ${count}`)
               .join(" · ")
           )}</p>`
        : ""
    }
    ${
      days.length
        ? `<div class="scroller" style="margin-top: var(--space-3)"><table>
            <thead><tr><th>Day</th><th class="num">Tickets</th></tr></thead>
            <tbody>${days
              .map(
                ([day, count]) =>
                  `<tr><td class="nowrap">${escapeHtml(day)}</td><td class="num">${count}</td></tr>`
              )
              .join("")}</tbody>
          </table></div>`
        : ""
    }`;
}

/* --- comparing two runs ----------------------------------------------------- */

function liftRow(name, pair) {
  const arrow = (value) => {
    if (value === null || value === undefined) return "—";
    const sign = value > 0 ? "+" : "";
    return `${sign}${(value * 100).toFixed(1)} pts`;
  };
  return `<tr>
    <td><strong>${escapeHtml(name)}</strong></td>
    <td class="num">${percent(pair.baseline_accuracy)}</td>
    <td class="num">${percent(pair.variant_accuracy)}</td>
    <td class="num">${escapeHtml(arrow(pair.accuracy_lift))}</td>
    <td class="num">${percent(pair.baseline_coverage)}</td>
    <td class="num">${percent(pair.variant_coverage)}</td>
    <td class="num">${escapeHtml(arrow(pair.coverage_lift))}</td>
  </tr>`;
}

async function loadCompare() {
  const query = new URLSearchParams({ baseline: RESULTS.against, variant: RESULTS.run });
  const found = await api("GET", `/autopilot/results/compare?${query}`);
  RESULTS.definitions = { ...RESULTS.definitions, ...found.definitions };
  setText(
    "compare-sub",
    `Both runs scored on the ${found.period} period. The baseline is the run you chose to compare against.`
  );
  const differs = document.getElementById("compare-differs");
  if (found.differs.length > 1) {
    differs.innerHTML = `<strong>These two runs differ in more than one thing:</strong> ${escapeHtml(
      found.differs.join(", ")
    )}. A difference in the numbers cannot be attributed to any one of them.`;
    differs.hidden = false;
  } else if (found.differs.length === 1) {
    differs.innerHTML = `They differ in one thing: <strong>${escapeHtml(found.differs[0])}</strong>.`;
    differs.hidden = false;
  } else {
    differs.hidden = true;
  }
  document.getElementById("compare-body").innerHTML = `
    <div class="scroller"><table>
      <thead><tr>
        <th>Field</th>
        <th class="num">Baseline ${defined("accuracy", "accuracy")}</th>
        <th class="num">Variant accuracy</th><th class="num">Difference</th>
        <th class="num">Baseline ${defined("coverage", "coverage")}</th>
        <th class="num">Variant coverage</th><th class="num">Difference</th>
      </tr></thead>
      <tbody>
        ${liftRow("Issue type", found.lift.issue_type)}
        ${liftRow("Urgency", found.lift.urgency)}
      </tbody>
    </table></div>
    <p class="small" style="margin-top: var(--space-3)">
      Called alert: ${number(found.lift.origin_alert_called.baseline)} → ${number(
        found.lift.origin_alert_called.variant
      )} · declined on origin: ${number(found.lift.origin_unsure.baseline)} → ${number(
        found.lift.origin_unsure.variant
      )}.
    </p>
    <p class="small muted">The difference is accuracy <em>and</em> coverage: an extra input that only makes the agent bolder moves the second, and would read as an improvement if only the first were shown.</p>`;
}

/* --- loading ---------------------------------------------------------------- */

async function loadDetail() {
  if (!RESULTS.run) return;
  const found = await api("GET", `/autopilot/results/runs/${RESULTS.run}`);
  RESULTS.definitions = { ...RESULTS.definitions, ...found.definitions };
  drawHeadline(found.gate);
  drawGate(found.gate);
  drawLiveReviews(found);
  drawGap(found);
  RESULTS.current = found.run;
  drawSubtitle(found.run);
  drawThresholds(found.periods, found.gate.chosen_threshold);
  drawBreakdown(found.periods.holdout);
  drawOrigin(found.origin_labels, found.periods.holdout);
  drawGrouping(found.groups_judged, found.periods.holdout);
  drawCost(found.cost, found.run);
  drawErrors(found.periods.holdout);
  const holdout = found.periods.holdout.issue_type;
  setText(
    "past-summary",
    `${number(holdout.judged)} past tickets scored against the ledger · accuracy ${percent(
      holdout.accuracy
    )} at coverage ${percent(holdout.coverage)}. Not a gate figure.`
  );
}

/** The line under the title: which client, which run is being read, and
 *  whether the forward shadow is polling. Three facts, joined by the middle
 *  dot the stylesheet draws. */
function drawSubtitle(run) {
  const parts = [
    run ? `${MODE_WORDS[run.mode] || run.mode} run, ${timeWords(run.started_at)}` : null,
    RESULTS.forwardLive === null || RESULTS.forwardLive === undefined
      ? null
      : `forward shadow ${RESULTS.forwardLive ? "on" : "off"}`,
  ];
  document.getElementById("subtitle").innerHTML = parts
    .filter(Boolean)
    .map((part) => `<span>${escapeHtml(String(part))}</span>`)
    .join("");
}

/** The parts that only mean something once a run exists. With none, the page
 *  says so where the answer would be and leaves the tabs empty rather than
 *  drawing four zeroes. */
function showAnswer(on) {
  document.getElementById("headline").hidden = !on;
  document.getElementById("gate").hidden = !on;
  document.getElementById("results-tabs").hidden = !on;
  for (const tab of TABS) document.getElementById(`tab-${tab}`).hidden = !on;
  if (on) showTab(RESULTS.tab || "live");
}

async function load() {
  if (needsClient()) {
    document.querySelector("#runs tbody").innerHTML =
      `<tr><td colspan="10">${chooseClientNotice()}</td></tr>`;
    showAnswer(false);
    return;
  }
  showError(null);
  try {
    const runs = await loadRuns();
    if (!runs.length) {
      showAnswer(false);
      drawSubtitle(null);
      return;
    }
    showAnswer(true);
    if (!RESULTS.run) {
      /* The newest run that finished without errors. A run that stopped or
         threw is still in the list, drawn red, and is never what the page
         opens on: a figure read off a half-finished run is worse than no
         figure, and nobody checks which run they are looking at. */
      /* A replay: the sections under the gate are about past tickets, and a
         live pass has none of those. The live figures above them are read per
         client and do not move with this picker. */
      const clean = runs.find(
        (run) => run.mode === "replay" && run.status === "finished" && !run.errors
      );
      RESULTS.run = (clean || runs[0]).run_id;
      document.getElementById("run").value = RESULTS.run;
      const picked = clean || runs[0];
      setText(
        "runs-sub",
        `${runs.length} runs, newest first. Showing ${
          clean ? "the newest clean replay" : "the newest run — no replay finished cleanly"
        }, started ${timeWords(picked.started_at, { time: true })}.`
      );
    }
    await loadDetail();
    await loadForward();
  } catch (error) {
    showError(error);
  }
}

document.getElementById("run").addEventListener("change", async (event) => {
  RESULTS.run = event.target.value || null;
  showError(null);
  try {
    await loadDetail();
    if (RESULTS.against) await loadCompare();
  } catch (error) {
    showError(error);
  }
});

document.getElementById("against").addEventListener("change", async (event) => {
  RESULTS.against = event.target.value || null;
  document.getElementById("compare").hidden = !RESULTS.against;
  if (!RESULTS.against) return;
  showError(null);
  try {
    await loadCompare();
  } catch (error) {
    showError(error);
  }
});

/* --- the tabs and the menu ------------------------------------------------------ */

const TABS = ["live", "past", "findings", "cost"];

function showTab(name) {
  const wanted = TABS.includes(name) ? name : "live";
  RESULTS.tab = wanted;
  for (const tab of TABS) {
    const panel = document.getElementById(`tab-${tab}`);
    const button = document.getElementById(`tab-button-${tab}`);
    if (panel) panel.hidden = tab !== wanted;
    if (button) button.setAttribute("aria-selected", String(tab === wanted));
  }
  const url = new URL(window.location.href);
  if (wanted === "live") url.searchParams.delete("tab");
  else url.searchParams.set("tab", wanted);
  window.history.replaceState({}, "", url);
}

for (const button of document.querySelectorAll("#results-tabs [data-tab]")) {
  button.addEventListener("click", () => showTab(button.dataset.tab));
}

/* Which run, and what to compare it with: two questions somebody asks once a
   session, which is what a menu is for. */
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
});
document.addEventListener("click", (event) => {
  if (!moreList.hidden && !moreList.contains(event.target) && event.target !== moreButton) {
    closeMore();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeMore();
});

showTab(new URLSearchParams(window.location.search).get("tab") || "live");
load();

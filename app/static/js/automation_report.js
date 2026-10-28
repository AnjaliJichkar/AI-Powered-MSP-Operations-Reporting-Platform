/* The latest report a scheduled workflow built. Read only.
 *
 * The chronic devices report: one table, a row per machine with three or
 * more alert tickets in the period, its customer, the count, the alert kinds,
 * and the tickets behind the row's disclosure. Built by the poll on the
 * workflow's schedule; this page reads the stored row.
 */

const NAME = DESK.workflowName;
const errorBox = document.getElementById("error");
const noticeBox = document.getElementById("notice");
const table = document.getElementById("devices");

function fail(error, what) {
  errorBox.innerHTML = problem(what || "Something on this page did not work.", error);
  errorBox.hidden = false;
}

const HEAD = "<thead><tr><th>Device</th><th>Client</th><th>Alerts in 30 days</th><th>Kinds</th><th></th></tr></thead>";

function draw(data) {
  const w = data.workflow;
  document.getElementById("title").textContent = w.title;
  setText("crumb-title", w.title);
  document.title = `${w.title} · Automations · Desk`;
  document.getElementById("workflow-link").href = `/automations/workflows/${encodeURIComponent(w.name)}`;
  const report = data.report;
  if (!report) {
    document.getElementById("subtitle").innerHTML = "<span>not built yet</span>";
    table.innerHTML = HEAD + `<tbody>${nothingRow(5, "Not built yet.")}</tbody>`;
    return;
  }
  const body = report.body;
  document.getElementById("subtitle").innerHTML = [
    `<span>built ${when(report.built_at)}</span>`,
    `<span>${when(report.period_start)} to ${when(report.period_end)}</span>`,
    `<span>${plural(body.tickets || 0, "alert ticket")}</span>`,
    body.unmatched ? `<span>${body.unmatched} unmatched</span>` : "",
  ].filter(Boolean).join("");
  if (body.error) {
    noticeBox.className = "notice bad";
    noticeBox.innerHTML = `<strong>Not built.</strong> <span class="advice">${escapeHtml(body.error)}</span>`;
    noticeBox.hidden = false;
  }
  const devices = body.devices || [];
  if (!devices.length) {
    table.innerHTML = HEAD + `<tbody>${nothingRow(5, "No chronic devices.")}</tbody>`;
    return;
  }
  table.innerHTML =
    HEAD +
    "<tbody>" +
    devices
      .map((d) => {
        const kinds = d.kinds.map((k) => `${k.title} (${k.count})`).join(", ");
        const tickets = d.kinds
          .flatMap((k) => k.tickets)
          .map((t) => `<a href="/tickets/${encodeURIComponent(t.number)}">${escapeHtml(t.number)}</a>${t.status ? ` <span class="muted">${escapeHtml(t.status)}</span>` : ""}`)
          .join("<br>");
        return `<tr>
          <td><strong>${escapeHtml(d.device)}</strong></td>
          <td>${escapeHtml(d.site || "")}</td>
          <td>${d.total}</td>
          <td>${escapeHtml(kinds)}</td>
          <td><details class="small"><summary>Tickets</summary><div>${tickets}</div></details></td>
        </tr>`;
      })
      .join("") +
    "</tbody>";
}

async function load() {
  if (needsClient()) {
    table.innerHTML = chooseClientNotice();
    return;
  }
  try {
    draw(await api("GET", `/automation/reports/${encodeURIComponent(NAME)}`));
  } catch (error) {
    fail(error, "Couldn't load this report.");
  }
}

load();

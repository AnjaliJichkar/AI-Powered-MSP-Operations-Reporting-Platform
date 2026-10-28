/* Sites: the client's end customers, and which vendors know each of them.
 *
 * A read and nothing else. Sites are discovered by `sync_sites` from the
 * vendors' own organisation lists and are deactivated rather than deleted when
 * a vendor stops listing one, so there is nothing to create or remove from a
 * page — the fix for a wrong one is `link_sites.py`, run by somebody with the
 * database in front of them.
 */

const rows = document.getElementById("rows");
const summary = document.getElementById("summary");
const errorBox = document.getElementById("error");
const includeInactive = document.getElementById("include-inactive");
const toolFilter = document.getElementById("tool-filter");

/** Three columns: who the customer is, which tools know them, and whether the
 *  vendors still list them. */
const COLUMNS = 3;

/** Which connector a name belongs to, for the chip beside a site. Names
 *  rather than ids: the vendor's organisation id is ours, not the reader's. */
const CONNECTOR_LABELS = {
  ninjaone: "NinjaOne",
  autotask: "Autotask",
  bitdefender: "Bitdefender",
  connectsecure: "ConnectSecure",
  cove: "Cove",
  blackpoint: "Blackpoint",
  microsoft365: "Microsoft 365",
};

/** The tools a customer can be known to, in the order the table shows them.
 *  Taken from the labels rather than from what this client happens to have, so
 *  a column does not appear and disappear as customers are onboarded. */
const TOOLS = ["ninjaone", "autotask", "bitdefender", "connectsecure", "cove", "microsoft365"];

/** What each tool is for, under its name in the header: one word per column
 *  instead of the same word on every row. */
const TOOL_ROLE = {
  ninjaone: "monitoring",
  autotask: "tickets",
  bitdefender: "protection",
  connectsecure: "scanning",
  cove: "backup",
  microsoft365: "directory",
};

function head() {
  return (
    '<tr><th scope="col">Customer</th>' +
    '<th scope="col">Integrations</th>' +
    '<th class="nowrap" scope="col">Status</th></tr>'
  );
}

/** The tools that know this customer, each as its mark and its name.
 *
 *  Only the ones they have. Six columns of ticks and dashes asked the reader
 *  to check a row against a header six times to learn one thing — which tools
 *  this customer is on — and drew the absences as loudly as the presences,
 *  which reads as six findings per row. A customer who does not buy a tool is
 *  not missing it, and this page cannot tell the two apart: the Coverage board
 *  is where a gap is asserted, with the rule it rests on. The tool filter
 *  above the table is how you ask the other question, one tool at a time. */
function toolChips(site) {
  const held = TOOLS.filter((key) => site.connectors.includes(key));
  if (!held.length) {
    return '<span class="muted small">No tool holds an organisation for them</span>';
  }
  return (
    '<span class="tool-chips">' +
    held
      .map((key) => {
        const label = CONNECTOR_LABELS[key];
        const role = TOOL_ROLE[key] || "";
        return (
          `<span class="tool-chip" title="${escapeHtml(`${label} holds an organisation for this customer (${role}).`)}">` +
          `${vendorMark(key)}${escapeHtml(label)}</span>`
        );
      })
      .join("") +
    "</span>"
  );
}

function row(site) {
  const status = site.is_active
    ? stateWord("good", "Active")
    : stateWord("neutral", "Inactive");
  /* The slug under the name rather than in a column of its own: it is how an
     engineer names this customer to Ask and to the scripts, which makes it a
     fact about the row rather than a second thing to scan. */
  return `<tr>
    <td><strong>${escapeHtml(site.name)}</strong>
      <div class="small muted mono">${escapeHtml(site.slug)}</div></td>
    <td>${toolChips(site)}</td>
    <td class="nowrap">${status}</td>
  </tr>`;
}

/** One line above the table, and three figures under it.
 *
 *  The subtitle used to carry every number this page holds — active, inactive
 *  and one per tool — joined by middle dots, which is a paragraph of digits
 *  rather than a sentence. Each is counted from the list below it, and the
 *  definitions are behind the one link, as they are on every other page. */
function summarise(data) {
  const inactive = data.total - data.active;
  summary.innerHTML = `<span>${data.active} end customers, discovered from the vendors</span>`;

  const everywhere = data.sites.filter(
    (s) => s.is_active && TOOLS.every((key) => s.connectors.includes(key)),
  ).length;
  const nowhere = data.sites.filter((s) => s.is_active && !s.connectors.length).length;
  const tiles = [
    {
      n: String(data.active),
      k: "Customers",
      why: inactive ? `${inactive} inactive` : "All active",
      definition:
        "End customers a vendor returns an organisation for. They are discovered by the site " +
        "sync and are never typed in; one a vendor stops listing is kept and marked inactive " +
        "rather than deleted.",
    },
    {
      n: String(everywhere),
      k: "On every tool",
      why: `of ${data.active} customers`,
      definition:
        "Customers every connector holds an organisation for. A customer missing from one is " +
        "not a fault: it may be a tool they do not buy. The Coverage board is where a gap is " +
        "asserted, with the rule it rests on.",
    },
    {
      n: String(nowhere),
      k: "On no tool",
      why: nowhere ? "Nothing to read for them" : "None",
      definition:
        "Active customers no connector holds an organisation for. Nothing can be read about " +
        "them at all, which usually means the site sync has not been run since they were " +
        "onboarded.",
    },
  ];
  renderTiles(tiles);
}

function renderTiles(tiles) {
  const grid = document.getElementById("tiles");
  if (!grid) return;
  grid.innerHTML = tiles
    .map(
      (t) =>
        `<section class="kpi"><span class="meta muted">${escapeHtml(t.k)}</span>` +
        `<span class="value">${escapeHtml(t.n)}</span>` +
        `<small class="muted">${escapeHtml(t.why)}</small></section>`,
    )
    .join("");
  const how = document.getElementById("tiles-how");
  if (!how) return;
  how.innerHTML =
    "<summary>How these are counted</summary>" +
    `<dl class="facts">${tiles
      .map((t) => `<dt>${escapeHtml(t.k)}</dt><dd>${escapeHtml(t.definition)}</dd>`)
      .join("")}</dl>`;
  how.hidden = false;
}

async function load() {
  if (needsClient()) {
    rows.innerHTML = `<tr><td colspan="${COLUMNS}">${chooseClientNotice()}</td></tr>`;
    summary.innerHTML = "";
    return;
  }
  errorBox.hidden = true;
  rows.innerHTML = `<tr><td colspan="${COLUMNS}" class="muted">Loading…</td></tr>`;
  try {
    const query = includeInactive.checked ? "?include_inactive=true" : "";
    const data = await api("GET", `/sites/list${query}`);
    summarise(data);
    document.getElementById("head").innerHTML = head();
    /* The figures above are about the client and do not move with the filter:
       narrowing the list to one tool does not change how many customers there
       are. The filter narrows the rows and says so in the footer. */
    const tool = toolFilter ? toolFilter.value : "";
    const shown = tool ? data.sites.filter((s) => s.connectors.includes(tool)) : data.sites;
    rows.innerHTML = shown.length
      ? shown.map(row).join("")
      : nothingRow(
          COLUMNS,
          tool
            ? `No end customer is on ${CONNECTOR_LABELS[tool]}.`
            : "No end customer has been discovered for this client yet.",
          tool
            ? undefined
            : {
                text: "Sites come from the vendors;",
                command: "python scripts/sync_sites.py --client <slug>",
              },
        );
    const footer = document.getElementById("shown");
    if (footer) {
      footer.textContent = tool
        ? `${shown.length} of ${data.sites.length} customers, on ${CONNECTOR_LABELS[tool]}`
        : `${shown.length} customers`;
    }
  } catch (error) {
    rows.innerHTML = "";
    summary.innerHTML = "";
    errorBox.innerHTML = problem("Couldn't load this client's end customers.", error);
    errorBox.hidden = false;
  }
}

includeInactive.addEventListener("change", load);
if (toolFilter) toolFilter.addEventListener("change", load);
load();

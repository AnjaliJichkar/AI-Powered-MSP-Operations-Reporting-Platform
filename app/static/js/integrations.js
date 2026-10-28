/* Integrations: which tools this franchisee is on, and when each last answered.
 *
 * The page computes nothing. Every value is one the endpoint read out of a
 * row somebody wrote at the time — a credential, a mapping, an audit row —
 * and where there is no row the card says so in words rather than showing a
 * zero or a colour.
 *
 * A card a tool, because what there is to say about one tool is a paragraph
 * and not a cell: what Desk uses it for, what it actually reads from it, how
 * read-only is kept, and when it last answered. The rest — every customer
 * mapped, the last few reads, what went wrong — is a drawer, because it is
 * detail somebody goes looking for rather than six rows of it on the page.
 */

const cards = document.getElementById("cards");
const tiles = document.getElementById("tiles");
const summary = document.getElementById("summary");
const errorBox = document.getElementById("error");
const searchBox = document.getElementById("search-box");
const search = document.getElementById("search");
const drawer = document.getElementById("drawer");
const drawerScrim = document.getElementById("drawer-scrim");
const drawerTitle = document.getElementById("drawer-title");
const drawerBody = document.getElementById("drawer-body");

/** Enough cards that a reader cannot take them in at once. Below it the box
 *  is a control that narrows six things to five. */
const SEARCH_FROM = 6;

let loaded = null;
let sitesByTool = null;
let openedFrom = null;

/** A tool's own line: set up or not, and whether anything has read it lately.
 *
 *  Four states and no fifth. **Not a health check**: "answering" means the
 *  last read we made of it worked, which is a fact about our last read and
 *  not a promise about the next one. */
function state(row, windowDays) {
  if (!row.configured) {
    return { key: "unknown", label: "Not set up", why: "No credential for this franchisee." };
  }
  if (row.last_ok) {
    return {
      key: "ok",
      label: "Answering",
      why: `The last read of it succeeded ${timeWords(row.last_ok)}.`,
    };
  }
  if (row.last_failed) {
    return {
      key: "missing",
      label: "Failing",
      why: `Every read of it in the last ${windowDays} days failed.`,
    };
  }
  return {
    key: "partial",
    label: "No reads",
    why: `Set up, and nothing has read it in the last ${windowDays} days.`,
  };
}

/** The tone each state is drawn in: two of the four are grey on purpose.
 *  "Not set up" and "nothing asked" are not faults and are not coloured as
 *  though they were. */
const TONE = { ok: "good", missing: "bad", partial: "neutral", unknown: "neutral" };

/** The mark and the name, at the size a card's header wants.
 *
 *  A wordmark where the vendor publishes one we hold; we hold icons, so this
 *  is the icon and the name set beside it, which says the same thing and does
 *  not redraw anybody's logo. */
function toolHeading(key) {
  return (
    `<span class="integration-brand">${vendorMark(key)}` +
    `<span class="integration-name">${escapeHtml(vendorName(key))}</span></span>`
  );
}

function card(row, windowDays) {
  const s = state(row, windowDays);
  const chips = row.reads
    .map((what) => `<span class="read-chip">${escapeHtml(what)}</span>`)
    .join("");
  /* Two facts about reading, on one line: when it last answered, and that
     Desk only reads. The second never changes and is said anyway — it is the
     promise the page exists to keep. */
  const read = row.last_ok
    ? `Last read ${timeWords(row.last_ok)}`
    : row.configured
      ? "No reads yet"
      : "Not connected";
  /* Only where it is the exception: this one is mapped by hand rather than
     discovered by the sync. */
  const mapped = row.configured
    ? `${plural(row.sites, "customer")} mapped` +
      (row.mapped_by_hand ? " <span class='muted'>· by hand</span>" : "")
    : "<span class='muted'>Nothing mapped</span>";
  const failure = row.last_failed
    ? `<div class="integration-failure">Last failure ${when(row.last_failed)}: ` +
      `${escapeHtml(row.last_error_words || "the read did not work.")}</div>`
    : "";
  return `<article class="integration-card" data-tool="${escapeHtml(row.key)}"
      data-search="${escapeHtml((vendorName(row.key) + " " + row.role + " " + row.reads.join(" ")).toLowerCase())}">
    <div class="integration-top">${toolHeading(row.key)}
      <span title="${escapeHtml(s.why)}">${stateWord(TONE[s.key], s.label)}</span></div>
    <p class="integration-category">${escapeHtml(row.role)}</p>
    <p class="integration-purpose">${escapeHtml(row.purpose)}</p>
    <div class="read-chips">${chips}</div>
    <p class="integration-meta">${escapeHtml(read)} · Read-only</p>
    <div class="integration-foot">
      <span class="small">${mapped}</span>
      <button type="button" class="btn small" data-details="${escapeHtml(row.key)}">Details</button>
    </div>
    ${failure}
  </article>`;
}

function render(data) {
  const counts = { ok: 0, missing: 0, partial: 0, unknown: 0 };
  for (const row of data.integrations) counts[state(row, data.window_days).key] += 1;
  const setUp = counts.ok + counts.missing + counts.partial;

  subtitleLine([
    `${setUp} of ${data.integrations.length} set up`,
    `read history over the last ${data.window_days} days`,
  ]);

  tiles.innerHTML = [
    { k: "Set up", n: setUp, why: `of ${data.integrations.length} tools`, tone: "" },
    { k: "Answering", n: counts.ok, why: "Last read succeeded", tone: "good" },
    {
      k: "Failing",
      n: counts.missing,
      why: counts.missing ? "Every recent read failed" : "None",
      tone: counts.missing ? "bad" : "",
    },
    {
      k: "No reads",
      n: counts.partial,
      why: counts.partial ? "Set up, nothing asked" : "None",
      tone: "",
    },
  ]
    .map(
      (t) =>
        `<section class="kpi ${t.tone}"><span class="meta muted">${escapeHtml(t.k)}</span>` +
        `<span class="value">${t.n}</span>` +
        `<small class="muted">${escapeHtml(t.why)}</small></section>`
    )
    .join("");

  searchBox.hidden = data.integrations.length < SEARCH_FROM;
  cards.innerHTML = data.integrations.map((row) => card(row, data.window_days)).join("");
}

function narrow(text) {
  const wanted = text.trim().toLowerCase();
  let shown = 0;
  for (const el of cards.querySelectorAll(".integration-card")) {
    const hit = !wanted || el.dataset.search.includes(wanted);
    el.hidden = !hit;
    if (hit) shown += 1;
  }
  const none = document.getElementById("no-match");
  if (none) none.remove();
  if (!shown) {
    cards.insertAdjacentHTML(
      "beforeend",
      `<p class="muted" id="no-match">No tool matches ${escapeHtml(text.trim())}.</p>`
    );
  }
}

/* --- the drawer ------------------------------------------------------------------- */

/** Which customers are on this tool, by name.
 *
 *  Read from the sites list the Sites page already uses rather than from a
 *  second endpoint: the names are the same names, and one list cannot drift
 *  from itself. Asked for once, the first time a drawer is opened. */
async function customersOn(key) {
  if (!sitesByTool) {
    try {
      /* Inactive customers are mapped too, and the card's count is of
         mappings: a list that quietly left them out would be one number on
         the card and a different one under it. */
      const data = await api("GET", "/sites/list?include_inactive=true");
      sitesByTool = {};
      for (const site of data.sites) {
        for (const connector of site.connectors) {
          (sitesByTool[connector] = sitesByTool[connector] || []).push({
            name: site.name,
            active: site.is_active,
          });
        }
      }
    } catch {
      sitesByTool = {};
      return null;
    }
  }
  return (sitesByTool[key] || []).slice().sort((a, b) => a.name.localeCompare(b.name));
}

function block(title, body) {
  return `<section class="drawer-block"><h3>${escapeHtml(title)}</h3>${body}</section>`;
}

function recentList(row) {
  if (!row.recent.length) {
    return `<p class="muted">Nothing has read it in the last ${loaded.window_days} days.</p>`;
  }
  return (
    '<ul class="drawer-reads">' +
    row.recent
      .map((read) => {
        const what = read.ok
          ? "Read"
          : escapeHtml(read.words || "The read did not work.");
        /* The class name is what an engineer reading the log wants; it is
           sent to Inputiv staff only and printed only here. */
        const raw = read.error ? ` <span class="mono muted">${escapeHtml(read.error)}</span>` : "";
        const tool = read.tool ? ` <span class="muted">· ${escapeHtml(read.tool)}</span>` : "";
        return (
          `<li>${stateWord(read.ok ? "good" : "bad", read.ok ? "Answered" : "Failed")}` +
          `<span class="drawer-read-what">${what}${tool}${raw}</span>` +
          `<span class="muted small">${when(read.at)}</span></li>`
        );
      })
      .join("") +
    "</ul>"
  );
}

async function openDrawer(key) {
  const row = loaded.integrations.find((r) => r.key === key);
  if (!row) return;
  openedFrom = document.querySelector(`[data-details="${CSS.escape(key)}"]`);
  drawerTitle.innerHTML = toolHeading(key);
  drawerBody.innerHTML =
    block("Customers mapped", '<p class="muted">Reading the customer list…</p>') +
    block("Last reads", recentList(row)) +
    block("What Desk reads", `<div class="read-chips">${row.reads
      .map((what) => `<span class="read-chip">${escapeHtml(what)}</span>`)
      .join("")}</div>`) +
    block("How read-only is kept", `<p>${escapeHtml(row.scope)}</p>`) +
    block(
      "How it is connected",
      `<p>${escapeHtml(row.setup.how)}</p>` +
        (row.setup.command
          ? `<pre class="drawer-command">${escapeHtml(row.setup.command)}</pre>`
          : "") +
        (row.setup.docs ? `<p class="muted small">${escapeHtml(row.setup.docs)}</p>` : "")
    );
  drawer.hidden = false;
  drawerScrim.hidden = false;
  document.getElementById("drawer-close").focus();

  const names = await customersOn(key);
  const first = drawerBody.querySelector(".drawer-block");
  if (!first) return;
  const body =
    names === null
      ? '<p class="muted">The customer list did not load.</p>'
      : names.length
        ? `<p class="small muted">${plural(names.length, "customer")} on this tool.</p>` +
          '<ul class="drawer-customers">' +
          names
            .map(
              (n) =>
                `<li>${escapeHtml(n.name)}` +
                (n.active ? "" : ' <span class="muted">· inactive</span>') +
                "</li>"
            )
            .join("") +
          "</ul>"
        : row.configured
          ? `<p class="muted">No customer is mapped on it yet.${
              row.mapped_by_hand ? " Each tenant is mapped by hand." : " The site sync maps them."
            }</p>`
          : '<p class="muted">Not set up for this franchisee.</p>';
  first.innerHTML = "<h3>Customers mapped</h3>" + body;
}

function closeDrawer() {
  drawer.hidden = true;
  drawerScrim.hidden = true;
  if (openedFrom && openedFrom.isConnected) openedFrom.focus();
  openedFrom = null;
}

/** The subtitle: spans, so the stylesheet draws the separators. */
function subtitleLine(parts) {
  summary.innerHTML = parts
    .filter(Boolean)
    .map((part) => `<span>${escapeHtml(part)}</span>`)
    .join("");
}

async function load() {
  if (needsClient()) {
    cards.innerHTML = chooseClientNotice();
    return;
  }
  try {
    loaded = await api("GET", "/integrations/list");
    render(loaded);
  } catch (error) {
    cards.innerHTML = "";
    errorBox.innerHTML = problem("The integrations list did not load.", error);
    errorBox.hidden = false;
  }
}

cards.addEventListener("click", (event) => {
  const button = event.target.closest("[data-details]");
  if (button) openDrawer(button.dataset.details);
});
drawerScrim.addEventListener("click", closeDrawer);
document.getElementById("drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !drawer.hidden) closeDrawer();
});
search.addEventListener("input", () => narrow(search.value));

document.addEventListener("DOMContentLoaded", load);

/* Ask: one conversation, streamed.
 *
 * Nothing here names a tenant or an engineer. Who is asking comes from the
 * session cookie and which client from the session row, so every request below
 * carries the question, the thread it belongs to, and nothing else — a body
 * that still named either would be refused (422) rather than quietly ignored.
 *
 * Two things about this page are decided by the room it is shown in. A model's
 * text is markdown, so it is parsed and sanitised before it is inserted: a
 * device called `<img src=x onerror=...>` renders as characters. And nothing is
 * shortened — the tool panel prints its arguments and results whole, and a wide
 * table scrolls inside the answer — because half a value on a wall is worse
 * than a scrollbar: the reader cannot tell whether the rest was cut off or was
 * never there.
 */

"use strict";

(function () {
  const feed = document.getElementById("feed");
  /* The column the feed and the composer share. While it holds no
     conversation it carries `data-empty`, which is what makes the question box
     the thing on the page rather than a strip along the bottom. */
  const askMain = document.querySelector(".ask-main");
  const threadList = document.getElementById("threads");
  const newThreadButton = document.getElementById("new-thread");
  const newThreadTop = document.getElementById("new-thread-top");
  const railToggle = document.getElementById("toggle-rail");
  const askRoot = document.getElementById("ask");
  const form = document.getElementById("ask-form");
  const messageInput = document.getElementById("message");
  const sendButton = document.getElementById("send");
  const hint = document.getElementById("hint");

  const DEFAULT_HINT = hint.textContent;

  /* The thread every question is sent with. Null means the next question
     starts one; the server creates it and names it in the first event. A
     thread belongs to one client, and switching client reloads the page, so
     there is no stale id to carry across. */
  let conversationId = null;
  let busy = false;

  /* --- text ------------------------------------------------------------- */

  /* Markdown, then sanitise. If either CDN script failed to load the answer is
     still shown, as escaped plain text: a blocked CDN degrades, never hides an
     answer the engine already produced. */
  function renderMarkdown(text) {
    if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
      return "<pre>" + escapeHtml(text) + "</pre>";
    }
    return DOMPurify.sanitize(marked.parse(text, { breaks: true }));
  }

  /* An answer into its box. Every table is then wrapped in a scroller, because
     a model asked for twelve columns of device facts will produce them and the
     alternative is a page that scrolls sideways under the composer. */
  function setAnswer(node, text) {
    node.innerHTML = renderMarkdown(text);
    for (const table of node.querySelectorAll("table")) {
      const box = document.createElement("div");
      box.className = "scroller";
      table.parentNode.insertBefore(box, table);
      box.appendChild(table);
    }
  }

  /* Following the newest answer down, without fighting a reader who has gone
     back up to read something.
   *
   * Asking a question, or an answer landing, always goes to the bottom: that is
   * what the reader just did and where the new thing is. A stream, which paints
   * many times a second, follows only while they are already at the bottom —
   * `nearBottom` — so scrolling up to re-read a table is not undone by the next
   * token. Coming back to the bottom starts it following again. */
  const FOLLOW_SLACK = 120;

  function nearBottom() {
    return feed.scrollHeight - feed.scrollTop - feed.clientHeight <= FOLLOW_SLACK;
  }

  function scrollToEnd(node, smooth) {
    node.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "end" });
  }

  /** While a stream is painting: only when the reader has not moved away. */
  function followEnd(node) {
    if (nearBottom()) scrollToEnd(node);
  }

  /* --- the thread list --------------------------------------------------- */

  function note(text) {
    return '<p class="small muted">' + escapeHtml(text) + "</p>";
  }

  function renderThreads(threads) {
    threadList.innerHTML = "";
    /* No conversations, nothing to open: the history button is not drawn
       rather than drawn and opening an empty column. */
    if (railToggle) railToggle.hidden = threads.length === 0;
    if (threads.length === 0) {
      setRail(false);
      threadList.innerHTML = note("No conversations for this client yet.");
      return;
    }
    for (const thread of threads) {
      const row = document.createElement("div");
      row.className = "list-row thread-row" + (thread.id === conversationId ? " is-on" : "");
      row.dataset.id = thread.id;
      /* The title, then one line of facts about the thread. Every thread in
         this list belongs to the session's client — that is what the endpoint
         returns — so naming it here says which estate the answers were about
         without the page asserting a client of its own. */
      const facts = [
        timeWords(thread.updated_at),
        DESK.client ? DESK.client.name : null,
        thread.turns ? plural(thread.turns, "question") : null,
      ]
        .filter(Boolean)
        .map((part) => escapeHtml(part))
        .join(" · ");
      const title = thread.title || "Untitled conversation";
      row.innerHTML =
        '<button type="button" class="thread" data-open' +
        (thread.id === conversationId ? ' aria-current="true"' : "") +
        /* The whole question is the tooltip. The rail shortens it to two lines
           in CSS and nowhere else, so nothing is lost by the shortening. */
        ' title="' + escapeHtml(title) + '"' +
        '><span class="what">' +
        escapeHtml(title) +
        '</span><span class="meta">' +
        facts +
        "</span></button>" +
        '<button type="button" class="action archive" data-archive title="Archive this conversation"' +
        ' aria-label="Archive this conversation">&#x2715;</button>';
      threadList.appendChild(row);
    }
  }

  function markCurrentThread() {
    for (const row of threadList.querySelectorAll(".thread-row")) {
      const button = row.querySelector(".thread");
      const current = row.dataset.id === conversationId;
      row.classList.toggle("is-on", current);
      if (current) button.setAttribute("aria-current", "true");
      else button.removeAttribute("aria-current");
    }
  }

  /* Returns the threads, so the caller can decide whether to resume one. */
  async function loadThreads() {
    try {
      /* Your own threads for the session's client. The server takes both from
         the session, so there is nothing to ask for here. */
      const threads = await api("GET", "/conversations");
      renderThreads(threads);
      return threads;
    } catch (error) {
      threadList.innerHTML = note("Could not load conversations: " + error.message);
      return [];
    }
  }

  /* --- the feed ----------------------------------------------------------- */

  /** Questions somebody can press rather than type.
   *
   *  Fixed, because they are examples of the shape a question takes, not a
   *  guess at what this client needs — a suggestion drawn from their own data
   *  would be a finding, and a finding belongs on the page that computed it. */
  const SUGGESTIONS = [
    "Which devices haven't checked in for two weeks?",
    "What's alerting right now?",
    "How many servers does this client have?",
    "Which machines have no protection agent?",
  ];

  function showEmpty(title) {
    /* The arrow the shell already draws, cloned rather than written out again:
       every icon in the product is in `_icons.html`. */
    const arrow = iconMarkup("icon-page");
    const cards = SUGGESTIONS.map(
      (q) =>
        `<button type="button" class="suggestion" data-ask="${escapeHtml(q)}">` +
        `<span>${escapeHtml(q)}</span><span class="go" aria-hidden="true">${arrow}</span>` +
        `</button>`,
    ).join("");
    feed.innerHTML =
      '<div class="empty">' +
      (title ? `<p class="small muted">${escapeHtml(title)}</p>` : "") +
      '<div class="kpis" id="ask-tiles"></div>' +
      '<details class="small how-counted" id="ask-how" hidden></details>' +
      '<p class="section-label">Start with one of these</p>' +
      `<div class="suggestions">${cards}</div>` +
      "</div>";
    askMain.dataset.empty = "true";
    for (const button of feed.querySelectorAll("[data-ask]")) {
      button.addEventListener("click", () => {
        messageInput.value = button.dataset.ask;
        messageInput.focus();
        messageInput.dispatchEvent(new Event("input"));
      });
    }
    void loadTiles();
  }

  /** The figures this client already has, from the pages that computed them.
   *
   *  Nothing here is worked out on this page and nothing is invented: each
   *  tile is one number from one endpoint, with that endpoint's own words
   *  under it and a link to the page it belongs to. A tile whose read fails is
   *  left out rather than shown as a zero. */
  async function loadTiles() {
    const grid = document.getElementById("ask-tiles");
    if (!grid || !DESK.client) return;
    const [board, tickets] = await Promise.all([
      api("GET", "/coverage/board").catch(() => null),
      api("GET", "/tickets/statistics").catch(() => null),
    ]);
    /* A caption is one short line. The method behind a figure — what a clock
       is measured from, which population a share is of, when the board was
       taken — is the whole definition, and the whole definition goes behind
       the one link under the tiles. A caption nobody finishes reading is a
       caption that has told them nothing. */
    const tiles = [];
    const defs = [];
    if (board && board.snapshot && board.snapshot.has_board) {
      tiles.push({
        n: String(board.managed),
        k: "Computers",
        why: `Across ${board.sites.length} customers`,
        href: "/coverage",
      });
      defs.push([
        "Computers",
        `Every computer NinjaOne manages for this client, as the coverage board stood ${board.snapshot.age_words}.`,
      ]);
      const protection = (board.tools || []).find((t) => t.key === "bitdefender");
      if (protection && protection.assessed && protection.covered_percent !== null) {
        tiles.push({
          n: `${protection.covered_percent}%`,
          k: "Endpoint protection",
          why: `${protection.covered} of ${protection.assessable} computers`,
          href: "/coverage",
        });
        defs.push([
          "Endpoint protection",
          "The share of computers that should carry a protection agent where Bitdefender holds a current record of the machine.",
        ]);
      }
    }
    const captions = {
      open: { k: "Open tickets", why: board ? `Across ${board.sites.length} customers` : "" },
      breached: { k: "Past SLA", why: "Need attention first" },
    };
    for (const key of ["open", "breached"]) {
      const card = tickets && (tickets.cards || []).find((c) => c.key === key);
      if (!card) continue;
      tiles.push({ n: String(card.value), k: captions[key].k, why: captions[key].why, href: "/tickets" });
      defs.push([captions[key].k, card.definition]);
    }
    if (!tiles.length) {
      grid.remove();
      return;
    }
    /* The shell's own KPI card, so a figure reads the same here as on the page
       it came from — and a link, because every one of them belongs to a page
       that can say more about it than a tile can. */
    grid.innerHTML = tiles
      .map(
        (t) =>
          `<a class="kpi" href="${escapeHtml(t.href)}">` +
          `<span class="meta muted">${escapeHtml(t.k)}</span>` +
          `<span class="value">${escapeHtml(t.n)}</span>` +
          (t.why ? `<small class="muted">${escapeHtml(t.why)}</small>` : "") +
          `</a>`,
      )
      .join("");
    const how = document.getElementById("ask-how");
    if (how) {
      how.innerHTML =
        "<summary>How these are counted</summary>" +
        `<dl class="facts">${defs
          .map(([term, definition]) => `<dt>${escapeHtml(term)}</dt><dd>${escapeHtml(definition)}</dd>`)
          .join("")}</dl>`;
      how.hidden = false;
    }
  }

  /* Stored messages, replayed as turns. A user message with text opens a turn;
     tool-only messages (a tool_use with no prose, a tool_result) are plumbing
     and are not shown. */
  function renderHistory(messages) {
    feed.innerHTML = "";
    let turn = null;
    for (const message of messages) {
      if (message.role === "user") {
        if (!message.text) continue;
        turn = document.createElement("div");
        turn.className = "turn";
        turn.innerHTML = '<div class="question"></div>';
        turn.firstChild.textContent = message.text;
        feed.appendChild(turn);
      } else if (message.text && turn) {
        const answer = document.createElement("div");
        answer.className = "answer";
        setAnswer(answer, message.text);
        turn.appendChild(answer);
        /* Who answered and when. A stored message carries its own time now, so
           a conversation reopened a week later says when each answer was given
           rather than only that Desk gave it. */
        const said = document.createElement("div");
        said.className = "meta";
        said.innerHTML = `<span>Desk</span>${message.at ? `<span>${when(message.at)}</span>` : ""}`;
        turn.appendChild(said);
      }
    }
    if (feed.children.length === 0) showEmpty("Nothing was said in this conversation yet.");
    else scrollToEnd(feed.lastElementChild);
  }

  async function openThread(id) {
    try {
      const messages = await api("GET", "/conversations/" + encodeURIComponent(id) + "/messages");
      conversationId = id;
      renderHistory(messages);
      markCurrentThread();
      messageInput.focus();
    } catch (error) {
      conversationId = null;
      showEmpty("Could not open that conversation: " + error.message);
    }
  }

  /* "New conversation" clears the thread and keeps the client: the client is
     the session's, chosen deliberately and shown in the header on every page,
     so there is nothing stale to guard against. */
  function startFresh() {
    conversationId = null;
    showEmpty("Ask your first question.");
    markCurrentThread();
    messageInput.focus();
  }

  /* --- the tool-call panel ------------------------------------------------- */

  /* Arguments as they were sent, one per line and whole. A value that is not a
     string is written as JSON, so a list reads as a list rather than as
     `[object Object]`. */
  function argumentLines(args) {
    return Object.entries(args || {})
      .map(([name, value]) =>
        name + " = " + (typeof value === "string" ? value : JSON.stringify(value))
      )
      .join("\n");
  }

  /* One call, live or finished, into an element that may already hold an
     earlier state of it. While it runs there is no outcome yet, so the time
     column says so; when it ends the same element is repainted. */
  function paintToolCall(node, call) {
    const running = !call.outcome;
    const failed = !running && call.outcome !== "success";
    const args = argumentLines(call.arguments);
    const result = running
      ? ""
      : String(call.result_summary || call.error_message || call.outcome);

    node.className = "tool-call" + (failed ? " failed" : "");
    node.innerHTML =
      '<div class="head">' +
      '<span class="name">' + escapeHtml(call.tool_name) + "</span>" +
      (call.connector ? '<span class="connector">' + escapeHtml(call.connector) + "</span>" : "") +
      (failed ? stateWord("bad", call.outcome) : "") +
      '<span class="spacer"></span>' +
      '<span class="took">' +
      escapeHtml(running ? "running…" : duration(call.duration_ms)) +
      "</span></div>" +
      (args ? '<div class="args">' + escapeHtml(args) + "</div>" : "") +
      (result ? '<div class="result">' + escapeHtml(result) + "</div>" : "");
  }

  function toolCallNode(call) {
    const node = document.createElement("div");
    paintToolCall(node, call);
    return node;
  }

  function callsLabel(count) {
    if (count === 0) return "No tools were called";
    return count === 1 ? "1 tool call" : count + " tool calls";
  }

  function toolsPanel(calls) {
    const panel = document.createElement("details");
    panel.className = "tools";
    const summary = document.createElement("summary");
    summary.textContent = callsLabel((calls || []).length);
    panel.appendChild(summary);
    for (const call of calls || []) panel.appendChild(toolCallNode(call));
    return panel;
  }

  /* The panel sits between the answer and its meta line, whether it is the
     live one or the one the finished run replaces it with. */
  function setToolsPanel(turn, panel) {
    const existing = turn.querySelector(".tools");
    if (existing) existing.remove();
    turn.insertBefore(panel, turn.querySelector(".meta"));
  }

  /* --- a turn -------------------------------------------------------------- */

  const STATUS_WORDS = {
    capped: ["warn", "stopped at the tool-call limit"],
    truncated: ["warn", "answer cut off"],
    timeout: ["warn", "timed out"],
    refused: ["bad", "declined"],
    error: ["bad", "failed"],
  };

  function statusChip(status) {
    if (status === "ok") return "";
    const known = STATUS_WORDS[status] || ["warn", status];
    return stateWord(known[0], known[1]);
  }

  function ratingBlock(auditLogId) {
    if (!auditLogId) return "";
    return (
      '<span class="rating" data-audit="' + escapeHtml(auditLogId) + '">' +
      "<span>Useful?</span>" +
      '<button type="button" class="action" data-rating="up" title="Useful"' +
      ' aria-label="Useful">&#128077;</button>' +
      '<button type="button" class="action" data-rating="down" title="Not useful"' +
      ' aria-label="Not useful">&#128078;</button>' +
      '<span data-rate-status></span></span>'
    );
  }

  function addTurn(question) {
    const turn = document.createElement("div");
    turn.className = "turn";
    turn.innerHTML =
      '<div class="question"></div><div class="answer" hidden></div><div class="meta"></div>';
    turn.querySelector(".question").textContent = question;
    if (feed.querySelector(".empty")) {
      feed.innerHTML = "";
      delete askMain.dataset.empty;
    }
    feed.appendChild(turn);
    scrollToEnd(turn, true);
    return turn;
  }

  function fillTurn(turn, result) {
    const answer = turn.querySelector(".answer");
    answer.hidden = false;
    setAnswer(answer, result.text);
    setToolsPanel(turn, toolsPanel(result.tool_calls));
    /* Who answered, when, and what it cost: one line, in that order, with the
       middle dots drawn by the stylesheet so a fact that is missing leaves no
       stray one behind. */
    turn.querySelector(".meta").innerHTML =
      "<span>Desk</span>" +
      "<span>" + escapeHtml(timeWords(new Date().toISOString())) + "</span>" +
      "<span>" + escapeHtml(result.model) + "</span>" +
      "<span>" + escapeHtml(duration(result.duration_ms)) + "</span>" +
      "<span>" + escapeHtml(result.input_tokens) + " in / " +
      escapeHtml(result.output_tokens) + " out tokens</span>" +
      statusChip(result.status) +
      ratingBlock(result.audit_log_id);
    scrollToEnd(turn, true);
  }

  function failTurn(turn, message) {
    const answer = turn.querySelector(".answer");
    answer.hidden = false;
    answer.innerHTML = '<div class="notice bad"></div>';
    answer.firstChild.textContent = message;
    turn.querySelector(".meta").innerHTML = stateWord("bad", "failed");
  }

  /* Progress for one turn: tool calls appear as they start and settle as they
     finish, and the model's text types out. All of it is a preview — when
     `done` arrives, fillTurn() replaces the lot with the answer of record. */
  function liveTurn(turn, label) {
    const answer = turn.querySelector(".answer");
    const meta = turn.querySelector(".meta");
    answer.hidden = false;
    answer.innerHTML = "";

    /* Closed, like the finished one. A panel that opens itself while the
       answer is being written moves the words somebody is reading; the line
       under the answer says what is being checked meanwhile, and the
       disclosure is there for anybody who wants to watch it. */
    const panel = document.createElement("details");
    panel.className = "tools";
    panel.innerHTML = "<summary></summary>";
    const summary = panel.querySelector("summary");
    setToolsPanel(turn, panel);

    const rows = new Map();
    let text = "";
    let painting = false;

    function status(words) {
      meta.innerHTML = "<span></span>";
      meta.firstChild.textContent = words;
    }

    function paint() {
      painting = false;
      setAnswer(answer, text);
      /* Where the next character will appear, so a pause reads as thinking
         rather than as a finished answer. */
      const cursor = document.createElement("span");
      cursor.className = "cursor";
      (answer.lastElementChild || answer).appendChild(cursor);
      followEnd(turn);
    }

    function rowFor(id) {
      let row = rows.get(id);
      if (!row) {
        row = document.createElement("div");
        rows.set(id, row);
        panel.appendChild(row);
        summary.textContent = callsLabel(rows.size);
      }
      return row;
    }

    summary.textContent = callsLabel(0);

    return {
      started() {
        status("Asking about " + label + "…");
      },
      modelCall(index) {
        /* Text written before a tool call is the model thinking aloud, not the
           answer: the answer is what the last model call writes. */
        if (index > 1) {
          text = "";
          paint();
          status("Reading the results…");
        }
      },
      text(piece) {
        text += piece;
        status("Answering…");
        /* One repaint per frame however fast the pieces arrive. */
        if (!painting) {
          painting = true;
          requestAnimationFrame(paint);
        }
      },
      toolStart(call) {
        paintToolCall(rowFor(call.id), call);
        status(
          rows.size === 1 ? "Checking " + call.tool_name + "…" : "Running " + rows.size + " checks…"
        );
        followEnd(turn);
      },
      /* A tool that never started — an unknown name, refused before it ran —
         still gets a row here rather than disappearing. */
      toolEnd(call) {
        paintToolCall(rowFor(call.id), call);
      },
      webSearch(query) {
        status("Searching the web: " + query);
      },
    };
  }

  /* --- the stream ----------------------------------------------------------- */

  /* Read a server-sent-event stream from a fetch() response. EventSource cannot
     POST, and the question belongs in a body, not a URL. */
  async function readEvents(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        let name = "message";
        const data = [];
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) name = line.slice(6).trim();
          else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
          /* a line starting with ":" is a keep-alive comment */
        }
        if (data.length) onEvent(name, JSON.parse(data.join("\n")));
      }
    }
  }

  async function ask(question) {
    const label = DESK.client ? DESK.client.name : "this client";
    const turn = addTurn(question);
    busy = true;
    sendButton.textContent = "Asking…";
    syncComposer();

    try {
      /* Every message carries the thread it belongs to and nothing else. The
         server refuses a thread that is not this session's client's or this
         account's, so a stale id cannot cross either. */
      const body = { message: question };
      if (conversationId) body.conversation_id = conversationId;
      const response = await fetch("/chat/stream", {
        method: "POST",
        headers: unsafeHeaders({ Accept: "text/event-stream" }),
        body: JSON.stringify(body),
      });
      if (!signedIn(response)) return;
      if (!response.ok) {
        failTurn(turn, await errorDetail(response));
        return;
      }

      const startedThread = !conversationId;
      const live = liveTurn(turn, label);
      let result = null;
      let failure = null;
      await readEvents(response, (name, data) => {
        if (name === "started") {
          /* Known from the first event, so a dropped connection still leaves
             the page pointing at the thread the run is being saved to. */
          if (data.conversation_id) conversationId = data.conversation_id;
          live.started();
        } else if (name === "model_call") live.modelCall(data.index);
        else if (name === "text") live.text(data.text);
        else if (name === "tool_start") live.toolStart(data);
        else if (name === "tool_end") live.toolEnd(data);
        else if (name === "web_search") live.webSearch(data.query);
        else if (name === "done") result = data;
        else if (name === "error") failure = data.detail;
      });

      if (result) {
        if (result.conversation_id) conversationId = result.conversation_id;
        fillTurn(turn, result);
      } else {
        failTurn(
          turn,
          failure ||
            "The connection dropped before the answer finished. The run carries on " +
              "without the page: reopen this conversation in a moment to see it."
        );
      }
      if (startedThread) await loadThreads();
      else markCurrentThread();
    } catch (error) {
      failTurn(turn, "Could not reach the engine: " + error.message);
    } finally {
      busy = false;
      sendButton.textContent = "Ask";
      syncComposer();
    }
  }

  /* --- the composer ---------------------------------------------------------- */

  /* Live only once the session is pointed at a client, and never twice at
     once: a second question while one is running would be answered against a
     thread the first has not finished writing to. */
  function syncComposer() {
    const ready = Boolean(DESK.client);
    messageInput.disabled = !ready || busy;
    sendButton.disabled = !ready || busy;
    newThreadButton.disabled = !ready || busy;
    if (newThreadTop) newThreadTop.disabled = !ready || busy;
    hint.textContent = ready ? DEFAULT_HINT : "Choose a client in the header to begin.";
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = messageInput.value.trim();
    if (!question || busy) return;
    messageInput.value = "";
    messageInput.style.height = "auto";
    ask(question);
  });

  messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  /* Grows with the question up to the height the stylesheet caps it at, so a
     long one is read in full before it is sent. */
  messageInput.addEventListener("input", () => {
    messageInput.style.height = "auto";
    messageInput.style.height = Math.min(messageInput.scrollHeight, 200) + "px";
  });

  function setRail(open) {
    if (!askRoot || !railToggle) return;
    askRoot.dataset.rail = open ? "open" : "closed";
    railToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  newThreadButton.addEventListener("click", startFresh);
  if (newThreadTop) newThreadTop.addEventListener("click", startFresh);
  if (railToggle) {
    railToggle.addEventListener("click", () => setRail(askRoot.dataset.rail !== "open"));
  }

  threadList.addEventListener("click", async (event) => {
    const row = event.target.closest(".thread-row");
    if (!row) return;
    if (event.target.closest("[data-archive]")) {
      const sure = await ask({
        title: "Archive this conversation?",
        body: "It leaves this list. Nothing is deleted, and the audit trail is untouched.",
        confirm: "Archive",
        tone: "destructive",
      });
      if (sure === null) return;
      try {
        await api("POST", "/conversations/" + encodeURIComponent(row.dataset.id) + "/archive", {});
      } catch (error) {
        await tell("That conversation could not be archived", error.message);
        return;
      }
      if (row.dataset.id === conversationId) {
        conversationId = null;
        showEmpty();
      }
      await loadThreads();
      return;
    }
    await openThread(row.dataset.id);
  });

  /* Ratings are delegated: answers are added long after this is bound. */
  feed.addEventListener("click", async (event) => {
    const button = event.target.closest(".rating button");
    if (!button) return;
    const holder = button.closest(".rating");
    const state = holder.querySelector("[data-rate-status]");
    const rating = button.dataset.rating;
    let comment = null;
    if (rating === "down") {
      const said = await ask({
        title: "What was wrong with it?",
        body: "Optional. It goes on the record with your name, beside the answer it is about.",
        reason: { label: "What was wrong", placeholder: "Leave empty to just mark it down" },
        confirm: "Send",
      });
      if (said === null) return;
      comment = said.trim() || null;
    }

    /* The chosen thumb stops being quiet, so which way it was rated is
       readable from across a room. */
    for (const other of holder.querySelectorAll("button")) other.classList.add("quiet");
    button.classList.remove("quiet");
    state.textContent = "saving…";

    try {
      await api("POST", "/feedback", {
        audit_log_id: Number(holder.dataset.audit),
        rating: rating,
        comment: comment,
      });
      state.textContent = "thanks";
    } catch (error) {
      state.textContent = "could not save: " + error.message;
    }
  });

  /* --- open ------------------------------------------------------------------ */

  (async function start() {
    /* An Inputiv session that has not chosen a client yet: every endpoint below
       would answer 409, so the page says so once rather than letting the
       thread list and the first question fail separately. */
    if (needsClient()) {
      feed.innerHTML = chooseClientNotice();
      threadList.innerHTML = note("Choose a client to see its conversations.");
      syncComposer();
      return;
    }
    syncComposer();
    try {
      /* On open: resume the most recent thread for the session's client. */
      const threads = await loadThreads();
      if (threads.length > 0) await openThread(threads[0].id);
      else showEmpty();
      messageInput.focus();
    } catch (error) {
      showEmpty("Could not reach the engine: " + error.message);
    }
  })();
})();

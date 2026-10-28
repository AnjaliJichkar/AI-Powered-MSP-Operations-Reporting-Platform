/* The words. One mapping from an internal state name to what it means.
 *
 * Coverage, Devices and Tickets all print the same underlying states, and
 * before this they each held their own half of the vocabulary: the board said
 * "RMM agent down", the device list said "rmm_stale", the ticket queue printed
 * "first_response overdue by 187.5 h" straight from the API. Three pages, three
 * dialects, one product. So every state a page shows is named here, once, with:
 *
 *   label   the plain-language name a person reads
 *   reason  one line saying what it means — shown under the label or as its
 *           tooltip, never left for the reader to infer from a colour
 *   tone    which of the four status colours the dot takes, the *second*
 *           signal and never the first
 *   mark    a glyph, so the state survives a projector, a photocopy, a
 *           colour-blind reader and a washed-out beamer. Colour is never the
 *           only signal on these pages; that rule is enforced here, by giving
 *           every state a mark it cannot be drawn without.
 *
 * Nothing in this file talks to the API or knows a page's markup. It is
 * vocabulary and two derivations built from it, and it is loaded before every
 * page script.
 */

/* --- the vocabulary ---------------------------------------------------------- */

const STATES = {
  /* `Device.status`, as the daily resolution writes it. */
  device: {
    active: {
      label: "Reporting",
      reason: "NinjaOne has heard from this machine recently.",
      tone: "good",
      mark: "✓",
    },
    rmm_stale: {
      label: "RMM not reporting",
      reason: "NinjaOne has not heard from this machine, and no other tool has either.",
      tone: "warn",
      mark: "!",
    },
    rmm_agent_down: {
      label: "RMM agent down",
      reason: "Another tool still hears from this machine, so it is alive and the NinjaOne agent is not.",
      tone: "bad",
      mark: "✕",
    },
    retired: {
      label: "Retired",
      reason: "No tool has returned a record of this machine for long enough that it is treated as gone. Nothing is deleted.",
      tone: "neutral",
      mark: "·",
    },
  },

  /* The coverage board's per-tool verdict (`app/coverage/config.py`). */
  coverage: {
    covered: {
      label: "Covered",
      reason: "The tool holds this machine and what it holds is current.",
      tone: "good",
      mark: "✓",
    },
    stale: {
      label: "Out of date",
      reason: "The tool holds this machine but has not heard from it recently enough.",
      tone: "warn",
      mark: "!",
    },
    absent: {
      label: "Not installed",
      reason: "The tool answered for this site and has no record of this machine, or one with no agent on it.",
      tone: "bad",
      mark: "✕",
    },
    unmatched: {
      label: "Cannot tell",
      reason: "Nothing could pair this machine with a record without guessing, so it was left unpaired.",
      tone: "warn",
      mark: "?",
    },
    rmm_stale: {
      label: "RMM not reporting",
      reason: "NinjaOne has not heard from this machine, and no other tool has either.",
      tone: "warn",
      mark: "!",
    },
    rmm_agent_down: {
      label: "RMM agent down",
      reason: "Another tool still hears from this machine, so it is alive and the NinjaOne agent is not.",
      tone: "bad",
      mark: "✕",
    },
    excluded: {
      label: "Not expected",
      reason: "Somebody recorded, with a reason, that this machine is not meant to have this tool.",
      tone: "neutral",
      mark: "·",
    },
    not_assessed: {
      label: "Not assessed",
      reason: "The tool is not connected for this customer, has no company for this site, or did not answer. Not the same as zero.",
      tone: "neutral",
      mark: "–",
    },
    not_in_scope: {
      label: "Not in backup scope",
      reason: "This customer does not back this machine up. Backup is sold per machine, so a laptop at a customer who backs up servers only is not a gap; it is left out of the percentage and counted on its own.",
      tone: "neutral",
      mark: "·",
    },
  },

  /* How a vendor's record was tied to a machine (`DeviceVendorLink.matched_by`,
     `ToolState.matched_by`). */
  match: {
    ninjaone: {
      label: "the machine's own RMM record",
      reason: "This is the NinjaOne record the machine is built from.",
      tone: "neutral",
      mark: "",
    },
    exact: {
      label: "same name",
      reason: "The tool's record carries the same hostname at the same site.",
      tone: "neutral",
      mark: "",
    },
    netbios15: {
      label: "same short name",
      reason: "The names agree over the first fifteen characters, which is all Windows lets some tools see.",
      tone: "neutral",
      mark: "",
    },
    mac: {
      label: "same network card",
      reason: "The names differ; a MAC address no other machine at the site reports paired them.",
      tone: "neutral",
      mark: "",
    },
    serial: {
      label: "same serial number",
      reason: "The names differ; a serial number no other machine at the site reports paired them.",
      tone: "neutral",
      mark: "",
    },
    vendor_only: {
      label: "not paired",
      reason: "Nothing paired this record with a machine, so it stands as a machine of its own.",
      tone: "warn",
      mark: "!",
    },
    manual: {
      label: "paired by a person",
      reason: "Somebody tied this record to this machine by hand, with a reason, and the daily job leaves it alone.",
      tone: "accent",
      mark: "✎",
    },
  },

  /* `sla.state` from `app/tickets/workspace.py`. */
  sla: {
    breached: {
      label: "Breached",
      reason: "An SLA clock on this ticket has already run out.",
      tone: "bad",
      mark: "✕",
    },
    due_soon: {
      label: "Due soon",
      reason: "An SLA clock on this ticket runs out within a day.",
      tone: "warn",
      mark: "!",
    },
    ok: {
      label: "In time",
      reason: "Every SLA clock on this ticket still has more than a day on it.",
      tone: "good",
      mark: "✓",
    },
    paused: {
      label: "Clock stopped",
      reason:
        "This ticket's status is one that pauses the clock — waiting on the customer, on a part, "
        + "on a vendor. How long it has been waiting is not something Autotask records, so nothing "
        + "is judged against it.",
      tone: "neutral",
      mark: "⏸",
    },
    none: {
      label: "No target",
      reason: "This franchisee has set no target for this ticket's priority, so no clock is run.",
      tone: "neutral",
      mark: "–",
    },
  },

  /* How sure we are which machine a ticket is about. */
  confidence: {
    high: {
      label: "Confident",
      reason: "One machine matched the ticket exactly, or an engineer chose it.",
      tone: "good",
      mark: "✓",
    },
    medium: {
      label: "Fairly sure",
      reason: "One machine matched, but on a short name or on who was logged in rather than on an exact name.",
      tone: "warn",
      mark: "!",
    },
    low: {
      label: "A guess",
      reason: "Several machines matched equally well. Somebody should say which.",
      tone: "bad",
      mark: "?",
    },
  },

  /* How the machine was found (`device.method`). */
  resolution: {
    hostname: {
      label: "a machine name in the ticket",
      reason: "A word in the ticket's text is this machine's name.",
      tone: "good",
      mark: "✓",
    },
    contact: {
      label: "who the contact last signed in on",
      reason: "NinjaOne's last logged-on user for this machine is the person who raised the ticket.",
      tone: "warn",
      mark: "!",
    },
    engineer: {
      label: "an engineer said so",
      reason: "Somebody chose this machine by hand; the choice is recorded and stands.",
      tone: "accent",
      mark: "✎",
    },
    none: {
      label: "nothing identified it",
      reason: "No machine name and no contact match; nobody has chosen one.",
      tone: "neutral",
      mark: "?",
    },
  },

  /* What an engineer has done with a plan step. */
  review: {
    proposed: {
      label: "Not reviewed",
      reason: "Nobody has accepted, edited or rejected this step yet.",
      tone: "neutral",
      mark: "",
    },
    accepted: {
      label: "Accepted",
      reason: "An engineer read this step and kept it as written.",
      tone: "good",
      mark: "✓",
    },
    edited: {
      label: "Edited",
      reason: "An engineer rewrote this step; the original is kept beside it.",
      tone: "accent",
      mark: "✎",
    },
    rejected: {
      label: "Rejected",
      reason: "An engineer read this step and struck it out.",
      tone: "bad",
      mark: "✕",
    },
  },

  /* The one overall mark per machine. Derived on the page from the states
     above — see `deviceHealth` — because nothing stores a verdict. */
  health: {
    healthy: {
      label: "Healthy",
      reason: "Reporting to the RMM, with every tool present.",
      tone: "good",
      mark: "✓",
    },
    attention: {
      label: "Needs attention",
      reason: "Something is missing or out of date, but the machine is not unprotected.",
      tone: "warn",
      mark: "!",
    },
    action: {
      label: "Action needed",
      reason: "Either nothing is protecting this machine, or the RMM agent is down on a machine that is still running.",
      tone: "bad",
      mark: "✕",
    },
    retired: {
      label: "Retired",
      reason: "No tool has returned a record of it for long enough that it is treated as gone.",
      tone: "neutral",
      mark: "·",
    },
  },

  /* NinjaOne's `nodeClass`, which is an enum and reads like one. Only the
     classes `is_agent_class` counts as computers appear on these pages; an
     unfamiliar one is tidied by `machineKind` rather than printed raw. */
  machine: {
    WINDOWS_WORKSTATION: { label: "Windows desktop or laptop", reason: "", tone: "neutral", mark: "·" },
    WINDOWS_SERVER: { label: "Windows server", reason: "", tone: "neutral", mark: "·" },
    MAC_WORKSTATION: { label: "Mac", reason: "", tone: "neutral", mark: "·" },
    LINUX_WORKSTATION: { label: "Linux desktop", reason: "", tone: "neutral", mark: "·" },
    LINUX_SERVER: { label: "Linux server", reason: "", tone: "neutral", mark: "·" },
    VMWARE_VM_GUEST: { label: "Virtual machine on VMware", reason: "", tone: "neutral", mark: "·" },
    HYPERV_VMM_GUEST: { label: "Virtual machine on Hyper-V", reason: "", tone: "neutral", mark: "·" },
    VMWARE_VM_HOST: { label: "VMware host", reason: "", tone: "neutral", mark: "·" },
    HYPERV_VMM_HOST: { label: "Hyper-V host", reason: "", tone: "neutral", mark: "·" },
    NMS_SWITCH: { label: "Network switch", reason: "", tone: "neutral", mark: "·" },
    NMS_ROUTER: { label: "Router", reason: "", tone: "neutral", mark: "·" },
    NMS_FIREWALL: { label: "Firewall", reason: "", tone: "neutral", mark: "·" },
    NMS_PRINTER: { label: "Printer", reason: "", tone: "neutral", mark: "·" },
    NMS_SERVER: { label: "Network appliance", reason: "", tone: "neutral", mark: "·" },
    NMS_OTHER: { label: "Other network device", reason: "", tone: "neutral", mark: "·" },
  },

  /* Whether a tool holds a record of a machine at all. */
  presence: {
    present: { label: "has it", reason: "", tone: "good", mark: "✓" },
    missing: { label: "does not have it", reason: "", tone: "bad", mark: "✕" },
  },

  /* One criterion of the Autopilot gate.
   *
   * "Not measured" is deliberately its own state rather than a shade of fail:
   * a criterion nobody has the evidence for yet is a gap in the reading, and
   * calling it a failure says the agent did badly at something nobody asked
   * it, while calling it a pass would ship Level 1 on nothing. The row says
   * what is missing instead. */
  gate: {
    pass: {
      label: "passed",
      reason: "Measured, and at or above the target.",
      tone: "good",
      mark: "✓",
    },
    fail: {
      label: "below target",
      reason: "Measured, and under the target. The row says by how much.",
      tone: "bad",
      mark: "✕",
    },
    not_measured: {
      label: "waiting for data",
      reason: "Nobody has produced the evidence this row is read from yet. Not a failure: the row says what is missing and who would do it.",
      tone: "neutral",
      mark: "○",
    },
  },

  /* One machine's backup, as `app/reports/tbr/backup.status_of` decides it.
   *
   * Four words rather than three, because a machine nothing has ever run on
   * is a different problem from one whose job ran and broke, and they want
   * different people. The same four the review's device table prints. */
  backup: {
    ok: {
      label: "Backed up",
      reason: "Every day in the window that could be judged was a successful backup.",
      tone: "good",
      mark: "✓",
    },
    warnings: {
      label: "Completed with errors",
      reason: "The job finished and reported errors, so what it wrote may not be everything.",
      tone: "warn",
      mark: "!",
    },
    failed: {
      label: "Backup failed",
      reason: "A job ran and broke, or nothing has succeeded for three days.",
      tone: "bad",
      mark: "✕",
    },
    not_backed_up: {
      label: "No backup",
      reason: "Nothing has backed this machine up for a week or more, which is not the same as a job that failed.",
      tone: "bad",
      mark: "✕",
    },
    not_in_scope: {
      label: "Not in backup scope",
      reason: "This customer does not back up machines of this kind, so Cove holding nothing for it is expected rather than a gap. The line beneath says which rule applied.",
      tone: "neutral",
      mark: "·",
    },
  },

  /* What a person said about a ticket they were asked to judge blind. */
  label: {
    alert: { label: "from an alert", reason: "A tool raised it; no person wrote in.", tone: "neutral", mark: "◆" },
    human: { label: "from a person", reason: "Somebody wrote in, rang, or walked up.", tone: "neutral", mark: "☺" },
    unsure: { label: "can't tell", reason: "The title does not say. A decline, not a guess — left out of the score.", tone: "warn", mark: "?" },
    same: { label: "one issue", reason: "Every ticket in the group is the same issue.", tone: "good", mark: "✓" },
    not_same: { label: "not one issue", reason: "The group holds tickets that are not the same issue.", tone: "bad", mark: "✕" },
    partly: { label: "partly", reason: "Most of the group is one issue; the named tickets do not belong. Counted as wrong.", tone: "warn", mark: "~" },
    skipped: { label: "skipped", reason: "Passed over without an answer. Not a label.", tone: "neutral", mark: "–" },
    withdrawn: { label: "withdrawn", reason: "An answer the labeller undid. Kept, and not scored.", tone: "neutral", mark: "↶" },
  },
};

const UNKNOWN_STATE = { label: "", reason: "No description for this state.", tone: "neutral", mark: "?" };

/** One state's entry, or a fallback carrying the raw name rather than nothing:
 *  a state the API grows before this file does prints as itself, never blank. */
function stateOf(family, key) {
  const group = STATES[family] || {};
  return group[key] || { ...UNKNOWN_STATE, label: String(key ?? "unknown") };
}

function stateLabel(family, key) {
  return stateOf(family, key).label;
}

function stateReason(family, key) {
  return stateOf(family, key).reason;
}

/** A status, drawn: a dot and a word, and nothing else.
 *
 *  This is the only way the product draws one. There used to be two — a `.chip`
 *  that painted its dot from CSS and a `.state` that carried one as an element —
 *  which rendered identically and meant a new page could pick the wrong one.
 *  The word is not optional: colour is never the only signal, and a function
 *  that cannot be called without the word is what enforces it. */
function stateWord(tone, text, title) {
  const attr = title ? ` title="${escapeHtml(title)}"` : "";
  const name = escapeHtml(tone || "neutral");
  return (
    `<span class="state ${name}"${attr}>` +
    `<span class="dot ${name}"></span>` +
    `<span class="word">${escapeHtml(text)}</span></span>`
  );
}

function stateChip(family, key, extra) {
  const state = stateOf(family, key);
  return stateWord(state.tone, state.label, [state.reason, extra].filter(Boolean).join(" "));
}

/** A status that is not one of the named states: a count, a badge, a note. The
 *  `mark` argument is gone — the dot took the glyph's place — and the callers
 *  that passed one pass the tone instead. */
function toneChip(tone, text, title) {
  return stateWord(tone, text, title);
}

/* --- the one overall mark per machine ------------------------------------------
 *
 * Nothing stores a per-device verdict: the resolution writes a status and a set
 * of vendor links, and the board computes per-tool states live. So the page
 * derives one, from those two things and nothing else, and the page prints the
 * rule underneath the table. The order is the order an engineer would work in:
 * a machine nothing is protecting comes before a machine nobody has scanned.
 */
/** Whether a protection or scanning agent belongs on this kind of machine at
 *  all. Mirrors `AGENT_NODE_CLASSES` in `app/coverage/core.py`, where it
 *  decides the coverage board's population: network gear, printers, phones and
 *  hypervisor hosts are counted and left out, so a switch with no Bitdefender
 *  endpoint is not a gap on the board and must not be a red mark here. */
const AGENT_CLASSES = ["WINDOWS_", "MAC", "LINUX_", "VMWARE_VM_GUEST", "HYPERV_VMM_GUEST"];

function isAgentClass(nodeClass) {
  return Boolean(nodeClass) && AGENT_CLASSES.some((prefix) => String(nodeClass).startsWith(prefix));
}

/** Is this machine in the coverage board's population?
 *
 * The board assesses the computers NinjaOne returned on the run it was taken,
 * and it takes a machine the RMM has lost out of every tool's gap count and
 * gives it a line of its own — because a machine nobody has heard from may be
 * in a cupboard, and calling it a protection gap is a finding about a machine
 * that may not exist. The Devices chips used to ignore that and counted those
 * machines as unprotected anyway, which is most of why the two pages disagreed:
 * 24 of a 27-machine gap on the live client.
 *
 * A retired machine is out for the same reason, and a machine with no NinjaOne
 * record at all was never in the board's population: the board reports those as
 * unmanaged endpoints instead. */
function assessableByTools(row) {
  if (row.status !== "active") return false;
  const ninja = row.vendors && row.vendors.ninjaone;
  return Boolean(ninja && ninja.present);
}

function deviceHealth(row) {
  const reasons = [];
  if (row.status === "retired") {
    return { key: "retired", reasons: [stateReason("device", "retired")] };
  }
  let key = "healthy";
  const raise = (level) => {
    if (level === "action" || (level === "attention" && key === "healthy")) key = level;
  };
  if (row.status === "rmm_agent_down") {
    reasons.push("The machine is alive — another tool heard from it — and the NinjaOne agent is not reporting.");
    raise("action");
  } else if (row.status === "rmm_stale") {
    reasons.push("NinjaOne has not heard from this machine, and no other tool has either.");
    raise("attention");
  }
  const vendors = row.vendors || {};
  if (!assessableByTools(row)) {
    /* The RMM has lost it, or it is not in the RMM at all. Whatever the other
       tools hold, a gap cannot be asserted against a machine nobody can confirm
       is still there — which is exactly what the coverage board does, and why
       an RMM-lost machine is its own line there rather than a gap in every
       tool's column. */
    if (!reasons.length) {
      reasons.push(
        "It is not in the RMM, so there is nothing to check the other tools against.",
      );
      raise("attention");
    }
    return { key, reasons };
  }
  if (!isAgentClass(row.node_class)) {
    /* Not a computer. It is judged on whether the RMM still hears from it and
       on nothing else, exactly as the coverage board leaves it out of every
       tool's population. */
    if (!reasons.length) {
      reasons.push(
        `It is a ${machineKind(row.node_class).toLowerCase() || "device of a kind"} rather than a computer, so no protection or scanning agent belongs on it. The coverage board leaves these out for the same reason.`,
      );
    }
    return { key, reasons };
  }
  if (vendors.bitdefender && !vendors.bitdefender.present) {
    reasons.push("Nothing is protecting it: Bitdefender holds no record of this machine.");
    raise("action");
  }
  if (vendors.connectsecure && !vendors.connectsecure.present) {
    reasons.push("It has never been scanned for vulnerabilities: ConnectSecure holds no record of it.");
    raise("attention");
  }
  /* A machine with no NinjaOne record never reaches here: `assessableByTools`
     returned above with that as its reason. */
  if (!reasons.length) reasons.push(stateReason("health", "healthy"));
  return { key, reasons };
}

/** The filter chips on the device list, and the one predicate behind each, so
 *  the chip a person presses and the sentence under it cannot drift apart. */
/* Each chip says, in the `counts` line the page prints under them, exactly what
   population it counted — because the Coverage board counts the same things
   over a different population, as of a different moment, and two pages showing
   different numbers for the same words is worse than either number. */
const DEVICE_FILTERS = [
  {
    key: "attention",
    label: "Needs attention",
    reason: "Anything that is not healthy and not retired.",
    counts:
      "Every machine whose mark is Needs attention or Action needed, on this page's own rule, "
      + "printed under the table.",
    test: (row) => {
      const health = deviceHealth(row).key;
      return health === "attention" || health === "action";
    },
  },
  {
    key: "no-protection",
    label: "No protection",
    reason:
      "A computer the RMM is reporting on, with no Bitdefender record. The same population the "
      + "coverage board calls Not installed.",
    counts:
      "Computers NinjaOne is still hearing from, with no active Bitdefender record. Network gear "
      + "and printers are out (no protection agent belongs on them), and so are machines the RMM "
      + "has lost, which get their own chip — the coverage board leaves both out of this count for "
      + "the same reasons.",
    test: (row) =>
      assessableByTools(row)
      && isAgentClass(row.node_class)
      && row.vendors.bitdefender
      && !row.vendors.bitdefender.present,
  },
  {
    key: "not-scanned",
    label: "Not scanned",
    reason:
      "A computer the RMM is reporting on, with no ConnectSecure record. The same population the "
      + "coverage board calls Not installed.",
    counts:
      "Computers NinjaOne is still hearing from, with no active ConnectSecure record. Network gear "
      + "and printers are out, and so are machines the RMM has lost.",
    test: (row) =>
      assessableByTools(row)
      && isAgentClass(row.node_class)
      && row.vendors.connectsecure
      && !row.vendors.connectsecure.present,
  },
  {
    key: "rmm-quiet",
    label: "RMM not reporting",
    reason: "NinjaOne has not heard from the machine, whether or not another tool has.",
    counts:
      "Machines NinjaOne has not heard from recently. The coverage board gives these a line of "
      + "their own too, and counts them as a gap in no tool: a machine nobody can confirm is still "
      + "there is not evidence that an agent is missing from it.",
    test: (row) => row.status === "rmm_stale" || row.status === "rmm_agent_down",
  },
  {
    key: "not-in-rmm",
    label: "Not in RMM",
    reason: "NinjaOne holds no record of the machine at all; another tool is the only one that has it.",
    counts:
      "Machines only another tool knows about. These are not on the coverage board at all — its "
      + "population is NinjaOne's computers — and it reports them as unmanaged endpoints instead.",
    test: (row) => row.vendors.ninjaone && !row.vendors.ninjaone.present,
  },
];

/* --- how a person says when and how long ------------------------------------
 *
 * How long until a deadline is this file's; how long *ago* is `timeWords` in
 * common.js, which every page shares. `humanAge` and `humanWhen` lived here and
 * said "17 hours ago" where the shared one said "17 h ago", for the same
 * instant on two pages.
 */

/** "overdue by 3 days", "due in 2 hours", "due in 25 minutes". */
function humanDue(iso) {
  if (!iso) return "";
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  const overdue = seconds < 0;
  const size = Math.abs(seconds);
  let amount;
  if (size < 3600) {
    const minutes = Math.max(1, Math.round(size / 60));
    amount = `${minutes} minute${minutes === 1 ? "" : "s"}`;
  } else if (size < 172800) {
    const hours = Math.round(size / 3600);
    amount = `${hours} hour${hours === 1 ? "" : "s"}`;
  } else {
    const days = Math.round(size / 86400);
    amount = `${days} day${days === 1 ? "" : "s"}`;
  }
  return overdue ? `overdue by ${amount}` : `due in ${amount}`;
}

function plural(count, word, plural_) {
  return `${count} ${count === 1 ? word : plural_ || word + "s"}`;
}

/** "1 needs action", "3 need action": the verb agrees with the count. */
function agrees(count, singular, plural_) {
  return `${count} ${count === 1 ? singular : plural_}`;
}

/** A vendor's own field name as words: `advancedThreatControl` reads "advanced
 *  threat control". A transform rather than a mapping, on purpose — GravityZone
 *  returns sixteen module keys, seven of them undocumented, and the module list
 *  is read from the rows and never from a fixed tuple, so a key nobody has seen
 *  before still comes out readable instead of missing. */
const ACRONYMS = new Set(["edr", "ai", "url", "dns", "vpn", "usb", "id", "ip"]);

function fromCamel(text) {
  const words = String(text)
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => (ACRONYMS.has(word) ? word.toUpperCase() : word));
  if (!words.length) return "";
  const first = words[0];
  return [ACRONYMS.has(first.toLowerCase()) ? first : first.charAt(0).toUpperCase() + first.slice(1), ...words.slice(1)].join(" ");
}

/** What kind of machine this is, in words. A class we have no word for is
 *  tidied rather than printed as an enum: `FOO_BAR_GUEST` reads "Foo bar guest". */
function machineKind(nodeClass) {
  if (!nodeClass) return "";
  const known = STATES.machine[nodeClass];
  if (known) return known.label;
  const words = String(nodeClass).toLowerCase().replace(/_/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
}

/* --- vendor consoles -----------------------------------------------------------
 *
 * A link straight to the machine in the vendor's own console, built from the
 * vendor id we already hold and the console's address, which the page is given
 * by the server from the client's own connector settings (never guessed: a
 * NinjaOne tenant may be on any of several instances, and a link to the wrong
 * one is worse than no link). `window.DESK_CONSOLES` is empty when we hold no
 * address we recognise, and then no link is drawn and the id is shown to copy.
 */
const CONSOLES = window.DESK_CONSOLES || {};

const CONSOLE_ROUTES = {
  ninjaone: (base, id) => `${base}/#/deviceDashboard/${encodeURIComponent(id)}/overview`,
  autotask: (base, id) => `${base}/Mvc/ServiceDesk/TicketDetail.mvc?ticketId=${encodeURIComponent(id)}`,
};

/** The console URL for one record, or null when we cannot build one. */
function consoleUrl(connector, externalId) {
  const base = CONSOLES[connector];
  const route = CONSOLE_ROUTES[connector];
  if (!base || !route || !externalId) return null;
  return route(base, externalId);
}

const VENDOR_NAMES = {
  ninjaone: "NinjaOne",
  bitdefender: "Bitdefender",
  connectsecure: "ConnectSecure",
  cove: "Cove",
  autotask: "Autotask",
};

/** Which vendor answers each entry on the coverage board.
 *
 *  The board's own keys name what is being measured — endpoint protection,
 *  vulnerability scanning, backup — because the measurement is the thing and
 *  the vendor could change. A column header is the other way round: somebody
 *  reading a table wants to know which console to open, so it says the vendor
 *  and the measurement sits under it as the role. Both come from here, so the
 *  two can never drift apart. */
const COVERAGE_TOOL_VENDOR = {
  bitdefender: "Bitdefender",
  connectsecure: "ConnectSecure",
  backup: "Cove",
};

/** What each vendor is on this page, in the words the rest of the page uses. */
const VENDOR_ROLE = {
  ninjaone: "monitoring and patching",
  bitdefender: "protection",
  connectsecure: "vulnerability scanning",
  cove: "backup",
  autotask: "tickets",
};

/* Settings: the caller's own account, and — for an administrator — other people's.
 *
 * Every refusal shown here is the server's own message. The rules about who may
 * invite whom live in `app/api/auth.py` and are enforced there; this page offers
 * the roles that will be accepted so the common case does not need a refusal to
 * find out, which is a convenience and not the control.
 */

const message = document.getElementById("message");

function say(text, kind) {
  message.textContent = text;
  message.className = `notice${kind ? " " + kind : ""}`;
  message.hidden = false;
  message.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

/** A failure, as a sentence somebody can act on, with the server's own words
 *  behind "Details" for the people who can do something with them. */
function sayProblem(what, error, next) {
  message.innerHTML = problem(what, error, next);
  message.className = "notice bad";
  message.hidden = false;
  message.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function clearMessage() {
  message.hidden = true;
}

/* --- password ------------------------------------------------------------- */

document.getElementById("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearMessage();
  const current = document.getElementById("current-password");
  const next = document.getElementById("new-password");
  try {
    await api("POST", "/auth/password", {
      current_password: current.value,
      new_password: next.value,
    });
    // Every session ended, this one included, so there is nothing to stay for.
    say("Password changed. Signing you in again…", "good");
    setTimeout(() => window.location.replace(DESK.loginPath || "/login"), 1200);
  } catch (error) {
    sayProblem("Your password was not changed.", error, "Check the current password and try again.");
    current.value = "";
    next.value = "";
    current.focus();
  }
});

/* --- the second factor ------------------------------------------------------ */

const totpState = document.getElementById("totp-state");
const panels = {
  off: document.getElementById("totp-off"),
  enrolling: document.getElementById("totp-enrolling"),
  on: document.getElementById("totp-on"),
};

function showTotp(which, label) {
  for (const [name, node] of Object.entries(panels)) node.hidden = name !== which;
  totpState.textContent = label;
}

async function refreshTotp() {
  try {
    const me = await api("GET", "/auth/me");
    showTotp(me.totp_enabled ? "on" : "off", me.totp_enabled ? "On" : "Off");
  } catch (error) {
    showTotp("off", "");
  }
}

document.getElementById("totp-start").addEventListener("click", async () => {
  clearMessage();
  try {
    const started = await api("POST", "/auth/totp/start");
    document.getElementById("totp-secret").textContent = started.secret;
    showTotp("enrolling", "Setting up");
    document.getElementById("totp-code").focus();
  } catch (error) {
    sayProblem("The authenticator could not be set up.", error);
  }
});

document.getElementById("totp-confirm").addEventListener("click", async () => {
  clearMessage();
  const code = document.getElementById("totp-code");
  try {
    await api("POST", "/auth/totp/confirm", { code: code.value.trim() });
    code.value = "";
    say("Two-factor authentication is on. You will be asked for a code next time.", "good");
    await refreshTotp();
  } catch (error) {
    sayProblem(
      "That code was not accepted.",
      error,
      "Codes change every thirty seconds; read the current one and try again.",
    );
    code.select();
  }
});

document.getElementById("totp-disable-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  clearMessage();
  const password = document.getElementById("totp-password");
  try {
    await api("POST", "/auth/totp/disable", { password: password.value });
    say("Two-factor authentication is off. Signing you in again…", "good");
    setTimeout(() => window.location.replace(DESK.loginPath || "/login"), 1200);
  } catch (error) {
    sayProblem(
      "Two-factor authentication was not turned off.",
      error,
      "Check your password and try again.",
    );
    password.value = "";
    password.focus();
  }
});

/* --- people ------------------------------------------------------------------ */

const ROLE_LABELS = {
  inputiv_admin: "Inputiv administrator",
  inputiv_engineer: "Inputiv engineer",
  franchisee_admin: "Administrator",
  franchisee_technician: "Technician",
  franchisee_viewer: "Viewer",
};

/** The roles this administrator may grant. An Inputiv admin may grant any; a
 *  franchisee admin may grant only franchisee roles, and the server refuses
 *  anything else with a 403 rather than trusting this list. */
function grantableRoles() {
  return DESK.role === "inputiv_admin"
    ? Object.keys(ROLE_LABELS)
    : ["franchisee_admin", "franchisee_technician", "franchisee_viewer"];
}

/** An account's state, as a word rather than an enum value. The database
 *  spelling is for the audit trail; a person reading a table wants a word
 *  spelled the way every other chip on the product spells one. */
const STATUS_CHIPS = {
  active: ["good", "Active"],
  invited: ["warn", "Invited"],
  suspended: ["bad", "Suspended"],
};

function statusChip(status) {
  const [kind, label] = STATUS_CHIPS[status] || ["neutral", status];
  return stateWord(kind, label);
}

function personRow(person) {
  const isSelf = person.email === DESK.email;
  const canSuspend = DESK.role === "inputiv_admin" && !isSelf;
  /* Suspending is the one destructive thing on this page, and it wears the
     product's one destructive style: the same faint ground every other control
     has, carrying the bad token, filling red only under the pointer. It asks
     before it does anything (see the handler below). */
  const action = canSuspend
    ? `<button type="button" class="btn small ${person.status === "suspended" ? "secondary" : "destructive"}"
         data-user="${escapeHtml(person.id)}"
         data-to="${person.status === "suspended" ? "active" : "suspended"}"
         data-email="${escapeHtml(person.email)}">
         ${person.status === "suspended" ? "Reinstate" : "Suspend"}</button>`
    : "";
  return `<tr>
    <td><strong>${escapeHtml(person.email)}</strong>${isSelf ? ` ${stateWord("neutral", "you")}` : ""}
      ${person.totp_enabled ? ` ${stateWord("accent", "2FA")}` : ""}</td>
    <td>${escapeHtml(ROLE_LABELS[person.role] || person.role)}</td>
    <td class="mono small muted">${escapeHtml(person.organisation)}</td>
    <td class="nowrap">${statusChip(person.status)}</td>
    <td class="nowrap small muted">${person.last_login_at ? escapeHtml(timeWords(person.last_login_at)) : "never"}</td>
    <td class="nowrap">${action}</td>
  </tr>`;
}

function inviteRow(invite) {
  const state = invite.accepted_at
    ? stateWord("good", "accepted")
    : invite.revoked_at
      ? stateWord("neutral", "revoked")
      : new Date(invite.expires_at) < new Date()
        ? stateWord("bad", "expired")
        : stateWord("warn", "open");
  return `<tr>
    <td>${escapeHtml(invite.email)}</td>
    <td>${escapeHtml(ROLE_LABELS[invite.role] || invite.role)}</td>
    <td class="mono small muted">${escapeHtml(invite.organisation)}</td>
    <td class="nowrap small muted">${when(invite.expires_at, { time: true })}</td>
    <td class="nowrap">${state}</td>
  </tr>`;
}

async function loadPeople() {
  const people = document.getElementById("people");
  const invites = document.getElementById("invites");
  try {
    const [users, pending] = await Promise.all([
      api("GET", "/auth/users"),
      api("GET", "/auth/invites"),
    ]);
    people.innerHTML = users.length
      ? users.map(personRow).join("")
      : nothingRow(6, "Nobody has an account here yet.", {
          text: "Invite somebody with the form above.",
        });
    invites.innerHTML = pending.length
      ? pending.map(inviteRow).join("")
      : nothingRow(5, "No invitation is outstanding.");
    document.getElementById("people-scope").textContent =
      DESK.role === "inputiv_admin" ? "Every organisation." : "Your organisation.";
  } catch (error) {
    people.innerHTML = `<tr><td colspan="6">${problem("Couldn't load the people.", error)}</td></tr>`;
    invites.innerHTML = "";
  }
}

function wirePeople() {
  const roleSelect = document.getElementById("invite-role");
  for (const role of grantableRoles()) {
    roleSelect.appendChild(new Option(ROLE_LABELS[role], role));
  }

  document.getElementById("invite-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearMessage();
    const email = document.getElementById("invite-email");
    const token = document.getElementById("invite-token");
    try {
      const invite = await api("POST", "/auth/invites", {
        email: email.value.trim(),
        role: roleSelect.value,
      });
      email.value = "";
      token.innerHTML =
        `<strong>Invite for ${escapeHtml(invite.email)}.</strong> Send them this token by a ` +
        "channel that is not this screen. It is shown once and cannot be looked up again." +
        `<div class="mono" style="margin-top: var(--space-2); overflow-wrap: anywhere">${escapeHtml(invite.token)}</div>`;
      token.hidden = false;
      await loadPeople();
    } catch (error) {
      sayProblem(
        "That invitation was not sent.",
        error,
        "Check the address, and whether they already have an account or an open invitation.",
      );
    }
  });

  document.getElementById("people").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-user]");
    if (!button) return;
    clearMessage();
    /* Suspending somebody signs them out of everywhere and takes their access
       away until a person puts it back. It is one click in a table of rows
       that otherwise do nothing, so it asks first — and names who, because the
       row under the pointer is not always the row somebody meant. */
    if (button.dataset.to === "suspended") {
      const who = button.dataset.email || "this account";
      const sure = await ask({
        title: `Suspend ${who}?`,
        body:
          "They are signed out everywhere immediately and cannot sign in again until " +
          "somebody reinstates them. Nothing they have done is deleted.",
        confirm: "Suspend",
        tone: "destructive",
      });
      if (sure === null) return;
    }
    button.disabled = true;
    try {
      await api("POST", `/auth/users/${button.dataset.user}/status`, {
        status: button.dataset.to,
      });
      await loadPeople();
    } catch (error) {
      button.disabled = false;
      sayProblem("That account was not changed.", error);
    }
  });

  loadPeople();
}

refreshTotp();
if (DESK.mayAdminister) wirePeople();

/* Signing in.
 *
 * This page does not use common.js: it has no session yet, so there is no CSRF
 * token to send and nothing to redirect to on a 401. `POST /auth/login` is a
 * public route for exactly that reason — the token belongs to the session the
 * request is asking to create.
 *
 * Every message shown here is the server's own. A refused sign-in says the
 * same thing whatever the cause, on purpose, so that this form cannot be used
 * to find out which addresses have accounts; paraphrasing it in the page would
 * be a second description of that refusal, free to drift from the first.
 */

const form = document.getElementById("signin");
const submit = document.getElementById("submit");
const errorBox = document.getElementById("error");
const totpField = document.getElementById("totp-field");
const totpInput = document.getElementById("totp");

/** Where to go after signing in. Only a path on this site: an absolute or
 *  protocol-relative `next` would make this form an open redirect. */
function destination() {
  const next = new URLSearchParams(window.location.search).get("next");
  if (next && next.startsWith("/") && !next.startsWith("//")) return next;
  return "/";
}

function showError(message, retryAfter) {
  errorBox.textContent = retryAfter ? `${message} (about ${retryAfter}s)` : message;
  errorBox.hidden = false;
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

async function detailOf(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
  } catch (error) {
    /* Not JSON: a proxy error page, or nothing. */
  }
  return `Sign-in failed (${response.status}).`;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  submit.disabled = true;

  const payload = {
    email: document.getElementById("email").value.trim(),
    password: document.getElementById("password").value,
  };
  const code = totpInput.value.trim();
  if (!totpField.hidden && code) payload.totp_code = code;

  let response;
  try {
    response = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    submit.disabled = false;
    showError("The server could not be reached.");
    return;
  }

  if (response.ok) {
    window.location.replace(destination());
    return;
  }

  submit.disabled = false;
  const message = await detailOf(response);

  if (response.headers.get("X-Second-Factor") === "required") {
    // The password was right. Reveal the code field and leave the other two
    // filled, so submitting again sends all three.
    totpField.hidden = false;
    totpInput.focus();
    showError(message);
    return;
  }

  if (response.status === 429) {
    showError(message, response.headers.get("Retry-After"));
    return;
  }

  showError(message);
  const password = document.getElementById("password");
  password.focus();
  password.select();
});

/* A browser that still holds a session has no reason to be here. */
(async function alreadySignedIn() {
  try {
    const response = await fetch("/auth/me", { headers: { Accept: "application/json" } });
    if (response.ok) window.location.replace(destination());
  } catch (error) {
    /* Offline, or the session is gone. Either way, the form is the next step. */
  }
})();

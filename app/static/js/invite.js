/* Accepting an invitation: choose a password, confirm it, and be signed in.
 *
 * Like the sign-in page this does not use common.js — there is no session yet,
 * so no CSRF token to send and nowhere to redirect on a 401.
 *
 * A word about the strength meter, because a meter that lies is worse than no
 * meter. **The only rule is length**, and the server enforces exactly that
 * (`check_password_policy`); complexity rules push people towards Passw0rd!
 * and a sticky note, which is why this codebase does not have them. What the
 * bar shows is therefore advice, and it is honest about what it measures:
 * length first, and a named warning for the two choices that are actually bad
 * — the address itself, and a password from everybody's first guess.
 */

const INVITE = window.DESK_INVITE || {};

const form = document.getElementById("accept");
const submit = document.getElementById("submit");
const errorBox = document.getElementById("error");
const password = document.getElementById("password");
const confirm = document.getElementById("confirm");
const confirmHelp = document.getElementById("confirm-help");
const strength = document.getElementById("strength");
const fill = document.getElementById("strength-fill");
const label = document.getElementById("strength-label");

/** What the server will accept. Kept in step with MIN_PASSWORD_LENGTH. */
const MIN_LENGTH = 12;

/** The handful of passwords that are tried first, always. Not a dictionary:
 *  a real one belongs on the server if we ever want to refuse them, and
 *  shipping 100kB of words to warn somebody would be the wrong trade. */
const OBVIOUS = [
  "password", "passw0rd", "letmein", "welcome", "qwerty", "admin",
  "changeme", "iloveyou", "monkey", "dragon", "football", "inputiv", "desk",
];

/** Advice, in four steps. Returns a share of the bar and a word for it. */
function assess(value) {
  if (!value) return null;

  const local = String(INVITE.email || "").split("@")[0].toLowerCase();
  const lowered = value.toLowerCase();

  if (local.length > 2 && lowered.includes(local)) {
    return { share: 0.15, word: "Too easy to guess", why: "it contains your email address", bad: true };
  }
  if (OBVIOUS.some((guess) => lowered.includes(guess))) {
    return { share: 0.15, word: "Too easy to guess", why: "it contains a very common password", bad: true };
  }
  if (/^(.)\1+$/.test(value)) {
    return { share: 0.15, word: "Too easy to guess", why: "it is one character repeated", bad: true };
  }
  if (value.length < MIN_LENGTH) {
    const short = MIN_LENGTH - value.length;
    return {
      share: Math.max(0.1, value.length / MIN_LENGTH) * 0.4,
      word: "Too short",
      why: `${short} more character${short === 1 ? "" : "s"} to go`,
      bad: true,
    };
  }

  // Past the rule. From here it is genuinely just length: every extra
  // character is worth more than any symbol substitution.
  if (value.length < 16) return { share: 0.55, word: "Fine", why: "longer would be better" };
  if (value.length < 24) return { share: 0.8, word: "Good", why: "" };
  return { share: 1, word: "Strong", why: "" };
}

function showStrength() {
  const verdict = assess(password.value);
  if (!verdict) {
    strength.hidden = true;
    return;
  }
  strength.hidden = false;
  fill.style.width = `${Math.round(verdict.share * 100)}%`;
  fill.className = `fill ${verdict.bad ? "weak" : verdict.share >= 0.8 ? "strong" : "fair"}`;
  label.textContent = verdict.why ? `${verdict.word} — ${verdict.why}` : verdict.word;
  label.className = `label${verdict.bad ? " weak" : ""}`;
}

function showConfirm() {
  if (!confirm.value) {
    confirmHelp.textContent = "";
    return;
  }
  const same = confirm.value === password.value;
  confirmHelp.textContent = same ? "These match." : "These do not match yet.";
  confirmHelp.className = same ? "help" : "help mismatch";
}

password.addEventListener("input", () => {
  showStrength();
  showConfirm();
});
confirm.addEventListener("input", showConfirm);

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  errorBox.scrollIntoView({ block: "nearest" });
}

async function detailOf(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    // A 422 from the request model: name the first field that was wrong.
    if (Array.isArray(body.detail) && body.detail.length) {
      return body.detail[0].msg || "That was not accepted.";
    }
  } catch (error) {
    /* Not JSON: a proxy error page, or nothing. */
  }
  return `That could not be saved (${response.status}).`;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;

  // The two client-side checks, said plainly rather than by refusing to submit:
  // a disabled button with no reason is the worst version of this form.
  if (password.value !== confirm.value) {
    showError("The two passwords do not match.");
    confirm.focus();
    return;
  }
  if (password.value.length < MIN_LENGTH) {
    showError(`A password must be at least ${MIN_LENGTH} characters.`);
    password.focus();
    return;
  }

  submit.disabled = true;
  const name = document.getElementById("display-name").value.trim();

  let response;
  try {
    response = await fetch("/auth/invites/accept", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: INVITE.token,
        password: password.value,
        display_name: name || null,
      }),
    });
  } catch (error) {
    submit.disabled = false;
    showError("The server could not be reached.");
    return;
  }

  if (response.ok) {
    // The cookie came with the response; Ask is the front page.
    window.location.replace("/");
    return;
  }

  submit.disabled = false;
  showError(await detailOf(response));
  password.value = "";
  confirm.value = "";
  showStrength();
  showConfirm();
  password.focus();
});

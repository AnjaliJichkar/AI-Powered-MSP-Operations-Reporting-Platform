# Deploying the Inputiv Engine to Render

A runbook, in order. Each step says what to do, the exact command, and what
"done" looks like. Read it once end to end; then work through the checklist
at the very end, which lists only the things that need a person with the
Render account.

Nothing in this document is a secret value, and nothing in the repository
is either. `render.yaml` marks every secret `sync: false`, which makes Render
ask for it when the Blueprint is created; the values live in Render's
environment and nowhere else.

What is being deployed: one FastAPI web service run by uvicorn, one managed
Postgres in Render's Ohio region, and (after the summit) one cron job for
the daily collector. The Blueprint is `render.yaml` at the repository root.

---

## 0. Before you start

Have to hand:

- a Render account with a workspace that can create paid services;
- the GitHub repository connected to Render (Render reads `render.yaml` from
  the branch you point it at, `main`);
- your laptop with this repository, Docker running (the local Postgres in
  `docker-compose.yml` is the source of the migration), and the `.venv`;
- an Anthropic API key for the deployment (a separate key from the one in
  your laptop's `.env`, so it can be revoked on its own);
- the domain the service will answer on (step 8 puts Cloudflare Access in
  front of it) or the willingness to use the `onrender.com` hostname first.

The code side is already done and tested (`tests/test_settings.py`,
`tests/api/test_authorization.py`): the settings refuse to start outside
`ENVIRONMENT=local` unless the production rules in step 3 hold.

---

## 1. The Blueprint: `render.yaml`

`render.yaml` declares:

| Resource | Name | What |
|---|---|---|
| web service | `inputiv-engine` | Render's native Python runtime (Python 3.12.7), `pip install -e .`, pre-deploy `alembic upgrade head`, start `uvicorn app.main:app`, health check `/health`, a 1 GB disk at `/var/data` for rendered decks, region `ohio`, plan `standard` |
| database | `inputiv-postgres` | Postgres 16, plan `basic-1gb`, region `ohio`, no public access (`ipAllowList: []`) |
| environment group | `inputiv-engine-config` | every non-secret setting production needs (step 3) |
| cron (parked) | `inputiv-daily-collector` | the daily collector: every active client, 09:00 UTC. Commented out by decision, 20 October 2026 (step 5) |

**Why Render's native Python runtime and not Docker.** There is no
Dockerfile to maintain; every dependency ships a wheel; Python is pinned by
`.python-version` and `PYTHON_VERSION`; nothing needs a system package. One
service does not justify a second build system to keep patched. If a system
dependency ever appears, switch `runtime: docker` and add a Dockerfile then.

**Why one uvicorn worker.** The sign-in rate limiter is in memory and per
process (CLAUDE.md, "Two throttles"). Two workers would each see half the
attempts. Scale by plan size, not by worker count, until the limiter moves to
the proxy (step 8) or the database.

**Why the pre-deploy command.** `alembic upgrade head` runs after the build
and before the new instance takes traffic, with the service's environment. A
failing migration fails the deploy and the previous instance keeps serving.
Alembic reads `DATABASE_URL` through `app.settings` (`alembic/env.py`), so
the same settings checks apply to it.

**Why `--proxy-headers --forwarded-allow-ips='*'`.** Render terminates TLS
and proxies to the service; the client address arrives in `X-Forwarded-For`.
Without these flags every sign-in attempt would appear to come from Render's
proxy and the per-address rate limit would throttle everyone together.

**Why a disk.** Rendered business-review decks are written under
`REPORTS_DIR`. Render's filesystem is otherwise ephemeral: every deck would
vanish on the next deploy. The cost is that a service with a disk restarts on
deploy with a few seconds' downtime instead of a zero-downtime swap. Decks
can be re-rendered from the stored review, so losing the disk loses nothing
that cannot be remade.

Nothing to run in this step. Create the Blueprint in step 4 once the
environment variables in step 2 are ready to paste.

---

## 2. Environment variables

Every value below is set in Render, never in the image and never in the
repository. The names are exactly what `app/settings.py` reads (pydantic
settings, upper-cased field names). Two names differ from the shorthand you
may have in your head:

- there is no `CREDENTIAL_KEY`; the credential cipher reads
  `CREDENTIAL_ENCRYPTION_KEYS` (a JSON map of key id to Fernet key) and
  `CREDENTIAL_ENCRYPTION_ACTIVE_KEY_ID`, so a key can be rotated without
  losing rows encrypted under the old one;
- `ENVIRONMENT` accepts `local`, `dev`, `staging` and `prod`; the production
  value is `prod` (not `production`), and `settings.is_production` tests for
  it.

### 2a. Secrets (Render asks for these when the Blueprint is created)

| Variable | What it is for | If it is missing or wrong |
|---|---|---|
| `CREDENTIAL_ENCRYPTION_KEYS` | Encrypts every vendor credential (`tool_credentials`) and every TOTP secret at rest. JSON: `{"v1": "<fernet key>"}`. **Generate one new key for production; do not reuse the laptop's.** | The service starts, but any connector read raises at the cipher ("No credential encryption keys configured") and every page that touches a vendor errors. With the *wrong* key, every stored credential fails to decrypt (`DecryptionError`) and nothing can be read from any vendor. See step 7 on what losing it means. |
| `ANTHROPIC_API_KEY` | Every model call: Ask, streaming, review narrative, ticket plans, the selection check. | The service starts. `/chat`, `/chat/stream`, plan generation and review generation fail with `ModelNotConfigured` ("ANTHROPIC_API_KEY is not set"); everything that reads vendors without a model still works. |
| `PUBLIC_BASE_URL` | The address people type, used to build the invite links the CLI and the Settings page print. Must be `https://` outside `local`. | Settings refuse to start with the reason. With the wrong host, invite links point at the wrong place; nothing else uses it. |
| `DATABASE_URL` | Set by Render from the database (`fromDatabase: connectionString`). Render's URL begins `postgres://`; settings rewrite it to `postgresql+asyncpg://`. | Settings refuse to start (`database_url` is required). |
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | The Slack interface. Optional. | Nothing: the Slack handlers are not started without them. Leave empty at Blueprint creation. |

Generate the production credential key on your laptop and paste it into
Render's prompt (it never goes in a file):

```bash
cd /e/Repositories/desk_platform
export PATH="$PWD/.venv/Scripts:$PATH"
python -c "from cryptography.fernet import Fernet; print('{\"v1\": \"' + Fernet.generate_key().decode() + '\"}')"
```

Paste the whole printed line, braces included, as the value of
`CREDENTIAL_ENCRYPTION_KEYS`. Then **store the same line in the company
password manager under "Inputiv Engine, Render, credential key v1"**. Step 7
says why that copy matters more than the database backups.

### 2b. Non-secrets (in the `inputiv-engine-config` group, already in `render.yaml`)

| Variable | Value | Why |
|---|---|---|
| `ENVIRONMENT` | `prod` | Turns on every production check in step 3. |
| `PYTHON_VERSION` | `3.12.7` | The project requires `>=3.12,<3.13`; Render's default is newer. |
| `PYTHONUNBUFFERED` | `1` | Log lines reach Render's log stream as they are written. |
| `DEBUG` | `false` | Refused `true` outside `local`. |
| `LOG_LEVEL` | `INFO` | `DEBUG` prints request bodies; not in production. |
| `DATABASE_ECHO` | `false` | Refused `true` outside `local`: SQL echo would log every credential row read. |
| `SESSION_COOKIE_SECURE` | `true` | Refused `false` outside `local`. |
| `TOTP_REQUIRED_FOR_INPUTIV` | `true` | Refused `false` outside `local`: staff hold a second factor (step 3). |
| `CREDENTIAL_ENCRYPTION_ACTIVE_KEY_ID` | `v1` | Which key in the map encrypts new rows. Must be a key in the map. |
| `REPORTS_DIR` | `/var/data/reports` | On the disk, so decks survive a deploy. |
| `BITDEFENDER_TIMEOUT_SECONDS` | `120` | GravityZone's own read timeout; its inventory pages can take a minute or more. The shared `CONNECTOR_TIMEOUT_SECONDS` (30) stays for the other vendors. |
| `BITDEFENDER_LAST_SEEN_REFRESH_HOURS` | `12` | How long a GravityZone `lastSeen` kept on a device link is trusted before it is read again. |
| `BITDEFENDER_DETAIL_CONCURRENCY` | `4` | Detail calls in flight at once during that read. |
| `DEVICE_IDENTITY_ENABLED` | `false` | As on the laptop: the identity layer is written daily and read only when this is true. Do not change it without a live agreement run (CLAUDE.md, "Device identity"). |
| `AUTOPILOT_SHADOW_LIVE` | `false` | The switch the whole live half of Autopilot hangs off. See **5a**, which covers it and the three other Autopilot settings worth setting. |
| `AUTOPILOT_MODEL` | `claude-sonnet-5` | See 5a.1: unset, this falls back to `ANTHROPIC_MODEL`, which is deliberately unset, so it would run on the code default — the most expensive model. |
| `AUTOPILOT_MAX_TOKENS_PER_RUN` | `8000000` | See 5a.1: the 5,000,000 default is under what one full replay of this tenant costs. |
| `AUTOPILOT_BATCH_REPLAY` | `false` | See 5a.1: true makes a replay's answers arrive within hours rather than minutes. |
| `AUTOMATION_TRIGGERS_LIVE` | `false` | The switch for the automation triggers' poll (`scripts/automation.py poll`). Separate from `AUTOPILOT_SHADOW_LIVE`. See **5b**. |

Everything else in `app/settings.py` (connector timeouts, page sizes, model
name and effort, caps, allowlists) keeps its code default in production,
which is the value the laptop runs with. Change one by adding it to the
group in `render.yaml` and syncing, so the change is a commit.

### 2c. What is deliberately not set

- `WEB_SEARCH_ALLOWED_DOMAINS`: unset means the chat agent's web search is
  unrestricted, as on the laptop. The review's and the ticket workspace's
  searches have their own non-empty allowlists in code.
- `ANTHROPIC_MODEL`: the code default. Pin it here only when a model change
  should be a deliberate act.

---

## 3. Production settings enforced at start-up

Outside `ENVIRONMENT=local`, `app/settings.py` refuses to construct at all
unless every one of these holds, so a misconfigured deploy fails in the
pre-deploy step with the reason in the log, before the old instance is
replaced:

| Rule | Setting | What the refusal says |
|---|---|---|
| Links are https | `PUBLIC_BASE_URL` starts with `https://` | `PUBLIC_BASE_URL must be https outside ENVIRONMENT=local` |
| Cookies are Secure | `SESSION_COOKIE_SECURE=true` | `SESSION_COOKIE_SECURE may only be false when ENVIRONMENT is 'local'` |
| No laptop options | `DEBUG=false`, `DATABASE_ECHO=false` | `DEBUG must be false`, `DATABASE_ECHO must be false` |
| No local addresses | `PUBLIC_BASE_URL` and `DATABASE_URL` hosts are not `localhost`, `127.0.0.1`, `0.0.0.0`, `::1`, `host.docker.internal`, `*.local`, `*.localhost` | `PUBLIC_BASE_URL points at the local machine (...)` |
| Staff hold a second factor | `TOTP_REQUIRED_FOR_INPUTIV=true` | `TOTP_REQUIRED_FOR_INPUTIV must be true: staff hold a second factor` |
| Identity flag as locally | `DEVICE_IDENTITY_ENABLED=false` | not a refusal: it is the code default, set explicitly in the group so a change is visible |

**What `TOTP_REQUIRED_FOR_INPUTIV` does.** An Inputiv account
(`inputiv_admin`, `inputiv_engineer`) without a confirmed authenticator code
can sign in, sees itself (`GET /auth/me` reports `second_factor_required:
true`), can sign out, can open the page shells, and can enrol on Settings.
Every other route answers `403` with `X-Second-Factor: enrol`, including
switching to a client. Every page shows a banner saying so until enrolment is
confirmed. Franchisee accounts are not held to it. The CLI scripts are not
affected: they are not sessions.

You can prove the checks from your laptop before creating anything:

```bash
cd /e/Repositories/desk_platform
export PATH="$PWD/.venv/Scripts:$PATH"
# Refused: http link, no second factor.
ENVIRONMENT=prod PUBLIC_BASE_URL=http://localhost:8000 DATABASE_URL=postgres://u:p@dpg-x-a/db \
  python -c "from app.settings import Settings; Settings.model_config['env_file']=None; Settings()"
# Accepted: what render.yaml sets.
ENVIRONMENT=prod PUBLIC_BASE_URL=https://desk.example.com DATABASE_URL=postgres://u:p@dpg-x-a/db \
  TOTP_REQUIRED_FOR_INPUTIV=true \
  python -c "from app.settings import Settings; Settings.model_config['env_file']=None; s=Settings(); print(s.environment, s.session_cookie_secure)"
```

The first prints the refusal; the second prints `prod True`.

---

## 4. Create the Blueprint and deploy

1. Commit `render.yaml` on `main` and push.
2. In the Render dashboard: **New → Blueprint**, pick the repository and
   `main`. Render reads `render.yaml` and lists what it will create: the web
   service, the database, the environment group.
3. Render prompts for every `sync: false` variable. Paste the values from
   step 2a. For `PUBLIC_BASE_URL` use the final address if the domain is
   ready (`https://desk.inputiv.com`), otherwise the service's Render address
   (`https://inputiv-engine.onrender.com`) and change it in step 8.
   Leave the two Slack values empty.
4. **Apply.** Render creates the database first, then builds the service,
   runs `alembic upgrade head` (the pre-deploy step; watch it in the deploy
   log), then starts uvicorn and polls `/health` until it answers `200`.
   Render allows fifteen minutes for that; the first build takes two to
   four.
5. Done looks like:

```bash
curl -sS https://inputiv-engine.onrender.com/health
# {"status":"ok","version":"0.1.0","environment":"prod"}
curl -sS https://inputiv-engine.onrender.com/health/ready
# {"status":"ready","database":"ok","detail":null}
```

If the deploy fails in the pre-deploy step, the log holds the settings
refusal from step 3 or the migration error; fix the variable in the
dashboard (Environment tab) and **Manual Deploy → Deploy latest commit**.

### 4a. The health check

`healthCheckPath: /health` is liveness: the process is up, nothing external
touched. Render polls it every few seconds; any `2xx` or `3xx` is healthy;
after fifteen seconds of failures it stops routing to the instance and after
sixty it restarts it; a new deploy takes traffic only once it passes. It is
`/health` and not `/health/ready` on purpose: readiness asks the database,
and a database blip must not make Render restart a healthy process.
`/health/ready` is for people and uptime monitors.

### 4b. SSE streaming on Render: check and record

`POST /chat/stream` is server-sent events. The application does its part:
`Cache-Control: no-cache`, `X-Accel-Buffering: no`, a `: keep-alive` comment
every fifteen seconds, and the answer of record in the final `done` event.
Whether Render's proxy passes events as they are written could not be
confirmed from a laptop: Render's documentation states support for
WebSockets and HTTP/2 and sets no fixed timeout on long-lived connections,
and says nothing either way about buffering streamed HTTP responses. So this
is the check, to run once after the first deploy and record:

```bash
# 1. Sign in and keep the cookie and CSRF token.
curl -sS -c cookies.txt -H "Content-Type: application/json" \
  -d '{"email":"you@inputiv.com","password":"…","totp_code":"123456"}' \
  https://inputiv-engine.onrender.com/auth/login | python -c "import sys,json; print(json.load(sys.stdin)['csrf_token'])"
# 2. Switch to the pilot client (staff only).
curl -sS -b cookies.txt -H "X-CSRF-Token: <token>" -H "Content-Type: application/json" \
  -d '{"client":"teamlogic-it-austin"}' https://inputiv-engine.onrender.com/auth/switch-client > /dev/null
# 3. Stream, with a timestamp on every line as it arrives.
curl -N -sS -b cookies.txt -H "X-CSRF-Token: <token>" -H "Content-Type: application/json" \
  -d '{"message":"what is alerting at shelton keller?"}' \
  https://inputiv-engine.onrender.com/chat/stream | while IFS= read -r line; do printf '%s %s\n' "$(date +%T)" "$line"; done
rm cookies.txt
```

Read the timestamps. **Not buffered**: `event: started` within a second,
`tool_start` and `tool_end` seconds apart, `text` events arriving over ten
seconds or more, `done` last. **Buffered**: nothing for the whole run, then
everything in the same second. In the buffered case the page still works
(the `done` event carries the complete answer), and the fix is one of: serve
the stream through Cloudflare (step 8; Cloudflare passes SSE when the origin
sends `Content-Type: text/event-stream` and no `Content-Length`), or turn
the page's streaming off and let it use `POST /chat`.

Record the finding as a line in `docs/validation-log.md` under a heading
"SSE on Render", with the timestamps of `started` and `done`.

### 4c. First accounts

The Inputiv organisation and the first administrator are made by the CLI,
which needs database credentials and so runs from a **Render shell** on the
service (dashboard → the service → Shell), never from a laptop against the
production database:

```bash
export INPUTIV_ADMIN_PASSWORD='…'        # never a command-line argument
python scripts/manage_auth.py bootstrap --email you@inputiv.com
unset INPUTIV_ADMIN_PASSWORD
```

Then sign in at `PUBLIC_BASE_URL/login`, go to Settings, enrol an
authenticator app. Until you do, every page shows the second-factor banner
and every client-scoped call is refused (step 3). The migration in step 6
brings the existing organisation, users and their enrolled second factors
across, so if you migrate first, bootstrap is not needed.

---

## 5. The daily collector (cron) — parked

> **Parked by decision, 20 October 2026.** The cron block in `render.yaml` is
> complete, has been run by hand, and is **commented out**: nothing is
> scheduled on Render. Enabling it is uncommenting that block, `git commit`
> and a Blueprint sync — the steps below are what happens when somebody does.
>
> While it is parked, **nothing recomputes a client's day on its own**. A
> coverage board is written when somebody presses Refresh or when the
> collector is run by hand, and at no other time; the board prints how old it
> is for that reason. The command works today:
>
> ```bash
> python scripts/collect_events.py --all-clients --days 1 --user <you>
> ```

**This is the only thing that recomputes a client's day.** Without it a
coverage board is written when somebody presses Refresh and at no other time:
one sat nineteen days old on a demo tenant reporting 0% endpoint protection at
every site while the Devices page, reading links a later resolution had
written, had the same machines covered. Neither page was wrong about what it
read.

The block in `render.yaml` is live. Setting it up on Render is:

1. Dashboard → **Blueprints** → the Blueprint → **Sync**. Render reads
   `render.yaml` and creates one new service:

   | Setting | Value | Where it comes from |
   | --- | --- | --- |
   | Type | **Cron Job** | `type: cron` |
   | Name | `inputiv-daily-collector` | the Blueprint |
   | Region | Ohio | the same region as the web service and the database |
   | Instance type | Starter | `plan: starter` |
   | Schedule | `0 9 * * *` (09:00 UTC, daily) | the Blueprint |
   | Build | `pip install --upgrade pip && pip install -e .` | the Blueprint |
   | Command | `python scripts/collect_events.py --all-clients --days 1 --user render-cron` | the Blueprint |

2. **Attach the environment group.** The Blueprint asks for
   `inputiv-engine-config`, the same group the web service uses. Nothing in it
   changes; the cron reads it. If Render does not attach it automatically,
   the cron service → **Environment** → **Link Environment Group** →
   `inputiv-engine-config`.

3. **Paste the two secrets Render will prompt for**, which are `sync: false`
   because a Blueprint never carries a secret: `CREDENTIAL_ENCRYPTION_KEYS`
   and `PUBLIC_BASE_URL`. **Exactly the same values as the web service's** —
   a different credential key means the cron cannot decrypt a single stored
   credential and every vendor read fails with an auth error that looks like a
   vendor problem. `DATABASE_URL` is wired by the Blueprint from the Postgres
   instance and is not typed.

   It does **not** need `ANTHROPIC_API_KEY`: no model is called on this path.

4. **Trigger a run by hand** from the cron service's page (**Trigger run**) and
   read the log before trusting the schedule. For each client, in order, you
   should see: the events window and its per-site lines, the outcome ledger
   line, the device resolution line, the coverage snapshot line and the
   agreement line — the same shapes the validation log records from a laptop.
   The last line says how many clients failed, if any.

5. **Check it again the next morning.** A cron that was created and never ran
   looks exactly like one that ran and did nothing.

It runs at 09:00 UTC daily (04:00 Austin in summer, 03:00 in winter): after
the vendors' overnight scans and before anyone at a franchisee is working. It
reads only, writes to our tables and audits every read. `--days 1` plus the
collector's own day of overlap means a missed night is caught up by the next
run.

**`--all-clients` is every active client, oldest first, one after another.** A
client that fails is reported with its slug and the run carries on, because
one franchisee's vendor being down for an hour must not cost every other
franchisee their snapshot, their ledger and their day in the trend. The exit
code is non-zero if any client failed, so Render's own failure alerting is
what tells us; the log names which. To run one client by hand instead, the
command takes `--client <slug>` in place of `--all-clients`.

---

## 5a. Autopilot (Level 0: shadow mode)

Everything this branch adds is measurement. **No tool writes to any vendor**,
and the one switch that makes it *read* one starts false. The web service needs
no new secret: the model key and the Autotask credentials are already there.

Seven migrations come with it — **0023** (shadow runs, predictions,
enrichments), **0024** (labelling), **0025** (suggestions, reviews, blind
checks), **0026** (what a review may be about), **0027** (the iteration
sample), **0028** (prediction reuse), **0029** (batches). They create tables,
widen two check constraints and add three columns; nothing is rewritten and
nothing is dropped. `preDeployCommand: alembic upgrade head` applies them
before the new code takes traffic, as it does for every other migration.

### 5a.1 Environment variables

All eighteen are new — `main` has none of them — and **every one has a usable
default**, so a deploy that sets nothing starts and behaves. Four are worth
setting anyway, and two of those because the default is wrong *here* rather
than wrong in general. They are all non-secret and live in the
`inputiv-engine-config` group in `render.yaml`.

| Variable | Set it to | If it is missing |
| --- | --- | --- |
| `AUTOPILOT_SHADOW_LIVE` | **`false`** | Defaults to false. Set explicitly: it is the switch the whole live half hangs off, and "nobody set it" and "somebody decided" should not look the same. False means the forward shadow refuses to poll Autotask, the backfill refuses to run, and a live client's ticket page says "Suggestions are off" by name. The replay is unaffected — it reads stored rows. |
| `AUTOPILOT_MODEL` | **`claude-sonnet-5`** | Falls back to `ANTHROPIC_MODEL`, which this deployment deliberately leaves unset (§2c), so it would fall back again to the code default — **`claude-opus-5`**. Every classification would run on the most expensive model, and none of the measurements in the validation log would describe what production is doing. |
| `AUTOPILOT_MAX_TOKENS_PER_RUN` | **`8000000`** | 5,000,000, which is **under what a full replay of this tenant costs**: the 2026-10-01 replay read and wrote 5,220,378 tokens. A full replay would stop early with `stopped_reason = token_cap`, having answered most of the window. The sample (about 1.2M) is comfortably inside either figure. |
| `AUTOPILOT_BATCH_REPLAY` | **`false`** | Defaults to false. Set explicitly for the same reason as the switch: true means a replay's answers arrive within hours instead of minutes, which is a surprise worth choosing rather than inheriting. |
| `AUTOPILOT_CONCURRENCY` | leave unset (8) | 8 classifications in flight. The thing to lower if the model starts rate-limiting a long run. |
| `AUTOPILOT_POLL_REQUESTS_PER_HOUR` | leave unset (60) | 60 Autotask requests an hour for the forward shadow, counted by us. Only read when the switch is on. |
| `AUTOPILOT_DISPLAY_THRESHOLD` | leave unset (0.80) | A suggestion below 0.80 confidence is not offered to an engineer. Stored on every suggestion as it is made, so changing it later does not retroactively promote anything. |
| `AUTOPILOT_BLIND_RATE` | leave unset (0.2) | One ticket in five asks the two questions before it shows anything. That answer is the only accuracy figure the gate may read; at 0 the gate has nothing to read and says "not measured". |
| `AUTOPILOT_THRESHOLD_MIN_ACCURACY` | leave unset (0.88) | The fixed rule for choosing a threshold on the development period. Changing it after seeing a number is choosing a verdict. |
| `AUTOPILOT_THRESHOLD_MIN_COVERAGE` | leave unset (0.40) | The other half of that rule. |
| `AUTOPILOT_DOUBLE_LABEL_COUNT` | leave unset (20) | How many of a labelling sample go to a second person. |
| `AUTOPILOT_GROUP_WINDOW_DAYS` | leave unset (30) | How far back grouping may look. On every run's config, so a number that moves is explainable. |
| `AUTOPILOT_GROUP_SIMILARITY` | leave unset (0.7) | The title-similarity floor for grouping. |
| `AUTOPILOT_INPUT_COST_PER_MTOK` | leave unset until the rate is agreed | No price, so every run reports **tokens** and prints "unpriced" rather than a cost computed from a rate nobody set (the price-book rule). `AUTOPILOT_COST_CAP_USD` cannot bite without it. |
| `AUTOPILOT_OUTPUT_COST_PER_MTOK` | same | same |
| `AUTOPILOT_CACHE_READ_COST_PER_MTOK` | same | Cached input is priced at the input rate, which overstates — the right direction for a cap, the wrong one for a quote. |
| `AUTOPILOT_COST_CAP_USD` | leave unset | No dollar cap; `AUTOPILOT_MAX_TOKENS_PER_RUN` is the backstop and always applies. |

### 5a.2 What only the laptop has

The schema arrives with the deploy; the rows do not. Four things live in the
laptop database today and production needs them before any of this means
anything. If you are doing the step 6 dump *after* this deploy, all four come
across with it and there is nothing to do; if production's database was
restored earlier, do these by hand.

**1. The franchisee's label policy.** One remap, chosen on the development
period and worth 3 to 8 points of accuracy (validation log, 2026-10-01). It is
a row in `report_configs`, not a setting. In a Render shell:

```bash
cat > /tmp/autopilot-teamlogic.json <<'JSON'
{
  "forbidden_issue_types": [],
  "issue_type_remap": {
    "Customer Onboarding Offboarding": "User Access and Management"
  }
}
JSON
python scripts/manage_client.py report-config \
  --client teamlogic-it-austin \
  --autopilot-file /tmp/autopilot-teamlogic.json \
  --user render-shell
```

Read it back — with no flags the command changes nothing and prints the stored
settings:

```bash
python scripts/manage_client.py report-config --client teamlogic-it-austin
```

Expect an `autopilot` block holding exactly that one remap. Without it the
agent predicts a label the franchisee files 9 tickets a year under, and every
accuracy figure production shows is several points lower than the laptop's for
no reason anybody can see.

**2. The outcome ledger** — see 5a.3.

**3. The shadow runs.** 6 runs and 6,664 predictions for the pilot exist only
on the laptop. They are the results page's whole content and the reuse cache:
without them the page is an empty state (5a.4) and the first replay on Render
pays for every ticket again, because reuse is keyed on rows this database does
not have.

**4. The iteration sample.** One row plus 600 ticket ids. A production replay
draws its own on first use, from the same ledger with the same fixed seed — but
a sample is drawn from the rows that are *there*, so a production ledger that
differs by a single ticket draws a different 600. **Numbers from a laptop
sample run and a Render sample run are not comparable unless the sample row
came across with the dump.** The run records its sample id; compare those
before comparing accuracies.

Not needed: the demo tenant's labelling answers and blind checks (131 and 5)
are the demo's own. The pilot has **no blind checks and no labelling answers**
anywhere yet, which is why the gate's two accuracy criteria read "not measured"
rather than a number — on the laptop as much as on Render.

### 5a.3 Does production have the ticket ledger?

**The table, yes; the rows, only if they were dumped.** `ticket_outcomes` is
migration 0022, which is on `main` and already deployed. It is filled by the
daily collector, which is parked (§5), so nothing on Render has written to it
unless somebody ran the collector by hand from a shell. The laptop holds 2,745 rows for the pilot. Check from a
Render shell:

```bash
python scripts/resolved.py ledger --client teamlogic-it-austin --days 1 --json | head -20
```

That reports what the ledger holds and fills the last day. To fill ninety days
(an Autotask read, read-only, a few minutes, and the thing the replay needs):

```bash
python scripts/resolved.py ledger --client teamlogic-it-austin --days 90
```

**A replay with an empty ledger is not an error, it is a run over nothing**: it
reads `ticket_outcomes`, finds no rows, draws an empty sample and finishes
having answered nothing. So fill the ledger before running one, or run the
daily collector by hand; it is parked, so it will not do it for you (§5).

### 5a.4 Does the results page work with no runs?

Yes, and it is the state it will be in on the first day. `/autopilot/results`
asks for this client's runs, gets an empty list, and draws "No shadow runs on
this client yet. A run is started from the command line; this page only reads
them." The run picker says "No runs on this client yet", the detail panel stays
hidden, and the forward-shadow panel reports the switch as off by name
(`AUTOPILOT_SHADOW_LIVE`) rather than showing four zeroes that read like a
result. Both Autopilot pages are Inputiv staff only; a franchisee gets 403 on
the pages and on every endpoint behind them, and sees no nav entry.

### 5a.5 First checks, in a Render Web Shell

Dashboard → the service → **Shell**. These are in order, and each is a decision
in itself: the first two cost nothing, the third is a handful of model calls,
the fourth is a few hundred.

`AUTOPILOT_SHADOW_LIVE` stays **false** on the service. The two commands that
need it true get it **for that command only**, which is why each is written
with the variable in front of it: nothing is persisted, the web service never
changes, and the ticket page keeps saying "Suggestions are off" until you
decide otherwise.

**1. Can we reach Autotask from Render at all** — counts only, no names, one
`tool_call` audit row:

```bash
python scripts/validate_connector.py --client teamlogic-it-austin \
  --tool count_tickets --arg group_by=status --actor render-shell
```

Expect a count per status label. A 401 means the credentials did not come
across; a timeout means the vendor, not us.

**2. The backfill, dry** — names the tickets it would process and the vendor
requests it would spend, and **calls no model**:

```bash
AUTOPILOT_SHADOW_LIVE=true python scripts/autopilot.py shadow backfill \
  --client teamlogic-it-austin --dry-run --user render-shell
```

Without the prefix this prints "AUTOPILOT_SHADOW_LIVE is false … nothing was
called and nothing was written" and exits 2, which is the correct answer to
running it by accident.

**3. Two tickets, for real.** Take two open ticket numbers from the dry run's
output (or from `/tickets`) and substitute them:

```bash
AUTOPILOT_SHADOW_LIVE=true python scripts/autopilot.py shadow backfill \
  --client teamlogic-it-austin \
  --ticket T20260001 --ticket T20260002 --user render-shell
```

It reports tickets processed, suggestions above the display threshold, model
tokens, Autotask requests and the enrichment's own tool reads. Then open
`/tickets/T20260001` as an Inputiv account: the card is there but reads
"Suggestions are off" while the service's switch is false — the rows are
written and the page is deliberately not showing them yet.

**4. The whole open queue**, once the two look right:

```bash
AUTOPILOT_SHADOW_LIVE=true python scripts/autopilot.py shadow backfill \
  --client teamlogic-it-austin --user render-shell
```

It is **resumable by construction** — a ticket that already has a suggestion is
skipped — so a shell that closes mid-run is fixed by running it again. It
spends the same hourly request budget as polling and works out before it starts
how many tickets that leaves room for; a run that stops early says so.

### 5a.6 Turning the forward shadow on, and the cron

Two separate acts, in this order:

1. **The cron**, which makes suggestions appear for new tickets. The
   `inputiv-autopilot-forward` block in `render.yaml` is complete and
   commented out, like the collector's. Uncomment, commit, Blueprint → Sync.
   It carries `AUTOPILOT_SHADOW_LIVE=true` **on that service only**, so the
   web service's switch is still the thing that decides whether anybody sees
   the result. It needs `ANTHROPIC_API_KEY`, which the collector does not.
2. **The web service's switch**, when you want engineers to see the cards:
   `AUTOPILOT_SHADOW_LIVE=true` in the `inputiv-engine-config` group, which
   redeploys. Until then the pages read exactly as they do today.

`shadow forward --once` is the only mode there is: one polling pass, reading
from the end of the last pass (or `--since-hours` back when there has never
been one). Nothing polls on its own — there is no background task in the web
service — so the schedule is the cron and nothing else.

### 5a.7 Rolling back across 0023–0029

**A code-only rollback is safe, with one exception.** Pressing **Rollback**
leaves the schema at 0029 and puts older code back. Everything 0023–0027 and
0029 adds is invisible to older code: six new tables it never queries, two
widened check constraints on `suggestion_reviews` (a widened constraint accepts
everything the narrow one did), and two nullable columns on
`shadow_predictions`.

The exception is **`shadow_runs.reused`** (0028): `NOT NULL` with no database
default, because the model always supplies it. Code from before 0028 does not
know the column, so its `INSERT` omits it and Postgres has nothing to put
there. Nothing in the **web service** creates a shadow run — runs are started
from the command line — so the site stays up either way; but `shadow replay`,
`shadow forward` and `shadow backfill` would fail on the first run they tried
to create, with `null value in column "reused" violates not-null constraint`.
If you roll back past 0028 and need those commands, give the column a default
from a shell:

```bash
psql "$DATABASE_URL" -c "ALTER TABLE shadow_runs ALTER COLUMN reused SET DEFAULT 0"
```

It is harmless to the newer code, which always sets the value, so it can be
left in place or dropped again when you roll forward.

**Rolling the schema back** is `alembic downgrade 0022` and it is lossy, by
design and in this order: 0029 drops the batches, 0028 drops the reuse keys
(the next run then re-asks every ticket — money, not correctness), 0027 drops
the iteration sample, **0026 deletes every review of a finding, a next step or
a card** because the narrow constraint has nowhere to keep them, 0025 drops the
suggestions, reviews and blind checks, 0024 the labelling, 0023 the runs and
predictions — and 0023, 0024 and 0025 each delete the `audit_log` rows of the
event types they added. Read the migration before running it; none of this is
recoverable from the schema alone, only from a backup (§9).

---

## 5b. Automation: the triggers' poll (cron)

The automation engine (CLAUDE.md, "Automation") writes to no client tool, and
nothing in this section changes that. What it adds is a second poller beside
the forward shadow's: `scripts/automation.py poll --client <slug> --once`
reads the Autotask tickets that arrived since the last poll — through the
**same reader and the same hourly request budget** as the shadow
(`app/tickets/poller.py`, `AUTOPILOT_POLL_REQUESTS_PER_HOUR`) — and
evaluates every workflow's ticket and alert rules over them. A match
**drafts** a run and nothing more: the dry run, the submission and the
two-person approval are still done on the Automations page. It never calls
the classifier or any model; `tests/automation/test_imports.py` fails the
suite if the command could reach one.

Four migrations come with the engine: **0030** (runs, steps, switches, the
practice table), **0031** (triggers, approval expiry, the trigger log),
**0032** (the alert notes: notes, modes per workflow, alert tickets for the
measurement, reports; the `autotask_notes` connector name and the
`automation_note` audit event) and **0033** (workflows as data: the
definition's hash on runs and notes, each step's resolved inputs and
outputs; all nullable, nothing to backfill). The workflows themselves are
the YAML files in `app/automation/definitions/`, loaded when the service
starts; a bad file stops the start with one line naming it, which is what
`python scripts/automation.py validate` prints without starting anything,
so run it before a deploy that touches a definition.

### 5b.1 The setting

| Variable | Set to | What it does |
|---|---|---|
| `AUTOMATION_TRIGGERS_LIVE` | **`false`** in the config group | False means the poll refuses to read a vendor and says so by name. It is **separate from `AUTOPILOT_SHADOW_LIVE`** on purpose: the shadow classifies tickets, the engine's rules draft runs from them, and each has one owner and one switch. Even when true, a rule drafts only for a client whose workflow switch and trigger switch are both on (the Automations page), and never while the client's kill switch is thrown. |

### 5b.2 The command

```bash
# One pass. Reads from the end of the last poll, or --since-hours back (24)
# when this client has never been polled. --once is the only mode there is.
AUTOMATION_TRIGGERS_LIVE=true python scripts/automation.py poll --client <slug> --once
# reading tickets created since 2026-10-13 09:00 UTC
#   31 ticket(s) read, 31 evaluation(s), 2 matched, 2 run(s) drafted
#   Autotask: 2 request(s) of 60 this hour
#   the Automations page has the log; every draft still needs a dry run and an approval
```

Without the prefix it prints "AUTOMATION_TRIGGERS_LIVE is false … nothing was
called" and exits 2. Every pass is an `automation_control` audit row
(`control: poll`) with its counts, which is also where the next pass reads
its starting point from. Run it twice and a ticket still produces one draft:
one draft per ticket per workflow is checked by the evaluator and kept by a
unique index.

### 5b.3 The cron

The `inputiv-automation-poll` block in `render.yaml` is complete and
commented out, shaped like the forward shadow's. Uncomment, commit,
Blueprint → Sync. It carries `AUTOMATION_TRIGGERS_LIVE=true` **on that
service only**; the web service's value stays false, which changes nothing
the pages do — the pages read the drafts the poll made either way. Every
quarter hour, like the shadow; a pass costs two Autotask reads plus three
picklists the connector caches, so the two crons together stay inside the
hourly budget. It needs the database and the credential key, not the model.

The worker that executes approved runs (`scripts/automation_worker.py
--loop`) is a separate long-lived process and is not yet in `render.yaml`:
until a workflow that touches a client's tool exists, nothing needs it
running in production. The alert notes do not go through the worker; the
poll builds and posts them itself (5b.4).

### 5b.4 Alert notes: the one write

The same poll builds the alert notes (CLAUDE.md, "Automation: alert notes";
`docs/automation-notes.md` is the runbook for a client). Three things are
different from everything above it, and each is a decision somebody makes:

1. **A second Autotask credential, `autotask_notes`.** The note writer is a
   separate client with its own API user, requested from the franchisee with
   the minimum security level in `docs/automation-notes.md` (ticket view,
   ticket note add, nothing else) and never the read user. Store it from a
   Web Shell exactly as the read credential is stored (2a), with the
   connector name `autotask_notes`; there is no environment variable for it.
   A live client with no such credential cannot post: the pages show the
   note, Post fails with a reason, auto-post marks it `post_failed`.
2. **Nothing is posted until somebody decides.** Every workflow is in
   **preview** for every client by default: notes are built into our
   database and shown on the ticket page, and only an Inputiv engineer's
   *Post to Autotask* sends one. Auto-post is switched per client per
   workflow on the Automations page and needs the franchisee's approver
   recorded. The per-client setting `automation.notes.franchisee_can_post`
   (default false) is what lets the franchisee's own engineers see and post
   notes; `manage_client.py report-config --client <slug>
   --franchisee-can-post` turns it on. **Before the first live post** the
   checks under "Notifications" in `docs/automation-notes.md` are done in the
   franchisee's Autotask and recorded: workflow rules on a note being added,
   the ticket note notification defaults, the Client Access Portal.
3. **Internal only is read from the tenant, not configured.** The writer
   reads the tenant's `publish` picklist and sends only the value labelled
   *Internal Project Team*; a tenant without that label gets no note. There
   is nothing to set here, and nothing to set that could widen it.

The backfill is the same command the cron runs, pointed at the open queue:

```bash
AUTOMATION_TRIGGERS_LIVE=true python scripts/automation.py notes-backfill --client <slug> --dry-run
# prints every note the open alert tickets would get; stores nothing
AUTOMATION_TRIGGERS_LIVE=true python scripts/automation.py notes-backfill --client <slug>
# builds them as preview notes; never posts, whatever the mode
```

The poll cron (5b.3) then keeps them current; with every mode in preview it
spends the vendor reads a note needs (NinjaOne, Bitdefender and Autotask per
alert ticket, the tool-call audit rows say which) and makes no write.

---

## 6. Migrating the database from your laptop

The laptop's Postgres (`docker-compose.yml`, container `desk_platform-postgres-1`,
database `inputiv`) holds the pilot's clients, sites, mappings, encrypted
credentials, organisations, users, audit trail, reviews, coverage snapshots,
devices and ticket workspaces. All of it moves. Rendered decks under
`var/reports` do not: they are regenerable and the disk starts empty.

**Order matters:** create the Blueprint first (step 4), so the Render
database exists and the first deploy has created the schema at head; then
restore over it with `--clean`. Two things to know before the first command:

- **Credentials stay readable only if the key travels with them.** Every
  `tool_credentials` row is encrypted with the laptop's
  `CREDENTIAL_ENCRYPTION_KEYS`. Either set Render's
  `CREDENTIAL_ENCRYPTION_KEYS` to the *same* map as the laptop's `.env` (then
  the rows decrypt as they are), or keep a new production key as the active
  one and add the laptop's key to the map under its own id (`{"v1": "<laptop
  key>", "v2": "<new key>"}` with `CREDENTIAL_ENCRYPTION_ACTIVE_KEY_ID=v2`):
  old rows decrypt with v1, new rows encrypt with v2, and re-storing a
  credential with `manage_client.py set-credentials` moves it to v2. The
  second is the right one for production; do it before the restore, in the
  dashboard, and redeploy so the running service has both keys.
- **Version match.** `pg_dump` and `pg_restore` must be at least the server's
  major version. Render is Postgres 16; the local container is
  `postgres:16-alpine`, so run both tools *inside the container* and avoid
  whatever `pg_dump` happens to be on the laptop's PATH.

### 6a. Open the Render database to your laptop, temporarily

Dashboard → `inputiv-postgres` → **Access Control** → add your current
public address (`curl -sS https://api.ipify.org`) with a note "laptop,
migration, remove today". Copy the **External Database URL** from the same
page. It begins `postgres://` and needs `?sslmode=require`; below it is
referred to as `$RENDER_DB` and never written into a file.

### 6b. Dump the laptop database

```bash
cd /e/Repositories/desk_platform
docker compose exec -T postgres pg_dump -U inputiv -d inputiv \
  --format=custom --no-owner --no-privileges --file=/tmp/inputiv-laptop.dump
docker compose exec -T postgres ls -l /tmp/inputiv-laptop.dump
```

The dump stays inside the container; it holds encrypted credential
ciphertext, password hashes and every audit row, and it is deleted in 6f.

### 6c. Restore into Render

Run the restore from the same container so the tool version matches. Pass
the URL as an environment variable to the `exec`, so it is not in your shell
history's command line:

```bash
read -rs RENDER_DB     # paste the External Database URL with ?sslmode=require, then Enter
docker compose exec -T -e RENDER_DB="$RENDER_DB" postgres \
  pg_restore --dbname="$RENDER_DB" --clean --if-exists --no-owner --no-privileges \
  --exit-on-error /tmp/inputiv-laptop.dump
```

`--clean --if-exists` drops and recreates every object the dump holds, so
the schema the pre-deploy migration created is replaced by the laptop's,
which is at the same head; `alembic_version` comes across with it. One
warning is expected and harmless: `--clean` cannot drop `public` itself.
`--exit-on-error` stops at the first real error instead of continuing with
a partial restore.

### 6d. Verify row counts per table

The same query against both databases, sorted, then compared. It counts
every table in `public`, so a new table is never left out:

```bash
COUNT_SQL="select 'select ''' || tablename || ''' as t, count(*) from ' || quote_ident(tablename) || ';' from pg_tables where schemaname='public' order by 1"
docker compose exec -T postgres psql -U inputiv -d inputiv -Atc "$COUNT_SQL" \
  | docker compose exec -T postgres psql -U inputiv -d inputiv -At > /tmp/counts-laptop.txt
docker compose exec -T -e RENDER_DB="$RENDER_DB" postgres sh -c \
  "psql \"\$RENDER_DB\" -Atc \"$COUNT_SQL\" | psql \"\$RENDER_DB\" -At" > /tmp/counts-render.txt
diff /tmp/counts-laptop.txt /tmp/counts-render.txt && echo "row counts identical"
cat /tmp/counts-render.txt
```

`diff` prints nothing and `row counts identical` when every table matches.
The tables you should see, one line each with its count: `alembic_version`,
`audit_log`, `clients`, `collected_events`, `collection_coverage`,
`conversation_messages`, `conversations`, `coverage_exclusions`,
`coverage_snapshots`, `device_agreements`, `device_unresolved`,
`device_vendor_links`, `devices`, `invites`, `organisations`,
`report_configs`, `sessions`, `sites`, `tbr_reports`, `ticket_plans`,
`ticket_workspaces`, `tool_credentials`, `tool_mappings`, `users`. Any
difference is a stop: do not continue, re-run 6c, and if it differs again,
look at the restore log.

Then prove the credentials decrypt on Render, from a Render shell on the
service (not the laptop):

```bash
python scripts/manage_client.py show --client teamlogic-it-austin
python scripts/validate_connector.py --client teamlogic-it-austin --tool count_devices
```

The first prints the credential field names and key id (never values); the
second makes one read-only vendor call through the production service and
writes its audit row. Both answering means the key map in step 6 is right.

### 6e. Redeploy

The running instance has connection pools and caches from before the
restore. **Manual Deploy → Deploy latest commit** so it starts clean against
the restored data; `alembic upgrade head` in the pre-deploy step finds the
schema already at head and does nothing.

### 6f. Delete both dumps, close the door

```bash
docker compose exec -T postgres rm -f /tmp/inputiv-laptop.dump
docker compose exec -T postgres ls /tmp/inputiv-laptop.dump 2>&1 | grep -q "No such file" && echo "dump deleted"
rm -f /tmp/counts-laptop.txt /tmp/counts-render.txt
unset RENDER_DB
history -c
```

Then dashboard → `inputiv-postgres` → **Access Control** → remove your
address. The `ipAllowList: []` in `render.yaml` is the steady state: the
database is reachable only from Render services.

Sign in at the production address with the account you migrated. Its
second factor came across with the row (`users.totp_secret`, encrypted with
the credential key, which is why the key map had to be right first).

---

## 7. Backups

**Render's own.** Every paid Render Postgres is continuously backed up with
point-in-time recovery: three days back on the Basic tier, seven on Pro.
Recovery does not overwrite the database; it creates a *new* instance at the
chosen time (not within the last ten minutes) for you to inspect, then you
point the service at it (step 9). Render also keeps its logical backups
seven days.

**A manual dump you can run**, from your laptop with the External Database
URL and your address temporarily on the allow list as in 6a. Run it before
any deploy you are unsure of, and before the summit:

```bash
cd /e/Repositories/desk_platform
read -rs RENDER_DB
docker compose exec -T -e RENDER_DB="$RENDER_DB" postgres \
  pg_dump "$RENDER_DB" --format=custom --no-owner --no-privileges \
  --file=/tmp/inputiv-render-$(date +%Y%m%d).dump
docker compose cp postgres:/tmp/inputiv-render-$(date +%Y%m%d).dump ./var/
docker compose exec -T postgres rm -f /tmp/inputiv-render-$(date +%Y%m%d).dump
unset RENDER_DB
```

The file lands in `var/` (gitignored). It contains every audit row and every
credential's ciphertext. Store it where the password manager or the encrypted
company drive keeps such things, and remove your address from the allow
list again.

**The plain statement about the key.** A database backup, Render's or yours,
holds the vendor credentials and staff TOTP secrets *as ciphertext*. They are
readable only with `CREDENTIAL_ENCRYPTION_KEYS`. **Losing that key makes
every stored vendor credential and every enrolled second factor
unrecoverable: no backup, Render support, or anyone else can bring them
back.** The recovery is then re-issuing every vendor credential (NinjaOne,
Autotask, Bitdefender, ConnectSecure) with each vendor and re-enrolling
every authenticator. That is why step 2a puts a copy of the key in the
password manager, and why rotation (a new key id in the map, the old one
kept) is the only way a key ever leaves the map.

---

## 8. Cloudflare Access in front of the service

For the summit the service is for Inputiv staff and one franchisee's people.
Cloudflare Access puts an identity check in front of the whole hostname, so a
browser that has not proved who it is never reaches the sign-in page.

1. **DNS.** Put the domain on Cloudflare (nameservers), and add a proxied
   (orange cloud) `CNAME desk.inputiv.com → inputiv-engine.onrender.com`.
2. **Render custom domain.** Dashboard → the service → **Settings → Custom
   Domains → Add** `desk.inputiv.com`. Render issues its certificate; with
   Cloudflare proxying, set Cloudflare SSL/TLS to **Full (strict)** so the
   Cloudflare-to-Render leg is verified TLS too.
3. **`PUBLIC_BASE_URL`.** Set it to `https://desk.inputiv.com` in the
   service's Environment tab and redeploy, so invite links carry the real
   name.
4. **Access application.** Cloudflare Zero Trust → **Access → Applications →
   Add → Self-hosted**, domain `desk.inputiv.com`, session duration a day.
   Policy "Inputiv and the pilot": *Allow* where email ends with
   `@inputiv.com` **or** email is in the list of the franchisee's invited
   addresses. Identity provider: the one-time PIN by email is enough to
   start; Google or Microsoft login can be added without changing the
   policy.
5. **Bypass for the health check.** Render's health checker calls
   `/health` on the `onrender.com` hostname, which Cloudflare never sees, so
   nothing is needed for it. If an external uptime monitor is added, give it
   a **Service Auth** policy on `/health` rather than opening the path.
6. **What Access does not close.** The `onrender.com` hostname still answers
   without Cloudflare in the path. Render has no inbound address filter, so
   the honest position is: Access removes the sign-in page from the open
   internet on the real name; the application's own controls (sessions,
   argon2id, lockout, per-address throttle, TOTP for staff, tenant scoping)
   are what protect the `onrender.com` name and are the controls of record.
   Closing that gap is one middleware that verifies Cloudflare's
   `Cf-Access-Jwt-Assertion` header against the team's public keys and
   refuses requests without it, which is a small follow-up and should be done
   before any customer-facing use.

**What changes when the service opens publicly later.** Remove the Access
policy (or narrow it to `/settings` and the invite pages if a staff-only
edge is wanted); keep Cloudflare proxying. Add a Cloudflare **rate-limiting
rule** on `POST /auth/login` (ten a minute per address, matching
`LOGIN_RATE_LIMIT_PER_MINUTE`), because the application's own limiter is per
process and Cloudflare's sees every instance; then the worker count in
`render.yaml` may rise. Add a WAF managed ruleset. Set
`PUBLIC_BASE_URL` to whatever the customer-facing name is. Nothing in the
application changes: every route already declares its scope and the client
is never taken from a request.

---

## 9. Rollback

**To a previous deploy.** Dashboard → the service → **Events** (or
**Deploys**) → the last good deploy → **Rollback**. Render restarts the
service on that build with the *current* environment variables. Two things
the button does not do:

- It does not roll the database back. A migration in the bad deploy stays
  applied. If the previous code cannot run on the new schema, downgrade
  first, from a Render shell on the service:

  ```bash
  alembic current
  alembic downgrade -1          # or: alembic downgrade 0016
  alembic current
  ```

  Every migration in `alembic/versions/` has a `downgrade()`; the ones that
  add audit event types delete the rows of those types on the way down, so
  read the migration before running it.
- It does not restore environment variables. If a variable was changed for
  the bad deploy, change it back in the Environment tab before pressing
  Rollback.
- It does not roll stored documents back. A business review is JSON in
  `tbr_reports.document`, read through `ReviewDocument`, and a release can
  change what a field may hold: the 2026-09-29 release writes
  `show_contact_names: null` (no override of the franchisee setting) and a
  `monthly_report` section at slide 0, neither of which the code before it
  can read or render. **After any deploy that touched
  `app/reports/tbr/model.py`, and before rolling one back**, run from a
  Render shell:

  ```bash
  python scripts/check_stored_documents.py --include-superseded
  ```

  It loads every stored review through the model and prints how many load
  and the ids that do not, with the failing field and never the document;
  exit status 1 means the running code cannot read a row somebody may open.
  Rolling back past such a release needs the newer rows regenerated or the
  newer code kept; the script says which rows.

**To a database backup.** Dashboard → `inputiv-postgres` → **Recovery** →
choose the time (not within the last ten minutes) → **Restore**. Render
creates a *new* database instance; the original is untouched. Then:

1. Check the new instance: add your address to *its* allow list, run the
   row-count query from 6d against it, remove your address.
2. Point the service at it: the service's Environment tab → `DATABASE_URL`
   → change the `fromDatabase` reference to the new instance (or paste its
   internal connection string) → save, which redeploys.
3. Run `alembic current` from a Render shell and, if the restore point is
   older than the running code's head, let the next deploy's pre-deploy
   `alembic upgrade head` bring it forward.
4. Keep the old instance until you are sure, then delete it; it is billed
   while it exists.

From the manual dump in step 7 instead: create an empty database (or use
the existing one) and run the `pg_restore --clean --if-exists` command from
6c against it.

---

## 10. SOC 2 scope

**For this host (what to add to the system description and the control
matrix):**

- *Infrastructure*: one Render web service and one Render Postgres in the
  Ohio region, defined by `render.yaml` in version control; the cron service
  once enabled. Changes to infrastructure are commits reviewed like code.
- *Data classification*: the database holds customer (franchisee) vendor
  credentials encrypted at rest with an application key held in Render's
  environment; end-customer device, ticket and user data read from vendors;
  staff and franchisee accounts with argon2id password hashes and encrypted
  TOTP secrets; an append-only audit log of every prompt, tool call, sign-in,
  refusal and change (hard rule 4).
- *Access*: Render workspace membership (who can read environment variables,
  open a shell, restore a backup) is an access-review item; require two-factor
  on every Render account; the workspace member list is reviewed quarterly
  and on departure. The database is not publicly reachable
  (`ipAllowList: []`); temporary openings for a migration are recorded with
  date and removed the same day (this runbook, 6a and 6f).
- *Encryption*: TLS at the edge (Render, and Cloudflare in front); Render
  Postgres encrypts storage at rest; application-level encryption of
  credentials and TOTP secrets with a key that is never in the repository or
  the image, held in Render's environment and escrowed in the password
  manager (step 7).
- *Logging and monitoring*: Render service logs (retention per plan) plus the
  application's `audit_log` table, which is the record of record and is
  included in every backup; `/health` polled by Render; the daily collector's
  audit rows once enabled.
- *Backup and recovery*: Render point-in-time recovery (three or seven days
  by tier), the manual dump procedure in step 7, and the rollback procedure
  in step 9, with a documented recovery test before the summit.
- *Change management*: deploys are manual (`autoDeployTrigger: "off"`) and
  every deploy runs the migration in a pre-deploy step; the test suite,
  ruff and mypy pass before a commit reaches `main` (CLAUDE.md).
- *Vulnerability management*: dependency updates via `pyproject.toml`; the
  Python runtime version is pinned and reviewed.

**For Render as a subprocessor:**

- Add Render Services, Inc. to the subprocessor list with the purpose
  "application hosting and managed database", the data categories above,
  and the region (United States, Ohio).
- Obtain and file Render's current SOC 2 Type II report and sign its Data
  Processing Addendum; record the review date.
- Note Anthropic as an existing subprocessor for model inference (prompts
  carry customer device and ticket text, scrubbed of secrets by
  `app/tickets/scrub.py`; the vendor credentials never reach it) and
  Cloudflare for DNS, TLS termination and Access, once step 8 is done.
- Record the shared-responsibility split: Render operates the hosts,
  network, storage encryption and backups; Inputiv operates the application,
  the credential key, account lifecycle, and the audit log.

---

## 10a. Deploying the Aurora branch (`ui/aurora`)

One release, and three things about it that are not ordinary.

**No migration and no new dependency.** It is templates, stylesheets, page
scripts, two new Python modules and tests. An ordinary code deploy.

**Restart every worker, and check there is only one.** The icon guard refuses
to build an application whose nav names a drawing the icon file does not have,
but it cannot help a process that is already running: a uvicorn started before
the rename keeps the old Python in memory while Jinja reloads the new
templates from disk, and the nav then draws a crossed box on every row. On a
developer's machine this showed up as two listeners on port 8000, one of them
days old and bound to `127.0.0.1`, which is the address a browser uses — so
restarting the other one changed nothing. `netstat -ano | findstr :8000` before
concluding a restart took.

**Press Refresh on Coverage, once, for every client.** This branch carries
three changes to what the board counts:

* Cove is wired into the board, so Backup has a reader where it used to read
  `not assessed`;
* a site's **backup scope** is applied to the board, so a workstation at a
  customer who backs up servers only is `not_in_scope` rather than a gap, and
  `auto` now asks how many of a site's workstations are backed up rather than
  whether any is;
* `device_role` is decided from the node class, because `Device.os` holds a
  family for a NinjaOne-sourced row — which had been putting every Windows
  server through as a workstation.

**The board is stored, not computed on open** ("Coverage" in CLAUDE.md): the
page reads the latest snapshot, so until a new one is taken it will keep
showing numbers the old rules produced. The daily collector's cron is parked
(§5), so nothing takes one on its own. Refresh runs `take_snapshot` in the
background, one at a time per client, and the page reloads when it lands —
50 to 120 seconds on a large tenant. Do it per client from the Coverage page,
or run `python scripts/collect_events.py --all-clients --coverage-only` on the
box.

Until that is done the board is not wrong about what it read; it is reporting
a snapshot taken under the old rules, and the page says how old it is.

## 11. Checklist: the things only you can do, in order

Work down this list; each line points at the step with the commands.

1. **Render account and workspace** with a payment method; two-factor on
   the account. (step 0)
2. **Connect the GitHub repository** to Render and confirm `render.yaml` is
   on `main`. (step 4.1)
3. **Generate the production credential key** on your laptop and put it in
   the password manager. (step 2a)
4. **Create the Blueprint** and paste the four secret values when prompted:
   `CREDENTIAL_ENCRYPTION_KEYS`, `ANTHROPIC_API_KEY` (a new key from the
   Anthropic console), `PUBLIC_BASE_URL` (the `onrender.com` address for
   now), and empty Slack values. Apply, and watch the pre-deploy migration
   and the health check pass. (step 4)
5. **Confirm the two health URLs and run the SSE check**; record the finding
   in the validation log. (step 4a, 4b)
6. **Add the laptop's credential key to the map** as a second id (or reuse
   it), redeploy. (step 6, first bullet)
7. **Open the database to your laptop's address**, dump, restore, verify
   the row counts, prove a credential decrypts from a Render shell, redeploy,
   delete both dumps, close the allow list. (step 6a to 6f)
8. **Sign in** at the production address with your migrated account; enrol
   the authenticator if you had not. (step 4c)
9. **DNS**: move the domain to Cloudflare, add the proxied CNAME, add the
   custom domain on Render, set SSL to Full (strict). (step 8.1, 8.2)
10. **Set `PUBLIC_BASE_URL`** to the real name and redeploy. (step 8.3)
11. **Cloudflare Access** application and policy for staff and the pilot's
    addresses. (step 8.4)
12. **Take one manual backup** and file it; run the recovery procedure once
    against a restored instance and delete it, so step 9 has been done
    before it is needed. (steps 7, 9)
13. **After the summit**: uncomment the cron block, commit, sync the
    Blueprint, trigger one run and read its log. (step 5)
14. **Compliance filing**: subprocessor list, Render's SOC 2 report and DPA,
    the access review of the Render workspace. (step 10)

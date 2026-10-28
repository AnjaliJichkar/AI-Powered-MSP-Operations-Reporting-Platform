# Inputiv Engine

Shared backend behind **Desk**, **Resolved** and **Memory**. See
[CLAUDE.md](CLAUDE.md) for the rules this codebase is built under and
[docs/CONTEXT.md](docs/CONTEXT.md) for why.

Phase 1 build order steps 1 to 4 are complete: application skeleton, database
models, migrations and settings; the connector layer with NinjaOne as the worked
example; the Claude tool-call loop behind `/chat`, with a web chat UI; and the
Autotask PSA connector. Next is the October 2 demo freeze, then the remaining
connectors. The Slack interface is not built yet — the web page stands in for it.

## What exists today

```
app/
  main.py            FastAPI factory (create_app)
  settings.py        pydantic-settings; every value from the environment
  db/                declarative base, async engine, session helpers
  models/            Client, Site, ToolMapping, ToolCredential, AuditLog,
                     Conversation, ConversationMessage
  security/crypto.py Fernet encryption for credentials, with key rotation
  api/health.py      GET /health, GET /health/ready
  api/chat.py        POST /chat, POST /feedback, GET /clients
  api/conversations.py  POST/GET /conversations, GET …/messages, POST …/archive
  agent/conversations.py  threads: the client guard, replay, trimming, persistence
  interfaces/web.py  serves the chat page at /
  static/chat.html   the chat UI: no build step, vanilla JS
  audit/recorder.py  the one way to write an audit_log row
  agent/
    registry.py      the @tool decorator and the tool catalogue
    prompts.py       the system prompt, versioned
    loop.py          the Claude tool-call loop, with caps and audit
  connectors/
    base.py          auth, retry, backoff, pagination, error normalisation
    errors.py        ConnectorError and its subclasses
    factory.py       client_id (+site) -> credentials -> connector. Nothing else builds one.
    scope.py         ClientScope/SiteRef: the allowlist and this call's targets
    sync.py          reconciles vendor organisations into Site rows
    naming.py        per-client rules for which organisations are internal
    ninjaone/        client.py, models.py, tools.py
    autotask/        client.py, models.py, tools.py
    bitdefender/     client.py (read-only allowlist), models.py, tools.py
    connectsecure/   client.py (read-only GET-path allowlist), models.py, tools.py
    matching.py      pair devices across tools by hostname, within a site
  events/collector.py  the daily collector: vendor events, idempotent, with coverage
scripts/
  manage_client.py        register clients, store encrypted credentials, set sync rules
  sync_sites.py           discover a client's end customers from a vendor
  link_sites.py           merge or deactivate sites the matching got wrong
  validate_connector.py   run one tool against a live vendor API
  chat_cli.py             ask one question from the terminal
  selection_check.py      score which tool the model calls first, per question
  tbr.py                  generate, review and render a Client Business Review
  collect_events.py       the daily collector: Bitdefender detections into collected_events
alembic/             async env.py + 0001 schema, 0002 feedback, 0003 sites,
                     0004 site matching and mapping adoption, 0005 web search
                     audit step, 0006 conversations, 0007 eval interface,
                     0008 business reviews
tests/               333 tests: 271 need nothing, 62 need PostgreSQL
```

## Setup

Requires Python 3.12 and PostgreSQL 13+ (`gen_random_uuid()` must be in core).

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
cp .env.example .env
```

Generate an encryption key and put it in `.env`:

```bash
python -c "from app.security.crypto import generate_key; print(generate_key())"
```

Start a local database and apply the schema:

```bash
docker compose up -d postgres
alembic upgrade head
```

Run the API, then open <http://127.0.0.1:8000>:

```bash
uvicorn app.main:app
uvicorn app.main:create_app --factory --reload   # equivalent
```

`create_app` is a factory on purpose — importing `app.main` reads no settings and
opens no connection, which is what keeps the tests and Alembic fast. The
module-level `app` is built lazily on first access (PEP 562 `__getattr__`), so
the conventional uvicorn form works without reintroducing import-time side
effects.

Deploying to Render is `docs/deploy.md`, a runbook in order; `render.yaml` at
the root is the Blueprint it walks through.

## Checks

```bash
pytest             # unit tests; no database, no network
ruff check . && ruff format .
mypy               # strict, on app/
```

The database-backed tests are skipped unless `TEST_DATABASE_URL` is set. Create
the scratch database once, then run them:

```bash
docker compose exec postgres createdb -U inputiv inputiv_test
TEST_DATABASE_URL=postgresql+asyncpg://inputiv:inputiv@localhost:5432/inputiv_test \
  pytest -m postgres
```

They apply the migration for real and then:

- run `alembic check`, which fails if the models and the migrated schema have
  drifted apart — run this before merging any model change;
- exercise the constraints that carry the hard rules, rather than their
  declarations: two clients cannot claim the same vendor tenant, an unknown
  connector cannot be stored, a client with audit history cannot be deleted,
  credentials reach the database as ciphertext, and a request with no resolvable
  client is still audited.

Each test runs in a transaction that is rolled back, so the scratch database
stays empty. Never point `TEST_DATABASE_URL` at a database you care about: the
fixture starts by running `alembic downgrade base`.

### The page-scrolling test needs a local browser

`tests/browser/` holds one check that cannot be done without a layout engine:
every page is filled with more content than fits and asked to scroll to its last
element. It exists because the shell is the viewport and does not scroll —
`.shell-main` does, and Ask manages two scrollers of its own — and a flex or
grid child that is missing `min-height: 0` silently stops overflowing and grows
past the frame instead, taking its last paragraph off the bottom of the screen
with no scrollbar and no error. Nothing in a unit test can see that.

**It is skipped unless you set it up, and there is no browser in this project's
dependencies**, so it never runs in CI. To run it you need a server on a
database with some data in it, an account to sign in with, and a browser
automation command that accepts `viewport WxH`, `goto <url>` and
`js <expression>` and prints the expression's value:

```bash
SCROLL_CHECK_BASE_URL=http://127.0.0.1:8000 \
SCROLL_CHECK_EMAIL=you@example.test SCROLL_CHECK_PASSWORD=... \
BROWSE_BIN=/path/to/your/browser-driver \
  pytest tests/browser -q
```

`BROWSE_BIN` may be left out if the driver is in the usual place. The same
check runs on its own as `scripts/check_page_scrolling.py`, with the same
environment, and prints a row per page and width. Run it after changing the
shell, a page's layout, or anything in `app.css` that touches `overflow`,
`height` or `display: flex|grid`.

## Connectors

One connector layer, shared by every product (hard rule 5). A connector talks to
one vendor on behalf of one client, and it is never handed a `client_id` to
interpret: `app/connectors/factory.py` resolves that server-side before the
connector exists.

```
client_id -> Client            the franchisee; owns the credentials
          -> Sites             its active end customers
          -> ToolMapping       each site's organisation id at the vendor
          -> ToolCredential    decrypted with CredentialCipher
          -> connector instance carrying a ClientScope
```

### Two levels of tenancy

A **client** is the franchisee we work for. A **site** is one of *its* end
customers, and is what a vendor calls an organisation (NinjaOne) or a company
(Autotask). Credentials belong to the client; the vendor ids that scope a read
belong to its sites.

Sites are discovered, never typed in:

```bash
python scripts/sync_sites.py --client teamlogic-austin --connector ninjaone
python scripts/sync_sites.py --client teamlogic-austin --connector bitdefender
python scripts/sync_sites.py --client teamlogic-austin --connector connectsecure
python scripts/sync_sites.py --client teamlogic-austin --all --dry-run
python scripts/sync_sites.py --client teamlogic-austin --list
```

A site the vendor stops listing is marked inactive, not deleted — audit rows and
mappings point at it, and a customer that disappears for a day should not lose
its history.

Every tool takes an optional site. **With one**, the engine verifies the site
belongs to the current client *before any API call* and queries that vendor id
alone. **Without one**, the connector runs across the client's active sites and
merges the results, tagging every row with the site it came from, so a merged
answer can never read as though it came from one customer.

`ClientScope` (`app/connectors/scope.py`) carries both the client's whole
allowlist of vendor ids and the subset this call targets. `_scoped()` uses the
difference to tell two failures apart: a record from one of the client's *other*
sites means a vendor-side filter did not bite (dropped, warning), while a record
from outside the allowlist means another franchisee's data reached us (dropped,
error). Neither is ever merged in.

The model names a site by **slug**. The system prompt carries the client's site
list as names and slugs only — never ids — and the loop resolves the slug within
the current client, refusing anything outside it before the factory is involved.

`BaseConnector` provides what every vendor needs, so no connector reimplements
it: an auth hook with one automatic re-authentication on a 401, exponential
backoff with jitter on 429 and 5xx (honouring `Retry-After`, capped by
settings), cursor pagination that reports truncation instead of returning a
quietly short list, and mapping of every failure onto `AuthError`,
`RateLimited`, `NotFound` or `UpstreamError`. Nothing escapes as an `httpx`
exception.

Timeouts, the retry budget and the page cap are settings, never literals
(hard rule 6) — see `CONNECTOR_*` in `.env.example`.

### Adding one

Copy `app/connectors/ninjaone/` and change the specifics:

- `client.py` extends `BaseConnector` and adds the vendor's auth and reads;
- `models.py` models only the response fields we use;
- `tools.py` holds `@tool`-decorated functions, each with a `client_id: str`
  and a docstring written for the model to read;
- register the class in `CONNECTOR_CLASSES` in `factory.py`;
- record scrubbed fixtures under `tests/fixtures/<connector>/`;
- write down anything the vendor's docs left ambiguous in
  `docs/api-notes/<vendor>.md` rather than guessing in code.

The `@tool` decorator refuses, at import time, any function without a required
`client_id: str`. A tool that could run unscoped cannot reach the model.

### NinjaOne

Sixteen read-only tools: `list_devices` (with class and OS filters),
`count_devices`, `get_device`, `list_devices_not_checked_in(days=7)`,
`list_open_alerts`, `list_patch_status` (the waiting list: pending by default, filtered by severity, category or KB, with per-patch and per-site totals),
`list_patch_installs` (failed, installed or all), `list_pending_reboot`, `list_disk_space`,
`list_antivirus_status`, `list_software`, `list_windows_services` (one site at
a time), `get_hardware`, `list_custom_fields`, `list_logged_on_users` and
`list_device_activities`. The report tools read
the tenant-wide `/v2/queries/*` endpoints and place every row through the
device inventory, since those rows name a device and never an organisation.
Auth is OAuth client credentials; the token is held in
memory and refreshed on expiry or rejection, and the stored credential is the
client id/secret pair, never a token.

Device reads use `/v2/organization/{id}/devices`, so NinjaOne itself limits the
response to this client's organisation. `/v2/alerts` has no equivalent path, so
alerts are filtered on `device.organizationId` after the fact. Both paths run
through `_scoped`, which drops anything belonging to another organisation and
logs a warning — if a vendor-side filter is ever ignored, that is what stops one
franchisee's data reaching another. `docs/api-notes/ninjaone.md` records what
the vendor spec confirms and what is still unverified.

### Autotask

Thirteen read-only tools: `list_open_tickets` (filters for age, priority,
status, assignee and queue), `count_tickets`, `search_tickets` (title,
description and notes — "have we seen this before"), `get_ticket(ticket_number)`
with its recent notes, `list_recent_tickets`, `list_sla_breaches`,
`list_tickets_for_device`, `list_tickets_for_contact`, `list_contacts`,
`list_configuration_items`, `get_company`, `list_service_calls` and
`list_recent_time_entries(days=7)`.

Three things differ from NinjaOne and are worth knowing before touching it:

- **Auth is static headers**, not a token: `ApiIntegrationCode`, `UserName` and
  `Secret` on every call, with nothing to refresh.
- **The host is per tenant.** Every Autotask database lives in a numbered zone.
  The connector resolves it once through the zone-neutral endpoint and caches
  it, reusing the base class's auth lock so concurrent calls resolve it once.
- **Paging is `nextPageUrl`**, not a cursor parameter, so `BaseConnector.paginate`
  does not apply; the client follows the vendor's own URLs and reports
  truncation when the page cap stops it.

**Status and priority numbers mean different things in different Autotask
databases.** Datto's documentation is explicit that picklist values cannot be
published, so the connector never hardcodes them: "open" is
`completedDate notExist` rather than a status number, and labels are resolved
from the tenant's own `entityInformation/fields` and cached. A number with no
matching entry is reported as `8 (unknown)`, never given an invented label.

Scoping is `companyID` from `ToolMapping.external_id`, checked again on every
returned record. Three entities have no company id of their own: TicketNotes and
TimeEntries are scoped through the ids of tickets already known to be this
client's, and Resources are the MSP's own staff rather than client data, so they
are only ever fetched by id to turn an assignee into a name.

`docs/api-notes/autotask.md` records what the vendor's Swagger and docs confirm,
and the five things still unverified — the most important being whether
`nextPageUrl` comes back for a POST query, which if wrong would silently cap
listings at 500 records.

## Live validation

Fixture tests prove we parse what we recorded. They do not prove we recorded the
right thing, so a connector is not done until someone has run it against the
real console:

Register the client and store its credentials, then make the call:

```bash
python scripts/manage_client.py add --slug teamlogic-austin --name "TeamLogic IT of Austin"

export NINJAONE_CLIENT_ID='...' NINJAONE_CLIENT_SECRET='...'
python scripts/manage_client.py set-credentials --client teamlogic-austin \
    --connector ninjaone --region us

# Discover the franchisee's end customers before validating anything.
python scripts/sync_sites.py --client teamlogic-austin --connector ninjaone

python scripts/validate_connector.py --client teamlogic-austin --tool list_devices
python scripts/validate_connector.py --client teamlogic-austin --tool list_devices \
    --site acme-dental

# Bitdefender GravityZone: one API key from a partner administrator account,
# with only the Companies, Network, Incidents, Reports and Licensing APIs
# enabled. It cannot be scoped read-only by the vendor, so the pre-flight
# proves the connector refuses a write method before anything else is run,
# and warns, naming each one, if the key can call an API the connector never uses.
export BITDEFENDER_API_KEY='...'
python scripts/manage_client.py set-credentials --client teamlogic-austin \
    --connector bitdefender --region us
python scripts/validate_connector.py --client teamlogic-austin --check-bitdefender
python scripts/sync_sites.py --client teamlogic-austin --connector bitdefender

# ConnectSecure: the client id and secret of one portal user with a viewer role,
# plus the tenant name and pod number. The role is the write surface; the
# connector sends only the login and allowlisted GETs, and the pre-flight warns
# when the user can do more than read.
export CONNECTSECURE_CLIENT_ID='...' CONNECTSECURE_CLIENT_SECRET='...'
python scripts/manage_client.py set-credentials --client teamlogic-austin \
    --connector connectsecure --tenant <tenant name> --pod 401
python scripts/validate_connector.py --client teamlogic-austin --check-connectsecure
python scripts/validate_connector.py --client teamlogic-austin --tool count_endpoints \
    --arg group_by=status
```

`manage_client.py` is the only way to store a credential: `secret_ciphertext`
must be a Fernet token from the configured key, so it cannot be written by hand.
Secrets come from the environment or a hidden prompt, never from an argument
that would land in shell history.

`validate_connector.py` resolves credentials from the database exactly as the
agent will, runs one read-only tool, and writes the call to `audit_log` with
`interface=cli`. Record the outcome in `docs/validation-log.md`.

## Business reviews (TBR)

```bash
python scripts/manage_client.py report-config --client teamlogic-austin \
    --price device_replacement=cost:1450,hours:3,per:device
python scripts/tbr.py generate --client teamlogic-austin --site acme-dental --period 2026-08
python scripts/tbr.py status   --client teamlogic-austin --report <id>
python scripts/tbr.py generate-all --client teamlogic-austin --period 2026-08  # every mapped, active site
python scripts/tbr.py contact-names --client teamlogic-austin --report <id> --show  # default is Contact A, B
python scripts/tbr.py notes    --client teamlogic-austin --report <id> --section projects --text "..."
python scripts/tbr.py render   --client teamlogic-austin --report <id>                  # client deck
python scripts/tbr.py render   --client teamlogic-austin --report <id> --mode internal  # fact ids in the notes
python scripts/tbr.py render   --client teamlogic-austin --report <id> --mode demo      # pseudonyms, "Sample data"
```

or open `/tbr` for the review screen. `generate` reads NinjaOne and Autotask for one site, computes the facts and findings, drafts each section and stores a draft; every slide comes back as filled, awaiting engineer input, dropped or deferred to a later connector, with the reason. `render` fills `docs/reference/tbr-template.pptx` and refuses to write a file if any template placeholder would survive. A client deck (the default) has empty speaker notes unless you wrote some with `notes`; an internal deck adds the fact-id trace to the notes; a demo deck replaces every client, site, device, contact and engineer name with a consistent pseudonym, keeps every number and stamps "Sample data" on every slide. The trace is always on the stored review and the review screen. The Key Takeaways slide carries a scorecard computed by the rules in `app/reports/tbr/scorecard.toml` (thresholds are changed there, nowhere else); the Accomplishments slide opens with a code-counted "Value delivered" block, which leaves out any figure the connectors cannot supply. Tickets resolved outside business hours needs a time zone: `manage_client.py report-config --client X --timezone America/Chicago --business-hours 08:00-17:00` (on Windows a named zone needs the `tzdata` package; a fixed offset such as `UTC-06:00` does not). `generate-all` ends with drafted, failed and skipped sites and exits 1 if any failed. Industry Trends research is limited to `REPORT_WEB_SEARCH_ALLOWED_DOMAINS` (regulators and industry associations; vendor domains are refused), separate from the chat agent's `WEB_SEARCH_ALLOWED_DOMAINS`. The rules are in CLAUDE.md under "Business reviews"; the slide-by-slide contract is `docs/reference/tbr-slide-map.md`. Decks are written to `REPORTS_DIR` (`var/reports`, git-ignored): they hold customer data.

## Asking questions

```bash
uvicorn app.main:create_app --factory --reload   # then open http://127.0.0.1:8000
python scripts/chat_cli.py --client teamlogic-austin "what has not checked in this week?"
```

The page streams: it posts to `POST /chat/stream` (server-sent events), shows each tool call as it starts and settles, types the answer out as the model writes it, and then replaces the preview with the final result. `POST /chat` returns the same result in one piece and is what `chat_cli.py` and the selection check use. Closing the tab does not cancel a run: it finishes, is audited and is saved to the conversation.

```bash
curl -N -X POST http://127.0.0.1:8000/chat/stream -H 'Content-Type: application/json' \
  -d '{"user_id": "you", "client_id": "<client UUID from GET /clients>", "message": "what is alerting?"}'
```

To check that the model still reaches for the right tool as tools are added:

```bash
python scripts/selection_check.py --client teamlogic-austin
```

It runs the questions in `tests/selection/questions.yaml` through the real
agent, stops before any tool is executed (no vendor is called), and ends with
`N of M correct at T tools`. Its audit rows carry `interface = 'eval'`, so
normal traffic is `WHERE interface <> 'eval'`.

`run_agent(user_id, client_id, message, interface)` is the one entry point: the
web page, the CLI and (next) Slack all go through it, so they cannot drift apart.

Three things hold no matter what the model does.

**The client is the caller's choice, not the model's.** `client_id` is stripped
from every tool schema before the model sees it, so the model has no parameter
through which to name a client, and the resolved id is injected server-side on
every call. A model that invents the field anyway has it discarded, with a
warning logged. The client is named to the model by name and slug only; its UUID
never appears in the prompt.

**Every run is bounded.** `MAX_TOOL_CALLS`, `TOOL_TIMEOUT_SECONDS` and
`AGENT_TIMEOUT_SECONDS` are settings. A run that hits one returns
`status="capped"` or `"timeout"` with a partial answer that reports what
actually ran — the model is not given a chance to summarise under pressure,
because that is exactly where invented detail comes from.

**Every step is audited.** Prompt, each model call with its token counts, each
tool call and result with timing, and the final answer. The final row's id comes
back as `audit_log_id`, which is what `/feedback` rates.

A connector failure does not end the run: the normalised error goes back as a
tool result so the model can tell the engineer what broke. The answer says
"NinjaOne rate-limited the request, so I could not check", never a guess.

### Conversation threads

A question is one turn of a thread, so an engineer names a device once. `POST
/chat` takes an optional `conversation_id`; without one the server starts a
thread and returns its id. The thread's stored messages — the engineer's
questions and the model's content blocks verbatim, tool calls, tool results
and web searches included — are replayed ahead of each new question.

| Endpoint | |
|---|---|
| `POST /conversations` | start an empty thread for a client |
| `GET /conversations?client_id=&user_id=` | the caller's open threads, newest first |
| `GET /conversations/{id}/messages?user_id=` | a thread's messages, in order |
| `POST /conversations/{id}/archive` | archive; nothing is deleted |

A thread belongs to one client and one engineer. A request scoped to client B
carrying client A's thread is refused with 403 before any model call, and
writes nothing. Over `CONVERSATION_CONTEXT_BUDGET` the replayed copy is
trimmed oldest-first (tool results stubbed, then whole turns dropped, never
the newest four) and a `context_trimmed` audit row records it; the stored
thread is never edited. `audit_log.conversation_id` is `ON DELETE RESTRICT`,
so archiving — or anything else done to a thread — cannot remove an audit row.

### The chat page

Served at `/` from `app/static/chat.html`. Plain HTML, CSS and vanilla JS, no
build step. Pick a client, type a handle (remembered in the browser), ask.
Answers render as markdown; the tool calls behind each answer are in a
collapsible section; thumbs up/down posts to `/feedback`.

The sidebar lists your threads for the selected client. Opening the page
resumes the most recent one; "New conversation" deliberately forgets the
remembered client and makes you pick one before the first message, because the
quickest way to ask the wrong client a question is not to notice which one is
selected. Every message is sent with its `conversation_id`.

Markdown is sanitised with DOMPurify before it reaches the DOM. That is not
belt-and-braces: device and alert names come from a client's own RMM, so a
machine named `<img src=x onerror=...>` is untrusted text arriving through our
API. If either CDN script fails to load the page falls back to escaped plain
text rather than rendering raw HTML.

**There is no authentication.** Phase 1 is internal, on a private network
(CLAUDE.md, "Out of scope"), and `user_id` is an attribution label for the audit
trail, not an identity claim. Put real auth in front of this before it is
reachable from anywhere else.

### The system prompt

`app/agent/prompts.py`, versioned, and the version is recorded on every audit
row so an old answer can be read against the prompt that produced it. Change it
in a commit that says why (CLAUDE.md, "Agent loop"). It is also the cached
prefix of every request, which is why the per-client detail lives in the first
user message instead of in the prompt.

## The data model

| Table | Purpose |
|---|---|
| `clients` | One MSP tenant. Its UUID is the `client_id` every tool call requires. |
| `tool_mappings` | `client_id` → the vendor's own identifier (NinjaOne org, Autotask company). |
| `tool_credentials` | Encrypted secrets, one row per `(client_id, connector_name)`. |
| `audit_log` | Append-only record of every prompt, tool call and result. |

Decisions worth knowing:

- **`clients.id` is a UUID**, because it travels outside the database — into tool
  arguments, Slack commands and audit rows. Every other primary key is
  `bigint generated always as identity`.
- **`tool_mappings` has a unique constraint on `(connector_name, external_id)`.**
  A vendor organisation can belong to exactly one site, so a misconfiguration
  cannot quietly point two clients at the same NinjaOne organisation, and
  `sync_sites` aborts rather than claim one another client holds. A check
  constraint keeps vendor ids off tenant-level rows, which exist for per-client
  configuration only.
- **`audit_log.client_id` is nullable and `ON DELETE RESTRICT`.** Nullable so a
  request rejected before a client could be resolved is still recorded (with the
  raw identifier in `client_ref`); RESTRICT so the audit trail cannot be deleted
  out from under itself.
- **Enums are `varchar` + check constraint**, not native Postgres enum types: a
  value can be added or removed in one ordinary migration.
- **`updated_at` is maintained by the ORM**, not a trigger. Code that writes
  through raw SQL must set it.
- `audit_log` is the only table expected to grow without bound. It is not
  partitioned yet; revisit around 100M rows.

## Credential encryption

`tool_credentials.secret_ciphertext` is a Fernet token over a JSON object of
secret fields. `encryption_key_id` records which key produced it, so rotation is
a config change rather than a bulk re-encryption: retired keys stay listed in
`CREDENTIAL_ENCRYPTION_KEYS` and keep decrypting old rows while only the active
key encrypts new ones. `CredentialCipher.needs_rotation()` identifies rows still
on an old key.

Nothing outside `app/security/crypto.py` handles a raw key, and
`ToolCredential.__repr__` deliberately omits the ciphertext.

## Migrations

```bash
alembic revision --autogenerate -m "what changed"   # needs a live database
alembic upgrade head
alembic downgrade -1
```

`alembic/env.py` takes its URL from `app.settings` (or `-x db_url=...`), so no
connection string is stored in `alembic.ini`.

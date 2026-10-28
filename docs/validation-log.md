# Live validation log

This log records counts, ids and site names, never device or user names.

A connector is not done until a human has run at least one call against the
real vendor console and confirmed the answer matches what the console shows
(CLAUDE.md, "Testing and quality"). Fixture tests prove we parse what we
recorded; only this proves we recorded the right thing.

Record every live run here. Append, never rewrite.

## Onboarding a client for the first time

This section is about the vendor credentials the engine reads with. Giving a
person a login is a separate job: see "Accounts: who can sign in" below, and
`scripts/manage_auth.py`.

You need three things from the vendor before anything can run: the tenant id,
a client id and a client secret. For NinjaOne, in the client's own NinjaOne
instance, under **Administration -> Apps -> API** (exact labels vary by
release), create an API application with:

- **Client credentials** as the allowed grant type — the engine is
  machine-to-machine and never does a browser redirect;
- the **monitoring** scope, which is what both endpoints we call require;
- **read-only** permissions. Nothing in Phase 1 writes (hard rule 2), so a
  read-only application means the credential itself cannot perform a write even
  if our code changed. Ask the franchisee for read-only explicitly.

The client secret is shown once, at creation. You do not need an organisation
id here: the franchisee's end customers are discovered by sync_sites below.

Then register the client locally. The secret goes in the environment, never in
the command, because an argument lands in your shell history and in the process
list:

```bash
python scripts/manage_client.py add \
    --slug teamlogic-austin --name "TeamLogic IT of Austin"

export NINJAONE_CLIENT_ID='...'
export NINJAONE_CLIENT_SECRET='...'
python scripts/manage_client.py set-credentials \
    --client teamlogic-austin --connector ninjaone \
    --region us

python scripts/manage_client.py show --client teamlogic-austin
```

On Windows PowerShell the line continuation is a backtick, and environment
variables are set differently:

```powershell
$env:NINJAONE_CLIENT_ID = '...'
$env:NINJAONE_CLIENT_SECRET = '...'
python scripts/manage_client.py set-credentials `
    --client teamlogic-austin --connector ninjaone `
    --region us
```

Omit the environment variables and the script prompts for them without echoing.
`show` prints the credential's field names, key id and config, never its values.

### Autotask

From the client's Autotask you need an **API-only user** (Admin -> Resources,
security level "API User (API-only)") with read-only permissions, its **secret**,
and the **integration code** issued for the integration. You do not need a
company id here either. Ask for read-only explicitly:
nothing in Phase 1 writes, so a read-only API user means the credential itself
cannot write even if our code changed.

```bash
export AUTOTASK_API_INTEGRATION_CODE='...'
export AUTOTASK_USERNAME='...'
export AUTOTASK_SECRET='...'
python scripts/manage_client.py set-credentials \
    --client teamlogic-austin --connector autotask
```

No region or host is needed: the zone is resolved from the API user at connect
time. `--base-url` exists only to pin a zone or point at a stub.


## Sites: discover the end customers first

Credentials belong to the franchisee; the vendor ids that scope a read belong to
its end customers, which the engine calls sites. Nothing can be validated until
those exist, so run this after storing credentials and again whenever the
franchisee onboards or loses a customer:

```bash
python scripts/sync_sites.py --client teamlogic-austin --all --dry-run
python scripts/sync_sites.py --client teamlogic-austin --all
python scripts/sync_sites.py --client teamlogic-austin --list
```

### What sync does, and what to check afterwards

Sync is a reconciliation, not an upsert, and each rule is something to verify
against the console the first time it runs for a franchisee:

- **One site per customer across vendors.** "Acme Dental, LLC" in Autotask and
  "Acme Dental (Main St)" in NinjaOne become one site with two mappings. The
  match is on the normalised name (trimmed, casefolded, punctuation and a
  trailing parenthetical removed, legal suffixes such as LLC/Inc/PLLC dropped).
  A site whose `--list` line shows only one vendor when the console has both is
  a name the heuristic did not join — see `link_sites.py` below.
- **Only customers.** Autotask companies are synced only when `companyType`
  resolves to the tenant's "Customer" label (through the picklist, never
  assumed to be 1) and `isActive` is true; company 0 is always skipped. If the
  tenant has renamed the type, sync fails with the labels it did find.
- **Internal organisations are recorded inactive.** By default a NinjaOne
  organisation starting with `zz`, an Autotask company matching `\bteamlogic\b`,
  or any organisation named like the client itself is created with
  `is_active = false`: visible in `--list` as `[INACTIVE]`, never in scope. The
  rules are per client and per connector:

  ```bash
  python scripts/manage_client.py sync-rules --client teamlogic-austin --connector ninjaone --show
  python scripts/manage_client.py sync-rules --client teamlogic-austin --connector ninjaone \
      --internal-prefix zz --internal-prefix "TL-" --no-match-tenant-name
  ```

- **Old tenant-level mappings are adopted.** A client configured before sites
  existed has one mapping with a vendor id and no site. Sync moves it onto the
  site it names instead of creating a second row. A vendor id that another
  client already holds aborts the whole sync with `SyncConflict` and writes
  nothing — that is hard rule 1, and the fix is deciding which franchisee owns
  the organisation, not re-running.
- **Nothing is deleted.** A site the vendor stops listing is marked inactive and
  reactivated if it comes back. A renamed organisation keeps its site and slug.

### Correcting the result

```bash
# Two sites that are really one customer: keep the first, fold in the second.
python scripts/link_sites.py --client teamlogic-austin \
    --keep acme-dental --merge acme-family-dentistry
# A site that should never be in scope.
python scripts/link_sites.py --client teamlogic-austin --deactivate teamlogic-lab
```

A merge moves the merged site's mappings to the kept site, keeps the kept
site's name and slug, and deactivates the merged one; it is refused when both
sites already map the same connector, because two different vendor
organisations cannot be one site. For a vendor-side organisation that is really
the franchisee's own, prefer a `sync-rules` entry over `--deactivate`: the rule
survives the next sync, a manual deactivation is overridden by it.

In development only, `sync_sites.py --reset` deletes the client's sites and
site-level mappings first, so the result is exactly what the vendors report
today. It is refused outside `ENVIRONMENT=local` and changes every site's UUID.

Check the list against the vendor console before trusting any answer: a missing
customer means every question about it will correctly report nothing, which is
easy to mistake for a quiet site.

## Accounts: who can sign in

A client can be onboarded, synced and reported on before anybody has an
account: credentials belong to the `Client`, and accounts belong to an
`Organisation` (CLAUDE.md, "Authentication and authorization"). So this is a
separate job from the one above, and it is the one that makes the web pages
usable by a person.

Everything here is `scripts/manage_auth.py`. It needs database credentials,
which is the point: it is the only way an account is made without an invite,
and it runs on the box by whoever deploys.

```bash
python scripts/manage_auth.py --help
```

### Once per deployment: the Inputiv organisation and the first administrator

There is a bootstrapping problem — an invite is created by an administrator,
and the first administrator has nobody to invite them. This is the answer:

```bash
# The password is never a command-line argument: an argument lands in your
# shell history and in the process list. Either export it, or let the script
# prompt twice without echoing.
export INPUTIV_ADMIN_PASSWORD='...'
python scripts/manage_auth.py bootstrap --email you@inputiv.com
```

It creates the organisation `inputiv` and one `inputiv_admin`. Running it again
is safe: the organisation is reused, and a second administrator is only added
if the address is new.

That account can now sign in at `/login` and, being Inputiv staff, switch
between clients in the header.

### Once per franchisee: their organisation, then their people

The client must already exist (`manage_client.py add`). Then:

```bash
python scripts/manage_auth.py add-organisation \
    --client teamlogic-it-austin --slug teamlogic-austin
python scripts/manage_auth.py invite \
    --email owner@example.com --role franchisee_admin \
    --organisation teamlogic-austin
```

Roles are `inputiv_admin`, `inputiv_engineer`, `franchisee_admin` and
`franchisee_viewer`; the first two only in `inputiv`, the last two only in a
franchisee's organisation.

`invite` prints a **link**, not a bare token — built from `PUBLIC_BASE_URL`,
which has to be set for a deployment to email anything usable, and which must
be https anywhere but `local`:

```
  Send them this link. It works once, and is not recoverable:
  https://desk.inputiv.example/invite/<token>
```

Opening it shows them who invited them and into which organisation, takes a
password twice, and signs them in on Ask. Only the token's hash is stored, so
a lost link is **reissued**, never looked up:

```bash
python scripts/manage_auth.py invite --email owner@example.com \
    --role franchisee_admin --organisation teamlogic-austin --reissue
```

Without `--reissue` a second invitation for an address is refused. That is
deliberate: two live tokens for one address means whoever holds the older email
uses it, the newer link then fails because the account exists, and the trail
says two people were invited once each. `--reissue` withdraws the outstanding
one first.

For one sent to the wrong address, reissuing to the right address does nothing
about the wrong one, which is still a live link into an organisation:

```bash
python scripts/manage_auth.py revoke-invite --email wrong@example.com
```

A link that has expired, been used, been withdrawn, or is simply not one of
ours gets a page saying which and what to do — not a stack trace, and not a
bare 404. An Inputiv admin, or that franchisee's own admin, can also invite
from the Settings page once they are signed in.

### Day to day

```bash
python scripts/manage_auth.py list-users                      # everybody
python scripts/manage_auth.py list-users --organisation inputiv
python scripts/manage_auth.py suspend --email them@example.com
python scripts/manage_auth.py activate --email them@example.com
python scripts/manage_auth.py reset-password --email them@example.com
```

`suspend` and `reset-password` end every session that account holds, so the
change takes effect on their next request rather than whenever a token happens
to expire. There is no password-reset-by-email: an administrator runs
`reset-password` and tells them the new one.

An address that has been invited but has not accepted is not a user yet, so
`suspend` and `reset-password` say so, and name when the invitation expires.
Use `revoke-invite` on it instead, or let it expire (`INVITE_EXPIRY_HOURS`,
72 by default).

## How to run one

```bash
# what can be run
python scripts/validate_connector.py --list-tools

# one tool, one client, against the live API
python scripts/validate_connector.py --client teamlogic-austin --tool list_devices
python scripts/validate_connector.py --client teamlogic-austin \
    --tool list_devices_not_checked_in --days 14
python scripts/validate_connector.py --client teamlogic-austin --tool list_open_alerts

# one end customer, by slug; omit --site to cover them all and merge
python scripts/validate_connector.py --client teamlogic-austin --tool list_devices --site acme-dental
python scripts/validate_connector.py --client teamlogic-austin --tool list_open_tickets --site acme-dental

# any other tool argument goes through --arg NAME=VALUE
python scripts/validate_connector.py --client teamlogic-austin --tool list_open_tickets
python scripts/validate_connector.py --client teamlogic-austin \
    --tool get_ticket --arg ticket_number=T20260915.0042
python scripts/validate_connector.py --client teamlogic-austin \
    --tool list_tickets_for_device --arg device_name=<device-name>
python scripts/validate_connector.py --client teamlogic-austin \
    --tool list_recent_time_entries --days 7
```

The script resolves credentials from the database exactly as the agent will,
runs one read-only tool, and writes the call to `audit_log` with
`interface=cli`. Then open the vendor console and check the numbers agree.

What to check, beyond "it returned something":

- the device or alert count matches the console for that organisation;
- a named device you can see in the console appears, spelled the same way;
- timestamps are plausible — a date in the year 5000 means the vendor switched
  to millisecond epochs (see `docs/api-notes/ninjaone.md`);
- `truncated` is `false`, or you understand why it is not;
- no "response was not scoped" warning appears in the log output (run with
  `--verbose`). That warning means a vendor-side filter was ignored and only
  our client-side scoping stopped another client's data coming through.
- when no `--site` is given, every row names the site it came from, and the
  count is the sum across the client's active sites;
- when `--site` is given, only that customer's records come back;
- no "outside the allowlist" error appears in the log output (run with
  `--verbose`). That would mean another franchisee's records reached us.

## Entries

| Date | Connector | Client | Tool | Result | Run by | Notes |
|---|---|---|---|---|---|---|
| 2026-09-10 | ninjaone | _(not recorded)_ | all three | pass | PS | Reported as "giving the correct data points" across `list_devices`, `list_devices_not_checked_in` and `list_open_alerts`. **Specifics were not captured at the time** — see below. |

The 2026-09-10 entry is thinner than an entry should be. Nobody wrote down the
client, the device count, the region, or whether the `df` scoping warning
appeared, so it confirms the connector works but settles none of the open
questions in `docs/api-notes/ninjaone.md`. Fill those in on the next run; the
checklist above says what to capture.

### Template

```
| 2026-09-15 | ninjaone | teamlogic-austin | list_devices | pass | PS |
  142 devices, matches console. Region us. lastContact dates plausible.
```

## Open items before the October 2 demo freeze

- [x] NinjaOne credentials for pilot franchisee 1, with the `monitoring` scope
      on a read-only application.
- [x] Run all three NinjaOne tools live (2026-09-10; details not captured).
- [ ] Re-run one tool with `--verbose` and record the client, device count and
      region, so the entry above is auditable.
- [ ] Confirm the franchisee's NinjaOne region (see api-notes item 2).
- [x] Confirm the `df` filter on `/v2/alerts` is honoured (2026-09-17: 40
      alerts across 53 sites, none dropped as another site's — see api-notes
      item 1). Note that until the same day every live alert had been dropped
      as unclassifiable, because the vendor sends no embedded `device`;
      alerts are now resolved through the device inventory.
- [ ] Note the real rate limits once a 429 has been observed.

### Report tools, live on 2026-09-17 (pilot franchisee 1, all sites)

Each run through `validate_connector.py --verbose`; no guard warning or error
on any of them, so every row resolved to a site through the device inventory.

| Tool | Result |
|---|---|
| `list_pending_reboot` | 96 of 1,032 devices need a reboot; 3.2 s |
| `list_disk_space` (below 10%) | 31 of 1,144 fixed volumes; 4.0 s |
| `list_antivirus_status` | 1,012 unhealthy rows across 1,032 devices, capped at 200 with `truncated: true`; product counts led by Defender 962 and Bitdefender 883. `NONE` appears as a product name (36 rows): no AV at all, reported unhealthy. One product name arrives with a mangled character (`OpenText� Core…`) — vendor-side encoding, passed through |
| `list_patch_status --arg failed_only=true` | 0 failed patches (both reports queried with `status=FAILED`) |
| `list_logged_on_users --arg user=admin` | 35 devices last used by an `admin` account |
| `count_tickets` | 117 open tickets across 16 statuses (`Follow Up` 39, `Scheduled` 21, …); 6.2 s |
| `list_sla_breaches` | 89 breaches on 117 open tickets: 74 past due date, 15 past resolution due; no first-response breaches; 8.5 s |
| `list_recent_tickets --days 3` | 113 tickets created in three days, 85 already complete; 13.3 s |

Not yet run live: `get_device`, `list_device_activities`, `list_software`
(needs a device name / an application name — pick one from `list_devices`),
`list_contacts`, `list_tickets_for_contact`, `get_company` (needs `--site`).
`list_configuration_items` and `list_tickets_for_device` **cannot** run on
this tenant until the API user can query ConfigurationItems (below).

- [ ] **Autotask API user security level.** Contracts and ConfigurationItems
      answer HTTP 500 to every query while Tickets, Companies, Contacts and
      Resources work (api-notes). Ask the franchisee to grant the API user
      read access to Contracts and Configuration Items, then run
      `list_configuration_items`, `list_tickets_for_device` and `get_company`
      (check `active_contracts` is a number, not null) and record the results.
- [ ] Run the six report tools above that need an argument, one each, and
      record them here.

### Search, hardware and service tools, live on 2026-09-18 (pilot franchisee 1)

Each run through `validate_connector.py --verbose`, all OK.

| Tool | Result |
|---|---|
| `list_patch_install_failures` (7 days) | 19 failed installs on 16 devices. The same day `list_patch_status --arg failed_only=true` still returned 0: the two reports are different things |
| `get_hardware` | 1,032 devices, capped at 200 with `truncated: true`; `age_available: false` — NinjaOne holds no purchase or BIOS date |
| `get_hardware --arg older_than_years=5` | refused without fetching, with the reason; no age is guessed |
| `list_custom_fields` | 55 definitions; the 5 `TEXT_ENCRYPTED` ones listed as withheld, values never requested |
| `list_windows_services --site rite-management-services` | 19,097 stopped services, 751 of them auto-start and not running (sorted first); capped at 200 |
| `list_windows_services` (no site) | refused before any API call: 311,880 rows tenant-wide |
| `search_tickets --arg query=printer --arg days=30` | 40 tickets; 37 with `include_notes=false`, so 3 were found through notes alone |
| `count_tickets --arg group_by=queue` | Help Desk 48, Escalation 47, Project Tasks 18, Alerts 2, System Maintenance 1, Triage 1 |
| `list_service_calls --arg days=30` | 0 — the tenant has never scheduled a service call. Reported as an empty result, not an error |

The site-scoped `list_windows_services` run logged a false "GET /v2/devices
response was not narrowed … dropped 923" warning. Cause and fix are in
api-notes/ninjaone.md ("The inventory is the whole client's"); fixed the same
day with a regression test.

- [x] Re-run after the fix (2026-09-18): `list_windows_services --site
      rite-management-services` returned 19,066 stopped services with no
      warning. The cache half of the fault cannot be shown from the CLI (each
      run is its own process); `test_a_site_scoped_call_does_not_leave_a_
      one_site_inventory_in_the_cache` covers it.
- [ ] `list_service_calls` has never seen a live row (api-notes/autotask.md,
      unverified item 6). Validate on the first tenant that uses service calls.
- [ ] `list_custom_fields --arg field=<name>`: run once with a harmless field
      chosen from the definitions and check the values against the console.

### Tool selection check, 2026-09-18 (pilot franchisee 1, `claude-sonnet-5`, effort high)

`scripts/selection_check.py --client teamlogic-it-austin`, the ten seed
questions in `tests/selection/questions.yaml`, select-only (no tool executed,
no vendor called). 29 tools: NinjaOne 16, Autotask 13.

| Run | Prompt | Result | Notes |
|---|---|---|---|
| 1 | v7 | 10 of 10 | "how many servers does each customer have?" chose `count_devices`, but took 18.2 s and 1,588 output tokens against ~2.5 s and under 130 for every other question |
| 2 | v8 | **9 of 10** | the same question chose `list_devices` |
| 2 (cont.) | | | that choice took 7.5 s and 626 output tokens |
| 3, 4 | v8, after the fix below | 10 of 10, 10 of 10 | that question chose `count_devices` in 2.7 s and 3.6 s (122 and 182 output tokens): the deliberation went away with the gap |

The miss was a **tool gap, not a description problem**: `count_devices`
grouped by one dimension and had no class or OS filter, so "servers per
customer" could not be answered by one count call, and the model — correctly
— fell back to a `list_devices` it would then have had to count from, capped
at 200 rows. `count_devices` now takes the same `node_class` and `os` filters
as `list_devices`. Live: `count_devices --arg node_class=server` returned 66
servers by site, which matches `group_by=node_class` on the same day (62
`WINDOWS_SERVER` + 4 `LINUX_SERVER`; 1,048 devices in all).

Typical question: ~2.4 s to choose, ~23,400 tokens read, of which ~19,600
are the cached prompt and tool list and ~3,750 are the site list and
question, not cached across threads.

Marking checked in the database after run 1: 50 `audit_log` rows, all
`interface = 'eval'`, actor `selection-check`; `prompt`, `model_response`,
`run_finished`, `conversation_started`, `conversation_archived` — ten each,
and no `tool_call` row. Ten threads, all archived; none active.

Caveat, also in the question file: the seed questions are lifted from the
tool descriptions and `chat_cli.py`, so they are easy mode. Ten questions
also means one miss is ten points.

- [ ] Replace or extend the seed questions with real engineer wording
      (30+, at least two per pair of overlapping tools) and re-run.
- [ ] Re-run after every tool, description or prompt-map change; record the
      line here.

Also on 2026-09-18:

- `list_patch_status` lost its `failed_only` argument. The 2026-09-17 entry
  above (`failed_only=true` → 0) is the evidence: it returned zero in a week
  with 19 failed installs, so it was wrong, not redundant. The tool now says
  it covers pending and approved patches only and names
  `list_patch_install_failures` for failures.
- `list_recent_tickets` stays (reasons in api-notes/autotask.md).
  `list_tickets_for_device` and `list_configuration_items` stay: they are
  blocked on the Autotask permission above, not redundant, and their open
  items stand.

### Request economy and patch history, 2026-09-18 (pilot franchisee 1)

Reported from a live run: one site-scoped question listed the whole tenant
twice and authenticated three times. After the four fixes of this date:

| Run | Result |
|---|---|
| `chat_cli.py`, one question about one site, three tools called in parallel (`list_open_alerts`, `list_pending_reboot`, `list_patch_installs`) | **1** `POST /ws/oauth/token`, **1** site listing (`/v2/organization/<org-id>/devices`, two pages), **0** `/v2/devices`; no warning or error; 2 model calls, 18.3 s |
| `list_patch_installs --site rite-management-services --arg status=installed` | 443 installed in 7 days on 97 devices, 91 distinct patches; rows capped at 200, `status_counts` and per-patch totals cover all 443; 2.5 s; same request pattern as above |
| `list_patch_installs` default (through the agent, same site) | `status=FAILED` sent; OK |
| filter probe (counts only; table in api-notes/ninjaone.md, "Patch report filters") | `os-patch-installs` accepts `FAILED` and `INSTALLED`, ignores anything else silently; `os-patches` honours `status`, `type`, `severity`, has no KB filter |

**Proposal, not implemented: make `list_patch_status` answer the real
question.** A live call returned 200 of 491 rows, truncated. A longer list
would not help; the waiting list is mostly not what anyone is asking about.
Tenant-wide it is 3,310 rows, of which 1,132 are `REJECTED` (someone already
declined them) and 1,907 are driver updates; approved security updates are
715, and critical ones 19.

1. **Default to what is actionable.** `status` = pending (`APPROVED` +
   `MANUAL`), sent server-side. Rejected patches only on request. That alone
   removes a third of the rows.
2. **Add the two filters the vendor honours**, passed server-side and
   combined: `severity` (`critical` / `important` / `optional`) and `category`
   (`security` / `rollup` / `feature` / `driver` / `definition`, mapped to the
   vendor's `type`). "Any critical patches outstanding" becomes a 19-row read
   instead of a 3,310-row one. Send only known values: this report returns
   zero rows for an unknown value, which would read as "fully patched".
3. **Add `kb`**, applied on our side after the fetch (there is no server-side
   KB filter; rows do carry `kbNumber`). "Is KB5060000 still missing
   anywhere" then returns the machines that lack it, and nothing else.
4. **Return an aggregate beside the rows**, as `list_patch_installs` now
   does: per-patch totals (patch, KB, severity, devices waiting, sites
   affected), most devices first, plus `severity_counts` and per-site counts —
   all computed before the row cap. "What is our patch exposure" is answered
   by "KB X: 143 devices across 12 customers", not by 200 rows of it.
5. **Do not use `device-health` as the aggregate.** Its
   `pendingOSPatchesCount` was non-zero on one device while `os-patches` held
   2,176 approved rows.
6. Third-party (`software-patches`) keeps `status` and gains `impact`; its
   filters were not probed and should be before relying on them.

No new tool: these are arguments and result fields on `list_patch_status`, so
the count stays at 29. Cost: one fixture re-recorded per filter, a selection
question ("any critical patches outstanding?"), prompt map wording.

Selection check after the rename, prompt v9: **11 of 11 correct at 29 tools**
(both patch-history questions chose `list_patch_installs`, the waiting-list
question chose `list_patch_status`).

- [x] Proposal above: implemented the same day (next entry).

### `list_patch_status` with filters and totals, 2026-09-18 (pilot franchisee 1)

All sites, through `validate_connector.py`; no warning or error on any run.
Figures are for the client's active sites, so they are below the tenant-wide
probe counts in the api-notes (inactive and internal organisations are
dropped by the guard).

| Run | Result |
|---|---|
| default (`status=pending`) | 2,701 waiting rows on 699 devices, 508 distinct patches; rows capped at 200, totals cover all 2,701. By severity: optional 1,217, important 806, software "recommended" 654, critical 11. 4.1 s |
| `--arg severity=critical` | **9 rows on 6 devices, 3 patches** (4, 3 and 2 machines, each across 2 customers); Windows patches only, said so in `kinds_covered`; 3.2 s |
| `--arg category=security` | 663 rows on 431 devices, 48 patches: 654 important, 9 critical; capped at 200 |
| `--arg status=rejected --arg category=security` | 6 rows: rejected patches appear only when asked for |
| `--arg kb=<the top critical KB>` | 4 rows on 4 devices — the same 4 the per-patch total reported, so the client-side KB filter and the aggregate agree |
| `--arg severity=severe` | refused before any request: `severity: Input should be 'critical', 'important' or 'optional'` |

Through the agent the same refusal reaches the model as a correctable error.
A tool's own `ValueError` used to arrive as "failed unexpectedly
(ValueError)" with the message lost; the loop now hands it back, and still
treats a Pydantic response-validation error as a failure the model cannot fix.

Through the agent, prompt v10: "any critical patches outstanding?" called
`list_patch_status(status=pending, severity=critical)` once and answered in
12.6 s. Selection check: **13 of 13 correct at 29 tools**, with two new
questions either side of the waiting-list / history boundary.

- [ ] `software-patches` `impact` and `type` filters are unprobed and unsent.
      Probe them before offering a third-party severity filter.
- [ ] Add selection questions with real engineer wording for patch history
      versus the waiting list (`list_patch_installs` was renamed from
      `list_patch_install_failures`; one question was added).

### Streaming `/chat/stream` and prompt v11, 2026-09-18 (pilot franchisee 1)

One site-scoped question ("what is alerting, and what still needs a reboot"),
real model and vendor, local server.

| Check | Result |
|---|---|
| raw stream (`httpx`, event names and timings only) | `started` 0.02 s; `tool_start` x2 at 3.0 s (the model called both in parallel); `tool_end` x2 at 5.3 s; text in **251 pieces from 7.9 s to 19.3 s**; `done` at 19.3 s with `status=ok`, `prompt=v11` and an `audit_log_id` |
| web page in headless Chromium | tool rows appeared and settled while the run was going; the answer grew 228 → 2,117 characters over about ten seconds; on `done` the preview was replaced by the final answer with its 2-call tool block and rating control; no console errors |
| answer shape under v11 | opened with a paragraph, then two tables; none of "actually", "on closer inspection", "correction", "wait," in the text |
| audit trail | same rows as an unstreamed run (covered by `test_watching_a_run_changes_nothing_about_it`) |

Those runs were made as engineer `sse-check` through the ordinary `api`
interface, so they sit in `audit_log` as normal traffic for that actor.

- [ ] Put the page behind the reverse proxy it will be served from and check
      events are not buffered (`X-Accel-Buffering: no` is sent for nginx; other
      proxies need their own setting) and that the 15 s keep-alive survives
      its idle timeout.
- [ ] Judge v11 on real questions for a few days: one run shows the structure
      held once, not that revision-narration is gone.

### Business review, first live run, 2026-09-21 (shelton-keller-group, August 2026)

`scripts/tbr.py generate` then `render`. 43 facts, 5 charts, 5 findings, no data gap. The deck opened in PowerPoint without a repair prompt and every slide was checked as an exported image. Render gate: passed, 16 slides.

| Slide | Result |
|---|---|
| 1 Title, 2 Agenda | filled. Agenda lost Industry Trends and the sample "Other applicable reports" line |
| 3 Industry Trends | **awaiting engineer**: Autotask's market segment is "Services", too broad to research. Research was exercised separately for "Legal Services": 8 industry-only queries, 3 trends accepted, every URL one the searches returned |
| 4, 10, 16, 21 dividers; 20 AI Essentials; 23 Thank you | static, kept |
| 5 Management | filled: 101 workstations, 1 physical server, 0 virtual, 5 locations (NinjaOne; Autotask says 3), 443 "Users (active contacts in Autotask)" — flagged to the engineer as doubtful. No improvement sentence: no earlier review stored |
| 6 Dashboard | filled: five native charts replaced the screenshot. 218 tickets created, 218 completed, 12 open; resolution SLA 98.2%, first response 100%. No observation crossed a threshold (11 password tickets of 167 from people, under the ten per cent threshold) |
| 7 Key Takeaways | filled, model-written: low disks, overdue project |
| 8 Accomplishments | **awaiting engineer**: no project completed in August, nothing to compare with |
| 9 CX Metrics | **awaiting engineer**: Surveys entities answer HTTP 500 |
| 11 Current Projects | filled: one project, status New, 0%, planned end July 2025 and said to be past it |
| 12 Replacement | **dropped**: every workstation is on Windows 11. Age-based replacement **deferred** to Autotask configuration items or ScalePad |
| 13 Upgrade | filled: three disks under 10% free; hours and cost "Quote pending" (empty price book) |
| 14 Cybersecurity | **awaiting engineer**: the only finding is 2 of 102 devices with no healthy antivirus product, a single-tool gap held for confirmation. 0 failed installs of 163 in August, 0 critical patches pending |
| 15 Backup | **deferred** to N-able Cove |
| 17 Scheduled Projects | **awaiting engineer**: no future-dated project; Quotes answers HTTP 500 |
| 18 Services, 19 Priorities, 22 Budget | filled from findings; budget "Quote pending", Q1 2027 |

Review actions were exercised on a throwaway copy under the handle `claude-verification` (confirm a gap, add an item, enter CX scores, a refused placeholder), then superseded by a clean generation.

Found and fixed during the run: an overdue project was told it "should be completed before July 2025"; integration and provider-support contacts were counted as people in the top-contacts chart (51 tickets moved to the automation bar); the template's NPS score box overlaps the CSAT score; recommendation quarters were counted from the day after the period instead of the quarter it ends in.

Also live on 2026-09-21: `list_projects --site shelton-keller-group --arg state=all` 1 project; `get_company` industry "Services", classification "Managed Services"; `get_hardware` tenant-wide 1,032 devices, 985 physical and 47 virtual, Windows 11: 923 already, 22 ineligible, 15 eligible, 7 unknown. Selection check, prompt v12: **15 of 15 correct at 30 tools**.

- [ ] Set the pilot franchisee's price book, logo and review period (`manage_client.py report-config`); until then every figure is "Quote pending".
- [ ] Ask the franchisee to grant the API user Surveys and Quotes (with Contracts and Configuration Items, above): slides 9 and 17 are engineer-typed until then.
- [ ] Reconcile the pilot site's contacts and locations in Autotask (443 contacts, 3 locations against NinjaOne's 5).
- [ ] Generate September for the same site: it is the first run with a baseline, so the first with improvements, regressions and the slide 5 improvement sentence.
- [x] Industry research used the chat agent's `WEB_SEARCH_ALLOWED_DOMAINS`, which on this install is a list of vendor sites, so trends cited vendor blogs. Reports now have `REPORT_WEB_SEARCH_ALLOWED_DOMAINS` (2026-09-21, below).

### Business review render modes, 2026-09-21 (shelton-keller-group, August 2026)

Regenerated as report `bc4c75ff-38aa-42ab-bbc9-0505b7ea2ea3` (the 08:52 report is superseded, not deleted) and rendered three times from the one review, all by `claude-verification`. Each passed the gate with the same 16 slides kept and 7 dropped.

| Deck | Notes pages with text | Fact ids in notes | Mapped real names found | Stamps | Users line |
|---|---|---|---|---|---|
| client | 1 (the engineer's note on slide 11) | 0 | n/a | 0 | none |
| internal | 8 | 18 | n/a | 0 | none |
| demo | 1 (pseudonymised) | 0 | 0 of 160 | 16 of 16 | none |

Counted from the decks' raw XML (slides, charts, notes, document properties), not from the renderer's own report. The collectors recorded 102 device names and 52 contact names for the site; the mapping held 156 exact and 4 loose entries. The one remaining "TeamLogic" string in the demo deck is the template's own copyright line on slide 1, identical in the template. Checked by eye in PowerPoint: stamp top right on every slide, slide 5 shows devices and locations only.

- The 08:52 client deck from the first run carried slide 1's template speaker notes. The gate did not read notes then. It does now, and that deck should be replaced by the new client render before it goes anywhere.
- The first version of the notes check applied the whole slide list to notes, so an engineer's note containing "talk track" was refused. Narrowed the same day: notes fail only on a run of five consecutive words from the template's own notes or a fill-in mark (`XX`, `ABC Company`, `202x`, `{`, `<<`). Re-rendered the client deck with a note reading "My talk track: insert the project update first": gate passed.
- Tool count rule changed in CLAUDE.md: selection at or above 90% on real engineer questions is the rule; the count (30) is pinned in one test so a change is deliberate, not capped.

### Business review: privacy, value delivered, scorecard, batch, 2026-09-21 (shelton-keller-group, August 2026)

Regenerated as report `9fa77c6a-242c-4158-ac2b-5c8707ce06ac` and rendered as a client deck by `claude-verification`: gate passed, 17 slides, checked by eye in PowerPoint.

- **Slide 6.** The contact chart reads Contact A to Contact E plus the automation bar. Names were not turned on.
- **Slide 7.** Scorecard: Service good, Devices attention, Patching good, Security and Backup not yet assessed. No arrows: no July review is stored for this site.
- **Slide 8.** Was awaiting an engineer; now filled with two of the four value figures (patches installed, tickets resolved). The other two were left out, each with a recorded gap:
  - *Alerts resolved.* NinjaOne's activity log for the site held nothing from before 1 August (it reached back to 4 September). Probed the same day: `type=CONDITION&status=RESET` is honoured and paged correctly, so the count works for a period the log still covers. It has not yet been seen live on a review, because no such period exists today.
  - *Out of hours.* No business-hours time zone is set for this franchisee. Not set during verification: it is the franchisee's fact, and this Windows machine has no time zone database (`tzdata` is not installed, and is not a declared dependency).
- **`generate-all` was not run live.** It would draft every active site of the pilot franchisee (about fifty reviews, each with vendor reads and model calls). Covered by database tests with `generate` replaced; the first real run should be watched.

- [ ] Decide whether to add `tzdata` as a dependency so named time zones work on Windows.
- [ ] Set `business_hours.timezone` for pilot franchisee 1 and confirm the out-of-hours figure against Autotask.
- [ ] Generate a review within two weeks of a period's end and confirm the alerts-resolved figure against the NinjaOne console.
- [ ] Review the scorecard thresholds in `scorecard.toml` with whoever owns service quality: they are first values, not measured ones.

### Bitdefender GravityZone connector, 2026-09-22 (pilot franchisee 1)

**No API key is held for the pilot, so no GravityZone call was made.** The connector,
its five tools, the cross-tool matcher, the review's security rules and the daily
collector are built and tested against documentation-shaped fixtures with invented
values (`tests/fixtures/bitdefender/`, marked unverified). What was done live:

- **Read-only guard on the pre-flight.** `validate_connector.py --client teamlogic-it-austin
  --check-bitdefender` refuses at the factory with `no bitdefender credentials stored`,
  before any HTTP. With a key it first proves `createScanTask` is refused with no request,
  then reads `getApiKeyDetails` and the customer company count. Unit tests cover scan,
  move, delete, policy, quarantine, isolate, blocklist and misspelt methods: none makes a
  request.
- **BitLocker from NinjaOne, live on shelton-keller-group.** `/v2/queries/volumes?include=bl`
  adds a `bitLockerStatus` object to 109 of 113 volume rows (conversionStatus
  FULLY_ENCRYPTED 92, FULLY_DECRYPTED 17; protectionStatus PROTECTED 86, UNPROTECTED 21,
  UNKNOWN 2; encryptionMethod XTS_AES_128 90, XTS_AES_256 2, NONE 17). Judged per device
  over local disks with a capacity: 100 devices report, 88 fully encrypted on the probe
  (87 on the regenerated review the next morning). So NinjaOne does report BitLocker, and
  the encryption rule reads it beside Bitdefender's encryption module.
- **Selection check, prompt v13: 21 of 21 correct at 35 tools** (16 NinjaOne, 14 Autotask,
  5 Bitdefender). Six questions were added, one per Bitdefender tool plus a module count,
  each on a boundary with a NinjaOne tool that sounds alike (`count_devices`,
  `list_antivirus_status`, `list_open_alerts`, `get_device`, `list_patch_status`).
- **`sync_sites --connector bitdefender`: not run.** It needs the key. On the pilot,
  Bitdefender companies will be matched to the 161 existing sites by normalised name;
  how many match is unknown until then (the NinjaOne antivirus report names Bitdefender
  Endpoint Security Tools on 883 of 1,032 devices tenant-wide, so most sites should).
- **Shelton Keller, August 2026, regenerated** as report `6312a9bc-d0c0-4c78-aab2-a1fff0a52055`
  (`bc4c75ff…` and `9fa77c6a…` superseded), internal deck rendered, 17 slides, gate passed.
  - Slide 7: the scorecard's Security chip changed from "Not yet assessed" to **"No data"**:
    the area is assessed now, and this site has no GravityZone company mapped, so no metric
    has a value and nothing is guessed. Service good, Devices attention, Patching good as
    before.
  - Slide 14: unchanged, still awaiting the engineer: the held antivirus finding could not
    be confirmed or dismissed automatically without Bitdefender, and no cross-tool rule
    could run. The facts gained `ninjaone.bitlocker.devices_reporting` 100, `.devices_encrypted`
    87, `.devices_unencrypted` 13; no encryption finding is asserted without Bitdefender's
    side of the evidence.
  - The gaps list names the missing source: "Bitdefender endpoint protection: this site has
    no Bitdefender GravityZone company mapped".
- **Daily collector.** No collector existed before this change; `scripts/collect_events.py`,
  `collected_events` and `collection_coverage` (migration 0009) were built for it and tested
  against the database with a scripted reader: a second run updates rather than duplicates,
  a failed site gets a `failed` coverage row and the run goes on, and `coverage_for` tells a
  collected period from an uncollected one. Not run live: it needs the key.

### Bitdefender, live for the first time, 2026-09-22 (pilot franchisee 1, partner key)

The key was stored the same day (`--region us`; Cloud Instance 2). Every step of the
"before the freeze" list below was run in order, by `claude-verification`. Counts only;
no customer device or company name is recorded here. Commits `0022efe` and the one
that follows it.

**Pre-flight.** `validate_connector.py --check-bitdefender`: `createScanTask` refused with
no request; `getApiKeyDetails` answered; 63 customer companies. The key has **twelve APIs
enabled**; the pre-flight now warns, naming each one beyond those the connector uses
(companies, network, incidents, reports, and licensing since the module-state fix below
reads `getLicenseInfo`): accounts, integrations, maintenancewindows, packages,
patchmanagement, policies, quarantine. Keys should be issued least-privilege (api-notes,
"Credentials to request").

**Three contradictions, stopped on and reported before any fix** (per the instruction to
not work around a live response that contradicts the code):

1. **The modules map is not the documented one.** Live rows carry 16 keys, seven of them
   undocumented (`antiTampering`, `advancedAntiExploit`, `userControl`, `antiphishing`,
   `trafficScan`, `remoteEnginesScanning`, `riskManagement`, plus `applicationControl`),
   and seven documented ones never appear (`edrSensor`, `hyperDetect`, `relay`,
   `sandboxAnalyzer`, `exchange`, `containerProtection`, `integrityMonitoring`).
   `patchManagement` appears on 2 of 1,186 managed rows tenant-wide. A fixed tuple of
   modules would have reported "disabled" for modules the tenant does not license.
   Fix chosen: a module has three states beside enabled: `disabled` (licensed, off),
   `not_licensed` (from `getLicenseInfo`, stored per site) and `not_reported`; the module
   list is read from the rows; `count_endpoints(group_by='module')` reports only modules
   present on the tenant.
2. **-32602 is also "not found".** A bogus endpoint id returns -32602 "does not exist",
   not the documented -32002; a bogus `companyId` on incidents returns -32602 "You do not
   have full access to". Fix: -32602 maps to `NotFound` only when the message says so;
   any other -32602 is returned to the model as `ArgumentRejected`, a correctable error
   carrying the vendor's list of accepted values.
3. **No EDR anywhere.** `getIncidentsList` on a company without EDR returns an empty
   list, no error, so an unlicensed site is indistinguishable from a quiet week. No
   company of the 63 has `manageEventCorrelator`. Fix: `edr_licensed` per site from the
   licence; `list_recent_detections` says EDR is not licensed rather than returning an
   empty week; the collector records such sites as `not_applicable` (migration 0010).

**Step 1, fixtures.** One live call per allowlisted method that takes no customer-specific
argument, saved outside the repository and scrubbed by a script that keeps keys, types
and enums and replaces every name, id, IP (192.0.2.x), MAC and timestamp. Now live-shaped:
`api_key_details`, `companies_list`, `company_details_partner`, `endpoint_details`,
`license_info` (the pilot's: no EDR, encryption, patch management), `network_inventory_*`,
`incidents_list_empty`, `rpc_error_not_found`, `rpc_error_invalid_params`,
`rpc_error_unknown_option`. Still documentation-shaped and marked as such:
`incidents_list` (no incident exists on the tenant to record), `license_info_edr`
(the live file with the licence flags flipped true), `rpc_error_authorization`,
`rpc_error_rate_limited`. Leak check on the fixtures directory passed. Six allowlisted
methods whose parameter shapes could not be determined (`getEndpointTags`,
`getScanTasksList`, `getIntegrators`, `getCompanyDetailsByUser`, `getIncident`,
`getIncidentsByIds`) were removed from the allowlist and recorded in the api-notes as
candidates; the unit test proves they are refused.

**Step 2, `productOutdated`.** On the inventory row `true` means outdated: the one row
with `true` on the pilot site is the one whose details report `agent.productOutdated`
true and an older `productVersion`. The code was right; the v1.1 inventory page is
inverted. Recorded in the api-notes.

**Step 3, filters.** GravityZone behaves the opposite way to NinjaOne: an unknown filter
key, an unknown value, an unknown option, `perPage` over 1,000, a bogus `parentId`, an
unknown incident status and a `startDate` without `endDate` all return -32602 **naming
the accepted values**; incidents `perPage` has a minimum of 10. Two silent behaviours:
a company read **without `depth.allItemsRecursively` returns 0 rows** and no error, and
incidents are **empty without an EDR licence**. All four are in the api-notes filter
table beside NinjaOne's.

**Step 4, `sync_sites --connector bitdefender`.** 63 customer companies: 53 matched an
existing site by normalised name, 10 created (`api-server-new`, `insurance-council-of-
texas-2`, `jgb-custom-homes-2`, `leaf-landscape-supply`, `medisys-rehabilitation-2`,
`miracle-foundation-2`, `sobering-center-2`, `taahp`, `teamlogic-it-2`, `the-chapman-firm`).
The `-2` ones are near-duplicates of existing sites whose GravityZone name differs by a
suffix or spelling: candidates for `link_sites.py`, not merged automatically;
`teamlogic-it-2` is the franchisee's own company and should be an internal sync rule.
Of the 53 active managed sites, 52 have a Bitdefender company (`urban-foundry-
architecture` has none); one company matched an Autotask-only site (`austin-recovery`).

**Step 5, hostname matcher, shelton-keller-group.** NinjaOne 101 computers (100
workstations, 1 Linux server), Bitdefender 106 endpoints, all managed: **matched 99,
NinjaOne-only 2, Bitdefender-only 7**. Why seven endpoints have no NinjaOne device is
for the engineer to check in both consoles; the NinjaOne-only two are the computers
slide 14 now names.

**Step 6, the five tools through `validate_connector.py`.**

| Tool | Scope | Result |
|---|---|---|
| `count_endpoints group_by=status` | tenant | 1,198 endpoints, 1,147 with agent; protected 1,133, no agent 51, core off 8, outdated 6; 63 sites, not truncated |
| `count_endpoints group_by=module` | site | 16 modules reported; `encryption` not_licensed 106, `applicationControl` disabled 106, `antimalware` enabled 106; `edr_licensed` false |
| `list_unprotected_endpoints` | tenant | 65 of 1,198 (51 no agent, 8 ATC disabled, 7 antimalware disabled, 7 outdated) across 25 sites |
| `list_unprotected_endpoints` | site | 1 of 106 (agent outdated) |
| `list_outdated_agents` | tenant | 7 across 5 sites |
| `list_recent_detections --days 30` | site and tenant | 0, with the note "EDR is not licensed…"; 63 of 63 sites without EDR, none queried |
| `get_endpoint_protection` | site, one device | matched; BEST 8.26.9.668; 12 modules enabled, 3 disabled, 8 not licensed; risk 13% Low |

**Step 7, collector.** `collect_events.py --days 7`: window 2026-09-14 to 2026-09-22,
**63 sites not_applicable** (no EDR licence), 92 skipped (no Bitdefender mapping), 0
collected, 0 events. Coverage starts the day the first company is licensed for EDR; until
then `coverage_for` reports every day uncovered, never quiet.

**Step 8, Shelton Keller August 2026, regenerated** as report `82d0285d-9584-4ca8-80e9-
e2dc30f7d847` (`62590c3e…` and `c2fe3fa3…` from the same morning superseded), internal
deck rendered under `var/`, 18 slides, gate passed.

- **Scorecard (slide 7): Security went from "No data" to "attention"**: protected 99.1%
  (good), computers without an agent 2 of 101 (good), company risk score 48% (attention),
  detections not assessable without EDR. Service good, Devices attention, Patching good
  as before.
- **Slide 7's three takeaways** are now the antivirus finding, unencrypted devices (12)
  and unpatched devices (7).
- **Slide 14** went from awaiting the engineer to filled with two findings:
  `antivirus.unprotected` and `security.no_protection_agent` (2 of 101 computers with
  no Bitdefender endpoint). Its reason line no longer says coverage "comes with the
  connectors".
- **The held antivirus finding was confirmed**, not dismissed: NinjaOne doubts 3
  devices; 1 of them is a computer the hostname join found no Bitdefender endpoint for,
  which is the second tool agreeing. The other two have a working agent in Bitdefender,
  so the finding is asserted at NinjaOne's count with Bitdefender named as confirming
  source. (A first regeneration showed the model citing the site-wide NinjaOne-only
  count, 2, as if it were the doubted devices; the rule now records the doubted-without-
  endpoint count as its own fact, `antivirus.unprotected.no_endpoint`, and the sentence
  cites that.)
- **Recommended Services (slide 18)** gained Full Disk Encryption (12 devices: encryption
  not licensed and BitLocker off) and Patch Remediation (7 devices: patch management not
  licensed and NinjaOne not patching). The patch figure was **66** on the first
  regeneration because "NinjaOne patching this device" rested on August installs alone
  (40 of 101 devices); live, 99 of 101 installed something in 90 days and 90 carry rows
  on the waiting list. `collect_patch_coverage` now reads 90 days of installs plus the
  APPROVED, MANUAL and REJECTED waiting lists, recorded as
  `ninjaone.patching.devices_managed` (100 of 101).
- **Still held:** `security.edr_sensor_off.not_licensed` (106 endpoints), by design until
  Blackpoint is connected.

**Slips, for the record.** A peek script printed one company-folder name and root
container ids to the terminal, and the tool-runner echoed one real device name in its
command line. Nothing reached the repository; the rule is keys, types, enums and counts.

### Bitdefender, before the freeze

- [x] Key stored, pre-flight, sync, fixtures, filters, tools, collector and the review:
      all on 2026-09-22 (entry above).
- [ ] Ask the franchisee for a key with only Companies, Network, Incidents, Reports and
      Licensing enabled, and revoke the twelve-API one.
- [ ] `link_sites.py` for the eight `-2` and near-duplicate companies the sync created,
      and a sync rule marking the franchisee's own company inactive.
- [ ] Record an incident row from a company with EDR (none exists on the pilot) and
      replace the documentation-shaped `incidents_list.json`.
- [ ] Confirm slide 14's two NinjaOne-only computers and the seven Bitdefender-only
      endpoints against both consoles with the engineer.

### ConnectSecure connector, 2026-09-22 (pilot franchisee 1)

**No API key is held for the pilot, so no ConnectSecure call was made.** The connector,
its five tools, the review's vulnerability rules and the scorecard component are built
and tested against documentation-shaped fixtures with invented values
(`tests/fixtures/connectsecure/`, each marked unverified in its `_note`). Commit
`4885871` and the one that follows it. What was done:

- **Research.** The vendor's public documentation (authentication, rate limits, the one
  documented row shape, the problem group ids, the scoring standard, the roles) and its
  attached condition reference, plus two open-source integrations built from the
  vendor's OpenAPI specification (359 operations, 280 GET). Recorded in
  `docs/api-notes/connectsecure.md` with every field name that did not come from a
  documented example marked as a guess. The vendor's Swagger is behind the portal login
  and was not read. **The risk score's direction is settled from the documentation**:
  0 to 100, higher is worse, A is 0 to 40 and F is 90 to 100; to be confirmed from live
  data.
- **Read-only guard on the pre-flight.** `validate_connector.py --client
  teamlogic-it-austin --check-connectsecure` refuses at the factory with `no
  connectsecure credentials stored`, before any HTTP. With a key it first proves a scan,
  a create, a delete and an unlisted read are refused with no request, then logs in,
  reads the key's own user and warns when its role is more than a viewer, then counts
  the companies. Fourteen unit tests cover writes, deletes, a read path with the wrong
  verb, a write path with the read verb, two reads that hold secrets, and misspelt
  paths: none makes a request, not even the login.
- **Selection check, prompt v14: 27 of 27 correct at 40 tools** (16 NinjaOne, 14
  Autotask, 5 Bitdefender, 5 ConnectSecure). Six questions were added, one per
  ConnectSecure tool plus a product count, each on a boundary with a NinjaOne or
  Bitdefender tool that sounds alike (`list_patch_status`, `count_endpoints`,
  `list_recent_detections`, `get_endpoint_protection`, `get_device`,
  `list_devices_not_checked_in`, `list_software`). The first run was 26 of 27: "how
  many critical vulnerabilities does each customer have?" went to
  `list_critical_vulnerabilities`, whose `counts_by_site` made it look like a count
  tool; both descriptions now say a count per customer is `count_vulnerabilities`.
- **`sync_sites --connector connectsecure`: not run.** It needs the key. Companies will
  be matched to the 53 active managed sites by normalised name; assessment (prospect)
  and deleted companies are left out.
- **Shelton Keller, August 2026, regenerated** as report `cf81c6b9-4b26-485c-a722-750e49697c52`
  (`82d0285d…` superseded). Nothing changed on the scorecard (Security attention), slide 7
  (the same three takeaways) or slide 14 (the same two findings): the site has no
  ConnectSecure company mapped, so the collector records the gap "ConnectSecure
  vulnerability scan: this site has no ConnectSecure company mapped" and no vulnerability
  rule runs. Slide 14's reason line now says "ConnectSecure not read for this site".
- **What the review will do once a key exists** (tested against the fixtures): slide 14
  gains, in rank order, known-exploited vulnerabilities (critical, a measurement), "a few
  updates resolve most critical findings" (the top three fixes' share), open critical
  findings, "fix awaiting install" (a critical finding whose fix names a KB already
  approved or manual in NinjaOne for the same device: reported, not a new gap), and scan
  coverage (NinjaOne computers with no ConnectSecure asset or not scanned in 30 days: a
  two-tool gap). The Security scorecard gains four metrics (known-exploited findings,
  share of assets with a critical finding, share of managed computers not scanned in 30
  days, the company risk score). Vulnerabilities are state: the facts are on the stored
  review, which is the next review's snapshot, and they are in `COMPARABLE`; nothing goes
  to the daily collector.

### ConnectSecure, live for the first time, 2026-09-22 (pilot franchisee 1)

The key for `teamlogic-it-austin` was stored and the checklist below was run in order.
**Three live responses contradicted the code; each was reported before anything
changed, and the decisions are recorded with them.** `docs/api-notes/connectsecure.md`
was rewritten from the live run and every documentation-shaped fixture replaced.

- **Pre-flight.** `validate_connector.py --check-connectsecure`: a scan, a create, a
  delete and an unlisted read are all refused with no HTTP; login succeeds; the key's
  role is **ADMIN**, so the least-privilege warning fires, correctly, on every run;
  **52 companies** on the tenant; `requests today: 3 of 30,000 (0%)`.
- **One live call per allowlisted endpoint**, all nine, plus `/w/authorize`. Every
  fixture in `tests/fixtures/connectsecure/` is now a scrubbed live response (twelve
  files); no `_note` says "documentation-shaped" any more. One surviving live integer
  was caught by the leak check and neutralised.
- **Paging.** `skip` is a **record offset**, not a page index (`limit=2` then `skip=2`
  returned the next two). But the report views have **no stable order across reads** and
  `order_by` did not fix it, so an offset walk drops and duplicates rows. Every listing
  is now read as one page sized to the vendor's `total`; `limit=10000` was honoured.
- **Filters.** The three documented grammars are one live: `field=value`, strings
  quoted. A SQL-like condition is accepted and ignored. Unknown field or value:
  `/r/company/*` and `/r/asset/*` return **zero rows silently**;
  `/r/report_queries/*` **ignore it and return everything**. The two families fail in
  opposite directions, which is now ConnectSecure's row in the filter table beside
  NinjaOne's and GravityZone's, in all three api-notes. Guessed field names were
  corrected against live rows; `FILTER_FIELDS` is a fixed map per path, the builder
  always quotes a string, and every filter is re-applied client-side (a row that fails
  it raises). The instructed "filtered total equals unfiltered total" check was
  replaced by that row-by-row re-check, because the equality is legitimate on a
  single-company tenant; the reason is in the `_filtered` docstring.
- **Risk score direction confirmed from live rows**, not the documentation alone: an
  asset graded `F` scores 100 and carries a critical and a known-exploited finding; a
  `B` scores 50 with one. 0 to 100, **higher is worse**.
- **CISA KEV is problem group 34**, confirmed: the filtered read returned 6 rows over
  5 assets, matching the `cisa_vul` sum over the same 5 assets exactly.
- **Contradiction 1: findings rows carry no fix and no KEV flag.** Decided: remediations
  come from `get_remediation`; KEV counts from `asset_view.cisa_vul`; the group-34 read
  only when listing KEV CVEs; a fix is joined to a finding by **asset id and normalised
  product, never hostname**, and an unmatched join reads "fix not reported".
- **Contradiction 2: fixes are target versions, not KB numbers** (**0 of 300** fix texts
  carry one). Decided: "fix awaiting install" needs a normalised product match against a
  NinjaOne pending software patch at or above the target version; OS updates stay
  unmatched unless a KB appears. **The live match rate is 0%** — the two products name
  software differently enough that nothing matched — recorded as an internal-only fact,
  never rendered to a customer. `Uninstall` is its own finding type (software to
  remove); `Run remediation script` is an engineer action, not a client recommendation.
- **Contradiction 3: confirmed and unconfirmed findings were being mixed.** The vendor
  returns both by default and they differ by more than half: **3,869 confirmed of 8,278**
  on the pilot site. The deck had written "240 critical findings across 4 devices", a
  suspected count against a confirmed device count. Probed and recorded:
  `unconfirmed=false` **is accepted** on both the findings and the remediation view, and
  reconciles exactly with the per-asset counters (confirmed critical 118 on both views;
  confirmed KEV 6 rows over 5 assets against a `cisa_vul` sum of 6). Because the filter
  exists, slide 14 still lists individual CVEs. Every review number is confirmed-only;
  one caveat fact carries the rest; every chat tool takes `include_unconfirmed=False`
  and returns a `population` string; every `Fact` carries a population and the narrative
  validator rejects a sentence citing two. The regression test reproduces the original
  sentence by name.
- **Daily request ledger.** `app/connectors/connectsecure/ledger.py` counts requests per
  tenant per UTC day against the 30,000 limit, warns once at 50%, 80% and 100%, and the
  pre-flight prints `requests today: N of 30,000 (x%)`. It is per process, so the figure
  is a floor; `audit_log` is the durable record. A whole site review costs 9 requests.
- **`sync_sites --connector connectsecure`: 48 of the 53 active managed sites matched**
  a company by normalised name. Five have no company; `sync_sites.py --list` names them,
  and they are not written down here, per the probe rule in CLAUDE.md. Four
  ConnectSecure-only companies became new sites: two near-duplicates of existing sites
  and two of the franchisee's own internal organisations, which the per-client sync
  rules made inactive.
- **All five tools ran live** against shelton-keller-group and answered. The hostname
  matcher paired **94 matched, 7 NinjaOne-only, 0 scanner-only**.
- **Selection check, prompt v14: 26 of 27 correct at 40 tools** (16 NinjaOne, 14
  Autotask, 5 Bitdefender, 5 ConnectSecure), run again because every ConnectSecure tool
  description gained a paragraph about the two populations. The miss is the same
  boundary as before: "how many critical vulnerabilities does each customer have?" goes
  to `list_critical_vulnerabilities`, whose `counts_by_site` reads like a count tool.
  96% is above the 90% bar, so it is recorded rather than chased.
- **Shelton Keller, August 2026, regenerated** as report
  `186d06fc-a64c-4982-87fc-b21b322939e6`. Both render modes pass the gate (18 slides,
  109 paragraphs; 5 dropped). Slide 14 now carries five model-written findings, one
  held back by the per-slide cap, plus the code-written caveat: known-exploited
  (2 CVEs on 4 devices, 5 confirmed findings), antivirus (2 of 101), critical
  vulnerabilities (118 on 4 devices, no update queued), top fixes (3 updates resolve
  100% of the critical findings the vendor's fixes address), scan coverage (7 of 101),
  and "a further 4,409 findings are suspected but not yet verified by a scan". Slide 7's
  first takeaway is now the 5 known-exploited findings, ahead of antivirus and
  encryption. The Security scorecard moved **attention to action**, on the
  known-exploited metric (5 findings, action above 0.5) and the ConnectSecure company
  risk score (75 of 100, action above 60). Every vulnerability number on both slides and
  the scorecard is confirmed-only; the caveat is the one unconfirmed sentence and cites
  nothing else.
- **Two naming slips, recorded per the new CLAUDE.md probe rule.** A device name reached
  the terminal as a probe argument, and an earlier risk-score peek printed customer
  company names. Neither reached the repository, a commit message or a fixture.

### TBR vulnerability wording: scope on every fact, 2026-09-22 (shelton-keller-group, August 2026)

Two sentences on the first live vulnerability deck were true in their parts and
misleading as sentences. Both are now refused by the narrative validator rather than
by review.

- **"3 updates would resolve 100% of the critical findings that ConnectSecure's
  recommended fixes address, across 4 devices."** The denominator was the critical
  findings the recommended updates happen to cover, which is not a number anywhere on
  the deck; beside a headline of 118 open criticals the 100% read as all of them. The
  share is now `internal_only` and the sentence states counts against the published
  total: **"3 updates would resolve all 118 open critical findings across 4 devices."**
  A new fact, `connectsecure.remediations.top_critical_resolved`, carries the count and
  names `connectsecure.vulnerabilities.critical_open` as its denominator. Where the two
  differ the sentence reads "M of the X open critical findings"; where they are equal it
  says "all X", because "118 of the 118" reads like a mistake.
- **"A further 4,409 findings are suspected but not yet verified by a scan."** It sat
  under "118 critical findings are open", and 4,409 counts every severity, mostly medium
  and high. The caveat now counts the criticals among the suspected:
  **"A further 111 critical findings are suspected but not yet verified by a scan."**

What enforces it: a fact's scope is now its population *and* its severity band
(`all`, `critical`, `high`, `medium`, `low`, or none for a fact that is not a count of
findings), plus a `denominator` naming the fact a share is out of. The validator
refuses a sentence citing two populations or two severities, and refuses a percentage
that names no denominator or whose denominator is not printed in the same sentence. A
fact cited as another's denominator is read as context for its numerator, not as a
second scope, which is what allows a band to be stated against its total. Prompt
**tbr-v3** states both rules, so the model writes to them rather than being rejected by
them; the length rejection now says how many characters to cut, that being the
commonest reason a section falls back.

Four tests reproduce the two sentences by name, and the finding-level test now asserts
one severity per rule as well as one population. Every percentage fact in the report
gained a denominator (`ninjaone.patches.attempted` was added to be one).

Regenerated as report `3fddc3b3-de24-4ab1-96cb-5ce57da7e98b`. Both render modes pass the
gate (18 slides, 109 paragraphs). Checked item by item: **0 of the 14 rendered items on
slides 7 and 14 breach the scope rules**, none cites an internal-only fact, and no
percentage appears on the client deck at all. The scorecard is unchanged, Security still
**action** on the known-exploited count and the company risk score. One regeneration
before this one fell back on slide 14 for a slot over the character budget, which is why
that message now says how much to cut.

### Authentication and per-organisation authorization, 2026-09-23

Sign-in, roles and per-organisation scoping. Migration 0011 adds
`organisations`, `users`, `invites` and `sessions`, plus a nullable
`audit_log.actor_user_id` and `conversations.owner_user_id`.

No live vendor call was involved. What was verified, and how:

- **Every route declares a scope, and the check is not vacuous.** 34 routes,
  all declared. The first version of the enumeration walked `app.routes` and
  found three documentation endpoints, because FastAPI 0.141 keeps an included
  router's routes behind `_IncludedRouter.original_router` rather than
  flattening them. It reported no problems and was worth nothing. The walk now
  recurses, and the test asserts a minimum route count and names one route per
  router, so the same mistake fails rather than passes.
- **`create_app` refuses to build with an undeclared route.** The test proves
  it on a throwaway application with one route that declares nothing, and on
  one that declares two.
- **The client cannot be named by a caller.** `client_id` and `user_id` are
  gone from every request model, and the models forbid unknown fields, so a
  caller that still sends one gets a 422 naming the field rather than having it
  ignored. Asserted on `/chat`, `/chat/stream`, `/conversations` and
  `/feedback`. On a route the caller's role may not use at all it is a 403
  first: whether the body was well formed is not something an outsider learns.
- **A franchisee is confined to their own client.** Their client comes from
  their organisation; `/auth/clients` returns exactly one row, so the switcher
  is not a way to learn another franchisee exists; and switching is a 403
  whatever slug is named. Another client's thread is a 403 by id, their report
  listing is empty, and the audit trail holds none of the other client's rows.
- **A viewer cannot open chat**: 403 on `/chat`, `/chat/stream`,
  `/conversations` (both verbs), `/feedback` and `/audit`. They can still read
  `/auth/me` and the released reviews.
- **A staff client switch is logged.** Two switches wrote two
  `client_switched` rows carrying the account, both client ids and the target
  slug, with the first recording no previous client and the second recording
  the first. A switch to a slug that does not exist is a 404 and leaves the
  session where it was.
- **Sessions.** The cookie is `HttpOnly; Secure; SameSite=lax`; the database
  holds only a SHA-256 of the token; a forged cookie is a 401; signing out
  revokes the row rather than deleting it and writes a `logout` row. An idle
  session and an expired one are both 401, and using a session does not move
  its absolute expiry.
- **CSRF.** A state-changing request with no `X-CSRF-Token`, or another
  session's token, is a 403; a read needs none.
- **Throttling.** Five wrong passwords lock the account, after which the right
  password is a 423 that says so; exactly one audit row records the lock, and
  the password appears in none of them. Ten attempts from one address, against
  ten addresses that do not exist, produce a 429 with `Retry-After`. Every
  refusal — unknown address, wrong password, invited account, suspended
  account — returns the same message.
- **Second factor.** Enrolment returns the secret once; an unconfirmed
  enrolment does not affect sign-in; a confirmed one makes a password-only
  sign-in a 401 with `X-Second-Factor: required`. The stored secret is
  encrypted (the plaintext does not appear in the column) and reads back
  through the credential cipher. Disabling needs the password and ends every
  session.
- **Invites.** One use: the second attempt with the same token is a 400.
  Addresses are normalised, only the hash is stored, and accepting signs the
  new account in. A franchisee admin may invite into their own organisation
  only, and may not grant an Inputiv role.
- **Suspending ends sessions.** The victim's cookie is a 401 on the next
  request.
- **argon2id and RFC 6238 on their own.** 25 unit tests, including the RFC 6238
  appendix B vectors against the code generator, drift of one step accepted and
  three rejected, and a hash made with weaker parameters reporting that it
  should be upgraded.

Two defects were found by these tests and fixed:

1. `verify_password(None, ...)` raised `VerifyMismatchError` instead of
   returning a refusal. The equal-work path for an account with no password was
   the whole point of the branch, and it was throwing out of the endpoint.
2. An Inputiv session that had switched client showed the client's **slug**
   where its name belonged, because `_me` read the name from the
   organisation's own client — which staff do not have. The name is now carried
   on the `Scope`.

Full suite, `ruff` and `mypy --strict` green. `argon2-cffi` was added to the
dependencies; the stack list in CLAUDE.md names it and says why. One-time codes
use the standard library.

Still open, and deliberately so:

- [ ] The rate limiter is per process. Put a sign-in limit in the proxy before
      running more than one worker.
- [ ] Microsoft Entra. The columns are there (`identity_provider`,
      `external_subject`, per-organisation config) and nothing else is.
- [ ] The CLI scripts are unauthenticated and attribute to an operator handle.
      They need database credentials, which is the boundary they rely on.
- [ ] No password-reset-by-email flow: an administrator runs
      `manage_auth.py reset-password`, which ends the account's sessions.

### The pages as one product, 2026-09-23

Sign in, Ask, Reports, Sites and Settings behind one server-rendered shell, one
stylesheet and one script per page. No build step. Three static HTML files with
their own inline CSS were replaced by six Jinja templates and six JavaScript
files; `jinja2` was added to the dependencies and nothing else was.

Checked by driving a real browser against a seeded scratch database at
1440x900, signing in and looking at every page. Four defects were found that
way and would not have been found any other way:

1. **A table in an answer broke device names mid-word.** A twenty-character
   hostname (`<device-name>`) printed as two lines, split inside the word. The
   table obeyed `width: 100%`
   inside its scroller, so it squeezed its columns to fit instead of
   overflowing, and `overflow-wrap: anywhere` then broke inside the words. A
   table in a `.scroller` is now `width: max-content; min-width: 100%` and
   cells wrap with `break-word`. This is the "no truncated tables" requirement
   failing in a way that is worse than truncation: a reader cannot tell a
   broken name from a short one.
2. **A scrollable table gave no sign it scrolled.** Chrome's overlay scrollbar
   is invisible until a hover nobody performs in a meeting. The scroller now
   draws an edge shadow that fades as it is scrolled, and styles its scrollbar
   so it is simply there.
3. **An edited stylesheet did not reach the browser.** Assets were stamped with
   the application version, which does not change during a working session.
   They are stamped with the file's own mtime now.
4. **Two routes claimed `/sites`** — the page and its own JSON endpoint.
   Whichever router was included first answered and the other vanished with no
   error. The data route moved to `/sites/list`, and `duplicate_routes` now
   refuses a repeated method and path at build, beside the scope check.

Two more found while reading the ported code:

5. **`escapeHtml` did not escape quotes.** The `textContent` round-trip is safe
   between tags and a hole inside `value="…"`, and both uses are all over these
   pages. It escapes `& < > " '` now, so there is one function rather than a
   second one to remember.
6. **Page rendering opened its own database connection** for the logo check,
   outside the request's session. It takes the request's session now.

Polish from looking at it: a column of solid red Suspend buttons pulled the eye
off the rows, so a destructive control in a table row is outlined and small;
account status read `active` where every other chip on the product reads
`Active`; and page prose is capped at 78 characters a line rather than running
the width of a 1440px window.

`app/api/sites.py` is new: the Sites page needed the vendor mappings per site,
and `GET /reports/tbr/sites` answers the report picker's narrower question and
was left alone rather than widened into two jobs.

23 page tests cover the shell, the redirect, the nav by role and the switcher.
Full suite, `ruff` and `mypy --strict` green.

Still open:

- [ ] `marked` and `DOMPurify` are loaded from cdnjs, as they were before. A
      customer meeting on a bad connection gets an answer rendered as plain
      text. Vendor them, or render markdown server-side.
- [ ] The nav has no Audit entry; `GET /audit` has no page yet.
- [ ] Nothing here was checked against a screen reader or at a projector's
      actual contrast; the colours were chosen for it and not measured on one.

### Invitations end to end, 2026-09-23

`invite` printed a bare token and there was no page to paste it into, so an
invitation could be created and not used. It now prints the link, and the link
goes somewhere.

- **`PUBLIC_BASE_URL`** is a new setting: a deployment has to say where it
  answers, because a link is built by the CLI as often as by a request and a
  `Host` header is something a caller controls. It must be https anywhere but
  `ENVIRONMENT=local`, checked at start-up, so a deployment cannot quietly
  email links that carry a one-time token in clear.
- **`GET /invite/<token>`** is public and shell-less. It names who invited them
  (or nobody, for one the CLI made, rather than claiming somebody did), the
  organisation, the role in words, which address the account becomes, and when
  the link expires. Password twice, with a meter.
- **`describe_invite` is the one place that decides what a token is**, used by
  the page and by `accept`, so the page cannot offer a form for a link the
  endpoint would then refuse.
- **Five endings, five sentences, five statuses**: unknown (404), expired
  (410), already used (410), withdrawn (410), address already has an account
  (409). Each says what happened and what to do, and offers sign-in. None is a
  stack trace: a link a week old is an ordinary thing.
- **`--reissue`** withdraws an outstanding invitation and sends a new one; a
  second invitation without it is refused. Two live tokens for one address
  means whoever holds the older email uses it and the newer link then fails.
  **`revoke-invite`** withdraws one sent to the wrong address, which reissuing
  to the right address does not.

On the strength meter: **the only rule is still length**, which is what the
server enforces. The bar is advice, and it says what it measures — length
first, and a named warning for the three choices that are actually bad (the
address itself, a very common password, one repeated character). No complexity
rules were added; they push people towards `Passw0rd!` and a sticky note, which
is why this codebase does not have them.

Checked by running it: the CLI through invite, a refused duplicate, a reissue
and two revokes; then a browser through the whole path — link, page, password,
signed in, landed on Ask with the client fixed in the header — and the used and
unknown pages afterwards.

Two defects found that way, both in the meter and both invisible from the code:

1. **The label rendered in uppercase and ran off the card.** `.strength` is a
   direct child of `.field`, which styles its labels as small caps; that is
   right for "PASSWORD" and wrong for "Too easy to guess — it contains your
   email address". The first fix did not take, because a bare `.strength` is
   one class against `.field > span`'s class-and-element and loses on
   specificity — the same reason `.help` is written `.field .help`.
2. **The sentence was `white-space: nowrap`**, so it was the part that got cut.
   It is the useful half: a bar alone says only that a computer disapproves.

13 tests in `tests/api/test_invites.py`: the link, the page, accepting, the new
password being the one that works, a short password leaving the link usable,
and each of the five endings. Full suite, `ruff` and `mypy --strict` green.

Still open:

- [ ] Nothing sends the email. The link is printed for a person to send, which
      is honest about what exists; an outbound mailer is its own job.
- [ ] No page for a franchisee admin to revoke an invitation — `revoke-invite`
      is the CLI only. The Settings page lists them and stops there.

### TBR text fitting and service mapping, 2026-09-23 (shelton-keller-group, August 2026)

The fifth service on slide 18 ran under the footer. The renderer chose a font
size by counting paragraphs and never measured against the box, so slide 18
(one paragraph per service) stayed at 16pt for five services while slide 14
(two per finding) dropped to 14pt for three. Every model-written slide now
goes through one fitting step (`app/reports/tbr/layout.py`).

- **One config.** `SLIDE_BUDGETS` declares for slides 3, 5, 6, 7, 11, 14 and
  18 the item cap, characters per item and per slide, and a size ladder from
  the template's size to a readable floor. The numbers were calibrated by
  rendering each slide at its budget into the real template and measuring,
  with the longest lead the code can put on it (the 100-character SASE service
  name; the 70-character password-ticket headline).
- **Three uses of one measurement.** The brief gives the model `max_chars` per
  slot and `slide_max_chars` up front; the validator refuses a draft over
  either; the renderer measures anyway and takes the largest size that fits,
  then drops whole items from the least serious end with a code-written line,
  then shortens at sentence boundaries. A code-written line with no slot (the
  suspected-findings caveat) is measured and never dropped.
- **`report_max_findings_per_slide` is gone.** The cap is the budget's.
  Slides 18, 11 and 3 gain the same cap and the same held-back sentence on the
  review screen that slide 14 had.
- **Slide 7's rows** were 0.62in under the scorecard and held about 90
  characters; they are 0.85in now (three rows end at 4.93in, above the footer)
  and each row is fitted as a cell of its own, since a table row grows to its
  text and pushes the rows under it down the slide.

The first regeneration sent slides 7, 14 and 18 to their fallback wording:
every rejection was length, and every overrun was a token. The limit counted
the template, so `{{connectsecure.vulnerabilities.known_exploited_unique}}`
cost 56 characters for a "2" on the slide, and a sentence that cited two facts
was over before it said anything. The limit now counts the text as it prints
(`printed_length`) and the prompt says so. Second regeneration: 7, 14 and 18
all model-written on the first attempt. Slide 7 needed one more thing — its
headline slot has its own cap (60) and the slide total counts it.

**Mapping.** Low disk space backed Hardware Lifecycle Planning because it was
the nearest name. It backs nothing now, as do short memory and the in-place
Windows 11 upgrade: jobs with a price-book line on slide 13, and no storage or
maintenance service in the catalogue to point at. Lifecycle is for age or end
of support, which until device age is collected means only Windows 10 devices
that cannot run Windows 11. Every other mapping was read against its finding
and left as it was. `test_a_finding_backs_the_service_that_addresses_it_or_none`
pins the three and checks every key is in the catalogue.

Rendered and exported through PowerPoint, every slide looked at:

| Slide | Before | After |
|---|---|---|
| 18 Recommended Services | 5 services at 16pt, the fifth under the footer; Hardware Lifecycle Planning backed by the disk finding | 4 services at 16pt (the fitter found the template's size fits), inside the placeholder; lifecycle gone |
| 14 Cybersecurity | 3 pairs at 14pt, third pair running long | 3 pairs at 16pt with the caveat line, a quarter of the box to spare |
| 7 Key Takeaways | fallback wording (length) | model-written, three rows, each within its cell |

Tests: `tests/reports/test_tbr_layout.py` renders each budgeted slide at its
maximum and reads the file back (nothing exceeds its placeholder, nothing
dropped) and at one item (it takes the template's size). Full suite, `ruff`,
`mypy --strict` green.

Still open:

- [ ] The measurement is an estimate of PowerPoint's layout (Arial metrics for
      Helvetica, 1.2 line height, a 6% safety margin). It has been right on
      every slide exported so far; a slide that overflows in PowerPoint and
      not in the measurement is the thing to bring back here.
- [ ] Slide 14's three pairs sit at 16pt with room below. The floor (12pt)
      is only reached when three long findings all carry long headlines; if
      that never happens live, the budget could grow.

### Fleet-wide coverage, live for the first time, 2026-09-23 (pilot franchisee 1)

`app/coverage/`: every NinjaOne managed computer against every tool that
should have an agent on it, by hostname within a site. One join
(`core.assess`) read by the Coverage page, the two chat tools and the
review's coverage findings. Run live through the daily collector
(`collect_events.py --coverage-only`), then probed for counts.

| | |
|---|---|
| Managed computers (the population) | 1,029 across 66 sites; 16 NinjaOne devices left out as not computers |
| Endpoint protection (Bitdefender) | **72.8 %** covered: 749 covered, 258 absent, 9 stale (4 agent out of date, 5 core protection off), 13 unmatched |
| Vulnerability scanning (ConnectSecure) | **77.7 %** covered: 773 covered, 204 absent, 5 stale (scan out of date), 13 unmatched, 34 not assessed (4 sites with no ConnectSecure company) |
| Backup | not assessed: Cove is not connected |
| Unmanaged (endpoints with no managed computer of that name at the site) | **556**: 405 in Bitdefender, 151 in ConnectSecure |
| Unmatched | 26 device-tool pairs, all one cause: two managed devices at the same site share a hostname |
| Computation | **15.2 s cold** (three tenant listings: 1,029 devices, 1,193 endpoints, 1,028 assets), **52 ms warm** (all three from the inventory caches); the snapshot's own run, 17.7 s |

Read against the numbers rather than taken as they are:

- **The worst site is a customer with a GravityZone company that holds
  nothing.** 125 managed computers, 0 % Bitdefender, and not one endpoint
  in the company — no unmanaged ones either, and none of its hostnames
  turn up under another company. The board says `absent` for all 125,
  which is the truth of the two inventories; whether the customer is on
  another product or genuinely unprotected is a question for the
  franchisee, and the exclusion action is how they answer it on the
  board without hiding anything. Sites two to five are the same shape at
  a smaller size (0 %, 0 %, 3.7 %, 6.2 %).
- **405 Bitdefender endpoints nobody manages in NinjaOne** is the bigger
  reverse gap: machines GravityZone protects that the RMM does not have.
  Some are the network-discovered rows with no agent (`absent` for their
  own hostnames); the rest are real machines outside the RMM.
- **The 26 unmatched pairs are 13 hostnames used twice at a site.** The
  join refuses to guess which of the two an endpoint is, so both show
  `unmatched` rather than one wrongly `covered`. The fix is in the RMM's
  naming, not here.
- **The review changed one number.** `coverage.ninjaone_only` ("no
  protection agent") now counts a GravityZone row with no agent as a gap,
  as the board does; on the recorded fixtures that moved one device
  (`<device-name>`, a GravityZone row with no agent) from matched to absent
  (`tests/reports/test_tbr_security.py`), and the test says why.

Exclusions and snapshots were exercised against the test database only
(`tests/api/test_coverage.py`): a reason is refused missing at the API, the
service and the database constraint; the audit row names device, tool and
reason; a viewer gets 403 on the write and 200 on the board; another
client's site is a 404; the day's snapshot is one row, replaced on a rerun,
and "new gaps" is exactly the device the scanner lost overnight. No live
exclusion has been recorded: that is the franchisee's call.

Still open:

- [ ] Bitdefender's endpoint list carries no last-seen, so a machine that
      went dark with a current agent reads `covered` until GravityZone
      flags it. `getManagedEndpointDetails` has the date at one call per
      endpoint; whether 1,193 calls a day is worth it is a question for
      after the freeze.
- [ ] The trend needs days of snapshots to be a trend. Schedule
      `collect_events.py` once a day for the pilot and look again in a week.
- [ ] Cove: one reader on the `backup` entry in `config.COVERAGE_TOOLS`.

### Coverage matching diagnostic, 2026-09-24 (pilot franchisee 1)

`scripts/coverage_diagnose.py` (`app/coverage/diagnose.py`): for every gap
the coverage join left, what would have paired it. Read-only; the join and
the board's numbers are unchanged. The earlier audit's rules
(`scripts/legacy/ninja_asset_audit.py`: 15-character key, `fqdn` over
`name`, tenant-wide sets) were each tried as a named normalisation. NinjaOne
MACs come from the `network-interfaces` query (new in the api-notes), serials
from `computer-systems`; ConnectSecure's `mac`, `serial_number` and `name`
are now read from the asset row.

Population: 1,029 managed computers, of which **118 are `rmm_stale`** (no
NinjaOne contact in 14 days) and are reported on their own, never as absent.

**Absent devices, by why they did not pair**

| | Bitdefender | ConnectSecure |
|---|---|---|
| site mismatch (same hostname, other site) | **97** | 2 |
| normalisation | 0 | **60** — all `netbios15` |
| secondary key (MAC) | 1 | 2 |
| rmm_stale | 38 | 98 |
| none | 121 | 53 |

**Unmanaged endpoints, three ways**

| | Bitdefender | ConnectSecure |
|---|---|---|
| at a site with no NinjaOne mapping (not assessable) | **237** | 6 |
| discovered, not endpoints (`isManaged: false`; probes, retired agents) | 23 | 44 (42 probes, 2 no agent) |
| real | 141 | 99 |
| real, of which: site mismatch / normalisation / MAC / none | 10 / 1 / 4 / 126 | 27 / 59 / 3 / 10 |

**Match rates, two definitions**

| | Bitdefender | ConnectSecure |
|---|---|---|
| within-site (the board): same hostname at the same site, over computers at sites the tool answered for | 75.0 % | 78.4 % |
| tenant-wide (legacy): 15-character key anywhere in the tool, over every computer | 85.7 % | 84.8 % |
| tenant-wide, active only (legacy `_active`) | 86.6 % | 93.5 % |

**Duplicate hostnames**: 6 groups (13 devices). Serial tells them apart in
5; last contact in 0 (both halves live, or both stale); neither in 1.

What the counts say:

1. **The Bitdefender gap is mostly a site problem, not a matching problem.**
   The 97 site-mismatch devices and the 237 not-assessable endpoints are the
   same eight customers: `sync_sites` gave each Bitdefender company its own
   site row beside the NinjaOne site of the same name: the `-2` slugs
   (`sobering-center-2`, `jgb-custom-homes-2`, `insurance-council-of-texas-2`,
   `miracle-foundation-2`, `medisys-rehabilitation-2`, `teamlogic-it-2`, the
   franchisee's own) plus `the-chapman-firm` and `leaf-landscape-supply`,
   which have no NinjaOne twin at all. `link_sites.py` merges the pairs;
   after that the board's within-site rate rises to about the tenant-wide
   one without any change to the join. Nine more devices point at a real
   site with 3 computers and 25 real unmanaged endpoints (`a1-parts-mart`):
   probably the same shape, worth a look before merging.
2. **Rite Management Services has no Bitdefender at all**: 107 of the 121
   Bitdefender `none` are there, and there is no endpoint, unmanaged or
   otherwise, with any of their names or MACs. That is a customer-level fact
   for the franchisee, not a join issue.
3. **ConnectSecure's 60 + 59 are one rule**: NinjaOne's `systemName` is the
   15-character NetBIOS name and ConnectSecure's `host_name` is the full one
   (16–24 characters); in every one of the 58 pairs checked the 15-character
   keys are equal. The legacy audit's `[:15]` was right about this.
4. **MAC pairs 10 in total, 8 of them renamed machines** whose names share
   nothing. Small, but each is a device the board calls absent that has the
   agent. Serial paired nothing across tools: ConnectSecure's `serial_number`
   was empty or a placeholder wherever it mattered.
5. **Dashes, underscores and whitespace: 1 pair each.** Not worth a rule.

What to adopt (proposed, not done; each changes board numbers and gets its
own commit with the counts before and after):

- [ ] **Merge the eight duplicate site pairs with `link_sites.py`** first. It
      is data, not code, and it moves 97 devices from absent to covered and
      237 endpoints from unmanaged to paired.
- [ ] **Add the 15-character rule to the join as a second pass**: exact key
      first, then, only for devices still absent and only where the
      15-character keys pair *one* device with *one* endpoint at the site,
      pair them. One-to-one keeps "never guess"; it recovers 58 on
      ConnectSecure and 1 on Bitdefender.
- [ ] **Add MAC as a third pass, same one-to-one rule, same site**: recovers
      10 (5 per tool), all real. Costs the `network-interfaces` query
      (7,199 rows, one cursor walk) per computation; cache it with the
      inventory.
- [ ] **Adopt `rmm_stale` on the board**: a device with no NinjaOne contact
      in 14 days is `rmm_stale` for every tool, not absent; 118 devices, 38 of
      Bitdefender's and 98 of ConnectSecure's absent count today.
- [ ] **Filter discovered rows out of unmanaged**: Bitdefender `isManaged:
      false` (23) and ConnectSecure probes and retired agents (44) are not
      endpoints anybody could manage in NinjaOne; report them separately.
- [ ] **Do not adopt**: the alternate name fields as a rule (once
      `netbios15` is in, `alt_field` adds nothing here), separators or
      whitespace (1 each), serial across tools (0), or tenant-wide matching
      (the 10 % it adds over within-site is the site mismatch, which the
      merge fixes properly).
- [ ] For the 13 duplicate-hostname devices, serial tells 5 of 6 groups
      apart: when the duplicate rule is revisited, prefer the device whose
      serial the tool also carries; last contact does not help.

### Two vendor organisations per site, three join passes, eight merges, 2026-09-24 (pilot franchisee 1)

The diagnostic's adoptions, built and then run live. Migration 0013 lets a
site hold more than one mapping per connector; the join runs three one-to-one
passes; the sync remembers merges, links by device overlap, strips
GravityZone's integration suffix and reports mappings the vendor stops
returning instead of touching them.

**Board, per tool** (1,029 managed computers, 118 of them `rmm_stale` at
>14 days and out of every denominator):

| | old join, old mappings (2026-09-23) | new join, old mappings | new join, after the merges |
|---|---|---|---|
| Endpoint protection (Bitdefender) | 72.8 % | 74.2 % | **83.5 %** (761 covered, 132 absent, 7 stale, 11 unmatched) |
| Vulnerability scanning (ConnectSecure) | 77.7 % | **91.6 %** | 91.6 % (809 covered, 61 absent, 2 stale, 11 unmatched, 29 not assessed) |
| Pairings by pass | — | — | Bitdefender: exact 768, MAC 2; ConnectSecure: exact 757, NetBIOS 54, MAC 1 |
| Unmanaged (real, at sites NinjaOne manages) | 556 | 288 | 338 (Bitdefender 271, ConnectSecure 67); 76 discovered rows and 46 endpoints at unmapped sites reported apart |
| Computation | 15 s cold / 52 ms warm | — | 20 s cold / 61 ms warm (the `network-interfaces` read is cached per client with the inventory's TTL) |

The ConnectSecure gain is the NetBIOS pass and `rmm_stale` (98 of its old
absent devices were RMM-stale); the Bitdefender gain is the merges. The
unmanaged count rose after the merges because the merged companies' endpoints
now sit at sites NinjaOne manages, where they can be compared; before, 237 of
them were at sites with no NinjaOne mapping and not assessable at all.

**Merges** (`link_sites.py`, each with its live count first, ids only):

| kept | merged | what moved |
|---|---|---|
| sobering-center | sobering-center-2 | Bitdefender: 53 endpoints / 50 agents joined a company of 5 / 2 |
| jgb-custom-homes | jgb-custom-homes-2 | Bitdefender: 35 / 35 joined 3 / 3 |
| insurance-council-of-texas | insurance-council-of-texas-2 | Bitdefender: 31 / 29 joined 4 / 2 |
| miracle-foundation | miracle-foundation-2 | Bitdefender: 13 / 13 joined 3 / 1 |
| medisys-rehabilitation | medisys-rehabilitation-2 | Bitdefender: 16 / 16 joined 3 / 1 |
| dobl-consulting | dobl-group-2 | ConnectSecure: 4 assets |
| desert-providers | desert-providers-medical-supplies-2 | ConnectSecure: 6 assets |
| teamlogic-it (inactive) | teamlogic-it-2 | Bitdefender: 51 / 48, the franchisee's own; off the board with its site |

`the-chapman-firm` and `leaf-landscape-supply` were not merged: neither has a
NinjaOne twin and none of their hostnames appears in NinjaOne anywhere. They
stay as their own sites (not assessable) for the franchisee to place.

**Remaining `absent`, by site over ten devices**: Bitdefender 132 across 14
sites, of which **Rite Management Services 106** — the customer with a
GravityZone company holding nothing; ConnectSecure 61 across 27 sites, none
over ten.

**Vendor duplication** (on the board and in `sync_sites` output, with an
export): five sites hold two GravityZone companies. In each, the
integration-created company (six-character lowercase suffix) is the live one
and the hand-made company (upper-case five-character tag or none) the small
remainder — Sobering Center 53 / 50 beside 5 / 2; JGB Custom Homes 35 / 35
beside 3 / 3; Insurance Council of Texas 31 / 29 beside 4 / 2; MediSys
Rehabilitation 16 / 16 beside 3 / 1; Miracle Foundation 13 / 13 beside 3 / 1.

**Sync, dry-run after the merges**: 63 organisations, 63 unchanged, the five
duplications listed, nothing to link and nothing missing. The first dry-run
would have *reactivated* `teamlogic-it` — the franchisee's own company, now
merged onto its inactive site — because the TeamLogic internal-name rule
applied to Autotask only; it applies to Bitdefender and ConnectSecure too
now (`naming.DEFAULT_SYNC_RULES`), with a test.

Diagnostic after: Bitdefender site-mismatch 97 → 9 (all pointing at
`a1-parts-mart`, still worth a look), `none` 121 → 123; the within-site
rate 84.5 % now sits beside the legacy tenant-wide 85.4 %, which is what the
merge was predicted to do. ConnectSecure unchanged, as expected.

Still open:

- [ ] `a1-parts-mart`: 3 managed computers, 25 real unmanaged Bitdefender
      endpoints, and 9 other sites' devices whose hostnames are in its
      company. Probably a company that holds several customers' machines;
      to be looked at with the franchisee before anything is merged.
- [ ] Rite Management Services: 106 computers and an empty GravityZone
      company. A customer-level question, not a join one.
- [ ] The legacy script (`scripts/legacy/ninja_asset_audit.py`) was
      reformatted by a `ruff format scripts` run in this session (whitespace
      only; semantics untouched) before `scripts/legacy` was excluded from
      ruff in `pyproject.toml`. It is not committed.

### Device identity, first live resolution, 2026-09-24 (pilot franchisee 1)

Migration 0014 applied (`devices`, `device_vendor_links`,
`device_unresolved`). `DEVICE_IDENTITY_ENABLED` stays **off**; the job ran
and the agreement was measured with the flag forced on for the reads only.

**Final first run from empty tables** (`scripts/devices.py resolve`, 22.6 s):

| | |
|---|---|
| Devices created | 1,419 (1,045 from NinjaOne, 374 from vendor rows NinjaOne does not manage) |
| Links by pass | ninjaone 1,045; exact 1,528; netbios15 54; mac 3; vendor_only 425 |
| Unresolved queue | 13, all from the exact pass: 9 Bitdefender and 4 ConnectSecure records whose hostname two managed devices at the site share (three of the Bitdefender rows name one hostname three NinjaOne devices carry), across four sites |
| rmm_stale | 125 (>14 days, or never contacted) |
| retired | 0 (nothing has been unseen for 90 days yet) |
| Rerun | 0 devices, 0 links created, 3,055 seen, 0 moved, 0 deactivated, 13 unresolved, 0 new |

**Agreement** (`scripts/devices_agreement.py --client … --site
shelton-keller-group`, exit 0): the board's per-site counts are identical
flag off and on across all 58 sites (1,029 managed, 338 unmanaged, 76
discovered; Bitdefender 83.5 % / 83.5 %, ConnectSecure 91.6 % / 91.6 %),
and Shelton Keller's coverage facts are identical: matched 96, NinjaOne-only
2, Bitdefender-only 8; scan matched 93, NinjaOne-only 5, uncovered 5.

**What the first two attempts got wrong**, each caught by the agreement
script or the rerun and fixed before this run (the tables were emptied and
the job rerun from nothing each time; the `devices_resolved` audit rows of
every attempt remain):

1. The passes' population was every non-retired device, including devices
   made from another vendor's rows earlier in the same run. A rerun paired
   three Bitdefender rows by NetBIOS to a ConnectSecure-only device that had
   not existed when Bitdefender ran the first time (not idempotent: 3 links
   created on the rerun), and the board never pairs against such a device.
   The population is now the devices NinjaOne returned this run.
2. Every device went into the passes with `last_contact = now`, so an
   RMM-stale device could be paired; the board excludes it before the
   passes. One ConnectSecure asset whose MAC an active and a stale device
   share was queued here and paired there. The population now carries
   NinjaOne's last contact and the board's threshold: 118 fewer exact
   pairings than the draft, all of them rows of stale devices, which are
   vendor-only until the machine comes back.
3. Records already linked were left out of the passes, so their devices were
   free for a weaker pass: four Bitdefender rows were attached by MAC to
   devices that already held an exact Bitdefender pairing, which one-to-one
   forbids on the board. Every record now goes in; a standing link is
   confirmed, a vendor-only one moves when a pass places it.
4. The identity reader reported a device whose name a twin shares, or that
   has no hostname, as `absent`; the board says `unmatched`. Five devices at
   one site and two at another. The reader and the review's group reader
   apply the join's two refusals now, with a manual link the only override.

Still open:

- [ ] The 13 queued records need a person: `scripts/devices.py unresolved`
      lists them with candidate device ids; `link --reason` settles one.
- [ ] A pairing a pass made that the join later finds ambiguous is kept, by
      design, and will show in the agreement script as a difference on that
      day. Decide then whether the identity layer's memory or the join's
      caution is right for that case.
- [ ] `retired` is untested live until something has been unseen for 90 days.

### The Devices page and the daily agreement, 2026-09-24 (pilot franchisee 1)

Migration 0015 (`device_agreements`) applied. The collector's coverage-only
run now ends with the agreement step:

```
device resolution: 0 device(s) created, links created by pass: none; 3055 link(s) seen,
  0 moved, 0 deactivated; 13 unresolved (0 new); 125 rmm_stale, 0 retired; 23935 ms
coverage snapshot 2026-09-24: 1029 managed, 338 unmanaged; bitdefender 83.5%,
  connectsecure 91.6%, backup n/a; 131 ms
device agreement: 58 sites identical, flag off and on
```

The first stored row: agree, 58 sites, 0 differences; managed 1,029 / 1,029,
unmanaged 338 / 338, discovered 76 / 76, Bitdefender 83.5 % / 83.5 %,
ConnectSecure 91.6 % / 91.6 %.

The list against the live tables: 1,419 devices across the pilot's sites
(1,294 active, 125 RMM stale, 0 retired); missing from NinjaOne 374, from
Bitdefender 308, from ConnectSecure 542; 15 vendor entries hold more than one
record of one machine; 47 ms to build. The device detail was exercised
against fakes only (the four tools it calls are each proven live already,
above); the page tests cover roles, scope and the audit row of a link. Not
yet done by a person: opening the page in a browser and clicking through a
device — the visual check, before the page is shown to anyone.

Found while writing the page tests and fixed: an `unlink` of a record that
had ever been queued before (linked from the queue, then unlinked) violated
the queue's unique key, because it inserted a second row instead of
reopening the settled one. The CLI had the same bug; neither had been run
that way live.

### The ticket workspace, first live plans, 2026-09-25 (pilot franchisee 1)

Migration 0016 applied. `scripts/tickets.py list` read 132 open Autotask
tickets across the pilot's sites (SLA state: 85 breached, 28 due within a
day, 19 ok). Three recent tickets at sites with every tool mapped were opened
and planned from the terminal (ticket numbers are ids; no device, contact or
step text was printed):

| ticket | device resolution | scrubbed | facts | plan | steps kept / written | citations | commands | generation |
|---|---|---|---|---|---|---|---|---|
| T20260924.0003 | none chosen: two `contact / low` candidates (the contact's logon on two machines) left to the engineer | 0 | 1 | v1 | 8 / 8 | 8 (6 tickets, 2 facts) | 0 | 131.5 s |
| T20260924.0016 | none: five hostname-like tokens, none a device at the site; contact without a logon match | 2 | 1 | v1 | 3 / 8 (5 dropped) | 3 (facts) | 0 | 44.8 s |
| T20260924.0016 | same | 2 | 1 | v2 | 8 / 8 | 11 (10 pages, 1 fact) | 0 | 52.9 s |
| T20260924.0034 | `hostname / high`: one machine named in the ticket | 0 | 4 | v1 | 0 / 0 | 0 | 0 | 119.8 s |
| T20260924.0034 | same | 0 | 4 | v2 | 10 / 10 | 17 (10 tickets, 5 facts, 2 pages) | 0 | 115.8 s |

Every plan made two web searches (18 to 20 pages returned); the evidence was
six resolved tickets where the title found any, and the facts. No command was
shown on any plan: none of the cited resolved tickets carried one word for
word, which is the rule working, not a gap. Generation time is dominated by
the model turn with search (40k to 55k input tokens) and the six `get_ticket`
reads for the evidence.

Two v1 plans taught the code something, each fixed before v2:

- **T20260924.0016 v1 dropped 5 of 8 steps** for citing pages "the searches
  did not return": the model retyped the URLs the search gave it (scheme,
  `www`, a trailing slash, a query string). Citations are now matched on host
  and path; the returned URL is what is stored. v2 kept all 8.
- **T20260924.0034 v1 kept nothing**: the model answered in text rather than
  calling the plan tool, after one of its two queries named the machine (the
  leak check voided the web citations, correctly). A plan that comes back as
  text or empty now gets one last turn with the plan tool required and search
  off. v2 kept 10 steps with 17 citations; the leak did not recur.

Two secrets were scrubbed from the second ticket's text before it was stored
or shown to the model — a live instance of the rule the workspace exists to
enforce.

Still open:

- [x] A site holding two GravityZone companies failed a site-scoped
      `getNetworkInventoryItems` on one of them (`Invalid value for
      'parentId'`). Fixed 2026-09-25: see "Site-scoped reads over every
      organisation of a site" below.
- [ ] Autotask configuration items return HTTP 500 for this tenant
      (`list_tickets_for_device`), so the device's ticket history comes from
      the text search fallback, which the panel names as its source.
- [ ] Nobody has opened the Tickets page in a browser yet: the visual check
      of the workspace, the review buttons and the outcome prompt.
- [ ] The outcome loop has not run live end to end: none of the three
      tickets has closed since. `POST /tickets/poll` and the prompt are
      covered by tests against fakes.

### Site-scoped reads over every organisation of a site, 2026-09-25 (pilot franchisee 1)

The cause of the Bitdefender failure above, reproduced per company: the
merged site sobering-center holds two GravityZone companies; the tenant
listing has 53 endpoints under the integration-made one and 0 under the
hand-made one, and `getNetworkInventoryItems` with the hand-made company as
`parentId` is refused (`Invalid value for 'parentId'`). The franchisee had
removed it at the vendor since the merge on 2026-09-24. The fan-out already
asked both companies; one refusal failed the whole site.

A survey of every site-scoped read against a two-mapping site found the
scope, the factory, the guards, the inventory cache, Autotask's `in` filter
and the coverage board all correct, and these not:

| read | was | now |
|---|---|---|
| every per-organisation fan-out (NinjaOne devices, alerts, queries, locations, activity reach; Bitdefender inventory, licences, incidents; ConnectSecure every scoped listing) | the first organisation to raise failed the site | `BaseConnector._fan_out_refs`: an organisation the vendor no longer knows (`NotFound`, or `ArgumentRejected` on the id) is spared when another organisation of the same site answered, and logged; a site whose only organisation is gone, or any other error, raises as before |
| Autotask company card, and the review's company facts | `targets[0]`: the first company only, contracts counted for it alone | every company of the site (`get_site_companies`), contracts counted across them in one `in` query, the other companies named on the card, industries joined when they differ |
| Bitdefender detections | asked every company of a site whose *union* licence had EDR, including one without it | only the companies whose own licence has EDR (`companies_with_edr`) |
| `sites_covered` on every tool, all four connectors | a site once per organisation | `scope.target_slugs`: once |
| ConnectSecure `count_vulnerabilities` | treated a one-site, two-company question as many sites and took the per-company totals path | counts distinct sites |

Left as it is, on purpose: `get_endpoint_protection` judges a module against
the endpoint's own company's licence, because that is the licence governing
that endpoint.

Tests: `tests/connectors/test_two_mappings_per_site.py` (Bitdefender: the
gone company spared for endpoints, licences and incidents; the only company
gone still fails; detections asked of the licensed company only) and
`tests/connectors/test_two_mappings_every_read.py` (NinjaOne: devices,
alerts, queries, locations and activity reach over both organisations and
with one gone; ConnectSecure: both companies asked, one-site counting, one
gone; Autotask: the card across both companies with contracts summed, the
review's industries). While writing them: the ConnectSecure test fake's
company filter had never worked — its regex held a literal backspace byte
where `\b` was meant, so it returned every row for any company — fixed, and
the existing single-site tests still pass.

Live after the fix: the merged site's ticket workspace enrichment reads 13
facts from 5 reads with only the Autotask configuration-item 500 left;
`sync_sites --dry-run` now reports five Bitdefender mappings "no longer
found (left in place)" — the franchisee consolidated four of the five
duplicated companies at the vendor since 2026-09-24 — for a person to
retire with `link_sites.py`.

### Deployment preparation for Render, 2026-09-25

`render.yaml` and `docs/deploy.md` were written; nothing was deployed and
no Render account was used. What was verified on the laptop:

- The production start-up refusals, from a shell with the environment set
  and no `.env` read: `ENVIRONMENT=prod` with an http `PUBLIC_BASE_URL`
  refuses to construct settings and names the reason; the configuration
  `render.yaml` sets (`prod`, an https link, `TOTP_REQUIRED_FOR_INPUTIV=true`,
  a `postgres://` database URL off the machine) constructs, with the cookie
  Secure and the database URL rewritten to asyncpg. `tests/test_settings.py`
  covers each refusal singly (debug, SQL echo, a skippable second factor, a
  local public or database host) and that the same options are accepted on
  `local`.
- `TOTP_REQUIRED_FOR_INPUTIV` end to end against the test database
  (`tests/api/test_authorization.py`): a staff account without a code signs
  in, reads itself, is refused a client switch and a client read with `403`
  and `X-Second-Factor: enrol`, enrols on Settings, and is then admitted; a
  franchisee administrator is not held to it.
- `render.yaml` parses; every secret is `sync: false`; the cron block is
  complete and commented out.

Not verifiable before the first deploy, and recorded here as open so the
first deploy records it:

- [ ] **SSE on Render.** The application sends `Cache-Control: no-cache`,
      `X-Accel-Buffering: no` and a keep-alive comment every 15 s, and
      Render's documentation states no buffering policy for streamed HTTP
      responses either way. Run the check in `docs/deploy.md` step 4b after
      the first deploy and write the `started` and `done` timestamps here.
- [ ] The Render health check on `/health` passing on the first deploy, and
      the pre-deploy `alembic upgrade head` log.
- [ ] The database migration's row-count comparison (step 6d), all tables
      identical, and a credential decrypting from a Render shell.

### Coverage matching: NetBIOS collisions, lost devices in the join, external targets, 2026-09-28 (pilot franchisee 1)

The coverage sheet for one site showed three matching defects; each was
fixed in the join and mirrored in the device resolution and the review.
Branch `fix/coverage-matching`; migration 0017 (`devices.status` gains
`rmm_agent_down`).

**What the tenant taught before the change** (counts only):

- `dnsName` against `systemName` across 1,024 computers: 946 agree on the
  first label, 77 have a 15-character `systemName` that is a prefix of a
  longer `dnsName` label (the NetBIOS truncation), 1 differs, 16 have no
  `dnsName`. `dnsName` labels are upper or mixed case like the console;
  where the two agree the join keeps `systemName`'s spelling.
- At the site in question, five machines shared two NetBIOS names.
  ConnectSecure knew all five by their full names; GravityZone knew them
  by the truncated name, one row per machine, each with the machine's own
  MAC. Both were unmatched, and their ten vendor rows were unmanaged.
- Lost devices (no NinjaOne contact in 14 days) tenant-wide: 116. A first
  rule counting a GravityZone row with an agent installed as "alive" made
  91 of them `rmm_agent_down`; 43 of those had been unseen by NinjaOne for
  over ninety days and 5 for over a year, and every one of the 91 rested
  on a row with no scan date. That rule was wrong and was replaced: a date
  is the only evidence of life, and `getManagedEndpointDetails.lastSeen`
  is now read for the rows paired with lost devices. With the dates, 6
  are `rmm_agent_down` via Bitdefender (all seen within 17 days) and 5
  via ConnectSecure (scanned within 5 days); 110 are `rmm_stale`.
- The detail method knows managed endpoints only: it answers "Invalid
  value for endpointId" for a row with no agent. Those rows are not read
  (they are not alive either way) and the rejection is per row, never the
  batch.
- ConnectSecure names that are external targets tenant-wide: 102 IPv4
  literals and 1 reverse-DNS name with an address in it. A further rule on
  ISP pool labels (`dsl-`, `pool-`, `host`, followed by digits) matched 2
  more names, both workstations NinjaOne manages at another site; it was
  dropped and the address shape is the whole test.
- ConnectSecure now sends `fix_script` as a list of `{os_type, script}`
  objects for some solutions (it was a string); the review's collection
  failed on it. The model folds the list into one text.

**Austin Tax Advisors, before and after** (30 managed computers):

| | before | after |
|---|---|---|
| Bitdefender covered / absent / unmatched / RMM stale / agent down | 21 / 1 / 5 / 3 / — | 26 / 1 / 0 / 3 / 0 |
| Bitdefender covered % | 77.8 | 96.3 |
| Bitdefender pairings by pass | 21 exact | 17 exact, 4 netbios15, 5 mac |
| ConnectSecure covered / absent / unmatched / RMM stale | 20 / 2 / 5 / 3 | 25 / 2 / 0 / 3 |
| ConnectSecure covered % | 74.1 | 92.6 |
| ConnectSecure pairings by pass | 16 exact, 4 netbios15 | 25 exact |
| unmanaged | 15 (7 Bitdefender, 8 ConnectSecure) | 4 (3 Bitdefender, 1 ConnectSecure) |
| of which noted "likely not yet onboarded" | — | 2 |
| external (ConnectSecure) | not a category | 2 (1 IP address, 1 reverse-DNS name) |
| gaps | 13 | 3 |

The remaining list after: 1 device absent from Bitdefender and 2 from
ConnectSecure (no row of the hostname, MAC or serial); 3 lost devices, two
of them with a GravityZone row whose `lastSeen` is outside 14 days
(`rmm_stale`, "bitdefender holds it as covered"); 3 GravityZone rows and 1
ConnectSecure row ("agent not reporting") with no managed device; 2
external scan targets. Every remaining unmanaged entry is one vendor row.

**Tenant-wide after**: 1,024 managed, 177 unmanaged (was 191 by the old
dedupe, of which 13 are now noted as default Windows names), 105 external,
60 discovered, 110 `rmm_stale`, 11 `rmm_agent_down`; Bitdefender 85.4 %
covered, ConnectSecure 92.2 %; pairings 800 exact + 65 netbios15 + 11 mac
for Bitdefender, 826 exact + 5 mac for ConnectSecure; no device unmatched.

**Resolution and agreement.** The first resolution under the new rules
created 18 links (9 exact, 9 mac), moved 94 vendor-only links onto managed
devices, deactivated 43 (external targets' former vendor-only links, and
records the vendors no longer return), and emptied the queue (9 open
entries, all now paired by MAC); a rerun created, moved and deactivated
nothing. The daily agreement: 58 sites identical, flag off and on. The
review agreement for `austin-tax-advisors`: identical (`coverage.matched`
26 both ways) once the review kept same-named vendor rows as rows; before
that its name-keyed groups collapsed the three GravityZone rows into one
machine and it paired 23.

**GravityZone was slow all day.** Inventory pages took 27 to 90 seconds;
under the shared 30-second timeout the listing failed on three of five
runs and Bitdefender came back "not assessed" for the whole tenant, which
is the right answer to a timeout and the wrong day to run five times.
Two things came of it: timeouts are per connector, with
`BITDEFENDER_TIMEOUT_SECONDS=120` the default and in the Render group; and
a vendor that does not answer is "not assessed this run" on every path,
never absent, never unmanaged, links untouched, proved with simulated
timeouts (`tests/api/test_vendor_failures.py`). The one live agreement run
after those changes, for `austin-tax-advisors` alone, ran with the default
timeout and agreed. Cold board: 50 s on this day (three listings, two
NinjaOne queries, 116 detail reads for the lost pairings); warm 0.1 s.

Open:

- [ ] The lost pairings' detail reads are one call each (116 today). If
      the lost population grows, read them in the collector and keep the
      dates on the device rather than on every board.
- [ ] `coverage.rmm_agent_down` and `connectsecure.assets.external` are
      facts on the review, not findings; slide 14 has no rule for them yet.

### The stored board, the last-seen kept on the links, and what the page costs, 2026-09-28 (pilot franchisee 1)

Branch `perf/coverage-snapshot` (from `fix/coverage-matching`); migration
0018 (`coverage_snapshots.board`, `device_vendor_links.vendor_last_seen_at`
and `vendor_checked_at`). One live whole-board run at the end, the daily
collector's coverage step (resolution, snapshot, agreement), verbose log
kept in the scratchpad and read for counts.

**Before** (the coverage page as it was, computing on every load): the
GravityZone inventory listing took 27 to 120 s on the day, then one
`getManagedEndpointDetails` call per row paired with a lost device — 116
on the tenant on the 28th, about 90 as reported — on every page load; a
cold board 50 to 120 s, a warm one 0.1 s only within the 180 s inventory
TTL, and every load after that cold again.

**After**, one run, 37 s wall for the whole collector step:

| step | time | vendor calls |
|---|---|---|
| resolution (listings, joins, last-seen reads) | 35.5 s | NinjaOne 1 device listing walk, 3 pages of `network-interfaces` (14,755 rows), 1 page of `computer-systems`; GravityZone 2 listing calls, 45 detail calls; ConnectSecure 3 listing calls |
| snapshot (board computed, stored with the board payload) | 171 ms | 0: inventories cached, dates from the links |
| agreement (board off and on) | under 1 s | 0 |
| a page load (latest snapshot, exclusions laid over, 1.1 MB board) | 27 to 56 ms | 0 |

**The last-seen read, per run**: 92 lost devices with a GravityZone row; 4
alive by the bulk scan date (`includeScanLogs` now on the listing; the
listing had carried no scan date before because it was never asked for);
42 lost for longer than `DEVICE_RETIRE_DAYS` (90) and not checked, their
verdicts saying so; 45 detail calls, all in the resolution; the three
computations after it in the same run took all 45 dates from the links and
made 0 calls, and so will every run for the next 12 hours
(`BITDEFENDER_LAST_SEEN_REFRESH_HOURS`). Concurrency during the read:
`BITDEFENDER_DETAIL_CONCURRENCY` (4). Agreement: 58 sites identical, flag
off and on. The board's numbers are unchanged by the change of path
(85.2 % / 92.2 % covered; 175 unmanaged, two fewer than the 28th's earlier
run, the vendors having moved on).

**Last seen in bulk**: none. The Network methods document and return
`firstSeen`, and `lastSuccessfulScan` with `includeScanLogs`; `lastSeen` is
on `getManagedEndpointDetails` only (api-notes/bitdefender.md). The scan
date is used as the first evidence of life; the detail call fills in the
rest under the window and the cache above.

**The ConnectSecure rows dropped at ERROR** (16 from `asset_view`, 17 from
`agents`) are all one company, id 22785: a live company in the tenant, not
an assessment and not deleted, that no site holds — `unsynced`. It stays at
ERROR until `sync_sites --connector connectsecure` is run and a person
decides whether it is a customer (a site) or the franchisee's own (an
inactive site, after which its rows drop quietly). Assessment and deleted
companies, which sync leaves out on purpose, now drop at INFO with their ids;
there were none behind these rows.

**The NinjaOne cursor** was not repeating a page. `/v2/queries/*` pages by a
server-side cursor name that is sent on every page after the first; the
walk logged this run's `network-interfaces` at DEBUG: page 1 no cursor,
pages 2 and 3 the same name at offsets 0 and 5,000 of 14,755, the last page
short with an empty envelope. The same value seen twice in a board load is
either that, or the whole query walked again after the 180 s inventory TTL
during a long GravityZone listing. The walk now raises on an offset behind
or ahead of the rows already read (a restarted or a skipping cursor), which
is the fault that would matter.

Open:

- [ ] Company 22785 in ConnectSecure: sync it or mark it internal.
- [ ] The board payload is 1.1 MB of JSONB per snapshot per client per day;
      fine at one client, worth a retention rule at ten.
- [ ] A refresh and the daily collector on the same day both replace the
      day's row and the later wins; if a refresh mid-day should not stand in
      for the night's snapshot, keep them apart.

### The Monthly Report PDF, 2026-09-29 (pilot franchisee 1)

Branch `feat/tbr-monthly-pdf`. The review's second output: a landscape US Letter
PDF rendered from the same stored facts as the deck, no model-written text, the
same three modes, the same gate rules, its own approval step (`monthly_report`,
slide 0). No migration in this task.

**PDF approach.** ReportLab 5.0.1: a pure-Python wheel (dependencies Pillow and
charset-normalizer, both wheels), so it installs and runs on Render's native
Python runtime with no system package; the charts are ReportLab graphics, so
they are vectors in the file. WeasyPrint needs Pango and Cairo as system
libraries, and any headless-browser route needs a browser binary, so both were
ruled out before anything was written; Docker is not needed and the runtime is
unchanged.

**One live probe, `GET /v2/queries/software-patch-installs`**, the third-party
install history the deck's collectors had never read (`os-patch-installs` was
the only history used). Read once for August 2026 with `installedAfter` and
`installedBefore`, tenant-wide, counts only: 4,646 rows on 800 devices, keys
`id, productIdentifier, title, impact, status, type, installedAt, deviceId,
timestamp` (the `os-patch-installs` shape with `software-patches`' vocabulary),
`count: null` in the cursor even without `df`, status `INSTALLED` 4,639 and
`FAILED` 7 (at most two failures on one device), type `PATCH` 4,644 and
`INSTALLER` 2, impact `recommended` and `unknown`. The fixture
`query_software_patch_installs.json` is four rows from that shape with ids,
device ids and product names replaced; the probe printed no name. Recorded in
`docs/api-notes/ninjaone.md`. `logged-on-users` was already documented and is
now read for the lifecycle tables' last logged-in user.

**Estimated age.** NinjaOne holds no purchase or warranty date (api-notes,
2026-09-18), so the tables carry the review year less the launch year of the
processor family the name carries (`hardware.processor_launch_year`; the
mapping is in `hardware.py` with its sources by family), blank when the name
does not place it and footnoted with the method. Live on the pilot site: 1 of
101 machines unknown; the ages run 1 to 9 years on the workstations and a
2012-generation server reads 14.

**Iterated against fixtures**: the synthetic review with lifecycle tables,
third-party installs and a first-response share, rendered in all three modes,
rasterised locally with PyMuPDF (local only, not a dependency) and read back with
pypdf (dev dependency). `tests/reports/test_tbr_monthly_pdf.py` (13 tests: the
approval step, the drafts stamp, the facts printed by id, `not reported` never a
zero, the storage and age bands, sub-issue types when Other or unset is over
half, sections omitted and reported, Backup's empty shape, contact and user
privacy, demo pseudonyms, the gate, the file names, and the agreement test that
renders the deck and the PDF from one review and asserts every shared fact
prints the same display string on both and every shared chart carries the same
value per category: 4 or more facts, 60 or more chart points on the synthetic
review) and the API test for render, download and staleness of both outputs.
Full suite: 1,191 passed (1,194 with the location tests added the same day); ruff and mypy strict clean.

**One live render, shelton-keller-group, August 2026.** The stored review
predated the new tables, so it was regenerated once (1 m 49 s, every connector
read live, the earlier row superseded), then the internal PDF rendered before
approval (13 pages, stamped as a draft, 1,457 strings through the gate), the
section approved from the CLI, and the client PDF rendered: **13 pages, no
section omitted** (Ticketing 2 pages, Monitoring 9: one page of patch cards, 7
of workstations, 1 of servers; Backup 1), 1,446 strings through the gate, none
refused. What it holds, counts only: 218 tickets created and 218 completed;
first response 100% and resolution 98.2% of 218 against the 90% target; 167
tickets from people and 51 from automation, the top 5 contacts as Contact A to
E; issue type set on 87.2%, so no sub-issue chart; 107 OS and 520 third-party
installs, none failed; 99 workstations and 2 servers in the lifecycle tables,
92 distinct logged-in users hidden as User A onwards, every workstation with
storage reported and both servers without (`not reported`, never zero);
Backup not yet assessed. Two layout corrections came from the live page and
not the fixtures: issue-type labels overprinted at eight categories (now angled
when a name is over 12 characters), and the stored contact chart's definition
mentioned the automation bar the PDF shows as a count (now a code-written line).

Open:

- [ ] The lifecycle tables list every machine: at 99 workstations that is seven
      pages. A per-site cap or a "worst first" ordering is a product decision.
- [x] `Location` is NinjaOne's location name, which can carry the customer's
      name. Done the same day: locations are recorded on `facts.names`,
      replaced whole in a demo (`Location A`, before the site name inside
      them), withheld in a private client render when they name a person, and
      the demo gate now refuses any real name (site, client, contact, device,
      user, location; three characters or longer) found as a substring,
      case-insensitive, anywhere in the deck or the PDF. Re-rendered the pilot's
      August demo PDF from the stored review: 13 pages, 255 terms checked,
      none survived, 4 locations pseudonymised; the demo deck passed the same
      check (17 slides). The cost of a substring rule is written down in the
      tests: a device named after a common word makes that word a hit.
- [ ] The Backup section's Cove facts (`cove.*`, `coverage.backup.*`) are
      declared and drawn empty; the Cove connector fills them and nothing on
      the page changes.

### People's names as a franchisee setting; workstation or server by operating system, 2026-09-29 (pilot franchisee 1)

Branch `feat/tbr-monthly-pdf`, from a review of the Sobering Center August PDF.
**Migration 0019** (one audit event type, `report_config_changed`).

**People's names.** `FranchiseeConfig.show_people_names`, set with
`manage_client.py report-config --show-people-names`, is now the default for the
contact chart, the lifecycle tables' last logged-in user and a person-named
location on client and internal renders of the deck and the PDF; the per-review
setting is an override of it (True, False, or cleared), a stored review from
before the setting reads as no override, and a demo render pseudonymises
everyone whatever it says. Turned on for the pilot: the change copied the value
onto 4 current reviews, emptied their renders (status back to draft, so a
released review is unreleased until rendered again) and wrote one
`report_config_changed` row naming them. The client-mode gate is armed only
while the effective setting is off; `tests/reports/test_tbr_privacy.py` and
the PDF tests cover both positions and the override.

**Device role.** One function, `hardware.device_role`, decides workstation or
server for the deck's counts, the low-memory list and the PDF's lifecycle
tables: Windows is a server only when the OS name says Windows Server, macOS
is a workstation, Linux and an unknown OS take NinjaOne's node class, and the
basis is kept per device. Devices where the two disagree go in an internal-only
table the internal PDF prints under the lifecycle tables and the internal deck
lists in the notes of the slide with the counts. An empty server table is
written and the PDF prints "No servers at this site". Iterated against the
recorded NinjaOne fixtures with one OS row changed in memory
(`tests/reports/test_tbr_device_roles.py`: a Windows 10 IoT device classed
`WINDOWS_SERVER` lands in the workstation table and the counts follow), and the
agreement test now asserts the lifecycle rows are the deck's counts.

**One live run, sobering-center, August 2026** (2 m 10 s, the earlier review
superseded), then the client, internal and demo PDFs (9 pages each, no section
omitted, gate passed) and the client deck (17 slides):

| count | before (node class) | after (operating system) |
|---|---|---|
| devices under management | 55 | 55 |
| workstations | 38 | 39 |
| physical servers | 1 | 0 |
| virtual servers | 0 | 0 |
| workstation lifecycle rows | 38 | 39 |
| server lifecycle rows | 1 | 0, "No servers at this site" |

Disagreeing devices: one. NinjaOne node class `WINDOWS_SERVER`, operating
system `Microsoft Windows 10 IoT Enterprise Edition`, counted as a
workstation; it is named on the internal PDF's list and in the internal deck's
notes for slide 5, and not here. The site has no server, which the earlier
review's "Physical servers: 1" contradicted. With the franchisee setting on,
the client PDF names the five contacts on the chart and the last logged-in user
on every row, and prints no `Contact A`, `User A` or withheld location.

**Stored documents after the release.** `GET /reports/tbr/{id}` failed
against a process still running the code from before this branch, once
`set_people_names` had rewritten four reviews with `show_contact_names:
null`: that code typed the field `bool`. The committed model types it
`bool | None = None`, and every reader resolves the effective value through
one property, `ReviewDocument.people_names_shown` (the override when set,
else `franchisee.show_people_names`, default False when the snapshot predates
it); the deck, the PDF, the API view and the page read nothing else. What
writes `null`: `set_people_names` (every current review of the client),
"Use the franchisee default" on the page (`set_contact_names(None)`), and
any fresh generation (no override). Migration 0019 touches only the audit
constraint and no document. Local database: 31 stored reviews (4 current,
27 superseded), stored values `false` 21, `null` 5, absent 4, `true` 1,
all 31 load (`scripts/check_stored_documents.py --include-superseded`).
Tests store and load the override as true, false, null and absent, at the
model and through the database and the API.

The other stored fields this branch changed the meaning of without changing
the type, each read by the new code without a migration:

- `renders` keys: `pdf/<mode>` beside the bare deck modes (`dict[str, str]`).
  Older code would list them as modes and never find a file under them.
- `sections[].slide = 0` for `monthly_report` (`int`). New `slides_to_keep`
  keeps only numbers in `SLIDES`; the code before it would raise on slide 0
  once the section is approved, so a rollback needs those reviews regenerated
  (deploy runbook, step 9).
- `facts.names` gained `users` and `locations` (`dict[str, list[str]]`);
  absent in older rows, read as empty.
- `franchisee.show_people_names` in the config snapshot; absent in older
  rows, read as False.
- `facts.tables` gained `ninjaone.devices.role_disagreements`
  (`internal_only`), the empty lifecycle server table, and
  `show_contact_names_by` is now set when an engineer hides as well as shows.

Open:

- [ ] `role_of_class` treats a VM guest as a server; a virtual desktop
      (`VMWARE_VM_GUEST` running Windows 11) is a workstation by OS, which is
      right, but a Linux VM guest is a server by class even when it is someone's
      desktop. Watch for it on a Linux-heavy site.
- [ ] The people setting has no API or page control yet; `report-config` on the
      CLI is the only way to change it. The review's own override is on the page.

### The demo tenant, 2026-09-29 (no live vendor calls)

Branch `feat/demo-tenant`. **Migration 0020** (`clients.kind`, `audit_log.demo`).
Nothing in this task touched a vendor; every run below was against the fakes
in `app/demo/`, in the local database.

What was built: `Client.kind` (live or demo); the factory serving a demo
client only from `app/demo/connectors.py` (the real clients over an
`httpx.MockTransport`, needing the generated `World` to be built at all) and a
live client only from the real classes; `set-credentials` refusing the demo
client; audit rows for a demo client marked `demo` in `record_event`; the
generator (`dataset.py`, seed 20260929), the public vocabulary, the four
fake vendor APIs written to the endpoint inventory of the real clients, the
scenario list with its check, and `scripts/demo_tenant.py create | reset |
delete`. Documented in docs/demo-tenant.md.

Dataset, seed 20260929: 8 customers, 262 devices (254 computers and one
switch each), 8 vendor-only endpoints, 686 tickets, 2,895 patch installs,
129 pending patches, 162 alerts, 14 detections, 595 vulnerability findings,
84 people. Scenarios: 30, every one present (`scenarios.check` empty).

Run through the product, locally:

- `demo_tenant.py create`: the client, then the same site sync a live client
  gets, through the fakes: NinjaOne 9 organisations (8 customers, the
  franchisee's own recorded internal), Autotask 8 (the internal and a
  prospect are not customers), GravityZone 9 with Fabrikam's second company
  linked by device overlap at 100% and reported as vendor duplication,
  ConnectSecure 7 (an assessment and a deleted company skipped). Report
  settings written, the demo organisation created, two invite links printed.
- `collect_events.py`: detections collected for the one EDR-licensed customer
  and `not_applicable` for the seven others; 266 devices resolved, 484 exact
  links and 2 by MAC (the NetBIOS twins), 4 unresolved (the twins nothing
  tells apart, both ways); the board 254 managed, 6 unmanaged, GravityZone
  98.0%, ConnectSecure 97.9%; 8 sites identical flag off and on. The board
  holds every state: covered, stale, absent, unmatched, `rmm_stale`,
  `rmm_agent_down`, `not_assessed` (the AV-only customer on ConnectSecure and
  Backup everywhere), plus 6 unmanaged of which 2 "likely not yet onboarded",
  2 discovered, 2 external, 8 network devices left out.
- `tbr.py generate` for Contoso, September 2026 (model narrative on): 19
  slides on the deck; slide 14 with three findings from three tools and three
  more held off it (open criticals, software to remove, a few updates resolve
  most); the client deck, the client PDF (10 pages) and the demo PDF all
  passed their gates. 132 audit rows for the demo client, all marked `demo`,
  none on any other client.

Tests: `tests/demo/` (the world: determinism, size, every scenario,
fictional names, relative time; the fakes through the real clients, both
silent filter behaviours, the read-only gates, the transport guard) and
`tests/agent/test_demo_factory.py` (the factory in both directions against
the database, the credentials refusal, the audit mark, and the whole product
— sync, board, review facts — through the fakes). Full suite green; ruff and
mypy strict clean.

Open:

- [ ] `excluded` is the one coverage state the world does not place: it is an
      action a person takes on the page, and the demo should show it that way.
- [ ] The narrative model is still called for a demo review; a canned
      narrative for the demo tenant would make `generate` free and instant.
- [ ] The chat agent was not run against the demo in this task (a model call
      per question); the tools it calls are the same connectors the board and
      the review used.

### Exercising the demo tenant as a presenter, 2026-09-29 (no live vendor calls)

Branch `feat/demo-tenant`. Every feature run against `tailwind-it-demo`, one
pass each. No migration. No vendor was called: the whole board and a whole
review were computed again with every real `httpx` transport replaced by one
that raises, and nothing attempted an outbound request (the model and its web
search are the only things that leave the process, and they are the Anthropic
SDK's own client).

**Chat**, eight booth questions through `selection_check --execute` against the
demo client, `show_people_names` on: **8 of 8 correct**, 11.7 to 35.7 seconds,
every answer non-empty and right against the dataset. The questions and what
each should show are `tests/selection/demo_questions.yaml`. Tools called, in
order: `list_open_alerts` (+3), `list_devices_not_checked_in` (+2),
`list_devices`, `list_critical_vulnerabilities`, `list_sla_breaches`,
`search_tickets`, `list_patch_installs`, `web_search`.

**Ticket workspace**: two tickets opened and planned. Both resolved their
device by hostname at high confidence; 10 of 10 steps kept on each, 28 and 29
citations (facts and prior tickets), 0 dropped, 0 commands shown, 22 s and
18 s. The plans cite the resolution notes added in this pass ("six prior
tickets with the identical symptom").

**Coverage**: page read of the stored board 8 ms; Refresh recomputed and stored
in 210 ms. An exclusion added with a reason as the demo admin turned the device
`excluded` with the reason on the board, and withdrawing it put it back to
`absent`. Device detail for three machines: 53 to 162 ms, four vendor blocks
each, `available: false` where no link exists.

**Reviews**: nine generated (Contoso, Northwind, Woodgrove for July, August and
September 2026), 56 to 80 seconds each. All three September reviews approved
and rendered in client mode: decks of 19 to 20 slides and PDFs of 9 to 10
pages, every gate passed. September carries the scorecard trend arrows against
August (up, steady, down) and month-over-month sentences on the deck
("Windows patch installations that failed in the period went from 10 to 5").
Woodgrove has no comparison sentence because nothing moved enough, which is the
rule working; demo Contoso.

**Roles and isolation**: the demo admin and viewer belong to the
`tailwind-it-demo` organisation, so their client is the demo client and no
other; a live franchisee's organisation points at its own client and can never
name the demo one. 841 audit rows for the demo client, every one marked `demo`,
and no demo-marked row on any other client.

**Two real product bugs, both fixed here with a test:**

- `scripts/selection_check.py` could not express a question whose right first
  move is the web. `AgentResult.first_tools` records `web_search` and the loop
  offers it, but `selection.parse_questions` validated expectations against the
  tool registry alone, so "what is KB… and are there known issues?" was a
  question the file could not hold.
- Chat and the business review disagreed about what a server is.
  `list_devices(node_class="server")` matched the substring `SERVER`, so a
  virtual server (`VMWARE_VM_GUEST`) was left out, while the deck's own count
  (`hardware.SERVER_CLASS_MARKERS`, `device_role`) includes it. "What servers
  does this customer have" and the deck's server count gave two answers for one
  site. The shorthand now uses the review's own marker set.

**One more, minor, also fixed**: `BitdefenderClient.list_companies` reads the
whole tenant (`getNetworkInventoryItems` for companies takes no `parentId`) but
was judged against the one targeted site, so every site-scoped review logged a
WARNING saying the response "was not narrowed" to a site nobody had asked the
vendor to narrow to. It is now judged against the whole allowlist, as the
tenant-wide endpoint read already was.

**Dataset fixes** (the demo, not the product): resolution notes now carry a
root cause and the fix for every issue type, so the ticket workspace has
something worth citing; two tickets per customer are open and 5 to 26 hours
past their due time, so "which tickets are breaching SLA" is about today;
every customer has open alerts raised in the last day, four at Northwind; one
Northwind machine is both unprotected and nine days dark; `offline` was true
for anything not seen in 72 minutes, so the whole fleet read as offline (now
about a quarter, as laptops do). Four scenarios added for these, 34 in all,
and `scenarios.check` still passes.

Full suite: 1,247 passed; ruff and mypy strict clean.

Open:

- [ ] Woodgrove's month-over-month numbers barely move, so its September review
      has no comparison sentence. Correct, and a thin demo: point at Contoso.
- [ ] `list_unprotected_endpoints` cannot see a machine with no GravityZone row
      at all (it is GravityZone's own view), so "no working protection" needs
      the coverage join to be complete. The model joined two lists and
      explained it, but a single tool that answers the question directly would
      be better.
- [ ] Generating a review still calls the model per section, about a minute a
      site. A canned narrative for the demo tenant would make the booth
      instant.

### The Resolved foundation: SLA clocks, the outcome ledger, baselines, 2026-09-30 (pilot franchisee 1)

Branch `feat/resolved-foundation`. **Migration 0022** (`ticket_outcomes`).
Read-only to every vendor; what is written is our own ledger table.

Built: per-franchisee Resolved settings (`app/resolved/config.py`, stored in
`report_configs.config` beside the business hours the clock runs on, every key
a label and never a picklist number); the business clock (`clock.py`, local-day
walk so a daylight-saving change cannot distort a span); the two clocks and the
five states (`sla.py`), which every breach figure in the product now comes from
— the Tickets page, `/tickets/statistics`, `list_sla_breaches` and the business
review's service facts; the outcome ledger (`ledger.py`, one upserted row per
completed ticket, reopenings updating the row); and baselines with cost by
class (`baselines.py`). `scripts/resolved.py ledger | baseline | breaches`, and
`report-config --resolved-file` to set the settings.

Demo tenant first (no vendor calls): 35 open tickets, breached 16 under the old
rule and 15 under the new one with 1 paused; the ledger 666 rows over 90 days,
byte-identical on a rerun; baselines L1 327 / L2 200 / L3 139 tickets with cost
per ticket 69.37, 87.11 and 125.81 — the class progression the shape of the
service predicts.

**Live, one franchisee, counts only.** Settings were held in memory for the run
rather than stored (see Open), and the ticket priority and status picklists were
read to key them: 4 priorities, 33 statuses, targets drafted for all 4
priorities, 7 statuses named as pausing a clock, 2 class rules (Critical → L3,
High → L2, everything else L1).

- Open queue: 139 tickets. Autotask's own fields on them: `dueDateTime` 139,
  `firstResponseDueDateTime` 102, `resolvedDueDateTime` 55,
  `firstResponseDateTime` 136. The two due fields being on 102 and 55 of 139 is
  the whole argument for computing the clocks ourselves.
- Breached, old rule (any due time Autotask populated, in the past): **82**.
  Breached, new rule: **108**. Every state, new rule: breached 108, due_soon 13,
  ok 10, paused 8. The 8 paused are reported and judged neither way.
- Ledger, 90 days: **2,721 rows** written from 2,721 completed tickets, 0 newly
  reopened, 5,033 time entries read, 2,258 tickets costed.
- Baseline by class, 90 days: L1 2,497 tickets (pickup median 0, p90 117 min;
  resolve median 88, p90 3,240; 0.95 h/ticket over 2,111), L2 222 (pickup median
  10, p90 114; resolve median 114, p90 590; 0.93 h/ticket over 145), L3 2
  (1.5 h/ticket). Cost per ticket at a provisional $75/h loaded cost: 71.45,
  69.70 and 112.50.

`list_sla_breaches`'s description changed with the calculation, so the
selection check was run against the demo tenant with a new question for it
("anything about to miss its SLA?"): **28 of 28 correct at 42 tools** (before
the change it was 27 of 27 at 42). No tool was added or removed.

**A pre-existing connector bug the ledger was the first thing to hit.** The
90-day completed-ticket listing is the first Autotask query in this codebase to
run past 500 rows, and it failed: a POST query's `pageDetails.nextPageUrl` is
`/V1.0/<entity>/query/next?paging=…`, not the `?search=` URL a GET query
returns, and it answers a GET with **405 Method Not Allowed** and `Allow: POST`.
`_query_all` now POSTs any `/query/next` URL with the same filter body; the
paging fixtures carry both shapes and a test asserts the POST and the refused
GET. Until today every Autotask listing over 500 rows would have failed outright
— the open queue and the by-id reads all fit one page, which is why nothing
caught it. Recorded in docs/api-notes/autotask.md. (This also answers the open
item below: the URL *is* returned, so listings do not stop silently at 500.)

Open:

- [ ] **The franchisee has no `business_hours.timezone`**, so the clock ran on
      08:00–17:00 **UTC** — for this franchisee that is 03:00 to 12:00 local, and
      it is most of why L1's pickup median is 0. CLAUDE.md says a zone is never
      guessed, so it was not set here; one `report-config --timezone` sets it,
      and the ledger and the baselines should be rebuilt after (the ledger is an
      upsert, so a rerun replaces the rows).
- [ ] **The Resolved settings are not stored for this franchisee.** The run held
      the draft in memory; until they are saved, the franchisee's targets are
      empty and every clock on the Tickets page reads `no_target`. The drafted
      targets are a sensible agreement's shape, not one anybody has signed, and
      the $75/h loaded cost is a placeholder.
- [ ] The 2,721 ledger rows were measured on those provisional settings and the
      UTC clock. They are an upsert away from being right and should be rebuilt
      once the settings and the zone are stored.
- [x] This machine had no time zone database, so the local-day walk across a
      daylight-saving change was asserted only where `tzdata` was installed
      (`tests/resolved/test_clock.py` skips otherwise) and the run could not
      show the Chicago figures beside the UTC ones. Closed 2026-10-01: `tzdata`
      is a dependency, so the DST assertion runs everywhere. The run was not
      repeated — the franchisee's zone is still unset, which is the item above.
- [ ] No status history exists at the vendor, so a clock is paused only while
      the ticket sits in a pausing status now. Time spent waiting last week is
      not deducted, which under-counts pauses and over-counts breaches by an
      amount nobody can measure from this API.

### The scrubber was eating the ticket's subject, 2026-10-01 (pilot franchisee 1)

Branch `feat/resolved-foundation`. No migration. No vendor call for the
finding: it came out of the ledger's own stored titles.

The ticket-mix analysis could not count password resets, and the reason was
ours. `_STRONG_KEYWORD` made the separator optional, so the word *after*
`password` was taken whether it was a secret or not: `Password reset for a
user` was stored as `Password [redacted] for a user`. Measured on the ledger,
2,745 stored titles: 49 mention a password and **30** carry a redaction
straight after a keyword with no punctuation before it — resets, expiries and
changes made indistinguishable, which is the commonest thing a helpdesk does.
Not one of the 36 redactions in those titles came from a keyword *with* a
separator, so the loose rule was doing all of the damage. One of the client's
nine ticket workspaces holds text the fix would change too.

The rule now follows the strength of the signal: a punctuation separator
(`:`, `=`, `->`) means somebody is writing a value down, so anything after it
goes; with `is`, `was`, `-` or nothing, only a token that looks like a secret
does — not an ordinary word, and carrying a digit or an inner symbol. Sentence
punctuation is stripped before that test, which is what keeps `Password
reset.` readable and is also why `password Welcome!` is kept: it is the same
shape as a sentence. A plain-word password written with no punctuation is the
deliberate cost, stated in the module and in a test.

**Stored titles were not rewritten** (the brief said not to, and the record is
append-only anyway): the fix applies to text read from now on, so the 30 are
only recovered by a rebuild that re-reads those tickets.

Also measured from stored data the same day, counts only: 15 distinct issue
types over 2,724 completed tickets; 2,170 hours logged over 2,272 of them;
recurrence by title similarity at the same site inside 30 days 25.8% overall
but 74% of untyped tickets, 72% of Backup/Restore and 64% of Cybersecurity;
the ten largest site-and-issue-type pairs are 28.5% of all tickets and four of
the ten are one customer; and **every** per-ticket time total is a multiple of
15 minutes (731 at exactly 15, 547 at 30, 284 at 45).

Open:

- [ ] Sub-issue types are not stored, so the 287 tickets that are `Other` or
      untyped cannot be split. Needs `Tickets.subIssueType` and its picklist,
      and a column to put it in.
- [ ] Per-time-entry durations are not stored either — only the per-ticket
      total — so "do entries cluster on 15 or 30 minutes" is answerable only
      at ticket level, and not per engineer at all.
- [ ] Recurrence by **contact** or **device** needs `contactID` and
      `configurationItemID`; neither is on the ledger, so same-site title
      similarity is the proxy and it cannot tell a repeat caller from a
      repeated fault.
- [ ] Autotask refused every query for roughly an hour on 2026-10-01 (a
      failed rebuild at 06:01 and a refused open-queue read at 06:5x, both
      HTTP 500 on filters that had answered 200 minutes earlier), then
      answered a small `Resources` query normally at 07:0x. Cause unknown; the
      api-notes item about what an exceeded request threshold returns is still
      open and is the obvious candidate.

### Autopilot Level 0, shadow mode, 2026-10-01 (pilot franchisee 1)

Branch `feat/autopilot-shadow`. **Migration 0023** (`shadow_runs`,
`shadow_predictions`, `shadow_enrichments`, three audit event types). One head
before and after. **No Autotask call for Part A**: the replay runs entirely on
stored `ticket_outcomes` rows, and Part B is tested against injected fakes only.

**Field inventory first, and it changed the design.** Of what the ledger holds,
only four fields are known at creation — the scrubbed title, the site, the
creation time and the ids. Two decisions came out of the inventory and were put
to the operator rather than assumed: the stored **queue** is read at completion
and `Escalation` (416 rows) and `Triage` (147) are routing destinations, so
including it would leak the outcome — excluded from the headline run, with a
`with_alert_queue` variant to measure its worth later; and the 30 titles the old
scrubber damaged are predicted on but quarantined out of every accuracy figure.
Two fields that would matter are not stored at all: the vendor's own `source`
(already in `AutotaskTicket`, never kept — it would be real ground truth for
origin) and the ticket **description**, so the classifier sees a subject line
whose median length is 5 words and of which 28% are three words or fewer.

**The replay covers completed tickets only.** `ticket_outcomes` is one row per
*completed* ticket, so the population is resolved work: no still-open ticket is
in it, and the forward shadow's population will not look like this one. Any
figure below is a figure about tickets that got finished.

**Full replay, 2,722 tickets, 9 minutes, 4.78M input + 442k output tokens, 0
invalid answers.** 175 answered by rule, 2,547 by the model. 2,013 groups out
of 2,722 tickets in.

| issue_type accuracy | answered | correct | share |
| --- | --- | --- | --- |
| everything | 2,274 | 1,433 | **63.0%** |
| confidence >= 0.80 | 916 | 711 | 77.6% |
| **>= 0.80, excluding one mis-used label** | 782 | 707 | **90.4%** |
| >= 0.90, excluding it | 260 | 242 | 93.1% |
| the rule path | 158 | 158 | **100%** |

**One label in the tenant's picklist costs about thirteen points.** The agent
predicted `Customer Onboarding Offboarding` 219 times; the tenant filed 9
tickets under it in 90 days and files that work under `User Access and
Management` instead. That single confusion is 151 tickets and **18% of every
wrong answer**, and six of the eight most confident errors (0.95-0.97). It is a
configuration fault, not a model one: the label exists, the franchisee does not
use it, and the closed set offers it anyway.

Calibration is **not monotonic at the top** — 0.80-0.90 scores 80.1% but
0.90-0.95 only 71.1% — which is almost entirely that same label arriving with
high confidence. Excluded, the ladder behaves (90.4% at 0.80, 93.1% at 0.90).

`urgency` 57.1% against the final priority, which is approximate ground truth by
construction and worth little until creation-time priority is captured.
`Other` 16.7% and `User Access and Management` 30.4% are the weak categories;
`Cybersecurity` 97.0%, `Phone` 92.9%, `Printer/Scanner/Copier` 90.0%,
`Backup/Restore` 88.8% and `Server` 88.6% are the strong ones.

Cost: **unpriced** — the two per-million-token settings have no default, so the
run reports 5.22M tokens and says so rather than printing a zero. Latency 2.3s
per model call; ~1,920 tokens a ticket, of which the cached system prompt is
most. At this franchisee's volume (2,722 completed tickets in 90 days, ~907 a
month) a month is about 1.74M tokens, and twenty franchisees about 34.9M —
multiply by whatever rate the operator enters.

Origin has no ground truth yet, as expected: 1,850 `human`, 524 `alert`, 348
`unsure`. Against the one weak proxy (a ticket nobody was ever assigned) 34.5%
of the 235 unassigned were called `alert` and 69.2% of the 2,457 assigned were
called `human` — reported as a proxy and not as a score. A 200-ticket labelling
sheet is exported to `var/` over 64 sites and all 15 issue types, and 30 groups
beside it for a spot-check.

Part B is built and **off** (`AUTOPILOT_SHADOW_LIVE` false; `forward_pass`
refuses and writes not even a run row). It reuses the workspace's own
`resolve_device` and `enrich` through `app/autopilot/wiring.py` rather than a
second enrichment path, counts its own requests per hour, backs off on any
non-200, and logs the full status, response headers and a bounded body of every
refusal — which is how the open "what does Autotask's threshold return"
question gets answered next time.

Against the provisional gate: issue_type **fails at 63% raw and 77.6% at any
threshold, and passes at 90.4% above 0.80 once the one mis-used label is
dropped**; origin precision is unmeasured until the sheet comes back; grouping
is unjudged until the 30 groups are read; the forward fortnight has not started.

Open:

- [ ] Drop or remap `Customer Onboarding Offboarding` for this franchisee and
      re-run: that is the 13 points, and it is a settings change.
- [ ] **The ledger widening — now 0025**, because labelling took 0024. In one
      list: `source` (the vendor's own
      origin field, already in `AutotaskTicket` and never kept — real ground
      truth for `origin`, which today has none), `subIssueType` (which would
      split the 287 tickets that are `Other` or untyped), `contactID` and
      `configurationItemID` (which recurrence by caller or by device needs),
      the ticket **description** (most of what a person reads to classify;
      the forward shadow passes it in flight and stores nothing), and the time
      aggregates. Creation-time **priority** belongs on that list too, or
      `urgency` can never be scored against anything but a value that moved.
- [x] Input tokens included cache reads, which are billed at a fraction.
      Closed 2026-10-01: counted apart, on the run and on every prediction,
      with their own rate setting.
- [x] A long replay was invisible until it committed and lost everything if
      interrupted. Closed 2026-10-01: committed in batches of 100 with progress
      printed, and resumable by run id.

### Autopilot Level 0 on a temporal holdout, 2026-10-01 (pilot franchisee 1)

Branch `feat/autopilot-shadow`. **Migration 0023** again, extended and
re-applied from scratch: the dev database was downgraded to 0022 and upgraded,
so all three tables and the new columns come from the migration rather than
from anything applied by hand. One head. No Autotask call: still stored rows
and the model only.

**The window is split now**, on creation time: the first 60 days are where a
label policy and a confidence threshold may be chosen, the last 30 are scored
and otherwise untouched. A threshold chosen by looking at the tickets it is
then scored on is a memory rather than a measurement, and the two numbers below
are the reason that distinction earns its keep.

**Did the first 60 days alone find the mis-used label? Yes, unmistakably.** In
the development period the agent predicted `Customer Onboarding Offboarding`
wrongly 167 times — **27.5% of all 607 wrong answers there** — against the 8
tickets the franchisee actually filed under it in those 60 days. The single
biggest confusion was 123 tickets that they file as `User Access and
Management`. So the policy was set from development evidence alone: a remap,
`Customer Onboarding Offboarding -> User Access and Management`, saved with
their other settings (`report-config --autopilot-file`).

**The holdout: 776 predictions, 720 judgeable.** With the policy applied:

| threshold | answered | accuracy | coverage |
| --- | --- | --- | --- |
| none | 608 | 65.6% | 84.4% |
| 0.60 | 464 | 73.3% | 64.4% |
| **0.66** (chosen on development) | **354** | **82.2%** | **49.2%** |
| 0.72 | 312 | 85.3% | 43.3% |
| 0.80 | 247 | 87.4% | 34.3% |
| 0.90 | 96 | 89.6% | 13.3% |

Before the policy the same ladder reads 61.2%, 68.1%, 75.7%, 78.9% and 77.1% —
so the remap is worth **3 to 8 points** depending on where the threshold sits,
and it is worth most exactly where a threshold would be chosen.

**The gate fails on the holdout, and it fails in the way the split exists to
expose.** Taking the lowest development threshold that met both conditions
(0.66: development 85.8% accuracy, 51.8% coverage) and reading the holdout at
that same threshold gives **82.2% and 49.2%** — coverage passes, accuracy
misses by 2.8 points. Had the threshold been picked by looking at the holdout,
0.72 would have "passed" at 85.3% and 43.3%; that number is not a measurement
of anything and is recorded here only to show the size of the trap. The
generalisation gap between the two periods is 3.6 points at the same
threshold.

**`User Access and Management`, before and after the label change**, on the
holdout: **32.9% -> 71.2%** over 80 judged tickets. That one remap is most of
the headline movement.

Accuracy by title length on the holdout, which went the opposite way to
expectation: three words or fewer **68.8%** (176 answered), four to six
**65.2%** (256), seven or more **63.1%** (176). Longer subjects are not easier
here — a long one is often a forwarded thread, and the extra words are
somebody's signature.

The rule path on the holdout: **45 of 45 correct**, 48 judged. Three runs in,
it has not been wrong yet.

**Cached input, now counted apart, changes the cost picture.** The whole run
read 1,017,233 fresh input tokens, 442,079 output and **3,761,066 cached** —
so 79% of the input was the same instructions being re-read, which the earlier
entry reported as fresh input and thereby overstated several-fold. Still
unpriced: the three per-million rates have no default.

Origin remains unscored, with two proxies and a new one worth noting: of the 49
holdout tickets raised into an alert-shaped queue, the agent called **49 of 49**
`alert`. That is a proxy, not a score — the labelling sheet is still the plan —
but it is the strongest signal yet that origin is learnable from the subject.

**The `with_alert_queue` variant could not be measured.** Its run died of an
account credit limit after 610 of 2,547 model calls: 1,937 tickets came back
`BadRequestError`, "your credit balance is too low". The run recorded every one
as an error and repaired none, which is the behaviour this phase is built on —
but it also ground through 1,937 calls nobody could have expected to succeed.
Fixed the same day: three consecutive errors of a kind that will not fix itself
(authentication, permission, bad request) now stop a run cleanly with
`stopped_reason = model_refused`, while a single refused ticket does not. The
variant is waiting on credit.

**Decided, 2026-10-01, and these stand.**

1. **The issue_type verdict is a fail.** 82.2% accuracy at the
   development-chosen threshold of 0.66 on the holdout, against a target of
   85%. Not "nearly", not "passes at 0.72": a fail, recorded as one.
2. **The threshold rule is fixed in advance from here on**: the lowest
   development-period threshold where accuracy is at least **88%** and coverage
   at least **40%**. In settings (`AUTOPILOT_THRESHOLD_MIN_ACCURACY`,
   `AUTOPILOT_THRESHOLD_MIN_COVERAGE`) and copied onto every run's config, so a
   run carries the rule it was judged by. 88 rather than 85 because the holdout
   came in 3.6 points below development at the same threshold, and a rule with
   no headroom for that gap is a rule that passes on development and fails in
   use.
3. **This holdout is spent.** It has been looked at, a label policy was chosen
   partly in its light, and a threshold rule was written after seeing it. The
   next gate reading comes from **fresh tickets through the forward shadow**,
   not from these 776 again.

Open:

- [ ] The variant is unmeasured. Re-run it once there is credit; the baseline
      run is on the record to compare against.
- [ ] Accuracy on the holdout is 2.8 points short at the development-chosen
      threshold. The next things to try, in order of expected value: the
      ticket **description** (0025 — the single biggest missing input), the
      `Other` category (9.7% accuracy on the holdout, and 52 judged tickets),
      and the remaining label overlaps the confusion matrix shows
      (`Cybersecurity` taken for `Email`, `Shared Drive` for
      `Application/Software`).
- [x] The selection rule for a threshold was not fixed in advance, and it
      changed the verdict. Closed 2026-10-01 by decision 2 above: lowest
      development threshold with accuracy >= 88% and coverage >= 40%, in
      settings and on every run's config.

### The two Autopilot pages, 2026-10-01 (demo tenant)

Branch `feat/autopilot-shadow`. **Migration 0024** (the three labelling tables)
applied; `alembic heads` prints one revision. **No vendor and no model call.**
The pages were built and read against the **demo tenant**, not the pilot:
the labelling page necessarily shows ticket titles and customer names, and a
screenshot of the pilot's would put both in a file that outlives the probe.
The results pages were read against the pilot's two replay runs as well, which
print ids, labels and counts only.

The demo tenant had no shadow run, and there is no model credit, so one was
made by passing the real `replay` a **stand-in model client** — the seam that
exists so tests can — answering from the demo ledger with deliberate noise.
664 tickets, two runs (`title_site_time`, `with_alert_queue`), 0 errors. The
rows are the real replay's; only the answers are stood in.

What the run through the pages found, in order:

1. **The labelling writes never committed.** `get_session` does not commit and
   every other write route in the application says so itself; these did not, so
   `POST /sets` returned a sample, the page drew it, and the transaction was
   discarded on the way out. Found by building a set twice and getting an empty
   list back. Fixed on all four writes.
2. **Going back was unreachable.** `next_item` walks past what the labeller has
   answered, which is right for working forward through 200 tickets and makes
   ← useless — and relabelling is explicitly allowed. Added `item_at` and an
   `at` parameter, so Back asks for a position rather than for "the next one".
3. **The gate read origin precision over the holdout only.** The labelled
   sample is drawn across the whole window and nothing is tuned on it, so
   narrowing it to the holdout threw away three quarters of the hand labels:
   6 tickets called alert instead of 19 on the demo run. Origin and grouping
   are now read over the run; the issue-type pair stays on the holdout, where
   the discipline is the point.

Read on the demo run after 88 origin labels (two labellers, 16 double-checked,
2 disagreements left out) and 22 of 30 groups judged: issue type **92.3% at
72.6% coverage** on the holdout at threshold 0.70 (chosen on development by the
fixed rule), origin precision **66.7% — fail**, grouping **100% — pass**,
forward shadow **not measured** (the switch is off). A gate panel with a pass,
a fail and a not-measured in it, which is what it was wanted for.

A franchisee administrator, technician and viewer each get **403** on both
pages and on every endpoint, live and in the suite.

Open:

- [ ] The demo tenant's ledger carries no unassigned tickets and no alert
      queue, so both origin proxies read "—" there. They are exercised on the
      pilot's runs and not on this one.
- [ ] Nothing has been looked at on a projector yet; the pages were read at
      1440x900.

### Suggestions on the Tickets page, 2026-10-02 (demo tenant)

Branch `feat/autopilot-suggestions`, cut from `feat/autopilot-shadow` rather
than from main: the shadow branch had not been merged, and branching from main
as the brief assumed would have dropped the whole foundation this extends.
**Migration 0025** (`ticket_suggestions`, `suggestion_reviews`, `blind_checks`)
applied; `alembic heads` prints one revision. **No vendor and no model call.**

The demo tenant's suggestions come from a **simulated forward pass**: the real
`forward_pass` with its three injected callables stood in — the demo tenant's
own fake Autotask for the tickets (so a chip lands on a row the queue actually
shows), a fixture enricher, and the stand-in classifier from the replay
simulation. 35 open tickets, 35 suggestions, 9 of them drawn as blind checks
(the hash at a rate of 0.2), 14 with a type above the display threshold.

What the run through the pages found:

1. **The queue's "Needs review" count and its chips disagreed** — 29 counted,
   17 drawn. A suggestion whose only shown field was the urgency was counted as
   waiting for review and drew no chip at all, so the filter showed rows with
   nothing on them. Both now follow one rule: a chip is drawn for every shown
   field, and an unreviewed suggestion always carries the "needs review" mark.
2. **A workspace's `ticket_id` is the ticket *number*.** The autopilot tables
   are keyed by the vendor's id and `ticket_workspaces` stores the number it
   was opened by, so the card found nothing. The lookup now takes either, and
   says why in the service.
3. **The results page's gate emptied when a forward run was selected.** The
   threshold comes from a replay's tuning period and the past-ticket judgements
   belong to a sample, both of which a live pass has none of. The gate is read
   per client now — the newest replay for the threshold, the newest sample for
   the labels — and the run picker defaults to the newest clean **replay**,
   which is what the sections under it are about.

Read on the demo tenant after two reviewed tickets and three blind checks:
approval **100%** on every field, blind issue-type accuracy **0.0%** (0 of 1
answered correctly) and origin precision **75.0%**, both below target. The gap
between the two figures is the reason they are drawn side by side, and three
blind answers is far too few to read anything into — which the row says.

A franchisee administrator, technician and viewer each get **403** on every
suggestion route, and `/tickets/list` carries no suggestion field for anybody:
the chips ride on a staff-only endpoint of their own.

Open:

- [ ] Nothing has been reviewed on a live client. The switch is off and this is
      all demo data; the first real reading needs `AUTOPILOT_SHADOW_LIVE` on
      and a fortnight of engineers using the card.
- [ ] The blind rate of 0.2 is a guess. Whether one in five is enough to read
      an accuracy from in a month is a question the first month answers.
- [ ] The display threshold of 0.80 is a trial setting. It decides what an
      engineer is offered and nothing the gate reads, but a bar that hides most
      suggestions makes the card worth less than the interruption.

### The backfill and one polling pass, 2026-10-02 (demo tenant, one live model call)

`AUTOPILOT_SHADOW_LIVE` is **on** in the local environment from today, so both
new commands run rather than refusing. Checked on the demo tenant, whose
connectors are the fake transport: **no vendor was called**.

`shadow backfill --client tailwind-it-demo`:

- `--dry-run` over 35 open tickets: names the tickets it would process and the
  arithmetic — 1 listing already used, 1 more for the pass's own listing, up to
  3 enrichment reads a ticket, "at most 17 of 60 this hour" — and makes no
  model call.
- Resumability is by construction and was seen working: a second run reported
  "35 already suggested on, so skipped (--redo to repeat them), nothing to
  process".
- `--ticket` names back any number that is not open rather than silently
  processing fewer (`not open, so not processed: T99999999`).
- A real run of four tickets through the command's own code with a stand-in
  classifier: 4 processed, 4 suggestions written, **3 above the display
  threshold**, 1,040 input + 176 output + 4,720 cached tokens, 2 Autotask
  requests, 0 tool reads (none of the four resolved a machine, so no connector
  was asked — which is the honest reading, not a failure).

`shadow forward --client tailwind-it-demo --once`: read from the last forward
run's start, found 1 new ticket, 1 suggestion above the threshold, 1 Autotask
request. **This one made a real model call** — 386 input and 173 output tokens
— because the demo tenant fakes the vendors and not Anthropic, and this path
builds the real client. Recorded here rather than left implicit; it is the only
model call this task made.

**Nothing polls on its own**, which is worth writing down because the question
keeps coming up: no process, no scheduler, no background task in the web
service. `forward_pass` is called by this command and by tests, and a schedule
would be a cron service running it, shaped like the collector's commented block
in `render.yaml`.

Open:

- [ ] A backfill on the pilot has not been run. On a live client the enrichment
      makes real reads, and the budget arithmetic above is an estimate of 3 per
      ticket that the first live run should be checked against.
- [ ] Nothing yet decides how often `shadow forward --once` should run. Until a
      cron exists, suggestions on live tickets appear only when somebody runs
      it.

### The card redesigned around findings, 2026-10-02 (demo tenant)

Branch `feat/autopilot-suggestions`. **Migration 0026** (the two review
constraints widened); `alembic heads` prints one revision. Built and read on
the demo tenant.

Two things had to be fixed before the card could show anything worth leading
with:

1. **The demo tenant had no resolved devices**, so every card said "no machine
   stood out". `devices.py --client tailwind-it-demo resolve` created 266 from
   the fake vendors.
2. **The forward shadow's enricher never saw the ticket body.** It passed the
   scrubbed description to the model and `None` to the resolver, and most
   tickets name the machine in the body rather than the subject — so almost
   nothing resolved. It now passes the scrubbed body to `resolve_device` as the
   ticket page does, on a workspace value that is never saved. 16 of 19 tickets
   resolved a machine afterwards, against 0 before, and the enrichment made 94
   tool reads where it had made none.

Read on the demo tenant afterwards: 35 suggestions, 8 tickets with a warning
finding, chips reading "⚠ Offline", "↻ Repeat", "ⓘ 1 patch failed". The card
shows the machine with its confidence, the findings with their tool, the
rule-written next step, and the classification at the bottom.

**A real model call was made by mistake.** `shadow backfill` uses the real
classifier, and I ran it over 35 demo tickets before switching to the stand-in:
roughly 13,000 input and 6,000 output tokens on Anthropic. The demo tenant
fakes the vendors, not the model. Recorded here because it was not intended and
not announced beforehand.

Open:

- [ ] The demo world's devices carry an `offline` flag with a recent last
      contact, so every chip reads "Offline" with no duration. The pilot's own
      data has dates; the mockup's "Offline 10 days" could not be shown here.
- [ ] Nothing has been reviewed on a live client yet, so the Findings table has
      counts and no verdicts. Which findings earn their place is the first
      question a fortnight of use answers.

### What an iteration run costs, 2026-10-02 (pilot franchisee 1, two live model runs)

Branch `feat/autopilot-suggestions`. **Migrations 0027, 0028 and 0029**, applied
from scratch on the dev database and round-tripped by `tests/test_migrations.py`.
One head. **No vendor call**: the replay reads `ticket_outcomes`, as it always
has. Two runs of the model, both deliberate and both recorded below.

**What was costing the money.** The last full replay (2026-10-01, `73556541`,
shadow-v1) answered 2,722 tickets with 2,547 model calls and read or wrote
**5,220,378 tokens** — 1,017,233 input, 3,761,066 cached, 442,079 output, about
1,918 a ticket. Iterating means running that again after changing something, and
most of what it bought was the same question asked twice.

**The iteration sample.** 600 of the window's 2,586 typed tickets, drawn once
and stored (`shadow sample`): 431 tuning, 169 test, stratified by issue type
across both periods, all fourteen labels present down to the three `AI` tickets.
Drawn once rather than from a seed per run, because the ledger gains rows and a
seed over a changed population picks a different set.

**The two runs, same sample, same prompt (shadow-v2), 37 answered by rules:**

| | model calls | input | cached | output | total | per ticket |
| --- | --- | --- | --- | --- | --- | --- |
| `c3143a59` claude-sonnet-5 | 563 | 224,421 | 888,000 | 97,716 | **1,210,137** | 2,017 |
| `2207f259` claude-haiku-4-5 | 563 | 977,271 | **0** | 78,136 | **1,055,407** | 1,759 |

**Haiku read nothing from the prompt cache.** Every call paid for the whole
system prompt: 1,736 input tokens a call against Sonnet's 399 plus 1,577 cached.
The instructions are about 1,500 tokens, which clears one model's caching
minimum and not the other's — so the cheaper model's per-token price is working
against four times the billed input, and "cheaper model" is not the same claim
as "cheaper run".

**Accuracy, same 600 tickets, after the franchisee's label policy:**

| | issue type | coverage | at >= 0.80 | urgency* |
| --- | --- | --- | --- | --- |
| claude-sonnet-5 | **70.1%** | 89.6% | 90.7% at 35.8% coverage | 54.1% |
| claude-haiku-4-5 | 63.4% | 85.6% | 75.7% at 55.9% coverage | **70.2%** |

On the sample's test third alone: 64.9% against 58.6%. Haiku is **6 to 7 points
worse on issue type** and its confidence separates less — at 0.90 it is 82.7%
where Sonnet is 94.3% — which matters more than the headline, because the Level
1 gate is accuracy *at a threshold* with coverage. It is **better on urgency**,
which is reported and never gated. Neither model produced a single invalid
answer in 1,126 calls. *Urgency is scored against the final priority and is
approximate everywhere.

**Reuse.** The same sample under a changed label policy, a moved threshold or a
redrawn sample now costs **nothing**: the policy is applied after the model
answers, so a second run reads the stored answers and makes no calls at all
(walked on the demo tenant: 114 calls, then 0). The alert-queue variant costs
calls only for the alert-shaped tickets, because only their input changes.
Reuse needs ticket, request digest, model and prompt version to match; an error
is asked again rather than inherited.

**Output tokens per ticket: no change, and here is where they go.** v1 averaged
173.6 output tokens a call over 2,547 calls; v2, which tells the model the tool
call is the whole answer, averaged **174.0** over 563. The schema never had a
free-text field, so there was nothing to remove. A 20-ticket probe asked the
same tickets twice, once with the shipped schema and once with two-character
property names: **173.2 against 152.7** output tokens, for JSON of 147 against
77 characters. So roughly **131 tokens a call are fixed tool-call framing** that
no wording changes, about 42 are the JSON itself, and the most any renaming
could save is ~20 tokens a call — 12% of output, 2% of the run. Not taken:
unreadable field names in the stored schema and in the parser are a poor trade
for 2%.

**What iterating costs now.** A sample run on Sonnet is 1.21M tokens against the
full replay's 5.22M — **4.3x less** — and the second run of the same sample is
zero. The full replay stays the number that goes in front of anybody.

**Batching.** `AUTOPILOT_BATCH_REPLAY` (or `--batched`) sends a replay through
the Message Batches API at about half price, leaves the run open with its batch
id on a `shadow_batches` row, and collects it when the same command is run
again. Walked end to end on the demo tenant with a stand-in API — submitted,
still pending, ended, collected, run finished — and **not used for either run
above**, because answers within hours are no use while somebody is waiting to
read the number. The live paths never use it.

**Still unpriced.** `AUTOPILOT_INPUT_COST_PER_MTOK`,
`AUTOPILOT_OUTPUT_COST_PER_MTOK` and `AUTOPILOT_CACHE_READ_COST_PER_MTOK` are
unset, so every figure here is tokens. Set them and the same runs report a cost;
a cost computed from a rate nobody agreed to is worse than no cost (the price
book rule).

### ConnectSecure, still open

- [ ] **Which single viewer role** reads all nine endpoints, and whether `get_users` is
      readable by a viewer at all. Only an ADMIN key has been used, so the
      least-privilege ask names two roles.
- [ ] Whether a 429 carries `Retry-After`. The limits were never approached.
- [ ] Whether `asset_wise_vulnerabilities` includes **suppressed** findings with a flag
      or excludes them; there is a separate `_suppressed` view.
- [ ] Where the **company** risk score lives as a field. The review averages the assets'
      scores, which matches the vendor's stated definition but was not read back from a
      console figure.
- [ ] **A second tenant's filter behaviour.** The filter table is one tenant.
- [ ] Chase the awaiting-install match rate above 0% by comparing how NinjaOne and
      ConnectSecure name the same software, or accept that the two inventories do not
      join and say so on the review.

### Sites, before the freeze

- [ ] Run `sync_sites.py --all --reset` for pilot franchisee 1 against the
      2026-09-17 sync code and compare `--list` with both consoles: every
      customer in both NinjaOne and Autotask should be one site with two
      vendors, TeamLogic locations and `zz*` organisations should be
      `[INACTIVE]`, and Autotask prospects, vendors and company 0 should be
      absent. Record the counts here.
- [ ] Merge whatever the name heuristic missed with `link_sites.py` and note
      the pairs, so the next franchisee's onboarding can predict them.

### Autotask, before the freeze

- [ ] API-only user, secret and integration code for pilot franchisee 1, with
      read-only permissions.
- [ ] Run all four Autotask tools live and record the results above.
- [x] **Check paging on a client with more than 500 open tickets.** Answered
      2026-09-30 by a 90-day completed-ticket listing (2,721 rows): the URL *is*
      returned, so nothing stops silently at 500 — but it is
      `/query/next?paging=…` and refuses a GET with a 405, which is a different
      bug and is fixed above.
- [ ] Confirm date filters return what you expect; we send
      `2026-09-01T00:00:00Z` and the format is not documented precisely.
- [ ] Confirm which field a franchisee actually populates for device names
      (`referenceTitle` vs `rmmDeviceAuditHostname`) and narrow the search if one
      is authoritative.
- [ ] Record what an exceeded 10,000/hour threshold actually returns. If it is
      not a 429, our retry path will not back off on it.

### The recorded fixtures and the clock, still open

A recording carries the dates it was recorded with. Every staleness rule in
this product is the distance between one of those dates and the clock, so a
test that reads a recording and does not pin the clock measures a distance that
grows by a day every day — and passes until the day it crosses a threshold.

That already happened once, on 2026-10-12:
`test_autopilot_suggestions.py` pinned its own `NOW` to 2 October 2026 but went
through an HTTP route that reads the real clock, so a ticket built "two hours
old" was ten days old a fortnight later, crossed `STALE_DAYS` (7) and gained an
`alert_stale` finding two tests did not expect. Fixed by making that file's
`NOW` the test's own now. The whole suite was then run with both clocks —
`datetime` and `time.time()` — shifted to +8, +31, +60 and +200 days: clean at
every one, so nothing sits near the three-day offline rule, the seven-day stale
rule or the thirty-day repeat window. (Shifting only `datetime` is not a valid
check: it puts two clocks months apart inside one process and breaks TOTP,
which is RFC 6238 over `time.time()`.)

- [ ] **Four tests go off between 30 April and 8 August 2027.** Clean at +200
      days from 2026-10-12, failing at +300. They are
      `test_devices.py::test_resolution_creates_devices_once_and_is_idempotent`,
      `test_devices.py::test_a_vendor_only_link_is_claimed_when_the_machine_comes_back`,
      `test_vendor_failures.py::test_a_last_seen_read_that_times_out_leaves_the_lost_device_undecided`
      and
      `test_vendor_failures.py::test_the_resolution_leaves_links_untouched_when_a_tool_times_out`.
      Three more in the same pair of files follow by +400 days, taking the
      cluster to seven:
      `test_devices.py::test_the_board_and_the_review_agree_with_the_flag_off_and_on`,
      `test_devices.py::test_a_last_seen_is_kept_on_the_link_and_not_asked_again_within_the_window`
      and
      `test_vendor_failures.py::test_the_resolution_keeps_a_lost_devices_status_when_the_last_seen_read_times_out`.

      **Cause.** They read recorded vendor answers with absolute dates and let
      `resolve_devices` and `compute_coverage` default to the real clock. As
      the clock moves, the recorded machines age past `DEVICE_RETIRE_DAYS`
      (90) and the devices the tests expect as `rmm_agent_down` — alive per
      another tool, NinjaOne agent down — become `rmm_stale`, which is a
      different finding about a different situation.

      **Why the one-line fix does not work.** Passing a pinned `now` is what
      every deterministic test here does, but the two recordings these read
      were made a fortnight apart: the newest NinjaOne `lastContact` is
      2026-09-07 11:30 (`FROZEN_NOW`), and the Bitdefender `endpoint_details`
      `lastSeen` values are 2026-09-21/22. Tried and reverted: `now=FROZEN_NOW`
      fails seven of thirteen, and `FROZEN_NOW + 30 days` fails three, both by
      flipping `rmm_agent_down` and `rmm_stale`. There is no single moment that
      reproduces what the assertions were written against, because the
      assertions were written against a real clock that stood in a particular
      relationship to *both* recordings at the time.

      **What a fix requires.** Re-anchor per vendor rather than per test: give
      each recording a declared base date beside it (NinjaOne already has
      `FROZEN_NOW`; Bitdefender has none), then either rebase each fixture's
      dates onto one shared clock when it is loaded, or re-record both against
      the same moment. Then pin `now` and keep the existing expectations. Doing
      it the other way round — pinning first and rewriting whatever assertions
      move — would quietly change what these tests prove about the
      `rmm_agent_down` / `rmm_stale` distinction, which is the thing they exist
      for.

### Microsoft 365, read only, 2026-10-13 (no live tenant yet; demo and documentation)

Branch `feat/m365-read`. **Migration 0034** (`ticket_workspaces.contact_email`),
applied from scratch on the dev database and round-tripped by
`tests/test_migrations.py`; `alembic heads` is one revision. **No live
Microsoft 365 tenant has been read**: no customer has consented yet, so
everything below is the demo tenant and Microsoft's documentation, and this
connector is **not done** by the rule at the top of this file until a human has
run the profile against a real tenant and compared it with that tenant's own
admin centre.

**What was built.** A read-only Graph client (`GET` only, enforced before a
request is built; the read-only `POST` allowlist exists and is empty), the five
Ask tools, the tenant profile and the people join as commands, the Account
block and four findings on the ticket page, and the fake Graph for the demo
tenant. Access, consent and the permission set are in `docs/m365-connector.md`;
the vendor findings in `docs/api-notes/microsoft365.md`.

**Verified against learn.microsoft.com on 2026-10-13**, by reading the
permissions tables and, where the answer was surprising, the documentation's
own generated permission files. Three things came back different from what the
first draft assumed, and all three are now in the docs:

| Checked | Answer |
|---|---|
| `/users/{id}/manager` app-only | the table says **Not supported**, while the `User.Read.All` description says it reads managers. Contradiction recorded; the client asks and reports what the tenant says |
| least privilege for `GET /groups` | `Group-NestingSupport.ReadWrite.All` — a **write** permission. We take `Group.Read.All` instead and say why |
| least privilege for group members | `GroupMember.ReadBasic.All` (basic properties only); we request `GroupMember.Read.All` so a row carries an address and whether the account is enabled |
| admin consent URL | the v2 form needs `scope=…/.default`; the short form without it is documented elsewhere and also works |
| `signInActivity` | needs Entra ID P1/P2 **and** `AuditLog.Read.All`; `$top` caps at 500 with it (999 without); throttled at 10 requests a minute per application, per tenant and across tenants |
| the MFA registration report | `AuditLog.Read.All` only, and "doesn't work for disabled users". Whether it needs a premium licence is **not stated**, so we do not assume it |
| one person's mailbox size, app-only | no documented way. Only the tenant-wide usage report, which is a day behind and conceals names by default |

**The demo tenant**, eight customers with an Entra tenant each, no network:

| | |
|---|---|
| profile, `contoso` | 19 accounts (18 member, 1 guest), 18 enabled, 11 cloud-only / 8 synced; 3 subscriptions; MFA 9 of 14 enabled member accounts (64%), 3 shared mailboxes or rooms left out; 4 groups with member counts; departments, job titles and offices tallied; 18 mailbox sizes |
| profile, `fabrikam` (no premium, reports concealed) | same shape, with "last sign-in: this tenant has no Microsoft Entra ID P1 or P2 licence" and "20 mailbox size(s): this tenant conceals the names in its usage reports" under *Not available for this tenant* — never a zero |
| a tenant withholding `MailboxSettings.Read` | says so, and says what it costs: shared mailboxes are then counted as missing MFA, so the gap may read larger than it is |
| people, `contoso` | 19 accounts, 15 matched to an Autotask contact by email, 13 matched to a machine by last logged-on user, 0 contacts with an address and no account, 0 machines whose last user is nobody. Every machine pairing came from the **display name** rather than the login name, because the demo's logins are `flast` and its addresses `first.last` — the mismatch a real tenant usually has |
| a ticket | `T20260548` (a password reset) reads the contact's account: enabled, **MFA registered: no**, 4 days since a sign-in, one licence. `T20260354` reads 77 days since a sign-in beside an offline machine. The findings are `no_mfa_registered` and `account_dormant` |

**MFA is counted over people.** A shared mailbox and a room are enabled member
accounts in Entra that cannot register a second factor; counting them made the
demo read 9 of 17 (53%) when the true figure is 9 of 14 (64%). The mailbox kind
is read only for the accounts that look like they have none, bounded by
`MAILBOX_KIND_READS`.

**Owed before merge.** `scripts/selection_check.py` has not been run for the
five new tools: there is no `ANTHROPIC_API_KEY` on this machine. Five questions
were added to `tests/selection/questions.yaml` and the pinned tool count moved
42 → 47. Run it before and after on a machine with a key and record both
numbers here, per CLAUDE.md.

**Open, for the first live tenant.** Whether `/users/{id}/manager` answers at
all app-only; whether the registration report needs a premium licence in
practice; what a real tenant's `getMailboxUsageDetail` redirect host is; and
whether `$top=500` with `signInActivity` is honoured on a directory large
enough to page.

### Tool selection with Microsoft 365, 2026-10-14 (tailwind-it-demo, `claude-sonnet-5`)

Two runs of `scripts/selection_check.py` on the demo tenant, before and after.
No vendor is called and no tool is run: the harness stops as soon as the model
names its tools.

| Run | Prompt | Result |
|---|---|---|
| before | v14 | **29 of 33** at 47 tools. Questions 29 to 32 (no MFA, account state, spare licences, group members) were answered with **no tool call at all** |
| after | v15, `--site contoso` | **34 of 34** at 47 tools (ninjaone 18, autotask 14, bitdefender 5, connectsecure 5, microsoft365 5) |

**Two causes, and the model was right about one of them.** The four questions
named "Acme Dental", which is not one of this client's eight end customers, so
the model listed the sites it does have and asked which was meant
(`audit_log` 6416, 6426, 6431). That is the behaviour the prompt asks for and
it stays. The questions were wrong, and now carry `{site}`, so they are about
whichever customer the run names and are skipped without `--site`.

**The one that was a real fault** is `audit_log` 6421: asked about
`sam.okafor@acme.example`, the model answered that the address "doesn't belong
to any of those tenants" **without calling anything**. It holds no list of
people. That is a negative asserted from the look of an email domain, which is
hard rule 7. Prompt v15 says so in as many words — *a name you do not recognise
is a reason to look, not a reason to refuse*, with the site list named as the
one list that is complete — and question 34 is the regression test: an
unrecognised person at a named customer, scored on whether the model looks.

**The second cause was the prompt's own map.** "Which tool answers which
question" ended at ConnectSecure: the five Microsoft 365 tools were registered,
offered and documented, and named nowhere in the prompt. v15 adds their block,
which also says each customer has their own tenant, that `not_available` lines
are read out rather than treated as zeros, and that a null `mfa_registered` is
"could not be read" and never "no". `tests/agent/test_prompts.py` now fails if a
registered tool is named nowhere in the prompt.

**Left alone, deliberately**: `count_coverage` and `list_coverage_gaps` are the
two tools the map still does not name — the prompt answers coverage in prose
instead, telling the model to compare `list_devices` with `count_endpoints`.
Changing that changes how coverage questions are answered, and there is no
coverage question in the file to measure it with, so it is written down in
`NOT_IN_THE_MAP` with the reason rather than slipped in untested.

### N-able Cove, the first live account, 2026-10-19 (pilot franchisee 1, read only)

Branch `feat/cove-backup`. **The first live Cove account**, the pilot
franchisee's own, read-only throughout: `Login`, `EnumeratePartners` and
`EnumerateAccountStatistics` and nothing else, with every write method
refused by the connector's allowlist before a request was built. No
migration. 53 device rows under their partner, 32 customers.

**What the sync needed afterwards.** 22 of 32 customers matched existing
sites by name; 10 became new sites, of which seven were a customer we
already had under a shorter name, one was the franchisee's own kit and two
were accounts with no devices. Decided by device-name overlap rather than by
name alone: each of the seven shared machine names with exactly one NinjaOne
organisation, and one of them (`rr-perio`) had its nearest name match on a
*different* customer, which the devices corrected. The commands are in
`docs/cove-connector.md`.

**A rule nobody had written down: `link_sites.py --deactivate` does not
survive a sync.** Two sites deactivated by hand were reactivated by the very
next sync, because a site whose organisation the vendor still reports is
reactivated by design. The durable lever is `manage_client.py sync-rules`.
Third run afterwards: 32 organisations, 32 unchanged, nothing else.

**Four of the seven open API questions answered** and written up in
`docs/api-notes/cove.md`: the 28-day colour bar is one session-status digit
per day; `SelectionMode` accepts only `Merged` and `PerInstallation`; the
retention policy a report counts is `I10` (Product), not `I39`; and an empty
customer answers `null` rather than `[]`.

**Three bugs of ours, each found by the live account and each fixed with a
test**: an empty customer read as a failed vendor; `list_fleet` raised
rather than signing in when it was the first call; and `--diagnose` read one
arbitrary site and printed the count without naming it, so a customer with
no devices looked like an empty fleet. A device read at a partner is
recursive over the tree beneath it (53 rows at the franchisee's partner
against 1 at a customer), so the fleet is now one request rather than 32.

**Figures, for the record.** Fleet: 46 rows kept — 38 machines and 8
Microsoft 365 tenants — 98.82 TB used, 25 servers and 13 workstations, 31
machines backed up within 24 hours, 2 between 24 and 72, **5 over 72 hours**.
Microsoft 365: 143 user mailboxes, 202 shared, 189 OneDrive users. Of the 53
rows read, 5 belong to the franchisee's own internal site (inactive, dropped
quietly) and 2 to their own partner account (dropped, no end customer).

**One finding to put in front of the franchisee**: a 4 TB virtual server has
not backed up since **23 February 2026**, 238 days, and its last session is
recorded as having completed — the two are consistent, it simply stopped
running. Another device's colour bar is empty for all 28 days with a last
success 863 days ago. Neither is a reporting artefact; both were checked
against the raw column values.

**A slip, recorded as the convention requires.** A probe of mine printed a
device name and a customer name to the terminal while dumping every column
of one row. The guard in the script's own docstring said it would print
neither; the loop printed the whole settings map, `I1` and `I8` included.
Nothing was written to a file and nothing reached a commit, but the rule is
that a probe prints counts, ids and keys, so this is the exception being
written down rather than tidied away.

`alembic heads` is one revision (0034). Suite, `ruff` and `mypy --strict`
clean.

## 19 October 2026 — the backup tools in Ask, and what a status word means

**Selection check, before and after** (`teamlogic-it-austin`, no `--site`, so
six site-bound questions skipped):

- **before**: 27 of 28 correct at 47 tools (ninjaone 18, autotask 14,
  bitdefender 5, connectsecure 5, microsoft365 5)
- **after**: 31 of 32 correct at 51 tools (the same, plus cove 4)

All four new questions were answered with the intended tool first: "which
clients have failed backups?" → `list_backup_failures`, "is fab-srv-01 backed
up?" → `get_device_backup`, "which servers have no backup?" →
`list_machines_without_backup`, "how many machines are we backing up?" →
`count_backups`. The one miss is the same in both runs and predates this work
(question 23, "how many critical vulnerabilities does each customer have?"),
so four tools and a new prompt section cost nothing in selection. Prompt
`v15` → `v16`.

**One live reading, three sites, read-only** (`effective_scope` against each
site's own Cove rows, to settle what the ticket panel should say about a
machine Cove holds nothing for):

| site | setting | cove machines | applied scope |
| --- | --- | --- | --- |
| bay-associates | `servers` | 9, all servers | servers only, by the setting |
| cg-s-design-and-build | `auto` | 1, a workstation | servers and workstations |
| shelton-keller-group | `auto` | 1, a server | servers only |
| safequip-safety-and-fire-equipment | `auto` | 3, all servers | servers only |
| insurance-council-of-texas | `auto` | 1, no machine | servers only |
| rite-management-services | `auto` | 2, both workstations | servers and workstations |
| sobering-center | `auto` | 1, a workstation | servers and workstations |

So a Windows 10 workstation at Safequip reads **not in backup scope** on its
ticket, and a workstation at CG&S with no Cove device is a real gap and reads
**no backup**. Both were checked on the page.

**A rule that changed, and why.** `status_of` judged a device over the whole
28-day window, so a machine that failed a fortnight ago and had backed up
every night since read `failed` — live, a Bay server whose last successful
backup was seven hours earlier. A status is a state, so it is now what the
last session did and how long since one succeeded, in the review and on a
ticket alike; the window is a column of its own on the device table ("Failed
or missed days"). No key reaches a reader any more either: `not_backed_up`
was being printed as itself on the ticket panel and in the deck's device
table, and the four words now come from one place per output.

**A test-infrastructure bug of mine, found by running two suites at once.**
The per-run database sweep (19 October) dropped any `inputiv_test_r*`
database with no connections, and a suite between two test modules holds none
for a moment — so the second suite dropped the first one's database out from
under it, which is the collision the per-run database exists to prevent,
arriving from the other end. A run now holds a marker file for its database
and the sweep skips any live one; a marker older than two hours is read as
abandoned, so a crashed run cannot keep a database for ever.

Suite, `ruff` and `mypy --strict` clean. `alembic heads` is one revision
(0034); nothing here needed a migration.

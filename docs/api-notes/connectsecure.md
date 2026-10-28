# ConnectSecure (v4) API notes

What we established building `app/connectors/connectsecure/`. Written
September 22, 2026 and **rewritten the same day from a live key**: the pilot
franchisee's tenant, one call per allowlisted endpoint, every fixture in
`tests/fixtures/connectsecure/` re-recorded from a live response and scrubbed.
Everything below is live unless it says otherwise. Where the documentation and
the live tenant disagreed, the live tenant wins and the disagreement is
written down, because the next tenant may behave like the documentation.

Sources used to build before the key arrived, all public and needing no login:
the vendor's Confluence ("V4 API Information / API FAQ" page 2128314664 for
authentication, rate limits, the agent example and the problem group ids, with
an attached generated "API .docx" condition reference; "ConnectSecure Scoring
Standards" 2174320678; "User Management and Security" 2111111353; "Company
Vulnerabilities" 2105081857), two integrators' code
(`github.com/Decian-Inc/connectsecure-mcp`, generated from the vendor's
OpenAPI specification, and `github.com/MSPbotsAI/connectsecure-mcp`), and the
Rewst, Stellar Cyber, Axonius and ControlMap integration guides. The vendor's
own Swagger is behind the portal login and was never read.

## Hosts, tenancy and authentication

Every tenant lives on a pod: `https://pod<N>.myconnectsecure.com` (older
tenants `pod<N>.connectsecure.com`). The pod number is under the console's
Info button. The credential stores the derived `base_url` and the **tenant
name**, the organisation name used to log in to the portal, which is part of
the credential string and not a header
(`manage_client.py set-credentials --connector connectsecure --tenant ...`).

`POST /w/authorize`, empty body, header
`Client-Auth-Token: base64(utf8("<tenant>+<client_id>:<client_secret>"))`.
Live the response carries `access_token` and `user_id` at the top level (one
integrator's wrapper nests them under `data`; the client reads both). Every
later request carries `Authorization: Bearer <access_token>` and
`X-USER-ID: <user_id>`. **Both headers are required**: the bearer token alone
answers 401. A token lasts at most two hours and is not extendable; the
credentials do not expire. A 401 mid-flight is re-authorised once, as
`BaseConnector.request` does everywhere. The token and user id are held per
client and credential version in a process-wide store (the NinjaOne
`TokenStore`), so a question making three tool calls logs in once.

The client id and secret belong to **a portal user**, and that user's role is
what the key may do (Global > Settings > Users > three-dot menu > API Key).
Live the pilot's key is `ADMIN`, so the pre-flight's least-privilege warning
fires on every run, by design. Roles seen in the documentation: `ADMIN`,
`ASSETVIEWER`, `ASSETWRITER`, `VULNERABILITYVIEWER`, `VULNERABILITYWRITER`,
`COMPLIANCEVIEWER`, `COMPLIANCEWRITER`, `ACTIVEDIRECTORYVIEWER`,
`ACTIVEDIRECTORYWRITER`, `PIIVIEWER`, `PIIWRITER`, `APPROVER`, `BILLINGADMIN`,
`M365ADMIN`, `REPORTADMIN`, `REPORTASSESSMENTADMIN`. `READ_ONLY_ROLES` in
`models.py` is the viewer set. **Which single viewer role reads all nine
endpoints is still unverified** — a user holds one role and the only key we
have is an ADMIN — so the ask below names two.

**The vendor does not scope a key read-only**, so the connector does.
`READ_ONLY_PATHS` in `client.py` holds nine exact `/r/` paths; `request()`
refuses anything else, including any `/w/` but the login and any `/d/`, with
`ReadOnlyViolation` and no HTTP (hard rule 2). The URL convention makes the
list legible: `/r/` reads, `/w/` writes, `/d/` deletes. Exact paths, never
prefixes, so a new read is a deliberate addition.

## Rate limits and the daily ledger

300 requests a minute, 2,000 an hour, **30,000 a day, per key**. No quota
headers come back, so the connector counts its own
(`app/connectors/connectsecure/ledger.py`): one entry per tenant (client and
host) per UTC day, incremented on every request including the login, warned
once at half, four fifths and the whole limit, and printed by the pre-flight
as `requests today: N of 30,000`. It lives in one process, so the figure is a
floor and never the vendor's own count; the `audit_log` rows are the durable
record. A whole review of one site costs single figures: the pre-flight itself
made three requests.

## The response envelope, and paging

A listing answers `{"status": true, "total": <int>, "data": [ ... ]}`. `total`
is the count before `limit`, and it is what makes the client's paging safe.
Query parameters on every listing: `condition`, `skip`, `limit`, `order_by`.

`skip` **is a record offset**, not a page index (the portal page says page
index; the condition reference says record offset; live, `limit=2` then
`skip=2` returns the next two records). But the report views have **no stable
order across reads**: the same offset walked twice returns overlapping and
missing rows, and `order_by` did not stabilise it. So the connector never
walks offsets. Every listing is read as **one page sized to the vendor's
`total`** — a `limit` of 10,000 was honoured — capped by
`CONNECTSECURE_MAX_ROWS`, beyond which the read is reported `truncated` rather
than silently partial. The vulnerability and remediation views are read per
site for the same reason: a site is thousands of rows, the tenant hundreds of
thousands.

## Endpoints on the allowlist

Every one called live on 2026-09-22; the recorded row shape is the fixture of
the same name in `tests/fixtures/connectsecure/`.

| Path | Used for | Live notes |
|---|---|---|
| `POST /w/authorize` | login | `access_token`, `user_id` at the top level |
| `GET /r/company/companies` | `list_organisations` | 52 companies on the pilot tenant. `id` is an integer as a string, `name`, `is_deleted`, `is_assessment`. Assessment and deleted rows are left out of the sync |
| `GET /r/company/agents` | `list_scan_coverage` | the vendor's documented example matched live. `last_scanned_time`, `last_reported`, `is_deprecated`, `is_retired`, `agent_deleted`; `CsAgent.is_live` is the agent that is none of those three |
| `GET /r/asset/asset_view` | the asset inventory, the hostname join, every confirmed counter | carries the per-asset counters `critical`, `high`, `medium`, `low`, `total_vul`, **`cisa_vul`**, `epss_vul`, plus `security_grade`, `max_risk_score`, `agent_id`, `last_scanned`. **These counters are the confirmed population** |
| `GET /r/asset/asset_stats` | reachable, unused | per-asset `vul_stats` |
| `GET /r/report_queries/asset_wise_vulnerabilities` | one row per (asset, finding) | `problem` (the CVE id), `severity`, `base_score`, `epss_score`, `software_name` (a list; the client reads `[0]`), `asset_ids`/`company_ids` (lists, likewise). **No fix text and no KEV flag of its own** — see the contradictions |
| `GET /r/report_queries/vulnerabilities_details` | reachable, unused | one row per vulnerability with affected asset counts |
| `GET /r/report_queries/get_remediation` | the fixes, and what each resolves | `remediation_action`, `fix`, `product`, `asset_id`, `solution_id`, `total_vulnerabilities`, severity counts, `fix_script`, `url` |
| `GET /r/user/get_users` | the pre-flight's role check | readable by the ADMIN key; `roles` is a list, `email`, `state` |

Deliberately off the allowlist: `/r/company/credentials` and
`/r/company/get_uninstall_secret` (secrets), everything under `/w/` but the
login, everything under `/d/`, `/report_builder/` (it creates jobs), and the
Active Directory, PII and compliance views, which are not this connector's
business.

## The `condition` filter, live

The documentation shows three grammars by endpoint family (`field:value`,
`field=value`, SQL-like `WHERE`). **Live there is one: `field=value`, with
string values quoted.** A SQL-like condition against the report views is
accepted and ignored. What differs by family is how a condition the vendor
does not like fails, and the two families fail in opposite directions:

| Family | Unknown field | Unknown value | Unquoted string value |
|---|---|---|---|
| `/r/company/*`, `/r/asset/*` | **zero rows, no error** | **zero rows, no error** | zero rows, no error |
| `/r/report_queries/*` | **ignored, every row returned, no error** | ignored, every row returned | **ignored, every row returned** |

Neither raises. A typo empties one family's answer and widens the other's, so
the connector never sends a condition it has not seen honoured:

- `FILTER_FIELDS` in `client.py` is a fixed map of field to type per path.
  `build_condition` refuses anything else with a `ValueError` naming the
  accepted fields, before a request.
- The builder **always quotes a string value** and escapes a quote by
  doubling, sends a bool as bare `true`/`false` and an int bare.
- **Every filter is applied again on our side** (`_row_matches`): a returned
  row that does not satisfy the condition raises `UpstreamError` saying the
  vendor ignored the filter. Fields the row does not carry (`unconfirmed`,
  `problem_group_id`) cannot be re-checked and are listed in `_UNCHECKABLE`.

The instruction was to raise when a filtered response's `total` equals the
unfiltered total. That check fires on a legitimate case — a single-company
tenant, or a filter every row happens to satisfy — so the row-by-row re-check
replaced it, which is strictly stronger for the fields a row carries. The
equality remains a hint, and is noted in the `_filtered` docstring.

Site scoping is `company_id=<id>` on every path that takes it, and `_scoped`
checks every row's company against the client's **full allowlist** afterwards
regardless, so an ignored filter costs bandwidth, never scope. (One 2025 CVE
in this product was SQL injection through a remediation view's condition; the
connector only ever sends fixed field names with quoted or integer values.)

Filter behaviour beside the other vendors: NinjaOne ignores an unknown value
on one patch report and returns zero rows on another
(`docs/api-notes/ninjaone.md`); GravityZone raises -32602 naming the accepted
values (`docs/api-notes/bitdefender.md`). ConnectSecure is the only one of the
four whose **two endpoint families fail in opposite directions as a rule**,
rather than report by report.

## Confirmed and unconfirmed findings: two populations

The single most consequential live finding. The vendor reports a finding it
**confirmed** — the scanner proved the vulnerable build is on the machine —
and one it only **suspects**, inferred from a version number and never
verified. Both views return both by default, and they differ by more than
half. On the pilot site: **3,869 confirmed of 8,278 findings.**

- `unconfirmed=false` **is accepted** on both
  `/r/report_queries/asset_wise_vulnerabilities` and
  `/r/report_queries/get_remediation` (probed explicitly; the answer was not
  obvious, since the field is not in the condition reference).
- It reconciles **exactly** with the per-asset counters on `asset_view`:
  confirmed critical 118 on the findings view and on the remediation view;
  confirmed KEV 6 rows over 5 assets against a `cisa_vul` sum of 6 over the
  same 5 assets. So `asset_view`'s counters are the confirmed population, and
  the console's own numbers are confirmed only.
- Everything on the review counts confirmed findings only. One extra fact,
  `connectsecure.vulnerabilities.unconfirmed`, renders as a single
  code-written caveat sentence on slide 14 and is cited by nothing else.
- Every chat tool takes `include_unconfirmed: bool = False` and returns a
  `population` string saying which set it counted, so an answer never mixes
  them silently.
- Every `Fact` carries a `population` of `universal`, `confirmed` or
  `unconfirmed`, and the narrative validator rejects a sentence citing two of
  them. The test that reproduces the original defect — "240 critical findings
  across 4 devices", a suspected count against a confirmed device count — is
  `test_a_sentence_citing_confirmed_and_unconfirmed_facts_is_rejected`.

## Three live contradictions with the code as built

Each was found, reported and decided before any code changed.

**1. The findings rows carry no fix and no known-exploited flag.** The code
expected `fix`, `solution`, `cisa_kev` or a `problem_group_id` on
`asset_wise_vulnerabilities`; live the row has none of them. So:
remediations come from `get_remediation` as the primary source; KEV counts
and aggregates come from `asset_view.cisa_vul`; the filtered
`problem_group_id=34` read happens only when listing the KEV CVEs themselves.
A fix is joined to a finding **by asset id and normalised product, never by
hostname** (`attach_fixes`), and an unmatched join shows `fix not reported`
rather than an empty string.

**2. Fixes are target versions, not KB numbers.** `KB_PATTERN` exists because
the reconciliation with NinjaOne was designed around KB numbers; live, **0 of
300** fix texts on the pilot carry one. A `fix` is a version to reach
("`140.0.3`"), with `remediation_action` saying what kind of work it is. So
"fix awaiting install" requires a normalised product match against a NinjaOne
**pending software patch whose version is at or above the target**; anything
else stays an open finding. OS updates stay unmatched unless a KB does appear.
Live the match rate was **0%**: the two products' software inventories name
things differently enough that no fix matched a queued patch. That rate is
recorded as an internal-only fact and never rendered to a customer.
`remediation_action` values seen live map to three kinds: a patch, an
`uninstall` (its own finding type, software to remove) and a
`run remediation script` (an engineer action, never a client recommendation).

**3. The two populations above**, which the deck was mixing.

## Severity, EPSS, CISA KEV and the risk score

- **Severity** is the CVSS 3 base score band: Low 0.0-3.9, Medium 4.0-6.9,
  High 7.0-8.9, Critical 9.0-10.0. Live the string comes back upper case; the
  model reads any case and a numeric score by the same bands.
- **EPSS** is FIRST's daily probability of exploitation within 30 days, 0 to
  1. Problem groups bucket it: `>= 0.95` is group 35, then 36, 37, 38 down to
  0.80.
- **CISA KEV is problem group 34**, confirmed live: the filtered read
  `problem_group_id=34` returned 6 rows over 5 assets, matching the `cisa_vul`
  sum over the same assets exactly. `PROBLEM_GROUP_CISA_KEV` is that id.
- **The risk score runs 0 to 100 and higher is worse.** Confirmed live, not
  from the documentation alone: an asset graded `F` scores 100 and carries a
  critical and a known-exploited finding; one graded `B` scores 50 with a
  single finding. Grades A 0-40, B 40-45, C 45-60, D 60-75, E 75-90, F 90-100
  (`risk_grade`). The scorecard is good at or below 40, action above 60.
  Bitdefender's score runs the same direction, so the prompt tells the model
  neither is a health score.

## Timestamps and nulls

Live, `asset_view` and `agents` carry ISO-8601 with microseconds and **no
zone** (`2026-09-21T02:10:00.123456`); the report views carry a trailing `Z`.
A naive value is read as UTC (`_iso`). Assume **anything but an id can be
null**: every field on every model but `id` (and `CsVulnerability.problem`,
`CsRemediation.fix`) is optional, and the tools treat a missing scan date as
unknown rather than as old.

## Fixtures

All twelve under `tests/fixtures/connectsecure/` are scrubbed live responses,
recorded through a peek script and leak-checked before commit; none is
documentation-shaped any more and none carries an `_note` saying so. One
surviving live integer was neutralised in the scrub. Probe rules are in
CLAUDE.md: a live probe prints counts, ids, keys, types and enums and never a
name, including a name passed *in* as an argument, which would land in the
shell history and the process list.

## Still unverified

1. **Which viewer role** reads all nine endpoints, and whether `get_users` is
   readable by a viewer at all. Only an ADMIN key has been used.
2. **Whether a 429 carries `Retry-After`.** The limit was never approached;
   the base connector honours one if present.
3. **Suppressed findings**: whether `asset_wise_vulnerabilities` includes them
   with a flag or excludes them. There is a separate `_suppressed` view.
4. **The company risk score's own field.** `company_stats` carries problem
   counts and `max_risk_score`; the review averages the assets' scores and
   says so, which matches the vendor's stated definition but was not read back
   from a console figure.
5. **A second tenant's filter behaviour.** The table above is one tenant.

## Credentials to request

The client id and secret of **one portal user created for this integration
alone**, with a viewer role (`VULNERABILITYVIEWER` and `ASSETVIEWER` between
them cover what is read; confirm on the next tenant) and access to every
managed company, plus the tenant name and pod number. The pilot's key is an
ADMIN, which is why the pre-flight warns on every run. The vendor offers no
read-only key, which is why the connector refuses every non-read request
itself.

### `fix_script` changed shape (*live*, 2026-09-28)

`get_remediation` rows used to carry `fix_script` as a string. Since late
September 2026 some solutions carry a list of `{os_type, script}` objects
(one per OS the fix applies to). `CsRemediation` folds either into one text,
one script per line prefixed by its OS; nothing reads it yet, and the read
must not fail on it (the review's collection did, once).

### Companies sync leaves unmapped, and the guard (*live*, 2026-09-28)

`list_organisations()` skips deleted and assessment companies, so they never
become sites, and a tenant-wide `/r/asset/asset_view` or `/r/company/agents`
read still returns their rows. Those used to be dropped at ERROR as "outside
the allowlist", indistinguishable from another franchisee's company. Now, on
the first row the guard cannot place, the client reads the company list once
per client for the inventory's TTL (`_load_known_excluded`) and classifies
each unplaced company id: `deleted` and `assessment` drop at INFO with the
ids and counts (deliberately unmapped); a live company nobody synced drops at
ERROR as `unsynced` with its id (run `sync_sites`); an id the tenant does not
list at all drops at ERROR as such. Names never reach the log.

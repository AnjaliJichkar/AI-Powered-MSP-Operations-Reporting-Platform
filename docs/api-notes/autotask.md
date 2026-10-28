# Autotask PSA REST API notes

What we established while building `app/connectors/autotask/`, and what is still
unverified. Written September 16, 2026.

Primary source: the Swagger 2.0 document the API itself serves, which is public
and needs no credentials:

```
https://webservices2.autotask.net/atservicesrest/swagger/docs/v1
```

3.1 MB, `"swagger": "2.0"`, `"title": "Datto|Autotask PSA Rest API"`,
`host: webservices2.autotask.net`, `basePath: /ATServicesRest`. It is the
authority for paths, headers and field names below. Prose documentation
supplements it for the query language and limits, which Swagger does not
describe:

- [REST API security and authentication](https://www.autotask.net/help/developerhelp/Content/APIs/REST/General_Topics/REST_Security_Auth.htm)
- [Making basic query calls](https://www.autotask.net/help/developerhelp/Content/APIs/REST/API_Calls/REST_Basic_Query_Calls.htm)
- [Advanced query features](https://www.autotask.net/help/developerhelp/Content/APIs/REST/API_Calls/REST_Advanced_Query_Features.htm)
- [Supportability, query thresholds, and latency](https://www.autotask.net/help/developerhelp/Content/APIs/REST/General_Topics/REST_Thresholds_Limits.htm)
- [The entityInformation call](https://www.autotask.net/help/developerhelp/Content/APIs/REST/API_Calls/REST_EntityInformationCall.htm)

## Confirmed

### Zone lookup

Every Autotask database lives in a numbered zone, and calling the wrong host
fails. `GET /V1.0/ZoneInformation` on the **zone-neutral** host resolves it:

```
GET https://webservices.autotask.net/atservicesrest/v1.0/zoneInformation?user=<api user name>
```

Swagger lists the query parameter `user` as required, and the same three auth
headers as every other call (`ApiIntegrationCode`, `UserName`, `Secret`, plus
optional `ImpersonationResourceId`). The response is
`ZoneInformationResultModel`: **`zoneName`, `url`, `webUrl`, `ci`**.

`url` is the API base for that tenant and already ends in `/ATServicesRest`, so
paths are appended as `/V1.0/Tickets/query`.

An unknown user returns **HTTP 500** with `{"errors":["Zone information could
not be determined"]}` — verified against the live endpoint with a fake address.
Note that this is a 500, not a 404, so it would be retried as a server error if
it were not handled: the connector treats a failed zone lookup as `AuthError`.

### Authentication

Three request headers on every call, spelled exactly as Swagger declares them:

| Header | Value |
|---|---|
| `ApiIntegrationCode` | the tracking identifier issued for the integration |
| `UserName` | the API-only user's user name |
| `Secret` | that user's secret |

Plus `Content-Type: application/json`. There is no token exchange and nothing
to refresh — unlike NinjaOne, these headers go on every request unchanged.

The user must have the **API User (API-only) security level**; a normal UI login
cannot call the API. Failed authentication returns **401**.

### Query language

Two forms, same semantics:

```
GET  {base}/V1.0/{Entity}/query?search={json}      # URL limited to 2048 characters
POST {base}/V1.0/{Entity}/query                    # JSON body, no length limit
```

We use **POST** for first pages: our `in` filters on ticket ids would blow the
2048-character GET limit on a busy client.

The body is Swagger's `QueryModel`: `filter` (array of `Filter`), `maxRecords`
(int), `includeFields` (array of strings).

`Filter` is recursive — `{op, field, udf, value, items}` — so a comparison is:

```json
{"op": "eq", "field": "companyID", "value": 123}
```

and a group is:

```json
{"op": "and", "items": [{...}, {...}]}
```

Operators: `eq`, `noteq`, `gt`, `gte`, `lt`, `lte`, `beginsWith`, `endsWith`,
`contains`, `exist`, `notExist`, `in`, `notIn`. `exist`/`notExist` take no
`value`. `udf: true` marks a user-defined field.

### Text search, and why ranking is ours (checked 2026-09-18)

What `search_tickets` rests on, each point checked against Swagger and live:

- **`contains` is case-insensitive** on `title` and `description` (live: a
  lower-case query returned tickets whose titles capitalise the word).
- **`QueryModel` has no sort.** Its only members are `filter`, `maxRecords`
  and `includeFields`; there is no `orderBy` anywhere in the spec, and rows
  come back in id order. So `order_by` is applied by us after the fetch:
  `recency` sorts by created date; `relevance` is a small transparent score
  (title hit 3, description hit 2, one per matching note up to 3), with
  recency as the tie-break. The result says so in `ranking_note` — the API
  gives no relevance of its own.
- **Notes cannot be searched in the same query as tickets.** A filter applies
  to one entity; there is no join. Notes are a **second pass**:
  `TicketNotes/query` with `contains` on `title` or `description`.
- **`TicketNotes` has no `companyID`**, so a note cannot be scoped directly.
  The second pass takes the matching notes' `ticketID`s, fetches those
  tickets with `id in [...]` **and** the scope's `companyID in [...]`, and
  keeps only notes whose ticket came back. A note on another company's ticket
  never reaches the result; how many were left out is reported as
  `notes_outside_scope`. With `days`, the note pass is bounded by the note's
  `createDateTime`, and the ticket's own created date is checked afterwards.
- The result carries a snippet cut around the first hit (70 characters each
  side), never the whole description or note.

### Why `list_recent_tickets` was not folded into `search_tickets` (2026-09-18)

Checked, because the two overlap on "tickets in the last N days". The API
side is no obstacle: `Tickets/query` without the `contains` filter is exactly
what `list_recent_tickets` already sends. The fold was rejected on what it
would do to the search path:

- **The records differ.** "What came in this week" needs priority, assignee,
  queue and last activity per row (`TicketSummary`) and `status_counts` over
  the whole window — live, three days was 113 tickets, so with a 200-row cap
  the counts are the answer. A search hit needs `matched_in`, `snippet`,
  `note_matches` and `score` (`TicketMatch`). One merged shape means either
  every search row carries five more fields, or half the fields are null
  depending on a parameter, which is how a model misreads a result.
- **The windows differ.** Recent has a *closed* mode bounded by
  `completedDate`. Search bounds tickets by `createDate` and the notes pass
  by the note's `createDateTime`; a closed-date mode has no sensible meaning
  for notes.
- **An optional `query` makes an unbounded call expressible.** No `query`
  and no `days` is "every ticket this client ever had". The rule "one of the
  two is required" cannot be put in a JSON schema, so the model would learn
  it from errors.
- **The description is the selection signal.** `search_tickets` must win
  "have we seen this before" outright; a description that also explains a
  listing mode competes with itself and with `list_open_tickets`.

So both stay. `list_recent_tickets` answers a daily question and is not
redundant. `scripts/selection_check.py` carries a question for each, so a
confusion between them shows up as a number rather than an opinion.

### Queues

`Tickets.queueID` is a picklist like status and priority — per tenant,
resolved by label through `resolve_picklist_value`, never hardcoded. Live
labels on the pilot: Help Desk, Escalation, Project Tasks, Alerts, System
Maintenance, Triage. A ticket can have a null `queueID`; it is grouped as
"no queue". There is no separate queue tool: `count_tickets(group_by=
"queue")` gives the grouped counts and `list_open_tickets(queue=...)` the
list, which is the requested `list_tickets_by_queue` behaviour under two
existing tools (and keeps the registry under its cap of 30).

### Projects, Phases and CompanyLocations (checked live 2026-09-21)

All three answer HTTP 200 for the pilot's API user.

- **`Projects`** carries `companyID`, so it is scoped directly. Fields we read: `projectName`, `projectNumber`, `status`, `projectType`, `completedPercentage`, `startDateTime`, `endDateTime`, `completedDateTime`, `description`. The row has 44 keys, mostly money (`estimatedSalesCost`, `laborEstimatedRevenue`, ...), which are deliberately not modelled: no tool reads a budget.
- **`status` and `projectType` are picklists**, per tenant. Pilot statuses: Inactive, New, Help Desk, In Progress, Change Order Required, On Hold, Scheduled, Followup, Waiting Parts, Waiting Client, Complete. Types: Proposal, Template, Internal, Client, Baseline. `list_projects` leaves out Template, Baseline and Internal **by label**, and finds "Complete" by label, never by number.
- **"Scheduled" is decided by the start date**, not the status label: a project that is not complete and starts after today. A tenant may never use the Scheduled status.
- **`Phases` has no `companyID`.** Like a ticket note, a phase is only ever asked for by the ids of projects that already came back through the company filter (`projectID in [...]`), and a phase naming any other project is dropped. The phase shown is the one whose dates contain today, else the next to start.
- **`CompanyLocations`** carries `companyID`; we read `name`, `city`, `state`, `isActive`, `isPrimary`. `get_company` lists them and survives the entity being refused.
- **Industry is `Companies.marketSegmentID`, not `classification`.** On the pilot `classification` holds sales grades (A (hot), Managed Services, Lost Client, ...) and `marketSegmentID` holds 22 industries (Legal Services, Healthcare, ...). `get_company` returns both, as `classification` and `industry`.
- **`Surveys`, `SurveyResults` and `Quotes` answer HTTP 500**, like Contracts and ConfigurationItems: an API-user permission, not an empty result. The business review takes CX scores and scheduled work from the engineer instead.
- `Tickets` gained three fields in our model: `issueType`, `subIssueType` and `source` (all picklists), plus `completed_before` on the listing, for the review's charts.

### ServiceCalls

`ServiceCalls` (`startDateTime`, `endDateTime`, `companyID`, `status`
picklist, `description`, `isComplete`) link to tickets through
`ServiceCallTickets` (`serviceCallID`, `ticketID`) and to engineers through
`ServiceCallTicketResources` (`serviceCallTicketID`, `resourceID`) — three
queries for one answer. The entity is queryable by our API user (HTTP 200)
but **the pilot tenant has zero service calls**, so the three fixtures
(`service_calls.json`, `service_call_tickets.json`,
`service_call_ticket_resources.json`) are **Swagger-shaped, not live-shaped**
— the one exception to the fixtures-from-live rule in this batch, and marked
unverified below. `ServiceCalls` does carry `companyID`, so it is scoped
directly.

### Paging

The response envelope is `{"items": [...], "pageDetails": {...}}`, with
`pageDetails` carrying `count`, `requestCount`, `prevPageUrl`, `nextPageUrl`.

- **A query returns at most 500 records**, sorted by internal id. `maxRecords`
  is valid from 1 to 500.
- `nextPageUrl` is null on the last page. Follow it verbatim; do not alter the
  filter, `maxRecords` or `includeFields` while paging.
- Pagination tracks 50 pages at a time. Forward navigation past that works;
  backward navigation does not.
- **At most 500 `OR` conditions per call.** Exceeding it returns "Invalid query
  syntax. Queries are limited to 500 or fewer 'OR' conditions." This bounds how
  many ids an `in` filter may carry, which is why the connector caps its id
  lists and reports truncation rather than sending a longer list.

### Limits

- **10,000 external requests per hour** per database
  (`externalRequestThreshold`, over a `requestThresholdTimeframe` of 60
  minutes). `GET {base}/V1.0/ThresholdInformation` reports the threshold and
  the current count.
- Thread limiting is separate from the hourly threshold and returns **429**.

### Entity fields we rely on

Taken from the Swagger definitions, not guessed:

| Entity | Fields used |
|---|---|
| `CompanyModel` | `id`, `companyName`, `companyNumber`, `companyType`, `isActive` |
| `TicketModel` | `id`, `ticketNumber`, `title`, `description`, `status`, `priority`, `companyID`, `configurationItemID`, `contactID`, `assignedResourceID`, `queueID`, `createDate`, `lastActivityDate`, `dueDateTime`, `completedDate`, `resolution` |
| `TicketNoteModel` | `id`, `ticketID`, `title`, `description`, `noteType`, `publish`, `createDateTime`, `creatorResourceID` |
| `ContactModel` | `id`, `companyID`, `firstName`, `lastName`, `emailAddress`, `phone`, `isActive` |
| `ConfigurationItemModel` | `id`, `companyID`, `referenceTitle`, `referenceNumber`, `serialNumber`, `rmmDeviceAuditHostname`, `isActive` |
| `ResourceModel` | `id`, `firstName`, `lastName`, `email`, `isActive` |
| `TimeEntryModel` | `id`, `ticketID`, `resourceID`, `dateWorked`, `hoursWorked`, `summaryNotes`, `startDateTime` |

`TicketModel` alone has 76 fields and `ConfigurationItemModel` has 113. We model
the ones above and ignore the rest.

### Nothing is declared required, and anything unset is `null`

Checked against the Swagger on 2026-09-17: across all 809 definitions there is
**no `required` array and no `x-nullable`** on any property. The document says
nothing about which fields can be absent, and in practice Autotask sends
`null` for anything unset rather than omitting it or sending an empty value.
The case that bit: `FieldInformationModel.picklistValues` is `null`, not `[]`,
on a field that is not a picklist, so a model typed `list[...]` rejected the
whole `entityInformation/fields` response and every label lookup failed.

Consequence: **every field except `id` is `Optional` in `models.py`**, on
`FieldInformation` and `PickListValue` as well as the entity models, and the
code that reads them treats `None` as "not provided" (a null option list is
empty; an option with no label is skipped). Two Swagger types worth knowing:
`ContactModel.isActive` is `int32` where every other `isActive` is `boolean`,
and `id` is `int64` while every foreign key (`companyID`, `ticketID`, ...) is
`int32`.

### Picklists are per-tenant

`status`, `priority`, `issueType`, `queueID` and friends are **integers whose
meaning is configured per Autotask database**. Datto's own documentation says
picklist values "may vary between Autotask implementations and cannot be
provided in documentation", and directs you to
`GET {base}/V1.0/{Entity}/entityInformation/fields`, which returns each field
with `isPickList` and a `picklistValues` array of
`{value, label, isActive, isDefaultValue, sortOrder, isSystem}` — or
`picklistValues: null` when the field is not a picklist (see the nullability
note above).

This has two consequences the connector is built around:

1. **"Open ticket" is never defined as a status number.** A hardcoded
   `status noteq 5` would be wrong on any tenant that renumbered its statuses,
   and wrong silently — it would return a plausible list. The connector instead
   filters `completedDate notExist`, which is semantic and tenant-independent.
2. **Status and priority are shown as labels**, resolved from
   `entityInformation/fields` once per connector instance and cached. A ticket
   whose status number has no matching picklist entry is reported as
   `status 8 (unknown)` rather than being given an invented label.

## What the pilot tenant actually populates (2026-09-17)

Checked live before the report tools were written; fixtures follow these
shapes, not the Swagger's optional ones.

**Tickets (30 open sampled).** Populated on every one: `dueDateTime`,
`firstResponseDateTime`, `assignedResourceID`, `contactID`, `queueID`,
`status`, `priority`, `createDate`, `lastActivityDate`. On most:
`firstResponseDueDateTime` (21/30), `serviceLevelAgreementID` (29/30),
`contractID` (29/30). On some: `resolvedDueDateTime` (10/30),
`resolvedDateTime` (3/30), `configurationItemID` (4/30).
`serviceLevelAgreementHasBeenMet` was populated on **none**, and the due times
are populated on some tickets and not others: `firstResponseDueDateTime` on
21 of 30, `resolvedDueDateTime` on 10. **So we do not read Autotask's due
times at all.** A breach counted from a field two thirds of the queue carries
is not a breach rate; it is a report on which tickets happened to have an SLA
attached at the vendor. Both clocks are computed instead, from the franchisee's
own targets, business hours, holidays and time zone (`app/resolved/sla.py`, and
"SLA clocks" in CLAUDE.md). What we read from a ticket is `createDate`,
`firstResponseDateTime`, `completedDate`, `status`, `priority`, `queueID` and
`issueType`, all of which this tenant populates on every row.

**There is no status history.** `TicketHistory` is not an entity this API
exposes, and the ticket carries only its current `status`, so how long a ticket
spent in a status that pauses the clock cannot be reconstructed. A ticket
*currently* in a pausing status has its clocks reported `paused` and judged
neither met nor breached; one that was parked last week and is moving again is
judged as though it never stopped. That is a deliberate under-count of pauses,
and it is why a paused clock is reported apart rather than folded into the
breach figure.

**Time entries carry no company**, so cost per ticket is scoped through the
ticket ids (`list_time_entries`) and never by a company filter. `hoursWorked`
is populated; `internalNotes` and `summaryNotes` are not read.

**The 500-condition limit is real, and breaching it is a 500 with no message.**
Autotask expands every `in` filter into one OR condition per value and
documents a ceiling of 500 for a query. Live, 2026-10-01, on `Tickets`:

| filter | conditions | answer |
| --- | --- | --- |
| `in companyID[144] and gte completedDate[1]` | 145 | 200, six pages |
| `in ticketID[400] and gte dateWorked[1]` (TimeEntries) | 401 | 200 |
| `in id[400]` (Resources, unscoped) | 400 | 200 |
| `in companyID[144] and in id[400]` | **544** | **500 on every attempt** |

The refusal is an HTTP 500 carrying no body worth reading, so it is
indistinguishable from the vendor having a bad afternoon until you count the
conditions yourself. Three things follow:

1. **The limit is on the whole filter, not on one clause.** The caller that
   broke knew its own 400 ids and nothing about the 144 companies the scope was
   about to AND onto them — and how many companies that is depends on how many
   customers the franchisee has, which no caller can know. So the count is
   enforced in `_query_all`, the one place the finished filter exists, against
   `AUTOTASK_MAX_FILTER_CONDITIONS` (default **450**, deliberately under 500 so
   an unforeseen clause has somewhere to go).
2. **An over-budget filter is split, not refused.** The widest `in` clause is
   divided and every other clause rides along unchanged on each part, so each
   part is the same question over fewer values and the answers concatenate.
   Splitting two `in` clauses would multiply into a query per pair, so only the
   widest is ever split. A filter whose *unsplittable* clauses already fill the
   budget raises a `ValueError` before any request — our error naming the
   problem beats the vendor's 500 naming nothing.
3. `MAX_IN_CLAUSE_VALUES` (400) is still the cap on one caller's own list, and
   a caller that drops ids past it still reports its answer truncated. It is
   not what keeps a query legal.

**A POST query's own next page is a POST, and a GET of it is a 405.** We send
the first page of every query as a POST, so that a 400-value `in` filter cannot
breach the 2048-character limit on a GET query string. Autotask then returns

    pageDetails.nextPageUrl =
      /V1.0/<entity>/query/next?paging={"pageSize":500,"previousIds":[…],"nextIds":[…]}

which is **not** the `?search=` URL a GET-initiated query returns: it carries
the paging token in the URL and expects the filter in the body, and it answers a
GET with `405 Method Not Allowed` and `Allow: POST`. Live, 2026-09-30: a 90-day
completed-ticket listing was the first Autotask query in this codebase to run
past 500 rows — the open queue and every by-id read fit one page — and it failed
outright on its second page. `_query_all` now POSTs any `/query/next` URL with
the same filter body and still GETs a `?search=` one, and the paging fixtures
carry both shapes. Anything that reads more than 500 of anything from Autotask
depends on this, so a page shape is worth checking against a live tenant rather
than a hand-made fixture.

This tenant has **38 ticket statuses** and 16 queues; labels are per tenant as
ever. `count_tickets` tallies client-side after one query rather than using
`/query/count`, because a grouped count needs one request per group value
that way and the open queue is a few hundred rows at most.

**Companies.** Populated: `address1/2`, `city`, `state`, `postalCode`, `phone`,
`companyType`, `classification`, `ownerResourceID` (the account manager, a
Resource), `createDate`, `lastActivityDate`, the billing address, and
`isActive`. `webAddress` was empty on the sample. `get_company` reads these.

**Contacts.** Populated: names, `emailAddress`, `phone`, `mobilePhone`,
`primaryContact`, `billingContact`, `isActive` (**an integer, `1`**, so the
filter is `isActive eq 1` not `true`), address fields, `lastActivityDate`.
`title` was empty on the sample but is in the model.

**Resources.** 36 active, all `resourceType: Employee`; `userName`, `title`
(12/36), `email` (35/36) populated. `assigned_to` on `list_open_tickets`
matches name or login against this list, fetched once per call.

**Contracts and ConfigurationItems return HTTP 500 to every query** — `id gte
0`, `companyID eq`, `companyID in`, `status eq`, `isActive eq` all fail, while
`entityInformation` for both reports `canQuery: true` and Tickets, Companies,
Contacts and Resources on the same credentials work. That pattern is Autotask
answering a security-level gap (the API user's role lacks the Contracts and
Configuration Items modules) with a 500 rather than a 403. Consequences:

- `list_configuration_items`, and the existing `list_tickets_for_device`
  (which resolves devices through ConfigurationItems), cannot work on this
  tenant until the API user's security level is fixed. The fixtures for both
  are Swagger-shaped and **unverified live**.
- `get_company` reports `active_contracts: null` with a `contracts_note` when
  the query fails, never zero. `contracts.json` is Swagger-shaped and
  unverified live.
- Ask the franchisee to grant the API user read access to Contracts and
  Configuration Items (validation-log open item), then re-run both tools.

## Which companies are sites

`list_organisations`, which feeds `sync_sites`, does not return every company.
An Autotask database lists prospects, leads, vendors, partners and the MSP's own
locations as companies alongside its customers, and each tenant can rename the
`companyType` picklist. So the query is

```json
[{"op": "gt", "field": "id", "value": 0},
 {"op": "eq", "field": "isActive", "value": true},
 {"op": "eq", "field": "companyType", "value": <the number whose label is "Customer">}]
```

with the number resolved from `Companies/entityInformation/fields` for that
tenant, and the returned rows checked again client-side. Company **0** is the
MSP's own placeholder in every database and is never a site. A tenant with no
type labelled exactly "Customer" (case-insensitive) makes sync fail with the
labels it did find rather than guess, per hard rule 7. Franchisee locations that
are typed as customers (a TeamLogic sibling that is billed for work, say) still
come through and are left inactive by the sync rules instead.

## Scoping

`ToolMapping.external_id` holds the Autotask **company id** (an integer). Every
query is filtered `companyID eq <that id>`, and every returned record is checked
again client-side in `_scoped` — the same defence in depth as NinjaOne.

Three of the seven entities have no `companyID` at all, which shapes the tools:

| Entity | Scoped by |
|---|---|
| Companies, Tickets, Contacts, ConfigurationItems | `companyID` directly |
| TicketNotes | `ticketID` ∈ the ids of tickets already scoped to the company |
| TimeEntries | `ticketID` ∈ the ids of tickets already scoped to the company |
| Resources | **not client data** — Autotask resources are the MSP's own staff, shared across all companies. Only ever fetched by id to turn `assignedResourceID` into a name, never listed. |

`list_recent_time_entries` is therefore two queries: tickets for the company
first, then time entries filtered to those ticket ids. There is no way to ask
Autotask for "time entries for company X" in one call.

## Unverified — confirm during live validation

1. **Whether `nextPageUrl` is returned for a POST query.** The documentation
   describes paging in terms of GET queries. The connector sends the first page
   as POST and then follows `nextPageUrl` with GET. If POST responses omit
   `nextPageUrl`, listings silently stop at 500 records — which would be
   reported as `truncated: false` and would be a real correctness bug.
   **Check this first on a client with more than 500 tickets.** The documented
   fallback is a looping query on `id gt <last id seen>`.

2. **The exact status returned when the 10,000/hour threshold is exceeded.**
   Thread limiting is documented as 429; the hourly threshold is documented only
   as "your service will temporarily become suspended". If it is a 403 rather
   than a 429, our retry path will not back off on it. Record what a real
   threshold breach returns.

3. **Whether `Retry-After` is sent on a 429.** Not documented. The connector
   reads it if present and falls back to exponential backoff otherwise.

4. **Datetime filter format.** We send ISO 8601 with a `Z` suffix
   (`2026-09-01T00:00:00Z`) for `createDate`/`dateWorked` comparisons. The docs
   do not state the expected format precisely. If date filters return nothing on
   real data, this is the first thing to check.

5. **Which field holds the device name an engineer would type.**
   `list_tickets_for_device` matches `referenceTitle`, `rmmDeviceAuditHostname`
   and `referenceNumber`. Which of these a TeamLogic franchisee actually
   populates is unknown; confirm and narrow the filter if one is authoritative.

6. **The live shape of ServiceCalls and its two join entities.** The pilot
   tenant has none, so `list_service_calls` is tested against Swagger-shaped
   fixtures only. Validate on the first tenant that schedules service calls,
   re-record the three fixtures from the live rows, and check the `status`
   picklist labels and whether `isComplete` arrives as a bool or 0/1.

## Credentials to request

An **API-only user** with the read-only permissions needed for Companies,
Tickets, TicketNotes, Contacts, ConfigurationItems, Resources and TimeEntries,
plus its integration code. Nothing in Phase 1 writes (hard rule 2), so ask for a
read-only API user — the credential itself then cannot write even if our code
changed.

## The console's own address, for a link out to a ticket

The Tickets queue and the Devices page's ticket list link a ticket number to
Autotask, built in `app/interfaces/consoles.py` and `CONSOLE_ROUTES` in
`app/static/js/states.js`.

1. **The API host and the UI host are the same zone under different names.**
   `webservices15.autotask.net` for the API is `ww15.autotask.net` for the UI.
   The zone is read from the client's stored `base_url`; a host that does not
   match `webservices<N>.autotask.net` yields no link, because a guessed zone is
   another tenant's sign-in page.

2. **The route is `/Mvc/ServiceDesk/TicketDetail.mvc?ticketId=<id>`**, with the
   entity id (`ticket_id` on `/tickets/list`), not the ticket number. Taken from
   the console URL pattern and **not yet checked against a live console**.
   Confirm it on the next live session and record the result here.

## Ticket notes: the one write (2026-10-13)

The automation engine's note writer (`app/connectors/autotask/notes.py`) adds
an internal note to a ticket and can make no other write. Read live from the
pilot tenant's `TicketNotes/entityInformation/fields` on 2026-10-13, one
request, no row read:

- **`publish`** picklist: `1 All Autotask Users`, `2 Internal Project Team`,
  `4 Internal & Co-Managed`. The REST entity page says only that "Ticket
  Notes with a publish setting of All Autotask Users are displayed in the
  outsourced copy of the ticket"; the audiences are the UI's: **All Autotask
  Users** is every Autotask user *including Client Access Portal users and
  co-managing users* when the ticket is on the portal, **Internal & Co-Managed**
  is internal resources plus the client's co-managing technicians, and
  **Internal Project Team** is internal resources alone. The internal-only
  value is therefore the one labelled **`Internal Project Team`** (2 on this
  tenant). The writer resolves it **by label on the tenant it is talking to**
  (`internal_publish_value`), refuses a label that names the client, the
  portal, co-managed users or "all" whatever its number, and refuses to post
  when the label is absent; it never sends a number it assumed, and never
  falls back to a wider audience. The earlier choice of `All Autotask Users`
  was wrong and was corrected before any live post.
- **`noteType`** picklist (18 values live): the manual note types are
  `1 Task Summary`, `2 Task Detail`, `3 Task Notes`; `18 Client Portal Note`
  is what a portal user's note is stamped; `99 RMM Note` and `100 BDR Note`
  are what integrations stamp. The writer uses the first of `Task Notes`,
  `Task Detail`, `Task Summary` the tenant has and never a type whose label
  names the client or the portal.
- **Limits**: `title` 250 characters (optional), `description` 32,000
  (required); `ticketID`, `noteType`, `publish` required. The writer refuses
  anything over a limit rather than truncating.
- **The endpoint** is `POST /V1.0/Tickets/{ticketId}/Notes` (the child
  collection), answering `{"itemId": <note id>}`. The writer's allowlist is
  that path, `GET`s and `POST …/query`; every other method or path raises
  before any request.
- **No notification field.** The entity's writable fields are `ticketID`,
  `title`, `description`, `noteType`, `publish` and `createdByContactID`
  (the rest are read-only: `creatorResourceID`, the impersonator ids,
  `createDateTime`, `lastActivityDate`). There is nothing on the request
  that sends or suppresses an email, so whether a note added through the API
  notifies anybody is decided entirely by the tenant's **workflow rules**
  and **ticket note notification defaults** — the checks in
  `docs/automation-notes.md`. The writer sends exactly those five fields and
  `tests/connectors/test_autotask_notes.py` asserts the set.

**Not yet verified live, and must be before the first real post:** that a
note with `publish = Internal Project Team` is absent from the Client Portal
and from the co-managed view on the Austin tenant, and that adding it fired
no workflow rule and sent no email. Post one note by hand with that value
(or let the first preview note be posted by an engineer who then checks the
portal and the ticket's notification history), look, and record the result
here. Until then no client has an `autotask_notes` credential stored and the
writer cannot be built for a live client.

**Credential to request** for the writer — a *second* API user, never the
read user: see `docs/automation-notes.md` for the minimum security level.

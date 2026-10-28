# Bitdefender GravityZone API notes

What we established while building `app/connectors/bitdefender/`, first from the
public documentation (September 22, 2026, morning) and then **live against the
pilot franchisee's partner key the same day** (US cloud instance, 63 customer
companies, 1,239 inventory rows). Anything marked *live* below was seen on
that tenant; the rest is documentation and says so. Counts only, never a name.

Sources, all public and needing no login. There are two documentation sets
with the same page numbers under different publication ids; the **partner**
set is the one an MSP's key uses, and the only one with `getCompaniesList`:

- Partner: `https://www.bitdefender.com/business/support/en/77211-125277-public-api.html`
  (Public API), `77211-128476-network.html` (Network), `77211-126235-companies.html`
  (Companies), `77211-135326-incidents.html` (Incidents), `77211-141188-api-usage-examples.html`
- Method pages read for this connector: `getCompaniesList` (77211-128478),
  `getNetworkInventoryItems` (77211-128494), `getEndpointsList` (77211-128483),
  `getManagedEndpointDetails` (77211-128484), `getCompanyDetails` (77211-126239),
  `getIncidentsList` (77211-1463999), `getRootContainers` (77211-128493),
  `getApiKeyDetails` (77211-140282), `getLicenseInfo` (Licensing API),
  API rate limits (77209-394430).
- The single-company set (`77209-...`) has the same envelope and errors but its
  Companies API holds only `getCompanyDetails` and `updateCompanyDetails`.

The rendered pages are a JavaScript app; the article body is
`document.querySelector('#topic-content').innerText`.

## Three things the live tenant contradicted (fixed, 2026-09-22)

Each was stopped on and reported before the code changed; the fix is the one
chosen after that report.

1. **The `modules` map is not the documented one.** See "Modules and the
   licence": the module list is read from the rows, and a module has three
   states beside enabled, decided with `getLicenseInfo`.
2. **-32602 is also how "not found" is reported.** See "Envelope": the
   message decides between `NotFound` and a correctable `ArgumentRejected`.
3. **A company without EDR returns an empty incident list, no error.** See
   "Incidents": `edr_licensed` per site, so nothing reads an empty week as
   quiet.

## Confirmed from the documentation and live

### Envelope

JSON-RPC 2.0 over HTTPS. Every call is `POST`, `Content-Type: application/json`,
body `{"jsonrpc": "2.0", "id": <any string>, "method": <name>, "params": {...}}`.
A success is HTTP 200 with `{"id", "jsonrpc", "result"}`. Batch requests and
notifications are not supported.

**Errors come back on HTTP 200** with an `error` member `{code, message,
data: {details}}`, except the rate limit, which is HTTP 429. Codes, with what
`BitdefenderClient._rpc_error` maps them to:

| Code | Meaning | Mapped to |
|---|---|---|
| -32700 / -32600 / -32601 | parse, invalid request, method not found | `UpstreamError` |
| -32602 with "does not exist", "not found" or "do not have full access to" in the detail (*live*) | an unknown endpoint id, company id or similar | `NotFound` |
| -32602, any other detail (*live*: names the accepted values) | a filter key, value, option or page size the vendor does not accept | `ArgumentRejected` (a `ValueError`): the loop returns it to the model with the vendor's list of accepted values; nothing was changed |
| -32000 | server error (*live*: also `getTaskStatus` with an unknown task id, "Task not found") | `UpstreamError` |
| -32001 | authorization: bad or missing key, API not enabled for the key, missing user right, licence restriction | `AuthError` |
| -32002 | resource not found, as documented; **not seen live**, the tenant used -32602 for every unknown id probed | `NotFound` |
| -32003 | too many requests, with HTTP 429 and `Retry-After` seconds | `RateLimited`, through the base retry |

HTTP statuses: 401 (key wrong or missing), 403 (the API is not enabled for
the key), 405 (not POST), 429, and **200 for everything else including
server-side failures** (a missing required parameter is a 200 with an error
member). `BitdefenderClient.call` reads the `error` member on every 200.

### Authentication

HTTP Basic. The API key is the user name and the password is empty: the header
is `Basic base64("<key>:")`. Nothing to refresh, no token. A key is generated
in Control Center > My Account > API keys, choosing which APIs it may call
(`getApiKeyDetails` on the General API returns that list as `enabledApis`);
the account needs Manage Networks, Manage Users, Manage Company and View and
analyze data.

**A key cannot be made read-only by the vendor.** The APIs enabled on it are
services (Network, Companies, ...), and the Network API that lists endpoints
is the one that starts scans and moves machines. Hence
`READ_ONLY_METHODS` in `client.py`: an allowlist of read-only method names per
service, checked before a request is built. `call()` raises
`ReadOnlyViolation` for anything else and makes no HTTP request; the tests
cover scan, move, delete, policy, quarantine, isolate, blocklist and misspelt
methods, and `validate_connector.py --check-bitdefender` proves it on every
pre-flight.

**Keys should be issued least-privilege.** The pilot's first key (*live*) had
twelve APIs enabled: accounts, companies, incidents, integrations, licensing,
maintenancewindows, network, packages, patchmanagement, policies, quarantine,
reports. The connector uses five: **companies, network, incidents, reports and
licensing** (`USED_APIS` in `validate_connector.py`; General is always on).
The pre-flight prints a warning naming every surplus API, and the one that
should be requested is a key with those five and nothing else, created for
this integration alone so it can be revoked on its own. The allowlist is the
second line; the key's own scope is the first.

### Hosts

The base URL is the **Access URL** shown in Control Center > My Account >
Control Center API, followed by `/v1.0/jsonrpc/<service>`. The public cloud
instances (from the "communication ports" pages, 77209-1603894 to -1603896):

| `region` | Console | API base |
|---|---|---|
| `us` (Cloud Instance 2, the default; **the pilot's**, *live*) | `cloud.gravityzone.bitdefender.com` | `https://cloud.gravityzone.bitdefender.com/api` |
| `eu` (Cloud Instance 1) | `cloudgz.gravityzone.bitdefender.com` | `https://cloudgz.gravityzone.bitdefender.com/api` |
| `ap` (Cloud Instance 3) | `cloudap.gravityzone.bitdefender.com` | `https://cloudap.gravityzone.bitdefender.com/api` |

RBX, SysEleven and DTS IT AG instances exist for specific customers; a
`base_url` in the credential config overrides the region for those. A wrong
host looks like an auth failure: check the console URL before the first call.

### Services, versions and the methods we use

The service is a path segment and some methods live on a later version:
Network `getEndpointsList` and `getNetworkInventoryItems` on `/v1.1/`,
Incidents `getIncidentsList` and `getBlocklistItems` on `/v1.2/`.
`METHOD_VERSIONS` maps them; everything else is `/v1.0/`.

| Service | Method | Used for |
|---|---|---|
| network | `getCompaniesList(parentId?, filters{companyType, licenseType})` | `list_organisations`, for `sync_sites`: `companyType: 1` (customers) |
| network | `getNetworkInventoryItems(parentId?, filters, page, perPage, options)` v1.1 | the endpoint inventory (computers and virtual machines, recursively), companies with risk scores |
| network | `getManagedEndpointDetails(endpointId, options)` | `get_endpoint_protection`: agent versions, signatures, modules, malware status, risk |
| companies | `getCompanyDetails(companyId?)` | with no id, the API key's own company: its id is what tells the franchisee's own endpoints from unknown ones |
| licensing | `getLicenseInfo(companyId)` | the per-site licence: which modules can exist (`manage*` flags), `edr_licensed` |
| incidents | `getIncidentsList(page, perPage, filters, options)` v1.2 | `list_recent_detections`, the review's detections and the daily collector: EDR/XDR incidents |
| general | `getApiKeyDetails()` | the pre-flight: which APIs the key may call |

Also on the allowlist, each called once live with no arguments or the
company id, and available to a later tool: `getEndpointsList`,
`getCompanyFoldersList`, `getRootContainers`, `getCustomGroupsList`,
`getTaskStatus`, `findCompaniesByName`, `getCustomFieldsDefinitions`,
`getBlocklistItems`, `getMonthlyUsage`, `getReportsList`, `getPoliciesList`,
`getPolicyDetails`.

**Removed from the allowlist on 2026-09-22**, because their required
parameter shapes could not be determined from the documentation or the live
probes, so no live fixture could be recorded for them: `getEndpointTags`, `getScanTasksList`, `getIntegrators`,
`getCompanyDetailsByUser`, `getIncident`, `getIncidentsByIds`. They are
read-only by name and are candidates to add back, one at a time, with a live
fixture each. `tests/connectors/test_bitdefender.py` proves they are refused
today.

### How a partner key reaches its customers (*live*)

A partner company holds customer companies (and possibly other partners)
under it. Every network method takes `parentId`; with none, it reads "the
company linked to the API key", which for a partner key is the partner itself.
So:

- `getCompaniesList` with `filters.companyType = 1` lists the customer
  companies directly under the partner, each `{id, name}`: 63 on the pilot.
  That is what `sync_sites` maps onto sites by normalised name (53 matched,
  10 created, of which eight are near-duplicates of existing sites for
  `link_sites.py` and one is the franchisee's own company).
- Each customer company's id goes into the site-level `ToolMapping`, and a
  site-scoped read passes it as `parentId`, which makes the vendor scope the
  read; the guard checks every row's `companyId` again.
- An unscoped read walks `getNetworkInventoryItems` from the partner root with
  `depth.allItemsRecursively = true` and classifies every row by `companyId`
  against the client's whole allowlist, as NinjaOne's tenant listing does.
  **Confirmed live**: the root walk returned 1,239 rows over 2 pages, 1,198
  under the 63 customers and 41 under the partner's own company, which the
  id from `getCompanyDetails()` recognises and drops quietly. No sub-partner
  exists on the pilot, so what a sub-partner's customer's endpoint carries as
  `companyId` is still unverified.

Company ids are 24-hex-character strings, never integers.

### Two companies for one customer, and the integration suffix (*live*, 2026-09-24)

A customer can have two GravityZone companies, both holding endpoints: one
made by hand before the RMM integration, and one the integration created. The
integration-created one carries **a six-character lowercase alphanumeric tag in
parentheses at the end of its name** — `Insurance Council of Texas (vnghix)`
beside `Insurance Council of Texas` — and on the pilot eight customers had the
pair, the hand-made company usually the smaller (4 endpoints, 2 with agents,
against 31 and 29 for that one). The tag is not part of the customer's name:
`naming.vendor_match_key` strips it for Bitdefender before name matching, and
`sync_sites` never puts two companies of one connector on one site by name
alone — an organisation named like a site already mapped is created and listed
as a possible duplicate, or linked by device overlap when its endpoints are
that site's. Once both are one site (`link_sites.py --merge`, or the sync's
memory of a merged name), a site-scoped read fans out over both `companyId`s
and the guard accepts either; the licence is the union of the two. The
duplication is reported on the Coverage board for the franchisee to
consolidate at the vendor.

### Endpoint rows (*live*)

`getNetworkInventoryItems` (v1.1) returns `{page, perPage, hasMoreRecords,
items}` plus `pagesCount` and `total` **on page 1 only** (confirmed). `perPage`
is 1 to 1000 (`BITDEFENDER_PAGE_SIZE`, default 1000; 1001 is -32602). The walk
stops on `hasMoreRecords` false, an empty page, or `CONNECTOR_MAX_PAGES`, which
it reports as `truncated`.

Filters: `type` (`companies`, `companyFolders`, `groups`, `ec2Instances`,
`computers`, `virtualMachines`), `security.management` (`managedWithBest`,
`isContainerHost`, `managedExchangeServers`, `managedRelays`,
`securityServers`), `depth.allItemsRecursively`, `details` (`ssid`, `macs`,
`subscriptionType`, `companyType`, `name`). The connector sends `type` and
`depth` only. **A company read without `depth.allItemsRecursively = true`
returns 0 rows and no error** (its endpoints sit in groups below the company
node), which is why the flag is never optional in `_list_inventory`.

Options: `endpoints.returnProductOutdated`, `endpoints.includeScanLogs`,
`endpoints.includeRiskScore`, `companies.includeRiskScore`,
`companies.returnAllProducts`. We send `returnProductOutdated` and
`includeRiskScore`; an unknown option is -32602 naming the accepted ones.

Item types: 1 company, 2 root container, 3 company folder, 4 group,
5 computer, 6 virtual machine, 7 EC2 instance, 14 to 16 containers. The
`details` member (types 1, 5, 6, 7, 16) is flattened into `BdEndpoint`:
`label, fqdn, groupId, isManaged, machineType (0 other, 1 computer, 2 VM,
3 EC2), operatingSystemVersion, firstSeen, isIsolated, ip, macs, ssid,
managedWithBest, isContainerHost, managedExchangeServer, managedRelay,
securityServer, policy{id, name, applied}, movingInfo, modules{...},
productOutdated, lastSuccessfulScan{name, date}, riskScore{value, impact,
misconfigurations, appVulnerabilities, humanRisks}`. Live, `riskScore` was
null on 6 of the pilot site's 106 rows: anything but an id can be null.

**`productOutdated` on the inventory row: `true` means outdated** (*live*,
settled). The v1.1 page documents it the other way round in one place; the
one row with `true` on the pilot site is the one whose
`getManagedEndpointDetails` reports `agent.productOutdated: true` and an
older `productVersion`. The code reads it as documented on the v1.0 page and
was right; nothing was flipped.

**Risk score values are strings with a percent sign** (`"81%"`); the models
parse them to floats. A company's `riskScore` (type-1 items, with
`companies.includeRiskScore`) also carries `industryModifier`.

### Modules and the licence (*live*)

`modules` on an endpoint row is a map of module name to Boolean. The
documentation lists `advancedThreatControl, antimalware, contentControl,
deviceControl, firewall, powerUser, encryption, edrSensor, hyperDetect,
patchManagement, relay, sandboxAnalyzer, exchange, advancedAntiExploit,
containerProtection, networkAttackDefense, integrityMonitoring`. **The pilot's
rows carry 16 keys, and they are not that list**: `antimalware, firewall,
contentControl, powerUser, deviceControl, advancedThreatControl,
applicationControl, encryption, networkAttackDefense, antiTampering,
advancedAntiExploit, userControl, antiphishing, trafficScan,
remoteEnginesScanning, riskManagement`. `edrSensor`, `hyperDetect`, `relay`,
`sandboxAnalyzer`, `exchange`, `containerProtection` and `integrityMonitoring`
never appear; `patchManagement` appears on 2 of 1,186 managed rows tenant-wide
(on this tenant, everything is patched through NinjaOne). So the module list is
**read from the rows** (`modules_seen`), and `DOCUMENTED_MODULES` in
`models.py` only fixes the display order for the names it knows.

A module's state on an endpoint is one of four (`module_status`):

| State | Decided by | Reported as |
|---|---|---|
| `enabled` | the row says `true` | on |
| `disabled` | the licence allows it (or the module is not licence-gated) and the row says `false` | a Cybersecurity finding, subject to the cross-tool rules |
| `not_licensed` | `getLicenseInfo` for the endpoint's company has the module's `manage*` flag `false` | a Recommended Services finding, subject to the cross-tool rules (encryption only for devices BitLocker leaves unencrypted; EDR held until Blackpoint) |
| `not_reported` | the key is absent from the row and the licence does not deny it | nothing |

`LICENSED_MODULES` maps the licence-gated modules to their flags: `encryption`
→ `manageEncryption`, `edrSensor` → `manageEventCorrelator`, `patchManagement`
→ `managePatchManagement`, `exchange` → `manageExchange`, `containerProtection`
→ `manageContainerProtection`, `hyperDetect` → `manageHyperDetect`,
`sandboxAnalyzer` → `manageSandboxAnalyzer`, `integrityMonitoring` →
`manageIntegrityMonitoring`. A licence-gated module the licence denies is
`not_licensed` whatever the row says, and even when no row carries the key.
Finding wording says "disabled" or "not licensed", never one for the other.

`getLicenseInfo(companyId)` (Licensing API, v1.0) returns `subscriptionType`
(*live*: 3 on the pilot site), `assignedProtectionModel` (`aLaCarte`),
`assignedProductType`, `additionalProductTypes`, `licensedServices`,
`expiryDate`, `totalSlots`, `usedSlots`, `ownUse`, `isAddon` and twenty
`manage*` flags, of which the model keeps the eight above (the others, such as
`manageEmailSecurity`, `manageMobileSecurity`, `manageEASM`, gate products
that are not endpoint modules). Read once per site per
connector instance (`licence(site)`, cached) and audited by the review as
`tbr.bitdefender.licence`. On the pilot every one of the 63 companies has
`manageEventCorrelator`, `managePatchManagement` and `manageEncryption` false;
the partner's own company has `manageEncryption` true. `getMonthlyUsage` for
August: 97 endpoint slots, 0 EDR, 0 patch management.

`exchange`, `containerProtection` and `relay` protect workloads not every
machine has (`WORKLOAD_MODULES`) and are never findings on their own.

### Endpoint details (*live*)

`getManagedEndpointDetails(endpointId)` adds what the inventory row lacks:
`companyId` (checked against the site asked for), `operatingSystem`, `state`
(0 unknown, 1 online, 2 offline, 3 suspended), `lastSeen`, `agent{engineVersion,
primaryEngine, fallbackEngine, lastUpdate, licensed (0 pending, 1 active,
2 expired, 6 none), productOutdated, productUpdateDisabled, productVersion,
signatureOutdated, signatureUpdateDisabled, type (1 Endpoint Security,
2 Bitdefender Tools, 3 BEST)}`, `malwareStatus{detection (last 24 h), infected}`,
`group`, `moveState`, and with options `lastSuccessfulScan` and
`lastLoggedUsers`. The pilot's details rows carry the same 16-key `modules`
map as the inventory (agent type 3, BEST 8.26.x). One documented sample has
spaces inside a timestamp (`"2015-06-22T13: 40: 06"`); the parser strips them.

`lastSeen` is the one date GravityZone gives for a machine, and only here,
one call per row. The method knows *managed* endpoints only: for a row with
no agent it answers -32602 `Invalid value for 'endpointId' parameter`
(`ArgumentRejected`), which the reader treats as "no date" for that row
rather than a failed batch. Live, 2026-09-28: 116 lost devices, 116 detail
reads on every board; inventory pages themselves took 27 to 90 s that day,
hence the connector's own 120 s timeout (`BITDEFENDER_TIMEOUT_SECONDS`).

### Last seen in bulk: there is none (*documentation and live*, 2026-09-28)

Checked for a list method that returns a last-seen for many endpoints at
once. The Public API's Network methods (`getNetworkInventoryItems`,
`getEndpointsList` v1.0 and v1.1) document and return `firstSeen` per row
and, with `options.endpoints.includeScanLogs: true`, `lastSuccessfulScan
{name, date}`; neither returns `lastSeen`, which is documented on
`getManagedEndpointDetails` only. The Reports API (`createReport`) is an
asynchronous report job, not a listing. So the design is:

- the inventory listing now sends `includeScanLogs: true`, and a row scanned
  within `COVERAGE_RMM_STALE_DAYS` is alive with no further call — the one
  bulk activity date there is (`config.bitdefender_endpoints` reads it into
  `ToolEndpoint.last_seen`);
- `lastSeen` is read one row at a time only for the rows paired with devices
  NinjaOne has lost for less than `DEVICE_RETIRE_DAYS` and not alive by scan
  date, under `BITDEFENDER_DETAIL_CONCURRENCY`, and kept on the record's
  `DeviceVendorLink` (`vendor_last_seen_at`, `vendor_checked_at`) for
  `BITDEFENDER_LAST_SEEN_REFRESH_HOURS`, so the board, the resolution and
  the review share one read a day (`app/devices/lastseen.py`). Every run
  logs the counts: lost devices, alive by scan date, beyond the retire
  window, from links, fetched.
An unknown `endpointId` is -32602 "does not exist" → `NotFound`.

### Incidents (*live*, on a tenant with no EDR)

`getIncidentsList` (v1.2) needs a licence with EDR (`manageEventCorrelator`).
**Without it the method returns an empty list and no error**, so on its own
the response cannot tell a company without EDR from a quiet week. Hence
`edr_licensed` from the licence on every path: `list_recent_detections` does
not query an unlicensed site and says "EDR is not licensed" in its note (and
lists `sites_without_edr` on a tenant-wide call); the review records a gap,
never a zero, and the daily collector writes the site's coverage row as
`not_applicable` (never `collected` with zero events). No company on the pilot
has EDR, so no live incident row exists: `incidents_list.json` stays
documentation-shaped and marked as such; `incidents_list_empty.json` is live.

`perPage` is **10** to 10000 (below 10 is -32602). Filters: `companyId`
(default: the key's own company; a bogus id is -32602 "You do not have full
access to" → `NotFound`), `endpointId`, `status[]` (`open, closed,
in_progress, false_positive, closed_mdr_reviewed`; an unknown value is -32602
naming these), `priority[]` (`unknown, low, medium, high, critical`),
`incidentType[]` (`incident` endpoint, `extendedIncident` organisation),
`mainAction[]` (`reported, blocked, partially_blocked`), `assignedUserId`, and
three date pairs that **must be sent together** (`startDate` alone is -32602):
`startDate`/`endDate` on `lastUpdated`, `processedStartDate`/`processedEndDate`
on `lastProcessed`, `changeStartDate`/`changeEndDate` on `lastIncidentChange`,
all ISO-8601. Options: `includeChildCompanies` (we send false: one company per
call, scoped by the guard), `sortBy` (`lastUpdated` default, descending).

Row (documentation): `incidentId, incidentNumber, incidentType, company{id,
name}, status, mainAction, created, lastUpdated, lastProcessed,
lastIncidentChange, severityScore (1 to 100), incidentLink, assignee,
priority, attackTypes[], details{detectionName, computerId, computerName,
computerFqdn, computerIp, computerMacAddresses, counters, incidentEvolution,
partOf}`. The connector windows on `lastUpdated`; the review counts by
`created` inside the period after fetching from the period's start to now. A
row with no `company` is placed by `details.computerId` through the inventory.

### Filter behaviour on unknown values (*live*, 2026-09-22)

The counterpart of NinjaOne's table (`docs/api-notes/ninjaone.md`, "Patch
report filters"), where one report ignores an unknown value and the other
returns nothing. **GravityZone errors, and the error names the accepted
values**, with two silent exceptions:

| Probe | What GravityZone does | What the caller sees |
|---|---|---|
| unknown filter key, unknown filter value (`type: {computer: true}`, `companyType: 9`), unknown option, `perPage` 1001, bogus `parentId`, unknown incident `status`, `startDate` without `endDate` | **-32602, and `data.details` names the accepted values** (`ArgumentRejected`) | a correctable error, returned to the model with the vendor's list; no request is retried and nothing changes |
| a company read **without `depth.allItemsRecursively`** | **0 rows, no error** | "the company has no endpoints", which is false: the flag is never optional |
| `getIncidentsList` on a company **without an EDR licence** | **empty list, no error** | "a quiet week", which is unknowable: `edr_licensed` decides before the call |
| unknown endpoint id, bogus incident `companyId` | -32602 with "does not exist" / "do not have full access to" | `NotFound` |
| `getTaskStatus` with an unknown task id | -32000 "Task not found" | `UpstreamError` |

So, unlike NinjaOne, a fixed map of accepted values is not needed for
correctness here: a wrong value cannot silently widen or empty a result. The
connector still sends only the values documented above, applies its own
filters again on the rows, and never relies on a vendor filter for the
allowlist guard.

The third vendor, ConnectSecure, was probed live on 2026-09-22 and is the
worst of the three: its `condition` filter fails **silently in both
directions, split by endpoint family**. An unknown field or value on
`/r/company/*` and `/r/asset/*` returns zero rows; on `/r/report_queries/*`
it is ignored and every row comes back. So NinjaOne's two directions, which
are one report against another, are ConnectSecure's rule
(`connectsecure.md`, "The `condition` filter, live"). Of the four vendors only
GravityZone tells the caller it was wrong.

### Rate limits

Default 10 requests per second per API key, with a burst queue of 10.
`getNetworkInventoryItems` is 5 per second (burst 5). Write methods are
30 per minute or 5 per minute, none of which we call. A 429 carries
`Retry-After` in seconds, which the base connector honours. JSON-RPC and
HTTP download methods have separate counters. `BITDEFENDER_CONCURRENCY`
defaults to 4 so a per-site fan-out stays under the inventory method's limit.
No 429 was provoked live; `rpc_error_rate_limited.json` is documentation-shaped.

## Fixtures

`tests/fixtures/bitdefender/` was re-recorded on 2026-09-22 from live
responses saved outside the repository and scrubbed by a script that keeps
keys, types and enums and replaces every name, id, IP (192.0.2.x), MAC and
timestamp with consistent fakes; a leak check runs over the directory before
commit. Live-shaped: `api_key_details`, `companies_list`,
`company_details_partner`, `endpoint_details`, `license_info` (the pilot's
licence), `network_inventory_companies`, `network_inventory_site` (with a
few rows edited to exercise an unmanaged agent, an outdated agent, a module
off, a VM and an Exchange server), `network_inventory_root` (with re-parented
copies for the guard tests), `incidents_list_empty`, `rpc_error_not_found`,
`rpc_error_invalid_params`, `rpc_error_unknown_option`. **Still
documentation-shaped**, and marked in the file: `incidents_list` (no incident
exists on the tenant), `license_info_edr` (the live file with the licence
flags flipped true), `rpc_error_authorization`, `rpc_error_rate_limited`.

## Still unverified

1. **Nested partners.** No sub-partner exists on the pilot; what `companyId`
   a sub-partner's customer's endpoint carries in the root walk, and whether
   `getCompaniesList(companyType: 1)` should recurse, is untested.
2. **An incident row.** Everything under "Incidents" about row shape is from
   the documentation until a company with EDR exists.
3. **Rate limit headers** and what a burst-queue overflow returns in the body.
4. **A key with only the five APIs enabled**: the pre-flight's `missing`
   line has only been exercised in tests.

## Re-running the live checks

```
python scripts/validate_connector.py --client teamlogic-it-austin --check-bitdefender
python scripts/sync_sites.py --client teamlogic-it-austin --connector bitdefender --dry-run
python scripts/validate_connector.py --client teamlogic-it-austin --tool count_endpoints --arg group_by=status
python scripts/validate_connector.py --client teamlogic-it-austin --tool count_endpoints --arg group_by=module --site shelton-keller-group
python scripts/validate_connector.py --client teamlogic-it-austin --tool list_unprotected_endpoints --site shelton-keller-group
python scripts/validate_connector.py --client teamlogic-it-austin --tool list_recent_detections --days 30
python scripts/validate_connector.py --client teamlogic-it-austin --tool get_endpoint_protection --site shelton-keller-group --arg device_name=<a device from list_devices>
python scripts/collect_events.py --client teamlogic-it-austin --days 7
python scripts/tbr.py generate --client teamlogic-it-austin --site shelton-keller-group --period 2026-08
```

Results go in `docs/validation-log.md` as counts.

## Credentials to request

One GravityZone API key generated by a **partner** administrator account of
the franchisee, with **only** the Companies, Network, Incidents, Reports and
Licensing APIs enabled (General is always available). Ask for a key created
for this integration alone, described as such, so it can be revoked on its
own. The vendor offers no read-only scope, which is why the connector refuses
every non-read method itself; the pre-flight warns, naming each one, when the
key can call an API the connector never uses, and that key should be replaced.

# NinjaOne API notes

What we established while building `app/connectors/ninjaone/`, and what is still
unverified. Written September 7, 2026.

Source: the official OpenAPI 3.0.1 documents the console serves, which are the
authority here rather than any blog post or SDK:

- `https://app.ninjarmm.com/apidocs/authorization.json` (2 paths)
- `https://app.ninjarmm.com/apidocs/NinjaRMM-API-v2.json` (250 paths)

The rendered docs at `/apidocs/` are a JavaScript app, so fetching that page
gives you nothing; the two JSON files above are what it loads.

## Confirmed from the spec

### Auth

`POST /ws/oauth/token`, body `application/x-www-form-urlencoded`.

| Field | Notes |
|---|---|
| `grant_type` | one of `authorization_code`, `client_credentials`, `refresh_token`. We use `client_credentials`. |
| `client_id` | required |
| `client_secret` | required for a private client, which ours is |
| `scope` | space-separated; the spec's example is `monitoring`, which is what we send |

Response: `access_token`, `expires_in` (seconds), `token_type` (`bearer`),
optional `scope` and `refresh_token`. Client-credentials grants do not return a
refresh token, so we simply request a new one when the old expires. We refresh
60 seconds early (`TOKEN_EXPIRY_MARGIN_SECONDS`).

**One token per client per process, not per connector** (2026-09-18). The
factory builds a connector per tool call, and each used to hold its own token:
a live question that made three tool calls made three token requests. The
token now lives in `app/connectors/ninjaone/tokens.py`, keyed by `(client,
credential version, host or region, OAuth scope)`, and every connector with
that key reads it and shares one lock, so parallel tool calls that find no
token make one request. It is never shared across clients; a new credential
version retires the old token; a connector built without a credential version
gets a private store; a 401 discards only the token that was rejected, so a
connector holding a stale one cannot wipe a newer one. Tokens are held in
memory only and are never logged. Verified live: one site-scoped question
with three parallel tool calls made one `POST /ws/oauth/token`.

Errors are OAuth-shaped: `{"error": ..., "error_description": ...}`. We surface
`error_description` in `AuthError.details["detail"]`.

### Devices

Two endpoints, chosen by whether the call names a site:

| Call | Endpoint | Why |
|---|---|---|
| no site (the whole franchisee) | `GET /v2/devices` | one paged listing for the tenant instead of one per organisation |
| one site | `GET /v2/organization/{id}/devices` | the vendor does the scoping, so a mistake in our filter syntax cannot leak another client's devices |

`GET /v2/organization/{id}/devices` parameters are exactly:

| Parameter | In | Notes |
|---|---|---|
| `id` | path | organization identifier (int32) |
| `pageSize` | query | "Limit number of devices to return" |
| `after` | query | "Last Node ID from previous page" |

`GET /v2/devices` (operationId `getDevices`) has `pageSize` and `after` with
the same descriptions, plus `df` ("Device filter", undocumented syntax — see
item 1 below; we do not send it, the org id on every row is what we classify
by).

**`pageSize` has no maximum in the spec.** Checked 2026-09-17 in
`NinjaRMM-API-v2.json`: on all three list endpoints it is a bare
`{"type": "integer", "format": "int32"}` with no `maximum`, no default and no
prose limit. So the tenant listing's page size is our choice —
`NINJAONE_DEVICES_PAGE_SIZE`, default 1000 — and because the vendor could cap
it silently, that listing pages **until an empty page** rather than stopping at
the first short one: a silently capped page looks exactly like a last page,
and stopping on it would report one page as the whole tenant. That costs one
extra request per listing. If live validation shows pages coming back smaller
than requested, the cap is real; record it here and lower the setting to match.

Cursor pagination on both: pass the `id` of the last device in a page as
`after`. The response is a bare JSON array, not an envelope.

On the tenant listing every device is classified by its `organizationId`
against the client's sites: an active site's device is kept; an inactive
site's (a deactivated customer, an internal `zz*` organisation) is dropped
quietly; an organisation we have no site for is dropped with an error, because
it is either another franchisee's or a customer nobody has synced.

Device fields we read: `id`, `organizationId`, `systemName`, `displayName`,
`dnsName`, `nodeClass`, `offline`, `lastContact`. There are about two dozen
more (`policyId`, `tags`, `userData`, `maintenance`, ...) that we deliberately
do not model.

`nodeClass` is an enum: `WINDOWS_SERVER`, `WINDOWS_WORKSTATION`,
`LINUX_WORKSTATION`, `MAC`, `ANDROID`, `APPLE_IOS`, `VMWARE_VM_HOST`,
`HYPERV_VMM_GUEST`, `NMS_SWITCH`, `NMS_FIREWALL`, and more. We pass it through
as a string rather than constraining it, so a new class does not break a read.

**There is no OS field on this endpoint.** The `os` the tools report, and the
`os` filter and `count_devices(group_by="os")` grouping, is an OS *family*
derived from the `nodeClass` prefix (`WINDOWS_*` → Windows, `MAC*` → macOS,
`LINUX_*` → Linux, `VMWARE_*`, `HYPERV_*`, `NMS_*` → network device, unmapped
→ "Other", missing → unknown). It answers "how many Windows machines", not
"which are still on Windows 10". The OS name and build live on
`GET /v2/devices-detailed` (`os.name`, `os.buildNumber`), which also takes the
`df=org=N` filter; unverified against a live tenant and not used yet.

### One device: `GET /v2/device/{id}`

**The spec's schema is missing most of what the endpoint returns.** The spec
lists the base node fields plus `ipAddresses`, `macAddresses`, `publicIP`,
`notes`, `deviceType`. Live (2026-09-17, a Windows workstation) the response
also carries `os` (`manufacturer`, `name`, `architecture`, `lastBootTime`,
`buildNumber`, `releaseId`, `locale`, `language`, `needsReboot`), `system`
(`manufacturer`, `model`, `serialNumber`, `biosSerialNumber`, `domain`,
`domainRole`, `numberOfProcessors`, `totalPhysicalMemory`, `virtualMachine`,
`chassisType`), `memory`, `processors[]`, `volumes[]` and `lastLoggedInUser`.
`get_device` reads all of those. **There is no agent-version field** on this
or any other endpoint we read; the tool reports it as unavailable rather than
guessing. `device_detail.json` is the live shape.

The detail's `organizationId` is checked against the allowlist before the
record is returned, exactly as a listing row is.

### Device-keyed reports: `GET /v2/queries/*`

Twenty-four report endpoints share one shape and one paging scheme, confirmed
live on thirteen of them:

- **Every row names a device by `deviceId` only** — never an `organizationId`.
  Rows are classified through the inventory's `deviceId → organizationId`
  map (`NinjaOneClient.place_by_device`) before the allowlist guard, exactly
  as alerts are. Which inventory depends on the scope (see "Which devices a
  call lists" below): unscoped, the tenant's, where an unknown `deviceId` is
  dropped at **error** level; site-scoped, that site's own listing, where an
  unknown `deviceId` is simply out of scope and dropped with a **warning**
  (the report was narrowed with `df`, so it should not have been sent).
- **Envelope** `{cursor: {name, offset, count, expires}, results: [...]}`.
  The next page is requested with `cursor=<name>`; `offset` advances by the
  page; `count` is the vendor's total for the query. **With a `df` filter,
  `count` is `null`**, so a scoped read ends on the first short page instead.
- **The same cursor name is sent on every page after the first.** The name
  is a server-side position, so a three-page walk sends `cursor=<name>`
  twice; a log that shows one cursor value repeated is showing the protocol,
  not a repeat of a page. A second way to see the same value twice is the
  whole query walked twice: `device_macs` is cached per client for the
  inventory's TTL (180 s), and a collector run whose GravityZone listing
  takes longer than that walks `network-interfaces` again for the snapshot
  after the resolution (the page log, added 2026-09-28, tells the two apart:
  the page numbers restart). Live, 2026-09-28: `network-interfaces` was
  14,755 rows (7,199 on the 24th), three pages at `pageSize=5000`, the name
  sent on pages 2 and 3, offsets 0 and 5000, and the last page's envelope
  carried no name, offset or count at all, so the walk's checks accept a
  missing offset and stop on the short page. What would be a fault is the server's
  `offset` disagreeing with the rows already read: behind (the cursor
  restarted or expired past `expires`; the rows would repeat) or ahead (a
  retry after a failed response advanced the position; rows would be
  skipped). `query()` checks the envelope's offset on every page and raises
  `UpstreamError` on either, and logs every page at DEBUG
  (`ninjaone query <report> page N: cursor= offset= count= rows= expires=`).
- **`df=org={id}` narrows these reports** (verified: every returned row's
  device belonged to that organisation). Unscoped tools read the whole tenant
  in one cursor walk; a named site sends `df`.
- **`pageSize` has no declared maximum; 5000 was honoured** live (`software`,
  174,676 rows: `pageSize=5000` returned 5000). `NINJAONE_QUERY_PAGE_SIZE`
  defaults to 5000; the walk stops at `CONNECTOR_MAX_PAGES` and reports
  `truncated`.

Per report, as observed live:

| Report | Row fields | Vocabulary seen |
|---|---|---|
| `os-patches` | `id, name, kbNumber, severity, status, type, deviceId, timestamp` | the **waiting list**: patches with no installation attempt. **Correction (2026-09-18):** rows do carry `kbNumber` — 1,371 of 3,310 tenant-wide; the first sample happened to be drivers, which have none. status `APPROVED` (approved, not yet installed, i.e. pending: 2,176), `REJECTED` (1,132), `MANUAL` (2); `status=FAILED` returned no rows on 2026-09-17. severity `OPTIONAL`, `IMPORTANT`, `CRITICAL`, `NONE`; type `DRIVER_UPDATES`, `SECURITY_UPDATES`, `DEFINITION_UPDATES`, `REGULAR_UPDATES`, `UPDATE_ROLLUPS`, `FEATURE_UPDATES`. Server-side filters: see "Patch report filters" below |
| `software-patches` | `id, productIdentifier, title, impact, status, type, deviceId, timestamp` | status `APPROVED`, `REJECTED`; impact `unknown`, `recommended`; type `INSTALLER`, `PATCH` |
| `operating-systems` | the `os` block above plus `deviceId, timestamp` | `needsReboot` true on 27 of 300; what `list_pending_reboot` reads. `device-health` agrees: its `pendingRebootReason` is `WINDOWS_PM` on the same 27 |
| `device-health` | `healthStatus`, `pendingRebootReason`, `failedOSPatchesCount`, `pendingOSPatchesCount`, `alertCount`, threat and vulnerability counts, `productsInstallationStatuses`, `deviceId` | healthStatus `HEALTHY`, `UNKNOWN`, `NEEDS_ATTENTION`, `UNHEALTHY`. Not used by a tool yet |
| `volumes` | `name, driveLetter, label, deviceType, fileSystem, capacity, freeSpace, serialNumber, deviceId, timestamp` | deviceType `Local Disk`, `Removable Disk`, `part`, `lvm`; Linux rows can report `capacity: 0`. `list_disk_space` counts only `Local Disk` with a capacity |
| `antivirus-status` | `productName, productState, definitionStatus, version, deviceId, timestamp` | productState `ON`/`OFF`; definitionStatus `Up-to-Date`/`Out-of-Date` (hyphenated, title case); products seen: Microsoft Defender, Bitdefender Endpoint Security Tools, Sophos, Webroot. A device with two rows (Defender OFF beside Bitdefender ON) is normal |
| `software` | `installDate, location, name, publisher, size, version, productCode, isSystemComponent, deviceId, timestamp` | **`installDate` is a date string** (`"2026-08-14"`), unlike every other timestamp in the API. **No server-side name filter**: a tenant-wide name search is a scan of the whole inventory (174,676 rows here), so `list_software` says how many rows it scanned and whether the cap stopped it; a named site narrows it with `df` |
| `logged-on-users` | `userName, logonTime, deviceId` | the *last* logon per device, not live sessions. The Monthly Report's lifecycle tables read it tenant-wide, keep the latest `logonTime` per device, and print `not reported` for a device with no row; the logins are people's names and are hidden as `User A`, `User B` unless the review's contact-names setting is on, and pseudonymised in a demo |
| `computer-systems` | `name, manufacturer, model, biosSerialNumber, serialNumber, domain, domainRole, numberOfProcessors, totalPhysicalMemory, virtualMachine, chassisType, deviceId, timestamp` | what `get_hardware` reads. **It carries no date of any kind** — no purchase date, no BIOS release date, no warranty field (checked live 2026-09-18 across the tenant; `timestamp` is when the row was collected). Hardware age therefore cannot be derived: `get_hardware` reports `age_available: false`, refuses an `older_than_years` filter without fetching, and offers the device's NinjaOne enrolment date (`created` on `/v2/devices`) labelled as enrolment, never as age. Warranty dates would come from the PSA's configuration items, which the pilot tenant's API user cannot read |
| `os-patch-installs` | `id, name, kbNumber, severity, status, type, installedAt, deviceId, timestamp` | the install **history**, distinct from `os-patches` (the waiting list); what `list_patch_installs` reads. `installedAt` is epoch seconds; `kbNumber` is empty on drivers and definition updates (present on 2,064 of 3,792 rows). status `INSTALLED`, `FAILED`; type also `UNKNOWN` and `UNSPECIFIED` here; severity also `NONE`. On 2026-09-17 `os-patches?status=FAILED` returned 0 rows while this report held 19 failed installs on 16 devices in 7 days — the two are not interchangeable. Filters: see "Patch report filters" below |
| `software-patch-installs` | `id, productIdentifier, title, impact, status, type, installedAt, deviceId, timestamp` | the third-party install **history**, the `os-patch-installs` shape with `software-patches`' vocabulary. Read live once on 2026-09-29 for August 2026 (`installedAfter`/`installedBefore`, no `df`): 4,646 rows on 800 devices, `count: null` in the cursor even unfiltered; status `INSTALLED` 4,639, `FAILED` 7 (at most 2 on one device); type `PATCH` 4,644, `INSTALLER` 2; impact `recommended`, `unknown`. `installedAt` is epoch seconds. Read by the review's third-party patch collector for the Monthly Report; not a model tool. Fixture: `query_software_patch_installs.json`, four rows with ids, device ids and product names replaced |
| `windows-services` | `name, displayName, description, startType, state, userName, deviceId, timestamp` | **311,880 rows tenant-wide**, so `list_windows_services` refuses without a single site and sends `df`. Filters `state` and `name` are sent server-side. startType seen `AUTO_START`, `AUTO_START_DELAYED`, `DEMAND_START`, `DISABLED`; only `state=STOPPED` rows were sampled. Most stopped services are demand-start and healthy; the tool flags an auto-start type that is not running (751 of 19,097 stopped rows on one site) and sorts those first. `userName` is the account the service runs as (`LocalSystem`, `NT AUTHORITY\NetworkService`, or a named service account); the tool returns it as `run_as` |
| `custom-fields` | `deviceId, fields` (`fields` is an object, field name → value) | takes `fields=<name>` to return one field; we never call it without. See "Custom fields" below |
| `network-interfaces` | `deviceId, adapterName, interfaceName, interfaceType, interfaceIndex, macAddress, ipAddress, dnsHostName, dnsServers, defaultGateway, subnetMask, linkSpeed, mtu, status` | **`macAddress` and `ipAddress` are lists**, one row per interface: 7,199 rows for 1,029 computers live on 2026-09-24, so a device has several. `dnsHostName` is the OS's own DNS host name and is often the full name where `systemName` is the 15-character NetBIOS truncation. Read by the coverage diagnostic (`scripts/coverage_diagnose.py`) for MAC-based pairing; not a model tool |

### Patch report filters (checked live 2026-09-18, counts only)

> **The two patch endpoints handle a filter value they do not know in opposite
> ways, and neither returns an error.**
>
> | Report | Unknown filter value | What the caller sees |
> |---|---|---|
> | `os-patches` / `software-patches` (the waiting list) | **no rows** (`severity=BOGUS`, `status=BOGUS`, `type=BOGUS` → 0) | "nothing is outstanding" — reads as fully patched |
> | `os-patch-installs` (the history) | **filter ignored, every row returned** (`status=BOGUS` → all 3,792) | everything, labelled as whatever was asked for |
>
> Case counts as unknown on the waiting list and not on the history:
> `os-patches?severity=critical` returned **0** rows where `CRITICAL` returned
> 19, while `os-patch-installs?status=installed` worked. HTTP 200 both times.
>
> So **no filter value reaches NinjaOne unless it is in a fixed map of values
> we have seen it honour** (`_vendor_value` in `tools.py`:
> `PATCH_STATE_PARAMS`, `PATCH_SEVERITY_PARAM`, `PATCH_CATEGORY_PARAM`,
> `INSTALL_STATUS_PARAM`), sent in the vendor's exact spelling. Anything else
> raises a `ValueError` naming the accepted values before any request is made,
> and the loop hands that message back to the model as a correctable error.
> Every filter is also applied again on our side after the fetch. Do not
> assume a third report behaves like either of these: probe it. GravityZone,
> probed the same way on 2026-09-22, does the opposite: an unknown value is a
> -32602 error naming the accepted values, and its two silent cases are a
> company read without the recursive flag (0 rows) and incidents without an
> EDR licence (empty). See `bitdefender.md`, "Filter behaviour on unknown
> values". ConnectSecure, probed live on 2026-09-22, is these two failures
> made a rule: its `/r/company/*` and `/r/asset/*` views return zero rows for
> an unknown field or value, its `/r/report_queries/*` views ignore it and
> return everything, and neither raises. What is one report against another
> here is one endpoint family against another there
> (`connectsecure.md`, "The `condition` filter, live").

`os-patch-installs` — `status`, `installedAfter`, `installedBefore`, `df`:

- **`status` accepts exactly `FAILED` and `INSTALLED`**, which is also all the
  spec lists. `list_patch_installs` maps `failed` / `installed` to them and
  sends nothing for `all`. Before this was validated in the tool, an unknown
  status fell through to "all" on our side too — the same wrong answer the
  vendor would have given.
- Volume, tenant-wide, 7 days: 3,760 `INSTALLED`, 32 `FAILED`. One site: 443
  installed across 97 devices and 91 distinct patches. Hence the per-patch
  totals in the tool's result; the rows alone would always be capped.

`os-patches` — `status`, `type`, `severity`, `ts`, `df`:

- **`status`, `type` and `severity` are all honoured server-side and
  combine** (`status=APPROVED&type=SECURITY_UPDATES` → 715 of 3,310 rows, every
  row matching).
- **One status per request.** `status=APPROVED,MANUAL` and a repeated
  `status=` parameter both returned no rows. "Pending" (`APPROVED` +
  `MANUAL`) is therefore two requests per report; rows are de-duplicated by
  `(id, deviceId)` in case a vendor change ever makes them overlap.
- Values we send: status `APPROVED`, `MANUAL`, `REJECTED`; severity
  `CRITICAL`, `IMPORTANT`, `OPTIONAL` (`NONE` exists on 2 rows and is not
  offered); type `SECURITY_UPDATES`, `UPDATE_ROLLUPS`, `FEATURE_UPDATES`,
  `DRIVER_UPDATES`, `DEFINITION_UPDATES`, `REGULAR_UPDATES` (each seen
  returning only matching rows).
- **There is no KB filter.** The spec declares none and a `kbNumber` parameter
  is ignored (all 3,310 rows came back). `list_patch_status(kb=...)` filters
  after the fetch, on `kbNumber` or on the KB in the patch name, since about
  half the rows carry no `kbNumber` field.

`software-patches` — `status`, `type`, `impact`, `productIdentifier`, `df`:

- `status` is honoured and behaves like `os-patches` (unknown → no rows;
  `MANUAL` → none on this tenant). **The report is dominated by rejected
  rows**: the first page of 5,000 was 4,845 `REJECTED`; `status=APPROVED`
  returned 677. Reading it unfiltered, as `list_patch_status` did until
  2026-09-18, paged through all of that to show a few hundred rows.
- `impact` (`recommended`, `critical`, `unknown` — lower case here) and `type`
  (`PATCH`, `INSTALLER`) were seen on rows but **not probed as filters**, so
  the tool does not send them. There is no severity, category or KB on these
  rows: `list_patch_status` leaves software patches out when any of those is
  asked for, and says so in `kinds_covered`.

`device-health` is **not** a usable aggregate for pending patches:
`pendingOSPatchesCount` was non-zero on 1 device (sum 2) while `os-patches`
held 2,176 approved rows. Its `failedOSPatchesCount` (24 devices, sum 40) is
at least in the region of the install history's 32 failures. The aggregates
`list_patch_status` returns are computed from the rows instead.

### Locations, processors, and what a machine is (checked live 2026-09-21)

- **`GET /v2/organization/{id}/locations`** returns a bare array of `{id, name, address, description}`. The path names the organisation, so the vendor scopes it; rows carry no organisation id. `GET /v2/organization/{id}` embeds the same list. Used by the business review only; it is not a model tool.
- **`/v2/queries/processors`**: `name, architecture, maxClockSpeed, clockSpeed, numCores, numLogicalCores, deviceId, timestamp`. One row per device on the pilot site. `name` is the marketing string ("13th Gen Intel(R) Core(TM) i7-1355U").
- **Physical or virtual** (`hardware.classify_machine`): the `virtualMachine` flag on `computer-systems`, overridden to virtual by a VM-guest node class or a hypervisor's manufacturer/model (VMware, Hyper-V's "Virtual Machine", KVM, Xen, VirtualBox, ...). Tenant-wide: 985 physical, 47 virtual. A device with no computer-system row is `unknown`, never guessed.
- **Windows 11 eligibility** (`hardware.windows11_eligibility`): from the OS name, the processor generation (Intel 8th generation and later, Core Ultra; AMD Ryzen 2000 and later except the 2x00G parts) and at least 4 GB of memory. **NinjaOne reports no TPM, Secure Boot or UEFI state**: the words do not appear in the spec. So `eligible` means "processor and memory pass", every result carries that caveat, and a processor whose generation cannot be read from its name (Xeon, Celeron, ARM) is `unknown`. Tenant-wide: 923 already on Windows 11, 22 ineligible, 15 eligible, 7 unknown, 65 not desktop Windows.
- `get_hardware` now joins three reports per device (computer-systems, processors, operating-systems) and takes `kind` and `windows11` filters.

### Custom fields

`GET /v2/device-custom-fields` lists the definitions on the tenant (55 on the
pilot): `name, label, description, type, entityType, scope, definitionScope,
apiPermission, technicianPermission, scriptPermission, system, active,
content, createTime, updateTime`. Types recorded in the fixture: `TEXT`, `CHECKBOX` and **`TEXT_ENCRYPTED`** (five
fields of that type on the pilot tenant).

Rule, enforced in `list_custom_fields`: a field whose type is `TEXT_ENCRYPTED`,
or whose name or label looks like a secret (`SECRET_FIELD_HINTS`: password, secret,
token, API key, recovery, BitLocker, credential, passphrase, PIN…), is listed as a definition marked `withheld` and
its values are **never requested** — the tool only ever queries
`custom-fields?fields=<one named, non-secret field>`. `showSecureValues` is
never sent. Values are truncated at 300 characters.

### Device activities: `GET /v2/device/{id}/activities`

Envelope `{lastActivityId, lastNodeActivityId, activities: [...]}`. Row:
`id, activityTime, deviceId, activityType, statusCode, status, message, type,
data`. Seen: activityType `ACTION`, `NINJA_REMOTE`, `MONITOR`; `status` is the
human label ("Session started", "Software Updated", "Completed") and
`statusCode` the enum. Filters `startTime`/`endTime` take ISO-8601; paging is
by activity id (`olderThan`). `list_device_activities` reads one page of the
tool's row cap and reports `truncated` when it was full. Every row's own
`deviceId` is checked against the device asked for.

### Tenant activity log: `GET /v2/activities` (checked live 2026-09-21, keys and counts only)

Used by the business review to count alerts that were resolved in a period
(`NinjaOneClient.list_activities`, `activity_log_reaches`). It is a client
method, not a model tool. `/v2/alerts` lists only *active* alerts, so a
resolved one exists nowhere but here.

- Envelope `{lastActivityId, activities: [...]}`. Row keys: `id, activityTime`
  (epoch seconds, float), `deviceId, severity, priority, seriesUid,
  activityType, statusCode, status, activityResult, sourceConfigUid,
  sourceName, subject, message, type, data`. **No organisation on any row**, so
  rows are placed through the inventory like `/v2/queries/*` rows.
- `activityType` seen: `ACTION`, `MONITOR`, `SCHEDULED_TASK`, `PATCH_MANAGEMENT`,
  `CONDITION`. A monitoring alert is a `CONDITION` row with `statusCode`
  `TRIGGERED`; its clearing is a second `CONDITION` row with `statusCode`
  `RESET` (and a `data.resetReason`). On the pilot site the two counts matched.
- Filters, all seen to bite: `df=org=N`; `type=CONDITION`; `status=RESET` or
  `TRIGGERED`; `after` and `before` as `YYYY-MM-DD`, day-granular, `before`
  exclusive. `yyyymmdd` and epoch values returned nothing.
  **An unknown `status` is an HTTP 500**, a third behaviour beside the two patch
  reports (one ignores an unknown value, one returns nothing). Only values from
  a fixed map are sent.
- Paging: newest first; `olderThan=<lowest id seen>` returns the next page with
  no overlap. `pageSize` below 10 is treated as 10. `lastActivityId` is neither
  the highest nor the lowest id on the page; it is not used.
- `sourceName`, `subject` and `message` are free text written by the tenant
  (script and policy names). They are customer data, not vendor vocabulary: do
  not print them from a peek script. The first probe on 2026-09-21 did, by
  treating `sourceName` as an enum; nothing was kept.
- **Retention is short and silent.** On 2026-09-21 the pilot site's log held
  nothing older than 2026-09-04 for any filter, and a window before that
  returned an empty list, not an error. A count over a period is therefore only
  a count if the log still reaches back past the period's start.
  `activity_log_reaches(start)` asks for anything before `start`; when there is
  nothing, the review records a gap and prints no figure. A review generated
  more than about two weeks after its period ends will not have this figure.
  Whether retention is a plan setting is not known.

### Alerts

`GET /v2/alerts` — "List active alerts (triggered conditions)". Parameters:
`sourceType`, `df` (documented only as "Device filter"), `lang`, `tz`.

**There is no `pageSize` or `after` on this endpoint**, so it is a single
request and there is nothing to paginate. If a client ever has more alerts than
NinjaOne will return in one response, we would not know; watch for this during
live validation on a busy tenant.

Alert fields we read: `uid`, `deviceId`, `message`, `subject`, `severity`,
`priority`, `sourceType`, `conditionName`, `createTime`, and the embedded
`device` object when present.

**Live alerts carry no `device` object and no organisation.** Confirmed on the
pilot franchisee on 2026-09-17: every row from `/v2/alerts` has `deviceId`,
`message`, `createTime`, `updateTime`, `sourceType`, `sourceConfigUid`,
`sourceName`, `subject`, `data`, `priority`, `severity`, `conditionName`,
`conditionHealthStatus`, `useGlobalHealthStatus` and sometimes `psaTicketId`
/ `ticketTemplateId` — and **not** the `device` object the spec lists as an
optional property. Until this was found the guard dropped every alert as
unclassifiable (`alert_count: 0` on a tenant with alerts on most sites), and
the recorded fixture, written from the spec rather than a live response, never
caught it. `alerts_no_device.json` is the live shape.

So an alert is classified by its device: the tenant inventory
(`GET /v2/devices`, cached — see "Cost of a multi-site read") keeps
`deviceId -> organizationId` for every device the tenant returned, inactive
sites included, and `list_alerts` resolves each alert through it before the
allowlist guard. An inventory device from an inactive site drops quietly; a
`deviceId` the inventory does not know is dropped at **error** level — the
tenant reported an alert on a device it does not list, which is not something
to paper over. The resolved device also lends the alert its name, which the
vendor omits. The inventory is fetched only when an alert needs it, so a
tenant that does embed the device costs nothing extra.

The `/v2/queries/*` family (24 endpoints: `antivirus-status`, `disks`,
`operating-systems`, `volumes`, `windows-services`, ...) returns rows keyed by
`deviceId` in the same way; none is used yet, and the same resolution applies
when one is.

Every guard log line now names the endpoint that produced the rows
(`GET /v2/alerts`, `GET /v2/devices`, `GET /v2/organization/{id}/devices`),
so the next unclassifiable row is diagnosable from the log alone. `/v2/devices`
on the pilot tenant returned 0 devices without an `organizationId` out of
1093; if one ever appears it is logged at INFO and left out, not warned about.

### Timestamps

`lastContact`, `created`, `lastUpdate`, `createTime` and `updateTime` are all
`number` / `double` in the spec, not strings. They are Unix epoch **seconds**
with a fractional part.

`epoch_to_datetime` refuses values above 100,000,000,000, which is the year
5138 — a value that large almost certainly means milliseconds, and inventing a
date for it would break hard rule 7. Such a field is reported as unknown
instead. If live validation shows plausible-but-wrong dates, milliseconds is
the first thing to check.

## The inventory is the whole client's, whoever asks

Found live on 2026-09-18: a site-scoped report tool classified the tenant
listing against the one site it targeted. That logged a false "response was
not narrowed … dropped 923 from the client's other sites" warning, and — the
real fault — cached only that site's rows under the client's key, so an
unscoped question within the TTL would have seen one site as the whole
franchisee. `_inventory()` now classifies `/v2/devices` against the client's
**full allowlist**; narrowing to the call's targets happens on read
(`list_all_devices` filters, `place_by_device` goes through the guard with
the call's own targets). Sibling rows in `/v2/devices` are expected — it is a
tenant-wide endpoint — and are not a filter failure; another tenant's
organisation is still an error.

**The cache key carries the scope (hardened the same day).** Before, an
entry was keyed by client alone, with the credential version checked on read
— so the key itself could not tell a one-site inventory from a whole one. An
entry is now keyed by `(client, the set of sites the fetching call
targeted)`, holds only those sites' rows, and serves a later call only when
it **covers every site that call targets** (`InventoryCache.get`, a superset
test):

- a whole-client entry serves a named site, narrowed on read — the common
  sequence "how many devices" then "which services are stopped at Acme" is
  still one listing;
- a site-scoped entry serves that site again and never a wider question: an
  unscoped call after it misses and lists the tenant again. That costs one
  extra listing in that order of questions, which is the price of making the
  original bug unrepresentable rather than merely fixed;
- a sync that adds a site changes the unscoped target set, so the old entry
  stops covering it and the next unscoped call re-lists.

`device_orgs` is still the whole tenant's map in every entry: it is what
tells a sibling's or an inactive site's device from one the tenant does not
know.

## Which devices a call lists, and how often

Found live on 2026-09-18: a single site-scoped question listed the whole
tenant twice. Two causes, both fixed:

- **Only an unscoped call touches the tenant inventory.** A site-scoped call
  builds its `deviceId → organizationId` map from that site's own
  `GET /v2/organization/{id}/devices` (`_list_sites`), not from `/v2/devices`:
  a one-site question has no business paging a thousand devices to place
  forty. A `deviceId` that listing does not hold is out of scope for the call
  and dropped. The site listing is cached under the site's own key for the
  same TTL, and `list_devices` for a named site reads it too, so a device list
  and a report about one site share a listing. If a whole-client entry is
  already cached it serves the site call with no request at all (the superset
  rule above).
- **Concurrent misses are single-flighted.** The loop runs a model's parallel
  tool calls together, each on its own connector; with a cold cache each one
  listed. `InventoryCache.flight(client_id)` is a per-client lock: the first
  caller fetches, the others wait, look again and are served. It is released
  on error, so a failed fetch does not wedge the waiters. Per client rather
  than per scope, so a site call waiting behind an unscoped one is served by
  the whole-client entry.

Verified live after the fix — one question about one site, three tools in
parallel (alerts, pending reboots, failed installs): one token request, one
site listing (two pages), no `/v2/devices`.

## NinjaOne holds no hardware dates — device age must come from elsewhere

Stated once, because it will be asked again: **no NinjaOne endpoint we read
carries a purchase date, a BIOS release date or a warranty date.** Checked
live on 2026-09-18: `/v2/queries/computer-systems` (tenant-wide), the
`system` block of `/v2/device/{id}`, and `/v2/devices`. The only dates on a
device are `created` (when NinjaOne enrolled it — a lower bound on nothing: a
five-year-old machine enrolled last month reads as one month) and collection
timestamps. NinjaOne's warranty tracking, where a tenant has it, is not
exposed on these endpoints.

So `get_hardware` never reports an age and refuses `older_than_years`. "Which
machines are due for replacement" needs another source, later:

- **Autotask configuration items** (`installDate`, `warrantyExpirationDate`)
  — already modelled by `list_configuration_items`, but blocked on the pilot
  tenant until the API user can read ConfigurationItems (validation log), and
  only as good as the franchisee's data entry;
- **ScalePad** (Lifecycle Manager), which looks warranty and purchase dates up
  from the manufacturer by serial number — the serials `get_hardware` returns
  are the join key. Not a connector yet and not on the Phase 1 build order.

Do not derive age from `created`, the OS install date or the model name.

**`nodeClass` is not the machine's role.** Live on the pilot, a Windows 10 IoT
Enterprise till carried `WINDOWS_SERVER`, so the review no longer counts by
class alone: `hardware.device_role` reads the `operating-systems` row first
(Windows is a server only as Windows Server, macOS is a workstation) and falls
back to the class for Linux and for a device with no OS row. The devices where
the two disagree are listed on the internal renders.

**The one estimate we do print, labelled as one.** The Monthly Report's
lifecycle tables carry an `Est. age (years)` column: the review year less the
launch year of the processor family NinjaOne names (`hardware.processor_launch_year`:
Intel Core by generation, Core Ultra by series, AMD Ryzen by model series, Xeon
Scalable by the generation digit of the model number, Xeon E by series; the table
of years is in `app/connectors/ninjaone/hardware.py`). It is the age of the design,
not a purchase date, the page footnotes the method (`AGE_METHOD`), and a processor
the name does not place leaves the cell blank. It is not a fact the deck cites and
no finding is derived from it; `get_hardware` still reports no age.

## Cost of a multi-site read

**Devices.** An unscoped question costs one tenant listing — one request per
page of `NINJAONE_DEVICES_PAGE_SIZE` devices plus the terminating empty page —
however many customers the franchisee has, and the result is cached in-process
per client for `NINJAONE_INVENTORY_TTL_SECONDS` (default 180 s). `count_devices`
and a filtered `list_devices` read from that cache, so a run of "how many"
questions is one listing. The cache key includes the credential's version, so
replacing a client's credentials makes its entry miss. Naming a site reads that
organisation's own path, cached under the site's own key (see "Which devices a
call lists" above).

**Alerts** still fan out: `/v2/alerts` takes a `df` filter rather than a path,
and the filter syntax is undocumented (item 1 below), so the connector narrows
it one organisation at a time rather than guessing at an `in`-style expression.
The calls run concurrently, at most `NINJAONE_CONCURRENCY` (default 5) in
flight, and the same bound applies to any other per-organisation fan-out.

A franchisee with forty customers therefore makes forty alert calls for one
unscoped alerts question, five at a time, against a vendor whose rate limits
we have not measured. If that proves costly, establish whether `df` accepts a
list of organisations, which would collapse it to a single call.

**Rate-limit headers.** The spec mentions none (`X-RateLimit-*` appears nowhere
in the document). `BaseConnector` records and logs any it does see, at WARNING
when `X-RateLimit-Remaining` is 0, so a live run will tell us whether NinjaOne
sends them. Retry itself relies only on `Retry-After` on a 429.

Autotask does not have this problem: its `in` operator covers every targeted
company in one query.

## Unverified — confirm during live validation

1. **The `df` filter syntax — now confirmed for `org=`.** The spec documents
   `df` as "Device filter" and nothing more. We send `df=org={id}` on
   `/v2/alerts`, and on 2026-09-17, once alerts could be classified at all, a
   live run across the pilot franchisee's 53 sites returned 40 alerts with
   **zero** dropped as belonging to another site — so the filter is honoured.
   Correctness still does not depend on it: `NinjaOneClient._scoped` drops
   any alert whose resolved organisation is not the one asked for and logs
   "GET /v2/alerts response was not narrowed ..." when it has to. If that
   line ever appears, the filter has stopped biting. Whether `df` accepts a
   list of organisations (which would collapse the fan-out to one call)
   remains untested.

2. **Region hosts.** `REGION_BASE_URLS` maps `us`/`eu`/`ca`/`oc` to
   `app`/`eu`/`ca`/`oc`.ninjarmm.com. The spec has no `servers` block, so these
   came from the console URL pattern rather than documentation. Only the US
   host has been exercised. Confirm the pilot franchisee's region before the
   first live run — a wrong host looks like an auth failure.

3. **Whether `lastContact` is ever absent versus null.** Our fixture has a
   device with `"lastContact": null`; we have not seen whether NinjaOne instead
   omits the key. Both are handled (the field is optional and nullable), but
   the `devices_never_seen` count is only meaningful if this is a real state.

4. **Rate limits.** The spec documents no rate-limit headers and no quota. Our
   retry path reads `Retry-After` if present and otherwise backs off
   exponentially. Note the real limits in this file once we have seen a 429.

## Scopes and permissions

The API application needs the `monitoring` scope for both endpoints we use.
Read-only is sufficient — nothing in Phase 1 writes (hard rule 2). When
requesting credentials from a franchisee, ask for a **read-only** application so
the credential itself cannot perform a write even if our code changed.

## The console's own address, for a link out to a device

The Devices page offers "open in NinjaOne" beside a device's vendor id, built in
`app/interfaces/consoles.py` and `CONSOLE_ROUTES` in `app/static/js/states.js`.
Two facts it rests on:

1. **The console and the API share a host.** The instance in the stored
   `base_url` is the instance the console is on, so `app.ninjarmm.com` for the
   API means `app.ninjarmm.com` for the console, and likewise `eu`, `ca` and
   `oc`. The link is therefore built from the client's own credential config and
   never from a default: a franchisee on the EU instance sent to `app` lands on
   a sign-in page for a tenant they have no account in, which reads as a broken
   product. A host we do not recognise as NinjaOne's yields no link at all, and
   the page shows the id to copy instead.

2. **The route is `/#/deviceDashboard/<deviceId>/overview`.** Taken from the
   console URL pattern, like the host list above, and **not yet checked against
   a live console** — no live run has been made for it. Confirm it on the next
   live session and record the result here. If it has moved, it moves in the
   two places named above and nowhere else.

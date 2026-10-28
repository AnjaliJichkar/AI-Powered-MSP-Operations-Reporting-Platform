# N-able Cove Data Protection (JSON-RPC) API notes

What we established building `app/connectors/cove/`, written 19 October 2026.

**Two kinds of claim live here and they are kept apart.** Everything down to
"What the live account answered" is N-able's own documentation, read on the
date below; the section after it is what the pilot franchisee's account
actually did, and where the two disagree the live answer wins and says so.
The fixtures under `tests/fixtures/cove/` are still shaped from the
documentation rather than recorded from the account.

Sources, all read on 19 October 2026 on `developer.n-able.com/n-able-cove`:
the Authorization, Construct A JSON-RPC API Call, Device Management,
Enumerating Devices, Enumerating Device Statistics, Customer Management,
Enumerating customers and Column Codes pages, and the
`Cove - Get Device Session Statistics` recipe. The documentation site
publishes a markdown copy of every page by appending `.md` to its URL, and
an index at `/n-able-cove/llms.txt`; that is much the cheapest way to read it
again, because the rendered pages are JavaScript and a plain fetch returns
only the navigation.

The operational page — the API user, its role, and mapping a customer to a
site — is `docs/cove-connector.md`.

## Host, envelope and authentication

- One endpoint for everything: `POST https://api.backup.management/jsonapi`,
  `Content-Type: application/json`. There is no path per method; the method
  is a field in the body.
- The envelope is JSON-RPC 2.0 with the visa beside it rather than in a
  header:

  ```json
  {"jsonrpc": "2.0", "id": "jsonrpc", "visa": "<visa>",
   "method": "EnumerateAccountStatistics", "params": {"query": {}}}
  ```

- **`Login` is the only unauthenticated call**: `params` are `partner` (the
  partner/company name), `username` (an email address) and `password`. The
  answer carries the user's own record in `result.result` — `Id`,
  `PartnerId`, `RoleId`, `EmailAddress`, `Flags` — and the token in a
  **top-level `visa`**, a sibling of `result`, not inside it.
- **A visa lasts 15 minutes, and every response carries a fresh one.** So a
  session is a chain: keep the newest visa from each answer and the clock
  restarts. Only a gap longer than 15 minutes between calls needs a second
  `Login`. We therefore re-read `visa` from every response, and re-login once
  when a call is refused for an expired one.
- `PartnerId` from the login answer is our own partner — the root to
  enumerate customers under. Nothing else tells us who we are.

## Read-only is ours to enforce

The API has no read-only scope. The same session that lists devices can
delete them: `AddAccount`, `ModifyAccount`, `RemoveAccount`, `AddPartner`,
`ModifyPartner`, `RemovePartner`, `AddUser`, `ModifyUser`, `RemoveUser` and
`RegenerateUid` are all ordinary methods on the same endpoint with the same
visa. A console role can be made read-only, but a role is somebody's setting
and not a guarantee we hold, so this is the Bitdefender and ConnectSecure
case again: **an allowlist in our code, checked before the request is
built** (`CoveClient.READ_ONLY_METHODS`, hard rule 2). The pre-flight should
warn when the API user's role can write, the way ConnectSecure's does; the
method for reading a role (`EnumerateUserRoles`, `GetUserInfo`) is
documented and not yet tried, so it is a candidate rather than a claim.

"Read-only mode" in the vendor's own recipes means something else entirely —
a *backup agent* that can restore but not back up, read from `config.ini` on
the machine. It has nothing to do with API access and must not be confused
with it.

## Customers are partners, and they nest

- `EnumeratePartners` takes `parentPartnerId` (required), `fetchRecursively`
  (false returns one level, true the whole tree) and `fields`, an array of
  integers selecting what comes back: 0 Name, 1 Level, 3 ChildServiceTypes,
  4 ServiceType, 5 State, 8 LocationId, 9 Flags, 10 Company, 18 ExternalCode
  and MailFrom, 20 CreationTime.
- Each partner carries `Id`, `Name`, `ParentId`, `Level` (`SubDistributor`,
  `EndCustomer`, …), `ServiceType`, `State` (`InTrial`, `Registered`, …) and
  `LocationId`. The answer is a **flat array**; the tree is in `ParentId`.
- `GetPartnerInfo` (by name) **is being deprecated** because two partners may
  share a name, and the vendor points at `GetPartnerInfoById` instead. We
  never look a customer up by name: the site's mapping holds the id.
- An end customer is the level we map to a `Site`. `State` is worth keeping:
  a customer `InTrial` is not the same as one `Registered`.

## Devices: one method, and column codes for everything

`EnumerateAccountStatistics` is the whole device read. `params.query` takes:

| Field | Meaning |
|---|---|
| `PartnerId` | the customer whose devices to list |
| `Filter` | an expression over column codes; see below |
| `ExcludedPartners` | partner ids to leave out |
| `SelectionMode` | `Undefined`, `Merged`, `PerInstallation`, `Count`. The vendor's own recipe uses `Merged` and the pages do not define the rest |
| `StartRecordNumber`, `RecordsCount` | paging; the vendor's recipe asks for 5,000 at a time |
| `OrderBy` | e.g. `"AU ASC"` |
| `Columns` | the column codes to return |
| `Totals` | `SUM`, `COUNT`, `MAX`, `MIN`, returned in `result.totalStatistics` |

The answer is `result.result`, an array of rows. Every row carries
`AccountId`, `PartnerId` and `Flags` **whatever columns were asked for**, and
the requested values arrive in `Settings`: a list of single-key objects,
`{"I14": "23630480"}`, which has to be folded into a mapping before anything
can read it. Values are strings even when they are numbers or Unix times.

**Three silences to design against**, all stated by the vendor:

- an unrecognised or badly formed column code is **ignored without an
  error**, so a typo returns a row with that value missing rather than a
  failure. Zero-padding is mandatory: `D1F3` fails silently where `D01F03`
  works.
- the documentation does not say whether the total number of matching devices
  comes back anywhere, so paging has to run until a page is short rather than
  trusting a count.
- **an empty list comes back as `null`, not `[]`** (live, 2026-10-19: two of
  the pilot's customers hold no devices, and both answered
  `result.result: null`). `None` is therefore no rows, and every other shape
  is still a failure. The test is `list` and not `Sequence`, because a string
  is a `Sequence` and iterating one yields characters.
- `Filter`'s grammar is undocumented beyond examples. The vendor's own recipe
  uses `"AT==1 && OT==2 && TL>0"` — **legacy short names**, mixed freely with
  new-style codes elsewhere in the same call. We build filters only from a
  fixed map of the ones we have a reason to trust, and apply them again on
  our side, as the ConnectSecure rule requires.

### The column codes we need

New-style ids, with the legacy short name in brackets where the vendor's own
examples use it. The full tables are on the Column Codes page.

| What | Code | Notes |
|---|---|---|
| Device id | `I0` (AU) | |
| Device name | `I1` (AN) | the name the console shows, with Cove's own suffix |
| Customer | `I8` (AR) | |
| Computer name | `I18` (MN) | |
| OS version | `I16` (OS) | a string, e.g. "Windows 11 Pro …, 64-bit" |
| OS type | `I32` (OT) | **1 workstation, 2 server, 0 undefined** |
| Physicality | `I81` | Undefined / Physical / Virtual |
| Used storage | `I14` (US) | bytes, as a string |
| Active data sources | `I78` (AP) | concatenated three-character codes |
| Retention units | `I39` (RU) | |
| Account type | `I59` (AT) | **0 unknown, 1 Backup Manager, 2 M365** |
| Recovery testing | `I80` | 0 none, 1 Recovery Testing, 2 Standby Image |
| Creation date | `I4` (CD) | Unix time |

`I59` is the one that matters most for the report: **it is how a Microsoft
365 tenant is told from a machine.** Cove counts the tenant as a device of
its own — the sample monthly report shows it in the device table with OS
type, physicality and OS version all "Undefined" — so every figure that is
about machines has to exclude `I59 == 2`, and the headline says both numbers
rather than one.

### Session statistics are per data source

A statistics field is combined with a data source into one code,
`D<source>F<field>`, both zero-padded. Data sources: `D01` Files and
Folders, `D02` System State, `D05` M365 SharePoint, `D08` VMware, `D14`
Hyper-V, `D19` M365 Exchange, `D20` M365 OneDrive, `D23` M365 Teams, and
**`D09` Total** (legacy `T`), which is the across-all-sources figure and
therefore the one a per-device status comes from.

| Field | Code | Notes |
|---|---|---|
| Last session status | `F00` | 1 InProcess, 2 Failed, 3 Aborted, 5 Completed, 6 Interrupted, 7 NotStarted, 8 CompletedWithErrors, 9 InProgressWithFaults, 10 OverQuota, 11 NoSelection, 12 Restarted |
| Last session errors count | `F06` | |
| Protected size | `F07` | |
| Colour bar, last 28 days | `F08` (B) | one character per day |
| Last **successful** session timestamp | `F09` (L) | Unix time |
| Last session timestamp | `F15` (G) | Unix time |
| Retention | `F14` (R) | |
| Last session user mailboxes count | `F20` (M) | |
| Last session shared mailboxes count | `F21` (@) | |

So the three Microsoft 365 figures the report needs are `D19F20` (protected
user mailboxes), `D19F21` (protected shared mailboxes) and `D20F20`
(OneDrive protected users), and a device's overall status and last good
backup are `D09F00` and `D09F09`.

**There are eleven session statuses and they are not three.** A report that
says OK or failed has to decide where `CompletedWithErrors`, `Interrupted`,
`Aborted`, `OverQuota` and `NoSelection` sit, and write that rule down
beside the figure rather than leaving it in a chart.

## History: the colour bar, not a history method

There is **no documented method for a device's session history on the main
endpoint**. The vendor's own recipe gets it by a second hop: `GetAccountInfoById`
returns the device's regional repository host and a token, and `QuerySessions`
is then posted to `https://<host>/repserv_json` with that token, an order,
a range and a query string like
`"0 != 1 and (Type & 1) == 1 and !((Flags & 2) == 2)"` (backup sessions that
are not cleaned).

We are not doing that, and the reason is worth recording: it is a second
endpoint, a per-device call and a per-device token, for a number the main
endpoint already carries. **`D09F08` is 28 days of per-day status in one
value on the row we already fetch**, which is a month of history for the
whole fleet in the call we were making anyway. The month-by-month trend
comes from the previous period's stored facts, which is how every other
comparison in a review is made. `QuerySessions` stays written down here as
the way to get exact per-session detail if a later question needs it.

The colour bar's encoding — which character means which status — is **not
documented on the pages above** and is the first thing to establish against
the live account.

## What the live account answered, 19 October 2026

The pilot franchisee's own Cove account, read-only, 53 device rows under
their partner. Everything in this section replaces a guess above it.

**A device read at a partner covers the whole tree beneath it.** 53 rows at
the franchisee's own partner against 1 at a single customer, and every row
carries its own `PartnerId`. So a fleet-wide question is **one request, not
one per customer** (`CoveClient.list_fleet`), and the rows are placed
afterwards. The guard is unchanged by that: a customer we hold no site for
is still dropped with an error, so a recursive read is a wider net and not a
looser one.

**`SelectionMode` accepts two values and names the rest.** `Merged` and
`PerInstallation` work; `Undefined` and `Count` are refused with
`Unknown selection mode: 0` and `: 3`, so the enum is positional and only
the middle two are usable. `PerInstallation` returned 55 rows where `Merged`
returned 53: a reinstalled agent is one row merged and two per installation,
which would double-count a machine. We send `Merged`.

**The column set does not change which rows come back.** Asked for 1, 5 and
24 columns, and with `RecordsCount` at 500 and 5000: 53 rows every time, the
same `AccountId`s. The vendor's hint about "a larger RecordsCount when some
columns are populated for only some devices" is not about row count.

**Times are Unix seconds in UTC**, and the three Total times agree when the
last session succeeded: on a device whose last backup was 23 February 2026,
`D09F09`, `D09F15` and `D09F18` were all `1771832885`. `I24` is the device's
own UTC offset (`-6` on a Central Time machine) and is for display: it is
not applied to any timestamp the API returns. A console showing `01:48`
local and our `07:48Z` are the same instant.

**The retention policy a report groups by is `I10` (Product), not `I39`.**
Live values of `I10`: `All-In` (8), `Documents` (4), `90D-52W-36M-7Y` (31),
`7ID-30D-24W-6M-10Y`, `30D-52W-12M-7Y`, `Max Retention 30ID-365D-52W-36M-10Y`,
`90 Day Retention` — the sample monthly report's two names are in there, so
that is the column its "Retention Policy Distribution" counts. The other two
candidates are different things and are kept as their own facts:

| Column | Live values | What it is |
|---|---|---|
| `I10` Product | `All-In`, `Documents`, `90D-52W-36M-7Y`, … | the retention policy, and what a report counts |
| `I39` Retention units | `Enhanced` (39), `Classic(Product)` (14) | the retention scheme behind it, two values only |
| `I57` SKU | `ServerPro`, `Office365`, `WorkstationPro`, `ServerProPlus 4TB`, … | the billing article, 15 values; not a policy |

`All-In` was the product on all 8 Microsoft 365 rows and nothing else, which
is why the sample report's 5-device customer showed `All-In 1` beside
`Documents 4`: one tenant and four workstations.

**The 28-day colour bar (`D09F08`) is one session-status digit per day**,
the same vocabulary as `F00`, with `0` for a day that had no session at all.
Read off the fleet:

| Character | Meaning | Seen on |
|---|---|---|
| `0` | no session that day | a device with no backup for 863 days: `0` × 28 |
| `1` | in process | a device backing up as we read it: `1` × 1 beside `5` × 27 |
| `2` | failed | |
| `5` | completed | a healthy daily backup: `5` × 28 |
| `6` | interrupted | |
| `8` | completed with errors | a device whose last session had errors: `8` × 19 |

Every row carried one, 28 characters long, 46 of 46. That is a month of
per-day history for the whole fleet inside the read we were already making,
and it is what a monthly success rate should be computed from — with the
caveat that **a success rate has to say where `8` sits**, because
"completed with errors" is neither plainly a success nor plainly a failure.

**The franchisee's own partner holds devices.** Two rows came back under the
partner we signed in as, belonging to no end customer. They are dropped, but
at INFO and named for what they are: we signed in as that partner, so it is
the one id we can be sure is ours, and calling it a containment breach would
be a false alarm on every fleet read.

## What is not established yet

What the first live run did not settle. Four of the seven are struck through
above; these are what is left, and `scripts/cove.py profile --diagnose` is
still where they get answered:

1. ~~the colour bar's per-character encoding~~ — **answered**: one
   session-status digit per day, `0` for no session;
2. whether a visa really is accepted for 15 minutes, and what the refusal
   looks like when it is not — code, message, HTTP status;
3. whether `result.totalStatistics` or anything else gives the total row
   count, or whether paging must run to a short page;
4. ~~what `SelectionMode` values other than `Merged` do~~ — **answered**:
   only `PerInstallation` is also accepted, and it double-counts a
   reinstalled agent;
5. ~~whether `I39` is the retention policy name the report needs~~ —
   **answered**: it is not; `I10` (Product) is;
6. ~~whether an end customer with no devices answers with an empty array or
   an error~~ — **answered, 2026-10-19: `result.result` is `null`.** The first
   draft raised `UpstreamError` for it, which reported a customer with nothing
   backed up as a vendor that did not answer;
7. the shape of an authentication failure versus a permission failure, which
   is what tells a wrong password from a role that may not read.

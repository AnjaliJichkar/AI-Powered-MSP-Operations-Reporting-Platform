# Microsoft Graph (v1.0) API notes

What we established building `app/connectors/microsoft365/`. Written
13 October 2026 **from Microsoft's documentation only**: no customer has
consented yet, so nothing below has been seen on a live tenant, and the fake
Graph in `app/demo/microsoft365_api.py` is built from these pages rather than
from recorded responses. Every claim here was read on learn.microsoft.com on
that date and the surprising ones were checked against the generated
permissions files in the documentation's own source, because the rendered
tables are easy to misread. **When the first tenant answers, this page is
rewritten from it and any disagreement is recorded**, as the ConnectSecure
notes were.

The operational page — access routes, consent, what to ask an administrator
for — is `docs/m365-connector.md`.

## Hosts, tenancy and authentication

- Graph: `https://graph.microsoft.com/v1.0`. Login:
  `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`.
- **A token is for one tenant.** Client credentials, `grant_type=client_credentials`,
  `scope=https://graph.microsoft.com/.default`. `.default` means "the application
  permissions this app was granted in that tenant's consent": an app-only token
  never asks for a scope at run time, so the consent is the whole of the access.
- Certificate authentication is a signed assertion, not a secret:
  `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`
  and an RS256 JWT whose header carries `x5t#S256` (the SHA-256 thumbprint of
  the certificate, base64url) so the tenant knows which key signed it. Built in
  `build_client_assertion`.
- `aud` is the token endpoint itself, `iss` and `sub` are the application id,
  and the assertion is short-lived. The connector mints one token per
  (client, credential version, tenant, scope) and keeps it in memory only, in
  the same shape as the NinjaOne token store.

## Reading users

- `GET /users` returns a **limited default property set**. Everything we use
  beyond that must be named in `$select`: `createdDateTime`, `userType`,
  `accountEnabled`, `onPremisesSyncEnabled`, `department`, `jobTitle`,
  `officeLocation`, `assignedLicenses`.
- Least-privileged application permission: `User.Read.All`.
- **Paging** is `@odata.nextLink`, followed as given. Default page 100, maximum
  `$top` **999** — **except** with `signInActivity`, where the maximum is
  **500**. The connector uses 999 and 500 respectively (`USERS_PAGE`,
  `USERS_PAGE_WITH_SIGN_IN`).
- **`signInActivity` needs Microsoft Entra ID P1 or P2 and `AuditLog.Read.All`.**
  A tenant without the licence refuses the whole request, so the connector asks
  once with it and, on that refusal, asks again without — the rest of a user's
  state is worth having on a tenant with no premium licence — and reports "last
  sign-in" as not available rather than as a zero.
- **Throttling is tighter for it**: 10 requests a minute per application for all
  tenants, and 10 a minute per application per tenant. A read of a large
  directory is a handful of pages, so this bites on a client with many customer
  tenants read back to back rather than within one read.

## MFA registration

- `GET /reports/authenticationMethods/userRegistrationDetails`.
- Application permission: **`AuditLog.Read.All`, and only that.**
  `Reports.Read.All` and `UserAuthenticationMethod.Read.All` are *not* listed
  for it; the latter is a much wider permission that reads and resets people's
  actual methods, and is not requested.
- **"This method doesn't work for disabled users"** — the documentation's own
  sentence. A disabled account therefore has no row, and its second factor is
  unknown rather than absent. Everything downstream treats `None` as unknown.
- Whether the report needs a premium licence is **not stated** on the Graph
  page. The Entra portal's *Usage and insights* dashboard does need P1 or P2,
  but that is the dashboard, not this API. We do not assume it: MFA coverage is
  read on tenants with no premium licence and the demo proves it.
- `isMfaCapable` is the figure a tenant's own screen shows and is what we count
  as registered; an account with a phone alone comes back registered and not
  capable.

## Licences

- `GET /subscribedSkus`, least-privileged application permission
  **`LicenseAssignment.Read.All`** (`Organization.Read.All` and
  `Directory.Read.All` also work and are wider).
- Supports `$select` only — **no `$filter`**, and it is not paged: one list.
- `prepaidUnits.enabled` is seats bought, `consumedUnits` is seats assigned;
  free is the difference and **never negative**, because a tenant in its grace
  period can be over its count.
- Names are Microsoft's part numbers (`ENTERPRISEPACK` is Office 365 E3).
  Nothing invents friendlier ones.

## Groups

- `GET /groups`: the documented least-privileged application permission is
  `Group-NestingSupport.ReadWrite.All`, **a write permission**, with
  `Group.Read.All` listed as higher-privileged. We take the read one; see
  `docs/m365-connector.md`.
- `GET /groups/{id}/members`: least privilege is `GroupMember.ReadBasic.All`
  (basic properties only); we request `GroupMember.Read.All` so a row can carry
  an address and whether the account is enabled. A group with hidden membership
  additionally needs `Member.Read.Hidden`, which we do not request: such a
  group's members are simply not listed.
- **`$count` needs the `ConsistencyLevel: eventual` header.** The header and
  `$count` are required together with `$search`, `$filter`, `$orderby` or an
  OData cast; the documented `/members/$count` example sends it, and so do we.
- What a group *is* has to be derived: `groupTypes` containing `Unified` is a
  Microsoft 365 group, `resourceProvisioningOptions` containing `Team` makes it
  a Team, `mailEnabled` without `securityEnabled` is a distribution list, both
  is a mail-enabled security group, and neither mail nor unified is a security
  group (`GraphGroup.kind`).

## Mailboxes

- **Size is only in the tenant-wide report.** `GET
  /reports/getMailboxUsageDetail(period='D30')`, application permission
  `Reports.Read.All`. There is no app-only way to read one person's mailbox
  size: the beta storage-quota API is not supported for application permissions
  and measures OneDrive.
- The call answers **302** with a `Location` pointing at a pre-authenticated
  download URL that is valid for a few minutes and **doesn't require an
  `Authorization` header**. The connector follows that hop itself
  (`follow_redirects=False`, then a plain `GET` of the location) rather than
  letting the HTTP client do it, so our token is never sent to the storage host
  the URL points at.
- The body is **CSV**, not JSON, and the columns are read **by name**: Microsoft
  has moved them, and a report read by position would quietly report the wrong
  number.
- **Names are concealed by default.** A tenant-wide setting hides user names in
  every usage report, including this one; it is read at `GET
  /admin/reportSettings` as `displayConcealedNames`
  (`ReportSettings.Read.All`). A concealed row carries an opaque identifier, so
  it is counted and said to be concealed rather than joined to anybody. (The
  documentation does not date this change; do not claim a year for it.)
- **Shared, room or person** is `GET /users/{id}/mailboxSettings/userPurpose`,
  application permission `MailboxSettings.Read`. Values: `user`, `linked`,
  `shared`, `room`, `equipment`, `others`, `unknownFutureValue`. The
  documentation names this as the way to tell a shared or equipment mailbox
  from a single user's. It is one request per account, so the profile reads it
  only for the accounts that look like they have no second factor — which is
  where the distinction changes a number — bounded by `MAILBOX_KIND_READS`.

## The manager, which the documentation contradicts itself about

- `GET /users/{id}/manager`: the permissions table reads **Application: Not
  supported**, in both the least- and higher-privileged columns. Checked
  against the documentation's own generated permissions file, because it is
  surprising.
- The description of the `User.Read.All` application permission says it allows
  the app to read "the full set of profile properties, reports, and managers of
  all users".
- Both cannot be true. The connector asks and reports what the tenant says: 404
  is a user with no manager, a refusal becomes `NotAvailable` and the page says
  the manager could not be read for this tenant. `GET /users?$expand=manager`
  is documented but says nothing about app-only, so it is treated as
  undocumented rather than as a way round.

## Errors, and the refusals that are facts

- Graph's error body nests the message: `{"error": {"code": …, "message": …}}`.
  The shared connector reads a flat `message`, so the Microsoft 365 client
  overrides `_error_detail` to read the nested one — without it a licence
  refusal arrives as an empty authentication error.
- A refusal whose message names a premium licence (Entra ID P1/P2, "AAD
  Premium") is mapped to `NotLicensed`, and a permission the tenant did not
  consent to is `NotAvailable`. Both are **facts about the tenant**, not
  failures: they become a sentence on the profile and under the reading they
  belong to, never a zero and never an exception a caller has to handle.
- 429 carries `Retry-After` and is retried by the shared backoff.

## The read-only POSTs we do not use

Graph has query endpoints that are POSTs and change nothing: `/$batch`,
`/directoryObjects/getByIds`, `/users/{id}/getMemberGroups`. The client's
`READ_ONLY_POSTS` allowlist exists for them and is **empty**, because nothing
here needs one: `transitiveMemberOf` answers what `getMemberGroups` would, and
batching is an optimisation nobody has measured a need for. Adding one is a
deliberate edit to that frozen set, and the guard refuses every other method
before a request is built.

## Exchange Online, if it is ever built

Not Graph: a different API with its own permission (`Exchange.ManageAsApp` on
*Office 365 Exchange Online*), a certificate (no client-secret path exists, and
the certificate must be CSP rather than CNG), a directory role on the service
principal (Global Reader is the read-only one; Microsoft says Exchange
Administrator covers every task, so a read-only role has to be proven on a real
tenant), and the ExchangeOnlineManagement PowerShell module — which is a
runtime this engine does not have. What it would add: per-mailbox size,
recipient types beyond `userPurpose`, distribution-group detail, mailbox
permissions, litigation hold and archive state.

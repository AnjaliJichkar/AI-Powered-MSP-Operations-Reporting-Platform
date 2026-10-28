# The Microsoft 365 connector: access, consent and what it reads

Desk reads one end customer's Microsoft 365 tenant to answer the questions an
engineer asks about people rather than machines: is this account enabled, does
it have a second factor, when did they last sign in, what is licensed, who is
in that group, what does this customer's directory actually look like.

**It reads and can do nothing else.** The client
(`app/connectors/microsoft365/client.py`) refuses any method but `GET` before a
request is built and raises `ReadOnlyViolation`; the list of read-only `POST`
endpoints Graph offers for queries is in the code and is deliberately **empty**,
because none of them is needed. No write permission is requested, no write
method exists, and `tests/connectors/test_microsoft365_client.py` fails the
suite if one appears.

This page is the operational half: the two ways to get access, what the
customer's administrator is asked to agree to and what they see, every
permission with what it is for, what a tenant cannot answer and why, and what
Microsoft 365 cannot give us at all. The design is in CLAUDE.md and the vendor
findings are in `docs/api-notes/microsoft365.md`.

## Tenancy: a tenant is a site, not a client

Every other connector gives us one tenant holding all of a franchisee's
customers. Microsoft 365 is the other way round: **each end customer has their
own Entra tenant**, consented separately, so

- the **credential** is the client's — our application's id and its certificate;
- the **tenant id** is the **site's** mapping (`ToolMapping.external_id`), one
  per end customer;
- a read is one tenant at a time, with a token minted for that tenant.

A Graph token is for one directory and cannot enumerate the others, so
**Microsoft 365 cannot be synced**: `list_organisations()` refuses, and the
tenant id goes onto the site by hand, from the consent that customer granted.

```
python scripts/manage_client.py map-site --client <client> --site <customer> \
    --connector microsoft365 --external-id <entra tenant id>
```

The command refuses an id another client already holds, exactly as the sync
does. A client with more than one tenant mapped must name the site on every
read; answering for all of them would merge two companies' directories into one
answer.

## Two ways in

### 1. Our multi-tenant application, consented per customer (preferred)

Inputiv registers **one** application in its own tenant, marked
*Accounts in any organizational directory* (multi-tenant). Each customer's
administrator grants it admin consent in their own tenant, which creates a
service principal there that they can review and revoke at any time, without
anybody creating an account or a credential in their directory.

Why this one: one credential to hold and rotate rather than one per customer;
the customer keeps the revocation switch; and onboarding a new customer is a
link and a mapping rather than an app registration.

**The consent link.** The documented v2.0 form, which is the one to use because
it names the scope:

```
https://login.microsoftonline.com/{tenant}/v2.0/adminconsent
    ?client_id={our application id}
    &scope=https://graph.microsoft.com/.default
    &redirect_uri={a redirect URI registered on the app}
    &state={something we can tie the reply to}
```

`{tenant}` is the customer's tenant id or a verified domain of theirs — never
`common`, which would let the consent land in whichever directory the person
happened to be signed in to. `.default` means "every application permission
this app has been granted in its registration", which is why the list below is
the list they are consenting to: an application asks for nothing at run time.

The shorter `…/adminconsent?client_id=…` form also works and is what Microsoft's
own Exchange documentation uses, but it carries no `scope`, so prefer the one
above.

**Who can click it.** Granting tenant-wide admin consent for *Microsoft Graph
application permissions* needs **Global Administrator** or **Privileged Role
Administrator**. Application Administrator and Cloud Application Administrator
can consent to most APIs but explicitly **not** to Microsoft Graph application
permissions, so sending the link to one of those gets a refusal that looks like
a bug and is not.

**What the administrator sees.** A Microsoft-hosted page naming the
application and its publisher, with the permissions written in Microsoft's own
words — "Read all users' full profiles", "Read all audit log data", "Read all
groups", "Read all usage reports", "Read tenant report settings", "Read all
users' mailbox settings", "Read all license assignments" — and the sentence
that it applies to the whole organisation. After they accept, the app is listed
under **Entra admin centre → Enterprise applications**, where they can read the
granted permissions, see the sign-in log for it, and remove it. Nothing is
installed, no mailbox is touched, and no user is prompted.

### 2. An application registered in the customer's own tenant

Where a customer will not consent to an outside application, their
administrator registers a single-tenant application themselves, adds the same
permissions, grants admin consent, and gives us the application id with a
certificate (or a secret). It works identically — only the credential differs —
and it costs one credential per customer to hold, rotate and lose, which is why
it is the fallback rather than the plan.

In both routes the credential is stored once per client and the tenant id per
site, so a customer on route 2 needs its own client-level credential and
therefore its own client row, or a credential override we do not have yet.
Today: route 1 for everybody, route 2 recorded here as the answer to "they said
no".

## The credential

Certificate preferred, secret accepted:

```
MICROSOFT365_CLIENT_ID=... \
MICROSOFT365_CERTIFICATE_PRIVATE_KEY="$(cat key.pem)" \
MICROSOFT365_CERTIFICATE_PEM="$(cat cert.pem)" \
python scripts/manage_client.py set-credentials --client <client> --connector microsoft365
```

A certificate is preferred because a copy of our configuration is then not a
copy of our access: the assertion is signed, the private key never leaves us,
and a leaked environment variable from a secret is a password. The client
builds the RS256 client assertion itself (`build_client_assertion`) with the
`x5t#S256` header the tenant uses to tell it which key signed it.

## The permissions, and what each one is for

All **application** permissions, all read-only. Verified against Microsoft Learn
on 13 October 2026; the per-endpoint detail is in
`docs/api-notes/microsoft365.md`.

| Permission | What it is for | Which features need it |
|---|---|---|
| `User.Read.All` | `GET /users` with the properties we select, one user, a user's `transitiveMemberOf` | everything: the profile, the Account block on a ticket, every Ask tool |
| `AuditLog.Read.All` | `signInActivity` on a user; the authentication-methods registration report | last sign-in, MFA coverage, "who has no MFA", the dormant-account finding |
| `LicenseAssignment.Read.All` | `GET /subscribedSkus` | the licence summary, the profile's subscriptions, licence names on an account |
| `Group.Read.All` | `GET /groups` | groups, Teams and distribution lists on the profile |
| `GroupMember.Read.All` | group members and `…/members/$count` | member counts, "who is in this group", a person's groups |
| `Reports.Read.All` | `getMailboxUsageDetail` | mailbox sizes on the profile |
| `ReportSettings.Read.All` | `GET /admin/reportSettings` | whether this tenant conceals names in its reports, so a concealed row is said to be concealed rather than guessed at |
| `MailboxSettings.Read` | `…/mailboxSettings/userPurpose` | telling a shared mailbox or a room from a person, which keeps both out of the MFA gap and out of the unlicensed-account finding |

Two notes on "least privilege", because Microsoft's own tables do not say what
you would expect:

- For `GET /groups` the documented least-privileged application permission is
  `Group-NestingSupport.ReadWrite.All` — a **write** permission. We will not
  request a write permission to do a read, so we take `Group.Read.All`, which
  the table calls higher-privileged and which cannot change anything.
- For group members the documented least privilege is
  `GroupMember.ReadBasic.All`, which returns only basic properties. We request
  `GroupMember.Read.All` so a member row can carry the address and whether the
  account is enabled. A customer who wants to trim can grant the basic one
  instead; the group tools then show fewer fields and nothing else changes.

Everything else a reviewer might expect is **not** requested and would be
refused by our own code anyway: no `Directory.Read.All` (wider than we need and
the usual reason a security review stops), no `Mail.Read`, no `Files.Read.All`,
no `Sites.Read.All`, no `UserAuthenticationMethod.Read.All` (the registration
*report* is enough; that permission reads and resets people's actual methods),
and nothing ending in `.ReadWrite`.

## What a tenant cannot always answer

Each of these is reported as a sentence saying what is missing and why —
**never as an error and never as zero**. They come back on the profile under
"Not available for this tenant", and under the reading they belong to on a
ticket.

- **Last sign-in needs a licence.** `signInActivity` requires Microsoft Entra
  ID P1 or P2. Without it the directory still reads: the profile prints "last
  sign-in: this tenant has no Microsoft Entra ID P1 or P2 licence", the Account
  block says the same under "Days since last sign-in", and the dormant-account
  finding simply never fires. It is not inferred from anything else.
- **MFA registration leaves out disabled accounts.** Microsoft's registration
  report "doesn't work for disabled users", so a disabled account's second
  factor is **unknown**, not absent, and the no-MFA finding never fires on one.
  (The disabled account has its own finding, which is the one that matters.)
- **Report names are concealed by default.** A tenant-wide setting hides user
  names in every usage report, including the mailbox report we read. We read
  the setting and say how many mailbox sizes could not be joined to a person,
  rather than attaching a size to the wrong one.
- **Mailbox kinds need `MailboxSettings.Read`.** Without it a shared mailbox
  looks like a person with no second factor and no licence, so the MFA gap
  reads larger than it is. The profile says exactly that when the permission is
  missing.
- **The manager is contradicted by Microsoft's own documentation.** The
  reference for `GET /users/{id}/manager` lists application permissions as "Not
  supported", while the description of `User.Read.All` says it reads "the full
  set of profile properties, reports, and managers of all users". We ask and
  report what the tenant says: a 404 is a user with no manager, a refusal is
  "the manager could not be read for this tenant", and nothing else changes.

## What Microsoft 365 cannot give us at all

Written down so nobody goes looking twice, and as the brief for a later
Exchange Online connector.

- **One person's mailbox size.** There is no app-only Graph call for it. The
  only source is the tenant-wide usage report we already read, which is a day
  behind and conceals names unless the customer turns concealment off. The beta
  storage-quota API is not supported for application permissions and measures
  OneDrive anyway.
- **Which mailboxes exist, and their type beyond `userPurpose`.** Graph has no
  mailbox inventory: a user with no mailbox is only discovered by the mailbox
  settings read refusing.
- **Distribution-list management detail** — delivery restrictions, moderation,
  who may send as the list — is Exchange's, not Graph's.
- **Who can open a shared mailbox.** Mailbox permissions are Exchange's.
- **Litigation hold, archive state, retention, forwarding rules, transport
  rules, connectors, anti-spam policy.** All Exchange.
- **Conditional access policy evaluation**, per-user legacy MFA state, and
  sign-in *logs* (as opposed to the last sign-in time). Out of scope on
  purpose: the first two are a different permission set and the third is a
  volume of personal data nobody has asked for.

**An Exchange Online connector would need**, and this is why it is not this
task:

1. The application permission **`Exchange.ManageAsApp`** on *Office 365
   Exchange Online* (a different API from Microsoft Graph), consented
   tenant-wide per customer.
2. A **certificate**. Exchange Online app-only has no client-secret path, and
   the certificate must be a CSP certificate — CNG is not supported.
3. A **directory role** assigned to the service principal: **Global Reader** is
   the read-only one; Exchange Administrator is the one Microsoft says covers
   every task, so `Get-MailboxStatistics` under Global Reader has to be proven
   on a real tenant before promising it.
4. A **PowerShell runtime**. The cmdlets (`Get-Mailbox
   -RecipientTypeDetails SharedMailbox`, `Get-MailboxStatistics`,
   `Get-DistributionGroup`, `Get-DistributionGroupMember`,
   `Get-MailboxPermission`) are the ExchangeOnlineManagement module, not an
   HTTP API we can call from `httpx`. That is a new kind of dependency for this
   engine and a decision rather than an implementation detail.

## Running it

```
python scripts/m365.py profile --client <client> --site <customer>
python scripts/m365.py people  --client <client> --site <customer>
```

`profile` prints what that tenant is: cloud-only against synced accounts,
subscriptions with assigned and free seats, MFA coverage over enabled member
accounts with the people who have none, groups and Teams and distribution lists
with member counts, how many people share each department, job title and office
— which is what a role matrix is drafted from, being what the tenant already
says rather than a guess — the mailboxes, and anything the tenant could not
answer. It **writes nothing to the customer** and stores only counts: the audit
row it leaves carries numbers and ids, never a name.

`people` joins that tenant's accounts to the customer's Autotask contacts by
email address and to the machines they are the last logged-on user of, through
the device identity layer. It stores nothing at all and prints ids.

On a ticket, the contact's account appears under the tool readings as a short
**Account** block, and four findings are ranked with the existing ones: the
account is disabled (beside "the machine is not there", because nothing they
sign into will work either), no MFA registered, no sign-in for 30 days, and an
unlicensed member account. A customer with no Microsoft 365 is **not assessed**
there, which is not the same as a gap.

Everything above works against the demo tenant with no network:
`python scripts/demo_tenant.py create` maps a fake Entra tenant onto each of
the eight customers.

## What to ask a customer's administrator for

Send them this, with the consent link:

1. **Who clicks it**: a Global Administrator or Privileged Role Administrator
   of *their* Microsoft 365 tenant. Nobody else can consent to Graph
   application permissions.
2. **What it is**: a read-only application, no account created in their
   directory, no mailbox or file content read, revocable by them at any time
   from Enterprise applications.
3. **Their tenant id** (Entra admin centre → Overview), so it can be mapped to
   their site here. The consent reply carries it too.
4. **Whether they have Microsoft Entra ID P1 or P2.** Without it there is no
   last-sign-in data, and two of the four ticket findings never fire.
5. **Whether their usage reports conceal user names** (Microsoft 365 admin
   centre → Settings → Org settings → Reports). Concealed is the default; left
   on, mailbox sizes cannot be joined to a person.
6. Confirmation that the permission list above is what they consented to, for
   the record.

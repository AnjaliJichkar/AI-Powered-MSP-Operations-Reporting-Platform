# N-able Cove Data Protection: the operational page

What to ask the franchisee for, how to store it, how to map their customers,
and how to check it works. The vendor findings — methods, column codes, the
visa, what is still unverified — are in `docs/api-notes/cove.md`.

**Nothing in this connector can write.** Cove has no read-only API scope: the
same session that lists devices can remove them, and the methods sit side by
side on one endpoint. So the guarantee is ours and is in our code — an
allowlist of three methods in `app/connectors/cove/client.py`, checked before
a request is built, with everything else refused and no HTTP made. A console
role that cannot write is worth asking for anyway, because two locks are
better than one, but the role is not what we rely on.

## What to ask for

One Cove Data Protection console user, created in the franchisee's own
Management Console under **Users**, with:

- **a role that can only read.** In the console a role is a set of
  permissions; the one to give this user is whichever of theirs grants
  viewing and nothing else. If the only role available can also write, say so
  and store it anyway — the connector refuses every write regardless — and
  record it, the way we record that the pilot's ConnectSecure key is an
  admin.
- **access to every customer they want reported on.** A user scoped to part
  of the tree will quietly return part of the estate, which looks exactly
  like a franchisee with fewer customers.
- **no multi-factor requirement on this account.** The JSON-RPC `Login`
  method takes a partner, a user name and a password and has no second step;
  an account that needs one cannot sign in this way.

Three values come back to us:

| Value | What it is | Secret? |
|---|---|---|
| partner name | the company name used to sign in, exactly as the console shows it | no |
| user name | the API user's email address | no |
| password | that user's password | **yes** |

The first two are identifiers and are stored in the clear beside the
credential. The password is encrypted at rest with the credential cipher like
every other secret, is never logged, never printed, and never appears in an
error message.

## Storing it

```
python scripts/manage_client.py set-credentials \
    --client <client-slug> --connector cove
```

It asks for the partner name and the user name at an ordinary prompt and for
the password at a hidden one, and prints back only how many characters it
read. **No command takes the password as an argument**, because an argument
lands in the shell history and in the process list where anyone on the box
can read it.

For a scripted run, the two identifiers can be flags and the password comes
from the environment:

```
COVE_PASSWORD='…' python scripts/manage_client.py set-credentials \
    --client <client-slug> --connector cove \
    --partner "<partner name>" --username <api-user@example.com> --no-prompt
```

`--base-url` exists for a tenant on another host; leave it off and the
connector uses `https://api.backup.management`.

## Mapping a customer to a site

Cove calls an end customer a **partner**, nested under the franchisee's own
partner. A site's mapping carries that partner's numeric id.

Find the ids with the check command, which prints how many customers the
account holds and how many are already mapped:

```
python scripts/cove.py profile --client <client-slug> --diagnose
```

Then map each one:

```
python scripts/manage_client.py map-site \
    --client <client-slug> --site <site-slug> \
    --connector cove --external-id <cove partner id>
```

A partner id already held by another client is refused, exactly as every
other connector's is, so two franchisees can never resolve to the same
customer.

The connector also implements `list_organisations()`, so
`python scripts/sync_sites.py --client <client-slug>` will discover the
customers and match them to existing sites by name in the usual way. Only
partners at the **EndCustomer** level become sites: the levels above are
distributors and the franchisee itself. A partner whose level the vendor did
not send is left out rather than guessed at, and the count of those is
logged.

## The first sync, and what it needed afterwards

Run once the credential is stored:

```
python scripts/sync_sites.py --client <client-slug> --connector cove
```

On the pilot, 19 October 2026, it reported 32 organisations: 22 matched
existing sites by name and 10 became new sites. Seven of the ten were the
same customer under a shorter name, one was the franchisee's own kit, and
two were accounts with no devices. **These are the exact commands that
settled it**, kept here so the same run on Render is a repeat rather than a
fresh investigation.

Seven merges, each moving the Cove mapping onto the established site and
leaving the new one inactive:

```
python scripts/link_sites.py --client teamlogic-it-austin --keep housing-authority-of-burnet-tx     --merge burnet-housing-authority
python scripts/link_sites.py --client teamlogic-it-austin --keep cg-s-design-and-build              --merge cg-s
python scripts/link_sites.py --client teamlogic-it-austin --keep elite-steel-and-supply             --merge elite-steel
python scripts/link_sites.py --client teamlogic-it-austin --keep ranger-protection-agency           --merge ranger-protection
python scripts/link_sites.py --client teamlogic-it-austin --keep rite-management-services           --merge rite-mgmt
python scripts/link_sites.py --client teamlogic-it-austin --keep round-rock-periodontics            --merge rr-perio
python scripts/link_sites.py --client teamlogic-it-austin --keep safequip-safety-and-fire-equipment --merge safequip
```

The franchisee's own machines, folded into the internal site they already
had:

```
python scripts/link_sites.py --client teamlogic-it-austin --keep teamlogic-it --merge tlitne-internal-devices
```

Then the rules that keep the internal and test accounts out **on every later
sync**:

```
python scripts/manage_client.py sync-rules --client teamlogic-it-austin --connector cove     --internal-prefix zz     --internal-pattern 'teamlogic' --internal-pattern 'tlitne'     --internal-pattern 'draas' --internal-pattern '^parallel$'
python scripts/sync_sites.py --client teamlogic-it-austin --connector cove
python scripts/sync_sites.py --client teamlogic-it-austin --list
```

### Deactivating a site the vendor still lists does not stick

This is the one thing to know before repeating any of it. `link_sites.py
--deactivate` takes a site out now, and **the next sync puts it straight
back**: a site whose organisation the vendor still reports is reactivated,
because that is how a customer who was switched off and then switched on
again returns. Measured on the pilot: `draas-testing` and `parallel` were
deactivated by hand and the very next sync reported `reactivate` for both.

The durable lever is the sync rules. An organisation matching
`internal_name_patterns` or an `internal_prefix` is recorded inactive every
run, so the decision holds. `--deactivate` is for a site whose organisation
the vendor has stopped listing, or a one-off before the rule is written.

`^parallel$` is in that list as a **holding position, not a finding**: the
account has no devices and nobody has yet said what it is. It is labelled
internal because internal is the only mechanism that survives a sync, which
is a limitation of the tool rather than a claim about the customer. When
Austin says what it is, drop that one pattern and re-run the two commands
above; the site reactivates by itself if it is a real customer.

## Checking it

```
python scripts/cove.py profile --client <client-slug> [--site <site-slug>]
python scripts/cove.py profile --client <client-slug> --diagnose
```

`profile` prints the estate **in counts**: how many devices, how many of them
are machines, storage, the spread of OS type, physicality, retention, last
session status and how long ago each machine last backed up successfully,
and the Microsoft 365 mailbox figures. It prints no customer, device or
person name — a probe's output outlives the probe.

`--diagnose` asks each call on its own and prints what Cove said to it: the
method, whether it answered, and the vendor's own error code and message when
it did not. That is what tells a wrong password from a role that may not read
from Cove being unavailable, three things an unlucky HTTP error makes look
identical. It finishes by asking the connector for each write method in turn
and showing it refused, which costs nothing because no request is made.

## Two things about the numbers, before anybody reads a report

**A Microsoft 365 tenant is a device.** Cove holds a customer's tenant as a
row in the device list, with no OS, no physicality and no version, and counts
it in its own device total. So "5 protected" can be four machines and a
tenant. Every figure about machines excludes it, the headline says both
numbers, and the mailbox counts come off that tenant row rather than from
anywhere else.

**A device's name carries a suffix.** Cove appends an underscore and five
characters, so `JC-LT-25` is `jc-lt-25_pqpsp` in the console. Matching a Cove
device to the same machine in another tool strips that suffix and lower-cases
the rest. It is a rule, not a certainty: a machine genuinely named
`report_x1a2b` would lose its tail, which is why anything built on this match
says "not backed up by Cove" rather than asserting a machine has no backup.

## Which machines count as a backup gap, per site

What a customer bought differs by customer: some back up servers only, some
back up everything. So the scope is a **per-site setting**, not a rule:

| `scope` | What counts as a gap |
|---|---|
| `auto` (the default) | workstations count where Cove already backs one up at that site; servers only where it does not |
| `servers` | a workstation with no Cove device is not a gap |
| `servers_and_workstations` | both count |

**`auto` is a reading, not a decision**, and the page says which way it fell
("Servers only, as no workstations are backed up here"). An explicit setting
overrides it, because the customer who has bought workstation backup and not
installed it yet is exactly the case `auto` gets wrong — and the case where
the gap list matters most. Set it once somebody has asked the customer.

Two more settings sit beside it, both per site: `no_backup_needed`, the
machines that are not expected to have one, and `coverage_reviewed`, whether
somebody has been through that site's list. Until a site is reviewed the
**customer's** copy says "Coverage review pending" rather than printing a
list nobody has checked; the coverage board and Ask show it in full either
way, because those are ours.

All three are one file:

```
cat > austin-backup.json <<'JSON'
{
  "scope": {"bay-associates": "servers"},
  "no_backup_needed": {},
  "coverage_reviewed": {}
}
JSON
python scripts/manage_client.py report-config     --client teamlogic-it-austin --backup-file austin-backup.json
```

It replaces the backup settings wholesale, like `--automation-file`, so the
file carries every site's entry and not just the one being changed. A scope
value the command does not recognise is refused rather than quietly read
back as `auto`.

**Set on the pilot, 19 October 2026**: `bay-associates` is `servers`, which
Austin confirmed. Every other site is `auto`.

## The backup window is 28 days, not a calendar month

Cove sends 28 days of per-day history on every device row and offers no way
to ask for a different span, so **every success rate in the report is the
28 days ending when the review was generated** — the state at generation,
not the review period, and the facts say so (`period_bound=False`). The page
prints the two dates rather than the words "last 28 days", because a reader
can check a date.

**Generating on the 1st or 2nd of the month makes the window line up almost
exactly with the month before**: 28 days back from 1 November is 5 October,
which is the previous month less its first four days. If somebody wants a
figure that reads as "October", generating then is the way to get closest to
it. It is still not October, and nothing in the report will claim it is.

The monthly trend is built from what each stored review measured, so its
axis is labelled by **review date and never by month name**: two reviews
generated on the 3rd and the 27th cover different windows, and putting
"September" and "October" over them would invite a comparison neither
supports.

## What is not built

- **A daily Cove snapshot.** Storing each device's per-day status once a day
  would give true calendar months and history past 28 days, and would make a
  device that was deleted mid-month still countable. It is the obvious next
  step and it is not built: today every figure comes from the 28 days Cove
  happens to be holding when the review runs.

- **No session history** beyond those 28 days. A device's per-session detail needs a second
  endpoint and a per-device token (`QuerySessions`, written up in the api
  notes). The 28-day status the main device read already carries is what a
  monthly success rate is made from.
- **No writes, and no write client.** When Cove needs one it will come
  through the automation engine like every other write, behind a switch, a
  dry run and an approval (hard rule 2).
- **Nothing has been run against a live account yet.** The fixtures are built
  from the vendor's documentation, and `docs/api-notes/cove.md` lists the
  seven things the first live run has to settle.

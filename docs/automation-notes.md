# Alert notes: the first real workflows

When a monitoring alert raises a ticket, a note workflow reads the facts
behind it from the tools we already hold credentials for and adds **one
internal note** to the Autotask ticket, so the engineer who opens it finds
what Desk found. Nothing is closed, merged, reassigned or changed. No model
is involved: the wording is the workflow's, fixed in code, and only the
values change, each with the time it was read.

This page is the operational half: what to request from the franchisee, what
keeps a note internal, what must be checked in their Autotask before a note
is posted live, how a client is rolled out, and what the first run against
the pilot needs. The design is in CLAUDE.md ("Automation: alert notes") and
the vendor findings in `docs/api-notes/autotask.md` ("Ticket notes: the one
write").

## The rules, in one place

- **The only write is adding an internal note to a ticket.** The writer
  (`app/connectors/autotask/notes.py`) is a separate client with its own
  credential, used by the automation engine and nothing else; it can send
  `GET`s, `POST …/query` and `POST /V1.0/Tickets/{id}/Notes`, and raises
  `WriteNotAllowed` before any request for anything else.
- **Internal only, read from the tenant's own field information.** Autotask's
  `publish` picklist is per tenant. The writer reads it and sends only the
  value labelled **Internal Project Team**, the one audience that is internal
  resources alone. It refuses a label that names the client, the portal,
  co-managed users or "all", whatever its number; it refuses to post when the
  tenant has no such label; and it never falls back to a wider audience.
  `noteType` is read the same way (`Task Notes`, `Task Detail` or `Task
  Summary`, never `Client Portal Note`). `tests/connectors/test_autotask_notes.py`
  asserts the sent value against the field information and fails on any other.
- **At most one note per workflow per ticket**, kept by a unique index.
- **A fact that cannot be read is one line saying so**; a missing core fact
  means no note.
- **No AI**: `tests/automation/test_imports.py` fails the suite if anything
  under `app/automation/` could reach the agent or the model.

## The Autotask API user to request

A **second** Autotask API user, never the one the read connector uses, so
the read path stays provably read-only and the write path can be switched off
on its own by removing one credential. Ask the franchisee's Autotask
administrator for:

1. **Admin → Resources/Users (HR) → New API User.** Name it so the notes it
   writes are recognisable in the ticket history (for example `Desk Notes
   (API)`); the note's `creatorResourceID` is this user and is what their
   engineers will see as the author.
2. **Security level: a copy of "API User (system)"** narrowed to what the
   writer needs. The writer makes three kinds of request, and the security
   level must allow exactly these:
   - *Service Desk → Tickets: view* for every ticket, because the writer
     reads a ticket before it writes and the poll reads the queue.
   - *Service Desk → Ticket notes: add.* Edit and delete are not needed and
     should be off; the writer never sends them, and a level that forbids
     them is one more line of defence.
   - *Field information* (`entityInformation/fields`) needs no extra right.
   - Everything else off: no ticket edit, no time entries, no contacts, no
     configuration items, no CRM, no projects, no contracts.
3. **API Tracking Identifier:** the integration code goes in the credential
   as `api_integration_code`, beside the username and secret.
4. **Store it** with the connector name `autotask_notes`:

   ```bash
   python scripts/manage_client.py set-credentials --client <slug> --connector autotask_notes
   # prompts for api_integration_code, username, secret; base_url is optional
   ```

   A demo client refuses it. A live client with no `autotask_notes` credential
   cannot build a writer: the pages show the note, Post fails with a reason,
   and auto-post marks the note `post_failed` rather than guessing a route.

## Notifications: what the franchisee must check before any live post

The TicketNotes entity has **no field that sends or suppresses a
notification** (`docs/api-notes/autotask.md`: the writable fields are the
ticket, title, description, type and publish). So adding a note through the
API can still make Autotask send email, through two mechanisms the tenant
owns, and the writer can do nothing about either. Before `Post to Autotask`
is pressed on a live ticket, and again before any workflow is switched to
auto-post, the franchisee's Autotask administrator checks:

1. **Workflow rules on a note being added.** *Admin → Features & Settings →
   Application-wide (Shared) Features → Workflow Rules* (Service Desk tab).
   For every active rule whose **Event** is *Ticket Note added* (or whose
   conditions include a note being created, or "any update"): what it does.
   A rule that emails the ticket's contact, the account manager or a
   distribution list on every note will fire on ours. Acceptable outcomes
   are: the rule's conditions exclude notes **published to Internal Project
   Team**, or exclude the Desk API user as the note's creator, or the rule
   is agreed to fire. Record which rules were looked at and what was decided
   in `docs/validation-log.md`.
2. **Ticket note notification defaults.** *Admin → Features & Settings →
   Service Desk (Tickets) → Notifications* (default notification settings
   for ticket notes, and the ticket categories' note defaults). These decide
   which recipients are pre-ticked when a **person** adds a note in the UI.
   A note added through the API does not go through that form, so they are
   expected not to apply — but the first live note is the test of that: look
   at the ticket's **Notification History** after it lands and record what,
   if anything, was sent.
3. **The Client Access Portal.** On a ticket the client can see, confirm the
   first live note is **absent** from the portal view and from the co-managed
   view. Record it in `docs/api-notes/autotask.md` under "Not yet verified
   live".
4. **The API user's own notifications.** The API user should have no email
   address that forwards anywhere, so a rule that notifies "the resource who
   added the note" reaches nobody.

Until 1 to 3 are recorded for a tenant, the only safe setting is the default:
every workflow in **preview**, posting by Inputiv staff only, one note at a
time, each checked.

## Rollout: modes, who posts, the kill switch

Every note workflow has a **mode per client**, set on the Automations page
by Inputiv staff and shown on the workflow page:

- **Preview** (the default): the note is built and stored in our database and
  shown on the ticket page as *Desk note, not posted*, with its facts and the
  time each was read. **Post to Autotask** sends it as an internal note
  (in Autotask the author is the Desk API user; Desk records which engineer
  posted it) and is that engineer's approval of it; **Not useful** dismisses
  it. Both are audited with who.
- **Auto-post**: every note the workflow builds is posted at once by the
  poll. Switching to it needs the **franchisee's approver recorded** — their
  name, how they agreed (email, call, meeting) and when — and the name must
  be on the client's approver list (`automation.approvers`). Switching back
  to preview is immediate and needs nobody. The approval rule for the mode in
  force is printed on the workflow page and under the note on the ticket.
- **The kill switch** (Automations page, *Halt all automation for this client*) stops all
  posting in every mode: Post is refused, auto-post marks the note built and
  not posted. Clearing it resumes nothing retroactively.

**Who may post.** *Post to Autotask* and *Not useful* are **Inputiv staff
only** by default, and a franchisee user sees no Desk note card on a ticket
at all. The per-client setting `automation.notes.franchisee_can_post`
(default false) opens both, and the card, to the franchisee's administrators
and technicians as well; viewers never see it:

```bash
python scripts/manage_client.py report-config --client <slug> --franchisee-can-post
python scripts/manage_client.py report-config --client <slug> --inputiv-posts-only   # back to the default
```

The order for a new client: preview with Inputiv posting → the notification
checks recorded → `franchisee_can_post` if their engineers want the button →
auto-post per workflow, with the approver recorded, starting with the
workflows whose notes were posted most and marked not useful least (the Notes
view on the Automations page has the figures).

## The workflows

| Workflow | Trigger (alert ticket whose title…) | Reads | Writes |
|---|---|---|---|
| Device back online | says offline / down / not responding | NinjaOne device state, last contact, last boot, open alerts | one note |
| Repeat alert on the same device | any alert | Autotask: the device's tickets in 7 days (device by the identity layer; no note unless matched confidently; the device's name is taken out of the title before alert kinds are compared, so `CON-LT-044` is never a repeat of `CON-LT-043`) | one note |
| Disk space | says disk / free space / volume | NinjaOne volumes, device state, disk alerts in 30 days | one note |
| Antivirus / EDR threat | says malware / threat / quarantine / ransomware / Blackpoint / MDR | Bitdefender endpoint state and detections in 24 h; a Blackpoint note always ends "MDR alert: review required, do not close without investigation." | one note |
| Backup notices | says backup / Veeam / N-able / Cove / [Success] [Warning] [Failed] | the ticket's own text, parsed by fixed Veeam and N-able rules; an unknown format gets no note | one note |
| Chronic devices | every 30 days | Autotask alert tickets in the period, each resolved to a device | nothing in Autotask; a page in Desk |

Per-client values a workflow uses (thresholds, phrases, label overrides) go
under `automation.workflows.<name>` in the client's settings
(`manage_client.py report-config --automation-file`).

## Backfill and live

Everything runs through the triggers' poll and the one setting it already
has, `AUTOMATION_TRIGGERS_LIVE` (`docs/deploy.md`, 5b). The poll reads the
new alert tickets, builds the notes for the workflows whose switch and
trigger switch are on, posts them only in auto-post mode, builds the chronic
report on its schedule, and records when noted tickets close for the
measurement.

```bash
# What the open alert queue would get: every note printed, nothing stored.
AUTOMATION_TRIGGERS_LIVE=true python scripts/automation.py notes-backfill --client <slug> --dry-run
# Build them as preview notes (never posted, whatever the mode).
AUTOMATION_TRIGGERS_LIVE=true python scripts/automation.py notes-backfill --client <slug>
# The ordinary pass, as the cron runs it.
AUTOMATION_TRIGGERS_LIVE=true python scripts/automation.py poll --client <slug> --once
```

Every vendor read is a `tool_call` audit row; every note built, posted or
dismissed is an `automation_note` row; every Autotask request spends the
same hourly budget as the forward shadow.

## Measurement

Per note: built, posted (by whom, or auto), not useful, and the ticket's time
to close. Per workflow, on the Notes view: notes built, the share posted
while in preview, the share marked not useful, and the median time to close
of tickets with a note against those without, per week. A workflow whose
notes are mostly dismissed is switched off, not reworded on a guess.

## Running against Austin, in order

1. Request the second API user (above) and store it as `autotask_notes`.
2. `notes-backfill --client teamlogic-it-austin --dry-run` and read every
   note it prints; nothing is stored.
3. Turn the workflows and their triggers on for the client (Automations
   page); every mode stays preview.
4. `notes-backfill` without `--dry-run`, then open a few tickets and read the
   notes beside the tickets.
5. The notification checks 1 to 4 above, recorded in the validation log.
6. One `Post to Autotask` by an Inputiv engineer on a ticket the client can
   see; check the portal, the co-managed view and the ticket's notification
   history; record the result in `docs/api-notes/autotask.md`.
7. Uncomment the poll cron with `AUTOMATION_TRIGGERS_LIVE=true` on that
   service only; the web service's value stays false.
8. After a fortnight of preview, read the Notes view and decide
   `franchisee_can_post` and, per workflow, auto-post with the approver
   recorded.

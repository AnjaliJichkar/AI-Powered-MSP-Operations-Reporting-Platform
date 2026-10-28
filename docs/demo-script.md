# The five-minute booth demo

For the demo tenant (`tailwind-it-demo`, see [demo-tenant.md](demo-tenant.md)).
Every number below is what the generated dataset holds, so you can say it
before the screen shows it. Nothing here touches a vendor: the connectors are
the fakes in `app/demo/`, and the only thing that leaves the process is the
model call and, on the last question, a web search.

**Sign in as** `demo-admin@tailwind-it.example`. The viewer account
(`demo-viewer@…`) is for the role beat at the end, if you get that far.

---

## Before anyone is standing there (two minutes, once)

Run these in order. They warm every cache the demo touches, and each one
tells you the demo is sound before you promise anything.

```bash
python scripts/demo_tenant.py create          # idempotent; prints the invite links
python scripts/collect_events.py --client tailwind-it-demo
python scripts/selection_check.py --client tailwind-it-demo \
    --questions tests/selection/demo_questions.yaml --execute
```

The last one runs the eight booth questions to full answers. It takes about
three minutes, must end `8 of 8 correct`, and leaves the NinjaOne, GravityZone
and ConnectSecure inventories cached, so the first question at the booth
answers in about 15 seconds instead of 35.

**Then open the Coverage page and leave it open.** That is the page whose
first load is a database read of the stored board (8 ms) and whose numbers you
quote in the opening line. Leaving it open is also the safest place to be
standing when somebody arrives.

If `selection_check` does not end `8 of 8`, the demo still works: the tools it
picks are all defensible. Do not re-run it in front of anyone.

---

## The walkthrough

### 0. The opening line, before you click anything (15 seconds)

> "This is a managed service provider with eight customers and 254 managed
> computers. One screen tells them which machines are missing a tool. Nobody
> has to ask the customer."

You are already on **Coverage**.

### 1. Coverage: the board (60 seconds)

**Click** nothing yet. Point at the site rows:

| Site | What to say |
|---|---|
| Northwind Traders | "Three machines have no protection agent at all. One of those three has not checked in for nine days: unprotected and nobody has noticed." |
| Contoso Ltd | "Two machines the RMM has lost. One of them GravityZone heard from this morning, so the machine is alive and the RMM agent is dead. That is a different job from the other one." |
| Fourth Coffee | "No vulnerability scanner at this customer at all. It says 'not assessed', not zero. A zero would be a lie." |
| Tailspin Toys | "Two machines share one name and nothing tells them apart, so we refuse to guess: both show as unmatched, each naming the other." |

**Click Refresh.** It recomputes and stores the board in about 200 ms, and the
page says when it was taken and by whom.

> "That button went to four vendors, joined 254 machines and came back. The
> page itself is a database read; nobody waits for a vendor."

**Fallback.** If Refresh spins, keep talking: the page you are looking at is
already the stored board from the daily collector, and its timestamp is on
screen. Nothing is missing.

### 2. Devices: one machine across four vendors (45 seconds)

**Click** the Northwind row, then the device `NWT-WS-001` (the one with no
protection agent).

> "Every vendor's answer for one machine, each asked by the name that vendor
> knows it as. GravityZone has no record of this one at all, and the page says
> so rather than leaving a blank."

**Click** `CON-DC01` at Contoso for the opposite: NinjaOne, GravityZone,
ConnectSecure and Autotask all answer, in about 160 ms.

**Fallback.** If a vendor block shows an error, that is the demo working:
say so, and point out that an error is shown as an error and never as "no
findings".

### 3. Ask: two questions (90 seconds)

**Click Ask.** Type, exactly:

> `anything wrong at Northwind today?`

About 15 seconds. It calls four tools across two vendors and comes back with
open alerts, the SLA-breached tickets, and the machine that has not checked in
for nine days.

> "It picked the tools. We did not write a Northwind report; there is no
> Northwind report. Every number in that answer has an audit row behind it."

Then, without clearing the thread:

> `which devices have no working protection and haven't checked in for a week?`

About 35 seconds, because it joins two lists. The answer names one machine and
explains what it ruled out, including that Defender showing "off" fleet-wide is
expected alongside GravityZone.

**Fallback.** If a question runs long, say what it is doing ("it is reading
the protection list and the check-in list and joining them") — the tool calls
appear on screen as they happen, so there is something to point at. If it
fails outright, the third question below is the short, reliable one:
`which patches failed this week?` answers in about 12 seconds from one tool.

### 4. Reports: the business review (75 seconds)

**Click Reports**, then the **Contoso Ltd, September 2026** review. It is
already released, so the deck and the PDF are both there.

Point at the scorecard on Key Takeaways:

> "Service, Devices, Patching, Security, each good, attention or action, by
> rules in one file, not by the model. The arrows compare with August, and up
> always means better."

Then the sentences underneath:

> "'Windows patch installations that failed in the period went from 10 to 5.'
> That is the previous month's stored review, not a memory."

**Click Download** for the deck, and **Download PDF** for the ten-page Monthly
Report.

> "Same facts, two documents. Nothing on either was written by a model without
> an engineer approving it, and the render refuses if a placeholder survives."

**Fallback.** If a download is slow, the review is on screen; read the
scorecard from it. Do not generate a new review at the booth: it takes about a
minute and calls the model for every section.

### 5. Tickets: the plan (45 seconds, only if you have it)

**Click Tickets**, open `T20260111` (Contoso, a laptop running slowly).

> "It found the machine from the ticket text, at high confidence, and said how.
> Then ten steps, every one citing either a fact we hold about that machine or
> a ticket we resolved before. A step that cannot cite anything is dropped, not
> shown."

**Fallback.** The plan is already generated and stored; you are reading it, not
making it. If you want a fresh one it takes about 20 seconds.

### 6. Roles, if asked (20 seconds)

Sign in as `demo-viewer@tailwind-it.example`:

> "A viewer sees the released reviews and nothing else. No Ask, no Coverage
> actions. Their client comes from their account; there is no client picker,
> because there is nothing else for them to pick."

Then `demo-tech@tailwind-it.example`, if they ask who actually uses it daily:

> "A technician has the work and not the account: Ask, the devices, the
> coverage board including Refresh and excluding a machine, and the ticket
> workspace. No user administration, and reviews read-only, the same as the
> viewer. Nobody has to hand out an administrator account to get the job done."

---

## The question that always lands

If somebody asks "is this real data?", the honest answer is the demo:

> `what is KB5063878 and are there known issues?`

About 17 seconds. It leaves the tenant, searches the web, and comes back with
the August 2025 Windows 11 cumulative update and its known issues, with links.
Then: "the customer data here is generated and the vendors are simulated. That
KB is real, and so is the search. The engine knows the difference between what
it holds and what it had to go and look up."

---

## What to do when something is slow

| Symptom | What is happening | What to say and do |
|---|---|---|
| First question takes 35 s | The vendor caches are cold | Run the warm-up above. At the booth: keep talking through the tool calls appearing on screen. |
| Coverage Refresh spins | A snapshot is being recomputed | The page is already showing the stored board; point at its timestamp. |
| A review will not render | Something in the review changed since it was rendered | Every edit makes the deck stale on purpose. Say that, and open a different site's review. |
| Ask returns an error | The model call failed | Ask the patch question instead; it is one tool and twelve seconds. |
| A page is empty | You are signed in as the viewer | Only released reviews are theirs to see. Sign back in as the admin. |

## Resetting between days

```bash
python scripts/demo_tenant.py reset
```

Deletes what the tenant accumulated (reviews, snapshots, workspaces, plans,
exclusions) and syncs the sites again from the generated world. The accounts
and the audit trail stay. Then run the warm-up again.

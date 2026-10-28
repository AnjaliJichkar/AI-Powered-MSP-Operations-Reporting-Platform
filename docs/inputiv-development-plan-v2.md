# Inputiv Development Plan v2
**Desk → Resolved → Memory** | September 2026 – June 2027

Revised for: self-funding from Phase 1, TeamLogic IT franchise strategy (20 of 350+ locations served), and the assumptions identified in the pre-mortem.

---

## 1. What changed from v1

| Area | v1 | v2 |
|---|---|---|
| Unfunded spend | 2 backend + 1 frontend for 9 months | 1 backend engineer for 13 weeks; everything after is gated on measured results |
| First revenue | March 2027, after full Resolved build | January 2027, per-outcome or per-user guarantee sold to pilots on Desk + humans |
| Resolved build order | Features first, billing last | SLA engine and billing first, features in cash-flow order |
| Memory Tier 2 | Anonymised cross-MSP pooled corpus, Q2 | **TeamLogic network knowledge** first (one brand, one consent), cross-MSP version deferred until demand is proven |
| Pilot MSPs | Any two fully-populated clients | Two TeamLogic franchisees, results written as a network case study |
| Franchisor | Not in plan | Dedicated track starting this month |
| Connector scope | Six tools matching TeamLogic's stack | ConnectWise adapter added in Phase 2 to keep the product portable |
| Concentration | Not addressed | Explicit diversification target and gate |
| Pre-mortem tests | None | Four cheap tests in week 1 that decide the most expensive assumptions |

---

## 2. Products and sequence

| Product | Ships | What it is | Buyer |
|---|---|---|---|
| **Desk** | Dec 2026 | Read-only cross-tool agent over Autotask, NinjaOne, Bitdefender, ConnectSecure, Cove, Blackpoint | Inputiv engineers only; never sold |
| **Resolved** | Jan 2027 (guarantee) → Mar 2027 (full) | AI triage in front of a guaranteed human resolution, priced per outcome or per user | MSP owners, starting with TeamLogic franchisees |
| **Memory** | Q1 2027 (TeamLogic network) → Q2 2027 (single-tenant for others) | Structured resolution knowledge from Inputiv-closed tickets | MSP technicians; franchisor; standalone buyers |
| TBR | Ongoing | Monthly proof-of-value narrative from the same engine | Renewal artefact |

Each phase ends with a gate. If the gate fails, the stated fallback applies and the next phase does not start.

---

## 3. Week 1 tests (Sep 8–12) — run before any code

These decide whether the plan's expensive assumptions hold. Total cost: a few days of one person's time.

| Test | Method | Threshold | If it fails |
|---|---|---|---|
| **Closing-note quality** | Sample 200 closed TeamLogic tickets; count those with a recorded root cause and fix steps | ≥ 50% | Memory moves to Q3; add a mandatory structured close form to the desk now so the corpus improves from today |
| **Baseline handling time** | Pull Autotask time entries per ticket class for the two pilot franchisees, last 90 days | Establish the number | None — this is the denominator for Gate 1 |
| **Pricing preference** | Ask 5 franchisee owners: per-user or per-outcome at the same expected monthly cost | Note the split | If 4+ choose per-user, the guarantee is the product and per-outcome is an option, not the headline |
| **Revenue per resolved ticket** | Current retainer revenue ÷ resolved tickets, per client | Establish the number | If below the planned $10–15 per-outcome price, price up or structure as retainer plus overage |

---

## 4. Team

| Role | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Backend engineer (Python/FastAPI, Claude Code) | 1 | 2 (second hire only after Gate 1) | 2 |
| Frontend engineer | 0 (Slack bot only) | 0.5 → 1 after first Resolved invoice | 1 |
| Product owner (founder) | 0.5 | 0.5 | 0.5 |
| L3 engineer as validator | 0.25 | 0.25 | 0.5 (Memory quality) |
| Compliance lead | 0.25 | 0.25 | 0.25 |
| External counsel | Network-sharing addendum | MSA amendment (per-outcome, SLA) | Cross-MSP consent (deferred) |

**Claude Code conventions, set in week 1:** `CLAUDE.md` at repo root with architecture, per-client scoping rule, read-only constraint, connector template. No secrets in code.

---

## 5. Phase 1 — Desk (Sep 15 – Dec 12, 2026)

**Goal:** a read-only cross-tool agent used daily by three Inputiv engineers on two TeamLogic franchisees, with measured handling-time reduction that pays for the engineer.

### Engineering (one backend engineer)

**Weeks 1–2 (Sep 15–26): Scope and skeleton**
- Collect the 20 most common cross-console questions from three engineers; rank by frequency × time.
- FastAPI skeleton: `clients` (cross-tool ID map), encrypted `credentials` keyed by (client, tool), `audit_log`, Alembic.
- Confirm API access and scopes for all six tools on both pilot franchisees.
- `CLAUDE.md` and connector template: `httpx`, Pydantic models, pagination, retry, `client_id` enforced on every call.

**Weeks 3–4 (Sep 29 – Oct 10): NinjaOne connector + chat loop**
- NinjaOne end to end; recorded fixtures as tests.
- `/chat` endpoint: prompt + tools → Claude API → tool-call loop → streamed answer. Per-request tool-call cap, timeouts, structured errors.
- Slack bot as the only interface. Internal demo end of week 4.

**Weeks 5–7 (Oct 13–31): Remaining connectors**
- Autotask → Bitdefender → Cove → ConnectSecure → Blackpoint.
- Each: fixture tests plus a 30-minute live validation with an L3 engineer.

**Weeks 8–9 (Nov 3–14): Hardening**
- Rate limits, token refresh under concurrency, pagination edges.
- Safety tests: cross-client prompts fail; expired tokens surface; model never selects credentials.
- System prompt v1; prompt logging with thumbs up/down in Slack.

**Weeks 10–13 (Nov 17 – Dec 12): Shadow pilot**
- Three engineers use Desk on every ticket for the two franchisees.
- Weekly prompt review: right / wrong / useless; fix top three failures weekly.
- Handling time measured against the week-1 baseline.

### Franchisor track (founder)
- **Sep:** Ask the two or three most respected franchisee owners for an introduction to TeamLogic corporate (operations or technology lead, or National Accounts). Introductory meeting framed as "we support 20 of your locations and want to align with your standards."
- **Sep:** Identify franchisee advisory council members; find which of the 20 is closest to one.
- **Sep:** Get the date of the next national conference and vendor sponsorship terms.
- **Oct:** One-page network-sharing addendum drafted by counsel; sent to all 20 franchisees for signature. This unlocks Memory Tier 2 (TeamLogic) with no anonymisation work.
- **Nov:** Draft the franchisor case study structure so the Desk pilot data lands in it directly.

### Compliance
- **Week 1:** Written notice to pilot franchisees of the internal AI pilot (read-only, logged, no model training).
- **Week 2:** SOC 2 system description updated; scope ticket opened with auditor.
- **Weeks 1–4:** API partner/terms requests to Datto, NinjaOne, Bitdefender, ConnectSecure, N-able, Blackpoint. Document rate limits and AI-use clauses.

### Gate 1 (Dec 12)
**Pass:** ≥ 15% reduction in handling time on Desk-assisted tickets (lowered from 20% pending the baseline), ≥ 80% answers rated right or useful, engineers ask to keep it.
**Fail:** Desk stays internal, no second engineer hired, January spent on top failure modes. The January guarantee offer still proceeds because it depends on humans plus SLA, not on AI.

---

## 6. Phase 2 — Resolved (Dec 15, 2026 – Mar 27, 2027)

**Goal:** guaranteed resolution billing from January; full AI stack by March; five MSPs live, at least two outside TeamLogic.

### Commercial first (Dec 15 – Jan 16)
- **Dec:** Pricing sheet from week-1 numbers: per-outcome tiers by ticket class and a per-user alternative. Margin modelled at 5%, 15%, 25% AI no-touch rates; plan on 5%.
- **Dec:** MSA amendment: per-outcome billing, SLA guarantee ("human pickup within SLA or the ticket is free"), 60-day payback clause.
- **Jan:** Offer the guarantee to both pilot franchisees. Delivery is Desk plus humans plus a manual SLA tracker. Invoice on the January cycle.
- **Jan:** Offer to the remaining 18 franchisees at renewal; offer to all 7 non-TeamLogic clients.

### Engineering (second backend engineer joins after Gate 1)

**Weeks 1–3 (Dec 15 – Jan 9): SLA engine and billing**
- SLA tracker: pickup deadline per ticket, human-assignment timestamp, breach flag, weekly SLA report.
- Billing export: resolved-ticket count and per-user count, reconciled to Autotask.
- These ship before any AI feature because they are what gets invoiced.

**Weeks 4–6 (Jan 12–30): Intake and enrichment**
- Ticket ingest from Autotask (webhook or polling).
- Classifier: category, urgency, sentiment, duplicate detection.
- Auto-enrichment on ticket creation: device state, recent alerts, backup status, open vulnerabilities written to an internal note. This is Desk on autopilot and the biggest per-ticket saver.

**Weeks 7–9 (Feb 2–20): Suggested resolution**
- Retrieval over Inputiv's closed-ticket history (the seed of Memory); suggested fix with confidence score.
- Thresholds: high → "likely fix"; medium → options; low → nothing, straight to human.
- Engineer accept/edit/reject logged for tuning.

**Weeks 10–12 (Feb 23 – Mar 13): Owner dashboard and ConnectWise adapter**
- Dashboard: tickets closed by AI-assisted automation, by Inputiv, by the MSP's team; MTTR; SLA hit rate; CSAT; cost per ticket; hours returned.
- ConnectWise PSA adapter (ingest, tickets, time entries) so non-TeamLogic deals are not blocked by stack.

**Weeks 13–15 (Mar 16–27): Automations with approval (only if margin is trending up)**
- Five allow-listed actions: password reset, MFA reset, group membership, disk cleanup, service restart. Each requires engineer approval; result written to the ticket. Nothing else in 2027.

### Franchisor track
- **Jan:** Formal preferred-vendor pitch to TeamLogic corporate: 20 franchisee references, Desk pilot results, network knowledge demo (see Phase 3). Position alongside Thread as the AI front door, not against it. Ask: preferred-vendor listing for outsourced support and network knowledge.
- **Q1:** Conference presence with franchisee references if the date falls in this window.

### Compliance
- SOC 2 scope for end-user messaging and (if built) write automations.

### Gate 2 (Mar 27)
**Pass:** SLA hit rate ≥ 95%; per-ticket gross margin above current retainer margin; ≥ 3 of 5 pilots renewed at 60 days; ≥ 2 non-TeamLogic MSPs live.
**Fail on margin:** revert commercial customers to retainer pricing; keep the AI layer as an internal cost reducer.
**Fail on diversification only:** proceed, but no further TeamLogic-specific features until two non-TeamLogic MSPs are signed.

---

## 7. Phase 3 — Memory (Jan 2027 start for TeamLogic; Mar 30 – Jun 26 for general release)

**Goal:** TeamLogic network knowledge live in Q1 as the centrepiece of the franchisor pitch; single-tenant Memory for all Resolved customers by June; standalone seat sold to at least two MSPs.

### Track A: TeamLogic network knowledge (Jan 5 – Mar 13, in parallel with Resolved)
Starts only if the week-1 closing-note test passed and network-sharing addenda are signed by ≥ 12 of 20 franchisees.

- **Jan:** Extraction pipeline over all TeamLogic tickets: symptom, environment, root cause, fix, time to resolve, what failed. L3 samples 100/week; 90% accuracy before exposure.
- **Feb:** "Seen this before?" in the ticket view and via Teams for Inputiv engineers, then for franchisee technicians. Playbook clusters for the TeamLogic stack (NinjaOne, Bitdefender, Cove, ConnectSecure, Blackpoint, M365).
- **Mar:** Franchisor demo: "new locations start with the accumulated fixes from 20 offices." Regression watch feeding TBR.

### Track B: General Memory (Mar 30 – Jun 26)
- **Weeks 1–3:** Extend extraction to non-TeamLogic tickets; single-tenant scoping per MSP.
- **Weeks 4–6:** Runbook export to IT Glue and Hudu; gaps report; browser sidebar.
- **Weeks 7–9:** Standalone "L3 knowledge access" seat; read-only API with per-MSP keys; TBR "what changed since last review" section.
- **Weeks 10–13:** Launch to non-Resolved MSPs at $40–60 per tech per month.

### Deferred: anonymised cross-MSP pooled tier
Not built until at least two standalone seats are sold or Resolved is at ten non-TeamLogic MSPs. Opt-in clause still goes into every MSA renewal from January because it costs nothing.

### Gate 3 (Jun 26)
**Pass:** ≥ 25% MTTR reduction on tickets where a Memory match was shown; TeamLogic network knowledge in use by ≥ 15 franchisees; ≥ 2 standalone seats sold.
**Fail on MTTR:** Memory stays bundled and free; standalone postponed.
**Fail on franchisee adoption:** keep as an Inputiv-internal tool; revisit after franchisor decision.

---

## 8. Concentration and diversification

TeamLogic franchisees are 20 of 27 clients. A single franchisor decision could endorse Inputiv, ignore it, or mandate a competitor. The plan must survive all three.

| Measure | Target | Review |
|---|---|---|
| Non-TeamLogic MSPs signed | ≥ 5 new by Jun 2027 | Monthly |
| Share of revenue from TeamLogic network | Declining trend from current level; hard ceiling set by founder | Quarterly |
| Stack portability | ConnectWise adapter live by Mar 2027; Datto RMM scoped | Gate 2 |
| Contract and IP hygiene | Data rights, IP ownership, and termination terms reviewed by counsel before the franchisor pitch | Dec 2026 |

Expansion inside the network is the easiest revenue available and is pursued fully; the measures above exist because it is also the easiest way to become captive.

---

## 9. Architecture principles (fixed until July 2027)

- One agent engine, three products; no product gets its own connector layer.
- Server-side client scoping on every call; the model never selects credentials.
- Read-only by default; write actions are an explicit allow-list with human approval; no endpoint remediation in 2027.
- Cache normalised data from every integration so a vendor API change degrades rather than breaks.

## 10. What is not being built
- Standalone AI triage sold as software.
- A client portal.
- Any PSA or RMM replacement.
- Autonomous endpoint remediation.
- Anonymised cross-MSP knowledge before demand is proven.

---

## 11. Milestone calendar

| Date | Milestone |
|---|---|
| Sep 8–12, 2026 | Week-1 tests complete; franchisor introduction requested |
| Sep 15 | Phase 1 start; API partner requests sent |
| Oct 10 | NinjaOne connector + Slack chat loop demo |
| Oct 31 | All six connectors complete; network-sharing addendum sent to 20 franchisees |
| Nov 17 | Shadow pilot begins |
| **Dec 12** | **Gate 1: Desk** |
| Jan 5, 2027 | TeamLogic network knowledge extraction starts (if tests passed) |
| Jan 9 | SLA engine and billing export live |
| Jan 16 | First guarantee invoices issued; formal franchisor pitch scheduled |
| Jan 30 | Intake and enrichment live |
| Feb 20 | Suggested resolution live |
| Mar 13 | Dashboard, ConnectWise adapter, network knowledge demo complete |
| **Mar 27** | **Gate 2: Resolved** |
| May 8 | Single-tenant Memory for all Resolved customers |
| **Jun 26** | **Gate 3: Memory launch** |

---

## 12. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Franchisor mandates Thread + a competing desk as national standard | Medium | Early introduction; 20 references; network knowledge as the thing competitors can't offer; diversification targets |
| Closing notes too thin for Memory | Medium-high | Week-1 test; structured close form from day one |
| Desk saving below 15% | Medium | Baseline measured first; January revenue does not depend on it |
| Franchisees prefer per-user pricing | Medium | Week-1 test; guarantee is the product either way |
| Vendor API restriction (Datto/Autotask most exposed) | Medium | Partner requests week 1; normalised cache; ConnectWise adapter |
| AI no-touch stalls at ~5% | High | Pricing modelled at 5%; margin from human efficiency |
| Bad automation on a franchisee tenant spreads through the network | Low, severe | Five-action allow-list, approval, audit trail; automations last and optional |
| Product becomes TeamLogic-only | Medium | ConnectWise adapter in Phase 2; diversification gate |
| Franchisor seeks exclusivity or acquisition on its terms | Low-medium | Contracts, IP, data rights cleaned up before the pitch |

---

## 13. First two weeks checklist

**Week of Sep 8**
1. Run the four week-1 tests (closing notes, baseline time, pricing preference, revenue per ticket).
2. Ask two or three franchisee owners for a franchisor introduction.
3. Identify advisory council members; get the conference date.
4. Brief counsel: network-sharing addendum; contract/IP review scope.

**Week of Sep 15**
5. Choose the two pilot franchisees; name the three engineers.
6. Send API partner and terms requests to all six vendors.
7. Notify pilot franchisees of the internal AI pilot.
8. Create the repo, `CLAUDE.md`, FastAPI skeleton, connector template.
9. Open the SOC 2 scope ticket.
10. Add a structured close form (root cause, fix, what failed) to the desk workflow so every ticket from today feeds Memory.

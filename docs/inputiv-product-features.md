# Inputiv Product Feature Reference

Four products on one engine. Features are tagged with the phase they ship in (P1 = Desk, Sep–Dec 2026; P2 = Resolved, Dec 2026–Mar 2027; P3 = Memory, Jan–Jun 2027; Later = after Gate 3).

---

## 1. Desk — the internal cross-tool agent

**Who uses it:** Inputiv engineers only. Never sold. Everything else runs on it.

### Connectors
- **NinjaOne** — devices, check-in status, alerts, patch status, software inventory, scripts (read) *(P1)*
- **Autotask** — tickets, companies, contacts, configuration items, time entries, SLA fields *(P1)*
- **Bitdefender GravityZone** — endpoint protection status, detections, quarantine, policy assignment *(P1)*
- **N-able Cove** — backup jobs, failures, last successful backup, storage, restore points *(P1)*
- **ConnectSecure** — vulnerabilities by asset, compliance findings, external scan results *(P1)*
- **Blackpoint** — MDR alerts, incidents, response actions taken *(P1)*
- **Microsoft 365 / Entra** — users, licences, MFA status, conditional access, admin roles (from TBR) *(P1)*
- **ConnectWise PSA** — tickets, companies, time entries *(P2)*
- **Datto RMM** — scoped, not built *(Later)*

### Query engine
- Plain-English question → answer, via Slack bot; tool calls chosen by the model from a curated list *(P1)*
- Cross-tool joins in one question ("endpoints with a critical ConnectSecure finding and a failed Cove backup") *(P1)*
- Per-client scoping enforced server-side; the model never selects credentials *(P1)*
- Tool-call cap per request, timeouts, structured errors, graceful degradation when an API is down *(P1)*
- Output formatting: tables for lists, prose for summaries, links back to the source console *(P1)*
- Saved questions (canned prompts) for the 20 most common queries *(P1)*
- In-ticket panel inside TBR or the PSA *(P2)*

### Governance
- Audit log of every prompt, tool call, result, and user *(P1)*
- Thumbs up/down on every answer, reviewed weekly *(P1)*
- Read-only by default; write actions only via the Resolved allow-list *(P1)*
- Normalised cache of every integration so a vendor API change degrades rather than breaks *(P1)*
- Per-client data-access notice and SOC 2 scope coverage *(P1)*

---

## 2. Resolved — guaranteed resolution with AI in front

**Who buys it:** MSP owners, starting with TeamLogic franchisees. Priced per resolved ticket (by class) or per user, with a human-pickup SLA guarantee.

### Intake
- Ticket ingest from Autotask (webhook/polling); ConnectWise in P2 *(P2)*
- Multi-channel front door: email, Teams, Slack, MSP client portal, phone via existing desk *(P2)*
- Classification on arrival: category, urgency, sentiment, affected client *(P2)*
- Duplicate and outage detection: merges tickets from the same incident *(P2)*
- Missing-information prompt: asks the end user for details before an engineer is assigned *(P2)*
- VIP and priority routing rules per MSP *(P2)*

### First pass (automated)
- Auto-enrichment note on every ticket: device state, recent alerts, endpoint protection, backup status, open vulnerabilities, recent M365 changes *(P2)*
- Suggested resolution from Inputiv's closed-ticket history, with a confidence score *(P2)*
- Confidence thresholds: high → "likely fix"; medium → options; low → straight to human *(P2)*
- Engineer accept / edit / reject on every suggestion, logged for tuning *(P2)*
- Safe automations with mandatory engineer approval — five actions only in 2027: password reset, MFA reset, group membership, disk cleanup, service restart *(P2, optional)*
- Auto-drafted closing note and time entry written back to the PSA *(P2)*
- Structured close form: root cause, fix, what failed (feeds Memory) *(P1 for engineers, P2 for product)*

### Human tier and guarantee
- Guaranteed engineer pickup within SLA, tracked per ticket with breach flags *(P2, first)*
- Named escalation path to Inputiv L3 *(P2)*
- Engineers see the AI's full context and reasoning at handoff *(P2)*
- 24/7 coverage; after-hours and weekend cover as a separate SKU *(P2)*
- Fractional L3 retainer: block of senior escalation hours per month *(P2 packaging, no build)*
- "Human pickup within SLA or the ticket is free" *(P2)*

### Owner-facing
- Owner dashboard: tickets closed by AI-assisted automation, by Inputiv, by the MSP's own team; MTTR; SLA hit rate; CSAT; cost per ticket trend *(P2)*
- Monthly "hours returned" report, expressed as headcount not hired *(P2)*
- Billing export reconciled to the PSA; per-outcome and per-user statements *(P2, first)*
- Per-client view so the owner can show their own customers *(P2)*
- SOC 2 evidence pack for the MSP's own audits *(P2)*
- 60-day payback guarantee and 30-day trial on one client site *(P2 commercial)*

### TeamLogic-specific packaging
- Fixed onboarding for the TeamLogic stack: go-live in days *(P2)*
- Network aggregate reporting for the franchisor (all served locations) *(P2)*
- Location-level benchmarking against network averages *(P3)*

---

## 3. Memory — resolution knowledge

**Who uses it:** technicians (Inputiv's and the MSP's), the franchisor, and standalone buyers. Bundled with Resolved; standalone "L3 knowledge access" at $40–60 per tech per month.

### Corpus
- Every closed ticket structured into: symptom, environment (OS, M365 config, tool versions), root cause, fix steps, time to resolve, what didn't work *(P3-A for TeamLogic, P3-B for others)*
- Continuous extraction from every new closed ticket *(P3)*
- Quality gate: L3 samples 100 extractions per week; 90% accuracy before customer exposure *(P3)*
- Vendor and platform playbooks that update themselves: Fortinet, Intune, Exchange Online, Entra Connect, NinjaOne, Bitdefender, Cove, ConnectSecure, Blackpoint *(P3)*
- Fix success rate per playbook so a technician knows which approach usually works *(P3)*

### Access
- "Seen this before?" panel in the ticket view: ranked matches with outcomes *(P3)*
- Plain-English search via Teams bot and browser sidebar *(P3)*
- Runbook generation from a cluster of similar resolutions, exported to IT Glue or Hudu *(P3)*
- Read-only API with per-MSP keys and rate limits, so the MSP's other AI tools (Thread, Pia) can query it *(P3)*
- Standalone seat for MSPs that keep their own desk *(P3)*

### Scoping tiers
- **Tier 1 — single-tenant:** each MSP sees only its own resolution history *(P3-B)*
- **Tier 2a — TeamLogic network knowledge:** all served franchisees share one corpus under a network-sharing addendum; new locations start with the accumulated fixes *(P3-A, Q1 2027)*
- **Tier 2b — anonymised cross-MSP:** opt-in, stripped of identifiers, re-identification tested *(Later, only after demand is proven)*

### Signals
- Regression watch: same issue recurring after a patch or policy change, flagged into TBR *(P3)*
- Gaps report: issue types with no reliable fix yet; doubles as Inputiv's product roadmap *(P3)*
- Knowledge coverage per client: share of tickets with a known-good fix available *(P3)*
- Time-to-productivity metric for new technicians and new locations *(P3, franchisor-facing)*

---

## 4. TBR — the proof layer

**Who uses it:** MSP owners with their clients; the franchisor. Not a separate SKU; the renewal artefact for everything above.

### Existing
- Microsoft 365 aggregation per client *(live)*
- Security and backup posture from the six integrations *(live)*
- White-label monthly review per client *(live)*

### Added on the engine
- "What changed since last review" narrative generated from Desk and Memory *(P3)*
- Ask-the-report box: plain-English questions over the review data *(P2)*
- Resolved outcomes section: tickets, SLA, MTTR, CSAT, hours returned, per client *(P2)*
- Regression and recurring-issue section from Memory *(P3)*
- M365 tenant hygiene findings: conditional access gaps, unused licences, stale admin accounts, with fixes queued for approval *(P3)*
- Compliance evidence pack per client for cyber-insurance questionnaires and CMMC readiness *(Later)*
- Network aggregate and location benchmarking view for TeamLogic *(P2 aggregate, P3 benchmarking)*
- Owner-shareable PDF and a short client-facing summary *(P2)*

---

## 5. Cross-product features

| Feature | Applies to | Phase |
|---|---|---|
| Single connector layer shared by all products | All | P1 |
| Server-side client scoping; model never selects credentials | All | P1 |
| Full audit log of prompts, tool calls, results, users | All | P1 |
| Read-only default; five-action allow-list with approval; no endpoint remediation in 2027 | Desk, Resolved | P2 |
| SOC 2 scope coverage for AI data flows, messaging, automations | All | P1–P2 |
| Structured close form on every ticket | Resolved, Memory | P1 |
| Per-MSP data isolation; network sharing only under signed addendum | Memory, TBR | P3 |
| Vendor API partner agreements and normalised cache | All | P1 |

---

## 6. Not features (explicitly out of scope through 2027)

- Autonomous endpoint remediation without human approval
- Client portal (CloudRadial territory)
- PSA or RMM replacement
- Generic AI triage sold as software without the human tier
- Anonymised cross-MSP pooling before demand and consent are proven
- Per-technician pricing for MSPs with fewer than five technicians

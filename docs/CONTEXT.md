# Project Context

Written September 7, 2026. This is the "why" behind the code. Update it when decisions change.

## Who we are

Inputiv (inputiv.com) is a white-label IT support company serving MSPs in the US and Australia: L1–L3 helpdesk, projects, and a monthly business review product called TBR. SOC 2 Type 2 certified. Backend is Python/FastAPI. 27 MSP clients; 20 of them are TeamLogic IT franchisees (TeamLogic is a 350+ location franchise network with a standardised stack).

## What we are building

One engine, three products, shipped in order:

1. **Desk** (Sep–Dec 2026). Read-only agent over the client's tool stack, used by Inputiv engineers via Slack. Goal: cut ticket handling time. Never sold.
2. **Resolved** (Dec 2026–Mar 2027). AI triage in front of a guaranteed human resolution, priced per resolved ticket or per user. Billing and SLA tracking ship first; AI features follow in cash-flow order.
3. **Memory** (Q1 2027 for TeamLogic, Q2 for others). Structured resolution knowledge base built from tickets Inputiv closes. First version is a shared "TeamLogic network knowledge" corpus under a signed sharing addendum; single-tenant version for other MSPs after.

TBR remains the monthly proof-of-value artefact and gets sections generated from the engine.

## The stack we integrate

Matches the TeamLogic IT franchise standard: Autotask (PSA), NinjaOne (RMM), Bitdefender GravityZone, ConnectSecure, N-able Cove, Blackpoint, Microsoft 365. ConnectWise PSA is added in Phase 2 so the product is not TeamLogic-only.

## Key decisions and why

- **Internal first.** Desk pays for itself on our own margin before anything is sold. One backend engineer is the only unfunded spend.
- **Human guarantee is the product.** AI-only vendors (Thread, Pia, Mizo, Atera Robin, ConnectWise Asio, Kaseya Cooper) hand exceptions back to the MSP's own staff. White-label desks have no AI. We sit in the seam. Pricing assumes only ~5% of tickets resolve with no human touch.
- **Read-only through 2027 except a five-action allow-list with approval** (password reset, MFA reset, group membership, disk cleanup, service restart). One bad automation on a franchisee tenant would spread through the network.
- **Network knowledge before anonymised pooling.** Sharing within one franchise brand needs one consent addendum, not an anonymisation pipeline. Cross-MSP pooling is deferred until demand is proven.
- **Portability.** ConnectWise adapter in Phase 2 and a diversification target (five non-TeamLogic MSPs by mid-2027) so we are not a captive vendor.
- **Server-side scoping.** The model never selects credentials. Non-negotiable.

## Near-term dates

- **Oct 2, 2026:** demo freeze. NinjaOne + Autotask + Slack chat loop working on one pilot franchisee's real data.
- **Oct 6–7, 2026:** TeamLogic IT owners' summit. We have a vendor booth. Goals: meet corporate, start new franchisee conversations, demo Desk and a Memory prototype.
- **Dec 12, 2026:** Gate 1 (Desk). Pass = ≥15% handling-time reduction, ≥80% useful answers.
- **Jan 2027:** first per-outcome invoices; formal franchisor preferred-vendor pitch.
- **Mar 27, 2027:** Gate 2 (Resolved).
- **Jun 26, 2027:** Gate 3 (Memory).

## Competitive notes (Sep 2026)

- Thread: AI front door, $19–34/license, ~$30M raised, integrated with Pia (Mar 2026), already a TeamLogic partner. Do not position against it; position alongside it.
- CrushBank: closest analog to Memory; single-tenant by design; ~$5.5M raised; claims 25% MTTR reduction. Our corpus is curated by our own L3s, which theirs is not.
- American HelpDesk: white-label desk, also a TeamLogic partner. Likely the incumbent competitor for the human tier.
- ConnectWise acquired ZofiQ (Jan 2026); Kaseya bundles Cooper Copilot; NinjaOne and Atera ship native AI. Vendor-native AI is the structural threat; cross-tool context is our answer.

## Open questions

- Closing-note quality: 200-ticket audit pending. Decides whether Memory is Q1 or Q3.
- Pricing preference: per-user vs per-outcome survey of five franchisee owners pending.
- Vendor API terms: partner requests to all six vendors pending; Datto/Autotask is the most exposed.
- Franchisor stance: introduction pending via franchisee advocates.

## Documents in this folder

- `inputiv-development-plan-v2.md` — phased plan with gates, team, risks, milestones.
- `inputiv-product-features.md` — every feature by product and phase.

# TBR Template Slide Map

Reference template: `docs/reference/tbr-template.pptx` (Client Business Review – 2026 Template, 23 slides).

## Rules for every slide

1. **Keep every slide topic and layout.** Titles, section dividers, agenda structure, and TeamLogic branding stay as they are.
2. **Red text (`FF0000`) is placeholder or instruction text.** None of it may appear in a rendered report. Red can be set on the run, on the paragraph default (`a:pPr/a:defRPr`), or on the shape's list style (`a:lstStyle`); resolve the effective colour at all three levels.
3. **Some sample content is not red** and must be treated as a placeholder anyway: the CX scores on slide 9 (88, 97, 97), and the dashboard screenshot image on slide 6.
4. **Numbers come from code.** The model never produces a count, percentage, price, hour estimate, or date.
5. **No data, no slide.** If a section has no facts and no engineer input, drop the slide and remove it from the agenda. Never ship a blank or sample slide.
6. **Render gate.** The final render fails if any of these remain: `FF0000` text, `XX`, `ABC Company`, `202x`, `Insert`, `{`, `<<`, `talk track`, `Examples provided`, `List specific`, `List recent`.

## Source legend

| Code | Meaning |
|---|---|
| **N** | NinjaOne (built) |
| **A** | Autotask (built) |
| **B** | Bitdefender GravityZone (built and validated live 2026-09-22) |
| **V** | ConnectSecure vulnerability scanning (built and validated live 2026-09-22) |
| **L** | Later connector: Blackpoint, Cove, M365 |
| **W** | Model with web search (industry only, never client data in queries) |
| **F** | Findings layer (rules over facts), phrased by the model |
| **C** | Franchisee config: name, logo, service catalog, price book |
| **E** | Engineer input on the review screen |

## Slides

| # | Topic | Placeholders to replace | Source | Now? |
|---|---|---|---|---|
| 1 | Title | Client name/logo box, Presented to, Presented by, period, generated date | Site name; franchisee name **C**; period; logo **C** or client name as text | Yes |
| 2 | Agenda | None, but must drop items for dropped sections | Derived | Yes |
| 3 | Industry Trends | All three red instruction bullets | Industry from Autotask company classification/market segment **A**, else **E**; trends via **W** with cited sources; engineer approves | Yes, with review |
| 4 | Service Review | Divider | Static | Yes |
| 5 | Current Management and Maintenance | `XX` workstations/laptops, physical servers, virtual servers, locations, users; "six" months; "{insert your opinion}"; improvement areas | Counts by node class **N**; physical vs virtual from system manufacturer/model **N**; locations from NinjaOne org locations or Autotask company locations **N/A**; users from **L** (M365), fallback active Autotask contacts, labelled as such; period length from report config; opinion and improvement areas from **F** using snapshot deltas, sentence dropped if no prior snapshot | Mostly |
| 6 | Customer Success Dashboard + Observations | Three red talk-track bullets; dashboard screenshot image | Replace image with native charts built in python-pptx (created vs completed daily, tickets by issue type, open by priority, SLA resolution and first-response rates, top contacts with automation separated) **A**; observations from ticket-pattern findings **F** (e.g. rising password resets → password manager) | Yes |
| 7 | Management Summary Report – Key Takeaways | Three red numbered takeaways (sample text) | Top three findings by severity across all sections **F**; model writes headline and detail citing facts | Yes, deepens as connectors land |
| 8 | Recent Accomplishments | Red instruction bullet | Metric improvements vs prior snapshot, completed projects, completed major changes in period **A** + **F**; engineer can add items **E** | Yes, with review |
| 9 | CX Metrics | Sample scores 88/97/97 (not red); three survey comment placeholders | Autotask survey results if the tenant uses them **A**, else survey tool export or **E**; drop slide if none | Depends on tenant |
| 10 | Open Technical Items | Divider | Static | Yes |
| 11 | Current Project Review | Project names, "ABC Company", phase, "{brief explanation here}", "July 202x" | Autotask Projects in progress: name, phase, % complete, end date **A** (new tool needed); explanation phrased by model from project fields | After new tool |
| 12 | Devices Recommended for **Replacement** | Red instruction bullet | Windows 10 devices whose CPU cannot run Windows 11 **N**; device age from **L** (Autotask configuration items, ScalePad) when available; hours and cost from price book **C** or "Quote pending" | Partly |
| 13 | Devices Recommended for Upgrade | Red instruction bullet | Windows 10 on Windows 11-capable hardware, RAM below threshold, low disk **N**; hours and cost from price book **C** or "Quote pending" | Yes |
| 14 | Cybersecurity | Italic instruction line; three sample Observation/Recommendation pairs | Keep the Observation → Recommendation format. Now: antivirus status, failed patch installs, outstanding critical patches **N**. Later: Bitdefender modules, Blackpoint coverage, ConnectSecure vulnerabilities **L**. Every gap requires cross-tool evidence **F**. Since 2026-09-22: NinjaOne computers with no Bitdefender endpoint (hostname join) **N/B**; a Bitdefender module that is *disabled* (licensed, off by policy) is a finding here, one that is *not licensed* goes to slide 18 instead, and one the row does not report is nothing; patch module off only where NinjaOne is not patching (no install in 90 days and nothing on its waiting list) **N/B**; encryption only where neither BitLocker (NinjaOne volumes, `include=bl`) nor the Bitdefender module protects **N/B**; EDR sensor off or unlicensed held until Blackpoint **B/E**; Exchange and container modules never findings. NinjaOne's held antivirus doubt is confirmed by a doubted device Bitdefender shows without a working agent or has no endpoint for at all, dismissed when every doubted device is protected **N/B**. Vulnerabilities **V/N**, ranked: known-exploited (CISA KEV) findings first, then "N updates resolve M of the X open critical findings" (counts, never a share of the subset the fixes happen to cover), then open critical findings, then software to remove (`Uninstall` remediations), then scan coverage (NinjaOne computers with no ConnectSecure asset or not scanned in 30 days, a two-tool gap); a finding whose fix is a target version matched by a NinjaOne pending software patch at or above it is "fix awaiting install", never a new gap (fixes are versions, not KBs: live, 0 of 300 carried one); every figure is as of the scan date. **Every vulnerability number is a confirmed finding**, the scanner having proved the build present; the suspected ones get exactly one code-written caveat sentence at the foot of the slide and nothing else, counting the same severity as the headline it qualifies. At most the slide's budgeted findings render (`layout.SLIDE_BUDGETS`: three pairs, each measured to fit the placeholder), with a sentence saying how many were left out | Yes |
| 15 | Backup / Business Continuity | Instruction line; three sample pairs | Cove coverage, offsite replication, M365 backup **L**; cross-check against workloads that exist (SQL, Hyper-V services found by **N**). Drop until Cove is connected | Later |
| 16 | Strategic Initiatives | Divider | Static | Yes |
| 17 | Scheduled Projects | Two red project bullets; "August 202x" | Autotask Projects with future start dates, or accepted quotes **A** (new tool); else **E** | After new tool |
| 18 | Recommended Services | Red instruction line; catalog list in red | Service catalog **C**; show only services backed by a finding **F**, each with its reason. A finding backs a service only when that service addresses it directly; a full disk or short memory backs none (no storage or maintenance service is catalogued) and Hardware Lifecycle Planning is backed only by end-of-support devices, since device age is not collected. At most four services, the rest named in the reason line; every service line is measured to fit (`layout.py`). Since 2026-09-22 a Bitdefender module the customer's licence lacks (`not_licensed`) lands here rather than on slide 14, under the same cross-tool rules: Full Disk Encryption for devices BitLocker leaves unencrypted, Patch Remediation for devices NinjaOne is not patching, EDR held until Blackpoint **B/N** | Yes |
| 19 | Near-Term Priorities | Three red numbered items | Top three recommended services or findings by severity **F** | Yes |
| 20 | AI Essentials | None (black offering list) | Keep unchanged as a static offering slide. Do not generate AI commentary from unrelated metrics | Yes |
| 21 | Implementation Timeline | Divider | Static | Yes |
| 22 | Budget and Timeline | Four red recommendation rows with `$` and quarter | Rows from recommended services **F**; budget from price book **C** or Autotask quote **A**, else "Quote pending"; timeline quarter from finding severity | Yes |
| 23 | Thank you | None | Static | Yes |

## Template issues to fix in the source file

- **Slide 12 title says "Recommended for Upgrade"**, the same as slide 13, but its body text is about replacement. Retitle it "Recommended for Replacement" in the template.
- **The template contains no native charts.** Slide 6 is a pasted screenshot. Charts must be created with `add_chart`, not updated with `chart.replace_data`.

- **Slide 9's NPS score box sits on top of the CSAT score.** In the template the `88` overlaps the left-hand `97`. The renderer moves it above its own "Net Promoter Score" label; fix the position in the template and that code can go.
- **The agenda's last line, "Other applicable reports", is sample text** that is not red and not in the gate's list. The renderer always removes it and the gate checks for it (`TEMPLATE_SAMPLES` in `app/reports/tbr/gate.py`), along with slide 7's three sample takeaways and slide 22's sample recommendation names.
- Until the title is fixed in the source file, slide 12 is retitled "Recommended for Replacement" at render time.

## How it was built (2026-09-21)

`app/reports/tbr/`, driven by `scripts/tbr.py` or the `/tbr` review screen. See CLAUDE.md, "Business reviews". What the first live run (shelton-keller-group, August 2026) showed about the map's assumptions:

- **Industry (slide 3).** Autotask's `classification` is a sales grade ("Managed Services", "A (hot)"), not an industry. The industry is `marketSegmentID`. The pilot site's is "Services", which is too broad to research, so the engineer names the industry on the review screen.
- **Users (slide 5): withdrawn on 2026-09-21.** The fallback to active Autotask contacts gave 443 contacts against 101 workstations: a contact is anyone who ever raised a ticket, not a licensed user. The Users line is left off the slide until a Microsoft 365 connector can count users. The contact count is no longer collected, no finding compares machines with people, and a review stored with the old line renders without it.
- **The template ships speaker notes** on slides 1, 3, 6 and 7: presenter instructions and AI prompts, including `<<insert industry here>>`. They are not red and not on a slide, so the first gate never saw them, and the first deck rendered (2026-09-21, 08:52) still carried slide 1's. Every render now clears the notes of every slide it keeps, and the gate reads notes as well as slides. For notes, rule 6's list is narrowed to its fill-in marks (`XX`, `ABC Company`, `202x`, `{`, `<<`) plus any run of five consecutive words from the template's own notes; "Insert", "talk track", "Examples provided", "List specific" and "List recent" are ordinary words in an engineer's notes and apply to slides only.
- **CX (slide 9) and accepted quotes (slide 17).** The Surveys, SurveyResults and Quotes entities answer HTTP 500 for the pilot's API user, as Contracts and ConfigurationItems do. Both slides are engineer-supplied until that is granted.
- **Windows 11 eligibility (slides 12, 13).** NinjaOne reports no TPM, Secure Boot or UEFI state at all. Eligibility is judged from processor generation and memory and says so on every result.

## Additions to the map (2026-09-21)

| Slide | Addition | Source | Written by |
|---|---|---|---|
| 6 | Tickets-by-contact chart labels people `Contact A`, `Contact B` by rank unless the engineer turns names on for the review | Autotask contacts | code |
| 7 | Scorecard above the takeaways: Service, Devices, Patching and (since 2026-09-22) Security as good, attention or action; Backup "Not yet assessed"; "No data" for an area whose facts were not collected; a trend arrow only with a prior snapshot. Security's vulnerability component (known-exploited findings, assets with a critical finding, managed computers unscanned in 30 days, the company risk score) counts confirmed findings only, and is left out for a site with no ConnectSecure company | facts, by the rules in `app/reports/tbr/scorecard.toml` | code, never the model |
| 8 | "Value delivered" block first: patches installed, alerts resolved, tickets resolved, tickets resolved outside business hours. A figure that cannot be supplied, or is zero, has no line | NinjaOne install history and activity log; Autotask tickets; franchisee business hours | code |

Alerts resolved is available only while NinjaOne's activity log still covers the whole period (about two weeks of retention on the pilot tenant). Out-of-hours needs the franchisee's business-hours time zone; none is assumed.

## Render modes (2026-09-21)

| Mode | For | Speaker notes | Names | Marks the review rendered |
|---|---|---|---|---|
| `client` (default) | the customer | empty, or the engineer's own notes for the slide | real | yes |
| `internal` | the engineer | the engineer's notes, then the fact-id trace and definitions | real | no |
| `demo` | a prospect | as `client`, pseudonymised | pseudonyms; "Sample data" on every slide | no |

The trace does not depend on the mode. It is on the stored review (every item's `cites`, every fact's definition) and on the review screen. A demo deck keeps every number and replaces the site, the franchisee, every device and contact the collectors saw (on a slide or not) and every engineer with one pseudonym each, built once per document so a name is the same everywhere; the file is named `sample-client-...`, the franchisee's logo is left off, and the gate fails the render if a real name survives in slide text, chart labels or notes. The template's own TeamLogic IT branding is the franchisor's and stays.

Industry Trends research uses `REPORT_WEB_SEARCH_ALLOWED_DOMAINS`, not the chat agent's list: regulators (cisa.gov, nist.gov, ic3.gov, ftc.gov, hhs.gov, sec.gov) and recognised industry associations. Vendor domains are refused in settings and a vendor-sourced trend is dropped in code.

## New tools and config this map requires

- Autotask: `list_projects(client_id, site_id, status)` with phase, percent complete, start and end dates; company classification or market segment; survey results if available.
- NinjaOne: organisation locations; physical vs virtual classification from system manufacturer and model; Windows 11 hardware eligibility from processor model, TPM and RAM where reported.
- Franchisee config: display name, logo, service catalog (slide 18 categories), price book (hours and cost per recommendation type), default review period length.
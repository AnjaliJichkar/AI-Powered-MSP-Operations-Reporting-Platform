# The demo tenant

A fictional franchisee, **Tailwind IT Services** (`tailwind-it-demo`), with
eight customers named after the fictional companies in Microsoft's own
documentation. Nothing in it comes from a real tenant: `app/demo/dataset.py`
generates the world from a seed, `app/demo/vocabulary.py` holds the public
words it is built from (Windows KB numbers, CISA known-exploited CVE ids,
common software titles, hardware models), and the four fake vendors in
`app/demo/*_api.py` answer the real connector clients over an in-process
transport that can reach no host. Every timestamp is an offset resolved
against the clock at serve time, so the demo is always the last ninety days.

## How it is kept apart from live clients

- `Client.kind` is `live` or `demo` (migration 0020). The connector factory
  builds a demo client's connectors only from `app/demo/connectors.py`, which
  need the generated world to be constructed at all, so the live path can never
  produce one; a live client is built only from the real classes and is refused,
  not given a fake, when it has no credential. `manage_client.py
  set-credentials` refuses the demo client before it asks for a secret.
- Every audit row written for the demo client is marked `demo`
  (`audit_log.demo`), whatever wrote it.
- `scripts/demo_tenant.py create | reset | delete` manages it. `create` runs
  the same site sync a live client gets, through the fakes, and prints invite
  links for a demo administrator, a demo technician and a demo viewer, one per
  franchisee role; `reset` deletes what the
  tenant accumulated and syncs again; `delete` removes the client, its
  organisation and accounts, and its demo-marked audit rows.

## The dataset (seed 20260929)

| what | count |
|---|---|
| customers | 8 |
| devices | 262 |
| vendor only endpoints | 8 |
| tickets | 697 |
| patch installs | 2739 |
| pending patches | 137 |
| alerts | 181 |
| detections | 14 |
| vulnerability findings | 576 |
| people | 84 |

Ninety days of tickets, patch installs, alerts, detections and vulnerability
findings; the device counts include one switch per customer, which the
coverage population counts and leaves out.

## The scenarios

Each is placed on purpose in `dataset._place_scenarios` and checked by
`scenarios.check(world)`; `scripts/demo_tenant.py create` and
`tests/demo/test_world.py` refuse a world that has lost one. 34
scenarios:

| Scenario | Where | What it shows |
|---|---|---|
| `coverage.covered` | every customer | Most managed computers are covered: a current GravityZone row and a recent scan. |
| `coverage.netbios_collision` | Contoso Ltd | Two accounting machines whose names differ only after the fifteenth character share one NetBIOS `systemName`; `dnsName` is whole, and MAC and serial tell them apart. |
| `coverage.unmatched` | Tailspin Toys | Two reimaged spare laptops enrolled under one name whose vendor rows carry no MAC and no serial: the name pass is ambiguous, nothing decides, both stay unmatched with each other named. |
| `coverage.not_computers` | every customer | A switch per customer: network gear NinjaOne lists, counted and left out. |
| `coverage.rmm_stale` | Contoso Ltd | A device NinjaOne has not heard from in 31 days, and neither has any other tool. |
| `coverage.rmm_agent_down` | Contoso Ltd | A device NinjaOne lost 21 days ago that GravityZone heard from this morning: the machine is alive, the RMM agent is not. |
| `coverage.never_contacted` | Contoso Ltd | A device enrolled in NinjaOne that has never checked in. |
| `coverage.absent_no_agent` | Northwind Traders | Managed computers with no GravityZone row at all: the 'no protection agent' gap. |
| `coverage.unprotected_and_dark` | Northwind Traders | One machine with no protection agent that has also not checked in for over a week: unprotected and unseen, the worst square on the board. |
| `autotask.open_sla_breaches` | every customer | Two tickets per customer open now and already past their due time: what 'which tickets are breaching SLA' shows today. |
| `ninjaone.alerts_today` | every customer | Open alerts raised in the last day at every customer, four at Northwind: 'anything wrong at X today' is never an empty answer. |
| `autotask.resolution_notes` | every customer | Every resolved ticket says what was wrong and what fixed it, so the ticket workspace can cite a similar one. |
| `coverage.not_scanned` | Northwind Traders | Managed computers ConnectSecure has never scanned, and one not scanned in 45 days. |
| `coverage.unmanaged` | Fabrikam Inc | A real machine both tools hold that NinjaOne does not manage (a reception kiosk). |
| `coverage.default_windows_names` | Litware Inc | Endpoints named DESKTOP-… and LAPTOP-…: real machines, likely not yet onboarded. |
| `coverage.external` | Northwind Traders | ConnectSecure scan targets named by an IP address and by a public host name: external, never an unmanaged endpoint. |
| `coverage.discovered` | Tailspin Toys | A GravityZone row that is not managed and a ConnectSecure probe: discovered, reported apart. |
| `bitdefender.two_companies` | Fabrikam Inc | One customer holding two GravityZone companies (a hand-made one and the integration's, with its six-character suffix); the site is read as their union. |
| `bitdefender.av_only` | Fourth Coffee | An antivirus-only customer: a licence that manages antimalware alone, no vulnerability scanner. |
| `bitdefender.encryption_not_licensed` | every customer | No licence manages encryption, while BitLocker already covers most Windows devices: the gap is the unencrypted minority, never the whole fleet. |
| `bitdefender.edr_not_licensed` | seven of eight customers | EDR is not licensed, so no detection can exist and the review says so rather than 'quiet'. |
| `bitdefender.edr_detections` | Woodgrove Bank | The one customer with EDR licensed, and ninety days of detections on it. |
| `ninjaone.windows_10_iot_classed_server` | Adventure Works Cycles | A till on Windows 10 IoT Enterprise that NinjaOne classes WINDOWS_SERVER: counted as a workstation by its operating system, and listed on the internal render. |
| `connectsecure.confirmed_and_unconfirmed_criticals` | every scanned customer | Critical findings in both populations, confirmed and suspected, never added together. |
| `connectsecure.known_exploited` | every scanned customer | Findings on CISA's Known Exploited Vulnerabilities catalogue, confirmed on some machines. |
| `connectsecure.version_remediations` | every scanned customer | Fixes that are a target version (Update Google Chrome to 139.0.7258.128), joined to NinjaOne's pending software patches. |
| `connectsecure.uninstall_remediations` | every scanned customer | Software to remove (uTorrent, CCleaner, an old Java): `Uninstall` remediations, their own finding. |
| `autotask.sla_breaches` | every customer | About one ticket in eight misses its first-response or resolution due time. |
| `autotask.automated_contacts` | every customer | Tickets raised by monitoring (source 'Monitoring Alert', no contact): counted apart from people. |
| `autotask.repeat_issue` | every customer | The same printer, the same person, most weeks: a repeat issue a plan can cite resolved tickets for. |
| `autotask.issue_type_unset` | every customer | Some tickets carry no issue type, so the review's 'largely unset' rule has something to measure. |
| `ninjaone.patch_install_failures` | every customer | OS and third-party patch installs that failed, on a few devices each month. |
| `ninjaone.pending_critical_patches` | every customer | Approved patches waiting to install, some critical, so the patch backlog has a number. |
| `ninjaone.windows_10_ineligible` | several customers | Windows 10 machines on processors Windows 11 does not support (6th and 8th generation Core): the replacement list. |

Not placed, because it is an action a person takes on the page: `excluded`
(a coverage exclusion with a reason), and the review's engineer steps
(approvals, talk tracks, CX scores). Do them live in the demo.

## Regenerating this page

`python -c "from app.demo.scenarios import describe; print(describe())"`
prints the table; the counts come from `build_world(seed).summary()`.

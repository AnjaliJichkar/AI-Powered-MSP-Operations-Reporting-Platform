"""Application configuration.

Every value comes from the environment (hard rule 3: no secrets in code).
Agent caps live here rather than as constants in the loop (hard rule 6).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "dev", "staging", "prod"]

#: Hosts that mean "this machine": refused in a production URL.
LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}  # noqa: S104
)

#: Where a business review's industry research may read, unless the environment says
#: otherwise: regulators and standards bodies first, then recognised associations for
#: the industries the franchisees' customers are in. No vendor appears here, and none
#: may be added (see `REPORT_VENDOR_DOMAINS`).
DEFAULT_REPORT_DOMAINS = ",".join(
    (
        # regulators and standards bodies
        "cisa.gov",
        "nist.gov",
        "ic3.gov",
        "ftc.gov",
        "hhs.gov",
        "sec.gov",
        # recognised industry associations
        "americanbar.org",
        "aicpa-cima.com",
        "finra.org",
        "ama-assn.org",
        "aha.org",
        "ada.org",
        "aia.org",
        "agc.org",
        "nam.org",
        "nar.realtor",
        "nrf.com",
        "restaurant.org",
        "ahla.com",
        "nada.org",
        "trucking.org",
        "shrm.org",
        "educause.edu",
        "councilofnonprofits.org",
        "cisecurity.org",
    )
)

#: Companies that sell to MSPs or to their customers. A report never cites them: a
#: review that recommends a product and sources its reasons from that product's maker
#: is marketing. Matching covers subdomains (`learn.microsoft.com`).
REPORT_VENDOR_DOMAINS: frozenset[str] = frozenset(
    {
        "ninjaone.com",
        "ninjarmm.com",
        "autotask.net",
        "datto.com",
        "kaseya.com",
        "connectwise.com",
        "n-able.com",
        "bitdefender.com",
        "blackpointcyber.com",
        "connectsecure.com",
        "scalepad.com",
        "microsoft.com",
        "google.com",
        "apple.com",
        "cisco.com",
        "fortinet.com",
        "sonicwall.com",
        "sophos.com",
        "crowdstrike.com",
        "sentinelone.com",
        "paloaltonetworks.com",
        "huntress.com",
        "proofpoint.com",
        "mimecast.com",
        "knowbe4.com",
        "veeam.com",
        "acronis.com",
        "dell.com",
        "hp.com",
        "lenovo.com",
    }
)


#: Where a ticket's remediation plan may look things up: the vendors whose
#: products the franchisee runs, their documentation and knowledge bases, and
#: the public vulnerability catalogues. The opposite of the report list on
#: purpose (`TICKET_WEB_SEARCH_ALLOWED_DOMAINS`), and never empty.
DEFAULT_TICKET_DOMAINS = ",".join(
    (
        "learn.microsoft.com",
        "support.microsoft.com",
        "techcommunity.microsoft.com",
        "ninjaone.com",
        "ninjarmm.com",
        "autotask.net",
        "kaseya.com",
        "bitdefender.com",
        "connectsecure.com",
        "blackpointcyber.com",
        "n-able.com",
        "support.apple.com",
        "dell.com",
        "hp.com",
        "lenovo.com",
        "cisco.com",
        "fortinet.com",
        "sonicwall.com",
        "ui.com",
        "veeam.com",
        "cisa.gov",
        "nvd.nist.gov",
        "msrc.microsoft.com",
    )
)


class Settings(BaseSettings):
    """Settings for the whole engine. Instantiate via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Application ----------------------------------------------------
    app_name: str = "inputiv-engine"
    environment: Environment = "local"
    debug: bool = False
    log_level: str = "INFO"

    # --- Database -------------------------------------------------------
    database_url: str
    database_echo: bool = False
    database_pool_size: Annotated[int, Field(ge=1)] = 5
    database_max_overflow: Annotated[int, Field(ge=0)] = 10
    database_pool_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    database_pool_recycle_seconds: Annotated[int, Field(gt=0)] = 1800

    # --- Credential encryption ------------------------------------------
    # key_id -> Fernet key. Retired keys stay listed so existing rows still
    # decrypt after a rotation; only the active key encrypts new rows.
    #
    # Note: pydantic-settings MERGES dict fields across sources rather than
    # replacing them, so a key present in .env stays in the map even when the
    # environment supplies a different one. Removing a key therefore means
    # removing it from every source. Deployments set the environment and ship
    # no .env, which is the only configuration where that is not a trap.
    credential_encryption_keys: dict[str, SecretStr] = Field(default_factory=dict)
    credential_encryption_active_key_id: str = "v1"

    # --- Anthropic ------------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5"
    anthropic_max_tokens: Annotated[int, Field(gt=0)] = 16000
    # How hard the model works per turn: low | medium | high | xhigh | max.
    # "high" is the API default. Lower it if chat latency matters more than
    # depth; raise it if answers are missing things an engineer would catch.
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    anthropic_max_retries: Annotated[int, Field(ge=0)] = 2

    # --- Web search -----------------------------------------------------
    # Anthropic's server-side web search, for vendor error codes, KB articles,
    # CVEs and end-of-life dates — never for a client's own data. Every search
    # is audited with its query and the URLs returned.
    web_search_enabled: bool = True
    # Searches the model may make while answering one question.
    web_search_max_uses: Annotated[int, Field(ge=1)] = 3
    # Optional. Comma-separated domains; when set, results are restricted to
    # them (e.g. "ninjaone.com,autotask.net,learn.microsoft.com,nvd.nist.gov").
    web_search_allowed_domains: str | None = None

    # --- Connector behaviour --------------------------------------------
    # Hard rule 6: timeouts, retry budget and pagination caps are settings, so
    # a misbehaving vendor is a config change rather than a code change.
    connector_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    connector_connect_timeout_seconds: Annotated[float, Field(gt=0)] = 10.0
    # A vendor's own read timeout, when it needs one the others do not:
    # `<CONNECTOR>_TIMEOUT_SECONDS` overrides CONNECTOR_TIMEOUT_SECONDS for that
    # connector alone (`connector_timeout_for`). GravityZone's inventory pages
    # took 27 to 90 seconds on 2026-09-28 and a 30-second budget made the whole
    # coverage board "not assessed" for it (validation log).
    ninjaone_timeout_seconds: Annotated[float, Field(gt=0)] | None = None
    autotask_timeout_seconds: Annotated[float, Field(gt=0)] | None = None
    #: The most OR conditions one Autotask query may carry. The vendor expands
    #: every `in` filter into one condition per value, documents a limit of 500
    #: and answers a query over it with a **500**, not a message (api-notes;
    #: live 2026-10-01: 544 conditions refused, 401 accepted the same minute).
    #: `_query_all` counts the whole filter and splits the `in` lists to stay
    #: under this, so the margin below 500 is what absorbs a clause we have not
    #: seen yet rather than a vendor's own overhead.
    autotask_max_filter_conditions: Annotated[int, Field(ge=2, le=500)] = 450
    bitdefender_timeout_seconds: Annotated[float, Field(gt=0)] | None = 120.0
    connectsecure_timeout_seconds: Annotated[float, Field(gt=0)] | None = None
    connector_max_retries: Annotated[int, Field(ge=0)] = 3
    connector_backoff_base_seconds: Annotated[float, Field(gt=0)] = 0.5
    connector_backoff_max_seconds: Annotated[float, Field(gt=0)] = 20.0
    connector_page_size: Annotated[int, Field(ge=1, le=1000)] = 100
    connector_max_pages: Annotated[int, Field(ge=1)] = 20

    # --- NinjaOne -------------------------------------------------------
    # Per-organisation requests in flight at once when a read fans out.
    ninjaone_concurrency: Annotated[int, Field(ge=1)] = 5
    # How long a tenant-wide device inventory is reused before NinjaOne is
    # asked again. 0 disables the cache.
    ninjaone_inventory_ttl_seconds: Annotated[float, Field(ge=0)] = 180.0
    # Page size for the tenant-wide GET /v2/devices. The spec declares no
    # maximum (api-notes); 1000 is our choice, and the listing pages until an
    # empty page so a silent vendor cap cannot make it stop early.
    ninjaone_devices_page_size: Annotated[int, Field(ge=1)] = 1000
    # Page size for the /v2/queries/* reports (patches, volumes, software, ...).
    # The spec declares no maximum; 5000 was honoured live (api-notes).
    ninjaone_query_page_size: Annotated[int, Field(ge=1)] = 5000

    # --- Bitdefender GravityZone -------------------------------------------
    #: Per-company requests in flight at once. The documented limit for
    #: getNetworkInventoryItems is 5 requests per second per API key.
    bitdefender_concurrency: Annotated[int, Field(ge=1)] = 4
    bitdefender_inventory_ttl_seconds: Annotated[float, Field(ge=0)] = 180.0
    #: getNetworkInventoryItems and getEndpointsList allow 1 to 1000 per page.
    bitdefender_page_size: Annotated[int, Field(ge=1, le=1000)] = 1000
    #: `getManagedEndpointDetails.lastSeen` is one call per row and is kept on the
    #: record's device link; it is asked again only when the stored value is
    #: older than this (0: every run), and only for devices NinjaOne has lost
    #: for less than DEVICE_RETIRE_DAYS.
    bitdefender_last_seen_refresh_hours: Annotated[int, Field(ge=0)] = 12
    #: Detail calls in flight at once during a last-seen read.
    bitdefender_detail_concurrency: Annotated[int, Field(ge=1)] = 4

    # --- ConnectSecure ------------------------------------------------------
    #: Per-company requests in flight at once. The vendor's limits are 300 a
    #: minute, 2,000 an hour and 30,000 a day per key, and it asks for large
    #: pages rather than many requests (api-notes).
    connectsecure_concurrency: Annotated[int, Field(ge=1)] = 4
    connectsecure_inventory_ttl_seconds: Annotated[float, Field(ge=0)] = 180.0
    #: `limit` of the first request of a listing; a page of 10,000 was honoured
    #: live, and a listing is read as one page sized to the vendor's total
    #: (the report views have no stable order for an offset walk).
    connectsecure_page_size: Annotated[int, Field(ge=1)] = 10000
    #: The largest single page asked for; a listing above it is reported truncated.
    connectsecure_max_rows: Annotated[int, Field(ge=1)] = 50000
    #: An asset not scanned for this many days is a coverage gap.
    connectsecure_stale_scan_days: Annotated[int, Field(ge=1)] = 30

    # --- Agent run caps -------------------------------------------------
    max_tool_calls: Annotated[int, Field(ge=1)] = 8
    tool_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    agent_timeout_seconds: Annotated[float, Field(gt=0)] = 120.0
    # Rows a list tool hands the model unless the tool sets its own cap. Beyond
    # this the result reports `total` and `truncated: true` instead of growing.
    tool_max_rows: Annotated[int, Field(ge=1)] = 200

    # --- Automation ------------------------------------------------------
    # `AUTOMATION_TRIGGERS_LIVE` is the switch for the automation triggers'
    # poll (`scripts/automation.py poll`), which reads new Autotask tickets
    # and evaluates the ticket and alert rules over them. Off by default and
    # separate from the forward shadow's switch: each feature has one owner,
    # and the per-client workflow and trigger switches still both apply.
    automation_triggers_live: bool = False

    # --- Autopilot (Level 0: shadow mode) -------------------------------
    # The shadow runner predicts and scores; it writes nothing to any vendor.
    # `AUTOPILOT_SHADOW_LIVE` is the switch for the forward shadow (Part B),
    # which polls Autotask for new tickets. Off by default and turned on by a
    # person: a replay on stored rows needs no vendor at all.
    autopilot_shadow_live: bool = False
    #: The model the shadow classifier uses. Unset follows `ANTHROPIC_MODEL`,
    #: so the two can be moved apart (a cheaper model for a 2,700-ticket
    #: replay) without touching the agent.
    autopilot_model: str | None = None
    #: Send a replay's requests through the Message Batches API: about half
    #: the per-token price, answered within hours rather than minutes. Off by
    #: default, because "the answers arrive tomorrow" is a surprise to anybody
    #: who has not chosen it, and never used by the live paths — the forward
    #: shadow, the backfill and the ticket card are answering about a ticket
    #: somebody is looking at now.
    autopilot_batch_replay: bool = False
    #: Classifications in flight at once. The replay is thousands of small
    #: independent calls, so this is what decides whether it takes minutes or
    #: an hour.
    autopilot_concurrency: Annotated[int, Field(ge=1, le=32)] = 8
    #: The cap that always bites. A run stops cleanly when the tokens it has
    #: read and written pass this, whether or not anybody has priced them.
    autopilot_max_tokens_per_run: Annotated[int, Field(ge=1000)] = 5_000_000
    #: What a million tokens costs, input and output. **No default**: a price
    #: nobody set is not a price, and a cost computed from a guessed rate is
    #: worse than no cost at all (the same rule the review's price book
    #: follows). Unset, a run reports tokens and says the cost is unpriced.
    autopilot_input_cost_per_mtok: Annotated[float, Field(ge=0)] | None = None
    autopilot_output_cost_per_mtok: Annotated[float, Field(ge=0)] | None = None
    #: What a million *cached* input tokens costs. Unset, cached reads are
    #: priced at the input rate above, which overstates — the right direction
    #: to err for a cap, the wrong one for a quote.
    autopilot_cache_read_cost_per_mtok: Annotated[float, Field(ge=0)] | None = None
    #: A run stops cleanly when the priced cost passes this. Only enforceable
    #: once the two prices above are set; `autopilot_max_tokens_per_run` is the
    #: backstop until then.
    autopilot_cost_cap_usd: Annotated[float, Field(gt=0)] | None = None
    #: Requests an hour the forward shadow's polling may make. Autotask
    #: refused every query for about an hour on 2026-10-01 and nobody knows
    #: what its threshold returns (api-notes), so the poller is capped by us.
    autopilot_poll_requests_per_hour: Annotated[int, Field(ge=1)] = 60
    #: The rule that picks a confidence threshold, fixed **before** a run
    #: rather than after seeing its numbers: the lowest threshold on the
    #: development period where accuracy is at least this and coverage is at
    #: least the next setting. Chosen in advance because the rule changes the
    #: verdict — on 2026-10-01 "the lowest threshold meeting the gate" failed
    #: the holdout while "the lowest with a margin" passed it, and picking
    #: between them after the fact is picking the answer.
    autopilot_threshold_min_accuracy: Annotated[float, Field(gt=0, le=1)] = 0.88
    autopilot_threshold_min_coverage: Annotated[float, Field(gt=0, le=1)] = 0.40
    #: Items from the front of a labelling set that go to a second labeller as
    #: well. Agreement between two people is the only measure we have of how
    #: hard the question is; 0 turns it off.
    autopilot_double_label_count: Annotated[int, Field(ge=0)] = 20
    #: The confidence below which a suggestion is **not shown** on a ticket.
    #: A trial setting for the internal review only: it decides what an
    #: engineer is offered, never what the gate reads, which is the threshold
    #: rule above. Recorded on every suggestion, so a figure read later knows
    #: which bar the suggestions it counted had to clear.
    autopilot_display_threshold: Annotated[float, Field(ge=0, le=1)] = 0.80
    #: The share of suggested tickets that ask the engineer first, with the
    #: suggestions hidden. Blind answers are the only ones the gate scores
    #: accuracy from: an engineer who has read the agent's answer is agreeing
    #: with it. 0 turns the checks off.
    autopilot_blind_rate: Annotated[float, Field(ge=0, le=1)] = 0.2

    #: How far back a grouping decision may look for an earlier ticket, and
    #: how similar two titles must read to be called the same thing.
    autopilot_group_window_days: Annotated[int, Field(ge=1)] = 30
    autopilot_group_similarity: Annotated[float, Field(gt=0, le=1)] = 0.7

    # --- Conversation threads -------------------------------------------
    # Input tokens of thread history replayed into the model on a turn. Over
    # this, old tool results are stubbed out, then whole old turns dropped.
    conversation_context_budget: Annotated[int, Field(ge=1000)] = 80_000
    # The newest turns that trimming never touches.
    conversation_keep_recent_turns: Annotated[int, Field(ge=1)] = 4

    # --- Reports --------------------------------------------------------
    # Where rendered business reviews are written. They contain a customer's
    # data: keep this outside the repository and off any shared drive.
    reports_dir: str = "var/reports"
    # Where a report's Industry Trends research may read. Separate from the chat
    # agent's WEB_SEARCH_ALLOWED_DOMAINS on purpose: an engineer looks things up on
    # vendor sites, but a customer-facing review cites regulators, standards bodies
    # and the customer's own industry associations, never a vendor's blog. A vendor
    # domain here is refused at start-up (`REPORT_VENDOR_DOMAINS`).
    report_web_search_allowed_domains: str = DEFAULT_REPORT_DOMAINS
    report_web_search_max_uses: Annotated[int, Field(ge=1, le=20)] = 6
    #: Output tokens one section's draft may use. Fourteen slots in one call
    #: overran the cap live on 2026-09-22 and the section fell back to code
    #: wording. How many findings a slide puts to the model is not a setting:
    #: it is measured from the template (`reports/tbr/layout.SLIDE_BUDGETS`).
    report_section_max_tokens: Annotated[int, Field(gt=0)] = 3000

    # --- Ticket workspace -------------------------------------------------
    # Where a remediation plan may search: vendor documentation and the public
    # vulnerability catalogues. Never empty; a query never carries client data
    # (the model is checked, and a plan whose search named the client loses its
    # web citations).
    ticket_web_search_allowed_domains: str = DEFAULT_TICKET_DOMAINS
    ticket_web_search_max_uses: Annotated[int, Field(ge=1, le=10)] = 4
    ticket_plan_max_tokens: Annotated[int, Field(gt=0)] = 6000
    #: Resolved tickets `search_tickets` may hand the model as evidence.
    ticket_similar_tickets: Annotated[int, Field(ge=1, le=20)] = 6
    #: How far back the enrichment panel looks for alerts, patch failures and tickets.
    ticket_recent_days: Annotated[int, Field(ge=1, le=365)] = 30
    #: How long the ticket statistics stay usable before they are read again.
    #: The figures are two Autotask listings, so a page load every few seconds
    #: while somebody reads the board would be a vendor read every few seconds.
    #: In memory and therefore per process, which is written down rather than
    #: hidden, the same as the sign-in rate limit.
    ticket_statistics_ttl_seconds: Annotated[int, Field(ge=0, le=3600)] = 300
    #: How many days the created-and-resolved chart covers.
    ticket_statistics_window_days: Annotated[int, Field(ge=1, le=90)] = 14
    #: The most resolved tickets the statistics will list beside the count. The
    #: count itself is never capped; the list says when it stopped.
    ticket_statistics_max_resolved: Annotated[int, Field(ge=1, le=1000)] = 200

    # --- Coverage --------------------------------------------------------
    #: A NinjaOne device with no contact for more than this many days is
    #: `rmm_stale` on the coverage board: its own line, never counted as absent
    #: from any other tool, because the RMM itself has lost it.
    coverage_rmm_stale_days: Annotated[int, Field(ge=1)] = 14
    #: The page never computes the board; it reads the latest stored snapshot.
    #: Past this many days that snapshot is reported as old, with its age in
    #: words, because a date alone reads as fine to somebody who does not know
    #: what the collector's cadence is — and a board nobody recomputed for
    #: three weeks disagreed with the Devices page about every machine on the
    #: demo tenant while printing a date that looked perfectly ordinary. The
    #: daily collector writes one a day, so anything past two means it has not
    #: run.
    coverage_board_stale_days: Annotated[int, Field(ge=1)] = 2
    #: A site whose backup scope is `auto` is read as buying workstation backup
    #: when Cove holds one for at least this share of its workstations. Below
    #: it, the machines Cove does hold are exceptions rather than a policy, and
    #: the rest are not reported as gaps every month. Half, as `sync_overlap_share`
    #: is: the two are the same kind of judgement about whether a handful of
    #: matching rows means a pattern. A site mid-rollout is set explicitly.
    backup_auto_workstation_share: Annotated[float, Field(gt=0, le=1)] = 0.5
    #: sync_sites: a new vendor organisation whose device hostnames overlap an
    #: existing site's by at least this share is mapped onto that site ("linked
    #: by device overlap"); below it, the site is created and listed as a
    #: possible duplicate.
    sync_overlap_share: Annotated[float, Field(gt=0, le=1)] = 0.5

    # --- Device identity -------------------------------------------------
    #: When on, the coverage board, the review's coverage findings and the
    #: device lookup tools resolve a machine through `devices` and its vendor
    #: links (app/devices/) instead of joining inventories inline. Off, nothing
    #: reads the tables; the daily resolution may still fill them.
    device_identity_enabled: bool = False
    #: A device none of the vendors has returned for this many days is retired.
    device_retire_days: Annotated[int, Field(ge=1)] = 90

    # --- Authentication -------------------------------------------------
    # Sessions live in a cookie that script cannot read, with two expiries:
    # `idle` closes a session left alone, `absolute` is never extended.
    session_cookie_name: str = "inputiv_session"
    session_idle_minutes: Annotated[int, Field(ge=1)] = 60
    session_absolute_hours: Annotated[int, Field(ge=1)] = 12
    #: Off only for local http development. A Secure cookie is not sent over
    #: http, so leaving this true locally means never being signed in; leaving
    #: it false anywhere else means the cookie travels in clear text. Refused
    #: at start-up outside `local`.
    session_cookie_secure: bool = True
    #: The path the browser is sent to when a session is missing or expired.
    #: Named here so a future front end can move without a code change.
    login_path: str = "/login"

    # Sign-in throttling. The lockout is per user and the window is per client
    # address; both are needed, because one attacker with many addresses is a
    # different problem from one address trying many accounts.
    login_max_failures: Annotated[int, Field(ge=1)] = 5
    login_lockout_minutes: Annotated[int, Field(ge=1)] = 15
    login_rate_limit_per_minute: Annotated[int, Field(ge=1)] = 10

    # argon2id cost. Raising these is safe at any time: an existing hash
    # carries the parameters it was made with and is upgraded on next sign-in.
    password_hash_time_cost: Annotated[int, Field(ge=1)] = 3
    password_hash_memory_kib: Annotated[int, Field(ge=8192)] = 65536
    password_hash_parallelism: Annotated[int, Field(ge=1)] = 4

    #: Where this deployment answers, as somebody outside it would type it.
    #: Used to build links we send to people — today the invite link, which is
    #: useless as a bare token. It is a setting and not a guess from a request:
    #: a link is built by the CLI as often as by a request, and a `Host` header
    #: is something a caller controls.
    public_base_url: str = "http://localhost:8000"

    #: How long an invite link is good for.
    invite_expiry_hours: Annotated[int, Field(ge=1)] = 72
    #: The issuer an authenticator app shows beside the account.
    totp_issuer: str = "Inputiv"
    #: Inputiv staff must hold a confirmed second factor. On, a staff account
    #: without one may sign in and reach only its own account (who am I,
    #: enrolment, sign-out, the page shells) until it has enrolled; every
    #: client-scoped route answers 403 with `X-Second-Factor: enrol`. Off is
    #: for a developer's machine and is refused outside `local`.
    totp_required_for_inputiv: bool = False

    # --- Slack ----------------------------------------------------------
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None

    @field_validator("report_web_search_allowed_domains")
    @classmethod
    def _no_vendors_in_reports(cls, value: str) -> str:
        domains = _domains(value)
        if not domains:
            raise ValueError(
                "REPORT_WEB_SEARCH_ALLOWED_DOMAINS must name at least one domain: report "
                "research is never unrestricted"
            )
        vendors = [domain for domain in domains if is_vendor_domain(domain)]
        if vendors:
            raise ValueError(
                f"REPORT_WEB_SEARCH_ALLOWED_DOMAINS may not include vendor domains: {vendors}. "
                "Reports cite regulators, standards bodies and industry associations."
            )
        return value

    @field_validator("ticket_web_search_allowed_domains")
    @classmethod
    def _ticket_domains_named(cls, value: str) -> str:
        if not _domains(value):
            raise ValueError(
                "TICKET_WEB_SEARCH_ALLOWED_DOMAINS must name at least one domain: a plan's "
                "research is never unrestricted"
            )
        return value

    @field_validator("public_base_url")
    @classmethod
    def _base_url_shape(cls, value: str) -> str:
        """A URL somebody can paste, with no trailing slash to double up on."""
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError(f"PUBLIC_BASE_URL must start with http:// or https://; got {value!r}")
        return cleaned

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Accept a bare ``postgresql://`` URL, or Render's ``postgres://``, and
        point it at asyncpg."""
        if value.startswith("postgresql+asyncpg://"):
            return value
        if value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value.removeprefix("postgresql://")
        if value.startswith("postgres://"):
            return "postgresql+asyncpg://" + value.removeprefix("postgres://")
        raise ValueError(
            "database_url must be a postgresql:// or postgresql+asyncpg:// URL; "
            f"got {value.split('://', 1)[0]!r}"
        )

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return level

    @model_validator(mode="after")
    def _links_are_https_off_the_laptop(self) -> Settings:
        """An invite link carries a one-time token; it does not travel in clear."""
        if self.environment != "local" and not self.public_base_url.startswith("https://"):
            raise ValueError(
                "PUBLIC_BASE_URL must be https outside ENVIRONMENT=local: an invite link "
                f"carries a one-time token. This is {self.environment!r} with "
                f"{self.public_base_url!r}."
            )
        return self

    def url_for(self, path: str) -> str:
        """An absolute URL for a path on this deployment."""
        return f"{self.public_base_url}/{path.lstrip('/')}"

    @model_validator(mode="after")
    def _cookies_are_secure_off_the_laptop(self) -> Settings:
        """A session cookie may only travel in clear text on a developer's machine."""
        if not self.session_cookie_secure and self.environment != "local":
            raise ValueError(
                "SESSION_COOKIE_SECURE may only be false when ENVIRONMENT is 'local'. "
                f"This is {self.environment!r}, where the session cookie must be Secure."
            )
        return self

    @model_validator(mode="after")
    def _laptop_options_stay_on_the_laptop(self) -> Settings:
        """Outside `local`, the options that only make sense on a developer's
        machine are refused at start-up rather than discovered in production:
        debug output, SQL echo, a second factor staff may skip, and a public
        or database address on the local machine."""
        if self.environment == "local":
            return self
        problems: list[str] = []
        if self.debug:
            problems.append("DEBUG must be false")
        if self.database_echo:
            problems.append("DATABASE_ECHO must be false")
        if not self.totp_required_for_inputiv:
            problems.append("TOTP_REQUIRED_FOR_INPUTIV must be true: staff hold a second factor")
        for label, url in (
            ("PUBLIC_BASE_URL", self.public_base_url),
            ("DATABASE_URL", self.database_url),
        ):
            host = (urlparse(url).hostname or "").lower()
            if host in LOCAL_HOSTS or host.endswith((".local", ".localhost")):
                problems.append(f"{label} points at the local machine ({host})")
        if problems:
            raise ValueError(
                f"ENVIRONMENT is {self.environment!r}, and: " + "; ".join(problems) + "."
            )
        return self

    @model_validator(mode="after")
    def _check_active_encryption_key(self) -> Settings:
        if self.credential_encryption_keys and (
            self.credential_encryption_active_key_id not in self.credential_encryption_keys
        ):
            raise ValueError(
                "credential_encryption_active_key_id "
                f"{self.credential_encryption_active_key_id!r} is not present in "
                "credential_encryption_keys"
            )
        return self

    def connector_timeout_for(self, connector: str) -> float:
        """The read timeout for one connector: its own setting, else the shared one."""
        own: float | None = getattr(self, f"{connector}_timeout_seconds", None)
        return own if own is not None else self.connector_timeout_seconds

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"

    @property
    def web_search_domains(self) -> list[str]:
        """`WEB_SEARCH_ALLOWED_DOMAINS` as a list. Empty means unrestricted."""
        return _domains(self.web_search_allowed_domains)

    @property
    def ticket_web_search_domains(self) -> list[str]:
        """`TICKET_WEB_SEARCH_ALLOWED_DOMAINS` as a list. Never empty."""
        return _domains(self.ticket_web_search_allowed_domains)

    @property
    def report_web_search_domains(self) -> list[str]:
        """`REPORT_WEB_SEARCH_ALLOWED_DOMAINS` as a list. Never empty, never a vendor."""
        return _domains(self.report_web_search_allowed_domains)


def _domains(raw: str | None) -> list[str]:
    parts = [part.strip().lower() for part in (raw or "").replace(";", ",").split(",")]
    # The API takes bare domains: a scheme or path would match nothing.
    cleaned = [p.removeprefix("https://").removeprefix("http://").strip("/") for p in parts]
    return list(dict.fromkeys(domain for domain in cleaned if domain))


def is_vendor_domain(domain: str) -> bool:
    # An allowlist entry may carry a path ("example.com/blog"): judge the host alone.
    host = domain.lower().split("/", 1)[0].removeprefix("www.")
    return any(host == vendor or host.endswith("." + vendor) for vendor in REPORT_VENDOR_DOMAINS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment

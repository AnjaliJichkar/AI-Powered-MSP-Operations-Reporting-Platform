"""Closed value sets stored in the database as text with a check constraint."""

from __future__ import annotations

from enum import StrEnum


class ConnectorName(StrEnum):
    """Every connector the engine supports.

    Adding a connector means adding a value here and a migration. That is
    deliberate: credentials and mappings cannot be created for a connector the
    engine does not know about.
    """

    NINJAONE = "ninjaone"
    AUTOTASK = "autotask"
    BITDEFENDER = "bitdefender"
    COVE = "cove"
    CONNECTSECURE = "connectsecure"
    BLACKPOINT = "blackpoint"
    AUTOTASK_NOTES = "autotask_notes"
    """The automation engine's note writer: a second Autotask API user, held
    per client like any other credential, that can add an internal ticket note
    and nothing else. Not a connector the factory builds and not a source of
    tools; it exists so the one write path has credentials of its own."""
    MICROSOFT365 = "microsoft365"


#: What each connector is called in front of a person. One spelling, here,
#: because a vendor's name is printed by the header, the Coverage board, the
#: Devices table, the Sites table, Integrations and the ticket card, and a
#: page that prints `connectsecure` where another prints `ConnectSecure` is
#: two products. The key is the enum's own value, so a connector added above
#: without a label here is a `KeyError` on the first page that names it rather
#: than a slug quietly reaching a customer.
VENDOR_LABELS: dict[str, str] = {
    ConnectorName.NINJAONE: "NinjaOne",
    ConnectorName.AUTOTASK: "Autotask",
    ConnectorName.AUTOTASK_NOTES: "Autotask",
    ConnectorName.BITDEFENDER: "Bitdefender",
    ConnectorName.COVE: "Cove",
    ConnectorName.CONNECTSECURE: "ConnectSecure",
    ConnectorName.BLACKPOINT: "Blackpoint",
    ConnectorName.MICROSOFT365: "Microsoft 365",
}


def vendor_label(key: str) -> str:
    """A connector's name for a reader. Unknown keys read as themselves.

    Unknown rather than raising, because this is called on strings that came
    out of stored rows — an enrichment's `not_assessed` keys, say — and a page
    that cannot draw because somebody wrote an unexpected key into a snapshot
    is worse than a page that prints the key.
    """
    return VENDOR_LABELS.get(key, key)


class Interface(StrEnum):
    """Where a request entered the engine."""

    SLACK = "slack"
    API = "api"
    CLI = "cli"
    SYSTEM = "system"
    EVAL = "eval"
    """Scripted harness runs (`scripts/selection_check.py`), never an engineer.
    Logged like any run; excluded from usage figures with `interface <> 'eval'`."""


class AuditEventType(StrEnum):
    """The kind of step being recorded in ``audit_log``."""

    RUN_STARTED = "run_started"
    PROMPT = "prompt"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RUN_FINISHED = "run_finished"
    ERROR = "error"
    WEB_SEARCH = "web_search"
    """One server-side web search the model made: its query and the URLs it read."""
    CONVERSATION_STARTED = "conversation_started"
    CONVERSATION_ARCHIVED = "conversation_archived"
    CONTEXT_TRIMMED = "context_trimmed"
    """History exceeded the context budget and was trimmed before the model saw it."""
    REPORT_GENERATED = "report_generated"
    """A business review was drafted: which site and period, how each slide came out."""
    REPORT_REVIEWED = "report_reviewed"
    """An engineer edited, added to, confirmed or approved part of a review."""
    REPORT_RENDERED = "report_rendered"
    """A review was rendered to a deck, or the render gate refused it and why."""
    REPORT_CONFIG_CHANGED = "report_config_changed"
    """A franchisee's report setting changed in a way that makes its renders stale
    (`show_people_names`): the setting, the value and the reviews marked changed."""
    EVENTS_COLLECTED = "events_collected"
    """The daily collector read one site's events from a vendor: the window and the counts."""
    LOGIN = "login"
    """A sign-in attempt. `success`, or `denied` with the reason and never the password."""
    LOGOUT = "logout"
    CLIENT_SWITCHED = "client_switched"
    """Inputiv staff pointed their session at a different franchisee. Both clients named."""
    ACCESS_DENIED = "access_denied"
    """A request refused by the scope resolver: the route, the role and what was missing."""
    USER_INVITED = "user_invited"
    INVITE_ACCEPTED = "invite_accepted"
    USER_CHANGED = "user_changed"
    """A user's role, status, password or second factor was changed, and by whom."""
    COVERAGE_EXCLUDED = "coverage_excluded"
    """A device was excluded from a coverage tool, or the exclusion withdrawn: the
    device, the tool, the reason, and who."""
    COVERAGE_SNAPSHOT = "coverage_snapshot"
    """The daily collector took the day's coverage snapshot for a client: the counts."""
    DEVICES_RESOLVED = "devices_resolved"
    """The daily device resolution ran for a client: devices created, links by pass,
    unresolved and retired counts."""
    DEVICE_LINKED = "device_linked"
    """A person linked a vendor record to a device, or unlinked one, with the reason."""
    TICKET_DEVICE_CHOSEN = "ticket_device_chosen"
    """An engineer chose or corrected the device a ticket workspace concerns."""
    TICKET_PLAN_GENERATED = "ticket_plan_generated"
    """A remediation plan was generated for a ticket: the version, steps kept and dropped."""
    TICKET_PLAN_REVIEWED = "ticket_plan_reviewed"
    """An engineer accepted, edited or rejected a plan step."""
    TICKET_OUTCOME_RECORDED = "ticket_outcome_recorded"
    """A ticket with a plan closed and its engineer said which steps resolved it."""
    AUTOPILOT_RUN_STARTED = "autopilot_run_started"
    """A shadow run began: the client, the window, the model, the prompt version
    and the config it will be comparable by."""
    AUTOPILOT_RUN_FINISHED = "autopilot_run_finished"
    """A shadow run ended: tickets seen, how many each path handled, tokens and
    cost, and whether a cap stopped it early."""
    AUTOPILOT_RUN_FAILED = "autopilot_run_failed"
    """A shadow run raised. Written in its own transaction, like any failed
    read, so a run that rolls back still says it happened."""
    AUTOPILOT_LABELLED = "autopilot_labelled"
    """Somebody judged a ticket's origin or a group, withdrew a judgement, or
    imported a filled-in sheet: the set, the item and the answer, never the
    note. A label is ground truth the gate is read against, so who said what
    and when is part of the record."""
    AUTOPILOT_SUGGESTION_REVIEWED = "autopilot_suggestion_reviewed"
    """An engineer marked a suggestion correct, changed or wrong, or answered a
    blind check before anything was revealed. The payload's `action` says
    which; a blind answer is the thing the gate scores, so it is recorded with
    the same weight as a label."""
    AUTOMATION_RUN = "automation_run"
    """A workflow run changed state: drafted, dry-run, submitted, approved,
    started, completed, failed, resumed, cancelled, abandoned or undone. The
    payload's `action` says which, with who did it; the approval row carries
    the client's authoriser as well as the engineer who recorded them."""
    AUTOMATION_STEP = "automation_step"
    """One check, action, verification or undo of one step of a run, with the
    before and after values where there are any. Every step of every run
    writes at least three of these — precondition, action, verify — so the
    record of what an automation did is the record, not a summary of one."""
    AUTOMATION_CONTROL = "automation_control"
    """A per-client, per-workflow switch or the client's kill switch was
    thrown or cleared, and by whom."""
    AUTOMATION_NOTE = "automation_note"
    """A Desk note on a ticket: built from facts read from the tools, posted to
    Autotask (by an engineer or automatically), marked not useful, or a post
    that failed. The payload carries the note's id, the ticket, the workflow,
    the facts with when each was read, and who."""


class OrganisationKind(StrEnum):
    """Who an organisation is. The two kinds see different things.

    `inputiv` is us: staff who work across every franchisee and switch between
    them explicitly. `franchisee` is one MSP we work for, bound to exactly one
    `Client`, whose people never see another franchisee's anything.
    """

    INPUTIV = "inputiv"
    FRANCHISEE = "franchisee"


class Role(StrEnum):
    """What a user may do. One role per user; the role names its organisation kind.

    An `inputiv_*` role only exists in the Inputiv organisation and a
    `franchisee_*` role only in a franchisee's, enforced by a check constraint
    on `users` as well as by the scope resolver.
    """

    INPUTIV_ADMIN = "inputiv_admin"
    """Everything, including creating organisations, users and invites."""
    INPUTIV_ENGINEER = "inputiv_engineer"
    """Chat, threads, business reviews and the review screen, for any client
    they have switched to. Not user administration."""
    FRANCHISEE_ADMIN = "franchisee_admin"
    """Their own client only: chat, their threads, released reviews, their audit."""
    FRANCHISEE_TECHNICIAN = "franchisee_technician"
    """The people who fix things, on their own client: Ask, the device pages, the
    coverage board including refreshing it and excluding a device, and the ticket
    workspace. Not accounts, not approving or releasing a review; reviews are read
    exactly as a viewer reads them, released only."""
    FRANCHISEE_VIEWER = "franchisee_viewer"
    """Their own client's released reviews, and nothing that runs a model."""

    @property
    def organisation_kind(self) -> OrganisationKind:
        return (
            OrganisationKind.INPUTIV
            if self.value.startswith("inputiv_")
            else OrganisationKind.FRANCHISEE
        )

    @property
    def is_inputiv(self) -> bool:
        return self.organisation_kind is OrganisationKind.INPUTIV


class UserStatus(StrEnum):
    """Whether a user may sign in at all."""

    INVITED = "invited"
    """Created by an invite and not yet accepted: no password, cannot sign in."""
    ACTIVE = "active"
    SUSPENDED = "suspended"
    """Kept for the audit trail; refused at login. Users are never deleted."""


class IdentityProvider(StrEnum):
    """Where a user's credentials live.

    Only `local` exists today. The column and the per-organisation default are
    here so an external provider (Microsoft Entra) can be added for one
    organisation without migrating the others: such a user gets
    `identity_provider` set, `password_hash` null, and `external_subject`
    holding the provider's stable subject claim.
    """

    LOCAL = "local"
    ENTRA = "entra"


class MessageRole(StrEnum):
    """Who a stored conversation message is from, as the Messages API names them."""

    USER = "user"
    ASSISTANT = "assistant"


class AuditOutcome(StrEnum):
    """How a step ended."""

    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    TIMEOUT = "timeout"
    CAPPED = "capped"
    IN_PROGRESS = "in_progress"


class FeedbackRating(StrEnum):
    """An engineer's verdict on one answer."""

    UP = "up"
    DOWN = "down"

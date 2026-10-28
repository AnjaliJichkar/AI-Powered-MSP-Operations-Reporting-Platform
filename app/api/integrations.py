"""GET /integrations/list: which tools this client is on, and when each last answered.

The header's row of vendor marks says whether a tool is **set up**, which is a
different question from whether it is working, and it says so in its own
docstring. This is where the second question is answered.

**Nothing here calls a vendor.** The page reports what the record already
shows: whether a credential exists, how many of the franchisee's customers are
mapped on that tool, and the last time a read of it succeeded or failed —
every one of those a row we wrote at the time. A "check it now" button would
be a live call per connector on a page load, six vendors deep, and the answer
it gave would be about this moment rather than about the work the product
actually does. A tool that has not been read since the collector last ran is a
tool nobody has asked anything of, and the page says that in those words
rather than colouring it green.

Scoped like everything else: the client comes from the session, and a
credential's secret half is never read here — only whether a row exists.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CLIENT_READ
from app.auth.scope import Scope
from app.connectors.factory import why_not_configured
from app.db.session import get_session
from app.models.audit_log import AuditLog
from app.models.enums import AuditOutcome, ConnectorName, Interface, vendor_label
from app.models.tool_mapping import ToolMapping

router = APIRouter(tags=["integrations"])

#: The connectors this page reports on, in the order it shows them. A tool is
#: listed whether or not this client has it: "not set up" is an answer, and a
#: page that leaves out what you do not have cannot be read as a list of what
#: there is.
LISTED: tuple[ConnectorName, ...] = (
    ConnectorName.NINJAONE,
    ConnectorName.AUTOTASK,
    ConnectorName.BITDEFENDER,
    ConnectorName.CONNECTSECURE,
    ConnectorName.COVE,
    ConnectorName.MICROSOFT365,
)

#: What each one is for, in a reader's words rather than ours. One line, and
#: the card prints it as the category above the sentence.
ROLE: dict[ConnectorName, str] = {
    ConnectorName.NINJAONE: "Monitoring & patching",
    ConnectorName.AUTOTASK: "PSA: tickets & contracts",
    ConnectorName.BITDEFENDER: "Endpoint protection",
    ConnectorName.CONNECTSECURE: "Vulnerability scanning",
    ConnectorName.COVE: "Backup",
    ConnectorName.MICROSOFT365: "Directory & licensing",
}

#: One sentence on what Desk uses the tool for. Not a description of the
#: vendor's product: a description of our use of it.
PURPOSE: dict[ConnectorName, str] = {
    ConnectorName.NINJAONE: (
        "Desk reads the fleet from here: which machines exist, what they are "
        "running, and what is waiting to be patched."
    ),
    ConnectorName.AUTOTASK: (
        "Desk reads tickets, the customers they belong to, and the contracts and time behind them."
    ),
    ConnectorName.BITDEFENDER: (
        "Desk reads which machines have a protection agent, what it is set to "
        "do, and what it has found."
    ),
    ConnectorName.CONNECTSECURE: (
        "Desk reads vulnerability findings, the fixes for them, and when each "
        "machine was last scanned."
    ),
    ConnectorName.COVE: ("Desk reads which machines are backed up and how the last backups went."),
    ConnectorName.MICROSOFT365: (
        "Desk reads one customer's directory: who is in it, what they are "
        "licensed for, and who has a second factor."
    ),
}

#: What Desk actually reads from the tool, as the chips on its card. Each one
#: names a read the connector makes — never a capability the vendor has and we
#: do not use, because a chip for that is a claim the product does not keep.
READS: dict[ConnectorName, tuple[str, ...]] = {
    ConnectorName.NINJAONE: ("Devices", "Patching", "Alerts", "Volumes", "Software"),
    ConnectorName.AUTOTASK: ("Tickets", "Customers", "Contracts", "Time entries"),
    ConnectorName.BITDEFENDER: ("Endpoints", "Modules", "Detections", "Licence"),
    ConnectorName.CONNECTSECURE: ("Assets", "Vulnerabilities", "Fixes", "Scan dates"),
    ConnectorName.COVE: ("Devices", "Backup status", "Microsoft 365 backup"),
    ConnectorName.MICROSOFT365: ("Accounts", "Licences", "Second factor", "Groups"),
}

#: How read-only is kept, per tool. Two kinds of answer, and the difference is
#: the point: for three of these the vendor cannot scope a key to reading, so
#: our own client refuses everything else before a request is built.
SCOPE: dict[ConnectorName, str] = {
    ConnectorName.NINJAONE: (
        "Read-only by the credential's own scope. The connector has no method that writes."
    ),
    ConnectorName.AUTOTASK: (
        "The API user Desk reads with is read-only. Adding an internal note to "
        "a ticket uses a second API user, which is not this one."
    ),
    ConnectorName.BITDEFENDER: (
        "A GravityZone key can start scans with the same key that lists them, "
        "so reading is not left to the vendor: every call is checked against a "
        "list of read methods and anything else is refused before a request is "
        "built."
    ),
    ConnectorName.CONNECTSECURE: (
        "The key is a user and its role is the write surface, so Desk sends "
        "exactly two kinds of request: the sign-in, and GETs on an exact list "
        "of read paths. Anything else is refused before any HTTP."
    ),
    ConnectorName.COVE: (
        "Read-only by the credential's own scope, and anything but a read is "
        "refused before a request is built."
    ),
    ConnectorName.MICROSOFT365: (
        "Any method but GET is refused before a request is built. Graph has "
        "read-only POST endpoints and the allowlist for them is empty, because "
        "none is needed. No write permission is requested."
    ),
}


class Setup(BaseModel):
    """How a tool is connected, and what connects it."""

    how: str
    #: Shown to Inputiv staff only: a franchisee cannot run it, and a command
    #: they cannot use is noise on their screen.
    command: str | None = None
    docs: str | None = None


SETUP: dict[ConnectorName, tuple[str, str, str]] = {
    ConnectorName.NINJAONE: (
        "Client credentials for the franchisee's own tenant. Their customers "
        "are then discovered from the vendor's organisation list.",
        "python scripts/manage_client.py set-credentials --client <slug> "
        "--connector ninjaone\npython scripts/sync_sites.py --client <slug>",
        "docs/api-notes/ninjaone.md",
    ),
    ConnectorName.AUTOTASK: (
        "An API user in the franchisee's own Autotask, on their own zone. "
        "Their customers are discovered from the company list.",
        "python scripts/manage_client.py set-credentials --client <slug> "
        "--connector autotask\npython scripts/sync_sites.py --client <slug>",
        "docs/api-notes/autotask.md",
    ),
    ConnectorName.BITDEFENDER: (
        "A GravityZone API key, asked for least-privilege: the five APIs Desk "
        "uses and no more. Companies become customers through the site sync.",
        "python scripts/validate_connector.py --check-bitdefender\n"
        "python scripts/sync_sites.py --client <slug>",
        "docs/api-notes/bitdefender.md",
    ),
    ConnectorName.CONNECTSECURE: (
        "An API user, which should be a viewer. The pre-flight reads the key's "
        "own role and warns, naming it, when it is not.",
        "python scripts/validate_connector.py --check-connectsecure\n"
        "python scripts/sync_sites.py --client <slug>",
        "docs/api-notes/connectsecure.md",
    ),
    ConnectorName.COVE: (
        "Cove credentials for the franchisee's own account. Their customers "
        "are matched to the names Cove holds.",
        "python scripts/manage_client.py set-credentials --client <slug> "
        "--connector cove\npython scripts/sync_sites.py --client <slug>",
        "docs/api-notes/cove.md",
    ),
    ConnectorName.MICROSOFT365: (
        "Our multi-tenant application, consented separately by each customer. "
        "A directory token is for one tenant and cannot enumerate the others, "
        "so each customer's tenant is mapped by hand rather than synced.",
        "python scripts/manage_client.py map-site --client <slug> "
        "--site <site> --connector microsoft365 --external-id <tenant id>",
        "docs/m365-connector.md",
    ),
}

#: A failure, in words. The class name is what an engineer reading the log
#: wants and is the wrong thing to put on a page: `UpstreamError` on a
#: franchisee's screen is a stack trace with the stack taken off. The raw name
#: is kept, and shown in the drawer to Inputiv staff only.
ERROR_WORDS: dict[str, str] = {
    "AuthError": "refused the credential",
    "RateLimited": "asked us to slow down",
    "NotFound": "did not have something we asked for",
    "UpstreamError": "did not answer",
    "TimeoutError": "took too long to answer",
    "ArgumentRejected": "rejected a request",
    "ValidationError": "answered in a shape we did not expect",
    "ReadOnlyViolation": "was not asked: the request was stopped here, because it was not a read",
    "WriteNotAllowed": "was not asked: the request was stopped here, because it was not allowed",
    "ConnectorNotConfigured": "is not set up for that customer",
    "UnknownSite": "was asked about a customer this franchisee does not have",
    "SyncConflict": "returned an organisation that belongs to another franchisee",
    "ToolArgumentError": "was asked a question in a shape it could not answer",
    # Live, over this installation's whole record: UpstreamError 39,
    # ArgumentRejected 2, ValidationError 2, UnknownClient 1. The map is the
    # kinds that happen, not every class that exists.
    "UnknownClient": "was read for a franchisee we hold no credentials for",
}


def error_words(error_type: str | None, vendor: str) -> str | None:
    """A failure as a sentence, with the vendor named and no class in it."""
    if not error_type:
        return None
    words = ERROR_WORDS.get(error_type)
    return f"{vendor} {words}." if words else f"The read of {vendor} did not work."


#: Microsoft 365 is mapped by hand, one tenant per customer, because a Graph
#: token is for one directory and cannot enumerate the others. Everything else
#: is discovered by `sync_sites`. The difference matters on this page: zero
#: customers on a synced tool means the sync has not been run, and zero on this
#: one means nobody has mapped a tenant yet.
BY_HAND: frozenset[ConnectorName] = frozenset({ConnectorName.MICROSOFT365})


class ReadRow(BaseModel):
    """One read of this tool, as the record holds it."""

    at: dt.datetime
    ok: bool
    tool: str | None = Field(description="The tool function that made the read.")
    words: str | None = Field(description="What went wrong, in a sentence, where it did.")
    error: str | None = Field(
        default=None, description="The failure's class name. Inputiv staff only."
    )


class IntegrationView(BaseModel):
    key: str
    role: str
    purpose: str = Field(description="One sentence on what Desk uses it for.")
    reads: list[str] = Field(description="What Desk actually reads from it.")
    scope: str = Field(description="How read-only is kept for this tool.")
    setup: Setup
    configured: bool = Field(description="A credential exists for this client and connector.")
    mapped_by_hand: bool
    sites: int = Field(description="How many of this client's customers are mapped on it.")
    last_ok: dt.datetime | None = Field(description="The last read of it that succeeded.")
    last_failed: dt.datetime | None = Field(description="The last read of it that did not.")
    last_error: str | None = Field(
        default=None, description="The last failure's class name. Inputiv staff only."
    )
    last_error_words: str | None = Field(
        default=None, description="The last failure, as a sentence."
    )
    recent: list[ReadRow] = Field(
        default_factory=list, description="The most recent reads, newest first, for the drawer."
    )


class IntegrationList(BaseModel):
    integrations: list[IntegrationView]
    #: How far back the read history was looked at, in words, so the page can
    #: say "no reads in the last 30 days" rather than "never".
    window_days: int
    #: Whether this session may see a failure's class name.
    staff: bool


#: How far back to look for a read. A connector nothing has asked anything of
#: in a month is not one this page should call healthy on the strength of a
#: success in August.
WINDOW_DAYS = 30

#: How many recent reads the drawer lists per tool. Enough to see a pattern,
#: few enough that the query stays one read of the tail of the log.
RECENT_PER_TOOL = 6


@router.get("/integrations/list", response_model=IntegrationList)
async def list_integrations(
    db: Annotated[AsyncSession, Depends(get_session)],
    scope: Annotated[Scope, Depends(CLIENT_READ)],
) -> IntegrationList:
    """Every connector, whether this client has it, and when it last answered."""
    client_id = scope.require_client()
    since = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=WINDOW_DAYS)

    # **The factory's own answer, not a credential row.** A demo client holds
    # no credential at all and its connectors work; a live client with a row
    # for a connector we cannot build is not set up either. `why_not_configured`
    # is the one place that question is answered — the ticket panel asks it to
    # tell "not set up for this customer" from "the read failed" — and it opens
    # and closes a connector without making a request.
    configured = {
        connector for connector in LISTED if await why_not_configured(client_id, connector) is None
    }

    site_counts = dict(
        (
            await db.execute(
                select(ToolMapping.connector_name, func.count(func.distinct(ToolMapping.site_id)))
                .where(ToolMapping.client_id == client_id, ToolMapping.site_id.is_not(None))
                .group_by(ToolMapping.connector_name)
            )
        ).all()
    )

    # One row per connector per outcome: the most recent of each, and the kind
    # of the last failure. `interface <> 'eval'` is the rule every usage figure
    # in the product follows — the selection check's runs are not work anybody
    # did (CLAUDE.md, "Tool selection check").
    reads = (
        await db.execute(
            select(
                AuditLog.connector_name,
                AuditLog.outcome,
                func.max(AuditLog.created_at),
            )
            .where(
                AuditLog.client_id == client_id,
                AuditLog.connector_name.is_not(None),
                AuditLog.created_at >= since,
                AuditLog.interface != Interface.EVAL,
            )
            .group_by(AuditLog.connector_name, AuditLog.outcome)
        )
    ).all()
    last_ok: dict[ConnectorName, dt.datetime] = {}
    last_failed: dict[ConnectorName, dt.datetime] = {}
    for connector, outcome, when in reads:
        # Anything that is not a success is a failure for this purpose: an
        # error, a timeout, a refusal. `in_progress` is a row we wrote before
        # the answer came back and is left out of both.
        if outcome == AuditOutcome.IN_PROGRESS:
            continue
        bucket = last_ok if outcome == AuditOutcome.SUCCESS else last_failed
        if connector is not None and (connector not in bucket or when > bucket[connector]):
            bucket[connector] = when

    # The tail of the log, bucketed per tool: the drawer's "last reads" and the
    # kind of the last failure come out of the same read rather than two.
    staff = scope.role.value.startswith("inputiv")
    tail = (
        await db.execute(
            select(
                AuditLog.connector_name,
                AuditLog.outcome,
                AuditLog.error_type,
                AuditLog.tool_name,
                AuditLog.created_at,
            )
            .where(
                AuditLog.client_id == client_id,
                AuditLog.connector_name.is_not(None),
                AuditLog.created_at >= since,
                AuditLog.interface != Interface.EVAL,
                AuditLog.outcome != AuditOutcome.IN_PROGRESS,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(400)
        )
    ).all()

    errors: dict[ConnectorName, str] = {}
    recent: dict[ConnectorName, list[ReadRow]] = {}
    for connector, outcome, error_type, tool_name, when in tail:
        if connector is None:
            continue
        ok = outcome == AuditOutcome.SUCCESS
        if not ok and connector not in errors and error_type:
            errors[connector] = error_type
        rows = recent.setdefault(connector, [])
        if len(rows) < RECENT_PER_TOOL:
            vendor = vendor_label(connector.value)
            rows.append(
                ReadRow(
                    at=when,
                    ok=ok,
                    tool=tool_name,
                    words=None if ok else error_words(error_type, vendor),
                    error=error_type if staff else None,
                )
            )

    return IntegrationList(
        window_days=WINDOW_DAYS,
        staff=staff,
        integrations=[
            IntegrationView(
                key=connector.value,
                role=ROLE[connector],
                purpose=PURPOSE[connector],
                reads=list(READS[connector]),
                scope=SCOPE[connector],
                setup=Setup(
                    how=SETUP[connector][0],
                    command=SETUP[connector][1] if staff else None,
                    docs=SETUP[connector][2] if staff else None,
                ),
                configured=connector in configured,
                mapped_by_hand=connector in BY_HAND,
                sites=int(site_counts.get(connector, 0)),
                last_ok=last_ok.get(connector),
                last_failed=last_failed.get(connector),
                last_error=errors.get(connector) if staff else None,
                last_error_words=error_words(errors.get(connector), vendor_label(connector.value)),
                recent=recent.get(connector, []),
            )
            for connector in LISTED
        ],
    )
